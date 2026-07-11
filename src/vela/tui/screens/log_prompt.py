from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from vela.tui.theme import BG_BASE, BG_PANEL, BORDER_STRONG, TEXT_PRIMARY


class LogPromptScreen(ModalScreen[str | None]):
    CSS = f"""
    LogPromptScreen {{
        align: center middle;
        background: {BG_BASE};
    }}

    #log-prompt-panel {{
        width: 72;
        height: auto;
        max-height: 80%;
        border: round {BORDER_STRONG};
        background: {BG_PANEL};
        padding: 1 2;
    }}

    #log-prompt-title {{
        margin-bottom: 1;
        color: {TEXT_PRIMARY};
        text-style: bold;
    }}
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(
        self,
        *,
        title: str,
        placeholder: str,
        initial: str = "",
        id: str = "log-prompt",
    ) -> None:
        super().__init__(id=id)
        self.title = title
        self.placeholder = placeholder
        self.initial = initial

    def compose(self) -> ComposeResult:
        with Vertical(id="log-prompt-panel"):
            yield Static(self.title or "", id="log-prompt-title")
            yield Input(
                value=self.initial,
                placeholder=self.placeholder,
                id="log-prompt-input",
            )

    def on_mount(self) -> None:
        self.query_one("#log-prompt-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)
