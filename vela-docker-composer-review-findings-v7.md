# Vela — Review v7: Validating the Coder's Spec-Check + Reassessment — 2026-06-06

**Method (round 7):** re-established ground truth at HEAD `d0ca4e6` → 7 **Sonnet 4.6** finders (each tasked to (a) independently validate the agentic coder's assigned spec-check claim(s) and (b) sweep its spec area) → **Opus 4.8** independently re-read every citation, rendered authoritative accuracy verdicts + verified findings + completeness pass → **Opus 4.8** synthesis (this doc) with my own corroboration of the load-bearing calls.
**Scope (expanded this round):** docker-runtime + composer specs **plus** `vela-onboarding-ux-spec-v1.md` and `vllm-model-management-spec-v1.md` (pulled in by the coder's P1/P2 findings).
**Ground truth:** **830 tests pass deterministically** (116.91s), **ruff clean**, **crown-jewel clean**. 3 commits since v6 baseline `4d2fdca` — incl. **`79729f8 "Complete docker composer punchlist"`** (the coder consumed my v6 M1–M10).
**Workflow stats:** 14 agents, 1.12M tokens, 596 tool calls, ~14 min. Coder-claim accuracy: **29 accurate / 3 mostly-accurate / 1 partially-accurate**. Verified findings: 7 high / 10 medium / 16 low / 29 info (52 confirmed, 8 adjusted, 2 refuted). Opus completeness caught 1 high + 7 medium missed.

---

## 0. Headline

**The coder's self-review is high-quality and honest** — 32 of 33 sub-claims are accurate as stated; the one correction is a *framing* nuance, not a factual error. **Two of the coder's findings I rate higher than they did**, and I am **correcting one of my own round-6 statements**.

**The round-6 docker/composer punchlist is ~94% closed** (`79729f8`) with real, non-hollow tests — M1–M8, M10, S3 genuinely done; M9 intentionally benign-open.

**The headline number drops from v6's 85% to ~72% overall — but NOT because of regression.** The docker/composer track actually *rose* (≈85→90%). The overall number falls because **the scope expanded**: onboarding (~13% built) and model-pin immutability (~55%) are now in the assessment, and both are early. Stated by track below.

---

## 1. Validation of the coder's 8 findings (the primary ask)

| Coder finding | My verdict | The precise truth |
|---|---|---|
| **P1 #1 — Onboarding not v1-ready** | ✅ **Accurate** | Every sub-claim confirmed: no `vela targets bootstrap`, no `vela doctor`, no first-class `--ssh-key`, `TargetConfig` has neither `agent_command` nor `ssh_key` (`targets.py:27-34`, `extra='forbid'`), SSH still emits `PATH=<venv>/bin:$PATH vela agent connect` with no discovery probe (`factory.py:15,343-348`), auth is env-only with no token-file/`gen-token --install` (`auth.py:18`, `cli.py:2524`). **Refinement:** "auth is env-only" is true, but do **not** read it as N5-1 still broken — the silent-token-drop bug **is fixed** (`stdio.py:140,187`→`agent-auth-required` frame) and N5-2 entropy check landed. |
| **P1 #2 — Blackwell validation gate too weak** | ⚠️ **Accurate risk, mis-framed** | The facts are all correct: the local FP8 script hard-fails on missing Cutlass-FP8/FlashInfer or MARLIN fallback (`start-…fp8….sh:216-229`); Vela's remote lane runs only `preview` + `smoke-tui` (`run_remote_tests.sh:659-661`); `smoke-tui` asserts only READY+URL (`cli.py:2290,2298`); artifacts record only READY. A wrong backend **could** slip through. **But** this is a **hardening suggestion beyond spec**, *not* a compliance gap: FR-D5 (`docker-spec:57`) makes READY "health-driven… not a log line" **by design**, and DK4's bar is READY + docker stop. Re-label "gate too weak" → "gate is health-only by spec; backend-evidence validation is a valuable beyond-spec addition." |
| **P2 #3 — TUI composer thinner than spec** | ✅ **Accurate** | `NewDeploymentScreen` is a 6-step modal with a static target label, runtime Select (process/docker/build/executable), a **bare** model text input, preset/host/port/exposure (`new_deployment.py:86-291`). Spec §1.4/§8/§10 want the primary surface to offer **in-wizard build create/adopt** and **model pin/adopt/download modes** with gated/cached state — genuinely absent (HIGH primary-surface gaps). The narrower DC4 acceptance bar ("composes + smokes from the TUI") **is** met and tested, so "exists but thin for TUI-first v1" is exactly right. |
| **P2 #4 — Literal secrets allowed in 0644 configs** | ✅ **Accurate** (+ corrects my v6) | `_lint_config` only **appends warnings**, never blocks (`composer.py:1067-1075`); `validate_config_payload` sets `ok = not errors`, so `api_key: sk-live` → `{ok: True, warnings:[…]}` (`:540`); `_save_config`/`_push_config` write plaintext to **0644** with no secret gate (`local.py:768,980,3485-3498`); a test asserts `sk-live` round-trips in a 0644 file with `ok=True` (`test_deployment_composer.py:954,971,980,983`). Spec **mandates a hard block**: "`config lint` **blocks** accidental secret literals" (`:219`) + "0644 configs, **no secrets inside**" (`:217`) + NFR-C4 "**never written** to a config except as env references" (`:72`). Real **medium** config-at-rest deviation. **My v6 "(_lint_config blocks literal secrets)" was imprecise** — the code detects+warns and did **not** change between HEADs (verified byte-identical); "blocks" was my loose shorthand. The coder's wording is the accurate one. |
| **P2 #5 — HF model pins can stay mutable** | ✅ **Accurate** (I rate the core HIGH) | All mechanics confirmed: `model pin <repo>` accepts no revision/commit (`cli.py:732-739`) and writes `commit_sha=None` with **no warning**; `_hf_model_info` swallows `ImportError` **and** every `HfApi` error into a silent `return None` (`model_registry.py:1171-1183`); launch falls back to the **mutable** revision via `commit_sha or revision` (`:1066`). **The core issue (new HIGH):** `_validate_model_handoff_prelaunch` (`local.py:4356-4387`) guards only **offline** + **gated** — there is **no "unresolved `commit_sha`" guard**, yet spec PM1 (`model-spec:442`) requires `offline/gated/**unresolved**` and precedence #2 (`:351`) says model_ref "must resolve… **else blocked with a named error**." A pinned model_ref that didn't resolve launches against a mutable ref. |
| **Aligned #1 — agent boundary closed** | ✅ **Confirmed** | `VelaApp` stores a `TargetClient` Protocol (connect/call/ping/subscribe — no process surface; `app.py:631,658`, `client.py:88-108`); every lifecycle verb routes through `_target_call`→RPC→`LocalAgent.handle()` (`app.py:3148`, `local.py:432-441`); zero `Popen`/subprocess in `app.py`; structural tests assert no stored handle/sidecar path. |
| **Aligned #2 — recipe source-of-truth in docs** | ✅ **Confirmed** | `docs/deployments.md:14-22` states the local Blackwell recipe (not HF metadata) owns image digest / CUDA arch / FlashInfer-CUTLASS / cache / FP8-BF16 shape; HF metadata is advisory-only. Corroborated by `docs/docker-runtime.md:31-37`. |
| **Aligned #3 — remote CI cadence sane** | ✅ **Confirmed** | `remote-validation.yml`: self-hosted runner (`:83`), nightly cron `17 8 * * *` (`:74`), `workflow_dispatch` (`:3-73`), concurrency guard `cancel-in-progress:false` (`:77-79`). |

**Net: 7 of 8 fully accurate; the 8th (Blackwell) is factually accurate but mis-framed as a compliance gap when it is a beyond-spec hardening idea.** The coder did not overstate or invent anything.

---

## 2. Progress since v6 — the M1–M10 punchlist is genuinely closed

Commit `79729f8 "Complete docker composer punchlist"`, verified item-by-item against real (non-hollow) tests:

| v6 item | Status | Evidence |
|---|---|---|
| **M1** docker-run stderr + DockerErrorKind | ✅ Closed | stderr scrubbed→durable log/event (`supervisor.py:216-228`); `DockerErrorKind` enum + `classify_docker_error` (`docker_runtime.py:21-27,209-226`) |
| **M2** pull policy + real digest | ✅ Closed | `prepare_docker_image` honors policy; `inspect_docker_image` resolves digest via `docker image inspect`; sidecar records resolved digest (`docker_runtime.py:134-206`, `supervisor.py:189-196`) |
| **M3** bounded TUI smoke | ✅ Closed | `_run_saved_config_smoke` → `probe_until_ready` then **finally** stop + `Phase.STOPPED` (`app.py:2334-2412`) |
| **M4** preflight image + disk | ✅ Closed | `docker_image_availability_detail` + `low_disk_space_detail` (`preflight.py:113-193`) |
| **M5** FlagManager raw flags | ✅ Closed | editable "Raw passthrough args" Input (`flag_manager.py:107-111,196-212`) |
| **M6** palette "New Deployment" | ✅ Closed | `app.py:843-847` |
| **M7** wizard runtime picker | ✅ Closed | process/docker/build/executable (`new_deployment.py:148-158`) |
| **M8** idempotency + dry-run test | ✅ Closed* | create idempotent-by-name + `--dry-run` tested. *Side-effect: create now **unconditionally overwrites** → reverses the v6-praised refuses-to-clobber safety; `--overwrite` is a dead no-op for create (low). |
| **M9** ipc+shm both emitted | ◑ Open (benign) | unchanged; docker ignores `--shm-size` under `--ipc=host` |
| **M10** safety-arm tests | ✅ Closed | name-mismatch + digest-mismatch stop-refusal tests; docker-log-scrub integration via new `ready-with-secret.log`; live-run-guard test now uses a real PID (no monkeypatch) |
| **S3** shared fake-docker harness | ✅ Closed | `tests/fakes/fake_docker.py` |

---

## 3. New issues this round (beyond the coder's findings — Opus completeness pass)

- **[HIGH] Model-pin "unresolved" guard missing** — see P2 #5 above. The building blocks exist (`verify_model` already flags `cache_state='partial'`/`missing-commit`) but launch never consults them. **The standout finding of round 7.**
- **[MEDIUM] Pin-time HF errors silently swallowed** — `_hf_model_info` collapses 401/403, 404, network, and `ImportError` into `return None`; spec mandates `GATED_AUTH`/`REVISION_NOT_FOUND` at pin time (`model-spec:263,144`). Operator pinning a gated/typo'd repo gets a false-success.
- **[MEDIUM] Wizard never calls `suggest_deployment_defaults`** — per-model dtype/kv/TP consistency + gated-needs-token warnings (§6.4) never reach the primary surface; the wizard composes from preset + typed overrides only.
- **[LOW] `vela deploy create` now unconditionally overwrites** (M8 side-effect) — silent clobber, no confirmation; `--overwrite` is dead for create.
- **[LOW] New docker ErrorKinds have no `ERROR_GUIDANCE` remediation entries** — banner shows the kind but no fix advice.
- **Verification value:** two finders reported **stale-as-open** items (FlagManager raw flags, wizard build/executable) that the punchlist had **closed** — caught only by the Opus read against HEAD. Reinforces that independent verification against current HEAD is essential.

---

## 4. Completion by track

| Track | % to v1 | Note |
|---|---|---|
| **Docker/composer feature (DK0-DK4, DC0-DC5)** | **~90%** | Punchlist closed; remaining = TUI primary-surface breadth + idempotency-overwrite nuance + minor polish. *(Up from v6's ~85%.)* |
| **Model-management pin immutability** | **~55%** | The unresolved-guard **HIGH** + swallowed pin-time errors are the gap. |
| **First-run onboarding UX** | **~13%** | Forward spec, essentially unbuilt: no bootstrap/doctor/ssh-key/agent_command/token-install. (R2 `config push/pull/lint` + N5-1/N5-2 fixes did land.) |
| **Safety invariants** | **intact** | Agent boundary closed; verify-before-signal, scrub-before-wire, crown-jewel all hold. Config-at-rest secret policy is the one drift (medium). |

**Overall "polished v1" (all three tracks in scope): ~72%.** If onboarding is treated as a **separate forward track** (its own spec says "no code written here"), then the **shipping docker/composer product is ~88–90%** and onboarding is a deliberate post-v1 effort. The team should decide that scoping explicitly — it's the single biggest swing factor in the number.

---

## 5. Recommended next steps (priority order)

1. **[HIGH] Model-pin unresolved guard** — block a pinned `model_ref` whose `commit_sha` didn't resolve with a named error (spec PM1/precedence #2), and raise `GATED_AUTH`/`REVISION_NOT_FOUND` at pin time instead of swallowing. The `verify_model` "partial/missing-commit" logic already exists — wire it into the launch chokepoint. Add the failure-path tests (`test_model_resolver`).
2. **[MEDIUM] Secrets-at-rest** — make `vela config lint` **block** (set `ok=False`) on literal secrets and gate `save`/`push` on it; centralize at `_write_public_text_atomic`'s callers. Update the test that currently enshrines the round-trip.
3. **[MEDIUM→ for "TUI-first v1"] Wizard primary-surface breadth** — model-step modes (existing-pin / pin-HF / adopt-local / bare + download toggle + gated/cached state), in-wizard build create/adopt handoff, and call `suggest_deployment_defaults` to surface per-model suggestions.
4. **[P1 if onboarding is in v1 scope] Onboarding** — `vela targets bootstrap` + `vela doctor` + `TargetConfig.agent_command` (auto-resolve, kills the PATH/`--venv` footgun) + first-class `--ssh-key` + `gen-token --install`. Biggest unbuilt surface.
5. **[Beyond-spec, high value for Blackwell] Backend-evidence gate** — reuse the existing container-log capture (FR-D11) to grep for Cutlass-FP8/FLASHINFER and fail on MARLIN fallback, mirroring the local script's exit 30/31/32 (optional `smoke-tui --assert-backend` flag).
6. **[LOW] M8 nuance** — restore a confirm (or make `--overwrite` meaningful) for `vela deploy create`; add `ERROR_GUIDANCE` for the new docker ErrorKinds.

**Bottom line:** the coder is executing accurately and self-auditing honestly; the docker/composer engine is near-v1; the gating work for a *polished, end-to-end v1* is now (a) the model-pin immutability HIGH, (b) the secret-at-rest block, (c) the TUI primary-surface breadth, and (d) onboarding — if onboarding is in scope.

*Snapshot 2026-06-06, HEAD `d0ca4e6`. Read-only review — no code modified.*
