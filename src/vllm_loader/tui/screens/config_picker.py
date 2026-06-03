from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from vllm_loader.config.loader import ConfigRegistry, ValidConfig
from vllm_loader.engine.command_builder import build_command
from vllm_loader.engine.profile import VllmProfileError, select_profile_for_config
from vllm_loader.tui.theme import ACCENT, SURFACE_ALT


class ConfigPickerScreen(ModalScreen):
    CSS = f"""
    ConfigPickerScreen {{
        align: center middle;
        background: #091015;
    }}

    #config-picker-panel {{
        width: 72;
        max-height: 32;
        border: solid {ACCENT};
        background: {SURFACE_ALT};
        padding: 1 2;
    }}

    #config-picker-filter {{
        margin-bottom: 1;
    }}

    #config-picker-list {{
        height: auto;
        max-height: 26;
        overflow-y: auto;
    }}
    """

    BINDINGS = [
        ("up", "previous", "Previous"),
        ("down", "next", "Next"),
        ("enter", "accept", "Select"),
        ("escape", "cancel", "Cancel"),
    ]

    def __init__(self, registry: ConfigRegistry) -> None:
        super().__init__(id="config-picker")
        self.registry = registry
        self.selected_index = 0
        self.summary = ""
        self.filter_text = ""

    def compose(self) -> ComposeResult:
        with Vertical(id="config-picker-panel"):
            yield Input(placeholder="Filter configs", id="config-picker-filter")
            yield Static("", id="config-picker-list")

    def on_mount(self) -> None:
        self.query_one("#config-picker-filter", Input).focus()
        self._refresh()

    def on_input_changed(self, event: Input.Changed) -> None:
        self.filter_text = event.value
        self.selected_index = 0
        self._refresh()

    def on_input_submitted(self, _event: Input.Submitted) -> None:
        self.action_accept()

    def action_previous(self) -> None:
        if self._filtered_valid():
            self.selected_index = max(0, self.selected_index - 1)
            self._refresh()

    def action_next(self) -> None:
        configs = self._filtered_valid()
        if configs:
            self.selected_index = min(len(configs) - 1, self.selected_index + 1)
            self._refresh()

    def action_accept(self) -> None:
        configs = self._filtered_valid()
        if not configs:
            self.app.pop_screen()
            return
        config = configs[self.selected_index].config
        self.app.select_config(config.name)
        self.app.pop_screen()

    def action_cancel(self) -> None:
        self.app.pop_screen()

    def _refresh(self) -> None:
        lines = ["Config Picker", ""]
        configs = self._filtered_valid()
        if self.filter_text:
            lines.append(f"Filter: {self.filter_text}")
        for index, item in enumerate(configs):
            marker = ">" if index == self.selected_index else " "
            lines.append(f"{marker} {item.config.name}  {item.config.model}")
        if self.registry.valid and not configs:
            lines.append("No matching configs")
        if self.registry.invalid:
            lines.append("")
            lines.append("Invalid configs")
            for item in self.registry.invalid:
                lines.append(f"⚠ {item.path.name}: {item.errors[0]}")
        preview = self._selected_preview()
        if preview:
            lines.extend(["", "Resolved command", preview])
        self.summary = "\n".join(lines)
        self.query_one("#config-picker-list", Static).update(self.summary)

    def _selected_preview(self) -> str:
        configs = self._filtered_valid()
        if not configs:
            return ""
        cfg = configs[self.selected_index].config
        try:
            profile = select_profile_for_config(cfg)
            return build_command(cfg, profile).preview
        except VllmProfileError as exc:
            return f"Preview unavailable: {exc}"

    def _filtered_valid(self) -> list[ValidConfig]:
        if not self.filter_text.strip():
            return list(self.registry.valid)
        needle = self.filter_text.casefold()
        return [
            item
            for item in self.registry.valid
            if _fuzzy_match(needle, f"{item.config.name} {item.config.model}".casefold())
        ]


def _fuzzy_match(needle: str, haystack: str) -> bool:
    if not needle:
        return True
    position = 0
    for character in needle:
        position = haystack.find(character, position)
        if position == -1:
            return False
        position += 1
    return True
