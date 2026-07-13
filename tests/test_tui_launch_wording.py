from __future__ import annotations

from pathlib import Path

import pytest
from conftest import write_yaml
from textual.widgets import Static

from vela.tui.app import VelaApp
from vela.tui.screens.help import HelpScreen


@pytest.mark.asyncio
async def test_compute_start_surfaces_consistently_say_launch(config_dir: Path) -> None:
    write_yaml(config_dir / "alpha.yaml", "name: alpha\nmodel: org/alpha")
    app = VelaApp(configs_dir=config_dir, target_ping_interval_seconds=None)

    async with app.run_test() as pilot:
        await pilot.pause()
        footer = str(app.query_one("#footer-bindings", Static).content)
        commands = list(app.get_system_commands(app.screen))
        titles = {command.title for command in commands}
        help_text = HelpScreen._help_text(120).plain

        assert "l Launch" in footer
        assert "l Load" not in footer
        assert "Launch selected config" in titles
        assert "Launch config: alpha" in titles
        assert not {title for title in titles if title.startswith("Load config:")}
        assert "Launch / control:" in help_text
        assert "l Launch" in help_text
        assert "Load / control:" not in help_text
        assert "l Load" not in help_text


@pytest.mark.asyncio
async def test_launch_errors_and_guidance_use_the_same_verb(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = VelaApp(configs_dir=config_dir, target_ping_interval_seconds=None)

    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_load()
        assert app.error_text == "No valid configs to launch"

        notices: list[str] = []
        monkeypatch.setattr(
            app,
            "notify",
            lambda message, *args, **kwargs: notices.append(str(message)),
        )
        monkeypatch.setattr(app, "_reopen_manager_later", lambda *_args: None)
        await app._pin_build_to_current_config("build-id")
        assert notices[-1] == "Select a config first — l launches, c picks one"


def test_active_operator_docs_use_launch_for_compute_start() -> None:
    docs = {
        "tui": Path("docs/tui.md").read_text(encoding="utf-8"),
        "docker": Path("docs/docker-runtime.md").read_text(encoding="utf-8"),
        "gpu": Path("docs/gpu-workflow.md").read_text(encoding="utf-8"),
        "canonical": Path("docs/specs/vllm-tui-loader-spec-v2-CANONICAL.md").read_text(
            encoding="utf-8"
        ),
    }

    assert "| `l`, `enter` | `load` | Launch |" in docs["tui"]
    assert "TUI launch/READY/stop flow" in docs["docker"]
    assert "normal Launch workflow" in docs["gpu"]
    assert "│ l Launch  s Stop" in docs["canonical"]
    assert "“Launch config: …”" in docs["canonical"]
