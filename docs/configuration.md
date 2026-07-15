# Configuration

[Documentation home](index.md) · [Getting started](getting-started.md) · [Operations guide](operations.md) · [CLI reference](cli-reference.md)

The controller chooses a target, then the target agent discovers configs on its
own filesystem. That keeps target-local paths honest: a model path, build id,
working directory, or wrapper script is resolved on the machine that will launch
vLLM.

## Target Registry

Targets live on the controller in `~/.config/vela/targets.yaml`.
`local` is implicit and cannot be removed.

```yaml
targets:
  gpu-node:
    transport: ssh
    host: user@gpu-host
    ssh_key: ~/.ssh/vela_ed25519
    workdir: /path/to/vela
    venv: /path/to/venv
    agent_command:
      - /path/to/venv/bin/vela
      - agent
      - connect
    local_transport: socket
```

### Choosing a target

Commands that operate on target-owned state take `--target NAME`. When it is
omitted, the target resolves in precedence order: the explicit `--target` flag,
then the `VELA_TARGET` environment variable, then a persisted default, then the
implicit `local`. Controller-local commands such as `version`, daemon
start/stop/restart, completion, and `runs prune` have no target option.

Persist a default with `vela targets use NAME` (clear it with
`vela targets use --clear`). It is stored as `default_target` in `targets.yaml`,
and `vela targets list` marks it with a leading `*`.

### One canonical command per operation

Each operation has a single canonical command; a few historical spellings remain
as hidden aliases so old scripts keep working but only the canonical verb shows
in `--help`: `vela list` (alias `vela deploy list`), `vela run --preview` (alias
`vela preview`), and `vela model pin` (alias `vela model add`).

Fields:

- `transport`: `local` or `ssh`.
- `host`: SSH host for remote targets.
- `ssh_key`: optional target-specific private key passed as `ssh -i`.
- `workdir`: remote directory used before starting `vela agent connect`.
- `venv`: remote venv whose `bin` directory is prepended to `PATH`.
- `agent_command`: optional argv list replacing the default `vela agent connect`.
  This is useful when the target has Vela installed in an absolute venv path.
- `local_transport`: `socket` or `in_process`; use `in_process` only for tests.
- `socket_path`: optional explicit Unix socket for a socket-backed local target.
- `ssh_opts_env`: optional environment variable containing SSH options. It may
  add option flags such as `-a`, `-i`, `-J`, `-p`, or `-o Key=Value`, but
  positional SSH arguments, agent forwarding (`-A`, `ForwardAgent=yes`), port
  forwarding (`-L`, `-R`, `-D`, `LocalForward`, `RemoteForward`,
  `DynamicForward`), command-suppression, and command-bearing `-o` options such
  as `ProxyCommand`, `RemoteCommand`, or `LocalCommand` are rejected. External
  config/control socket options (`-F`, `-S`, `Include`, `ControlPath`) and
  user/host override options (`-l`, `User`, `HostName`) are also rejected.
  Provider-loading options (`-I`,
  `PKCS11Provider`, `SecurityKeyProvider`) are rejected so `ssh_opts_env` cannot
  load local provider code while connecting. Host-verification weakening options
  such as `StrictHostKeyChecking=no`, `CheckHostIP=no`, null known-hosts files,
  `HostKeyAlgorithms`, and `KnownHostsCommand` are rejected. The configured
  `BatchMode`, `ServerAliveInterval`, and `ServerAliveCountMax` options are also
  managed by Vela and cannot be supplied through this environment hook.
  TTY allocation options (`-t`, `-tt`, `RequestTTY=yes`) are rejected because
  the agent transport is an NDJSON stdio stream; explicit TTY disabling (`-T` or
  `RequestTTY=no`) is allowed. Stdio/session suppression options
  (`ForkAfterAuthentication=yes`, `SessionType=none`, `StdinNull=yes`) are also
  rejected because they detach, omit, or starve the agent RPC stream.
  Vela also adds `-a` to the generated SSH command so agent forwarding
  stays disabled even when a user's SSH config enables it by default.
  The configured target host and `agent connect` command cannot be replaced by
  the environment.

For first-run setup, `vela targets bootstrap` writes the same registry shape and
`vela doctor` reports missing setup steps. `vela agent gen-token --install`
writes a capability token to the default file read by the agent when
`VELA_AGENT_TOKEN` is not set. For SSH targets, run
`vela agent gen-token --install --target <name>` to install the same token on
the controller and the target agent's default token file. `vela doctor --target
<name>` reports target auth as `none`, `required+provided`,
`required+missing`, `mismatch`, or `malformed-token`; any non-ready token state
uses the same target-aware install command as its next step.

## Config Discovery

Config discovery runs agent-side in this order and selects the first applicable
directory; roots are not merged:

1. `--configs-dir`
2. `VELA_CONFIGS`
3. `./configs`
4. `~/.config/vela/configs`

Paths are resolved on the host that owns the agent, not on the controller. Use
`vela doctor --target <name>`, `vela targets test <name>`, or
`vela agent status --target <name>` to see the target host's resolved config,
runs, builds, model-registry, token, and socket paths.

Use `vela config edit <name> --target <name>` for a target-owned edit
round-trip: Vela pulls the YAML from the target, opens `$EDITOR`, asks the
target agent to lint the edited text, and only then pushes it back with the
same config name. Literal secrets are rejected by the target lint step before
the push happens.

In the TUI, press `t` for Target Manager. From there, `b` shows the exact
`vela targets bootstrap ... --install` command for the selected target, and
`p` pushes the currently selected local config to the selected remote target
through the agent's `push_config` validation/write path.

Host path overrides:

- `VELA_CONFIGS`: overrides config discovery for that agent process.
- `XDG_CONFIG_HOME`: controls defaults such as `~/.config/vela` and the
  managed `agent-token` file.
- `XDG_DATA_HOME`: controls the managed build root, defaulting to
  `~/.local/share/vela/builds`.
- `XDG_STATE_HOME`: controls durable run/log state and model-registry metadata,
  defaulting to `~/.local/state/vela/...`. Model weights remain in the Hugging
  Face cache rather than either Vela-owned root.
- `XDG_RUNTIME_DIR`: controls the Unix agent socket directory when present.

## Config Fields

Minimal config:

```yaml
name: fake-child
target: blackbird  # optional home target label
model: fake/model
server:
  host: 127.0.0.1
  port: 8765
logging:
  request_logging: false
launch:
  mode: detached
```

Important fields:

- `name`: unique config name.
- `target`: optional home target label. The active CLI `--target` or TUI target
  still decides which agent receives the request; absent means "use the active
  target." The label is useful for config detail, review, and avoiding
  wrong-host confusion.
- `model`: repo id, local path, or URL handed to vLLM when `model_ref` is not
  used.
- `model_ref`: optional model-registry entry id or display name.
- `revision`: optional model revision or resolved commit.
- `served_model_name`: optional OpenAI-compatible served model name.
- `command.entrypoint`: `serve` or module entrypoint.
- `command.runtime`: `process` or `docker`.
- `command.executable`: explicit vLLM executable or wrapper script for process
  runtime.
- `command.build`: managed build id/label for process runtime; overrides the
  target default build.
- `command.docker`: Docker runtime settings such as `image`, `container_name`,
  optional Docker `runtime`, `gpus`, `network`, `ipc_host`, `shm_size`,
  `hf_cache`, `volumes`, `env`, `pull`, `auto_remove`, `evict`, and
  `extra_run_args`. `auto_remove: true` emits Docker `--rm`, requires
  `restart: "no"`, and removes the stopped container after Vela stops it.
- `command.cwd`: target-local working directory for relative paths.
- `engine`: modeled vLLM flags. vLLM-owned values default to unset so the
  installed vLLM default wins.
- `server.host`, `server.port`, `server.exposure`: bind/probe settings.
- `server.api_key`: direct vLLM API-key value used by the child and readiness
  probe. Composer/lint reject real literal secrets, and target-side environment
  references are not yet resolved end to end; see the
  [API-key limitation](environment.md#api-key-limitation).
- `server.probe_host`: optional host used for readiness probes when it differs
  from the bind host.
- `logging.request_logging`: app policy for request logging flags.
- `logging.suppress_access_log_for`: endpoint-specific access log suppression.
- `env`: target-side environment overlay. Keep secrets on the target.
- `extra_args`: passthrough flags appended after modeled flags.
- `launch.mode`: compatibility label; all agent launches are supervised.
- `launch.runs_dir`: optional target-local run artifact directory.
- `launch.ready_timeout_seconds`: launch readiness timeout.
- `launch.required_hostname`: optional exact target hostname guard for a
  machine-specific profile. Target-side preflight rejects a mismatch before
  Vela starts, stops, removes, or replaces any process or container.
- `vllm.version_profile`: optional flag-compatibility profile hint. This is
  not necessarily the runtime package version inside a pinned Docker image.
- `vllm.version`, `vllm.transformers_version`, `vllm.torch_version`,
  `vllm.cuda_version`: optional provenance for a known-good runtime stack.

### Complete deployment schema reference

Every object rejects unknown keys. `null` below means the field is optional and
left to vLLM, the selected runtime, or composition logic. Newly composed process
profiles must resolve an immutable build; newly composed Docker profiles must use
a complete `@sha256:<64 hex>` image digest even though the loader can still read
older configs for migration.

#### Top level and command

| Field | Default | Contract |
| --- | --- | --- |
| `name` | required | Non-empty deployment/profile name; must be unique in the selected config root. |
| `target` | `null` | Informational home target label; never overrides CLI/TUI routing. |
| `description` | `null` | Operator-facing profile description. |
| `model` | required | Hugging Face repo ID, URL, or target-local path. |
| `revision` | `null` | Revision or immutable commit baked into the launch. |
| `model_ref` | `null` | Model registry ID, unique display name/alias, or unique repo ID. Cannot accompany an explicit local `model` path. |
| `served_model_name` | model basename | ID exposed by the OpenAI-compatible model endpoint. |
| `env` | `{}` | Target-side process environment; values are normalized to strings. |
| `extra_args` | `[]` | Raw vLLM arguments appended after modeled flags. |
| `command` | process defaults | Runtime, entrypoint, executable/build, working directory, and optional Docker block described below. |
| `engine` | `{}` | Modeled vLLM engine flags described below; unset values preserve runtime defaults. |
| `server` | loopback on port `8000` | Bind, exposure, API-key, and probe settings described below. |
| `logging` | request logging disabled | vLLM request-log controls described below. |
| `launch` | supervised attached defaults | Readiness, artifact, cache, and hostname policy described below. |
| `vllm` | `{}` | Flag-compatibility requirements and proven runtime-version metadata described below. |
| `command.runtime` | `process` | Persisted values are `process` or `docker`. CLI composer values `build` and `executable` are shorthands that save as process runtime. |
| `command.entrypoint` | `serve` | `serve` or `module`; Docker requires `serve`. |
| `command.executable` | `null` | Target-local executable/wrapper for process runtime; mutually exclusive with `command.build`. |
| `command.build` | `null` | Managed build ID or label for process runtime; mutually exclusive with `command.executable`. |
| `command.cwd` | `null` | Target-local working directory used to resolve relative paths. |
| `command.docker` | `null` | Required for Docker runtime and forbidden for process runtime. |

#### Engine

| Field | Default | Constraint / emitted behavior |
| --- | --- | --- |
| `engine.tensor_parallel_size` | `null` | Integer ≥ 1; `--tensor-parallel-size`. |
| `engine.pipeline_parallel_size` | `null` | Integer ≥ 1; `--pipeline-parallel-size`. |
| `engine.gpu_memory_utilization` | `null` | Float > 0 and ≤ 1. |
| `engine.max_model_len` | `null` | Integer ≥ 1. |
| `engine.dtype` | `null` | `auto`, `half`, `float16`, `bfloat16`, `float`, or `float32`. |
| `engine.quantization` | `null` | vLLM quantization value; compatibility is checked against the selected runtime. |
| `engine.kv_cache_dtype` | `null` | vLLM KV-cache dtype. |
| `engine.load_format` | `null` | vLLM model load format. |
| `engine.enforce_eager` | `null` | Boolean eager-mode choice. |
| `engine.swap_space` | `null` | Integer ≥ 0. |
| `engine.block_size` | `null` | Integer ≥ 1. |
| `engine.seed` | `null` | Integer random seed. |
| `engine.max_num_seqs` | `null` | Integer ≥ 1. |

#### Server and logging

| Field | Default | Contract |
| --- | --- | --- |
| `server.host` | `127.0.0.1` | Bind host. Non-loopback/wildcard values require `lan` or `public` exposure. |
| `server.port` | `8000` | Integer from 1 through 65535. |
| `server.exposure` | `local` | `local`, `lan`, or `public`; explicit operator acknowledgement, not a firewall. |
| `server.api_key` | `null` | Direct value passed to vLLM and used by readiness probes. It is redacted from output, but the current composer rejects literal secrets and does not resolve target-side placeholders; see the environment reference. A key does not secure every endpoint. |
| `server.probe_host` | `null` | Alternative target-side host for health probes. |
| `logging.request_logging` | `false` | Enables/disables vLLM request logging according to runtime support. |
| `logging.suppress_access_log_for` | `[]` | Endpoint paths whose access logs should be suppressed when supported. |
| `logging.max_log_len` | `null` | Value passed to vLLM's `--max-log-len` request/prompt logging control; it does not truncate Vela's scrubbed log records. |

#### Launch, health, and runtime provenance

| Field | Default | Contract |
| --- | --- | --- |
| `launch.mode` | `attached` | `attached` or `detached`; both remain agent-supervised. |
| `launch.ready_timeout_seconds` | `900` | Integer ≥ 0. |
| `launch.health` | path `/health`, interval `2.0` | Target-side readiness-probe policy containing the two fields below. |
| `launch.health.path` | `/health` | Readiness path probed on the target. |
| `launch.health.interval_seconds` | `2.0` | Probe interval > 0 seconds. |
| `launch.runs_dir` | `null` | Target-local artifact root; otherwise XDG state default. |
| `launch.require_cached_models` | `false` | Hard-fail before launch when a pinned model is not cached; an unpinned model can only warn. |
| `launch.required_hostname` | `null` | Non-blank exact hostname gate evaluated before process/container action. |
| `vllm.version_profile` | `null` | Vela flag-compatibility profile, not necessarily the runtime package version. |
| `vllm.version` | `null` | Proven vLLM version metadata. |
| `vllm.transformers_version` | `null` | Proven Transformers version metadata. |
| `vllm.torch_version` | `null` | Proven Torch version metadata. |
| `vllm.cuda_version` | `null` | Proven CUDA version metadata. |
| `vllm.require_flags` | `[]` | Flags that must be supported; missing required flags are a hard compatibility failure. |

#### Docker block

| Field | Default | Contract |
| --- | --- | --- |
| `command.docker.image` | required | Image reference; new profiles require a full digest. |
| `command.docker.container_name` | `null` | Agent-owned runtime identity; composer derives a unique name when absent. |
| `command.docker.runtime` | `null` | Optional Docker `--runtime` value. |
| `command.docker.gpus` | `all` | Docker GPU request. |
| `command.docker.ipc_host` | `true` | Emits host IPC when true. |
| `command.docker.shm_size` | `null` | Optional Docker shared-memory size. |
| `command.docker.network` | `host` | Docker network mode. |
| `command.docker.volumes` | `[]` | Target-host-to-container mount specifications. |
| `command.docker.hf_cache` | `null` | Target Hugging Face cache to mount. |
| `command.docker.hf_cache_target` | `/root/.cache/huggingface` | Container cache destination. |
| `command.docker.env` | `{}` | Container environment overlay; values normalize to strings and secrets must be references/placeholders. |
| `command.docker.restart` | `no` | Docker restart policy. |
| `command.docker.auto_remove` | `false` | Emits `--rm`; requires restart to be empty or `no`. |
| `command.docker.stop_grace_seconds` | `90` | Grace period passed to Docker stop. |
| `command.docker.entrypoint` | `null` | Optional image entrypoint override. |
| `command.docker.pull` | `never` | `never`, `missing`, or `always`. |
| `command.docker.evict` | `[]` | Named agent-owned containers eligible for reviewed pre-launch eviction. |
| `command.docker.extra_run_args` | `[]` | Raw Docker arguments emitted before the image. |

## Precedence

Build selection resolves as:

1. `command.runtime: docker` uses `command.docker.image`; there is no managed
   venv.
2. `command.executable`
3. `command.build`
4. target default build
5. bare `vllm` on `PATH`

Model selection resolves as:

1. `model_ref` plus `revision` when present
2. bare `model` plus `revision` when present
3. bare `model`

A pinned registry entry can be recorded with `vela model pin --offline` (no
Hugging Face lookup), in which case it carries `validated: false`; a
`--commit-sha` pin still detects gating so `HF_TOKEN` reaches the launch env. See
`docs/builds-and-models.md` for the pin/revision/verify rules.

Preflight, flag detection, version/profile selection, local-path checks, and a
disk-headroom check — free space on the resolved Hugging Face cache directory
greater than an uncached pinned model's known size plus a 10% margin, before the
launch downloads it — run on the target agent.

For Blackwell Docker deployments, local deployment scripts and proven configs
are the compatibility source of truth. Hugging Face model metadata can help with
model identity and safe generic defaults, but it must not be used to infer the
vLLM image, CUDA arch, CUTLASS/FlashInfer backend, cache layout, or memory
shape for `sm_120` cards.

## Docker Runtime

Docker configs are single-container deployments owned by the target agent. The
agent generates `docker run`, records container name/id/image digest in the
sidecar, streams `docker logs -f` through the scrubbed log sink, waits on
`docker wait`, and verifies identity before every `docker stop` or `docker kill`.

Example shape:

```yaml
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
    pull: never
    hf_cache: /path/to/models/qwen36-dual-fp8-vlm/hf-cache
    volumes:
      - /path/to/models/qwen36-27b-fp8-rp6000/flashinfer-cache:/root/.cache/flashinfer
    env:
      FLASHINFER_CUDA_ARCH_LIST: 12.0f
      PYTORCH_CUDA_ALLOC_CONF: expandable_segments:True
    extra_run_args: [--ulimit, memlock=-1, --ulimit, stack=67108864]
```

The `vllm/vllm-openai` image entrypoint already runs `vllm serve`, so Vela
strips the leading `serve` token from the generated process argv and passes the
model positionally after the image.

`command.docker.pull` is the pull policy: `never` (the default the shipped
recipes use — the image must already be present), `missing` (pull only when
absent), or `always`. When Vela does pull a real ~10GB image, the pull streams
its progress into the run log and is bounded by the target-agent environment
variable `VELA_DOCKER_PULL_TIMEOUT_SECONDS` (default `1800` seconds; `0` or
negative disables the limit). Quick docker commands such as `docker image
inspect` keep a short 10-second timeout. A pull that exceeds the limit is
recorded as a classified `image-pull-timeout` failure rather than crashing the
launch. See `docs/docker-runtime.md` for the full pull semantics.

## Server Exposure

`server.exposure` is the operator acknowledgement for where vLLM will be
reachable:

- `local`: loopback-only binds such as `127.0.0.1`, `localhost`, or `::1`.
- `lan`: a LAN-reachable host or wildcard bind such as `0.0.0.0`.
- `public`: an intentionally public bind.

Non-loopback host values and wildcard binds require `exposure: lan` or
`exposure: public`; `exposure: local` is rejected for those configs. Treat
`lan` and `public` as security-sensitive: use a vLLM API key or another access
control layer before exposing the server beyond the target host.

## Agent Daemon

For local-controller targets, `local_transport: socket` uses a Unix socket
daemon. `vela agent connect` auto-starts that daemon when the configured
socket is missing or stale, then bridges stdio to the socket.

Default paths:

- Socket directory precedence: `$VELA_AGENT_RUNTIME_DIR` (used verbatim) >
  `$XDG_RUNTIME_DIR/vela` > `$XDG_STATE_HOME/vela` > `~/.local/state/vela`. So
  setting `XDG_STATE_HOME` alone now isolates the socket, not just
  `XDG_RUNTIME_DIR`.
- Socket file: `agent.sock` in that directory.
- Legacy fallback: a controller whose resolved socket has no live daemon also
  probes the pre-upgrade path (`$XDG_RUNTIME_DIR/vela` else `~/.local/state/vela`)
  so an already-running daemon is not orphaned mid-upgrade. `vela agent status`
  reports the socket actually in use.
- Identity file: `agent.json` beside the socket (records the daemon version and
  git revision).
- Startup log: `agent-start.err` beside the socket captures a spawned daemon's
  stderr; a start failure names it, and a clean stop removes it.

Operator commands:

```bash
vela agent start
vela agent status
vela agent restart
vela agent stop
```

Each command accepts `--socket PATH` to manage a non-default daemon. To run the
daemon in the foreground, use:

```bash
vela agent run
```

The local socket daemon is long-lived and keeps the working directory and code it
was launched with. On first contact the controller compares its version + git
revision against the daemon's; on a mismatch it prints a single warning like
`local daemon is running vela X (started <date>) — restart with: vela agent restart`.
Because the daemon keeps that first working directory, an unknown-config error
names the directories it searched and that cwd, and an unreachable local daemon
points at `vela agent status` and the `agent-start.err` log (not the SSH setup
path).

For SSH targets, `vela agent status --target <name>` queries the remote agent
and prints the resolved per-host paths and toolchain/auth status instead of the
local daemon socket status.

A user-service template is available at
`packaging/systemd/vela-agent.service`. Install it under
`~/.config/systemd/user/`, then enable it with:

```bash
systemctl --user daemon-reload
systemctl --user enable --now vela-agent.service
```
