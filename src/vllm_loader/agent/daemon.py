from __future__ import annotations

import asyncio
import json
import os
import signal
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil

from vllm_loader import __version__
from vllm_loader.agent.local import LocalAgent, PROTOCOL_VERSION
from vllm_loader.agent.socket import serve_unix_socket_agent
from vllm_loader.engine.sidecar import procfs_starttime_from_pid


@dataclass
class AgentDaemon:
    socket_path: Path
    identity_path: Path
    server: asyncio.Server

    def close(self) -> None:
        self.server.close()

    async def wait_closed(self) -> None:
        await self.server.wait_closed()


def default_agent_runtime_dir() -> Path:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir:
        return Path(runtime_dir) / "vllm-loader"
    return Path.home() / ".local" / "state" / "vllm-loader"


def default_agent_socket_path() -> Path:
    return default_agent_runtime_dir() / "agent.sock"


def agent_identity_path(socket_path: str | Path) -> Path:
    return Path(socket_path).with_name("agent.json")


async def start_agent_daemon(
    agent: LocalAgent | None = None,
    *,
    socket_path: str | Path | None = None,
) -> AgentDaemon:
    resolved_socket_path = Path(socket_path) if socket_path is not None else default_agent_socket_path()
    resolved_socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    resolved_socket_path.parent.chmod(0o700)
    server = await serve_unix_socket_agent(agent or LocalAgent(), resolved_socket_path)
    resolved_socket_path.chmod(0o600)
    identity_path = agent_identity_path(resolved_socket_path)
    _write_agent_identity(identity_path, resolved_socket_path)
    return AgentDaemon(
        socket_path=resolved_socket_path,
        identity_path=identity_path,
        server=server,
    )


async def run_agent_daemon(
    agent: LocalAgent | None = None,
    *,
    socket_path: str | Path | None = None,
) -> None:
    daemon = await start_agent_daemon(agent, socket_path=socket_path)
    stop_requested = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop_requested.set)
        except NotImplementedError:
            pass
    try:
        await stop_requested.wait()
    finally:
        daemon.close()
        await daemon.wait_closed()
        daemon.socket_path.unlink(missing_ok=True)
        daemon.identity_path.unlink(missing_ok=True)


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
        "protocol_versions": [PROTOCOL_VERSION],
        "socket_path": str(socket_path),
    }
