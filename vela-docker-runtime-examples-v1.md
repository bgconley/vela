# Vela — Docker Runtime — Worked Examples (DK4 anchor): Blackbird Qwen3.6 27B FP8 & BF16

**Purpose:** convert the two **proven** Blackbird wrapper deployments into native `runtime: docker` configs, as the concrete acceptance target for the Docker runtime feature (`vela-docker-runtime-spec-v1.md`). These anchor:
- **DK0** — `build_docker_run(cfg)` must generate the exact `docker run` shown below from each config.
- **DK4** — launching these on Blackbird via the native runtime must reach READY (FP8 `:18003`, BF16 `:18002`) and Stop via `docker stop`, retiring the hand-written wrappers.

> **Do not drop these into `configs/` yet.** `runtime: docker` / `command.docker` don't exist in `config/schema.py` until DK0/S1 land; with `extra="forbid"` they'd surface as *invalid* configs in `vela list`. Keep them here until the runtime ships, then move them into `configs/` (and retire `scripts/blackbird_qwen36_*_vllm_foreground.sh`).

> **Source of truth:** these reproduce `configs/qwen36-27b-fp8-kvfp8-rp6000-blackbird.yaml` + `scripts/blackbird_qwen36_vllm_foreground.sh` and `configs/qwen36-27b-bf16-rp6000-blackbird.yaml` + `scripts/blackbird_qwen36_bf16_vllm_foreground.sh` — i.e. the wrappers' Docker shell moves into `command.docker`, while the vLLM serve flags stay as the existing top-level `model`/`engine`/`server`/`extra_args` fields (the command builder still emits them).

---

## 0. The one generation rule the examples pin down (DK0)

The `vllm/vllm-openai` image's entrypoint already runs `vllm serve`. Vela's command builder emits `serve <model> <flags>`. So `build_docker_run` must **strip the leading `serve`** and pass `<model-positional> <flags>` to the container (the proven wrappers do exactly this). Net container argv: `<model> <flags…>` — never `serve <model>` (that double-serves).

Also pinned: defaults injected only when unset (`--gpus`, `--ipc=host`/`--shm-size`, `--name`, `--restart no`, network/port, HF-cache mount); secrets (`VLLM_API_KEY`/`HF_TOKEN`) passed as `-e` and masked in preview; **BF16 must not inherit the FP8 `--kv-cache-memory-bytes` pin** (the footgun — FP8 pins 60 GB; BF16 auto-sizes from `--gpu-memory-utilization`).

---

## 1. Field mapping (wrapper → `command.docker`)

| Wrapper concept | Native `command.docker` field |
|---|---|
| `IMAGE` (digest-pinned) | `image` |
| `CONTAINER` | `container_name` |
| `--gpus all` | `gpus: all` |
| `--ipc=host` | `ipc_host: true` |
| `--shm-size=32g` | `shm_size: 32g` |
| `--network host` | `network: host` |
| `--restart no` | `restart: "no"` (default) |
| `docker stop -t 90` (cleanup) | `stop_grace_seconds: 90` |
| `PULL_IMAGE=0` | `pull: never` |
| `-v HF_CACHE_ROOT:/root/.cache/huggingface` | `hf_cache:` (mounts to the in-image HF path) |
| other `-v host:container` | `volumes: [...]` |
| `-e KEY=VAL` | `env: {...}` |
| `--ulimit …` | `extra_run_args: [...]` (escape hatch) |
| the `for container in … stop_if_exists` list | `evict: [...]` |
| `MODEL`, `--dtype`, `--port`, … | stay as top-level `model`/`engine`/`server`/`extra_args` (command builder) |
| foreground `docker logs -f` + `docker wait` + signal-stop | **owned by the DockerRuntime backend** (no longer in a script) |

---

## 2. FP8 — `qwen36-27b-fp8-rp6000-blackbird` (native docker)

```yaml
name: qwen36-27b-fp8-rp6000-blackbird
description: Blackbird RTX PRO 6000 Qwen3.6-27B-FP8 via native Vela docker runtime.
model: Qwen/Qwen3.6-27B-FP8
served_model_name: qwen36-27b-fp8-kvfp8-rp6000
command:
  entrypoint: serve
  runtime: docker
  docker:
    image: vllm/vllm-openai@sha256:b13d6e5fda0785f3d41752df8513ff832f67cb231a216c76b6b4f2a515bf0046
    container_name: qwen36-27b-fp8-kvfp8-rp6000-vela
    gpus: all
    ipc_host: true
    shm_size: 32g
    network: host
    restart: "no"
    stop_grace_seconds: 90
    pull: never
    hf_cache: /home/bgconley/models/qwen36-dual-fp8-vlm/hf-cache   # -> -v ...:/root/.cache/huggingface
    volumes:
      - /home/bgconley/models/qwen36-27b-fp8-rp6000/vllm-cache:/root/.cache/vllm
      - /home/bgconley/models/qwen36-27b-fp8-rp6000/triton-cache:/root/.cache/triton
      - /home/bgconley/models/qwen36-27b-fp8-rp6000/torch-compile-cache:/root/.cache/torch
      - /home/bgconley/models/qwen36-27b-fp8-rp6000/flashinfer-cache:/root/.cache/flashinfer
      - /home/bgconley/models/qwen36-27b-fp8-rp6000/tmp:/tmp/qwen36-27b-fp8-rp6000
    env:
      HF_HOME: /root/.cache/huggingface
      HF_HUB_CACHE: /root/.cache/huggingface/hub
      VLLM_CACHE_ROOT: /root/.cache/vllm
      TRITON_CACHE_DIR: /root/.cache/triton
      TORCHINDUCTOR_CACHE_DIR: /root/.cache/torch
      FLASHINFER_CUDA_ARCH_LIST: 12.0f
      FLASHINFER_LOGLEVEL: "0"
      FLASHINFER_JIT_VERBOSE: "0"
      PYTORCH_CUDA_ALLOC_CONF: expandable_segments:True
      SAFETENSORS_FAST_GPU: "1"
    extra_run_args: ["--ulimit", "memlock=-1", "--ulimit", "stack=67108864"]
    evict:
      - qwen36-27b-fp8-kvbf16-rp6000-server
      - qwen36-27b-fp8-kvfp8-rp6000-server
      - qwen36-27b-fp8-rp6000-server
      - qwen3-coder-next-nvfp4-server
      - qwen3-coder-next-fp8-server
      - qwen36-27b-bf16-rp6000-server
      - qwen36-27b-bf16-rp6000-vela
      - qwen36-dual-27b-fp8-vlm
      - qwen36-dual-35b-fp8-vlm
engine:
  gpu_memory_utilization: 0.97
  max_model_len: 262144
  dtype: auto
  kv_cache_dtype: fp8
  max_num_seqs: 16
server:
  host: 0.0.0.0
  port: 18003
  exposure: lan
  api_key: EMPTY
logging:
  request_logging: false
  suppress_access_log_for: [/health]
extra_args:
  - --kv-cache-memory-bytes
  - "64424509440"            # FP8 pins ~60 GB KV (correct for FP8 weights)
  - --max-num-batched-tokens
  - "8192"
  - --max-num-partial-prefills
  - "1"
  - --max-long-partial-prefills
  - "1"
  - --attention-backend
  - FLASHINFER
  - --trust-remote-code
  - --language-model-only
  - --enable-chunked-prefill
  - --enable-prefix-caching
  - --enable-auto-tool-choice
  - --reasoning-parser
  - qwen3
  - --tool-call-parser
  - qwen3_coder
  - --limit-mm-per-prompt
  - '{"image":0,"video":0}'
  - --compilation-config
  - '{"cudagraph_capture_sizes":[1,2,4,8,16],"cudagraph_num_of_warmups":1}'
  - --cudagraph-metrics
  - --disable-uvicorn-access-log
launch:
  mode: attached
  ready_timeout_seconds: 1800
  health: { interval_seconds: 2 }
  runs_dir: /home/bgconley/models/qwen36-27b-fp8-rp6000/vela-runs
vllm:
  version_profile: "0.11"
```

**Expected `docker run` (DK0 generation target; flag order is builder-determined — assert presence, not order):**
```
docker run -d --name qwen36-27b-fp8-kvfp8-rp6000-vela --restart no \
  --gpus all --ipc=host --shm-size=32g --network host \
  --ulimit memlock=-1 --ulimit stack=67108864 \
  -e HF_HOME=/root/.cache/huggingface -e HF_HUB_CACHE=/root/.cache/huggingface/hub \
  -e VLLM_CACHE_ROOT=/root/.cache/vllm -e TRITON_CACHE_DIR=/root/.cache/triton \
  -e TORCHINDUCTOR_CACHE_DIR=/root/.cache/torch -e FLASHINFER_CUDA_ARCH_LIST=12.0f \
  -e FLASHINFER_LOGLEVEL=0 -e FLASHINFER_JIT_VERBOSE=0 \
  -e PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True -e SAFETENSORS_FAST_GPU=1 \
  -v /home/bgconley/models/qwen36-dual-fp8-vlm/hf-cache:/root/.cache/huggingface \
  -v /home/bgconley/models/qwen36-27b-fp8-rp6000/vllm-cache:/root/.cache/vllm \
  -v /home/bgconley/models/qwen36-27b-fp8-rp6000/triton-cache:/root/.cache/triton \
  -v /home/bgconley/models/qwen36-27b-fp8-rp6000/torch-compile-cache:/root/.cache/torch \
  -v /home/bgconley/models/qwen36-27b-fp8-rp6000/flashinfer-cache:/root/.cache/flashinfer \
  -v /home/bgconley/models/qwen36-27b-fp8-rp6000/tmp:/tmp/qwen36-27b-fp8-rp6000 \
  vllm/vllm-openai@sha256:b13d6e5… \
  Qwen/Qwen3.6-27B-FP8 \
    --served-model-name qwen36-27b-fp8-kvfp8-rp6000 --host 0.0.0.0 --port 18003 \
    --api-key '••••' --dtype auto --kv-cache-dtype fp8 --max-model-len 262144 \
    --gpu-memory-utilization 0.97 --max-num-seqs 16 \
    --disable-access-log-for-endpoints /health \
    --kv-cache-memory-bytes 64424509440 --max-num-batched-tokens 8192 \
    --max-num-partial-prefills 1 --max-long-partial-prefills 1 \
    --attention-backend FLASHINFER --trust-remote-code --language-model-only \
    --enable-chunked-prefill --enable-prefix-caching --enable-auto-tool-choice \
    --reasoning-parser qwen3 --tool-call-parser qwen3_coder \
    --limit-mm-per-prompt '{"image":0,"video":0}' \
    --compilation-config '{"cudagraph_capture_sizes":[1,2,4,8,16],"cudagraph_num_of_warmups":1}' \
    --cudagraph-metrics --disable-uvicorn-access-log
```
(`--api-key` value is `EMPTY`; shown masked here — it is masked in preview and scrubbed in logs.)

---

## 3. BF16 — `qwen36-27b-bf16-rp6000-blackbird` (native docker)

```yaml
name: qwen36-27b-bf16-rp6000-blackbird
description: Blackbird RTX PRO 6000 Qwen3.6-27B (BF16, kv bf16) via native Vela docker runtime.
model: Qwen/Qwen3.6-27B
served_model_name: qwen36-27b-bf16-rp6000
command:
  entrypoint: serve
  runtime: docker
  docker:
    image: vllm/vllm-openai@sha256:b13d6e5fda0785f3d41752df8513ff832f67cb231a216c76b6b4f2a515bf0046
    container_name: qwen36-27b-bf16-rp6000-vela
    gpus: all
    ipc_host: true
    shm_size: 32g
    network: host
    restart: "no"
    stop_grace_seconds: 90
    pull: never
    volumes:
      - /home/bgconley/models/qwen36-27b-bf16:/home/bgconley/models/qwen36-27b-bf16   # ROOT:ROOT
    env:
      HF_HOME: /home/bgconley/models/qwen36-27b-bf16/hf-cache
      HF_HUB_CACHE: /home/bgconley/models/qwen36-27b-bf16/hf-cache/hub
      VLLM_CACHE_ROOT: /home/bgconley/models/qwen36-27b-bf16/vllm-cache
      TRITON_CACHE_DIR: /home/bgconley/models/qwen36-27b-bf16/triton-cache
      TORCHINDUCTOR_CACHE_DIR: /home/bgconley/models/qwen36-27b-bf16/torch-compile-cache
      TMPDIR: /home/bgconley/models/qwen36-27b-bf16/tmp
      CUDA_VISIBLE_DEVICES: "0"
      PYTORCH_CUDA_ALLOC_CONF: expandable_segments:True
      SAFETENSORS_FAST_GPU: "1"
    extra_run_args: ["--ulimit", "memlock=-1", "--ulimit", "stack=67108864"]
    evict:
      - qwen36-27b-bf16-rp6000-server
      - qwen36-27b-bf16-rp6000-vela
      - qwen36-27b-fp8-kvbf16-rp6000-server
      - qwen36-27b-fp8-kvfp8-rp6000-server
      - qwen36-27b-fp8-kvfp8-rp6000-vela
      - qwen36-27b-fp8-rp6000-server
      - qwen3-coder-next-nvfp4-server
      - qwen3-coder-next-fp8-server
      - qwen36-dual-27b-fp8-vlm
      - qwen36-dual-35b-fp8-vlm
engine:
  gpu_memory_utilization: 0.95
  max_model_len: 262144
  dtype: bfloat16
  kv_cache_dtype: bfloat16
  max_num_seqs: 4
server:
  host: 0.0.0.0
  port: 18002
  exposure: lan
  api_key: EMPTY
logging:
  request_logging: false
  suppress_access_log_for: [/health]
extra_args:
  - --max-num-batched-tokens
  - "8192"
  - --trust-remote-code
  - --language-model-only
  - --enable-prefix-caching
  - --enable-auto-tool-choice
  - --reasoning-parser
  - qwen3
  - --tool-call-parser
  - qwen3_coder
  # NOTE: no --kv-cache-memory-bytes — BF16 weights are ~2x FP8, so the KV cache is
  # sized from --gpu-memory-utilization (0.95). Pinning 60 GB here would OOM the RP6000.
launch:
  mode: attached
  ready_timeout_seconds: 1800
  health: { interval_seconds: 2 }
  runs_dir: /home/bgconley/models/qwen36-27b-bf16/vela-runs
vllm:
  version_profile: "0.11"
```

**Expected `docker run` (DK0 generation target):**
```
docker run -d --name qwen36-27b-bf16-rp6000-vela --restart no \
  --gpus all --ipc=host --shm-size=32g --network host \
  --ulimit memlock=-1 --ulimit stack=67108864 \
  -e HF_HOME=/home/bgconley/models/qwen36-27b-bf16/hf-cache \
  -e HF_HUB_CACHE=/home/bgconley/models/qwen36-27b-bf16/hf-cache/hub \
  -e VLLM_CACHE_ROOT=/home/bgconley/models/qwen36-27b-bf16/vllm-cache \
  -e TRITON_CACHE_DIR=/home/bgconley/models/qwen36-27b-bf16/triton-cache \
  -e TORCHINDUCTOR_CACHE_DIR=/home/bgconley/models/qwen36-27b-bf16/torch-compile-cache \
  -e TMPDIR=/home/bgconley/models/qwen36-27b-bf16/tmp -e CUDA_VISIBLE_DEVICES=0 \
  -e PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True -e SAFETENSORS_FAST_GPU=1 \
  -v /home/bgconley/models/qwen36-27b-bf16:/home/bgconley/models/qwen36-27b-bf16 \
  vllm/vllm-openai@sha256:b13d6e5… \
  Qwen/Qwen3.6-27B \
    --served-model-name qwen36-27b-bf16-rp6000 --host 0.0.0.0 --port 18002 \
    --api-key '••••' --dtype bfloat16 --kv-cache-dtype bfloat16 --max-model-len 262144 \
    --gpu-memory-utilization 0.95 --max-num-seqs 4 \
    --disable-access-log-for-endpoints /health \
    --max-num-batched-tokens 8192 --trust-remote-code --language-model-only \
    --enable-prefix-caching --enable-auto-tool-choice \
    --reasoning-parser qwen3 --tool-call-parser qwen3_coder
```

---

## 4. What these examples assert (acceptance hooks)

**DK0 (`test_docker_run_generation`)** — load each example config, run `build_docker_run`, assert:
- container argv begins with the **model positional** (no `serve`), image is the digest, `--name`/`--restart no`/`--gpus all`/`--ipc=host`/`--shm-size=32g`/`--network host` present.
- all `env`/`volumes`/`extra_run_args` present; `hf_cache` (FP8) expands to `-v …:/root/.cache/huggingface`.
- **FP8 has `--kv-cache-memory-bytes 64424509440`; BF16 does NOT** (footgun guard).
- `--api-key` masked in the preview string.

**DK1 (`test_docker_lifecycle`)** — sidecar records `container_name`/`container_id`/`image_digest`; `stop` → `docker stop -t 90 <id>`; identity re-verify before stop; recycled name → `-32002`.

**DK3 (`test_docker_eviction_portguard`)** — the `evict` list is `docker stop`-ed before run; `:18003`/`:18002` already-bound → `PORT_IN_USE` pre-run.

**DK4 (manual, real Blackbird)** — `vela run qwen36-27b-fp8-rp6000-blackbird` / `…bf16…` reach READY (`http://127.0.0.1:18003` / `:18002`), `vela` Stop → `docker stop`, run survives agent restart (re-discovered by container name+id). Commit a dated artifact; then delete `scripts/blackbird_qwen36_*_vllm_foreground.sh`.

---

## 5. Notes for the implementer
- **`version_profile: "0.11"`** is carried from the existing configs for flag spellings (stable across the lab's vLLM). A future refinement: detect the image's vLLM version (`docker run --rm <image> vllm --version`) and pick the profile automatically; not required for DK4.
- **Two mount styles on purpose:** FP8 uses the `hf_cache` convenience + per-cache mounts under `/root/.cache/*`; BF16 mounts `ROOT:ROOT` with `HF_HOME` pointed inside it. Both must work — they exercise the schema's flexibility and mirror the two real wrappers exactly.
- **`evict` is shared** across both (the RP6000 fits one big model); launching FP8 stops the BF16 container and vice-versa. Keep both lists supersetted so either lane cleanly preempts the other.
- **`--network host`** means the container binds the host's `0.0.0.0:<port>` → non-loopback → `exposure: lan` + the canonical bind warning (already set).
- When DK0/DK1 land, move these two YAMLs into `configs/` (replacing the wrapper-based ones) and delete the wrapper scripts — that retirement is the DK4 "done" signal.

> No application code written in this document — it is the worked-example anchor for the Docker runtime feature.
