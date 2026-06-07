from __future__ import annotations

from pathlib import Path

from vela.config.loader import load_registry
from vela.config.schema import RuntimeKind
from vela.engine.command_builder import build_command
from vela.monitoring.health import probe_host_for


def test_blackbird_qwen36_fp8_config_uses_native_docker_runtime() -> None:
    registry = load_registry(Path("configs"))
    cfg = registry.by_name("qwen36-27b-fp8-kvfp8-rp6000-blackbird")
    build = build_command(cfg)

    assert cfg.model == "Qwen/Qwen3.6-27B-FP8"
    assert cfg.served_model_name == "qwen36-27b-fp8-kvfp8-rp6000"
    assert cfg.server.host == "0.0.0.0"
    assert cfg.server.port == 18003
    assert cfg.server.api_key == "EMPTY"
    assert probe_host_for(cfg.server) == "127.0.0.1"
    assert cfg.command.runtime is RuntimeKind.DOCKER
    assert cfg.command.docker is not None
    assert cfg.command.docker.container_name == "qwen36-27b-fp8-kvfp8-rp6000-vela"
    assert cfg.vllm.version_profile == "current"
    assert cfg.vllm.version == "0.20.2rc1.dev9+g01d4d1ad3"
    assert cfg.vllm.torch_version == "2.11.0+cu130"
    assert cfg.vllm.cuda_version == "13.0"
    assert build.metadata["runtime"] == "docker"
    assert build.argv[:5] == [
        "docker",
        "run",
        "-d",
        "--name",
        "qwen36-27b-fp8-kvfp8-rp6000-vela",
    ]
    image_index = build.argv.index(cfg.command.docker.image)
    assert build.argv[image_index + 1] == "Qwen/Qwen3.6-27B-FP8"
    assert build.argv[image_index + 1] != "serve"
    assert "--kv-cache-memory-bytes" in build.argv
    assert "64424509440" in build.argv
    assert build.env["FLASHINFER_CUDA_ARCH_LIST"] == "12.0f"
    assert "flashinfer-cache:/root/.cache/flashinfer" in " ".join(build.argv)
    assert "VLLM_API_KEY='••••'" in build.preview
    assert "VLLM_API_KEY=EMPTY" not in build.preview


def test_blackbird_qwen36_bf16_config_uses_native_docker_without_fp8_pins() -> None:
    registry = load_registry(Path("configs"))
    cfg = registry.by_name("qwen36-27b-bf16-rp6000-blackbird")
    build = build_command(cfg)

    assert cfg.model == "Qwen/Qwen3.6-27B"
    assert cfg.served_model_name == "qwen36-27b-bf16-rp6000"
    assert cfg.server.host == "0.0.0.0"
    assert cfg.server.port == 18002
    assert cfg.server.api_key == "EMPTY"
    assert probe_host_for(cfg.server) == "127.0.0.1"
    assert cfg.command.runtime is RuntimeKind.DOCKER
    assert cfg.command.docker is not None
    assert cfg.command.docker.container_name == "qwen36-27b-bf16-rp6000-vela"
    assert cfg.vllm.version_profile == "current"
    assert cfg.vllm.version == "0.20.2rc1.dev9+g01d4d1ad3"
    assert cfg.vllm.torch_version == "2.11.0+cu130"
    assert cfg.vllm.cuda_version == "13.0"
    assert build.metadata["runtime"] == "docker"
    image_index = build.argv.index(cfg.command.docker.image)
    assert build.argv[image_index + 1] == "Qwen/Qwen3.6-27B"
    assert "--kv-cache-memory-bytes" not in build.argv
    assert "--attention-backend" not in build.argv
    assert "FLASHINFER_CUDA_ARCH_LIST" not in build.env
    bf16_root_mount = (
        "/home/bgconley/models/qwen36-27b-bf16:/home/bgconley/models/qwen36-27b-bf16"
    )
    assert bf16_root_mount in " ".join(build.argv)
    assert "VLLM_API_KEY='••••'" in build.preview
    assert "VLLM_API_KEY=EMPTY" not in build.preview
