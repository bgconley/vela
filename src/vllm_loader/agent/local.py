from __future__ import annotations

import asyncio
import contextlib
import json
import os
import platform
import signal
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vllm_loader import __version__
from vllm_loader.config.loader import ConfigRegistry, InvalidConfig, ValidConfig, load_registry
from vllm_loader.config.schema import EntryPoint, ModelConfig, default_run_artifacts_dir
from vllm_loader.engine.build_registry import (
    BuildHandoff,
    BuildRegistryError,
    adopt_build,
    build_reference_aliases,
    default_builds_root,
    inspect_build,
    list_builds,
    remove_build,
    resolve_build_handoff,
    select_build,
    verify_build,
)
from vllm_loader.engine.command_builder import CommandBuildResult, build_command
from vllm_loader.engine.log_sink import LogRecord, level_for_line
from vllm_loader.engine.model_registry import (
    ModelHandoff,
    ModelRegistryError,
    default_models_registry_path,
    download_hf_model,
    inspect_model,
    list_models,
    mark_hf_model_partial,
    model_reference_aliases,
    pin_model,
    refresh_models,
    remove_model,
    resolve_model_handoff,
    verify_model,
)
from vllm_loader.engine.phases import PhaseFSM
from vllm_loader.engine.preflight import check_launch_preflight
from vllm_loader.engine.process_manager import (
    DetachedLaunch,
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
from vllm_loader.monitoring.gpu import GpuPollResult, GpuSample
from vllm_loader.monitoring.gpu import sample_gpus as default_gpu_sampler
from vllm_loader.monitoring.health import HealthEvent, probe_host_for, probe_loop

PROTOCOL_VERSION = 1
AGENT_CAPABILITIES = [
    "handshake",
    "ping",
    "list_configs",
    "preview",
    "prepare_launch",
    "launch",
    "wait",
    "stop",
    "kill",
    "gpu",
    "status",
    "health",
    "probe_until_ready",
    "tail_detached",
    "discover_runs",
    "discover_runs_no_paths",
    "discover_detached",
    "reattach",
    "reattach_detached",
    "list_builds",
    "adopt_build",
    "inspect_build",
    "select_build",
    "verify_build",
    "remove_build",
    "list_models",
    "pin_model",
    "refresh_models",
    "inspect_model",
    "verify_model",
    "remove_model",
    "create_build",
    "download_model",
    "cancel_job",
    "sample_gpus",
    "subscribe",
    "unsubscribe",
]

JobProgressEmitter = Callable[[dict[str, Any]], None]
JobRunner = Callable[
    [dict[str, Any], JobProgressEmitter, asyncio.Event],
    Awaitable[dict[str, Any]],
]


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


@dataclass
class LocalJob:
    job_id: str
    kind: str
    task: asyncio.Task[None]
    cancel_event: asyncio.Event
    status: str = "running"
    result: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LocalCommandPreparation:
    result: CommandBuildResult
    preflight_config: ModelConfig


class LocalAgent:
    def __init__(
        self,
        *,
        target_name: str = "local",
        gpu_sampler: Callable[[], GpuPollResult] = default_gpu_sampler,
        builds_root: str | Path | None = None,
        models_registry_path: str | Path | None = None,
        build_job_runner: JobRunner | None = None,
        model_job_runner: JobRunner | None = None,
    ) -> None:
        self.target_name = target_name
        self._gpu_sampler = gpu_sampler
        self._builds_root = Path(builds_root) if builds_root is not None else default_builds_root()
        self._models_registry_path = (
            Path(models_registry_path)
            if models_registry_path is not None
            else default_models_registry_path()
        )
        self._build_job_runner = build_job_runner or self._default_build_job_runner
        self._model_job_runner = model_job_runner or self._default_model_job_runner
        self._jobs: dict[str, LocalJob] = {}
        self._detached_runs: dict[str, LocalDetachedRun] = {}
        self._detached_sidecar_paths: dict[str, Path] = {}
        self._known_runs_dirs: set[Path] = {default_run_artifacts_dir()}
        self._event_sequences: dict[str, int] = {}
        self._event_buffers: dict[str, list[dict[str, Any]]] = {}
        self._event_buffer_size = 5000
        self._subscribers: dict[str, list[asyncio.Queue[dict[str, Any]]]] = {}
        self._gpu_stream_tasks: dict[str, asyncio.Task[None]] = {}
        self._start_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self._controller_version: str | None = None

    def handle(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | Awaitable[dict[str, Any]]:
        payload = params or {}
        if method == "handshake":
            return self._handshake(payload)
        if method == "ping":
            return self._ping()
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
        if method == "status":
            return self._status(payload)
        if method in {"health", "probe_until_ready"}:
            return self._probe_until_ready(payload)
        if method == "tail_detached":
            return self._tail_detached(payload)
        if method in {"discover_runs", "discover_runs_no_paths", "discover_detached"}:
            return self._discover_detached(payload)
        if method in {"reattach", "reattach_detached"}:
            return self._reattach_detached(payload)
        if method == "list_builds":
            return self._list_builds()
        if method == "adopt_build":
            return self._adopt_build(payload)
        if method == "inspect_build":
            return self._inspect_build(payload)
        if method == "select_build":
            return self._select_build(payload)
        if method == "verify_build":
            return self._verify_build(payload)
        if method == "remove_build":
            return self._remove_build(payload)
        if method == "list_models":
            return self._list_models()
        if method == "pin_model":
            return self._pin_model(payload)
        if method == "refresh_models":
            return self._refresh_models()
        if method == "inspect_model":
            return self._inspect_model(payload)
        if method == "verify_model":
            return self._verify_model(payload)
        if method == "remove_model":
            return self._remove_model(payload)
        if method == "create_build":
            return self._create_build(payload)
        if method == "download_model":
            return self._download_model(payload)
        if method == "cancel_job":
            return self._cancel_job(payload)
        if method in {"gpu", "sample_gpus"}:
            return self._sample_gpus(payload)
        if method == "unsubscribe":
            return self._unsubscribe(payload)
        raise TargetCallError("method-not-found", f"unknown agent method: {method}")

    def _ping(self) -> dict[str, Any]:
        return {
            "pong": True,
            "target": self.target_name,
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "mono": time.monotonic(),
        }

    def _handshake(self, params: dict[str, Any]) -> dict[str, Any]:
        controller_protocol_version = self._controller_protocol_version(params)
        if controller_protocol_version > PROTOCOL_VERSION:
            raise TargetCallError(
                "version-mismatch",
                (
                    "controller protocol version "
                    f"{controller_protocol_version} is newer than agent protocol "
                    f"{PROTOCOL_VERSION}"
                ),
                {
                    "required": int(controller_protocol_version),
                    "actual": PROTOCOL_VERSION,
                },
            )
        if controller_protocol_version < PROTOCOL_VERSION - 1:
            raise TargetCallError(
                "version-mismatch",
                (
                    "controller protocol version "
                    f"{controller_protocol_version} is too old for agent protocol "
                    f"{PROTOCOL_VERSION}"
                ),
                {
                    "required": PROTOCOL_VERSION - 1,
                    "actual": controller_protocol_version,
                },
            )
        controller_version = params.get("controller_version")
        self._controller_version = (
            str(controller_version)
            if isinstance(controller_version, str) and controller_version
            else None
        )
        requested_capabilities = params.get("capabilities") or []
        if isinstance(requested_capabilities, list):
            missing_capabilities = [
                str(capability)
                for capability in requested_capabilities
                if str(capability) not in AGENT_CAPABILITIES
            ]
            if missing_capabilities:
                raise TargetCallError(
                    "feature-unavailable",
                    "target agent does not support requested capabilities",
                    {"missing_capabilities": missing_capabilities},
                )
        return {
            "agent_version": __version__,
            "agent_protocol_version": PROTOCOL_VERSION,
            "protocol_version": controller_protocol_version,
            "controller_version": self._controller_version,
            "target": self.target_name,
            "daemon_pid": os.getpid(),
            "daemon_start_ts": self._start_ts,
            "host_info": {
                "hostname": platform.node(),
                "platform": platform.platform(),
                "driver": _driver_version(),
                "vllm_loader_version": __version__,
            },
            "capabilities": list(AGENT_CAPABILITIES),
        }

    @staticmethod
    def _controller_protocol_version(params: dict[str, Any]) -> int:
        value = params.get("protocol_version", PROTOCOL_VERSION)
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise TargetCallError(
                "invalid-params",
                "handshake protocol_version must be an integer",
                {"protocol_version": value},
            ) from exc

    def _remember_registry_runs_dirs(self, registry: ConfigRegistry) -> None:
        for item in registry.valid:
            self._remember_run_config(item.config)

    def _remember_run_config(self, cfg: ModelConfig) -> None:
        self._known_runs_dirs.add(cfg.run_artifacts_dir)

    def _list_configs(self, params: dict[str, Any]) -> dict[str, Any]:
        registry = load_registry(_configs_dir(params))
        self._remember_registry_runs_dirs(registry)
        return {
            "valid": [_valid_config_payload(item) for item in registry.valid],
            "invalid": [_invalid_config_payload(item) for item in registry.invalid],
        }

    def _preview(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or not name.strip():
            raise TargetCallError("invalid-params", "preview requires config name")
        registry = load_registry(_configs_dir(params))
        self._remember_registry_runs_dirs(registry)
        cfg = _config_by_name(registry, name)
        try:
            result = self._build_command_for_config(cfg)
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
        self._remember_registry_runs_dirs(registry)
        cfg = _config_by_name(registry, name)
        self._remember_run_config(cfg)
        try:
            preparation = self._prepare_command_for_config(cfg)
        except VllmProfileError as exc:
            raise TargetCallError("profile-error", str(exc)) from exc
        result = preparation.result
        failure = check_launch_preflight(preparation.preflight_config, cwd=result.cwd)
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

    def _build_command_for_config(self, cfg: ModelConfig) -> CommandBuildResult:
        return self._prepare_command_for_config(cfg).result

    def _prepare_command_for_config(self, cfg: ModelConfig) -> LocalCommandPreparation:
        resolved_cfg, model_handoff = self._resolve_model_handoff_config(cfg)
        result = self._build_command_for_resolved_config(resolved_cfg)
        if model_handoff is not None:
            result = replace(
                result,
                metadata={
                    **result.metadata,
                    **model_handoff.metadata(),
                },
            )
        return LocalCommandPreparation(result=result, preflight_config=resolved_cfg)

    def _build_command_for_resolved_config(
        self, cfg: ModelConfig
    ) -> CommandBuildResult:
        handoff = self._resolve_build_handoff(cfg)
        if handoff is None:
            return build_command(cfg, select_profile_for_config(cfg))
        executable = (
            handoff.python
            if cfg.command.entrypoint is EntryPoint.MODULE
            else handoff.executable
        )
        resolved_command = cfg.command.model_copy(
            update={"executable": str(executable), "build": None}
        )
        resolved_vllm = cfg.vllm
        if handoff.vllm_version_profile:
            resolved_vllm = cfg.vllm.model_copy(
                update={"version_profile": handoff.vllm_version_profile}
            )
        resolved_cfg = cfg.model_copy(
            update={"command": resolved_command, "vllm": resolved_vllm}
        )
        profile = select_profile(
            handoff.vllm_version_profile or cfg.vllm.version_profile,
            executable=str(handoff.executable),
        )
        result = build_command(resolved_cfg, profile)
        metadata = {
            **result.metadata,
            "build_id": handoff.build_id,
            "build_label": handoff.label,
            "env_overlay": dict(handoff.env_overlay),
            "vllm_version": handoff.vllm_version,
            "vllm_version_profile": handoff.vllm_version_profile
            or result.metadata.get("vllm_version_profile"),
        }
        return replace(result, metadata=metadata)

    def _resolve_model_handoff_config(
        self, cfg: ModelConfig
    ) -> tuple[ModelConfig, ModelHandoff | None]:
        try:
            handoff = resolve_model_handoff(cfg.model_ref, self._models_registry_path)
        except ModelRegistryError as exc:
            raise TargetCallError(exc.code, exc.message, exc.details) from exc
        if handoff is None:
            return cfg, None
        extra_args = _extra_args_with_model_handoff(cfg.extra_args, handoff)
        resolved_cfg = cfg.model_copy(
            update={
                "model": handoff.model_arg,
                "revision": handoff.revision or cfg.revision,
                "extra_args": extra_args,
            }
        )
        return resolved_cfg, handoff

    def _resolve_build_handoff(self, cfg: ModelConfig) -> BuildHandoff | None:
        try:
            return resolve_build_handoff(cfg.command.build, self._builds_root)
        except BuildRegistryError as exc:
            raise TargetCallError(exc.code, exc.message, exc.details) from exc

    def _launch(self, params: dict[str, Any]) -> dict[str, Any]:
        prepared = self._prepare_launch(params)
        cfg = ModelConfig.model_validate(prepared["config"])
        self._remember_run_config(cfg)
        requested_run_id = params.get("run_id")
        run_id = str(requested_run_id) if requested_run_id is not None else None
        requested_launch_mode = cfg.launch.mode.value
        if run_id is not None and (
            run_id in self._detached_runs or run_id in self._detached_sidecar_paths
        ):
            return {
                "run_id": run_id,
                "launch_mode": requested_launch_mode,
                "status": "already-running",
            }
        launch = self._spawn_detached_supervisor(prepared, run_id=run_id)
        self._detached_sidecar_paths[launch.run_id] = launch.sidecar_path
        self._load_detached_run(launch.sidecar_path, verify=False)
        return {
            "run_id": launch.run_id,
            "launch_mode": requested_launch_mode,
            "status": "started",
        }

    async def _wait(self, params: dict[str, Any]) -> dict[str, Any]:
        run_id = _run_id_param(params)
        return await self._await_run_exit_payload(run_id)

    def _stop(self, params: dict[str, Any]) -> dict[str, Any]:
        run_id = _run_id_param(params)
        interrupt_timeout = float(params.get("interrupt_timeout", 5))
        terminate_timeout = float(params.get("terminate_timeout", 5))
        self._request_stop_signal(
            run_id,
            interrupt_timeout=interrupt_timeout,
            terminate_timeout=terminate_timeout,
        )
        return {"run_id": run_id, "signaled": True}

    def _kill(self, params: dict[str, Any]) -> dict[str, Any]:
        run_id = _run_id_param(params)
        self._request_kill_signal(run_id)
        return {"run_id": run_id, "signaled": True}

    async def _probe_until_ready(self, params: dict[str, Any]) -> dict[str, Any]:
        run_id = _run_id_param(params)
        run_config, fsm = self._run_config_and_fsm_or_error(run_id)
        last_event: dict[str, Any] = {}
        completed = asyncio.Event()

        def capture(event: HealthEvent) -> None:
            last_event.clear()
            last_event.update(_health_payload(event, run_config))
            self._publish_health_events(run_id, run_config, fsm, event)
            last_event["phase"] = fsm.phase.value
            if fsm.error_excerpt is not None:
                last_event["error_excerpt"] = fsm.error_excerpt
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

    def _spawn_detached_supervisor(
        self, prepared: dict[str, Any], *, run_id: str | None = None
    ) -> DetachedLaunch:
        cfg = ModelConfig.model_validate(prepared["config"])
        build = _build_result_from_payload(prepared["build"])
        secrets = [cfg.server.api_key or "", cfg.env.get("HF_TOKEN", "")]
        launch_kwargs: dict[str, Any] = {}
        if run_id is not None:
            launch_kwargs["run_id"] = run_id
        try:
            return start_detached(
                cfg,
                build,
                secrets=secrets,
                vllm_version=(
                    _metadata_str(build.metadata.get("vllm_version"))
                    or detect_vllm_version_for_config(cfg)
                ),
                vllm_version_profile=build.metadata.get("vllm_version_profile"),
                **launch_kwargs,
            )
        except FileNotFoundError as exc:
            command = str(exc.filename or build.argv[0])
            raise TargetCallError(
                "command-not-found",
                f"Command not found: {command}",
                {"command": command, "fallback": build.argv[0]},
            ) from exc

    def _load_detached_run(
        self, sidecar_path: Path | str, *, verify: bool
    ) -> LocalDetachedRun:
        path = Path(sidecar_path)
        if verify:
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
        self._detached_sidecar_paths[run.run_id] = path
        return run

    def _load_verified_detached_run(self, sidecar_path: Path | str) -> LocalDetachedRun:
        return self._load_detached_run(sidecar_path, verify=True)

    def _discover_detached_sidecars(
        self, runs_dirs: list[Path | str]
    ) -> list[LocalDetachedRunSummary]:
        summaries: list[LocalDetachedRunSummary] = []
        for path in discover_active_sidecars([Path(item) for item in runs_dirs]):
            sidecar = load_sidecar(path)
            self._detached_sidecar_paths[sidecar.run_id] = path
            summaries.append(
                LocalDetachedRunSummary(
                    run_id=sidecar.run_id,
                    sidecar_path=path,
                    config_name=sidecar.config_name,
                )
            )
        return summaries

    def is_run_alive(self, run_id: str) -> bool:
        detached = self._detached_runs.get(run_id)
        if detached is not None:
            return _detached_run_alive(detached)
        return False

    def _request_stop_signal(
        self,
        run_id: str,
        *,
        interrupt_timeout: float = 5,
        terminate_timeout: float = 5,
    ) -> None:
        detached = self._detached_run_or_error(run_id)
        detached.intentional_shutdown = True
        stop_sidecar_from_system(
            detached.sidecar_path,
            interrupt_timeout=interrupt_timeout,
            terminate_timeout=terminate_timeout,
        )

    def _request_kill_signal(self, run_id: str) -> None:
        detached = self._detached_run_or_error(run_id)
        detached.intentional_shutdown = True
        signal_sidecar_from_system(detached.sidecar_path, signal.SIGKILL)

    async def _await_run_exit_payload(self, run_id: str) -> dict[str, Any]:
        run = self._detached_run_or_error(run_id)
        exit_payload = await self._tail_detached_log_to_events(
            run_id,
            start_position=0,
            wait_for_exit_status=True,
        )
        return {
            "run_id": run_id,
            "returncode": exit_payload["returncode"],
            "intentional": run.intentional_shutdown,
            "phase": exit_payload["phase"],
            **_error_payload_from_fsm(run.fsm),
        }

    async def _tail_detached(self, params: dict[str, Any]) -> dict[str, Any]:
        run_id = _run_id_param(params)
        start_position = params.get("start_position")
        poll_interval = float(params.get("poll_interval", 0.25))
        await self._tail_detached_log_to_events(
            run_id,
            start_position=int(start_position) if start_position is not None else None,
            poll_interval=poll_interval,
        )
        return {"run_id": run_id, "status": "ended"}

    def _discover_detached(self, params: dict[str, Any]) -> dict[str, Any]:
        runs_dirs = params.get("runs_dirs")
        if isinstance(runs_dirs, list):
            dirs = [Path(str(item)) for item in runs_dirs]
        else:
            dirs = sorted(self._known_runs_dirs)
        summaries = self._discover_detached_sidecars(dirs)
        return {"runs": [_detached_summary_payload(summary) for summary in summaries]}

    def _reattach_detached(self, params: dict[str, Any]) -> dict[str, Any]:
        run_id = _run_id_param(params)
        run = self._detached_runs.get(run_id)
        if run is None:
            sidecar_path = self._detached_sidecar_paths.get(run_id)
            if sidecar_path is None:
                raise TargetCallError("run-not-found", f"unknown detached run: {run_id}")
            run = self._load_verified_detached_run(sidecar_path)
        return _detached_run_payload(run)

    def _status(self, params: dict[str, Any]) -> dict[str, Any]:
        run_id = _run_id_param(params)
        run = self._detached_runs.get(run_id)
        if run is None:
            sidecar_path = self._detached_sidecar_paths.get(run_id)
            if sidecar_path is None:
                raise TargetCallError("run-not-found", f"unknown run: {run_id}")
            run = self._load_verified_detached_run(sidecar_path)
        return _detached_run_payload(run)

    async def _sample_gpus(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        if params.get("sub_id") is not None and not params.get("emit_event"):
            return self._start_gpu_stream(params)
        payload = await self._gpu_payload_from_sampler()
        if params.get("emit_event"):
            sub_id = params.get("sub_id")
            event_payload = dict(payload)
            if isinstance(sub_id, str) and sub_id:
                event_payload["sub_id"] = sub_id
            self._publish_event(AgentEvent("gpu", "__agent__", event_payload))
        return payload

    def _start_gpu_stream(self, params: dict[str, Any]) -> dict[str, Any]:
        sub_id = params.get("sub_id")
        if not isinstance(sub_id, str) or not sub_id.strip():
            raise TargetCallError("invalid-params", "gpu stream requires sub_id")
        interval = _gpu_stream_interval(params.get("interval_s"))
        existing = self._gpu_stream_tasks.pop(sub_id, None)
        if existing is not None:
            existing.cancel()
        task = asyncio.create_task(self._run_gpu_stream(sub_id, interval))
        self._gpu_stream_tasks[sub_id] = task
        return {"sub_id": sub_id}

    async def _run_gpu_stream(self, sub_id: str, interval: float) -> None:
        try:
            while True:
                payload = await self._gpu_payload_from_sampler()
                event_payload = dict(payload)
                event_payload["sub_id"] = sub_id
                self._publish_event(AgentEvent("gpu", "__agent__", event_payload))
                await asyncio.sleep(interval)
        finally:
            current_task = asyncio.current_task()
            if self._gpu_stream_tasks.get(sub_id) is current_task:
                self._gpu_stream_tasks.pop(sub_id, None)

    async def _gpu_payload_from_sampler(self) -> dict[str, Any]:
        try:
            return _gpu_poll_payload(await asyncio.to_thread(self.sample_gpus))
        except Exception as exc:
            return _gpu_poll_payload(
                GpuPollResult(
                    [],
                    note=f"GPU stats unavailable: {exc}",
                    unavailable=True,
                )
            )

    def _list_builds(self) -> dict[str, Any]:
        return list_builds(self._builds_root)

    def _adopt_build(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            return adopt_build(params, self._builds_root)
        except BuildRegistryError as exc:
            raise TargetCallError(exc.code, exc.message, exc.details) from exc

    def _inspect_build(self, params: dict[str, Any]) -> dict[str, Any]:
        reference = params.get("build")
        if not isinstance(reference, str) or not reference.strip():
            raise TargetCallError("invalid-params", "inspect_build requires build")
        try:
            return inspect_build(reference, self._builds_root)
        except BuildRegistryError as exc:
            raise TargetCallError(exc.code, exc.message, exc.details) from exc

    def _select_build(self, params: dict[str, Any]) -> dict[str, Any]:
        reference = params.get("build")
        if not isinstance(reference, str) or not reference.strip():
            raise TargetCallError("invalid-params", "select_build requires build")
        try:
            return select_build(reference, self._builds_root)
        except BuildRegistryError as exc:
            raise TargetCallError(exc.code, exc.message, exc.details) from exc

    def _verify_build(self, params: dict[str, Any]) -> dict[str, Any]:
        reference = params.get("build")
        if not isinstance(reference, str) or not reference.strip():
            raise TargetCallError("invalid-params", "verify_build requires build")
        try:
            return verify_build(reference, self._builds_root)
        except BuildRegistryError as exc:
            raise TargetCallError(exc.code, exc.message, exc.details) from exc

    def _remove_build(self, params: dict[str, Any]) -> dict[str, Any]:
        reference = params.get("build")
        if not isinstance(reference, str) or not reference.strip():
            raise TargetCallError("invalid-params", "remove_build requires build")
        try:
            aliases = build_reference_aliases(reference, self._builds_root)
        except BuildRegistryError as exc:
            raise TargetCallError(exc.code, exc.message, exc.details) from exc
        pinned_configs = _configs_pinning_build(_configs_dir(params), aliases)
        if pinned_configs:
            raise TargetCallError(
                "resource-in-use",
                "build is pinned by one or more configs",
                {
                    "build": reference,
                    "reason": "config-pin",
                    "configs": pinned_configs,
                },
            )
        try:
            return remove_build(reference, self._builds_root)
        except BuildRegistryError as exc:
            raise TargetCallError(exc.code, exc.message, exc.details) from exc

    def _list_models(self) -> dict[str, Any]:
        return list_models(self._models_registry_path)

    def _pin_model(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            return pin_model(params, self._models_registry_path)
        except ModelRegistryError as exc:
            raise TargetCallError(exc.code, exc.message, exc.details) from exc

    def _refresh_models(self) -> dict[str, Any]:
        try:
            return refresh_models(self._models_registry_path)
        except ModelRegistryError as exc:
            raise TargetCallError(exc.code, exc.message, exc.details) from exc

    def _inspect_model(self, params: dict[str, Any]) -> dict[str, Any]:
        reference = params.get("model_ref")
        if not isinstance(reference, str) or not reference.strip():
            raise TargetCallError("invalid-params", "inspect_model requires model_ref")
        try:
            return inspect_model(reference, self._models_registry_path)
        except ModelRegistryError as exc:
            raise TargetCallError(exc.code, exc.message, exc.details) from exc

    def _verify_model(self, params: dict[str, Any]) -> dict[str, Any]:
        reference = params.get("model_ref")
        if not isinstance(reference, str) or not reference.strip():
            raise TargetCallError("invalid-params", "verify_model requires model_ref")
        try:
            return verify_model(reference, self._models_registry_path)
        except ModelRegistryError as exc:
            raise TargetCallError(exc.code, exc.message, exc.details) from exc

    def _remove_model(self, params: dict[str, Any]) -> dict[str, Any]:
        reference = params.get("model_ref")
        if not isinstance(reference, str) or not reference.strip():
            raise TargetCallError("invalid-params", "remove_model requires model_ref")
        try:
            aliases = model_reference_aliases(reference, self._models_registry_path)
        except ModelRegistryError as exc:
            raise TargetCallError(exc.code, exc.message, exc.details) from exc
        pinned_configs = _configs_pinning_model(_configs_dir(params), aliases)
        if pinned_configs:
            raise TargetCallError(
                "resource-in-use",
                "model is pinned by one or more configs",
                {
                    "model_ref": reference,
                    "reason": "config-pin",
                    "configs": pinned_configs,
                },
            )
        try:
            return remove_model(reference, self._models_registry_path)
        except ModelRegistryError as exc:
            raise TargetCallError(exc.code, exc.message, exc.details) from exc

    async def _create_build(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._start_job("create_build", params, self._build_job_runner)

    async def _download_model(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._start_job("download_model", params, self._model_job_runner)

    async def _cancel_job(self, params: dict[str, Any]) -> dict[str, Any]:
        job_id = _job_id_param(params)
        job = self._jobs.get(job_id)
        if job is None:
            return {"job_id": job_id, "cancelled": False, "status": "not-found"}
        if job.task.done():
            return {"job_id": job_id, "cancelled": False, "status": job.status}
        job.cancel_event.set()
        job.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await job.task
        return {"job_id": job_id, "cancelled": True, "status": job.status}

    async def _start_job(
        self,
        kind: str,
        params: dict[str, Any],
        runner: JobRunner,
    ) -> dict[str, Any]:
        job_id = _job_id_param(params)
        existing = self._jobs.get(job_id)
        if existing is not None:
            return _job_payload(existing)
        payload = {**params, "job_id": job_id}
        cancel_event = asyncio.Event()
        task = asyncio.create_task(
            self._run_job(job_id, kind, payload, runner, cancel_event)
        )
        job = LocalJob(
            job_id=job_id,
            kind=kind,
            task=task,
            cancel_event=cancel_event,
        )
        self._jobs[job_id] = job
        return _job_payload(job)

    async def _run_job(
        self,
        job_id: str,
        kind: str,
        params: dict[str, Any],
        runner: JobRunner,
        cancel_event: asyncio.Event,
    ) -> None:
        job = self._jobs[job_id]

        def emit(payload: dict[str, Any]) -> None:
            self._publish_event(AgentEvent("job_progress", job_id, dict(payload)))

        try:
            result = await runner(params, emit, cancel_event)
        except asyncio.CancelledError:
            job.status = "cancelled"
            result = {"ok": False, "error_kind": "cancelled", "detail": "cancelled"}
        except Exception as exc:
            job.status = "failed"
            result = {
                "ok": False,
                "error_kind": "agent-internal",
                "detail": str(exc),
            }
        else:
            job.status = "succeeded" if bool(result.get("ok")) else "failed"
        job.result = dict(result)
        self._publish_event(AgentEvent("job_done", job_id, dict(result)))

    async def _default_build_job_runner(
        self,
        params: dict[str, Any],
        emit: JobProgressEmitter,
        _cancel_event: asyncio.Event,
    ) -> dict[str, Any]:
        method = str(params.get("method") or "").strip().lower()
        if method in {"adopt", "adopt-existing", "adopt-existing-venv"}:
            adopt_params = {
                "build_id": params.get("build_id") or params.get("job_id"),
                "label": params.get("label"),
                "venv_path": params.get("venv_path") or params.get("path"),
                "vllm_version": params.get("vllm_version"),
                "vllm_version_profile": params.get("vllm_version_profile"),
                "notes": params.get("notes"),
            }
            adopt_params = {
                key: value for key, value in adopt_params.items() if value is not None
            }
            label = str(
                adopt_params.get("label")
                or adopt_params.get("build_id")
                or adopt_params.get("venv_path")
                or "external build"
            )
            emit(
                {
                    "kind": "committed",
                    "text": f"Adopting build {label}",
                    "level": "INFO",
                    "phase": "VERIFYING",
                }
            )
            try:
                adopted = adopt_build(adopt_params, self._builds_root)
            except BuildRegistryError as exc:
                result = {
                    "ok": False,
                    "error_kind": exc.code,
                    "detail": exc.message,
                }
                result.update(exc.details)
                return result
            return {
                "ok": True,
                "detail": "build adopted",
                "build_id": adopted["build_id"],
                "label": adopted["label"],
                "status": adopted["status"],
                "manifest": adopted["manifest"],
            }

        emit(
            {
                "kind": "committed",
                "text": f"Build creation method is not implemented: {method or 'unknown'}",
                "level": "WARNING",
                "phase": "FAILED",
            }
        )
        return {
            "ok": False,
            "error_kind": "feature-unavailable",
            "detail": f"create_build method is not implemented: {method or 'unknown'}",
        }

    async def _default_model_job_runner(
        self,
        params: dict[str, Any],
        emit: JobProgressEmitter,
        _cancel_event: asyncio.Event,
    ) -> dict[str, Any]:
        model_ref = params.get("model_ref") or params.get("model")
        if not isinstance(model_ref, str) or not model_ref.strip():
            return {
                "ok": False,
                "error_kind": "invalid-params",
                "detail": "download_model requires model_ref",
            }

        emit(
            {
                "kind": "committed",
                "text": "Resolving model",
                "level": "INFO",
                "phase": "RESOLVING",
            }
        )
        try:
            verified = verify_model(model_ref, self._models_registry_path)
        except ModelRegistryError as exc:
            result = {
                "ok": False,
                "error_kind": exc.code,
                "detail": exc.message,
            }
            result.update(exc.details)
            return result

        if verified.get("ok"):
            return {
                "ok": True,
                "detail": "model cached",
                "entry_id": verified.get("entry_id"),
                "cache_state": verified.get("cache_state"),
                "entry": verified.get("entry"),
            }

        entry = verified.get("entry") if isinstance(verified.get("entry"), dict) else {}
        source = str(entry.get("source") or "")
        if source == "hf_repo":
            repo_id = str(entry.get("repo_id") or model_ref)
            allow_patterns = _optional_str_list(params.get("allow_patterns"))
            ignore_patterns = _optional_str_list(params.get("ignore_patterns"))
            try:
                mark_hf_model_partial(
                    model_ref,
                    self._models_registry_path,
                    allow_patterns=allow_patterns,
                    ignore_patterns=ignore_patterns,
                )
            except ModelRegistryError as exc:
                result = {
                    "ok": False,
                    "error_kind": exc.code,
                    "detail": exc.message,
                }
                result.update(exc.details)
                return result
            emit(
                {
                    "kind": "committed",
                    "text": f"Downloading model {repo_id}",
                    "level": "INFO",
                    "phase": "DOWNLOADING",
                }
            )
            try:
                downloaded = await asyncio.to_thread(
                    download_hf_model,
                    model_ref,
                    self._models_registry_path,
                    revision=_optional_param_str(params.get("revision")),
                    allow_patterns=allow_patterns,
                    ignore_patterns=ignore_patterns,
                )
            except ModelRegistryError as exc:
                result = {
                    "ok": False,
                    "error_kind": exc.code,
                    "detail": exc.message,
                }
                result.update(exc.details)
                return result
            return {
                "ok": True,
                "detail": "model cached",
                "entry_id": downloaded.get("entry_id"),
                "cache_state": downloaded.get("cache_state"),
                "entry": downloaded.get("entry"),
                "snapshot_path": downloaded.get("snapshot_path"),
            }

        error_kind = "invalid-config" if source == "local_path" else "feature-unavailable"
        detail = str(verified.get("detail") or "model is not cached")
        if error_kind == "feature-unavailable":
            detail = "remote model download is not implemented for this agent yet"
        result = {
            "ok": False,
            "error_kind": error_kind,
            "detail": detail,
            "entry_id": verified.get("entry_id"),
            "cache_state": verified.get("cache_state"),
            "entry": entry,
        }
        reason = verified.get("reason")
        if reason is not None:
            result["reason"] = reason
        return result

    def _unsubscribe(self, params: dict[str, Any]) -> dict[str, Any]:
        sub_id = params.get("sub_id")
        if not isinstance(sub_id, str) or not sub_id.strip():
            raise TargetCallError("invalid-params", "sub_id is required")
        task = self._gpu_stream_tasks.pop(sub_id, None)
        if task is not None:
            task.cancel()
        return {"sub_id": sub_id}

    async def _tail_detached_log_to_events(
        self,
        run_id: str,
        *,
        start_position: int | None = None,
        poll_interval: float = 0.25,
        wait_for_exit_status: bool = False,
    ) -> dict[str, Any]:
        run = self._detached_run_or_error(run_id)
        log_path = Path(run.manifest.active_log.path)
        if wait_for_exit_status:
            await _wait_for_path(_detached_event_log_path(run.sidecar_path), timeout=0.5)
        position = 0
        pending = ""
        active_source: str | None = None
        while True:
            event_log_path = _detached_event_log_path(run.sidecar_path)
            source_path = event_log_path if event_log_path.exists() else log_path
            source = "event" if source_path == event_log_path else "durable"
            if source != active_source:
                position = _initial_tail_position(
                    source_path,
                    source=source,
                    start_position=start_position,
                )
                pending = ""
                active_source = source
            if source == "event":
                position, pending = self._publish_event_spool_records(
                    run,
                    source_path,
                    position=position,
                    pending=pending,
                )
            elif source_path.exists():
                position, pending = self._publish_durable_log_records(
                    run,
                    source_path,
                    position=position,
                    pending=pending,
                )
            if not self.is_run_alive(run_id):
                break
            await asyncio.sleep(poll_interval)
        returncode = (
            await _wait_detached_returncode(run)
            if wait_for_exit_status
            else _read_detached_returncode(run)
        )
        previous_phase = run.fsm.phase
        run.fsm.process_exited(returncode, intentional=run.intentional_shutdown)
        event = _phase_event_from_transition(run_id, run.fsm, previous_phase)
        if event is not None:
            self._publish_event(event)
        self._publish_event(
            AgentEvent(
                "exited",
                run_id,
                {
                    "returncode": returncode,
                    "intentional": run.intentional_shutdown,
                    "phase": run.fsm.phase.value,
                    **_error_payload_from_fsm(run.fsm),
                },
            )
        )
        return {
            "run_id": run_id,
            "returncode": returncode,
            "intentional": run.intentional_shutdown,
            "phase": run.fsm.phase.value,
            **_error_payload_from_fsm(run.fsm),
        }

    def _publish_event_spool_records(
        self,
        run: LocalDetachedRun,
        path: Path,
        *,
        position: int,
        pending: str,
    ) -> tuple[int, str]:
        if position > path.stat().st_size:
            position = 0
        with path.open("r", encoding="utf-8", errors="replace") as file:
            file.seek(position)
            chunk = file.read()
            position = file.tell()
        if not chunk:
            return position, pending
        pending += chunk
        *lines, pending = pending.split("\n")
        for line in lines:
            if not line:
                continue
            record = _log_record_from_event_spool_line(line)
            if record is None:
                continue
            for event in _events_from_log_record(run.run_id, run.fsm, record):
                self._publish_event(event)
        return position, pending

    def _publish_durable_log_records(
        self,
        run: LocalDetachedRun,
        path: Path,
        *,
        position: int,
        pending: str,
    ) -> tuple[int, str]:
        if position > path.stat().st_size:
            position = 0
        with path.open("r", encoding="utf-8", errors="replace") as file:
            file.seek(position)
            chunk = file.read()
            next_position = file.tell()
        if not chunk:
            return position, pending
        pending += chunk
        *lines, pending = pending.split("\n")
        log_inode = path.stat().st_ino
        line_position = position
        for line in lines:
            if line:
                byte_offset = line_position + len(line.encode("utf-8")) + 1
                record = LogRecord(
                    "committed",
                    line,
                    level=level_for_line(line),
                    log_inode=log_inode,
                    byte_offset=byte_offset,
                )
                for event in _events_from_log_record(run.run_id, run.fsm, record):
                    self._publish_event(event)
            line_position += len((line + "\n").encode("utf-8"))
        return next_position, pending

    def sample_gpus(self) -> GpuPollResult:
        return self._gpu_sampler()

    def subscribe(
        self,
        run_ids: list[str] | tuple[str, ...] | set[str],
        *,
        resume_from: object = "live",
    ) -> AsyncIterator[dict[str, Any]]:
        selected_run_ids = {str(run_id) for run_id in run_ids}
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        for run_id in selected_run_ids:
            self._subscribers.setdefault(run_id, []).append(queue)

        async def iterator() -> AsyncIterator[dict[str, Any]]:
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

        return iterator()

    def _detached_run_or_error(self, run_id: str) -> LocalDetachedRun:
        run = self._detached_runs.get(run_id)
        if run is None:
            raise TargetCallError("run-not-found", f"unknown run: {run_id}")
        return run

    def _run_config_and_fsm_or_error(self, run_id: str) -> tuple[ModelConfig, PhaseFSM]:
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
        id_key = "job_id" if event.kind in {"job_progress", "job_done"} else "run_id"
        return {
            "event": event.kind,
            id_key: event.run_id,
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
        if _is_log_offset_resume(resume_from):
            return self._replay_durable_log_events(run_ids, resume_from)
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
        return sorted(
            events,
            key=lambda item: (_event_stream_id(item) or "", int(item["seq"])),
        )

    def _replay_durable_log_events(
        self, run_ids: set[str], resume_from: object
    ) -> list[dict[str, Any]]:
        assert isinstance(resume_from, dict)
        expected_inode = int(resume_from["log_inode"])
        start_position = max(0, int(resume_from["byte_offset"]))
        events: list[dict[str, Any]] = []
        for run_id in run_ids:
            run = self._detached_run_or_error(run_id)
            active_log = run.manifest.active_log
            if active_log.inode != expected_inode:
                if not _manifest_has_rotated_inode(run.manifest, expected_inode):
                    raise TargetCallError(
                        "identity-verification-failed",
                        "active log inode does not match resume cursor",
                        {
                            "run_id": run_id,
                            "expected_log_inode": expected_inode,
                            "active_log_inode": active_log.inode,
                        },
                    )
                start_position = 0
                events.append(
                    self._wire_event(
                        AgentEvent(
                            "log",
                            run.run_id,
                            {
                                "kind": "committed",
                                "text": "[resumed after rotation]",
                                "level": "INFO",
                            },
                        )
                    )
                )
            path = Path(active_log.path)
            if not path.exists():
                continue
            position = min(start_position, path.stat().st_size)
            with path.open("rb") as file:
                file.seek(position)
                while True:
                    raw_line = file.readline()
                    if not raw_line:
                        break
                    byte_offset = file.tell()
                    text = raw_line.decode("utf-8", errors="replace").rstrip("\n")
                    if not text:
                        continue
                    record = LogRecord(
                        "committed",
                        text,
                        level=level_for_line(text),
                        log_inode=active_log.inode,
                        byte_offset=byte_offset,
                    )
                    for event in _events_from_log_record(run.run_id, run.fsm, record):
                        events.append(self._wire_event(event))
        return events

    def _publish_health_events(
        self, run_id: str, cfg: ModelConfig, fsm: PhaseFSM, event: HealthEvent
    ) -> None:
        self._publish_event(AgentEvent("health", run_id, _health_payload(event, cfg)))
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
    log_pointer = _log_pointer_payload(record)
    if record.kind != "committed":
        return [
            AgentEvent(
                "progress",
                run_id,
                {"text": record.text, **log_pointer},
            )
        ]
    events = [
        AgentEvent(
            "log",
            run_id,
            {
                "kind": record.kind,
                "text": record.text,
                "level": record.level,
                **log_pointer,
            },
        )
    ]
    previous_phase = fsm.phase
    fsm.feed_line(record.text)
    phase_event = _phase_event_from_transition(run_id, fsm, previous_phase)
    if phase_event is not None:
        events.append(phase_event)
    return events


def _is_log_offset_resume(resume_from: object) -> bool:
    return (
        isinstance(resume_from, dict)
        and "log_inode" in resume_from
        and "byte_offset" in resume_from
    )


def _log_pointer_payload(record: LogRecord) -> dict[str, int]:
    payload: dict[str, int] = {}
    if record.log_inode is not None:
        payload["log_inode"] = record.log_inode
    if record.byte_offset is not None:
        payload["byte_offset"] = record.byte_offset
    return payload


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


def _error_payload_from_fsm(fsm: PhaseFSM) -> dict[str, str]:
    payload: dict[str, str] = {}
    if fsm.error_kind is not None:
        payload["error_kind"] = fsm.error_kind.value
    if fsm.error_excerpt is not None:
        payload["error_excerpt"] = fsm.error_excerpt
    return payload


def _detached_run_alive(run: LocalDetachedRun) -> bool:
    try:
        return verify_sidecar_from_system(run.sidecar_path)
    except Exception:
        return False


async def _wait_detached_returncode(
    run: LocalDetachedRun, *, timeout: float = 5.0
) -> int | None:
    deadline = time.monotonic() + timeout
    while True:
        returncode = _read_detached_returncode(run)
        if returncode is not None:
            return returncode
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.05)


async def _wait_for_path(path: Path, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if path.exists():
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.02)


def _read_detached_returncode(run: LocalDetachedRun) -> int | None:
    path = _detached_exit_status_path(run.sidecar_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = payload.get("returncode")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _detached_exit_status_path(sidecar_path: Path) -> Path:
    return sidecar_path.with_suffix(".exit-status")


def _detached_event_log_path(sidecar_path: Path) -> Path:
    return sidecar_path.with_suffix(".events.ndjson")


def _initial_tail_position(
    path: Path, *, source: str, start_position: int | None
) -> int:
    if start_position is not None:
        if source == "durable":
            return start_position
        return 0
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _log_record_from_event_spool_line(line: str) -> LogRecord | None:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    kind = payload.get("kind")
    if kind not in {"committed", "transient"}:
        return None
    text = payload.get("text")
    if not isinstance(text, str):
        return None
    level = payload.get("level")
    log_inode = payload.get("log_inode")
    byte_offset = payload.get("byte_offset")
    return LogRecord(
        kind,
        text,
        level=str(level) if level is not None else None,
        log_inode=log_inode if isinstance(log_inode, int) else None,
        byte_offset=byte_offset if isinstance(byte_offset, int) else None,
    )


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


def _configs_pinning_model(configs_dir: Path | None, aliases: set[str]) -> list[str]:
    registry = load_registry(configs_dir)
    pinned = [
        item.config.name
        for item in registry.valid
        if item.config.model_ref is not None and item.config.model_ref in aliases
    ]
    return sorted(pinned)


def _configs_pinning_build(configs_dir: Path | None, aliases: set[str]) -> list[str]:
    registry = load_registry(configs_dir)
    pinned = [
        item.config.name
        for item in registry.valid
        if item.config.command.build is not None and item.config.command.build in aliases
    ]
    return sorted(pinned)


def _manifest_has_rotated_inode(manifest: Manifest, inode: int) -> bool:
    return any(pointer.inode == inode for pointer in manifest.rotated)


def _extra_args_with_model_handoff(
    extra_args: list[str], handoff: ModelHandoff
) -> list[str]:
    resolved = list(extra_args)
    if handoff.tokenizer and not _extra_args_include_tokenizer(resolved):
        resolved.extend(["--tokenizer", handoff.tokenizer])
    return resolved


def _extra_args_include_tokenizer(extra_args: list[str]) -> bool:
    return any(arg == "--tokenizer" or arg.startswith("--tokenizer=") for arg in extra_args)


def _optional_param_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_str_list(value: object) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        return None
    return [str(item) for item in value if str(item)]


def _event_stream_id(event: dict[str, Any]) -> str | None:
    for key in ("run_id", "job_id", "sub_id"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _driver_version() -> str | None:
    return os.environ.get("NVIDIA_DRIVER_VERSION")


def _run_id_param(params: dict[str, Any]) -> str:
    value = params.get("run_id")
    if not isinstance(value, str) or not value.strip():
        raise TargetCallError("invalid-params", "run_id is required")
    return value


def _job_id_param(params: dict[str, Any]) -> str:
    value = params.get("job_id")
    if not isinstance(value, str) or not value.strip():
        raise TargetCallError("invalid-params", "job_id is required")
    return value


def _job_payload(job: LocalJob) -> dict[str, Any]:
    payload = {
        "job_id": job.job_id,
        "kind": job.kind,
        "status": job.status,
    }
    if job.result:
        payload["result"] = dict(job.result)
    return payload


def _health_payload(event: HealthEvent, cfg: ModelConfig | None = None) -> dict[str, Any]:
    payload = {
        "ready": event.ready,
        "detail": event.detail,
        "models": list(event.models or []),
        "error_kind": event.error_kind.value if event.error_kind is not None else None,
    }
    if event.ready and cfg is not None:
        payload["reachable_url"] = _reachable_url(cfg)
    return payload


def _gpu_poll_payload(result: GpuPollResult) -> dict[str, Any]:
    return {
        "samples": [_gpu_sample_payload(sample) for sample in result.samples],
        "note": result.note,
        "unavailable": result.unavailable,
    }


def _gpu_stream_interval(value: object) -> float:
    if value is None:
        return 2.0
    try:
        interval = float(value)
    except (TypeError, ValueError) as exc:
        raise TargetCallError(
            "invalid-params",
            "gpu interval_s must be a positive number",
            {"interval_s": value},
        ) from exc
    if interval <= 0:
        raise TargetCallError(
            "invalid-params",
            "gpu interval_s must be a positive number",
            {"interval_s": value},
        )
    return interval


def _gpu_sample_payload(sample: GpuSample) -> dict[str, Any]:
    return {
        "visible_index": sample.visible_index,
        "uuid": sample.uuid,
        "name": sample.name,
        "memory_used_mb": sample.memory_used_mb,
        "memory_total_mb": sample.memory_total_mb,
        "utilization_percent": sample.utilization_percent,
        "temperature_c": sample.temperature_c,
        "power_w": sample.power_w,
        "mig_instance_id": sample.mig_instance_id,
    }


def _metadata_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _reachable_url(cfg: ModelConfig) -> str:
    return f"http://{probe_host_for(cfg.server)}:{cfg.server.port}"


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


def _detached_summary_payload(summary: LocalDetachedRunSummary) -> dict[str, Any]:
    return {
        "run_id": summary.run_id,
        "config_name": summary.config_name,
    }


def _detached_run_payload(run: LocalDetachedRun) -> dict[str, Any]:
    sidecar = run.sidecar
    return {
        "run_id": run.run_id,
        "config": run.config.model_dump(mode="json"),
        "sidecar": {
            "config_name": sidecar.config_name,
            "host": sidecar.host,
            "port": sidecar.port,
            "exposure": sidecar.exposure,
            "served_model_names": list(sidecar.served_model_names),
            "launch_mode": sidecar.launch_mode,
            "vllm_version_profile": sidecar.vllm_version_profile,
            "reachable_url": _reachable_url(run.config),
        },
        "fsm": {
            "vllm_version_profile": sidecar.vllm_version_profile,
        },
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
