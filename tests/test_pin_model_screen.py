"""Headless tests for the rebuilt PinModelScreen (Mac-safe; no GPU/vLLM).

Pins the M-M1 redesign (Figma page "Journey v2 — Friction Pass", node 70:2):
a Source select with progressive disclosure, Field-wrapped inputs with
self-explaining helpers, a collapsed Advanced section behind Ctrl+R, the
canonical gated/HF_TOKEN note, a WILL PIN preview, and a KeyHintBar footer —
while every existing ``#pin-model-*`` control stays mounted and the dismiss
payload contract is preserved (gaining only the optional ``download_now``).
"""

from __future__ import annotations

import pytest
from textual.app import App
from textual.widgets import Checkbox, Input, Select, Static

from vela.tui.screens.pin_model import PinModelScreen
from vela.tui.widgets import KeyHintBar


class _Host(App):
    pass


@pytest.mark.asyncio
async def test_pin_model_defaults_to_hf_source_with_disclosure() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = PinModelScreen()
        await app.push_screen(screen)
        await pilot.pause()
        assert str(screen.query_one("#pin-model-source", Select).value) == "hf"
        # HF fields visible; the other sources' fields hidden but mounted.
        assert screen.query_one("#pm-repo-id").display is True
        assert screen.query_one("#pm-revision").display is True
        assert screen.query_one("#pm-commit-sha").display is True
        assert screen.query_one("#pm-local-path").display is False
        assert screen.query_one("#pm-url").display is False
        assert screen.query_one("#pin-model-local-path", Input)  # still mounted
        # Advanced starts collapsed.
        for key in ("quant-format", "tokenizer", "notes"):
            assert screen.query_one(f"#pm-{key}").display is False
        assert screen.query_one("#pm-advanced-checks").display is False


@pytest.mark.asyncio
async def test_pin_model_source_derived_from_initial() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = PinModelScreen(initial={"local_path": "/agent/models/qwen"})
        await app.push_screen(screen)
        await pilot.pause()
        assert str(screen.query_one("#pin-model-source", Select).value) == "local"
        assert screen.query_one("#pm-local-path").display is True
        assert screen.query_one("#pin-model-local-path", Input).value == "/agent/models/qwen"
        assert screen.query_one("#pm-repo-id").display is False
        assert screen.query_one("#pm-revision").display is False


@pytest.mark.asyncio
async def test_pin_model_advanced_toggle_and_initial_expansion() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = PinModelScreen()
        await app.push_screen(screen)
        await pilot.pause()
        assert screen.query_one("#pm-tokenizer").display is False
        screen.action_toggle_advanced()
        assert screen.query_one("#pm-tokenizer").display is True
        assert screen.query_one("#pm-advanced-checks").display is True
        screen.action_toggle_advanced()
        assert screen.query_one("#pm-tokenizer").display is False
    second_app = _Host()
    async with second_app.run_test() as pilot:
        # An initial advanced value must not be hidden.
        screen = PinModelScreen(initial={"tokenizer": "org/other-model"})
        await second_app.push_screen(screen)
        await pilot.pause()
        assert screen.query_one("#pm-tokenizer").display is True


@pytest.mark.asyncio
async def test_pin_model_payload_contract_preserved() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = PinModelScreen()
        await app.push_screen(screen)
        await pilot.pause()
        screen.query_one("#pin-model-repo-id", Input).value = "Qwen/Qwen3-32B"
        screen.query_one("#pin-model-display-name", Input).value = "qwen-remote"
        screen.query_one("#pin-model-revision", Input).value = "main"
        screen.query_one("#pin-model-commit-sha", Input).value = "abc123"
        screen.query_one("#pin-model-quant-format", Input).value = "bf16"
        params = screen._collect_model_pin_params()
        assert params == {
            "repo_id": "Qwen/Qwen3-32B",
            "display_name": "qwen-remote",
            "revision": "main",
            "commit_sha": "abc123",
            "quant_format": "bf16",
        }


@pytest.mark.asyncio
async def test_pin_model_hidden_source_fields_do_not_leak() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = PinModelScreen(initial={"repo_id": "Qwen/Qwen3-32B", "revision": "main"})
        await app.push_screen(screen)
        await pilot.pause()
        screen.query_one("#pin-model-source", Select).value = "url"
        await pilot.pause()
        screen.query_one("#pin-model-url", Input).value = "https://host/model.gguf"
        params = screen._collect_model_pin_params()
        assert params["url"] == "https://host/model.gguf"
        assert params["source"] == "url"
        assert "repo_id" not in params
        assert "revision" not in params


@pytest.mark.asyncio
async def test_pin_model_download_now_flag() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = PinModelScreen()
        await app.push_screen(screen)
        await pilot.pause()
        screen.query_one("#pin-model-repo-id", Input).value = "Qwen/Qwen3-32B"
        params = screen._collect_model_pin_params()
        assert "download_now" not in params
        screen.query_one("#pin-model-download-now", Checkbox).value = True
        params = screen._collect_model_pin_params()
        assert params["download_now"] is True


@pytest.mark.asyncio
async def test_pin_model_helpers_footer_and_gated_note() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = PinModelScreen(target_label="gpu-node")
        await app.push_screen(screen)
        await pilot.pause()
        hints = screen.query_one("#pin-model-footer", KeyHintBar)._hints
        assert ("Ctrl+R", "Advanced") in hints
        assert ("⏎", "Pin") in hints
        note = str(screen.query_one("#pin-model-gated-note", Static).content)
        assert "huggingface.co" in note
        assert "agent env or config env" in note
        assert screen.query_one("#pin-model-preview")
        assert "target: gpu-node" in str(
            screen.query_one("#pin-model-target", Static).content
        )


@pytest.mark.asyncio
async def test_pin_model_validation_message_is_human_per_source() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = PinModelScreen()
        await app.push_screen(screen)
        await pilot.pause()
        screen.action_submit()
        error = str(screen.query_one("#pin-model-error", Static).content)
        assert "org/model" in error and "huggingface.co" in error
        screen.query_one("#pin-model-source", Select).value = "local"
        await pilot.pause()
        screen.action_submit()
        error = str(screen.query_one("#pin-model-error", Static).content)
        assert "absolute path" in error
