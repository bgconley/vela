from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

from vllm_loader.tui.theme import ACCENT, SURFACE_ALT, TEXT


class BuildManagerScreen(ModalScreen):
    CSS = f"""
    BuildManagerScreen {{
        align: center middle;
        background: #091015;
    }}

    #build-manager-panel {{
        width: 96;
        max-height: 34;
        border: solid {ACCENT};
        background: {SURFACE_ALT};
        padding: 1 2;
    }}

    #build-manager-list {{
        width: 42;
        height: auto;
        max-height: 20;
        color: {TEXT};
    }}

    #build-manager-detail {{
        width: 1fr;
        height: auto;
        max-height: 20;
        color: {TEXT};
    }}

    #build-manager-footer {{
        margin-top: 1;
        color: #8ba4ae;
    }}
    """

    BINDINGS = [
        ("up", "previous", "Previous"),
        ("down", "next", "Next"),
        ("enter", "accept", "Select"),
        ("n", "new", "New"),
        ("a", "adopt", "Adopt"),
        ("v", "verify", "Verify"),
        ("x", "remove", "Remove"),
        ("escape", "cancel", "Cancel"),
    ]

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(id="build-manager")
        builds = payload.get("builds", [])
        self.builds = [dict(item) for item in builds if isinstance(item, dict)]
        self.selected_index = self._active_index()

    def compose(self) -> ComposeResult:
        with Vertical(id="build-manager-panel"):
            with Horizontal():
                yield Static("", id="build-manager-list")
                yield Static("", id="build-manager-detail")
            yield Static(
                "Enter Select   n New   a Adopt   v Verify   x Remove   Esc Close",
                id="build-manager-footer",
            )

    def on_mount(self) -> None:
        self._refresh()

    def action_previous(self) -> None:
        if self.builds:
            self.selected_index = max(0, self.selected_index - 1)
            self._refresh()

    def action_next(self) -> None:
        if self.builds:
            self.selected_index = min(len(self.builds) - 1, self.selected_index + 1)
            self._refresh()

    def action_accept(self) -> None:
        build = self._selected_build()
        if build is None:
            self.dismiss(None)
            return
        self.dismiss(_build_reference(build))

    def action_new(self) -> None:
        self.dismiss({"action": "create_build"})

    def action_adopt(self) -> None:
        self.dismiss({"action": "adopt_build"})

    def action_verify(self) -> None:
        build = self._selected_build()
        if build is None:
            self.dismiss(None)
            return
        self.dismiss(_build_action_payload("verify_build", build))

    def action_remove(self) -> None:
        build = self._selected_build()
        if build is None:
            self.dismiss(None)
            return
        self.dismiss(_build_action_payload("remove_build", build))

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _refresh(self) -> None:
        self.query_one("#build-manager-list", Static).update(self._render_list())
        self.query_one("#build-manager-detail", Static).update(self._render_detail())

    def _render_list(self) -> str:
        lines = ["Build Manager", ""]
        if not self.builds:
            lines.append("No builds found")
            return "\n".join(lines)
        for index, build in enumerate(self.builds):
            marker = ">" if index == self.selected_index else " "
            status = str(build.get("status") or "unknown")
            active = "  ● active" if build.get("default") else ""
            lines.append(
                f"{marker} {_build_status_dot(status)} {_build_label(build)}  {status}{active}"
            )
        return "\n".join(lines)

    def _render_detail(self) -> str:
        build = self._selected_build()
        if build is None:
            return "No build selected"
        resolved = _dict_or_empty(build.get("resolved"))
        paths = _dict_or_empty(build.get("paths"))
        lines = [
            "Detail",
            "",
            f"label: {_build_label(build)}",
            f"build_id: {build.get('build_id') or '-'}",
            f"status: {build.get('status') or 'unknown'}",
            f"vllm: {resolved.get('vllm') or '-'}",
            f"cuda: {resolved.get('cuda') or '-'}",
            f"executable: {paths.get('executable') or '-'}",
        ]
        return "\n".join(lines)

    def _selected_build(self) -> dict[str, Any] | None:
        if not self.builds:
            return None
        return self.builds[self.selected_index]

    def _active_index(self) -> int:
        for index, build in enumerate(self.builds):
            if build.get("default"):
                return index
        return 0


def _build_reference(build: dict[str, Any]) -> str:
    return str(build.get("label") or build.get("build_id") or "")


def _build_label(build: dict[str, Any]) -> str:
    return str(build.get("label") or build.get("build_id") or "unnamed-build")


def _build_action_payload(action: str, build: dict[str, Any]) -> dict[str, Any]:
    return {
        "action": action,
        "build": _build_reference(build),
        "label": _build_label(build),
        "paths": _dict_or_empty(build.get("paths")),
    }


def _build_status_dot(status: str) -> str:
    normalized = status.lower()
    if normalized in {"ready", "ok"}:
        return "●"
    if normalized in {"creating", "installing", "verifying"}:
        return "◐"
    if normalized in {"broken", "missing", "failed"}:
        return "✕"
    if normalized in {"drift", "partial"}:
        return "▲"
    return "▣"


def _dict_or_empty(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}
