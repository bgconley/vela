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
- `ssh_opts_env`: optional environment variable containing SSH options.

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
