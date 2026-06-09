"""Headless tests for the refactored AdoptBuildScreen (Mac-safe; no GPU/vLLM).

Pins the redesign (Figma 52:2): a ValidationCard (auto-detected stack), Field-wrapped
inputs, and a WILL-DO preview — while the dismiss-payload contract (venv_path required,
copy flag from the checkbox) is preserved.
"""

from __future__ import annotations

import pytest
from textual.app import App
from textual.widgets import Checkbox, Input

from vela.tui.screens.adopt_build import AdoptBuildScreen
from vela.tui.widgets import Field, ValidationCard


class _Host(App):
    pass


@pytest.mark.asyncio
async def test_adopt_build_shows_validation_card() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = AdoptBuildScreen()
        await app.push_screen(screen)
        await pilot.pause()
        assert len(screen.query(ValidationCard)) == 1


@pytest.mark.asyncio
async def test_adopt_build_wraps_inputs_in_field_widgets() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = AdoptBuildScreen()
        await app.push_screen(screen)
        await pilot.pause()
        assert len(screen.query(Field)) >= 3


@pytest.mark.asyncio
async def test_adopt_build_shows_will_do_preview() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = AdoptBuildScreen()
        await app.push_screen(screen)
        await pilot.pause()
        assert screen.query_one("#adopt-build-preview")


@pytest.mark.asyncio
async def test_adopt_build_collect_params_contract_preserved() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = AdoptBuildScreen()
        await app.push_screen(screen)
        await pilot.pause()
        screen.query_one("#adopt-build-venv-path", Input).value = "/home/user/venvs/vllm-nightly"
        screen.query_one("#adopt-build-label", Input).value = "vllm-nightly"
        screen.query_one("#adopt-build-copy", Checkbox).value = True
        params = screen._collect_adopt_build_params()
        assert params["venv_path"] == "/home/user/venvs/vllm-nightly"
        assert params["label"] == "vllm-nightly"
        assert params["copy"] == "true"
