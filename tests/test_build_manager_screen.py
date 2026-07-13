"""Headless tests for the rebuilt BuildManagerScreen (Task 4.4, bug-237).

Task 4.4 rebuilds the manager the way 4.2/4.3 rebuilt Target/Model: the two-pane
``MasterDetail`` squeezed into a fixed ``width: 96`` box (clipped past 80 cols, and
whose single-row footer clipped its own last hint to ``Esc Clos``) is replaced by
the shared 4.1 modal frame + a full-width list-in-a-``VerticalScroll`` STACKED ABOVE
the detail, with the footer packed into as many rows as fit (``pack_hint_rows``) and
docked so its last hint always renders inside the panel. The empty state now
advertises only the applicable verbs (``n New  a Adopt  Esc Close``). The list-row
and detail ``key: value`` substring contract the smoke suite relies on is preserved
verbatim — only the layout changed.
"""

from __future__ import annotations

import pytest
from textual.app import App
from textual.containers import VerticalScroll
from textual.css.scalar import Unit
from textual.widgets import Label, Static

from vela.tui.screens.build_manager import (
    _FOOTER_HINTS,
    BuildManagerScreen,
    _build_action_payload,
    _build_reference,
)
from vela.tui.widgets import KeyHintBar


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


def test_build_manager_actions_use_immutable_id_not_mutable_label() -> None:
    first = {"build_id": "01BUILD-A", "label": "nightly", "paths": {}}
    replacement = {"build_id": "01BUILD-B", "label": "nightly", "paths": {}}

    assert _build_reference(first) == "01BUILD-A"
    assert _build_reference(replacement) == "01BUILD-B"
    assert _build_action_payload("verify_build", replacement)["build"] == "01BUILD-B"
    assert _build_reference({"label": "legacy-label"}) == "legacy-label"


# ── Layout rebuild ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_manager_uses_stacked_full_width_layout_and_footer() -> None:
    # Task 4.4 DROPS the side-by-side MasterDetail for a full-width
    # list-in-a-VerticalScroll stacked above the detail. The pinned
    # #build-manager-list / -detail Statics + KeyHintBar footer(s) survive.
    app = _Host()
    async with app.run_test(size=(80, 24)) as pilot:
        screen = _make_screen()
        await app.push_screen(screen)
        await pilot.pause()
        # The cramped two-pane widget was deleted outright in Phase-9; the
        # full-width list-in-a-VerticalScroll is the positive guard now.
        assert len(screen.query(VerticalScroll)) == 1  # list scroll region
        assert len(screen.query(KeyHintBar)) >= 1  # footer keybar(s)
        assert screen.query_one("#build-manager-list", Static)
        assert screen.query_one("#build-manager-detail", Static)


@pytest.mark.asyncio
async def test_build_manager_panel_uses_shared_frame_and_stacks_list() -> None:
    # The panel uses the 4.1 frame (percentage width, height auto, percentage
    # max-height, scroll) and the list is a VerticalScroll stacked ABOVE the
    # detail with the scroll kept out of the Tab order.
    app = _Host()
    async with app.run_test(size=(80, 24)) as pilot:
        screen = _make_screen()
        await app.push_screen(screen)
        await pilot.pause()
        await pilot.pause()
        panel = screen.query_one("#build-manager-panel")
        # 4.1 idiom: NEVER is_percent (False on resolved styles) — check units.
        assert panel.styles.width.unit == Unit.WIDTH
        assert panel.styles.height.is_auto
        assert panel.styles.max_height.unit == Unit.HEIGHT
        assert panel.styles.overflow_y == "auto"
        scroll = screen.query_one("#build-manager-list-scroll", VerticalScroll)
        detail = screen.query_one("#build-manager-detail", Static)
        assert scroll.region.y < detail.region.y  # list STACKED ABOVE detail
        assert scroll.can_focus is False  # out of the Tab order (on_mount)


@pytest.mark.asyncio
async def test_build_manager_fits_without_clipping_at_80x24() -> None:
    # bug-237: at 80x24 nothing clips — the panel is >=90% of the terminal width
    # and the footer's LAST hint (Esc Close) renders inside the panel region
    # (docked so it survives a long, scrolling detail). The old single-row
    # footer clipped this very hint to "Esc Clos".
    app = _Host()
    async with app.run_test(size=(80, 24)) as pilot:
        screen = _make_screen()
        await app.push_screen(screen)
        await pilot.pause()
        await pilot.pause()
        panel = screen.query_one("#build-manager-panel")
        # The whole panel fits within the 80-col terminal — a fixed `width: 96`
        # box overflows the screen (bug-237), the percentage frame never does.
        assert panel.region.x >= 0
        assert panel.region.right <= 80
        assert panel.region.width >= 0.9 * 80  # still near-full-width
        footer = screen.query_one("#build-manager-footer")
        close = next(lab for lab in footer.query(Label) if str(lab.render()) == "Close")
        region = close.region
        assert panel.region.x <= region.x and region.right <= panel.region.right
        assert panel.region.y <= region.y and region.bottom <= panel.region.bottom


@pytest.mark.asyncio
async def test_build_manager_empty_state_footer_shows_only_applicable_hints() -> None:
    # bug-237: with no builds you cannot Select/Verify/Repair/Pin/Remove anything,
    # so the footer advertises only n New / a Adopt / Esc Close. A populated
    # manager keeps the full verb set.
    app = _Host()
    async with app.run_test(size=(80, 24)) as pilot:
        empty = BuildManagerScreen({"builds": []})
        await app.push_screen(empty)
        await pilot.pause()
        empty_hints = [pair for bar in empty.query(KeyHintBar) for pair in bar._hints]
        assert empty_hints == [("n", "New"), ("a", "Adopt"), ("Esc", "Close")]
        assert ("v", "Verify") not in empty_hints
        assert ("x", "Remove") not in empty_hints

    app2 = _Host()
    async with app2.run_test(size=(80, 24)) as pilot:
        full = _make_screen()
        await app2.push_screen(full)
        await pilot.pause()
        full_hints = [pair for bar in full.query(KeyHintBar) for pair in bar._hints]
        assert full_hints == _FOOTER_HINTS  # full set, order preserved across rows
        assert ("v", "Verify") in full_hints
        assert ("x", "Remove") in full_hints
        assert ("Esc", "Close") in full_hints


# ── Preserved list / detail contract (pinned, unchanged rows) ───────────────


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
    # J8: the active build's detail states it is the default for every config
    # without a pin — not just the explicitly pinned ones.
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
