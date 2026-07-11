"""``KeyHintBar`` — renders footer keybinding hints as ``key label`` pairs.

Maps to the Figma footer keybar (Component Kit ``61:2``): a bold cyan key followed
by a dim label, repeated per hint. Screens pass an explicit list of
``(key, label)`` pairs so the bar stays decoupled from any one screen's bindings.
"""

from __future__ import annotations

from collections.abc import Iterable

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Label

from vela.tui.theme import CYAN, TEXT_SECONDARY


class KeyHintBar(Horizontal):
    """A horizontal row of ``key label`` hint pairs."""

    DEFAULT_CSS = f"""
    KeyHintBar {{ height: 1; }}
    KeyHintBar .keyhint {{ width: auto; height: 1; margin-right: 2; }}
    KeyHintBar .keyhint-key {{ color: {CYAN}; text-style: bold; }}
    KeyHintBar .keyhint-label {{ color: {TEXT_SECONDARY}; margin-left: 1; }}
    """

    def __init__(
        self,
        hints: Iterable[tuple[str, str]],
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self._hints = list(hints)

    def compose(self) -> ComposeResult:
        for key, label in self._hints:
            with Horizontal(classes="keyhint"):
                yield Label(key, classes="keyhint-key")
                yield Label(label, classes="keyhint-label")


def hint_row_width(hint: tuple[str, str]) -> int:
    """Rendered width of one ``KeyHintBar`` pair.

    key + 1-col gap + label + the widget's 2-col right margin. Matches the
    ``KeyHintBar`` TCSS so :func:`pack_hint_rows` can keep every packed row
    inside a panel's content region.
    """
    key, label = hint
    return len(key) + 1 + len(label) + 2


def pack_hint_rows(
    hints: list[tuple[str, str]], *, max_width: int = 68
) -> list[list[tuple[str, str]]]:
    """Pack footer hints into as few rows as fit within ``max_width``.

    The stacked manager layout (Task 4.2/4.3, bug-237) gives the vertical room
    for a second footer row, so the full verb set always renders without
    clipping at 80 cols instead of the old single row that ran off the panel's
    right edge. The default cap fits the 80-col panel content region; both the
    Target Manager and Model Manager consume this shared packer.
    """
    rows: list[list[tuple[str, str]]] = []
    row: list[tuple[str, str]] = []
    used = 0
    for hint in hints:
        width = hint_row_width(hint)
        if row and used + width > max_width:
            rows.append(row)
            row, used = [], 0
        row.append(hint)
        used += width
    if row:
        rows.append(row)
    return rows
