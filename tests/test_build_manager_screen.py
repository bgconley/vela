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


@pytest.mark.asyncio
async def test_build_manager_empty_state_names_first_actions() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = BuildManagerScreen({"builds": []})
        await app.push_screen(screen)
        await pilot.pause()
        content = str(screen.query_one("#build-manager-list", Static).content)
        assert "n create" in content
        assert "a adopt" in content


@pytest.mark.asyncio
async def test_build_manager_explains_select_semantics() -> None:
    # J7: the list pane carries the one line that explains what Enter does.
    app = _Host()
    async with app.run_test() as pilot:
        screen = _make_screen()
        await app.push_screen(screen)
        await pilot.pause()
        content = str(screen.query_one("#build-manager-list", Static).content)
        assert "sets the default build" in content
        assert "pinned" in content.lower()


@pytest.mark.asyncio
async def test_build_manager_active_build_names_unpinned_default() -> None:
    # J8: the active build's detail states it is the default for every
    # config without a pin — not just the explicitly pinned ones.
    app = _Host()
    async with app.run_test() as pilot:
        screen = _make_screen()
        await app.push_screen(screen)
        await pilot.pause()
        detail = str(screen.query_one("#build-manager-detail", Static).content)
        assert "all unpinned configs" in detail


@pytest.mark.asyncio
async def test_build_manager_focus_build_selects_named_build() -> None:
    # J5 support: the manager can open focused on a freshly created build.
    app = _Host()
    async with app.run_test() as pilot:
        screen = BuildManagerScreen(
            {
                "builds": [
                    {"label": "stable-cu124", "build_id": "b1", "default": True},
                    {"label": "nightly-cu130", "build_id": "b2"},
                ]
            },
            focus_build="nightly-cu130",
        )
        await app.push_screen(screen)
        await pilot.pause()
        assert screen.selected_index == 1
