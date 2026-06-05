from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Select, Static

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

    .create-build-field-label {{
        margin-top: 1;
        color: {TEXT};
    }}

    #create-build-uv-note {{
        margin-top: 1;
        color: {TEXT};
    }}

    #create-build-error {{
        margin-top: 1;
        color: {BAD};
    }}
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(
        self,
        *,
        initial: dict[str, Any] | None = None,
        error_message: str = "",
    ) -> None:
        super().__init__(id="create-build")
        self.initial = dict(initial or {})
        self.error_message = error_message

    def compose(self) -> ComposeResult:
        method = self._initial_value("method") or "nightly"
        with Vertical(id="create-build-panel"):
            yield Static("Create Build", id="create-build-title")
            yield Static("Method", classes="create-build-field-label")
            yield Select(
                [
                    ("Nightly", "nightly"),
                    ("Pip", "pip"),
                    ("Commit", "commit"),
                    ("Git", "git"),
                    ("Wheel", "wheel"),
                ],
                allow_blank=False,
                value=method,
                id="create-build-method",
            )
            yield Static(
                "Nightly and commit require uv on the target; pip, wheel, and git "
                "can fall back to pip.",
                id="create-build-uv-note",
            )
            yield Static("Label", classes="create-build-field-label")
            yield Input(
                placeholder="nvfp4-cu130",
                value=self._initial_value("label"),
                id="create-build-label",
            )
            yield Static("Package spec", classes="create-build-field-label")
            yield Input(
                placeholder="vllm==0.11.2",
                value=self._initial_value("spec"),
                id="create-build-spec",
            )
            yield Static("Channel / variant", classes="create-build-field-label")
            yield Input(
                placeholder="cu130",
                value=self._initial_value("channel"),
                id="create-build-channel",
            )
            yield Static("Python", classes="create-build-field-label")
            yield Input(
                placeholder="3.12",
                value=self._initial_value("python"),
                id="create-build-python",
            )
            yield Static("Commit", classes="create-build-field-label")
            yield Input(
                placeholder="abcdef123456",
                value=self._initial_value("commit"),
                id="create-build-commit",
            )
            yield Static("Git URL", classes="create-build-field-label")
            yield Input(
                placeholder="https://github.com/vllm-project/vllm.git",
                value=self._initial_value("url"),
                id="create-build-url",
            )
            yield Static("Wheel / venv path", classes="create-build-field-label")
            yield Input(
                placeholder="/agent/wheels/vllm.whl",
                value=self._initial_value("path"),
                id="create-build-path",
            )
            yield Static("Environment", classes="create-build-field-label")
            yield Input(
                placeholder="KEY=value OTHER=value",
                value=self._initial_env(),
                id="create-build-env",
            )
            yield Static(self.error_message, id="create-build-error")

    def on_mount(self) -> None:
        self.query_one("#create-build-label", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.action_submit()

    def action_submit(self) -> None:
        try:
            self.dismiss(self._collect_build_params())
        except ValueError as exc:
            self.query_one("#create-build-error", Static).update(str(exc))

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _field_value(self, selector: str) -> str:
        return self.query_one(selector, Input).value.strip()

    def _initial_value(self, key: str) -> str:
        value = self.initial.get(key)
        return str(value) if value is not None else ""

    def _initial_env(self) -> str:
        value = self.initial.get("env")
        if isinstance(value, list):
            return " ".join(str(item) for item in value)
        return str(value) if value is not None else ""

    def _collect_build_params(self) -> dict[str, Any]:
        method = self.query_one("#create-build-method", Select).value
        params: dict[str, Any] = {"method": str(method or "").strip()}
        fields = {
            "label": self._field_value("#create-build-label"),
            "spec": self._field_value("#create-build-spec"),
            "channel": self._field_value("#create-build-channel"),
            "python": self._field_value("#create-build-python"),
            "commit": self._field_value("#create-build-commit"),
            "url": self._field_value("#create-build-url"),
            "path": self._field_value("#create-build-path"),
        }
        params.update({key: value for key, value in fields.items() if value})
        env = self._field_value("#create-build-env")
        if env:
            params["env"] = [token for token in env.split() if token]
        if not params["method"]:
            raise ValueError("Choose a build method")
        return params
