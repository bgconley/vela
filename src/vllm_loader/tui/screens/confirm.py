from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

from vllm_loader.tui.theme import ACCENT, BAD, GOOD, MUTED, SURFACE_ALT, TEXT, WARN


class ConfirmScreen(ModalScreen):
    CSS = f"""
    ConfirmScreen {{
        align: center middle;
        background: #091015;
    }}

    #confirm-panel {{
        width: 68;
        border: solid {WARN};
        background: {SURFACE_ALT};
        padding: 1 2;
    }}

    #confirm-message {{
        color: {TEXT};
    }}
    """

    BINDINGS = [
        ("enter", "confirm", "Confirm"),
        ("s", "stop", "Stop"),
        ("K", "kill", "Kill"),
        ("escape,c", "cancel", "Cancel"),
    ]

    def __init__(
        self,
        message: str,
        *,
        title: str = "Confirm stop",
        confirm_label: str = "Stop",
        confirm_action: str = "confirm_stop_running",
    ) -> None:
        super().__init__(id="confirm")
        self.message = message
        self.title = title
        self.confirm_label = confirm_label
        self.confirm_action = confirm_action

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-panel"):
            yield Static(self._message_text(), id="confirm-message")

    def action_confirm(self) -> None:
        getattr(self.app, self.confirm_action)()

    def action_stop(self) -> None:
        if self.confirm_label.lower() == "stop":
            self.action_confirm()

    def action_kill(self) -> None:
        if self.confirm_label.lower() == "kill":
            self.action_confirm()

    def action_cancel(self) -> None:
        self.app.pop_screen()

    def _message_text(self) -> Text:
        if self.confirm_label.lower() == "kill":
            title_style = f"bold {BAD}"
        else:
            title_style = f"bold {ACCENT}"
        text = Text(f"{self.title}\n\n", style=title_style)
        text.append(self.message, style=TEXT)
        confirm_key = "Enter/K " if self.confirm_label.lower() == "kill" else "Enter/s "
        text.append(f"\n\n{confirm_key}", style=MUTED)
        text.append(self.confirm_label, style=f"bold {BAD}")
        text.append("    Esc/c ", style=MUTED)
        text.append("Cancel", style=f"bold {GOOD}")
        return text
