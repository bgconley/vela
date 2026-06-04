# vLLM Loader

`vllm-loader` is a Python package and terminal TUI for launching vLLM servers
from named YAML configs, watching scrubbed logs, tracking load phases, probing
readiness, and managing per-target builds and model pins.

The current architecture is controller/agent based: the controller can run on a
workstation while a target agent owns process lifecycle, sidecars, GPU sampling,
health checks, build installs, and model downloads on the host that actually has
the files and GPUs.

## Quickstart

Install in editable mode and run the no-GPU smoke path:

```bash
pip install -e ".[dev]"
vllm-loader list
vllm-loader preview fake-child
vllm-loader run fake-child --preview
vllm-loader smoke fake-child
```

Open the TUI:

```bash
vllm-loader tui
```

Useful first checks:

```bash
vllm-loader targets list
vllm-loader build list
vllm-loader model list
```

## Remote Targets

Targets are stored on the controller in `~/.config/vllm-loader/targets.yaml`.
`local` is implicit. An SSH target runs `vllm-loader agent connect` on the
remote host; the remote daemon then performs all host-local work.

Example target:

```yaml
targets:
  blackbird:
    transport: ssh
    host: bgconley@10.25.0.51
    workdir: /home/bgconley/repos/lab-tui
    venv: /home/bgconley/venvs/lab-tui
    local_transport: socket
```

Register and test a target from the CLI:

```bash
vllm-loader targets add blackbird \
  --host bgconley@10.25.0.51 \
  --workdir /home/bgconley/repos/lab-tui \
  --venv /home/bgconley/venvs/lab-tui
vllm-loader targets test blackbird
```

For Mac to P620 controller to Blackbird agent validation, use SSH agent
forwarding and the nested target:

```bash
VLLM_LOADER_SSH_OPTS="-A -i /Users/brennanconley/vibecode/infx/ubuntu24_ed25519 -o BatchMode=yes" \
VLLM_LOADER_REMOTE_VENV=/home/bgconley/venvs/lab-tui \
VLLM_LOADER_REMOTE_TARGET=blackbird \
VLLM_LOADER_REMOTE_BUILD_SPEC=vllm==0.11.2 \
VLLM_LOADER_REMOTE_MODEL_REPO=hf-internal-testing/tiny-random-LlamaForCausalLM \
VLLM_LOADER_REMOTE_REAL_RESUME_CONFIG=tiny-random-llama-detached-blackbird \
  scripts/run_remote_tests.sh bgconley@10.25.0.50 /home/bgconley/repos/lab-tui \
  qwen36-27b-fp8-kvfp8-rp6000-blackbird
```

See [docs/gpu-workflow.md](docs/gpu-workflow.md) for the repeatable GPU
validation lane and artifact workflow.

## Config Schema

Config discovery runs on the target agent, because configs usually reference
target-local paths. Discovery order is:

1. `--configs-dir`
2. `VLLM_LOADER_CONFIGS`
3. `./configs`
4. `~/.config/vllm-loader/configs`

Common YAML fields:

```yaml
name: qwen-example
model: Qwen/Qwen3.6-27B-FP8
model_ref: pinned-qwen        # optional registry entry id/display name
revision: main               # optional model revision or commit
command:
  entrypoint: serve
  executable: ./scripts/launch_vllm.sh
  build: vllm-0-11-2
  cwd: /home/user/repos/lab-tui
engine:
  tensor_parallel_size: 1
  gpu_memory_utilization: 0.9
server:
  host: 127.0.0.1
  port: 8000
  exposure: local
logging:
  request_logging: false
env:
  CUDA_VISIBLE_DEVICES: "0"
extra_args: []
launch:
  mode: detached
  ready_timeout_seconds: 900
vllm:
  version_profile: "0.11"
```

Details are in [docs/configuration.md](docs/configuration.md).

## Build Methods

Managed builds are per-target venvs under the target data dir. They can be
created, verified, selected, repaired, removed, or adopted from an external
venv:

```bash
vllm-loader build add --target blackbird --method pip --spec 'vllm==0.11.2' --label vllm-0-11-2
vllm-loader build verify vllm-0-11-2 --target blackbird
vllm-loader build select vllm-0-11-2 --target blackbird
```

Build methods: `pip`, `nightly`, `commit`, `git`, `wheel`, and `adopt`.
`nightly` and `commit` require `uv` on the target because pip cannot enforce
the index-priority semantics those wheel feeds need. `pip`, `wheel`, and `git`
can fall back to Python venv plus pip.

Details are in [docs/builds-and-models.md](docs/builds-and-models.md).

## Model Registry

Models are cataloged, not owned. Hugging Face weights stay in the target's
standard HF cache, while the loader stores metadata, pins, and verification
state.

```bash
vllm-loader model pin tiny-llama \
  --target blackbird \
  --repo-id hf-internal-testing/tiny-random-LlamaForCausalLM \
  --revision main
vllm-loader model download tiny-llama --target blackbird
vllm-loader model verify tiny-llama --target blackbird
```

Keep `HF_TOKEN` on the target host for gated repos. Tokens are scrubbed before
job output leaves the agent.

## Agent/RPC Overview

The controller talks to a target through a uniform `TargetClient`. The agent
owns launch, stop, kill, restart, preflight, sidecar identity verification,
phase FSM, durable log scrubbing, health, GPU sampling, build jobs, and model
jobs. The controller passes run/job ids and renders events; it never signals
PIDs or dereferences target-local paths.

Core RPCs include `handshake`, `list_configs`, `preflight`, `preview`,
`launch`, `stop`, `kill`, `restart`, `subscribe`, `discover_runs`,
`reattach`, `list_builds`, `create_build`, `list_models`, and
`download_model`.

Details are in [docs/agent-rpc.md](docs/agent-rpc.md).

## Tested Matrix

The preferred architecture smoke is P620-01 controller (`10.25.0.50`) to
Blackbird agent (`10.25.0.51`) with the Qwen3.6 27B FP8 Docker stack. The latest
repeatable artifacts are
`artifacts/remote-validation/2026-06-04-p620-blackbird-b085610-build-model-resume.md`
and
`artifacts/remote-validation/2026-06-04-p620-blackbird-b085610-qwen-smoke.md`.

The fallback 620-01 host-local lane covers the Qwen3-32B FP8 lab surface. Treat
these as tested lab surfaces; when bumping vLLM, rerun the remote validation
lane and update fixtures/profile rules if startup or error text changes.

## Security Notes

Run artifacts default to `~/.local/state/vllm-loader/runs/` on the target.
Durable logs are scrubbed before display and persistence and are created with
mode `0600`.

vLLM API keys do not protect every network-reachable endpoint, including
`/invocations`. Keep the default localhost binding unless the host is protected
by a firewall or reverse proxy.
