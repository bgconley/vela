from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import psutil

from vela import __version__
from vela.agent.local import PROTOCOL_VERSION, LocalAgent
from vela.agent.socket import serve_unix_socket_agent
from vela.engine.run_pruning import prune_run_records
from vela.engine.sidecar import procfs_starttime_from_pid


@dataclass
class AgentDaemon:
    socket_path: Path
    identity_path: Path
    server: asyncio.Server
    active_connections: Callable[[], int]

    def close(self) -> None:
        self.server.close()

    async def wait_closed(self) -> None:
        await self.server.wait_closed()


def default_agent_runtime_dir() -> Path:
    """The dir that holds ``agent.sock`` (D5, bug-238).

    Precedence: ``VELA_AGENT_RUNTIME_DIR`` (vela-specific override, used verbatim)
    > ``XDG_RUNTIME_DIR`` (shared runtime dir → ``/vela`` subdir) >
    ``$XDG_STATE_HOME/vela`` > ``~/.local/state/vela``. Honouring ``XDG_STATE_HOME``
    lets an isolated instance escape the shared long-running daemon; previously
    only ``XDG_RUNTIME_DIR`` did, so setting ``XDG_STATE_HOME`` alone silently
    reconnected to the user's daemon.
    """
    explicit = os.environ.get("VELA_AGENT_RUNTIME_DIR")
    if explicit:
        return Path(explicit)
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir:
        return Path(runtime_dir) / "vela"
    state_home = os.environ.get("XDG_STATE_HOME")
    if state_home:
        return Path(state_home) / "vela"
    return Path.home() / ".local" / "state" / "vela"


def default_agent_socket_path() -> Path:
    return default_agent_runtime_dir() / "agent.sock"


def legacy_agent_socket_path() -> Path:
    """Where a daemon started before D5 would have bound its socket.

    That is the pre-D5 resolution (``XDG_RUNTIME_DIR/vela`` else
    ``~/.local/state/vela``). When the new resolution diverges from this (only when
    ``XDG_RUNTIME_DIR`` is unset but ``XDG_STATE_HOME`` is set), a controller probes
    it so an already-running daemon is not orphaned mid-upgrade.
    """
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir:
        return Path(runtime_dir) / "vela" / "agent.sock"
    return Path.home() / ".local" / "state" / "vela" / "agent.sock"


def resolve_default_agent_socket_path() -> Path:
    """The socket a controller should use: the new path, else a live legacy one.

    Returns the new resolved path when a daemon is running there (or when nothing
    is running anywhere — new is canonical). Only when the new path has no live
    daemon but the legacy path does, and they differ, is the legacy path returned.
    """
    primary = default_agent_socket_path()
    if _inspect_agent_daemon_at(primary)["status"] == "running":
        return primary
    legacy = legacy_agent_socket_path()
    if legacy != primary and _inspect_agent_daemon_at(legacy)["status"] == "running":
        return legacy
    return primary


@lru_cache(maxsize=1)
def source_revision() -> str | None:
    """Best-effort ``git describe`` of the vela source tree, frozen at first call.

    Returns None when git or the repo is unavailable (a released wheel). A daemon
    freezes this the first time it is computed — at daemon start (LocalAgent primes
    it) or its first handshake — so a long-running daemon keeps reporting the commit
    it was launched from even after the working tree moves on. That is what makes
    the month-stale trap visible: ``__version__`` is a static string, so only the
    revision reveals a same-version daemon running month-old code (bug-238).
    """
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--always", "--dirty"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def stale_local_daemon_banner(
    agent_info: Mapping[str, Any],
    *,
    controller_version: str = __version__,
    controller_revision: str | None = None,
) -> str | None:
    """A single stable warning line when the local daemon's code is stale.

    Compares the handshake's ``agent_version`` (+ ``agent_revision`` git-describe
    when available on both sides) against the running controller. Returns None on a
    match. Wording is stable and pinned (bug-238). Callers gate this on the target
    being a local socket daemon (``vela agent restart`` is the local-only fix).
    """
    daemon_version = str(agent_info.get("agent_version") or "").strip()
    daemon_revision = str(agent_info.get("agent_revision") or "").strip()
    ctrl_revision = (
        controller_revision if controller_revision is not None else source_revision()
    )
    version_drift = bool(daemon_version) and daemon_version != controller_version
    revision_drift = (
        bool(daemon_revision) and bool(ctrl_revision) and daemon_revision != ctrl_revision
    )
    if not (version_drift or revision_drift):
        return None
    label = daemon_version or "unknown"
    if daemon_revision:
        label = f"{label} ({daemon_revision})"
    started = _daemon_start_date(agent_info.get("daemon_start_ts"))
    started_clause = f" (started {started})" if started else ""
    return (
        f"local daemon is running vela {label}{started_clause} "
        "— restart with: vela agent restart"
    )


def _daemon_start_date(daemon_start_ts: Any) -> str | None:
    if not isinstance(daemon_start_ts, str) or not daemon_start_ts:
        return None
    return daemon_start_ts.split("T", 1)[0] or None


def agent_identity_path(socket_path: str | Path) -> Path:
    return Path(socket_path).with_name("agent.json")


def agent_start_err_path(socket_path: str | Path) -> Path:
    """Where a spawned daemon's startup stderr is captured (bug-238)."""
    return Path(socket_path).with_name("agent-start.err")


def inspect_agent_daemon(socket_path: str | Path | None = None) -> dict[str, Any]:
    resolved_socket_path = (
        Path(socket_path)
        if socket_path is not None
        else resolve_default_agent_socket_path()
    )
    return _inspect_agent_daemon_at(resolved_socket_path)


def _inspect_agent_daemon_at(resolved_socket_path: Path) -> dict[str, Any]:
    identity_path = agent_identity_path(resolved_socket_path)
    base = {
        "socket_path": str(resolved_socket_path),
        "identity_path": str(identity_path),
    }
    if not identity_path.exists():
        return {"status": "not-running", **base}
    try:
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"status": "stale", "reason": f"invalid identity: {exc}", **base}
    if not isinstance(identity, dict):
        return {"status": "stale", "reason": "identity is not an object", **base}
    if str(identity.get("socket_path")) != str(resolved_socket_path):
        return {"status": "stale", "reason": "identity socket_path mismatch", **base}
    if not resolved_socket_path.exists():
        return {"status": "stale", "reason": "socket missing", **base, **identity}
    if not _identity_matches_live_process(identity):
        return {"status": "stale", "reason": "identity process mismatch", **base, **identity}
    return {"status": "running", **base, **identity}


def start_agent_daemon_process(
    socket_path: str | Path | None = None,
    *,
    timeout: float = 5.0,
) -> dict[str, Any]:
    resolved_socket_path = (
        Path(socket_path)
        if socket_path is not None
        else resolve_default_agent_socket_path()
    )
    current_status = inspect_agent_daemon(resolved_socket_path)
    if current_status["status"] == "running":
        return current_status
    command = [
        sys.executable,
        "-m",
        "vela.cli",
        "agent",
        "run",
        "--socket",
        str(resolved_socket_path),
    ]
    # Capture the spawned daemon's startup stderr so a silent start-failure (a bad
    # socket path, a crashed import, a bind error) leaves a readable log instead of
    # vanishing into DEVNULL (bug-238). A cleanly-stopped daemon removes it again.
    stderr_log = agent_start_err_path(resolved_socket_path)
    with contextlib.suppress(OSError):
        resolved_socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    stderr_target: Any = subprocess.DEVNULL
    try:
        stderr_target = open(stderr_log, "a", encoding="utf-8")  # noqa: SIM115
    except OSError:
        stderr_target = subprocess.DEVNULL
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stderr_target,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        if stderr_target is not subprocess.DEVNULL:
            stderr_target.close()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = inspect_agent_daemon(resolved_socket_path)
        if status["status"] == "running":
            return status
        if process.poll() is not None:
            return {
                "status": "start-failed",
                "reason": f"daemon exited with {process.returncode}",
                "socket_path": str(resolved_socket_path),
                "identity_path": str(agent_identity_path(resolved_socket_path)),
                "stderr_log": str(stderr_log),
            }
        time.sleep(0.05)
    return {
        "status": "starting",
        "pid": process.pid,
        "socket_path": str(resolved_socket_path),
        "identity_path": str(agent_identity_path(resolved_socket_path)),
        "stderr_log": str(stderr_log),
    }


def stop_agent_daemon(
    socket_path: str | Path | None = None,
    *,
    timeout: float = 5.0,
) -> dict[str, Any]:
    status = inspect_agent_daemon(socket_path)
    if status["status"] != "running":
        return status
    pid = int(status["pid"])
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return {**status, "status": "stopped"}
    deadline = time.monotonic() + timeout
    identity_path = Path(str(status["identity_path"]))
    while time.monotonic() < deadline:
        if not identity_path.exists() or not _identity_matches_live_process(status):
            return {**status, "status": "stopped"}
        time.sleep(0.05)
    if not _identity_matches_live_process(status):
        return {**status, "status": "stopped"}
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return {**status, "status": "stopped"}
    kill_deadline = time.monotonic() + max(0.5, min(timeout, 2.0))
    while time.monotonic() < kill_deadline:
        if not identity_path.exists() or not _identity_matches_live_process(status):
            identity_path.unlink(missing_ok=True)
            Path(str(status["socket_path"])).unlink(missing_ok=True)
            return {**status, "status": "stopped", "signal": "SIGKILL"}
        time.sleep(0.05)
    return {**status, "status": "stopping"}


def restart_agent_daemon_process(
    socket_path: str | Path | None = None,
    *,
    timeout: float = 5.0,
) -> dict[str, Any]:
    previous = inspect_agent_daemon(socket_path)
    previous_pid = previous.get("pid") if previous["status"] == "running" else None
    if previous["status"] == "running":
        stopped = stop_agent_daemon(socket_path, timeout=timeout)
        if stopped["status"] != "stopped":
            return {
                **stopped,
                "status": "restart-failed",
                "reason": "daemon did not stop",
                "previous_pid": previous_pid,
            }
    started = start_agent_daemon_process(socket_path, timeout=timeout)
    if previous_pid is not None:
        started = {**started, "previous_pid": previous_pid}
    return started


async def start_agent_daemon(
    agent: LocalAgent | None = None,
    *,
    socket_path: str | Path | None = None,
) -> AgentDaemon:
    resolved_socket_path = (
        Path(socket_path) if socket_path is not None else default_agent_socket_path()
    )
    parent_exists = resolved_socket_path.parent.exists()
    resolved_socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if socket_path is None or not parent_exists:
        resolved_socket_path.parent.chmod(0o700)
    connection_count = 0

    def set_connection_count(count: int) -> None:
        nonlocal connection_count
        connection_count = count

    server = await serve_unix_socket_agent(
        agent or LocalAgent(),
        resolved_socket_path,
        on_connection_count_changed=set_connection_count,
    )
    resolved_socket_path.chmod(0o600)
    identity_path = agent_identity_path(resolved_socket_path)
    _write_agent_identity(identity_path, resolved_socket_path)
    return AgentDaemon(
        socket_path=resolved_socket_path,
        identity_path=identity_path,
        server=server,
        active_connections=lambda: connection_count,
    )


AUTO_PRUNE_KEEP_RECENT = 50
AUTO_PRUNE_OLDER_THAN_SECONDS = 14 * 86_400.0


def auto_prune_run_records(agent: LocalAgent) -> None:
    """Best-effort retention pass over the agent's known runs dirs."""
    try:
        prune_run_records(
            agent.known_runs_dirs,
            keep_recent=AUTO_PRUNE_KEEP_RECENT,
            older_than_seconds=AUTO_PRUNE_OLDER_THAN_SECONDS,
        )
    except Exception:
        pass


async def run_agent_daemon(
    agent: LocalAgent | None = None,
    *,
    socket_path: str | Path | None = None,
    idle_timeout_seconds: float | None = None,
) -> None:
    resolved_agent = agent or LocalAgent()
    await asyncio.to_thread(auto_prune_run_records, resolved_agent)
    daemon = await start_agent_daemon(resolved_agent, socket_path=socket_path)
    stop_requested = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop_requested.set)
        except NotImplementedError:
            pass
    idle_task: asyncio.Task[None] | None = None
    if idle_timeout_seconds is not None:
        idle_task = asyncio.create_task(
            _stop_after_idle_timeout(
                daemon,
                resolved_agent,
                idle_timeout_seconds=idle_timeout_seconds,
                stop_requested=stop_requested,
            )
        )
    try:
        await stop_requested.wait()
    finally:
        if idle_task is not None:
            idle_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await idle_task
        daemon.close()
        await daemon.wait_closed()
        daemon.socket_path.unlink(missing_ok=True)
        # A clean shutdown means the startup log is empty/irrelevant; remove it so
        # the runtime dir has no leftover artifacts. A FAILED start never reaches
        # here (start_agent_daemon raised before the try), so its log persists.
        agent_start_err_path(daemon.socket_path).unlink(missing_ok=True)
        daemon.identity_path.unlink(missing_ok=True)


async def _stop_after_idle_timeout(
    daemon: AgentDaemon,
    agent: LocalAgent,
    *,
    idle_timeout_seconds: float,
    stop_requested: asyncio.Event,
) -> None:
    timeout = max(0.0, idle_timeout_seconds)
    while not stop_requested.is_set():
        await asyncio.sleep(timeout or 0.1)
        if daemon.active_connections() == 0 and not agent.has_active_runs():
            stop_requested.set()
            return


def _write_agent_identity(identity_path: Path, socket_path: Path) -> None:
    payload = _current_agent_identity(socket_path)
    identity_path.write_text(
        json.dumps(payload, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    identity_path.chmod(0o600)


def _current_agent_identity(socket_path: Path) -> dict[str, Any]:
    pid = os.getpid()
    process = psutil.Process(pid)
    return {
        "pid": pid,
        "create_time": process.create_time(),
        "procfs_starttime": procfs_starttime_from_pid(pid),
        "start_ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "version": __version__,
        "revision": source_revision(),
        "protocol_versions": [PROTOCOL_VERSION],
        "socket_path": str(socket_path),
    }


def _identity_matches_live_process(identity: dict[str, Any]) -> bool:
    try:
        pid = int(identity["pid"])
        recorded_create_time = float(identity["create_time"])
    except (KeyError, TypeError, ValueError):
        return False
    try:
        process = psutil.Process(pid)
        if process.status() == psutil.STATUS_ZOMBIE:
            return False
        live_create_time = process.create_time()
    except (psutil.Error, OSError):
        return False
    if abs(live_create_time - recorded_create_time) > 0.001:
        return False
    recorded_starttime = identity.get("procfs_starttime")
    live_starttime = procfs_starttime_from_pid(pid)
    if recorded_starttime is not None and live_starttime is not None:
        return int(recorded_starttime) == int(live_starttime)
    return True
