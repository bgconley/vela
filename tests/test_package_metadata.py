from __future__ import annotations

from pathlib import Path

import tomllib


def test_model_download_progress_runtime_dependency_is_declared() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert any(
        str(dependency).lower().startswith("tqdm")
        for dependency in project["dependencies"]
    )
