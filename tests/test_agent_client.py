from __future__ import annotations

import asyncio
import inspect
import io
import json
import os
import signal
import socket
import sys
import threading
import time
import uuid
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import write_yaml

from vllm_loader import __version__
from vllm_loader.agent import local as local_agent_module
from vllm_loader.agent.local import LocalAgent, TargetCallError
from vllm_loader.engine import model_registry as model_registry_module
from vllm_loader.engine.phases import ErrorKind, Phase
from vllm_loader.engine.process_manager import DetachedLaunch
from vllm_loader.engine.sidecar import Manifest, Sidecar
from vllm_loader.monitoring.gpu import GpuPollResult, GpuSample
from vllm_loader.monitoring.health import HealthEvent
from vllm_loader.transport.client import REQUIRED_AGENT_CAPABILITIES
from vllm_loader.transport.inprocess import InProcessTargetClient


def _subprocess_target_client_class():
    try:
        from vllm_loader.transport.subprocess import SubprocessTargetClient
    except ModuleNotFoundError as exc:
        pytest.fail(f"SubprocessTargetClient missing: {exc}")
    return SubprocessTargetClient


def _agent_connect_command() -> list[str]:
    return [sys.executable, "-m", "vllm_loader.cli", "agent", "connect"]


def _agent_connect_socket_command(socket_path: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "vllm_loader.cli",
        "agent",
        "connect",
        "--socket",
        str(socket_path),
    ]


def _short_socket_path() -> Path:
    return Path("/tmp") / f"vllm-loader-agent-{uuid.uuid4().hex}.sock"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _next_event(events, *, event_name: str) -> dict:
    while True:
        event = await anext(events)
        if event.get("event") == event_name:
            return event


@pytest.fixture(autouse=True)
def _isolate_hf_cache_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        model_registry_module,
        "_scan_hf_cache_info",
        lambda: None,
        raising=False,
    )
    monkeypatch.setattr(
        model_registry_module,
        "_hf_model_info",
        lambda repo_id, revision=None: None,
        raising=False,
    )


def test_target_client_requires_lifecycle_capabilities() -> None:
    assert set(REQUIRED_AGENT_CAPABILITIES) >= {
        "list_configs",
        "preview",
        "prepare_launch",
        "launch",
        "wait",
        "stop",
        "kill",
        "gpu",
        "health",
        "status",
        "tail_detached",
        "discover_runs",
        "discover_runs_no_paths",
        "reattach",
        "subscribe",
        "unsubscribe",
    }


@pytest.mark.asyncio
async def test_agent_connect_bridges_stdio_to_unix_socket_agent() -> None:
    from vllm_loader.agent.socket import serve_unix_socket_agent

    socket_path = _short_socket_path()
    server = await serve_unix_socket_agent(
        LocalAgent(target_name="socket-local"),
        socket_path,
    )
    client = _subprocess_target_client_class()(_agent_connect_socket_command(socket_path))
    try:
        connected = await client.connect()

        assert connected["protocol_version"] == 1
        assert connected["target"] == "socket-local"
    finally:
        await client.disconnect()
        server.close()
        await server.wait_closed()
        socket_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_unix_socket_target_client_handshake_exposes_socket_agent() -> None:
    from vllm_loader.agent.socket import serve_unix_socket_agent
    from vllm_loader.transport.socket import UnixSocketTargetClient

    socket_path = _short_socket_path()
    server = await serve_unix_socket_agent(
        LocalAgent(target_name="socket-client-local"),
        socket_path,
    )
    client = UnixSocketTargetClient(socket_path, auto_start=False)
    try:
        connected = await client.connect()
        result = await client.call("handshake")

        assert client.connected is True
        assert connected["protocol_version"] == 1
        assert connected["controller_version"] == __version__
        assert connected["target"] == "socket-client-local"
        assert result["target"] == "socket-client-local"
    finally:
        await client.disconnect()
        server.close()
        await server.wait_closed()
        socket_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_unix_socket_target_client_ping_returns_agent_timestamps() -> None:
    from vllm_loader.agent.socket import serve_unix_socket_agent
    from vllm_loader.transport.socket import UnixSocketTargetClient

    socket_path = _short_socket_path()
    server = await serve_unix_socket_agent(
        LocalAgent(target_name="socket-client-local"),
        socket_path,
    )
    client = UnixSocketTargetClient(socket_path, auto_start=False)
    try:
        await client.connect()

        result = await client.call("ping")

        assert result["pong"] is True
        assert result["target"] == "socket-client-local"
        assert isinstance(result["ts"], str)
        assert isinstance(result["mono"], float)
    finally:
        await client.disconnect()
        server.close()
        await server.wait_closed()
        socket_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_unix_socket_target_client_ping_method_uses_ping_rpc() -> None:
    from vllm_loader.agent.socket import serve_unix_socket_agent
    from vllm_loader.transport.socket import UnixSocketTargetClient

    socket_path = _short_socket_path()
    server = await serve_unix_socket_agent(
        LocalAgent(target_name="socket-client-ping-method"),
        socket_path,
    )
    client = UnixSocketTargetClient(socket_path, auto_start=False)
    try:
        await client.connect()

        result = await client.ping()

        assert result["pong"] is True
        assert result["target"] == "socket-client-ping-method"
    finally:
        await client.disconnect()
        server.close()
        await server.wait_closed()
        socket_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_unix_socket_target_client_auto_starts_missing_socket_daemon() -> None:
    from vllm_loader.agent.daemon import agent_identity_path, stop_agent_daemon
    from vllm_loader.transport.socket import UnixSocketTargetClient

    socket_path = _short_socket_path()
    identity_path = agent_identity_path(socket_path)
    client = UnixSocketTargetClient(socket_path)
    try:
        connected = await client.connect()

        assert connected["target"] == "local"
        assert connected["daemon_pid"] > 0
        assert identity_path.exists()
    finally:
        await client.disconnect()
        stop_agent_daemon(socket_path)
        socket_path.unlink(missing_ok=True)
        identity_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_unix_socket_target_client_auto_starts_refused_stale_socket() -> None:
    from vllm_loader.agent.daemon import agent_identity_path, stop_agent_daemon
    from vllm_loader.agent.socket import serve_unix_socket_agent
    from vllm_loader.transport.socket import UnixSocketTargetClient

    socket_path = _short_socket_path()
    identity_path = agent_identity_path(socket_path)
    server = await serve_unix_socket_agent(LocalAgent(), socket_path)
    server.close()
    await server.wait_closed()
    assert socket_path.exists()
    client = UnixSocketTargetClient(socket_path)
    try:
        connected = await client.connect()

        assert connected["target"] == "local"
        assert connected["daemon_pid"] > 0
        assert identity_path.exists()
    finally:
        await client.disconnect()
        stop_agent_daemon(socket_path)
        socket_path.unlink(missing_ok=True)
        identity_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_unix_socket_target_client_restarts_daemon_missing_required_capability() -> None:
    from vllm_loader.agent.daemon import agent_identity_path, stop_agent_daemon
    from vllm_loader.agent.socket import serve_unix_socket_agent
    from vllm_loader.transport.socket import UnixSocketTargetClient

    required_capabilities = (
        "health",
        "discover_runs",
        "discover_runs_no_paths",
        "reattach",
    )

    class LegacyCapabilityAgent(LocalAgent):
        def handle(self, method: str, params: dict | None = None):
            if method == "handshake":
                missing = [
                    str(capability)
                    for capability in (params or {}).get("capabilities", [])
                    if str(capability) in required_capabilities
                ]
                if missing:
                    raise TargetCallError(
                        "feature-unavailable",
                        "target agent does not support requested capabilities",
                        {"missing_capabilities": missing},
                    )
                result = super().handle(method, params)
                result["capabilities"] = [
                    capability
                    for capability in result.get("capabilities", [])
                    if capability not in required_capabilities
                ]
                return result
            return super().handle(method, params)

    socket_path = _short_socket_path()
    identity_path = agent_identity_path(socket_path)
    legacy_server = await serve_unix_socket_agent(LegacyCapabilityAgent(), socket_path)
    client = UnixSocketTargetClient(socket_path)
    try:
        connected = await client.connect()

        assert connected["target"] == "local"
        assert connected["daemon_pid"] > 0
        assert identity_path.exists()
        assert set(required_capabilities).issubset(connected["capabilities"])
    finally:
        await client.disconnect()
        legacy_server.close()
        await legacy_server.wait_closed()
        stop_agent_daemon(socket_path)
        socket_path.unlink(missing_ok=True)
        identity_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_in_process_target_client_handshake_exposes_local_agent() -> None:
    client = InProcessTargetClient(LocalAgent(target_name="local"))

    assert client.connected is False

    connected = await client.connect()
    result = await client.call("handshake")

    assert client.connected is True
    assert connected["protocol_version"] == 1
    assert connected["controller_version"] == __version__
    assert connected["target"] == "local"
    assert result["protocol_version"] == 1
    assert result["target"] == "local"
    assert "list_configs" in result["capabilities"]
    assert "preview" in result["capabilities"]
    assert "gpu" in result["capabilities"]
    assert "health" in result["capabilities"]
    assert "status" in result["capabilities"]
    assert "discover_runs" in result["capabilities"]
    assert "discover_runs_no_paths" in result["capabilities"]
    assert "reattach" in result["capabilities"]
    assert "list_builds" in result["capabilities"]
    assert "list_models" in result["capabilities"]
    assert "unsubscribe" in result["capabilities"]
    assert result["daemon_pid"] > 0
    assert result["daemon_start_ts"]
    assert result["host_info"]["vllm_loader_version"] == result["agent_version"]
    assert result["host_info"]["hostname"]
    assert result["host_info"]["platform"]

    await client.disconnect()
    assert client.connected is False


def test_local_agent_handshake_downgrades_for_older_controller_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(local_agent_module, "PROTOCOL_VERSION", 2)
    agent = LocalAgent(target_name="local")

    result = agent.handle(
        "handshake",
        {
            "protocol_version": 1,
            "controller_version": "controller-0.9.0",
        },
    )

    assert result["protocol_version"] == 1
    assert result["agent_protocol_version"] == 2
    assert result["controller_version"] == "controller-0.9.0"
    assert "driver" in result["host_info"]


@pytest.mark.asyncio
async def test_in_process_target_client_ping_returns_agent_timestamps() -> None:
    client = InProcessTargetClient(LocalAgent(target_name="local-ping"))

    await client.connect()
    try:
        result = await client.call("ping")
    finally:
        await client.disconnect()

    assert result["pong"] is True
    assert result["target"] == "local-ping"
    assert isinstance(result["ts"], str)
    assert isinstance(result["mono"], float)


@pytest.mark.asyncio
async def test_in_process_target_client_ping_method_uses_ping_rpc() -> None:
    client = InProcessTargetClient(LocalAgent(target_name="local-ping-method"))

    await client.connect()
    try:
        result = await client.ping()
    finally:
        await client.disconnect()

    assert result["pong"] is True
    assert result["target"] == "local-ping-method"


@pytest.mark.asyncio
async def test_in_process_target_client_rejects_non_serializable_call_params() -> None:
    client = InProcessTargetClient(LocalAgent())

    await client.connect()
    try:
        with pytest.raises(TypeError, match="JSON serializable"):
            await client.call("ping", {"path": Path("/tmp/agent-local")})
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_in_process_target_client_rejects_non_serializable_call_results() -> None:
    class PathReturningAgent(LocalAgent):
        def handle(self, method: str, params: dict | None = None):
            if method == "leak_path":
                return {"path": Path("/tmp/agent-local")}
            return super().handle(method, params)

    client = InProcessTargetClient(PathReturningAgent())

    await client.connect()
    try:
        with pytest.raises(TypeError, match="JSON serializable"):
            await client.call("leak_path")
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_in_process_target_client_rejects_non_serializable_events() -> None:
    class PathEventAgent(LocalAgent):
        def subscribe(self, run_ids, *, resume_from: object = "live"):
            async def events():
                yield {
                    "event": "log",
                    "run_id": str(next(iter(run_ids))),
                    "path": Path("/tmp/agent-local"),
                }

            return events()

    client = InProcessTargetClient(PathEventAgent())

    await client.connect()
    events = client.subscribe(["run-1"], resume_from="live")
    try:
        with pytest.raises(TypeError, match="JSON serializable"):
            await anext(events)
    finally:
        await events.aclose()
        await client.disconnect()


@pytest.mark.asyncio
async def test_subprocess_target_client_ping_returns_agent_timestamps() -> None:
    client = _subprocess_target_client_class()(_agent_connect_command())

    await client.connect()
    try:
        result = await client.call("ping")
    finally:
        await client.disconnect()

    assert result["pong"] is True
    assert result["target"] == "local"
    assert isinstance(result["ts"], str)
    assert isinstance(result["mono"], float)


@pytest.mark.asyncio
async def test_subprocess_target_client_ping_method_uses_ping_rpc() -> None:
    client = _subprocess_target_client_class()(_agent_connect_command())

    await client.connect()
    try:
        result = await client.ping()
    finally:
        await client.disconnect()

    assert result["pong"] is True
    assert result["target"] == "local"


@pytest.mark.asyncio
async def test_in_process_target_client_requires_connection() -> None:
    client = InProcessTargetClient(LocalAgent())

    with pytest.raises(RuntimeError, match="not connected"):
        await client.call("handshake")


@pytest.mark.asyncio
async def test_in_process_target_client_handshake_rejects_newer_controller_protocol() -> None:
    client = InProcessTargetClient(LocalAgent())

    await client.connect()
    with pytest.raises(TargetCallError) as exc_info:
        await client.call("handshake", {"protocol_version": 2})

    assert exc_info.value.code == "version-mismatch"
    assert exc_info.value.details == {
        "required": 2,
        "actual": 1,
    }


@pytest.mark.asyncio
async def test_in_process_target_client_handshake_rejects_missing_capability() -> None:
    client = InProcessTargetClient(LocalAgent())

    await client.connect()
    with pytest.raises(TargetCallError) as exc_info:
        await client.call("handshake", {"capabilities": ["models"]})

    assert exc_info.value.code == "feature-unavailable"
    assert exc_info.value.details == {"missing_capabilities": ["models"]}


def test_local_agent_lifecycle_boundary_is_handle_and_subscribe_only() -> None:
    public_lifecycle_helpers = {
        "start_attached_run",
        "start_detached_run",
        "stop_run",
        "kill_run",
        "wait_attached_run",
        "probe_run_until_ready",
        "reattach_detached_run",
        "tail_detached_run",
        "discover_detached_runs",
        "subscribe_run",
    }

    assert public_lifecycle_helpers.isdisjoint(LocalAgent.__dict__)
    assert {f"_{name}" for name in public_lifecycle_helpers}.isdisjoint(
        LocalAgent.__dict__
    )
    assert "handle" in LocalAgent.__dict__
    assert "subscribe" in LocalAgent.__dict__


def test_local_agent_lifecycle_helpers_do_not_accept_controller_callbacks() -> None:
    lifecycle_helpers = [
        LocalAgent._spawn_detached_supervisor,
        LocalAgent._tail_detached_log_to_events,
    ]

    for helper in lifecycle_helpers:
        parameter_names = set(inspect.signature(helper).parameters)
        assert "emit" not in parameter_names
        assert "emit_event" not in parameter_names


@pytest.mark.asyncio
async def test_subprocess_target_client_handshake_exposes_agent() -> None:
    client = _subprocess_target_client_class()(_agent_connect_command())

    connected = await client.connect()
    try:
        result = await client.call("handshake")
    finally:
        await client.disconnect()

    assert connected["protocol_version"] == 1
    assert connected["controller_version"] == __version__
    assert connected["target"] == "local"
    assert result["protocol_version"] == 1
    assert result["target"] == "local"
    assert "status" in result["capabilities"]
    assert "gpu" in result["capabilities"]
    assert "list_configs" in result["capabilities"]
    assert "unsubscribe" in result["capabilities"]
    assert result["daemon_pid"] > 0
    assert result["daemon_start_ts"]
    assert result["host_info"]["vllm_loader_version"] == result["agent_version"]
    assert result["host_info"]["hostname"]
    assert result["host_info"]["platform"]


@pytest.mark.asyncio
async def test_subprocess_target_client_handshake_rejects_newer_controller_protocol() -> None:
    client = _subprocess_target_client_class()(_agent_connect_command())

    await client.connect()
    try:
        with pytest.raises(TargetCallError) as exc_info:
            await client.call("handshake", {"protocol_version": 2})
    finally:
        await client.disconnect()

    assert exc_info.value.code == "version-mismatch"
    assert exc_info.value.details == {
        "required": 2,
        "actual": 1,
    }


@pytest.mark.asyncio
async def test_subprocess_target_client_handshake_rejects_missing_capability() -> None:
    client = _subprocess_target_client_class()(_agent_connect_command())

    await client.connect()
    try:
        with pytest.raises(TargetCallError) as exc_info:
            await client.call("handshake", {"capabilities": ["models"]})
    finally:
        await client.disconnect()

    assert exc_info.value.code == "feature-unavailable"
    assert exc_info.value.details == {"missing_capabilities": ["models"]}


@pytest.mark.asyncio
async def test_subprocess_target_client_lists_configs_from_agent(
    config_dir: Path,
) -> None:
    write_yaml(config_dir / "remoteish.yaml", "name: remoteish\nmodel: org/model")
    client = _subprocess_target_client_class()(_agent_connect_command())

    await client.connect()
    try:
        result = await client.call("list_configs", {"configs_dir": str(config_dir)})
    finally:
        await client.disconnect()

    assert result["valid"][0]["name"] == "remoteish"
    assert result["valid"][0]["path"].endswith("remoteish.yaml")


@pytest.mark.asyncio
async def test_subprocess_target_client_reconstructs_target_call_errors(
    config_dir: Path,
) -> None:
    client = _subprocess_target_client_class()(_agent_connect_command())

    await client.connect()
    try:
        with pytest.raises(TargetCallError) as exc_info:
            await client.call("preview", {"name": "missing", "configs_dir": str(config_dir)})
    finally:
        await client.disconnect()

    assert exc_info.value.code == "unknown-config"
    assert exc_info.value.details["name"] == "missing"


@pytest.mark.asyncio
async def test_subprocess_target_client_demuxes_run_events(
    config_dir: Path, tmp_path: Path
) -> None:
    child = tmp_path / "child.py"
    child.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import sys",
                "import time",
                "print('INFO Starting to load model', flush=True)",
                "print('Loading checkpoint shards: 45% 1/2', end='\\r', flush=True)",
                "time.sleep(0.05)",
                "print('INFO Uvicorn running on http://127.0.0.1:8000', flush=True)",
                "sys.exit(0)",
            ]
        ),
        encoding="utf-8",
    )
    child.chmod(0o755)
    write_yaml(
        config_dir / "rpc-events.yaml",
        f"""
        name: rpc-events
        model: fake/model
        command:
          entrypoint: serve
          executable: {child}
        server:
          port: {_free_port()}
        launch:
          runs_dir: {tmp_path / "runs"}
        """,
    )
    client = _subprocess_target_client_class()(_agent_connect_command())

    await client.connect()
    events = client.subscribe(["rpc-run"], resume_from="live")
    try:
        launch = await client.call(
            "launch",
            {"run_id": "rpc-run", "name": "rpc-events", "configs_dir": str(config_dir)},
        )
        wait_task = asyncio.create_task(client.call("wait", {"run_id": "rpc-run"}))
        observed: dict[str, dict] = {}
        for _ in range(6):
            item = await asyncio.wait_for(events.__anext__(), timeout=5)
            if item.get("event") in {"log", "progress"}:
                observed[str(item["event"])] = item
            if {"log", "progress"}.issubset(observed):
                break
        wait_result = await wait_task
    finally:
        await events.aclose()
        await client.disconnect()

    assert launch["run_id"] == "rpc-run"
    event = observed["log"]
    progress = observed["progress"]
    assert event["run_id"] == "rpc-run"
    assert event["event"] == "log"
    assert event["text"] == "INFO Starting to load model"
    assert progress["run_id"] == "rpc-run"
    assert progress["event"] == "progress"
    assert progress["text"] == "Loading checkpoint shards: 45% 1/2"
    assert wait_result["returncode"] == 0
    durable_log = tmp_path / "runs" / "rpc-run.run.log"
    event_spool = tmp_path / "runs" / "rpc-run.events.ndjson"
    assert "Loading checkpoint shards" not in durable_log.read_text(encoding="utf-8")
    assert "Loading checkpoint shards" in event_spool.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_subprocess_target_client_fans_out_concurrent_subscriptions(
    tmp_path: Path,
) -> None:
    bridge = tmp_path / "fake_bridge.py"
    bridge.write_text(
        "\n".join(
            [
                "import json",
                "import sys",
                "for line in sys.stdin:",
                "    frame = json.loads(line)",
                "    request_id = frame.get('id')",
                "    method = frame.get('method')",
                "    params = frame.get('params') or {}",
                "    if method == 'handshake':",
                "        result = {",
                "            'protocol_version': params.get('protocol_version', 1),",
                "            'target': 'fake',",
                "            'capabilities': params.get('capabilities', []),",
                "            'agent_version': 'test',",
                "            'daemon_pid': 123,",
                "            'daemon_start_ts': '2026-06-03T00:00:00Z',",
                "            'host_info': {},",
                "        }",
                "        print(json.dumps({'id': request_id, 'result': result}), flush=True)",
                "    elif method == 'subscribe':",
                "        print(json.dumps({'id': request_id, 'result': "
                "{'sub_id': params.get('sub_id')}}), flush=True)",
                "    elif method == 'emit_run':",
                "        print(json.dumps({",
                "            'event': 'log',",
                "            'run_id': 'run-1',",
                "            'kind': 'committed',",
                "            'text': 'INFO run line',",
                "            'level': 'INFO',",
                "            'seq': 1,",
                "            'ts': '2026-06-03T00:00:01Z',",
                "            'mono': 1.0,",
                "        }), flush=True)",
                "        print(json.dumps({",
                "            'event': 'gpu',",
                "            'run_id': '__agent__',",
                "            'sub_id': 'gpu-panel',",
                "            'samples': [],",
                "            'note': '',",
                "            'unavailable': False,",
                "            'seq': 2,",
                "            'ts': '2026-06-03T00:00:02Z',",
                "            'mono': 2.0,",
                "        }), flush=True)",
                "        print(json.dumps({'id': request_id, 'result': {}}), flush=True)",
                "    elif method == 'unsubscribe':",
                "        print(json.dumps({'id': request_id, 'result': "
                "{'sub_id': params.get('sub_id')}}), flush=True)",
                "    else:",
                "        print(json.dumps({'id': request_id, 'result': {}}), flush=True)",
            ]
        ),
        encoding="utf-8",
    )
    client = _subprocess_target_client_class()([sys.executable, str(bridge)])

    await client.connect()
    gpu_events = client.subscribe(["__agent__"], resume_from="live")
    run_events = client.subscribe(["run-1"], resume_from="live")
    try:
        gpu_task = asyncio.create_task(gpu_events.__anext__())
        await client.call("emit_run", {})
        gpu_event = await asyncio.wait_for(gpu_task, timeout=2)
        run_event = await asyncio.wait_for(run_events.__anext__(), timeout=2)
    finally:
        await gpu_events.aclose()
        await run_events.aclose()
        await client.disconnect()

    assert gpu_event["event"] == "gpu"
    assert gpu_event["run_id"] == "__agent__"
    assert run_event["event"] == "log"
    assert run_event["run_id"] == "run-1"
    assert run_event["text"] == "INFO run line"


@pytest.mark.asyncio
async def test_subprocess_target_client_unsubscribes_when_event_stream_closes(
    tmp_path: Path,
) -> None:
    calls_path = tmp_path / "calls.ndjson"
    bridge = tmp_path / "fake_bridge.py"
    bridge.write_text(
        "\n".join(
            [
                "import json",
                "import sys",
                f"calls_path = {str(calls_path)!r}",
                "def write_call(method, params):",
                "    with open(calls_path, 'a', encoding='utf-8') as calls:",
                "        calls.write("
                "json.dumps({'method': method, 'params': params}, sort_keys=True) + '\\n')",
                "for line in sys.stdin:",
                "    frame = json.loads(line)",
                "    request_id = frame.get('id')",
                "    method = frame.get('method')",
                "    params = frame.get('params') or {}",
                "    write_call(method, params)",
                "    if method == 'handshake':",
                "        result = {",
                "            'protocol_version': params.get('protocol_version', 1),",
                "            'target': 'fake',",
                "            'capabilities': params.get('capabilities', []),",
                "            'agent_version': 'test',",
                "            'daemon_pid': 123,",
                "            'daemon_start_ts': '2026-06-03T00:00:00Z',",
                "            'host_info': {},",
                "        }",
                "        print(json.dumps({'id': request_id, 'result': result}), flush=True)",
                "    elif method == 'subscribe':",
                "        sub_id = params.get('sub_id')",
                "        print(json.dumps("
                "{'id': request_id, 'result': {'sub_id': sub_id}}), flush=True)",
                "        print(json.dumps({",
                "            'event': 'log',",
                "            'run_id': params.get('run_ids', ['run-1'])[0],",
                "            'kind': 'committed',",
                "            'text': 'INFO one line',",
                "            'level': 'INFO',",
                "            'seq': 1,",
                "            'ts': '2026-06-03T00:00:01Z',",
                "            'mono': 1.0,",
                "        }), flush=True)",
                "    elif method == 'unsubscribe':",
                "        print(json.dumps({'id': request_id, 'result': "
                "{'sub_id': params.get('sub_id')}}), flush=True)",
                "    else:",
                "        print(json.dumps({'id': request_id, 'result': {}}), flush=True)",
            ]
        ),
        encoding="utf-8",
    )
    client = _subprocess_target_client_class()([sys.executable, str(bridge)])

    await client.connect()
    events = client.subscribe(["run-1"], resume_from="live")
    try:
        event = await asyncio.wait_for(events.__anext__(), timeout=2)
    finally:
        await events.aclose()
        await client.disconnect()

    calls = [
        json.loads(line)
        for line in calls_path.read_text(encoding="utf-8").splitlines()
    ]
    subscribe_call = next(call for call in calls if call["method"] == "subscribe")
    unsubscribe_call = next(call for call in calls if call["method"] == "unsubscribe")

    assert event["text"] == "INFO one line"
    assert subscribe_call["params"]["run_ids"] == ["run-1"]
    assert subscribe_call["params"]["resume_from"] == "live"
    assert isinstance(subscribe_call["params"]["sub_id"], str)
    assert subscribe_call["params"]["sub_id"]
    assert unsubscribe_call["params"] == {
        "sub_id": subscribe_call["params"]["sub_id"]
    }


@pytest.mark.asyncio
async def test_local_agent_lists_configs_from_agent_side_registry(config_dir: Path) -> None:
    write_yaml(
        config_dir / "blackbird.yaml",
        """
        name: blackbird-qwen
        target: blackbird
        model: Qwen/Qwen3.6-27B-FP8
        """,
    )
    write_yaml(config_dir / "broken.yaml", "name: broken")
    client = InProcessTargetClient(LocalAgent())

    await client.connect()
    result = await client.call("list_configs", {"configs_dir": str(config_dir)})

    assert result["valid"][0]["name"] == "blackbird-qwen"
    assert result["valid"][0]["model"] == "Qwen/Qwen3.6-27B-FP8"
    assert result["valid"][0]["target"] == "blackbird"
    assert result["valid"][0]["path"].endswith("blackbird.yaml")
    assert result["valid"][0]["warnings"] == []
    assert result["invalid"][0]["path"].endswith("broken.yaml")
    assert result["invalid"][0]["errors"]


@pytest.mark.asyncio
async def test_local_agent_preview_matches_existing_command_shape(config_dir: Path) -> None:
    write_yaml(
        config_dir / "preview.yaml",
        """
        name: preview
        model: org/model
        vllm:
          version_profile: current
        server:
          port: 8012
        """,
    )
    client = InProcessTargetClient(LocalAgent())

    await client.connect()
    result = await client.call("preview", {"name": "preview", "configs_dir": str(config_dir)})

    assert result["preview"].startswith("cwd=")
    assert "vllm serve org/model" in result["preview"]
    assert "--port 8012" in result["preview"]
    assert result["warnings"] == []


@pytest.mark.asyncio
async def test_local_agent_prepare_launch_uses_command_cwd_for_relative_model(
    config_dir: Path, tmp_path: Path
) -> None:
    work_dir = tmp_path / "serve-root"
    model_dir = work_dir / "relative-model"
    model_dir.mkdir(parents=True)
    write_yaml(
        config_dir / "cwd-local.yaml",
        f"""
        name: cwd-local
        model: relative-model
        command:
          cwd: {work_dir}
        """,
    )
    client = InProcessTargetClient(LocalAgent())

    await client.connect()
    result = await client.call(
        "prepare_launch",
        {"name": "cwd-local", "configs_dir": str(config_dir)},
    )

    assert result["build"]["cwd"] == str(work_dir)
    assert result["build"]["preview"].startswith(f"cwd={work_dir}\n")
    assert result["build"]["argv"][:3] == ["vllm", "serve", "relative-model"]


@pytest.mark.asyncio
async def test_local_agent_preview_reports_unknown_config(config_dir: Path) -> None:
    client = InProcessTargetClient(LocalAgent())

    await client.connect()
    with pytest.raises(TargetCallError) as exc_info:
        await client.call("preview", {"name": "missing", "configs_dir": str(config_dir)})

    assert exc_info.value.code == "unknown-config"


@pytest.mark.asyncio
async def test_local_agent_prepare_launch_returns_serialized_build(config_dir: Path) -> None:
    write_yaml(
        config_dir / "launch.yaml",
        """
        name: launch
        model: org/model
        vllm:
          version_profile: current
        server:
          port: 8017
        """,
    )
    client = InProcessTargetClient(LocalAgent())

    await client.connect()
    result = await client.call("prepare_launch", {"name": "launch", "configs_dir": str(config_dir)})

    assert result["config"]["name"] == "launch"
    assert result["build"]["argv"][:3] == ["vllm", "serve", "org/model"]
    assert result["build"]["env"]["PYTHONUNBUFFERED"] == "1"
    assert result["build"]["cwd"]
    assert result["build"]["warnings"] == []
    assert result["preflight"] is None


@pytest.mark.asyncio
async def test_local_agent_prepare_launch_reports_preflight_failure(
    config_dir: Path, tmp_path: Path
) -> None:
    missing_model = tmp_path / "missing-model"
    write_yaml(
        config_dir / "missing.yaml",
        f"""
        name: missing
        model: {missing_model}
        """,
    )
    client = InProcessTargetClient(LocalAgent())

    await client.connect()
    with pytest.raises(TargetCallError) as exc_info:
        await client.call("prepare_launch", {"name": "missing", "configs_dir": str(config_dir)})

    assert exc_info.value.code == "preflight-failed"
    assert exc_info.value.details["kind"] == "MODEL_NOT_FOUND"
    assert str(missing_model) in exc_info.value.details["detail"]


@pytest.mark.asyncio
async def test_local_agent_starts_and_stops_attached_run_by_run_id(
    config_dir: Path, tmp_path: Path
) -> None:
    marker = tmp_path / "marker.txt"
    child = tmp_path / "child.py"
    child.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import signal",
                "import time",
                "from pathlib import Path",
                f"marker = Path({str(marker)!r})",
                "marker.write_text('started', encoding='utf-8')",
                "def stop(signum, frame):",
                "    marker.write_text('stopped', encoding='utf-8')",
                "    raise SystemExit(0)",
                "signal.signal(signal.SIGINT, stop)",
                "while True:",
                "    time.sleep(0.05)",
            ]
        ),
        encoding="utf-8",
    )
    child.chmod(0o755)
    write_yaml(
        config_dir / "attached.yaml",
        f"""
        name: attached
        model: fake/model
        command:
          entrypoint: serve
          executable: {child}
        launch:
          runs_dir: {tmp_path / "runs"}
        """,
    )
    agent = LocalAgent()
    client = InProcessTargetClient(agent)
    await client.connect()
    run_id: str | None = None
    try:
        launch = await client.call(
            "launch", {"name": "attached", "configs_dir": str(config_dir)}
        )
        run_id = str(launch["run_id"])
        for _ in range(100):
            if marker.exists():
                break
            await asyncio.sleep(0.01)

        assert marker.read_text(encoding="utf-8") == "started"
        assert agent.is_run_alive(run_id) is True

        await client.call(
            "stop",
            {"run_id": run_id, "interrupt_timeout": 1, "terminate_timeout": 1},
        )
        result = await client.call("wait", {"run_id": run_id})

        assert result["intentional"] is True
        assert result["returncode"] == 0
        assert marker.read_text(encoding="utf-8") == "stopped"
        assert agent.is_run_alive(run_id) is False
    finally:
        if run_id is not None and agent.is_run_alive(run_id):
            await client.call("kill", {"run_id": run_id})
            await client.call("wait", {"run_id": run_id})
        await client.disconnect()


@pytest.mark.asyncio
async def test_local_agent_probes_attached_run_health_by_run_id(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = tmp_path / "child.py"
    child.write_text(
        "#!/usr/bin/env python3\nimport time\nwhile True:\n    time.sleep(0.05)\n",
        encoding="utf-8",
    )
    child.chmod(0o755)
    write_yaml(
        config_dir / "health.yaml",
        f"""
        name: health
        model: fake/model
        command:
          entrypoint: serve
          executable: {child}
        launch:
          runs_dir: {tmp_path / "runs"}
        server:
          port: 8129
        """,
    )
    seen: dict[str, object] = {}

    async def fake_probe_loop(cfg, *, emit, is_process_alive):
        seen["name"] = cfg.name
        seen["alive"] = is_process_alive()
        emit(HealthEvent(ready=True, detail="ready", models=["served"]))

    monkeypatch.setattr(local_agent_module, "probe_loop", fake_probe_loop)
    agent = LocalAgent()
    client = InProcessTargetClient(agent)
    await client.connect()
    run_id: str | None = None

    try:
        launch = await client.call(
            "launch", {"name": "health", "configs_dir": str(config_dir)}
        )
        run_id = str(launch["run_id"])
        result = await client.call("health", {"run_id": run_id})
        events = client.subscribe([run_id], resume_from="start")
        try:
            health_event = await _next_event(events, event_name="health")
        finally:
            await events.aclose()

        assert seen == {"name": "health", "alive": True}
        assert result["ready"] is True
        assert result["detail"] == "ready"
        assert result["models"] == ["served"]
        assert result["reachable_url"] == "http://127.0.0.1:8129"
        assert health_event["ready"] is True
        assert health_event["models"] == ["served"]
    finally:
        if run_id is not None and agent.is_run_alive(run_id):
            await client.call("kill", {"run_id": run_id})
            await client.call("wait", {"run_id": run_id})
        await client.disconnect()


@pytest.mark.asyncio
async def test_local_agent_probes_detached_run_health_by_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_path = tmp_path / "run-1.run.log"
    log_path.write_text("", encoding="utf-8")
    manifest_path = tmp_path / "run-1.manifest.json"
    manifest = Manifest.from_active_log(log_path)
    sidecar_path = tmp_path / "run-1.json"
    sidecar = Sidecar(
        run_id="run-1",
        config_name="detached",
        command_argv=["vllm", "serve", "fake/model"],
        command_hash="sha256:abc",
        pid=123,
        pgid=123,
        process_create_time=1.0,
        executable="/bin/vllm",
        cwd=str(tmp_path),
        launch_mode="detached",
        host="127.0.0.1",
        port=8123,
        served_model_names=["served"],
        exposure="local",
        manifest_path=str(manifest_path),
        config_snapshot={
            "name": "detached",
            "model": "fake/model",
            "server": {"host": "127.0.0.1", "port": 8123},
            "launch": {"mode": "detached", "health": {"interval_seconds": 0.05}},
        },
    )
    seen: dict[str, object] = {}

    async def fake_probe_loop(cfg, *, emit, is_process_alive):
        seen["name"] = cfg.name
        seen["port"] = cfg.server.port
        seen["alive"] = is_process_alive()
        emit(HealthEvent(ready=True, detail="ready", models=["served"]))

    monkeypatch.setattr(local_agent_module, "verify_sidecar_from_system", lambda path: True)
    monkeypatch.setattr(
        local_agent_module,
        "discover_active_sidecars",
        lambda runs_dirs: [sidecar_path],
    )
    monkeypatch.setattr(local_agent_module, "load_sidecar", lambda path: sidecar)
    monkeypatch.setattr(local_agent_module, "load_manifest", lambda path: manifest)
    monkeypatch.setattr(local_agent_module, "probe_loop", fake_probe_loop)
    agent = LocalAgent()
    client = InProcessTargetClient(agent)
    await client.connect()

    try:
        await client.call("discover_detached", {"runs_dirs": [str(tmp_path)]})
        await client.call("reattach_detached", {"run_id": "run-1"})
        result = await client.call("probe_until_ready", {"run_id": "run-1"})
        events = client.subscribe(["run-1"], resume_from="start")
        try:
            health_event = await _next_event(events, event_name="health")
        finally:
            await events.aclose()
    finally:
        await client.disconnect()

    assert seen == {"name": "detached", "port": 8123, "alive": True}
    assert result["ready"] is True
    assert result["detail"] == "ready"
    assert result["models"] == ["served"]
    assert health_event["ready"] is True
    assert health_event["models"] == ["served"]


@pytest.mark.asyncio
async def test_local_agent_emits_attached_log_and_phase_events(
    config_dir: Path, tmp_path: Path
) -> None:
    child = tmp_path / "child.py"
    child.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "print('INFO Starting to load model', flush=True)",
            ]
        ),
        encoding="utf-8",
    )
    child.chmod(0o755)
    write_yaml(
        config_dir / "events.yaml",
        f"""
        name: events
        model: fake/model
        command:
          entrypoint: serve
          executable: {child}
        launch:
          runs_dir: {tmp_path / "runs"}
        """,
    )
    assert not hasattr(local_agent_module, "start_attached")
    client = InProcessTargetClient(LocalAgent())
    await client.connect()
    try:
        launch = await client.call(
            "launch", {"name": "events", "configs_dir": str(config_dir)}
        )
        result = await client.call("wait", {"run_id": launch["run_id"]})
        events = client.subscribe([launch["run_id"]], resume_from="start")
        try:
            log_event = await _next_event(events, event_name="log")
            phase_event = await _next_event(events, event_name="phase")
        finally:
            await events.aclose()
    finally:
        await client.disconnect()

    assert result["intentional"] is False
    assert result["returncode"] == 0
    assert result["phase"] == Phase.ERROR.value
    assert result["error_kind"] == ErrorKind.CRASHED.value
    assert result["error_excerpt"] == "INFO Starting to load model"
    assert log_event["text"] == "INFO Starting to load model"
    assert phase_event["phase"] == Phase.LOADING_WEIGHTS.value


@pytest.mark.asyncio
async def test_local_agent_starts_detached_run_from_prepared_launch(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "child.py"
    executable.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    executable.chmod(0o755)
    write_yaml(
        config_dir / "detached.yaml",
        f"""
        name: detached
        model: fake/model
        command:
          entrypoint: serve
          executable: {executable}
        server:
          api_key: literal-api-key
        env:
          HF_TOKEN: hf_literal
        launch:
          mode: detached
          runs_dir: {tmp_path / "runs"}
        vllm:
          version_profile: current
        """,
    )
    sidecar_path = tmp_path / "runs" / "run-1.json"
    manifest_path = tmp_path / "runs" / "run-1.manifest.json"
    log_path = tmp_path / "runs" / "run-1.run.log"
    seen: dict[str, object] = {}

    def fake_start_detached(cfg, build, **kwargs):
        seen["cfg_name"] = cfg.name
        seen["argv"] = list(build.argv)
        seen["secrets"] = kwargs["secrets"]
        seen["vllm_version"] = kwargs["vllm_version"]
        seen["vllm_version_profile"] = kwargs["vllm_version_profile"]
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("", encoding="utf-8")
        Manifest.from_active_log(log_path).write_atomic(manifest_path)
        Sidecar(
            run_id="run-1",
            config_name=cfg.name,
            command_argv=list(build.argv),
            command_hash="sha256:abc",
            pid=123,
            pgid=123,
            process_create_time=1.0,
            executable=str(build.argv[0]),
            cwd=str(build.cwd),
            launch_mode=cfg.launch.mode.value,
            host=cfg.server.host,
            port=cfg.server.port,
            served_model_names=[cfg.served_model_name]
            if cfg.served_model_name
            else [],
            exposure=cfg.server.exposure.value,
            manifest_path=str(manifest_path),
            config_snapshot=cfg.model_dump(mode="json"),
            vllm_version_profile=kwargs["vllm_version_profile"],
        ).write_atomic(sidecar_path)
        return DetachedLaunch(
            run_id="run-1",
            supervisor_pid=123,
            sidecar_path=sidecar_path,
            manifest_path=manifest_path,
            log_path=log_path,
        )

    monkeypatch.setattr(local_agent_module, "start_detached", fake_start_detached)
    client = InProcessTargetClient(LocalAgent())
    await client.connect()

    try:
        launch = await client.call(
            "launch", {"name": "detached", "configs_dir": str(config_dir)}
        )
    finally:
        await client.disconnect()

    assert launch == {
        "run_id": "run-1",
        "launch_mode": "detached",
        "status": "started",
    }
    assert seen["cfg_name"] == "detached"
    assert seen["secrets"] == ["literal-api-key", "hf_literal"]
    assert seen["vllm_version"] is None
    assert seen["vllm_version_profile"] == "current"


@pytest.mark.asyncio
async def test_target_client_detached_launch_can_reattach_by_run_id(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "child.py"
    executable.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    executable.chmod(0o755)
    write_yaml(
        config_dir / "detached-wire.yaml",
        f"""
        name: detached-wire
        model: fake/model
        command:
          entrypoint: serve
          executable: {executable}
        launch:
          mode: detached
          runs_dir: {tmp_path / "runs"}
        """,
    )
    sidecar_path = tmp_path / "runs" / "run-1.json"
    log_path = tmp_path / "runs" / "run-1.run.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("", encoding="utf-8")
    manifest = Manifest.from_active_log(log_path)
    sidecar = Sidecar(
        run_id="run-1",
        config_name="detached-wire",
        command_argv=["vllm", "serve", "fake/model"],
        command_hash="sha256:abc",
        pid=123,
        pgid=123,
        process_create_time=1.0,
        executable="/bin/vllm",
        cwd=str(tmp_path),
        launch_mode="detached",
        host="127.0.0.1",
        port=8000,
        served_model_names=["served"],
        exposure="local",
        manifest_path=str(tmp_path / "runs" / "run-1.manifest.json"),
        config_snapshot={"name": "detached-wire", "model": "fake/model"},
    )

    monkeypatch.setattr(
        local_agent_module,
        "start_detached",
        lambda *_args, **_kwargs: DetachedLaunch(
            run_id="run-1",
            supervisor_pid=123,
            sidecar_path=sidecar_path,
            manifest_path=tmp_path / "runs" / "run-1.manifest.json",
            log_path=log_path,
        ),
    )
    monkeypatch.setattr(local_agent_module, "verify_sidecar_from_system", lambda path: True)
    monkeypatch.setattr(local_agent_module, "load_sidecar", lambda path: sidecar)
    monkeypatch.setattr(local_agent_module, "load_manifest", lambda path: manifest)
    client = InProcessTargetClient(LocalAgent())
    await client.connect()

    launch = await client.call(
        "launch", {"name": "detached-wire", "configs_dir": str(config_dir)}
    )
    reattached = await client.call("reattach_detached", {"run_id": "run-1"})

    assert launch == {
        "run_id": "run-1",
        "launch_mode": "detached",
        "status": "started",
    }
    assert reattached["run_id"] == "run-1"
    assert reattached["sidecar"]["config_name"] == "detached-wire"
    json.dumps(launch)
    json.dumps(reattached)


@pytest.mark.asyncio
async def test_target_client_detached_launch_is_idempotent_by_requested_run_id(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "child.py"
    executable.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    executable.chmod(0o755)
    write_yaml(
        config_dir / "detached-idempotent.yaml",
        f"""
        name: detached-idempotent
        model: fake/model
        command:
          entrypoint: serve
          executable: {executable}
        launch:
          mode: detached
          runs_dir: {tmp_path / "runs"}
        """,
    )
    starts: list[str | None] = []

    def fake_start_detached(cfg, build, *_, run_id=None, **_kwargs) -> DetachedLaunch:
        starts.append(run_id)
        actual_run_id = str(run_id or "generated")
        log_path = tmp_path / "runs" / f"{actual_run_id}.run.log"
        manifest_path = tmp_path / "runs" / f"{actual_run_id}.manifest.json"
        sidecar_path = tmp_path / "runs" / f"{actual_run_id}.json"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("", encoding="utf-8")
        Manifest.from_active_log(log_path).write_atomic(manifest_path)
        Sidecar(
            run_id=actual_run_id,
            config_name=cfg.name,
            command_argv=list(build.argv),
            command_hash="sha256:abc",
            pid=123,
            pgid=123,
            process_create_time=1.0,
            executable=str(build.argv[0]),
            cwd=str(build.cwd),
            launch_mode=cfg.launch.mode.value,
            host=cfg.server.host,
            port=cfg.server.port,
            served_model_names=[cfg.served_model_name]
            if cfg.served_model_name
            else [],
            exposure=cfg.server.exposure.value,
            manifest_path=str(manifest_path),
            config_snapshot=cfg.model_dump(mode="json"),
        ).write_atomic(sidecar_path)
        return DetachedLaunch(
            run_id=actual_run_id,
            supervisor_pid=123,
            sidecar_path=sidecar_path,
            manifest_path=manifest_path,
            log_path=log_path,
        )

    monkeypatch.setattr(local_agent_module, "start_detached", fake_start_detached)
    client = InProcessTargetClient(LocalAgent())
    await client.connect()

    first = await client.call(
        "launch",
        {
            "name": "detached-idempotent",
            "configs_dir": str(config_dir),
            "run_id": "detached-idem-1",
        },
    )
    second = await client.call(
        "launch",
        {
            "name": "detached-idempotent",
            "configs_dir": str(config_dir),
            "run_id": "detached-idem-1",
        },
    )

    assert first == {
        "run_id": "detached-idem-1",
        "launch_mode": "detached",
        "status": "started",
    }
    assert second == {
        "run_id": "detached-idem-1",
        "launch_mode": "detached",
        "status": "already-running",
    }
    assert starts == ["detached-idem-1"]


@pytest.mark.asyncio
async def test_local_agent_reattaches_and_stops_detached_run_by_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sidecar_path = tmp_path / "run-1.json"
    manifest_path = tmp_path / "run-1.manifest.json"
    log_path = tmp_path / "run-1.run.log"
    sidecar = Sidecar(
        run_id="run-1",
        config_name="detached",
        command_argv=["vllm", "serve", "fake/model"],
        command_hash="sha256:abc",
        pid=123,
        pgid=123,
        process_create_time=1.0,
        executable="/bin/vllm",
        cwd=str(tmp_path),
        launch_mode="detached",
        host="127.0.0.1",
        port=8000,
        served_model_names=["served"],
        exposure="local",
        manifest_path=str(manifest_path),
    )
    manifest = Manifest.from_active_log(log_path)
    stopped: list[tuple[Path, float, float]] = []

    monkeypatch.setattr(local_agent_module, "verify_sidecar_from_system", lambda path: True)
    monkeypatch.setattr(
        local_agent_module,
        "discover_active_sidecars",
        lambda runs_dirs: [sidecar_path],
    )
    monkeypatch.setattr(local_agent_module, "load_sidecar", lambda path: sidecar)
    monkeypatch.setattr(local_agent_module, "load_manifest", lambda path: manifest)
    monkeypatch.setattr(
        local_agent_module,
        "stop_sidecar_from_system",
        lambda path, *, interrupt_timeout, terminate_timeout: stopped.append(
            (path, interrupt_timeout, terminate_timeout)
        ),
    )
    agent = LocalAgent()
    client = InProcessTargetClient(agent)
    await client.connect()

    try:
        discovered = await client.call(
            "discover_detached", {"runs_dirs": [str(tmp_path)]}
        )
        reattached = await client.call("reattach_detached", {"run_id": "run-1"})
        await client.call(
            "stop",
            {"run_id": "run-1", "interrupt_timeout": 2, "terminate_timeout": 3},
        )
    finally:
        await client.disconnect()

    assert discovered == {"runs": [{"run_id": "run-1", "config_name": "detached"}]}
    assert reattached["run_id"] == "run-1"
    assert agent.is_run_alive("run-1") is True
    assert stopped == [(sidecar_path, 2, 3)]


@pytest.mark.asyncio
async def test_local_agent_discovers_detached_runs_from_agent_side_sidecars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    runs_dir = tmp_path / "runs"
    write_yaml(
        config_dir / "detached.yaml",
        f"""
        name: detached
        model: fake/model
        launch:
          runs_dir: {runs_dir}
        """,
    )
    sidecar_path = tmp_path / "run-1.json"
    sidecar = Sidecar(
        run_id="run-1",
        config_name="detached",
        command_argv=["vllm", "serve", "fake/model"],
        command_hash="sha256:abc",
        pid=123,
        pgid=123,
        process_create_time=1.0,
        executable="/bin/vllm",
        cwd=str(tmp_path),
        launch_mode="detached",
        host="127.0.0.1",
        port=8000,
        served_model_names=["served"],
        exposure="local",
        manifest_path=str(tmp_path / "run-1.manifest.json"),
    )
    seen: dict[str, object] = {}

    def fake_discover(runs_dirs):
        seen["runs_dirs"] = runs_dirs
        return [sidecar_path]

    monkeypatch.setattr(local_agent_module, "discover_active_sidecars", fake_discover)
    monkeypatch.setattr(local_agent_module, "load_sidecar", lambda path: sidecar)
    client = InProcessTargetClient(LocalAgent())
    await client.connect()

    try:
        await client.call("list_configs", {"configs_dir": str(config_dir)})
        discovered = await client.call("discover_runs")
        discovered_no_paths = await client.call("discover_runs_no_paths")
    finally:
        await client.disconnect()

    assert runs_dir in seen["runs_dirs"]
    assert discovered == {"runs": [{"run_id": "run-1", "config_name": "detached"}]}
    assert discovered_no_paths == discovered
    assert "sidecar_path" not in discovered_no_paths["runs"][0]
    json.dumps(discovered_no_paths)


@pytest.mark.asyncio
async def test_target_client_discovers_and_reattaches_detached_runs_by_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_path = tmp_path / "run-1.run.log"
    log_path.write_text("INFO Uvicorn running on http://127.0.0.1:8000\n", encoding="utf-8")
    manifest = Manifest.from_active_log(log_path)
    sidecar_path = tmp_path / "run-1.json"
    sidecar = Sidecar(
        run_id="run-1",
        config_name="detached",
        command_argv=["vllm", "serve", "fake/model"],
        command_hash="sha256:abc",
        pid=123,
        pgid=123,
        process_create_time=1.0,
        executable="/bin/vllm",
        cwd=str(tmp_path),
        launch_mode="detached",
        host="0.0.0.0",
        port=8000,
        served_model_names=["served"],
        exposure="lan",
        manifest_path=str(tmp_path / "run-1.manifest.json"),
        config_snapshot={
            "name": "detached",
            "model": "fake/model",
            "server": {"host": "0.0.0.0", "port": 8000, "exposure": "lan"},
            "launch": {"mode": "detached"},
        },
        vllm_version_profile="current",
    )

    monkeypatch.setattr(
        local_agent_module,
        "discover_active_sidecars",
        lambda runs_dirs: [sidecar_path],
    )
    monkeypatch.setattr(local_agent_module, "verify_sidecar_from_system", lambda path: True)
    monkeypatch.setattr(local_agent_module, "load_sidecar", lambda path: sidecar)
    monkeypatch.setattr(local_agent_module, "load_manifest", lambda path: manifest)
    client = InProcessTargetClient(LocalAgent())
    await client.connect()

    discovered = await client.call(
        "discover_detached", {"runs_dirs": [str(tmp_path / "runs")]}
    )
    status = await client.call("status", {"run_id": "run-1"})
    reattached = await client.call("reattach_detached", {"run_id": "run-1"})

    assert discovered == {
        "runs": [
            {
                "run_id": "run-1",
                "config_name": "detached",
            }
        ]
    }
    assert status == reattached
    assert "sidecar_path" not in status
    assert "manifest" not in status
    json.dumps(status)
    json.dumps(discovered)
    assert reattached["run_id"] == "run-1"
    assert reattached["config"]["name"] == "detached"
    assert reattached["sidecar"] == {
        "config_name": "detached",
        "host": "0.0.0.0",
        "port": 8000,
        "exposure": "lan",
        "served_model_names": ["served"],
        "launch_mode": "detached",
        "vllm_version_profile": "current",
        "reachable_url": "http://127.0.0.1:8000",
    }
    assert reattached["fsm"] == {"vllm_version_profile": "current"}
    json.dumps(reattached)


@pytest.mark.asyncio
async def test_local_agent_tails_detached_log_and_emits_phase_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_path = tmp_path / "run-1.run.log"
    log_path.write_text("INFO Starting to load model\n", encoding="utf-8")
    manifest_path = tmp_path / "run-1.manifest.json"
    manifest = Manifest.from_active_log(log_path)
    sidecar_path = tmp_path / "run-1.json"
    sidecar = Sidecar(
        run_id="run-1",
        config_name="detached",
        command_argv=["vllm", "serve", "fake/model"],
        command_hash="sha256:abc",
        pid=123,
        pgid=123,
        process_create_time=1.0,
        executable="/bin/vllm",
        cwd=str(tmp_path),
        launch_mode="detached",
        host="127.0.0.1",
        port=8000,
        served_model_names=["served"],
        exposure="local",
        manifest_path=str(manifest_path),
        config_snapshot={"name": "detached", "model": "fake/model"},
    )
    alive_checks = 0

    def fake_verify(_path: Path) -> bool:
        nonlocal alive_checks
        alive_checks += 1
        return alive_checks < 2

    monkeypatch.setattr(local_agent_module, "verify_sidecar_from_system", fake_verify)
    monkeypatch.setattr(
        local_agent_module,
        "discover_active_sidecars",
        lambda runs_dirs: [sidecar_path],
    )
    monkeypatch.setattr(local_agent_module, "load_sidecar", lambda path: sidecar)
    monkeypatch.setattr(local_agent_module, "load_manifest", lambda path: manifest)
    agent = LocalAgent()
    client = InProcessTargetClient(agent)
    await client.connect()

    try:
        await client.call("discover_detached", {"runs_dirs": [str(tmp_path)]})
        await client.call("reattach_detached", {"run_id": "run-1"})
        alive_checks = 0
        tail_result = await client.call(
            "tail_detached",
            {"run_id": "run-1", "start_position": 0, "poll_interval": 0},
        )
        events = client.subscribe(["run-1"], resume_from="start")
        try:
            log_event = await _next_event(events, event_name="log")
            phase_event = await _next_event(events, event_name="phase")
        finally:
            await events.aclose()
    finally:
        await client.disconnect()

    assert tail_result == {"run_id": "run-1", "status": "ended"}
    assert log_event["text"] == "INFO Starting to load model"
    assert phase_event["phase"] == Phase.LOADING_WEIGHTS.value


@pytest.mark.asyncio
async def test_target_client_tails_detached_run_with_serialized_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_path = tmp_path / "run-1.run.log"
    log_path.write_text("INFO Starting to load model\n", encoding="utf-8")
    manifest_path = tmp_path / "run-1.manifest.json"
    manifest = Manifest.from_active_log(log_path)
    sidecar_path = tmp_path / "run-1.json"
    sidecar = Sidecar(
        run_id="run-1",
        config_name="detached",
        command_argv=["vllm", "serve", "fake/model"],
        command_hash="sha256:abc",
        pid=123,
        pgid=123,
        process_create_time=1.0,
        executable="/bin/vllm",
        cwd=str(tmp_path),
        launch_mode="detached",
        host="127.0.0.1",
        port=8000,
        served_model_names=["served"],
        exposure="local",
        manifest_path=str(manifest_path),
        config_snapshot={"name": "detached", "model": "fake/model"},
    )
    alive_checks = 0

    def fake_verify(_path: Path) -> bool:
        nonlocal alive_checks
        alive_checks += 1
        return alive_checks < 2

    monkeypatch.setattr(local_agent_module, "verify_sidecar_from_system", fake_verify)
    monkeypatch.setattr(
        local_agent_module,
        "discover_active_sidecars",
        lambda runs_dirs: [sidecar_path],
    )
    monkeypatch.setattr(local_agent_module, "load_sidecar", lambda path: sidecar)
    monkeypatch.setattr(local_agent_module, "load_manifest", lambda path: manifest)
    agent = LocalAgent()
    client = InProcessTargetClient(agent)
    await client.connect()

    try:
        await client.call("discover_detached", {"runs_dirs": [str(tmp_path)]})
        await client.call("reattach_detached", {"run_id": "run-1"})
        alive_checks = 0
        tail_result = await client.call(
            "tail_detached",
            {"run_id": "run-1", "start_position": 0, "poll_interval": 0},
        )
        events = client.subscribe(["run-1"], resume_from="start")
        try:
            replayed = [
                await asyncio.wait_for(events.__anext__(), timeout=2) for _ in range(4)
            ]
        finally:
            await events.aclose()
    finally:
        await client.disconnect()

    assert tail_result == {"run_id": "run-1", "status": "ended"}
    log_event = next(event for event in replayed if event["event"] == "log")
    phase_event = next(event for event in replayed if event["event"] == "phase")
    exited_event = next(event for event in replayed if event["event"] == "exited")
    assert log_event["text"] == "INFO Starting to load model"
    assert phase_event["phase"] == Phase.LOADING_WEIGHTS.value
    assert exited_event["run_id"] == "run-1"
    json.dumps(log_event)
    json.dumps(exited_event)


def test_local_agent_samples_gpus_with_injected_sampler() -> None:
    calls = 0

    def sampler() -> GpuPollResult:
        nonlocal calls
        calls += 1
        return GpuPollResult(
            [
                GpuSample(
                    visible_index=0,
                    uuid="GPU-a",
                    name="A100",
                    memory_used_mb=1024,
                    memory_total_mb=81920,
                    utilization_percent=25,
                    temperature_c=42,
                    power_w=110,
                )
            ]
        )

    agent = LocalAgent(gpu_sampler=sampler)

    result = agent.sample_gpus()

    assert calls == 1
    assert result.samples[0].name == "A100"


@pytest.mark.asyncio
async def test_target_client_samples_gpus_with_spec_named_gpu_method() -> None:
    def sampler() -> GpuPollResult:
        return GpuPollResult(
            [
                GpuSample(
                    visible_index=0,
                    uuid="GPU-a",
                    name="A100",
                    memory_used_mb=1024,
                    memory_total_mb=81920,
                    utilization_percent=25,
                    temperature_c=42,
                    power_w=110,
                )
            ]
        )

    client = InProcessTargetClient(LocalAgent(gpu_sampler=sampler))
    await client.connect()
    try:
        result = await client.call("gpu")
    finally:
        await client.disconnect()

    assert result == {
        "samples": [
            {
                "visible_index": 0,
                "uuid": "GPU-a",
                "name": "A100",
                "memory_used_mb": 1024,
                "memory_total_mb": 81920,
                "utilization_percent": 25,
                "temperature_c": 42,
                "power_w": 110,
                "mig_instance_id": None,
            }
        ],
        "note": "",
        "unavailable": False,
    }
    json.dumps(result)


@pytest.mark.asyncio
async def test_agent_lists_builds_from_agent_owned_data_root(tmp_path: Path) -> None:
    builds_root = tmp_path / "data" / "vllm-loader" / "builds"
    build_dir = builds_root / "01BUILDREADY"
    build_dir.mkdir(parents=True)
    (builds_root / "active.json").write_text(
        json.dumps({"build_id": "01BUILDREADY"}), encoding="utf-8"
    )
    (build_dir / "build.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "build_id": "01BUILDREADY",
                "label": "nightly-cu130",
                "status": "ready",
                "install": {
                    "method": "nightly",
                    "installer": "uv",
                    "python_requested": "3.12",
                    "provenance": {"nightly_channel": "cu130"},
                    "exit_code": 0,
                },
                "resolved": {
                    "vllm": "0.17.0.dev",
                    "vllm_commit": "abc123",
                    "vllm_version_profile": "current",
                    "torch": "2.9.0+cu130",
                    "cuda": "13.0",
                    "python": "3.12.7",
                },
                "paths": {
                    "root": str(build_dir),
                    "venv": "venv",
                    "executable": "bin/vllm",
                    "python": "bin/python",
                    "activate": "activate",
                    "run_script": "run.sh",
                },
                "created_at": "2026-06-02T14:03:11Z",
                "last_used_at": "2026-06-02T18:20:05Z",
                "notes": "Blackwell test build",
            }
        ),
        encoding="utf-8",
    )
    broken_dir = builds_root / "BROKEN"
    broken_dir.mkdir()
    (broken_dir / "build.json").write_text("{", encoding="utf-8")

    client = InProcessTargetClient(LocalAgent(builds_root=builds_root))
    await client.connect()
    try:
        result = await client.call("list_builds")
    finally:
        await client.disconnect()

    assert result["default_build_id"] == "01BUILDREADY"
    assert result["builds"] == [
        {
            "build_id": "01BUILDREADY",
            "label": "nightly-cu130",
            "status": "ready",
            "default": True,
            "install": {
                "method": "nightly",
                "installer": "uv",
                "python_requested": "3.12",
                "provenance": {"nightly_channel": "cu130"},
                "exit_code": 0,
            },
            "resolved": {
                "vllm": "0.17.0.dev",
                "vllm_commit": "abc123",
                "vllm_version_profile": "current",
                "torch": "2.9.0+cu130",
                "cuda": "13.0",
                "python": "3.12.7",
            },
            "paths": {
                "root": str(build_dir),
                "venv": "venv",
                "executable": "bin/vllm",
                "python": "bin/python",
                "activate": "activate",
                "run_script": "run.sh",
            },
            "created_at": "2026-06-02T14:03:11Z",
            "last_used_at": "2026-06-02T18:20:05Z",
            "notes": "Blackwell test build",
        }
    ]
    assert result["skipped"] == [{"build_id": "BROKEN", "reason": "invalid-json"}]
    json.dumps(result)


@pytest.mark.asyncio
async def test_agent_selects_build_default_from_agent_owned_registry(
    tmp_path: Path,
) -> None:
    builds_root = tmp_path / "data" / "vllm-loader" / "builds"
    build_dir = builds_root / "01BUILDREADY"
    bin_dir = build_dir / "bin"
    bin_dir.mkdir(parents=True)
    vllm_bin = bin_dir / "vllm"
    python_bin = bin_dir / "python"
    vllm_bin.write_text("#!/bin/sh\necho 'vLLM 0.11.2'\n", encoding="utf-8")
    python_bin.write_text("#!/bin/sh\necho '0.11.2'\n", encoding="utf-8")
    vllm_bin.chmod(0o755)
    python_bin.chmod(0o755)
    (build_dir / "build.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "build_id": "01BUILDREADY",
                "label": "nightly-cu130",
                "status": "ready",
                "paths": {
                    "root": str(build_dir),
                    "venv": "venv",
                    "executable": "bin/vllm",
                    "python": "bin/python",
                },
            }
        ),
        encoding="utf-8",
    )

    client = InProcessTargetClient(LocalAgent(builds_root=builds_root))
    await client.connect()
    try:
        selected = await client.call("select_build", {"build": "nightly-cu130"})
        result = await client.call("list_builds")
    finally:
        await client.disconnect()

    active = json.loads((builds_root / "active.json").read_text(encoding="utf-8"))
    assert selected == {
        "build_id": "01BUILDREADY",
        "label": "nightly-cu130",
        "active": True,
    }
    assert active["schema_version"] == 1
    assert active["build_id"] == "01BUILDREADY"
    assert active["label"] == "nightly-cu130"
    assert isinstance(active["updated_at"], str)
    assert result["default_build_id"] == "01BUILDREADY"
    assert result["builds"][0]["default"] is True
    json.dumps(result)


@pytest.mark.asyncio
async def test_agent_adopts_and_inspects_external_build_venv(tmp_path: Path) -> None:
    builds_root = tmp_path / "data" / "vllm-loader" / "builds"
    venv_dir = tmp_path / "external" / "vllm-nightly"
    bin_dir = venv_dir / "bin"
    bin_dir.mkdir(parents=True)
    vllm_bin = bin_dir / "vllm"
    python_bin = bin_dir / "python"
    vllm_bin.write_text("#!/bin/sh\necho 'vLLM 0.17.0.dev'\n", encoding="utf-8")
    python_bin.write_text("#!/bin/sh\necho '0.17.0.dev'\n", encoding="utf-8")
    vllm_bin.chmod(0o755)
    python_bin.chmod(0o755)
    (bin_dir / "activate").write_text("# activate\n", encoding="utf-8")

    client = InProcessTargetClient(LocalAgent(builds_root=builds_root))
    await client.connect()
    try:
        adopted = await client.call(
            "adopt_build",
            {
                "build_id": "01ADOPTED",
                "label": "external-nightly",
                "venv_path": str(venv_dir),
                "vllm_version": "0.17.0.dev",
                "vllm_version_profile": "current",
            },
        )
        inspected = await client.call("inspect_build", {"build": "external-nightly"})
    finally:
        await client.disconnect()

    build_dir = builds_root / "01ADOPTED"
    manifest_path = build_dir / "build.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert adopted["build_id"] == "01ADOPTED"
    assert adopted["label"] == "external-nightly"
    assert adopted["status"] == "adopted"
    assert adopted["manifest"]["paths"] == {
        "root": str(build_dir),
        "venv": "venv",
        "executable": "bin/vllm",
        "python": "bin/python",
        "activate": "activate",
        "run_script": "run.sh",
    }
    assert (build_dir / "venv").resolve() == venv_dir.resolve()
    assert (build_dir / "bin" / "vllm").resolve() == (venv_dir / "bin" / "vllm")
    assert (build_dir / "bin" / "python").resolve() == (
        venv_dir / "bin" / "python"
    )
    assert (build_dir / "activate").resolve() == (venv_dir / "bin" / "activate")
    run_script = build_dir / "run.sh"
    assert run_script.stat().st_mode & 0o111
    assert 'exec "${BUILD_ROOT}/bin/vllm" "$@"' in run_script.read_text(
        encoding="utf-8"
    )
    assert manifest["status"] == "adopted"
    assert manifest["install"]["method"] == "adopt"
    assert manifest["integrity"]["executable_sha256"] == _sha256_uri(
        vllm_bin.read_bytes()
    )
    assert manifest["integrity"]["freeze_sha256"] == _sha256_uri(b"0.17.0.dev")
    assert inspected["manifest"]["build_id"] == "01ADOPTED"
    assert inspected["manifest"]["resolved"]["vllm"] == "0.17.0.dev"
    assert inspected["manifest"]["verify"]["ok"] is True
    json.dumps(adopted)
    json.dumps(inspected)


@pytest.mark.asyncio
async def test_agent_rejects_invalid_external_build_adoption(tmp_path: Path) -> None:
    builds_root = tmp_path / "data" / "vllm-loader" / "builds"
    venv_dir = tmp_path / "external" / "broken"
    (venv_dir / "bin").mkdir(parents=True)
    (venv_dir / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")

    client = InProcessTargetClient(LocalAgent(builds_root=builds_root))
    await client.connect()
    try:
        with pytest.raises(TargetCallError) as exc_info:
            await client.call(
                "adopt_build",
                {
                    "build_id": "01BROKEN",
                    "label": "broken",
                    "venv_path": str(venv_dir),
                },
            )
    finally:
        await client.disconnect()

    assert exc_info.value.code == "invalid-config"
    assert exc_info.value.details["reason"] == "missing-executable"
    assert not (builds_root / "01BROKEN" / "build.json").exists()


@pytest.mark.asyncio
async def test_agent_rejects_external_build_adoption_when_vllm_probe_fails(
    tmp_path: Path,
) -> None:
    builds_root = tmp_path / "data" / "vllm-loader" / "builds"
    venv_dir = tmp_path / "external" / "probe-broken"
    bin_dir = venv_dir / "bin"
    bin_dir.mkdir(parents=True)
    vllm_bin = bin_dir / "vllm"
    python_bin = bin_dir / "python"
    vllm_bin.write_text("#!/bin/sh\nexit 42\n", encoding="utf-8")
    python_bin.write_text("#!/bin/sh\necho '0.11.2'\n", encoding="utf-8")
    (bin_dir / "activate").write_text("# activate\n", encoding="utf-8")
    vllm_bin.chmod(0o755)
    python_bin.chmod(0o755)

    client = InProcessTargetClient(LocalAgent(builds_root=builds_root))
    await client.connect()
    try:
        with pytest.raises(TargetCallError) as exc_info:
            await client.call(
                "adopt_build",
                {
                    "build_id": "01PROBEBROKEN",
                    "label": "probe-broken",
                    "venv_path": str(venv_dir),
                },
            )
    finally:
        await client.disconnect()

    assert exc_info.value.code == "invalid-config"
    assert exc_info.value.details["reason"] == "vllm-version-probe-failed"
    assert not (builds_root / "01PROBEBROKEN" / "build.json").exists()


@pytest.mark.asyncio
async def test_agent_verifies_ready_build_from_agent_owned_registry(
    tmp_path: Path,
) -> None:
    builds_root = tmp_path / "data" / "vllm-loader" / "builds"
    build_dir = builds_root / "01BUILDREADY"
    bin_dir = build_dir / "bin"
    bin_dir.mkdir(parents=True)
    vllm_bin = bin_dir / "vllm"
    python_bin = bin_dir / "python"
    vllm_bin.write_text("#!/bin/sh\necho 'vLLM 0.11.2'\n", encoding="utf-8")
    python_bin.write_text("#!/bin/sh\necho '0.11.2'\n", encoding="utf-8")
    vllm_bin.chmod(0o755)
    python_bin.chmod(0o755)
    (build_dir / "build.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "build_id": "01BUILDREADY",
                "label": "nightly-cu130",
                "status": "ready",
                "paths": {
                    "root": str(build_dir),
                    "venv": "venv",
                    "executable": "bin/vllm",
                    "python": "bin/python",
                },
            }
        ),
        encoding="utf-8",
    )

    client = InProcessTargetClient(LocalAgent(builds_root=builds_root))
    await client.connect()
    try:
        verified = await client.call("verify_build", {"build": "nightly-cu130"})
    finally:
        await client.disconnect()

    assert verified["build_id"] == "01BUILDREADY"
    assert verified["ok"] is True
    assert verified["status"] == "ready"
    assert verified["detail"] == "build verified"
    assert verified["manifest"]["status"] == "ready"
    assert verified["manifest"]["verify"]["verify_output"]["python_import"] == "0.11.2"
    assert verified["manifest"]["verify"]["verify_output"]["vllm_version"] == "vLLM 0.11.2"
    integrity = verified["manifest"]["integrity"]
    assert integrity["strategy"] == "pip_freeze_sha256"
    assert integrity["executable_sha256"] == _sha256_uri(vllm_bin.read_bytes())
    assert integrity["freeze_sha256"] == _sha256_uri(b"0.11.2")
    assert integrity["verify_command"] == ["bin/vllm", "--version"]
    assert integrity["verify_output"] == "vLLM 0.11.2"
    json.dumps(verified)


@pytest.mark.asyncio
async def test_agent_verify_marks_build_broken_when_executable_missing(
    tmp_path: Path,
) -> None:
    builds_root = tmp_path / "data" / "vllm-loader" / "builds"
    build_dir = builds_root / "01BROKENBUILD"
    bin_dir = build_dir / "bin"
    bin_dir.mkdir(parents=True)
    python_bin = bin_dir / "python"
    python_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    (build_dir / "build.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "build_id": "01BROKENBUILD",
                "label": "broken-build",
                "status": "ready",
                "paths": {
                    "root": str(build_dir),
                    "venv": "venv",
                    "executable": "bin/vllm",
                    "python": "bin/python",
                },
            }
        ),
        encoding="utf-8",
    )

    client = InProcessTargetClient(LocalAgent(builds_root=builds_root))
    await client.connect()
    try:
        verified = await client.call("verify_build", {"build": "broken-build"})
        listed = await client.call("list_builds")
    finally:
        await client.disconnect()

    manifest = json.loads((build_dir / "build.json").read_text(encoding="utf-8"))
    assert verified["build_id"] == "01BROKENBUILD"
    assert verified["ok"] is False
    assert verified["status"] == "broken"
    assert verified["reason"] == "missing-executable"
    assert verified["manifest"]["status"] == "broken"
    assert manifest["status"] == "broken"
    assert manifest["verify"]["reason"] == "missing-executable"
    assert listed["builds"][0]["status"] == "broken"
    json.dumps(verified)


@pytest.mark.asyncio
async def test_agent_verify_marks_build_broken_when_vllm_probe_fails(
    tmp_path: Path,
) -> None:
    builds_root = tmp_path / "data" / "vllm-loader" / "builds"
    build_dir = builds_root / "01PROBEFAIL"
    bin_dir = build_dir / "bin"
    bin_dir.mkdir(parents=True)
    vllm_bin = bin_dir / "vllm"
    python_bin = bin_dir / "python"
    vllm_bin.write_text("#!/bin/sh\nexit 42\n", encoding="utf-8")
    python_bin.write_text("#!/bin/sh\necho 0.11.2\n", encoding="utf-8")
    vllm_bin.chmod(0o755)
    python_bin.chmod(0o755)
    (build_dir / "build.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "build_id": "01PROBEFAIL",
                "label": "probe-fail",
                "status": "ready",
                "paths": {
                    "root": str(build_dir),
                    "venv": "venv",
                    "executable": "bin/vllm",
                    "python": "bin/python",
                },
            }
        ),
        encoding="utf-8",
    )

    client = InProcessTargetClient(LocalAgent(builds_root=builds_root))
    await client.connect()
    try:
        verified = await client.call("verify_build", {"build": "probe-fail"})
    finally:
        await client.disconnect()

    manifest = json.loads((build_dir / "build.json").read_text(encoding="utf-8"))
    assert verified["ok"] is False
    assert verified["status"] == "broken"
    assert verified["reason"] == "vllm-version-probe-failed"
    assert manifest["status"] == "broken"
    assert manifest["verify"]["reason"] == "vllm-version-probe-failed"
    assert manifest["verify"]["verify_output"]["vllm_returncode"] == 42
    json.dumps(verified)


@pytest.mark.asyncio
async def test_agent_verify_marks_build_broken_when_executable_hash_drifts(
    tmp_path: Path,
) -> None:
    builds_root = tmp_path / "data" / "vllm-loader" / "builds"
    build_dir = builds_root / "01HASHDRIFT"
    bin_dir = build_dir / "bin"
    bin_dir.mkdir(parents=True)
    vllm_bin = bin_dir / "vllm"
    python_bin = bin_dir / "python"
    original_vllm = b"#!/bin/sh\necho 'vLLM 0.11.2'\n"
    vllm_bin.write_bytes(original_vllm)
    python_bin.write_text("#!/bin/sh\necho '0.11.2'\n", encoding="utf-8")
    vllm_bin.chmod(0o755)
    python_bin.chmod(0o755)
    (build_dir / "build.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "build_id": "01HASHDRIFT",
                "label": "hash-drift",
                "status": "ready",
                "integrity": {
                    "strategy": "pip_freeze_sha256",
                    "executable_sha256": _sha256_uri(original_vllm),
                    "freeze_sha256": _sha256_uri(b"0.11.2"),
                    "verify_command": ["bin/vllm", "--version"],
                    "verify_output": "vLLM 0.11.2",
                },
                "paths": {
                    "root": str(build_dir),
                    "venv": "venv",
                    "executable": "bin/vllm",
                    "python": "bin/python",
                },
            }
        ),
        encoding="utf-8",
    )
    vllm_bin.write_text("#!/bin/sh\necho 'vLLM 0.11.3'\n", encoding="utf-8")

    client = InProcessTargetClient(LocalAgent(builds_root=builds_root))
    await client.connect()
    try:
        verified = await client.call("verify_build", {"build": "hash-drift"})
    finally:
        await client.disconnect()

    manifest = json.loads((build_dir / "build.json").read_text(encoding="utf-8"))
    assert verified["ok"] is False
    assert verified["status"] == "broken"
    assert verified["reason"] == "executable-integrity-mismatch"
    assert manifest["status"] == "broken"
    assert manifest["verify"]["reason"] == "executable-integrity-mismatch"
    assert manifest["integrity"]["executable_sha256"] == _sha256_uri(original_vllm)
    assert manifest["verify"]["integrity"]["current_executable_sha256"] == _sha256_uri(
        vllm_bin.read_bytes()
    )
    json.dumps(verified)


def _sha256_uri(data: bytes) -> str:
    return f"sha256:{sha256(data).hexdigest()}"


@pytest.mark.asyncio
async def test_agent_removes_unpinned_build_from_agent_owned_registry(
    config_dir: Path, tmp_path: Path
) -> None:
    builds_root = tmp_path / "data" / "vllm-loader" / "builds"
    build_dir = builds_root / "01REMOVEME"
    bin_dir = build_dir / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "vllm").write_text("#!/bin/sh\n", encoding="utf-8")
    (bin_dir / "python").write_text("#!/bin/sh\n", encoding="utf-8")
    (build_dir / "build.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "build_id": "01REMOVEME",
                "label": "old-build",
                "status": "broken",
                "paths": {
                    "root": str(build_dir),
                    "venv": "venv",
                    "executable": "bin/vllm",
                    "python": "bin/python",
                },
            }
        ),
        encoding="utf-8",
    )
    write_yaml(config_dir / "other.yaml", "name: other\nmodel: org/other")

    client = InProcessTargetClient(LocalAgent(builds_root=builds_root))
    await client.connect()
    try:
        removed = await client.call(
            "remove_build",
            {"build": "old-build", "configs_dir": str(config_dir)},
        )
        listed = await client.call("list_builds")
    finally:
        await client.disconnect()

    assert removed == {
        "build_id": "01REMOVEME",
        "label": "old-build",
        "removed": True,
        "removed_path": str(build_dir),
    }
    assert not build_dir.exists()
    assert listed["builds"] == []
    json.dumps(removed)


@pytest.mark.asyncio
async def test_agent_refuses_to_remove_build_pinned_by_config(
    config_dir: Path, tmp_path: Path
) -> None:
    builds_root = tmp_path / "data" / "vllm-loader" / "builds"
    build_dir = builds_root / "01PINNEDBUILD"
    bin_dir = build_dir / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "vllm").write_text("#!/bin/sh\n", encoding="utf-8")
    (bin_dir / "python").write_text("#!/bin/sh\n", encoding="utf-8")
    (build_dir / "build.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "build_id": "01PINNEDBUILD",
                "label": "pinned-build",
                "status": "ready",
                "paths": {
                    "root": str(build_dir),
                    "venv": "venv",
                    "executable": "bin/vllm",
                    "python": "bin/python",
                },
            }
        ),
        encoding="utf-8",
    )
    write_yaml(
        config_dir / "uses-build.yaml",
        """
        name: uses-build
        model: org/model
        command:
          build: pinned-build
        """,
    )

    client = InProcessTargetClient(LocalAgent(builds_root=builds_root))
    await client.connect()
    try:
        with pytest.raises(TargetCallError) as exc_info:
            await client.call(
                "remove_build",
                {"build": "pinned-build", "configs_dir": str(config_dir)},
            )
    finally:
        await client.disconnect()

    assert exc_info.value.code == "resource-in-use"
    assert exc_info.value.details["reason"] == "config-pin"
    assert exc_info.value.details["configs"] == ["uses-build"]
    assert build_dir.exists()


@pytest.mark.asyncio
async def test_agent_refuses_to_remove_build_used_by_live_run(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builds_root = tmp_path / "data" / "vllm-loader" / "builds"
    build_dir = builds_root / "01LIVEBUILD"
    bin_dir = build_dir / "bin"
    bin_dir.mkdir(parents=True)
    vllm_bin = bin_dir / "vllm"
    vllm_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    (bin_dir / "python").write_text("#!/bin/sh\n", encoding="utf-8")
    (build_dir / "build.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "build_id": "01LIVEBUILD",
                "label": "live-build",
                "status": "ready",
                "paths": {
                    "root": str(build_dir),
                    "venv": "venv",
                    "executable": "bin/vllm",
                    "python": "bin/python",
                },
            }
        ),
        encoding="utf-8",
    )
    write_yaml(config_dir / "other.yaml", "name: other\nmodel: org/other")
    sidecar_path = tmp_path / "runs" / "live-build.json"
    live_sidecar = Sidecar(
        run_id="run-live-build",
        config_name="live-config",
        command_argv=[str(vllm_bin), "serve", "org/model"],
        command_hash="sha256:test",
        pid=123,
        pgid=123,
        process_create_time=1.0,
        executable=str(vllm_bin),
        cwd=str(tmp_path),
        launch_mode="detached",
        host="127.0.0.1",
        port=8000,
        served_model_names=["org/model"],
        exposure="local",
        manifest_path=str(tmp_path / "runs" / "live-build.manifest.json"),
    )
    monkeypatch.setattr(
        local_agent_module,
        "discover_active_sidecars",
        lambda runs_dirs: [sidecar_path],
    )
    monkeypatch.setattr(local_agent_module, "verify_sidecar_from_system", lambda path: True)
    monkeypatch.setattr(local_agent_module, "load_sidecar", lambda path: live_sidecar)

    client = InProcessTargetClient(LocalAgent(builds_root=builds_root))
    await client.connect()
    try:
        with pytest.raises(TargetCallError) as exc_info:
            await client.call(
                "remove_build",
                {"build": "live-build", "configs_dir": str(config_dir)},
            )
    finally:
        await client.disconnect()

    assert exc_info.value.code == "resource-in-use"
    assert exc_info.value.details["reason"] == "live-run"
    assert exc_info.value.details["build"] == "live-build"
    assert exc_info.value.details["run_id"] == "run-live-build"
    assert exc_info.value.details["config_name"] == "live-config"
    assert build_dir.exists()


@pytest.mark.asyncio
async def test_agent_ignores_unverified_sidecar_when_removing_build(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builds_root = tmp_path / "data" / "vllm-loader" / "builds"
    build_dir = builds_root / "01DEADSIDECAR"
    bin_dir = build_dir / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "vllm").write_text("#!/bin/sh\n", encoding="utf-8")
    (bin_dir / "python").write_text("#!/bin/sh\n", encoding="utf-8")
    (build_dir / "build.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "build_id": "01DEADSIDECAR",
                "label": "dead-sidecar-build",
                "status": "ready",
                "paths": {
                    "root": str(build_dir),
                    "venv": "venv",
                    "executable": "bin/vllm",
                    "python": "bin/python",
                },
            }
        ),
        encoding="utf-8",
    )
    write_yaml(config_dir / "other.yaml", "name: other\nmodel: org/other")
    sidecar_path = tmp_path / "runs" / "dead-sidecar.json"
    monkeypatch.setattr(
        local_agent_module,
        "discover_active_sidecars",
        lambda runs_dirs: [sidecar_path],
    )
    monkeypatch.setattr(local_agent_module, "verify_sidecar_from_system", lambda path: False)
    monkeypatch.setattr(
        local_agent_module,
        "load_sidecar",
        lambda path: (_ for _ in ()).throw(AssertionError("unverified sidecar loaded")),
    )

    client = InProcessTargetClient(LocalAgent(builds_root=builds_root))
    await client.connect()
    try:
        removed = await client.call(
            "remove_build",
            {"build": "dead-sidecar-build", "configs_dir": str(config_dir)},
        )
    finally:
        await client.disconnect()

    assert removed["build_id"] == "01DEADSIDECAR"
    assert removed["removed"] is True
    assert not build_dir.exists()


@pytest.mark.asyncio
async def test_agent_refuses_to_remove_active_default_build(tmp_path: Path) -> None:
    builds_root = tmp_path / "data" / "vllm-loader" / "builds"
    build_dir = builds_root / "01ACTIVEBUILD"
    bin_dir = build_dir / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "vllm").write_text("#!/bin/sh\n", encoding="utf-8")
    (bin_dir / "python").write_text("#!/bin/sh\n", encoding="utf-8")
    (build_dir / "build.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "build_id": "01ACTIVEBUILD",
                "label": "active-build",
                "status": "ready",
                "paths": {
                    "root": str(build_dir),
                    "venv": "venv",
                    "executable": "bin/vllm",
                    "python": "bin/python",
                },
            }
        ),
        encoding="utf-8",
    )

    client = InProcessTargetClient(LocalAgent(builds_root=builds_root))
    await client.connect()
    try:
        await client.call("select_build", {"build": "active-build"})
        with pytest.raises(TargetCallError) as exc_info:
            await client.call("remove_build", {"build": "active-build"})
    finally:
        await client.disconnect()

    assert exc_info.value.code == "resource-in-use"
    assert exc_info.value.details["reason"] == "active-build"
    assert build_dir.exists()


@pytest.mark.asyncio
async def test_agent_prepare_launch_resolves_pinned_build_handoff(
    config_dir: Path, tmp_path: Path
) -> None:
    builds_root = tmp_path / "data" / "vllm-loader" / "builds"
    build_dir = builds_root / "01BUILDREADY"
    bin_dir = build_dir / "bin"
    bin_dir.mkdir(parents=True)
    vllm_bin = bin_dir / "vllm"
    python_bin = bin_dir / "python"
    vllm_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    python_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    vllm_bin.chmod(0o755)
    python_bin.chmod(0o755)
    (build_dir / "build.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "build_id": "01BUILDREADY",
                "label": "nightly-cu130",
                "status": "ready",
                "resolved": {
                    "vllm": "0.17.0.dev",
                    "vllm_version_profile": "current",
                },
                "paths": {
                    "root": str(build_dir),
                    "venv": "venv",
                    "executable": "bin/vllm",
                    "python": "bin/python",
                    "activate": "activate",
                    "run_script": "run.sh",
                },
            }
        ),
        encoding="utf-8",
    )
    write_yaml(
        config_dir / "built.yaml",
        """
        name: built
        model: org/model
        command:
          build: nightly-cu130
        """,
    )

    client = InProcessTargetClient(LocalAgent(builds_root=builds_root))
    await client.connect()
    try:
        prepared = await client.call(
            "prepare_launch", {"name": "built", "configs_dir": str(config_dir)}
        )
    finally:
        await client.disconnect()

    build = prepared["build"]
    assert build["argv"][:3] == [str(vllm_bin), "serve", "org/model"]
    assert build["metadata"]["build_id"] == "01BUILDREADY"
    assert build["metadata"]["build_label"] == "nightly-cu130"
    assert build["metadata"]["vllm_version"] == "0.17.0.dev"
    assert build["metadata"]["vllm_version_profile"] == "current"
    assert build["metadata"]["env_overlay"] == {
        "VIRTUAL_ENV": str(build_dir / "venv"),
        "PATH_PREPEND": str(build_dir / "venv" / "bin"),
    }
    assert prepared["config"]["command"]["build"] == "nightly-cu130"
    assert prepared["config"]["command"]["executable"] is None
    json.dumps(prepared)


@pytest.mark.asyncio
async def test_agent_prepare_launch_resolves_build_python_for_module_entrypoint(
    config_dir: Path, tmp_path: Path
) -> None:
    builds_root = tmp_path / "data" / "vllm-loader" / "builds"
    build_dir = builds_root / "01MODULEBUILD"
    bin_dir = build_dir / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "vllm").write_text("#!/bin/sh\n", encoding="utf-8")
    python_bin = bin_dir / "python"
    python_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    (bin_dir / "vllm").chmod(0o755)
    python_bin.chmod(0o755)
    (build_dir / "build.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "build_id": "01MODULEBUILD",
                "label": "module-build",
                "status": "adopted",
                "resolved": {"vllm_version_profile": "current"},
                "paths": {
                    "root": str(build_dir),
                    "venv": "venv",
                    "executable": "bin/vllm",
                    "python": "bin/python",
                },
            }
        ),
        encoding="utf-8",
    )
    write_yaml(
        config_dir / "module.yaml",
        """
        name: module
        model: org/model
        command:
          entrypoint: module
          build: 01MODULEBUILD
        """,
    )

    client = InProcessTargetClient(LocalAgent(builds_root=builds_root))
    await client.connect()
    try:
        prepared = await client.call(
            "prepare_launch", {"name": "module", "configs_dir": str(config_dir)}
        )
    finally:
        await client.disconnect()

    assert prepared["build"]["argv"][:5] == [
        str(python_bin),
        "-m",
        "vllm.entrypoints.openai.api_server",
        "--model",
        "org/model",
    ]


@pytest.mark.asyncio
async def test_agent_lists_models_from_agent_owned_registry(tmp_path: Path) -> None:
    registry_path = tmp_path / "state" / "vllm-loader" / "models" / "registry.json"
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "default_cache": "hf",
                "app_download_dir": None,
                "entries": [
                    {
                        "entry_id": "01MODEL",
                        "display_name": "llama-3.1-8b",
                        "source": "hf_repo",
                        "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
                        "revision": "main",
                        "commit_sha": "abc123",
                        "local_path": None,
                        "url": None,
                        "quant_format": "none",
                        "tokenizer": None,
                        "files": {
                            "count": 7,
                            "total_bytes": 16060530000,
                            "weights_format": "safetensors",
                        },
                        "size_bytes": 16060530000,
                        "cache_state": "cached",
                        "gated": True,
                        "token_required": True,
                        "created_at": "2026-06-02T14:03:11Z",
                        "last_used_at": "2026-06-02T18:20:05Z",
                        "notes": "pinned for repro",
                    },
                    {"display_name": "missing identity"},
                    {
                        "entry_id": "01BROKEN",
                        "display_name": "broken metadata",
                        "size_bytes": "not-a-number",
                    },
                    "not-a-record",
                ],
            }
        ),
        encoding="utf-8",
    )

    client = InProcessTargetClient(LocalAgent(models_registry_path=registry_path))
    await client.connect()
    try:
        result = await client.call("list_models")
    finally:
        await client.disconnect()

    assert result["default_cache"] == "hf"
    assert result["app_download_dir"] is None
    assert result["models"] == [
        {
            "entry_id": "01MODEL",
            "display_name": "llama-3.1-8b",
            "source": "hf_repo",
            "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
            "revision": "main",
            "commit_sha": "abc123",
            "local_path": None,
            "url": None,
            "quant_format": "none",
            "tokenizer": None,
            "files": {
                "count": 7,
                "total_bytes": 16060530000,
                "weights_format": "safetensors",
            },
            "size_bytes": 16060530000,
            "cache_state": "cached",
            "gated": True,
            "token_required": True,
            "created_at": "2026-06-02T14:03:11Z",
            "last_used_at": "2026-06-02T18:20:05Z",
            "notes": "pinned for repro",
        }
    ]
    assert result["skipped"] == [
        {"entry_id": "", "reason": "missing-entry-id"},
        {"entry_id": "01BROKEN", "reason": "invalid-entry"},
        {"entry_id": "", "reason": "invalid-entry"},
    ]
    json.dumps(result)


@pytest.mark.asyncio
async def test_agent_list_models_merges_hf_cache_scan_with_pinned_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_path = tmp_path / "state" / "vllm-loader" / "models" / "registry.json"
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "default_cache": "hf",
                "app_download_dir": None,
                "entries": [
                    {
                        "entry_id": "01PINNED",
                        "display_name": "llama-pin",
                        "source": "hf_repo",
                        "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
                        "revision": "main",
                        "commit_sha": "abc123",
                        "local_path": None,
                        "url": None,
                        "quant_format": "awq",
                        "tokenizer": None,
                        "files": {},
                        "size_bytes": 0,
                        "cache_state": "remote_only",
                        "gated": False,
                        "token_required": False,
                        "created_at": "2026-06-02T14:03:11Z",
                        "last_used_at": None,
                        "notes": "pinned for repro",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    fake_files = (
        SimpleNamespace(file_name="config.json", size_on_disk=10),
        SimpleNamespace(file_name="model.safetensors", size_on_disk=100),
        SimpleNamespace(file_name="tokenizer.json", size_on_disk=20),
    )
    fake_revision = SimpleNamespace(
        commit_hash="abc123",
        size_on_disk=130,
        files=fake_files,
        refs=("main",),
    )
    fake_repo = SimpleNamespace(
        repo_id="meta-llama/Llama-3.1-8B-Instruct",
        repo_type="model",
        revisions=(fake_revision,),
    )
    monkeypatch.setattr(
        model_registry_module,
        "_scan_hf_cache_info",
        lambda: SimpleNamespace(repos=(fake_repo,)),
        raising=False,
    )

    client = InProcessTargetClient(LocalAgent(models_registry_path=registry_path))
    await client.connect()
    try:
        result = await client.call("list_models")
    finally:
        await client.disconnect()

    assert result["models"] == [
        {
            "entry_id": "01PINNED",
            "display_name": "llama-pin",
            "source": "hf_repo",
            "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
            "revision": "main",
            "commit_sha": "abc123",
            "local_path": None,
            "url": None,
            "quant_format": "awq",
            "tokenizer": None,
            "files": {
                "count": 3,
                "total_bytes": 130,
                "weights_format": "safetensors",
            },
            "size_bytes": 130,
            "cache_state": "cached",
            "gated": False,
            "token_required": False,
            "created_at": "2026-06-02T14:03:11Z",
            "last_used_at": None,
            "notes": "pinned for repro",
        }
    ]
    assert result["skipped"] == []
    json.dumps(result)


@pytest.mark.asyncio
async def test_agent_pins_model_to_agent_owned_registry(tmp_path: Path) -> None:
    registry_path = tmp_path / "state" / "vllm-loader" / "models" / "registry.json"

    client = InProcessTargetClient(LocalAgent(models_registry_path=registry_path))
    await client.connect()
    try:
        pinned = await client.call(
            "pin_model",
            {
                "entry_id": "01PINNED",
                "display_name": "llama-pinned",
                "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
                "revision": "main",
                "commit_sha": "abc123",
                "tokenizer": "meta-llama/Llama-3.1-8B-Instruct",
                "gated": True,
                "token_required": True,
                "notes": "metadata-only pin",
            },
        )
        listed = await client.call("list_models")
    finally:
        await client.disconnect()

    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    assert pinned["entry"]["entry_id"] == "01PINNED"
    assert pinned["entry"]["source"] == "hf_repo"
    assert pinned["entry"]["cache_state"] == "remote_only"
    assert registry["schema_version"] == 1
    assert registry["entries"][0]["entry_id"] == "01PINNED"
    assert registry["entries"][0]["token_required"] is True
    assert "HF_TOKEN" not in json.dumps(registry)
    assert listed["models"][0]["entry_id"] == "01PINNED"
    assert listed["models"][0]["commit_sha"] == "abc123"
    assert listed["models"][0]["cache_state"] == "remote_only"
    json.dumps(pinned)
    json.dumps(listed)


@pytest.mark.asyncio
async def test_agent_adopts_local_model_path_for_launch_handoff(
    config_dir: Path, tmp_path: Path
) -> None:
    registry_path = tmp_path / "state" / "vllm-loader" / "models" / "registry.json"
    model_dir = tmp_path / "models" / "local-llama"
    model_dir.mkdir(parents=True)
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    (model_dir / "model.safetensors").write_text("weights", encoding="utf-8")
    (model_dir / "tokenizer.json").write_text("{}", encoding="utf-8")
    write_yaml(
        config_dir / "local-model.yaml",
        """
        name: local-model
        model: local-llama
        model_ref: 01LOCAL
        """,
    )

    client = InProcessTargetClient(LocalAgent(models_registry_path=registry_path))
    await client.connect()
    try:
        adopted = await client.call(
            "pin_model",
            {
                "entry_id": "01LOCAL",
                "display_name": "local-llama",
                "source": "local_path",
                "local_path": str(model_dir),
            },
        )
        prepared = await client.call(
            "prepare_launch",
            {"name": "local-model", "configs_dir": str(config_dir)},
        )
    finally:
        await client.disconnect()

    assert adopted["entry"]["entry_id"] == "01LOCAL"
    assert adopted["entry"]["source"] == "local_path"
    assert adopted["entry"]["local_path"] == str(model_dir)
    assert adopted["entry"]["cache_state"] == "cached"
    assert adopted["entry"]["files"] == {
        "count": 3,
        "weights_format": "safetensors",
    }
    assert adopted["entry"]["integrity"]["strategy"] == "local_files_sha256"
    assert adopted["entry"]["integrity"]["files_sha256"].startswith("sha256:")
    assert adopted["entry"]["integrity"]["file_count"] == 3
    assert prepared["build"]["argv"][:3] == ["vllm", "serve", str(model_dir)]
    assert "--revision" not in prepared["build"]["argv"]
    assert prepared["build"]["metadata"]["model_source"] == "local_path"
    assert prepared["build"]["metadata"]["model_local_path"] == str(model_dir)
    json.dumps(adopted)
    json.dumps(prepared)


@pytest.mark.asyncio
async def test_agent_rejects_invalid_local_model_adoption(tmp_path: Path) -> None:
    registry_path = tmp_path / "state" / "vllm-loader" / "models" / "registry.json"
    model_dir = tmp_path / "models" / "broken"
    model_dir.mkdir(parents=True)
    (model_dir / "config.json").write_text("{}", encoding="utf-8")

    client = InProcessTargetClient(LocalAgent(models_registry_path=registry_path))
    await client.connect()
    try:
        with pytest.raises(TargetCallError) as exc_info:
            await client.call(
                "pin_model",
                {
                    "entry_id": "01BROKEN",
                    "source": "local_path",
                    "local_path": str(model_dir),
                },
            )
    finally:
        await client.disconnect()

    assert exc_info.value.code == "invalid-config"
    assert exc_info.value.details["reason"] == "missing-weights"
    assert not registry_path.exists()


@pytest.mark.asyncio
async def test_agent_verifies_adopted_local_model(tmp_path: Path) -> None:
    registry_path = tmp_path / "state" / "vllm-loader" / "models" / "registry.json"
    model_dir = tmp_path / "models" / "local-llama"
    model_dir.mkdir(parents=True)
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    (model_dir / "model.safetensors").write_text("weights", encoding="utf-8")
    (model_dir / "tokenizer.json").write_text("{}", encoding="utf-8")

    client = InProcessTargetClient(LocalAgent(models_registry_path=registry_path))
    await client.connect()
    try:
        await client.call(
            "pin_model",
            {
                "entry_id": "01LOCAL",
                "source": "local_path",
                "local_path": str(model_dir),
            },
        )
        verified = await client.call("verify_model", {"model_ref": "01LOCAL"})
    finally:
        await client.disconnect()

    assert verified["entry_id"] == "01LOCAL"
    assert verified["ok"] is True
    assert verified["cache_state"] == "cached"
    assert verified["detail"] == "local model verified"
    assert verified["entry"]["cache_state"] == "cached"
    assert verified["entry"]["integrity"]["strategy"] == "local_files_sha256"
    assert verified["entry"]["integrity"]["files_sha256"].startswith("sha256:")
    assert verified["entry"]["integrity"]["file_count"] == 3
    assert verified["entry"]["integrity"]["total_bytes"] == (
        len(b"{}") + len(b"weights") + len(b"{}")
    )
    json.dumps(verified)


@pytest.mark.asyncio
async def test_agent_verify_marks_cached_hf_model_partial_without_identity(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "state" / "vllm-loader" / "models" / "registry.json"

    client = InProcessTargetClient(LocalAgent(models_registry_path=registry_path))
    await client.connect()
    try:
        await client.call(
            "pin_model",
            {
                "entry_id": "01REMOTE",
                "display_name": "remote-llama",
                "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
                "cache_state": "cached",
            },
        )
        verified = await client.call("verify_model", {"model_ref": "01REMOTE"})
        listed = await client.call("list_models")
    finally:
        await client.disconnect()

    assert verified["entry_id"] == "01REMOTE"
    assert verified["ok"] is False
    assert verified["cache_state"] == "partial"
    assert verified["reason"] == "missing-commit"
    assert verified["entry"]["cache_state"] == "partial"
    assert listed["models"][0]["cache_state"] == "partial"
    json.dumps(verified)


@pytest.mark.asyncio
async def test_agent_inspects_model_metadata(tmp_path: Path) -> None:
    registry_path = tmp_path / "state" / "vllm-loader" / "models" / "registry.json"

    client = InProcessTargetClient(LocalAgent(models_registry_path=registry_path))
    await client.connect()
    try:
        await client.call(
            "pin_model",
            {
                "entry_id": "01MODEL",
                "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
                "display_name": "llama-pin",
                "revision": "main",
                "commit_sha": "abc123",
                "quant_format": "awq",
            },
        )
        inspected = await client.call("inspect_model", {"model_ref": "llama-pin"})
    finally:
        await client.disconnect()

    assert inspected["entry"]["entry_id"] == "01MODEL"
    assert inspected["entry"]["display_name"] == "llama-pin"
    assert inspected["entry"]["repo_id"] == "meta-llama/Llama-3.1-8B-Instruct"
    assert inspected["entry"]["commit_sha"] == "abc123"
    json.dumps(inspected)


@pytest.mark.asyncio
async def test_agent_pin_model_resolves_revision_to_commit_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_path = tmp_path / "state" / "vllm-loader" / "models" / "registry.json"
    calls: list[dict[str, str | None]] = []

    def fake_hf_model_info(repo_id: str, revision: str | None = None) -> object:
        calls.append({"repo_id": repo_id, "revision": revision})
        return SimpleNamespace(sha="abc123", gated="manual")

    monkeypatch.setattr(model_registry_module, "_hf_model_info", fake_hf_model_info)

    client = InProcessTargetClient(LocalAgent(models_registry_path=registry_path))
    await client.connect()
    try:
        pinned = await client.call(
            "pin_model",
            {
                "entry_id": "01REMOTE",
                "display_name": "llama-remote",
                "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
                "revision": "main",
            },
        )
        inspected = await client.call("inspect_model", {"model_ref": "01REMOTE"})
    finally:
        await client.disconnect()

    assert calls == [
        {
            "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
            "revision": "main",
        }
    ]
    assert pinned["entry"]["commit_sha"] == "abc123"
    assert inspected["entry"]["commit_sha"] == "abc123"
    assert inspected["entry"]["gated"] is True
    assert inspected["entry"]["token_required"] is True
    json.dumps(inspected)


@pytest.mark.asyncio
async def test_agent_verify_marks_local_model_partial_after_drift(tmp_path: Path) -> None:
    registry_path = tmp_path / "state" / "vllm-loader" / "models" / "registry.json"
    model_dir = tmp_path / "models" / "local-llama"
    model_dir.mkdir(parents=True)
    weights_path = model_dir / "model.safetensors"
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    weights_path.write_text("weights", encoding="utf-8")
    (model_dir / "tokenizer.json").write_text("{}", encoding="utf-8")

    client = InProcessTargetClient(LocalAgent(models_registry_path=registry_path))
    await client.connect()
    try:
        await client.call(
            "pin_model",
            {
                "entry_id": "01LOCAL",
                "source": "local_path",
                "local_path": str(model_dir),
            },
        )
        weights_path.unlink()
        verified = await client.call("verify_model", {"model_ref": "01LOCAL"})
        listed = await client.call("list_models")
    finally:
        await client.disconnect()

    assert verified["entry_id"] == "01LOCAL"
    assert verified["ok"] is False
    assert verified["cache_state"] == "partial"
    assert verified["reason"] == "missing-weights"
    assert verified["entry"]["cache_state"] == "partial"
    assert listed["models"][0]["cache_state"] == "partial"
    json.dumps(verified)


@pytest.mark.asyncio
async def test_agent_verify_marks_local_model_partial_after_integrity_drift(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "state" / "vllm-loader" / "models" / "registry.json"
    model_dir = tmp_path / "models" / "local-llama"
    model_dir.mkdir(parents=True)
    weights_path = model_dir / "model.safetensors"
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    weights_path.write_text("weights", encoding="utf-8")
    (model_dir / "tokenizer.json").write_text("{}", encoding="utf-8")

    client = InProcessTargetClient(LocalAgent(models_registry_path=registry_path))
    await client.connect()
    try:
        adopted = await client.call(
            "pin_model",
            {
                "entry_id": "01LOCAL",
                "source": "local_path",
                "local_path": str(model_dir),
            },
        )
        expected_sha = adopted["entry"]["integrity"]["files_sha256"]
        weights_path.write_text("changed-weights", encoding="utf-8")
        verified = await client.call("verify_model", {"model_ref": "01LOCAL"})
        listed = await client.call("list_models")
    finally:
        await client.disconnect()

    assert verified["entry_id"] == "01LOCAL"
    assert verified["ok"] is False
    assert verified["cache_state"] == "partial"
    assert verified["reason"] == "integrity-mismatch"
    assert verified["integrity"]["expected_files_sha256"] == expected_sha
    assert verified["integrity"]["current_files_sha256"].startswith("sha256:")
    assert verified["integrity"]["current_files_sha256"] != expected_sha
    assert verified["entry"]["cache_state"] == "partial"
    assert verified["entry"]["integrity"]["files_sha256"] == expected_sha
    assert listed["models"][0]["cache_state"] == "partial"
    assert listed["models"][0]["integrity"]["files_sha256"] == expected_sha
    json.dumps(verified)


@pytest.mark.asyncio
async def test_agent_refresh_reconciles_local_model_entries(tmp_path: Path) -> None:
    registry_path = tmp_path / "state" / "vllm-loader" / "models" / "registry.json"
    model_dir = tmp_path / "models" / "local-llama"
    model_dir.mkdir(parents=True)
    weights_path = model_dir / "model.safetensors"
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    weights_path.write_text("weights", encoding="utf-8")
    (model_dir / "tokenizer.json").write_text("{}", encoding="utf-8")

    client = InProcessTargetClient(LocalAgent(models_registry_path=registry_path))
    await client.connect()
    try:
        await client.call(
            "pin_model",
            {
                "entry_id": "01LOCAL",
                "display_name": "local-llama",
                "source": "local_path",
                "local_path": str(model_dir),
            },
        )
        weights_path.unlink()
        refreshed = await client.call("refresh_models")
    finally:
        await client.disconnect()

    assert refreshed["refreshed"] == 1
    assert refreshed["models"][0]["entry_id"] == "01LOCAL"
    assert refreshed["models"][0]["cache_state"] == "partial"
    json.dumps(refreshed)


@pytest.mark.asyncio
async def test_agent_refresh_reconciles_hf_pin_from_cache_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_path = tmp_path / "state" / "vllm-loader" / "models" / "registry.json"
    fake_revision = SimpleNamespace(
        commit_hash="abc123",
        size_on_disk=130,
        files=(
            SimpleNamespace(file_name="config.json", size_on_disk=10),
            SimpleNamespace(file_name="model.safetensors", size_on_disk=100),
            SimpleNamespace(file_name="tokenizer.json", size_on_disk=20),
        ),
        refs=("main",),
    )
    fake_repo = SimpleNamespace(
        repo_id="meta-llama/Llama-3.1-8B-Instruct",
        repo_type="model",
        revisions=(fake_revision,),
    )
    monkeypatch.setattr(
        model_registry_module,
        "_scan_hf_cache_info",
        lambda: SimpleNamespace(repos=(fake_repo,)),
        raising=False,
    )

    client = InProcessTargetClient(LocalAgent(models_registry_path=registry_path))
    await client.connect()
    try:
        await client.call(
            "pin_model",
            {
                "entry_id": "01REMOTE",
                "display_name": "llama-remote",
                "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
                "revision": "main",
                "cache_state": "remote_only",
            },
        )
        refreshed = await client.call("refresh_models")
    finally:
        await client.disconnect()

    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    [entry] = registry["entries"]
    assert refreshed["refreshed"] == 1
    assert refreshed["models"][0]["entry_id"] == "01REMOTE"
    assert refreshed["models"][0]["cache_state"] == "cached"
    assert refreshed["models"][0]["commit_sha"] == "abc123"
    assert refreshed["models"][0]["files"]["total_bytes"] == 130
    assert entry["cache_state"] == "cached"
    assert entry["commit_sha"] == "abc123"
    assert entry["files"]["weights_format"] == "safetensors"
    json.dumps(refreshed)


@pytest.mark.asyncio
async def test_agent_refresh_marks_missing_hf_cache_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_path = tmp_path / "state" / "vllm-loader" / "models" / "registry.json"
    monkeypatch.setattr(
        model_registry_module,
        "_scan_hf_cache_info",
        lambda: SimpleNamespace(repos=()),
        raising=False,
    )

    client = InProcessTargetClient(LocalAgent(models_registry_path=registry_path))
    await client.connect()
    try:
        await client.call(
            "pin_model",
            {
                "entry_id": "01REMOTE",
                "display_name": "llama-remote",
                "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
                "revision": "main",
                "commit_sha": "abc123",
                "cache_state": "cached",
            },
        )
        refreshed = await client.call("refresh_models")
    finally:
        await client.disconnect()

    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    [entry] = registry["entries"]
    assert refreshed["refreshed"] == 1
    assert refreshed["models"][0]["entry_id"] == "01REMOTE"
    assert refreshed["models"][0]["cache_state"] == "missing"
    assert entry["cache_state"] == "missing"
    json.dumps(refreshed)


@pytest.mark.asyncio
async def test_agent_removes_unpinned_local_model_metadata(
    config_dir: Path, tmp_path: Path
) -> None:
    registry_path = tmp_path / "state" / "vllm-loader" / "models" / "registry.json"
    model_dir = tmp_path / "models" / "local-llama"
    model_dir.mkdir(parents=True)
    weights_path = model_dir / "model.safetensors"
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    weights_path.write_text("weights", encoding="utf-8")
    (model_dir / "tokenizer.json").write_text("{}", encoding="utf-8")
    write_yaml(config_dir / "other.yaml", "name: other\nmodel: org/other")

    client = InProcessTargetClient(LocalAgent(models_registry_path=registry_path))
    await client.connect()
    try:
        await client.call(
            "pin_model",
            {
                "entry_id": "01LOCAL",
                "display_name": "local-llama",
                "source": "local_path",
                "local_path": str(model_dir),
            },
        )
        removed = await client.call(
            "remove_model",
            {"model_ref": "01LOCAL", "configs_dir": str(config_dir)},
        )
        listed = await client.call("list_models")
    finally:
        await client.disconnect()

    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    assert removed["entry_id"] == "01LOCAL"
    assert removed["source"] == "local_path"
    assert removed["removed_weights"] is False
    assert removed["entry"]["local_path"] == str(model_dir)
    assert listed["models"] == []
    assert registry["entries"] == []
    assert weights_path.exists()
    json.dumps(removed)


@pytest.mark.asyncio
async def test_agent_removes_cached_hf_model_via_cache_delete_revisions(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_path = tmp_path / "state" / "vllm-loader" / "models" / "registry.json"
    write_yaml(config_dir / "other.yaml", "name: other\nmodel: org/other")
    executed: list[tuple[str, ...]] = []

    class FakeDeleteStrategy:
        expected_freed_size = 42

        def __init__(self, revisions: tuple[str, ...]) -> None:
            self.revisions = revisions

        def execute(self) -> None:
            executed.append(self.revisions)

    class FakeCacheInfo:
        def delete_revisions(self, *revisions: str) -> FakeDeleteStrategy:
            return FakeDeleteStrategy(tuple(revisions))

    monkeypatch.setattr(
        model_registry_module,
        "_scan_hf_cache_info",
        lambda: FakeCacheInfo(),
        raising=False,
    )

    client = InProcessTargetClient(LocalAgent(models_registry_path=registry_path))
    await client.connect()
    try:
        await client.call(
            "pin_model",
            {
                "entry_id": "01REMOTE",
                "display_name": "llama-remote",
                "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
                "commit_sha": "abc123",
                "cache_state": "cached",
            },
        )
        removed = await client.call(
            "remove_model",
            {"model_ref": "01REMOTE", "configs_dir": str(config_dir)},
        )
        listed = await client.call("list_models")
    finally:
        await client.disconnect()

    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    assert executed == [("abc123",)]
    assert removed["entry_id"] == "01REMOTE"
    assert removed["source"] == "hf_repo"
    assert removed["removed_weights"] is True
    assert removed["expected_freed_size"] == 42
    assert listed["models"] == []
    assert registry["entries"] == []
    json.dumps(removed)


@pytest.mark.asyncio
async def test_agent_removes_remote_only_hf_model_metadata_without_cache_access(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_path = tmp_path / "state" / "vllm-loader" / "models" / "registry.json"
    write_yaml(config_dir / "other.yaml", "name: other\nmodel: org/other")

    def fail_scan_cache() -> object:
        raise AssertionError("remote-only removal should not inspect the HF cache")

    monkeypatch.setattr(
        model_registry_module,
        "_scan_hf_cache_info",
        fail_scan_cache,
        raising=False,
    )

    client = InProcessTargetClient(LocalAgent(models_registry_path=registry_path))
    await client.connect()
    try:
        await client.call(
            "pin_model",
            {
                "entry_id": "01REMOTE",
                "display_name": "llama-remote",
                "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
                "commit_sha": "abc123",
                "cache_state": "remote_only",
            },
        )
        removed = await client.call(
            "remove_model",
            {"model_ref": "01REMOTE", "configs_dir": str(config_dir)},
        )
    finally:
        await client.disconnect()

    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    assert removed["entry_id"] == "01REMOTE"
    assert removed["source"] == "hf_repo"
    assert removed["removed_weights"] is False
    assert removed["expected_freed_size"] == 0
    assert registry["entries"] == []
    json.dumps(removed)


@pytest.mark.asyncio
async def test_agent_refuses_to_remove_model_pinned_by_config(
    config_dir: Path, tmp_path: Path
) -> None:
    registry_path = tmp_path / "state" / "vllm-loader" / "models" / "registry.json"
    write_yaml(
        config_dir / "uses-pinned.yaml",
        """
        name: uses-pinned
        model: meta-llama/Llama-3.1-8B-Instruct
        model_ref: 01PINNED
        """,
    )

    client = InProcessTargetClient(LocalAgent(models_registry_path=registry_path))
    await client.connect()
    try:
        await client.call(
            "pin_model",
            {
                "entry_id": "01PINNED",
                "display_name": "llama-pinned",
                "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
            },
        )
        with pytest.raises(TargetCallError) as exc_info:
            await client.call(
                "remove_model",
                {"model_ref": "01PINNED", "configs_dir": str(config_dir)},
            )
        listed = await client.call("list_models")
    finally:
        await client.disconnect()

    assert exc_info.value.code == "resource-in-use"
    assert exc_info.value.details["reason"] == "config-pin"
    assert exc_info.value.details["configs"] == ["uses-pinned"]
    assert listed["models"][0]["entry_id"] == "01PINNED"


@pytest.mark.asyncio
async def test_agent_refuses_to_remove_model_pinned_by_bare_model_revision(
    config_dir: Path, tmp_path: Path
) -> None:
    registry_path = tmp_path / "state" / "vllm-loader" / "models" / "registry.json"
    write_yaml(
        config_dir / "uses-revision.yaml",
        """
        name: uses-revision
        model: meta-llama/Llama-3.1-8B-Instruct
        revision: abc123
        """,
    )

    client = InProcessTargetClient(LocalAgent(models_registry_path=registry_path))
    await client.connect()
    try:
        await client.call(
            "pin_model",
            {
                "entry_id": "01PINNED",
                "display_name": "llama-pinned",
                "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
                "revision": "main",
                "commit_sha": "abc123",
            },
        )
        with pytest.raises(TargetCallError) as exc_info:
            await client.call(
                "remove_model",
                {"model_ref": "01PINNED", "configs_dir": str(config_dir)},
            )
        listed = await client.call("list_models")
    finally:
        await client.disconnect()

    assert exc_info.value.code == "resource-in-use"
    assert exc_info.value.details["reason"] == "config-pin"
    assert exc_info.value.details["configs"] == ["uses-revision"]
    assert listed["models"][0]["entry_id"] == "01PINNED"


@pytest.mark.asyncio
async def test_agent_force_removes_model_pinned_by_config(
    config_dir: Path, tmp_path: Path
) -> None:
    registry_path = tmp_path / "state" / "vllm-loader" / "models" / "registry.json"
    write_yaml(
        config_dir / "uses-pinned.yaml",
        """
        name: uses-pinned
        model: meta-llama/Llama-3.1-8B-Instruct
        model_ref: 01PINNED
        """,
    )

    client = InProcessTargetClient(LocalAgent(models_registry_path=registry_path))
    await client.connect()
    try:
        await client.call(
            "pin_model",
            {
                "entry_id": "01PINNED",
                "display_name": "llama-pinned",
                "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
            },
        )
        removed = await client.call(
            "remove_model",
            {
                "model_ref": "01PINNED",
                "configs_dir": str(config_dir),
                "force": True,
            },
        )
        listed = await client.call("list_models")
    finally:
        await client.disconnect()

    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    assert removed["entry_id"] == "01PINNED"
    assert removed["entry"]["display_name"] == "llama-pinned"
    assert removed["removed_weights"] is False
    assert listed["models"] == []
    assert registry["entries"] == []
    json.dumps(removed)


@pytest.mark.asyncio
async def test_agent_refuses_to_force_remove_model_used_by_live_run(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_path = tmp_path / "state" / "vllm-loader" / "models" / "registry.json"
    write_yaml(config_dir / "other.yaml", "name: other\nmodel: org/other")
    sidecar_path = tmp_path / "runs" / "live-model.json"
    live_sidecar = Sidecar(
        run_id="run-live-model",
        config_name="live-config",
        command_argv=[
            "/opt/vllm/bin/vllm",
            "serve",
            "meta-llama/Llama-3.1-8B-Instruct",
            "--revision",
            "abc123",
        ],
        command_hash="sha256:test",
        pid=123,
        pgid=123,
        process_create_time=1.0,
        executable="/opt/vllm/bin/vllm",
        cwd=str(tmp_path),
        launch_mode="detached",
        host="127.0.0.1",
        port=8000,
        served_model_names=["llama-live"],
        exposure="local",
        manifest_path=str(tmp_path / "runs" / "live-model.manifest.json"),
        config_snapshot={
            "name": "live-config",
            "model": "meta-llama/Llama-3.1-8B-Instruct",
            "model_ref": "01REMOTE",
            "revision": "abc123",
        },
    )
    monkeypatch.setattr(
        local_agent_module,
        "discover_active_sidecars",
        lambda runs_dirs: [sidecar_path],
    )
    monkeypatch.setattr(local_agent_module, "verify_sidecar_from_system", lambda path: True)
    monkeypatch.setattr(local_agent_module, "load_sidecar", lambda path: live_sidecar)

    client = InProcessTargetClient(LocalAgent(models_registry_path=registry_path))
    await client.connect()
    try:
        await client.call(
            "pin_model",
            {
                "entry_id": "01REMOTE",
                "display_name": "llama-remote",
                "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
                "revision": "main",
                "commit_sha": "abc123",
                "cache_state": "remote_only",
            },
        )
        with pytest.raises(TargetCallError) as exc_info:
            await client.call(
                "remove_model",
                {
                    "model_ref": "llama-remote",
                    "configs_dir": str(config_dir),
                    "force": True,
                },
            )
    finally:
        await client.disconnect()

    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    assert exc_info.value.code == "resource-in-use"
    assert exc_info.value.details["reason"] == "live-run"
    assert exc_info.value.details["model_ref"] == "llama-remote"
    assert exc_info.value.details["run_id"] == "run-live-model"
    assert exc_info.value.details["config_name"] == "live-config"
    assert registry["entries"][0]["entry_id"] == "01REMOTE"


@pytest.mark.asyncio
async def test_agent_prepare_launch_resolves_hf_model_ref_handoff(
    config_dir: Path, tmp_path: Path
) -> None:
    registry_path = tmp_path / "state" / "vllm-loader" / "models" / "registry.json"
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "entries": [
                    {
                        "entry_id": "01MODEL",
                        "display_name": "llama-pin",
                        "source": "hf_repo",
                        "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
                        "revision": "main",
                        "commit_sha": "abc123",
                        "tokenizer": "meta-llama/Llama-3.1-8B-Instruct",
                        "cache_state": "cached",
                        "gated": True,
                        "token_required": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    write_yaml(
        config_dir / "pinned-model.yaml",
        """
        name: pinned-model
        model: meta-llama/Llama-3.1-8B-Instruct
        model_ref: 01MODEL
        env:
          HF_TOKEN: hf_live
        """,
    )

    client = InProcessTargetClient(LocalAgent(models_registry_path=registry_path))
    await client.connect()
    try:
        prepared = await client.call(
            "prepare_launch",
            {"name": "pinned-model", "configs_dir": str(config_dir)},
        )
    finally:
        await client.disconnect()

    argv = prepared["build"]["argv"]
    metadata = prepared["build"]["metadata"]
    assert argv[:3] == [
        "vllm",
        "serve",
        "meta-llama/Llama-3.1-8B-Instruct",
    ]
    assert argv[argv.index("--revision") + 1] == "abc123"
    assert argv[argv.index("--tokenizer") + 1] == "meta-llama/Llama-3.1-8B-Instruct"
    assert metadata["model_ref"] == "01MODEL"
    assert metadata["model_entry_id"] == "01MODEL"
    assert metadata["model_display_name"] == "llama-pin"
    assert metadata["model_source"] == "hf_repo"
    assert metadata["model_repo_id"] == "meta-llama/Llama-3.1-8B-Instruct"
    assert metadata["model_revision"] == "abc123"
    assert metadata["model_token_required"] is True
    assert metadata["model_gated"] is True
    assert prepared["config"]["model"] == "meta-llama/Llama-3.1-8B-Instruct"
    assert prepared["config"]["model_ref"] == "01MODEL"
    json.dumps(prepared)


@pytest.mark.asyncio
async def test_agent_prepare_launch_rejects_model_ref_repo_mismatch(
    config_dir: Path, tmp_path: Path
) -> None:
    registry_path = tmp_path / "state" / "vllm-loader" / "models" / "registry.json"
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "entries": [
                    {
                        "entry_id": "01MODEL",
                        "display_name": "llama-pin",
                        "source": "hf_repo",
                        "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
                        "revision": "main",
                        "commit_sha": "abc123",
                        "cache_state": "cached",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    write_yaml(
        config_dir / "mismatched-model.yaml",
        """
        name: mismatched-model
        model: Qwen/Qwen3-32B
        model_ref: 01MODEL
        """,
    )

    client = InProcessTargetClient(LocalAgent(models_registry_path=registry_path))
    await client.connect()
    try:
        with pytest.raises(TargetCallError) as exc_info:
            await client.call(
                "prepare_launch",
                {"name": "mismatched-model", "configs_dir": str(config_dir)},
            )
    finally:
        await client.disconnect()

    assert exc_info.value.code == "invalid-config"
    assert "model_ref" in exc_info.value.message
    assert exc_info.value.details == {
        "model": "Qwen/Qwen3-32B",
        "model_ref": "01MODEL",
        "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
        "reason": "model-ref-repo-mismatch",
    }


@pytest.mark.asyncio
async def test_agent_prepare_launch_blocks_gated_model_ref_without_hf_token(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_path = tmp_path / "state" / "vllm-loader" / "models" / "registry.json"
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "entries": [
                    {
                        "entry_id": "01GATED",
                        "display_name": "llama-gated",
                        "source": "hf_repo",
                        "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
                        "revision": "main",
                        "commit_sha": "abc123",
                        "cache_state": "cached",
                        "gated": True,
                        "token_required": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    write_yaml(
        config_dir / "gated-model.yaml",
        """
        name: gated-model
        model: meta-llama/Llama-3.1-8B-Instruct
        model_ref: 01GATED
        """,
    )
    monkeypatch.delenv("HF_TOKEN", raising=False)

    client = InProcessTargetClient(LocalAgent(models_registry_path=registry_path))
    await client.connect()
    try:
        with pytest.raises(TargetCallError) as exc_info:
            await client.call(
                "prepare_launch",
                {"name": "gated-model", "configs_dir": str(config_dir)},
            )
    finally:
        await client.disconnect()

    assert exc_info.value.code == "hf-auth-required"
    assert "HF_TOKEN" in exc_info.value.message
    assert exc_info.value.details == {
        "model_ref": "01GATED",
        "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
        "reason": "missing-hf-token",
    }


@pytest.mark.asyncio
async def test_agent_prepare_launch_blocks_remote_only_model_ref_when_offline(
    config_dir: Path, tmp_path: Path
) -> None:
    registry_path = tmp_path / "state" / "vllm-loader" / "models" / "registry.json"
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "entries": [
                    {
                        "entry_id": "01REMOTE",
                        "display_name": "llama-remote",
                        "source": "hf_repo",
                        "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
                        "revision": "main",
                        "commit_sha": "abc123",
                        "cache_state": "remote_only",
                        "gated": False,
                        "token_required": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    write_yaml(
        config_dir / "offline-model.yaml",
        """
        name: offline-model
        model: meta-llama/Llama-3.1-8B-Instruct
        model_ref: 01REMOTE
        env:
          HF_HUB_OFFLINE: "1"
        """,
    )

    client = InProcessTargetClient(LocalAgent(models_registry_path=registry_path))
    await client.connect()
    try:
        with pytest.raises(TargetCallError) as exc_info:
            await client.call(
                "prepare_launch",
                {"name": "offline-model", "configs_dir": str(config_dir)},
            )
    finally:
        await client.disconnect()

    assert exc_info.value.code == "model-unavailable"
    assert "offline" in exc_info.value.message
    assert exc_info.value.details == {
        "model_ref": "01REMOTE",
        "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
        "cache_state": "remote_only",
        "reason": "offline-remote-only",
    }


@pytest.mark.asyncio
async def test_agent_preview_renders_model_ref_even_when_launch_would_block(
    config_dir: Path, tmp_path: Path
) -> None:
    registry_path = tmp_path / "state" / "vllm-loader" / "models" / "registry.json"
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "entries": [
                    {
                        "entry_id": "01REMOTE",
                        "display_name": "llama-remote",
                        "source": "hf_repo",
                        "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
                        "revision": "main",
                        "commit_sha": "abc123",
                        "cache_state": "remote_only",
                        "gated": False,
                        "token_required": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    write_yaml(
        config_dir / "offline-preview.yaml",
        """
        name: offline-preview
        model: meta-llama/Llama-3.1-8B-Instruct
        model_ref: 01REMOTE
        env:
          HF_HUB_OFFLINE: "1"
        """,
    )

    client = InProcessTargetClient(LocalAgent(models_registry_path=registry_path))
    await client.connect()
    try:
        result = await client.call(
            "preview",
            {"name": "offline-preview", "configs_dir": str(config_dir)},
        )
    finally:
        await client.disconnect()

    assert "vllm serve meta-llama/Llama-3.1-8B-Instruct" in result["preview"]
    assert "--revision abc123" in result["preview"]
    assert result["metadata"]["model_ref"] == "01REMOTE"


@pytest.mark.asyncio
async def test_agent_prepare_launch_resolves_local_model_ref_handoff(
    config_dir: Path, tmp_path: Path
) -> None:
    model_dir = tmp_path / "models" / "llama-local"
    model_dir.mkdir(parents=True)
    registry_path = tmp_path / "state" / "vllm-loader" / "models" / "registry.json"
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "entries": [
                    {
                        "entry_id": "01LOCAL",
                        "display_name": "local-llama",
                        "source": "local_path",
                        "local_path": str(model_dir),
                        "cache_state": "cached",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    write_yaml(
        config_dir / "local-model.yaml",
        """
        name: local-model
        model: local-llama
        model_ref: local-llama
        """,
    )

    client = InProcessTargetClient(LocalAgent(models_registry_path=registry_path))
    await client.connect()
    try:
        prepared = await client.call(
            "prepare_launch",
            {"name": "local-model", "configs_dir": str(config_dir)},
        )
    finally:
        await client.disconnect()

    argv = prepared["build"]["argv"]
    metadata = prepared["build"]["metadata"]
    assert argv[:3] == ["vllm", "serve", str(model_dir)]
    assert "--revision" not in argv
    assert metadata["model_ref"] == "local-llama"
    assert metadata["model_entry_id"] == "01LOCAL"
    assert metadata["model_display_name"] == "local-llama"
    assert metadata["model_source"] == "local_path"
    assert metadata["model_local_path"] == str(model_dir)
    assert prepared["config"]["model"] == "local-llama"
    assert prepared["config"]["model_ref"] == "local-llama"
    json.dumps(prepared)


@pytest.mark.asyncio
async def test_gpu_method_can_emit_serialized_agent_stream_event() -> None:
    def sampler() -> GpuPollResult:
        return GpuPollResult(
            [
                GpuSample(
                    visible_index=0,
                    uuid="GPU-a",
                    name="A100",
                    memory_used_mb=1024,
                    memory_total_mb=81920,
                    utilization_percent=25,
                    temperature_c=42,
                    power_w=110,
                )
            ]
        )

    client = InProcessTargetClient(LocalAgent(gpu_sampler=sampler))
    await client.connect()
    events = client.subscribe(["__agent__"], resume_from="live")
    try:
        result = await client.call(
            "gpu", {"emit_event": True, "sub_id": "gpu-panel"}
        )
        event = await asyncio.wait_for(events.__anext__(), timeout=2)
    finally:
        await events.aclose()
        await client.disconnect()

    assert result["samples"][0]["name"] == "A100"
    assert event["event"] == "gpu"
    assert event["run_id"] == "__agent__"
    assert event["sub_id"] == "gpu-panel"
    assert event["samples"][0]["name"] == "A100"
    json.dumps(event)


@pytest.mark.asyncio
async def test_gpu_method_starts_and_stops_agent_stream_by_sub_id() -> None:
    samples = [
        GpuPollResult(
            [
                GpuSample(
                    visible_index=0,
                    uuid="GPU-a",
                    name="A100",
                    memory_used_mb=1024,
                    memory_total_mb=81920,
                    utilization_percent=25,
                    temperature_c=42,
                    power_w=110,
                )
            ]
        ),
        GpuPollResult(
            [
                GpuSample(
                    visible_index=0,
                    uuid="GPU-a",
                    name="A100",
                    memory_used_mb=2048,
                    memory_total_mb=81920,
                    utilization_percent=50,
                    temperature_c=43,
                    power_w=120,
                )
            ]
        ),
    ]
    calls = 0

    def sampler() -> GpuPollResult:
        nonlocal calls
        result = samples[min(calls, len(samples) - 1)]
        calls += 1
        return result

    client = InProcessTargetClient(LocalAgent(gpu_sampler=sampler))
    await client.connect()
    events = client.subscribe(["__agent__"], resume_from="live")
    try:
        result = await client.call(
            "gpu", {"sub_id": "gpu-panel", "interval_s": 0.01}
        )
        first = await asyncio.wait_for(events.__anext__(), timeout=2)
        second = await asyncio.wait_for(events.__anext__(), timeout=2)
        unsubscribed = await client.call("unsubscribe", {"sub_id": "gpu-panel"})
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(events.__anext__(), timeout=0.05)
    finally:
        await events.aclose()
        await client.disconnect()

    assert result == {"sub_id": "gpu-panel"}
    assert first["event"] == "gpu"
    assert first["run_id"] == "__agent__"
    assert first["sub_id"] == "gpu-panel"
    assert first["samples"][0]["memory_used_mb"] == 1024
    assert second["event"] == "gpu"
    assert second["sub_id"] == "gpu-panel"
    assert second["samples"][0]["memory_used_mb"] == 2048
    assert unsubscribed == {"sub_id": "gpu-panel"}
    json.dumps(first)
    json.dumps(second)


@pytest.mark.asyncio
async def test_agent_create_build_adopt_job_streams_and_writes_manifest(
    tmp_path: Path,
) -> None:
    builds_root = tmp_path / "data" / "vllm-loader" / "builds"
    venv_dir = tmp_path / "external" / "vllm-nightly"
    bin_dir = venv_dir / "bin"
    bin_dir.mkdir(parents=True)
    vllm_bin = bin_dir / "vllm"
    python_bin = bin_dir / "python"
    vllm_bin.write_text("#!/bin/sh\necho 'vLLM 0.17.0.dev'\n", encoding="utf-8")
    python_bin.write_text("#!/bin/sh\necho '0.17.0.dev'\n", encoding="utf-8")
    vllm_bin.chmod(0o755)
    python_bin.chmod(0o755)

    client = InProcessTargetClient(LocalAgent(builds_root=builds_root))
    await client.connect()
    events = client.subscribe(["job-build-adopt-1"], resume_from="live")
    try:
        result = await client.call(
            "create_build",
            {
                "job_id": "job-build-adopt-1",
                "method": "adopt",
                "build_id": "01ADOPTED",
                "label": "external-nightly",
                "path": str(venv_dir),
                "vllm_version": "0.17.0.dev",
                "vllm_version_profile": "current",
            },
        )
        progress = await asyncio.wait_for(events.__anext__(), timeout=2)
        done = await asyncio.wait_for(events.__anext__(), timeout=2)
    finally:
        await events.aclose()
        await client.disconnect()

    manifest = json.loads(
        (builds_root / "01ADOPTED" / "build.json").read_text(encoding="utf-8")
    )
    assert result == {
        "job_id": "job-build-adopt-1",
        "kind": "create_build",
        "status": "running",
    }
    assert progress["event"] == "job_progress"
    assert progress["job_id"] == "job-build-adopt-1"
    assert progress["text"] == "Adopting build external-nightly"
    assert done["event"] == "job_done"
    assert done["job_id"] == "job-build-adopt-1"
    assert done["ok"] is True
    assert done["detail"] == "build adopted"
    assert done["build_id"] == "01ADOPTED"
    assert manifest["status"] == "adopted"
    assert manifest["integrity"]["executable_sha256"] == _sha256_uri(
        vllm_bin.read_bytes()
    )
    assert manifest["paths"]["root"] == str(builds_root / "01ADOPTED")
    assert manifest["paths"]["executable"] == "bin/vllm"
    assert (builds_root / "01ADOPTED" / "bin" / "vllm").resolve() == (
        venv_dir / "bin" / "vllm"
    )
    json.dumps(progress)
    json.dumps(done)


@pytest.mark.asyncio
async def test_agent_create_build_adopt_job_reports_registry_errors(
    tmp_path: Path,
) -> None:
    builds_root = tmp_path / "data" / "vllm-loader" / "builds"
    venv_dir = tmp_path / "external" / "broken-vllm"
    (venv_dir / "bin").mkdir(parents=True)
    (venv_dir / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")

    client = InProcessTargetClient(LocalAgent(builds_root=builds_root))
    await client.connect()
    events = client.subscribe(["job-build-adopt-broken"], resume_from="live")
    try:
        result = await client.call(
            "create_build",
            {
                "job_id": "job-build-adopt-broken",
                "method": "adopt-existing-venv",
                "build_id": "01BROKEN",
                "path": str(venv_dir),
            },
        )
        progress = await asyncio.wait_for(events.__anext__(), timeout=2)
        done = await asyncio.wait_for(events.__anext__(), timeout=2)
    finally:
        await events.aclose()
        await client.disconnect()

    assert result == {
        "job_id": "job-build-adopt-broken",
        "kind": "create_build",
        "status": "running",
    }
    assert progress["event"] == "job_progress"
    assert progress["text"] == "Adopting build 01BROKEN"
    assert done["event"] == "job_done"
    assert done["job_id"] == "job-build-adopt-broken"
    assert done["ok"] is False
    assert done["error_kind"] == "invalid-config"
    assert done["reason"] == "missing-executable"
    assert not (builds_root / "01BROKEN" / "build.json").exists()
    json.dumps(progress)
    json.dumps(done)


@pytest.mark.asyncio
async def test_agent_create_build_adopt_job_defaults_build_id_from_job_id(
    tmp_path: Path,
) -> None:
    builds_root = tmp_path / "data" / "vllm-loader" / "builds"
    venv_dir = tmp_path / "external" / "generated-build-id"
    bin_dir = venv_dir / "bin"
    bin_dir.mkdir(parents=True)
    vllm_bin = bin_dir / "vllm"
    python_bin = bin_dir / "python"
    vllm_bin.write_text("#!/bin/sh\necho 'vLLM 0.17.0.dev'\n", encoding="utf-8")
    python_bin.write_text("#!/bin/sh\necho '0.17.0.dev'\n", encoding="utf-8")
    vllm_bin.chmod(0o755)
    python_bin.chmod(0o755)

    client = InProcessTargetClient(LocalAgent(builds_root=builds_root))
    await client.connect()
    events = client.subscribe(["job-build-generated-id"], resume_from="live")
    try:
        result = await client.call(
            "create_build",
            {
                "job_id": "job-build-generated-id",
                "method": "adopt",
                "label": "generated-build-id",
                "path": str(venv_dir),
            },
        )
        progress = await asyncio.wait_for(events.__anext__(), timeout=2)
        done = await asyncio.wait_for(events.__anext__(), timeout=2)
    finally:
        await events.aclose()
        await client.disconnect()

    manifest = json.loads(
        (builds_root / "job-build-generated-id" / "build.json").read_text(
            encoding="utf-8"
        )
    )
    assert result == {
        "job_id": "job-build-generated-id",
        "kind": "create_build",
        "status": "running",
    }
    assert progress["text"] == "Adopting build generated-build-id"
    assert done["ok"] is True
    assert done["build_id"] == "job-build-generated-id"
    assert manifest["build_id"] == "job-build-generated-id"
    assert manifest["status"] == "adopted"
    assert manifest["integrity"]["executable_sha256"] == _sha256_uri(
        vllm_bin.read_bytes()
    )
    json.dumps(progress)
    json.dumps(done)


@pytest.mark.asyncio
async def test_agent_create_build_pip_job_installs_managed_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builds_root = tmp_path / "data" / "vllm-loader" / "builds"
    command_calls: list[list[str]] = []

    async def fake_build_subprocess_exec(
        argv: list[str],
        *,
        env: dict[str, str],
        cwd: Path,
        emit,
        phase: str,
        cancel_event: asyncio.Event,
    ) -> int:
        command_calls.append(list(argv))
        assert env["VLLM_USE_PRECOMPILED"] == "1"
        assert cwd == builds_root / "01PIP"
        assert cancel_event.is_set() is False
        if argv[1:3] == ["-m", "venv"]:
            venv_dir = Path(argv[-1])
            bin_dir = venv_dir / "bin"
            bin_dir.mkdir(parents=True)
            (bin_dir / "python").write_text("#!/bin/sh\n", encoding="utf-8")
            (bin_dir / "pip").write_text("#!/bin/sh\n", encoding="utf-8")
            (bin_dir / "vllm").write_text("#!/bin/sh\n", encoding="utf-8")
            (bin_dir / "activate").write_text("# activate\n", encoding="utf-8")
        emit(
            {
                "kind": "committed",
                "text": f"{phase}: {' '.join(argv)}",
                "level": "INFO",
                "phase": phase,
            }
        )
        return 0

    def fake_resolve_versions(_venv_path: Path) -> dict[str, str]:
        return {
            "vllm": "0.11.2",
            "vllm_version_profile": "current",
            "python": "3.12.7",
        }

    monkeypatch.setattr(
        local_agent_module,
        "_build_subprocess_exec",
        fake_build_subprocess_exec,
        raising=False,
    )
    monkeypatch.setattr(local_agent_module, "_find_uv_executable", lambda: None, raising=False)
    monkeypatch.setattr(
        local_agent_module,
        "_managed_build_resolved_versions",
        fake_resolve_versions,
        raising=False,
    )

    client = InProcessTargetClient(LocalAgent(builds_root=builds_root))
    await client.connect()
    events = client.subscribe(["job-build-pip"], resume_from="live")
    try:
        result = await client.call(
            "create_build",
            {
                "job_id": "job-build-pip",
                "method": "pip",
                "build_id": "01PIP",
                "label": "stable-cu124",
                "spec": "vllm==0.11.2",
                "python": "3.12",
                "env": ["VLLM_USE_PRECOMPILED=1"],
            },
        )
        progress = []
        done = None
        for _ in range(6):
            event = await asyncio.wait_for(events.__anext__(), timeout=2)
            if event.get("event") == "job_done":
                done = event
                break
            progress.append(event)
    finally:
        await events.aclose()
        await client.disconnect()

    build_dir = builds_root / "01PIP"
    manifest = json.loads((build_dir / "build.json").read_text(encoding="utf-8"))
    assert result == {
        "job_id": "job-build-pip",
        "kind": "create_build",
        "status": "running",
    }
    assert done is not None
    assert [event["event"] for event in progress] == ["job_progress"] * 4
    assert progress[0]["text"] == "Creating build stable-cu124"
    assert progress[-1]["text"] == "Verifying build stable-cu124"
    assert done["event"] == "job_done"
    assert done["job_id"] == "job-build-pip"
    assert done["ok"] is True
    assert done["detail"] == "build ready"
    assert done["build_id"] == "01PIP"
    assert done["label"] == "stable-cu124"
    assert done["status"] == "ready"
    assert done["manifest"]["paths"]["root"] == str(build_dir)
    assert manifest["status"] == "ready"
    assert manifest["install"] == {
        "method": "pip",
        "installer": "pip",
        "python_requested": "3.12",
        "provenance": {
            "pip_spec": "vllm==0.11.2",
            "env_overrides": {"VLLM_USE_PRECOMPILED": "1"},
        },
        "exit_code": 0,
    }
    assert manifest["resolved"]["vllm"] == "0.11.2"
    assert manifest["paths"] == {
        "root": str(build_dir),
        "venv": "venv",
        "executable": "bin/vllm",
        "python": "bin/python",
        "activate": "activate",
        "run_script": "run.sh",
    }
    assert (build_dir / "bin" / "vllm").resolve() == (
        build_dir / "venv" / "bin" / "vllm"
    ).resolve()
    assert (build_dir / "bin" / "python").resolve() == (
        build_dir / "venv" / "bin" / "python"
    ).resolve()
    assert (build_dir / "activate").resolve() == (
        build_dir / "venv" / "bin" / "activate"
    ).resolve()
    assert (build_dir / "run.sh").stat().st_mode & 0o111
    assert (build_dir / "install.log").stat().st_mode & 0o077 == 0
    assert command_calls == [
        [sys.executable, "-m", "venv", str(build_dir / "venv")],
        [
            str(build_dir / "venv" / "bin" / "python"),
            "-m",
            "pip",
            "install",
            "vllm==0.11.2",
        ],
    ]
    json.dumps(progress)
    json.dumps(done)


@pytest.mark.asyncio
async def test_agent_create_build_pip_job_prefers_uv_when_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builds_root = tmp_path / "data" / "vllm-loader" / "builds"
    uv_path = "/opt/bin/uv"
    command_calls: list[list[str]] = []

    async def fake_build_subprocess_exec(
        argv: list[str],
        *,
        env: dict[str, str],
        cwd: Path,
        emit,
        phase: str,
        cancel_event: asyncio.Event,
    ) -> int:
        del env, cwd, cancel_event
        command_calls.append(list(argv))
        if argv[:2] == [uv_path, "venv"]:
            venv_dir = Path(argv[-1])
            bin_dir = venv_dir / "bin"
            bin_dir.mkdir(parents=True)
            (bin_dir / "python").write_text("#!/bin/sh\n", encoding="utf-8")
            (bin_dir / "pip").write_text("#!/bin/sh\n", encoding="utf-8")
            (bin_dir / "vllm").write_text("#!/bin/sh\n", encoding="utf-8")
            (bin_dir / "activate").write_text("# activate\n", encoding="utf-8")
        emit(
            {
                "kind": "committed",
                "text": f"{phase}: {' '.join(argv)}",
                "level": "INFO",
                "phase": phase,
            }
        )
        return 0

    monkeypatch.setattr(local_agent_module, "_find_uv_executable", lambda: uv_path, raising=False)
    monkeypatch.setattr(
        local_agent_module,
        "_build_subprocess_exec",
        fake_build_subprocess_exec,
        raising=False,
    )
    monkeypatch.setattr(
        local_agent_module,
        "_managed_build_resolved_versions",
        lambda _venv_path: {
            "vllm": "0.11.2",
            "vllm_version_profile": "current",
            "python": "3.12.7",
        },
        raising=False,
    )

    client = InProcessTargetClient(LocalAgent(builds_root=builds_root))
    await client.connect()
    events = client.subscribe(["job-build-uv-pip"], resume_from="live")
    try:
        await client.call(
            "create_build",
            {
                "job_id": "job-build-uv-pip",
                "method": "pip",
                "build_id": "01UVPIP",
                "label": "uv-stable",
                "spec": "vllm==0.11.2",
                "python": "3.12",
            },
        )
        done = await _next_event(events, event_name="job_done")
    finally:
        await events.aclose()
        await client.disconnect()

    build_dir = builds_root / "01UVPIP"
    manifest = json.loads((build_dir / "build.json").read_text(encoding="utf-8"))
    assert done["ok"] is True
    assert manifest["install"]["installer"] == "uv"
    assert manifest["install"]["provenance"]["torch_backend"] == "auto"
    assert command_calls == [
        [uv_path, "venv", "--python", "3.12", str(build_dir / "venv")],
        [
            uv_path,
            "pip",
            "install",
            "--python",
            str(build_dir / "venv" / "bin" / "python"),
            "vllm==0.11.2",
            "--torch-backend=auto",
        ],
    ]
    json.dumps(done)


@pytest.mark.asyncio
async def test_agent_create_build_nightly_requires_uv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builds_root = tmp_path / "data" / "vllm-loader" / "builds"
    monkeypatch.setattr(local_agent_module, "_find_uv_executable", lambda: None, raising=False)

    client = InProcessTargetClient(LocalAgent(builds_root=builds_root))
    await client.connect()
    events = client.subscribe(["job-build-nightly-no-uv"], resume_from="live")
    try:
        await client.call(
            "create_build",
            {
                "job_id": "job-build-nightly-no-uv",
                "method": "nightly",
                "build_id": "01NIGHTLY",
                "label": "nightly-cu130",
                "channel": "cu130",
            },
        )
        done = await _next_event(events, event_name="job_done")
    finally:
        await events.aclose()
        await client.disconnect()

    assert done["ok"] is False
    assert done["error_kind"] == "feature-unavailable"
    assert done["reason"] == "uv-required"
    assert "requires uv" in done["detail"]
    assert not (builds_root / "01NIGHTLY").exists()
    json.dumps(done)


@pytest.mark.asyncio
async def test_agent_create_build_nightly_uses_uv_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builds_root = tmp_path / "data" / "vllm-loader" / "builds"
    uv_path = "/opt/bin/uv"
    command_calls: list[list[str]] = []

    async def fake_build_subprocess_exec(
        argv: list[str],
        *,
        env: dict[str, str],
        cwd: Path,
        emit,
        phase: str,
        cancel_event: asyncio.Event,
    ) -> int:
        del env, cwd, cancel_event
        command_calls.append(list(argv))
        if argv[:2] == [uv_path, "venv"]:
            venv_dir = Path(argv[-1])
            bin_dir = venv_dir / "bin"
            bin_dir.mkdir(parents=True)
            (bin_dir / "python").write_text("#!/bin/sh\n", encoding="utf-8")
            (bin_dir / "pip").write_text("#!/bin/sh\n", encoding="utf-8")
            (bin_dir / "vllm").write_text("#!/bin/sh\n", encoding="utf-8")
            (bin_dir / "activate").write_text("# activate\n", encoding="utf-8")
        emit(
            {
                "kind": "committed",
                "text": f"{phase}: {' '.join(argv)}",
                "level": "INFO",
                "phase": phase,
            }
        )
        return 0

    monkeypatch.setattr(local_agent_module, "_find_uv_executable", lambda: uv_path, raising=False)
    monkeypatch.setattr(
        local_agent_module,
        "_build_subprocess_exec",
        fake_build_subprocess_exec,
        raising=False,
    )
    monkeypatch.setattr(
        local_agent_module,
        "_managed_build_resolved_versions",
        lambda _venv_path: {
            "vllm": "0.17.0.dev",
            "vllm_version_profile": "current",
            "python": "3.12.7",
        },
        raising=False,
    )

    client = InProcessTargetClient(LocalAgent(builds_root=builds_root))
    await client.connect()
    events = client.subscribe(["job-build-nightly"], resume_from="live")
    try:
        await client.call(
            "create_build",
            {
                "job_id": "job-build-nightly",
                "method": "nightly",
                "build_id": "01NIGHTLY",
                "label": "nightly-cu130",
                "channel": "cu130",
            },
        )
        done = await _next_event(events, event_name="job_done")
    finally:
        await events.aclose()
        await client.disconnect()

    build_dir = builds_root / "01NIGHTLY"
    manifest = json.loads((build_dir / "build.json").read_text(encoding="utf-8"))
    assert done["ok"] is True
    assert manifest["install"]["method"] == "nightly"
    assert manifest["install"]["installer"] == "uv"
    assert manifest["install"]["provenance"]["nightly_channel"] == "cu130"
    assert manifest["install"]["provenance"]["index_url"] == (
        "https://wheels.vllm.ai/nightly/cu130"
    )
    assert command_calls == [
        [uv_path, "venv", str(build_dir / "venv")],
        [
            uv_path,
            "pip",
            "install",
            "--python",
            str(build_dir / "venv" / "bin" / "python"),
            "-U",
            "vllm",
            "--torch-backend=auto",
            "--extra-index-url",
            "https://wheels.vllm.ai/nightly/cu130",
        ],
    ]
    json.dumps(done)


@pytest.mark.asyncio
async def test_agent_create_build_commit_requires_uv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builds_root = tmp_path / "data" / "vllm-loader" / "builds"
    monkeypatch.setattr(local_agent_module, "_find_uv_executable", lambda: None, raising=False)

    client = InProcessTargetClient(LocalAgent(builds_root=builds_root))
    await client.connect()
    events = client.subscribe(["job-build-commit-no-uv"], resume_from="live")
    try:
        await client.call(
            "create_build",
            {
                "job_id": "job-build-commit-no-uv",
                "method": "commit",
                "build_id": "01COMMIT",
                "commit": "0123456789abcdef0123456789abcdef01234567",
                "channel": "cu130",
            },
        )
        done = await _next_event(events, event_name="job_done")
    finally:
        await events.aclose()
        await client.disconnect()

    assert done["ok"] is False
    assert done["error_kind"] == "feature-unavailable"
    assert done["reason"] == "uv-required"
    assert "requires uv" in done["detail"]
    assert not (builds_root / "01COMMIT").exists()
    json.dumps(done)


@pytest.mark.asyncio
async def test_agent_create_build_commit_uses_uv_commit_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builds_root = tmp_path / "data" / "vllm-loader" / "builds"
    uv_path = "/opt/bin/uv"
    commit_sha = "0123456789abcdef0123456789abcdef01234567"
    command_calls: list[list[str]] = []

    async def fake_build_subprocess_exec(
        argv: list[str],
        *,
        env: dict[str, str],
        cwd: Path,
        emit,
        phase: str,
        cancel_event: asyncio.Event,
    ) -> int:
        del env, cwd, cancel_event
        command_calls.append(list(argv))
        if argv[:2] == [uv_path, "venv"]:
            venv_dir = Path(argv[-1])
            bin_dir = venv_dir / "bin"
            bin_dir.mkdir(parents=True)
            (bin_dir / "python").write_text("#!/bin/sh\n", encoding="utf-8")
            (bin_dir / "pip").write_text("#!/bin/sh\n", encoding="utf-8")
            (bin_dir / "vllm").write_text("#!/bin/sh\n", encoding="utf-8")
            (bin_dir / "activate").write_text("# activate\n", encoding="utf-8")
        emit(
            {
                "kind": "committed",
                "text": f"{phase}: {' '.join(argv)}",
                "level": "INFO",
                "phase": phase,
            }
        )
        return 0

    monkeypatch.setattr(local_agent_module, "_find_uv_executable", lambda: uv_path, raising=False)
    monkeypatch.setattr(
        local_agent_module,
        "_build_subprocess_exec",
        fake_build_subprocess_exec,
        raising=False,
    )
    monkeypatch.setattr(
        local_agent_module,
        "_managed_build_resolved_versions",
        lambda _venv_path: {
            "vllm": "0.17.0.dev",
            "vllm_version_profile": "current",
            "python": "3.12.7",
        },
        raising=False,
    )

    client = InProcessTargetClient(LocalAgent(builds_root=builds_root))
    await client.connect()
    events = client.subscribe(["job-build-commit"], resume_from="live")
    try:
        await client.call(
            "create_build",
            {
                "job_id": "job-build-commit",
                "method": "commit",
                "build_id": "01COMMIT",
                "label": "commit-cu130",
                "commit": commit_sha,
                "channel": "cu130",
            },
        )
        done = await _next_event(events, event_name="job_done")
    finally:
        await events.aclose()
        await client.disconnect()

    build_dir = builds_root / "01COMMIT"
    manifest = json.loads((build_dir / "build.json").read_text(encoding="utf-8"))
    index_url = f"https://wheels.vllm.ai/{commit_sha}/cu130"
    assert done["ok"] is True
    assert manifest["install"]["method"] == "commit"
    assert manifest["install"]["installer"] == "uv"
    assert manifest["install"]["provenance"]["vllm_commit"] == commit_sha
    assert manifest["install"]["provenance"]["index_url"] == index_url
    assert command_calls == [
        [uv_path, "venv", str(build_dir / "venv")],
        [
            uv_path,
            "pip",
            "install",
            "--python",
            str(build_dir / "venv" / "bin" / "python"),
            "vllm",
            "--torch-backend=auto",
            "--extra-index-url",
            index_url,
        ],
    ]
    json.dumps(done)


@pytest.mark.asyncio
async def test_agent_create_build_wheel_requires_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builds_root = tmp_path / "data" / "vllm-loader" / "builds"
    missing_wheel = tmp_path / "wheels" / "vllm-0.11.2.whl"
    monkeypatch.setattr(
        local_agent_module,
        "_find_uv_executable",
        lambda: "/opt/bin/uv",
        raising=False,
    )

    client = InProcessTargetClient(LocalAgent(builds_root=builds_root))
    await client.connect()
    events = client.subscribe(["job-build-wheel-missing"], resume_from="live")
    try:
        await client.call(
            "create_build",
            {
                "job_id": "job-build-wheel-missing",
                "method": "wheel",
                "build_id": "01WHEEL",
                "path": str(missing_wheel),
            },
        )
        done = await _next_event(events, event_name="job_done")
    finally:
        await events.aclose()
        await client.disconnect()

    assert done["ok"] is False
    assert done["error_kind"] == "invalid-config"
    assert done["reason"] == "missing-wheel"
    assert done["path"] == str(missing_wheel)
    assert not (builds_root / "01WHEEL").exists()
    json.dumps(done)


@pytest.mark.asyncio
async def test_agent_create_build_wheel_uses_uv_with_extra_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builds_root = tmp_path / "data" / "vllm-loader" / "builds"
    uv_path = "/opt/bin/uv"
    wheel_path = tmp_path / "wheels" / "vllm-0.11.2+cu130.whl"
    wheel_path.parent.mkdir(parents=True)
    wheel_path.write_text("wheel", encoding="utf-8")
    torch_index = "https://download.pytorch.org/whl/cu130"
    command_calls: list[list[str]] = []

    async def fake_build_subprocess_exec(
        argv: list[str],
        *,
        env: dict[str, str],
        cwd: Path,
        emit,
        phase: str,
        cancel_event: asyncio.Event,
    ) -> int:
        del env, cwd, cancel_event
        command_calls.append(list(argv))
        if argv[:2] == [uv_path, "venv"]:
            venv_dir = Path(argv[-1])
            bin_dir = venv_dir / "bin"
            bin_dir.mkdir(parents=True)
            (bin_dir / "python").write_text("#!/bin/sh\n", encoding="utf-8")
            (bin_dir / "pip").write_text("#!/bin/sh\n", encoding="utf-8")
            (bin_dir / "vllm").write_text("#!/bin/sh\n", encoding="utf-8")
            (bin_dir / "activate").write_text("# activate\n", encoding="utf-8")
        emit(
            {
                "kind": "committed",
                "text": f"{phase}: {' '.join(argv)}",
                "level": "INFO",
                "phase": phase,
            }
        )
        return 0

    monkeypatch.setattr(local_agent_module, "_find_uv_executable", lambda: uv_path, raising=False)
    monkeypatch.setattr(
        local_agent_module,
        "_build_subprocess_exec",
        fake_build_subprocess_exec,
        raising=False,
    )
    monkeypatch.setattr(
        local_agent_module,
        "_managed_build_resolved_versions",
        lambda _venv_path: {
            "vllm": "0.11.2",
            "vllm_version_profile": "current",
            "python": "3.12.7",
        },
        raising=False,
    )

    client = InProcessTargetClient(LocalAgent(builds_root=builds_root))
    await client.connect()
    events = client.subscribe(["job-build-wheel"], resume_from="live")
    try:
        await client.call(
            "create_build",
            {
                "job_id": "job-build-wheel",
                "method": "wheel",
                "build_id": "01WHEEL",
                "label": "wheel-cu130",
                "path": str(wheel_path),
                "extra_index_url": torch_index,
            },
        )
        done = await _next_event(events, event_name="job_done")
    finally:
        await events.aclose()
        await client.disconnect()

    build_dir = builds_root / "01WHEEL"
    manifest = json.loads((build_dir / "build.json").read_text(encoding="utf-8"))
    assert done["ok"] is True
    assert manifest["install"]["method"] == "wheel"
    assert manifest["install"]["installer"] == "uv"
    assert manifest["install"]["provenance"]["local_wheel_path"] == str(wheel_path)
    assert manifest["install"]["provenance"]["index_url"] == torch_index
    assert command_calls == [
        [uv_path, "venv", str(build_dir / "venv")],
        [
            uv_path,
            "pip",
            "install",
            "--python",
            str(build_dir / "venv" / "bin" / "python"),
            str(wheel_path),
            "--torch-backend=auto",
            "--extra-index-url",
            torch_index,
        ],
    ]
    json.dumps(done)


@pytest.mark.asyncio
async def test_agent_create_build_wheel_falls_back_to_pip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builds_root = tmp_path / "data" / "vllm-loader" / "builds"
    wheel_path = tmp_path / "wheels" / "vllm-0.11.2.whl"
    wheel_path.parent.mkdir(parents=True)
    wheel_path.write_text("wheel", encoding="utf-8")
    command_calls: list[list[str]] = []

    async def fake_build_subprocess_exec(
        argv: list[str],
        *,
        env: dict[str, str],
        cwd: Path,
        emit,
        phase: str,
        cancel_event: asyncio.Event,
    ) -> int:
        del env, cwd, cancel_event
        command_calls.append(list(argv))
        if argv[1:3] == ["-m", "venv"]:
            venv_dir = Path(argv[-1])
            bin_dir = venv_dir / "bin"
            bin_dir.mkdir(parents=True)
            (bin_dir / "python").write_text("#!/bin/sh\n", encoding="utf-8")
            (bin_dir / "pip").write_text("#!/bin/sh\n", encoding="utf-8")
            (bin_dir / "vllm").write_text("#!/bin/sh\n", encoding="utf-8")
            (bin_dir / "activate").write_text("# activate\n", encoding="utf-8")
        emit(
            {
                "kind": "committed",
                "text": f"{phase}: {' '.join(argv)}",
                "level": "INFO",
                "phase": phase,
            }
        )
        return 0

    monkeypatch.setattr(local_agent_module, "_find_uv_executable", lambda: None, raising=False)
    monkeypatch.setattr(
        local_agent_module,
        "_build_subprocess_exec",
        fake_build_subprocess_exec,
        raising=False,
    )
    monkeypatch.setattr(
        local_agent_module,
        "_managed_build_resolved_versions",
        lambda _venv_path: {
            "vllm": "0.11.2",
            "vllm_version_profile": "current",
            "python": "3.12.7",
        },
        raising=False,
    )

    client = InProcessTargetClient(LocalAgent(builds_root=builds_root))
    await client.connect()
    events = client.subscribe(["job-build-wheel-pip"], resume_from="live")
    try:
        await client.call(
            "create_build",
            {
                "job_id": "job-build-wheel-pip",
                "method": "wheel",
                "build_id": "01WHEELPIP",
                "path": str(wheel_path),
            },
        )
        done = await _next_event(events, event_name="job_done")
    finally:
        await events.aclose()
        await client.disconnect()

    build_dir = builds_root / "01WHEELPIP"
    manifest = json.loads((build_dir / "build.json").read_text(encoding="utf-8"))
    assert done["ok"] is True
    assert manifest["install"]["method"] == "wheel"
    assert manifest["install"]["installer"] == "pip"
    assert manifest["install"]["provenance"]["local_wheel_path"] == str(wheel_path)
    assert command_calls == [
        [sys.executable, "-m", "venv", str(build_dir / "venv")],
        [
            str(build_dir / "venv" / "bin" / "python"),
            "-m",
            "pip",
            "install",
            str(wheel_path),
        ],
    ]
    json.dumps(done)


@pytest.mark.asyncio
async def test_agent_create_build_git_requires_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builds_root = tmp_path / "data" / "vllm-loader" / "builds"
    monkeypatch.setattr(
        local_agent_module,
        "_find_uv_executable",
        lambda: "/opt/bin/uv",
        raising=False,
    )

    client = InProcessTargetClient(LocalAgent(builds_root=builds_root))
    await client.connect()
    events = client.subscribe(["job-build-git-missing-url"], resume_from="live")
    try:
        await client.call(
            "create_build",
            {
                "job_id": "job-build-git-missing-url",
                "method": "git",
                "build_id": "01GIT",
                "ref": "main",
            },
        )
        done = await _next_event(events, event_name="job_done")
    finally:
        await events.aclose()
        await client.disconnect()

    assert done["ok"] is False
    assert done["error_kind"] == "invalid-params"
    assert done["reason"] == "missing-git-url"
    assert not (builds_root / "01GIT").exists()
    json.dumps(done)


@pytest.mark.asyncio
async def test_agent_create_build_git_clones_and_installs_with_uv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builds_root = tmp_path / "data" / "vllm-loader" / "builds"
    uv_path = "/opt/bin/uv"
    git_url = "https://github.com/vllm-project/vllm.git"
    git_ref = "abc123"
    command_calls: list[list[str]] = []

    async def fake_build_subprocess_exec(
        argv: list[str],
        *,
        env: dict[str, str],
        cwd: Path,
        emit,
        phase: str,
        cancel_event: asyncio.Event,
    ) -> int:
        del env, cwd, cancel_event
        command_calls.append(list(argv))
        if argv[:2] == [uv_path, "venv"]:
            venv_dir = Path(argv[-1])
            bin_dir = venv_dir / "bin"
            bin_dir.mkdir(parents=True)
            (bin_dir / "python").write_text("#!/bin/sh\n", encoding="utf-8")
            (bin_dir / "pip").write_text("#!/bin/sh\n", encoding="utf-8")
            (bin_dir / "vllm").write_text("#!/bin/sh\n", encoding="utf-8")
            (bin_dir / "activate").write_text("# activate\n", encoding="utf-8")
        if argv[:2] == ["git", "clone"]:
            Path(argv[-1]).mkdir(parents=True)
        emit(
            {
                "kind": "committed",
                "text": f"{phase}: {' '.join(argv)}",
                "level": "INFO",
                "phase": phase,
            }
        )
        return 0

    monkeypatch.setattr(local_agent_module, "_find_uv_executable", lambda: uv_path, raising=False)
    monkeypatch.setattr(
        local_agent_module,
        "_build_subprocess_exec",
        fake_build_subprocess_exec,
        raising=False,
    )
    monkeypatch.setattr(
        local_agent_module,
        "_managed_build_resolved_versions",
        lambda _venv_path: {
            "vllm": "0.17.0.dev",
            "vllm_version_profile": "current",
            "python": "3.12.7",
        },
        raising=False,
    )

    client = InProcessTargetClient(LocalAgent(builds_root=builds_root))
    await client.connect()
    events = client.subscribe(["job-build-git"], resume_from="live")
    try:
        await client.call(
            "create_build",
            {
                "job_id": "job-build-git",
                "method": "git",
                "build_id": "01GIT",
                "label": "source-build",
                "url": git_url,
                "ref": git_ref,
                "python": "3.12",
            },
        )
        done = await _next_event(events, event_name="job_done")
    finally:
        await events.aclose()
        await client.disconnect()

    build_dir = builds_root / "01GIT"
    source_dir = build_dir / "source"
    manifest = json.loads((build_dir / "build.json").read_text(encoding="utf-8"))
    assert done["ok"] is True
    assert manifest["install"]["method"] == "git"
    assert manifest["install"]["installer"] == "uv"
    assert manifest["install"]["provenance"]["git_url"] == git_url
    assert manifest["install"]["provenance"]["git_ref"] == git_ref
    assert command_calls == [
        [uv_path, "venv", "--python", "3.12", str(build_dir / "venv")],
        ["git", "clone", git_url, str(source_dir)],
        ["git", "-C", str(source_dir), "checkout", git_ref],
        [
            uv_path,
            "pip",
            "install",
            "--python",
            str(build_dir / "venv" / "bin" / "python"),
            "-e",
            str(source_dir),
            "--torch-backend=auto",
        ],
    ]
    json.dumps(done)


@pytest.mark.asyncio
async def test_agent_create_build_git_falls_back_to_pip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builds_root = tmp_path / "data" / "vllm-loader" / "builds"
    git_url = "https://github.com/vllm-project/vllm.git"
    command_calls: list[list[str]] = []

    async def fake_build_subprocess_exec(
        argv: list[str],
        *,
        env: dict[str, str],
        cwd: Path,
        emit,
        phase: str,
        cancel_event: asyncio.Event,
    ) -> int:
        del env, cwd, cancel_event
        command_calls.append(list(argv))
        if argv[1:3] == ["-m", "venv"]:
            venv_dir = Path(argv[-1])
            bin_dir = venv_dir / "bin"
            bin_dir.mkdir(parents=True)
            (bin_dir / "python").write_text("#!/bin/sh\n", encoding="utf-8")
            (bin_dir / "pip").write_text("#!/bin/sh\n", encoding="utf-8")
            (bin_dir / "vllm").write_text("#!/bin/sh\n", encoding="utf-8")
            (bin_dir / "activate").write_text("# activate\n", encoding="utf-8")
        if argv[:2] == ["git", "clone"]:
            Path(argv[-1]).mkdir(parents=True)
        emit(
            {
                "kind": "committed",
                "text": f"{phase}: {' '.join(argv)}",
                "level": "INFO",
                "phase": phase,
            }
        )
        return 0

    monkeypatch.setattr(local_agent_module, "_find_uv_executable", lambda: None, raising=False)
    monkeypatch.setattr(
        local_agent_module,
        "_build_subprocess_exec",
        fake_build_subprocess_exec,
        raising=False,
    )
    monkeypatch.setattr(
        local_agent_module,
        "_managed_build_resolved_versions",
        lambda _venv_path: {
            "vllm": "0.17.0.dev",
            "vllm_version_profile": "current",
            "python": "3.12.7",
        },
        raising=False,
    )

    client = InProcessTargetClient(LocalAgent(builds_root=builds_root))
    await client.connect()
    events = client.subscribe(["job-build-git-pip"], resume_from="live")
    try:
        await client.call(
            "create_build",
            {
                "job_id": "job-build-git-pip",
                "method": "git",
                "build_id": "01GITPIP",
                "url": git_url,
                "precompiled": True,
            },
        )
        done = await _next_event(events, event_name="job_done")
    finally:
        await events.aclose()
        await client.disconnect()

    build_dir = builds_root / "01GITPIP"
    source_dir = build_dir / "source"
    manifest = json.loads((build_dir / "build.json").read_text(encoding="utf-8"))
    assert done["ok"] is True
    assert manifest["install"]["method"] == "git"
    assert manifest["install"]["installer"] == "pip"
    assert manifest["install"]["provenance"]["precompiled"] is True
    assert manifest["install"]["provenance"]["env_overrides"] == {
        "VLLM_USE_PRECOMPILED": "1"
    }
    assert command_calls == [
        [sys.executable, "-m", "venv", str(build_dir / "venv")],
        ["git", "clone", git_url, str(source_dir)],
        [
            str(build_dir / "venv" / "bin" / "python"),
            "-m",
            "pip",
            "install",
            "-e",
            str(source_dir),
        ],
    ]
    json.dumps(done)


@pytest.mark.asyncio
async def test_agent_create_build_pip_job_scrubs_output_before_wire_and_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builds_root = tmp_path / "data" / "vllm-loader" / "builds"
    index_url = "https://user:build-token-12345@packages.example/simple"

    async def fake_build_subprocess_exec(
        argv: list[str],
        *,
        env: dict[str, str],
        cwd: Path,
        emit,
        phase: str,
        cancel_event: asyncio.Event,
    ) -> int:
        del env, cwd, cancel_event
        if argv[1:3] == ["-m", "venv"]:
            venv_dir = Path(argv[-1])
            bin_dir = venv_dir / "bin"
            bin_dir.mkdir(parents=True)
            (bin_dir / "python").write_text("#!/bin/sh\n", encoding="utf-8")
            (bin_dir / "pip").write_text("#!/bin/sh\n", encoding="utf-8")
            (bin_dir / "vllm").write_text("#!/bin/sh\n", encoding="utf-8")
            (bin_dir / "activate").write_text("# activate\n", encoding="utf-8")
        text = f"{phase}: pip output from {index_url}"
        emit({"kind": "committed", "text": text, "level": "INFO", "phase": phase})
        return 0

    def fake_resolve_versions(_venv_path: Path) -> dict[str, str]:
        return {
            "vllm": "0.11.2",
            "vllm_version_profile": "current",
            "python": "3.12.7",
        }

    monkeypatch.setattr(
        local_agent_module,
        "_build_subprocess_exec",
        fake_build_subprocess_exec,
        raising=False,
    )
    monkeypatch.setattr(
        local_agent_module,
        "_managed_build_resolved_versions",
        fake_resolve_versions,
        raising=False,
    )

    client = InProcessTargetClient(LocalAgent(builds_root=builds_root))
    await client.connect()
    events = client.subscribe(["job-build-scrub"], resume_from="live")
    try:
        await client.call(
            "create_build",
            {
                "job_id": "job-build-scrub",
                "method": "pip",
                "build_id": "01SCRUB",
                "spec": "vllm==0.11.2",
                "env": [f"PIP_INDEX_URL={index_url}"],
            },
        )
        progress = []
        done = None
        for _ in range(6):
            event = await asyncio.wait_for(events.__anext__(), timeout=2)
            if event.get("event") == "job_done":
                done = event
                break
            progress.append(event)
    finally:
        await events.aclose()
        await client.disconnect()

    assert done is not None
    build_dir = builds_root / "01SCRUB"
    log_text = (build_dir / "install.log").read_text(encoding="utf-8")
    wire_text = json.dumps({"progress": progress, "done": done}, ensure_ascii=False)
    manifest_text = (build_dir / "build.json").read_text(encoding="utf-8")
    assert "build-token-12345" not in wire_text
    assert "build-token-12345" not in log_text
    assert "build-token-12345" not in manifest_text
    assert "••••" in wire_text
    assert "••••" in log_text
    assert done["ok"] is True
    json.dumps(done)


@pytest.mark.asyncio
async def test_agent_create_build_pip_job_marks_failed_when_verify_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builds_root = tmp_path / "data" / "vllm-loader" / "builds"

    async def fake_build_subprocess_exec(
        argv: list[str],
        *,
        env: dict[str, str],
        cwd: Path,
        emit,
        phase: str,
        cancel_event: asyncio.Event,
    ) -> int:
        del env, cwd, cancel_event
        if argv[1:3] == ["-m", "venv"]:
            venv_dir = Path(argv[-1])
            bin_dir = venv_dir / "bin"
            bin_dir.mkdir(parents=True)
            (bin_dir / "python").write_text("#!/bin/sh\n", encoding="utf-8")
            (bin_dir / "pip").write_text("#!/bin/sh\n", encoding="utf-8")
            (bin_dir / "vllm").write_text("#!/bin/sh\n", encoding="utf-8")
            (bin_dir / "activate").write_text("# activate\n", encoding="utf-8")
        emit(
            {
                "kind": "committed",
                "text": f"{phase}: {' '.join(argv)}",
                "level": "INFO",
                "phase": phase,
            }
        )
        return 0

    def failing_resolve_versions(_venv_path: Path) -> dict[str, str]:
        raise local_agent_module.BuildRegistryError(
            "invalid-config",
            "vLLM import failed",
            {"reason": "vllm-import-failed"},
        )

    monkeypatch.setattr(
        local_agent_module,
        "_build_subprocess_exec",
        fake_build_subprocess_exec,
        raising=False,
    )
    monkeypatch.setattr(
        local_agent_module,
        "_managed_build_resolved_versions",
        failing_resolve_versions,
        raising=False,
    )

    client = InProcessTargetClient(LocalAgent(builds_root=builds_root))
    await client.connect()
    events = client.subscribe(["job-build-verify-fail"], resume_from="live")
    try:
        await client.call(
            "create_build",
            {
                "job_id": "job-build-verify-fail",
                "method": "pip",
                "build_id": "01VERIFYFAIL",
                "spec": "vllm==0.11.2",
            },
        )
        done = await _next_event(events, event_name="job_done")
    finally:
        await events.aclose()
        await client.disconnect()

    build_dir = builds_root / "01VERIFYFAIL"
    manifest = json.loads((build_dir / "build.json").read_text(encoding="utf-8"))
    assert done["ok"] is False
    assert done["error_kind"] == "invalid-config"
    assert done["detail"] == "vLLM import failed"
    assert done["reason"] == "vllm-import-failed"
    assert done["build_id"] == "01VERIFYFAIL"
    assert done["status"] == "failed"
    assert manifest["status"] == "failed"
    assert manifest["install"]["exit_code"] == 0
    assert manifest["paths"]["root"] == str(build_dir)
    json.dumps(done)


@pytest.mark.asyncio
async def test_build_subprocess_exec_derives_install_phases_from_output(
    tmp_path: Path,
) -> None:
    installer = tmp_path / "fake_installer.py"
    installer.write_text(
        "\n".join(
            [
                "print('Collecting vllm==0.11.2')",
                "print('Downloading vllm-0.11.2.whl')",
                "print('Building wheel for vllm (pyproject.toml)')",
                "print('ninja: build stopped: subcommand failed')",
                "print('Successfully installed vllm-0.11.2')",
                "",
            ]
        ),
        encoding="utf-8",
    )
    events: list[dict[str, object]] = []

    exit_code = await local_agent_module._build_subprocess_exec(
        [sys.executable, str(installer)],
        env=dict(os.environ),
        cwd=tmp_path,
        emit=events.append,
        phase="INSTALLING",
        cancel_event=asyncio.Event(),
    )

    assert exit_code == 0
    assert [(event["text"], event["phase"]) for event in events] == [
        ("Collecting vllm==0.11.2", "DOWNLOADING"),
        ("Downloading vllm-0.11.2.whl", "DOWNLOADING"),
        ("Building wheel for vllm (pyproject.toml)", "BUILDING"),
        ("ninja: build stopped: subcommand failed", "BUILDING"),
        ("Successfully installed vllm-0.11.2", "INSTALLING"),
    ]
    json.dumps(events)


@pytest.mark.asyncio
async def test_build_subprocess_exec_terminates_child_on_task_cancel(
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "installer.pid"
    installer = tmp_path / "slow_installer.py"
    installer.write_text(
        "\n".join(
            [
                "import os",
                "import time",
                f"{str(pid_file)!r} and open({str(pid_file)!r}, 'w').write(str(os.getpid()))",
                "print('started', flush=True)",
                "time.sleep(60)",
                "",
            ]
        ),
        encoding="utf-8",
    )
    started = asyncio.Event()

    def emit(event: dict[str, object]) -> None:
        if event.get("text") == "started":
            started.set()

    task = asyncio.create_task(
        local_agent_module._build_subprocess_exec(
            [sys.executable, str(installer)],
            env=dict(os.environ),
            cwd=tmp_path,
            emit=emit,
            phase="INSTALLING",
            cancel_event=asyncio.Event(),
        )
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    pid = int(pid_file.read_text(encoding="utf-8"))
    try:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
        for _ in range(20):
            if not _pid_alive(pid):
                break
            await asyncio.sleep(0.05)
        assert not _pid_alive(pid)
    finally:
        if _pid_alive(pid):
            os.kill(pid, signal.SIGKILL)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.asyncio
async def test_agent_create_build_pip_cancel_marks_failed_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builds_root = tmp_path / "data" / "vllm-loader" / "builds"
    install_started = asyncio.Event()

    async def fake_build_subprocess_exec(
        argv: list[str],
        *,
        env: dict[str, str],
        cwd: Path,
        emit,
        phase: str,
        cancel_event: asyncio.Event,
    ) -> int:
        del env, cwd, cancel_event
        if argv[1:3] == ["-m", "venv"]:
            venv_dir = Path(argv[-1])
            bin_dir = venv_dir / "bin"
            bin_dir.mkdir(parents=True)
            (bin_dir / "python").write_text("#!/bin/sh\n", encoding="utf-8")
            (bin_dir / "pip").write_text("#!/bin/sh\n", encoding="utf-8")
            (bin_dir / "vllm").write_text("#!/bin/sh\n", encoding="utf-8")
            (bin_dir / "activate").write_text("# activate\n", encoding="utf-8")
            return 0
        emit({"kind": "committed", "text": "Collecting vllm", "phase": phase})
        install_started.set()
        await asyncio.Event().wait()
        return 0

    monkeypatch.setattr(local_agent_module, "_find_uv_executable", lambda: None, raising=False)
    monkeypatch.setattr(
        local_agent_module,
        "_build_subprocess_exec",
        fake_build_subprocess_exec,
        raising=False,
    )

    client = InProcessTargetClient(LocalAgent(builds_root=builds_root))
    await client.connect()
    events = client.subscribe(["job-build-cancel"], resume_from="live")
    try:
        await client.call(
            "create_build",
            {
                "job_id": "job-build-cancel",
                "method": "pip",
                "build_id": "01CANCEL",
                "spec": "vllm==0.11.2",
            },
        )
        await asyncio.wait_for(install_started.wait(), timeout=2)
        cancel_result = await client.call("cancel_job", {"job_id": "job-build-cancel"})
        done = await _next_event(events, event_name="job_done")
    finally:
        await events.aclose()
        await client.disconnect()

    manifest = json.loads((builds_root / "01CANCEL" / "build.json").read_text())
    assert cancel_result == {
        "job_id": "job-build-cancel",
        "cancelled": True,
        "status": "cancelled",
    }
    assert done["event"] == "job_done"
    assert done["ok"] is False
    assert done["error_kind"] == "cancelled"
    assert done["detail"] == "build install cancelled"
    assert done["build_id"] == "01CANCEL"
    assert done["status"] == "failed"
    assert manifest["status"] == "failed"
    assert manifest["install"]["exit_code"] == 130
    assert manifest["install"]["cancelled"] is True
    json.dumps(done)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("installer_line", "expected_kind"),
    [
        (
            "ERROR: Could not install packages due to an OSError: "
            "[Errno 28] No space left on device",
            "disk-full",
        ),
        (
            "ERROR: Could not find a version that satisfies the requirement vllm==9.9",
            "package-not-found",
        ),
        (
            "HTTPSConnectionPool: Read timed out while downloading wheel",
            "network",
        ),
        (
            "403 Client Error: Forbidden for url https://packages.example/simple",
            "auth",
        ),
        (
            "ninja: build stopped: subcommand failed",
            "build-failed",
        ),
    ],
)
async def test_agent_create_build_pip_job_classifies_install_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    installer_line: str,
    expected_kind: str,
) -> None:
    builds_root = tmp_path / "data" / "vllm-loader" / "builds"

    async def fake_build_subprocess_exec(
        argv: list[str],
        *,
        env: dict[str, str],
        cwd: Path,
        emit,
        phase: str,
        cancel_event: asyncio.Event,
    ) -> int:
        del env, cwd, cancel_event
        if argv[1:3] == ["-m", "venv"]:
            venv_dir = Path(argv[-1])
            bin_dir = venv_dir / "bin"
            bin_dir.mkdir(parents=True)
            (bin_dir / "python").write_text("#!/bin/sh\n", encoding="utf-8")
            (bin_dir / "pip").write_text("#!/bin/sh\n", encoding="utf-8")
            (bin_dir / "vllm").write_text("#!/bin/sh\n", encoding="utf-8")
            (bin_dir / "activate").write_text("# activate\n", encoding="utf-8")
            return 0
        emit(
            {
                "kind": "committed",
                "text": installer_line,
                "level": "ERROR",
                "phase": phase,
            }
        )
        return 1

    monkeypatch.setattr(local_agent_module, "_find_uv_executable", lambda: None, raising=False)
    monkeypatch.setattr(
        local_agent_module,
        "_build_subprocess_exec",
        fake_build_subprocess_exec,
        raising=False,
    )

    client = InProcessTargetClient(LocalAgent(builds_root=builds_root))
    await client.connect()
    events = client.subscribe(["job-build-install-fail"], resume_from="live")
    try:
        await client.call(
            "create_build",
            {
                "job_id": "job-build-install-fail",
                "method": "pip",
                "build_id": "01INSTALLFAIL",
                "spec": "vllm==9.9",
            },
        )
        done = await _next_event(events, event_name="job_done")
    finally:
        await events.aclose()
        await client.disconnect()

    manifest = json.loads(
        (builds_root / "01INSTALLFAIL" / "build.json").read_text(encoding="utf-8")
    )
    assert done["ok"] is False
    assert done["error_kind"] == expected_kind
    assert done["status"] == "failed"
    assert manifest["status"] == "failed"
    assert manifest["install"]["exit_code"] == 1
    json.dumps(done)


@pytest.mark.asyncio
async def test_agent_download_model_job_streams_by_job_id() -> None:
    async def model_job_runner(params, emit, cancel_event) -> dict[str, object]:
        assert params["job_id"] == "job-model-1"
        assert cancel_event.is_set() is False
        emit(
            {
                "kind": "committed",
                "text": "Resolving model",
                "level": "INFO",
                "phase": "RESOLVING",
            }
        )
        return {"ok": True, "detail": "model cached"}

    client = InProcessTargetClient(LocalAgent(model_job_runner=model_job_runner))
    await client.connect()
    events = client.subscribe(["job-model-1"], resume_from="live")
    try:
        result = await client.call(
            "download_model",
            {"job_id": "job-model-1", "model_ref": "01MODEL"},
        )
        progress = await asyncio.wait_for(events.__anext__(), timeout=2)
        done = await asyncio.wait_for(events.__anext__(), timeout=2)
    finally:
        await events.aclose()
        await client.disconnect()

    assert result == {
        "job_id": "job-model-1",
        "kind": "download_model",
        "status": "running",
    }
    assert progress["event"] == "job_progress"
    assert progress["job_id"] == "job-model-1"
    assert "run_id" not in progress
    assert progress["text"] == "Resolving model"
    assert done["event"] == "job_done"
    assert done["job_id"] == "job-model-1"
    assert done["ok"] is True
    assert done["detail"] == "model cached"
    json.dumps(progress)
    json.dumps(done)


@pytest.mark.asyncio
async def test_agent_download_model_job_scrubs_progress_before_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HF_TOKEN", "model-token-12345")

    async def model_job_runner(_params, emit, _cancel_event) -> dict[str, object]:
        emit(
            {
                "kind": "committed",
                "text": "Downloading with token model-token-12345",
                "level": "INFO",
                "phase": "DOWNLOADING",
            }
        )
        return {"ok": True, "detail": "model-token-12345 finished"}

    client = InProcessTargetClient(LocalAgent(model_job_runner=model_job_runner))
    await client.connect()
    events = client.subscribe(["job-model-scrub"], resume_from="live")
    try:
        await client.call(
            "download_model",
            {"job_id": "job-model-scrub", "model_ref": "01MODEL"},
        )
        progress = await asyncio.wait_for(events.__anext__(), timeout=2)
        done = await asyncio.wait_for(events.__anext__(), timeout=2)
    finally:
        await events.aclose()
        await client.disconnect()

    wire_text = json.dumps({"progress": progress, "done": done}, ensure_ascii=False)
    assert "model-token-12345" not in wire_text
    assert "••••" in wire_text
    assert progress["text"] == "Downloading with token ••••"
    assert done["detail"] == "•••• finished"
    json.dumps(done)


@pytest.mark.asyncio
async def test_agent_download_model_job_verifies_cached_model_entry(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "state" / "vllm-loader" / "models" / "registry.json"
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "default_cache": "hf",
                "app_download_dir": None,
                "entries": [
                    {
                        "entry_id": "01MODEL",
                        "display_name": "llama-pin",
                        "source": "hf_repo",
                        "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
                        "revision": "main",
                        "commit_sha": "abc123",
                        "local_path": None,
                        "url": None,
                        "quant_format": "none",
                        "tokenizer": None,
                        "files": {
                            "count": 3,
                            "total_bytes": 130,
                            "weights_format": "safetensors",
                        },
                        "size_bytes": 130,
                        "cache_state": "cached",
                        "gated": False,
                        "token_required": False,
                        "created_at": "2026-06-03T00:00:00Z",
                        "last_used_at": None,
                        "notes": "",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    client = InProcessTargetClient(LocalAgent(models_registry_path=registry_path))
    await client.connect()
    events = client.subscribe(["job-model-cached"], resume_from="live")
    try:
        result = await client.call(
            "download_model",
            {"job_id": "job-model-cached", "model_ref": "01MODEL"},
        )
        progress = await asyncio.wait_for(events.__anext__(), timeout=2)
        done = await asyncio.wait_for(events.__anext__(), timeout=2)
    finally:
        await events.aclose()
        await client.disconnect()

    assert result == {
        "job_id": "job-model-cached",
        "kind": "download_model",
        "status": "running",
    }
    assert progress["event"] == "job_progress"
    assert progress["text"] == "Resolving model"
    assert done["event"] == "job_done"
    assert done["job_id"] == "job-model-cached"
    assert done["ok"] is True
    assert done["detail"] == "model cached"
    assert done["entry_id"] == "01MODEL"
    assert done["cache_state"] == "cached"
    assert done["entry"]["repo_id"] == "meta-llama/Llama-3.1-8B-Instruct"
    json.dumps(progress)
    json.dumps(done)


@pytest.mark.asyncio
async def test_agent_download_model_job_downloads_uncached_hf_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_path = tmp_path / "state" / "vllm-loader" / "models" / "registry.json"
    snapshot_calls: list[dict[str, object]] = []

    def fake_snapshot_download(**kwargs: object) -> str:
        snapshot_calls.append(dict(kwargs))
        return str(tmp_path / "hf-cache" / "snapshots" / "abc123")

    fake_revision = SimpleNamespace(
        commit_hash="abc123",
        size_on_disk=130,
        files=(
            SimpleNamespace(file_name="config.json", size_on_disk=10),
            SimpleNamespace(file_name="model.safetensors", size_on_disk=100),
            SimpleNamespace(file_name="tokenizer.json", size_on_disk=20),
        ),
        refs=("main",),
    )
    fake_repo = SimpleNamespace(
        repo_id="meta-llama/Llama-3.1-8B-Instruct",
        repo_type="model",
        revisions=(fake_revision,),
    )
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.setattr(
        model_registry_module,
        "_snapshot_download",
        fake_snapshot_download,
        raising=False,
    )
    monkeypatch.setattr(
        model_registry_module,
        "_scan_hf_cache_info",
        lambda: SimpleNamespace(repos=(fake_repo,)),
        raising=False,
    )

    client = InProcessTargetClient(LocalAgent(models_registry_path=registry_path))
    await client.connect()
    events = client.subscribe(["job-model-remote"], resume_from="live")
    try:
        await client.call(
            "pin_model",
            {
                "entry_id": "01REMOTE",
                "display_name": "llama-remote",
                "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
                "revision": "main",
            },
        )
        result = await client.call(
            "download_model",
            {
                "job_id": "job-model-remote",
                "model_ref": "01REMOTE",
                "allow_patterns": ["*.safetensors", "*.json"],
                "ignore_patterns": ["*.msgpack"],
            },
        )
        progress = await asyncio.wait_for(events.__anext__(), timeout=2)
        downloading = await asyncio.wait_for(events.__anext__(), timeout=2)
        verifying = await asyncio.wait_for(events.__anext__(), timeout=2)
        done = await asyncio.wait_for(events.__anext__(), timeout=2)
    finally:
        await events.aclose()
        await client.disconnect()

    assert result == {
        "job_id": "job-model-remote",
        "kind": "download_model",
        "status": "running",
    }
    assert progress["event"] == "job_progress"
    assert progress["text"] == "Resolving model"
    assert downloading["event"] == "job_progress"
    assert downloading["text"] == "Downloading model meta-llama/Llama-3.1-8B-Instruct"
    assert verifying["event"] == "job_progress"
    assert verifying["text"] == "Verifying model meta-llama/Llama-3.1-8B-Instruct"
    assert verifying["phase"] == "VERIFYING"
    assert done["event"] == "job_done"
    assert done["job_id"] == "job-model-remote"
    assert done["ok"] is True
    assert done["detail"] == "model cached"
    assert done["entry_id"] == "01REMOTE"
    assert done["cache_state"] == "cached"
    assert done["entry"]["commit_sha"] == "abc123"
    assert done["entry"]["files"] == {
        "count": 3,
        "total_bytes": 130,
        "weights_format": "safetensors",
    }
    tqdm_class = snapshot_calls[0].pop("tqdm_class")
    assert isinstance(tqdm_class, type)
    assert snapshot_calls == [
        {
            "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
            "revision": "main",
            "allow_patterns": ["*.safetensors", "*.json"],
            "ignore_patterns": ["*.msgpack"],
        }
    ]
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    [entry] = registry["entries"]
    assert entry["cache_state"] == "cached"
    assert entry["commit_sha"] == "abc123"
    assert entry["files"]["total_bytes"] == 130
    json.dumps(progress)
    json.dumps(downloading)
    json.dumps(done)


@pytest.mark.asyncio
async def test_agent_download_model_job_streams_snapshot_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_path = tmp_path / "state" / "vllm-loader" / "models" / "registry.json"

    def fake_snapshot_download(**kwargs: object) -> str:
        tqdm_class = kwargs.get("tqdm_class")
        assert tqdm_class is not None
        progress = tqdm_class(total=200, file=io.StringIO())
        progress.update(50)
        progress.update(150)
        progress.close()
        return str(tmp_path / "hf-cache" / "snapshots" / "abc123")

    fake_revision = SimpleNamespace(
        commit_hash="abc123",
        size_on_disk=200,
        files=(
            SimpleNamespace(file_name="config.json", size_on_disk=20),
            SimpleNamespace(file_name="model.safetensors", size_on_disk=180),
        ),
        refs=("main",),
    )
    fake_repo = SimpleNamespace(
        repo_id="meta-llama/Llama-3.1-8B-Instruct",
        repo_type="model",
        revisions=(fake_revision,),
    )
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.setattr(
        model_registry_module,
        "_snapshot_download",
        fake_snapshot_download,
        raising=False,
    )
    monkeypatch.setattr(
        model_registry_module,
        "_scan_hf_cache_info",
        lambda: SimpleNamespace(repos=(fake_repo,)),
        raising=False,
    )

    client = InProcessTargetClient(LocalAgent(models_registry_path=registry_path))
    await client.connect()
    events = client.subscribe(["job-model-progress"], resume_from="live")
    try:
        await client.call(
            "pin_model",
            {
                "entry_id": "01REMOTE",
                "display_name": "llama-remote",
                "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
                "revision": "main",
            },
        )
        await client.call(
            "download_model",
            {"job_id": "job-model-progress", "model_ref": "01REMOTE"},
        )
        progress_events = []
        while True:
            event = await asyncio.wait_for(events.__anext__(), timeout=2)
            if event.get("event") == "job_done":
                done = event
                break
            progress_events.append(event)
    finally:
        await events.aclose()
        await client.disconnect()

    percents = [event.get("percent") for event in progress_events]
    assert 25 in percents
    assert 100 in percents
    assert any(event.get("bytes_total") == 200 for event in progress_events)
    assert any(event.get("phase") == "VERIFYING" for event in progress_events)
    assert done["ok"] is True
    assert done["cache_state"] == "cached"
    json.dumps({"progress": progress_events, "done": done})


@pytest.mark.asyncio
async def test_agent_download_model_job_injects_hf_token_without_persisting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_path = tmp_path / "state" / "vllm-loader" / "models" / "registry.json"
    snapshot_calls: list[dict[str, object]] = []
    hf_token = "hf_live_download_token"

    def fake_snapshot_download(**kwargs: object) -> str:
        snapshot_calls.append(dict(kwargs))
        return str(tmp_path / "hf-cache" / "snapshots" / "abc123")

    fake_revision = SimpleNamespace(
        commit_hash="abc123",
        size_on_disk=130,
        files=(
            SimpleNamespace(file_name="config.json", size_on_disk=10),
            SimpleNamespace(file_name="model.safetensors", size_on_disk=100),
            SimpleNamespace(file_name="tokenizer.json", size_on_disk=20),
        ),
        refs=("main",),
    )
    fake_repo = SimpleNamespace(
        repo_id="meta-llama/Llama-3.1-8B-Instruct",
        repo_type="model",
        revisions=(fake_revision,),
    )
    monkeypatch.setenv("HF_TOKEN", hf_token)
    monkeypatch.setattr(
        model_registry_module,
        "_snapshot_download",
        fake_snapshot_download,
        raising=False,
    )
    monkeypatch.setattr(
        model_registry_module,
        "_scan_hf_cache_info",
        lambda: SimpleNamespace(repos=(fake_repo,)),
        raising=False,
    )

    client = InProcessTargetClient(LocalAgent(models_registry_path=registry_path))
    await client.connect()
    events = client.subscribe(["job-model-token"], resume_from="live")
    try:
        await client.call(
            "pin_model",
            {
                "entry_id": "01REMOTE",
                "display_name": "llama-remote",
                "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
                "revision": "main",
            },
        )
        await client.call(
            "download_model",
            {"job_id": "job-model-token", "model_ref": "01REMOTE"},
        )
        done = await _next_event(events, event_name="job_done")
    finally:
        await events.aclose()
        await client.disconnect()

    tqdm_class = snapshot_calls[0].pop("tqdm_class")
    assert isinstance(tqdm_class, type)
    assert snapshot_calls == [
        {
            "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
            "revision": "main",
            "token": hf_token,
        }
    ]
    assert done["ok"] is True
    assert done["cache_state"] == "cached"
    registry_text = registry_path.read_text(encoding="utf-8")
    assert hf_token not in registry_text
    assert hf_token not in json.dumps(done)
    assert "HF_TOKEN" not in registry_text
    json.dumps(done)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "expected_kind"),
    [
        ("GatedRepoError: Cannot access gated repo; 403 Client Error", "gated-auth"),
        ("RevisionNotFoundError: revision main does not exist", "revision-not-found"),
        ("OSError: [Errno 28] No space left on device", "disk-full"),
        ("ConnectionError: timed out while downloading", "network"),
        ("Hash mismatch while validating downloaded blob", "integrity-mismatch"),
    ],
)
async def test_agent_download_model_job_classifies_snapshot_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
    expected_kind: str,
) -> None:
    registry_path = tmp_path / "state" / "vllm-loader" / "models" / "registry.json"

    def failing_snapshot_download(**_kwargs: object) -> str:
        raise RuntimeError(message)

    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.setattr(
        model_registry_module,
        "_snapshot_download",
        failing_snapshot_download,
        raising=False,
    )

    client = InProcessTargetClient(LocalAgent(models_registry_path=registry_path))
    await client.connect()
    events = client.subscribe(["job-model-failure"], resume_from="live")
    try:
        await client.call(
            "pin_model",
            {
                "entry_id": "01REMOTE",
                "display_name": "llama-remote",
                "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
                "revision": "main",
            },
        )
        await client.call(
            "download_model",
            {"job_id": "job-model-failure", "model_ref": "01REMOTE"},
        )
        resolving = await asyncio.wait_for(events.__anext__(), timeout=2)
        downloading = await asyncio.wait_for(events.__anext__(), timeout=2)
        done = await asyncio.wait_for(events.__anext__(), timeout=2)
    finally:
        await events.aclose()
        await client.disconnect()

    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    [entry] = registry["entries"]
    assert resolving["text"] == "Resolving model"
    assert downloading["text"] == "Downloading model meta-llama/Llama-3.1-8B-Instruct"
    assert done["event"] == "job_done"
    assert done["ok"] is False
    assert done["error_kind"] == expected_kind
    assert done["model_ref"] == "01REMOTE"
    assert done["repo_id"] == "meta-llama/Llama-3.1-8B-Instruct"
    assert done["revision"] == "main"
    assert entry["cache_state"] == "partial"
    json.dumps(done)


@pytest.mark.asyncio
async def test_agent_cancelled_model_download_marks_entry_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_path = tmp_path / "state" / "vllm-loader" / "models" / "registry.json"

    def slow_snapshot_download(**_kwargs: object) -> str:
        time.sleep(0.2)
        return str(tmp_path / "hf-cache" / "snapshots" / "abc123")

    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.setattr(
        model_registry_module,
        "_snapshot_download",
        slow_snapshot_download,
        raising=False,
    )

    client = InProcessTargetClient(LocalAgent(models_registry_path=registry_path))
    await client.connect()
    events = client.subscribe(["job-model-cancel"], resume_from="live")
    try:
        await client.call(
            "pin_model",
            {
                "entry_id": "01REMOTE",
                "display_name": "llama-remote",
                "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
                "revision": "main",
            },
        )
        await client.call(
            "download_model",
            {"job_id": "job-model-cancel", "model_ref": "01REMOTE"},
        )
        await asyncio.wait_for(events.__anext__(), timeout=2)
        downloading = await asyncio.wait_for(events.__anext__(), timeout=2)
        cancel_result = await client.call("cancel_job", {"job_id": "job-model-cancel"})
        done = await asyncio.wait_for(events.__anext__(), timeout=2)
    finally:
        await events.aclose()
        await client.disconnect()

    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    [entry] = registry["entries"]
    assert downloading["text"] == "Downloading model meta-llama/Llama-3.1-8B-Instruct"
    assert cancel_result == {
        "job_id": "job-model-cancel",
        "cancelled": True,
        "status": "cancelled",
    }
    assert done["event"] == "job_done"
    assert done["error_kind"] == "cancelled"
    assert entry["cache_state"] == "partial"


@pytest.mark.asyncio
async def test_agent_cancelled_model_download_interrupts_progress_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_path = tmp_path / "state" / "vllm-loader" / "models" / "registry.json"
    first_progress = threading.Event()
    continue_download = threading.Event()
    worker_interrupted = threading.Event()

    def fake_snapshot_download(**kwargs: object) -> str:
        tqdm_class = kwargs.get("tqdm_class")
        assert tqdm_class is not None
        progress = tqdm_class(total=100, file=io.StringIO())
        progress.update(10)
        first_progress.set()
        continue_download.wait(timeout=2)
        try:
            progress.update(10)
        except Exception:
            worker_interrupted.set()
            raise
        return str(tmp_path / "hf-cache" / "snapshots" / "abc123")

    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.setattr(
        model_registry_module,
        "_snapshot_download",
        fake_snapshot_download,
        raising=False,
    )

    client = InProcessTargetClient(LocalAgent(models_registry_path=registry_path))
    await client.connect()
    events = client.subscribe(["job-model-interrupt"], resume_from="live")
    try:
        await client.call(
            "pin_model",
            {
                "entry_id": "01REMOTE",
                "display_name": "llama-remote",
                "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
                "revision": "main",
            },
        )
        await client.call(
            "download_model",
            {"job_id": "job-model-interrupt", "model_ref": "01REMOTE"},
        )
        assert await asyncio.to_thread(first_progress.wait, 2)
        cancel_task = asyncio.create_task(
            client.call("cancel_job", {"job_id": "job-model-interrupt"})
        )
        await asyncio.sleep(0.05)
        assert cancel_task.done() is False
        continue_download.set()
        cancel_result = await asyncio.wait_for(cancel_task, timeout=2)
        done = await _next_event(events, event_name="job_done")
    finally:
        continue_download.set()
        await events.aclose()
        await client.disconnect()

    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    [entry] = registry["entries"]
    assert worker_interrupted.is_set()
    assert cancel_result == {
        "job_id": "job-model-interrupt",
        "cancelled": True,
        "status": "cancelled",
    }
    assert done["event"] == "job_done"
    assert done["error_kind"] == "cancelled"
    assert done["detail"] == "model download cancelled"
    assert entry["cache_state"] == "partial"


@pytest.mark.asyncio
async def test_agent_cancel_job_emits_cancelled_job_done() -> None:
    started = asyncio.Event()

    async def model_job_runner(_params, emit, _cancel_event) -> dict[str, object]:
        emit({"kind": "committed", "text": "Downloading model", "level": "INFO"})
        started.set()
        await asyncio.Event().wait()
        return {"ok": True}

    client = InProcessTargetClient(LocalAgent(model_job_runner=model_job_runner))
    await client.connect()
    events = client.subscribe(["job-model-2"], resume_from="live")
    try:
        await client.call(
            "download_model",
            {"job_id": "job-model-2", "model_ref": "01MODEL"},
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        cancel_result = await client.call("cancel_job", {"job_id": "job-model-2"})
        progress = await asyncio.wait_for(events.__anext__(), timeout=2)
        done = await asyncio.wait_for(events.__anext__(), timeout=2)
    finally:
        await events.aclose()
        await client.disconnect()

    assert cancel_result == {
        "job_id": "job-model-2",
        "cancelled": True,
        "status": "cancelled",
    }
    assert progress["event"] == "job_progress"
    assert progress["job_id"] == "job-model-2"
    assert done["event"] == "job_done"
    assert done["job_id"] == "job-model-2"
    assert done["ok"] is False
    assert done["error_kind"] == "cancelled"
    assert done["detail"] == "cancelled"
    json.dumps(done)


@pytest.mark.asyncio
async def test_target_client_launches_attached_run_with_serialized_events(
    config_dir: Path, tmp_path: Path
) -> None:
    child = tmp_path / "child.py"
    child.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "print('INFO Starting to load model', flush=True)",
            ]
        ),
        encoding="utf-8",
    )
    child.chmod(0o755)
    write_yaml(
        config_dir / "wire.yaml",
        f"""
        name: wire
        model: fake/model
        command:
          entrypoint: serve
          executable: {child}
        launch:
          runs_dir: {tmp_path / "runs"}
        """,
    )
    client = InProcessTargetClient(LocalAgent())
    await client.connect()

    launch = await client.call(
        "launch",
        {"name": "wire", "configs_dir": str(config_dir), "run_id": "run-wire-1"},
    )

    assert launch == {
        "run_id": "run-wire-1",
        "launch_mode": "attached",
        "status": "started",
    }
    json.dumps(launch)

    events = client.subscribe(["run-wire-1"], resume_from="live")
    wait_task = asyncio.create_task(client.call("wait", {"run_id": "run-wire-1"}))
    event = await asyncio.wait_for(events.__anext__(), timeout=2)
    wait_result = await wait_task
    await events.aclose()

    assert event["event"] == "log"
    assert event["run_id"] == "run-wire-1"
    assert event["text"] == "INFO Starting to load model"
    assert isinstance(event["seq"], int)
    assert isinstance(event["ts"], str)
    assert isinstance(event["mono"], float)
    assert isinstance(event["log_inode"], int)
    assert isinstance(event["byte_offset"], int)
    assert event["byte_offset"] > 0
    json.dumps(event)
    assert wait_result == {
        "run_id": "run-wire-1",
        "returncode": 0,
        "intentional": False,
        "phase": Phase.ERROR.value,
        "error_kind": ErrorKind.CRASHED.value,
        "error_excerpt": "INFO Starting to load model",
    }
    json.dumps(wait_result)


@pytest.mark.asyncio
async def test_target_client_replays_buffered_run_events_from_sequence(
    config_dir: Path, tmp_path: Path
) -> None:
    child = tmp_path / "child.py"
    child.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "print('INFO Starting to load model', flush=True)",
                "print('INFO Uvicorn running on http://127.0.0.1:8000', flush=True)",
            ]
        ),
        encoding="utf-8",
    )
    child.chmod(0o755)
    write_yaml(
        config_dir / "replay.yaml",
        f"""
        name: replay
        model: fake/model
        command:
          entrypoint: serve
          executable: {child}
        launch:
          runs_dir: {tmp_path / "runs"}
        """,
    )
    client = InProcessTargetClient(LocalAgent())
    await client.connect()

    await client.call(
        "launch",
        {"name": "replay", "configs_dir": str(config_dir), "run_id": "run-replay-1"},
    )
    await client.call("wait", {"run_id": "run-replay-1"})

    events = client.subscribe(["run-replay-1"], resume_from={"seq": 1})
    replayed = await asyncio.wait_for(events.__anext__(), timeout=2)
    await events.aclose()

    assert replayed["event"] == "phase"
    assert replayed["run_id"] == "run-replay-1"
    assert replayed["seq"] > 1
    json.dumps(replayed)


@pytest.mark.asyncio
async def test_target_client_replays_durable_log_events_from_offset(
    config_dir: Path, tmp_path: Path
) -> None:
    child = tmp_path / "child.py"
    child.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "print('INFO first line', flush=True)",
                "print('INFO second line', flush=True)",
            ]
        ),
        encoding="utf-8",
    )
    child.chmod(0o755)
    write_yaml(
        config_dir / "offset-replay.yaml",
        f"""
        name: offset-replay
        model: fake/model
        command:
          entrypoint: serve
          executable: {child}
        launch:
          runs_dir: {tmp_path / "runs"}
        """,
    )
    client = InProcessTargetClient(LocalAgent())
    await client.connect()

    await client.call(
        "launch",
        {
            "name": "offset-replay",
            "configs_dir": str(config_dir),
            "run_id": "run-offset-replay-1",
        },
    )
    await client.call("wait", {"run_id": "run-offset-replay-1"})

    log_path = tmp_path / "runs" / "run-offset-replay-1.run.log"
    first_line_offset = len(b"INFO first line\n")
    events = client.subscribe(
        ["run-offset-replay-1"],
        resume_from={
            "log_inode": log_path.stat().st_ino,
            "byte_offset": first_line_offset,
        },
    )
    replayed = await asyncio.wait_for(events.__anext__(), timeout=2)
    await events.aclose()

    assert replayed["event"] == "log"
    assert replayed["run_id"] == "run-offset-replay-1"
    assert replayed["text"] == "INFO second line"
    assert replayed["byte_offset"] == len(b"INFO first line\nINFO second line\n")
    json.dumps(replayed)


@pytest.mark.asyncio
async def test_target_client_replays_from_new_active_log_after_rotation(
    config_dir: Path, tmp_path: Path
) -> None:
    child = tmp_path / "child.py"
    child.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "print('INFO old active line', flush=True)",
            ]
        ),
        encoding="utf-8",
    )
    child.chmod(0o755)
    write_yaml(
        config_dir / "rotation-replay.yaml",
        f"""
        name: rotation-replay
        model: fake/model
        command:
          entrypoint: serve
          executable: {child}
        launch:
          runs_dir: {tmp_path / "runs"}
        """,
    )
    agent = LocalAgent()
    client = InProcessTargetClient(agent)
    await client.connect()

    await client.call(
        "launch",
        {
            "name": "rotation-replay",
            "configs_dir": str(config_dir),
            "run_id": "run-rotation-replay-1",
        },
    )
    await client.call("wait", {"run_id": "run-rotation-replay-1"})

    old_log_path = tmp_path / "runs" / "run-rotation-replay-1.run.log"
    old_inode = old_log_path.stat().st_ino
    rotated_log_path = tmp_path / "runs" / "run-rotation-replay-1.run.log.1"
    rotated_log_path.write_text("INFO new active line\n", encoding="utf-8")
    run = agent._detached_runs["run-rotation-replay-1"]
    run.manifest.rotate_to(rotated_log_path)

    events = client.subscribe(
        ["run-rotation-replay-1"],
        resume_from={
            "log_inode": old_inode,
            "byte_offset": old_log_path.stat().st_size,
        },
    )
    resumed = await asyncio.wait_for(events.__anext__(), timeout=2)
    replayed = await asyncio.wait_for(events.__anext__(), timeout=2)
    await events.aclose()

    assert resumed["event"] == "log"
    assert resumed["text"] == "[resumed after rotation]"
    assert replayed["event"] == "log"
    assert replayed["text"] == "INFO new active line"
    assert replayed["log_inode"] == rotated_log_path.stat().st_ino


@pytest.mark.asyncio
async def test_target_client_kills_attached_run_by_run_id(
    config_dir: Path, tmp_path: Path
) -> None:
    child = tmp_path / "child.py"
    child.write_text(
        "#!/usr/bin/env python3\nimport time\nwhile True:\n    time.sleep(0.05)\n",
        encoding="utf-8",
    )
    child.chmod(0o755)
    write_yaml(
        config_dir / "kill-wire.yaml",
        f"""
        name: kill-wire
        model: fake/model
        command:
          entrypoint: serve
          executable: {child}
        launch:
          runs_dir: {tmp_path / "runs"}
        """,
    )
    client = InProcessTargetClient(LocalAgent())
    await client.connect()

    await client.call(
        "launch",
        {
            "name": "kill-wire",
            "configs_dir": str(config_dir),
            "run_id": "run-kill-1",
        },
    )
    kill = await client.call("kill", {"run_id": "run-kill-1"})
    wait_result = await client.call("wait", {"run_id": "run-kill-1"})

    assert kill == {"run_id": "run-kill-1", "signaled": True}
    json.dumps(kill)
    assert wait_result["run_id"] == "run-kill-1"
    assert wait_result["intentional"] is True
    json.dumps(wait_result)


@pytest.mark.asyncio
async def test_target_client_probe_until_ready_emits_serialized_health_events(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = tmp_path / "child.py"
    child.write_text(
        "#!/usr/bin/env python3\nimport time\nwhile True:\n    time.sleep(0.05)\n",
        encoding="utf-8",
    )
    child.chmod(0o755)
    write_yaml(
        config_dir / "probe-wire.yaml",
        f"""
        name: probe-wire
        model: fake/model
        command:
          entrypoint: serve
          executable: {child}
        server:
          host: 127.0.0.1
          port: 8128
        launch:
          runs_dir: {tmp_path / "runs"}
        """,
    )
    seen: dict[str, object] = {}

    async def fake_probe_loop(cfg, *, emit, is_process_alive):
        seen["name"] = cfg.name
        seen["alive"] = is_process_alive()
        emit(HealthEvent(ready=True, detail="ready", models=["served"]))

    monkeypatch.setattr(local_agent_module, "probe_loop", fake_probe_loop)
    client = InProcessTargetClient(LocalAgent())
    await client.connect()

    await client.call(
        "launch",
        {
            "name": "probe-wire",
            "configs_dir": str(config_dir),
            "run_id": "run-probe-1",
        },
    )
    probe = await client.call("probe_until_ready", {"run_id": "run-probe-1"})
    events = client.subscribe(["run-probe-1"], resume_from="start")
    replayed = [await asyncio.wait_for(events.__anext__(), timeout=2) for _ in range(3)]
    await events.aclose()
    await client.call("stop", {"run_id": "run-probe-1", "interrupt_timeout": 1})
    await client.call("wait", {"run_id": "run-probe-1"})

    assert seen == {"name": "probe-wire", "alive": True}
    assert probe == {
        "run_id": "run-probe-1",
        "ready": True,
        "detail": "ready",
        "models": ["served"],
        "error_kind": None,
        "reachable_url": "http://127.0.0.1:8128",
        "phase": Phase.READY.value,
    }
    health_event = next(event for event in replayed if event["event"] == "health")
    ready_event = next(event for event in replayed if event["event"] == "ready")
    assert health_event["ready"] is True
    assert health_event["models"] == ["served"]
    assert ready_event["reachable_url"] == "http://127.0.0.1:8128"
    json.dumps(health_event)
    json.dumps(ready_event)
