# Configuration

The controller chooses a target, then the target agent discovers configs on its
own filesystem. That keeps target-local paths honest: a model path, build id,
working directory, or wrapper script is resolved on the machine that will launch
vLLM.

## Target Registry

Targets live on the controller in `~/.config/vllm-loader/targets.yaml`.
`local` is implicit and cannot be removed.

```yaml
targets:
  blackbird:
    transport: ssh
    host: bgconley@10.25.0.51
    workdir: /home/bgconley/repos/lab-tui
    venv: /home/bgconley/venvs/lab-tui
    local_transport: socket
```

Fields:

- `transport`: `local` or `ssh`.
- `host`: SSH host for remote targets.
- `workdir`: remote directory used before starting `vllm-loader agent connect`.
- `venv`: remote venv whose `bin` directory is prepended to `PATH`.
- `local_transport`: `socket` for the daemon path or `inprocess` for tests.
- `ssh_opts_env`: optional environment variable containing SSH options. It may
  add option flags such as `-A`, `-i`, `-J`, `-p`, or `-o Key=Value`, but
  positional SSH arguments, port forwarding, command-suppression, and
  command-bearing `-o` options such as `ProxyCommand`, `RemoteCommand`, or
  `LocalCommand` are rejected. External config/control socket options (`-F`,
  `-S`, `ControlPath`) are also rejected so the configured target host and
  `agent connect` command cannot be replaced by the environment.

## Config Discovery

Config discovery runs agent-side in this order:

1. `--configs-dir`
2. `VLLM_LOADER_CONFIGS`
3. `./configs`
4. `~/.config/vllm-loader/configs`

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
- `command.executable`: explicit vLLM executable or wrapper script.
- `command.build`: managed build id/label; overrides the target default build.
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
- `vllm.version_profile`: optional profile hint.

## Precedence

Build selection resolves as:

1. `command.executable`
2. `command.build`
3. target default build
4. bare `vllm` on `PATH`

Model selection resolves as:

1. `model_ref` plus `revision` when present
2. bare `model` plus `revision` when present
3. bare `model`

Preflight, flag detection, version/profile selection, and local-path checks run
on the target agent.

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
daemon. `vllm-loader agent connect` auto-starts that daemon when the configured
socket is missing or stale, then bridges stdio to the socket.

Default paths:

- Socket: `$XDG_RUNTIME_DIR/vllm-loader/agent.sock` when `XDG_RUNTIME_DIR` is
  set.
- Fallback socket: `~/.local/state/vllm-loader/agent.sock`.
- Identity file: `agent.json` beside the socket.

Operator commands:

```bash
vllm-loader agent start
vllm-loader agent status
vllm-loader agent restart
vllm-loader agent stop
```

Each command accepts `--socket PATH` to manage a non-default daemon. To run the
daemon in the foreground, use:

```bash
vllm-loader agent run
```

A user-service template is available at
`packaging/systemd/vllm-loader-agent.service`. Install it under
`~/.config/systemd/user/`, then enable it with:

```bash
systemctl --user daemon-reload
systemctl --user enable --now vllm-loader-agent.service
```
