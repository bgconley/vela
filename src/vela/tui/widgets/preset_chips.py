"""``PresetChips`` — a row of selectable preset chips.

Maps to the Figma Component Kit (node ``61:2``): compact pills, the selected one
highlighted in cyan. Used for things like Download Model's file-pattern presets
(safetensors only / everything / no pickle) with an "advanced (raw)" escape hatch.
"""

from __future__ import annotations

from collections.abc import Iterable

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Label

from vela.tui.theme import BG_FIELD, CYAN, SURFACE_CYAN, TEXT_SECONDARY


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

    def __init__(
        self,
        options: Iterable[str],
        *,
        selected: int = 0,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self._options = list(options)
        self._selected = selected

    def compose(self) -> ComposeResult:
        for index, option in enumerate(self._options):
            classes = "preset-chip selected" if index == self._selected else "preset-chip"
            yield Label(option, classes=classes)
