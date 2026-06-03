from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from conftest import write_yaml

from vllm_loader.agent.local import LocalAgent, TargetCallError
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
