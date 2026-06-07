# Vela — v1 Completion Punchlist & Coder Handoff

**Created:** 2026-06-06 · **Baseline HEAD:** `40858d4` (v1-hardening landed in `443a9e0`) · **Author:** review synthesis (rounds 6–8)
**Baseline health:** 845 tests pass deterministically (`PYTHONPATH=src python -m pytest -q -p no:randomly`); `ruff check .` clean; crown-jewel grep clean; working tree clean.

This document is a **complete, self-contained handoff** to take three tracks to 100%:

| Track | Current | Target | Scope |
|---|---|---|---|
| **A — Core deploy/runtime/model engine (v1 MVP)** | ~88% | 100% | docker runtime, composer, model-mgmt, secrets, backend-evidence gate, safety |
| **B — Onboarding self-guiding UX** | ~36% | 100% | `vela-onboarding-ux-spec-v1.md` (bootstrap, doctor, R1–R6) |
| **C — TUI primary-surface breadth** | ~52% | 100% | `vela-deployment-composer-spec-v1.md` §7–§8 wizard |

Every item is anchored to its governing spec line and to the current code (`file:line`). Items are ordered by priority within each track. Severity tags: **[HIGH]** = correctness/security or a named spec MUST; **[MED]** = real functional/spec gap; **[LOW]** = polish/coverage to reach a defensible 100%.

---

## 0. Conventions, ground rules & global Definition of Done

**Read before starting any item:**
- `.wolf/cerebrum.md` Do-Not-Repeat list (before generating code), `.wolf/anatomy.md` (before reading files), `.wolf/buglog.json` (before fixing anything).
- After actions: update `.wolf/anatomy.md` for new/renamed files, append to `.wolf/memory.md`, and log any bug/fix to `.wolf/buglog.json`.

**Non-negotiable invariants (do NOT regress — re-verify after every item):**
1. **Crown-jewel:** `tui/app.py` and `cli.py` never call `subprocess`/`docker`/`pynvml`/`huggingface_hub`/process internals. All such work is agent-side. (`grep -nE "Popen|os\.kill|killpg|pynvml|snapshot_download|docker\.from_env|import subprocess" src/vela/tui/app.py src/vela/cli.py` must stay empty.)
2. **Verify-before-destructive-signal:** any `docker stop/kill` (and process kill) re-verifies sidecar identity (id+name+digest) first; mismatch refuses (`sidecar.py`).
3. **Scrub-before-wire:** secrets never appear in argv or logs; container logs/argv pass through the redaction `LogSink`. Secrets are `-e KEY` name-only in docker argv.
4. **Agent boundary:** the TUI holds a `TargetClient` (RPC only), never a process/sidecar handle.

**Test discipline:**
- Every item lands with a **real behavioral test** (drive the code, assert outcomes) — not a kwarg/mock-introspection test. The bar is the existing suite's quality (fake-docker binary, real agent dispatch, real RPC round-trips).
- Keep the suite deterministically green: `PYTHONPATH=src python -m pytest -q -p no:randomly` and `ruff check .` must both pass before declaring an item done.
- **New harness needed for Track B:** a **fake-SSH / fake-remote harness** analogous to `tests/fakes/fake_docker.py` (a stub `ssh` binary or an injectable transport double) so bootstrap/doctor/discovery flows can be tested without real hosts. Build this first (item **B0**) — most of Track B depends on it.

**Per-track exit criteria** are listed at the end of each track. A track is "100%" only when every HIGH+MED item is done with tests, the LOW items are done or explicitly deferred-with-rationale, and the spec's stated acceptance command(s) run green.

---

# TRACK A — Core deploy/runtime/model engine (88% → 100%)

### Already done (do NOT redo — confirmed real + tested in round 8)
Model-pin unresolved-`commit_sha` guard (correctly scoped, does not over-block precedence #1); pin-time `gated-auth`/`revision-not-found` raised; secrets-block for `save`/`push`/`edit`/`migrate` writers (old enshrining test inverted); backend-evidence gate fail-closed for FP8 + wired after smoke-tui; all r6 M1–M10 docker punchlist items; no safety regression.

---

### A1 — [HIGH] Close the `_clone_config` secret-at-rest bypass
- **Spec:** composer §11 (`vela-deployment-composer-spec-v1.md:219` "config lint **blocks** accidental secret literals"; `:217` "0644 configs, no secrets inside"; NFR-C4 `:72` "never written to a config except as env references").
- **Current state:** `src/vela/agent/local.py:822-875` `_clone_config` validates only via `ModelConfig.model_validate` (`:849`, schema-only) and **never calls `validate_config_payload`**. `_apply_config_overrides(payload, overrides)` runs at `:847` **before** `_write_public_text_atomic` at `:863`. The other four writers (`_save_config`, `_push_config`, `_edit_config`, `_migrate_wrapper_config`) all gate on `validate_config_payload`. Clone is the one unguarded writer.
- **Required change:** after overrides are applied and before the write, run the final payload through `validate_config_payload` (the path that invokes `_secret_literal_errors`); if `ok is False`, raise `TargetCallError("invalid-config", …)` with the secret-literal detail — exactly as `_save_config` does at `local.py:761`. The check **must** run post-override so `--set server.api_key=sk-live` is caught.
- **Acceptance criteria:** (a) `vela deploy clone src new --set server.api_key=sk-live` raises `invalid-config` and writes **no** file; (b) cloning a secret-bearing source raises; (c) cloning a clean config still succeeds and writes 0644.
- **Tests to add:** `test_agent_clone_config_blocks_literal_secret` with **two vectors** — (1) clean source + override injection, (2) secret-bearing source — each asserting the destination YAML does not exist; plus a positive `test_agent_clone_config_succeeds_for_clean_config`.
- **Notes:** This is the single urgent security item. Small, surgical fix (the gate already exists; clone just skips it).

### A2 — [MED] Test the backend-gate `_config_shape_errors` branch + the FLASHINFER-absent reject path
- **Spec:** beyond-spec hardening mirroring the local FP8 script gates (`/Users/brennanconley/vibecode/infx/qwen36-27b-test/start-qwen36-27b-fp8-rp6000-blackbird.sh:216-229`, exit 30/31/32).
- **Current state:** `scripts/backend_evidence_check.py:117-147` `_config_shape_errors` (5 fail-closed modes: `command.runtime==docker`, pinned image SHA, `FLASHINFER_CUDA_ARCH_LIST==12.0f`, `engine.kv_cache_dtype==fp8`, `--attention-backend FLASHINFER`) has **zero tests**. The reject parametrize at `tests/test_remote_workflow.py:539-556` covers only cutlass-missing + MARLIN-forbidden. The **exit-31 equivalent** (cutlass present + FLASHINFER absent) is implemented and fail-closed but **untested**.
- **Required change:** none to logic (it is correct/fail-closed by execution); add tests.
- **Acceptance criteria:** parametrized reject tests cover all 5 `_config_shape_errors` modes (each violation → raise) and a log fixture with cutlass-present/FLASHINFER-absent → raise.
- **Tests to add:** extend `tests/test_remote_workflow.py` reject suite with the 5 config-shape cases + the FLASHINFER-absent log case; one accept case proving a fully-correct FP8 config+log passes.

### A3 — [MED] Close the backend-gate fail-open on config-name drift
- **Spec:** same as A2 (the gate must not silently skip a Blackwell run).
- **Current state:** the rule is keyed on the CLI `config_name` arg (`scripts/backend_evidence_check.py:76`) with no cross-check against the reattached run's `config['name']`. A renamed FP8 config (rule key not matched) silently returns `{checked: False}` → exit 0. Bounded today because `run_remote_tests.sh` passes the same `$real_config` to both smoke and gate.
- **Required change:** add a cross-check — assert the reattached run's `config['name']` matches the `config_name` rule key, and/or fail-closed if a config whose shape looks Blackwell-FP8 (docker runtime + fp8 kv) runs with **no** matching rule. Prefer: when `_config_shape_errors` would apply (FP8 docker recipe) but no `BACKEND_EVIDENCE_RULES` entry matches, raise rather than skip.
- **Acceptance criteria:** a renamed/unregistered FP8 config does not silently pass; the gate raises or explicitly logs a non-skippable warning that fails CI.
- **Tests to add:** `test_backend_evidence_does_not_silently_skip_unregistered_fp8_config`.

### A4 — [MED] Add `_edit_config` literal-secret test coverage
- **Spec:** composer §11 (`:219`).
- **Current state:** `_edit_config` (`local.py:799-807`) validates-before-write (code-correct, would block) but **no test** injects a literal secret. A regression removing the `:799` guard ships silently.
- **Required change:** none (code correct); add test.
- **Acceptance criteria / tests:** `test_agent_edit_config_blocks_literal_secret` driving `vela deploy edit <name> --set server.api_key=sk-live` → `invalid-config`, no write; passes only while the guard exists.

### A5 — [MED] Resolve the model-pin **offline** deviation (spec L263)
- **Spec:** `vllm-model-management-spec-v1.md:263` — offline pin → record the ref with `commit_sha=null`, `cache_state=remote_only`, **warn** (register intent now, download later). Precedence #2 (`:351`) + PM1 (`:442`) require the *launch-time* guard to block an unresolved pinned `model_ref`.
- **Current state:** the new pin-time HF resolution **hard-fails** the offline-without-commit case (`model_registry.py` `_resolved_hf_model_info`), removing the spec L263 capability. (Round 8: empirically `network` error, no registry entry written.)
- **Required change (recommended option A):** separate "register intent" from "launch safety." At **pin time**, when offline/unresolvable, record a `remote_only` entry with `commit_sha=null` and emit a **warning** (restore L263). Keep the **launch-time** unresolved guard (`local.py:4367`) as the immutability enforcer — it already blocks launching an unresolved `model_ref`. This is spec-compliant and still safe (you cannot *launch* a mutable pin). *Option B (if you keep the hard-fail):* document it as a deliberate deviation in the spec + `docs/` and add a `--remote-only`/`--allow-unresolved` escape hatch on `model pin`.
- **Acceptance criteria:** offline `vela model pin <repo>` (no revision) records a `remote_only` entry + warning (no hard error); launching it is still blocked by the unresolved guard.
- **Tests to add:** `test_model_pin_offline_records_remote_only_with_warning` + `test_launch_blocks_unresolved_remote_only_pin` (the latter likely already exists — assert both halves).

### A6 — [MED] Complete the pin-time error taxonomy (repo-not-found)
- **Spec:** `vllm-model-management-spec-v1.md:144` error taxonomy; `:263`.
- **Current state:** `gated-auth` (401/403) and `revision-not-found` (404) are raised, but `RepositoryNotFoundError` / some 404s fall through to a generic `model-download-failed` (round-8 missed finding).
- **Required change:** map `RepositoryNotFoundError` (nonexistent repo) → a named `repo-not-found` kind (or fold into `revision-not-found` with a repo-level message) with the spec remediation; ensure no HF resolution error reaches a generic kind.
- **Acceptance criteria / tests:** `test_model_pin_nonexistent_repo_raises_named_kind` → named kind, not generic.

### A7 — [MED] Docker discover/reattach across a true process restart (the one genuine docker test gap)
- **Spec:** `vela-docker-runtime-spec-v1.md` §6.4 (DK2 "Done when: a fake docker run … reattach verifies identity"); the spec-named `test_docker_discover_reattach`.
- **Current state:** reattach happy-path is tested (`tests/test_agent_client.py:2014`) and log-replay is tested (`:2094`), but **no** test spawns a **fresh** agent that re-reads a persisted docker sidecar from disk and re-verifies id+digest; and there is no **mismatch-on-reattach** (stale id/digest) refusal test.
- **Required change:** none to logic (the path exists via `verify_sidecar_from_system` → `verify_container_running` → `_verified_container_inspect`); add tests using the fake-docker harness.
- **Acceptance criteria / tests:** `test_docker_discover_reattach_across_restart` (fresh agent reads the persisted sidecar, `discover` lists it, reattach verifies id+name+digest) **plus** a `…_refuses_stale_container` variant (fake inspect returns a recycled id/digest → refusal, no destructive command).

### A8 — [LOW] Assert the remaining docker error-kind classifications
- **Current state:** `IMAGE_NOT_FOUND` and `PORT_IN_USE` are asserted; `NAME_CONFLICT` and `GPU_NOT_AVAILABLE` `DockerErrorKind` paths are **not** directly asserted (round-8).
- **Required change / tests:** drive the fake-docker `run` to emit a name-conflict stderr and a gpu-not-available stderr; assert `classify_docker_error` → the correct kind. (`docker_runtime.py:209-226`.)

### A9 — [LOW] Add `ERROR_GUIDANCE` entries for the new docker ErrorKinds
- **Current state:** `tui/app.py:395-407` `ERROR_GUIDANCE` has 11 entries; the docker kinds added in DK1 (`engine/phases.py:27-30`: `IMAGE_NOT_FOUND`, `DAEMON_UNREACHABLE`, `NAME_CONFLICT`, `GPU_NOT_AVAILABLE`) have **none** → the operator sees the generic "Check the last log lines." fallback.
- **Required change:** add specific remediation for each — e.g. `IMAGE_NOT_FOUND` → "pull or correct `command.docker.image` (check the `@sha256:` digest)"; `DAEMON_UNREACHABLE` → "start Docker / check the daemon socket on the target"; `NAME_CONFLICT` → "a container with this name exists — add it to `command.docker.evict` or remove it"; `GPU_NOT_AVAILABLE` → "check `--gpus` / the nvidia container runtime / the driver."
- **Acceptance / tests:** each kind renders its specific guidance; `test_error_guidance_covers_docker_kinds`.

### A10 — [LOW] Make `vela deploy create` overwrite explicit (M8 carryover)
- **Spec:** composer FR-C12 / §9 (`:205`) "idempotent on `<name>` (re-create = update)."
- **Current state:** `cli.py:1259` passes `overwrite=True` **unconditionally** → `--overwrite` is dead for `create` and a same-name create **silently** clobbers. (Pre-existing from `acd2514`, not a regression.)
- **Required change:** keep idempotent-update semantics (spec-sanctioned) but make it **non-silent** — echo "updated existing config `<name>`" when overwriting, and either wire `--overwrite` to gate it (default-refuse without it) or drop the dead flag from `create` with a help note. Recommended: idempotent update + explicit echo; keep `--overwrite` meaningful by making the default behavior refuse-then-hint unless `--overwrite` or an interactive confirm.
- **Acceptance / tests:** same-name create without `--overwrite` either refuses with `config-exists`+hint or echoes an explicit update; `--overwrite` is not dead. Test asserts the chosen behavior.

### A11 — [LOW] Stop emitting `--shm-size` when `--ipc=host` (M9 carryover, benign)
- **Spec:** `vela-docker-runtime-spec-v1.md:131` argv template `(--ipc=host | --shm-size <shm_size>)` (alternatives).
- **Current state:** `docker_runtime.py:71-75` emits `--ipc=host` (default) **and** always `--shm-size` (computed default 16g/32g). Docker ignores `--shm-size` under `--ipc=host`, so benign, but spec-divergent and noisy.
- **Required change:** only emit the **computed** `--shm-size` default when `ipc_host` is false; always honor an **explicit** `docker.shm_size`.
- **Acceptance / tests:** default docker config (`ipc_host=true`, no `shm_size`) emits `--ipc=host` and **no** `--shm-size`; explicit `shm_size` still emitted; update `tests/test_command_builder.py`.

### A12 — [LOW] Lock the exposure-mismatch lint **warn-not-block** contract
- **Spec:** composer FR-C5 (warn on exposure mismatch, do not block).
- **Current state:** lint emits the exposure warning, but `tests/test_deployment_composer.py:1043` asserts only the warning string, never that `ok` stays `True`.
- **Required change / tests:** extend the test to assert `ok is True` on exposure-mismatch (warning present, not an error) — locks the contract distinct from the secret-literal block.

### A13 — [LOW] Test the model-GC dedup-aware display
- **Current state:** dedup-aware removal (`delete_revisions(...).expected_freed_size`) is real but the "X unique / Y nominal" freed-size display is untested (round-8 F10).
- **Required change / tests:** `test_model_remove_reports_unique_vs_nominal_freed_size`.

### A14 — [PROCESS] DK4 native-docker hardware re-validation + retire wrappers
- **Current state:** existing DK4 artifacts are authentic READY runs but predate the backend-evidence gate. The legacy `scripts/blackbird_qwen36_*_vllm_foreground.sh` wrappers still exist (configs already migrated to native `runtime: docker`).
- **Required change (operator/hardware):** run a fresh native-docker **FP8 + BF16** validation on Blackbird through the full remote lane so the **backend-evidence gate** passes (not just READY); commit the artifacts; then **retire or archive** the foreground wrapper scripts now that native docker is the path.
- **Acceptance:** committed native-docker FP8+BF16 artifacts that include a backend-evidence **PASS**; wrappers removed or moved to an `archive/` with a note.

### A15 — [LOW] Harden the remote-lane run_id contract + apply gate to restart
- **Current state:** `run_id` is extracted via a loose `run_id=<token>` substring print contract; the backend-evidence gate is not applied to the resume/restart lane.
- **Required change:** emit `run_id` as a structured/labeled field the gate parses robustly; optionally run the gate after a restart in the remote lane for Blackwell recipes.
- **Acceptance / tests:** run_id parse is robust to log-format changes; documented.

**Track A exit criteria:** A1–A6 done with tests; A7–A13 done; A14 hardware-validated; A15 done or explicitly deferred. Suite green, ruff clean, crown-jewel clean, all four invariants re-verified.

---

# TRACK B — Onboarding self-guiding UX (36% → 100%)

> The onboarding spec (`vela-onboarding-ux-spec-v1.md`) is a forward/recommendations doc. Its own phasing is **P0** (fix N5-1 — DONE), **P1** (R1 auto-resolve + named-failure remediations), **P2** (bootstrap + doctor), **P3** (R2 edit + R6 managed token), **P4** (nice-to-haves). This track follows that order.

### Already done (do NOT redo — confirmed in round 8)
`target.agent_command` field **and** consumed verbatim by `factory.py:_remote_agent_command` (R1 infrastructure); first-class `--ssh-key` → `-i` (R4); ControlMaster defaults (R4); **N5-1 silent-token-drop fixed** (P0); N5-2 token entropy check; token-as-file **read** fallback (`auth.py`); SSH stderr → named error codes `ssh-auth`/`ssh-failed`/`command-not-found` (`transport/subprocess.py:335-397`) (R4 capture half); R2 `config push`/`pull`/`lint`.

### What's missing: the *flows* (bootstrap/doctor are scaffolds today) and the discovery probe.

---

### B0 — [HIGH, do first] Build the fake-SSH / fake-remote test harness
- **Why:** B1–B8 all exercise SSH-mediated remote behavior. Without a harness they cannot be tested for real, and "tests pass" must mean something (round 8 flagged the current bootstrap/doctor tests as hollow).
- **Required change:** add `tests/fakes/fake_ssh.py` (a stub `ssh` binary à la `fake_docker.py`, parameterized by env: present/absent `vela`, `vela --version` output, SSH exit code/stderr, install success) **or** an injectable transport double that satisfies the `TargetClient`/subprocess-bridge seam. Provide canned `host_report` payloads.
- **Acceptance:** a test can simulate (a) reachable host with `vela` at a canonical path, (b) reachable host with no `vela`, (c) unreachable host (exit 255 + "Permission denied (publickey)"), (d) version mismatch.

### B1 — [HIGH/P1] R1 — SSH agent **discovery probe** (the spec's "highest-value" fix)
- **Spec:** R1 (`vela-onboarding-ux-spec-v1.md:42-44`): during `targets add`/`bootstrap`/`test`, probe in order `command -v vela` → `~/.local/share/vela/venv/bin/vela` → `~/venvs/vela/bin/vela` → `<venv>/bin/vela` (if `--venv`) → `python3 -m vela`; pick the first returning a **compatible** `vela --version`; store as `agent_command`. None found → `AGENT_NOT_INSTALLED` naming `vela targets bootstrap <name> --install`.
- **Current state:** **absent** — no `probe`/`discover`/`find_agent` function in `src/vela`. The operator must hand-wire `--agent-command` (the `--venv` footgun is merely relabeled).
- **Required change:** implement an SSH discovery probe (reuse the `transport/factory.py` SSH-option builder + the subprocess bridge) that runs the ordered path search over SSH, validates version compatibility against the controller's `vela_version` (reuse the handshake version logic), and returns the resolved **absolute** `agent_command`. Wire it into `targets add` (when `--agent-command` is not supplied), `targets bootstrap`, and `targets test`. Emit `AGENT_NOT_INSTALLED` with the exact remediation when nothing is found.
- **Acceptance criteria:** `vela targets add --host user@host` with **no** `--venv` and **no** `--agent-command` connects when `vela` is installed in any canonical location; if absent, a named `AGENT_NOT_INSTALLED` error carrying `vela targets bootstrap <name> --install`.
- **Tests (need B0):** discovery resolves each canonical path; version-mismatch is rejected; not-found → `AGENT_NOT_INSTALLED` with the remediation string.

### B2 — [HIGH/P1] §3 — Named-failure remediations with **exact commands** + target-name injection
- **Spec:** §3 (`:87-88`) — the "cheapest, highest-leverage" change. `AGENT_NOT_INSTALLED` → `vela targets bootstrap <name> --install`; `AGENT_UNREACHABLE` → `vela targets setup-ssh <name>` + the actual SSH stderr; `AGENT_VERSION_MISMATCH` → "upgrade the agent: `vela targets bootstrap <name> --install`"; build `feature-unavailable: uv-required` → `vela build doctor` + install-uv.
- **Current state:** named **codes** route correctly, but banners carry generic prose with **no command and no target name** (`tui/app.py:3492-3512`: "Upgrade the older side", "Check SSH/socket connectivity", "Install vela on the target").
- **Required change:** a single remediation map keyed by error code → `(one-line fix, exact command template)`, with the **active target name interpolated**. Render it in both the TUI banner (`app.py`) and the CLI error path. Reuse the captured SSH stderr (`subprocess.py`) in the `AGENT_UNREACHABLE` banner.
- **Acceptance criteria:** each named failure shows the exact command with the real target name (e.g. "SSH auth failed: Permission denied (publickey) — run `vela targets setup-ssh blackbird`").
- **Tests:** assert the rendered remediation (command + target name) for each of the four codes.

### B3 — [HIGH/P2] `vela targets bootstrap` — the full guided flow
- **Spec:** §1 (`:20-28`): (1) SSH reachability+auth probe → on failure offer `setup-ssh`; (2) agent discovery (R1) → if absent and `--install`, install to the canonical location and record the absolute path; (3) build readiness → with `--build`, preflight uv/toolchain and create a default managed build; (4) write the target with the auto-resolved agent path (no manual `--venv`); (5) handshake test + per-line green/red summary with fixes.
- **Current state:** `cli.py:249-290` is `targets add` renamed — builds a `TargetConfig`, `upsert_target_file`, echoes "bootstrapped" + a static "next". **No** probe, discovery, `--install`, `--build`, or handshake (the `--install`/`--build` flags don't even exist).
- **Required change:** implement the orchestration: add `--install` and `--build <spec>` flags; SSH reachability probe (reuse the factory SSH builder); call the B1 discovery probe; on `--install`, run a remote install job (`pip install 'vela @ git+…'` into `~/.local/share/vela/venv` — see B12 — as a streamed job) and record the resolved `agent_command`; on `--build`, run `check_build_prerequisites` + `create_build`; write the target with the resolved fields; run a handshake test; print a per-check green/red summary that routes each red line through the B2 remediation map.
- **Acceptance criteria (spec's literal acceptance, `:28`):** `vela targets bootstrap blackbird --host bgconley@10.25.0.51 --install --build 'vllm==0.11.2'` yields a connectable, launch-ready target with **zero hand-edited fields**.
- **Tests (need B0):** fake-SSH drives the matrix — reachable+vela-present → all green, no install; reachable+vela-absent+`--install` → install job runs then discovery resolves; unreachable → `setup-ssh` remediation; `--build` → prerequisites + create_build invoked.

### B4 — [HIGH/P2] `vela doctor [--target] [--json]` — real two-host introspection
- **Spec:** §1 (`:30-31`) + R5 (`:73-75`): introspect controller + target — vela version match, SSH reachability/auth, resolved agent path, Python/uv/CUDA/driver/GPU on the target, resolved per-host dirs (config/runs/builds/models/socket), token/auth status, active build/model. Each failing check carries an exact remediation; healthy = all green.
- **Current state:** `cli.py:177-192` `doctor` has only `--json` (no `--target`); `_doctor_payload` (`cli.py:1937-1972`) runs **two local-filesystem checks** (targets file parses; `configured_agent_token()` doesn't raise) and returns an **unconditional static** `next_steps` list — which round 8 flagged as **actively misleading** (it tells a healthy target to re-run bootstrap).
- **Required change:** (a) add `--target`; (b) add a `diagnose`/`doctor` **agent RPC** returning a structured `host_report` (version, resolved dirs, python/uv/cuda/driver/gpu, auth status, active build/model) — reuse `check_build_prerequisites` for the toolchain and the handshake `host_info` for dirs; (c) controller-side render a **per-check green/red checklist** with a remediation per failing check (reuse B2); (d) make `next_steps` **conditional on failures** — remove the static literal; an all-green target shows no "run bootstrap" nag; (e) `--json` structured output.
- **Acceptance criteria:** `vela doctor --target blackbird` shows a green/red checklist for both hosts; a misconfigured target → a red line + exact remediation per failing check; a healthy target → all green with **no** misleading next-steps.
- **Tests (need B0):** fake-agent returns a `host_report`; doctor renders pass/fail + conditional next_steps; an all-green run asserts the absence of the bootstrap nag; `--json` shape asserted.

### B5 — [MED/P2] R5 — per-host path visibility in doctor + `agent status` + `targets test`
- **Spec:** R5 (`:73-75`): print the resolved paths on each host (config `~/.config/vela`, runs `~/.local/state/vela/runs`, builds `~/.local/share/vela/builds`, models registry, socket, version). Document `VELA_CONFIGS`/`XDG_*` overrides in one place.
- **Current state:** handshake `host_info` (`local.py:560-564`) carries only hostname/platform/driver/`vela_version` — no dirs. No diagnose RPC.
- **Required change:** extend `host_info`/`host_report` with `config_dir`/`runs_dir`/`builds_dir`/`models_registry`/`socket_path`; render in `doctor` (B4) and `vela agent status`; surface `host_info`/resolved paths/version-match in `targets test` (a cheap R5 win). Add a `docs/` section documenting the `VELA_CONFIGS`/`XDG_*` overrides.
- **Acceptance / tests:** one command answers "where does this host keep its stuff"; `targets test` shows host_info + version match; `test_handshake_host_report_includes_resolved_dirs`.

### B6 — [MED/P3] R4 — `vela targets setup-ssh <name>` (guided `ssh-copy-id`)
- **Spec:** R4 (`:68`).
- **Current state:** absent.
- **Required change:** a `targets setup-ssh <name>` command that runs `ssh-copy-id` (with the target's host + `--ssh-key`), with clear success/failure output; wire the `AGENT_UNREACHABLE`/`ssh-auth` remediation (B2) to name it.
- **Acceptance / tests (need B0):** a passwordless-not-configured target → the `setup-ssh` remediation; running `setup-ssh` invokes `ssh-copy-id` with the right args (assert via the fake-SSH harness).

### B7 — [MED/P3] R6 — `vela agent gen-token --install --target` pushes the token to the remote
- **Spec:** R6 (`:81`): `gen-token --install` writes `~/.config/vela/agent-token` (0600) on the host **and** (for SSH targets) pushes the matching token to the target's file; both sides read the file if `VELA_AGENT_TOKEN` is unset.
- **Current state:** `gen-token --install` (`cli.py:2658-2691`) is controller-local only — writes the local file; **no `--target`, no remote push**. (The file-read fallback exists.)
- **Required change:** add `--target` to `gen-token --install`; push the same token to the target's `~/.config/vela/agent-token` (0600) over the transport (reuse the `push_config` write path or a dedicated `write_agent_token` RPC). Report auth status.
- **Acceptance criteria (spec acceptance, `:83`):** `vela agent gen-token --install --target blackbird` enables auth on both hosts in one command.
- **Tests (need B0):** token written locally (0600) **and** pushed to the fake target file (0600).

### B8 — [MED/P3] R3 — `vela build doctor [--target]` standalone diagnostic
- **Spec:** R3 (`:59-60`): report python, uv, CUDA toolkit, driver, GPU arch, and which build methods are available with install hints (reuse `check_build_prerequisites` → `uv_available`). Optionally offer an agent-side "install uv now" streamed job.
- **Current state:** `check_build_prerequisites` is reused only as an **internal silent preflight** inside `_create_build_cli` (`cli.py:2345-2354`); no user-facing command.
- **Required change:** a `vela build doctor [--target]` command rendering the toolchain + available build methods + install hints from `check_build_prerequisites`. (Optional P4: an "install uv" streamed job and a one-keypress offer when `nightly`/`commit` is picked without uv.)
- **Acceptance / tests:** `vela build doctor --target blackbird` reports the toolchain + which methods (pip/nightly/commit/git/wheel/adopt) are available + install hints; `test_cli_build_doctor_renders_prerequisites` against a fake agent.

### B9 — [MED/P3] R2 — `vela config edit <name> --target` (pull → `$EDITOR` → push)
- **Spec:** R2 (`:51`).
- **Current state:** `push`/`pull`/`lint` exist; `edit` (the round-trip) is **absent** (round-8 F-10).
- **Required change:** a `config edit` command — pull the target config, open `$EDITOR`, **lint + secret-block** (route through the same gate as A1/A4), push back atomically.
- **Acceptance / tests:** edit a target config locally without manual SSH; lint/secret-block enforced before push; `test_cli_config_edit_round_trip` with a stubbed editor (and a secret-injection variant that refuses to push).

### B10 — [MED/P2-3] Richer auth-status reporting (5 states)
- **Spec:** R6 (`:82`): report `none | required+provided | required+missing | mismatch | malformed-token`.
- **Current state:** `_doctor_payload` collapses to binary present/absent (round-8 missed finding).
- **Required change:** surface the five auth states in `doctor` (B4) and `targets test`, derived from the handshake/auth result.
- **Acceptance / tests:** each of the five states renders distinctly; tests for `required+missing`, `mismatch`, `malformed-token`.

### B11 — [LOW/P4] TUI affordances — "Bootstrap target…" / "Push config…"
- **Spec:** §4 P4 (`:97`) + R2 (`:53`).
- **Required change:** command-palette / target-manager affordances to launch bootstrap and to "Push this config…" when a local config isn't present on the target.
- **Acceptance / tests:** affordances present + wired; Textual smoke test.

### B12 — [LOW/P4, but B3 depends on it] Canonical install path + remote install job
- **Spec:** R1 (`:41`) canonical `~/.local/share/vela/venv/bin/vela`; §1 `bootstrap --install` installs there.
- **Required change:** a controller-driven remote install routine (create the canonical venv + `pip install 'vela @ git+…'`, or rsync + venv) as a streamed job, used by `bootstrap --install` (B3). Record the resulting absolute `agent_command`.
- **Acceptance / tests:** `bootstrap --install` on a vela-less host installs to the canonical path and discovery (B1) resolves it; fake-SSH asserts the install command + resulting path.

**Track B exit criteria:** B0 harness built; B1–B4 (the P1/P2 headline: discovery probe, remediations, bootstrap flow, doctor) done with real tests; B5–B10 (R5/R4/R6/R3/R2-edit/auth-states) done; B11–B12 done or deferred. The spec's two acceptance commands run green: `vela targets bootstrap … --install --build …` and `vela doctor --target …`. `doctor` next-steps are conditional (no misleading static nag).

---

# TRACK C — TUI primary-surface breadth (52% → 100%)

> Spec: `vela-deployment-composer-spec-v1.md` §7 (customize) + §8 (the six wizard steps) + §10 (composition). The narrow DC4 bar ("composes + smokes a deployment from the TUI") is already met; this track closes the **breadth** the spec calls the "primary surface."

### Already done (do NOT redo — confirmed in round 8)
Existing-pin model mode + existing-build adopt (wired into a composer that consumes `model_ref`/`revision`, `composer.py:753-773`), one substantive end-to-end Textual test (`tests/test_tui_smoke.py:3850-3962`); the compose → review → preflight → save → smoke pipeline; FlagManager reused via the review→customize affordance; `n` binding + palette entry; recipe prefill; per-model dtype/kv/TP suggestions + gated-needs-token warning **do** reach the Review surface via `compose_config` for registry pins.

---

### C1 — [HIGH] Step 2 Runtime — in-wizard "Create build" (→ build flow) + "Adopt venv"
- **Spec:** §8 step 2 (`:182`): "existing build · **create build (→ build flow)** · **adopt venv** · Docker image · explicit executable"; §10 (`:210`): "if `create_build`, the wizard runs the build flow first then pins it."
- **Current state:** `tui/screens/new_deployment.py:152-158` runtime `Select` = `{Process, Docker, Build, Executable}`, where "Build" is an **adopt-existing-registered-build** dropdown (`:168-174`) + bare-text fallback. There is **no** "Create build" or "Adopt venv"; those flows live only in the standalone build manager (`app.py:1191-1202`, `CreateBuildScreen`/`AdoptBuildScreen`).
- **Required change:** add "Create build" and "Adopt venv" runtime options. On selection, **hand off to the existing `CreateBuildScreen`/`AdoptBuildScreen`** (reuse — do not reimplement), then return the resulting build id into the wizard draft and pin it (`command.build`). Wire the `app.py` callback so the wizard resumes at Review after the build flow completes.
- **Acceptance criteria:** from the wizard, an operator can **create** a new managed build (the build flow runs, then the deployment pins it) and **adopt a venv** — without leaving for the standalone manager.
- **Tests:** a Textual test drives runtime = "Create build" → the build flow → returns to Review with `command.build` set on the composed config; an adopt-venv variant.

### C2 — [MED] Step 3 Model — pin-HF-repo (+revision), adopt-local-path, download now/at-launch, gated/cached state
- **Spec:** §8 step 3 (`:183`): "existing pin · **pin HF repo** · **adopt local path** · bare repo id; **download now / at launch** choice; **gated/cached state shown**"; §10 (`:211`): gated/cached pre-checks feed warnings; "download now" reuses `download_model`.
- **Current state:** `new_deployment.py:184-196` = pinned-model `Select` (existing-pin) + a bare-text model `Input`. Missing: a distinct **pin-HF-repo+revision** mode, an **adopt-local-path** mode, the **download now/at-launch** toggle, and **gated/cached** state display.
- **Required change:** a model-mode selector (existing pin / pin HF repo / adopt local path / bare repo id). For pin-HF: a revision field + a "download now / at launch" toggle (download-now → `download_model` RPC). For adopt-local: a path field. Show gated/cached state + the gated-needs-token warning (from `list_models`/model metadata). Reuse the standalone Model Manager's pin/adopt/download flows where possible (handoff like C1) rather than duplicating.
- **Acceptance criteria:** an operator can pin an HF repo + revision and choose download-now from the wizard; gated/cached state + token warning shown for the selected model.
- **Tests:** Textual tests — pin-HF mode → `compose_config` receives `model_ref`+`revision`; download-now triggers `download_model`; gated model shows the token warning.

### C3 — [MED] Step 1 Target — registry picker with connection dot
- **Spec:** §8 step 1 (`:181`): "pick from the targets registry (connection dot shown); defaults to the active target."
- **Current state:** `new_deployment.py:129/138` = a **static** "Active target" `Static`; the target is fixed to the active target (round-8). No registry `Select`, no connection dot.
- **Required change:** a target `Select` populated from the targets registry, defaulting to the active target, with a per-target connection-state dot (reuse the target-manager's connection check). On change, re-scope the wizard's RPC calls to the chosen target.
- **Acceptance criteria:** the wizard lets the operator pick any registered target with a live connection dot; compose/preflight/save run against the chosen target.
- **Tests:** Textual test selects a non-active target; the subsequent RPCs target it.

### C4 — [MED] Step 4 Customize — FlagManager §7 affordances (preset picker, reset-to-preset, show-changed-only)
- **Spec:** §7 (`:174`): "New affordances: a 'preset' picker at the top, a 'reset to preset/default' per field, and a 'show changed only' filter."
- **Current state:** FlagManager is reused (good); reset-to-build-**default** exists (`d` binding), but the §7 additions are missing — no in-FlagManager **preset picker**, no per-field **reset-to-preset value**, no **show-changed-only** filter.
- **Required change:** add to `FlagManagerScreen`: a preset picker at the top (re-seed `engine.*` from the chosen preset), a per-field "reset to preset value" (distinct from reset-to-build-default), and a "show changed only" toggle/filter.
- **Acceptance criteria:** in customize, the operator can switch preset, reset a field to its preset value, and filter to changed-only.
- **Tests:** each affordance asserted (preset switch re-seeds; reset-to-preset; filter hides unchanged).

### C5 — [MED] Live `suggest_deployment_defaults` hints before Review
- **Spec:** §6.4 per-model dtype/kv/TP suggestions + gated-needs-token warning on the primary surface; §8 step 3 gated/cached state.
- **Current state:** `compose_config`'s embedded `_engine_suggestions` surfaces dtype/kv/TP + the warning **at Review** for registry pins (round 8 refuted the "absent" claim) — but the dedicated `suggest_deployment_defaults` RPC is **never called by the TUI**, so there's no **live, at-selection-time** hint **before** Review, and the bare-text path (no registry entry) gets no suggestions.
- **Required change:** call `suggest_deployment_defaults` at model-selection time to show live dtype/kv/TP hints + gated-needs-token warning **before** Review, including for the bare-text/HF-pin path.
- **Acceptance criteria:** selecting a model shows live suggestions + token warning before reaching Review (registry **and** bare-text).
- **Tests:** model selection triggers `suggest_deployment_defaults`; hints rendered pre-Review.

### C6 — [LOW] Test the review→Customize→FlagManager round-trip + a real bounded-smoke walk
- **Current state:** no Textual test drives review→Customize (FlagManager edit)→re-review; the wizard Save-&-Smoke test stubs `prepare_launch` rather than walking a fake-docker container to READY (round-8 F18 residual).
- **Required change / tests:** (1) a test driving review→Customize (edit a flag)→re-review that asserts the edited flag flows into the resolved command; (2) a wizard Save-&-Smoke test that walks a **fake-docker** container (reuse `tests/fakes/fake_docker.py`) to READY through the wizard, then auto-stops (`Phase.STOPPED`).

### C7 — [LOW] Step 6 Save & Smoke — surface the **named failure** + remediation in the wizard
- **Spec:** §8 step 6 (`:186`): bounded smoke; shows READY URL/model **or the named failure**.
- **Current state:** bounded smoke is wired (r7 M3); confirm it renders the **named failure + remediation** on smoke failure (not a generic error), reusing the B2 remediation map.
- **Required change / tests:** on smoke failure the wizard shows the named failure + fix; a fake-docker run that fails → the wizard surfaces it (test).

**Track C exit criteria:** C1 (in-wizard create-build/adopt) + C2 (model modes/download/gated-cached) + C3 (target picker) + C4 (FlagManager affordances) + C5 (live suggestions) done with Textual tests; C6–C7 done. The wizard now realizes all six §8 steps with their full per-step breadth.

---

## 4. Suggested sequencing (dependency-aware)

1. **A1 first** — the one urgent security fix (small, isolated).
2. **B0** (fake-SSH harness) — unblocks all of Track B's real tests; do early.
3. **Track B P1**: B1 (discovery probe) → B2 (named remediations). These are small, high-leverage, and feed B3/B4.
4. **Track B P2**: B12 (install job) → B3 (bootstrap) ; B5 (host_report dirs) → B4 (doctor) → B10 (auth states). Bootstrap and doctor are the headline.
5. **Track C** can proceed in parallel with Track B (different files): C1 → C2 → C3 → C4 → C5, then C6–C7. C1/C2 reuse existing build/model screens, so they're handoff-wiring, not new flows.
6. **Track A remainder**: A2–A6 (tests + the offline/taxonomy fixes), then A7–A13 (coverage/polish), A10/A11 (the M8/M9 nits), and A14 (hardware re-validation) when a Blackbird window is available.
7. **B P3**: B6 (setup-ssh), B7 (token push), B8 (build doctor), B9 (config edit). **B P4 + C/A LOW**: B11, A15, and any deferred LOW items last.

## 5. Definition of "done" for the whole handoff
- All **HIGH** and **MED** items across A/B/C implemented with real behavioral tests; **LOW** items done or explicitly deferred with a one-line rationale in `.wolf/memory.md`.
- `PYTHONPATH=src python -m pytest -q -p no:randomly` green; `ruff check .` clean; crown-jewel grep empty; the four safety invariants re-verified.
- The three spec acceptance commands run green: `vela targets bootstrap <name> --host … --install --build …`, `vela doctor --target <name>`, and a TUI New-Deployment wizard run that creates a build, pins an HF model, and bounded-smokes to READY — end to end.
- `.wolf/anatomy.md` updated for new files (`fake_ssh.py`, any new screens/RPCs); `.wolf/buglog.json` updated for the A1 clone bypass and any bug found while implementing.

## 6. Pointers (key seams to reuse, not reinvent)
- SSH: `transport/factory.py` (option builder, `_remote_agent_command`, ControlMaster), `transport/subprocess.py` (stderr capture + named reasons).
- Agent RPC dispatch: `agent/local.py:handle()` (add `diagnose`/`write_agent_token` here); capability list in `transport/client.py`.
- Config writers + the secrets gate: `agent/local.py` `_save_config`/`_push_config`/`_edit_config`/`_migrate_wrapper_config` (the model for A1); `engine/composer.py:validate_config_payload` / `_secret_literal_errors`.
- Build/model flows to hand off from the wizard: `tui/screens/` `CreateBuildScreen`/`AdoptBuildScreen`/the Model Manager; `engine/composer.py` `suggest_deployment_defaults`/`compose_config`.
- Test harnesses: `tests/fakes/fake_docker.py` (template for `fake_ssh.py`), `tests/fixtures/docker_logs/`.
- Backend gate: `scripts/backend_evidence_check.py`, `scripts/run_remote_tests.sh`, `tests/test_remote_workflow.py`.

---
*Handoff prepared 2026-06-06 from review rounds 6–8 (findings v6/v7/v8). Read-only review — no code modified in producing this document.*
