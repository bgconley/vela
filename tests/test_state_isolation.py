"""Pins the test-suite state isolation (the durable bug-185 fix).

Suites must never read or write the user's real ``~/.local/state/vela`` (runs,
agent socket/daemon, models registry) or ``~/.local/share/vela`` (builds):
shared state accumulates across runs until launch tests time out, and a stale
agent daemon on the shared socket serves OLD code to every test after a source
change. The session-scoped ``isolated_vela_state`` fixture in conftest.py points
all of it at a per-session temp dir instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vela.agent.daemon import default_agent_socket_path
from vela.config.schema import default_run_artifacts_dir
from vela.engine.build_registry import default_builds_root
from vela.engine.model_registry import default_models_registry_path


def test_run_artifacts_dir_honors_xdg_state_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert default_run_artifacts_dir() == tmp_path / "state" / "vela" / "runs"


def test_suite_state_is_isolated_from_the_user_home() -> None:
    home_state = str(Path.home() / ".local")
    for path in (
        default_run_artifacts_dir(),
        default_agent_socket_path(),
        default_builds_root(),
        default_models_registry_path(),
    ):
        assert not str(path).startswith(home_state), (
            f"{path} points at the user's real state — the isolated_vela_state "
            "fixture is not in effect"
        )
