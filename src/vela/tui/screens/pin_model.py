"""Pin Model — register a model so deployments can reference it by a stable id.

Rebuilt to the M-M1 mock (Figma page "Journey v2 — Friction Pass", node 70:2):
a Source select with progressive disclosure (HF repo / local path / URL), the
shared Field form language, a collapsed Advanced section behind Ctrl+R, the
canonical gated/HF_TOKEN note, and a live WILL PIN preview.

Contract: every pre-rebuild ``#pin-model-*`` control stays mounted (display is
toggled, never unmounted) and the dismiss payload keeps its exact shape — the
only addition is the optional ``download_now: True`` flag, which app.py strips
before the pin RPC and uses to kick the existing download job.
"""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
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
from vela.tui.widgets import Checkbox, Field, KeyHintBar

_HF_FIELDS = {"repo-id", "revision", "commit-sha"}
_SOURCE_FIELDS = {
    "hf": {"repo-id", "display-name", "revision", "commit-sha"},
    "local": {"local-path", "display-name"},
    "url": {"url", "display-name"},
}
_ADVANCED_FIELDS = ("quant-format", "tokenizer", "notes")


class PinModelScreen(ModalScreen[dict[str, Any] | None]):
    CSS = f"""
    PinModelScreen {{
        align: center middle;
        background: {BG_BASE};
    }}
    #pin-model-panel {{
        width: 76;
        max-height: 90%;
        overflow-y: auto;
        border: round {BORDER_STRONG};
        background: {BG_PANEL};
        padding: 1 2;
    }}
    #pin-model-titlebar {{ height: 1; }}
    #pin-model-title {{ width: 1fr; color: {CYAN}; text-style: bold; }}
    #pin-model-target {{ width: auto; color: {TEXT_FAINT}; }}
    #pin-model-subtitle {{ color: {TEXT_SECONDARY}; height: auto; margin-bottom: 1; }}
    #pin-model-gated-note {{ color: {TEXT_FAINT}; height: auto; margin-top: 1; }}
    #pin-model-advanced-hint {{ color: {TEXT_FAINT}; height: auto; margin-top: 1; }}
    #pm-advanced-checks {{ height: auto; }}
    .pm-check-help {{ color: {TEXT_FAINT}; height: auto; }}
    #pin-model-preview {{
        border: round {BORDER_SUBTLE};
        background: {BG_INSET};
        padding: 1 2;
        margin-top: 1;
        height: auto;
    }}
    #pin-model-preview-title {{ color: {TEXT_FAINT}; }}
    #pin-model-preview-line {{ color: {GREEN}; height: auto; }}
    #pin-model-preview-note {{ color: {TEXT_SECONDARY}; height: auto; }}
    #pin-model-error {{ margin-top: 1; color: {RED}; height: auto; }}
    #pin-model-footer {{ margin-top: 1; }}
    """

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("ctrl+s", "submit", "Pin"),
        # Ctrl+R (not a bare letter): printable keys are consumed by whichever
        # Input has focus, so a bare hotkey could never fire on a form.
        ("ctrl+r", "toggle_advanced", "Advanced"),
    ]

    def __init__(
        self,
        *,
        initial: dict[str, Any] | None = None,
        target_label: str = "",
    ) -> None:
        super().__init__(id="pin-model")
        self.initial_params = dict(initial or {})
        self.target_label = target_label
        self._ready = False
        # Advanced values supplied up front must not be hidden.
        self._advanced_visible = any(
            self.initial_params.get(key)
            for key in ("quant_format", "tokenizer", "notes", "gated", "token_required")
        )

    def compose(self) -> ComposeResult:
        with Vertical(id="pin-model-panel"):
            with Horizontal(id="pin-model-titlebar"):
                yield Static("Pin Model", id="pin-model-title")
                yield Static(self._target_text(), id="pin-model-target")
            yield Static(
                "Register a model so deployments can reference it by a stable id — "
                "pin now, download later.",
                id="pin-model-subtitle",
            )
            yield Field(
                "Source",
                Select(
                    [("HF repo", "hf"), ("Local path", "local"), ("URL", "url")],
                    value=self._initial_source(),
                    allow_blank=False,
                    id="pin-model-source",
                ),
                helper=(
                    "Where the model lives: HF repo · local path on the target · "
                    "direct URL. Pick one — only its field shows."
                ),
                id="pm-source",
            )
            yield Field(
                "Repo id",
                Input(
                    value=self._initial_value("repo_id"),
                    placeholder="org/model",
                    id="pin-model-repo-id",
                ),
                helper=(
                    "From huggingface.co — the org/model path in the repo URL. "
                    "Gated repos are detected automatically."
                ),
                required=True,
                id="pm-repo-id",
            )
            yield Field(
                "Path",
                Input(
                    value=self._initial_value("local_path"),
                    placeholder="/agent/models/model",
                    id="pin-model-local-path",
                ),
                helper="Absolute path on the target — already on disk, no download.",
                required=True,
                id="pm-local-path",
            )
            yield Field(
                "URL",
                Input(
                    value=self._initial_value("url"),
                    placeholder="https://host/model.gguf",
                    id="pin-model-url",
                ),
                helper=(
                    "Direct .gguf / .safetensors link — launch-time-only; vLLM "
                    "resolves it at launch, it is never cached."
                ),
                required=True,
                id="pm-url",
            )
            yield Field(
                "Display name",
                Input(
                    value=self._initial_value("display_name"),
                    placeholder="qwen-remote",
                    id="pin-model-display-name",
                ),
                helper="Shown in the model list & pickers — blank = derived from the repo.",
                optional=True,
                id="pm-display-name",
            )
            yield Field(
                "Revision",
                Input(
                    value=self._initial_value("revision"),
                    placeholder="main",
                    id="pin-model-revision",
                ),
                helper=(
                    "Branch or tag · blank = default branch. Resolved to a commit "
                    "at pin time."
                ),
                optional=True,
                id="pm-revision",
            )
            yield Field(
                "Commit sha",
                Input(
                    value=self._initial_value("commit_sha"),
                    placeholder="abcdef123456",
                    id="pin-model-commit-sha",
                ),
                helper="Full sha pins immutably — recommended for reproducible deployments.",
                optional=True,
                id="pm-commit-sha",
            )
            yield Field(
                "Quant format",
                Input(
                    value=self._initial_value("quant_format"),
                    placeholder="bf16 / fp8 / awq / gptq",
                    id="pin-model-quant-format",
                ),
                helper="Auto-detected from the repo when possible — set only to override.",
                optional=True,
                id="pm-quant-format",
            )
            yield Field(
                "Tokenizer",
                Input(
                    value=self._initial_value("tokenizer"),
                    placeholder="org/other-model",
                    id="pin-model-tokenizer",
                ),
                helper="Different repo to load the tokenizer from — rare; blank = the model's own.",
                optional=True,
                id="pm-tokenizer",
            )
            yield Field(
                "Notes",
                Input(
                    value=self._initial_value("notes"),
                    placeholder="operator note",
                    id="pin-model-notes",
                ),
                helper="Free text shown in the model detail — provenance, caveats, anything.",
                optional=True,
                id="pm-notes",
            )
            with Vertical(id="pm-advanced-checks"):
                yield Checkbox(
                    "Gated repository",
                    value=self._initial_bool("gated"),
                    id="pin-model-gated",
                )
                yield Static(
                    "Detected automatically — force on/off only if detection is wrong.",
                    classes="pm-check-help",
                )
                yield Checkbox(
                    "Token required",
                    value=self._initial_bool("token_required"),
                    id="pin-model-token-required",
                )
                yield Static(
                    "Detected automatically — force on/off only if detection is wrong.",
                    classes="pm-check-help",
                )
                yield Checkbox(
                    "Download now",
                    value=False,
                    id="pin-model-download-now",
                )
                yield Static(
                    "Start the download right after pinning (same as pressing d in "
                    "Models). Progress streams on the dashboard.",
                    classes="pm-check-help",
                )
            yield Static(
                "🔒 Gated model? Accept the license on huggingface.co and set HF_TOKEN "
                "(agent env or config env: block) before downloading.",
                id="pin-model-gated-note",
            )
            with Vertical(id="pin-model-preview"):
                yield Static("▸ WILL PIN", id="pin-model-preview-title")
                yield Static("", id="pin-model-preview-line")
                yield Static("", id="pin-model-preview-note")
            yield Static(
                "Ctrl+R Advanced — quant override · tokenizer · notes · "
                "detection overrides · download now",
                id="pin-model-advanced-hint",
            )
            yield Static("", id="pin-model-error")
            yield KeyHintBar(
                [
                    ("⏎", "Pin"),
                    ("Ctrl+S", "Pin"),
                    ("Ctrl+R", "Advanced"),
                    ("Tab", "Next"),
                    ("Esc", "Cancel"),
                ],
                id="pin-model-footer",
            )

    def on_mount(self) -> None:
        self._apply_disclosure()
        self._render_preview()
        self._focus_source_entry()
        self._ready = True

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.action_submit()

    def on_input_changed(self, event: Input.Changed) -> None:
        if self._ready:
            self._render_preview()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if self._ready and event.checkbox.id == "pin-model-download-now":
            self._render_preview()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.control.id != "pin-model-source":
            return
        if self._ready:
            self._apply_disclosure()
            self._render_preview()
            self._focus_source_entry()

    def action_submit(self) -> None:
        try:
            self.dismiss(self._collect_model_pin_params())
        except ValueError as exc:
            self.query_one("#pin-model-error", Static).update(str(exc))

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_toggle_advanced(self) -> None:
        self._advanced_visible = not self._advanced_visible
        self._apply_disclosure()

    def _source(self) -> str:
        return str(self.query_one("#pin-model-source", Select).value or "hf")

    def _initial_source(self) -> str:
        if self.initial_params.get("url"):
            return "url"
        if self.initial_params.get("local_path"):
            return "local"
        return "hf"

    def _apply_disclosure(self) -> None:
        source = self._source()
        visible = _SOURCE_FIELDS.get(source, _SOURCE_FIELDS["hf"])
        for key in ("repo-id", "local-path", "url", "display-name", "revision", "commit-sha"):
            self.query_one(f"#pm-{key}", Field).display = key in visible
        for key in _ADVANCED_FIELDS:
            self.query_one(f"#pm-{key}", Field).display = self._advanced_visible
        self.query_one("#pm-advanced-checks").display = self._advanced_visible
        self.query_one("#pin-model-advanced-hint").display = not self._advanced_visible

    def _focus_source_entry(self) -> None:
        entry = {
            "hf": "#pin-model-repo-id",
            "local": "#pin-model-local-path",
            "url": "#pin-model-url",
        }[self._source()]
        self.query_one(entry, Input).focus()

    def _render_preview(self) -> None:
        source = self._source()
        if source == "local":
            line = f"registry entry: {self._field_value('#pin-model-local-path') or '<path>'}"
        elif source == "url":
            url = self._field_value("#pin-model-url") or "<url>"
            line = f"registry entry: {url} (launch-time-only)"
        else:
            repo = self._field_value("#pin-model-repo-id") or "org/model"
            pin = (
                self._field_value("#pin-model-commit-sha")
                or self._field_value("#pin-model-revision")
                or "default branch"
            )
            line = f"registry entry: {repo} @ {pin}"
        self.query_one("#pin-model-preview-line", Static).update(line)
        if bool(self.query_one("#pin-model-download-now", Checkbox).value):
            note = (
                "Pins, then starts the download immediately — progress streams "
                "on the dashboard."
            )
        else:
            note = (
                "Pin only — nothing is downloaded. d in Models downloads it; "
                "deployments reference the pin by id."
            )
        self.query_one("#pin-model-preview-note", Static).update(note)

    def _initial_value(self, key: str) -> str:
        value = self.initial_params.get(key)
        return str(value) if isinstance(value, str) else ""

    def _initial_bool(self, key: str) -> bool:
        return bool(self.initial_params.get(key))

    def _field_value(self, selector: str) -> str:
        return self.query_one(selector, Input).value.strip()

    def _checked(self, selector: str) -> bool:
        return bool(self.query_one(selector, Checkbox).value)

    def _target_text(self) -> str:
        return f"target: {self.target_label}" if self.target_label else ""

    def _collect_model_pin_params(self) -> dict[str, Any]:
        source = self._source()
        fields = {
            "repo_id": self._field_value("#pin-model-repo-id") if source == "hf" else "",
            "local_path": (
                self._field_value("#pin-model-local-path") if source == "local" else ""
            ),
            "url": self._field_value("#pin-model-url") if source == "url" else "",
            "display_name": self._field_value("#pin-model-display-name"),
            "revision": self._field_value("#pin-model-revision") if source == "hf" else "",
            "commit_sha": self._field_value("#pin-model-commit-sha") if source == "hf" else "",
            "quant_format": self._field_value("#pin-model-quant-format"),
            "tokenizer": self._field_value("#pin-model-tokenizer"),
            "notes": self._field_value("#pin-model-notes"),
        }
        params: dict[str, Any] = {key: value for key, value in fields.items() if value}
        if params.get("url"):
            params["source"] = "url"
        if self._checked("#pin-model-gated"):
            params["gated"] = True
        if self._checked("#pin-model-token-required"):
            params["token_required"] = True
        # URL pins are launch-time-only — never cached, so nothing to download.
        if source != "url" and self._checked("#pin-model-download-now"):
            params["download_now"] = True
        if not params.get("repo_id") and not params.get("local_path") and not params.get("url"):
            raise ValueError(
                {
                    "hf": "Enter a repo id — the org/model path from huggingface.co.",
                    "local": "Enter the absolute path to the model on the target.",
                    "url": "Enter the direct model URL (.gguf / .safetensors).",
                }[source]
            )
        return params
