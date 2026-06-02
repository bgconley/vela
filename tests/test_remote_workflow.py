from __future__ import annotations

from pathlib import Path


def test_remote_validation_uses_readiness_smoke_for_real_config() -> None:
    script = Path("scripts/run_remote_tests.sh").read_text(encoding="utf-8")

    assert "sample_gpus" in script
    assert "vllm --version" in script
    assert "vllm serve --help" in script
    assert 'vllm-loader smoke "$real_config"' in script
    assert 'vllm-loader run "$real_config"' not in script
