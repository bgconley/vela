#!/usr/bin/env bash
set -euo pipefail

# Foreground Docker launcher for the Blackbird Qwen3.6 27B FP8 smoke lane.
# It translates the vLLM-style argv emitted by vela into the pinned
# Docker launch shape, streams container logs, and stops the container on signal.

CONTAINER="${CONTAINER:-qwen36-27b-fp8-kvfp8-rp6000-vela}"
IMAGE="${IMAGE:-vllm/vllm-openai@sha256:b13d6e5fda0785f3d41752df8513ff832f67cb231a216c76b6b4f2a515bf0046}"
PULL_IMAGE="${PULL_IMAGE:-0}"
ROOT="${ROOT:-/home/bgconley/models/qwen36-27b-fp8-rp6000}"
HF_CACHE_ROOT="${HF_CACHE_ROOT:-/home/bgconley/models/qwen36-dual-fp8-vlm/hf-cache}"

MODEL="${MODEL:-Qwen/Qwen3.6-27B-FP8}"
SERVED_MODEL_NAME="${SERVED_MODEL_NAME:-qwen36-27b-fp8-kvfp8-rp6000}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-18003}"
API_KEY="${VLLM_API_KEY:-EMPTY}"
DTYPE="${DTYPE:-auto}"
KV_CACHE_DTYPE="${KV_CACHE_DTYPE:-fp8}"
KV_CACHE_MEMORY_BYTES="${KV_CACHE_MEMORY_BYTES:-64424509440}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-262144}"
GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.97}"
MAX_NUM_SEQS="${MAX_NUM_SEQS:-16}"
MAX_NUM_BATCHED_TOKENS="${MAX_NUM_BATCHED_TOKENS:-8192}"
MAX_NUM_PARTIAL_PREFILLS="${MAX_NUM_PARTIAL_PREFILLS:-1}"
MAX_LONG_PARTIAL_PREFILLS="${MAX_LONG_PARTIAL_PREFILLS:-1}"
ATTENTION_BACKEND="${ATTENTION_BACKEND:-FLASHINFER}"
REASONING_PARSER="${REASONING_PARSER:-qwen3}"
TOOL_CALL_PARSER="${TOOL_CALL_PARSER:-qwen3_coder}"
LIMIT_MM_PER_PROMPT="${LIMIT_MM_PER_PROMPT:-{\"image\":0,\"video\":0}}"
COMPILATION_CONFIG="${COMPILATION_CONFIG:-{\"cudagraph_capture_sizes\":[1,2,4,8,16],\"cudagraph_num_of_warmups\":1}}"

TRUST_REMOTE_CODE=1
LANGUAGE_MODEL_ONLY=1
ENABLE_CHUNKED_PREFILL=1
ENABLE_PREFIX_CACHING=1
ENABLE_AUTO_TOOL_CHOICE=1
CUDAGRAPH_METRICS=1
DISABLE_UVICORN_ACCESS_LOG=1
PASSTHROUGH_ARGS=()
PASSTHROUGH_COUNT=0

if [[ "${1:-}" == "serve" ]]; then
  shift
fi
if [[ $# -gt 0 && "${1:-}" != --* ]]; then
  MODEL="$1"
  shift
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --served-model-name)
      SERVED_MODEL_NAME="$2"; shift 2 ;;
    --host)
      HOST="$2"; shift 2 ;;
    --port)
      PORT="$2"; shift 2 ;;
    --dtype)
      DTYPE="$2"; shift 2 ;;
    --kv-cache-dtype)
      KV_CACHE_DTYPE="$2"; shift 2 ;;
    --kv-cache-memory-bytes)
      KV_CACHE_MEMORY_BYTES="$2"; shift 2 ;;
    --max-model-len)
      MAX_MODEL_LEN="$2"; shift 2 ;;
    --gpu-memory-utilization)
      GPU_MEMORY_UTILIZATION="$2"; shift 2 ;;
    --max-num-seqs)
      MAX_NUM_SEQS="$2"; shift 2 ;;
    --max-num-batched-tokens)
      MAX_NUM_BATCHED_TOKENS="$2"; shift 2 ;;
    --max-num-partial-prefills)
      MAX_NUM_PARTIAL_PREFILLS="$2"; shift 2 ;;
    --max-long-partial-prefills)
      MAX_LONG_PARTIAL_PREFILLS="$2"; shift 2 ;;
    --attention-backend)
      ATTENTION_BACKEND="$2"; shift 2 ;;
    --reasoning-parser)
      REASONING_PARSER="$2"; shift 2 ;;
    --tool-call-parser)
      TOOL_CALL_PARSER="$2"; shift 2 ;;
    --limit-mm-per-prompt)
      LIMIT_MM_PER_PROMPT="$2"; shift 2 ;;
    --compilation-config)
      COMPILATION_CONFIG="$2"; shift 2 ;;
    --trust-remote-code)
      TRUST_REMOTE_CODE=1; shift ;;
    --language-model-only)
      LANGUAGE_MODEL_ONLY=1; shift ;;
    --enable-chunked-prefill)
      ENABLE_CHUNKED_PREFILL=1; shift ;;
    --enable-prefix-caching)
      ENABLE_PREFIX_CACHING=1; shift ;;
    --enable-auto-tool-choice)
      ENABLE_AUTO_TOOL_CHOICE=1; shift ;;
    --cudagraph-metrics)
      CUDAGRAPH_METRICS=1; shift ;;
    --disable-uvicorn-access-log)
      DISABLE_UVICORN_ACCESS_LOG=1; shift ;;
    --disable-access-log-for-endpoints)
      shift 2 ;;
    --enable-log-requests|--disable-log-requests|--no-enable-log-requests)
      shift ;;
    *)
      PASSTHROUGH_ARGS+=("$1")
      PASSTHROUGH_COUNT=$((PASSTHROUGH_COUNT + 1))
      shift ;;
  esac
done

vllm_args=(
  --host "$HOST"
  --port "$PORT"
  --api-key "$API_KEY"
  --model "$MODEL"
  --served-model-name "$SERVED_MODEL_NAME"
  --dtype "$DTYPE"
  --attention-backend "$ATTENTION_BACKEND"
  --kv-cache-dtype "$KV_CACHE_DTYPE"
  --kv-cache-memory-bytes "$KV_CACHE_MEMORY_BYTES"
  --max-model-len "$MAX_MODEL_LEN"
  --gpu-memory-utilization "$GPU_MEMORY_UTILIZATION"
  --max-num-seqs "$MAX_NUM_SEQS"
  --max-num-batched-tokens "$MAX_NUM_BATCHED_TOKENS"
  --max-num-partial-prefills "$MAX_NUM_PARTIAL_PREFILLS"
  --max-long-partial-prefills "$MAX_LONG_PARTIAL_PREFILLS"
  --reasoning-parser "$REASONING_PARSER"
  --tool-call-parser "$TOOL_CALL_PARSER"
  --limit-mm-per-prompt "$LIMIT_MM_PER_PROMPT"
  --compilation-config "$COMPILATION_CONFIG"
)

[[ "$TRUST_REMOTE_CODE" == "1" ]] && vllm_args+=(--trust-remote-code)
[[ "$LANGUAGE_MODEL_ONLY" == "1" ]] && vllm_args+=(--language-model-only)
[[ "$ENABLE_CHUNKED_PREFILL" == "1" ]] && vllm_args+=(--enable-chunked-prefill)
[[ "$ENABLE_PREFIX_CACHING" == "1" ]] && vllm_args+=(--enable-prefix-caching)
[[ "$ENABLE_AUTO_TOOL_CHOICE" == "1" ]] && vllm_args+=(--enable-auto-tool-choice)
[[ "$CUDAGRAPH_METRICS" == "1" ]] && vllm_args+=(--cudagraph-metrics)
[[ "$DISABLE_UVICORN_ACCESS_LOG" == "1" ]] && vllm_args+=(--disable-uvicorn-access-log)
if [[ "$PASSTHROUGH_COUNT" -gt 0 ]]; then
  vllm_args+=("${PASSTHROUGH_ARGS[@]}")
fi

print_dry_run() {
  printf 'container=%s\n' "$CONTAINER"
  printf 'image=%s\n' "$IMAGE"
  printf 'model=%s\n' "$MODEL"
  printf 'served_model_name=%s\n' "$SERVED_MODEL_NAME"
  printf 'host=%s\n' "$HOST"
  printf 'port=%s\n' "$PORT"
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
  "$ROOT/vllm-cache" \
  "$ROOT/triton-cache" \
  "$ROOT/torch-compile-cache" \
  "$ROOT/flashinfer-cache" \
  "$ROOT/logs" \
  "$ROOT/runs" \
  "$ROOT/tmp" \
  "$HF_CACHE_ROOT"

echo "INFO Blackbird Qwen3.6 27B FP8 foreground launcher"
echo "INFO container=$CONTAINER image=$IMAGE"
echo "INFO model=$MODEL served_model_name=$SERVED_MODEL_NAME host=$HOST port=$PORT"

for container in \
  "$CONTAINER" \
  qwen36-27b-fp8-kvbf16-rp6000-server \
  qwen36-27b-fp8-kvfp8-rp6000-server \
  qwen36-27b-fp8-rp6000-server \
  qwen3-coder-next-nvfp4-server \
  qwen3-coder-next-fp8-server \
  qwen36-27b-bf16-rp6000-server \
  qwen36-dual-27b-fp8-vlm \
  qwen36-dual-35b-fp8-vlm
do
  stop_if_exists "$container"
done

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
  -e HF_HOME=/root/.cache/huggingface \
  -e HF_HUB_CACHE=/root/.cache/huggingface/hub \
  -e VLLM_CACHE_ROOT=/root/.cache/vllm \
  -e TRITON_CACHE_DIR=/root/.cache/triton \
  -e TORCHINDUCTOR_CACHE_DIR=/root/.cache/torch \
  -e FLASHINFER_CUDA_ARCH_LIST=12.0f \
  -e FLASHINFER_LOGLEVEL=0 \
  -e FLASHINFER_JIT_VERBOSE=0 \
  -e PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
  -e SAFETENSORS_FAST_GPU=1 \
  -v "$HF_CACHE_ROOT:/root/.cache/huggingface" \
  -v "$ROOT/vllm-cache:/root/.cache/vllm" \
  -v "$ROOT/triton-cache:/root/.cache/triton" \
  -v "$ROOT/torch-compile-cache:/root/.cache/torch" \
  -v "$ROOT/flashinfer-cache:/root/.cache/flashinfer" \
  -v "$ROOT/tmp:/tmp/qwen36-27b-fp8-rp6000" \
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
