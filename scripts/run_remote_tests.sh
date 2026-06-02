#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/run_remote_tests.sh USER@GPU_HOST /absolute/remote/path [real-config-name]

Runs the GPU-box validation flow after scripts/rsync_to_gpu.sh.

The default flow is safe on machines without vLLM/GPU access:
  - install editable dev package
  - run the no-GPU pytest suite
  - run CLI preview/list checks
  - print host GPU/vLLM profile diagnostics

If real-config-name is provided, the remote host also runs:
  - vllm-loader preview REAL_CONFIG
  - timeout-bound vllm-loader smoke REAL_CONFIG

Example:
  scripts/run_remote_tests.sh blackbird /srv/lab-tui
  scripts/run_remote_tests.sh blackbird /srv/lab-tui llama-3.1-8b
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" || $# -lt 2 || $# -gt 3 ]]; then
  usage
  exit $([[ $# -eq 1 ]] && [[ "${1:-}" =~ ^- ]] && echo 0 || echo 2)
fi

host="$1"
remote_path="$2"
real_config="${3:-}"
remote_timeout="${VLLM_LOADER_REMOTE_TIMEOUT:-1800}"
remote_python="${VLLM_LOADER_REMOTE_PYTHON:-auto}"
remote_venv="${VLLM_LOADER_REMOTE_VENV:-/tank/venvs/lab-tui}"
ssh_cmd=(ssh)
if [[ -n "${VLLM_LOADER_SSH_OPTS:-}" ]]; then
  # shellcheck disable=SC2206
  ssh_cmd+=(${VLLM_LOADER_SSH_OPTS})
fi
ssh_cmd+=("$host" bash -s -- "$remote_path" "$remote_timeout" "$remote_python" "$remote_venv")
if [[ -n "$real_config" ]]; then
  ssh_cmd+=("$real_config")
fi

"${ssh_cmd[@]}" <<'REMOTE'
set -euo pipefail

remote_path="$1"
remote_timeout="$2"
remote_python="${3:-auto}"
remote_venv="${4:-/tank/venvs/lab-tui}"
real_config="${5:-}"

if [[ "$remote_python" == "auto" ]]; then
  if [[ -x /tank/preproc/venv/bin/python ]]; then
    remote_python=/tank/preproc/venv/bin/python
  elif command -v python3 >/dev/null 2>&1; then
    remote_python=python3
  elif command -v python >/dev/null 2>&1; then
    remote_python=python
  else
    echo "python3 or python is required on the remote host" >&2
    exit 127
  fi
fi

cd "$remote_path"
venv_python="$remote_venv/bin/python"
venv_bin="$remote_venv/bin"
if [[ ! -x "$venv_python" ]]; then
  mkdir -p "$(dirname "$remote_venv")"
  "$remote_python" -m venv "$remote_venv"
fi
if [[ ! -x "$venv_python" ]]; then
  echo "Remote venv was not created at $remote_venv; install python3-venv/ensurepip or set VLLM_LOADER_REMOTE_PYTHON to a venv-capable Python." >&2
  exit 127
fi
if ! "$venv_python" -m pip --version >/dev/null 2>&1; then
  echo "Remote venv lacks pip; install python3-venv/ensurepip or set VLLM_LOADER_REMOTE_PYTHON to a venv-capable Python: $remote_python" >&2
  exit 127
fi
export PATH="$venv_bin:$PATH"
"$venv_python" -m pip install -e ".[dev]"
echo "== Remote host =="
hostname
"$venv_python" - <<'PY'
from vllm_loader.monitoring.gpu import sample_gpus

result = sample_gpus()
print(f"GPU unavailable={result.unavailable} note={result.note}")
for sample in result.samples:
    mig = f" mig={sample.mig_instance_id}" if sample.mig_instance_id else ""
    util = "unknown" if sample.utilization_percent is None else f"{sample.utilization_percent}%"
    print(
        "GPU "
        f"{sample.visible_index} {sample.name} {sample.uuid}{mig} "
        f"mem={sample.memory_used_mb}/{sample.memory_total_mb}MiB util={util}"
    )
PY
if command -v vllm >/dev/null 2>&1; then
  echo "== vLLM version =="
  vllm --version || true
  echo "== vLLM serve help flags =="
  vllm serve --help | sed -n '1,80p' || true
else
  echo "vllm not found on PATH; no-GPU package checks will still run"
fi
"$venv_python" -m ruff check .
"$venv_python" -m pytest -q
"$venv_bin/vllm-loader" list
"$venv_bin/vllm-loader" preview fake-child

if [[ -n "$real_config" ]]; then
  "$venv_bin/vllm-loader" preview "$real_config"
  timeout "$remote_timeout" "$venv_bin/vllm-loader" smoke "$real_config"
fi
REMOTE
