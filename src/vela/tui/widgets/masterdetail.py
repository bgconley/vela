"""``MasterDetail`` — the shared two-pane layout for the manager screens.

A fixed-width list pane beside a flexible detail pane, with an optional footer
slotted underneath. It wraps **caller-provided** panes (the same contract-
preserving trick as :class:`~vela.tui.widgets.field.Field`): the smoke suite
queries ``#*-list`` / ``#*-detail`` directly and reads ``.content``, so those
panes must stay the caller's own widgets with their ids intact. ``MasterDetail``
only supplies the layout chrome.

Maps to the Figma master-detail frames (Target ``44:2``, Flag ``55:2``) and the
Component Kit (``61:2``).
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widget import Widget


class MasterDetail(Vertical):
    DEFAULT_CSS = """
    MasterDetail {
        height: auto;
    }

    MasterDetail .master-detail-body {
        height: auto;
    }

    MasterDetail .master-detail-list {
        width: 44;
        height: auto;
    }

    MasterDetail .master-detail-detail {
        width: 1fr;
        height: auto;
    }
    """

    def __init__(
        self,
        list_pane: Widget,
        detail_pane: Widget,
        *,
        footer: Widget | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self._list_pane = list_pane
        self._detail_pane = detail_pane
        self._footer = footer
        # Tag the caller panes for default sizing; screens can still override by
        # id (id selectors beat the class defaults above).
        list_pane.add_class("master-detail-list")
        detail_pane.add_class("master-detail-detail")

    def compose(self) -> ComposeResult:
        with Horizontal(classes="master-detail-body"):
            yield self._list_pane
            yield self._detail_pane
        if self._footer is not None:
            yield self._footer
