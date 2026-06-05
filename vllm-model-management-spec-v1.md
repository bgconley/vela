# vLLM Model Registry & Management — Feature Specification & Implementation Plan (v1)

**Feature:** a catalog/index over model weights the loader can see, pin, pre-download, verify, and GC · **Status:** spec-ready · **Audience:** the engineer(s) extending `vela`.

> **Sibling of the build spec.** This document is the companion to `vllm-build-management-spec-v1.md` and an additive extension to `vllm-tui-loader-spec-v2-CANONICAL.md`. It **reuses the build feature's machinery wholesale** — the install-as-a-streamed-job pipeline (retargeted to HuggingFace downloads), `LogSink`-scrubbed streaming, a dedicated lifecycle FSM, two-tier `flock` locking, refcount-by-verified-sidecar identity, atomic temp-write+rename with ULID ids and caller-supplied timestamps, the precedence/handoff pattern, and the Textual UI conventions. It adds one subsystem (`models/`) plus small additive schema/UI changes.
>
> **The one structural inversion from builds — read this first.** Builds are **app-owned artifacts**: the loader creates and owns a venv under `~/.local/share`. **Models are not.** Model weights live in the **shared HuggingFace hub cache** (`$HF_HUB_CACHE`, default `~/.cache/huggingface/hub`) that standalone `vllm`, `transformers`, and every HF tool already populate and read. The loader **must not relocate weights into an app-owned tree by default.** So the model registry is a **catalog / index**, not a store: it indexes (a) the HF cache via `huggingface_hub.scan_cache_dir()`, (b) user-registered local model directories, and (c) curated pins (`repo_id @ revision`). The registry holds **only metadata** (KBs of JSON) and is **never a runtime dependency**. That inversion flips three of the build spec's choices and makes independent runnability *inherent* rather than engineered (§1.3, §10).

-----

## 0. Document status & what this adds

`ModelConfig.model` is already a free string handed straight to vLLM — a HF repo id (`meta-llama/Llama-3.1-8B-Instruct`) or an absolute local path — with `served_model_name` defaulting from `model_basename(model)`. vLLM/HF do all resolution and download today, and **that works and must keep working with zero regression** (§5 is the precise as-is). What the loader cannot do today (confirmed by code audit, §5):

- **See** what models are already cached/available — no inventory of the HF cache or local dirs.
- **Pin** a model at an exact `repo_id @ commit_sha` so a config is reproducible across cache churn and across machines.
- **Pre-download** a model as a first-class, observable, cancellable job (today the first launch blocks on an opaque HF download *inside* the vLLM subprocess; the loader only *observes* `DOWNLOADING_MODEL` from logs).
- **Reclaim disk safely** — see per-revision (dedup-aware) size and refuse to delete weights a running server or a pinning config still needs.
- **Pre-check gated/cached state** before launch (today a gated model is discovered as `HF_AUTH` mid-load).

This spec designs all five as an **additive, opt-in metadata layer**. Motivating case: *“I have 40 models churning through my HF cache; I want to see/pin/clean them, pre-pull the next one with a progress bar, pin an exact commit for reproducibility, and never have a `vllm serve` fail because I GC’d weights out from under a live server — without the loader ever owning the weights or being required to run them.”*

**Verified grounding (Appendix D).** HF cache/download facts were checked against current `huggingface_hub` + vLLM docs and context7. Two **version-sensitive** shifts are flagged and handled: the `huggingface-cli` → **`hf` CLI** rename (and `hf cache ls/rm/prune` redesign), and **`HF_HUB_ENABLE_HF_TRANSFER` being deprecated in favor of the Xet backend**. The design **prefers the `huggingface_hub` Python API** (`scan_cache_dir`, `snapshot_download`, `delete_revisions`) over shelling to the CLI precisely to dodge that churn.

-----

## 1. Vision & elemental concepts

### 1.1 What a “model entry” is

A **model entry** is **metadata** naming a set of weights the loader can launch against, plus what’s known about them (identity, cache state, size, integrity). Three **sources** fold into one catalog view:

| Source | What it is | Where weights live | Loader owns weights? |
|---|---|---|---|
| `hf_repo` (scanned) | a `repo_id @ commit` discovered by `scan_cache_dir()` | shared HF hub cache | **No** — HF owns it |
| `hf_repo` (pinned) | a `repo_id @ revision` the user curated (may be `remote_only`) | HF cache once downloaded | **No** |
| `local_path` (adopted) | a directory with `config.json` + weights + tokenizer | wherever the user put it | **No** — user owns it |
| `url` (recorded, e.g. a GGUF) | a single-file weight URL the user pins | user path, or opt-in app dir | **No** by default |

The entry’s job is to make `(repo_id, revision)` or a local path **stable, nameable, and reproducible** — not to hold bytes.

### 1.2 Why catalog, not own (the inversion from builds, stated once and hard)

- A **build** *cannot coexist* with another build in one `site-packages`, so the loader owns one venv per build — ownership is **load-bearing**.
- A **model** has no such constraint: the HF cache is *designed* for many models/revisions side-by-side with **blob-level dedup across revisions**, and it’s the cache `vllm serve <repo>` already uses. If the loader copied weights into an app tree it would (1) **double disk**, (2) **fight HF dedup**, (3) make the loader a **runtime dependency** (vLLM wouldn’t find the private copy), and (4) desync from what every other HF tool sees. So for models, **not owning is the correct default** — the inverse of builds.
- **Consequence:** the registry’s footprint is a **single small JSON index** (KBs), not GBs. Weights are external by construction.

### 1.3 Independent runnability is *inherent*, not engineered

Builds *earn* independent runnability with generated `run.sh`/`bin/` artifacts (build spec §7.4). **Models get it for free**: the weights are *already* in the standard cache/dir bare vLLM uses. Delete the loader, its index, or its process — model availability is unchanged. A human runs `vllm serve <repo> --revision <sha>` (or `vllm serve /models/foo`) and gets **bit-identical resolution**, because the loader never moved the bytes (§10).

### 1.4 Selection composes with builds

A launch is **build B × model M@rev**. The build resolver answers *which vLLM binary* (build spec §7.5); the model resolver answers *which weights + what HF env* (§9). They’re orthogonal and meet only at the existing `build_command` + spawn chokepoint — neither spawns the server, neither names the other.

-----

## 2. Scope & non-goals

**In scope (v1):** a metadata index under an XDG **state** dir (single JSON, atomic writes, ULID ids); a **merged catalog** (`scan_cache_dir()` ∪ registered local dirs ∪ curated pins, deduped by `repo_id@commit_sha`); **add/pin** `repo_id @ revision` → resolved `commit_sha`; **pre-download** as a streamed background job (reusing the build install-job infra) with allow/ignore patterns; **verify**, **adopt-local**, **inspect**, **refresh**; **remove/GC** via `delete_revisions`, refusing if in use or pinned, dedup-aware reclaim; an optional default-off app download-dir; additive config schema (`revision`, `model_ref`) + precedence; CLI parity (`vela model …`).

**Out of scope (v1):** producing/converting weights (quantizing, GGUF conversion, LoRA merge); browsing/searching the HF Hub beyond resolving a given repo; changing vLLM’s implicit download-on-launch (we *add* explicit pre-download, we don’t remove the fallback); datasets; cross-machine weight sync (the index is sync-*friendly* metadata, but the loader never moves weights); private-registry mirrors; Windows (Linux-primary, matching the build spec).

-----

## 3. Functional & non-functional requirements

**Catalog & lifecycle**
- **FR-M1** Build a merged catalog (scan ∪ local ∪ pins), dedup by `repo_id@commit_sha`; tolerate malformed/half-written index records by skipping (mirror `discover_active_sidecars`).
- **FR-M2** `add/pin` `repo_id` + `revision` (branch/tag/sha) → resolve to an immutable `commit_sha` (via `HfApi().model_info(...).sha`); entry may be `remote_only` (pinned, not downloaded).
- **FR-M3** `pre-download` as a non-blocking streamed job with live scrubbed log + phase/%, honoring `allow_patterns`/`ignore_patterns` (e.g. skip `*.bin`/`*.pth` when safetensors exist; fetch one GGUF quant).
- **FR-M4** `verify`: required files present (`config.json` + ≥1 weight + tokenizer), commit matches; optional deep blob/safetensors hash check.
- **FR-M5** `adopt-local`: verify a directory really is a loadable model **before** writing the entry.
- **FR-M6** `remove/GC` via `delete_revisions`: **refuse** if a live server uses it; **refuse** if a config pins it; report **dedup-aware** reclaimed bytes.
- **FR-M7** `refresh`: re-scan and reconcile (vanished → `missing`, downloaded pins → `cached`).
- **FR-M8** `inspect`: full detail incl. true on-disk vs nominal size, in-use, pinned-by.

**Selection & resolution**
- **FR-M9** Resolve an entry (or a bare `model` string, unchanged) into the **model handoff record** (§9): a model ref (`repo_id` + `--revision <sha>` when pinned, or an absolute local path), an HF **env contribution** (`HF_TOKEN` for gated by default; `HF_HOME`/`HF_HUB_CACHE` only when an app dir is active; `HF_HUB_OFFLINE` only on explicit request), and an optional tokenizer override.
- **FR-M10** A config may reference an entry by `model_ref` and/or pin a `revision`; enforce precedence (§12) deterministically and surface which weights a launch will use and why.

**Integrity & safety**
- **FR-M11** Pin records the resolved `commit_sha`; resolution prefers the pinned sha for reproducibility.
- **FR-M12** Cheap pre-launch re-check: pinned `cached` → confirm the snapshot still exists; `remote_only` under offline → fail early with a named error.
- **FR-M13** Refcount live runs against `model@revision` via verified sidecar identity (anti-PID-reuse).

**Gated/auth**
- **FR-M14** Detect `gated`/`token_required`; record booleans only (never the token); surface a clear “accept license / set `HF_TOKEN`” message on 401/403.

**CLI**
- **FR-M15** `model` subcommands (`list`/`add`/`download`/`verify`/`remove`/`adopt`/`inspect`/`refresh`), headless, `--json`.

**Non-functional (deltas from build NFRs):**
- **NFR-M1 Independent runnability** — weights external; loader never required to run a model (hard, §10), stronger than builds (nothing to generate).
- **NFR-M2 Don’t-fight-HF** — default cache = the standard HF cache; read it, never relocate it.
- **NFR-M3 Index is metadata-only** — no weights, no secrets; losing the index loses *names/pins*, never *models*.
- **NFR-M4** Non-blocking downloads, identity rigor, version-knowledge isolation, safety, security, **testable without GPU or network** (fakes + a stub downloader + a fake `scan_cache_dir`) — inherited from the build NFRs.

-----

## 4. Architecture

### 4.1 New component, existing seams

```
                ┌──────────────── TUI (existing) ───────────────┐
                │ Header(+ActiveModel) • ModelManagerScreen •     │
                │ DownloadModelScreen • download stream •         │
                │ (reuses RichLog, ProgressLine, ErrorBanner)     │
                └───────▲────────────────────────▲────────────────┘
                        │ messages (existing +    │ download-job stream (reuses LogSink)
                        │ ModelPhaseChanged)      │
   ┌───────────────┐  ┌─┴───────────── models/ (NEW) ────────────┴─┐  ┌──────────────┐
   │ config/schema │  │ index • entry • catalog(scan∪reg∪pin) •      │  │ engine/       │
   │ (+revision,   │─▶│ resolver • downloader(hf) • download-FSM •    │─▶│ command_builder│
   │  +model_ref)  │  │ verify • GC(delete_revisions) • refcount      │  │ + process_mgr │
   └───────────────┘  └──────────────────────────────────────────────┘  └──────┬───────┘
                            │ writes (metadata only)        hands off          │ spawn
                            ▼ ~/.local/state/vela/models/registry.json   ▼ vllm child
                        (KBs of JSON — NEVER weights)        model ref + HF env  (reads HF cache)
```

**Single integration chokepoint.** A resolved entry feeds the *existing* `build_command(...)` (model ref → `model` argv + `--revision`/tokenizer) and the *existing* `process_manager` env-merge (HF env). The model layer **never spawns** and **never holds weights**.

### 4.2 Composition with existing infra (reuse, don’t rebuild)

| Need | Reused existing piece |
|---|---|
| Stream download output, scrubbed, to log + file | `engine/log_sink.py` `LogSink` (same as build installs) |
| Surface download lines / % / errors in UI | `messages.py` `LogLineCommitted`/`ProgressUpdated`/`EngineError`; RichLog + ProgressLine + ErrorBanner |
| Atomic, identity-rigorous on-disk metadata | `engine/sidecar.py` `write_atomic`/`_write_private_text`/ULID (the build-manifest dataclass pattern) |
| Refuse mutating weights with a live run | `engine/sidecar.py` `discover_active_sidecars` + `verify_sidecar_from_system` |
| Download-as-a-streamed-job pipeline & FSM scaffolding | the build installer/install-FSM (build spec §7.1/§7.2), retargeted to `huggingface_hub` |
| Two-tier locking | the build `flock` tiers (registry-wide + per-entry) |
| Model ref / served-name basename | `config/schema.py` `ModelConfig.model`, `model_basename()` (unchanged) |
| Modal list+preview / confirm / help UI | `tui/screens/config_picker.py`, `confirm.py`, `help.py` |

### 4.3 New message/enum surface (minimal)

A **separate download-lifecycle FSM** (not the serve `PhaseFSM`, not the build install-FSM — download output differs from both). New messages: `ModelPhaseChanged(entry_id, phase)`, `ModelJobExited(entry_id, returncode)`; reuse `LogLineCommitted`/`ProgressUpdated`/`EngineError` for the stream. New enums:

```
DownloadPhase  { RESOLVING, DOWNLOADING, VERIFYING, READY, FAILED }
ModelErrorKind { NETWORK, GATED_AUTH, REVISION_NOT_FOUND, DISK_FULL,
                 INTEGRITY_MISMATCH, CACHE_CORRUPT, ADOPT_INVALID, CANCELLED }
CacheState     { cached, partial, remote_only, missing }
```

-----

## 5. How models are handled today (the as-is, code-grounded)

This is the baseline the feature extends; every claim is anchored.

**Model flow (config → argv → launch):**
- `ModelConfig.model: str` is an **unvalidated free string** (`config/schema.py`); `served_model_name` defaults via `model_basename(model)` (basename of a path, or the rightmost segment of a repo id).
- `_base_argv` (`engine/command_builder.py`) passes `cfg.model` **verbatim**: serve mode → `["vllm","serve",cfg.model]`; module mode → `[python,"-m","vllm.entrypoints.openai.api_server","--model",cfg.model]`. No `--revision`, no tokenizer flag, no preprocessing.
- The **local-path vs repo-id rule** (`is_local_model_reference`): local iff it starts with `/`,`./`,`../`,`~` or resolves under cwd; else treated as a repo id and left to vLLM/HF.

**What IS handled today:**
- **Local-path existence preflight** — `preflight.missing_local_model_path()` fails local paths that don’t exist (`ErrorKind.MODEL_NOT_FOUND`). Repo ids are not checked.
- **`served_model_name` resolution** from basename when unset (`schema.py`).
- **Download/resolve phase *observation*** — `RESOLVING_MODEL` / `DOWNLOADING_MODEL` regexes in the profile pattern pack match vLLM’s log lines; the FSM *observes*, it does not act.
- **`HF_AUTH` classification** — gated/401 lines → `ErrorKind.HF_AUTH` with the hint “Set HF_TOKEN and accept the model license if it is gated.”
- **`HF_TOKEN`/`HF_HOME` are *generic env passthrough*** — `env = {"PYTHONUNBUFFERED":"1", **cfg.env}`; only `VLLM_API_KEY` is special-cased. `HF_TOKEN` is added to the scrub `secrets` list for log redaction, but otherwise just passed through.
- `/v1/models` readback names the served model after READY (`monitoring/health.py`).

**What is NOT handled today (confirmed by absence — no hits for `scan_cache_dir`, HF cache, `snapshot`, `revision`, `gated` pre-check, disk/GC):** no model registry/catalog; no HF-cache scan or inventory; no pre-download; no disk/size tracking or GC; **no revision/commit pinning surface** (the schema has no `revision`); no model picker; no gated/cached pre-check before launch; no multi-file/quant awareness.

**Integration seams the feature hooks into (anchors):** the `cfg.model` substitution point in `_base_argv`; `preflight.check_launch_preflight` (add a model-availability/gated check); the env assembly in `build_command` (HF env contribution); the existing `RESOLVING/DOWNLOADING` phases (an explicit pre-download removes the need to discover this mid-launch); the `/v1/models` readback (confirm served id). These are exactly where §9’s handoff record plugs in — **no rewrites, only additions.**

-----

## 6. On-disk layout & the catalog merge

### 6.1 Layout

```
$XDG_STATE_HOME/vela/                # default ~/.local/state/vela  (STATE, not data)
├── runs/                                   # existing (canonical)
└── models/
    ├── registry.json                       # the ENTIRE index — metadata only, atomic write, 0600
    ├── registry.lock                        # registry-wide flock (add/remove/pin/refresh)
    └── downloads/<entry_id>.log            # 0600 download log (may carry tokened URLs); per-entry
                                            # NO weights, NO per-entry dirs of bytes
$HF_HUB_CACHE  (e.g. ~/.cache/huggingface/hub)   # weights live HERE — owned by HF, not the loader
$XDG_DATA_HOME/vela/models-cache/    # OPTIONAL app download-dir, DEFAULT OFF (§6.4)
```

**STATE not DATA — deliberately the *opposite* call from builds.** Builds are durable *artifacts* (DATA); the model index is a **regenerable pointer into external truth** — `scan_cache_dir()` rebuilds most of it any time, only curated pins/notes are user-authored — which is the textbook definition of STATE. Ownership is inverted, so the XDG category flips with it. **Single `registry.json`, not per-entry dirs:** entries own no bytes, so there is no payload to wrap a directory around; one small atomic JSON is simpler, diffable, and sync-friendly.

### 6.2 The catalog merge (how `list` is computed — it’s *derived*, not just read)

```
catalog =
   scan_cache_dir().repos[*].revisions[*]        # SOURCE A: live HF-cache truth (one row per repo@commit)
 ⊎ registry.json.entries where source==local_path # SOURCE B: adopted local dirs
 ⊎ registry.json.entries where source∈{hf_repo,url}# SOURCE C: curated pins (may be remote_only)
 dedup_key = (repo_id, commit_sha) | abspath | url
```

- A **scanned-but-unpinned** cached revision shows with `pinned=false` (you see your whole cache without registering it).
- A **pin that is also cached** merges to one row: curated fields (display_name, notes) from the index, live fields (size, files, `cache_state=cached`) from the scan.
- A **pin not yet downloaded** shows `cache_state=remote_only`.
- Sizes/`cache_state`/files for cached rows come **live from `scan_cache_dir()` every list** (cheap; no clock/hash work) — the index never stores stale sizes.

### 6.3 Default cache vs the optional app download-dir

- **Default `"hf"`** — every download/scan targets the standard HF cache (`$HF_HUB_CACHE`/`$HF_HOME`). Don’t fight HF: independent runnability and blob dedup are then automatic and the loader sets nothing it doesn’t have to.
- **Optional `app_download_dir` (default off).** If set, downloads point `HF_HOME` there *for that op* (and the launcher must get the same `HF_HOME` — §9). For `url`/GGUF entries with no HF-cache semantics, this is where the single file lands. **Kept off by default** because it’s the only way to put weights outside the canonical cache — the one case that complicates the §10 guarantee.

-----

## 7. The model-entry record (`registry.json`)

Plain dataclass serialized with the sidecar’s atomic write (Pydantic `extra="forbid"` stays reserved for the user-authored config layer; the new *config field* `model_ref` is Pydantic). Identity-only, **no `HF_TOKEN`/secrets**. Open strings for HF-/format-drifting values (`quant_format`); closed enums only for fields we own. **Timestamps are passed in, never clock-read here** (sidecar discipline).

```json
{
  "schema_version": 1,
  "default_cache": "hf",
  "app_download_dir": null,
  "entries": [
    {
      "entry_id": "01J9Z9ABCDEF2VEXAMPLEMODEL01",
      "display_name": "llama-3.1-8b-instruct",
      "source": "hf_repo",                                 // hf_repo | local_path | url
      "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
      "revision": "main",                                  // user-supplied ref (branch/tag/sha)
      "commit_sha": "0e9e39f249a16976918f6564b8830bc894c89659",  // resolved immutable pin
      "local_path": null,                                  // iff source==local_path (absolute)
      "url": null,                                         // iff source==url
      "quant_format": "none",                              // awq|gptq|fp8|nvfp4|gguf|none|other (OPEN str)
      "tokenizer": null,                                   // optional override repo/path; null = same as model
      "files": { "count": 7, "total_bytes": 16060530000, "weights_format": "safetensors" },
      "size_bytes": 16060530000,                           // NOMINAL; real reclaim is dedup-aware (§11)
      "cache_state": "cached",                             // cached | partial | remote_only | missing
      "gated": true,
      "token_required": true,                              // boolean ONLY — token NEVER stored
      "integrity": {
        "strategy": "commit_plus_safetensors_sha",         // commit_only | commit_plus_safetensors_sha
        "commit_sha": "0e9e39f249a169769...",
        "blob_hashes": { "model-00001-of-00004.safetensors": "sha256:..." },
        "verified_at": "2026-06-02T14:05:00Z"
      },
      "created_at": "2026-06-02T14:03:11Z",
      "last_used_at": "2026-06-02T18:20:05Z",
      "notes": "license accepted; pinned for repro"
    }
  ]
}
```

**`entry_id`** = ULID, immutable; **configs reference `entry_id`, never `display_name`** (rename-safe — the build spec’s “store id, never label”). **`commit_sha`** is the identity/dedup anchor (the model analogue of the build’s content-freeze hash). **`cache_state`:** `cached` (full snapshot for `commit_sha`), `partial` (some blobs missing — interrupted or pattern-filtered), `remote_only` (pinned, nothing downloaded), `missing` (was cached, vanished on refresh).

-----

## 8. Operations & semantics

All mutating ops take `registry.lock`; per-entry download/verify take a per-entry lock keyed by `entry_id` (two-tier `flock`). All writes atomic temp+rename. **The `huggingface_hub` Python API is used directly** (not the `hf` CLI) to avoid the CLI rename churn (Appendix D).

- **`list`** — compute the §6.2 merge; dedup by `(repo_id, commit_sha)`; cheap (one `scan_cache_dir()` + read the JSON), no hashing.
- **`add`/`pin`** — resolve `revision` → `commit_sha` via `HfApi().model_info(repo_id, revision=…).sha`; write an `hf_repo` entry. **Metadata-only — no bytes move.** Offline → record the ref with `commit_sha=null`, `cache_state=remote_only`, warn. 401/403 → `GATED_AUTH` with the “accept license / set `HF_TOKEN`” message.
- **`pre-download` (streamed job — reuses the build install-job infra):** take per-entry lock → `cache_state=partial` → `snapshot_download(repo_id, revision=commit_sha, cache_dir=<hf|app>, allow_patterns=…, ignore_patterns=…, token=<from env>)` → pipe through `LogSink` → `downloads/<entry_id>.log` (0600) + UI → drive the FSM `RESOLVING → DOWNLOADING(%) → VERIFYING → READY/FAILED` → on success scan to confirm, set `cache_state=cached`, fill files/size from the scan. `hf_transfer` is enabled if present for throughput; **note (version-sensitive):** the Hub is moving to the **Xet** backend, so on current `huggingface_hub` prefer `HF_XET_HIGH_PERFORMANCE=1` and treat `HF_HUB_ENABLE_HF_TRANSFER` as a legacy fallback (Appendix D). `allow/ignore` patterns are **recorded on the entry** so re-download is reproducible. **Cancel** → `partial` (resumable from blobs), never silently `cached`.
- **`verify`** — default cheap check (required files + commit present); `--deep` recomputes content-addressed blob hashes and compares (HF blobs are content-addressed, so this is strong but I/O-heavy). Mismatch → `INTEGRITY_MISMATCH`, mark `partial`/`missing`.
- **`remove`/`GC`** — the safety-critical op; three hard guards in order (§11): in-use (verified sidecar), pin-protection (any config pinning this `commit_sha`), dedup-aware reclaim reporting. Deletion is **always** via `HFCacheInfo.delete_revisions(commit_sha).execute()` — never hand-unlink blobs.
- **`adopt-local`** — verify before writing: assert `config.json` + ≥1 weight (`*.safetensors`/`*.bin`/`*.gguf`) + a tokenizer are present, then synthesize a `local_path` entry (`cache_state=cached`, `commit_sha=null`). A vanished dir flips to `missing` on `refresh`/pre-launch.
- **`inspect`** — full record + live scan detail (per-blob list, **true on-disk vs nominal** size, shared-blob count, in-use, pinned-by).
- **`refresh`** — re-scan and reconcile (pins present → `cached`, cached gone → `missing`), metadata-only.

-----

## 9. Model → launch handoff contract

Given a resolved entry (or a bare `model` string), the registry returns **one record** to the existing launch path; it spawns nothing and moves no bytes. Sibling of the build handoff (build spec §7.5).

```json
{
  "entry_id": "01J9Z9ABCDEF2VEXAMPLEMODEL01",
  "model_arg": "meta-llama/Llama-3.1-8B-Instruct",   // repo_id OR an absolute local_path
  "revision": "0e9e39f249a16976918f6564b8830bc894c89659", // commit_sha when pinned; null ⇒ no --revision
  "tokenizer_arg": null,                              // → --tokenizer <ref> only if entry.tokenizer set
  "served_model_name": "llama-3.1-8b-instruct",       // unchanged: model_basename() or config override
  "env_contribution": {
    "HF_HOME": null,                                  // set ONLY if app_download_dir is in use
    "HF_HUB_CACHE": null,                             // same — default is "don't touch, use std cache"
    "HF_TOKEN": "${HF_TOKEN}",                        // injected from env for gated; PLACEHOLDER in previews
    "HF_HUB_OFFLINE": null                            // "1" only on explicit strict-offline request
  }
}
```

**Wiring:** `model_arg` becomes the `model` positional the builder already emits — *identical to today* when unchanged; for a pinned `hf_repo` the builder **adds `--revision <commit_sha>`**; for `local_path`, `model_arg` is the absolute path and there is no `--revision`. `tokenizer_arg` → `--tokenizer` only when overridden. `env_contribution` is merged at the **same `process_manager` spawn chokepoints** the build env-overlay uses (attached env-merge **and** the detached supervisor payload — closing the same detached-env gap), and **by default contributes only `HF_TOKEN`** (for gated repos); the standard cache is inherited.

**Composition with the build handoff (launch = build B × model M@rev):**

```
launch_spec =
   build_handoff(build_id)        # WHICH vLLM:   executable + python + PATH/VIRTUAL_ENV overlay + version
 × model_handoff(entry_id|model)  # WHICH WEIGHTS: model_arg + --revision + tokenizer + HF env
 → build_command(cfg, profile)    # existing: executable … serve <model_arg> [--revision sha] …
 → process_manager.spawn(env = config.env ⊕ build.env_overlay ⊕ model.env_contribution)
```

The records are orthogonal (build never names a model; model never names a binary) and meet only at `build_command` + spawn. A config pinned to build `vllm-nightly-cu130-nvfp4` **and** model `llama-3.1-8b @ <sha>` launches the nightly against that exact revision and reproduces standalone as `…/builds/<id>/bin/vllm serve meta-llama/Llama-3.1-8B-Instruct --revision <sha>`.

-----

## 10. Independent-runnability guarantee (the headline property)

- **Weights live in the standard location** — the HF cache (`$HF_HUB_CACHE`) or the user’s own dir for `local_path` entries. The loader reads; it never relocates.
- **The registry is metadata-only.** `registry.json` holds identity, cache-state, size, integrity, notes — never weights, never secrets.
- **Deleting the TUI (or its index) never affects model availability.** `rm registry.json` and every cached model is exactly where vLLM/HF expect it. You lose *names/pins*, not *models*.
- **Standalone reproduction is exact:** pinned → `vllm serve <repo> --revision <commit_sha>`; local → `vllm serve <abs_path>`. No `run.sh`/activation/app artifact is needed — nothing was app-owned. **Contrast with builds**, whose independent runnability is *engineered* (generated `bin/`/`run.sh`) because the loader owns the venv; a model’s is *inherent* because the loader owns nothing — the strongest form of the guarantee.
- **One named sharp edge:** opting into the app download-dir (§6.3, default off) is the only case weights leave the canonical cache; even then the path is absolute/recorded and GC honors pins/in-use, but it’s opt-in precisely to keep the default guarantee free.

-----

## 11. Disk, dedup, & GC safety

The HF cache **dedups blobs across revisions** (snapshots are dirs of symlinks into a content-addressed `blobs/` store). Two consequences the GC must honor:

- **`size_bytes` is dedup-naive.** Deleting a revision frees only blobs unique to it; if it shares blobs with a retained revision, real reclaim is far less. **GC computes real reclaim from `delete_revisions(...).expected_freed_size`** and reports *that* (“Free ~2.1 GB (of 16 GB nominal; 13.9 GB shared with 2 retained revisions)”).
- **Catalog rows can’t be summed to “disk used.”** `inspect`/`refresh` show true on-disk usage from the scan with a “shared with N revisions” annotation.

**Protections (enforced at GC, §8 `remove`):**
1. **In-use** — reuse `discover_active_sidecars` + `verify_sidecar_from_system`; match the sidecar’s `model`+`revision` (the sidecar is extended to carry these, as builds extended it with build identity); **alive+matching ⇒ refuse**, naming the run; dead/recycled ⇒ stale, ignore.
2. **Pin-protection** — never delete a revision any config pins; merely *referencing* the repo (no revision pin) warns, doesn’t block.
3. **HF-API-only deletion** — never hand-unlink; `delete_revisions(...).execute()` lets HF’s own refcounting decide what’s safe to free.
4. **Partial-on-cancel** — interrupted downloads stay `partial` (resumable), never a half-state masquerading as complete.

-----

## 12. Schema change, precedence & back-compat

`ModelConfig` keeps `model: str` **exactly as today** and gains **two optional, additive** fields; `extra="forbid"` preserved.

```yaml
# UNCHANGED — zero regression; resolved by vLLM/HF as always:
model: meta-llama/Llama-3.1-8B-Instruct

# NEW (both optional):
revision: 0e9e39f249a16976918f6564b8830bc894c89659   # pin exact commit (str: sha/branch/tag)
model_ref: "01J9Z9ABCDEF2VEXAMPLEMODEL01"            # OR a registry entry (entry_id or display_name)
```

A `model_validator` makes `model_ref` and an explicit local-path `model` mutually exclusive; a `model_ref` resolving to a different repo than an explicit `model` is a `CONFIG_INVALID` error.

**Precedence (highest → lowest) for which weights launch:**
1. **Explicit `model` + `revision` string** → `serve <model> --revision <revision>`. Raw, registry bypassed (pin without registering — the model analogue of build’s “explicit `executable` wins”).
2. **`model_ref`** → resolve the entry → `model_arg` (+ `--revision <commit_sha>` if pinned) + tokenizer + HF env. Must resolve (`cached` or downloadable), else blocked with a named error.
3. **Bare `model` string** → **exactly today’s behavior** (handed to vLLM/HF, implicit resolve/download). **Zero regression.**

**Why both `revision` and `model_ref`:** `revision` is the minimal, registry-free pin (works with no registry; it’s what standalone vLLM takes) — required for the “pin without registering” path and to keep the registry non-mandatory. `model_ref` adds the named, rename-safe, tokenizer/HF-env-carrying indirection. *Alt rejected:* only `model_ref` (would make the registry a launch dependency, violating metadata-only/zero-regression); *alt rejected:* only `revision` (loses rename-safety + tokenizer override + named UX).

-----

## 13. UI / UX design

Matches canonical §8 + build-spec §9 conventions (header segments with `status--*` classes, icon+word+color status, palette-first, `ModalScreen`s like `ConfigPickerScreen`/`ConfirmScreen`, breakpoints collapse modals to single-pane, **the log never disappears**).

**New keys (collision-checked vs existing `l s K r c / f p w g G tab ? F1 q ^C ^P` and the build spec’s `b`/`F`):** **`m` → ModelManager** (free). Modal-scoped keys inside ModelManager: `Enter` select · `d` download · `p` pin revision · `v` verify · `x` remove · `r` refresh · `Esc` close. Download-cancel reuses `s` (the universal stop/cancel-job verb).

**Header model segment** (peer to the active-build segment): `M <name> <dot> <size>`, dot from the model status vocab — `● cached`(green) · `○ remote-only`(grey) · `🔒 gated`(amber) · `◐ downloading`(amber, pulse) · `▲ partial/drift`(amber) · `✕ unresolved`(red); a revision-pinned config prefixes `📌`. Compacts to `M ● <size>` (`-narrow`) then `M ●` (`-compact`).

```
┌ Vela ──  ▣ vllm-nightly ●  M 📌qwen3-32b ● 62GB  ●READY  http://127.0.0.1:8000  12:42:07 ┐
                  └ active build ┘  └─ active model ──┘   └status┘
```

**ModelManagerScreen (modal, two-pane list+detail):** left = the merged catalog; row `<marker> <status-icon> <name>  <quant> <size> [@rev] [🔒 gated] [⇩ used by N configs]` (markers: ` ` cached · `⌂` registered-local · `+` curated-remote · `>` cursor). Right = detail: repo/revision(→sha), files+sizes, gated + `HF_TOKEN` status, cached path, **which configs use it** (`📌` if pinned), last used, **dedup-aware size** (“2.1 GB unique / 16 GB nominal”). Navigation = `ConfigPickerScreen` parity (fuzzy filter, ↑/↓ re-preview, `Enter` select-for-active-config).

```
┌ Model Manager ──────────────────────────────────────────────────────────────────────┐
│ Filter models: qwen▌                          7 models · 412GB cache · 3 used by cfgs │
│┌ Models ─────────────────────────────┐┌ Detail ─────────────────────────────────────┐│
││> ● qwen3-32b  awq 62GB [⇩ 2 configs]││ repo_id    Qwen/Qwen3-32B                     ││
││  ▲ qwen3-32b  bf16 — @v0.3 [⇩ 1]    ││ revision   main → a1b2c3d (commit)           ││
││  🔒 llama-3.3-70b  ○ — gated        ││ quant awq   size 62.0 GB (2.1 unique/cached)  ││
││  ⌂ ● local-mixtral  fp8 88GB        ││ files 4×model-*.safetensors · config.json …   ││
││  + deepseek-v3  ○ — (curated)       ││ gated no   HF_TOKEN ✓   path …/snapshots/a1b2 ││
││                                     ││ used by  llama-prod 📌a1b2c3 · qwen-dev       ││
│└─────────────────────────────────────┘└──────────────────────────────────────────────┘│
│ Enter Select  d Download  p Pin rev  v Verify  x Remove  r Refresh  Esc Close          │
└───────────────────────────────────────────────────────────────────────────────────────┘
```

**Pre-download as a job:** a small `DownloadModelScreen` collector (repo + revision + optional allow-patterns chip `*.safetensors,*.json,tokenizer*` + `HF_TOKEN` status) → pop → stream through the **existing** `#log` RichLog + `#progress` ProgressLine + phase banner + `#error` ErrorBanner, FSM `RESOLVING → DOWNLOADING %→ VERIFYING → READY/FAILED`; `s` cancels (intentional-shutdown guard, partial-resumable); **401/403 → the existing HF_AUTH banner** with “set `HF_TOKEN` / accept license at hf.co/<repo>”.

**Config integration & pre-launch guards:** the config preview surfaces the model’s cached/gated/size state and offers “Download now” when not cached; the launch path **blocks before spawn** with a named banner on (a) `remote_only` + `HF_HUB_OFFLINE`, (b) gated + no `HF_TOKEN`, (c) bad/missing revision — mirroring the build in-use/validity guards. Cached+ungated passes silently (no new friction).

**Remove/GC:** in-use and pin guards block (with the specific reason) before reaching a `ConfirmScreen` that states **dedup-aware reclaimed GB** and irreversibility.

**Cross-feature dashboard:** header carries both `▣ build` and `M model` segments; the config detail shows stacked `build:`/`model:` rows above the resolved-command preview, which already fuses both (`…/bin/vllm serve <repo> --revision <sha> --flags`).

-----

## 14. CLI surface

```
vela model list [--json] [--cached-only] [--pinned-only]
vela model add     <repo_id> [--revision main] [--name <display>] [--tokenizer <ref>]
vela model download <id|repo[@rev]> [--allow '*.safetensors'] [--ignore '*.pth'] [--app-dir]
vela model verify  <id|repo[@rev]> [--deep]
vela model adopt   <path> [--name <display>] [--tokenizer <ref>]
vela model inspect <id|repo[@rev]> [--json]
vela model remove  <id|repo[@rev]> [--yes] [--force]   # refuses if in-use or pinned (force overrides pin)
vela model refresh                                     # re-scan + reconcile
```

`download` streams through the scrubbed sink and exits non-zero on failure; `remove` reports dedup-aware reclaim; default cache = HF cache, `--app-dir` opts into the configured app download-dir.

-----

## 15. Security

- **Index is identity-only, no secrets** — `gated`/`token_required` are booleans; `HF_TOKEN` is read from env at op/launch time, **redacted to `${HF_TOKEN}`** in previews/exports.
- **`downloads/<entry_id>.log` is `0600`** — download output can contain tokened index/repo URLs; scrub through the same `LogSink` masking as run logs.
- **Deletion only via `delete_revisions`** — never hand-unlink the shared blob store.
- Network exposure unchanged (canonical §7.9); models don’t alter bind/auth behavior.

-----

## 16. Testing strategy (no GPU, no network)

Fakes + a **stub downloader** (canned `huggingface_hub`-style `\r` progress + failure signatures) + a **fake `scan_cache_dir()`** (synthetic repos/revisions with shared blobs):

- **`test_model_index`** — atomic write/read; identity-only (no token); timestamps-passed-in; `cache_state` transitions; malformed-record tolerance.
- **`test_catalog_merge`** — scan ⊎ local ⊎ pins; dedup by `(repo, commit_sha)`; pinned-and-cached merges to one row; unpinned-cached visible; `remote_only` shown.
- **`test_model_resolver`** — precedence matrix (explicit `model`+`revision` > `model_ref` > bare `model`); handoff record (repo+`--revision` vs local path; tokenizer; HF env contributes only `HF_TOKEN` by default; `HF_HOME` only when app-dir on); env applied at **both** attached and detached payloads; `model_ref`-vs-explicit-path mutual-exclusion.
- **`test_download_fsm`** — line→phase; `ModelErrorKind` classification (network, gated-auth, revision-not-found, disk-full, integrity-mismatch); cancel → `partial`; allow/ignore honored + recorded.
- **`test_model_gc_safety`** — **dedup-aware** reclaim (shared blobs free less than nominal); in-use guard via faked verified sidecars (alive+matching ⇒ refuse; dead/recycled ⇒ allow); pin guard (config pinning ⇒ refuse); delete only via `delete_revisions`.
- **`test_model_verify`** — files+commit (cheap) vs `--deep` blob-hash; adopt verify-before-write; drift → `partial`/`missing`.
- **`test_cli_model`** — all subcommands happy + failure against stubs.
- **TUI smoke** (`App.run_test()`/`Pilot`): open ModelManager, pin a repo, stream a stub download, in-use guard blocks remove, refresh reconciles. **Manual:** pin a real gated repo, pre-download, GC and confirm reclaimed GB matches `scan_cache_dir`.

-----

## 17. Implementation plan (phased; independently demoable)

- **PM0 Index + catalog merge (~1–1.5d):** `models/` package; entry dataclass + atomic write (reuse sidecar helpers); `scan_cache_dir()` integration; merged `list`; `add/pin`/`refresh`/`inspect`. *Done when:* `model list/add/inspect/refresh` show cache ∪ pins; `test_model_index`/`test_catalog_merge` green.
- **PM1 Resolver + launch integration (~1d):** handoff record; `--revision`/tokenizer emission; HF env contribution at both spawn chokepoints; schema `revision`/`model_ref` + precedence; pre-launch guards (offline/gated/unresolved). *Done when:* a pinned config launches a (fake) child with `--revision`; `test_model_resolver` green.
- **PM2 Pre-download job + verify (~1.5–2d):** `snapshot_download` streamed via `LogSink`; download-FSM + `ModelErrorKind`; allow/ignore; verify (cheap + `--deep`); Xet/`hf_transfer` handling. *Done when:* `model download` fetches a small real repo (and the stub drives the FSM in tests); `test_download_fsm`/`test_model_verify` green.
- **PM3 GC + adopt + refcount (~1d):** dedup-aware `remove` via `delete_revisions` with in-use + pin guards (sidecar extended with `model`+`revision`); adopt-local. *Done when:* GC refuses in-use/pinned, reports real reclaim; `test_model_gc_safety` green.
- **PM4 TUI model management (~1.5–2d):** header model segment; `ModelManagerScreen`; `DownloadModelScreen` + streamed download; `m` binding + palette; config-preview model state + “Download now”; pre-launch guard banners. *Done when:* pin/download/select/remove from the TUI with live streaming and guards.
- **PM5 Cross-feature + CLI parity + docs (~1d):** build×model dashboard rows + fused preview; `model` CLI group `--json`; README + a gated/pinned worked example. *Done when:* the headless §14 workflow is documented and tested.

**MVP = PM0–PM4** (see/pin/pre-download/select models, first-class in the TUI, GC-safe). **Full v1 = PM0–PM5 (~7–9 days).** Sequences naturally *after* the build feature (shares the install-job infra and the sidecar `model`/`revision` extension).

-----

## 18. Future enhancements

HF-Hub search/browse to discover models in-TUI; auto-suggest a build for a model from its `quantization_config` (NVFP4 ⇒ a Blackwell nightly build, tying models×builds); dataset entries; ModelScope source (`VLLM_USE_MODELSCOPE`); cross-machine pin export/import (ship the metadata index, re-resolve on the target); a “cache doctor” (orphaned blobs, age-based prune suggestions) tied into the disk panel; per-model warm-up/health after download; LoRA-adapter entries layered on a base model.

-----

## Appendix A — Example: pinned, registry-backed model + managed build

```yaml
name: llama-prod
model: meta-llama/Llama-3.1-8B-Instruct
revision: 0e9e39f249a16976918f6564b8830bc894c89659   # exact commit pin (reproducible)
command:
  entrypoint: serve
  build: vllm-0.11-cu124            # managed build (sibling feature)
server: { host: 127.0.0.1, port: 8000, exposure: local }
launch: { mode: attached, ready_timeout_seconds: 900 }
# Standalone reproduction (loader absent):
#   …/builds/<id>/bin/vllm serve meta-llama/Llama-3.1-8B-Instruct --revision 0e9e39f2…
```

## Appendix B — Decision log

- **Catalog/index, NOT app-owned store** — weights live in the shared HF cache + user dirs; the loader indexes, never relocates. *Why:* copying would double disk, fight dedup, and make the loader a runtime dependency. *The single biggest divergence from the build spec.*
- **Default to the standard HF cache; app download-dir opt-in, default off** — keep weights canonical so independent runnability + dedup are automatic.
- **Index under XDG `state`, not `data`** — *opposite* of builds: builds are durable artifacts (DATA); the index is a regenerable pointer into external truth (STATE).
- **Single `registry.json`, not per-entry dirs** — entries own no bytes; one atomic JSON is simpler and sync-friendly.
- **Revision pinning via resolved `commit_sha`** — record both user ref and immutable sha; resolve to the sha for reproducibility (the model analogue of the build content-freeze hash).
- **Reuse the build install-job + FSM infra for downloads**, retargeted to `huggingface_hub.snapshot_download` (+ Xet/`hf_transfer`); a *separate* download-FSM (output differs from serve and build-install). Bare-`model` implicit download stays as the fallback.
- **GC = `delete_revisions` only, guarded by refcount-by-verified-sidecar + pin-protection, dedup-aware reclaim** — never hand-delete blobs; never delete a revision a live (identity-verified) server uses or a config pins; report *real* freed bytes.
- **Prefer the `huggingface_hub` Python API over the `hf` CLI** — dodges the `huggingface-cli`→`hf` rename + `scan`/`delete`→`ls`/`rm`/`prune` churn.
- **Additive, opt-in schema; bare `model` unchanged; precedence explicit `model`+`revision` > `model_ref` > bare `model`** — zero regression; registry never a runtime dependency.
- **Independent runnability is *inherent*, not engineered** — weights already sit where bare vLLM looks; `vllm serve <repo> --revision <sha>` reproduces a launch with the loader absent.

## Appendix C — How this answers “first-class like the builds feature”

| Dimension | Builds feature | Model feature (this doc) |
|---|---|---|
| Ownership | App owns the venv (DATA dir) | **Catalog over the shared HF cache** (STATE index) |
| On-disk | Per-build dirs of bytes + manifest | **One small metadata JSON**; weights external |
| Identity anchor | content-freeze hash + ULID | **`repo_id @ commit_sha`** + ULID |
| Create job | `uv/pip install` streamed | **`snapshot_download` streamed** (same pipeline, new FSM) |
| Independent run | *Engineered* (`run.sh`/`bin/`) | **Inherent** (weights already canonical) |
| Selection | header build segment, `b`, pin via `command.build` | header model segment, `m`, pin via `revision`/`model_ref` |
| Safety | refcount-by-verified-sidecar | **same** + pin-protection + **dedup-aware** reclaim |
| Launch handoff | executable + env overlay | **model_arg + `--revision` + HF env**; composes as build×model |

## Appendix D — Verified HF/model facts & sources (version-sensitive flagged)

- **Cache layout:** `HF_HOME` (default `~/.cache/huggingface`), `HF_HUB_CACHE` (`$HF_HOME/hub`); per-repo `models--<org>--<name>/{snapshots/<sha>,blobs,refs}`; snapshots are **symlinks into content-addressed `blobs/`** (dedup across revisions); `refs/<branch>` → commit sha. *Source: huggingface_hub manage-cache + env-vars docs.*
- **Python APIs:** `scan_cache_dir() -> HFCacheInfo` (`.size_on_disk` = **blob** bytes, dedup-correct; repos→revisions→files, `last_accessed`/`last_modified`, refs); `.delete_revisions(*sha).execute()` (preview `expected_freed_size`); `snapshot_download(repo_id, revision, allow_patterns, ignore_patterns, cache_dir|local_dir, token)`; `HfApi().model_info(repo_id, revision, files_metadata=True)` → `.sha`, `.gated` (`False|"auto"|"manual"`), `.siblings[].size`, `.safetensors`; `list_repo_refs()` resolves branch/tag→sha. *Source: huggingface_hub cache/hf_api/download reference + `_cache_manager.py`.*
- **CLI rename (VERSION-SENSITIVE):** `huggingface-cli` → **`hf`** (v0.34+); cache subcommands became `hf cache ls/rm/prune/verify` (were `scan-cache`/`delete-cache`). **Prefer the Python API** to avoid version-gating CLI calls. *Source: HF “hf CLI” blog + migration guide.*
- **Transfer accel (VERSION-SENSITIVE):** `HF_HUB_ENABLE_HF_TRANSFER` is **deprecated**; the Hub is Xet-backed — use `HF_XET_HIGH_PERFORMANCE=1` (and `HF_HUB_DISABLE_XET=1` to opt out) on current versions; treat `hf_transfer` as legacy fallback. *Source: huggingface_hub env-vars docs.*
- **Revision/auth:** `--revision`/`revision=` accept branch|tag|sha; pin a sha for reproducibility. Gated detected via `model_info().gated` or `GatedRepoError`/401/403; `HF_TOKEN` env overrides the on-disk token; vLLM also takes `--hf-token`. *Source: huggingface_hub errors + vLLM engine-args.*
- **vLLM model resolution:** `--model` = repo id **or** local path; `--download-dir` defaults to the HF cache (omit it to share); `--revision`, `--tokenizer`, `--load-format` (incl. `gguf`, list varies by version — flag), `VLLM_USE_MODELSCOPE`, `VLLM_MODEL_REDIRECT_PATH`. Local models need `config.json` + weights (`*.safetensors`/`*.bin`, sharded → `*.index.json`) + tokenizer files; **vLLM 0.12 tightened tokenizer-file requirements (version-sensitive)**; **GGUF is single-file + must pass `--tokenizer <base>`, “highly experimental.”** *Source: vLLM engine-args/env-vars/HF-integration/GGUF docs.*
- **Disk:** tens of GB, sharded safetensors, **blob dedup ⇒ a revision’s nominal size ≠ its reclaimable size** — always report `scan_cache_dir`/`delete_revisions` numbers, never naive sums.

> **Verify-at-runtime, don’t hardcode:** the `hf cache` subcommand names, the Xet vs `hf_transfer` accelerator, vLLM’s `--load-format` choice list, and vLLM’s per-version tokenizer-file requirements. The index records what was actually resolved, so the loader reasons from detected facts, not assumptions.
