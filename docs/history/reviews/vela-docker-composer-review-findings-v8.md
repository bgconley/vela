# Vela — Review v8: Adversarial Completion Audit ("coder asserts complete") — 2026-06-06

**Method (round 8):** ground truth at HEAD `40858d4` (v1-hardening in commit `443a9e0`) → 7 **Sonnet 4.6** finders with an **adversarial completion mandate** (separate genuinely-functional+tested from scaffolded-but-unwired / hollow-tested) → **Opus 4.8** independent re-read + authoritative `complete/substantially-complete/partial/scaffolded` verdict + completeness pass → **Opus 4.8** synthesis (this doc), with my own corroboration of the two headline calls.
**Scope:** the full enhanced spec set — docker/composer + onboarding + model-management.
**Ground truth:** **845 tests pass deterministically**, **ruff clean**, **crown-jewel clean**, working tree clean of code changes.
**Workflow stats:** 14 agents, 1.12M tokens, 734 tool calls, ~19 min. Domain status: **2 partial, 5 substantially-complete**. Verified findings: 5 high / 12 medium / 13 low / 49 info (71 confirmed, 5 adjusted, 3 refuted). Opus completeness caught 1 high + 3 medium the finders missed.

---

## 0. Verdict: the "complete" claim is an OVERCLAIM — but a narrow, honest-effort one

**The core deploy/runtime/model engine is substantially complete and authentically tested.** The round-7 priorities genuinely landed *with real tests* — the model-pin HIGH is closed and correctly scoped, the secrets-block is real for 4 of 5 writers, the backend-evidence gate is fail-closed for FP8, and **no safety invariant or prior punchlist item regressed** (the hardening commit didn't touch a single safety-critical file). The new tests are **real** — the Opus pass *refuted* three finder claims that tests were "hollow" (the PM2 download FSM, FlagManager wiring, and the compose→smoke pipeline are all genuinely tested).

**But "the whole enhanced spec is complete" does not hold, for three concrete reasons:**
1. **[HIGH, NEW] `_clone_config` bypasses the secrets-at-rest gate** — a real, operator-reachable plaintext-secret leak the claim missed.
2. **Onboarding's headline commands are scaffolds** — `vela targets bootstrap` is `targets add` renamed; `vela doctor` is two local-filesystem checks with a static, *unconditional* next-steps list. ~36% complete.
3. **The TUI primary surface still can't create/adopt a build from the wizard** (F-TUI-2, HIGH). ~52% complete.

**Honest completion: ~80% overall** (Opus meta-estimate), or **~88% if onboarding is scoped as the separate P2 forward track its own spec defines** (the onboarding doc is explicitly "no code written here," with bootstrap/doctor as P2). The 20% gap is concentrated, not diffuse.

---

## 1. Completion scorecard (authoritative Opus per-domain)

| Domain | Claimed | **Actual** | Opus % |
|---|---|---|---|
| Model-pin immutability (the r7 HIGH) | complete | **substantially-complete** | **93** |
| Docker/composer regression + safety | complete | **substantially-complete** (no regression) | **90** |
| Blackwell backend-evidence gate | complete | **substantially-complete** | **83** |
| Secrets-at-rest + config security | complete | **substantially-complete** | **80** |
| "Is it really complete" / test authenticity | complete | **substantially-complete** | **80** |
| TUI primary-surface breadth | complete | **partial** | **52** |
| Onboarding | complete | **partial** | **36** |

---

## 2. Genuinely DONE — fair credit (confirmed real + tested)

- **Model-pin r7 HIGH closed, correctly scoped.** `_validate_model_handoff_prelaunch` (local.py:4367) blocks `commit_sha is None` hf_repo handoffs with `model-unavailable`/`missing-commit`, and **does not over-block** precedence #1 (explicit model+revision) — the guard is structurally unreachable for that path (`resolve_model_handoff(None)→None`); verifier executed both paths to confirm. Pin-time `gated-auth`/`revision-not-found` now raised; the bare `except Exception: return None` swallow is removed. (93%)
- **Secrets-block real for 4/5 writers.** `_secret_literal_errors`→`errors[]`→`ok=False`; save/push/edit/migrate all validate-before-write; the r7 write-then-lint order bug in `_push_config` is fixed; the old test that enshrined `sk-live` persisting in a 0644 file was **genuinely inverted** (not just supplemented); `_literal_secret` correctly passes `$VAR`/`${VAR}`/`EMPTY` and catches `sk-`/`hf_`.
- **Backend-evidence gate fail-closed for FP8.** Missing Cutlass-FP8 / missing FLASHINFER / MARLIN-present all raise (mirrors the local script's exit 30/31/32); wired after the real smoke-tui under `set -euo pipefail`; reads the correct log source (docker stdout→LogSink→durable file); does **not** false-fail BF16 (per-recipe skip).
- **No regression.** `git show 443a9e0 --name-only` confirms the hardening commit did not touch `sidecar.py`/`supervisor.py`/`docker_runtime.py`/`schema.py`/`preflight.py`/`flag_manager.py`/`phases.py`. All four do-not-regress invariants re-verified intact; r6 M1–M10 still closed.
- **Onboarding plumbing is real** (even though the flows aren't): `target.agent_command` is stored **and consumed verbatim** by `factory.py` (defeats the `--venv`/PATH footgun when set), `--ssh-key`→`-i`, ControlMaster defaults, the **N5-1 silent-token-drop P0 is fixed**, token-as-file fallback, SSH stderr→named error codes, R2 `config push/pull/lint`.
- **Test authenticity holds.** The Opus pass refuted finder "hollow test" claims: the §6.2 docker launch/supervise loop is implemented (a function, not a missing class), PM2 download FSM + 0600 durable log are tested, FlagManager is wired in `app.py`, the compose→review→preflight→save→smoke pipeline is demonstrated from the TUI.

---

## 3. What contradicts "complete" — the actionable gaps

**[HIGH — NEW, my-eyes-confirmed] `_clone_config` secret-at-rest bypass.**
`_clone_config` (local.py:822-875) validates only via `ModelConfig.model_validate` (schema, line 849) and **never calls `validate_config_payload`**; `_apply_config_overrides` runs at line 847 *before* the write at 863. So `vela deploy clone src new --set server.api_key=sk-live` writes the literal secret into a new 0644 YAML **even when the source is clean** — and cloning a secret-bearing config does the same. Save/push/edit/migrate all block; clone is the one unguarded writer. Directly violates §11/NFR-C4. *Fix is small — route clone through the same `validate_config_payload` gate the other four writers use.*

**[HIGH] Onboarding `bootstrap`/`doctor` are scaffolds (not the spec flow).**
- `targets_bootstrap` (cli.py:249-290): builds a `TargetConfig`, `upsert_target_file`, echoes "bootstrapped" + a static "next" hint. **No SSH reachability probe, no agent discovery across canonical paths, no `--install`, no `--build`, no handshake.** The spec's acceptance command (`bootstrap … --install --build …`) **cannot run** — those flags don't exist.
- `doctor` (cli.py:1937-1972): no `--target`, no RPC; two local-filesystem checks; **unconditional static `next_steps`** that tells even a healthy target to "run bootstrap" (actively misleading).
- **R1 SSH discovery probe — the spec's "highest-value" fix — is absent.** The operator trades the `--venv` footgun for a manual `--agent-command`.
- The bootstrap/doctor **tests are hollow** (assert persistence + exit-code, not the spec flow) — "tests pass" does not evidence onboarding completion.
- *Scoping note:* these are **P2** in the onboarding spec's own priority scale and outside the docker/composer/model v1 MVP — so HIGH against the "complete" claim, but reasonably deferrable if onboarding is an explicit post-v1 track.

**[HIGH] TUI F-TUI-2: no in-wizard build creation / adopt-venv.**
The wizard runtime Select offers {Process, Docker, Build, Executable}, where "Build" is an *adopt-existing-registered-build* dropdown. The spec's "create build (→ build flow)" and "adopt venv" handoffs are absent from the wizard (they live only in the standalone build manager). *Existing-pin model mode + existing-build adopt are genuinely wired+tested, so this is partial (52%), not scaffolding.*

**[MEDIUM] Backend-gate test/robustness holes.**
- The entire `_config_shape_errors` branch (5 fail-closed modes: docker runtime, pinned image SHA, `FLASHINFER_CUDA_ARCH_LIST==12.0f`, `kv_cache_dtype==fp8`, `--attention-backend FLASHINFER`) has **zero tests** — the broadest part of the gate is unexercised.
- The **exit-31 / FLASHINFER-absent** reject path is untested (only cutlass-missing + MARLIN cases exist).
- **Fail-open on config-name drift:** the rule is keyed on the CLI `config_name` arg with no cross-check against the reattached run's `config['name']` — a renamed FP8 config silently skips the gate. (Bounded: the default lane passes the same string to both smoke and gate.)

**[MEDIUM] Secrets — residual.**
- `_edit_config` secret-block is code-correct but **untested** for literal-secret injection (a regression would ship silently).
- **Behavior change:** `edit_config`/`migrate_wrapper_config` now hard-fail on pre-existing secret-bearing legacy configs (secret→error flips `ok`), with no in-tool remediation.

**[MEDIUM] Model-pin deviation.** Offline pin without a `commit_sha` now **hard-fails** instead of recording `commit_sha=null`/`remote_only`+warn per spec L263 — a documented capability (pre-register a repo to download later while offline) is lost. Defensible for immutability, but undocumented as a deliberate deviation.

**[LOW] Pre-existing footguns (not regressions):** `vela deploy create` still unconditionally overwrites (`--overwrite` dead for create); new docker ErrorKinds (IMAGE_NOT_FOUND/DAEMON_UNREACHABLE/NAME_CONFLICT/GPU_NOT_AVAILABLE) have no `ERROR_GUIDANCE` remediation entries.

---

## 4. Verification value this round

71 confirmed / 5 adjusted / **3 refuted**. The Opus pass again earned its keep — the "is-it-complete" finder was **overly pessimistic** (manufactured "blocking gaps" by grepping the wrong file or demanding spec-exact test *names*): it claimed no DockerRuntime class (it's a function), an untested PM2 FSM (it's tested), and an unwired FlagManager (wired in `app.py` it never opened). Opus refuted all three and corrected the overall estimate **up** (72→80). Net: the picture reported here is the corrected one, not the finder's pessimism — and the one genuine HIGH the finders *missed* (the clone bypass's CLI-override-injection vector) was caught by the completeness pass.

---

## 5. Is it really done? — by track

| Track | % | One-line |
|---|---|---|
| **Core deploy/runtime/model engine (v1 MVP)** | **~88%** | Substantially complete + authentically tested; one HIGH (clone secret) to fix. |
| **Model-pin immutability** | **93%** | r7 HIGH genuinely closed, correctly scoped. |
| **Secrets-at-rest** | **80%** | Real for 4/5 writers; clone bypass is the HIGH hole. |
| **Backend-evidence gate** | **83%** | Logic correct + fail-closed; test coverage is the gap. |
| **TUI primary-surface breadth** | **52%** | Existing-pin + adopt-build wired; in-wizard build-create absent. |
| **Onboarding** | **36%** | Plumbing real; headline bootstrap/doctor UX scaffolded. |
| **Safety / regression** | **clean** | No invariant or punchlist regression. |

**Overall vs the coder's "complete" assertion: ~80%.** The claim is an overclaim by roughly that 20% — but the coder fabricated nothing; everything built is real and tested. The gap is (a) the clone secret leak, (b) onboarding's guided flows, (c) in-wizard build creation, (d) backend-gate test coverage.

---

## 6. Recommended priority

1. **[HIGH] Fix `_clone_config`** — route it through `validate_config_payload` like the other four writers (small fix; closes the plaintext-secret leak). Add a clone-with-secret block test.
2. **[HIGH / or re-scope] Onboarding** — either implement the real flows (`bootstrap`: SSH probe + agent discovery + `--install`/`--build` + handshake; `doctor`: `--target` + a `diagnose` RPC returning per-host dirs/version/auth/toolchain), **or** explicitly re-scope onboarding as a P2 post-v1 track and stop reporting it complete. Make `doctor` next_steps conditional on failures.
3. **[HIGH] TUI F-TUI-2** — wire in-wizard create-build / adopt-venv handoffs (the build flow already exists in the manager; reuse it).
4. **[MED] Backend gate** — test `_config_shape_errors` (5 modes) + the FLASHINFER-absent reject path; add a `config['name']==config_name` cross-check to close the fail-open.
5. **[MED] Secrets** — add the `_edit_config` secret-injection test; decide/doc the offline-pin behavior (spec L263).
6. **[LOW] Polish** — `ERROR_GUIDANCE` for the new docker ErrorKinds; revisit `deploy create` unconditional overwrite.

**Bottom line:** the coder is close — the core architecture is near-v1 and genuinely tested — but "complete" is premature. One HIGH security fix (clone), the onboarding guided-flow scoping decision, in-wizard build creation, and a handful of test/robustness gaps stand between the current state and a defensible "done."

*Snapshot 2026-06-06, HEAD `40858d4`. Read-only review — no code modified.*
