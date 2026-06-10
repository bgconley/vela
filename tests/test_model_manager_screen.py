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


@pytest.mark.asyncio
async def test_model_manager_empty_state_names_first_action() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = ModelManagerScreen({"models": []})
        await app.push_screen(screen)
        await pilot.pause()
        content = str(screen.query_one("#model-manager-list", Static).content)
        assert "press p" in content


@pytest.mark.asyncio
async def test_model_manager_auth_row_names_where_to_set_hf_token() -> None:
    # J18: every HF_TOKEN mention says WHERE the token lives.
    app = _Host()
    async with app.run_test() as pilot:
        screen = ModelManagerScreen(
            {
                "models": [
                    {
                        "entry_id": "gated-pin",
                        "display_name": "gated-pin",
                        "repo_id": "org/gated",
                        "cache_state": "remote_only",
                        "gated": True,
                    }
                ]
            }
        )
        await app.push_screen(screen)
        await pilot.pause()
        detail = str(screen.query_one("#model-manager-detail", Static).content)
        assert "gated, requires HF_TOKEN" in detail
        assert "agent env or config env" in detail


@pytest.mark.asyncio
async def test_model_manager_shows_used_by_configs() -> None:
    # J17: pin-impact data — which configs reference this model.
    app = _Host()
    async with app.run_test() as pilot:
        screen = ModelManagerScreen(
            {
                "models": [
                    {
                        "entry_id": "llama-pin",
                        "display_name": "llama-pin",
                        "repo_id": "org/llama",
                        "cache_state": "cached",
                        "config_refs": ["alpha", "beta"],
                    }
                ]
            }
        )
        await app.push_screen(screen)
        await pilot.pause()
        detail = str(screen.query_one("#model-manager-detail", Static).content)
        assert "used_by: 2 (alpha, beta)" in detail


@pytest.mark.asyncio
async def test_model_manager_focus_model_selects_named_entry() -> None:
    # J16 support: the manager can open focused on a freshly pinned model.
    app = _Host()
    async with app.run_test() as pilot:
        screen = ModelManagerScreen(
            {
                "models": [
                    {"entry_id": "first", "display_name": "first"},
                    {"entry_id": "second-pin", "display_name": "second-pin"},
                ]
            },
            focus_model="second-pin",
        )
        await app.push_screen(screen)
        await pilot.pause()
        assert screen.selected_index == 1
