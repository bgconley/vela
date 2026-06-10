"""The ``Field`` compound widget — the shared "form language" for Vela screens.

Maps to the Figma Component Kit (node ``61:2``). A ``Field`` renders a bold label,
an optional ``required``/``optional`` tag, a caller-provided control, and one or
more dim helper lines (what the field does + where its value comes from).

The control (``Input``/``Select``/``Checkbox``) is passed in and kept as-is, so its
``id`` and event handlers are preserved — only presentation is standardized. This
lets screens adopt the widget without rewiring behavior.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Label, Static

from vela.tui.theme import (
    AMBER,
    BG_FIELD,
    BORDER_FOCUS,
    BORDER_SUBTLE,
    TEXT_FAINT,
    TEXT_PRIMARY,
)


class Field(Vertical):
    """Label · required/optional tag · control · helper line(s)."""

    DEFAULT_CSS = f"""
    Field {{
        height: auto;
        margin-bottom: 1;
    }}
    Field .field-labelrow {{ height: 1; }}
    Field .field-label {{ color: {TEXT_PRIMARY}; text-style: bold; }}
    Field .field-req {{ color: {AMBER}; margin-left: 1; }}
    Field .field-opt {{ color: {TEXT_FAINT}; margin-left: 1; }}
    Field Input, Field Select {{
        background: {BG_FIELD};
        border: round {BORDER_SUBTLE};
        color: {TEXT_PRIMARY};
    }}
    Field Input:focus, Field Select:focus {{
        border: round {BORDER_FOCUS};
    }}
    Field .field-helper {{ color: {TEXT_FAINT}; height: auto; }}
    """

    def __init__(
        self,
        label: str,
        control: Widget,
        *,
        helper: str | list[str] = "",
        required: bool = False,
        optional: bool = False,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self._label = label
        self._control = control
        if isinstance(helper, str):
            self._helpers = [helper] if helper else []
        else:
            self._helpers = list(helper)
        self._required = required
        self._optional = optional

    def compose(self) -> ComposeResult:
        with Horizontal(classes="field-labelrow"):
            yield Label(self._label, classes="field-label")
            if self._required:
                yield Label("required", classes="field-req")
            elif self._optional:
                yield Label("optional", classes="field-opt")
        yield self._control
        for text in self._helpers:
            yield Static(text, classes="field-helper")
