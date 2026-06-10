"""Shared color/text primitives for the master-detail manager screens.

These are pure helpers (no Textual widgets) because the smoke suite pins the
manager screens' ``#*-list`` / ``#*-detail`` panes as ``Static`` widgets and
asserts on ``str(static.content)``. Rendering color as Rich :class:`Text`
(rather than markup strings) keeps ``str(content)`` plain, so every pinned
substring survives while the panes still carry the Figma semantic palette.

Maps to the Figma Component Kit (``61:2``) source-tag + summary primitives.
"""

from __future__ import annotations

from collections.abc import Iterable

from rich.text import Text

from vela.tui.theme import AMBER, CYAN, TEXT_SECONDARY, VIOLET

# Source-tag palette (Figma): where a value comes from / how it is treated.
_SOURCE_TAG_COLORS = {
    "modeled": CYAN,  # known to the build's flag map
    "passthrough": VIOLET,  # raw arg forwarded verbatim
    "unknown": AMBER,  # not recognized by the build
    "recipe": AMBER,  # recipe-protected precision flag
}

# Precision-critical flags the local Blackwell SM120 recipe is the authority for;
# editing them risks diverging from the validated stack, so they get a warning.
RECIPE_FLAGS = frozenset({"dtype", "kv_cache_dtype"})


def source_tag(kind: str, label: str | None = None) -> Text:
    """A single-color tag word (e.g. ``modeled``/``passthrough``/``recipe``).

    Returns a Rich :class:`Text` so its plain ``str()`` is just the label —
    appendable into a pane's content without leaking markup into asserted
    substrings.
    """
    color = _SOURCE_TAG_COLORS.get(kind, TEXT_SECONDARY)
    return Text(kind if label is None else label, style=color)


def summarize_capabilities(caps: Iterable[str], *, limit: int = 8) -> str:
    """Collapse a capability list to a count + view-all once it gets long.

    Small lists render inline (sorted) so the operator sees exactly what's
    supported; long ones collapse to ``"N supported ✓ · ⤢ view all"`` instead of
    dumping a ~60-method wall (the Target Manager screenshot-#2 fix).
    """
    items = sorted({str(cap) for cap in caps if str(cap)})
    if len(items) <= limit:
        return ", ".join(items)
    return f"{len(items)} supported ✓ · v view all"


def is_recipe_flag(field: str) -> bool:
    """True for precision-critical, recipe-protected engine fields."""
    return field in RECIPE_FLAGS
