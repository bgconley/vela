"""Headless tests for the refactored CreateBuildScreen (Mac-safe; no GPU/vLLM).

These pin the redesign (Figma 49:2): inputs wrapped in `Field` widgets, progressive
disclosure by method, and a WILL-RUN preview — while the dismiss-payload contract
consumed by app.py is preserved.
"""

from __future__ import annotations

import pytest
from textual.app import App
from textual.widgets import Input

from vela.tui.screens.create_build import CreateBuildScreen
from vela.tui.widgets import Field


class _Host(App):
    pass


@pytest.mark.asyncio
async def test_create_build_wraps_inputs_in_field_widgets() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = CreateBuildScreen(
            target_label="gpu-node", uv_available=True, initial={"method": "nightly"}
        )
        await app.push_screen(screen)
        await pilot.pause()
        assert len(screen.query(Field)) >= 4


@pytest.mark.asyncio
async def test_create_build_nightly_hides_irrelevant_fields() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = CreateBuildScreen(
            target_label="gpu-node", uv_available=True, initial={"method": "nightly"}
        )
        await app.push_screen(screen)
        await pilot.pause()
        # Progressive disclosure: nightly shows label/channel/python/env...
        for key in ("label", "channel", "python", "env"):
            assert screen.query_one(f"#cb-{key}", Field).display is True
        # ...and hides the fields that belong to other methods.
        for key in ("spec", "commit", "url", "path"):
            assert screen.query_one(f"#cb-{key}", Field).display is False


@pytest.mark.asyncio
async def test_create_build_shows_will_run_preview() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = CreateBuildScreen(
            target_label="gpu-node",
            uv_available=True,
            initial={"method": "nightly", "channel": "cu130"},
        )
        await app.push_screen(screen)
        await pilot.pause()
        assert screen.query_one("#create-build-preview")  # element exists
        assert screen._preview_command.startswith("uv pip install --pre vllm")


@pytest.mark.asyncio
async def test_create_build_collect_params_contract_preserved() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = CreateBuildScreen(
            target_label="gpu-node", uv_available=True, initial={"method": "nightly"}
        )
        await app.push_screen(screen)
        await pilot.pause()
        screen.query_one("#create-build-label", Input).value = "nightly-cu130"
        screen.query_one("#create-build-channel", Input).value = "cu130"
        screen.query_one("#create-build-python", Input).value = "3.12"
        params = screen._collect_build_params()
        assert params == {
            "method": "nightly",
            "label": "nightly-cu130",
            "channel": "cu130",
            "python": "3.12",
        }
