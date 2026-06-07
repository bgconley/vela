from __future__ import annotations

import inspect
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from conftest import write_yaml

from vela.agent import local as local_agent_module
from vela.agent.local import LocalAgent, TargetCallError
from vela.engine import composer as composer_module
from vela.engine.sidecar import Manifest, Sidecar, command_hash, process_identity_from_pid

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
    assert config["vllm"]["version_profile"] == "0.11"
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


def test_agent_warns_when_blackbird_fp8_docker_lacks_lab_recipe(
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
            "name": "experimental-fp8",
            "target": "blackbird",
            "runtime": {
                "kind": "docker",
                "image": "vllm/vllm-openai@sha256:experimental",
            },
            "model_ref": "experimental-fp8",
        },
    )

    assert result["config"]["engine"]["kv_cache_dtype"] == "fp8"
    assert "blackwell-fp8-runtime-recipe-required" in result["warnings"]


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
    assert "--kv-cache-memory-bytes" not in bf16["extra_args"]
    assert "FLASHINFER_CUDA_ARCH_LIST" not in bf16["docker"]["env"]
    assert bf16["source_artifacts"] == [
        "infx/qwen36-27b-test/start-qwen36-bf16-rp6000-blackbird.sh",
        "infx/qwen36-27b-test/qwen-bf16-rp6000-blackbird-reload-20260509.md",
    ]


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
