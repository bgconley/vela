from __future__ import annotations

from pathlib import Path


def test_visual_qa_harness_captures_canonical_screens() -> None:
    script = Path("scripts/visual_qa.py").read_text(encoding="utf-8")

    for screen in (
        "dashboard",
        "config-picker",
        "new-deployment",
        "build-manager",
        "model-manager",
        "target-manager",
        "help-modal",
        "log-prompt-modal",
        "confirm-modal",
        "target-edit-modal",
    ):
        assert screen in script
    for size in ("wide-144x42", "standard-120x36", "compact-80x24"):
        assert size in script
    assert "vLLM-TUI-Loader-Screens---Canonical-v2" in script
    assert "layout/anatomy artifact" in script
