"""Headless tests for the shared Vela TUI widgets (Mac-safe; no GPU/vLLM)."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.containers import Horizontal
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
