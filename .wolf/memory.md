# Memory

> Chronological action log. Hooks and AI append to this file automatically.
> Old sessions are consolidated by the daemon weekly.

| 01:19 | Continued canonical v2 audit; selected detached launch as next missing production slice | src/, tests/, spec | baseline pytest 48 passed before new work | ~3000 |
| 01:19 | Added detached CLI artifact regression test | tests/test_cli_run.py | red: CLI rejected launch.mode detached | ~1200 |
| 01:19 | Implemented detached supervisor launch, sidecar, manifest, and scrubbed log artifacts | src/vllm_loader/engine/process_manager.py, src/vllm_loader/engine/supervisor.py, src/vllm_loader/cli.py | detached fake child launches and writes artifacts | ~3000 |
| 01:19 | Added system sidecar verification helper and permission regression | src/vllm_loader/engine/sidecar.py, tests/test_sidecar.py, tests/test_cli_run.py | targeted detached and sidecar tests pass | ~1800 |
| 01:19 | Added health probe loop for readiness timeout and READY/DEGRADED recovery, wired TUI to keep polling | src/vllm_loader/monitoring/health.py, src/vllm_loader/tui/app.py, tests/test_health.py, tests/test_tui_smoke.py | health and TUI smoke tests pass | ~1800 |
| 01:19 | Removed generated Python cache directories from source tree | src/, tests/ | no __pycache__ paths remain | ~200 |
| 01:19 | Ran final Mac-safe verification for this continuation | pyproject/test suite/scripts | ruff clean, pytest 52 passed, smoke_fake_child passed | ~900 |
| 01:25 | Added TUI ConfigPicker and Confirm modal screens with smoke coverage | src/vllm_loader/tui/screens/config_picker.py, src/vllm_loader/tui/screens/confirm.py, src/vllm_loader/tui/app.py, tests/test_tui_smoke.py | targeted config picker and attached quit confirmation tests pass | ~1800 |
| 01:26 | Added functional TUI log search/filter state and RichLog refresh | src/vllm_loader/tui/app.py, tests/test_tui_smoke.py | TUI smoke tests pass | ~1200 |
| 01:26 | Ran full verification after TUI polish and removed regenerated pycache | entire project | ruff clean, pytest 55 passed, smoke_fake_child passed, no __pycache__ remains | ~1000 |
| 01:32 | Added app-specific command palette commands and per-config load entries | src/vllm_loader/tui/app.py, tests/test_tui_smoke.py | palette smoke test passes | ~1200 |
| 01:32 | Added TUI reattach discovery for detached sidecars with log load and health probing | src/vllm_loader/tui/app.py, src/vllm_loader/engine/sidecar.py, tests/test_tui_smoke.py | detached reattach smoke test passes | ~2200 |
| 01:32 | Wired reattached detached Stop/Kill through sidecar identity re-verification before signal | src/vllm_loader/tui/app.py, src/vllm_loader/engine/sidecar.py, tests/test_tui_smoke.py | detached stop smoke and sidecar tests pass | ~1600 |
| 01:32 | Ran full verification after command palette and detached reattach control | entire project | ruff clean, pytest 58 passed, smoke_fake_child 10 passed, no __pycache__ remains | ~1000 |
| 01:39 | Honored Mac-to-GPU workflow clarification while adding TUI detached Load coverage | tests/test_tui_smoke.py, src/vllm_loader/tui/app.py | launch.mode detached now starts supervisor and reattaches via sidecar | ~1800 |
| 01:39 | Fixed health probe shutdown race after detached Stop | tests/test_health.py, src/vllm_loader/monitoring/health.py | /v1/models connection failures return not-ready instead of crashing worker | ~1200 |
| 01:39 | Ran Mac-safe local verification after detached TUI Load fix | entire project | ruff clean, pytest 60 passed, smoke_fake_child 11 passed | ~1000 |
| 01:48 | Added READY status URL/model state and visible non-local bind warnings | src/vllm_loader/tui/app.py, tests/test_tui_smoke.py | focused tests pass and TUI smoke reached 12 passed before next slice | ~1800 |
| 01:48 | Added deterministic phase timeline elapsed rendering | src/vllm_loader/tui/app.py, tests/test_tui_smoke.py | phase panel now tracks per-phase and overall elapsed time with fake-clock test | ~1700 |
| 01:48 | Fixed Ruff E501 from phase timeline row formatting | src/vllm_loader/tui/app.py, .wolf/buglog.json | ruff clean after splitting elapsed formatting | ~500 |
| 01:51 | Fixed detached reattach log-tail race discovered by fresh smoke run | src/vllm_loader/tui/app.py, tests/test_tui_smoke.py | deterministic offset regression and TUI smoke 14 passed | ~1800 |
| 01:53 | Hardened dynamic command-palette reattach smoke against timing flake | tests/test_tui_smoke.py | targeted command test and TUI smoke 14 passed | ~700 |
| 01:58 | Added live GPU panel refresh worker and richer GPU rows | src/vllm_loader/tui/app.py, tests/test_tui_smoke.py | GPU panel refresh test and TUI smoke pass | ~1700 |
| 01:58 | Added named error banner guidance for classified log errors | src/vllm_loader/tui/app.py, tests/test_tui_smoke.py | OOM banner suggestion test and TUI smoke pass | ~1400 |
| 02:04 | Added selected config resolved command preview with masked secrets | src/vllm_loader/tui/app.py, tests/test_tui_smoke.py | focused preview test and TUI smoke pass | ~1400 |
| 02:04 | Wired Pause to RichLog autoscroll | src/vllm_loader/tui/app.py, tests/test_tui_smoke.py | autoscroll toggle test and TUI smoke pass | ~800 |
| 02:04 | Fixed Ruff E501 in selected preview build call | src/vllm_loader/tui/app.py, .wolf/buglog.json | ruff clean after split statements | ~400 |
| 02:11 | Preserved named health error kinds from probe events into TUI banners | src/vllm_loader/engine/phases.py, src/vllm_loader/tui/app.py, tests/test_phases.py, tests/test_tui_smoke.py | HF_AUTH health event now renders named banner and guidance | ~1700 |
| 02:11 | Corrected Superpowers skill path lookup after wrong cache-root read failed | .wolf/cerebrum.md, .wolf/buglog.json | skill reads succeeded from openai-curated path and gotcha recorded | ~500 |
| 02:11 | Ran Mac-safe verification after health error banner fix | entire project | ruff clean, pytest 70 passed, smoke_fake_child 20 passed | ~900 |
| 02:11 | Removed pytest-generated Python cache directories after verification | src/, tests/ | find shows no __pycache__ directories and no fake child process remains | ~200 |
| 02:17 | Added bounded TUI log buffers for bursty output | src/vllm_loader/tui/app.py, tests/test_tui_smoke.py | RichLog and app-side log/search buffers now cap at max_log_lines | ~1600 |
| 02:17 | Investigated one-off detached reattach READY timeout in TUI smoke suite | tests/test_tui_smoke.py, .wolf/buglog.json | focused rerun and full TUI suite rerun passed; flake recorded | ~900 |
| 02:17 | Ran Mac-safe verification after bounded log buffer slice | entire project | json valid, ruff clean, pytest 71 passed, smoke_fake_child 21 passed | ~900 |
| 02:22 | Added debug self-observability path for the TUI | src/vllm_loader/cli.py, src/vllm_loader/tui/app.py, tests/test_cli_run.py, tests/test_tui_smoke.py | --debug enables Textual debug/devtools env and JSONL app events | ~2200 |
| 02:22 | Fixed missing os import in new CLI debug regression | tests/test_cli_run.py, .wolf/buglog.json | focused CLI debug test passes after import fix | ~300 |
| 02:22 | Ran Mac-safe verification after debug slice | entire project | json valid, ruff clean, pytest 73 passed, smoke_fake_child 22 passed | ~900 |
| 02:27 | Added generic CRASHED banner coverage for child exit before READY | src/vllm_loader/engine/phases.py, src/vllm_loader/tui/app.py, tests/test_phases.py, tests/test_tui_smoke.py | non-zero pre-ready exit now shows CRASHED with last log excerpt | ~1900 |
| 02:27 | Corrected misleading generic-crash test fixture | tests/test_tui_smoke.py, .wolf/buglog.json | missing-file fixture replaced with unclassified temporary failing script | ~600 |
| 02:27 | Ran Mac-safe verification after CRASHED banner slice | entire project | json valid, ruff clean, pytest 75 passed, smoke_fake_child 23 passed | ~900 |
| 02:34 | Added FR-24 responsive TUI layout breakpoints | src/vllm_loader/tui/app.py, tests/test_tui_smoke.py | sidebar collapses below 100 cols, GPU drops below 60 cols, log remains displayed | ~1500 |
| 02:34 | Corrected Textual responsive smoke resize helper | tests/test_tui_smoke.py, .wolf/buglog.json | Pilot uses resize_terminal rather than resize in Textual 8.2.7 | ~500 |
| 02:34 | Ran Mac-safe verification after responsive layout slice | entire project | json valid, ruff clean, pytest 76 passed, smoke_fake_child 24 passed | ~900 |
| 02:39 | Added FR-12 search highlight regression | tests/test_tui_smoke.py | red: search_matches existed but RichLog had no styled matching segment | ~900 |
| 02:39 | Implemented manual Rich Text search highlighting for logs | src/vllm_loader/tui/app.py | search spans now layer over severity style on live writes and refresh | ~1300 |
| 02:39 | Ran Mac-safe verification after search highlight slice | entire project | json valid, ruff clean, pytest 77 passed, smoke_fake_child 25 passed | ~900 |
| 02:43 | Added FR-10 ProgressBar regression for transient records | tests/test_tui_smoke.py | red: #progress was Static, not ProgressBar | ~800 |
| 02:43 | Replaced static progress line with ProgressBar plus progress text | src/vllm_loader/tui/app.py | transient percentages update ProgressBar and stay out of committed logs | ~1200 |
| 02:43 | Ran Mac-safe verification after ProgressBar slice | entire project | json valid, ruff clean, pytest 78 passed, smoke_fake_child 26 passed | ~900 |
| 02:47 | Added missing-executable TUI launch regression | tests/test_tui_smoke.py | red: FileNotFoundError escaped from start_attached | ~900 |
| 02:47 | Added COMMAND_NOT_FOUND banner handling for missing executable | src/vllm_loader/engine/phases.py, src/vllm_loader/engine/process_manager.py, src/vllm_loader/tui/app.py | missing launch executable now shows install/module guidance and closes failed PTY master | ~1400 |
| 02:47 | Corrected command-not-found guidance wording | src/vllm_loader/tui/app.py, .wolf/buglog.json | focused error tests pass with spec phrase `install vLLM` | ~500 |
| 02:47 | Ran Mac-safe verification after missing-executable slice | entire project | json valid, ruff clean, pytest 79 passed, smoke_fake_child 27 passed | ~900 |
| 02:52 | Added detached missing-executable launch regression | tests/test_tui_smoke.py | red: detached supervisor exited before writing sidecar | ~900 |
| 02:52 | Preflighted detached child executable before supervisor launch | src/vllm_loader/engine/process_manager.py, src/vllm_loader/tui/app.py | detached missing binaries now show COMMAND_NOT_FOUND and avoid run artifacts | ~1300 |
| 02:52 | Fixed Ruff import order after adding shutil | src/vllm_loader/engine/process_manager.py, .wolf/buglog.json | ruff clean after moving shutil before signal | ~400 |
| 02:52 | Ran Mac-safe verification after detached command-not-found slice | entire project | json valid, ruff clean, pytest 80 passed, smoke_fake_child 28 passed | ~900 |
| 03:00 | Added NFR-2 RichLog batching regression | tests/test_tui_smoke.py | red: burst writes grew RichLog immediately from 1 to 26 lines | ~700 |
| 03:00 | Implemented short-timer batched TUI log writes | src/vllm_loader/tui/app.py | app state updates immediately while RichLog writes flush through set_timer; refresh clears stale pending writes | ~1000 |
| 03:00 | Corrected stale bounded-buffer pytest selector | tool invocation, .wolf/buglog.json | current test is test_log_buffers_are_bounded_for_bursty_output; focused log group passed | ~350 |
| 03:06 | Added detached supervisor log-rotation regression | tests/test_cli_run.py | red: manifest.active_log stayed on original run.log and rotated list was empty | ~850 |
| 03:06 | Implemented detached durable log rotation | src/vllm_loader/engine/log_sink.py, src/vllm_loader/engine/supervisor.py, src/vllm_loader/engine/process_manager.py | supervisor opens private rotated active logs and atomically updates manifest | ~1300 |
| 03:06 | Fixed Ruff import ordering after rotation test | tests/test_cli_run.py, .wolf/buglog.json | focused sink/sidecar/detached tests passed after Ruff I001 autofix | ~450 |
| 03:09 | Investigated non-reproduced smoke miss | tests/test_tui_smoke.py, scripts/smoke_fake_child.sh | isolated test passed, exact smoke pytest subset passed, full smoke rerun passed | ~700 |
| 03:13 | Added attached restart smoke coverage | tests/test_tui_smoke.py | restart already stopped first fake child and started a new pid; no production code needed | ~700 |
| 03:13 | Added FR-17 bound-but-unhealthy timeout regression | tests/test_health.py | red: timeout detail only said readiness timeout after 0s | ~650 |
| 03:13 | Preserved last readiness cause in timeout detail | src/vllm_loader/monitoring/health.py | TIMED_OUT now distinguishes still loading/not bound from bound but unhealthy status details | ~700 |
| 03:13 | Corrected stale TUI launch smoke selector | tool invocation, .wolf/buglog.json | current selector is test_fake_child_launch_streams_logs_and_stop_works; lifecycle group passed | ~350 |
| 03:18 | Added missing local model path TUI regression | tests/test_tui_smoke.py | red: path-like missing model launched child and ended STOPPED instead of MODEL_NOT_FOUND | ~850 |
| 03:18 | Preflighted missing local model paths before TUI launch | src/vllm_loader/tui/app.py | MODEL_NOT_FOUND banner includes resolved path and child is not spawned | ~900 |
| 03:18 | Corrected stale command-builder selector | tool invocation, .wolf/buglog.json | current selector is test_model_reference_local_vs_hf_repo_logic; focused group passed | ~300 |
| 03:21 | Completed Mac-safe verification and cleanup for local model preflight slice | entire project | json valid, ruff clean, pytest 85 passed, fake-child smoke 31 passed, no fake child or __pycache__; real vLLM/GPU tests remain post-rsync | ~450 |
| 03:24 | Added prompt-flow regressions for search and filter actions | tests/test_tui_smoke.py | red: pressing / or f left search_text/filter_text empty | ~650 |
| 03:24 | Implemented LogPromptScreen and wired search/filter callbacks | src/vllm_loader/tui/screens/log_prompt.py, src/vllm_loader/tui/app.py | / bad Enter updates search_matches; f ERROR Enter filters visible_log_lines | ~900 |
| 03:25 | Corrected stale command-palette selector in focused prompt verification | tool invocation, .wolf/buglog.json | current selector is test_command_palette_exposes_core_actions_and_config_loads; focused prompt group passed | ~300 |
| 03:27 | Ran Mac-safe verification after search/filter prompt slice | entire project | json valid, ruff clean, focused prompt group 5 passed, pytest 87 passed, fake-child smoke 33 passed, no fake child or __pycache__ | ~900 |
| 03:30 | Added supervisor initial-log-open failure regression | tests/test_cli_run.py | red: LogSink constructor FileExistsError escaped before child drain | ~650 |
| 03:30 | Added supervisor artifact-write failure regression | tests/test_cli_run.py | red under temporary old behavior: simulated manifest write failure escaped before drain | ~750 |
| 03:30 | Added drain-only supervisor fallback for setup I/O failures | src/vllm_loader/engine/supervisor.py | initial log open failures and artifact write failures no longer prevent child output draining | ~900 |
| 03:32 | Ran Mac-safe verification after supervisor drain fallback slice | entire project | json valid, ruff clean, focused supervisor group 4 passed, pytest 89 passed, fake-child smoke 33 passed, no fake child process | ~900 |
| 03:34 | Added FR-22 palette coverage for navigation and quit actions | tests/test_tui_smoke.py | red: Scroll logs to top/bottom and Quit app were missing from palette titles | ~450 |
| 03:34 | Wired missing top/bottom/quit palette commands | src/vllm_loader/tui/app.py | existing action_top/action_bottom/action_quit are now reachable via Ctrl+P | ~350 |
| 03:34 | Fixed Ruff E501 in new palette command entry | src/vllm_loader/tui/app.py | split Scroll logs to top SystemCommand over multiple lines | ~150 |
| 03:36 | Ran Mac-safe verification after FR-22 palette completion slice | entire project | json valid, ruff clean, focused TUI palette/log group 4 passed, pytest 89 passed, fake-child smoke 33 passed, no fake child process | ~850 |
| 03:39 | Added detached-tail classified-error banner regression | tests/test_tui_smoke.py | red: tailed OOM line set ERROR/OOM but left error_text blank | ~650 |
| 03:39 | Rendered shared error banner for detached tailed classified errors | src/vllm_loader/tui/app.py | detached OOM tail now shows OOM banner with excerpt and guidance | ~450 |
| 03:40 | Ran Mac-safe verification after detached-tail banner slice | entire project | json valid, ruff clean, focused detached/error banner group 4 passed, pytest 90 passed, fake-child smoke 34 passed, no fake child process | ~900 |
| 11:39 | Added loaded detached log classified-error banner regression | tests/test_tui_smoke.py | red: replayed OOM line set ERROR/OOM but left error_text blank on reattach load | ~550 |
| 11:39 | Rendered shared error banner after detached log replay | src/vllm_loader/tui/app.py | _load_scrubbed_log_file now shows OOM banner with excerpt and guidance | ~350 |
| 11:41 | Ran Mac-safe verification after loaded detached log banner slice | entire project | json valid, ruff clean, focused detached/error group 4 passed, pytest 91 passed, fake-child smoke 35 passed, no fake child process | ~900 |
| 11:45 | Added TUI unsupported require_flags pre-launch regression | tests/test_tui_smoke.py | red: VllmProfileError escaped from build_command during TUI load | ~550 |
| 11:45 | Rendered CONFIG_INVALID banner for profile hard-gate failures | src/vllm_loader/engine/phases.py, src/vllm_loader/tui/app.py | missing required vLLM flags now show a pre-launch config error without spawning the child | ~500 |
| 11:46 | Ran Mac-safe verification after require_flags preflight slice | entire project | json valid, ruff clean, pytest 92 passed, fake-child smoke 36 passed, no fake child or __pycache__ | ~850 |
| 11:48 | Added config-picker resolved-command preview regression | tests/test_tui_smoke.py | red: picker modal showed only the config list and no masked preview | ~450 |
| 11:48 | Rendered selected config preview inside ConfigPickerScreen | src/vllm_loader/tui/screens/config_picker.py | modal now shows masked build_command preview and gracefully reports profile preview failures | ~450 |
| 11:49 | Ran Mac-safe verification after config-picker preview slice | entire project | json valid, ruff clean, pytest 93 passed, fake-child smoke 37 passed, no fake child or __pycache__ | ~850 |
| 11:51 | Added repeated partial-line overflow regression | tests/test_log_sink.py | red: one huge unterminated line left an over-limit tail committed whole on close | ~350 |
| 11:51 | Repeatedly flushed over-limit LogSink pending text | src/vllm_loader/engine/log_sink.py | pending partial lines now stay bounded across oversized reads | ~250 |
| 11:52 | Ran Mac-safe verification after LogSink overflow slice | entire project | json valid, ruff clean, pytest 94 passed, fake-child smoke 37 passed, no fake child or __pycache__ | ~850 |
| 11:54 | Added CLI unsupported require_flags regression | tests/test_cli_run.py | red: vllm-loader preview emitted a Rich traceback for VllmProfileError | ~450 |
| 11:54 | Routed CLI profile hard-gate failures through plain stderr exit | src/vllm_loader/cli.py | preview/run now print ERROR with unsupported flags and exit nonzero without traceback | ~350 |
| 11:55 | Ran Mac-safe verification after CLI profile hard-gate slice | entire project | json valid, ruff clean, pytest 95 passed, fake-child smoke 37 passed, no fake child or __pycache__ | ~850 |
| 11:57 | Added GPU sampler thread regression | tests/test_tui_smoke.py | red: periodic GPU sampler ran on the Textual/event-loop thread | ~450 |
| 11:57 | Moved TUI GPU sampling onto asyncio.to_thread | src/vllm_loader/tui/app.py | sampler work now runs off-loop and UI rendering stays on the app thread | ~350 |
| 11:58 | Ran Mac-safe verification after GPU threading slice | entire project | json valid, ruff clean, pytest 96 passed, fake-child smoke 38 passed, no fake child or __pycache__ | ~850 |
| 12:00 | Added CLI missing-executable regression | tests/test_cli_run.py | red: vllm-loader run emitted a Rich traceback for FileNotFoundError | ~450 |
| 12:00 | Routed CLI launch FileNotFoundError through command guidance | src/vllm_loader/cli.py | attached/detached run now print Command not found with install/module guidance | ~400 |
| 12:01 | Ran Mac-safe verification after CLI missing-executable slice | entire project | json valid, ruff clean, pytest 97 passed, fake-child smoke 38 passed, no fake child or __pycache__ | ~850 |
| 12:05 | Added quit-confirm exit regression | tests/test_tui_smoke.py | red: Stop in the quit confirmation stopped the child but left the TUI running | ~350 |
| 12:05 | Made confirmed attached quit stop then exit | src/vllm_loader/tui/app.py | ConfirmScreen Stop now exits the app after stopping the attached process; Cancel still resumes | ~250 |
| 12:06 | Ran Mac-safe verification after quit-confirm slice | entire project | json valid, ruff clean, pytest 97 passed, fake-child smoke 38 passed, no fake child or __pycache__ | ~850 |
| 12:07 | Aligned health probe host test with canonical non-loopback rule | tests/test_health.py | red: LAN host probes returned the LAN bind address instead of 127.0.0.1 | ~300 |
| 12:07 | Probed localhost for non-loopback binds by default | src/vllm_loader/monitoring/health.py | probe_host override still wins; loopback hosts are preserved | ~250 |
| 12:08 | Ran Mac-safe verification after health probe host slice | entire project | json valid, ruff clean, pytest 97 passed, fake-child smoke 38 passed, no fake child or __pycache__ | ~850 |
| 12:11 | Added NVML MIG identity sampling regression | tests/test_gpu.py | red: _sample_nvml populated UUID/name but left mig_instance_id unset | ~350 |
| 12:11 | Captured NVML GPU/compute instance IDs when present | src/vllm_loader/monitoring/gpu.py | GpuSample.mig_instance_id now becomes `GI n / CI m` and existing TUI rendering displays it | ~300 |
| 12:12 | Ran Mac-safe verification after MIG identity slice | entire project | json valid, ruff clean, pytest 98 passed, fake-child smoke 38 passed, no fake child or __pycache__ | ~850 |
| 12:14 | Added config-picker fuzzy filter regression | tests/test_tui_smoke.py | red: typing in ConfigPickerScreen did not narrow the visible config list | ~350 |
| 12:14 | Added Input-backed fuzzy filtering to ConfigPickerScreen | src/vllm_loader/tui/screens/config_picker.py | picker now filters by name/model, previews the filtered selection, and Enter selects the filtered item | ~700 |
| 12:15 | Ran Mac-safe verification after fuzzy config-picker slice | entire project | json valid, ruff clean, pytest 99 passed, fake-child smoke 39 passed, no fake child or __pycache__ | ~850 |
| 12:17 | Recorded validation workflow clarification | project process | code is generated on Mac, then rsynced to GPU boxes for real vLLM/GPU tests; local checks stay Mac-safe/fake-child | ~150 |
| 12:21 | Added status badge icon/class regression | tests/test_tui_smoke.py | red: app.status_text was bare `IDLE` and #status lacked phase color classes | ~450 |
| 12:21 | Added icon and class based status strip rendering | src/vllm_loader/tui/app.py | #status now renders icon plus phase word, uses status--* classes, and pulses loading phases | ~650 |
| 12:23 | Ran Mac-safe verification after status badge slice | entire project | json valid, ruff clean, focused status test passed, detached selector passed on rerun after one file-level timeout, TUI smoke 39 passed, pytest 100 passed, fake-child smoke 40 passed, no fake child or __pycache__ | ~900 |
| 12:26 | Added copy-server-URL clipboard regression | tests/test_tui_smoke.py | red: last_copied_url was set but Textual clipboard stayed empty | ~350 |
| 12:26 | Wired Copy server URL to Textual clipboard | src/vllm_loader/tui/app.py | action_copy_server_url now copies ready_url/current server URL via copy_to_clipboard | ~300 |
| 12:28 | Investigated intermittent detached TUI log timeout | tests/test_tui_smoke.py, run artifacts | failed artifact durable log contained Uvicorn line; 10-run focused diagnostic passed; fresh fake-child smoke rerun passed | ~700 |
| 12:28 | Ran Mac-safe verification after clipboard slice | entire project | json valid, ruff clean, focused clipboard test passed, TUI smoke 40 passed, pytest 101 passed, fake-child smoke rerun 41 passed, no fake child or __pycache__ | ~900 |
| 12:49 | Added CLI unknown-config regression | tests/test_cli_run.py | red: preview/run emitted Rich traceback with KeyError for a missing config name | ~350 |
| 12:49 | Routed CLI config lookup misses through plain stderr | src/vllm_loader/cli.py | preview/run now print the missing name plus available valid configs and exit nonzero without traceback | ~300 |
| 12:49 | Ran Mac-safe verification after CLI unknown-config slice | entire project | json valid, ruff clean, CLI tests 11 passed, pytest 105 passed, fake-child smoke 43 passed, no fake child process | ~900 |
| 12:50 | Built spec-only Figma screen set and recorded no-screenshot correction | Figma file 9xUgzyoFqWmd40tV5dwaHv; .wolf/cerebrum.md | 11 editable frames validated; existing TUI screenshots not used | ~0 |
| 12:53 | Added CLI invalid-named-config regression | tests/test_cli_run.py | red: preview/run reported `Unknown config: bad` instead of field-level invalid config errors | ~350 |
| 12:53 | Surfaced retained invalid configs in CLI lookup | src/vllm_loader/cli.py | matching invalid raw_name/path stem now prints file name plus validation errors before unknown fallback | ~300 |
| 12:53 | Ran Mac-safe verification after CLI invalid-config slice | entire project | json valid, ruff clean, CLI tests 13 passed, pytest 107 passed, fake-child smoke 43 passed, no fake child process | ~900 |
| 12:50 | Cleaned Figma deliverable page and reran readback | Figma file 9xUgzyoFqWmd40tV5dwaHv | blank Page 1 removed; one Spec-only TUI screens page remains with 11 frames, no image layers | ~0 |
| 12:59 | Added detached-run detach regression | tests/test_tui_smoke.py | red: reattached detached run had no `Detach from detached run` command/action | ~450 |
| 12:59 | Added explicit TUI detach action for sidecar-backed runs | src/vllm_loader/tui/app.py | command cancels detached tail/health workers and clears local control while leaving fake-child server alive | ~450 |
| 12:59 | Ran Mac-safe verification after detached-action slice | entire project | json valid, ruff clean, focused detach passed, TUI smoke 43 passed, pytest 108 passed, fake-child smoke 44 passed, no fake child process | ~900 |
| 13:04 | Added malformed-sidecar reattach regression | tests/test_tui_smoke.py | red: JSONDecodeError raised through app.reattach_detached_run for broken.json | ~350 |
| 13:04 | Guarded TUI reattach preflight against stale/corrupt artifacts | src/vllm_loader/tui/app.py | verification, sidecar parsing, and manifest loading now render `Unable to reattach` without mutating attachment state | ~400 |
| 13:04 | Ran Mac-safe verification after reattach robustness slice | entire project | json valid, ruff clean, focused reattach group 3 passed, TUI smoke 44 passed, pytest 109 passed, fake-child smoke 45 passed, no fake child process | ~900 |
| 13:10 | Built spec-complete Textual-realistic Figma page | Figma file 9xUgzyoFqWmd40tV5dwaHv; .firecrawl | 20 editable terminal-cell frames, no existing TUI screenshot mirroring, Firecrawl docs support Textual widget/TCSS feasibility, sampled screenshots clean | ~0 |
| 13:10 | Added stale-sidecar Stop/Kill regressions | tests/test_tui_smoke.py | red: TrackedProcessMismatch raised through action_stop/action_kill after detached reattach | ~450 |
| 13:10 | Guarded reattached sidecar Stop/Kill signals | src/vllm_loader/tui/app.py | failed sidecar signals now render Unable to stop/kill and keep sidecar path/phase unchanged | ~450 |
| 13:10 | Ran Mac-safe verification after sidecar signal guard slice | entire project | json valid, ruff clean, focused detached lifecycle 5 passed, TUI smoke 46 passed, pytest 111 passed, fake-child smoke 47 passed, no fake child process | ~900 |
| 13:17 | Added CLI run-preview warning regression | tests/test_cli_run.py | red: `run --preview` printed the resolved non-local bind command but stderr had no command-builder WARNING | ~350 |
| 13:17 | Shared CLI command warning emission across preview paths | src/vllm_loader/cli.py | `preview` and `run --preview` now both print result.warnings so remote dry-runs surface bind/API-key caveats | ~300 |
| 13:17 | Ran Mac-safe verification after CLI run-preview warning slice | entire project | json valid, ruff clean, CLI/command-builder 23 passed, pytest 112 passed, fake-child smoke 47 passed, no fake child or __pycache__ | ~900 |
| 13:21 | Added dashboard select_config profile-gate regression | tests/test_tui_smoke.py | red: selecting a valid config with unsupported require_flags raised VllmProfileError before launch | ~350 |
| 13:21 | Guarded selected-config preview profile failures | src/vllm_loader/tui/app.py | select_config now preserves selection and shows `Preview unavailable: ...` instead of raising; launch hard-gate still handles start attempts | ~300 |
| 13:21 | Ran Mac-safe verification after select-config preview guard | entire project | json valid, ruff clean, focused TUI group 4 passed after correcting stale selectors, pytest 113 passed, fake-child smoke 48 passed, no fake child or __pycache__ | ~900 |
| 13:26 | Added TP/PP visible-GPU preflight regression | tests/test_tui_smoke.py | red: config with world size 4 and CUDA_VISIBLE_DEVICES=0,1 spawned the child and ended STOPPED instead of TP_MISMATCH | ~450 |
| 13:26 | Preflighted parallel world size against explicit visible GPUs | src/vllm_loader/tui/app.py | TUI launch now reports TP_MISMATCH before spawn when tp*pp exceeds numeric/UUID CUDA_VISIBLE_DEVICES count | ~500 |
| 13:26 | Ran Mac-safe verification after TP mismatch preflight | entire project | json valid, ruff clean, focused preflight group 4 passed, pytest 114 passed, fake-child smoke 49 passed, no fake child or __pycache__ | ~900 |
| 13:34 | Added occupied-port TUI preflight regression | tests/test_tui_smoke.py | red: a config with an already-bound server.port launched the child and ended STOPPED instead of PORT_IN_USE | ~500 |
| 13:34 | Preflighted occupied ports before TUI launch | src/vllm_loader/tui/app.py | PORT_IN_USE now renders before spawn with the configured host/port; reusable bind semantics avoid blocking restart on TIME_WAIT | ~650 |
| 13:34 | Fixed restart interaction from port preflight | src/vllm_loader/tui/app.py, tests/test_tui_smoke.py | full suite initially failed restart; SO_REUSEADDR probe plus short grace kept occupied-port detection while restart passed | ~500 |
| 13:34 | Ran Mac-safe verification after port-in-use preflight | entire project | json valid, ruff clean, focused group 6 passed, pytest 115 passed, fake-child smoke 50 passed, no fake child or __pycache__ | ~900 |
| 13:35 | Ran comprehensive Figma polish pass | Figma file 9xUgzyoFqWmd40tV5dwaHv | Polished Textual Rich UX page now has clean visible text/bounds/overlap audit, fixed GPU rows/filter chips/error banners/responsive chip, and rendered contact-sheet scan across 14 frames | ~0 |
| 13:38 | Added full generic sk-token scrubbing regression | tests/test_log_sink.py | red: `sk-live.secret/with-symbols?abc=123` survived in both emitted UI text and durable log | ~350 |
| 13:38 | Matched generic sk-token scrubber to spec sk-nonwhitespace rule | src/vllm_loader/engine/log_sink.py | TOKEN_RE now masks the entire non-whitespace sk- token, including punctuation-heavy suffixes | ~250 |
| 13:38 | Ran Mac-safe verification after sk-token scrubbing fix | entire project | json valid, ruff clean, log_sink 9 passed, pytest 116 passed, fake-child smoke 50 passed, no fake child or __pycache__ | ~900 |
| 13:57 | Added Figma-derived dashboard shell and color regressions | tests/test_tui_smoke.py | red: default dashboard lacked terminal-shell/chrome/footer/status-strip IDs and Rich styled renderables for semantic color | ~750 |
| 13:57 | Implemented Textual/Rich dashboard shell from Figma artifact | src/vllm_loader/tui/app.py | custom terminal shell/chrome/footer/sidebar/log/progress panels now map Figma surfaces while preserving existing widget IDs/action behavior | ~1100 |
| 13:57 | Added intentional Rich color semantics for TUI surfaces | src/vllm_loader/tui/app.py | status badge, log controls, status strip, severity rails, GPU memory bars, progress labels, and error banner now emit styled Rich Text | ~900 |
| 13:57 | Ran Mac-safe verification after Figma TUI implementation pass | entire project | ruff clean, dashboard focus 3 passed, TUI smoke 52 passed, pytest 119 passed, fake-child smoke 53 passed, no fake child or __pycache__ | ~1100 |
| 14:02 | Added root CLI --version regression | tests/test_cli_run.py | red: `python -m vllm_loader.cli --version` exited 2 because only the `version` subcommand existed | ~300 |
| 14:02 | Implemented eager Typer root --version option | src/vllm_loader/cli.py | root callback prints `__version__` and exits before constructing the Textual app, matching deployment docs/spec | ~250 |
| 14:02 | Ran Mac-safe verification after root --version fix | entire project | direct `vllm-loader --version` printed 0.1.0, ruff clean, CLI 15 passed, pytest 120 passed, fake-child smoke 53 passed, no fake child or __pycache__ | ~1000 |
| 14:13 | Added sidebar/banner semantic-color regression | tests/test_tui_smoke.py | red: config title/list and phase panel were plain strings even after shell color work; warning/error banner roles needed explicit Rich style checks | ~450 |
| 14:13 | Styled config list, stable workflow phases, and warning banners | src/vllm_loader/tui/app.py | visible config and phase widgets now use Rich Text with cyan selected/title, green completed/ready, amber loading/warn, red faults, and slate metadata/upcoming states | ~650 |
| 14:13 | Ran Mac-safe verification after semantic-color slice | entire project | ruff clean, focused TUI color/timeline 3 passed, TUI smoke 53 passed, pytest 121 passed, fake-child smoke 54 passed, saved current-textual-colored.svg, no fake child on 8765 | ~1200 |
| 14:20 | Added Tab focus UX regression | tests/test_tui_smoke.py | red: canonical `Tab focus` was absent from Help, footer bindings, and command palette exposure | ~250 |
| 14:20 | Wired Tab focus through Textual focus_next | src/vllm_loader/tui/app.py, src/vllm_loader/tui/screens/help.py | dashboard now binds Tab to Textual focus_next, exposes `Focus next widget` in the palette, and documents Tab in footer/help | ~250 |
| 14:20 | Ran Mac-safe verification after Tab focus slice | entire project | focused 3 passed, ruff clean, TUI smoke 53 passed, pytest 121 passed, initial parallel fake-child smoke flaked on detached READY but selector passed alone; sequential fake-child smoke 54 passed, no fake child on 8765 | ~1100 |
| 14:32 | Added modal/color regressions for Help and quit confirmation | tests/test_tui_smoke.py | red: Help content was plain string; quit-while-attached confirmation was a non-modal Screen with no destructive/safe Rich color roles | ~350 |
| 14:32 | Shared Figma palette across Textual modal screens | src/vllm_loader/tui/theme.py, src/vllm_loader/tui/screens/*.py, src/vllm_loader/tui/app.py | Help, config picker, log prompt, and confirm surfaces now use the same cyan/green/amber/red/slate palette; ConfirmScreen is a centered ModalScreen | ~500 |
| 14:32 | Ran Mac-safe verification after modal/color slice | entire project | focused modal 2 passed, quit-confirm passed, ruff clean, TUI smoke 54 passed, pytest 122 passed, first fake-child smoke flaked on detached READY but selector passed alone; rerun fake-child smoke 55 passed, no fake child process | ~1200 |
| 14:41 | Added responsive sidebar-overlay regression | tests/test_tui_smoke.py | red: at 99 columns the sidebar disappeared with no `#sidebar-overlay` despite §8.6 requiring sidebar collapse to overlay | ~250 |
| 14:41 | Implemented narrow/compact sidebar overlay | src/vllm_loader/tui/app.py | main column now includes a Rich `#sidebar-overlay` with selected config, phase/status, and URL context when the full sidebar is hidden; log remains visible | ~400 |
| 14:41 | Ran Mac-safe verification after responsive overlay slice | entire project | focused responsive passed, targeted/full ruff clean, TUI smoke 54 passed, first pytest full run flaked on detached command discovery but selector passed alone; rerun pytest 122 passed, fake-child smoke 55 passed, saved actual-tui-narrow-overlay.svg | ~1100 |
| 14:48 | Added ErrorBanner jump-to-lines regression | tests/test_tui_smoke.py | red: OOM banner had kind/guidance/excerpt but no `Jump to error log line` affordance or palette action | ~300 |
| 14:48 | Implemented ErrorBanner jump command | src/vllm_loader/tui/app.py | named error banners now store an excerpt-derived jump target, advertise the palette action, and `Jump to error log line` applies log search/highlight plus scroll-to-bottom | ~500 |
| 14:48 | Ran Mac-safe verification after ErrorBanner jump slice | entire project | focused error passed, focused error/palette 3 passed, targeted/full ruff clean, TUI smoke 54 passed, pytest 122 passed, fake-child smoke 55 passed | ~1000 |
| 15:23 | Added attached force-kill lifecycle regression | tests/test_tui_smoke.py | red: confirmed K/SIGKILL stopped the fake child but never reached STOPPED | ~300 |
| 15:23 | Tracked intentional attached shutdown by PID | src/vllm_loader/tui/app.py | operator Stop/Kill exits are now consumed as STOPPED while ordinary nonzero child exits still classify as CRASHED | ~450 |
| 15:23 | Logged intentional kill lifecycle fix | .wolf/buglog.json, .wolf/cerebrum.md, .wolf/anatomy.md, .wolf/memory.md | bug-081 and project learning recorded for future TUI lifecycle work | ~250 |
| 15:24 | Ran verification after intentional kill lifecycle fix | entire project | json valid, ruff clean, pytest 128 passed, fake-child smoke 58 passed, no fake-child processes remained | ~900 |
| 15:30 | Initialized Git repository setup | .gitignore, .wolf/anatomy.md, .wolf/memory.md | branch renamed to main; caches/build/run artifacts ignored before first snapshot | ~250 |
