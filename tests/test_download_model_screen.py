"""Headless tests for the refactored DownloadModelScreen (Mac-safe; no GPU/vLLM).

Pins the redesign (Figma 50:2): a read-only ContextCard, a revision-override input
with a *true* hint (not the commit sha as a ghost placeholder), preset file-pattern
chips, and a WILL-DOWNLOAD preview — while the dismiss-payload contract is preserved.
"""

from __future__ import annotations

import pytest
from textual.app import App
from textual.widgets import Input

from vela.tui.screens.download_model import DownloadModelScreen
from vela.tui.widgets import ContextCard, PresetChips

MODEL = {
    "model_ref": "Qwen/Qwen3.6-27B-FP8",
    "label": "Qwen3.6-27B-FP8",
    "repo_id": "Qwen/Qwen3.6-27B-FP8",
    "commit_sha": "2f9a1c7",
    "cache_state": "cached",
}


class _Host(App):
    pass


@pytest.mark.asyncio
async def test_download_model_shows_readonly_context_card() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = DownloadModelScreen(MODEL)
        await app.push_screen(screen)
        await pilot.pause()
        assert len(screen.query(ContextCard)) == 1


@pytest.mark.asyncio
async def test_download_model_revision_uses_true_hint_not_ghost_sha() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = DownloadModelScreen(MODEL)
        await app.push_screen(screen)
        await pilot.pause()
        placeholder = screen.query_one("#download-model-revision", Input).placeholder or ""
        # The pinned sha must NOT masquerade as a pre-filled value...
        assert MODEL["commit_sha"] not in placeholder
        # ...it's a real hint instead.
        assert "blank" in placeholder.lower()


@pytest.mark.asyncio
async def test_download_model_has_preset_chips_for_patterns() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = DownloadModelScreen(MODEL)
        await app.push_screen(screen)
        await pilot.pause()
        assert len(screen.query(PresetChips)) >= 1


@pytest.mark.asyncio
async def test_download_model_shows_will_download_preview() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = DownloadModelScreen(MODEL)
        await app.push_screen(screen)
        await pilot.pause()
        assert screen.query_one("#download-model-preview")


@pytest.mark.asyncio
async def test_download_model_collect_params_contract_preserved() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = DownloadModelScreen(MODEL)
        await app.push_screen(screen)
        await pilot.pause()
        screen.query_one("#download-model-revision", Input).value = "abc123"
        params = screen._collect_download_params()
        assert params["model_ref"] == "Qwen/Qwen3.6-27B-FP8"
        assert params["revision"] == "abc123"
