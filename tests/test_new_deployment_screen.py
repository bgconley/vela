"""Headless tests for the refactored New Deployment wizard + review (Figma 56:2-58:2).

These pin the redesign — the shared StepIndicator, KeyHintBar footers, and the
"opens a dedicated screen" handoff signposting — while re-verifying the review
panel substring contract the smoke suite relies on. The 24 whole-handoff
acceptance tests in test_tui_smoke.py remain the behavioral gate.
"""

from __future__ import annotations

import pytest
from textual.app import App
from textual.widgets import Input, Select, Static

from vela.tui.screens.new_deployment import NewDeploymentReviewScreen, NewDeploymentScreen
from vela.tui.widgets import KeyHintBar, StepIndicator


class _Host(App):
    pass


@pytest.mark.asyncio
async def test_new_deployment_wizard_uses_step_indicator_and_footer() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = NewDeploymentScreen(target_label="gpu-node", presets=[{"name": "balanced"}])
        await app.push_screen(screen)
        await pilot.pause()
        steps = screen.query_one("#new-deployment-steps", StepIndicator)
        assert "▸ Target" in str(steps.content)  # current step highlighted
        assert len(screen.query(KeyHintBar)) >= 1  # footer keybar
        # Advancing re-marks done/current.
        screen.action_next_step()
        await pilot.pause()
        content = str(steps.content)
        assert "✓ Target" in content
        assert "▸ Runtime" in content


@pytest.mark.asyncio
async def test_new_deployment_handoff_choices_signpost_screens() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = NewDeploymentScreen(target_label="gpu-node", presets=[{"name": "balanced"}])
        await app.push_screen(screen)
        await pilot.pause()
        helpers = " ".join(str(h.content) for h in screen.query(".new-deployment-helper"))
        # Runtime + model-source steps each explain that some choices open a screen.
        assert "dedicated screen" in helpers


@pytest.mark.asyncio
async def test_new_deployment_review_uses_step_indicator_and_preserves_panels() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = NewDeploymentReviewScreen(
            config={"name": "demo", "model": "org/m"},
            preview="vllm serve org/m --tensor-parallel-size 2",
            derived=[{"field": "server.port", "value": "8000", "source": "auto"}],
            warnings=["pinned remote-only model has no immutable commit sha"],
        )
        await app.push_screen(screen)
        await pilot.pause()
        steps = screen.query_one("#new-deployment-review-steps", StepIndicator)
        assert "▸ Review" in str(steps.content)
        assert len(screen.query(KeyHintBar)) >= 1
        # The smoke-suite substring contract for the review panels is preserved.
        derived = str(screen.query_one("#new-deployment-review-derived", Static).content)
        preview = str(screen.query_one("#new-deployment-review-preview", Static).content)
        warnings = str(screen.query_one("#new-deployment-review-warnings", Static).content)
        assert "server.port" in derived
        assert "vllm serve org/m" in preview
        assert "no immutable commit sha" in warnings


@pytest.mark.asyncio
async def test_recipe_helper_and_loud_application() -> None:
    # J19: recipes are explained before use and announce what they changed.
    app = _Host()
    async with app.run_test() as pilot:
        screen = NewDeploymentScreen(
            target_label="gpu-node",
            presets=[{"name": "balanced", "description": "Balanced", "engine": {}}],
            recipes=[
                {
                    "key": "qwen36-27b-bf16-blackwell",
                    "name": "qwen36-27b-bf16-blackwell",
                    "runtime": "docker",
                    "model": "org/qwen3.6-27b",
                    "image": "vllm/vllm-openai@sha256:abc",
                    "server": {"port": 18001, "exposure": "lan"},
                }
            ],
        )
        await app.push_screen(screen)
        await pilot.pause()
        note = str(screen.query_one("#new-deployment-recipe-note", Static).content)
        assert "pre-fills" in note
        screen.query_one("#new-deployment-recipe", Select).value = "qwen36-27b-bf16-blackwell"
        await pilot.pause()
        note = str(screen.query_one("#new-deployment-recipe-note", Static).content)
        assert "Recipe applied" in note
        assert "runtime=docker" in note
        assert "port=18001" in note


@pytest.mark.asyncio
async def test_customize_and_runtime_helpers_present() -> None:
    # J22: image / port helpers say what blank means and where digests come from.
    app = _Host()
    async with app.run_test() as pilot:
        screen = NewDeploymentScreen(target_label="gpu-node", presets=[])
        await app.push_screen(screen)
        await pilot.pause()
        image_help = str(screen.query_one("#new-deployment-image-help", Static).content)
        assert "Blank = recipe/preset default" in image_help
        assert "digest" in image_help
        port_help = str(screen.query_one("#new-deployment-port-help", Static).content)
        assert "auto-allocated on the target" in port_help


@pytest.mark.asyncio
async def test_preset_description_rendered_in_wizard() -> None:
    # J21: the preset's description (already in the agent data) is shown.
    app = _Host()
    async with app.run_test() as pilot:
        screen = NewDeploymentScreen(
            target_label="gpu-node",
            presets=[
                {
                    "name": "balanced",
                    "description": "Steady defaults for general serving",
                    "engine": {},
                },
                {
                    "name": "throughput",
                    "description": "Maximize tokens/sec at higher memory pressure",
                    "engine": {},
                },
            ],
        )
        await app.push_screen(screen)
        await pilot.pause()
        help_text = str(screen.query_one("#new-deployment-preset-help", Static).content)
        assert "Steady defaults" in help_text
        screen.query_one("#new-deployment-preset", Select).value = "throughput"
        await pilot.pause()
        help_text = str(screen.query_one("#new-deployment-preset-help", Static).content)
        assert "tokens/sec" in help_text


@pytest.mark.asyncio
async def test_runtime_step_discloses_only_active_group() -> None:
    # J24: one runtime, one visible field group — controls stay mounted.
    app = _Host()
    async with app.run_test() as pilot:
        screen = NewDeploymentScreen(target_label="gpu-node", presets=[])
        await app.push_screen(screen)
        await pilot.pause()
        # Default runtime=process → no runtime-specific fields.
        assert screen.query_one("#nd-group-image").display is False
        assert screen.query_one("#nd-group-build").display is False
        assert screen.query_one("#nd-group-executable").display is False
        screen.query_one("#new-deployment-runtime", Select).value = "docker"
        await pilot.pause()
        assert screen.query_one("#nd-group-image").display is True
        assert screen.query_one("#nd-group-build").display is False
        screen.query_one("#new-deployment-runtime", Select).value = "build"
        await pilot.pause()
        assert screen.query_one("#nd-group-build").display is True
        assert screen.query_one("#nd-group-image").display is False
        # Hidden controls stay mounted (smoke contract).
        assert screen.query_one("#new-deployment-image", Input)
        assert screen.query_one("#new-deployment-executable", Input)


@pytest.mark.asyncio
async def test_model_step_mode_discloses_pinned_vs_bare() -> None:
    # J25: the mode select finally drives what's visible.
    app = _Host()
    async with app.run_test() as pilot:
        screen = NewDeploymentScreen(target_label="gpu-node", presets=[])
        await app.push_screen(screen)
        await pilot.pause()
        assert screen.query_one("#nd-group-pinned").display is True
        assert screen.query_one("#nd-group-bare").display is False
        screen.query_one("#new-deployment-model-mode", Select).value = "bare"
        await pilot.pause()
        assert screen.query_one("#nd-group-pinned").display is False
        assert screen.query_one("#nd-group-bare").display is True
        bare_help = str(screen.query_one("#new-deployment-bare-help", Static).content)
        assert "resolved at launch" in bare_help
        # Both controls stay mounted.
        assert screen.query_one("#new-deployment-model-ref", Select)
        assert screen.query_one("#new-deployment-model", Input)


@pytest.mark.asyncio
async def test_blank_name_uses_suggested_slug() -> None:
    # J27: the name is suggested from model + target, not demanded.
    app = _Host()
    async with app.run_test() as pilot:
        screen = NewDeploymentScreen(target_label="gpu-node", presets=[])
        await app.push_screen(screen)
        await pilot.pause()
        screen.query_one("#new-deployment-model", Input).value = "Qwen/Qwen3-32B"
        await pilot.pause()
        # Live ghost: the Name placeholder shows the suggestion.
        placeholder = screen.query_one("#new-deployment-name", Input).placeholder
        assert "qwen3-32b-gpu-node" in placeholder
        spec = screen._collect_spec()
        assert spec["name"] == "qwen3-32b-gpu-node"


@pytest.mark.asyncio
async def test_customize_advanced_group_overrides_derived_fields() -> None:
    # J28: served_model_name / runs_dir / container_name editable behind Ctrl+R.
    app = _Host()
    async with app.run_test() as pilot:
        screen = NewDeploymentScreen(target_label="gpu-node", presets=[])
        await app.push_screen(screen)
        await pilot.pause()
        # Collapsed by default — zero novice cost.
        assert screen.query_one("#nd-group-derived").display is False
        screen.action_toggle_advanced()
        assert screen.query_one("#nd-group-derived").display is True
        screen.query_one("#new-deployment-model", Input).value = "Qwen/Qwen3-32B"
        screen.query_one("#new-deployment-served-name", Input).value = "qwen-prod"
        screen.query_one("#new-deployment-runs-dir", Input).value = "/home/user/runs"
        screen.query_one("#new-deployment-container-name", Input).value = "vela-qwen-prod"
        spec = screen._collect_spec()
        assert spec["overrides"]["served_model_name"] == "qwen-prod"
        assert spec["overrides"]["launch"]["runs_dir"] == "/home/user/runs"
        assert spec["overrides"]["container_name"] == "vela-qwen-prod"
        # Blank advanced fields add nothing (contract-stable overrides shape).
        screen.query_one("#new-deployment-served-name", Input).value = ""
        screen.query_one("#new-deployment-runs-dir", Input).value = ""
        screen.query_one("#new-deployment-container-name", Input).value = ""
        spec = screen._collect_spec()
        assert "served_model_name" not in spec["overrides"]
        assert "launch" not in spec["overrides"]
        assert "container_name" not in spec["overrides"]
