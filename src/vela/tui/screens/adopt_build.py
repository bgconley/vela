from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Select, Static

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
from vela.tui.widgets import Checkbox, Field, KeyHintBar, ValidationCard


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
    #adopt-build-validation {{ height: auto; }}
    #adopt-build-discovered-slot {{ height: auto; }}
    #adopt-build-validation-note {{ color: {TEXT_FAINT}; height: auto; }}
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    _NEUTRAL_NOTE = (
        "Enter a venv path — it is validated on the target as you type "
        "(vLLM present, versions detected)."
    )

    def __init__(
        self,
        *,
        probe: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
        discover: Callable[[], Awaitable[list[dict[str, Any]]]] | None = None,
    ) -> None:
        super().__init__(id="adopt-build")
        # Optional async venv probe + discovery (wired to the target agent by
        # app.py). Kwarg-only defaults keep the no-arg constructor contract.
        self._probe = probe
        self._discover = discover
        self._autofill_version = ""

    def compose(self) -> ComposeResult:
        with Vertical(id="adopt-build-panel"):
            yield Static("Adopt Build", id="adopt-build-title")
            yield Static(
                "Register a vLLM virtualenv that already exists on the target as a "
                "managed build — no install, no download.",
                id="adopt-build-subtitle",
            )
            yield Vertical(id="adopt-build-discovered-slot")
            yield Field(
                "Venv path",
                Input(placeholder="/home/user/venvs/vllm-nightly", id="adopt-build-venv-path"),
                helper="Absolute path on the target to a venv that already has vLLM.",
                id="ab-venv",
            )
            with Vertical(id="adopt-build-validation"):
                yield Static(self._NEUTRAL_NOTE, id="adopt-build-validation-note")
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
                [("⏎", "Adopt"), ("Esc", "Cancel")],
                id="adopt-build-footer",
            )

    def on_mount(self) -> None:
        self.query_one("#adopt-build-venv-path", Input).focus()
        if self._discover is not None:
            self.run_worker(
                self._load_discovered(),
                exclusive=True,
                group="adopt-venv-discover",
            )

    async def _load_discovered(self) -> None:
        discover = self._discover
        if discover is None:
            return
        try:
            entries = await discover()
        except Exception:
            return
        options: list[tuple[str, str]] = []
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            path = str(entry.get("venv_path") or "")
            if not path:
                continue
            if entry.get("ok"):
                version = str(entry.get("vllm_version") or "?")
                options.append((f"{path} — vllm {version}", path))
            else:
                reason = str(entry.get("reason") or "not adoptable")
                options.append((f"{path} — {reason}", path))
        if not options:
            return
        slot = self.query_one("#adopt-build-discovered-slot", Vertical)
        await slot.mount(
            Field(
                "Discovered venvs",
                Select(
                    [("Type a path manually", "__manual__"), *options],
                    value="__manual__",
                    allow_blank=False,
                    id="adopt-build-discovered",
                ),
                helper=(
                    "Found under common venv roots on the target — pick one, "
                    "or type a path below."
                ),
                id="ab-discovered",
            )
        )

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.control.id != "adopt-build-discovered":
            return
        value = str(event.value or "")
        if value and value != "__manual__":
            self.query_one("#adopt-build-venv-path", Input).value = value

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.action_submit()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "adopt-build-venv-path":
            # Exclusive worker: a newer keystroke cancels the in-flight probe.
            self.run_worker(
                self._probe_and_render(event.value.strip()),
                exclusive=True,
                group="adopt-venv-probe",
            )

    async def _probe_and_render(self, path: str) -> None:
        if not path or self._probe is None:
            await self._swap_validation(None)
            return
        try:
            result = await self._probe(path)
        except Exception as exc:
            result = {"ok": False, "reason": f"validation unavailable: {exc}"}
        if not isinstance(result, dict):
            result = {"ok": False, "reason": "validation unavailable"}
        await self._swap_validation(result)
        if result.get("ok"):
            version = str(result.get("vllm_version") or "")
            if version:
                version_input = self.query_one("#adopt-build-vllm-version", Input)
                # Auto-fill, but never clobber a hand-typed override.
                if not version_input.value or version_input.value == self._autofill_version:
                    version_input.value = version
                    self._autofill_version = version

    async def _swap_validation(self, result: dict[str, Any] | None) -> None:
        slot = self.query_one("#adopt-build-validation", Vertical)
        await slot.remove_children()
        if result is None:
            await slot.mount(Static(self._NEUTRAL_NOTE, id="adopt-build-validation-note"))
            return
        if result.get("ok"):
            detail = " · ".join(
                part
                for part in (
                    f"vllm {result.get('vllm_version')}" if result.get("vllm_version") else "",
                    f"torch {result.get('torch_version')}" if result.get("torch_version") else "",
                    (
                        f"python {result.get('python_version')}"
                        if result.get("python_version")
                        else ""
                    ),
                )
                if part
            )
            await slot.mount(
                ValidationCard(
                    True,
                    "Validated — vLLM detected",
                    detail=detail,
                    note="Detected from the venv on the target — you don't type the version.",
                )
            )
            return
        reason = str(result.get("reason") or result.get("error") or "validation failed")
        await slot.mount(
            ValidationCard(
                False,
                "Not adoptable yet",
                detail=reason,
                note="Fix the path (or the venv) — validation re-runs as you type.",
            )
        )

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
