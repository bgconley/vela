#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path

BASELINE_IGNORED_MODULES = {
    "vela.agent.local",
    "vela.cli",
    "vela.engine.build_registry",
    "vela.engine.model_registry",
    "vela.engine.process_manager",
    "vela.engine.supervisor",
    "vela.tui.app",
    "vela.tui.screens.config_picker",
    "vela.tui.screens.flag_manager",
    "vela.tui.screens.model_manager",
    "vela.tui.screens.new_deployment",
}


def main() -> int:
    pyproject = Path("pyproject.toml")
    if not pyproject.exists():
        print("pyproject.toml not found", file=sys.stderr)
        return 2

    modules = _ignored_mypy_modules(pyproject.read_text(encoding="utf-8"))
    unexpected = sorted(set(modules) - BASELINE_IGNORED_MODULES)
    if unexpected:
        joined = ", ".join(unexpected)
        print(f"mypy override ratchet failed; new ignored modules: {joined}", file=sys.stderr)
        print(
            "Remove the new override or update docs/mypy-debt.md with an explicit decision.",
            file=sys.stderr,
        )
        return 1

    duplicates = sorted({module for module in modules if modules.count(module) > 1})
    if duplicates:
        print(f"duplicate mypy override modules: {', '.join(duplicates)}", file=sys.stderr)
        return 1

    print(f"mypy override ratchet ok: {len(modules)} ignored modules")
    return 0


def _ignored_mypy_modules(text: str) -> list[str]:
    modules: list[str] = []
    in_module_list = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("module = ["):
            in_module_list = True
        if in_module_list:
            modules.extend(re.findall(r'"([^"]+)"', stripped))
            if stripped.endswith("]"):
                in_module_list = False
    return modules


if __name__ == "__main__":
    raise SystemExit(main())
