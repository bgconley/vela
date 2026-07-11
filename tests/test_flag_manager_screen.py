"""Headless tests for the refactored FlagManagerScreen (Mac-safe; no GPU/vLLM).

These pin the redesign (Figma 55:2): a grouped flag table with recipe-safety cues
(amber `recipe` tags on dtype/kv-cache-dtype + a Recipe-protected warning), a
self-explaining detail pane with the `→ engine.*` mapping, and the masked
resolved-command panel — while the list/detail substring contract the smoke suite
relies on is preserved.
"""

from __future__ import annotations

import pytest
from textual.app import App
from textual.widgets import Static

from vela.config.schema import EngineConfig, ModelConfig
from vela.tui.screens.flag_manager import FlagManagerScreen
from vela.tui.widgets import KeyHintBar

_FLAG_MAP = {
    "tensor_parallel_size": "--tensor-parallel-size",
    "kv_cache_dtype": "--kv-cache-dtype",
    "gpu_memory_utilization": "--gpu-memory-utilization",
    "dtype": "--dtype",
}

_PREVIEW = (
    "VLLM_LOGGING_LEVEL=INFO vllm serve org/model "
    "--tensor-parallel-size 2 --kv-cache-dtype fp8 "
    "--moe-backend flashinfer_cutlass --legacy-flag value"
)


class _Host(App):
    pass


def _make_screen() -> FlagManagerScreen:
    config = ModelConfig(
        name="flags",
        model="org/model",
        engine=EngineConfig(tensor_parallel_size=2, kv_cache_dtype="fp8"),
        extra_args=["--moe-backend", "flashinfer_cutlass", "--legacy-flag", "value"],
    )
    return FlagManagerScreen(
        config,
        preview=_PREVIEW,
        metadata={"flag_map": _FLAG_MAP, "known_flags": ["--moe-backend"]},
    )


@pytest.mark.asyncio
async def test_flag_manager_title_and_context_render_first() -> None:
    # bug-237 (live-observed): the preset select + Changed-only checkbox
    # rendered ABOVE the `Flag Manager` title and build/config context. The
    # title row must be the panel's topmost rendered row, controls below it.
    app = _Host()
    async with app.run_test(size=(120, 40)) as pilot:
        screen = _make_screen()
        await app.push_screen(screen)
        await pilot.pause()
        title = screen.query_one("#flag-manager-title", Static)
        content = str(title.content)
        assert content.splitlines()[0] == "Flag Manager"
        assert "build:" in content
        assert "config: flags" in content
        controls = screen.query_one("#flag-manager-controls")
        flag_list = screen.query_one("#flag-manager-list", Static)
        assert title.region.y < controls.region.y
        assert title.region.y < flag_list.region.y
        # Topmost rendered row of the panel == the title's first row.
        panel = screen.query_one("#flag-manager-panel")
        assert title.region.y == min(child.region.y for child in panel.children)


@pytest.mark.asyncio
async def test_flag_manager_preserves_list_contract() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = _make_screen()
        await app.push_screen(screen)
        await pilot.pause()
        # "Flag Manager" moved to the topmost #flag-manager-title widget
        # (bug-237 title-first fix); the list keeps the flag-table contract.
        flag_list = str(screen.query_one("#flag-manager-list", Static).content)
        assert "modeled 2" in flag_list
        assert "passthrough 1" in flag_list
        assert "unknown 1" in flag_list
        assert "MODELED" in flag_list
        assert "PASSTHROUGH" in flag_list
        assert "UNKNOWN-TO-BUILD" in flag_list
        assert "kv-cache-dtype = fp8" in flag_list
        assert "tensor-parallel-size = 2" in flag_list
        assert "--moe-backend flashinfer_cutlass" in flag_list
        assert "--legacy-flag value" in flag_list


@pytest.mark.asyncio
async def test_flag_manager_tags_recipe_flags_in_list() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = _make_screen()
        await app.push_screen(screen)
        await pilot.pause()
        flag_list = str(screen.query_one("#flag-manager-list", Static).content)
        # kv-cache-dtype is recipe-protected -> a visible recipe tag (Refinement B).
        assert "recipe" in flag_list


@pytest.mark.asyncio
async def test_flag_manager_detail_explains_selected_flag_with_recipe_cue() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = _make_screen()
        await app.push_screen(screen)
        await pilot.pause()
        # modeled[0]=tensor-parallel-size, modeled[1]=kv-cache-dtype (recipe).
        screen.action_next()
        await pilot.pause()
        detail = str(screen.query_one("#flag-manager-detail", Static).content)
        # Self-explaining detail: plain-language description + engine mapping.
        assert "KV cache" in detail
        assert "engine.kv_cache_dtype" in detail
        # Recipe-safety cue (Refinement B, locked requirement).
        assert "Recipe-protected" in detail
        # The resolved-command contract is preserved.
        assert "Resolved command" in detail
        assert "--kv-cache-dtype fp8" in detail


@pytest.mark.asyncio
async def test_flag_manager_detail_preserves_resolved_command() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = _make_screen()
        await app.push_screen(screen)
        await pilot.pause()
        detail = str(screen.query_one("#flag-manager-detail", Static).content)
        assert "Resolved command" in detail
        assert "--kv-cache-dtype fp8" in detail
        assert "--moe-backend flashinfer_cutlass" in detail


@pytest.mark.asyncio
async def test_flag_manager_save_payload_contract_preserved() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = _make_screen()
        await app.push_screen(screen)
        await pilot.pause()
        captured: dict = {}
        screen.dismiss = lambda result=None: captured.update(result or {})  # type: ignore[assignment]
        screen.action_save()
        assert captured["action"] == "save_flags"
        assert captured["name"] == "flags"
        assert "engine" in captured
        assert captured["extra_args"] == [
            "--moe-backend",
            "flashinfer_cutlass",
            "--legacy-flag",
            "value",
        ]


@pytest.mark.asyncio
async def test_flag_manager_legend_explains_sources() -> None:
    # J20: the modeled/passthrough/unknown taxonomy is defined in-UI.
    app = _Host()
    async with app.run_test() as pilot:
        screen = _make_screen()
        await app.push_screen(screen)
        await pilot.pause()
        content = str(screen.query_one("#flag-manager-list", Static).content)
        assert "modeled = typed flags this build understands" in content
        assert "passthrough = raw args forwarded as-is" in content
        assert "unknown = not recognized by this build" in content


@pytest.mark.asyncio
async def test_flag_manager_protection_note_offers_alternative() -> None:
    # J20: recipe protection names the safe alternative action.
    app = _Host()
    async with app.run_test() as pilot:
        screen = _make_screen()
        await app.push_screen(screen)
        await pilot.pause()
        screen.selected_index = next(
            index
            for index, row in enumerate(screen.modeled)
            if row.get("field") == "kv_cache_dtype"
        )
        screen._refresh()
        detail = str(screen.query_one("#flag-manager-detail", Static).content)
        assert "Recipe-protected" in detail
        assert "switch recipe or preset" in detail


@pytest.mark.asyncio
async def test_flag_manager_footer_advertises_tab_edit() -> None:
    # J23: how to reach the value editor is stated.
    app = _Host()
    async with app.run_test() as pilot:
        screen = _make_screen()
        await app.push_screen(screen)
        await pilot.pause()
        hints = screen.query_one("#flag-manager-footer", KeyHintBar)._hints
        assert ("Tab", "Edit value") in hints


@pytest.mark.asyncio
async def test_flag_manager_preset_description_rendered() -> None:
    # J21: preset descriptions surface next to the preset select.
    app = _Host()
    async with app.run_test() as pilot:
        config = ModelConfig(
            name="flags",
            model="org/model",
            engine=EngineConfig(tensor_parallel_size=2, kv_cache_dtype="fp8"),
        )
        screen = FlagManagerScreen(
            config,
            preview=_PREVIEW,
            metadata={"flag_map": _FLAG_MAP},
            presets=[
                {
                    "name": "balanced",
                    "description": "Steady defaults for general serving",
                    "engine": {},
                }
            ],
            selected_preset="balanced",
        )
        await app.push_screen(screen)
        await pilot.pause()
        help_text = str(screen.query_one("#flag-manager-preset-help", Static).content)
        assert "Steady defaults" in help_text


@pytest.mark.asyncio
async def test_flag_manager_uses_full_width_scrollable_flag_list() -> None:
    # Rebuilt layout (fits-everything): the flag list is a tall, full-width
    # scroll region stacked ABOVE the editor, not a cramped ~46-col side column.
    from textual.containers import VerticalScroll

    app = _Host()
    async with app.run_test(size=(120, 40)) as pilot:
        screen = _make_screen()
        await app.push_screen(screen)
        await pilot.pause()
        screen.query_one("#flag-manager-list-scroll", VerticalScroll)
        flag_list = screen.query_one("#flag-manager-list", Static)
        editor = screen.query_one("#flag-manager-editor")
        # full width (was capped at ~46) and stacked above the editor (was beside it)
        assert flag_list.region.width > 80
        assert flag_list.region.y < editor.region.y
