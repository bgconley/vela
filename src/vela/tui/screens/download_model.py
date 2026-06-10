from __future__ import annotations

import shlex
from typing import Any

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static

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
from vela.tui.widgets import ContextCard, Field, KeyHintBar, PresetChips


class DownloadModelScreen(ModalScreen[dict[str, Any] | None]):
    CSS = f"""
    DownloadModelScreen {{
        align: center middle;
        background: {BG_BASE};
    }}
    #download-model-panel {{
        width: 80;
        max-height: 90%;
        overflow-y: auto;
        border: round {BORDER_STRONG};
        background: {BG_PANEL};
        padding: 1 2;
    }}
    #download-model-title {{ color: {CYAN}; text-style: bold; margin-bottom: 1; }}
    .dm-section-label {{ color: {TEXT_SECONDARY}; text-style: bold; margin-top: 1; }}
    #download-model-files-help {{ color: {TEXT_FAINT}; height: auto; }}
    #download-model-preview {{
        border: round {BORDER_SUBTLE};
        background: {BG_INSET};
        padding: 1 2;
        margin-top: 1;
        height: auto;
    }}
    #download-model-preview-title {{ color: {TEXT_FAINT}; }}
    #download-model-preview-cmd {{ color: {GREEN}; height: auto; }}
    #download-model-preview-note {{ color: {TEXT_SECONDARY}; height: auto; }}
    #download-model-error {{ color: {RED}; height: auto; margin-top: 1; }}
    #download-model-footer {{ margin-top: 1; }}
    """

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        # Ctrl+R (not a bare letter): printable keys are consumed by whichever
        # Input has focus, so a bare `a`/`r` hotkey could never fire on a form.
        ("ctrl+r", "toggle_raw", "Raw patterns"),
    ]

    # (chip label, allow patterns, ignore patterns)
    _PRESETS: list[tuple[str, str, str]] = [
        ("safetensors only", "*.safetensors *.json", "*.bin *.pth"),
        ("everything", "", ""),
        ("no pickle", "", "*.bin *.pt *.pth *.pickle"),
    ]

    def __init__(self, model: dict[str, Any]) -> None:
        super().__init__(id="download-model")
        self.model = dict(model)
        # Raw pattern fields start collapsed behind the chips only when the
        # model's patterns match a preset; unrecognized patterns stay visible.
        self._raw_visible = (
            self._match_preset(
                self._patterns_value("allow_patterns"),
                self._patterns_value("ignore_patterns"),
            )
            is None
        )

    def compose(self) -> ComposeResult:
        with Vertical(id="download-model-panel"):
            yield Static("Download Model", id="download-model-title")
            yield ContextCard("MODEL · read-only", self._card_rows())
            yield Field(
                "Revision",
                Input(
                    placeholder=self._revision_placeholder(),
                    id="download-model-revision",
                ),
                helper=(
                    "Downloads the pinned commit for reproducibility. "
                    "Paste a branch / tag / sha to override."
                ),
                id="dm-revision",
            )
            yield Static("Files", classes="dm-section-label")
            yield PresetChips(
                [name for name, _, _ in self._PRESETS],
                selected=self._match_preset(
                    self._patterns_value("allow_patterns"),
                    self._patterns_value("ignore_patterns"),
                ),
                id="download-model-presets",
            )
            yield Static(
                "safetensors only = *.safetensors *.json (skips .bin / .pth). "
                "Ctrl+R shows raw patterns for fine control.",
                id="download-model-files-help",
            )
            yield Field(
                "Allow patterns (raw)",
                Input(
                    value=self._patterns_value("allow_patterns"),
                    placeholder="*.safetensors *.json",
                    id="download-model-allow",
                ),
                helper="Space-separated globs to include.",
                id="dm-allow",
            )
            yield Field(
                "Ignore patterns (raw)",
                Input(
                    value=self._patterns_value("ignore_patterns"),
                    placeholder="*.bin *.pth",
                    id="download-model-ignore",
                ),
                helper="Space-separated globs to exclude.",
                id="dm-ignore",
            )
            with Vertical(id="download-model-preview"):
                yield Static("▸ WILL DOWNLOAD", id="download-model-preview-title")
                yield Static(self._download_summary(), id="download-model-preview-cmd")
                yield Static(
                    "Fetches missing shards into the target's HF cache; already-cached "
                    "files are skipped. Nothing is launched.",
                    id="download-model-preview-note",
                )
            yield Static("", id="download-model-error")
            yield KeyHintBar(
                [
                    ("⏎", "Download"),
                    ("Ctrl+R", "Raw patterns"),
                    ("Esc", "Cancel"),
                ],
                id="download-model-footer",
            )

    def on_mount(self) -> None:
        self._apply_raw_visibility()
        self.query_one("#download-model-revision", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.action_submit()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id in {"download-model-allow", "download-model-ignore"}:
            self.query_one("#download-model-presets", PresetChips).highlight(
                self._match_preset(
                    self._field_value("#download-model-allow"),
                    self._field_value("#download-model-ignore"),
                )
            )

    def on_preset_chips_selected(self, event: PresetChips.Selected) -> None:
        _, allow, ignore = self._PRESETS[event.index]
        self.query_one("#download-model-allow", Input).value = allow
        self.query_one("#download-model-ignore", Input).value = ignore

    def action_toggle_raw(self) -> None:
        self._raw_visible = not self._raw_visible
        self._apply_raw_visibility()

    def _apply_raw_visibility(self) -> None:
        self.query_one("#dm-allow").display = self._raw_visible
        self.query_one("#dm-ignore").display = self._raw_visible

    @classmethod
    def _match_preset(cls, allow: str, ignore: str) -> int | None:
        for index, (_, preset_allow, preset_ignore) in enumerate(cls._PRESETS):
            if _patterns_from_input(allow) == _patterns_from_input(
                preset_allow
            ) and _patterns_from_input(ignore) == _patterns_from_input(preset_ignore):
                return index
        return None

    def action_submit(self) -> None:
        try:
            self.dismiss(self._collect_download_params())
        except ValueError as exc:
            self.query_one("#download-model-error", Static).update(str(exc))

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _card_rows(self) -> list[tuple[str, str]]:
        rows: list[tuple[str, str]] = [
            ("repo", str(self.model.get("repo_id") or _model_ref(self.model) or "-")),
        ]
        sha = self.model.get("commit_sha") or self.model.get("revision")
        if sha:
            rows.append(("pinned", f"{sha}  ✓ immutable"))
        rows.append(("cache", str(self.model.get("cache_state") or "unknown")))
        if self.model.get("gated") or self.model.get("token_required"):
            rows.append(
                ("access", "needs HF_TOKEN on the target (agent env or config env: block)")
            )
        else:
            rows.append(("access", "public · no token required"))
        return rows

    def _download_summary(self) -> str:
        ref = _model_ref(self.model)
        cache = self.model.get("cache_state") or "unknown"
        return f"snapshot of {ref} → the target's HF cache (currently: {cache})"

    def _revision_placeholder(self) -> str:
        return "leave blank to keep the pinned commit"

    def _field_value(self, selector: str) -> str:
        return self.query_one(selector, Input).value.strip()

    def _patterns_value(self, field: str) -> str:
        value = self.model.get(field)
        if not isinstance(value, list):
            return ""
        return " ".join(str(item) for item in value if isinstance(item, str) and item)

    def _collect_download_params(self) -> dict[str, Any]:
        model_ref = _model_ref(self.model)
        if not model_ref:
            raise ValueError("Selected model has no model_ref")
        params: dict[str, Any] = {"model_ref": model_ref}
        revision = self._field_value("#download-model-revision")
        allow_patterns = _patterns_from_input(self._field_value("#download-model-allow"))
        ignore_patterns = _patterns_from_input(self._field_value("#download-model-ignore"))
        if revision:
            params["revision"] = revision
        if allow_patterns:
            params["allow_patterns"] = allow_patterns
        if ignore_patterns:
            params["ignore_patterns"] = ignore_patterns
        return params


def _model_label(model: dict[str, Any]) -> str:
    return str(model.get("label") or model.get("display_name") or model.get("entry_id") or "")


def _model_ref(model: dict[str, Any]) -> str:
    return str(model.get("model_ref") or model.get("entry_id") or model.get("display_name") or "")


def _patterns_from_input(value: str) -> list[str]:
    if not value:
        return []
    tokens: list[str] = []
    for token in shlex.split(value):
        tokens.extend(part.strip() for part in token.split(",") if part.strip())
    return tokens
