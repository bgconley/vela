# vLLM Build Management — Feature Specification & Implementation Plan (v1)

**Feature:** managed, selectable, independently-runnable vLLM builds · **Status:** spec-ready · **Audience:** the engineer(s) extending `vllm-loader`.

> **Relationship to the canonical spec.** This document is an **additive extension** to `vllm-tui-loader-spec-v2-CANONICAL.md`. It reuses that architecture wholesale — the `VllmProfile`, command builder, scrubbing log sink, message taxonomy, phase FSM, sidecar/manifest discipline, and Textual UI conventions — and adds one new subsystem (`builds/`) plus small, surgical schema and UI additions. Where this document references “the canonical spec” it means that file. Nothing here removes or contradicts it; it slots beneath the existing `command.executable` mechanism (canonical §7.3).

-----

## 0. Document status & what this adds

The canonical loader can already *point at* a specific vLLM binary (`command.executable`, §7.3) and adapts flag spellings to it (`VllmProfile`, §7.2). What it cannot do today:

- **Create / own** isolated vLLM installations (a stable release, a nightly, a per-commit wheel, a source build for NVFP4/cutlass, or an adopted external venv).
- **Select** among multiple builds as a first-class, always-visible TUI capability.
- **Run a build independently** of the TUI (the TUI is a manager, never a runtime dependency).
- **Manage flags scoped to the selected build** — browse that build’s *actual* `vllm serve --help`, edit modeled vs passthrough flags, soft-validate, and persist into a config.

This spec designs all four. The motivating case: *“I need a specific nightly (or source build) for cutlass/NVFP4 on Blackwell, and I want to pick it per config without hand-managing venvs.”*

**Verified grounding (Appendix D).** All vLLM install/build facts were checked against current vLLM docs and context7; version-specific knowledge stays out of hardcoded logic and is **detected at build-create time** and stored in the build manifest, mirroring the canonical “never bake vLLM specifics” rule (§2.7).

-----

## 1. Vision & elemental concepts

### 1.1 What a “build” is

A **build** is an isolated, resolved vLLM installation the loader can launch against: one **per-build virtual environment** containing a specific `vllm` + its matching `torch` (with bundled CUDA userspace libs) + a Python interpreter + compiled kernels for a target GPU arch set. A vLLM install is a tightly-coupled `(vllm, torch, CUDA-build, python-ABI, kernel-arch)` bundle that **cannot coexist** with a different bundle in one `site-packages` — so the venv is the natural unit, and one venv = one selectable build.

### 1.2 Why per-build venv, and why `uv`

- **Isolation:** each build owns its Python+torch+vllm, so builds are independently creatable / deletable / launchable.
- **`uv` is the preferred installer** (with `pip` fallback). Two reasons beyond speed: (1) `uv` can target or download an arbitrary Python per build; (2) **`uv`’s index-priority semantics are required** to install nightly and per-commit wheels correctly — plain `pip` merges `--extra-index-url` with PyPI and silently picks the *released* version over the dev wheel (vLLM issues #27877/#28438). `uv` is **detected, not required**: absent `uv`, fall back to `python -m venv` + `pip`, and *disable* the nightly/per-commit index methods with a clear message rather than installing the wrong thing.

### 1.3 The independent-runnability guarantee (hard requirement)

**The TUI manages builds; it is never required to run them.** Every build directory is self-contained and absolute-pathed, with a generated `run.sh`, a stable `bin/vllm`, and an `activate` pointer. Deleting the loader, its configs, or its process **never disables a build**. A human can `source …/builds/<id>/activate` or run `…/builds/<id>/bin/vllm serve …` with zero loader involvement. This is enforced by the artifacts in §7.4 and is the property that lets the same builds be used by `systemd`, tmux, CI, or another person.

### 1.4 Selection is first-class

The **active build** is shown in the header next to the model/phase indicators (§9.2), changeable with one key (`b`) or the palette, and the build a load will use is always unambiguous: an explicit per-config pin wins over the global default, which wins over bare `vllm` on `PATH` (precedence §7.9).

### 1.5 Flag management is an offshoot of build selection

Because a build *is* a concrete `vllm serve --help`, flag management is naturally **scoped to the selected build**: the FlagManager (§9.5) reads that build’s cached help, distinguishes **modeled** flags (schema-backed `EngineConfig` fields, via `VllmProfile.flag_map`) from **passthrough** flags (`extra_args`), soft-validates against the build’s known enum sets, and persists edits back into a config. It is deliberately specified as a dependent capability, not a parallel one.

-----

## 2. Scope & non-goals

**In scope (v1):**
- A build registry under an XDG **data** dir, with per-build manifests (identity-only, atomic writes).
- Build creation as a **streamed background job**: pip-pinned, nightly, per-commit wheel, git source build, local wheel, and **adopt-existing-venv**.
- Global-default selection + per-config pin; resolution into the existing launch path.
- Concurrency/locking/refcounting so a build in use cannot be mutated or deleted.
- Integrity verification (create-time proof + cheap pre-launch re-check).
- Build-scoped flag management (the offshoot).
- CLI parity (`vllm-loader build …`) so everything is scriptable headlessly.
- Fixes to the version/flag-detection friction the feature exposes (§7.8).

**Out of scope (v1):** managing CUDA toolkits/drivers themselves; cross-machine build sync (artifacts are sync-*friendly* but the loader doesn’t orchestrate it); a package-version solver/“upgrade all builds” flow; GUI editing of arbitrary `pyproject` build args beyond the modeled env vars; Windows (Linux-primary, per canonical NFR-4); ROCm/CPU build variants beyond passthrough (future, §13).

-----

## 3. Functional requirements

**Registry & lifecycle**
- **FR-B1** Discover builds under the builds root; list with status, resolved versions, in-use, and default markers; tolerate malformed/half-written manifests by skipping (mirror `discover_active_sidecars`).
- **FR-B2** Create a build via a chosen method (pip-pinned · nightly · per-commit wheel · git source · local wheel · adopt-existing) as a non-blocking streamed job with live scrubbed log + phase/progress.
- **FR-B3** Verify/repair a build: re-derive the manifest from the venv, recompute integrity, regenerate helper artifacts; mark drift as `broken`.
- **FR-B4** Remove/GC a build, **refusing** if it is in use; clear/repoint the global default if it pointed at the removed build.
- **FR-B5** Adopt an existing external venv: verify it really contains a working vLLM **before** writing anything; synthesize an `adopted` manifest.

**Selection & resolution**
- **FR-B6** Maintain a single **global default** build pointer, atomically updated.
- **FR-B7** Allow a config to **pin** a build (by id or label) that overrides the global default for that config.
- **FR-B8** Resolve a selected build into the existing launch path: hand off `executable` + `python` + an **env overlay** (`VIRTUAL_ENV` + `PATH` prepend) + pre-detected `vllm_version`/`version_profile`, so subprocess vLLM resolves inside the build with no activation script.
- **FR-B9** Enforce precedence (§7.9) deterministically and surface it in the UI (which build a load will use, and why).

**Integrity & safety**
- **FR-B10** Prove a working vLLM at create/adopt time (`vllm --version` **and** `import vllm`, must agree) before `status=ready`/`adopted`.
- **FR-B11** Re-verify cheaply before every launch (status ∈ {ready, adopted}; executable resolves; fast hash match); block launch on a `broken` build with a named error.
- **FR-B12** Refcount live runs against a build via verified sidecar identity (anti-PID-reuse), so a crashed run cannot pin a build and a recycled PID cannot masquerade as in-use.

**Flag management (offshoot)**
- **FR-B13** Browse the selected build’s actual flags (from cached `serve --help`), partitioned into modeled / passthrough / unknown-to-build.
- **FR-B14** Edit modeled flags as typed values and passthrough flags as raw tokens, with a live resolved-command preview (reuse `build_command().preview`).
- **FR-B15** Soft-validate edits against the build’s known enum sets and `known_flags` (warn, don’t block); treat a missing `require_flags` entry as a hard, named error.
- **FR-B16** Persist edits into a config’s `engine.*` (modeled) or `extra_args` (passthrough); confirm before overwriting a hand-edited YAML.

**CLI**
- **FR-B17** Provide `build` subcommands (`add`/`list`/`select`/`inspect`/`verify`/`remove`/`adopt`/`run`) mirroring the TUI, usable headlessly.

## 4. Non-functional requirements

- **NFR-B1 Independent runnability:** a build is fully usable without the loader (hard guarantee, §1.3 / §7.4).
- **NFR-B2 Non-blocking installs:** creation runs in a background worker; the UI stays responsive (canonical NFR-1).
- **NFR-B3 Identity rigor:** build identity and refcounting reuse the sidecar’s anti-PID-reuse discipline (canonical §7.10).
- **NFR-B4 Version knowledge isolation:** no vLLM version specifics are hardcoded; they are detected at create time and stored in the manifest (canonical §2.7/NFR-9).
- **NFR-B5 Reproducibility:** a build’s provenance (exact spec/commit/wheel/index) is recorded; a config+build can be exported as a standalone runnable script.
- **NFR-B6 Safety:** destructive ops (remove, overwrite) are guarded and confirmed; a build in use is immutable.
- **NFR-B7 Security:** install logs are `0600` (may contain index/token/git creds); manifests are identity-only with **no secrets**; secrets are masked/redacted in previews and exported scripts (canonical §7.9/§7.5).
- **NFR-B8 Back-compat:** existing configs (with or without `command.executable`) behave exactly as today when no builds exist (§7.9).
- **NFR-B9 Testability:** registry, manifest, resolver, install-FSM, and flag logic are importable and testable without a GPU or a real vLLM (fakes + a stub installer).

-----

## 5. Architecture

### 5.1 New component, existing seams

```
                ┌──────────────── TUI (existing) ───────────────┐
                │ Header(+ActiveBuild) • BuildManagerScreen •     │
                │ CreateBuildScreen • FlagManagerScreen •         │
                │ (reuses RichLog, ProgressLine, ErrorBanner)     │
                └───────▲────────────────────────▲────────────────┘
                        │ messages (existing +    │ build-job stream (reuses LogSink)
                        │ BuildPhaseChanged)      │
   ┌───────────────┐  ┌─┴───────────── builds/ (NEW) ─────────────┴─┐  ┌──────────────┐
   │ config/schema │  │ registry  • manifest • resolver •            │  │ engine/       │
   │ (+command.    │─▶│ installer (uv/pip) • install-FSM • locks/refs│─▶│ command_builder│
   │  build pin)   │  │ • integrity • independent-run artifacts      │  │ + process_mgr │
   └───────────────┘  └──────────────────────────────────────────────┘  └──────┬───────┘
                            │ writes                         hands off          │ spawn
                            ▼ ~/.local/share/vllm-loader/builds/<id>/           ▼ vllm child
                        build.json · venv/ · bin/vllm · run.sh · install.log    (in build venv)
```

**Single integration chokepoint.** A selected build resolves to a small record (§7.5) that feeds the *existing* `build_command(...)` and `process_manager` launch. The build layer **never spawns the server itself** — it only installs builds and hands an executable + env overlay to the canonical launch path.

### 5.2 Composition with existing infra (reuse, don’t rebuild)

| Need | Reused existing piece (anchor) |
|---|---|
| Stream install output, scrubbed, to log + file | `engine/log_sink.py` `LogSink` (`feed`/`close`/`scrub`, `0600` file) |
| Surface install lines / progress / errors in UI | `messages.py` `LogLineCommitted` / `ProgressUpdated` / `EngineError`; RichLog + ProgressLine + ErrorBanner in `tui/app.py` |
| Atomic, identity-rigorous on-disk artifact | `engine/sidecar.py` `write_atomic` / `_write_private_text` / ULID ids / identity verify |
| Select vLLM binary at launch | `command_builder.py` `_base_argv` (`executable` substitution point) |
| Adapt flags to the build’s vLLM | `engine/profile.py` `select_profile_for_config` + `collect_serve_help` + `flag_map`/`known_flags`/`soft_validate` |
| Refuse mutating a build with a live run | `engine/sidecar.py` `discover_active_sidecars` + `verify_sidecar_from_system` |
| Modal list+preview / confirm / help UI patterns | `tui/screens/config_picker.py`, `confirm.py`, `help.py` |

### 5.3 New message/enum surface (minimal)

A **separate build-lifecycle FSM** is introduced rather than overloading the serve `PhaseFSM` (whose `phase_rules`/`error_rules` are vLLM-output–specific). New messages: `BuildPhaseChanged(build_id, phase)`, `BuildJobExited(build_id, returncode)`, and reuse of `LogLineCommitted`/`ProgressUpdated`/`EngineError` for the stream. New enums: `BuildPhase {RESOLVING, DOWNLOADING, BUILDING, INSTALLING, VERIFYING, READY, FAILED}` and `BuildErrorKind {NETWORK, TORCH_CUDA_MISMATCH, DRIVER_TOO_OLD, ARCH_MISMATCH, COMPILE_OOM, COMPILE_FAILED, VLLM_IMPORT_FAILED, ADOPT_INVALID, CANCELLED}`. These live in a new `engine/builds/` package next to the registry.

-----

## 6. On-disk layout & manifest

### 6.1 Layout

```
$XDG_DATA_HOME/vllm-loader/                 # default ~/.local/share/vllm-loader  (DATA, not state)
└── builds/
    ├── active.json                         # global default-build pointer (§7.6)
    ├── builds.lock                          # registry-wide flock (create/remove/rename/active swap)
    └── <build_id>/                          # one dir per build; name == build_id (ULID, immutable)
        ├── build.json                       # manifest, 0644, identity-only (§6.2)
        ├── build.lock                       # per-build flock (mutation guard)
        ├── refs/<run_id>.ref                # refcount: sidecar path + child create_time (§7.7)
        ├── install.log                      # 0600 (may contain index/token/git creds)
        ├── venv/bin/{python,vllm,pip,activate}   # the isolated environment (uv venv / python -m venv)
        ├── bin/{vllm,python}                # stable symlinks → venv/bin/* (decouple handle from internals)
        ├── activate                         # symlink → venv/bin/activate
        └── run.sh                           # 0755 standalone launcher (absolute paths, no TUI refs)
```

**Why DATA not STATE:** runs (`~/.local/state/vllm-loader/runs`, canonical §14) are volatile and regenerable; a venv is a durable installed *artifact*, so it belongs under `~/.local/share`. **Why the `bin/` indirection:** `bin/vllm` is the stable handle the resolver and `run.sh` target, so repair/relocate can swap the target atomically without rewriting references — the same “decouple stable identity from volatile location” principle as the run manifest (canonical §7.10).

### 6.2 Build manifest (`build.json`)

Plain dataclass serialized with the sidecar’s atomic write (not Pydantic — Pydantic `extra="forbid"` stays reserved for the user-authored config layer). Identity-only, **no `api_key`/`HF_TOKEN`/`env`**. Open strings for vLLM-drifting values; closed enums only for fields we own (`status`, `method`).

```json
{
  "schema_version": 1,
  "build_id": "01J9Z8KQ4M7R2VEXAMPLE0001",
  "label": "vllm-nightly-cu130-nvfp4",
  "status": "ready",                       // creating | ready | failed | broken | adopted

  "install": {
    "method": "nightly",                   // pip | nightly | commit | git | wheel | adopted
    "installer": "uv",                     // uv | pip
    "python_requested": "3.12",
    "provenance": {
      "pip_spec": null,
      "vllm_commit": "72d9c316d3f6ede485146fe5aabd4e61dbc59069",
      "nightly_channel": "cu130",
      "index_url": "https://wheels.vllm.ai/nightly/cu130",
      "torch_backend": "auto",
      "git_url": null, "git_ref": null,
      "local_wheel_path": null,
      "adopted_external_path": null,
      "env_overrides": { "TORCH_CUDA_ARCH_LIST": "10.0", "VLLM_USE_PRECOMPILED": "1" }
    },
    "exit_code": 0
  },

  "resolved": {
    "vllm": "0.17.0.dev399+g3c7461c18",
    "vllm_commit": "3c7461c18",
    "vllm_version_profile": "current",
    "torch": "2.9.0+cu130",
    "cuda": "13.0",
    "python": "3.12.7",
    "driver_min": "≥ matching cu130",      // advisory
    "flashinfer": "0.2.x"
  },

  "gpu_arch_targets": ["sm_100", "sm_120"],
  "paths": {                                // root absolute; rest relative → relocatable
    "root": "/home/u/.local/share/vllm-loader/builds/01J9...0001",
    "venv": "venv", "executable": "bin/vllm", "python": "bin/python",
    "activate": "activate", "run_script": "run.sh"
  },

  "created_at": "2026-06-02T14:03:11Z",     // timestamps passed in by caller, never read from a clock here
  "last_used_at": "2026-06-02T18:20:05Z",

  "integrity": {
    "strategy": "pip_freeze_sha256",
    "freeze_sha256": "sha256:7d9a…",
    "executable_sha256": "sha256:11c4…",
    "verified_at": "2026-06-02T14:05:00Z",
    "verify_command": ["bin/vllm", "--version"],
    "verify_output": "vLLM 0.17.0.dev399+g3c7461c18"
  },

  "size_bytes": 9123456789,
  "notes": "Blackwell NVFP4 via flashinfer_cutlass MoE"
}
```

**`status` semantics:** `creating` (install in flight — never launchable), `ready` (verified), `failed` (install errored — kept for diagnosis), `broken` (was ready; drift/corruption on re-verify), `adopted` (externally built; launchable but badged as not-fully-managed). A crash mid-install leaves `creating` with no held `build.lock`; a startup sweep demotes stale `creating` → `failed`.

**`build_id` / `label`:** ULID id, immutable, **is the directory name** (lexicographically sortable by creation time, free chronological `list`). `label` is mutable, **unique per registry**, used in UI and as a secondary selector. **References (active pointer, per-config pins) always store `build_id`, never label**, so rename can’t break references by construction. Re-install/upgrade-in-place keeps the same id (transitions through `creating`); “keep the old env too” = a *new* id.

-----

## 7. Detailed component design

### 7.1 Installer & install-as-a-job pipeline

Creation runs a streamed background job (canonical worker discipline): allocate ULID → `mkdir` → write manifest `status=creating` (holding `build.lock`) → run the installer under a PTY → pipe through a `LogSink` to `install.log` (`0600`) and to the UI via `LogLineCommitted`/`ProgressUpdated` → resolve versions → generate independent-run artifacts (§7.4) → verify (§7.6) → atomic rewrite `status=ready` (or `failed`). Cancellation terminates the install subprocess and marks `failed (cancelled)`; a half-installed venv is marked `broken`, never silently `ready`.

**Install methods** (commands are illustrative; the exact index tags are *detected/looked up*, never hardcoded — Appendix D):

| Method | Shape (uv preferred) | When |
|---|---|---|
| `pip` (pinned) | `uv pip install vllm==X.Y.Z --torch-backend=auto` | A specific stable release. |
| `nightly` | `uv pip install -U vllm --torch-backend=auto --extra-index-url https://wheels.vllm.ai/nightly[/<variant>]` | Latest dev wheel (e.g. `cu130`). **Requires `uv`.** |
| `commit` | `uv pip install vllm --torch-backend=auto --extra-index-url https://wheels.vllm.ai/<40-char-sha>[/<variant>]` | Exact build for a cutlass/NVFP4 fix — **no compile**. **Requires `uv`.** |
| `git` (source) | clone + `uv pip install -e . --torch-backend=auto` (full compile) **or** `VLLM_USE_PRECOMPILED=1 uv pip install -e .` (python-only) | Modified kernels / unreleased / no published wheel. Needs CUDA toolkit, `TORCH_CUDA_ARCH_LIST`; minutes→tens-of-minutes. |
| `wheel` (local) | `uv pip install /path/vllm-…whl --extra-index-url <torch-index>` | A cached/shipped wheel; local-path-exists preflight first. |
| `adopt` | no install; probe an existing venv | Register an env the user already built (§7.3). |

The installer auto-selects `uv` if present, else `pip` + `python -m venv`; with `pip` only, the `nightly`/`commit` methods are disabled with the reason surfaced (pip can’t honor index priority).

### 7.2 Install-lifecycle FSM

Separate from the serve `PhaseFSM`. Phases drive the same ProgressLine/phase-banner the serve loader uses:

```
RESOLVING → DOWNLOADING → BUILDING → INSTALLING → VERIFYING → READY
                                                          ↘ FAILED
```

Line→phase hints are matched against an **installer**-pattern pack (e.g. `Collecting`/`Resolved` → RESOLVING/DOWNLOADING; `Building wheel`/`nvcc`/`ninja` → BUILDING; `Installed`/`Successfully installed` → INSTALLING; verify step → VERIFYING). Failure classification (`BuildErrorKind`) recognizes the common, surfaceable failures from Appendix D: torch/CUDA mismatch, driver-too-old (cu130 under an old driver), arch mismatch (`no kernel image` / `undefined symbol cutlass_moe_mm_sm100`), compile OOM, network, vLLM import failure.

### 7.3 Adopt-existing

Input is a path to a venv (or its `python`/`vllm`). **Verify before writing**: run `bin/vllm --version` and `bin/python -c "import vllm; print(vllm.__version__)"`; both must succeed and agree. Then synthesize a manifest with `method=adopted`, `status=adopted`, `provenance.adopted_external_path=<abspath>`. Default is **adopt-by-reference** (symlink `venv/` → external path; flagged as not-owned, so repair is limited and GC never deletes the external dir); `--copy` opt-in clones for isolation. Generated `bin/`+`run.sh` make even adopted builds TUI-independent. A periodic existence check (folded into `list`/pre-launch) flips a vanished external venv to `broken`.

### 7.4 Independent-runnability artifacts

Generated at create time, all absolute-pathed, none referencing the loader:

- **`bin/vllm`**, **`bin/python`** — stable symlinks into `venv/bin`.
- **`activate`** — symlink to `venv/bin/activate` (`source …/builds/<id>/activate` works standalone).
- **`run.sh`** (`0755`) — sets `VIRTUAL_ENV`, prepends venv `bin` to `PATH`, `exec`s `vllm "$@"`:

```bash
#!/usr/bin/env bash
# Generated by vllm-loader. Standalone — does NOT require the TUI.
# build_id: 01J9...0001  label: vllm-nightly-cu130-nvfp4
set -euo pipefail
BUILD_ROOT="/home/u/.local/share/vllm-loader/builds/01J9...0001"
export VIRTUAL_ENV="${BUILD_ROOT}/venv"
export PATH="${VIRTUAL_ENV}/bin:${PATH}"
exec "${VIRTUAL_ENV}/bin/vllm" "$@"
```

- **Standalone resolved-argv export (per config, optional action):** run a `ModelConfig` through the existing command builder against the build’s `VllmProfile` and emit the fully-resolved `vllm serve …` as a runnable script — the exact command the TUI would run, minus the TUI. Secrets (`HF_TOKEN`, `--api-key`) are **redacted to `${VAR}` placeholders** by default (`--include-secrets` to inline), matching the canonical masking posture.

### 7.5 Build → launch handoff contract

Given a selected `build_id`, the registry returns one record to the existing launch path; it spawns nothing itself:

```json
{
  "build_id": "01J9...0001",
  "executable": "/home/u/.local/share/vllm-loader/builds/01J9...0001/bin/vllm",
  "python":     "/home/u/.local/share/vllm-loader/builds/01J9...0001/bin/python",
  "env_overlay": {
    "VIRTUAL_ENV": "/home/u/.local/share/vllm-loader/builds/01J9...0001/venv",
    "PATH_PREPEND": "/home/u/.local/share/vllm-loader/builds/01J9...0001/venv/bin"
  },
  "vllm_version": "0.17.0.dev399+g3c7461c18",
  "vllm_version_profile": "current"
}
```

- **`executable`** becomes the `command.executable` the builder already understands (`_base_argv`); for `entrypoint: module` the builder uses `python` instead.
- **`env_overlay` application is the one new launch step**, applied at **both** spawn chokepoints in `process_manager` (attached env-merge and the detached supervisor payload): set `VIRTUAL_ENV`, and prepend `PATH_PREPEND` to the child’s real `PATH` (the manager owns the `:`-join so it composes with the config’s `env`). This makes subprocess vLLM and any `python -m` resolve inside the build with **no activation script** — identical effect to `run.sh`, and it fixes the detached-supervisor PATH gap (friction §7.8).
- **`vllm_version`/`version_profile`** are handed off so the loader **skips re-detecting** (the build already detected them at create time) and selects the matching `VllmProfile` directly — and these same values flow into the run **sidecar** (`executable`, `vllm_version`, `vllm_version_profile`), closing the identity loop with refcounting (§7.7).

### 7.6 Selection: global default + per-config pin

- **Global default** = `builds/active.json` (`{schema_version, build_id, label, updated_at}`), atomically written; `build_id` authoritative, label for display. Selecting a non-`ready`/`adopted` build is refused.
- **Per-config pin** = a new optional `command.build` field (§7.9) holding a `build_id` **or** label (resolver tries id, then unique-label). Pin overrides the global default for that config only.

### 7.7 Concurrency, locking, refcounting

Mirrors the sidecar’s anti-PID-reuse rigor — a refcount is trusted only after the referenced process’s identity is re-verified.

- **Two `flock` tiers:** `builds/builds.lock` for registry-wide structural ops (create/remove/rename/`active.json` swap); `builds/<id>/build.lock` for per-build mutation (install/repair/remove). Held only for the critical section; all writes stay atomic temp+rename.
- **`refs/` dir = refcount with identity:** on launch, the process manager drops `builds/<id>/refs/<run_id>.ref` containing the sidecar path + child PID + `process_create_time`. On clean teardown the ref is removed.
- **In-use is computed by *verifying*, not by counting:** read each ref → load the named sidecar → run sidecar identity verification → **alive+matching ⇒ in use ⇒ refuse mutate/delete**; **dead or PID-recycled ⇒ stale ref, GC it, don’t count it.** This reuses `verify_sidecar_from_system`-style logic, so a crashed run can’t pin a build forever and a recycled PID can’t fake “in use.”

### 7.8 Friction fixes the feature requires

These are concrete gaps in today’s code (anchors by symbol) that multi-build use exposes; the feature must address them:

1. **`@lru_cache` staleness across builds** — `detect_vllm_version(executable)` / `collect_serve_help(executable)` are cached **by executable string alone** (`engine/profile.py`). Two builds installed at the same path, or an in-place upgrade, return stale results. **Fix:** key the cache on `(executable, build_id|mtime)` or expose `clear_profile_caches(executable)` invoked on build (re)install/selection. The build manifest already stores the resolved version, so launch can bypass detection entirely (§7.5).
2. **Module-mode detection blind spot** — `select_profile_for_config` only honors `command.executable` for `entrypoint: serve` **and** when `_looks_like_vllm_executable` matches; a custom `python` (module mode) is ignored and detection falls back to bare `vllm` on `PATH`. **Fix:** when a build is resolved, detect against the build’s `bin/vllm` regardless of entrypoint (the build always has a `vllm` console script), or pass the pre-detected version through (§7.5).
3. **Detached-supervisor PATH** — the supervisor spawns the child with an inherited environment; without the venv `bin` on `PATH`, a detached run won’t find the build’s vLLM. **Fix:** the env-overlay (§7.5) must be folded into the detached **payload env**, not only the attached merge.
4. **No per-config `cwd`** — `build_command(cwd=…)` defaults to `Path.cwd()` and there’s no schema field. Multi-build + relative model paths/source-dir builds may need a specific cwd. **Fix (small):** add optional `command.cwd`/`launch.cwd`; default unchanged. (Non-blocking; recommend absolute model paths meanwhile.)
5. **Build-scoped `require_flags` re-validation** — on build selection, re-run `soft_validate`/`require_flags` for the selected config against the new build’s profile so incompatibilities surface immediately (powers the FlagManager warnings).

### 7.9 Schema changes & precedence

`CommandConfig` gains one optional field; `extra="forbid"` preserved:

```yaml
command:
  entrypoint: serve
  executable: /opt/venv/bin/vllm   # still honored, highest precedence (unchanged)
  build: "01J9...0001"             # OR a label: "vllm-nightly-cu130-nvfp4"  (new, optional)
  cwd: /srv/models                 # new, optional (friction fix #4)
```

A `model_validator` makes `executable` and `build` **mutually exclusive** (they’re competing intents). **Precedence (highest → lowest) for which vLLM launches:**

1. **`command.executable`** explicit → used verbatim, raw override, **no env overlay** (power users point anywhere). Managed builds bypassed.
2. **`command.build`** (per-config pin) → resolve handoff record (§7.5). Must be `ready`/`adopted`, else launch blocked with a named error.
3. **Global default** `builds/active.json` → that build’s handoff record.
4. **Bare `vllm` on `PATH`** → exactly today’s behavior, including the “`vllm` not on PATH → install vLLM or set `command.entrypoint: module`” error.

This makes managed builds **opt-in and never a regression**: no builds + no `active.json` ⇒ identical to the current spec; an explicit `executable` always wins.

-----

## 8. Security

- **No secrets in manifests** (identity-only, like the sidecar); `env`/`api_key`/`HF_TOKEN` never written there.
- **`install.log` is `0600`** — pip/git output can contain `--extra-index-url https://USER:TOKEN@…` or git-over-HTTPS creds; scrub through the same `LogSink` masking as run logs.
- **Exported scripts redact secrets** to `${VAR}` placeholders by default; `run.sh` carries no config/secrets at all.
- **Adopt verifies before trusting** an external path; never executes arbitrary install scripts — only the chosen installer with explicit args.
- **Network exposure** unchanged (canonical §7.9); builds don’t alter bind/auth behavior.

-----

## 9. UI / UX design

Matches canonical §8 conventions: header chrome of `Static` segments with `status--*` classes; status as **icon + word + color** (monochrome-usable); palette-first discoverability (`Ctrl+P`); `ModalScreen`s styled like `ConfigPickerScreen`/`ConfirmScreen`; `HORIZONTAL_BREAKPOINTS = [(0,"-compact"),(60,"-narrow"),(100,"-wide")]`; the **log never disappears**.

### 9.1 New keybindings & palette (collision-checked vs existing `l enter s K r c / f p w g G tab ? F1 q ^C ^P`)

| Surface | Key | Action | Palette command |
|---|---|---|---|
| Dashboard | `b` | open BuildManager | `Manage vLLM builds` |
| Dashboard | `F` | open FlagManager (build+config selected) | `Manage vLLM flags` / `Edit flags for this build…` |
| Dashboard/palette | — | select active build | `Select vLLM build: <label>` (one per build) |
| Dashboard/palette | — | create build | `Create vLLM build…` |
| Dashboard/palette | — | adopt env | `Adopt existing environment…` |
| BuildManager | `Enter`/`n`/`a`/`v`/`F`/`x`/`Esc` | select / new / adopt / verify / flags / remove / close | `Verify build`, `Remove build…` |
| FlagManager | `Enter`/`Space`/`d`/`/`/`Ctrl+S`/`Esc` | edit / toggle / reset-to-default / search / save / close | — |

(`F` ≠ existing `f` filter and `F1` help; `d`/`Space`/`Ctrl+S` are modal-scoped so they don’t shadow dashboard bindings.)

### 9.2 Header active-build segment (always visible)

```
┌ vLLM Loader ─────  ▣ vllm-nightly ●  llama-3.1-70b  ●READY  http://127.0.0.1:8000  12:42:07 ┐
                     └ active build ┘ └ active-model ┘ └status┘
```

`▣ <label-or-version> <validity-dot>` where the dot reuses status vocab: `● ready`(green) · `▲ drift`(amber) · `✕ broken`(red) · `◐ creating`(amber) · `▣ adopted`(neutral). A pinned config shows a `📌` so the operator sees the displayed build is pin-driven, not the global default. Under `-narrow` it compacts to `▣0.17●`; under `-compact` to `▣●` (full label in palette/BuildManager).

### 9.3 BuildManagerScreen (modal, two-pane list+detail)

```
┌ Build Manager ─────────────────────────────────────────────────────────────────────┐
│ Filter builds: nvfp4▌                                       5 builds · 1 active      │
│┌ Builds ───────────────────────────┐┌ Detail ─────────────────────────────────────┐│
││> ● vllm 0.17        ready  ● active││ Label     vllm-nightly-cu130-nvfp4           ││
││  ▣ vllm-nightly     ◐ creating 47% ││ Version   0.17.0.dev399+g3c7461c18 (current) ││
││  ● vllm 0.11-awq    ready  🔒 in use││ Method    nightly  cu130   commit 3c7461c18  ││
││  ✕ vllm-src@a1b2    broken         ││ Torch     2.9.0+cu130   CUDA 13.0  py 3.12.7  ││
││  ▣ vllm-adopted     adopted        ││ Arch      sm_100, sm_120     Integrity ● ok   ││
││                                    ││ Exec      …/builds/01J9…/bin/vllm             ││
││                                    │├ Resolved command (selected config) ──────────┤│
││                                    ││ cwd=…  VLLM_API_KEY=*** vllm serve … \         ││
││                                    ││   --quantization compressed-tensors \         ││
││                                    ││   --moe-backend flashinfer_cutlass …          ││
│└────────────────────────────────────┘└──────────────────────────────────────────────┘│
│ Enter Select  n New  a Adopt  v Verify  F Flags  x Remove  Esc Close                  │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

Row anatomy: `<marker> <status-icon> <label>  <status-word> [● active] [🔒 in use]`. The detail pane is the manifest summary + the **masked resolved-command preview** for the selected config on this build (reuses `build_command().preview` + `.warnings`). Navigation matches `ConfigPickerScreen` exactly (type to fuzzy-filter, ↑/↓ re-preview, `Enter` select).

### 9.4 Create-build flow + streamed install

A small `CreateBuildScreen` collects method + params (version spec / nightly channel / git url+ref / wheel path / adopt path / target python / label / profile). On `Enter` it validates, writes a `creating` manifest, **pops the modal**, and starts the background install worker; the **dashboard** then shows the stream:

```
RESOLVING → DOWNLOADING → BUILDING → INSTALLING → VERIFYING → READY/FAILED
```

Install stdout flows through the **existing** `#log` RichLog (committed lines, per-level `Text`), `\r` progress drives the **existing** ProgressLine, the phase shows in the phase banner, success → toast (`Build "…" ready (312 flags)`), failure → the **existing** ErrorBanner (cause + suggestion + “Jump to error log line”). `s` cancels the in-flight build (terminate subprocess + cancel the `build` worker group), mirroring attached-Stop and the “intentional shutdown, don’t render CRASHED” guard.

### 9.5 FlagManagerScreen (the offshoot, scoped to the selected build)

```
┌ Flag Manager — build: vllm 0.17   config: llama-3.1-70b-awq ──────────────────────────┐
│ Filter: kv▌            [✎ changed only]    modeled 13 · passthrough 4 · unknown 0       │
│┌ Flags ─────────────────────────────┐┌ Editor + live preview ───────────────────────┐│
││ MODELED (schema-backed)            ││ kv-cache-dtype  → engine.kv_cache_dtype        ││
││> ▲ kv-cache-dtype     = fp8 ✎      ││   value [ fp8        ]  build default: auto    ││
││  ● tensor-parallel-size = 4 ✎      ││   ▲ soft-validate: known for profile current   ││
││  ● quantization      = compressed… │├ Resolved command (live) ─────────────────────┤│
││ PASSTHROUGH (→ extra_args)         ││ … vllm serve … --kv-cache-dtype fp8 \          ││
││  • --moe-backend flashinfer_cutlass││   --quantization compressed-tensors \          ││
││  + add passthrough flag…           ││   --moe-backend flashinfer_cutlass            ││
││ UNKNOWN-TO-BUILD  (none)           ││ ⚠ 0 warnings                                   ││
│└────────────────────────────────────┘└──────────────────────────────────────────────┘│
│ Enter Edit  Space Toggle  d Reset-to-default  / Search  Ctrl+S Save  Esc Close         │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

- **Source of flags** = the selected build’s `known_flags` (cached `serve --help`). **Modeled** = members of `profile.flag_map` ⇒ typed `engine.*` edit; **passthrough** = anything else ⇒ `extra_args` token; **unknown-to-build** = a flag in the config not in this build’s `known_flags` ⇒ `⚠`.
- **The editor states where a value lands** (`→ engine.kv_cache_dtype` vs `→ extra_args`) so intent is never ambiguous.
- **Soft-validation reused:** bad enum (`kv_cache_dtype`/`quantization`/`load_format` outside the build’s known sets) → amber `▲` + warning under the live preview, non-blocking; a missing `require_flags` entry → hard `CONFIG_INVALID` error, no launch (identical to `VllmProfileError` today).
- **Live preview** re-renders `build_command(cfg, profile).preview` (masked env) + `.warnings` on each edit. `Ctrl+S` writes modeled edits to `engine.*` and passthrough to `extra_args`, refreshes the sidebar preview, toasts; overwriting a hand-edited YAML is confirmed.

### 9.6 Guards & responsiveness

- **In-use guard:** Remove (and any mutation) checks live runs via attached state **and** `discover_active_sidecars`/`verify_sidecar_from_system` (detached runs survive the TUI). If in use, **block** with the specific reason (`a server is running on it (PID …, http://…). Stop it first.`).
- **Destructive remove** uses `ConfirmScreen` (the canonical destructive surface) with the venv path + size and irreversibility; removing the active build forces choosing/auto-selecting a new default.
- **Responsiveness:** `-narrow` collapses the modals to single-pane (preview via `Tab`) and compacts the header build segment; `-compact` reduces the segment to `▣●`; the log never disappears.

-----

## 10. CLI surface

New `build` command group (Typer), slotting beside `run`/`list`/`preview`/`version`/`smoke`; everything the TUI does is scriptable headlessly:

```
vllm-loader build add  --method nightly --channel cu130 --label nvfp4 [--python 3.12] [--env TORCH_CUDA_ARCH_LIST=10.0]
vllm-loader build add  --method commit  --commit <sha> --channel cu130
vllm-loader build add  --method git     --url <repo> --ref <sha> [--precompiled]
vllm-loader build add  --method pip     --spec 'vllm==0.11.0'
vllm-loader build add  --method wheel   --path /path/vllm-…whl
vllm-loader build adopt --path /opt/venvs/vllm-nightly [--copy]
vllm-loader build list [--json]
vllm-loader build select <id|label>            # sets global default (active.json)
vllm-loader build inspect <id|label>
vllm-loader build verify  <id|label>
vllm-loader build remove  <id|label> [--yes]   # refuses if in use
vllm-loader build run     <id|label> -- serve <model> [flags…]   # standalone, bypasses configs
```

`build add` streams to the terminal (same scrubbed sink) and exits non-zero on failure. Config discovery and run-artifact dirs are unchanged (canonical §14); builds live under `~/.local/share/vllm-loader/builds`.

-----

## 11. Testing strategy

All without a GPU or real vLLM (fakes + a stub installer that emits canned pip/build output, incl. `\r` progress and the failure signatures from Appendix D):

- **`test_build_manifest`** — atomic write/read; identity-only (no secrets); relocatable paths; `status` transitions; stale-`creating` sweep.
- **`test_build_registry`** — create/list/select/remove/adopt; label uniqueness + rename keeps references; malformed-manifest tolerance; `active.json` atomic swap; GC refuses in-use.
- **`test_build_locks_refs`** — refcount via faked sidecars: alive+matching ⇒ in-use (mutation refused); dead/recycled ⇒ stale ref GC’d; concurrent create/remove serialized by `flock`.
- **`test_build_resolver`** — precedence matrix (explicit executable > pin > default > PATH); handoff record correctness; env-overlay applied to **both** attached and detached payloads (friction #3); `executable`+`build` mutual-exclusion validation.
- **`test_install_fsm`** — installer line→phase mapping; `BuildErrorKind` classification (torch/CUDA mismatch, driver-too-old, arch mismatch, compile-OOM, network, import-fail); cancel → `failed (cancelled)`; half-install → `broken`.
- **`test_build_integrity`** — create-time `--version`+import-agree gate; adopt verify-before-write; drift → `broken`; cheap pre-launch re-check.
- **`test_profile_cache_invalidation`** — friction #1: stale `lru_cache` cleared on (re)install/selection; module-mode detection uses the build’s `bin/vllm` (friction #2).
- **`test_flag_manager_logic`** — modeled vs passthrough partition from `flag_map`/`known_flags`; soft-validate warnings; persist to `engine.*` vs `extra_args`; live preview matches `build_command`.
- **`test_cli_build`** — `add/list/select/inspect/verify/remove/adopt/run` happy + failure paths against the stub installer.
- **TUI smoke** (`App.run_test()`/`Pilot`): open BuildManager, create via stub installer streaming through RichLog, select, in-use guard blocks remove, FlagManager edit+save. **Manual:** a real nightly cu130 NVFP4 build on Blackwell — full create→verify→select→serve→standalone `run.sh`.

-----

## 12. Implementation plan (phased; independently demoable)

Estimates assume one engineer fluent in async Python; each phase is shippable.

- **PB0 Registry & manifest (~1–1.5d):** `engine/builds/` package; manifest dataclass + atomic write (reuse sidecar helpers); registry create-stub/list/select/inspect/remove; `active.json`; locks. *Done when:* `build list/select/inspect/remove` work against hand-made fixture builds; `test_build_manifest`/`test_build_registry`/`test_build_locks_refs` green.
- **PB1 Resolver + launch integration + friction fixes (~1–1.5d):** handoff record; env-overlay applied at both spawn chokepoints; precedence + `command.build`/`command.cwd` schema; profile-cache invalidation + module-mode detection. *Done when:* a hand-made build launches a (fake) child with the venv on PATH in attached **and** detached; `test_build_resolver`/`test_profile_cache_invalidation` green.
- **PB2 Installer + install-FSM + integrity (~2–2.5d):** uv/pip installer (pip-pinned, nightly, commit, git, wheel), streamed via `LogSink`; install-FSM + `BuildErrorKind`; create-time verify; independent-run artifacts (`bin/`, `activate`, `run.sh`). *Done when:* `build add` installs a real stable release end-to-end (and the stub installer drives the FSM in tests); `run.sh` launches standalone; `test_install_fsm`/`test_build_integrity` green.
- **PB3 Adopt + verify/repair + GC (~1d):** adopt-by-reference/`--copy`, verify/repair, in-use-guarded remove/GC, stale-`creating` sweep. *Done when:* an external venv adopts and launches; broken/drift detected; `test_*` green.
- **PB4 TUI build selection (~1.5–2d):** header active-build segment; `BuildManagerScreen`; `CreateBuildScreen` + streamed install through RichLog/ProgressLine/ErrorBanner; `b` binding + palette commands; per-config pin surfaced. *Done when:* create/select/remove from the TUI with live install streaming; in-use guard blocks remove.
- **PB5 Flag-management offshoot (~1.5d):** `FlagManagerScreen` (modeled/passthrough/unknown partition, soft-validate, live preview, persist); `F` binding + palette. *Done when:* edit+save round-trips into a config; warnings match `soft_validate`.
- **PB6 CLI parity + docs (~1d):** `build run` standalone; `--json`; README + an NVFP4 worked example (Appendix C); pin tested vLLM/uv range. *Done when:* the headless workflow in §10 is documented and tested.

**MVP = PB0–PB4** (create/select/run managed builds, first-class in the TUI, independently runnable). **Full v1 = PB0–PB6** (~9–11 days).

-----

## 13. Future enhancements

ROCm/CPU/XPU build variants; a “duplicate & upgrade” flow that preserves the old build; cross-machine build export/import (tar the venv-relative artifacts + manifest); a build “doctor” that diagnoses arch/driver mismatches against detected GPUs (tie into the GPU panel’s NVML identity); shared base-layer venvs to dedupe torch across builds; auto-suggest a build for a config from its quantization/kv-cache flags (e.g. NVFP4 ⇒ a Blackwell nightly); a PTY-owning supervisor so detached builds keep live bars (canonical §16).

-----

## Appendix A — Example: per-config pin to a managed build

```yaml
name: qwen3-nvfp4-nightly
model: /models/Qwen3-32B-NVFP4
served_model_name: qwen3-32b
command:
  entrypoint: serve
  build: vllm-nightly-cu130-nvfp4     # resolves to a managed build (id or label)
engine:
  quantization: compressed-tensors    # open str; soft-validated against the build
  kv_cache_dtype: fp8_e4m3
extra_args:
  - --moe-backend
  - flashinfer_cutlass                 # passthrough: not modeled, appended verbatim
env:
  CUDA_VISIBLE_DEVICES: "0"
server: { host: 127.0.0.1, port: 8001, exposure: local }
launch: { mode: attached, ready_timeout_seconds: 1200 }
```

The same config runs standalone via the build’s exported script (§7.4) — no loader required.

## Appendix B — Decision log

- **Builds under XDG `data`, not `state`** — venvs are durable artifacts, not regenerable run scratch. *Alt:* `state` (rejected).
- **`build_id` = ULID, immutable, == dir name; `label` mutable+unique; references store id** — rename/upgrade can’t break references. *Alt:* content-hash id (rejected; breaks on in-place upgrade — the freeze hash lives in `integrity` instead).
- **Manifest = plain dataclass + atomic write (mirror sidecar), not Pydantic** — Pydantic `extra="forbid"` stays for the user-authored config layer; on-disk identity mirrors the sidecar. The **new config field** (`command.build`) *is* Pydantic.
- **`uv` preferred, detected-with-`pip`-fallback; nightly/commit disabled without `uv`** — pip can’t honor index priority and would silently install the released wheel.
- **`executable` and `build` mutually exclusive; precedence executable > pin > default > PATH** — opt-in, never a regression; explicit override always wins.
- **Separate install-FSM, not the serve `PhaseFSM`** — install output patterns differ from vLLM serve output; keep the serve FSM clean.
- **Adopt-by-reference (symlink) by default, `--copy` opt-in** — don’t silently duplicate a multi-GB env the user already built.
- **Env-overlay applied at the spawn chokepoints (not by mutating `build_command` purity)** — keeps the builder pure and fixes attached **and** detached PATH in one place.
- **Flag management scoped to the selected build, persisted into configs** — a build *is* a `serve --help`; modeled/passthrough falls out of `flag_map` vs `extra_args`.

## Appendix C — NVFP4 / cutlass worked example (why this feature exists)

NVFP4 is a Blackwell 4-bit float format vLLM runs via **CUTLASS** (and flashinfer’s `flashinfer_cutlass` MoE backend). It needs **Blackwell** (sm_100 B200/GB200, or sm_120/121 consumer) and, in practice, a **nightly cu130 build or a source build** — the exact vLLM version/commit at which a given NVFP4 path works **diverges by target sm** and drifts release-to-release (e.g. `undefined symbol cutlass_moe_mm_sm100` on consumer Blackwell with older wheels). This is precisely why builds are **detected and pinned**, not assumed: the operator creates a `commit`/`nightly`/`git` build known-good for their GPU, pins the config to it, verifies, and can reproduce it standalone. Serve flags seen in working recipes: `--quantization compressed-tensors` (often auto-detected from `config.json`), `--moe-backend flashinfer_cutlass`, `--kv-cache-dtype fp8_e4m3`; relevant build env: `TORCH_CUDA_ARCH_LIST` (e.g. `10.0`/`12.0`), `VLLM_USE_PRECOMPILED`, `VLLM_CUTLASS_SRC_DIR`.

## Appendix D — Verified install/build facts & sources

- **Install methods & index URLs:** stable `uv pip install vllm --torch-backend=auto`; nightly `--extra-index-url https://wheels.vllm.ai/nightly[/cu130]`; **per-commit wheel** `https://wheels.vllm.ai/<40-char-sha>[/variant]` (every commit since v0.5.3); GitHub-release pinned-CUDA wheel `vllm-<ver>+cu<NNN>-cp38-abi3-manylinux_2_35_<arch>.whl`; source `uv pip install -e .` (full compile) or `VLLM_USE_PRECOMPILED=1 uv pip install -e .` (python-only). *Source: vLLM GPU/CUDA install doc; `nightly_builds` doc; context7 `/vllm-project/vllm`.*
- **`uv` required for nightly/commit:** pip merges extra-index with PyPI and picks the highest version → installs the *released* wheel, not the dev one (issues #27877/#28438). *Source: `nightly_builds` doc.*
- **Default CUDA wheel tag drifts** (cu126→cu129→cu130); **detect at runtime, don’t hardcode**. *Source: `update_pytorch_version` doc.*
- **Identity/compat fields & how to read them:** `vllm --version`; `python -c "import torch;print(torch.__version__, torch.version.cuda)"`; `nvidia-smi` (driver/max-CUDA); `torch.cuda.get_device_capability()` (sm). NVFP4 needs sm_100+; below that → weight-only. cu130 wheels need a newer driver (e.g. 535 too old). *Sources: GPU install doc; issues #20173/#29030/#30633.*
- **Build env vars:** `VLLM_USE_PRECOMPILED`, `VLLM_PRECOMPILED_WHEEL_COMMIT`, `TORCH_CUDA_ARCH_LIST`, `VLLM_TARGET_DEVICE`, `MAX_JOBS`, `NVCC_THREADS`, `CMAKE_BUILD_TYPE`, `CUDA_HOME`, `VLLM_CUTLASS_SRC_DIR`; runtime `VLLM_USE_FLASHINFER_MOE_FP4`. *Sources: GPU install doc; context7.*
- **Failure modes to classify:** torch/CUDA ABI mismatch; driver-too-old for cu130; arch mismatch (`no kernel image` / `undefined symbol cutlass_moe_mm_sm100`); compile OOM (cap `MAX_JOBS`); pip-picks-released-over-nightly; per-commit wheel not yet built; CUTLASS fetch during source build. *Sources: vLLM issues #13306/#20173/#21861/#27877/#28438/#29030/#30633; PR #21309.*

> **Verify-at-build-time, never hardcode:** (1) the default CUDA wheel tag; (2) the exact vLLM version/commit at which an NVFP4/cutlass capability works **for the specific target sm**; (3) per-build driver minimums for cu130. The manifest records what was actually resolved, so the loader reasons from detected facts, not assumptions.
