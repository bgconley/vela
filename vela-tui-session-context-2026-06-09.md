# Vela TUI — Figma Redesign → Textual Implementation: COMPREHENSIVE SESSION CONTEXT

**Created:** 2026-06-09 · **Purpose:** a single, self-contained, exhaustive record so a future session (human or agent) can resume the **Textual UI overhaul** of the Vela TUI cold — with zero re-derivation. It preserves: the application, the design problem, the Figma redesign (the visual spec) and *why* it exists, the Textual implementation overhaul now in progress (approach, discipline, environment, what's built, contracts preserved), the live task list with current status, the precise immediate next steps, and every gotcha learned.

> **Read this top-to-bottom before touching code again.** The load-bearing sections for resuming are **§7 (implementation status + what's built)**, **§8 (per-screen contracts to preserve)**, **§9 (task list + immediate next steps for Phase 3)**, and **§10 (environment + tooling gotchas)**.

> **Companion canonical docs (in repo root, all still valid):**
> - `vela-tui-figma-redesign-handoff.md` — the **Figma visual spec**: design tokens, per-screen specs, exact copy (Appendix C), the Textual mapping (Appendix B), and the final **node-ID map (§13)**.
> - `vela-tui-overhaul-implementation-plan-v1.md` — the **execution plan** with the living phase checklist (§10 of that doc).
> - This doc consolidates both + the current implementation state into one resumable record.

---

## 0. TL;DR — resume in 90 seconds

- **App:** **Vela** — a Textual TUI + Typer CLI that launches/monitors/manages **vLLM** inference servers via a **controller/agent split**. Package is **`vela`** (renamed from `vllm_loader`), repo `/Users/brennanconley/vibecode/lab-tui`, remote `github.com/bgconley/vela.git`. It is **v1-DONE** (12 review rounds, hardware-validated). Code lives in `src/vela/`.
- **Two phases of work:**
  1. **Figma redesign — COMPLETE.** We designed the missing *workflow/input* screens (they were never mocked) in Figma, terminal-faithful, on page `39:2` of file `9xUgzyoFqWmd40tV5dwaHv`. All 7 screens + a component-kit reference frame exist and were approved. (See §5–§6.)
  2. **Textual implementation overhaul — IN PROGRESS (this is the current work).** We are bringing the *running* TUI up to the approved mocks. Branch **`claude-ui-implementation`**. (See §7 onward.)
- **The implementation is a PRESENTATION REFACTOR, not a rewrite.** All 14 screens already exist and are wired to real logic. **Rule #1: preserve every functional contract** (dismiss-payload shapes, widget `id=`s, action handlers, RPC calls). We restyle `compose()`/CSS only.
- **Discipline: STRICT red-green TDD** (the `superpowers:test-driven-development` skill). Failing test first → watch it fail for the right reason → minimal code to green → refactor. (Adopted mid-session after a user correction; see §11.)
- **Progress: ALL 6 PHASES ✅ DONE + COMMITTED** (2026-06-09). 4 commits on `claude-ui-implementation` (not pushed): `0ea1518` Phases 1-3, `19baa94` Phase 4, `f993be5` Phase 5, `2935f2c` Phase 6. Shared widget kit (each red→green): `Field`, `KeyHintBar`, `ContextCard`, `PresetChips`, `ValidationCard`, `MasterDetail`, `StepIndicator` + `tags.py` helpers (`source_tag`/`summarize_capabilities`/`is_recipe_flag`). Screens refactored to their Figma nodes: Create Build `49:2`, Download Model `50:2`, Adopt Build `52:2`, Target Manager `44:2`, Flag Manager `55:2`, New Deployment wizard+review `56:2-58:2`, Model/Build managers (consistency), dashboard log-classification `60:2` (screenshot #7). **Full test suite 998 green**; ruff clean; every screen rendered + eyeballed.
- **Overhaul COMPLETE — meets §15 definition of done.** Only OPTIONAL polish remains (live tracker #7, all non-flagged/functional): small-modals CSS modernization (`confirm`/`help`/`log_prompt`/`config_picker`/`pin_model`/`target_edit` still use old `ACCENT`/`SURFACE_ALT`/solid borders) + the Download-Model advanced-patterns toggle.
- **All work is COMMITTED** on `claude-ui-implementation` (4 phase commits above). **Not yet pushed** — push on user request.
- **CRITICAL constraint:** never put the user's unique environment into UI text — use generic placeholders (`gpu-node`, `user@gpu-host`, `/home/user/...`, config `qwen36-27b-bf16-blackwell`, any Blackwell GPU = `Blackwell sm_120`, placeholder run_ids). (See §11.)

---

## 1. The application — Vela (full context)

**What it is:** a phase-aware **Textual TUI + Typer CLI** for launching, monitoring, and managing **vLLM** inference servers from named YAML configs, with managed vLLM **builds**, a model **registry**, a **deployment composer**, a **Docker runtime**, and a **controller/agent** architecture. It spawns/monitors `vllm serve` (or a Docker container) as a child process; it **never `import`s vllm**. One package serves both sides; the agent runs on the GPU box as `vela agent connect`/`run`.

**Version & packaging:** `0.1.0`. Python ≥3.10/3.12, hatchling build, console script `vela = vela.cli:main`. Optional extras: `gpu = ["nvidia-ml-py>=12.535"]`, `dev = ["pytest>=8.2","pytest-asyncio>=0.24","ruff>=0.6"]`. Pytest config: `testpaths=["tests"]`, `pythonpath=[".","tests"]`, `asyncio_mode="auto"`. Ruff: `line-length=100`, `target-version="py310"`. OpenWolf-managed (`.wolf/`).

**v1 status:** **Done** — across 12 multi-agent review rounds (Sonnet finders → Opus verification → Opus synthesis), taken from concept to an independently-verified, hardware-validated v1 with zero high/medium open defects. 954 tests at v1. Real on-hardware Blackwell FP8/BF16 backend-evidence validation. The completion audit lives at `vela-v1-completion-audit-2026-06-07.md`; review history in `vela-docker-composer-review-findings-v6..v12.md`.

**Lab infrastructure (the user's REAL hardware — but DO NOT name it in UI mockups, see §11):**
- **Mac (author box):** `/Users/brennanconley/vibecode/lab-tui`, no GPU; where this session runs. The repo's editable install + `pytest`/`ruff` live in **Homebrew Python** (`/opt/homebrew/bin/...`), not the repo `.venv`.
- **P620-01 = controller** — `bgconley@10.25.0.50`, RTX PRO 4000 Blackwell, hosts a self-hosted GitHub Actions runner.
- **Blackbird = GPU agent** — `bgconley@10.25.0.51`, RTX PRO 6000 Blackwell Max-Q (sm120), runs Qwen3.6 27B.
- Code is authored on the Mac and rsynced to GPU boxes for real vLLM/GPU tests; **local validation is no-GPU/no-vLLM by default** (fake-child + headless Textual).

**Why this matters for the UI work:** these screens drive a *real, working* product. The mocks and their Textual implementation must be faithful to the actual data/flows (config names, target fields, build methods, model refs, flag categories) so the implementation maps 1:1.

---

## 2. The design problem — why we redesigned the workflow screens

The user ran the v1 TUI, successfully launched an LLM, but found the UI **"not intuitive at all," "unclear how any of it works," with "way too much ambiguity and friction."** After analysis, the root cause is **three missing design inputs — not bad coding:**

1. **The workflow screens were never mocked.** The canonical Figma file ("Polished Textual Rich UX", page `22:2`) contained 14 polished **dashboard/monitoring** screens (Live Load Dashboard, READY Proof, Config Picker, Command Palette, Error Banners, etc.). It had **zero** designs for the **workflow/input** screens — Target Manager, Create Build, Download Model, Adopt Build, Flag Manager, and the New Deployment wizard. So the coder built those as raw stacks of `Static`/`Label` + `Input` widgets with no hierarchy, spacing, guidance, or affordances → they read like unstyled forms.
2. **No shared "form language."** Each modal reinvented layout. No consistent field component (label · helper · validation), header/footer, spacing scale, or semantic color use.
3. **Content-altitude problem.** Internal data dumped raw at users — the 60-method `capabilities` list, full env-var blocks, raw commit hashes shown as ghost placeholders, agent file paths. Users need summaries and context, not the API surface.

**The 7 screenshots the user flagged (verbatim intent):**
1. **Dashboard / run monitor:** "whitespace issues and crowding at the top." Crowded header with cryptic glyphs (`□ PATH ○ M`), config name wraps mid-word, giant amber security banner eats the log pane, broken underscore "sparkline".
2. **Target Manager:** "super cramped, unorganized, visually cluttered." A raw key:value dump + a wall of ~60 comma-separated RPC capability names. The worst offender.
3. **Create Build:** "incredibly unclear where information is obtained, free text prone to error, input boxes aren't reliably selected/deselected, no indication where to obtain any of the variables." 8 free-text fields shown at once regardless of the selected Method.
4. **Download Model:** name/ref/repo/cache read-only header, then a Revision-override with a **commit hash shown as a ghost placeholder that looks pre-filled**, Allow/Ignore patterns as free text, lots of dead space.
5. **Adopt Build:** "literally no idea what to do." Label, Venv path, vLLM version, Version profile + "Copy venv" checkbox. Unclear what "adopt" means or where to get the venv path.
6. **Flag Manager:** "a totally unacceptable mess. Nearly unusable. Needs full redesign." Misaligned two-column chaos: preset Select + "Changed only" checkbox, then MODELED/PASSTHROUGH/UNKNOWN lists, value box + raw-args box + "Resolved command" wall-of-env-vars.
7. **Log shutdown + NCCL warning:** the user asked about the last log line — `destroy_process_group() was not called before program exit`. **Answer: benign shutdown noise**, not an error (PyTorch/NCCL emits it when a process exits without `torch.distributed.destroy_process_group()`; OS reclaims resources). The UX fix is **log-level classification** (de-emphasize known-benign lines), not a runtime change.

The user said **"Don't limit yourself to the screens I showed you"** → scope expanded to all workflow screens + the New Deployment wizard + a shared component kit + the dashboard/log cleanup.

---

## 3. The design decisions (LOCKED by the user)

- **Decision 1 — Design medium:** **Terminal-faithful, max-polished.** Design natively for Textual (monospace, box-drawing, real Textual widgets) but use every styling lever Textual has (color, spacing, Rich markup, focus borders, rounded panels). Mocks must be **directly implementable 1:1**. (Rejected: aspirational web-style; rejected: both-layers.)
- **Decision 2 — Scope:** **Everything.** Component kit + all 6 workflow screens + the New Deployment wizard + dashboard/log cleanup.
- **Refinement A (this project's standing rule):** **Every field/selection explains itself** — each input has a helper saying *what it does* and *where its value comes from*; method/mode choices get a plain-language description of *what will happen* (what gets downloaded/built, which option to pick and why, what it does NOT do). Be target-aware where possible.
- **Refinement B:** **Recipe-safety cues** — the Flag Manager flags recipe-critical precision flags (`dtype`, `kv-cache-dtype`) as "recipe-protected" with a warning, because the project's local deployment scripts are the authority for the validated Blackwell SM120 stack.
- **Refinement C (correction):** **No unique-environment details in mockups.** (See §11.)

---

## 4. Why this is a two-phase effort

The Figma redesign (Phase A) produced the **visual spec**. The Textual overhaul (Phase B, current) implements it. The redesign was done first (and approved) so the implementation can follow it 1:1 instead of guessing. **No TUI code was touched during the Figma phase.** The implementation phase only began after the user explicitly approved the mocks and asked to proceed.

---

## 5. The Figma redesign — file, page, tokens (COMPLETE)

**File:** `9xUgzyoFqWmd40tV5dwaHv` — "vLLM-TUI-Loader-Screens — Canonical v2". URL: `https://www.figma.com/design/9xUgzyoFqWmd40tV5dwaHv/vLLM-TUI-Loader-Screens---Canonical-v2?node-id=39-2`.

**Pages:** `22:2` is the original canonical (14 dashboard frames). **`39:2` = "Workflow Screens — Redesign v1"** — the new page with everything we built.

**"Vela Terminal" token collection** (`VariableCollectionId:39:3`): 23 COLOR variables + 8 IBM Plex Mono text styles. These hex values ARE the source of truth and were mirrored into `src/vela/tui/theme.py` (see §7.1):

| token | hex | token | hex |
|---|---|---|---|
| bg/base | `#0c141b` | accent/green | `#67e8a5` |
| bg/panel | `#101923` | accent/cyan | `#60d7f8` |
| bg/raised | `#172532` | accent/amber | `#f6c85f` |
| bg/inset | `#0d151d` | accent/red | `#ff6b7a` |
| bg/field | `#0a1118` | accent/blue | `#5fa8e8` |
| border/subtle | `#22384a` | accent/violet | `#b69cf0` |
| border/strong | `#2f5168` | surface/green | `#0e2a21` |
| border/focus | `#60d7f8` | surface/amber | `#2b2410` |
| text/primary | `#e8f1f2` | surface/red | `#2b1218` |
| text/secondary | `#8ba4ae` | surface/blue | `#0c2238` |
| text/faint | `#56707c` | surface/cyan | `#0c2330` |
| text/onAccent | `#06120c` | | |

**Type ramp (IBM Plex Mono):** title Bold 15/22 · header Bold 12/18 · label SemiBold 11/16 · strong SemiBold 11/16 · body Regular 11/16 · helper Regular 10/15 · key Bold 11/16 · meta Regular 10/14.

**Semantic color usage:** green = active/success/match/READY · cyan = title/info/focus/selection · amber = warn/in-progress/required/recipe · red = error · violet = passthrough source tag · dim (faint/secondary) for everything else.

---

## 6. The Figma node-ID map (all screens built + verified + approved)

All on page `39:2`. This is the visual spec each Textual screen must match.

| Screen | Figma node | Notes |
|---|---|---|
| **Target Manager** | `44:2` | master-detail; the 60-method capability wall collapsed to "60 supported ✓ · ⤢ view all"; grouped detail sections (CONNECTION/VERSIONS/PATHS/AUTH/CAPABILITIES) |
| **Create Build** | `49:2` | method-driven form; progressive disclosure; self-explaining fields; WILL-RUN preview. (Was `48:2`, rebuilt with added context.) |
| **Download Model** | `50:2` | read-only model context card; revision override affordance (no ghost-placeholder); preset chips; WILL-DOWNLOAD preview |
| **Adopt Build** | `52:2` | what-it-does subtitle; live validation card (auto-detect); copy checkbox |
| **Flag Manager** | `55:2` | table + detail + resolved-command + **recipe-safety cues** (recipe tags on dtype/kv-cache-dtype + Recipe-protected warning). (Was `53:2`, rebuilt.) |
| **New Deployment wizard** | `56:2` (1 Target), `57:2` (2 Runtime), `57:72` (3 Model), `57:150` (4 Customize), `58:2` (5 Review), `58:68` (6 Save & Smoke) | shared step-indicator; radio choices with "opens screen →" handoffs to Create/Adopt Build, Download Model, Flag Manager; per-model suggestions; Review summary; Save & Smoke green result card (uses real READY data shape) |
| **Dashboard (run monitor)** | `60:2` | full 1440×900; 3-zone header (no cryptic glyphs); vertical phase stepper; GPU card; compact one-line security notice; **log-level classification** (benign warnings dimmed) |
| **Component Kit (reference)** | `61:2` | not a runtime screen — documents every primitive + tokens; the spec the Textual widgets map to |

The full per-screen specs + **exact copy** are in `vela-tui-figma-redesign-handoff.md` Appendix C; the Textual mapping guidance is in its Appendix B.

---

## 7. The Textual implementation overhaul — CURRENT WORK

**Branch:** `claude-ui-implementation` (created from `main` @ `dbdd7ac`, pushed to origin, tracks `origin/claude-ui-implementation`). `main` @ `dbdd7ac` carries the Figma handoff doc, the `v12` review, and the cerebrum design rules. The old `claude-ui-overhaul` branch was deleted (local + remote) because it sat at the pre-redesign commit `f7e61ae` and had no unique commits.

**Canonical plan:** `vela-tui-overhaul-implementation-plan-v1.md` (root). Its §10 has the living phase checklist.

### 7.0 The reframe (most important)
This is a **PRESENTATION REFACTOR**, discovered via a codebase audit: **all 14 screens already exist and are wired to real logic** in `src/vela/tui/`. So we do NOT rewrite — we restyle `compose()`/CSS and add shared widgets, **preserving every functional contract** (dismiss-payload shapes, `id=`s the app/tests query, action handlers, RPC calls).

### 7.1 Foundation — tokens (`src/vela/tui/theme.py`) — DONE
The file originally had only 11 tokens that drifted from Figma. We **expanded it additively**: kept all legacy names unchanged for back-compat (`ACCENT`, `ACCENT_SURFACE`, `GOOD`, `GOOD_SURFACE`, `WARN`, `WARN_SURFACE`, `BAD`, `BAD_SURFACE`, `MUTED`, `MUTED_SURFACE`, `TEXT`, `BASE=#091015`, `SURFACE`, `SURFACE_ALT`, `BORDER=#274254`, `PURPLE`, `PURPLE_SURFACE`) and **added the full "Vela Terminal" set** at the canonical Figma hex: `BG_BASE=#0c141b, BG_PANEL=#101923, BG_RAISED=#172532, BG_INSET=#0d151d, BG_FIELD=#0a1118, BORDER_SUBTLE=#22384a, BORDER_STRONG=#2f5168, BORDER_FOCUS=#60d7f8, TEXT_PRIMARY=#e8f1f2, TEXT_SECONDARY=#8ba4ae, TEXT_FAINT=#56707c, TEXT_ON_ACCENT=#06120c, GREEN=#67e8a5, CYAN=#60d7f8, AMBER=#f6c85f, RED=#ff6b7a, BLUE=#5fa8e8, VIOLET=#b69cf0, SURFACE_GREEN=#0e2a21, SURFACE_AMBER=#2b2410, SURFACE_RED=#2b1218, SURFACE_BLUE=#0c2238, SURFACE_CYAN=#0c2330`.
- **Decision:** keep the proven `theme.py` Python-constants-interpolated-into-CSS-f-strings pattern (rather than introducing a `.tcss` variable file). Lower risk.
- New widgets + refactored screens use the new tokens. Old screens keep their hardcoded hex literals until refactored (transient, subtle palette difference; migrate per-screen).

### 7.2 Foundation — shared widgets (`src/vela/tui/widgets/`) — 5 built
The package was empty; now contains (each grounded **red→green**, exported from `__init__.py`):

- **`Field`** (`field.py`) — the keystone "form language" widget. Renders a bold label, optional `required`/`optional` tag, a **caller-provided control** (Input/Select/Checkbox — kept as-is so its `id` + event handlers are preserved), and one or more dim helper lines. API: `Field(label, control, *, helper: str|list[str]="", required=False, optional=False, id=...)`. Stores `self._label`. CSS classes: `.field-label`, `.field-req`, `.field-opt`, `.field-helper`; styles `Field Input/Select` with round border + `:focus` focus border.
- **`KeyHintBar`** (`keyhintbar.py`) — footer keybinding hints. API: `KeyHintBar(hints: Iterable[tuple[str,str]], *, id=...)`. Renders per hint a cyan bold key (`.keyhint-key`) + dim label (`.keyhint-label`) inside `.keyhint`.
- **`ContextCard`** (`contextcard.py`) — read-only raised card. API: `ContextCard(heading, rows: Iterable[tuple[str,str]], *, id=...)`. Renders `.context-card-heading` + a `.context-row` per row (key `.context-key` width 16 + value `.context-value`). Raised bg, subtle border.
- **`PresetChips`** (`preset_chips.py`) — selectable chip row. API: `PresetChips(options: Iterable[str], *, selected: int=0, id=...)`. Renders `.preset-chip` per option; selected gets `.preset-chip.selected` (cyan). 1-row pills (bg highlight, no border).
- **`ValidationCard`** (`validation_card.py`) — green/red live-validation result. API: `ValidationCard(ok: bool, heading, detail="", note="", *, id=...)`. Card class `validation-card` + `-ok`/`-bad` state class; renders `.validation-heading` (✓/✗ + heading), optional `.validation-detail`, `.validation-note`. Green border/bg for ok, red for bad.

**Deferred widgets (build when first needed):** `ScreenFrame` (a ModalScreen base — YAGNI until 2-3 screens share the exact frame), and the Phase 3 set: `MasterDetail`, `StatusPill`/`SourceTag`, `ResolvedCommandPanel`, plus Phase 4's `StepIndicator` and Phase 5's `PhaseStepper`.

### 7.3 Screens refactored — 3 done (Create Build, Download Model, Adopt Build)
Each: TDD (new-behavior tests written first → red → refactor → green), payload/ids preserved, **195 smoke tests green** after, ruff clean, rendered headlessly + visually confirmed against its Figma node. Details + contracts in §8.

### 7.4 Tests added this session
- `tests/test_tui_widgets.py` — 7 tests (Field ×2, KeyHintBar, ContextCard, PresetChips, ValidationCard ×2).
- `tests/test_create_build_screen.py` — 4 tests (Field widgets present, nightly progressive disclosure, WILL-RUN preview, payload contract).
- `tests/test_download_model_screen.py` — 5 tests (ContextCard present, revision true-hint-not-ghost-sha, PresetChips present, WILL-DOWNLOAD preview, payload contract).
- `tests/test_adopt_build_screen.py` — 4 tests (ValidationCard present, Field widgets, WILL-DO preview, payload contract incl. copy checkbox).
- Existing guards preserved: `tests/test_tui_screen_parsers.py` asserts `not hasattr(create_build, "_parse_build_params")` and `not hasattr(adopt_build, "_parse_adopt_build_params")` — **do not reintroduce those names.**

---

## 8. Per-screen contracts (CRITICAL — preserve these in all future work)

### 8.1 Create Build — `src/vela/tui/screens/create_build.py` → Figma `49:2` (DONE)
- **Class:** `CreateBuildScreen(ModalScreen[dict|None])`, `id="create-build"`.
- **Constructor:** `__init__(*, initial: dict|None=None, error_message="", uv_available: bool|None=None, target_label="")`.
- **Dismiss payload** (`_collect_build_params`): `{"method", "label"?, "spec"?, "channel"?, "python"?, "commit"?, "url"?, "path"?, "env"?: [tokens]}` — only non-empty fields, `method` always; raises `ValueError` if no method.
- **Widget ids:** `#create-build-method` (Select: nightly/pip/commit/git/wheel), `#create-build-{label,spec,channel,python,commit,url,path,env}` (Inputs), `#create-build-error`, `#create-build-title`, `#create-build-uv-note` (the method-note Static), `#create-build-preview` + `#create-build-preview-cmd`, `#create-build-footer`.
- **uv gating:** `nightly`/`commit` require uv; if `uv_available is False`, block with `_uv_block_message`. `_method_requires_uv`, `_uv_note_text`, `_method_note_text`.
- **Refactor delivered:** inputs wrapped in `Field` with `#cb-<key>` wrapper ids; **progressive disclosure** via `_VISIBLE` map + `_apply_disclosure()` (inputs stay mounted, `Field.display` toggled → ids/payload intact); WILL-RUN preview via `_render_preview()` + `_build_will_run()` setting `self._preview_command`; `KeyHintBar` footer; `on_input_changed`/`on_select_changed` update note+disclosure+preview (guarded by `self._ready` set in `on_mount`). `_VISIBLE = {nightly:{label,channel,python,env}, pip:{+spec}, commit:{label,commit,channel,python}, git:{label,url,python,env}, wheel:{label,path,python}}`.
- **No `_parse_build_params`** (guard test).

### 8.2 Download Model — `src/vela/tui/screens/download_model.py` → Figma `50:2` (DONE)
- **Class:** `DownloadModelScreen(ModalScreen[dict|None])`, `id="download-model"`. **Constructor:** `__init__(self, model: dict)`.
- **Dismiss payload** (`_collect_download_params`): `{"model_ref", "revision"?, "allow_patterns"?: [..], "ignore_patterns"?: [..]}`; raises if no `model_ref`. Module fns `_model_ref`, `_model_label`, `_patterns_from_input` (splits on whitespace + commas).
- **Widget ids (smoke-critical):** `#download-model-revision`, `#download-model-allow`, `#download-model-ignore` (Inputs — the smoke test at `test_tui_smoke.py:9741-9743` sets these directly then submits), `#download-model-error`. New: `#download-model-presets` (PresetChips), `#download-model-preview` + children, `#download-model-files-help`.
- **Key fix delivered:** the revision input's placeholder was the **commit sha** (ghost-placeholder bug, screenshot #4). Now `_revision_placeholder()` returns the **true hint** `"leave blank to keep the pinned commit"`; the pinned sha lives in a read-only `ContextCard` ("MODEL · read-only": repo, `pinned <sha> ✓ immutable`, cache, access). Raw allow/ignore inputs kept (wrapped in `Field`s) so the payload + smoke contract hold; `PresetChips` added as the preset front-end.
- **Deferred to Phase 6:** the raw allow/ignore inputs are shown directly; the `a Advanced patterns` toggle (collapse raw behind the chips) is an interactive-state nicety not yet wired (footer advertises it).

### 8.3 Adopt Build — `src/vela/tui/screens/adopt_build.py` → Figma `52:2` (DONE)
- **Class:** `AdoptBuildScreen(ModalScreen[dict|None])`, `id="adopt-build"`. **Constructor:** `__init__(self)` (no args).
- **Dismiss payload** (`_collect_adopt_build_params`): `{"label"?, "venv_path" (required), "vllm_version"?, "vllm_version_profile"?, "copy": "true"?}`; raises if no `venv_path`. `_field_value`, `_checked`.
- **Widget ids (smoke-critical):** `#adopt-build-{label,venv-path,vllm-version,vllm-version-profile}` (Inputs), `#adopt-build-copy` (Checkbox), `#adopt-build-error`. The smoke tests at `4807-4812` and `8791-8797` set all four inputs + the checkbox directly → all must stay editable/mounted (including `vllm-version`, even though the mock frames it as auto-detected). New: `#adopt-build-subtitle`, `#adopt-build-preview` + children, `#adopt-build-copy-help`, `#adopt-build-footer`.
- **Refactor delivered:** what-it-does subtitle; a green `ValidationCard` (presentational auto-detect display — live SSH-based venv validation wiring is a *follow-up behavior feature*, out of scope for the presentation refactor); Field-wrapped inputs; copy `Checkbox` + tradeoff helper; WILL-DO preview; `KeyHintBar`. Submission via `on_input_submitted` → `action_submit` (added). Focus on `#adopt-build-venv-path` on mount.
- **No `_parse_adopt_build_params`** (guard test).

---

## 9. TASK LIST + current status + IMMEDIATE NEXT STEPS

> **This section is the DURABLE task state.** The harness `TaskCreate` tracker is ephemeral — a `/clear` wipes it and reassigns IDs. On session restore, reconstruct the live tracker from the list below (do not treat an empty task list as "tasks lost"). Originally IDs #9–#14; rebuilt as #1–#6 on the 2026-06-09 restore.

Live task tracker:
- **Phase 1 — Foundation + Create Build — ✅ COMPLETED** (commit `0ea1518`).
- **Phase 2 — Form screens (Download Model, Adopt Build) — ✅ COMPLETED** (`0ea1518`).
- **Phase 3 — Master-detail (Target Manager `44:2`, Flag Manager `55:2`) — ✅ COMPLETED** (`0ea1518`). Built `MasterDetail` + `tags.py` (`source_tag`/`summarize_capabilities`/`is_recipe_flag`); recipe-safety cues delivered. (StatusPill/SourceTag/ResolvedCommandPanel realized as styled-`Text` render helpers, not mounted widgets — the panes are pinned Statics.)
- **Phase 4 — New Deployment wizard + review `56:2-58:2` — ✅ COMPLETED** (`19baa94`). Built `StepIndicator`; kept Selects (no RadioSet) to protect the 24-test contract.
- **Phase 5 — Dashboard log classification `60:2` (screenshot #7) — ✅ COMPLETED** (`f993be5`). `display_level_for_line` + `BENIGN` dim level; dashboard chrome was already v1-styled.
- **Phase 6 — Polish — ✅ COMPLETED** (`2935f2c`). Model/Build manager consistency + anatomy.md refresh (+ `.wolf/config.json` cache excludes).
- **#7 (optional, non-flagged) — small-modals CSS modernization + Download advanced-patterns toggle — ⬜ PENDING.** Not required for §15 done.

### IMMEDIATE NEXT STEPS — Phase 3 (master-detail), in order:
1. **Audit the contracts first** (do NOT trust memory): `Read` `src/vela/tui/screens/target_manager.py`, `flag_manager.py`, and also `model_manager.py` + `build_manager.py` (they share the list+detail pattern). Then `grep -rn "target-manager\|target_manager\|TargetManager" tests/` and the same for `flag-manager`/`flag_manager` to learn which ids + dismiss payloads the smoke suite drives. (We already know from the earlier audit excerpt that `flag_manager.py` compose has `#flag-manager-preset` Select, `#flag-manager-changed-only` Checkbox, `#flag-manager-list` Static, `#flag-manager-editor`, `#flag-manager-value` Input, `#flag-manager-extra-args` Input, `#flag-manager-detail` Static, `#flag-manager-footer`; and its dismiss payload is `{"action": "save_flags", "name", "engine": {...}, "extra_args": [...]}` — but RE-READ the full file to confirm before editing.)
2. **Build the Phase-3 widgets (strict TDD, red→green, add tests to `tests/test_tui_widgets.py`):**
   - **`MasterDetail`** — a two-pane container (list/table pane + detail pane). Use the **column height-equalization** trick learned in Figma: set both columns to natural height, read `left.height`/`right.height`, set the body to `max`, then set both columns `layoutSizingVertical="FILL"` (in Textual terms: the equivalent — make one pane drive height and the other fill; or fix the body height). The Figma master-detail rule: exactly one column drives height (HUG), the other(s) FILL; never both FILL.
   - **`StatusPill(text, kind)`** and **`SourceTag(text, kind)`** — kind ∈ green/amber/red/cyan/violet. Pills for connection/phase/validation; source tags for modeled (cyan) / passthrough (violet) / unknown (amber) / recipe (amber).
   - **`ResolvedCommandPanel`** — scrollable, monospace, **secret-masked** (`VLLM_API_KEY='••••'`), **one env-var per line**, with a copy affordance. Reused by Flag Manager + the wizard Review step.
3. **Refactor `target_manager.py` → `44:2` (TDD):** master-detail; left list with connection dots; right grouped sections (CONNECTION/VERSIONS/PATHS/AUTH/CAPABILITIES) as `kv` rows; **collapse the ~60-capability wall to "N supported ✓ · ⤢ view all"** (the headline fix for screenshot #2); footer keybar. Preserve its dismiss payload + ids.
4. **Refactor `flag_manager.py` → `55:2` (TDD — THE HARDEST SCREEN):** toolbar (preset Select + changed-only Checkbox + search); a grouped flag **table** (MODELED/PASSTHROUGH/UNKNOWN with counts, color source tags, amber changed-dots); a rich detail pane (plain-language description + value editor + range/default/preset + `→ engine.*` mapping); a **`ResolvedCommandPanel`**; and the **recipe-safety cues** (amber `recipe` tags on `dtype`/`kv-cache-dtype` + a "Recipe-protected" amber warning callout in the detail). Preserve the `{"action":"save_flags", "name", "engine", "extra_args"}` payload + all `#flag-manager-*` ids the smoke suite uses.
5. **Consistency pass** on `model_manager.py` + `build_manager.py` (same `MasterDetail`/`ContextCard` language). Lower priority.
6. **Verify each:** targeted screen tests green + the **full `tests/test_tui_smoke.py` (195) green** (regression gate) + ruff clean + render via qlmanage and eyeball vs the Figma node. Consider a user check-in (gate) after Flag Manager given it's the hardest.

### Then (later phases):
- **Phase 4 — New Deployment wizard** (`56:2`–`58:68`): build `StepIndicator`; refactor `new_deployment.py` (+`NewDeploymentReviewScreen`) — step indicator, radio rows with "opens screen →" handoffs, per-model suggestions, Review summary, Save & Smoke result card. Preserve the 6-step flow + payloads. This is the centerpiece; the smoke suite has whole-handoff acceptance tests that drive it.
- **Phase 5 — Dashboard** (`60:2`): build `PhaseStepper`; refactor `app.py` (the BIG file, ~5204 lines — riskiest edit, do it in small steps leaning on existing `id=`s + tests): 3-zone header (drop cryptic glyphs, no mid-word config wrap), vertical phase stepper, GPU card, compact one-line security notice, and **log-level classification** (dim benign NCCL/`0.0.0.0` lines instead of error-styling them — the screenshot #7 fix).
- **Phase 6 — Polish:** small modals (`config_picker/confirm/help/log_prompt/pin_model/target_edit`) adopt the shared widgets; behavior-fix audit (reliable focus, ghost-placeholder removal everywhere, summaries-over-dumps, log classes, the Download-Model advanced toggle); Component-Kit parity; refresh the stale `.wolf/anatomy.md`.

---

## 10. Environment & tooling — gotchas that will waste time if forgotten

- **Run tests with Homebrew Python, NOT the repo `.venv`:** `python3 -m pytest ...` (Homebrew `python3` has `vela` editable-installed + `textual` + `pytest 9.0.2`). The repo `.venv` exists but **has no pytest**. `which pytest` → `/opt/homebrew/bin/pytest`. Run from repo root (pythonpath includes `.`). `asyncio_mode=auto` so async tests need no decorator (but the existing ones have `@pytest.mark.asyncio`, harmless).
- **Textual version is `8.2.7`** (`textual.__version__`), pinned `textual>=0.86`. Unusual version string but Textual-compatible API.
- **`Label`/`Static` do NOT expose `.renderable`** in this build — to assert rendered text, use the widget's own stored attribute (e.g. `field._label`) or structural `.query(".class")` counts, not `.renderable`.
- **Rendering a screen to a viewable image:** `app.run_test(size=(W,H))` → set values → `app.save_screenshot(path.svg)` produces an **SVG**. There are **no SVG→PNG converters installed** (no rsvg-convert/magick/cairosvg) and **Playwright blocks `file://`** (and its browser backend timed out even via a localhost `http.server`). The working path is **macOS `qlmanage`**: `qlmanage -t -s 1400 -o <outdir> file.svg` → `file.svg.png`, then `Read` the PNG. (Tall screens scroll under `max-height: 90%` + `overflow-y: auto`; capture with a tall `size=(96,64)` and note focus-scroll can push the title above the fold — cosmetic.)
- **CSS in f-string `DEFAULT_CSS`/`CSS`:** escape literal braces as `{{ }}`; interpolate tokens as `{TOKEN}`.
- **ModalScreen test harness:** a bare `class _Host(App): pass`, then `await app.push_screen(screen)` + `await pilot.pause()`; query/assert on `screen`.
- **Textual border types used:** `round` (rounded box), `solid`, `tall`. Chips are 1-row bg-highlight (terminal can't box a 1-row element).
- **ruff line-length = 100.** Long helper/copy strings with em-dashes get split into implicit-concatenated string literals.
- **Screens are tall:** panels use `max-height: 90%; overflow-y: auto;`. Progressive disclosure (Create Build) keeps visible fields to ~4 per method, much shorter than the old always-8.

---

## 11. Key learnings, corrections & DO-NOT-REPEAT (this session)

- **[CORRECTION] STRICT red-green TDD is mandatory** (the `superpowers:test-driven-development` skill). The user caught the `Field` widget written test-alongside; it was **deleted and redone** via a real red→green cycle. Going forward: write the failing test FIRST, run it, watch it fail **for the right reason** (feature missing — `ModuleNotFoundError`/`NoMatches`/assertion), then minimal code → green → refactor. Recorded in `.wolf/cerebrum.md` User Preferences.
- **[CORRECTION] No unique-environment details in mockups OR implementation sample text.** No real hostnames (`blackbird`), usernames (`bgconley`), IPs (`10.25.0.51`), home paths (`/home/bgconley/...`), card models (`rp6000`/RTX PRO 6000), or real run_ids. Use: target `gpu-node`, host `user@gpu-host`, paths `/home/user/...`, config `qwen36-27b-bf16-blackwell`, any Blackwell GPU = **`Blackwell sm_120`**, placeholder run_ids. Recorded in cerebrum. (In Figma we genericized 34 text nodes in one sweep.)
- **[PRINCIPLE] Presentation refactor — preserve contracts.** Wrap existing controls in `Field` (keeps their id + handlers). Progressive disclosure = keep inputs mounted, toggle the wrapper's `display`. Never change a dismiss-payload shape or an id the app/tests query.
- **[PREFERENCE] Self-explaining fields** (every field/selection says what it does + where the value comes from; method/mode choices say what will happen) and **target-aware guidance** where possible. Use `·`/`→`/`—` separators (the terminal renders them; matches the mock) — the user explicitly asked for this polish over ASCII `-`/`->`.
- **[GOTCHA] `.wolf/anatomy.md` is STALE** — it still lists `src/vllm_loader/`; the package is `vela`. Refresh it in Phase 6.
- **[FACT] The 195-test `tests/test_tui_smoke.py` is the regression gate.** It includes whole-flow handoff tests (new_deployment → create_build/adopt_build/download_model) that set screen inputs by id and submit — which is why preserving ids is non-negotiable. The 6–8 "Event loop is closed" warnings are pre-existing asyncio cleanup noise, not failures.

---

## 12. File inventory (this session)

**Created (implementation):**
- `vela-tui-overhaul-implementation-plan-v1.md` (root — canonical plan)
- `src/vela/tui/widgets/field.py`, `keyhintbar.py`, `contextcard.py`, `preset_chips.py`, `validation_card.py`
- `tests/test_tui_widgets.py`, `tests/test_create_build_screen.py`, `tests/test_download_model_screen.py`, `tests/test_adopt_build_screen.py`
- `vela-tui-session-context-2026-06-09.md` (this doc)

**Modified:**
- `src/vela/tui/theme.py` (added full token set)
- `src/vela/tui/widgets/__init__.py` (exports the 5 widgets)
- `src/vela/tui/screens/create_build.py`, `download_model.py`, `adopt_build.py` (refactored to mocks)
- `.wolf/memory.md`, `.wolf/cerebrum.md` (session log + learnings/preferences)

**All implementation work is UNCOMMITTED** on `claude-ui-implementation`. (The Figma handoff doc + the v12 review + the cerebrum design rules were committed earlier as `dbdd7ac` on `main`, which `claude-ui-implementation` is based on.)

---

## 13. Git state summary

- **Current branch:** `claude-ui-implementation` (tracks `origin/claude-ui-implementation`), based on `main` @ `dbdd7ac`.
- **`main` @ `dbdd7ac`** = "Add Vela TUI workflow-screen Figma redesign + handoff" (the Figma phase + handoff doc + v12 review + cerebrum rules). Pushed to origin.
- **`claude-ui-overhaul`** branch: DELETED (local + remote) — it pointed at `f7e61ae` with no unique commits.
- **Uncommitted:** all of Phase 1+2 implementation (theme, 5 widgets, 3 screens, 4 test files, the plan doc, this context doc, `.wolf/` updates). A "commit the Phase 1+2 milestone" checkpoint was offered to the user and is pending their decision.

---

## 14. Cross-references

- **Figma visual spec:** `vela-tui-figma-redesign-handoff.md` (tokens, per-screen specs, **Appendix B** Textual mapping, **Appendix C** exact copy, **§13** node-ID map).
- **Execution plan + living checklist:** `vela-tui-overhaul-implementation-plan-v1.md`.
- **OpenWolf:** `.wolf/memory.md` (chronological log), `.wolf/cerebrum.md` (learnings + User Preferences incl. TDD + no-unique-env rules), `.wolf/anatomy.md` (file map — STALE, refresh in Phase 6), `.wolf/buglog.json`.
- **v1 history / app specs:** `vela-v1-completion-audit-2026-06-07.md`, `vela-docker-composer-review-findings-v6..v12.md`, `vllm-tui-loader-spec-v2-CANONICAL.md`, `vllm-agent-architecture-spec-v1.md`, `vllm-build-management-spec-v1.md`, `vllm-model-management-spec-v1.md`, `vela-deployment-composer-spec-v1.md`, `vela-docker-runtime-spec-v1.md`.
- **Figma MCP:** file `9xUgzyoFqWmd40tV5dwaHv`; page `39:2`; tokens collection `39:3`. Auth is OAuth + short-lived; re-`authenticate` if a future session needs to touch Figma (but the Figma phase is DONE — implementation no longer needs Figma except to re-screenshot a node for reference).

---

## 15. Definition of done for the overhaul

All 6 phases in `vela-tui-overhaul-implementation-plan-v1.md` complete: every workflow screen + the dashboard refactored to its Figma node, each grounded by tests written test-first, all dismiss-payload/id contracts preserved, the full test suite green (Mac-safe), ruff clean, and the shared widget library + token theme in place as the durable "form language." Then real-hardware validation only after rsync to the GPU host (out of scope for the UI work itself). **No screen is "done" until its targeted tests pass AND the full smoke suite stays green AND it has been rendered and eyeballed against its mock.**

*End of context. Resume at §9 (Phase 3): audit target_manager.py + flag_manager.py contracts → build MasterDetail / StatusPill / SourceTag / ResolvedCommandPanel widgets (red→green) → refactor Target Manager (44:2) → refactor Flag Manager (55:2, hardest, recipe cues) → verify (targeted + 195 smoke + render).*
