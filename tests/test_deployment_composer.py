from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from conftest import write_yaml

from vela.agent.local import LocalAgent, TargetCallError


def _call(agent: LocalAgent, method: str, params: dict) -> dict:
    result = agent.handle(method, params)
    assert not inspect.isawaitable(result)
    return result


def test_agent_composes_docker_deployment_draft_for_tui(config_dir: Path) -> None:
    write_yaml(
        config_dir / "taken.yaml",
        """
        name: taken
        model: org/taken
        server:
          port: 18000
        """,
    )
    agent = LocalAgent()

    result = _call(
        agent,
        "compose_config",
        {
            "configs_dir": str(config_dir),
            "name": "qwen3-32b-bf16",
            "target": "blackbird",
            "runtime": {
                "kind": "docker",
                "image": "vllm/vllm-openai@sha256:abc",
            },
            "model": "Qwen/Qwen3-32B",
            "preset": "qwen3-text",
            "overrides": {
                "server": {"host": "0.0.0.0", "exposure": "lan"},
                "engine": {"dtype": "bfloat16", "gpu_memory_utilization": 0.95},
                "extra_args": ["--max-num-batched-tokens", "4096"],
            },
        },
    )

    config = result["config"]
    assert config["name"] == "qwen3-32b-bf16"
    assert config["target"] == "blackbird"
    assert config["served_model_name"] == "Qwen3-32B"
    assert config["server"]["port"] == 18001
    assert config["server"]["host"] == "0.0.0.0"
    assert config["server"]["exposure"] == "lan"
    assert config["launch"]["runs_dir"].endswith("/qwen3-32b-bf16")
    assert config["command"]["runtime"] == "docker"
    assert config["command"]["docker"]["image"] == "vllm/vllm-openai@sha256:abc"
    assert config["command"]["docker"]["container_name"] == "vela-qwen3-32b-bf16"
    assert config["engine"]["dtype"] == "bfloat16"
    assert config["engine"]["gpu_memory_utilization"] == 0.95
    assert "--language-model-only" in config["extra_args"]
    assert "--max-num-batched-tokens" in config["extra_args"]
    assert {item["field"] for item in result["derived"]} >= {
        "served_model_name",
        "server.port",
        "launch.runs_dir",
        "command.docker.container_name",
    }
    assert result["warnings"] == []


def test_agent_validates_composed_draft(config_dir: Path) -> None:
    agent = LocalAgent()
    draft = _call(
        agent,
        "compose_config",
        {
            "configs_dir": str(config_dir),
            "name": "qwen3",
            "runtime": "process",
            "model": "Qwen/Qwen3-32B",
        },
    )["config"]

    result = _call(agent, "validate_config", {"config": draft})

    assert result["ok"] is True
    assert result["errors"] == []


def test_agent_previews_unsaved_composed_draft(config_dir: Path) -> None:
    agent = LocalAgent()
    draft = _call(
        agent,
        "compose_config",
        {
            "configs_dir": str(config_dir),
            "name": "draft-docker",
            "runtime": {
                "kind": "docker",
                "image": "vllm/vllm-openai@sha256:abc",
            },
            "model": "Qwen/Qwen3-32B",
        },
    )["config"]

    result = _call(agent, "preview", {"config": draft, "configs_dir": str(config_dir)})

    assert "docker run" in result["preview"]
    assert "vllm/vllm-openai@sha256:abc" in result["preview"]
    assert "Qwen/Qwen3-32B" in result["preview"]


def test_agent_save_config_writes_yaml_and_refuses_clobber(config_dir: Path) -> None:
    agent = LocalAgent()
    draft = _call(
        agent,
        "compose_config",
        {
            "configs_dir": str(config_dir),
            "name": "qwen3",
            "runtime": "process",
            "model": "Qwen/Qwen3-32B",
        },
    )["config"]

    saved = _call(
        agent,
        "save_config",
        {"configs_dir": str(config_dir), "name": "qwen3", "config": draft},
    )

    path = Path(saved["path"])
    assert path == config_dir / "qwen3.yaml"
    assert "Qwen/Qwen3-32B" in path.read_text(encoding="utf-8")
    assert (path.stat().st_mode & 0o777) == 0o644
    with pytest.raises(TargetCallError) as exc_info:
        _call(
            agent,
            "save_config",
            {"configs_dir": str(config_dir), "name": "qwen3", "config": draft},
        )
    assert exc_info.value.code == "config-exists"


def test_agent_lists_composer_presets_for_wizard() -> None:
    result = _call(LocalAgent(), "list_presets", {})

    names = {item["name"] for item in result["presets"]}
    assert {"balanced", "qwen3-text", "low-memory"} <= names
