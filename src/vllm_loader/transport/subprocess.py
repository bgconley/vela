from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from vllm_loader.agent.local import PROTOCOL_VERSION, TargetCallError
from vllm_loader.transport.client import (
    event_matches_subscription,
    handshake_params,
)
from vllm_loader.transport.ndjson import (
    FRAME_STREAM_LIMIT,
    NdjsonFrameError,
    decode_frame,
    encode_frame,
)
from vllm_loader.transport.rpc_errors import target_call_error_from_rpc_payload

_STDERR_TAIL_LINES = 20
_STDERR_TAIL_CHARS = 4000
_EXIT_CONTEXT_TIMEOUT_SECONDS = 0.2


class SubprocessTargetClient:
    def __init__(
        self,
        command: Sequence[str],
        *,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._command = [str(part) for part in command]
        self._cwd = Path(cwd) if cwd is not None else None
        self._env = dict(env) if env is not None else None
        self._process: asyncio.subprocess.Process | None = None
        self._connected = False
        self._request_seq = 0
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._event_subscribers: dict[
            str, tuple[set[str], asyncio.Queue[dict[str, Any]]]
        ] = {}
        self._subscription_tasks: set[asyncio.Task[dict[str, Any]]] = set()
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._agent_info: dict[str, Any] | None = None
        self._stderr_tail: list[str] = []
        self._disconnecting = False

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> dict[str, Any]:
        if self._connected:
            return self._agent_info or {}
        self._disconnecting = False
        self._stderr_tail.clear()
        env = os.environ.copy()
        if self._env is not None:
            env.update(self._env)
        try:
            self._process = await asyncio.create_subprocess_exec(
                *self._command,
                cwd=str(self._cwd) if self._cwd is not None else None,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=FRAME_STREAM_LIMIT,
            )
        except OSError as exc:
            command = str(exc.filename or self._command[0])
            raise TargetCallError(
                "agent-unreachable",
                f"Unable to start target agent command: {command}",
                {"command": command},
            ) from exc
        self._connected = True
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        try:
            self._agent_info = await self.call(
                "handshake",
                handshake_params(PROTOCOL_VERSION),
            )
        except Exception:
            await self.disconnect()
            raise
        return self._agent_info

    async def disconnect(self) -> None:
        process = self._process
        self._disconnecting = True
        self._connected = False
        if process is not None and process.stdin is not None:
            process.stdin.close()
            try:
                await process.stdin.wait_closed()
            except BrokenPipeError:
                pass
        if process is not None:
            try:
                await asyncio.wait_for(process.wait(), timeout=1)
            except asyncio.TimeoutError:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=1)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *(task for task in (self._reader_task, self._stderr_task) if task is not None),
            return_exceptions=True,
        )
        self._fail_pending(RuntimeError("target client disconnected"))
        self._process = None
        self._reader_task = None
        self._stderr_task = None
        self._agent_info = None
        self._disconnecting = False

    async def call(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if not self._connected or self._process is None or self._process.stdin is None:
            raise RuntimeError("target client is not connected")
        if method != "subscribe" and self._subscription_tasks:
            await asyncio.gather(*list(self._subscription_tasks))
        self._request_seq += 1
        request_id = str(self._request_seq)
        frame = {"id": request_id, "method": method, "params": params or {}}
        payload = encode_frame(frame)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = future
        try:
            async with self._write_lock:
                self._process.stdin.write(payload)
                await self._process.stdin.drain()
        except OSError as exc:
            self._pending.pop(request_id, None)
            raise await self._agent_exit_error(
                "target agent process exited before accepting request"
            ) from exc
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
        all_runs: bool = False,
    ) -> AsyncIterator[dict[str, Any]]:
        sub_id = uuid.uuid4().hex
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._event_subscribers[sub_id] = (set(run_ids), queue)
        wire_all_runs = bool(all_runs or not run_ids)
        subscribe_task = asyncio.create_task(
            self.call(
                "subscribe",
                {
                    "sub_id": sub_id,
                    "run_ids": list(run_ids),
                    "all": wire_all_runs,
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

    async def _read_stdout(self) -> None:
        assert self._process is not None
        assert self._process.stdout is not None
        try:
            while line := await self._process.stdout.readline():
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
            if not self._disconnecting:
                self._fail_pending(
                    await self._agent_exit_error("target agent process exited")
                )

    async def _drain_stderr(self) -> None:
        assert self._process is not None
        assert self._process.stderr is not None
        while line := await self._process.stderr.readline():
            text = line.decode("utf-8", errors="replace").rstrip()
            if not text:
                continue
            self._stderr_tail.append(text)
            del self._stderr_tail[:-_STDERR_TAIL_LINES]

    async def _agent_exit_error(self, message: str) -> TargetCallError:
        exit_code = await self._process_exit_code()
        await self._wait_for_stderr_tail()
        return _agent_process_exit_error(
            message,
            exit_code=exit_code,
            stderr=_stderr_excerpt(self._stderr_tail),
        )

    async def _process_exit_code(self) -> int | None:
        process = self._process
        if process is None:
            return None
        if process.returncode is not None:
            return process.returncode
        try:
            await asyncio.wait_for(
                process.wait(),
                timeout=_EXIT_CONTEXT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            return process.returncode
        return process.returncode

    async def _wait_for_stderr_tail(self) -> None:
        stderr_task = self._stderr_task
        if (
            stderr_task is None
            or stderr_task.done()
            or stderr_task is asyncio.current_task()
        ):
            return
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(
                asyncio.shield(stderr_task),
                timeout=_EXIT_CONTEXT_TIMEOUT_SECONDS,
            )

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


def _agent_process_exit_error(
    message: str,
    *,
    exit_code: int | None,
    stderr: str,
) -> TargetCallError:
    details: dict[str, Any] = {}
    if exit_code is not None:
        details["exit_code"] = exit_code
    if stderr:
        details["stderr"] = stderr

    missing_command = _missing_agent_command(stderr)
    if exit_code == 127 or missing_command is not None:
        command = missing_command or "vllm-loader"
        details["command"] = command
        return TargetCallError(
            "command-not-found",
            f"Target agent command not found: {command}",
            details,
        )

    ssh_reason = _ssh_failure_reason(exit_code, stderr)
    if ssh_reason is not None:
        details["reason"] = ssh_reason
        return TargetCallError(
            "agent-unreachable",
            "SSH target agent bridge failed",
            details,
        )

    return TargetCallError("agent-unreachable", message, details)


def _stderr_excerpt(lines: Sequence[str]) -> str:
    text = "\n".join(lines)
    if len(text) <= _STDERR_TAIL_CHARS:
        return text
    return text[-_STDERR_TAIL_CHARS:]


def _missing_agent_command(stderr: str) -> str | None:
    for line in reversed(stderr.splitlines()):
        lowered = line.lower()
        if "command not found" not in lowered and "not found" not in lowered:
            continue
        parts = [part.strip() for part in line.split(":")]
        for part in reversed(parts[:-1]):
            if not part or part.isdigit() or part.lower() in {"bash", "sh"}:
                continue
            return part.split()[0]
    return None


def _ssh_failure_reason(exit_code: int | None, stderr: str) -> str | None:
    lowered = stderr.lower()
    if "permission denied" in lowered:
        return "ssh-auth"
    if "host key verification failed" in lowered:
        return "ssh-host-key"
    if "could not resolve hostname" in lowered:
        return "ssh-name-resolution"
    if any(
        phrase in lowered
        for phrase in (
            "connection refused",
            "connection timed out",
            "no route to host",
            "connection closed",
            "ssh_exchange_identification",
        )
    ):
        return "ssh-connect"
    if exit_code == 255:
        return "ssh-failed"
    return None
