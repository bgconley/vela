#!/usr/bin/env python3
"""Publish the validated UI walkthrough as stable documentation assets.

The source screenshots are the immutable, checksummed Oxcart human-workflow
evidence captured from runtime commit ``cd9569a``.  This script gives the
selected frames stable, reader-facing names under ``docs/img/tutorial`` and
writes a provenance manifest.  It never modifies the source evidence.

Run from the repository root::

    python3 scripts/sync_docs_screenshots.py
    python3 scripts/sync_docs_screenshots.py --check
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_RELATIVE = Path(
    "artifacts/human-workflow-validation/2026-07-13/"
    "oxcart-cd9569a5643a-20260713T121208Z/screenshots"
)
SOURCE_ROOT = REPO_ROOT / EVIDENCE_RELATIVE
DEST_ROOT = REPO_ROOT / "docs/img/tutorial"
MANIFEST_PATH = DEST_ROOT / "manifest.json"
SOURCE_RUNTIME_COMMIT = "cd9569a5643a41b53e0ee4d133b0b6d2d616d9d7"

# destination name, source path relative to SOURCE_ROOT, reader-facing purpose
ASSETS: tuple[tuple[str, str, str], ...] = (
    ("dashboard-empty.jpg", "01-dashboard-empty.jpg", "Honest first-run empty dashboard"),
    ("target-selected.jpg", "02-target-oxcart.jpg", "Target step connected to local Oxcart"),
    ("recipe-selected.jpg", "03-recipe-fields.jpg", "Recipe selection and derived name"),
    ("custom-values-restored.jpg", "03a-custom-restored.jpg", "Custom recipe round-trip"),
    ("runtime-pinned-image.jpg", "04-runtime-digest.jpg", "Immutable Docker image selection"),
    ("model-pin-required.jpg", "05-model-exact-pin-required.jpg", "Exact model pin guidance"),
    ("model-pin-full-sha.jpg", "06-pin-full-sha.jpg", "Model pin with full resolved commit"),
    ("model-pin-applied.jpg", "07-pin-cached-return.jpg", "Pinned cached model returned to wizard"),
    ("review-summary.jpg", "08-review-top.jpg", "Review identity and deployment summary"),
    ("review-provenance.jpg", "09-review-provenance-command.jpg", "Per-field provenance"),
    ("review-flags.jpg", "10-review-flags-command.jpg", "Resolved modeled and raw flags"),
    ("review-redacted.jpg", "11-review-redacted-command.jpg", "Redacted environment and command"),
    (
        "review-command-environment.jpg",
        "13-review-resolved-command-save.jpg",
        "Resolved-command environment prefix and separate save controls",
    ),
    ("profile-saved-idle.jpg", "14-saved-idle-not-launched.jpg", "Saved profile without launch"),
    (
        "profile-cold-reload.jpg",
        "15-cold-reload-selected.jpg",
        "Profile restored after cold restart",
    ),
    ("run-starting.jpg", "16-run1-starting.jpg", "Run entering startup phases"),
    ("run-loading.jpg", "18-run1-loading-weights.jpg", "Live model-weight loading progress"),
    ("run-ready.jpg", "21-run1-ready.jpg", "READY deployment and endpoint"),
    ("run-stopped.jpg", "25a-run1-stop-check.jpg", "Operator stop and terminal state"),
    (
        "target-manager.jpg",
        "matrix/02-target-manager-current-sha.jpg",
        "Target Manager details and capabilities",
    ),
    (
        "target-connecting.jpg",
        "matrix/04-dead-target-connecting.jpg",
        "Non-blocking dead-target connection attempt",
    ),
    (
        "target-manager-responsive.jpg",
        "matrix/05-dead-connect-target-manager-responsive.jpg",
        "Target Manager responsive during connection",
    ),
    (
        "target-returned-local.jpg",
        "matrix/06a-returned-local-settled.jpg",
        "Returned safely to local target",
    ),
    (
        "model-manager.jpg",
        "matrix/07-model-manager-current-sha.jpg",
        "Model Manager pin, cache, and usage detail",
    ),
    (
        "model-use-once.jpg",
        "matrix/08-use-once-applied.jpg",
        "Transient Use once model override",
    ),
    (
        "build-manager-empty.jpg",
        "matrix/09-build-manager-current-sha.jpg",
        "Honest empty Build Manager",
    ),
    (
        "flag-manager.jpg",
        "matrix/10-flag-manager-current-sha.jpg",
        "Flag Manager provenance and categories",
    ),
    (
        "config-picker.jpg",
        "matrix/11-config-picker-current-sha.jpg",
        "Config Picker with selected profile detail",
    ),
    (
        "clone-command.jpg",
        "matrix/13-clone-command-disclosure.jpg",
        "Clone identity regeneration disclosure",
    ),
    (
        "clone-result.jpg",
        "matrix/14-clone-result-current-sha.jpg",
        "Cloned deployment result",
    ),
    (
        "save-conflict.jpg",
        "matrix/18-save-conflict-recovery.jpg",
        "Recoverable duplicate-name validation",
    ),
    (
        "save-conflict-recovered.jpg",
        "matrix/20-conflict-recovery-saved.jpg",
        "Renamed deployment saved successfully",
    ),
    ("wrong-host-failed.jpg", "36-wrong-host-failed.jpg", "Wrong-host preflight failure"),
    (
        "wrong-host-help-responsive.jpg",
        "37-wrong-host-help-responsive.jpg",
        "Help remains responsive after failure",
    ),
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def expected_manifest() -> dict[str, object]:
    assets: list[dict[str, object]] = []
    for destination, source_name, purpose in ASSETS:
        source = SOURCE_ROOT / source_name
        if not source.is_file():
            raise FileNotFoundError(f"missing source screenshot: {source}")
        assets.append(
            {
                "file": destination,
                "source": str(EVIDENCE_RELATIVE / source_name),
                "purpose": purpose,
                "sha256": _sha256(source),
                "bytes": source.stat().st_size,
            }
        )
    return {
        "version": 1,
        "evidence_session_started_at": "2026-07-13T12:12:08Z",
        "source_runtime_commit": SOURCE_RUNTIME_COMMIT,
        "source_evidence": str(EVIDENCE_RELATIVE.parent),
        "secret_scan": "passed in the source evidence manifest",
        "notes": [
            "Images are byte-for-byte copies of checksummed live-workflow evidence.",
            "The example is host-specific; readers must adapt target, paths, ports, and model.",
            "Known duplicate or misleading stop frames were intentionally excluded.",
        ],
        "assets": assets,
    }


def sync() -> None:
    manifest = expected_manifest()
    DEST_ROOT.mkdir(parents=True, exist_ok=True)
    expected_names = {destination for destination, _, _ in ASSETS}
    for path in DEST_ROOT.glob("*.jpg"):
        if path.name not in expected_names:
            path.unlink()
    for destination, source_name, _purpose in ASSETS:
        shutil.copy2(SOURCE_ROOT / source_name, DEST_ROOT / destination)
    MANIFEST_PATH.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def check() -> list[str]:
    errors: list[str] = []
    try:
        expected = expected_manifest()
    except FileNotFoundError as exc:
        return [str(exc)]
    if not MANIFEST_PATH.is_file():
        errors.append(f"missing manifest: {MANIFEST_PATH}")
    else:
        try:
            actual = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"unreadable manifest: {exc}")
        else:
            if actual != expected:
                errors.append("manifest differs from the selected source evidence")
    expected_names = {destination for destination, _, _ in ASSETS}
    actual_names = {path.name for path in DEST_ROOT.glob("*.jpg")}
    if actual_names != expected_names:
        errors.append(
            "published screenshot names differ: "
            f"missing={sorted(expected_names - actual_names)!r} "
            f"unexpected={sorted(actual_names - expected_names)!r}"
        )
    for destination, source_name, _purpose in ASSETS:
        published = DEST_ROOT / destination
        source = SOURCE_ROOT / source_name
        if published.is_file() and published.read_bytes() != source.read_bytes():
            errors.append(f"published screenshot drifted from source: {destination}")
    return errors


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Publish or verify Vela's tutorial screenshot corpus.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify committed assets and manifest without changing files.",
    )
    args = parser.parse_args(argv)
    if args.check:
        errors = check()
        if errors:
            raise SystemExit("\n".join(errors))
        print(f"documentation screenshots OK: {len(ASSETS)} assets")
        return
    sync()
    print(f"published {len(ASSETS)} screenshots to {DEST_ROOT}")


if __name__ == "__main__":
    main()
