from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Select, Static

from vela.tui.theme import ACCENT, BAD, GOOD, MUTED, SURFACE_ALT, TEXT


class NewDeploymentScreen(ModalScreen[dict[str, Any] | None]):
    CSS = f"""
    NewDeploymentScreen {{
        align: center middle;
        background: #091015;
    }}

    #new-deployment-panel {{
        width: 76;
        border: solid {ACCENT};
        background: {SURFACE_ALT};
        padding: 1 2;
    }}

    #new-deployment-title {{
        margin-bottom: 1;
        color: {TEXT};
        text-style: bold;
    }}

    #new-deployment-target {{
        margin-bottom: 1;
        color: {MUTED};
    }}

    .new-deployment-field-label {{
        margin-top: 1;
        color: {TEXT};
    }}

    .new-deployment-row {{
        height: auto;
    }}

    .new-deployment-column {{
        width: 1fr;
        padding-right: 2;
    }}

    #new-deployment-error {{
        margin-top: 1;
        color: {BAD};
    }}

    #new-deployment-actions {{
        margin-top: 1;
        color: {GOOD};
    }}
    """

    BINDINGS = [("ctrl+s", "submit", "Save"), ("escape", "cancel", "Cancel")]

    def __init__(
        self,
        *,
        target_label: str,
        presets: list[dict[str, Any]],
    ) -> None:
        super().__init__(id="new-deployment")
        self.target_label = target_label
        self.presets = [dict(preset) for preset in presets]

    def compose(self) -> ComposeResult:
        with Vertical(id="new-deployment-panel"):
            yield Static("New Deployment", id="new-deployment-title")
            yield Static(f"Target: {self.target_label}", id="new-deployment-target")
            with Horizontal(classes="new-deployment-row"):
                with Vertical(classes="new-deployment-column"):
                    yield Static("Name", classes="new-deployment-field-label")
                    yield Input(placeholder="qwen3-32b-bf16", id="new-deployment-name")
                    yield Static("Runtime", classes="new-deployment-field-label")
                    yield Select(
                        [("Process", "process"), ("Docker", "docker")],
                        value="process",
                        allow_blank=False,
                        id="new-deployment-runtime",
                    )
                    yield Static("Model", classes="new-deployment-field-label")
                    yield Input(
                        placeholder="Qwen/Qwen3-32B",
                        id="new-deployment-model",
                    )
                    yield Static("Docker image", classes="new-deployment-field-label")
                    yield Input(
                        placeholder="vllm/vllm-openai@sha256:...",
                        id="new-deployment-image",
                    )
                with Vertical(classes="new-deployment-column"):
                    yield Static("Preset", classes="new-deployment-field-label")
                    yield Select(
                        self._preset_options(),
                        value=self._default_preset(),
                        allow_blank=False,
                        id="new-deployment-preset",
                    )
                    yield Static("Host", classes="new-deployment-field-label")
                    yield Input(
                        value="127.0.0.1",
                        placeholder="127.0.0.1",
                        id="new-deployment-host",
                    )
                    yield Static("Port", classes="new-deployment-field-label")
                    yield Input(placeholder="auto", id="new-deployment-port")
                    yield Static("Exposure", classes="new-deployment-field-label")
                    yield Select(
                        [("Local", "local"), ("LAN", "lan"), ("Public", "public")],
                        value="local",
                        allow_blank=False,
                        id="new-deployment-exposure",
                    )
            yield Static("", id="new-deployment-error")
            yield Static("Ctrl+S Save   Esc Cancel", id="new-deployment-actions")

    def on_mount(self) -> None:
        self.query_one("#new-deployment-name", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.action_submit()

    def action_submit(self) -> None:
        try:
            self.dismiss(self._collect_spec())
        except ValueError as exc:
            self.query_one("#new-deployment-error", Static).update(str(exc))

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _collect_spec(self) -> dict[str, Any]:
        name = self._field_value("#new-deployment-name")
        model = self._field_value("#new-deployment-model")
        runtime = str(self.query_one("#new-deployment-runtime", Select).value or "process")
        image = self._field_value("#new-deployment-image")
        preset = str(self.query_one("#new-deployment-preset", Select).value or "balanced")
        host = self._field_value("#new-deployment-host") or "127.0.0.1"
        port = self._field_value("#new-deployment-port")
        exposure = str(self.query_one("#new-deployment-exposure", Select).value or "local")
        if not name:
            raise ValueError("Name is required")
        if not model:
            raise ValueError("Model is required")
        spec: dict[str, Any] = {
            "name": name,
            "target": self.target_label,
            "model": model,
            "preset": preset,
            "runtime": runtime,
            "overrides": {"server": {"host": host, "exposure": exposure}},
        }
        if port:
            try:
                spec["overrides"]["server"]["port"] = int(port)
            except ValueError as exc:
                raise ValueError("Port must be an integer or blank") from exc
        if runtime == "docker":
            if not image:
                raise ValueError("Docker image is required")
            spec["runtime"] = {"kind": "docker", "image": image}
        return spec

    def _field_value(self, selector: str) -> str:
        return self.query_one(selector, Input).value.strip()

    def _preset_options(self) -> list[tuple[str, str]]:
        options: list[tuple[str, str]] = []
        for preset in self.presets:
            name = str(preset.get("name") or "").strip()
            if name:
                options.append((name, name))
        return options or [("balanced", "balanced")]

    def _default_preset(self) -> str:
        names = {value for _, value in self._preset_options()}
        return "balanced" if "balanced" in names else next(iter(names))
