from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from vela.config.loader import ConfigRegistry, ValidConfig
from vela.tui.theme import (
    ACCENT,
    AMBER,
    BG_BASE,
    BG_PANEL,
    BORDER_STRONG,
    MODAL_LIST_CSS,
    MODAL_PANEL_CSS,
    MUTED,
    WARN,
)


class ConfigPickerScreen(ModalScreen):
    CSS = f"""
    ConfigPickerScreen {{
        align: center middle;
        background: {BG_BASE};
    }}

    ConfigPickerScreen #config-picker-panel {{
        {MODAL_PANEL_CSS}
        border: round {BORDER_STRONG};
        background: {BG_PANEL};
        padding: 1 2;
    }}

    ConfigPickerScreen #config-picker-filter {{
        margin-bottom: 1;
    }}

    ConfigPickerScreen #config-picker-scroll {{
        {MODAL_LIST_CSS}
        max-height: 16;
    }}

    ConfigPickerScreen #config-picker-list {{
        width: 1fr;
        height: auto;
    }}
    """

    BINDINGS = [
        ("up", "previous", "Previous"),
        ("down", "next", "Next"),
        ("enter", "accept", "Select"),
        ("ctrl+t", "push", "Push to target"),
        ("escape", "cancel", "Cancel"),
    ]

    def __init__(
        self,
        registry: ConfigRegistry,
        preview_cache: dict[str, str] | None = None,
        *,
        connection_state: str = "connected",
        current_config_name: str | None = None,
    ) -> None:
        super().__init__(id="config-picker")
        self.registry = registry
        self.preview_cache = {} if preview_cache is None else preview_cache
        self.connection_state = connection_state
        self.current_config_name = current_config_name
        self.selected_index = self._preferred_index(list(self.registry.valid))
        self.summary = ""
        self.filter_text = ""
        self._selected_line: int | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="config-picker-panel"):
            yield Input(placeholder="Filter configs", id="config-picker-filter")
            with VerticalScroll(id="config-picker-scroll"):
                yield Static("", id="config-picker-list")

    def on_mount(self) -> None:
        # Keep the scroll region out of the Tab order so the filter Input holds
        # focus and ↑/↓/Enter reach the screen bindings.
        try:
            self.query_one("#config-picker-scroll").can_focus = False
        except Exception:
            pass
        self.query_one("#config-picker-filter", Input).focus()
        self._refresh()

    def on_input_changed(self, event: Input.Changed) -> None:
        self.filter_text = event.value
        self.selected_index = self._preferred_index(self._filtered_valid())
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
            # bug-237: never silently dismiss on Enter when there is nothing to
            # select — the list already carries an "Esc to close" hint, so keep
            # the picker open instead of vanishing.
            return
        config = configs[self.selected_index].config
        self.app.select_config(config.name)
        self.app.pop_screen()

    def action_cancel(self) -> None:
        self.app.pop_screen()

    def action_push(self) -> None:
        configs = self._filtered_valid()
        if not configs:
            return
        config = configs[self.selected_index].config
        self.app.select_config(config.name)
        self.app.pop_screen()
        self.app.push_config_affordance()

    def _refresh(self) -> None:
        rows: list[tuple[str, str | None]] = [
            ("Config Picker", f"bold {ACCENT}"),
            ("", None),
        ]
        configs = self._filtered_valid()
        if self.filter_text:
            rows.append((f"Filter: {self.filter_text}", MUTED))
        self._selected_line = None
        header_offset = len(rows)
        for index, item in enumerate(configs):
            selected = index == self.selected_index
            current = item.config.name == self.current_config_name
            marker = ">" if selected else " "
            current_label = "  [current]" if current else ""
            line = f"{marker} {item.config.name}  {item.config.model}{current_label}"
            rows.append((line, f"bold {ACCENT}" if selected else None))
            if selected:
                self._selected_line = header_offset + index
        if self.registry.valid and not configs:
            rows.append(("no match — Esc to close", AMBER))
        if not self.registry.valid and not self.registry.invalid:
            if self.connection_state != "connected":
                # bug-252 carry-forward: offline with nothing cached is not the
                # first-run empty state — say the target is unreachable, not that
                # the user has no configs.
                rows.append(("target unreachable — configs unknown", AMBER))
            else:
                # The focused filter Input eats 'n', so the empty copy has to
                # tell the user to close the picker before pressing n.
                rows.append(
                    ("No configs yet — Esc to close, then press n on the dashboard", MUTED)
                )
        if self.registry.invalid:
            rows.append(("", None))
            rows.append(("Invalid configs", f"bold {WARN}"))
            for item in self.registry.invalid:
                first_error, *remaining_errors = item.errors or ["invalid config"]
                rows.append((f"⚠ {item.path.name}: {first_error}", WARN))
                rows.extend((f"  {error}", MUTED) for error in remaining_errors)
        preview = self._selected_preview()
        if preview:
            rows.append(("", None))
            rows.append(("Resolved command", MUTED))
            rows.append((preview, None))
        self.summary = "\n".join(text for text, _ in rows)
        self._render_rows(rows)
        # Layout must settle before the scroll offset is meaningful; scroll the
        # marker into view after the next refresh (bug-237).
        self.call_after_refresh(self._scroll_selection_into_view)

    def _render_rows(self, rows: list[tuple[str, str | None]]) -> None:
        text = Text()
        last = len(rows) - 1
        for index, (line, style) in enumerate(rows):
            text.append(line, style=style)
            if index != last:
                text.append("\n")
        self.query_one("#config-picker-list", Static).update(text)

    def _scroll_selection_into_view(self) -> None:
        line = self._selected_line
        if line is None:
            return
        try:
            scroll = self.query_one("#config-picker-scroll", VerticalScroll)
        except Exception:
            return
        view_height = scroll.size.height
        if view_height <= 0:
            return
        top = scroll.scroll_offset.y
        if line < top:
            scroll.scroll_to(y=line, animate=False)
        elif line >= top + view_height:
            scroll.scroll_to(y=line - view_height + 1, animate=False)

    def _selected_preview(self) -> str:
        configs = self._filtered_valid()
        if not configs:
            return ""
        return self.preview_cache.get(configs[self.selected_index].config.name, "")

    def _filtered_valid(self) -> list[ValidConfig]:
        if not self.filter_text.strip():
            return list(self.registry.valid)
        needle = self.filter_text.casefold()
        return [
            item
            for item in self.registry.valid
            if _fuzzy_match(needle, f"{item.config.name} {item.config.model}".casefold())
        ]

    def _preferred_index(self, configs: list[ValidConfig]) -> int:
        if self.current_config_name is not None:
            for index, item in enumerate(configs):
                if item.config.name == self.current_config_name:
                    return index
        return 0


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
