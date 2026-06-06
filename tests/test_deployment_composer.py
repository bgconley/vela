from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest
from conftest import write_yaml

from vela.agent.local import LocalAgent, TargetCallError
from vela.engine import composer as composer_module

BLACKBIRD_IMAGE = (
    "vllm/vllm-openai@sha256:"
    "b13d6e5fda0785f3d41752df8513ff832f67cb231a216c76b6b4f2a515bf0046"
)


def _call(agent: LocalAgent, method: str, params: dict) -> dict:
    result = agent.handle(method, params)
    assert not inspect.isawaitable(result)
    return result


def _write_model_registry(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "default_cache": "hf",
                "app_download_dir": None,
                "entries": [
                    {
                        "entry_id": "01QWENFP8",
                        "display_name": "qwen36-fp8",
                        "aliases": ["qwen-fp8"],
                        "source": "hf_repo",
                        "repo_id": "Qwen/Qwen3.6-27B-FP8",
                        "revision": "main",
                        "commit_sha": "abc123",
                        "local_path": None,
                        "url": None,
                        "quant_format": "fp8",
                        "tokenizer": None,
                        "files": {
                            "count": 7,
                            "total_bytes": 62000000000,
                            "weights_format": "safetensors",
                        },
                        "size_bytes": 62000000000,
                        "cache_state": "remote_only",
                        "gated": True,
                        "token_required": True,
                        "created_at": "2026-06-06T00:00:00Z",
                        "last_used_at": None,
                        "notes": "composer suggestion fixture",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


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


def test_agent_composes_blackbird_qwen36_fp8_from_lab_recipe(config_dir: Path) -> None:
    agent = LocalAgent()

    result = _call(
        agent,
        "compose_config",
        {
            "configs_dir": str(config_dir),
            "name": "qwen36-27b-fp8-kvfp8-rp6000-blackbird",
            "target": "blackbird",
            "runtime": {"kind": "docker"},
            "model": "Qwen/Qwen3.6-27B-FP8",
        },
    )

    config = result["config"]
    docker = config["command"]["docker"]
    assert config["served_model_name"] == "qwen36-27b-fp8-kvfp8-rp6000"
    assert config["server"] == {
        "host": "0.0.0.0",
        "port": 18003,
        "exposure": "lan",
        "api_key": "EMPTY",
        "probe_host": None,
    }
    assert config["engine"]["gpu_memory_utilization"] == 0.97
    assert config["engine"]["max_model_len"] == 262144
    assert config["engine"]["dtype"] == "auto"
    assert config["engine"]["kv_cache_dtype"] == "fp8"
    assert config["engine"]["max_num_seqs"] == 16
    assert docker["image"] == BLACKBIRD_IMAGE
    assert docker["shm_size"] == "32g"
    assert docker["network"] == "host"
    assert docker["hf_cache"] == "/home/bgconley/models/qwen36-dual-fp8-vlm/hf-cache"
    assert docker["env"]["FLASHINFER_CUDA_ARCH_LIST"] == "12.0f"
    assert docker["env"]["TRITON_CACHE_DIR"] == "/root/.cache/triton"
    flashinfer_volume = (
        "/home/bgconley/models/qwen36-27b-fp8-rp6000/"
        "flashinfer-cache:/root/.cache/flashinfer"
    )
    assert flashinfer_volume in docker["volumes"]
    assert docker["extra_run_args"] == [
        "--ulimit",
        "memlock=-1",
        "--ulimit",
        "stack=67108864",
    ]
    assert "--attention-backend" in config["extra_args"]
    assert "FLASHINFER" in config["extra_args"]
    assert "--kv-cache-memory-bytes" in config["extra_args"]
    assert "64424509440" in config["extra_args"]
    assert config["launch"]["ready_timeout_seconds"] == 1800
    assert config["launch"]["runs_dir"] == "/home/bgconley/models/qwen36-27b-fp8-rp6000/vela-runs"
    assert config["vllm"]["version_profile"] == "0.11"
    assert any(
        item["field"] == "deployment.recipe"
        and item["value"] == "blackbird-qwen36-27b-fp8-rp6000"
        for item in result["derived"]
    )


def test_agent_composes_blackbird_qwen36_bf16_without_fp8_pins(config_dir: Path) -> None:
    agent = LocalAgent()

    result = _call(
        agent,
        "compose_config",
        {
            "configs_dir": str(config_dir),
            "name": "qwen36-27b-bf16-rp6000-blackbird",
            "target": "blackbird",
            "runtime": {"kind": "docker"},
            "model": "Qwen/Qwen3.6-27B",
        },
    )

    config = result["config"]
    docker = config["command"]["docker"]
    assert config["served_model_name"] == "qwen36-27b-bf16-rp6000"
    assert config["server"]["port"] == 18002
    assert config["engine"]["dtype"] == "bfloat16"
    assert config["engine"]["kv_cache_dtype"] == "bfloat16"
    assert config["engine"]["max_num_seqs"] == 4
    assert docker["image"] == BLACKBIRD_IMAGE
    assert docker["shm_size"] == "32g"
    assert "FLASHINFER_CUDA_ARCH_LIST" not in docker["env"]
    assert "--kv-cache-memory-bytes" not in config["extra_args"]
    assert "--attention-backend" not in config["extra_args"]


def test_agent_suggests_defaults_from_pinned_model_registry(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_path = tmp_path / "state" / "vela" / "models" / "registry.json"
    _write_model_registry(registry_path)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.setattr(composer_module, "_load_hf_model_config", lambda *_args, **_kwargs: {})
    agent = LocalAgent(models_registry_path=registry_path)

    result = _call(
        agent,
        "suggest_deployment_defaults",
        {
            "configs_dir": str(config_dir),
            "name": "qwen-fp8",
            "runtime": {"kind": "docker"},
            "model_ref": "qwen-fp8",
        },
    )

    assert result["model"] == "Qwen/Qwen3.6-27B-FP8"
    assert result["model_ref"] == "qwen-fp8"
    assert result["served_model_name"] == "qwen36-fp8"
    assert result["container_name"] == "vela-qwen-fp8"
    assert result["engine_suggestions"] == {
        "dtype": "auto",
        "kv_cache_dtype": "fp8",
        "tensor_parallel_size": 1,
    }
    assert "model_registry" in result["sources"]
    assert "gated-needs-token" in result["warnings"]


def test_agent_composes_model_ref_with_model_suggestions_without_clobbering_overrides(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_path = tmp_path / "state" / "vela" / "models" / "registry.json"
    _write_model_registry(registry_path)
    monkeypatch.setattr(composer_module, "_load_hf_model_config", lambda *_args, **_kwargs: {})
    agent = LocalAgent(models_registry_path=registry_path)

    result = _call(
        agent,
        "compose_config",
        {
            "configs_dir": str(config_dir),
            "name": "qwen-fp8",
            "runtime": {
                "kind": "docker",
                "image": "vllm/vllm-openai@sha256:abc",
            },
            "model_ref": "qwen-fp8",
            "overrides": {"engine": {"kv_cache_dtype": "bfloat16"}},
        },
    )

    config = result["config"]
    assert config["model"] == "Qwen/Qwen3.6-27B-FP8"
    assert config["model_ref"] == "qwen-fp8"
    assert config["served_model_name"] == "qwen36-fp8"
    assert config["engine"]["dtype"] == "auto"
    assert config["engine"]["kv_cache_dtype"] == "bfloat16"
    assert any(
        item["field"] == "engine.tensor_parallel_size"
        and item["source"] == "model_registry"
        for item in result["derived"]
    )


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


def test_agent_exports_docker_config_as_target_local_standalone_script(
    config_dir: Path, tmp_path: Path
) -> None:
    agent = LocalAgent()
    draft = _call(
        agent,
        "compose_config",
        {
            "configs_dir": str(config_dir),
            "name": "qwen36-export",
            "runtime": {
                "kind": "docker",
                "image": "vllm/vllm-openai@sha256:abc",
            },
            "model": "Qwen/Qwen3.6-27B-FP8",
            "overrides": {"env": {"HF_TOKEN": "hf_export_secret"}},
        },
    )["config"]
    output_path = tmp_path / "qwen36-export.sh"

    result = _call(
        agent,
        "export_config",
        {
            "config": draft,
            "output_path": str(output_path),
        },
    )

    assert result["name"] == "qwen36-export"
    assert result["path"] == str(output_path)
    assert output_path.stat().st_mode & 0o111
    script = output_path.read_text(encoding="utf-8")
    assert result["script"] == script
    assert "docker run" in script
    assert "vllm/vllm-openai@sha256:abc" in script
    assert "hf_export_secret" not in script
    assert ': "${HF_TOKEN:?Set HF_TOKEN before running}"' in script


def test_agent_lists_composer_presets_for_wizard() -> None:
    result = _call(LocalAgent(), "list_presets", {})

    names = {item["name"] for item in result["presets"]}
    assert {"balanced", "qwen3-text", "low-memory"} <= names
