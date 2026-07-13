#!/usr/bin/env python3
"""Fail-closed host guard for the Oxcart live GPU validation lane.

With no action argument this script performs the read-only preflight. ``cleanup``
is the only mutating action, and it refuses to touch a container unless the exact
validation name and both Vela ownership labels still match immediately before each
mutation.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any, Protocol

import psutil

EXPECTED_HOSTNAME = "oxcart"
PROFILE_NAME = "oxcart-qwen36-27b-fp8-mtp-vl"
VALIDATION_CONTAINER = "vela-oxcart-qwen36-27b-fp8-mtp-vl"
VALIDATION_PORT = 18004
EXPECTED_IMAGE = (
    "vllm/vllm-openai@sha256:"
    "b13d6e5fda0785f3d41752df8513ff832f67cb231a216c76b6b4f2a515bf0046"
)
MODEL_REVISION = "e89b16ebf1988b3d6befa7de50abc2d76f26eb09"
HF_CACHE = Path("/tank/ai/models/qwen36-27b-fp8/hf-cache")
# The profile mounts HF_CACHE as HF_HOME and explicitly places HF_HUB_CACHE at
# HF_HOME/hub inside the container. Inspect the same host-side layout.
MODEL_CACHE_REPO = HF_CACHE / "hub" / "models--Qwen--Qwen3.6-27B-FP8"
REQUIRED_CACHE_FILES = frozenset({"config.json", "model.safetensors.index.json"})
OWNERSHIP_LABELS = {
    "ai.vela.managed": "true",
    "ai.vela.profile": PROFILE_NAME,
}
SNAPSHOT_SCHEMA = "vela.oxcart-live-baseline.v1"
MAX_IDLE_GPU_MEMORY_MIB = 1024
GPU_RESTORE_TOLERANCE_MIB = 512
STOP_TIMEOUT_SECONDS = 90


class GuardError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class ContainerState:
    container_id: str
    name: str
    image: str
    status: str
    running: bool
    labels: dict[str, str]
    started_at: str = ""
    restart_count: int = 0
    exit_code: int = 0


@dataclasses.dataclass(frozen=True)
class GuardState:
    hostname: str
    listening_ports: frozenset[int]
    image_repo_digests: tuple[str, ...]
    cache_main_ref: str | None
    cache_files: frozenset[str]
    gpu_memory_used_mib: tuple[int, ...]
    gpu_compute_processes: tuple[str, ...]
    containers: tuple[ContainerState, ...]


class CleanupProbe(Protocol):
    def hostname(self) -> str: ...

    def validation_container(self) -> ContainerState | None: ...

    def container_by_id(self, container_id: str) -> ContainerState | None: ...

    def stop_container(self, container_id: str, timeout_seconds: int) -> None: ...

    def remove_container(self, container_id: str) -> None: ...


def validate_preflight(state: GuardState) -> dict[str, Any]:
    """Validate a collected state without mutating it and return its baseline."""

    failures: list[dict[str, str]] = []
    if state.hostname != EXPECTED_HOSTNAME:
        _failure(
            failures,
            "hostname",
            f"expected {EXPECTED_HOSTNAME}, got {state.hostname or '<empty>'}",
        )
    if VALIDATION_PORT in state.listening_ports:
        _failure(failures, "port", f"port {VALIDATION_PORT} is already listening")
    if EXPECTED_IMAGE not in state.image_repo_digests:
        _failure(failures, "image", f"exact image digest is not present: {EXPECTED_IMAGE}")
    missing_cache_files = sorted(REQUIRED_CACHE_FILES - state.cache_files)
    if missing_cache_files:
        _failure(
            failures,
            "cache",
            "immutable cache snapshot is missing: " + ", ".join(missing_cache_files),
        )
    if not state.gpu_memory_used_mib:
        _failure(failures, "gpu-baseline", "no NVIDIA GPU memory samples were returned")
    elif any(value > MAX_IDLE_GPU_MEMORY_MIB for value in state.gpu_memory_used_mib):
        _failure(
            failures,
            "gpu-baseline",
            f"idle GPU memory must be <= {MAX_IDLE_GPU_MEMORY_MIB} MiB per GPU; "
            f"got {list(state.gpu_memory_used_mib)}",
        )
    if state.gpu_compute_processes:
        _failure(
            failures,
            "gpu-processes",
            "GPU compute processes are active: " + "; ".join(state.gpu_compute_processes),
        )

    validation = _validation_container(state.containers)
    if validation is not None:
        if not _has_ownership_labels(validation):
            _failure(
                failures,
                "container-ownership",
                f"{VALIDATION_CONTAINER} exists without both exact Vela ownership labels",
            )
        else:
            _failure(
                failures,
                "container-residue",
                f"owned validation container still exists ({validation.status}); "
                "run the explicit cleanup action before preflight",
            )

    snapshot = _baseline_snapshot(state)
    return {
        "ok": not failures,
        "phase": "preflight",
        "failures": failures,
        "snapshot": snapshot,
    }


def validate_postflight(
    state: GuardState,
    baseline: dict[str, Any],
) -> dict[str, Any]:
    """Check that the validation lane restored its baseline without mutating it."""

    _validate_baseline(baseline)
    failures: list[dict[str, str]] = []
    if state.hostname != EXPECTED_HOSTNAME:
        _failure(
            failures,
            "hostname",
            f"expected {EXPECTED_HOSTNAME}, got {state.hostname or '<empty>'}",
        )
    if VALIDATION_PORT in state.listening_ports:
        _failure(failures, "port-residue", f"port {VALIDATION_PORT} is still listening")
    if state.gpu_compute_processes:
        _failure(
            failures,
            "gpu-residue",
            "GPU compute processes remain: " + "; ".join(state.gpu_compute_processes),
        )

    baseline_memory = _int_list(baseline.get("gpu_memory_used_mib"))
    if len(state.gpu_memory_used_mib) != len(baseline_memory):
        _failure(
            failures,
            "gpu-residue",
            "GPU sample count changed from "
            f"{len(baseline_memory)} to {len(state.gpu_memory_used_mib)}",
        )
    elif any(
        current > max(MAX_IDLE_GPU_MEMORY_MIB, original + GPU_RESTORE_TOLERANCE_MIB)
        for current, original in zip(state.gpu_memory_used_mib, baseline_memory, strict=True)
    ):
        _failure(
            failures,
            "gpu-residue",
            f"GPU memory did not return to baseline: before={baseline_memory}, "
            f"after={list(state.gpu_memory_used_mib)}",
        )

    validation = _validation_container(state.containers)
    if validation is not None:
        ownership = "owned" if _has_ownership_labels(validation) else "foreign"
        _failure(
            failures,
            "container-residue",
            f"{ownership} validation container remains: id={validation.container_id} "
            f"status={validation.status}",
        )

    before = baseline.get("unrelated_containers")
    after = _unrelated_container_snapshot(state.containers)
    if before != after:
        _failure(
            failures,
            "unrelated-containers",
            "unrelated container identities or states changed during validation",
        )
    return {
        "ok": not failures,
        "phase": "postflight",
        "failures": failures,
        "before": before,
        "after": after,
    }


def cleanup_validation_container(probe: CleanupProbe) -> dict[str, Any]:
    """Explicitly stop/remove only the doubly-labelled validation container."""

    hostname = probe.hostname().split(".", 1)[0].lower()
    if hostname != EXPECTED_HOSTNAME:
        raise GuardError(
            f"cleanup is restricted to hostname {EXPECTED_HOSTNAME}; got {hostname or '<empty>'}"
        )
    container = probe.validation_container()
    if container is None:
        return {
            "cleaned": False,
            "container_id": None,
            "stopped": False,
            "removed": False,
        }
    _require_owned_validation_container(container)

    container_id = container.container_id
    stopped = False
    if container.running:
        probe.stop_container(container_id, STOP_TIMEOUT_SECONDS)
        stopped = True

    current = probe.container_by_id(container_id)
    if current is None:
        return {
            "cleaned": True,
            "container_id": container_id,
            "stopped": stopped,
            "removed": True,
        }
    _require_owned_validation_container(current)
    if current.container_id != container_id:
        raise GuardError("validation container identity changed before removal")
    probe.remove_container(container_id)
    return {
        "cleaned": True,
        "container_id": container_id,
        "stopped": stopped,
        "removed": True,
    }


class RealProbe:
    """Read live host state; mutations are exposed only for explicit cleanup."""

    def hostname(self) -> str:
        return socket.gethostname().split(".", 1)[0].lower()

    def collect_state(self) -> GuardState:
        cache_main_ref, cache_files = self._cache_state()
        gpu_memory, gpu_processes = self._gpu_state()
        return GuardState(
            hostname=self.hostname(),
            listening_ports=self._listening_ports(),
            image_repo_digests=self._image_repo_digests(),
            cache_main_ref=cache_main_ref,
            cache_files=cache_files,
            gpu_memory_used_mib=gpu_memory,
            gpu_compute_processes=gpu_processes,
            containers=self._containers(),
        )

    def validation_container(self) -> ContainerState | None:
        return self._inspect_container(VALIDATION_CONTAINER)

    def container_by_id(self, container_id: str) -> ContainerState | None:
        return self._inspect_container(container_id)

    def stop_container(self, container_id: str, timeout_seconds: int) -> None:
        self._run(
            ["docker", "container", "stop", "--time", str(timeout_seconds), container_id],
            operation="stop owned validation container",
        )

    def remove_container(self, container_id: str) -> None:
        self._run(
            ["docker", "container", "rm", container_id],
            operation="remove owned validation container",
        )

    def _listening_ports(self) -> frozenset[int]:
        try:
            ports = {
                int(connection.laddr.port)
                for connection in psutil.net_connections(kind="inet")
                if connection.status == psutil.CONN_LISTEN and connection.laddr
            }
        except (OSError, psutil.Error) as exc:
            raise GuardError(f"unable to inspect listening ports: {exc}") from exc
        return frozenset(ports)

    def _image_repo_digests(self) -> tuple[str, ...]:
        completed = self._run(
            ["docker", "image", "inspect", EXPECTED_IMAGE],
            operation="inspect pinned image",
            allow_failure=True,
        )
        if completed.returncode != 0:
            return ()
        payload = _json_list(completed.stdout, "docker image inspect")
        if not payload:
            return ()
        repo_digests = payload[0].get("RepoDigests") or []
        return tuple(sorted(str(item) for item in repo_digests))

    def _cache_state(self) -> tuple[str | None, frozenset[str]]:
        ref_path = MODEL_CACHE_REPO / "refs" / "main"
        try:
            cache_main_ref = ref_path.read_text(encoding="utf-8").strip()
        except OSError:
            cache_main_ref = None
        # refs/main is mutable. The profile launches the immutable commit, so
        # validate that exact snapshot directly and retain main only as evidence.
        snapshot = MODEL_CACHE_REPO / "snapshots" / MODEL_REVISION
        present = frozenset(name for name in REQUIRED_CACHE_FILES if (snapshot / name).is_file())
        return cache_main_ref, present

    def _gpu_state(self) -> tuple[tuple[int, ...], tuple[str, ...]]:
        memory_result = self._run(
            [
                "nvidia-smi",
                "--query-gpu=memory.used",
                "--format=csv,noheader,nounits",
            ],
            operation="sample GPU memory",
        )
        try:
            memory = tuple(
                int(line.strip().split()[0])
                for line in memory_result.stdout.splitlines()
                if line.strip()
            )
        except (IndexError, ValueError) as exc:
            raise GuardError("nvidia-smi returned invalid GPU memory data") from exc

        process_result = self._run(
            [
                "nvidia-smi",
                "--query-compute-apps=pid,process_name,used_gpu_memory",
                "--format=csv,noheader,nounits",
            ],
            operation="sample GPU compute processes",
        )
        processes = tuple(
            line.strip() for line in process_result.stdout.splitlines() if line.strip()
        )
        return memory, processes

    def _containers(self) -> tuple[ContainerState, ...]:
        listed = self._run(
            ["docker", "container", "ls", "-aq", "--no-trunc"],
            operation="list containers",
        )
        container_ids = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
        if not container_ids:
            return ()
        inspected = self._run(
            ["docker", "container", "inspect", *container_ids],
            operation="inspect containers",
        )
        payload = _json_list(inspected.stdout, "docker inspect")
        return tuple(_container_from_inspect(item) for item in payload)

    def _inspect_container(self, reference: str) -> ContainerState | None:
        inspected = self._run(
            ["docker", "container", "inspect", reference],
            operation=f"inspect container {reference}",
            allow_failure=True,
        )
        if inspected.returncode != 0:
            combined = f"{inspected.stdout}\n{inspected.stderr}".lower()
            if "no such" in combined:
                return None
            raise GuardError(
                f"unable to inspect container {reference}: {inspected.stderr.strip()}"
            )
        payload = _json_list(inspected.stdout, "docker container inspect")
        if len(payload) != 1:
            raise GuardError(f"expected one inspected container for {reference}")
        return _container_from_inspect(payload[0])

    @staticmethod
    def _run(
        argv: list[str],
        *,
        operation: str,
        allow_failure: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        try:
            completed = subprocess.run(
                argv,
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GuardError(f"unable to {operation}: {exc}") from exc
        if completed.returncode != 0 and not allow_failure:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise GuardError(f"unable to {operation}: {detail}")
        return completed


def _baseline_snapshot(state: GuardState) -> dict[str, Any]:
    return {
        "schema": SNAPSHOT_SCHEMA,
        "hostname": state.hostname,
        "cache_main_ref": state.cache_main_ref,
        "model_snapshot_revision": MODEL_REVISION,
        "gpu_memory_used_mib": list(state.gpu_memory_used_mib),
        "unrelated_containers": _unrelated_container_snapshot(state.containers),
    }


def _unrelated_container_snapshot(
    containers: tuple[ContainerState, ...],
) -> dict[str, dict[str, object]]:
    return {
        container.name: {
            "container_id": container.container_id,
            "image": container.image,
            "status": container.status,
            "running": container.running,
            "started_at": container.started_at,
            "restart_count": container.restart_count,
            "exit_code": container.exit_code,
        }
        for container in sorted(containers, key=lambda item: item.name)
        if container.name != VALIDATION_CONTAINER
    }


def _validation_container(
    containers: tuple[ContainerState, ...],
) -> ContainerState | None:
    matches = [item for item in containers if item.name == VALIDATION_CONTAINER]
    if len(matches) > 1:
        raise GuardError(f"multiple containers unexpectedly use name {VALIDATION_CONTAINER}")
    return matches[0] if matches else None


def _has_ownership_labels(container: ContainerState) -> bool:
    return all(container.labels.get(key) == value for key, value in OWNERSHIP_LABELS.items())


def _require_owned_validation_container(container: ContainerState) -> None:
    if container.name != VALIDATION_CONTAINER or not _has_ownership_labels(container):
        raise GuardError(
            f"refusing cleanup: {VALIDATION_CONTAINER} must retain both ownership labels"
        )


def _container_from_inspect(payload: dict[str, Any]) -> ContainerState:
    raw_config = payload.get("Config")
    config: dict[str, Any] = dict(raw_config) if isinstance(raw_config, dict) else {}
    raw_state = payload.get("State")
    state: dict[str, Any] = dict(raw_state) if isinstance(raw_state, dict) else {}
    raw_labels = config.get("Labels")
    labels = dict(raw_labels) if isinstance(raw_labels, dict) else {}
    return ContainerState(
        container_id=str(payload.get("Id") or ""),
        name=str(payload.get("Name") or "").removeprefix("/"),
        image=str(config.get("Image") or ""),
        status=str(state.get("Status") or "unknown"),
        running=bool(state.get("Running")),
        labels={str(key): str(value) for key, value in labels.items()},
        started_at=str(state.get("StartedAt") or ""),
        restart_count=int(payload.get("RestartCount") or 0),
        exit_code=int(state.get("ExitCode") or 0),
    )


def _json_list(raw: str, operation: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GuardError(f"{operation} returned invalid JSON") from exc
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise GuardError(f"{operation} did not return a JSON object list")
    return payload


def _validate_baseline(baseline: dict[str, Any]) -> None:
    if baseline.get("schema") != SNAPSHOT_SCHEMA:
        raise GuardError(f"baseline schema must be {SNAPSHOT_SCHEMA}")
    if baseline.get("hostname") != EXPECTED_HOSTNAME:
        raise GuardError(f"baseline hostname must be {EXPECTED_HOSTNAME}")
    if baseline.get("model_snapshot_revision") != MODEL_REVISION:
        raise GuardError(f"baseline model_snapshot_revision must be {MODEL_REVISION}")
    if baseline.get("cache_main_ref") is not None and not isinstance(
        baseline.get("cache_main_ref"), str
    ):
        raise GuardError("baseline cache_main_ref must be a string or null")
    if not isinstance(baseline.get("unrelated_containers"), dict):
        raise GuardError("baseline unrelated_containers must be a mapping")
    _int_list(baseline.get("gpu_memory_used_mib"))


def _int_list(value: object) -> list[int]:
    if not isinstance(value, list) or not value or not all(
        isinstance(item, int) and not isinstance(item, bool) for item in value
    ):
        raise GuardError("baseline gpu_memory_used_mib must be a non-empty integer list")
    return list(value)


def _failure(failures: list[dict[str, str]], check: str, detail: str) -> None:
    failures.append({"check": check, "detail": detail})


def _write_snapshot(path: Path, snapshot: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _load_snapshot(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GuardError(f"unable to read baseline {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise GuardError("baseline root must be a JSON object")
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only by default: validate the isolated Oxcart live lane and "
            "optionally compare or explicitly clean its labelled container."
        )
    )
    subparsers = parser.add_subparsers(dest="action")
    preflight = subparsers.add_parser("preflight", help="read-only preflight")
    preflight.add_argument("--snapshot", type=Path)
    postflight = subparsers.add_parser("postflight", help="read-only residue check")
    postflight.add_argument("--snapshot", type=Path, required=True)
    subparsers.add_parser("cleanup", help="explicitly remove the doubly-labelled container")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    probe = RealProbe()
    try:
        if args.action == "cleanup":
            result = cleanup_validation_container(probe)
            print(json.dumps({"ok": True, "phase": "cleanup", **result}, sort_keys=True))
            return 0

        state = probe.collect_state()
        if args.action == "postflight":
            report = validate_postflight(state, _load_snapshot(args.snapshot))
        else:
            report = validate_preflight(state)
            snapshot_path = getattr(args, "snapshot", None)
            if snapshot_path is not None and report["ok"]:
                _write_snapshot(snapshot_path, report["snapshot"])
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report["ok"] else 2
    except GuardError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
