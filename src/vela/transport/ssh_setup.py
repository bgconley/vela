from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from vela.agent.local import TargetCallError
from vela.config.targets import TargetConfig, TransportKind


@dataclass(frozen=True)
class SshSetupResult:
    stdout: str
    stderr: str


def setup_ssh_key(
    target: TargetConfig,
    *,
    identity_file: Path | None = None,
) -> SshSetupResult:
    if target.transport is not TransportKind.SSH:
        raise ValueError("setup-ssh requires an ssh target")
    if target.host is None:
        raise ValueError(f"ssh target {target.name!r} requires host")
    command = ["ssh-copy-id"]
    key = identity_file or target.ssh_key
    if key is not None:
        command.extend(["-i", str(key)])
    command.append(target.host)
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError as exc:
        raise TargetCallError(
            "feature-unavailable",
            "ssh-copy-id is required to set up SSH keys",
            {"reason": "ssh-copy-id-required", "command": "ssh-copy-id"},
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise TargetCallError(
            "agent-unreachable",
            "ssh-copy-id timed out",
            {"target": target.name, "timeout_seconds": 60},
        ) from exc
    stderr = completed.stderr or ""
    if completed.returncode != 0:
        raise TargetCallError(
            "ssh-setup-failed",
            "ssh-copy-id failed",
            {
                "target": target.name,
                "exit_code": completed.returncode,
                "stderr": stderr.strip(),
            },
        )
    return SshSetupResult(stdout=completed.stdout or "", stderr=stderr)
