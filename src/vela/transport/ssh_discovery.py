from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from vela import __version__
from vela.agent.local import TargetCallError
from vela.config.targets import TargetConfig, TransportKind
from vela.transport.factory import ssh_command_for_target

SSH_DISCOVERY_TIMEOUT_SECONDS = 10.0
CANONICAL_AGENT_PATH_EXPR = "$HOME/.local/share/vela/venv/bin/vela"
USER_VENV_AGENT_PATH_EXPR = "$HOME/venvs/vela/bin/vela"


@dataclass(frozen=True)
class AgentDiscoveryResult:
    agent_command: list[str]
    source: str
    version: str


@dataclass(frozen=True)
class _ProbeResult:
    returncode: int
    stdout: str
    stderr: str


def discover_ssh_agent_command(target: TargetConfig) -> AgentDiscoveryResult:
    if target.transport is not TransportKind.SSH:
        raise ValueError("agent discovery requires an ssh target")
    mismatches: list[dict[str, str]] = []

    command_v = _run_ssh_probe(target, "command -v vela")
    if command_v.returncode == 0:
        path = _first_stdout_line(command_v.stdout)
        if path:
            result = _probe_versioned_path(
                target,
                path=path,
                source="command-v",
                mismatches=mismatches,
            )
            if result is not None:
                return result

    for source, path_expr in _path_candidates(target):
        result = _probe_candidate_expression(
            target,
            source=source,
            path_expr=path_expr,
            mismatches=mismatches,
        )
        if result is not None:
            return result

    result = _probe_python_module(target, mismatches=mismatches)
    if result is not None:
        return result

    if mismatches:
        mismatch = mismatches[0]
        raise TargetCallError(
            "version-mismatch",
            (
                "target Vela agent version "
                f"{mismatch['version']} is not compatible with controller {__version__}"
            ),
            {
                "agent_version": mismatch["version"],
                "controller_version": __version__,
                "candidate": mismatch["candidate"],
                "mismatches": mismatches,
            },
        )
    raise TargetCallError(
        "command-not-found",
        "Target agent command not found: vela",
        {
            "command": "vela",
            "target": target.name,
            "remediation": f"vela targets bootstrap {target.name} --install",
        },
    )


def _path_candidates(target: TargetConfig) -> list[tuple[str, str]]:
    candidates = [
        ("canonical-venv", CANONICAL_AGENT_PATH_EXPR),
        ("user-venv", USER_VENV_AGENT_PATH_EXPR),
    ]
    if target.venv is not None:
        candidates.append(("target-venv", str(target.venv / "bin" / "vela")))
    return candidates


def _probe_versioned_path(
    target: TargetConfig,
    *,
    path: str,
    source: str,
    mismatches: list[dict[str, str]],
) -> AgentDiscoveryResult | None:
    version_probe = _run_ssh_probe(target, f"{_shell_quote(path)} --version")
    if version_probe.returncode != 0:
        return None
    version = _first_stdout_line(version_probe.stdout)
    if not version:
        return None
    if not _compatible_version(version):
        mismatches.append({"candidate": path, "version": version, "source": source})
        return None
    return AgentDiscoveryResult(
        agent_command=[path, "agent", "connect"],
        source=source,
        version=version,
    )


def _probe_candidate_expression(
    target: TargetConfig,
    *,
    source: str,
    path_expr: str,
    mismatches: list[dict[str, str]],
) -> AgentDiscoveryResult | None:
    result = _run_ssh_probe(target, _candidate_expression_probe(path_expr))
    if result.returncode != 0:
        return None
    lines = _stdout_lines(result.stdout)
    if len(lines) < 2:
        return None
    version, path = lines[0], lines[-1]
    if not _compatible_version(version):
        mismatches.append({"candidate": path, "version": version, "source": source})
        return None
    return AgentDiscoveryResult(
        agent_command=[path, "agent", "connect"],
        source=source,
        version=version,
    )


def _candidate_expression_probe(path_expr: str) -> str:
    assignment = (
        f"candidate={path_expr}"
        if path_expr.startswith("$HOME/")
        else f"candidate={_shell_quote(path_expr)}"
    )
    return (
        f'{assignment}; if [ -x "$candidate" ]; then '
        '"$candidate" --version; printf \'\\n%s\\n\' "$candidate"; fi'
    )


def _probe_python_module(
    target: TargetConfig,
    *,
    mismatches: list[dict[str, str]],
) -> AgentDiscoveryResult | None:
    result = _run_ssh_probe(target, "python3 -m vela --version")
    if result.returncode != 0:
        return None
    version = _first_stdout_line(result.stdout)
    if not version:
        return None
    if not _compatible_version(version):
        mismatches.append(
            {"candidate": "python3 -m vela", "version": version, "source": "python-module"}
        )
        return None
    return AgentDiscoveryResult(
        agent_command=["python3", "-m", "vela", "agent", "connect"],
        source="python-module",
        version=version,
    )


def _run_ssh_probe(target: TargetConfig, remote_command: str) -> _ProbeResult:
    command = ssh_command_for_target(target, remote_command)
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=SSH_DISCOVERY_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise TargetCallError(
            "agent-unreachable",
            "SSH target agent discovery timed out",
            {"target": target.name, "timeout_seconds": SSH_DISCOVERY_TIMEOUT_SECONDS},
        ) from exc
    except OSError as exc:
        raise TargetCallError(
            "agent-unreachable",
            f"Unable to run ssh for target discovery: {exc}",
            {"target": target.name},
        ) from exc
    stderr = completed.stderr or ""
    if completed.returncode == 255:
        raise TargetCallError(
            "agent-unreachable",
            "SSH target agent discovery failed",
            {
                "target": target.name,
                "reason": _ssh_failure_reason(stderr),
                "stderr": stderr.strip(),
            },
        )
    return _ProbeResult(
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=stderr,
    )


def _stdout_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _first_stdout_line(text: str) -> str:
    lines = _stdout_lines(text)
    return lines[0] if lines else ""


def _compatible_version(version: str) -> bool:
    return version.strip() == __version__


def _ssh_failure_reason(stderr: str) -> str:
    lowered = stderr.lower()
    if "permission denied" in lowered:
        return "ssh-auth"
    if "host key verification failed" in lowered:
        return "ssh-host-key"
    if "could not resolve hostname" in lowered:
        return "ssh-name-resolution"
    return "ssh-failed"


def _shell_quote(value: str | Path) -> str:
    import shlex

    return shlex.quote(str(value))
