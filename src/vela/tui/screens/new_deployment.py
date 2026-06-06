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

    #new-deployment-steps {{
        margin-bottom: 1;
        color: {ACCENT};
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
            yield Static(
                "Target -> Runtime -> Model -> Customize -> Review -> Save",
                id="new-deployment-steps",
            )
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


class NewDeploymentReviewScreen(ModalScreen[dict[str, Any] | None]):
    CSS = f"""
    NewDeploymentReviewScreen {{
        align: center middle;
        background: #091015;
    }}

    #new-deployment-review-panel {{
        width: 92;
        max-height: 38;
        border: solid {ACCENT};
        background: {SURFACE_ALT};
        padding: 1 2;
    }}

    #new-deployment-review-title {{
        color: {TEXT};
        text-style: bold;
    }}

    #new-deployment-review-steps {{
        margin-bottom: 1;
        color: {ACCENT};
    }}

    .new-deployment-review-label {{
        margin-top: 1;
        color: {TEXT};
        text-style: bold;
    }}

    #new-deployment-review-summary,
    #new-deployment-review-derived,
    #new-deployment-review-warnings,
    #new-deployment-review-preview {{
        color: {TEXT};
    }}

    #new-deployment-review-preview {{
        max-height: 12;
        background: #091015;
        padding: 0 1;
    }}

    #new-deployment-review-actions {{
        margin-top: 1;
        color: {GOOD};
    }}
    """

    BINDINGS = [("ctrl+s", "save", "Save"), ("escape", "cancel", "Cancel")]

    def __init__(
        self,
        *,
        config: dict[str, Any],
        preview: str,
        derived: list[dict[str, Any]],
        warnings: list[str],
    ) -> None:
        super().__init__(id="new-deployment-review")
        self.config = dict(config)
        self.preview = preview
        self.derived = [dict(item) for item in derived]
        self.warnings = list(warnings)

    def compose(self) -> ComposeResult:
        with Vertical(id="new-deployment-review-panel"):
            yield Static("Review Deployment", id="new-deployment-review-title")
            yield Static(
                "Target -> Runtime -> Model -> Customize -> Review -> Save",
                id="new-deployment-review-steps",
            )
            yield Static("Summary", classes="new-deployment-review-label")
            yield Static(self._summary_text(), id="new-deployment-review-summary")
            yield Static("Auto-derived fields", classes="new-deployment-review-label")
            yield Static(self._derived_text(), id="new-deployment-review-derived")
            yield Static("Warnings", classes="new-deployment-review-label")
            yield Static(self._warnings_text(), id="new-deployment-review-warnings")
            yield Static("Resolved command", classes="new-deployment-review-label")
            yield Static(self.preview, id="new-deployment-review-preview")
            yield Static(
                "Ctrl+S Save   Esc Cancel",
                id="new-deployment-review-actions",
            )

    def action_save(self) -> None:
        self.dismiss({"config": self.config})

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _summary_text(self) -> str:
        server = self.config.get("server") if isinstance(self.config.get("server"), dict) else {}
        command = (
            self.config.get("command") if isinstance(self.config.get("command"), dict) else {}
        )
        docker = command.get("docker") if isinstance(command.get("docker"), dict) else {}
        lines = [
            f"Name: {self.config.get('name', '')}",
            f"Model: {self.config.get('model', '')}",
            f"Runtime: {command.get('runtime', 'process')}",
            f"Endpoint: {server.get('host', '127.0.0.1')}:{server.get('port', '')}",
            f"Exposure: {server.get('exposure', 'local')}",
        ]
        if docker:
            lines.append(f"Image: {docker.get('image', '')}")
            lines.append(f"Container: {docker.get('container_name', '')}")
        return "\n".join(lines)

    def _derived_text(self) -> str:
        if not self.derived:
            return "No auto-derived fields reported."
        lines = []
        for item in self.derived:
            field = str(item.get("field") or "")
            value = str(item.get("value") or "")
            source = str(item.get("source") or "auto")
            lines.append(f"{field}: {value} ({source})")
        return "\n".join(lines)

    def _warnings_text(self) -> str:
        if not self.warnings:
            return "No warnings."
        return "\n".join(f"- {warning}" for warning in self.warnings)
