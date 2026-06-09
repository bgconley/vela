from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Checkbox, Input, Static

from vela.tui.theme import (
    BG_BASE,
    BG_INSET,
    BG_PANEL,
    BORDER_STRONG,
    BORDER_SUBTLE,
    CYAN,
    GREEN,
    RED,
    TEXT_FAINT,
    TEXT_SECONDARY,
)
from vela.tui.widgets import Field, KeyHintBar, ValidationCard


class AdoptBuildScreen(ModalScreen[dict[str, Any] | None]):
    CSS = f"""
    AdoptBuildScreen {{
        align: center middle;
        background: {BG_BASE};
    }}
    #adopt-build-panel {{
        width: 80;
        max-height: 90%;
        overflow-y: auto;
        border: round {BORDER_STRONG};
        background: {BG_PANEL};
        padding: 1 2;
    }}
    #adopt-build-title {{ color: {CYAN}; text-style: bold; }}
    #adopt-build-subtitle {{ color: {TEXT_SECONDARY}; height: auto; margin-bottom: 1; }}
    #adopt-build-copy {{ margin-top: 1; }}
    #adopt-build-copy-help {{ color: {TEXT_FAINT}; height: auto; }}
    #adopt-build-preview {{
        border: round {BORDER_SUBTLE};
        background: {BG_INSET};
        padding: 1 2;
        margin-top: 1;
        height: auto;
    }}
    #adopt-build-preview-title {{ color: {TEXT_FAINT}; }}
    #adopt-build-preview-cmd {{ color: {GREEN}; height: auto; }}
    #adopt-build-preview-note {{ color: {TEXT_SECONDARY}; height: auto; }}
    #adopt-build-error {{ color: {RED}; height: auto; margin-top: 1; }}
    #adopt-build-footer {{ margin-top: 1; }}
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self) -> None:
        super().__init__(id="adopt-build")

    def compose(self) -> ComposeResult:
        with Vertical(id="adopt-build-panel"):
            yield Static("Adopt Build", id="adopt-build-title")
            yield Static(
                "Register a vLLM virtualenv that already exists on the target as a "
                "managed build — no install, no download.",
                id="adopt-build-subtitle",
            )
            yield Field(
                "Venv path",
                Input(placeholder="/home/user/venvs/vllm-nightly", id="adopt-build-venv-path"),
                helper="Absolute path on the target to a venv that already has vLLM.",
                id="ab-venv",
            )
            yield ValidationCard(
                True,
                "Validated — vLLM importable",
                detail="vllm 0.11.2 · torch 2.6.0 · python 3.12 · CUDA 12.8",
                note=(
                    "Detected automatically by importing vLLM at the path — "
                    "you don't type the version."
                ),
            )
            yield Field(
                "Label",
                Input(placeholder="vllm-nightly", id="adopt-build-label"),
                helper="Short name for the build list & when creating deployments.",
                required=True,
                id="ab-label",
            )
            yield Field(
                "vLLM version",
                Input(placeholder="0.11.2", id="adopt-build-vllm-version"),
                helper=(
                    "Auto-detected from the venv (above) — override only if "
                    "detection is unavailable."
                ),
                id="ab-version",
            )
            yield Field(
                "Version profile",
                Input(placeholder="current", id="adopt-build-vllm-version-profile"),
                helper="Flag-compatibility profile · usually current.",
                id="ab-profile",
            )
            yield Checkbox("Copy venv into the managed build", id="adopt-build-copy")
            yield Static(
                "Copy = isolated & safe (recommended). Uncheck to reference in place — saves "
                "disk, but later changes to that venv affect this build.",
                id="adopt-build-copy-help",
            )
            with Vertical(id="adopt-build-preview"):
                yield Static("▸ WILL DO", id="adopt-build-preview-title")
                yield Static(
                    "Register the venv as a managed build on the target.",
                    id="adopt-build-preview-cmd",
                )
                yield Static(
                    "Copy = an isolated copy into managed builds (or reference in place if "
                    "unchecked). No packages are installed or downloaded.",
                    id="adopt-build-preview-note",
                )
            yield Static("", id="adopt-build-error")
            yield KeyHintBar(
                [("⏎", "Adopt"), ("space", "Toggle copy"), ("Esc", "Cancel")],
                id="adopt-build-footer",
            )

    def on_mount(self) -> None:
        self.query_one("#adopt-build-venv-path", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.action_submit()

    def action_submit(self) -> None:
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
            "vllm_version_profile": self._field_value("#adopt-build-vllm-version-profile"),
        }
        cleaned = {key: value for key, value in params.items() if value}
        if self._checked("#adopt-build-copy"):
            cleaned["copy"] = "true"
        if not cleaned.get("venv_path"):
            raise ValueError("Enter venv_path=<path>")
        return cleaned
