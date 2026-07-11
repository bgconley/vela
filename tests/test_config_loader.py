from __future__ import annotations

from pathlib import Path

import pytest
from conftest import write_yaml

from vela.config.loader import discover_config_dirs, load_registry
from vela.config.schema import ModelConfig, RuntimeKind


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


def test_command_runtime_defaults_to_process() -> None:
    cfg = ModelConfig.model_validate({"name": "x", "model": "org/model"})

    assert cfg.command.runtime is RuntimeKind.PROCESS
    assert cfg.command.docker is None


def test_docker_runtime_accepts_docker_config() -> None:
    cfg = ModelConfig.model_validate(
        {
            "name": "x",
            "model": "org/model",
            "command": {
                "runtime": "docker",
                "docker": {
                    "image": "vllm/vllm-openai@sha256:abc",
                    "container_name": "vela-x",
                    "runtime": "nvidia",
                    "hf_cache": "/tank/models/hf-cache",
                    "volumes": ["/tank/models:/root/.cache/huggingface:rw"],
                    "env": {"HF_HOME": "/root/.cache/huggingface"},
                },
            },
        }
    )

    assert cfg.command.runtime is RuntimeKind.DOCKER
    assert cfg.command.docker is not None
    assert cfg.command.docker.image == "vllm/vllm-openai@sha256:abc"
    assert cfg.command.docker.container_name == "vela-x"
    assert cfg.command.docker.runtime == "nvidia"


@pytest.mark.parametrize(
    "command, message",
    [
        ({"runtime": "docker"}, "requires command.docker"),
        (
            {
                "runtime": "docker",
                "executable": "/opt/vllm/bin/vllm",
                "docker": {"image": "vllm/vllm-openai@sha256:abc"},
            },
            "cannot be set with command.executable",
        ),
        (
            {
                "runtime": "docker",
                "build": "01BUILD",
                "docker": {"image": "vllm/vllm-openai@sha256:abc"},
            },
            "cannot be set with command.build",
        ),
        (
            {
                "runtime": "process",
                "docker": {"image": "vllm/vllm-openai@sha256:abc"},
            },
            "command.docker requires command.runtime",
        ),
    ],
)
def test_docker_runtime_rejects_process_handoff_fields(
    command: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        ModelConfig.model_validate({"name": "x", "model": "org/model", "command": command})


def test_model_ref_and_explicit_local_model_path_are_mutually_exclusive() -> None:
    with pytest.raises(ValueError, match="model_ref"):
        ModelConfig.model_validate(
            {"name": "x", "model": "/models/llama", "model_ref": "01MODEL"}
        )


def test_launch_require_cached_models_defaults_false() -> None:
    cfg = ModelConfig.model_validate({"name": "x", "model": "org/model"})

    assert cfg.launch.require_cached_models is False


def test_launch_require_cached_models_accepts_true() -> None:
    cfg = ModelConfig.model_validate(
        {"name": "x", "model": "org/model", "launch": {"require_cached_models": True}}
    )

    assert cfg.launch.require_cached_models is True


def test_launch_require_cached_models_rejects_non_bool() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ModelConfig.model_validate(
            {"name": "x", "model": "org/model", "launch": {"require_cached_models": "sometimes"}}
        )


def test_vllm_pass_through_defaults_are_unset() -> None:
    cfg = ModelConfig.model_validate({"name": "x", "model": "org/model"})

    assert cfg.engine.tensor_parallel_size is None
    assert cfg.engine.gpu_memory_utilization is None
    assert cfg.engine.max_model_len is None
    assert cfg.engine.kv_cache_dtype is None
    assert cfg.engine.quantization is None
    assert cfg.engine.load_format is None


def test_schema_rejects_out_of_range_numeric_values() -> None:
    # Guardrails against footgun configs that would otherwise reach vLLM.
    from pydantic import ValidationError

    base = {"name": "x", "model": "org/model"}
    bad_cases = [
        {"engine": {"gpu_memory_utilization": 5.0}},
        {"engine": {"gpu_memory_utilization": 0.0}},
        {"engine": {"tensor_parallel_size": 0}},
        {"engine": {"pipeline_parallel_size": 0}},
        {"engine": {"max_num_seqs": 0}},
        {"engine": {"max_model_len": 0}},
        {"server": {"port": 0}},
        {"server": {"port": 70000}},
        {"launch": {"ready_timeout_seconds": -1}},
        {"launch": {"health": {"interval_seconds": 0}}},
    ]
    for overrides in bad_cases:
        with pytest.raises(ValidationError):
            ModelConfig.model_validate({**base, **overrides})


def test_schema_accepts_valid_numeric_bounds() -> None:
    cfg = ModelConfig.model_validate(
        {
            "name": "x",
            "model": "org/model",
            "engine": {
                "gpu_memory_utilization": 0.9,
                "tensor_parallel_size": 2,
                "max_num_seqs": 256,
            },
            "server": {"port": 18003, "host": "0.0.0.0", "exposure": "lan"},
            "launch": {"ready_timeout_seconds": 600, "health": {"interval_seconds": 1.5}},
        }
    )
    assert cfg.engine.gpu_memory_utilization == 0.9
    assert cfg.engine.tensor_parallel_size == 2
    assert cfg.server.port == 18003


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
