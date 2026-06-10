from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
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


class ModelManagerScreen(ModalScreen):
    CSS = f"""
    ModelManagerScreen {{
        align: center middle;
        background: {BG_BASE};
    }}

    #model-manager-panel {{
        width: 104;
        max-height: 90%;
        overflow-y: auto;
        border: round {BORDER_STRONG};
        background: {BG_PANEL};
        padding: 1 2;
    }}

    #model-manager-list {{ width: 50; height: auto; max-height: 24; color: {TEXT_PRIMARY}; }}
    #model-manager-detail {{ width: 1fr; height: auto; max-height: 24; color: {TEXT_PRIMARY}; }}
    #model-manager-footer {{ margin-top: 1; }}
    """

    BINDINGS = [
        ("up", "previous", "Previous"),
        ("down", "next", "Next"),
        ("enter", "accept", "Select"),
        ("d", "download", "Download"),
        ("p", "pin", "Pin"),
        Binding("r", "refresh_models", "Refresh", priority=True),
        ("v", "verify", "Verify"),
        ("x", "remove", "Remove"),
        ("escape", "cancel", "Close"),
    ]

    def __init__(
        self, payload: dict[str, Any], *, focus_model: str | None = None
    ) -> None:
        super().__init__(id="model-manager")
        models = payload.get("models", [])
        self.models = [dict(item) for item in models if isinstance(item, dict)]
        self.selected_index = self._focus_index(focus_model)

    def compose(self) -> ComposeResult:
        yield MasterDetail(
            Static(id="model-manager-list"),
            Static(id="model-manager-detail"),
            footer=KeyHintBar(
                [
                    ("⏎", "Select"),
                    ("d", "Download"),
                    ("p", "Pin"),
                    ("r", "Refresh"),
                    ("v", "Verify"),
                    ("x", "Remove"),
                    ("Esc", "Close"),
                ],
                id="model-manager-footer",
            ),
            id="model-manager-panel",
        )

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

    def action_accept(self) -> None:
        model = self._selected_model()
        if model is None:
            self.dismiss(None)
            return
        self.dismiss(_model_selection_payload(model))

    def action_download(self) -> None:
        model = self._selected_model()
        if model is None:
            self.dismiss(None)
            return
        if _is_url_model(model):
            self.dismiss(
                {
                    "action": "download_unavailable",
                    "label": _model_label(model),
                    "reason": "launch-time-only",
                }
            )
            return
        self.dismiss(_model_download_payload(model))

    def action_verify(self) -> None:
        model = self._selected_model()
        if model is None:
            self.dismiss(None)
            return
        self.dismiss(_model_action_payload("verify_model", model))

    def action_pin(self) -> None:
        model = self._selected_model()
        initial = _initial_pin_params(model) if model is not None else {}
        self.dismiss({"action": "pin_model", "initial": initial})

    def action_refresh_models(self) -> None:
        self.dismiss({"action": "refresh_models"})

    def action_remove(self) -> None:
        model = self._selected_model()
        if model is None:
            self.dismiss(None)
            return
        self.dismiss(_model_action_payload("remove_model", model))

    def _refresh(self) -> None:
        self.query_one("#model-manager-list", Static).update(self._render_list())
        self.query_one("#model-manager-detail", Static).update(self._render_detail())

    def _render_list(self) -> Text:
        text = Text()
        text.append("Model Manager\n", style=f"bold {CYAN}")
        if not self.models:
            text.append(
                "\nNo models yet — press p to pin one (HF repo id, local path, or URL)",
                style=TEXT_FAINT,
            )
            return text
        text.append("\n")
        for index, model in enumerate(self.models):
            selected = index == self.selected_index
            text.append(">" if selected else " ", style=CYAN if selected else TEXT_FAINT)
            text.append(" ")
            text.append(f"{_model_status_dot(model)} ", style=_model_status_color(model))
            text.append(
                _model_label(model),
                style=f"bold {TEXT_PRIMARY}" if selected else TEXT_PRIMARY,
            )
            revision = _revision_label(model)
            gated = " 🔒" if model.get("gated") else ""
            text.append(
                f"  {_quant_label(model)}  {_size_label(model)} @{revision}{gated}\n",
                style=TEXT_FAINT,
            )
        return text

    def _render_detail(self) -> Text:
        model = self._selected_model()
        if model is None:
            return Text("No model selected", style=TEXT_FAINT)
        files = _dict_or_empty(model.get("files"))
        text = Text()
        text.append(f"{_model_label(model)}\n", style=f"bold {TEXT_PRIMARY}")
        text.append("\n")
        rows = [
            ("entry_id", str(model.get("entry_id") or "-")),
            ("source", str(model.get("source") or "-")),
            ("repo", str(model.get("repo_id") or "-")),
            ("revision", _revision_detail(model)),
            ("cache", str(model.get("cache_state") or "unknown")),
            ("size", _size_label(model)),
        ]
        auth = _auth_detail(model)
        if auth:
            rows.append(("auth", auth))
        config_refs = model.get("config_refs")
        if isinstance(config_refs, list):
            rows.append(("used_by", _config_refs_label(config_refs)))
        rows.append(("files", _files_label(files)))
        if _is_url_model(model):
            rows.append(("download", "launch-time-only"))
            rows.append(("url", str(model.get("url") or "-")))
        for key, value in rows:
            text.append(f"{key}: ", style=TEXT_FAINT)
            text.append(f"{value}\n", style=TEXT_PRIMARY)
        return text

    def _selected_model(self) -> dict[str, Any] | None:
        if not self.models:
            return None
        return self.models[self.selected_index]

    def _focus_index(self, focus_model: str | None) -> int:
        if focus_model:
            for index, model in enumerate(self.models):
                if focus_model in {
                    str(model.get("entry_id") or ""),
                    str(model.get("display_name") or ""),
                }:
                    return index
        return 0


def _model_label(model: dict[str, Any]) -> str:
    return str(model.get("display_name") or model.get("entry_id") or "unnamed-model")


def _model_reference(model: dict[str, Any]) -> str:
    return str(model.get("entry_id") or model.get("display_name") or "")


def _model_action_payload(action: str, model: dict[str, Any]) -> dict[str, Any]:
    return {
        "action": action,
        "model_ref": _model_reference(model),
        "label": _model_label(model),
    }


def _model_download_payload(model: dict[str, Any]) -> dict[str, Any]:
    payload = _model_action_payload("download", model)
    for field in (
        "entry_id",
        "display_name",
        "repo_id",
        "revision",
        "commit_sha",
        "cache_state",
        "gated",
        "token_required",
        "allow_patterns",
        "ignore_patterns",
    ):
        if field in model:
            payload[field] = model[field]
    return payload


def _model_selection_payload(model: dict[str, Any]) -> dict[str, Any]:
    payload = _model_action_payload("select_model", model)
    revision = model.get("commit_sha") or model.get("revision")
    if isinstance(revision, str) and revision.strip():
        payload["revision"] = revision.strip()
    for field in ("cache_state", "gated", "token_required"):
        if field in model:
            payload[field] = model[field]
    return payload


def _is_url_model(model: dict[str, Any]) -> bool:
    return str(model.get("source") or "") == "url"


def _initial_pin_params(model: dict[str, Any]) -> dict[str, Any]:
    fields = [
        ("entry_id", model.get("entry_id")),
        ("repo_id", model.get("repo_id")),
        ("url", model.get("url")),
        ("display_name", model.get("display_name")),
        ("revision", model.get("revision")),
        ("commit_sha", model.get("commit_sha")),
        ("quant_format", model.get("quant_format")),
        ("tokenizer", model.get("tokenizer")),
        ("notes", model.get("notes")),
    ]
    params = {key: value for key, value in fields if isinstance(value, str) and value.strip()}
    if model.get("gated"):
        params["gated"] = True
    if model.get("token_required"):
        params["token_required"] = True
    if params.get("url"):
        params["source"] = "url"
    return params


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


def _model_status_color(model: dict[str, Any]) -> str:
    state = str(model.get("cache_state") or "").lower()
    if state in {"cached", "ready", "local"}:
        return GREEN
    if state in {"partial", "drift"}:
        return AMBER
    if state in {"downloading", "in-progress"}:
        return CYAN
    if state in {"missing", "unresolved"}:
        return RED
    return TEXT_FAINT


def _size_label(model: dict[str, Any]) -> str:
    unique = _size_value(model.get("unique_size_bytes"))
    nominal = _size_value(model.get("nominal_size_bytes"))
    if unique > 0 and nominal > 0 and nominal != unique:
        return f"{_gb_label(unique)} unique / {_gb_label(nominal)} nominal"
    size = unique or nominal or _size_value(model.get("size_bytes"))
    if size <= 0:
        return "--"
    return _gb_label(size)


_HF_TOKEN_WHERE = "(agent env or config env: block)"


def _config_refs_label(refs: list[object]) -> str:
    names = [str(item) for item in refs if str(item)]
    if not names:
        return "0 configs"
    visible = names[:3]
    suffix = f", +{len(names) - len(visible)}" if len(names) > len(visible) else ""
    return f"{len(names)} ({', '.join(visible)}{suffix})"


def _auth_detail(model: dict[str, Any]) -> str:
    gated = bool(model.get("gated"))
    token_required = bool(model.get("token_required")) or gated
    if gated and token_required:
        return f"gated, requires HF_TOKEN {_HF_TOKEN_WHERE}"
    if token_required:
        return f"requires HF_TOKEN {_HF_TOKEN_WHERE}"
    return ""


def _size_value(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _gb_label(size: int) -> str:
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
