from __future__ import annotations

import asyncio
import signal
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vllm_loader import __version__
from vllm_loader.config.loader import ConfigRegistry, InvalidConfig, ValidConfig, load_registry
from vllm_loader.config.schema import ModelConfig
from vllm_loader.engine.command_builder import CommandBuildResult, build_command
from vllm_loader.engine.log_sink import LogRecord, level_for_line
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
    select_profile,
    select_profile_for_config,
)
from vllm_loader.engine.sidecar import (
    Manifest,
    Sidecar,
    discover_active_sidecars,
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
    config: ModelConfig
    fsm: PhaseFSM
    intentional_shutdown: bool = False


@dataclass(frozen=True)
class LocalDetachedRunSummary:
    run_id: str
    sidecar_path: Path
    config_name: str


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
        self._event_sequences: dict[str, int] = {}
        self._event_buffers: dict[str, list[dict[str, Any]]] = {}
        self._event_buffer_size = 5000
        self._subscribers: dict[str, list[asyncio.Queue[dict[str, Any]]]] = {}

    def handle(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | Awaitable[dict[str, Any]]:
        payload = params or {}
        if method == "handshake":
            return self._handshake()
        if method == "list_configs":
            return self._list_configs(payload)
        if method == "preview":
            return self._preview(payload)
        if method == "prepare_launch":
            return self._prepare_launch(payload)
        if method == "launch":
            return self._launch(payload)
        if method == "wait":
            return self._wait(payload)
        if method == "stop":
            return self._stop(payload)
        if method == "kill":
            return self._kill(payload)
        if method == "probe_until_ready":
            return self._probe_until_ready(payload)
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
                "launch",
                "wait",
                "stop",
                "kill",
                "probe_until_ready",
                "subscribe",
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

    def _launch(self, params: dict[str, Any]) -> dict[str, Any]:
        prepared = self._prepare_launch(params)
        cfg = ModelConfig.model_validate(prepared["config"])
        requested_run_id = params.get("run_id")
        run_id = str(requested_run_id) if requested_run_id is not None else None
        if run_id is not None and run_id in self._attached_runs:
            return {
                "run_id": run_id,
                "launch_mode": "attached",
                "status": "already-running",
            }
        if cfg.launch.mode.value == "detached":
            launch = self.start_detached_run(prepared)
            return {
                "run_id": launch.run_id,
                "launch_mode": "detached",
                "status": "started",
            }
        run = self.start_attached_run(prepared, run_id=run_id)
        return {
            "run_id": run.run_id,
            "launch_mode": "attached",
            "status": "started",
        }

    async def _wait(self, params: dict[str, Any]) -> dict[str, Any]:
        run_id = _run_id_param(params)
        return await self._wait_attached_run_payload(run_id)

    def _stop(self, params: dict[str, Any]) -> dict[str, Any]:
        run_id = _run_id_param(params)
        interrupt_timeout = float(params.get("interrupt_timeout", 5))
        terminate_timeout = float(params.get("terminate_timeout", 5))
        self.stop_run(
            run_id,
            interrupt_timeout=interrupt_timeout,
            terminate_timeout=terminate_timeout,
        )
        return {"run_id": run_id, "signaled": True}

    def _kill(self, params: dict[str, Any]) -> dict[str, Any]:
        run_id = _run_id_param(params)
        self.kill_run(run_id)
        return {"run_id": run_id, "signaled": True}

    async def _probe_until_ready(self, params: dict[str, Any]) -> dict[str, Any]:
        run_id = _run_id_param(params)
        run_config, fsm = self._run_config_and_fsm_or_error(run_id)
        last_event: dict[str, Any] = {}
        completed = asyncio.Event()

        def capture(event: HealthEvent) -> None:
            last_event.clear()
            last_event.update(_health_payload(event))
            self._publish_health_events(run_id, run_config, fsm, event)
            if event.ready or event.error_kind is not None:
                completed.set()

        probe_task = asyncio.create_task(
            probe_loop(
                run_config,
                emit=capture,
                is_process_alive=lambda: self.is_run_alive(run_id),
            )
        )
        completed_task = asyncio.create_task(completed.wait())
        done, _pending = await asyncio.wait(
            {probe_task, completed_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if completed_task in done:
            probe_task.cancel()
        else:
            completed_task.cancel()
        return {"run_id": run_id, **last_event}

    def start_attached_run(
        self,
        prepared: dict[str, Any],
        *,
        run_id: str | None = None,
        emit: Callable[[LogRecord], None] | None = None,
        emit_event: Callable[[AgentEvent], None] | None = None,
    ) -> LocalAttachedRun:
        cfg = ModelConfig.model_validate(prepared["config"])
        build = _build_result_from_payload(prepared["build"])
        run_id = run_id or uuid.uuid4().hex
        existing = self._attached_runs.get(run_id)
        if existing is not None:
            return existing
        fsm = PhaseFSM(select_profile_for_config(cfg))
        run_dir = cfg.run_artifacts_dir
        run_dir.mkdir(parents=True, exist_ok=True)
        secrets = [cfg.server.api_key or "", cfg.env.get("HF_TOKEN", "")]

        def emit_record(record: LogRecord) -> None:
            if emit is not None:
                emit(record)
            if emit_event is not None:
                events = _events_from_log_record(run_id, fsm, record)
                for event in events:
                    emit_event(event)
            else:
                events = _events_from_log_record(run_id, fsm, record)
            for event in events:
                self._publish_event(event)

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
            config=_config_from_detached_sidecar(sidecar),
            fsm=PhaseFSM(select_profile(sidecar.vllm_version_profile)),
        )
        self._detached_runs[run.run_id] = run
        return run

    def discover_detached_runs(
        self, runs_dirs: list[Path | str]
    ) -> list[LocalDetachedRunSummary]:
        summaries: list[LocalDetachedRunSummary] = []
        for path in discover_active_sidecars([Path(item) for item in runs_dirs]):
            sidecar = load_sidecar(path)
            summaries.append(
                LocalDetachedRunSummary(
                    run_id=sidecar.run_id,
                    sidecar_path=path,
                    config_name=sidecar.config_name,
                )
            )
        return summaries

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
        detached.intentional_shutdown = True
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
        detached.intentional_shutdown = True
        signal_sidecar_from_system(detached.sidecar_path, signal.SIGKILL)

    async def wait_attached_run(self, run_id: str) -> tuple[int | None, bool]:
        result = await self._wait_attached_run_payload(run_id)
        return result["returncode"], bool(result["intentional"])

    async def _wait_attached_run_payload(self, run_id: str) -> dict[str, Any]:
        run = self._attached_run_or_error(run_id)
        returncode = await run.process.read_loop()
        intentional = run.intentional_shutdown
        previous_phase = run.fsm.phase
        run.fsm.process_exited(returncode, intentional=intentional)
        phase_event = _phase_event_from_transition(run_id, run.fsm, previous_phase)
        if phase_event is not None:
            self._publish_event(phase_event)
        self._publish_event(
            AgentEvent(
                "exited",
                run_id,
                {
                    "returncode": returncode,
                    "intentional": intentional,
                    "phase": run.fsm.phase.value,
                },
            )
        )
        self._attached_runs.pop(run_id, None)
        return {
            "run_id": run_id,
            "returncode": returncode,
            "intentional": intentional,
        }

    async def probe_run_until_ready(
        self, run_id: str, *, emit: Callable[[HealthEvent], None]
    ) -> None:
        run_config, fsm = self._run_config_and_fsm_or_error(run_id)

        def publish_and_emit(event: HealthEvent) -> None:
            emit(event)
            self._publish_health_events(run_id, run_config, fsm, event)

        await probe_loop(
            run_config,
            emit=publish_and_emit,
            is_process_alive=lambda: self.is_run_alive(run_id),
        )

    async def tail_detached_run(
        self,
        run_id: str,
        *,
        emit_event: Callable[[AgentEvent], None],
        start_position: int | None = None,
        poll_interval: float = 0.25,
    ) -> None:
        run = self._detached_run_or_error(run_id)
        log_path = Path(run.manifest.active_log.path)
        position = (
            start_position
            if start_position is not None
            else log_path.stat().st_size
            if log_path.exists()
            else 0
        )
        pending = ""
        while self.is_run_alive(run_id):
            if log_path.exists():
                with log_path.open("r", encoding="utf-8", errors="replace") as file:
                    file.seek(position)
                    chunk = file.read()
                    position = file.tell()
                if chunk:
                    pending += chunk
                    *lines, pending = pending.split("\n")
                    for line in lines:
                        if line:
                            record = LogRecord(
                                "committed",
                                line,
                                level=level_for_line(line),
                            )
                            for event in _events_from_log_record(
                                run_id, run.fsm, record
                            ):
                                emit_event(event)
            await asyncio.sleep(poll_interval)
        previous_phase = run.fsm.phase
        run.fsm.process_exited(None, intentional=run.intentional_shutdown)
        event = _phase_event_from_transition(run_id, run.fsm, previous_phase)
        if event is not None:
            emit_event(event)

    def sample_gpus(self) -> GpuPollResult:
        return self._gpu_sampler()

    async def subscribe_run(
        self,
        run_ids: list[str] | tuple[str, ...] | set[str],
        *,
        resume_from: object = "live",
    ) -> AsyncIterator[dict[str, Any]]:
        selected_run_ids = {str(run_id) for run_id in run_ids}
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        for run_id in selected_run_ids:
            self._subscribers.setdefault(run_id, []).append(queue)
        try:
            for event in self._replay_events(selected_run_ids, resume_from):
                yield event
            while True:
                yield await queue.get()
        finally:
            for run_id in selected_run_ids:
                queues = self._subscribers.get(run_id, [])
                if queue in queues:
                    queues.remove(queue)
                if not queues:
                    self._subscribers.pop(run_id, None)

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

    def _run_config_and_fsm_or_error(self, run_id: str) -> tuple[ModelConfig, PhaseFSM]:
        attached = self._attached_runs.get(run_id)
        if attached is not None:
            return attached.config, attached.fsm
        detached = self._detached_run_or_error(run_id)
        return detached.config, detached.fsm

    def _publish_event(self, event: AgentEvent) -> dict[str, Any]:
        wire_event = self._wire_event(event)
        buffer = self._event_buffers.setdefault(event.run_id, [])
        buffer.append(wire_event)
        del buffer[:- self._event_buffer_size]
        for queue in list(self._subscribers.get(event.run_id, [])):
            queue.put_nowait(wire_event)
        return wire_event

    def _wire_event(self, event: AgentEvent) -> dict[str, Any]:
        seq = self._event_sequences.get(event.run_id, 0) + 1
        self._event_sequences[event.run_id] = seq
        return {
            "event": event.kind,
            "run_id": event.run_id,
            "seq": seq,
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "mono": time.monotonic(),
            **event.payload,
        }

    def _replay_events(
        self, run_ids: set[str], resume_from: object
    ) -> list[dict[str, Any]]:
        if resume_from == "live":
            return []
        min_seq = 0
        if isinstance(resume_from, dict):
            try:
                min_seq = int(resume_from.get("seq", 0))
            except (TypeError, ValueError):
                min_seq = 0
        events: list[dict[str, Any]] = []
        for run_id in run_ids:
            for event in self._event_buffers.get(run_id, []):
                if event["seq"] > min_seq:
                    events.append(event)
        return sorted(events, key=lambda item: (str(item["run_id"]), int(item["seq"])))

    def _publish_health_events(
        self, run_id: str, cfg: ModelConfig, fsm: PhaseFSM, event: HealthEvent
    ) -> None:
        self._publish_event(AgentEvent("health", run_id, _health_payload(event)))
        previous_phase = fsm.phase
        if event.ready:
            models = event.models or []
            fsm.health_ready(models)
            phase_event = _phase_event_from_transition(run_id, fsm, previous_phase)
            if phase_event is not None:
                self._publish_event(phase_event)
            self._publish_event(
                AgentEvent(
                    "ready",
                    run_id,
                    {
                        "models": models,
                        "bind_host": cfg.server.host,
                        "port": cfg.server.port,
                        "reachable_url": _reachable_url(cfg),
                    },
                )
            )
            return
        if event.error_kind is not None:
            fsm.health_error(event.error_kind, event.detail)
        else:
            fsm.health_failed(event.detail)
        phase_event = _phase_event_from_transition(run_id, fsm, previous_phase)
        if phase_event is not None:
            self._publish_event(phase_event)


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
    phase_event = _phase_event_from_transition(run_id, fsm, previous_phase)
    if phase_event is not None:
        events.append(phase_event)
    return events


def _phase_event_from_transition(
    run_id: str, fsm: PhaseFSM, previous_phase
) -> AgentEvent | None:
    if fsm.phase is previous_phase:
        return None
    payload: dict[str, Any] = {
        "phase": fsm.phase.value,
        "prev_phase": previous_phase.value,
    }
    if fsm.error_kind is not None:
        payload["error_kind"] = fsm.error_kind.value
    if fsm.error_excerpt is not None:
        payload["error_excerpt"] = fsm.error_excerpt
    return AgentEvent("phase", run_id, payload)


def _detached_run_alive(run: LocalDetachedRun) -> bool:
    try:
        return verify_sidecar_from_system(run.sidecar_path)
    except Exception:
        return False


def _config_from_detached_sidecar(sidecar: Sidecar) -> ModelConfig:
    if sidecar.config_snapshot:
        return ModelConfig.model_validate(sidecar.config_snapshot)
    return ModelConfig.model_validate(
        {
            "name": sidecar.config_name,
            "model": sidecar.served_model_names[0]
            if sidecar.served_model_names
            else sidecar.config_name,
            "server": {
                "host": sidecar.host,
                "port": sidecar.port,
                "exposure": sidecar.exposure,
            },
            "served_model_name": sidecar.served_model_names[0]
            if sidecar.served_model_names
            else None,
            "launch": {"mode": sidecar.launch_mode},
        }
    )


def _configs_dir(params: dict[str, Any]) -> Path | None:
    value = params.get("configs_dir")
    if value is None:
        return None
    return Path(str(value))


def _run_id_param(params: dict[str, Any]) -> str:
    value = params.get("run_id")
    if not isinstance(value, str) or not value.strip():
        raise TargetCallError("invalid-params", "run_id is required")
    return value


def _health_payload(event: HealthEvent) -> dict[str, Any]:
    return {
        "ready": event.ready,
        "detail": event.detail,
        "models": list(event.models or []),
        "error_kind": event.error_kind.value if event.error_kind is not None else None,
    }


def _reachable_url(cfg: ModelConfig) -> str:
    return f"http://{cfg.server.host}:{cfg.server.port}"


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
