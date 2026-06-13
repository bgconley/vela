from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from textwrap import dedent

import pytest

_VELA_STATE_ENV_KEYS = (
    "XDG_STATE_HOME",
    "XDG_RUNTIME_DIR",
    "XDG_DATA_HOME",
    "XDG_CONFIG_HOME",
)


def scaled_timeout(seconds: float) -> float:
    raw_scale = os.environ.get("VELA_TEST_TIMEOUT_SCALE", "1")
    try:
        scale = float(raw_scale)
    except ValueError:
        scale = 1.0
    return seconds * max(scale, 1.0)


@pytest.fixture(scope="session", autouse=True)
def isolated_vela_state() -> Iterator[Path]:
    """Point ALL vela state at a per-session temp dir (the durable bug-185 fix).

    Without this, suites read/write the user's real ``~/.local/state/vela``:
    run records accumulate across runs until launch tests blow their 5s
    deadlines, and a long-lived agent daemon on the shared socket keeps serving
    OLD code to every test after a source change. A fresh state dir per session
    means a fresh daemon (running this checkout's code), and the teardown stops
    it so nothing leaks.

    Uses ``tempfile.mkdtemp`` (short ``/tmp`` path), not pytest's tmp factory:
    macOS caps Unix socket paths at ~104 chars and the factory paths are too
    deep for ``agent.sock``.
    """
    state_root = Path(tempfile.mkdtemp(prefix="vela-test-state-"))
    previous = {key: os.environ.get(key) for key in _VELA_STATE_ENV_KEYS}
    os.environ["XDG_STATE_HOME"] = str(state_root / "state")
    os.environ["XDG_RUNTIME_DIR"] = str(state_root / "runtime")
    os.environ["XDG_DATA_HOME"] = str(state_root / "data")
    # Config too: tests must not see the developer machine's targets.yaml
    # (the remote-validation run exposed 4 tests that silently depended on it).
    os.environ["XDG_CONFIG_HOME"] = str(state_root / "config")
    (state_root / "runtime").mkdir(parents=True, exist_ok=True)
    try:
        yield state_root
    finally:
        # Stop the daemon (if any test spawned one) while the env still points
        # at the session socket, then restore the caller's environment.
        subprocess.run(
            [sys.executable, "-m", "vela.cli", "agent", "stop"],
            capture_output=True,
            timeout=30,
            check=False,
        )
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(state_root, ignore_errors=True)


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
    monkeypatch.delenv("VELA_CONFIGS", raising=False)
    monkeypatch.delenv("VELA_AGENT_TOKEN", raising=False)
    monkeypatch.delenv("VELA_AGENT_REQUIRE_TOKEN", raising=False)
    yield
