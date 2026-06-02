from __future__ import annotations

import errno
import socket
from dataclasses import dataclass
from pathlib import Path

from vllm_loader.config.schema import ModelConfig
from vllm_loader.engine.command_builder import is_local_model_reference
from vllm_loader.engine.phases import ErrorKind
from vllm_loader.monitoring.gpu import parse_cuda_visible_devices


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
    return None
