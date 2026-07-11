from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Checkbox, Input, Select, Static

from vela.tui.theme import (
    BG_BASE,
    BG_INSET,
    BG_PANEL,
    BORDER_STRONG,
    BORDER_SUBTLE,
    CYAN,
    GREEN,
    RED,
    TEXT_FAINT,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)
from vela.tui.widgets import KeyHintBar, StepIndicator


def _recipe_name(recipe: dict[str, Any]) -> str:
    name = str(recipe.get("name") or "").strip()
    if name:
        return name
    served = str(recipe.get("served_model_name") or "").strip()
    target = str(recipe.get("target") or "").strip()
    if served and target:
        return f"{served}-{target}"
    return ""


def _target_rows(
    targets: list[dict[str, Any]] | None,
    active_target: str,
) -> list[dict[str, Any]]:
    rows = [dict(target) for target in (targets or []) if isinstance(target, dict)]
    active_rows = [
        target for target in rows if str(target.get("name") or "") == active_target
    ]
    if active_rows:
        other_rows = [
            target
            for target in rows
            if str(target.get("name") or "") != active_target
        ]
        return [*active_rows, *other_rows]
    return [{"name": active_target, "transport": "", "host": ""}, *rows]


def _connection_dot(state: str) -> str:
    return {
        "connected": "●",
        "connecting": "◐",
        "reconnecting": "◐",
        "disconnected": "○",
        "version-mismatch": "▲",
        "unreachable": "✕",
    }.get(state, "○")


# Shown in the pinned-model Select when the target has zero pins. Replaces the
# phantom "Custom model" row so switching to "Existing pin" is an honest dead
# end that points back at the "Pin HF repo →" source. Its value stays the
# __custom__ no-op sentinel, so _selected_model_ref() returns None and Review
# still blocks (bug-236b).
_NO_PINS_PLACEHOLDER = 'No pins on this target — pick "Pin HF repo →"'

# Review-time validation-error prefixes, bound ONCE here and shared with the
# app-side handlers. app.py imports this module (never the reverse), so defining
# them here is the correct import direction and keeps the wizard's
# _ERROR_STEP_PREFIXES mapping and app.py's raising sites from drifting apart.
# These are prefixes: app-side messages append guidance suffixes.
MODEL_REQUIRED_ERROR = "Model is required"
DOWNLOAD_NEEDS_PIN_ERROR = "Download now requires a pinned model"


class NewDeploymentScreen(ModalScreen[dict[str, Any] | None]):
    CSS = f"""
    NewDeploymentScreen {{
        align: center middle;
        background: {BG_BASE};
    }}

    #new-deployment-panel {{
        width: 76;
        max-height: 90%;
        overflow-y: auto;
        border: round {BORDER_STRONG};
        background: {BG_PANEL};
        padding: 1 2;
    }}

    #new-deployment-title {{ color: {CYAN}; text-style: bold; }}

    #new-deployment-target {{ margin-bottom: 1; color: {TEXT_FAINT}; }}

    #new-deployment-steps {{ margin-bottom: 1; }}

    #new-deployment-current-step {{
        margin-bottom: 1;
        color: {CYAN};
        text-style: bold;
    }}

    #new-deployment-model-suggestions {{ margin-bottom: 1; color: {TEXT_SECONDARY}; }}
    #new-deployment-model-state {{ color: {TEXT_SECONDARY}; }}
    #new-deployment-target-state {{ color: {TEXT_SECONDARY}; }}

    .new-deployment-field-label {{ margin-top: 1; color: {TEXT_SECONDARY}; }}
    .new-deployment-helper {{ color: {TEXT_FAINT}; }}

    .new-deployment-row {{ height: auto; }}
    .new-deployment-column {{ width: 1fr; height: auto; padding-right: 2; }}

    #nd-group-image, #nd-group-build, #nd-group-executable,
    #nd-group-pinned, #nd-group-bare, #nd-group-derived {{ height: auto; }}

    #new-deployment-error {{ margin-top: 1; color: {RED}; }}
    .step-error {{ margin-top: 1; color: {RED}; }}
    #new-deployment-footer {{ margin-top: 1; }}
    """

    STEP_TITLES = (
        "Target",
        "Runtime",
        "Model",
        "Customize",
        "Review",
        "Save & Smoke",
    )
    STEP_IDS = (
        "#new-deployment-step-target",
        "#new-deployment-step-runtime",
        "#new-deployment-step-model",
        "#new-deployment-step-customize",
        "#new-deployment-step-review",
        "#new-deployment-step-save",
    )
    BINDINGS = [
        Binding("ctrl+n", "next_step", "Next", priority=True),
        Binding("ctrl+b", "previous_step", "Back", priority=True),
        ("enter", "advance_or_submit", "Next"),
        ("ctrl+r", "toggle_advanced", "Advanced"),
        ("ctrl+s", "submit", "Review"),
        ("escape", "cancel", "Cancel"),
    ]

    # Model sources that support Download-now. A pinned entry ("existing") or a
    # fresh HF pin ("pin_hf") resolves to an immutable revision the agent can
    # pre-fetch; a bare repo id or an adopted local path cannot, so the box is
    # hidden and reset for those (bug-236).
    _PREDOWNLOADABLE_MODEL_SOURCES = frozenset({"existing", "pin_hf"})

    # Per-step validation (bug-236c). Steps with an advance gate also carry a
    # step-adjacent .step-error Static so the message renders next to the
    # offending field, not only at the panel bottom.
    _MODEL_STEP_INDEX = STEP_TITLES.index("Model")
    _STEP_ERROR_STATICS = {_MODEL_STEP_INDEX: "#new-deployment-model-error"}
    # Known review-time validation errors attributed to the wizard step that
    # owns the field (matched by prefix — app-side messages carry guidance
    # suffixes). First match wins (see _error_step_for). Unmapped errors render
    # exactly as before: panel bottom only.
    _ERROR_STEP_PREFIXES: tuple[tuple[str, int], ...] = (
        (MODEL_REQUIRED_ERROR, _MODEL_STEP_INDEX),
        (DOWNLOAD_NEEDS_PIN_ERROR, _MODEL_STEP_INDEX),
    )

    def __init__(
        self,
        *,
        target_label: str,
        presets: list[dict[str, Any]],
        recipes: list[dict[str, Any]] | None = None,
        models: list[dict[str, Any]] | None = None,
        builds: list[dict[str, Any]] | None = None,
        initial: dict[str, Any] | None = None,
        targets: list[dict[str, Any]] | None = None,
        connection_state: str = "disconnected",
        agent_info: dict[str, Any] | None = None,
        error_message: str = "",
        target_state_resolver: Callable[[], Awaitable[list[dict[str, Any]]]]
        | None = None,
        suggestion_resolver: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
        | None = None,
    ) -> None:
        super().__init__(id="new-deployment")
        self.error_message = error_message
        # Captured at submit so the app can restore the wizard if server-side
        # review fails — the dismiss payload (the spec) stays contract-exact.
        self.last_draft: dict[str, Any] | None = None
        self.target_label = target_label
        self.presets = [dict(preset) for preset in presets]
        self.recipes = [dict(recipe) for recipe in (recipes or [])]
        self.models = [dict(model) for model in (models or [])]
        self.builds = [dict(build) for build in (builds or [])]
        self.initial = dict(initial or {})
        self.targets = _target_rows(targets, target_label)
        self.connection_state = connection_state
        self.agent_info = dict(agent_info or {})
        self.target_state_resolver = target_state_resolver
        self.suggestion_resolver = suggestion_resolver
        self._suggestion_revision = 0
        self._suppress_model_suggestion_events = False
        self._suppress_target_events = False
        self._target_selection_touched = False
        self._applying_initial = False
        self._advanced_visible = any(
            (initial or {}).get(key)
            for key in ("served_model_name", "runs_dir", "container_name")
        )
        self.step_index = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="new-deployment-panel"):
            yield Static("New Deployment", id="new-deployment-title")
            yield Static(f"Target: {self.target_label}", id="new-deployment-target")
            yield StepIndicator(
                self.STEP_TITLES, current=self.step_index, id="new-deployment-steps"
            )
            yield Static("", id="new-deployment-current-step")
            with Vertical(id="new-deployment-step-target"):
                yield Static("Target", classes="new-deployment-field-label")
                yield Select(
                    self._target_options(),
                    value=self.target_label,
                    allow_blank=False,
                    id="new-deployment-target-select",
                )
                yield Static("", id="new-deployment-target-state")
                yield Static("Recipe", classes="new-deployment-field-label")
                yield Select(
                    self._recipe_options(),
                    value="__custom__",
                    allow_blank=False,
                    id="new-deployment-recipe",
                )
                yield Static(
                    "A validated stack for this target — picking one pre-fills "
                    "runtime, image, model, flags & port. Custom starts blank.",
                    id="new-deployment-recipe-note",
                    classes="new-deployment-helper",
                )
                yield Static("Name", classes="new-deployment-field-label")
                yield Input(placeholder="qwen3-32b-bf16", id="new-deployment-name")
            with Vertical(id="new-deployment-step-runtime"):
                yield Static("Runtime", classes="new-deployment-field-label")
                yield Select(
                    [
                        ("Process", "process"),
                        ("Docker", "docker"),
                        ("Build", "build"),
                        ("Create build →", "create_build"),
                        ("Adopt venv →", "adopt_build"),
                        ("Executable", "executable"),
                    ],
                    value="process",
                    allow_blank=False,
                    id="new-deployment-runtime",
                )
                yield Static(
                    "Create build / Adopt venv open a dedicated screen, then return here.",
                    classes="new-deployment-helper",
                )
                with Vertical(id="nd-group-image"):
                    yield Static("Docker image", classes="new-deployment-field-label")
                    yield Input(
                        placeholder="vllm/vllm-openai@sha256:...",
                        id="new-deployment-image",
                    )
                    yield Static(
                        "Blank = recipe/preset default. Pin a digest "
                        "(vllm/vllm-openai@sha256:…) from Docker Hub for reproducibility.",
                        id="new-deployment-image-help",
                        classes="new-deployment-helper",
                    )
                with Vertical(id="nd-group-build"):
                    yield Static("Build", classes="new-deployment-field-label")
                    yield Select(
                        self._build_options(),
                        value="__custom__",
                        allow_blank=False,
                        id="new-deployment-build-select",
                    )
                    yield Input(
                        placeholder="target build id or label",
                        id="new-deployment-build",
                    )
                with Vertical(id="nd-group-executable"):
                    yield Static("Executable", classes="new-deployment-field-label")
                    yield Input(
                        placeholder="/path/to/vllm",
                        id="new-deployment-executable",
                    )
            with Vertical(id="new-deployment-step-model"):
                yield Static("Model source", classes="new-deployment-field-label")
                yield Select(
                    [
                        ("Existing pin", "existing"),
                        ("Pin HF repo →", "pin_hf"),
                        ("Adopt local path →", "adopt_local"),
                        ("Bare repo id", "bare"),
                    ],
                    value=self._default_model_mode(),
                    allow_blank=False,
                    id="new-deployment-model-mode",
                )
                yield Static(
                    "Pin HF repo / Adopt local path open a dedicated screen, then return here.",
                    classes="new-deployment-helper",
                )
                with Vertical(id="nd-group-pinned"):
                    yield Static("Pinned model", classes="new-deployment-field-label")
                    yield Select(
                        self._model_options(),
                        value="__custom__",
                        allow_blank=False,
                        id="new-deployment-model-ref",
                    )
                    yield Static("", id="new-deployment-model-state")
                    # Cached-but-unpinned signpost (M3): filled + display-toggled
                    # by _render_model_scan_help when the target has HF-cache-scan
                    # rows the picker excluded. Bare Static (no wrapper container)
                    # per the step's helper convention; living inside
                    # #nd-group-pinned means the mode disclosure gates it too.
                    model_scan_help = Static(
                        "",
                        id="new-deployment-model-scan-help",
                        classes="new-deployment-helper",
                    )
                    model_scan_help.display = False
                    yield model_scan_help
                yield Static("", id="new-deployment-model-suggestions")
                with Vertical(id="nd-group-bare"):
                    yield Static("Model", classes="new-deployment-field-label")
                    yield Static(
                        "No pin: the repo id is resolved at launch — no immutable "
                        "commit, no pre-download. Pin it (mode above) for "
                        "reproducibility.",
                        id="new-deployment-bare-help",
                        classes="new-deployment-helper",
                    )
                    yield Input(
                        placeholder="Qwen/Qwen3-32B or /agent/models/model",
                        id="new-deployment-model",
                    )
                yield Static("Revision", classes="new-deployment-field-label")
                yield Input(
                    placeholder="main, tag, or commit",
                    id="new-deployment-model-revision",
                )
                yield Checkbox("Download now", id="new-deployment-download-now")
                # Step-adjacent validation message (bug-236c): a bare Static
                # with a display toggle — no wrapper container to inflate.
                model_error = Static(
                    "", id="new-deployment-model-error", classes="step-error"
                )
                model_error.display = False
                yield model_error
            with Vertical(id="new-deployment-step-customize"):
                with Horizontal(classes="new-deployment-row"):
                    with Vertical(classes="new-deployment-column"):
                        yield Static("Preset", classes="new-deployment-field-label")
                        yield Select(
                            self._preset_options(),
                            value=self._default_preset(),
                            allow_blank=False,
                            id="new-deployment-preset",
                        )
                        yield Static(
                            "",
                            id="new-deployment-preset-help",
                            classes="new-deployment-helper",
                        )
                        yield Static("Host", classes="new-deployment-field-label")
                        yield Input(
                            value="127.0.0.1",
                            placeholder="127.0.0.1",
                            id="new-deployment-host",
                        )
                    with Vertical(classes="new-deployment-column"):
                        yield Static("Port", classes="new-deployment-field-label")
                        yield Input(placeholder="auto", id="new-deployment-port")
                        yield Static(
                            "Blank = auto-allocated on the target (collision-safe).",
                            id="new-deployment-port-help",
                            classes="new-deployment-helper",
                        )
                        yield Static("Exposure", classes="new-deployment-field-label")
                        yield Select(
                            [("Local", "local"), ("LAN", "lan"), ("Public", "public")],
                            value="local",
                            allow_blank=False,
                            id="new-deployment-exposure",
                        )
                yield Static(
                    "Ctrl+R Advanced — override the auto-derived served name, "
                    "runs dir & container name",
                    id="new-deployment-derived-hint",
                    classes="new-deployment-helper",
                )
                with Vertical(id="nd-group-derived"):
                    yield Static(
                        "Served model name", classes="new-deployment-field-label"
                    )
                    yield Input(
                        placeholder="auto — derived from the model",
                        id="new-deployment-served-name",
                    )
                    yield Static("Runs dir", classes="new-deployment-field-label")
                    yield Input(
                        placeholder="auto — per-deployment under the state dir",
                        id="new-deployment-runs-dir",
                    )
                    yield Static(
                        "Container name", classes="new-deployment-field-label"
                    )
                    yield Input(
                        placeholder="auto — vela-<name> (docker runtime only)",
                        id="new-deployment-container-name",
                    )
            with Vertical(id="new-deployment-step-review"):
                yield Static("Review", classes="new-deployment-field-label")
                yield Static(
                    "Press Ctrl+S to compose, validate, and preview the deployment.",
                    id="new-deployment-review-summary",
                )
            with Vertical(id="new-deployment-step-save"):
                yield Static("Save & Smoke", classes="new-deployment-field-label")
                yield Static(
                    "The next screen writes the target-local config after review.",
                    id="new-deployment-save-summary",
                )
            yield Static("", id="new-deployment-error")
            yield KeyHintBar(
                [
                    ("Ctrl+B", "Back"),
                    ("Ctrl+N", "Next"),
                    ("Ctrl+S", "Review"),
                    ("⏎", "Next"),
                    ("Esc", "Cancel"),
                ],
                id="new-deployment-footer",
            )

    def on_mount(self) -> None:
        # Step containers are focus anchors: when a step has no visible Input,
        # focusing the container keeps Textual's focus-restoration from
        # grabbing a Select (which would swallow the Enter-walk).
        for selector in self.STEP_IDS:
            self.query_one(selector).can_focus = True
        self._apply_initial()
        self._render_preset_help()
        self._apply_runtime_disclosure()
        self._apply_model_disclosure()
        self._render_model_scan_help()
        self._apply_advanced_disclosure()
        # Render + focus the step LAST: _focus_step_entry (inside _refresh_step)
        # picks the step's first EFFECTIVELY-visible Input, so it must run AFTER
        # the disclosure passes settle which groups are hidden — otherwise a
        # restored draft focuses a widget (e.g. #new-deployment-image) that a
        # later disclosure pass then hides, leaving focus on an invisible,
        # zero-region widget (Enter-safe but invisible; bug-235 follow-up).
        self._refresh_step()
        if self.error_message:
            self._render_wizard_error(self.error_message)
        self.call_later(self._queue_target_state_refresh)
        self._queue_model_suggestions()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.action_advance_or_submit()

    def on_input_changed(self, event: Input.Changed) -> None:
        if self._applying_initial:
            return
        if self._suppress_model_suggestion_events:
            return
        if event.input.id in {
            "new-deployment-name",
            "new-deployment-model",
            "new-deployment-model-revision",
            "new-deployment-port",
        }:
            if event.input.id == "new-deployment-model":
                self._refresh_name_suggestion()
            self._queue_model_suggestions()

    def on_select_changed(self, event: Select.Changed) -> None:
        if self.app.screen is not self:
            return
        if self._applying_initial:
            return
        if event.select.id == "new-deployment-target-select":
            if self._suppress_target_events:
                return
            target = str(event.value or "")
            if target and target != self.target_label:
                self._target_selection_touched = True
                event.stop()
                draft = self._draft_state()
                draft["target"] = target
                draft["selected_target"] = target
                self.dismiss({"action": "target", "target": target, "draft": draft})
            else:
                self._refresh_target_state()
            return
        if event.select.id == "new-deployment-runtime":
            runtime = str(event.value or "")
            if runtime in {"create_build", "adopt_build"}:
                event.stop()
                self.dismiss({"action": runtime, "draft": self._draft_state()})
            else:
                self._apply_runtime_disclosure()
                self._queue_model_suggestions()
            return
        if event.select.id == "new-deployment-model-mode":
            mode = str(event.value or "")
            if mode in {"pin_hf", "adopt_local"}:
                event.stop()
                # Hide/reset download-now for unpinnable sources before capturing
                # the draft so a stale check can't carry forward and dead-end
                # Review after the handoff returns (bug-236). Delegated to the
                # single disclosure path so the reset rule lives in one place.
                self._apply_model_disclosure()
                self.dismiss(
                    {
                        "action": "pin_model",
                        "draft": self._draft_state(),
                        "initial": self._pin_model_initial(mode),
                    }
                )
                return
            self._apply_model_disclosure()
            return
        if event.select.id == "new-deployment-recipe":
            event.stop()
            self._apply_recipe(str(event.value or ""))
            return
        if event.select.id == "new-deployment-preset":
            self._render_preset_help()
            return
        if event.select.id == "new-deployment-model-ref":
            event.stop()
            self._apply_model_ref(str(event.value or ""))
            self._refresh_model_state()
            self._refresh_name_suggestion()
            self._queue_model_suggestions()
            return
        if event.select.id == "new-deployment-build-select":
            event.stop()
            self._apply_build(str(event.value or ""))
            return

    def action_next_step(self) -> None:
        # Per-step advance gate (bug-236c): a step that cannot possibly review
        # blocks Next HERE, with the message next to the field — instead of
        # letting the operator walk into a Review dead-end. Focus is left
        # untouched on a block so the Enter-walk stays Enter-safe (bug-235).
        error = self._validate_step(self.step_index)
        if error is not None:
            self._mark_step_error(self.step_index, error)
            return
        self._clear_step_error(self.step_index)
        # Detach focus BEFORE hiding the old step: Textual otherwise restores
        # focus to the next focusable widget (a Select, which then swallows
        # Enter) after our own focus handling has run.
        self.set_focus(None)
        self.step_index = min(self.step_index + 1, len(self.STEP_TITLES) - 1)
        self._refresh_step()
        self._focus_step_entry()

    def _validate_step(self, index: int) -> str | None:
        """Advance gate for a wizard step; None means the step may advance.

        One rule today: the Model step must resolve a model — no pinned ref
        selected, no bare repo id typed, and a non-handoff source ("pin_hf" /
        "adopt_local" dismiss to a dedicated screen and never advance) means
        Next would walk into a guaranteed Review failure. More rules slot in
        per step index as they earn their keep.
        """
        if index == self._MODEL_STEP_INDEX:
            mode = str(self.query_one("#new-deployment-model-mode", Select).value or "")
            if (
                mode not in {"pin_hf", "adopt_local"}
                and self._selected_model_ref() is None
                and not self._field_value("#new-deployment-model")
            ):
                return MODEL_REQUIRED_ERROR
        return None

    def _mark_step_error(self, index: int, message: str) -> None:
        selector = self._STEP_ERROR_STATICS.get(index)
        if selector is not None:
            widget = self.query_one(selector, Static)
            widget.update(message)
            widget.display = True
        self.query_one("#new-deployment-steps", StepIndicator).set_error(index)

    def _clear_step_error(self, index: int) -> None:
        selector = self._STEP_ERROR_STATICS.get(index)
        if selector is not None:
            widget = self.query_one(selector, Static)
            widget.update("")
            widget.display = False
        self.query_one("#new-deployment-steps", StepIndicator).clear_error(index)
        # A resolved step also clears a stale panel-bottom error that was
        # attributed to it (item H) — otherwise "Model is required — …" lingered
        # after the operator fixed the field and advanced. Panel-only / unmapped
        # errors (owning step None or a different step) are left untouched.
        panel = self.query_one("#new-deployment-error", Static)
        if self._error_step_for(str(panel.content)) == index:
            panel.update("")

    def action_previous_step(self) -> None:
        self.set_focus(None)
        self.step_index = max(self.step_index - 1, 0)
        self._refresh_step()
        self._focus_step_entry()

    def _focus_step_entry(self) -> None:
        # Keep the Enter-walk chaining: focus the step's first Input so Enter
        # advances again; with no Input (e.g. Review) clear focus so the
        # screen-level Enter binding fires instead of a Select swallowing it.
        try:
            container = self.query_one(self.STEP_IDS[self.step_index])
        except Exception:
            return
        for input_widget in container.query(Input):
            if self._effectively_visible(input_widget, container):
                input_widget.focus()
                return
        container.focus()

    @staticmethod
    def _effectively_visible(widget: Input, container: object) -> bool:
        node = widget
        while node is not None and node is not container:
            if not node.display:
                return False
            node = node.parent
        return True

    def action_advance_or_submit(self) -> None:
        # Enter walks the steps; only the Review step submits the wizard.
        # Ctrl+S remains the submit-from-anywhere shortcut.
        if self.step_index >= 4:
            self.action_submit()
        else:
            self.action_next_step()

    def action_submit(self) -> None:
        try:
            spec = self._collect_spec()
        except ValueError as exc:
            self._render_wizard_error(str(exc))
            return
        self.last_draft = self._draft_state()
        self.dismiss(spec)

    def _error_step_for(self, message: str) -> int | None:
        """First-match-wins step attribution for a review-time error message.

        Matches by prefix (app-side messages append guidance suffixes); returns
        the owning step index, or None for panel-only / unmapped errors.
        """
        return next(
            (
                index
                for prefix, index in self._ERROR_STEP_PREFIXES
                if message.startswith(prefix)
            ),
            None,
        )

    def _render_wizard_error(self, message: str) -> None:
        """Render a validation error at #new-deployment-error (pinned contract).

        When the message is attributable to a wizard step (bug-236c), the
        breadcrumb marks that step ✗ instead of a dishonest ✓ and the text gains
        a direction-honest pointer to it (item F). Unmapped messages render
        exactly as before.
        """
        step_index = self._error_step_for(message)
        if step_index is not None:
            self.query_one("#new-deployment-steps", StepIndicator).set_error(step_index)
            if step_index < self.step_index:
                # The owning step is behind us — Ctrl+B steps back toward it (one
                # step per press; the owning step may be several presses away).
                message = f"{message} — Ctrl+B back to {self.STEP_TITLES[step_index]}"
            elif step_index > self.step_index:
                # The owning step is ahead — Ctrl+B is the wrong direction, so
                # point at it without prescribing a key that walks away from it.
                message = f"{message} — see {self.STEP_TITLES[step_index]} step"
            # owning == current: the operator is already on the step with its
            # fields in view, so the message alone is the fix — no nav suffix.
        self.query_one("#new-deployment-error", Static).update(message)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _collect_spec(self) -> dict[str, Any]:
        name = self._field_value("#new-deployment-name")
        model = self._field_value("#new-deployment-model")
        model_ref = self._selected_model_ref()
        model_revision = self._field_value("#new-deployment-model-revision")
        target = self._selected_target()
        runtime = str(self.query_one("#new-deployment-runtime", Select).value or "process")
        image = self._field_value("#new-deployment-image")
        build = self._field_value("#new-deployment-build")
        executable = self._field_value("#new-deployment-executable")
        preset = str(self.query_one("#new-deployment-preset", Select).value or "balanced")
        host = self._field_value("#new-deployment-host") or "127.0.0.1"
        port = self._field_value("#new-deployment-port")
        exposure = str(self.query_one("#new-deployment-exposure", Select).value or "local")
        if not model and model_ref is None:
            raise ValueError(MODEL_REQUIRED_ERROR)
        if not name:
            name = self._suggested_name()
        if not name:
            raise ValueError("Name is required")
        spec: dict[str, Any] = {
            "name": name,
            "target": target,
            "preset": preset,
            "runtime": runtime,
            "overrides": {"server": {"host": host, "exposure": exposure}},
        }
        served_name = self._field_value("#new-deployment-served-name")
        if served_name:
            spec["overrides"]["served_model_name"] = served_name
        runs_dir = self._field_value("#new-deployment-runs-dir")
        if runs_dir:
            spec["overrides"]["launch"] = {"runs_dir": runs_dir}
        container_name = self._field_value("#new-deployment-container-name")
        if container_name:
            spec["overrides"]["container_name"] = container_name
        if model:
            spec["model"] = model
        if model_ref is not None:
            spec["model_ref"] = model_ref
            revision = model_revision or self._selected_model_revision(model_ref)
            if revision:
                spec["revision"] = revision
        elif model_revision:
            spec["revision"] = model_revision
        if bool(self.query_one("#new-deployment-download-now", Checkbox).value):
            spec["download_now"] = True
        if port:
            try:
                spec["overrides"]["server"]["port"] = int(port)
            except ValueError as exc:
                raise ValueError("Port must be an integer or blank") from exc
        if runtime == "docker":
            spec["runtime"] = {"kind": "docker"}
            if image:
                spec["runtime"]["image"] = image
        elif runtime == "build":
            if not build:
                raise ValueError("Build is required for build runtime")
            spec["runtime"] = {"kind": "build", "build": build}
        elif runtime == "executable":
            if not executable:
                raise ValueError("Executable is required for executable runtime")
            spec["runtime"] = {"kind": "executable", "executable": executable}
        warnings = _warning_texts(self.initial.get("warnings"))
        if warnings:
            spec["warnings"] = warnings
        return spec

    def _draft_state(self) -> dict[str, Any]:
        model_ref = self._selected_model_ref()
        draft: dict[str, Any] = {
            "name": self._field_value("#new-deployment-name"),
            "target": self._selected_target(),
            "selected_target": self._selected_target(),
            "runtime": str(
                self.query_one("#new-deployment-runtime", Select).value or "process"
            ),
            "model": self._field_value("#new-deployment-model"),
            "model_mode": str(
                self.query_one("#new-deployment-model-mode", Select).value or "existing"
            ),
            "model_revision": self._field_value("#new-deployment-model-revision"),
            "download_now": bool(
                self.query_one("#new-deployment-download-now", Checkbox).value
            ),
            "image": self._field_value("#new-deployment-image"),
            "build": self._field_value("#new-deployment-build"),
            "executable": self._field_value("#new-deployment-executable"),
            "preset": str(self.query_one("#new-deployment-preset", Select).value or "balanced"),
            "host": self._field_value("#new-deployment-host") or "127.0.0.1",
            "port": self._field_value("#new-deployment-port"),
            "exposure": str(
                self.query_one("#new-deployment-exposure", Select).value or "local"
            ),
            "recipe": str(
                self.query_one("#new-deployment-recipe", Select).value or "__custom__"
            ),
            "served_model_name": self._field_value("#new-deployment-served-name"),
            "runs_dir": self._field_value("#new-deployment-runs-dir"),
            "container_name": self._field_value("#new-deployment-container-name"),
            "step_index": self.step_index,
        }
        if model_ref is not None:
            draft["model_ref"] = model_ref
            revision = self._selected_model_revision(model_ref)
            if revision:
                draft["revision"] = revision
        warnings = _warning_texts(self.initial.get("warnings"))
        if warnings:
            draft["warnings"] = warnings
        return draft

    def _field_value(self, selector: str) -> str:
        return self.query_one(selector, Input).value.strip()

    def _selected_target(self) -> str:
        try:
            value = str(self.query_one("#new-deployment-target-select", Select).value or "")
        except Exception:
            value = ""
        return value or self.target_label

    def _pin_model_initial(self, mode: str) -> dict[str, Any]:
        model = self._field_value("#new-deployment-model")
        revision = self._field_value("#new-deployment-model-revision")
        if mode == "pin_hf":
            params: dict[str, Any] = {}
            if model:
                params["repo_id"] = model
            if revision:
                params["revision"] = revision
            return params
        if mode == "adopt_local":
            return {"local_path": model} if model else {}
        return {}

    def _apply_initial(self) -> None:
        initial = self.initial
        if not initial:
            return
        self._applying_initial = True
        try:
            for selector, key in (
                ("#new-deployment-name", "name"),
                ("#new-deployment-model", "model"),
                ("#new-deployment-model-revision", "model_revision"),
                ("#new-deployment-image", "image"),
                ("#new-deployment-build", "build"),
                ("#new-deployment-executable", "executable"),
                ("#new-deployment-host", "host"),
                ("#new-deployment-port", "port"),
                ("#new-deployment-served-name", "served_model_name"),
                ("#new-deployment-runs-dir", "runs_dir"),
                ("#new-deployment-container-name", "container_name"),
            ):
                value = str(initial.get(key) or "").strip()
                if value:
                    self.query_one(selector, Input).value = value
            runtime = _initial_runtime_value(initial)
            if runtime in {"process", "docker", "build", "executable"}:
                self._set_select_value("#new-deployment-runtime", runtime)
            for selector, key in (
                ("#new-deployment-recipe", "recipe"),
                ("#new-deployment-target-select", "selected_target"),
                ("#new-deployment-model-mode", "model_mode"),
                ("#new-deployment-model-ref", "model_ref"),
                ("#new-deployment-preset", "preset"),
                ("#new-deployment-exposure", "exposure"),
            ):
                value = str(initial.get(key) or "").strip()
                if not value:
                    continue
                if key == "model_mode":
                    # Never restore a raw handoff mode into the Select — its
                    # deferred Select.Changed would re-fire the dismissal after
                    # _applying_initial clears (bug-250; see _restored_model_mode).
                    value = self._restored_model_mode(value)
                self._set_select_value(selector, value)
            try:
                step_index = int(initial.get("step_index", 0))
            except (TypeError, ValueError):
                step_index = 0
            self.step_index = max(0, min(step_index, len(self.STEP_TITLES) - 1))
            self.query_one("#new-deployment-download-now", Checkbox).value = bool(
                initial.get("download_now")
            )
        finally:
            self._applying_initial = False
        self._refresh_target_state()
        self._refresh_model_state()

    def _queue_target_state_refresh(self) -> None:
        if self.target_state_resolver is None:
            return

        async def refresh() -> None:
            await self._refresh_target_states()

        self.run_worker(
            refresh,
            name="new-deployment-target-states",
            group="new-deployment-target-states",
            exclusive=True,
            exit_on_error=False,
        )

    async def _refresh_target_states(self) -> None:
        if self.target_state_resolver is None:
            return
        try:
            targets = await self.target_state_resolver()
        except Exception:
            return
        selected = self._selected_target()
        self.targets = _target_rows(targets, self.target_label)
        try:
            select = self.query_one("#new-deployment-target-select", Select)
            options = self._target_options()
            values = {value for _label, value in options}
            if not self._target_selection_touched and self.target_label in values:
                selected = self.target_label
            self._suppress_target_events = True
            try:
                select.set_options(options)
                if selected in values:
                    select.value = selected
            finally:
                self._suppress_target_events = False
            self._refresh_target_state()
        except Exception:
            return

    def _set_select_value(self, selector: str, value: str) -> None:
        try:
            self.query_one(selector, Select).value = value
        except Exception:
            return

    def _target_options(self) -> list[tuple[str, str]]:
        options: list[tuple[str, str]] = []
        for target in self.targets:
            name = str(target.get("name") or "").strip()
            if not name:
                continue
            state = self._target_connection_state(name)
            dot = _connection_dot(state)
            transport = str(target.get("transport") or "").strip()
            host = str(target.get("host") or "").strip()
            detail = host or transport
            label = f"{dot} {name}" if not detail else f"{dot} {name}  {detail}"
            options.append((label, name))
        return options or [(self.target_label, self.target_label)]

    def _refresh_target_state(self) -> None:
        try:
            widget = self.query_one("#new-deployment-target-state", Static)
        except Exception:
            return
        selected = self._selected_target()
        state = self._target_connection_state(selected)
        dot = _connection_dot(state)
        parts = [f"{dot} {selected} {state}"]
        agent = self._target_agent_version(selected)
        if agent:
            parts.append(f"agent {agent}")
        detail = self._target_connection_detail(selected)
        if detail:
            parts.append(detail)
        widget.update("   ".join(parts))

    def _target_connection_state(self, name: str) -> str:
        target = self._target_row(name)
        if target is not None:
            state = str(target.get("connection_state") or "").strip()
            if state:
                return state
        return self.connection_state if name == self.target_label else "disconnected"

    def _target_connection_detail(self, name: str) -> str:
        target = self._target_row(name)
        if target is not None:
            return str(target.get("connection_detail") or "").strip()
        return ""

    def _target_agent_version(self, name: str) -> str:
        target = self._target_row(name)
        if target is not None:
            agent = str(target.get("agent_version") or "").strip()
            if agent:
                return agent
        if name == self.target_label:
            return str(self.agent_info.get("agent_version") or "").strip()
        return ""

    def _target_row(self, name: str) -> dict[str, Any] | None:
        for target in self.targets:
            if str(target.get("name") or "").strip() == name:
                return target
        return None

    def _preset_options(self) -> list[tuple[str, str]]:
        options: list[tuple[str, str]] = []
        for preset in self.presets:
            name = str(preset.get("name") or "").strip()
            if name:
                options.append((name, name))
        return options or [("balanced", "balanced")]

    def _recipe_options(self) -> list[tuple[str, str]]:
        options = [("Custom", "__custom__")]
        for recipe in self.recipes:
            key = str(recipe.get("key") or "").strip()
            label = str(recipe.get("label") or key).strip()
            if key:
                options.append((label, key))
        return options

    def _pinned_model_options(self) -> list[tuple[str, str]]:
        # The real, selectable pins the picker can offer: only registry pins the
        # composer can resolve (model_ref → _entry_for_reference matches the
        # registry file). list_models marks those pinned=True and synthetic
        # HF-cache-scan rows (entry_id "repo@sha12") pinned=False (M3); offering
        # a scan row would dead-end Review with "unknown model reference:
        # repo@sha12". Entries WITHOUT the marker (older/simple test fixtures)
        # are treated as pinned=True — compatible-by-default so bare
        # {"entry_id": ...} dicts keep offering their entry.
        options: list[tuple[str, str]] = []
        for model in self.models:
            if not _is_pinned_entry(model):
                continue
            ref = _model_reference(model)
            if not ref:
                continue
            options.append((_model_option_label(model), ref))
        return options

    def _render_model_scan_help(self) -> None:
        # Honest signpost (M3): count the HF-cache-scan rows the pinned picker
        # excluded (pinned=False) and, when any exist, tell the operator they are
        # cached-but-unpinned and how to make one selectable. Rows WITHOUT the
        # marker are treated as pins (see _is_pinned_entry), so they never
        # inflate this count. Display-toggled; shown in the empty-pins case too.
        count = sum(1 for model in self.models if not _is_pinned_entry(model))
        helper = self.query_one("#new-deployment-model-scan-help", Static)
        if count:
            noun = "model" if count == 1 else "models"
            helper.update(
                f"{count} cached (unpinned) {noun} on this target — "
                '"Pin HF repo →" to use one'
            )
        helper.display = bool(count)

    def _model_options(self) -> list[tuple[str, str]]:
        pins = self._pinned_model_options()
        if not pins:
            # Empty registry: no real refs to offer. Show an honest placeholder
            # (keeping the __custom__ no-op value) instead of the phantom
            # "Custom model" so "Existing pin" is a dead-obvious dead end that
            # points back at "Pin HF repo →" (bug-236b).
            return [(_NO_PINS_PLACEHOLDER, "__custom__")]
        return [("Custom model", "__custom__"), *pins]

    def _default_model_mode(self) -> str:
        # A target with zero pins has nothing to select under "Existing pin", so
        # default the Model source to "Bare repo id" — its Model input is
        # immediately visible instead of the dead-end placeholder picker
        # (bug-236b). A restored draft's model_mode overrides this in
        # _apply_initial, so this only governs the first, draft-less open.
        return "existing" if self._pinned_model_options() else "bare"

    def _restored_model_mode(self, mode: str) -> str:
        # Map a restored draft's model_mode onto a Select value that will NOT
        # re-fire the pin/adopt handoff. A raw handoff draft (Cancel of the Pin
        # HF / Adopt local screen, or any crafted resume) carries model_mode
        # "pin_hf"/"adopt_local"; restoring that into the model-source Select
        # posts a deferred Select.Changed that lands AFTER _applying_initial
        # clears, so on_select_changed re-hits the handoff branch and dismisses
        # the just-restored wizard — Cancel becomes an inescapable reopen→
        # re-dismiss loop (bug-250). Coerce it to a real Model source (mirrors
        # _default_model_mode, but keeps a typed bare model id visible): "bare"
        # when the draft carried a model id or the target has no pins, else
        # "existing". Non-handoff modes pass through untouched, so the
        # successful-pin resume (model_mode already "existing") is unaffected.
        if mode not in {"pin_hf", "adopt_local"}:
            return mode
        has_bare_model = bool(str(self.initial.get("model") or "").strip())
        if has_bare_model or not self._pinned_model_options():
            return "bare"
        return "existing"

    def _build_options(self) -> list[tuple[str, str]]:
        options = [("Custom build", "__custom__")]
        for build in self.builds:
            ref = _build_reference(build)
            if not ref:
                continue
            options.append((_build_option_label(build), ref))
        return options

    def _default_preset(self) -> str:
        names = {value for _, value in self._preset_options()}
        return "balanced" if "balanced" in names else next(iter(names))

    def _suggested_name(self) -> str:
        model = (
            self._field_value("#new-deployment-model")
            or self._selected_model_ref()
            or ""
        )
        slug = "".join(
            ch if ch.isalnum() or ch == "-" else "-"
            for ch in str(model).split("/")[-1].lower()
        ).strip("-")
        target = self._selected_target() or self.target_label
        if not slug:
            return ""
        return f"{slug}-{target}" if target else slug

    def _refresh_name_suggestion(self) -> None:
        suggestion = self._suggested_name()
        name_input = self.query_one("#new-deployment-name", Input)
        name_input.placeholder = (
            f"{suggestion} (blank uses it)" if suggestion else "qwen3-32b-bf16"
        )

    def action_toggle_advanced(self) -> None:
        self._advanced_visible = not self._advanced_visible
        self._apply_advanced_disclosure()

    def _apply_advanced_disclosure(self) -> None:
        self.query_one("#nd-group-derived").display = self._advanced_visible
        self.query_one("#new-deployment-derived-hint").display = not self._advanced_visible

    def _apply_runtime_disclosure(self) -> None:
        runtime = str(self.query_one("#new-deployment-runtime", Select).value or "process")
        visible = {"docker": "image", "build": "build", "executable": "executable"}.get(runtime)
        for key in ("image", "build", "executable"):
            self.query_one(f"#nd-group-{key}").display = key == visible

    def _apply_model_disclosure(self) -> None:
        mode = str(self.query_one("#new-deployment-model-mode", Select).value or "existing")
        # Download-now only applies to pinnable sources. Hide AND reset it for a
        # bare repo id / adopted local path so a stale check can't dead-end
        # Review with "Download now requires a pinned model" (bug-236). Computed
        # before the handoff early-return so a restored bare draft resets too.
        download = self.query_one("#new-deployment-download-now", Checkbox)
        download_visible = mode in self._PREDOWNLOADABLE_MODEL_SOURCES
        download.display = download_visible
        if not download_visible:
            download.value = False
        if mode in {"pin_hf", "adopt_local"}:
            return  # handoffs dismiss immediately; keep the current state
        self.query_one("#nd-group-pinned").display = mode == "existing"
        self.query_one("#nd-group-bare").display = mode == "bare"

    def _render_preset_help(self) -> None:
        selected = str(self.query_one("#new-deployment-preset", Select).value or "")
        description = ""
        for preset in self.presets:
            if str(preset.get("name") or "") == selected:
                description = str(preset.get("description") or "")
                break
        self.query_one("#new-deployment-preset-help", Static).update(description)

    def _apply_recipe(self, key: str) -> None:
        if key == "__custom__":
            return
        recipe = self._recipe_by_key(key)
        if recipe is None:
            return
        applied: list[str] = []
        name = _recipe_name(recipe)
        if name:
            self.query_one("#new-deployment-name", Input).value = name
            applied.append(f"name={name}")
        runtime = str(recipe.get("runtime") or "process")
        if runtime in {"process", "docker", "build", "executable"}:
            self.query_one("#new-deployment-runtime", Select).value = runtime
            applied.append(f"runtime={runtime}")
        model = str(recipe.get("model") or "").strip()
        if not model:
            models = recipe.get("models")
            if isinstance(models, list) and models:
                model = str(models[0] or "").strip()
        if model:
            self.query_one("#new-deployment-model", Input).value = model
            self.query_one("#new-deployment-model-mode", Select).value = "bare"
            applied.append(f"model={model}")
        image = str(recipe.get("image") or "").strip()
        if image:
            self.query_one("#new-deployment-image", Input).value = image
            applied.append("image pinned")
        build = str(recipe.get("build") or "").strip()
        if build:
            self.query_one("#new-deployment-build", Input).value = build
        executable = str(recipe.get("executable") or "").strip()
        if executable:
            self.query_one("#new-deployment-executable", Input).value = executable
        server = recipe.get("server")
        if isinstance(server, dict):
            host = str(server.get("host") or "").strip()
            port = server.get("port")
            exposure = str(server.get("exposure") or "").strip()
            if host:
                self.query_one("#new-deployment-host", Input).value = host
            if port is not None:
                self.query_one("#new-deployment-port", Input).value = str(port)
                applied.append(f"port={port}")
            if exposure in {"local", "lan", "public"}:
                self.query_one("#new-deployment-exposure", Select).value = exposure
                applied.append(f"exposure={exposure}")
        self.query_one("#new-deployment-recipe-note", Static).update(
            "Recipe applied: " + " · ".join(applied)
            if applied
            else "Recipe applied (no fields changed)."
        )

    def _apply_model_ref(self, model_ref: str) -> None:
        if model_ref == "__custom__":
            self._refresh_model_state()
            return
        model = self._model_by_ref(model_ref)
        if model is None:
            self._refresh_model_state()
            return
        model_arg = _model_launch_arg(model)
        self._suppress_model_suggestion_events = True
        try:
            if model_arg:
                self.query_one("#new-deployment-model", Input).value = model_arg
            revision = self._selected_model_revision(model_ref)
            if revision:
                self.query_one("#new-deployment-model-revision", Input).value = revision
            self._set_select_value("#new-deployment-model-mode", "existing")
        finally:
            self._suppress_model_suggestion_events = False
        self._refresh_model_state()

    def _apply_build(self, build_ref: str) -> None:
        if build_ref == "__custom__":
            return
        self.query_one("#new-deployment-runtime", Select).value = "build"
        self.query_one("#new-deployment-build", Input).value = build_ref

    def _recipe_by_key(self, key: str) -> dict[str, Any] | None:
        for recipe in self.recipes:
            if str(recipe.get("key") or "") == key:
                return recipe
        return None

    def _model_by_ref(self, model_ref: str) -> dict[str, Any] | None:
        for model in self.models:
            if _model_reference(model) == model_ref:
                return model
        return None

    def _selected_model_ref(self) -> str | None:
        value = str(self.query_one("#new-deployment-model-ref", Select).value or "")
        if not value or value == "__custom__":
            return None
        return value

    def _selected_model_revision(self, model_ref: str) -> str | None:
        model = self._model_by_ref(model_ref)
        if model is None:
            return None
        for field in ("commit_sha", "revision"):
            value = str(model.get(field) or "").strip()
            if value:
                return value
        return None

    def _refresh_model_state(self) -> None:
        try:
            widget = self.query_one("#new-deployment-model-state", Static)
        except Exception:
            return
        model_ref = self._selected_model_ref()
        if model_ref is None:
            widget.update("cache: unpinned")
            return
        model = self._model_by_ref(model_ref)
        widget.update(_model_state_summary(model) if model is not None else "cache: unknown")

    def _queue_model_suggestions(self) -> None:
        try:
            widget = self.query_one("#new-deployment-model-suggestions", Static)
        except Exception:
            return
        if self.suggestion_resolver is None:
            widget.update("")
            return
        params = self._suggestion_params()
        if params is None:
            widget.update("")
            return
        self._suggestion_revision += 1
        revision = self._suggestion_revision

        async def refresh() -> None:
            await self._refresh_model_suggestions(revision, params)

        self.run_worker(
            refresh,
            name="new-deployment-suggestions",
            group="new-deployment-suggestions",
            exclusive=True,
            exit_on_error=False,
        )

    def _suggestion_params(self) -> dict[str, Any] | None:
        model_ref = self._selected_model_ref()
        model = self._field_value("#new-deployment-model")
        if model_ref is None and not model:
            return None
        params: dict[str, Any] = {
            "target": self._selected_target(),
            "runtime": str(self.query_one("#new-deployment-runtime", Select).value or "process"),
        }
        name = self._field_value("#new-deployment-name")
        if name:
            params["name"] = name
        if model:
            params["model"] = model
        if model_ref is not None:
            params["model_ref"] = model_ref
            revision = (
                self._field_value("#new-deployment-model-revision")
                or self._selected_model_revision(model_ref)
            )
            if revision:
                params["revision"] = revision
        else:
            revision = self._field_value("#new-deployment-model-revision")
            if revision:
                params["revision"] = revision
        port = self._field_value("#new-deployment-port")
        if port:
            try:
                params["preferred_port"] = int(port)
            except ValueError:
                pass
        return params

    async def _refresh_model_suggestions(
        self, revision: int, params: dict[str, Any]
    ) -> None:
        if self.suggestion_resolver is None:
            return
        try:
            result = await self.suggestion_resolver(params)
        except Exception as exc:
            text = f"hints unavailable: {exc}"
        else:
            text = _model_suggestions_summary(result)
        if revision != self._suggestion_revision:
            return
        try:
            self.query_one("#new-deployment-model-suggestions", Static).update(text)
        except Exception:
            return

    def _refresh_step(self) -> None:
        for index, selector in enumerate(self.STEP_IDS):
            self.query_one(selector).display = index == self.step_index
        self.query_one("#new-deployment-steps", StepIndicator).set_current(self.step_index)
        self.query_one("#new-deployment-current-step", Static).update(
            f"Step {self.step_index + 1} of {len(self.STEP_TITLES)}: "
            f"{self.STEP_TITLES[self.step_index]}"
        )
        # Every step refresh (mount restore included) routes focus through the
        # Enter-safe entry helper: focus the step's first Input, else the inert
        # step container. Steps 1/2/3 otherwise land on a Select, which swallows
        # the screen-level "enter" binding and breaks the Enter-walk (bug-235).
        self._focus_step_entry()


def _model_reference(model: dict[str, Any]) -> str:
    return str(model.get("entry_id") or model.get("display_name") or "").strip()


def _is_pinned_entry(model: dict[str, Any]) -> bool:
    # A model row is a real, composer-resolvable pin when list_models marked it
    # pinned=True; synthetic HF-cache-scan rows are pinned=False (M3). A row
    # WITHOUT the field (older/simple test fixtures) is treated as pinned=True —
    # compatible-by-default so bare {"entry_id": ...} dicts still qualify.
    # Tradeoff: that default fails OPEN — an engine regression that dropped the
    # field would re-offer scan rows (M3 recurring) rather than hide real pins;
    # the agent-side exact-payload tests pin the field to catch that.
    value = model.get("pinned")
    if value is None:
        return True
    return bool(value)


def _model_suggestions_summary(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    engine = payload.get("engine_suggestions")
    if isinstance(engine, dict):
        field_order = (
            "dtype",
            "kv_cache_dtype",
            "tensor_parallel_size",
            "max_model_len",
            "max_num_seqs",
            "quantization",
        )
        hints = [
            f"{field}={engine[field]}"
            for field in field_order
            if field in engine and engine[field] not in (None, "")
        ]
        if hints:
            parts.append("suggested: " + " ".join(hints))
    warnings = payload.get("warnings")
    if isinstance(warnings, list) and warnings:
        parts.append("warnings " + ", ".join(str(item) for item in warnings))
    # The compose-response `sources` list is internal provenance metadata
    # ("configured_ports", "defaults", …) — deliberately NOT surfaced to the
    # operator (bug-236d). No screen-side debug sink is wired, so it is simply
    # dropped rather than re-plumbed elsewhere.
    return "   ".join(parts)


def _warning_texts(warnings: object) -> list[str]:
    if not isinstance(warnings, list):
        return []
    texts: list[str] = []
    for warning in warnings:
        if isinstance(warning, dict):
            text = str(
                warning.get("detail") or warning.get("message") or warning.get("kind") or ""
            ).strip()
        else:
            text = str(warning).strip()
        if text:
            texts.append(text)
    return texts


def _initial_runtime_value(initial: dict[str, Any]) -> str:
    runtime = initial.get("runtime")
    if isinstance(runtime, dict):
        return str(runtime.get("kind") or "process")
    return str(runtime or "process")


def _model_option_label(model: dict[str, Any]) -> str:
    label = str(model.get("display_name") or model.get("entry_id") or "model").strip()
    cache = str(model.get("cache_state") or "").strip()
    revision = str(model.get("commit_sha") or model.get("revision") or "").strip()
    suffixes = []
    if cache:
        suffixes.append(cache)
    if revision:
        suffixes.append(revision[:12])
    return f"{label} ({', '.join(suffixes)})" if suffixes else label


def _model_launch_arg(model: dict[str, Any]) -> str:
    for field in ("repo_id", "local_path", "url", "display_name"):
        value = str(model.get(field) or "").strip()
        if value:
            return value
    return ""


def _model_state_summary(model: dict[str, Any]) -> str:
    cache = str(model.get("cache_state") or "unknown").strip() or "unknown"
    parts = [f"cache: {cache}"]
    if model.get("gated") and (model.get("token_required") or model.get("gated")):
        parts.append("auth: gated, requires HF_TOKEN (agent env or config env: block)")
    elif model.get("token_required"):
        parts.append("auth: requires HF_TOKEN (agent env or config env: block)")
    return "   ".join(parts)


def _build_reference(build: dict[str, Any]) -> str:
    return str(build.get("label") or build.get("build_id") or "").strip()


def _build_option_label(build: dict[str, Any]) -> str:
    label = str(build.get("label") or build.get("build_id") or "build").strip()
    status = str(build.get("status") or "").strip()
    resolved = build.get("resolved") if isinstance(build.get("resolved"), dict) else {}
    vllm = str(resolved.get("vllm") or "").strip()
    suffixes = []
    if status:
        suffixes.append(status)
    if vllm:
        suffixes.append(f"vLLM {vllm}")
    return f"{label} ({', '.join(suffixes)})" if suffixes else label


class NewDeploymentReviewScreen(ModalScreen[dict[str, Any] | None]):
    CSS = f"""
    NewDeploymentReviewScreen {{
        align: center middle;
        background: {BG_BASE};
    }}

    #new-deployment-review-panel {{
        width: 92;
        max-height: 90%;
        overflow-y: auto;
        border: round {BORDER_STRONG};
        background: {BG_PANEL};
        padding: 1 2;
    }}

    #new-deployment-review-title {{ color: {CYAN}; text-style: bold; }}

    #new-deployment-review-steps {{ margin-bottom: 1; }}

    .new-deployment-review-label {{
        margin-top: 1;
        color: {TEXT_SECONDARY};
        text-style: bold;
    }}

    #new-deployment-review-summary,
    #new-deployment-review-derived,
    #new-deployment-review-warnings {{
        color: {TEXT_PRIMARY};
    }}

    #new-deployment-review-preview {{
        max-height: 12;
        overflow-y: auto;
        border: round {BORDER_SUBTLE};
        background: {BG_INSET};
        color: {GREEN};
        padding: 0 1;
    }}

    #new-deployment-review-actions {{ margin-top: 1; }}
    """

    BINDINGS = [
        ("b", "back", "Back"),
        ("f", "customize", "Flags"),
        Binding("s", "save_smoke", "Smoke", priority=True),
        ("ctrl+s", "save", "Save"),
        ("escape", "cancel", "Cancel"),
    ]

    def __init__(
        self,
        *,
        config: dict[str, Any],
        preview: str,
        derived: list[dict[str, Any]],
        warnings: list[str],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(id="new-deployment-review")
        self.config = dict(config)
        self.preview = preview
        self.derived = [dict(item) for item in derived]
        self.warnings = list(warnings)
        self.metadata = dict(metadata or {})

    def compose(self) -> ComposeResult:
        with Vertical(id="new-deployment-review-panel"):
            yield Static("Review Deployment", id="new-deployment-review-title")
            yield StepIndicator(
                NewDeploymentScreen.STEP_TITLES, current=4, id="new-deployment-review-steps"
            )
            yield Static("Summary", classes="new-deployment-review-label")
            yield Static(self._summary_text(), id="new-deployment-review-summary")
            yield Static("Auto-derived fields", classes="new-deployment-review-label")
            yield Static(self._derived_text(), id="new-deployment-review-derived")
            yield Static("Warnings", classes="new-deployment-review-label")
            yield Static(self._warnings_text(), id="new-deployment-review-warnings")
            yield Static("Resolved command", classes="new-deployment-review-label")
            yield Static(self.preview, id="new-deployment-review-preview")
            yield KeyHintBar(
                [
                    # Case matches the actual lowercase b/f/s bindings above —
                    # capital letters are distinct Shift-bindings elsewhere in
                    # the app, so showing B/F/S here would be a lie (bug-236d).
                    ("b", "Back"),
                    ("f", "Flags"),
                    ("Ctrl+S", "Save"),
                    ("s", "Save & Smoke"),
                    ("Esc", "Cancel"),
                ],
                id="new-deployment-review-actions",
            )

    def action_back(self) -> None:
        self.dismiss({"action": "back", "config": self.config})

    def action_customize(self) -> None:
        self.dismiss(
            {
                "action": "customize",
                "config": self.config,
                "preview": self.preview,
                "derived": list(self.derived),
                "warnings": list(self.warnings),
                "metadata": dict(self.metadata),
            }
        )

    def action_save(self) -> None:
        self.dismiss({"action": "save", "config": self.config})

    def action_save_smoke(self) -> None:
        self.dismiss({"action": "save_smoke", "config": self.config})

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _summary_text(self) -> str:
        server = self.config.get("server") if isinstance(self.config.get("server"), dict) else {}
        command = (
            self.config.get("command") if isinstance(self.config.get("command"), dict) else {}
        )
        docker = command.get("docker") if isinstance(command.get("docker"), dict) else {}
        lines = [
            f"Name: {self.config.get('name', '')}",
            f"Model: {self.config.get('model', '')}",
            f"Runtime: {command.get('runtime', 'process')}",
            f"Endpoint: {server.get('host', '127.0.0.1')}:{server.get('port', '')}",
            f"Exposure: {server.get('exposure', 'local')}",
        ]
        if docker:
            lines.append(f"Image: {docker.get('image', '')}")
            lines.append(f"Container: {docker.get('container_name', '')}")
        return "\n".join(lines)

    def _derived_text(self) -> str:
        if not self.derived:
            return "No auto-derived fields reported."
        lines = []
        for item in self.derived:
            field = str(item.get("field") or "")
            value = str(item.get("value") or "")
            source = str(item.get("source") or "auto")
            lines.append(f"{field}: {value} ({source})")
        return "\n".join(lines)

    def _warnings_text(self) -> str:
        if not self.warnings:
            return "No warnings."
        return "\n".join(f"- {warning}" for warning in self.warnings)
