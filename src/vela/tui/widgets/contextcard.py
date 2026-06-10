"""``ContextCard`` — a read-only "what you're operating on" card.

Maps to the Figma Component Kit (node ``61:2``): a raised, bordered card with a
dim heading and ``key  value`` rows. Visually distinct from editable fields (no
caret, raised background) so it reads as context, not input.
"""

from __future__ import annotations

from collections.abc import Iterable

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Label, Static

from vela.tui.theme import BG_RAISED, BORDER_SUBTLE, TEXT_FAINT, TEXT_PRIMARY


class ContextCard(Vertical):
    """A raised, read-only card: a heading plus ``key value`` rows."""

    DEFAULT_CSS = f"""
    ContextCard {{
        height: auto;
        background: {BG_RAISED};
        border: round {BORDER_SUBTLE};
        padding: 1 2;
        margin-bottom: 1;
    }}
    ContextCard .context-card-heading {{ color: {TEXT_FAINT}; text-style: bold; }}
    ContextCard .context-row {{ height: 1; }}
    ContextCard .context-key {{ width: 16; color: {TEXT_FAINT}; }}
    ContextCard .context-value {{ width: 1fr; color: {TEXT_PRIMARY}; }}
    """

    def __init__(
        self,
        heading: str,
        rows: Iterable[tuple[str, str]],
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self._heading = heading
        self._rows = list(rows)

    def compose(self) -> ComposeResult:
        yield Static(self._heading, classes="context-card-heading")
        for key, value in self._rows:
            with Horizontal(classes="context-row"):
                yield Label(key, classes="context-key")
                yield Label(value, classes="context-value")
