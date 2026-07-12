# vLLM Agent Architecture — Implementation Review Punch List **v5** (v1 sign-off checklist)

**Supersedes (does not replace):** v1–v4 punch lists. **All of v1, v2, v3, and v4 are CLOSED** (verified across successive reviews).
**Reviewing:** `vllm-agent-architecture-spec-v1.md` (+ build/model sibling specs).
**Review state (2026-06-04, HEAD `5609cdd`):** **580 tests passing, clean tree.** Architecture ~98% • features ~97% • validated ~90% • **overall ~95–96% to a shippable v1.**
**Method:** Sonnet 4.6 reviewers; load-bearing/safety findings independently verified by Opus 4.8 (**[Opus-verified]**).

**Headline:** the hardest item (proving the self-hosted CI lane actually runs) is CLOSED. After topology review, Mac sleep is **not** a v1 controller/agent gate because the TUI controller runs on P620-01, not on the Mac. What remains is doc completeness and one optional UX check; the physical sleep script stays as an optional drill for any future laptop-as-controller topology.

---

## ✅ Closed since v4 (verified) — DO NOT REGRESS

- **Live self-hosted GitHub Actions run → CLOSED. [Opus-verified]** `artifacts/remote-validation/2026-06-04T20-04-41Z-…-remote-validation.md` embeds a real Actions run URL (`…/actions/runs/26976430928`) and build/model IDs of the form `gha-26976430928-1-build` — only producible by the workflow interpolating `${{ github.run_id }}`/`${{ github.run_attempt }}` — plus the runner-local key path `/home/bgconley/.ssh/vllm-loader-remote-validation`. The nightly/self-hosted lane has demonstrably fired green against real Blackwell hardware. *(Ultimate confirmation: the embedded Actions URL.)*
- **Gated-model auth → CLOSED. [Opus-verified]** `scripts/gated_model_auth_check.py` forces `HF_TOKEN=""` and asserts `error_kind=gated-auth` against a real gated repo; `…2026-06-04T20-34-19Z…` artifact line 111: `GATED_MODEL_AUTH_OK repo_id=meta-llama/Llama-2-7b-hf … error_kind=gated-auth`.
- **url-download UX → CLOSED.** `model_manager.py` shows `download: launch-time-only` + size + `🔒` gated marker (no dead affordance).
- **Real-model resume, real build install, real Qwen smoke (v4) → CLOSED.** Proven at `b085610` (== HEAD functionally; no engine code changed since).

**Foundational invariants (re-verified this review — [Opus-verified]):** crown-jewel clean (`app.py`/`cli.py` hold no process/registry authority); live-run remove guard intact (force can't bypass); installers real subprocess; scrub-before-wire; wire-safety round-trip. **No agent/engine/transport changes since v4** (only `model_manager.py` +14). Add a regression test before refactoring near any of these.

---

## V5-P1 — Reframe laptop-sleep reconnect as optional topology-specific validation **[Opus-verified — script read, artifacts grepped]**

**Severity:** Low for the current lab topology; Medium only if the controller itself is a sleeping laptop.
**Carries from:** the v4 "laptop sleep not exercised" note.

**State.** `scripts/laptop_sleep_reconnect_check.py` is **well-designed and sound by inspection**: requires a real *detached* config (rejects `fake/model`), waits for a log event carrying a `{log_inode, byte_offset}` cursor, **operator-gates a real pause via `input()`** ("sleep this controller, wake it, press Enter"), then reconnects, `discover_runs`, reattaches with `resume_from=cursor`, and asserts the pre-sleep line is **not** re-delivered (gap-free resume). On success it prints `LAPTOP_SLEEP_RECONNECT_OK resume_inode=… resume_offset=…`.

**Topology correction.** In the intended deployment, the user remotes into P620-01 and runs the TUI/controller there. Blackbird and P620-01 are the target agents. If the Mac sleeps while it is only an SSH terminal into P620, that tests the outer SSH/operator session, not the app's controller-to-agent boundary. The architecture-relevant restart/reconnect surfaces are already represented by P620-to-Blackbird detached discovery, reattach, real-model resume, and daemon-restart validation artifacts.

**Why it can't be CI-automated.** It is inherently a manual operator action when the controller really is a laptop — a human must physically sleep/wake that controller and press Enter. So "done" for a laptop-controller deployment means running the drill once and committing the artifact.

**Fix direction.** Do **not** require a Mac sleep artifact for v1. Keep the drill documented in `docs/gpu-workflow.md` for future laptop-as-controller use, and use `tmux`/`screen`/`systemd-run` guidance for the Mac-to-P620 operator SSH session case.

**Acceptance.** Current v1 topology is accepted without a Mac sleep artifact. If the controller is later moved to a laptop, acceptance becomes a dated artifact containing a real `LAPTOP_SLEEP_RECONNECT_OK` line from an actual controller sleep/wake cycle.

---

## V5-P2 — Doc completeness **[Verified and addressed in this patch]**

**Severity:** Medium (doc-only; a few focused edits).

The v4 doc gaps were open after the README/gpu-workflow edits and are now
addressed in `README.md`, `docs/configuration.md`, and `docs/agent-rpc.md`:

- **Config-level `target:` field — documented.** `schema.py:120` defines `target: str | None` on `ModelConfig`. This is an optional home-target label; the active CLI `--target` or TUI target still decides which agent receives a request.
- **`exposure: lan/public` — documented.** `schema.py:21-24` + the validator at `:73-81` enforce that non-loopback/wildcard binds require `lan`/`public`; docs now describe values, semantics, and the network-exposure warning.
- **Daemon lifecycle — documented.** `vllm-loader agent start/stop/status/restart`, auto-spawn in `agent connect`, default socket/identity paths, foreground run, and the packaged systemd user unit are now covered.
- **`agent-rpc.md` method list — refreshed.** The live capability list now includes the previously missing dispatched methods: `ping`, `prepare_launch`, `update_config_flags`, `repair_build`, `run_build`, `refresh_models`, `sample_gpus`, plus the related build/model/detail aliases.

**Fix direction.** Complete.

**Acceptance.** A new operator can understand `target`/`exposure`, start/manage the daemon, and enumerate every RPC method from the docs alone.

---

## V5-P3 — CLI/TUI active `uv` pre-check (minor UX) **[Opus-verified]**

**Severity:** Low (correctness already handled agent-side).

The **agent already actively rejects** nightly/commit without `uv` (`_find_uv_executable` = `shutil.which("uv")`, `local.py:3136`; raises `feature-unavailable` at `:2827-2863`) — so the job fails cleanly and immediately, not silently. But the CLI (`cli.py:238`) and create-build screen (`create_build.py:71`) only show a **static hint**, so the user experiences an async job failure rather than a pre-dispatch rejection.

**Fix direction.** Optionally add a local/preflight `uv` availability check in the CLI/TUI create-build flow for nightly/commit so the rejection is immediate. Low value; skip if time-constrained.

**Acceptance.** Selecting nightly/commit on a uv-less target is flagged before the job starts.

---

## Definition of done (v1 sign-off)

**v1 = V5-P2 (doc completeness), with V5-P1 documented as optional for laptop-controller deployments.** V5-P3 is optional polish.

At that point the architecture is built, proven to run green in self-hosted CI against real Blackwell hardware, validated across real build install / model download / gated-auth / resume / daemon-restart, and fully documented. **The remaining required work is documentation, not engine work.**

> **Note on `url`-source model download (intentional, CLOSED):** `local.py:1797-1812` returns `ok` with `cache_state: remote_only` for `source == "url"` — download is launch-time-only by design, and the TUI now surfaces that. Not a stub; do not "fix."
