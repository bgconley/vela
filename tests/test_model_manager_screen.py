"""Headless tests for the refactored ModelManagerScreen (Phase 6 consistency pass).

Brings the screen into the shared master-detail language (MasterDetail + Rich Text
color + KeyHintBar) while preserving the list/detail substring contract the smoke
suite relies on.
"""

from __future__ import annotations

import pytest
from textual.app import App
from textual.widgets import Static

from vela.tui.screens.model_manager import ModelManagerScreen
from vela.tui.widgets import KeyHintBar, MasterDetail


class _Host(App):
    pass


def _make_screen() -> ModelManagerScreen:
    return ModelManagerScreen(
        {
            "models": [
                {
                    "entry_id": "llama-pin",
                    "display_name": "llama-pin",
                    "cache_state": "cached",
                    "quant_format": "awq",
                    "commit_sha": "abc123",
                    "revision": "main",
                    "gated": True,
                    "token_required": True,
                    "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
                    "unique_size_bytes": 2_100_000_000,
                    "nominal_size_bytes": 16_100_000_000,
                    "files": {"count": 7, "weights_format": "safetensors"},
                },
                {
                    "entry_id": "qwen-remote",
                    "display_name": "qwen-remote",
                    "cache_state": "remote_only",
                    "quant_format": "bf16",
                    "revision": "main",
                },
            ]
        }
    )


@pytest.mark.asyncio
async def test_model_manager_uses_master_detail_and_footer() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = _make_screen()
        await app.push_screen(screen)
        await pilot.pause()
        assert len(screen.query(MasterDetail)) == 1
        assert len(screen.query(KeyHintBar)) == 1


@pytest.mark.asyncio
async def test_model_manager_preserves_list_and_detail_contract() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = _make_screen()
        await app.push_screen(screen)
        await pilot.pause()
        model_list = str(screen.query_one("#model-manager-list", Static).content)
        detail = str(screen.query_one("#model-manager-detail", Static).content)
        assert "Model Manager" in model_list
        assert "llama-pin" in model_list
        assert "awq" in model_list
        assert "@abc123" in model_list
        assert "repo: meta-llama/Llama-3.1-8B-Instruct" in detail
        assert "revision: main → abc123" in detail
        assert "auth: gated, requires HF_TOKEN" in detail
        assert "files: 7 safetensors" in detail
