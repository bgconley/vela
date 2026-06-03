from __future__ import annotations

from typing import Any, Protocol


class TargetClient(Protocol):
    @property
    def connected(self) -> bool: ...

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def call(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...
