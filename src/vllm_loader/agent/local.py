from __future__ import annotations

import signal
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vllm_loader import __version__
from vllm_loader.config.loader import ConfigRegistry, InvalidConfig, ValidConfig, load_registry
from vllm_loader.config.schema import ModelConfig
from vllm_loader.engine.command_builder import CommandBuildResult, build_command
from vllm_loader.engine.log_sink import LogRecord
from vllm_loader.engine.phases import PhaseFSM
from vllm_loader.engine.preflight import check_launch_preflight
from vllm_loader.engine.process_manager import (
    AttachedProcess,
    DetachedLaunch,
    start_attached,
    start_detached,
)
from vllm_loader.engine.profile import (
    VllmProfileError,
    detect_vllm_version_for_config,
    select_profile_for_config,
)
from vllm_loader.engine.sidecar import (
    Manifest,
    Sidecar,
    load_manifest,
    load_sidecar,
    signal_sidecar_from_system,
    stop_sidecar_from_system,
    verify_sidecar_from_system,
)
from vllm_loader.monitoring.gpu import GpuPollResult
from vllm_loader.monitoring.gpu import sample_gpus as default_gpu_sampler
from vllm_loader.monitoring.health import HealthEvent, probe_loop

PROTOCOL_VERSION = 1


@dataclass
class TargetCallError(RuntimeError):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class AgentEvent:
    kind: str
    run_id: str
    payload: dict[str, Any]


@dataclass
class LocalAttachedRun:
    run_id: str
    config: ModelConfig
    build: CommandBuildResult
    process: AttachedProcess
    fsm: PhaseFSM
    intentional_shutdown: bool = False


@dataclass
class LocalDetachedRun:
    run_id: str
    sidecar_path: Path
    sidecar: Sidecar
    manifest: Manifest


class LocalAgent:
    def __init__(
        self,
        *,
        target_name: str = "local",
        gpu_sampler: Callable[[], GpuPollResult] = default_gpu_sampler,
    ) -> None:
        self.target_name = target_name
        self._gpu_sampler = gpu_sampler
        self._attached_runs: dict[str, LocalAttachedRun] = {}
        self._detached_runs: dict[str, LocalDetachedRun] = {}

    def handle(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = params or {}
        if method == "handshake":
            return self._handshake()
        if method == "list_configs":
            return self._list_configs(payload)
        if method == "preview":
            return self._preview(payload)
        if method == "prepare_launch":
            return self._prepare_launch(payload)
        raise TargetCallError("method-not-found", f"unknown agent method: {method}")

    def _handshake(self) -> dict[str, Any]:
        return {
            "agent_version": __version__,
            "protocol_version": PROTOCOL_VERSION,
            "target": self.target_name,
            "capabilities": [
                "handshake",
                "list_configs",
                "preview",
                "prepare_launch",
            ],
        }

    def _list_configs(self, params: dict[str, Any]) -> dict[str, Any]:
        registry = load_registry(_configs_dir(params))
        return {
            "valid": [_valid_config_payload(item) for item in registry.valid],
            "invalid": [_invalid_config_payload(item) for item in registry.invalid],
        }

    def _preview(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or not name.strip():
            raise TargetCallError("invalid-params", "preview requires config name")
        registry = load_registry(_configs_dir(params))
        cfg = _config_by_name(registry, name)
        try:
            result = build_command(cfg, select_profile_for_config(cfg))
        except VllmProfileError as exc:
            raise TargetCallError("profile-error", str(exc)) from exc
        return {
            "preview": result.preview,
            "warnings": list(result.warnings),
            "metadata": dict(result.metadata),
        }

    def _prepare_launch(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or not name.strip():
            raise TargetCallError("invalid-params", "prepare_launch requires config name")
        registry = load_registry(_configs_dir(params))
        cfg = _config_by_name(registry, name)
        try:
            result = build_command(cfg, select_profile_for_config(cfg))
        except VllmProfileError as exc:
            raise TargetCallError("profile-error", str(exc)) from exc
        failure = check_launch_preflight(cfg, cwd=result.cwd)
        if failure is not None:
            raise TargetCallError(
                "preflight-failed",
                failure.detail,
                {"kind": failure.kind.value, "detail": failure.detail},
            )
        return {
            "config": cfg.model_dump(mode="json"),
            "build": _build_payload(result),
            "preflight": None,
        }

    def start_attached_run(
        self,
        prepared: dict[str, Any],
        *,
        emit: Callable[[LogRecord], None] | None = None,
        emit_event: Callable[[AgentEvent], None] | None = None,
    ) -> LocalAttachedRun:
        cfg = ModelConfig.model_validate(prepared["config"])
        build = _build_result_from_payload(prepared["build"])
        run_id = uuid.uuid4().hex
        fsm = PhaseFSM(select_profile_for_config(cfg))
        run_dir = cfg.run_artifacts_dir
        run_dir.mkdir(parents=True, exist_ok=True)
        secrets = [cfg.server.api_key or "", cfg.env.get("HF_TOKEN", "")]

        def emit_record(record: LogRecord) -> None:
            if emit is not None:
                emit(record)
            if emit_event is not None:
                for event in _events_from_log_record(run_id, fsm, record):
                    emit_event(event)

        try:
            process = start_attached(
                build,
                log_path=run_dir / f"{cfg.name}.run.log",
                secrets=secrets,
                emit=emit_record,
            )
        except FileNotFoundError as exc:
            command = str(exc.filename or build.argv[0])
            raise TargetCallError(
                "command-not-found",
                f"Command not found: {command}",
                {"command": command, "fallback": build.argv[0]},
            ) from exc
        run = LocalAttachedRun(
            run_id=run_id,
            config=cfg,
            build=build,
            process=process,
            fsm=fsm,
        )
        self._attached_runs[run.run_id] = run
        return run

    def start_detached_run(self, prepared: dict[str, Any]) -> DetachedLaunch:
        cfg = ModelConfig.model_validate(prepared["config"])
        build = _build_result_from_payload(prepared["build"])
        secrets = [cfg.server.api_key or "", cfg.env.get("HF_TOKEN", "")]
        try:
            return start_detached(
                cfg,
                build,
                secrets=secrets,
                vllm_version=detect_vllm_version_for_config(cfg),
                vllm_version_profile=build.metadata.get("vllm_version_profile"),
            )
        except FileNotFoundError as exc:
            command = str(exc.filename or build.argv[0])
            raise TargetCallError(
                "command-not-found",
                f"Command not found: {command}",
                {"command": command, "fallback": build.argv[0]},
            ) from exc

    def reattach_detached_run(self, sidecar_path: Path | str) -> LocalDetachedRun:
        path = Path(sidecar_path)
        verify_sidecar_from_system(path)
        sidecar = load_sidecar(path)
        manifest = load_manifest(sidecar.manifest_path)
        run = LocalDetachedRun(
            run_id=sidecar.run_id,
            sidecar_path=path,
            sidecar=sidecar,
            manifest=manifest,
        )
        self._detached_runs[run.run_id] = run
        return run

    def is_run_alive(self, run_id: str) -> bool:
        run = self._attached_runs.get(run_id)
        if run is not None:
            return run.process.proc.poll() is None
        detached = self._detached_runs.get(run_id)
        if detached is not None:
            return _detached_run_alive(detached)
        return False

    def stop_run(
        self,
        run_id: str,
        *,
        interrupt_timeout: float = 5,
        terminate_timeout: float = 5,
    ) -> None:
        run = self._attached_runs.get(run_id)
        if run is not None:
            run.intentional_shutdown = True
            run.process.stop(
                interrupt_timeout=interrupt_timeout,
                terminate_timeout=terminate_timeout,
            )
            return
        detached = self._detached_run_or_error(run_id)
        stop_sidecar_from_system(
            detached.sidecar_path,
            interrupt_timeout=interrupt_timeout,
            terminate_timeout=terminate_timeout,
        )

    def kill_run(self, run_id: str) -> None:
        run = self._attached_runs.get(run_id)
        if run is not None:
            run.intentional_shutdown = True
            run.process.kill()
            return
        detached = self._detached_run_or_error(run_id)
        signal_sidecar_from_system(detached.sidecar_path, signal.SIGKILL)

    async def wait_attached_run(self, run_id: str) -> tuple[int | None, bool]:
        run = self._attached_run_or_error(run_id)
        returncode = await run.process.read_loop()
        intentional = run.intentional_shutdown
        self._attached_runs.pop(run_id, None)
        return returncode, intentional

    async def probe_run_until_ready(
        self, run_id: str, *, emit: Callable[[HealthEvent], None]
    ) -> None:
        run = self._attached_run_or_error(run_id)
        await probe_loop(
            run.config,
            emit=emit,
            is_process_alive=lambda: self.is_run_alive(run_id),
        )

    def sample_gpus(self) -> GpuPollResult:
        return self._gpu_sampler()

    def _attached_run_or_error(self, run_id: str) -> LocalAttachedRun:
        run = self._attached_runs.get(run_id)
        if run is None:
            raise TargetCallError("run-not-found", f"unknown run: {run_id}")
        return run

    def _detached_run_or_error(self, run_id: str) -> LocalDetachedRun:
        run = self._detached_runs.get(run_id)
        if run is None:
            raise TargetCallError("run-not-found", f"unknown run: {run_id}")
        return run


def _events_from_log_record(
    run_id: str, fsm: PhaseFSM, record: LogRecord
) -> list[AgentEvent]:
    if record.kind != "committed":
        return [
            AgentEvent(
                "progress",
                run_id,
                {"text": record.text},
            )
        ]
    events = [
        AgentEvent(
            "log",
            run_id,
            {"kind": record.kind, "text": record.text, "level": record.level},
        )
    ]
    previous_phase = fsm.phase
    fsm.feed_line(record.text)
    if fsm.phase is not previous_phase:
        payload: dict[str, Any] = {
            "phase": fsm.phase.value,
            "prev_phase": previous_phase.value,
        }
        if fsm.error_kind is not None:
            payload["error_kind"] = fsm.error_kind.value
        if fsm.error_excerpt is not None:
            payload["error_excerpt"] = fsm.error_excerpt
        events.append(AgentEvent("phase", run_id, payload))
    return events


def _detached_run_alive(run: LocalDetachedRun) -> bool:
    try:
        return verify_sidecar_from_system(run.sidecar_path)
    except Exception:
        return False


def _configs_dir(params: dict[str, Any]) -> Path | None:
    value = params.get("configs_dir")
    if value is None:
        return None
    return Path(str(value))


def _config_by_name(registry: ConfigRegistry, name: str):
    try:
        return registry.by_name(name)
    except KeyError as exc:
        invalid_matches = [
            item
            for item in registry.invalid
            if item.raw_name == name or (item.raw_name is None and item.path.stem == name)
        ]
        if invalid_matches:
            matches = [_invalid_config_payload(item) for item in invalid_matches]
            errors = "; ".join(
                f"{item.path.name}: {', '.join(item.errors)}" for item in invalid_matches
            )
            raise TargetCallError(
                "invalid-config",
                errors,
                {"name": name, "matches": matches},
            ) from exc
        available = [item.config.name for item in registry.valid]
        raise TargetCallError(
            "unknown-config",
            f"unknown config: {name}",
            {"name": name, "available": available},
        ) from exc


def _valid_config_payload(item: ValidConfig) -> dict[str, Any]:
    return {
        "path": str(item.path),
        "name": item.config.name,
        "model": item.config.model,
        "target": item.config.target,
        "warnings": list(item.warnings),
        "config": item.config.model_dump(mode="json"),
    }


def _invalid_config_payload(item: InvalidConfig) -> dict[str, Any]:
    return {
        "path": str(item.path),
        "errors": list(item.errors),
        "raw_name": item.raw_name,
    }


def _build_payload(result: CommandBuildResult) -> dict[str, Any]:
    return {
        "argv": list(result.argv),
        "env": dict(result.env),
        "cwd": str(result.cwd),
        "warnings": list(result.warnings),
        "metadata": dict(result.metadata),
        "preview": result.preview,
    }


def _build_result_from_payload(payload: dict[str, Any]) -> CommandBuildResult:
    return CommandBuildResult(
        argv=list(payload["argv"]),
        env=dict(payload["env"]),
        cwd=Path(payload["cwd"]),
        warnings=list(payload.get("warnings", [])),
        metadata=dict(payload.get("metadata", {})),
        preview=str(payload.get("preview", "")),
    )
