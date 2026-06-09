"""``ValidationCard`` — a green/red live-validation result card.

Maps to the Figma Component Kit (node ``61:2``). Used where a value is detected /
checked rather than typed (e.g. Adopt Build importing vLLM from a venv path): a
green card on success, red on failure, with an optional detail line and dim note.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from vela.tui.theme import GREEN, RED, SURFACE_GREEN, SURFACE_RED, TEXT_FAINT, TEXT_PRIMARY


class ValidationCard(Vertical):
    """A success/failure card: ``✓/✗ heading`` + optional detail + note."""

    DEFAULT_CSS = f"""
    ValidationCard {{
        height: auto;
        padding: 1 2;
        margin-bottom: 1;
        border: round {GREEN};
        background: {SURFACE_GREEN};
    }}
    ValidationCard.-bad {{ border: round {RED}; background: {SURFACE_RED}; }}
    ValidationCard .validation-heading {{ color: {GREEN}; text-style: bold; }}
    ValidationCard.-bad .validation-heading {{ color: {RED}; }}
    ValidationCard .validation-detail {{ color: {TEXT_PRIMARY}; height: auto; }}
    ValidationCard .validation-note {{ color: {TEXT_FAINT}; height: auto; }}
    """

    def __init__(
        self,
        ok: bool,
        heading: str,
        detail: str = "",
        note: str = "",
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        base = f"validation-card {'-ok' if ok else '-bad'}"
        if classes:
            base = f"{base} {classes}"
        super().__init__(name=name, id=id, classes=base)
        self._ok = ok
        self._heading = heading
        self._detail = detail
        self._note = note

    def compose(self) -> ComposeResult:
        marker = "✓" if self._ok else "✗"
        yield Static(f"{marker} {self._heading}", classes="validation-heading")
        if self._detail:
            yield Static(self._detail, classes="validation-detail")
        if self._note:
            yield Static(self._note, classes="validation-note")
