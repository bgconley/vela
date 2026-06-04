#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/run_remote_tests.sh USER@GPU_HOST /absolute/remote/path [real-config-name]

Runs the GPU-box validation flow after local commit/push; the GPU node pulls from git.

The default flow is safe on machines without vLLM/GPU access:
  - install editable dev package
  - run the no-GPU pytest suite
  - run CLI preview/list checks
  - print host GPU/vLLM profile diagnostics

If real-config-name is provided, the remote host also runs:
  - vllm-loader preview REAL_CONFIG
  - timeout-bound vllm-loader smoke-tui REAL_CONFIG

Optional real artifact validation is enabled by local environment variables:
  - VLLM_LOADER_REMOTE_BUILD_SPEC='vllm==X.Y.Z' runs build add/verify
  - VLLM_LOADER_REMOTE_BUILD_METHOD defaults to pip
  - VLLM_LOADER_REMOTE_BUILD_LABEL defaults to remote-smoke-build
  - VLLM_LOADER_REMOTE_MODEL_REPO pins a HF repo before download
  - VLLM_LOADER_REMOTE_MODEL_ID defaults to remote-smoke-model
  - VLLM_LOADER_REMOTE_MODEL_REF downloads an existing pinned entry instead
  - VLLM_LOADER_REMOTE_MODEL_REVISION optionally pins/downloads a revision

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
remote_build_method="${VLLM_LOADER_REMOTE_BUILD_METHOD:-pip}"
remote_build_spec="${VLLM_LOADER_REMOTE_BUILD_SPEC:-}"
remote_build_label="${VLLM_LOADER_REMOTE_BUILD_LABEL:-remote-smoke-build}"
remote_model_id="${VLLM_LOADER_REMOTE_MODEL_ID:-remote-smoke-model}"
remote_model_repo="${VLLM_LOADER_REMOTE_MODEL_REPO:-}"
remote_model_ref="${VLLM_LOADER_REMOTE_MODEL_REF:-}"
remote_model_revision="${VLLM_LOADER_REMOTE_MODEL_REVISION:-}"
ssh_cmd=(ssh)
if [[ -n "${VLLM_LOADER_SSH_OPTS:-}" ]]; then
  # shellcheck disable=SC2206
  ssh_cmd+=(${VLLM_LOADER_SSH_OPTS})
fi
ssh_cmd+=("$host" bash -s -- "$remote_path" "$remote_timeout" "$remote_python" "$remote_venv")
if [[ -n "$real_config" ]]; then
  ssh_cmd+=("$real_config")
fi
if [[ -n "$remote_build_spec" || -n "$remote_model_repo" || -n "$remote_model_ref" ]]; then
  if [[ -z "$real_config" ]]; then
    ssh_cmd+=("")
  fi
  ssh_cmd+=(
    "$remote_build_method"
    "$remote_build_spec"
    "$remote_build_label"
    "$remote_model_id"
    "$remote_model_repo"
    "$remote_model_ref"
    "$remote_model_revision"
  )
fi

"${ssh_cmd[@]}" <<'REMOTE'
set -euo pipefail

remote_path="$1"
remote_timeout="$2"
remote_python="${3:-auto}"
remote_venv="${4:-/tank/venvs/lab-tui}"
real_config="${5:-}"
remote_build_method="${6:-pip}"
remote_build_spec="${7:-}"
remote_build_label="${8:-remote-smoke-build}"
remote_model_id="${9:-remote-smoke-model}"
remote_model_repo="${10:-}"
remote_model_ref="${11:-}"
remote_model_revision="${12:-}"

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
echo "== Remote git pull =="
git -C "$remote_path" rev-parse --is-inside-work-tree >/dev/null
git -C "$remote_path" pull --ff-only origin main
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
echo "== Remote agent restart =="
"$venv_bin/vllm-loader" agent restart
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

if [[ -n "$remote_build_spec" ]]; then
  echo "== Real build install =="
  "$venv_bin/vllm-loader" build add \
    --method "$remote_build_method" \
    --label "$remote_build_label" \
    --spec "$remote_build_spec"
  "$venv_bin/vllm-loader" build verify "$remote_build_label"
fi

if [[ -n "$remote_model_repo" ]]; then
  echo "== Real model pin =="
  if [[ -n "$remote_model_revision" ]]; then
    "$venv_bin/vllm-loader" model pin "$remote_model_id" \
      --repo-id "$remote_model_repo" \
      --revision "$remote_model_revision"
  else
    "$venv_bin/vllm-loader" model pin "$remote_model_id" \
      --repo-id "$remote_model_repo"
  fi
  remote_model_ref="$remote_model_id"
fi

if [[ -n "$remote_model_ref" ]]; then
  echo "== Real model download =="
  if [[ -n "$remote_model_revision" ]]; then
    "$venv_bin/vllm-loader" model download "$remote_model_ref" \
      --revision "$remote_model_revision"
  else
    "$venv_bin/vllm-loader" model download "$remote_model_ref"
  fi
  "$venv_bin/vllm-loader" model verify "$remote_model_ref"
fi

if [[ -n "$real_config" ]]; then
  "$venv_bin/vllm-loader" preview "$real_config"
  timeout "$remote_timeout" "$venv_bin/vllm-loader" smoke-tui "$real_config"
fi
REMOTE
