# Cerebrum

> OpenWolf's learning memory. Updated automatically as the AI learns from interactions.
> Do not edit manually unless correcting an error.
> Last updated: 2026-06-02

## User Preferences

<!-- How the user likes things done. Code style, tools, patterns, communication. -->

- User clarified that code is generated on the Mac and rsynced to GPU boxes for real vLLM/GPU tests; keep local validation no-GPU/no-vLLM by default and provide explicit rsync/remote-test workflow.
- User explicitly rejected mirroring existing TUI screenshots for Figma screen work; build Figma screens from the canonical app spec and implementation plan only.
- On Figma workflow/form screens the user wants explicit explanatory context: every field/selection needs a helper saying what it does AND where the value comes from, and method/mode choices (e.g. build Method=nightly) need a plain-language description of what will happen (what gets downloaded/built, which option to pick and why, what it does NOT do). Be target-aware where possible (e.g. recommend the CUDA wheel channel that matches the selected target's GPU). Applies to ALL remaining redesign screens. (Confirmed 2026-06-08 reviewing Create Build 48:2; approved Target Manager 44:2 as-is.)
- NEVER put the user's unique environment details into Figma UI mockups (they may be shared). No real hostnames (e.g. `blackbird`), usernames (`bgconley`), IPs (`10.25.0.51`), home paths (`/home/bgconley/...`), specific card models (`rp6000`/RTX PRO 6000), or real run_ids. Use generic placeholders: target name `gpu-node`, host `user@gpu-host`, paths `/home/user/...`. For ANY Blackwell GPU/card, label it `Blackwell sm_120`. (Correction 2026-06-08 — applies to all current + future mock screens.)
- Use STRICT red-green TDD (the `superpowers:test-driven-development` skill) for ALL code — widgets, screen refactors, bug fixes: write the failing test FIRST, run it and watch it fail for the RIGHT reason (feature missing, not a typo), then write minimal code to pass (GREEN), then refactor. NEVER write production code test-alongside or test-after — if you do, delete it and redo red-green. (Correction 2026-06-08 — user caught the `Field` widget written test-alongside; it was redone via a real red→green cycle.)

## Key Learnings

- **Project:** lab-tui
- Canonical v2 requires detached mode to survive TUI/CLI exit; supervisor must drain child pipes live and write scrubbed durable logs, sidecar, and manifest for reattach.
- Script executables launched through a shebang may briefly report `/usr/bin/env`; sidecar identity should record the actual settled process cmdline/executable before verification.
- Health probing must continue after READY; the TUI should consume loop events so health failures move to DEGRADED and later 200 responses recover to READY.
- Health checks must treat `/v1/models` connection failures like `/health` connection failures because shutdown can happen after `/health` returns 200 but before the model probe completes.
- `/health` 200 proves liveness; malformed `/v1/models` JSON should not crash the probe, and should return READY with empty models plus an invalid-JSON detail.
- `/v1/models` payload shape is advisory too; wrong JSON shapes should return READY with empty models plus an unexpected-shape detail rather than raising during model extraction.
- `/v1/models` 401 is not advisory; even if no API key is configured locally, classify it as `HF_AUTH` so remote smoke does not claim READY for an auth-blocked model endpoint.
- Health probing should preserve explicit `server.probe_host`, preserve loopback bind hosts, and otherwise probe `127.0.0.1` for non-loopback bind addresses per the canonical spec.
- TUI modal screens are lightweight Textual `Screen` subclasses under `src/vllm_loader/tui/screens/`; smoke tests inspect `app.screen.id` and simple screen attributes such as `summary`.
- Textual 8.2.7 app command palette commands are exposed by overriding `get_system_commands()` and yielding `SystemCommand` instances; tests can inspect that generator directly without opening the palette UI.
- Textual message dataclasses should subclass the local `LoaderMessage` base in `src/vllm_loader/messages.py`; it calls `Message.__post_init__()` directly so Textual's slots initialize without recursion.
- VllmLoaderApp consumes canonical messages through `on_*` handlers; attached log, health, and GPU workers should post `from_log_record`, `HealthChanged`, `GpuStatsUpdated`, and `GpuStatsUnavailable` rather than mutating widgets directly.
- Reattached detached runs should be controlled through sidecar helpers that re-verify identity immediately before each signal, then signal the recorded process group.
- Reattached Stop/Kill must catch sidecar signal failures in the TUI; identity mismatches should render an Unable to stop/kill refusal and keep the attachment state unchanged.
- Sidecar executable identity is weaker than an exact live command-line match after PID/create_time/PGID have matched; macOS shebang launches can drift between framework and Homebrew Python executable paths.
- LogSink's bounded partial-line guard must loop until pending text is at or below the cap; a single huge read can otherwise leave an over-limit tail that is committed whole on close.
- TUI Load must honor `launch.mode`; detached configs should call `start_detached`, then immediately reattach to the sidecar so the same log tail, health probe, and Stop/Kill paths are used.
- Detached TUI control has two different meanings: Stop/Kill signal the verified detached process group, while Detach only cancels local tail/health workers and leaves the server running.
- TUI READY state keeps explicit `ready_url` and `/v1/models` served model names so the status strip can show `READY <url> as <model>`.
- Command-builder warnings, especially non-local bind security warnings, should be promoted into the TUI banner/log so `--api-key` scope caveats are visible during launch.
- Phase timeline elapsed tests use an injected app `clock` for deterministic headless timing; `_set_phase` is responsible for accumulating elapsed time and rendering the existing `#phases` panel.
- Detached reattach must tail from the exact byte offset read by `_load_scrubbed_log_file`; starting the tail from the file's current size can skip lines written between initial load and worker start.
- TUI command palette tests for dynamic detached sidecar commands should poll briefly for the expected `SystemCommand`; a one-shot snapshot can flake while process identity settles.
- FR-22 command palette coverage must include navigation and exit actions too: `Scroll logs to top`, `Scroll logs to bottom`, and `Quit app`, not only launch/control/log-filter commands.
- GPU panel refresh uses injectable `gpu_sampler` and `gpu_interval_seconds` on `VllmLoaderApp`, then runs a non-fatal Textual worker (`exit_on_error=False`) after the initial mount sample.
- TUI GPU sampling must call the sampler via `asyncio.to_thread`; only `_render_gpu_panel` should mutate UI state on the Textual loop.
- Detached reattach health probing is an optional monitor too; schedule `reattach-health` with `exit_on_error=False` so Textual worker failures do not crash the app.
- Optional monitor worker errors should be surfaced through `on_worker_state_changed`; normalize current GPU worker groups to `gpu` and notify with warning severity.
- GPU sampler exceptions should be converted to an unavailable `GpuPollResult` with detail before rendering, so the panel explains why stats are unavailable instead of staying generic.
- NVML GPU samples should best-effort populate `mig_instance_id` from GPU instance and compute instance IDs; the TUI panel already renders this as `MIG ...` when present.
- TUI classified log errors should render `fsm.error_kind` and `fsm.error_excerpt` into the banner with `ErrorKind`-specific suggestions, not only transition the phase to ERROR.
- Detached tailed logs use their own `_tail_detached_log` path; when a tailed committed line classifies an error, render the same named `ErrorBanner` as attached `handle_log_record`.
- Reattach's initial detached log load uses `_load_scrubbed_log_file`; it also needs to render the named error banner after replaying existing committed lines because errors may already be present before the tail worker starts.
- Reattach preflight should treat stale or malformed sidecar/manifest artifacts as UI errors, not exceptions; verify, parse sidecar, and load manifest inside the guarded path before mutating attachment state.
- TUI config selection now exposes `selected_config_preview`, generated by `build_command`, inside the config sidebar so masked resolved command/env is inspectable before launch.
- Log pause/resume must update the underlying `RichLog.auto_scroll`; changing only app state does not actually stop autoscroll.
- Health probe events with `error_kind` are already classified; the TUI should route them through `PhaseFSM.health_error` so remote auth/timeouts render named banners and guidance.
- Textual `RichLog` supports `max_lines`; pair it with bounded app-side `log_lines`/`log_records`/search buffers so NFR-2 memory limits apply to both the widget and shadow state.
- Textual 8.2.7 enables debug/devtools through the `TEXTUAL` feature env (`debug,devtools`), not an `App.run(devtools=...)` argument; set it before constructing `VllmLoaderApp`.
- TUI self-observability is a separate JSONL debug stream (`debug_log_path`) with structured events such as `app.mounted`, `log.committed`, and `phase.changed`.
- Generic child exits before READY should keep the last unclassified committed log line in `PhaseFSM.error_excerpt`, then render the normal `CRASHED` banner after `read_loop()` returns.
- Textual 8.2.7 responsive breakpoints are minimum-width classes; for lab-tui use `HORIZONTAL_BREAKPOINTS = [(0, "-compact"), (60, "-narrow"), (100, "-wide")]` and an explicit resize handler so tests can assert sidebar/GPU/log display state.
- FR-24 responsive layout is Mac-safe to verify with Textual `run_test`: wide keeps sidebar/GPU/log displayed, sub-100 hides the sidebar, sub-60 also hides the GPU panel, and the log widget always remains displayed.
- FR-12 search highlighting must be manual Rich `Text` spans because `RichLog(markup=False, highlight=False)` is required for raw log safety; layer `Text.stylize(...)` search matches over the severity style before writing to `RichLog`.
- FR-12/FR-22 search and filter keybindings should open `LogPromptScreen` and apply the submitted text through `apply_log_search`/`apply_log_filter`; reapplying stored empty state makes the action reachable but unusable.
- The `Copy server URL` command palette action should call Textual's `copy_to_clipboard`; `last_copied_url` is only a testable record of what was copied.
- Wrap toggles are state changes too; after updating `RichLog.wrap` and visible chrome, emit a `Wrap enabled`/`Wrap disabled` toast just like pause/search/filter controls.
- FR-10 transient carriage-return records now drive a real Textual `ProgressBar` at `#progress` plus a separate `#progress-text` label; transient progress must not be appended to committed `log_lines` or durable logs.
- ConfigPickerScreen should include the selected config's masked resolved-command preview from `build_command`, not just the valid/invalid config list.
- ConfigPickerScreen should also be a fuzzy list: type into the filter input, reset selection to the first filtered match, preview that match, and select it on Enter.
- Status strip text should include a monochrome-friendly phase icon and the `#status` widget should carry `status--*` classes; loading phases also carry `status--pulse` for TCSS styling.
- Missing launch executables are represented as `ErrorKind.COMMAND_NOT_FOUND` in the TUI, with the spec phrase "install vLLM or set command.entrypoint: module" and the missing command path as the excerpt.
- CLI missing launch executables should print the same "Command not found" plus "install vLLM or set command.entrypoint: module" guidance without a traceback.
- Quit confirmation for an attached running process is a stop-then-exit flow; Cancel is the path that keeps the TUI running.
- Detached launch preflights the child executable before creating run artifacts or starting the supervisor, so missing binaries render `COMMAND_NOT_FOUND` in the TUI instead of a generic "supervisor exited before writing sidecar" runtime error.
- NFR-2 UI log batching uses a short Textual `set_timer()` flush: app-side log/search/filter/debug state updates immediately, while visible `RichLog.write()` calls are coalesced; filter/search refreshes clear stale pending writes and render synchronously.
- Detached supervisor durable log rotation is driven by `log_rotate_bytes` in the payload, opens each new active log at `0600`, and atomically rewrites the run manifest so reattach verifies the current active log inode.
- Detached supervisor robustness includes failures before the drain thread starts: if the durable log cannot be opened, use a drain-only sink; if sidecar/manifest writes fail, keep draining and let the caller surface missing reattach artifacts.
- FR-17 readiness timeout details preserve the last not-ready health cause: connection/refusal stays "still loading or not bound yet", while non-200 health/model responses become "bound but unhealthy: ...".
- FR-21 local model paths are preflighted in the TUI before launch: path-like model refs that do not exist set `MODEL_NOT_FOUND` with the resolved local path and do not spawn the child process.
- `vllm.require_flags` is the profile system's hard pre-launch gate; TUI launch must catch `VllmProfileError`, render `CONFIG_INVALID`, and return before attached/detached child startup.
- TUI launch should preflight `tensor_parallel_size * pipeline_parallel_size` against explicit numeric/UUID `CUDA_VISIBLE_DEVICES`; if the world size exceeds visible GPUs, render `TP_MISMATCH` before spawning.
- TUI launch should preflight `server.host`/`server.port` with a reusable bind probe; if the port is actively occupied, render `PORT_IN_USE` before spawning and include the port.
- LogSink generic `sk-` scrubbing should follow the spec's `sk-\S+` intent and mask punctuation-heavy tokens all the way to whitespace, not only alphanumeric prefixes.
- CLI `preview` and `run` should also catch `VllmProfileError` and exit with a plain stderr error instead of Typer/Rich tracebacks.
- CLI `preview` and `run` should treat unknown config names as operator-facing lookup errors, printing the requested name plus available valid configs and exiting nonzero without a traceback.
- CLI `preview` and `run` should distinguish retained invalid configs from true unknown names; if an invalid entry matches `raw_name` or filename stem, print its file name and field-level validation errors.
- CLI `preview` and `run --preview` should share command-builder warning emission so non-local bind and request-logging caveats are visible in Mac-side dry runs before rsyncing to GPU boxes.
- Root `vllm-loader --version` is part of the deployment contract, not just the `version` subcommand; implement it as an eager callback option so it exits before launching Textual.
- Dashboard `select_config()` is also a preview surface; unsupported `vllm.require_flags` should render `Preview unavailable: ...` there instead of raising before the operator launches.
- Textual-focused Figma deliverables should be terminal-cell representations: map state to RichLog, ProgressBar, Static/DataTable/Input, modal Screens, TCSS borders/backgrounds, Unicode markers, and command palette actions rather than arbitrary vector/canvas graphics.
- Textual-realistic Figma should still be polished and full-screen: use Rich/Textual-style color, density, panels, modal overlays, ProgressLine bars, status hierarchy, and scrolling log surfaces, as long as each visual maps back to widgets/TCSS.
- Textual implementation of the Figma TUI should combine structural TCSS chrome with Rich `Text` renderables for semantic color; headless `run_test()` screenshots may carry `nocolor`, so color verification should inspect Rich styles/spans in addition to visual artifacts.
- Sidebar color matters as much as log/status color in the Figma-derived TUI: config title/list, selected config, invalid configs, stable phase workflow, warning banners, and error banners should all be Rich `Text` renderables with the shared cyan/green/amber/red/slate role palette.
- FR-22/§8.5 includes `Tab` focus traversal alongside command actions; expose it in `BINDINGS`, footer/help text, and the command palette via Textual's built-in `focus_next` action.
- FR-24/§8.6 says sub-100-column layouts collapse the sidebar to an overlay, not just hide it; keep a `#sidebar-overlay` visible in narrow/compact modes with selected config, phase/status, and server URL context while the log remains visible.
- ErrorBanner's §8.6 "jump-to-lines" affordance is implemented as a dynamic `Jump to error log line` palette command; named error banners derive a search target from `fsm.error_excerpt`, advertise the command, then highlight the matching log line via the existing search path.
- ConfirmScreen is also the canonical destructive-confirm surface for `K` kill; attached and reattached kills should open Kill/Cancel first and defer `process.kill()` or `SIGKILL` to `confirm_kill_running`.
- `vllm.require_flags` should be checked against collected `vllm serve --help` flags when help is available; CLI/TUI previews and launches use a config-aware profile selector, but arbitrary custom scripts should not be probed unless their executable name is vLLM-like.
- The fake child must answer `--version` and `serve --help` like a vLLM binary, because profile detection runs before fake-child launches in CLI/TUI tests.
- GPU-host real-config validation should use `vllm-loader smoke`, not a blind timeout around `vllm-loader run`; smoke waits for `/health` and `/v1/models`, prints READY URL/models, then stops the server.
- GPU-host no-real-config validation now runs on `10.25.0.50` from `/tank/repos/lab-tui` with the persistent ZFS venv `/tank/venvs/lab-tui`; `/tank/preproc/venv/bin/python` is the seed interpreter when present.
- Remote validation must export the validation venv's `bin` directory onto PATH before pytest so spawned `vllm-loader` commands and `/usr/bin/env python3` fake-child scripts stay inside the venv.
- Attached TUI Stop/Kill should record operator shutdown intent by process PID before signalling; otherwise a confirmed SIGKILL return code is indistinguishable from an unintentional child crash and can incorrectly render CRASHED instead of STOPPED.
- Detached tail workers must classify an unexpected disappearance of the currently attached sidecar as a terminal process exit; otherwise the UI can remain stuck in the last loading phase after the detached server is gone.

## Do-Not-Repeat

<!-- Mistakes made and corrected. Each entry prevents the same mistake recurring. -->
<!-- Format: [YYYY-MM-DD] Description of what went wrong and what to do instead. -->

- [2026-06-02] Do not use buffered `stdout.read(4096)` for supervisor log drains; use `os.read(fd, 4096)` so startup logs stream before EOF.
- [2026-06-02] Do not flush LogSink's over-limit pending buffer only once; repeated overflow from one large read must be broken into bounded truncated records.
- [2026-06-02] Do not ignore repo-local OpenWolf instructions; read `.wolf/anatomy.md` before file reads and `.wolf/cerebrum.md` before code generation.
- [2026-06-02] Do not treat reattach as a UI-only state; Stop/Kill after reattach must signal the verified detached child process group via sidecar identity checks.
- [2026-06-02] Do not assume `/health` success means the subsequent `/v1/models` probe is safe; catch connection failures on both requests to avoid shutdown-race worker crashes.
- [2026-06-02] Do not trust `/v1/models` 200 bodies to be parseable JSON; model-list parsing is advisory and must not crash readiness probing.
- [2026-06-02] Do not trust `/v1/models` parsed payload shape either; validate the top-level object, `data` list, and item dicts before extracting ids.
- [2026-06-02] Do not treat `/v1/models` 401 as a harmless non-200 model-list response; it means auth is required or mismatched and should be `HF_AUTH`, not READY.
- [2026-06-02] Do not probe LAN/public bind addresses directly by default; use localhost unless `server.probe_host` explicitly overrides it.
- [2026-06-02] Do not put dense timeline formatting in one f-string; repo Ruff enforces 100-column lines.
- [2026-06-02] When adding `vllm_loader` test imports, put `import vllm_loader...` before `from vllm_loader...` in the same project import block so Ruff I001 stays clean.
- [2026-06-02] In `src/vllm_loader/tui/app.py`, keep `vllm_loader.messages` imports before `vllm_loader.monitoring.*` imports or Ruff I001 will reorder the project import block.
- [2026-06-02] Do not initialize detached log tails from latest file size after loading existing lines; carry forward the loaded file offset to avoid reattach races.
- [2026-06-02] If `test_tui_load_honors_detached_launch_mode` misses `Uvicorn running` in a full-suite run, rerun the exact selector and then the full suite before changing code; one occurrence did not reproduce.
- [2026-06-02] Do not assert dynamic sidecar command-palette entries from a single immediate snapshot in full-suite smoke tests; wait for the expected command.
- [2026-06-02] Do not leave optional TUI monitoring as a mount-only sample; live monitors need a periodic worker that cannot crash the app.
- [2026-06-02] Do not call the GPU sampler directly from Textual workers; NVML/nvidia-smi can block, so use `asyncio.to_thread` and render the result afterward.
- [2026-06-02] Do not rely on Textual's default worker error behavior for detached reattach health probing; explicitly pass `exit_on_error=False`.
- [2026-06-02] Do not make optional monitor workers non-crashing but silent; add an `on_worker_state_changed` warning backstop for GPU/health worker errors.
- [2026-06-02] Do not let GPU sampler exceptions bypass `_render_gpu_panel`; catch them and render an unavailable detail string in the panel.
- [2026-06-02] Do not assume a posted monitor message can always find its widget during Textual teardown; render helpers should preserve state and tolerate `NoMatches` for optional panels.
- [2026-06-02] Do not ignore MIG identity fields in GPU sampling; capture GPU/compute instance IDs when pynvml exposes them.
- [2026-06-02] Do not treat FR-13 pause as a flag-only toggle; wire it to `RichLog.auto_scroll`.
- [2026-06-02] Do not coerce `HealthEvent.error_kind` into `timeout()` or raw detail text; preserve the specific kind such as `HF_AUTH` or `TIMED_OUT` in the TUI banner.
- [2026-06-02] Do not read Superpowers skills from the bundled plugin cache root; this session's Superpowers paths are under `openai-curated/superpowers/bebc3d6a/skills`.
- [2026-06-02] Do not hard-code a prior Superpowers cache hash; locate the current `openai-curated/superpowers/*/skills/.../SKILL.md` path if the remembered hash is gone.
- [2026-06-02] Do not call `Message.__init__()` from a Textual message dataclass `__post_init__`; Textual's `__init__` calls `self.__post_init__()` and will recurse. Call `Message.__post_init__(self)` instead.
- [2026-06-02] Do not cap only the displayed `RichLog`; prune app-side log/search/filter buffers too or bursty output still grows memory unbounded.
- [2026-06-02] If a detached reattach smoke times out waiting for READY, rerun the exact test before changing code; a one-off suite failure was not reproduced by the focused test or full-suite rerun.
- [2026-06-02] Do not try to pass `devtools` into `App.run()` for Textual 8.2.7; merge `debug,devtools` into the `TEXTUAL` env before app construction.
- [2026-06-02] Do not use Python's missing-file error as a generic crash fixture; the "No such file" text is intentionally classified as `MODEL_NOT_FOUND`. Use an unclassified temporary script that exits non-zero.
- [2026-06-02] Do not call `pilot.resize(...)` in Textual 8.2.7 smoke tests; the available helper is `await pilot.resize_terminal(width, height)`.
- [2026-06-02] When testing spec-prescribed banner wording, match the required phrase exactly; `install vLLM` with lower-case `install` was intentional in the missing-executable guidance test.
- [2026-06-02] Do not let CLI launch FileNotFoundError bubble through Typer; remote validation should get command-not-found guidance, not a Rich traceback.
- [2026-06-02] Do not implement quit-confirm Stop as a plain modal pop; for FR-8 it must stop the attached child and exit the TUI.
- [2026-06-02] After adding stdlib imports, rerun Ruff before broader tests; `shutil` sorts before `signal` in this repo's import order.
- [2026-06-02] Before expanding a focused pytest command from memory, confirm renamed test selectors with `rg`; `test_log_buffers_are_bounded_by_max_log_lines` is now `test_log_buffers_are_bounded_for_bursty_output`.
- [2026-06-02] Before expanding TUI lifecycle smoke commands, confirm selectors with `rg`; the launch smoke is `test_fake_child_launch_streams_logs_and_stop_works`.
- [2026-06-02] Before expanding command-builder focused tests, confirm selectors with `rg`; local model reference coverage is `test_model_reference_local_vs_hf_repo_logic`.
- [2026-06-02] Do not satisfy FR-22 by listing a command only; bound actions like `/` search and `f` filter need an input path that changes application state.
- [2026-06-02] Do not treat `Copy server URL` as a notification-only action; use Textual's clipboard API so the palette command actually copies.
- [2026-06-02] Do not treat wrap as a silent display preference; §8.6 expects toasts for visible state changes, so `w` should notify just like pause.
- [2026-06-02] Do not treat ConfigPickerScreen as only a static list; the canonical screen is filterable/fuzzy and its preview/accept behavior must use the filtered set.
- [2026-06-02] Do not render the status strip as bare phase text; canonical UX requires icon-plus-word status with phase color/pulse state.
- [2026-06-02] Before expanding command-palette smoke commands, confirm selectors with `rg`; the core palette selector is `test_command_palette_exposes_core_actions_and_config_loads`.
- [2026-06-02] Do not let detached supervisor setup failures happen before a pipe-drain path exists; durable log open and artifact writes can fail independently of the child process.
- [2026-06-02] Do not equate "core" palette commands with all FR-22 actions; compare against the actual `BINDINGS` list so top/bottom/quit do not slip out.
- [2026-06-02] Do not assume attached log handling covers detached reattach/tail behavior; detached tails feed the FSM separately and need their own banner/update hooks.
- [2026-06-02] Do not assume detached tail coverage handles reattach's existing-log replay; `_load_scrubbed_log_file` is a third FSM-feed path.
- [2026-06-02] Do not let profile hard-gate failures escape from the TUI worker; unsupported `vllm.require_flags` should surface as a pre-launch `CONFIG_INVALID` banner without spawning anything.
- [2026-06-02] Do not leave CLI profile hard-gate failures uncaught; `preview` and `run --preview` should be useful in remote validation scripts without traceback noise.
- [2026-06-02] Do not mirror existing TUI screenshot artifacts when asked to build Figma screens from the app spec; treat the canonical spec and implementation plan as the only design source.
- [2026-06-02] Do not confuse a visible shell mockup with full spec coverage; read the full workflow, error, ops, and edge-state sections before declaring Figma screens complete.
- [2026-06-02] Do not overpromise Textual graphics in Figma; convert rounded cards, pixel dots, and smooth decorative bars into Textual-native widgets and terminal-cell styling.
- [2026-06-02] Do not call Figma TUI screens polished until visible text-fit, bounds, and overlap audits are clean and a rendered contact sheet has been visually scanned.
- [2026-06-02] Do not let `ConfigRegistry.by_name` `KeyError` escape from CLI commands; remote validation should get unknown-config guidance and the available config names.
- [2026-06-02] Do not collapse retained invalid configs into unknown-config CLI errors; FR-2 requires field-level errors to remain visible when the named config exists but is invalid.
- [2026-06-02] Do not treat detached Quit/Stop/Kill as the only ways to leave a detached run; the TUI needs an explicit detach action that stops local monitoring without signalling the server.
- [2026-06-02] Do not catch only `TrackedProcessMismatch` in TUI reattach; corrupt JSON, missing manifest, or stale artifact paths are normal remote-run debris and should render `Unable to reattach ...` instead of raising.
- [2026-06-02] Do not call sidecar Stop/Kill helpers directly from TUI actions; stale sidecar identity must abort in the UI without clearing the sidecar path or changing the phase to STOPPED.
- [2026-06-02] Do not print only the resolved command in `run --preview`; remote dry-run output also needs command-builder warnings, especially non-local bind/API-key caveats.
- [2026-06-02] Do not satisfy the CLI version contract with only a `version` subcommand; the spec/deployment surface also expects root `vllm-loader --version`.
- [2026-06-02] Do not assume config-picker preview guards protect dashboard selection; `select_config()` can be called directly by palette commands and needs its own profile-error guard.
- [2026-06-02] Before composing focused TUI smoke commands, confirm exact selectors with `rg`; stale names like `test_tui_load_reports_unsupported_required_flags_without_spawning_child` waste a verification cycle.
- [2026-06-02] Do not rely only on vLLM log classification for obvious TP/PP world-size mistakes; when CUDA_VISIBLE_DEVICES gives a clear count, fail before launch with TP_MISMATCH.
- [2026-06-02] Do not use a plain bind-only port preflight; after restart, no process may own the port while plain bind still fails. Use SO_REUSEADDR and a short grace window so true listeners still fail but restart is not blocked.
- [2026-06-02] Do not implement generic OpenAI-style token scrubbing as a narrow alphanumeric prefix; FR-27 says `sk-\S+`, so punctuation-heavy leaked tokens must be fully masked in UI and persisted logs.
- [2026-06-02] Do not rely on TCSS container color alone for the Figma-derived TUI; semantic surfaces such as status, log controls, GPU bars, progress, error banners, and log severity rails should emit Rich `Text` styles directly.
- [2026-06-02] Do not leave config and phase panels as plain strings after adding dashboard chrome; the operator reads those panels constantly, so selected/valid/invalid configs and complete/current/upcoming phases need explicit Rich style roles too.
- [2026-06-02] Do not omit `Tab` from TUI discoverability checks; the spec lists it as a focus binding, so tests should cover Help, footer, and palette exposure.
- [2026-06-02] Do not leave quit-while-attached confirmation as a plain `Screen`; canonical Stop/Cancel confirmations are modal surfaces and should show destructive vs safe actions with explicit Rich color roles.
- [2026-06-02] Do not wire `K` directly to attached or reattached kill signals; canonical kill uses ConfirmScreen first, and only the confirm action should send `process.kill()` or `SIGKILL`.
- [2026-06-02] Do not run profile help probes against arbitrary custom executables before launch preflights; custom scripts can have side effects. Probe only vLLM-like executable names, and keep fake child probe commands side-effect-free.
- [2026-06-02] Do not treat `timeout vllm-loader run REAL_CONFIG` as proof of a real GPU validation; it only bounds a long-running server. Use `vllm-loader smoke REAL_CONFIG` for a READY-bound remote gate.
- [2026-06-02] Do not run lab-tui remote validation from system Python on the GPU nodes; use `/tank/venvs/lab-tui` and put its `bin` directory on PATH before running pytest.
- [2026-06-02] Do not assume blackbird `10.25.0.51` has the same visible ZFS venv layout as `10.25.0.50`; verify the target host's `/tank` mount and venv path before remote validation.
- [2026-06-02] When interpolating shared palette tokens into TCSS strings, escape CSS braces in Python f-strings (`{{` and `}}`) or selectors/properties are parsed as Python expressions.
- [2026-06-02] Do not satisfy responsive behavior by simply hiding the sidebar; canonical narrow mode still needs a sidebar overlay so config/status context is available without making the log disappear.
- [2026-06-02] Do not treat ErrorBanner completion as only kind/guidance/excerpt text; §8.6 also expects a jump-to-lines affordance, so expose a palette command that highlights the excerpted log line.
- [2026-06-02] Do not classify all nonzero attached process exits as CRASHED; first check whether the TUI itself intentionally signalled that exact PID for Stop/Kill.
- [2026-06-02] Do not let `_tail_detached_log` simply fall out of its sidecar-alive loop; active reattach sessions need a terminal FSM update when the sidecar unexpectedly disappears.

## Decision Log

<!-- Significant technical decisions with rationale. Why X was chosen over Y. -->

- [2026-06-02] Detached mode uses a separate Python supervisor process launched with `start_new_session=True`; it owns child pipes, writes scrubbed logs, and writes sidecar/manifest artifacts so the Mac-authored code can be verified on GPU hosts.

## Session 2026-06-08 learnings
- **Vela config discovery dir is `~/.config/vela/configs/` (the `configs/` SUBDIR), not `~/.config/vela/`.** Order: `--configs-dir` > `$VELA_CONFIGS` > `$CWD/configs` > `~/.config/vela/configs` (src/vela/config/loader.py:50-53). Targets live in `~/.config/vela/targets.yaml`. (Do-Not-Repeat: I copied configs to ~/.config/vela/ first and `vela list` was empty.)
- **Canonical Figma file** = `9xUgzyoFqWmd40tV5dwaHv`, current page "Polished Textual Rich UX" `22:2`; redesign page "Workflow Screens — Redesign v1" `39:2`; tokens collection `39:3`. Design language: IBM Plex Mono + dark terminal palette (green #67e8a5 active, cyan #60d7f8 title/focus, amber #f6c85f warn, red #ff6b7a error on #0c141b base / #101923 panel).
- **Figma use_figma spacer gotcha:** a freshly created empty frame is 100×100; used as an auto-layout spacer it inflates its row to ~128px. FIX: `spacer.resize(10,1); spacer.layoutSizingVertical="FIXED"; spacer.layoutGrow=1`. Also `counterAxisAlignItems` has NO "STRETCH" (use child `layoutSizing*="FILL"`). Master-detail sizing: right pane HUGs (drives height), left pane + divider FILL.
- **The implemented TUI workflow screens miss the canonical mocks because those workflow screens were never designed** — only the dashboard/monitoring screens were mocked. The redesign adds the missing form/master-detail/wizard language (terminal-faithful). Full plan + reusable build kit + per-screen specs in `vela-tui-figma-redesign-handoff.md`.

## Session 2026-06-09 learnings (Textual UI overhaul)
- **The TUI overhaul is a PRESENTATION REFACTOR** of existing, already-wired screens in `src/vela/tui/` (package is `vela`; `.wolf/anatomy.md` is STALE — still says `vllm_loader`). Preserve every dismiss-payload shape + widget `id=` + action handler; restyle `compose()`/CSS only. Branch `claude-ui-implementation` (from main `dbdd7ac`). Canonical plan: `vela-tui-overhaul-implementation-plan-v1.md`; full cold-resume context: `vela-tui-session-context-2026-06-09.md`.
- **Run TUI tests with Homebrew `python3 -m pytest`, NOT the repo `.venv`** (`.venv` has no pytest; `vela`+`textual 8.2.7`+`pytest 9.0.2` live in `/opt/homebrew`). `tests/test_tui_smoke.py` (195 tests) is the regression gate and drives screen inputs *by id* through new_deployment handoff flows — so ids are non-negotiable.
- **`Label`/`Static` do NOT expose `.renderable` in Textual 8.2.7** — assert rendered content via the widget's own stored attribute or structural `.query(".class")` counts, never `.renderable`.
- **Render a screen to a viewable PNG:** `app.run_test(size=(W,H))` → set input values → `app.save_screenshot(x.svg)`, then macOS `qlmanage -t -s 1400 -o <dir> x.svg` → `x.svg.png` → Read it. No SVG→PNG converters installed; Playwright blocks `file://` (its backend also timed out via localhost http.server).
- **Shared widget kit at `src/vela/tui/widgets/`** (maps to Figma Component Kit `61:2`): `Field` (wraps a caller-provided control so its id/handlers survive — the key contract-preserving trick), `KeyHintBar`, `ContextCard`, `PresetChips`, `ValidationCard`. `theme.py` expanded with the full "Vela Terminal" token set (BG_*/BORDER_*/TEXT_*/GREEN/CYAN/AMBER/RED/BLUE/VIOLET/SURFACE_*); legacy token names kept for back-compat.
- **Progressive-disclosure pattern (Create Build):** keep all inputs mounted, toggle the `Field` wrapper's `.display` per a method→visible-fields map — so ids + the dismiss payload stay intact while the UI hides irrelevant fields.
- **CSS in f-string `DEFAULT_CSS`:** escape literal braces `{{ }}`; ruff line-length 100 (split long em-dash helper strings). Use Textual border `round` for rounded boxes; 1-row chips use a background highlight (a terminal can't box a 1-row element). ModalScreen test harness: bare `App` + `await app.push_screen(screen)` + `pilot.pause()`.
- **[DECISION 2026-06-09] Keep `theme.py` Python-constants-interpolated-into-CSS-f-strings** (not a `.tcss` variable file) — matches the proven existing pattern, lower risk.
- **[GOTCHA verified 2026-06-09] `Static.content` LEAKS markup strings but NOT Rich `Text`.** `s.update("[cyan]x[/]")` → `str(s.content) == "[cyan]x[/]"` (markup visible — can split an asserted substring and break it); `s.update(Text())` built with `.append(seg, style=...)` → `str(s.content)` is PLAIN (color lives in spans). So to add color to a screen's pinned `Static` panes without breaking smoke substring asserts, render Rich `Text`, NEVER markup strings. (`.renderable` absent in 8.2.7; tests read `.content`.) Probe: `$CLAUDE_JOB_DIR/tmp/probe_static_content.py`.
- **[Phase 3 master-detail approach 2026-06-09]** The smoke suite pins `#*-list`/`#*-detail` as `Static` and asserts `str(.content)` substrings, so the Figma "widgets" (StatusPill/SourceTag/ResolvedCommandPanel) are realized as styled-`Text` RENDER HELPERS feeding those Statics — NOT separately-mounted child widgets (which would move content out of the queried Static). Built `widgets/tags.py` (`source_tag(kind)→Text` cyan=modeled/violet=passthrough/amber=unknown+recipe; `summarize_capabilities(caps, limit=8)` collapses the wall to `"N supported ✓ · ⤢ view all"`; `RECIPE_FLAGS={dtype,kv_cache_dtype}`/`is_recipe_flag`) + `widgets/masterdetail.py` `MasterDetail(list_pane, detail_pane, *, footer)` that WRAPS caller panes (Field-style → preserves their ids). Standalone pill/tag/resolved-command WIDGETS deferred to Phase 4 (wizard/dashboard free composition).
- **[Target Manager 44:2 DONE 2026-06-09]** `target_manager.py` was STACKED (list above detail); refactored to side-by-side `MasterDetail` + grouped detail (CONNECTION/VERSIONS/PATHS/CAPABILITIES/RUNTIME) + capability collapse + `KeyHintBar` footer. Preserved exact list-row format `"{marker} {dot} {name}  {transport}  {host}"` (2-space separators) + all `key: value` detail substrings (grouped under headers with 2-space indent — `in` substring asserts survive leading whitespace). Removed the old `actions:` detail line (now in the footer keybar; not asserted). Tests: `tests/test_target_manager_screen.py`.
- **[Flag Manager 55:2 DONE 2026-06-09 — Phase 3 core complete]** Refactored to Rich `Text`: grouped table (source-tag colors cyan modeled / violet passthrough / amber unknown + amber `*` changed-dots + visible amber `recipe` tag on dtype/kv-cache-dtype), self-explaining detail (module-level `_FLAG_DESCRIPTIONS` keyed by engine field + `value · preset · → engine.<field>` mapping + amber `Recipe-protected` warning = Refinement B), masked resolved-command panel. KEPT the bespoke `Horizontal(list, editor)` layout (the right pane is a COMPOSITE: `#flag-manager-value` Input + `#flag-manager-extra-args` Input + `#flag-manager-detail` Static) — `MasterDetail` models a simple list+detail so it does NOT fit here; `MasterDetail` is for Target/Model/Build managers. Preserved all `#flag-manager-*` ids + the `save_flags` payload + every smoke substring. Tests: `tests/test_flag_manager_screen.py`. Model/Build managers have NO Figma mock (not in the §6 node map) → their consistency pass is deferred to Phase 6, not part of Phase 3.
- **[New Deployment wizard 56:2-58:2 DONE 2026-06-09 — Phase 4]** Two classes in `new_deployment.py`: `NewDeploymentScreen` (6-step wizard) + `NewDeploymentReviewScreen`. Built `StepIndicator` (widgets/step_indicator.py: ✓done green / ▸current cyan / faint future; `set_current()` re-renders). Wizard: StepIndicator replaces the plain arrow `#new-deployment-steps`; token CSS (round border, BG_PANEL); `KeyHintBar` footer (replaced `#new-deployment-actions` dynamic Static + removed `_actions_text`); `→` on handoff option labels + `.new-deployment-helper` Statics signposting that Create build / Adopt venv / Pin HF / Adopt local open a dedicated screen. Review: token CSS, StepIndicator at current=4, inset GREEN masked resolved-command, KeyHintBar. CONTRACT-PRESERVING: kept all 24 `#new-deployment-*` ids + handoff dismisses (`on_select_changed`) + `_collect_spec`/`_draft_state` payloads + review-panel substrings. Deliberately did NOT convert Selects→RadioSet or mass Field-wrap (too risky for the 24-test screen) → deferred to Phase 6. Self-introduced regression: panel width 80 broke `test_new_deployment_screen_opens_from_tui_binding` (asserts `region.x > 0` = centered at the 80-col default `run_test()`); reverted to 76 — REMEMBER modal panels must stay < 80 wide to center at the default test size. Tests: `tests/test_new_deployment_screen.py`.
- **[Dashboard log classification 60:2 DONE 2026-06-09 — Phase 5, screenshot #7 fix]** Added `display_level_for_line(text)` + `BENIGN_PATTERNS` (currently just `destroy_process_group() was not called`) to `log_sink.py` — leaves `level_for_line` and the error-FSM path UNTOUCHED (display-only). Added a `"BENIGN"` level → faint `#56707c` in `app.py` `LEVEL_STYLE`/`LEVEL_RAIL_STYLE`. Wired `display_level_for_line` into `_handle_committed_log` (live committed lines — preserves upstream level for non-benign, dims benign) + `_load_scrubbed_log_file` (detached reattach replay). Now benign NCCL/torch shutdown noise is dimmed, not amber. The dashboard CHROME was already clean from v1 (3-zone header `Vela · ●target● · config · status · clock`, sidebar Config/Phases/GPU cards, footer keybar — the screenshot-#1 cryptic glyphs / giant banner were the pre-redesign version). PhaseStepper widget extraction deferred to Phase 6 (existing `_render_phase_timeline` already a vertical stepper). To extend benign dimming: add patterns to `BENIGN_PATTERNS`. Tests: `tests/test_log_sink.py::test_display_level_dims_known_benign_shutdown_noise`.
- **[Phase 6 polish DONE 2026-06-09 — overhaul complete]** Model + Build Manager consistency: refactored `model_manager.py` + `build_manager.py` to the shared master-detail language (`MasterDetail` + Rich `Text` colored status dots via `_model_status_color`/`_build_status_color` + `KeyHintBar` + token CSS), preserving all `#model/build-manager-*` ids + exact list/detail substrings (tests: `test_{model,build}_manager_screen.py`). anatomy.md refresh: **`openwolf scan` does NOT respect `.gitignore`** and was indexing `.mypy_cache` (565 junk lines) — fixed at the root by adding `.mypy_cache`/`.pytest_cache`/`.ruff_cache`/`.venv`/`.playwright-mcp` to `.wolf/config.json` `exclude_patterns`, then rescanning (305 lines, 223 files, `vllm_loader` staleness gone). Ghost-placeholder audit: the flagged #4 offender (download-model revision sha) was already fixed in Phase 2; remaining placeholders are clear hints. Full suite 998 green. DEFERRED as optional (non-flagged, functional): small-modals CSS token-modernization (confirm/help/log_prompt/config_picker/pin_model/target_edit still use ACCENT/SURFACE_ALT/solid borders) + the Download-Model advanced-patterns toggle. **All 6 phases done; the overhaul meets its §15 definition of done.**

## Do-Not-Repeat additions (2026-06-09)
- [2026-06-09] Don't write production widget/screen code test-alongside — use STRICT red-green TDD (`superpowers:test-driven-development`): failing test FIRST, watch it fail for the right reason, then minimal green, then refactor. (User corrected the `Field` widget; it was deleted + redone red→green.)
- [2026-06-09] Don't run TUI tests with `.venv/bin/python` (no pytest there). Use Homebrew `python3 -m pytest`.
- [2026-06-09] Don't assert Textual rendered text via `.renderable` (absent in 8.2.7). Use stored attrs / structural `.query()` counts.
- [2026-06-09] Don't reintroduce `_parse_build_params` / `_parse_adopt_build_params` — guard tests in `tests/test_tui_screen_parsers.py` assert they don't exist.
- [2026-06-09] Don't break a screen's dismiss-payload shape or queried `id=`s when restyling — the 195-test smoke suite sets inputs by id and asserts payloads.
- [2026-06-09] The harness `TaskCreate`/`TaskList` tracker is EPHEMERAL — a `/clear` wipes it and reassigns IDs (it's session/conversation state, not on-disk). Durable task state lives in `vela-tui-session-context-2026-06-09.md` §9. On EVERY session restore, after reading §9, immediately reconstruct the live tracker from it (don't wait to be asked). Never treat the empty task list after a restore as "tasks were lost" — the content is in §9.

## Session 2026-06-09 (evening) learnings — DoD verification review

- **Key Learning:** Launch/attach tests and the local agent share the REAL `~/.local/state/vela` state dir (runs dir + agent.sock) — suites leak run records (9,676 found) and fake_vllm_child/supervisor processes, and accumulated state degrades active-run discovery past the tests' 5s deadlines. The suite can be green in the morning and fail in the afternoon with zero code change (bug-185; same family as bug-019/090).
- **Key Learning (spec gap):** `probe_until_ready` cancels `probe_loop` at the first ready event (`agent/local.py:1442-1473`), and the TUI only ever calls `probe_until_ready` — so FR-18 post-READY DEGRADED/recovery detection is NOT wired in production even though probe_loop/FSM/TUI-handler all support it (unit+smoke tested via posted messages only).

## Do-Not-Repeat additions (2026-06-09 evening)

- **(2026-06-09)** When launch/attach tests fail, do NOT assume a code regression: first check for leaked vela processes (`ps aux | grep -E "vela|fake_vllm"`) and `~/.local/state/vela/runs` bloat, then prove regression-vs-environment by running the same tests at the base commit in a clean worktree (`git worktree add /tmp/lab-tui-base <ref>` + `PYTHONPATH=/tmp/lab-tui-base/src`).
- **(2026-06-09)** "Rendered + eyeballed" misses state-dependent chrome: the dashboard header's `▣`/`M` build/model glyph segments only render when a target is connected AND a config is loaded — an idle render looks clean. Render the connected+config state too before claiming chrome is clean. (This is how "dashboard chrome already v1-styled" got recorded while glyphs remained at app.py:4185-4233.)
- **(2026-06-09)** Don't ship footer key hints without a matching binding/action: `o`/`a` advertised in download_model and "⤢ view all" in target_manager are dead affordances flagged in review. If a feature is deferred, drop its hint until wired.

## Session 2026-06-09 (functional pass) learnings

- **Key Learning (CRITICAL):** The TUI's local target uses `LocalTransportKind.SOCKET` → a PERSISTENT `vela agent run` daemon on `default_agent_socket_path()`. The daemon is spawned once and reused across test runs — so after editing agent-side code, tests keep validating the OLD daemon's code until the daemon restarts. The conftest `isolated_vela_state` fixture now gives each pytest session a fresh XDG temp state dir (fresh daemon, current code) and stops it at teardown. If debugging the agent OUTSIDE pytest, restart the daemon first: `python -m vela.cli agent stop`.
- **Key Learning:** macOS caps Unix socket paths (~104 chars) — never put `agent.sock` under pytest's deep tmp_path factory dirs; use `tempfile.mkdtemp` under /tmp.
- **Key Learning:** `on_phase_changed` has a post-READY monotonicity guard; any new post-READY phase (like DEGRADED) must be explicitly allowed through or the agent's phase events are silently dropped.
- **Do-Not-Repeat (2026-06-09):** When an agent-side change has no effect in TUI tests, FIRST check which process actually serves the RPC (`ps aux | grep "vela.cli agent"`) before debugging the code path — instrumenting `LocalAgent.handle` with a print that never fires is the 30-second diagnostic.

## Session 2026-06-09 (journey phases A-C) learnings

- **Key Learning (Textual):** an Enter-driven wizard walk needs focus-follow: when a step container's display flips off, focus jumps to the next focusable widget — often a Select, which consumes Enter (opens its overlay) and silently eats the screen-level "enter" binding. Fix pattern: after step advance, focus the step's first Input, else `set_focus(None)` so the screen binding fires (new_deployment._focus_step_entry).
- **Key Learning (contract-safe draft restore):** to preserve a pinned dismiss-payload shape while still recovering UI state after dismissal, stash state on the screen object at submit (`self.last_draft = self._draft_state()`) and have the app keep the screen reference in the push_screen callback closure — plain attributes stay readable after unmount; widget queries do not.
- **Convention:** completion bridges only auto-reopen a manager when `len(self.screen_stack) == 1` (user is on the dashboard) — never push a modal over whatever the user navigated to during a long job.

## Session 2026-06-10 (phases E+F) learnings

- **Key Learning (Textual layout):** plain `Vertical` containers nested in screens default to `height: 1fr` — wrapper groups added for disclosure CLIP their children or inflate to fill the step, pushing siblings out of view. ALWAYS add `height: auto` CSS for new wrapper containers (and `1fr`-width columns) inside form screens.
- **Key Learning (Textual focus):** hiding the focused widget triggers an ASYNC focus-restoration that grabs the next focusable (often a Select that swallows Enter) AFTER your own focus code ran. Fix: `set_focus(None)` BEFORE flipping displays AND give step containers `can_focus=True` to act as inert focus anchors.
- **Do-Not-Repeat (2026-06-10):** never wait on `app.screen.id == "<screen>"` alone before querying children — heavier screens register before children mount (bug-207). Include a child/value check in the wait condition.

## Session 2026-06-10 (GPU validation closed) learnings

- **Key Learning (Select mount race, VERIFIED on GPU host):** assigning `Select.value` before the Select's internals compose raises `NoMatches '#label' on SelectCurrent` — only reproducible on slower hosts (blackbird), never the Mac. The readiness gate for a screen whose Select you'll assign is `app.screen.id == "<screen>" and bool(app.screen.query("#<select-id> SelectCurrent #label"))` (string selector, no import needed). Fixed bug-209 (4 gates in 2 wizard tests); blackbird round 3 went 1087/1087.
- **Key Learning (remote validation green path):** the full `run_remote_tests.sh` green sequence on blackbird is: git pull (VELA_REMOTE_BRANCH) → editable install → agent restart → host/GPU probe → ruff → pytest → config list/preview → fake-child preview → daemon-restart live-run survival (`DAEMON_RESTART_LIVE_RUN_OK`) → disconnect/reconnect stream resume (`DISCONNECT_RECONNECT_RESUME_OK`) → live docker `smoke-tui` launch (`READY` → auto-stop, `VELA_SMOKE_RUN_ID`) → `BACKEND_EVIDENCE_OK` → artifact written LOCALLY under `artifacts/remote-validation/` (commit it yourself).
- **Do-Not-Repeat (2026-06-10):** OpenWolf's auto-detect hook can append JUNK buglog entries from ordinary Edits (bug-209 was first auto-logged as "Incorrect value in code" with my edit strings as root_cause/fix). After fixing a real bug, check the buglog TAIL and rewrite the auto-entry with the real error/root-cause/fix instead of appending a duplicate.
