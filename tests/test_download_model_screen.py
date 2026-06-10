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
from vela.tui.widgets import ContextCard, KeyHintBar, PresetChips

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
async def test_download_model_preset_selection_fills_pattern_inputs() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = DownloadModelScreen(MODEL)
        await app.push_screen(screen)
        await pilot.pause()
        chips = screen.query_one("#download-model-presets", PresetChips)
        # No patterns on the model → truthfully highlights "everything".
        assert chips.selected == 1
        chips.select(0)
        await pilot.pause()
        assert screen.query_one("#download-model-allow", Input).value == "*.safetensors *.json"
        assert screen.query_one("#download-model-ignore", Input).value == "*.bin *.pth"
        params = screen._collect_download_params()
        assert params["allow_patterns"] == ["*.safetensors", "*.json"]
        assert params["ignore_patterns"] == ["*.bin", "*.pth"]


@pytest.mark.asyncio
async def test_download_model_manual_pattern_edit_clears_stale_chip_highlight() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = DownloadModelScreen(MODEL)
        await app.push_screen(screen)
        await pilot.pause()
        screen.query_one("#download-model-ignore", Input).value = "custom-*"
        await pilot.pause()
        chips = screen.query_one("#download-model-presets", PresetChips)
        assert chips.selected is None


@pytest.mark.asyncio
async def test_download_model_raw_patterns_collapse_behind_real_toggle() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = DownloadModelScreen(MODEL)
        await app.push_screen(screen)
        await pilot.pause()
        # Patterns match a preset → raw fields start collapsed behind the chips.
        assert screen.query_one("#dm-allow").display is False
        assert screen.query_one("#dm-ignore").display is False
        screen.action_toggle_raw()
        assert screen.query_one("#dm-allow").display is True
        assert screen.query_one("#dm-ignore").display is True
        # The footer advertises only keys that actually work.
        hints = screen.query_one("#download-model-footer", KeyHintBar)._hints
        labels = [label for _, label in hints]
        assert "Override revision" not in labels
        assert ("Ctrl+R", "Raw patterns") in hints


@pytest.mark.asyncio
async def test_download_model_custom_patterns_start_expanded_with_no_chip() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = DownloadModelScreen({**MODEL, "ignore_patterns": ["custom-*"]})
        await app.push_screen(screen)
        await pilot.pause()
        # Unrecognized patterns must stay visible — hiding them would hide config.
        assert screen.query_one("#dm-allow").display is True
        assert screen.query_one("#dm-ignore").display is True
        chips = screen.query_one("#download-model-presets", PresetChips)
        assert chips.selected is None


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


@pytest.mark.asyncio
async def test_download_model_gated_access_names_token_location() -> None:
    # J18: the gated-access row says where HF_TOKEN lives.
    app = _Host()
    async with app.run_test() as pilot:
        screen = DownloadModelScreen({**MODEL, "gated": True})
        await app.push_screen(screen)
        await pilot.pause()
        access = dict(screen._card_rows()).get("access", "")
        assert "HF_TOKEN" in access
        assert "agent env or config env" in access
