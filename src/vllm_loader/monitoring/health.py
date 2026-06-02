from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

from vllm_loader.config.schema import ModelConfig, ServerConfig
from vllm_loader.engine.phases import ErrorKind


@dataclass(frozen=True)
class HealthEvent:
    ready: bool
    detail: str
    models: list[str] | None = None
    error_kind: ErrorKind | None = None


def probe_host_for(server: ServerConfig) -> str:
    if server.probe_host:
        return server.probe_host
    if server.host in {"127.0.0.1", "localhost", "::1"}:
        return server.host
    return "127.0.0.1"


async def check_once(
    cfg: ModelConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float = 2.0,
) -> HealthEvent:
    host = probe_host_for(cfg.server)
    base_url = f"http://{host}:{cfg.server.port}"
    async with httpx.AsyncClient(transport=transport, timeout=timeout, base_url=base_url) as client:
        try:
            health = await client.get(cfg.launch.health.path)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout):
            return HealthEvent(ready=False, detail="not ready")
        except httpx.HTTPError as exc:
            return HealthEvent(ready=False, detail=str(exc))
        if health.status_code != 200:
            return HealthEvent(ready=False, detail=f"health returned {health.status_code}")

        headers = {}
        if cfg.server.api_key:
            headers["Authorization"] = f"Bearer {cfg.server.api_key}"
        try:
            models = await client.get("/v1/models", headers=headers)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout):
            return HealthEvent(ready=False, detail="not ready")
        except httpx.HTTPError as exc:
            return HealthEvent(ready=False, detail=str(exc))
        if models.status_code == 401 and cfg.server.api_key:
            return HealthEvent(
                ready=False,
                detail="Bearer token mismatch for /v1/models; check VLLM_API_KEY/api_key",
                error_kind=ErrorKind.HF_AUTH,
            )
        if models.status_code != 200:
            return HealthEvent(
                ready=True, detail=f"ready; /v1/models returned {models.status_code}", models=[]
            )
        try:
            data = models.json()
        except ValueError:
            return HealthEvent(
                ready=True, detail="ready; /v1/models returned invalid JSON", models=[]
            )
        names = _model_names_from_payload(data)
        if names is None:
            return HealthEvent(
                ready=True,
                detail="ready; unexpected /v1/models response shape",
                models=[],
            )
        return HealthEvent(ready=True, detail="ready", models=names)


async def probe_loop(
    cfg: ModelConfig,
    *,
    emit: Callable[[HealthEvent], None],
    is_process_alive: Callable[[], bool],
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Callable[[float], Awaitable[None] | None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    sleep_func = sleep or asyncio.sleep
    started_at = clock()
    was_ready = False
    last_ready: bool | None = None
    last_not_ready_detail = "not ready"

    while is_process_alive():
        event = await check_once(cfg, transport=transport)
        if event.ready:
            if not was_ready or last_ready is not True:
                emit(event)
            was_ready = True
            last_ready = True
        else:
            last_not_ready_detail = event.detail
            if was_ready:
                if last_ready is not False:
                    emit(event)
                last_ready = False
            elif clock() - started_at >= cfg.launch.ready_timeout_seconds:
                emit(
                    HealthEvent(
                        ready=False,
                        detail=_timeout_detail(
                            cfg.launch.ready_timeout_seconds,
                            last_not_ready_detail,
                        ),
                        error_kind=ErrorKind.TIMED_OUT,
                    )
                )
                return
        result = sleep_func(cfg.launch.health.interval_seconds)
        if result is not None:
            await result


def _timeout_detail(timeout_seconds: int, last_detail: str) -> str:
    if last_detail == "not ready":
        cause = "still loading or not bound yet"
    else:
        cause = f"bound but unhealthy: {last_detail}"
    return f"readiness timeout after {timeout_seconds}s; {cause}"


def _model_names_from_payload(data: object) -> list[str] | None:
    if not isinstance(data, dict):
        return None
    items = data.get("data", [])
    if not isinstance(items, list):
        return None
    names: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            return None
        if item_id := item.get("id"):
            names.append(str(item_id))
    return names
