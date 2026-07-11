"""Headless tests for the reframed HelpScreen (Task 4.4, bug-237).

Help adopts the shared 4.1 modal frame, is retitled for humans
(``HelpScreen - bindings + palette hint`` -> ``Help — keys & markers``), packs
its Markers legend to the panel content width so label-value pairs wrap whole
(no orphaned glyphs), and migrates its legacy ACCENT/GOOD/WARN/PURPLE color
tokens to the canonical theme.py set. Every text substring except the title is
preserved.
"""

from __future__ import annotations

import pytest
from rich.cells import cell_len
from rich.text import Text
from textual.app import App
from textual.css.scalar import Unit
from textual.widgets import Static

from vela.tui.screens.help import _MARKER_PAIRS, HelpScreen
from vela.tui.theme import PURPLE, VIOLET


class _Host(App):
    pass


def _uses_style(text: Text, fragment: str) -> bool:
    if text.style and fragment in str(text.style):
        return True
    return any(fragment in str(span.style) for span in text.spans)


def _marker_lines(plain: str) -> list[str]:
    lines = plain.split("\n")
    start = next(i for i, ln in enumerate(lines) if ln.startswith("Markers:"))
    out = [lines[start]]
    for ln in lines[start + 1 :]:
        if ln.startswith(" "):  # indented continuation line
            out.append(ln)
        else:
            break
    return out


@pytest.mark.asyncio
async def test_help_panel_uses_shared_frame_and_fits_at_80x24() -> None:
    # bug-237: the panel adopts the shared frame; the fixed `width: 82` box
    # overflowed the 80-col screen.
    app = _Host()
    async with app.run_test(size=(80, 24)) as pilot:
        screen = HelpScreen()
        await app.push_screen(screen)
        await pilot.pause()
        await pilot.pause()
        panel = screen.query_one("#help-panel")
        assert panel.styles.width.unit == Unit.WIDTH
        assert panel.styles.height.is_auto
        assert panel.styles.max_height.unit == Unit.HEIGHT
        assert panel.styles.overflow_y == "auto"
        # The whole panel fits within the 80-col terminal, centered.
        assert panel.region.x > 0
        assert panel.region.right <= 80
        assert panel.region.width >= 0.9 * 80


@pytest.mark.asyncio
async def test_help_retitled_for_humans() -> None:
    # Retitle: "HelpScreen - bindings + palette hint" -> "Help — keys & markers".
    app = _Host()
    async with app.run_test(size=(80, 24)) as pilot:
        screen = HelpScreen()
        await app.push_screen(screen)
        await pilot.pause()
        content = str(screen.query_one("#help-text", Static).content)
        assert "Help — keys & markers" in content
        assert "HelpScreen - bindings + palette hint" not in content
        assert "HelpScreen" not in content


@pytest.mark.asyncio
async def test_help_markers_legend_wraps_whole_pairs() -> None:
    # bug-237: the Markers legend is packed to the panel content width so a
    # glyph/label pair never wraps mid-pair (orphaning the glyph at line end).
    app = _Host()
    async with app.run_test(size=(80, 24)) as pilot:
        screen = HelpScreen()
        await app.push_screen(screen)
        await pilot.pause()
        await pilot.pause()  # let on_resize settle the real width
        content_width = screen._content_width()
        content = str(screen.query_one("#help-text", Static).content)
        markers = _marker_lines(content)
        # The legend actually wrapped (more than one line) at 80 cols.
        assert len(markers) >= 2
        # No line is wide enough to force a soft-wrap that would orphan a glyph.
        for line in markers:
            assert cell_len(line) <= content_width
        # Every pair survives whole (glyph + label contiguous on one line).
        joined = "\n".join(markers)
        for glyph, label in _MARKER_PAIRS:
            pair = f"{glyph} {label}"
            assert pair in joined
            assert any(pair in line for line in markers)
        # The pinned smoke contract substrings are still present.
        assert "📌" in content and "pinned" in content
        assert "🔒" in content


@pytest.mark.asyncio
async def test_help_colors_migrated_off_legacy_purple() -> None:
    # The legacy PURPLE token (#b48cff) is migrated to the canonical VIOLET
    # (#b69cf0). Visual-only; every text substring is preserved.
    app = _Host()
    async with app.run_test(size=(80, 24)) as pilot:
        screen = HelpScreen()
        await app.push_screen(screen)
        await pilot.pause()
        help_text = screen.query_one("#help-text", Static).content
        assert isinstance(help_text, Text)
        assert _uses_style(help_text, VIOLET)  # migrated token in use
        assert not _uses_style(help_text, PURPLE)  # legacy token gone


@pytest.mark.asyncio
async def test_help_action_pills_dock_to_panel_bottom() -> None:
    # bug-237 (4.4 carry-forward): dock the action pills at the panel bottom (the
    # three-screen proven pattern) so they stay visible at 80x24 instead of
    # scrolling off the bottom of a tall help panel.
    app = _Host()
    async with app.run_test(size=(80, 24)) as pilot:
        screen = HelpScreen()
        await app.push_screen(screen)
        await pilot.pause()
        await pilot.pause()
        pills = screen.query_one("#help-actions", Static)
        assert pills.styles.dock == "bottom"
        # The pills sit within the visible terminal, not pushed below the fold.
        assert pills.region.height > 0
        assert pills.region.bottom <= 24
