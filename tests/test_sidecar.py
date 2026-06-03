from __future__ import annotations

import signal
from pathlib import Path

import pytest

import vllm_loader.engine.sidecar as sidecar_module
from vllm_loader.engine.sidecar import (
    Manifest,
    ProcessIdentity,
    Sidecar,
    TrackedProcessMismatch,
    command_hash,
    destructive_signal,
    stop_sidecar_from_system,
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


def test_identity_accepts_python_interpreter_alias_when_script_args_match(
    tmp_path: Path,
) -> None:
    script = str(tmp_path / "fake_vllm_child.py")
    sidecar = make_sidecar(tmp_path)
    sidecar.command_argv = ["python3", script, "serve", "fake/model"]
    sidecar.command_hash = command_hash(sidecar.command_argv)
    child = ProcessIdentity(
        pid=100,
        create_time=123.0,
        pgid=100,
        executable="/opt/homebrew/Cellar/python@3.11/3.11.14/bin/python3.11",
        cmdline=[
            "/System/Library/Frameworks/Python.framework/Versions/3.11/Resources/"
            "Python.app/Contents/MacOS/Python",
            script,
            "serve",
            "fake/model",
        ],
    )
    supervisor = ProcessIdentity(
        pid=90, create_time=122.0, pgid=90, executable="/bin/python", cmdline=["python"]
    )

    assert verify_sidecar_identity(sidecar, child, supervisor)


def test_identity_accepts_python_alias_when_recorded_secret_args_are_redacted(
    tmp_path: Path,
) -> None:
    script = str(tmp_path / "fake_vllm_child.py")
    original = ["python3", script, "serve", "fake/model", "--api-key-copy", "literal-api-key"]
    sidecar = make_sidecar(tmp_path)
    sidecar.command_argv = [
        "python3",
        script,
        "serve",
        "fake/model",
        "--api-key-copy",
        "••••",
    ]
    sidecar.command_hash = command_hash(original)
    child = ProcessIdentity(
        pid=100,
        create_time=123.0,
        pgid=100,
        executable="/opt/homebrew/Cellar/python@3.11/3.11.14/bin/python3.11",
        cmdline=[
            "/System/Library/Frameworks/Python.framework/Versions/3.11/Resources/"
            "Python.app/Contents/MacOS/Python",
            script,
            "serve",
            "fake/model",
            "--api-key-copy",
            "literal-api-key",
        ],
    )
    supervisor = ProcessIdentity(
        pid=90, create_time=122.0, pgid=90, executable="/bin/python", cmdline=["python"]
    )

    assert verify_sidecar_identity(sidecar, child, supervisor)


def test_identity_accepts_python_script_cmdline_when_interpreter_is_omitted(
    tmp_path: Path,
) -> None:
    script = str(tmp_path / "fake_vllm_child.py")
    sidecar = make_sidecar(tmp_path)
    sidecar.command_argv = ["python3", script, "serve", "fake/model"]
    sidecar.command_hash = command_hash(sidecar.command_argv)
    child = ProcessIdentity(
        pid=100,
        create_time=123.0,
        pgid=100,
        executable="/opt/homebrew/Cellar/python@3.11/3.11.14/bin/python3.11",
        cmdline=[script, "serve", "fake/model"],
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


def test_procfs_starttime_is_parsed_from_linux_stat_with_spaced_process_name(
    tmp_path: Path,
) -> None:
    proc_root = tmp_path / "proc"
    proc_dir = proc_root / "123"
    proc_dir.mkdir(parents=True)
    # Fields are: pid, comm, then 3..22. Start time is field 22.
    stat_fields_after_comm = ["S", *["0"] * 18, "8675309"]
    (proc_dir / "stat").write_text(
        f"123 (python worker) {' '.join(stat_fields_after_comm)}\n",
        encoding="utf-8",
    )

    assert hasattr(sidecar_module, "procfs_starttime_from_pid")
    assert sidecar_module.procfs_starttime_from_pid(123, proc_root=proc_root) == 8675309


def test_procfs_starttime_is_unavailable_off_linux_procfs(tmp_path: Path) -> None:
    assert hasattr(sidecar_module, "procfs_starttime_from_pid")
    assert (
        sidecar_module.procfs_starttime_from_pid(123, proc_root=tmp_path / "missing-proc")
        is None
    )


def test_child_procfs_starttime_mismatch_is_rejected(tmp_path: Path) -> None:
    sidecar = make_sidecar(tmp_path)
    sidecar.procfs_starttime = 8675309
    child = ProcessIdentity(
        pid=100,
        create_time=123.0,
        pgid=100,
        executable="/bin/vllm",
        cmdline=sidecar.command_argv,
        procfs_starttime=8675310,
    )
    supervisor = ProcessIdentity(
        pid=90,
        create_time=122.0,
        pgid=90,
        executable="/bin/python",
        cmdline=["python"],
        procfs_starttime=None,
    )

    with pytest.raises(TrackedProcessMismatch, match="procfs starttime"):
        verify_sidecar_identity(sidecar, child, supervisor)


def test_supervisor_identity_checked_for_detached_mode(tmp_path: Path) -> None:
    sidecar = make_sidecar(tmp_path)
    child = ProcessIdentity(
        pid=100, create_time=123.0, pgid=100, executable="/bin/vllm", cmdline=sidecar.command_argv
    )

    with pytest.raises(TrackedProcessMismatch):
        verify_sidecar_identity(sidecar, child, None)


def test_supervisor_procfs_starttime_mismatch_is_rejected(tmp_path: Path) -> None:
    sidecar = make_sidecar(tmp_path)
    sidecar.supervisor_procfs_starttime = 8675280
    child = ProcessIdentity(
        pid=100, create_time=123.0, pgid=100, executable="/bin/vllm", cmdline=sidecar.command_argv
    )
    supervisor = ProcessIdentity(
        pid=90,
        create_time=122.0,
        pgid=90,
        executable="/bin/python",
        cmdline=["python"],
        procfs_starttime=8675281,
    )

    with pytest.raises(TrackedProcessMismatch, match="supervisor procfs starttime"):
        verify_sidecar_identity(sidecar, child, supervisor)


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


def test_stop_sidecar_waits_after_final_sigkill(tmp_path: Path, monkeypatch) -> None:
    sidecar_path = tmp_path / "run.json"
    sidecar = make_sidecar(tmp_path)
    sidecar.write_atomic(sidecar_path)
    signals: list[int] = []
    waits: list[float] = []

    def signal_sidecar(_path: Path, signal_number: int) -> None:
        signals.append(signal_number)

    def wait_process(_pid: int, _create_time: float, timeout: float) -> bool:
        waits.append(timeout)
        return len(waits) >= 3

    monkeypatch.setattr(
        "vllm_loader.engine.sidecar.signal_sidecar_from_system",
        signal_sidecar,
    )
    monkeypatch.setattr("vllm_loader.engine.sidecar._wait_process_exit", wait_process)

    stop_sidecar_from_system(sidecar_path, interrupt_timeout=0.1, terminate_timeout=0.2)

    assert signals == [signal.SIGINT, signal.SIGTERM, signal.SIGKILL]
    assert waits == [0.1, 0.2, 0.2]


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
