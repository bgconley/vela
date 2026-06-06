#!/usr/bin/env bash
set -euo pipefail

# Foreground Docker launcher for the Blackbird Qwen3.6 27B BF16 lane.
#
# Mirrors scripts/blackbird_qwen36_vllm_foreground.sh (the FP8 lane) so vela can
# supervise it as an attached child: it runs the pinned vLLM container, streams
# the container logs to the TUI, and stops the container when vela signals stop.
#
# Differences vs the FP8 launcher (intentional):
#   * Defaults target the BF16 weights / container / port (18002, not 18003).
#   * It does NOT pin --kv-cache-memory-bytes. BF16 weights are ~2x larger, so a
#     fixed KV cap would OOM; the KV cache is sized from --gpu-memory-utilization
#     instead, matching the proven recipe in
#     /tank/repos/infx/qwen36-27b-test/start-qwen36-bf16-rp6000-blackbird.sh.
#   * Engine flags come from the vela config (engine + extra_args) and are
#     forwarded verbatim; this wrapper only adds the Docker shell + --api-key
#     (which vela passes via the VLLM_API_KEY env, not as a flag).

CONTAINER="${CONTAINER:-qwen36-27b-bf16-rp6000-vela}"
IMAGE="${IMAGE:-vllm/vllm-openai@sha256:b13d6e5fda0785f3d41752df8513ff832f67cb231a216c76b6b4f2a515bf0046}"
PULL_IMAGE="${PULL_IMAGE:-0}"
ROOT="${ROOT:-/home/bgconley/models/qwen36-27b-bf16}"
HF_CACHE_ROOT="${HF_CACHE_ROOT:-${ROOT}/hf-cache}"
API_KEY="${VLLM_API_KEY:-EMPTY}"

# vela invokes: <wrapper> serve <model> <flags...>.  Strip the leading "serve",
# then forward the model (positional) and every flag straight to the container.
if [[ "${1:-}" == "serve" ]]; then
  shift
fi
vllm_args=("$@")

# vela passes the api key via VLLM_API_KEY (not as a flag); add it if absent.
case " ${vllm_args[*]} " in
  *" --api-key "*) ;;
  *) vllm_args+=(--api-key "$API_KEY") ;;
esac

print_dry_run() {
  printf 'container=%s\n' "$CONTAINER"
  printf 'image=%s\n' "$IMAGE"
  printf 'root=%s\n' "$ROOT"
  printf 'vllm_args='
  printf '%s ' "${vllm_args[@]}"
  printf '\n'
}

if [[ "${VELA_BLACKBIRD_DRY_RUN:-0}" == "1" ]]; then
  print_dry_run
  exit 0
fi

stop_if_exists() {
  local name="$1"
  if docker ps -a --format '{{.Names}}' | grep -qx "$name"; then
    docker update --restart=no "$name" >/dev/null 2>&1 || true
    docker stop -t 90 "$name" >/dev/null 2>&1 || true
    docker rm "$name" >/dev/null 2>&1 || true
  fi
}

mkdir -p \
  "$ROOT/hf-cache" \
  "$ROOT/vllm-cache" \
  "$ROOT/triton-cache" \
  "$ROOT/torch-compile-cache" \
  "$ROOT/logs" \
  "$ROOT/runs" \
  "$ROOT/tmp"

echo "INFO Blackbird Qwen3.6 27B BF16 foreground launcher"
echo "INFO container=$CONTAINER image=$IMAGE root=$ROOT"

# The RP6000 only fits one of these large models at a time; evict any sibling
# Qwen3.6 container (FP8 / BF16 / NVFP4 / dual) before launching.
for container in \
  "$CONTAINER" \
  qwen36-27b-bf16-rp6000-server \
  qwen36-27b-fp8-kvbf16-rp6000-server \
  qwen36-27b-fp8-kvfp8-rp6000-server \
  qwen36-27b-fp8-kvfp8-rp6000-vela \
  qwen36-27b-fp8-rp6000-server \
  qwen3-coder-next-nvfp4-server \
  qwen3-coder-next-fp8-server \
  qwen36-dual-27b-fp8-vlm \
  qwen36-dual-35b-fp8-vlm
do
  stop_if_exists "$container"
done

PORT="$(printf '%s\n' "${vllm_args[@]}" | awk '/^--port$/{getline; print; exit}')"
PORT="${PORT:-18002}"
if ss -ltn "sport = :${PORT}" | grep -q ":${PORT}"; then
  echo "ERROR port ${PORT} is already listening"
  ss -ltnp "sport = :${PORT}" || true
  exit 20
fi

if [[ "$PULL_IMAGE" == "1" ]]; then
  docker pull "$IMAGE"
else
  echo "INFO skipping docker pull; set PULL_IMAGE=1 to refresh"
fi

docker run -d \
  --name "$CONTAINER" \
  --restart no \
  --gpus all \
  --network host \
  --ipc=host \
  --shm-size=32g \
  --ulimit memlock=-1 \
  --ulimit stack=67108864 \
  -e "HF_HOME=${ROOT}/hf-cache" \
  -e "HF_HUB_CACHE=${ROOT}/hf-cache/hub" \
  -e "VLLM_CACHE_ROOT=${ROOT}/vllm-cache" \
  -e "TRITON_CACHE_DIR=${ROOT}/triton-cache" \
  -e "TORCHINDUCTOR_CACHE_DIR=${ROOT}/torch-compile-cache" \
  -e "TMPDIR=${ROOT}/tmp" \
  -e "CUDA_VISIBLE_DEVICES=0" \
  -e "PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True" \
  -e "SAFETENSORS_FAST_GPU=1" \
  -v "${ROOT}:${ROOT}" \
  "$IMAGE" \
  "${vllm_args[@]}"

logs_pid=""
cleanup() {
  local status=$?
  trap - INT TERM EXIT
  if [[ -n "$logs_pid" ]]; then
    kill "$logs_pid" >/dev/null 2>&1 || true
    wait "$logs_pid" >/dev/null 2>&1 || true
  fi
  docker update --restart=no "$CONTAINER" >/dev/null 2>&1 || true
  docker stop -t 90 "$CONTAINER" >/dev/null 2>&1 || true
  docker rm "$CONTAINER" >/dev/null 2>&1 || true
  exit "$status"
}
trap cleanup INT TERM EXIT

docker logs -f "$CONTAINER" &
logs_pid="$!"
container_code="$(docker wait "$CONTAINER" || printf '1')"
kill "$logs_pid" >/dev/null 2>&1 || true
wait "$logs_pid" >/dev/null 2>&1 || true
trap - EXIT
docker rm "$CONTAINER" >/dev/null 2>&1 || true
exit "$container_code"
