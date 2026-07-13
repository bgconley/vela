"""Reusable Textual widgets for the Vela TUI.

These compound widgets are the shared "form language" the workflow screens build
on, mapping to the Figma Component Kit reference frame (node ``61:2``).
"""

from vela.tui.widgets.checkbox import Checkbox
from vela.tui.widgets.contextcard import ContextCard
from vela.tui.widgets.field import Field
from vela.tui.widgets.keyhintbar import KeyHintBar, pack_hint_rows
from vela.tui.widgets.preset_chips import PresetChips
from vela.tui.widgets.step_indicator import StepIndicator
from vela.tui.widgets.tags import (
    RECIPE_FLAGS,
    is_recipe_flag,
    source_tag,
    summarize_capabilities,
)
from vela.tui.widgets.validation_card import ValidationCard

__all__ = [
    "RECIPE_FLAGS",
    "Checkbox",
    "ContextCard",
    "Field",
    "KeyHintBar",
    "PresetChips",
    "StepIndicator",
    "ValidationCard",
    "is_recipe_flag",
    "pack_hint_rows",
    "source_tag",
    "summarize_capabilities",
]
