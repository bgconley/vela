from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_mypy_override_ratchet_is_documented_and_enforced() -> None:
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    docs = Path("docs/mypy-debt.md").read_text(encoding="utf-8")
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "docs/mypy-debt.md" in pyproject
    assert "135 errors across 11 modules" in docs
    assert "vela.engine.model_registry" in docs
    assert "python scripts/check_mypy_overrides.py" in workflow

    result = subprocess.run(
        [sys.executable, "scripts/check_mypy_overrides.py"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "11 ignored modules" in result.stdout
