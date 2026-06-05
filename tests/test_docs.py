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
