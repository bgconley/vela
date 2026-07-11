"""Cell-aware text helpers shared across the TUI (bug-237).

Terminal columns are not characters: emoji and CJK glyphs occupy two cells.
Truncating by character count therefore splits a wide glyph across a truncation
boundary, printing half a cell or shoving the layout one column wide. These
helpers measure with :func:`rich.cells.cell_len` so a truncated string always
lands on a whole-cell boundary with a trailing ellipsis.

Hoisted here (rather than imported from ``screens/model_manager``) so both the
dashboard header in ``app.py`` and the manager rows can share one
implementation without the app importing from a screen module.
"""

from __future__ import annotations

from rich.cells import cell_len


def truncate_cells(text: str, budget: int) -> str:
    """Left-justified cell-aware truncation with a trailing ellipsis.

    Returns ``text`` unchanged when it already fits ``budget`` cells; otherwise
    keeps as many leading whole glyphs as fit alongside a one-cell ``…`` so the
    result never exceeds ``budget`` and never splits a double-width glyph.
    """
    if budget <= 0:
        return ""
    if cell_len(text) <= budget:
        return text
    if budget == 1:
        return "…"
    used = 0
    out: list[str] = []
    for char in text:
        width = cell_len(char)
        if used + width > budget - 1:
            break
        out.append(char)
        used += width
    return "".join(out) + "…"
