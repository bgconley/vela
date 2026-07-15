# Environment variables and storage paths

[Documentation index](index.md) · [Configuration](configuration.md) ·
[Operations](operations.md) · [Troubleshooting](troubleshooting.md)

This page is the operator reference for Vela's process environment and default
filesystem layout. It covers the application itself and, in a separate section,
the repository's remote-validation harness.

## Set variables on the process that uses them

Vela has three execution scopes:

1. The **controller** is the `vela` CLI or TUI you started.
2. The **target agent** reads configs, owns registries, performs preflight, and
   launches or stops workloads. For an SSH target, this process runs on the
   target host.
3. The **workload** is the vLLM process or Docker container launched by the
   agent.

An exported variable is not automatically copied across those boundaries. For
example, `VELA_TARGET` belongs on the controller, while a gated model's
`HF_TOKEN` must be available to the target agent. Paths are target-local too:
`$XDG_STATE_HOME/vela/models/registry.json` is on whichever host runs the
agent.

The local socket agent inherits its environment when the daemon starts. Restart
it after changing agent-side variables:

```bash
vela agent restart
```

An SSH stdio agent receives the remote non-interactive SSH environment, not the
controller's shell environment. Put persistent target-side values in the
target's service or approved shell setup, or use an explicit target
`agent_command` wrapper. Do not assume interactive shell startup files run over
SSH.

## Operator controls

| Variable | Scope | Default | Effect |
| --- | --- | --- | --- |
| `VELA_TARGET` | controller | saved default target, then `local` | Selects the target when a command has no `--target`. An explicit `--target` wins. A deployment YAML's `target:` field does not route execution. |
| `VELA_CONFIGS` | process loading configs | unset | Selects one deployment-config directory when `--configs-dir` is absent. See [Config discovery](#config-discovery-is-not-a-merge). |
| `VELA_AGENT_RUNTIME_DIR` | local agent/controller | unset | Highest-priority override for the daemon socket, identity, and startup-error directory. Used verbatim; Vela does not append `/vela`. |
| `VELA_AGENT_TOKEN` | controller and agent | token file, if present | Capability token used by the RPC handshake. Both sides must use the same high-entropy value. Prefer the installed token file over a long-lived shell export. |
| `VELA_AGENT_TOKEN_FILE` | controller and agent | `$XDG_CONFIG_HOME/vela/agent-token` | Overrides the capability-token file path independently on each side. |
| `VELA_AGENT_REQUIRE_TOKEN` | agent | false | Truthy values `1`, `true`, `yes`, and `on` make the agent fail closed if no valid token is configured. Recommended on shared accounts or hosts. |
| `VELA_AGENT_JOB_RETENTION_LIMIT` | agent | `50` | Keeps at least this many newest terminal async jobs and their in-memory event buffers. Invalid values use the default; negative values clamp to zero. |
| `VELA_AGENT_JOB_RETENTION_SECONDS` | agent | `3600` | Keeps terminal async jobs for at least this many seconds. Invalid values use the default; negative values clamp to zero. |
| `VELA_MIN_FREE_DISK_BYTES` | agent | `1073741824` (1 GiB) | Minimum generic free-space headroom used by preflight. `0` disables this generic guard. Invalid values restore the default. |
| `VELA_DOCKER_PULL_TIMEOUT_SECONDS` | agent | `1800` | Timeout for `docker pull`. It accepts fractional seconds; `0` or a negative value disables the pull-specific timeout. Invalid values restore the default. |
| `EDITOR` | controller | platform editor resolution | Editor used by `vela config edit`. |

The two job-retention rules are additive: a terminal job remains available if
it is among the newest count **or** is younger than the age limit. Set both
retention values to `0` to prune terminal in-memory job state immediately.
These settings do not delete run artifacts or model/build registries.

### Target precedence

The active target resolves in this order:

1. command-line `--target`;
2. non-empty `VELA_TARGET`;
3. `default_target` saved by `vela targets use`;
4. `local`.

Use `vela targets list` to see the saved default and `vela doctor --json` to
inspect controller and target setup.

### Config discovery is not a merge

When `--configs-dir` is absent, config lookup resolves in this order:

1. `VELA_CONFIGS`, when non-empty;
2. the first existing directory among:
   - `$PWD/configs`;
   - `$XDG_CONFIG_HOME/vela/configs`, or `~/.config/vela/configs` when
     `XDG_CONFIG_HOME` is unset.

Only one directory is loaded. An existing but empty `$PWD/configs` shadows the
XDG config directory. If neither default exists, Vela reports `$PWD/configs` as
the first location.

Config-writing commands are a separate concern. On the target, commands such as
deployment create/edit/clone/delete and config push use an explicit
`--configs-dir` when supplied; otherwise their target-side fallback is
`$PWD/configs`. Pass `--configs-dir` for deterministic automation, especially
when the controller and target have different working directories.

## XDG variables and path precedence

| Variable | Vela fallback | Vela-owned data |
| --- | --- | --- |
| `XDG_CONFIG_HOME` | `~/.config` | targets, installed agent token, and the user config candidate |
| `XDG_DATA_HOME` | `~/.local/share` | managed builds |
| `XDG_STATE_HOME` | `~/.local/state` | runs, model registry, debug log, and daemon fallback state |
| `XDG_RUNTIME_DIR` | unset | preferred transient daemon runtime directory |
| `XDG_CACHE_HOME` | `~/.cache` | not read directly by Vela; Hugging Face uses it when deriving `HF_HOME` |

Do not point unrelated hosts at a shared XDG tree. Build manifests, model
metadata, run identities, and daemon sockets describe host-local resources.

### Storage-path reference

| Purpose | Resolution |
| --- | --- |
| Deployment configs read by the loader | explicit `--configs-dir` > `VELA_CONFIGS` > first existing `$PWD/configs` or `$XDG_CONFIG_HOME/vela/configs` |
| Target registry and saved default target | `$XDG_CONFIG_HOME/vela/targets.yaml`; legacy `targets.json` is read only when YAML is absent |
| Installed agent token | `VELA_AGENT_TOKEN_FILE` > `$XDG_CONFIG_HOME/vela/agent-token` |
| Managed builds | `$XDG_DATA_HOME/vela/builds` |
| Model metadata registry | `$XDG_STATE_HOME/vela/models/registry.json` |
| Hugging Face model weights | `HF_HUB_CACHE`, as resolved by `huggingface_hub` |
| Default run artifacts | `$XDG_STATE_HOME/vela/runs` |
| TUI debug log with `--debug` | `$XDG_STATE_HOME/vela/debug.jsonl`, unless `--debug-log` is supplied |
| Agent runtime directory | `VELA_AGENT_RUNTIME_DIR` > `$XDG_RUNTIME_DIR/vela` > `$XDG_STATE_HOME/vela` > `~/.local/state/vela` |
| Agent socket | `<agent-runtime-dir>/agent.sock` |
| Agent identity | `<agent-runtime-dir>/agent.json` |
| Agent startup stderr | `<agent-runtime-dir>/agent-start.err` |

A controller can temporarily connect to a live daemon on the pre-XDG-state
legacy socket when the new canonical socket is absent. `vela agent status
--json` reports the socket actually in use. New daemons use the canonical
precedence above.

Per-deployment `launch.runs_dir` overrides the default runs directory. Composer
generated configs commonly use a deployment-specific child under the default
runs root.

## Hugging Face authentication and cache controls

Vela delegates cache resolution and downloads to `huggingface_hub`. Set these
variables before starting the target agent; Hugging Face constants may be
resolved during process import.

| Variable | Scope | Purpose |
| --- | --- | --- |
| `HF_TOKEN` | target agent and model-download jobs | Authenticates gated or private repositories. Vela does not store it in the model registry. |
| `HF_HOME` | target agent | Root for Hugging Face state. Default is `$XDG_CACHE_HOME/huggingface`, or `~/.cache/huggingface`. |
| `HF_HUB_CACHE` | target agent | Exact Hub snapshot/cache directory used by Vela downloads and scans. Default is `$HF_HOME/hub`. |
| `HUGGINGFACE_HUB_CACHE` | target agent | Legacy upstream cache override. `HF_HUB_CACHE` takes precedence when both are set. |
| `HF_HUB_OFFLINE` | target agent or deployment `env` | Truthy upstream setting disables Hub network access. Vela blocks a remote-only pinned model when offline mode is active. |
| `HF_HUB_DISABLE_IMPLICIT_TOKEN` | target agent | Upstream hardening switch that prevents implicit token use. The remote-validation gated-auth lane uses it for an intentional no-token probe. |

The model registry and the weight cache are different things:

- `$XDG_STATE_HOME/vela/models/registry.json` stores Vela metadata, pins,
  integrity information, and aliases.
- `HF_HUB_CACHE` stores downloaded Hugging Face blobs and snapshots.

Deleting one does not safely remove the other. Use `vela model remove ... --yes`
so Vela can enforce live-use/config-pin protection and account for deduplicated
cache content.

### Docker cache visibility

The default Docker composition bind-mounts the agent's `HF_HOME` into the
container at `/root/.cache/huggingface`. If `HF_HUB_CACHE` is relocated outside
`$HF_HOME/hub`, that default mount does not contain the downloaded snapshots.
Vela warns about this mismatch. Fix it with an explicit
`command.docker.hf_cache`/`hf_cache_target` or a covering volume; do not download
a second copy merely to silence the warning.

### Gated-model setup

Keep the token on the target that performs the download and launch. For a
one-session local-agent setup, read it without placing the value in shell
history, then restart the daemon from that same shell:

```bash
# Run on the target host; input is hidden and not stored in command history.
read -rsp 'HF token: ' HF_TOKEN && printf '\n'
export HF_TOKEN
vela agent restart
```

For a persistent service, use that target's approved secret store/service
environment. For SSH stdio, configure the remote non-interactive environment or
an approved `agent_command` wrapper; exporting on the controller does not send
the token across SSH.

Avoid putting `HF_TOKEN` in deployment YAML, shell history, screenshots, or CI
variables that are not secret-scoped. Confirm access without printing the
token:

```bash
vela model inspect MODEL_REF
vela model download MODEL_REF
vela model verify MODEL_REF
```

## CUDA, build, and workload environment

These variables are read on the target. Put per-deployment runtime values under
the config's `env:` mapping; put build-time values in `vela build add --env
KEY=VALUE`. A managed-build handoff and a normal deployment environment are not
the same scope.

| Variable | Who controls it | Behavior |
| --- | --- | --- |
| `CUDA_VISIBLE_DEVICES` | operator/config | Limits visible GPUs. Vela uses the deployment value for tensor/pipeline world-size preflight and the agent value when mapping GPU monitoring samples. |
| `CUDA_VERSION` | operator/platform | First CUDA toolkit-version hint used by diagnostics. |
| `CUDA_HOME` | operator/platform | Toolkit root checked for `version.txt` when `CUDA_VERSION` is absent. |
| `CUDA_PATH` | operator/platform | Secondary toolkit-root hint when `CUDA_HOME` is absent or unusable. |
| `NVIDIA_DRIVER_VERSION` | operator/platform | Driver-version override for diagnostics; otherwise Vela queries `nvidia-smi`. |
| `PATH` | platform/Vela | Used to discover `vela`, `vllm`, `uv`, Docker, CUDA tools, and Python. Managed builds prepend their venv `bin` directory for launched children. |
| `VIRTUAL_ENV` | Vela build handoff | Set to a selected managed/adopted build venv for the launched child. No shell activation script is required. |
| `PATH_PREPEND` | Vela internal | Internal build-handoff field consumed into `PATH` and removed before child launch. Do not set it as a user config contract. |
| `VLLM_USE_PRECOMPILED` | Vela build installer | Set to `1` for a build created with `vela build add --precompiled`. It is an install overlay, not a global runtime requirement. |
| `VLLM_API_KEY` | operator/Vela | Workload API-key environment understood by vLLM. A configured `server.api_key` maps to this value; an ambient value alone is not a complete Vela probe/container setup. See the limitation below. |
| `PYTHONUNBUFFERED` | Vela | Set to `1` for launched process logs. |

Other vLLM, Torch, FlashInfer, NCCL, CUDA, or allocator variables are forwarded
as ordinary build/deployment environment values; Vela does not validate their
upstream semantics. Examples include `TORCH_CUDA_ARCH_LIST`,
`FLASHINFER_CUDA_ARCH_LIST`, and `PYTORCH_CUDA_ALLOC_CONF`. Keep hardware-specific
values in target-specific configs rather than a global controller shell.

Example deployment scope:

```yaml
env:
  CUDA_VISIBLE_DEVICES: "0,1"
  PYTORCH_CUDA_ALLOC_CONF: expandable_segments:True
```

Example build-install scope:

```bash
vela build add --method git \
  --url https://github.com/vllm-project/vllm.git \
  --ref COMMIT \
  --env TORCH_CUDA_ARCH_LIST=12.0 \
  --target oxcart
```

### API-key limitation

Vela's current `server.api_key` field is passed directly to the child as
`VLLM_API_KEY` and is also used by Vela's `/v1/models` readiness probe. Docker
receives only environment entries Vela explicitly constructs. Consequently:

- an ambient target-side `VLLM_API_KEY` is not consulted by the readiness probe;
- it is not automatically forwarded into a Docker container;
- `$VLLM_API_KEY` written in YAML is accepted as a placeholder but is not shell-
  expanded during a normal launch; and
- storing a real value in `server.api_key` would persist a secret in YAML.

The composer, lint, and validated write paths reject literal secret-looking
values, but the low-level YAML loader alone does not sanitize arbitrary manually
authored files. Do not use that gap as a secret mechanism. Until Vela has a
target-side secret-reference resolver, use loopback exposure plus an
authenticated reverse proxy or firewall for real credentials. The non-secret
`EMPTY` sentinel used by isolated lab recipes is only suitable where network
access is already constrained; it is not authentication.

## TUI and terminal variables

| Variable | Default/behavior |
| --- | --- |
| `NO_COLOR` | When present, prevents Vela from forcing Textual truecolor. |
| `TEXTUAL_COLOR_SYSTEM` | Defaults to `truecolor` when `NO_COLOR` is absent. An explicit existing value is preserved. |
| `TEXTUAL` | Textual feature list. `vela --debug` adds `debug` and `devtools` while preserving existing entries. |

For a permanent low-color environment, set `NO_COLOR=1` rather than editing the
application theme. Use `--debug-log PATH` when multiple isolated controller
sessions must not share the default debug log.

## SSH option environment

A target may name an environment variable through its `ssh_opts_env` field:

```yaml
targets:
  oxcart:
    transport: ssh
    host: user@oxcart
    ssh_opts_env: VELA_OXCART_SSH_OPTS
```

The controller shell can then supply additional options:

```bash
export VELA_OXCART_SSH_OPTS='-i ~/.ssh/oxcart -o ConnectTimeout=10'
```

Application target transport validates this string before use. It rejects
positional hosts, target overrides, forwarding, remote commands, provider or
routing configuration, TTY/session suppression, and host-verification
weakening. The variable name is operator-defined; `VELA_OXCART_SSH_OPTS` is only
an example.

`VELA_SSH_OPTS`, described below, is different: repository maintenance scripts
consume it as a trusted raw option string. Do not confuse that script interface
with the validated application target interface.

## Maintainer-only remote-validation variables

The `VELA_REMOTE_*` namespace is not part of normal Vela CLI configuration. It
belongs to `scripts/run_remote_tests.sh` and
`.github/workflows/remote-validation.yml`. Application users should use
`--target`, `VELA_TARGET`, target definitions, and deployment configs instead.

### Validation script controls

| Variable | Manual-script default | Purpose |
| --- | --- | --- |
| `VELA_REMOTE_TIMEOUT` | `1800` | Timeout in seconds for real smoke validation. The workflow defaults to `2700`. |
| `VELA_REMOTE_PYTHON` | `auto` | Python used to create the validation venv. Auto checks `/tank/preproc/venv/bin/python`, then `python3`, then `python`. |
| `VELA_REMOTE_VENV` | `/tank/venvs/vela` | Reusable venv used to install and execute the checked-out revision. |
| `VELA_REMOTE_AGENT_RUNTIME_DIR` | `<remote-venv>/agent-runtime` | Isolated validation daemon directory; becomes `VELA_AGENT_RUNTIME_DIR` on the remote controller. |
| `VELA_REMOTE_BRANCH` | `main` | Published branch fetched into the owned validation worktree. |
| `VELA_REMOTE_EXPECTED_SHA` | resolved branch head | Exact revision required after fetch and checkout. A mismatch fails before tests. |
| `VELA_REMOTE_PYTEST_ARGS` | `-q` | Trusted shell-split pytest arguments on the remote host. The workflow supplies a narrower default test list. |
| `VELA_REMOTE_TARGET` | unset | Optional Vela target used for build/model/config checks from the remote controller. |
| `VELA_REMOTE_BUILD_METHOD` | `pip` | Build method used when a real build spec is requested. |
| `VELA_REMOTE_BUILD_SPEC` | unset | Enables real build add/verify using this spec. |
| `VELA_REMOTE_BUILD_LABEL` | `remote-smoke-build` | Label for the validation build. |
| `VELA_REMOTE_MODEL_REPO` | unset | Hugging Face repo to pin before validation download. |
| `VELA_REMOTE_MODEL_REF` | unset | Existing pinned model reference to download instead of pinning a repo. |
| `VELA_REMOTE_MODEL_ID` | `remote-smoke-model` | Display name for the validation pin. |
| `VELA_REMOTE_MODEL_REVISION` | unset | Optional model revision for pin/download. |
| `VELA_REMOTE_GATED_MODEL_REPO` | unset | Enables the intentional no-token gated-auth probe. |
| `VELA_REMOTE_GATED_MODEL_ID` | `remote-gated-model` | Display name for the gated probe entry. |
| `VELA_REMOTE_GATED_MODEL_REVISION` | unset | Optional gated-model revision. |
| `VELA_REMOTE_REAL_RESUME_CONFIG` | unset | Enables real-model daemon restart and reconnect/resume validation. |
| `VELA_REMOTE_ARTIFACT` | unset | `1` writes a Markdown evidence artifact on the machine running the script. |
| `VELA_REMOTE_ARTIFACT_DIR` | `artifacts/remote-validation` when enabled | Local artifact directory. Supplying a directory also enables artifact output. |
| `VELA_REMOTE_ARTIFACT_NAME` | timestamp/host/config-derived | Deterministic artifact filename override. |

The manual script still takes its remote host, absolute repository path, and
optional real config as positional arguments:

```bash
scripts/run_remote_tests.sh USER@HOST /absolute/repo/path [REAL_CONFIG]
```

### GitHub Actions repository variables and secret

These names configure `workflow_dispatch`; the workflow converts them into
script arguments or environment values:

| Variable | Default | Purpose |
| --- | --- | --- |
| `VELA_REMOTE_HOST` | required unless dispatched explicitly | SSH host for the GPU/controller validation node. |
| `VELA_REMOTE_PATH` | required unless dispatched explicitly | Absolute repository path on that host. |
| `VELA_REMOTE_PROFILE` | `full` | `full` runs requested real surfaces; `fast` clears real resume/config validation. |
| `VELA_REMOTE_RUNNER_LABEL` | `self-hosted` | Runner label with network access to the host. |
| `VELA_REMOTE_REAL_CONFIG` | unset | Real config passed as the script's third positional argument. |
| `VELA_REMOTE_SSH_OPTS` | batch mode plus `StrictHostKeyChecking=accept-new` | Trusted outer-hop SSH options used by the workflow. |
| `VELA_REMOTE_SSH_KEY` | unset | GitHub Actions **secret** containing the SSH private key. A legacy secret fallback may also be accepted by the workflow. |

The workflow also accepts dispatch inputs and sets run-specific build/model
labels, branch, exact SHA, artifact directory, and timeout. Inputs are evaluated
before repository variables. Empty inputs fall back to repository variables,
but non-empty input defaults normally win: `validation_profile` defaults to
`full`, `runner_label` to `self-hosted`, `ssh_opts` to its batch/host-key string,
`remote_timeout` to `2700`, and the build/model inputs also have non-empty
defaults. Set those values in the dispatch form when you need to override their
defaults; repository variables are reliable fallbacks only for inputs left
empty (such as host, path, target, venv, and real-config fields) or for variables
with no dispatch input.

### Raw maintenance-script SSH options

`scripts/run_remote_tests.sh` and `scripts/rsync_to_gpu.sh` read
`VELA_SSH_OPTS`. They shell-split or interpolate this trusted maintainer value
for the outer SSH/rsync hop. Unlike application `ssh_opts_env`, this path is not
the strict target-transport allowlist.

Never populate `VELA_SSH_OPTS`, `VELA_REMOTE_PYTEST_ARGS`, or other
shell-interpreted maintenance controls from untrusted input. In CI, store the
private key in `VELA_REMOTE_SSH_KEY` as a GitHub secret, not a repository
variable. Keep the GitHub runner and remote host keys restricted to the
validation environment.

## Security guidance

- Generate agent tokens with `vela agent gen-token --install`; installed token
  files are written with mode `0600`. Do not hand-author weak shared secrets.
- Set `VELA_AGENT_REQUIRE_TOKEN=1` for agents reachable by multiple users or
  where peer credentials cannot be verified. The controller and agent must use
  the same token.
- Keep `HF_TOKEN`, SSH private keys, and API keys out of YAML, Git, command
  history, screenshots, and non-secret CI variables. Vela's composer, lint, and
  validated write paths reject literal secret-looking fields and scrub known
  values from previews, events, and logs. Arbitrary manually authored YAML is
  not universally rejected again at launch; redaction is not a substitute for
  correct storage. See [API-key limitation](#api-key-limitation).
- Do not print `env` or run `set -x` in a shell that contains credentials.
- `server.api_key`/`VLLM_API_KEY` does not protect every vLLM endpoint. A
  non-loopback deployment still needs an appropriate firewall or authenticated
  reverse proxy. See [Operations](operations.md).
- Prefer the validated target `ssh_opts_env` interface for application SSH.
  Host-verification weakening and forwarding are deliberately rejected there.
- Treat `XDG_*`, cache, and runtime paths as host-local. Sharing an agent socket,
  build registry, or run-state directory across machines can defeat Vela's
  identity and ownership checks.

## Safe path diagnostics

These checks show effective paths without dumping tokens:

```bash
vela doctor --json
vela agent status --json

python - <<'PY'
from vela.agent.auth import default_agent_token_file
from vela.agent.daemon import default_agent_runtime_dir
from vela.config.schema import default_run_artifacts_dir
from vela.engine.build_registry import default_builds_root
from vela.engine.model_registry import (
    default_hf_home_dir,
    default_hf_hub_cache_dir,
    default_models_registry_path,
)

print("agent runtime:", default_agent_runtime_dir())
print("agent token file:", default_agent_token_file())
print("runs:", default_run_artifacts_dir())
print("builds:", default_builds_root())
print("model registry:", default_models_registry_path())
print("HF_HOME:", default_hf_home_dir())
print("HF_HUB_CACHE:", default_hf_hub_cache_dir())
PY
```

For recovery procedures, use [Troubleshooting](troubleshooting.md). For the YAML
fields that create target-side workload environment, use
[Configuration](configuration.md).
