from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Select, Static

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
    TEXT_SECONDARY,
)
from vela.tui.widgets import Field, KeyHintBar


class CreateBuildScreen(ModalScreen[dict[str, Any] | None]):
    # Which fields are relevant per build method (progressive disclosure).
    # All inputs stay mounted regardless — only visibility is toggled — so the
    # widget ids and the dismiss payload contract are preserved.
    _VISIBLE = {
        "nightly": {"label", "channel", "python", "env"},
        # pip installs from PyPI/spec — the CUDA channel field is ignored by
        # the agent for this method, so it is not shown (J32).
        "pip": {"label", "spec", "python", "env"},
        "commit": {"label", "commit", "channel", "python"},
        "git": {"label", "url", "ref", "python", "env"},
        "wheel": {"label", "path", "python"},
    }

    CSS = f"""
    CreateBuildScreen {{
        align: center middle;
        background: {BG_BASE};
    }}
    #create-build-panel {{
        width: 76;
        max-height: 90%;
        overflow-y: auto;
        border: round {BORDER_STRONG};
        background: {BG_PANEL};
        padding: 1 2;
    }}
    #create-build-titlebar {{ height: 1; margin-bottom: 1; }}
    #create-build-title {{ width: 1fr; color: {CYAN}; text-style: bold; }}
    #create-build-target {{ width: auto; color: {TEXT_FAINT}; }}
    #create-build-uv-note {{ color: {TEXT_SECONDARY}; height: auto; margin-bottom: 1; }}
    #create-build-preview {{
        border: round {BORDER_SUBTLE};
        background: {BG_INSET};
        padding: 1 2;
        margin-top: 1;
        height: auto;
    }}
    #create-build-preview-title {{ color: {TEXT_FAINT}; }}
    #create-build-preview-cmd {{ color: {GREEN}; height: auto; }}
    #create-build-error {{ color: {RED}; height: auto; margin-top: 1; }}
    #create-build-footer {{ margin-top: 1; }}
    """

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        # ctrl+g, not ctrl+u: Inputs consume ctrl+u (delete-line), and an
        # Input always has focus on this form.
        ("ctrl+g", "install_uv", "Install uv"),
    ]

    def __init__(
        self,
        *,
        initial: dict[str, Any] | None = None,
        error_message: str = "",
        uv_available: bool | None = None,
        target_label: str = "",
    ) -> None:
        super().__init__(id="create-build")
        self.initial = dict(initial or {})
        self.error_message = error_message
        self.uv_available = uv_available
        self.target_label = target_label
        self._preview_command = ""
        self._ready = False

    def compose(self) -> ComposeResult:
        method = self._initial_value("method") or "nightly"
        with Vertical(id="create-build-panel"):
            with Horizontal(id="create-build-titlebar"):
                yield Static("Create Build", id="create-build-title")
                yield Static(self._target_text(), id="create-build-target")
            yield Field(
                "Method",
                Select(
                    [
                        ("Nightly", "nightly"),
                        ("Pip", "pip"),
                        ("Commit", "commit"),
                        ("Git", "git"),
                        ("Wheel", "wheel"),
                    ],
                    allow_blank=False,
                    value=method,
                    id="create-build-method",
                ),
                id="cb-method",
            )
            yield Static(self._method_note_text(method), id="create-build-uv-note")
            yield Field(
                "Label",
                Input(
                    placeholder="nightly-cu130",
                    value=self._initial_value("label"),
                    id="create-build-label",
                ),
                helper="Short name shown in the build list & when creating deployments.",
                required=True,
                id="cb-label",
            )
            yield Field(
                "Package spec",
                Input(
                    placeholder="vllm==0.11.2",
                    value=self._initial_value("spec"),
                    id="create-build-spec",
                ),
                helper="pip requirement · e.g. vllm==0.11.2 or vllm.",
                id="cb-spec",
            )
            yield Field(
                "Channel / variant",
                Input(
                    placeholder="cu130",
                    value=self._initial_value("channel"),
                    id="create-build-channel",
                ),
                helper=(
                    "CUDA wheel channel · cu121 / cu124 / cu128 / cu130 · from "
                    "wheels.vllm.ai. Match your GPU (Blackwell sm_120 → cu128 / cu130)."
                ),
                id="cb-channel",
            )
            yield Field(
                "Python",
                Input(
                    placeholder="3.12",
                    value=self._initial_value("python"),
                    id="create-build-python",
                ),
                helper="Interpreter for the build's isolated venv · 3.12 recommended.",
                id="cb-python",
            )
            yield Field(
                "Commit",
                Input(
                    placeholder="abcdef123456",
                    value=self._initial_value("commit"),
                    id="create-build-commit",
                ),
                helper="Full git sha from github.com/vllm-project/vllm/commits.",
                id="cb-commit",
            )
            yield Field(
                "Git URL",
                Input(
                    placeholder="https://github.com/vllm-project/vllm.git",
                    value=self._initial_value("url"),
                    id="create-build-url",
                ),
                helper="Git repository to build vLLM from.",
                id="cb-url",
            )
            yield Field(
                "Ref",
                Input(
                    placeholder="main, v0.11.2, or a sha",
                    value=self._initial_value("ref"),
                    id="create-build-ref",
                ),
                helper="Branch, tag, or commit to build · blank = default branch tip.",
                optional=True,
                id="cb-ref",
            )
            yield Field(
                "Wheel / venv path",
                Input(
                    placeholder="/path/to/vllm.whl",
                    value=self._initial_value("path"),
                    id="create-build-path",
                ),
                helper=(
                    "Absolute path on the target to a .whl file — to register "
                    "a venv, use Adopt (a)."
                ),
                id="cb-path",
            )
            yield Field(
                "Environment",
                Input(
                    placeholder="KEY=value OTHER=value",
                    value=self._initial_env(),
                    id="create-build-env",
                ),
                helper="Extra env vars applied during the build step. Usually blank.",
                optional=True,
                id="cb-env",
            )
            with Vertical(id="create-build-preview"):
                yield Static("▸ WILL RUN", id="create-build-preview-title")
                yield Static("", id="create-build-preview-cmd")
            yield Static(self.error_message, id="create-build-error")
            hints = [("⏎", "Create"), ("Tab", "Next"), ("⇧Tab", "Prev")]
            if self.uv_available is False:
                hints.append(("Ctrl+G", "Install uv"))
            hints.append(("Esc", "Cancel"))
            yield KeyHintBar(hints, id="create-build-footer")

    def on_mount(self) -> None:
        self.query_one("#create-build-label", Input).focus()
        self._apply_disclosure()
        self._render_preview()
        self._ready = True

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.action_submit()

    def on_input_changed(self, event: Input.Changed) -> None:
        if self._ready:
            self._render_preview()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.control.id != "create-build-method":
            return
        method = str(event.value or "").strip()
        self.query_one("#create-build-uv-note", Static).update(self._method_note_text(method))
        error = self.query_one("#create-build-error", Static)
        if self._method_requires_uv(method) and self.uv_available is False:
            error.update(self._uv_block_message(method))
        elif self.uv_available is False:
            error.update("")
        if self._ready:
            self._apply_disclosure()
            self._render_preview()

    def action_submit(self) -> None:
        try:
            method = str(self.query_one("#create-build-method", Select).value or "").strip()
            if self._method_requires_uv(method) and self.uv_available is False:
                self.query_one("#create-build-error", Static).update(
                    self._uv_block_message(method)
                )
                return
            self.dismiss(self._collect_build_params())
        except ValueError as exc:
            self.query_one("#create-build-error", Static).update(str(exc))

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_install_uv(self) -> None:
        if self.uv_available is not False:
            return
        try:
            form_values = self._collect_build_params()
        except ValueError:
            form_values = {
                "method": str(
                    self.query_one("#create-build-method", Select).value or "nightly"
                )
            }
        self.dismiss({"action": "install_uv", "params": form_values})

    def _apply_disclosure(self) -> None:
        method = str(self.query_one("#create-build-method", Select).value or "").strip()
        visible = self._VISIBLE.get(method, {"label", "channel", "python", "env"})
        for key in ("label", "spec", "channel", "python", "commit", "url", "ref", "path", "env"):
            self.query_one(f"#cb-{key}", Field).display = key in visible

    def _render_preview(self) -> None:
        method = str(self.query_one("#create-build-method", Select).value or "").strip()
        values = {
            "channel": self._field_value("#create-build-channel"),
            "spec": self._field_value("#create-build-spec"),
            "commit": self._field_value("#create-build-commit"),
            "url": self._field_value("#create-build-url"),
            "path": self._field_value("#create-build-path"),
        }
        self._preview_command = self._build_will_run(method, values)
        self.query_one("#create-build-preview-cmd", Static).update(self._preview_command)

    @staticmethod
    def _build_will_run(method: str, values: dict[str, str]) -> str:
        channel = values.get("channel") or "cu130"
        if method == "nightly":
            return (
                "uv pip install --pre vllm "
                f"--extra-index-url https://wheels.vllm.ai/nightly/{channel}"
            )
        if method == "pip":
            return f"pip install {values.get('spec') or 'vllm'}"
        if method == "commit":
            commit = values.get("commit") or "<sha>"
            return f"uv pip install --pre vllm @ https://wheels.vllm.ai/{commit}/vllm-*.whl"
        if method == "git":
            url = values.get("url") or "https://github.com/vllm-project/vllm.git"
            return f"uv pip install 'vllm @ git+{url}'"
        if method == "wheel":
            return f"uv pip install {values.get('path') or '<wheel-path>'}"
        return ""

    def _field_value(self, selector: str) -> str:
        return self.query_one(selector, Input).value.strip()

    def _initial_value(self, key: str) -> str:
        value = self.initial.get(key)
        return str(value) if value is not None else ""

    def _initial_env(self) -> str:
        value = self.initial.get("env")
        if isinstance(value, list):
            return " ".join(str(item) for item in value)
        return str(value) if value is not None else ""

    def _collect_build_params(self) -> dict[str, Any]:
        method = self.query_one("#create-build-method", Select).value
        params: dict[str, Any] = {"method": str(method or "").strip()}
        # Only the fields relevant to the active method (the visible ones) may
        # contribute — stale values typed under a previous method must not leak.
        visible = self._VISIBLE.get(params["method"], {"label", "channel", "python", "env"})
        fields = {
            "label": self._field_value("#create-build-label"),
            "spec": self._field_value("#create-build-spec"),
            "channel": self._field_value("#create-build-channel"),
            "python": self._field_value("#create-build-python"),
            "commit": self._field_value("#create-build-commit"),
            "url": self._field_value("#create-build-url"),
            "ref": self._field_value("#create-build-ref"),
            "path": self._field_value("#create-build-path"),
        }
        params.update({key: value for key, value in fields.items() if value and key in visible})
        env = self._field_value("#create-build-env")
        if env and "env" in visible:
            params["env"] = [token for token in env.split() if token]
        if not params["method"]:
            raise ValueError("Choose a build method")
        return params

    def _target_text(self) -> str:
        return f"target: {self.target_label}" if self.target_label else ""

    def _method_note_text(self, method: str) -> str:
        desc = {
            "nightly": "Nightly — latest pre-release vLLM wheel, rebuilt nightly; "
            "newest models & fixes first, less battle-tested.",
            "pip": "Pip — install a released vLLM from PyPI or a custom index.",
            "commit": "Commit — install the pre-release wheel built at a specific vLLM commit.",
            "git": "Git — build vLLM from a git repo / ref.",
            "wheel": "Wheel — install a prebuilt wheel already on the target.",
        }.get(method, "")
        return f"{desc}  {self._uv_note_text()}"

    def _uv_note_text(self) -> str:
        target = f" on {self.target_label}" if self.target_label else " on the target"
        if self.uv_available is True:
            return f"uv available{target} — nightly & commit can run."
        if self.uv_available is False:
            return (
                f"uv not found{target} — nightly & commit require uv. "
                "Choose pip, wheel, or git."
            )
        return "Nightly & commit require uv on the target; pip / wheel / git fall back to pip."

    def _uv_block_message(self, method: str) -> str:
        target = f" on {self.target_label}" if self.target_label else " on the target"
        return f"{method} build creation requires uv{target}. Choose pip, wheel, or git."

    @staticmethod
    def _method_requires_uv(method: str) -> bool:
        return method in {"nightly", "commit"}
