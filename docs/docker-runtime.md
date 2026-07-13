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
  running;
- selects the vLLM flag-compatibility profile from the bundled profile map as-is
  instead of probing a host `vllm --help` — the container's vLLM, not whatever
  vLLM happens to be on the target host, decides which flags are valid.

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
    auto_remove: true
    stop_grace_seconds: 90
    pull: never
    hf_cache: /path/to/models/qwen36-dual-fp8-vlm/hf-cache
    volumes:
      - /path/to/models/qwen36-27b-fp8-rp6000/vllm-cache:/root/.cache/vllm
      - /path/to/models/qwen36-27b-fp8-rp6000/triton-cache:/root/.cache/triton
      - /path/to/models/qwen36-27b-fp8-rp6000/torch-compile-cache:/root/.cache/torch
      - /path/to/models/qwen36-27b-fp8-rp6000/flashinfer-cache:/root/.cache/flashinfer
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

## Image Pull

`command.docker.pull` selects the pull policy the target agent applies before it
starts the container:

- `never` (what every shipped Blackbird recipe uses): the image must already be
  present on the target; the agent only runs `docker image inspect`.
- `missing`: inspect first, and pull only when the image is absent.
- `always`: pull on every launch.

A real vLLM image is on the order of 10 GB, so `missing`/`always` pulls are
bounded by `VELA_DOCKER_PULL_TIMEOUT_SECONDS` (target-agent environment,
default `1800`, i.e. 30 minutes; set it to `0` or a negative value to disable
the limit and let a long pull run unbounded). Quick commands such as
`docker image inspect` keep their short 10-second timeout.

`docker pull` progress (the `Pulling from …` / `Downloading …` phase lines) is
streamed through the same scrubbed log sink as container logs, so the TUI shows
the download instead of a silent hang. A pull that exceeds the timeout is
recorded as a classified `image-pull-timeout` failure in the run log and
exit-status file — it never crashes the supervisor, and because no container
has been created yet there is nothing to orphan. For very large images, prefer
pre-pulling on the target (or keep `pull: never` with a pinned digest) rather
than raising the timeout.

## Preview, Smoke, And Export

Render the exact masked Docker command:

```bash
vela preview qwen36-27b-fp8-kvfp8-rp6000-blackbird --target blackbird
```

Run the same TUI launch/READY/stop flow headlessly:

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

### HF cache mount

For a generic (non-recipe) Docker deployment of a Hugging Face repo model —
whether pinned through `model_ref` or given as a bare `model:` repo id — the
composer **mounts the agent HF cache by default**. It sets
`command.docker.hf_cache` to the target's resolved Hugging Face cache directory
(`HF_HOME`, the same tree `vela model download` and the registry scan use) so it
is bind-mounted at `hf_cache_target` (`/root/.cache/huggingface`). Without the
mount every fresh container re-downloads the model into its own filesystem and
any pre-download is wasted disk. Local-path and URL models get no auto-mount
(a local path has its own volume handling; a URL needs nothing), and an explicit
`command.docker.hf_cache` — or a matched lab recipe's own cache mounts — always
wins.

Hand-written Docker YAML that omits the mount is not blocked. Launching a
`runtime: docker` config whose model is a Hugging Face repo with no
`command.docker.hf_cache` (and no volume covering that cache) surfaces a
`docker-no-hf-cache-mount` launch warning explaining that the container will
re-download the weights on every fresh start.

Before a `runtime: docker` launch downloads an uncached Hugging Face model into
the mounted cache, the agent applies the same disk-headroom precheck as a
process launch: it requires free space on the resolved cache directory greater
than the model's known size plus a 10% staging margin, failing with a
size/percentage-only `DISK_FULL` detail (never a host path) rather than filling
the disk mid-pull.

One limitation: the default mount is `HF_HOME`, but registry downloads land in
`HF_HUB_CACHE`. If the agent host relocates `HF_HUB_CACHE` **outside** `HF_HOME`
(so the hub cache is not `HF_HOME/hub`), the default HF_HOME mount will not
contain the agent's downloads. In that case a docker + Hugging Face launch that
relies on the default mount surfaces a `docker-hf-cache-env-mismatch` launch
warning asking you to set `command.docker.hf_cache` explicitly to the target's
actual hub cache directory.

## Real-Hardware Proof

Current maintainer-lab P620-to-Blackbird native-Docker validations:

- `artifacts/remote-validation/2026-06-06-p620-blackbird-native-docker-fp8-d67b3a6.md`
- `artifacts/remote-validation/2026-06-06-p620-blackbird-native-docker-bf16-9b107b4.md`

Both reached READY through `vela smoke-tui`, stopped cleanly through Docker,
left the port free, and returned the Blackbird GPU to idle.
