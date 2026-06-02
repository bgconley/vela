# vLLM Loader

`vllm-loader` is a Python package and terminal application for launching vLLM
servers from named YAML configs, watching scrubbed logs, tracking load phases,
probing readiness, and keeping GPU/status context visible.

Install locally:

```bash
pip install -e ".[dev]"
vllm-loader list
vllm-loader preview fake-child
vllm-loader run fake-child --preview
vllm-loader smoke fake-child
```

Mac to GPU workflow:

```bash
scripts/rsync_to_gpu.sh USER@GPU_HOST:/absolute/remote/path
scripts/run_remote_tests.sh USER@GPU_HOST /absolute/remote/path
scripts/run_remote_tests.sh USER@GPU_HOST /absolute/remote/path my-real-config
```

See `docs/gpu-workflow.md` for the full remote validation flow.

Config discovery follows:

1. `--configs-dir`
2. `VLLM_LOADER_CONFIGS`
3. `./configs`
4. `~/.config/vllm-loader/configs`

Run artifacts default to `~/.local/state/vllm-loader/runs/`. Durable logs are
scrubbed before display and persistence, and are created with mode `0600`.

Security note: vLLM API keys do not protect every network-reachable endpoint
such as `/invocations`. Keep the default localhost binding unless you are also
using a firewall or reverse proxy.
