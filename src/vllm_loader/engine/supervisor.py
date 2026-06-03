from __future__ import annotations

import argparse
import fcntl
import json
import os
import struct
import subprocess
import sys
import termios
import threading
import time
from copy import deepcopy
from pathlib import Path

import psutil

from vllm_loader.engine.log_sink import LogSink, is_pty_eof
from vllm_loader.engine.redaction import scrub_text as scrub_secret_text
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
    master_fd, slave_fd = os.openpty()
    _set_winsize(slave_fd, rows=40, cols=200)
    try:
        try:
            child = subprocess.Popen(
                argv,
                cwd=cwd,
                env={**os.environ, **env},
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,
                close_fds=True,
            )
        except Exception:
            os.close(master_fd)
            raise
    finally:
        os.close(slave_fd)
    sink, durable_log_available = _open_log_sink(log_path, secrets)
    manifest_path = Path(payload["manifest_path"]) if payload is not None else None
    manifest: Manifest | None = None
    rotate_bytes = _log_rotate_bytes(payload)
    rotation_index = 0

    def drain() -> None:
        nonlocal rotation_index
        try:
            while True:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError as exc:
                    if is_pty_eof(exc):
                        break
                    raise
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
        finally:
            try:
                os.close(master_fd)
            except OSError:
                pass
        try:
            sink.close()
        except Exception:
            pass

    thread = threading.Thread(target=drain, daemon=True)
    thread.start()
    if payload is not None and durable_log_available:
        try:
            manifest = _write_run_artifacts(payload, child, log_path)
        except Exception:
            manifest = None
    returncode = child.wait()
    thread.join(timeout=5)
    if payload is not None:
        _write_exit_status(payload, returncode)
    return returncode


def _set_winsize(fd: int, *, rows: int, cols: int) -> None:
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except Exception:
        pass


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
    child_create_time = _safe_create_time(child_proc, fallback=0.0)
    child_pgid = _safe_pgid(child.pid, fallback=child.pid)
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
        pgid=child_pgid,
        process_create_time=child_create_time,
        procfs_starttime=procfs_starttime_from_pid(child.pid),
        supervisor_pid=os.getpid(),
        supervisor_create_time=_safe_create_time(supervisor_proc, fallback=0.0),
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


def _write_exit_status(payload: dict, returncode: int) -> None:
    path_value = payload.get("exit_status_path")
    if path_value is None:
        return
    path = Path(path_value)
    body = json.dumps(
        {
            "run_id": payload.get("run_id"),
            "returncode": returncode,
            "exited_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        indent=2,
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(body)
    except OSError:
        return


def _safe_exe(proc: psutil.Process, *, fallback: str) -> str:
    try:
        return proc.exe()
    except Exception:
        return fallback


def _safe_create_time(proc: psutil.Process, *, fallback: float) -> float:
    try:
        return proc.create_time()
    except Exception:
        return fallback


def _safe_pgid(pid: int, *, fallback: int) -> int:
    try:
        return os.getpgid(pid)
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
    return scrub_secret_text(text, secrets=secrets)


if __name__ == "__main__":
    main(sys.argv[1:])
