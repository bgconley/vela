from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from vela.config.schema import ModelConfig
from vela.engine.command_builder import ENGINE_VALUE_FIELDS
from vela.engine.profile import bundled_profile
from vela.tui.theme import ACCENT, SURFACE_ALT, TEXT


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

    #flag-manager-editor {{
        width: 1fr;
    }}

    #flag-manager-value {{
        margin-bottom: 1;
    }}

    #flag-manager-footer {{
        margin-top: 1;
        color: #8ba4ae;
    }}
    """

    BINDINGS = [
        ("up", "previous", "Previous"),
        ("down", "next", "Next"),
        Binding("d", "reset_default", "Reset", priority=True),
        ("ctrl+s", "save", "Save"),
        ("escape", "cancel", "Close"),
    ]

    def __init__(
        self,
        config: ModelConfig,
        *,
        preview: str,
        metadata: dict[str, Any] | None = None,
        preview_resolver: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        super().__init__(id="flag-manager")
        self.config = config
        self.preview = preview
        self.metadata = dict(metadata or {})
        self.warnings: list[str] = []
        self.preview_resolver = preview_resolver
        self.engine_updates: dict[str, object | None] = {}
        self.modeled = _modeled_flags(config, self.metadata)
        self.selected_index = 0
        self._preview_revision = 0
        self._updating_value_input = False
        self.passthrough, self.unknown = _partition_extra_args(
            config.extra_args,
            known_flags=_known_flags(self.metadata),
        )

    def compose(self) -> ComposeResult:
        with Vertical(id="flag-manager-panel"):
            with Horizontal():
                yield Static(self._render_list(), id="flag-manager-list")
                with Vertical(id="flag-manager-editor"):
                    yield Input(
                        value=self._selected_value(),
                        placeholder="Flag value",
                        id="flag-manager-value",
                    )
                    yield Static(self._render_detail(), id="flag-manager-detail")
            yield Static(
                "↑↓ Select   edit value   d Reset-to-default   Ctrl+S Save   Esc Close",
                id="flag-manager-footer",
            )

    def on_mount(self) -> None:
        self.call_later(lambda: self.set_focus(None))

    def action_previous(self) -> None:
        if self.modeled:
            self.selected_index = max(0, self.selected_index - 1)
            self._refresh()
            self._refresh_value_input()

    def action_next(self) -> None:
        if self.modeled:
            self.selected_index = min(len(self.modeled) - 1, self.selected_index + 1)
            self._refresh()
            self._refresh_value_input()

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
        self._refresh_value_input()
        self._queue_preview_refresh()

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

    def _refresh_value_input(self) -> None:
        try:
            value_input = self.query_one("#flag-manager-value", Input)
        except Exception:
            return
        self._updating_value_input = True
        try:
            value_input.value = self._selected_value()
        finally:
            self._updating_value_input = False

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "flag-manager-value":
            return
        if self._updating_value_input:
            return
        item = self._selected_item()
        if item is None:
            return
        value = event.value.strip()
        if (
            item["field"] not in self.engine_updates
            and value == str(item.get("value") or "")
        ):
            return
        item["value"] = value
        self.engine_updates[item["field"]] = value
        self._refresh()
        self._queue_preview_refresh()

    def _selected_item(self) -> dict[str, str] | None:
        if not self.modeled:
            return None
        return self.modeled[self.selected_index]

    def _selected_value(self) -> str:
        item = self._selected_item()
        return "" if item is None else str(item.get("value") or "")

    def _queue_preview_refresh(self) -> None:
        if self.preview_resolver is None:
            return
        self._preview_revision += 1
        revision = self._preview_revision
        self.run_worker(
            self._refresh_preview(revision),
            name="flag-preview",
            group="flag-preview",
            exclusive=True,
            exit_on_error=False,
        )

    async def _refresh_preview(self, revision: int) -> None:
        if self.preview_resolver is None:
            return
        result = await self.preview_resolver(
            {
                "name": self.config.name,
                "engine": dict(self.engine_updates),
                "extra_args": list(self.config.extra_args),
            }
        )
        if revision != self._preview_revision:
            return
        self.preview = str(result.get("preview") or "")
        warnings = result.get("warnings")
        self.warnings = [str(item) for item in warnings] if isinstance(warnings, list) else []
        metadata = result.get("metadata")
        if isinstance(metadata, dict):
            self.metadata = {**self.metadata, **metadata}
        self._refresh()

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
        warnings = self.warnings
        if warnings:
            lines.append("")
            lines.append(f"warnings {len(warnings)}")
            lines.extend(f"- {item}" for item in warnings)
        return "\n".join(lines)


def _modeled_flags(config: ModelConfig, metadata: dict[str, Any]) -> list[dict[str, str]]:
    flag_map = _flag_map(metadata)
    profile = (
        None
        if flag_map is not None
        else bundled_profile(config.vllm.version_profile or "current")
    )
    rows: list[dict[str, str]] = []
    for field_name in ENGINE_VALUE_FIELDS:
        value = getattr(config.engine, field_name)
        if value is None:
            continue
        flag = (
            flag_map.get(field_name)
            if flag_map is not None
            else profile.flag_for(field_name)
        )
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
        flag = (
            flag_map.get("enforce_eager")
            if flag_map is not None
            else profile.flag_for("enforce_eager")
        )
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


def _flag_map(metadata: dict[str, Any]) -> dict[str, str] | None:
    value = metadata.get("flag_map")
    if not isinstance(value, dict):
        return None
    flags = {
        str(field): str(flag)
        for field, flag in value.items()
        if isinstance(field, str)
        and isinstance(flag, str)
        and field
        and flag.startswith("--")
    }
    return flags or None


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
