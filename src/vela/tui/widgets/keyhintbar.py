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
