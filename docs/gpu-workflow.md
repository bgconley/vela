# Mac to GPU Workflow

This project is expected to be authored on a Mac and exercised for real vLLM
runtime behavior on GPU boxes. Local Mac validation should stay no-GPU and
no-vLLM by default.

## 1. Sync the tree

```bash
scripts/rsync_to_gpu.sh USER@GPU_HOST:/tank/repos/lab-tui
```

The sync excludes virtualenvs, caches, build products, run logs, and `.git`.
Machine-specific secrets should stay on the GPU host. Do not put `HF_TOKEN` or
API keys in example configs.

If the GPU host needs a specific SSH key or options, use the same option string
for sync and validation:

```bash
export VLLM_LOADER_SSH_OPTS="-i /path/to/gpu_key -o BatchMode=yes"
scripts/rsync_to_gpu.sh USER@GPU_HOST:/tank/repos/lab-tui
```

## 2. Run remote validation

No-GPU-safe validation on the GPU host:

```bash
scripts/run_remote_tests.sh USER@GPU_HOST /tank/repos/lab-tui
```

This installs the editable package, prints host/GPU/vLLM diagnostics, runs
Ruff/pytest, and checks the fake config preview path.

The remote script creates or reuses a persistent ZFS-backed validation
environment at `/tank/venvs/lab-tui` by default, then installs this package into
that venv. Override the venv path only when the host has a different ZFS layout:

```bash
VLLM_LOADER_REMOTE_VENV=/tank/venvs/custom-lab-tui \
  scripts/run_remote_tests.sh USER@GPU_HOST /tank/repos/lab-tui
```

The venv is created with `/tank/preproc/venv/bin/python` when that seed
interpreter exists, otherwise `python3`, then `python`. Override the seed
interpreter when the GPU box needs a specific venv-capable Python:

```bash
VLLM_LOADER_REMOTE_PYTHON=/path/to/venv/bin/python \
  scripts/run_remote_tests.sh USER@GPU_HOST /tank/repos/lab-tui
```

That interpreter must be able to create a pip-enabled venv; otherwise install
`python3-venv`/`ensurepip` support or point `VLLM_LOADER_REMOTE_PYTHON` at a
prepared environment.

Real vLLM validation with a named config already present in the synced
`configs/` directory or the host's configured config directory:

```bash
scripts/run_remote_tests.sh USER@GPU_HOST /tank/repos/lab-tui my-real-config
```

Preferred future real smoke target: `blackbird` (`10.25.0.51`) with the
`RTX PRO 6000 Blackwell Max-Q` GPU and Qwen3.6 27B FP8:

```bash
scripts/rsync_to_gpu.sh bgconley@10.25.0.51:/home/bgconley/repos/lab-tui
VLLM_LOADER_REMOTE_VENV=/home/bgconley/venvs/lab-tui \
  scripts/run_remote_tests.sh bgconley@10.25.0.51 /home/bgconley/repos/lab-tui \
  qwen36-27b-fp8-kvfp8-rp6000-blackbird
```

That config uses a repo-local foreground Docker wrapper,
`./scripts/blackbird_qwen36_vllm_foreground.sh`, to launch the pinned
`vllm/vllm-openai` image for `Qwen/Qwen3.6-27B-FP8`, stream container logs into
the TUI, and stop the container when the TUI Stop flow runs. The config serves
`qwen36-27b-fp8-kvfp8-rp6000` on host port `18003` and probes localhost. It may
stop conflicting Blackbird Qwen containers while active, because the RP6000 GPU
cannot host the full test lane alongside another large model.

Historical/fallback real smoke target: `620-01` (`10.25.0.50`) with Qwen3-32B
FP8:

```bash
scripts/run_remote_tests.sh bgconley@10.25.0.50 /tank/repos/lab-tui qwen3-32b-fp8-62001
```

That config uses `/tank/triton/venv-vllm/bin/vllm` directly and serves
`/tank/trt/models/Qwen3-32B-FP8` on `127.0.0.1:8017` with two visible GPUs.
The remote validation venv does not install vLLM, so the diagnostic line may
say `vllm not found on PATH`; the real config preview/smoke still validates the
absolute lab vLLM executable path.

The real run uses `vllm-loader smoke-tui`: it mounts the Textual app headlessly,
selects the config, follows the normal Load workflow, waits for READY via the
app's health/model state, prints the READY URL/model names, then follows the
normal Stop workflow. It is still wrapped in `timeout` as a hard guard. Override
the limit with:

```bash
VLLM_LOADER_REMOTE_TIMEOUT=2400 scripts/run_remote_tests.sh USER@GPU_HOST /tank/repos/lab-tui my-real-config
```

## 3. Tested vLLM surface

The preferred Blackbird lane is based on the validated Qwen3.6 27B FP8 Docker
stack from `10.25.0.51`: vLLM `0.20.2rc1.dev9+g01d4d1ad3`, Transformers `5.7.0`,
Torch `2.11.0+cu130`, FP8 KV, FlashInfer attention, and Cutlass FP8 GEMM.

The fallback 620-01 lane covers the tested vLLM 0.19 lab surface and was
verified with vLLM
`v0.19.1rc1.dev119+gba4a78eb5` from `/tank/triton/venv-vllm/bin/vllm`. Treat
these as the tested lab surfaces, not a promise that older or newer vLLM builds
emit the same flags and log strings. When bumping a lab vLLM build, rerun the
real smoke and add or adjust recorded log fixtures and `VllmProfile` rules for
any changed startup, download, readiness, or error text.

## 4. Browser access through Textual

For browser access on a GPU host, use Textual's own `textual serve` entrypoint
around `vllm-loader` only on a trusted network/auth boundary. The served TUI
controls model launches, stops, kills, and log access; do not expose it as an
unauthenticated public service.

## 5. Where results land

By default, `vllm-loader run` writes scrubbed run artifacts on the GPU host:

```text
~/.local/state/vllm-loader/runs/
```

The durable log contains scrubbed committed lines only. Transient carriage-return
progress frames are shown in the UI/process stream but are not persisted.
