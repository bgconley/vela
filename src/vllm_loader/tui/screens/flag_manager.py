from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

from vllm_loader.config.schema import ModelConfig
from vllm_loader.engine.command_builder import ENGINE_VALUE_FIELDS
from vllm_loader.engine.profile import bundled_profile
from vllm_loader.tui.theme import ACCENT, SURFACE_ALT, TEXT


class FlagManagerScreen(ModalScreen):
    CSS = f"""
    FlagManagerScreen {{
        align: center middle;
        background: #091015;
    }}

    #flag-manager-panel {{
        width: 104;
        max-height: 34;
        border: solid {ACCENT};
        background: {SURFACE_ALT};
        padding: 1 2;
    }}

    #flag-manager-list {{
        width: 44;
        height: auto;
        max-height: 22;
        color: {TEXT};
    }}

    #flag-manager-detail {{
        width: 1fr;
        height: auto;
        max-height: 22;
        color: {TEXT};
    }}

    #flag-manager-footer {{
        margin-top: 1;
        color: #8ba4ae;
    }}
    """

    BINDINGS = [
        ("up", "previous", "Previous"),
        ("down", "next", "Next"),
        ("d", "reset_default", "Reset"),
        ("ctrl+s", "save", "Save"),
        ("escape", "cancel", "Close"),
    ]

    def __init__(
        self,
        config: ModelConfig,
        *,
        preview: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(id="flag-manager")
        self.config = config
        self.preview = preview
        self.metadata = dict(metadata or {})
        self.engine_updates: dict[str, object | None] = {}
        self.modeled = _modeled_flags(config)
        self.selected_index = 0
        self.passthrough, self.unknown = _partition_extra_args(
            config.extra_args,
            known_flags=_known_flags(self.metadata),
        )

    def compose(self) -> ComposeResult:
        with Vertical(id="flag-manager-panel"):
            with Horizontal():
                yield Static(self._render_list(), id="flag-manager-list")
                yield Static(self._render_detail(), id="flag-manager-detail")
            yield Static(
                "↑↓ Select   d Reset-to-default   Ctrl+S Save   Esc Close",
                id="flag-manager-footer",
            )

    def action_previous(self) -> None:
        if self.modeled:
            self.selected_index = max(0, self.selected_index - 1)
            self._refresh()

    def action_next(self) -> None:
        if self.modeled:
            self.selected_index = min(len(self.modeled) - 1, self.selected_index + 1)
            self._refresh()

    def action_reset_default(self) -> None:
        if not self.modeled:
            return
        item = self.modeled.pop(self.selected_index)
        self.engine_updates[item["field"]] = None
        if self.modeled:
            self.selected_index = min(self.selected_index, len(self.modeled) - 1)
        else:
            self.selected_index = 0
        self._refresh()

    def action_save(self) -> None:
        self.dismiss(
            {
                "action": "save_flags",
                "name": self.config.name,
                "engine": dict(self.engine_updates),
                "extra_args": list(self.config.extra_args),
            }
        )

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _refresh(self) -> None:
        self.query_one("#flag-manager-list", Static).update(self._render_list())
        self.query_one("#flag-manager-detail", Static).update(self._render_detail())

    def _render_list(self) -> str:
        lines = [
            "Flag Manager",
            f"build: {_build_label(self.config, self.metadata)}",
            f"config: {self.config.name}",
            "",
            (
                f"modeled {len(self.modeled)} · passthrough {len(self.passthrough)} · "
                f"unknown {len(self.unknown)}"
            ),
            "",
            "MODELED",
        ]
        if self.modeled:
            lines.extend(
                f"{'>' if index == self.selected_index else ' '} {_flag_value_text(item)}"
                for index, item in enumerate(self.modeled)
            )
        else:
            lines.append("  none")
        lines.append("PASSTHROUGH")
        if self.passthrough:
            lines.extend(f"  {item}" for item in self.passthrough)
        else:
            lines.append("  none")
        lines.append("UNKNOWN-TO-BUILD")
        if self.unknown:
            lines.extend(f"  {item}" for item in self.unknown)
        else:
            lines.append("  none")
        return "\n".join(lines)

    def _render_detail(self) -> str:
        lines = ["Editor + live preview", "", "Resolved command"]
        if self.preview:
            lines.extend(self.preview.splitlines())
        else:
            lines.append("Preview unavailable")
        warnings = self.metadata.get("warnings")
        if isinstance(warnings, list) and warnings:
            lines.append("")
            lines.append(f"warnings {len(warnings)}")
            lines.extend(f"- {item}" for item in warnings)
        return "\n".join(lines)


def _modeled_flags(config: ModelConfig) -> list[dict[str, str]]:
    profile = bundled_profile(config.vllm.version_profile or "current")
    rows: list[dict[str, str]] = []
    for field_name in ENGINE_VALUE_FIELDS:
        value = getattr(config.engine, field_name)
        if value is None:
            continue
        flag = profile.flag_for(field_name)
        if flag is None:
            continue
        rows.append(
            {
                "field": field_name,
                "flag": flag,
                "label": flag.removeprefix("--"),
                "target": f"engine.{field_name}",
                "value": str(value),
            }
        )
    if config.engine.enforce_eager is True:
        flag = profile.flag_for("enforce_eager")
        if flag is not None:
            rows.append(
                {
                    "flag": flag,
                    "field": "enforce_eager",
                    "label": flag.removeprefix("--"),
                    "target": "engine.enforce_eager",
                    "value": "true",
                }
            )
    return rows


def _partition_extra_args(
    extra_args: list[str], *, known_flags: set[str] | None
) -> tuple[list[str], list[str]]:
    passthrough: list[str] = []
    unknown: list[str] = []
    index = 0
    while index < len(extra_args):
        token = str(extra_args[index])
        display = token
        flag = _flag_name(token)
        if flag is not None and "=" not in token and index + 1 < len(extra_args):
            next_token = str(extra_args[index + 1])
            if not next_token.startswith("-"):
                display = f"{token} {next_token}"
                index += 1
        target = unknown if known_flags is not None and flag not in known_flags else passthrough
        target.append(display)
        index += 1
    return passthrough, unknown


def _known_flags(metadata: dict[str, Any]) -> set[str] | None:
    value = metadata.get("known_flags")
    if not isinstance(value, list):
        return None
    flags = {str(item) for item in value if isinstance(item, str) and item.startswith("--")}
    return flags if flags else None


def _flag_name(token: str) -> str | None:
    if not token.startswith("--"):
        return None
    return token.split("=", 1)[0]


def _flag_value_text(item: dict[str, str]) -> str:
    return f"{item['label']} = {item['value']} -> {item['target']}"


def _build_label(config: ModelConfig, metadata: dict[str, Any]) -> str:
    for value in (
        metadata.get("build_label"),
        config.command.build,
        metadata.get("vllm_version"),
        metadata.get("vllm_version_profile"),
    ):
        if value:
            return str(value)
    return "PATH"
