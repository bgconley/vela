from __future__ import annotations

import asyncio
import contextlib
import heapq
import inspect
import sys
import uuid
from collections.abc import AsyncIterator
from typing import Any, BinaryIO

from vllm_loader.agent.local import LocalAgent, TargetCallError
from vllm_loader.transport.ndjson import (
    FRAME_STREAM_LIMIT,
    NdjsonFrameError,
    decode_frame,
    encode_frame,
)
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
    all_value = payload.get("all", False)
    if not isinstance(run_ids, list):
        raise TargetCallError("invalid-params", "subscribe requires run_ids list")
    if not isinstance(all_value, bool):
        raise TargetCallError("invalid-params", "subscribe all must be a boolean")
    all_runs = all_value
    sub_id = str(payload.get("sub_id") or uuid.uuid4().hex)
    resume_from = payload.get("resume_from", "live")
    existing = subscription_tasks.pop(sub_id, None)
    if existing is not None:
        existing.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await existing
    task = asyncio.create_task(
        _stream_events(
            agent.subscribe(run_ids, resume_from=resume_from, all_runs=all_runs),
            write_frame,
        )
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
    def __init__(
        self,
        writer: asyncio.StreamWriter,
        *,
        max_pending_frames: int = 1024,
    ) -> None:
        self._writer = writer
        self._condition = asyncio.Condition()
        self._items: list[tuple[int, int, dict[str, Any]]] = []
        self._sequence = 0
        self._active = False
        self._latest_lossy_sequence: dict[tuple[str, str], int] = {}
        self._max_pending_frames = max(1, max_pending_frames)
        self._task = asyncio.create_task(self._drain())

    async def write(self, frame: dict[str, Any]) -> None:
        async with self._condition:
            self._sequence += 1
            sequence = self._sequence
            lossy_key = _lossy_event_key(frame)
            if lossy_key is not None:
                self._latest_lossy_sequence[lossy_key] = sequence
            heapq.heappush(self._items, (_frame_priority(frame), sequence, frame))
            if len(self._items) > self._max_pending_frames:
                self._compact_pending_lossy_events()
            self._condition.notify()

    async def close(self) -> None:
        async with self._condition:
            await self._condition.wait_for(lambda: not self._items and not self._active)
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task

    async def _drain(self) -> None:
        while True:
            async with self._condition:
                await self._condition.wait_for(lambda: bool(self._items))
                _priority, sequence, frame = heapq.heappop(self._items)
                lossy_key = _lossy_event_key(frame)
                if (
                    lossy_key is not None
                    and self._latest_lossy_sequence.get(lossy_key) != sequence
                ):
                    if not self._items:
                        self._condition.notify_all()
                    continue
                if lossy_key is not None:
                    self._latest_lossy_sequence.pop(lossy_key, None)
                self._active = True
            try:
                self._writer.write(encode_frame(frame))
                await self._writer.drain()
            finally:
                async with self._condition:
                    self._active = False
                    if not self._items:
                        self._condition.notify_all()

    def _compact_pending_lossy_events(self) -> None:
        self._items = [
            (priority, sequence, frame)
            for priority, sequence, frame in self._items
            if (
                (lossy_key := _lossy_event_key(frame)) is None
                or self._latest_lossy_sequence.get(lossy_key) == sequence
            )
        ]
        heapq.heapify(self._items)


def _frame_priority(frame: dict[str, Any]) -> int:
    return 0 if "id" in frame else 1


def _lossy_event_key(frame: dict[str, Any]) -> tuple[str, str] | None:
    event = frame.get("event")
    if event == "progress":
        run_id = frame.get("run_id")
        return ("run", str(run_id)) if run_id is not None else None
    if event == "job_progress" and frame.get("kind") == "transient":
        job_id = frame.get("job_id")
        return ("job", str(job_id)) if job_id is not None else None
    return None


async def _stdio_streams(
    stdin: BinaryIO,
    stdout: BinaryIO,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=FRAME_STREAM_LIMIT)
    reader_protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: reader_protocol, stdin)
    writer_transport, writer_protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin,
        stdout,
    )
    writer = asyncio.StreamWriter(writer_transport, writer_protocol, None, loop)
    return reader, writer
