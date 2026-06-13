from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

from vela.tui.theme import (
    ACCENT,
    BG_BASE,
    BG_PANEL,
    BORDER_STRONG,
    GOOD,
    MUTED,
    PURPLE,
    PURPLE_SURFACE,
    TEXT,
    WARN,
)


class HelpScreen(ModalScreen):
    CSS = f"""
    HelpScreen {{
        align: center middle;
        background: {BG_BASE};
    }}

    #help-panel {{
        width: 82;
        height: auto;
        max-height: 90%;
        border: round {BORDER_STRONG};
        background: {BG_PANEL};
        padding: 1 2;
    }}

    #help-text {{
        color: {TEXT};
    }}

    #help-actions {{
        margin-top: 1;
        color: {MUTED};
    }}
    """

    BINDINGS = [
        ("escape", "close", "Close"),
        ("?", "close", "Close"),
        ("f1", "close", "Close"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="help-panel"):
            yield Static(self._help_text(), id="help-text")
            yield Static(self._action_pills(), id="help-actions")

    def action_close(self) -> None:
        # A bare ``pop_screen`` action string does not resolve against a
        # ModalScreen in Textual, so the modal was a live trap (bug-221).
        # Dismiss explicitly, matching every other modal in the app.
        self.dismiss()

    @staticmethod
    def _help_text() -> Text:
        text = Text("HelpScreen - bindings + palette hint\n\n", style=f"bold {ACCENT}")
        text.append("Load / control:  ", style=MUTED)
        text.append("l Load   ", style=GOOD)
        text.append("s Stop   ", style=WARN)
        text.append("K Kill   ", style=f"bold {WARN}")
        text.append("r Restart   ", style=GOOD)
        text.append("q Quit\n", style=WARN)
        text.append("Logs:            ", style=MUTED)
        text.append("/ Search   f Filter   p Pause autoscroll   w Wrap\n", style=TEXT)
        text.append("Navigation:      ", style=MUTED)
        text.append("g Top   G Bottom   Tab focus\n", style=TEXT)
        text.append("Composition:     ", style=MUTED)
        text.append("n New deployment   b Builds   m Models   F Flags\n", style=TEXT)
        text.append("Targets:         ", style=MUTED)
        text.append("t Target manager   R Reconnect\n", style=TEXT)
        text.append("Discovery:       ", style=MUTED)
        text.append("Ctrl+P palette has every action\n", style=PURPLE)
        text.append("Screens:         ", style=MUTED)
        text.append("c Config picker   ? or F1 Help\n\n", style=TEXT)
        text.append("Markers:         ", style=MUTED)
        text.append(
            "📌 pinned   ● ready/cached   ○ remote/inactive   ▲ partial/drift   "
            "✕ broken   🔒 gated / in use   ⇩ used by configs\n",
            style=TEXT,
        )
        text.append("Journey:         ", style=MUTED)
        text.append(
            "target × build × model@revision × config → run. A config launches its "
            "pinned build, else the default build (⏎ in Builds sets it).\n\n",
            style=TEXT,
        )
        text.append("Debug: ", style=MUTED)
        text.append(
            "--debug writes structured JSONL self log and enables Textual devtools.\n",
            style=TEXT,
        )
        text.append("Ops: ", style=MUTED)
        text.append(
            "run artifacts live under ~/.local/state/vela/runs/ with 0600 logs.",
            style=TEXT,
        )
        return text

    @staticmethod
    def _action_pills() -> Text:
        text = Text("Open palette", style=f"bold {PURPLE} on {PURPLE_SURFACE}")
        text.append("  ")
        text.append("Close", style=f"bold {GOOD} on #0e2a21")
        return text
