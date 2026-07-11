from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shlex
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlsplit, urlunsplit

# The canonical Figma screens depend on truecolor hex tokens. Textual reads this
# during import, so set the default before importing any Textual modules.
if "NO_COLOR" not in os.environ:
    os.environ.setdefault("TEXTUAL_COLOR_SYSTEM", "truecolor")

from rich.cells import cell_len
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult, ScreenStackError, SystemCommand
from textual.containers import Horizontal, Vertical, VerticalScroll
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
from vela.engine.log_sink import LogRecord, display_level_for_line
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
from vela.remediation import remediation_for_error
from vela.transport.factory import target_client_for_config
from vela.tui.cells import truncate_cells
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
from vela.tui.screens.new_deployment import (
    DOWNLOAD_NEEDS_PIN_ERROR,
    NewDeploymentReviewScreen,
    NewDeploymentScreen,
)
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
    # Known-benign shutdown/teardown noise: de-emphasized so it never reads as
    # a warning (the screenshot-#7 NCCL destroy_process_group fix).
    "BENIGN": "#56707c",
    # Display-only run chrome (bug-237): the `── run … ──` separator and the
    # `── STOPPED by operator ──` closure lines render dim, never as log data.
    "RULE": "#56707c",
}

LEVEL_RAIL_STYLE = {
    "CRITICAL": "#ff6b6b",
    "ERROR": "#ff6b6b",
    "WARNING": "#f6c85f",
    "INFO": "#e8f1f2",
    "DEBUG": "#526a75",
    "BENIGN": "#56707c",
    "RULE": "#56707c",
}
NEW_DEPLOYMENT_TARGET_PROBE_TIMEOUT_SECONDS = 3.0
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
# Adaptive top-chrome reveal thresholds (bug-237). The header collapses
# right-to-left (badge > target > model > URL > clock): the server URL and the
# clock — the two lowest-priority segments — only appear once the terminal is
# wide enough to show them without starving the model/badge slots. These are
# finer than HORIZONTAL_BREAKPOINTS (which only has compact/narrow/wide buckets)
# because 100/120/140 are all "wide" yet must behave differently.
HEADER_URL_MIN_WIDTH = 112
HEADER_CLOCK_MIN_WIDTH = 132
# Sidebar vertical fit (bug-237). The sidebar cards hug their content inside a
# VerticalScroll column, but a short terminal can still run out of rows. When the
# terminal is shorter than this, drop the GPU monitor card first — mirroring the
# way the compact WIDTH breakpoint sheds it — so the config and phase cards keep
# their space. 24 rows is the classic VT100 minimum height; at or above it all
# four cards render.
SIDEBAR_GPU_MIN_HEIGHT = 24
# Context-sensitive footer (bug-237). The footer advertises only the actions that
# apply to the current dashboard state and always fits: it packs cell-aware into
# at most FOOTER_MAX_ROWS rows, dropping the lowest-priority hints from the tail —
# but never `? Help` / `q Quit`, the two keys a new user needs to find Help and
# escape. The BINDINGS stay unconditional; only the ADVERTISEMENT is contextual
# (the Help screen remains the full key reference).
FOOTER_MAX_ROWS = 2
# Cells the footer row loses to chrome: terminal-shell border (1 each side) plus
# the footer's own `padding: 0 2` (2 each side).
FOOTER_CHROME_CELLS = 6
# Cells rendered between two adjacent footer hints.
FOOTER_HINT_GAP = 2
# Always advertised, pinned last, never dropped by the width packer.
FOOTER_PROTECTED_HINTS = (("?", "Help"), ("q", "Quit"))
# Fixed cell cost of the status badge box around its label: solid border (1 each
# side) + horizontal padding 0 1 (1 each side) + the width-3 status dot.
HEADER_BADGE_CHROME_CELLS = 7
# Cells the header reserves between adjacent segments (CSS margin-right: 2).
HEADER_SEGMENT_GAP = 2
LOADING_PHASES = {
    Phase.STARTING,
    Phase.RESOLVING_MODEL,
    Phase.DOWNLOADING_MODEL,
    Phase.LOADING_WEIGHTS,
    Phase.PROFILING_KV,
    Phase.CAPTURING_GRAPHS,
    Phase.SERVER_STARTING,
}
# The agent-busy overlay borrows the loading-phase chrome (amber pulse) so a
# transient RPC verb reads exactly like a run loading state without being bound
# to any real run phase.
BUSY_BADGE_PHASE = Phase.STARTING
# Run phases in which stale RUN progress records may no longer paint the
# transient panel (bug-237). IDLE is deliberately absent: background jobs
# (model/build downloads) stream progress while no run is active.
PROGRESS_SUPPRESSED_PHASES = frozenset(
    {Phase.READY, Phase.DEGRADED, Phase.STOPPED, Phase.ERROR}
)

# Result type for the shared _with_agent_busy RPC wrapper.
_T = TypeVar("_T")
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


def _section_error_code(exc: BaseException) -> str:
    """Short, visible marker for a best-effort wizard-section RPC failure (Part A #3).

    Production sections fail with ``TargetCallError`` → its ``.code`` (e.g.
    ``agent-unreachable``); any other error shows its type name. Either way the
    failure is now VISIBLE (a warning row + notify) instead of the old silent
    ``except Exception: {}`` empty-dropdown swallow.
    """
    return exc.code if isinstance(exc, TargetCallError) else type(exc).__name__


def _blocker_suffix(exc: TargetCallError) -> str:
    """Names the configs blocking a removal, when the agent reports them (J34)."""
    details = getattr(exc, "details", None)
    blockers = details.get("configs") if isinstance(details, dict) else None
    if isinstance(blockers, list) and blockers:
        return f" (pinned by: {', '.join(str(item) for item in blockers)})"
    return ""


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


def _target_connection_state_for_error(exc: TargetCallError) -> str:
    if exc.code == "version-mismatch":
        return "version-mismatch"
    if exc.code in {"agent-unreachable", "command-not-found", "ssh-auth", "ssh-failed"}:
        return "unreachable"
    return "disconnected"


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
    # The launch ("engine") and detached log-tail ("tail") workers run with
    # exit_on_error=False so an unexpected payload or a dropped link can never
    # crash the TUI; register them here so on_worker_state_changed still
    # surfaces their failure instead of swallowing it silently.
    "engine": "launch",
    "tail": "log stream",
    "restart": "restart",
    "engine-signal": "stop/kill",
    "quit": "quit",
    "target-switch": "target switch",
    "reattach": "reattach",
    # Task 1.2 carry-forward (Part B): the opener/verb/push worker groups. Each
    # has at least one body that can raise an UNGUARDED non-TargetCallError (the
    # shared connect-layer re-raise at minimum, plus concrete hazards like a
    # malformed-payload KeyError in config-preview or a ModelConfig.model_validate
    # ValidationError in a save path), so a failure must surface as a warning
    # rather than being swallowed by exit_on_error=False. Groups whose every
    # worker broad-guards live in SELF_REPORTING_WORKER_GROUPS instead.
    "new-deployment": "new deployment",
    "build-manager": "build manager",
    "model-manager": "model manager",
    "model-download": "model download",
    "flag-manager": "flag manager",
    "config-preview": "config preview",
    "target-config-push": "config push",
    "target-connection": "target connection",
    # The keepalive loop is a persistent drop-detection monitor in its OWN group
    # so an exclusive reconnect (group "target-connection") can't cancel it
    # (bug-253). Its per-ping link failures are caught inside _target_keepalive_once
    # and routed to connection-state chrome, but the loop body ALSO reloads the
    # registry + refreshes target-backed views on a recovery flip and calls
    # _refresh_chrome outside the guarded ping — none of that self-guards, so a
    # truly-unexpected fault must surface as a warning here rather than silently
    # killing drop detection.
    "target-keepalive": "target keepalive",
    "job-cancel": "job cancel",
    "manager-reopen": "manager",
}

# Worker groups whose every spawned body fully self-guards (a broad try/except
# that reports its own outcome) and so does NOT need the on_worker_state_changed
# backstop. Kept EXPLICIT + separate from OPTIONAL_MONITOR_GROUP_LABELS so the
# structural test forces every group to make the monitored-vs-self-reporting
# choice consciously (Task 1.2 carry-forward, Part B).
#   * detached-discovery: `_refresh_detached_runs` wraps its `discover_runs` RPC
#     in `except Exception` and degrades to an empty run list by design — a
#     best-effort background poll must not nag on a flaky link, so a monitor
#     notification would be noise rather than signal.
SELF_REPORTING_WORKER_GROUPS = frozenset({"detached-discovery"})

ERROR_GUIDANCE = {
    ErrorKind.OOM: "Try lowering gpu_memory_utilization or max_model_len.",
    ErrorKind.PORT_IN_USE: "Choose a different server.port or stop the process using it.",
    ErrorKind.IMAGE_NOT_FOUND: "Pull or correct command.docker.image and its digest.",
    ErrorKind.DAEMON_UNREACHABLE: "Start Docker or check the daemon socket on the target.",
    ErrorKind.NAME_CONFLICT: "Remove the existing container or add it to command.docker.evict.",
    ErrorKind.GPU_NOT_AVAILABLE: "Check --gpus, the NVIDIA runtime, driver, and target GPU.",
    ErrorKind.MODEL_NOT_FOUND: "Check the model path/name and Hugging Face access.",
    ErrorKind.TP_MISMATCH: "Check tensor_parallel_size, pipeline_parallel_size, and visible GPUs.",
    ErrorKind.HF_AUTH: (
        "Accept the model license on huggingface.co, then set HF_TOKEN in the "
        "target agent's environment or the config's env: block."
    ),
    ErrorKind.API_KEY_AUTH: "Check server.api_key/VLLM_API_KEY for the running server.",
    ErrorKind.DISK_FULL: "Free disk space on the target or move runs/cache paths.",
    ErrorKind.COMMAND_NOT_FOUND: "install vLLM or set command.entrypoint: module.",
    ErrorKind.CONFIG_INVALID: "Fix the config or choose a compatible vLLM version_profile.",
    ErrorKind.CRASHED: "Check the last log lines and resolved command.",
    ErrorKind.TIMED_OUT: "Check /health, model load progress, GPU memory, and network binding.",
}

DEFAULT_MAX_LOG_LINES = 50_000
DEFAULT_LOG_BATCH_INTERVAL_SECONDS = 0.025
# bug-234: the quit-stop worker waits this long (seconds) for current_run_id to
# clear after signalling stop before giving up and rendering an "unreachable"
# banner. Tests inject a small value via app._quit_stop_wait_timeout_seconds.
DEFAULT_QUIT_STOP_WAIT_SECONDS = 30.0


class VelaApp(App):
    HORIZONTAL_BREAKPOINTS = [(0, "-compact"), (60, "-narrow"), (100, "-wide")]

    CSS = """
    Screen { layout: vertical; background: #091015; color: #e8f1f2; }
    /* bug-237: unambiguous Checkbox states via theme tokens. Unchecked reads as
       a dim slate block (TEXT_FAINT #56707c); checked is bright green
       (GREEN #67e8a5) with a dark glyph (TEXT_ON_ACCENT #06120c). Default
       Textual gives both states the SAME near-invisible background. Global so
       every modal Checkbox (wizard Download-now, Flag Manager Changed-only, …)
       inherits it. */
    Checkbox > .toggle--button {
        color: #56707c;
        background: #56707c;
        text-style: bold;
    }
    Checkbox.-on > .toggle--button {
        color: #06120c;
        background: #67e8a5;
    }
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
        width: auto;
        margin-right: 2;
        color: #60d7f8;
        text-style: bold;
        content-align: left middle;
    }
    #target-segment {
        width: auto;
        margin-right: 2;
        color: #8ba4ae;
        content-align: left middle;
    }
    #active-model {
        width: 1fr;
        min-width: 0;
        margin-right: 2;
        color: #e8f1f2;
        text-style: bold;
        content-align: left middle;
    }
    #server-url {
        width: auto;
        margin-right: 2;
        color: #8ba4ae;
        content-align: left middle;
    }
    #chrome-clock {
        width: auto;
        color: #8ba4ae;
        content-align: left middle;
    }
    #body { height: 1fr; padding: 1 2; }
    #sidebar { width: 34; min-width: 24; height: 1fr; margin-right: 2; }
    #main { width: 1fr; }
    #sidebar-overlay {
        height: auto;
        max-height: 7;
        margin-bottom: 1;
        background: #101923;
        border: solid #274254;
        padding: 0 1;
    }
    #config-panel { height: auto; max-height: 9; }
    /* 13 = title + 8 workflow rows + terminal marker row + Overall + border 2
       (bug-237: at 12 the terminal row pushed Overall past the clip). */
    #phase-panel { height: auto; max-height: 13; }
    #gpu-panel { height: auto; max-height: 11; }
    .side-panel {
        height: auto;
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
        width: auto;
        margin-right: 2;
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
        width: auto;
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
        height: auto;
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
        ("n", "new_deployment", "New"),
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
        self._new_deployment_presets: list[dict[str, Any]] = []
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
        self._pending_config_push: dict[str, str] | None = None
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
        # Operator stop/kill verbs recorded by run_id BEFORE signalling (the
        # sidecar-intent idiom), so the terminal STOPPED can say which verb
        # closed the run: "Stopped <id>" vs "Killed <id>" (bug-237).
        self._operator_signal_verbs: dict[str, str] = {}
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
                with VerticalScroll(id="sidebar"):
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
        self._apply_responsive_layout(self.size.width, self.size.height)
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
            group="target-keepalive",
            exclusive=True,
            exit_on_error=False,
        )
        self._write_log("INFO Vela ready")
        if not self.registry.valid and not self.registry.invalid:
            # First-run quick start (J13): an empty install must not be a
            # dead end. Gone as soon as any config exists.
            self._write_log("INFO Quick start:")
            self._write_log(
                "INFO   t  add or bootstrap a target (local works out of the box)"
            )
            self._write_log(
                "INFO   n  create a deployment — pin a model & build inside the wizard"
            )
            self._write_log("INFO   ⏎  review · s saves & smoke-tests it")
            self._write_log("INFO   l  launch the saved config")
        self._debug_event(
            "app.mounted",
            configs_dir=str(self.configs_dir) if self.configs_dir is not None else None,
            valid_configs=len(self.registry.valid),
            invalid_configs=len(self.registry.invalid),
        )

    def on_resize(self, event: events.Resize) -> None:
        self._apply_responsive_layout(event.size.width, event.size.height)

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
            "New Deployment",
            "Create a target-local deployment config",
            self.action_new_deployment,
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
                f"Bootstrap target: {target.name}",
                f"Show the bootstrap command for {target.name}",
                lambda selected=target.name: self._handle_target_manager_selection(
                    TargetManagerRequest("bootstrap", selected)
                ),
            )
            if self.target_name == "local" and self.current_config is not None:
                yield SystemCommand(
                    f"Push selected config to: {target.name}",
                    f"Push {self.current_config.name} to {target.name}",
                    lambda selected=target.name: self._handle_target_manager_selection(
                        TargetManagerRequest("push_config", selected)
                    ),
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
        for item in self.registry.valid:
            name = item.config.name
            yield SystemCommand(
                f"Clone deployment: {name}",
                f"Open the wizard prefilled from {name}",
                lambda selected=name: self._clone_deployment_from_palette(selected),
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
                    exit_on_error=False,
                ),
            )

    def _load_config_from_palette(self, name: str) -> None:
        self.select_config(name)
        self.action_load()

    def action_help(self) -> None:
        self.push_screen(HelpScreen(id="help"))

    def _reopen_manager_later(self, opener_factory) -> None:
        """Reopen a manager AFTER the dismissing modal fully settles.

        Pushing a screen while another screen's dismissal is still being
        processed gets the new screen popped by that dismissal — defer past
        it with a short timer, then re-check the stack (J30).
        """

        def _push() -> None:
            if len(self.screen_stack) != 1:
                return
            self.run_worker(
                opener_factory(),
                name="manager-reopen",
                group="manager-reopen",
                exclusive=True,
                exit_on_error=False,
            )

        self.set_timer(0.05, _push)

    def push_config_affordance(self) -> None:
        """Picker Ctrl+U: route into the target manager's push flow (J36)."""
        self.notify("Pick a target — p pushes the selected config to it")
        self.action_targets()

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
            elif selection.action == "bootstrap" and selection.target_name is not None:
                self._show_target_bootstrap_command(selection.target_name)
            elif selection.action == "push_config" and selection.target_name is not None:
                self.run_worker(
                    self._push_selected_config_to_target(selection.target_name),
                    name="target-config-push",
                    group="target-config-push",
                    exclusive=True,
                    exit_on_error=False,
                )
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

    def _show_target_bootstrap_command(self, target_name: str) -> None:
        try:
            target = load_targets_file().by_name(target_name)
        except Exception as exc:
            self._set_error_text(f"Unable to build bootstrap command: {exc}", style=f"bold {BAD}")
            return
        command = _target_bootstrap_command(target)
        self._set_error_text(
            f"Bootstrap target with:\n{command}",
            style=f"bold {ACCENT}",
        )
        self._write_log(f"INFO bootstrap command for {target.name}: {command}", "INFO")

    async def _push_selected_config_to_target(self, target_name: str) -> None:
        if self.current_config is None:
            self._set_error_text("Select a local config before pushing it to a target")
            return
        if self.target_name != "local":
            self._set_error_text(
                "Switch to the local target and select a local config before pushing",
                style=f"bold {WARN}",
            )
            return
        if target_name == "local":
            self._set_error_text("Select a remote target to push this config")
            return
        config_item = self._selected_valid_config_item()
        if config_item is None:
            self._set_error_text(
                f"Unable to find local YAML for {self.current_config.name}",
                style=f"bold {BAD}",
            )
            return
        try:
            yaml_text = config_item.path.read_text(encoding="utf-8")
        except OSError as exc:
            self._set_error_text(f"Unable to read local config: {exc}", style=f"bold {BAD}")
            return
        await self._push_config_yaml_to_target(
            target_name,
            self.current_config.name,
            yaml_text,
            overwrite=False,
        )

    async def _push_config_yaml_to_target(
        self,
        target_name: str,
        config_name: str,
        yaml_text: str,
        *,
        overwrite: bool,
    ) -> None:
        try:
            target = load_targets_file().by_name(target_name)
            target_client = target_client_for_config(target)
        except Exception as exc:
            self._set_error_text(
                f"Unable to connect to target {target_name}: {exc}",
                style=f"bold {BAD}",
            )
            return
        try:
            await target_client.connect()
            params: dict[str, object] = {"name": config_name, "yaml": yaml_text}
            if overwrite:
                params["overwrite"] = True
            result = await target_client.call("push_config", params)
        except TargetCallError as exc:
            if exc.code == "config-exists" and not overwrite:
                self._confirm_push_config_overwrite(
                    target_name,
                    config_name,
                    yaml_text,
                    str(exc.details.get("path") or ""),
                )
                return
            self._set_error_text(
                f"Unable to push config to {target_name}: {exc}",
                style=f"bold {BAD}",
            )
            return
        except Exception as exc:
            self._set_error_text(
                f"Unable to push config to {target_name}: {exc}",
                style=f"bold {BAD}",
            )
            return
        finally:
            disconnect = getattr(target_client, "disconnect", None)
            if callable(disconnect):
                with contextlib.suppress(Exception):
                    await disconnect()
        path = result.get("path", "")
        verb = "Updated" if overwrite else "Pushed"
        self.notify(f"{verb} {config_name} on {target_name}")
        self._write_log(
            f"INFO {verb.lower()} config {config_name} on {target_name}: {path}",
            "INFO",
        )

    def _confirm_push_config_overwrite(
        self,
        target_name: str,
        config_name: str,
        yaml_text: str,
        remote_path: str,
    ) -> None:
        self._pending_config_push = {
            "target": target_name,
            "name": config_name,
            "yaml": yaml_text,
            "path": remote_path,
        }
        path_text = f"\n\nExisting path: {remote_path}" if remote_path else ""
        self.push_screen(
            ConfirmScreen(
                (
                    f"Overwrite config {config_name} on {target_name}?"
                    f"{path_text}\n\nThe remote config will be replaced."
                ),
                title="Overwrite target config",
                confirm_label="Overwrite",
                confirm_action="confirm_push_config_overwrite",
            )
        )

    def _selected_valid_config_item(self) -> ValidConfig | None:
        if self.current_config is None:
            return None
        for item in self.registry.valid:
            if item.config.name == self.current_config.name:
                return item
        return None

    def confirm_push_config_overwrite(self) -> None:
        if self.screen.id == "confirm":
            self.pop_screen()
        pending = self._pending_config_push
        self._pending_config_push = None
        if pending is None:
            return
        self.run_worker(
            self._push_config_yaml_to_target(
                pending["target"],
                pending["name"],
                pending["yaml"],
                overwrite=True,
            ),
            name="target-config-push-overwrite",
            group="target-config-push",
            exclusive=True,
            exit_on_error=False,
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
        # Both RPCs (config preview + preset list) run behind ONE busy overlay.
        # The compound loader returns a dict (never None on success) so the
        # _with_agent_busy None sentinel stays unambiguous (3.2 constraint).
        result = await self._with_agent_busy(
            "loading flags…",
            self._load_flag_manager_sections(),
        )
        if result is None:
            return
        self.push_screen(
            FlagManagerScreen(
                self.current_config,
                preview=self.selected_config_preview,
                metadata=self.selected_config_metadata,
                presets=result["presets"],
                selected_preset=_optional_str(
                    self.selected_config_metadata.get("selected_preset")
                ),
                preview_resolver=self._preview_flag_manager_draft,
            ),
            callback=self._handle_flag_manager_selection,
        )

    async def _load_flag_manager_sections(self) -> dict[str, Any]:
        # Compound loader for _open_flag_manager's busy overlay. Both sub-calls
        # self-guard (preview → "Preview unavailable"; presets → []), so this
        # returns a populated dict rather than propagating — the flag manager
        # still opens in a degraded state instead of aborting.
        await self._refresh_selected_config_preview()
        presets = await self._load_flag_manager_presets()
        return {"presets": presets}

    async def _load_flag_manager_presets(self) -> list[dict[str, Any]]:
        if not self._target_supports_capability("list_presets"):
            return []
        try:
            result = await self._target_call("list_presets", {})
        except Exception:
            return []
        presets = result.get("presets")
        if not isinstance(presets, list):
            return []
        return [dict(item) for item in presets if isinstance(item, dict)]

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

    async def _open_build_manager(self, *, focus_build: str | None = None) -> None:
        result = await self._with_agent_busy(
            "loading builds…",
            self._target_call("list_builds", {"configs_dir": str(self.configs_dir)}),
        )
        if result is None:
            return
        self.push_screen(
            BuildManagerScreen(result, focus_build=focus_build),
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
                    AdoptBuildScreen(
                    probe=self._probe_adopt_venv,
                    discover=self._discover_adopt_venvs,
                ),
                    callback=self._handle_adopt_build_submission,
                )
            elif action == "pin_config_build":
                build = _optional_str(selection.get("build"))
                aliases = {
                    alias
                    for alias in (
                        build,
                        _optional_str(selection.get("build_id")),
                        _optional_str(selection.get("label")),
                    )
                    if alias
                }
                if build is not None:
                    self.run_worker(
                        self._pin_build_to_current_config(build, aliases=aliases),
                        name="build-pin-config",
                        group="build-manager",
                        exclusive=True,
                        exit_on_error=False,
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
        result = await self._with_agent_busy(
            "selecting build…",
            self._target_call("select_build", {"build": build}),
        )
        if result is None:
            self._reopen_manager_later(lambda: self._open_build_manager(focus_build=build))
            return
        label = _optional_str(result.get("label")) or build
        self.notify(f"Selected build: {label} — now the default for unpinned configs")
        if self.current_config is not None:
            await self._refresh_selected_config_preview()
        self._refresh_target_backed_views()

    async def _pin_build_to_current_config(
        self, build: str, *, aliases: set[str] | None = None
    ) -> None:
        """Toggle a build pin on the selected config (J31)."""
        cfg = self.current_config
        if cfg is None:
            self.notify("Select a config first — l loads, c picks one", severity="warning")
            self._reopen_manager_later(lambda: self._open_build_manager(focus_build=build))
            return
        currently_pinned = str(cfg.command.build) if cfg.command.build is not None else None
        known = aliases or {build}
        new_build: str | None = None if currently_pinned in known else build
        result = await self._with_agent_busy(
            "updating build pin…",
            self._target_call(
                "set_config_build",
                {
                    "name": cfg.name,
                    "configs_dir": str(self.configs_dir),
                    "build": new_build,
                },
            ),
        )
        if result is None:
            self._reopen_manager_later(lambda: self._open_build_manager(focus_build=build))
            return
        if new_build is None:
            self.notify(
                f"Unpinned build from {cfg.name} — it now uses the default build"
            )
        else:
            self.notify(f"Pinned build {new_build} to {cfg.name}")
        self.registry = await self._load_registry_from_agent()
        try:
            self.current_config = self.registry.by_name(cfg.name)
        except KeyError:
            pass
        if self.current_config is not None:
            await self._refresh_selected_config_preview()
        self._refresh_target_backed_views()
        self._reopen_manager_later(lambda: self._open_build_manager(focus_build=build))

    async def _verify_build(self, build: str) -> None:
        result = await self._with_agent_busy(
            "verifying build…",
            self._target_call("verify_build", {"build": build}),
        )
        if result is None:
            return
        label = _optional_str(result.get("label")) or build
        status = _optional_str(result.get("status")) or "verified"
        self.notify(f"Verified build: {label} ({status})")
        if self.current_config is not None:
            await self._refresh_selected_config_preview()
        self._refresh_target_backed_views()
        self._reopen_manager_later(lambda: self._open_build_manager(focus_build=build))

    async def _repair_build(self, build: str) -> None:
        result = await self._with_agent_busy(
            "repairing build…",
            self._target_call("repair_build", {"build": build}),
        )
        if result is None:
            return
        label = _optional_str(result.get("label")) or build
        detail = _optional_str(result.get("detail")) or _optional_str(result.get("status"))
        suffix = f" ({detail})" if detail else ""
        self.notify(f"Repaired build: {label}{suffix}")
        if self.current_config is not None:
            await self._refresh_selected_config_preview()
        self._refresh_target_backed_views()
        self._reopen_manager_later(lambda: self._open_build_manager(focus_build=build))

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
        with self._busy_badge("removing build…"):
            try:
                result = await self._target_call(
                    "remove_build",
                    {"build": build, "configs_dir": str(self.configs_dir)},
                )
            except TargetCallError as exc:
                self._set_error_text(
                    f"Unable to remove build: {exc}{_blocker_suffix(exc)}",
                    style=f"bold {BAD}",
                )
                return
        removed_label = _optional_str(result.get("label")) or label
        self.notify(f"Removed build: {removed_label}")
        if self.current_config is not None:
            await self._refresh_selected_config_preview()
        self._refresh_target_backed_views()

    def _handle_create_build_submission(self, params: dict[str, Any] | None) -> None:
        if not params:
            return
        if params.get("action") == "install_uv":
            form_values = params.get("params")
            self.run_worker(
                self._install_uv_then_reopen(
                    dict(form_values) if isinstance(form_values, dict) else {}
                ),
                name="uv-install",
                group="build-manager",
                exclusive=True,
                exit_on_error=False,
            )
            return
        self.run_worker(
            self._create_build(params, reopen_form_on_uv_failure=True),
            name="build-create",
            group="build-manager",
            exclusive=True,
            exit_on_error=False,
        )

    async def _install_uv_then_reopen(self, form_values: dict[str, Any]) -> None:
        """One-key uv install from the uv-block state (J37, OB R3a)."""
        self.notify("Installing uv — output streams below · s cancels")
        result = await self._run_target_job(
            "install_uv",
            {"job_id": uuid.uuid4().hex},
            error_action="install uv",
            incomplete_label="uv install",
        )
        if result is not None and result.get("ok") is True:
            self.notify("uv installed — nightly & commit builds unlocked")
            uv_available: bool | None = True
            try:
                probe = await self._target_call(
                    "check_build_prerequisites", {"method": "pip"}
                )
                uv_value = probe.get("uv_available")
                if isinstance(uv_value, bool):
                    uv_available = uv_value
            except TargetCallError:
                pass
            self.call_later(
                self._push_create_build_form, dict(form_values), "", uv_available
            )
            return
        self.call_later(
            self._push_create_build_form,
            dict(form_values),
            "uv install did not complete — details in the log.",
            False,
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
            self._set_error_text(
                self._render_target_call_error("Unable to create build", exc),
                style=f"bold {BAD}",
            )
            return
        job_params = dict(params)
        job_params["job_id"] = uuid.uuid4().hex
        self.notify("Build started — install log streams below · s cancels")
        result = await self._run_target_job(
            "create_build",
            job_params,
            error_action="create build",
            incomplete_label="Build creation",
        )
        if result is not None and result.get("ok") is True:
            label = _optional_str(result.get("label")) or "build"
            self.notify(
                f"Build ready: {label} — ⏎ in Builds makes it the default, "
                "or pin it in a deployment"
            )
            if len(self.screen_stack) == 1:
                await self._open_build_manager(
                    focus_build=_optional_str(result.get("build_id")) or label
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
        callback: Callable[[dict[str, Any] | None], None] | None = None,
    ) -> None:
        self.push_screen(
            CreateBuildScreen(
                initial=params,
                error_message=error_message,
                uv_available=uv_available,
                target_label=self._target_label(),
            ),
            callback=callback or self._handle_create_build_submission,
        )

    def _render_uv_prerequisite_error(
        self,
        params: dict[str, Any],
        exc: TargetCallError,
    ) -> str:
        method = _optional_str(exc.details.get("method")) or _optional_str(params.get("method"))
        method_label = f"{method} " if method else ""
        target = self._target_label()
        remediation = remediation_for_error(
            exc.code,
            target_name=target,
            details=exc.details,
        )
        fix = f" {remediation.fix}" if remediation is not None else ""
        return (
            f"{method_label}build creation requires uv on {target}. "
            "Install uv on the target or choose pip, wheel, or git."
            f"{fix}"
        )

    def _render_target_call_error(self, prefix: str, exc: TargetCallError) -> str:
        remediation = remediation_for_error(
            exc.code,
            target_name=self._target_label(),
            details=exc.details,
        )
        if remediation is None:
            return f"{prefix}: {exc}"
        lines = [f"{prefix}: {remediation.label}: {exc.message}"]
        lines.extend(remediation.extra_lines)
        lines.append(remediation.fix)
        return "\n".join(lines)

    async def _discover_adopt_venvs(self) -> list[dict[str, Any]]:
        """Candidate venvs for Adopt's picker (J35); never raises into the UI."""
        try:
            result = await self._target_call("discover_venvs", {})
        except Exception:
            return []
        venvs = result.get("venvs")
        if not isinstance(venvs, list):
            return []
        return [dict(item) for item in venvs if isinstance(item, dict)]

    async def _probe_adopt_venv(self, venv_path: str) -> dict[str, Any]:
        """Live venv validation for AdoptBuildScreen; never raises into the UI."""
        try:
            result = await self._target_call("inspect_venv", {"venv_path": venv_path})
        except Exception as exc:
            return {"ok": False, "reason": f"validation unavailable: {exc}"}
        if not isinstance(result, dict):
            return {"ok": False, "reason": "validation unavailable"}
        return result

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
        self.notify(
            f"Adopted build: {rendered} — ⏎ in Builds makes it the default, "
            "or pin it in a deployment"
        )
        if self.current_config is not None:
            await self._refresh_selected_config_preview()
        self._refresh_target_backed_views()
        if len(self.screen_stack) == 1:
            await self._open_build_manager(focus_build=build_id or label)

    async def _open_new_deployment_create_build_form(
        self,
        draft: dict[str, Any],
    ) -> None:
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
        self.call_later(
            self._push_new_deployment_create_build_form,
            dict(draft),
            {},
            "",
            uv_available,
        )

    def _push_new_deployment_create_build_form(
        self,
        draft: dict[str, Any],
        params: dict[str, Any],
        error_message: str,
        uv_available: bool | None = None,
    ) -> None:
        self._push_create_build_form(
            params,
            error_message,
            uv_available,
            callback=lambda selection: self._handle_new_deployment_create_build_submission(
                selection,
                dict(draft),
            ),
        )

    def _handle_new_deployment_create_build_submission(
        self,
        params: dict[str, Any] | None,
        draft: dict[str, Any],
    ) -> None:
        if params and params.get("action") == "install_uv":
            form_values = params.get("params")
            self.run_worker(
                self._install_uv_then_reopen(
                    dict(form_values) if isinstance(form_values, dict) else {}
                ),
                name="uv-install",
                group="build-manager",
                exclusive=True,
                exit_on_error=False,
            )
            return
        if not params:
            self.run_worker(
                self._open_new_deployment(initial=draft),
                name="new-deployment",
                group="new-deployment",
                exclusive=True,
                exit_on_error=False,
            )
            return
        self.run_worker(
            self._create_build_for_new_deployment(params, draft),
            name="new-deployment-create-build",
            group="new-deployment",
            exclusive=True,
            exit_on_error=False,
        )

    async def _create_build_for_new_deployment(
        self,
        params: dict[str, Any],
        draft: dict[str, Any],
    ) -> None:
        try:
            await self._target_call(
                "check_build_prerequisites",
                _build_prerequisite_params(params),
            )
        except TargetCallError as exc:
            if exc.details.get("reason") == "uv-required":
                self.call_later(
                    self._push_new_deployment_create_build_form,
                    dict(draft),
                    dict(params),
                    self._render_uv_prerequisite_error(params, exc),
                    False,
                )
                return
            self._set_error_text(
                self._render_target_call_error("Unable to create build", exc),
                style=f"bold {BAD}",
            )
            return
        job_params = dict(params)
        job_params["job_id"] = uuid.uuid4().hex
        result = await self._run_target_job(
            "create_build",
            job_params,
            error_action="create build",
            incomplete_label="Build creation",
        )
        if result is None or result.get("ok") is not True:
            return
        build_ref = _build_ref_from_build_result(result, params)
        await self._resume_new_deployment_with_build(draft, build_ref)

    def _handle_new_deployment_adopt_build_submission(
        self,
        params: dict[str, Any] | None,
        draft: dict[str, Any],
    ) -> None:
        if not params:
            self.run_worker(
                self._open_new_deployment(initial=draft),
                name="new-deployment",
                group="new-deployment",
                exclusive=True,
                exit_on_error=False,
            )
            return
        self.run_worker(
            self._adopt_build_for_new_deployment(params, draft),
            name="new-deployment-adopt-build",
            group="new-deployment",
            exclusive=True,
            exit_on_error=False,
        )

    async def _adopt_build_for_new_deployment(
        self,
        params: dict[str, Any],
        draft: dict[str, Any],
    ) -> None:
        try:
            result = await self._target_call("adopt_build", dict(params))
        except TargetCallError as exc:
            self._set_error_text(f"Unable to adopt build: {exc}", style=f"bold {BAD}")
            return
        build_ref = _build_ref_from_build_result(result, params)
        rendered = build_ref or "build"
        self.notify(f"Adopted build: {rendered}")
        await self._resume_new_deployment_with_build(draft, build_ref)

    async def _resume_new_deployment_with_build(
        self,
        draft: dict[str, Any],
        build_ref: str | None,
    ) -> None:
        if not build_ref:
            self._set_error_text("Build flow completed without a build id or label")
            return
        resumed = dict(draft)
        resumed["runtime"] = "build"
        resumed["build"] = build_ref
        resumed["step_index"] = 4
        await self._open_new_deployment(initial=resumed)

    def _handle_new_deployment_pin_model_submission(
        self,
        params: dict[str, Any] | None,
        draft: dict[str, Any],
    ) -> None:
        if not params:
            self.run_worker(
                self._open_new_deployment(initial=draft),
                name="new-deployment",
                group="new-deployment",
                exclusive=True,
                exit_on_error=False,
            )
            return
        self.run_worker(
            self._pin_model_for_new_deployment(params, draft),
            name="new-deployment-pin-model",
            group="new-deployment",
            exclusive=True,
            exit_on_error=False,
        )

    async def _pin_model_for_new_deployment(
        self,
        params: dict[str, Any],
        draft: dict[str, Any],
    ) -> None:
        params = dict(params)
        download_now = bool(params.pop("download_now", False))
        try:
            result = await self._target_call("pin_model", params)
        except TargetCallError as exc:
            self._set_error_text(f"Unable to pin model: {exc}", style=f"bold {BAD}")
            return
        entry = result.get("entry") if isinstance(result.get("entry"), dict) else {}
        model_ref = _model_ref_from_model_entry(entry, params)
        rendered = (
            _optional_str(entry.get("display_name"))
            or model_ref
            or _optional_str(params.get("repo_id"))
            or _optional_str(params.get("local_path"))
            or "model"
        )
        if download_now:
            self.notify(f"Pinned & downloading: {rendered} — progress streams below")
            self.run_worker(
                self._download_model({"model_ref": model_ref or rendered}),
                name="model-download",
                group="model-download",
                exclusive=True,
                exit_on_error=False,
            )
        else:
            self.notify(f"Pinned model: {rendered}")
        await self._resume_new_deployment_with_model(
            draft,
            entry,
            params,
            warnings=_warning_texts(result.get("warnings")),
        )

    async def _resume_new_deployment_with_model(
        self,
        draft: dict[str, Any],
        entry: dict[str, Any],
        params: dict[str, Any],
        *,
        warnings: list[str] | None = None,
    ) -> None:
        model_ref = _model_ref_from_model_entry(entry, params)
        if not model_ref:
            self._set_error_text("Model pin completed without a model id or label")
            return
        resumed = dict(draft)
        resumed["model_mode"] = "existing"
        resumed["model_ref"] = model_ref
        model_arg = _model_launch_arg_from_model_entry(entry, params)
        if model_arg:
            resumed["model"] = model_arg
        revision = _model_revision_from_model_entry(entry, params)
        if revision:
            resumed["model_revision"] = revision
        pin_warnings = list(warnings or [])
        if pin_warnings:
            resumed["warnings"] = [*_warning_texts(resumed.get("warnings")), *pin_warnings]
        resumed["step_index"] = 4
        await self._open_new_deployment(initial=resumed)

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

    async def _open_model_manager(self, *, focus_model: str | None = None) -> None:
        result = await self._with_agent_busy(
            "loading models…",
            self._target_call("list_models", {"configs_dir": str(self.configs_dir)}),
        )
        if result is None:
            return
        models = result.get("models")
        # Kept for the remove-confirm's reclaim estimate (J17).
        self._model_manager_models = (
            [dict(item) for item in models if isinstance(item, dict)]
            if isinstance(models, list)
            else []
        )
        self.push_screen(
            ModelManagerScreen(result, focus_model=focus_model),
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
                PinModelScreen(initial=initial, target_label=self.target_name),
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
        result = await self._run_target_job(
            "download_model",
            params,
            error_action="download model",
            incomplete_label="Model download",
        )
        if result is not None and result.get("ok") is True:
            model_ref = _optional_str(params.get("model_ref")) or "model"
            self.notify(f"Downloaded {model_ref} — cached on {self.target_name}")
            if len(self.screen_stack) == 1:
                await self._open_model_manager()

    async def _refresh_models(self) -> None:
        # Funnel the refresh verb through the shared busy convention (paints
        # "refreshing models…"; TargetCallError -> unified remediation banner +
        # None sentinel). This was the one RPC verb 3.2 missed (A5 ii).
        result = await self._with_agent_busy(
            "refreshing models…",
            self._target_call("refresh_models", {}),
        )
        if result is None:
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
        params = dict(params)
        # The screen-level flag never reaches the pin RPC; it kicks the
        # existing download job after a successful pin (J15).
        download_now = bool(params.pop("download_now", False))
        result = await self._with_agent_busy(
            "pinning model…",
            self._target_call("pin_model", params),
        )
        if result is None:
            return
        entry = result.get("entry") if isinstance(result.get("entry"), dict) else {}
        label = _optional_str(entry.get("display_name"))
        entry_id = _optional_str(entry.get("entry_id"))
        rendered = label or entry_id or _optional_str(params.get("entry_id")) or "model"
        if download_now:
            self.notify(f"Pinned & downloading: {rendered} — progress streams below")
        else:
            self.notify(f"Pinned model: {rendered} — d in Models downloads it now")
        self._record_warnings(_warning_texts(result.get("warnings")))
        if self.current_config is not None:
            await self._refresh_selected_config_preview()
        self._refresh_target_backed_views()
        if download_now:
            self.run_worker(
                self._download_model({"model_ref": entry_id or rendered}),
                name="model-download",
                group="model-download",
                exclusive=True,
                exit_on_error=False,
            )
            return
        if len(self.screen_stack) == 1:
            await self._open_model_manager(focus_model=entry_id or rendered)

    async def _verify_model(self, model_ref: str) -> None:
        result = await self._with_agent_busy(
            "verifying model…",
            self._target_call("verify_model", {"model_ref": model_ref}),
        )
        if result is None:
            return
        cache_state = _optional_str(result.get("cache_state")) or "verified"
        detail = _optional_str(result.get("detail"))
        suffix = f": {detail}" if detail else ""
        self.notify(f"Verified model: {model_ref} ({cache_state}){suffix}")
        if self.current_config is not None:
            await self._refresh_selected_config_preview()
        self._refresh_target_backed_views()
        self._reopen_manager_later(lambda: self._open_model_manager(focus_model=model_ref))

    def _confirm_remove_model(self, selection: dict[str, Any]) -> None:
        model_ref = _optional_str(selection.get("model_ref"))
        if model_ref is None:
            return
        label = _optional_str(selection.get("label")) or model_ref
        target_label = self._target_label()
        message = (
            f"Remove model {label} on {target_label}?"
            f"\n\nThis deletes the registry entry and any cached weights on "
            f"{target_label} — it cannot be undone."
            f"\n{self._model_remove_reclaim_line(model_ref)}"
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

    def _model_remove_reclaim_line(self, model_ref: str) -> str:
        for model in getattr(self, "_model_manager_models", []):
            if model_ref in {
                str(model.get("entry_id") or ""),
                str(model.get("model_ref") or ""),
                str(model.get("display_name") or ""),
            }:
                unique = model.get("unique_size_bytes") or model.get("size_bytes") or 0
                try:
                    unique = int(unique)
                except (TypeError, ValueError):
                    unique = 0
                if unique > 0:
                    return (
                        f"Frees up to {unique / 1_000_000_000:.1f} GB of cache "
                        "(unique, dedup-aware)."
                    )
                break
        return "No cached weights to reclaim."

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
        with self._busy_badge("removing model…"):
            try:
                result = await self._target_call(
                    "remove_model",
                    {"model_ref": model_ref, "configs_dir": str(self.configs_dir)},
                )
            except TargetCallError as exc:
                self._set_error_text(
                    f"Unable to remove model: {exc}{_blocker_suffix(exc)}",
                    style=f"bold {BAD}",
                )
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
    ) -> dict[str, Any] | None:
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
                return None
            return await self._consume_target_job_events_until_done(
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
    ) -> dict[str, Any] | None:
        async for event in events:
            if event.get("job_id") != job_id:
                continue
            self._post_wire_event_message(event)
            if event.get("event") != "job_done":
                continue
            if event.get("ok") and self.current_config is not None:
                await self._refresh_selected_config_preview()
            return dict(event)
        self._set_error_text(
            f"{incomplete_label} stream ended before completion: {job_id}",
            style=f"bold {BAD}",
        )
        return None

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
        try:
            await self._ensure_target_client_connected()
            # A restored link must restore the honest card: reload the registry and
            # re-render so a successful reconnect clears the "target unreachable"
            # state on the dashboard instead of leaving the offline copy frozen
            # (bug-252).
            self.registry = await self._load_registry_from_agent()
            if self.current_config is None and self.registry.valid:
                self.current_config = self.registry.valid[0].config
                await self._refresh_selected_config_preview()
            self._refresh_target_backed_views()
        finally:
            # If the Target Manager is still open on top of the stack, push the
            # fresh live state into it — on EVERY path (bug-237/bug-257): a
            # successful reconnect flips its frozen snapshot to the honest
            # connected card, and a FAILED one (_ensure_target_client_connected
            # re-raises after marking unreachable/disconnected, killing this
            # worker) must still replace the optimistic `reconnecting…` with the
            # truthful failed state instead of contradicting the chrome banner.
            self._refresh_open_target_manager()

    def _target_manager_state_payload(self) -> dict[str, object]:
        return {
            "active_target": self.target_name,
            "connection_state": self.target_connection_state,
            "connection_detail": self.target_connection_detail,
            "agent_info": dict(self._target_agent_info),
            "last_seen": self._target_last_seen_at,
            "active_runs": list(self.detached_run_summaries),
            "gpu_summary": self.gpu_panel_text,
        }

    def _refresh_open_target_manager(self) -> None:
        try:
            screen = self.screen
        except Exception:
            return
        if isinstance(screen, TargetManagerScreen):
            screen.refresh_target_state(self._target_manager_state_payload())

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

        # Surface the connect/registry-load as a pulsing "connecting to <target>…"
        # overlay — otherwise a slow SSH handshake is a silent wait (Part A #5).
        # _load_registry_from_agent self-guards TargetCallError (banner + empty
        # registry), so the None sentinel is defensive only.
        registry = await self._with_agent_busy(
            f"connecting to {self.target_name}…",
            self._load_registry_from_agent(),
        )
        self.registry = registry if registry is not None else ConfigRegistry()
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
        self.run_worker(
            self._run_selected_config(),
            name="load",
            group="engine",
            exclusive=True,
            exit_on_error=False,
        )

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
                exit_on_error=False,
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
                exit_on_error=False,
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
        # bug-279: don't stack a second id='confirm' screen (DuplicateIds crash)
        # when a ConfirmScreen is already open (e.g. via the palette 'Kill').
        if isinstance(self.screen, ConfirmScreen):
            return
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
                exit_on_error=False,
            )
            return
        if self.reattached_run_id is not None:
            self.run_worker(
                self._restart_reattached_target_run(),
                name="restart",
                group="restart",
                exclusive=True,
                exit_on_error=False,
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

    def _target_declares_capability(self, capability: str) -> bool:
        capabilities = self._target_agent_info.get("capabilities")
        if not isinstance(capabilities, list):
            return False
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
        self._cancel_monitor_workers()
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
                connection_state=self.target_connection_state,
            )
        )

    def action_new_deployment(self) -> None:
        if self._target_capability_blocked("compose_config", "deployment composer"):
            return
        self.run_worker(
            self._open_new_deployment(),
            name="new-deployment",
            group="new-deployment",
            exclusive=True,
            exit_on_error=False,
        )

    def _clone_deployment_from_palette(self, name: str) -> None:
        self.run_worker(
            self._clone_deployment(name),
            name="new-deployment-clone",
            group="new-deployment",
            exclusive=True,
            exit_on_error=False,
        )

    async def _clone_deployment(self, name: str) -> None:
        try:
            cfg = self.registry.by_name(name)
        except KeyError:
            self._set_error_text(f"Unable to clone: config not found: {name}")
            return
        draft: dict[str, Any] = {
            "name": f"{cfg.name}-2",
            "model": cfg.model,
            "host": cfg.server.host,
            "exposure": cfg.server.exposure.value,
            # Port deliberately blank: auto-allocation avoids cloning a
            # collision (J26).
        }
        runtime = cfg.command.runtime.value
        if runtime in {"process", "docker"}:
            draft["runtime"] = runtime
        if cfg.command.docker is not None and cfg.command.docker.image:
            draft["image"] = str(cfg.command.docker.image)
        if cfg.command.build is not None:
            draft["runtime"] = "build"
            draft["build"] = str(cfg.command.build)
        if cfg.model_ref:
            draft["model_ref"] = str(cfg.model_ref)
            draft["model_mode"] = "existing"
        else:
            draft["model_mode"] = "bare"
        if cfg.revision:
            draft["model_revision"] = str(cfg.revision)
        await self._open_new_deployment(initial=draft)

    async def _open_new_deployment(
        self,
        *,
        initial: dict[str, Any] | None = None,
        error_message: str = "",
    ) -> None:
        # All four RPCs (presets → recipes → models → builds) run behind ONE
        # busy overlay via a compound loader. presets is required: its failure
        # aborts the open (the loader lets the TargetCallError propagate, so
        # _with_agent_busy renders the unified banner and returns the None
        # sentinel). recipes/models/builds are best-effort — a failure records a
        # per-section marker so the wizard shows a visible warning row instead of
        # a silently-empty dropdown (Task 3.2 Part A #3).
        result = await self._with_agent_busy(
            "loading deployment options…",
            self._load_new_deployment_sections(),
        )
        if result is None:
            return
        presets = result["presets"]
        self._new_deployment_presets = [
            dict(item) for item in presets if isinstance(item, dict)
        ]
        section_errors = result["section_errors"]
        for section, code in section_errors.items():
            self.notify(f"{section} unavailable: {code}", severity="warning")
        self.call_later(
            self._push_new_deployment_screen,
            presets,
            result["recipes"],
            result["models"],
            result["builds"],
            initial,
            self._new_deployment_target_rows(),
            error_message,
            section_errors,
        )

    async def _load_new_deployment_sections(self) -> dict[str, Any]:
        # Compound loader for the wizard's ONE busy overlay. presets is REQUIRED:
        # its call is unguarded, so any failure propagates to _with_agent_busy
        # (TargetCallError → banner + None → abort; other errors → monitored
        # worker). recipes/models/builds are OPTIONAL enrichments and must never
        # abort the core wizard, so each self-guards and degrades to a per-section
        # error CODE — the wizard then renders a VISIBLE warning row + a notify,
        # replacing the old silent `except Exception: {}` empty-dropdown swallow.
        presets_result = await self._target_call("list_presets", {})
        presets = presets_result.get("presets")
        section_errors: dict[str, str] = {}
        recipes: object = []
        models: object = []
        builds: object = []
        if self._target_supports_capability("list_deployment_recipes"):
            try:
                recipe_result = await self._target_call(
                    "list_deployment_recipes", {"target": self.target_name}
                )
                recipes = recipe_result.get("recipes")
            except Exception as exc:
                section_errors["recipes"] = _section_error_code(exc)
                self._debug_event(
                    "new_deployment.section_failed",
                    section="recipes",
                    error=repr(exc),
                )
        if self._target_supports_capability("list_models"):
            try:
                models_result = await self._target_call("list_models", {})
                models = models_result.get("models")
            except Exception as exc:
                section_errors["models"] = _section_error_code(exc)
                self._debug_event(
                    "new_deployment.section_failed",
                    section="models",
                    error=repr(exc),
                )
        if self._target_supports_capability("list_builds"):
            try:
                builds_result = await self._target_call("list_builds", {})
                builds = builds_result.get("builds")
            except Exception as exc:
                section_errors["builds"] = _section_error_code(exc)
                self._debug_event(
                    "new_deployment.section_failed",
                    section="builds",
                    error=repr(exc),
                )
        return {
            "presets": presets if isinstance(presets, list) else [],
            "recipes": recipes if isinstance(recipes, list) else [],
            "models": models if isinstance(models, list) else [],
            "builds": builds if isinstance(builds, list) else [],
            "section_errors": section_errors,
        }

    def _push_new_deployment_screen(
        self,
        presets: list[dict[str, Any]],
        recipes: list[dict[str, Any]],
        models: list[dict[str, Any]],
        builds: list[dict[str, Any]],
        initial: dict[str, Any] | None,
        target_rows: list[dict[str, str]],
        error_message: str = "",
        section_errors: dict[str, str] | None = None,
    ) -> None:
        screen = NewDeploymentScreen(
            target_label=self.target_name,
            presets=presets,
            recipes=recipes,
            models=models,
            builds=builds,
            initial=initial,
            targets=target_rows,
            connection_state=self.target_connection_state,
            agent_info=self._target_agent_info,
            error_message=error_message,
            section_errors=section_errors,
            target_state_resolver=self._refresh_new_deployment_target_rows,
            suggestion_resolver=(
                self._suggest_new_deployment_defaults
                if self._target_declares_capability("suggest_deployment_defaults")
                else None
            ),
        )
        self.push_screen(
            screen,
            # The screen stashes its draft at submit; keeping the reference
            # lets a failed server-side review restore the wizard (J1).
            callback=lambda selection: self._handle_new_deployment_selection(
                selection, draft_source=screen
            ),
        )

    def _handle_new_deployment_selection(
        self, selection: object, draft_source: NewDeploymentScreen | None = None
    ) -> None:
        if not isinstance(selection, dict):
            return
        action = _optional_str(selection.get("action"))
        draft = selection.get("draft")
        draft_payload = dict(draft) if isinstance(draft, dict) else {}
        if action == "target":
            target = _optional_str(selection.get("target"))
            if target is not None:
                self.run_worker(
                    self._switch_new_deployment_target(target, draft_payload),
                    name="new-deployment-target-switch",
                    group="new-deployment",
                    exclusive=True,
                    exit_on_error=False,
                )
            return
        if action == "create_build":
            self.run_worker(
                self._open_new_deployment_create_build_form(draft_payload),
                name="new-deployment-create-build-form",
                group="new-deployment",
                exclusive=True,
                exit_on_error=False,
            )
            return
        if action == "adopt_build":
            self.push_screen(
                AdoptBuildScreen(
                    probe=self._probe_adopt_venv,
                    discover=self._discover_adopt_venvs,
                ),
                callback=lambda params: self._handle_new_deployment_adopt_build_submission(
                    params,
                    draft_payload,
                ),
            )
            return
        if action == "pin_model":
            initial = selection.get("initial") if isinstance(selection.get("initial"), dict) else {}
            self.push_screen(
                PinModelScreen(initial=initial, target_label=self.target_name),
                callback=lambda params: self._handle_new_deployment_pin_model_submission(
                    params,
                    draft_payload,
                ),
            )
            return
        draft = getattr(draft_source, "last_draft", None)
        self.run_worker(
            self._review_new_deployment(
                selection,
                draft=dict(draft) if isinstance(draft, dict) else None,
            ),
            name="new-deployment-review",
            group="new-deployment",
            exclusive=True,
            exit_on_error=False,
        )

    def _new_deployment_target_rows(self) -> list[dict[str, str]]:
        try:
            targets = load_targets_file().targets
        except Exception:
            targets = [self._target_config]
        if not any(target.name == self.target_name for target in targets):
            targets = [self._target_config, *targets]
        return [self._new_deployment_target_base_row(target) for target in targets]

    def _new_deployment_target_base_row(self, target: TargetConfig) -> dict[str, str]:
        row: dict[str, str] = {
            "name": target.name,
            "transport": target.transport.value,
            "host": target.host or "",
        }
        if target.name == self.target_name:
            row["connection_state"] = self.target_connection_state
            row["connection_detail"] = self.target_connection_detail
            agent_version = _optional_str(self._target_agent_info.get("agent_version"))
            if agent_version is not None:
                row["agent_version"] = agent_version
            return row
        row["connection_state"] = "connecting"
        row["connection_detail"] = "checking"
        return row

    async def _refresh_new_deployment_target_rows(self) -> list[dict[str, str]]:
        try:
            targets = load_targets_file().targets
        except Exception:
            targets = [self._target_config]
        if not any(target.name == self.target_name for target in targets):
            targets = [self._target_config, *targets]
        rows = await asyncio.gather(
            *(self._new_deployment_target_row(target) for target in targets)
        )
        return list(rows)

    async def _new_deployment_target_row(self, target: TargetConfig) -> dict[str, str]:
        row = self._new_deployment_target_base_row(target)
        if target.name == self.target_name:
            return row
        row.update(await self._probe_new_deployment_target(target))
        return row

    async def _probe_new_deployment_target(self, target: TargetConfig) -> dict[str, str]:
        target_client: Any | None = None
        try:
            target_client = target_client_for_config(target)
            agent_info = await asyncio.wait_for(
                target_client.connect(),
                timeout=NEW_DEPLOYMENT_TARGET_PROBE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            return {
                "connection_state": "unreachable",
                "connection_detail": "connection timeout",
            }
        except TargetCallError as exc:
            return {
                "connection_state": _target_connection_state_for_error(exc),
                "connection_detail": str(exc),
            }
        except Exception as exc:
            return {
                "connection_state": "unreachable",
                "connection_detail": str(exc),
            }
        finally:
            if target_client is not None:
                disconnect = getattr(target_client, "disconnect", None)
                if callable(disconnect):
                    with contextlib.suppress(Exception):
                        await disconnect()
        row = {"connection_state": "connected", "connection_detail": ""}
        if isinstance(agent_info, dict):
            agent_version = _optional_str(agent_info.get("agent_version"))
            if agent_version is not None:
                row["agent_version"] = agent_version
        return row

    async def _switch_new_deployment_target(
        self,
        target_name: str,
        draft: dict[str, Any],
    ) -> None:
        if target_name == self.target_name:
            await self._open_new_deployment(initial=draft)
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
        await self._switch_target(target_name)
        if self.target_name != target_name:
            return
        resumed = dict(draft)
        resumed["target"] = target_name
        resumed["selected_target"] = target_name
        await self._open_new_deployment(initial=resumed)

    async def _suggest_new_deployment_defaults(
        self, spec: dict[str, Any]
    ) -> dict[str, Any]:
        params = dict(spec)
        params.update(self._agent_params(configs_dir=self.configs_dir))
        return await self._target_call("suggest_deployment_defaults", params)

    async def _review_new_deployment(
        self, spec: dict[str, Any], *, draft: dict[str, Any] | None = None
    ) -> None:
        params = dict(spec)
        download_now = bool(params.pop("download_now", False))
        params.update(self._agent_params(configs_dir=self.configs_dir))

        async def fail(message: str) -> None:
            # Never discard the operator's typed draft on a server-side
            # failure: reopen the wizard with the error inside it (J1).
            if draft is not None:
                await self._open_new_deployment(initial=draft, error_message=message)
            else:
                self._set_error_text(message, style=f"bold {BAD}")

        try:
            if download_now:
                download_error = await self._download_new_deployment_model(params)
                if download_error is not None:
                    await fail(download_error)
                    return
            composed = await self._target_call("compose_config", params)
            config = composed.get("config")
            if not isinstance(config, dict):
                raise TargetCallError("compose-invalid", "composer returned no config")
            validation = await self._target_call("validate_config", {"config": config})
            if validation.get("ok") is not True:
                await fail(_format_validation_errors(validation))
                return
            preview = await self._target_call(
                "preview",
                {
                    "config": config,
                    **self._agent_params(configs_dir=self.configs_dir),
                },
            )
        except TargetCallError as exc:
            await fail(f"Unable to review deployment: {exc}")
            return
        warnings = [
            *_warning_texts(params.get("warnings")),
            *_warning_texts(composed.get("warnings")),
            *_warning_texts(validation.get("warnings")),
            *_warning_texts(preview.get("warnings")),
        ]
        derived = composed.get("derived")
        metadata = _preview_metadata(preview)
        selected_preset = _optional_str(params.get("preset"))
        if selected_preset is not None:
            metadata["selected_preset"] = selected_preset
        self.push_screen(
            NewDeploymentReviewScreen(
                config=config,
                preview=str(preview.get("preview") or ""),
                derived=derived if isinstance(derived, list) else [],
                warnings=warnings,
                metadata=metadata,
            ),
            callback=lambda selection: self._handle_new_deployment_review(
                selection, draft=draft
            ),
        )

    async def _download_new_deployment_model(self, params: dict[str, Any]) -> str | None:
        """Returns an error message on failure, None on success."""
        model_ref = _optional_str(params.get("model_ref"))
        if model_ref is None:
            return (
                f"{DOWNLOAD_NEEDS_PIN_ERROR}. "
                "Pin the HF repo or choose an existing pin."
            )
        job_params: dict[str, Any] = {"job_id": uuid.uuid4().hex, "model_ref": model_ref}
        revision = _optional_str(params.get("revision"))
        if revision is not None:
            job_params["revision"] = revision
        result = await self._run_target_job(
            "download_model",
            job_params,
            error_action="download model",
            incomplete_label="Model download",
        )
        if result is not None and result.get("ok") is True:
            return None
        return "Model download did not complete — details in the dashboard log."

    def _handle_new_deployment_review(
        self, selection: object, draft: dict[str, Any] | None = None
    ) -> None:
        if not isinstance(selection, dict):
            return
        if selection.get("action") == "back":
            # Back to the wizard with everything the operator typed (J2).
            self.run_worker(
                self._open_new_deployment(initial=draft),
                name="new-deployment-back",
                group="new-deployment",
                exclusive=True,
                exit_on_error=False,
            )
            return
        config = selection.get("config")
        if not isinstance(config, dict):
            return
        if selection.get("action") == "customize":
            self._open_new_deployment_flag_manager(
                config,
                preview=str(selection.get("preview") or ""),
                derived=(
                    selection.get("derived")
                    if isinstance(selection.get("derived"), list)
                    else []
                ),
                warnings=[
                    str(item)
                    for item in (
                        selection.get("warnings")
                        if isinstance(selection.get("warnings"), list)
                        else []
                    )
                ],
                metadata=(
                    selection.get("metadata")
                    if isinstance(selection.get("metadata"), dict)
                    else {}
                ),
            )
            return
        smoke_after_save = selection.get("action") == "save_smoke"
        self.run_worker(
            self._save_reviewed_new_deployment(config, smoke=smoke_after_save),
            name="new-deployment-smoke" if smoke_after_save else "new-deployment-save",
            group="new-deployment",
            exclusive=True,
            exit_on_error=False,
        )

    def _open_new_deployment_flag_manager(
        self,
        config: dict[str, Any],
        *,
        preview: str,
        derived: list[dict[str, Any]],
        warnings: list[str],
        metadata: dict[str, Any],
    ) -> None:
        try:
            cfg = ModelConfig.model_validate(config)
        except Exception as exc:
            self._set_error_text(f"Unable to customize deployment: {exc}", style=f"bold {BAD}")
            return
        self.push_screen(
            FlagManagerScreen(
                cfg,
                preview=preview,
                metadata=metadata,
                presets=self._new_deployment_presets,
                selected_preset=_optional_str(metadata.get("selected_preset")),
                preview_resolver=lambda selection: self._preview_new_deployment_flag_draft(
                    config, selection
                ),
            ),
            callback=lambda selection: self._handle_new_deployment_flag_selection(
                selection,
                config=config,
                preview=preview,
                derived=derived,
                warnings=warnings,
                metadata=metadata,
            ),
        )

    def _handle_new_deployment_flag_selection(
        self,
        selection: object,
        *,
        config: dict[str, Any],
        preview: str,
        derived: list[dict[str, Any]],
        warnings: list[str],
        metadata: dict[str, Any],
    ) -> None:
        if not isinstance(selection, dict) or selection.get("action") != "save_flags":
            self.push_screen(
                NewDeploymentReviewScreen(
                    config=config,
                    preview=preview,
                    derived=derived,
                    warnings=warnings,
                    metadata=metadata,
                ),
                callback=self._handle_new_deployment_review,
            )
            return
        self.run_worker(
            self._review_customized_new_deployment(
                config,
                selection,
                derived=derived,
                warnings=warnings,
            ),
            name="new-deployment-customize",
            group="new-deployment",
            exclusive=True,
            exit_on_error=False,
        )

    async def _review_customized_new_deployment(
        self,
        config: dict[str, Any],
        selection: dict[str, Any],
        *,
        derived: list[dict[str, Any]],
        warnings: list[str],
    ) -> None:
        try:
            updated_config = _draft_config_with_flag_updates(config, selection)
            validation = await self._target_call(
                "validate_config", {"config": updated_config}
            )
            if validation.get("ok") is not True:
                self._set_error_text(
                    _format_validation_errors(validation),
                    style=f"bold {BAD}",
                )
                return
            preview = await self._target_call(
                "preview",
                {
                    "config": updated_config,
                    **self._agent_params(configs_dir=self.configs_dir),
                },
            )
        except TargetCallError as exc:
            self._set_error_text(f"Unable to review deployment: {exc}", style=f"bold {BAD}")
            return
        except Exception as exc:
            self._set_error_text(f"Unable to customize deployment: {exc}", style=f"bold {BAD}")
            return
        refreshed_warnings = [
            *warnings,
            *[str(item) for item in validation.get("warnings") or []],
            *[str(item) for item in preview.get("warnings") or []],
        ]
        self.push_screen(
            NewDeploymentReviewScreen(
                config=updated_config,
                preview=str(preview.get("preview") or ""),
                derived=derived,
                warnings=refreshed_warnings,
                metadata=_preview_metadata(preview),
            ),
            callback=self._handle_new_deployment_review,
        )

    async def _preview_new_deployment_flag_draft(
        self,
        config: dict[str, Any],
        selection: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            updated_config = _draft_config_with_flag_updates(config, selection)
            return await self._target_call(
                "preview",
                {
                    "config": updated_config,
                    **self._agent_params(configs_dir=self.configs_dir),
                },
            )
        except TargetCallError as exc:
            return {
                "preview": f"Preview unavailable: {exc}",
                "warnings": [],
                "metadata": {},
            }
        except Exception as exc:
            return {
                "preview": f"Preview unavailable: {exc}",
                "warnings": [],
                "metadata": {},
            }

    async def _save_reviewed_new_deployment(
        self, config: dict[str, Any], *, smoke: bool = False
    ) -> None:
        save_params = self._agent_params(
            name=str(config.get("name") or ""),
            configs_dir=self.configs_dir,
        )
        save_params["config"] = config
        try:
            preflight = await self._target_call(
                "preflight",
                {
                    "config": config,
                    **self._agent_params(configs_dir=self.configs_dir),
                },
            )
            if not self._handle_preflight_result(preflight):
                return
            saved = await self._target_call("save_config", save_params)
        except TargetCallError as exc:
            self._set_error_text(f"Unable to save deployment: {exc}", style=f"bold {BAD}")
            return
        saved_config = saved.get("config")
        if isinstance(saved_config, dict):
            self.current_config = ModelConfig.model_validate(saved_config)
        self.registry = await self._load_registry_from_agent()
        if self.current_config is not None:
            try:
                self.current_config = self.registry.by_name(self.current_config.name)
            except KeyError:
                pass
            await self._refresh_selected_config_preview()
        self._refresh_chrome()
        self.notify(f"Saved deployment: {saved.get('name') or config.get('name')}")
        if smoke:
            await self._run_saved_config_smoke()

    async def _run_saved_config_smoke(self) -> None:
        cfg = self.current_config
        if cfg is None:
            self._set_error_text("No saved deployment selected for smoke")
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
        self.current_run_id = run_id
        wait_needed = True
        smoke_passed = False
        try:
            probe = await self._target_call("probe_until_ready", {"run_id": run_id})
            error_kind = None
            if probe.get("error_kind") is not None:
                error_kind = _error_kind_from_agent_payload(probe.get("error_kind"))
            ready = bool(probe.get("ready"))
            detail = str(probe.get("detail") or "")
            self._handle_health_changed(
                ready=ready,
                detail=detail,
                models=[str(model) for model in probe.get("models") or []],
                error_kind=error_kind,
                reachable_url=_optional_str(probe.get("reachable_url")),
            )
            if not ready:
                if error_kind is None:
                    self._set_error_text(f"Smoke did not reach READY: {detail}")
                self.notify(
                    f"Smoke failed — config '{cfg.name}' is saved · "
                    "F adjust flags · l retry",
                    severity="warning",
                )
                return
            suffix = f" ({', '.join(self.served_models)})" if self.served_models else ""
            self.notify(f"Smoke READY: {self.ready_url or self._server_url(cfg)}{suffix}")
            smoke_passed = True
        except TargetCallError as exc:
            self._handle_launch_agent_error(exc)
            return
        finally:
            if self.current_run_id == run_id:
                await self._target_stop_run(
                    run_id,
                    interrupt_timeout=2,
                    terminate_timeout=2,
                )
                try:
                    await asyncio.wait_for(
                        self._target_call("wait", {"run_id": run_id}),
                        timeout=_smoke_stop_timeout_seconds(cfg),
                    )
                except asyncio.TimeoutError:
                    wait_needed = False
                    self._set_error_text(f"Smoke stop timed out for {run_id}")
                except Exception as exc:
                    wait_needed = False
                    self._set_error_text(f"Unable to wait for smoke stop: {exc}")
                if wait_needed:
                    self.current_run_id = None
                    self._set_phase(Phase.STOPPED)
                if smoke_passed:
                    # The auto-stop is intentional — say so and name the next
                    # step, or STOPPED reads like a failure (J6).
                    self.notify(
                        f"Smoke passed — '{cfg.name}' saved & selected · "
                        "press l to launch"
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
        self._cancel_monitor_workers()
        sidecar_name = str(self.reattached_run_id)
        self.reattached_run_id = None
        self.current_run_id = None
        self._write_log(f"INFO detached from {sidecar_name}; server continues running")
        self.notify("Detached from run; server continues running")

    def _has_reattached_run(self) -> bool:
        return self.reattached_run_id is not None

    def _cancel_monitor_workers(self) -> None:
        """Cancel the tail + health monitor workers (the detach/quit teardown pair)."""
        self.workers.cancel_group(self, "tail")
        self.workers.cancel_group(self, "health")

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
        self._cancel_monitor_workers()
        self.reattached_run_id = None
        self._set_phase(Phase.STOPPED)
        self._announce_operator_shutdown(run_id, action)

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
        # bug-279 (1.3 carry-forward): a ConfirmScreen is already the top of the
        # stack (e.g. the user hit q, then picked 'Quit app' from the palette).
        # Pushing another id='confirm' screen raises DuplicateIds and crashes the
        # app; no-op so exactly one confirm is ever live.
        if isinstance(self.screen, ConfirmScreen):
            return
        if self._attached_run_is_alive():
            # bug-234 follow-up: with the target unreachable, stop/kill/detach/
            # target-switch are all blocked, so a banner-and-return here would
            # leave no in-app way to quit. Offer quit-without-stopping instead.
            if self.target_connection_state != "connected":
                run_id = self.current_run_id
                self.push_screen(
                    ConfirmScreen(
                        f"Cannot stop run {run_id} from here. "
                        "Quit and leave it running on the target?",
                        title="Confirm quit — target unreachable",
                        confirm_label="Quit without stopping",
                        confirm_action="confirm_quit_without_stop",
                    )
                )
                return
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
        if self.screen.id == "confirm":
            self.pop_screen()
        if self.current_run_id is not None:
            run_id = self.current_run_id
            self.notify(f"Stopping run {run_id} …")
            self.run_worker(
                self._exit_after_target_run_exit(run_id),
                name="quit-stop",
                group="quit",
                exclusive=True,
                exit_on_error=False,
            )
            return
        self.exit()

    def cancel_pending_quit(self) -> None:
        """Cancel any pending quit-stop worker; the app owns the 'quit' group name."""
        self.workers.cancel_group(self, "quit")

    async def _exit_after_target_run_exit(self, run_id: str) -> None:
        stopped = await self._target_stop_run(
            run_id,
            interrupt_timeout=2,
            terminate_timeout=2,
        )
        if not stopped:
            self._render_quit_stop_failure(run_id)
            return
        if await self._wait_for_run_id_cleared(run_id):
            self.exit()
        else:
            self._render_quit_stop_failure(run_id)

    async def _wait_for_run_id_cleared(self, run_id: str) -> bool:
        timeout = float(
            getattr(self, "_quit_stop_wait_timeout_seconds", DEFAULT_QUIT_STOP_WAIT_SECONDS)
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout)
        while self.current_run_id == run_id:
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(0.05)
        return True

    def _render_quit_stop_failure(self, run_id: str) -> None:
        message = f"Unable to stop run {run_id} — target unreachable?"
        self._set_error_text(message, style=f"bold {BAD}")
        self.notify(message, severity="error")

    def confirm_quit_without_stop(self) -> None:
        # bug-234 follow-up: quit with an unreachable target leaves the run
        # untouched (no stop RPC). Cancel local monitor workers the same way
        # detach does so exit does not race them into crash noise.
        if self.screen.id == "confirm":
            self.pop_screen()
        self._cancel_monitor_workers()
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
                exit_on_error=False,
            )
            return
        if self.current_run_id is not None:
            self.run_worker(
                self._target_kill_run(self.current_run_id),
                name="kill",
                group="engine-signal",
                exclusive=True,
                exit_on_error=False,
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
        # Post-READY, ignore stale loading-phase events — but READY<->DEGRADED
        # must flow both ways so post-READY health polling works (FR-18).
        if self.phase in {Phase.READY, Phase.DEGRADED} and message.phase not in {
            Phase.ERROR,
            Phase.STOPPED,
            Phase.DEGRADED,
            Phase.READY,
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
        # Dim known-benign noise; otherwise keep the upstream level.
        self._write_log(text, "BENIGN" if display_level_for_line(text) == "BENIGN" else level)

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

    def _write_run_separator(self, run_id: str, config_name: str) -> None:
        """Dim display-only delimiter written before a run's first log line.

        Marks where each launch/reattach begins so consecutive runs never read
        as one concatenated stream (bug-237). Display path only — durable logs
        are written agent-side and never carry TUI chrome (FR-27).
        """
        target = self.target_name or "local"
        self._write_log(f"── run {run_id} · {config_name} · {target} ──", "RULE")

    def _announce_operator_shutdown(self, run_id: str, verb: str) -> None:
        """Close the operator stop/kill loop: a toast plus a display log line.

        Without these the only evidence of a completed stop was the status pill
        flipping (bug-237). Display path only, like the run separator.
        """
        label = "Killed" if verb == "kill" else "Stopped"
        self.notify(f"{label} {run_id}")
        self._write_log(f"── {label.upper()} by operator ──", "RULE")

    def _update_progress(self, text: str) -> None:
        if self.phase in PROGRESS_SUPPRESSED_PHASES and self._active_job_id is None:
            # bug-237: once the run leaves the loading family (READY) or ends
            # (STOPPED/ERROR), a trailing carriage-return record must not
            # resurrect the transient panel _set_phase already cleared. Live
            # background jobs (e.g. a download while READY) keep streaming.
            return
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
            self._write_log(line, display_level_for_line(line))
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
        if lines:
            return "\n".join(lines)
        if self.target_connection_state != "connected":
            return "target unreachable — configs unknown · R reconnect"
        return "No configs yet — press n to create your first deployment · ? help"

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
            if self.target_connection_state != "connected":
                # Offline with nothing cached: say so honestly instead of the
                # first-run "No configs yet" copy, which reads as "your configs
                # were deleted" when the target is merely unreachable (bug-252).
                text.append(
                    "target unreachable — configs unknown · R reconnect",
                    style=WARN,
                )
            else:
                text.append(
                    "No configs yet — press n to create your first deployment · ? help",
                    style=MUTED,
                )
        text.rstrip()
        # Long config names truncate with an ellipsis instead of wrapping
        # mid-word in the narrow sidebar (screenshot-#1 fix).
        text.no_wrap = True
        text.overflow = "ellipsis"
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
        text = Text("Configs", style=f"bold {ACCENT}")
        if self.target_connection_state != "connected":
            # Offline: the live counts are unknown (or stale, if cached), so
            # replace the confident count badges with an honest connection marker
            # rather than a tally that reads as authoritative (bug-252).
            text.append(
                "  target unreachable",
                style=f"bold {WARN} on {WARN_SURFACE}",
            )
            return text
        valid_count = len(self.registry.valid)
        invalid_count = len(self.registry.invalid)
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
            # Any agent error at registry load is a connection-surface problem, never a
            # reason to crash the TUI out of on_mount (bug-233). Route every code through
            # the same connection-error banner + empty-registry sentinel that the
            # version-mismatch / agent-unreachable codes already used.
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
        # Re-render the whole target-backed dashboard (not just chrome) so the
        # Configs card flips to the offline state at once when an RPC surfaces a
        # connection error (bug-253).
        self._refresh_target_backed_views()
        self._set_error_text(
            self._render_target_connection_banner(
                exc.code,
                str(exc),
                details=exc.details,
            ),
            style=f"bold {BAD}",
        )

    async def _target_call(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        await self._ensure_target_client_connected()
        return await self._target_client.call(method, params)

    async def _with_agent_busy(self, verb: str, awaitable: Awaitable[_T]) -> _T | None:
        """Run an agent RPC (or compound coroutine) behind the busy badge.

        Sets the status badge to the pulsing loading state labelled ``verb``
        (e.g. ``loading models…``, ``composing…``) while ``awaitable`` runs, then
        restores the badge to the live phase — on success OR failure. This is the
        shared convention every RPC opener/verb funnels through (Task 3.2).

        Return contract (chosen for 3.2 ergonomics — one uniform shape at every
        call site):
          * success -> the awaited result.
          * ``TargetCallError`` -> renders the unified remediation banner via
            ``_mark_target_connection_error`` and returns ``None`` so the caller
            aborts opening a screen with a plain ``if result is None: return``.
            ``_target_call`` never resolves to ``None``, so the sentinel is
            unambiguous.
          * any other exception -> propagates unchanged. Workers are
            ``exit_on_error=False`` + labelled since Phase 1, so real bugs surface
            in ``on_worker_state_changed`` rather than being masked here.

        Restore reads the CURRENT ``self.phase`` in the ``finally``: a run-phase
        transition that lands from a worker during the busy window is preserved
        (last-writer-wins). The overlay is transient chrome and never mutates
        ``self.phase``.
        """
        with self._busy_badge(verb):
            try:
                return await awaitable
            except TargetCallError as exc:
                self._mark_target_connection_error(exc)
                return None

    @contextlib.contextmanager
    def _busy_badge(self, verb: str) -> Iterator[None]:
        """Paint the pulsing busy badge for ``verb``, restoring the live phase on exit.

        Exception-safe on both edges (the widget may be missing during teardown).
        Shared by ``_with_agent_busy`` and the manager verbs that keep a bespoke
        banner — build/model removal must preserve the J34 "pinned by …" suffix,
        which the unified connection banner would drop, so they cannot funnel
        through ``_with_agent_busy`` but still want the busy overlay.
        """
        try:
            self._paint_status_badge_busy(verb)
            # Re-fit the header for the (usually wider) verb label so the 1fr
            # model slot re-truncates instead of wrapping the 80-col chrome
            # (_status_label_plain reads the painted label; 4.5 follow-up).
            self._refresh_chrome()
        except WIDGET_MISSING_EXCEPTIONS:
            pass
        try:
            yield
        finally:
            try:
                self._paint_status_badge(self.phase)
                self._refresh_chrome()
            except WIDGET_MISSING_EXCEPTIONS:
                pass

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
        # Capture the state BEFORE _ensure_target_client_connected, which sets it
        # to "connected" on a successful reconnect — so the else-branch can still
        # tell a non-connected -> connected FLIP from a steady-state ping.
        was_connected = self.target_connection_state == "connected"
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
            if not was_connected:
                # Recovery flip: reload the registry (the config set may have
                # changed while the link was down) and re-render the target-backed
                # views so a frozen offline card is replaced, not just chrome
                # (bug-253). _load_registry_from_agent self-guards TargetCallError.
                self.registry = await self._load_registry_from_agent()
                if self.current_config is None and self.registry.valid:
                    self.current_config = self.registry.valid[0].config
                    await self._refresh_selected_config_preview()
                self._refresh_target_backed_views()
                # An open Target Manager tracks the auto-recovery too (bug-257).
                self._refresh_open_target_manager()
            else:
                self._refresh_chrome()

    async def _mark_target_disconnected(self, detail: str) -> None:
        self.target_connection_state = "disconnected"
        self.target_connection_detail = detail
        # Re-render the whole target-backed dashboard (not just chrome) so the
        # Configs card flips to the offline state at once on a mid-session drop
        # (bug-253).
        self._refresh_target_backed_views()
        # An open Target Manager must render the drop truthfully too (bug-257).
        self._refresh_open_target_manager()
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
        result_run_id = result.get("run_id")
        if result_run_id is None:
            self._set_error_text(
                f"Unable to reattach {run_id}: malformed agent payload (missing run_id)"
            )
            return
        self.reattached_run_id = str(result_run_id)
        self.current_run_id = None
        self._write_run_separator(self.reattached_run_id, config_name)
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
            exit_on_error=False,
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
    ) -> bool:
        # Record intent BEFORE signalling (the sidecar idiom) so the monitor's
        # terminal STOPPED can name the verb even if `wait` resolves first.
        self._operator_signal_verbs[run_id] = "stop"
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
            self._operator_signal_verbs.pop(run_id, None)
            self._set_error_text(f"Unable to stop {run_id}: {exc}")
            return False
        return True

    async def _target_kill_run(self, run_id: str) -> None:
        self._operator_signal_verbs[run_id] = "kill"
        try:
            await self._target_call("kill", {"run_id": run_id})
        except Exception as exc:
            self._operator_signal_verbs.pop(run_id, None)
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

    def _refresh_chrome(self, width: int | None = None) -> None:
        if width is None:
            width = self.size.width
        try:
            target_widget = self.query_one("#target-segment", Static)
            model_widget = self.query_one("#active-model", Static)
            url_widget = self.query_one("#server-url", Static)
            clock_widget = self.query_one("#chrome-clock", Static)
        except WIDGET_MISSING_EXCEPTIONS:
            return
        # Auto-width siblings (target, URL, clock) fix the columns the 1fr model
        # slot has left, so render them first, then size the model text to the
        # exact leftover — that way it ellipsizes instead of hard-clipping and
        # the badge is never shoved off the right edge (bug-237).
        target_text = self._render_target_segment()
        target_widget.update(target_text)
        url_full = self._chrome_url_plain()
        url_shown = bool(url_widget.display and url_full)
        clock_shown = bool(clock_widget.display)
        url_widget.update(self._render_chrome_url(url_full))
        clock_widget.update(datetime.now().strftime("%H:%M:%S") if clock_shown else "")
        model_budget = self._active_model_budget(
            width,
            target_cells=cell_len(target_text.plain),
            url_cells=cell_len(url_full) if url_shown else 0,
            clock_cells=8 if clock_shown else 0,
        )
        model_widget.update(truncate_cells(self._render_active_model(), model_budget))
        # The footer advertises state-dependent actions (run controls, Reconnect)
        # and re-fits to width, so refresh it wherever the chrome refreshes: phase
        # changes, connection changes, and every resize (bug-237).
        self._refresh_footer(width)

    def _active_model_budget(
        self,
        width: int,
        *,
        target_cells: int,
        url_cells: int,
        clock_cells: int,
    ) -> int:
        """Columns the 1fr ``#active-model`` slot is left with after the
        auto-width siblings claim theirs (bug-237).

        Mirrors the header box model exactly so the model text can be truncated
        to fit: ``#terminal-shell`` border (1 each side) + ``#top-chrome``
        padding ``0 2``, then the auto widths of title, target, badge, URL and
        clock, plus one ``margin-right: 2`` gap per segment that carries one.
        """
        inner = width - 2 - (2 * 2)
        badge_cells = HEADER_BADGE_CHROME_CELLS + cell_len(self._status_label_plain())
        # margin-right: 2 sits on title, target, model, badge, and — when shown —
        # the URL (the clock is the last segment and carries none).
        gap_segments = 4 + (1 if url_cells else 0)
        gaps = HEADER_SEGMENT_GAP * gap_segments
        used = (
            cell_len("Vela")
            + target_cells
            + badge_cells
            + url_cells
            + clock_cells
            + gaps
        )
        return max(0, inner - used)

    def _status_label_plain(self) -> str:
        """Plain text of the badge label as currently painted — the phase name
        or the live agent-busy verb — so budgeting reserves the real box width."""
        try:
            content = self.query_one("#status-label", Static).content
        except WIDGET_MISSING_EXCEPTIONS:
            return self.phase.value
        return content.plain if isinstance(content, Text) else str(content)

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
        self,
        key: str | None = None,
        detail: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> str:
        remediation = remediation_for_error(
            key or self.target_connection_state,
            target_name=self.target_name,
            details=details,
        )
        if remediation is None:
            banner_kind, cause, suggestion = self._target_connection_banner_parts(
                key or self.target_connection_state
            )
            extra_lines: tuple[str, ...] = ()
        else:
            banner_kind = remediation.label
            cause = remediation.cause
            suggestion = remediation.fix
            extra_lines = remediation.extra_lines
        lines = [
            f"{banner_kind}: {cause}",
            f"target: {self.target_name}",
        ]
        detail_text = detail if detail is not None else self.target_connection_detail
        if detail_text:
            lines.append(detail_text)
        lines.extend(line for line in extra_lines if line not in lines)
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
            return "build: — · model: —"
        return (
            f"build: {self._render_active_build_segment()}"
            f" · model: {self._render_active_model_segment()}"
        )

    def _render_active_build_segment(self) -> str:
        assert self.current_config is not None
        metadata = self.selected_config_metadata
        label = (
            _optional_str(metadata.get("build_label"))
            or _optional_str(self.current_config.command.build)
            or _optional_str(metadata.get("vllm_version"))
            or "unmanaged"
        )
        marker = "📌" if self.current_config.command.build is not None else ""
        return f"{marker}{label} {self._build_status_dot(metadata)}"

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
        return f"{marker}{label} {self._model_status_dot(metadata)}{suffix}"

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

    def _chrome_url_plain(self) -> str:
        if self.ready_url:
            return self.ready_url
        if self.current_config is None:
            return ""
        return self._server_url(self.current_config)

    def _render_chrome_url(self, url: str | None = None) -> Text:
        if url is None:
            url = self._chrome_url_plain()
        if not url:
            return Text("")
        if self.phase in {Phase.READY, Phase.DEGRADED}:
            # Only a server that is actually serving gets the live colour.
            style = self._status_style_for_phase(self.phase)
        else:
            # Honest chrome (bug-237): a configured URL that is not live reads
            # dim, so IDLE/STOPPED never advertise a server that isn't there.
            style = MUTED
        return Text(url, style=style)

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
        # bug-237: in narrow/compact mode the sidebar is hidden, so this overlay
        # stands in for its Configs card. Render the SAME config content the wide
        # sidebar shows (titled "Config") plus a glanceable phase/status line —
        # no meta spec-note about the overlay itself.
        text = Text("Config", style=f"bold {ACCENT}")
        text.append("  ")
        status_style = self._status_style_for_phase(self.phase)
        text.append(self._status_icon_for_phase(self.phase), style=status_style)
        text.append(f" {self.phase.value}", style=status_style)
        if self.ready_url:
            text.append(f"  {self.ready_url}", style=GOOD)
        elif self.current_config is not None:
            text.append(f"  {self._server_url(self.current_config)}", style=GOOD)
        text.append("\n")
        text.append(self._render_config_summary())
        text.no_wrap = True
        text.overflow = "ellipsis"
        return text

    def _has_active_run(self) -> bool:
        """A launched-or-reattached server is under this dashboard's control."""
        return self._attached_run_is_alive() or self._has_reattached_run()

    def _footer_droppable_hints(self) -> list[tuple[str, str]]:
        """Ordered, state-filtered footer hints, highest priority first.

        Excludes the always-on ``? Help`` / ``q Quit`` (the packer pins those and
        never drops them). This governs only what the footer ADVERTISES; the
        BINDINGS stay unconditional, so every key still works when hidden and the
        Help screen remains the full reference (bug-237).
        """
        hints: list[tuple[str, str]] = []
        # Reconnect only when the target link is not healthily connected — a new
        # user at a healthy IDLE never sees an inert `R Reconnect`.
        if self.target_connection_state != "connected":
            hints.append(("R", "Reconnect"))
        if self._has_active_run():
            # A live/attached run: the control keys and the log-navigation keys
            # apply and return to the footer.
            hints += [
                ("s", "Stop"),
                ("K", "Kill"),
                ("r", "Restart"),
                ("/", "Search"),
                ("f", "Filter"),
                ("p", "Pause"),
                ("w", "Wrap"),
                ("g/G", "Top/Bottom"),
            ]
        else:
            # No run yet: Load is the primary action; Stop/Kill/Restart are inert.
            hints.append(("l", "Load"))
        # Navigation / global actions apply in every dashboard state. `n New` leads
        # — it is the front door for a new deployment (J11). `F Flags` stays here:
        # it edits the SELECTED config's vLLM flags, available at IDLE just like
        # `c Configs`, so it belongs in the dashboard IDLE set.
        hints += [
            ("n", "New"),
            ("c", "Configs"),
            ("t", "Targets"),
            ("b", "Builds"),
            ("m", "Models"),
            ("F", "Flags"),
            ("Tab", "Focus"),
            ("^P", "Palette"),
        ]
        return hints

    @staticmethod
    def _footer_hint_cells(hint: tuple[str, str]) -> int:
        key, label = hint
        return cell_len(f"{key} {label}")

    @classmethod
    def _pack_footer_rows(
        cls, hints: list[tuple[str, str]], usable: int
    ) -> list[list[tuple[str, str]]]:
        """Greedy first-fit of ``hints`` into rows no wider than ``usable`` cells."""
        rows: list[list[tuple[str, str]]] = [[]]
        used = 0
        for hint in hints:
            cells = cls._footer_hint_cells(hint)
            addition = cells if not rows[-1] else FOOTER_HINT_GAP + cells
            if rows[-1] and used + addition > usable:
                rows.append([hint])
                used = cells
            else:
                rows[-1].append(hint)
                used += addition
        return rows

    def _footer_rows_for_width(self, width: int) -> list[list[tuple[str, str]]]:
        """State- and width-fitted footer rows.

        Packs the applicable hints plus the pinned ``? Help`` / ``q Quit`` into at
        most ``FOOTER_MAX_ROWS`` rows; when they overflow, the lowest-priority
        DROPPABLE hint is shed from the tail and the pack retried — the protected
        pair is never removed, so Help and Quit survive every width (bug-237).
        """
        usable = max(1, width - FOOTER_CHROME_CELLS)
        protected = list(FOOTER_PROTECTED_HINTS)
        droppable = self._footer_droppable_hints()
        while True:
            rows = self._pack_footer_rows(droppable + protected, usable)
            if len(rows) <= FOOTER_MAX_ROWS or not droppable:
                return rows
            droppable = droppable[:-1]

    def _render_footer_bindings(self, width: int | None = None) -> Text:
        if width is None:
            width = self.size.width or 80
        rows = self._footer_rows_for_width(width)
        text = Text(no_wrap=True, overflow="ellipsis")
        for row_index, row in enumerate(rows):
            if row_index:
                text.append("\n")
            for hint_index, (key, label) in enumerate(row):
                if hint_index:
                    text.append(" " * FOOTER_HINT_GAP)
                text.append(key, style=f"bold {ACCENT}")
                text.append(f" {label}", style=MUTED)
        return text

    def _refresh_footer(self, width: int | None = None) -> None:
        try:
            footer = self.query_one("#footer-bindings", Static)
        except WIDGET_MISSING_EXCEPTIONS:
            return
        footer.update(self._render_footer_bindings(width))

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

    def _apply_responsive_layout(self, width: int, height: int | None = None) -> None:
        if height is None:
            height = self.size.height
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
            gpu_panel = self.query_one("#gpu-panel")
            gpu = self.query_one("#gpu")
            log = self.query_one("#log", RichLog)
            server_url = self.query_one("#server-url", Static)
            clock = self.query_one("#chrome-clock", Static)
        except WIDGET_MISSING_EXCEPTIONS:
            return
        sidebar.display = self.responsive_mode == "wide"
        sidebar_overlay.display = self.responsive_mode != "wide"
        # The GPU monitor is the lowest-priority sidebar card: it sheds first when
        # the WIDTH collapses to compact and, now, when the HEIGHT can no longer
        # hold every card (bug-237) — the config and phase cards keep their rows.
        # Toggle the whole bordered card (frees its rows) and the inner readout
        # together so neither an empty border nor a stray readout is left behind.
        gpu_visible = self.responsive_mode != "compact" and height >= SIDEBAR_GPU_MIN_HEIGHT
        gpu_panel.display = gpu_visible
        gpu.display = gpu_visible
        log.display = True
        # Top chrome collapses right-to-left: the two lowest-priority segments
        # (server URL, then clock) only appear once the terminal is wide enough
        # that revealing them will not starve the badge/model slots (bug-237).
        server_url.display = width >= HEADER_URL_MIN_WIDTH
        clock.display = width >= HEADER_CLOCK_MIN_WIDTH
        if self.responsive_mode != previous_mode:
            self._debug_event(
                "layout.responsive",
                width=width,
                mode=self.responsive_mode,
            )
        # Re-fit the header on every resize, not just on mode changes: 100/120/140
        # are all "wide" yet need different model budgets and URL/clock reveals.
        self._refresh_chrome(width)

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
        self._write_run_separator(run_id, cfg.name)
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
            terminal_phase = await self._await_target_exit_event_phase(events_task)
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
                self._announce_operator_shutdown(
                    run_id, self._operator_signal_verbs.pop(run_id, "stop")
                )
            self._set_phase(resolved_phase)
            return
        self._set_error_text("Agent wait result did not include a terminal phase")
        self._set_phase(self.fsm.phase)

    async def _await_target_exit_event_phase(
        self, events_task: asyncio.Task[Phase | None]
    ) -> Phase | None:
        timeout = float(
            getattr(self, "_target_exit_event_drain_timeout_seconds", 2.0)
        )
        if timeout <= 0:
            return events_task.result() if events_task.done() else None
        try:
            return await asyncio.wait_for(events_task, timeout=timeout)
        except asyncio.TimeoutError:
            return None

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
        failure_list = [
            item
            for item in (failures if isinstance(failures, list) else [])
            if isinstance(item, dict)
        ]
        failure = failure_list[0] if failure_list else {}
        kind = _error_kind_from_agent_payload(failure.get("kind"))
        # The whole checklist, not just the first failure (J29).
        lines = [
            f"✗ {str(item.get('kind') or 'CHECK')}: "
            f"{str(item.get('detail') or 'failed')}"
            for item in failure_list
        ]
        detail = "\n".join(lines) if lines else "Launch preflight failed"
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
            self._paint_status_badge(phase)
            self.query_one("#phases", Static).update(timeline)
        except WIDGET_MISSING_EXCEPTIONS:
            return
        self._refresh_sidebar_overlay()
        self._refresh_chrome()

    def _paint_status_badge(self, phase: Phase) -> None:
        """Paint the status badge (classes + dot + label) for ``phase``.

        Extracted from ``_set_phase`` so the agent-busy overlay can restore the
        badge to the live phase without re-running phase-time bookkeeping. Lets
        ``WIDGET_MISSING_EXCEPTIONS`` propagate; callers wrap it in their own
        guard (``_set_phase`` and ``_with_agent_busy`` both do).
        """
        status_badge = self.query_one("#status-badge")
        self._apply_status_classes(status_badge, phase)
        self.query_one("#status-dot", Static).update(self._render_status_dot(phase))
        self.query_one("#status-label", Static).update(self._render_status_label(phase))

    def _paint_status_badge_busy(self, verb: str) -> None:
        """Overlay the pulsing loading badge with an in-flight agent verb.

        Reuses the loading-phase chrome (amber ``status--loading`` + pulse, dot,
        surface) and only swaps the label for ``verb`` so a busy RPC reads like a
        loading state while naming what is running (e.g. ``loading models…``).
        """
        status_badge = self.query_one("#status-badge")
        self._apply_status_classes(status_badge, BUSY_BADGE_PHASE)
        self.query_one("#status-dot", Static).update(
            self._render_status_dot(BUSY_BADGE_PHASE)
        )
        style = self._status_style_for_phase(BUSY_BADGE_PHASE)
        surface = self._status_surface_for_phase(BUSY_BADGE_PHASE)
        self.query_one("#status-label", Static).update(
            Text(verb, style=f"{style} on {surface}")
        )

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
            label = phase.value
            if (
                state == "terminal"
                and phase is Phase.ERROR
                and self.fsm.error_kind is ErrorKind.CRASHED
            ):
                label = "CRASHED"
            text.append("\n")
            text.append(marker, style=f"bold {style}")
            # Terminal rows carry their own colour (dim/red) end to end so a
            # dead run never renders with the live bold-text treatment.
            label_style = style if state in {"upcoming", "terminal"} else f"bold {TEXT}"
            text.append(f" {label}", style=label_style)
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
        if self.phase is Phase.DEGRADED:
            elapsed = self._format_duration(self._elapsed_for(self.phase))
            rows.append(("●", self.phase, elapsed, "current"))
        elif self.phase in {Phase.STOPPED, Phase.ERROR}:
            # Terminal marker row (bug-237): the run has ENDED, so the timeline
            # must not read as a live "current" state — ■ STOPPED dim,
            # ✗ ERROR/CRASHED red — while the READY history above is kept.
            elapsed = self._format_duration(self._elapsed_for(self.phase))
            marker = "■" if self.phase is Phase.STOPPED else "✗"
            rows.append((marker, self.phase, elapsed, "terminal"))
        return rows

    @staticmethod
    def _phase_timeline_style(phase: Phase, state: str) -> str:
        if state == "complete":
            return GOOD
        if state == "upcoming":
            return MUTED
        if state == "terminal":
            return BAD if phase is Phase.ERROR else MUTED
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
        self._operator_signal_verbs.clear()
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


def _target_bootstrap_command(target: TargetConfig) -> str:
    parts = ["vela", "targets", "bootstrap", target.name]
    if target.host:
        parts.extend(["--host", target.host])
    if target.ssh_key is not None:
        parts.extend(["--ssh-key", str(target.ssh_key)])
    parts.append("--install")
    return " ".join(shlex.quote(part) for part in parts)


def _build_ref_from_build_result(
    result: dict[str, Any],
    params: dict[str, Any],
) -> str | None:
    return (
        _optional_str(result.get("label"))
        or _optional_str(params.get("label"))
        or _optional_str(result.get("build_id"))
        or _optional_str(params.get("build_id"))
    )


def _model_ref_from_model_entry(
    entry: dict[str, Any],
    params: dict[str, Any],
) -> str | None:
    return (
        _optional_str(entry.get("entry_id"))
        or _optional_str(entry.get("display_name"))
        or _optional_str(params.get("entry_id"))
        or _optional_str(params.get("display_name"))
    )


def _model_launch_arg_from_model_entry(
    entry: dict[str, Any],
    params: dict[str, Any],
) -> str | None:
    for source in (entry, params):
        for field in ("repo_id", "local_path", "url", "display_name"):
            value = _optional_str(source.get(field))
            if value is not None:
                return value
    return None


def _model_revision_from_model_entry(
    entry: dict[str, Any],
    params: dict[str, Any],
) -> str | None:
    return (
        _optional_str(entry.get("commit_sha"))
        or _optional_str(entry.get("revision"))
        or _optional_str(params.get("commit_sha"))
        or _optional_str(params.get("revision"))
    )


def _format_validation_errors(payload: dict[str, Any]) -> str:
    errors = payload.get("errors")
    if not isinstance(errors, list) or not errors:
        return "Deployment config did not validate"
    lines = ["Deployment config did not validate:"]
    for item in errors:
        if isinstance(item, dict):
            field = str(item.get("field") or "config")
            message = str(item.get("message") or item)
            lines.append(f"{field}: {message}")
        else:
            lines.append(str(item))
    return "\n".join(lines)


def _warning_texts(warnings: object) -> list[str]:
    if not isinstance(warnings, list):
        return []
    rendered: list[str] = []
    for warning in warnings:
        if isinstance(warning, dict):
            detail = warning.get("detail") or warning.get("message") or warning.get("kind")
            if detail:
                rendered.append(str(detail))
                continue
        rendered.append(str(warning))
    return rendered


def _draft_config_with_flag_updates(
    config: dict[str, Any], selection: dict[str, Any]
) -> dict[str, Any]:
    payload = ModelConfig.model_validate(config).model_dump(mode="python")
    engine_updates = selection.get("engine")
    if isinstance(engine_updates, dict):
        engine = dict(payload.get("engine") or {})
        for key, value in engine_updates.items():
            if value is None or value == "":
                engine.pop(str(key), None)
            else:
                engine[str(key)] = value
        payload["engine"] = engine
    extra_args = selection.get("extra_args")
    if isinstance(extra_args, list):
        payload["extra_args"] = [str(item) for item in extra_args]
    return ModelConfig.model_validate(payload).model_dump(mode="json", exclude_none=True)


def _smoke_stop_timeout_seconds(cfg: ModelConfig) -> float:
    if cfg.command.runtime.value == "docker" and cfg.command.docker is not None:
        return max(10.0, float(cfg.command.docker.stop_grace_seconds) + 10.0)
    return 10.0


def _preview_metadata(preview: dict[str, Any]) -> dict[str, Any]:
    metadata = preview.get("metadata")
    return dict(metadata) if isinstance(metadata, dict) else {}


def _target_seen_timestamp(payload: dict[str, Any]) -> str:
    ts = payload.get("ts")
    if isinstance(ts, str) and ts:
        return ts
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
