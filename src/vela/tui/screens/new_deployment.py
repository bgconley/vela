from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Checkbox, Input, Select, Static

from vela.tui.theme import ACCENT, BAD, GOOD, MUTED, SURFACE_ALT, TEXT


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
    if any(str(target.get("name") or "") == active_target for target in rows):
        return rows
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


class NewDeploymentScreen(ModalScreen[dict[str, Any] | None]):
    CSS = f"""
    NewDeploymentScreen {{
        align: center middle;
        background: #091015;
    }}

    #new-deployment-panel {{
        width: 76;
        border: solid {ACCENT};
        background: {SURFACE_ALT};
        padding: 1 2;
    }}

    #new-deployment-title {{
        margin-bottom: 1;
        color: {TEXT};
        text-style: bold;
    }}

    #new-deployment-target {{
        margin-bottom: 1;
        color: {MUTED};
    }}

    #new-deployment-steps {{
        margin-bottom: 1;
        color: {ACCENT};
    }}

    #new-deployment-current-step {{
        margin-bottom: 1;
        color: {TEXT};
        text-style: bold;
    }}

    .new-deployment-field-label {{
        margin-top: 1;
        color: {TEXT};
    }}

    .new-deployment-row {{
        height: auto;
    }}

    .new-deployment-column {{
        width: 1fr;
        padding-right: 2;
    }}

    #new-deployment-error {{
        margin-top: 1;
        color: {BAD};
    }}

    #new-deployment-actions {{
        margin-top: 1;
        color: {GOOD};
    }}
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
        ("ctrl+s", "submit", "Review"),
        ("escape", "cancel", "Cancel"),
    ]

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
    ) -> None:
        super().__init__(id="new-deployment")
        self.target_label = target_label
        self.presets = [dict(preset) for preset in presets]
        self.recipes = [dict(recipe) for recipe in (recipes or [])]
        self.models = [dict(model) for model in (models or [])]
        self.builds = [dict(build) for build in (builds or [])]
        self.initial = dict(initial or {})
        self.targets = _target_rows(targets, target_label)
        self.connection_state = connection_state
        self.agent_info = dict(agent_info or {})
        self._applying_initial = False
        self.step_index = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="new-deployment-panel"):
            yield Static("New Deployment", id="new-deployment-title")
            yield Static(f"Target: {self.target_label}", id="new-deployment-target")
            yield Static(
                "Target -> Runtime -> Model -> Customize -> Review -> Save",
                id="new-deployment-steps",
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
                yield Static("Name", classes="new-deployment-field-label")
                yield Input(placeholder="qwen3-32b-bf16", id="new-deployment-name")
            with Vertical(id="new-deployment-step-runtime"):
                yield Static("Runtime", classes="new-deployment-field-label")
                yield Select(
                    [
                        ("Process", "process"),
                        ("Docker", "docker"),
                        ("Build", "build"),
                        ("Create build", "create_build"),
                        ("Adopt venv", "adopt_build"),
                        ("Executable", "executable"),
                    ],
                    value="process",
                    allow_blank=False,
                    id="new-deployment-runtime",
                )
                yield Static("Docker image", classes="new-deployment-field-label")
                yield Input(
                    placeholder="vllm/vllm-openai@sha256:...",
                    id="new-deployment-image",
                )
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
                        ("Pin HF repo", "pin_hf"),
                        ("Adopt local path", "adopt_local"),
                        ("Bare repo id", "bare"),
                    ],
                    value="existing",
                    allow_blank=False,
                    id="new-deployment-model-mode",
                )
                yield Static("Pinned model", classes="new-deployment-field-label")
                yield Select(
                    self._model_options(),
                    value="__custom__",
                    allow_blank=False,
                    id="new-deployment-model-ref",
                )
                yield Static("", id="new-deployment-model-state")
                yield Static("Model", classes="new-deployment-field-label")
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
                        yield Static("Host", classes="new-deployment-field-label")
                        yield Input(
                            value="127.0.0.1",
                            placeholder="127.0.0.1",
                            id="new-deployment-host",
                        )
                    with Vertical(classes="new-deployment-column"):
                        yield Static("Port", classes="new-deployment-field-label")
                        yield Input(placeholder="auto", id="new-deployment-port")
                        yield Static("Exposure", classes="new-deployment-field-label")
                        yield Select(
                            [("Local", "local"), ("LAN", "lan"), ("Public", "public")],
                            value="local",
                            allow_blank=False,
                            id="new-deployment-exposure",
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
            yield Static("", id="new-deployment-actions")

    def on_mount(self) -> None:
        self._apply_initial()
        self._refresh_step()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.action_submit()

    def on_select_changed(self, event: Select.Changed) -> None:
        if self._applying_initial:
            return
        if event.select.id == "new-deployment-target-select":
            target = str(event.value or "")
            if target and target != self.target_label:
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
            return
        if event.select.id == "new-deployment-model-mode":
            mode = str(event.value or "")
            if mode in {"pin_hf", "adopt_local"}:
                event.stop()
                self.dismiss(
                    {
                        "action": "pin_model",
                        "draft": self._draft_state(),
                        "initial": self._pin_model_initial(mode),
                    }
                )
            return
        if event.select.id == "new-deployment-recipe":
            event.stop()
            self._apply_recipe(str(event.value or ""))
            return
        if event.select.id == "new-deployment-model-ref":
            event.stop()
            self._apply_model_ref(str(event.value or ""))
            self._refresh_model_state()
            return
        if event.select.id == "new-deployment-build-select":
            event.stop()
            self._apply_build(str(event.value or ""))
            return

    def action_next_step(self) -> None:
        self.step_index = min(self.step_index + 1, len(self.STEP_TITLES) - 1)
        self._refresh_step()

    def action_previous_step(self) -> None:
        self.step_index = max(self.step_index - 1, 0)
        self._refresh_step()

    def action_submit(self) -> None:
        try:
            self.dismiss(self._collect_spec())
        except ValueError as exc:
            self.query_one("#new-deployment-error", Static).update(str(exc))

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
        if not name:
            raise ValueError("Name is required")
        if not model and model_ref is None:
            raise ValueError("Model is required")
        spec: dict[str, Any] = {
            "name": name,
            "target": target,
            "preset": preset,
            "runtime": runtime,
            "overrides": {"server": {"host": host, "exposure": exposure}},
        }
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
            "step_index": self.step_index,
        }
        if model_ref is not None:
            draft["model_ref"] = model_ref
            revision = self._selected_model_revision(model_ref)
            if revision:
                draft["revision"] = revision
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
                if value:
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
            dot = _connection_dot(self.connection_state) if name == self.target_label else "○"
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
        if selected == self.target_label:
            dot = _connection_dot(self.connection_state)
            parts = [f"{dot} {selected} {self.connection_state}"]
            agent = str(self.agent_info.get("agent_version") or "").strip()
            if agent:
                parts.append(f"agent {agent}")
            widget.update("   ".join(parts))
            return
        widget.update(f"○ {selected} inactive")

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

    def _model_options(self) -> list[tuple[str, str]]:
        options = [("Custom model", "__custom__")]
        for model in self.models:
            ref = _model_reference(model)
            if not ref:
                continue
            options.append((_model_option_label(model), ref))
        return options

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

    def _apply_recipe(self, key: str) -> None:
        if key == "__custom__":
            return
        recipe = self._recipe_by_key(key)
        if recipe is None:
            return
        name = _recipe_name(recipe)
        if name:
            self.query_one("#new-deployment-name", Input).value = name
        runtime = str(recipe.get("runtime") or "process")
        if runtime in {"process", "docker", "build", "executable"}:
            self.query_one("#new-deployment-runtime", Select).value = runtime
        model = str(recipe.get("model") or "").strip()
        if not model:
            models = recipe.get("models")
            if isinstance(models, list) and models:
                model = str(models[0] or "").strip()
        if model:
            self.query_one("#new-deployment-model", Input).value = model
        image = str(recipe.get("image") or "").strip()
        if image:
            self.query_one("#new-deployment-image", Input).value = image
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
            if exposure in {"local", "lan", "public"}:
                self.query_one("#new-deployment-exposure", Select).value = exposure

    def _apply_model_ref(self, model_ref: str) -> None:
        if model_ref == "__custom__":
            self._refresh_model_state()
            return
        model = self._model_by_ref(model_ref)
        if model is None:
            self._refresh_model_state()
            return
        model_arg = _model_launch_arg(model)
        if model_arg:
            self.query_one("#new-deployment-model", Input).value = model_arg
        revision = self._selected_model_revision(model_ref)
        if revision:
            self.query_one("#new-deployment-model-revision", Input).value = revision
        self._set_select_value("#new-deployment-model-mode", "existing")
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

    def _refresh_step(self) -> None:
        for index, selector in enumerate(self.STEP_IDS):
            self.query_one(selector).display = index == self.step_index
        self.query_one("#new-deployment-current-step", Static).update(
            f"Step {self.step_index + 1} of {len(self.STEP_TITLES)}: "
            f"{self.STEP_TITLES[self.step_index]}"
        )
        self.query_one("#new-deployment-actions", Static).update(self._actions_text())
        self._focus_current_step()

    def _actions_text(self) -> str:
        parts: list[str] = []
        if self.step_index > 0:
            parts.append("Ctrl+B Back")
        if self.step_index < len(self.STEP_TITLES) - 1:
            parts.append("Ctrl+N Next")
        parts.append("Ctrl+S Review")
        parts.append("Esc Cancel")
        return "   ".join(parts)

    def _focus_current_step(self) -> None:
        selector = {
            0: "#new-deployment-name",
            1: "#new-deployment-runtime",
            2: "#new-deployment-model-ref",
            3: "#new-deployment-preset",
        }.get(self.step_index)
        if selector is None:
            return
        try:
            self.query_one(selector).focus()
        except Exception:
            return


def _model_reference(model: dict[str, Any]) -> str:
    return str(model.get("entry_id") or model.get("display_name") or "").strip()


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
        parts.append("auth: gated, requires HF_TOKEN")
    elif model.get("token_required"):
        parts.append("auth: requires HF_TOKEN")
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
        background: #091015;
    }}

    #new-deployment-review-panel {{
        width: 92;
        max-height: 38;
        border: solid {ACCENT};
        background: {SURFACE_ALT};
        padding: 1 2;
    }}

    #new-deployment-review-title {{
        color: {TEXT};
        text-style: bold;
    }}

    #new-deployment-review-steps {{
        margin-bottom: 1;
        color: {ACCENT};
    }}

    .new-deployment-review-label {{
        margin-top: 1;
        color: {TEXT};
        text-style: bold;
    }}

    #new-deployment-review-summary,
    #new-deployment-review-derived,
    #new-deployment-review-warnings,
    #new-deployment-review-preview {{
        color: {TEXT};
    }}

    #new-deployment-review-preview {{
        max-height: 12;
        background: #091015;
        padding: 0 1;
    }}

    #new-deployment-review-actions {{
        margin-top: 1;
        color: {GOOD};
    }}
    """

    BINDINGS = [
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
            yield Static(
                "Target -> Runtime -> Model -> Customize -> Review -> Save",
                id="new-deployment-review-steps",
            )
            yield Static("Summary", classes="new-deployment-review-label")
            yield Static(self._summary_text(), id="new-deployment-review-summary")
            yield Static("Auto-derived fields", classes="new-deployment-review-label")
            yield Static(self._derived_text(), id="new-deployment-review-derived")
            yield Static("Warnings", classes="new-deployment-review-label")
            yield Static(self._warnings_text(), id="new-deployment-review-warnings")
            yield Static("Resolved command", classes="new-deployment-review-label")
            yield Static(self.preview, id="new-deployment-review-preview")
            yield Static(
                "F Flags   Ctrl+S Save   S Save & Smoke   Esc Cancel",
                id="new-deployment-review-actions",
            )

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
