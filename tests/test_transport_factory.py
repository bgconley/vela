from __future__ import annotations

from pathlib import Path

import pytest

from vllm_loader.config.targets import TargetConfig, TransportKind
from vllm_loader.transport.inprocess import InProcessTargetClient
from vllm_loader.transport.socket import UnixSocketTargetClient
from vllm_loader.transport.subprocess import SubprocessTargetClient


def _target_client_for_config():
    try:
        from vllm_loader.transport.factory import target_client_for_config
    except ModuleNotFoundError as exc:
        pytest.fail(f"target client factory missing: {exc}")
    return target_client_for_config


@pytest.mark.asyncio
async def test_target_client_factory_builds_local_in_process_client() -> None:
    client = _target_client_for_config()(TargetConfig(name="local"))

    assert isinstance(client, InProcessTargetClient)
    await client.connect()
    try:
        handshake = await client.call("handshake")
    finally:
        await client.disconnect()

    assert handshake["target"] == "local"


def test_target_client_factory_builds_explicit_local_socket_client(
    tmp_path: Path,
) -> None:
    socket_path = tmp_path / "agent.sock"
    target = TargetConfig(
        name="local-socket",
        local_transport="socket",
        socket_path=socket_path,
    )

    client = _target_client_for_config()(target)

    assert isinstance(client, UnixSocketTargetClient)
    assert client._socket_path == socket_path


def test_target_client_factory_builds_ssh_subprocess_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VLLM_LOADER_SSH_OPTS", "-i /tmp/gpu-key -o ProxyJump=bastion")
    target = TargetConfig(
        name="blackbird",
        transport=TransportKind.SSH,
        host="bgconley@10.25.0.51",
        workdir=Path("/tank/repos/lab-tui"),
        venv=Path("/tank/venvs/lab-tui"),
        ssh_opts_env="VLLM_LOADER_SSH_OPTS",
    )

    client = _target_client_for_config()(target)

    assert isinstance(client, SubprocessTargetClient)
    assert client._command == [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-i",
        "/tmp/gpu-key",
        "-o",
        "ProxyJump=bastion",
        "bgconley@10.25.0.51",
        "cd /tank/repos/lab-tui && PATH=/tank/venvs/lab-tui/bin:$PATH vllm-loader agent connect",
    ]
