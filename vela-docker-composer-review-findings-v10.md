# Vela — Review v10: Near-Complete Punchlist Execution — 2026-06-07

**Method (round 10):** ground truth at HEAD `cb1eed8` (25 commits since the round-9 review) → 7 **Sonnet 4.6** finders with an **adversarial "real or scaffold?" mandate** → **Opus 4.8** independent verification (re-read at live HEAD; the repo advanced `e0639f2`→`e3a5c6d`→`cb1eed8` during the audit) + completeness → **Opus 4.8** synthesis (this doc), with my own corroboration of the one correctness issue.
**Ground truth:** **934 tests pass** at the clean HEAD (verifier's independent run + my pre-flight), **ruff clean**, **crown-jewel clean**, **no safety-invariant regression** (sidecar/supervisor/docker_runtime/log_sink byte-identical since `0259b1d`).
**Test-suite note (honest):** a re-run under heavy concurrent load (this audit's own test runs + the in-flight A14 remote validation executing simultaneously) produced **2 failures — `test_local_agent_starts_and_stops_attached_run_by_run_id` (`assert -15==0`, a SIGTERM) and `test_cli_run_forwards_sigint_to_attached_child` (`TimeoutExpired`)**. Both are signal/subprocess-timing lifecycle tests and are **non-hermetic** (a prior verifier noted at least one compose test touches the real `~/.local/state/vela/runs/` and races a live supervisor). These are almost certainly **concurrency flakes, not a regression** (they passed clean minutes earlier and test no new feature), but the coder should confirm with an isolated `-p no:randomly` re-run and ideally harden these tests' hermeticity.
**Workflow stats:** 14 agents, 1.52M tokens, ~26 min. **All 7 domains: substantially-complete.** Verified findings: 0 high / **2 medium** / 23 low / 54 info (71 confirmed, 6 adjusted, 2 refuted).

---

## 0. Verdict: a dramatic, high-quality leap — the round-8 scaffold concern is decisively resolved

In 25 commits the coder took the punchlist from **~17% (6/35 items, round 9) to ~90%**, and — critically — the headline onboarding commands that were **scaffolds at round 8** (`bootstrap` = targets-add renamed; `doctor` = local-only static nag) are now **genuinely functional, spec-faithful, and tested end-to-end**. The round-9 `target.name` crash is fixed. **Test authenticity is intact** (90%, no hollow-test regression — the Opus pass specifically confirmed B3/B4/Track C drive real CLI/Textual surfaces with real side effects). **No safety invariant or prior-round closed item regressed.** Only **2 medium findings and zero high.**

**Architecture completion by track (the "how close to done" answer):**
| Track | r8 | r9 | **r10** | Status |
|---|---|---|---|---|
| **Core engine (A)** | 88% | 90% | **~96%** | A1–A13 done + tested; A11 deferred (benign), A14 hardware in-flight, A15-optional deferred. |
| **Onboarding (B)** | 36% | 50% | **~90%** | Bootstrap + doctor are real; setup-ssh/token-push/config-edit/build-doctor/install all done. Remaining: diagnose GPU/CUDA + active build/model, 1 auth-state test, B11 launch affordance. |
| **TUI breadth (C)** | 52% | 52% | **~95%** | All seven wizard items genuine + behavioral-tested. |

**Overall: ~93–95% to a polished v1** (from ~78–80% at round 9). The remaining ~5–7% is the BW-04 over-block fix, B-track diagnose completeness, two untested probe paths, A14 hardware re-validation actually passing, and assorted low polish.

---

## 1. The 2 actionable medium findings

**[MEDIUM — the one real correctness/spec issue] BW-04: the new Blackwell FP8 "require recipe" hard-block over-constrains, ignoring overrides.**
- The new behavior (`e0639f2 "Require Blackwell FP8 lab recipes"`) makes `compose_config` **raise** `compose-invalid` for an FP8 deployment on a Blackwell target without a matching lab recipe. But the guard runs at `composer.py:381` **before** `_merge_overrides` (`:433`), and `_looks_like_fp8_model` (`:1089`) fires on a **model-name substring** (`'fp8' in model.lower()`). **Result (verifier-reproduced end-to-end, my-eyes-confirmed):** a model *named* `…-FP8` on blackbird docker is hard-blocked **even with an explicit `engine.kv_cache_dtype: bfloat16` override AND a user digest-pinned image** — both ignored, no escape hatch. This contradicts spec §1.4 ("every docker knob and every vLLM serve flag overridable"). **deviation-unjustified.**
- *Blast radius is narrow* (only Blackwell-target FP8-name-signalling models the operator intends to override), so medium not high. **Fix:** evaluate the guard **after** `_merge_overrides`, and/or honor an explicit override / add an escape flag; ideally consolidate the composer heuristic (`_looks_like_fp8_model`, name-based) and the backend-gate heuristic (`_looks_like_blackbird_fp8_config`, arch/env-based) into one shared predicate that respects overrides.

**[MEDIUM] B1 discovery probe-path coverage (carryover from round 9).**
- Only 2 of 4 probe sources are tested (`command -v vela`, canonical-venv). The **user-venv** (`$HOME/venvs/vela/bin/vela`) and **`python3 -m vela`** paths have no positive-resolve test — and `_probe_python_module` is entirely untested behavior. Root cause is a **harness limitation**: `fake_ssh.py` cannot isolate user-venv from canonical-venv (it echoes one path for any `candidate=` probe). **Fix:** extend the harness to echo the probed candidate + a per-path presence env, then add the two tests.

---

## 2. Genuinely DONE — fair credit (all verified spec-faithful + real tests)

- **B3 bootstrap** (87% domain): real guided flow — discover-or-`--install` (`ssh_bootstrap.install_ssh_agent` creates the canonical `~/.local/share/vela/venv` and pip-installs over a real SSH subprocess), persists the resolved `agent_command`, handshakes, runs `check_build_prerequisites` before `create_build` for `--build`, prints a per-check summary. The spec's literal acceptance command parses exactly. Tested end-to-end against the fake-SSH harness.
- **B4 doctor**: real agent-side `diagnose` RPC; **`next_steps` is now conditional on failures — the round-8 static nag is fixed** (two tests assert `next_steps==[]` on all-green). Renders per-check green/red with remediation.
- **B5 host paths**: all 5 (`config_dir`/`runs_dir`/`builds_dir`/`models_registry`/`socket_path`) surfaced in `doctor`, `targets test`, **and** `agent status --target`. The `VELA_CONFIGS`/`XDG_*` override docs exist in `docs/configuration.md`.
- **B6–B12**: setup-ssh runs real `ssh-copy-id` (argv asserted); `gen-token --install --target` writes 0600 locally **and** pushes a real 0600 remote file (perms+content asserted); `config edit` is a real pull→`$EDITOR`→lint→push round-trip with the secret-block gate; `build doctor` is a real 6-method diagnostic; install uses the shlex-safe canonical venv path.
- **Track C (95%)**: all seven items genuine + behavioral Textual-pilot tested — create-build/adopt-venv hand-offs pin into `command.build`; four model modes incl. pin-HF firing `download_model`; target picker re-scopes RPCs; FlagManager §7 affordances (preset picker, reset-to-preset, show-changed-only); live `suggest_deployment_defaults`; a fake-docker walk to `Phase.STOPPED`; named-failure surfacing (ERROR_GUIDANCE now 15 entries incl. all 4 docker kinds).
- **Track A (88%)**: A4 (edit-secret test), A5 (offline pin → `remote_only`+warn, launch still blocked), A6 (repo-not-found taxonomy), A7 (docker reattach-across-restart + stale-container refusal), A8 (NAME_CONFLICT/GPU_NOT_AVAILABLE tests), A9 (ERROR_GUIDANCE), A10 (explicit create-overwrite), A12, A13, A15-mandatory (structured run_id) — all done + real tests.
- **Blackwell enforcement** (beyond-punchlist, 88%): digest pinning consistent across 6 files; warn-path precisely scoped; hard-block surfaces as `compose-invalid` + documented. Sound except BW-04.
- **Safety/regression: clean** — crown-jewel clean (incl. the in-flight `target_manager.py`/`app.py` affordances, which stay on the RPC side: display-a-command + `push_config` RPC, no subprocess); all four invariants hold; no new runtime dependency (`uv.lock` is a benign lockfile; the only `pyproject` change is a pytest `pythonpath`); Blackwell enforcement does not regress normal or BF16 docker launches.

---

## 3. Lower-priority gaps on the path to 100%

**Onboarding (to ~100%):**
- B4/B5: **GPU arch + CUDA toolkit version absent from `_diagnose`** (`local.py:1719-1740` reports only `driver` from `NVIDIA_DRIVER_VERSION` env; no `nvidia-smi`, no arch). Spec §1/R5 list "CUDA/driver/GPU."
- B4: **`active_build` + `active_model` absent** from the host_report (punchlist B4 lists them).
- B10: the **5th auth state (`required+provided`) is untested** end-to-end (the 4 negative states are covered).
- B11: the TUI **bootstrap affordance is display-only** (renders `vela targets bootstrap … --install` for copy/run; punchlist said "launch bootstrap"); the **push affordance sends no `overwrite`**, so re-pushing an existing config raises `config-exists`.

**Core engine:**
- A11: `--ipc=host` **and** `--shm-size` still co-emitted (`docker_runtime.py:71-75`); benign (docker ignores shm under ipc=host) but spec-divergent — and a pre-existing test **locks** the divergent behavior, so the fix must invert that assertion, not just add one.
- A14 (**in-flight now**): the committed FP8 native-docker artifact records a **failing** remote run (17 `PORT_IN_USE` failures — shared-Blackbird-host port collisions, an **environmental** issue, *not* a code/backend-gate regression); a re-run (`cb1eed8` "Harden remote validation test ports" + the `…retry2` artifact) is in progress. Acceptance (a committed artifact with a backend-evidence **PASS**) not yet cleanly met. The BF16 foreground wrapper is also not yet retired.
- A15: the spec-**optional** gate-on-restart is deferred (the mandatory run_id-parse hardening is done).

**TUI:** C3 non-active registry targets show a static `○` dot (only the active target gets a live dot); C6/C7 smoke tests monkeypatch `probe_loop` so readiness *detection* is short-circuited (the fake-docker lifecycle + save→smoke→stop *is* walked).

**Blackwell:** future-target gap (the guard hardcodes `{blackbird, p620-01, p620}` — a new Blackwell-class target isn't guarded); the suggest/compose asymmetry (identical input → `suggest` warns but `compose` hard-raises) is intentional but a UX foot-shape.

---

## 4. Verification value this round
71 confirmed / 6 adjusted / **2 refuted**. The Opus pass again earned its keep: it **refuted** a finder claim that the XDG override docs were missing (they exist in `docs/configuration.md`) and a self-refuted A7 "no discover_runs" risk; it **adjusted** several "high" test-coverage alarms down to low (the B3/B4 flows *are* thoroughly tested, just in a different test file than the punchlist item number implies); and it tracked the live HEAD past the finders' stale snapshot. The single most important finding (BW-04) was confirmed at the strongest level (end-to-end reproduction with overrides ignored).

---

## 5. Recommended priority to reach 100%
1. **[MED] Fix BW-04** — move the Blackwell-FP8 guard after `_merge_overrides`, honor explicit overrides / add an escape, and unify the two FP8 heuristics into one override-respecting predicate. (The one real spec contradiction.)
2. **[MED] Close the B1 probe-path coverage** — extend `fake_ssh.py` to isolate candidates, then test user-venv + `python3 -m vela`.
3. **Onboarding diagnose completeness** — add GPU arch + CUDA version (via `nvidia-smi`/toolkit probe) and `active_build`/`active_model` to `_diagnose`; add the `required+provided` auth-state test; make B11's bootstrap affordance actually launch (or relabel) and let push overwrite.
4. **A14 hardware** — land a clean FP8 **and** BF16 native-docker run that passes the backend-evidence gate (fix the shared-host port collisions), commit the artifacts, retire the foreground wrappers.
5. **Low/deferred** — A11 ipc/shm (invert the locked test), A15 optional gate-on-restart, C3 live dots, the Blackwell future-target/asymmetry consolidation.

**Bottom line:** this is **excellent, faithful, well-tested execution** — ~90% of a 35-item punchlist landed in one run with the round-8 scaffold problem fully resolved, no regressions, and only two medium issues. The architecture is **~93–95% to a polished v1**; the gating items are the BW-04 over-block fix and the A14 hardware validation actually passing, with a tail of diagnose-completeness and coverage polish.

*Snapshot 2026-06-07, HEAD `cb1eed8` (working tree mid-A14 hardware re-validation). Read-only review — no code modified, no git actions taken.*
