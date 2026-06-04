from __future__ import annotations

import asyncio
import os
import socket as stdlib_socket
import struct
import sys
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

from vllm_loader.agent.local import LocalAgent
from vllm_loader.agent.stdio import _stdio_streams, serve_agent_stream


async def serve_unix_socket_agent(
    agent: LocalAgent,
    socket_path: str | Path,
    *,
    on_connection_count_changed: Callable[[int], None] | None = None,
) -> asyncio.Server:
    path = Path(socket_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    active_connections = 0

    async def handle_connection(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        nonlocal active_connections
        try:
            verify_same_user_peer(writer)
        except PermissionError:
            writer.close()
            await writer.wait_closed()
            return
        active_connections += 1
        if on_connection_count_changed is not None:
            on_connection_count_changed(active_connections)
        try:
            await serve_agent_stream(agent, reader, writer)
        finally:
            active_connections -= 1
            if on_connection_count_changed is not None:
                on_connection_count_changed(active_connections)

    return await asyncio.start_unix_server(handle_connection, path=str(path))


def verify_same_user_peer(writer: asyncio.StreamWriter) -> None:
    sock = writer.get_extra_info("socket")
    if sock is None:
        return
    peer_uid = _peer_uid_from_socket(sock)
    if peer_uid is None:
        return
    current_uid = os.getuid()
    if peer_uid != current_uid:
        raise PermissionError(f"peer uid {peer_uid} does not match current uid {current_uid}")


def _peer_uid_from_socket(sock) -> int | None:
    getpeereid = getattr(sock, "getpeereid", None)
    if getpeereid is not None:
        try:
            peer_uid, _peer_gid = getpeereid()
            return int(peer_uid)
        except OSError:
            return None
    if hasattr(stdlib_socket, "SO_PEERCRED"):
        try:
            data = sock.getsockopt(
                stdlib_socket.SOL_SOCKET,
                stdlib_socket.SO_PEERCRED,
                struct.calcsize("3i"),
            )
            _pid, uid, _gid = struct.unpack("3i", data)
            return int(uid)
        except OSError:
            return None
    if hasattr(stdlib_socket, "LOCAL_PEERCRED"):
        try:
            data = sock.getsockopt(
                getattr(stdlib_socket, "SOL_LOCAL", 0),
                stdlib_socket.LOCAL_PEERCRED,
                64,
            )
            _version, uid = struct.unpack("ii", data[: struct.calcsize("ii")])
            return int(uid)
        except OSError:
            return None
    return None


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
