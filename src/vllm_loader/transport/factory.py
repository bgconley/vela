from __future__ import annotations

import os
import shlex
from collections.abc import Callable, Sequence

from vllm_loader.agent.daemon import default_agent_socket_path
from vllm_loader.agent.local import LocalAgent
from vllm_loader.config.targets import LocalTransportKind, TargetConfig, TransportKind
from vllm_loader.transport.client import TargetClient
from vllm_loader.transport.inprocess import InProcessTargetClient
from vllm_loader.transport.socket import UnixSocketTargetClient
from vllm_loader.transport.subprocess import SubprocessTargetClient

DEFAULT_AGENT_COMMAND = ("vllm-loader", "agent", "connect")
DEFAULT_SSH_CONTROL_OPTIONS = {
    "ControlMaster": "auto",
    "ControlPersist": "60s",
    "ControlPath": "~/.ssh/vllm-loader-%C",
}


def target_client_for_config(
    target: TargetConfig,
    *,
    agent_command: Sequence[str] = DEFAULT_AGENT_COMMAND,
    local_agent_factory: Callable[..., LocalAgent] = LocalAgent,
) -> TargetClient:
    if target.transport is TransportKind.LOCAL:
        if target.local_transport is LocalTransportKind.SOCKET:
            return UnixSocketTargetClient(target.socket_path or default_agent_socket_path())
        return InProcessTargetClient(local_agent_factory(target_name=target.name))
    if target.transport is TransportKind.SSH:
        return SubprocessTargetClient(_ssh_agent_command(target, agent_command))
    raise ValueError(f"unsupported target transport: {target.transport}")


def _ssh_agent_command(target: TargetConfig, agent_command: Sequence[str]) -> list[str]:
    if target.host is None:
        raise ValueError(f"ssh target {target.name!r} requires host")
    command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
    ]
    ssh_opts = (
        shlex.split(os.environ.get(target.ssh_opts_env, ""))
        if target.ssh_opts_env
        else []
    )
    command.extend(ssh_opts)
    for key, value in DEFAULT_SSH_CONTROL_OPTIONS.items():
        if not _ssh_option_present(ssh_opts, key):
            command.extend(["-o", f"{key}={value}"])
    command.append(target.host)
    command.append(_remote_agent_command(target, agent_command))
    return command


def _remote_agent_command(target: TargetConfig, agent_command: Sequence[str]) -> str:
    command = " ".join(shlex.quote(str(part)) for part in agent_command)
    if target.venv is not None:
        command = f"PATH={shlex.quote(str(target.venv / 'bin'))}:$PATH {command}"
    if target.workdir is not None:
        command = f"cd {shlex.quote(str(target.workdir))} && {command}"
    return command


def _ssh_option_present(options: Sequence[str], key: str) -> bool:
    needle = key.lower()
    for index, option in enumerate(options):
        if option == "-o" and index + 1 < len(options):
            if _ssh_option_key(options[index + 1]) == needle:
                return True
        elif option.startswith("-o") and _ssh_option_key(option[2:]) == needle:
            return True
    return False


def _ssh_option_key(value: str) -> str:
    return value.split("=", 1)[0].strip().lower()
