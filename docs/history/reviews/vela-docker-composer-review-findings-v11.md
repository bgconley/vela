# Vela — Review v11: Punchlist Tail Close-Out (essentially v1-complete) — 2026-06-07

**Method (round 11):** ground truth at HEAD `a57b711` → 6 **Sonnet 4.6** finders (adversarial "did the round-10 closeouts land correctly, without over-correction?") → **Opus 4.8** independent verification (re-read at live HEAD; repo advanced `a57b711`→…→`81a96b9` during the audit) + completeness → **Opus 4.8** synthesis (this doc), with my own corroboration of the load-bearing calls.
**Ground truth:** **~948–949 tests pass deterministically in an isolated run** (the round-10 "2 failures" were confirmed concurrency flakes), **ruff clean**, **crown-jewel clean**, **no safety-invariant regression**.
**Workflow stats:** 12 agents, 1.23M tokens, ~23 min. **Domain status: 1 done, 5 substantially-complete.** Verified findings: **2 high / 4 medium — all positive confirmations** / 15 low / 36 info (50 confirmed, 7 adjusted, **0 refuted-as-real-problem**). Zero new high/medium *defects*.

---

## 0. Verdict: the two round-10 mediums are genuinely fixed (no over-correction); the architecture is essentially v1-complete

Every round-10 finding was addressed **correctly**: the BW-04 over-block is fixed *without under-blocking*, B1 probe coverage is closed, diagnose gained real GPU/CUDA/driver/active-build probes, **A11 (ipc/shm) was also fixed** (it had been deferred), the **A14 hardware validation now has authentic FP8 + BF16 `BACKEND_EVIDENCE_OK` runs on a real Blackwell GPU**, and the **A15 restart-lane gate is done and proven on hardware**. There are **no high or medium open correctness issues.** The remaining tail is entirely low-severity polish + one housekeeping item.

**Architecture completion by track:**
| Track | r10 | **r11** | Note |
|---|---|---|---|
| **Core engine (A)** | ~96% | **~99%** | A1–A13/A15 done; **A11 now fixed**; A14 hardware-validated (only wrapper-retirement housekeeping + the `active_model` stub remain). |
| **Onboarding (B)** | ~90% | **~95%** | bootstrap/doctor/setup-ssh/token-push/config-edit/build-doctor/install + diagnose GPU/CUDA/driver/active_build all real. Remaining: `active_model` stub, B11 launch affordance (P4), the 5th auth-state test. |
| **TUI breadth (C)** | ~95% | **~96%** | Only C3 non-active connection dots (polish). |

**Overall: ~96–97% to a polished v1** (from ~93–95% at r10). No high/medium open issues — the gap is low-severity polish + the wrapper retirement.

---

## 1. The round-10 closeouts — all landed correctly

**BW-04 fix (`db53f2f`) — DONE (95%), faithful, no over-correction (corroborated end-to-end).**
The guard moved to run **after** `_merge_overrides` (`composer.py:436`) and evaluates the **resolved** config via `_config_uses_fp8_runtime_shape` (`:1097-1105`), which keys on the *effective* `--kv-cache-dtype` (extra_args, both space and `=` forms, last-wins) → `engine.kv_cache_dtype` → and only falls back to the model-name/quant heuristic when no explicit runtime shape is set. **Verified both directions:** an FP8-named model + `kv_cache_dtype: bfloat16` override + digest-pinned image now **composes** (the round-10 false-positive is gone), while a genuine recipe-less FP8 shape (structured, passthrough, or name+quant) still **hard-blocks** — including a conflicting structured-bf16 + passthrough-fp8, which fails safe to BLOCK. The guard correctly scopes to the **KV-cache dtype** (the actual footgun), not weight quantization, so FP8-weights + BF16-KV is correctly allowed per FR-C4. *Minor:* the `extra_args` passthrough bypass path is correct but lacks a dedicated test (the Opus pass exercised it manually); the two FP8 heuristics (composer predicate vs evidence-checker) weren't consolidated (the round-10 "ideal," not the mandate).

**B1 probe coverage (`e861685`) — DONE.** The two previously-untested paths now have genuine positive-resolve tests asserting `source=='user-venv'` / `'python-module'` and exact absolute paths, backed by a **real harness fix** (`fake_ssh.py` now dispatches per-candidate with independent presence env vars — the round-10 limitation that one path served any candidate is gone). All 4 discovery paths tested.

**Diagnose completeness (`6214cd5`, `7a80147`) — substantially done.** `_diagnose` now adds real, bounded (2s-timeout, graceful-None) probes: GPU architecture (NVML/nvidia-smi product-name inference), a 4-step CUDA-toolkit fallback, a real `nvidia-smi` driver fallback (was env-only), and `active_build` (via the builds registry). All agent-side (crown-jewel intact). *Gap:* `active_model` is a hardcoded `None` stub (test-asserted) — closeable cheaply via the already-wired `discover_active_sidecars` seam.

**A11 (`docker_runtime.py:73-77`) — fixed** (bonus; was deferred at r10): the computed `_default_shm_size` is now suppressed when `ipc_host=True`; an explicit `docker.shm_size` is still honored. Matches the punchlist recommendation.

---

## 2. A14 hardware validation — authentic (corroborated)

The committed FP8 + BF16 Blackbird native-docker artifacts are **genuine on-device runs with a real backend-evidence PASS**, not READY-only records:
- **FP8** (e.g. `…fp8-63b73b3.md`): a full `docker run -d` with the real Blackwell recipe (`FLASHINFER_CUDA_ARCH_LIST=12.0f`, `--kv-cache-dtype fp8`, `--attention-backend FLASHINFER`, `--kv-cache-memory-bytes 64424509440`, pinned `@sha256:b13d6e5f…`), `READY @10.25.0.51:18003 run_id=8ce9fa2e…`, **`BACKEND_EVIDENCE_OK`**, `Exit 0` — plus `DAEMON_RESTART_LIVE_RUN_OK` and `DISCONNECT_RECONNECT_RESUME_OK` (real reattach/resume with inode+offset). `VLLM_API_KEY='••••'` (scrub intact in the artifact).
- **BF16**: two authentic runs (`d667b75e`, `35ca79c6`, Exit 0, OK).
- Distinct PIDs/ports/run_ids across runs; embedded commit hashes match git history → not templated/placeholder.
- The FP8 gate **fail-closes** under `set -euo pipefail` (strictly requires Cutlass-FP8 + FLASHINFER, forbids MARLIN), and is **exhaustively unit-tested** for accept + the full reject matrix — so `BACKEND_EVIDENCE_OK` on hardware is meaningful. **945 tests also pass on the real Blackwell box.**

**A15 restart-lane gate (`66d657c`) — done + hardware-proven.** The resume lane now runs `backend_evidence_check.py` after a structured `REAL_MODEL_DAEMON_RESTART_OK run_id=` parse (fail-closed exit-35 on empty), under `set -euo pipefail`, and an artifact shows it passing end-to-end on Blackbird. (Both the mandatory run_id-parse hardening and the *optional* gate-on-restart are done.)

**Backend gate — BF16 shape (`63522cd`) + stopped-run artifacts (`63b73b3`):** the BF16 rule validates shape-only and correctly **does not false-fail** on absent FP8 Cutlass evidence; the FP8 gate is unchanged (no regression). Stopped-run artifact reading replaces a brittle tail/reattach approach.

---

## 3. Remaining gaps — all low / process / polish (no high/medium defects)

- **[LOW/process — the one clear unmet acceptance line] A14 wrapper retirement.** `scripts/blackbird_qwen36_vllm_foreground.sh` + `…bf16_vllm_foreground.sh` still exist. *Nuance:* the bf16 wrapper filename is referenced by a `migrate_wrapper_config` test fixture, so retirement = archive **+** fixture update, not a pure `rm`.
- **[LOW] B4 `active_model` stub** — `_diagnose_active_state` hardcodes `model: None` (active_build is real). Cheap to close via `discover_active_sidecars`.
- **[LOW/P4 deferred] B11 TUI bootstrap affordance is display-only** (renders the command; doesn't launch); the push affordance sends no `overwrite` (re-push raises `config-exists`).
- **[LOW] B10 5th auth state** — all 5 reachable, 4 have doctor tests; only `required+provided` lacks an end-to-end test. *(The finder's "2 states absent" was refuted — `required+missing`/`mismatch` exist, derived controller-side.)*
- **[LOW] C3** non-active registry targets show a static `○` dot (only the active target gets a live dot).
- **[LOW/INFO, spec-faithful] BF16 gate is config-shape-only** (no positive runtime log evidence — weaker assurance than FP8, but the punchlist only required shape for BF16); and **F8:** an *unregistered* BF16 Blackbird config silently `SKIPPED`s the gate (the A3 fail-open guard was extended to FP8 but not BF16) — **zero blast radius today** (the one BF16 config is registered).
- **[LOW] Two FP8 heuristics not consolidated** (composer's kv_cache_dtype-keyed predicate + the evidence-checker's shape-based one) — the round-10 "ideal," not the mandate. Keep in sync.
- **[LOW] A15 "documented"** — the run_id structured-parse contract isn't written up in `docs/`.

---

## 4. Verification value this round
50 confirmed / 7 adjusted / **0 refuted-as-real-problem**. The Opus pass **refuted** the finder's overstated B10 claim ("2 auth states absent" — they exist, derived controller-side), **strengthened** the A14/A15 verdicts by crediting the behavioral reject-test suite (the durable, CI-runnable authenticity proof the finders under-cited), and tracked the live HEAD past the finders' stale snapshot. The headline calls (BW-04 correct, A14 authentic, A11 fixed) I independently corroborated.

---

## 5. Recommendation
The architecture is **essentially v1-complete**. To close the last ~3–4%, in priority:
1. **A14 wrapper retirement** — archive the two foreground wrappers + update the `migrate_wrapper_config` fixture path. (The single clearest unmet acceptance line.)
2. **`active_model`** in `_diagnose` (cheap, seam exists) + the `required+provided` auth-state test.
3. **Optional hardening:** extend the unregistered-config fail-closed guard to BF16 (close F8); consolidate the two FP8 heuristics into one shared override-respecting predicate; add the `extra_args` BW-04 bypass test; document the run_id contract.
4. **Deferred/polish:** B11 launch affordance (P4), C3 live connection dots.

**Bottom line:** across rounds 9–11 the coder executed a 35-item punchlist **faithfully, with real tests, on-hardware Blackwell validation, and zero regressions**, and resolved every review finding **correctly without over-correcting**. The new architecture is **~96–97% to a polished v1** with **no high or medium open issues** — the remainder is low-severity polish and one housekeeping retirement.

*Snapshot 2026-06-07, HEAD advanced to `81a96b9` during the audit (working tree effectively clean). Read-only review — no code modified, no git actions taken.*
