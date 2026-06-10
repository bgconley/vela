"""Retention for flat-file run records under runs directories.

A run record is the group of files sharing a run-id stem:
``<run_id>.json`` (sidecar), ``<run_id>.manifest.json``, ``<run_id>.run.log``
plus rotations, ``<run_id>.events.ndjson``, and ``<run_id>.exit-status``.

Safety model:

- a group whose sidecar identity-verifies as a live process/container is
  never touched;
- ``.exit-status`` marks a clean terminal run, so it is pruned without
  paying for identity verification;
- only groups whose newest file is older than the age threshold are
  eligible, so anything still being written is protected by mtime;
- the newest ``keep_recent`` eligible groups per directory are retained as
  debugging history.
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from vela.engine.sidecar import verify_sidecar_from_system

_LOG_NAME = re.compile(r"^(?P<stem>[^.]+)\.run\.log(\.\d+)?$")
_FIXED_SUFFIXES = (".manifest.json", ".events.ndjson", ".exit-status", ".json")


@dataclass
class PruneResult:
    scanned_groups: int = 0
    pruned_run_ids: list[str] = field(default_factory=list)
    pruned_files: int = 0
    reclaimed_bytes: int = 0
    kept_recent: int = 0
    skipped_active: int = 0
    skipped_fresh: int = 0
    dry_run: bool = False


def _record_stem(name: str) -> str | None:
    match = _LOG_NAME.match(name)
    if match:
        return match.group("stem")
    for suffix in _FIXED_SUFFIXES:
        if name.endswith(suffix):
            stem = name[: -len(suffix)]
            if stem and "." not in stem:
                return stem
    return None


def prune_run_records(
    runs_dirs: Sequence[Path | str],
    *,
    keep_recent: int = 20,
    older_than_seconds: float = 7 * 86_400.0,
    dry_run: bool = False,
    now: float | None = None,
) -> PruneResult:
    moment = time.time() if now is None else now
    result = PruneResult(dry_run=dry_run)
    seen: set[Path] = set()
    for item in runs_dirs:
        runs_dir = Path(item)
        if runs_dir in seen or not runs_dir.is_dir():
            continue
        seen.add(runs_dir)
        _prune_dir(
            runs_dir,
            result,
            keep_recent=keep_recent,
            older_than_seconds=older_than_seconds,
            dry_run=dry_run,
            moment=moment,
        )
    return result


def _prune_dir(
    runs_dir: Path,
    result: PruneResult,
    *,
    keep_recent: int,
    older_than_seconds: float,
    dry_run: bool,
    moment: float,
) -> None:
    groups: dict[str, list[Path]] = {}
    for entry in runs_dir.iterdir():
        if not entry.is_file():
            continue
        stem = _record_stem(entry.name)
        if stem is not None:
            groups.setdefault(stem, []).append(entry)

    eligible: list[tuple[float, str, list[Path]]] = []
    for run_id, files in sorted(groups.items()):
        result.scanned_groups += 1
        try:
            newest = max(path.stat().st_mtime for path in files)
        except OSError:
            continue
        terminal = (runs_dir / f"{run_id}.exit-status").exists()
        sidecar_path = runs_dir / f"{run_id}.json"
        if not terminal and sidecar_path.exists():
            try:
                if verify_sidecar_from_system(sidecar_path):
                    result.skipped_active += 1
                    continue
            except Exception:
                pass
        if moment - newest <= older_than_seconds:
            result.skipped_fresh += 1
            continue
        eligible.append((newest, run_id, files))

    eligible.sort(key=lambda entry: entry[0], reverse=True)
    kept = eligible[:keep_recent] if keep_recent > 0 else []
    result.kept_recent += len(kept)
    for _, run_id, files in eligible[len(kept) :]:
        for path in files:
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            if not dry_run:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    continue
            result.pruned_files += 1
            result.reclaimed_bytes += size
        result.pruned_run_ids.append(run_id)
