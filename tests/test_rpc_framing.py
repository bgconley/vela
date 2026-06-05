from __future__ import annotations

import asyncio
import io
import sys
from types import SimpleNamespace

import pytest

from vllm_loader.agent import socket as agent_socket_module
from vllm_loader.agent import stdio as agent_stdio_module
from vllm_loader.agent.local import LocalAgent, TargetCallError
from vllm_loader.agent.stdio import _handle_frame, _PrioritizedFrameWriter
from vllm_loader.monitoring.gpu import GpuPollResult
from vllm_loader.transport import socket as socket_transport_module
from vllm_loader.transport import subprocess as subprocess_transport_module
from vllm_loader.transport.ndjson import (
    MAX_FRAME_BYTES,
    NdjsonFrameError,
    decode_frame,
    encode_frame,
)
from vllm_loader.transport.rpc_errors import rpc_error_payload
from vllm_loader.transport.socket import UnixSocketTargetClient
from vllm_loader.transport.socket import (
    _target_call_error_from_payload as socket_error_from_payload,
)
from vllm_loader.transport.subprocess import (
    SubprocessTargetClient,
)
from vllm_loader.transport.subprocess import (
    _target_call_error_from_payload as subprocess_error_from_payload,
)


def test_ndjson_frame_round_trips_json_object() -> None:
    frame = {"id": "r1", "method": "handshake", "params": {"protocol_version": 1}}

    encoded = encode_frame(frame)

    assert encoded.endswith(b"\n")
    assert decode_frame(encoded) == frame


def test_ndjson_frame_rejects_oversized_payload() -> None:
    with pytest.raises(NdjsonFrameError, match="exceeds"):
        encode_frame({"payload": "x" * 8}, max_bytes=4)


def test_ndjson_frame_rejects_non_object_payload() -> None:
    with pytest.raises(NdjsonFrameError, match="object"):
        decode_frame(b'["not", "an", "object"]\n')


@pytest.mark.asyncio
async def test_stdio_stream_reader_accepts_protocol_sized_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limits: list[int | None] = []

    class FakeTransport:
        def is_closing(self) -> bool:
            return False

        def close(self) -> None:
            pass

    async def fake_connect_read_pipe(factory, _pipe):
        factory()
        return FakeTransport()

    async def fake_connect_write_pipe(_factory, _pipe):
        return FakeTransport(), SimpleNamespace()

    original_stream_reader = asyncio.StreamReader

    def stream_reader_factory(*, limit: int | None = None):
        limits.append(limit)
        if limit is None:
            return original_stream_reader()
        return original_stream_reader(limit=limit)

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(agent_stdio_module.asyncio, "StreamReader", stream_reader_factory)
    monkeypatch.setattr(loop, "connect_read_pipe", fake_connect_read_pipe)
    monkeypatch.setattr(loop, "connect_write_pipe", fake_connect_write_pipe)

    await agent_stdio_module._stdio_streams(io.BytesIO(), io.BytesIO())

    assert limits == [MAX_FRAME_BYTES + 1]


@pytest.mark.asyncio
async def test_socket_transport_uses_protocol_frame_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    captured_kwargs: list[dict] = []

    async def fake_open_unix_connection(*_args, **kwargs):
        captured_kwargs.append(kwargs)
        return object(), object()

    monkeypatch.setattr(
        socket_transport_module.asyncio,
        "open_unix_connection",
        fake_open_unix_connection,
    )

    client = UnixSocketTargetClient(tmp_path / "agent.sock", auto_start=False)
    await client._open_socket()

    assert captured_kwargs == [{"limit": MAX_FRAME_BYTES + 1}]


@pytest.mark.asyncio
async def test_subprocess_transport_uses_protocol_frame_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_kwargs: dict | None = None

    async def fake_create_subprocess_exec(*_args, **kwargs):
        nonlocal captured_kwargs
        captured_kwargs = kwargs
        raise OSError("no agent")

    monkeypatch.setattr(
        subprocess_transport_module.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    client = SubprocessTargetClient([sys.executable, "-c", "pass"])
    with pytest.raises(TargetCallError, match="Unable to start target agent command"):
        await client.connect()

    assert captured_kwargs is not None
    assert captured_kwargs["limit"] == MAX_FRAME_BYTES + 1


@pytest.mark.asyncio
async def test_agent_socket_server_uses_protocol_frame_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    captured_kwargs: dict | None = None

    async def fake_start_unix_server(*_args, **kwargs):
        nonlocal captured_kwargs
        captured_kwargs = kwargs
        return SimpleNamespace(close=lambda: None)

    monkeypatch.setattr(agent_socket_module.asyncio, "start_unix_server", fake_start_unix_server)

    await agent_socket_module.serve_unix_socket_agent(LocalAgent(), tmp_path / "agent.sock")

    assert captured_kwargs is not None
    assert captured_kwargs["limit"] == MAX_FRAME_BYTES + 1


@pytest.mark.asyncio
async def test_stdio_writer_prioritizes_response_over_buffered_events() -> None:
    class PausedDrainWriter:
        def __init__(self) -> None:
            self.frames: list[dict] = []
            self.drain_calls = 0
            self.first_drain_started = asyncio.Event()
            self.release_first_drain = asyncio.Event()

        def write(self, data: bytes) -> None:
            self.frames.append(decode_frame(data))

        async def drain(self) -> None:
            self.drain_calls += 1
            if self.drain_calls == 1:
                self.first_drain_started.set()
                await self.release_first_drain.wait()

    sink = PausedDrainWriter()
    writer = _PrioritizedFrameWriter(sink)
    await writer.write({"event": "log", "seq": 1})
    await asyncio.wait_for(sink.first_drain_started.wait(), timeout=2)

    await writer.write({"event": "log", "seq": 2})
    await writer.write({"id": "stop-1", "result": {"signaled": True}})
    sink.release_first_drain.set()
    await writer.close()

    assert sink.frames == [
        {"event": "log", "seq": 1},
        {"id": "stop-1", "result": {"signaled": True}},
        {"event": "log", "seq": 2},
    ]


@pytest.mark.asyncio
async def test_stdio_writer_coalesces_backpressured_progress_events() -> None:
    class PausedDrainWriter:
        def __init__(self) -> None:
            self.frames: list[dict] = []
            self.drain_calls = 0
            self.first_drain_started = asyncio.Event()
            self.release_first_drain = asyncio.Event()

        def write(self, data: bytes) -> None:
            self.frames.append(decode_frame(data))

        async def drain(self) -> None:
            self.drain_calls += 1
            if self.drain_calls == 1:
                self.first_drain_started.set()
                await self.release_first_drain.wait()

    sink = PausedDrainWriter()
    writer = _PrioritizedFrameWriter(sink)
    await writer.write({"event": "log", "run_id": "run-1", "seq": 1, "text": "start"})
    await asyncio.wait_for(sink.first_drain_started.wait(), timeout=2)

    await writer.write({"event": "progress", "run_id": "run-1", "seq": 2, "text": "10%"})
    await writer.write({"event": "progress", "run_id": "run-1", "seq": 3, "text": "20%"})
    await writer.write({"event": "progress", "run_id": "run-1", "seq": 4, "text": "30%"})
    await writer.write({"event": "log", "run_id": "run-1", "seq": 5, "text": "done"})

    sink.release_first_drain.set()
    await writer.close()

    assert sink.frames == [
        {"event": "log", "run_id": "run-1", "seq": 1, "text": "start"},
        {"event": "progress", "run_id": "run-1", "seq": 4, "text": "30%"},
        {"event": "log", "run_id": "run-1", "seq": 5, "text": "done"},
    ]


@pytest.mark.asyncio
async def test_subprocess_target_client_requires_connection() -> None:
    client = SubprocessTargetClient([sys.executable, "-c", "pass"])

    with pytest.raises(RuntimeError, match="not connected"):
        await client.call("handshake")


@pytest.mark.asyncio
async def test_subprocess_target_client_reports_command_startup_failure() -> None:
    client = SubprocessTargetClient(["definitely-missing-vllm-loader-agent"])

    with pytest.raises(TargetCallError) as exc_info:
        await client.connect()

    assert exc_info.value.code == "agent-unreachable"
    assert "definitely-missing-vllm-loader-agent" in exc_info.value.message


@pytest.mark.asyncio
async def test_subprocess_target_client_reports_bridge_exit_before_response() -> None:
    client = SubprocessTargetClient([sys.executable, "-c", ""])

    with pytest.raises(TargetCallError) as exc_info:
        await client.connect()

    assert exc_info.value.code == "agent-unreachable"
    assert "target agent process exited" in exc_info.value.message


@pytest.mark.asyncio
async def test_stdio_agent_errors_use_json_rpc_integer_codes_and_data_key() -> None:
    class FailingAgent:
        def handle(self, method: str, _params=None):
            if method == "launch":
                raise TargetCallError(
                    "preflight-failed",
                    "preflight failed",
                    {"kind": "MODEL_NOT_FOUND"},
                )
            raise TargetCallError("method-not-found", f"unknown method: {method}")

    frames: list[dict] = []

    async def write_frame(frame: dict) -> None:
        frames.append(frame)

    await _handle_frame(
        FailingAgent(),
        {"id": "r1", "method": "launch", "params": {}},
        write_frame,
        {},
    )

    error = frames[-1]["error"]
    assert error["code"] == -32005
    assert error["message"] == "preflight failed"
    assert error["data"] == {"kind": "MODEL_NOT_FOUND"}
    assert "details" not in error

    await _handle_frame(
        FailingAgent(),
        {"id": "r2", "method": "missing", "params": {}},
        write_frame,
        {},
    )

    error = frames[-1]["error"]
    assert error["code"] == -32601
    assert error["data"] == {}

    await _handle_frame(FailingAgent(), {"id": "r3", "params": {}}, write_frame, {})

    error = frames[-1]["error"]
    assert error["code"] == -32600
    assert error["data"] == {}


@pytest.mark.asyncio
async def test_stdio_subscribe_all_registers_wildcard_event_stream() -> None:
    seen: dict[str, object] = {}
    event_written = asyncio.Event()

    class SubscribeAllAgent:
        def subscribe(self, run_ids, *, resume_from: object = "live", all_runs=False):
            seen["run_ids"] = list(run_ids)
            seen["resume_from"] = resume_from
            seen["all_runs"] = all_runs

            async def events():
                if all_runs:
                    yield {
                        "event": "log",
                        "run_id": "run-any",
                        "kind": "committed",
                        "text": "INFO all",
                        "level": "INFO",
                        "seq": 1,
                        "ts": "2026-06-03T00:00:00Z",
                        "mono": 1.0,
                    }
                    return
                await asyncio.Event().wait()

            return events()

    frames: list[dict] = []

    async def write_frame(frame: dict) -> None:
        frames.append(frame)
        if frame.get("event") == "log":
            event_written.set()

    subscription_tasks: dict[str, asyncio.Task[None]] = {}
    await _handle_frame(
        SubscribeAllAgent(),
        {
            "id": "sub-all-1",
            "method": "subscribe",
            "params": {"sub_id": "all-runs", "all": True, "resume_from": "start"},
        },
        write_frame,
        subscription_tasks,
    )
    try:
        assert seen == {
            "run_ids": [],
            "resume_from": "start",
            "all_runs": True,
        }
        await asyncio.wait_for(event_written.wait(), timeout=2)
    finally:
        for task in subscription_tasks.values():
            task.cancel()
        await asyncio.gather(*subscription_tasks.values(), return_exceptions=True)

    assert {"id": "sub-all-1", "result": {"sub_id": "all-runs"}} in frames
    event = next(frame for frame in frames if frame.get("event") == "log")
    assert event["run_id"] == "run-any"
    assert event["text"] == "INFO all"


@pytest.mark.asyncio
async def test_stdio_unsubscribe_stops_agent_gpu_stream() -> None:
    def sampler() -> GpuPollResult:
        return GpuPollResult([])

    agent = LocalAgent(gpu_sampler=sampler)
    frames: list[dict] = []

    async def write_frame(frame: dict) -> None:
        frames.append(frame)

    await _handle_frame(
        agent,
        {"id": "gpu-1", "method": "gpu", "params": {"sub_id": "gpu-panel"}},
        write_frame,
        {},
    )
    assert "gpu-panel" in agent._gpu_stream_tasks

    await _handle_frame(
        agent,
        {"id": "unsub-1", "method": "unsubscribe", "params": {"sub_id": "gpu-panel"}},
        write_frame,
        {},
    )

    assert frames[-1] == {"id": "unsub-1", "result": {"sub_id": "gpu-panel"}}
    assert "gpu-panel" not in agent._gpu_stream_tasks


def test_transport_clients_decode_json_rpc_error_data_to_target_call_error() -> None:
    payload = {
        "code": -32005,
        "message": "preflight failed",
        "data": {"kind": "MODEL_NOT_FOUND"},
    }

    for error_from_payload in (
        subprocess_error_from_payload,
        socket_error_from_payload,
    ):
        error = error_from_payload(payload)
        assert error.code == "preflight-failed"
        assert error.message == "preflight failed"
        assert error.details == {"kind": "MODEL_NOT_FOUND"}


def test_ambiguous_json_rpc_error_codes_preserve_symbolic_target_error_code() -> None:
    payload = rpc_error_payload(
        "invalid-config",
        "bad.yaml: model: Field required",
        {"name": "bad"},
    )

    assert payload["code"] == -32004
    assert payload["data"]["target_error_code"] == "invalid-config"
    for error_from_payload in (
        subprocess_error_from_payload,
        socket_error_from_payload,
    ):
        error = error_from_payload(payload)
        assert error.code == "invalid-config"
        assert error.details["name"] == "bad"
