from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ErrorRemediation:
    label: str
    cause: str
    fix: str
    extra_lines: tuple[str, ...] = ()


def remediation_for_error(
    code: str,
    *,
    target_name: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> ErrorRemediation | None:
    detail_map = details or {}
    target = _target_name(target_name, detail_map)
    if code == "command-not-found":
        command = str(detail_map.get("command") or "vela")
        return ErrorRemediation(
            label="AGENT_NOT_INSTALLED",
            cause=f"agent command not found ({command})",
            fix=f"Fix: run `vela targets bootstrap {target} --install`.",
        )
    if code == "version-mismatch":
        return ErrorRemediation(
            label="AGENT_VERSION_MISMATCH",
            cause="agent/controller version mismatch",
            fix=(
                f"Fix: run `vela targets bootstrap {target} --install` "
                "to upgrade the target agent."
            ),
        )
    if code == "agent-unreachable":
        stderr = str(detail_map.get("stderr") or "").strip()
        reason = str(detail_map.get("reason") or "")
        cause = _agent_unreachable_cause(reason)
        extra_lines = (f"SSH stderr: {stderr}",) if stderr else ()
        return ErrorRemediation(
            label="AGENT_UNREACHABLE",
            cause=cause,
            fix=f"Fix: run `vela targets setup-ssh {target}`.",
            extra_lines=extra_lines,
        )
    if code == "feature-unavailable" and detail_map.get("reason") == "uv-required":
        return ErrorRemediation(
            label="UV_REQUIRED",
            cause="uv is required for this build method",
            fix=(
                f"Fix: run `vela build doctor --target {target}`; "
                "install uv on the target or choose pip, wheel, or git."
            ),
        )
    return None


def _target_name(target_name: str | None, details: Mapping[str, Any]) -> str:
    value = details.get("target") or details.get("target_name") or target_name or "local"
    rendered = str(value).strip()
    return rendered or "local"


def _agent_unreachable_cause(reason: str) -> str:
    return {
        "ssh-auth": "SSH auth failed",
        "ssh-host-key": "SSH host key verification failed",
        "ssh-name-resolution": "SSH host name did not resolve",
        "ssh-connect": "SSH connection failed",
        "ssh-failed": "SSH failed",
    }.get(reason, "target unreachable")
