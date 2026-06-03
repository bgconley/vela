from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from vllm_loader.agent.local import TargetCallError
from vllm_loader.transport.ndjson import NdjsonFrameError, decode_frame, encode_frame


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
        self._events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subscription_tasks: set[asyncio.Task[dict[str, Any]]] = set()
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        if self._connected:
            return
        env = os.environ.copy()
        if self._env is not None:
            env.update(self._env)
        self._process = await asyncio.create_subprocess_exec(
            *self._command,
            cwd=str(self._cwd) if self._cwd is not None else None,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._connected = True
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def disconnect(self) -> None:
        process = self._process
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

    async def call(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if not self._connected or self._process is None or self._process.stdin is None:
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
                self._process.stdin.write(encode_frame(frame))
                await self._process.stdin.drain()
        except Exception:
            self._pending.pop(request_id, None)
            raise
        return await future

    def subscribe(
        self,
        run_ids: list[str],
        *,
        resume_from: object = "live",
    ) -> AsyncIterator[dict[str, Any]]:
        subscribe_task = asyncio.create_task(
            self.call(
                "subscribe",
                {"run_ids": list(run_ids), "resume_from": resume_from},
            )
        )
        self._subscription_tasks.add(subscribe_task)
        subscribe_task.add_done_callback(self._subscription_tasks.discard)

        async def events() -> AsyncIterator[dict[str, Any]]:
            await subscribe_task
            selected = set(run_ids)
            while True:
                event = await self._events.get()
                run_id = event.get("run_id")
                if not selected or run_id in selected:
                    yield event

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
                    await self._events.put(frame)
        finally:
            self._connected = False
            self._fail_pending(RuntimeError("target process exited"))

    async def _drain_stderr(self) -> None:
        assert self._process is not None
        assert self._process.stderr is not None
        while await self._process.stderr.readline():
            pass

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

    def _fail_pending(self, exc: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()


def _target_call_error_from_payload(payload: dict[str, Any]) -> TargetCallError:
    code = str(payload.get("code") or "target-error")
    message = str(payload.get("message") or code)
    details = payload.get("details")
    return TargetCallError(code, message, details if isinstance(details, dict) else {})
