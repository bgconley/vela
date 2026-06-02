from __future__ import annotations

from pathlib import Path

import pytest

from vllm_loader.engine.sidecar import (
    Manifest,
    ProcessIdentity,
    Sidecar,
    TrackedProcessMismatch,
    command_hash,
    destructive_signal,
    verify_sidecar_identity,
)


def make_sidecar(tmp_path: Path) -> Sidecar:
    manifest_path = tmp_path / "run.manifest.json"
    return Sidecar(
        run_id="run-1",
        config_name="cfg",
        command_argv=["vllm", "serve", "org/model"],
        command_hash="sha256:abc",
        pid=100,
        pgid=100,
        process_create_time=123.0,
        executable="/bin/vllm",
        cwd=str(tmp_path),
        supervisor_pid=90,
        supervisor_create_time=122.0,
        supervisor_executable="/bin/python",
        launch_mode="detached",
        host="127.0.0.1",
        port=8000,
        served_model_names=["model"],
        exposure="local",
        manifest_path=str(manifest_path),
    )


def test_identity_verification_passes_for_matching_process_metadata(tmp_path: Path) -> None:
    sidecar = make_sidecar(tmp_path)
    child = ProcessIdentity(
        pid=100, create_time=123.0, pgid=100, executable="/bin/vllm", cmdline=sidecar.command_argv
    )
    supervisor = ProcessIdentity(
        pid=90, create_time=122.0, pgid=90, executable="/bin/python", cmdline=["python"]
    )

    assert verify_sidecar_identity(sidecar, child, supervisor)


def test_identity_accepts_executable_alias_when_command_line_matches(tmp_path: Path) -> None:
    sidecar = make_sidecar(tmp_path)
    sidecar.command_hash = command_hash(sidecar.command_argv)
    child = ProcessIdentity(
        pid=100,
        create_time=123.0,
        pgid=100,
        executable="/opt/homebrew/bin/python3.11",
        cmdline=sidecar.command_argv,
    )
    supervisor = ProcessIdentity(
        pid=90, create_time=122.0, pgid=90, executable="/bin/python", cmdline=["python"]
    )

    assert verify_sidecar_identity(sidecar, child, supervisor)


def test_recycled_pid_create_time_mismatch_rejected(tmp_path: Path) -> None:
    sidecar = make_sidecar(tmp_path)
    child = ProcessIdentity(
        pid=100, create_time=999.0, pgid=100, executable="/bin/vllm", cmdline=sidecar.command_argv
    )

    with pytest.raises(TrackedProcessMismatch):
        verify_sidecar_identity(sidecar, child, None)


def test_supervisor_identity_checked_for_detached_mode(tmp_path: Path) -> None:
    sidecar = make_sidecar(tmp_path)
    child = ProcessIdentity(
        pid=100, create_time=123.0, pgid=100, executable="/bin/vllm", cmdline=sidecar.command_argv
    )

    with pytest.raises(TrackedProcessMismatch):
        verify_sidecar_identity(sidecar, child, None)


def test_destructive_signal_path_reverifies_before_signaling(tmp_path: Path, monkeypatch) -> None:
    sidecar = make_sidecar(tmp_path)
    calls: list[tuple[int, int]] = []
    child = ProcessIdentity(
        pid=100, create_time=123.0, pgid=100, executable="/bin/vllm", cmdline=sidecar.command_argv
    )
    supervisor = ProcessIdentity(
        pid=90, create_time=122.0, pgid=90, executable="/bin/python", cmdline=["python"]
    )

    monkeypatch.setattr("os.killpg", lambda pgid, sig: calls.append((pgid, sig)))

    destructive_signal(sidecar, 15, child=child, supervisor=supervisor)

    assert calls == [(100, 15)]


def test_manifest_active_log_verification_and_rotation_update(tmp_path: Path) -> None:
    log = tmp_path / "run.log"
    log.write_text("hi", encoding="utf-8")
    manifest = Manifest.from_active_log(log)
    rotated = tmp_path / "run.log.1"

    manifest.rotate_to(rotated)

    assert manifest.active_log.path == str(rotated)
    assert manifest.rotated[0].path == str(log)


def test_sidecar_and_manifest_atomic_writes_are_private(tmp_path: Path) -> None:
    sidecar_path = tmp_path / "run.json"
    manifest_path = tmp_path / "run.manifest.json"
    log = tmp_path / "run.log"
    log.write_text("hi", encoding="utf-8")

    make_sidecar(tmp_path).write_atomic(sidecar_path)
    Manifest.from_active_log(log).write_atomic(manifest_path)

    assert sidecar_path.stat().st_mode & 0o777 == 0o600
    assert manifest_path.stat().st_mode & 0o777 == 0o600
