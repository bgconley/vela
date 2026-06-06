from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import psutil

from vela.engine.redaction import MASK


class TrackedProcessMismatch(RuntimeError):
    pass


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    create_time: float
    pgid: int
    executable: str
    cmdline: list[str]
    procfs_starttime: int | None = None


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
    runtime: str = "process"
    config_snapshot: dict | None = None
    docker_binary: str = "docker"
    docker_container_name: str | None = None
    docker_container_id: str | None = None
    docker_image_digest: str | None = None
    docker_stop_grace_seconds: int | None = None
    build_id: str | None = None
    build_label: str | None = None
    model_ref: str | None = None
    model_entry_id: str | None = None
    model_repo_id: str | None = None
    model_revision: str | None = None
    model_commit_sha: str | None = None
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
    if sidecar.runtime == "docker":
        return verify_container_identity(sidecar)
    if child.pid != sidecar.pid or abs(child.create_time - sidecar.process_create_time) > 0.001:
        raise TrackedProcessMismatch(
            "tracked process is gone; refusing to signal a possibly-recycled PID"
        )
    _verify_procfs_starttime(
        "process",
        recorded=sidecar.procfs_starttime,
        live=child.procfs_starttime,
    )
    if child.pgid != sidecar.pgid:
        raise TrackedProcessMismatch("tracked process group does not match sidecar")
    command_line_matches = False
    if child.cmdline and command_hash(child.cmdline) != sidecar.command_hash:
        if not _command_lines_equivalent(child.cmdline, sidecar.command_argv):
            raise TrackedProcessMismatch("tracked command line does not match sidecar")
        command_line_matches = True
    elif child.cmdline:
        command_line_matches = True
    if (
        sidecar.executable
        and child.executable != sidecar.executable
        and not command_line_matches
    ):
        raise TrackedProcessMismatch("tracked executable does not match sidecar")
    if sidecar.launch_mode == "detached":
        if supervisor is None:
            raise TrackedProcessMismatch("detached run supervisor identity is unavailable")
        if supervisor.pid != sidecar.supervisor_pid:
            raise TrackedProcessMismatch("supervisor PID does not match sidecar")
        if sidecar.supervisor_create_time is not None:
            if abs(supervisor.create_time - sidecar.supervisor_create_time) > 0.001:
                raise TrackedProcessMismatch("supervisor create_time does not match sidecar")
        _verify_procfs_starttime(
            "supervisor",
            recorded=sidecar.supervisor_procfs_starttime,
            live=supervisor.procfs_starttime,
        )
    return True


def destructive_signal(
    sidecar: Sidecar,
    signal_number: int,
    *,
    child: ProcessIdentity,
    supervisor: ProcessIdentity | None,
) -> None:
    if sidecar.runtime == "docker":
        verify_container_identity(sidecar)
        if signal_number == signal.SIGKILL:
            _run_docker_command(
                [
                    sidecar.docker_binary,
                    "kill",
                    _required_container_id(sidecar),
                ]
            )
            return
        timeout = sidecar.docker_stop_grace_seconds or 90
        _run_docker_command(
            [
                sidecar.docker_binary,
                "stop",
                "-t",
                str(timeout),
                _required_container_id(sidecar),
            ]
        )
        return
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
        procfs_starttime=procfs_starttime_from_pid(pid),
    )


def verify_sidecar_from_system(path: Path | str) -> bool:
    sidecar = load_sidecar(path)
    if sidecar.runtime == "docker":
        verify_container_identity(sidecar)
        manifest = load_manifest(sidecar.manifest_path)
        active_path = Path(manifest.active_log.path)
        if _inode(active_path) != manifest.active_log.inode:
            raise TrackedProcessMismatch("active log inode does not match manifest")
        return True
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
    if sidecar.runtime == "docker":
        destructive_signal(
            sidecar,
            signal_number,
            child=ProcessIdentity(0, 0.0, 0, "", []),
            supervisor=None,
        )
        return
    child = process_identity_from_pid(sidecar.pid)
    supervisor = None
    if sidecar.supervisor_pid is not None:
        supervisor = process_identity_from_pid(sidecar.supervisor_pid)
    destructive_signal(sidecar, signal_number, child=child, supervisor=supervisor)


def stop_sidecar_from_system(
    path: Path | str, *, interrupt_timeout: float = 5, terminate_timeout: float = 5
) -> None:
    sidecar = load_sidecar(path)
    if sidecar.runtime == "docker":
        signal_sidecar_from_system(path, signal.SIGTERM)
        return
    signal_sidecar_from_system(path, signal.SIGINT)
    if _wait_process_exit(sidecar.pid, sidecar.process_create_time, interrupt_timeout):
        return
    signal_sidecar_from_system(path, signal.SIGTERM)
    if _wait_process_exit(sidecar.pid, sidecar.process_create_time, terminate_timeout):
        return
    signal_sidecar_from_system(path, signal.SIGKILL)
    _wait_process_exit(sidecar.pid, sidecar.process_create_time, terminate_timeout)


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


def verify_container_identity(sidecar: Sidecar) -> bool:
    expected_id = _required_container_id(sidecar)
    expected_name = _required_container_name(sidecar)
    expected_digest = _required_image_digest(sidecar)
    inspect = _docker_inspect_container(sidecar.docker_binary, expected_id)
    live_id = str(inspect.get("Id") or "")
    if live_id != expected_id:
        raise TrackedProcessMismatch("tracked docker container id does not match sidecar")
    live_name = str(inspect.get("Name") or "").lstrip("/")
    if live_name != expected_name:
        raise TrackedProcessMismatch("tracked docker container name does not match sidecar")
    if not _inspect_matches_image_digest(inspect, expected_digest):
        raise TrackedProcessMismatch("tracked docker image digest does not match sidecar")
    return True


def _required_container_id(sidecar: Sidecar) -> str:
    if sidecar.docker_container_id:
        return sidecar.docker_container_id
    raise TrackedProcessMismatch("docker sidecar is missing container id")


def _required_container_name(sidecar: Sidecar) -> str:
    if sidecar.docker_container_name:
        return sidecar.docker_container_name
    raise TrackedProcessMismatch("docker sidecar is missing container name")


def _required_image_digest(sidecar: Sidecar) -> str:
    if sidecar.docker_image_digest:
        return sidecar.docker_image_digest
    raise TrackedProcessMismatch("docker sidecar is missing image digest")


def _docker_inspect_container(docker_binary: str, container_id: str) -> dict:
    proc = subprocess.run(
        [docker_binary, "inspect", container_id],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "docker inspect failed").strip()
        raise TrackedProcessMismatch(f"tracked docker container is unavailable: {detail}")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise TrackedProcessMismatch("docker inspect returned invalid JSON") from exc
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return payload[0]
    if isinstance(payload, dict):
        return payload
    raise TrackedProcessMismatch("docker inspect returned no container metadata")


def _run_docker_command(argv: list[str]) -> None:
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "docker command failed").strip()
        raise TrackedProcessMismatch(detail)


def _inspect_matches_image_digest(inspect: dict, expected_digest: str) -> bool:
    candidates = {
        str(inspect.get("Image") or ""),
    }
    config = inspect.get("Config")
    if isinstance(config, dict):
        candidates.add(str(config.get("Image") or ""))
    repo_digests = inspect.get("RepoDigests")
    if isinstance(repo_digests, list):
        candidates.update(str(item) for item in repo_digests)
    return any(
        _image_candidate_matches_digest(candidate, expected_digest)
        for candidate in candidates
    )


def _image_candidate_matches_digest(candidate: str, expected_digest: str) -> bool:
    if not candidate:
        return False
    if candidate == expected_digest:
        return True
    return candidate.endswith(f"@{expected_digest}") or candidate.endswith(expected_digest)


def command_hash(argv: list[str]) -> str:
    payload = "\0".join(argv).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _command_lines_equivalent(live: list[str], recorded: list[str]) -> bool:
    return _argv_equivalent(live, recorded) or _argv_equivalent(
        _python_script_tail(live), _python_script_tail(recorded)
    )


def _argv_equivalent(live: list[str], recorded: list[str]) -> bool:
    if len(live) != len(recorded):
        return False
    return all(
        _arg_equivalent(live_arg, recorded_arg)
        for live_arg, recorded_arg in zip(live, recorded, strict=True)
    )


def _arg_equivalent(live: str, recorded: str) -> bool:
    if MASK not in recorded:
        return live == recorded
    pattern = ".*".join(re.escape(part) for part in recorded.split(MASK))
    return re.fullmatch(pattern, live) is not None


def _python_script_tail(argv: list[str]) -> list[str]:
    if len(argv) < 2:
        return argv
    executable_name = Path(argv[0]).name.lower()
    if executable_name.startswith("python"):
        return argv[1:]
    return argv


def _inode(path: Path) -> int:
    try:
        return path.stat().st_ino
    except FileNotFoundError:
        return -1


def procfs_starttime_from_pid(pid: int, *, proc_root: Path = Path("/proc")) -> int | None:
    try:
        stat_text = (proc_root / str(pid) / "stat").read_text(encoding="utf-8")
    except OSError:
        return None
    comm_end = stat_text.rfind(")")
    if comm_end == -1:
        return None
    fields_after_comm = stat_text[comm_end + 1 :].strip().split()
    if len(fields_after_comm) < 20:
        return None
    try:
        return int(fields_after_comm[19])
    except ValueError:
        return None


def _verify_procfs_starttime(label: str, *, recorded: int | None, live: int | None) -> None:
    if recorded is None:
        return
    if live != recorded:
        raise TrackedProcessMismatch(f"{label} procfs starttime does not match sidecar")


def _write_private_text(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(text)


def _signal_process_group(pgid: int, signal_number: int) -> None:
    try:
        os.killpg(pgid, signal_number)
    except ProcessLookupError:
        return
    except PermissionError as exc:
        raise TrackedProcessMismatch(
            "permission denied signaling tracked process group"
        ) from exc


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
