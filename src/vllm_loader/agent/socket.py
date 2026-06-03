from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import BinaryIO

from vllm_loader.agent.local import LocalAgent
from vllm_loader.agent.stdio import _stdio_streams, serve_agent_stream


async def serve_unix_socket_agent(
    agent: LocalAgent,
    socket_path: str | Path,
) -> asyncio.Server:
    path = Path(socket_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)

    async def handle_connection(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        await serve_agent_stream(agent, reader, writer)

    return await asyncio.start_unix_server(handle_connection, path=str(path))


async def bridge_stdio_to_unix_socket(
    socket_path: str | Path,
    *,
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
) -> None:
    socket_reader, socket_writer = await asyncio.open_unix_connection(str(socket_path))
    stdio_reader, stdio_writer = await _stdio_streams(
        stdin or sys.stdin.buffer,
        stdout or sys.stdout.buffer,
    )

    stdin_to_socket = asyncio.create_task(_pipe_stream(stdio_reader, socket_writer))
    socket_to_stdout = asyncio.create_task(_pipe_stream(socket_reader, stdio_writer))
    done, pending = await asyncio.wait(
        {stdin_to_socket, socket_to_stdout},
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in done:
        task.result()
    if stdin_to_socket in done:
        await socket_to_stdout
        return
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


async def _pipe_stream(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    try:
        while chunk := await reader.read(65536):
            writer.write(chunk)
            await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()
