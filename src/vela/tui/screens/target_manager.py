from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text
from textual.app import ComposeResult
from textual.screen import ModalScreen
from textual.widgets import Static

from vela.config.targets import TargetConfig, TargetsRegistry
from vela.tui.theme import (
    AMBER,
    BG_BASE,
    BG_PANEL,
    BORDER_STRONG,
    CYAN,
    GREEN,
    RED,
    TEXT_FAINT,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)
from vela.tui.widgets import KeyHintBar, MasterDetail, summarize_capabilities

_FOOTER_HINTS = [
    ("↑↓", "Select"),
    ("⏎", "Switch"),
    ("n", "New"),
    ("e", "Edit"),
    ("b", "Bootstrap"),
    ("p", "Push"),
    ("R", "Reconnect"),
    ("x", "Remove"),
    ("Esc", "Close"),
]


@dataclass(frozen=True)
class TargetManagerRequest:
    action: str
    target_name: str | None = None


class TargetManagerScreen(ModalScreen):
    CSS = f"""
    TargetManagerScreen {{
        align: center middle;
        background: {BG_BASE};
    }}

    #target-manager-panel {{
        width: 100;
        max-height: 90%;
        overflow-y: auto;
        border: round {BORDER_STRONG};
        background: {BG_PANEL};
        padding: 1 2;
    }}

    #target-manager-list {{
        width: 40;
        height: auto;
        color: {TEXT_PRIMARY};
    }}

    #target-manager-detail {{
        width: 1fr;
        height: auto;
        color: {TEXT_PRIMARY};
    }}

    #target-manager-footer {{ margin-top: 1; }}
    """

    BINDINGS = [
        ("up", "previous", "Previous"),
        ("down", "next", "Next"),
        ("enter", "accept", "Select"),
        ("n", "new", "New"),
        ("e", "edit", "Edit"),
        ("b", "bootstrap", "Bootstrap"),
        ("p", "push_config", "Push config"),
        ("R", "reconnect", "Reconnect"),
        ("x", "remove", "Remove"),
        ("escape", "cancel", "Cancel"),
    ]

    def __init__(
        self,
        registry: TargetsRegistry,
        *,
        active_target: str,
        connection_state: str,
        connection_detail: str = "",
        agent_info: dict[str, object] | None = None,
        last_seen: str | None = None,
        active_runs: list[dict[str, object]] | None = None,
        gpu_summary: str | None = None,
    ) -> None:
        super().__init__(id="target-manager")
        self.targets = registry.targets
        self.active_target = active_target
        self.connection_state = connection_state
        self.connection_detail = connection_detail
        self.agent_info = dict(agent_info or {})
        self.last_seen = last_seen
        self.active_runs = [dict(run) for run in active_runs or []]
        self.gpu_summary = gpu_summary
        self.selected_index = self._active_index()

    def compose(self) -> ComposeResult:
        yield MasterDetail(
            Static(id="target-manager-list"),
            Static(id="target-manager-detail"),
            footer=KeyHintBar(_FOOTER_HINTS, id="target-manager-footer"),
            id="target-manager-panel",
        )

    def on_mount(self) -> None:
        self._refresh()

    def action_previous(self) -> None:
        if self.targets:
            self.selected_index = max(0, self.selected_index - 1)
            self._refresh()

    def action_next(self) -> None:
        if self.targets:
            self.selected_index = min(len(self.targets) - 1, self.selected_index + 1)
            self._refresh()

    def action_accept(self) -> None:
        target = self._selected_target()
        if target is None:
            self.dismiss(None)
            return
        self.dismiss(target.name)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_reconnect(self) -> None:
        self.app.action_reconnect()
        self._refresh()

    def action_new(self) -> None:
        self.dismiss(TargetManagerRequest("new"))

    def action_edit(self) -> None:
        target = self._selected_target()
        if target is None:
            self.dismiss(None)
            return
        self.dismiss(TargetManagerRequest("edit", target.name))

    def action_bootstrap(self) -> None:
        target = self._selected_target()
        if target is None:
            self.dismiss(None)
            return
        self.dismiss(TargetManagerRequest("bootstrap", target.name))

    def action_push_config(self) -> None:
        target = self._selected_target()
        if target is None:
            self.dismiss(None)
            return
        self.dismiss(TargetManagerRequest("push_config", target.name))

    def action_remove(self) -> None:
        target = self._selected_target()
        if target is None:
            self.dismiss(None)
            return
        self.dismiss(TargetManagerRequest("remove", target.name))

    def _refresh(self) -> None:
        self.query_one("#target-manager-list", Static).update(self._render_list())
        self.query_one("#target-manager-detail", Static).update(self._render_detail())

    def _render_list(self) -> Text:
        text = Text()
        text.append("Target Manager\n", style=f"bold {CYAN}")
        if not self.targets:
            text.append("\nNo targets configured", style=TEXT_FAINT)
            return text
        text.append("\n")
        for index, target in enumerate(self.targets):
            selected = index == self.selected_index
            if target.name == self.active_target:
                dot = _connection_dot(self.connection_state)
                dot_color = _connection_color(self.connection_state)
            else:
                dot = "○"
                dot_color = TEXT_FAINT
            host = target.host or "-"
            marker = ">" if selected else " "
            text.append(f"{marker} ", style=CYAN if selected else TEXT_FAINT)
            text.append(f"{dot} ", style=dot_color)
            text.append(
                target.name,
                style=f"bold {TEXT_PRIMARY}" if selected else TEXT_SECONDARY,
            )
            text.append(f"  {target.transport.value}  {host}\n", style=TEXT_FAINT)
        return text

    def _render_detail(self) -> Text:
        target = self._selected_target()
        if target is None:
            return Text("No target selected", style=TEXT_FAINT)
        active = target.name == self.active_target
        state = self.connection_state if active else "inactive"
        text = Text()
        # Header: target name + connection state.
        text.append(target.name, style=f"bold {TEXT_PRIMARY}")
        text.append("  ")
        text.append(f"{_connection_dot(state)} {state}", style=_connection_color(state))
        text.append("\n")
        # CONNECTION.
        self._section(text, "CONNECTION")
        self._kv(text, "connection", state)
        if active and self.connection_detail:
            self._kv(text, "detail", self.connection_detail)
        self._kv(text, "transport", target.transport.value)
        self._kv(text, "host", target.host or "-")
        if active and self.last_seen:
            self._kv(text, "last_seen", self.last_seen)
        # VERSIONS.
        if active:
            versions = _agent_version_rows(self.agent_info)
            if versions:
                self._section(text, "VERSIONS")
                for key, value in versions:
                    self._kv(text, key, value)
        # PATHS.
        self._section(text, "PATHS")
        self._kv(text, "workdir", _path_or_dash(target.workdir))
        self._kv(text, "venv", _path_or_dash(target.venv))
        # CAPABILITIES (collapse the wall once it grows past the limit).
        if active:
            capabilities = _agent_capabilities(self.agent_info.get("capabilities"))
            if capabilities:
                self._section(text, "CAPABILITIES")
                self._kv(text, "capabilities", summarize_capabilities(capabilities, limit=8))
        # RUNTIME.
        if active:
            self._section(text, "RUNTIME")
            self._kv(text, "active_runs", _active_runs_value(self.active_runs))
            gpu = _gpu_summary_value(self.gpu_summary)
            if gpu is not None:
                self._kv(text, "gpu", gpu)
        return text

    def _section(self, text: Text, title: str) -> None:
        text.append("\n")
        text.append(f"{title}\n", style=f"bold {TEXT_SECONDARY}")

    def _kv(self, text: Text, key: str, value: str) -> None:
        text.append(f"  {key}: ", style=TEXT_FAINT)
        text.append(f"{value}\n", style=TEXT_PRIMARY)

    def _selected_target(self) -> TargetConfig | None:
        if not self.targets:
            return None
        return self.targets[self.selected_index]

    def _active_index(self) -> int:
        for index, target in enumerate(self.targets):
            if target.name == self.active_target:
                return index
        return 0


def _connection_dot(state: str) -> str:
    return {
        "connected": "●",
        "connecting": "◐",
        "reconnecting": "◐",
        "disconnected": "○",
        "version-mismatch": "▲",
        "unreachable": "✕",
    }.get(state, "○")


def _connection_color(state: str) -> str:
    return {
        "connected": GREEN,
        "connecting": AMBER,
        "reconnecting": AMBER,
        "version-mismatch": AMBER,
        "unreachable": RED,
    }.get(state, TEXT_FAINT)


def _path_or_dash(value: object | None) -> str:
    return str(value) if value is not None else "-"


def _agent_version_rows(agent_info: dict[str, object]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    agent_version = _optional_agent_str(agent_info.get("agent_version"))
    controller_version = _optional_agent_str(agent_info.get("controller_version"))
    protocol_version = _optional_agent_str(
        agent_info.get("agent_protocol_version") or agent_info.get("protocol_version")
    )
    if agent_version is not None:
        rows.append(("agent", agent_version))
    if controller_version is not None:
        rows.append(("controller", controller_version))
    if protocol_version is not None:
        rows.append(("protocol", protocol_version))
    return rows


def _agent_capabilities(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted(str(item) for item in value if isinstance(item, str) and item)


def _active_runs_value(active_runs: list[dict[str, object]]) -> str:
    labels = [
        label
        for label in (
            _optional_agent_str(run.get("config_name"))
            or _optional_agent_str(run.get("run_id"))
            for run in active_runs
        )
        if label is not None
    ]
    if not labels:
        return str(len(active_runs))
    visible = labels[:3]
    if len(labels) > len(visible):
        visible.append(f"+{len(labels) - len(visible)}")
    return f"{len(active_runs)} ({', '.join(visible)})"


def _gpu_summary_value(value: str | None) -> str | None:
    if value is None:
        return None
    for line in value.splitlines():
        text = line.strip()
        if text:
            return text
    return None


def _optional_agent_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None
