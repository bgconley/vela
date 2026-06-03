from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol


class TargetClient(Protocol):
    @property
    def connected(self) -> bool: ...

    async def connect(self) -> dict[str, Any]: ...

    async def disconnect(self) -> None: ...

    async def call(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...

    async def ping(self) -> dict[str, Any]: ...

    def subscribe(
        self,
        run_ids: list[str],
        *,
        resume_from: object = "live",
    ) -> AsyncIterator[dict[str, Any]]: ...
