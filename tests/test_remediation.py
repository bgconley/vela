from __future__ import annotations

from vela.remediation import remediation_for_error


def test_agent_not_installed_remediation_names_bootstrap_command() -> None:
    remediation = remediation_for_error(
        "command-not-found",
        target_name="blackbird",
        details={"command": "vela"},
    )

    assert remediation is not None
    assert remediation.label == "AGENT_NOT_INSTALLED"
    assert "vela targets bootstrap blackbird --install" in remediation.fix


def test_agent_unreachable_remediation_names_setup_ssh_and_stderr() -> None:
    remediation = remediation_for_error(
        "agent-unreachable",
        target_name="blackbird",
        details={"reason": "ssh-auth", "stderr": "Permission denied (publickey)."},
    )

    assert remediation is not None
    assert remediation.label == "AGENT_UNREACHABLE"
    assert remediation.cause == "SSH auth failed"
    assert "Permission denied (publickey)." in remediation.extra_lines[0]
    assert "vela targets setup-ssh blackbird" in remediation.fix


def test_version_mismatch_remediation_names_bootstrap_command() -> None:
    remediation = remediation_for_error("version-mismatch", target_name="blackbird")

    assert remediation is not None
    assert remediation.label == "AGENT_VERSION_MISMATCH"
    assert "vela targets bootstrap blackbird --install" in remediation.fix


def test_uv_required_remediation_names_build_doctor_command() -> None:
    remediation = remediation_for_error(
        "feature-unavailable",
        target_name="blackbird",
        details={"reason": "uv-required", "method": "nightly"},
    )

    assert remediation is not None
    assert remediation.label == "UV_REQUIRED"
    assert "vela build doctor --target blackbird" in remediation.fix
