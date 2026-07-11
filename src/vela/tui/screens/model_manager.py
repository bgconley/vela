from __future__ import annotations

from typing import Any

from rich.cells import cell_len
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.events import Resize
from textual.screen import ModalScreen
from textual.widgets import Static

from vela.tui.cells import truncate_cells as _truncate_cells
from vela.tui.theme import (
    AMBER,
    BG_BASE,
    BG_PANEL,
    BORDER_STRONG,
    CYAN,
    GREEN,
    MODAL_LIST_CSS,
    MODAL_PANEL_CSS,
    RED,
    TEXT_FAINT,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)
from vela.tui.widgets import KeyHintBar, pack_hint_rows, source_tag

_FOOTER_HINTS = [
    ("⏎", "Select"),
    ("d", "Download"),
    ("p", "Pin"),
    ("r", "Refresh"),
    ("v", "Verify"),
    ("x", "Remove"),
    ("Esc", "Close"),
]

# Short source words for the list row's source-tag column.
_SOURCE_WORDS = {"hf_repo": "hf", "local_path": "local", "url": "url"}

# Terminal-width breakpoints for the responsive row: below these column counts
# the row sheds its rightmost columns so it never wraps (bug-237). sha8 goes
# first (identity lives in the detail too), then size.
_SHOW_SHA_MIN_COLS = 100
_SHOW_SIZE_MIN_COLS = 80


class ModelManagerScreen(ModalScreen):
    # Full-width STACKED rebuild (bug-237): the shared 4.1 modal frame
    # (MODAL_PANEL_CSS / MODAL_LIST_CSS) replaces the old fixed ``width: 104``
    # box that clipped even at 100 cols, and the two-pane MasterDetail is dropped
    # for a full-width list-in-a-VerticalScroll STACKED ABOVE the detail (the
    # Target Manager 4.2 precedent). The list rows use a scannable one-line
    # grammar that truncates the name and drops columns by width instead of
    # wrapping into interleaved SHA fragments.
    CSS = f"""
    ModelManagerScreen {{
        align: center middle;
        background: {BG_BASE};
    }}

    ModelManagerScreen #model-manager-panel {{
        {MODAL_PANEL_CSS}
        border: round {BORDER_STRONG};
        background: {BG_PANEL};
        padding: 1 2;
    }}

    ModelManagerScreen #model-manager-list-scroll {{
        {MODAL_LIST_CSS}
        max-height: 16;
        margin-bottom: 1;
    }}

    ModelManagerScreen #model-manager-list {{
        width: 1fr;
        height: auto;
        color: {TEXT_PRIMARY};
    }}

    ModelManagerScreen #model-manager-detail {{
        width: 1fr;
        height: auto;
        color: {TEXT_PRIMARY};
    }}

    ModelManagerScreen #model-manager-footer {{
        dock: bottom;
        height: auto;
        margin-top: 1;
        background: {BG_PANEL};
    }}
    """

    BINDINGS = [
        ("up", "previous", "Previous"),
        ("down", "next", "Next"),
        ("enter", "accept", "Select"),
        ("d", "download", "Download"),
        ("p", "pin", "Pin"),
        Binding("r", "refresh_models", "Refresh", priority=True),
        ("v", "verify", "Verify"),
        ("x", "remove", "Remove"),
        ("escape", "cancel", "Close"),
    ]

    def __init__(
        self, payload: dict[str, Any], *, focus_model: str | None = None
    ) -> None:
        super().__init__(id="model-manager")
        models = payload.get("models", [])
        self.models = [dict(item) for item in models if isinstance(item, dict)]
        self.selected_index = self._focus_index(focus_model)

    def compose(self) -> ComposeResult:
        with Vertical(id="model-manager-panel"):
            with VerticalScroll(id="model-manager-list-scroll"):
                yield Static(id="model-manager-list")
            yield Static(id="model-manager-detail")
            with Vertical(id="model-manager-footer"):
                for index, row in enumerate(pack_hint_rows(_FOOTER_HINTS)):
                    yield KeyHintBar(row, id=f"model-manager-footer-row-{index}")

    def on_mount(self) -> None:
        # Keep the list scroll out of the Tab order so key bindings reach the
        # screen instead of scrolling the region (no focusable inputs here).
        try:
            self.query_one("#model-manager-list-scroll").can_focus = False
        except Exception:
            pass
        self._refresh()

    def on_resize(self, event: Resize) -> None:
        # Re-render rows once the real width is known / on terminal resize so the
        # width-responsive columns and name truncation track the panel size.
        try:
            self._refresh()
        except Exception:
            pass

    def action_previous(self) -> None:
        if self.models:
            self.selected_index = max(0, self.selected_index - 1)
            self._refresh()

    def action_next(self) -> None:
        if self.models:
            self.selected_index = min(len(self.models) - 1, self.selected_index + 1)
            self._refresh()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_accept(self) -> None:
        model = self._selected_model()
        if model is None:
            self.dismiss(None)
            return
        self.dismiss(_model_selection_payload(model))

    def action_download(self) -> None:
        model = self._selected_model()
        if model is None:
            self.dismiss(None)
            return
        if _is_url_model(model):
            self.dismiss(
                {
                    "action": "download_unavailable",
                    "label": _model_label(model),
                    "reason": "launch-time-only",
                }
            )
            return
        self.dismiss(_model_download_payload(model))

    def action_verify(self) -> None:
        model = self._selected_model()
        if model is None:
            self.dismiss(None)
            return
        self.dismiss(_model_action_payload("verify_model", model))

    def action_pin(self) -> None:
        model = self._selected_model()
        initial = _initial_pin_params(model) if model is not None else {}
        self.dismiss({"action": "pin_model", "initial": initial})

    def action_refresh_models(self) -> None:
        self.dismiss({"action": "refresh_models"})

    def action_remove(self) -> None:
        model = self._selected_model()
        if model is None:
            self.dismiss(None)
            return
        self.dismiss(_model_action_payload("remove_model", model))

    def _refresh(self) -> None:
        self.query_one("#model-manager-list", Static).update(self._render_list())
        self.query_one("#model-manager-detail", Static).update(self._render_detail())

    def _render_list(self) -> Text:
        text = Text()
        text.append("Model Manager\n", style=f"bold {CYAN}")
        if not self.models:
            text.append(
                "\nNo models yet — press p to pin one (HF repo id, local path, or URL)",
                style=TEXT_FAINT,
            )
            return text
        text.append("\n")
        content_width = self._list_content_width()
        term_width = self.size.width or 100
        show_size = term_width >= _SHOW_SIZE_MIN_COLS
        show_sha = term_width >= _SHOW_SHA_MIN_COLS
        for index, model in enumerate(self.models):
            self._append_row(text, index, model, content_width, show_size, show_sha)
        return text

    def _append_row(
        self,
        text: Text,
        index: int,
        model: dict[str, Any],
        content_width: int,
        show_size: bool,
        show_sha: bool,
    ) -> None:
        selected = index == self.selected_index
        marker = ">" if selected else " "
        dot = _model_status_dot(model)
        tag = _row_source_tag(model)
        cache_state = str(model.get("cache_state") or "unknown")
        size = _row_size_label(model)
        sha = _sha8(model)
        # Reserve the fixed right-side columns, give the name whatever is left,
        # then truncate it so the row never wraps.
        fixed = (
            1  # marker
            + 1  # space
            + cell_len(dot)
            + 1  # space
            + 2 + cell_len(tag.plain)
            + 2 + cell_len(cache_state)
            + (2 + cell_len(size) if show_size else 0)
            + (2 + cell_len(sha) if show_sha else 0)
        )
        name = _truncate_cells(_model_label(model), max(1, content_width - fixed))
        text.append(marker, style=CYAN if selected else TEXT_FAINT)
        text.append(" ")
        text.append(dot, style=_model_status_color(model))
        text.append(" ")
        text.append(name, style=f"bold {TEXT_PRIMARY}" if selected else TEXT_PRIMARY)
        text.append("  ")
        text.append(tag.plain, style=tag.style)
        text.append("  ")
        text.append(cache_state, style=TEXT_SECONDARY)
        if show_size:
            text.append("  ")
            text.append(size, style=TEXT_FAINT)
        if show_sha:
            text.append("  ")
            text.append(sha, style=TEXT_FAINT)
        text.append("\n")

    def _render_detail(self) -> Text:
        model = self._selected_model()
        if model is None:
            return Text("No model selected", style=TEXT_FAINT)
        files = _dict_or_empty(model.get("files"))
        text = Text()
        text.append(f"{_model_label(model)}\n", style=f"bold {TEXT_PRIMARY}")
        text.append("\n")
        rows = [
            ("entry_id", str(model.get("entry_id") or "-")),
            ("source", str(model.get("source") or "-")),
            ("pinned", "yes" if _is_pinned(model) else "no"),
            ("repo", str(model.get("repo_id") or "-")),
            ("revision", _revision_detail(model)),
            ("cache", str(model.get("cache_state") or "unknown")),
            ("quant", _quant_label(model)),
            ("size", _size_label(model)),
        ]
        auth = _auth_detail(model)
        if auth:
            rows.append(("auth", auth))
        config_refs = model.get("config_refs")
        if isinstance(config_refs, list):
            rows.append(("used_by", _config_refs_label(config_refs)))
        rows.append(("files", _files_label(files)))
        if _is_url_model(model):
            rows.append(("download", "launch-time-only"))
            rows.append(("url", str(model.get("url") or "-")))
        for key, value in rows:
            text.append(f"{key}: ", style=TEXT_FAINT)
            text.append(f"{value}\n", style=TEXT_PRIMARY)
        return text

    def _list_content_width(self) -> int:
        # Content region available to a list row: 96% panel − round border (2) −
        # padding 1 2 (4) − vertical scrollbar (2). A safe lower bound (matches
        # the measured width when the scrollbar is present) so truncation never
        # under-reserves and wraps.
        term = self.size.width or 100
        return max(24, int(term * 0.96) - 8)

    def _selected_model(self) -> dict[str, Any] | None:
        if not self.models:
            return None
        return self.models[self.selected_index]

    def _focus_index(self, focus_model: str | None) -> int:
        if focus_model:
            for index, model in enumerate(self.models):
                if focus_model in {
                    str(model.get("entry_id") or ""),
                    str(model.get("display_name") or ""),
                }:
                    return index
        return 0


def _model_label(model: dict[str, Any]) -> str:
    return str(model.get("display_name") or model.get("entry_id") or "unnamed-model")


def _model_reference(model: dict[str, Any]) -> str:
    return str(model.get("entry_id") or model.get("display_name") or "")


def _model_action_payload(action: str, model: dict[str, Any]) -> dict[str, Any]:
    return {
        "action": action,
        "model_ref": _model_reference(model),
        "label": _model_label(model),
    }


def _model_download_payload(model: dict[str, Any]) -> dict[str, Any]:
    payload = _model_action_payload("download", model)
    for field in (
        "entry_id",
        "display_name",
        "repo_id",
        "revision",
        "commit_sha",
        "cache_state",
        "gated",
        "token_required",
        "allow_patterns",
        "ignore_patterns",
    ):
        if field in model:
            payload[field] = model[field]
    return payload


def _model_selection_payload(model: dict[str, Any]) -> dict[str, Any]:
    payload = _model_action_payload("select_model", model)
    revision = model.get("commit_sha") or model.get("revision")
    if isinstance(revision, str) and revision.strip():
        payload["revision"] = revision.strip()
    for field in ("cache_state", "gated", "token_required"):
        if field in model:
            payload[field] = model[field]
    return payload


def _is_url_model(model: dict[str, Any]) -> bool:
    return str(model.get("source") or "") == "url"


def _is_pinned(model: dict[str, Any]) -> bool:
    # Mirrors new_deployment._is_pinned_entry: real registry pins are
    # pinned=True, synthetic HF-cache-scan rows are pinned=False, and a missing
    # field fails open to pinned=True (compatible-by-default for simple fixtures).
    value = model.get("pinned")
    if value is None:
        return True
    return bool(value)


def _initial_pin_params(model: dict[str, Any]) -> dict[str, Any]:
    fields = [
        ("entry_id", model.get("entry_id")),
        ("repo_id", model.get("repo_id")),
        ("url", model.get("url")),
        ("display_name", model.get("display_name")),
        ("revision", model.get("revision")),
        ("commit_sha", model.get("commit_sha")),
        ("quant_format", model.get("quant_format")),
        ("tokenizer", model.get("tokenizer")),
        ("notes", model.get("notes")),
    ]
    params = {key: value for key, value in fields if isinstance(value, str) and value.strip()}
    if model.get("gated"):
        params["gated"] = True
    if model.get("token_required"):
        params["token_required"] = True
    if params.get("url"):
        params["source"] = "url"
    return params


def _row_source_tag(model: dict[str, Any]) -> Text:
    """Source/pin tag for a list row, colored via the shared source-tag palette.

    Keeps pinned entries visually distinct from HF-cache-scan rows: registry
    pins read as ``modeled`` (cyan), launch-time-only URL models as
    ``passthrough`` (violet), and unpinned cache-scan rows as ``unknown``
    (amber). Returns a Rich :class:`Text` so its plain ``str()`` stays markup-free.
    """
    source = str(model.get("source") or "").lower()
    word = _SOURCE_WORDS.get(source, source or "scan")
    if source == "url":
        kind = "passthrough"
    elif _is_pinned(model):
        kind = "modeled"
    else:
        kind = "unknown"
    return source_tag(kind, word)


def _quant_label(model: dict[str, Any]) -> str:
    return str(model.get("quant_format") or "none")


def _revision_detail(model: dict[str, Any]) -> str:
    revision = str(model.get("revision") or "-")
    commit = model.get("commit_sha")
    if commit:
        return f"{revision} → {commit}"
    return revision


def _sha8(model: dict[str, Any]) -> str:
    # Short 8-char identity for the row; the full sha stays in the detail pane.
    sha = str(model.get("commit_sha") or model.get("revision") or "").strip()
    if not sha:
        return "—"
    if cell_len(sha) <= 8:
        return sha
    if _is_hex_sha(sha):
        return sha[:8]  # conventional short-sha prefix (01234567)
    # A non-sha ref longer than 8 cells (a branch/tag like release-candidate)
    # ellipsizes to a whole word — release… — instead of a bare chop that leaves
    # a dangling separator (release-).
    return _truncate_cells(sha, 8)


def _is_hex_sha(value: str) -> bool:
    return all(char in "0123456789abcdefABCDEF" for char in value)


def _model_status_dot(model: dict[str, Any]) -> str:
    state = str(model.get("cache_state") or "").lower()
    if state in {"cached", "ready", "local"}:
        return "●"
    if state in {"remote_only", "remote-only"}:
        return "○"
    if state in {"partial", "drift"}:
        return "▲"
    if state in {"downloading", "in-progress"}:
        return "◐"
    if state in {"missing", "unresolved"}:
        return "✕"
    return "○"


def _model_status_color(model: dict[str, Any]) -> str:
    state = str(model.get("cache_state") or "").lower()
    if state in {"cached", "ready", "local"}:
        return GREEN
    if state in {"partial", "drift"}:
        return AMBER
    if state in {"downloading", "in-progress"}:
        return CYAN
    if state in {"missing", "unresolved"}:
        return RED
    return TEXT_FAINT


def _row_size_label(model: dict[str, Any]) -> str:
    # Row size: `—` for unknown / zero / metadata-only (all-`unknown`-weights)
    # caches — never the misleading `0.0 GB` — `<0.1 GB` for small-but-real
    # weights, else the shared GB formatting.
    files = _dict_or_empty(model.get("files"))
    size = (
        _size_value(model.get("unique_size_bytes"))
        or _size_value(model.get("nominal_size_bytes"))
        or _size_value(model.get("size_bytes"))
    )
    if size <= 0 or not _weights_known(files):
        return "—"
    gb = size / 1_000_000_000
    if gb < 0.1:
        return "<0.1 GB"
    return _gb_label(size)


def _weights_known(files: dict[str, Any]) -> bool:
    fmt = str(files.get("weights_format") or "").lower()
    return bool(fmt) and fmt != "unknown"


def _size_label(model: dict[str, Any]) -> str:
    unique = _size_value(model.get("unique_size_bytes"))
    nominal = _size_value(model.get("nominal_size_bytes"))
    if unique > 0 and nominal > 0 and nominal != unique:
        return f"{_gb_label(unique)} unique / {_gb_label(nominal)} nominal"
    size = unique or nominal or _size_value(model.get("size_bytes"))
    if size <= 0:
        return "--"
    return _gb_label(size)


_HF_TOKEN_WHERE = "(agent env or config env: block)"


def _config_refs_label(refs: list[object]) -> str:
    names = [str(item) for item in refs if str(item)]
    if not names:
        return "0 configs"
    visible = names[:3]
    suffix = f", +{len(names) - len(visible)}" if len(names) > len(visible) else ""
    return f"{len(names)} ({', '.join(visible)}{suffix})"


def _auth_detail(model: dict[str, Any]) -> str:
    gated = bool(model.get("gated"))
    token_required = bool(model.get("token_required")) or gated
    if gated and token_required:
        return f"gated, requires HF_TOKEN {_HF_TOKEN_WHERE}"
    if token_required:
        return f"requires HF_TOKEN {_HF_TOKEN_WHERE}"
    return ""


def _size_value(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _gb_label(size: int) -> str:
    return f"{size / 1_000_000_000:.1f} GB"




def _files_label(files: dict[str, Any]) -> str:
    count = files.get("count")
    weights_format = files.get("weights_format")
    if count is None and not weights_format:
        return "-"
    parts = []
    if count is not None:
        parts.append(str(count))
    if weights_format:
        parts.append(str(weights_format))
    return " ".join(parts)


def _dict_or_empty(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}
