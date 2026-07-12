# vLLM Agent Architecture — Implementation Review Punch List

**Reviewing:** the in-progress implementation of `vllm-agent-architecture-spec-v1.md`
**State at review:** PA0–PA4 complete, most of PA3 (daemon/socket/bridge) complete, PA5 started; **329 tests passing**.
**Method:** five Sonnet 4.6 reviewers; all load-bearing findings independently verified by Opus 4.8 (items tagged **[Opus-verified]** were checked against the code directly, not just relayed).

This is a prioritized, actionable follow-up list. It does **not** ask for rework of what's done — the architecture is sound and on-spec. It captures the gaps to close, ordered by impact.

---

## ✅ Confirmed correct — DO NOT REGRESS

These are the hard-won invariants the spec requires. They are verified in place; any future change must preserve them. Add a regression test if one isn't already pinning each.

- **Controller holds no process authority.** `grep -nE "current_process|Popen|\.proc\.|stop_sidecar_from_system|signal_sidecar_from_system|killpg|os\.kill" src/vllm_loader/tui/app.py src/vllm_loader/cli.py` → **zero matches**. The controller carries only a `run_id` string. **[Opus-verified]**
- **In-process transport is wire-safe.** `transport/inprocess.py:89-90` round-trips `decode_frame(encode_frame(...))` on params (`:47`), result (`:52`), and every subscription event (`:80`). A non-serializable object (Popen/Path/Sidecar/callback) raises at the boundary. Keep this sentinel — it is what guarantees the socket/SSH transports are byte-identical. **[Opus-verified]**
- **Verify-before-every-destructive-signal, agent-side, no bypass.** `agent/local.py` stop/kill (`:143/:145`) → `_request_stop_signal`/`_request_kill_signal` → `sidecar.py` `destructive_signal` → `verify_sidecar_identity` (`:148`) before `os.killpg`. Re-verified at each SIGINT→TERM→KILL step. **[Opus-verified]**
- **SO_PEERCRED peer-UID check** on every socket connection (`agent/socket.py` `verify_same_user_peer`); rejects a mismatched UID with `PermissionError`. Pinned by `tests/test_agent_socket.py`. **[Opus-verified via the dedicated test]**
- **No network port bound** (Unix socket only); dir `0700` / socket `0600` / `agent.json` `0600`.
- **Scrub-before-emit:** every log line passes `LogSink.scrub()` (`log_sink.py:141-143` → `redaction.py:11-18`) before durable write and before the event stream. No raw-log/bypass-scrub RPC exists.
- **Clock discipline:** phase elapsed computed from agent `mono`/`ts`, never the controller clock (`app.py:2059-2076, 2187-2200`).
- **Idempotent launch** on a controller-minted `run_id` (`agent/local.py:271-281`); **both** resume paths exist — warm seq ring buffer (`:118-119, 660-699`) and `{log_inode, byte_offset}` offset replay (`:701-744`).

---

## P1 — Functional: `reachable_url` is loopback for remote targets **(fix before the first real SSH run)** **[Opus-verified]**

**Severity:** High (silently wrong for the headline P620‑01 → Blackbird use case; passes today only because all end-to-end testing is local).
**Spec:** §9, §7.6 — the `ready`/`health` events must carry a **controller-reachable** URL, *distinct* from the loopback probe host.

**Problem.** `agent/local.py:1036-1037`:
```python
def _reachable_url(cfg): return f"http://{probe_host_for(cfg.server)}:{cfg.server.port}"
```
and `monitoring/health.py:22-27` `probe_host_for` returns `127.0.0.1` for **any** non-loopback bind (including `0.0.0.0` *and* a routable IP). So an agent on Blackbird reports `reachable_url = http://127.0.0.1:18003`. The controller (which now trusts the event value — commits "honor health event reachable url") shows a URL pointing at its **own** loopback, not Blackbird.

**Fix direction.** Decouple "where to probe" from "where the controller reaches it":
- Keep `probe_host_for` for the agent's loopback probe (correct).
- Compute `reachable_url` from a routable host: the **target registry `host`** (the controller knows it for SSH targets — `config/targets.py`), or `server.host` when it is a concrete routable address, or a dedicated `server.advertised_host`. For a `local` target, loopback remains correct.
- Decide ownership: simplest is the **controller** rewriting the host of the agent-reported URL using the active target's `host` (agent reports `bind_host`+`port`; controller composes the reachable URL). This keeps the agent host-agnostic.

**Acceptance.** A unit test where an SSH target with `host=10.25.0.51` and a config bound to `0.0.0.0:18003` yields a controller-facing URL of `http://10.25.0.51:18003`; local target still yields `http://127.0.0.1:<port>`.

---

## P2 — Finish PA5 controller UI (the state machine is done; only rendering remains)

**Severity:** Medium-High (most visible remaining MVP work; the connection state is fully tracked and tested but invisible).
**Spec:** §12; §16 PA5.

The connection-state machine is complete and tested (all 5 states set: `connected/connecting/reconnecting/disconnected/unreachable/version-mismatch`, `app.py:1441-1519`). What's missing is the **rendering**:

- **Header target segment** — render `⊕ <name> <conn-dot>` in `#top-chrome` (`_refresh_chrome`, ~`app.py:1713`); the dot reflects `target_connection_state`. Currently no `⊕` glyph or target slot exists in the header. Compaction rules per §12.
- **`TargetManagerScreen`** — no `tui/screens/target_manager.py` yet (spec Appendix C lists it). Modal list+detail like `config_picker.py`.
- **Keybindings** — add `t` (Target Manager) and `R` (reconnect) to `BINDINGS` (~`app.py:446-462`) + `action_targets`/`action_reconnect`; both confirmed collision-free.
- **Named failure banners** — surface `AGENT_UNREACHABLE` / `AGENT_VERSION_MISMATCH` / `AGENT_NOT_INSTALLED` (cause + suggestion + `(R) Reconnect`/`(t) Switch target`) via the existing ErrorBanner. State→banner mapping already exists at `_mark_target_connection_error` (`app.py:1479`); only the banner render is missing.
- **Disconnected-dashboard guard** — `action_load` (~`app.py:729`) does not check `target_connection_state` before launching; add a pre-launch guard ("target unreachable — reconnect first") and grey/disable lifecycle actions while disconnected; promote `R Reconnect` in the footer.

**Already done (keep):** target-named destructive confirms ("…on blackbird?", `app.py:771/781`, pinned by `test_tui_smoke.py:2415`).

**Acceptance.** Smoke tests asserting the header dot reflects each state; `t` opens the manager; a simulated unreachable/version-mismatch renders the named banner; `action_load` is blocked when disconnected.

---

## P3 — Tighten the protocol contract (before any cross-version / SSH-CLI client depends on it)

**Severity:** Medium (cosmetic in-process today; it's the wire contract that a remote/older client will parse).
**Spec:** §6.1, §6.3, §7.4.

- **Integer error codes + `data` key.** `agent/stdio.py:73,93,104` emit string codes (`"invalid-request"`, …) and spell the payload key `details`. Spec wants integer codes (`-32700/-32600/-32601/-32602/-32000…-32010`) and `error.data`. Map `TargetCallError` codes → ints; rename `details`→`data` on the wire (keep the Python attr if convenient).
- **`discover_runs_no_paths` advertised but not dispatched.** It's in `AGENT_CAPABILITIES` (`agent/local.py:62`) but absent from `handle()` dispatch — a client trusting that capability gets method-not-found. Either implement it or drop it from the advertised set. **[Opus-verified]**
- **Handshake completeness (§7.4).** Add the older-controller **downgrade path** (currently only `controller > agent` is rejected; `controller < agent` succeeds silently with the agent's version — `agent/local.py:166-208`); parse/record `controller_version` (distinct from `protocol_version`); add `driver` to `host_info`.
- **Response-vs-event prioritization** (`agent/stdio.py:31-55` shares one write lock) — minor; consider a small priority on response frames so a `stop` reply isn't queued behind a burst of `log` events.
- **`launch` result shape** — returns `{run_id, launch_mode, status}`, omits `sidecar_path` from the §6.3 example. The controller doesn't need it (arguably better for the boundary) — either add it for spec-shape parity or note the intentional omission.

**Acceptance.** Extend `test_rpc_framing` to assert integer error codes and the `data` key; a handshake test for older-controller downgrade; remove/cover the dangling capability.

---

## P4 — Robustness hardening

**Severity:** Medium-Low (degrade gracefully today; below spec under adverse conditions).
**Spec:** §7.6, §9, §6.4.

- **Graceful log-rotation on resume.** `agent/local.py:714-721` **raises** `identity-verification-failed` on an inode mismatch instead of restarting at the new active log with a `[resumed after rotation]` note (§7.6). A rotation during a disconnect would currently fail the reconnect. Fall back to the manifest's new `active_log` and emit the note.
- **Exponential reconnect backoff** (§7.6, 100 ms→cap 10 s). Reconnect currently relies on the ping interval (no backoff loop in `app.py`/`socket.py`/`subprocess.py`). Add a backoff so a hard-down host isn't hammered every ping tick.
- **GPU as push events** (§9/§6.4). GPU is sampled agent-side (correct) but **RPC-polled** (`app.py:~2292`), not emitted as `gpu` events. Fine for one controller; move to push events before multi-controller fanout so all subscribers see the same stream.

**Acceptance.** A reconnect test across a simulated rotation (no error, stream resumes); a backoff unit test; a `gpu` event kind in the stream consumed by the panel.

---

## Deferred / optional (PA3 polish & spec-optional)

Not blocking; track them so they aren't lost.

- **systemd user unit** (`vllm-loader-agent.service`) — §5.1/§16 list it as a PA3 deliverable; auto-spawn covers zero-config, so it's a convenience/production-supervision artifact, currently absent.
- **`--idle-timeout`** — spec-optional, default-off; not implemented.
- **Daemon startup discovery** — registry is rebuilt lazily on first `discover_runs` (`agent/local.py:397`) rather than eagerly at startup; functionally equivalent, but eager rebuild matches the §5.1 wording.
- **Auto-spawn double-fork** — uses `Popen(start_new_session=True)` (`agent/daemon.py:~97`) rather than a POSIX double-fork; works on Linux/macOS, low risk.
- **`ControlMaster`** is opt-in via `ssh_opts_env`, not a default (§7.2 mentions it for near-instant connect) — consider defaulting it.

---

## Out of scope for this list

**PA6 (builds/models over RPC)** — not started, correctly. When it lands, `list_builds`/`create_build`/`list_models`/`download_model`/`cancel_job` reuse the same NDJSON channel and the `job_progress`/`job_done` events (none of which exist yet — expected). The `cancel_job` method and the `gpu`/`job_*`/`error`/`agent_error` event kinds are PA6-era.

---

## Definition of done for "remote works end-to-end"

The single acceptance test that closes the headline gap: **controller on P620‑01, agent on Blackbird over SSH** — `t` selects `blackbird`, handshake succeeds, configs/builds/models list from Blackbird, `l` launches there, the header shows `⊕ blackbird ●` and **`●READY http://10.25.0.51:18003`** (P1), stop re-verifies identity on Blackbird, the laptop sleeps/reconnects and resumes the stream gap-free (P4), and the run survives a daemon restart. P1 + P2 + P4 are the items standing between today and that test passing.
