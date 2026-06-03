from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from typing import Any

from vllm_loader.agent.local import PROTOCOL_VERSION, LocalAgent


class InProcessTargetClient:
    def __init__(self, agent: LocalAgent) -> None:
        self._agent = agent
        self._connected = False
        self._agent_info: dict[str, Any] | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> dict[str, Any]:
        if self._connected and self._agent_info is not None:
            return self._agent_info
        self._connected = True
        try:
            self._agent_info = await self.call(
                "handshake",
                {"protocol_version": PROTOCOL_VERSION},
            )
        except Exception:
            self._connected = False
            self._agent_info = None
            raise
        return self._agent_info

    async def disconnect(self) -> None:
        self._connected = False
        self._agent_info = None

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
        return self._agent.subscribe(run_ids, resume_from=resume_from)
