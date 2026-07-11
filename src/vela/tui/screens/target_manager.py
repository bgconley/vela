from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
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
    MODAL_LIST_CSS,
    MODAL_PANEL_CSS,
    RED,
    TEXT_FAINT,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)
from vela.tui.widgets import KeyHintBar, pack_hint_rows, summarize_capabilities

_FOOTER_HINTS = [
    ("↑↓", "Select"),
    ("⏎", "Switch"),
    ("n", "New"),
    ("e", "Edit"),
    ("b", "Bootstrap"),
    ("p", "Push"),
    ("R", "Reconnect"),
    ("x", "Remove"),
    ("v", "view all"),
    ("Esc", "Close"),
]


@dataclass(frozen=True)
class TargetManagerRequest:
    action: str
    target_name: str | None = None


class TargetManagerScreen(ModalScreen):
    # Full-width STACKED rebuild (bug-237): the shared modal frame (Task 4.1
    # MODAL_PANEL_CSS / MODAL_LIST_CSS) replaces the old fixed `width: 100` box
    # that clipped past 80 cols, and the two-pane MasterDetail is dropped for a
    # full-width list-in-a-VerticalScroll STACKED ABOVE the detail (the Flag
    # Manager precedent). The frame's `max-height: 96%` bounds the panel so
    # arrowing between targets can no longer resize/jump the modal.
    CSS = f"""
    TargetManagerScreen {{
        align: center middle;
        background: {BG_BASE};
    }}

    TargetManagerScreen #target-manager-panel {{
        {MODAL_PANEL_CSS}
        border: round {BORDER_STRONG};
        background: {BG_PANEL};
        padding: 1 2;
    }}

    TargetManagerScreen #target-manager-list-scroll {{
        {MODAL_LIST_CSS}
        max-height: 16;
        margin-bottom: 1;
    }}

    TargetManagerScreen #target-manager-list {{
        width: 1fr;
        height: auto;
        color: {TEXT_PRIMARY};
    }}

    TargetManagerScreen #target-manager-detail {{
        width: 1fr;
        height: auto;
        color: {TEXT_PRIMARY};
    }}

    TargetManagerScreen #target-manager-footer {{
        dock: bottom;
        height: auto;
        margin-top: 1;
        background: {BG_PANEL};
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
        ("v", "view_capabilities", "View all capabilities"),
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
        self._show_all_capabilities = False

    def compose(self) -> ComposeResult:
        with Vertical(id="target-manager-panel"):
            with VerticalScroll(id="target-manager-list-scroll"):
                yield Static(id="target-manager-list")
            yield Static(id="target-manager-detail")
            with Vertical(id="target-manager-footer"):
                for index, row in enumerate(pack_hint_rows(_FOOTER_HINTS)):
                    yield KeyHintBar(row, id=f"target-manager-footer-row-{index}")

    def on_mount(self) -> None:
        # Keep the list scroll out of the Tab order so key bindings (↑↓ etc.)
        # reach the screen instead of scrolling the region (the manager has no
        # focusable inputs to Tab into).
        try:
            self.query_one("#target-manager-list-scroll").can_focus = False
        except Exception:
            pass
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
        # Optimistic feedback (bug-237): render `reconnecting…` in the detail's
        # connection row immediately, BEFORE the app's reconnect worker resolves.
        # The app's reconnect-completion path calls `refresh_target_state` to
        # replace it with the real live state once the worker finishes.
        self.connection_state = "reconnecting"
        self._refresh()
        self.app.action_reconnect()  # type: ignore[attr-defined]

    def refresh_target_state(self, payload: dict[str, object]) -> None:
        """Re-render the detail (and the active target's list dot) from a fresh
        live-state payload while the manager stays open.

        The app calls this when a `Reconnect` worker completes and the manager
        is still the top screen, so a restored link flips the frozen snapshot to
        the honest connected state without closing the modal (bug-237). Only the
        keys present in ``payload`` are applied, mirroring the constructor.
        """
        if "active_target" in payload:
            self.active_target = str(payload["active_target"])
        if "connection_state" in payload:
            self.connection_state = str(payload["connection_state"])
        if "connection_detail" in payload:
            self.connection_detail = str(payload["connection_detail"] or "")
        if "agent_info" in payload:
            info = payload["agent_info"]
            self.agent_info = dict(info) if isinstance(info, dict) else {}
        if "last_seen" in payload:
            seen = payload["last_seen"]
            self.last_seen = str(seen) if seen else None
        if "active_runs" in payload:
            runs = payload["active_runs"]
            self.active_runs = (
                [dict(run) for run in runs if isinstance(run, dict)]
                if isinstance(runs, list)
                else []
            )
        if "gpu_summary" in payload:
            gpu = payload["gpu_summary"]
            self.gpu_summary = str(gpu) if gpu is not None else None
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

    def action_view_capabilities(self) -> None:
        self._show_all_capabilities = not self._show_all_capabilities
        self._refresh()

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
        state_label = _connection_label(state)
        text = Text()
        # Header: target name + connection state.
        text.append(target.name, style=f"bold {TEXT_PRIMARY}")
        text.append("  ")
        text.append(f"{_connection_dot(state)} {state_label}", style=_connection_color(state))
        text.append("\n")
        # CONNECTION.
        self._section(text, "CONNECTION")
        self._kv(text, "connection", state_label)
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
        # CAPABILITIES (collapse the wall once it grows past the limit; the
        # `v` binding toggles the full list).
        if active:
            capabilities = _agent_capabilities(self.agent_info.get("capabilities"))
            if capabilities:
                self._section(text, "CAPABILITIES")
                if self._show_all_capabilities:
                    value = ", ".join(capabilities)
                    if len(capabilities) > 8:
                        value += "  · v collapse"
                    self._kv(text, "capabilities", value)
                else:
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


def _connection_label(state: str) -> str:
    # Transitional states read as in-progress; the trailing ellipsis is the
    # optimistic `reconnecting…` feedback the detail shows the instant `R` is
    # pressed, before the app's reconnect worker resolves (bug-237).
    if state in ("connecting", "reconnecting"):
        return f"{state}…"
    return state


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
