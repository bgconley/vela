from __future__ import annotations

import hashlib
import json
import os
import signal
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import psutil


class TrackedProcessMismatch(RuntimeError):
    pass


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    create_time: float
    pgid: int
    executable: str
    cmdline: list[str]


@dataclass
class LogPointer:
    path: str
    inode: int
    rotated_at: str | None = None


@dataclass
class Manifest:
    active_log: LogPointer
    rotated: list[LogPointer] = field(default_factory=list)

    @classmethod
    def from_active_log(cls, path: Path) -> Manifest:
        return cls(active_log=LogPointer(str(path), _inode(path)))

    def rotate_to(self, new_path: Path) -> None:
        old = self.active_log
        old.rotated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.rotated.insert(0, old)
        self.active_log = LogPointer(str(new_path), _inode(new_path))

    def write_atomic(self, path: Path) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        _write_private_text(tmp, json.dumps(asdict(self), indent=2))
        tmp.replace(path)


@dataclass
class Sidecar:
    run_id: str
    config_name: str
    command_argv: list[str]
    command_hash: str
    pid: int
    pgid: int
    process_create_time: float
    executable: str
    cwd: str
    launch_mode: str
    host: str
    port: int
    served_model_names: list[str]
    exposure: str
    manifest_path: str
    schema_version: int = 1
    config_snapshot: dict | None = None
    vllm_version: str | None = None
    vllm_version_profile: str | None = None
    supervisor_pid: int | None = None
    supervisor_create_time: float | None = None
    supervisor_procfs_starttime: int | None = None
    supervisor_executable: str | None = None
    procfs_starttime: int | None = None
    log_redaction: str = "scrubbed"

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    def write_atomic(self, path: Path) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        _write_private_text(tmp, self.to_json())
        tmp.replace(path)


def verify_sidecar_identity(
    sidecar: Sidecar,
    child: ProcessIdentity,
    supervisor: ProcessIdentity | None,
) -> bool:
    if child.pid != sidecar.pid or abs(child.create_time - sidecar.process_create_time) > 0.001:
        raise TrackedProcessMismatch(
            "tracked process is gone; refusing to signal a possibly-recycled PID"
        )
    if child.pgid != sidecar.pgid:
        raise TrackedProcessMismatch("tracked process group does not match sidecar")
    if sidecar.executable and child.executable != sidecar.executable:
        raise TrackedProcessMismatch("tracked executable does not match sidecar")
    if child.cmdline and command_hash(child.cmdline) != sidecar.command_hash:
        if child.cmdline != sidecar.command_argv:
            raise TrackedProcessMismatch("tracked command line does not match sidecar")
    if sidecar.launch_mode == "detached":
        if supervisor is None:
            raise TrackedProcessMismatch("detached run supervisor identity is unavailable")
        if supervisor.pid != sidecar.supervisor_pid:
            raise TrackedProcessMismatch("supervisor PID does not match sidecar")
        if sidecar.supervisor_create_time is not None:
            if abs(supervisor.create_time - sidecar.supervisor_create_time) > 0.001:
                raise TrackedProcessMismatch("supervisor create_time does not match sidecar")
    return True


def destructive_signal(
    sidecar: Sidecar,
    signal_number: int,
    *,
    child: ProcessIdentity,
    supervisor: ProcessIdentity | None,
) -> None:
    verify_sidecar_identity(sidecar, child, supervisor)
    _signal_process_group(sidecar.pgid, signal_number)


def load_sidecar(path: Path | str) -> Sidecar:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return Sidecar(**data)


def load_manifest(path: Path | str) -> Manifest:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return Manifest(
        active_log=LogPointer(**data["active_log"]),
        rotated=[LogPointer(**item) for item in data.get("rotated", [])],
    )


def process_identity_from_pid(pid: int) -> ProcessIdentity:
    proc = psutil.Process(pid)
    return ProcessIdentity(
        pid=pid,
        create_time=proc.create_time(),
        pgid=os.getpgid(pid),
        executable=_safe_exe(proc),
        cmdline=_safe_cmdline(proc),
    )


def verify_sidecar_from_system(path: Path | str) -> bool:
    sidecar = load_sidecar(path)
    child = process_identity_from_pid(sidecar.pid)
    supervisor = None
    if sidecar.supervisor_pid is not None:
        supervisor = process_identity_from_pid(sidecar.supervisor_pid)
    verify_sidecar_identity(sidecar, child, supervisor)
    manifest = load_manifest(sidecar.manifest_path)
    active_path = Path(manifest.active_log.path)
    if _inode(active_path) != manifest.active_log.inode:
        raise TrackedProcessMismatch("active log inode does not match manifest")
    return True


def signal_sidecar_from_system(path: Path | str, signal_number: int) -> None:
    sidecar = load_sidecar(path)
    child = process_identity_from_pid(sidecar.pid)
    supervisor = None
    if sidecar.supervisor_pid is not None:
        supervisor = process_identity_from_pid(sidecar.supervisor_pid)
    destructive_signal(sidecar, signal_number, child=child, supervisor=supervisor)


def stop_sidecar_from_system(
    path: Path | str, *, interrupt_timeout: float = 5, terminate_timeout: float = 5
) -> None:
    sidecar = load_sidecar(path)
    signal_sidecar_from_system(path, signal.SIGINT)
    if _wait_process_exit(sidecar.pid, sidecar.process_create_time, interrupt_timeout):
        return
    signal_sidecar_from_system(path, signal.SIGTERM)
    if _wait_process_exit(sidecar.pid, sidecar.process_create_time, terminate_timeout):
        return
    signal_sidecar_from_system(path, signal.SIGKILL)


def discover_active_sidecars(runs_dirs: list[Path]) -> list[Path]:
    active: list[Path] = []
    seen: set[Path] = set()
    for runs_dir in runs_dirs:
        if not runs_dir.exists():
            continue
        for path in sorted(
            runs_dir.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True
        ):
            if path.name.endswith(".manifest.json") or path in seen:
                continue
            seen.add(path)
            try:
                sidecar = load_sidecar(path)
                if sidecar.launch_mode == "detached" and verify_sidecar_from_system(path):
                    active.append(path)
            except Exception:
                continue
    return active


def command_hash(argv: list[str]) -> str:
    payload = "\0".join(argv).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _inode(path: Path) -> int:
    try:
        return path.stat().st_ino
    except FileNotFoundError:
        return -1


def _write_private_text(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(text)


def _signal_process_group(pgid: int, signal_number: int) -> None:
    try:
        os.killpg(pgid, signal_number)
    except ProcessLookupError:
        return


def _wait_process_exit(pid: int, create_time: float, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            proc = psutil.Process(pid)
            if abs(proc.create_time() - create_time) > 0.001:
                return True
        except psutil.NoSuchProcess:
            return True
        time.sleep(0.05)
    return False


def _safe_exe(proc: psutil.Process) -> str:
    try:
        return proc.exe()
    except Exception:
        return ""


def _safe_cmdline(proc: psutil.Process) -> list[str]:
    try:
        return proc.cmdline()
    except Exception:
        return []
