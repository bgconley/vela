# anatomy.md

> Auto-maintained by OpenWolf. Last scanned: 2026-06-02T07:24:31.000Z
> Files: 45 tracked | Anatomy hits: 0 | Misses: 0

## ./

- `CLAUDE.md` — OpenWolf (~57 tok)
- `.gitignore` — Git ignore rules for Python caches, envs, build output, and local run logs (~90 tok)
- `README.md` — install, CLI, Mac-to-GPU workflow, config discovery, security note (~280 tok)
- `pyproject.toml` — package metadata, dependencies, console script, pytest and ruff settings (~330 tok)
- `vllm-tui-loader-spec-v2-CANONICAL.md` — vLLM TUI Model Loader — Canonical Specification & Implementation Plan (v2) (~13104 tok)

## .claude/

- `settings.json` (~441 tok)

## .claude/rules/

- `openwolf.md` (~313 tok)

## .firecrawl/

- `textual-actions.md` — Firecrawl snapshot of official Textual actions and command palette docs (~15500 tok)
- `textual-app-api.md` — Firecrawl snapshot of official Textual App API docs, including breakpoints and command APIs (~81000 tok)
- `textual-css.md` — Firecrawl snapshot of official Textual CSS/TCSS styling docs (~61000 tok)
- `textual-data-table.md` — Firecrawl snapshot of official Textual DataTable widget docs (~28000 tok)
- `textual-input.md` — Firecrawl snapshot of official Textual Input widget docs (~26000 tok)
- `textual-progress-bar.md` — Firecrawl snapshot of official Textual ProgressBar widget docs (~27000 tok)
- `textual-rich-log.md` — Firecrawl snapshot of official Textual RichLog widget docs (~25000 tok)
- `textual-screens.md` — Firecrawl snapshot of official Textual Screen and ModalScreen guide (~30000 tok)
- `textual-workers.md` — Firecrawl snapshot of official Textual worker/thread-worker guide (~35000 tok)

## configs/

- `fake-child.yaml` — local no-GPU fake child config for smoke tests (~115 tok)
- `real-vllm.example.yaml` — real vLLM GPU-host example config template (~160 tok)

## docs/

- `gpu-workflow.md` — rsync and remote validation workflow for GPU boxes (~330 tok)

## scripts/

- `fake_vllm_child.py` — executable wrapper around `vllm_loader.fake_child` (~20 tok)
- `rsync_to_gpu.sh` — rsync helper excluding caches, envs, and run artifacts (~260 tok)
- `run_remote_tests.sh` — SSH remote install/lint/test/optional real-config runner (~320 tok)
- `smoke_fake_child.sh` — local editable install plus fake-child/TUI smoke tests (~70 tok)

## src/vllm_loader/

- `__init__.py` — package version (~20 tok)
- `cli.py` — Typer CLI: interactive, list, preview, run attached/detached, version (~900 tok)
- `fake_child.py` — no-GPU fake vLLM-like child with health/models HTTP endpoints and progress logs (~550 tok)
- `messages.py` — typed app/event dataclasses for logs, phases, readiness, GPU (~260 tok)

## src/vllm_loader/config/

- `schema.py` — Pydantic v2 config schema with unset vLLM pass-through defaults (~1250 tok)
- `loader.py` — YAML config discovery/loading, invalid config retention, duplicate detection (~850 tok)

## src/vllm_loader/engine/

- `command_builder.py` — pure command/env/preview builder with masking and profile-aware flags (~1200 tok)
- `log_sink.py` — incremental UTF-8 decode/split/scrub/persist log sink (~900 tok)
- `phases.py` — phase FSM and error classification state (~730 tok)
- `process_manager.py` — attached PTY launch plus detached supervisor launcher (~1100 tok)
- `profile.py` — bundled vLLM profiles, flag maps, version/help probing, soft validation (~1600 tok)
- `sidecar.py` — sidecar/manifest dataclasses, identity verification, system verification (~1150 tok)
- `supervisor.py` — detached supervisor process, pipe drain, sidecar/manifest writer (~850 tok)

## src/vllm_loader/monitoring/

- `gpu.py` — NVML/nvidia-smi GPU sampling and CUDA_VISIBLE_DEVICES mapping (~1100 tok)
- `health.py` — httpx health and `/v1/models` check helpers (~450 tok)

## src/vllm_loader/tui/

- `app.py` — Textual app dashboard, lifecycle controls, config summary, log view, attached/detached launch paths (~1700 tok)
- `screens/config_picker.py` — modal config picker showing valid and invalid configs with keyboard selection (~450 tok)
- `screens/confirm.py` — modal Stop/Cancel confirmation for attached-running quit (~180 tok)
- `screens/help.py` — modal help screen (~120 tok)
- `screens/log_prompt.py` — modal text prompt for log search and filter actions (~170 tok)

## tests/

- `conftest.py` — config fixtures and YAML writer (~360 tok)
- `test_cli_run.py` — CLI attached and detached fake-child integration tests (~1150 tok)
- `test_command_builder.py` — command builder/profile/request-logging/masking tests (~1500 tok)
- `test_config_loader.py` — config loader/schema/default tests (~900 tok)
- `test_gpu.py` — GPU fallback and CUDA visibility tests (~550 tok)
- `test_health.py` — health auth/probe-host tests (~700 tok)
- `test_log_sink.py` — log split/scrub/truncation/mode tests (~900 tok)
- `test_messages.py` — canonical Textual message taxonomy and log-record conversion tests (~260 tok)
- `test_phases.py` — FSM success/error/ready/degraded tests (~850 tok)
- `test_process_manager.py` — attached PTY fake-child integration test (~650 tok)
- `test_sidecar.py` — sidecar identity, manifest, permissions tests (~900 tok)
- `test_tui_smoke.py` — Textual start/help/lifecycle/fake-child launch smoke tests (~950 tok)
