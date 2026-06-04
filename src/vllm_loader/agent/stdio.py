from __future__ import annotations

import asyncio
import contextlib
import inspect
import sys
import uuid
from collections.abc import AsyncIterator
from typing import Any, BinaryIO

from vllm_loader.agent.local import LocalAgent, TargetCallError
from vllm_loader.transport.ndjson import NdjsonFrameError, decode_frame, encode_frame
from vllm_loader.transport.rpc_errors import rpc_error_payload


async def serve_stdio_agent(
    agent: LocalAgent,
    *,
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
) -> None:
    reader, writer = await _stdio_streams(
        stdin or sys.stdin.buffer,
        stdout or sys.stdout.buffer,
    )
    await serve_agent_stream(agent, reader, writer)


async def serve_agent_stream(
    agent: LocalAgent,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    frame_writer = _PrioritizedFrameWriter(writer)
    handler_tasks: set[asyncio.Task[None]] = set()
    subscription_tasks: dict[str, asyncio.Task[None]] = {}

    async def write_frame(frame: dict[str, Any]) -> None:
        await frame_writer.write(frame)

    try:
        while line := await reader.readline():
            try:
                frame = decode_frame(line)
            except NdjsonFrameError:
                continue
            task = asyncio.create_task(
                _handle_frame(agent, frame, write_frame, subscription_tasks)
            )
            handler_tasks.add(task)
            task.add_done_callback(handler_tasks.discard)
    finally:
        tasks = [*handler_tasks, *subscription_tasks.values()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await frame_writer.close()
        writer.close()
        await writer.wait_closed()


async def _handle_frame(
    agent,
    frame: dict[str, Any],
    write_frame,
    subscription_tasks: dict[str, asyncio.Task[None]],
) -> None:
    request_id = frame.get("id")
    method = frame.get("method")
    params = frame.get("params")
    if not isinstance(method, str):
        await write_frame(
            {
                "id": request_id,
                "error": rpc_error_payload(
                    "invalid-request",
                    "request method is required",
                    {},
                ),
            }
        )
        return
    try:
        if method == "subscribe":
            result = await _subscribe(agent, params, write_frame, subscription_tasks)
        elif method == "unsubscribe":
            result = await _unsubscribe(params, subscription_tasks)
            agent_result = agent.handle(
                method, params if isinstance(params, dict) else None
            )
            if inspect.isawaitable(agent_result):
                agent_result = await agent_result
            if isinstance(agent_result, dict):
                result = agent_result
        else:
            result = agent.handle(method, params if isinstance(params, dict) else None)
            if inspect.isawaitable(result):
                result = await result
        await write_frame({"id": request_id, "result": result})
    except TargetCallError as exc:
        await write_frame(
            {
                "id": request_id,
                "error": rpc_error_payload(exc.code, exc.message, exc.details),
            }
        )
    except Exception as exc:
        await write_frame(
            {
                "id": request_id,
                "error": rpc_error_payload("internal-error", str(exc), {}),
            }
        )


async def _subscribe(
    agent,
    params: object,
    write_frame,
    subscription_tasks: dict[str, asyncio.Task[None]],
) -> dict[str, Any]:
    payload = params if isinstance(params, dict) else {}
    run_ids = payload.get("run_ids", [])
    if not isinstance(run_ids, list):
        raise TargetCallError("invalid-params", "subscribe requires run_ids list")
    sub_id = str(payload.get("sub_id") or uuid.uuid4().hex)
    resume_from = payload.get("resume_from", "live")
    existing = subscription_tasks.pop(sub_id, None)
    if existing is not None:
        existing.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await existing
    task = asyncio.create_task(
        _stream_events(agent.subscribe(run_ids, resume_from=resume_from), write_frame)
    )
    subscription_tasks[sub_id] = task

    def forget(done: asyncio.Task[None]) -> None:
        if subscription_tasks.get(sub_id) is done:
            subscription_tasks.pop(sub_id, None)

    task.add_done_callback(forget)
    return {"sub_id": sub_id}


async def _unsubscribe(
    params: object,
    subscription_tasks: dict[str, asyncio.Task[None]],
) -> dict[str, Any]:
    payload = params if isinstance(params, dict) else {}
    sub_id = payload.get("sub_id")
    if not isinstance(sub_id, str) or not sub_id.strip():
        raise TargetCallError("invalid-params", "sub_id is required")
    task = subscription_tasks.pop(sub_id, None)
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    return {"sub_id": sub_id}


async def _stream_events(
    events: AsyncIterator[dict[str, Any]],
    write_frame,
) -> None:
    try:
        async for event in events:
            await write_frame(event)
    finally:
        aclose = getattr(events, "aclose", None)
        if callable(aclose):
            await aclose()


class _PrioritizedFrameWriter:
    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self._writer = writer
        self._queue: asyncio.PriorityQueue[tuple[int, int, dict[str, Any]]] = (
            asyncio.PriorityQueue()
        )
        self._sequence = 0
        self._task = asyncio.create_task(self._drain())

    async def write(self, frame: dict[str, Any]) -> None:
        self._sequence += 1
        await self._queue.put((_frame_priority(frame), self._sequence, frame))

    async def close(self) -> None:
        await self._queue.join()
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task

    async def _drain(self) -> None:
        while True:
            _priority, _sequence, frame = await self._queue.get()
            try:
                self._writer.write(encode_frame(frame))
                await self._writer.drain()
            finally:
                self._queue.task_done()


def _frame_priority(frame: dict[str, Any]) -> int:
    return 0 if "id" in frame else 1


async def _stdio_streams(
    stdin: BinaryIO,
    stdout: BinaryIO,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    reader_protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: reader_protocol, stdin)
    writer_transport, writer_protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin,
        stdout,
    )
    writer = asyncio.StreamWriter(writer_transport, writer_protocol, None, loop)
    return reader, writer
