# Configuration

The controller chooses a target, then the target agent discovers configs on its
own filesystem. That keeps target-local paths honest: a model path, build id,
working directory, or wrapper script is resolved on the machine that will launch
vLLM.

## Target Registry

Targets live on the controller in `~/.config/vela/targets.yaml`.
`local` is implicit and cannot be removed.

```yaml
targets:
  blackbird:
    transport: ssh
    host: bgconley@10.25.0.51
    ssh_key: /home/bgconley/.ssh/vela_ed25519
    workdir: /home/bgconley/repos/lab-tui
    venv: /home/bgconley/venvs/lab-tui
    agent_command:
      - /home/bgconley/venvs/lab-tui/bin/vela
      - agent
      - connect
    local_transport: socket
```

Fields:

- `transport`: `local` or `ssh`.
- `host`: SSH host for remote targets.
- `ssh_key`: optional target-specific private key passed as `ssh -i`.
- `workdir`: remote directory used before starting `vela agent connect`.
- `venv`: remote venv whose `bin` directory is prepended to `PATH`.
- `agent_command`: optional argv list replacing the default `vela agent connect`.
  This is useful when the target has Vela installed in an absolute venv path.
- `local_transport`: `socket` or `in_process`; use `in_process` only for tests.
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

Config discovery runs agent-side in this order:

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

In the TUI, press `t` for Target Manager. From there, `B` shows the exact
`vela targets bootstrap ... --install` command for the selected target, and
`P` pushes the currently selected local config to the selected remote target
through the agent's `push_config` validation/write path.

Host path overrides:

- `VELA_CONFIGS`: overrides config discovery for that agent process.
- `XDG_CONFIG_HOME`: controls defaults such as `~/.config/vela` and the
  managed `agent-token` file.
- `XDG_DATA_HOME`: controls the managed build/model data roots, defaulting to
  `~/.local/share/vela/...`.
- `XDG_STATE_HOME`: controls durable run/log state, defaulting to
  `~/.local/state/vela/...`.
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
  `hf_cache`, `volumes`, `env`, `pull`, `evict`, and `extra_run_args`.
- `command.cwd`: target-local working directory for relative paths.
- `engine`: modeled vLLM flags. vLLM-owned values default to unset so the
  installed vLLM default wins.
- `server.host`, `server.port`, `server.exposure`: bind/probe settings.
- `server.api_key`: optional vLLM API key, scrubbed from logs.
- `server.probe_host`: optional host used for readiness probes when it differs
  from the bind host.
- `logging.request_logging`: app policy for request logging flags.
- `logging.suppress_access_log_for`: endpoint-specific access log suppression.
- `env`: target-side environment overlay. Keep secrets on the target.
- `extra_args`: passthrough flags appended after modeled flags.
- `launch.mode`: compatibility label; all agent launches are supervised.
- `launch.runs_dir`: optional target-local run artifact directory.
- `launch.ready_timeout_seconds`: launch readiness timeout.
- `vllm.version_profile`: optional flag-compatibility profile hint. This is
  not necessarily the runtime package version inside a pinned Docker image.
- `vllm.version`, `vllm.transformers_version`, `vllm.torch_version`,
  `vllm.cuda_version`: optional provenance for a known-good runtime stack.

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

Preflight, flag detection, version/profile selection, and local-path checks run
on the target agent.

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
    hf_cache: /home/bgconley/models/qwen36-dual-fp8-vlm/hf-cache
    volumes:
      - /home/bgconley/models/qwen36-27b-fp8-rp6000/flashinfer-cache:/root/.cache/flashinfer
    env:
      FLASHINFER_CUDA_ARCH_LIST: 12.0f
      PYTORCH_CUDA_ALLOC_CONF: expandable_segments:True
    extra_run_args: [--ulimit, memlock=-1, --ulimit, stack=67108864]
```

The `vllm/vllm-openai` image entrypoint already runs `vllm serve`, so Vela
strips the leading `serve` token from the generated process argv and passes the
model positionally after the image.

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

- Socket: `$XDG_RUNTIME_DIR/vela/agent.sock` when `XDG_RUNTIME_DIR` is
  set.
- Fallback socket: `~/.local/state/vela/agent.sock`.
- Identity file: `agent.json` beside the socket.

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
