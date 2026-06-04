from __future__ import annotations

import shlex
from typing import Any

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Checkbox, Input, Static

from vllm_loader.tui.theme import ACCENT, BAD, SURFACE_ALT, TEXT


class PinModelScreen(ModalScreen[dict[str, Any] | None]):
    CSS = f"""
    PinModelScreen {{
        align: center middle;
        background: #091015;
    }}

    #pin-model-panel {{
        width: 96;
        border: solid {ACCENT};
        background: {SURFACE_ALT};
        padding: 1 2;
    }}

    #pin-model-title {{
        margin-bottom: 1;
        color: {TEXT};
        text-style: bold;
    }}

    .pin-model-field-label {{
        margin-top: 1;
        color: {TEXT};
    }}

    #pin-model-error {{
        margin-top: 1;
        color: {BAD};
    }}
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, *, initial: str = "") -> None:
        super().__init__(id="pin-model")
        self.initial = initial
        try:
            self.initial_params = _parse_model_pin_params(initial) if initial else {}
        except ValueError:
            self.initial_params = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="pin-model-panel"):
            yield Static("Pin Model", id="pin-model-title")
            yield Static("Repo id", classes="pin-model-field-label")
            yield Input(
                value=self._initial_value("repo_id"),
                placeholder="org/model",
                id="pin-model-repo-id",
            )
            yield Static("Local path", classes="pin-model-field-label")
            yield Input(
                value=self._initial_value("local_path"),
                placeholder="/agent/models/model",
                id="pin-model-local-path",
            )
            yield Static("URL", classes="pin-model-field-label")
            yield Input(
                value=self._initial_value("url"),
                placeholder="https://host/model.gguf",
                id="pin-model-url",
            )
            yield Static("Display name", classes="pin-model-field-label")
            yield Input(
                value=self._initial_value("display_name"),
                placeholder="qwen-remote",
                id="pin-model-display-name",
            )
            yield Static("Revision", classes="pin-model-field-label")
            yield Input(
                value=self._initial_value("revision"),
                placeholder="main",
                id="pin-model-revision",
            )
            yield Static("Commit sha", classes="pin-model-field-label")
            yield Input(
                value=self._initial_value("commit_sha"),
                placeholder="abcdef123456",
                id="pin-model-commit-sha",
            )
            yield Static("Quant format", classes="pin-model-field-label")
            yield Input(
                value=self._initial_value("quant_format"),
                placeholder="bf16",
                id="pin-model-quant-format",
            )
            yield Static("Tokenizer", classes="pin-model-field-label")
            yield Input(
                value=self._initial_value("tokenizer"),
                placeholder="optional tokenizer ref",
                id="pin-model-tokenizer",
            )
            yield Static("Notes", classes="pin-model-field-label")
            yield Input(
                value=self._initial_value("notes"),
                placeholder="operator note",
                id="pin-model-notes",
            )
            yield Checkbox(
                "Gated repository",
                value=self._initial_bool("gated"),
                id="pin-model-gated",
            )
            yield Checkbox(
                "Token required",
                value=self._initial_bool("token_required"),
                id="pin-model-token-required",
            )
            yield Static("", id="pin-model-error")

    def on_mount(self) -> None:
        self.query_one("#pin-model-repo-id", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.action_submit()

    def action_submit(self) -> None:
        try:
            self.dismiss(self._collect_model_pin_params())
        except ValueError as exc:
            self.query_one("#pin-model-error", Static).update(str(exc))

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _initial_value(self, key: str) -> str:
        value = self.initial_params.get(key)
        return str(value) if isinstance(value, str) else ""

    def _initial_bool(self, key: str) -> bool:
        return bool(self.initial_params.get(key))

    def _field_value(self, selector: str) -> str:
        return self.query_one(selector, Input).value.strip()

    def _checked(self, selector: str) -> bool:
        return bool(self.query_one(selector, Checkbox).value)

    def _collect_model_pin_params(self) -> dict[str, Any]:
        fields = {
            "repo_id": self._field_value("#pin-model-repo-id"),
            "local_path": self._field_value("#pin-model-local-path"),
            "url": self._field_value("#pin-model-url"),
            "display_name": self._field_value("#pin-model-display-name"),
            "revision": self._field_value("#pin-model-revision"),
            "commit_sha": self._field_value("#pin-model-commit-sha"),
            "quant_format": self._field_value("#pin-model-quant-format"),
            "tokenizer": self._field_value("#pin-model-tokenizer"),
            "notes": self._field_value("#pin-model-notes"),
        }
        params: dict[str, Any] = {key: value for key, value in fields.items() if value}
        if params.get("url"):
            params["source"] = "url"
        if self._checked("#pin-model-gated"):
            params["gated"] = True
        if self._checked("#pin-model-token-required"):
            params["token_required"] = True
        if not params.get("repo_id") and not params.get("local_path") and not params.get("url"):
            raise ValueError("Enter repo_id=<repo>, local_path=<path>, or url=<url>")
        return params


def _parse_model_pin_params(value: str) -> dict[str, Any]:
    tokens = [token.strip() for token in shlex.split(value) if token.strip()]
    if not tokens:
        raise ValueError("Enter model pin metadata")
    params: dict[str, Any] = {}
    for token in tokens:
        if "=" not in token:
            raise ValueError(f"Use key=value for '{token}'")
        key, raw_value = token.split("=", 1)
        key = key.strip().replace("-", "_")
        raw_value = raw_value.strip()
        if not key or not raw_value:
            raise ValueError("Model pin fields must use key=value")
        if key in {"gated", "token_required"}:
            params[key] = raw_value.lower() in {"1", "true", "yes", "on"}
        else:
            params[key] = raw_value
    if params.get("url"):
        params["source"] = "url"
    if not params.get("repo_id") and not params.get("local_path") and not params.get("url"):
        raise ValueError("Enter repo_id=<repo>, local_path=<path>, or url=<url>")
    return params
