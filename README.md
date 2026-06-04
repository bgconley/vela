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
git push origin main
scripts/run_remote_tests.sh USER@GPU_HOST /tank/repos/lab-tui
scripts/run_remote_tests.sh USER@GPU_HOST /tank/repos/lab-tui my-real-config
```

Pre-release validation can write a dated Markdown artifact from the same
command:

```bash
VLLM_LOADER_REMOTE_ARTIFACT=1 \
VLLM_LOADER_REMOTE_BUILD_SPEC='vllm==0.11.2' \
VLLM_LOADER_REMOTE_MODEL_REPO=hf-internal-testing/tiny-random-LlamaForCausalLM \
  scripts/run_remote_tests.sh USER@GPU_HOST /tank/repos/lab-tui my-real-config
```

Preferred real smoke target:

```bash
git push origin main
VLLM_LOADER_REMOTE_VENV=/home/bgconley/venvs/lab-tui \
  scripts/run_remote_tests.sh bgconley@10.25.0.51 /home/bgconley/repos/lab-tui \
  qwen36-27b-fp8-kvfp8-rp6000-blackbird
```

See `docs/gpu-workflow.md` for the full remote validation flow and the manual
`Remote Validation` GitHub Actions lane.

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
