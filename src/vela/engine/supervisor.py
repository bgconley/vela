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

from vela.engine.docker_runtime import (
    DEFAULT_DOCKER_COMMAND_TIMEOUT_SECONDS,
    DockerCommandError,
    DockerErrorKind,
    classify_docker_error,
    prepare_docker_image,
)
from vela.engine.log_sink import LogRecord, LogSink, is_pty_eof
from vela.engine.redaction import scrub_text as scrub_secret_text
from vela.engine.sidecar import Manifest, Sidecar, command_hash, procfs_starttime_from_pid

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


class _EventSpool:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._file = None
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            self._file = os.fdopen(fd, "w", encoding="utf-8")
        except OSError:
            self._file = None

    def emit(self, record: LogRecord) -> None:
        if self._file is None:
            return
        payload = {"kind": record.kind, "text": record.text, "level": record.level}
        if record.log_inode is not None:
            payload["log_inode"] = record.log_inode
        if record.byte_offset is not None:
            payload["byte_offset"] = record.byte_offset
        try:
            self._file.write(json.dumps(payload, separators=(",", ":")) + "\n")
            self._file.flush()
        except OSError:
            self.close()

    def close(self) -> None:
        if self._file is None:
            return
        try:
            self._file.close()
        finally:
            self._file = None


def run_supervisor(
    argv: list[str],
    env: dict[str, str],
    cwd: str | None,
    log_path: Path,
    secrets: list[str],
    *,
    payload: dict | None = None,
) -> int:
    if payload is not None and payload.get("runtime") == "docker":
        return _run_docker_supervisor(argv, env, cwd, log_path, secrets, payload=payload)
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
    event_spool = _EventSpool(_event_log_path(payload))
    sink, durable_log_available = _open_log_sink(
        log_path,
        secrets,
        emit=event_spool.emit,
    )
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
        event_spool.close()

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


def _run_docker_supervisor(
    argv: list[str],
    env: dict[str, str],
    cwd: str | None,
    log_path: Path,
    secrets: list[str],
    *,
    payload: dict,
) -> int:
    docker = payload.get("docker") if isinstance(payload.get("docker"), dict) else {}
    docker_binary = str(docker.get("binary") or argv[0])

    # Open the sink BEFORE image preparation so `docker pull` progress streams
    # through the scrubbed log/event sink (a real ~10GB vLLM image is otherwise
    # a silent multi-minute hang), and so any pre-run failure is recorded via
    # the same sink rather than a throwaway one (bug-240).
    event_spool = _EventSpool(_event_log_path(payload))
    sink, durable_log_available = _open_log_sink(
        log_path,
        secrets,
        emit=event_spool.emit,
    )
    manifest_path = Path(payload["manifest_path"])
    manifest: Manifest | None = None
    rotate_bytes = _log_rotate_bytes(payload)
    rotation_index = 0

    def _finish_failure(*, label: str, detail: str, returncode: int, kind: str) -> int:
        line = f"ERROR {label} ({kind}, exit {returncode}): {detail or 'no output'}"
        try:
            sink.feed((line + "\n").encode("utf-8", errors="replace"))
        finally:
            try:
                sink.close()
            finally:
                event_spool.close()
        _write_exit_status(payload, returncode)
        return int(returncode)

    _evict_docker_containers(
        docker_binary,
        docker.get("evict"),
        cwd=cwd,
        env=env,
    )
    try:
        image_info = prepare_docker_image(
            docker_binary,
            str(docker.get("image") or ""),
            str(docker.get("pull") or "never"),
            cwd=cwd,
            env=env,
            progress=sink.feed,
        )
        docker["image_digest"] = image_info.digest
    except DockerCommandError as exc:
        return _finish_failure(
            label="docker image preparation failed",
            detail=exc.detail,
            returncode=exc.returncode,
            kind=exc.kind.value,
        )
    except subprocess.TimeoutExpired as exc:
        # The pull path classifies its own timeout; this covers a quick prep
        # command (docker image inspect) that blows its short timeout on a
        # wedged daemon, so the supervisor records a classified failure +
        # exit-status instead of crashing untracked (bug-240).
        return _finish_failure(
            label="docker image preparation timed out",
            detail=_timeout_detail(exc),
            returncode=124,
            kind=DockerErrorKind.DAEMON_UNREACHABLE.value,
        )
    run = subprocess.run(
        argv,
        cwd=cwd,
        env={**os.environ, **env},
        capture_output=True,
        check=False,
    )
    if run.returncode != 0:
        detail = _docker_result_text(run)
        return _finish_failure(
            label="docker run failed",
            detail=detail,
            returncode=int(run.returncode),
            kind=classify_docker_error(detail).value,
        )
    container_id = _container_id_from_run_stdout(run.stdout)
    if not container_id:
        return _finish_failure(
            label="docker run produced no container id",
            detail="",
            returncode=1,
            kind=DockerErrorKind.OCI_RUNTIME_ERROR.value,
        )

    if durable_log_available:
        try:
            manifest = _write_docker_run_artifacts(payload, container_id, log_path)
        except Exception:
            manifest = None
    if manifest is None:
        # Without a sidecar/manifest the controller can never track, reattach,
        # or stop this container; leaving it running would strand an orphaned
        # GPU container. Stop+remove it and surface the failure rather than
        # draining an untrackable run.
        _evict_docker_containers(docker_binary, [container_id], cwd=cwd, env=env)
        try:
            sink.feed(
                b"ERROR run artifacts unavailable; container stopped "
                b"(io-error, exit 1): could not persist sidecar/manifest\n"
            )
        finally:
            try:
                sink.close()
            finally:
                event_spool.close()
        _write_exit_status(payload, 1)
        return 1

    logs = subprocess.Popen(
        [docker_binary, "logs", "-f", container_id],
        cwd=cwd,
        env={**os.environ, **env},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        close_fds=True,
    )

    def drain_logs() -> None:
        nonlocal rotation_index
        try:
            assert logs.stdout is not None
            while True:
                chunk = logs.stdout.read(4096)
                if not chunk:
                    break
                try:
                    sink.feed(chunk)
                except Exception:
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
                sink.close()
            except Exception:
                pass
            event_spool.close()

    thread = threading.Thread(target=drain_logs, daemon=True)
    thread.start()
    wait = subprocess.run(
        [docker_binary, "wait", container_id],
        cwd=cwd,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        logs.wait(timeout=5)
    except subprocess.TimeoutExpired:
        logs.terminate()
        logs.wait(timeout=5)
    thread.join(timeout=5)
    returncode = _docker_wait_returncode(wait)
    _write_exit_status(payload, returncode)
    return returncode


def _timeout_detail(exc: subprocess.TimeoutExpired) -> str:
    cmd = exc.cmd
    if isinstance(cmd, (list, tuple)):
        rendered = " ".join(str(part) for part in cmd)
    else:
        rendered = str(cmd)
    return f"{rendered} timed out after {exc.timeout}s"


def _docker_result_text(result: subprocess.CompletedProcess[bytes]) -> str:
    data = b""
    if result.stderr:
        data += result.stderr
    if result.stdout:
        if data:
            data += b"\n"
        data += result.stdout
    return data.decode("utf-8", errors="replace").strip()


def _evict_docker_containers(
    docker_binary: str,
    names: object,
    *,
    cwd: str | None,
    env: dict[str, str],
) -> None:
    if not isinstance(names, list):
        return
    seen: set[str] = set()
    effective_env = {**os.environ, **env}
    for raw_name in names:
        if not isinstance(raw_name, str):
            continue
        name = raw_name.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        for action in ("stop", "rm"):
            try:
                subprocess.run(
                    [docker_binary, action, name],
                    cwd=cwd,
                    env=effective_env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    # 5.1 per-command bound: a wedged docker daemon must not hang the
                    # supervisor. Eviction is best-effort, so a timeout is swallowed.
                    timeout=DEFAULT_DOCKER_COMMAND_TIMEOUT_SECONDS,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue


def _set_winsize(fd: int, *, rows: int, cols: int) -> None:
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except Exception:
        pass


def _event_log_path(payload: dict | None) -> Path | None:
    if payload is None:
        return None
    value = payload.get("event_log_path")
    if value is None:
        return None
    return Path(value)


def _open_log_sink(
    log_path: Path,
    secrets: list[str],
    *,
    emit,
) -> tuple[LogSink | _DrainOnlySink, bool]:
    try:
        return LogSink(log_path, secrets=secrets, emit=emit), True
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
        build_id=payload.get("build_id"),
        build_label=payload.get("build_label"),
        model_ref=payload.get("model_ref"),
        model_entry_id=payload.get("model_entry_id"),
        model_repo_id=payload.get("model_repo_id"),
        model_revision=payload.get("model_revision"),
        model_commit_sha=payload.get("model_commit_sha"),
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


def _write_docker_run_artifacts(
    payload: dict, container_id: str, log_path: Path
) -> Manifest:
    manifest_path = Path(payload["manifest_path"])
    sidecar_path = Path(payload["sidecar_path"])
    manifest = Manifest.from_active_log(log_path)
    manifest.write_atomic(manifest_path)

    supervisor_proc = psutil.Process(os.getpid())
    secrets = [secret for secret in payload.get("secrets", []) if secret]
    docker = payload.get("docker") if isinstance(payload.get("docker"), dict) else {}
    sidecar = Sidecar(
        run_id=payload["run_id"],
        config_name=payload["config_name"],
        config_snapshot=payload.get("config_snapshot"),
        command_argv=_scrub_argv_for_artifact(payload["argv"], secrets),
        command_hash=payload["command_hash"],
        runtime="docker",
        docker_binary=str(docker.get("binary") or payload["argv"][0]),
        docker_container_name=str(docker.get("container_name") or ""),
        docker_container_id=container_id,
        docker_image_digest=str(docker.get("image_digest") or docker.get("image") or ""),
        docker_stop_grace_seconds=int(docker.get("stop_grace_seconds") or 90),
        build_id=payload.get("build_id"),
        build_label=payload.get("build_label"),
        model_ref=payload.get("model_ref"),
        model_entry_id=payload.get("model_entry_id"),
        model_repo_id=payload.get("model_repo_id"),
        model_revision=payload.get("model_revision"),
        model_commit_sha=payload.get("model_commit_sha"),
        vllm_version=payload.get("vllm_version"),
        vllm_version_profile=payload.get("vllm_version_profile"),
        executable=str(docker.get("binary") or payload["argv"][0]),
        cwd=payload.get("cwd") or os.getcwd(),
        pid=0,
        pgid=0,
        process_create_time=0.0,
        procfs_starttime=None,
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


def _container_id_from_run_stdout(stdout: bytes) -> str:
    for line in reversed(stdout.decode(errors="replace").splitlines()):
        value = line.strip()
        if value:
            return value
    return ""


def _docker_wait_returncode(wait: subprocess.CompletedProcess[str]) -> int:
    if wait.returncode != 0:
        return int(wait.returncode)
    for line in wait.stdout.splitlines():
        value = line.strip()
        if not value:
            continue
        try:
            return int(value)
        except ValueError:
            continue
    return 0


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
