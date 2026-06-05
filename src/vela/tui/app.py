from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import time
import uuid
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

# The canonical Figma screens depend on truecolor hex tokens. Textual reads this
# during import, so set the default before importing any Textual modules.
if "NO_COLOR" not in os.environ:
    os.environ.setdefault("TEXTUAL_COLOR_SYSTEM", "truecolor")

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult, ScreenStackError, SystemCommand
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import ProgressBar, RichLog, Static
from textual.worker import Worker, WorkerState

from vela.agent.local import AgentEvent, TargetCallError
from vela.config.loader import ConfigRegistry, InvalidConfig, ValidConfig
from vela.config.schema import ModelConfig
from vela.config.targets import (
    TargetConfig,
    TransportKind,
    load_targets_file,
    remove_target_file,
    upsert_target_file,
)
from vela.engine.command_builder import CommandBuildResult
from vela.engine.log_sink import LogRecord, level_for_line
from vela.engine.phases import ErrorKind, Phase, PhaseFSM
from vela.engine.profile import (
    VllmProfileError,
    bundled_profile,
)
from vela.messages import (
    AgentError,
    EngineError,
    GpuStatsUnavailable,
    GpuStatsUpdated,
    HealthChanged,
    LogLineCommitted,
    PhaseChanged,
    ProcessExited,
    ProgressUpdated,
    ServerReady,
    from_log_record,
)
from vela.monitoring.gpu import (
    GpuPollResult,
    GpuSample,
)
from vela.monitoring.health import HealthEvent
from vela.transport.factory import target_client_for_config
from vela.tui.screens.adopt_build import AdoptBuildScreen
from vela.tui.screens.build_manager import BuildManagerScreen
from vela.tui.screens.config_picker import ConfigPickerScreen
from vela.tui.screens.confirm import ConfirmScreen
from vela.tui.screens.create_build import CreateBuildScreen
from vela.tui.screens.download_model import DownloadModelScreen
from vela.tui.screens.flag_manager import FlagManagerScreen
from vela.tui.screens.help import HelpScreen
from vela.tui.screens.log_prompt import LogPromptScreen
from vela.tui.screens.model_manager import ModelManagerScreen
from vela.tui.screens.pin_model import PinModelScreen
from vela.tui.screens.target_edit import TargetEditScreen
from vela.tui.screens.target_manager import (
    TargetManagerRequest,
    TargetManagerScreen,
)
from vela.tui.theme import (
    ACCENT,
    ACCENT_SURFACE,
    BAD,
    BAD_SURFACE,
    GOOD,
    GOOD_SURFACE,
    MUTED,
    MUTED_SURFACE,
    TEXT,
    WARN,
    WARN_SURFACE,
)

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
LEVEL_FILTER_ALIASES = {
    "CRIT": "CRITICAL",
    "CRITICAL": "CRITICAL",
    "ERR": "ERROR",
    "ERROR": "ERROR",
    "WARN": "WARNING",
    "WARNING": "WARNING",
    "INFO": "INFO",
    "DEBUG": "DEBUG",
}

WIDGET_MISSING_EXCEPTIONS = (NoMatches, ScreenStackError)
SEARCH_HIGHLIGHT_STYLE = "black on yellow"
PROGRESS_PERCENT_RE = re.compile(r"(?P<percent>\d{1,3}(?:\.\d+)?)%")
PROGRESS_TRACK_WIDTH = 72
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
WIRE_EVENT_META_KEYS = {"event", "run_id", "job_id", "seq", "ts", "mono"}


def _looks_secret_env_key(key: str) -> bool:
    upper = key.upper()
    return "TOKEN" in upper or "KEY" in upper or "SECRET" in upper or "AUTH" in upper


def _config_registry_from_agent_payload(payload: dict[str, Any]) -> ConfigRegistry:
    return ConfigRegistry(
        valid=[
            ValidConfig(
                path=Path(item["path"]),
                config=ModelConfig.model_validate(item["config"]),
                warnings=list(item.get("warnings", [])),
            )
            for item in payload.get("valid", [])
        ],
        invalid=[
            InvalidConfig(
                path=Path(item["path"]),
                errors=list(item.get("errors", [])),
                raw_name=item.get("raw_name"),
            )
            for item in payload.get("invalid", [])
        ],
    )


def _command_build_result_from_agent_payload(payload: dict[str, Any]) -> CommandBuildResult:
    return CommandBuildResult(
        argv=list(payload["argv"]),
        env=dict(payload["env"]),
        cwd=Path(payload["cwd"]),
        warnings=list(payload.get("warnings", [])),
        metadata=dict(payload.get("metadata", {})),
        preview=str(payload.get("preview", "")),
    )


def _gpu_poll_result_from_agent_payload(payload: dict[str, Any]) -> GpuPollResult:
    return GpuPollResult(
        samples=[
            GpuSample(
                visible_index=int(item["visible_index"]),
                uuid=str(item["uuid"]),
                name=str(item["name"]),
                memory_used_mb=int(item["memory_used_mb"]),
                memory_total_mb=int(item["memory_total_mb"]),
                utilization_percent=_optional_int(item.get("utilization_percent")),
                temperature_c=_optional_int(item.get("temperature_c")),
                power_w=_optional_int(item.get("power_w")),
                mig_instance_id=(
                    str(item["mig_instance_id"])
                    if item.get("mig_instance_id") is not None
                    else None
                ),
            )
            for item in payload.get("samples", [])
        ],
        note=str(payload.get("note") or ""),
        unavailable=bool(payload.get("unavailable", False)),
    )


def _optional_int(value: object) -> int | None:
    return int(value) if value is not None else None


def _phase_fsm_from_agent_metadata(metadata: dict[str, Any] | None) -> PhaseFSM:
    profile_name = str((metadata or {}).get("vllm_version_profile") or "current")
    return PhaseFSM(bundled_profile(profile_name))


def _error_kind_from_agent_payload(value: object) -> ErrorKind:
    try:
        return ErrorKind(str(value))
    except ValueError:
        return ErrorKind.CONFIG_INVALID


def _message_from_agent_event(
    event: AgentEvent,
    *,
    agent_mono: float | None = None,
) -> LogLineCommitted | ProgressUpdated | PhaseChanged | None:
    payload = event.payload
    if event.kind == "log":
        return LogLineCommitted(
            str(payload.get("text", "")),
            _optional_str(payload.get("level")),
            feed_phase=False,
        )
    if event.kind == "progress":
        return ProgressUpdated(str(payload.get("text", "")))
    if event.kind == "phase":
        error_kind = None
        if payload.get("error_kind") is not None:
            error_kind = _error_kind_from_agent_payload(payload.get("error_kind"))
        return PhaseChanged(
            Phase(str(payload["phase"])),
            error_kind=error_kind,
            error_excerpt=_optional_str(payload.get("error_excerpt")),
            agent_mono=agent_mono,
        )
    return None


def _message_from_wire_event(
    event: dict[str, Any],
) -> (
    LogLineCommitted
    | ProgressUpdated
    | PhaseChanged
    | HealthChanged
    | ServerReady
    | GpuStatsUpdated
    | GpuStatsUnavailable
    | EngineError
    | AgentError
    | None
):
    kind = str(event.get("event", ""))
    payload = {
        key: value for key, value in event.items() if key not in WIRE_EVENT_META_KEYS
    }
    if kind in {"log", "progress", "phase"}:
        return _message_from_agent_event(
            AgentEvent(kind=kind, run_id=str(event.get("run_id", "")), payload=payload),
            agent_mono=_optional_float(event.get("mono")) if kind == "phase" else None,
        )
    if kind == "health":
        error_kind = None
        if payload.get("error_kind") is not None:
            error_kind = _error_kind_from_agent_payload(payload.get("error_kind"))
        return HealthChanged(
            ready=bool(payload.get("ready")),
            detail=str(payload.get("detail", "")),
            models=[str(model) for model in payload.get("models") or []],
            error_kind=error_kind,
            reachable_url=_optional_str(payload.get("reachable_url")),
            feed_phase=False,
        )
    if kind == "ready":
        return ServerReady(
            [str(model) for model in payload.get("models") or []],
            reachable_url=_optional_str(payload.get("reachable_url")),
            feed_phase=False,
        )
    if kind == "gpu":
        if "samples" not in payload and "devices" in payload:
            payload["samples"] = payload["devices"]
        result = _gpu_poll_result_from_agent_payload(payload)
        if result.unavailable:
            return GpuStatsUnavailable(result.note or "GPU stats unavailable")
        return GpuStatsUpdated(result)
    if kind == "job_progress":
        if payload.get("kind") == "committed":
            return LogLineCommitted(
                str(payload.get("text", "")),
                _optional_str(payload.get("level")),
                feed_phase=False,
            )
        return ProgressUpdated(str(payload.get("text", "")))
    if kind == "job_done":
        detail = str(payload.get("detail") or "")
        if bool(payload.get("ok")):
            return ProgressUpdated(detail or "Job complete")
        if str(payload.get("error_kind") or "") == "cancelled":
            return ProgressUpdated(detail or "Job cancelled")
        return EngineError(
            _error_kind_from_agent_payload(payload.get("error_kind")),
            detail or "Job failed",
        )
    if kind == "agent_error":
        return AgentError(
            str(payload.get("detail") or "Target agent error"),
            fatal=bool(payload.get("fatal")),
        )
    if kind == "exited" and payload.get("phase") is not None:
        return PhaseChanged(
            Phase(str(payload["phase"])),
            agent_mono=_optional_float(event.get("mono")),
        )
    return None


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    return float(value)


def _controller_host_from_ssh_target(host: str) -> str:
    value = str(host).strip()
    if "@" in value:
        value = value.rsplit("@", 1)[1]
    if value.startswith("["):
        closing_bracket = value.find("]")
        if closing_bracket > 0:
            return value[1:closing_bracket]
    if value.count(":") == 1:
        hostname, _separator, maybe_port = value.rpartition(":")
        if hostname and maybe_port.isdigit():
            return hostname
    return value


def _url_netloc(host: str, port: int | None) -> str:
    rendered_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    if port is None:
        return rendered_host
    return f"{rendered_host}:{port}"


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
    ErrorKind.API_KEY_AUTH: "Check server.api_key/VLLM_API_KEY for the running server.",
    ErrorKind.COMMAND_NOT_FOUND: "install vLLM or set command.entrypoint: module.",
    ErrorKind.CONFIG_INVALID: "Fix the config or choose a compatible vLLM version_profile.",
    ErrorKind.CRASHED: "Check the last log lines and resolved command.",
    ErrorKind.TIMED_OUT: "Check /health, model load progress, GPU memory, and network binding.",
}

DEFAULT_MAX_LOG_LINES = 50_000
DEFAULT_LOG_BATCH_INTERVAL_SECONDS = 0.025


class VelaApp(App):
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
    #target-segment {
        width: 18;
        color: #8ba4ae;
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
    #status-badge {
        width: 26;
        height: 3;
        border: solid #526a75;
        background: #14202b;
        align: center middle;
        padding: 0 1;
    }
    #status-dot {
        width: 3;
        height: 1;
        content-align: center middle;
    }
    #status-label {
        width: 1fr;
        height: 1;
        content-align: left middle;
        text-style: bold;
    }
    #status-badge.status--idle {
        color: #8ba4ae;
        background: #14202b;
    }
    #status-badge.status--loading {
        color: #f6c85f;
        border: solid #f6c85f;
        background: #2b2410;
    }
    #status-badge.status--ready {
        color: #67e8a5;
        border: solid #67e8a5;
        background: #0e2a21;
    }
    #status-badge.status--degraded {
        color: #f6c85f;
        border: solid #f6c85f;
        background: #2b2410;
    }
    #status-badge.status--error {
        color: #ff6b6b;
        border: solid #ff6b6b;
        background: #351b1f;
    }
    #status-badge.status--stopped {
        color: #8ba4ae;
        background: #14202b;
    }
    #status-badge.status--pulse { text-style: bold; }
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
    #progress { display: none; width: 0; height: 1; }
    #progress-track { width: 1fr; height: 1; }
    #progress-percent {
        width: 8;
        height: 1;
        content-align: right middle;
    }
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
        ("t", "targets", "Targets"),
        ("b", "builds", "Builds"),
        ("F", "flags", "Flags"),
        ("m", "models", "Models"),
        ("R", "reconnect", "Reconnect"),
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
        gpu_interval_seconds: float = 2.0,
        max_log_lines: int = DEFAULT_MAX_LOG_LINES,
        log_batch_interval_seconds: float = DEFAULT_LOG_BATCH_INTERVAL_SECONDS,
        debug_log_path: str | Path | None = None,
        target_client: Any | None = None,
        target_name: str = "local",
        target_ping_interval_seconds: float | None = 30.0,
        target_ping_timeout_seconds: float = 15.0,
        launch_overrides: dict[str, str | None] | None = None,
    ) -> None:
        super().__init__()
        self.configs_dir = Path(configs_dir) if configs_dir is not None else None
        self._launch_overrides = {
            key: str(value)
            for key, value in (launch_overrides or {}).items()
            if value is not None
        }
        self.target_name = target_name
        target_config = TargetConfig(name="local")
        if target_client is None:
            target_config = (
                TargetConfig(name="local")
                if target_name == "local"
                else load_targets_file().by_name(target_name)
            )
            target_client = target_client_for_config(target_config)
        elif target_name != "local":
            try:
                target_config = load_targets_file().by_name(target_name)
            except (KeyError, ValueError):
                target_config = TargetConfig(name=target_name)
        self._target_client = target_client
        self._target_config = target_config
        self.target_connection_state = "disconnected"
        self.target_connection_detail = ""
        self.target_agent_restarted = False
        self._target_agent_info: dict[str, Any] = {}
        self._target_last_seen_at: str | None = None
        self._target_has_connected_once = False
        self._target_daemon_start_ts: str | None = None
        self._target_last_event_seq_by_run: dict[str, int] = {}
        self._target_last_log_cursor_by_run: dict[str, dict[str, int]] = {}
        self._target_ping_interval_seconds = target_ping_interval_seconds
        self._target_ping_timeout_seconds = target_ping_timeout_seconds
        self._target_reconnect_backoff_initial_seconds = 0.1
        self._target_reconnect_backoff_cap_seconds = 10.0
        self._target_reconnect_backoff_seconds = (
            self._target_reconnect_backoff_initial_seconds
        )
        self._clock = clock
        self._gpu_interval_seconds = gpu_interval_seconds
        self._max_log_lines = max(1, max_log_lines)
        self._log_batch_interval_seconds = max(0.0, log_batch_interval_seconds)
        self.debug_log_path = Path(debug_log_path) if debug_log_path is not None else None
        self.registry = ConfigRegistry()
        self.config_summary = ""
        self.selected_config_preview = ""
        self.selected_config_metadata: dict[str, Any] = {}
        self._config_preview_cache: dict[str, str] = {}
        self.paused = False
        self.wrap = False
        self.filter_text = ""
        self.search_text = ""
        self.fsm = PhaseFSM(bundled_profile("current"))
        self.current_config: ModelConfig | None = None
        self.current_run_id: str | None = None
        self.log_lines: list[str] = []
        self.log_records: list[tuple[str, str | None]] = []
        self.visible_log_lines: list[str] = []
        self.search_matches: list[str] = []
        self._pending_log_writes: list[tuple[str, str | None]] = []
        self._pending_build_remove: dict[str, str] | None = None
        self._pending_model_remove: dict[str, str] | None = None
        self._pending_target_remove: dict[str, str] | None = None
        self._active_job_id: str | None = None
        self._active_job_label: str = ""
        self._log_flush_scheduled = False
        self.last_copied_url: str | None = None
        self.reattached_run_id: str | None = None
        self.detached_run_summaries: list[dict[str, str]] = []
        self.status_text = "○ IDLE"
        self.error_text = ""
        self.error_jump_text = ""
        self.responsive_mode = "wide"
        self.warning_lines: list[str] = []
        self.ready_url: str | None = None
        self.served_models: list[str] = []
        self.health_detail = ""
        self.run_started_at: float | None = None
        self.run_started_uses_agent_mono = False
        self.current_phase_started_at: float | None = None
        self.current_phase_started_uses_agent_mono = False
        self._intentional_shutdown_pids: set[int] = set()
        self.phase_elapsed: dict[Phase, float] = {}
        self.phase_history: list[Phase] = []
        self.phase_timeline_text = "Phases\n○ IDLE"
        self.gpu_panel_text = "GPU stats unavailable"
        self.progress_text = ""

    def compose(self) -> ComposeResult:
        with Vertical(id="terminal-shell"):
            with Horizontal(id="top-chrome"):
                yield Static("Vela", id="app-title")
                yield Static(self._render_target_segment(), id="target-segment")
                yield Static("", id="active-model")
                with Horizontal(id="status-badge", classes="status--idle"):
                    yield Static(self._render_status_dot(Phase.IDLE), id="status-dot")
                    yield Static(
                        self._render_status_label(Phase.IDLE),
                        id="status-label",
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
                        yield Static("", id="progress-text")
                        with Horizontal(id="progress-line"):
                            yield ProgressBar(total=None, show_eta=False, id="progress")
                            yield Static("", id="progress-track")
                            yield Static("", id="progress-percent")
            yield Static(self._render_footer_bindings(), id="footer-bindings")

    async def on_mount(self) -> None:
        self.registry = await self._load_registry_from_agent()
        if self.current_config is None and self.registry.valid:
            self.current_config = self.registry.valid[0].config
            await self._refresh_selected_config_preview()
        self.config_summary = self._render_config_summary_plain()
        self.query_one("#configs-title", Static).update(self._render_configs_title())
        self.query_one("#configs", Static).update(self._render_config_summary())
        self._refresh_sidebar_overlay()
        self._refresh_dashboard_shell()
        self._apply_responsive_layout(self.size.width)
        self._clear_progress()
        self.run_worker(
            self._stream_gpu_panel(),
            name="gpu",
            group="monitoring",
            exclusive=True,
            exit_on_error=False,
        )
        self.run_worker(
            self._refresh_detached_runs(),
            name="detached-discovery",
            group="detached-discovery",
            exclusive=True,
            exit_on_error=False,
        )
        self.run_worker(
            self._poll_target_keepalive(),
            name="target-keepalive",
            group="target-connection",
            exclusive=True,
            exit_on_error=False,
        )
        self._write_log("INFO Vela ready")
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
        yield SystemCommand(
            "Manage targets", "View and switch controller targets", self.action_targets
        )
        yield SystemCommand(
            "Agent info", "View active target agent details", self.action_targets
        )
        try:
            targets = load_targets_file().targets
        except Exception:
            targets = []
        for target in targets:
            if target.name == self.target_name:
                continue
            yield SystemCommand(
                f"Switch target: {target.name}",
                f"Connect to target {target.name}",
                lambda selected=target.name: self._handle_target_manager_selection(selected),
            )
        yield SystemCommand(
            "Manage vLLM builds", "View and select target-local vLLM builds", self.action_builds
        )
        yield SystemCommand(
            "Manage vLLM flags",
            "Inspect flags for the selected config and build",
            self.action_flags,
        )
        yield SystemCommand(
            "Manage models", "View target-local model catalog entries", self.action_models
        )
        yield SystemCommand(
            "Reconnect agent", "Reconnect to the active target", self.action_reconnect
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
        if self._has_reattached_run():
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
                f"Select and launch {name}",
                lambda selected=name: self._load_config_from_palette(selected),
            )
        for run in self.detached_run_summaries:
            run_id = run["run_id"]
            config_name = run["config_name"]
            yield SystemCommand(
                f"Reattach detached run: {config_name}",
                f"Resume tailing {config_name}",
                lambda selected_run_id=run_id: self.run_worker(
                    self._reattach_target_detached_run(selected_run_id),
                    name="reattach",
                    group="engine",
                    exclusive=True,
                ),
            )

    def _load_config_from_palette(self, name: str) -> None:
        self.select_config(name)
        self.action_load()

    def action_help(self) -> None:
        self.push_screen(HelpScreen(id="help"))

    def action_targets(self) -> None:
        try:
            registry = load_targets_file()
        except Exception as exc:
            self._set_error_text(f"Target registry unavailable: {exc}", style=f"bold {BAD}")
            return
        self.push_screen(
            TargetManagerScreen(
                registry,
                active_target=self.target_name,
                connection_state=self.target_connection_state,
                connection_detail=self.target_connection_detail,
                agent_info=self._target_agent_info,
                last_seen=self._target_last_seen_at,
                active_runs=self.detached_run_summaries,
                gpu_summary=self.gpu_panel_text,
            ),
            callback=self._handle_target_manager_selection,
        )

    def _handle_target_manager_selection(
        self, selection: str | TargetManagerRequest | None
    ) -> None:
        if isinstance(selection, TargetManagerRequest):
            if selection.action == "new":
                self._open_target_edit(None)
            elif selection.action == "edit" and selection.target_name is not None:
                self._open_target_edit(selection.target_name)
            elif selection.action == "remove" and selection.target_name is not None:
                self._confirm_remove_target(selection.target_name)
            return
        target_name = selection
        if not target_name or target_name == self.target_name:
            return
        if self._attached_run_is_alive() or self._has_reattached_run():
            self._set_error_text(
                "Stop or detach the active run before switching targets",
                style=f"bold {WARN}",
            )
            self.notify(
                "Stop or detach the active run before switching targets",
                severity="warning",
            )
            return
        self.run_worker(
            self._switch_target(target_name),
            name="target-switch",
            group="target-switch",
            exclusive=True,
            exit_on_error=False,
        )

    def _open_target_edit(self, target_name: str | None) -> None:
        if target_name is None:
            self.push_screen(TargetEditScreen(), callback=self._handle_target_edit)
            return
        if target_name == "local":
            self._set_error_text("The local target is implicit and cannot be edited")
            return
        try:
            target = load_targets_file().by_name(target_name)
        except Exception as exc:
            self._set_error_text(f"Unable to edit target: {exc}", style=f"bold {BAD}")
            return
        self.push_screen(TargetEditScreen(target), callback=self._handle_target_edit)

    def _handle_target_edit(self, target: TargetConfig | None) -> None:
        if target is None:
            self.action_targets()
            return
        try:
            upsert_target_file(target)
        except ValueError as exc:
            self._set_error_text(f"Unable to save target: {exc}", style=f"bold {BAD}")
            return
        self.notify(f"Saved target: {target.name}")
        if target.name == self.target_name:
            self._target_config = target
        self.action_targets()

    def _confirm_remove_target(self, target_name: str) -> None:
        if target_name == "local":
            self._set_error_text("The local target is implicit and cannot be removed")
            return
        if target_name == self.target_name:
            self._set_error_text("Switch targets before removing the active target")
            return
        self._pending_target_remove = {"target": target_name}
        self.push_screen(
            ConfirmScreen(
                (
                    f"Remove target {target_name}?"
                    "\n\nThis deletes the controller-side target registry entry."
                ),
                title="Remove target",
                confirm_label="Remove",
                confirm_action="confirm_remove_target",
            )
        )

    def confirm_remove_target(self) -> None:
        if self.screen.id == "confirm":
            self.pop_screen()
        pending = self._pending_target_remove
        self._pending_target_remove = None
        if pending is None:
            return
        target_name = pending["target"]
        try:
            remove_target_file(target_name)
        except ValueError as exc:
            self._set_error_text(f"Unable to remove target: {exc}", style=f"bold {BAD}")
            return
        self.notify(f"Removed target: {target_name}")

    def action_builds(self) -> None:
        if self._target_capability_blocked("list_builds", "vLLM builds"):
            return
        self.run_worker(
            self._open_build_manager(),
            name="build-manager",
            group="build-manager",
            exclusive=True,
            exit_on_error=False,
        )

    def action_flags(self) -> None:
        if self._target_capability_blocked("update_config_flags", "vLLM flags"):
            return
        if self.current_config is None:
            self._set_error_text("Select a config before opening Flag Manager")
            return
        self.run_worker(
            self._open_flag_manager(),
            name="flag-manager",
            group="flag-manager",
            exclusive=True,
            exit_on_error=False,
        )

    async def _open_flag_manager(self) -> None:
        if self.current_config is None:
            return
        await self._refresh_selected_config_preview()
        self.push_screen(
            FlagManagerScreen(
                self.current_config,
                preview=self.selected_config_preview,
                metadata=self.selected_config_metadata,
                preview_resolver=self._preview_flag_manager_draft,
            ),
            callback=self._handle_flag_manager_selection,
        )

    def _handle_flag_manager_selection(self, selection: object) -> None:
        if not isinstance(selection, dict) or selection.get("action") != "save_flags":
            return
        self.run_worker(
            self._save_flag_manager_changes(selection),
            name="flag-save",
            group="flag-manager",
            exclusive=True,
            exit_on_error=False,
        )

    async def _open_build_manager(self) -> None:
        try:
            result = await self._target_call(
                "list_builds",
                {"configs_dir": str(self.configs_dir)},
            )
        except TargetCallError as exc:
            self._set_error_text(f"Unable to list builds: {exc}", style=f"bold {BAD}")
            return
        self.push_screen(
            BuildManagerScreen(result),
            callback=self._handle_build_manager_selection,
        )

    async def _save_flag_manager_changes(self, selection: dict[str, Any]) -> None:
        name = _optional_str(selection.get("name"))
        if name is None:
            return
        engine = selection.get("engine")
        extra_args = selection.get("extra_args")
        try:
            result = await self._target_call(
                "update_config_flags",
                {
                    "name": name,
                    "configs_dir": str(self.configs_dir),
                    "engine": engine if isinstance(engine, dict) else {},
                    "extra_args": extra_args if isinstance(extra_args, list) else [],
                },
            )
        except TargetCallError as exc:
            self._set_error_text(f"Unable to save flags: {exc}", style=f"bold {BAD}")
            return
        config_payload = result.get("config")
        if isinstance(config_payload, dict):
            self.current_config = ModelConfig.model_validate(config_payload)
        self.registry = await self._load_registry_from_agent()
        if self.current_config is not None:
            try:
                self.current_config = self.registry.by_name(self.current_config.name)
            except KeyError:
                pass
            await self._refresh_selected_config_preview()
        self._refresh_chrome()
        self.notify(f"Saved flags: {name}")

    async def _preview_flag_manager_draft(
        self, selection: dict[str, Any]
    ) -> dict[str, Any]:
        name = _optional_str(selection.get("name"))
        if name is None:
            return {
                "preview": "Preview unavailable: missing config name",
                "warnings": [],
                "metadata": {},
            }
        params: dict[str, Any] = self._agent_params(
            name=name,
            configs_dir=self.configs_dir,
        )
        engine = selection.get("engine")
        if isinstance(engine, dict):
            params["engine"] = dict(engine)
        extra_args = selection.get("extra_args")
        if isinstance(extra_args, list):
            params["extra_args"] = list(extra_args)
        try:
            return await self._target_call("preview", params)
        except TargetCallError as exc:
            return {
                "preview": f"Preview unavailable: {exc}",
                "warnings": [],
                "metadata": {},
            }

    def _handle_build_manager_selection(self, selection: object) -> None:
        if not selection:
            return
        if isinstance(selection, dict):
            action = selection.get("action")
            if action == "create_build":
                self.run_worker(
                    self._open_create_build_form(),
                    name="build-create-form",
                    group="build-manager",
                    exclusive=True,
                    exit_on_error=False,
                )
            elif action == "adopt_build":
                self.push_screen(
                    AdoptBuildScreen(),
                    callback=self._handle_adopt_build_submission,
                )
            elif action == "verify_build":
                build = _optional_str(selection.get("build"))
                if build is not None:
                    self.run_worker(
                        self._verify_build(build),
                        name="build-verify",
                        group="build-manager",
                        exclusive=True,
                        exit_on_error=False,
                    )
            elif action == "repair_build":
                build = _optional_str(selection.get("build"))
                if build is not None:
                    self.run_worker(
                        self._repair_build(build),
                        name="build-repair",
                        group="build-manager",
                        exclusive=True,
                        exit_on_error=False,
                    )
            elif action == "flags":
                self.action_flags()
            elif action == "remove_build":
                self._confirm_remove_build(selection)
            return
        build = _optional_str(selection)
        if not build:
            return
        self.run_worker(
            self._select_build(build),
            name="build-select",
            group="build-manager",
            exclusive=True,
            exit_on_error=False,
        )

    async def _select_build(self, build: str) -> None:
        try:
            result = await self._target_call("select_build", {"build": build})
        except TargetCallError as exc:
            self._set_error_text(f"Unable to select build: {exc}", style=f"bold {BAD}")
            return
        label = _optional_str(result.get("label")) or build
        self.notify(f"Selected build: {label}")
        if self.current_config is not None:
            await self._refresh_selected_config_preview()
        self._refresh_target_backed_views()

    async def _verify_build(self, build: str) -> None:
        try:
            result = await self._target_call("verify_build", {"build": build})
        except TargetCallError as exc:
            self._set_error_text(f"Unable to verify build: {exc}", style=f"bold {BAD}")
            return
        label = _optional_str(result.get("label")) or build
        status = _optional_str(result.get("status")) or "verified"
        self.notify(f"Verified build: {label} ({status})")
        if self.current_config is not None:
            await self._refresh_selected_config_preview()
        self._refresh_target_backed_views()

    async def _repair_build(self, build: str) -> None:
        try:
            result = await self._target_call("repair_build", {"build": build})
        except TargetCallError as exc:
            self._set_error_text(f"Unable to repair build: {exc}", style=f"bold {BAD}")
            return
        label = _optional_str(result.get("label")) or build
        detail = _optional_str(result.get("detail")) or _optional_str(result.get("status"))
        suffix = f" ({detail})" if detail else ""
        self.notify(f"Repaired build: {label}{suffix}")
        if self.current_config is not None:
            await self._refresh_selected_config_preview()
        self._refresh_target_backed_views()

    def _confirm_remove_build(self, selection: dict[str, Any]) -> None:
        build = _optional_str(selection.get("build"))
        if build is None:
            return
        label = _optional_str(selection.get("label")) or build
        paths = selection.get("paths") if isinstance(selection.get("paths"), dict) else {}
        executable = _optional_str(paths.get("executable"))
        target_label = self._target_label()
        message = (
            f"Remove build {label} on {target_label}?"
            f"\n\nThis deletes target-local build artifacts on {target_label}."
        )
        if executable:
            message += f"\nExecutable: {executable}"
        self._pending_build_remove = {"build": build, "label": label}
        self.push_screen(
            ConfirmScreen(
                message,
                title="Remove build",
                confirm_label="Remove",
                confirm_action="confirm_remove_build",
            )
        )

    def confirm_remove_build(self) -> None:
        if self.screen.id == "confirm":
            self.pop_screen()
        pending = self._pending_build_remove
        self._pending_build_remove = None
        if pending is None:
            return
        self.run_worker(
            self._remove_build(pending["build"], pending["label"]),
            name="build-remove",
            group="build-manager",
            exclusive=True,
            exit_on_error=False,
        )

    async def _remove_build(self, build: str, label: str) -> None:
        try:
            result = await self._target_call(
                "remove_build",
                {"build": build, "configs_dir": str(self.configs_dir)},
            )
        except TargetCallError as exc:
            self._set_error_text(f"Unable to remove build: {exc}", style=f"bold {BAD}")
            return
        removed_label = _optional_str(result.get("label")) or label
        self.notify(f"Removed build: {removed_label}")
        if self.current_config is not None:
            await self._refresh_selected_config_preview()
        self._refresh_target_backed_views()

    def _handle_create_build_submission(self, params: dict[str, Any] | None) -> None:
        if not params:
            return
        self.run_worker(
            self._create_build(params, reopen_form_on_uv_failure=True),
            name="build-create",
            group="build-manager",
            exclusive=True,
            exit_on_error=False,
        )

    async def _open_create_build_form(self) -> None:
        uv_available: bool | None = None
        try:
            result = await self._target_call(
                "check_build_prerequisites",
                {"method": "pip"},
            )
        except TargetCallError:
            result = {}
        uv_value = result.get("uv_available")
        if isinstance(uv_value, bool):
            uv_available = uv_value
        self.call_later(self._push_create_build_form, {}, "", uv_available)

    async def _create_build(
        self,
        params: dict[str, Any],
        *,
        reopen_form_on_uv_failure: bool = False,
    ) -> None:
        try:
            await self._target_call(
                "check_build_prerequisites",
                _build_prerequisite_params(params),
            )
        except TargetCallError as exc:
            if reopen_form_on_uv_failure and exc.details.get("reason") == "uv-required":
                self._reopen_create_build_form(
                    params,
                    self._render_uv_prerequisite_error(params, exc),
                )
                return
            self._set_error_text(f"Unable to create build: {exc}", style=f"bold {BAD}")
            return
        job_params = dict(params)
        job_params["job_id"] = uuid.uuid4().hex
        await self._run_target_job(
            "create_build",
            job_params,
            error_action="create build",
            incomplete_label="Build creation",
        )

    def _reopen_create_build_form(
        self,
        params: dict[str, Any],
        error_message: str,
    ) -> None:
        self.call_later(self._push_create_build_form, dict(params), error_message, False)

    def _push_create_build_form(
        self,
        params: dict[str, Any],
        error_message: str,
        uv_available: bool | None = None,
    ) -> None:
        self.push_screen(
            CreateBuildScreen(
                initial=params,
                error_message=error_message,
                uv_available=uv_available,
                target_label=self._target_label(),
            ),
            callback=self._handle_create_build_submission,
        )

    def _render_uv_prerequisite_error(
        self,
        params: dict[str, Any],
        exc: TargetCallError,
    ) -> str:
        method = _optional_str(exc.details.get("method")) or _optional_str(params.get("method"))
        method_label = f"{method} " if method else ""
        target = self._target_label()
        return (
            f"{method_label}build creation requires uv on {target}. "
            "Install uv on the target or choose pip, wheel, or git."
        )

    def _handle_adopt_build_submission(self, params: dict[str, Any] | None) -> None:
        if not params:
            return
        self.run_worker(
            self._adopt_build(params),
            name="build-adopt",
            group="build-manager",
            exclusive=True,
            exit_on_error=False,
        )

    async def _adopt_build(self, params: dict[str, Any]) -> None:
        try:
            result = await self._target_call("adopt_build", dict(params))
        except TargetCallError as exc:
            self._set_error_text(f"Unable to adopt build: {exc}", style=f"bold {BAD}")
            return
        label = _optional_str(result.get("label")) or _optional_str(params.get("label"))
        build_id = _optional_str(result.get("build_id")) or _optional_str(params.get("build_id"))
        rendered = label or build_id or "build"
        self.notify(f"Adopted build: {rendered}")
        if self.current_config is not None:
            await self._refresh_selected_config_preview()
        self._refresh_target_backed_views()

    def action_models(self) -> None:
        if self._target_capability_blocked("list_models", "models"):
            return
        self.run_worker(
            self._open_model_manager(),
            name="model-manager",
            group="model-manager",
            exclusive=True,
            exit_on_error=False,
        )

    async def _open_model_manager(self) -> None:
        try:
            result = await self._target_call("list_models", {})
        except TargetCallError as exc:
            self._set_error_text(f"Unable to list models: {exc}", style=f"bold {BAD}")
            return
        self.push_screen(
            ModelManagerScreen(result),
            callback=self._handle_model_manager_selection,
        )

    def _handle_model_manager_selection(self, selection: object) -> None:
        if not isinstance(selection, dict):
            return
        action = selection.get("action")
        if action == "select_model":
            self._select_model_for_active_config(selection)
            return
        if action == "pin_model":
            initial = selection.get("initial") if isinstance(selection.get("initial"), dict) else {}
            self.push_screen(
                PinModelScreen(initial=initial),
                callback=self._handle_pin_model_submission,
            )
            return
        if action == "refresh_models":
            self.run_worker(
                self._refresh_models(),
                name="model-refresh",
                group="model-manager",
                exclusive=True,
                exit_on_error=False,
            )
            return
        if action == "download_unavailable":
            label = _optional_str(selection.get("label")) or "model"
            self._set_error_text(
                f"{label} is launch-time-only; vLLM resolves the URL at launch."
            )
            self.notify(f"{label} is launch-time-only", severity="warning")
            return
        model_ref = _optional_str(selection.get("model_ref"))
        if model_ref is None:
            return
        if action == "download":
            self.push_screen(
                DownloadModelScreen(selection),
                callback=self._handle_download_model_submission,
            )
        elif action == "verify_model":
            self.run_worker(
                self._verify_model(model_ref),
                name="model-verify",
                group="model-manager",
                exclusive=True,
                exit_on_error=False,
            )
        elif action == "remove_model":
            self._confirm_remove_model(selection)

    def _select_model_for_active_config(self, selection: dict[str, Any]) -> None:
        model_ref = _optional_str(selection.get("model_ref"))
        if model_ref is None:
            return
        label = _optional_str(selection.get("label")) or model_ref
        revision = _optional_str(selection.get("revision"))
        self._launch_overrides["model_ref"] = model_ref
        if revision is not None:
            self._launch_overrides["revision"] = revision
        else:
            self._launch_overrides.pop("revision", None)

        metadata = dict(self.selected_config_metadata)
        metadata["model_ref"] = model_ref
        metadata["model_display_name"] = label
        if revision is not None:
            metadata["model_revision"] = revision
        else:
            metadata.pop("model_revision", None)
        cache_state = _optional_str(selection.get("cache_state"))
        if cache_state is not None:
            metadata["model_cache_state"] = cache_state
        if "gated" in selection:
            metadata["model_gated"] = selection["gated"]
        self.selected_config_metadata = metadata
        self._refresh_target_backed_views()
        self.notify(f"Selected model: {label}")
        if self.current_config is not None:
            self.run_worker(
                self._refresh_selected_config_preview(),
                name="model-select-preview",
                group="model-manager",
                exclusive=True,
                exit_on_error=False,
            )

    def _handle_download_model_submission(self, params: dict[str, Any] | None) -> None:
        if not params:
            return
        self.run_worker(
            self._download_model(params),
            name="model-download",
            group="model-download",
            exclusive=True,
            exit_on_error=False,
        )

    async def _download_model(self, params: dict[str, Any]) -> None:
        params = {"job_id": uuid.uuid4().hex, **dict(params)}
        await self._run_target_job(
            "download_model",
            params,
            error_action="download model",
            incomplete_label="Model download",
        )

    async def _refresh_models(self) -> None:
        try:
            result = await self._target_call("refresh_models", {})
        except TargetCallError as exc:
            self._set_error_text(f"Unable to refresh models: {exc}", style=f"bold {BAD}")
            return
        try:
            refreshed = int(result.get("refreshed") or 0)
        except (TypeError, ValueError):
            refreshed = 0
        self.notify(f"Refreshed models: {refreshed}")
        if self.current_config is not None:
            await self._refresh_selected_config_preview()
        self._refresh_target_backed_views()
        self.call_later(self._push_model_manager_screen, result)

    def _push_model_manager_screen(self, result: dict[str, Any]) -> None:
        self.push_screen(
            ModelManagerScreen(result),
            callback=self._handle_model_manager_selection,
        )

    def _handle_pin_model_submission(self, params: dict[str, Any] | None) -> None:
        if not params:
            return
        self.run_worker(
            self._pin_model(params),
            name="model-pin",
            group="model-manager",
            exclusive=True,
            exit_on_error=False,
        )

    async def _pin_model(self, params: dict[str, Any]) -> None:
        try:
            result = await self._target_call("pin_model", params)
        except TargetCallError as exc:
            self._set_error_text(f"Unable to pin model: {exc}", style=f"bold {BAD}")
            return
        entry = result.get("entry") if isinstance(result.get("entry"), dict) else {}
        label = _optional_str(entry.get("display_name"))
        entry_id = _optional_str(entry.get("entry_id"))
        rendered = label or entry_id or _optional_str(params.get("entry_id")) or "model"
        self.notify(f"Pinned model: {rendered}")
        if self.current_config is not None:
            await self._refresh_selected_config_preview()
        self._refresh_target_backed_views()

    async def _verify_model(self, model_ref: str) -> None:
        try:
            result = await self._target_call("verify_model", {"model_ref": model_ref})
        except TargetCallError as exc:
            self._set_error_text(f"Unable to verify model: {exc}", style=f"bold {BAD}")
            return
        cache_state = _optional_str(result.get("cache_state")) or "verified"
        detail = _optional_str(result.get("detail"))
        suffix = f": {detail}" if detail else ""
        self.notify(f"Verified model: {model_ref} ({cache_state}){suffix}")
        if self.current_config is not None:
            await self._refresh_selected_config_preview()
        self._refresh_target_backed_views()

    def _confirm_remove_model(self, selection: dict[str, Any]) -> None:
        model_ref = _optional_str(selection.get("model_ref"))
        if model_ref is None:
            return
        label = _optional_str(selection.get("label")) or model_ref
        target_label = self._target_label()
        message = (
            f"Remove model {label} on {target_label}?"
            f"\n\nThis removes target-local model metadata on {target_label}."
        )
        self._pending_model_remove = {"model_ref": model_ref, "label": label}
        self.push_screen(
            ConfirmScreen(
                message,
                title="Remove model",
                confirm_label="Remove",
                confirm_action="confirm_remove_model",
            )
        )

    def confirm_remove_model(self) -> None:
        if self.screen.id == "confirm":
            self.pop_screen()
        pending = self._pending_model_remove
        self._pending_model_remove = None
        if pending is None:
            return
        self.run_worker(
            self._remove_model(pending["model_ref"], pending["label"]),
            name="model-remove",
            group="model-manager",
            exclusive=True,
            exit_on_error=False,
        )

    async def _remove_model(self, model_ref: str, label: str) -> None:
        try:
            result = await self._target_call(
                "remove_model",
                {"model_ref": model_ref, "configs_dir": str(self.configs_dir)},
            )
        except TargetCallError as exc:
            self._set_error_text(f"Unable to remove model: {exc}", style=f"bold {BAD}")
            return
        entry = result.get("entry") if isinstance(result.get("entry"), dict) else {}
        removed_label = _optional_str(entry.get("display_name")) or label
        self.notify(f"Removed model: {removed_label}")
        if self.current_config is not None:
            await self._refresh_selected_config_preview()
        self._refresh_target_backed_views()

    async def _run_target_job(
        self,
        method: str,
        params: dict[str, Any],
        *,
        error_action: str,
        incomplete_label: str,
    ) -> None:
        job_id = str(params["job_id"])
        self._active_job_id = job_id
        self._active_job_label = incomplete_label
        await self._ensure_target_client_connected()
        events = self._target_client.subscribe([job_id], resume_from="live")
        try:
            try:
                await self._target_client.call(method, params)
            except TargetCallError as exc:
                self._set_error_text(
                    f"Unable to {error_action}: {exc}",
                    style=f"bold {BAD}",
                )
                return
            await self._consume_target_job_events_until_done(
                job_id,
                events,
                incomplete_label=incomplete_label,
            )
        finally:
            if self._active_job_id == job_id:
                self._active_job_id = None
                self._active_job_label = ""
            aclose = getattr(events, "aclose", None)
            if aclose is not None:
                await aclose()

    async def _consume_target_job_events_until_done(
        self,
        job_id: str,
        events,
        *,
        incomplete_label: str,
    ) -> None:
        async for event in events:
            if event.get("job_id") != job_id:
                continue
            self._post_wire_event_message(event)
            if event.get("event") != "job_done":
                continue
            if event.get("ok") and self.current_config is not None:
                await self._refresh_selected_config_preview()
            return
        self._set_error_text(
            f"{incomplete_label} stream ended before completion: {job_id}",
            style=f"bold {BAD}",
        )

    def action_reconnect(self) -> None:
        self.run_worker(
            self._reconnect_target(),
            name="target-reconnect",
            group="target-connection",
            exclusive=True,
            exit_on_error=False,
        )

    async def _reconnect_target(self) -> None:
        try:
            await self._target_client.disconnect()
        except Exception:
            pass
        await self._ensure_target_client_connected()

    async def _switch_target(self, target_name: str) -> None:
        try:
            target_config = load_targets_file().by_name(target_name)
            target_client = target_client_for_config(target_config)
        except Exception as exc:
            self._set_error_text(f"Target switch failed: {exc}", style=f"bold {BAD}")
            return

        try:
            await self._target_client.disconnect()
        except Exception as exc:
            self._debug_event(
                "target.disconnect_failed",
                target=self.target_name,
                error=str(exc),
            )

        self.target_name = target_config.name
        self._target_config = target_config
        self._target_client = target_client
        self.target_connection_state = "disconnected"
        self.target_connection_detail = ""
        self.target_agent_restarted = False
        self._target_agent_info = {}
        self._target_last_seen_at = None
        self._target_has_connected_once = False
        self._target_daemon_start_ts = None
        self._target_last_event_seq_by_run.clear()
        self._target_last_log_cursor_by_run.clear()
        self._target_reconnect_backoff_seconds = self._target_reconnect_backoff_initial_seconds
        self.detached_run_summaries = []
        self.registry = ConfigRegistry()
        self.current_config = None
        self.selected_config_preview = ""
        self.selected_config_metadata = {}
        self._config_preview_cache.clear()
        self.current_run_id = None
        self.reattached_run_id = None
        self.fsm = PhaseFSM(bundled_profile("current"))
        self.phase = Phase.IDLE
        self._reset_run_state()
        self._set_phase(Phase.IDLE)

        self.registry = await self._load_registry_from_agent()
        if self.registry.valid:
            self.current_config = self.registry.valid[0].config
            await self._refresh_selected_config_preview()
        else:
            self.config_summary = self._render_config_summary_plain()
        await self._refresh_detached_runs()
        self._refresh_target_backed_views()

    def _refresh_target_backed_views(self) -> None:
        self.config_summary = self._render_config_summary_plain()
        try:
            self.query_one("#configs-title", Static).update(self._render_configs_title())
            self.query_one("#configs", Static).update(self._render_config_summary())
        except WIDGET_MISSING_EXCEPTIONS:
            pass
        self._refresh_sidebar_overlay()
        self._refresh_dashboard_shell()

    def action_load(self) -> None:
        if self._attached_run_is_alive():
            self.notify("A process is already running", severity="warning")
            return
        if self._has_reattached_run():
            self.notify("A detached run is already attached", severity="warning")
            return
        if self.target_connection_state != "connected":
            self._set_error_text(
                self._render_target_connection_banner(),
                style=f"bold {BAD}",
            )
            self.notify("Target unreachable - reconnect first", severity="warning")
            return
        if not self.registry.valid:
            self._set_error_text("No valid configs to load")
            return
        if self.current_config is None:
            self.current_config = self.registry.valid[0].config
        self.run_worker(self._run_selected_config(), name="load", group="engine", exclusive=True)

    def action_stop(self) -> None:
        if self._active_job_id is not None:
            job_id = self._active_job_id
            self.run_worker(
                self._cancel_target_job(job_id),
                name="job-cancel",
                group="job-cancel",
                exclusive=True,
                exit_on_error=False,
            )
            return
        if self._target_control_blocked("stop"):
            return
        if self.reattached_run_id is not None:
            self.run_worker(
                self._signal_reattached_target_run("stop"),
                name="reattach-stop",
                group="engine-signal",
                exclusive=True,
            )
            return
        if self.current_run_id is not None:
            self.run_worker(
                self._target_stop_run(
                    self.current_run_id,
                    interrupt_timeout=2,
                    terminate_timeout=2,
                ),
                name="stop",
                group="engine-signal",
                exclusive=True,
            )
            return
        self._set_phase(Phase.STOPPED)
        self._write_log("INFO stop requested")

    async def _cancel_target_job(self, job_id: str) -> None:
        try:
            await self._target_call("cancel_job", {"job_id": job_id})
        except TargetCallError as exc:
            label = self._active_job_label or "job"
            self._set_error_text(f"Unable to cancel {label}: {exc}", style=f"bold {BAD}")
            return
        label = self._active_job_label or "job"
        self.notify(f"Cancelled {label}")

    def action_kill(self) -> None:
        if self._target_control_blocked("kill"):
            return
        if self._attached_run_is_alive():
            self.push_screen(
                ConfirmScreen(
                    f"Force kill the attached server process on {self._target_label()}?",
                    title="Confirm kill",
                    confirm_label="Kill",
                    confirm_action="confirm_kill_running",
                )
            )
            return
        if self._has_reattached_run():
            self.push_screen(
                ConfirmScreen(
                    f"Force kill the detached server process group on {self._target_label()}?",
                    title="Confirm kill",
                    confirm_label="Kill",
                    confirm_action="confirm_kill_running",
                )
            )
            return
        self._set_error_text("Kill requested")

    def action_restart(self) -> None:
        if self._target_control_blocked("restart"):
            return
        if self.current_run_id is not None:
            run_id = self.current_run_id
            self.run_worker(
                self._restart_attached_run(run_id),
                name="restart",
                group="restart",
                exclusive=True,
            )
            return
        if self.reattached_run_id is not None:
            self.run_worker(
                self._restart_reattached_target_run(),
                name="restart",
                group="restart",
                exclusive=True,
            )
            return
        self.action_stop()
        self.action_load()

    def _target_control_blocked(self, action: str) -> bool:
        if self.target_connection_state == "connected":
            return False
        self._set_error_text(
            self._render_target_connection_banner(),
            style=f"bold {BAD}",
        )
        self.notify(
            f"Target unavailable - reconnect before {action}",
            severity="warning",
        )
        return True

    def _target_capability_blocked(self, capability: str, feature: str) -> bool:
        if self._target_supports_capability(capability):
            return False
        self._set_error_text(
            (
                f"Feature not available on {self._target_label()}: {feature} "
                f"(missing capability {capability})"
            ),
            style=f"bold {WARN}",
        )
        self.notify(
            f"Feature unavailable on {self._target_label()}: {feature}",
            severity="warning",
        )
        return True

    def _target_supports_capability(self, capability: str) -> bool:
        capabilities = self._target_agent_info.get("capabilities")
        if not isinstance(capabilities, list):
            return True
        return capability in {str(item) for item in capabilities}

    def _target_label(self) -> str:
        return self.target_name

    async def _restart_attached_run(self, run_id: str) -> None:
        cfg = self.current_config
        if cfg is None:
            self._set_error_text("No config selected for restart")
            return
        self._set_phase(Phase.STARTING)
        params = self._restart_agent_params(run_id=run_id, name=cfg.name)
        try:
            result = await self._target_call("restart", params)
        except TargetCallError as exc:
            self._handle_launch_agent_error(exc)
            return
        await self._monitor_restart_result(
            cfg,
            result,
            fallback_run_id=str(params["new_run_id"]),
        )

    async def _restart_reattached_target_run(self) -> None:
        run_id = self.reattached_run_id
        cfg = self.current_config
        if run_id is None:
            return
        if cfg is None:
            self._set_error_text("No config selected for restart")
            return
        self.workers.cancel_group(self, "tail")
        self.workers.cancel_group(self, "health")
        self._set_phase(Phase.STARTING)
        params = self._restart_agent_params(run_id=run_id, name=cfg.name)
        try:
            result = await self._target_call("restart", params)
        except TargetCallError as exc:
            self._handle_launch_agent_error(exc)
            return
        self.reattached_run_id = None
        self.current_run_id = None
        await self._monitor_restart_result(
            cfg,
            result,
            fallback_run_id=str(params["new_run_id"]),
        )

    def action_config_picker(self) -> None:
        self.push_screen(
            ConfigPickerScreen(
                self.registry,
                preview_cache=self._config_preview_cache,
            )
        )

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
        if not self._has_reattached_run():
            self.notify("No detached run is attached", severity="warning")
            return
        self.workers.cancel_group(self, "tail")
        self.workers.cancel_group(self, "health")
        sidecar_name = str(self.reattached_run_id)
        self.reattached_run_id = None
        self.current_run_id = None
        self._write_log(f"INFO detached from {sidecar_name}; server continues running")
        self.notify("Detached from run; server continues running")

    def _has_reattached_run(self) -> bool:
        return self.reattached_run_id is not None

    async def _signal_reattached_target_run(self, action: str) -> None:
        if self.reattached_run_id is None:
            return
        run_id = self.reattached_run_id
        try:
            if action == "stop":
                await self._target_call(
                    "stop",
                    {
                        "run_id": run_id,
                        "interrupt_timeout": 2,
                        "terminate_timeout": 2,
                    },
                )
            elif action == "kill":
                await self._target_call("kill", {"run_id": run_id})
            else:
                raise ValueError(f"unknown reattached run action: {action}")
        except Exception as exc:
            self._set_error_text(f"Unable to {action} {run_id}: {exc}")
            return
        self.workers.cancel_group(self, "tail")
        self.workers.cancel_group(self, "health")
        self.reattached_run_id = None
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
        if self._attached_run_is_alive():
            self.push_screen(
                ConfirmScreen("Attached server is still running. Stop it before quit?")
            )
            return
        self.exit()

    def select_config(self, name: str) -> None:
        self.current_config = self.registry.by_name(name)
        self.config_summary = self._render_config_summary_plain()
        self.query_one("#configs", Static).update(self._render_config_summary())
        self.query_one("#configs-title", Static).update(self._render_configs_title())
        self._refresh_sidebar_overlay()
        self._refresh_chrome()
        self.run_worker(
            self._refresh_selected_config_preview(),
            name="config-preview",
            group="config-preview",
            exclusive=True,
            exit_on_error=False,
        )

    def confirm_stop_running(self) -> None:
        if self.current_run_id is not None:
            run_id = self.current_run_id
            self.run_worker(
                self._exit_after_target_run_exit(run_id),
                name="quit-stop",
                group="quit",
                exclusive=True,
            )
            return
        self.exit()

    async def _exit_after_target_run_exit(self, run_id: str) -> None:
        await self._target_stop_run(
            run_id,
            interrupt_timeout=2,
            terminate_timeout=2,
        )
        while self.current_run_id == run_id:
            await asyncio.sleep(0.05)
        self.exit()

    def confirm_kill_running(self) -> None:
        if self.screen.id == "confirm":
            self.pop_screen()
        if self.reattached_run_id is not None:
            self.run_worker(
                self._signal_reattached_target_run("kill"),
                name="reattach-kill",
                group="engine-signal",
                exclusive=True,
            )
            return
        if self.current_run_id is not None:
            self.run_worker(
                self._target_kill_run(self.current_run_id),
                name="kill",
                group="engine-signal",
                exclusive=True,
            )
            return
        self._set_error_text("Kill requested")

    def _config_from_sidecar_snapshot(self, config_name: str, snapshot: dict) -> ModelConfig:
        snapshot_config = ModelConfig.model_validate(snapshot)
        try:
            registry_config = self.registry.by_name(config_name)
        except KeyError:
            return snapshot_config
        return self._restore_registry_secrets(snapshot_config, registry_config)

    @staticmethod
    def _restore_registry_secrets(
        snapshot_config: ModelConfig, registry_config: ModelConfig
    ) -> ModelConfig:
        data = snapshot_config.model_dump(mode="python")
        if snapshot_config.server.api_key is None and registry_config.server.api_key:
            data["server"] = {
                **data.get("server", {}),
                "api_key": registry_config.server.api_key,
            }
        env = dict(data.get("env", {}))
        for key, value in registry_config.env.items():
            if key not in env and _looks_secret_env_key(key):
                env[key] = value
        data["env"] = env
        return ModelConfig.model_validate(data)

    def on_log_line_committed(self, message: LogLineCommitted) -> None:
        if not message.feed_phase:
            self.fsm.recent_lines.append(message.text)
            self._write_log(message.text, message.level)
            return
        self._handle_committed_log(message.text, message.level)

    def on_progress_updated(self, message: ProgressUpdated) -> None:
        self._update_progress(message.text)

    def on_phase_changed(self, message: PhaseChanged) -> None:
        if self.phase in {Phase.READY, Phase.DEGRADED} and message.phase not in {
            Phase.ERROR,
            Phase.STOPPED,
        }:
            return
        self.fsm.phase = message.phase
        if message.error_kind is not None:
            self.fsm.error_kind = message.error_kind
        if message.error_excerpt is not None:
            self.fsm.error_excerpt = message.error_excerpt
        if message.phase is Phase.ERROR and message.error_kind is not None:
            self._set_error_banner(message.error_kind)
        self._set_phase(message.phase, agent_mono=message.agent_mono)

    def on_server_ready(self, message: ServerReady) -> None:
        self._handle_server_ready(
            message.models,
            reachable_url=message.reachable_url,
            feed_phase=message.feed_phase,
        )

    def on_health_changed(self, message: HealthChanged) -> None:
        self._handle_health_changed(
            ready=message.ready,
            detail=message.detail,
            models=message.models,
            error_kind=message.error_kind,
            reachable_url=message.reachable_url,
            feed_phase=message.feed_phase,
        )

    def on_process_exited(self, message: ProcessExited) -> None:
        self.fsm.process_exited(message.returncode)
        if self.fsm.phase is Phase.ERROR and self.fsm.error_kind is not None:
            self._set_error_banner(self.fsm.error_kind)
        self._set_phase(self.fsm.phase)

    def on_engine_error(self, message: EngineError) -> None:
        self.fsm.health_error(message.kind, message.detail)
        self._set_error_banner(message.kind)
        self._set_phase(self.fsm.phase)

    def on_agent_error(self, message: AgentError) -> None:
        if message.fatal:
            self.fsm.health_error(ErrorKind.CRASHED, message.detail)
            self._set_error_banner(ErrorKind.CRASHED)
            self._set_phase(self.fsm.phase)
            return
        self._set_error_text(message.detail, style=f"bold {WARN}")

    def on_gpu_stats_updated(self, message: GpuStatsUpdated) -> None:
        self._render_gpu_panel(message.result)

    def on_gpu_stats_unavailable(self, message: GpuStatsUnavailable) -> None:
        self._render_gpu_panel(
            GpuPollResult([], note=message.detail, unavailable=True)
        )

    def handle_log_record(self, record: LogRecord) -> None:
        message = from_log_record(record)
        if isinstance(message, LogLineCommitted):
            self.on_log_line_committed(message)
            return
        self.on_progress_updated(message)

    def _post_log_record_message(self, record: LogRecord) -> None:
        self.post_message(from_log_record(record))

    def _handle_committed_log(self, text: str, level: str | None) -> None:
        self.fsm.feed_line(text)
        self._set_phase(self.fsm.phase)
        if self.fsm.phase is Phase.ERROR and self.fsm.error_kind is not None:
            self._set_error_banner(self.fsm.error_kind)
        self._write_log(text, level)

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
        try:
            self.query_one("#progress-panel").display = True
            self.query_one("#progress-label", Static).update(
                Text(self._progress_label(text), style=f"bold {WARN}")
            )
            self.query_one("#progress-line").display = True
            self.query_one("#progress-text", Static).update(
                Text(self._progress_sublabel(text), style=MUTED)
            )
            progress_track = self.query_one("#progress-track", Static)
            progress_percent = self.query_one("#progress-percent", Static)
            progress = self.query_one("#progress", ProgressBar)
        except WIDGET_MISSING_EXCEPTIONS:
            return
        match = PROGRESS_PERCENT_RE.search(text)
        if match is None:
            progress.update(total=None, progress=0)
            progress_track.update(self._render_progress_track(None, style=WARN))
            progress_percent.update("")
            return
        percent = max(0.0, min(100.0, float(match.group("percent"))))
        progress.update(total=100, progress=percent)
        progress_track.update(self._render_progress_track(percent, style=WARN))
        progress_percent.update(
            Text(self._format_progress_percent(percent), style=f"bold {WARN}")
        )

    def _clear_progress(self) -> None:
        self.progress_text = ""
        try:
            self.query_one("#progress-panel").display = False
            self.query_one("#progress-label", Static).update("")
            self.query_one("#progress-text", Static).update("")
            self.query_one("#progress-track", Static).update("")
            self.query_one("#progress-percent", Static).update("")
            self.query_one("#progress", ProgressBar).update(total=None, progress=0)
            self.query_one("#progress-line").display = False
        except WIDGET_MISSING_EXCEPTIONS:
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
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self._log_flush_scheduled = False
            return
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
        except WIDGET_MISSING_EXCEPTIONS:
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
        target_level = LEVEL_FILTER_ALIASES.get(self.filter_text.upper())
        return needle in text.lower() or (
            target_level is not None and target_level == (level or "").upper()
        )

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
            lines.extend(
                f"{label}: {value}" for label, value in self._composition_detail_rows()
            )
        if self.selected_config_preview:
            lines.append("Full preview: press c")
            lines.append("")
        lines.extend(f"✓ {item.config.name}" for item in self.registry.valid)
        for item in self.registry.invalid:
            first_error, *remaining_errors = item.errors or ["invalid config"]
            lines.append(f"⚠ {item.path.name}: {first_error}")
            lines.extend(f"  {error}" for error in remaining_errors)
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
            for label, value in self._composition_detail_rows():
                text.append(f"\n{label}: ", style=MUTED)
                text.append(value, style=TEXT)
            if self.selected_config_preview:
                text.append("\nFull preview: press c", style=MUTED)
            text.append("\n\n")
        if self.registry.valid:
            selected_name = self.current_config.name if self.current_config else None
            for item in self.registry.valid:
                cfg = item.config
                selected = cfg.name == selected_name
                marker = ">" if selected else "✓"
                row_surface = f" on {ACCENT_SURFACE}" if selected else ""
                marker_style = f"bold {ACCENT}{row_surface}" if selected else GOOD
                name_style = f"bold {TEXT}{row_surface}" if selected else TEXT
                text.append(marker, style=marker_style)
                text.append(f" {cfg.name}", style=name_style)
                meta = self._config_meta(cfg)
                if meta:
                    text.append(f"  {meta}", style=f"{MUTED}{row_surface}")
                text.append("\n")
        if self.registry.invalid:
            for item in self.registry.invalid:
                first_error, *remaining_errors = item.errors or ["invalid config"]
                warning_surface = f" on {WARN_SURFACE}"
                text.append("⚠ ", style=f"bold {WARN}{warning_surface}")
                text.append(item.path.name, style=f"bold {WARN}{warning_surface}")
                text.append(f": {first_error}", style=f"{MUTED}{warning_surface}")
                text.append("\n")
                for error in remaining_errors:
                    text.append("  ", style=MUTED)
                    text.append(error, style=MUTED)
                    text.append("\n")
                text.append("\n")
        if not text.plain:
            text.append("No configs found", style=MUTED)
        text.rstrip()
        return text

    def _composition_detail_rows(self) -> list[tuple[str, str]]:
        if self.current_config is None:
            return []
        return [
            ("Target", self.target_name or "local"),
            ("Build", self._render_active_build_segment()),
            ("Model state", self._render_active_model_segment()),
        ]

    def _render_configs_title(self) -> Text:
        valid_count = len(self.registry.valid)
        invalid_count = len(self.registry.invalid)
        text = Text("Configs", style=f"bold {ACCENT}")
        if valid_count:
            text.append(
                f"  {valid_count} valid",
                style=f"bold {GOOD} on {GOOD_SURFACE}",
            )
        if invalid_count:
            text.append(
                f"  {invalid_count} invalid",
                style=f"bold {WARN} on {WARN_SURFACE}",
            )
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

    async def _refresh_selected_config_preview(self) -> None:
        if self.current_config is None:
            self.selected_config_preview = ""
            self.selected_config_metadata = {}
            return
        try:
            result = await self._target_call(
                "preview",
                self._agent_params(
                    name=self.current_config.name,
                    configs_dir=self.configs_dir,
                    **self._launch_overrides,
                ),
            )
            self.selected_config_preview = str(result["preview"])
            self._config_preview_cache[self.current_config.name] = (
                self.selected_config_preview
            )
            metadata = result.get("metadata")
            self.selected_config_metadata = dict(metadata) if isinstance(metadata, dict) else {}
        except TargetCallError as exc:
            self.selected_config_preview = f"Preview unavailable: {exc}"
            self._config_preview_cache[self.current_config.name] = (
                self.selected_config_preview
            )
            self.selected_config_metadata = {}
        self.config_summary = self._render_config_summary_plain()
        try:
            self.query_one("#configs", Static).update(self._render_config_summary())
            self.query_one("#configs-title", Static).update(self._render_configs_title())
            self._refresh_sidebar_overlay()
            self._refresh_chrome()
        except WIDGET_MISSING_EXCEPTIONS:
            return

    async def _load_registry_from_agent(self) -> ConfigRegistry:
        try:
            result = await self._target_call(
                "list_configs",
                self._agent_params(configs_dir=self.configs_dir),
            )
        except TargetCallError as exc:
            if exc.code not in {"version-mismatch", "agent-unreachable"}:
                raise
            self._mark_target_connection_error(exc)
            return ConfigRegistry()
        return _config_registry_from_agent_payload(result)

    def _agent_params(self, **values) -> dict[str, str]:
        return {key: str(value) for key, value in values.items() if value is not None}

    def _launch_agent_params(self, **values) -> dict[str, str]:
        params = self._agent_params(**values)
        params["run_id"] = uuid.uuid4().hex
        return params

    def _restart_agent_params(self, *, run_id: str, name: str) -> dict[str, Any]:
        params: dict[str, Any] = self._agent_params(
            name=name,
            configs_dir=self.configs_dir,
            **self._launch_overrides,
        )
        params["run_id"] = run_id
        params["new_run_id"] = uuid.uuid4().hex
        params["interrupt_timeout"] = 2
        params["terminate_timeout"] = 2
        return params

    async def _ensure_target_client_connected(self) -> None:
        agent_restarted = False
        if not getattr(self._target_client, "connected", False):
            self.target_connection_state = (
                "reconnecting" if self._target_has_connected_once else "connecting"
            )
            self._refresh_chrome()
            try:
                agent_info = await self._target_client.connect()
            except TargetCallError as exc:
                self._mark_target_connection_error(exc)
                raise
            except Exception as exc:
                self.target_connection_state = "unreachable"
                self.target_connection_detail = str(exc)
                self._refresh_chrome()
                self._set_error_text(
                    self._render_target_connection_banner("agent-unreachable", str(exc)),
                    style=f"bold {BAD}",
                )
                raise
            if isinstance(agent_info, dict):
                self._target_agent_info = dict(agent_info)
                self._target_last_seen_at = _target_seen_timestamp(agent_info)
                daemon_start_ts = agent_info.get("daemon_start_ts")
                if isinstance(daemon_start_ts, str) and daemon_start_ts:
                    previous_daemon_start_ts = self._target_daemon_start_ts
                    if (
                        self._target_has_connected_once
                        and previous_daemon_start_ts is not None
                        and previous_daemon_start_ts != daemon_start_ts
                    ):
                        agent_restarted = True
                        self.target_agent_restarted = True
                        self._target_last_event_seq_by_run.clear()
                        self._debug_event(
                            "target.agent_restarted",
                            previous_daemon_start_ts=previous_daemon_start_ts,
                            daemon_start_ts=daemon_start_ts,
                        )
                    self._target_daemon_start_ts = daemon_start_ts
        self._target_has_connected_once = True
        self.target_connection_state = "connected"
        self.target_connection_detail = (
            "agent restarted; rediscovering detached runs" if agent_restarted else ""
        )
        self._refresh_chrome()
        if agent_restarted:
            await self._refresh_detached_runs()

    def _mark_target_connection_error(self, exc: TargetCallError) -> None:
        if exc.code == "version-mismatch":
            self.target_connection_state = "version-mismatch"
        elif exc.code in {"agent-unreachable", "command-not-found"}:
            self.target_connection_state = "unreachable"
        else:
            self.target_connection_state = "disconnected"
        self.target_connection_detail = str(exc)
        self._refresh_chrome()
        self._set_error_text(
            self._render_target_connection_banner(exc.code, str(exc)),
            style=f"bold {BAD}",
        )

    async def _target_call(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        await self._ensure_target_client_connected()
        return await self._target_client.call(method, params)

    async def _poll_target_keepalive(self) -> None:
        interval = self._target_ping_interval_seconds
        ping = getattr(self._target_client, "ping", None)
        if interval is None or interval <= 0 or not callable(ping):
            return
        while True:
            await asyncio.sleep(self._target_keepalive_delay_seconds())
            await self._target_keepalive_once()
            self._update_target_reconnect_backoff()

    def _target_keepalive_delay_seconds(self) -> float:
        if self.target_connection_state == "connected":
            interval = self._target_ping_interval_seconds
            return float(interval) if interval is not None else 0.0
        return self._target_reconnect_backoff_seconds

    def _update_target_reconnect_backoff(self) -> None:
        if self.target_connection_state == "connected":
            self._target_reconnect_backoff_seconds = (
                self._target_reconnect_backoff_initial_seconds
            )
            return
        self._target_reconnect_backoff_seconds = min(
            self._target_reconnect_backoff_seconds * 2,
            self._target_reconnect_backoff_cap_seconds,
        )

    async def _target_keepalive_once(self) -> None:
        try:
            await self._ensure_target_client_connected()
            ping = await asyncio.wait_for(
                self._target_client.ping(),
                timeout=self._target_ping_timeout_seconds,
            )
        except asyncio.TimeoutError:
            await self._mark_target_disconnected("ping timeout")
        except Exception as exc:
            await self._mark_target_disconnected(str(exc))
        else:
            if isinstance(ping, dict):
                self._target_last_seen_at = _target_seen_timestamp(ping)
            self.target_connection_state = "connected"
            self.target_connection_detail = ""
            self._refresh_chrome()

    async def _mark_target_disconnected(self, detail: str) -> None:
        self.target_connection_state = "disconnected"
        self.target_connection_detail = detail
        self._refresh_chrome()
        try:
            await self._target_client.disconnect()
        except Exception:
            return

    async def _refresh_detached_runs(self) -> None:
        try:
            result = await self._target_call("discover_runs", {})
        except Exception as exc:
            self.detached_run_summaries = []
            self._debug_event("detached.discovery_failed", error=str(exc))
            return
        summaries: list[dict[str, str]] = []
        for item in result.get("runs", []):
            if not isinstance(item, dict):
                continue
            run_id = item.get("run_id")
            config_name = item.get("config_name")
            if run_id is None or config_name is None:
                continue
            summaries.append(
                {
                    "run_id": str(run_id),
                    "config_name": str(config_name),
                }
            )
        self.detached_run_summaries = summaries

    async def _reattach_target_detached_run(self, run_id: str) -> None:
        try:
            result = await self._target_call("reattach", {"run_id": run_id})
        except Exception as exc:
            self._set_error_text(f"Unable to reattach {run_id}: {exc}")
            return
        sidecar = dict(result.get("sidecar") or {})
        config_name = str(sidecar.get("config_name") or result.get("run_id") or run_id)
        try:
            self.current_config = self._config_from_sidecar_snapshot(
                config_name, dict(result["config"])
            )
        except Exception as exc:
            self._set_error_text(f"Unable to reattach {run_id}: {exc}")
            return
        self.reattached_run_id = str(result["run_id"])
        self.current_run_id = None
        self.fsm = _phase_fsm_from_agent_metadata(
            dict(result.get("fsm") or {})
        )
        self.ready_url = self._server_url_from_sidecar_payload(sidecar)
        self.served_models = [str(model) for model in sidecar.get("served_model_names") or []]
        self._set_phase(Phase.SERVER_STARTING)
        self.run_worker(
            self._target_tail_detached_run(self.reattached_run_id, start_position=0),
            name="reattach-tail",
            group="tail",
            exclusive=True,
        )
        self.run_worker(
            self._target_probe_run_until_ready(
                self.reattached_run_id,
            ),
            name="reattach-health",
            group="health",
            exclusive=True,
            exit_on_error=False,
        )

    def _post_wire_event_message(self, event: dict[str, Any]) -> None:
        message = _message_from_wire_event(event)
        if message is not None:
            self.post_message(message)

    async def _target_stop_run(
        self,
        run_id: str,
        *,
        interrupt_timeout: float,
        terminate_timeout: float,
    ) -> None:
        try:
            await self._target_call(
                "stop",
                {
                    "run_id": run_id,
                    "interrupt_timeout": interrupt_timeout,
                    "terminate_timeout": terminate_timeout,
                },
            )
        except Exception as exc:
            self._set_error_text(f"Unable to stop {run_id}: {exc}")

    async def _target_kill_run(self, run_id: str) -> None:
        try:
            await self._target_call("kill", {"run_id": run_id})
        except Exception as exc:
            self._set_error_text(f"Unable to kill {run_id}: {exc}")

    async def _target_probe_run_until_ready(
        self, run_id: str, *, publish_result: bool = True
    ) -> None:
        result = await self._target_call("probe_until_ready", {"run_id": run_id})
        if not publish_result:
            return
        error_kind = None
        if result.get("error_kind") is not None:
            error_kind = _error_kind_from_agent_payload(result.get("error_kind"))
        self.post_message(
            HealthChanged(
                ready=bool(result.get("ready")),
                detail=str(result.get("detail", "")),
                models=[str(model) for model in result.get("models") or []],
                error_kind=error_kind,
                reachable_url=_optional_str(result.get("reachable_url")),
                feed_phase=False,
            )
        )
        phase_value = result.get("phase")
        if phase_value is not None:
            self.post_message(
                PhaseChanged(
                    Phase(str(phase_value)),
                    error_kind=error_kind,
                    error_excerpt=_optional_str(result.get("error_excerpt")),
                )
            )

    async def _target_tail_detached_run(
        self, run_id: str, *, start_position: int | None = None
    ) -> None:
        params: dict[str, Any] = {"run_id": run_id}
        if start_position is not None:
            params["start_position"] = start_position
        events_task = asyncio.create_task(
            self._consume_target_run_events_until_exit(run_id)
        )
        await asyncio.sleep(0)
        try:
            await self._target_call("tail_detached", params)
            await events_task
        finally:
            if not events_task.done():
                events_task.cancel()

    async def _consume_target_run_events_until_exit(self, run_id: str) -> Phase | None:
        await self._ensure_target_client_connected()
        last_seq = self._target_last_event_seq_by_run.get(run_id)
        resume_from: object = (
            {"seq": last_seq}
            if last_seq is not None
            else self._target_last_log_cursor_by_run.get(run_id, "live")
        )
        events = self._target_client.subscribe([run_id], resume_from=resume_from)
        terminal_phase: Phase | None = None
        try:
            async for event in events:
                if str(event.get("run_id")) != run_id:
                    continue
                seq = event.get("seq")
                if isinstance(seq, int):
                    self._target_last_event_seq_by_run[run_id] = max(
                        seq,
                        self._target_last_event_seq_by_run.get(run_id, 0),
                    )
                log_inode = event.get("log_inode")
                byte_offset = event.get("byte_offset")
                if isinstance(log_inode, int) and isinstance(byte_offset, int):
                    self._target_last_log_cursor_by_run[run_id] = {
                        "log_inode": log_inode,
                        "byte_offset": byte_offset,
                    }
                if event.get("event") == "exited":
                    phase_value = event.get("phase")
                    if phase_value is not None:
                        terminal_phase = Phase(str(phase_value))
                self._post_wire_event_message(event)
                if event.get("event") == "exited":
                    break
        finally:
            aclose = getattr(events, "aclose", None)
            if aclose is not None:
                await aclose()
        return terminal_phase

    def _attached_run_is_alive(self) -> bool:
        return self.current_run_id is not None

    def _refresh_dashboard_shell(self) -> None:
        self._refresh_chrome()
        self._refresh_log_controls()
        self._refresh_status_strip()

    def _refresh_chrome(self) -> None:
        try:
            self.query_one("#target-segment", Static).update(
                self._render_target_segment()
            )
            self.query_one("#active-model", Static).update(self._render_active_model())
            self.query_one("#server-url", Static).update(self._render_chrome_url())
            self.query_one("#chrome-clock", Static).update(
                datetime.now().strftime("%H:%M:%S")
            )
        except WIDGET_MISSING_EXCEPTIONS:
            return

    def _render_target_segment(self) -> Text:
        dot = self._target_connection_dot(self.target_connection_state)
        dot_style = self._target_connection_style(self.target_connection_state)
        name = self.target_name or "local"

        text = Text()
        if self.responsive_mode == "compact":
            text.append("⊕", style=MUTED)
            text.append(dot, style=dot_style)
            return text
        if self.responsive_mode == "narrow":
            text.append("⊕", style=MUTED)
            text.append(self._compact_target_name(name), style=f"bold {TEXT}")
            text.append(dot, style=dot_style)
            return text

        text.append("⊕ ", style=MUTED)
        text.append(name, style=f"bold {TEXT}")
        text.append(f" {dot}", style=dot_style)
        return text

    @staticmethod
    def _compact_target_name(name: str) -> str:
        alnum = "".join(char for char in name.lower() if char.isalnum())
        if not alnum:
            return "?"
        consonants = "".join(char for char in alnum if char not in "aeiou")
        compact = consonants or alnum
        if len(compact) <= 4:
            return compact
        return compact[0] + compact[-3:]

    @staticmethod
    def _target_connection_dot(state: str) -> str:
        return {
            "connected": "●",
            "connecting": "◐",
            "reconnecting": "◐",
            "disconnected": "○",
            "version-mismatch": "▲",
            "unreachable": "✕",
        }.get(state, "○")

    @staticmethod
    def _target_connection_style(state: str) -> str:
        return {
            "connected": GOOD,
            "connecting": WARN,
            "reconnecting": WARN,
            "disconnected": MUTED,
            "version-mismatch": WARN,
            "unreachable": BAD,
        }.get(state, MUTED)

    def _render_target_connection_banner(
        self, key: str | None = None, detail: str | None = None
    ) -> str:
        banner_kind, cause, suggestion = self._target_connection_banner_parts(
            key or self.target_connection_state
        )
        lines = [
            f"{banner_kind}: {cause}",
            f"target: {self.target_name}",
        ]
        detail_text = detail if detail is not None else self.target_connection_detail
        if detail_text:
            lines.append(detail_text)
        lines.append(suggestion)
        lines.append("Actions: (R) Reconnect   (t) Switch target")
        return "\n".join(lines)

    @staticmethod
    def _target_connection_banner_parts(key: str) -> tuple[str, str, str]:
        return {
            "version-mismatch": (
                "AGENT_VERSION_MISMATCH",
                "agent protocol version mismatch",
                "Upgrade the older side, then reconnect.",
            ),
            "agent-unreachable": (
                "AGENT_UNREACHABLE",
                "target unreachable",
                "Check SSH/socket connectivity or start the target agent.",
            ),
            "unreachable": (
                "AGENT_UNREACHABLE",
                "target unreachable",
                "Check SSH/socket connectivity or start the target agent.",
            ),
            "command-not-found": (
                "AGENT_NOT_INSTALLED",
                "agent not installed",
                "Install vela on the target, then reconnect.",
            ),
            "disconnected": (
                "AGENT_UNREACHABLE",
                "target disconnected",
                "Reconnect before launching.",
            ),
        }.get(
            key,
            (
                "AGENT_UNREACHABLE",
                "target unreachable",
                "Check SSH/socket connectivity or start the target agent.",
            ),
        )

    def _render_active_model(self) -> str:
        if self.current_config is None:
            return "no config selected"
        if self.target_connection_state != "connected":
            return "▣ —  M —"
        return f"{self._render_active_build_segment()}  {self._render_active_model_segment()}"

    def _render_active_build_segment(self) -> str:
        assert self.current_config is not None
        metadata = self.selected_config_metadata
        label = (
            _optional_str(metadata.get("build_label"))
            or _optional_str(self.current_config.command.build)
            or _optional_str(metadata.get("vllm_version"))
            or "PATH"
        )
        marker = "📌" if self.current_config.command.build is not None else ""
        return f"▣ {marker}{label} {self._build_status_dot(metadata)}"

    @staticmethod
    def _build_status_dot(metadata: dict[str, Any]) -> str:
        status = (_optional_str(metadata.get("build_status")) or "").lower()
        if status in {"broken", "missing", "unresolved"}:
            return "✕"
        if status in {"drift", "creating", "partial"}:
            return "▲"
        if (
            metadata.get("build_id") is not None
            or metadata.get("build_label") is not None
            or metadata.get("vllm_version") is not None
        ):
            return "●"
        return "○"

    def _render_active_model_segment(self) -> str:
        assert self.current_config is not None
        metadata = self.selected_config_metadata
        label = (
            _optional_str(metadata.get("model_display_name"))
            or _optional_str(metadata.get("model_ref"))
            or _optional_str(self.current_config.model_ref)
            or self.current_config.name
        )
        revision = _optional_str(metadata.get("model_revision")) or _optional_str(
            self.current_config.revision
        )
        pinned = (
            self.current_config.model_ref is not None
            or self.current_config.revision is not None
        )
        marker = "📌" if pinned else ""
        suffix = f" {revision}" if revision else ""
        return f"M {marker}{label} {self._model_status_dot(metadata)}{suffix}"

    @staticmethod
    def _model_status_dot(metadata: dict[str, Any]) -> str:
        gated = metadata.get("model_gated")
        if gated is True or (isinstance(gated, str) and gated.lower() == "true"):
            return "🔒"
        cache_state = (_optional_str(metadata.get("model_cache_state")) or "").lower()
        if cache_state in {"cached", "ready", "local"}:
            return "●"
        if cache_state in {"partial", "drift"}:
            return "▲"
        if cache_state in {"downloading", "in-progress", "creating"}:
            return "◐"
        if cache_state in {"missing", "unresolved"}:
            return "✕"
        return "○"

    def _render_chrome_url(self) -> str:
        if self.ready_url:
            return self.ready_url
        if self.current_config is None:
            return ""
        return self._server_url(self.current_config)

    def _refresh_log_controls(self) -> None:
        try:
            self.query_one("#log-controls", Static).update(self._render_log_controls())
        except WIDGET_MISSING_EXCEPTIONS:
            return

    def _render_log_controls(self) -> Text:
        autoscroll = "OFF" if self.paused else "ON"
        wrap = "ON" if self.wrap else "OFF"
        text = Text("autoscroll ", style=MUTED)
        text.append(
            autoscroll,
            style=(
                f"bold {GOOD} on {GOOD_SURFACE}"
                if autoscroll == "ON"
                else f"bold {WARN} on {WARN_SURFACE}"
            ),
        )
        text.append("   wrap ", style=MUTED)
        text.append(
            wrap,
            style=(
                f"bold {ACCENT} on {ACCENT_SURFACE}"
                if wrap == "ON"
                else f"bold {MUTED} on {MUTED_SURFACE}"
            ),
        )
        return text

    def _refresh_status_strip(self) -> None:
        try:
            self.query_one("#status-strip", Static).update(self._render_status_strip())
        except WIDGET_MISSING_EXCEPTIONS:
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
        except WIDGET_MISSING_EXCEPTIONS:
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
            "l Load   s Stop   K Kill   r Restart   t Targets   "
            "b Builds   m Models   F Flags   R Reconnect   / Search   f Filter   "
            "p Pause   w Wrap   g/G Top/Bottom   Tab Focus   ? Help   ^P Palette   q Quit"
        )

    @staticmethod
    def _progress_label(text: str) -> str:
        label, _separator, _tail = text.partition(":")
        return label.strip() or "Progress"

    @staticmethod
    def _progress_sublabel(text: str) -> str:
        _label, separator, tail = text.partition(":")
        return tail.strip() if separator else text.strip()

    @staticmethod
    def _format_progress_percent(percent: float) -> str:
        if percent.is_integer():
            return f"{int(percent)}%"
        return f"{percent:.1f}%"

    @staticmethod
    def _render_progress_track(percent: float | None, *, style: str) -> Text:
        tick_positions = {
            max(
                0,
                min(
                    PROGRESS_TRACK_WIDTH - 1,
                    round(PROGRESS_TRACK_WIDTH * index / 10),
                ),
            )
            for index in range(1, 10)
        }
        filled = 0
        if percent is not None:
            filled = max(
                0,
                min(PROGRESS_TRACK_WIDTH, round(PROGRESS_TRACK_WIDTH * percent / 100)),
            )
        text = Text()
        for index in range(PROGRESS_TRACK_WIDTH):
            if index in tick_positions:
                text.append("│", style=MUTED)
            elif index < filled:
                text.append("━", style=style)
            else:
                text.append("─", style=MUTED)
        return text

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
        except WIDGET_MISSING_EXCEPTIONS:
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
            self._refresh_chrome()

    async def _run_selected_config(self) -> None:
        cfg = self.current_config
        if cfg is None:
            return
        self._reset_run_state()
        self.fsm = PhaseFSM(bundled_profile("current"))
        self._set_phase(Phase.STARTING)
        try:
            preflight = await self._preflight_from_agent(cfg.name)
            if not self._handle_preflight_result(preflight):
                return
            prepared = await self._prepare_launch_from_agent(cfg.name)
        except TargetCallError as exc:
            self._handle_launch_agent_error(exc)
            return
        cfg = ModelConfig.model_validate(prepared["config"])
        self.current_config = cfg
        build = _command_build_result_from_agent_payload(prepared["build"])
        self.fsm = _phase_fsm_from_agent_metadata(build.metadata)
        self._record_warnings(build.warnings)
        if cfg.launch.mode.value == "detached":
            try:
                launch = await self._target_call(
                    "launch",
                    self._launch_agent_params(
                        name=cfg.name,
                        configs_dir=self.configs_dir,
                        **self._launch_overrides,
                    ),
                )
            except TargetCallError as exc:
                self._handle_attached_start_agent_error(exc, build.argv[0])
                return
            await self._reattach_target_detached_run(str(launch["run_id"]))
            return
        try:
            launch = await self._target_call(
                "launch",
                self._launch_agent_params(
                    name=cfg.name,
                    configs_dir=self.configs_dir,
                    **self._launch_overrides,
                ),
            )
        except TargetCallError as exc:
            self._handle_attached_start_agent_error(exc, build.argv[0])
            return
        run_id = str(launch["run_id"])
        await self._monitor_attached_run(cfg, run_id)

    async def _monitor_attached_run(self, cfg: ModelConfig, run_id: str) -> None:
        self.current_run_id = run_id
        events_task = asyncio.create_task(
            self._consume_target_run_events_until_exit(run_id)
        )
        await asyncio.sleep(0)
        health_task = asyncio.create_task(
            self._probe_until_ready(cfg, publish_result=False)
        )
        await asyncio.sleep(0)
        try:
            wait_result = await self._target_call("wait", {"run_id": run_id})
            terminal_phase = await events_task
        finally:
            health_task.cancel()
            if not events_task.done():
                events_task.cancel()
        if self.current_run_id != run_id:
            return
        self.current_run_id = None
        intentional = bool(wait_result.get("intentional"))
        wait_phase = None
        phase_value = wait_result.get("phase")
        if phase_value is not None:
            wait_phase = Phase(str(phase_value))
        resolved_phase = terminal_phase or wait_phase
        if resolved_phase is not None:
            if wait_result.get("error_kind") is not None:
                self.fsm.error_kind = _error_kind_from_agent_payload(
                    wait_result.get("error_kind")
                )
            if wait_result.get("error_excerpt") is not None:
                self.fsm.error_excerpt = str(wait_result["error_excerpt"])
            self.fsm.phase = resolved_phase
            if resolved_phase is Phase.ERROR and self.fsm.error_kind is not None:
                self._set_error_banner(self.fsm.error_kind)
            if intentional and resolved_phase is Phase.STOPPED:
                self._set_error_text("")
            self._set_phase(resolved_phase)
            return
        self._set_error_text("Agent wait result did not include a terminal phase")
        self._set_phase(self.fsm.phase)

    async def _monitor_restart_result(
        self,
        cfg: ModelConfig,
        result: dict[str, Any],
        *,
        fallback_run_id: str,
    ) -> None:
        launch = dict(result.get("launch") or {})
        new_run_id = str(
            result.get("new_run_id") or launch.get("run_id") or fallback_run_id
        )
        launch_mode = str(launch.get("launch_mode") or "attached")
        if launch_mode == "detached":
            self.current_run_id = None
            await self._reattach_target_detached_run(new_run_id)
            return
        self.reattached_run_id = None
        await self._monitor_attached_run(cfg, new_run_id)

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

    async def _preflight_from_agent(self, name: str) -> dict[str, Any]:
        return await self._target_call(
            "preflight",
            self._agent_params(
                name=name,
                configs_dir=self.configs_dir,
                **self._launch_overrides,
            ),
        )

    def _handle_preflight_result(self, result: dict[str, Any]) -> bool:
        if bool(result.get("ok")):
            return True
        failures = result.get("failures")
        failure = failures[0] if isinstance(failures, list) and failures else {}
        kind = _error_kind_from_agent_payload(
            failure.get("kind") if isinstance(failure, dict) else None
        )
        detail = (
            str(failure.get("detail") or "Launch preflight failed")
            if isinstance(failure, dict)
            else "Launch preflight failed"
        )
        self.fsm.health_error(kind, detail)
        self._set_error_banner(kind)
        self._set_phase(self.fsm.phase)
        return False

    async def _prepare_launch_from_agent(self, name: str) -> dict[str, Any]:
        return await self._target_call(
            "prepare_launch",
            self._agent_params(
                name=name,
                configs_dir=self.configs_dir,
                **self._launch_overrides,
            ),
        )

    def _handle_launch_agent_error(self, exc: TargetCallError) -> None:
        if exc.code == "profile-error":
            self._handle_profile_error(VllmProfileError(str(exc)))
            return
        if exc.code == "preflight-failed":
            kind = _error_kind_from_agent_payload(exc.details.get("kind"))
            detail = str(exc.details.get("detail") or exc)
            self.fsm.health_error(kind, detail)
            self._set_error_banner(kind)
            self._set_phase(self.fsm.phase)
            return
        self._handle_profile_error(VllmProfileError(str(exc)))

    def _handle_attached_start_agent_error(
        self, exc: TargetCallError, fallback_command: str
    ) -> None:
        if exc.code == "command-not-found":
            command = str(exc.details.get("command") or fallback_command)
            self._handle_command_not_found(FileNotFoundError(command), fallback_command)
            return
        self._handle_launch_agent_error(exc)

    async def _probe_until_ready(
        self, cfg: ModelConfig, *, publish_result: bool = True
    ) -> None:
        if self.current_run_id is None:
            return
        await self._target_probe_run_until_ready(
            self.current_run_id,
            publish_result=publish_result,
        )

    def _handle_health_event(self, event: HealthEvent) -> None:
        self._handle_health_changed(
            ready=event.ready,
            detail=event.detail,
            models=event.models,
            error_kind=event.error_kind,
        )

    def _post_health_message(self, event: HealthEvent) -> None:
        self.post_message(
            HealthChanged(
                ready=event.ready,
                detail=event.detail,
                models=event.models,
                error_kind=event.error_kind,
            )
        )

    def _handle_health_changed(
        self,
        *,
        ready: bool,
        detail: str,
        models: list[str] | None = None,
        error_kind: ErrorKind | None = None,
        reachable_url: str | None = None,
        feed_phase: bool = True,
    ) -> None:
        self.health_detail = detail
        if ready:
            self._handle_server_ready(
                models or [],
                reachable_url=reachable_url,
                feed_phase=feed_phase,
            )
            return
        if not feed_phase:
            return
        if error_kind:
            self.fsm.health_error(error_kind, detail)
            self._set_error_banner(error_kind)
        else:
            self.fsm.health_failed(detail)
        self._set_phase(self.fsm.phase)

    def _handle_server_ready(
        self,
        models: list[str],
        *,
        reachable_url: str | None = None,
        feed_phase: bool = True,
    ) -> None:
        if reachable_url is not None:
            self.ready_url = self._controller_reachable_url(reachable_url)
        elif self.ready_url is None and self.current_config is not None:
            self.ready_url = self._controller_reachable_url(
                self._server_url(self.current_config)
            )
        self.served_models = models
        if not feed_phase:
            self._refresh_chrome()
            self._refresh_sidebar_overlay()
            return
        self.fsm.health_ready(models)
        self._set_phase(self.fsm.phase)

    def _set_phase(self, phase: Phase, *, agent_mono: float | None = None) -> None:
        if phase in {Phase.READY, Phase.DEGRADED, Phase.ERROR, Phase.STOPPED, Phase.IDLE}:
            self._clear_progress()
        self._track_phase_time(phase, agent_mono=agent_mono)
        self.phase = phase
        self.status_text = self._render_status(phase)
        timeline = self._render_phase_timeline()
        self.phase_timeline_text = timeline.plain
        self._debug_event("phase.changed", phase=phase.value, status=self.status_text)
        try:
            status_badge = self.query_one("#status-badge")
            self._apply_status_classes(status_badge, phase)
            self.query_one("#status-dot", Static).update(self._render_status_dot(phase))
            self.query_one("#status-label", Static).update(
                self._render_status_label(phase)
            )
            self.query_one("#phases", Static).update(timeline)
        except WIDGET_MISSING_EXCEPTIONS:
            return
        self._refresh_sidebar_overlay()
        self._refresh_chrome()

    def _track_phase_time(self, phase: Phase, *, agent_mono: float | None = None) -> None:
        now = agent_mono if agent_mono is not None else self._clock()
        uses_agent_mono = agent_mono is not None
        if self.run_started_at is None and phase not in {Phase.IDLE, Phase.STOPPED}:
            self.run_started_at = now
            self.run_started_uses_agent_mono = uses_agent_mono
        previous = self.phase
        if previous is not phase:
            if self.current_phase_started_at is not None:
                elapsed_delta = 0.0
                if self.current_phase_started_uses_agent_mono == uses_agent_mono:
                    elapsed_delta = now - self.current_phase_started_at
                self.phase_elapsed[previous] = (
                    self.phase_elapsed.get(previous, 0.0)
                    + max(0.0, elapsed_delta)
                )
            self.current_phase_started_at = now
            self.current_phase_started_uses_agent_mono = uses_agent_mono
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

    def _render_status_dot(self, phase: Phase) -> Text:
        style = self._status_style_for_phase(phase)
        surface = self._status_surface_for_phase(phase)
        return Text(self._status_icon_for_phase(phase), style=f"{style} on {surface}")

    def _render_status_label(self, phase: Phase) -> Text:
        style = self._status_style_for_phase(phase)
        surface = self._status_surface_for_phase(phase)
        return Text(phase.value, style=f"{style} on {surface}")

    def _apply_status_classes(self, status: Widget, phase: Phase) -> None:
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
    def _status_surface_for_phase(phase: Phase) -> str:
        if phase in LOADING_PHASES:
            return WARN_SURFACE
        if phase is Phase.READY:
            return GOOD_SURFACE
        if phase is Phase.DEGRADED:
            return WARN_SURFACE
        if phase is Phase.ERROR:
            return BAD_SURFACE
        return MUTED_SURFACE

    @staticmethod
    def _status_icon_for_phase(phase: Phase) -> str:
        if phase in LOADING_PHASES:
            return "●"
        return STATUS_ICONS[phase]

    def _server_url(self, cfg: ModelConfig) -> str:
        return f"http://{cfg.server.host}:{cfg.server.port}"

    def _server_url_from_sidecar_payload(self, sidecar: dict[str, Any]) -> str:
        reachable_url = _optional_str(sidecar.get("reachable_url"))
        if reachable_url is not None:
            return self._controller_reachable_url(reachable_url)
        return self._controller_reachable_url(
            f"http://{sidecar.get('host', '127.0.0.1')}:{int(sidecar.get('port', 8000))}"
        )

    def _controller_reachable_url(self, url: str) -> str:
        target_config = getattr(self, "_target_config", None)
        if (
            target_config is None
            or target_config.transport is not TransportKind.SSH
            or not target_config.host
        ):
            return url
        split = urlsplit(url)
        if not split.scheme or not split.netloc:
            return url
        public_host = _controller_host_from_ssh_target(target_config.host)
        netloc = _url_netloc(public_host, split.port)
        return urlunsplit((split.scheme, netloc, split.path, split.query, split.fragment))

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
            overall = self._format_duration(self._overall_elapsed())
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
            if self.current_phase_started_uses_agent_mono:
                return elapsed
            elapsed += self._clock() - self.current_phase_started_at
        return elapsed

    def _overall_elapsed(self) -> float:
        if self.run_started_at is None:
            return 0.0
        if self.run_started_uses_agent_mono:
            return sum(self.phase_elapsed.values()) + self._elapsed_for(self.phase)
        return self._clock() - self.run_started_at

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
        self.run_started_uses_agent_mono = False
        self.current_phase_started_at = None
        self.current_phase_started_uses_agent_mono = False
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
        try:
            self.query_one("#error", Static).update(Text(text, style=render_style) if text else "")
        except WIDGET_MISSING_EXCEPTIONS:
            return

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

    async def _stream_gpu_panel(self) -> None:
        events = None
        stream_started = False
        try:
            await self._ensure_target_client_connected()
            events = self._target_client.subscribe(["__agent__"], resume_from="live")
            await self._target_client.call(
                "gpu",
                {"sub_id": "gpu-panel", "interval_s": self._gpu_interval_seconds},
            )
            stream_started = True
            async for event in events:
                if event.get("event") != "gpu" or event.get("sub_id") != "gpu-panel":
                    continue
                self._post_wire_event_message(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.post_message(GpuStatsUnavailable(f"GPU stats unavailable: {exc}"))
            return
        finally:
            if events is not None:
                aclose = getattr(events, "aclose", None)
                if aclose is not None:
                    with contextlib.suppress(Exception):
                        await aclose()
            if stream_started:
                with contextlib.suppress(Exception):
                    await self._target_client.call("unsubscribe", {"sub_id": "gpu-panel"})

    def _render_gpu_panel(self, result: GpuPollResult) -> None:
        try:
            gpu_panel = self.query_one("#gpu", Static)
        except WIDGET_MISSING_EXCEPTIONS:
            gpu_panel = None
        if result.unavailable:
            self.gpu_panel_text = result.note
            if gpu_panel is not None:
                gpu_panel.update(Text(result.note, style=MUTED))
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
        if gpu_panel is not None:
            gpu_panel.update(renderable)

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


def _build_prerequisite_params(params: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in params.items() if key != "job_id"}


def _target_seen_timestamp(payload: dict[str, Any]) -> str:
    ts = payload.get("ts")
    if isinstance(ts, str) and ts:
        return ts
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
