from __future__ import annotations

import errno
import os
import re
import shutil
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path

from vela.config.schema import ModelConfig, RuntimeKind
from vela.engine.command_builder import is_local_model_reference
from vela.engine.docker_runtime import DockerCommandError, DockerErrorKind, inspect_docker_image
from vela.engine.phases import ErrorKind
from vela.monitoring.gpu import parse_cuda_visible_devices

MIN_FREE_DISK_BYTES = 1024 * 1024 * 1024


@dataclass(frozen=True)
class LaunchPreflightFailure:
    kind: ErrorKind
    detail: str


def check_launch_preflight(
    cfg: ModelConfig, *, cwd: Path | None = None
) -> LaunchPreflightFailure | None:
    cwd = cwd or Path.cwd()
    if missing_model_path := missing_local_model_path(cfg, cwd=cwd):
        return LaunchPreflightFailure(
            ErrorKind.MODEL_NOT_FOUND, f"Local model path not found: {missing_model_path}"
        )
    if parallel_mismatch := parallel_world_size_mismatch(cfg):
        return LaunchPreflightFailure(ErrorKind.TP_MISMATCH, parallel_mismatch)
    if occupied_port := occupied_port_detail(cfg):
        return LaunchPreflightFailure(ErrorKind.PORT_IN_USE, occupied_port)
    if docker_image_failure := docker_image_availability_detail(cfg):
        return docker_image_failure
    if low_disk := low_disk_space_detail(cfg, cwd=cwd):
        return LaunchPreflightFailure(ErrorKind.DISK_FULL, low_disk)
    return None


def missing_local_model_path(cfg: ModelConfig, *, cwd: Path) -> Path | None:
    if not is_local_model_reference(cfg.model, cwd=cwd):
        return None
    candidate = Path(cfg.model).expanduser()
    if not candidate.is_absolute():
        candidate = cwd / candidate
    if candidate.exists():
        return None
    return candidate


def parallel_world_size_mismatch(cfg: ModelConfig) -> str | None:
    visible = parse_cuda_visible_devices(cfg.env.get("CUDA_VISIBLE_DEVICES"))
    visible_count = len(visible.numeric) + len(visible.uuids)
    if visible_count == 0:
        return None
    tensor_parallel = cfg.engine.tensor_parallel_size or 1
    pipeline_parallel = cfg.engine.pipeline_parallel_size or 1
    world_size = tensor_parallel * pipeline_parallel
    if world_size <= visible_count:
        return None
    gpu_word = "GPU" if visible_count == 1 else "GPUs"
    return (
        f"Configured world size {world_size} "
        f"(tensor_parallel_size={tensor_parallel}, "
        f"pipeline_parallel_size={pipeline_parallel}) exceeds "
        f"{visible_count} visible {gpu_word} from CUDA_VISIBLE_DEVICES={visible.raw}."
    )


def occupied_port_detail(cfg: ModelConfig) -> str | None:
    family = socket.AF_INET6 if ":" in cfg.server.host else socket.AF_INET
    try:
        with socket.socket(family) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((cfg.server.host, cfg.server.port))
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            return f"Port {cfg.server.port} is already in use on {cfg.server.host}."
    if docker_port_detail := docker_published_port_detail(cfg):
        return docker_port_detail
    return None


def docker_published_port_detail(cfg: ModelConfig) -> str | None:
    if cfg.command.runtime is not RuntimeKind.DOCKER:
        return None
    docker_binary = shutil.which("docker")
    if docker_binary is None:
        return None
    try:
        result = subprocess.run(
            [docker_binary, "ps", "--format", "{{.Ports}}"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    if not _docker_ports_include_host_port(result.stdout, cfg.server.port):
        return None
    return f"Port {cfg.server.port} is already published by a Docker container."


def docker_image_availability_detail(cfg: ModelConfig) -> LaunchPreflightFailure | None:
    if cfg.command.runtime is not RuntimeKind.DOCKER or cfg.command.docker is None:
        return None
    docker_binary = shutil.which("docker")
    if docker_binary is None:
        return LaunchPreflightFailure(
            ErrorKind.DAEMON_UNREACHABLE,
            "Docker executable not found on target PATH.",
        )
    docker = cfg.command.docker
    if docker.pull != "never":
        return None
    try:
        inspect_docker_image(docker_binary, docker.image)
    except DockerCommandError as exc:
        return LaunchPreflightFailure(_docker_error_kind_to_preflight_kind(exc.kind), exc.detail)
    return None


def _docker_error_kind_to_preflight_kind(kind: DockerErrorKind) -> ErrorKind:
    if kind is DockerErrorKind.IMAGE_NOT_FOUND:
        return ErrorKind.IMAGE_NOT_FOUND
    if kind is DockerErrorKind.DAEMON_UNREACHABLE:
        return ErrorKind.DAEMON_UNREACHABLE
    if kind is DockerErrorKind.NAME_CONFLICT:
        return ErrorKind.NAME_CONFLICT
    if kind is DockerErrorKind.GPU_NOT_AVAILABLE:
        return ErrorKind.GPU_NOT_AVAILABLE
    return ErrorKind.CONFIG_INVALID


def low_disk_space_detail(cfg: ModelConfig, *, cwd: Path) -> str | None:
    minimum_free = _minimum_free_disk_bytes()
    if minimum_free <= 0:
        return None
    seen: set[Path] = set()
    for path in _disk_preflight_paths(cfg, cwd=cwd):
        probe = _existing_disk_probe_path(path)
        if probe is None or probe in seen:
            continue
        seen.add(probe)
        try:
            usage = shutil.disk_usage(probe)
        except OSError:
            continue
        if usage.free >= minimum_free:
            continue
        return (
            f"Only {_format_bytes(usage.free)} free on {probe}; "
            f"need at least {_format_bytes(minimum_free)} for launch artifacts/cache."
        )
    return None


def _minimum_free_disk_bytes() -> int:
    raw = os.environ.get("VELA_MIN_FREE_DISK_BYTES")
    if raw is None or not raw.strip():
        return MIN_FREE_DISK_BYTES
    try:
        return max(0, int(raw))
    except ValueError:
        return MIN_FREE_DISK_BYTES


def _disk_preflight_paths(cfg: ModelConfig, *, cwd: Path) -> list[Path]:
    paths = [cwd]
    if cfg.command.cwd is not None:
        paths.append(_host_path(cfg.command.cwd, cwd=cwd))
    if cfg.launch.runs_dir is not None:
        paths.append(_host_path(cfg.launch.runs_dir, cwd=cwd))
    if is_local_model_reference(cfg.model, cwd=cwd):
        paths.append(_host_path(Path(cfg.model), cwd=cwd))
    docker = cfg.command.docker
    if docker is not None:
        if docker.hf_cache is not None:
            paths.append(_host_path(docker.hf_cache, cwd=cwd))
        for volume in docker.volumes:
            source = _docker_volume_source(volume)
            if source is not None:
                paths.append(_host_path(Path(source), cwd=cwd))
    return paths


def _host_path(path: Path, *, cwd: Path) -> Path:
    expanded = path.expanduser()
    return expanded if expanded.is_absolute() else cwd / expanded


def _existing_disk_probe_path(path: Path) -> Path | None:
    candidate = path
    while True:
        if candidate.exists():
            return candidate
        parent = candidate.parent
        if parent == candidate:
            return None
        candidate = parent


def _docker_volume_source(volume: str) -> str | None:
    if not volume:
        return None
    source = volume.split(":", 1)[0].strip()
    return source or None


def _format_bytes(value: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    amount = float(value)
    for unit in units[:-1]:
        if amount < 1024:
            if unit == "B":
                return f"{int(amount)} {unit}"
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} {units[-1]}"


def _docker_ports_include_host_port(ports_output: str, port: int) -> bool:
    host_port = re.escape(str(port))
    return re.search(rf"(?<!\d){host_port}->", ports_output) is not None
