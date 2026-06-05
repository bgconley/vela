from __future__ import annotations

from vela.agent.local import BUILD_INSTALL_PHASE_RULES, _build_install_phase_for_line
from vela.engine.job_phases import BuildPhase, DownloadPhase


def test_build_phase_enum_matches_spec_values() -> None:
    assert [phase.value for phase in BuildPhase] == [
        "RESOLVING",
        "DOWNLOADING",
        "BUILDING",
        "INSTALLING",
        "VERIFYING",
        "READY",
        "FAILED",
    ]


def test_download_phase_enum_matches_spec_values() -> None:
    assert [phase.value for phase in DownloadPhase] == [
        "RESOLVING",
        "DOWNLOADING",
        "VERIFYING",
        "READY",
        "FAILED",
    ]


def test_build_install_phase_rules_are_enum_backed_wire_values() -> None:
    assert all(isinstance(phase, BuildPhase) for _pattern, phase in BUILD_INSTALL_PHASE_RULES)
    assert _build_install_phase_for_line("Collecting vllm", "INSTALLING") == (
        BuildPhase.DOWNLOADING.value
    )
