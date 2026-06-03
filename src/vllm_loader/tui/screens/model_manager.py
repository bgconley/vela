from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

from vllm_loader.tui.theme import ACCENT, SURFACE_ALT, TEXT


class ModelManagerScreen(ModalScreen):
    CSS = f"""
    ModelManagerScreen {{
        align: center middle;
        background: #091015;
    }}

    #model-manager-panel {{
        width: 104;
        max-height: 34;
        border: solid {ACCENT};
        background: {SURFACE_ALT};
        padding: 1 2;
    }}

    #model-manager-list {{
        width: 50;
        height: auto;
        max-height: 20;
        color: {TEXT};
    }}

    #model-manager-detail {{
        width: 1fr;
        height: auto;
        max-height: 20;
        color: {TEXT};
    }}

    #model-manager-footer {{
        margin-top: 1;
        color: #8ba4ae;
    }}
    """

    BINDINGS = [
        ("up", "previous", "Previous"),
        ("down", "next", "Next"),
        ("escape", "cancel", "Close"),
    ]

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(id="model-manager")
        models = payload.get("models", [])
        self.models = [dict(item) for item in models if isinstance(item, dict)]
        self.selected_index = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="model-manager-panel"):
            with Horizontal():
                yield Static("", id="model-manager-list")
                yield Static("", id="model-manager-detail")
            yield Static("Esc Close", id="model-manager-footer")

    def on_mount(self) -> None:
        self._refresh()

    def action_previous(self) -> None:
        if self.models:
            self.selected_index = max(0, self.selected_index - 1)
            self._refresh()

    def action_next(self) -> None:
        if self.models:
            self.selected_index = min(len(self.models) - 1, self.selected_index + 1)
            self._refresh()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _refresh(self) -> None:
        self.query_one("#model-manager-list", Static).update(self._render_list())
        self.query_one("#model-manager-detail", Static).update(self._render_detail())

    def _render_list(self) -> str:
        lines = ["Model Manager", ""]
        if not self.models:
            lines.append("No models found")
            return "\n".join(lines)
        for index, model in enumerate(self.models):
            marker = ">" if index == self.selected_index else " "
            revision = _revision_label(model)
            gated = " 🔒" if model.get("gated") else ""
            lines.append(
                f"{marker} {_model_status_dot(model)} {_model_label(model)}  "
                f"{_quant_label(model)}  {_size_label(model)} @{revision}{gated}"
            )
        return "\n".join(lines)

    def _render_detail(self) -> str:
        model = self._selected_model()
        if model is None:
            return "No model selected"
        files = _dict_or_empty(model.get("files"))
        lines = [
            "Detail",
            "",
            f"name: {_model_label(model)}",
            f"entry_id: {model.get('entry_id') or '-'}",
            f"source: {model.get('source') or '-'}",
            f"repo: {model.get('repo_id') or '-'}",
            f"revision: {_revision_detail(model)}",
            f"cache: {model.get('cache_state') or 'unknown'}",
            f"files: {_files_label(files)}",
        ]
        return "\n".join(lines)

    def _selected_model(self) -> dict[str, Any] | None:
        if not self.models:
            return None
        return self.models[self.selected_index]


def _model_label(model: dict[str, Any]) -> str:
    return str(model.get("display_name") or model.get("entry_id") or "unnamed-model")


def _quant_label(model: dict[str, Any]) -> str:
    return str(model.get("quant_format") or "none")


def _revision_label(model: dict[str, Any]) -> str:
    return str(model.get("commit_sha") or model.get("revision") or "-")


def _revision_detail(model: dict[str, Any]) -> str:
    revision = str(model.get("revision") or "-")
    commit = model.get("commit_sha")
    if commit:
        return f"{revision} → {commit}"
    return revision


def _model_status_dot(model: dict[str, Any]) -> str:
    state = str(model.get("cache_state") or "").lower()
    if state in {"cached", "ready", "local"}:
        return "●"
    if state in {"remote_only", "remote-only"}:
        return "○"
    if state in {"partial", "drift"}:
        return "▲"
    if state in {"downloading", "in-progress"}:
        return "◐"
    if state in {"missing", "unresolved"}:
        return "✕"
    return "○"


def _size_label(model: dict[str, Any]) -> str:
    try:
        size = int(model.get("size_bytes") or 0)
    except (TypeError, ValueError):
        size = 0
    if size <= 0:
        return "--"
    return f"{size / 1_000_000_000:.1f} GB"


def _files_label(files: dict[str, Any]) -> str:
    count = files.get("count")
    weights_format = files.get("weights_format")
    if count is None and not weights_format:
        return "-"
    parts = []
    if count is not None:
        parts.append(str(count))
    if weights_format:
        parts.append(str(weights_format))
    return " ".join(parts)


def _dict_or_empty(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}
