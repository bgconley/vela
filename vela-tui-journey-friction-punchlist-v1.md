# Vela TUI — Journey Friction Punchlist & Implementation Plan (v1)

**Status:** APPROVED · Phases A-C DELIVERED 2026-06-09 (J1-J14) · Phase D DELIVERED 2026-06-10 (J15-J18; M-M1 approved) · Phases E+F DELIVERED 2026-06-10 (J19-J29) · Phase G DELIVERED 2026-06-10 (J30-J37; reviewed: 2-agent pass, 3 majors fixed pre-commit) · ALL 37 ITEMS COMPLETE · **Created:** 2026-06-09 · **Source:** the 2026-06-09 three-pass UX journey audit (builds journey, models + wizard journey, scoped-intent baseline vs `vela-onboarding-ux-spec-v1.md` / `vela-deployment-composer-user-stories-v1.md` / `vela-deployment-composer-spec-v1.md`) plus a cold-start render probe.

**Goal:** make the full journey — *cold start → target → build → model → deployment → serving → tuning* — frictionless for someone new to these concepts, **without slowing down power users**. The audit's verdict: the individual forms already meet the bar (helpers, live validation, previews); what's missing is the *connective tissue* — state safety, next-step bridges, entry-point discoverability, and concept explanations at point of use.

> **Companion docs:** `vela-tui-figma-redesign-handoff.md` (design language, tokens `39:3`, node map), `vela-tui-overhaul-implementation-plan-v1.md` (execution conventions, contract rules). This doc follows both.

---

## 1. Design principles for this effort

1. **Never lose user work.** Any path that can discard typed state is a defect, full stop.
2. **Every action ends with a bridge.** Success or failure, the UI says what just happened *and the one key to press next*. A toast that names a result but not a next step is half-finished.
3. **Explain concepts at point of use, in one faint line.** The existing helper pattern (dim, one line, "what it does + where the value comes from") is the locked language — extend it to the places that still assume architecture knowledge (Select semantics, recipes, flag sources, HF_TOKEN).
4. **Progressive disclosure everywhere a choice gates fields.** Create Build's `_VISIBLE` pattern is the template: irrelevant fields stay *mounted* (contract-safe) but hidden.
5. **Power users lose nothing.** All existing keys, payloads, ids, and the palette stay; helpers are one dim line each (skimmable/ignorable); disclosure only removes *irrelevant* fields; advanced controls collapse behind a toggle rather than disappear. No confirmation is added to a previously unconfirmed non-destructive action.
6. **Empty states are onboarding.** "No X found" must always be followed by "— press &lt;key&gt; to &lt;first action&gt;".
7. **Honesty rules carry over:** no fabricated/inert affordances (no dead keys, no decorative selections), no real environment details in mocks (`gpu-node`, `user@gpu-host`, `/home/user/...`, `Blackwell sm_120`, `qwen36-27b-bf16-blackwell`).

**Engineering conventions (unchanged from the overhaul):** strict red→green TDD per item; preserve all dismiss-payload/widget-id contracts; where a smoke test pins *presentation* that an item deliberately changes, update the pin in the same red→green pass; ruff clean; render + eyeball each touched screen against its mock; log to `.wolf/` per OpenWolf.

---

## 2. The journey spine (the mental model the UI must teach)

```
target (where)  ×  build (which vLLM)  ×  model@revision (what)  ×  config (how)  →  run
```

- A config may **pin** a build; otherwise it launches with the **active** build (`active.json`); otherwise its raw executable ("unmanaged").
- This sentence appears **nowhere in the UI today** and is the root of half the confusion. Items J7, J8, J30 put it where users need it.

---

## 3. Punchlist

Severity: ■ blocker-class friction (stops a new user) · ◆ major · ○ minor. Size: S (&lt; ½ day) / M (½–1 day) / L (multi-day). "Mock" = needs a Figma frame before implementation (see §4).

### Phase A — Never lose work (wizard state safety) — ■ ✅ DELIVERED (J1-J4)

| ID | Item | Detail | Files | Size | Mock |
|---|---|---|---|---|---|
| **J1** | ■ Preserve the wizard draft on compose/validate/preview failure | Wizard currently dismisses before server-side validation (`new_deployment.py:406-410`); failure paths in `app.py:2700-2728` drop the draft. Thread the draft through `_review_new_deployment`; on failure reopen `_open_new_deployment(initial=draft, error_message=…)` with the error rendered *inside* the wizard (same pattern the uv-gate uses for Create Build). | new_deployment.py, app.py | M | M-W3 |
| **J2** | ■ `B Back` on the Review screen | Esc on Review discards everything; there is no back-to-edit. Add a `b`/`Ctrl+B` binding that dismisses with `{"action": "back", "draft": …}` and reopens the wizard at the last step. Esc keeps meaning cancel (it may keep discarding — but only once Back exists). | new_deployment.py (review), app.py | S | M-W4 |
| **J3** | ■ Enter advances; it does not submit the wizard | `on_input_submitted` → `action_submit` fires full compose from *any* input (`new_deployment.py:327-329`). Change Enter to advance to the next step (Ctrl+S remains "Review"). Update the smoke pins that rely on Enter-submit deliberately. **Risk:** the 24-test wizard contract — payloads/ids untouched, only the Enter binding changes. | new_deployment.py | S | — |
| **J4** | ◆ Acceptance test: the golden-path journey | New Pilot smoke test: cold start → `n` → walk every step with Enter → trigger a validation failure → assert the draft survives → Back from Review → save. This is the regression net for the whole phase. | tests/test_tui_smoke.py | M | — |

### Phase B — Close the loop (next-step bridges) — ■/◆ ✅ DELIVERED (J5-J10)

| ID | Item | Detail | Files | Size | Mock |
|---|---|---|---|---|---|
| **J5** | ■ Build job completion bridge | On `job_done ok` (result currently discarded, `app.py:1530-1535`): toast `"Build ready: <label> — press b ⏎ to make it the default, or pin it in a deployment"`, and reopen the Build Manager focused on the new build. Same for adopt success. | app.py | M | M-B1 |
| **J6** | ■ Smoke completion bridge | After "Smoke READY" + intentional auto-stop, the phase reads STOPPED like a crash. Final notify: `"Smoke passed — '<name>' saved & selected · press l to launch"`. On smoke failure: `"Smoke failed (<kind>) — config saved · F adjust flags · l retry"`. | app.py:3046-3071 | S | — |
| **J7** | ■ Explain Select semantics in the Build Manager | One helper line under the list: `"⏎ sets the default build — used by every config without a pinned build. Pinned configs and live runs are unaffected."` Toast becomes `"Selected build: <label> — now the default for unpinned configs"`. | build_manager.py, app.py:1394 | S | M-B1 |
| **J8** | ◆ Active build detail row tells the truth about usage | `used_by_configs` counts only pins (`local.py:4490-4496`); the active build shows "used by 0 configs". Add `"+ default for all unpinned configs"` to the active build's detail. | build_manager.py | S | M-B1 |
| **J9** | ◆ Download completion bridge | `job_done ok` currently yields transient strip text "model cached". Add toast `"Downloaded <repo> — cached on <target>"`; reopen Model Manager when the download was started from it. | app.py:338-341, 1974-1981 | S | — |
| **J10** | ○ Build job start announcement | `"Build started — install log streams below · s cancels"` when the create/adopt job kicks off. | app.py | S | — |

### Phase C — First contact (discoverability & empty states) — ■ ✅ DELIVERED (J11-J14)

| ID | Item | Detail | Files | Size | Mock |
|---|---|---|---|---|---|
| **J11** | ■ `n New` (and `c Configs`) join the persistent footer | The flagship flow's key is absent from `_render_footer_bindings` (`app.py:4358-4363`). Add with responsive priority: at narrow widths, `n` survives before less-critical keys. Audit footer truncation order generally. | app.py | S | M-D1 |
| **J12** | ■ Empty states become calls to action | Dashboard Configs panel: `"No configs yet — press n to create your first deployment · ? help"`. Model Manager: `"No models yet — press p to pin one (HF repo id, local path, or URL)"`. Build Manager: `"No builds yet — n create · a adopt an existing venv"`. Config picker: equivalent line. | app.py:3586/3632, model_manager.py:152, build_manager.py:145, config_picker.py | S | M-D1 |
| **J13** | ◆ First-run quick-start block in the log pane | When the registry is empty AND no run has ever happened, the log pane (currently just "INFO Vela ready") renders a 4-line quick start: `1 t add/bootstrap a target · 2 n create a deployment (pin model + build inside) · 3 ⏎ save & smoke · 4 l launch`. Disappears as soon as a config exists. | app.py | M | M-D1 |
| **J14** | ○ Glyph legend in Help | 📌 ● ○ ▲ ✕ 🔒 ⇩ are never defined. One legend line in HelpScreen; Help also gains the journey-spine sentence (§2). | help.py | S | — |

### Phase D — Pin Model rebuild + model decision data — ■/◆ ✅ DELIVERED (J15-J18; J15 adds the approved Advanced section incl. the new Download-now option)

| ID | Item | Detail | Files | Size | Mock |
|---|---|---|---|---|---|
| **J15** | ■ Rebuild PinModelScreen in the form language | The only way a model first appears is the app's weakest form: 9 flat fields, zero helpers, CLI-syntax error text (`pin_model.py:50-168`). Rebuild: **Source** select (`HF repo / local path / URL`) with progressive disclosure → one visible source field; helpers ("repo ids: huggingface.co — e.g. org/model"; "absolute path on the target"; "direct .gguf/.safetensors URL"); revision/sha as optional fields with the Download screen's reproducibility copy; gated note "detected automatically — you usually don't set this"; KeyHintBar footer. **Preserve** the dismiss payload + `#pin-model-*` ids; inputs stay mounted. | pin_model.py, tests | M-L | M-M1 |
| **J16** | ◆ Pin success bridge | Toast + reopen Model Manager focused on the new entry, with `"d downloads it now"` hint (today: toast only, manager closed). | app.py:2016-2030 | S | — |
| **J17** | ◆ Model Manager decision data | Add detail rows: `used_by: N configs`, dedup-aware `size: X GB unique / Y GB nominal` (MS spec §371 scoped both). Remove-confirm gains reclaimed-GB + irreversibility line (MS §392). Requires small agent additions if the data isn't in `list_models` payloads yet. | model_manager.py, agent/local.py | M | M-M2 |
| **J18** | ■ One canonical HF_TOKEN string, everywhere | Named in 4+ surfaces, located in none. Canonical copy: `"set HF_TOKEN in the target agent's environment, or in this config's env: block"`. Apply to model_manager.py:333-340, download_model.py:209, app.py:419 guidance, agent preflight detail. | 4 files | S | — |

### Phase E — Explain the jargon at point of use — ◆ ✅ DELIVERED (J19-J23)

| ID | Item | Detail | Files | Size | Mock |
|---|---|---|---|---|---|
| **J19** | ◆ Recipes explained + applied loudly | Helper under the Recipe select: `"A validated stack for this target — picking one pre-fills runtime, image, model, flags & port. Custom starts blank."` After application (silently rewrites 6+ fields today, `new_deployment.py:732-770`): summary line/toast `"Recipe applied: set runtime=docker · image=pinned · port=18001 …"`. | new_deployment.py | S | M-W1 |
| **J20** | ◆ Flag Manager legend + recipe-protection alternative | One faint line under the counts: `"modeled = typed flags this build understands · passthrough = raw args forwarded as-is · unknown = not recognized by this build"`. Recipe-protection note gains an action: `"…to change precision safely, switch recipe or preset instead."` | flag_manager.py:479-498, 560-566 | S | M-F1 |
| **J21** | ◆ Preset descriptions rendered | The agent already ships `description` per preset; both UIs drop it (`new_deployment.py:693-699`, `flag_manager.py:616-632`). Render under the select. | both | S | M-W2 |
| **J22** | ○ Docker image / port / suggestion helpers | Image: `"blank = recipe/preset default · pin a digest (vllm/vllm-openai@sha256:…) from Docker Hub"`. Port: `"blank = auto-allocated on the target"`. Suggestions line gains the label `"suggested:"` and drops bare `field=value` jargon where possible. | new_deployment.py:214-218, 290, 946-971 | S | M-W2 |
| **J23** | ○ Flag Manager initial focus + edit hint | Mounts with focus None (`flag_manager.py:190-191`); add `Tab edit value` hint or focus the value input. | flag_manager.py | S | — |

### Phase F — Wizard structure (disclosure, clone, editable-derived) — ◆ ✅ DELIVERED (J24-J29; clone realized as palette command + prefilled wizard rather than clone_config RPC — review/edit-before-save beats a blind file copy)

| ID | Item | Detail | Files | Size | Mock |
|---|---|---|---|---|---|
| **J24** | ◆ Runtime step progressive disclosure | Docker image, Build select+input, Executable are all visible regardless of runtime choice (`new_deployment.py:195-234`). Apply the `_VISIBLE` display-toggle pattern (fields stay mounted → 24-test contract safe). | new_deployment.py | M | M-W1 |
| **J25** | ◆ Model step disclosure + de-duplicate the two model fields | Mode select ("Existing pin / Pin HF → / Adopt local → / Bare repo id") changes nothing visibly; "Pinned model" select and free-text "Model" input coexist unexplained. Mode now drives which field shows; helper explains the bare-repo-id tradeoff (no pin = resolved at launch, no immutability). | new_deployment.py:235-271 | M | M-W1 |
| **J26** | ◆ Clone deployment in the TUI (US E1.4, P1 — currently CLI-only) | Config picker + palette action `"Clone deployment: <name>"` → `clone_config` RPC (already wired agent-side) → open the wizard prefilled, name suggested `<name>-2`. The scoped "new variant in 30 seconds" story. | config_picker.py, app.py | M | M-W5 |
| **J27** | ◆ Name suggested, not demanded | Blank Name → suggest `<model-slug>-<dtype>-<gpu>-<target>` (CS §6.1) as a ghost the user can accept with Tab or overwrite — instead of "Name is required" (`new_deployment.py:429-430`). | new_deployment.py, composer.py | M | M-W1 |
| **J28** | ◆ Editable derived fields (power-user, collapsed) | served_model_name / runs_dir / container_name are derive-only (US E1.2 scoped "auto **and editable**"). Add a collapsed `Ctrl+R Advanced` group on Customize exposing them, pre-filled with derived values + `(auto)` markers. Hidden by default → zero novice cost. | new_deployment.py | M | M-W2 |
| **J29** | ○ Preflight checklist + compose-time TP advisory | Review/save shows only `failures[0]` (`app.py:4587-4603`); render the full scoped pass/fail checklist with per-check fixes. Add the compose-time `tensor_parallel_size` vs GPU-count advisory (US E2.3) instead of save-time-only failure. | app.py, new_deployment review, composer.py | M | M-W4 |

### Phase G — Build surface power & clarity — ◆/○ ✅ DELIVERED (J30-J37; J30 realized as settle-then-reopen-focused)

| ID | Item | Detail | Files | Size | Mock |
|---|---|---|---|---|---|
| **J30** | ◆ Managers stay open for non-terminal actions | verify/repair (and failed select) currently `dismiss` the whole manager (`build_manager.py:97-132`) — results land as toasts over the dashboard; maintenance loops cost a reopen each time. Refresh in place instead. Mirror in Model Manager for verify/refresh. | build_manager.py, model_manager.py, app.py | M | M-B1 |
| **J31** | ◆ Pin/unpin a build on an existing config | Today pinning exists only inside New Deployment; flag manager shows `build:` read-only. Add `P pin/unpin` in Build Manager acting on the selected config (or via config picker). Closes the "must create a whole new deployment to pin" gap. | build_manager.py, app.py, agent | M | M-B1 |
| **J32** | ■ Fix the wheel-helper trap + inert pip channel | Helper says "prebuilt wheel **or venv**" but the agent requires `path.is_file()` (`local.py:4242-4248`) — venvs fail late, on the target. New copy: `"path to a .whl file — to register a venv, use Adopt (a)"`. Drop `channel` from `_VISIBLE["pip"]` (agent ignores it, `local.py:4017-4030`). | create_build.py | S | — |
| **J33** | ○ Git method optional Ref field | Agent supports `ref`/`precompiled` (`local.py:4266-4277`); the form can't express it. Optional `Ref` field under git method. | create_build.py, app.py | S | — |
| **J34** | ○ Remove-refusal names the blockers | "build is pinned by one or more configs" drops `details["configs"]` (`app.py:1472`). Append the config names. Same for model remove. | app.py | S | — |
| **J35** | ◆ Venv discovery for Adopt | Validation ≠ discovery: the venv path must come from outside the TUI today. Agent RPC `discover_venvs` scanning common roots (`~/venvs`, `~/.venvs`, conda envs, builds root) → picker above the path input ("or type a path"). The single biggest power-assist for adopt. | agent/local.py, build_registry.py, adopt_build.py | L | M-B2 |
| **J36** | ○ Config picker "Push this config →" affordance | OB R2 scoped it *in the picker* (exists today only in palette/TargetManager). | config_picker.py, app.py | S | — |
| **J37** | ○ "Install uv now" one-key job (OB R3a) | uv-gate currently offers only the fallback path; the scoped one-keypress agent-side `install uv` job never shipped. Offer it from the uv-block error state. | create_build.py, app.py, agent | M | — |

---

## 4. Figma mock plan (design before code)

Same file (`9xUgzyoFqWmd40tV5dwaHv`), **new page: "Journey v2 — Friction Pass"**, reusing the *Vela Terminal* token collection (`39:3`) and the locked form language. Terminal-faithful, generic placeholders only (`gpu-node`, `user@gpu-host`, `/home/user/...`, `Blackwell sm_120`, config `qwen36-27b-bf16-blackwell`). Every field shows its helper. Each mock gets a user approval gate before its punchlist items are implemented (same process as the overhaul's Phase-1 gate).

| Mock | Frame | Defines | Backs items |
|---|---|---|---|
| **M-D1** | Dashboard — first run | Empty-state CTAs in Configs panel, quick-start block in the log pane, footer including `n New` + `c Configs`, narrow-width footer priority | J11 J12 J13 |
| **M-W1** | Wizard steps 1–3 v2 | Recipe helper + applied-summary line, runtime disclosure (one field-group visible per runtime), model-mode disclosure, suggested-name ghost | J19 J24 J25 J27 |
| **M-W2** | Wizard step 4 (Customize) v2 | Preset description line, port/exposure/image helpers, collapsed `Advanced (auto-derived)` group with `(auto)` markers | J21 J22 J28 |
| **M-W3** | Wizard — validation failure state | Error banner *inside* the wizard with the draft intact, field-level highlights where mappable | J1 |
| **M-W4** | Review v2 | `B Back` in footer, full preflight checklist (✓/✗ per check + one-line fix), warnings, masked command (unchanged) | J2 J29 |
| **M-W5** | Clone entry | Config picker row affordance + prefilled-wizard state ("cloned from `<name>`" note) | J26 |
| **M-B1** | Build Manager v2 | Select-semantics helper line, active build "+ default for all unpinned configs" row, in-place verify/repair result states, `P pin to config` action, post-build-job focused state (new build highlighted + bridge toast spec) | J5 J7 J8 J30 J31 |
| **M-B2** | Adopt — venv discovery | Discovered-venv picker (path + detected versions per row) above the manual path input | J35 |
| **M-M1** | Pin Model v2 | Source select + per-source disclosure, helpers incl. huggingface.co pointer, optional revision/sha block, gated auto-detect note, footer | J15 |
| **M-M2** | Model Manager v2 | `used_by` + unique/nominal size rows, remove-confirm with reclaimed GB + irreversibility | J17 |
| **M-F1** | Flag Manager legend | Source-taxonomy legend line, recipe-protection note with alternative action, initial-focus/edit hint | J20 J23 |

Notes for the designer (me, next session): Build Manager, Model Manager, and Pin Model have **no existing mocks** (the overhaul extrapolated them) — M-B1/M-M1/M-M2 are net-new nodes; the wizard/review/flag frames are v2 revisions of `56:2`–`58:68` and `55:2`. Toast/bridge copy (J5/J6/J9/J16) needs no frames — specify the strings in M-B1's annotation panel since Textual `notify()` styling is fixed.

---

## 5. Sequencing & estimates

| Order | Phase | Why first | Rough effort |
|---|---|---|---|
| 1 | **A** (J1–J4) | Data loss is the only defect class that *punishes* a new user for trying | ~2 days incl. the golden-path test |
| 2 | **C** (J11–J14) | Cheapest items, largest first-impression delta; unblocks "new user can even start" | ~1 day |
| 3 | **B** (J5–J10) | Converts every completed action into momentum; mostly strings + small handler changes | ~1–1.5 days |
| 4 | **D** (J15–J18) | The model entry point is the most knowledge-hungry wall; J18 is a 30-min string sweep | ~2 days |
| 5 | **E** (J19–J23) | Point-of-use concept copy; all S-sized | ~1 day |
| 6 | **F** (J24–J29) | Structural wizard work — needs M-W1/2/4/5 approved; touches the 24-test contract most | ~3 days |
| 7 | **G** (J30–J37) | Power & polish; J32 (trap fix) can be pulled forward into any earlier batch | ~3 days (J35 is the long pole) |

Figma work precedes each phase that has mocks (≈1 session for M-D1+M-B1+M-M1 batch, ≈1 for the wizard set). Phases A–C need only M-D1/M-W3/M-W4 sketches and could start immediately after approval.

---

## 6. Definition of done

1. **The golden-path test passes**: a scripted Pilot journey from empty state to saved-and-smoked deployment using only what the UI itself surfaces — every required value either has an in-UI source pointer or a working default; no step requires out-of-band knowledge.
2. **No data-loss path remains** in the wizard (J1–J3 covered by tests that intentionally fail validation and Esc from Review).
3. **Every async action lands a bridge** (build/download/adopt/smoke: result + next key).
4. **Every empty state names its first action.**
5. **The information-source scorecard from the audit reaches "UI-tells or UI-hints" for every demanded value** (HF repo id, sha, channel, venv path, image digest, port, HF_TOKEN, recipe, presets) — zero "UI-silent" cells.
6. Full suite green (Mac-safe), ruff clean, every touched screen rendered + eyeballed against its approved mock, all dismiss-payload/id contracts preserved (deliberate pin updates only, each noted in the commit).

---

*End v1. Next actions: approve/adjust the punchlist → design the M-D1/M-W3/M-W4/M-B1/M-M1 mocks in Figma → execute Phase A.*
