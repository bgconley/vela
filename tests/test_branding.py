from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import tomllib

REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_TEXT_FILES = [
    "README.md",
    "docs/agent-rpc.md",
    "docs/builds-and-models.md",
    "docs/configuration.md",
    "docs/gpu-workflow.md",
    "packaging/systemd/vela-agent.service",
    "scripts/run_remote_tests.sh",
    "scripts/rsync_to_gpu.sh",
]

LAB_PATHS_WITH_LEGACY_REPO_NAME = (
    "/home/bgconley/repos/lab-tui",
    "/home/bgconley/venvs/lab-tui",
    "/tank/venvs/lab-tui",
)


def test_project_is_branded_as_vela() -> None:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]

    assert project["name"] == "vela"
    assert project["scripts"] == {"vela": "vela.cli:main"}
    assert "Vela" in project["description"]


def test_default_product_paths_and_agent_command_use_vela(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Discovery now honors $XDG_CONFIG_HOME over ~/.config (bug-238), so clear it to
    # assert the injected-home fallback deterministically.
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    from vela.agent.daemon import default_agent_socket_path
    from vela.config.loader import discover_config_dirs
    from vela.config.schema import default_run_artifacts_dir
    from vela.config.targets import default_targets_path
    from vela.engine.build_registry import default_builds_root
    from vela.engine.model_registry import default_models_registry_path
    from vela.transport.factory import DEFAULT_AGENT_COMMAND, DEFAULT_SSH_CONTROL_OPTIONS

    assert DEFAULT_AGENT_COMMAND == ("vela", "agent", "connect")
    assert DEFAULT_SSH_CONTROL_OPTIONS["ControlPath"] == "~/.ssh/vela-%C"
    assert "vela" in str(default_agent_socket_path())
    assert "vela" in str(default_targets_path())
    assert discover_config_dirs(cwd="/workspace/project", home="/home/user") == [
        Path("/workspace/project/configs"),
        Path("/home/user/.config/vela/configs"),
    ]
    assert "vela" in str(default_run_artifacts_dir())
    assert "vela" in str(default_builds_root())
    assert "vela" in str(default_models_registry_path())


def test_module_cli_help_is_vela_branded() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "vela.cli", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "Vela" in result.stdout
    for old_name in (
        "vllm-loader",
        "vLLM Loader",
        "VLLM_LOADER",
        "vllm_loader",
        "VllmLoader",
        "lab-tui",
        "LAB_TUI",
    ):
        assert old_name not in result.stdout


def test_live_docs_and_scripts_do_not_emit_old_app_names() -> None:
    forbidden = (
        "vllm-loader",
        "vLLM Loader",
        "VLLM_LOADER",
        "vllm_loader",
        "VllmLoader",
        "lab-tui",
        "LAB_TUI",
    )
    for relative_path in LIVE_TEXT_FILES:
        text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        for lab_path in LAB_PATHS_WITH_LEGACY_REPO_NAME:
            text = text.replace(lab_path, "")
        for old_name in forbidden:
            assert old_name not in text, f"{old_name!r} remains in {relative_path}"
