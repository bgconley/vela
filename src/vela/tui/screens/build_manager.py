from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.screen import ModalScreen
from textual.widgets import Static

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
)
from vela.tui.widgets import KeyHintBar, MasterDetail


class BuildManagerScreen(ModalScreen):
    CSS = f"""
    BuildManagerScreen {{
        align: center middle;
        background: {BG_BASE};
    }}

    #build-manager-panel {{
        width: 96;
        max-height: 90%;
        overflow-y: auto;
        border: round {BORDER_STRONG};
        background: {BG_PANEL};
        padding: 1 2;
    }}

    #build-manager-list {{ width: 42; height: auto; max-height: 24; color: {TEXT_PRIMARY}; }}
    #build-manager-detail {{ width: 1fr; height: auto; max-height: 24; color: {TEXT_PRIMARY}; }}
    #build-manager-footer {{ margin-top: 1; }}
    """

    BINDINGS = [
        ("up", "previous", "Previous"),
        ("down", "next", "Next"),
        ("enter", "accept", "Select"),
        ("n", "new", "New"),
        ("a", "adopt", "Adopt"),
        ("v", "verify", "Verify"),
        ("P", "pin_config", "Pin to config"),
        ("r", "repair", "Repair"),
        ("F", "flags", "Flags"),
        ("x", "remove", "Remove"),
        ("escape", "cancel", "Cancel"),
    ]

    def __init__(
        self, payload: dict[str, Any], *, focus_build: str | None = None
    ) -> None:
        super().__init__(id="build-manager")
        builds = payload.get("builds", [])
        self.builds = [dict(item) for item in builds if isinstance(item, dict)]
        self.selected_index = self._focus_index(focus_build)

    def compose(self) -> ComposeResult:
        yield MasterDetail(
            Static(id="build-manager-list"),
            Static(id="build-manager-detail"),
            footer=KeyHintBar(
                [
                    ("⏎", "Select"),
                    ("n", "New"),
                    ("a", "Adopt"),
                    ("v", "Verify"),
                    ("r", "Repair"),
                    ("P", "Pin to config"),
                    ("F", "Flags"),
                    ("x", "Remove"),
                    ("Esc", "Close"),
                ],
                id="build-manager-footer",
            ),
            id="build-manager-panel",
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

    def action_pin_config(self) -> None:
        build = self._selected_build()
        if build is None:
            self.dismiss(None)
            return
        self.dismiss(
            {
                "action": "pin_config_build",
                "build": _build_reference(build),
                # Both identifiers so the toggle matches however the config
                # spelled its pin.
                "build_id": str(build.get("build_id") or ""),
                "label": str(build.get("label") or ""),
            }
        )

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

    def _render_list(self) -> Text:
        text = Text()
        text.append("Build Manager\n", style=f"bold {CYAN}")
        if not self.builds:
            text.append(
                "\nNo builds yet — n create · a adopt an existing venv",
                style=TEXT_FAINT,
            )
            return text
        text.append("\n")
        for index, build in enumerate(self.builds):
            selected = index == self.selected_index
            status = str(build.get("status") or "unknown")
            active = "  ● active" if build.get("default") else ""
            in_use = "  🔒 in use" if _live_refs(build) else ""
            config_refs = _config_ref_badge(build)
            text.append(">" if selected else " ", style=CYAN if selected else TEXT_FAINT)
            text.append(" ")
            text.append(f"{_build_status_dot(status)} ", style=_build_status_color(status))
            text.append(
                _build_label(build),
                style=f"bold {TEXT_PRIMARY}" if selected else TEXT_PRIMARY,
            )
            text.append(f"  {status}{active}{in_use}{config_refs}\n", style=TEXT_FAINT)
        text.append(
            "\n⏎ sets the default build — used by configs without a pinned build.\n"
            "Pinned configs and live runs are unaffected.",
            style=TEXT_FAINT,
        )
        return text

    def _render_detail(self) -> Text:
        build = self._selected_build()
        if build is None:
            return Text("No build selected", style=TEXT_FAINT)
        install = _dict_or_empty(build.get("install"))
        resolved = _dict_or_empty(build.get("resolved"))
        paths = _dict_or_empty(build.get("paths"))
        text = Text()
        text.append(f"{_build_label(build)}\n", style=f"bold {TEXT_PRIMARY}")
        text.append("\n")
        rows = [
            ("build_id", str(build.get("build_id") or "-")),
            ("status", str(build.get("status") or "unknown")),
            ("source", _build_source_detail(install)),
            ("in_use", _in_use_detail(build)),
            ("used_by_configs", _config_ref_detail(build)),
            *(
                [("default_for", "all unpinned configs")]
                if build.get("default")
                else []
            ),
            ("vllm", str(resolved.get("vllm") or "-")),
            ("cuda", str(resolved.get("cuda") or "-")),
            ("executable", str(paths.get("executable") or "-")),
        ]
        for key, value in rows:
            text.append(f"{key}: ", style=TEXT_FAINT)
            text.append(f"{value}\n", style=TEXT_PRIMARY)
        return text

    def _selected_build(self) -> dict[str, Any] | None:
        if not self.builds:
            return None
        return self.builds[self.selected_index]

    def _active_index(self) -> int:
        for index, build in enumerate(self.builds):
            if build.get("default"):
                return index
        return 0

    def _focus_index(self, focus_build: str | None) -> int:
        if focus_build:
            for index, build in enumerate(self.builds):
                if focus_build in {
                    str(build.get("build_id") or ""),
                    str(build.get("label") or ""),
                }:
                    return index
        return self._active_index()


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


def _build_status_color(status: str) -> str:
    normalized = status.lower()
    if normalized in {"ready", "ok"}:
        return GREEN
    if normalized in {"creating", "installing", "verifying"}:
        return CYAN
    if normalized in {"broken", "missing", "failed"}:
        return RED
    if normalized in {"drift", "partial"}:
        return AMBER
    return TEXT_FAINT


def _dict_or_empty(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}
