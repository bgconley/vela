# Mac to GPU Workflow

This project is expected to be authored on a Mac and exercised for real vLLM
runtime behavior on GPU boxes. Local Mac validation should stay no-GPU and
no-vLLM by default.

## 1. Sync the tree

```bash
scripts/rsync_to_gpu.sh USER@GPU_HOST:/absolute/remote/path
```

The sync excludes virtualenvs, caches, build products, run logs, and `.git`.
Machine-specific secrets should stay on the GPU host. Do not put `HF_TOKEN` or
API keys in example configs.

## 2. Run remote validation

No-GPU-safe validation on the GPU host:

```bash
scripts/run_remote_tests.sh USER@GPU_HOST /absolute/remote/path
```

This installs the editable package, prints host/GPU/vLLM diagnostics, runs
Ruff/pytest, and checks the fake config preview path.

Real vLLM validation with a named config already present in the synced
`configs/` directory or the host's configured config directory:

```bash
scripts/run_remote_tests.sh USER@GPU_HOST /absolute/remote/path my-real-config
```

The real run uses `vllm-loader smoke`: it launches the config, waits for READY
via `/health` and `/v1/models`, prints the READY URL/model names, then stops the
server. It is still wrapped in `timeout` as a hard guard. Override the limit with:

```bash
VLLM_LOADER_REMOTE_TIMEOUT=2400 scripts/run_remote_tests.sh USER@GPU_HOST /absolute/remote/path my-real-config
```

## 3. Where results land

By default, `vllm-loader run` writes scrubbed run artifacts on the GPU host:

```text
~/.local/state/vllm-loader/runs/
```

The durable log contains scrubbed committed lines only. Transient carriage-return
progress frames are shown in the UI/process stream but are not persisted.
