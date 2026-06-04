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

Common request methods:

- `handshake`
- `list_configs`
- `preview`
- `preflight`
- `launch`
- `stop`
- `kill`
- `restart`
- `wait`
- `status`
- `health`
- `discover_runs`
- `reattach`
- `subscribe`
- `unsubscribe`
- `gpu`
- `list_builds`, `create_build`, `verify_build`, `remove_build`
- `list_models`, `pin_model`, `download_model`, `verify_model`, `remove_model`

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
