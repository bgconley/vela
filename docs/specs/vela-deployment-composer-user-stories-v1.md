# Vela — Deployment Composer & Docker Runtime — User Stories (v1)

**What this covers:** the product gap between *"I have managed builds and models"* and *"I have a launchable, customizable Vela deployment"* — plus making **Docker a first-class runtime** so Vela natively follows the lab's docker-wrapper convention instead of hand-written scripts.
**Companion specs:** `vela-deployment-composer-spec-v1.md`, `vela-docker-runtime-spec-v1.md`. **Plan:** `vela-deployment-composer-implementation-plan-v1.md`.
**Format:** each story has a persona, the As/I-want/So-that, **acceptance criteria** (Given/When/Then), and a priority (P0 must-have for MVP → P2 polish).

---

## Personas
- **Researcher (Riya)** — wants a brand-new model up in minutes without learning YAML or `docker run`.
- **Lab admin (Avi)** — wants repeatable, version-controlled, customizable deployments across P620/Blackbird, following the established docker-wrapper convention.
- **Operator/debugger (Dev)** — wants to tweak any vLLM parameter before a launch, preview the exact command, preflight it, and smoke it — all from the TUI.
- **CI/automation (the pipeline)** — wants the same composer behind a headless CLI for scripted, reproducible deployment creation.

---

## Epic E1 — Create a new deployment from nothing (TUI-first)

**E1.1 (Riya, P0)** — *As a researcher, I want a "New Deployment" flow in the TUI, so that I can turn a model + target into a launchable config without writing YAML.*
- **Given** I press `n` (or pick "New Deployment" from the palette) on a connected target,
- **When** I step through target → runtime → model → profile → review,
- **Then** Vela writes a valid config on the target, shows me the exact resolved launch command, and offers "Launch smoke now."

**E1.2 (Riya, P0)** — *As a researcher, I want sensible defaults filled in automatically, so that I only choose what matters.*
- **Given** I picked model `Qwen/Qwen3.6-27B` on `blackbird`,
- **When** the composer drafts the config,
- **Then** `served_model_name`, a free `port`, `runs_dir`, `exposure`, container name, and a starting engine preset are pre-filled, each clearly marked as "auto" and editable.

**E1.3 (Avi, P1)** — *As a lab admin, I want the generated config to match our existing conventions, so that new deployments look like the ones we hand-wrote.*
- **Given** the lab uses digest-pinned `vllm/vllm-openai` docker deployments,
- **When** I create a docker-runtime deployment,
- **Then** the config and resolved command match the shape of `qwen36-27b-*-blackbird` (image, ipc=host, shm, cache mounts, foreground supervision) with no hand-written wrapper required.

**E1.4 (Avi, P1)** — *As a lab admin, I want to clone/derive a deployment from an existing one, so that a new variant (e.g. BF16 from FP8) is a 30-second edit.*
- **Given** an existing `qwen36-27b-fp8-...` config,
- **When** I choose "Clone deployment" and change model/dtype/port,
- **Then** Vela produces a new named config with the deltas applied and a fresh non-colliding port/container/run-dir.

---

## Epic E2 — Customize any vLLM parameter before deploy

**E2.1 (Dev, P0)** — *As an operator, I want to change any vLLM parameter before launch, so that every deployment is tunable without editing files.*
- **Given** a drafted deployment,
- **When** I open the customize step,
- **Then** I can edit modeled engine flags (typed) and add/remove passthrough flags (raw), with a live resolved-command preview and soft-validation warnings.

**E2.2 (Dev, P0)** — *As an operator, I want presets I can start from and then tweak, so that I'm not configuring 20 flags by hand each time.*
- **Given** the profile step,
- **When** I pick a preset (e.g. `throughput`, `long-context`, `low-memory`, `qwen3-coder`),
- **Then** the engine flags seed from the preset and remain fully editable; the preset name is recorded on the config.

**E2.3 (Dev, P1)** — *As an operator, I want the composer to suggest flags from the model's own metadata, so that obvious settings (dtype, kv, tokenizer) are right by default.*
- **Given** a model with a known `quantization_config`/dtype (from the model registry or HF `config.json`),
- **When** the composer drafts engine flags,
- **Then** it suggests `dtype`/`kv_cache_dtype`/`tensor_parallel_size` consistent with that metadata and flags any mismatch as a warning (not a block).

**E2.4 (Dev, P2)** — *As an operator, I want to see what each parameter does and its current-vs-default value, so that I tune with confidence.*
- **Given** the customize step, **When** I focus a flag, **Then** I see its description, the build's default, and whether my value differs.

---

## Epic E3 — Docker as a first-class runtime

**E3.1 (Avi, P0)** — *As a lab admin, I want to choose "Docker" as a runtime and have Vela own the container lifecycle, so that I don't maintain wrapper scripts.*
- **Given** the runtime step,
- **When** I pick "Docker image" and supply/confirm an image (digest-pinned),
- **Then** Vela generates the `docker run` itself, launches/monitors the container, streams its logs, and Stop performs a graceful `docker stop`.

**E3.2 (Avi, P0)** — *As a lab admin, I want the docker runtime to apply vLLM's required flags automatically, so that I don't forget `--ipc=host`/`--shm-size`/cache mounts.*
- **Given** a docker deployment,
- **When** Vela builds the run command,
- **Then** it includes `--gpus`, `--ipc=host` (or `--shm-size` sized by TP), the HF-cache volume, `HF_TOKEN` for gated models, and the published/host port — overridable but correct by default.

**E3.3 (Avi, P1)** — *As a lab admin, I want one large model to evict conflicting containers before launch, so that the single-GPU box doesn't double-book.*
- **Given** a target that fits one big model at a time,
- **When** I launch a docker deployment,
- **Then** Vela stops the configured sibling containers first and refuses if the port is already bound, with a named error.

**E3.4 (Dev, P1)** — *As an operator, I want Stop/Kill/Restart to map correctly to container actions, so that lifecycle is identical to process runtimes.*
- **Given** a running docker deployment, **When** I Stop, **Then** Vela `docker stop -t <grace>`; **Kill** → `docker kill`; **Restart** → stop + run; identity is re-verified (container id/name) before each destructive action.

**E3.5 (Avi, P2)** — *As a lab admin, I want to export a docker deployment as a standalone wrapper script, so that it runs without Vela if needed (parity with `run.sh`).*
- **Given** a docker deployment, **When** I "Export standalone", **Then** Vela writes a self-contained `docker run` script (secrets redacted) reproducing the launch.

---

## Epic E4 — Review → preflight → smoke (the guided path)

**E4.1 (Riya, P0)** — *As a researcher, I want to see the exact command and warnings before anything runs, so that there are no surprises.*
- **Given** the review step, **When** I view it, **Then** I see the masked resolved command (process or `docker run`), the target/build/model/port summary, exposure warnings, and any soft-validation notes.

**E4.2 (Dev, P0)** — *As an operator, I want a preflight before save/launch, so that obvious failures are caught early.*
- **Given** review, **When** I run preflight, **Then** Vela checks (agent-side) port availability, model/weights presence or gated-token, build/image availability, world-size-vs-GPUs, and disk — returning structured pass/fail with fixes.

**E4.3 (Riya, P0)** — *As a researcher, I want to save and optionally smoke in one step, so that I immediately know it works.*
- **Given** a passing preflight, **When** I choose "Save & Smoke", **Then** Vela writes the config on the target and runs a bounded smoke (load → READY → stop), reporting the READY URL/model or the named failure.

**E4.4 (Avi, P1)** — *As a lab admin, I want the saved config to be ready to commit to git, so that deployments are version-controlled.*
- **Given** a saved config, **When** I view it, **Then** it's a clean, portable YAML in the target's config dir (and surfaced for `vela config pull` to the controller for committing).

---

## Epic E5 — CLI parity (CI/scripting, secondary)

**E5.1 (pipeline, P1)** — *As automation, I want `vela deploy create` to do everything the wizard does headlessly, so that deployments are scriptable.*
- **Given** `vela deploy create qwen36-bf16 --target blackbird --model Qwen/Qwen3.6-27B --runtime docker --preset qwen3 --port auto --json`,
- **When** it runs, **Then** it composes, validates, writes the config on the target, prints the resolved command, and (with `--smoke`) runs the smoke — exiting non-zero on any failure.

**E5.2 (pipeline, P2)** — *As automation, I want `--dry-run` to emit the config + command without writing, so that CI can diff/approve before applying.*

**E5.3 (pipeline, P2)** — *As automation, I want `vela deploy create` to be idempotent on the deployment name, so that re-running updates rather than duplicating.*

---

## Epic E6 — Safety, correctness, and non-surprise

**E6.1 (all, P0)** — *No silent collisions.* Auto-allocated port, container name, and run-dir must not collide with existing configs/runs/containers on the target; collisions are detected and resolved or surfaced.
**E6.2 (all, P0)** — *Agent-side authority.* Composing, validating, port-scanning, and saving happen on the target agent (configs are target-local); the controller only guides and renders.
**E6.3 (all, P0)** — *Secrets never leak.* `HF_TOKEN`/`api_key` are referenced by env, masked in previews, scrubbed from logs and any exported script.
**E6.4 (all, P1)** — *Portable by default.* Generated configs prefer `model_ref`/image-digest over absolute host paths where possible; `vela config lint` flags host-local absolutes.
**E6.5 (Dev, P1)** — *Reversible.* Saving a config never starts anything; launch is always a separate, explicit step.

---

## Priority summary (MVP cut)
- **P0 (MVP):** E1.1, E1.2, E2.1, E2.2, E3.1, E3.2, E4.1, E4.2, E4.3, E6.1–E6.3.
- **P1:** E1.3, E1.4, E2.3, E3.3, E3.4, E4.4, E5.1, E6.4, E6.5.
- **P2:** E2.4, E3.5, E5.2, E5.3.

**Definition of done (product):** an operator opens `vela`, chooses **New Deployment**, picks a model + target + Docker (or build) runtime, tweaks any flag, sees the exact command, preflights, and smokes — producing a clean, committable, convention-matching config — with the same flow available headlessly via `vela deploy create`.
