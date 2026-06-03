from __future__ import annotations

import asyncio
import inspect
import json
import socket
import sys
import uuid
from pathlib import Path

import pytest
from conftest import write_yaml

from vllm_loader.agent import local as local_agent_module
from vllm_loader.agent.local import LocalAgent, TargetCallError
from vllm_loader.engine.phases import Phase
from vllm_loader.engine.process_manager import DetachedLaunch
from vllm_loader.engine.sidecar import Manifest, Sidecar
from vllm_loader.monitoring.gpu import GpuPollResult, GpuSample
from vllm_loader.monitoring.health import HealthEvent
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
    assert connected["target"] == "local"
    assert result["protocol_version"] == 1
    assert result["target"] == "local"
    assert "list_configs" in result["capabilities"]
    assert "preview" in result["capabilities"]
    assert "health" in result["capabilities"]
    assert "discover_runs" in result["capabilities"]
    assert "discover_runs_no_paths" in result["capabilities"]
    assert "reattach" in result["capabilities"]
    assert result["daemon_pid"] > 0
    assert result["daemon_start_ts"]
    assert result["host_info"]["vllm_loader_version"] == result["agent_version"]
    assert result["host_info"]["hostname"]
    assert result["host_info"]["platform"]

    await client.disconnect()
    assert client.connected is False


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
    assert connected["target"] == "local"
    assert result["protocol_version"] == 1
    assert result["target"] == "local"
    assert "list_configs" in result["capabilities"]
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
    finally:
        await client.disconnect()

    assert runs_dir in seen["runs_dirs"]
    assert discovered == {"runs": [{"run_id": "run-1", "config_name": "detached"}]}


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
    reattached = await client.call("reattach_detached", {"run_id": "run-1"})

    assert discovered == {
        "runs": [
            {
                "run_id": "run-1",
                "config_name": "detached",
            }
        ]
    }
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
    }
    health_event = next(event for event in replayed if event["event"] == "health")
    ready_event = next(event for event in replayed if event["event"] == "ready")
    assert health_event["ready"] is True
    assert health_event["models"] == ["served"]
    assert ready_event["reachable_url"] == "http://127.0.0.1:8128"
    json.dumps(health_event)
    json.dumps(ready_event)
