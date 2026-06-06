from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vela.config.schema import ModelConfig


@dataclass(frozen=True)
class DockerRunCommand:
    argv: list[str]
    env: dict[str, str]
    metadata: dict[str, Any] = field(default_factory=dict)


def build_docker_run(
    cfg: ModelConfig,
    resolved_serve_args: list[str],
    env: dict[str, str],
    *,
    docker_binary: str = "docker",
) -> DockerRunCommand:
    docker = cfg.command.docker
    if docker is None:
        raise ValueError("docker runtime requires command.docker")

    container_name = docker.container_name or f"vela-{cfg.name}"
    container_env = {**docker.env, **env}
    argv = [docker_binary, "run", "-d", "--name", container_name]
    if docker.gpus:
        argv.extend(["--gpus", docker.gpus])
    if docker.network:
        argv.extend(["--network", docker.network])
    if docker.ipc_host:
        argv.append("--ipc=host")
    shm_size = docker.shm_size or _default_shm_size(cfg)
    if shm_size:
        argv.extend(["--shm-size", shm_size])
    if docker.restart:
        argv.extend(["--restart", docker.restart])
    if docker.entrypoint:
        argv.extend(["--entrypoint", docker.entrypoint])

    for key in sorted(container_env):
        argv.extend(["-e", key])
    for volume in _docker_volumes(cfg):
        argv.extend(["-v", volume])
    argv.extend(docker.extra_run_args)
    argv.append(docker.image)
    argv.extend(_container_serve_args(resolved_serve_args))

    return DockerRunCommand(
        argv=argv,
        env=container_env,
        metadata={
            "runtime": "docker",
            "docker_binary": docker_binary,
            "docker_image": docker.image,
            "docker_image_digest": _image_digest_for_sidecar(docker.image),
            "docker_container_name": container_name,
            "docker_evict": list(docker.evict),
            "docker_stop_grace_seconds": docker.stop_grace_seconds,
        },
    )


def _container_serve_args(resolved_serve_args: list[str]) -> list[str]:
    args = list(resolved_serve_args)
    if args and args[0] == "serve":
        return args[1:]
    return args


def _default_shm_size(cfg: ModelConfig) -> str:
    tensor_parallel_size = cfg.engine.tensor_parallel_size or 1
    return "32g" if tensor_parallel_size > 1 else "16g"


def _docker_volumes(cfg: ModelConfig) -> list[str]:
    docker = cfg.command.docker
    if docker is None:
        return []
    volumes = list(docker.volumes)
    if docker.hf_cache is not None:
        source = Path(docker.hf_cache).expanduser()
        volumes.append(f"{source}:{docker.hf_cache_target}:rw")
    return volumes


def _image_digest_for_sidecar(image: str) -> str:
    if "@sha256:" in image:
        return "sha256:" + image.rsplit("@sha256:", 1)[1]
    return image
