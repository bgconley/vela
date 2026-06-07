# Docker Runtime

Vela supports Docker as a first-class runtime for single-container vLLM
deployments. A Docker config uses native `command.runtime: docker` and a
`command.docker` block; it does not need a shell wrapper for normal launch,
logging, stop, or smoke flows.

The target agent owns Docker. The controller only sends config names, run ids,
job ids, and event subscriptions. It never runs `docker`, stores container
handles, dereferences target-local paths, or signals host processes.

## What Vela Owns

For a Docker deployment, the target agent:

- builds the masked `docker run` preview from the same command builder used by
  process configs;
- strips the leading `serve` token because the `vllm/vllm-openai` image
  entrypoint already runs `vllm serve`;
- launches with `docker run -d --name ...`;
- tails `docker logs -f` through the scrubbed log sink;
- waits on `docker wait`;
- writes a Docker sidecar with container name, container id, and image digest;
- verifies container identity before `docker stop` or `docker kill`;
- treats a Docker sidecar as live only when the verified container is still
  running.

The vLLM image is the build artifact for Docker runtime configs. Managed venv
builds still apply to `command.runtime: process`.

Known Blackbird entries are local Blackwell recipe ports, not generic Hugging
Face derivations. The image digest, `sm_120` arch setting, CUTLASS-sensitive
backend choices, FlashInfer cache layout, and FP8/BF16 memory shape are copied
from local deployment scripts and configs that were validated on the target
hardware. Hugging Face metadata is advisory for model identity and broad model
defaults only; it must not replace the local Blackwell recipe when selecting
the vLLM build/container shape.

The current Qwen3.6 Blackbird recipes use the pinned
`vllm/vllm-openai@sha256:b13d6e5fda0785f3d41752df8513ff832f67cb231a216c76b6b4f2a515bf0046`
image, proven in local run records with vLLM
`0.20.2rc1.dev9+g01d4d1ad3`, Transformers `5.7.0`, Torch
`2.11.0+cu130`, and CUDA `13.0`. Saved configs record `version_profile:
current` because the profile is a Vela flag-compatibility hint, not a claim
that the Docker image contains an older package release.

## Config Shape

```yaml
name: qwen36-27b-fp8-kvfp8-rp6000-blackbird
target: blackbird
model: Qwen/Qwen3.6-27B-FP8
served_model_name: qwen36-27b-fp8-kvfp8-rp6000
command:
  entrypoint: serve
  runtime: docker
  docker:
    image: vllm/vllm-openai@sha256:b13d6e5fda0785f3d41752df8513ff832f67cb231a216c76b6b4f2a515bf0046
    container_name: qwen36-27b-fp8-kvfp8-rp6000-vela
    gpus: all
    network: host
    ipc_host: true
    shm_size: 32g
    restart: "no"
    stop_grace_seconds: 90
    pull: never
    hf_cache: /home/bgconley/models/qwen36-dual-fp8-vlm/hf-cache
    volumes:
      - /home/bgconley/models/qwen36-27b-fp8-rp6000/vllm-cache:/root/.cache/vllm
      - /home/bgconley/models/qwen36-27b-fp8-rp6000/triton-cache:/root/.cache/triton
      - /home/bgconley/models/qwen36-27b-fp8-rp6000/torch-compile-cache:/root/.cache/torch
      - /home/bgconley/models/qwen36-27b-fp8-rp6000/flashinfer-cache:/root/.cache/flashinfer
    env:
      FLASHINFER_CUDA_ARCH_LIST: 12.0f
      PYTORCH_CUDA_ALLOC_CONF: expandable_segments:True
    extra_run_args: [--ulimit, memlock=-1, --ulimit, stack=67108864]
vllm:
  version_profile: current
  version: 0.20.2rc1.dev9+g01d4d1ad3
  transformers_version: 5.7.0
  torch_version: 2.11.0+cu130
  cuda_version: "13.0"
```

FP8 and BF16 Blackbird configs intentionally differ:

- FP8 keeps `--kv-cache-memory-bytes 64424509440`, FlashInfer attention, and
  `FLASHINFER_CUDA_ARCH_LIST=12.0f`.
- BF16 omits the FP8 KV-byte cap and FlashInfer arch pin, using
  `gpu_memory_utilization` to size KV cache.

The Blackbird recipes intentionally emit both `--ipc=host` and `--shm-size 32g`
because the proven local wrappers do the same. Generic Docker guidance treats
them as alternatives: Vela omits a computed default `--shm-size` when host IPC
is enabled, but still emits an explicit `command.docker.shm_size` such as the
Blackbird recipes' `32g`. This preserves the validated lab launch shape for
these configs without adding noisy defaults elsewhere.

## Preview, Smoke, And Export

Render the exact masked Docker command:

```bash
vela preview qwen36-27b-fp8-kvfp8-rp6000-blackbird --target blackbird
```

Run the same TUI load/READY/stop flow headlessly:

```bash
vela smoke-tui qwen36-27b-fp8-kvfp8-rp6000-blackbird --target blackbird
```

Export a standalone script when a deployment needs to run without Vela:

```bash
vela deploy export qwen36-27b-fp8-kvfp8-rp6000-blackbird \
  --target blackbird \
  --output /tmp/qwen36-fp8-docker.sh
```

Secrets are redacted in the exported script. Required secret environment
variables are emitted as runtime requirements rather than literal values.

Legacy wrapper configs can be migrated through the target-local agent:

```bash
vela deploy from-wrapper legacy-qwen-fp8 legacy-qwen-fp8-docker \
  --target blackbird \
  --dry-run
```

The migration helper recognizes only the known Blackbird wrapper scripts and
emits the native recipe copied from those wrappers. It is review-required and
does not infer vLLM image, CUTLASS, FlashInfer, FlashAttention, or memory shape
from Hugging Face metadata.

## Composer

The TUI **New Deployment** wizard and the headless `vela deploy create` command
use the same agent-side composer. For known lab recipes such as Blackbird
Qwen3.6 FP8/BF16, the composer fills the pinned image, cache mounts, container
name, port, exposure, run directory, and FP8-vs-BF16 flags before review.

For Blackbird/P620 FP8 Docker deployments with no matched lab recipe, the
default-suggestion surface may warn, but final composition is refused with
`blackwell-fp8-runtime-recipe-required`. The composer must not invent the vLLM
image, CUTLASS/FlashInfer/FlashAttention shape, `sm_120` arch settings, or KV
memory layout from Hugging Face metadata alone.

```bash
vela deploy create qwen36-fp8 \
  --target blackbird \
  --model Qwen/Qwen3.6-27B-FP8 \
  --runtime docker \
  --port auto \
  --dry-run
```

## Real-Hardware Proof

Current P620-to-Blackbird native-Docker validations:

- `artifacts/remote-validation/2026-06-06-p620-blackbird-native-docker-fp8-d67b3a6.md`
- `artifacts/remote-validation/2026-06-06-p620-blackbird-native-docker-bf16-9b107b4.md`

Both reached READY through `vela smoke-tui`, stopped cleanly through Docker,
left the port free, and returned the Blackbird GPU to idle.
