from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.fakes.fake_ssh import write_fake_ssh_runtime
from vela import __version__
from vela.agent.local import TargetCallError
from vela.cli import app
from vela.config.targets import TargetConfig, TransportKind, load_targets_file


def _install_fake_ssh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_fake_ssh_runtime(bin_dir / "ssh")
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_SSH_VELA_VERSION", __version__)


def test_discover_ssh_agent_command_uses_command_v_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_ssh(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_SSH_VELA_PATH", "/opt/vela/bin/vela")
    from vela.transport.ssh_discovery import discover_ssh_agent_command

    result = discover_ssh_agent_command(
        TargetConfig(
            name="blackbird",
            transport=TransportKind.SSH,
            host="bgconley@fake",
        )
    )

    assert result.agent_command == ["/opt/vela/bin/vela", "agent", "connect"]
    assert result.source == "command-v"
    assert result.version == __version__


def test_discover_ssh_agent_command_uses_canonical_path_when_shell_path_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_ssh(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_SSH_COMMAND_V_PRESENT", "0")
    monkeypatch.setenv(
        "FAKE_SSH_VELA_PATH",
        "/home/bgconley/.local/share/vela/venv/bin/vela",
    )
    from vela.transport.ssh_discovery import discover_ssh_agent_command

    result = discover_ssh_agent_command(
        TargetConfig(
            name="blackbird",
            transport=TransportKind.SSH,
            host="bgconley@fake",
        )
    )

    assert result.agent_command == [
        "/home/bgconley/.local/share/vela/venv/bin/vela",
        "agent",
        "connect",
    ]
    assert result.source == "canonical-venv"


def test_discover_ssh_agent_command_rejects_absent_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_ssh(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_SSH_VELA_PRESENT", "0")
    from vela.transport.ssh_discovery import discover_ssh_agent_command

    with pytest.raises(TargetCallError) as error:
        discover_ssh_agent_command(
            TargetConfig(
                name="blackbird",
                transport=TransportKind.SSH,
                host="bgconley@fake",
            )
        )

    assert error.value.code == "command-not-found"
    assert error.value.details["remediation"] == (
        "vela targets bootstrap blackbird --install"
    )


def test_discover_ssh_agent_command_rejects_version_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_ssh(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_SSH_VELA_VERSION", "0.0.1")
    from vela.transport.ssh_discovery import discover_ssh_agent_command

    with pytest.raises(TargetCallError) as error:
        discover_ssh_agent_command(
            TargetConfig(
                name="blackbird",
                transport=TransportKind.SSH,
                host="bgconley@fake",
            )
        )

    assert error.value.code == "version-mismatch"
    assert error.value.details["controller_version"] == __version__
    assert error.value.details["agent_version"] == "0.0.1"


def test_cli_targets_add_discovers_and_persists_agent_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_ssh(tmp_path, monkeypatch)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("FAKE_SSH_VELA_PATH", "/opt/vela/bin/vela")

    result = CliRunner().invoke(
        app,
        [
            "targets",
            "add",
            "blackbird",
            "--transport",
            "ssh",
            "--host",
            "bgconley@fake",
        ],
    )

    assert result.exit_code == 0, result.output
    target = load_targets_file(tmp_path / "vela" / "targets.yaml").by_name("blackbird")
    assert target.agent_command == ["/opt/vela/bin/vela", "agent", "connect"]


def test_cli_targets_add_absent_agent_prints_bootstrap_remediation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_ssh(tmp_path, monkeypatch)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("FAKE_SSH_VELA_PRESENT", "0")

    result = CliRunner().invoke(
        app,
        [
            "targets",
            "add",
            "blackbird",
            "--transport",
            "ssh",
            "--host",
            "bgconley@fake",
        ],
    )

    assert result.exit_code == 2
    assert "ERROR AGENT_NOT_INSTALLED" in result.output
    assert "vela targets bootstrap blackbird --install" in result.output


def test_cli_targets_add_version_mismatch_prints_upgrade_remediation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_ssh(tmp_path, monkeypatch)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("FAKE_SSH_VELA_VERSION", "0.0.1")

    result = CliRunner().invoke(
        app,
        [
            "targets",
            "add",
            "blackbird",
            "--transport",
            "ssh",
            "--host",
            "bgconley@fake",
        ],
    )

    assert result.exit_code == 2
    assert "ERROR AGENT_VERSION_MISMATCH" in result.output
    assert "vela targets bootstrap blackbird --install" in result.output


def test_cli_targets_add_ssh_auth_failure_prints_setup_ssh_remediation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_ssh(tmp_path, monkeypatch)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("FAKE_SSH_UNREACHABLE", "1")
    monkeypatch.setenv("FAKE_SSH_STDERR", "Permission denied (publickey).")

    result = CliRunner().invoke(
        app,
        [
            "targets",
            "add",
            "blackbird",
            "--transport",
            "ssh",
            "--host",
            "bgconley@fake",
        ],
    )

    assert result.exit_code == 2
    assert "ERROR AGENT_UNREACHABLE" in result.output
    assert "Permission denied (publickey)." in result.output
    assert "vela targets setup-ssh blackbird" in result.output


def test_cli_targets_test_discovers_persists_and_handshakes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_ssh(tmp_path, monkeypatch)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    target = TargetConfig(
        name="blackbird",
        transport=TransportKind.SSH,
        host="bgconley@fake",
    )
    from vela.config.targets import upsert_target_file

    upsert_target_file(target)

    result = CliRunner().invoke(app, ["targets", "test", "blackbird"])

    assert result.exit_code == 0, result.output
    assert f"agent={__version__}" in result.output
    persisted = load_targets_file(tmp_path / "vela" / "targets.yaml").by_name(
        "blackbird"
    )
    assert persisted.agent_command == [
        "/home/bgconley/.local/share/vela/venv/bin/vela",
        "agent",
        "connect",
    ]
