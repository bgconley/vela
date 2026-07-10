"""Headless tests for the refactored New Deployment wizard + review (Figma 56:2-58:2).

These pin the redesign — the shared StepIndicator, KeyHintBar footers, and the
"opens a dedicated screen" handoff signposting — while re-verifying the review
panel substring contract the smoke suite relies on. The 24 whole-handoff
acceptance tests in test_tui_smoke.py remain the behavioral gate.
"""

from __future__ import annotations

import pytest
from textual.app import App
from textual.widgets import Checkbox, Input, Select, Static

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
async def test_restored_draft_mount_keeps_enter_walk_off_the_runtime_select() -> None:
    # bug-235: reopening the wizard with a restored draft goes through the
    # constructor `initial=` path shared by every handoff round-trip and target
    # switch (app._switch_new_deployment_target reopens with `initial=draft`,
    # preserving the draft's own step_index). `_refresh_step` used to route mount
    # focus through `_focus_current_step`, which mapped steps 1/2/3 straight to
    # Select widgets — so a Runtime-step (step_index=1) restore landed focus on
    # #new-deployment-runtime and Enter opened its dropdown overlay forever
    # instead of advancing. Every mount must route through the Enter-safe
    # `_focus_step_entry` (focus the step's first Input, else the inert step
    # container) so the advertised Enter-walk survives a restore.
    app = _Host()
    async with app.run_test() as pilot:
        draft = {
            "step_index": 1,  # Runtime step (rendered "Step 2 of 6")
            "runtime": "process",
            "name": "demo",
            "selected_target": "gpu-node",
            "model_mode": "existing",
            "preset": "balanced",
        }
        screen = NewDeploymentScreen(
            target_label="gpu-node",
            presets=[{"name": "balanced"}],
            initial=draft,
        )
        await app.push_screen(screen)
        await pilot.pause()
        focused = screen.focused
        # A focused Select swallows the screen-level "enter" binding; focus must
        # instead be an Input or the step container (the inert focus anchor).
        assert not isinstance(focused, Select)
        step_container = screen.query_one(NewDeploymentScreen.STEP_IDS[1])
        assert isinstance(focused, Input) or focused is step_container
        # Enter now advances the wizard rather than expanding the runtime dropdown.
        await pilot.press("enter")
        await pilot.pause()
        assert screen.step_index == 2


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


@pytest.mark.asyncio
async def test_download_now_hidden_and_reset_for_bare_source() -> None:
    # bug-236: Download-now only applies to pinnable sources. Switching to a
    # bare repo id hides the box AND resets it to False; switching back shows
    # it again without re-checking (the box's state stays independent).
    app = _Host()
    async with app.run_test() as pilot:
        screen = NewDeploymentScreen(target_label="gpu-node", presets=[])
        await app.push_screen(screen)
        await pilot.pause()
        download = screen.query_one("#new-deployment-download-now", Checkbox)
        # Existing pin (the default source) → visible.
        assert download.display is True
        download.value = True  # operator checks it under a pinnable source
        # Bare repo id → nothing to pre-download → hidden + reset.
        screen.query_one("#new-deployment-model-mode", Select).value = "bare"
        await pilot.pause()
        assert download.display is False
        assert download.value is False
        # Back to Existing pin → visible again, but NOT re-checked.
        screen.query_one("#new-deployment-model-mode", Select).value = "existing"
        await pilot.pause()
        assert download.display is True
        assert download.value is False


@pytest.mark.asyncio
async def test_download_now_spec_obeys_model_source() -> None:
    # bug-236: the "Download now requires a pinned model" gate must stay
    # reachable for pinnable sources (the flag reaches the spec) yet never fire
    # for a bare source (the box was reset, so the flag is absent).
    app = _Host()
    async with app.run_test() as pilot:
        screen = NewDeploymentScreen(target_label="gpu-node", presets=[])
        await app.push_screen(screen)
        await pilot.pause()
        screen.query_one("#new-deployment-model", Input).value = "Qwen/Qwen3-32B"
        screen.query_one("#new-deployment-download-now", Checkbox).value = True
        # Existing pin (default, pinnable) → the flag survives into the spec so
        # app.py can still block when no pin is actually selected.
        assert screen._collect_spec().get("download_now") is True
        # Bare repo id → box reset → the flag is gone → Review is never
        # dead-ended by the pinned-model gate.
        screen.query_one("#new-deployment-model-mode", Select).value = "bare"
        await pilot.pause()
        spec = screen._collect_spec()
        assert "download_now" not in spec
        assert spec["model"] == "Qwen/Qwen3-32B"


@pytest.mark.asyncio
async def test_restored_bare_draft_resets_download_now() -> None:
    # bug-236 round-trip: a draft (saved before this fix, or crafted) can carry
    # download_now=True with a bare model source. After restore + disclosure the
    # box must end hidden AND False so Review is not dead-ended.
    app = _Host()
    async with app.run_test() as pilot:
        draft = {
            "step_index": 2,  # Model step
            "model_mode": "bare",
            "model": "Qwen/Qwen3-32B",
            "download_now": True,
            "selected_target": "gpu-node",
            "preset": "balanced",
        }
        screen = NewDeploymentScreen(
            target_label="gpu-node",
            presets=[{"name": "balanced"}],
            initial=draft,
        )
        await app.push_screen(screen)
        await pilot.pause()
        download = screen.query_one("#new-deployment-download-now", Checkbox)
        assert download.display is False
        assert download.value is False
        assert "download_now" not in screen._collect_spec()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,expected_download",
    [("adopt_local", False), ("pin_hf", True)],
)
async def test_model_handoff_draft_download_now_obeys_source(
    mode: str, expected_download: bool
) -> None:
    # bug-236: the "→" model sources dismiss immediately to a dedicated screen,
    # capturing the wizard draft as they go. Adopt local path is unpinnable, so a
    # checked box must be reset to False before the draft is captured; Pin HF
    # repo is pinnable, so the box's checked state must survive into the draft
    # (the download job runs after the pin returns).
    app = _Host()
    captured: list[dict[str, object]] = []
    async with app.run_test() as pilot:
        screen = NewDeploymentScreen(target_label="gpu-node", presets=[])
        await app.push_screen(screen, captured.append)
        await pilot.pause()
        screen.query_one("#new-deployment-model", Input).value = "Qwen/Qwen3-32B"
        screen.query_one("#new-deployment-download-now", Checkbox).value = True
        screen.query_one("#new-deployment-model-mode", Select).value = mode
        await pilot.pause()
    assert captured, "the handoff did not dismiss the wizard"
    result = captured[0]
    assert result["action"] == "pin_model"
    assert result["draft"]["download_now"] is expected_download
