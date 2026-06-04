# vLLM Loader — Full-Repo Review & Findings (cross-spec) **v1**

**Scope:** the entire `lab-tui` / `vllm_loader` codebase against **all four specs** — canonical TUI loader v2, build management v1, model management v1, agent/controller architecture v1 — plus a full code-quality pass, a test/CI/validation audit, and a UI/Figma alignment check.
**Method:** 7 parallel **Sonnet 4.6** reviewers (one per spec domain + tests/CI + code-quality + UI), then **Opus 4.8** independently re-verified every load-bearing/safety claim against the code. Items I personally read and confirmed are tagged **[Opus-verified]**; items I personally re-rated are tagged **[Opus-corrected]**.
**State at review (2026-06-04, HEAD `c20d6c1`):** **580 tests passing (independently re-run: `580 passed in 100.32s`), ruff clean, tree clean** (only `.wolf/*` dirty).
**Relationship to prior punchlists:** extends the agent-architecture punchlist series (v2–v5). Those remain accurate for what they reviewed; this document is broader (all specs) and surfaces a set of real, previously-unrecorded gaps plus **corrects two agent over-ratings**.

---

## 0. Headline verdict

The repository is **substantial, coherent, and largely complete** — ~18,800 LOC implementing four composed specs, with the hard safety invariants genuinely intact and a **real** self-hosted CI lane proven on Blackwell hardware. The `feature-unavailable` sites that look like stubs are, on inspection, **legitimate catch-all guards** with real main paths above them (real `uv`/`pip` subprocess installers; real `huggingface_hub.snapshot_download`).

However, the v5 framing of **"~95–96% to a shippable v1"** is **mildly optimistic**. Independent scrutiny found a cluster of **Medium** correctness/fidelity gaps that a true v1 sign-off should close — none catastrophic, none large missing features, but more than "docs only." Realistic completeness:

| Domain | Genuine completeness | Confidence |
|---|---|---|
| Canonical core engine (v2) | ~88% | High |
| Build management (v1) | ~75% | High |
| Model management (v1) | ~80% | High |
| Agent/controller architecture (v1) | ~85% | High |
| **Overall, to a polished v1** | **~83–86%** | High |

The remaining work is **correctness/polish + test-organization + a few spec-fidelity gaps**, not architecture. The crown-jewel design holds.

---

## 1. Confirmed solid — DO NOT REGRESS (re-verified this review)

- **Crown jewel [Opus-verified].** `grep -nE "current_process|Popen|os\.kill|killpg|getpgid|scan_cache_dir|snapshot_download|delete_revisions|pynvml|openpty" tui/app.py cli.py` → **clean**. The controller holds no process/registry authority; all lifecycle goes through `TargetClient.call()`.
- **Live-run remove guard, builds AND models; force cannot bypass it [Opus-verified].** `agent/local.py` `_remove_build`/`_remove_model` run the live-run check **unconditionally**; `force` only overrides the config-pin guard. Pinned test: `test_agent_refuses_to_force_remove_model_used_by_live_run` asserts `resource-in-use` even with `force=True`.
- **Real installers, not stubs [Opus-verified].** `create_build` → `_run_pip_build_job` → `_build_subprocess_exec` (`asyncio.create_subprocess_exec`, line-streamed) for all five methods (pip/nightly/commit/git/wheel); nightly/commit correctly hard-require `uv`; git/pip-pinned/wheel fall back to `python -m venv`+pip. The two `create_build method is not implemented` returns (`local.py:1686`, `:2917`) fire **only** for unrecognized method strings.
- **Real model downloads, not stubs [Opus-verified].** `agent/local.py` `hf_repo` source → real `snapshot_download`; `url` source is **intentionally** launch-time-only (`remote_only`, surfaced in the TUI); `local.py:1911-1914` and `model_registry.py:220,:750` are correct catch-all/optional-dependency guards.
- **Scrub-before-wire, unconditional [Opus-verified].** `LogSink.scrub` runs before the durable write *and* the event emit; job output scrubbed via `_scrub_job_payload`; no raw-log RPC. Corroborated for run + build + model jobs by tests.
- **Sidecar identity discipline.** Full 5-check `verify_sidecar_identity`; `stop_sidecar_from_system` re-verifies before **each** SIGINT→SIGTERM→SIGKILL step (correct anti-PID-reuse) **[Opus-verified]**.
- **Canonical engine load-bearing paths.** PTY launch (close-slave-in-parent, EIO-as-EOF, fixed 200-col width), bounded 1 MiB partial-line LogSink buffer, supervisor drain-always-even-on-write-failure, version-aware flag-emission rule, GPU NVML+nvidia-smi fallback, phase FSM with health-driven READY.
- **Protocol/daemon.** NDJSON framing with 2 MiB cap + response-priority writer; controller-minted-id idempotency; warm `seq`-indexed event buffer with offset-fallback resume; daemon Unix socket `0700` dir / `0600` socket + `SO_PEERCRED`; SSH transport with `BatchMode=yes`/`ServerAliveInterval`/`ControlMaster`.
- **Catalog/registry.** Real `scan_cache_dir()` merge (scan ∪ local ∪ pins, deduped by `(repo_id, commit_sha)`); pin via real `HfApi().model_info().sha`; dedup-aware GC via `delete_revisions(...).expected_freed_size`.
- **CI + artifacts are genuine [Opus-verified].** `.github/workflows/remote-validation.yml` targets a self-hosted runner, daily cron + manual `fast|full`, concurrency guard, build label `gha-{run_id}-{run_attempt}`. Artifacts embed a real Actions run URL, NVML UUIDs, multi-GB wheel sizes, random ports, real inode numbers, ULIDs — **authentic**, not fabricated.

Add a regression test before refactoring near any of the above.

---

## 2. Scrutiny corrections (agent findings I re-rated) — for accuracy

Two Sonnet findings were rated **High** but are **not** high-severity on inspection:

- **"pip `--python` flag is invalid → pip fallback broken" → DOWNGRADED to Low / runtime-unverified [Opus-corrected].** `_pip_bootstrap_argv` (`local.py:3110`) emits `sys.executable -m pip --python <venv>/bin/python install pip`. `pip --python <target>` has been **valid since pip 23.1 (2023)** and its runner can bootstrap into a `--without-pip` venv. So this is plausibly correct on modern pip. Residual risk: only if the agent host's pip is <23.1, and the uv-less fallback path appears **unexercised** by real validation (the artifacts used `uv`). Worth a one-time real test on a uv-less host; not a confirmed break.
- **"sidecar re-verify TOCTOU skips SIGKILL" → NOT A BUG [Opus-corrected].** `stop_sidecar_from_system` re-verifying identity before each escalation and **aborting on a recycled PID is the anti-PID-reuse guard working as designed** — you must *not* SIGKILL a recycled PID. It collapses into finding **P1** below (wrong error code), plus a Low `PermissionError`-not-caught hardening nit (**Q5**).

Also re-rated: the `redaction.py` greedy `\S+` "High" → **cosmetic/Low** (it is the spec-mandated `sk-\S+`/`hf_` form and *over*-masks, which is safe — never leaks less). The health `HF_AUTH` finding "High" → **Low-Med** (detail text is correct; blocking READY on a 401 is partly intentional per recorded design).

---

## 3. Punchlist — Medium (close before v1 sign-off)

### P1 — Recycled-PID refusal returns `-32000`, not the spec's `-32002` **[Opus-verified]**
`agent/local.py` `_stop`/`_kill` → `_request_stop_signal`/`_request_kill_signal` → `stop_sidecar_from_system`, which raises `TrackedProcessMismatch` (a plain `RuntimeError`). The stdio dispatcher (`agent/stdio.py:102-115`) maps `TargetCallError` → named code, else `Exception` → `internal-error` (`-32000`). `TrackedProcessMismatch` is not a `TargetCallError`, so an identity-mismatch refusal surfaces as `-32000`. The only `identity-verification-failed` (`-32002`) raise (`local.py:2172`) is in the unrelated log-inode resume path. **Safety holds** (the signal is still refused), but FR-A4/§6.3/§6.5's named-error contract is broken and the controller cannot distinguish a security refusal from a crash. *No test asserts the `-32002` wire code (the missing `test_agent_authority` is why this slipped through).* **Fix:** catch `TrackedProcessMismatch` in `_stop`/`_kill` (or the signal helpers) and raise `TargetCallError("identity-verification-failed", …)`.

### P2 — NDJSON receive path silently drops frames between 64 KB and 2 MiB **[Opus-verified]**
Readers construct `asyncio.StreamReader()` with **no `limit=`** (default 64 KiB): `agent/stdio.py:269`, `transport/socket.py` (readline at :212), `transport/subprocess.py` (:187). The framing layer advertises a **2 MiB** cap and the LogSink truncates committed lines at **1 MiB** — so a large committed log line (or a big `list_*`/`preview` result) produces a frame >64 KiB. `readline()` then returns it in chunks, `decode_frame` raises `NdjsonFrameError`, and `serve_agent_stream` does `continue` → **the frame is silently lost**. **Fix:** construct readers with `limit=MAX_FRAME_BYTES + 1` (and pass `limit=` to `open_unix_connection`/`create_subprocess_exec`/`start_unix_server`).

### P3 — Model download has no `downloads/<entry_id>.log` (0600) and bypasses `LogSink`
Model §6.1/§15 require a `0600` per-entry download log (download output can carry tokened HF/index URLs). The model-download path streams to the in-memory event callback only — no `downloads/` dir, no file, no `chmod(0o600)`. **Mitigation present:** download **events are scrubbed** (HF token in `job_secrets`), so this is a missing-durable-artifact / defense-in-depth gap, not a live wire leak. Build installs already do this correctly (`install.log`, 0600). **Fix:** route model-download output through `LogSink` to `models/downloads/<entry_id>.log` (0600), matching builds.

### P4 — Model layer does not inject `HF_TOKEN` at spawn for gated repos
Model §9's handoff `env_contribution` says the registry contributes `HF_TOKEN` from the runtime env for gated models. The implementation only **validates** its presence pre-launch (`_validate_model_handoff_prelaunch`) and relies on the user having put `HF_TOKEN` in `cfg.env`; it does not inject `os.environ["HF_TOKEN"]` into the spawn env (attached or detached supervisor payload). **Fails loud** (the gated+no-token pre-launch guard blocks), so not silent — but a divergence from the handoff contract. **Fix:** add `HF_TOKEN` to the model handoff env contribution at both spawn chokepoints.

### P5 — `--deep` model verify is a no-op for HF-cache entries
`model verify --deep` plumbs `deep=True` but `_verify_hf_model_status` only re-scans `scan_cache_dir()` and checks file/commit presence — no content-addressed blob hashing (model §8/FR-M4). Only `local_path` entries get real deep verification. **Fix:** recompute blob hashes for HF entries on `--deep`, or remove the flag for HF entries and say so.

### P6 — No pre-launch build integrity re-check (FR-B11)
`engine/preflight.py:check_launch_preflight` checks model-path/port/world-size but **not** build `status ∈ {ready, adopted}` or an executable-hash match. A `broken` build is only gated at resolve time, not re-checked before each launch. **Fix:** add a cheap build re-verify (status + `executable_sha256`) to preflight.

### P7 — `BuildErrorKind` classification misses the GPU/CUDA failures the feature exists for
`local.py` install-error classification covers disk-full/auth/network/build-failed but **not** `TORCH_CUDA_MISMATCH`, `DRIVER_TOO_OLD`, `ARCH_MISMATCH` (`no kernel image` / `undefined symbol cutlass_moe_mm_sm100`), `COMPILE_OOM` (build §7.2/Appendix D) — exactly the NVFP4/Blackwell signatures the build feature was built to handle. They collapse to generic `build-failed`. **Fix:** add the Appendix-D patterns.

### P8 — No stale-`creating` startup sweep (build §6.2)
A crash mid-install leaves a build stuck in `status=creating` forever (never launchable, never cleaned). The spec requires a startup sweep demoting stale `creating` → `failed`. Not found. **Fix:** add the sweep on agent startup.

### P9 — Registry RPC handlers run `fcntl.flock` on the asyncio event loop
`agent/stdio.py:98` calls `agent.handle(...)` synchronously on the loop; the sync registry handlers (`_list_builds`/`_remove_build`/`_verify_build`/`_list_models`/`_pin_model`/`_remove_model`/…) take `fcntl.flock(LOCK_EX)` + do file IO inline. GPU sampling is correctly offloaded via `to_thread`; registry ops are not. **Single-user impact: low** (short critical sections); **multi-controller + long install holding the lock: medium** (stalls pings/events). **Fix:** wrap blocking registry handlers in `asyncio.to_thread`.

---

## 4. Punchlist — Low (polish / hardening / fidelity)

- **Q1** `health.py:57-67` classifies any `/v1/models` 401 as `error_kind=HF_AUTH` (misleading "set HF_TOKEN" guidance when the real issue is `VLLM_API_KEY`), and `probe_loop:112-114` `return`s on any `error_kind`, disabling DEGRADED recovery after a 401 blip. Detail text is correct and 401-blocks-READY is partly intentional. Consider a distinct `API_KEY_AUTH` kind and not terminating the probe loop on a post-READY auth blip. **[Opus-verified]**
- **Q2** Adopt-build skips the version-agreement cross-check (FR-B10): `_build_verify_output` confirms `vllm --version` and `import vllm` each succeed but doesn't compare them (the create path does, `local.py:2533-2542`).
- **Q3** Run-artifacts dir created without `0o700` (`process_manager.py:126`); individual sidecar/log/exit files are `0600`, but the dir (and any future plain-`open()` file in it) is `0755`. The socket dir *is* `0700`. Harden the runs dir.
- **Q4** Config / build-manifest temp files written via `Path.write_text` (umask → 0644) before atomic rename; a config may contain `api_key`. The detached secret payload path correctly uses `os.open(..., 0o600)` — apply the same to these temp writes.
- **Q5** `engine/sidecar.py:_signal_process_group` catches only `ProcessLookupError`, not `PermissionError` (signaling a recycled PID now owned by another user — a sub-millisecond window after a passing verify). `process_manager._kill_group` handles this; mirror it.
- **Q6** `redaction.py:8` `TOKEN_RE = \b(?:sk-|hf_)\S+` greedily consumes trailing `"`/`}`/`,` (cosmetic; spec-mandated `sk-\S+`; **over**-masks, so safe). Optionally tighten to `[^\s"'&;,\]})]+`.
- **Q7** UI fidelity: `ModelManagerScreen` lacks the `Enter` select-for-active-config binding (model §13); `BuildManagerScreen` exposes `r Repair` in-modal instead of the spec's `F Flags` (F is reachable top-level); `HelpScreen` doesn't document `b`/`m`/`F`. Header merges `▣build`+`M model` into one 32-wide widget (content/order correct; may overflow on long names).
- **Q8** Build `remove` of the **active** default is **refused** (`resource-in-use`) rather than auto-repointing/clearing the default (build §9.6).
- **Q9** `feature-unavailable` maps to `-32011`; spec §6.3 said unadvertised-capability → `-32601` (defensible split: `-32601` for true method-not-found, `-32011` for capability-gated). `subscribe` requires `run_ids` (spec allows `all?`). `health` RPC aliases to `probe_until_ready` (loop) rather than a single-shot snapshot.
- **Q10** Ad-hoc strings instead of formal enums (`BuildPhase`/`BuildErrorKind`/`DownloadPhase`/`ModelErrorKind`/`CacheState`); sidecar carries model identity via the generic `config_snapshot` dict rather than typed `model_ref`/`revision`/`repo_id` fields — works, but a malformed/absent snapshot could weaken model-in-use detection (false-negative → GC of a live model's weights). Consider typed fields for the safety-critical match.
- **Q11** `packaging/systemd/vllm-loader-agent.service` uses `After=network.target` (inappropriate for a user-socket daemon); auto-spawn uses a single `setsid` (`start_new_session=True`) rather than the spec's double-fork (generally fine on Linux).
- **Q12** `lru_cache` profile staleness (build friction #1) only partially mitigated (bypassed when the manifest carries a version); no `clear_profile_caches()` / mtime-keying for the bare-PATH case.

---

## 5. UI / Figma alignment

**Method:** captured the live Textual screens headless via `App.run_test()` + `save_screenshot()` (10 SVGs), viewed the committed PNG renders, and read the new-screen SVG text directly.

- **Structural alignment with all four specs' UI sections: strong.** Header order `app-title · ⊕target · ▣build · M model · ●STATUS · url · clock` is correct; connection-dot/status vocab and icons match; `HORIZONTAL_BREAKPOINTS = [(0,"-compact"),(60,"-narrow"),(100,"-wide")]` exactly, with sidebar→overlay <100, GPU dropped <60, log always visible. All 14 screens exist and render (Build/Model/Target managers as two-pane list+detail; FlagManager with real `Input`+live preview; Create/Adopt/Download/Pin form screens).
- **Render quality (committed PNGs): clean, polished, monochrome-friendly** — matches the canonical §8 dashboard and the terminal-cell aesthetic.
- **Figma comparison — honest limitation.** The referenced Figma file is private/auth-gated; this environment exposes only Figma *auth* stubs (no node-read tool) and `WebFetch` cannot read authenticated URLs. Per the project's own recorded decisions (`.wolf/cerebrum.md`), **the Figma frames were built *from* the canonical spec** (and explicitly *not* mirrored from app screenshots). Figma and the app are therefore **siblings of the same spec**, so the rigorous alignment check is app↔spec (done) + render verification (done) — a private-Figma pixel diff would require an interactive OAuth flow unavailable to this session and would not add an independent source of truth. **If a direct Figma diff is still wanted, complete `figma authenticate` interactively and re-run the comparison against node `22-2`.**
- **UI fidelity nits:** see **Q7**. Committed screenshots in `artifacts/tui-screenshots/01–06` are **stale** (pre-agent/build/model footer) and should be regenerated.

---

## 6. Tests / CI / validation

- **580 passed / 0 failed / 0 skipped, 100.32s** — independently re-run, matches the claim exactly. **Ruff clean.** Test *quality* is good-to-very-good (exact value assertions on argv/env, phase histories, sidecar identity, frame ordering), not hollow smoke.
- **Coverage organization gap.** Many build/model spec-named unit areas are **consolidated into `test_tui_smoke.py` / `test_agent_client.py` integration tests** rather than dedicated files. Behavior is mostly covered (GC safety, scrub-before-wire, authority boundary **are** tested), but a few areas are genuinely thin/absent: **`test_agent_authority` (the `-32002` contract — see P1), `test_profile_cache_invalidation`, `test_compose_builds_models`, `test_build_locks_refs`/`test_build_resolver` as isolated units.** Adding `test_agent_authority` would have caught P1.
- **CI + artifacts authentic** (see §1). The `LAPTOP_SLEEP_RECONNECT_OK` drill is documented-but-unexercised — **correctly** accepted by v5 as optional for the current P620-controller topology.

---

## 7. Definition-of-done delta vs punchlist v5

v5 declared the remaining work "documentation, not engine." This review's correction: **documentation is essentially done, but there is a focused set of Medium engine/correctness items (P1–P9) that a true v1 sign-off should close** — most importantly **P1** (named identity-error contract + its missing test) and **P2** (large-frame loss), then the model/build fidelity items (P3–P8). None are architectural; all are bounded fixes. With P1–P9 closed and a few dedicated regression tests added (`test_agent_authority`, large-frame round-trip, model-download-log, build-integrity-preflight), the **~83–86%** estimate rises to a defensible shippable v1.

> **No code was modified as part of this review** (per instruction). This document and its punchlist are the only outputs written to the repo.
