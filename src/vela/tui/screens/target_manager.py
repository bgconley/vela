from __future__ import annotations

from dataclasses import dataclass

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

from vela.config.targets import TargetConfig, TargetsRegistry
from vela.tui.theme import ACCENT, SURFACE_ALT, TEXT


@dataclass(frozen=True)
class TargetManagerRequest:
    action: str
    target_name: str | None = None


class TargetManagerScreen(ModalScreen):
    CSS = f"""
    TargetManagerScreen {{
        align: center middle;
        background: #091015;
    }}

    #target-manager-panel {{
        width: 84;
        max-height: 32;
        border: solid {ACCENT};
        background: {SURFACE_ALT};
        padding: 1 2;
    }}

    #target-manager-list {{
        height: auto;
        max-height: 14;
        color: {TEXT};
    }}

    #target-manager-detail {{
        margin-top: 1;
        color: {TEXT};
    }}
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
        with Vertical(id="target-manager-panel"):
            yield Static("", id="target-manager-list")
            yield Static("", id="target-manager-detail")

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

    def _render_list(self) -> str:
        lines = ["Target Manager", ""]
        if not self.targets:
            lines.append("No targets configured")
            return "\n".join(lines)
        for index, target in enumerate(self.targets):
            marker = ">" if index == self.selected_index else " "
            dot = (
                _connection_dot(self.connection_state)
                if target.name == self.active_target
                else "○"
            )
            host = target.host or "-"
            lines.append(
                f"{marker} {dot} {target.name}  {target.transport.value}  {host}"
            )
        return "\n".join(lines)

    def _render_detail(self) -> str:
        target = self._selected_target()
        if target is None:
            return "No target selected"
        lines = [
            f"name: {target.name}",
            f"transport: {target.transport.value}",
            f"host: {target.host or '-'}",
            f"workdir: {_path_or_dash(target.workdir)}",
            f"venv: {_path_or_dash(target.venv)}",
        ]
        if target.name == self.active_target:
            lines.append(f"connection: {self.connection_state}")
            if self.connection_detail:
                lines.append(f"detail: {self.connection_detail}")
            lines.extend(_agent_detail_lines(self.agent_info, self.last_seen))
            lines.extend(_runtime_detail_lines(self.active_runs, self.gpu_summary))
        else:
            lines.append("connection: inactive")
        lines.append("actions: Enter switch | B bootstrap | P push config | E edit")
        return "\n".join(lines)

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


def _path_or_dash(value: object | None) -> str:
    return str(value) if value is not None else "-"


def _agent_detail_lines(agent_info: dict[str, object], last_seen: str | None) -> list[str]:
    lines: list[str] = []
    agent_version = _optional_agent_str(agent_info.get("agent_version"))
    controller_version = _optional_agent_str(agent_info.get("controller_version"))
    protocol_version = _optional_agent_str(
        agent_info.get("agent_protocol_version") or agent_info.get("protocol_version")
    )
    capabilities = _agent_capabilities(agent_info.get("capabilities"))
    if agent_version is not None:
        lines.append(f"agent: {agent_version}")
    if controller_version is not None:
        lines.append(f"controller: {controller_version}")
    if protocol_version is not None:
        lines.append(f"protocol: {protocol_version}")
    if capabilities:
        lines.append(f"capabilities: {', '.join(capabilities)}")
    if last_seen:
        lines.append(f"last_seen: {last_seen}")
    return lines


def _agent_capabilities(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted(str(item) for item in value if isinstance(item, str) and item)


def _runtime_detail_lines(
    active_runs: list[dict[str, object]], gpu_summary: str | None
) -> list[str]:
    lines = [_active_runs_line(active_runs)]
    gpu = _gpu_summary_line(gpu_summary)
    if gpu is not None:
        lines.append(f"gpu: {gpu}")
    return lines


def _active_runs_line(active_runs: list[dict[str, object]]) -> str:
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
        return f"active_runs: {len(active_runs)}"
    visible = labels[:3]
    if len(labels) > len(visible):
        visible.append(f"+{len(labels) - len(visible)}")
    return f"active_runs: {len(active_runs)} ({', '.join(visible)})"


def _gpu_summary_line(value: str | None) -> str | None:
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
