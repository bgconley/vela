from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from copy import deepcopy
from pathlib import Path

import psutil

from vllm_loader.engine.log_sink import LogSink
from vllm_loader.engine.sidecar import Manifest, Sidecar, command_hash, procfs_starttime_from_pid

DEFAULT_LOG_ROTATE_BYTES = 256 * 1024 * 1024


class _DrainOnlySink:
    def __init__(self, path: Path) -> None:
        self.path = path

    def feed(self, _chunk: bytes) -> None:
        return

    def close(self) -> None:
        return

    def rotate_to(self, path: Path) -> None:
        self.path = path


def run_supervisor(
    argv: list[str],
    env: dict[str, str],
    cwd: str | None,
    log_path: Path,
    secrets: list[str],
    *,
    payload: dict | None = None,
) -> int:
    child = subprocess.Popen(
        argv,
        cwd=cwd,
        env={**os.environ, **env},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    sink, durable_log_available = _open_log_sink(log_path, secrets)
    manifest_path = Path(payload["manifest_path"]) if payload is not None else None
    manifest: Manifest | None = None
    if payload is not None and durable_log_available:
        try:
            manifest = _write_run_artifacts(payload, child, log_path)
        except Exception:
            manifest = None
    rotate_bytes = _log_rotate_bytes(payload)
    rotation_index = 0

    def drain() -> None:
        nonlocal rotation_index
        assert child.stdout is not None
        fd = child.stdout.fileno()
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            try:
                sink.feed(chunk)
            except Exception:
                # The supervisor must keep draining even if persistence fails.
                continue
            rotation_index = _rotate_log_if_needed(
                sink,
                log_path,
                manifest_path,
                manifest,
                rotate_bytes,
                rotation_index,
            )
        try:
            sink.close()
        except Exception:
            pass

    thread = threading.Thread(target=drain, daemon=True)
    thread.start()
    returncode = child.wait()
    thread.join(timeout=5)
    return returncode


def _open_log_sink(log_path: Path, secrets: list[str]) -> tuple[LogSink | _DrainOnlySink, bool]:
    try:
        return LogSink(log_path, secrets=secrets), True
    except OSError:
        return _DrainOnlySink(log_path), False


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", required=True)
    args = parser.parse_args(argv)
    payload_path = Path(args.payload)
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    try:
        payload_path.unlink()
    except FileNotFoundError:
        pass
    raise SystemExit(
        run_supervisor(
            payload["argv"],
            payload.get("env", {}),
            payload.get("cwd"),
            Path(payload["log_path"]),
            payload.get("secrets", []),
            payload=payload,
        )
    )


def _write_run_artifacts(
    payload: dict, child: subprocess.Popen[bytes], log_path: Path
) -> Manifest:
    manifest_path = Path(payload["manifest_path"])
    sidecar_path = Path(payload["sidecar_path"])
    manifest = Manifest.from_active_log(log_path)
    manifest.write_atomic(manifest_path)

    child_proc = psutil.Process(child.pid)
    supervisor_proc = psutil.Process(os.getpid())
    secrets = [secret for secret in payload.get("secrets", []) if secret]
    actual_cmdline = _wait_for_actual_cmdline(child_proc, payload["argv"])
    sidecar = Sidecar(
        run_id=payload["run_id"],
        config_name=payload["config_name"],
        config_snapshot=payload.get("config_snapshot"),
        command_argv=_scrub_argv_for_artifact(actual_cmdline, secrets),
        command_hash=command_hash(actual_cmdline),
        vllm_version=payload.get("vllm_version"),
        vllm_version_profile=payload.get("vllm_version_profile"),
        executable=_safe_exe(child_proc, fallback=payload["argv"][0]),
        cwd=payload.get("cwd") or os.getcwd(),
        pid=child.pid,
        pgid=os.getpgid(child.pid),
        process_create_time=child_proc.create_time(),
        procfs_starttime=procfs_starttime_from_pid(child.pid),
        supervisor_pid=os.getpid(),
        supervisor_create_time=supervisor_proc.create_time(),
        supervisor_procfs_starttime=procfs_starttime_from_pid(os.getpid()),
        supervisor_executable=_safe_exe(supervisor_proc, fallback=sys.executable),
        host=payload["host"],
        port=int(payload["port"]),
        served_model_names=payload.get("served_model_names", []),
        exposure=payload["exposure"],
        launch_mode=payload["launch_mode"],
        manifest_path=str(manifest_path),
    )
    sidecar.write_atomic(sidecar_path)
    return manifest


def _log_rotate_bytes(payload: dict | None) -> int:
    if payload is None:
        return DEFAULT_LOG_ROTATE_BYTES
    value = payload.get("log_rotate_bytes", DEFAULT_LOG_ROTATE_BYTES)
    if value is None:
        return 0
    return max(0, int(value))


def _rotate_log_if_needed(
    sink: LogSink,
    original_log_path: Path,
    manifest_path: Path | None,
    manifest: Manifest | None,
    rotate_bytes: int,
    rotation_index: int,
) -> int:
    if rotate_bytes <= 0 or manifest_path is None or manifest is None:
        return rotation_index
    try:
        if sink.path.stat().st_size <= rotate_bytes:
            return rotation_index
        next_index = rotation_index + 1
        next_path = _rotated_log_path(original_log_path, next_index)
        candidate = deepcopy(manifest)
        _prepare_private_log(next_path)
        candidate.rotate_to(next_path)
        candidate.write_atomic(manifest_path)
        sink.rotate_to(next_path)
        manifest.active_log = candidate.active_log
        manifest.rotated = candidate.rotated
        return next_index
    except Exception:
        # Keep draining child pipes even if rotation or manifest persistence fails.
        return rotation_index


def _rotated_log_path(original_log_path: Path, rotation_index: int) -> Path:
    return original_log_path.with_name(f"{original_log_path.name}.{rotation_index}")


def _prepare_private_log(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


def _safe_exe(proc: psutil.Process, *, fallback: str) -> str:
    try:
        return proc.exe()
    except Exception:
        return fallback


def _wait_for_actual_cmdline(proc: psutil.Process, fallback: list[str]) -> list[str]:
    deadline = time.monotonic() + 1.0
    last: list[str] = []
    while time.monotonic() < deadline:
        try:
            current = proc.cmdline()
        except Exception:
            current = []
        if current:
            last = current
            if current[0] != "/usr/bin/env":
                return current
        time.sleep(0.02)
    return last or fallback


def _scrub_argv_for_artifact(argv: list[str], secrets: list[str]) -> list[str]:
    return [_scrub_text(item, secrets) for item in argv]


def _scrub_text(text: str, secrets: list[str]) -> str:
    scrubbed = text
    for secret in secrets:
        scrubbed = scrubbed.replace(secret, "••••")
    return scrubbed


if __name__ == "__main__":
    main(sys.argv[1:])
