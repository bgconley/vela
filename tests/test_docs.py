from __future__ import annotations

from pathlib import Path


def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def test_readme_covers_new_contributor_v1_paths() -> None:
    text = _read("README.md")

    for phrase in (
        "## Quickstart",
        "## Remote Targets",
        "## Config Schema",
        "## Build Methods",
        "## Model Registry",
        "## Agent/RPC Overview",
        "## Tested Matrix",
    ):
        assert phrase in text


def test_user_docs_cover_schema_artifacts_and_rpc() -> None:
    configuration = _read("docs/configuration.md")
    builds_models = _read("docs/builds-and-models.md")
    agent_rpc = _read("docs/agent-rpc.md")

    assert "command.build" in configuration
    assert "model_ref" in configuration
    assert "targets.yaml" in configuration
    assert "positional SSH arguments" in configuration
    assert "nightly and commit require uv" in builds_models
    assert "HF_TOKEN" in builds_models
    assert "controller passes only run_id" in agent_rpc
    assert "subscribe" in agent_rpc
    assert "VELA_AGENT_TOKEN" in agent_rpc
    assert "local_transport`: `socket` or `in_process`" in configuration
    assert "inprocess" not in configuration
    assert "Build removal has no --force override" in builds_models
    assert "Model removal --force only overrides config-pin protection" in builds_models
    assert "vela agent status --target <name>" in configuration
    assert "VELA_CONFIGS" in configuration
    assert "vela agent gen-token --install --target <name>" in configuration
    assert "write_agent_token" in agent_rpc
    assert "XDG_CONFIG_HOME" in configuration
    assert "XDG_DATA_HOME" in configuration
    assert "XDG_STATE_HOME" in configuration
    assert "XDG_RUNTIME_DIR" in configuration


def test_build_model_docs_cover_operational_cli_surfaces() -> None:
    text = _read("docs/builds-and-models.md")

    for phrase in (
        "vela build run",
        "vela build repair",
        "--copy",
        "vela model download tiny-llama --target blackbird --json",
        "vela model verify tiny-llama --target blackbird --deep",
    ):
        assert phrase in text


def test_v15_docs_cover_native_docker_and_composer_surfaces() -> None:
    readme = _read("README.md")
    configuration = _read("docs/configuration.md")
    agent_rpc = _read("docs/agent-rpc.md")
    gpu_workflow = _read("docs/gpu-workflow.md")
    docker_runtime = _read("docs/docker-runtime.md")
    deployments = _read("docs/deployments.md")

    assert "New Deployment" in readme
    assert "vela deploy create" in readme
    assert "command.runtime" in configuration
    assert "command.docker" in configuration
    assert "compose_config" in agent_rpc
    assert "export_config" in agent_rpc
    assert "native `command.runtime: docker`" in docker_runtime
    assert "TUI is the primary deployment composer" in deployments
    assert "local Blackwell recipe" in deployments
    assert "Hugging Face metadata is advisory" in deployments
    assert "2026-06-06-p620-blackbird-native-docker-fp8" in gpu_workflow
    assert "2026-06-06-p620-blackbird-native-docker-bf16" in gpu_workflow
    assert "VELA_SMOKE_RUN_ID" in gpu_workflow
    assert "REAL_MODEL_DAEMON_RESTART_OK" in gpu_workflow
    assert "run_id=<run_id>" in gpu_workflow


def test_lab_topology_docs_use_current_repo_and_venv_paths() -> None:
    for path in ("README.md", "docs/configuration.md", "docs/gpu-workflow.md"):
        text = _read(path)
        assert "/home/bgconley/repos/vela" not in text
        assert "/home/bgconley/venvs/vela" not in text
        assert "/home/bgconley/repos/current-vela" not in text
        assert "/home/bgconley/venvs/current-vela" not in text


def test_blackwell_docs_treat_local_recipes_as_runtime_truth() -> None:
    docker_runtime = _read("docs/docker-runtime.md")
    configuration = _read("docs/configuration.md")

    assert "local Blackwell recipe" in docker_runtime
    assert "Hugging Face metadata is advisory" in docker_runtime
    assert "sm_120" in docker_runtime
    assert "CUTLASS" in docker_runtime
    assert "FlashInfer" in docker_runtime
    assert "intentionally emit both `--ipc=host` and `--shm-size 32g`" in docker_runtime
    assert "vela deploy from-wrapper" in docker_runtime
    assert "does not infer vLLM image" in docker_runtime
    assert "local deployment scripts" in configuration


def test_docker_examples_doc_matches_native_docker_cutover() -> None:
    text = _read("vela-docker-runtime-examples-v1.md")

    assert "runtime has shipped" in text
    assert "configs/qwen36-27b-fp8-kvfp8-rp6000-blackbird.yaml" in text
    assert "wrappers are retained as reference" in text
    assert "Do not drop these into `configs/` yet" not in text
    assert "delete the wrapper scripts" not in text
