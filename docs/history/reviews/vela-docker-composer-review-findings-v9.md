# Vela — Review v9: Punchlist Execution Progress (coder mid-flight) — 2026-06-06

**Method (round 9):** ground truth at HEAD `0259b1d` → 6 **Sonnet 4.6** finders → **Opus 4.8** independent verification + completeness → **Opus 4.8** synthesis (this doc), with my own corroboration of the one actionable bug. The coder is **actively implementing** `vela-v1-completion-punchlist.md`; the repo HEAD advanced *during* the audit (`b21084e`→`f1f57ca`→`0259b1d`), so the Opus pass re-read at the live HEAD and corrected the finders' stale snapshot.
**Ground truth:** **881 tests pass deterministically**, **ruff clean**, **crown-jewel clean**, **no safety-invariant regression** (sidecar/docker_runtime/log_sink byte-identical across the range).
**Workflow stats:** 12 agents, 1.0M tokens, ~20 min. Domain status: 2 done, 1 in-flight-with-issue, 3 in-flight-on-track. 74 confirmed / 8 adjusted / 1 refuted.

---

## 0. Verdict: faithful, high-quality, in-sequence execution — with one in-flight bug to fix

The coder is executing the punchlist **exactly in the recommended sequence** (A1 → A2/A3 → B0 → B1 → B2 → B3-in-flight) and the **work quality is excellent**: every committed item is correct, spec-faithful, and backed by **real behavioral tests** (zero hollow/over-mocked tests found across all six domains), with **no regression** to any safety invariant. The coder is correctly front-loading the **highest-leverage** items (A1 security, the R1 discovery probe, the §3 named-failure remediations).

**One actionable bug** (in-flight B3 work): a `target.name`-on-a-`str` crash at three CLI error handlers.

**Completion: ~17% of the punchlist (6 of 35 software items)** — early but on-track. Architecture by track (below): Core engine ~90%, Onboarding ~50%, TUI breadth ~52% (unchanged).

---

## 1. Done-committed (6 items) — all verified spec-faithful + real tests

| Item | Commit | Status | Notes |
|---|---|---|---|
| **A1** clone secret bypass | `e765e07` | **DONE 100% faithful** | `_clone_config` routes through `validate_config_payload` **post-override** (`local.py:857`), raises `invalid-config` before write (`:875`). Both vectors (override-injection + secret-source) tested asserting the file is **not written**; clean clone writes 0644. All 5 config writers now guarded. Secret-block correctly precedes the config-exists guard. |
| **A2** backend-gate config-shape + FLASHINFER-absent tests | `acb6708` | **DONE** | All 5 `_config_shape_errors` modes fire in isolation (live-verified); the exit-31-equivalent (cutlass-present/FLASHINFER-absent) reject is tested; an accept case passes. |
| **A3** backend-gate fail-open fix | `acb6708` | **DONE** | The config-name cross-check (`backend_evidence_check.py:90-94`) is the **primary production drift guard** (the reattached config always carries a required `ModelConfig.name`); `_looks_like_blackbird_fp8_config` is the secondary backstop. BF16 does not false-fail. |
| **B0** fake-SSH harness | `b21084e` | **DONE** | Genuine PATH-injected stub-`ssh` binary simulating reachable+present / absent (exit 127) / unreachable (exit 255 + stderr) / version-mismatch / install / host_report. Real subprocess tests. Usable foundation for B1–B8. |
| **B1** SSH discovery probe | `f1f57ca` | **DONE faithful** | Probe order matches spec R1 **exactly** (`command -v vela` → canonical venv → user venv → explicit venv → `python3 -m vela`); returns an absolute `agent_command`; cleanly separates resolved / version-mismatch / `AGENT_NOT_INSTALLED`, each with its own code + the exact `vela targets bootstrap <name> --install` remediation; wired into `targets add`/`bootstrap`/`test`; `__main__.py` enables the module probe. The R1 "highest-value" no-PATH-dependency acceptance is genuinely realized. |
| **B2** named-failure remediations | `0259b1d` | **DONE faithful** | `src/vela/remediation.py` — a code-keyed remediation map with **active-target-name interpolation**, wired into **both** the CLI error path and the TUI banner, with unit + CLI-integration + TUI-smoke tests. The spec's exact acceptance string ("SSH auth failed: Permission denied (publickey) — run `vela targets setup-ssh blackbird`") is realized. (This is the §3 "cheapest, highest-leverage" change.) |

**Security note (corroborated):** the new SSH execution paths (`ssh_discovery.py`, and the in-flight `ssh_bootstrap.py` install job) inherit the **hardened SSH option allowlist + `BatchMode`** via the shared `factory._ssh_base_command` builder; the refactor is byte-equivalent for existing SSH targets. **No SSH security regression.** All `subprocess` use stays in `transport/` (crown-jewel clean).

---

## 2. The one actionable bug (in-flight B3 work)

**[HIGH — in-flight, fix before B3 lands] `target.name` on a `str` → `AttributeError` at three CLI error handlers.**
- `cli.py:321` in `targets_bootstrap` (def `:256`), `cli.py:367` in `targets_test` (def `:359`), `cli.py:545` in `build_inspect` (def `:530`) each call `_echo_target_error_or_exit(exc, target_name=target.name)`, but `target` is the `--target` **string** option (`Annotated[str,…] = "local"`). On any `TargetCallError` (unknown config, agent-unreachable, version-mismatch) these crash with `AttributeError: 'str' object has no attribute 'name'`.
- The correct form `target_name=target` is used everywhere else (e.g. `:163`, `:420`, `:425`). **Fix: `target.name` → `target`** at all three sites (the `git diff` shows the bootstrap/test ones are uncommitted B3 work; the `build_inspect` one rode in with B1).
- **Why the green suite misses it:** these are defensive error-paths; happy-path tests don't exercise them. **Add an error-path test** that drives a `TargetCallError` through `targets test`/`build inspect`/`targets bootstrap` and asserts the remediation renders (not a crash).

---

## 3. B1 follow-up gaps (the committed discovery probe — close before calling B1 "100%")

- **[MED] 3 of 5 probe paths untested for positive resolve:** only `command -v vela` and canonical-venv are exercised; user-venv (`$HOME/venvs/vela/bin/vela`), explicit `--venv`, and `python3 -m vela` positive paths have **no** dedicated test (the harness supports them, but echoes `FAKE_SSH_VELA_PATH` for any `candidate=` probe, so closing this likely needs a per-candidate harness extension).
- **[LOW] command-v branch does not assert the resolved path is absolute** — spec R1 requires an absolute `agent_command`; a shell function/alias could yield a non-absolute, broken command. Recommend `startswith('/')` enforcement.
- **[LOW] version-compat is strict string equality** (`ssh_discovery.py:232 version == __version__`) rather than the handshake's protocol-version window the punchlist said to reuse. Bounded (bare `vela --version` emits `__version__`, so same-version works), but over-restrictive.
- **[LOW] no unit test asserts `ssh_command_for_target()` carries `BatchMode=yes` / the denylist** for the discovery path specifically (inherited structurally + exercised e2e; a 2-line unit test would lock the security contract).
- **[LOW]** `targets bootstrap`'s discovery wiring is untested via fake-SSH (the only bootstrap test passes `--agent-command` explicitly, short-circuiting discovery).

## 4. A2/A3 minor notes
- A2's `+33` lines added a **6th** config-shape gate (`--kv-cache-memory-bytes 64424509440`, hardcoded) beyond the punchlist's enumerated 5 — it matches the real FP8 config, so it's correct/stronger, just an undocumented extra. A3's positive cross-check path is only implicitly tested.
- **Process:** `scripts/backend_evidence_check.py` was edited but not added to `.wolf/anatomy.md` (OpenWolf protocol miss; does not affect correctness).

---

## 5. Completion sweep

**Punchlist: ~17% (6/35 software items).** Track A 3/15 (A1,A2,A3); Track B 3/13 (B0,B1,B2); Track C 0/7. In-flight: **B3** (bootstrap flow — `ssh_bootstrap.py` install job being written).

**Not started:** A4 (edit-secret test), A5 (model-pin offline deviation), A6 (repo-not-found taxonomy), A7 (docker reattach-across-restart test), A8–A13 (docker error-kind/ERROR_GUIDANCE/overwrite/ipc-shm/exposure-ok/GC tests), A14 (DK4 hardware), A15 (run_id); **B4 doctor [HIGH]** (still has the "actively misleading" static next-steps nag), B5–B12; **all of Track C (C1–C7) [C1 HIGH]**.

**Architecture by track (the "how close to done" answer):**
| Track | r8 | now | Movement |
|---|---|---|---|
| **Core engine (A)** | 88% | **~90%** | A1 (HIGH security) + backend-gate hardening done; remainder is mostly MED/LOW tests + the offline/taxonomy MEDs. |
| **Onboarding (B)** | 36% | **~50%** | The P1 high-leverage layer landed — R1 discovery probe (the footgun fix) + §3 remediations + harness. The P2 **headline** (B3 bootstrap in-flight, B4 doctor not started) is the bulk of the remaining gap. |
| **TUI breadth (C)** | 52% | **52%** | Untouched (0 items started). |

**Overall: still ~78–80%** against the full enhanced spec — but with strong momentum, correct sequencing (highest-leverage first), and excellent execution quality.

---

## 6. Recommendations
1. **Fix the `target.name` bug** at `cli.py:321/367/545` (→ `target`) and add an error-path test — do this before B3 commits.
2. **Backfill B1 tests** — the 3 untested probe paths + the absolute-path assertion + a `BatchMode` unit assertion — before declaring B1 done.
3. Continue the sequence: finish **B3** (bootstrap full flow incl. handshake summary), then **B4 doctor** (the other HIGH headline — and kill the static next-steps nag), then Track C (C1 HIGH first).
4. Minor hygiene: add the `backend_evidence_check.py` anatomy entry; reconsider the strict version-equality check.

**Bottom line:** the coder is doing **faithful, well-tested, regression-free** work in the right order, ~17% through the punchlist, ~ on-track. The only thing that needs attention right now is the three-site `target.name` crash in the in-flight B3 code.

*Snapshot 2026-06-06, HEAD `0259b1d` (working tree mid-B3). Read-only review — no code modified, no git actions taken.*
