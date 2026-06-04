from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from vllm_loader.tui.theme import ACCENT, BAD, SURFACE_ALT, TEXT


class AdoptBuildScreen(ModalScreen[dict[str, Any] | None]):
    CSS = f"""
    AdoptBuildScreen {{
        align: center middle;
        background: #091015;
    }}

    #adopt-build-panel {{
        width: 88;
        border: solid {ACCENT};
        background: {SURFACE_ALT};
        padding: 1 2;
    }}

    #adopt-build-title {{
        margin-bottom: 1;
        color: {TEXT};
        text-style: bold;
    }}

    #adopt-build-error {{
        margin-top: 1;
        color: {BAD};
    }}
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self) -> None:
        super().__init__(id="adopt-build")

    def compose(self) -> ComposeResult:
        with Vertical(id="adopt-build-panel"):
            yield Static("Adopt Build", id="adopt-build-title")
            yield Input(
                placeholder=(
                    "label=external venv_path=/agent/venvs/vllm"
                ),
                id="adopt-build-input",
            )
            yield Static("", id="adopt-build-error")

    def on_mount(self) -> None:
        self.query_one("#adopt-build-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        try:
            self.dismiss(_parse_adopt_build_params(event.value))
        except ValueError as exc:
            self.query_one("#adopt-build-error", Static).update(str(exc))

    def action_cancel(self) -> None:
        self.dismiss(None)


def _parse_adopt_build_params(value: str) -> dict[str, Any]:
    tokens = [token.strip() for token in value.split() if token.strip()]
    if not tokens:
        raise ValueError("Enter venv_path=<path>")
    params: dict[str, Any] = {}
    for token in tokens:
        if "=" not in token:
            raise ValueError(f"Use key=value for '{token}'")
        key, raw_value = token.split("=", 1)
        key = key.strip().replace("-", "_")
        raw_value = raw_value.strip()
        if not key or not raw_value:
            raise ValueError("Build fields must use key=value")
        params[key] = raw_value
    if not params.get("venv_path"):
        raise ValueError("Enter venv_path=<path>")
    return params
