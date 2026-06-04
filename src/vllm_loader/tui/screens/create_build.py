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
                value="nightly",
                id="create-build-method",
            )
            yield Static("Label", classes="create-build-field-label")
            yield Input(
                placeholder="nvfp4-cu130",
                id="create-build-label",
            )
            yield Static("Package spec", classes="create-build-field-label")
            yield Input(
                placeholder="vllm==0.11.2",
                id="create-build-spec",
            )
            yield Static("Channel / variant", classes="create-build-field-label")
            yield Input(
                placeholder="cu130",
                id="create-build-channel",
            )
            yield Static("Python", classes="create-build-field-label")
            yield Input(
                placeholder="3.12",
                id="create-build-python",
            )
            yield Static("Commit", classes="create-build-field-label")
            yield Input(
                placeholder="abcdef123456",
                id="create-build-commit",
            )
            yield Static("Git URL", classes="create-build-field-label")
            yield Input(
                placeholder="https://github.com/vllm-project/vllm.git",
                id="create-build-url",
            )
            yield Static("Wheel / venv path", classes="create-build-field-label")
            yield Input(
                placeholder="/agent/wheels/vllm.whl",
                id="create-build-path",
            )
            yield Static("Build id", classes="create-build-field-label")
            yield Input(
                placeholder="optional; registry mints one when blank",
                id="create-build-build-id",
            )
            yield Static("Environment", classes="create-build-field-label")
            yield Input(
                placeholder="KEY=value OTHER=value",
                id="create-build-env",
            )
            yield Static("", id="create-build-error")

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

    def _collect_build_params(self) -> dict[str, Any]:
        method = self.query_one("#create-build-method", Select).value
        params: dict[str, Any] = {"method": str(method or "").strip()}
        fields = {
            "build_id": self._field_value("#create-build-build-id"),
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
