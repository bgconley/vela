from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from vllm_loader.tui.theme import ACCENT, BAD, SURFACE_ALT, TEXT


class CreateBuildScreen(ModalScreen[dict[str, Any] | None]):
    CSS = f"""
    CreateBuildScreen {{
        align: center middle;
        background: #091015;
    }}

    #create-build-panel {{
        width: 82;
        border: solid {ACCENT};
        background: {SURFACE_ALT};
        padding: 1 2;
    }}

    #create-build-title {{
        margin-bottom: 1;
        color: {TEXT};
        text-style: bold;
    }}

    #create-build-error {{
        margin-top: 1;
        color: {BAD};
    }}
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self) -> None:
        super().__init__(id="create-build")

    def compose(self) -> ComposeResult:
        with Vertical(id="create-build-panel"):
            yield Static("Create Build", id="create-build-title")
            yield Input(
                placeholder="method=nightly label=nvfp4 channel=cu130 python=3.12",
                id="create-build-input",
            )
            yield Static("", id="create-build-error")

    def on_mount(self) -> None:
        self.query_one("#create-build-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        try:
            self.dismiss(_parse_build_params(event.value))
        except ValueError as exc:
            self.query_one("#create-build-error", Static).update(str(exc))

    def action_cancel(self) -> None:
        self.dismiss(None)


def _parse_build_params(value: str) -> dict[str, Any]:
    tokens = [token.strip() for token in value.split() if token.strip()]
    if not tokens:
        raise ValueError("Enter a build method")
    params: dict[str, Any] = {}
    for index, token in enumerate(tokens):
        if "=" not in token:
            if index == 0 and "method" not in params:
                params["method"] = token
                continue
            raise ValueError(f"Use key=value for '{token}'")
        key, raw_value = token.split("=", 1)
        key = key.strip().replace("-", "_")
        raw_value = raw_value.strip()
        if not key or not raw_value:
            raise ValueError("Build fields must use key=value")
        params[key] = raw_value
    if not params.get("method"):
        raise ValueError("Enter method=<type>")
    return params
