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

# ── Shared modal frame (bug-232 Flag Manager relayout — verified shipped) ────
# Ready-to-interpolate CSS *declaration* blocks for the near-full-screen,
# content-hugging modal frame that Tasks 4.2-4.4 apply to every manager/modal.
# Consume them exactly like the hex tokens above: interpolate the constant into a
# screen's f-string ``CSS`` (or ``DEFAULT_CSS``) INSIDE the panel's own selector
# block, escaping the literal CSS braces as ``{{ }}``. For example:
#
#     CSS = f'''
#     TargetManagerScreen #target-manager-panel {{
#         {MODAL_PANEL_CSS}
#         border: round {BORDER_STRONG};
#         background: {BG_PANEL};
#         padding: 1 2;
#     }}
#     TargetManagerScreen #target-manager-list-scroll {{
#         {MODAL_LIST_CSS}
#         max-height: 28;   /* screen-specific scroll cap */
#     }}
#     '''
#
# The constants carry NO braces, so they interpolate as plain string *values*
# (f-string brace-escaping applies only to the literal braces the screen writes,
# never to an interpolated value). That is why the frame is a declaration block,
# not a full ``selector {{ ... }}`` rule — each screen keeps its own panel id.
#
# Every panel rule is load-bearing. Do NOT reintroduce a fixed pixel/col width;
# that is exactly the bug-237 modal-clip regression this frame exists to prevent:
#   * width: 96%       fits every terminal >= the modal minimum and is never wider
#                      than the screen, so it cannot clip off the right edge.
#   * height: auto     hug the content instead of a fixed row count, so a short
#                      modal stays small and never leaves a mid-screen gap.
#   * max-height: 96%  cap growth just under the viewport so a tall modal never
#                      overflows past the top or bottom edge.
#   * overflow-y: auto once content exceeds max-height, scroll INSIDE the panel
#                      rather than pushing rows off-screen.
MODAL_PANEL_CSS = "width: 96%; height: auto; max-height: 96%; overflow-y: auto;"

# Companion rule for the long/variable list that lives in a ``VerticalScroll``
# inside the panel: full-width and content-hugging (it grows), then the consuming
# screen appends its own ``max-height: N`` so the list scrolls once it hits that
# cap — the "grow, then scroll" half of the bug-232 relayout. Pair with
# ``scroll.can_focus = False`` in ``on_mount`` so Tab still reaches the inputs.
MODAL_LIST_CSS = "width: 1fr; height: auto;"
