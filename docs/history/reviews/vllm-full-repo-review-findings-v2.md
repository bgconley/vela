# vLLM Loader — Progress Re-Evaluation (cross-spec) **v2**

**What this is:** a re-review of the agentic coder's progress since findings **v1**, measuring how faithfully the punchlist was closed and how close the (new) agent/controller architecture is to done.
**Baseline → HEAD:** `c20d6c1` → `aa497e0` — **17 commits, +3,119 / −278 across 35 files** (engine/agent/transport/tui + ~+1,640 lines of tests).
**Method:** 4 parallel **Sonnet 4.6** verifiers (Medium P-items, Low Q-items + UI, tests/CI, new-bug/spec-drift), then **Opus 4.8** independently re-read every load-bearing claim. `[Opus-verified]` = I read the code; `[Opus-corrected]` = I re-rated an agent finding.
**Test state:** suite grew **580 → ~634 (+~50 regression tests)**, **0 skips**, and is green on most runs — **but not deterministically** (see **N6**): a clean full run produced **633 passed + 1 intermittent failure** (`test_build_manager_keeps_create_form_open_on_uv_precheck_failure`), which passes in isolation and on re-run. Pass counts also vary run-to-run (627/631/633). Ruff: **1 trivial new violation** (was clean). All five core safety invariants **HELD**.

---

## 0. Headline

**The coder executed the v1 punchlist with discipline and spec-fidelity.** Of 21 itemized findings, **~20 are genuinely closed with dedicated, substantive regression tests** — verified by reading the code, not the commit messages. Core invariants (crown-jewel, scrub-before-wire, live-run remove guard, identity verify, frame limits) all held. This is high-quality, spec-faithful progress, not commit-message theater.

The trajectory moved the estimate up materially:

| Domain | v1 review | **now** | Δ |
|---|---|---|---|
| Canonical core engine (v2) | ~88% | **~90%** | health/redaction/perms |
| Build management (v1) | ~75% | **~88%** | P6/P7/P8 + Q2/Q8/Q12 closed |
| Model management (v1) | ~80% | **~84%** | P3/P4/P5 closed, but P5 introduced a High bug |
| **Agent/controller architecture (v1)** | ~85% | **~93%** | P1/P2/P9 + Q1/Q9/Q11 closed |
| **Overall, to a polished v1** | ~83–86% | **~89–91%** | |

**But the 17 commits also introduced one new High bug (deep-verify OOM) and a few Med/Low issues** — so this is "much closer, with a short fresh punchlist," not "done."

---

## 1. v1 punchlist — closure status (Opus-verified)

### Medium (P1–P9): **9 / 9 FIXED**
| Item | Status | Evidence | Test |
|---|---|---|---|
| **P1** `-32002` on recycled-PID stop/kill | ✅ FIXED **[Opus-verified]** | `local.py:1077-1085, 1090-1099` catch `TrackedProcessMismatch` → `TargetCallError("identity-verification-failed")` → `-32002`; catch is narrowly scoped | `test_rpc_framing` asserts wire code `-32002`; `test_agent_client` asserts named code |
| **P2** large-frame (64 KB–2 MiB) silent drop | ✅ FIXED **[Opus-verified]** | `FRAME_STREAM_LIMIT = MAX_FRAME_BYTES+1` applied at **all 6** reader sites (`stdio:287`, `socket:52,109`, `transport/socket:128`, `transport/subprocess:72`); malformed frames now surface `parse-error`/`agent_error` | ⚠️ test asserts the `limit=` kwarg, **not** a real >64 KB byte round-trip (see N4) |
| **P3** model-download `0600` log via LogSink | ✅ FIXED | `local.py:1962` `LogSink(_model_download_log_path,…)`; `0600`; `downloads/<entry_id>.log` | mode + content asserted |
| **P4** HF_TOKEN injected at spawn (gated) | ✅ FIXED | `model_registry.py:64-70` `env_contribution()` reads `os.environ["HF_TOKEN"]`; merged at `local.py:736`, serialized to detached payload + scrub list | attached path asserted (detached shares the prepare-launch injection point) |
| **P5** `--deep` HF verify blob-hash | ✅ FIXED (⚠️ see **N1**) | `_hf_model_integrity_payload` real per-blob SHA-256 | strategy + blob hashes + mismatch asserted |
| **P6** pre-launch build-integrity re-check | ✅ FIXED | `build_registry.py:89-155` `check_build_launch_integrity` (status + executable hash), called in prepare_launch + preflight | broken-build-blocked asserted |
| **P7** BuildErrorKind CUDA/arch/OOM classes | ✅ FIXED | `local.py:207-240` adds `torch-cuda-mismatch`/`driver-too-old`/`arch-mismatch` (incl. `cutlass_moe_mm_sm100`)/`compile-oom` | parametrized over all four |
| **P8** stale-`creating` startup sweep | ✅ FIXED | `build_registry.py:158-189` `sweep_stale_creating_builds`, called in `LocalAgent.__init__` | demote + leave-locked-alone asserted |
| **P9** registry handlers off the event loop | ✅ FIXED | `local.py:393-421` wraps 13 registry handlers in `asyncio.to_thread`; `stdio.py` awaits awaitables | 8/13 handlers in the to-thread test (rest wired identically) |

### Low (Q1–Q12): **11 FIXED, 1 not targeted**
`Q1` API_KEY_AUTH kind + DEGRADED-recovery ✅ · `Q2` adopt version-agreement ✅ · `Q3` runs dir `0o700` ✅ · `Q4` private-atomic `0600` temp ✅ · `Q5` `PermissionError`→mismatch ✅ · `Q6` redaction delimiter-safe regex ✅ · `Q7` Model `Enter`-select / Build `F`-flags / Help `b m F` ✅ · `Q8` active-build remove repoints default ✅ · `Q9` `subscribe all?` + single-shot `health` ✅ (the `-32601` vs `-32011` nit unchanged — defensible) · `Q11` systemd `After=network.target` removed ✅ · `Q12` profile cache mtime/size-keyed + `clear_profile_caches()` ✅ · **`Q10` typed sidecar model fields / formal enums — NOT targeted** (lowest-priority polish; still uses `config_snapshot` dict).

**UI / Figma:** three real commits — a surface-token system in `theme.py`, a status-badge anatomy split (`#status-dot`+`#status-label` with per-phase surfaces), and a custom text-based progress track (`━/─/│`, 72-wide, hiding the stock `ProgressBar`). Targeted and on-spec, not screenshot theater. Header still merges `▣build`+`M model` into one fixed-width widget (long-name overflow risk persists — cosmetic).

---

## 2. New findings introduced by the 17 commits (a short fresh punchlist)

### N1 — `verify --deep` reads entire weight shards into RAM → OOM **[Opus-verified, High]**
`engine/model_registry.py:1468` `data = path.read_bytes()` then `digest.update(data)` — each blob is fully materialized before hashing (same pattern for local models at `:1587`). On realistic models (this lab's Qwen3-32B FP8 ≈ 62 GB; a single safetensors shard is multiple GB) `verify --deep` will balloon the agent's heap and likely OOM — failing on exactly the large models people most want to verify. **Off the hot launch path (opt-in op), but feature-breaking on real inputs. Fix:** stream the file in fixed chunks (`while chunk := f.read(1<<20): digest.update(chunk)`) instead of `read_bytes()`.

### N2 — New named errors not in the RPC code map **[Opus-verified, Low-Med]**
`build-integrity-failed` (P6), `cancelled`, and `profile-error` are absent from `transport/rpc_errors.py:ERROR_CODE_BY_NAME`, so they go on the wire as `-32000` (internal-error). The string is preserved in `data.target_error_code` and the client recovers it, so behavior is correct — but a client reading only the numeric code sees a generic internal error (the same gap class P1 just fixed for identity). **Fix:** give `build-integrity-failed` (and ideally `cancelled`) their own codes, mirroring P1.

### N3 — Ruff regressed (was clean) **[Opus-verified, Low]**
`ruff check .` → 1 error: `transport/socket.py:1 I001` (unsorted import block), auto-fixable. The tree was ruff-clean at v1; a commit slipped this in (the cerebrum even has a do-not-repeat on I001). **Fix:** `ruff check --fix` + add ruff to the pre-commit/CI gate.

### N4 — P2 regression test is structural, not a real large-frame round-trip **[agent-reported, Low]**
The frame-limit tests assert `asyncio.StreamReader(limit=…)` is constructed with the right constant, but **no test pushes a >64 KB (ideally ~1 MiB) payload through the reader and asserts byte-perfect decode.** A future regression in large-payload decode would pass. **Fix:** add a real round-trip test near the 1 MiB boundary.

### N5 — Client-side malformed frame fails ALL pending RPCs **[agent-reported, Low-Med design]**
`transport/subprocess.py:~217` / `transport/socket.py:~226`: one malformed inbound frame calls `_fail_pending(...)` (rejects every outstanding future) + publishes `agent_error`. Defensible (a corrupt frame means the stream is unsynchronized) and asymmetric-by-design with the server (which sends `parse-error` and continues), but a single noisy byte drops all in-flight callers rather than one. Worth a deliberate decision/comment.

### N6 — Test suite is intermittently flaky (not deterministically green) **[Opus-verified, Med — test health]**
A clean full run (deterministic file-order; pytest-randomly is **not** installed) produced **1 failure** — `test_tui_smoke.py::test_build_manager_keeps_create_form_open_on_uv_precheck_failure` ("create build form did not reopen with uv precheck error") — while the **same test passes in isolation** (`1 passed in 0.65s`) and the **same full suite passed on re-run**. Pass counts also drift run-to-run (627/631/633). The test uses a self-contained fake client, so this is **not** a create-build product bug — it's **Textual/asyncio test-isolation/timing fragility** (the deferred form-reopen assertion races under full-suite load; same family as the agent-observed "first-run 3-test flake," a pytest-asyncio event-loop-init artifact). **Why it matters:** the +50 regression tests are the safety net for all the punchlist fixes; a flaky suite produces spurious red/green and can mask a real regression. **Fix:** stabilize the timing (await the reopen via `pilot.pause()`/explicit wait rather than a bare assert), pin `asyncio_default_fixture_loop_scope`, and consider adding `pytest-randomly` to *surface* isolation leaks deliberately rather than hide them.

### Also still open from v1 (unchanged, as expected)
- **Q10** typed sidecar model fields / formal `BuildPhase`/`DownloadPhase` enums (lowest-priority polish).
- **`pip --python` uv-less fallback** still **unexercised on a real uv-less host** (valid on pip ≥23.1; worth one real run).
- Broader Figma widget-anatomy (log columns, GPU memory tracks) — never in the Q-scope.

---

## 3. Scrutiny corrections (for accuracy)
- **"probe_loop never exits on post-READY auth error" → DISMISSED [Opus-corrected].** `health.py:103-137`: a post-READY `error_kind` deliberately emits DEGRADED once and keeps polling (repeats suppressed via `last_ready`), which **is** the FR-18 DEGRADED↔READY recovery; only pre-READY auth errors return terminally. Working as designed.
- **Test count:** my isolated run = **627**; a concurrent-load run = **631**; an agent observed a **first-run 3-test flake** (pytest-asyncio event-loop init artifact) that passes warm. The exact number is mildly sensitive to concurrency; **0 failures** in every run is the load-bearing fact.

---

## 4. How close to done is the new (agent/controller) architecture?

**~93% of its v1 scope — essentially feature-complete and well-tested.** Every functional surface the spec's MVP (PA0–PA5) and full-v1 (PA0–PA7) call for is implemented and now hardened: uniform `TargetClient` over local-socket + SSH-bridge transports; NDJSON-RPC with correct framing/limits/idempotency/response-priority; **agent-side verify-before-signal now returning the spec's `-32002`**; warm `seq`-buffer + offset-fallback resume; daemon (socket `0700/0600`, `SO_PEERCRED`, `agent.json`, auto-spawn, systemd) ; targets registry + full controller UX; builds/models over RPC with scrubbed job streams; **and real P620→Blackbird validation already proven in self-hosted CI**. The remaining agent-arch deltas are small: **N2** (a couple of error codes), **N5** (malformed-frame blast-radius decision), **N4** (one real large-frame test). Everything else the spec defers to "future" (HTTP/WS/gRPC transports, multi-host runs-overview, per-connection capability-token auth) is **explicitly out of v1 scope (§2/§17)** — not a gap.

**Net:** the architecture refactor is **done in substance**; what's left is bug-fix + a thin polish/test layer, not new subsystems.

---

## 5. Definition of done — remaining for v1 sign-off
1. **N1 (High)** — chunk the deep-verify hashing so `verify --deep` doesn't OOM on real models. *(the only product must-fix)*
2. **N6 (Med — test health)** — stabilize the flaky TUI test and gate a **deterministic** green suite; it's the safety net guarding every fix above.
3. **N2 / N3 (Low-Med)** — add wire codes for `build-integrity-failed`/`cancelled`; `ruff --fix` + gate ruff in CI.
4. **N4 (Low)** — one genuine >64 KB frame round-trip test.
5. **N5 (Low)** — decide/annotate the malformed-frame fail-all behavior.
6. *(optional)* Q10 typed sidecar fields; one real uv-less pip-fallback build; the header long-name overflow.

With N1 fixed and N2–N4 mopped up, this is a defensible, shippable v1 across all four specs.

> **No code was modified in this review** (per instruction). This report is the only output written to the repo.
