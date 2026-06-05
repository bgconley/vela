from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol

from vllm_loader import __version__

REQUIRED_AGENT_CAPABILITIES = (
    "list_configs",
    "preview",
    "preflight",
    "prepare_launch",
    "launch",
    "wait",
    "stop",
    "kill",
    "restart",
    "gpu",
    "status",
    "health",
    "tail_detached",
    "discover_runs",
    "discover_runs_no_paths",
    "reattach",
    "subscribe",
    "unsubscribe",
)


def handshake_params(protocol_version: int) -> dict[str, Any]:
    return {
        "protocol_version": protocol_version,
        "controller_version": __version__,
        "capabilities": list(REQUIRED_AGENT_CAPABILITIES),
    }


def subscription_event_id(event: dict[str, Any]) -> str | None:
    for key in ("run_id", "job_id", "sub_id"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def event_matches_subscription(event: dict[str, Any], selected_ids: set[str]) -> bool:
    if not selected_ids:
        return True
    event_id = subscription_event_id(event)
    return event_id in selected_ids if event_id is not None else False


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
        all_runs: bool = False,
    ) -> AsyncIterator[dict[str, Any]]: ...
