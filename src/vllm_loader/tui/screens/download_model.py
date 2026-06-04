from __future__ import annotations

import shlex
from typing import Any

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from vllm_loader.tui.theme import ACCENT, BAD, SURFACE_ALT, TEXT


class DownloadModelScreen(ModalScreen[dict[str, Any] | None]):
    CSS = f"""
    DownloadModelScreen {{
        align: center middle;
        background: #091015;
    }}

    #download-model-panel {{
        width: 86;
        border: solid {ACCENT};
        background: {SURFACE_ALT};
        padding: 1 2;
    }}

    #download-model-title {{
        margin-bottom: 1;
        color: {TEXT};
        text-style: bold;
    }}

    #download-model-summary {{
        color: {TEXT};
    }}

    .download-model-field-label {{
        margin-top: 1;
        color: {TEXT};
    }}

    #download-model-error {{
        margin-top: 1;
        color: {BAD};
    }}
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, model: dict[str, Any]) -> None:
        super().__init__(id="download-model")
        self.model = dict(model)

    def compose(self) -> ComposeResult:
        with Vertical(id="download-model-panel"):
            yield Static("Download Model", id="download-model-title")
            yield Static(self._summary(), id="download-model-summary")
            yield Static("Revision override", classes="download-model-field-label")
            yield Input(
                placeholder=self._revision_placeholder(),
                id="download-model-revision",
            )
            yield Static("Allow patterns", classes="download-model-field-label")
            yield Input(
                value=self._patterns_value("allow_patterns"),
                placeholder="*.safetensors *.json",
                id="download-model-allow",
            )
            yield Static("Ignore patterns", classes="download-model-field-label")
            yield Input(
                value=self._patterns_value("ignore_patterns"),
                placeholder="*.bin *.pth",
                id="download-model-ignore",
            )
            yield Static("", id="download-model-error")

    def on_mount(self) -> None:
        self.query_one("#download-model-revision", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.action_submit()

    def action_submit(self) -> None:
        try:
            self.dismiss(self._collect_download_params())
        except ValueError as exc:
            self.query_one("#download-model-error", Static).update(str(exc))

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _summary(self) -> str:
        parts = [
            f"name: {_model_label(self.model)}",
            f"ref: {_model_ref(self.model)}",
            f"repo: {self.model.get('repo_id') or '-'}",
            f"cache: {self.model.get('cache_state') or 'unknown'}",
        ]
        if self.model.get("gated") or self.model.get("token_required"):
            parts.append("auth: HF_TOKEN must be set on the target")
        return "\n".join(parts)

    def _revision_placeholder(self) -> str:
        return str(self.model.get("commit_sha") or self.model.get("revision") or "main")

    def _field_value(self, selector: str) -> str:
        return self.query_one(selector, Input).value.strip()

    def _patterns_value(self, field: str) -> str:
        value = self.model.get(field)
        if not isinstance(value, list):
            return ""
        return " ".join(str(item) for item in value if isinstance(item, str) and item)

    def _collect_download_params(self) -> dict[str, Any]:
        model_ref = _model_ref(self.model)
        if not model_ref:
            raise ValueError("Selected model has no model_ref")
        params: dict[str, Any] = {"model_ref": model_ref}
        revision = self._field_value("#download-model-revision")
        allow_patterns = _patterns_from_input(self._field_value("#download-model-allow"))
        ignore_patterns = _patterns_from_input(self._field_value("#download-model-ignore"))
        if revision:
            params["revision"] = revision
        if allow_patterns:
            params["allow_patterns"] = allow_patterns
        if ignore_patterns:
            params["ignore_patterns"] = ignore_patterns
        return params


def _model_label(model: dict[str, Any]) -> str:
    return str(model.get("label") or model.get("display_name") or model.get("entry_id") or "")


def _model_ref(model: dict[str, Any]) -> str:
    return str(model.get("model_ref") or model.get("entry_id") or model.get("display_name") or "")


def _patterns_from_input(value: str) -> list[str]:
    if not value:
        return []
    tokens: list[str] = []
    for token in shlex.split(value):
        tokens.extend(part.strip() for part in token.split(",") if part.strip())
    return tokens
