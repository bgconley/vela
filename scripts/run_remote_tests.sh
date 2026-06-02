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

ssh "$host" bash -s -- "$remote_path" "$real_config" <<'REMOTE'
set -euo pipefail

remote_path="$1"
real_config="${2:-}"

cd "$remote_path"
python -m pip install -e ".[dev]"
echo "== Remote host =="
hostname
python - <<'PY'
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
python -m ruff check .
pytest -q
vllm-loader list
vllm-loader preview fake-child

if [[ -n "$real_config" ]]; then
  vllm-loader preview "$real_config"
  timeout "${VLLM_LOADER_REMOTE_TIMEOUT:-1800}" vllm-loader smoke "$real_config"
fi
REMOTE
