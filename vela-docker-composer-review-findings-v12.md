# Vela — Review v12: Final v1-Done Verification (coder reports done) — 2026-06-07

**Method (round 12):** ground truth at HEAD `f7e61ae` → 6 **Sonnet 4.6** finders **validating the coder's own completion audit** (`vela-v1-completion-audit-2026-06-07.md`) → **Opus 4.8** independent verification (re-read code/tests/artifacts; did not trust the finders) + completeness → **Opus 4.8** synthesis (this doc), with my own corroboration of the load-bearing calls.
**Ground truth:** **954 tests pass** in an isolated run (matches the audit's count exactly), **ruff clean**, **crown-jewel clean**, **0 skip/xfail markers**, **no safety-invariant regression**.
**Workflow stats:** 12 agents, 1.15M tokens, ~40 min. **All 6 domains: done-with-justified-deviation (97–98%).** Verified findings: **7 high / 6 medium — all positive confirmations** / 16 low / 54 info (78 confirmed, 5 adjusted, **0 refuted-as-a-real-problem**). **Zero high/medium defects.**

---

## 0. Verdict: the v1-done claim is independently validated — and the coder's audit is honest (it even *undersells* one item)

Unlike round 8's "complete" overclaim, the coder's done-claim is **genuine, bounded, and accurate.** Every domain came back **done-with-justified-deviation**, and the Opus pass — searching adversarially for round-8-style failure modes (scaffolds, hollow acceptance tests, spec-MUSTs quietly scoped away) — **found none.** The completion audit's specific "Closed" claims were cross-checked against code (6+ per domain) and hold. The headline acceptance test is real. The two open items are **disclosed deviations with sound rationale**, and one of them (B11) is actually *more* done than the audit claims.

**Final completion by track:**
| Track | r11 | **r12** | Verdict |
|---|---|---|---|
| **Core engine (A)** | ~99% | **~97%** | done-with-justified-deviation (A14 wrapper-retirement literal line) |
| **Onboarding (B)** | ~95% | **~98%** | done-with-justified-deviation (B11 overwrite-modal, P4 — affordance present) |
| **TUI breadth (C)** | ~96% | **~98%** | done (acceptance flow genuine) |

**Overall: ~98% to a literal polished v1; functionally v1-DONE for the declared scope** (proven lab recipes + the punchlist workflows). There are **no high or medium open issues.**

---

## 1. What the tail closed (round-11 remainder) — all genuine, all tested

- **`active_model`** (round-11's one stub): was a hardcoded `"model": None`; `f715e74` replaced it with `_diagnose_active_model(self._verified_live_sidecars())` — a real discover + process-verify (`verify_sidecar_from_system`) + load pipeline, with the test flipped from `assert … is None` to asserting the full sidecar dict. **HIGH confirmation.**
- **F8 BF16 unregistered-config fail-open** (round-11 forward-risk): closed — `_looks_like_blackbird_bf16_config` (`backend_evidence_check.py:216`) wired into the unregistered-rule branch (`:105`); a renamed BF16 Blackbird config now raises `BackendEvidenceError → exit 2` instead of silently `SKIPPED → exit 0`. Tested (`test_remote_workflow.py:851`).
- **C3 live connection dots** (round-11 polish): non-active registry targets now get a genuine ephemeral per-target connection probe (`new_deployment.py:633-648`), not a static `○`.
- **B10 5th auth state** (`required+provided`): now has a real end-to-end fake-SSH test (`test_ssh_discovery.py:478`), completing all five states.
- **A15 run_id contract documented** (`9d0910a`); **A11** has positive + negative shm tests.
- **The headline acceptance test** `test_new_deployment_build_pin_and_smoke_acceptance_flow` (390 lines): **genuinely behavioral** (corroborated myself) — drives the real Textual wizard create-build → pin-HF → download-now → compose → save → bounded-smoke → READY → `Phase.STOPPED`, with real RPC handlers, exact param assertions (`runtime=={'kind':'build','build':'nightly-cu130-sm120'}`, `model_ref`, `revision`), a strict `save<prepare<launch<probe<stop` ordering assertion, and a catch-all `unexpected target client call` guard. Not hollow.

---

## 2. The 2 disclosed deviations — both reasonable

- **A14 foreground-wrapper retirement [LOW, disclosed].** The punchlist literally said "retire or archive the wrappers"; both `scripts/blackbird_qwen36_*_vllm_foreground.sh` are **retained** (annotated "Reference-only"). The coder's rationale — they are the Blackwell **runtime-authority provenance** — is consistent with the recipe-led philosophy from rounds 7–8. *Nuance the Opus pass corrected:* archiving them would **not** break the migration map (it keys on the bare basename via `Path(executable).name`), so this is trivially closable if a literal 100% is wanted — but keeping them as human-readable provenance is a defensible, disclosed choice. The substance (native docker is the lane, hardware-validated FP8+BF16) is done.
- **B11 TUI bootstrap/push affordance [P4, deferred].** *The Opus pass found the audit UNDERSELLS this:* the "Bootstrap target…" / "Push config…" affordances are **present, wired into the command palette, and smoke-tested** — meeting B11's literal punchlist acceptance. Only the **overwrite/confirm modal** refinement is missing (a re-push of an existing config raises `config-exists`). A conservative understatement in the coder's favor — the opposite of an overclaim.

---

## 3. Residual low/info notes (none blocking; for a future hardening pass)

- **[LOW] BF16 unregistered-detector is narrower than FP8** — it requires the exact pinned image digest, so a renamed BF16 recipe that *also* swaps the image would still skip the gate. Defensible (a different image isn't the proven recipe) but asymmetric with the FP8 detector.
- **[INFO] `active.model` reports only the FIRST live sidecar** in a multi-sidecar scenario (loop-with-early-return) — fine for the single-active-deployment lab model; informational doctor output, not a safety gate.
- **[LOW] `DOCTOR_ACCEPTANCE_OK` marker** is an unsaved one-off (unlike the committed bootstrap-acceptance artifact) — an evidence-traceability nit; the doctor behavior is covered by a real fake-SSH test.
- **[LOW/test-harness] One daemon-spawn test** (`test_unix_socket_target_client_auto_starts_missing_socket_daemon` / `test_command_palette_reattaches_detached_run`) flakes **under concurrent load** (passes in isolation; 954 green deterministically when run alone) — the same non-hermetic class seen in rounds 10–11. Real-daemon-spawn tests should be serialized/marked; **not a product defect.**
- **[INFO] FP8 heuristic consolidation** (v11's "ideal, not mandate") + the `extra_args` BW-04 bypass test — code-quality niceties, no correctness impact.

---

## 4. Verification value this round
78 confirmed / 5 adjusted / **0 refuted-as-a-real-problem.** The Opus pass corrected several *finder* inaccuracies (the finder wrongly said app.py was unchanged — it gained 102 lines in C3; the finder overstated the wrapper-archival obstacle; the finder under-credited B11) while confirming the substantive verdict. Crucially it **hunted for and found no undisclosed gaps** beyond the 2 disclosed deviations. I independently corroborated the load-bearing calls: 954 green (ran it), the acceptance test is genuine (read its RPC handlers + assertions), the audit is honest (read it in full), `active_model`/F8/C3 wired (code), wrappers retained (`ls`).

---

## 5. Final read

**Across rounds 6–12, the coder took a 35-item punchlist from concept to an independently-verified, hardware-validated v1** — executing it faithfully, with **real behavioral tests**, **on-hardware Blackwell FP8+BF16 backend-evidence validation**, **zero regressions**, resolving **every** review finding **correctly without over-correcting**, and writing an **honest self-audit that undersells rather than overstates**. The four safety invariants hold throughout; the crown-jewel boundary is clean; the recipe-led runtime-authority boundary is enforced in code + docs + the gate.

**This is a genuine v1-done** for the declared scope, at **~98% to a literal polished v1**. The only gap to a literal 100% is the **A14 wrapper retirement** (a disclosed, deliberate, trivially-closable deviation) plus a handful of **low/info hardening nits** (BF16-detector breadth, the B11 overwrite modal, the daemon-spawn test serialization, multi-sidecar `active.model`). **No high or medium open issues remain.** If the team wants a literal 100% sign-off: archive the two wrappers (updating `deployments.md`'s note), add the B11 overwrite-confirm modal, serialize the daemon-spawn tests, and widen the BF16 detector — none of which are v1-blocking.

*Snapshot 2026-06-07, HEAD `f7e61ae` (working tree clean). Read-only review — no code modified, no git actions taken.*
