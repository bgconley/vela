from __future__ import annotations

import httpx
import pytest

from vela.config.schema import ModelConfig
from vela.engine.phases import ErrorKind
from vela.monitoring.health import HealthEvent, check_once, probe_host_for, probe_loop


@pytest.mark.asyncio
async def test_health_called_without_auth_and_models_with_bearer_when_key_configured() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/health":
            return httpx.Response(200)
        return httpx.Response(200, json={"data": [{"id": "served"}]})

    cfg = ModelConfig.model_validate(
        {"name": "x", "model": "org/model", "server": {"api_key": "sk-live"}}
    )

    event = await check_once(cfg, transport=httpx.MockTransport(handler))

    assert event.ready
    assert event.models == ["served"]
    assert "Authorization" not in seen[0].headers
    assert seen[1].headers["Authorization"] == "Bearer sk-live"


@pytest.mark.asyncio
async def test_models_401_with_key_yields_specific_token_mismatch_hint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200)
        return httpx.Response(401)

    cfg = ModelConfig.model_validate(
        {"name": "x", "model": "org/model", "server": {"api_key": "sk-live"}}
    )

    event = await check_once(cfg, transport=httpx.MockTransport(handler))

    assert event.ready is False
    assert event.error_kind is ErrorKind.API_KEY_AUTH
    assert "Bearer" in event.detail


@pytest.mark.asyncio
async def test_models_401_without_configured_key_is_not_ready() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200)
        return httpx.Response(401)

    cfg = ModelConfig.model_validate({"name": "x", "model": "org/model"})

    event = await check_once(cfg, transport=httpx.MockTransport(handler))

    assert event.ready is False
    assert event.error_kind is ErrorKind.API_KEY_AUTH
    assert "api_key" in event.detail


@pytest.mark.asyncio
async def test_malformed_models_response_does_not_crash_health_probe() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200)
        return httpx.Response(200, content=b"not json")

    cfg = ModelConfig.model_validate({"name": "x", "model": "org/model"})

    event = await check_once(cfg, transport=httpx.MockTransport(handler))

    assert event.ready is True
    assert event.models == []
    assert "invalid JSON" in event.detail


@pytest.mark.asyncio
async def test_unexpected_models_payload_shape_does_not_crash_health_probe() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200)
        return httpx.Response(200, json={"data": "served"})

    cfg = ModelConfig.model_validate({"name": "x", "model": "org/model"})

    event = await check_once(cfg, transport=httpx.MockTransport(handler))

    assert event.ready is True
    assert event.models == []
    assert "unexpected /v1/models response" in event.detail


@pytest.mark.asyncio
async def test_connection_refused_during_loading_is_not_ready_yet() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    cfg = ModelConfig.model_validate({"name": "x", "model": "org/model"})

    event = await check_once(cfg, transport=httpx.MockTransport(handler))

    assert event == HealthEvent(ready=False, detail="not ready")


@pytest.mark.asyncio
async def test_connection_refused_during_model_probe_is_not_ready_yet() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200)
        raise httpx.ConnectError("refused", request=request)

    cfg = ModelConfig.model_validate({"name": "x", "model": "org/model"})

    event = await check_once(cfg, transport=httpx.MockTransport(handler))

    assert event == HealthEvent(ready=False, detail="not ready")


def test_probe_host_rules() -> None:
    wildcard = ModelConfig.model_validate(
        {"name": "x", "model": "org/model", "server": {"host": "0.0.0.0", "exposure": "lan"}}
    )
    lan = ModelConfig.model_validate(
        {"name": "x", "model": "org/model", "server": {"host": "192.168.1.5", "exposure": "lan"}}
    )
    override = ModelConfig.model_validate(
        {
            "name": "x",
            "model": "org/model",
            "server": {"host": "0.0.0.0", "exposure": "lan", "probe_host": "10.0.0.2"},
        }
    )

    assert probe_host_for(wildcard.server) == "127.0.0.1"
    assert probe_host_for(lan.server) == "127.0.0.1"
    assert probe_host_for(override.server) == "10.0.0.2"


@pytest.mark.asyncio
async def test_probe_loop_times_out_before_ready() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    cfg = ModelConfig.model_validate(
        {
            "name": "x",
            "model": "org/model",
            "launch": {"ready_timeout_seconds": 0, "health": {"interval_seconds": 0.01}},
        }
    )
    events: list[HealthEvent] = []

    await probe_loop(
        cfg,
        emit=events.append,
        is_process_alive=lambda: True,
        transport=httpx.MockTransport(handler),
        sleep=lambda _delay: None,
    )

    assert events[-1].error_kind is ErrorKind.TIMED_OUT
    assert "timeout" in events[-1].detail.lower()


@pytest.mark.asyncio
async def test_probe_loop_timeout_distinguishes_bound_but_unhealthy() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    cfg = ModelConfig.model_validate(
        {
            "name": "x",
            "model": "org/model",
            "launch": {"ready_timeout_seconds": 0, "health": {"interval_seconds": 0.01}},
        }
    )
    events: list[HealthEvent] = []

    await probe_loop(
        cfg,
        emit=events.append,
        is_process_alive=lambda: True,
        transport=httpx.MockTransport(handler),
        sleep=lambda _delay: None,
    )

    assert events[-1].error_kind is ErrorKind.TIMED_OUT
    assert "bound but unhealthy" in events[-1].detail
    assert "health returned 503" in events[-1].detail


@pytest.mark.asyncio
async def test_probe_loop_emits_health_error_kind_before_timeout() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if request.url.path == "/health":
            return httpx.Response(200)
        return httpx.Response(401)

    cfg = ModelConfig.model_validate(
        {
            "name": "x",
            "model": "org/model",
            "launch": {"ready_timeout_seconds": 999, "health": {"interval_seconds": 0.01}},
        }
    )
    events: list[HealthEvent] = []

    await probe_loop(
        cfg,
        emit=events.append,
        is_process_alive=lambda: calls < 2,
        transport=httpx.MockTransport(handler),
        sleep=lambda _delay: None,
    )

    assert [(event.error_kind, event.detail) for event in events] == [
        (ErrorKind.API_KEY_AUTH, "/v1/models requires auth; set server.api_key/VLLM_API_KEY")
    ]


@pytest.mark.asyncio
async def test_probe_loop_degrades_and_recovers_after_ready_auth_blip() -> None:
    responses = [
        httpx.Response(200),
        httpx.Response(200, json={"data": [{"id": "served"}]}),
        httpx.Response(200),
        httpx.Response(401),
        httpx.Response(200),
        httpx.Response(200, json={"data": [{"id": "served"}]}),
    ]
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        response = responses[min(calls, len(responses) - 1)]
        calls += 1
        return response

    cfg = ModelConfig.model_validate(
        {"name": "x", "model": "org/model", "launch": {"health": {"interval_seconds": 0.01}}}
    )
    events: list[HealthEvent] = []

    def alive() -> bool:
        return len(events) < 3

    await probe_loop(
        cfg,
        emit=events.append,
        is_process_alive=alive,
        transport=httpx.MockTransport(handler),
        sleep=lambda _delay: None,
    )

    assert [(event.ready, event.error_kind, event.detail) for event in events] == [
        (True, None, "ready"),
        (False, None, "/v1/models requires auth; set server.api_key/VLLM_API_KEY"),
        (True, None, "ready"),
    ]


@pytest.mark.asyncio
async def test_probe_loop_emits_degraded_and_recovery_after_ready() -> None:
    responses = [
        httpx.Response(200),
        httpx.Response(200, json={"data": [{"id": "served"}]}),
        httpx.Response(503),
        httpx.Response(200),
        httpx.Response(200, json={"data": [{"id": "served"}]}),
    ]
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        response = responses[min(calls, len(responses) - 1)]
        calls += 1
        return response

    cfg = ModelConfig.model_validate(
        {"name": "x", "model": "org/model", "launch": {"health": {"interval_seconds": 0.01}}}
    )
    events: list[HealthEvent] = []

    def alive() -> bool:
        return len(events) < 3

    await probe_loop(
        cfg,
        emit=events.append,
        is_process_alive=alive,
        transport=httpx.MockTransport(handler),
        sleep=lambda _delay: None,
    )

    assert [(event.ready, event.detail) for event in events] == [
        (True, "ready"),
        (False, "health returned 503"),
        (True, "ready"),
    ]
