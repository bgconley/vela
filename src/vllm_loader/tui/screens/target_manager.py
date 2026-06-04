from __future__ import annotations

from dataclasses import dataclass

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

from vllm_loader.config.targets import TargetConfig, TargetsRegistry
from vllm_loader.tui.theme import ACCENT, SURFACE_ALT, TEXT


@dataclass(frozen=True)
class TargetManagerRequest:
    action: str
    target_name: str


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
    ) -> None:
        super().__init__(id="target-manager")
        self.targets = registry.targets
        self.active_target = active_target
        self.connection_state = connection_state
        self.connection_detail = connection_detail
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
        else:
            lines.append("connection: inactive")
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
