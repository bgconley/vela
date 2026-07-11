from __future__ import annotations

import json
import os
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from vela.config.schema import ModelConfig

DEFAULT_DOCKER_COMMAND_TIMEOUT_SECONDS = 10
DEFAULT_DOCKER_PULL_TIMEOUT_SECONDS = 1800
DOCKER_PULL_TIMEOUT_ENV = "VELA_DOCKER_PULL_TIMEOUT_SECONDS"


@dataclass(frozen=True)
class DockerRunCommand:
    argv: list[str]
    env: dict[str, str]
    metadata: dict[str, Any] = field(default_factory=dict)


class DockerErrorKind(str, Enum):
    IMAGE_NOT_FOUND = "image-not-found"
    IMAGE_PULL_FAILED = "image-pull-failed"
    IMAGE_PULL_TIMEOUT = "image-pull-timeout"
    DAEMON_UNREACHABLE = "daemon-unreachable"
    NAME_CONFLICT = "name-conflict"
    OCI_RUNTIME_ERROR = "oci-runtime-error"
    GPU_NOT_AVAILABLE = "gpu-not-available"


class DockerCommandError(RuntimeError):
    def __init__(
        self,
        kind: DockerErrorKind,
        detail: str,
        *,
        returncode: int = 1,
    ) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail
        self.returncode = returncode


@dataclass(frozen=True)
class DockerImageInspection:
    image: str
    digest: str
    raw: dict[str, Any]


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
    if docker.runtime:
        argv.extend(["--runtime", docker.runtime])
    if docker.gpus:
        argv.extend(["--gpus", docker.gpus])
    if docker.network:
        argv.extend(["--network", docker.network])
    if docker.ipc_host:
        argv.append("--ipc=host")
    shm_size = (
        docker.shm_size
        if docker.ipc_host
        else docker.shm_size or _default_shm_size(cfg)
    )
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
            "docker_pull_policy": docker.pull,
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
    return ""


def prepare_docker_image(
    docker_binary: str,
    image: str,
    pull_policy: str,
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    progress: Callable[[bytes], None] | None = None,
) -> DockerImageInspection:
    policy = pull_policy if pull_policy in {"never", "missing", "always"} else "never"
    if policy == "always":
        pull_docker_image(docker_binary, image, cwd=cwd, env=env, progress=progress)
        return inspect_docker_image(docker_binary, image, cwd=cwd, env=env)
    if policy == "missing":
        try:
            return inspect_docker_image(docker_binary, image, cwd=cwd, env=env)
        except DockerCommandError as exc:
            if exc.kind is not DockerErrorKind.IMAGE_NOT_FOUND:
                raise
        pull_docker_image(docker_binary, image, cwd=cwd, env=env, progress=progress)
        return inspect_docker_image(docker_binary, image, cwd=cwd, env=env)
    return inspect_docker_image(docker_binary, image, cwd=cwd, env=env)


def pull_docker_image(
    docker_binary: str,
    image: str,
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    progress: Callable[[bytes], None] | None = None,
) -> None:
    """Pull ``image``, streaming progress to ``progress`` and enforcing a timeout.

    A real vLLM image is ~10 GB, so the pull runs under
    ``VELA_DOCKER_PULL_TIMEOUT_SECONDS`` (default 1800, ``<=0`` disables it) —
    not the 10 s used for quick inspect/stop commands. A timeout is mapped to a
    classified :class:`DockerCommandError` (``image-pull-timeout``) rather than
    an uncaught ``subprocess.TimeoutExpired`` so the supervisor can record a
    real failure (bug-240). ``docker pull`` writes phase/progress lines to
    stdout; each chunk is forwarded to ``progress`` so the TUI shows the
    download instead of a silent multi-minute hang.
    """
    timeout = _pull_timeout_seconds()
    proc = subprocess.Popen(
        [docker_binary, "pull", image],
        cwd=cwd,
        env={**os.environ, **dict(env or {})},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        close_fds=True,
    )
    captured = bytearray()

    def _read_output() -> None:
        stream = proc.stdout
        if stream is None:
            return
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                captured.extend(chunk)
                if progress is not None:
                    try:
                        progress(chunk)
                    except Exception:
                        # Progress is best-effort; a sink failure must never
                        # abort the pull itself.
                        pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    reader = threading.Thread(target=_read_output, daemon=True)
    reader.start()
    try:
        returncode = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        reader.join(timeout=5)
        raise DockerCommandError(
            DockerErrorKind.IMAGE_PULL_TIMEOUT,
            _pull_timeout_detail(image, timeout),
            returncode=124,
        ) from exc
    reader.join(timeout=5)
    if returncode == 0:
        return
    detail = bytes(captured).decode("utf-8", errors="replace").strip() or "docker pull failed"
    kind = classify_docker_error(detail, default=DockerErrorKind.IMAGE_PULL_FAILED)
    raise DockerCommandError(kind, detail, returncode=int(returncode))


def _pull_timeout_seconds() -> float | None:
    raw = os.environ.get(DOCKER_PULL_TIMEOUT_ENV)
    if raw is None or not raw.strip():
        return float(DEFAULT_DOCKER_PULL_TIMEOUT_SECONDS)
    try:
        value = float(raw)
    except ValueError:
        return float(DEFAULT_DOCKER_PULL_TIMEOUT_SECONDS)
    if value <= 0:
        # An explicit non-positive value opts out of the timeout entirely and
        # relies on the supervisor's own lifecycle to cancel a stuck pull.
        return None
    return value


def _pull_timeout_detail(image: str, timeout: float | None) -> str:
    limit = "no limit" if timeout is None else f"{timeout:g}s"
    return (
        f"docker pull for {image} exceeded {limit}; "
        f"raise {DOCKER_PULL_TIMEOUT_ENV} or pre-pull the image on the target"
    )


def inspect_docker_image(
    docker_binary: str,
    image: str,
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> DockerImageInspection:
    result = _run_docker(
        [docker_binary, "image", "inspect", image],
        cwd=cwd,
        env=env,
    )
    if result.returncode != 0:
        detail = _docker_result_detail(result)
        kind = classify_docker_error(detail, default=DockerErrorKind.OCI_RUNTIME_ERROR)
        raise DockerCommandError(kind, detail, returncode=int(result.returncode))
    try:
        parsed = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise DockerCommandError(
            DockerErrorKind.OCI_RUNTIME_ERROR,
            "docker image inspect returned invalid JSON",
        ) from exc
    item = _first_inspect_object(parsed)
    digest = _digest_from_image_inspect(item)
    if not digest:
        raise DockerCommandError(
            DockerErrorKind.OCI_RUNTIME_ERROR,
            f"docker image inspect returned no digest for {image}",
        )
    return DockerImageInspection(image=image, digest=digest, raw=item)


def classify_docker_error(
    detail: str, *, default: DockerErrorKind = DockerErrorKind.OCI_RUNTIME_ERROR
) -> DockerErrorKind:
    lowered = detail.lower()
    if "no such image" in lowered or "not found" in lowered:
        return DockerErrorKind.IMAGE_NOT_FOUND
    if (
        "cannot connect to the docker daemon" in lowered
        or "is the docker daemon running" in lowered
    ):
        return DockerErrorKind.DAEMON_UNREACHABLE
    if "conflict" in lowered and "container name" in lowered:
        return DockerErrorKind.NAME_CONFLICT
    if "already in use by container" in lowered:
        return DockerErrorKind.NAME_CONFLICT
    if "could not select device driver" in lowered or "nvidia" in lowered and "gpu" in lowered:
        return DockerErrorKind.GPU_NOT_AVAILABLE
    return default


def _run_docker(
    argv: list[str],
    *,
    cwd: str | None,
    env: dict[str, str] | None,
    timeout: float | None = DEFAULT_DOCKER_COMMAND_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        env={**os.environ, **dict(env or {})},
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _docker_result_detail(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr or result.stdout or "docker command failed").strip()


def _first_inspect_object(payload: Any) -> dict[str, Any]:
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return payload[0]
    if isinstance(payload, dict):
        return payload
    raise DockerCommandError(
        DockerErrorKind.OCI_RUNTIME_ERROR,
        "docker image inspect returned no image metadata",
    )


def _digest_from_image_inspect(payload: dict[str, Any]) -> str:
    candidates: list[str] = []
    image_id = payload.get("Id")
    if isinstance(image_id, str):
        candidates.append(image_id)
    repo_digests = payload.get("RepoDigests")
    if isinstance(repo_digests, list):
        candidates.extend(str(item) for item in repo_digests)
    for candidate in candidates:
        digest = _digest_from_image_candidate(candidate)
        if digest:
            return digest
    return ""


def _digest_from_image_candidate(candidate: str) -> str:
    if candidate.startswith("sha256:"):
        return candidate
    if "@sha256:" in candidate:
        return "sha256:" + candidate.rsplit("@sha256:", 1)[1]
    return ""
