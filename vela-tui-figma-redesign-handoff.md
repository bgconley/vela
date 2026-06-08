# Vela TUI → Figma Workflow-Screen Redesign — COMPREHENSIVE SESSION HANDOFF

**Created:** 2026-06-08 · **Purpose:** a self-contained, exhaustive record so a future session (human or agent) can resume the Figma redesign work cold, with zero re-derivation. Preserves: the application, the design problem, the design decisions, the Figma project structure + tokens + reusable build code, exactly what has been built, the per-screen specs for everything remaining, the task list with current status, and the precise next actions.

> **Read this top-to-bottom before touching Figma again.** Sections 5–6 (Figma IDs + the reusable build kit, including the *critical spacer bug*) and Section 10 (next steps) are the load-bearing parts for resuming.

---

## 0. TL;DR — resume in 60 seconds

- **App:** **Vela** — a Textual TUI + Typer CLI that launches/monitors/manages **vLLM** servers via a **controller/agent split** (TUI on a workstation, GPU box runs the agent). Repo: `/Users/brennanconley/vibecode/lab-tui`. It is **v1-done** (verified across 12 review rounds; findings docs `vela-docker-composer-review-findings-v6..v12.md`). It is **installed and running on both lab hosts** (Blackbird GPU agent + P620-01 controller).
- **Why we're in Figma:** the user ran the v1 TUI and the **workflow/input screens are confusing and unpolished** (raw, cramped, ambiguous). Root cause: those screens **were never designed** — the canonical Figma mocks only covered the *dashboard/monitoring* screens. We are designing the missing **workflow screens** in Figma **first**, then (later) implementing them in Textual. **NO code is being edited right now** — Figma mocks only.
- **Design decisions (locked by the user):** (1) **Terminal-faithful + max-polished** — design natively for Textual (monospace, box-drawing, real widgets) but use every styling lever Textual has, so the mock is **implementable 1:1**. (2) **Scope = "everything"** — component kit + all 6 workflow screens + the New Deployment wizard + dashboard/log cleanup.
- **Figma:** file `9xUgzyoFqWmd40tV5dwaHv` ("vLLM-TUI-Loader-Screens — Canonical v2"). I created a **new page "Workflow Screens — Redesign v1" (id `39:2`)** with a **"Vela Terminal" token system** (23 color variables `39:4`–`39:26`, 8 IBM Plex Mono text styles). **Built so far:** Target Manager (`44:2`, ✅ verified) and Create Build (`49:2`, built — screenshot verification was the in-flight step when we paused).
- **STATUS (2026-06-08, 2nd session): Figma phase COMPLETE.** All §8.1–§8.6 screens built + screenshot-verified + genericized (no unique-env). Full node-ID map in **§13**. Next phase = Textual implementation (Appendix B), pending user approval — **do NOT edit code until the user approves the mocks.**
- **Figma MCP auth is CONNECTED** this session (OAuth completed). Tools available: `use_figma`, `get_metadata`, `get_screenshot`, `get_variable_defs`, `search_design_system`, `get_design_context`, etc. (load via ToolSearch each session). Skills `figma:figma-use` (MANDATORY before `use_figma`) and `figma:figma-generate-design` were loaded.

---

## 1. The application — Vela (full context)

**What it is:** a phase-aware **Textual TUI + Typer CLI** for launching, monitoring, and managing **vLLM** inference servers from named YAML configs, with managed vLLM **builds**, a model **registry**, and a **controller/agent** architecture. It spawns/monitors `vllm serve` (or a Docker container) as a child; it never `import`s vllm. **One package both sides**; the agent runs on the GPU box as `vela agent connect`/`run`.

**Version:** `0.1.0`. Python ≥3.10/3.12, hatchling, console script `vela = vela.cli:main`. OpenWolf-managed (`.wolf/`). Repo remote `https://github.com/bgconley/vela.git`, branch `main`, current HEAD at session end `f7e61ae` ("Record Vela v1 completion audit").

**v1 status:** **Done** — across 12 multi-agent review rounds (Sonnet-4.6 finders → Opus-4.8 verification → Opus-4.8 synthesis), the architecture was taken from concept to an independently-verified, **hardware-validated v1** with zero high/medium open defects. The 35-item completion punchlist (`vela-v1-completion-punchlist.md`) was executed faithfully with real tests + on-hardware Blackwell FP8/BF16 backend-evidence validation. Two disclosed deviations (foreground wrappers kept as provenance; B11 TUI bootstrap/push affordance deferred as P4). The coder's own honest completion audit lives at `vela-v1-completion-audit-2026-06-07.md`.

**Lab infrastructure (the user's real hardware):**
- **Mac (author box)** — `/Users/brennanconley/vibecode/lab-tui`, no GPU; where this session runs.
- **P620-01 = controller** — `bgconley@10.25.0.50`, hostname `620-01`, RTX PRO 4000 Blackwell, also hosts a self-hosted **GitHub Actions runner** for the remote-validation CI lane.
- **Blackbird = GPU agent** — `bgconley@10.25.0.51`, hostname `blackbird`, **RTX PRO 6000 Blackwell Max-Q (sm120)**, runs Qwen3.6 27B.
- **SSH key (lab-wide):** `/Users/brennanconley/vibecode/infx/ubuntu24_ed25519` (Mac→both hosts). P620→Blackbird uses `~/.ssh/vllm-loader-remote-validation` on P620.

**Current install state (we set this up THIS session — both hosts on `f7e61ae`):**
- **Blackbird:** clean `~/venvs/vela` (vela 0.1.0) from `~/repos/vela`@`f7e61ae`. Old `~/venvs/lab-tui` + `~/venvs/vela`(stale 2542867) removed; old clones renamed `*.bak`. Configs in `~/.config/vela/configs/` (bf16 + fp8 blackbird YAMLs). **Real-hardware validated:** `vela smoke-tui qwen36-27b-bf16-rp6000-blackbird` → `READY http://127.0.0.1:18002`, run_id `c28cb7be…`, exit 0 (~2 min).
- **P620-01:** clean parallel `~/venvs/vela`@`f7e61ae` (CI lane `lab-tui`/`/tank` installs untouched). `blackbird` target re-pointed at Blackbird's new install (discovery probe auto-resolved `agent_command`); `vela targets test blackbird` + `vela doctor --target blackbird` both **all green**.
- **PATH entries added** to `~/.bashrc` on both hosts (`export PATH="$HOME/.local/bin:$HOME/venvs/vela/bin:$PATH"`, behind bash's interactive guard so CI is unaffected) + **`uv` symlinked into `~/venvs/vela/bin/`** on both so the agent detects it (`uv=no`→`uv=yes` in diagnose). `cuda=unknown` is expected (host has no CUDA toolkit; the container carries CUDA).

**Config-discovery gotcha (learned this session):** Vela reads configs from `~/.config/vela/configs/` (the `configs/` **subdir**), NOT `~/.config/vela/` directly. Resolution order (`src/vela/config/loader.py:50-53`): `--configs-dir` → `$VELA_CONFIGS` → `$CWD/configs` → `~/.config/vela/configs`. Targets live in `~/.config/vela/targets.yaml`.

**Why this matters for the redesign:** these screens drive a *real, working* product. The mocks must be implementable in Textual and faithful to the actual data/flows (config names, target fields, build methods, model refs, flag categories) so the Textual implementation can follow them 1:1.

---

## 2. The design problem — why the implemented TUI misses the mark (THE DIAGNOSIS)

The user ran the v1 TUI, successfully launched an LLM, but found the UI "not intuitive at all," "unclear how any of it works," with "way too much ambiguity and friction." After analysis, the root cause is **three missing design inputs** — not bad coding:

1. **The workflow screens were never mocked.** The canonical Figma file ("Polished Textual Rich UX", node `22:2`) contains 14 polished **dashboard/monitoring** screens (Live Load Dashboard, READY Proof, Config Picker, Command Palette, Error Banners, Detached Supervisor, Stop/Kill Confirm, Security Warning, GPU Edge States, Help/Ops, Responsive Narrow). It has **zero** designs for the **workflow/input** screens — Target Manager, Create Build, Download Model, Adopt Build, Flag Manager, and the New Deployment wizard. So the coder built those as raw stacks of `Label + Input` Textual widgets with no hierarchy, spacing, guidance, or affordances → they read like unstyled forms.

2. **No shared "form language."** Each modal reinvents layout. No consistent field component (label · helper · validation), no consistent header/footer, no spacing scale, no semantic color use. Textual *can* do color, Rich markup, rounded-ish panels, and good spacing — the workflow screens use none of it.

3. **A medium mismatch (conceptual).** Even where mocks existed, the canonical screens *look* like web UI to a casual eye; Textual is a terminal grid (monospace cells, box-drawing borders, terminal palette). The fix — and the user's chosen direction — is to design the new mocks **natively for Textual** so the implementation can be faithful 1:1. **Critically, the canonical screens ARE already terminal-faithful** (IBM Plex Mono, dark terminal palette) — so we extend that proven language to the workflow screens.

**Content-altitude problem throughout:** internal data is dumped raw at users — e.g. the **60-method `capabilities` list** in Target Manager, full env-var blocks, raw commit hashes shown as ghost placeholders, agent file paths. Users need **summaries and context**, not the API surface.

---

## 3. The 7 screenshots the user flagged (per-screen problems, verbatim intent)

The user supplied 7 screenshots of the *running* TUI with specific complaints:

1. **Dashboard / run monitor (image #1):** "whitespace issues and crowding at the top." Crowded header with cryptic glyphs (`□ PATH ○ M`), config name wraps mid-word in the left panel, a giant amber security banner eats the log pane, and a broken-looking underscore "sparkline" at the bottom. (This is a *canonical-mocked* screen that drifted in implementation.)
2. **Target Manager (image #2):** "super cramped, unorganized, visually cluttered." A raw key:value dump + a **wall of ~60 comma-separated RPC capability names** (adopt_build, allocate_port, cancel_job, …). The worst offender.
3. **Create Build (image #3):** "incredibly unclear where information is obtained, free text prone to error, input boxes aren't reliably selected/deselected, no indication where to obtain any of the variables." 8 free-text fields shown at once (Label, Package spec, Channel/variant, Python, Commit, Git URL, Wheel/venv path, Environment) regardless of the selected Method.
4. **Download Model (image #4):** same complaints. name/ref/repo/cache read-only header, then Revision-override (a long commit hash shown as a ghost placeholder that looks pre-filled), Allow patterns, Ignore patterns as free text. Tons of dead space.
5. **Adopt Build (image #5):** "literally no idea what to do." Label, Venv path, vLLM version, Version profile + "Copy venv into managed build" checkbox. Unclear what "adopt" means or where to get the venv path.
6. **Flag Manager (image #6):** "a totally unacceptable mess. Nearly unusable. Needs full redesign." Misaligned two-column chaos: preset Select + "Changed only" checkbox, then MODELED/PASSTHROUGH/UNKNOWN lists on the left, a value box + raw-args box + "Resolved command" wall-of-env-vars on the right.
7. **Log shutdown + NCCL warning (image #7):** the user asked specifically about the last log line — see Section 9.

The user said: **"Don't limit yourself to the screens I showed you"** — so the redesign also covers the New Deployment wizard and a shared component kit.

---

## 4. The design decisions (LOCKED by the user via explicit choice)

Two decisions were asked and answered:

**Decision 1 — Design medium:** **"Terminal-faithful, max-polished."** Design natively for Textual — monospace, box-drawing borders, real Textual widgets (Input, Select, Checkbox, Button, Rule, DataTable, Tabs, Markdown, etc.) — but use every styling lever Textual actually has (color, spacing, Rich markup, focus borders, rounded panels). Mocks must be **directly implementable 1:1** so the build finally matches. (Rejected: aspirational web-style; rejected: both-layers.)

**Decision 2 — Scope:** **"Everything."** Component kit + all 6 workflow screens + the New Deployment wizard + dashboard/log cleanup. (This is a large, multi-pass build — pace accordingly.)

**The reusable design language / component system (the real deliverable):**
- **Screen frame:** title bar (screen name + context) · body · footer (keybindings) — identical across all modals.
- **Field component:** bold label · `required *`/`optional` tag · input/select/checkbox · dim **helper line that says where to find the value** · inline ✓/✗ validation.
- **Read-only context cards** visually distinct from editable fields.
- **Progressive disclosure** (show only fields relevant to the current choice, e.g. build method), **live command preview**, **clear focus/tab/enter/esc** semantics, a **spacing scale**, and a small **semantic palette** used consistently.
- **Master–detail** for list screens (Targets, Builds, Models); **summaries over dumps**; **guided wizard** (step indicator + validation gating + review) for New Deployment.

---

## 5. The Figma project — file, pages, extracted design system, tokens created

**File:** `9xUgzyoFqWmd40tV5dwaHv` — "vLLM-TUI-Loader-Screens — Canonical v2". Canonical reference URL the user gave: `https://www.figma.com/design/9xUgzyoFqWmd40tV5dwaHv/vLLM-TUI-Loader-Screens---Canonical-v2?node-id=22-2`.

**Pages in the file (3):**
- `3:2` — "Superseded - initial subset" (23 nodes, old).
- `17:2` — "Superseded - plain terminal inventory" (old).
- `22:2` — **"Polished Textual Rich UX"** — the **current canonical** page, 14 dashboard/monitoring frames (each 1440×900). Key frames: `22:3` (00 Live Load Dashboard), `22:178` (01 READY Proof), `22:336` (02 Config Picker), `22:481` (03 Search/Filter/Pause/Wrap), `22:644` (04 Command Palette), `22:819` (05 Error Banners), `22:957` (06 Readiness Timeout), `22:1095` (07 Detached Supervisor), `22:1238` (08 Stop/Kill Confirm), `22:1383` (09 Security Warning), `22:1518` (10 GPU Edge States), `22:1657` (11 Help/Ops), `22:1792` (12 Responsive Narrow, 760×900), `22:1885` (00A Coverage Note).

**Design system extracted from the canonical page (it has NO Figma variables/styles — all hardcoded; I recreated it as proper tokens):**
- **Font:** **IBM Plex Mono** (Regular, SemiBold, Bold), sizes 8/9/10/11/12/13/15/16px. Dominant: 10–11px Regular body, 11–12px Bold headers/keys.
- **Palette (hex → semantic role):**
  - `#0c141b` base bg · `#101923` panel bg · `#172532` raised/border surface · `#0d151d` inset · darker variants `#091015`/`#0f1a22`/`#14202b`.
  - `#e8f1f2` primary text · `#8ba4ae` secondary/dim text · `#526a75`/`#56707c` faint.
  - **`#67e8a5` green** (active/success/READY — most-used accent) · **`#60d7f8` cyan** (titles/info/focus) · **`#f6c85f` amber** (warn/in-progress phase) · **`#ff6b7a` red** (error).
  - Tinted surfaces: green `#0e2a21` · amber `#2b2410` · red `#2b1218` · blue `#0c2238` · cyan-ish `#0c2330`.
- **Confirmed semantic usage (from text samples):** title "vLLM Loader" = cyan `#60d7f8`; model name = white `#e8f1f2`; phase "LOADING_WEIGHTS" = amber `#f6c85f`; URL = green `#67e8a5`; clock/footer-keys = dim `#8ba4ae`; section title "Configs" = cyan; "3 valid" = green.
- **Visual treatment (from the `22:3` screenshot):** rounded panels (~6–10px radius), 1px subtle borders, lighter panel bg than base, generous-but-dense spacing, green for active/selected rows (left-accent / tinted bg), amber progress lines, cyan titles, a clean footer keybar of `key label` pairs in dim text.

**NEW page + tokens I created this session:**
- **Page "Workflow Screens — Redesign v1" → id `39:2`.** (All new screens live here, laid out left-to-right.)
- **Variable collection "Vela Terminal" → `VariableCollectionId:39:3`, mode `39:0`.** 23 COLOR variables (scopes set to FRAME_FILL/SHAPE_FILL/TEXT_FILL/STROKE_COLOR). **Variable ID map (memorize / reuse):**

| token | VariableID | hex |
|---|---|---|
| bg/base | `VariableID:39:4` | #0c141b |
| bg/panel | `VariableID:39:5` | #101923 |
| bg/raised | `VariableID:39:6` | #172532 |
| bg/inset | `VariableID:39:7` | #0d151d |
| bg/field | `VariableID:39:8` | #0a1118 |
| border/subtle | `VariableID:39:9` | #22384a |
| border/strong | `VariableID:39:10` | #2f5168 |
| border/focus | `VariableID:39:11` | #60d7f8 |
| text/primary | `VariableID:39:12` | #e8f1f2 |
| text/secondary | `VariableID:39:13` | #8ba4ae |
| text/faint | `VariableID:39:14` | #56707c |
| text/onAccent | `VariableID:39:15` | #06120c |
| accent/green | `VariableID:39:16` | #67e8a5 |
| accent/cyan | `VariableID:39:17` | #60d7f8 |
| accent/amber | `VariableID:39:18` | #f6c85f |
| accent/red | `VariableID:39:19` | #ff6b7a |
| accent/blue | `VariableID:39:20` | #5fa8e8 |
| accent/violet | `VariableID:39:21` | #b69cf0 |
| surface/green | `VariableID:39:22` | #0e2a21 |
| surface/amber | `VariableID:39:23` | #2b2410 |
| surface/red | `VariableID:39:24` | #2b1218 |
| surface/blue | `VariableID:39:25` | #0c2238 |
| surface/cyan | `VariableID:39:26` | #0c2330 |

- **Text styles (IBM Plex Mono type ramp), names + style IDs:**
  - `mono/title` — Bold 15/22 — `S:2e85ba341a40abc5aaec92ddaad44278f8778649,`
  - `mono/header` — Bold 12/18 — `S:b3046270935ae5153227ab1fb18291a465dd428a,`
  - `mono/label` — SemiBold 11/16 — `S:a8c91c0dd656e4d81584c8ebf18ab9dd28faec8a,`
  - `mono/strong` — SemiBold 11/16 — `S:00303140d9ec3b2ce630dcc167ecda349e0249b8,`
  - `mono/body` — Regular 11/16 — `S:6dfdba5b0c1d0ed2ca870f2faf8db926cd766e89,`
  - `mono/helper` — Regular 10/15 — `S:24658bc6414fbf7fae3f832754db33fc0ac1cf9f,`
  - `mono/key` — Bold 11/16 — `S:872905956ecbd5168e3c062ba0efeb3a5dfffa09,`
  - `mono/meta` — Regular 10/14 — `S:6e1fcbc5f8a0abf44911792f40693652e2fffa34,`
  - (NOTE: in build scripts I set `fontName`/`fontSize` directly via the `RAMP` map rather than applying these style IDs — simpler & more reliable. The styles exist for documentation / future binding. The trailing comma in IDs is a Figma artifact.)

---

## 6. The reusable build kit (helper code, sizing patterns, the CRITICAL spacer bug)

**Every `use_figma` build script must include this helper preamble** (helpers do NOT persist across calls — each call is a fresh JS context). This is the validated, working version (after fixing the spacer bug):

```js
const page = await figma.getNodeByIdAsync("39:2");
await figma.setCurrentPageAsync(page);                          // page context resets each call
for (const s of ["Regular","SemiBold","Bold"]) await figma.loadFontAsync({family:"IBM Plex Mono", style:s});

// numeric suffix of each VariableID:39:N  (see Section 5 table)
const C={base:4,panel:5,raised:6,inset:7,field:8,subtle:9,strong:10,focus:11,primary:12,secondary:13,faint:14,onAccent:15,green:16,cyan:17,amber:18,red:19,blue:20,violet:21,sgreen:22,samber:23,sred:24,sblue:25,scyan:26};
const vc={}; async function gv(k){ if(!vc[k]) vc[k]=await figma.variables.getVariableByIdAsync("VariableID:39:"+C[k]); return vc[k]; }
async function paint(k){ return figma.variables.setBoundVariableForPaint({type:'SOLID',color:{r:0,g:0,b:0}},'color',await gv(k)); } // returns NEW paint
async function fillV(n,k){ n.fills=[await paint(k)]; }
async function strokeV(n,k,w=1){ n.strokes=[await paint(k)]; n.strokeWeight=w; }

const RAMP={title:["Bold",15,22],header:["Bold",12,18],label:["SemiBold",11,16],strong:["SemiBold",11,16],body:["Regular",11,16],helper:["Regular",10,15],key:["Bold",11,16],meta:["Regular",10,14]};
async function T(chars,role,k){ const t=figma.createText(); const [st,sz,lh]=RAMP[role]; t.fontName={family:"IBM Plex Mono",style:st}; t.fontSize=sz; t.characters=chars; t.lineHeight={unit:"PIXELS",value:lh}; t.fills=[await paint(k)]; return t; }

function AL(dir){ const f=figma.createFrame(); f.layoutMode=dir; f.fills=[]; f.clipsContent=false; f.itemSpacing=0; f.paddingTop=f.paddingBottom=f.paddingLeft=f.paddingRight=0; f.primaryAxisSizingMode="AUTO"; f.counterAxisSizingMode="AUTO"; return f; }

// *** CRITICAL: empty frames default to 100x100. A spacer MUST be resized to 1px tall or it inflates its row. ***
function spacer(parent){ const s=AL("HORIZONTAL"); parent.appendChild(s); s.resize(10,1); s.layoutSizingVertical="FIXED"; s.layoutGrow=1; return s; }

async function dot(k){ const e=figma.createEllipse(); e.resize(7,7); e.fills=[await paint(k)]; return e; }
async function kv(key,val,vk="primary"){ const r=AL("HORIZONTAL"); r.itemSpacing=8; r.counterAxisAlignItems="MIN"; const kk=await T(key,"body","secondary"); kk.resize(96,16); kk.textAutoResize="HEIGHT"; r.appendChild(kk); kk.layoutSizingHorizontal="FIXED"; r.appendChild(await T(val,"body",vk)); return r; }

// field(label,value,helper,{required,optional,focused,placeholder,suffix,helperColor})  -> a vertical field group
async function field(labelText, valueText, helperText, opt={}){
  const col=AL("VERTICAL"); col.itemSpacing=5;
  const lr=AL("HORIZONTAL"); lr.itemSpacing=8; lr.counterAxisAlignItems="CENTER";
  lr.appendChild(await T(labelText,"label","primary"));
  if(opt.required) lr.appendChild(await T("required","helper","amber"));
  else if(opt.optional) lr.appendChild(await T("optional","helper","faint"));
  col.appendChild(lr);
  const box=AL("HORIZONTAL"); box.paddingLeft=11;box.paddingRight=11;box.paddingTop=8;box.paddingBottom=8; box.cornerRadius=6; box.counterAxisAlignItems="CENTER"; box.itemSpacing=1;
  await fillV(box,"field"); await strokeV(box, opt.focused?"focus":"subtle", opt.focused?1.5:1);
  box.appendChild(await T(valueText, "body", opt.placeholder?"faint":"primary"));
  if(opt.focused){ const car=figma.createRectangle(); car.resize(1.5,15); await fillV(car,"cyan"); box.appendChild(car); }  // caret
  if(opt.suffix){ spacer(box); box.appendChild(await T(opt.suffix,"body","secondary")); }  // e.g. "▾" for selects
  col.appendChild(box); box.layoutSizingHorizontal="FILL";
  if(helperText){ const h=await T(helperText,"helper", opt.helperColor||"faint"); col.appendChild(h); h.layoutSizingHorizontal="FILL"; }
  return col;
}
```

**Sizing pattern that WORKS (learned the hard way this session):**
- **Window/modal:** `AL("VERTICAL")`; set width fixed + height hug → `win.resize(W,100); win.layoutSizingHorizontal="FIXED"; win.counterAxisSizingMode="FIXED"; win.primaryAxisSizingMode="AUTO";`
- Children: title bar (`layoutSizingHorizontal="FILL"`, `layoutSizingVertical="HUG"`), a 1px divider frame (`resize(W,1)`, `fillV subtle`, `FILL` width), the body (FILL width), another divider, footer (FILL width, HUG vertical).
- **Master-detail body:** `AL("HORIZONTAL")`; the **right column HUGs vertically (drives the height)**, the **left column + vertical divider FILL vertically** to match. Do NOT set both columns to FILL (nothing drives the height → broken). `body.layoutSizingVertical="HUG"`.
- **`layoutSizingHorizontal/Vertical="FILL"` MUST be set AFTER `appendChild`.**
- **`resize()` before sizing modes** (resize resets them to FIXED).
- **Spacers:** use the `spacer(parent)` helper (resizes to 1px tall) — never an unresized empty frame.
- **Footer keybar:** `AL("HORIZONTAL")`, gap 14; each hint = `AL("HORIZONTAL")` gap 4 of `T(key,"key","cyan")` + `T(label,"helper","secondary")`. Keys list e.g. `[["↑↓","Select"],["⏎","Detail"],["a","Add"],…,["Esc","Close"]]`.

**Layout positions on page `39:2` (lay screens left-to-right with ~60px gaps):**
- Target Manager window `44:2` at `x=120, y=140` (760 wide).
- Create Build window `49:2` at `x=940, y=140` (480 wide).
- Next screens: continue to the right (e.g. Download Model at `x≈1480`, Adopt Build next, Flag Manager wider, wizard steps in a row below at `y≈800`, dashboard full 1440×900 below all). Always scan `figma.currentPage.children` for clear space, or just hardcode increasing x.

**Figma MCP / use_figma gotchas confirmed this session:**
- Colors 0–1 range. Fills/strokes are read-only arrays — clone/reassign (`node.fills=[paint]`).
- `setBoundVariableForPaint` returns a NEW paint — capture it.
- `counterAxisAlignItems` valid values: `MIN | MAX | CENTER | BASELINE` — **`STRETCH` is INVALID** (to stretch children, use `child.layoutSizingVertical/Horizontal="FILL"` instead). This caused the first atomic failure.
- Failed scripts are **atomic** — nothing is created on error; fix and retry.
- `return` is the only output channel; **return all created/mutated node IDs**.
- `get_screenshot` returns a short-lived URL; download with `curl -o file.png "<url>"` then Read the PNG (token-efficient vs base64 inline). `original_width/height` in the metadata = node's true size.
- IBM Plex Mono is available in the file; load Regular/SemiBold/Bold before any text op.

---

## 7. What's been BUILT so far (this session)

1. **Page + tokens + type ramp** — page `39:2`, "Vela Terminal" collection (23 vars), 8 text styles. ✅
2. **Target Manager (master-detail) — node `44:2`** — ✅ **built and visually verified.** Layout: title bar ("Target Manager" cyan + "controller → agent connections" + "2 targets"); left sidebar (full-height, base bg) listing `local` and **`blackbird` selected** (cyan-bordered, tinted); right detail with grouped sections **CONNECTION** (transport/host), **VERSIONS** (agent/controller/protocol + "✓ match" green), **PATHS** (config/runs/builds/models/socket), **AUTH** (none), **CAPABILITIES** ("60 supported ✓ · ⏎ view all" — **collapsed**, replacing the raw 60-method wall); footer keybar (Select/Detail/Add/Edit/Test/Bootstrap/Remove/Close). This is the dramatic before/after fix for screenshot #2. Window hugs to 760×474.
3. **Create Build (method-driven form) — node `49:2`** — **built; screenshot verification was the in-flight step when we paused** (the curl to download the screenshot was mid-retry). Layout: title ("Create Build" cyan + "target: blackbird"); **Method** select ("Nightly ▾") with green helper "uv ✓ on blackbird — nightly & commit can run"; then ONLY the nightly-relevant fields — **Label*** (focused, with caret, "nightly-cu130", helper "short name shown in the build list"), **Channel** ("cu130", helper "CUDA wheel channel · cu121/cu124/cu128/cu130 · wheels.vllm.ai"), **Python** select ("3.12 ▾"), **Environment** (optional, placeholder); a **"▸ WILL RUN"** preview box (inset) showing `uv pip install --pre vllm --extra-index-url https://wheels.vllm.ai/nightly/cu130` in green; footer (Create/Next/Prev/Cancel). Window 480 wide, hugs to ~626 tall. **NEXT ACTION: re-screenshot `49:2` and verify it renders cleanly (the spacer fix is already applied in its build script, so it should be correct — but confirm rows aren't inflated and the preview wraps).**

**Screenshot-download note:** the last curl used a slightly corrupted/typo'd URL (the asset URL had been mangled in a retry) and saved "JSON data" instead of a PNG. Just re-run `get_screenshot` on `49:2` to get a fresh URL, then curl+Read.

---

## 8. Per-screen redesign SPECS (everything remaining)

All screens use the kit in Section 6, the tokens in Section 5, and the screen-frame/field/footer patterns. Each is a window modal on page `39:2` unless noted.

### 8.1 Download Model (fixes screenshot #4) — TODO
- **Title:** "Download Model" + context "target: blackbird".
- **Read-only model context card** (distinct treatment, e.g. raised bg, no edit affordance): `repo Qwen/Qwen3.6-27B-FP8` · `pinned <sha7> ✓` (green check = immutable) · `cache ● cached` (green badge) or `remote-only` (amber). Make the cache state a **badge/pill**, not plain text.
- **Revision:** show "pinned to `abc1234…` (immutable commit)" as a read-only line with an **"override ⏎"** affordance; if overriding, an input with helper "leave blank to use the pinned commit — paste a branch/tag/sha from huggingface.co/<repo>/commits". DO NOT show a hash as a ghost placeholder that looks pre-filled (the #4 bug).
- **Allow / Ignore patterns:** replace free-text with **preset chips** + an "advanced (raw)" toggle. Presets: "safetensors only (`*.safetensors *.json`)" [selected], "everything", "no pickle (`-*.bin -*.pth`)". Show a one-line explanation.
- **Download estimate:** "~14.2 GB · 15 shards" (from model metadata) + a "needs HF_TOKEN" warning pill if gated.
- **Footer:** `⏎ Download   o Override revision   a Advanced patterns   Esc Cancel`.

### 8.2 Adopt Build (fixes screenshot #5) — TODO
- **Title:** "Adopt Build" + a one-line **what-it-does**: "Register an existing vLLM virtualenv as a managed build (no install)".
- **Venv path** field with **live validation**: input `/home/bgconley/venvs/vllm-nightly` + a green "✓ found vllm 0.11.2 · torch 2.x · python 3.12" line (auto-detected) OR red "✗ no vllm importable at this path". This replaces "type the version yourself."
- **Label** (required) helper "short name for the build list".
- **vLLM version** shown **auto-detected & read-only** (not a field to type).
- **Version profile** select with explanation "flag-compatibility profile · usually `current`".
- **Checkbox** "Copy venv into managed build (vs reference in place)" with a helper explaining the tradeoff (copy = isolated/safe; reference = saves disk).
- **Footer:** `⏎ Adopt   space Toggle copy   Esc Cancel`.

### 8.3 Flag Manager (fixes screenshot #6 — "needs full redesign") — TODO, the hardest
- **Toolbar (top):** preset Select ("balanced ▾"), "changed-only" toggle, and a search field. Build/config context as a compact line: "build: current · config: qwen36-27b-bf16-rp6000-blackbird".
- **Main = a TABLE of flags** (use a DataTable-style layout): columns **Flag · Value · Source · Default**. Rows selectable; the selected row highlighted. Source column uses colored tags: `modeled` (cyan), `passthrough` (violet), `unknown` (amber). Group/segment by source via tabs or section headers ("MODELED 5 · PASSTHROUGH 0 · UNKNOWN 7").
- **Selected-row editor:** inline (expand the row) or a right detail pane — type-appropriate input (number/enum/text), with the modeled flag's `→ engine.xxx` mapping shown, and a "reset to default / reset to preset" affordance.
- **Resolved command panel (bottom or right):** the `docker run …`/`vllm serve …` formatted cleanly — **one env var per line**, wrapped, monospace, in an inset box, with masked secrets (`VLLM_API_KEY='••••'`) and a copy affordance. This replaces the env-var wall.
- **Footer:** `↑↓ Select   ⏎ Edit   r Raw args   d Default   p Preset   x Changed-only   / Search   ^S Save   Esc Close`.
- This is the densest screen — budget extra build calls; consider building the table rows via a loop.

### 8.4 New Deployment wizard (6 steps — the centerpiece, ties everything together) — TODO
A guided flow; build each step as a frame in a row, plus a shared **step indicator** header (`Target ▸ Runtime ▸ Model ▸ Customize ▸ Review ▸ Save & Smoke` with the current step highlighted green, completed steps with ✓, future steps dim). Validation gating between steps. Steps:
1. **Target** — pick from the targets registry (a small picker with **connection dots**), defaults to the active target. (Note: in the *implementation* this is currently a static label — the mock should show the intended registry picker.)
2. **Runtime/Build** — radio/segmented choice: existing build · **create build (→ hands off to Create Build flow)** · **adopt venv (→ Adopt Build)** · Docker image · executable. (The impl already supports process/docker/build/executable; the wizard should expose all + the create/adopt handoffs.)
3. **Model** — modes: existing pin · **pin HF repo (+revision)** · adopt local path · bare repo id; a **download now / at launch** toggle; **gated/cached state** shown. (→ can hand off to Download Model.)
4. **Customize** — preset picker → **reuse Flag Manager** for full flag editing → port/exposure/context. Show live per-model suggestions (dtype/kv/TP) + a gated-needs-token warning.
5. **Review** — masked resolved command + a summary card (target/build|image/model/port) + warnings + the `derived[]` field list (auto-derived served-name, port, run-dir, container-name).
6. **Save & Smoke** — `save_config` on the target, then optional **bounded smoke** (load → READY → auto-stop) showing the READY URL/model **or the named failure + remediation**.
- Each step: step indicator header · body · footer (`⏎ Next / Back / Esc Cancel`, with `⏎ Save & Smoke` on the last).

### 8.5 Dashboard + log-view cleanup (fixes screenshot #1) — TODO (full 1440×900 frame, like canonical)
- **3-zone header:** left = brand + target chip (with connection dot); center = active deployment name + **phase pill** + READY URL; right = clock. Drop/clarify cryptic glyphs (`□ PATH ○ M`). Don't wrap the config name mid-word (ellipsize or wrap gracefully).
- **Left rail:** Deployment card (name, not truncated) · **vertical phase stepper** (STARTING ✓ → RESOLVING_MODEL → DOWNLOADING_MODEL → LOADING_WEIGHTS ● → PROFILING_KV → CAPTURING_GRAPHS → SERVER_STARTING → READY, with clear current/done/pending states + per-phase timing) · compact GPU card (name, mem bar, temp/util/power).
- **Security warning → one-line collapsible notice** (not a giant amber banner eating the log).
- **Log pane:** clear header with autoscroll/wrap as labeled pills; **log-level classification** so benign warnings (the NCCL shutdown line, the "binds to 0.0.0.0" notice) are de-emphasized/filterable, not screaming amber; replace the broken underscore "sparkline" with a real throughput sparkline or remove it.
- **Footer keybar** (cleaned).

### 8.6 Component kit reference frame (nice-to-have) — TODO
A labeled frame on the page documenting the primitives (field default/focused/error, select, checkbox, status pills green/amber/red/cyan, context card, table row, button, footer keybar, the color tokens, the type ramp) — so the Textual implementation has a single component spec to map to CSS.

---

## 9. The NCCL warning (screenshot #7) — ANSWER for the user

The last log line — `[rank0]:[W608 … ProcessGroupNCCL.cpp:1575] Warning: WARNING: destroy_process_group() was not called before program exit` — is **benign and expected, NOT an error.** It's PyTorch/NCCL noise emitted when a process exits without explicitly calling `torch.distributed.destroy_process_group()`. It appears **right after "Finished server process"** — i.e. during **shutdown**, which is exactly what the bounded `smoke-tui` does (load → READY → **stop**). NCCL's resources are reclaimed by the OS at exit; no leak, no correctness impact, nothing served was affected. Known, cosmetic vLLM/PyTorch shutdown message.
- **Functionally:** ignore it.
- **UX-wise:** it's a symptom of the log-view problem — benign warnings render like failures. The fix is **log-level classification** (de-emphasize/filter known-benign lines), part of the dashboard/log redesign (§8.5), NOT a runtime change.

---

## 10. TASK LIST — current status + immediate next steps

Live task tracker (TaskCreate IDs #13–#20):
- **#13 Connect Figma + read canonical v2 tokens — ✅ COMPLETED.**
- **#14 Create new page + terminal-faithful component kit — ✅ COMPLETED** (page `39:2`, tokens, type ramp, and the field/section/footer helper patterns proven on the first two screens).
- **#15 Build Target Manager (master-detail) — ✅ COMPLETED** (node `44:2`, verified).
- **#16 Build Create Build (method-driven form) — �− IN PROGRESS** (node `49:2` built; **needs screenshot verification** — that was the exact in-flight step at pause).
- **#17 Build Download Model + Adopt Build — ⬜ PENDING** (specs §8.1, §8.2).
- **#18 Build Flag Manager (table + resolved command) — ⬜ PENDING** (spec §8.3 — hardest).
- **#19 Build New Deployment wizard (6 steps) — ⬜ PENDING** (spec §8.4 — centerpiece).
- **#20 Build Dashboard + log-view cleanup — ⬜ PENDING** (spec §8.5).
- (Implied) Component-kit reference frame §8.6 + final full-page screenshot for the user.

**IMMEDIATE NEXT STEPS (in order):** — ⚠️ SUPERSEDED: the Figma phase is COMPLETE (2026-06-08). See **§13** for final status + node IDs. The steps below are historical.
1. **Re-screenshot `49:2`** (`get_screenshot fileKey=9xUgzyoFqWmd40tV5dwaHv nodeId=49:2 maxDimension=560`), curl the fresh URL to a PNG, Read it, confirm Create Build renders cleanly (no inflated rows; preview wraps). Fix if needed. Mark #16 complete.
2. **Check in with the user** showing Target Manager + Create Build (the two exemplars covering master-detail + form patterns) to confirm visual direction BEFORE grinding the remaining 5 screens. (The user explicitly said "I'll check in after the component kit + first couple screens.")
3. On confirmation, build in order: **Download Model → Adopt Build → Flag Manager → New Deployment wizard (6 steps) → Dashboard/log cleanup → component-kit reference frame.** Use the Section 6 kit. Screenshot-verify each.
4. When all screens are built, take a full-page screenshot of `39:2`, present to the user, and (separately, later, only when the user says so) plan the Textual *implementation* (mapping tokens → Textual CSS theme, building shared widgets for field/select/footer/master-detail). **Do NOT edit code until the user approves the Figma mocks.**

**Pacing note:** this is a large multi-pass build. Each screen ≈ 1 build call + 1 fix call + 1 screenshot. Budget accordingly. The spacer bug + sizing pattern are now solved, so subsequent screens should need fewer fix passes.

---

## 11. Figma MCP operational notes (for resuming cold)

- **Auth:** the Figma MCP (`plugin:figma:figma`, remote `mcp.figma.com`) was authenticated this session via OAuth (`authenticate` → user opened URL → pasted `localhost:<port>/callback?code=…` → `complete_authentication`; note the flow is short-lived — if it says "no flow in progress," re-call `authenticate` for a fresh URL). If a future session starts disconnected, the only available tools will be `authenticate`/`complete_authentication`; re-auth first. Once connected, the design tools register as deferred — **load them via ToolSearch each session**: `mcp__plugin_figma_figma__use_figma`, `…get_screenshot`, `…get_metadata`, `…get_variable_defs`, `…search_design_system`, `…get_design_context`.
- **Skills:** load **`figma:figma-use`** (MANDATORY before any `use_figma` call — Plugin API rules) and **`figma:figma-generate-design`** (section-by-section build workflow). Pass `skillNames:"figma-use,figma-generate-design"` on `use_figma` calls (logging only).
- **`use_figma` requires `fileKey`** (`9xUgzyoFqWmd40tV5dwaHv`) + `code` + `description`. Operates on that file. Page context resets each call → `await figma.setCurrentPageAsync(page)` at the start.
- **Validation loop:** after each build call, `get_screenshot` the node → curl the URL → Read the PNG → fix with a targeted call. Don't rebuild whole screens to fix one thing.
- **Temp files:** download screenshots to `/Users/brennanconley/.claude/jobs/<jobid>/tmp/` (job-scoped). This session used `/Users/brennanconley/.claude/jobs/1ec9bb76/tmp/` — that jobid will differ next session; use the current `$CLAUDE_JOB_DIR/tmp`.

---

## 12. Cross-references & artifacts

- **App repo:** `/Users/brennanconley/vibecode/lab-tui` (branch `main`, HEAD `f7e61ae`).
- **Review history (context on the app's v1):** `vela-docker-composer-review-findings-v6.md` … `v12.md`, `vela-v1-completion-punchlist.md`, `vela-v1-completion-audit-2026-06-07.md`, `vela-session-context-2026-06-06.md`.
- **OpenWolf:** `.wolf/memory.md` (session log — updated this session), `.wolf/cerebrum.md` (learnings), `.wolf/anatomy.md` (file map), `.wolf/buglog.json`.
- **Figma file:** `9xUgzyoFqWmd40tV5dwaHv`; canonical page `22:2`; **redesign page `39:2`**; tokens collection `39:3`; built nodes `44:2` (Target Manager), `49:2` (Create Build).
- **This handoff doc:** `vela-tui-figma-redesign-handoff.md` (repo root).

**Definition of done for the Figma phase:** all of §8.1–§8.6 built on page `39:2` in the terminal-faithful language, each screenshot-verified, presented to the user, and approved as the spec for the eventual Textual implementation. Only after approval does any TUI *code* change begin.

*End of handoff. Resume at Section 10, step 1.*

---

## APPENDIX A — Exact structure of the two built screens (for regeneration/extension)

### A.1 Target Manager (`44:2`) — node tree
```
win (VERTICAL, FIXED 760w, HUG h, cornerRadius 10, fill panel, stroke strong 1) "Target Manager — redesign"
├─ titlebar (HORIZONTAL, FILL w, HUG h, pad 18/14, gap 10, counter CENTER)
│   ├─ T("Target Manager", title, cyan)
│   ├─ T("controller → agent connections", helper, faint)
│   ├─ spacer()                       ← layoutGrow 1, 1px tall
│   └─ T("2 targets", meta, secondary)
├─ divider (760x1, fill subtle, FILL w)
├─ body (HORIZONTAL, FILL w, HUG h)
│   ├─ left (VERTICAL, FIXED 248w, FILL h, fill base, pad 10/12, gap 4)
│   │   ├─ T("TARGETS", meta, faint, letterSpacing 6%)
│   │   ├─ trow("local","local", sel=false)   ← HORIZONTAL row, dot(green)+name+spacer+transport
│   │   └─ trow("blackbird","ssh", sel=true)   ← fill surface/cyan, stroke focus 1, cornerRadius 6
│   ├─ vdivider (1xN, fill subtle, FILL h)
│   └─ right (VERTICAL, FILL w, HUG h ← DRIVES BODY HEIGHT, pad 18/14, gap 14)
│       ├─ detailHeader (HORIZONTAL): T("blackbird",header,primary)+dot(green)+T("connected",body,green)+spacer()+T("12ms",meta,faint)
│       ├─ section("CONNECTION", [kv("transport","ssh"), kv("host","bgconley@10.25.0.51")])
│       ├─ section("VERSIONS",   [row: agent 0.1.0 · controller 0.1.0 · protocol 1 · "✓ match"(green)])
│       ├─ section("PATHS",      [kv config/runs/builds/models/socket, vk=secondary])
│       ├─ section("AUTH",       [row: "none"(secondary) + "token not required"(helper,faint)])
│       └─ section("CAPABILITIES",[row: "60 supported"(primary)+"✓"(green)+spacer()+"⏎ view all"(helper,faint)])
├─ divider (760x1, fill subtle, FILL w)
└─ footer (HORIZONTAL, FILL w, HUG h, pad 18/10, gap 14): keyHint pairs for Select/Detail/Add/Edit/Test/Bootstrap/Remove/Close
```
`section(title, rowFns)` = VERTICAL gap 4 with a `T(title, meta, faint, letterSpacing 6%)` header then each row FILL-width.

### A.2 Create Build (`49:2`) — node tree
```
win (VERTICAL, FIXED 480w, HUG h, radius 10, fill panel, stroke strong) "Create Build — redesign" @ (940,140)
├─ titlebar: T("Create Build", title, cyan) + spacer() + T("target: blackbird", helper, faint)
├─ divider
├─ form (VERTICAL, FILL w, pad 18/16, gap 14)
│   ├─ field("Method","Nightly", "uv ✓ on blackbird — nightly & commit can run", {suffix:"▾", helperColor:"green"})
│   ├─ field("Label","nightly-cu130","short name shown in the build list", {required:true, focused:true})
│   ├─ field("Channel","cu130","CUDA wheel channel · cu121 / cu124 / cu128 / cu130 · wheels.vllm.ai", {})
│   ├─ field("Python","3.12","interpreter for the managed venv", {suffix:"▾"})
│   ├─ field("Environment","KEY=value  KEY2=value","extra env applied during the build step", {optional:true, placeholder:true})
│   └─ preview (VERTICAL, inset bg, stroke subtle, radius 6, pad 12/10): T("▸ WILL RUN", meta, faint) + T(command, body, green)
├─ divider
└─ footer: keyHint pairs Create/Next/Prev/Cancel
```
**Progressive disclosure rule:** the visible fields are a function of `Method`. The mock shows the **nightly** set. Document the other method field-sets (for the implementation) in Appendix C.

---

## APPENDIX B — Textual implementation mapping (for the LATER code phase — do not start until mocks approved)

When the Figma mocks are approved, the implementation maps cleanly because the mocks are terminal-faithful. Mapping guidance:

**B.1 Tokens → Textual CSS theme.** Define a Textual theme / CSS variables mirroring the "Vela Terminal" collection:
```
$bg-base:    #0c141b;   $bg-panel:  #101923;   $bg-raised: #172532;   $bg-inset: #0d151d;  $bg-field: #0a1118;
$border-subtle: #22384a; $border-strong: #2f5168; $border-focus: #60d7f8;
$text-primary: #e8f1f2; $text-secondary: #8ba4ae; $text-faint: #56707c;
$accent-green: #67e8a5; $accent-cyan: #60d7f8; $accent-amber: #f6c85f; $accent-red: #ff6b7a;
$surface-green: #0e2a21; $surface-amber: #2b2410; $surface-red: #2b1218; $surface-cyan: #0c2330;
```
These belong in a shared `tui/styles/theme.tcss` (or the app's existing CSS). The current screens hardcode colors ad hoc; centralize them.

**B.2 Shared widgets to build (the missing "form language").** Create reusable Textual widgets/compose helpers so every workflow screen is consistent:
- `Field(label, *, required=False, helper="", validator=None)` — a compound widget: a `Label` (bold), an `Input` (or `Select`/`Checkbox`), and a dim helper `Static`; shows inline validation. This is the single biggest fix — it replaces ad-hoc `Label`+`Input` stacks.
- `ScreenFrame` — a `ModalScreen` base with a consistent title bar (`#title`), body container, and a footer key-hint bar built from the screen's `BINDINGS`. Standardize padding/border (`border: round $border-strong;`).
- `KeyHintBar` — renders `BINDINGS` as `key label` pairs (key in cyan, label dim) — replaces the ad-hoc footer strings.
- `StatusPill(text, kind)` — kind ∈ green/amber/red/cyan for connection/phase/validation states.
- `MasterDetail` — a two-pane container (list + detail) for Target/Build/Model managers; the detail uses grouped `Section(title, rows)` blocks.
- `ContextCard` — a read-only, raised-bg card for "what you're operating on" (model summary, target summary), visually distinct from editable fields.
- `ResolvedCommandPanel` — a scrollable, monospace, secret-masked, one-env-var-per-line block with copy (`c` binding). Reused by Flag Manager + the wizard Review step.

**B.3 Behavior fixes the mocks imply (and that the current TUI lacks):**
- **Reliable focus/selection:** the user reported "input boxes aren't reliably selected/deselected." Audit Textual focus handling — ensure a clear focus border (`$border-focus`), `Tab`/`Shift+Tab` order, and that clicking/entering a field reliably focuses it and `Esc` blurs/cancels.
- **Progressive disclosure:** the Create Build screen should `mount`/`remove` (or `display`-toggle) fields based on the selected `Method`, not show all 8 at once.
- **Ghost-placeholder fix:** never use a real value (e.g. a commit hash) as an `Input` placeholder — it reads as pre-filled. Use a true hint ("leave blank to use the pinned commit").
- **Summaries over dumps:** Target Manager must NOT render the raw capabilities list; show "60 supported ✓" with an optional expander. Resolved commands: one env var per line; secrets masked.
- **Log-level classification:** classify known-benign lines (NCCL `destroy_process_group` shutdown warning; the `binds to 0.0.0.0` notice) as benign so they don't render as errors; make them filterable. This addresses screenshot #7.
- **Header de-crowding:** the dashboard header glyphs `□ PATH ○ M` are cryptic — label them or move to a status detail; never wrap the config name mid-word.

**B.4 Files likely involved (from the v1 codebase):** `src/vela/tui/app.py` (dashboard, header, log view, command palette), `src/vela/tui/screens/` (`target_manager.py`, `create_build.py`/`adopt_build.py`, `download_model.py`, `flag_manager.py`, `new_deployment.py`, `target_edit.py`, `model_manager.py`, etc.), and a new shared `tui/widgets/` package for the reusable components above + `tui/styles/theme.tcss`. **Confirm exact filenames via the repo before editing** (do not trust this list blindly — it reflects the v1 structure).

---

## APPENDIX C — Exact field copy & method field-sets (drop-in strings for mock + implementation)

### C.1 Create Build — field-set per Method (progressive disclosure)
- **pip** → Label* · Package spec (`vllm==0.11.2`) · Channel/index (`--index-url`/`--extra-index-url`) · Python · Environment(optional). Helper for Package spec: "pip requirement · `vllm==0.11.2` or `vllm`".
- **nightly** → Label* · Channel (cu121/cu124/cu128/cu130) · Python · Environment(optional). Preview: `uv pip install --pre vllm --extra-index-url https://wheels.vllm.ai/nightly/<channel>`. (uv REQUIRED — show uv-availability status.)
- **commit** → Label* · Commit (full sha; helper "git sha from github.com/vllm-project/vllm/commits") · Channel · Python. Preview: `uv pip install --pre vllm @ https://wheels.vllm.ai/<sha>/...`. (uv REQUIRED.)
- **git** → Label* · Git URL (`https://github.com/vllm-project/vllm.git`) · Ref (branch/tag/sha) · Python · Environment(optional). Preview: `uv pip install 'vllm @ git+<url>@<ref>'`.
- **wheel** → Label* · Wheel path (`/agent/wheels/vllm.whl`, helper "absolute path on the TARGET") · Python. Preview: `uv pip install <wheel-path>`.
- **adopt** → handled by the separate Adopt Build screen (§8.2).
- Cross-cutting: each Method shows a **uv availability** line (green "uv ✓ … nightly & commit can run" or amber "uv not found — only pip/git/wheel available · install via `vela build doctor`").

### C.2 Download Model — exact copy
- Context card: `repo`, `pinned <sha7> ✓ immutable`, `cache ● cached`/`◐ partial`/`○ remote-only`, `size ~14.2 GB · 15 shards`, gated → amber pill "needs HF_TOKEN".
- Revision: "pinned to `<sha>` — immutable" + "override ⏎"; override input helper "branch/tag/sha from huggingface.co/<repo>/commits · blank = keep pinned commit".
- Allow patterns presets: `safetensors only` (`*.safetensors *.json`) [default] · `everything` · `no pickle`. Advanced raw toggle: free-text `Allow` / `Ignore`.
- Footer: `⏎ Download   o Override revision   a Advanced patterns   Esc Cancel`.

### C.3 Adopt Build — exact copy
- Subtitle: "Register an existing vLLM virtualenv as a managed build (no install)."
- Venv path helper: "absolute path on the TARGET · e.g. /home/bgconley/venvs/vllm-nightly". Validation lines: green "✓ found vllm 0.11.2 · torch 2.6 · python 3.12" / red "✗ no importable vllm at this path".
- vLLM version: read-only "0.11.2 (auto-detected)".
- Version profile helper: "flag-compatibility profile · usually `current`".
- Checkbox: "Copy venv into managed build" helper "copy = isolated & safe · uncheck to reference in place (saves disk, but external changes affect the build)".
- Footer: `⏎ Adopt   space Toggle copy   Esc Cancel`.

### C.4 Flag Manager — exact copy
- Toolbar: `preset ▾` (balanced/throughput/long-context/low-memory/qwen3-text) · `[x] changed only` · `/ search`. Context: "build: current · config: <name>".
- Table header: `FLAG  VALUE  SOURCE  DEFAULT`. Source tags: modeled (cyan) / passthrough (violet) / unknown (amber). Counts line: "MODELED 5 · PASSTHROUGH 0 · UNKNOWN 7".
- Modeled row example: `gpu-memory-utilization  0.95  modeled  → engine.gpu_memory_utilization  (default 0.90)`.
- Unknown-to-build example: `--max-num-batched-tokens 8192  ·  --trust-remote-code  ·  --language-model-only`.
- Resolved command panel header "RESOLVED COMMAND" with one env var per line + masked `VLLM_API_KEY='••••'` + `c copy`.
- Footer: `↑↓ Select   ⏎ Edit value   r Raw args   d Default   p Preset   x Changed-only   / Search   ^S Save   Esc Close`.

---

## APPENDIX D — Design principles (durable) + anti-patterns this redesign kills

**Principles (apply to every screen):**
1. **One job per screen, guided.** Title says what you're doing; fields are ordered and gated; a footer says the available actions. No blank canvases.
2. **Tell me where the value comes from.** Every input has a helper line citing the source (a URL, a registry, the target, "auto-detected"). Free text without guidance is the enemy.
3. **Progressive disclosure.** Only show fields relevant to the current choice (method, runtime, model mode). Hide the rest.
4. **Read-only context vs editable fields are visually distinct.** Context cards (raised bg, no caret) vs fields (field bg, focus border, caret).
5. **Summaries over dumps.** Never show internal API surface (capabilities), full env blocks, or raw hashes-as-placeholders. Collapse, badge, ellipsize, or mask.
6. **Live preview.** Show the resolved command/action the inputs will produce, with secrets masked.
7. **Semantic color, used sparingly & consistently.** green=active/success/match, cyan=title/info/focus, amber=warn/in-progress/required, red=error. Dim everything else.
8. **Terminal-faithful.** Monospace grid, box-drawing/rounded panels, real Textual widgets. The mock = the spec; the implementation must match 1:1.

**Anti-patterns being eliminated (from the 7 screenshots):**
- Capability/RPC-method walls dumped at users (Target Manager). → collapse to a count.
- All form fields shown at once regardless of mode (Create Build). → progressive disclosure.
- Values shown as ghost placeholders that look pre-filled (Download Model revision). → true hints.
- "Type the version/path yourself" when it can be auto-detected (Adopt Build). → validate & auto-detect.
- Misaligned multi-column chaos + env-var walls (Flag Manager). → table + formatted resolved-command panel.
- Crowded cryptic headers + mid-word wrapping + log noise reading as errors (Dashboard). → 3-zone header, phase stepper, log-level classification.

*End of appendices.*

---

## 13. STATUS — Figma phase COMPLETE (2026-06-08, second session)

All §8.1–§8.6 screens are built on page `39:2`, each screenshot-verified, and fully **genericized** (no unique-environment details). Node-ID map (current):

| Screen | Node(s) | Notes |
|---|---|---|
| Target Manager | `44:2` | master-detail; 60-capability wall collapsed to "60 supported ✓" |
| Create Build | `49:2` | method-driven form; rebuilt from 48:2 with added per-field context |
| Download Model | `50:2` | read-only context card, revision override affordance, preset chips |
| Adopt Build | `52:2` | live venv-validation card, copy checkbox |
| Flag Manager | `55:2` | table + detail + resolved-command; recipe-safety cues (rebuilt from 53:2) |
| New Deployment wizard | `56:2`(Target) `57:2`(Runtime) `57:72`(Model) `57:150`(Customize) `58:2`(Review) `58:68`(Save & Smoke) | shared step-indicator; steps hand off to the screens above |
| Dashboard (run monitor) | `60:2` | 1440×900; 3-zone header, vertical phase stepper, GPU card, log-level classification |
| Component Kit (reference) | `61:2` | tokens, type ramp, every primitive — the impl spec |

**Genericization rule (locked in `.wolf/cerebrum.md`):** never put unique env in mocks — hostname→`gpu-node`, host→`user@gpu-host`, paths→`/home/user/…`, config→`qwen36-27b-bf16-blackwell`, any Blackwell card→**"Blackwell sm_120"**, run_id→placeholder.

**Two refinements from user review this session:** (1) every field/selection explains what it does + where its value comes from, and method/mode choices say what will happen — applied across all screens; (2) Flag Manager flags recipe-critical precision flags (`dtype`, `kv-cache-dtype`) with `recipe` tags + a "Recipe-protected" warning.

**NEXT PHASE (only when the user approves the mocks):** implement in Textual per Appendix B — tokens→TCSS theme; shared widgets (`Field`, `ScreenFrame`, `KeyHintBar`, `StatusPill`, `MasterDetail`, `ContextCard`, `ResolvedCommandPanel`); behavior fixes (reliable focus, progressive disclosure, log-level classification). **No code changes until approved.**

*Figma phase done. Resume at §13 → await approval → implement per Appendix B.*
