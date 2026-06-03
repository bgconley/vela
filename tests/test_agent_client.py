from __future__ import annotations

import asyncio
import json
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


@pytest.mark.asyncio
async def test_in_process_target_client_handshake_exposes_local_agent() -> None:
    client = InProcessTargetClient(LocalAgent(target_name="local"))

    assert client.connected is False

    await client.connect()
    result = await client.call("handshake")

    assert client.connected is True
    assert result["protocol_version"] == 1
    assert result["target"] == "local"
    assert "list_configs" in result["capabilities"]
    assert "preview" in result["capabilities"]

    await client.disconnect()
    assert client.connected is False


@pytest.mark.asyncio
async def test_in_process_target_client_requires_connection() -> None:
    client = InProcessTargetClient(LocalAgent())

    with pytest.raises(RuntimeError, match="not connected"):
        await client.call("handshake")


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
    prepared = agent.handle("prepare_launch", {"name": "attached", "configs_dir": str(config_dir)})

    run = agent.start_attached_run(prepared)
    try:
        for _ in range(100):
            if marker.exists():
                break
            await asyncio.sleep(0.01)

        assert marker.read_text(encoding="utf-8") == "started"
        assert agent.is_run_alive(run.run_id) is True

        agent.stop_run(run.run_id, interrupt_timeout=1, terminate_timeout=1)
        returncode, intentional = await agent.wait_attached_run(run.run_id)

        assert intentional is True
        assert returncode == 0
        assert marker.read_text(encoding="utf-8") == "stopped"
        assert agent.is_run_alive(run.run_id) is False
    finally:
        if agent.is_run_alive(run.run_id):
            agent.kill_run(run.run_id)
            await agent.wait_attached_run(run.run_id)


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
    prepared = agent.handle("prepare_launch", {"name": "health", "configs_dir": str(config_dir)})
    run = agent.start_attached_run(prepared)
    events: list[HealthEvent] = []

    try:
        await agent.probe_run_until_ready(run.run_id, emit=events.append)

        assert seen == {"name": "health", "alive": True}
        assert events == [HealthEvent(ready=True, detail="ready", models=["served"])]
    finally:
        if agent.is_run_alive(run.run_id):
            agent.kill_run(run.run_id)
        await agent.wait_attached_run(run.run_id)


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
    monkeypatch.setattr(local_agent_module, "load_sidecar", lambda path: sidecar)
    monkeypatch.setattr(local_agent_module, "load_manifest", lambda path: manifest)
    monkeypatch.setattr(local_agent_module, "probe_loop", fake_probe_loop)
    agent = LocalAgent()
    agent.reattach_detached_run(sidecar_path)
    events: list[HealthEvent] = []

    await agent.probe_run_until_ready("run-1", emit=events.append)

    assert seen == {"name": "detached", "port": 8123, "alive": True}
    assert events == [HealthEvent(ready=True, detail="ready", models=["served"])]


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
    agent = LocalAgent()
    prepared = agent.handle("prepare_launch", {"name": "events", "configs_dir": str(config_dir)})
    events: list[object] = []

    run = agent.start_attached_run(prepared, emit_event=events.append)
    returncode, intentional = await agent.wait_attached_run(run.run_id)

    assert intentional is False
    assert returncode == 0
    log_events = [event for event in events if getattr(event, "kind", None) == "log"]
    phase_events = [event for event in events if getattr(event, "kind", None) == "phase"]
    assert log_events[-1].payload["text"] == "INFO Starting to load model"
    assert phase_events[-1].payload["phase"] == Phase.LOADING_WEIGHTS.value


def test_local_agent_starts_detached_run_from_prepared_launch(
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
        return DetachedLaunch(
            run_id="run-1",
            supervisor_pid=123,
            sidecar_path=sidecar_path,
            manifest_path=manifest_path,
            log_path=log_path,
        )

    monkeypatch.setattr(local_agent_module, "start_detached", fake_start_detached)
    agent = LocalAgent()
    prepared = agent.handle(
        "prepare_launch", {"name": "detached", "configs_dir": str(config_dir)}
    )

    launch = agent.start_detached_run(prepared)

    assert launch.run_id == "run-1"
    assert launch.sidecar_path == sidecar_path
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


def test_local_agent_reattaches_and_stops_detached_run_by_run_id(
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

    run = agent.reattach_detached_run(sidecar_path)
    agent.stop_run("run-1", interrupt_timeout=2, terminate_timeout=3)

    assert run.run_id == "run-1"
    assert run.sidecar_path == sidecar_path
    assert agent.is_run_alive("run-1") is True
    assert stopped == [(sidecar_path, 2, 3)]


def test_local_agent_discovers_detached_runs_from_agent_side_sidecars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    agent = LocalAgent()

    runs = agent.discover_detached_runs([tmp_path / "runs"])

    assert seen["runs_dirs"] == [tmp_path / "runs"]
    assert runs[0].run_id == "run-1"
    assert runs[0].config_name == "detached"
    assert runs[0].sidecar_path == sidecar_path


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
    monkeypatch.setattr(local_agent_module, "load_sidecar", lambda path: sidecar)
    monkeypatch.setattr(local_agent_module, "load_manifest", lambda path: manifest)
    agent = LocalAgent()
    agent.reattach_detached_run(sidecar_path)
    alive_checks = 0
    events: list[object] = []

    await agent.tail_detached_run(
        "run-1",
        start_position=0,
        emit_event=events.append,
        poll_interval=0,
    )

    log_events = [event for event in events if getattr(event, "kind", None) == "log"]
    phase_events = [event for event in events if getattr(event, "kind", None) == "phase"]
    assert log_events[-1].payload["text"] == "INFO Starting to load model"
    assert phase_events[0].payload["phase"] == Phase.LOADING_WEIGHTS.value


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
    monkeypatch.setattr(local_agent_module, "load_sidecar", lambda path: sidecar)
    monkeypatch.setattr(local_agent_module, "load_manifest", lambda path: manifest)
    agent = LocalAgent()
    agent.reattach_detached_run(sidecar_path)
    alive_checks = 0
    client = InProcessTargetClient(agent)
    await client.connect()

    tail_result = await client.call(
        "tail_detached",
        {"run_id": "run-1", "start_position": 0, "poll_interval": 0},
    )
    events = client.subscribe(["run-1"], resume_from="start")
    replayed = [await asyncio.wait_for(events.__anext__(), timeout=2) for _ in range(4)]
    await events.aclose()

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
    }
    health_event = next(event for event in replayed if event["event"] == "health")
    ready_event = next(event for event in replayed if event["event"] == "ready")
    assert health_event["ready"] is True
    assert health_event["models"] == ["served"]
    assert ready_event["reachable_url"] == "http://127.0.0.1:8128"
    json.dumps(health_event)
    json.dumps(ready_event)
