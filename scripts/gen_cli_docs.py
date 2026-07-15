#!/usr/bin/env python3
"""Generate ``docs/cli-reference.md`` from Vela's public Typer command tree.

The public command names, arguments, options, defaults, and help text come from
the live application.  A small amount of prose below records behavior that is
not expressible in Click metadata (target precedence, hidden compatibility
aliases, and command-specific automation semantics).

Run from the repository root::

    python3 scripts/gen_cli_docs.py
    python3 scripts/gen_cli_docs.py --stdout

``tests/test_docs.py::test_cli_doc_matches_public_command_tree`` compares the
committed page with this renderer so command changes require a documentation
update in the same commit.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable
from enum import Enum
from pathlib import Path
from typing import Any

import click
from typer.main import get_command

from vela.cli import app

DOC_PATH = Path(__file__).resolve().parents[1] / "docs" / "cli-reference.md"


def _escape(value: object) -> str:
    """Render one safe single-line Markdown-table cell."""
    normalized = str(value).replace("|", r"\|")
    return " ".join(normalized.split()) or "—"


def _default(value: Any) -> str:
    if value is None or value == () or value == []:
        return "—"
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, bool):
        return "`true`" if value else "`false`"
    if callable(value):
        return "dynamic"
    return f"`{_escape(value)}`"


def _type_hint(param: click.Parameter) -> str:
    if isinstance(param.type, click.Choice):
        value = " | ".join(str(choice) for choice in param.type.choices)
    else:
        value = getattr(param.type, "name", None) or str(param.type)
    if getattr(param, "multiple", False):
        value = f"{value}; repeatable"
    return _escape(value)


def _option_name(option: click.Option) -> str:
    names = [*option.opts, *option.secondary_opts]
    return ", ".join(f"`{name}`" for name in names)


def _arguments(command: click.Command) -> list[click.Argument]:
    return [param for param in command.params if isinstance(param, click.Argument)]


def _options(command: click.Command) -> list[click.Option]:
    return [
        param for param in command.params if isinstance(param, click.Option) and not param.hidden
    ]


def _usage(path: tuple[str, ...], command: click.Command) -> str:
    parts = ["vela", *path]
    for argument in _arguments(command):
        name = (argument.human_readable_name or argument.name or "ARG").upper()
        token = f"<{name}>" if argument.required else f"[{name}]"
        if argument.nargs == -1 or argument.multiple:
            token += "..."
        parts.append(token)
    if path == ("build", "run"):
        parts.append("[--target NAME] -- COMMAND [ARGS...]")
    elif _options(command):
        parts.append("[OPTIONS]")
    return " ".join(parts)


def _table(headers: list[str], rows: Iterable[list[str]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines)


def _render_parameters(command: click.Command) -> list[str]:
    out: list[str] = []
    arguments = _arguments(command)
    if arguments:
        out.extend(
            [
                "**Arguments**",
                "",
                _table(
                    ["Argument", "Required", "Type", "Description"],
                    (
                        [
                            f"`{(arg.human_readable_name or arg.name).upper()}`",
                            "yes" if arg.required else "no",
                            _type_hint(arg),
                            _escape(getattr(arg, "help", None) or "—"),
                        ]
                        for arg in arguments
                    ),
                ),
                "",
            ]
        )
    options = _options(command)
    if options:
        out.extend(
            [
                "**Options**",
                "",
                _table(
                    ["Option", "Required", "Value", "Default", "Description"],
                    (
                        [
                            _option_name(option),
                            "yes" if option.required else "no",
                            "flag" if option.is_flag else _type_hint(option),
                            _default(option.default),
                            _escape(option.help or "—"),
                        ]
                        for option in options
                    ),
                ),
                "",
            ]
        )
    return out


def _leaf_commands(
    group: click.Group,
    prefix: tuple[str, ...] = (),
) -> list[tuple[tuple[str, ...], click.Command]]:
    found: list[tuple[tuple[str, ...], click.Command]] = []
    for name, command in group.commands.items():
        if command.hidden:
            continue
        path = (*prefix, name)
        if isinstance(command, click.Group):
            found.extend(_leaf_commands(command, path))
        else:
            found.append((path, command))
    return found


def _render_command(path: tuple[str, ...], command: click.Command, level: int) -> list[str]:
    heading = "#" * level
    out = [
        f"{heading} `vela {' '.join(path)}`",
        "",
        _escape(command.help or command.short_help or ""),
        "",
        "```text",
        _usage(path, command),
        "```",
        "",
    ]
    out.extend(_render_parameters(command))
    if path == ("build", "run"):
        out.extend(
            [
                "Everything after `--` is executed by the selected build on the target.",
                "The controller never resolves the target's virtual-environment path.",
                "",
            ]
        )
    return out


def render_cli_docs() -> str:
    """Render the full public CLI reference deterministically."""
    root = get_command(app)
    if not isinstance(root, click.Group):
        raise TypeError("Vela's Typer root did not compile to a Click group")

    out: list[str] = [
        "# Vela CLI reference",
        "",
        "[Documentation home](index.md) · [Getting started](getting-started.md) · "
        "[Operations guide](operations.md) · [Configuration](configuration.md)",
        "",
        (
            "Generated from Vela's public Typer command tree by "
            "`scripts/gen_cli_docs.py`. Do not edit this file by hand. Run the "
            "generator after changing a command; the documentation test fails on drift."
        ),
        "",
        "## Invocation",
        "",
        "Running `vela` with no command opens the TUI. `vela tui` is the explicit "
        "equivalent. Append `--help` to any command or group for terminal-native help.",
        "",
        "For commands that operate on target-owned state, target selection is "
        "deterministic:",
        "",
        "1. An explicit `--target`.",
        "2. A non-empty `VELA_TARGET`.",
        "3. The target saved by `vela targets use`.",
        "4. The implicit `local` target.",
        "",
        "A deployment YAML's `target:` field is provenance, not command routing.",
        "",
        "## Root TUI options",
        "",
        "These options belong to the root invocation that opens the TUI. They are "
        "not inherited across a subcommand boundary: write `vela model list "
        "--target gpu-node`, not `vela --target gpu-node model list`. Each command "
        "table below lists the options it actually accepts.",
        "",
        _table(
            ["Option", "Required", "Value", "Default", "Description"],
            (
                [
                    _option_name(option),
                    "yes" if option.required else "no",
                    "flag" if option.is_flag else _type_hint(option),
                    _default(option.default),
                    _escape(option.help or "—"),
                ]
                for option in _options(root)
            ),
        ),
        "",
        "## Public command map",
        "",
        _table(
            ["Command", "Purpose"],
            (
                [f"`vela {' '.join(path)}`", _escape(command.help or command.short_help or "")]
                for path, command in _leaf_commands(root)
            ),
        ),
        "",
        "## Top-level commands",
        "",
    ]

    top_level = [(path, command) for path, command in _leaf_commands(root) if len(path) == 1]
    for path, command in top_level:
        out.extend(_render_command(path, command, level=3))

    for group_name, group in root.commands.items():
        if group.hidden or not isinstance(group, click.Group):
            continue
        out.extend(
            [
                f"## `{group_name}` commands",
                "",
                _escape(group.help or group.short_help or ""),
                "",
            ]
        )
        for path, command in _leaf_commands(group, (group_name,)):
            out.extend(_render_command(path, command, level=3))

    out.extend(
        [
            "## Hidden compatibility aliases",
            "",
            "These remain accepted for compatibility but are intentionally absent from "
            "normal help. Use the canonical spelling in scripts and documentation.",
            "",
            "| Compatibility alias | Canonical command |",
            "| --- | --- |",
            "| `vela preview NAME ...` | `vela run NAME --preview ...` |",
            "| `vela deploy list ...` | `vela list ...` |",
            "| `vela model add ...` | `vela model pin ...` |",
            "",
            "## Automation and exit-status rules",
            "",
            "`--json` changes output shape. Commands that validate or mutate state now "
            "preserve their failure exit where stated below, but consumers should still "
            "inspect the returned field (`ok`, `preflight_ok`, job status, or equivalent) "
            "as well as the process exit code.",
            "",
            "- Typer usage errors, ambiguous references, transport failures, and missing"
            " destructive `--yes` confirmation normally exit `2`.",
            "- Attached `vela run` and `vela logs --follow` return the eventual run code;"
            " a successful detached launch returns `0`.",
            "- `vela smoke` returns `0` only after READY and `/v1/models` verification,"
            " uses `2` for classified failures, and can use `1` for generic not-ready paths.",
            "- `vela smoke-tui` returns `0` for success and `2` for failure.",
            "- Build/model jobs normally return `0` for success and `2` for a failed job.",
            "- `vela config lint --json` emits its payload and returns `1` when `ok` is"
            " false. Build verify/repair and model verify emit their result and return"
            " `2` on failure.",
            "- `vela deploy create --json` never bypasses preflight: a failed preflight"
            " returns `2`, reports `preflight_ok: false`, and does not save unless"
            " `--force` was explicitly supplied.",
            "- `vela runs prune` operates on controller-local artifact directories and has"
            " no target option; begin with `--dry-run`.",
            "",
            "## Related references",
            "",
            "- [Configuration schema and discovery](configuration.md)",
            "- [Environment and storage paths](environment.md)",
            "- [TUI key reference](tui.md)",
            "- [Troubleshooting](troubleshooting.md)",
            "",
        ]
    )
    return "\n".join(out).rstrip() + "\n"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Generate docs/cli-reference.md from Vela's Typer command tree.",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print the rendered page instead of writing docs/cli-reference.md.",
    )
    args = parser.parse_args(argv)
    content = render_cli_docs()
    if args.stdout:
        print(content, end="")
        return
    DOC_PATH.write_text(content, encoding="utf-8")
    print(f"wrote {DOC_PATH}")


if __name__ == "__main__":
    main()
