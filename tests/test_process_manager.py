from __future__ import annotations

import asyncio
import signal
import socket
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from vllm_loader.engine.command_builder import CommandBuildResult
from vllm_loader.engine.log_sink import LogRecord
from vllm_loader.engine.process_manager import _signal_group_with_escalation, start_attached


@pytest.mark.asyncio
async def test_attached_fake_child_streams_logs_progress_and_stops(tmp_path: Path) -> None:
    port = _free_port()
    records: list[LogRecord] = []
    build = CommandBuildResult(
        argv=[
            sys.executable,
            "-m",
            "vllm_loader.fake_child",
            "serve",
            "fake/model",
            "--port",
            str(port),
            "--sleep",
            "0.01",
        ],
        env={"PYTHONUNBUFFERED": "1"},
        cwd=Path.cwd(),
    )

    process = start_attached(
        build,
        log_path=tmp_path / "run.log",
        secrets=[],
        emit=records.append,
    )
    task = asyncio.create_task(process.read_loop())
    await _wait_for_health(port)
    await _wait_for_record(records, "Uvicorn running")
    process.stop(interrupt_timeout=1, terminate_timeout=1)
    returncode = await asyncio.wait_for(task, timeout=5)

    committed = [record.text for record in records if record.kind == "committed"]
    transient = [record.text for record in records if record.kind == "transient"]
    durable = (tmp_path / "run.log").read_text(encoding="utf-8")
    assert returncode == 0
    assert any("Initializing a V1 LLM engine" in line for line in committed)
    assert any("checkpoint shards" in line for line in transient)
    assert "checkpoint shards" not in durable
    assert "Uvicorn running" in durable


def test_stop_escalation_waits_after_final_sigkill(monkeypatch: pytest.MonkeyPatch) -> None:
    class StubbornProc:
        pid = 12345

        def __init__(self) -> None:
            self.wait_calls: list[float] = []

        def poll(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> None:
            self.wait_calls.append(float(timeout or 0))
            if len(self.wait_calls) <= 2:
                raise subprocess.TimeoutExpired("fake", timeout)
            return None

    proc = StubbornProc()
    signals: list[int] = []
    monkeypatch.setattr("os.getpgid", lambda pid: pid)
    monkeypatch.setattr("os.killpg", lambda _pgid, sig: signals.append(sig))

    _signal_group_with_escalation(proc, interrupt_timeout=0.1, terminate_timeout=0.2)

    assert signals == [signal.SIGINT, signal.SIGTERM, signal.SIGKILL]
    assert proc.wait_calls == [0.1, 0.2, 0.2]


async def _wait_for_health(port: int) -> None:
    deadline = asyncio.get_running_loop().time() + 5
    async with httpx.AsyncClient(timeout=0.2) as client:
        while asyncio.get_running_loop().time() < deadline:
            try:
                response = await client.get(f"http://127.0.0.1:{port}/health")
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.05)
    raise AssertionError("fake child did not become healthy")


async def _wait_for_record(records: list[LogRecord], text: str) -> None:
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        if any(text in record.text for record in records):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"fake child did not emit {text!r}")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
