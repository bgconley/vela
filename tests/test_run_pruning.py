"""Run-record retention: prune terminal/stale run artifacts, never live runs.

Mac-safe (no GPU/vLLM). Groups are fabricated flat-file run records in tmp
dirs; identity verification is monkeypatched so no real processes are needed.
"""

import json
import os
import time
from pathlib import Path

from typer.testing import CliRunner

import vela.cli as cli_module
from vela.agent import daemon as daemon_module
from vela.agent.local import LocalAgent
from vela.config.schema import default_run_artifacts_dir
from vela.engine import run_pruning
from vela.engine.run_pruning import PruneResult, prune_run_records
from vela.engine.sidecar import TrackedProcessMismatch

DAY = 86_400.0
NOW = 1_900_000_000.0


def _write_group(
    runs_dir: Path,
    run_id: str,
    *,
    exit_status: bool = True,
    sidecar: bool = True,
    rotated_log: bool = False,
    age_seconds: float = 30 * DAY,
    now: float = NOW,
) -> list[Path]:
    runs_dir.mkdir(parents=True, exist_ok=True)
    entries: list[tuple[Path, str]] = []
    if sidecar:
        content = json.dumps({"run_id": run_id, "launch_mode": "detached"})
        entries.append((runs_dir / f"{run_id}.json", content))
    entries.append((runs_dir / f"{run_id}.manifest.json", "{}"))
    entries.append((runs_dir / f"{run_id}.run.log", "log line\n"))
    entries.append((runs_dir / f"{run_id}.events.ndjson", "{}\n"))
    if rotated_log:
        entries.append((runs_dir / f"{run_id}.run.log.1", "older log\n"))
    if exit_status:
        entries.append((runs_dir / f"{run_id}.exit-status", "0\n"))
    stamp = now - age_seconds
    paths: list[Path] = []
    for path, content in entries:
        path.write_text(content, encoding="utf-8")
        os.utime(path, (stamp, stamp))
        paths.append(path)
    return paths


def test_prune_removes_old_terminal_run_group_without_verification(tmp_path, monkeypatch):
    def _must_not_verify(path):
        raise AssertionError("terminal runs (exit-status present) must not be identity-verified")

    monkeypatch.setattr(run_pruning, "verify_sidecar_from_system", _must_not_verify)
    runs = tmp_path / "runs"
    paths = _write_group(runs, "aaa111", exit_status=True, age_seconds=30 * DAY)

    result = prune_run_records([runs], keep_recent=0, older_than_seconds=7 * DAY, now=NOW)

    assert result.pruned_run_ids == ["aaa111"]
    assert result.reclaimed_bytes > 0
    assert all(not path.exists() for path in paths)


def test_prune_keeps_newest_terminal_runs_within_keep_recent(tmp_path):
    runs = tmp_path / "runs"
    _write_group(runs, "old30", age_seconds=30 * DAY)
    _write_group(runs, "old20", age_seconds=20 * DAY)
    _write_group(runs, "old10", age_seconds=10 * DAY)

    result = prune_run_records([runs], keep_recent=2, older_than_seconds=7 * DAY, now=NOW)

    assert result.pruned_run_ids == ["old30"]
    assert not (runs / "old30.json").exists()
    assert (runs / "old20.json").exists()
    assert (runs / "old10.json").exists()
    assert result.kept_recent == 2


def test_prune_respects_age_threshold(tmp_path):
    runs = tmp_path / "runs"
    paths = _write_group(runs, "fresh1", age_seconds=2 * DAY)

    result = prune_run_records([runs], keep_recent=0, older_than_seconds=7 * DAY, now=NOW)

    assert result.pruned_run_ids == []
    assert result.skipped_fresh == 1
    assert all(path.exists() for path in paths)


def test_prune_never_touches_verified_alive_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(run_pruning, "verify_sidecar_from_system", lambda path: True)
    runs = tmp_path / "runs"
    paths = _write_group(runs, "live01", exit_status=False, age_seconds=60 * DAY)

    result = prune_run_records([runs], keep_recent=0, older_than_seconds=7 * DAY, now=NOW)

    assert result.pruned_run_ids == []
    assert result.skipped_active == 1
    assert all(path.exists() for path in paths)


def test_prune_removes_stale_debris_when_identity_verification_fails(tmp_path, monkeypatch):
    def _mismatch(path):
        raise TrackedProcessMismatch("pid recycled by another process")

    monkeypatch.setattr(run_pruning, "verify_sidecar_from_system", _mismatch)
    runs = tmp_path / "runs"
    paths = _write_group(runs, "stale1", exit_status=False, age_seconds=60 * DAY)

    result = prune_run_records([runs], keep_recent=0, older_than_seconds=7 * DAY, now=NOW)

    assert result.pruned_run_ids == ["stale1"]
    assert all(not path.exists() for path in paths)


def test_prune_handles_orphan_groups_and_ignores_unrelated_files(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir(parents=True)
    orphan = runs / "bbb222.run.log"
    orphan.write_text("leftover\n", encoding="utf-8")
    stamp = NOW - 30 * DAY
    os.utime(orphan, (stamp, stamp))
    notes = runs / "notes.txt"
    notes.write_text("operator scratch file\n", encoding="utf-8")
    os.utime(notes, (stamp, stamp))

    result = prune_run_records([runs], keep_recent=0, older_than_seconds=7 * DAY, now=NOW)

    assert result.pruned_run_ids == ["bbb222"]
    assert not orphan.exists()
    assert notes.exists()


def test_prune_groups_rotated_logs_with_their_run(tmp_path):
    runs = tmp_path / "runs"
    paths = _write_group(runs, "rot001", rotated_log=True, age_seconds=30 * DAY)

    result = prune_run_records([runs], keep_recent=0, older_than_seconds=7 * DAY, now=NOW)

    assert result.pruned_run_ids == ["rot001"]
    assert all(not path.exists() for path in paths)


def test_dry_run_reports_without_deleting(tmp_path):
    runs = tmp_path / "runs"
    paths = _write_group(runs, "ddd444", age_seconds=30 * DAY)

    result = prune_run_records(
        [runs], keep_recent=0, older_than_seconds=7 * DAY, dry_run=True, now=NOW
    )

    assert result.pruned_run_ids == ["ddd444"]
    assert result.dry_run is True
    assert all(path.exists() for path in paths)


def test_cli_runs_prune_dry_run_keeps_files_and_reports(tmp_path):
    runs = tmp_path / "runs"
    _write_group(runs, "ccc333", age_seconds=30 * DAY, now=time.time())

    result = CliRunner().invoke(
        cli_module.app,
        [
            "runs",
            "prune",
            "--runs-dir",
            str(runs),
            "--keep",
            "0",
            "--older-than-days",
            "7",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "1 run record" in result.output
    assert (runs / "ccc333.json").exists()


def test_cli_runs_prune_deletes_for_real(tmp_path):
    runs = tmp_path / "runs"
    _write_group(runs, "eee555", age_seconds=30 * DAY, now=time.time())

    result = CliRunner().invoke(
        cli_module.app,
        ["runs", "prune", "--runs-dir", str(runs), "--keep", "0", "--older-than-days", "7"],
    )

    assert result.exit_code == 0, result.output
    assert "1 run record" in result.output
    assert not (runs / "eee555.json").exists()


def test_daemon_auto_prune_uses_agent_known_dirs_with_conservative_defaults(monkeypatch):
    calls: dict[str, object] = {}

    def _fake_prune(dirs, *, keep_recent, older_than_seconds, dry_run=False, now=None):
        calls["dirs"] = [Path(item) for item in dirs]
        calls["keep_recent"] = keep_recent
        calls["older_than_seconds"] = older_than_seconds
        return PruneResult()

    monkeypatch.setattr(daemon_module, "prune_run_records", _fake_prune)
    daemon_module.auto_prune_run_records(LocalAgent())

    assert default_run_artifacts_dir() in calls["dirs"]
    assert calls["keep_recent"] >= 20
    assert calls["older_than_seconds"] >= 7 * DAY


def test_daemon_auto_prune_is_non_fatal(monkeypatch):
    def _explode(*args, **kwargs):
        raise RuntimeError("disk unavailable")

    monkeypatch.setattr(daemon_module, "prune_run_records", _explode)
    daemon_module.auto_prune_run_records(LocalAgent())
