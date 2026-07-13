from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from vela.agent.local import LocalAgent, TargetCallError
from vela.transport.inprocess import InProcessTargetClient
from vela.tui.app import VelaApp, _provenance_with_flag_updates
from vela.tui.screens.new_deployment import NewDeploymentReviewScreen


def _oxcart_review_config() -> dict[str, object]:
    return {
        "name": "oxcart-qwen36",
        "target": "local",
        "model": "Qwen/Qwen3.6-27B-FP8",
        "model_ref": "Qwen/Qwen3.6-27B-FP8",
        "revision": "e89b16ebf1988b3d6befa7de50abc2d76f26eb09",
        "served_model_name": "qwen36-27b-fp8-oxcart",
        "command": {
            "runtime": "docker",
            "docker": {
                "image": "vllm/vllm-openai@sha256:" + "a" * 64,
                "container_name": "vela-oxcart-qwen36",
                "hf_cache": "/tank/hf-cache",
                "hf_cache_target": "/root/.cache/huggingface",
                "auto_remove": True,
                "volumes": [
                    "/tank/vllm:/root/.cache/vllm",
                    "/tank/triton:/root/.cache/triton",
                ],
                "evict": ["vela-oxcart-qwen36"],
            },
        },
        "server": {
            "host": "127.0.0.1",
            "port": 18004,
            "exposure": "local",
        },
        "launch": {
            "required_hostname": "oxcart",
            "require_cached_models": True,
            "runs_dir": "/tank/vela-runs",
        },
    }


def test_review_summary_discloses_saved_identity_mounts_and_destructive_actions() -> None:
    screen = NewDeploymentReviewScreen(
        config=_oxcart_review_config(),
        preview="docker run … vllm serve …",
        derived=[],
        warnings=[],
    )

    summary = screen._summary_text()

    assert "Target: local" in summary
    assert "Model ref: Qwen/Qwen3.6-27B-FP8" in summary
    assert "Resolved revision: e89b16ebf1988b3d6befa7de50abc2d76f26eb09" in summary
    assert "Served model: qwen36-27b-fp8-oxcart" in summary
    assert f"Image: vllm/vllm-openai@sha256:{'a' * 64}" in summary
    assert "Required hostname: oxcart" in summary
    assert "Cached models required: yes" in summary
    assert "HF cache: /tank/hf-cache -> /root/.cache/huggingface" in summary
    assert "Mounts:" in summary
    assert "/tank/vllm:/root/.cache/vllm" in summary
    assert "Destructive actions: may replace only vela-oxcart-qwen36" in summary
    assert "Container cleanup: auto-remove after stop" in summary
    assert "Runs: /tank/vela-runs" in summary


def test_review_renders_provenance_in_plain_language_and_redacts_secrets() -> None:
    screen = NewDeploymentReviewScreen(
        config=_oxcart_review_config(),
        preview="docker run …",
        derived=[
            {
                "field": "revision",
                "value": "e89b16ebf1988b3d6befa7de50abc2d76f26eb09",
                "source": "model_registry:resolved_commit",
            },
            {
                "field": "command.docker.image",
                "value": "vllm/vllm-openai@sha256:" + "a" * 64,
                "source": "lab_recipe:oxcart-qwen36-27b-fp8-mtp-vl",
            },
            {
                "field": "env.HF_TOKEN",
                "value": "hf_do_not_render_this",
                "source": "operator_override",
            },
            {
                "field": "command.docker.env",
                "value": '{"UNUSUAL_CREDENTIAL": "plain-do-not-render"}',
                "source": "lab_recipe:oxcart-qwen36-27b-fp8-mtp-vl",
            },
        ],
        warnings=[],
    )

    provenance = screen._derived_text()

    assert "from model registry resolved commit" in provenance
    assert "from lab recipe oxcart-qwen36-27b-fp8-mtp-vl" in provenance
    assert "from operator override" in provenance
    assert "hf_do_not_render_this" not in provenance
    assert "plain-do-not-render" not in provenance
    assert "env.HF_TOKEN: ••••" in provenance
    assert "command.docker.env: •••• (values redacted)" in provenance


@pytest.mark.asyncio
async def test_review_renders_bracket_rich_oxcart_docker_provenance_as_plain_text() -> None:
    """Oxcart's JSON-shaped ownership labels must never be parsed as markup."""
    from textual.app import App
    from textual.widgets import Static

    extra_run_args = (
        '["--label", "ai.vela.managed=true", "--label", '
        '"ai.vela.profile=oxcart-qwen36-27b-fp8-mtp-vl"]'
    )
    app = App()
    async with app.run_test() as pilot:
        screen = NewDeploymentReviewScreen(
            config=_oxcart_review_config(),
            preview="docker run …",
            derived=[
                {
                    "field": "command.docker.extra_run_args",
                    "value": extra_run_args,
                    "source": "lab_recipe:oxcart-qwen36-27b-fp8-mtp-vl",
                }
            ],
            warnings=[],
        )
        await app.push_screen(screen)
        await pilot.pause()

        rendered = screen.query_one("#new-deployment-review-derived", Static)
        assert extra_run_args in str(rendered.render())


def test_flag_customization_relabels_recipe_provenance_as_operator_override() -> None:
    original = {
        "name": "demo",
        "model": "org/model",
        "engine": {"max_model_len": 4096},
        "extra_args": ["--enable-prefix-caching"],
    }
    updated = {
        **original,
        "engine": {"max_model_len": 8192},
        "extra_args": ["--api-key", "plain-secret-value"],
    }
    derived = [
        {
            "field": "engine.max_model_len",
            "value": "4096",
            "source": "lab_recipe:demo",
        },
        {
            "field": "extra_args",
            "value": '["--enable-prefix-caching"]',
            "source": "lab_recipe:demo",
        },
    ]

    result = _provenance_with_flag_updates(
        derived,
        original_config=original,
        updated_config=updated,
        selection={
            "engine": {"max_model_len": "8192"},
            "extra_args": ["--api-key", "plain-secret-value"],
        },
    )
    provenance = {item["field"]: item for item in result}

    assert provenance["engine.max_model_len"] == {
        "field": "engine.max_model_len",
        "value": "8192",
        "source": "operator_override",
    }
    assert provenance["extra_args"]["source"] == "operator_override"
    assert "plain-secret-value" not in provenance["extra_args"]["value"]


def test_review_summary_discloses_process_build_identity() -> None:
    screen = NewDeploymentReviewScreen(
        config={
            "name": "built",
            "target": "local",
            "model": "org/model",
            "command": {"runtime": "process", "build": "01IMMUTABLEBUILD"},
            "server": {"host": "127.0.0.1", "port": 18000, "exposure": "local"},
        },
        preview="/build/bin/vllm serve org/model",
        derived=[],
        warnings=[],
    )

    assert "Build id: 01IMMUTABLEBUILD" in screen._summary_text()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "config,expected",
    [
        (
            {
                "name": "mutable-process",
                "model": "org/model",
                "command": {"runtime": "process"},
            },
            "immutable build",
        ),
        (
            {
                "name": "mutable-docker",
                "model": "org/model",
                "command": {
                    "runtime": "docker",
                    "docker": {"image": "vllm/vllm-openai:latest"},
                },
            },
            "full @sha256 digest",
        ),
    ],
)
async def test_review_save_refuses_mutable_runtime_identity(
    config: dict[str, object], expected: str
) -> None:
    from textual.app import App
    from textual.widgets import Static

    app = App()
    dismissed: list[dict[str, object] | None] = []
    async with app.run_test() as pilot:
        screen = NewDeploymentReviewScreen(
            config=config,
            preview="vllm serve org/model",
            derived=[],
            warnings=[],
        )
        await app.push_screen(screen, dismissed.append)
        await pilot.pause()

        screen.action_save()
        await pilot.pause()

        assert dismissed == []
        blocker = screen.query_one("#new-deployment-review-error", Static)
        assert blocker.display is True
        assert expected in str(blocker.content)


@pytest.mark.asyncio
async def test_app_save_boundary_refuses_mutable_runtime_before_agent_rpc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = VelaApp(
        configs_dir=tmp_path,
        target_client=InProcessTargetClient(LocalAgent()),
        target_ping_interval_seconds=None,
    )
    async with app.run_test():
        target_call = AsyncMock()
        monkeypatch.setattr(app, "_target_call", target_call)

        await app._save_reviewed_new_deployment(
            {
                "name": "mutable",
                "model": "org/model",
                "command": {"runtime": "process"},
            }
        )

        target_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_oxcart_digest_profile_passes_review_save_identity_gate() -> None:
    from textual.app import App

    app = App()
    dismissed: list[dict[str, object] | None] = []
    async with app.run_test() as pilot:
        screen = NewDeploymentReviewScreen(
            config=_oxcart_review_config(),
            preview="docker run …",
            derived=[],
            warnings=[],
        )
        await app.push_screen(screen, dismissed.append)
        await pilot.pause()

        screen.action_save()
        await pilot.pause()

    assert dismissed and dismissed[0] is not None
    assert dismissed[0]["action"] == "save"


@pytest.mark.asyncio
async def test_save_name_conflict_restores_the_human_draft_for_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = VelaApp(
        configs_dir=tmp_path,
        target_client=InProcessTargetClient(LocalAgent()),
        target_ping_interval_seconds=None,
    )
    draft = {
        "name": "already-there",
        "model": "org/model",
        "runtime": "build",
        "build": "01IMMUTABLEBUILD",
        "step_index": 4,
    }
    config = {
        "name": "already-there",
        "model": "org/model",
        "command": {"runtime": "process", "build": "01IMMUTABLEBUILD"},
    }

    async with app.run_test():
        async def target_call(method: str, _params: dict[str, object]) -> dict[str, object]:
            if method == "preflight":
                return {"ok": True, "failures": []}
            if method == "save_config":
                raise TargetCallError(
                    "config-exists",
                    "config already exists",
                    {"name": "already-there"},
                )
            raise AssertionError(method)

        reopen = AsyncMock()
        monkeypatch.setattr(app, "_target_call", target_call)
        monkeypatch.setattr(app, "_open_new_deployment", reopen)

        await app._save_reviewed_new_deployment(config, draft=draft)

    restored = dict(draft)
    restored["step_index"] = 0
    reopen.assert_awaited_once()
    assert reopen.await_args.kwargs["initial"] == restored
    assert "already exists" in reopen.await_args.kwargs["error_message"]
