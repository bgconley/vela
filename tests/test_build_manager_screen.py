"""Headless tests for the refactored BuildManagerScreen (Phase 6 consistency pass).

Brings the screen into the shared master-detail language (MasterDetail + Rich Text
color + KeyHintBar) while preserving the list/detail substring contract.
"""

from __future__ import annotations

import pytest
from textual.app import App
from textual.widgets import Static

from vela.tui.screens.build_manager import BuildManagerScreen
from vela.tui.widgets import KeyHintBar, MasterDetail


class _Host(App):
    pass


def _make_screen() -> BuildManagerScreen:
    return BuildManagerScreen(
        {
            "builds": [
                {
                    "label": "stable-cu124",
                    "build_id": "b1",
                    "status": "ready",
                    "default": True,
                    "install": {"method": "nightly", "source": "cu130"},
                    "resolved": {"vllm": "0.11.2", "cuda": "12.4"},
                    "paths": {"executable": "/opt/vllm/bin/vllm"},
                    "live_refs": [{"run_id": "run-live"}],
                    "config_refs": ["buildable", "canary"],
                },
                {"label": "nightly-cu130", "build_id": "b2", "status": "ready"},
            ]
        }
    )


@pytest.mark.asyncio
async def test_build_manager_uses_master_detail_and_footer() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = _make_screen()
        await app.push_screen(screen)
        await pilot.pause()
        assert len(screen.query(MasterDetail)) == 1
        assert len(screen.query(KeyHintBar)) == 1


@pytest.mark.asyncio
async def test_build_manager_preserves_list_and_detail_contract() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = _make_screen()
        await app.push_screen(screen)
        await pilot.pause()
        build_list = str(screen.query_one("#build-manager-list", Static).content)
        detail = str(screen.query_one("#build-manager-detail", Static).content)
        assert "Build Manager" in build_list
        assert "> ● stable-cu124  ready  ● active  🔒 in use  ⇩ used by 2 configs" in build_list
        assert "source: nightly/cu130" in detail
        assert "in_use: 1 live run (run-live)" in detail
        assert "used_by_configs: 2 (buildable, canary)" in detail
