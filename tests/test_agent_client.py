from __future__ import annotations

import asyncio
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
