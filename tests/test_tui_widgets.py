"""Headless tests for the shared Vela TUI widgets (Mac-safe; no GPU/vLLM)."""

from __future__ import annotations

import re

import pytest
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.css.scalar import Unit
from textual.screen import ModalScreen
from textual.widgets import Input, Label, Static

from vela.tui.theme import AMBER, CYAN, VIOLET
from vela.tui.widgets.contextcard import ContextCard
from vela.tui.widgets.field import Field
from vela.tui.widgets.keyhintbar import KeyHintBar
from vela.tui.widgets.masterdetail import MasterDetail
from vela.tui.widgets.preset_chips import PresetChips
from vela.tui.widgets.step_indicator import StepIndicator
from vela.tui.widgets.tags import (
    RECIPE_FLAGS,
    is_recipe_flag,
    source_tag,
    summarize_capabilities,
)
from vela.tui.widgets.validation_card import ValidationCard


class _FieldHarness(App):
    def compose(self) -> ComposeResult:
        yield Field(
            "Channel",
            Input(value="cu130", id="ctl"),
            helper="CUDA build channel - match your GPU.",
            required=True,
            id="f1",
        )


@pytest.mark.asyncio
async def test_field_wraps_control_and_renders_label_and_helper() -> None:
    app = _FieldHarness()
    async with app.run_test() as pilot:
        await pilot.pause()
        field = app.query_one("#f1", Field)
        # The wrapped control keeps its own id + value (behavior contract preserved).
        ctl = app.query_one("#ctl", Input)
        assert ctl.value == "cu130"
        # Label + required tag both render.
        assert len(field.query(Label)) == 2
        assert len(field.query(".field-req")) == 1
        assert field._label == "Channel"
        # Exactly one helper line.
        assert len(field.query(".field-helper")) == 1


class _OptionalFieldHarness(App):
    def compose(self) -> ComposeResult:
        yield Field("Environment", Input(id="c2"), optional=True, id="f2")


@pytest.mark.asyncio
async def test_field_optional_tag_and_no_helper() -> None:
    app = _OptionalFieldHarness()
    async with app.run_test() as pilot:
        await pilot.pause()
        field = app.query_one("#f2", Field)
        assert len(field.query(".field-opt")) == 1
        assert len(field.query(".field-req")) == 0
        assert len(field.query(".field-helper")) == 0


class _KeyHintHarness(App):
    def compose(self) -> ComposeResult:
        yield KeyHintBar([("⏎", "Create"), ("Tab", "Next"), ("Esc", "Cancel")], id="kb")


@pytest.mark.asyncio
async def test_keyhintbar_renders_a_key_and_label_per_hint() -> None:
    app = _KeyHintHarness()
    async with app.run_test() as pilot:
        await pilot.pause()
        bar = app.query_one("#kb", KeyHintBar)
        assert len(bar.query(".keyhint-key")) == 3
        assert len(bar.query(".keyhint-label")) == 3


class _ContextCardHarness(App):
    def compose(self) -> ComposeResult:
        yield ContextCard(
            "MODEL",
            [("repo", "Qwen/Qwen3.6-27B"), ("cache", "cached"), ("size", "~14 GB")],
            id="cc",
        )


@pytest.mark.asyncio
async def test_context_card_renders_heading_and_one_row_per_entry() -> None:
    app = _ContextCardHarness()
    async with app.run_test() as pilot:
        await pilot.pause()
        cc = app.query_one("#cc", ContextCard)
        assert len(cc.query(".context-card-heading")) == 1
        assert len(cc.query(".context-row")) == 3


class _PresetChipsHarness(App):
    def compose(self) -> ComposeResult:
        yield PresetChips(
            ["safetensors only", "everything", "no pickle"], selected=0, id="pc"
        )


@pytest.mark.asyncio
async def test_preset_chips_renders_chip_per_option_and_marks_selected() -> None:
    app = _PresetChipsHarness()
    async with app.run_test() as pilot:
        await pilot.pause()
        pc = app.query_one("#pc", PresetChips)
        assert len(pc.query(".preset-chip")) == 3
        assert len(pc.query(".preset-chip.selected")) == 1


class _PresetChipsSelectHarness(App):
    def __init__(self) -> None:
        super().__init__()
        self.selections: list[tuple[int, str]] = []

    def compose(self) -> ComposeResult:
        yield PresetChips(
            ["safetensors only", "everything", "no pickle"], selected=0, id="pc"
        )

    def on_preset_chips_selected(self, event: PresetChips.Selected) -> None:
        self.selections.append((event.index, event.option))


@pytest.mark.asyncio
async def test_preset_chips_select_moves_highlight_and_posts_message() -> None:
    app = _PresetChipsSelectHarness()
    async with app.run_test() as pilot:
        await pilot.pause()
        pc = app.query_one("#pc", PresetChips)
        pc.select(2)
        await pilot.pause()
        assert pc.selected == 2
        assert len(pc.query(".preset-chip.selected")) == 1
        assert app.selections == [(2, "no pickle")]


@pytest.mark.asyncio
async def test_preset_chips_highlight_is_silent_and_clearable() -> None:
    app = _PresetChipsSelectHarness()
    async with app.run_test() as pilot:
        await pilot.pause()
        pc = app.query_one("#pc", PresetChips)
        pc.highlight(None)
        await pilot.pause()
        assert pc.selected is None
        assert len(pc.query(".preset-chip.selected")) == 0
        assert app.selections == []  # highlight() never posts a message


@pytest.mark.asyncio
async def test_preset_chips_click_selects_chip() -> None:
    app = _PresetChipsSelectHarness()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#pc .chip-1")
        await pilot.pause()
        pc = app.query_one("#pc", PresetChips)
        assert pc.selected == 1
        assert app.selections == [(1, "everything")]


class _ValidationOkHarness(App):
    def compose(self) -> ComposeResult:
        yield ValidationCard(
            True,
            "Validated — vLLM importable",
            detail="vllm 0.11.2 · torch 2.6.0 · python 3.12",
            note="Detected automatically — you never type the version.",
            id="vc-ok",
        )


@pytest.mark.asyncio
async def test_validation_card_ok_renders_heading_detail_note() -> None:
    app = _ValidationOkHarness()
    async with app.run_test() as pilot:
        await pilot.pause()
        vc = app.query_one("#vc-ok", ValidationCard)
        assert vc.has_class("-ok")
        assert len(vc.query(".validation-heading")) == 1
        assert len(vc.query(".validation-detail")) == 1
        assert len(vc.query(".validation-note")) == 1


class _ValidationBadHarness(App):
    def compose(self) -> ComposeResult:
        yield ValidationCard(False, "No importable vLLM at this path", id="vc-bad")


@pytest.mark.asyncio
async def test_validation_card_bad_uses_bad_class() -> None:
    app = _ValidationBadHarness()
    async with app.run_test() as pilot:
        await pilot.pause()
        vc = app.query_one("#vc-bad", ValidationCard)
        assert vc.has_class("-bad")


# --- Phase 3 render helpers (master-detail color language) ---------------------
# These are pure helpers (no Textual app needed). They feed the manager screens'
# pinned #*-list / #*-detail Static panes as Rich Text, so str(content) stays
# plain and the smoke-suite substring assertions survive.


def test_source_tag_styles_each_kind_without_markup_leak() -> None:
    # Plain text is preserved (no markup leak) so Static.content substrings hold.
    assert str(source_tag("modeled")) == "modeled"
    assert str(source_tag("passthrough")) == "passthrough"
    assert str(source_tag("unknown")) == "unknown"
    assert str(source_tag("recipe")) == "recipe"
    # Each kind carries its semantic color (Figma source-tag palette).
    assert source_tag("modeled").style == CYAN
    assert source_tag("passthrough").style == VIOLET
    assert source_tag("unknown").style == AMBER
    assert source_tag("recipe").style == AMBER
    # A caller-supplied label overrides the displayed text but keeps the color.
    custom = source_tag("modeled", "tp")
    assert str(custom) == "tp"
    assert custom.style == CYAN


def test_summarize_capabilities_lists_small_sets_sorted() -> None:
    result = summarize_capabilities(["health", "gpu", "preview"], limit=8)
    assert result == "gpu, health, preview"


def test_summarize_capabilities_collapses_large_sets() -> None:
    caps = [f"cap{n:02d}" for n in range(12)]
    result = summarize_capabilities(caps, limit=8)
    # The 60-method-wall fix: a count + view-all affordance, not an inline dump.
    assert "12 supported" in result
    assert "view all" in result
    assert "cap00" not in result


def test_recipe_flags_cover_precision_critical_fields() -> None:
    assert RECIPE_FLAGS == frozenset({"dtype", "kv_cache_dtype"})
    assert is_recipe_flag("dtype")
    assert is_recipe_flag("kv_cache_dtype")
    assert not is_recipe_flag("tensor_parallel_size")


class _MasterDetailHarness(App):
    def compose(self) -> ComposeResult:
        yield MasterDetail(
            Static("L", id="md-list"),
            Static("R", id="md-detail"),
            footer=KeyHintBar([("⏎", "Select"), ("Esc", "Close")]),
            id="md",
        )


@pytest.mark.asyncio
async def test_master_detail_lays_out_panes_side_by_side_with_footer() -> None:
    app = _MasterDetailHarness()
    async with app.run_test() as pilot:
        await pilot.pause()
        md = app.query_one("#md", MasterDetail)
        # Caller-provided panes keep their ids + content (contract-preserving wrap).
        assert str(app.query_one("#md-list", Static).content) == "L"
        assert str(app.query_one("#md-detail", Static).content) == "R"
        # Both panes sit side-by-side inside the one horizontal body.
        body = md.query_one(".master-detail-body", Horizontal)
        assert body.query_one("#md-list", Static) is app.query_one("#md-list", Static)
        assert body.query_one("#md-detail", Static) is app.query_one("#md-detail", Static)
        # The optional footer is mounted.
        assert len(md.query(KeyHintBar)) == 1


class _StepIndicatorHarness(App):
    def compose(self) -> ComposeResult:
        yield StepIndicator(["Target", "Runtime", "Model", "Review"], current=1, id="si")


@pytest.mark.asyncio
async def test_step_indicator_marks_done_current_and_future() -> None:
    app = _StepIndicatorHarness()
    async with app.run_test() as pilot:
        await pilot.pause()
        si = app.query_one("#si", StepIndicator)
        content = str(si.content)
        assert "✓ Target" in content  # a completed step
        assert "▸ Runtime" in content  # the current step
        assert "Model" in content  # future steps still labelled
        assert "Review" in content
        # Advancing re-marks done/current.
        si.set_current(2)
        content2 = str(si.content)
        assert "✓ Runtime" in content2
        assert "▸ Model" in content2


@pytest.mark.asyncio
async def test_step_indicator_set_error_marks_step_and_clear_restores() -> None:
    # bug-236c: the breadcrumb must be honest about failed steps — set_error(i)
    # renders an amber ✗ that wins over done/current/future, survives
    # set_current re-renders, and clear_error restores the base state.
    app = _StepIndicatorHarness()
    async with app.run_test() as pilot:
        await pilot.pause()
        si = app.query_one("#si", StepIndicator)
        si.set_error(0)  # a done step (was ✓)
        si.set_error(2)  # a future step (was faint)
        content = str(si.content)
        assert "✗ Target" in content
        assert "✓ Target" not in content
        assert "✗ Model" in content
        assert "▸ Runtime" in content  # untouched steps keep their states
        # The error glyph uses the shared amber theme token.
        text = si._build_text()
        assert any(AMBER in str(span.style) for span in text.spans)
        # Errors persist across set_current re-renders.
        si.set_current(3)
        content = str(si.content)
        assert "✗ Target" in content
        assert "✗ Model" in content
        assert "▸ Review" in content
        # clear_error restores that one step; the other error remains.
        si.clear_error(2)
        content = str(si.content)
        assert "✓ Model" in content
        assert "✗ Target" in content
        # clear_errors wipes the rest.
        si.clear_errors()
        content = str(si.content)
        assert "✗" not in content
        assert "✓ Target" in content


# --- Task 4.1: shared modal frame tokens (bug-232 Flag Manager → bug-237 base) --
# theme.py carries ready-to-interpolate CSS declaration blocks for the
# near-full-screen, content-hugging modal frame that Tasks 4.2-4.4 apply to every
# manager/modal. These structural tests pin the four load-bearing panel
# properties so a future screen can't silently re-hardcode a fixed pixel/col
# width (the bug-237 regression), and prove the constant drops into an f-string
# CSS and resolves to real applied TCSS end-to-end.


def test_modal_panel_css_encodes_the_four_load_bearing_frame_rules() -> None:
    from vela.tui.theme import MODAL_PANEL_CSS

    css = MODAL_PANEL_CSS
    # The four properties the bug-232 relayout proved (content-hug, never clip).
    assert "width: 96%" in css  # fits every terminal; never off the right edge
    assert "height: auto" in css  # hug content; no fixed rows, no mid-screen gap
    assert "max-height: 96%" in css  # cap under the viewport; never past top/bottom
    assert "overflow-y: auto" in css  # scroll INSIDE the panel, never off-screen
    # The panel width must stay the percentage — no re-hardcoded fixed col width.
    assert re.search(r"width:\s*\d+\s*;", css) is None
    # A bare declaration block (no selector/braces) so it interpolates as a plain
    # f-string value exactly like the hex tokens — that is the whole mechanism.
    assert "{" not in css and "}" not in css


def test_modal_list_css_grows_then_scrolls() -> None:
    from vela.tui.theme import MODAL_LIST_CSS

    # Companion for the VerticalScroll list: full-width and content-hugging (grow);
    # the consuming screen appends its own `max-height: N` scroll cap.
    assert "width: 1fr" in MODAL_LIST_CSS
    assert "height: auto" in MODAL_LIST_CSS
    assert "{" not in MODAL_LIST_CSS and "}" not in MODAL_LIST_CSS


@pytest.mark.asyncio
async def test_modal_panel_css_interpolates_into_screen_css_end_to_end() -> None:
    # Interpolation proving ground (the way flag_manager.py consumes theme tokens):
    # drop MODAL_PANEL_CSS into a screen f-string CSS inside the panel's own
    # selector block, escaping the literal braces as {{ }}, and confirm the frame
    # resolves to real applied TCSS — a percentage, never a fixed width.
    from vela.tui.theme import BG_PANEL, BORDER_STRONG, MODAL_PANEL_CSS

    class _FrameModal(ModalScreen[None]):
        CSS = f"""
        _FrameModal #frame-panel {{
            {MODAL_PANEL_CSS}
            border: round {BORDER_STRONG};
            background: {BG_PANEL};
            padding: 1 2;
        }}
        """

        def compose(self) -> ComposeResult:
            with Vertical(id="frame-panel"):
                yield Static("body", id="frame-body")

    app = App()
    async with app.run_test() as pilot:
        screen = _FrameModal()
        await app.push_screen(screen)
        await pilot.pause()
        await pilot.pause()
        panel = screen.query_one("#frame-panel")
        # width: 96% resolves to a width-relative percentage, never fixed cells
        # (a hardcoded `width: 96` would be Unit.CELLS — the bug-237 regression).
        assert panel.styles.width.value == 96.0
        assert panel.styles.width.unit == Unit.WIDTH
        # height hugs content; max-height caps at 96% of the viewport; scroll inside.
        assert panel.styles.height.is_auto
        assert panel.styles.max_height.value == 96.0
        assert panel.styles.max_height.unit == Unit.HEIGHT
        assert panel.styles.overflow_y == "auto"
