# vLLM Loader — Progress Re-Evaluation (cross-spec) **v3**

**What this is:** a third pass measuring the agentic coder's progress since findings **v2**, how faithfully it tracks the specs, and how close the (new) agent/controller architecture is to done.
**Baseline → HEAD:** `aa497e0` → `1f473c7` — **20 commits, +1,947/−76** in the committed window `aa497e0..b3e728f`, plus 3 more SSH-hardening commits landed *during* this review (`f5a0201`, `e4a2b1f`, `1f473c7`). **The coder is committing live — HEAD moved 3× mid-review.**
**Method:** 4 parallel **Sonnet 4.6** reviewers (v2-punchlist verify, new security-hardening review, tests/determinism, new-bug/spec-drift), then **Opus 4.8** independently re-read every load-bearing claim. `[Opus-verified]` = I read the code.
**Test state:** **~706 passing, 0 failed, 0 skipped**, and now **deterministically green** (the v2 flaky test is fixed; agents ran the full suite 4× clean; my isolated run = 699 at `b3e728f`). **Ruff: clean** (the v2 `I001` is fixed). All five core safety invariants **HELD** (crown-jewel grep clean).

---

## 0. Headline

**This is a model run of punchlist execution.** The coder closed the **entire v2 punchlist (N1–N6) plus the lowest-priority Q10 polish** — each with a dedicated, substantive regression test — and then went *beyond* it with a well-tested **SSH/agent-auth security-hardening wave** that anticipates the agent spec's §13/§17 security model. No new High or confirmed-Med product bugs were introduced; every new finding is Low. Core invariants held, and the headline correctness risk I most wanted to check — whether the streamed deep-verify changed the hash — is **clean**.

| Domain | v2 | **v3** | What moved |
|---|---|---|---|
| Canonical core engine (v2) | ~90% | ~90% | stable |
| Build management (v1) | ~88% | ~89% | formal `BuildPhase` enum |
| Model management (v1) | ~84% | **~88%** | N1 OOM fixed (backward-compatible), `DownloadPhase` enum, typed sidecar identity |
| **Agent/controller architecture (v1)** | ~93% | **~95–96%** | identity-error codes, frame limits, SSH-injection defense, token auth, NDJSON validation |
| **Overall, to a polished v1** | ~89–91% | **~92–94%** | |

---

## 1. v2 punchlist — closure (Opus-verified)

| Item (v2 severity) | Status | Evidence | Test |
|---|---|---|---|
| **N1 (High)** deep-verify OOM | ✅ FIXED **[Opus-verified, incl. hash backward-compat]** | `model_registry.py:1794` `_stream_file_sha256_uri` chunks at `HASH_CHUNK_BYTES`; both HF (`:1476`) and local (`:1594`) paths use it; **zero `read_bytes()` remain**. Caller order (`name\0 size\0 content \0`, `stat().st_size==len(data)`) makes the aggregate digest **byte-identical to the old whole-file version** → stored integrity still matches. | `…without_reading_files_into_memory` (HF + local) monkeypatch `read_bytes` to raise and assert verify still passes |
| **N6 (Med)** flaky TUI test | ✅ FIXED — **deterministically green** | test now uses `_wait_for_textual_condition` (`pilot.pause()` loop) + an `asyncio.Event` precheck sentinel instead of a fixed sleep | suite ran 4× clean across two reviewers; isolated 5/5 |
| **N2 (Low-Med)** missing RPC codes | ✅ FIXED | `rpc_errors.py:29-31` `build-integrity-failed→-32014`, `cancelled→-32015`, `profile-error→-32016` (+ `agent-auth-required→-32017`) | parametrized round-trip test asserts each code |
| **N4 (Low)** real large-frame test | ✅ FIXED | `test_rpc_framing.py:64` round-trips a **1 MiB** payload through a real `asyncio.StreamReader` and asserts byte-perfect `decode_frame` equality | self |
| **N5 (Low-Med)** malformed-frame surfacing | ✅ FIXED | `subprocess.py`/`socket.py` now `_publish_event(agent_error_event(...))` + `continue`; TUI `on_agent_error` branches on `fatal` | client agent-error tests + TUI wiring |
| **Q10 (Low)** formal enums + typed sidecar | ✅ FIXED | new `engine/job_phases.py` `BuildPhase`(7)/`DownloadPhase`(5) used at ~18 sites; `sidecar.py` adds typed `model_ref`/`model_entry_id`/`model_repo_id`/`model_revision`/`model_commit_sha`; in-use guard checks typed fields first, `config_snapshot` retained for back-compat | `test_job_phases.py` + typed-sidecar in-use guard tests |

**6 / 6 closed.** Even the polish item I explicitly de-prioritized in v2 (Q10) was done properly, with the `config_snapshot` fallback correctly retained for old sidecars.

---

## 2. NEW: SSH / agent-auth security hardening (beyond the punchlist)

A substantial, spec-relevant security wave (agent spec §13 SSH-only reach + §17 shared-host token). **Assessed sound for the §13 single-user threat model.**

- **SSH option-injection defense** (`transport/factory.py`, +249) — **[Opus-verified sound].** A **hybrid allowlist/denylist**: unknown bare flags are *rejected* (`else: raise "unsupported SSH option"`); `-o Key=Value` (both spaced and concatenated forms) is denied for command-bearing (`ProxyCommand`/`LocalCommand`/`PermitLocalCommand`/`RemoteCommand`), host-verification-weakening (`StrictHostKeyChecking=no`, `UserKnownHostsFile=/dev/null`, value-conditional), identity-override (`HostName`/`User`/`-l`), provider-loading (`PKCS11Provider`/`Include`/`-I`/`-F`), routing (`ControlPath`/`-S`), TTY (`-t`/`RequestTTY`), stdio-suppression, and managed-option override (`BatchMode`/`ServerAliveInterval`). Legit options pass (`-i`/`IdentityFile`, `-J`/`ProxyJump`, `Port`, `ControlMaster`/`ControlPersist`). Reviewers checked `=`-vs-space, case, `-o` concatenation, shlex newline, repetition — **no bypass found.**
- **Agent token auth** (`agent/auth.py` + `agent/stdio.py` + `agent/local.py`) — **[Opus-verified].** `hmac.compare_digest` (timing-safe); required before any method except `handshake` (auth gate `stdio.py`); fail-closed; token never logged/leaked; **frictionless local preserved** (`configured_agent_token()` returns `None` when `VLLM_LOADER_AGENT_TOKEN` unset → every connection pre-authenticated). Composes with the existing `SO_PEERCRED`/same-user socket guard. This is the §17 shared-host hardening, landing early.
- **NDJSON request validation** (`agent/stdio.py`) — rejects missing/non-string `id`, non-string `method`, non-dict `params` with correct JSON-RPC codes, without crashing the loop.

---

## 3. New findings (all Low — a short, optional list)

- **V3-1 [Low]** `agent/stdio.py` read loop doesn't catch `readline()` `ValueError` (oversized line >2 MiB) or `json.loads` `RecursionError` (deeply-nested object) — a crafted frame crashes that **one connection** (daemon survives; self-DoS only). Matters before §17 shared-host exposure. *(Agent-reported; consistent with the read-loop structure.)*
- **V3-2 [Low]** `-A` (ForwardAgent) is in `_SAFE_SSH_FLAGS`, and `-o LocalForward/RemoteForward/DynamicForward` aren't denied (only the `-L/-R/-D` flag forms are) → agent-forward / tunnels possible. Operator-controlled and §13-bounded; block for §17. **[Opus-verified `-A` present at `factory.py:27`.]**
- **V3-3 [Low]** `-o=Key=Value` yields an empty key that passes the validator. **No real bypass** — OpenSSH rejects the malformed option — but the validator should reject it for a clean error. **[Opus-verified.]**
- **V3-4 [Low]** A non-`TargetCallError` exception *mid-handshake* (after a valid token check) leaves the connection unauthenticated → next call blocked until reconnect. Fail-closed (safe), but a hard-to-diagnose usability trap. *(Agent-reported.)*
- **V3-5 [Low]** `_handle_frame(auth_state=None)` default skips the auth gate; only test call-sites use it today, but the default is a latent bypass — better to require the arg. *(Agent-reported.)*
- **V3-6 [Low]** No `agent gen-token` utility / entropy floor — an operator could set a weak `VLLM_LOADER_AGENT_TOKEN`. Usability guardrail for §17, not a code bug. *(Agent-reported.)*
- **V3-7 [Low, test]** The **local-model** deep-verify test asserts blob *file names* but not hash *values* (the HF variant does check values) — a silent hash corruption in the local path would pass. Add value assertions. *(Agent-reported.)*
- *(info)* 77 other TUI tests still use the older `_wait_for_condition` (fixed-sleep) helper; passing reliably now, but the same structural timing weakness the N6 fix removed. Migrating them to `_wait_for_textual_condition` would harden determinism.

No new High/Med-confirmed product bugs. That the entire fresh-finding list is Low is itself a quality signal.

---

## 4. Invariants — all HELD (regression check)
Crown-jewel (no authority in `app.py`/`cli.py`) **clean**; scrub-before-wire intact (the auth token rides its own channel, never logged); live-run remove guard + force-can't-bypass intact; **streamed deep-verify hash backward-compatible** (verified); auth-required does **not** break the frictionless local/in-process path.

---

## 5. How close to done is the new (agent/controller) architecture?

**~95–96% of its v1 scope — functionally complete and now security-hardened.** Since v2 the agent architecture gained: the spec's named identity-error code (`-32002`), correct frame limits + a real large-frame test, formal job-phase enums, typed sidecar resource identity, a comprehensive SSH option-injection defense, per-stream token auth (the §17 shared-host hardening, landing early), and NDJSON request validation — all tested and deterministically green. Real P620→Blackbird validation was already proven; the uncommitted working-tree changes (`scripts/real_model_resume_check.py`, `tests/test_remote_workflow.py`, `docs/gpu-workflow.md`) suggest the coder is currently **refreshing that real-hardware validation**.

What remains for the agent architecture is **Low-severity polish** (V3-1 read-loop hardening, V3-2/3 SSH edge cases, V3-4/5 auth ergonomics, V3-6 token UX). Everything the spec defers (HTTP/WS/gRPC transports, multi-host runs-overview) is **explicitly out of v1 scope (§2/§17)**. **In substance, the architecture refactor is done; what's left is hardening and ergonomics, not features.**

---

## 6. Definition of done — remaining for a clean v1 sign-off
1. **V3-7** — add hash-value assertions to the local-model deep-verify test (closes the one test that could mask a hash regression).
2. **V3-1** — wrap the agent read loop so an oversized/deeply-nested frame fails the connection cleanly (cheap; needed before any §17 shared-host use).
3. *(if pursuing §17 shared-host)* V3-2 (block `-A`/`-o *Forward`), V3-6 (token gen + entropy floor).
4. *(polish)* V3-3/V3-4/V3-5 SSH empty-key + handshake fail-closed ergonomics + drop the `auth_state=None` default; migrate the remaining 77 TUI tests off the fixed-sleep helper.

None are blockers for the §13 single-user v1. With V3-7 and V3-1 done, this is a defensible, shippable v1 across all four specs.

> **No code was modified in this review** (per instruction). This report is the only output written to the repo. **Caveat:** HEAD advanced from `aa497e0`→`b3e728f`→`1f473c7` during the review (live commits); findings are pinned to that window and a couple of just-landed SSH-hardening commits may already address V3-2/V3-3.
