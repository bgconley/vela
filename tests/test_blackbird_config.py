from __future__ import annotations

import os
import subprocess
from pathlib import Path

from vela.config.loader import load_registry
from vela.engine.command_builder import build_command
from vela.monitoring.health import probe_host_for


def test_blackbird_qwen36_config_uses_foreground_docker_wrapper() -> None:
    registry = load_registry(Path("configs"))
    cfg = registry.by_name("qwen36-27b-fp8-kvfp8-rp6000-blackbird")
    build = build_command(cfg)

    assert cfg.model == "Qwen/Qwen3.6-27B-FP8"
    assert cfg.served_model_name == "qwen36-27b-fp8-kvfp8-rp6000"
    assert cfg.server.host == "0.0.0.0"
    assert cfg.server.port == 18003
    assert cfg.server.api_key == "EMPTY"
    assert probe_host_for(cfg.server) == "127.0.0.1"
    assert build.argv[0] == "./scripts/blackbird_qwen36_vllm_foreground.sh"
    assert "--kv-cache-memory-bytes" in build.argv
    assert "64424509440" in build.argv
    assert "VLLM_API_KEY='••••'" in build.preview
    assert "VLLM_API_KEY=EMPTY" not in build.preview


def test_blackbird_wrapper_dry_run_derives_container_launch() -> None:
    script = Path("scripts/blackbird_qwen36_vllm_foreground.sh")
    env = {
        **os.environ,
        "VELA_BLACKBIRD_DRY_RUN": "1",
        "CONTAINER": "test-qwen36-container",
    }

    result = subprocess.run(
        [
            "bash",
            str(script),
            "serve",
            "Qwen/Qwen3.6-27B-FP8",
            "--served-model-name",
            "qwen36-27b-fp8-kvfp8-rp6000",
            "--host",
            "0.0.0.0",
            "--port",
            "18003",
            "--kv-cache-dtype",
            "fp8",
            "--kv-cache-memory-bytes",
            "64424509440",
            "--max-model-len",
            "262144",
            "--gpu-memory-utilization",
            "0.97",
            "--max-num-seqs",
            "16",
            "--max-num-batched-tokens",
            "8192",
            "--attention-backend",
            "FLASHINFER",
            "--disable-uvicorn-access-log",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "container=test-qwen36-container" in result.stdout
    assert "model=Qwen/Qwen3.6-27B-FP8" in result.stdout
    assert "served_model_name=qwen36-27b-fp8-kvfp8-rp6000" in result.stdout
    assert "port=18003" in result.stdout
    assert "--model Qwen/Qwen3.6-27B-FP8" in result.stdout
    assert "--served-model-name qwen36-27b-fp8-kvfp8-rp6000" in result.stdout
    assert "--kv-cache-memory-bytes 64424509440" in result.stdout
    assert "--max-num-batched-tokens 8192" in result.stdout
    assert "--attention-backend FLASHINFER" in result.stdout
