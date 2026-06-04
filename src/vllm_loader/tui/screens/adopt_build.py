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

    .adopt-build-field-label {{
        margin-top: 1;
        color: {TEXT};
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
            yield Static("Label", classes="adopt-build-field-label")
            yield Input(
                placeholder="external-nightly",
                id="adopt-build-label",
            )
            yield Static("Venv path", classes="adopt-build-field-label")
            yield Input(
                placeholder="/agent/venvs/vllm-nightly",
                id="adopt-build-venv-path",
            )
            yield Static("vLLM version", classes="adopt-build-field-label")
            yield Input(
                placeholder="0.17.0.dev",
                id="adopt-build-vllm-version",
            )
            yield Static("Version profile", classes="adopt-build-field-label")
            yield Input(
                placeholder="current",
                id="adopt-build-vllm-version-profile",
            )
            yield Static("", id="adopt-build-error")

    def on_mount(self) -> None:
        self.query_one("#adopt-build-label", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        try:
            self.dismiss(self._collect_adopt_build_params())
        except ValueError as exc:
            self.query_one("#adopt-build-error", Static).update(str(exc))

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _field_value(self, selector: str) -> str:
        return self.query_one(selector, Input).value.strip()

    def _collect_adopt_build_params(self) -> dict[str, Any]:
        params = {
            "label": self._field_value("#adopt-build-label"),
            "venv_path": self._field_value("#adopt-build-venv-path"),
            "vllm_version": self._field_value("#adopt-build-vllm-version"),
            "vllm_version_profile": self._field_value(
                "#adopt-build-vllm-version-profile"
            ),
        }
        cleaned = {key: value for key, value in params.items() if value}
        if not cleaned.get("venv_path"):
            raise ValueError("Enter venv_path=<path>")
        return cleaned


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
