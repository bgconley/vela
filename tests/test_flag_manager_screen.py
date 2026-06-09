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
async def test_flag_manager_preserves_list_contract() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = _make_screen()
        await app.push_screen(screen)
        await pilot.pause()
        flag_list = str(screen.query_one("#flag-manager-list", Static).content)
        assert "Flag Manager" in flag_list
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
