# vLLM Agent Architecture — Implementation Review Punch List **v2**

**Supersedes (does not replace):** `vllm-agent-architecture-review-punchlist.md` (v1). Keep both; v1's P1–P4 are all **CLOSED** (see below).
**Reviewing:** in-progress implementation of `vllm-agent-architecture-spec-v1.md` (+ the build/model sibling specs it composes).
**State at this review:** **438 tests passing.** PA0–PA4 complete; PA5 ~80%; PA6 ~65% (scaffolded, key pieces stubbed); PA7 ~40%.
**Method:** five Sonnet 4.6 reviewers; load-bearing + safety-critical findings independently verified by Opus 4.8 (tagged **[Opus-verified]** = checked against the code directly).

**How close to done (this review's headline):** architecture/plumbing ~90% • feature completeness ~70% • validated/production-ready ~50%. The hard part is essentially done; what remains is connecting the skeleton to real external tools, one safety fix, and validation.

---

## Current status after remediation — 2026-06-04

This review snapshot is now stale. The punchlist below is kept as review
history, but the current tree has closed the load-bearing gaps it identified:

- **P1 closed:** build/model removal now refuses verified live-run usage, with
  sidecar/ref tests for live, stale, and config-pinned cases.
- **P2/P3 closed:** build `pip`/`uv` install jobs and model download jobs stream
  through the agent, support cancellation/partial state, inject op-time
  `HF_TOKEN` where needed, and scrub job output before both the wire and
  durable job logs. The TUI's universal `s` Stop binding cancels an in-flight
  build/model job via `cancel_job`.
- **P4 closed for the current done gate:** remote validation is recorded in
  `artifacts/remote-validation/2026-06-04-p620-blackbird-smoke.md`, including
  P620-01 controller to Blackbird agent handshake/list/preview/Qwen smoke,
  Blackbird daemon-restart and disconnect/reconnect resume checks, a real tiny
  Hugging Face model download/verify, and a real `vllm==0.11.2` build
  install/verify. Physical laptop sleep was not separately exercised; the
  reconnect/resume path it depends on was.
- **P5 closed:** registry-minted build/model ULIDs, integrity metadata/verify,
  two-tier locks, build ref files, bare `model:`+`revision:` pin protection,
  `command.cwd`, and `FlagManagerScreen` are implemented and tested.
- **P6 substantially closed:** target manager add/edit/remove, rich active-target
  detail, disconnected run-control gating, structured create/adopt/pin screens,
  manager detail panes, target-named destructive confirms, palette target
  commands, and agent-info entry are implemented and tested.
- **PA7 follow-up closed in this pass:** stdio response priority now also
  coalesces backpressured transient progress frames, and build/model/flag entry
  points are gated against missing target capabilities.

Remaining work should be treated as incremental polish or new validation scope,
not as the original P1-P4 safety/functionality blockers.

---

## ✅ v1 items — CLOSED (verified)

- **v1‑P1 reachable_url for remote** → **DONE.** Controller-side rewrite `_controller_reachable_url` (`tui/app.py:2986`) + `_controller_host_from_ssh_target` (`:321`) rewrites the agent-reported URL to the SSH target host, applied to ready/health/sidecar URLs; pinned by an SSH-target test. **[Opus-verified]**
- **v1‑P2 PA5 UI core** → **DONE.** Header `⊕ target` segment, `TargetManagerScreen`, `t`/`R` keys, named `AGENT_UNREACHABLE`/`VERSION_MISMATCH`/`NOT_INSTALLED` banners, disconnected-launch guard. (Polish remnants → v2‑P6.)
- **v1‑P3 protocol contract** → **DONE.** Integer JSON-RPC codes + `data` key (`transport/rpc_errors.py`), handshake downgrade + `controller_version`, `discover_runs_no_paths` now dispatched, `status`/`unsubscribe` present.
- **v1‑P4 robustness** → **DONE.** Graceful log-rotation resume, exponential reconnect backoff (100 ms→10 s), GPU push `gpu` events.
- **v1 deferred** → **DONE.** systemd user unit, `--idle-timeout`, ControlMaster default.

---

## ✅ Confirmed correct — DO NOT REGRESS (re-verified after +11k lines)

- **Controller holds no process/registry authority.** `grep -nE "current_process|Popen|\.proc\.|stop_sidecar_from_system|signal_sidecar_from_system|killpg|os\.kill|scan_cache_dir|snapshot_download|nvml" tui/app.py cli.py` → **clean**; structural tests assert those attributes are absent from the app. **[Opus-verified]**
- **In-process transport stays wire-safe** — `transport/inprocess.py` round-trips `decode_frame(encode_frame(...))` on params/results **and every event**, now including `job_progress`/`job_done`. No live-object leak in build/model payloads.
- **Verify-before-every-destructive-signal** unchanged agent-side (`sidecar.py` `destructive_signal` → `verify_sidecar_identity`).
- **Daemon security:** Unix socket `0700`/`0600`, `agent.json` identity + stale detection, **SO_PEERCRED**, no network port.
- **Launch handoffs:** build env-overlay at both spawn chokepoints; precedence `executable > build > default > PATH`; model `--revision` + tokenizer; both compose.

---

## P1 — SAFETY: wire the in-use remove guard for builds **and** models **(top priority)** **[Opus-verified]**

**Severity:** High (violates a spec hard-guarantee; data-loss / live-run-breakage footgun).
**Spec:** build §7.7, model §11 — "refuse removal if a **live server** is using the build/model@revision," verified via `discover_active_sidecars` + `verify_sidecar_from_system`.

**Problem (verified by reading the code).** `_remove_build` (`agent/local.py:831-853`) and `_remove_model` (`:888-911`) check **only config-pin protection** (`_configs_pinning_build`/`_configs_pinning_model`). Neither consults sidecars; `engine/build_registry.py` and `engine/model_registry.py` contain **zero** sidecar references. So removing a build/model that a **running server** uses — but that no current config pins (ad-hoc launch, bare `model:` string, or a config not in the dir) — proceeds to `shutil.rmtree(venv)` / `delete_revisions().execute()`. For a live multi-GPU run's venv that can crash the run; for a model it's a data-loss footgun.

**Fix direction (cheap — building block already present).** `discover_active_sidecars` is already imported (`agent/local.py:64`) and used at `:638`. Before remove, enumerate active sidecars on the host, verify identity, and refuse (`resource-in-use`, `reason: "live-run"`, naming the run) if any live run resolves to this build (its `executable`/`build_id`) or this model@revision (the sidecar's `served_model_names`/resolved model+commit). The config-pin guard stays; this adds the live-run guard the spec mandates. Apply the same to a force path: `force` may override a config-pin, but a **live run** should not be removable even with force (stop it first).

**Acceptance.** Tests: (a) a faked live sidecar serving model M@rev → `model remove M` refused with `resource-in-use`; (b) a faked live run whose executable is build B's `bin/vllm` → `build remove B` refused; (c) dead/recycled sidecar → remove allowed. Mirror `test_sidecar`'s identity discipline.

---

## P2 — FUNCTIONAL: implement the real build installer; finish model download **(biggest feature hole)** **[Opus-verified]**

**Severity:** High (the create-from-scratch flows don't work yet).
**Spec:** build §7.1/§7.2, model §8.

**Problem (verified).** `_default_build_job_runner` (`agent/local.py:986+`) returns `feature-unavailable`/"create_build method is not implemented" for **every method except `adopt`** (`:1041, :1048-1049`) — no `uv`/`pip` subprocess exists. You can adopt an existing venv but cannot **install** a build (pip/nightly/commit/git/wheel) through the app. Model download has a real `snapshot_download` path but also a "remote model download is not implemented" branch (`:1149-1152`), and the working path has **no `%` progress, no download FSM, no working cancel, and no automatic `HF_TOKEN` injection** for gated repos.

**Fix direction.**
- **Build installer:** implement the `uv`(preferred)/`pip` install methods as a real streamed subprocess job (build spec §7.1 command shapes); add the `BuildPhase` FSM (`RESOLVING→DOWNLOADING→BUILDING→INSTALLING→VERIFYING→READY/FAILED`) and `BuildErrorKind` classification; stream output through a `LogSink` (see P3) into `job_progress`. uv-detected-with-pip-fallback; disable nightly/commit without uv.
- **Model download:** reconcile the dual path (real `snapshot_download` vs the not-implemented branch); drive a `RESOLVING→DOWNLOADING(%)→VERIFYING→READY/FAILED` FSM; make cancel actually interrupt (→ `partial`, resumable); inject `HF_TOKEN` from env at op time for gated repos (model spec §9 env contribution) rather than requiring it in `cfg.env`.

**Acceptance.** `build add --method pip --spec 'vllm==0.11.0'` actually installs into a venv and reaches `ready` (against a controlled index or a fake-installer in CI + one real run, see P4); `model download` streams `%` and cancel leaves `partial`. Add `test_build_install_fsm` / `test_model_download_fsm` with a stub installer that emits canned progress + failure signatures.

---

## P3 — SECURITY (do with P2): scrub build/model job output before the wire

**Severity:** Medium (latent now; becomes live the moment P2 streams real installer/`hf` output).
**Spec:** agent §8 (scrub agent-side before any event leaves the host).

**Problem.** `job_progress` text is emitted raw, **not** through `LogSink.scrub`/`redaction`. Today's exposure is near-zero (install stubbed; download output not streamed). But P2 will stream `uv pip install --extra-index-url https://USER:TOKEN@…` and `hf` output, which can carry index-URL creds / HF tokens.

**Fix direction.** Route every build/model job's subprocess output through the same `LogSink` scrubbing path the run logs use (secrets = config `api_key`/`HF_TOKEN` + the install index creds) before it becomes a `job_progress` event and before it hits the `0600` install/download log. No bypass.

**Acceptance.** A `test_scrub_before_wire` covering an install job whose output contains a token → the streamed event and the durable log are masked.

---

## P4 — VALIDATION: run it for real **(this is the gate between "code-complete" and "done")**

**Severity:** High for "done," even though the code may look finished.
**Spec:** the "definition of done" in v1's final section.

**Problem.** Everything is exercised **locally/faked** — no real `uv/pip` install, no real `huggingface_hub` download, no real SSH→GPU run. The only "remote" test asserts on the **content of a shell script**, not execution. So 438 green ≠ validated.

**Fix direction (three real runs, ideally one CI lane + manual GPU lane).**
1. A **real build install** on a GPU host (e.g. a small `vllm==X` into a fresh venv) reaching `ready` and launching.
2. A **real model download** (a small gated + a small ungated repo) through the agent.
3. The **P620‑01 → Blackbird end-to-end smoke:** `t` selects `blackbird`, handshake, configs/builds/models list from Blackbird, `l` launches there, header shows `⊕ blackbird ●` + `●READY http://10.25.0.51:18003` (v1‑P1), stop re-verifies identity on Blackbird, laptop sleep/reconnect resumes the stream gap-free, run survives a daemon restart.

**Acceptance.** A documented, repeatable remote smoke (extend `scripts/run_remote_tests.sh`) that *executes* (not greps) against a real GPU host, plus a record of one successful real install + download.

---

## P5 — PA6 registry rigor (sibling-spec fidelity)

**Severity:** Medium.

- **Integrity hashes absent** — no `freeze_sha256`/`executable_sha256`/`verify_output` in build manifests (build §6.2) or model `integrity` block (model §7). Add them; have `verify` recompute and compare.
- **Real verify** — build/model `verify` is **path-existence only**; run `bin/vllm --version` + `python -c "import vllm"` (build §7.3/§7.6) and the model file/commit checks (model §8.4).
- **Locking/refcount** — no `flock` (`builds.lock`/`build.lock`) and no `refs/` refcount dir (build §7.7). Add the two-tier lock + the verified-sidecar refcount (which also feeds P1).
- **ULID ids** — minted by the **registry**, not caller-supplied (build §6.2, model §7).
- **FlagManager** (build-spec offshoot, §9.5) — `FlagManagerScreen` + `F` binding entirely absent. Lower priority than the above.
- Minor: `command.cwd` schema field (build friction #4); pin-protection should also catch bare `model:`+`revision:` configs, not only `model_ref`.

**Acceptance.** Per-area unit tests (integrity round-trip, verify executes the binary, remove honors flock + refs).

---

## P6 — PA5/PA6 UI polish

**Severity:** Medium-Low (functional, but below the spec's UX).

- **TargetManagerScreen:** add `n/e/x` (add/edit/remove targets); detail pane should show agent-version-vs-controller, capabilities (dim unavailable `b`/`m`), GPU summary, active-runs, last-seen; connecting `◐` pulse + success toast; per-target "Switch target: …" palette commands + "Agent info".
- **Disconnected dashboard:** grey last-known state and gate Stop/Kill/Restart on connection (only Load is gated today); read-only log with a "disconnected at …" rule.
- **Create/Adopt/Pin screens:** replace free-form `key=value` text inputs with structured method/param pickers; install **phase banner** during create/download.
- **Manager detail panes:** dedup-aware model size ("2.1 GB unique / 16 GB nominal"), `🔒 in use` build badge, `⇩ used by N configs`, source markers, resolved-command preview sub-pane.
- **Target-named build/model remove confirms** (kill confirm already names the target; build/model removes don't).

**Acceptance.** Smoke tests for the new bindings/flows; visual check against build §9 / model §13 sketches.

---

## Recommended order

**P1 (safety) → P2+P3 (installer + scrub, together) → P4 (validate for real) → P5 (registry rigor) → P6 (UI polish).**
P1 is small and must-fix. P2 is the largest remaining functional build. P4 is what converts "feature-complete" into "done" — do it as soon as P2 makes a real install possible. P5/P6 are fidelity/polish that can land incrementally.

**Done = P1–P4 closed and the P4 remote smoke passing**; P5/P6 are the difference between "works" and "matches the spec's full UX/rigor."
