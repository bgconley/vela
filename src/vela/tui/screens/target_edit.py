from __future__ import annotations

import shlex
from pathlib import Path

from pydantic import ValidationError
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from vela.config.targets import TargetConfig
from vela.tui.theme import BAD, BG_BASE, BG_PANEL, BORDER_STRONG, TEXT


class TargetEditScreen(ModalScreen[TargetConfig | None]):
    CSS = f"""
    TargetEditScreen {{
        align: center middle;
        background: {BG_BASE};
    }}

    #target-edit-panel {{
        width: 96;
        border: round {BORDER_STRONG};
        background: {BG_PANEL};
        padding: 1 2;
    }}

    #target-edit-title {{
        margin-bottom: 1;
        color: {TEXT};
        text-style: bold;
    }}

    #target-edit-error {{
        margin-top: 1;
        color: {BAD};
    }}
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, target: TargetConfig | None = None) -> None:
        super().__init__(id="target-edit")
        self.target = target

    def compose(self) -> ComposeResult:
        with Vertical(id="target-edit-panel"):
            yield Static(
                "Edit Target" if self.target is not None else "Add Target",
                id="target-edit-title",
            )
            yield Input(
                value=_target_to_input(self.target) if self.target is not None else "",
                placeholder=(
                    "name=blackbird transport=ssh host=user@host "
                    "workdir=/agent/repo venv=/agent/venv"
                ),
                id="target-edit-input",
            )
            yield Static("", id="target-edit-error")

    def on_mount(self) -> None:
        self.query_one("#target-edit-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        try:
            self.dismiss(_parse_target_config(event.value))
        except ValueError as exc:
            self.query_one("#target-edit-error", Static).update(str(exc))

    def action_cancel(self) -> None:
        self.dismiss(None)


def _parse_target_config(value: str) -> TargetConfig:
    try:
        tokens = [token.strip() for token in shlex.split(value) if token.strip()]
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    if not tokens:
        raise ValueError("Enter target fields")
    params: dict[str, object] = {}
    for token in tokens:
        if "=" not in token:
            raise ValueError(f"Use key=value for '{token}'")
        key, raw_value = token.split("=", 1)
        key = key.strip().replace("-", "_")
        raw_value = raw_value.strip()
        if not key or not raw_value:
            raise ValueError("Target fields must use key=value")
        params[key] = raw_value
    try:
        return TargetConfig.model_validate(params)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors(include_url=False)
        )
        raise ValueError(details) from exc
    except ValueError as exc:
        raise ValueError(str(exc)) from exc


def _target_to_input(target: TargetConfig) -> str:
    parts = [
        f"name={target.name}",
        f"transport={target.transport.value}",
    ]
    if target.host:
        parts.append(f"host={target.host}")
    if target.ssh_key:
        parts.append(f"ssh_key={_path_value(target.ssh_key)}")
    if target.agent_command:
        command = " ".join(shlex.quote(part) for part in target.agent_command)
        parts.append(f"agent_command={shlex.quote(command)}")
    if target.workdir:
        parts.append(f"workdir={_path_value(target.workdir)}")
    if target.venv:
        parts.append(f"venv={_path_value(target.venv)}")
    if target.ssh_opts_env:
        parts.append(f"ssh_opts_env={target.ssh_opts_env}")
    if target.socket_path:
        parts.append(f"socket_path={_path_value(target.socket_path)}")
    return " ".join(parts)


def _path_value(path: Path) -> str:
    return str(path)
