# Vela — Deployment Composer + Docker Runtime — Implementation Plan (v1)

**Covers:** `vela-deployment-composer-spec-v1.md` (DC phases) and `vela-docker-runtime-spec-v1.md` (DK phases), sequenced together. **Audience:** the agentic coder implementing this. **Companion:** `vela-deployment-composer-user-stories-v1.md`.

> **House rules (carry over from the existing codebase):** TDD (red test → green); reuse existing seams, don't rebuild; crown-jewel preserved (controller runs no `docker`/process/registry authority); scrub-before-wire; verify-before-every-destructive-action; agent-side config/port/preflight; Pydantic `extra="forbid"` for config; new RPC error codes added to `transport/rpc_errors.py`; `ruff` clean; every phase ships green with its own tests; record a validation artifact for the real-hardware phase.

---

## 0. The two features, and how they relate
- **Docker Runtime (DK)** — a new launch backend so Vela natively owns vLLM containers (replaces wrapper scripts). *Independently valuable* — even without the composer, it lets the lab convert the Blackbird wrappers to native `runtime: docker` configs.
- **Deployment Composer (DC)** — generates valid, customizable configs from `(target × runtime × model × profile)`. Its **Docker runtime option depends on DK**; its `build`/`executable` options do not.

**Recommended sequence:** interleave — land **DK0–DK1** alongside **DC0–DC2**, so by the time the TUI wizard (DC4) needs a "Docker" runtime, the backend exists. Net order:
```
DK0 → DC0 → DK1 → DC1 → DC2 → DK2 → DC3 → DK3 → DC4 → DK4 → DC5
```
(DK4 = real-hardware validation, runs after DC4 so the wizard-generated docker config is what's validated.)

---

## 1. Pre-work: shared foundations (~0.5d)
Land first; everything else builds on these.
- **S1. Runtime discriminator in the schema.** Add `command.runtime: Literal["process","docker"] = "process"` and a `command.docker: DockerConfig | None` to `config/schema.py`; `model_validator` enforces `docker` XOR `executable` XOR `build`, and `docker` requires `image`. *Test:* `test_config_loader` — valid/invalid combinations.
- **S2. New error codes.** Add to `transport/rpc_errors.py`: `image-not-found -32018`, `name-conflict -32019`, `daemon-unreachable -32020`, `compose-invalid -32021`, `config-exists -32022` (numbers indicative; keep contiguous). *Test:* `test_rpc_framing` round-trips.
- **S3. Fake `docker` test harness.** A `tests/fakes/fake_docker.py` (and a `fake_docker` PATH shim) that answers `image inspect|run|logs|wait|inspect|stop|kill|ps` with canned/scriptable output; plus 2–3 recorded vLLM container-log fixtures under `tests/fixtures/docker_logs/`. *Test:* the harness itself (sanity).

---

## 2. Docker Runtime phases (DK)

### DK0 — Schema + `docker run` generation (~1.5d)
**Build:** `engine/docker_runtime.py` with a **pure** `build_docker_run(cfg, serve_args, defaults) -> list[str]`; default injection (FR-D3: `--gpus`, `--ipc=host`/`--shm-size`-by-TP, HF-cache volume, `HF_TOKEN`, `--name`, `--restart no`, network/port); in-image HF cache path detection/override; mutual-exclusion already from S1. Wire `preview` (`engine/command_builder` path) to render the masked `docker run` when `runtime == docker`.
**Tests (`test_docker_run_generation`):** defaults injected only when unset; `--shm-size` 16g/32g by `tensor_parallel_size`; digest used verbatim; secrets masked in preview; **BF16 does not inherit an FP8 KV pin** (the footgun); `extra_run_args` appended; `docker`+`executable` rejected.
**Done when:** `vela preview <docker-config>` prints a correct masked `docker run`; unit tests green.

### DK1 — DockerRuntime backend + lifecycle (~2–3d)
**Build:** in `agent/local.py`, route `launch` to a `DockerRuntime` when `runtime == docker`: resolve+record image digest (`docker image inspect`), pull per policy, `docker run -d --name`, capture id, write the **docker sidecar** (§7), background `docker wait`. Extend `engine/sidecar.py` with the runtime discriminator + container identity + a `verify_container_identity` (exists ∧ name ∧ digest). Implement stop (`docker stop -t`), kill (`docker kill`), restart — each **re-verifying identity first**, raising `TrackedProcessMismatch → identity-verification-failed (-32002)` on mismatch. `DockerErrorKind` classification.
**Tests (`test_docker_lifecycle`, `test_docker_errors`):** sidecar records name/id/digest; stop/kill/restart issue the right docker commands; recycled name with different id/digest aborts (`-32002`, no action); image-not-found/name-conflict/daemon-unreachable/port-in-use named errors.
**Done when:** full lifecycle works against fake docker; crown-jewel grep still clean.

### DK2 — Logs / health / phase / discover / reattach (~1.5d)
**Build:** stream `docker logs -f` through the scrubbing `LogSink` → durable 0600 log + scrubbed events (FR-D9); health probe + `reachable_url` from the target host (reuse `monitoring/health`); phase FSM over container log lines (reuse profile packs); `discover_runs`/`reattach` handle docker sidecars (verify id+digest, resume by log offset).
**Tests:** `test_docker_logs_scrub`, `test_docker_health_phase`, `test_docker_discover_reattach`.
**Done when:** a fake docker run walks STARTING→READY, logs are scrubbed, reattach verifies identity.

### DK3 — Eviction + port guard + standalone export + docs (~1d)
**Build:** sibling-eviction (`command.docker.evict` → `docker stop` before launch); port preflight (`ss`/`docker ps`) → `PORT_IN_USE` before run; `vela ... export` writes a self-contained `docker run` script (secrets redacted, FR-D15); `vela deploy from-wrapper` best-effort migration helper; docs (`docs/docker-runtime.md`, README, `agent-rpc.md` notes).
**Tests:** `test_docker_eviction_portguard`, `test_docker_export`.
**Done when:** eviction/guard tested; export reproduces the run; docs land (and `test_docs` gates them).

### DK4 — Real-hardware validation (~1d, after DC4)
**Build:** none. Convert the Blackbird Qwen3.6 27B **FP8 and BF16** lanes to native `runtime: docker` configs (generated by the composer), launch on Blackbird via the agent, reach READY on `18003`/`18002`, Stop = `docker stop`. Record a dated artifact under `artifacts/remote-validation/`.
**Done when:** the native-docker Blackbird lane reaches READY + clean stop; artifact committed; the hand-written wrappers can be retired (kept for reference).

---

## 3. Deployment Composer phases (DC)

### DC0 — Composer core + derive + presets + validate (~1.5–2d)
**Build:** `engine/composer.py` — the pure pipeline `spec → derive_identity → runtime_handoff → seed_engine(preset, suggest) → apply_overrides → assemble → validate`; a presets table (`engine/presets.py` or `presets/*.yaml`); RPCs `compose_config`, `list_presets`, `validate_config` in `agent/local.py` + `agent/stdio.py` dispatch. Override precedence: **explicit > preset > model-suggestion > default**.
**Tests (`test_composer_derive`, `test_composer_validate_lint`):** served-name/run-dir/exposure derivation; preset seeding; override precedence; pydantic + soft-validate + lint (host-local absolutes, exposure mismatch, gated token).
**Done when:** a valid draft config is composed + validated headlessly against a fake target.

### DC1 — Port/name/run-dir allocation + model suggestions (~1–1.5d)
**Build:** `allocate_port` (scan existing configs' ports + live sidecars + `ss` + `docker ps`; default band 18000–18999; honor `preferred`, else reassign + warn); `suggest_deployment_defaults` (model registry entry + HF `config.json` `quantization_config`/`torch_dtype` → dtype/kv/TP suggestions; gated-without-token warning; container-name derivation + `docker ps -a` collision check).
**Tests (`test_composer_port_alloc`, `test_composer_model_suggest`):** no silent port reuse; preferred honored/reassigned; dtype/kv/TP suggestions from a fake catalog/`config.json`; gated warning.
**Done when:** allocation never collides; suggestions are correct + advisory.

### DC2 — Save / clone / delete + config push/pull/lint (~1d)
**Build:** `save_config` (atomic 0644 into the agent config dir; refuse clobber w/o `overwrite`; never launches); `clone_config` (fresh non-colliding port/name/run-dir); `delete_config` (refuse if a live run uses it); `list_config_files`/`pull_config`/`push_config`; `lint` (flag non-portable absolutes + secret literals).
**Tests (`test_composer_save_clone`):** atomic write; clobber refusal; clone deltas + fresh fields; delete-guard on live run; push/pull round-trip; lint findings.
**Done when:** config CRUD + movement round-trips green.

### DC3 — `vela deploy` CLI (~1d)
**Build:** `vela deploy create/edit/clone/list/delete` + `vela config push/pull/lint` in `cli.py` (Typer group): `--runtime docker|build|create-build|adopt|executable`, `--image`, `--build`, `--model`, `--preset`, `--port auto|<n>`, `--exposure`, repeatable `--set key=value` (arbitrary overrides) + `--extra-arg`, `--dry-run`, `--json`, `--smoke`, `--overwrite`; idempotent on `name`.
**Tests (`test_cli_deploy`):** create/edit/clone/list/delete happy + failure; `--dry-run` emits without writing; `--json`; idempotency; `--smoke` drives the bounded smoke against the fake child / fake docker.
**Done when:** the headless §9 (composer spec) flow works end-to-end against a fake target.

### DC4 — TUI "New Deployment" wizard (~2–3d)
**Build:** `tui/screens/new_deployment.py` — a 6-step modal (Target → Runtime → Model → Profile/Customize → Review → Save&Smoke); reuse `FlagManagerScreen` for the customize step (every parameter editable + live preview), `ConfigPickerScreen` patterns for pickers, `ConfirmScreen` for exposure/destructive confirms; `n` binding + palette "New Deployment…"; calls the composer/preview/preflight/save/smoke RPCs (no controller authority). Wire the Runtime step's "Docker" option to DK0/DK1.
**Tests (`test_tui_smoke`):** wizard steps build a valid draft; review renders the resolved (process or docker) command; save writes on the (fake) target; a (fake) smoke reaches READY; Back preserves edits; Esc cancels without writing.
**Done when:** an operator composes + customizes + previews + saves + smokes a deployment entirely from `vela`.

### DC5 — Docs + polish (~0.5d)
README "New Deployment" section, `docs/deployments.md`, palette/help entries, the "Clone deployment" affordance from an existing config's detail pane, and a `vela deploy` reference. `test_docs` gates the new sections.

---

## 4. Dependency graph & parallelization
```
S1,S2,S3  ─┬─► DK0 ─► DK1 ─► DK2 ─► DK3 ───────────────┐
           │                                            ├─► DK4 (real hw, after DC4)
           └─► DC0 ─► DC1 ─► DC2 ─► DC3 ─► DC4 ─────────┘
                                   (DC4 Docker option needs DK0–DK1)
```
- **Two coders can parallelize:** one on DK (runtime), one on DC (composer), syncing at S1 (shared schema) and DC4 (wizard needs DK).
- **Single coder:** follow the interleaved order in §0.

---

## 5. Reuse map (do not rebuild)
| Need | Reuse |
|---|---|
| Resolved serve args (model + flags) | `engine/command_builder.py` (`build_command`/`preview`) |
| Config schema + validation | `config/schema.py` (+ new `command.runtime`/`command.docker`, `extra="forbid"`) |
| Atomic config writes | the `_write_private_text_atomic`/`update_config_flags` write path |
| Preflight (port/world-size/model-path) | `engine/preflight.py` (+ docker image/port checks) |
| Log scrubbing + durable 0600 | `engine/log_sink.py` + `engine/redaction.py` |
| Health / reachable-url | `monitoring/health.py` + the controller URL rewrite |
| Phase FSM + error packs | `engine/phases.py` + `engine/profile.py` |
| Identity discipline | `engine/sidecar.py` (+ docker container identity) |
| Build/model resolution | `engine/build_registry.py` / `engine/model_registry.py` |
| Modeled/passthrough flag editing | `tui/screens/flag_manager.py` |
| Modal/list/confirm UI patterns | `tui/screens/{config_picker,confirm,help}.py` |
| RPC framing + auth + dispatch | `agent/stdio.py` + `agent/local.py` `handle()` + capabilities |

---

## 6. Risks & mitigations
- **Docker daemon variance (rootless, non-root images, cgroup v2, HF cache path).** *Mitigate:* detect the in-image HF cache path + user; make every docker knob overridable (`command.docker.*`, `extra_run_args`); `vela doctor`-style preflight reports docker/GPU availability (ties to the onboarding spec).
- **Container identity vs PID rigor.** *Mitigate:* track container **id + image digest** (not just name); verify-before-act; abort on mismatch (`-32002`).
- **Composer drift from the real profile.** *Mitigate:* validate against the target's installed/managed vLLM profile (soft) and run `preflight` before save/launch; never block on advisory suggestions.
- **Port/name/run-dir collisions.** *Mitigate:* agent-side scan across configs + sidecars + listeners + `docker ps`; never silently reuse.
- **Scope creep into Compose/K8s.** *Mitigate:* v1 is single-container, single-deployment; Compose/fleets are explicitly future.
- **Secret leakage via docker args/exports.** *Mitigate:* `-e` from agent env only; mask in preview; scrub in logs; redact in exports; never in config/sidecar.

---

## 7. Definition of done (combined v1)
1. **Docker runtime:** a vLLM container deploys, monitors, and stops natively via `runtime: docker` (no wrapper); identity-verified stop/kill/restart; logs scrubbed; health/READY; discover/reattach; validated on real Blackbird hardware (FP8 + BF16) with a committed artifact.
2. **Composer:** `vela` → **New Deployment** produces a valid, customizable, convention-matching config (any vLLM parameter editable), with auto port/served-name/run-dir/exposure/container-name, review → preflight → save → smoke; `vela deploy create` mirrors it headlessly.
3. **Quality:** all new phases ship green with their own tests; `ruff` clean; crown-jewel grep clean; docs (incl. `docs/docker-runtime.md`, `docs/deployments.md`) land and are gated by `test_docs`.

**Net outcome:** the gap between "I have a build/model/image" and "I have a launchable, customizable, repeatable Vela deployment" is closed — TUI-first, Docker-native, CI-scriptable.

> No code written in this document — implementation-ready hand-off only.
