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
        ("r", "repair", "Repair"),
        ("F", "flags", "Flags"),
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
                "Enter Select   n New   a Adopt   v Verify   r Repair   "
                "F Flags   x Remove   Esc Close",
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

    def action_repair(self) -> None:
        build = self._selected_build()
        if build is None:
            self.dismiss(None)
            return
        self.dismiss(_build_action_payload("repair_build", build))

    def action_flags(self) -> None:
        self.dismiss({"action": "flags"})

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
            in_use = "  🔒 in use" if _live_refs(build) else ""
            config_refs = _config_ref_badge(build)
            lines.append(
                f"{marker} {_build_status_dot(status)} {_build_label(build)}  "
                f"{status}{active}{in_use}{config_refs}"
            )
        return "\n".join(lines)

    def _render_detail(self) -> str:
        build = self._selected_build()
        if build is None:
            return "No build selected"
        install = _dict_or_empty(build.get("install"))
        resolved = _dict_or_empty(build.get("resolved"))
        paths = _dict_or_empty(build.get("paths"))
        lines = [
            "Detail",
            "",
            f"label: {_build_label(build)}",
            f"build_id: {build.get('build_id') or '-'}",
            f"status: {build.get('status') or 'unknown'}",
            f"source: {_build_source_detail(install)}",
            f"in_use: {_in_use_detail(build)}",
            f"used_by_configs: {_config_ref_detail(build)}",
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


def _live_refs(build: dict[str, Any]) -> list[dict[str, Any]]:
    refs = build.get("live_refs")
    if not isinstance(refs, list):
        return []
    return [dict(item) for item in refs if isinstance(item, dict)]


def _in_use_detail(build: dict[str, Any]) -> str:
    refs = _live_refs(build)
    if not refs:
        return "no"
    labels = [
        str(ref.get("run_id"))
        for ref in refs
        if isinstance(ref.get("run_id"), str) and ref.get("run_id")
    ]
    visible = ", ".join(labels[:3])
    if len(labels) > 3:
        visible = f"{visible}, +{len(labels) - 3}" if visible else f"+{len(labels) - 3}"
    suffix = f" ({visible})" if visible else ""
    noun = "run" if len(refs) == 1 else "runs"
    return f"{len(refs)} live {noun}{suffix}"


def _config_refs(build: dict[str, Any]) -> list[str]:
    refs = build.get("config_refs")
    if not isinstance(refs, list):
        return []
    return [str(ref) for ref in refs if isinstance(ref, str) and ref]


def _config_ref_count(build: dict[str, Any]) -> int:
    refs = _config_refs(build)
    if refs:
        return len(refs)
    try:
        return int(build.get("config_ref_count") or 0)
    except (TypeError, ValueError):
        return 0


def _config_ref_badge(build: dict[str, Any]) -> str:
    count = _config_ref_count(build)
    if count <= 0:
        return ""
    noun = "config" if count == 1 else "configs"
    return f"  ⇩ used by {count} {noun}"


def _config_ref_detail(build: dict[str, Any]) -> str:
    refs = _config_refs(build)
    count = _config_ref_count(build)
    if count <= 0:
        return "no"
    visible = ", ".join(refs[:3])
    if len(refs) > 3:
        visible = f"{visible}, +{len(refs) - 3}" if visible else f"+{len(refs) - 3}"
    suffix = f" ({visible})" if visible else ""
    return f"{count}{suffix}"


def _build_source_detail(install: dict[str, Any]) -> str:
    method = _optional_build_str(install.get("method"))
    source = _optional_build_str(install.get("source"))
    if source is None:
        provenance = _dict_or_empty(install.get("provenance"))
        source = (
            _optional_build_str(provenance.get("nightly_channel"))
            or _optional_build_str(provenance.get("commit"))
            or _optional_build_str(provenance.get("url"))
            or _optional_build_str(provenance.get("path"))
        )
    if method is None and source is None:
        return "-"
    if method is None:
        return source or "-"
    if source is None:
        return method
    return f"{method}/{source}"


def _optional_build_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


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
