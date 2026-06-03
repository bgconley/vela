from __future__ import annotations

import os
import shlex
from collections.abc import Callable, Sequence

from vllm_loader.agent.local import LocalAgent
from vllm_loader.config.targets import TargetConfig, TransportKind
from vllm_loader.transport.client import TargetClient
from vllm_loader.transport.inprocess import InProcessTargetClient
from vllm_loader.transport.subprocess import SubprocessTargetClient

DEFAULT_AGENT_COMMAND = ("vllm-loader", "agent", "connect")


def target_client_for_config(
    target: TargetConfig,
    *,
    agent_command: Sequence[str] = DEFAULT_AGENT_COMMAND,
    local_agent_factory: Callable[..., LocalAgent] = LocalAgent,
) -> TargetClient:
    if target.transport is TransportKind.LOCAL:
        return InProcessTargetClient(local_agent_factory(target_name=target.name))
    if target.transport is TransportKind.SSH:
        return SubprocessTargetClient(_ssh_agent_command(target, agent_command))
    raise ValueError(f"unsupported target transport: {target.transport}")


def _ssh_agent_command(target: TargetConfig, agent_command: Sequence[str]) -> list[str]:
    if target.host is None:
        raise ValueError(f"ssh target {target.name!r} requires host")
    command = ["ssh", "-o", "BatchMode=yes"]
    if target.ssh_opts_env:
        command.extend(shlex.split(os.environ.get(target.ssh_opts_env, "")))
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
