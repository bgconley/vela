from __future__ import annotations

import asyncio
import errno
import json
import re
import signal
import socket
import tempfile
import time
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult, SystemCommand
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import ProgressBar, RichLog, Static
from textual.worker import Worker, WorkerState

from vllm_loader.config.loader import ConfigRegistry, load_registry
from vllm_loader.config.schema import ModelConfig
from vllm_loader.engine.command_builder import build_command, is_local_model_reference
from vllm_loader.engine.log_sink import LogRecord, level_for_line
from vllm_loader.engine.phases import ErrorKind, Phase, PhaseFSM
from vllm_loader.engine.process_manager import AttachedProcess, start_attached, start_detached
from vllm_loader.engine.profile import (
    VllmProfileError,
    bundled_profile,
    select_profile,
    select_profile_for_config,
)
from vllm_loader.engine.sidecar import (
    discover_active_sidecars,
    load_manifest,
    load_sidecar,
    signal_sidecar_from_system,
    stop_sidecar_from_system,
    verify_sidecar_from_system,
)
from vllm_loader.monitoring.gpu import (
    GpuPollResult,
    GpuSample,
    parse_cuda_visible_devices,
    sample_gpus,
)
from vllm_loader.monitoring.health import HealthEvent, probe_loop
from vllm_loader.tui.screens.config_picker import ConfigPickerScreen
from vllm_loader.tui.screens.confirm import ConfirmScreen
from vllm_loader.tui.screens.help import HelpScreen
from vllm_loader.tui.screens.log_prompt import LogPromptScreen
from vllm_loader.tui.theme import ACCENT, BAD, GOOD, MUTED, TEXT, WARN

LEVEL_STYLE = {
    "CRITICAL": "bold #e8f1f2 on #ff6b6b",
    "ERROR": "bold #ff6b6b",
    "WARNING": "#f6c85f",
    "INFO": "",
    "DEBUG": "#526a75",
}

LEVEL_RAIL_STYLE = {
    "CRITICAL": "#ff6b6b",
    "ERROR": "#ff6b6b",
    "WARNING": "#f6c85f",
    "INFO": "#e8f1f2",
    "DEBUG": "#526a75",
}

SEARCH_HIGHLIGHT_STYLE = "black on yellow"
PROGRESS_PERCENT_RE = re.compile(r"(?P<percent>\d{1,3}(?:\.\d+)?)%")
LOADING_PHASES = {
    Phase.STARTING,
    Phase.RESOLVING_MODEL,
    Phase.DOWNLOADING_MODEL,
    Phase.LOADING_WEIGHTS,
    Phase.PROFILING_KV,
    Phase.CAPTURING_GRAPHS,
    Phase.SERVER_STARTING,
}
WORKFLOW_PHASES = (
    Phase.STARTING,
    Phase.RESOLVING_MODEL,
    Phase.DOWNLOADING_MODEL,
    Phase.LOADING_WEIGHTS,
    Phase.PROFILING_KV,
    Phase.CAPTURING_GRAPHS,
    Phase.SERVER_STARTING,
    Phase.READY,
)
STATUS_CLASSES = (
    "status--idle",
    "status--loading",
    "status--ready",
    "status--degraded",
    "status--error",
    "status--stopped",
    "status--pulse",
)
STATUS_ICONS = {
    Phase.IDLE: "○",
    Phase.READY: "●",
    Phase.DEGRADED: "▲",
    Phase.STOPPED: "○",
    Phase.ERROR: "✕",
}
OPTIONAL_MONITOR_GROUP_LABELS = {
    "gpu": "gpu",
    "gpu-initial": "gpu",
    "monitoring": "gpu",
    "health": "health",
}

ERROR_GUIDANCE = {
    ErrorKind.OOM: "Try lowering gpu_memory_utilization or max_model_len.",
    ErrorKind.PORT_IN_USE: "Choose a different server.port or stop the process using it.",
    ErrorKind.MODEL_NOT_FOUND: "Check the model path/name and Hugging Face access.",
    ErrorKind.TP_MISMATCH: "Check tensor_parallel_size, pipeline_parallel_size, and visible GPUs.",
    ErrorKind.HF_AUTH: "Set HF_TOKEN and accept the model license if it is gated.",
    ErrorKind.COMMAND_NOT_FOUND: "install vLLM or set command.entrypoint: module.",
    ErrorKind.CONFIG_INVALID: "Fix the config or choose a compatible vLLM version_profile.",
    ErrorKind.CRASHED: "Check the last log lines and resolved command.",
    ErrorKind.TIMED_OUT: "Check /health, model load progress, GPU memory, and network binding.",
}

DEFAULT_MAX_LOG_LINES = 50_000
DEFAULT_LOG_BATCH_INTERVAL_SECONDS = 0.025
PORT_PREFLIGHT_GRACE_SECONDS = 2.0


class VllmLoaderApp(App):
    HORIZONTAL_BREAKPOINTS = [(0, "-compact"), (60, "-narrow"), (100, "-wide")]

    CSS = """
    Screen { layout: vertical; background: #091015; color: #e8f1f2; }
    #terminal-shell {
        height: 1fr;
        background: #0c141b;
        border: solid #274254;
    }
    #top-chrome {
        height: 3;
        background: #0f1a22;
        border-bottom: solid #274254;
        padding: 0 2;
        align: left middle;
    }
    #app-title {
        width: 20;
        color: #60d7f8;
        text-style: bold;
        content-align: left middle;
    }
    #active-model {
        width: 32;
        color: #e8f1f2;
        text-style: bold;
        content-align: left middle;
    }
    #server-url {
        width: 1fr;
        color: #67e8a5;
        text-style: bold;
        content-align: left middle;
    }
    #chrome-clock {
        width: 10;
        color: #8ba4ae;
        content-align: right middle;
    }
    #body { height: 1fr; padding: 1 2; }
    #sidebar { width: 34; min-width: 24; margin-right: 2; }
    #main { width: 1fr; }
    #sidebar-overlay {
        height: 4;
        margin-bottom: 1;
        background: #101923;
        border: solid #274254;
        padding: 0 1;
    }
    #config-panel { height: 7; }
    #phase-panel { height: 11; }
    #gpu-panel { height: 10; }
    .side-panel {
        background: #101923;
        border: solid #274254;
        padding: 0 1;
        margin-bottom: 1;
    }
    #configs-title, #log-title {
        color: #60d7f8;
        text-style: bold;
    }
    #configs, #phases, #gpu, #status-strip {
        color: #e8f1f2;
    }
    #status {
        width: 26;
        height: 3;
        border: solid #526a75;
        content-align: center middle;
    }
    #status.status--idle { color: #8ba4ae; }
    #status.status--loading { color: #f6c85f; border: solid #f6c85f; }
    #status.status--ready { color: #67e8a5; border: solid #67e8a5; }
    #status.status--degraded { color: #f6c85f; border: solid #f6c85f; }
    #status.status--error { color: #ff6b6b; border: solid #ff6b6b; }
    #status.status--stopped { color: #8ba4ae; }
    #status.status--pulse { text-style: bold; }
    #status-strip {
        height: 3;
        background: #101923;
        border: solid #274254;
        padding: 0 1;
    }
    #log-panel {
        height: 1fr;
        background: #0d151d;
        border: solid #274254;
    }
    #log-header {
        height: 3;
        border-bottom: solid #274254;
        padding: 0 1;
        align: left middle;
    }
    #log-title {
        width: 1fr;
        content-align: left middle;
    }
    #log-controls {
        width: 34;
        color: #8ba4ae;
        content-align: right middle;
    }
    RichLog {
        height: 1fr;
        background: #091015;
        padding: 0 1;
    }
    #progress-panel {
        height: 5;
        margin-top: 1;
        background: #101923;
        border: solid #274254;
        padding: 0 1;
    }
    #progress-label {
        height: 1;
        color: #e8f1f2;
        text-style: bold;
    }
    #progress-line { height: 1; }
    #progress { width: 30; height: 1; }
    #progress-text { width: 1fr; height: 1; }
    #error { color: #ff6b6b; text-style: bold; height: auto; }
    #footer-bindings {
        height: 2;
        background: #0f1a22;
        border-top: solid #274254;
        color: #8ba4ae;
        padding: 0 2;
        content-align: left middle;
    }
    """

    BINDINGS = [
        ("l,enter", "load", "Load"),
        ("s", "stop", "Stop"),
        ("K", "kill", "Kill"),
        ("r", "restart", "Restart"),
        ("c", "config_picker", "Configs"),
        ("/", "search", "Search"),
        ("f", "filter", "Filter"),
        ("p", "pause", "Pause"),
        ("w", "wrap", "Wrap"),
        ("g", "top", "Top"),
        ("G", "bottom", "Bottom"),
        ("tab", "focus_next", "Focus"),
        ("?", "help", "Help"),
        ("f1", "help", "Help"),
        ("q", "quit", "Quit"),
        ("ctrl+c", "quit", "Quit"),
    ]

    phase: reactive[Phase] = reactive(Phase.IDLE)

    def __init__(
        self,
        *,
        configs_dir: str | Path | None = None,
        clock: Callable[[], float] = time.monotonic,
        gpu_sampler: Callable[[], GpuPollResult] = sample_gpus,
        gpu_interval_seconds: float = 2.0,
        max_log_lines: int = DEFAULT_MAX_LOG_LINES,
        log_batch_interval_seconds: float = DEFAULT_LOG_BATCH_INTERVAL_SECONDS,
        debug_log_path: str | Path | None = None,
    ) -> None:
        super().__init__()
        self.configs_dir = Path(configs_dir) if configs_dir is not None else None
        self._clock = clock
        self._gpu_sampler = gpu_sampler
        self._gpu_interval_seconds = gpu_interval_seconds
        self._max_log_lines = max(1, max_log_lines)
        self._log_batch_interval_seconds = max(0.0, log_batch_interval_seconds)
        self.debug_log_path = Path(debug_log_path) if debug_log_path is not None else None
        self.registry = ConfigRegistry()
        self.config_summary = ""
        self.selected_config_preview = ""
        self.paused = False
        self.wrap = False
        self.filter_text = ""
        self.search_text = ""
        self.fsm = PhaseFSM(bundled_profile("current"))
        self.current_config: ModelConfig | None = None
        self.current_process: AttachedProcess | None = None
        self.log_lines: list[str] = []
        self.log_records: list[tuple[str, str | None]] = []
        self.visible_log_lines: list[str] = []
        self.search_matches: list[str] = []
        self._pending_log_writes: list[tuple[str, str | None]] = []
        self._log_flush_scheduled = False
        self.last_copied_url: str | None = None
        self.reattached_sidecar_path: Path | None = None
        self.status_text = "○ IDLE"
        self.error_text = ""
        self.error_jump_text = ""
        self.responsive_mode = "wide"
        self.warning_lines: list[str] = []
        self.ready_url: str | None = None
        self.served_models: list[str] = []
        self.health_detail = ""
        self.run_started_at: float | None = None
        self.current_phase_started_at: float | None = None
        self._intentional_shutdown_pids: set[int] = set()
        self.phase_elapsed: dict[Phase, float] = {}
        self.phase_history: list[Phase] = []
        self.phase_timeline_text = "Phases\n○ IDLE"
        self.gpu_panel_text = "GPU stats unavailable"
        self.progress_text = ""

    def compose(self) -> ComposeResult:
        with Vertical(id="terminal-shell"):
            with Horizontal(id="top-chrome"):
                yield Static("vLLM Loader", id="app-title")
                yield Static("", id="active-model")
                yield Static(
                    self._render_status_badge(Phase.IDLE),
                    id="status",
                    classes="status--idle",
                )
                yield Static("", id="server-url")
                yield Static("", id="chrome-clock")
            with Horizontal(id="body"):
                with Vertical(id="sidebar"):
                    with Vertical(id="config-panel", classes="side-panel"):
                        yield Static("Configs", id="configs-title")
                        yield Static("", id="configs")
                    with Vertical(id="phase-panel", classes="side-panel"):
                        yield Static(self._render_phase_timeline(), id="phases")
                    with Vertical(id="gpu-panel", classes="side-panel"):
                        yield Static(self.gpu_panel_text, id="gpu")
                    yield Static(self._render_status_strip(), id="status-strip")
                with Vertical(id="main"):
                    yield Static("", id="sidebar-overlay")
                    with Vertical(id="log-panel"):
                        with Horizontal(id="log-header"):
                            yield Static(
                                "Logs - unified child stdout/stderr stream",
                                id="log-title",
                            )
                            yield Static(self._render_log_controls(), id="log-controls")
                        yield Static("", id="error")
                        yield RichLog(
                            id="log",
                            markup=False,
                            highlight=False,
                            wrap=False,
                            max_lines=self._max_log_lines,
                        )
                    with Vertical(id="progress-panel"):
                        yield Static("", id="progress-label")
                        with Horizontal(id="progress-line"):
                            yield ProgressBar(total=None, show_eta=False, id="progress")
                            yield Static("", id="progress-text")
            yield Static(self._render_footer_bindings(), id="footer-bindings")

    def on_mount(self) -> None:
        self.registry = load_registry(self.configs_dir)
        if self.current_config is None and self.registry.valid:
            self.current_config = self.registry.valid[0].config
            self._refresh_selected_config_preview()
        self.config_summary = self._render_config_summary_plain()
        self.query_one("#configs-title", Static).update(self._render_configs_title())
        self.query_one("#configs", Static).update(self._render_config_summary())
        self._refresh_sidebar_overlay()
        self._refresh_dashboard_shell()
        self._apply_responsive_layout(self.size.width)
        self._clear_progress()
        self.run_worker(
            self._sample_gpu_panel_once(),
            name="gpu-initial",
            group="gpu-initial",
            exit_on_error=False,
        )
        self.run_worker(
            self._poll_gpu_panel(),
            name="gpu",
            group="monitoring",
            exclusive=True,
            exit_on_error=False,
        )
        self._write_log("INFO vLLM Loader ready")
        self._debug_event(
            "app.mounted",
            configs_dir=str(self.configs_dir) if self.configs_dir is not None else None,
            valid_configs=len(self.registry.valid),
            invalid_configs=len(self.registry.invalid),
        )

    def on_resize(self, event: events.Resize) -> None:
        self._apply_responsive_layout(event.size.width)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.state is not WorkerState.ERROR:
            return
        label = OPTIONAL_MONITOR_GROUP_LABELS.get(event.worker.group)
        if label is None:
            return
        self.notify(f"{label} monitor stopped: {event.worker.error}", severity="warning")

    def get_system_commands(self, screen: Screen) -> Iterable[SystemCommand]:
        yield from super().get_system_commands(screen)
        yield SystemCommand(
            "Load selected config", "Start the selected vLLM config", self.action_load
        )
        yield SystemCommand("Stop server", "Stop the running server gracefully", self.action_stop)
        yield SystemCommand(
            "Force kill server", "Confirm and force-kill the running server", self.action_kill
        )
        yield SystemCommand(
            "Restart server", "Stop and start the selected config", self.action_restart
        )
        yield SystemCommand(
            "Open config picker", "Choose a model config", self.action_config_picker
        )
        yield SystemCommand("Search logs", "Search the visible log lines", self.action_search)
        yield SystemCommand(
            "Filter logs", "Filter log lines by text or severity", self.action_filter
        )
        yield SystemCommand(
            "Toggle autoscroll", "Pause or resume log autoscroll", self.action_pause
        )
        yield SystemCommand("Toggle wrap", "Toggle log line wrapping", self.action_wrap)
        yield SystemCommand(
            "Scroll logs to top", "Jump to the first visible log line", self.action_top
        )
        yield SystemCommand(
            "Scroll logs to bottom", "Jump to the latest visible log line", self.action_bottom
        )
        yield SystemCommand(
            "Focus next widget",
            "Move keyboard focus to the next focusable widget",
            self.action_focus_next,
        )
        yield SystemCommand("Open help", "Show key bindings and commands", self.action_help)
        yield SystemCommand(
            "Copy server URL",
            "Copy or remember the current server URL",
            self.action_copy_server_url,
        )
        if self.error_jump_text:
            yield SystemCommand(
                "Jump to error log line",
                "Highlight the log line referenced by the current error banner",
                self.action_jump_to_error,
            )
        if self.reattached_sidecar_path is not None:
            yield SystemCommand(
                "Detach from detached run",
                "Stop tailing this run while leaving the server running",
                self.action_detach,
            )
        yield SystemCommand("Quit app", "Exit the TUI", self.action_quit)
        for item in self.registry.valid:
            name = item.config.name
            yield SystemCommand(
                f"Load config: {name}",
                f"Select {name} for launch",
                lambda selected=name: self.select_config(selected),
            )
        for path in discover_active_sidecars(self._runs_dirs()):
            sidecar = load_sidecar(path)
            yield SystemCommand(
                f"Reattach detached run: {sidecar.config_name}",
                f"Resume tailing {sidecar.config_name}",
                lambda sidecar_path=path: self.reattach_detached_run(sidecar_path),
            )

    def action_help(self) -> None:
        self.push_screen(HelpScreen(id="help"))

    def action_load(self) -> None:
        if self.current_process and self.current_process.proc.poll() is None:
            self.notify("A process is already running", severity="warning")
            return
        if not self.registry.valid:
            self._set_error_text("No valid configs to load")
            return
        if self.current_config is None:
            self.current_config = self.registry.valid[0].config
        self.run_worker(self._run_selected_config(), name="load", group="engine", exclusive=True)

    def action_stop(self) -> None:
        if self.current_process and self.current_process.proc.poll() is None:
            self._mark_current_process_shutdown_intent()
            self.current_process.stop(interrupt_timeout=2, terminate_timeout=2)
            return
        if self.reattached_sidecar_path is not None:
            self._signal_reattached_sidecar(
                "stop",
                lambda path: stop_sidecar_from_system(
                    path, interrupt_timeout=2, terminate_timeout=2
                ),
            )
            return
        self._set_phase(Phase.STOPPED)
        self._write_log("INFO stop requested")

    def action_kill(self) -> None:
        if self.current_process and self.current_process.proc.poll() is None:
            self.push_screen(
                ConfirmScreen(
                    "Force kill the attached server process?",
                    title="Confirm kill",
                    confirm_label="Kill",
                    confirm_action="confirm_kill_running",
                )
            )
            return
        if self.reattached_sidecar_path is not None:
            self.push_screen(
                ConfirmScreen(
                    "Force kill the detached server process group?",
                    title="Confirm kill",
                    confirm_label="Kill",
                    confirm_action="confirm_kill_running",
                )
            )
            return
        self._set_error_text("Kill requested")

    def action_restart(self) -> None:
        self.action_stop()
        self.action_load()

    def action_config_picker(self) -> None:
        self.push_screen(ConfigPickerScreen(self.registry))

    def action_search(self) -> None:
        self.push_screen(
            LogPromptScreen(
                title="Search logs",
                placeholder="Text to highlight",
                initial=self.search_text,
                id="log-search-prompt",
            ),
            callback=self._apply_search_prompt,
        )

    def action_filter(self) -> None:
        self.push_screen(
            LogPromptScreen(
                title="Filter logs",
                placeholder="Severity or text",
                initial=self.filter_text,
                id="log-filter-prompt",
            ),
            callback=self._apply_filter_prompt,
        )

    def _apply_search_prompt(self, value: str | None) -> None:
        if value is None:
            return
        self.apply_log_search(value)
        self.notify("Search updated")

    def _apply_filter_prompt(self, value: str | None) -> None:
        if value is None:
            return
        self.apply_log_filter(value)
        self.notify("Filter updated")

    def action_pause(self) -> None:
        self.paused = not self.paused
        self.query_one("#log", RichLog).auto_scroll = not self.paused
        self._refresh_log_controls()
        self._refresh_status_strip()
        self.notify(f"Autoscroll {'paused' if self.paused else 'resumed'}")

    def action_wrap(self) -> None:
        self.wrap = not self.wrap
        self.query_one("#log", RichLog).wrap = self.wrap
        self._refresh_log_controls()
        self._refresh_status_strip()
        self.notify(f"Wrap {'enabled' if self.wrap else 'disabled'}")

    def action_top(self) -> None:
        self.query_one("#log", RichLog).scroll_home(animate=False)

    def action_bottom(self) -> None:
        self.query_one("#log", RichLog).scroll_end(animate=False)

    def action_jump_to_error(self) -> None:
        if not self.error_jump_text:
            self.notify("No error log line available", severity="warning")
            return
        self.apply_log_search(self.error_jump_text)
        self.action_bottom()
        self.notify("Error log line highlighted")

    def action_copy_server_url(self) -> None:
        url = self._server_url_for_copy()
        if url is None:
            self.last_copied_url = None
            self.notify("No server URL available", severity="warning")
            return
        self.last_copied_url = url
        self.copy_to_clipboard(url)
        self.notify(f"Server URL: {self.last_copied_url}")

    def action_detach(self) -> None:
        if self.reattached_sidecar_path is None:
            self.notify("No detached run is attached", severity="warning")
            return
        self.workers.cancel_group(self, "tail")
        self.workers.cancel_group(self, "health")
        sidecar_name = self.reattached_sidecar_path.name
        self.reattached_sidecar_path = None
        self.current_process = None
        self._write_log(f"INFO detached from {sidecar_name}; server continues running")
        self.notify("Detached from run; server continues running")

    def _signal_reattached_sidecar(
        self, action: str, signaler: Callable[[Path], None]
    ) -> None:
        if self.reattached_sidecar_path is None:
            return
        sidecar_path = self.reattached_sidecar_path
        try:
            signaler(sidecar_path)
        except Exception as exc:
            self._set_error_text(f"Unable to {action} {sidecar_path.name}: {exc}")
            return
        self.reattached_sidecar_path = None
        self._set_phase(Phase.STOPPED)

    def _server_url_for_copy(self) -> str | None:
        if self.ready_url:
            return self.ready_url
        cfg = self.current_config or (
            self.registry.valid[0].config if self.registry.valid else None
        )
        if cfg is None:
            return None
        return self._server_url(cfg)

    def action_quit(self) -> None:
        if self.current_process and self.current_process.proc.poll() is None:
            self.push_screen(
                ConfirmScreen("Attached server is still running. Stop it before quit?")
            )
            return
        self.exit()

    def select_config(self, name: str) -> None:
        self.current_config = self.registry.by_name(name)
        self._refresh_selected_config_preview()
        self.config_summary = self._render_config_summary_plain()
        self.query_one("#configs", Static).update(self._render_config_summary())
        self.query_one("#configs-title", Static).update(self._render_configs_title())
        self._refresh_sidebar_overlay()
        self._refresh_chrome()

    def confirm_stop_running(self) -> None:
        if self.current_process and self.current_process.proc.poll() is None:
            self._mark_current_process_shutdown_intent()
            self.current_process.stop(interrupt_timeout=2, terminate_timeout=2)
        self.exit()

    def confirm_kill_running(self) -> None:
        if self.screen.id == "confirm":
            self.pop_screen()
        if self.current_process and self.current_process.proc.poll() is None:
            self._mark_current_process_shutdown_intent()
            self.current_process.kill()
            return
        if self.reattached_sidecar_path is not None:
            self._signal_reattached_sidecar(
                "kill",
                lambda path: signal_sidecar_from_system(path, signal.SIGKILL),
            )
            return
        self._set_error_text("Kill requested")

    def reattach_detached_run(self, sidecar_path: Path) -> None:
        try:
            verify_sidecar_from_system(sidecar_path)
            sidecar = load_sidecar(sidecar_path)
            manifest = load_manifest(sidecar.manifest_path)
        except Exception as exc:
            self._set_error_text(f"Unable to reattach {sidecar_path.name}: {exc}")
            return
        log_path = Path(manifest.active_log.path)
        if sidecar.config_snapshot:
            self.current_config = ModelConfig.model_validate(sidecar.config_snapshot)
        else:
            self.current_config = self.registry.by_name(sidecar.config_name)
        self.reattached_sidecar_path = sidecar_path
        self.current_process = None
        self.fsm = PhaseFSM(select_profile(sidecar.vllm_version_profile))
        tail_position = self._load_scrubbed_log_file(log_path)
        self.run_worker(
            self._probe_detached_until_ready(self.current_config, sidecar_path),
            name="reattach-health",
            group="health",
            exclusive=True,
            exit_on_error=False,
        )
        self.run_worker(
            self._tail_detached_log(log_path, sidecar_path, start_position=tail_position),
            name="reattach-tail",
            group="tail",
            exclusive=True,
        )

    def handle_log_record(self, record: LogRecord) -> None:
        if record.kind == "transient":
            self._update_progress(record.text)
            return
        self.fsm.feed_line(record.text)
        self._set_phase(self.fsm.phase)
        if self.fsm.phase is Phase.ERROR and self.fsm.error_kind is not None:
            self._set_error_banner(self.fsm.error_kind)
        self._write_log(record.text, record.level)

    def _write_log(self, text: str, level: str | None = None) -> None:
        self.log_lines.append(text)
        self.log_records.append((text, level))
        for dropped_text, dropped_level in self._trim_log_records():
            if self._line_matches_filter(dropped_text, dropped_level):
                self._discard_first(self.visible_log_lines, dropped_text)
                self._discard_first_pending_log_write(dropped_text, dropped_level)
            if self.search_text and self.search_text.lower() in dropped_text.lower():
                self._discard_first(self.search_matches, dropped_text)
        if self._line_matches_filter(text, level):
            self.visible_log_lines.append(text)
            self._queue_log_write(text, level)
        if self.search_text and self.search_text.lower() in text.lower():
            self.search_matches.append(text)
        self._debug_event(
            "log.committed",
            text=text,
            level=level,
            retained_lines=len(self.log_lines),
        )
        self._refresh_status_strip()

    def _update_progress(self, text: str) -> None:
        self.progress_text = text
        self.query_one("#progress-panel").display = True
        self.query_one("#progress-label", Static).update(
            Text(self._progress_label(text), style=f"bold {WARN}")
        )
        self.query_one("#progress-line").display = True
        self.query_one("#progress-text", Static).update(Text(text, style=MUTED))
        progress = self.query_one("#progress", ProgressBar)
        match = PROGRESS_PERCENT_RE.search(text)
        if match is None:
            progress.update(total=None, progress=0)
            return
        percent = max(0.0, min(100.0, float(match.group("percent"))))
        progress.update(total=100, progress=percent)

    def _clear_progress(self) -> None:
        self.progress_text = ""
        try:
            self.query_one("#progress-panel").display = False
            self.query_one("#progress-label", Static).update("")
            self.query_one("#progress-text", Static).update("")
            self.query_one("#progress", ProgressBar).update(total=None, progress=0)
            self.query_one("#progress-line").display = False
        except NoMatches:
            return

    def apply_log_filter(self, text: str) -> None:
        self.filter_text = text.strip()
        self._refresh_log_view()

    def apply_log_search(self, text: str) -> None:
        self.search_text = text.strip()
        self._refresh_log_view()

    def _refresh_log_view(self) -> None:
        self._pending_log_writes = []
        self._log_flush_scheduled = False
        log = self.query_one("#log", RichLog)
        log.clear()
        self.visible_log_lines = []
        for line, level in self.log_records:
            if self._line_matches_filter(line, level):
                self.visible_log_lines.append(line)
                log.write(self._make_log_text(line, level))
        self._update_search_matches()

    def _queue_log_write(self, text: str, level: str | None) -> None:
        self._pending_log_writes.append((text, level))
        if self._log_flush_scheduled:
            return
        self._log_flush_scheduled = True
        self.set_timer(
            self._log_batch_interval_seconds,
            self._flush_log_batch,
            name="log-batch",
        )

    def _flush_log_batch(self) -> None:
        self._log_flush_scheduled = False
        if not self._pending_log_writes:
            return
        pending = self._pending_log_writes
        self._pending_log_writes = []
        try:
            log = self.query_one("#log", RichLog)
        except NoMatches:
            return
        for text, level in pending:
            log.write(self._make_log_text(text, level))

    def _update_search_matches(self) -> None:
        if not self.search_text:
            self.search_matches = []
            return
        needle = self.search_text.lower()
        self.search_matches = [line for line in self.visible_log_lines if needle in line.lower()]

    def _make_log_text(self, text: str, level: str | None) -> Text:
        prefix = "▌ "
        styled = Text(prefix, style=LEVEL_RAIL_STYLE.get(level or "", MUTED))
        styled.append(text, style=LEVEL_STYLE.get(level or "", ""))
        if not self.search_text:
            return styled
        needle = self.search_text.lower()
        haystack = text.lower()
        offset = len(prefix)
        start = 0
        while True:
            index = haystack.find(needle, start)
            if index == -1:
                break
            styled.stylize(
                SEARCH_HIGHLIGHT_STYLE,
                offset + index,
                offset + index + len(needle),
            )
            start = index + len(needle)
        return styled

    def _line_matches_filter(self, text: str, level: str | None) -> bool:
        if not self.filter_text:
            return True
        needle = self.filter_text.lower()
        return needle in text.lower() or needle == (level or "").lower()

    def _trim_log_records(self) -> list[tuple[str, str | None]]:
        overflow = len(self.log_records) - self._max_log_lines
        if overflow <= 0:
            return []
        dropped = self.log_records[:overflow]
        del self.log_records[:overflow]
        del self.log_lines[:overflow]
        return dropped

    @staticmethod
    def _discard_first(lines: list[str], text: str) -> None:
        try:
            lines.remove(text)
        except ValueError:
            pass

    def _discard_first_pending_log_write(self, text: str, level: str | None) -> None:
        for index, (pending_text, pending_level) in enumerate(self._pending_log_writes):
            if pending_text == text and pending_level == level:
                del self._pending_log_writes[index]
                return

    def _load_scrubbed_log_file(self, path: Path) -> int:
        if not path.exists():
            return 0
        with path.open("r", encoding="utf-8", errors="replace") as file:
            text = file.read()
            position = file.tell()
        for line in text.splitlines():
            self.fsm.feed_line(line)
            self._write_log(line, level_for_line(line))
        self._set_phase(self.fsm.phase)
        if self.fsm.phase is Phase.ERROR and self.fsm.error_kind is not None:
            self._set_error_banner(self.fsm.error_kind)
        return position

    def _render_config_summary_plain(self) -> str:
        lines = []
        if self.current_config is not None:
            lines.extend(
                [
                    f"Selected: {self.current_config.name}",
                    f"Model: {self.current_config.model}",
                    f"Server: {self._server_url(self.current_config)}",
                ]
            )
            if self.selected_config_preview:
                lines.append("Full preview: press c")
            lines.append("")
        lines.extend(f"✓ {item.config.name}" for item in self.registry.valid)
        lines.extend(f"⚠ {item.path.name}: {item.errors[0]}" for item in self.registry.invalid)
        return "\n".join(lines) if lines else "No configs found"

    def _render_config_summary(self) -> Text:
        text = Text()
        if self.current_config is not None:
            text.append("Selected: ", style=MUTED)
            text.append(self.current_config.name, style=f"bold {ACCENT}")
            text.append("\nModel: ", style=MUTED)
            text.append(self.current_config.model, style=TEXT)
            text.append("\nServer: ", style=MUTED)
            text.append(self._server_url(self.current_config), style=GOOD)
            if self.selected_config_preview:
                text.append("\nFull preview: press c", style=MUTED)
            text.append("\n\n")
        if self.registry.valid:
            selected_name = self.current_config.name if self.current_config else None
            for item in self.registry.valid:
                cfg = item.config
                selected = cfg.name == selected_name
                marker = ">" if selected else "✓"
                marker_style = f"bold {ACCENT}" if selected else GOOD
                name_style = f"bold {TEXT}" if selected else TEXT
                text.append(marker, style=marker_style)
                text.append(f" {cfg.name}", style=name_style)
                meta = self._config_meta(cfg)
                if meta:
                    text.append(f"  {meta}", style=MUTED)
                text.append("\n")
        if self.registry.invalid:
            for item in self.registry.invalid:
                text.append("! ", style=f"bold {WARN}")
                text.append(item.path.name, style=f"bold {WARN}")
                text.append(f": {item.errors[0]}", style=MUTED)
                text.append("\n")
        if not text.plain:
            text.append("No configs found", style=MUTED)
        text.rstrip()
        return text

    def _render_configs_title(self) -> Text:
        valid_count = len(self.registry.valid)
        invalid_count = len(self.registry.invalid)
        text = Text("Configs", style=f"bold {ACCENT}")
        if valid_count:
            text.append(f"  {valid_count} valid", style=f"bold {GOOD}")
        if invalid_count:
            text.append(f"  {invalid_count} invalid", style=f"bold {WARN}")
        if self.current_config is None:
            return text
        text.append("\nSelected: ", style=MUTED)
        text.append(self.current_config.name, style=f"bold {TEXT}")
        return text

    @staticmethod
    def _config_meta(cfg: ModelConfig) -> str:
        parts = []
        if cfg.engine.tensor_parallel_size:
            parts.append(f"TP={cfg.engine.tensor_parallel_size}")
        if cfg.engine.pipeline_parallel_size:
            parts.append(f"PP={cfg.engine.pipeline_parallel_size}")
        if cfg.engine.kv_cache_dtype:
            parts.append(str(cfg.engine.kv_cache_dtype).upper())
        return " ".join(parts)

    def _refresh_selected_config_preview(self) -> None:
        if self.current_config is None:
            self.selected_config_preview = ""
            return
        profile = select_profile_for_config(self.current_config)
        try:
            build = build_command(self.current_config, profile)
            self.selected_config_preview = build.preview
        except VllmProfileError as exc:
            self.selected_config_preview = f"Preview unavailable: {exc}"

    def _refresh_dashboard_shell(self) -> None:
        self._refresh_chrome()
        self._refresh_log_controls()
        self._refresh_status_strip()

    def _refresh_chrome(self) -> None:
        try:
            self.query_one("#active-model", Static).update(self._render_active_model())
            self.query_one("#server-url", Static).update(self._render_chrome_url())
            self.query_one("#chrome-clock", Static).update(
                datetime.now().strftime("%H:%M:%S")
            )
        except NoMatches:
            return

    def _render_active_model(self) -> str:
        if self.current_config is None:
            return "no config selected"
        return self.current_config.name

    def _render_chrome_url(self) -> str:
        if self.ready_url:
            return self.ready_url
        if self.current_config is None:
            return ""
        return self._server_url(self.current_config)

    def _refresh_log_controls(self) -> None:
        try:
            self.query_one("#log-controls", Static).update(self._render_log_controls())
        except NoMatches:
            return

    def _render_log_controls(self) -> Text:
        autoscroll = "OFF" if self.paused else "ON"
        wrap = "ON" if self.wrap else "OFF"
        text = Text("autoscroll ", style=MUTED)
        text.append(autoscroll, style=GOOD if autoscroll == "ON" else WARN)
        text.append("   wrap ", style=MUTED)
        text.append(wrap, style=ACCENT if wrap == "ON" else MUTED)
        return text

    def _refresh_status_strip(self) -> None:
        try:
            self.query_one("#status-strip", Static).update(self._render_status_strip())
        except NoMatches:
            return
        self._refresh_sidebar_overlay()

    def _render_status_strip(self) -> Text:
        autoscroll = "OFF" if self.paused else "ON"
        wrap = "ON" if self.wrap else "OFF"
        text = Text()
        text.append(f"{len(self.log_lines):,} lines", style=f"bold {TEXT}")
        text.append(" · autoscroll ", style=MUTED)
        text.append(autoscroll, style=GOOD if autoscroll == "ON" else WARN)
        text.append("\nscrubbed log 0600", style=MUTED)
        text.append(" · wrap ", style=MUTED)
        text.append(wrap, style=ACCENT if wrap == "ON" else MUTED)
        return text

    def _refresh_sidebar_overlay(self) -> None:
        try:
            self.query_one("#sidebar-overlay", Static).update(
                self._render_sidebar_overlay()
            )
        except NoMatches:
            return

    def _render_sidebar_overlay(self) -> Text:
        text = Text("Sidebar overlay", style=f"bold {ACCENT}")
        text.append("  ")
        if self.current_config is None:
            text.append("no config selected", style=MUTED)
        else:
            text.append(self.current_config.name, style=f"bold {TEXT}")
            meta = self._config_meta(self.current_config)
            if meta:
                text.append(f"  {meta}", style=MUTED)
        text.append("\n")
        status_style = self._status_style_for_phase(self.phase)
        text.append(self._status_icon_for_phase(self.phase), style=status_style)
        text.append(f" {self.phase.value}", style=status_style)
        if self.ready_url:
            text.append(f"  {self.ready_url}", style=GOOD)
        elif self.current_config is not None:
            text.append(f"  {self._server_url(self.current_config)}", style=GOOD)
        text.append("\n")
        text.append("Log remains primary; press c for full config picker", style=MUTED)
        return text

    @staticmethod
    def _render_footer_bindings() -> str:
        return (
            "l Load   s Stop   K Kill   r Restart   / Search   f Filter   "
            "p Pause   w Wrap   g/G Top/Bottom   Tab Focus   ? Help   ^P Palette   q Quit"
        )

    @staticmethod
    def _progress_label(text: str) -> str:
        label, _separator, _tail = text.partition(":")
        return label.strip() or "Progress"

    def _apply_responsive_layout(self, width: int) -> None:
        previous_mode = self.responsive_mode
        if width < 60:
            self.responsive_mode = "compact"
        elif width < 100:
            self.responsive_mode = "narrow"
        else:
            self.responsive_mode = "wide"
        try:
            sidebar = self.query_one("#sidebar")
            sidebar_overlay = self.query_one("#sidebar-overlay")
            gpu_panel = self.query_one("#gpu")
            log = self.query_one("#log", RichLog)
        except NoMatches:
            return
        sidebar.display = self.responsive_mode == "wide"
        sidebar_overlay.display = self.responsive_mode != "wide"
        gpu_panel.display = self.responsive_mode != "compact"
        log.display = True
        if self.responsive_mode != previous_mode:
            self._debug_event(
                "layout.responsive",
                width=width,
                mode=self.responsive_mode,
            )

    async def _run_selected_config(self) -> None:
        cfg = self.current_config
        if cfg is None:
            return
        self._reset_run_state()
        profile = select_profile_for_config(cfg)
        self.fsm = PhaseFSM(profile)
        self._set_phase(Phase.STARTING)
        try:
            build = build_command(cfg, profile)
        except VllmProfileError as exc:
            self._handle_profile_error(exc)
            return
        missing_model_path = self._missing_local_model_path(cfg, build.cwd)
        if missing_model_path is not None:
            self._handle_model_not_found(missing_model_path)
            return
        parallel_mismatch = self._parallel_world_size_mismatch(cfg)
        if parallel_mismatch is not None:
            self._handle_tp_mismatch(parallel_mismatch)
            return
        occupied_port = await self._occupied_port_detail_after_grace(cfg)
        if occupied_port is not None:
            self._handle_port_in_use(occupied_port)
            return
        self._record_warnings(build.warnings)
        secrets = [cfg.server.api_key or "", cfg.env.get("HF_TOKEN", "")]
        if cfg.launch.mode.value == "detached":
            try:
                launch = await asyncio.to_thread(
                    lambda: start_detached(cfg, build, secrets=secrets)
                )
            except FileNotFoundError as exc:
                self._handle_command_not_found(exc, build.argv[0])
                return
            self.reattach_detached_run(launch.sidecar_path)
            return
        run_dir = cfg.run_artifacts_dir
        if self.configs_dir is not None:
            run_dir = Path(tempfile.gettempdir()) / "vllm-loader-runs"
        run_dir.mkdir(parents=True, exist_ok=True)
        try:
            attached_process = start_attached(
                build,
                log_path=run_dir / f"{cfg.name}.run.log",
                secrets=secrets,
                emit=self.handle_log_record,
            )
        except FileNotFoundError as exc:
            self._handle_command_not_found(exc, build.argv[0])
            return
        self.current_process = attached_process
        health_task = asyncio.create_task(self._probe_until_ready(cfg))
        returncode = await attached_process.read_loop()
        health_task.cancel()
        if self.current_process is not attached_process:
            return
        if self._consume_intentional_shutdown(attached_process):
            self.fsm.process_exited(0)
            self._set_error_text("")
        else:
            self.fsm.process_exited(returncode)
        if self.fsm.phase is Phase.ERROR and self.fsm.error_kind is not None:
            self._set_error_banner(self.fsm.error_kind)
        self._set_phase(self.fsm.phase)

    def _mark_current_process_shutdown_intent(self) -> None:
        if self.current_process is None:
            return
        pid = getattr(self.current_process.proc, "pid", None)
        if isinstance(pid, int):
            self._intentional_shutdown_pids.add(pid)

    def _consume_intentional_shutdown(self, process: AttachedProcess) -> bool:
        pid = getattr(process.proc, "pid", None)
        if not isinstance(pid, int):
            return False
        if pid not in self._intentional_shutdown_pids:
            return False
        self._intentional_shutdown_pids.remove(pid)
        return True

    def _handle_command_not_found(
        self, exc: FileNotFoundError, fallback_command: str
    ) -> None:
        command = str(exc.filename or fallback_command)
        self.fsm.health_error(
            ErrorKind.COMMAND_NOT_FOUND,
            f"Command not found: {command}",
        )
        self._set_error_banner(ErrorKind.COMMAND_NOT_FOUND)
        self._set_phase(self.fsm.phase)

    def _handle_profile_error(self, exc: VllmProfileError) -> None:
        self.fsm.health_error(ErrorKind.CONFIG_INVALID, str(exc))
        self._set_error_banner(ErrorKind.CONFIG_INVALID)
        self._set_phase(self.fsm.phase)

    def _missing_local_model_path(self, cfg: ModelConfig, cwd: Path) -> Path | None:
        if not is_local_model_reference(cfg.model, cwd=cwd):
            return None
        candidate = Path(cfg.model).expanduser()
        if not candidate.is_absolute():
            candidate = cwd / candidate
        if candidate.exists():
            return None
        return candidate

    def _handle_model_not_found(self, model_path: Path) -> None:
        self.fsm.health_error(
            ErrorKind.MODEL_NOT_FOUND,
            f"Local model path not found: {model_path}",
        )
        self._set_error_banner(ErrorKind.MODEL_NOT_FOUND)
        self._set_phase(self.fsm.phase)

    def _parallel_world_size_mismatch(self, cfg: ModelConfig) -> str | None:
        visible = parse_cuda_visible_devices(cfg.env.get("CUDA_VISIBLE_DEVICES"))
        visible_count = len(visible.numeric) + len(visible.uuids)
        if visible_count == 0:
            return None
        tensor_parallel = cfg.engine.tensor_parallel_size or 1
        pipeline_parallel = cfg.engine.pipeline_parallel_size or 1
        world_size = tensor_parallel * pipeline_parallel
        if world_size <= visible_count:
            return None
        gpu_word = "GPU" if visible_count == 1 else "GPUs"
        return (
            f"Configured world size {world_size} "
            f"(tensor_parallel_size={tensor_parallel}, "
            f"pipeline_parallel_size={pipeline_parallel}) exceeds "
            f"{visible_count} visible {gpu_word} from CUDA_VISIBLE_DEVICES={visible.raw}."
        )

    def _handle_tp_mismatch(self, detail: str) -> None:
        self.fsm.health_error(ErrorKind.TP_MISMATCH, detail)
        self._set_error_banner(ErrorKind.TP_MISMATCH)
        self._set_phase(self.fsm.phase)

    def _occupied_port_detail(self, cfg: ModelConfig) -> str | None:
        family = socket.AF_INET6 if ":" in cfg.server.host else socket.AF_INET
        try:
            with socket.socket(family) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((cfg.server.host, cfg.server.port))
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                return f"Port {cfg.server.port} is already in use on {cfg.server.host}."
        return None

    async def _occupied_port_detail_after_grace(self, cfg: ModelConfig) -> str | None:
        detail = self._occupied_port_detail(cfg)
        if detail is None:
            return None
        deadline = self._clock() + PORT_PREFLIGHT_GRACE_SECONDS
        while self._clock() < deadline:
            await asyncio.sleep(0.05)
            detail = self._occupied_port_detail(cfg)
            if detail is None:
                return None
        return detail

    def _handle_port_in_use(self, detail: str) -> None:
        self.fsm.health_error(ErrorKind.PORT_IN_USE, detail)
        self._set_error_banner(ErrorKind.PORT_IN_USE)
        self._set_phase(self.fsm.phase)

    async def _probe_until_ready(self, cfg: ModelConfig) -> None:
        await probe_loop(
            cfg,
            emit=self._handle_health_event,
            is_process_alive=lambda: bool(
                self.current_process and self.current_process.proc.poll() is None
            ),
        )

    async def _probe_detached_until_ready(self, cfg: ModelConfig, sidecar_path: Path) -> None:
        await probe_loop(
            cfg,
            emit=self._handle_health_event,
            is_process_alive=lambda: self._sidecar_is_alive(sidecar_path),
        )

    async def _tail_detached_log(
        self, log_path: Path, sidecar_path: Path, *, start_position: int | None = None
    ) -> None:
        position = (
            start_position
            if start_position is not None
            else log_path.stat().st_size
            if log_path.exists()
            else 0
        )
        pending = ""
        while self._sidecar_is_alive(sidecar_path):
            if log_path.exists():
                with log_path.open("r", encoding="utf-8", errors="replace") as file:
                    file.seek(position)
                    chunk = file.read()
                    position = file.tell()
                if chunk:
                    pending += chunk
                    *lines, pending = pending.split("\n")
                    for line in lines:
                        if line:
                            self.fsm.feed_line(line)
                            self._set_phase(self.fsm.phase)
                            if (
                                self.fsm.phase is Phase.ERROR
                                and self.fsm.error_kind is not None
                            ):
                                self._set_error_banner(self.fsm.error_kind)
                            self._write_log(line, level_for_line(line))
            await asyncio.sleep(0.25)
        self._handle_detached_tail_ended(sidecar_path)

    def _handle_detached_tail_ended(self, sidecar_path: Path) -> None:
        if self.reattached_sidecar_path != sidecar_path:
            return
        if self.phase in {Phase.ERROR, Phase.STOPPED}:
            return
        self.fsm.process_exited(None)
        if self.fsm.phase is Phase.ERROR and self.fsm.error_kind is not None:
            self._set_error_banner(self.fsm.error_kind)
        self._set_phase(self.fsm.phase)

    def _sidecar_is_alive(self, sidecar_path: Path) -> bool:
        try:
            return verify_sidecar_from_system(sidecar_path)
        except Exception:
            return False

    def _handle_health_event(self, event: HealthEvent) -> None:
        self.health_detail = event.detail
        if event.ready:
            if self.current_config is not None:
                self.ready_url = self._server_url(self.current_config)
            self.served_models = event.models or []
            self.fsm.health_ready(event.models or [])
        elif event.error_kind:
            self.fsm.health_error(event.error_kind, event.detail)
            self._set_error_banner(event.error_kind)
        else:
            self.fsm.health_failed(event.detail)
        self._set_phase(self.fsm.phase)

    def _set_phase(self, phase: Phase) -> None:
        if phase in {Phase.READY, Phase.DEGRADED, Phase.ERROR, Phase.STOPPED, Phase.IDLE}:
            self._clear_progress()
        self._track_phase_time(phase)
        self.phase = phase
        self.status_text = self._render_status(phase)
        status = self.query_one("#status", Static)
        self._apply_status_classes(status, phase)
        status.update(self._render_status_badge(phase))
        timeline = self._render_phase_timeline()
        self.phase_timeline_text = timeline.plain
        self.query_one("#phases", Static).update(timeline)
        self._refresh_sidebar_overlay()
        self._refresh_chrome()
        self._debug_event("phase.changed", phase=phase.value, status=self.status_text)

    def _track_phase_time(self, phase: Phase) -> None:
        now = self._clock()
        if self.run_started_at is None and phase not in {Phase.IDLE, Phase.STOPPED}:
            self.run_started_at = now
        previous = self.phase
        if previous is not phase:
            if self.current_phase_started_at is not None:
                self.phase_elapsed[previous] = (
                    self.phase_elapsed.get(previous, 0.0)
                    + now
                    - self.current_phase_started_at
                )
            self.current_phase_started_at = now
            if phase not in self.phase_history:
                self.phase_history.append(phase)

    def _render_status(self, phase: Phase) -> str:
        icon = self._status_icon_for_phase(phase)
        if phase is Phase.READY and self.current_config is not None:
            url = self.ready_url or self._server_url(self.current_config)
            models = self.served_models or [self.current_config.served_model_name or ""]
            model_text = ", ".join(model for model in models if model)
            if model_text:
                return f"{icon} {phase.value} {url} as {model_text}"
            return f"{icon} {phase.value} {url}"
        if phase is Phase.DEGRADED and self.ready_url:
            detail = f" ({self.health_detail})" if self.health_detail else ""
            return f"{icon} {phase.value} {self.ready_url}{detail}"
        return f"{icon} {phase.value}"

    def _render_status_badge(self, phase: Phase) -> Text:
        style = self._status_style_for_phase(phase)
        return Text(f"{self._status_icon_for_phase(phase)} {phase.value}", style=style)

    def _apply_status_classes(self, status: Static, phase: Phase) -> None:
        for class_name in STATUS_CLASSES:
            status.remove_class(class_name)
        status.add_class(self._status_class_for_phase(phase))
        status.set_class(phase in LOADING_PHASES, "status--pulse")

    @staticmethod
    def _status_class_for_phase(phase: Phase) -> str:
        if phase in LOADING_PHASES:
            return "status--loading"
        return f"status--{phase.value.lower()}"

    @staticmethod
    def _status_style_for_phase(phase: Phase) -> str:
        if phase in LOADING_PHASES:
            return f"bold {WARN}"
        if phase is Phase.READY:
            return f"bold {GOOD}"
        if phase is Phase.DEGRADED:
            return f"bold {WARN}"
        if phase is Phase.ERROR:
            return f"bold {BAD}"
        return MUTED

    @staticmethod
    def _status_icon_for_phase(phase: Phase) -> str:
        if phase in LOADING_PHASES:
            return "●"
        return STATUS_ICONS[phase]

    def _server_url(self, cfg: ModelConfig) -> str:
        return f"http://{cfg.server.host}:{cfg.server.port}"

    def _render_phase_timeline(self) -> Text:
        rows = self._phase_timeline_rows()
        text = Text("Phases", style=f"bold {ACCENT}")
        for marker, phase, elapsed, state in rows:
            style = self._phase_timeline_style(phase, state)
            text.append("\n")
            text.append(marker, style=f"bold {style}")
            text.append(f" {phase.value}", style=style if state == "upcoming" else f"bold {TEXT}")
            text.append(f" {elapsed}", style=MUTED)
        if self.run_started_at is not None:
            overall = self._format_duration(self._clock() - self.run_started_at)
            text.append(f"\nOverall {overall}", style=MUTED)
        return text

    def _phase_timeline_rows(self) -> list[tuple[str, Phase, str, str]]:
        if self.phase is Phase.IDLE and not self.phase_history:
            return [("○", Phase.IDLE, "--", "upcoming")]
        rows = []
        history = set(self.phase_history)
        for phase in WORKFLOW_PHASES:
            if phase is self.phase:
                elapsed = self._format_duration(self._elapsed_for(phase))
                rows.append(("●", phase, elapsed, "current"))
            elif phase in history:
                elapsed = self._format_duration(self._elapsed_for(phase))
                rows.append(("✓", phase, elapsed, "complete"))
            else:
                rows.append(("○", phase, "--", "upcoming"))
        if self.phase in {Phase.DEGRADED, Phase.STOPPED, Phase.ERROR}:
            elapsed = self._format_duration(self._elapsed_for(self.phase))
            rows.append(("●", self.phase, elapsed, "current"))
        return rows

    @staticmethod
    def _phase_timeline_style(phase: Phase, state: str) -> str:
        if state == "complete":
            return GOOD
        if state == "upcoming":
            return MUTED
        if phase is Phase.READY:
            return GOOD
        if phase is Phase.DEGRADED:
            return WARN
        if phase is Phase.ERROR:
            return BAD
        if phase in LOADING_PHASES:
            return WARN
        return MUTED

    def _elapsed_for(self, phase: Phase) -> float:
        elapsed = self.phase_elapsed.get(phase, 0.0)
        if phase is self.phase and self.current_phase_started_at is not None:
            elapsed += self._clock() - self.current_phase_started_at
        return elapsed

    @staticmethod
    def _format_duration(seconds: float) -> str:
        total_seconds = max(0, int(seconds))
        minutes, secs = divmod(total_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours:02}:{minutes:02}:{secs:02}"
        return f"{minutes:02}:{secs:02}"

    def _reset_run_state(self) -> None:
        self.ready_url = None
        self.served_models = []
        self.health_detail = ""
        self.warning_lines = []
        self.run_started_at = None
        self.current_phase_started_at = None
        self.phase_elapsed = {}
        self.phase_history = []
        self.phase_timeline_text = "Phases\n○ IDLE"
        self._set_error_text("")
        self._refresh_chrome()

    def _record_warnings(self, warnings: list[str]) -> None:
        if not warnings:
            return
        self.warning_lines.extend(warnings)
        self._set_error_text("\n".join(self.warning_lines), style=f"bold {WARN}")
        for warning in warnings:
            self._write_log(f"WARNING {warning}", "WARNING")

    def _set_error_text(
        self,
        text: str,
        *,
        style: str | None = None,
        jump_text: str = "",
    ) -> None:
        self.error_text = text
        self.error_jump_text = jump_text
        render_style = style or f"bold {BAD}"
        self.query_one("#error", Static).update(Text(text, style=render_style) if text else "")

    def _set_error_banner(self, kind: ErrorKind) -> None:
        self._set_error_text(
            self._render_error_banner(kind),
            jump_text=self._error_jump_target(),
        )

    def _render_error_banner(self, kind: ErrorKind) -> str:
        excerpt = self.fsm.error_excerpt or ""
        guidance = ERROR_GUIDANCE.get(kind, "Check the last log lines.")
        jump_hint = ""
        if self._error_jump_target():
            jump_hint = "\nJump: Ctrl+P → Jump to error log line"
        if excerpt:
            return f"{kind.value}: {guidance}\n{excerpt}{jump_hint}"
        return f"{kind.value}: {guidance}{jump_hint}"

    def _error_jump_target(self) -> str:
        excerpt = self.fsm.error_excerpt.strip() if self.fsm.error_excerpt else ""
        if not excerpt:
            return ""
        return re.sub(r"^(?:CRITICAL|ERROR|WARNING|INFO|DEBUG)\s+", "", excerpt)

    def _debug_event(self, event: str, **payload: object) -> None:
        if self.debug_log_path is None:
            return
        record = {
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "event": event,
            "payload": payload,
        }
        try:
            self.debug_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.debug_log_path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        except OSError:
            return

    async def _poll_gpu_panel(self) -> None:
        while True:
            await asyncio.sleep(self._gpu_interval_seconds)
            await self._sample_gpu_panel_once()

    async def _sample_gpu_panel_once(self) -> None:
        try:
            result = await asyncio.to_thread(self._gpu_sampler)
        except Exception as exc:
            result = GpuPollResult(
                [], note=f"GPU stats unavailable: {exc}", unavailable=True
            )
        self._render_gpu_panel(result)

    def _render_gpu_panel(self, result: GpuPollResult) -> None:
        if result.unavailable:
            self.gpu_panel_text = result.note
            self.query_one("#gpu", Static).update(Text(result.note, style=MUTED))
            return
        lines = []
        renderable = Text()
        if result.note:
            lines.append(result.note)
            renderable.append(result.note, style=MUTED)
            renderable.append("\n")
        for sample in result.samples:
            details = [
                f"{sample.visible_index}",
                sample.name,
                f"[{sample.uuid}]",
                f"{sample.memory_used_mb}/{sample.memory_total_mb}MB",
            ]
            if sample.utilization_percent is not None:
                details.append(f"{sample.utilization_percent}%")
            if sample.temperature_c is not None:
                details.append(f"{sample.temperature_c}C")
            if sample.power_w is not None:
                details.append(f"{sample.power_w}W")
            if sample.mig_instance_id:
                details.append(f"MIG {sample.mig_instance_id}")
            lines.append(" ".join(details))
            renderable.append(f"{sample.visible_index}", style=f"bold {ACCENT}")
            renderable.append(f" {sample.name} ", style=f"bold {TEXT}")
            renderable.append(
                self._memory_bar(sample.memory_used_mb, sample.memory_total_mb),
                style=self._gpu_bar_style(sample),
            )
            renderable.append(
                f" {sample.memory_used_mb}/{sample.memory_total_mb}MB",
                style=MUTED,
            )
            if sample.utilization_percent is not None:
                renderable.append(f" {sample.utilization_percent}%", style=GOOD)
            if sample.temperature_c is not None:
                renderable.append(f" {sample.temperature_c}C", style=WARN)
            if sample.power_w is not None:
                renderable.append(f" {sample.power_w}W", style=MUTED)
            if sample.mig_instance_id:
                renderable.append(f" MIG {sample.mig_instance_id}", style=ACCENT)
            renderable.append("\n")
        self.gpu_panel_text = "\n".join(lines) if lines else "GPU stats unavailable"
        if not result.samples:
            renderable = Text("GPU stats unavailable", style=MUTED)
        self.query_one("#gpu", Static).update(renderable)

    @staticmethod
    def _memory_bar(used_mb: int, total_mb: int, width: int = 8) -> str:
        if total_mb <= 0:
            return "▱" * width
        filled = max(0, min(width, round(width * used_mb / total_mb)))
        if used_mb > 0 and filled == 0:
            filled = 1
        return "▰" * filled + "▱" * (width - filled)

    @staticmethod
    def _gpu_bar_style(sample: GpuSample) -> str:
        if sample.memory_total_mb <= 0:
            return MUTED
        ratio = sample.memory_used_mb / sample.memory_total_mb
        if ratio >= 0.9:
            return BAD
        if ratio >= 0.75:
            return WARN
        return GOOD

    def _runs_dirs(self) -> list[Path]:
        dirs = {item.config.run_artifacts_dir for item in self.registry.valid}
        return sorted(dirs)
