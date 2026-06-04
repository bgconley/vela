from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from vllm_loader.agent.daemon import (
    restart_agent_daemon_process,
    start_agent_daemon_process,
)
from vllm_loader.agent.local import PROTOCOL_VERSION, TargetCallError
from vllm_loader.transport.client import (
    REQUIRED_AGENT_CAPABILITIES,
    event_matches_subscription,
    handshake_params,
)
from vllm_loader.transport.ndjson import NdjsonFrameError, decode_frame, encode_frame
from vllm_loader.transport.rpc_errors import target_call_error_from_rpc_payload


class UnixSocketTargetClient:
    def __init__(self, socket_path: str | Path, *, auto_start: bool = True) -> None:
        self._socket_path = Path(socket_path)
        self._auto_start = auto_start
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected = False
        self._request_seq = 0
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._event_subscribers: dict[
            str, tuple[set[str], asyncio.Queue[dict[str, Any]]]
        ] = {}
        self._subscription_tasks: set[asyncio.Task[dict[str, Any]]] = set()
        self._reader_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._agent_info: dict[str, Any] | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> dict[str, Any]:
        if self._connected:
            return self._agent_info or {}
        started_agent = False
        if self._auto_start and not self._socket_path.exists():
            self._start_agent_or_raise()
            started_agent = True
        try:
            await self._open_socket()
        except OSError as exc:
            if not self._auto_start or started_agent:
                raise _agent_socket_unreachable_error(self._socket_path) from exc
            self._start_agent_or_raise()
            try:
                await self._open_socket()
            except OSError as retry_exc:
                raise _agent_socket_unreachable_error(self._socket_path) from retry_exc
        self._connected = True
        self._reader_task = asyncio.create_task(self._read_socket())
        try:
            self._agent_info = await self._handshake()
        except TargetCallError as exc:
            if not self._should_restart_for_missing_capability(exc, started_agent):
                await self.disconnect()
                raise
            await self.disconnect()
            self._restart_agent_or_raise()
            await self._open_socket()
            self._connected = True
            self._reader_task = asyncio.create_task(self._read_socket())
            try:
                self._agent_info = await self._handshake()
            except Exception:
                await self.disconnect()
                raise
        except Exception:
            await self.disconnect()
            raise
        return self._agent_info

    async def _handshake(self) -> dict[str, Any]:
        return await self.call(
            "handshake",
            handshake_params(PROTOCOL_VERSION),
        )

    def _should_restart_for_missing_capability(
        self, exc: TargetCallError, started_agent: bool
    ) -> bool:
        if not self._auto_start or started_agent or exc.code != "feature-unavailable":
            return False
        missing = exc.details.get("missing_capabilities")
        if not isinstance(missing, list):
            return False
        return any(str(capability) in REQUIRED_AGENT_CAPABILITIES for capability in missing)

    def _start_agent_or_raise(self) -> None:
        status = start_agent_daemon_process(self._socket_path)
        if status["status"] != "running":
            raise TargetCallError(
                "agent-unreachable",
                "unable to start local target agent daemon",
                status,
            )

    def _restart_agent_or_raise(self) -> None:
        status = restart_agent_daemon_process(self._socket_path)
        if status["status"] != "running":
            raise TargetCallError(
                "agent-unreachable",
                "unable to restart local target agent daemon",
                status,
            )

    async def _open_socket(self) -> None:
        self._reader, self._writer = await asyncio.open_unix_connection(
            str(self._socket_path)
        )

    async def disconnect(self) -> None:
        self._connected = False
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except BrokenPipeError:
                pass
        if self._reader_task is not None:
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
        self._fail_pending(RuntimeError("target client disconnected"))
        self._reader = None
        self._writer = None
        self._reader_task = None
        self._agent_info = None

    async def call(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if not self._connected or self._writer is None:
            raise RuntimeError("target client is not connected")
        if method != "subscribe" and self._subscription_tasks:
            await asyncio.gather(*list(self._subscription_tasks))
        self._request_seq += 1
        request_id = str(self._request_seq)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = future
        frame = {"id": request_id, "method": method, "params": params or {}}
        try:
            async with self._write_lock:
                self._writer.write(encode_frame(frame))
                await self._writer.drain()
        except Exception:
            self._pending.pop(request_id, None)
            raise
        return await future

    async def ping(self) -> dict[str, Any]:
        return await self.call("ping")

    def subscribe(
        self,
        run_ids: list[str],
        *,
        resume_from: object = "live",
    ) -> AsyncIterator[dict[str, Any]]:
        sub_id = uuid.uuid4().hex
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._event_subscribers[sub_id] = (set(run_ids), queue)
        subscribe_task = asyncio.create_task(
            self.call(
                "subscribe",
                {
                    "sub_id": sub_id,
                    "run_ids": list(run_ids),
                    "resume_from": resume_from,
                },
            )
        )
        self._subscription_tasks.add(subscribe_task)
        subscribe_task.add_done_callback(self._subscription_tasks.discard)

        async def events() -> AsyncIterator[dict[str, Any]]:
            active_sub_id = sub_id
            subscribed = False
            try:
                result = await subscribe_task
                active_sub_id = str(result.get("sub_id") or sub_id)
                subscribed = True
                while True:
                    yield await queue.get()
            finally:
                self._event_subscribers.pop(sub_id, None)
                if not subscribe_task.done():
                    subscribe_task.cancel()
                    with contextlib.suppress(Exception):
                        await subscribe_task
                if subscribed and self._connected:
                    with contextlib.suppress(Exception):
                        await self.call("unsubscribe", {"sub_id": active_sub_id})

        return events()

    async def _read_socket(self) -> None:
        assert self._reader is not None
        try:
            while line := await self._reader.readline():
                try:
                    frame = decode_frame(line)
                except NdjsonFrameError:
                    continue
                if "id" in frame:
                    self._resolve_response(frame)
                    continue
                if "event" in frame:
                    self._publish_event(frame)
        finally:
            self._connected = False
            self._fail_pending(_agent_unreachable_error("target agent socket closed"))

    def _resolve_response(self, frame: dict[str, Any]) -> None:
        request_id = str(frame.get("id"))
        future = self._pending.pop(request_id, None)
        if future is None or future.done():
            return
        error = frame.get("error")
        if isinstance(error, dict):
            future.set_exception(_target_call_error_from_payload(error))
            return
        result = frame.get("result")
        future.set_result(result if isinstance(result, dict) else {})

    def _publish_event(self, event: dict[str, Any]) -> None:
        for selected, queue in list(self._event_subscribers.values()):
            if event_matches_subscription(event, selected):
                queue.put_nowait(event)

    def _fail_pending(self, exc: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()


def _target_call_error_from_payload(payload: dict[str, Any]) -> TargetCallError:
    return target_call_error_from_rpc_payload(payload)


def _agent_unreachable_error(message: str) -> TargetCallError:
    return TargetCallError("agent-unreachable", message)


def _agent_socket_unreachable_error(socket_path: Path) -> TargetCallError:
    return TargetCallError(
        "agent-unreachable",
        f"Unable to connect to target agent socket: {socket_path}",
        {"socket_path": str(socket_path)},
    )
