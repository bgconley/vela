"""Reusable Textual widgets for the Vela TUI.

These compound widgets are the shared "form language" the workflow screens build
on, mapping to the Figma Component Kit reference frame (node ``61:2``).
"""

from vela.tui.widgets.contextcard import ContextCard
from vela.tui.widgets.field import Field
from vela.tui.widgets.keyhintbar import KeyHintBar
from vela.tui.widgets.masterdetail import MasterDetail
from vela.tui.widgets.preset_chips import PresetChips
from vela.tui.widgets.tags import (
    RECIPE_FLAGS,
    is_recipe_flag,
    source_tag,
    summarize_capabilities,
)
from vela.tui.widgets.validation_card import ValidationCard

__all__ = [
    "RECIPE_FLAGS",
    "ContextCard",
    "Field",
    "KeyHintBar",
    "MasterDetail",
    "PresetChips",
    "ValidationCard",
    "is_recipe_flag",
    "source_tag",
    "summarize_capabilities",
]
