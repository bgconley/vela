from __future__ import annotations

import inspect
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from conftest import write_yaml
from huggingface_hub import constants as hf_constants

from vela.agent import local as local_agent_module
from vela.agent.local import LocalAgent, TargetCallError
from vela.config.schema import ModelConfig
from vela.engine import composer as composer_module
from vela.engine.sidecar import Manifest, Sidecar, command_hash, process_identity_from_pid

BLACKBIRD_IMAGE = (
    "vllm/vllm-openai@sha256:"
    "b13d6e5fda0785f3d41752df8513ff832f67cb231a216c76b6b4f2a515bf0046"
)
OXCART_RECIPE_KEY = "oxcart-qwen36-27b-fp8-mtp-vl"
OXCART_COMMIT = "e89b16ebf1988b3d6befa7de50abc2d76f26eb09"


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


def _write_experimental_fp8_registry(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "default_cache": "hf",
                "app_download_dir": None,
                "entries": [
                    {
                        "entry_id": "01EXPERIMENTALFP8",
                        "display_name": "experimental-fp8",
                        "aliases": ["experimental-fp8"],
                        "source": "hf_repo",
                        "repo_id": "Example/Experimental-FP8",
                        "revision": "main",
                        "commit_sha": "def456",
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
                        "gated": False,
                        "token_required": False,
                        "created_at": "2026-06-06T00:00:00Z",
                        "last_used_at": None,
                        "notes": "non-recipe FP8 warning fixture",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _write_oxcart_multi_pin_registry(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = []
    for entry_id, commit_sha in (
        ("01OXCARTEXACT", OXCART_COMMIT),
        ("01OXCARTSTALE", "f" * 40),
    ):
        entries.append(
            {
                "entry_id": entry_id,
                "display_name": entry_id.lower(),
                "aliases": [],
                "source": "hf_repo",
                "repo_id": "Qwen/Qwen3.6-27B-FP8",
                "revision": commit_sha,
                "commit_sha": commit_sha,
                "files": {
                    "count": 2,
                    "total_bytes": 16,
                    "weights_format": "safetensors",
                },
                "size_bytes": 16,
                "cache_state": "cached",
                "gated": False,
                "token_required": False,
            }
        )
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "default_cache": "hf",
                "app_download_dir": None,
                "entries": entries,
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
    provenance = {item["field"]: item for item in result["derived"]}
    assert set(provenance) >= {
        "served_model_name",
        "server.port",
        "launch.runs_dir",
        "command.docker.container_name",
    }
    assert provenance["server.host"]["source"] == "operator_override"
    assert provenance["server.exposure"]["source"] == "operator_override"
    assert provenance["server.port"]["source"] == "port_allocator"
    assert provenance["launch.runs_dir"]["source"] == "generated_runs_dir"
    assert provenance["command.docker.image"]["source"] == "operator_input"
    assert provenance["command.docker.container_name"]["source"] == (
        "generated_container_name"
    )
    assert provenance["engine.dtype"]["source"] == "operator_override"
    assert provenance["engine.gpu_memory_utilization"]["source"] == "operator_override"
    assert provenance["extra_args"]["source"] == "preset:qwen3-text + operator_override"
    assert "non-local-bind" in "\n".join(result["warnings"])


def test_agent_composes_generic_docker_with_fresh_container_name_from_docker_ps(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        local_agent_module,
        "_docker_container_names",
        lambda: {"vela-qwen3", "vela-qwen3-2"},
        raising=False,
    )
    agent = LocalAgent()

    result = _call(
        agent,
        "compose_config",
        {
            "configs_dir": str(config_dir),
            "name": "qwen3",
            "runtime": {"kind": "docker", "image": "vllm/vllm-openai@sha256:abc"},
            "model": "Qwen/Qwen3-32B",
        },
    )

    assert result["config"]["command"]["docker"]["container_name"] == "vela-qwen3-3"
    assert any(
        item["field"] == "command.docker.container_name"
        and item["source"] == "docker_container_name_collision"
        for item in result["derived"]
    )
    assert "container-name-reassigned" in result["warnings"]


def test_agent_composer_skips_live_sidecar_and_listener_ports(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_yaml(
        config_dir / "taken.yaml",
        """
        name: taken
        model: org/taken
        server:
          port: 18000
        """,
    )
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    log_path = runs_dir / "live.log"
    log_path.write_text("", encoding="utf-8")
    manifest_path = runs_dir / "live.manifest.json"
    Manifest.from_active_log(log_path).write_atomic(manifest_path)
    sidecar_path = runs_dir / "live.json"
    Sidecar(
        run_id="run-live",
        config_name="live",
        command_argv=["vllm", "serve", "fake/model"],
        command_hash="sha256:live",
        pid=123,
        pgid=123,
        process_create_time=1.0,
        executable="vllm",
        cwd=str(tmp_path),
        launch_mode="detached",
        host="127.0.0.1",
        port=18001,
        served_model_names=["fake"],
        exposure="local",
        manifest_path=str(manifest_path),
    ).write_atomic(sidecar_path)

    def fake_run(args, **_kwargs):
        assert args[:2] == ["ss", "-ltn"]
        return SimpleNamespace(
            returncode=0,
            stdout="LISTEN 0 128 127.0.0.1:18002 0.0.0.0:*\n",
            stderr="",
        )

    agent = LocalAgent()
    agent._detached_sidecar_paths["run-live"] = sidecar_path
    monkeypatch.setattr(local_agent_module, "verify_sidecar_from_system", lambda _path: True)
    monkeypatch.setattr(local_agent_module.subprocess, "run", fake_run)

    result = _call(
        agent,
        "compose_config",
        {
            "configs_dir": str(config_dir),
            "name": "new",
            "runtime": "process",
            "model": "Qwen/Qwen3-32B",
        },
    )

    assert result["config"]["server"]["port"] == 18003
    assert any(
        item["field"] == "server.port" and item["value"] == "18003"
        for item in result["derived"]
    )


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
    assert docker["container_name"] == "qwen36-27b-fp8-kvfp8-rp6000-vela"
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
    assert config["vllm"] | {"require_flags": []} == {
        "version_profile": "current",
        "version": "0.20.2rc1.dev9+g01d4d1ad3",
        "transformers_version": "5.7.0",
        "torch_version": "2.11.0+cu130",
        "cuda_version": "13.0",
        "require_flags": [],
    }
    assert "blackwell-fp8-runtime-recipe-required" not in result["warnings"]
    assert any(
        item["field"] == "deployment.recipe"
        and item["value"] == "blackbird-qwen36-27b-fp8-rp6000"
        for item in result["derived"]
    )


def test_agent_composer_preserves_blackbird_recipe_container_name_when_live_name_exists(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        local_agent_module,
        "_docker_container_names",
        lambda: {"qwen36-27b-fp8-kvfp8-rp6000-vela"},
        raising=False,
    )
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

    assert (
        result["config"]["command"]["docker"]["container_name"]
        == "qwen36-27b-fp8-kvfp8-rp6000-vela"
    )
    assert "container-name-reassigned" not in result["warnings"]


def test_agent_composer_keeps_lab_recipe_image_when_runtime_image_differs(
    config_dir: Path,
) -> None:
    agent = LocalAgent()

    result = _call(
        agent,
        "compose_config",
        {
            "configs_dir": str(config_dir),
            "name": "qwen36-27b-fp8-kvfp8-rp6000-blackbird",
            "target": "blackbird",
            "runtime": {
                "kind": "docker",
                "image": "vllm/vllm-openai@sha256:not-the-blackbird-stack",
            },
            "model": "Qwen/Qwen3.6-27B-FP8",
        },
    )

    assert result["config"]["command"]["docker"]["image"] == BLACKBIRD_IMAGE
    assert "recipe-image-override-ignored" in result["warnings"]


def test_agent_refuses_blackbird_fp8_docker_without_lab_recipe(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_path = tmp_path / "state" / "vela" / "models" / "registry.json"
    _write_experimental_fp8_registry(registry_path)
    monkeypatch.setattr(composer_module, "_load_hf_model_config", lambda *_args, **_kwargs: {})
    agent = LocalAgent(models_registry_path=registry_path)

    with pytest.raises(TargetCallError) as excinfo:
        _call(
            agent,
            "compose_config",
            {
                "configs_dir": str(config_dir),
                "name": "experimental-fp8",
                "target": "blackbird",
                "runtime": {
                    "kind": "docker",
                    "image": "vllm/vllm-openai@sha256:experimental",
                },
                "model_ref": "experimental-fp8",
            },
        )

    assert excinfo.value.code == "compose-invalid"
    assert "blackwell-fp8-runtime-recipe-required" in excinfo.value.message


def test_agent_allows_blackbird_fp8_named_model_with_explicit_bf16_override(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_path = tmp_path / "state" / "vela" / "models" / "registry.json"
    _write_experimental_fp8_registry(registry_path)
    monkeypatch.setattr(composer_module, "_load_hf_model_config", lambda *_args, **_kwargs: {})
    agent = LocalAgent(models_registry_path=registry_path)

    result = _call(
        agent,
        "compose_config",
        {
            "configs_dir": str(config_dir),
            "name": "experimental-fp8-bf16-kv",
            "target": "blackbird",
            "runtime": {
                "kind": "docker",
                "image": "vllm/vllm-openai@sha256:operator-pinned",
            },
            "model_ref": "experimental-fp8",
            "overrides": {"engine": {"kv_cache_dtype": "bfloat16"}},
        },
    )

    config = result["config"]
    assert config["engine"]["kv_cache_dtype"] == "bfloat16"
    assert (
        config["command"]["docker"]["image"]
        == "vllm/vllm-openai@sha256:operator-pinned"
    )
    assert "blackwell-fp8-runtime-recipe-required" not in result["warnings"]


def test_agent_refuses_blackbird_fp8_extra_args_even_with_bf16_engine_override(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_path = tmp_path / "state" / "vela" / "models" / "registry.json"
    _write_experimental_fp8_registry(registry_path)
    monkeypatch.setattr(composer_module, "_load_hf_model_config", lambda *_args, **_kwargs: {})
    agent = LocalAgent(models_registry_path=registry_path)

    with pytest.raises(TargetCallError) as excinfo:
        _call(
            agent,
            "compose_config",
            {
                "configs_dir": str(config_dir),
                "name": "experimental-fp8-passthrough",
                "target": "blackbird",
                "runtime": {
                    "kind": "docker",
                    "image": "vllm/vllm-openai@sha256:operator-pinned",
                },
                "model_ref": "experimental-fp8",
                "overrides": {
                    "engine": {"kv_cache_dtype": "bfloat16"},
                    "extra_args": ["--kv-cache-dtype", "fp8"],
                },
            },
        )

    assert excinfo.value.code == "compose-invalid"
    assert "blackwell-fp8-runtime-recipe-required" in excinfo.value.message


def test_agent_suggestions_warn_when_blackbird_fp8_docker_lacks_lab_recipe(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_path = tmp_path / "state" / "vela" / "models" / "registry.json"
    _write_experimental_fp8_registry(registry_path)
    monkeypatch.setattr(composer_module, "_load_hf_model_config", lambda *_args, **_kwargs: {})
    agent = LocalAgent(models_registry_path=registry_path)

    result = _call(
        agent,
        "suggest_deployment_defaults",
        {
            "configs_dir": str(config_dir),
            "name": "experimental-fp8",
            "target": "blackbird",
            "runtime": {
                "kind": "docker",
                "image": "vllm/vllm-openai@sha256:experimental",
            },
            "model_ref": "experimental-fp8",
        },
    )

    assert result["engine_suggestions"]["kv_cache_dtype"] == "fp8"
    assert "blackwell-fp8-runtime-recipe-required" in result["warnings"]


def test_agent_composer_ignores_existing_same_name_config_port_for_idempotent_create(
    config_dir: Path,
) -> None:
    write_yaml(
        config_dir / "qwen36-27b-fp8-kvfp8-rp6000-blackbird.yaml",
        """
        name: qwen36-27b-fp8-kvfp8-rp6000-blackbird
        model: Qwen/Qwen3.6-27B-FP8
        served_model_name: qwen36-27b-fp8-kvfp8-rp6000
        command:
          runtime: docker
          docker:
            image: vllm/vllm-openai@sha256:abc
            container_name: qwen36-27b-fp8-kvfp8-rp6000-vela
        server:
          host: 0.0.0.0
          port: 18003
          exposure: lan
        """,
    )
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

    assert result["config"]["server"]["port"] == 18003
    assert "port-reassigned" not in result["warnings"]


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
    assert docker["container_name"] == "qwen36-27b-bf16-rp6000-vela"
    assert docker["shm_size"] == "32g"
    assert "FLASHINFER_CUDA_ARCH_LIST" not in docker["env"]
    assert "--kv-cache-memory-bytes" not in config["extra_args"]
    assert "--attention-backend" not in config["extra_args"]


def test_agent_migrates_blackbird_fp8_wrapper_to_native_docker_recipe(
    config_dir: Path,
) -> None:
    write_yaml(
        config_dir / "legacy-fp8.yaml",
        """
        name: legacy-fp8
        target: blackbird
        model: Qwen/Qwen3.6-27B-FP8
        served_model_name: qwen36-27b-fp8-kvfp8-rp6000
        command:
          entrypoint: serve
          executable: ./scripts/blackbird_qwen36_vllm_foreground.sh
        server:
          host: 0.0.0.0
          port: 18003
          exposure: lan
        """,
    )
    agent = LocalAgent()

    result = _call(
        agent,
        "migrate_wrapper_config",
        {
            "configs_dir": str(config_dir),
            "src_name": "legacy-fp8",
            "new_name": "legacy-fp8-native",
            "dry_run": True,
        },
    )

    config = result["config"]
    docker = config["command"]["docker"]
    assert result["written"] is False
    assert not (config_dir / "legacy-fp8-native.yaml").exists()
    assert config["command"]["runtime"] == "docker"
    assert config["server"]["port"] == 18003
    assert docker["image"] == BLACKBIRD_IMAGE
    assert docker["container_name"] == "qwen36-27b-fp8-kvfp8-rp6000-vela"
    assert docker["env"]["FLASHINFER_CUDA_ARCH_LIST"] == "12.0f"
    assert "/root/.cache/flashinfer" in "\n".join(docker["volumes"])
    assert "--attention-backend" in config["extra_args"]
    assert "FLASHINFER" in config["extra_args"]
    assert "--kv-cache-memory-bytes" in config["extra_args"]
    assert "64424509440" in config["extra_args"]
    assert "wrapper-migration-review-required" in result["warnings"]
    assert any(
        item["field"] == "deployment.recipe"
        and item["value"] == "blackbird-qwen36-27b-fp8-rp6000"
        for item in result["derived"]
    )


def test_agent_migrates_blackbird_bf16_wrapper_without_fp8_pins(
    config_dir: Path,
) -> None:
    write_yaml(
        config_dir / "legacy-bf16.yaml",
        """
        name: legacy-bf16
        target: blackbird
        model: Qwen/Qwen3.6-27B
        served_model_name: qwen36-27b-bf16-rp6000
        command:
          entrypoint: serve
          executable: ./scripts/blackbird_qwen36_bf16_vllm_foreground.sh
        server:
          host: 0.0.0.0
          port: 18002
          exposure: lan
        """,
    )
    agent = LocalAgent()

    result = _call(
        agent,
        "migrate_wrapper_config",
        {
            "configs_dir": str(config_dir),
            "src_name": "legacy-bf16",
            "new_name": "legacy-bf16-native",
            "dry_run": True,
        },
    )

    config = result["config"]
    docker = config["command"]["docker"]
    assert config["server"]["port"] == 18002
    assert docker["container_name"] == "qwen36-27b-bf16-rp6000-vela"
    assert docker["env"]["CUDA_VISIBLE_DEVICES"] == "0"
    assert "FLASHINFER_CUDA_ARCH_LIST" not in docker["env"]
    assert "--kv-cache-memory-bytes" not in config["extra_args"]
    assert "--attention-backend" not in config["extra_args"]
    assert "/home/bgconley/models/qwen36-27b-bf16:/home/bgconley/models/qwen36-27b-bf16" in (
        docker["volumes"]
    )


def test_agent_refuses_unknown_wrapper_migration(config_dir: Path) -> None:
    write_yaml(
        config_dir / "legacy.yaml",
        """
        name: legacy
        target: blackbird
        model: Qwen/Qwen3.6-27B-FP8
        command:
          entrypoint: serve
          executable: ./scripts/custom-wrapper.sh
        """,
    )
    agent = LocalAgent()

    with pytest.raises(TargetCallError) as exc_info:
        _call(
            agent,
            "migrate_wrapper_config",
            {
                "configs_dir": str(config_dir),
                "src_name": "legacy",
                "dry_run": True,
            },
        )

    assert exc_info.value.code == "invalid-config"
    assert "unsupported wrapper script" in str(exc_info.value)


def test_agent_composer_preserves_blackbird_recipe_when_preset_changes(config_dir: Path) -> None:
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
            "preset": "throughput",
        },
    )

    config = result["config"]
    docker = config["command"]["docker"]
    assert config["extra_args"] == list(composer_module.QWEN36_FP8_EXTRA_ARGS)
    assert docker["image"] == BLACKBIRD_IMAGE
    assert docker["env"]["FLASHINFER_CUDA_ARCH_LIST"] == "12.0f"
    assert config["engine"]["kv_cache_dtype"] == "fp8"


def test_agent_lists_lab_deployment_recipes_for_tui() -> None:
    agent = LocalAgent()

    result = _call(agent, "list_deployment_recipes", {"target": "blackbird"})

    recipes = result["recipes"]
    fp8 = next(
        recipe
        for recipe in recipes
        if recipe["key"] == "blackbird-qwen36-27b-fp8-rp6000"
    )
    bf16 = next(
        recipe
        for recipe in recipes
        if recipe["key"] == "blackbird-qwen36-27b-bf16-rp6000"
    )
    assert fp8["label"] == "Blackbird Qwen3.6 27B FP8 RP6000"
    assert fp8["target"] == "blackbird"
    assert fp8["runtime"] == "docker"
    assert fp8["model"] == "Qwen/Qwen3.6-27B-FP8"
    assert fp8["image"] == BLACKBIRD_IMAGE
    assert fp8["server"]["port"] == 18003
    assert fp8["engine"]["kv_cache_dtype"] == "fp8"
    assert fp8["docker"]["env"]["FLASHINFER_CUDA_ARCH_LIST"] == "12.0f"
    assert "--kv-cache-memory-bytes" in fp8["extra_args"]
    assert fp8["source_artifacts"] == [
        "infx/qwen36-27b-test/start-qwen36-27b-fp8-rp6000-blackbird.sh",
        (
            "infx/qwen36-27b-test/"
            "qwen36-27b-fp8-bf16-stack-redeploy-blackbird-20260528.md"
        ),
    ]
    assert bf16["model"] == "Qwen/Qwen3.6-27B"
    assert bf16["server"]["port"] == 18002
    assert bf16["engine"]["kv_cache_dtype"] == "bfloat16"
    assert bf16["vllm"] == {
        "version_profile": "current",
        "version": "0.20.2rc1.dev9+g01d4d1ad3",
        "transformers_version": "5.7.0",
        "torch_version": "2.11.0+cu130",
        "cuda_version": "13.0",
    }
    assert "--kv-cache-memory-bytes" not in bf16["extra_args"]
    assert "FLASHINFER_CUDA_ARCH_LIST" not in bf16["docker"]["env"]
    assert bf16["source_artifacts"] == [
        "infx/qwen36-27b-test/start-qwen36-bf16-rp6000-blackbird.sh",
        "infx/qwen36-27b-test/qwen-bf16-rp6000-blackbird-reload-20260509.md",
    ]


def test_agent_lists_oxcart_recipe_only_on_oxcart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = LocalAgent()

    monkeypatch.setattr(local_agent_module.platform, "node", lambda: "workstation")
    recipes = _call(agent, "list_deployment_recipes", {"target": "local"})["recipes"]
    assert OXCART_RECIPE_KEY not in {recipe["key"] for recipe in recipes}

    monkeypatch.setattr(local_agent_module.platform, "node", lambda: "oxcart")
    recipes = _call(agent, "list_deployment_recipes", {"target": "local"})["recipes"]
    oxcart = next(recipe for recipe in recipes if recipe["key"] == OXCART_RECIPE_KEY)
    assert oxcart["required_hostname"] == "oxcart"
    assert oxcart["revision"] == "e89b16ebf1988b3d6befa7de50abc2d76f26eb09"


def test_agent_explicit_custom_disables_automatic_recipe_matching(
    config_dir: Path,
) -> None:
    result = _call(
        LocalAgent(),
        "compose_config",
        {
            "configs_dir": str(config_dir),
            "name": "custom-bf16",
            "target": "blackbird",
            "runtime": {
                "kind": "docker",
                "image": "example/custom@sha256:abc",
            },
            "model": "Qwen/Qwen3.6-27B",
            "recipe": "__custom__",
        },
    )

    config = result["config"]
    assert config["served_model_name"] == "Qwen3.6-27B"
    assert config["command"]["docker"]["image"] == "example/custom@sha256:abc"
    assert not any(item["field"] == "deployment.recipe" for item in result["derived"])


def test_agent_explicit_null_recipe_disables_automatic_recipe_suggestions(
    config_dir: Path,
) -> None:
    result = _call(
        LocalAgent(),
        "suggest_deployment_defaults",
        {
            "configs_dir": str(config_dir),
            "target": "blackbird",
            "runtime": "docker",
            "model": "Qwen/Qwen3.6-27B",
            "recipe": None,
        },
    )

    assert "recipe" not in result
    assert not any(source.startswith("lab_recipe:") for source in result["sources"])


@pytest.mark.parametrize(
    ("changes", "expected_field"),
    [
        ({"target": "blackbird"}, "target"),
        ({"runtime": "process"}, "runtime"),
        ({"model": "Qwen/Qwen3.6-27B"}, "model"),
    ],
)
def test_agent_rejects_explicit_recipe_field_mismatches(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    changes: dict[str, object],
    expected_field: str,
) -> None:
    monkeypatch.setattr(local_agent_module.platform, "node", lambda: "oxcart")
    params: dict[str, object] = {
        "configs_dir": str(config_dir),
        "target": "local",
        "runtime": "docker",
        "model": "Qwen/Qwen3.6-27B-FP8",
        "recipe": OXCART_RECIPE_KEY,
    }
    params.update(changes)

    with pytest.raises(TargetCallError) as exc_info:
        _call(LocalAgent(), "compose_config", params)

    assert exc_info.value.code == "compose-invalid"
    assert expected_field in exc_info.value.message


def test_agent_rejects_oxcart_recipe_on_another_hostname(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(local_agent_module.platform, "node", lambda: "not-oxcart")

    with pytest.raises(TargetCallError) as exc_info:
        _call(
            LocalAgent(),
            "compose_config",
            {
                "configs_dir": str(config_dir),
                "target": "local",
                "runtime": "docker",
                "model": "Qwen/Qwen3.6-27B-FP8",
                "recipe": OXCART_RECIPE_KEY,
            },
        )

    assert exc_info.value.code == "compose-invalid"
    assert "hostname" in exc_info.value.message
    assert "oxcart" in exc_info.value.message


def test_agent_composes_oxcart_recipe_exactly_from_checked_in_profile(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(local_agent_module.platform, "node", lambda: "oxcart")
    agent = LocalAgent()
    monkeypatch.setattr(agent, "_occupied_port_sources", lambda: {})
    monkeypatch.setattr(agent, "_occupied_docker_container_names", lambda: set())

    result = _call(
        agent,
        "compose_config",
        {
            "configs_dir": str(config_dir),
            "target": "local",
            "runtime": "docker",
            "model": "Qwen/Qwen3.6-27B-FP8",
            "recipe": OXCART_RECIPE_KEY,
        },
    )
    profile_path = (
        Path(__file__).parents[1] / "configs" / "oxcart-qwen36-27b-fp8-mtp-vl.yaml"
    )
    expected = ModelConfig.model_validate(
        yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    ).model_dump(mode="json")

    assert result["config"] == expected


def test_oxcart_review_provenance_names_every_release_critical_identity(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The review payload must explain the exact Oxcart launch, not mislabel it auto."""
    monkeypatch.setattr(local_agent_module.platform, "node", lambda: "oxcart")
    agent = LocalAgent()
    monkeypatch.setattr(agent, "_occupied_port_sources", lambda: {})
    monkeypatch.setattr(agent, "_occupied_docker_container_names", lambda: set())

    result = _call(
        agent,
        "compose_config",
        {
            "configs_dir": str(config_dir),
            "target": "local",
            "runtime": "docker",
            "model": "Qwen/Qwen3.6-27B-FP8",
            "recipe": OXCART_RECIPE_KEY,
        },
    )
    provenance = {item["field"]: item for item in result["derived"]}
    recipe_source = f"lab_recipe:{OXCART_RECIPE_KEY}"

    for field in (
        "model",
        "model_ref",
        "revision",
        "served_model_name",
        "server.host",
        "server.port",
        "server.exposure",
        "launch.runs_dir",
        "launch.required_hostname",
        "launch.require_cached_models",
        "command.docker.image",
        "command.docker.container_name",
        "command.docker.hf_cache",
        "command.docker.hf_cache_target",
        "command.docker.volumes",
        "command.docker.auto_remove",
        "command.docker.extra_run_args",
        "extra_args",
    ):
        assert provenance[field]["source"] == recipe_source, field
    assert provenance["revision"]["value"] == OXCART_COMMIT
    assert provenance["server.port"]["value"] == "18004"
    assert provenance["command.docker.image"]["value"] == BLACKBIRD_IMAGE
    assert provenance["command.docker.auto_remove"]["value"] == "true"
    assert provenance["command.docker.evict"] == {
        "field": "command.docker.evict",
        "value": "[]",
        "source": "schema_default",
    }
    assert "/tank/ai/models/qwen36-27b-fp8/hf-cache" in provenance[
        "command.docker.hf_cache"
    ]["value"]
    assert "ai.vela.managed=true" in provenance["command.docker.extra_run_args"][
        "value"
    ]


def test_oxcart_wizard_echoed_recipe_fields_keep_recipe_provenance(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recipe-populated UI fields are not operator overrides unless changed."""
    monkeypatch.setattr(local_agent_module.platform, "node", lambda: "oxcart")
    agent = LocalAgent()
    monkeypatch.setattr(agent, "_occupied_port_sources", lambda: {})
    monkeypatch.setattr(agent, "_occupied_docker_container_names", lambda: set())

    result = _call(
        agent,
        "compose_config",
        {
            "configs_dir": str(config_dir),
            "target": "local",
            "runtime": {
                "kind": "docker",
                "image": BLACKBIRD_IMAGE,
            },
            "model": "Qwen/Qwen3.6-27B-FP8",
            "recipe": OXCART_RECIPE_KEY,
            "overrides": {
                "served_model_name": "qwen36-27b-fp8-oxcart",
                "container_name": "vela-oxcart-qwen36-27b-fp8-mtp-vl",
                "server": {
                    "host": "127.0.0.1",
                    "port": 18004,
                    "exposure": "local",
                },
                "launch": {
                    "runs_dir": (
                        "/tank/ai/models/qwen36-27b-fp8/"
                        "vllm-rp6000-mtp-vl/vela-runs"
                    )
                },
            },
        },
    )
    provenance = {item["field"]: item for item in result["derived"]}
    recipe_source = f"lab_recipe:{OXCART_RECIPE_KEY}"

    for field in (
        "served_model_name",
        "server.host",
        "server.port",
        "server.exposure",
        "launch.runs_dir",
        "command.docker.container_name",
    ):
        assert provenance[field]["source"] == recipe_source, field


def test_agent_composes_oxcart_recipe_on_matching_fqdn(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        local_agent_module.platform,
        "node",
        lambda: "oxcart.lab.conley.ai",
    )
    agent = LocalAgent()
    monkeypatch.setattr(agent, "_occupied_port_sources", lambda: {})
    monkeypatch.setattr(agent, "_occupied_docker_container_names", lambda: set())

    result = _call(
        agent,
        "compose_config",
        {
            "configs_dir": str(config_dir),
            "target": "local",
            "runtime": "docker",
            "model": "Qwen/Qwen3.6-27B-FP8",
            "recipe": OXCART_RECIPE_KEY,
        },
    )

    assert result["config"]["launch"]["required_hostname"] == "oxcart"
    assert any(
        item == {
            "field": "deployment.recipe",
            "value": OXCART_RECIPE_KEY,
            "source": "lab_recipe",
        }
        for item in result["derived"]
    )


def test_oxcart_recipe_preserves_concrete_selected_pin_among_multiple_pins(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_path = tmp_path / "state" / "vela" / "models" / "registry.json"
    _write_oxcart_multi_pin_registry(registry_path)
    monkeypatch.setattr(local_agent_module.platform, "node", lambda: "oxcart")
    agent = LocalAgent(models_registry_path=registry_path)
    monkeypatch.setattr(agent, "_occupied_port_sources", lambda: {})
    monkeypatch.setattr(agent, "_occupied_docker_container_names", lambda: set())

    result = _call(
        agent,
        "compose_config",
        {
            "configs_dir": str(config_dir),
            "target": "local",
            "runtime": "docker",
            "model_ref": "01OXCARTEXACT",
            "recipe": OXCART_RECIPE_KEY,
        },
    )

    assert result["config"]["model_ref"] == "01OXCARTEXACT"
    assert result["config"]["revision"] == OXCART_COMMIT
    provenance = {item["field"]: item for item in result["derived"]}
    assert provenance["model"]["source"] == "model_registry"
    assert provenance["model_ref"]["source"] == "model_registry:selected_pin"
    assert provenance["revision"] == {
        "field": "revision",
        "value": OXCART_COMMIT,
        "source": "model_registry:resolved_commit",
    }


def test_oxcart_recipe_rejects_selected_pin_at_another_commit(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_path = tmp_path / "state" / "vela" / "models" / "registry.json"
    _write_oxcart_multi_pin_registry(registry_path)
    monkeypatch.setattr(local_agent_module.platform, "node", lambda: "oxcart")
    agent = LocalAgent(models_registry_path=registry_path)
    monkeypatch.setattr(agent, "_occupied_port_sources", lambda: {})
    monkeypatch.setattr(agent, "_occupied_docker_container_names", lambda: set())

    with pytest.raises(TargetCallError) as exc_info:
        _call(
            agent,
            "compose_config",
            {
                "configs_dir": str(config_dir),
                "target": "local",
                "runtime": "docker",
                "model_ref": "01OXCARTSTALE",
                "recipe": OXCART_RECIPE_KEY,
            },
        )

    assert exc_info.value.code == "compose-invalid"
    assert "01OXCARTSTALE" in exc_info.value.message
    assert OXCART_COMMIT in exc_info.value.message


def test_agent_lists_spec_aligned_composer_presets_for_wizard() -> None:
    result = _call(LocalAgent(), "list_presets", {})
    presets = {item["name"]: item for item in result["presets"]}

    assert "--enable-chunked-prefill" in presets["balanced"]["extra_args"]
    assert "--max-num-batched-tokens" in presets["throughput"]["extra_args"]
    assert "--compilation-config" in presets["throughput"]["extra_args"]
    assert presets["long-context"]["engine"]["max_model_len"] == 131072
    assert presets["low-memory"]["engine"]["enforce_eager"] is True


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


def test_agent_suggests_fresh_docker_container_name_from_docker_ps(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        local_agent_module,
        "_docker_container_names",
        lambda: {"vela-qwen3"},
        raising=False,
    )
    agent = LocalAgent()

    result = _call(
        agent,
        "suggest_deployment_defaults",
        {
            "configs_dir": str(config_dir),
            "name": "qwen3",
            "runtime": {"kind": "docker", "image": "vllm/vllm-openai@sha256:abc"},
            "model": "Qwen/Qwen3-32B",
        },
    )

    assert result["container_name"] == "vela-qwen3-2"
    assert "container-name-reassigned" in result["warnings"]


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


def test_composer_persists_selected_pin_as_immutable_commit_not_symbolic_ref(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_path = tmp_path / "state" / "vela" / "models" / "registry.json"
    _write_model_registry(registry_path)
    monkeypatch.setattr(composer_module, "_load_hf_model_config", lambda *_a, **_k: {})
    agent = LocalAgent(models_registry_path=registry_path)

    result = _call(
        agent,
        "compose_config",
        {
            "configs_dir": str(config_dir),
            "name": "immutable-pin",
            "runtime": "process",
            "model_ref": "qwen-fp8",
            "revision": "main",
            "recipe": "__custom__",
        },
    )
    saved = _call(
        agent,
        "save_config",
        {
            "configs_dir": str(config_dir),
            "name": "immutable-pin",
            "config": result["config"],
        },
    )

    assert result["config"]["model_ref"] == "qwen-fp8"
    assert result["config"]["revision"] == "abc123"
    stored = yaml.safe_load(Path(saved["path"]).read_text(encoding="utf-8"))
    assert stored["model_ref"] == "qwen-fp8"
    assert stored["revision"] == "abc123"


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


def test_composer_warns_bare_process_cannot_reinstantiate_exactly(
    config_dir: Path,
) -> None:
    agent = LocalAgent()
    bare = _call(
        agent,
        "compose_config",
        {
            "configs_dir": str(config_dir),
            "name": "bare-process",
            "runtime": "process",
            "model": "org/model",
        },
    )
    warning_text = "\n".join(bare["warnings"])
    assert "current mutable agent environment" in warning_text
    assert "cannot promise exact reinstantiation" in warning_text

    pinned_build = _call(
        agent,
        "compose_config",
        {
            "configs_dir": str(config_dir),
            "name": "pinned-build",
            "runtime": {"kind": "build", "build": "01BUILD"},
            "model": "org/model",
        },
    )
    assert "cannot promise exact reinstantiation" not in "\n".join(
        pinned_build["warnings"]
    )

    executable = _call(
        agent,
        "compose_config",
        {
            "configs_dir": str(config_dir),
            "name": "explicit-executable",
            "runtime": {"kind": "executable", "executable": "/opt/vllm/bin/vllm"},
            "model": "org/model",
        },
    )
    assert "cannot promise exact reinstantiation" not in "\n".join(
        executable["warnings"]
    )


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


def test_agent_clones_deployment_with_fresh_runtime_identity(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_yaml(
        config_dir / "source.yaml",
        """
        name: source
        model: Qwen/Qwen3.6-27B-FP8
        command:
          runtime: docker
          docker:
            image: vllm/vllm-openai@sha256:abc
            container_name: vela-source
        server:
          port: 18003
        launch:
          runs_dir: /tmp/vela-runs/source
        """,
    )
    agent = LocalAgent()
    monkeypatch.setattr(local_agent_module, "_listening_ports", lambda: {18000, 18001})

    result = _call(
        agent,
        "clone_config",
        {
            "configs_dir": str(config_dir),
            "src_name": "source",
            "new_name": "copy",
        },
    )

    config = result["config"]
    assert result["path"] == str(config_dir / "copy.yaml")
    assert config["name"] == "copy"
    assert config["server"]["port"] == 18002
    assert config["launch"]["runs_dir"] == "/tmp/vela-runs/copy"
    assert config["command"]["docker"]["container_name"] == "vela-copy"
    assert (config_dir / "copy.yaml").exists()
    assert any(item["field"] == "server.port" for item in result["derived"])


def test_agent_clone_config_blocks_literal_secret_overrides(config_dir: Path) -> None:
    write_yaml(
        config_dir / "source.yaml",
        """
        name: source
        model: Qwen/Qwen3.6-27B-FP8
        command:
          runtime: docker
          docker:
            image: vllm/vllm-openai@sha256:abc
        server:
          port: 18003
        """,
    )
    agent = LocalAgent()

    with pytest.raises(TargetCallError) as exc_info:
        _call(
            agent,
            "clone_config",
            {
                "configs_dir": str(config_dir),
                "src_name": "source",
                "new_name": "copy",
                "overrides": {"server": {"api_key": "sk-live-secret"}},
            },
        )

    assert exc_info.value.code == "invalid-config"
    assert exc_info.value.details["new_name"] == "copy"
    validation = exc_info.value.details["validation"]
    assert validation["ok"] is False
    assert validation["errors"] == [
        {
            "field": "server.api_key",
            "message": "contains a literal secret; prefer target env injection",
        }
    ]
    assert not (config_dir / "copy.yaml").exists()


def test_agent_clone_config_blocks_literal_secret_from_source(config_dir: Path) -> None:
    write_yaml(
        config_dir / "source.yaml",
        """
        name: source
        model: Qwen/Qwen3.6-27B-FP8
        command:
          runtime: docker
          docker:
            image: vllm/vllm-openai@sha256:abc
        env:
          HF_TOKEN: hf_live_secret
        server:
          port: 18003
        """,
    )
    agent = LocalAgent()

    with pytest.raises(TargetCallError) as exc_info:
        _call(
            agent,
            "clone_config",
            {
                "configs_dir": str(config_dir),
                "src_name": "source",
                "new_name": "copy",
            },
        )

    assert exc_info.value.code == "invalid-config"
    validation = exc_info.value.details["validation"]
    assert validation["ok"] is False
    assert validation["errors"] == [
        {
            "field": "env.HF_TOKEN",
            "message": "contains a literal secret; prefer target env injection",
        }
    ]
    assert not (config_dir / "copy.yaml").exists()


def test_agent_clone_config_writes_clean_config(config_dir: Path) -> None:
    write_yaml(
        config_dir / "source.yaml",
        """
        name: source
        model: Qwen/Qwen3.6-27B-FP8
        command:
          runtime: docker
          docker:
            image: vllm/vllm-openai@sha256:abc
        server:
          port: 18003
        """,
    )
    agent = LocalAgent()

    result = _call(
        agent,
        "clone_config",
        {
            "configs_dir": str(config_dir),
            "src_name": "source",
            "new_name": "copy",
        },
    )

    path = Path(result["path"])
    assert path == config_dir / "copy.yaml"
    assert path.exists()
    assert (path.stat().st_mode & 0o777) == 0o644
    assert result["config"]["name"] == "copy"


def test_agent_clones_docker_config_with_fresh_container_name_from_docker_ps(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_yaml(
        config_dir / "source.yaml",
        """
        name: source
        model: Qwen/Qwen3.6-27B-FP8
        command:
          runtime: docker
          docker:
            image: vllm/vllm-openai@sha256:abc
            container_name: vela-source
        launch:
          runs_dir: /tmp/vela-runs/source
        """,
    )
    monkeypatch.setattr(
        local_agent_module,
        "_docker_container_names",
        lambda: {"vela-copy"},
        raising=False,
    )
    agent = LocalAgent()

    result = _call(
        agent,
        "clone_config",
        {
            "configs_dir": str(config_dir),
            "src_name": "source",
            "new_name": "copy",
        },
    )

    assert result["config"]["command"]["docker"]["container_name"] == "vela-copy-2"
    assert any(
        item["field"] == "command.docker.container_name"
        and item["source"] == "docker_container_name_collision"
        for item in result["derived"]
    )


def test_agent_clone_discloses_regenerated_docker_ownership_identity(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_yaml(
        config_dir / "source.yaml",
        """
        name: source
        model: org/model
        command:
          runtime: docker
          docker:
            image: vllm/vllm-openai@sha256:abc
            container_name: vela-source
            evict: [vela-source, shared-cache]
            extra_run_args: [--label, ai.vela.profile=source, --label, keep=yes]
        """,
    )
    monkeypatch.setattr(local_agent_module, "_docker_container_names", lambda: set())

    result = _call(
        LocalAgent(),
        "clone_config",
        {
            "configs_dir": str(config_dir),
            "src_name": "source",
            "new_name": "copy",
        },
    )

    docker = result["config"]["command"]["docker"]
    assert docker["evict"] == ["shared-cache"]
    assert docker["extra_run_args"] == [
        "--label",
        "ai.vela.profile=copy",
        "--label",
        "keep=yes",
    ]
    assert {
        (item["field"], item["source"])
        for item in result["derived"]
    } >= {
        ("command.docker.evict", "clone_source_eviction_removed"),
        ("command.docker.extra_run_args", "clone_profile_label"),
    }


def test_agent_clone_sanitizes_source_identity_with_container_name_override(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_yaml(
        config_dir / "source.yaml",
        """
        name: source
        model: org/model
        command:
          runtime: docker
          docker:
            image: vllm/vllm-openai@sha256:abc
            container_name: vela-source
            evict: [vela-source]
            extra_run_args: [--label, ai.vela.profile=source]
        """,
    )
    monkeypatch.setattr(local_agent_module, "_docker_container_names", lambda: set())

    result = _call(
        LocalAgent(),
        "clone_config",
        {
            "configs_dir": str(config_dir),
            "src_name": "source",
            "new_name": "copy",
            "overrides": {
                "command": {"docker": {"container_name": "explicit-copy-container"}}
            },
        },
    )

    docker = result["config"]["command"]["docker"]
    assert docker["container_name"] == "explicit-copy-container"
    assert docker["evict"] == []
    assert docker["extra_run_args"] == ["--label", "ai.vela.profile=copy"]
    assert {
        item["source"] for item in result["derived"]
    } >= {"clone_source_eviction_removed", "clone_profile_label"}


def test_agent_edits_deployment_config_with_overrides(config_dir: Path) -> None:
    write_yaml(
        config_dir / "editable.yaml",
        """
        name: editable
        model: Qwen/Qwen3-32B
        engine:
          dtype: auto
        server:
          host: 127.0.0.1
          port: 18001
        extra_args:
          - --enable-prefix-caching
        """,
    )
    agent = LocalAgent()

    result = _call(
        agent,
        "edit_config",
        {
            "configs_dir": str(config_dir),
            "name": "editable",
            "overrides": {
                "engine": {"dtype": "bfloat16", "max_num_seqs": 4},
                "server": {"port": 18009},
                "extra_args": ["--max-num-batched-tokens", "4096"],
            },
        },
    )

    config = result["config"]
    assert result["updated"] is True
    assert result["path"] == str(config_dir / "editable.yaml")
    assert config["engine"]["dtype"] == "bfloat16"
    assert config["engine"]["max_num_seqs"] == 4
    assert config["server"]["port"] == 18009
    assert config["extra_args"] == [
        "--enable-prefix-caching",
        "--max-num-batched-tokens",
        "4096",
    ]
    written = (config_dir / "editable.yaml").read_text(encoding="utf-8")
    assert "dtype: bfloat16" in written
    assert "port: 18009" in written


def test_agent_edit_config_blocks_literal_secret(config_dir: Path) -> None:
    write_yaml(
        config_dir / "editable.yaml",
        """
        name: editable
        model: Qwen/Qwen3-32B
        server:
          host: 127.0.0.1
          port: 18001
        """,
    )
    agent = LocalAgent()

    with pytest.raises(TargetCallError) as exc_info:
        _call(
            agent,
            "edit_config",
            {
                "configs_dir": str(config_dir),
                "name": "editable",
                "overrides": {"server": {"api_key": "sk-live-secret"}},
            },
    )

    assert exc_info.value.code == "invalid-config"
    assert exc_info.value.details["validation"]["errors"] == [
        {
            "field": "server.api_key",
            "message": "contains a literal secret; prefer target env injection",
        }
    ]
    written = (config_dir / "editable.yaml").read_text(encoding="utf-8")
    assert "sk-live-secret" not in written
    assert "api_key" not in written


def test_agent_delete_config_refuses_live_run(config_dir: Path, tmp_path: Path) -> None:
    write_yaml(
        config_dir / "active.yaml",
        """
        name: active
        model: fake/model
        launch:
          runs_dir: /tmp/vela-runs/active
        """,
    )
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    log_path = runs_dir / "active.log"
    log_path.write_text("", encoding="utf-8")
    manifest_path = runs_dir / "active.manifest.json"
    Manifest.from_active_log(log_path).write_atomic(manifest_path)
    sidecar_path = runs_dir / "active.json"
    identity = process_identity_from_pid(os.getpid())
    Sidecar(
        run_id="run-live",
        config_name="active",
        command_argv=identity.cmdline,
        command_hash=command_hash(identity.cmdline),
        pid=identity.pid,
        pgid=identity.pgid,
        process_create_time=identity.create_time,
        procfs_starttime=identity.procfs_starttime,
        executable=identity.executable,
        cwd=str(tmp_path),
        launch_mode="attached",
        host="127.0.0.1",
        port=18000,
        served_model_names=["fake"],
        exposure="local",
        manifest_path=str(manifest_path),
    ).write_atomic(sidecar_path)
    agent = LocalAgent()
    agent._detached_sidecar_paths["run-live"] = sidecar_path

    with pytest.raises(TargetCallError) as exc_info:
        _call(agent, "delete_config", {"configs_dir": str(config_dir), "name": "active"})

    assert exc_info.value.code == "config-in-use"
    assert (config_dir / "active.yaml").exists()


def test_agent_delete_config_removes_inactive_file(config_dir: Path) -> None:
    write_yaml(
        config_dir / "old.yaml",
        """
        name: old
        model: fake/model
        """,
    )
    agent = LocalAgent()

    result = _call(agent, "delete_config", {"configs_dir": str(config_dir), "name": "old"})

    assert result == {"name": "old", "path": str(config_dir / "old.yaml"), "deleted": True}
    assert not (config_dir / "old.yaml").exists()


def test_agent_push_pull_list_and_lint_config_round_trip(config_dir: Path) -> None:
    agent = LocalAgent()
    raw_yaml = """
    name: pushed
    model: /models/pushed
    command:
      executable: /opt/vllm/bin/vllm
    server:
      host: 127.0.0.1
      port: 18008
    env:
      VLLM_LOGGING_LEVEL: DEBUG
    """

    pushed = _call(
        agent,
        "push_config",
        {
            "configs_dir": str(config_dir),
            "yaml": raw_yaml,
        },
    )

    config_path = config_dir / "pushed.yaml"
    assert pushed["name"] == "pushed"
    assert pushed["path"] == str(config_path)
    assert oct(config_path.stat().st_mode & 0o777) == "0o644"

    listed = _call(agent, "list_config_files", {"configs_dir": str(config_dir)})
    assert listed["valid"][0]["name"] == "pushed"
    assert listed["valid"][0]["path"] == str(config_path)

    pulled = _call(agent, "pull_config", {"configs_dir": str(config_dir), "name": "pushed"})
    assert pulled["config"]["name"] == "pushed"
    assert "name: pushed" in pulled["yaml"]
    assert "VLLM_LOGGING_LEVEL: DEBUG" in pulled["yaml"]

    linted = _call(agent, "lint_config", {"config": pulled["config"]})
    assert linted["ok"] is True
    warnings = "\n".join(linted["warnings"])
    assert "model uses a host-local absolute path" in warnings
    assert "command.executable is host-local" in warnings

    with pytest.raises(TargetCallError) as exc_info:
        _call(agent, "push_config", {"configs_dir": str(config_dir), "yaml": raw_yaml})
    assert exc_info.value.code == "config-exists"


def test_agent_lint_save_and_push_block_literal_config_secrets(config_dir: Path) -> None:
    agent = LocalAgent()
    secret_config = {
        "name": "secret-config",
        "model": "Qwen/Qwen3-32B",
        "server": {"host": "127.0.0.1", "port": 18008, "api_key": "sk-live"},
        "env": {"HF_TOKEN": "hf_live"},
        "command": {
            "runtime": "docker",
            "docker": {
                "image": "vllm/vllm-openai@sha256:abc",
                "env": {"DB_PASSWORD": "hunter2"},
            },
        },
    }
    raw_yaml = yaml.safe_dump(secret_config, sort_keys=False)

    linted = _call(agent, "lint_config", {"config": secret_config})

    assert linted["ok"] is False
    assert linted["warnings"] == []
    assert linted["errors"] == [
        {
            "field": "server.api_key",
            "message": "contains a literal secret; prefer target env injection",
        },
        {
            "field": "env.HF_TOKEN",
            "message": "contains a literal secret; prefer target env injection",
        },
        {
            "field": "command.docker.env.DB_PASSWORD",
            "message": "contains a literal secret; prefer target env injection",
        },
    ]

    with pytest.raises(TargetCallError) as push_exc:
        _call(agent, "push_config", {"configs_dir": str(config_dir), "yaml": raw_yaml})
    with pytest.raises(TargetCallError) as save_exc:
        _call(
            agent,
            "save_config",
            {"configs_dir": str(config_dir), "name": "secret-config", "config": secret_config},
        )

    assert push_exc.value.code == "invalid-config"
    assert save_exc.value.code == "invalid-config"
    assert not (config_dir / "secret-config.yaml").exists()


def test_agent_lints_docker_pinning_gpus_and_exposure_mismatch() -> None:
    agent = LocalAgent()

    linted = _call(
        agent,
        "lint_config",
        {
            "config": {
                "name": "docker-lint",
                "model": "Qwen/Qwen3-32B",
                "command": {
                    "runtime": "docker",
                    "docker": {
                        "image": "vllm/vllm-openai:latest",
                        "gpus": "",
                    },
                },
                "server": {
                    "host": "127.0.0.1",
                    "port": 18000,
                    "exposure": "lan",
                },
            }
        },
    )

    warnings = "\n".join(linted["warnings"])
    assert linted["ok"] is True
    assert "uses :latest" in warnings
    assert "not digest-pinned" in warnings
    assert "command.docker.gpus is blank" in warnings
    assert "server.exposure is lan but server.host is loopback" in warnings


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


def test_compose_overrides_served_name_runs_dir_and_container_name(
    config_dir: Path, tmp_path: Path
) -> None:
    # J28: the auto-derived identity fields are operator-overridable.
    agent = LocalAgent()
    result = _call(
        agent,
        "compose_config",
        {
            "configs_dir": str(config_dir),
            "name": "qwen3-32b",
            "target": "local",
            "runtime": {"kind": "docker", "image": "vllm/vllm-openai@sha256:abc"},
            "model": "Qwen/Qwen3-32B",
            "preset": "balanced",
            "overrides": {
                "served_model_name": "qwen-prod",
                "container_name": "vela-qwen-prod",
                "launch": {"runs_dir": str(tmp_path / "runs")},
            },
        },
    )
    config = result["config"]
    assert config["served_model_name"] == "qwen-prod"
    assert config["command"]["docker"]["container_name"] == "vela-qwen-prod"
    assert config["launch"]["runs_dir"] == str(tmp_path / "runs")


def test_compose_warns_when_world_size_exceeds_visible_gpus(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # J29: the TP-vs-GPU mismatch is an early compose-time advisory, not just
    # a save-time preflight failure.
    from vela.monitoring.gpu import GpuPollResult, GpuSample

    agent = LocalAgent()
    monkeypatch.setattr(
        agent,
        "sample_gpus",
        lambda: GpuPollResult(
            [
                GpuSample(
                    visible_index=0,
                    uuid="GPU-a",
                    name="Blackwell sm_120",
                    memory_used_mb=0,
                    memory_total_mb=96000,
                    utilization_percent=0,
                    temperature_c=40,
                    power_w=80,
                )
            ]
        ),
    )
    result = _call(
        agent,
        "compose_config",
        {
            "configs_dir": str(config_dir),
            "name": "qwen3",
            "target": "local",
            "runtime": "process",
            "model": "Qwen/Qwen3-32B",
            "preset": "balanced",
            "overrides": {"engine": {"tensor_parallel_size": 4}},
        },
    )
    warnings = [str(w) for w in result.get("warnings", [])]
    assert any("exceeds 1 visible GPU" in w for w in warnings)


# --- H3 (bug-284): docker composes mount the agent HF cache by default -------

_AGENT_HF_HOME = str(Path(hf_constants.HF_HOME))


def _write_source_registry(path: Path, *, source: str, tmp_path: Path) -> dict:
    if source == "hf_repo":
        specifics: dict[str, object] = {
            "repo_id": "org/plain-llm",
            "local_path": None,
            "url": None,
        }
    elif source == "local_path":
        model_dir = tmp_path / "local-model"
        model_dir.mkdir(parents=True, exist_ok=True)
        specifics = {"repo_id": None, "local_path": str(model_dir), "url": None}
    else:  # url
        specifics = {
            "repo_id": None,
            "local_path": None,
            "url": "https://example.com/model.gguf",
        }
    entry = {
        "entry_id": "01SRC",
        "display_name": f"{source}-model",
        "aliases": [source],
        "source": source,
        "revision": "main",
        "commit_sha": "abc123",
        "quant_format": "none",
        "tokenizer": None,
        "files": {},
        "size_bytes": 0,
        "cache_state": "cached",
        "gated": False,
        "token_required": False,
        "created_at": "2026-07-11T00:00:00Z",
        "last_used_at": None,
        "notes": "",
        **specifics,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "default_cache": "hf",
                "app_download_dir": None,
                "entries": [entry],
            }
        ),
        encoding="utf-8",
    )
    return entry


def test_generic_docker_pinned_hf_repo_mounts_agent_hf_cache_by_default(
    config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_path = tmp_path / "state" / "vela" / "models" / "registry.json"
    _write_source_registry(registry_path, source="hf_repo", tmp_path=tmp_path)
    monkeypatch.setattr(composer_module, "_load_hf_model_config", lambda *_a, **_k: {})
    agent = LocalAgent(models_registry_path=registry_path)

    result = _call(
        agent,
        "compose_config",
        {
            "configs_dir": str(config_dir),
            "name": "plain-docker",
            "runtime": {"kind": "docker", "image": "vllm/vllm-openai@sha256:abc"},
            "model_ref": "01SRC",
        },
    )

    docker = result["config"]["command"]["docker"]
    assert docker["hf_cache"] == _AGENT_HF_HOME


def test_generic_docker_bare_hf_repo_model_mounts_agent_hf_cache_by_default(
    config_dir: Path,
) -> None:
    agent = LocalAgent()

    result = _call(
        agent,
        "compose_config",
        {
            "configs_dir": str(config_dir),
            "name": "bare-docker",
            "runtime": {"kind": "docker", "image": "vllm/vllm-openai@sha256:abc"},
            "model": "Qwen/Qwen3-8B",
        },
    )

    docker = result["config"]["command"]["docker"]
    assert docker["hf_cache"] == _AGENT_HF_HOME


def test_generic_docker_explicit_hf_cache_wins_and_is_shown_in_preview(
    config_dir: Path, tmp_path: Path
) -> None:
    custom_hf_cache = tmp_path / "operator-hf-cache"
    agent = LocalAgent()

    result = _call(
        agent,
        "compose_config",
        {
            "configs_dir": str(config_dir),
            "name": "custom-cache-docker",
            "runtime": {
                "kind": "docker",
                "image": "vllm/vllm-openai@sha256:abc",
                "hf_cache": str(custom_hf_cache),
            },
            "model": "Qwen/Qwen3-8B",
        },
    )

    config = result["config"]
    assert config["command"]["docker"]["hf_cache"] == str(custom_hf_cache)

    preview = _call(
        agent,
        "preview",
        {"config": config, "configs_dir": str(config_dir)},
    )["preview"]
    assert f"{custom_hf_cache}:/root/.cache/huggingface:rw" in preview


def test_generic_docker_bare_local_path_model_has_no_auto_hf_cache_mount(
    config_dir: Path, tmp_path: Path
) -> None:
    # A local-path model gets its own volume handling; the composer must not
    # auto-mount the agent HF cache for it.
    model_dir = tmp_path / "local-model"
    model_dir.mkdir()
    agent = LocalAgent()

    result = _call(
        agent,
        "compose_config",
        {
            "configs_dir": str(config_dir),
            "name": "local-docker",
            "runtime": {"kind": "docker", "image": "vllm/vllm-openai@sha256:abc"},
            "model": str(model_dir),
        },
    )

    assert result["config"]["command"]["docker"]["hf_cache"] is None


def test_local_path_pin_composes_saves_and_previews_through_registry_identity(
    config_dir: Path, tmp_path: Path
) -> None:
    registry_path = tmp_path / "state" / "vela" / "models" / "registry.json"
    entry = _write_source_registry(registry_path, source="local_path", tmp_path=tmp_path)
    agent = LocalAgent(models_registry_path=registry_path)

    composed = _call(
        agent,
        "compose_config",
        {
            "configs_dir": str(config_dir),
            "name": "local-pinned",
            "runtime": "process",
            "model_ref": "01SRC",
            "recipe": "__custom__",
        },
    )["config"]
    saved = _call(
        agent,
        "save_config",
        {
            "configs_dir": str(config_dir),
            "name": "local-pinned",
            "config": composed,
        },
    )
    preview = _call(
        agent,
        "preview",
        {"configs_dir": str(config_dir), "name": "local-pinned"},
    )

    assert composed["model_ref"] == "01SRC"
    assert composed["model"] == "local_path-model"
    assert not composed["model"].startswith("/")
    stored = yaml.safe_load(Path(saved["path"]).read_text(encoding="utf-8"))
    assert stored["model"] == "local_path-model"
    assert stored["model_ref"] == "01SRC"
    assert str(entry["local_path"]) in preview["preview"]
    assert preview["metadata"]["model_source"] == "local_path"
    assert preview["metadata"]["model_local_path"] == entry["local_path"]


def test_generic_docker_url_model_ref_has_no_auto_hf_cache_mount(
    config_dir: Path, tmp_path: Path
) -> None:
    registry_path = tmp_path / "state" / "vela" / "models" / "registry.json"
    _write_source_registry(registry_path, source="url", tmp_path=tmp_path)
    agent = LocalAgent(models_registry_path=registry_path)

    result = _call(
        agent,
        "compose_config",
        {
            "configs_dir": str(config_dir),
            "name": "url-docker",
            "runtime": {"kind": "docker", "image": "vllm/vllm-openai@sha256:abc"},
            "model_ref": "01SRC",
        },
    )

    assert result["config"]["command"]["docker"]["hf_cache"] is None


def test_fp8_recipe_docker_keeps_recipe_hf_cache_over_agent_default(
    config_dir: Path,
) -> None:
    # Explicit/recipe value always wins: the FP8 recipe's own hf_cache mount is
    # preserved byte-identically and the agent default is NOT substituted.
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

    docker = result["config"]["command"]["docker"]
    assert docker["hf_cache"] == "/home/bgconley/models/qwen36-dual-fp8-vlm/hf-cache"
    assert docker["hf_cache"] != _AGENT_HF_HOME


def test_explicit_hf_cache_overrides_recipe_without_discarding_recipe_settings(
    config_dir: Path, tmp_path: Path
) -> None:
    custom_hf_cache = tmp_path / "operator-recipe-cache"
    agent = LocalAgent()

    result = _call(
        agent,
        "compose_config",
        {
            "configs_dir": str(config_dir),
            "name": "qwen36-27b-fp8-kvfp8-rp6000-blackbird",
            "target": "blackbird",
            "runtime": {"kind": "docker", "hf_cache": str(custom_hf_cache)},
            "model": "Qwen/Qwen3.6-27B-FP8",
        },
    )

    docker = result["config"]["command"]["docker"]
    assert docker["hf_cache"] == str(custom_hf_cache)
    assert docker["image"] == BLACKBIRD_IMAGE
    assert docker["env"]["FLASHINFER_CUDA_ARCH_LIST"] == "12.0f"


def test_bf16_recipe_docker_does_not_inject_agent_hf_cache_default(
    config_dir: Path,
) -> None:
    # The BF16 recipe mounts the cache through env + a root volume, not hf_cache;
    # a matched recipe must stay byte-identical (no agent default injected).
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

    assert result["config"]["command"]["docker"]["hf_cache"] is None
