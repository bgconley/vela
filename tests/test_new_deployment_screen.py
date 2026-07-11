"""Headless tests for the refactored New Deployment wizard + review (Figma 56:2-58:2).

These pin the redesign — the shared StepIndicator, KeyHintBar footers, and the
"opens a dedicated screen" handoff signposting — while re-verifying the review
panel substring contract the smoke suite relies on. The 24 whole-handoff
acceptance tests in test_tui_smoke.py remain the behavioral gate.
"""

from __future__ import annotations

import pytest
from textual.app import App
from textual.css.scalar import Unit
from textual.widgets import Checkbox, Input, Label, Select, Static

from vela.tui.screens.new_deployment import NewDeploymentReviewScreen, NewDeploymentScreen
from vela.tui.widgets import KeyHintBar, StepIndicator


class _Host(App):
    pass


# The exact honest placeholder the pinned-model Select shows on an empty
# registry (bug-236b). Hard-coded here (not imported) so the tests pin the
# literal contract string, not whatever the screen constant happens to be.
_EXPECTED_NO_PINS_PLACEHOLDER = 'No pins on this target — pick "Pin HF repo →"'


def _hint_pairs(keybar: KeyHintBar) -> list[tuple[str, str]]:
    # The rendered (key, label) pairs a KeyHintBar shows, read from its Labels.
    keys = [str(lbl.content) for lbl in keybar.query(".keyhint-key").results(Label)]
    labels = [str(lbl.content) for lbl in keybar.query(".keyhint-label").results(Label)]
    return list(zip(keys, labels, strict=True))


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
async def test_new_deployment_renders_per_section_warning_rows() -> None:
    # A section whose agent RPC failed gets a visible warning row (id per the
    # #new-deployment-* convention) instead of a silently-empty dropdown; the
    # sections that loaded fine stay silent. Kills the old except-Exception:{}
    # swallows (Task 3.2 Part A #3).
    app = _Host()
    async with app.run_test() as pilot:
        screen = NewDeploymentScreen(
            target_label="gpu-node",
            presets=[{"name": "balanced"}],
            section_errors={"builds": "agent-unreachable"},
        )
        await app.push_screen(screen)
        await pilot.pause()
        build_warning = screen.query_one("#new-deployment-build-warning", Static)
        assert build_warning.display is True
        assert "builds unavailable: agent-unreachable" in str(build_warning.content)
        # Recipes/models loaded fine → their rows stay hidden.
        assert screen.query_one("#new-deployment-recipe-warning", Static).display is False
        assert screen.query_one("#new-deployment-model-warning", Static).display is False


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
async def test_review_panel_uses_shared_frame_and_fits_at_80x24() -> None:
    # Task 4.4 (bug-237): the review panel adopts the shared 4.1 modal frame in
    # place of the fixed `width: 92` box that overflowed the 80-col screen. The
    # wizard owns the content (contract-pinned) — this is width/frame only.
    app = _Host()
    async with app.run_test(size=(80, 24)) as pilot:
        screen = NewDeploymentReviewScreen(
            config={"name": "demo", "model": "org/m"},
            preview="vllm serve org/m",
            derived=[],
            warnings=[],
        )
        await app.push_screen(screen)
        await pilot.pause()
        await pilot.pause()
        panel = screen.query_one("#new-deployment-review-panel")
        # 4.1 idiom: percentage width/height, never fixed cells (Unit.WIDTH not CELLS).
        assert panel.styles.width.unit == Unit.WIDTH
        assert panel.styles.height.is_auto
        assert panel.styles.max_height.unit == Unit.HEIGHT
        assert panel.styles.overflow_y == "auto"
        # The whole panel fits within the 80-col terminal, centered — a fixed
        # width: 92 box overflows it (region.x == 0, right == 92).
        assert panel.region.x > 0
        assert panel.region.right <= 80
        assert panel.region.width >= 0.9 * 80
        # The KeyHintBar's last hint (Esc Cancel) is not clipped off the right edge.
        actions = screen.query_one("#new-deployment-review-actions", KeyHintBar)
        cancel = next(lab for lab in actions.query(Label) if str(lab.render()) == "Cancel")
        assert panel.region.x <= cancel.region.x
        assert cancel.region.right <= panel.region.right


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
        # A pin keeps the default source "Existing pin" so this test still
        # exercises the existing→pinned / bare→bare disclosure mapping; bug-236b
        # flips the default to "bare" only on an empty registry.
        screen = NewDeploymentScreen(
            target_label="gpu-node",
            presets=[],
            models=[{"entry_id": "qwen-pin", "display_name": "Qwen Pin"}],
        )
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
    # bug-235 (+ follow-up): a restored draft reopens through the constructor
    # `initial=` path shared by every handoff round-trip and target switch
    # (app._switch_new_deployment_target reopens with `initial=draft`, preserving
    # the draft's own step_index). Mount focus must route through the Enter-safe
    # `_focus_step_entry` — the step's first effectively-visible Input, else the
    # inert step container — AND land after disclosure settles which groups are
    # hidden, so the advertised Enter-walk survives a restore and focus never
    # lands on a widget a later disclosure pass hides.
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
        # The focused widget must be EFFECTIVELY visible, not one whose own
        # .display/.visible read True while a disclosure-hidden ancestor keeps it
        # out of the layout. A laid-out widget has a non-empty region; a widget
        # under a display:none ancestor collapses to a zero-area region. Focus
        # placement must therefore run AFTER the disclosure passes settle.
        assert focused is not None
        assert focused.region.area > 0
        # Enter must advance, not expand the runtime dropdown.
        await pilot.press("enter")
        await pilot.pause()
        assert screen.step_index == 2


@pytest.mark.asyncio
async def test_restored_pin_hf_draft_does_not_re_fire_the_handoff() -> None:
    # bug-250: Cancelling the Pin HF / Adopt local handoff reopens the wizard
    # with the RAW stashed draft, whose model_mode is still the handoff value.
    # _apply_initial restores it into the model-source Select and the deferred
    # Select.Changed fires AFTER _applying_initial clears — re-hitting the
    # handoff branch and dismissing the just-restored wizard (the cancel loop).
    # The restore must coerce a handoff mode to a real Model source so the
    # wizard settles instead of bouncing straight back out to the pin screen.
    app = _Host()
    async with app.run_test() as pilot:
        draft = {
            "step_index": 2,  # Model step
            "model_mode": "pin_hf",
            "model": "Qwen/Qwen3-32B",
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
        # Let the deferred Select.Changed (posted while _applying_initial was
        # True, delivered after it cleared) run its full course.
        for _ in range(6):
            await pilot.pause()
        # The wizard is still up — the restored handoff mode did not self-dismiss.
        assert app.screen is screen
        mode = screen.query_one("#new-deployment-model-mode", Select).value
        assert mode not in {"pin_hf", "adopt_local"}
        # The draft's other fields survived the coercion.
        assert screen.query_one("#new-deployment-model", Input).value == "Qwen/Qwen3-32B"


@pytest.mark.asyncio
async def test_restored_adopt_local_draft_does_not_re_fire_the_handoff() -> None:
    # bug-250 sibling: the Adopt local path handoff restores model_mode=
    # "adopt_local", which must be coerced on restore the same way pin_hf is.
    app = _Host()
    async with app.run_test() as pilot:
        draft = {
            "step_index": 2,
            "model_mode": "adopt_local",
            "model": "/agent/models/qwen",
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
        for _ in range(6):
            await pilot.pause()
        assert app.screen is screen
        mode = screen.query_one("#new-deployment-model-mode", Select).value
        assert mode not in {"pin_hf", "adopt_local"}
        assert (
            screen.query_one("#new-deployment-model", Input).value == "/agent/models/qwen"
        )


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
        # A pin keeps the default source "Existing pin" (bug-236b defaults an
        # empty registry to "bare" instead); this test is about the source→box
        # visibility mapping, not the empty-registry default.
        screen = NewDeploymentScreen(
            target_label="gpu-node",
            presets=[],
            models=[{"entry_id": "qwen-pin", "display_name": "Qwen Pin"}],
        )
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
    # bug-236: pinnable sources must emit the download_now flag into the spec;
    # bare sources must not (the box was reset, so the flag is absent). The
    # review-time gate consuming the flag is pinned app-level by test_tui_smoke
    # .py::test_new_deployment_review_blocks_download_now_without_pin.
    app = _Host()
    async with app.run_test() as pilot:
        # A pin keeps the default source "Existing pin" (bug-236b defaults an
        # empty registry to "bare"); this test asserts the existing→bare source
        # switch drives the download_now flag in/out of the spec.
        screen = NewDeploymentScreen(
            target_label="gpu-node",
            presets=[],
            models=[{"entry_id": "qwen-pin", "display_name": "Qwen Pin"}],
        )
        await app.push_screen(screen)
        await pilot.pause()
        screen.query_one("#new-deployment-model", Input).value = "Qwen/Qwen3-32B"
        screen.query_one("#new-deployment-download-now", Checkbox).value = True
        # Existing pin (default, pinnable) → the flag must survive into the
        # spec; the no-pin block it feeds is asserted by the smoke test above.
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


@pytest.mark.asyncio
async def test_empty_registry_defaults_to_bare_repo_source() -> None:
    # bug-236b: a target with zero pins has nothing to select under "Existing
    # pin", so the Model step defaults to "Bare repo id" — the Model input is
    # immediately visible instead of the dead-end placeholder picker. Download-now
    # (pinnable-only) stays hidden per Task 2.2's rule (compound disclosure).
    app = _Host()
    async with app.run_test() as pilot:
        screen = NewDeploymentScreen(target_label="gpu-node", presets=[], models=[])
        await app.push_screen(screen)
        await pilot.pause()
        assert screen.query_one("#new-deployment-model-mode", Select).value == "bare"
        # The bare Model input group is disclosed so a model id can be typed now;
        # the pinned picker group is hidden.
        assert screen.query_one("#nd-group-bare").display is True
        assert screen.query_one("#nd-group-pinned").display is False
        # Download-now (pinnable-only) is hidden AND unchecked (Task 2.2 rule).
        download = screen.query_one("#new-deployment-download-now", Checkbox)
        assert download.display is False
        assert download.value is False


@pytest.mark.asyncio
async def test_zero_pins_existing_source_shows_honest_placeholder() -> None:
    # bug-236b: if the operator switches to "Existing pin" ANYWAY on an empty
    # registry, the pinned Select shows an honest placeholder row (not the phantom
    # "Custom model"), resolves to no ref, and cannot satisfy Review.
    app = _Host()
    async with app.run_test() as pilot:
        screen = NewDeploymentScreen(target_label="gpu-node", presets=[], models=[])
        await app.push_screen(screen)
        await pilot.pause()
        screen.query_one("#new-deployment-model-mode", Select).value = "existing"
        await pilot.pause()
        assert screen.query_one("#nd-group-pinned").display is True
        # The picker offers only the honest placeholder row — no real refs.
        assert screen._model_options() == [(_EXPECTED_NO_PINS_PLACEHOLDER, "__custom__")]
        # The live Select displays that placeholder as its current label.
        ref_select = screen.query_one("#new-deployment-model-ref", Select)
        current_label = getattr(ref_select.query_one("SelectCurrent"), "label", None)
        assert str(current_label) == _EXPECTED_NO_PINS_PLACEHOLDER
        # It resolves to no ref, so Review is blocked.
        assert screen._selected_model_ref() is None
        with pytest.raises(ValueError, match="Model is required"):
            screen._collect_spec()


@pytest.mark.asyncio
async def test_restored_existing_draft_wins_over_empty_registry_default() -> None:
    # bug-236b: the empty-registry "bare" default applies ONLY when the draft does
    # not pin a mode. A restored draft carrying model_mode="existing" with zero
    # pins must WIN — the mode stays existing, the placeholder Select shows, and
    # mount does not crash.
    app = _Host()
    async with app.run_test() as pilot:
        draft = {
            "step_index": 2,  # Model step
            "model_mode": "existing",
            "selected_target": "gpu-node",
            "preset": "balanced",
        }
        screen = NewDeploymentScreen(
            target_label="gpu-node",
            presets=[{"name": "balanced"}],
            models=[],
            initial=draft,
        )
        await app.push_screen(screen)
        await pilot.pause()
        assert screen.query_one("#new-deployment-model-mode", Select).value == "existing"
        assert screen.query_one("#nd-group-pinned").display is True
        assert screen._selected_model_ref() is None
        assert screen._model_options() == [(_EXPECTED_NO_PINS_PLACEHOLDER, "__custom__")]


@pytest.mark.asyncio
async def test_nonempty_registry_keeps_existing_default_and_custom_model_row() -> None:
    # Scope guard for bug-236b: with >=1 pin the pre-existing behavior is
    # unchanged — the source defaults to "Existing pin" and the pinned Select
    # still offers the "Custom model" sentinel row plus the real pins.
    app = _Host()
    async with app.run_test() as pilot:
        screen = NewDeploymentScreen(
            target_label="gpu-node",
            presets=[],
            models=[{"entry_id": "qwen-pin", "display_name": "Qwen Pin"}],
        )
        await app.push_screen(screen)
        await pilot.pause()
        assert screen.query_one("#new-deployment-model-mode", Select).value == "existing"
        assert screen.query_one("#nd-group-pinned").display is True
        options = screen._model_options()
        assert options[0] == ("Custom model", "__custom__")
        assert any(value == "qwen-pin" for _label, value in options)


@pytest.mark.asyncio
async def test_model_step_blocks_next_without_resolvable_model() -> None:
    # bug-236c bullet 1: Ctrl+N from the Model step with no model resolvable
    # (source=Existing pin, nothing selected, no bare id) must stay on the step,
    # render "Model is required" in the step-adjacent .step-error Static, and
    # mark the breadcrumb with the amber ✗ — not silently advance to a dead-end.
    app = _Host()
    async with app.run_test() as pilot:
        screen = NewDeploymentScreen(target_label="gpu-node", presets=[], models=[])
        await app.push_screen(screen)
        await pilot.pause()
        # Empty registry defaults to bare (bug-236b); drive the trap explicitly.
        screen.query_one("#new-deployment-model-mode", Select).value = "existing"
        await pilot.pause()
        await pilot.press("ctrl+n")  # Target → Runtime
        await pilot.press("ctrl+n")  # Runtime → Model
        await pilot.pause()
        assert screen.step_index == 2
        await pilot.press("ctrl+n")  # blocked: no model resolvable
        await pilot.pause()
        assert screen.step_index == 2  # stays on the Model step
        err = screen.query_one("#new-deployment-model-error", Static)
        assert err.has_class("step-error")
        assert err.display is True
        assert "Model is required" in str(err.content)
        # The step-adjacent Static lives INSIDE the model step group, not only
        # at the panel bottom.
        assert screen.query_one("#new-deployment-step-model").query_one(
            "#new-deployment-model-error", Static
        ) is err
        # The breadcrumb is honest about the failed step.
        steps = screen.query_one("#new-deployment-steps", StepIndicator)
        assert "✗ Model" in str(steps.content)
        # Focus stays Enter-safe: not the error Static, never a Select.
        assert screen.focused is not err
        assert not isinstance(screen.focused, Select)
        # Fix the field: a valid advance clears the error state + Static.
        screen.query_one("#new-deployment-model-mode", Select).value = "bare"
        screen.query_one("#new-deployment-model", Input).value = "Qwen/Qwen3-32B"
        await pilot.pause()
        await pilot.press("ctrl+n")
        await pilot.pause()
        assert screen.step_index == 3
        assert err.display is False
        assert str(err.content) == ""
        assert "✗" not in str(steps.content)


@pytest.mark.asyncio
async def test_reopened_review_error_marks_owning_step_and_offers_ctrl_b() -> None:
    # bug-236c bullet 2: when review-time compose fails with an error we can
    # attribute to a wizard step, the reopened wizard marks that step ✗ in the
    # breadcrumb (instead of a dishonest ✓) and the panel error offers the way
    # back: "… — Ctrl+B back to Model".
    app = _Host()
    async with app.run_test() as pilot:
        draft = {
            "step_index": 4,  # Review — the step the operator submitted from
            "model_mode": "existing",
            "selected_target": "gpu-node",
            "preset": "balanced",
            "download_now": True,
        }
        screen = NewDeploymentScreen(
            target_label="gpu-node",
            presets=[{"name": "balanced"}],
            initial=draft,
            error_message=(
                "Download now requires a pinned model. "
                "Pin the HF repo or choose an existing pin."
            ),
        )
        await app.push_screen(screen)
        await pilot.pause()
        error_text = str(screen.query_one("#new-deployment-error", Static).content)
        # The pinned 2.2 substrings survive (additive contract) …
        assert "Download now requires a pinned model" in error_text
        assert "Pin the HF repo or choose an existing pin" in error_text
        # … and the honest navigation affordance is appended.
        assert error_text.endswith("— Ctrl+B back to Model")
        steps = screen.query_one("#new-deployment-steps", StepIndicator)
        assert "✗ Model" in str(steps.content)  # honest, not ✓
        assert "✓ Model" not in str(steps.content)


@pytest.mark.asyncio
async def test_reopened_unmapped_error_stays_panel_bottom_only() -> None:
    # bug-236c bullet 2 scope guard: errors we cannot attribute to a step render
    # exactly as before — panel bottom, no suffix, no breadcrumb mark.
    app = _Host()
    async with app.run_test() as pilot:
        screen = NewDeploymentScreen(
            target_label="gpu-node",
            presets=[{"name": "balanced"}],
            initial={"step_index": 4, "selected_target": "gpu-node"},
            error_message="target agent exploded",
        )
        await app.push_screen(screen)
        await pilot.pause()
        error_text = str(screen.query_one("#new-deployment-error", Static).content)
        assert error_text == "target agent exploded"  # untouched
        assert "✗" not in str(screen.query_one("#new-deployment-steps", StepIndicator).content)


@pytest.mark.asyncio
async def test_submit_validation_error_marks_owning_step() -> None:
    # bug-236c bullet 2 + item F: Ctrl+S from the Target step (index 0) with no
    # model resolvable renders "Model is required" at #new-deployment-error and
    # marks the Model step ✗. The Model step is AHEAD of the current step, so
    # Ctrl+B is the wrong direction — the affordance is the direction-neutral
    # "see Model step", not a "Ctrl+B to Model" that would walk away from it.
    app = _Host()
    async with app.run_test() as pilot:
        screen = NewDeploymentScreen(target_label="gpu-node", presets=[], models=[])
        await app.push_screen(screen)
        await pilot.pause()
        assert screen.step_index == 0  # submitting from Target, Model is ahead
        await pilot.press("ctrl+s")
        await pilot.pause()
        error_text = str(screen.query_one("#new-deployment-error", Static).content)
        assert error_text == "Model is required — see Model step"
        steps = screen.query_one("#new-deployment-steps", StepIndicator)
        assert "✗ Model" in str(steps.content)


@pytest.mark.asyncio
async def test_model_step_suggestions_drop_sources_debug_line() -> None:
    # bug-236d (item A): the compose-response `sources` metadata (observed live
    # as "sources configured_ports, defaults") was leaking raw into the model
    # step's live suggestions Static. Real engine hints + warnings still render;
    # the internal sources line does not.
    async def resolver(_params: dict[str, object]) -> dict[str, object]:
        return {
            "engine_suggestions": {"dtype": "auto", "kv_cache_dtype": "fp8"},
            "warnings": ["gated-needs-token"],
            "sources": ["configured_ports", "defaults"],
        }

    app = _Host()
    async with app.run_test() as pilot:
        screen = NewDeploymentScreen(
            target_label="gpu-node", presets=[], suggestion_resolver=resolver
        )
        await app.push_screen(screen)
        await pilot.pause()
        screen.query_one("#new-deployment-model", Input).value = "org/m"
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        content = str(
            screen.query_one("#new-deployment-model-suggestions", Static).content
        )
        assert "suggested:" in content  # real engine hints survive
        assert "gated-needs-token" in content  # warnings survive
        assert "sources" not in content  # the internal debug line is gone


@pytest.mark.asyncio
async def test_review_hint_bar_key_case_matches_lowercase_bindings() -> None:
    # bug-236d (item B): the review screen's action keys are lowercase b/f/s
    # bindings, but the KeyHintBar showed uppercase B/F/S — a lie in an app where
    # Shift+letter is a real, distinct binding. The shown case must match reality.
    app = _Host()
    async with app.run_test() as pilot:
        screen = NewDeploymentReviewScreen(
            config={"name": "demo", "model": "org/m"},
            preview="vllm serve org/m",
            derived=[],
            warnings=[],
        )
        await app.push_screen(screen)
        await pilot.pause()
        keybar = screen.query_one("#new-deployment-review-actions", KeyHintBar)
        keys = [key for key, _label in _hint_pairs(keybar)]
        # Single-letter action keys are shown lowercase, matching the bindings.
        assert "b" in keys
        assert "f" in keys
        assert "s" in keys
        # No dishonest uppercase single-letter keys remain.
        assert "B" not in keys
        assert "F" not in keys
        assert "S" not in keys
        # Binding behavior is unchanged: the actions are still keyed b/f/s.
        binding_keys = {
            (binding.key if hasattr(binding, "key") else binding[0])
            for binding in NewDeploymentReviewScreen.BINDINGS
        }
        assert {"b", "f", "s"} <= binding_keys


@pytest.mark.asyncio
async def test_wizard_hint_bar_advertises_enter_advance() -> None:
    # bug-236d (item C): the wizard walks steps on Enter, but the footer only
    # advertised Ctrl+B/Ctrl+N/Ctrl+S/Esc. Surface the Enter-advance too.
    app = _Host()
    async with app.run_test() as pilot:
        screen = NewDeploymentScreen(target_label="gpu-node", presets=[])
        await app.push_screen(screen)
        await pilot.pause()
        keybar = screen.query_one("#new-deployment-footer", KeyHintBar)
        pairs = _hint_pairs(keybar)
        keys = [key for key, _label in pairs]
        assert ("⏎", "Next") in pairs
        # Additive: the existing hints survive and Esc/Cancel stays last.
        assert keys[0] == "Ctrl+B"
        assert keys[-1] == "Esc"


@pytest.mark.asyncio
async def test_advancing_past_fixed_step_clears_stale_panel_error() -> None:
    # bug-236 (item H): a submit-time step error rendered at the panel bottom
    # (#new-deployment-error) went stale — _clear_step_error cleared the
    # breadcrumb ✗ and the step-adjacent Static but left the panel error pinned
    # after the operator fixed the field and advanced. A valid advance that
    # clears a step error now also clears the panel-bottom error IF it maps to
    # that same step (unmapped / panel-only errors stay put).
    app = _Host()
    async with app.run_test() as pilot:
        screen = NewDeploymentScreen(target_label="gpu-node", presets=[], models=[])
        await app.push_screen(screen)
        await pilot.pause()
        # Walk to the Model step; force the dead-end (empty registry defaults to
        # bare, so switch to "Existing pin" with nothing selectable).
        screen.query_one("#new-deployment-model-mode", Select).value = "existing"
        await pilot.pause()
        await pilot.press("ctrl+n")  # Target → Runtime
        await pilot.press("ctrl+n")  # Runtime → Model
        await pilot.pause()
        assert screen.step_index == 2
        # Submit from the Model step (owning == current) → panel-bottom error.
        await pilot.press("ctrl+s")
        await pilot.pause()
        panel = screen.query_one("#new-deployment-error", Static)
        assert str(panel.content) == "Model is required"
        # Fix the field and advance — the now-resolved step clears the panel too.
        screen.query_one("#new-deployment-model-mode", Select).value = "bare"
        screen.query_one("#new-deployment-model", Input).value = "Qwen/Qwen3-32B"
        await pilot.pause()
        await pilot.press("ctrl+n")
        await pilot.pause()
        assert screen.step_index == 3
        assert str(panel.content) == ""


def test_shared_error_constants_bind_the_mapped_prefixes() -> None:
    # bug-236 (item G): the review-time error prefixes are bound ONCE as module
    # constants (in the screen module — app.py imports it, never the reverse) so
    # the wizard's _ERROR_STEP_PREFIXES mapping and app.py's raising sites cannot
    # drift. The literal values are the pinned contract.
    from vela.tui.screens.new_deployment import (
        DOWNLOAD_NEEDS_PIN_ERROR,
        MODEL_REQUIRED_ERROR,
    )

    assert MODEL_REQUIRED_ERROR == "Model is required"
    assert DOWNLOAD_NEEDS_PIN_ERROR == "Download now requires a pinned model"
    # The step-attribution mapping references the constants; both map to Model.
    prefixes = dict(NewDeploymentScreen._ERROR_STEP_PREFIXES)
    model_step = NewDeploymentScreen.STEP_TITLES.index("Model")
    assert prefixes[MODEL_REQUIRED_ERROR] == model_step
    assert prefixes[DOWNLOAD_NEEDS_PIN_ERROR] == model_step


# The exact honest cached-scan helper line, count 1 (M3) — singular "model".
# Hard-coded here (not built from a screen constant) so the test pins the
# literal contract string.
_EXPECTED_SCAN_HELPER_ONE = (
    '1 cached (unpinned) model on this target — "Pin HF repo →" to use one'
)


@pytest.mark.asyncio
async def test_model_step_offers_only_pinned_refs_and_flags_cached_scans() -> None:
    # M3: the pinned-model Select must offer ONLY entries the composer can
    # resolve (real registry pins, pinned=True). Synthetic HF-cache-scan rows
    # (pinned=False, entry_id "repo@sha12") are NOT selectable — compose rejects
    # them as "unknown model reference" — so they are excluded from the picker
    # and summarized in an honest helper line pointing back at "Pin HF repo →".
    app = _Host()
    async with app.run_test() as pilot:
        screen = NewDeploymentScreen(
            target_label="gpu-node",
            presets=[],
            models=[
                {"entry_id": "qwen-pin", "display_name": "Qwen Pin", "pinned": True},
                {
                    "entry_id": "Qwen/Qwen3-32B@abc123def456",
                    "display_name": "Qwen/Qwen3-32B",
                    "pinned": False,
                },
            ],
        )
        await app.push_screen(screen)
        await pilot.pause()
        # One real pin exists → the source defaults to "Existing pin".
        assert screen.query_one("#new-deployment-model-mode", Select).value == "existing"
        assert screen._default_model_mode() == "existing"
        # The picker offers the real pin and NOT the cache-scan row.
        values = [value for _label, value in screen._model_options()]
        assert "qwen-pin" in values
        assert "Qwen/Qwen3-32B@abc123def456" not in values
        # The one excluded scan row is summarized in the honest helper line.
        helper = screen.query_one("#new-deployment-model-scan-help", Static)
        assert helper.display is True
        assert str(helper.content) == _EXPECTED_SCAN_HELPER_ONE


@pytest.mark.asyncio
async def test_scan_only_registry_defaults_to_bare_and_flags_cached_scans() -> None:
    # M3 + bug-236b: a target whose only models are HF-cache-scan rows
    # (pinned=False) has ZERO composer-resolvable pins, so _default_model_mode
    # counts none and the Model step defaults to "Bare repo id". Switching to
    # "Existing pin" ANYWAY shows the honest empty-pins placeholder AND the
    # cached-scan helper line (both present).
    app = _Host()
    async with app.run_test() as pilot:
        screen = NewDeploymentScreen(
            target_label="gpu-node",
            presets=[],
            models=[
                {
                    "entry_id": "Qwen/Qwen3-32B@abc123def456",
                    "display_name": "Qwen/Qwen3-32B",
                    "pinned": False,
                },
            ],
        )
        await app.push_screen(screen)
        await pilot.pause()
        # Scan-only list → no real pins → default bare (2.3 now counts only pins).
        assert screen._default_model_mode() == "bare"
        assert screen.query_one("#new-deployment-model-mode", Select).value == "bare"
        # Switch to "Existing pin": the picker shows only the honest placeholder …
        screen.query_one("#new-deployment-model-mode", Select).value = "existing"
        await pilot.pause()
        assert screen.query_one("#nd-group-pinned").display is True
        assert screen._model_options() == [(_EXPECTED_NO_PINS_PLACEHOLDER, "__custom__")]
        # … and the cached-scan helper line renders alongside it (singular).
        helper = screen.query_one("#new-deployment-model-scan-help", Static)
        assert helper.display is True
        assert "1 cached (unpinned) model on" in str(helper.content)
