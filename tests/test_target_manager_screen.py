"""Headless tests for the refactored TargetManagerScreen (Mac-safe; no GPU/vLLM).

Task 4.2 rebuilt the manager (bug-237): the two-pane MasterDetail squeezed into a
fixed ``width: 100`` box (clipped past 80 cols, jumped as you arrowed) is replaced
by the shared 4.1 modal frame + a full-width list-in-a-``VerticalScroll`` STACKED
ABOVE the detail (the Flag Manager precedent). The manager also gained live state
(``refresh_target_state``) so a completed Reconnect flips the frozen snapshot in
place, plus optimistic ``reconnecting…`` feedback. The grouped detail
(CONNECTION / VERSIONS / PATHS / CAPABILITIES), the collapsed capability wall, and
the list/detail substring contract the smoke suite relies on are all preserved.

Uses generic placeholders (gpu-node, user@gpu-host, /home/user/...) per the
no-unique-environment rule.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.app import App
from textual.containers import VerticalScroll
from textual.css.scalar import Unit
from textual.widgets import Label, Static

from vela.config.targets import TargetConfig, TransportKind
from vela.tui.screens.target_manager import _FOOTER_HINTS, TargetManagerScreen
from vela.tui.widgets import KeyHintBar, MasterDetail


class _FakeRegistry:
    def __init__(self, targets: list[TargetConfig]) -> None:
        self._targets = targets

    @property
    def targets(self) -> list[TargetConfig]:
        return self._targets


class _Host(App):
    pass


class _ReconnectHost(App):
    """Minimal host that records the app-level reconnect the screen delegates to."""

    def __init__(self) -> None:
        super().__init__()
        self.reconnect_calls = 0

    def action_reconnect(self) -> None:
        self.reconnect_calls += 1


def _make_screen(
    *,
    capabilities: list[str],
    active: str = "gpu-node",
    connection_state: str = "connected",
) -> TargetManagerScreen:
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
        connection_state=connection_state,
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


# ── Layout rebuild (bullet 1/2) ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_target_manager_uses_stacked_full_width_layout_and_footer() -> None:
    # UPDATED (was test_..._uses_master_detail_layout_and_footer): Task 4.2 DROPS
    # the side-by-side MasterDetail for a full-width list-in-a-VerticalScroll
    # stacked above the detail. The pinned #target-manager-list / -detail Statics
    # + a KeyHintBar footer survive; only the container changed.
    app = _Host()
    async with app.run_test(size=(80, 24)) as pilot:
        screen = _make_screen(capabilities=["gpu", "health", "preview"])
        await app.push_screen(screen)
        await pilot.pause()
        assert len(screen.query(MasterDetail)) == 0  # the cramped two-pane is gone
        assert len(screen.query(VerticalScroll)) == 1  # list scroll region
        assert len(screen.query(KeyHintBar)) >= 1  # footer keybar(s)
        # The pinned panes survive as queryable Statics.
        assert screen.query_one("#target-manager-list", Static)
        assert screen.query_one("#target-manager-detail", Static)


@pytest.mark.asyncio
async def test_target_manager_panel_uses_shared_frame_and_stacks_list() -> None:
    # Bullet 1: the panel uses the 4.1 frame (percentage width, height auto,
    # percentage max-height, scroll) and the list is a VerticalScroll stacked
    # ABOVE the detail with the scroll kept out of the Tab order.
    app = _Host()
    async with app.run_test(size=(80, 24)) as pilot:
        screen = _make_screen(capabilities=["gpu"])
        await app.push_screen(screen)
        await pilot.pause()
        await pilot.pause()
        panel = screen.query_one("#target-manager-panel")
        # 4.1 idiom: NEVER is_percent (False on resolved styles) — check units.
        assert panel.styles.width.unit == Unit.WIDTH
        assert panel.styles.height.is_auto
        assert panel.styles.max_height.unit == Unit.HEIGHT
        assert panel.styles.overflow_y == "auto"
        scroll = screen.query_one("#target-manager-list-scroll", VerticalScroll)
        detail = screen.query_one("#target-manager-detail", Static)
        assert scroll.region.y < detail.region.y  # list STACKED ABOVE detail
        assert scroll.can_focus is False  # out of the Tab order (on_mount)


@pytest.mark.asyncio
async def test_target_manager_fits_without_clipping_at_both_sizes() -> None:
    # Bullet 1: at 80x24 and 140x40 nothing clips — the panel is >=90% of the
    # terminal width and the footer's LAST hint (Esc Close) renders inside the
    # panel region (docked so it survives a long, scrolling detail).
    for width, height in ((80, 24), (140, 40)):
        app = _Host()
        async with app.run_test(size=(width, height)) as pilot:
            caps = [f"rpc_method_{n:02d}" for n in range(40)]
            screen = _make_screen(capabilities=caps)
            await app.push_screen(screen)
            await pilot.pause()
            await pilot.pause()
            panel = screen.query_one("#target-manager-panel")
            assert panel.region.width >= 0.9 * width  # never the clipped fixed box
            footer = screen.query_one("#target-manager-footer")
            close = next(
                lab for lab in footer.query(Label) if str(lab.render()) == "Close"
            )
            region = close.region
            assert panel.region.x <= region.x and region.right <= panel.region.right
            assert panel.region.y <= region.y and region.bottom <= panel.region.bottom


@pytest.mark.asyncio
async def test_target_manager_panel_stays_stable_across_selection() -> None:
    # Bullet 2: arrowing between targets must NOT resize/jump the panel. The
    # frame's max-height bounds the panel so the region is identical across a
    # selection change (short inactive detail <-> long active detail).
    app = _Host()
    async with app.run_test(size=(80, 24)) as pilot:
        caps = [f"rpc_method_{n:02d}" for n in range(40)]
        screen = _make_screen(capabilities=caps)
        await app.push_screen(screen)
        await pilot.pause()
        await pilot.pause()
        panel = screen.query_one("#target-manager-panel")
        before = panel.region
        screen.action_previous()  # active gpu-node -> inactive local (short detail)
        await pilot.pause()
        after_prev = panel.region
        screen.action_next()  # back to gpu-node (long detail)
        await pilot.pause()
        after_next = panel.region
        assert before == after_prev == after_next


# ── Live state + reconnect feedback (bullet 3/4) ────────────────────────────


@pytest.mark.asyncio
async def test_target_manager_refresh_target_state_rerenders_detail_and_dot() -> None:
    # Bullet 3: refresh_target_state re-renders the detail AND the active
    # target's list dot from a fresh payload, in place (screen stays open).
    app = _Host()
    async with app.run_test() as pilot:
        screen = _make_screen(capabilities=["gpu"], connection_state="disconnected")
        await app.push_screen(screen)
        await pilot.pause()
        detail = str(screen.query_one("#target-manager-detail", Static).content)
        assert "connection: disconnected" in detail
        target_list = str(screen.query_one("#target-manager-list", Static).content)
        assert "> ○ gpu-node" in target_list  # disconnected dot on the active row
        screen.refresh_target_state(
            {
                "connection_state": "connected",
                "agent_info": {
                    "agent_version": "1.0.0-agent",
                    "capabilities": ["gpu", "health"],
                },
                "active_runs": [{"config_name": "gamma"}],
            }
        )
        await pilot.pause()
        detail = str(screen.query_one("#target-manager-detail", Static).content)
        assert "connection: connected" in detail
        assert "agent: 1.0.0-agent" in detail
        assert "active_runs: 1 (gamma)" in detail
        target_list = str(screen.query_one("#target-manager-list", Static).content)
        assert "> ● gpu-node" in target_list  # connected dot after the live refresh
        assert app.screen is screen  # never closed


@pytest.mark.asyncio
async def test_target_manager_reconnect_shows_feedback_then_live_state() -> None:
    # Bullet 4: pressing R renders `reconnecting…` in the detail's connection row
    # immediately (before the worker resolves) and delegates to the app; the live
    # refresh then replaces it once the app's reconnect completes.
    app = _ReconnectHost()
    async with app.run_test() as pilot:
        screen = _make_screen(capabilities=["gpu"])
        await app.push_screen(screen)
        await pilot.pause()
        # Moment 1: immediate optimistic feedback + delegation to the app.
        screen.action_reconnect()
        await pilot.pause()
        assert app.reconnect_calls == 1
        detail = str(screen.query_one("#target-manager-detail", Static).content)
        assert "connection: reconnecting…" in detail
        # Moment 2: the app's completion path pushes fresh live state back in.
        screen.refresh_target_state(
            {"connection_state": "connected", "agent_info": {"agent_version": "1.0.0-agent"}}
        )
        await pilot.pause()
        detail = str(screen.query_one("#target-manager-detail", Static).content)
        assert "connection: connected" in detail
        assert "agent: 1.0.0-agent" in detail


# ── Footer verb set (bullet 5) ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_target_manager_footer_advertises_full_verb_set() -> None:
    # Bullet 5: `v view all` is added to _FOOTER_HINTS and the whole set renders
    # (the packed KeyHintBar rows' union is the full, ordered set incl. Esc Close).
    app = _Host()
    async with app.run_test(size=(80, 24)) as pilot:
        screen = _make_screen(capabilities=["gpu"])
        await app.push_screen(screen)
        await pilot.pause()
        rendered = [pair for bar in screen.query(KeyHintBar) for pair in bar._hints]
        assert ("v", "view all") in rendered
        assert ("Esc", "Close") in rendered
        assert rendered == _FOOTER_HINTS  # full set, order preserved across rows


# ── Preserved detail / list contract (bullet 6 — pinned, unchanged) ─────────


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
