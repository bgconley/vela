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
REQUIRED_SSH_OPTIONS = {
    "BatchMode": "yes",
    "ServerAliveInterval": "15",
    "ServerAliveCountMax": "3",
}
DEFAULT_SSH_CONTROL_OPTIONS = {
    "ControlMaster": "auto",
    "ControlPersist": "60s",
    "ControlPath": "~/.ssh/vllm-loader-%C",
}
_SAFE_SSH_FLAGS = {"-4", "-6", "-A", "-a", "-C", "-q", "-T", "-t", "-tt", "-x"}
_SAFE_SSH_VALUE_OPTIONS = {
    "-B",
    "-b",
    "-c",
    "-E",
    "-e",
    "-F",
    "-I",
    "-i",
    "-J",
    "-l",
    "-m",
    "-o",
    "-p",
    "-S",
}
_DISALLOWED_SSH_OPTIONS = {
    "-D",
    "-f",
    "-G",
    "-L",
    "-N",
    "-O",
    "-Q",
    "-R",
    "-s",
    "-V",
    "-W",
    "-w",
}
_DISALLOWED_SSH_OPTION_KEYS = {
    "localcommand",
    "permitlocalcommand",
    "proxycommand",
    "remotecommand",
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
    command = ["ssh"]
    ssh_opts = _ssh_options_from_env(target)
    command.extend(ssh_opts)
    for key, value in REQUIRED_SSH_OPTIONS.items():
        command.extend(["-o", f"{key}={value}"])
    for key, value in DEFAULT_SSH_CONTROL_OPTIONS.items():
        if not _ssh_option_present(ssh_opts, key):
            command.extend(["-o", f"{key}={value}"])
    command.append(target.host)
    command.append(_remote_agent_command(target, agent_command))
    return command


def _ssh_options_from_env(target: TargetConfig) -> list[str]:
    if not target.ssh_opts_env:
        return []
    options = shlex.split(os.environ.get(target.ssh_opts_env, ""))
    _validate_extra_ssh_options(options, source=target.ssh_opts_env)
    return options


def _validate_extra_ssh_options(options: Sequence[str], *, source: str) -> None:
    index = 0
    while index < len(options):
        option = options[index]
        if option == "--" or not option.startswith("-"):
            raise ValueError(
                f"{source} contains positional SSH argument {option!r}; "
                "only SSH options are allowed"
            )
        if _is_disallowed_ssh_option(option):
            raise ValueError(
                f"{source} contains unsupported SSH option {option!r}; "
                "forwarding, command-suppression, and query options are not allowed"
            )
        if option == "-o":
            if index + 1 >= len(options):
                raise ValueError(f"{source} option '-o' requires a value")
            _validate_ssh_option_assignment(options[index + 1], source=source)
            index += 2
            continue
        if option.startswith("-o") and len(option) > 2:
            _validate_ssh_option_assignment(option[2:], source=source)
            index += 1
            continue
        if option in _SAFE_SSH_VALUE_OPTIONS:
            if index + 1 >= len(options):
                raise ValueError(f"{source} option {option!r} requires a value")
            index += 2
            continue
        if _is_concatenated_safe_value_option(option) or _is_safe_ssh_flag(option):
            index += 1
            continue
        raise ValueError(
            f"{source} contains unsupported SSH option {option!r}; "
            "use -o Key=Value or a documented identity/proxy option"
        )


def _validate_ssh_option_assignment(value: str, *, source: str) -> None:
    key = _ssh_option_key(value)
    if key in _DISALLOWED_SSH_OPTION_KEYS:
        raise ValueError(
            f"{source} contains command-bearing SSH option {key!r}; "
            "command-bearing SSH options are not allowed"
        )


def _is_disallowed_ssh_option(option: str) -> bool:
    return option in _DISALLOWED_SSH_OPTIONS or option[:2] in _DISALLOWED_SSH_OPTIONS


def _is_concatenated_safe_value_option(option: str) -> bool:
    return any(
        option.startswith(prefix) and len(option) > len(prefix)
        for prefix in _SAFE_SSH_VALUE_OPTIONS
        if prefix != "-o"
    )


def _is_safe_ssh_flag(option: str) -> bool:
    return option in _SAFE_SSH_FLAGS or (
        len(option) > 1 and option[1:] and set(option[1:]) == {"v"}
    )


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
