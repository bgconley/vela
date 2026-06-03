from __future__ import annotations

import asyncio
import inspect
import sys
from collections.abc import AsyncIterator
from typing import Any, BinaryIO

from vllm_loader.agent.local import LocalAgent, TargetCallError
from vllm_loader.transport.ndjson import NdjsonFrameError, decode_frame, encode_frame


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
    write_lock = asyncio.Lock()
    subscription_tasks: set[asyncio.Task[None]] = set()

    async def write_frame(frame: dict[str, Any]) -> None:
        async with write_lock:
            writer.write(encode_frame(frame))
            await writer.drain()

    try:
        while line := await reader.readline():
            try:
                frame = decode_frame(line)
            except NdjsonFrameError:
                continue
            task = asyncio.create_task(
                _handle_frame(agent, frame, write_frame, subscription_tasks)
            )
            subscription_tasks.add(task)
            task.add_done_callback(subscription_tasks.discard)
    finally:
        for task in subscription_tasks:
            task.cancel()
        if subscription_tasks:
            await asyncio.gather(*subscription_tasks, return_exceptions=True)
        writer.close()
        await writer.wait_closed()


async def _handle_frame(
    agent,
    frame: dict[str, Any],
    write_frame,
    subscription_tasks: set[asyncio.Task[None]],
) -> None:
    request_id = frame.get("id")
    method = frame.get("method")
    params = frame.get("params")
    if not isinstance(method, str):
        await write_frame(
            {
                "id": request_id,
                "error": {
                    "code": "invalid-request",
                    "message": "request method is required",
                    "details": {},
                },
            }
        )
        return
    try:
        if method == "subscribe":
            result = await _subscribe(agent, params, write_frame, subscription_tasks)
        else:
            result = agent.handle(method, params if isinstance(params, dict) else None)
            if inspect.isawaitable(result):
                result = await result
        await write_frame({"id": request_id, "result": result})
    except TargetCallError as exc:
        await write_frame(
            {
                "id": request_id,
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                },
            }
        )
    except Exception as exc:
        await write_frame(
            {
                "id": request_id,
                "error": {
                    "code": "internal-error",
                    "message": str(exc),
                    "details": {},
                },
            }
        )


async def _subscribe(
    agent,
    params: object,
    write_frame,
    subscription_tasks: set[asyncio.Task[None]],
) -> dict[str, Any]:
    payload = params if isinstance(params, dict) else {}
    run_ids = payload.get("run_ids", [])
    if not isinstance(run_ids, list):
        raise TargetCallError("invalid-params", "subscribe requires run_ids list")
    resume_from = payload.get("resume_from", "live")
    task = asyncio.create_task(
        _stream_events(agent.subscribe(run_ids, resume_from=resume_from), write_frame)
    )
    subscription_tasks.add(task)
    task.add_done_callback(subscription_tasks.discard)
    return {"subscribed": True}


async def _stream_events(
    events: AsyncIterator[dict[str, Any]],
    write_frame,
) -> None:
    async for event in events:
        await write_frame(event)


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
