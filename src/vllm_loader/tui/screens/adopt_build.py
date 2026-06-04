from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Checkbox, Input, Static

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
            yield Checkbox("Copy venv into managed build", id="adopt-build-copy")
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

    def _checked(self, selector: str) -> bool:
        return bool(self.query_one(selector, Checkbox).value)

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
        if self._checked("#adopt-build-copy"):
            cleaned["copy"] = "true"
        if not cleaned.get("venv_path"):
            raise ValueError("Enter venv_path=<path>")
        return cleaned
