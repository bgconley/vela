# Vela Remediation Implementation Plan (v1)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.
> Also MANDATORY per repo policy (`.wolf/cerebrum.md`): STRICT red-green TDD for every code task — write the failing test FIRST, watch it fail for the right reason, then minimal green, then refactor. Never test-alongside.

**Goal:** Eliminate every defect and friction point found in the 2026-07-09 full-repo + live-TUI review (bug-233…bug-240 plus the unlogged findings): crash/hang classes, wizard dead-ends, layout breakage, model-lifecycle missed expectations, daemon/discovery traps, CLI/docs friction, and repo bloat.

**Architecture:** Ten phases ordered by user-facing severity and dependency. Phases 1–3 are surgical functional fixes (small diffs, big safety wins). Phase 4 is the layout-system pass that generalizes the already-proven Flag Manager full-width rebuild. Phase 5 fixes the pin→download→verify→deploy contract at the agent/composer layer. Phases 6–8 remove onboarding traps and document the product. Phase 9 is the repo diet. Phase 10 (optional) splits the giant modules. Every phase is independently shippable and ends with the full quality gate.

**Tech stack:** Python 3.10+, Textual 8.2.7 (pinned `>=8.2,<9`), Typer, Pydantic v2, pytest (run with Homebrew `python3 -m pytest`, NOT `.venv`), ruff (line length 100), mypy (respect `pyproject.toml` override burn-down list).

---

## How to execute this plan (cross-cutting rules)

**Read first, every session:** `.wolf/OPENWOLF.md`, `.wolf/cerebrum.md` (esp. Do-Not-Repeat), `.wolf/buglog.json` entries bug-233…240. Line anchors below were verified 2026-07-09 at HEAD `88d18d8`; if a file has drifted, locate by the quoted symbol, not the number.

**Contract preservation (non-negotiable):** The 1,133-test suite pins widget `id=`s, screen dismiss-payload shapes, and rendered substrings (esp. `tests/test_tui_smoke.py`, 224 tests drive screens *by id*). When a task intentionally changes rendered text or layout, update the pinned test in the SAME red-green cycle — never delete an assertion to make it pass without a replacement that pins the new behavior.

**Per-task loop:** failing test → verify fails for the right reason → minimal implementation → test passes → `python3 -m ruff check . && python3 -m mypy` → focused pytest → commit (`fix:`/`feat:`/`refactor:`/`docs:`/`chore:` prefix, reference the bug id).

**Per-phase gate:**
```bash
python3 -m ruff check . && python3 -m mypy && python3 -m pytest -q   # expect: all pass (count grows as tasks add tests)
```
For UI phases (2, 3, 4) additionally run the visual QA loop (recipe in cerebrum, session 2026-07-09):
```bash
uv venv /tmp/vela-qa/venv --python 3.11 && uv pip install --python /tmp/vela-qa/venv/bin/python -e ".[dev]" textual-serve pillow
# serve wrapper sets XDG_CONFIG_HOME/XDG_STATE_HOME/XDG_RUNTIME_DIR to short /tmp dirs; drive with Playwright;
# capture before/after at 142×38 AND ~100×26 AND 80×24-equivalent browser sizes.
```

**Bookkeeping after each fix (OpenWolf mandatory):** mark the matching bug-233…240 entry in `.wolf/buglog.json` (`fix:` description, keep `id`), append a `.wolf/memory.md` line, add cerebrum entries for any new gotcha discovered while fixing.

**Worker safety pattern (used repeatedly in Phase 1):** every `run_worker` that touches the network/agent gets `exit_on_error=False` AND its `group` registered in `OPTIONAL_MONITOR_GROUP_LABELS` (`src/vela/tui/app.py:412–423`) so `on_worker_state_changed` surfaces failures as warnings. (Established by bug-084/085/227.)

**Decisions pre-made for this plan** (change only with the owner):
- D1: Launching an hf_repo model that is not cached **warns** by default; `--require-cached` (CLI) / `require_cached_models: true` (config `launch:` block) upgrades it to a preflight failure. Rationale: don't break existing lab flows.
- D2: Root historical markdowns are **archived** under `docs/history/`, not deleted (git keeps history either way; archive keeps them greppable).
- D3: Unreferenced `artifacts/` content is **deleted** (regenerable / superseded); the 7 doc-referenced validation logs stay.
- D4: The remote-validation **cron schedule is removed**; `workflow_dispatch` stays.
- D5: Socket-dir precedence becomes `VELA_AGENT_RUNTIME_DIR > XDG_RUNTIME_DIR > XDG_STATE_HOME/vela > ~/.local/state/vela`. A running daemon on the legacy path is still honored (see Task 6.1 compat note).
- D6: No git-history rewrite in this plan (privacy finding is flagged to the owner; content-level scrubbing only).

---

## Phase 1 — Crash & hang class (bug-233, bug-234, worker safety)

*Everything in this phase is a small diff to `src/vela/tui/app.py`. These are the failures that turn a bad network moment into a dead TUI.*

### Task 1.1: Tolerate every `TargetCallError` in `_load_registry_from_agent` (bug-233)

**Files:**
- Modify: `src/vela/tui/app.py:4184–4186` (the `except TargetCallError` allowlist inside `_load_registry_from_agent`)
- Test: `tests/test_tui_smoke.py` (add near the existing unreachable-target startup tests — grep `agent-unreachable` in that file for the neighborhood and fake-agent fixture to copy)

**Step 1: Write the failing test.** Copy the structure of the nearest existing test that boots the app against a fake agent raising `TargetCallError("agent-unreachable", …)`, and make the fake raise code `"agent-auth-required"` instead. Assert: app reaches a mounted state (no exception escapes `run_test()`), and the remediation text for auth (see `src/vela/remediation.py:51`, `AGENT_AUTH_REQUIRED`) is rendered in the log/banner the same way the unreachable case is.

```python
async def test_startup_survives_unexpected_agent_error_code(...):
    # fake agent: handshake ok, list_configs raises TargetCallError(code="agent-auth-required")
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.is_running                      # did not crash out of on_mount
        assert "AGENT_AUTH_REQUIRED" in rendered_log_text(app)   # reuse the helper the unreachable test uses
```

**Step 2: Run it, watch it fail** with the traceback re-raised out of `on_mount` (that IS the bug):
`python3 -m pytest tests/test_tui_smoke.py -k unexpected_agent_error -x -q`

**Step 3: Minimal implementation.** Replace the allowlist with a catch-all that routes through the exact same handling the two allowlisted codes already get (mirror the adjacent branch — same helper, same banner path, return the same empty-registry sentinel):

```python
        except TargetCallError as exc:
            # Any agent error at registry load is a connection-surface problem,
            # never a reason to crash the TUI (bug-233).
            <same call the version-mismatch/agent-unreachable branch makes>(exc)
            return <the empty-registry value the existing branch returns>
```

**Step 4: Test passes. Step 5: Full smoke file passes** (`python3 -m pytest tests/test_tui_smoke.py -q`). **Step 6: Commit** `fix: never crash TUI on unexpected agent error codes at registry load (bug-233)`.

### Task 1.2: Register + flag the eight unsafe workers (bug-227 class)

**Files:**
- Modify: `src/vela/tui/app.py` — `run_worker` calls at lines ~1004 (reattach), 2693, 2701 (restart), 2631, 2639 (stop/kill), 3684, 3707, 3715 (quit paths); `OPTIONAL_MONITOR_GROUP_LABELS` at 412–423
- Test: `tests/test_tui_smoke.py`

**Steps (red-green ×2):**
1. Failing test A: a restart whose monitor path raises (fake agent: `restart` RPC succeeds, then the subscribe/monitor call raises) must NOT kill the app; assert `app.is_running` and a warning notification was produced (existing pattern: grep `on_worker_state_changed` tests).
2. Failing test B: structural — assert every `run_worker(` call in `app.py` passes `exit_on_error=False` (read the source in the test with a regex, mirroring the style of `tests/test_tui_screen_parsers.py`), and assert groups `{"restart", "engine-signal", "quit", "target-switch", "reattach"}` ⊆ `OPTIONAL_MONITOR_GROUP_LABELS`.
3. Implement: add `exit_on_error=False` to the eight spawns; add the five group labels with human-readable label strings matching the existing dict style.
4. Also guard the reattach payload: at `app.py:4377` replace `str(result["run_id"])` with a `.get()` + early `Unable to reattach…` UI error (the codepath for corrupt sidecars already exists — reuse its message helper).
5. Gate + commit `fix: crash-proof restart/reattach/stop/quit workers (bug-227 class)`.

### Task 1.3: Rebuild the Quit→Stop flow (bug-234)

**Files:**
- Modify: `src/vela/tui/app.py:3681–3701` (`confirm_stop_running` and `_exit_after_target_run_exit`), `:4423–4424` (`_target_stop_run` exception swallow)
- Test: `tests/test_tui_smoke.py`

**Behavior contract to implement (write tests for each bullet FIRST, one at a time):**
1. Confirming quit-stop **pops the ConfirmScreen immediately** and shows a `Stopping run …` notification/status.
2. The wait for `current_run_id` to clear is **bounded** (reuse the injected `clock`/timeout scaling helpers other tests use; 30s scaled default). On timeout: render an error banner (`Unable to stop run … — target unreachable?`) and DO NOT exit.
3. `_target_stop_run` no longer swallows exceptions silently when called from the quit path — it returns success/failure so (2) can render the failure instead of waiting forever.
4. Cancelling the confirm cancels the `quit` worker group (`self.workers.cancel_group(self, "quit")`) so no zombie exits the app later.
5. `action_quit` uses the same `_target_control_blocked` guard stop/kill/restart already use (grep it) — quitting while disconnected with a live run gets the disconnect banner instead of a dead modal.

Run the focused tests, then the full smoke file. Commit `fix: quit-stop pops modal, bounds the wait, cancels cleanly (bug-234)`.

### Task 1.4: Phase-1 gate

Run the full per-phase gate + update buglog entries 233/234 to fixed, memory.md line, commit `chore: phase 1 gate — crash/hang class closed`.

---

## Phase 2 — New Deployment wizard state machine (bug-235, bug-236)

### Task 2.1: One focus path — kill `_focus_current_step` (bug-235)

**Files:**
- Modify: `src/vela/tui/screens/new_deployment.py:1144–1156` (`_refresh_step`), delete `_focus_current_step`; keep `_focus_step_entry` (507–519)
- Test: `tests/test_new_deployment_screen.py`

**Steps:**
1. Failing test (probe-verified repro): push the screen **with a restored draft** (constructor arg used by the handoff round-trip — grep `last_draft` / draft restore in `app.py` push_screen callbacks), `await pilot.pause()`, assert focused widget is an `Input` or the step container — NOT a `Select`; then press `enter` and assert `screen.step == 2`.
2. Verify it fails: focus lands on `Select(id='new-deployment-runtime')` and Enter expands the overlay.
3. Implement: in `_refresh_step`, replace the `_focus_current_step()` call with `self._focus_step_entry()`; delete the dead method.
4. Green, then run the full 26-test wizard cluster: `python3 -m pytest tests/test_new_deployment_screen.py -q` and `python3 -m pytest tests/test_tui_smoke.py -k new_deployment -q`.
5. Commit `fix: wizard focus always routes through Enter-safe _focus_step_entry (bug-235)`.

### Task 2.2: "Download now" obeys the model source (bug-236a)

**Files:**
- Modify: `src/vela/tui/screens/new_deployment.py` (model step compose + `on_select_changed` for `#new-deployment-model-source`; the checkbox id — grep `download_now`)
- Test: `tests/test_new_deployment_screen.py`

**Behavior contract (test-first per bullet):**
1. Source = `Bare repo id` or `Adopt local path` → the Download-now `Field` wrapper is hidden (`display = False`) **and** its value reset to False. (Use the existing progressive-disclosure pattern from Create Build: toggle the `Field` wrapper's `.display` per a source→visible-fields map — cerebrum session 2026-06-09.)
2. Source = `Existing pin` / `Pin HF repo` → checkbox visible again (state stays independent).
3. Review/compose with a bare repo id and a previously-checked box must NOT emit "Download now requires a pinned model" (the checkbox was reset).
4. Dismiss payload shape unchanged (`_collect_spec` still emits the same keys — assert against the existing payload test).

Commit `fix: Download now hides+resets for unpinnable model sources (bug-236)`.

### Task 2.3: Empty registry defaults to a workable source (bug-236b)

**Files:**
- Modify: `src/vela/tui/screens/new_deployment.py` (model step `on_mount`/initial source selection)
- Test: `tests/test_new_deployment_screen.py`

**Contract:** when the models list passed to the screen has zero pinned entries: initial `Model source` = `Bare repo id` (so the Model input is immediately visible), and the pinned-model Select — when the user switches to `Existing pin` anyway — shows a disabled placeholder row `No pins on this target — pick "Pin HF repo →"` instead of the phantom `Custom model`. Test both. Commit `fix: wizard model step defaults to bare-repo when registry is empty (bug-236)`.

### Task 2.4: Per-step validation + honest breadcrumb (bug-236c)

**Files:**
- Modify: `src/vela/tui/screens/new_deployment.py` (`action_next_step`, the review-error rendering, `StepIndicator` usage), `src/vela/tui/widgets/step_indicator.py` (add an error/incomplete marker state)
- Test: `tests/test_new_deployment_screen.py`, `tests/test_tui_widgets.py`

**Contract (test-first per bullet):**
1. `Ctrl+N` from the Model step with no model resolvable (source=Existing pin, nothing selected, no bare id) **stays on the step** and renders `Model is required` in a `.step-error` Static adjacent to the model field (not only at panel bottom).
2. When review-time compose returns a validation error naming a field owned by step N, the wizard marks step N in the breadcrumb with the error glyph (`✗` amber, new StepIndicator state) instead of `✓`, and `Ctrl+B` navigation back to it is offered in the error text (`Model is required — Ctrl+B to Model`).
3. StepIndicator widget: red-green a `set_error(index)` API in `tests/test_tui_widgets.py` first.

Commit `feat: per-step wizard validation with honest breadcrumb states (bug-236)`.

### Task 2.5: Remove the debug leak + fix hint honesty (bug-236d + review-hint case)

**Files:**
- Modify: `src/vela/tui/screens/new_deployment.py` (grep the literal `sources ` render — it prints compose-response metadata `sources configured_ports, defaults` under the source helper; delete or demote to the debug JSONL log), review screen `BINDINGS`/`KeyHintBar` (1305–1352)
- Test: `tests/test_new_deployment_screen.py`

**Steps:** failing test asserting the rendered model step contains no `sources ` metadata line; failing test asserting the review screen's hint bar labels match its actual lowercase `b`/`f`/`s` bindings (render hints as `b Build  f Flags  s Save` — or rebind to capitals; pick lowercase to match the smoke suite's existing key presses). Also append `⏎ Next` to the wizard KeyHintBar (Enter-advance is real but undocumented — verified live). Implement, green, commit `fix: strip compose debug text from model step; truthful review/wizard key hints`.

### Task 2.6: Wizard pinned-model picker only offers referenceable entries (lifecycle M3)

**Files:**
- Modify: `src/vela/tui/app.py:2888` (`_open_new_deployment` model listing)
- Test: `tests/test_tui_smoke.py` (wizard cluster)

**Contract:** the list passed to the wizard includes only entries that `composer` can resolve (pinned registry entries), i.e. call `list_models` with `pinned_only: true` (add the param agent-side if absent — grep `pinned_only` in `src/vela/agent/local.py`; if the RPC lacks it, filter controller-side on the `pinned`/`source` field the payload already carries). Cache-scan rows may still be shown but only under a `cached (unpinned) — pin to use` disabled group. Test: fake agent returns one pinned + one scan row; assert the Select's options contain the pin and not a selectable scan row. Commit `fix: wizard only offers model refs that compose can resolve (M3)`.

### Task 2.7: Phase-2 gate

Full gate + visual QA of the wizard (fresh-open walk with Enter only; handoff round-trip walk; empty-registry walk; bare-repo + checked-box walk). Update buglog 235/236 → fixed. Commit `chore: phase 2 gate — wizard state machine closed`.

---

## Phase 3 — One loading/feedback convention for agent RPCs (bug-239)

### Task 3.1: A `_with_agent_busy` helper + status-badge busy state

**Files:**
- Modify: `src/vela/tui/app.py` (new helper near the other `_agent`* helpers; status badge classes — grep `status--loading` which already exists in CSS)
- Test: `tests/test_tui_smoke.py`

**Contract:** an async context helper that (a) sets the status badge to the pulsing loading state with a verb (`loading models…`), (b) restores it after, (c) on `TargetCallError` renders the standard remediation banner and re-raises a sentinel the caller can turn into "keep the dashboard". Test with a fake agent that delays: assert the badge class flips while in flight (drive with `pilot.pause(0)` steps), and with a raising agent: assert banner + no crash. Commit `feat: shared busy/error convention for agent RPCs`.

### Task 3.2: Wire every RPC-backed opener + manager verb through it

**Files:**
- Modify: `src/vela/tui/app.py` — `_open_new_deployment` (2857–2907, four sequential RPCs), `_open_model_manager` (2167–2185), `_open_build_manager` (1392–1403), `_open_flag_manager` (1350–1367), verify/repair/download/pin/remove verbs (grep `_reopen_manager_later` callers)
- Test: extend the Task 3.1 tests per surface (one test per opener is enough — same helper).

Also fix the swallow: in `_open_new_deployment`, replace the three `except Exception: {}` blocks (2883/2889/2895) with the helper's error path — the wizard opens only if `list_presets` succeeds; recipes/models/builds failures open the wizard **with a visible per-section warning row** (`builds unavailable: <code>`) rather than silently-empty dropdowns. Test: fake agent fails `list_builds` only → assert wizard opens and renders `builds unavailable`. Commit `fix: no silent RPC swallows; busy feedback on n/m/b/F and manager verbs (bug-239)`.

### Task 3.3: Offline-aware empty states (the "No configs yet" lie)

**Files:**
- Modify: `src/vela/tui/app.py` (Configs card renderer — grep `No configs yet`; it must branch on target connection state), same for manager empty states (`No builds yet`, models)
- Test: `tests/test_tui_smoke.py`

**Contract:** when `connection_state != connected`, the Configs card renders `target unreachable — configs unknown · R reconnect` (amber), NOT the first-run copy. Verified-live repro: switch to a dead target, card must not say "No configs yet". Test with the unreachable fake. Commit `fix: empty states distinguish offline from empty (bug-239/UX)`.

### Task 3.4: Verify verbs return you where you were

**Files:**
- Modify: `src/vela/tui/app.py:2364–2376` (`_verify_model`) to end with the same `_reopen_manager_later` the build path uses (1599–1611)
- Test: `tests/test_tui_smoke.py` (mirror the existing build-verify reopen test for models)

Commit `fix: model verify reopens Model Manager like build verify does`.

### Task 3.5: Phase-3 gate

Full gate + visual QA (open wizard/managers against a fake-slow agent; dead-target empty states). Buglog 239 → fixed. Commit `chore: phase 3 gate — feedback convention everywhere`.

---

## Phase 4 — Layout system (bug-237 + screen polish)

*Generalize the proven Flag Manager pattern (cerebrum 2026-06-13): panel `width: 96%; height: auto; max-height: 96%; overflow-y: auto`, long lists in `VerticalScroll` (`height: auto; max-height: N`, `.can_focus = False`), stacked full-width sections, preserved ids/substrings/dismiss payloads.*

### Task 4.1: Shared modal frame tokens

**Files:**
- Modify: `src/vela/tui/theme.py` (add `MODAL_PANEL_CSS` constant or equivalent token strings)
- Test: `tests/test_tui_widgets.py` (structural: constant exists and encodes `96%`/`auto` rules — keeps future screens from re-hardcoding)

Commit `feat: shared modal frame tokens in theme.py`.

### Task 4.2: Target Manager → full-width stacked rebuild (+ live state, + reconnect feedback)

**Files:**
- Modify: `src/vela/tui/screens/target_manager.py` (CSS `width: 100` at :52 → shared frame; `Horizontal(MasterDetail)` → stacked list-above-detail; constructor snapshot at 100–109; `action_reconnect` 144–147; `_FOOTER_HINTS` 25–35 add `v view all`)
- Modify: `src/vela/tui/app.py` (push-site: pass a refresh callback / have the app re-push state — mirror how Flag Manager receives updates)
- Test: `tests/test_target_manager_screen.py` (this file pins the list-row format `"{marker} {dot} {name}  {transport}  {host}"` and detail substrings — KEEP those; add layout + refresh tests)

**Test-first contract:**
1. Layout: panel region width ≥ 90% of an 80×24 and a 140×40 `run_test` size; list is a `VerticalScroll` stacked above the detail; nothing clipped (assert the footer hint bar's last hint `Esc Close` is inside the panel region — this pins the "Esc Clos" clip fix).
2. Live state: screen exposes `refresh_target_state(payload)`; after the app's reconnect worker finishes it calls it; test drives a fake reconnect and asserts the detail flips `inactive → connected` **without closing the screen**.
3. Reconnect feedback: pressing `R` immediately renders `reconnecting…` in the detail connection row (before the worker resolves).
4. All existing substring/payload tests stay green unmodified.

Commit `feat: Target Manager full-width stacked + live refresh + reconnect feedback (bug-237)`.

### Task 4.3: Model Manager → full-width rebuild with a readable row grammar

**Files:**
- Modify: `src/vela/tui/screens/model_manager.py` (CSS `width: 104` at :33; row renderer)
- Test: `tests/test_model_manager_screen.py` (row-format substrings WILL change — update pins in the same cycle)

**Row grammar (test-first):** one line per entry, no wrapping at ≥100 cols:
`{dot} {display_name:<truncate-ellipsis} {source_tag} {cache_state} {size} {sha8}` where `size` renders `—` when unknown/zero-weights (`files: N unknown` → `—`), `<0.1 GB` for small-but-real, and `sha8` is the 8-char short sha (full sha stays in the detail pane only). Detail pane keeps every existing `key: value` substring (pinned tests). At <100 cols the row drops `sha8` then `size` (test both widths with `run_test(size=…)`).
Commit `feat: Model Manager full-width + scannable rows (bug-237)`.

### Task 4.4: Build Manager + Review screen + Target Edit + Help widths

**Files:**
- Modify: `src/vela/tui/screens/build_manager.py:32` (`width: 96`), `new_deployment.py:1270` (review `width: 92`), `target_edit.py:24` (`width: 96`), `help.py:31` (`width: 82`)
- Test: respective `tests/test_*_screen.py` files + `tests/test_tui_smoke.py -k help`

**Contract per screen (one red-green each):** shared frame tokens; at 80×24 nothing clips (panel ≤ terminal width; footer hints inside panel). Help additionally: title becomes `Help — keys & markers` (kill the `HelpScreen` class-name leak; update the pinned substring test), and the Markers legend lines wrap as label-value pairs that don't orphan glyphs (set the legend width to the panel content width). Build Manager empty state shows only applicable hints (`n New  a Adopt  Esc Close`) — pass the hint list conditionally to `KeyHintBar` exactly like the non-empty path.
Commit `fix: all modals fit 80 columns; Help titled for humans (bug-237)`.

### Task 4.5: Adaptive top chrome (header)

**Files:**
- Modify: `src/vela/tui/app.py:467–528` (top-chrome CSS fixed widths), `_render_active_model` (4663–4718), the floating status pill widget/CSS (grep `status--` and the pill's absolute offset), `_apply_responsive_layout` (4875–4893)
- Test: `tests/test_tui_smoke.py` (new layout-measurement tests via `run_test(size=…)`, mirroring the FR-24 responsive tests)

**Contract (test-first per width):**
1. The pill is IN-FLOW in the header row (no absolute offset overlap — it currently floats over `model:`; give it a fixed slot between title and clock), full border visible at every width ≥ 80.
2. Priority collapse right-to-left: status badge > target > model (ellipsized, `text-overflow: ellipsis` via Rich truncation in `_render_active_model` — no mid-glyph cuts) > URL > clock. At 80×24: badge + target visible; at 100: + model; at 120: + URL (truncated middle `http://…:8765`); at 140: everything.
3. The server URL renders dim/absent unless phase is READY/DEGRADED (kill the lit-cyan-URL-while-IDLE/STOPPED lie) — assert style/absence at IDLE and presence at READY (reuse the READY fixture flow).
4. `build: unmanaged ○ · model: …` never wraps to a second header line (assert header height == its fixed row count at 80/100/142).

Commit `feat: adaptive truthful header; pill in-flow; URL only when live (bug-237)`.

### Task 4.6: Sidebar vertical fit

**Files:**
- Modify: `src/vela/tui/app.py:505–507` (fixed panel heights) — panels get `height: auto` with `max-height`, inside a `VerticalScroll` column; `_apply_responsive_layout` keys on height too
- Test: `tests/test_tui_smoke.py`

**Contract:** at 100×30 all four sidebar cards render at least their title + first line (nothing fully clipped — the `N lines · autoscroll` card was invisible at ≤30 rows); at 142×38 the cards hug content (Phases card with 1 line is ~3 rows, not 11). Existing FR-24 width-breakpoint tests stay green.
Commit `fix: sidebar hugs content and fits short terminals (bug-237)`.

### Task 4.7: Context-sensitive footer that always fits

**Files:**
- Modify: `src/vela/tui/app.py` (footer hint assembly — grep the footer/KeyHintBar hint list; add state-conditional filtering + priority ordering)
- Test: `tests/test_tui_smoke.py`

**Contract:**
1. At IDLE with no run: `s Stop  K Kill  r Restart  R Reconnect  / Search  f Filter  p Pause  w Wrap  g/G Top/Bottom` are HIDDEN; `? Help` and `q Quit` ALWAYS render (assert at 80 cols).
2. With a live attached run: control keys return, log keys return; if width can't fit all, drop from a defined priority tail but never `? Help  q Quit` (assert presence at 80/100/142 with a running fake).
3. Help screen's grouped bindings remain the full reference (unchanged).

Commit `feat: footer shows only applicable actions; Help/Quit always visible (bug-237)`.

### Task 4.8: Small-screen polish batch

**Files:**
- Modify: `src/vela/tui/screens/config_picker.py` (:19–25 add `height: auto`; list → `VerticalScroll` with keyboard-followed selection; `action_accept` no-match guard at 86–88; empty-state copy at :116), `src/vela/tui/app.py:4800,4818` (narrow-overlay copy), `src/vela/tui/widgets/preset_chips.py` (keyboard selection), global `Checkbox` CSS (theme tokens: visible unchecked `[ ]` / checked `[✓]` glyph styling), `confirm.py`/`log_prompt.py`/`target_edit.py` legacy token → theme.py migration
- Test: respective screen test files

**Contract (one red-green per bullet):**
1. Config Picker panel hugs content (`height: auto`), selection stays in view when arrowing past the fold (list in `VerticalScroll`, call `scroll_visible` on the marker line's region — or convert rows to an `OptionList` if substring pins allow; prefer minimal: keep Static + programmatic scroll).
2. Enter with zero filter matches keeps the picker open with `no match — Esc to close` hint (no silent dismiss).
3. Empty-state copy says `close and press n on the dashboard` (the focused filter input eats `n` — verified).
4. Narrow overlay: kill literal `Sidebar overlay` and spec-note copy; render the same Configs-card content the wide sidebar shows, titled `Config`.
5. Checkboxes everywhere show an unambiguous unchecked state (`[ ]` dim vs `[✓]` green) — test via the wizard Download-now field and Flag Manager Changed-only.
6. PresetChips: Left/Right + Enter selects a chip when the chip row is focused (keep click behavior).
7. Confirm/log-prompt/target-edit use theme.py tokens (visual-only; keep ids/labels — snapshot substrings unchanged).

Commit `fix: picker scroll+guards, honest narrow overlay, visible checkboxes, keyboard chips (bug-237 tail)`.

### Task 4.9: Run-lifecycle feedback polish

**Files:**
- Modify: `src/vela/tui/app.py` — transient progress clear (grep `#progress` handlers; clear/hide on terminal phase), run-separator write into the RichLog on each launch (`── run <id> · <config> · <target> ──` dim line, written to display only, NOT durable logs), stop/kill result notification + log line (`STOPPED by operator` — the intent recorder from cerebrum already knows), phase stepper terminal state (STOPPED/CRASHED row replaces the stale `READY ✓` contradiction: keep READY history but append the terminal marker)
- Test: `tests/test_tui_smoke.py` (launch→ready→stop fixture flow already exists — extend it)

**Contract:** after stop: progress panel hidden; a `Stopped <run-id>` toast fired; display log's last line is the operator-stop line; phases panel shows `■ STOPPED` (or `✗ CRASHED` for crash fixture) as the final row; second launch writes the separator line first. Durable-log scrubbing tests must stay untouched (display-only writes).
Commit `feat: unmistakable run start/stop feedback (bug-237 tail)`.

### Task 4.10: Flag Manager title order (single nit)

**Files:** `src/vela/tui/screens/flag_manager.py` (compose order: title `Flag Manager` + `build:`/`config:` context row FIRST, then preset select + changed-only row, then helper)
**Test:** `tests/test_flag_manager_screen.py` — assert the first rendered row of the panel contains `Flag Manager` (keep all other pinned substrings).
Commit `fix: Flag Manager title precedes controls`.

### Task 4.11: Phase-4 gate + visual evidence

Full gate; visual QA at 142×38, ~100×26, 80×24-equivalent: dashboard idle/running/stopped, every manager, wizard, help, picker. Save shots to `.playwright-mcp/shots/after-phase4/`. Update buglog 237 → fixed. Commit `chore: phase 4 gate — layout system pass complete`.

---

## Phase 5 — Model-lifecycle contract (bug-240 + lifecycle H1–H4, M1–M7)

### Task 5.1: Docker pull that can actually pull (bug-240 / H1)

**Files:**
- Modify: `src/vela/engine/docker_runtime.py:239–247` (`_run_docker` hard `timeout=10`), `:138–177` (`prepare_docker_image`/`pull_docker_image`), `src/vela/engine/supervisor.py:197` (catch scope)
- Test: `tests/test_command_builder.py` (docker error classifier tests live here — grep `docker_error_classifier`) + `tests/test_docker_supervisor.py` with `tests/fakes/fake_docker.py`

**Steps:**
1. Failing test A: fake docker whose `pull` sleeps past the command timeout → `prepare_docker_image` must classify as `IMAGE_PULL_FAILED` (new `DockerCommandError` kind), not raise `TimeoutExpired`.
2. Failing test B: supervisor with `pull: missing` and the slow fake → writes a classified failure to the run log + exit-status file; no orphaned container (mirror the bug-228 eviction test).
3. Implement: `_run_docker(cmd, timeout=10)` gains a per-call timeout param; pull paths pass `timeout=None` (rely on the supervisor's own lifecycle for cancellation) or `VELA_DOCKER_PULL_TIMEOUT_SECONDS` env default `1800`; wrap `subprocess.TimeoutExpired` → `DockerCommandError("image-pull-timeout", …)`; widen the supervisor catch to include it.
4. Also emit pull progress lines into the run log sink (docker pull writes progress to stdout — feed it through the existing scrubbed sink so the TUI shows *something* during a 10 GB pull).
5. Gate + commit `fix: docker pull gets a real timeout + classified failures + logged progress (bug-240)`.

### Task 5.2: Prelaunch cache check + post-READY registry refresh (H2, decision D1)

**Files:**
- Modify: `src/vela/agent/local.py:5030–5076` (`_validate_model_handoff_prelaunch`), the READY transition hook (grep `_track_post_ready_probe`), `src/vela/config/schema.py` (`launch.require_cached_models: bool = False`), `src/vela/cli.py` (`--require-cached` on `run`/`smoke`/`smoke-tui`)
- Test: `tests/test_agent_client.py` (prelaunch cluster — grep `prelaunch`) + `tests/test_config_loader.py` for the schema field

**Contract (test-first):**
1. hf_repo handoff with `cache_state != cached` → launch result carries a structured warning (`model-not-cached`, with size if known) surfaced by the TUI banner path and CLI stderr.
2. Same + `require_cached_models: true` (or CLI flag) → preflight FAILURE `model-not-cached` before spawn.
3. After READY, the agent re-scans that entry and updates `cache_state` (vLLM just downloaded it) — assert registry file updated via the existing refresh helper (`model_registry.py:375–419`).
4. Bare `model:` configs (no `model_ref`): warning only when `require_cached_models` set (can't check registry for unpinned — the warning text says so).

Commit `feat: launch warns/blocks on uncached models; registry learns from READY (H2, D1)`.

### Task 5.3: Composer defaults an HF-cache mount for docker + hf_repo (H3)

**Files:**
- Modify: `src/vela/engine/composer.py:608–615` (generic docker compose), `src/vela/agent/local.py` (agent must resolve its default HF cache dir — grep the cache-scan helper for the path source)
- Test: `tests/test_deployment_composer.py`

**Contract:** composing runtime=docker with an hf_repo model (pin or bare) and no explicit `docker.hf_cache` → the draft sets `docker.hf_cache: <agent-resolved HF cache dir>` and the review preview shows the mount; explicit user value always wins; local-path/url models unaffected. Preflight warns (`hf_repo model with no HF cache mount`) if a config still lacks it at launch (covers hand-written YAML). Commit `feat: docker composes mount the agent HF cache by default (H3)`.

### Task 5.4: `vela deploy create` surfaces preflight (H4)

**Files:**
- Modify: `src/vela/cli.py:1477–1495`
- Test: `tests/test_cli_run.py` (deploy-create cluster — grep `deploy_create`)

**Contract:** text mode prints each preflight failure line (`preflight: port-in-use — 8000 busy`) and exits 2 without saving unless `--force` (then saves + prints warnings, exit 0); `--json` unchanged except a top-level `preflight_ok` field. Commit `fix: deploy create fails loudly on failed preflight (H4)`.

### Task 5.5: Referenceable pins: repo-id resolution + upsert (M4)

**Files:**
- Modify: `src/vela/engine/model_registry.py` — `_entry_for_reference` (1018–1066) to match unique `repo_id`; pin path (1123–1126, 1775–1797) to default `display_name = repo_id` for hf pins and to UPSERT (same repo_id + same revision intent → update entry in place, preserve entry_id) with `--new` escape hatch
- Test: `tests/test_agent_client.py` (model registry cluster)

**Contract (test-first):** `pin org/repo` then `model_ref: org/repo` resolves; ambiguous repo_id (two entries) → error listing candidates; re-pin updates rather than duplicating; CLI/TUI display unchanged elsewhere (list tests). Commit `feat: model_ref accepts repo ids; pin upserts (M4)`.

### Task 5.6: Verify means verified (M1)

**Files:**
- Modify: `src/vela/engine/model_registry.py` — `_hf_model_status` (1507–1543) gains an online manifest comparison (reuse the `HfApi.model_info(files_metadata=True)` siblings list already fetched at pin time — store `expected_files`/`expected_size` on the entry at pin/refresh); `_apply_cached_model_payload` (664–673) must NOT promote `partial → cached` when the file inventory is short; deep verify (1438–1485) reports `baseline_established` as a WARN-level line in CLI/TUI output
- Test: `tests/test_agent_client.py`

**Contract:** entry with 1 of 12 expected shards → shallow verify FAILs (`missing 11 of 12 weight files`); offline (no expected manifest stored) → current behavior + explicit `presence-only check (no manifest)` note; cancelled download stays `partial` across refresh; first deep run prints `baseline established — rerun to compare`. Commit `fix: verify checks inventory against upstream manifest; partial stays partial (M1)`.

### Task 5.7: Revision single-source-of-truth (M2)

**Files:**
- Modify: `src/vela/agent/local.py:2909–2927` (cached short-circuit must compare the *requested* revision first), `src/vela/engine/model_registry.py:664–673` (download with explicit revision override does NOT mutate the pin's `commit_sha`; it records `last_download_revision` instead)
- Test: `tests/test_agent_client.py`

**Contract:** `download --revision other` on a cached entry actually downloads `other`; pin sha unchanged afterward (assert); entry detail shows both pin sha and last-download revision when they differ, and `verify` warns on divergence. Commit `fix: download --revision is honored and never rewrites the pin (M2)`.

### Task 5.8: `--commit-sha` pins stay validated (M5)

**Files:**
- Modify: `src/vela/engine/model_registry.py:1223–1224`
- Test: `tests/test_agent_client.py`

**Contract:** with `--commit-sha`, `model_info` still runs best-effort for gating/existence (network errors downgrade to the existing `remote-only-unresolved` warning path; the given sha is trusted, not re-resolved); gated repo detected → `token_required: true` so the docker env contribution carries `HF_TOKEN`. New `--offline` flag skips the call explicitly and records `validated: false`. Commit `fix: sha pins still detect gating so HF_TOKEN reaches containers (M5)`.

### Task 5.9: Lifecycle tail batch (M6, M7, L2, L3)

**Files & contracts (one red-green each):**
- `src/vela/agent/local.py:1388–1391`: run `check_build_launch_integrity` on the RESOLVED handoff's build id (default/active build included), not only explicit `command.build` (M6). Test: tampered active build → launch preflight fails.
- `src/vela/engine/preflight.py:177–193` + `model_registry.py`: disk preflight probes the resolved HF cache dir; when entry `size_bytes` known and download expected, require `free > size × 1.1` (M7). Download path gets the same precheck. Tests in `tests/test_agent_client.py`.
- `src/vela/engine/model_registry.py:1507–1515` + `local.py:2931–2946`: URL entries verify → `ok (launch-time source; nothing to verify)` matching download's wording (L3).
- `src/vela/engine/profile.py:117–157`: docker-runtime configs skip host `vllm --help` flag filtering (use the bundled profile map) (L2). Test in `tests/test_command_builder.py`.

Commit `fix: lifecycle tail — default-build integrity, disk prechecks, url verify, docker profiles`.

### Task 5.10: Phase-5 gate

Full gate; buglog 240 → fixed (+ note H2–H4/M-series closures on the 2026-07-09 review entries); update `docs/builds-and-models.md` + `docs/docker-runtime.md` + `docs/configuration.md` for: cache-mount default, `require_cached_models`, pull timeout env, verify semantics, revision rules — `tests/test_docs.py` pins some of this prose; update pins in the same commits. Commit `chore: phase 5 gate — lifecycle contract honest end-to-end`.

---

## Phase 6 — Daemon & discovery honesty (bug-238)

### Task 6.1: Socket dir respects XDG state (D5)

**Files:**
- Modify: `src/vela/agent/daemon.py:41–48` (`default_agent_runtime_dir`)
- Test: `tests/test_agent_daemon.py`

**Contract (test-first):** precedence `VELA_AGENT_RUNTIME_DIR > XDG_RUNTIME_DIR > $XDG_STATE_HOME/vela > ~/.local/state/vela`. Compat: `inspect_agent_daemon`/client connect first probes the new resolved path, then the legacy `~/.local/state/vela/agent.sock` if different and alive (so existing daemons aren't orphaned mid-upgrade); `vela agent status` prints which path is in use. Update `tests/conftest.py` isolation fixture if it relies on the old order (grep `XDG_RUNTIME_DIR` there). Commit `fix: agent socket honors XDG state isolation with legacy fallback (bug-238, D5)`.

### Task 6.2: Controller↔daemon version-mismatch detection

**Files:**
- Modify: `src/vela/agent/daemon.py` (identity file already records version — grep `agent.json` fields), `src/vela/transport/factory.py`/socket client handshake path, `src/vela/tui/app.py` + `src/vela/cli.py` surfacing
- Test: `tests/test_agent_daemon.py` + `tests/test_cli_run.py`

**Contract:** handshake compares `vela.__version__` + git describe when available; mismatch → single warning line/banner `local daemon is running vela 0.1.0+abc (started Jun 9) — restart with: vela agent restart` on first contact (not every call). The month-stale-daemon trap becomes visible. Commit `feat: surface stale local daemon version at handshake (bug-238)`.

### Task 6.3: Unknown-config errors name the searched world

**Files:**
- Modify: `src/vela/cli.py` (unknown-config error paths — grep `Unknown config`), `src/vela/agent/local.py` `list_configs`/error payload to include `searched_dirs` + agent `cwd`
- Test: `tests/test_cli_run.py`

**Contract:** `vela run nope` prints:
```
Unknown config: nope
Searched (agent 'local', cwd /Users/x/somewhere): /Users/x/somewhere/configs, ~/.config/vela/configs
Hint: the local agent keeps its first working directory — `vela agent restart` if you launched it elsewhere.
Available configs: none
```
Test asserts dirs + hint text. Commit `fix: unknown-config errors show searched dirs and the daemon-cwd hint (bug-238)`.

### Task 6.4: Transport-aware remediation + daemon stderr capture

**Files:**
- Modify: `src/vela/remediation.py:40–48` (agent-unreachable branches on transport: local → `check 'vela agent status'; log at <path>`; ssh → existing setup-ssh text), `src/vela/agent/daemon.py:105` (`stderr=subprocess.DEVNULL` → append to `<runtime_dir>/agent-start.err`, path included in start-failed errors)
- Test: `tests/test_remediation.py` (existing style: one test per remediation string) + `tests/test_agent_daemon.py` (failed spawn writes the err file and the error names it)

Commit `fix: local agent failures get local remediation and a stderr file (bug-238)`.

### Task 6.5: Config discovery honors XDG_CONFIG_HOME

**Files:**
- Modify: `src/vela/config/loader.py:53`
- Test: `tests/test_config_loader.py`

```python
def _default_config_dir(home_path: Path) -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else home_path / ".config"
    return base / "vela" / "configs"
```
(Adapt to the file's actual shape at :40–53; keep discovery order.) Docs claim this already works (`docs/configuration.md:101–102`) — code catches up to docs. Commit `fix: config discovery honors XDG_CONFIG_HOME (docs already promised it)`.

### Task 6.6: Phase-6 gate

Full gate; buglog 238 → fixed. **Manual step for the owner:** `vela agent stop` on the Mac (June-9 daemon) and kill leaked PID 20556. Commit `chore: phase 6 gate — daemon honesty`.

---

## Phase 7 — CLI friendliness

### Task 7.1: Docstring all 52 commands

**Files:** `src/vela/cli.py` (every `@app.command`/sub-app command), sub-app `add_typer(..., help=…)` lines 49–62
**Test-first:** in `tests/test_cli_run.py`, a structural test: build the Typer app, walk `app.registered_commands` + groups, assert every command has non-empty `help`/docstring. Then write the ~52 one-liners (verbs first, plain language; `smoke` = "Launch, wait for READY, verify /v1/models, then stop — agent-side"; `smoke-tui` = "Same gate driven through the real TUI headlessly").
Commit `docs: every CLI command self-documents (was 42 blank)`.

### Task 7.2: Run lifecycle trio: `vela runs list` / `vela stop` / `vela logs`

**Files:**
- Modify: `src/vela/cli.py` (`runs` sub-app + root `stop`; new `logs`), thin wrappers over existing RPCs `discover_runs`, `stop`, `reattach`/`tail_detached` (see `docs/agent-rpc.md:60–74`)
- Test: `tests/test_cli_run.py` with the fake agent fixtures

**Contract (test-first per command):**
- `vela runs list [--target]` → table: run_id, config, phase, ready url, started, pid-safe identity (NO sidecar paths/PIDs on the wire — respect the bug-225 scrubbing rule).
- `vela stop RUN_ID|CONFIG [--target] [--kill]` → resolves unique live run (ambiguity → list candidates, exit 2), confirms result.
- `vela logs RUN_ID [--follow --lines N]` → replays scrubbed log via the agent (never reads target paths directly).
- Detached-launch message (cli.py:2891) becomes: `detached run started: <id>\n  watch:  vela logs <id> --follow\n  stop:   vela stop <id>`.

Commit `feat: CLI run lifecycle — runs list / stop / logs (closes the TUI-only gap)`.

### Task 7.3: Default target: `VELA_TARGET` + `vela targets use`

**Files:** `src/vela/cli.py` (shared `--target` default resolution helper: explicit flag > `VELA_TARGET` > persisted default > `local`), `src/vela/config/targets.py` (persist `default_target` in targets.yaml), `targets use NAME` + `targets use --clear`
**Tests:** `tests/test_cli_run.py` + `tests/test_targets.py` (round-trip persistence; unknown name rejected; `targets list` marks the default with `*`).
Commit `feat: default target via VELA_TARGET / vela targets use`.

### Task 7.4: Deduplicate command surface

**Files:** `src/vela/cli.py`
**Contract (structural test):** `deploy list`, `preview`, `model add` become `hidden=True` aliases delegating to `list`, `run --preview`, `model pin`; visible help shows exactly one canonical verb per operation; `config edit` help says "open in $EDITOR"; `deploy edit` help says "set fields non-interactively (--set)". No behavior change (existing tests prove aliases still work).
Commit `refactor: one canonical command per operation; aliases hidden`.

### Task 7.5: Consistent error + empty-state text

**Files:** `src/vela/cli.py` (unknown build/model paths — grep `unknown build`, `unable to read model registry`), list commands' empty output
**Contract (test-first):** unknown build/model errors match the config shape (`Unknown build: X` + `Available builds: …`); registry-miss no longer claims an I/O failure; `vela list` on empty prints `no configs found in: <dirs> — create one with 'vela deploy create' or the TUI (n)`; `build list`/`model list` similar; `doctor` prints a `next:` line when 0 targets configured.
Commit `fix: uniform unknown-X errors and helpful empty states`.

### Task 7.6: systemd unit correctness

**Files:** `packaging/systemd/vela-agent.service`
```ini
Documentation=https://github.com/bgconley/vela
ExecStart=/usr/local/bin/vela agent run    # ← absolute path; adjust per install (uv tool: %h/.local/bin/vela)
# Environment=HF_TOKEN=hf_xxx              # required for gated models on this target
# EnvironmentFile=%h/.config/vela/agent.env
```
**Test:** `tests/test_agent_daemon.py`/`test_branding.py` pin the unit content (grep which one asserts it) — update pins.
Commit `fix: systemd unit points at vela, absolute ExecStart, HF_TOKEN hook (M7-cli)`.

### Task 7.7: Phase-7 gate

Full gate. Commit `chore: phase 7 gate — CLI reads as a product`.

---

## Phase 8 — Docs & README golden paths

### Task 8.1: README restructure

**Files:** `README.md`
**Contract (update `tests/test_docs.py` pins in the same commit — it asserts README covers the v1 paths):**
1. Two explicit quickstarts: **"Installed tool"** (uv tool install → `vela` → TUI-first, targets bootstrap) and **"Cloned repo"** (pip install -e → fake-child demo works because `./configs` exists — say so explicitly, including "run from the repo root").
2. Remote-target golden path = `vela targets bootstrap gpu-node --host user@host --install` → `vela targets test gpu-node`; hand-edited targets.yaml demoted to reference.
3. Config-discovery section calls out the `configs/` SUBDIR of `~/.config/vela` explicitly.
4. Fix leftover "the loader stores/copies" prose (README:222 area); label gpu-workflow.md link as maintainer runbook.

### Task 8.2: `docs/tui.md` generated from BINDINGS

**Files:** Create `scripts/gen_tui_docs.py` (walks `VelaApp.BINDINGS` + each screen's `BINDINGS`/hints → markdown table), create `docs/tui.md`, test `tests/test_docs.py::test_tui_doc_matches_bindings` regenerates in-memory and diffs (drift-proof, same pattern as the workflow content pins).

### Task 8.3: `docs/troubleshooting.md` from remediation.py

**Files:** Create `docs/troubleshooting.md` — one section per `remediation.py` error kind (AGENT_UNREACHABLE local/ssh, AGENT_NOT_INSTALLED, AGENT_AUTH_REQUIRED, gated-auth, preflight failures, model-not-cached, image-pull-timeout, daemon-cwd hint), each: symptom → cause → exact fix command. Pin with a `test_docs.py` test asserting every remediation kind name appears.

### Task 8.4: Phase-8 gate

`python3 -m pytest tests/test_docs.py -q` + full gate. Commit `docs: golden paths, TUI reference, troubleshooting (phase 8)`.

---

## Phase 9 — Repo diet (mostly `git mv`/`git rm --cached`; decisions D2–D4, D6)

### Task 9.1: Untrack OpenWolf churn + scraped docs + gitignore

```bash
git rm --cached .wolf/token-ledger.json .wolf/memory.md .wolf/buglog.json
git rm -r --cached .firecrawl
printf '%s\n' '.wolf/token-ledger.json' '.wolf/memory.md' '.wolf/buglog.json' '.wolf/designqc-captures/' '.firecrawl/' '.playwright-mcp/' >> .gitignore
git commit -m "chore: untrack session bookkeeping and scraped docs (was 30% of repo churn)"
git gc
```
Verify: `git status` clean of `.wolf` churn; OpenWolf still functions (files remain on disk).

### Task 9.2: Root markdown migration (D2)

```bash
mkdir -p docs/specs docs/history/{reviews,sessions,plans}
# living specs (9): vllm-tui-loader-spec-v2-CANONICAL, vllm-agent-architecture-spec-v1, vllm-build-management-spec-v1,
#   vllm-model-management-spec-v1, vela-docker-runtime-spec-v1, vela-deployment-composer-spec-v1,
#   vela-onboarding-ux-spec-v1, vela-deployment-composer-user-stories-v1, vela-docker-runtime-examples-v1  → docs/specs/
# review/punchlist iterations (17) → docs/history/reviews/ ; session handoffs (4) → docs/history/sessions/
# implementation plans (2) + docs/superpowers/plans/2026-06-03-* → docs/history/plans/
git mv vela-docker-runtime-examples-v1.md docs/specs/   # …and the rest per the groups above
```
**MUST update in the same commit:** `tests/test_docs.py:122` → `_read("docs/specs/vela-docker-runtime-examples-v1.md")` (the ONLY code pin — verified). Run `python3 -m pytest tests/test_docs.py -q`. Root ends with `README.md CHANGELOG.md CLAUDE.md LICENSE pyproject.toml uv.lock`.
Commit `chore: archive historical specs/reviews under docs/ (root: 38 → 3 markdowns)`.

### Task 9.3: Prune artifacts (D3)

Delete `artifacts/tui-screenshots/`, `artifacts/screenshots/`, `artifacts/visual-qa/`, and the 21 `artifacts/remote-validation/` logs NOT named in README/docs (keep the 7 referenced: both `2026-06-13T01-*`, `2026-06-04T20-04-41Z-*`, `2026-06-04T20-34-19Z-*`, `2026-06-06-*-bf16-9b107b4`, `2026-06-06-*-fp8-d67b3a6`, `2026-06-10T07-47-58Z-*`). Verify `python3 -m pytest tests/test_remote_workflow.py tests/test_docs.py -q` (they pin doc *strings*, not files — but confirm). Commit `chore: prune unreferenced artifacts (−5 MB, −80 files)`.

### Task 9.4: CI honesty (D4)

**Files:** `.github/workflows/remote-validation.yml` (delete the `schedule:` block, keep `workflow_dispatch`; cancel the queued run via `gh run cancel`), `pyproject.toml` dev extras gain `mypy>=1.8`, `.github/workflows/ci.yml` lint step → `pip install -e ".[dev]"`. **Test pin:** `tests/test_remote_workflow.py` content-asserts the workflow file (bug-211) — update the pin in the same commit. Also `git rm scripts/smoke_fake_child.sh` (zero references). Commit `chore: kill zombie cron, pin mypy in dev extras, drop dead script`.

### Task 9.5: Refresh OpenWolf anatomy

```bash
openwolf scan   # respects .wolf/config.json excludes; updates anatomy.md (was ~84 files stale)
```
Commit `chore: rescan anatomy after reorg`.

---

## Phase 10 — Structural splits (optional; schedule after 1–9 are merged)

Ordered by risk; each is its own plan-worthy chunk — do NOT interleave with feature work:
1. **tests/test_tui_smoke.py (15,234 lines)** → move screen clusters into the existing `tests/test_<screen>_screen.py` files; shared fakes (`RecordingConfigAgent` hierarchy, `_isolate_hf_cache_scan`) → `tests/fakes/`. Mechanical; suite count is the invariant (1,133+ before == after).
2. **src/vela/cli.py (3,527 lines)** → `src/vela/cli/` package, one module per sub-app (`agent, build, config, deploy, model, runs, targets, root`); `vela.cli:main` export preserved (pyproject `[project.scripts]` pins it; `tests/test_branding.py` greps it).
3. **src/vela/agent/local.py (5,653 lines)** → extract build-install subsystem (~3869–4540) and diagnostics (~3624–3775) into `agent/build_install.py` / `agent/diagnostics.py`; then convert the 63-branch `if method ==` dispatch to a handler table. 223 tests pin the wire behavior — pure moves only.
4. **src/vela/tui/app.py (5,700 lines)** → extract the module-level wire-payload adapters (177–405) to `tui/wire.py` ONLY. Defer any `VelaApp` class split — highest risk, lowest payoff.

---

## Definition of done (whole plan)

1. `python3 -m ruff check .` clean · `python3 -m mypy` clean (overrides list not grown) · full `python3 -m pytest -q` green.
2. `.wolf/buglog.json`: bug-233…240 all carry real `fix:` text; `.wolf/cerebrum.md` has the new-gotchas entries; anatomy rescanned.
3. Visual re-run of the 2026-07-09 walkthrough script (first-run, dead-target switch, full wizard incl. handoff round-trip, managers, fake-child launch→READY→stop, 80/100/142-col dashboards) with after-shots saved — zero of the captured defects reproduce.
4. A fresh-machine dry-run of both README quickstarts (tool-install and cloned-repo) succeeds as written.
5. Remote lane: one `workflow_dispatch` remote-validation run (or manual `run_remote_tests.sh`) green on blackbird before tagging — schedule with the owner; deploy per the two-host topology notes in cerebrum (different repo paths; never `vela agent stop` the shared blackbird daemon).
