from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.fakes.fake_ssh import write_fake_ssh_runtime
from vela import __version__
from vela.agent.auth import default_agent_token_file
from vela.agent.local import TargetCallError
from vela.cli import app
from vela.config.targets import (
    TargetConfig,
    TransportKind,
    load_targets_file,
    upsert_target_file,
)


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
    assert "version\tok" in result.output
    assert "host\thostname=fake-remote" in result.output
    assert "paths\tconfig=/home/bgconley/.config/vela" in result.output
    assert "toolchain\tpython=/usr/bin/python3 uv=yes" in result.output
    assert "auth\tnone" in result.output
    persisted = load_targets_file(tmp_path / "vela" / "targets.yaml").by_name(
        "blackbird"
    )
    assert persisted.agent_command == [
        "/home/bgconley/.local/share/vela/venv/bin/vela",
        "agent",
        "connect",
    ]


def test_cli_targets_bootstrap_installs_absent_agent_then_handshakes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_ssh(tmp_path, monkeypatch)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("FAKE_SSH_VELA_PRESENT", "0")
    monkeypatch.setenv("FAKE_SSH_INSTALLED_MARKER", str(tmp_path / "installed"))

    result = CliRunner().invoke(
        app,
        [
            "targets",
            "bootstrap",
            "blackbird",
            "--host",
            "bgconley@fake",
            "--install",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "OK\tssh\treachable" in result.output
    assert (
        "OK\tagent\tinstalled /home/bgconley/.local/share/vela/venv/bin/vela"
        in result.output
    )
    assert f"OK\thandshake\tagent={__version__}" in result.output
    assert (tmp_path / "installed").read_text(encoding="utf-8") == "installed"
    persisted = load_targets_file(tmp_path / "vela" / "targets.yaml").by_name(
        "blackbird"
    )
    assert persisted.agent_command == [
        "/home/bgconley/.local/share/vela/venv/bin/vela",
        "agent",
        "connect",
    ]


def test_cli_targets_bootstrap_build_creates_default_pip_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_ssh(tmp_path, monkeypatch)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("FAKE_SSH_VELA_PRESENT", "0")
    monkeypatch.setenv("FAKE_SSH_INSTALLED_MARKER", str(tmp_path / "installed"))

    result = CliRunner().invoke(
        app,
        [
            "targets",
            "bootstrap",
            "blackbird",
            "--host",
            "bgconley@fake",
            "--install",
            "--build",
            "vllm==0.11.2",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "OK\thandshake" in result.output
    assert "bootstrapped target blackbird" in result.output
    assert "Installing build" in result.output
    assert "DONE\t" in result.output
    assert "build ready" in result.output


def test_cli_agent_gen_token_install_target_pushes_token_over_fake_ssh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_ssh(tmp_path, monkeypatch)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("VELA_AGENT_TOKEN", raising=False)
    monkeypatch.delenv("VELA_AGENT_TOKEN_FILE", raising=False)
    remote_token_path = tmp_path / "remote" / "agent-token"
    monkeypatch.setenv("FAKE_SSH_AGENT_TOKEN_FILE", str(remote_token_path))
    upsert_target_file(
        TargetConfig(
            name="blackbird",
            transport=TransportKind.SSH,
            host="bgconley@fake",
            agent_command=["vela", "agent", "connect"],
        ),
        tmp_path / "vela" / "targets.yaml",
    )

    result = CliRunner().invoke(
        app,
        ["agent", "gen-token", "--install", "--target", "blackbird"],
    )

    assert result.exit_code == 0, result.output
    local_token = default_agent_token_file().read_text(encoding="utf-8").strip()
    assert remote_token_path.read_text(encoding="utf-8").strip() == local_token
    assert (remote_token_path.stat().st_mode & 0o777) == 0o600
    assert f"installed agent token\t{default_agent_token_file()}" in result.output
    assert f"installed target agent token\tblackbird\t{remote_token_path}" in result.output


def test_cli_doctor_target_reports_remote_host_state_without_static_nag(
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

    result = CliRunner().invoke(app, ["doctor", "--target", "blackbird", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    checks = {check["name"]: check for check in payload["checks"]}
    assert payload["ok"] is True
    assert payload["next_steps"] == []
    assert checks["target_connection"]["ok"] is True
    assert checks["target_version"]["detail"] == f"agent={__version__} controller={__version__}"
    assert "config=/home/bgconley/.config/vela" in checks["target_paths"]["detail"]
    assert "uv=yes" in checks["target_toolchain"]["detail"]
    assert checks["target_auth"]["detail"] == "none"


def test_cli_agent_status_target_reports_remote_paths(
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

    result = CliRunner().invoke(app, ["agent", "status", "--target", "blackbird"])

    assert result.exit_code == 0, result.output
    assert "target\tblackbird" in result.output
    assert f"version\tok\tagent={__version__} controller={__version__}" in result.output
    assert "paths\tconfig=/home/bgconley/.config/vela" in result.output
    assert "toolchain\tpython=/usr/bin/python3 uv=yes" in result.output
    assert "auth\tnone" in result.output
