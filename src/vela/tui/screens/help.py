from __future__ import annotations

from rich.cells import cell_len
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.events import Resize
from textual.screen import ModalScreen
from textual.widgets import Static

from vela.tui.theme import (
    AMBER,
    BG_BASE,
    BG_PANEL,
    BG_RAISED,
    BORDER_STRONG,
    CYAN,
    GREEN,
    MODAL_PANEL_CSS,
    SURFACE_GREEN,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
    VIOLET,
)

_MARKER_PAIRS: list[tuple[str, str]] = [
    ("📌", "pinned"),
    ("●", "ready/cached"),
    ("○", "remote/inactive"),
    ("▲", "partial/drift"),
    ("✕", "broken"),
    ("🔒", "gated / in use"),
    ("⇩", "used by configs"),
]
# The value column every help label line ("Markers:", "Journey:", …) aligns to.
_LABEL_COL = 17


class HelpScreen(ModalScreen):
    # bug-237: adopt the shared 4.1 modal frame in place of the fixed ``width: 82``
    # box that overflowed the 80-col screen, and pack the Markers legend to the
    # panel content width so a glyph/label pair never wraps mid-pair (orphaning
    # its glyph at the line end). Colors are migrated off the legacy
    # ACCENT/GOOD/WARN/PURPLE tokens to the canonical theme.py set.
    CSS = f"""
    HelpScreen {{
        align: center middle;
        background: {BG_BASE};
    }}

    HelpScreen #help-panel {{
        {MODAL_PANEL_CSS}
        border: round {BORDER_STRONG};
        background: {BG_PANEL};
        padding: 1 2;
    }}

    HelpScreen #help-text {{
        color: {TEXT_PRIMARY};
    }}

    HelpScreen #help-actions {{
        dock: bottom;
        height: auto;
        margin-top: 1;
        background: {BG_PANEL};
        color: {TEXT_SECONDARY};
    }}
    """

    BINDINGS = [
        ("escape", "close", "Close"),
        ("?", "close", "Close"),
        ("f1", "close", "Close"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="help-panel"):
            yield Static(id="help-text")
            yield Static(self._action_pills(), id="help-actions")

    def on_mount(self) -> None:
        self._render_help_text()

    def on_resize(self, event: Resize) -> None:
        # Re-pack the Markers legend to the new content width so its label-value
        # pairs keep wrapping whole instead of orphaning a glyph at the line end.
        try:
            self._render_help_text()
        except Exception:
            pass

    def _render_help_text(self) -> None:
        self.query_one("#help-text", Static).update(self._help_text(self._content_width()))

    def _content_width(self) -> int:
        # Content region available to a help line: 96% panel − round border (2) −
        # padding 1 2 (4) − vertical scrollbar (2). Mirrors the manager screens.
        term = self.size.width or 100
        return max(24, int(term * 0.96) - 8)

    def action_close(self) -> None:
        # A bare ``pop_screen`` action string does not resolve against a
        # ModalScreen in Textual, so the modal was a live trap (bug-221).
        # Dismiss explicitly, matching every other modal in the app.
        self.dismiss()

    @staticmethod
    def _help_text(content_width: int) -> Text:
        text = Text("Help — keys & markers\n\n", style=f"bold {CYAN}")
        text.append("Load / control:  ", style=TEXT_SECONDARY)
        text.append("l Load   ", style=GREEN)
        text.append("s Stop   ", style=AMBER)
        text.append("K Kill   ", style=f"bold {AMBER}")
        text.append("r Restart   ", style=GREEN)
        text.append("q Quit\n", style=AMBER)
        text.append("Logs:            ", style=TEXT_SECONDARY)
        text.append("/ Search   f Filter   p Pause autoscroll   w Wrap\n", style=TEXT_PRIMARY)
        text.append("Navigation:      ", style=TEXT_SECONDARY)
        text.append("g Top   G Bottom   Tab focus\n", style=TEXT_PRIMARY)
        text.append("Composition:     ", style=TEXT_SECONDARY)
        text.append("n New deployment   b Builds   m Models   F Flags\n", style=TEXT_PRIMARY)
        text.append("Targets:         ", style=TEXT_SECONDARY)
        text.append("t Target manager   R Reconnect\n", style=TEXT_PRIMARY)
        text.append("Discovery:       ", style=TEXT_SECONDARY)
        text.append("Ctrl+P palette has every action\n", style=VIOLET)
        text.append("Screens:         ", style=TEXT_SECONDARY)
        text.append("c Config picker   ? or F1 Help\n\n", style=TEXT_PRIMARY)
        _append_markers(text, content_width)
        text.append("Journey:         ", style=TEXT_SECONDARY)
        text.append(
            "target × build × model@revision × config → run. A config launches its "
            "pinned build, else the default build (⏎ in Builds sets it).\n\n",
            style=TEXT_PRIMARY,
        )
        text.append("Debug: ", style=TEXT_SECONDARY)
        text.append(
            "--debug writes structured JSONL self log and enables Textual devtools.\n",
            style=TEXT_PRIMARY,
        )
        text.append("Ops: ", style=TEXT_SECONDARY)
        text.append(
            "run artifacts live under ~/.local/state/vela/runs/ with 0600 logs.",
            style=TEXT_PRIMARY,
        )
        return text

    @staticmethod
    def _action_pills() -> Text:
        text = Text("Open palette", style=f"bold {VIOLET} on {BG_RAISED}")
        text.append("  ")
        text.append("Close", style=f"bold {GREEN} on {SURFACE_GREEN}")
        return text


def _append_markers(text: Text, content_width: int) -> None:
    # Pack the marker legend into as many lines as fit the panel content width,
    # each holding only whole ``glyph label`` pairs, so a pair never wraps mid-way
    # and orphans its glyph at the line end (bug-237). Continuation lines align
    # under the value column, matching the other help labels.
    label = "Markers:"
    text.append(label + " " * (_LABEL_COL - len(label)), style=TEXT_SECONDARY)
    lines = _wrap_marker_pairs(_MARKER_PAIRS, max(8, content_width - _LABEL_COL))
    for index, line in enumerate(lines):
        if index:
            text.append("\n" + " " * _LABEL_COL)
        text.append(line, style=TEXT_PRIMARY)
    text.append("\n")


def _wrap_marker_pairs(pairs: list[tuple[str, str]], width: int) -> list[str]:
    sep = "   "  # 3-space separator between whole pairs (matches the legacy legend)
    lines: list[str] = []
    current: list[str] = []
    used = 0
    for glyph, label in pairs:
        piece = f"{glyph} {label}"
        piece_width = cell_len(piece)
        addition = piece_width if not current else len(sep) + piece_width
        if current and used + addition > width:
            lines.append(sep.join(current))
            current, used = [], 0
            addition = piece_width
        current.append(piece)
        used += addition
    if current:
        lines.append(sep.join(current))
    return lines
