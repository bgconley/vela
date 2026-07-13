# Agent RPC

`vela` uses a controller/agent split. The controller is the TUI or CLI.
The agent runs on the target host and owns every host-local action.

## Authority Boundary

The agent owns:

- launch, stop, kill, restart, wait, and reattach
- sidecar identity verification before every destructive signal
- preflight checks for target-local paths, ports, and GPUs
- phase FSM and error classification
- health probes and GPU sampling
- durable log writes and scrubbing
- build installs and model downloads

The controller passes only run_id or job_id for lifecycle operations. It does
not hold PIDs, process handles, sidecar paths, manifest paths, or raw secrets.

## Transports

The `TargetClient` API is uniform:

- `local` with a Unix socket daemon or in-process test agent
- `ssh` with `ssh host 'vela agent connect'`

Both use the same request/response shape and event stream.

## Core Methods

Common request methods and capabilities:

- `handshake`
- `ping`
- `list_configs`
- `update_config_flags`
- `compose_config`
- `suggest_deployment_defaults`
- `allocate_port`
- `list_presets`
- `list_deployment_recipes`
- `validate_config`
- `save_config`
- `edit_config`
- `clone_config`
- `delete_config`
- `migrate_wrapper_config`
- `write_agent_token`
- `list_config_files`
- `pull_config`
- `push_config`
- `lint_config`
- `export_config`
- `preview`
- `preflight`
- `prepare_launch`
- `launch`
- `wait`
- `stop`
- `kill`
- `restart`
- `status`
- `gpu`
- `sample_gpus`
- `health`
- `probe_until_ready`
- `tail_detached`
- `read_run_artifact`
- `discover_runs`
- `discover_runs_no_paths`
- `discover_detached`
- `reattach`
- `reattach_detached`
- `list_builds`
- `adopt_build`
- `inspect_build`
- `select_build`
- `verify_build`
- `repair_build`
- `check_build_prerequisites`
- `remove_build`
- `run_build`
- `list_models`
- `pin_model`
- `refresh_models`
- `inspect_model`
- `verify_model`
- `remove_model`
- `create_build`
- `download_model`
- `cancel_job`
- `subscribe`
- `unsubscribe`

Build and model jobs emit `job_progress` and `job_done` events and can be
cancelled with `cancel_job`.

## Run Lifecycle From The CLI

The controller exposes detached-run management as thin wrappers over these
methods, so operators never touch target paths directly:

- `vela runs list` wraps `discover_runs` (enriched per run via `status`) and
  shows run id, config, phase, ready url, controller-safe UTC start time, and
  served model — never the sidecar path or PID.
- `vela stop RUN_ID|CONFIG` resolves the unique live run and wraps `stop` (or
  `kill` with `--kill`).
- `vela logs RUN_ID` replays the agent-scrubbed durable log via
  `read_run_artifact`; `--follow` streams it from the start via `tail_detached`.

The `handshake` result reports `agent_version` and `agent_revision` (a
git-describe frozen at daemon start) alongside `daemon_start_ts`, so a controller
can detect a stale local socket daemon on first contact. As an intentional
exception to the authority boundary, an `unknown-config` error payload carries the
`searched_dirs` it looked in plus the agent `cwd` — diagnostic surface for the
frozen daemon working directory, not a path the controller acts on.

## Event Stream

`subscribe` streams run events such as:

- `phase`
- `log`
- `progress`
- `ready`
- `health`
- `gpu`
- `exited`
- `error`

Events carry sequence and log cursor metadata where applicable. Reconnect can
resume from a warm sequence buffer or from `{log_inode, byte_offset}` against
the durable target log.

## Safety And Security

The agent verifies sidecar identity immediately before every signal escalation.
If a PID was recycled or identity data no longer matches, the signal is refused.

Scrubbing is unconditional and agent-side. There is no raw-log RPC, and the
controller does not need target secrets to render logs.

Sidecars carry typed build/model identity fields such as `build_id`,
`model_ref`, `model_entry_id`, and `model_revision` so agent-side live-run
guards do not need to infer managed resources from generic config snapshots or
command argv. Path-bearing sidecar and manifest details remain agent-local.

For shared-host hardening, install a capability token on both the controller and
target with `vela agent gen-token --install --target <name>`, or set
`VELA_AGENT_TOKEN` on both processes manually. Configured tokens must be a
single non-whitespace value with at least 128 bits of entropy. When the agent
has a token, the first successful `handshake` on each socket/SSH stream must
include the matching capability token. Other RPC methods on that stream return
`agent-auth-required` until the handshake succeeds. Single-user lab hosts can
leave it unset; the default Unix-socket permissions, same-user peer check, and
SSH authentication still apply, and the peer check fails closed when peer
credentials cannot be read and no token is configured. Hosts where multiple
engineers share one agent account should set `VELA_AGENT_REQUIRE_TOKEN=1`, which
makes the agent refuse to authenticate unless a token is installed (fail closed)
instead of accepting any same-uid caller.

`vela doctor --target <name>` renders target auth as `none`,
`required+provided`, `required+missing`, `mismatch`, or `malformed-token`.
Missing, mismatched, and malformed states point at
`vela agent gen-token --install --target <name>` so the controller and target
converge on the same high-entropy token.
