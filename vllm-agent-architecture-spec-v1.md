# Vela — Agent/Controller Architecture (Remote Execution) — Specification & Implementation Plan (v1)

**Feature:** "agent everywhere, controller anywhere" — a per-host agent that owns all lifecycle, an RPC protocol, and pluggable transports (local + SSH) · **Status:** spec-ready · **Audience:** the engineer(s) refactoring `vela`.

> **Relationship to the existing specs.** This document is a **structural refactor** of the architecture in `vllm-tui-loader-spec-v2-CANONICAL.md`. It does **not** change *what* the loader does; it changes *where* the work runs. It supersedes the canonical assumption that "the TUI runs on the machine that launches vLLM" (canonical §2.3/§6) and replaces it with a controller↔agent split. It **composes with** `vllm-build-management-spec-v1.md` and `vllm-model-management-spec-v1.md`: builds and models are **per-target, agent-owned** resources surfaced over the same RPC. Where this document and the canonical spec disagree on *topology*, this document wins; on *behavior* (phases, scrubbing, identity rigor, UI conventions), the canonical spec and the sibling specs still govern — this document lifts that behavior behind an agent boundary unchanged.
>
> **This refactor is the foundation the other two specs assume.** Builds live under the host's data dir; models live in the host's HF cache; sidecars/runs live under the host's state dir. All three are inherently *per-machine*. Once the controller can run on a different machine than the GPU, every one of those resources is **the agent's**, reached by RPC. Sequence this refactor's MVP before (or alongside) the build/model TUI work so they share one boundary.

-----

## 0. Document status & the problem

### 0.1 The shape we want

> **Controller anywhere, agent everywhere.** Run the TUI on a workstation (P620‑01); use a GPU box (Blackbird) — or the local machine itself — as an execution *target*. Every target runs a `vela agent` that owns launch, stop/kill/restart, sidecar identity, durable logs, health, and GPU sampling. The controller asks; the agent acts. **Local is just another target.** The protocol is identical; only the transport changes.

### 0.2 Why the current app cannot do this (grounded)

Today the loader assumes the TUI and the vLLM process share one machine. Concretely (verified against the current code, ~199 tests):

- **Process launch is local.** `process_manager.start_attached/start_detached` call `subprocess.Popen(...)` and `os.openpty()` on the controller's host (`process_manager.py` `_base_argv`/PTY fork/supervisor spawn). The Blackbird config drives `command.executable → scripts/blackbird_qwen36_vllm_foreground.sh`, so launching it from P620‑01 runs the script **on P620‑01**, not Blackbird — the exact failure the owner observed.
- **Signals are local.** Stop/kill go through `os.killpg(os.getpgid(pid), …)` (`process_manager._kill_group`, `sidecar._signal_process_group`). A pgid from Blackbird is meaningless — or dangerously *valid for an unrelated process* — on P620‑01.
- **Identity is local.** `verify_sidecar_identity` reads live `psutil` `create_time`, `/proc/<pid>/stat` `procfs_starttime`, and pgid (`sidecar.py`). From a different host these read the wrong process table; the anti‑PID‑reuse guard silently breaks.
- **Discovery, logs, sidecars, manifests are local files.** `discover_active_sidecars` globs the controller's runs dir; durable logs and sidecars are `0600` files on the controller's disk.
- **GPU and health are local.** `monitoring/gpu.sample_gpus` reads NVML/`nvidia-smi` on the controller; `monitoring/health.probe_host_for` maps non‑loopback → `127.0.0.1` and probes from the controller's host.
- **Preflight is local.** `preflight.missing_local_model_path` stats the controller's filesystem; `occupied_port_detail` binds a socket on the controller's network stack; world‑size checks read the controller's `CUDA_VISIBLE_DEVICES`.

The quick hack — `command.executable: ssh` running a wrapper remotely — leaves *every* item above wrong: stop/kill, health, GPU, sidecar identity, and reattach would all target the controller, not the GPU box. **The correct fix is a remote-execution backend plus a remote agent that owns lifecycle authority.** The existing detached supervisor + sidecar work is the foundation; this refactor lifts it behind an agent boundary so "local" and "ssh:blackbird" look identical to the TUI.

### 0.3 What this adds

A new `agent` subsystem and a `targets` layer: (1) a per-host **agent daemon** that owns all host-local authority; (2) an **NDJSON-RPC protocol** (request/response + server-pushed events) over **stdio**; (3) two **transports** — a local Unix-socket client and an SSH stdio↔socket bridge — behind a uniform `TargetClient`; (4) a controller-local **targets registry**; (5) controller UX for target selection, connection state, and the composed `target × build × model × config` scope.

-----

## 1. Vision & elemental concepts

### 1.1 The authority principle

**All lifecycle authority lives inside the target agent.** The controller is a view/orchestrator that *asks*; it never holds a PID, writes a sidecar, delivers a signal, scrubs a secret, or probes a port directly. The agent owns: launch, stop/kill/restart, **sidecar identity verification (verify-before-every-destructive-signal)**, durable log persistence + scrubbing, health probes, GPU sampling, preflight, the phase FSM, and reattach/discovery — plus (composed in) builds and models for its host.

### 1.2 Uniform shape: local is just another target

One protocol, one `TargetClient` API. A `target` declares a **transport**: `local` (in-process agent — zero serialization) or `ssh` (`ssh host vela agent`, NDJSON over stdio). The TUI's call sites are transport-agnostic. This is non-negotiable: even "control the local GPUs" goes through the same agent API, so there is no privileged local path that drifts from the remote one.

### 1.3 The launch/observe split (this refactor's key simplification)

Today there are two launch paths: **attached** (PTY read directly by the TUI; the run is a child of the TUI) and **detached** (supervisor owns pipes; TUI tails a file). A PTY master cannot cross an RPC boundary, and an attached run would die when an SSH connection drops. Therefore:

> **The agent always executes runs in a durable, supervised manner.** "Attached vs detached" collapses into **"is the controller currently subscribed to this run's event stream?"** The agent supervises every run (survivable, sidecar-backed); the controller *observes* by holding a subscription, and *detaches* by dropping it. The run is unaffected either way.

To preserve vLLM's live `\r` progress bars (canonical §2.4) the supervisor must **own a PTY** rather than plain pipes — the "PTY-owning supervisor" the canonical §16 deferred. **This refactor promotes it to a near-term requirement**, because it is now the *only* way to get live bars on a remote target. (The pure-local in-process transport may keep a true attached PTY as an optimization, but the canonical, uniform path is supervised.)

### 1.4 Survivability layers (precisely)

1. **A run survives its agent.** Runs are supervisor-detached (`setsid`, double-fork lineage); the supervisor + vLLM are *not* children of the agent daemon. Restarting or killing the daemon (or dropping SSH) does not touch a run.
2. **The agent daemon survives the controller.** The daemon is a standing per-user process (§5.1), not spawned by — and not tied to — any connection. A controller disconnect, crash, restart, or even a reboot of the *controller* machine leaves the daemon and its runs untouched, with **warm in-memory state** (run registry + bounded per-run event buffers, §6.6) retained.
3. **The controller re-attaches to warm state.** On reconnect the controller resumes each run **gap-free from the daemon's event buffer by sequence number** (§7.6); on buffer overflow or a daemon restart it falls back to **resume-by-offset** from the durable `0600` log (the complete record). Sidecars on the host remain the cross-restart source of truth — canonical §7.10, now daemon-side.

### 1.5 Composition: a launch is `target × build × model@rev × config`

A launch is scoped, outermost to innermost: **target** (which host) → **build** (which vLLM venv, sibling spec) → **model@revision** (which weights, sibling spec) → **config** (flags). Builds and models are the *agent's* resources; the controller selects them over RPC. The agent resolves the build handoff (build spec §7.5) and model handoff (model spec §9) locally and applies both to the supervisor payload.

-----

## 2. Scope & non-goals

**In scope (v1):** a controller-local `targets` registry (local + ssh); a uniform `TargetClient` over `LocalTransport` (in-process) and `SshStdioTransport`; an `Agent` that wraps the existing engine and owns authority; the NDJSON-RPC protocol (request/response + streamed events, idempotent launch, handshake/version/capability negotiation, reconnect + resume-by-offset); agent-side preflight/scrubbing/durable-logs/sidecars/FSM/health/GPU; per-target configs/builds/models over RPC with job streaming; the `vela agent` CLI and `--target` selection; controller UX (target segment, `TargetManagerScreen`, disconnected state).

**Out of scope (v1):** HTTP/WebSocket/gRPC transports (deferred — §17); a daemon **token/capability auth** for shared multi-tenant hosts (v1 gates on Unix-socket filesystem permissions + `SO_PEERCRED` + SSH; §13); simultaneous multi-host "runs overview" (deferred — one active target at a time); a control plane that fans launches across hosts; agent auto-install/bootstrap beyond a surfaced command; Windows targets (Linux-primary). The local in-process path keeps a true attached PTY as an internal optimization, but no new local-only features.

-----

## 3. Functional & non-functional requirements

**Targets & transport**
- **FR-A1** A controller-local targets registry declares targets with a transport (`local`|`ssh`), host, optional workdir/venv, and SSH options; `local` is implicit, always present, non-removable.
- **FR-A2** A uniform `TargetClient` exposes `connect/call/subscribe/disconnect` identically over `LocalTransport` and `SshStdioTransport`; the TUI is transport-agnostic.

**Agent authority**
- **FR-A3** The agent owns launch/stop/kill/restart on its host; the controller passes only a `run_id` and never a PID/pgid/sidecar path for destructive ops.
- **FR-A4** **Verify-before-every-destructive-signal runs agent-side** (the canonical anti-PID-reuse rule); the controller has no path to bypass it; identity mismatch returns a named error and aborts.
- **FR-A5** The agent runs **preflight** (model-path, port, world-size-vs-actual-GPUs), **scrubbing**, **durable log persistence** (`0600`), **sidecar/manifest** writes, the **phase FSM**, **health probing**, and **GPU sampling** — all on the target host.

**Protocol**
- **FR-A6** NDJSON-RPC over stdio: `{id,method,params}` requests, `{id,result|error}` responses, and `{event,run_id|sub_id,…}` server-pushed events multiplexed on one stream.
- **FR-A7** `launch` (and `create_build`/`download_model`) are **idempotent** on a controller-minted id; a re-sent request returns the existing run/job rather than double-starting.
- **FR-A8** Handshake negotiates protocol version + capabilities + host info; incompatible versions and missing capabilities fail with named errors.
- **FR-A9** Streamed events reconstruct the full UI: `phase`, `log`(committed/transient), `progress`, `ready`(with a controller-reachable URL), `health`, `gpu`, `exited`, `error`, and `job_progress`/`job_done` for builds/models. Jobs are cancellable.

**Connection lifecycle**
- **FR-A10** Reattach/discover live runs via sidecars (`discover_runs`); reconnect resumes tailing **by `{log_inode, byte_offset}`**, not full replay; rotation is detected via the manifest inode.
- **FR-A11** Named failure modes surface in the UI: `AGENT_UNREACHABLE`, `AGENT_NOT_INSTALLED`, `AGENT_VERSION_MISMATCH`, plus reconnect.

**Composition & UX & CLI**
- **FR-A12** Per-target `list_configs`/`list_builds`/`list_models` and their mutators are agent RPC methods; switching target re-scopes the config/build/model/run views.
- **FR-A13** Controller UX: a header **target segment** with a connection dot, a `TargetManagerScreen`, a disconnected dashboard state, and **target-named confirm dialogs** for destructive actions.
- **FR-A14** `vela agent` CLI (the agent entrypoint) and `--target` selection on controller commands.
- **FR-A15** The agent runs as a **per-user daemon** on each target (Unix-socket listener; systemd-user / auto-spawn / explicit-CLI lifecycle), surviving controller disconnects and serving multiple controllers; runs survive a daemon restart via sidecar re-discovery.

**Non-functional**
- **NFR-A1 Uniformity** — local == remote at the `TargetClient` API; no privileged local path.
- **NFR-A2 No secrets off-host** — scrubbing is agent-side and unconditional; there is no "raw logs" RPC; the secrets list never leaves the agent.
- **NFR-A3 No new network port** — the daemon listens on a user-owned Unix domain socket (`0600`); remote reach is an SSH stdio↔socket bridge reusing SSH auth; nothing binds a network port.
- **NFR-A4 Survivability** — the layers of §1.4 hold; the daemon decouples run/agent state from the controller connection.
- **NFR-A5 Identity rigor preserved** — the canonical five-check identity suite runs agent-side, unchanged.
- **NFR-A6 Responsiveness under backpressure** — transient/progress events are lossy-coalesced under stream backpressure; committed-log/phase/lifecycle/error/response frames are lossless; the durable agent log is the complete record.
- **NFR-A7 Back-compat** — a user with only a `local` target sees no behavior change; "local" is the first agent implementation.
- **NFR-A8 Testability** — the agent, protocol, and transports are testable without SSH or a GPU (a fake transport + an in-process agent + recorded NDJSON fixtures).
- **NFR-A9 Reconnect resilience** — idempotent launch + resume-by-offset make a dropped SSH connection non-destructive and non-duplicating.

-----

## 4. Architecture

### 4.1 Components & the authority boundary

```
┌──────────────────── CONTROLLER (anywhere) ─────────────────────┐
│ TUI (thin view) • targets registry • TargetClient(s)            │
│   header: ⊕target ▣build M model ●STATUS url                    │
└───────────────▲───────────────────────────────▲────────────────┘
                │ NDJSON-RPC (requests / events) │
   ┌────────────┴─── transport (uniform API) ────┴───────────┐
   │ LocalTransport (in-process)   |   SshStdioTransport (ssh) │
   └───────────────────────────────┬───────────────────────────┘
                                    │ stdio (NDJSON)         ══════════ AUTHORITY BOUNDARY ══════════
   ┌───────────────────── AGENT (on the target host) ────────────────────────────────────────────┐
   │ run registry (discover-rebuilt) • preflight • phase FSM • scrubbing LogSink •                 │
   │ health probe • GPU sampler • verify-before-signal • builds/models (per-host)                  │
   │   wraps the EXISTING: process_manager / supervisor(+PTY) / sidecar / log_sink / monitoring    │
   └───────────────┬──────────────────────────────────────────────────────────────┬──────────────┘
                   │ supervises (setsid, durable)                                   │ probes/samples
                   ▼ vLLM child (survives agent)   sidecars/manifests/logs 0600 ◄───┘ 127.0.0.1 + NVML
```

Everything right of the boundary is host-local and never crosses the wire except as **scrubbed events**. The controller holds only the targets registry (its sole persistent state) and an in-memory, per-session cache of agent-reported views.

### 4.2 What moves agent-side vs stays controller-side (the cut-point classification)

Derived from a full audit of today's `tui/app.py`/`cli.py` call sites into the engine/monitoring.

| Capability (today's call site) | Side | Why |
|---|---|---|
| `start_attached`/`start_detached` (`process_manager`) | **Agent** | Spawns a process on the host; PTY/pipes are local kernel objects |
| stop/kill/restart `os.killpg` (`process_manager`,`sidecar`) | **Agent** | A pgid is meaningless/dangerous off-host |
| `verify_sidecar_identity` / `*_from_system` (`sidecar`) | **Agent** | Reads live `psutil`/`/proc` identity |
| `discover_active_sidecars`, `load_sidecar/manifest` (`sidecar`) | **Agent** | Globs/reads the host's runs dir |
| `LogSink` (scrub + `0600` durable file) (`log_sink`) | **Agent** | Secrets must not leave the host; durable log stays on host |
| `sample_gpus` (NVML/`nvidia-smi`) (`monitoring/gpu`) | **Agent** | Samples the host's GPUs |
| `probe_loop`/`probe_host_for` (`monitoring/health`) | **Agent** | Loopback probe must run where vLLM binds |
| `PhaseFSM.feed_line/health_*/process_exited` (`phases`) | **Agent** | Inputs (logs, health, exit) are all agent-local; profile is build-specific (agent-local) |
| preflight: `missing_local_model_path`/`occupied_port_detail`/`parallel_world_size_mismatch` (`preflight`) | **Agent** | Test the target's filesystem/network/GPUs |
| `select_profile_for_config`/`detect_vllm_version_for_config`/`build_command` (`profile`,`command_builder`) | **Agent** | Depend on the target's installed/managed vLLM build; `build_command` is pure but is fed agent-resolved build/model handoffs |
| config discovery `load_registry` (`config/loader`) | **Agent** (primary) | Configs reference target-local paths (model, build, cwd); see §10.3 |
| targets registry | **Controller** | The only genuinely controller-local data |
| Widget rendering, phase-timeline elapsed, filter/search/pause | **Controller** | Pure view; driven by streamed events |
| Config *schema* validity (cheap) | **Controller** (optional pre-check) | Host-independent; host-dependent preflight is still agent-side |

-----

## 5. The agent

### 5.1 Process model & lifetime — a per-user daemon

The agent is a **long-lived per-user daemon** on each target host, **independent of any controller connection** (the change from a session-scoped process: robustness over a flaky link, warm state, and decoupling from the orchestrator's connection). It holds no GPU memory and is near-idle when no run is active.

- **Listener.** The daemon listens on a **Unix domain socket** at `$XDG_RUNTIME_DIR/vela/agent.sock` (fallback `~/.local/state/vela/agent.sock`), in a `0700` dir with a `0600` socket owned by the user. **No network port is opened** (NFR-A3 preserved). It accepts concurrent connections (multi-controller; §5.5).
- **Remote reach.** The controller runs a tiny **stateless SSH stdio↔socket bridge** — `ssh [opts] host vela agent connect` — forwarding NDJSON between the SSH connection's stdio and the local socket. SSH provides transport + auth; the daemon is the authority and persists across bridges. The bridge is disposable; killing it (or an SSH drop) never touches the daemon or its runs.
- **Local reach.** The controller connects **directly to the local socket** (uniform with remote, minus SSH). An in-process `Agent` remains a zero-dependency dev/test mode, but the daemon-over-socket path is canonical so local and remote share warm-state/multi-controller semantics.
- **Identity file.** The daemon writes `agent.json` beside the socket — `{pid, create_time, procfs_starttime, start_ts, version, protocol_versions, socket_path}` — so a connector detects a **stale socket** (dead/mismatched daemon) using the same identity discipline as sidecars and refuses an impostor.

**Lifecycle (three complementary ways):**
1. **systemd user service** (recommended for production): `systemctl --user enable --now vela-agent`; with lingering it survives logout and starts on boot — clean supervision, restart-on-crash, journald logs.
2. **Auto-spawn on first connect** (zero-config): the bridge / local connector, finding no live daemon (missing/stale `agent.json`, dead identity, refused connect), **double-forks a detached daemon** (`setsid`), waits for the socket, then connects. The spawned daemon **persists after the connector leaves** — that is the point.
3. **Explicit CLI:** `vela agent start|stop|status|restart`.

**Idle policy.** Default **persist** (robustness; an idle daemon is a tiny process). Optional `--idle-timeout <minutes>` self-exits when there are no connections **and** no active runs; default off (predictable).

**Daemon-restart resilience.** If the daemon crashes or is restarted, **runs keep running** (supervisor-detached, §1.4 layer 1); on startup the daemon rebuilds its run registry from `discover_active_sidecars`. The warm event buffers (§6.6) are lost on restart, but the durable `0600` logs remain complete and the controller falls back to resume-by-offset.

### 5.5 Multiple controllers

Because the daemon outlives connections and the socket accepts concurrent peers, **multiple controllers may attach simultaneously** (e.g. a live TUI plus a `vela status` CLI, or two operators). The daemon fans events out per `sub_id`. Destructive ops stay safe: verify-before-signal is per-call, and a second `stop` on an already-stopped run simply observes it gone. Each connection is authenticated by socket filesystem permission (same user) plus an `SO_PEERCRED` UID check.

### 5.2 Always-supervised execution (§1.3 applied)

`launch` → preflight → resolve build+model handoffs → `start_detached`-style supervised spawn with a **PTY-owning supervisor** (live bars + survivability) → return when the sidecar appears (`_wait_for_sidecar`). The agent then streams `phase`/`log`/`progress`/`ready`/`exited` events to any subscriber. The supervisor's drain thread feeds the **scrubbing** `LogSink`, which writes the `0600` durable log and emits scrubbed records the agent forwards as events. The vLLM child is `setsid`-detached and outlives the agent.

### 5.3 Lifecycle authority (the crown jewel, agent-side)

For stop/kill/restart the controller sends only `{run_id, timeouts}`. The agent:
1. loads the sidecar from disk (current, not cached),
2. reads live identity for the child (and supervisor, if detached) via `psutil`/`/proc`,
3. runs the five-check `verify_sidecar_identity` (pid+create_time, procfs_starttime, pgid, command_hash, supervisor identity),
4. **only on full pass** delivers `os.killpg(pgid, sig)` with SIGINT→SIGTERM→SIGKILL escalation, re-verifying before *each* escalation step (as today).

Mismatch → `-32002 IDENTITY_VERIFICATION_FAILED` naming the failing check; the controller renders "tracked process is gone; refused to signal a possibly-recycled PID." **The sidecar and its identity data never cross the wire** — the controller knows only `run_id`.

### 5.4 Reattach & discovery

`discover_runs` → `discover_active_sidecars` on the host → live run summaries. `reattach(run_id)` → verify identity, load manifest, expose the active-log pointer for tailing. After a full controller restart this reconstructs the run list with no user action. On a *reconnect* to a still-warm daemon, the controller resumes **gap-free by event sequence** from the daemon's buffer; on overflow or a daemon restart it resumes by log offset (§7.6).

-----

## 6. The RPC protocol (NDJSON over stdio)

### 6.1 Framing

One JSON object per `\n`-delimited line; each side buffers until `\n`. Max line ~**2 MiB** (a committed log line is already bounded to 1 MiB by the LogSink truncation rule; this caps a pathological frame, not normal traffic). Three shapes share the stream:

```
Request  (C→A): {"id":"<ulid>","method":"<name>","params":{…}}
Response (A→C): {"id":"<ulid>","result":{…}}  |  {"id":"<ulid>","error":{"code":<int>,"message":"…","data":{…}}}
Event    (A→C): {"event":"<name>","run_id|sub_id|job_id":"…","seq":<per-run monotonic>,"ts":"<agent wall>","mono":<agent monotonic>, …}
```

The controller demuxes: a line with `id` resolves a pending request future; a line with `event` dispatches to subscription handlers. Requests flow on stdin, responses+events on stdout (separate pipes), so a `stop` request reaches the agent promptly regardless of event volume. **Response frames are prioritized ahead of buffered events** on the outbound writer. Every run event carries a per-run monotonic `seq`, so a reconnecting controller can request replay since its last `seq` (§6.6/§7.6).

### 6.2 Idempotency (NFR-A9)

The controller mints the `run_id`/`job_id` (a ULID) and passes it into `launch`/`create_build`/`download_model`. The agent treats it as an idempotency key: a re-sent request for an id that is already live returns the existing run/job (no double-launch). This makes a dropped response over flaky SSH safe to retry and prevents orphaned runs.

### 6.3 Method set

| Method | Params | Result | Notes |
|---|---|---|---|
| `handshake` | `{protocol_version, controller_version, capabilities[]}` | `{agent_version, protocol_version, capabilities[], host_info, daemon_pid, daemon_start_ts}` | First call; version/capability negotiation (§7.4); `daemon_start_ts` lets the controller detect a daemon restart (→ resume by offset) |
| `ping` | `{}` | `{ts}` | App-layer keepalive (§7.5) |
| `list_configs` | `{include_invalid?}` | `{configs:[ConfigSummary]}` | Agent-side discovery (§10.3) |
| `preview` | `{config_name, build_id?}` | `{argv[], masked_argv[], env, warnings[]}` | Reuses `build_command().preview`; secrets masked |
| `preflight` | `{config_name, build_id?}` | `{ok, failures:[…]}` | Agent-side host checks |
| `launch` | `{run_id, config_name, build_id?, model_ref?, revision?, log_rotate_bytes?}` | `{run_id, sidecar_path}` | Idempotent; supervised; returns when sidecar appears |
| `stop` | `{run_id, interrupt_timeout?, terminate_timeout?}` | `{signaled}` | Verify-before-signal escalation |
| `kill` | `{run_id}` | `{signaled}` | Immediate SIGKILL, still verified |
| `restart` | `{run_id, new_run_id, config_name?, build_id?}` | `{new_run_id}` | stop+launch, idempotent on `new_run_id` |
| `status` | `{run_id}` | `RunStatus` | Single-shot snapshot |
| `subscribe` | `{sub_id, run_ids?|all?, resume_from?:{seq}|{log_inode,byte_offset}|"live"|"start"}` | `{sub_id}` | Stream events; warm replay by `seq`, else by offset (§6.6/§7.6) |
| `unsubscribe` | `{sub_id}` | `{}` | |
| `gpu` | `{sub_id, interval_s?}` | `{sub_id}` | Start GPU sampling stream |
| `health` | `{run_id}` | `{ready, models[], detail, reachable_url}` | Single-shot probe |
| `discover_runs` | `{}` | `{runs:[RunSummary]}` | Rebuild registry from sidecars |
| `reattach` | `{run_id}` | `RunStatus` | Verify + expose active-log pointer |
| `cancel_job` | `{job_id}` | `{}` | Cancel a build install / model download |
| `list_builds`/`create_build`/`select_build`/`verify_build`/`remove_build` | (build spec) | (build spec) | Per-host; `create_build` streams `job_progress` |
| `list_models`/`pin_model`/`download_model`/`verify_model`/`remove_model` | (model spec) | (model spec) | Per-host; `download_model` streams `job_progress` |

Error codes: `-32700/-32600/-32601/-32602` (parse/invalid/no-method/bad-params), `-32000` agent-internal, `-32001` run-not-found, **`-32002` identity-verification-failed**, `-32003` not-stoppable, `-32004` config-not-found, **`-32005` preflight-failed** (structured `data`), **`-32006` version-mismatch** (`data.required/actual`), `-32007` build-not-found, `-32008` model-not-found, `-32009` resource-in-use, `-32010` job-already-running.

### 6.4 Event set (all carry agent `ts` + `mono`; §7.7 clock discipline)

| Event | Correlate | Fields | Maps to message |
|---|---|---|---|
| `phase` | `run_id` | `phase, prev_phase, error_kind?, error_excerpt?` | `PhaseChanged` (+ error fields) |
| `log` | `run_id` | `kind:"committed", text, level` | `LogLineCommitted` (scrubbed) |
| `progress` | `run_id` | `text` | `LogLineTransient`/`ProgressUpdated` (scrubbed, lossy) |
| `ready` | `run_id` | `models[], bind_host, port, reachable_url` | `ServerReady` (+ reachable URL, §9.4) |
| `health` | `run_id` | `ready, detail, models?, error_kind?` | `HealthChanged` |
| `gpu` | `sub_id` | `devices:[GpuSample], note?, unavailable?` | `GpuStatsUpdated`/`Unavailable` |
| `exited` | `run_id` | `returncode?, phase` | `ProcessExited` |
| `error` | `run_id` | `kind, detail` | `EngineError` |
| `job_progress` | `job_id` | `kind, text, level?, phase?, progress_pct?` | build/model install stream |
| `job_done` | `job_id` | `ok, exit_code?, error_kind?, detail?` | terminal for a job |
| `agent_error` | — | `detail, fatal` | agent-level fault |

### 6.5 Worked sequence (launch → stream → ready)

```json
{"id":"r1","method":"launch","params":{"run_id":"01JRUN…","config_name":"qwen36-…-blackbird","build_id":"01JB…"}}
{"id":"r1","result":{"run_id":"01JRUN…","sidecar_path":"/home/bg/.local/state/vela/runs/01JRUN….json"}}
{"id":"r2","method":"subscribe","params":{"sub_id":"01JSUB…","run_ids":["01JRUN…"],"resume_from":"start"}}
{"id":"r2","result":{"sub_id":"01JSUB…"}}
{"event":"phase","run_id":"01JRUN…","phase":"STARTING","prev_phase":"IDLE","ts":"2026-06-02T18:00:01Z","mono":12042.11}
{"event":"log","run_id":"01JRUN…","kind":"committed","text":"INFO … Initializing a V1 LLM engine","level":"INFO","ts":"…","mono":…}
{"event":"progress","run_id":"01JRUN…","text":"Loading safetensors checkpoint shards: 50% 1/2","ts":"…","mono":…}
{"event":"phase","run_id":"01JRUN…","phase":"READY","prev_phase":"SERVER_STARTING","ts":"…","mono":…}
{"event":"ready","run_id":"01JRUN…","models":["qwen3.6-27b-fp8"],"bind_host":"0.0.0.0","port":18003,"reachable_url":"http://10.25.0.51:18003"}
```

Stop with agent-side re-verify:
```json
{"id":"r9","method":"stop","params":{"run_id":"01JRUN…","interrupt_timeout":2,"terminate_timeout":2}}
{"id":"r9","error":{"code":-32002,"message":"tracked process is gone; refusing to signal a possibly-recycled PID","data":{"check":"procfs_starttime"}}}
```

### 6.6 Warm event buffer (daemon)

The daemon keeps a **bounded per-run ring buffer** of recent events (default ~5k events / a few MiB per run, configurable — the same order of magnitude as the controller's log ring). Each event has a per-run `seq`. A controller that reconnects within the buffer window calls `subscribe(resume_from={seq})` and receives **every missed event in order** — no gap, no re-tail. On overflow (a long disconnect, or high-volume model load) or a daemon restart (`daemon_start_ts` changed), it falls back to `subscribe(resume_from={log_inode, byte_offset})` against the durable log. The buffer is a convenience over the durable record, never a replacement: the `0600` log is always complete.

-----

## 7. Transports & connection lifecycle

### 7.1 LocalTransport (Unix socket)

The controller connects to the local daemon's **Unix domain socket** and speaks the same NDJSON protocol — uniform with remote, minus SSH. If no live daemon is found (missing/stale `agent.json`, dead identity, refused connect) it **auto-spawns** one (§5.1) and connects. An in-process `Agent` (objects in, results out, events via an asyncio queue — no serialization) remains a zero-dependency dev/test mode selected by `local_transport: in_process`, but the socket path is canonical so local shares the daemon's warm-state/multi-controller semantics.

### 7.2 SshStdioTransport (bridge to the remote socket)

`subprocess` of `ssh -o BatchMode=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 [ControlMaster opts] host vela agent connect`, where `agent connect` is a **stateless stdio↔socket bridge** forwarding NDJSON between the SSH connection and the remote daemon's socket (auto-spawning the daemon if absent). Three loops: a stdout NDJSON reader (dispatch to futures/subscriptions), a stderr reader (bridge/daemon diagnostics, never parsed as protocol), and a write lock on stdin. `BatchMode=yes` prevents an interactive password hang; `ControlMaster` reuse makes connect near-instant. **No network port is opened** — the daemon is reached only via its local socket, and remotely only through SSH. Killing the bridge (or an SSH drop) leaves the daemon and its runs untouched.

### 7.3 The uniform `TargetClient`

```python
class TargetClient(Protocol):
    async def connect(self) -> AgentInfo: ...
    async def call(self, method: str, params: dict) -> dict: ...
    def subscribe(self, run_ids, *, resume_from) -> AsyncIterator[AgentEvent]: ...
    async def disconnect(self) -> None: ...
    @property
    def connected(self) -> bool: ...
```
A factory selects the transport from the `TargetConfig`. The TUI never names a transport class.

### 7.4 Handshake & negotiation

First call must be `handshake`. **Version rule (conservative):** the agent advertises the highest protocol it supports; if `controller > agent`, the agent returns `-32006` (upgrade the agent); if `controller < agent`, the agent downgrades to the controller's version within one major. **Capabilities** are additive flags (`builds`,`models`,`gpu`,…); a method from an unadvertised capability returns `-32601`, surfaced as "feature not available on this target." `host_info` carries hostname/platform/driver/`vela_version` for the detail pane.

### 7.5 Keepalive

Application-layer `ping`/pong every 30 s (configurable); no pong within 15 s → declare the **connection** dead and reconnect — the **daemon is unaffected** and keeps supervising runs and buffering events. SSH `ServerAliveInterval` is the transport-layer backstop. App-layer ping catches a stalled bridge or a wedged daemon event loop.

### 7.6 Reconnect & resume-by-offset (Opus correction)

On loss → `RECONNECTING` (the UI freezes last-known state; it does **not** render a false "exited"). Exponential backoff (100 ms→cap 10 s). On reconnect → `handshake`; if `daemon_start_ts` is unchanged the **same warm daemon** is still up, and the controller resumes each run **gap-free by `seq`** from the warm buffer (§6.6). If the buffer overflowed the gap, or `daemon_start_ts` changed (daemon restarted), it falls back to `subscribe(resume_from={log_inode, byte_offset})`; the daemon **validates the inode** against the manifest's `active_log` and, if rotated, restarts at the new active log with a `[resumed after rotation]` note (canonical §7.10 inode machinery). Either way no events are lost and gigabytes are not re-streamed.

### 7.7 Clock-skew discipline (Opus correction)

Every event carries the **agent's** `ts` (wall) and `mono` (monotonic). The controller computes per-phase and overall elapsed (canonical FR-15) **purely from agent timestamps** — never mixing its own wall-clock — so cross-host skew cannot distort the timeline. Display clocks may use the controller's wall-time, but durations always derive from `mono`.

### 7.8 Named failure modes

| Failure | Detection | UI |
|---|---|---|
| SSH/bridge drop | stdout EOF | reconnect to the (still-running) daemon; resume by `seq`/offset |
| Daemon not running | no live `agent.json` / refused connect | **auto-spawn** the daemon and connect; if spawn fails, `AGENT_UNREACHABLE` with `vela agent start` |
| Daemon crash/restart | `daemon_start_ts` changed on reconnect | `discover_runs` (runs survive) + resume by offset; toast "agent restarted, runs intact" |
| SSH auth failure | exit 255 + stderr | `AGENT_UNREACHABLE` banner; no auto-retry |
| Agent not installed | exit 127 / "command not found" | `AGENT_NOT_INSTALLED` banner with the install command |
| Version mismatch | `-32006` on handshake | `AGENT_VERSION_MISMATCH` banner |
| Ping timeout | no pong | treat as connection loss; reconnect (daemon unaffected) |

-----

## 8. Backpressure, ordering & scrubbing

- **Scrubbing is agent-side and unconditional.** `LogSink.scrub` runs before the durable write *and* before the emit that becomes an event; the secrets list (from the supervisor payload + config `api_key`/`HF_TOKEN`) **never leaves the host**. There is **no "raw logs" RPC** and no bypass. The controller trusts agent-side scrubbing and does not re-scrub (which would require shipping secrets to it).
- **Backpressure with lossy coalescing (Opus correction, NFR-A6).** The agent writes events from a bounded outbound queue. Under stdout backpressure (slow controller/terminal), **transient `progress` events are coalesced/dropped** (a progress bar is allowed to skip frames); **committed `log`, `phase`, `ready`, `exited`, `error`, and all responses are lossless.** The complete record is always the durable log on the agent, re-tailable by offset.
- **Ordering.** Events for one `run_id` are strictly ordered (single drain → queue → writer). Across runs they interleave; the controller assumes no cross-run ordering.
- **No agent-side micro-batching timer.** OS pipe buffering plus the controller's existing bounded ring buffer + batched `RichLog` writes (canonical NFR-2) handle burst; don't add a second batching layer.

-----

## 9. Subsystem migration (behavior unchanged, location moved)

- **Process lifecycle** → agent (always supervised, PTY-owning; §5.2).
- **Sidecar/identity** → agent; verify-before-signal unchanged; sidecars/manifests are `0600` on the host (§5.3).
- **Logs** → agent scrubs + persists; only scrubbed `log`/`progress` events stream; reconnect re-tails by offset (§7.6).
- **GPU** → agent samples NVML/`nvidia-smi` for *its* GPUs; streams `gpu` events; `CUDA_VISIBLE_DEVICES` mapping uses the agent's env.
- **Health** → agent probes `127.0.0.1:port` locally; the `ready`/`health` events carry a **controller-reachable URL** computed from the target's routable address (distinct from the loopback probe host — Opus correction, ties to the recent "reachable url" fix).
- **Phase FSM** → **agent-side** (Opus-confirmed): its inputs (committed logs, health, exit) are agent-local, and its `phase_rules`/`error_rules` come from the build-specific `VllmProfile` the agent owns; it emits `phase` events (with `error_kind`/`error_excerpt`) so the controller never regex-matches raw logs.
- **Preflight** → **agent-side** (Opus correction, resolving an inter-draft contradiction): model-path, port, and world-size-vs-*actual*-GPUs test the target; `-32005` carries structured failures for the named banner.
- **Messages → events** → the existing taxonomy maps 1:1 (table §6.4); `ServerReady`/`EngineError` fold into `ready`/`phase` as today's code already half-anticipates (the messages exist but the attached path drives the FSM directly).

-----

## 10. Config, targets, discovery & CLI

### 10.1 Targets registry (controller-local — the only controller-owned state)

`~/.config/vela/targets.yaml` (or `targets.json` beside the controller's state). `local` is implicit, always present, non-removable.

```yaml
targets:
  local:
    transport: local            # implicit; shown, not editable/removable
  blackbird:
    transport: ssh
    host: bgconley@10.25.0.51
    workdir: /home/bgconley/repos/vela     # optional
    venv: /home/bgconley/venvs/vela        # optional; agent binary path
    ssh_opts_env: VELA_SSH_OPTS         # optional extra ssh args
  p620-01:
    transport: ssh
    host: user@10.25.0.20
```

### 10.2 Per-config target reference (additive)

`ModelConfig` gains an optional top-level `target: str | None` (a key into the targets registry); `extra="forbid"` preserved. A config may name its home target, or the UI chooses target + config separately. Absent → the active target. This composes with the sibling specs' additive fields (`command.build`, `model_ref`/`revision`).

### 10.3 Config ownership: agent-side, with a push option (Opus decision)

Configs reference **target-local** paths (model paths, `command.build`, scripts, `cwd`), so discovery (`load_registry`'s precedence: `--configs-dir › VELA_CONFIGS › ./configs › ~/.config/vela/configs`) runs **on the agent**, and `list_configs`/`preview` are agent methods. The controller's `ConfigRegistry` becomes a per-target **view cache** populated by RPC, not a filesystem scan. *Alternative considered (rejected as the default):* controller-authored configs pushed to the agent — kept as an optional `push_config` convenience for author-once-run-anywhere, but a config's host-local references make agent-side discovery the correct primary. (Configs that name only a `model_ref` + `build` are target-portable; encourage that style for multi-host.)

### 10.4 CLI

`vela agent start|stop|status|restart` manages the daemon; `vela agent connect` is the stdio↔socket bridge SSH invokes; `vela agent run` (foreground) aids debugging. Controller commands gain `--target <name>`; a `targets` group manages the registry (`targets list/add/remove/test`). The coder's `agent launch/status/tail/stop/kill/gpu` verbs are thin RPC clients that attach to the target's daemon (cross-connection continuity is the daemon's warm state; cross-restart continuity is via sidecars).

-----

## 11. Composition with builds & models (per-target, over RPC)

Builds (`~/.local/share/vela/builds/`) and models (HF cache + `~/.local/state/vela/models/registry.json`) are **the agent's** — per host. `list_builds`/`create_build`/`select_build`/`verify_build`/`remove_build` and `list_models`/`pin_model`/`download_model`/`verify_model`/`remove_model` are agent RPC methods (§6.3). Their **install/download "streamed jobs"** (build spec §7.1, model spec §8) reuse this protocol's **same event stream** as `job_progress`/`job_done` — the controller renders them through the existing `RichLog`/`ProgressLine`/`ErrorBanner`, identical to run logs. At `launch`, the agent resolves the build handoff (build spec §7.5: executable + env-overlay + version) and model handoff (model spec §9: `model_arg` + `--revision` + HF env) **locally** and folds both into the supervisor payload — the controller passes only `build_id`/`model_ref`/`revision`. Switching target re-scopes every picker to that host's resources.

-----

## 12. Controller TUI UX

Matches canonical §8 + the sibling specs. Header order (outermost scope first): `app-title · ⊕target · ▣build · M model · ●STATUS · url · clock`.

**Target segment** `⊕ <name> <conn-dot>` with the connection vocabulary `● connected` / `◐ connecting (pulse)` / `○ disconnected` / `▲ version-mismatch` / `✕ unreachable`. The **connection dot never disappears** (safety-critical, like "the log never disappears"); compacts `⊕ blackbird ●` → `⊕bbrd●` → `⊕●`. When disconnected, build/model segments show `—`.

```
 Vela  ⊕ blackbird ●  ▣ vllm-nightly ●  M 📌qwen3-32b ● 62GB  ●READY  http://10.25.0.51:18003  12:42
              └─ target ──┘  └── build ─────┘  └──── model ───────┘  └ status ┘
```

**TargetManagerScreen** (modal, list+detail like `ConfigPickerScreen`): rows `<marker> <conn-dot> <name>  <transport> <host>  [agent v…]  [N runs]`; detail = transport/host/workdir/venv, agent version vs controller, **capabilities** (dim the ones an older agent lacks → disables that target's `b`/`m`/`F`), GPUs summary, active-runs, last-seen. `Enter` switches the active target (re-scopes pickers, starts a connect, updates the dot); `n/e/x` manage targets; `R` reconnect; `Esc` close.

**Connection lifecycle:** connecting shows a pulsing `◐` + a `ProgressLine`; success toasts `Connected to blackbird (agent v…)`; failures use the **ErrorBanner** with named causes (`AGENT_UNREACHABLE`/`AGENT_NOT_INSTALLED`/`AGENT_VERSION_MISMATCH`) carrying a concrete suggestion (the `ssh …`/`pip install` command) and `(R) Reconnect`/`(t) Switch target` affordances.

**Disconnected dashboard:** last-known state greyed (`status--idle`), Load/Stop/Kill/Restart **disabled** (warn-toast if pressed), `R Reconnect` promoted to a primary footer binding, the log shown read-only with a "disconnected at …" rule. Fresh start + unreachable = "unknown" (the cache is per-session, in-memory).

**Composition:** config detail/preview gains `target:` / `build:` / `model:` rows above the resolved command; the preview already includes `--revision` and the build's env overlay. **Destructive confirms name the target** (Opus addition): "Stop qwen3-32b **on blackbird**?" — preventing wrong-host actions. A pre-launch guard blocks launching against a disconnected/unreachable target.

**Keys (collision-checked vs `l s K r c / f p w g G ? F1 q ^C ^P` + builds `b`/`F` + models `m`):** **`t`** → Target Manager, **`R`** → reconnect (capital, mirrors `K`; does not shadow `r` restart). Palette: `Manage targets`, `Switch target: <name>`, `Reconnect agent`, `Agent info`.

-----

## 13. Security

SSH auth is reused (keys/agent/`ProxyJump`/`ControlMaster`); `BatchMode=yes` blocks interactive hangs; **no network port is opened** — the daemon listens only on a **Unix domain socket** in a `0700` dir with a `0600` socket owned by the user, and additionally checks the peer UID via `SO_PEERCRED`. Remote reach is **only** through SSH (the stdio↔socket bridge), so a controller can reach the daemon iff it can already SSH in as that user. The daemon runs **as the target user** — exactly vLLM's own permissions, no escalation. The RPC surface can launch/stop/kill, so: the controller never supplies a PID/pgid/sidecar path (it sends `run_id`; the daemon re-reads + re-verifies before every signal); there is **no bypass-scrub method**; build installs/model downloads run arbitrary package code **by design** (the user authorized that host).

**The daemon is a standing process** (the cost of robustness). Honest framing: it grants **no capability the principal does not already have** — anyone who can connect (same local user, or SSH-as-that-user) can already launch vLLM directly. The marginal surface is a *longer-lived* unprivileged process, contained by socket permissions + `SO_PEERCRED` + same-user + no network exposure + no privilege. For **shared/multi-tenant hosts**, a per-connection capability token over the socket is the planned hardening (§17); single-user hosts need none.

-----

## 14. Testing strategy

Without SSH or a GPU: a **FakeTransport** (scripted NDJSON in/out) + an **in-process Agent** over the existing `fake_child`. 
- **`test_rpc_framing`** — request/response correlation; event demux; interleaving; idempotent re-launch; line-cap truncation; response-priority over buffered events.
- **`test_agent_authority`** — verify-before-signal agent-side (alive+matching ⇒ signal; recycled PID ⇒ `-32002`, no signal); preflight `-32005` structured failures; controller cannot pass a PID.
- **`test_transport_local`/`test_transport_ssh`** — uniform `TargetClient` behavior; SSH transport against a fake `ssh` that execs a local agent; stderr-vs-stdout separation.
- **`test_connection_lifecycle`** — handshake/version/capability negotiation; ping-timeout; reconnect; **resume-by-offset** (inode match + rotation restart); named failure modes.
- **`test_event_mapping`** — every existing message reconstructed from events; clock-skew elapsed computed from agent `mono` only.
- **`test_backpressure`** — transient coalescing under a stalled reader; lossless lifecycle/log frames; durable log complete.
- **`test_scrub_before_wire`** — secrets never appear in any event or response; no raw-log path exists.
- **`test_discover_reattach`** — `discover_runs` rebuilds from fake sidecars; reattach verifies identity.
- **`test_compose_builds_models`** — `launch` folds build+model handoffs; `job_progress`/`job_done`/`cancel_job` for installs/downloads.
- **TUI smoke** (`App.run_test()`): switch targets re-scopes pickers; disconnected disables actions; confirm names the target. **Manual:** controller on P620‑01 launches the Qwen3.6 config on Blackbird over SSH; full phase walk, READY at the reachable URL, stop with agent-side verify, reconnect resumes tailing.

-----

## 15. Migration / back-compat

**Local-only users are unaffected.** The first agent implementation *is* the local in-process agent over the existing engine; with only a `local` target the UX and behavior match today. The refactor proceeds by **strangler**: wrap each engine capability behind the `Agent` API, route the local TUI through `TargetClient(local)`, prove parity, then add the SSH transport. The brittle status quo it removes — a Blackbird config whose `command.executable` points at a shell script that runs on the *controller* — is replaced by a real remote agent that owns lifecycle on Blackbird. Each phase keeps the app fully working.

-----

## 16. Implementation plan (phased; each shippable)

- **PA0 Agent API + in-process LocalTransport (~2–3d):** define the `Agent` class wrapping today's `process_manager`/`sidecar`/`log_sink`/`monitoring`/`phases`/`preflight`/`command_builder`; a `TargetClient` with `LocalTransport`. Route the TUI's launch/stop/log/health/GPU/FSM calls through it for the `local` target. *Done when:* the TUI runs entirely via the local agent with byte-for-byte parity; full suite green.
- **PA1 NDJSON protocol + framing (~2d):** request/response futures, event demux, idempotency keys, line cap, response priority. *Done when:* `test_rpc_framing` green; an in-process subprocess agent passes the same tests as the in-process object.
- **PA2 Always-supervised + PTY-owning supervisor (~2–3d):** unify launch onto the supervised path; give the supervisor a PTY so live `\r` bars survive; "attached" = a live subscription. *Done when:* live progress bars stream over the local subprocess transport; runs survive agent exit.
- **PA3 Daemon + Unix-socket + SSH bridge + lifecycle (~3–4d):** the per-user daemon (socket listener, `agent.json` identity, `SO_PEERCRED`, warm per-run event buffer with `seq`); the `agent connect` stdio↔socket bridge; systemd-user unit + auto-spawn + `agent start/stop/status`; handshake/version/capability; ping/keepalive; reconnect with **seq-replay then offset-fallback**; named failure modes incl. daemon-restart detection. *Done when:* controller on host A drives the daemon on host B; killing the controller leaves the daemon+runs up; reconnect replays the gap gap-free; a daemon restart re-discovers runs.
- **PA4 Agent-side authority hardening (~1–2d):** verify-before-signal, preflight, discover/reattach all agent-side over RPC; `-32002`/`-32005`. *Done when:* `test_agent_authority`/`test_discover_reattach` green; recycled-PID refusal works remotely.
- **PA5 Targets registry + CLI + controller UX (~3d):** `targets.yaml`, `--target`, `vela agent`/`targets` CLI; header target segment, `TargetManagerScreen`, disconnected state, target-named confirms, reconnect. *Done when:* switch/connect/disconnect/reconnect from the TUI; the Blackbird end-to-end manual test passes.
- **PA6 Builds/models over RPC (~2d, after sibling specs):** `list_*`/mutators + `job_progress`/`job_done`/`cancel_job`; `launch` folds handoffs. *Done when:* create-build and download-model stream through the controller against a remote target.
- **PA7 Backpressure, scrubbing, polish, docs (~1–2d):** lossy coalescing, scrub-before-wire tests, capability-gated UI, README + the P620‑01↔Blackbird worked example.

**MVP = PA0–PA5** (local parity + real remote control with full lifecycle/identity/UX). **Full v1 = PA0–PA7.** PA0 alone is a safe, valuable refactor (decouples the TUI from the engine) even before any remote work.

-----

## 17. Future enhancements

HTTP/WebSocket/gRPC transports behind auth (for a hosted controller not reached via SSH); a **per-connection capability token** over the socket for shared/multi-tenant hosts; a **runs-across-targets overview** (the daemon model makes this natural — persistent background connections to several daemons); fan-out launches across a pool; agent auto-install/bootstrap from the controller; a control-plane inventory of targets' GPUs/builds/models; mTLS for non-SSH transports. (The PTY-owning supervisor, previously deferred in canonical §16, is pulled *into* this v1 by PA2; the persistent daemon, previously deferred, is **now v1** per the owner's robustness requirement.)

-----

## Appendix A — Decision log (incl. Opus-4.8 review corrections)

- **Agent owns all lifecycle authority; controller is a thin view.** The only safe place to signal/verify/probe/sample is the host running vLLM.
- **Local is just another target; uniform `TargetClient`.** No privileged local path that can drift from remote.
- **Always-supervised execution; "attached" = a subscription (Opus).** A PTY can't cross RPC and won't survive SSH drop; collapsing the two paths is a simplification, and the **PTY-owning supervisor** is promoted to v1 to keep live bars.
- **Controller-minted `run_id`/`job_id` idempotency keys (Opus).** A dropped response over flaky SSH must not orphan or double-launch.
- **Lossy coalescing of transient events; lossless lifecycle/log/responses (Opus).** Progress may skip frames under backpressure; the durable agent log is the complete record. Dropped the over-engineered 5 ms batching; right-sized the line cap to ~2 MiB.
- **Resume-by-offset on reconnect (Opus).** `{log_inode, byte_offset}`, inode-validated against the manifest; no full replay; no agent memory buffer.
- **Clock-skew discipline (Opus).** Elapsed derives only from agent `mono`; never mix controller wall-clock across hosts.
- **Preflight is agent-side (Opus).** It tests the target's filesystem/network/GPUs; resolved an inter-draft contradiction.
- **`ready` carries a controller-reachable URL (Opus).** Distinct from the loopback probe host.
- **Phase FSM is agent-side.** Inputs are agent-local; the profile is build-specific (agent-owned).
- **Scrub agent-side, unconditional, no bypass.** Secrets never leave the host; controller doesn't re-scrub.
- **Agent lifetime = a persistent per-user daemon (owner decision).** A standing process on each target, decoupled from any controller connection — chosen for robustness over a flaky link and warm state across reconnects. Reached via a **user-owned Unix socket**, remotely through an **SSH stdio↔socket bridge** (no network port; SSH still gates remote access). Lifecycle = systemd-user / auto-spawn-on-connect / `agent start|stop`. It survives controller disconnect/restart, **serves multiple controllers**, and keeps a **warm per-run event buffer** (`seq`-indexed) so reconnect is gap-free; runs survive a daemon restart via sidecar re-discovery, with the `0600` durable log as the offset-fallback record. *Tradeoff accepted:* a standing unprivileged listener (vs the ephemeral session model) — contained by socket perms + `SO_PEERCRED` + same-user + SSH-only remote reach; per-connection token auth deferred to shared-host hardening (§17). *Supersedes the earlier session-scoped recommendation.*
- **Configs are agent-side (discovery on the target), with an optional `push_config` (Opus).** Configs reference host-local paths; portable configs (`model_ref`+`build`) encouraged for multi-host.
- **`cancel_job`, target-named confirms, implicit non-removable `local` target (Opus additions).**
- **One active target in v1; multi-host overview deferred.** Keeps the connection model simple; architecture leaves room for always-on connections later.
- **NDJSON over SSH stdio first; HTTP/WS/gRPC later.** No new open port; reuses SSH auth; safer than an exposed control server.

## Appendix B — Example: controller on P620‑01 driving Blackbird

```yaml
# ~/.config/vela/targets.yaml  (controller-local)
targets:
  local:     { transport: local }
  blackbird: { transport: ssh, host: bgconley@10.25.0.51,
               workdir: /home/bgconley/repos/vela, venv: /home/bgconley/venvs/vela }
```
```yaml
# A config discovered BY THE BLACKBIRD AGENT (lives on Blackbird), target-portable:
name: qwen36-27b-fp8-kvfp8-rp6000
target: blackbird                 # optional home target
model: Qwen/Qwen3.6-27B-FP8       # resolved against Blackbird's HF cache
command: { build: vllm-nightly-cu130-rp6000 }   # resolved against Blackbird's builds
engine: { kv_cache_dtype: fp8 }
server: { host: 0.0.0.0, port: 18003, exposure: lan }
launch: { ready_timeout_seconds: 1200 }
```
One-time on Blackbird: `systemctl --user enable --now vela-agent` (or let the first connect auto-spawn it). Controller flow: `t` → select `blackbird` → the SSH bridge attaches to Blackbird's running daemon → handshake → `list_configs`/`list_builds`/`list_models` (Blackbird's) → `l` Load → the daemon preflights on Blackbird, supervises the run, streams `phase`/`log`/`ready` → header shows `⊕ blackbird ● … ●READY http://10.25.0.51:18003`. Close the laptop, reopen, reconnect → the daemon is still up and replays the gap gap-free. Stop re-verifies identity on Blackbird before signaling. The run also survives a daemon restart (re-discovered from sidecars) and reproduces standalone via `vllm serve … --revision …` — the controller is never required at runtime.

## Appendix C — Code anchors for the refactor (current tree, ~199 tests)

Cut points to route through the `Agent` API (file:symbol): `tui/app.py` `_run_selected_config` (launch/preflight/build/profile/health), `action_stop`/`confirm_kill_running`/`action_restart` (lifecycle), `reattach_detached_run`/`_tail_detached_log`/`_sidecar_is_alive` (discover/tail/verify), `get_system_commands` (`discover_active_sidecars`), `_poll_gpu_panel`/`_sample_gpu_panel_once` (GPU), `_probe_until_ready`/`_server_url` (health), `_handle_committed_log`/`on_health_changed`/`on_process_exited` (FSM feeds); `cli.py` `run_config`/`smoke*`/`preview`/`list_configs`. Engine to wrap unchanged behind the agent: `engine/process_manager.py`, `engine/supervisor.py` (+PTY), `engine/sidecar.py`, `engine/log_sink.py` (+`engine/redaction.py` scrub), `engine/phases.py`, `engine/preflight.py`, `monitoring/gpu.py`, `monitoring/health.py`. New: `agent/` (daemon, Unix-socket server, run registry, warm `seq`-indexed event buffer, NDJSON, `agent connect` bridge, `agent.json` identity), `transport/` (uds-local, ssh-bridge, `TargetClient`), `config/targets.py`, `tui/screens/target_manager.py`, plus a `vela-agent` systemd user unit.
