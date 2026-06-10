"""Headless tests for the refactored TargetManagerScreen (Mac-safe; no GPU/vLLM).

These pin the redesign (Figma 44:2): a side-by-side master-detail layout, a
grouped detail pane (CONNECTION / VERSIONS / PATHS / CAPABILITIES), the ~60-method
capability wall collapsed to a count + view-all, and a footer keybar — while the
list/detail substring contract the smoke suite relies on is preserved.

Uses generic placeholders (gpu-node, user@gpu-host, /home/user/...) per the
no-unique-environment rule.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.app import App
from textual.widgets import Static

from vela.config.targets import TargetConfig, TransportKind
from vela.tui.screens.target_manager import TargetManagerScreen
from vela.tui.widgets import KeyHintBar, MasterDetail


class _FakeRegistry:
    def __init__(self, targets: list[TargetConfig]) -> None:
        self._targets = targets

    @property
    def targets(self) -> list[TargetConfig]:
        return self._targets


class _Host(App):
    pass


def _make_screen(*, capabilities: list[str], active: str = "gpu-node") -> TargetManagerScreen:
    gpu_node = TargetConfig(
        name="gpu-node",
        transport=TransportKind.SSH,
        host="user@gpu-host",
        workdir=Path("/home/user/vela"),
        venv=Path("/home/user/venvs/vela"),
    )
    registry = _FakeRegistry([TargetConfig(name="local"), gpu_node])
    return TargetManagerScreen(
        registry,
        active_target=active,
        connection_state="connected",
        agent_info={
            "agent_version": "0.9.0-agent",
            "controller_version": "0.9.0-controller",
            "protocol_version": 1,
            "capabilities": capabilities,
        },
        last_seen="2026-06-09T00:00:00Z",
        active_runs=[{"config_name": "alpha"}, {"config_name": "beta"}],
        gpu_summary="Blackwell sm_120 1024/81920MB 25%",
    )


@pytest.mark.asyncio
async def test_target_manager_uses_master_detail_layout_and_footer() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = _make_screen(capabilities=["gpu", "health", "preview"])
        await app.push_screen(screen)
        await pilot.pause()
        # Side-by-side master-detail + footer keybar (Figma 44:2).
        assert len(screen.query(MasterDetail)) == 1
        assert len(screen.query(KeyHintBar)) == 1
        # The pinned panes survive as queryable Statics.
        assert screen.query_one("#target-manager-list", Static)
        assert screen.query_one("#target-manager-detail", Static)


@pytest.mark.asyncio
async def test_target_manager_detail_groups_into_sections() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = _make_screen(capabilities=["gpu", "health", "preview"])
        await app.push_screen(screen)
        await pilot.pause()
        detail = str(screen.query_one("#target-manager-detail", Static).content)
        for header in ("CONNECTION", "VERSIONS", "PATHS", "CAPABILITIES"):
            assert header in detail
        # Grouped, but the key:value contract still holds (substring-safe).
        assert "workdir: /home/user/vela" in detail
        assert "venv: /home/user/venvs/vela" in detail
        assert "connection: connected" in detail
        assert "agent: 0.9.0-agent" in detail


@pytest.mark.asyncio
async def test_target_manager_lists_small_capability_sets_inline() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = _make_screen(capabilities=["health", "gpu", "preview"])
        await app.push_screen(screen)
        await pilot.pause()
        detail = str(screen.query_one("#target-manager-detail", Static).content)
        assert "capabilities: gpu, health, preview" in detail


@pytest.mark.asyncio
async def test_target_manager_collapses_large_capability_walls() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        caps = [f"rpc_method_{n:02d}" for n in range(40)]
        screen = _make_screen(capabilities=caps)
        await app.push_screen(screen)
        await pilot.pause()
        detail = str(screen.query_one("#target-manager-detail", Static).content)
        # The screenshot-#2 fix: a count + view-all, not a 40-name wall.
        assert "40 supported" in detail
        assert "view all" in detail
        assert "rpc_method_00" not in detail


@pytest.mark.asyncio
async def test_target_manager_view_all_expands_and_collapses_capabilities() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        caps = [f"rpc_method_{n:02d}" for n in range(40)]
        screen = _make_screen(capabilities=caps)
        await app.push_screen(screen)
        await pilot.pause()
        detail = str(screen.query_one("#target-manager-detail", Static).content)
        # The affordance names a key that actually works.
        assert "v view all" in detail
        assert any(key == "v" for key, *_ in TargetManagerScreen.BINDINGS)
        screen.action_view_capabilities()
        await pilot.pause()
        detail = str(screen.query_one("#target-manager-detail", Static).content)
        assert "rpc_method_00" in detail
        assert "rpc_method_39" in detail
        assert "v collapse" in detail
        screen.action_view_capabilities()
        await pilot.pause()
        detail = str(screen.query_one("#target-manager-detail", Static).content)
        assert "rpc_method_00" not in detail
        assert "v view all" in detail


@pytest.mark.asyncio
async def test_target_manager_list_row_format_preserved() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = _make_screen(capabilities=["gpu", "health"])
        await app.push_screen(screen)
        await pilot.pause()
        target_list = str(screen.query_one("#target-manager-list", Static).content)
        assert "Target Manager" in target_list
        # Exact list-row format (marker · dot · name · transport · host).
        assert "> ● gpu-node  ssh  user@gpu-host" in target_list
