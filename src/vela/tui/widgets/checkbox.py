"""Vela's unambiguous terminal checkbox control."""

from __future__ import annotations

from textual.content import Content
from textual.widgets import Checkbox as TextualCheckbox


class Checkbox(TextualCheckbox):
    """Render literal off/on glyphs while retaining Textual toggle semantics.

    Textual's stock toggle always renders an ``X`` and makes it disappear in the
    off state by painting it the same color as its background.  That produces a
    solid block in Vela's palette, so the state is ambiguous.  Rendering the
    state explicitly also makes the visual contract available to headless tests.
    """

    @property
    def _button(self) -> Content:
        glyph = "[✓]" if self.value else "[ ]"
        return Content.from_text(glyph, markup=False).stylize_before(
            self.get_visual_style("toggle--button")
        )
