from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path

import pytest

from vllm_loader.agent.local import LocalAgent
from vllm_loader.transport.subprocess import SubprocessTargetClient


def _short_socket_path() -> Path:
    return Path("/tmp") / f"vllm-loader-daemon-{uuid.uuid4().hex}" / "agent.sock"


def _agent_connect_socket_command(socket_path: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "vllm_loader.cli",
        "agent",
        "connect",
        "--socket",
        str(socket_path),
    ]


def _agent_run_socket_command(socket_path: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "vllm_loader.cli",
        "agent",
        "run",
        "--socket",
        str(socket_path),
    ]


@pytest.mark.asyncio
async def test_foreground_daemon_writes_identity_and_serves_socket() -> None:
    from vllm_loader.agent.daemon import agent_identity_path, start_agent_daemon

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
    finally:
        await client.disconnect()
        daemon.close()
        await daemon.wait_closed()
        socket_path.unlink(missing_ok=True)
        identity_path.unlink(missing_ok=True)
        if socket_path.parent.exists():
            socket_path.parent.rmdir()


@pytest.mark.asyncio
async def test_agent_run_foreground_command_serves_socket_daemon() -> None:
    from vllm_loader.agent.daemon import agent_identity_path

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


async def _wait_for_identity_file(
    identity_path: Path,
    process: asyncio.subprocess.Process,
    *,
    timeout: float = 2.0,
) -> None:
    deadline = time.monotonic() + timeout
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
        await asyncio.wait_for(process.wait(), timeout=2)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
