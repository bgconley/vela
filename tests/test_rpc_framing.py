from __future__ import annotations

import asyncio
import inspect
import io
import sys
from types import SimpleNamespace

import pytest

from vela.agent import socket as agent_socket_module
from vela.agent import stdio as agent_stdio_module
from vela.agent.auth import generate_agent_token
from vela.agent.local import LocalAgent, TargetCallError
from vela.agent.stdio import (
    _ConnectionAuthState,
    _handle_frame,
    _PrioritizedFrameWriter,
    serve_agent_stream,
)
from vela.monitoring.gpu import GpuPollResult
from vela.transport import socket as socket_transport_module
from vela.transport import subprocess as subprocess_transport_module
from vela.transport.ndjson import (
    FRAME_STREAM_LIMIT,
    MAX_FRAME_BYTES,
    NdjsonFrameError,
    decode_frame,
    encode_frame,
)
from vela.transport.rpc_errors import (
    rpc_error_payload,
    target_call_error_from_rpc_payload,
)
from vela.transport.socket import UnixSocketTargetClient
from vela.transport.socket import (
    _target_call_error_from_payload as socket_error_from_payload,
)
from vela.transport.subprocess import (
    SubprocessTargetClient,
)
from vela.transport.subprocess import (
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
async def test_protocol_stream_reader_round_trips_large_frame_payload() -> None:
    payload = "x" * (1024 * 1024)
    frame = {"id": "large-1", "result": {"payload": payload}}
    encoded = encode_frame(frame)
    assert len(encoded) > 64 * 1024
    assert len(encoded) <= MAX_FRAME_BYTES + 1
    reader = asyncio.StreamReader(limit=FRAME_STREAM_LIMIT)

    reader.feed_data(encoded)
    line = await asyncio.wait_for(reader.readline(), timeout=0.2)

    assert decode_frame(line) == frame


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
async def test_subprocess_target_client_round_trips_large_frame_payload() -> None:
    payload = "x" * (128 * 1024)
    assert len(encode_frame({"id": "2", "result": {"payload": payload}})) > 64 * 1024

    child_script = """
import json
import sys

for raw in sys.stdin.buffer:
    request = json.loads(raw)
    method = request.get("method")
    params = request.get("params") if isinstance(request.get("params"), dict) else {}
    if method == "handshake":
        result = {"target": "large-frame-agent", "capabilities": []}
    else:
        result = {"payload": params.get("payload", "")}
    sys.stdout.write(json.dumps({"id": request.get("id"), "result": result}) + "\\n")
    sys.stdout.flush()
"""
    client = SubprocessTargetClient([sys.executable, "-u", "-c", child_script])

    try:
        await client.connect()
        result = await asyncio.wait_for(
            client.call("echo", {"payload": payload}),
            timeout=5,
        )
    finally:
        await client.disconnect()

    assert result == {"payload": payload}


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
async def test_stdio_agent_reports_parse_error_and_continues_stream() -> None:
    class PingAgent:
        def handle(self, method: str, _params=None):
            if method == "ping":
                return {"pong": True}
            raise TargetCallError("method-not-found", f"unknown method: {method}")

    class CaptureWriter:
        def __init__(self) -> None:
            self.frames: list[dict] = []
            self.closed = False

        def write(self, data: bytes) -> None:
            self.frames.append(decode_frame(data))

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            self.closed = True

        async def wait_closed(self) -> None:
            return None

    reader = asyncio.StreamReader()
    writer = CaptureWriter()
    server = asyncio.create_task(serve_agent_stream(PingAgent(), reader, writer))
    reader.feed_data(b"{not-json}\n")
    reader.feed_data(encode_frame({"id": "ping-1", "method": "ping", "params": {}}))

    async def ping_response_seen() -> bool:
        for _ in range(20):
            if any(frame.get("id") == "ping-1" for frame in writer.frames):
                return True
            await asyncio.sleep(0.01)
        return False

    assert await ping_response_seen()
    reader.feed_eof()
    await asyncio.wait_for(server, timeout=2)

    assert writer.frames == [
        {
            "id": None,
            "error": {
                "code": -32700,
                "message": "Expecting property name enclosed in double quotes: "
                "line 1 column 2 (char 1)",
                "data": {},
            },
        },
        {"id": "ping-1", "result": {"pong": True}},
    ]
    assert writer.closed is True


@pytest.mark.asyncio
async def test_stdio_agent_closes_cleanly_on_reader_limit_error() -> None:
    class PingAgent:
        def handle(self, method: str, _params=None):
            if method == "ping":
                return {"pong": True}
            raise TargetCallError("method-not-found", f"unknown method: {method}")

    class LimitErrorReader:
        async def readline(self) -> bytes:
            raise ValueError("Separator is found, but chunk is longer than limit")

    class CaptureWriter:
        def __init__(self) -> None:
            self.frames: list[dict] = []
            self.closed = False

        def write(self, data: bytes) -> None:
            self.frames.append(decode_frame(data))

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            self.closed = True

        async def wait_closed(self) -> None:
            return None

    writer = CaptureWriter()

    await asyncio.wait_for(serve_agent_stream(PingAgent(), LimitErrorReader(), writer), timeout=2)

    assert writer.frames == [
        {
            "id": None,
            "error": {
                "code": -32700,
                "message": "unable to read NDJSON frame: Separator is found, "
                "but chunk is longer than limit",
                "data": {},
            },
        }
    ]
    assert writer.closed is True


@pytest.mark.asyncio
async def test_stdio_agent_reports_deep_json_parse_error_and_continues() -> None:
    class PingAgent:
        def handle(self, method: str, _params=None):
            if method == "ping":
                return {"pong": True}
            raise TargetCallError("method-not-found", f"unknown method: {method}")

    class CaptureWriter:
        def __init__(self) -> None:
            self.frames: list[dict] = []
            self.closed = False

        def write(self, data: bytes) -> None:
            self.frames.append(decode_frame(data))

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            self.closed = True

        async def wait_closed(self) -> None:
            return None

    reader = asyncio.StreamReader()
    writer = CaptureWriter()
    server = asyncio.create_task(serve_agent_stream(PingAgent(), reader, writer))
    deep_json = b'{"x":' + b"[" * 10000 + b"0" + b"]" * 10000 + b"}\n"
    reader.feed_data(deep_json)
    reader.feed_data(encode_frame({"id": "ping-1", "method": "ping", "params": {}}))

    async def ping_response_seen() -> bool:
        for _ in range(20):
            if any(frame.get("id") == "ping-1" for frame in writer.frames):
                return True
            await asyncio.sleep(0.01)
        return False

    assert await ping_response_seen()
    reader.feed_eof()
    await asyncio.wait_for(server, timeout=2)

    error = writer.frames[0]["error"]
    assert error["code"] == -32700
    assert "maximum recursion depth exceeded" in error["message"]
    assert writer.frames[-1] == {"id": "ping-1", "result": {"pong": True}}
    assert writer.closed is True


@pytest.mark.asyncio
async def test_stdio_agent_requires_authenticated_handshake_before_other_methods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = generate_agent_token()
    monkeypatch.setenv("VELA_AGENT_TOKEN", token)

    class CaptureWriter:
        def __init__(self) -> None:
            self.frames: list[dict] = []
            self.closed = False

        def write(self, data: bytes) -> None:
            self.frames.append(decode_frame(data))

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            self.closed = True

        async def wait_closed(self) -> None:
            return None

    async def frame_with_id(writer: CaptureWriter, request_id: str) -> dict:
        for _attempt in range(20):
            for frame in writer.frames:
                if frame.get("id") == request_id:
                    return frame
            await asyncio.sleep(0.01)
        raise AssertionError(f"frame {request_id!r} was not written")

    reader = asyncio.StreamReader()
    writer = CaptureWriter()
    server = asyncio.create_task(serve_agent_stream(LocalAgent(), reader, writer))

    reader.feed_data(encode_frame({"id": "ping-1", "method": "ping", "params": {}}))
    unauthenticated = await frame_with_id(writer, "ping-1")
    reader.feed_data(
        encode_frame(
            {
                "id": "handshake-1",
                "method": "handshake",
                "params": {
                    "protocol_version": 1,
                    "capability_token": token,
                },
            }
        )
    )
    handshake = await frame_with_id(writer, "handshake-1")
    reader.feed_data(encode_frame({"id": "ping-2", "method": "ping", "params": {}}))
    authenticated = await frame_with_id(writer, "ping-2")

    reader.feed_eof()
    await asyncio.wait_for(server, timeout=2)

    assert unauthenticated == {
        "id": "ping-1",
        "error": {
            "code": -32017,
            "message": "target agent requires a valid capability token",
            "data": {"reason": "capability-token-required"},
        },
    }
    assert handshake["result"]["target"] == "local"
    assert authenticated["result"]["pong"] is True
    assert writer.closed is True


def test_stdio_frame_handler_requires_explicit_auth_state() -> None:
    signature = inspect.signature(_handle_frame)

    assert signature.parameters["auth_state"].default is inspect.Parameter.empty


@pytest.mark.asyncio
async def test_stdio_frame_handler_auth_state_blocks_direct_unauthenticated_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VELA_AGENT_TOKEN", generate_agent_token())
    called = False

    class PingAgent:
        def handle(self, _method: str, _params=None):
            nonlocal called
            called = True
            return {"pong": True}

    frames: list[dict] = []

    async def write_frame(frame: dict) -> None:
        frames.append(frame)

    await _handle_frame(
        PingAgent(),
        {"id": "ping-1", "method": "ping", "params": {}},
        write_frame,
        {},
        _ConnectionAuthState(),
    )

    assert called is False
    assert frames == [
        {
            "id": "ping-1",
            "error": {
                "code": -32017,
                "message": "target agent requires a valid capability token",
                "data": {"reason": "capability-token-required"},
            },
        }
    ]


@pytest.mark.asyncio
async def test_socket_client_fails_pending_calls_on_malformed_agent_frame(tmp_path) -> None:
    client = UnixSocketTargetClient(tmp_path / "agent.sock", auto_start=False)
    reader = asyncio.StreamReader()
    client._reader = reader
    client._connected = True
    future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
    client._pending["1"] = future
    task = asyncio.create_task(client._read_socket())

    try:
        reader.feed_data(b"{not-json}\n")

        with pytest.raises(TargetCallError) as exc_info:
            await asyncio.wait_for(asyncio.shield(future), timeout=0.2)

        assert exc_info.value.code == "parse-error"
        assert "Expecting property name" in exc_info.value.message
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if future.done() and not future.cancelled():
            future.exception()


@pytest.mark.asyncio
async def test_subprocess_client_fails_pending_calls_on_malformed_agent_frame() -> None:
    client = SubprocessTargetClient([sys.executable, "-c", "pass"])
    reader = asyncio.StreamReader()

    class FakeProcess:
        stdout = reader
        returncode = None

        async def wait(self) -> int:
            self.returncode = 0
            return 0

    client._process = FakeProcess()
    client._connected = True
    future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
    client._pending["1"] = future
    task = asyncio.create_task(client._read_stdout())

    try:
        reader.feed_data(b"{not-json}\n")

        with pytest.raises(TargetCallError) as exc_info:
            await asyncio.wait_for(asyncio.shield(future), timeout=0.2)

        assert exc_info.value.code == "parse-error"
        assert "Expecting property name" in exc_info.value.message
    finally:
        client._disconnecting = True
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if future.done() and not future.cancelled():
            future.exception()


@pytest.mark.asyncio
async def test_socket_client_publishes_agent_error_on_malformed_stream_event(tmp_path) -> None:
    client = UnixSocketTargetClient(tmp_path / "agent.sock", auto_start=False)
    reader = asyncio.StreamReader()
    queue: asyncio.Queue[dict] = asyncio.Queue()
    client._reader = reader
    client._connected = True
    client._event_subscribers["sub-1"] = ({"run-1"}, queue)
    task = asyncio.create_task(client._read_socket())

    try:
        reader.feed_data(b"{not-json}\n")
        event = await asyncio.wait_for(queue.get(), timeout=0.2)

        assert event["event"] == "agent_error"
        assert event["fatal"] is False
        assert "Malformed NDJSON frame from target agent" in event["detail"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_subprocess_client_publishes_agent_error_on_malformed_stream_event() -> None:
    client = SubprocessTargetClient([sys.executable, "-c", "pass"])
    reader = asyncio.StreamReader()
    queue: asyncio.Queue[dict] = asyncio.Queue()

    class FakeProcess:
        stdout = reader
        returncode = None

        async def wait(self) -> int:
            self.returncode = 0
            return 0

    client._process = FakeProcess()
    client._connected = True
    client._event_subscribers["sub-1"] = ({"run-1"}, queue)
    task = asyncio.create_task(client._read_stdout())

    try:
        reader.feed_data(b"{not-json}\n")
        event = await asyncio.wait_for(queue.get(), timeout=0.2)

        assert event["event"] == "agent_error"
        assert event["fatal"] is False
        assert "Malformed NDJSON frame from target agent" in event["detail"]
    finally:
        client._disconnecting = True
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_subprocess_target_client_requires_connection() -> None:
    client = SubprocessTargetClient([sys.executable, "-c", "pass"])

    with pytest.raises(RuntimeError, match="not connected"):
        await client.call("handshake")


@pytest.mark.asyncio
async def test_subprocess_target_client_reports_command_startup_failure() -> None:
    client = SubprocessTargetClient(["definitely-missing-vela-agent"])

    with pytest.raises(TargetCallError) as exc_info:
        await client.connect()

    assert exc_info.value.code == "agent-unreachable"
    assert "definitely-missing-vela-agent" in exc_info.value.message


@pytest.mark.asyncio
async def test_subprocess_target_client_reports_bridge_exit_before_response() -> None:
    client = SubprocessTargetClient([sys.executable, "-c", ""])

    with pytest.raises(TargetCallError) as exc_info:
        await client.connect()

    assert exc_info.value.code == "agent-unreachable"
    assert "target agent process exited" in exc_info.value.message


@pytest.mark.asyncio
async def test_subprocess_target_client_reports_ssh_auth_bridge_failure() -> None:
    client = SubprocessTargetClient(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "sys.stderr.write('Permission denied (publickey).\\n'); "
                "sys.stderr.flush(); "
                "sys.exit(255)"
            ),
        ]
    )

    with pytest.raises(TargetCallError) as exc_info:
        await client.connect()

    assert exc_info.value.code == "agent-unreachable"
    assert "SSH target agent bridge failed" in exc_info.value.message
    assert exc_info.value.details["exit_code"] == 255
    assert exc_info.value.details["reason"] == "ssh-auth"
    assert "Permission denied" in exc_info.value.details["stderr"]


@pytest.mark.asyncio
async def test_subprocess_target_client_reports_remote_agent_command_not_found() -> None:
    client = SubprocessTargetClient(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "sys.stderr.write('bash: vela: command not found\\n'); "
                "sys.stderr.flush(); "
                "sys.exit(127)"
            ),
        ]
    )

    with pytest.raises(TargetCallError) as exc_info:
        await client.connect()

    assert exc_info.value.code == "command-not-found"
    assert "Target agent command not found" in exc_info.value.message
    assert exc_info.value.details["exit_code"] == 127
    assert exc_info.value.details["command"] == "vela"
    assert "command not found" in exc_info.value.details["stderr"]


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
            if method == "stop":
                raise TargetCallError(
                    "identity-verification-failed",
                    "tracked process group does not match sidecar",
                    {"run_id": "run-1"},
                )
            raise TargetCallError("method-not-found", f"unknown method: {method}")

    frames: list[dict] = []

    async def write_frame(frame: dict) -> None:
        frames.append(frame)

    auth_state = _ConnectionAuthState()
    await _handle_frame(
        FailingAgent(),
        {"id": "r1", "method": "launch", "params": {}},
        write_frame,
        {},
        auth_state,
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
        auth_state,
    )

    error = frames[-1]["error"]
    assert error["code"] == -32601
    assert error["data"] == {}

    await _handle_frame(
        FailingAgent(),
        {"id": "r3", "method": "stop", "params": {"run_id": "run-1"}},
        write_frame,
        {},
        auth_state,
    )

    error = frames[-1]["error"]
    assert error["code"] == -32002
    assert error["message"] == "tracked process group does not match sidecar"
    assert error["data"] == {"run_id": "run-1"}

    await _handle_frame(
        FailingAgent(),
        {"id": "r4", "params": {}},
        write_frame,
        {},
        auth_state,
    )

    error = frames[-1]["error"]
    assert error["code"] == -32600
    assert error["data"] == {}


@pytest.mark.asyncio
async def test_stdio_agent_rejects_request_without_string_id() -> None:
    called = False

    class PingAgent:
        def handle(self, _method: str, _params=None):
            nonlocal called
            called = True
            return {"pong": True}

    frames: list[dict] = []

    async def write_frame(frame: dict) -> None:
        frames.append(frame)

    await _handle_frame(
        PingAgent(),
        {"method": "ping", "params": {}},
        write_frame,
        {},
        _ConnectionAuthState(),
    )

    assert called is False
    assert frames == [
        {
            "id": None,
            "error": {
                "code": -32600,
                "message": "request id must be a non-empty string",
                "data": {},
            },
        }
    ]


@pytest.mark.asyncio
async def test_stdio_agent_rejects_non_object_params() -> None:
    called = False

    class PingAgent:
        def handle(self, _method: str, _params=None):
            nonlocal called
            called = True
            return {"pong": True}

    frames: list[dict] = []

    async def write_frame(frame: dict) -> None:
        frames.append(frame)

    await _handle_frame(
        PingAgent(),
        {"id": "ping-1", "method": "ping", "params": []},
        write_frame,
        {},
        _ConnectionAuthState(),
    )

    assert called is False
    assert frames == [
        {
            "id": "ping-1",
            "error": {
                "code": -32602,
                "message": "request params must be an object",
                "data": {},
            },
        }
    ]


@pytest.mark.parametrize(
    ("target_error_code", "wire_code"),
    [
        ("build-integrity-failed", -32014),
        ("cancelled", -32015),
        ("profile-error", -32016),
        ("agent-auth-required", -32017),
    ],
)
def test_named_target_errors_have_specific_json_rpc_codes(
    target_error_code: str, wire_code: int
) -> None:
    payload = rpc_error_payload(target_error_code, "target failure", {})

    assert payload == {
        "code": wire_code,
        "message": "target failure",
        "data": {},
    }
    recovered = target_call_error_from_rpc_payload(payload)
    assert recovered.code == target_error_code
    assert recovered.message == "target failure"


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
        _ConnectionAuthState(),
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
        _ConnectionAuthState(),
    )
    assert "gpu-panel" in agent._gpu_stream_tasks

    await _handle_frame(
        agent,
        {"id": "unsub-1", "method": "unsubscribe", "params": {"sub_id": "gpu-panel"}},
        write_frame,
        {},
        _ConnectionAuthState(),
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
