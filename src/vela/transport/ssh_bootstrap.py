from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass

from vela.agent.local import TargetCallError
from vela.config.targets import TargetConfig, TransportKind
from vela.transport.factory import ssh_command_for_target
from vela.transport.ssh_discovery import (
    CANONICAL_AGENT_PATH_EXPR,
    SSH_DISCOVERY_TIMEOUT_SECONDS,
)

DEFAULT_AGENT_INSTALL_SPEC = "vela @ git+https://github.com/bgconley/vela.git"


@dataclass(frozen=True)
class AgentInstallResult:
    stdout: str
    stderr: str


def install_ssh_agent(
    target: TargetConfig,
    *,
    install_spec: str = DEFAULT_AGENT_INSTALL_SPEC,
) -> AgentInstallResult:
    if target.transport is not TransportKind.SSH:
        raise ValueError("agent install requires an ssh target")
    remote_command = _install_command(install_spec)
    try:
        completed = subprocess.run(
            ssh_command_for_target(target, remote_command),
            check=False,
            capture_output=True,
            text=True,
            timeout=SSH_DISCOVERY_TIMEOUT_SECONDS * 6,
        )
    except subprocess.TimeoutExpired as exc:
        raise TargetCallError(
            "agent-unreachable",
            "SSH target agent install timed out",
            {"target": target.name, "timeout_seconds": SSH_DISCOVERY_TIMEOUT_SECONDS * 6},
        ) from exc
    except OSError as exc:
        raise TargetCallError(
            "agent-unreachable",
            f"Unable to run ssh for target agent install: {exc}",
            {"target": target.name},
        ) from exc
    stderr = completed.stderr or ""
    if completed.returncode == 255:
        raise TargetCallError(
            "agent-unreachable",
            "SSH target agent install failed",
            {
                "target": target.name,
                "reason": _ssh_failure_reason(stderr),
                "stderr": stderr.strip(),
            },
        )
    if completed.returncode != 0:
        raise TargetCallError(
            "agent-install-failed",
            "remote target agent install failed",
            {
                "target": target.name,
                "exit_code": completed.returncode,
                "stderr": stderr.strip(),
            },
        )
    return AgentInstallResult(stdout=completed.stdout or "", stderr=stderr)


def _install_command(install_spec: str) -> str:
    venv_expr = CANONICAL_AGENT_PATH_EXPR.removesuffix("/bin/vela")
    python_expr = f"{venv_expr}/bin/python"
    return (
        "set -e; "
        f'python3 -m venv "{venv_expr}"; '
        f'"{python_expr}" -m pip install --upgrade pip; '
        f'"{python_expr}" -m pip install --upgrade {shlex.quote(install_spec)}; '
        f'"{CANONICAL_AGENT_PATH_EXPR}" --version'
    )


def _ssh_failure_reason(stderr: str) -> str:
    lowered = stderr.lower()
    if "permission denied" in lowered:
        return "ssh-auth"
    if "host key verification failed" in lowered:
        return "ssh-host-key"
    if "could not resolve hostname" in lowered:
        return "ssh-name-resolution"
    return "ssh-failed"
