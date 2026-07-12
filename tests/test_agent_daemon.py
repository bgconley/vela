from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import psutil
import pytest
from conftest import scaled_timeout

from vela.agent.local import LocalAgent
from vela.engine.sidecar import procfs_starttime_from_pid
from vela.transport.subprocess import SubprocessTargetClient


def _short_socket_path() -> Path:
    return Path("/tmp") / f"vela-daemon-{uuid.uuid4().hex}" / "agent.sock"


def _bind_live_daemon(socket_path: Path) -> socket.socket:
    """Bind a real socket + write an identity naming THIS process as the daemon."""
    from vela.agent.daemon import agent_identity_path

    socket_path.parent.mkdir(parents=True, exist_ok=True)
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(socket_path))
    agent_identity_path(socket_path).write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "create_time": psutil.Process(os.getpid()).create_time(),
                "procfs_starttime": procfs_starttime_from_pid(os.getpid()),
                "socket_path": str(socket_path),
                "version": "0.1.0",
                "protocol_versions": [1],
            }
        ),
        encoding="utf-8",
    )
    return sock


def _cleanup_live_daemon(socket_path: Path) -> None:
    from vela.agent.daemon import agent_identity_path

    socket_path.unlink(missing_ok=True)
    agent_identity_path(socket_path).unlink(missing_ok=True)


def test_stale_local_daemon_banner_flags_version_drift() -> None:
    from vela.agent.daemon import stale_local_daemon_banner

    banner = stale_local_daemon_banner(
        {"agent_version": "0.0.9", "daemon_start_ts": "2026-06-09T12:00:00Z"},
        controller_version="0.1.0",
        controller_revision=None,
    )

    assert banner == (
        "local daemon is running vela 0.0.9 (started 2026-06-09) "
        "— restart with: vela agent restart"
    )


def test_stale_local_daemon_banner_flags_revision_drift_same_version() -> None:
    # The month-stale trap: __version__ is a static string, so a same-version
    # daemon running an older commit must still be caught via git-describe drift.
    from vela.agent.daemon import stale_local_daemon_banner

    banner = stale_local_daemon_banner(
        {
            "agent_version": "0.1.0",
            "agent_revision": "v0.1.0-40-gabc0000",
            "daemon_start_ts": "2026-06-09T12:00:00Z",
        },
        controller_version="0.1.0",
        controller_revision="v0.1.0-77-g75ebb73",
    )

    assert banner is not None
    assert banner.startswith(
        "local daemon is running vela 0.1.0 (v0.1.0-40-gabc0000) (started 2026-06-09)"
    )
    assert banner.endswith("— restart with: vela agent restart")


def test_stale_local_daemon_banner_none_when_matching() -> None:
    from vela.agent.daemon import stale_local_daemon_banner

    assert (
        stale_local_daemon_banner(
            {"agent_version": "0.1.0", "agent_revision": "v0.1.0-77-g75ebb73"},
            controller_version="0.1.0",
            controller_revision="v0.1.0-77-g75ebb73",
        )
        is None
    )


def test_stale_local_daemon_banner_none_when_revision_unavailable() -> None:
    # Best-effort: a released wheel has no git-describe on either side, so a
    # matching __version__ must not false-positive (revision drift needs both).
    from vela.agent.daemon import stale_local_daemon_banner

    assert (
        stale_local_daemon_banner(
            {"agent_version": "0.1.0"},
            controller_version="0.1.0",
            controller_revision=None,
        )
        is None
    )


def test_start_agent_daemon_failure_captures_stderr_log_and_names_it() -> None:
    # bug-238: a silent start-failure must leave a readable log instead of vanishing
    # into DEVNULL, and the start-failed error must name it. A socket path that
    # exceeds the macOS AF_UNIX limit forces the daemon to fail to bind and exit
    # non-zero (the runtime dir + regular err file allow far longer paths).
    from vela.agent.daemon import agent_start_err_path, start_agent_daemon_process

    long_dir = Path("/tmp") / ("d" * 90)
    socket_path = long_dir / "agent.sock"  # > 104 chars total → bind fails
    try:
        status = start_agent_daemon_process(socket_path, timeout=scaled_timeout(6))

        assert status["status"] == "start-failed"
        stderr_log = Path(str(status["stderr_log"]))
        assert stderr_log == agent_start_err_path(socket_path)
        assert stderr_log.exists()
        # The captured stderr names the bind failure, not an empty file.
        assert stderr_log.read_text(encoding="utf-8").strip()
    finally:
        shutil.rmtree(long_dir, ignore_errors=True)


def test_isolation_fixture_clears_shell_vela_agent_runtime_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # bug-294: VELA_AGENT_RUNTIME_DIR is D5's TOP precedence, so a developer shell
    # exporting it (docs now advertise it) would beat the isolated XDG_RUNTIME_DIR
    # and escape suite isolation entirely — sockets would resolve in the real dir
    # and the session teardown would `vela agent stop` against it. The isolation
    # fixture must POP it (the _VELA_STATE_ENV_KEYS snapshot already restores it).
    import conftest as conftest_module

    from vela.agent import daemon as daemon_module

    shell_override = Path("/tmp") / f"vela-shell-{uuid.uuid4().hex}"
    monkeypatch.setenv("VELA_AGENT_RUNTIME_DIR", str(shell_override))

    # Drive the session fixture's generator directly (__wrapped__ = the raw
    # generator under pytest's direct-call guard) to simulate a session that
    # starts with the shell export in place.
    fixture_gen = conftest_module.isolated_vela_state.__wrapped__()
    state_root = next(fixture_gen)
    try:
        assert os.environ.get("VELA_AGENT_RUNTIME_DIR") is None
        resolved = daemon_module.default_agent_socket_path()
        assert resolved == state_root / "runtime" / "vela" / "agent.sock"
    finally:
        try:
            next(fixture_gen)
        except StopIteration:
            pass

    # Teardown restored the shell value for the caller's environment.
    assert os.environ.get("VELA_AGENT_RUNTIME_DIR") == str(shell_override)


def test_default_agent_runtime_dir_precedence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # D5: VELA_AGENT_RUNTIME_DIR > XDG_RUNTIME_DIR > $XDG_STATE_HOME/vela >
    # ~/.local/state/vela. Setting XDG_STATE_HOME alone must now isolate the socket
    # dir (bug-238: it previously fell through to the shared ~/.local/state daemon).
    from vela.agent import daemon as daemon_module

    explicit = tmp_path / "explicit"
    runtime = tmp_path / "run"
    state = tmp_path / "state"
    home = tmp_path / "home"

    # 1. VELA_AGENT_RUNTIME_DIR wins and is used verbatim (vela-specific, no /vela).
    monkeypatch.setenv("VELA_AGENT_RUNTIME_DIR", str(explicit))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    assert daemon_module.default_agent_runtime_dir() == explicit

    # 2. XDG_RUNTIME_DIR next (shared runtime dir → /vela subdir).
    monkeypatch.delenv("VELA_AGENT_RUNTIME_DIR", raising=False)
    assert daemon_module.default_agent_runtime_dir() == runtime / "vela"

    # 3. $XDG_STATE_HOME/vela next — the isolation gap this closes.
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert daemon_module.default_agent_runtime_dir() == state / "vela"

    # 4. ~/.local/state/vela is the last resort.
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(home))
    assert daemon_module.default_agent_runtime_dir() == home / ".local" / "state" / "vela"


def test_inspect_and_resolve_fall_back_to_running_legacy_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Compat: the new resolved runtime dir has no daemon, but a live daemon sits on
    # the legacy path. inspect/resolve must return the legacy one so an existing
    # daemon isn't orphaned mid-upgrade, and `agent status` shows the in-use socket.
    # Short /tmp paths only (macOS caps AF_UNIX paths at ~104 chars).
    from vela.agent import daemon as daemon_module

    base = Path("/tmp") / f"vela-d5-{uuid.uuid4().hex}"
    primary_dir = base / "run"
    legacy_socket = base / "legacy" / "agent.sock"
    monkeypatch.setenv("VELA_AGENT_RUNTIME_DIR", str(primary_dir))
    monkeypatch.setattr(daemon_module, "legacy_agent_socket_path", lambda: legacy_socket)
    sock = _bind_live_daemon(legacy_socket)
    try:
        assert daemon_module.resolve_default_agent_socket_path() == legacy_socket
        status = daemon_module.inspect_agent_daemon()
        assert status["status"] == "running"
        assert status["socket_path"] == str(legacy_socket)
    finally:
        sock.close()
        _cleanup_live_daemon(legacy_socket)
        shutil.rmtree(base, ignore_errors=True)


def test_resolve_prefers_running_primary_over_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # When the primary (new) socket has a live daemon, the legacy probe is never
    # consulted — the new canonical path wins. Short /tmp paths only (socket cap).
    from vela.agent import daemon as daemon_module

    base = Path("/tmp") / f"vela-d5-{uuid.uuid4().hex}"
    primary_dir = base / "run"
    primary_socket = primary_dir / "agent.sock"
    legacy_socket = base / "legacy" / "agent.sock"
    monkeypatch.setenv("VELA_AGENT_RUNTIME_DIR", str(primary_dir))
    monkeypatch.setattr(daemon_module, "legacy_agent_socket_path", lambda: legacy_socket)
    primary_sock = _bind_live_daemon(primary_socket)
    legacy_sock = _bind_live_daemon(legacy_socket)
    try:
        assert daemon_module.resolve_default_agent_socket_path() == primary_socket
    finally:
        primary_sock.close()
        legacy_sock.close()
        _cleanup_live_daemon(primary_socket)
        _cleanup_live_daemon(legacy_socket)
        shutil.rmtree(base, ignore_errors=True)


def _agent_connect_socket_command(socket_path: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "vela.cli",
        "agent",
        "connect",
        "--socket",
        str(socket_path),
    ]


def _agent_run_socket_command(socket_path: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "vela.cli",
        "agent",
        "run",
        "--socket",
        str(socket_path),
    ]


def _agent_run_idle_socket_command(socket_path: Path, idle_timeout: float) -> list[str]:
    return [
        *_agent_run_socket_command(socket_path),
        "--idle-timeout",
        str(idle_timeout),
    ]


def _agent_start_json_command(socket_path: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "vela.cli",
        "agent",
        "start",
        "--socket",
        str(socket_path),
        "--json",
    ]


def _agent_status_json_command(socket_path: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "vela.cli",
        "agent",
        "status",
        "--socket",
        str(socket_path),
        "--json",
    ]


def _agent_stop_json_command(socket_path: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "vela.cli",
        "agent",
        "stop",
        "--socket",
        str(socket_path),
        "--json",
    ]


def _agent_restart_json_command(socket_path: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "vela.cli",
        "agent",
        "restart",
        "--socket",
        str(socket_path),
        "--json",
    ]


def test_agent_stop_escalates_when_daemon_ignores_sigterm() -> None:
    from vela.agent.daemon import agent_identity_path, stop_agent_daemon

    socket_path = _short_socket_path()
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    sock = socket.socket(socket.AF_UNIX)
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import signal, sys, time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "print('ready', flush=True)\n"
                "time.sleep(60)\n"
            ),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline() == b"ready\n"
        sock.bind(str(socket_path))
        identity_path = agent_identity_path(socket_path)
        identity_path.write_text(
            json.dumps(
                {
                    "pid": process.pid,
                    "create_time": psutil.Process(process.pid).create_time(),
                    "procfs_starttime": procfs_starttime_from_pid(process.pid),
                    "socket_path": str(socket_path),
                    "version": "test",
                    "protocol_versions": [1],
                }
            ),
            encoding="utf-8",
        )

        result = stop_agent_daemon(socket_path, timeout=0.1)

        assert result["status"] == "stopped"
        assert result["signal"] == "SIGKILL"
        assert process.wait(timeout=scaled_timeout(2)) != 0
    finally:
        sock.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=scaled_timeout(2))
        socket_path.unlink(missing_ok=True)
        agent_identity_path(socket_path).unlink(missing_ok=True)


def test_systemd_user_unit_runs_foreground_agent_daemon() -> None:
    service_path = Path(__file__).parents[1] / "packaging" / "systemd" / (
        "vela-agent.service"
    )

    service = service_path.read_text(encoding="utf-8")

    assert "[Unit]" in service
    assert "Description=Vela target agent daemon" in service
    assert "After=network.target" not in service
    assert "[Service]" in service
    assert "Type=simple" in service
    assert "ExecStart=vela agent run" in service
    assert "Restart=on-failure" in service
    assert "[Install]" in service
    assert "WantedBy=default.target" in service


@pytest.mark.asyncio
async def test_foreground_daemon_writes_identity_and_serves_socket() -> None:
    from vela.agent.daemon import agent_identity_path, start_agent_daemon

    socket_path = _short_socket_path()
    identity_path = agent_identity_path(socket_path)
    daemon = await start_agent_daemon(
        LocalAgent(target_name="daemon-local"),
        socket_path=socket_path,
    )
    client = SubprocessTargetClient(_agent_connect_socket_command(socket_path))
    try:
        identity = json.loads(identity_path.read_text(encoding="utf-8"))

        assert identity["pid"] == os.getpid()
        assert isinstance(identity["create_time"], float)
        assert "start_ts" in identity
        assert identity["version"]
        assert identity["protocol_versions"] == [1]
        assert identity["socket_path"] == str(socket_path)

        connected = await client.connect()

        assert connected["target"] == "daemon-local"
        assert connected["daemon_pid"] == os.getpid()
        # The handshake reports a frozen-at-start source revision (git describe)
        # so a stale local daemon is detectable even when __version__ is unchanged.
        assert "agent_revision" in connected
    finally:
        await client.disconnect()
        daemon.close()
        await daemon.wait_closed()
        socket_path.unlink(missing_ok=True)
        identity_path.unlink(missing_ok=True)
        if socket_path.parent.exists():
            socket_path.parent.rmdir()


@pytest.mark.asyncio
async def test_agent_status_reports_running_daemon_identity() -> None:
    from vela.agent.daemon import agent_identity_path, start_agent_daemon

    socket_path = _short_socket_path()
    identity_path = agent_identity_path(socket_path)
    daemon = await start_agent_daemon(
        LocalAgent(target_name="status-local"),
        socket_path=socket_path,
    )
    try:
        result = await _run_command(_agent_status_json_command(socket_path))
        status = json.loads(result["stdout"])

        assert result["returncode"] == 0
        assert status["status"] == "running"
        assert status["pid"] == os.getpid()
        assert status["socket_path"] == str(socket_path)
        assert status["identity_path"] == str(identity_path)
        assert status["version"]
    finally:
        daemon.close()
        await daemon.wait_closed()
        socket_path.unlink(missing_ok=True)
        identity_path.unlink(missing_ok=True)
        if socket_path.parent.exists():
            socket_path.parent.rmdir()


@pytest.mark.asyncio
async def test_agent_status_reports_missing_daemon_identity() -> None:
    socket_path = _short_socket_path()
    result = await _run_command(_agent_status_json_command(socket_path))
    status = json.loads(result["stdout"])

    assert result["returncode"] == 1
    assert status["status"] == "not-running"
    assert status["socket_path"] == str(socket_path)
    assert status["identity_path"] == str(socket_path.with_name("agent.json"))


@pytest.mark.asyncio
async def test_agent_status_rejects_live_identity_when_socket_is_missing() -> None:
    from vela.agent.daemon import agent_identity_path, start_agent_daemon

    socket_path = _short_socket_path()
    identity_path = agent_identity_path(socket_path)
    daemon = await start_agent_daemon(LocalAgent(), socket_path=socket_path)
    try:
        daemon.close()
        await daemon.wait_closed()
        socket_path.unlink(missing_ok=True)

        result = await _run_command(_agent_status_json_command(socket_path))
        status = json.loads(result["stdout"])

        assert result["returncode"] == 1
        assert status["status"] == "stale"
        assert status["reason"] == "socket missing"
        assert status["pid"] == os.getpid()
    finally:
        daemon.close()
        await daemon.wait_closed()
        socket_path.unlink(missing_ok=True)
        identity_path.unlink(missing_ok=True)
        if socket_path.parent.exists():
            socket_path.parent.rmdir()


@pytest.mark.asyncio
async def test_agent_start_spawns_detached_socket_daemon() -> None:
    from vela.agent.daemon import agent_identity_path

    socket_path = _short_socket_path()
    identity_path = agent_identity_path(socket_path)
    client = SubprocessTargetClient(_agent_connect_socket_command(socket_path))
    try:
        result = await _run_command(_agent_start_json_command(socket_path))
        started = json.loads(result["stdout"])

        assert result["returncode"] == 0
        assert started["status"] == "running"
        assert started["pid"] > 0
        assert started["socket_path"] == str(socket_path)
        assert identity_path.exists()

        connected = await client.connect()

        assert connected["daemon_pid"] == started["pid"]
    finally:
        await client.disconnect()
        await _run_command(_agent_stop_json_command(socket_path))
        socket_path.unlink(missing_ok=True)
        identity_path.unlink(missing_ok=True)
        if socket_path.parent.exists():
            socket_path.parent.rmdir()


@pytest.mark.asyncio
async def test_agent_restart_replaces_socket_daemon() -> None:
    from vela.agent.daemon import agent_identity_path

    socket_path = _short_socket_path()
    identity_path = agent_identity_path(socket_path)
    client = SubprocessTargetClient(_agent_connect_socket_command(socket_path))
    try:
        start_result = await _run_command(_agent_start_json_command(socket_path))
        started = json.loads(start_result["stdout"])

        restart_result = await _run_command(_agent_restart_json_command(socket_path))
        restarted = json.loads(restart_result["stdout"])

        assert start_result["returncode"] == 0
        assert restart_result["returncode"] == 0
        assert restarted["status"] == "running"
        assert restarted["previous_pid"] == started["pid"]
        assert restarted["pid"] != started["pid"]
        assert restarted["socket_path"] == str(socket_path)

        connected = await client.connect()

        assert connected["daemon_pid"] == restarted["pid"]
    finally:
        await client.disconnect()
        await _run_command(_agent_stop_json_command(socket_path))
        socket_path.unlink(missing_ok=True)
        identity_path.unlink(missing_ok=True)
        if socket_path.parent.exists():
            socket_path.parent.rmdir()


@pytest.mark.asyncio
async def test_agent_connect_auto_starts_missing_socket_daemon() -> None:
    from vela.agent.daemon import agent_identity_path

    socket_path = _short_socket_path()
    identity_path = agent_identity_path(socket_path)
    client = SubprocessTargetClient(_agent_connect_socket_command(socket_path))
    try:
        connected = await client.connect()
        status_result = await _run_command(_agent_status_json_command(socket_path))
        status = json.loads(status_result["stdout"])

        assert connected["target"] == "local"
        assert connected["daemon_pid"] == status["pid"]
        assert status["status"] == "running"
        assert identity_path.exists()
    finally:
        await client.disconnect()
        await _run_command(_agent_stop_json_command(socket_path))
        socket_path.unlink(missing_ok=True)
        identity_path.unlink(missing_ok=True)
        if socket_path.parent.exists():
            socket_path.parent.rmdir()


@pytest.mark.asyncio
async def test_agent_connect_auto_starts_refused_stale_socket_daemon() -> None:
    from vela.agent.daemon import agent_identity_path, stop_agent_daemon
    from vela.agent.socket import serve_unix_socket_agent

    socket_path = _short_socket_path()
    identity_path = agent_identity_path(socket_path)
    server = await serve_unix_socket_agent(LocalAgent(), socket_path)
    server.close()
    await server.wait_closed()
    assert socket_path.exists()
    client = SubprocessTargetClient(_agent_connect_socket_command(socket_path))
    try:
        connected = await client.connect()

        assert connected["target"] == "local"
        assert connected["daemon_pid"] > 0
        assert identity_path.exists()
    finally:
        await client.disconnect()
        stop_agent_daemon(socket_path)
        socket_path.unlink(missing_ok=True)
        identity_path.unlink(missing_ok=True)
        if socket_path.parent.exists():
            socket_path.parent.rmdir()


@pytest.mark.asyncio
async def test_agent_stop_terminates_foreground_socket_daemon() -> None:
    from vela.agent.daemon import agent_identity_path

    socket_path = _short_socket_path()
    identity_path = agent_identity_path(socket_path)
    process = await asyncio.create_subprocess_exec(
        *_agent_run_socket_command(socket_path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        await _wait_for_identity_file(identity_path, process)

        result = await _run_command(_agent_stop_json_command(socket_path))
        stopped = json.loads(result["stdout"])

        assert result["returncode"] == 0
        assert stopped["status"] == "stopped"
        assert stopped["pid"] == process.pid
        assert stopped["socket_path"] == str(socket_path)
        await asyncio.wait_for(process.wait(), timeout=scaled_timeout(2))
        assert not socket_path.exists()
        assert not identity_path.exists()
    finally:
        await _terminate_process(process)
        socket_path.unlink(missing_ok=True)
        identity_path.unlink(missing_ok=True)
        if socket_path.parent.exists():
            socket_path.parent.rmdir()


@pytest.mark.asyncio
async def test_agent_run_foreground_command_serves_socket_daemon() -> None:
    from vela.agent.daemon import agent_identity_path

    socket_path = _short_socket_path()
    identity_path = agent_identity_path(socket_path)
    process = await asyncio.create_subprocess_exec(
        *_agent_run_socket_command(socket_path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    client = SubprocessTargetClient(_agent_connect_socket_command(socket_path))
    try:
        await _wait_for_identity_file(identity_path, process)

        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        connected = await client.connect()

        assert identity["pid"] == process.pid
        assert connected["target"] == "local"
        assert connected["daemon_pid"] == process.pid
    finally:
        await client.disconnect()
        await _terminate_process(process)
        socket_path.unlink(missing_ok=True)
        identity_path.unlink(missing_ok=True)
        if socket_path.parent.exists():
            socket_path.parent.rmdir()


@pytest.mark.asyncio
async def test_agent_run_foreground_command_exits_after_idle_timeout() -> None:
    from vela.agent.daemon import agent_identity_path

    socket_path = _short_socket_path()
    identity_path = agent_identity_path(socket_path)
    process = await asyncio.create_subprocess_exec(
        *_agent_run_idle_socket_command(socket_path, 0.1),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        await _wait_for_identity_file(identity_path, process)

        await asyncio.wait_for(process.wait(), timeout=scaled_timeout(2))

        assert process.returncode == 0
        assert not socket_path.exists()
        assert not identity_path.exists()
    finally:
        await _terminate_process(process)
        socket_path.unlink(missing_ok=True)
        identity_path.unlink(missing_ok=True)
        if socket_path.parent.exists():
            socket_path.parent.rmdir()


async def _wait_for_identity_file(
    identity_path: Path,
    process: asyncio.subprocess.Process,
    *,
    timeout: float = 10.0,
) -> None:
    deadline = time.monotonic() + scaled_timeout(timeout)
    while time.monotonic() < deadline:
        if identity_path.exists():
            return
        if process.returncode is not None:
            stderr = b""
            if process.stderr is not None:
                stderr = await process.stderr.read()
            pytest.fail(
                "agent run exited before writing identity: "
                f"{stderr.decode(errors='replace')}"
            )
        await asyncio.sleep(0.05)
    pytest.fail(f"agent run did not write identity file: {identity_path}")


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=scaled_timeout(2))
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()


async def _run_command(command: list[str]) -> dict[str, object]:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    return {
        "returncode": process.returncode,
        "stdout": stdout.decode(),
        "stderr": stderr.decode(),
    }
