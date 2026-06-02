from __future__ import annotations

import asyncio
import errno
import fcntl
import json
import os
import shutil
import signal
import struct
import subprocess
import sys
import termios
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vllm_loader.config.schema import ModelConfig
from vllm_loader.engine.command_builder import CommandBuildResult
from vllm_loader.engine.log_sink import LogRecord, LogSink, is_pty_eof


@dataclass
class AttachedProcess:
    proc: subprocess.Popen[bytes]
    master_fd: int
    log_sink: LogSink
    log_sink_failed: bool = False

    async def read_loop(self) -> int | None:
        try:
            while True:
                try:
                    chunk = await asyncio.to_thread(os.read, self.master_fd, 4096)
                except OSError as exc:
                    if is_pty_eof(exc):
                        break
                    raise
                if not chunk:
                    break
                if not self.log_sink_failed:
                    try:
                        self.log_sink.feed(chunk)
                    except Exception:
                        self.log_sink_failed = True
        finally:
            try:
                self.log_sink.close()
            except Exception:
                pass
            try:
                os.close(self.master_fd)
            except OSError:
                pass
        return await asyncio.to_thread(self.proc.wait)

    def stop(self, *, interrupt_timeout: float = 5, terminate_timeout: float = 5) -> None:
        _signal_group_with_escalation(self.proc, interrupt_timeout, terminate_timeout)

    def kill(self) -> None:
        _kill_group(self.proc, signal.SIGKILL)


@dataclass(frozen=True)
class DetachedLaunch:
    run_id: str
    supervisor_pid: int
    sidecar_path: Path
    manifest_path: Path
    log_path: Path


def start_attached(
    build: CommandBuildResult,
    *,
    log_path: Path,
    secrets: list[str],
    emit: Callable[[LogRecord], None] | None = None,
) -> AttachedProcess:
    master_fd, slave_fd = os.openpty()
    _set_winsize(slave_fd, rows=40, cols=200)
    env = {**os.environ, **build.env}
    try:
        try:
            proc = subprocess.Popen(
                build.argv,
                cwd=build.cwd,
                env=env,
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
    return AttachedProcess(
        proc=proc, master_fd=master_fd, log_sink=LogSink(log_path, secrets=secrets, emit=emit)
    )


def start_detached(
    cfg: ModelConfig,
    build: CommandBuildResult,
    *,
    secrets: list[str],
    vllm_version: str | None = None,
    vllm_version_profile: str | None = None,
    wait_timeout: float = 5.0,
    log_rotate_bytes: int | None = None,
) -> DetachedLaunch:
    _require_executable(build.argv[0], cwd=build.cwd, env={**os.environ, **build.env})
    secret_values = [secret for secret in secrets if secret]
    run_id = uuid.uuid4().hex
    run_dir = cfg.run_artifacts_dir
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / f"{run_id}.run.log"
    manifest_path = run_dir / f"{run_id}.manifest.json"
    sidecar_path = run_dir / f"{run_id}.json"
    payload_path = run_dir / f"{run_id}.supervisor-payload.json"
    payload = {
        "argv": build.argv,
        "env": build.env,
        "cwd": str(build.cwd),
        "log_path": str(log_path),
        "manifest_path": str(manifest_path),
        "sidecar_path": str(sidecar_path),
        "secrets": secret_values,
        "run_id": run_id,
        "config_name": cfg.name,
        "config_snapshot": _scrub_config_snapshot(cfg, secrets=secret_values),
        "command_hash": _command_hash(build.argv),
        "vllm_version": vllm_version,
        "vllm_version_profile": vllm_version_profile,
        "host": cfg.server.host,
        "port": cfg.server.port,
        "served_model_names": [cfg.served_model_name] if cfg.served_model_name else [],
        "exposure": cfg.server.exposure.value,
        "launch_mode": cfg.launch.mode.value,
    }
    if log_rotate_bytes is not None:
        payload["log_rotate_bytes"] = log_rotate_bytes
    _write_secret_payload(payload_path, payload)
    proc = subprocess.Popen(
        [sys.executable, "-m", "vllm_loader.engine.supervisor", "--payload", str(payload_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    _wait_for_sidecar(sidecar_path, proc, wait_timeout)
    return DetachedLaunch(
        run_id=run_id,
        supervisor_pid=proc.pid,
        sidecar_path=sidecar_path,
        manifest_path=manifest_path,
        log_path=log_path,
    )


def _set_winsize(fd: int, *, rows: int, cols: int) -> None:
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except Exception:
        pass


def _require_executable(executable: str, *, cwd: Path, env: dict[str, str]) -> None:
    if os.sep in executable:
        path = Path(executable)
        candidate = path if path.is_absolute() else cwd / path
        if candidate.exists():
            return
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(candidate))
    if shutil.which(executable, path=env.get("PATH")) is not None:
        return
    raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), executable)


def _signal_group_with_escalation(
    proc: subprocess.Popen[bytes], interrupt_timeout: float, terminate_timeout: float
) -> None:
    if proc.poll() is not None:
        return
    _kill_group(proc, signal.SIGINT)
    try:
        proc.wait(timeout=interrupt_timeout)
        return
    except subprocess.TimeoutExpired:
        pass
    _kill_group(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=terminate_timeout)
        return
    except subprocess.TimeoutExpired:
        pass
    _kill_group(proc, signal.SIGKILL)
    try:
        proc.wait(timeout=terminate_timeout)
    except subprocess.TimeoutExpired:
        pass


def _kill_group(proc: subprocess.Popen[bytes], sig: int) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except ProcessLookupError:
        return
    except PermissionError:
        try:
            proc.send_signal(sig)
        except ProcessLookupError:
            return
    except OSError as exc:
        if exc.errno != errno.ESRCH:
            raise


def _write_secret_payload(path: Path, payload: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        json.dump(payload, file)


def _wait_for_sidecar(path: Path, proc: subprocess.Popen[bytes], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if proc.poll() is not None:
            raise RuntimeError(
                f"detached supervisor exited before writing sidecar: {proc.returncode}"
            )
        time.sleep(0.05)
    raise TimeoutError(f"detached supervisor did not write sidecar within {timeout}s")


def _scrub_config_snapshot(
    cfg: ModelConfig, *, secrets: list[str] | tuple[str, ...] = ()
) -> dict:
    snapshot = cfg.model_dump(mode="json")
    if isinstance(snapshot.get("server"), dict):
        snapshot["server"]["api_key"] = None
    env = snapshot.get("env")
    if isinstance(env, dict):
        snapshot["env"] = {key: value for key, value in env.items() if not _looks_secret_key(key)}
    return _scrub_secret_values(snapshot, tuple(secret for secret in secrets if secret))


def _scrub_secret_values(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        return _scrub_text(value, secrets)
    if isinstance(value, list):
        return [_scrub_secret_values(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: _scrub_secret_values(item, secrets) for key, item in value.items()}
    return value


def _scrub_text(text: str, secrets: tuple[str, ...]) -> str:
    scrubbed = text
    for secret in secrets:
        scrubbed = scrubbed.replace(secret, "••••")
    return scrubbed


def _looks_secret_key(key: str) -> bool:
    upper = key.upper()
    return "TOKEN" in upper or "KEY" in upper or "SECRET" in upper or "AUTH" in upper


def _command_hash(argv: list[str]) -> str:
    from vllm_loader.engine.sidecar import command_hash

    return command_hash(argv)
