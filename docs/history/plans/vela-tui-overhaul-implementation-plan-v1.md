# Vela TUI Overhaul — Implementation Plan (v1)

**Status:** ACTIVE · **Branch:** `claude-ui-implementation` · **Created:** 2026-06-08
**Canonical doc** for the Textual UI overhaul. Living — update the §10 checklist as phases land.

> **Companion docs (read together):**
> - `vela-tui-figma-redesign-handoff.md` — the **visual spec**: design tokens, per-screen specs + exact copy (Appendix C), the Textual implementation mapping (Appendix B), and the final Figma **node-ID map (§13)**.
> - This doc — the **execution plan**: how we refactor the *existing* code to match those mocks, in what order, preserving behavior.

---

## 1. Goal & framing

The v1 TUI **works** but its workflow/input screens are unintuitive (raw `Static`+`Input` stacks, no shared form language, content-altitude dumps). We designed the polished target in Figma (page `39:2`, terminal-faithful, approved by the user). This overhaul brings the **implementation up to the approved mocks**.

**This is a PRESENTATION REFACTOR, not a rewrite.** Every screen already exists and is wired into real logic (dismiss payloads, action handlers, target-client RPCs, validation). The #1 rule:

> ⚠️ **Preserve all functional contracts.** Do not change a screen's dismiss-payload shape, its `id=`s that the app/tests query, its action handlers, or its target/agent calls — unless a change is explicitly required and re-wired end-to-end. We restyle and restructure `compose()`/CSS; we do **not** rewire behavior.

**Success =** each screen visually matches its Figma node (layout, copy, semantic color, the self-explaining helpers, recipe cues) **and** all existing tests still pass, with new tests covering the new structure.

---

## 2. Current state (from 2026-06-08 codebase audit)

- **Package:** `vela` (renamed from `vllm_loader`, no shim). TUI at `src/vela/tui/`.
- **`app.py`** (~5204 lines): `VelaApp(App)`. The dashboard shell already exists (`compose()`: `#terminal-shell` → `#top-chrome`, `#sidebar` [configs/phases/gpu/status-strip], `#main` [log-panel, progress-panel], `#footer-bindings`). Styling = one large inline `CSS` string + Rich `Text` renderables. 22 `BINDINGS`. Screens opened via `self.push_screen(Screen(...), callback=self._handle_*)`, dismissed with payload dicts.
- **`theme.py`** (~20 lines): 11 semantic tokens (`ACCENT/GOOD/WARN/BAD/MUTED/TEXT/BASE/SURFACE/BORDER/PURPLE` + surfaces). Figma-ish but **drifts** from the canonical "Vela Terminal" hex (e.g. `BASE=#091015` vs Figma `bg/base=#0c141b`; `BAD=#ff6b6b` vs `#ff6b7a`) and is an **incomplete subset** (no raised/inset/field bg, no border/subtle·strong·focus split, no text primary/secondary/faint split, no blue).
- **`widgets/`**: exists but **EMPTY** (`__init__.py` only). No reusable compound widgets — every screen reinvents form fields, list+detail, modal frames.
- **14 screens** in `tui/screens/`: `adopt_build, build_manager, config_picker, confirm, create_build, download_model, flag_manager, help, log_prompt, model_manager, new_deployment` (+`NewDeploymentReviewScreen`)`, pin_model, target_edit, target_manager`. Pattern: `ModalScreen` + inline `CSS = f"""..."""` (token interpolation, some hardcoded hex) + `compose()` of base widgets (`Static/Input/Select/Checkbox/RichLog/ProgressBar`).
- **Styling approach:** `theme.py` constants interpolated into per-screen CSS f-strings; Rich `Text` for multi-color content. **No `.tcss` files.**
- **Tests:** `tests/test_tui_smoke.py` (~1000 lines) via `app.run_test()` + `Pilot`, headless/Mac-safe; asserts app state + `query_one(...)` widget presence + dismiss payloads.

**OpenWolf note:** `.wolf/anatomy.md` is stale (still lists `src/vllm_loader/`). To be refreshed as files change this overhaul.

---

## 3. Mapping: Figma node → existing screen → work

| Figma node | Screen file | Pattern | Key changes (preserve contract) |
|---|---|---|---|
| `44:2` Target Manager | `target_manager.py` | master-detail | `MasterDetail` widget; grouped detail sections; collapse capability dump → "N supported ✓"; host/paths as `kv` rows |
| `49:2` Create Build | `create_build.py` | form | `Field` widgets; progressive disclosure by method; method description + target-aware channel helper; WILL-RUN preview |
| `50:2` Download Model | `download_model.py` | form + card | `ContextCard` (read-only model); revision override affordance (kill ghost-placeholder); `PresetChips` for patterns; WILL-DOWNLOAD preview |
| `52:2` Adopt Build | `adopt_build.py` | form + validation | `ValidationCard` (auto-detected stack); `Field`; copy `Checkbox` w/ tradeoff helper |
| `55:2` Flag Manager | `flag_manager.py` | master-detail + cmd | `MasterDetail` table (source tags + changed dots) + rich detail + `ResolvedCommandPanel`; **recipe-safety cues** on precision flags |
| `56:2`–`58:68` New Deployment | `new_deployment.py` | wizard | `StepIndicator` widget; radio rows w/ handoff markers; per-model suggestions; Review summary; Save&Smoke result card |
| `60:2` Dashboard | `app.py` | full screen | 3-zone header (drop cryptic glyphs); vertical phase stepper; GPU card; compact security notice; **log-level classification** (dim benign warnings) |
| `61:2` Component Kit | (reference only) | — | not a runtime screen; the spec the widgets map to |
| (no Figma yet) | `model_manager.py`, `build_manager.py` | master-detail | apply the same `MasterDetail`/`ContextCard` language for consistency (lower priority) |
| (no Figma yet) | `config_picker, confirm, help, log_prompt, pin_model, target_edit` | small modals | adopt `ScreenFrame`/`KeyHintBar`/`Field` for consistency (low priority) |

---

## 4. Foundation (Phase 1) — what everything depends on

### 4.1 Token system (`src/vela/tui/theme.py`)
Expand to the **full "Vela Terminal" set** and reconcile hex to the **Figma values** (the approved mocks are the source of truth). Add the missing tokens: `BG_BASE/PANEL/RAISED/INSET/FIELD`, `BORDER_SUBTLE/STRONG/FOCUS`, `TEXT_PRIMARY/SECONDARY/FAINT`, `GREEN/CYAN/AMBER/RED/BLUE/VIOLET`, surfaces `SURFACE_GREEN/AMBER/RED/BLUE/CYAN`, plus `ON_ACCENT`.
- **Keep the existing names as aliases** (back-compat) so current screens/tests don't break; introduce the new names alongside.
- **Decision:** stay with **Python constants interpolated into CSS f-strings** (the proven existing pattern) rather than introducing a `.tcss` variable file. Lower risk, no mechanism change. (Deviation from handoff Appendix B.1's `theme.tcss` suggestion — rationale: match what works.)
- ⚠️ Reconciling `BASE`/`BAD` hex slightly shifts current dashboard colors toward the approved palette — intended.

### 4.2 Shared widgets (`src/vela/tui/widgets/`) — the missing "form language"
Build these (each: small, tested, documented, mapping to a Component-Kit `61:2` primitive):
- `Field` — label · `required`/`optional` tag · input/select/checkbox · one-or-more **helper lines** (what it does + where the value comes from) · inline `✓/✗` validation · focus border. The single biggest fix.
- `ScreenFrame` — `ModalScreen` base: consistent title bar, body, footer built from `BINDINGS`. Standardizes border/padding.
- `KeyHintBar` — renders `BINDINGS` as `key label` pairs (cyan key, dim label).
- `StatusPill(text, kind)` / `SourceTag(text, kind)` — kind ∈ green/amber/red/cyan/violet.
- `MasterDetail` — list/table pane + detail pane (the Target/Flag/Model/Build managers), with the height-balancing learned in Figma.
- `ContextCard` — raised, read-only "what you're operating on" card.
- `ValidationCard` — green/red live-validation result (Adopt Build).
- `PresetChips` — selectable chips + "advanced (raw)" toggle (Download Model patterns; Flag Manager presets).
- `ResolvedCommandPanel` — scrollable, monospace, **secret-masked**, one-env-var-per-line block w/ copy. Reused by Flag Manager + wizard Review.
- `StepIndicator` — wizard breadcrumb (`✓done` green / current cyan / future dim).
- `PhaseStepper` — vertical phase list w/ done/current/pending + timing (dashboard).

---

## 5. Behavior fixes (fold into the relevant screen phase)
From handoff Appendix B.3 — these are real UX bugs the mocks imply:
- **Reliable focus/selection** — clear focus border (`BORDER_FOCUS`), correct `Tab`/`Shift+Tab` order, click/enter focuses, `Esc` blurs/cancels. (User reported "input boxes aren't reliably selected/deselected.")
- **Progressive disclosure** — Create Build shows only the fields for the selected `Method` (mount/remove or `display`-toggle), not all 8.
- **Ghost-placeholder fix** — never use a real value (commit hash) as an `Input` placeholder; use true hints.
- **Summaries over dumps** — Target Manager capability count not the 60-method wall; resolved commands one-env-per-line, secrets masked.
- **Log-level classification** — classify known-benign lines (NCCL `destroy_process_group`, `binds to 0.0.0.0`) as benign → dimmed/filterable, not error-styled. (Dashboard; addresses screenshot #7.)
- **Header de-crowding** — replace cryptic `□ PATH ○ M` glyphs; never wrap the config name mid-word.
- **Recipe-safety cues** — Flag Manager flags precision flags (`dtype`, `kv-cache-dtype`) as recipe-protected with a warning.

---

## 6. Phasing (with an early validation gate)

- **Phase 1 — Foundation + vertical slice.** Token set (4.1) + core widgets (`Field`, `ScreenFrame`, `KeyHintBar`, `StatusPill`/`SourceTag`). Then refactor **Create Build** (`49:2`) end-to-end as the proof: progressive disclosure, self-explaining fields, WILL-RUN preview — preserving its dismiss payload. Tests + headless screenshot vs `49:2`. **GATE: user confirms fidelity before scaling.**
- **Phase 2 — Form screens.** Download Model (`50:2`, +`ContextCard`/`PresetChips`), Adopt Build (`52:2`, +`ValidationCard`/checkbox).
- **Phase 3 — Master-detail.** Target Manager (`44:2`), Flag Manager (`55:2`, +`ResolvedCommandPanel` + recipe cues). Apply `MasterDetail`/`ContextCard` to `model_manager`/`build_manager` for consistency.
- **Phase 4 — Wizard.** New Deployment (`56:2`–`58:68`): `StepIndicator`, radio rows + handoffs, suggestions, Review, Save&Smoke card.
- **Phase 5 — Dashboard.** `app.py` compose/CSS: 3-zone header, `PhaseStepper`, GPU card, compact security notice, log-level classification.
- **Phase 6 — Polish.** Small modals (`config_picker/confirm/help/log_prompt/pin_model/target_edit`) adopt the shared widgets; full behavior-fix audit; Component-Kit parity pass; full suite green; refresh `.wolf/anatomy.md`.

---

## 7. Testing & verification (Mac-safe — per project convention)
- **Strict red-green TDD** (`superpowers:test-driven-development`) — MANDATORY for every widget and screen change: write the FAILING test first, run it and watch it fail for the right reason, then minimal code → green → refactor. No production code without a failing test first; code written otherwise gets deleted and redone.
- **No GPU/vLLM locally.** All TUI tests run headless via `app.run_test()` + `Pilot`; real GPU validation only after rsync to the GPU host (out of scope for UI work).
- Per screen: assert structure (`query_one` for new widget ids/types), the **preserved dismiss payload**, focus order, progressive-disclosure visibility, and Rich style/role for semantic color (headless screenshots can be `nocolor`, so assert styles too — per cerebrum).
- **Visual fidelity:** export Textual SVG screenshots (`run_test` → save) and compare layout against the Figma node; spot-check by eye.
- Gate each phase on: `ruff` clean (100-col), full `pytest` green, no regression in existing TUI tests.

---

## 8. Constraints & conventions (from `.wolf/cerebrum.md`)
- Mac-safe local validation (no GPU/vLLM); rsync→GPU for real runs only.
- `ruff` enforces 100-col; keep project import ordering (I001).
- **Every field/selection explains itself** (what it does + where the value comes from; method choices say what will happen).
- **No unique-environment details** in any sample text/placeholder — use `gpu-node`, `user@gpu-host`, `/home/user/...`, config `qwen36-27b-bf16-blackwell`, any Blackwell card = `Blackwell sm_120`, placeholder run_ids.
- Preserve functional contracts (see §1).
- Log every bug to `.wolf/buglog.json`; update `.wolf/memory.md`/`anatomy.md` as files change.

---

## 9. Risks / open decisions
- **app.py is ~5204 lines** — the dashboard refactor (Phase 5) is the riskiest edit; do it last, in small steps, leaning on existing `id=`s and tests.
- **Token hex reconciliation** shifts current dashboard colors subtly to match the approved mocks (intended; flag if undesired).
- **Textual version** — cerebrum says "Textual 8.2.7"; confirm widget/CSS APIs against the installed version before relying on newer features (`pip show textual`).
- Keep deviations from the Figma mock documented here if the medium forces them.

---

## 10. Status checklist (living)

### Phase 1 — Foundation + Create Build slice
- [x] Expand/reconcile `theme.py` token set (legacy names kept; full Figma set added)
- [x] `Field` widget + tests (grounded via red→green)
- [x] `KeyHintBar` widget + tests (red→green)
- [ ] `ScreenFrame` — deferred (YAGNI; extract once 2-3 screens share the frame)
- [ ] `StatusPill` / `SourceTag` — deferred to Phase 3 (the master-detail managers need them)
- [x] Refactor `create_build.py` → match `49:2` (progressive disclosure, self-explaining, WILL-RUN); payload + ids + uv-gating preserved
- [x] Tests + render vs `49:2` (4 new screen tests + payload-contract green; 195 smoke green; ruff clean)
- [x] **GATE: fidelity approved by user** (`·`/`→`/`—` separator polish applied to match the mock)

### Phase 2 — Form screens
- [x] Download Model (`50:2`) · ContextCard + PresetChips widgets (red→green) · revision override (ghost-placeholder fixed) · WILL-DOWNLOAD preview · payload preserved · 195 smoke green · (raw-patterns "advanced" toggle deferred to Phase 6)
- [x] Adopt Build (`52:2`) · ValidationCard widget (red→green) + copy checkbox + WILL-DO preview · payload preserved · 195 smoke green

### Phase 3 — Master-detail
- [x] `MasterDetail` + `ContextCard` widgets (`ResolvedCommandPanel` realized as styled-`Text` render helpers — smoke pins the panes as Statics)
- [x] Target Manager (`44:2`) — capability collapse + grouped sections
- [x] Flag Manager (`55:2`) + recipe-safety cues
- [x] model_manager / build_manager consistency pass (commit `2935f2c`)

### Phase 4 — Wizard
- [x] `StepIndicator` widget (red→green; bug-184)
- [x] New Deployment (`56:2`–`58:68`) — Selects kept (no RadioSet) to protect the 24-test contract (commit `19baa94`)

### Phase 5 — Dashboard
- [ ] `PhaseStepper` widget — NOT BUILT (descoped; existing `_render_phase_timeline` retained)
- [x] log-level classification → `60:2` (commit `f993be5`; BENIGN dim level for NCCL shutdown noise)
- [x] header de-crowding — delivered 2026-06-09 evening pass: `▣`/`M` glyph segments replaced with labeled `build:`/`model:` segments; sidebar config names ellipsize instead of wrapping mid-word
- [ ] GPU card / compact security notice rework — NOT BUILT (descoped; existing panels retained)

### Phase 6 — Polish
- [ ] Small modals adopt shared widgets — still pending (tracker #7, optional)
- [x] Behavior-fix audit (focus, ghost-placeholder, summaries, log classes)
- [x] Component-kit parity; full suite green; refresh `anatomy.md` (commit `2935f2c`)

### Post-review functional pass (2026-06-09 evening)
Follow-up from the definition-of-done review — "make everything advertised real":
- [x] Create Build: hidden-field values no longer leak into the dismiss payload
- [x] Resolved-command preview renders one env var per line (env-wall fix, `command_builder.render_preview`)
- [x] Download Model: PresetChips are interactive (click/`select()`), presets fill the raw inputs, highlight derives from the values; raw fields collapse behind a real `Ctrl+R` toggle (bare-letter keys can't fire while an Input has focus — documented deviation from the mock's `a`); dead `o` hint removed
- [x] Target Manager: `v` binding expands/collapses the full capability list ("v view all" is now real)
- [x] Adopt Build: validation card starts neutral and renders REAL probe results (new `inspect_venv` engine fn + agent method, wired via an optional `probe` kwarg); detected vllm version auto-fills; dead `space` hint removed
- [x] FR-18: post-READY health polling wired end-to-end (agent keeps `probe_loop` alive after READY; `on_phase_changed` lets READY↔DEGRADED flow) — new end-to-end smoke test with a health-toggling fake child
- [x] `ProcessExited.signaled` field added (spec §6.3)
- [x] Test/state isolation: suites now run against a per-session temp XDG state dir with a fresh agent daemon, stopped at session end (durable bug-185 fix; also fixes "tests silently validate a stale daemon's old code")

---

*Plan v1. Phases 1–6 executed (commits `0ea1518`, `19baa94`, `f993be5`, `2935f2c`); Phase 5 partially descoped as noted. Post-review functional pass applied 2026-06-09 evening.*
