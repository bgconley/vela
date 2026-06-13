"""Generate canonical TUI visual QA artifacts.

This is the broader sibling of ``readme_screenshots.py``: it captures the
canonical dashboard, manager, wizard, and modal surfaces at multiple terminal
sizes so the SVGs can be compared against the Figma handoff.

Run from the repo root:

    python3 scripts/visual_qa.py
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

from readme_screenshots import QWEN_CONFIG, TINY_CONFIG, ScreenshotClient, _tmp_state

from vela.tui.app import VelaApp
from vela.tui.screens.confirm import ConfirmScreen
from vela.tui.screens.target_edit import TargetEditScreen

SIZES = {
    "wide-144x42": (144, 42),
    "standard-120x36": (120, 36),
    "compact-80x24": (80, 24),
}

FIGMA_REFERENCE = (
    "https://www.figma.com/design/9xUgzyoFqWmd40tV5dwaHv/"
    "vLLM-TUI-Loader-Screens---Canonical-v2?node-id=22-2"
)


async def _capture_size(out_dir: Path, label: str, size: tuple[int, int]) -> list[str]:
    configs_dir = Path(_tmp_state) / f"configs-{label}"
    configs_dir.mkdir(parents=True, exist_ok=True)
    (configs_dir / "qwen3-8b-bf16.yaml").write_text(QWEN_CONFIG, encoding="utf-8")
    (configs_dir / "tiny-llama-smoke.yaml").write_text(TINY_CONFIG, encoding="utf-8")

    app = VelaApp(
        configs_dir=configs_dir,
        target_client=ScreenshotClient(configs_dir),
        target_name="gpu-node",
        target_ping_interval_seconds=None,
    )
    saved: list[str] = []
    async with app.run_test(size=size) as pilot:
        await _settle(pilot)
        saved.append(_save(app, out_dir, label, "dashboard"))

        await pilot.press("c")
        await _settle(pilot)
        saved.append(_save(app, out_dir, label, "config-picker"))
        await pilot.press("escape")
        await _settle(pilot)

        await pilot.press("n")
        await _settle(pilot)
        saved.append(_save(app, out_dir, label, "new-deployment"))
        await pilot.press("escape")
        await _settle(pilot)

        await pilot.press("b")
        await _settle(pilot)
        saved.append(_save(app, out_dir, label, "build-manager"))
        await pilot.press("escape")
        await _settle(pilot)

        await pilot.press("m")
        await _settle(pilot)
        saved.append(_save(app, out_dir, label, "model-manager"))
        await pilot.press("escape")
        await _settle(pilot)

        await pilot.press("t")
        await _settle(pilot)
        saved.append(_save(app, out_dir, label, "target-manager"))
        await pilot.press("escape")
        await _settle(pilot)

        await pilot.press("?")
        await _settle(pilot)
        saved.append(_save(app, out_dir, label, "help-modal"))
        await pilot.press("escape")
        await _settle(pilot)

        await pilot.press("/")
        await _settle(pilot)
        saved.append(_save(app, out_dir, label, "log-prompt-modal"))
        await pilot.press("escape")
        await _settle(pilot)

        app.push_screen(
            ConfirmScreen(
                "Overwrite config qwen3-8b-bf16 on gpu-node?\n\nRemote path: "
                "~/.config/vela/configs/qwen3-8b-bf16.yaml",
                title="Overwrite target config",
                confirm_label="Overwrite",
                confirm_action="confirm_push_config_overwrite",
            )
        )
        await _settle(pilot)
        saved.append(_save(app, out_dir, label, "confirm-modal"))
        await pilot.press("escape")
        await _settle(pilot)

        app.push_screen(TargetEditScreen())
        await _settle(pilot)
        saved.append(_save(app, out_dir, label, "target-edit-modal"))
        await pilot.press("escape")
        await _settle(pilot)

    return saved


async def _settle(pilot) -> None:
    await pilot.pause()
    await pilot.pause()
    await asyncio.sleep(0.05)
    await pilot.pause()


def _save(app: VelaApp, out_dir: Path, size_label: str, screen: str) -> str:
    name = f"{size_label}-{screen}.svg"
    app.save_screenshot(str(out_dir / name))
    return name


def _write_verdict(out_dir: Path, saved: list[str]) -> Path:
    generated = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    path = out_dir / f"{out_dir.name}-visual-qa.md"
    lines = [
        "# Vela Visual QA",
        "",
        f"- Generated: `{generated}`",
        f"- Figma reference: {FIGMA_REFERENCE}",
        "- Method: Textual headless SVG capture with placeholder-only state.",
        (
            "- Verdict: layout/anatomy artifact; Textual SVG export may quantize "
            "the truecolor palette, so use an interactive terminal or "
            "browser-backed capture for final color sign-off."
        ),
        "",
        "## Captured Screens",
        "",
    ]
    lines.extend(f"- `{name}`" for name in saved)
    lines.extend(
        [
            "",
            "## Review Notes",
            "",
            "- Compare dashboard shell, manager screens, New Deployment wizard, and small",
            "  modals against the Canonical v2 Figma node map.",
            "- Record any spacing, color-token, or modal anatomy drift here before tagging.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


async def _main(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    for label, size in SIZES.items():
        saved.extend(await _capture_size(out_dir, label, size))
    verdict = _write_verdict(out_dir, saved)
    for name in saved:
        print(f"ok  {out_dir / name}")
    print(f"ok  {verdict}")


def main() -> None:
    if len(sys.argv) > 1:
        out_dir = Path(sys.argv[1])
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        out_dir = Path("artifacts") / "visual-qa" / stamp
    asyncio.run(_main(out_dir))


if __name__ == "__main__":
    main()
