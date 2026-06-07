from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.fakes.fake_ssh import write_fake_ssh_runtime
from vela.agent.local import TargetCallError
from vela.config.targets import TargetConfig, TransportKind
from vela.transport.factory import target_client_for_config


def _write_fake_ssh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ssh = bin_dir / "ssh"
    write_fake_ssh_runtime(ssh)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    return ssh


def test_fake_ssh_simulates_agent_discovery_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ssh = _write_fake_ssh(tmp_path, monkeypatch)
    env = {
        **os.environ,
        "FAKE_SSH_VELA_PATH": "/home/bgconley/.local/share/vela/venv/bin/vela",
    }

    result = subprocess.run(
        [str(ssh), "bgconley@fake", "command", "-v", "vela"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "/home/bgconley/.local/share/vela/venv/bin/vela"


def test_fake_ssh_simulates_agent_absent_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ssh = _write_fake_ssh(tmp_path, monkeypatch)
    env = {**os.environ, "FAKE_SSH_VELA_PRESENT": "0"}

    command_probe = subprocess.run(
        [str(ssh), "bgconley@fake", "command -v vela"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    agent_start = subprocess.run(
        [str(ssh), "bgconley@fake", "vela agent connect"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert command_probe.returncode == 1
    assert agent_start.returncode == 127
    assert "vela: not found" in agent_start.stderr


def test_fake_ssh_simulates_version_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ssh = _write_fake_ssh(tmp_path, monkeypatch)
    env = {**os.environ, "FAKE_SSH_VELA_VERSION": "0.0.1"}

    result = subprocess.run(
        [str(ssh), "bgconley@fake", "vela --version"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "0.0.1"


@pytest.mark.asyncio
async def test_fake_ssh_drives_subprocess_target_client_handshake(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_fake_ssh(tmp_path, monkeypatch)
    command_log = tmp_path / "ssh-commands.jsonl"
    monkeypatch.setenv("FAKE_SSH_COMMAND_LOG", str(command_log))
    monkeypatch.setenv("FAKE_SSH_VELA_VERSION", "9.9.9")
    target = TargetConfig(
        name="blackbird",
        transport=TransportKind.SSH,
        host="bgconley@fake",
    )
    client = target_client_for_config(target)

    handshake = await client.connect()
    try:
        ping = await client.ping()
    finally:
        await client.disconnect()

    assert handshake["target"] == "blackbird"
    assert handshake["agent_version"] == "9.9.9"
    assert ping == {"ok": True}
    logged = json.loads(command_log.read_text(encoding="utf-8").splitlines()[0])
    assert logged["host"] == "bgconley@fake"
    assert logged["remote_command"] == "vela agent connect"


@pytest.mark.asyncio
async def test_fake_ssh_drives_unreachable_auth_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_fake_ssh(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_SSH_UNREACHABLE", "1")
    target = TargetConfig(
        name="blackbird",
        transport=TransportKind.SSH,
        host="bgconley@fake",
    )
    client = target_client_for_config(target)

    with pytest.raises(TargetCallError) as error:
        await client.connect()

    assert error.value.code == "agent-unreachable"
    assert error.value.details["reason"] == "ssh-auth"
    assert "Permission denied" in error.value.details["stderr"]


def test_fake_ssh_simulates_install_and_host_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ssh = _write_fake_ssh(tmp_path, monkeypatch)
    report = {
        "hostname": "fake-blackbird",
        "python": "/home/bgconley/.local/share/vela/venv/bin/python",
        "gpu": "NVIDIA RTX PRO 6000 Blackwell",
    }
    env = {
        **os.environ,
        "FAKE_SSH_INSTALL_SUCCESS": "1",
        "FAKE_SSH_HOST_REPORT_JSON": json.dumps(report, sort_keys=True),
    }

    install = subprocess.run(
        [str(ssh), "bgconley@fake", "python3 -m venv ~/.local/share/vela/venv"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    host_report = subprocess.run(
        [str(ssh), "bgconley@fake", "vela host_report --json"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert install.returncode == 0
    assert "installed" in install.stdout
    assert host_report.returncode == 0
    assert json.loads(host_report.stdout) == report


def test_fake_ssh_simulates_install_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ssh = _write_fake_ssh(tmp_path, monkeypatch)
    env = {
        **os.environ,
        "FAKE_SSH_INSTALL_SUCCESS": "0",
        "FAKE_SSH_INSTALL_STDERR": "uv unavailable",
    }

    result = subprocess.run(
        [str(ssh), "bgconley@fake", "pip install vela"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 1
    assert "uv unavailable" in result.stderr
