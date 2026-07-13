"""Headless tests for TargetEditScreen's shared modal frame (Task 4.4, bug-237).

The panel adopts the shared 4.1 modal frame (MODAL_PANEL_CSS) in place of the
fixed ``width: 96`` box that overflowed the 80-col screen. Frame only — the raw
``key=value`` input language stays (Phase 4.8 may revisit the form).

Uses generic placeholders (gpu-node, user@gpu-host) per the no-unique-environment
rule.
"""

from __future__ import annotations

import pytest
from textual.app import App
from textual.css.scalar import Unit
from textual.widgets import Input

from vela.config.targets import TargetConfig, TransportKind
from vela.tui.screens.target_edit import TargetEditScreen


class _Host(App):
    pass


def test_target_edit_uses_only_canonical_palette_tokens() -> None:
    import vela.tui.screens.target_edit as target_edit_module

    for legacy in ("BAD", "TEXT"):
        assert not hasattr(target_edit_module, legacy)
    for canonical in ("RED", "TEXT_PRIMARY"):
        assert hasattr(target_edit_module, canonical)


@pytest.mark.asyncio
async def test_target_edit_panel_uses_shared_frame_and_fits_at_80x24() -> None:
    # bug-237: the panel adopts the shared frame; the fixed `width: 96` box
    # overflowed the 80-col screen.
    app = _Host()
    async with app.run_test(size=(80, 24)) as pilot:
        screen = TargetEditScreen()
        await app.push_screen(screen)
        await pilot.pause()
        await pilot.pause()
        panel = screen.query_one("#target-edit-panel")
        # 4.1 idiom: percentage width/height, never fixed cells (Unit.WIDTH not CELLS).
        assert panel.styles.width.unit == Unit.WIDTH
        assert panel.styles.height.is_auto
        assert panel.styles.max_height.unit == Unit.HEIGHT
        assert panel.styles.overflow_y == "auto"
        # The whole panel fits within the 80-col terminal, centered (region.x > 0)
        # — a fixed width: 96 box overflows it.
        assert panel.region.x > 0
        assert panel.region.right <= 80
        assert panel.region.width >= 0.9 * 80
        # The input renders inside the panel (no horizontal clip).
        field = screen.query_one("#target-edit-input", Input)
        assert panel.region.x <= field.region.x
        assert field.region.right <= panel.region.right


@pytest.mark.asyncio
async def test_target_edit_preserves_raw_key_value_contract() -> None:
    # Task 4.4 keeps the raw key=value input language untouched (frame only).
    app = _Host()
    async with app.run_test() as pilot:
        target = TargetConfig(
            name="gpu-node", transport=TransportKind.SSH, host="user@gpu-host"
        )
        screen = TargetEditScreen(target)
        await app.push_screen(screen)
        await pilot.pause()
        field = screen.query_one("#target-edit-input", Input)
        # A pre-filled edit shows the raw `key=value` form (not a Field-per-key form).
        assert "name=gpu-node" in field.value
        assert "transport=ssh" in field.value
        assert "host=user@gpu-host" in field.value
        # The placeholder still teaches the key=value language.
        assert "transport=ssh" in field.placeholder
