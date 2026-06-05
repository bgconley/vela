from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from typing import Any

from vllm_loader.agent.local import PROTOCOL_VERSION, LocalAgent
from vllm_loader.transport.client import handshake_params
from vllm_loader.transport.ndjson import decode_frame, encode_frame


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
                handshake_params(PROTOCOL_VERSION),
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
        request = _wire_round_trip({"params": params or {}})
        wire_params = request.get("params")
        result = self._agent.handle(method, wire_params if isinstance(wire_params, dict) else {})
        if inspect.isawaitable(result):
            result = await result
        response = _wire_round_trip({"result": result})
        wire_result = response.get("result")
        return wire_result if isinstance(wire_result, dict) else {}

    async def ping(self) -> dict[str, Any]:
        return await self.call("ping")

    def subscribe(
        self,
        run_ids: list[str],
        *,
        resume_from: object = "live",
        all_runs: bool = False,
    ) -> AsyncIterator[dict[str, Any]]:
        if not self._connected:
            raise RuntimeError("target client is not connected")
        wire_all_runs = bool(all_runs or not run_ids)
        request = _wire_round_trip(
            {
                "run_ids": list(run_ids),
                "resume_from": resume_from,
                "all": wire_all_runs,
            }
        )
        wire_run_ids = request.get("run_ids")
        wire_resume_from = request.get("resume_from", "live")
        wire_all = request.get("all")
        source_run_ids = wire_run_ids if isinstance(wire_run_ids, list) else []
        if wire_all is True:
            source = self._agent.subscribe(
                source_run_ids,
                resume_from=wire_resume_from,
                all_runs=True,
            )
        else:
            source = self._agent.subscribe(
                source_run_ids,
                resume_from=wire_resume_from,
            )

        async def events() -> AsyncIterator[dict[str, Any]]:
            try:
                async for event in source:
                    yield _wire_round_trip(event)
            finally:
                aclose = getattr(source, "aclose", None)
                if callable(aclose):
                    await aclose()

        return events()


def _wire_round_trip(frame: dict[str, Any]) -> dict[str, Any]:
    return decode_frame(encode_frame(frame))
