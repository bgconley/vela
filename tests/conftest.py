from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from textwrap import dedent

import pytest


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    path = tmp_path / "configs"
    path.mkdir()
    return path


def write_yaml(path: Path, text: str) -> Path:
    path.write_text(dedent(text).strip() + "\n", encoding="utf-8")
    return path


@pytest.fixture
def valid_config_text() -> str:
    return """
    name: llama
    description: local llama
    model: meta-llama/Llama-3.1-8B-Instruct
    engine:
      tensor_parallel_size: 2
      gpu_memory_utilization: 0.91
      dtype: auto
      kv_cache_dtype: fp8_ds_mla
      quantization: some-new-quant
      enforce_eager: false
    server:
      host: 127.0.0.1
      port: 8001
      api_key: sk-test-secret
    logging:
      request_logging: false
      suppress_access_log_for: [/health, /metrics]
      max_log_len: 256
    env:
      CUDA_VISIBLE_DEVICES: "0,1"
      HF_TOKEN: hf_secret
    extra_args: ["--new-vllm-flag", "value"]
    launch:
      mode: attached
      ready_timeout_seconds: 20
    """


@pytest.fixture(autouse=True)
def clear_config_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("VLLM_LOADER_CONFIGS", raising=False)
    monkeypatch.delenv("VLLM_LOADER_AGENT_TOKEN", raising=False)
    yield
