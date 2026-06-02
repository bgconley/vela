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

On `620-01` (`10.25.0.50`), the checked-in real smoke config is:

```bash
scripts/run_remote_tests.sh bgconley@10.25.0.50 /tank/repos/lab-tui qwen3-32b-fp8-62001
```

That config uses `/tank/triton/venv-vllm/bin/vllm` directly and serves
`/tank/trt/models/Qwen3-32B-FP8` on `127.0.0.1:8017` with two visible GPUs.
The remote validation venv does not install vLLM, so the diagnostic line may
say `vllm not found on PATH`; the real config preview/smoke still validates the
absolute lab vLLM executable path.

The real run uses `vllm-loader smoke`: it launches the config, waits for READY
via `/health` and `/v1/models`, prints the READY URL/model names, then stops the
server. It is still wrapped in `timeout` as a hard guard. Override the limit with:

```bash
VLLM_LOADER_REMOTE_TIMEOUT=2400 scripts/run_remote_tests.sh USER@GPU_HOST /tank/repos/lab-tui my-real-config
```

## 3. Where results land

By default, `vllm-loader run` writes scrubbed run artifacts on the GPU host:

```text
~/.local/state/vllm-loader/runs/
```

The durable log contains scrubbed committed lines only. Transient carriage-return
progress frames are shown in the UI/process stream but are not persisted.
