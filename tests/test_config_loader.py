from __future__ import annotations

from pathlib import Path

import pytest
from conftest import write_yaml

from vela.config.loader import discover_config_dirs, load_registry
from vela.config.schema import ModelConfig


def test_valid_config_loads(config_dir: Path, valid_config_text: str) -> None:
    write_yaml(config_dir / "llama.yaml", valid_config_text)

    registry = load_registry(config_dir)

    assert [item.config.name for item in registry.valid] == ["llama"]
    assert registry.invalid == []


def test_invalid_yaml_or_schema_error_is_retained(config_dir: Path) -> None:
    write_yaml(config_dir / "broken.yaml", "name: [unterminated")
    write_yaml(config_dir / "bad-schema.yaml", "name: missing-model")

    registry = load_registry(config_dir)

    assert registry.valid == []
    assert {item.path.name for item in registry.invalid} == {"broken.yaml", "bad-schema.yaml"}
    assert all(item.errors for item in registry.invalid)


def test_duplicate_names_are_detected(config_dir: Path) -> None:
    write_yaml(config_dir / "one.yaml", "name: same\nmodel: repo/one")
    write_yaml(config_dir / "two.yaml", "name: same\nmodel: repo/two")

    registry = load_registry(config_dir)

    assert registry.valid == []
    assert len(registry.invalid) == 2
    assert all("duplicate config name" in item.errors[0].lower() for item in registry.invalid)


def test_duplicate_names_report_each_file_once(config_dir: Path) -> None:
    write_yaml(config_dir / "one.yaml", "name: same\nmodel: repo/one")
    write_yaml(config_dir / "two.yaml", "name: same\nmodel: repo/two")
    write_yaml(config_dir / "three.yaml", "name: same\nmodel: repo/three")

    registry = load_registry(config_dir)

    assert registry.valid == []
    assert sorted(item.path.name for item in registry.invalid) == [
        "one.yaml",
        "three.yaml",
        "two.yaml",
    ]


def test_served_model_name_defaults_to_model_basename() -> None:
    cfg = ModelConfig.model_validate({"name": "x", "model": "org/model-name"})

    assert cfg.served_model_name == "model-name"


def test_model_config_accepts_optional_target_reference() -> None:
    cfg = ModelConfig.model_validate(
        {"name": "x", "model": "org/model-name", "target": "blackbird"}
    )

    assert cfg.target == "blackbird"


def test_model_config_accepts_build_and_model_pins() -> None:
    cfg = ModelConfig.model_validate(
        {
            "name": "x",
            "model": "org/model-name",
            "revision": "abc123",
            "model_ref": "01MODEL",
            "command": {"build": "01BUILD"},
        }
    )

    assert cfg.revision == "abc123"
    assert cfg.model_ref == "01MODEL"
    assert cfg.command.build == "01BUILD"


def test_command_build_and_executable_are_mutually_exclusive() -> None:
    with pytest.raises(ValueError, match="command.build"):
        ModelConfig.model_validate(
            {
                "name": "x",
                "model": "org/model-name",
                "command": {"executable": "/opt/vllm/bin/vllm", "build": "01BUILD"},
            }
        )


def test_model_ref_and_explicit_local_model_path_are_mutually_exclusive() -> None:
    with pytest.raises(ValueError, match="model_ref"):
        ModelConfig.model_validate(
            {"name": "x", "model": "/models/llama", "model_ref": "01MODEL"}
        )


def test_vllm_pass_through_defaults_are_unset() -> None:
    cfg = ModelConfig.model_validate({"name": "x", "model": "org/model"})

    assert cfg.engine.tensor_parallel_size is None
    assert cfg.engine.gpu_memory_utilization is None
    assert cfg.engine.max_model_len is None
    assert cfg.engine.kv_cache_dtype is None
    assert cfg.engine.quantization is None
    assert cfg.engine.load_format is None


def test_open_string_enum_fields_are_accepted() -> None:
    cfg = ModelConfig.model_validate(
        {
            "name": "x",
            "model": "org/model",
            "engine": {
                "kv_cache_dtype": "brand_new_dtype",
                "quantization": "brand_new_quant",
                "load_format": "brand_new_loader",
            },
        }
    )

    assert cfg.engine.kv_cache_dtype == "brand_new_dtype"
    assert cfg.engine.quantization == "brand_new_quant"
    assert cfg.engine.load_format == "brand_new_loader"


def test_discovery_precedence(config_dir: Path, monkeypatch) -> None:
    env_dir = config_dir / "env"
    env_dir.mkdir()
    cli_dir = config_dir / "cli"
    cli_dir.mkdir()
    monkeypatch.setenv("VELA_CONFIGS", str(env_dir))

    assert discover_config_dirs(configs_dir=cli_dir, cwd=config_dir)[0] == cli_dir
    assert discover_config_dirs(cwd=config_dir)[0] == env_dir
