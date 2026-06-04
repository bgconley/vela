from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static

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

    #pin-model-error {{
        margin-top: 1;
        color: {BAD};
    }}
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, *, initial: str = "") -> None:
        super().__init__(id="pin-model")
        self.initial = initial

    def compose(self) -> ComposeResult:
        with Vertical(id="pin-model-panel"):
            yield Static("Pin Model", id="pin-model-title")
            yield Input(
                value=self.initial,
                placeholder=(
                    "repo_id=org/model display_name=name revision=main commit_sha=abc123"
                ),
                id="pin-model-input",
            )
            yield Static("", id="pin-model-error")

    def on_mount(self) -> None:
        self.query_one("#pin-model-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        try:
            self.dismiss(_parse_model_pin_params(event.value))
        except ValueError as exc:
            self.query_one("#pin-model-error", Static).update(str(exc))

    def action_cancel(self) -> None:
        self.dismiss(None)


def _parse_model_pin_params(value: str) -> dict[str, Any]:
    tokens = [token.strip() for token in value.split() if token.strip()]
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
    if not params.get("repo_id") and not params.get("local_path"):
        raise ValueError("Enter repo_id=<repo> or local_path=<path>")
    return params
