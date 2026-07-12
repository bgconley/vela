"""Headless tests for the rebuilt ModelManagerScreen (Task 4.3, bug-237).

The manager is rebuilt full-width: the old ``width: 104`` two-pane MasterDetail
(which clipped even at 100 cols and wrapped every list row into interleaved SHA
fragments) is replaced by the shared 4.1 modal frame + a full-width list in a
``VerticalScroll`` STACKED ABOVE the detail (the Task 4.2 Target Manager
precedent). The list rows gain a scannable one-line grammar

    {dot} {display_name…} {source_tag} {cache_state} {size} {sha8}

that truncates the name to prevent wrapping, shows ``—`` for metadata-only /
zero-weight caches (never ``0.0 GB``), keeps only the 8-char short sha in the
row (full sha stays in the detail), and drops ``sha8`` then ``size`` as the
terminal narrows. Pinned entries are visually distinct from HF-cache-scan rows
via the shared source-tag palette. The detail ``key: value`` contract is
preserved and gains a ``pinned:`` line.
"""

from __future__ import annotations

import pytest
from rich.cells import cell_len
from textual.app import App
from textual.containers import VerticalScroll
from textual.css.scalar import Unit
from textual.widgets import Label, Static

from vela.tui.screens.model_manager import (
    _FOOTER_HINTS,
    ModelManagerScreen,
    _row_source_tag,
)
from vela.tui.theme import AMBER, CYAN, VIOLET
from vela.tui.widgets import KeyHintBar
from vela.tui.widgets.keyhintbar import pack_hint_rows


class _Host(App):
    pass


def _make_screen(focus_model: str | None = None) -> ModelManagerScreen:
    return ModelManagerScreen(
        {
            "models": [
                {
                    "entry_id": "llama-pin",
                    "display_name": "llama-pin",
                    "source": "hf_repo",
                    "pinned": True,
                    "cache_state": "cached",
                    "quant_format": "awq",
                    "commit_sha": "0123456789abcdef",
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
                    "source": "hf_repo",
                    "pinned": False,
                    "cache_state": "remote_only",
                    "quant_format": "bf16",
                    "revision": "main",
                    "commit_sha": None,
                    "size_bytes": 0,
                    "files": {},
                },
            ]
        },
        focus_model=focus_model,
    )


# ── Layout rebuild (mirror Task 4.2) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_model_manager_uses_stacked_full_width_layout_and_footer() -> None:
    # The cramped two-pane MasterDetail is dropped for a full-width list in a
    # VerticalScroll stacked above the detail; the pinned Statics + KeyHintBar
    # footer survive, only the container changed.
    app = _Host()
    async with app.run_test(size=(80, 24)) as pilot:
        screen = _make_screen()
        await app.push_screen(screen)
        await pilot.pause()
        # The cramped two-pane widget was deleted outright in Phase-9; the
        # full-width list-in-a-VerticalScroll is the positive guard now.
        assert len(screen.query(VerticalScroll)) == 1
        assert len(screen.query(KeyHintBar)) >= 1
        assert screen.query_one("#model-manager-list", Static)
        assert screen.query_one("#model-manager-detail", Static)


@pytest.mark.asyncio
async def test_model_manager_panel_uses_shared_frame_and_stacks_list() -> None:
    app = _Host()
    async with app.run_test(size=(80, 24)) as pilot:
        screen = _make_screen()
        await app.push_screen(screen)
        await pilot.pause()
        await pilot.pause()
        panel = screen.query_one("#model-manager-panel")
        # 4.1 idiom: NEVER is_percent (False on resolved styles) — check units.
        assert panel.styles.width.unit == Unit.WIDTH
        assert panel.styles.height.is_auto
        assert panel.styles.max_height.unit == Unit.HEIGHT
        assert panel.styles.overflow_y == "auto"
        scroll = screen.query_one("#model-manager-list-scroll", VerticalScroll)
        detail = screen.query_one("#model-manager-detail", Static)
        assert scroll.region.y < detail.region.y  # list STACKED ABOVE detail
        assert scroll.can_focus is False  # out of the Tab order (on_mount)


@pytest.mark.asyncio
async def test_model_manager_fits_without_clipping_at_both_sizes() -> None:
    for width, height in ((80, 24), (140, 40)):
        app = _Host()
        async with app.run_test(size=(width, height)) as pilot:
            screen = _make_screen()
            await app.push_screen(screen)
            await pilot.pause()
            await pilot.pause()
            panel = screen.query_one("#model-manager-panel")
            assert panel.region.width >= 0.9 * width  # never the clipped fixed box
            footer = screen.query_one("#model-manager-footer")
            close = next(
                lab for lab in footer.query(Label) if str(lab.render()) == "Close"
            )
            region = close.region
            assert panel.region.x <= region.x and region.right <= panel.region.right
            assert panel.region.y <= region.y and region.bottom <= panel.region.bottom


@pytest.mark.asyncio
async def test_model_manager_panel_stays_stable_across_selection() -> None:
    app = _Host()
    async with app.run_test(size=(80, 24)) as pilot:
        screen = _make_screen()
        await app.push_screen(screen)
        await pilot.pause()
        await pilot.pause()
        panel = screen.query_one("#model-manager-panel")
        before = panel.region
        screen.action_next()  # llama-pin -> qwen-remote (shorter detail)
        await pilot.pause()
        after_next = panel.region
        screen.action_previous()  # back to llama-pin (longer detail)
        await pilot.pause()
        after_prev = panel.region
        assert before == after_next == after_prev


@pytest.mark.asyncio
async def test_model_manager_footer_packs_via_shared_packer() -> None:
    # The footer reuses the hoisted shared packer; the union of the packed
    # KeyHintBar rows is the full ordered verb set, and there are exactly as
    # many bars as pack_hint_rows produces.
    app = _Host()
    async with app.run_test(size=(80, 24)) as pilot:
        screen = _make_screen()
        await app.push_screen(screen)
        await pilot.pause()
        bars = list(screen.query(KeyHintBar))
        rendered = [pair for bar in bars for pair in bar._hints]
        assert rendered == _FOOTER_HINTS
        assert len(bars) == len(pack_hint_rows(_FOOTER_HINTS))
        assert ("Esc", "Close") in rendered


# ── Row grammar (test-first; row substrings CHANGE) ─────────────────────────


@pytest.mark.asyncio
async def test_model_manager_row_grammar_is_one_scannable_line() -> None:
    app = _Host()
    async with app.run_test(size=(140, 40)) as pilot:
        screen = _make_screen()
        await app.push_screen(screen)
        await pilot.pause()
        await pilot.pause()
        list_widget = screen.query_one("#model-manager-list", Static)
        model_list = str(list_widget.content)
        assert "Model Manager" in model_list
        # New grammar: {dot} {name} {source_tag} {cache_state} {size} {sha8}
        assert "● llama-pin  hf  cached  2.1 GB  01234567" in model_list
        assert "○ qwen-remote  hf  remote_only  —  main" in model_list
        # 8-char short sha only; the full sha lives in the DETAIL pane, not rows.
        assert "0123456789abcdef" not in model_list
        # No wrapping: every rendered row fits within the list content width.
        width = list_widget.size.width
        assert width > 0
        assert all(cell_len(line) <= width for line in model_list.splitlines())
        detail = str(screen.query_one("#model-manager-detail", Static).content)
        assert "0123456789abcdef" in detail  # full sha survives in the detail


@pytest.mark.asyncio
async def test_model_manager_row_size_reads_honestly() -> None:
    # `—` for zero/metadata-only weights (never `0.0 GB`), `<0.1 GB` for
    # small-but-real, GB for real weights.
    app = _Host()
    async with app.run_test(size=(140, 40)) as pilot:
        screen = ModelManagerScreen(
            {
                "models": [
                    {
                        "entry_id": "meta-only",
                        "display_name": "meta-only",
                        "source": "hf_repo",
                        "cache_state": "partial",
                        "size_bytes": 480_000,  # a few metadata files
                        "files": {"count": 2, "weights_format": "unknown"},
                    },
                    {
                        "entry_id": "tiny-real",
                        "display_name": "tiny-real",
                        "source": "hf_repo",
                        "cache_state": "cached",
                        "unique_size_bytes": 50_000_000,  # 0.05 GB, real weights
                        "files": {"count": 2, "weights_format": "safetensors"},
                    },
                    {
                        "entry_id": "big-real",
                        "display_name": "big-real",
                        "source": "hf_repo",
                        "cache_state": "cached",
                        "unique_size_bytes": 2_100_000_000,
                        "files": {"count": 7, "weights_format": "safetensors"},
                    },
                ]
            }
        )
        await app.push_screen(screen)
        await pilot.pause()
        await pilot.pause()
        model_list = str(screen.query_one("#model-manager-list", Static).content)
        assert "meta-only  hf  partial  —" in model_list
        assert "0.0 GB" not in model_list
        assert "tiny-real  hf  cached  <0.1 GB" in model_list
        assert "big-real  hf  cached  2.1 GB" in model_list


@pytest.mark.asyncio
async def test_model_manager_row_drops_sha_then_size_as_width_narrows() -> None:
    async def row_for(width: int) -> str:
        app = _Host()
        async with app.run_test(size=(width, 30)) as pilot:
            screen = _make_screen()
            await app.push_screen(screen)
            await pilot.pause()
            await pilot.pause()
            return str(screen.query_one("#model-manager-list", Static).content)

    wide = await row_for(120)  # >= 100: full row (size + sha8)
    assert "cached  2.1 GB  01234567" in wide

    mid = await row_for(90)  # < 100: sha8 dropped, size kept
    assert "cached  2.1 GB" in mid
    assert "01234567" not in mid

    narrow = await row_for(70)  # < 80: size dropped too
    assert "01234567" not in narrow
    assert "2.1 GB" not in narrow
    assert "llama-pin  hf  cached" in narrow


@pytest.mark.asyncio
async def test_model_manager_truncates_long_display_name_with_ellipsis() -> None:
    long_name = "org/" + "a-really-long-model-repository-name-that-would-wrap" * 2
    app = _Host()
    async with app.run_test(size=(80, 24)) as pilot:
        screen = ModelManagerScreen(
            {
                "models": [
                    {
                        "entry_id": long_name,
                        "display_name": long_name,
                        "source": "hf_repo",
                        "cache_state": "cached",
                        "unique_size_bytes": 2_100_000_000,
                        "files": {"count": 7, "weights_format": "safetensors"},
                    }
                ]
            }
        )
        await app.push_screen(screen)
        await pilot.pause()
        await pilot.pause()
        list_widget = screen.query_one("#model-manager-list", Static)
        model_list = str(list_widget.content)
        assert "…" in model_list  # truncated
        assert long_name not in model_list  # never rendered in full
        width = list_widget.size.width
        assert all(cell_len(line) <= width for line in model_list.splitlines())


@pytest.mark.asyncio
async def test_model_manager_pinned_rows_distinct_from_cache_scan_rows() -> None:
    # Pinned entries reuse the source-tag palette (cyan modeled / violet
    # passthrough / amber unknown) so they read differently from HF-cache-scan
    # rows.
    pinned_hf = {"source": "hf_repo", "pinned": True}
    scan_hf = {"source": "hf_repo", "pinned": False}
    url_pin = {"source": "url", "pinned": True}
    assert str(_row_source_tag(pinned_hf).style) == CYAN
    assert str(_row_source_tag(scan_hf).style) == AMBER
    assert str(_row_source_tag(url_pin).style) == VIOLET
    # missing `pinned` fails open to pinned=True (matches _is_pinned_entry).
    assert str(_row_source_tag({"source": "hf_repo"}).style) == CYAN


# ── Preserved detail / list contract + new pinned line ──────────────────────


@pytest.mark.asyncio
async def test_model_manager_preserves_detail_contract_and_adds_pinned() -> None:
    app = _Host()
    async with app.run_test(size=(140, 40)) as pilot:
        screen = _make_screen()
        await app.push_screen(screen)
        await pilot.pause()
        model_list = str(screen.query_one("#model-manager-list", Static).content)
        detail = str(screen.query_one("#model-manager-detail", Static).content)
        assert "Model Manager" in model_list
        assert "llama-pin" in model_list
        # Detail key:value substrings are preserved.
        assert "repo: meta-llama/Llama-3.1-8B-Instruct" in detail
        assert "revision: main → 0123456789abcdef" in detail
        assert "auth: gated, requires HF_TOKEN" in detail
        assert "files: 7 safetensors" in detail
        # The 2.6 pinned field is user-relevant here.
        assert "pinned: yes" in detail
        # Carry-forward (4.3 review): the quant detail line had no pin.
        assert "quant: awq" in detail


@pytest.mark.asyncio
async def test_model_manager_detail_marks_unpinned_cache_scan() -> None:
    app = _Host()
    async with app.run_test(size=(140, 40)) as pilot:
        screen = _make_screen()
        await app.push_screen(screen)
        await pilot.pause()
        screen.action_next()  # qwen-remote is pinned=False
        await pilot.pause()
        detail = str(screen.query_one("#model-manager-detail", Static).content)
        assert "pinned: no" in detail


@pytest.mark.asyncio
async def test_model_manager_row_ellipsizes_non_sha_revision() -> None:
    # Carry-forward (4.3 review): _sha8 must ellipsize a non-sha ref longer than
    # 8 cells (release-candidate -> release…), not bare-chop it to a dangling
    # "release-". Real hex shas keep the conventional 8-char prefix (tested via
    # the fixture's 0123456789abcdef -> 01234567 in the layout tests).
    app = _Host()
    async with app.run_test(size=(140, 40)) as pilot:
        screen = ModelManagerScreen(
            {
                "models": [
                    {
                        "entry_id": "rc",
                        "display_name": "rc-model",
                        "source": "hf_repo",
                        "pinned": True,
                        "cache_state": "cached",
                        "revision": "release-candidate",
                        "commit_sha": None,
                        "files": {"count": 1, "weights_format": "safetensors"},
                    }
                ]
            }
        )
        await app.push_screen(screen)
        await pilot.pause()
        model_list = str(screen.query_one("#model-manager-list", Static).content)
        assert "release…" in model_list  # ellipsized whole word
        assert "release-" not in model_list  # not the bare 8-char chop


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
