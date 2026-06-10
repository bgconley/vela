"""Headless tests for the refactored CreateBuildScreen (Mac-safe; no GPU/vLLM).

These pin the redesign (Figma 49:2): inputs wrapped in `Field` widgets, progressive
disclosure by method, and a WILL-RUN preview — while the dismiss-payload contract
consumed by app.py is preserved.
"""

from __future__ import annotations

import pytest
from textual.app import App
from textual.widgets import Input, Select

from vela.tui.screens.create_build import CreateBuildScreen
from vela.tui.widgets import Field


class _Host(App):
    pass


@pytest.mark.asyncio
async def test_create_build_wraps_inputs_in_field_widgets() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = CreateBuildScreen(
            target_label="gpu-node", uv_available=True, initial={"method": "nightly"}
        )
        await app.push_screen(screen)
        await pilot.pause()
        assert len(screen.query(Field)) >= 4


@pytest.mark.asyncio
async def test_create_build_nightly_hides_irrelevant_fields() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = CreateBuildScreen(
            target_label="gpu-node", uv_available=True, initial={"method": "nightly"}
        )
        await app.push_screen(screen)
        await pilot.pause()
        # Progressive disclosure: nightly shows label/channel/python/env...
        for key in ("label", "channel", "python", "env"):
            assert screen.query_one(f"#cb-{key}", Field).display is True
        # ...and hides the fields that belong to other methods.
        for key in ("spec", "commit", "url", "path"):
            assert screen.query_one(f"#cb-{key}", Field).display is False


@pytest.mark.asyncio
async def test_create_build_shows_will_run_preview() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = CreateBuildScreen(
            target_label="gpu-node",
            uv_available=True,
            initial={"method": "nightly", "channel": "cu130"},
        )
        await app.push_screen(screen)
        await pilot.pause()
        assert screen.query_one("#create-build-preview")  # element exists
        assert screen._preview_command.startswith("uv pip install --pre vllm")


@pytest.mark.asyncio
async def test_create_build_hidden_fields_do_not_leak_into_params() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = CreateBuildScreen(
            target_label="gpu-node", uv_available=True, initial={"method": "pip"}
        )
        await app.push_screen(screen)
        await pilot.pause()
        screen.query_one("#create-build-label", Input).value = "my-build"
        screen.query_one("#create-build-spec", Input).value = "vllm==0.11.2"
        screen.query_one("#create-build-env", Input).value = "FOO=bar"
        # Switch to a method that hides spec — its stale value must not leak.
        screen.query_one("#create-build-method", Select).value = "nightly"
        await pilot.pause()
        params = screen._collect_build_params()
        assert params["method"] == "nightly"
        assert "spec" not in params
        assert params["env"] == ["FOO=bar"]  # env IS relevant to nightly
        # Switch to a method that hides env too.
        screen.query_one("#create-build-method", Select).value = "wheel"
        await pilot.pause()
        screen.query_one("#create-build-path", Input).value = "/tmp/vllm.whl"
        params = screen._collect_build_params()
        assert params["method"] == "wheel"
        assert "env" not in params
        assert "spec" not in params
        assert params["path"] == "/tmp/vllm.whl"


@pytest.mark.asyncio
async def test_create_build_collect_params_contract_preserved() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = CreateBuildScreen(
            target_label="gpu-node", uv_available=True, initial={"method": "nightly"}
        )
        await app.push_screen(screen)
        await pilot.pause()
        screen.query_one("#create-build-label", Input).value = "nightly-cu130"
        screen.query_one("#create-build-channel", Input).value = "cu130"
        screen.query_one("#create-build-python", Input).value = "3.12"
        params = screen._collect_build_params()
        assert params == {
            "method": "nightly",
            "label": "nightly-cu130",
            "channel": "cu130",
            "python": "3.12",
        }


@pytest.mark.asyncio
async def test_wheel_helper_routes_venvs_to_adopt_and_pip_hides_channel() -> None:
    # J32: the wheel helper must not invite venv paths (the agent requires a
    # .whl file); pip ignores channel so the field is not shown for it.
    app = _Host()
    async with app.run_test() as pilot:
        screen = CreateBuildScreen(
            target_label="gpu-node", uv_available=True, initial={"method": "pip"}
        )
        await app.push_screen(screen)
        await pilot.pause()
        assert screen.query_one("#cb-channel", Field).display is False
        helper_texts = [
            str(static.content)
            for static in screen.query_one("#cb-path", Field).query("Static")
        ]
        assert any("Adopt" in text for text in helper_texts)
        assert not any("or venv" in text for text in helper_texts)


@pytest.mark.asyncio
async def test_git_method_offers_optional_ref() -> None:
    # J33: the agent supports a git ref; the form can finally express it.
    app = _Host()
    async with app.run_test() as pilot:
        screen = CreateBuildScreen(
            target_label="gpu-node", uv_available=True, initial={"method": "git"}
        )
        await app.push_screen(screen)
        await pilot.pause()
        assert screen.query_one("#cb-ref", Field).display is True
        screen.query_one("#create-build-label", Input).value = "git-build"
        screen.query_one("#create-build-url", Input).value = "https://github.com/org/vllm.git"
        screen.query_one("#create-build-ref", Input).value = "v0.11.2"
        params = screen._collect_build_params()
        assert params["ref"] == "v0.11.2"
        # ref is git-only: switching method drops it.
        screen.query_one("#create-build-method", Select).value = "nightly"
        await pilot.pause()
        assert screen.query_one("#cb-ref", Field).display is False
        assert "ref" not in screen._collect_build_params()
