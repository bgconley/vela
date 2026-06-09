"""``StepIndicator`` — the shared wizard step breadcrumb.

Renders the wizard steps with done (green ✓), current (cyan ▸ bold), and future
(faint) states, separated by arrows. Maps to the Figma New Deployment wizard's
shared step indicator (``56:2``–``58:68``). Built on ``Static`` (renders Rich
``Text``) so callers can read ``.content`` and re-mark the current step with
``set_current()`` as the user navigates.
"""

from __future__ import annotations

from collections.abc import Iterable

from rich.text import Text
from textual.widgets import Static

from vela.tui.theme import CYAN, GREEN, TEXT_FAINT


class StepIndicator(Static):
    DEFAULT_CSS = "StepIndicator { height: auto; }"

    def __init__(
        self, steps: Iterable[str], *, current: int = 0, id: str | None = None
    ) -> None:
        self._steps = list(steps)
        self._current = current
        super().__init__(self._build_text(), id=id)

    def set_current(self, index: int) -> None:
        self._current = index
        self.update(self._build_text())

    def _build_text(self) -> Text:
        text = Text()
        last = len(self._steps) - 1
        for index, label in enumerate(self._steps):
            if index < self._current:
                text.append(f"✓ {label}", style=GREEN)
            elif index == self._current:
                text.append(f"▸ {label}", style=f"bold {CYAN}")
            else:
                text.append(label, style=TEXT_FAINT)
            if index != last:
                text.append("  →  ", style=TEXT_FAINT)
        return text
