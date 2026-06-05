# Agent RPC

`vllm-loader` uses a controller/agent split. The controller is the TUI or CLI.
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
- `ssh` with `ssh host 'vllm-loader agent connect'`

Both use the same request/response shape and event stream.

## Core Methods

Common request methods and capabilities:

- `handshake`
- `ping`
- `list_configs`
- `update_config_flags`
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

For shared-host hardening, set `VLLM_LOADER_AGENT_TOKEN` on both the target
agent and the controller process. When the agent has this variable, the first
`handshake` must include the matching capability token or the agent returns
`agent-auth-required`. Single-user lab hosts can leave it unset; the default
Unix-socket permissions, same-user peer check, and SSH authentication still
apply.
