# Vela — Deployment Composer — Feature Specification & Implementation Plan (v1)

**Feature:** a first-class "create a managed deployment" flow that turns **(target × runtime × model × profile)** into a valid, customizable, launchable Vela config — TUI-first, CLI-secondary, **agent-side**. · **Status:** spec-ready · **Audience:** the engineer(s) extending `vela`.

> **Relationship to existing specs.** Additive extension to the canonical loader, build, model, and agent/controller specs. It **composes existing machinery** — `config/schema.py`, `engine/command_builder.py` (preview), `engine/preflight.py`, the build registry (`engine/build_registry.py`), the model registry (`engine/model_registry.py`), the agent RPC surface (`agent/local.py`/`agent/stdio.py`), and the TUI screens — and adds one subsystem (`engine/composer.py` + a `ComposerService` on the agent) plus a TUI wizard and a `vela deploy` CLI group. The **Docker runtime** it can target is specified separately in `vela-docker-runtime-spec-v1.md`; this document treats "runtime" as a pluggable choice.

---

## 0. Problem & what this adds

Vela can manage builds and models and launch hand-written configs, but there is **no path from "I have a build + a model" to "I have a launchable config."** Operators hand-author YAML, hand-pick ports, hand-derive served-model-names, and hand-write Docker wrappers. That defeats the "easy, repeatable, customizable" promise.

This feature adds:
- An **agent-side composer** that drafts a complete, valid config from a few choices, auto-deriving the boilerplate.
- A **TUI "New Deployment" wizard** (the primary surface) and a **`vela deploy` CLI** (secondary, for CI).
- **Engine presets** + **per-model auto-suggestions**, fully editable before save (reusing the FlagManager).
- A **guided review → preflight → save → smoke** path.

It does **not** re-implement launching, builds, models, the command builder, or preflight — it orchestrates them.

---

## 1. Vision & elemental concepts

### 1.1 A "deployment" = a named, launchable config
A deployment is exactly today's Vela config (`ModelConfig`) — there is no new runtime object. The composer's job is to **generate** that config correctly and make it **trivially customizable**. A deployment is identified by its `name` and lives in the target's config dir (agent-side discovery, canonical §10.3).

### 1.2 Compose, don't constrain
The composer fills defaults and presets but every field stays editable before save. "Customizable prior to every deployment" is a hard requirement (User Story E2.1): the customize step exposes **all** modeled engine fields (typed) and **all** passthrough flags (raw), with live preview + soft-validation.

### 1.3 Agent-side authority (canonical §10.3, agent §4)
Configs reference target-local paths (model dirs, builds, scripts, ports, GPUs). So the composer **runs on the agent**: it port-scans the target, resolves builds/models on the target, validates against the target's vLLM profile, and writes the file on the target. The controller/TUI guides; the agent composes.

### 1.4 Runtime is a pluggable choice
A deployment's runtime is one of: **existing build** (`command.build`), **create-build** (build spec flow, then pin), **adopted venv** (`command.build` adopted), **docker** (`command.runtime: docker`, see the docker spec), or **explicit executable** (`command.executable`, power users). The composer asks the runtime, then resolves it the same way `launch` already does (precedence: executable > build > default > PATH; docker is selected by `command.runtime`).

---

## 2. Scope & non-goals

**In scope (v1):** the composer service + RPCs; auto-derivation (served-name, port, run-dir, exposure, container name); engine presets + per-model suggestions; the TUI "New Deployment" wizard; `vela deploy create/edit/clone/list/delete`; review/preflight/save/smoke; `vela config push/pull/lint` (controller↔agent config movement). **Out of scope (v1):** multi-deployment orchestration / fleets; autoscaling; a config-authoring GUI beyond the wizard + FlagManager; non-vLLM engines; editing arbitrary Docker Compose stacks (single-container deployments only — Compose is a future doc).

---

## 3. Functional & non-functional requirements

**Compose & derive**
- **FR-C1** `compose_config(spec)` returns a complete draft `ModelConfig` (as a dict) + `warnings[]` + `derived[]` (what was auto-filled and why) from `{name, target, runtime, model, build?, preset?, overrides?}`.
- **FR-C2** Auto-derive, each marked as auto + overridable: `served_model_name` (from model basename), `server.port` (next free, §6.2), `launch.runs_dir`, `server.exposure` (default `local`; `lan`/`public` only if host is non-loopback and the operator confirms), and (docker) `command.docker.container_name`.
- **FR-C3** Seed `engine.*` from a named **preset** (§6.3) and from **per-model suggestions** (§6.4); never silently override an explicit user value.
- **FR-C4** Accept arbitrary `overrides` (modeled engine fields and passthrough `extra_args`) so any vLLM parameter is customizable before save.

**Validate & preview**
- **FR-C5** `validate_config(config)` runs Pydantic validation + profile soft-validation + a **lint** pass (host-local absolute paths, missing gated token, exposure mismatch) → structured `{ok, errors[], warnings[]}`.
- **FR-C6** `preview` (reuse existing) renders the exact masked resolved command for the draft (process or `docker run`).
- **FR-C7** `preflight` (reuse `engine/preflight.py`, extended for docker — image present, port free, evict-list resolvable) returns structured pass/fail.

**Persist**
- **FR-C8** `save_config(name, config, {overwrite})` writes the config atomically (0644) into the target's config dir; refuses to clobber an existing name without `overwrite`; never launches anything.
- **FR-C9** `clone_config(src, new_name, overrides)` derives a new deployment with fresh non-colliding port/container/run-dir.
- **FR-C10** `delete_config(name)` removes a config (with confirm), refusing if a live run uses it.

**Surfaces**
- **FR-C11** TUI "New Deployment" wizard (the primary product surface) + palette command + `n` binding.
- **FR-C12** `vela deploy create/edit/clone/list/delete` (headless, `--json`, `--dry-run`, `--smoke`); idempotent on `name`.
- **FR-C13** `vela config push/pull/lint` to move a config between controller and target and flag non-portable fields.

**Non-functional**
- **NFR-C1** Agent-side: composing/validating/port-scanning/saving run on the target; controller renders.
- **NFR-C2** Customizability: every modeled + passthrough vLLM parameter editable pre-save with live preview.
- **NFR-C3** No silent collisions (ports, names, run-dirs, container names).
- **NFR-C4** Secrets masked/scrubbed in previews and any export; never written to a config except as env references.
- **NFR-C5** Testable without a GPU/Docker/network (fakes: stub catalog, stub port-scan, fake child, fake docker).
- **NFR-C6** Idempotent + reversible: re-running create updates; saving never launches.

---

## 4. Architecture

### 4.1 New component, existing seams
```
        ┌──────────────── CONTROLLER (TUI / CLI) ────────────────┐
        │ NewDeploymentScreen (wizard)  •  `vela deploy` CLI      │
        │   guides; renders draft + preview + preflight + smoke   │
        └───────────────▲──────────────────────────▲─────────────┘
                        │ RPC (compose/validate/save/derive/port) │ existing (preview/preflight/launch/list_*)
   ┌────────────────────┴──── AGENT (target host) ───────────────┴───────────────┐
   │ ComposerService (engine/composer.py)                                          │
   │   draft = derive(spec) ⊕ preset ⊕ model-suggest ⊕ overrides                   │
   │   validate(draft) → lint + pydantic + soft-validate                           │
   │   allocate_port() ← scan runs/sidecars + existing configs (+docker ps ports)  │
   │   save(draft) → atomic write into the config dir                              │
   │   reuses: build_registry • model_registry • command_builder.preview •         │
   │           preflight • profile • config/loader                                 │
   └───────────────────────────────────────────────────────────────────────────────┘
```

### 4.2 The compose pipeline (pure where possible)
`spec → derive_identity → choose_runtime_handoff → seed_engine(preset, model_suggest) → apply_overrides → assemble ModelConfig → validate → (port/name/run-dir allocation) → draft`. Everything except port/name allocation and validation-against-installed-profile is a **pure function** (testable without I/O). Allocation and profile checks touch the target and are isolated.

### 4.3 New RPC methods (agent §6.3 additions)
| Method | Params | Result | Notes |
|---|---|---|---|
| `compose_config` | `{name, target, runtime, model, build?, model_ref?, revision?, preset?, overrides?}` | `{config, warnings[], derived[]}` | Pure-ish; no write |
| `suggest_deployment_defaults` | `{target, model|model_ref, runtime}` | `{served_model_name, port, runs_dir, exposure, container_name, engine_suggestions, sources[]}` | Per-model/target suggestions |
| `allocate_port` | `{preferred?, range?}` | `{port, scanned}` | §6.2 |
| `list_presets` | `{}` | `{presets:[{name, description, engine, extra_args, applies_to}]}` | §6.3 |
| `validate_config` | `{config}` | `{ok, errors[], warnings[]}` | Pydantic + soft-validate + lint |
| `save_config` | `{name, config, overwrite?}` | `{path}` | Atomic 0644; refuses clobber |
| `clone_config` | `{src_name, new_name, overrides?}` | `{config, path?}` | Fresh port/name/run-dir |
| `delete_config` | `{name}` | `{}` | Refuse if live run uses it |
| `list_config_files`/`pull_config`/`push_config` | (controller↔agent) | … | Config movement + `lint` |
Existing reused as-is: `list_configs`, `preview`, `preflight`, `launch`, `list_builds`, `list_models`, `check_build_prerequisites`.

---

## 5. The compose spec (input contract)

```json
{
  "name": "qwen36-27b-bf16-rp6000-blackbird",
  "target": "blackbird",
  "runtime": { "kind": "docker", "image": "vllm/vllm-openai@sha256:b13d6e5…" },
  // runtime.kind ∈ { build | create_build | adopt | docker | executable }
  "model": "Qwen/Qwen3.6-27B",            // OR model_ref + revision
  "preset": "qwen3-text",                  // optional named preset
  "overrides": {                            // optional — any vLLM parameter
    "engine": { "dtype": "bfloat16", "kv_cache_dtype": "bfloat16", "max_num_seqs": 4 },
    "server": { "port": 18002, "exposure": "lan" },
    "extra_args": ["--language-model-only"]
  }
}
```
`runtime.kind` selects the handoff: `build`/`create_build`/`adopt` → `command.build`; `docker` → `command.runtime: docker` + `command.docker.*` (docker spec); `executable` → `command.executable`.

---

## 6. Auto-derivation rules (the boilerplate killer)

### 6.1 Identity
- `served_model_name` ← `model_basename(model)` (existing helper), lowercased, unless overridden or already set by a `model_ref` entry.
- `name` ← caller-supplied; validated unique on the target; suggested as `<model-slug>-<dtype>-<gpu>-<target>` when blank.
- (docker) `container_name` ← `vela-<name>` (deterministic, collision-checked vs `docker ps -a`).
- `runs_dir` ← `<runs_root>/<name>` (or model ROOT for docker, mirroring the lab convention).

### 6.2 Port allocation (`allocate_port`)
Scan, on the target: (a) ports in existing configs' `server.port`, (b) ports in live sidecars/runs, (c) `ss -ltn` listeners, (d) for docker, published ports from `docker ps`. Pick the lowest free port in a configurable range (default `18000–18999`, the lab's band). Honor a `preferred` port if free; otherwise return the next free + a `port-reassigned` warning. **Never** silently reuse an occupied port (NFR-C3).

### 6.3 Engine presets (`list_presets`)
Named, editable starting points stored as data (a `presets/` dir or a bundled table), each = `{engine:{...}, extra_args:[...], applies_to:[model-family|all]}`. v1 ships:
| Preset | Seeds |
|---|---|
| `balanced` (default) | gpu_mem_util 0.90, dtype auto, chunked-prefill + prefix-caching |
| `throughput` | higher max_num_seqs / max_num_batched_tokens, cudagraphs |
| `long-context` | large max_model_len, conservative max_num_seqs |
| `low-memory` | gpu_mem_util 0.85, smaller max_num_seqs, enforce_eager option |
| `qwen3-text` | `--language-model-only`, `--reasoning-parser qwen3`, `--tool-call-parser qwen3_coder`, prefix-caching |
Presets are **seeds, not locks** — every value remains editable (FR-C4). The chosen preset name is recorded in the config (`description`/a `vela.preset` note) for provenance.

### 6.4 Per-model suggestions (`suggest_deployment_defaults`)
From the model registry entry and/or the model's HF `config.json` (`quantization_config`, `torch_dtype`, architecture):
- `dtype`/`kv_cache_dtype` consistent with the checkpoint (FP8 checkpoint → `dtype auto`+fp8 kv hint; BF16 checkpoint → `dtype bfloat16`).
- `tensor_parallel_size` hint vs the target's visible GPU count (warn on mismatch).
- gated/token status → if gated and no `HF_TOKEN` on target, a `gated-needs-token` warning with the fix.
Suggestions are advisory; mismatches warn, never block (consistent with soft-validation).

### 6.5 Exposure
Default `local`. If the chosen runtime binds non-loopback (e.g. docker `--network host` on `0.0.0.0`, common in the lab), the composer sets `exposure: lan` **only after the operator confirms** in the review step, and always emits the canonical non-local-bind warning.

---

## 7. Customization surface (E2)

The **customize step reuses `FlagManagerScreen`** (build §9.5): modeled flags edit `engine.*` (typed), passthrough edit `extra_args` (raw), soft-validation warns on unknown enums, and the resolved command re-renders live (`preview`). New affordances: a "preset" picker at the top, a "reset to preset/default" per field, and a "show changed only" filter. This guarantees **any vLLM parameter is tunable before deploy** with no file editing.

---

## 8. TUI — "New Deployment" wizard (the primary surface)

A `NewDeploymentScreen` modal sequence (mirrors existing `ModalScreen` conventions; `n` binding + palette "New Deployment…"):
1. **Target** — pick from the targets registry (connection dot shown); defaults to the active target.
2. **Runtime** — existing build · create build (→ build flow) · adopt venv · **Docker image** (→ image pick/confirm) · explicit executable.
3. **Model** — existing pin · pin HF repo · adopt local path · bare repo id; "download now / at launch" choice; gated/cached state shown.
4. **Profile / Customize** — preset picker → FlagManager (every parameter editable) → port/exposure/context/logging.
5. **Review** — masked resolved command + target/build|image/model/port summary + warnings + `derived[]` list.
6. **Save & Smoke** — `save_config` on the target, then optional bounded smoke (reuse `smoke-tui` path); shows READY URL/model or the named failure.

Each step writes into an in-memory draft; **Back** never loses edits; **Esc** cancels without writing. The wizard calls the agent RPCs (§4.3) — it holds no authority (crown-jewel preserved).

---

## 9. CLI surface (secondary, for CI)
```
vela deploy create <name> --target <t> --model <repo|path|model_ref[@rev]>
   (--runtime docker --image <ref> | --build <id|label> | --create-build --method … | --executable <path>)
   [--preset <name>] [--port auto|<n>] [--exposure local|lan|public]
   [--set engine.kv_cache_dtype=fp8 --set server.port=18002 …]   # arbitrary overrides
   [--extra-arg --language-model-only] [--dry-run] [--json] [--smoke] [--overwrite]
vela deploy edit  <name> --target <t> [--set …] [--extra-arg …]    # load → apply → validate → save
vela deploy clone <src> <new> --target <t> [--set …]
vela deploy list  --target <t> [--json]
vela deploy delete <name> --target <t> [--yes]
vela config push <file> --target <t> | pull <name> --target <t> | lint <file>
```
`--dry-run` emits config+command without writing; default writes on the target and prints the resolved command; `--smoke` runs the bounded smoke; idempotent on `<name>` (re-create = update).

---

## 10. Composition with builds & models & docker
- **Builds:** `runtime: build|create_build|adopt` → resolves to `command.build`; if `create_build`, the wizard runs the build flow first (build spec) then pins it.
- **Models:** model choice → bare `model` or `model_ref`(+`revision`); gated/cached pre-checks feed the warnings; "download now" reuses `download_model`.
- **Docker:** `runtime: docker` → `command.runtime: docker` + `command.docker.*` per `vela-docker-runtime-spec-v1.md`; the model arg/HF env fold into the container env/`--model` by the docker runtime. **For docker, the "build" IS the image** — no managed venv is required.

---

## 11. Security
- Composing/saving touch only the target's config dir (0644 configs, no secrets inside — `HF_TOKEN`/`api_key` are env references, masked in preview, scrubbed everywhere).
- Port-scan / `docker ps` are read-only agent-side probes.
- `vela config lint` blocks accidental secret literals and flags non-portable absolutes.
- Exposure changes require explicit operator confirmation + the canonical warning.

---

## 12. Testing strategy (no GPU/Docker/network)
- **`test_composer_derive`** — served-name/run-dir/container-name/exposure derivation; preset seeding; override precedence (explicit > preset > suggestion > default).
- **`test_composer_port_alloc`** — free-port selection against faked configs/sidecars/listeners/`docker ps`; preferred-port honored/reassigned; no silent reuse.
- **`test_composer_model_suggest`** — dtype/kv/TP suggestions from a fake catalog + fake `config.json`; gated-without-token warning.
- **`test_composer_validate_lint`** — pydantic + soft-validate + lint (host-local absolutes, exposure mismatch, gated token).
- **`test_composer_save_clone`** — atomic write, clobber refusal, clone with fresh non-colliding fields, delete refused on live run.
- **`test_cli_deploy`** — create/edit/clone/list/delete happy + failure; `--dry-run`/`--json`/idempotency.
- **TUI smoke** (`run_test`) — wizard steps produce a valid draft; review shows the resolved command; save writes; (fake) smoke reaches READY.

---

## 13. Implementation phases (each shippable)
- **DC0 Composer core + derive + presets (~1.5–2d):** `engine/composer.py` pure pipeline; presets table; `compose_config`/`list_presets`/`validate_config` RPCs; unit tests. *Done when:* a draft config is produced + validated headlessly.
- **DC1 Port/name/run-dir allocation + suggestions (~1–1.5d):** `allocate_port` (target scan incl. docker ps), `suggest_deployment_defaults` (catalog + config.json). *Done when:* no-collision allocation + model suggestions tested.
- **DC2 Save/clone/delete + config push/pull/lint (~1d):** atomic writes, clone, delete-guard, controller↔agent movement, lint. *Done when:* round-trips green.
- **DC3 CLI `vela deploy` (~1d):** create/edit/clone/list/delete + `--set`/`--dry-run`/`--json`/`--smoke`/idempotency. *Done when:* the headless §9 flow works against a fake target.
- **DC4 TUI New Deployment wizard (~2–3d):** the 6-step modal, FlagManager reuse, review/preflight/save/smoke. *Done when:* an operator composes + smokes a deployment from the TUI.
- **DC5 Docs + polish (~0.5d):** README/docs, palette/help, the "Clone deployment" affordance.

**MVP = DC0–DC4** (depends on the Docker runtime spec for the `docker` runtime option; the `build`/`executable` runtimes work without it). **Sequence:** land `vela-docker-runtime-spec-v1.md` (DK phases) alongside DC0–DC2 so DC4's runtime step can offer Docker.

> No code written in this document. Hand off with the user stories and the implementation plan.
