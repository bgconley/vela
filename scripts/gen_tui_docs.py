#!/usr/bin/env python3
"""Generate ``docs/tui.md`` from the TUI's declared key bindings (drift-proof).

Walks ``VelaApp.BINDINGS`` and each screen's own class-level ``BINDINGS`` and
renders one markdown table per screen. This is pure introspection — it imports the
app and screen classes but never constructs or runs them, so it is safe to call in
a headless environment.

Regenerate after changing any screen's ``BINDINGS``::

    python3 scripts/gen_tui_docs.py

``tests/test_docs.py::test_tui_doc_matches_bindings`` regenerates the same content
in memory and diffs it against the committed ``docs/tui.md``, so a bindings change
without a docs regen fails there.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import re
from pathlib import Path

from textual.binding import Binding
from textual.screen import Screen

import vela.tui.screens as screens_pkg
from vela.tui.app import VelaApp

DOC_PATH = Path(__file__).resolve().parents[1] / "docs" / "tui.md"

_BindingRow = tuple[str, str, str]


def _normalize(entry: object) -> _BindingRow:
    """Normalize a BINDINGS entry (plain tuple or ``Binding``) to (key, action, desc)."""
    if isinstance(entry, Binding):
        return entry.key, entry.action, entry.description or ""
    if isinstance(entry, tuple):
        key = str(entry[0])
        action = str(entry[1]) if len(entry) > 1 else ""
        description = str(entry[2]) if len(entry) > 2 else ""
        return key, action, description
    raise TypeError(f"unsupported BINDINGS entry: {entry!r}")


def _own_bindings(cls: type) -> list[_BindingRow]:
    """The class's OWN declared bindings (never inherited Textual defaults)."""
    raw = cls.__dict__.get("BINDINGS")
    if not raw:
        return []
    return [_normalize(entry) for entry in raw]


def _title(cls: type) -> str:
    if cls.__name__ == "VelaApp":
        return "Dashboard (root app bindings)"
    name = re.sub(r"Screen$", "", cls.__name__)
    words = re.findall(r"[A-Z][a-z0-9]*|[A-Z]+(?![a-z])", name)
    return " ".join(words) or name


def _key_cell(key: str) -> str:
    # A binding key can list several keys for one action (e.g. "l,enter"); show each.
    parts = [part.strip() for part in key.split(",") if part.strip()]
    return ", ".join(f"`{part}`" for part in parts) or "`?`"


def _render_table(bindings: list[_BindingRow]) -> str:
    lines = ["| Key | Action | Description |", "| --- | --- | --- |"]
    for key, action, description in bindings:
        lines.append(f"| {_key_cell(key)} | `{action}` | {description or '—'} |")
    return "\n".join(lines)


def _discover_screens() -> list[type]:
    """Every screen class in ``vela.tui.screens`` that declares its own BINDINGS."""
    found: dict[str, type] = {}
    for module_info in pkgutil.iter_modules(screens_pkg.__path__):
        module = importlib.import_module(f"{screens_pkg.__name__}.{module_info.name}")
        for _name, obj in inspect.getmembers(module, inspect.isclass):
            if (
                issubclass(obj, Screen)
                and obj.__module__ == module.__name__
                and "BINDINGS" in obj.__dict__
                and _own_bindings(obj)
            ):
                found[obj.__qualname__] = obj
    return sorted(found.values(), key=_title)


def render_tui_docs() -> str:
    """Render the full ``docs/tui.md`` content from live BINDINGS (deterministic)."""
    out: list[str] = [
        "# Vela TUI key reference",
        "",
        (
            "Generated from the TUI's declared key bindings by "
            "`scripts/gen_tui_docs.py` — do not edit by hand. Regenerate after "
            "changing any screen's `BINDINGS`; "
            "`tests/test_docs.py::test_tui_doc_matches_bindings` fails if this file "
            "drifts from the code."
        ),
        "",
        (
            "The dashboard footer advertises a state-filtered subset of these keys "
            "(control keys only during a run, log keys only when a log is present, and "
            "so on), but every binding below still works even when its footer hint is "
            "hidden."
        ),
        "",
    ]
    blocks: list[tuple[str, list[_BindingRow]]] = [(_title(VelaApp), _own_bindings(VelaApp))]
    for cls in _discover_screens():
        blocks.append((_title(cls), _own_bindings(cls)))
    for title, bindings in blocks:
        out.append(f"## {title}")
        out.append("")
        out.append(_render_table(bindings))
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def main() -> None:
    DOC_PATH.write_text(render_tui_docs(), encoding="utf-8")
    print(f"wrote {DOC_PATH}")


if __name__ == "__main__":
    main()
