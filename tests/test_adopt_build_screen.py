"""Headless tests for the refactored AdoptBuildScreen (Mac-safe; no GPU/vLLM).

Pins the redesign (Figma 52:2): a ValidationCard (auto-detected stack), Field-wrapped
inputs, and a WILL-DO preview — while the dismiss-payload contract (venv_path required,
copy flag from the checkbox) is preserved.
"""

from __future__ import annotations

import pytest
from textual.app import App
from textual.widgets import Checkbox, Input, Static

from vela.tui.screens.adopt_build import AdoptBuildScreen
from vela.tui.widgets import Field, KeyHintBar, ValidationCard


class _Host(App):
    pass


@pytest.mark.asyncio
async def test_adopt_build_validation_starts_neutral_without_fabrication() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = AdoptBuildScreen()
        await app.push_screen(screen)
        await pilot.pause()
        # No probe has run → no green card asserting a validation that never
        # happened; a neutral note explains what will happen.
        assert len(screen.query(ValidationCard)) == 0
        note = str(screen.query_one("#adopt-build-validation-note", Static).content)
        assert "validat" in note.lower()


@pytest.mark.asyncio
async def test_adopt_build_probe_success_renders_real_versions() -> None:
    calls: list[str] = []

    async def probe(path: str) -> dict[str, object]:
        calls.append(path)
        return {
            "ok": True,
            "vllm_version": "0.12.1",
            "torch_version": "2.7.0",
            "python_version": "3.13.1",
        }

    app = _Host()
    async with app.run_test() as pilot:
        screen = AdoptBuildScreen(probe=probe)
        await app.push_screen(screen)
        await pilot.pause()
        screen.query_one("#adopt-build-venv-path", Input).value = "/home/user/venvs/vllm-nightly"
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert calls == ["/home/user/venvs/vllm-nightly"]
        card = screen.query_one(ValidationCard)
        text = str(card.query_one(".validation-detail", Static).content)
        assert "vllm 0.12.1" in text
        assert "torch 2.7.0" in text
        assert "python 3.13.1" in text
        # The detected version auto-fills the (still editable) version input.
        assert screen.query_one("#adopt-build-vllm-version", Input).value == "0.12.1"


@pytest.mark.asyncio
async def test_adopt_build_probe_failure_renders_red_with_reason() -> None:
    async def probe(path: str) -> dict[str, object]:
        return {"ok": False, "reason": "vllm is not installed in this venv"}

    app = _Host()
    async with app.run_test() as pilot:
        screen = AdoptBuildScreen(probe=probe)
        await app.push_screen(screen)
        await pilot.pause()
        screen.query_one("#adopt-build-venv-path", Input).value = "/home/user/venvs/empty"
        await app.workers.wait_for_complete()
        await pilot.pause()
        card = screen.query_one(ValidationCard)
        assert card.has_class("-bad")
        text = str(card.query_one(".validation-detail", Static).content)
        assert "vllm is not installed" in text


@pytest.mark.asyncio
async def test_adopt_build_footer_advertises_only_working_keys() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = AdoptBuildScreen()
        await app.push_screen(screen)
        await pilot.pause()
        hints = screen.query_one("#adopt-build-footer", KeyHintBar)._hints
        # `space` only toggles the checkbox when it happens to have focus —
        # advertising it as a global key was misleading.
        assert all(key != "space" for key, _ in hints)


@pytest.mark.asyncio
async def test_adopt_build_wraps_inputs_in_field_widgets() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = AdoptBuildScreen()
        await app.push_screen(screen)
        await pilot.pause()
        assert len(screen.query(Field)) >= 3


@pytest.mark.asyncio
async def test_adopt_build_shows_will_do_preview() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = AdoptBuildScreen()
        await app.push_screen(screen)
        await pilot.pause()
        assert screen.query_one("#adopt-build-preview")


@pytest.mark.asyncio
async def test_adopt_build_collect_params_contract_preserved() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = AdoptBuildScreen()
        await app.push_screen(screen)
        await pilot.pause()
        screen.query_one("#adopt-build-venv-path", Input).value = "/home/user/venvs/vllm-nightly"
        screen.query_one("#adopt-build-label", Input).value = "vllm-nightly"
        screen.query_one("#adopt-build-copy", Checkbox).value = True
        params = screen._collect_adopt_build_params()
        assert params["venv_path"] == "/home/user/venvs/vllm-nightly"
        assert params["label"] == "vllm-nightly"
        assert params["copy"] == "true"


@pytest.mark.asyncio
async def test_adopt_build_discovered_venvs_picker_fills_path() -> None:
    # J35: discovered venvs are offered; choosing one fills the path (and the
    # live probe then validates it).
    async def discover() -> list[dict[str, object]]:
        return [
            {
                "venv_path": "/home/user/venvs/vllm-nightly",
                "ok": True,
                "vllm_version": "0.11.2",
            },
            {
                "venv_path": "/home/user/venvs/plain-env",
                "ok": False,
                "reason": "vllm is not installed in this venv",
            },
        ]

    app = _Host()
    async with app.run_test() as pilot:
        screen = AdoptBuildScreen(discover=discover)
        await app.push_screen(screen)
        await app.workers.wait_for_complete()
        await pilot.pause()
        from textual.widgets import Select

        select = screen.query_one("#adopt-build-discovered", Select)
        values = [value for _, value in select._options if value != Select.BLANK]
        assert "/home/user/venvs/vllm-nightly" in values
        select.value = "/home/user/venvs/vllm-nightly"
        await pilot.pause()
        assert (
            screen.query_one("#adopt-build-venv-path", Input).value
            == "/home/user/venvs/vllm-nightly"
        )
