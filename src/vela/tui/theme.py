"""Shared Figma-derived color tokens for the Textual TUI.

Two sets live here during the UI overhaul:

* **Legacy tokens** (``ACCENT``/``GOOD``/``BASE``/...): the original subset, kept
  unchanged for back-compat with screens not yet refactored.
* **"Vela Terminal" canonical tokens** (``BG_BASE``/``TEXT_PRIMARY``/``GREEN``/...):
  the full palette from the approved Figma redesign (page ``39:2``). New widgets
  and refactored screens use these. As each screen is refactored, its hardcoded
  hex/legacy tokens are migrated to this set.
"""

# ── Legacy tokens (pre-overhaul; kept for back-compat) ──────────────────────
ACCENT = "#60d7f8"
ACCENT_SURFACE = "#0c2238"
GOOD = "#67e8a5"
GOOD_SURFACE = "#0e2a21"
WARN = "#f6c85f"
WARN_SURFACE = "#2b2410"
BAD = "#ff6b6b"
BAD_SURFACE = "#351b1f"
MUTED = "#8ba4ae"
MUTED_SURFACE = "#14202b"
TEXT = "#e8f1f2"
BASE = "#091015"
SURFACE = "#101923"
SURFACE_ALT = "#101a22"
BORDER = "#274254"
PURPLE = "#b48cff"
PURPLE_SURFACE = "#241b37"

# ── "Vela Terminal" canonical tokens (Figma 39:2; use these going forward) ──
# Backgrounds
BG_BASE = "#0c141b"
BG_PANEL = "#101923"
BG_RAISED = "#172532"
BG_INSET = "#0d151d"
BG_FIELD = "#0a1118"
# Borders
BORDER_SUBTLE = "#22384a"
BORDER_STRONG = "#2f5168"
BORDER_FOCUS = "#60d7f8"
# Text
TEXT_PRIMARY = "#e8f1f2"
TEXT_SECONDARY = "#8ba4ae"
TEXT_FAINT = "#56707c"
TEXT_ON_ACCENT = "#06120c"
# Accents
GREEN = "#67e8a5"
CYAN = "#60d7f8"
AMBER = "#f6c85f"
RED = "#ff6b7a"
BLUE = "#5fa8e8"
VIOLET = "#b69cf0"
# Tinted surfaces
SURFACE_GREEN = "#0e2a21"
SURFACE_AMBER = "#2b2410"
SURFACE_RED = "#2b1218"
SURFACE_BLUE = "#0c2238"
SURFACE_CYAN = "#0c2330"
