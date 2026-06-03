from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from typing import Any

from vllm_loader.agent.local import LocalAgent


class InProcessTargetClient:
    def __init__(self, agent: LocalAgent) -> None:
        self._agent = agent
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self._connected:
            raise RuntimeError("target client is not connected")
        result = self._agent.handle(method, params)
        if inspect.isawaitable(result):
            return await result
        return result

    def subscribe(
        self,
        run_ids: list[str],
        *,
        resume_from: object = "live",
    ) -> AsyncIterator[dict[str, Any]]:
        if not self._connected:
            raise RuntimeError("target client is not connected")
        return self._agent.subscribe_run(run_ids, resume_from=resume_from)
