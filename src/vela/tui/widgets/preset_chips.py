"""``PresetChips`` — a row of selectable preset chips.

Maps to the Figma Component Kit (node ``61:2``): compact pills, the selected one
highlighted in cyan. Used for things like Download Model's file-pattern presets
(safetensors only / everything / no pickle) with an "advanced (raw)" escape hatch.

Chips are clickable. A click (or a programmatic :meth:`select`) posts
:class:`PresetChips.Selected` so the owning screen can apply the preset;
:meth:`highlight` moves the visual selection silently for derived state (e.g.
when raw inputs are edited to match — or stop matching — a preset).
"""

from __future__ import annotations

from collections.abc import Iterable

from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.message import Message
from textual.widgets import Label

from vela.tui.theme import BG_FIELD, CYAN, SURFACE_CYAN, TEXT_SECONDARY


class _Chip(Label):
    """One clickable chip; clicks delegate to the owning ``PresetChips``."""

    def __init__(self, text: str, *, index: int, classes: str | None = None) -> None:
        super().__init__(text, classes=classes)
        self.index = index

    def on_click(self, event: events.Click) -> None:
        event.stop()
        parent = self.parent
        if isinstance(parent, PresetChips):
            parent.select(self.index)


class PresetChips(Horizontal):
    """A horizontal row of preset chips; the ``selected`` index is highlighted."""

    DEFAULT_CSS = f"""
    PresetChips {{ height: 1; }}
    PresetChips .preset-chip {{
        height: 1;
        padding: 0 2;
        margin-right: 1;
        background: {BG_FIELD};
        color: {TEXT_SECONDARY};
    }}
    PresetChips .preset-chip.selected {{
        background: {SURFACE_CYAN};
        color: {CYAN};
        text-style: bold;
    }}
    """

    class Selected(Message):
        """The user picked a chip (by click or :meth:`PresetChips.select`)."""

        def __init__(self, chips: PresetChips, index: int, option: str) -> None:
            super().__init__()
            self.chips = chips
            self.index = index
            self.option = option

        @property
        def control(self) -> PresetChips:
            return self.chips

    def __init__(
        self,
        options: Iterable[str],
        *,
        selected: int | None = 0,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self._options = list(options)
        self._selected: int | None = selected

    @property
    def selected(self) -> int | None:
        return self._selected

    def compose(self) -> ComposeResult:
        for index, option in enumerate(self._options):
            classes = f"preset-chip chip-{index}"
            if index == self._selected:
                classes += " selected"
            yield _Chip(option, index=index, classes=classes)

    def select(self, index: int) -> None:
        """User-intent selection: move the highlight and notify listeners."""
        self.highlight(index)
        self.post_message(self.Selected(self, index, self._options[index]))

    def highlight(self, index: int | None) -> None:
        """Move (or clear) the visual selection without posting a message."""
        self._selected = index
        for chip in self.query(_Chip):
            chip.set_class(chip.index == index, "selected")
