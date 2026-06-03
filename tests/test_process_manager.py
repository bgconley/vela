from __future__ import annotations

import asyncio
import json
import signal
import socket
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

import vllm_loader.engine.process_manager as process_manager_module
from vllm_loader.config.schema import ModelConfig
from vllm_loader.engine.command_builder import CommandBuildResult
from vllm_loader.engine.log_sink import LogRecord
from vllm_loader.engine.process_manager import (
    _scrub_config_snapshot,
    _signal_group_with_escalation,
    start_attached,
    start_detached,
)


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


@pytest.mark.asyncio
async def test_attached_reader_keeps_draining_when_log_sink_feed_fails(
    tmp_path: Path,
) -> None:
    port = _free_port()
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
    )

    def fail_feed(_chunk: bytes) -> None:
        raise OSError("simulated durable log failure")

    process.log_sink.feed = fail_feed
    task = asyncio.create_task(process.read_loop())
    await _wait_for_health(port)
    await asyncio.sleep(0.2)

    try:
        assert not task.done()
        process.stop(interrupt_timeout=1, terminate_timeout=1)
        returncode = await asyncio.wait_for(task, timeout=5)
        assert returncode == 0
    finally:
        if process.proc.poll() is None:
            process.kill()


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


def test_config_snapshot_scrubs_generic_secret_patterns() -> None:
    cfg = ModelConfig.model_validate(
        {
            "name": "metadata",
            "model": "fake/model",
            "server": {"api_key": "sk-config-secret"},
            "env": {"HF_TOKEN": "hf_config_secret", "SAFE_ENV": "kept"},
            "extra_args": [
                "--api-key",
                "sk-extra-secret",
                "--header",
                "Authorization: Bearer snapshot-bearer",
                "--hf-token-copy",
                "hf_extra_secret",
            ],
        }
    )

    snapshot = _scrub_config_snapshot(cfg, secrets=[])
    text = json.dumps(snapshot, ensure_ascii=False)

    assert snapshot["server"]["api_key"] is None
    assert snapshot["env"] == {"SAFE_ENV": "kept"}
    assert "sk-config-secret" not in text
    assert "hf_config_secret" not in text
    assert "sk-extra-secret" not in text
    assert "snapshot-bearer" not in text
    assert "hf_extra_secret" not in text
    assert "Authorization: Bearer ••••" in text
    assert "••••" in text


def test_detached_launch_cleans_up_supervisor_when_sidecar_handshake_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs_dir = tmp_path / "runs"
    cfg = ModelConfig.model_validate(
        {
            "name": "detached-timeout",
            "model": "fake/model",
            "launch": {"mode": "detached", "runs_dir": runs_dir},
        }
    )
    build = CommandBuildResult(
        argv=[sys.executable, "-c", "pass"],
        env={},
        cwd=tmp_path,
    )
    cleanup_pids: list[int] = []

    class FakeSupervisor:
        pid = 4321
        returncode = None

        def poll(self) -> None:
            return None

    monkeypatch.setattr(
        process_manager_module.subprocess,
        "Popen",
        lambda *args, **kwargs: FakeSupervisor(),
    )
    monkeypatch.setattr(
        process_manager_module,
        "_wait_for_sidecar",
        lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("missing sidecar")),
    )
    monkeypatch.setattr(
        process_manager_module,
        "_signal_group_with_escalation",
        lambda proc, interrupt_timeout=1, terminate_timeout=1: cleanup_pids.append(proc.pid),
    )

    with pytest.raises(TimeoutError, match="missing sidecar"):
        start_detached(cfg, build, secrets=["secret-value"], wait_timeout=0.01)

    assert cleanup_pids == [4321]
    assert list(runs_dir.glob("*.supervisor-payload.json")) == []


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
