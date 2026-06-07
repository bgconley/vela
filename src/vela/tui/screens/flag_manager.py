from __future__ import annotations

import shlex
from collections.abc import Awaitable, Callable
from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Checkbox, Input, Select, Static

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
        max-height: 21;
        color: {TEXT};
    }}

    #flag-manager-detail {{
        width: 1fr;
        height: auto;
        max-height: 21;
        color: {TEXT};
    }}

    #flag-manager-controls {{
        height: auto;
        margin-bottom: 1;
    }}

    #flag-manager-preset {{
        width: 38;
        margin-right: 2;
    }}

    #flag-manager-changed-only {{
        width: 22;
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
        Binding("p", "reset_preset", "Preset", priority=True),
        Binding("x", "toggle_changed_only", "Changed", priority=True),
        ("ctrl+s", "save", "Save"),
        ("escape", "cancel", "Close"),
    ]

    def __init__(
        self,
        config: ModelConfig,
        *,
        preview: str,
        metadata: dict[str, Any] | None = None,
        presets: list[dict[str, Any]] | None = None,
        selected_preset: str | None = None,
        preview_resolver: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        super().__init__(id="flag-manager")
        self.config = config
        self.preview = preview
        self.metadata = dict(metadata or {})
        self.warnings: list[str] = []
        self.preview_resolver = preview_resolver
        self.engine_updates: dict[str, object | None] = {}
        self.presets = _normalize_presets(presets)
        self.selected_preset = _selected_preset_name(self.presets, selected_preset)
        self.show_changed_only = False
        self._base_engine_values = _config_engine_values(config)
        self.modeled = self._build_modeled_rows()
        self.selected_index = 0
        self._preview_revision = 0
        self._updating_value_input = False
        self._updating_extra_args_input = False
        self.extra_args = list(config.extra_args)
        self.extra_args_error: str | None = None
        self.passthrough, self.unknown = _partition_extra_args(
            self.extra_args,
            known_flags=_known_flags(self.metadata),
        )

    def compose(self) -> ComposeResult:
        with Vertical(id="flag-manager-panel"):
            with Horizontal(id="flag-manager-controls"):
                yield Select(
                    self._preset_options(),
                    value=self.selected_preset or "__none__",
                    allow_blank=False,
                    id="flag-manager-preset",
                )
                yield Checkbox(
                    "Changed only",
                    value=self.show_changed_only,
                    id="flag-manager-changed-only",
                )
            with Horizontal():
                yield Static(self._render_list(), id="flag-manager-list")
                with Vertical(id="flag-manager-editor"):
                    yield Input(
                        value=self._selected_value(),
                        placeholder="Flag value",
                        id="flag-manager-value",
                    )
                    yield Input(
                        value=_quote_extra_args(self.extra_args),
                        placeholder="Raw passthrough args",
                        id="flag-manager-extra-args",
                    )
                    yield Static(self._render_detail(), id="flag-manager-detail")
            yield Static(
                "↑↓ Select   edit value   edit raw args   d Default   "
                "p Preset   x Changed-only   Ctrl+S Save   Esc Close",
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
        item = self._selected_item()
        if item is None:
            return
        self.engine_updates[item["field"]] = None
        self._rebuild_modeled_rows(selected_field=item["field"])
        self._refresh()
        self._refresh_value_input()
        self._queue_preview_refresh()

    def action_reset_preset(self) -> None:
        item = self._selected_item()
        if item is None:
            return
        preset_value = item.get("preset_value")
        if preset_value is None:
            return
        self.engine_updates[item["field"]] = preset_value
        self._rebuild_modeled_rows(selected_field=item["field"])
        self._refresh()
        self._refresh_value_input()
        self._queue_preview_refresh()

    def action_toggle_changed_only(self) -> None:
        self.show_changed_only = not self.show_changed_only
        try:
            self.query_one("#flag-manager-changed-only", Checkbox).value = (
                self.show_changed_only
            )
        except Exception:
            pass
        self._rebuild_modeled_rows()
        self._refresh()
        self._refresh_value_input()

    def action_save(self) -> None:
        self.dismiss(
            {
                "action": "save_flags",
                "name": self.config.name,
                "engine": dict(self.engine_updates),
                "extra_args": list(self.extra_args),
            }
        )

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "flag-manager-preset":
            return
        event.stop()
        value = str(event.value or "")
        next_preset = None if value == "__none__" else value
        if next_preset == self.selected_preset:
            return
        self.selected_preset = next_preset
        preset_engine = self._active_preset_engine()
        for field, preset_value in preset_engine.items():
            if field in _supported_engine_fields():
                self.engine_updates[field] = preset_value
        self._rebuild_modeled_rows()
        self._refresh()
        self._refresh_value_input()
        self._queue_preview_refresh()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id != "flag-manager-changed-only":
            return
        event.stop()
        self.show_changed_only = bool(event.value)
        self._rebuild_modeled_rows()
        self._refresh()
        self._refresh_value_input()

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
            if event.input.id == "flag-manager-extra-args":
                self._handle_extra_args_changed(event.value)
            return
        if self._updating_value_input:
            return
        item = self._selected_item()
        if item is None:
            return
        value = event.value.strip()
        if (
            item["field"] in self.engine_updates
            and self.engine_updates[item["field"]] is None
        ):
            if value == "":
                return
        if (
            item["field"] not in self.engine_updates
            and value == str(item.get("value") or "")
        ):
            return
        item["value"] = value
        self.engine_updates[item["field"]] = value
        self._refresh()
        self._queue_preview_refresh()

    def _handle_extra_args_changed(self, value: str) -> None:
        if self._updating_extra_args_input:
            return
        try:
            parsed = shlex.split(value)
        except ValueError as exc:
            self.extra_args_error = f"Raw passthrough args: {exc}"
            self._refresh()
            return
        self.extra_args_error = None
        self.extra_args = parsed
        self.passthrough, self.unknown = _partition_extra_args(
            self.extra_args,
            known_flags=_known_flags(self.metadata),
        )
        self._refresh()
        self._queue_preview_refresh()

    def _active_preset_engine(self) -> dict[str, object]:
        preset = _preset_by_name(self.presets, self.selected_preset)
        engine = preset.get("engine") if preset is not None else None
        return dict(engine) if isinstance(engine, dict) else {}

    def _build_modeled_rows(self) -> list[dict[str, object]]:
        flag_map = _flag_map(self.metadata)
        profile = (
            None
            if flag_map is not None
            else bundled_profile(self.config.vllm.version_profile or "current")
        )
        preset_engine = self._active_preset_engine()
        fields = list(ENGINE_VALUE_FIELDS)
        if any(
            "enforce_eager" in values
            for values in (self._base_engine_values, preset_engine, self.engine_updates)
        ):
            fields.append("enforce_eager")
        rows: list[dict[str, object]] = []
        for field in fields:
            if (
                field not in self._base_engine_values
                and field not in self.engine_updates
                and field not in preset_engine
            ):
                continue
            flag = (
                flag_map.get(field)
                if flag_map is not None
                else profile.flag_for(field) if profile is not None else None
            )
            if flag is None:
                continue
            value = self._effective_engine_value(field)
            preset_value = preset_engine.get(field)
            changed = not _values_equal(value, preset_value)
            if self.show_changed_only and not changed:
                continue
            rows.append(
                {
                    "field": field,
                    "flag": flag,
                    "label": flag.removeprefix("--"),
                    "target": f"engine.{field}",
                    "value": "" if value is None else str(value),
                    "preset_value": preset_value,
                    "changed": changed,
                }
            )
        return rows

    def _rebuild_modeled_rows(self, *, selected_field: str | None = None) -> None:
        previous_field = selected_field
        if previous_field is None:
            current = self._selected_item()
            previous_field = str(current.get("field")) if current is not None else None
        self.modeled = self._build_modeled_rows()
        if not self.modeled:
            self.selected_index = 0
            return
        if previous_field is not None:
            for index, item in enumerate(self.modeled):
                if item.get("field") == previous_field:
                    self.selected_index = index
                    return
        self.selected_index = min(self.selected_index, len(self.modeled) - 1)

    def _effective_engine_value(self, field: str) -> object | None:
        if field in self.engine_updates:
            return self.engine_updates[field]
        return self._base_engine_values.get(field)

    def _preset_options(self) -> list[tuple[str, str]]:
        if not self.presets:
            return [("No presets", "__none__")]
        return [
            (str(preset.get("name") or "preset"), str(preset.get("name") or "__none__"))
            for preset in self.presets
        ]

    def _selected_item(self) -> dict[str, object] | None:
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
                "extra_args": list(self.extra_args),
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
            self.passthrough, self.unknown = _partition_extra_args(
                self.extra_args,
                known_flags=_known_flags(self.metadata),
            )
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
        if self.extra_args_error:
            lines.append(self.extra_args_error)
            lines.append("")
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


def _normalize_presets(presets: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for preset in presets or []:
        if not isinstance(preset, dict):
            continue
        name = str(preset.get("name") or "").strip()
        if not name:
            continue
        engine = preset.get("engine")
        rows.append(
            {
                "name": name,
                "description": str(preset.get("description") or ""),
                "engine": dict(engine) if isinstance(engine, dict) else {},
            }
        )
    return rows


def _selected_preset_name(
    presets: list[dict[str, Any]], selected_preset: str | None
) -> str | None:
    names = {str(preset.get("name") or "") for preset in presets}
    if selected_preset in names:
        return selected_preset
    if "balanced" in names:
        return "balanced"
    return next(iter(names), None)


def _preset_by_name(
    presets: list[dict[str, Any]], name: str | None
) -> dict[str, Any] | None:
    if name is None:
        return None
    for preset in presets:
        if str(preset.get("name") or "") == name:
            return preset
    return None


def _config_engine_values(config: ModelConfig) -> dict[str, object]:
    values: dict[str, object] = {}
    for field in ENGINE_VALUE_FIELDS:
        value = getattr(config.engine, field)
        if value is not None:
            values[field] = value
    if config.engine.enforce_eager is True:
        values["enforce_eager"] = True
    return values


def _supported_engine_fields() -> set[str]:
    return {*ENGINE_VALUE_FIELDS, "enforce_eager"}


def _values_equal(left: object | None, right: object | None) -> bool:
    if left is None and right in (None, ""):
        return True
    if right is None and left in (None, ""):
        return True
    return str(left) == str(right)


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


def _flag_value_text(item: dict[str, object]) -> str:
    marker = "*" if item.get("changed") else " "
    value = str(item.get("value") or "unset")
    return f"{marker} {item['label']} = {value} -> {item['target']}"


def _quote_extra_args(extra_args: list[str]) -> str:
    return shlex.join(extra_args)


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
