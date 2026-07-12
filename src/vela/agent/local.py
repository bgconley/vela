from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import fcntl
import hmac
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import yaml

from vela import __version__
from vela.agent.auth import (
    AgentTokenError,
    agent_token_required,
    configured_agent_token,
    default_agent_token_file,
    install_agent_token,
)
from vela.config.loader import (
    ConfigRegistry,
    InvalidConfig,
    ValidConfig,
    discover_config_dirs,
    load_registry,
)
from vela.config.schema import EntryPoint, ModelConfig, RuntimeKind, default_run_artifacts_dir
from vela.engine.build_registry import (
    BuildHandoff,
    BuildRegistryError,
    active_build_id,
    adopt_build,
    build_reference_aliases,
    check_build_launch_integrity,
    default_builds_root,
    discover_venvs,
    inspect_build,
    inspect_venv,
    list_builds,
    mint_build_id,
    record_build_ref,
    remove_build,
    repair_build,
    resolve_build_handoff,
    select_build,
    sweep_stale_creating_builds,
    verify_build,
)
from vela.engine.command_builder import (
    CommandBuildResult,
    build_command,
    is_local_model_reference,
    render_preview,
    render_standalone_docker_script,
)
from vela.engine.composer import (
    allocate_port,
    compose_config,
    list_deployment_recipes,
    list_presets,
    suggest_deployment_defaults,
    validate_config_payload,
)
from vela.engine.job_phases import BuildPhase, DownloadPhase
from vela.engine.log_sink import LogRecord, LogSink, level_for_line
from vela.engine.model_registry import (
    ModelHandoff,
    ModelRegistryError,
    default_hf_home_dir,
    default_hf_hub_cache_dir,
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
    revision_diverges_from_pin,
    verify_model,
)
from vela.engine.phases import ErrorKind, Phase, PhaseFSM
from vela.engine.preflight import check_launch_preflight
from vela.engine.process_manager import (
    DetachedLaunch,
    start_detached,
)
from vela.engine.profile import (
    VllmProfileError,
    detect_vllm_version_for_config,
    select_profile,
    select_profile_for_config,
)
from vela.engine.redaction import scrub_text as scrub_secret_text
from vela.engine.sidecar import (
    Manifest,
    Sidecar,
    TrackedProcessMismatch,
    discover_active_sidecars,
    load_manifest,
    load_sidecar,
    signal_sidecar_from_system,
    stop_sidecar_from_system,
    verify_sidecar_from_system,
)
from vela.monitoring.gpu import GpuPollResult, GpuSample
from vela.monitoring.gpu import sample_gpus as default_gpu_sampler
from vela.monitoring.health import HealthEvent, check_once, probe_host_for, probe_loop

PROTOCOL_VERSION = 1
JOB_RETENTION_LIMIT_ENV = "VELA_AGENT_JOB_RETENTION_LIMIT"
JOB_RETENTION_SECONDS_ENV = "VELA_AGENT_JOB_RETENTION_SECONDS"
DEFAULT_JOB_RETENTION_LIMIT = 50
DEFAULT_JOB_RETENTION_SECONDS = 3600.0
MAX_EXPIRED_JOB_TOMBSTONES = 1000

AGENT_CAPABILITIES = [
    "handshake",
    "ping",
    "list_configs",
    "update_config_flags",
    "set_config_build",
    "compose_config",
    "suggest_deployment_defaults",
    "allocate_port",
    "list_presets",
    "list_deployment_recipes",
    "validate_config",
    "save_config",
    "edit_config",
    "clone_config",
    "delete_config",
    "migrate_wrapper_config",
    "write_agent_token",
    "list_config_files",
    "pull_config",
    "push_config",
    "lint_config",
    "export_config",
    "preview",
    "preflight",
    "prepare_launch",
    "launch",
    "wait",
    "stop",
    "kill",
    "restart",
    "gpu",
    "diagnose",
    "status",
    "health",
    "probe_until_ready",
    "tail_detached",
    "read_run_artifact",
    "discover_runs",
    "discover_runs_no_paths",
    "discover_detached",
    "reattach",
    "reattach_detached",
    "typed_sidecar_resources",
    "docker_runtime_sidecar_identity",
    "list_builds",
    "adopt_build",
    "inspect_venv",
    "discover_venvs",
    "inspect_build",
    "select_build",
    "verify_build",
    "repair_build",
    "check_build_prerequisites",
    "remove_build",
    "run_build",
    "list_models",
    "pin_model",
    "refresh_models",
    "inspect_model",
    "verify_model",
    "remove_model",
    "create_build",
    "download_model",
    "install_uv",
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
# Patchable for tests; the real install is pip-into-the-agent-env (J37).
_INSTALL_UV_ARGV = [sys.executable, "-m", "pip", "install", "--upgrade", "uv"]

JOB_SECRET_ENV_MARKERS = (
    "TOKEN",
    "KEY",
    "SECRET",
    "AUTH",
    "PASSWORD",
    "PASS",
    "CREDENTIAL",
)
URL_TEXT_RE = re.compile(r"https?://[^\s\"'<>]+")
BUILD_INSTALL_PHASE_RULES: tuple[tuple[re.Pattern[str], BuildPhase], ...] = (
    (
        re.compile(
            r"\b(collecting|downloading|fetching|obtaining|cloning|checkout|"
            r"receiving objects|resolving deltas)\b",
            re.IGNORECASE,
        ),
        BuildPhase.DOWNLOADING,
    ),
    (
        re.compile(
            r"\b(building wheel|building editable|preparing metadata|compiling|"
            r"ninja|cmake|nvcc|build_ext|running build|pyproject\.toml)\b",
            re.IGNORECASE,
        ),
        BuildPhase.BUILDING,
    ),
    (
        re.compile(
            r"\b(installing collected packages|successfully installed|"
            r"requirement already satisfied|installed)\b",
            re.IGNORECASE,
        ),
        BuildPhase.INSTALLING,
    ),
)
BUILD_INSTALL_ERROR_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(errno 28|no space left|disk full)\b", re.IGNORECASE), "disk-full"),
    (
        re.compile(
            r"(could not find a version|no matching distribution|package .* not found)",
            re.IGNORECASE,
        ),
        "package-not-found",
    ),
    (
        re.compile(
            r"\b(401|403)\b|forbidden|unauthorized|authentication required|"
            r"invalid credentials",
            re.IGNORECASE,
        ),
        "auth",
    ),
    (
        re.compile(
            r"(connectionpool|connection error|read timed out|timed out|timeout|"
            r"temporary failure|network is unreachable|connection reset|"
            r"connection refused|ssl|tls)",
            re.IGNORECASE,
        ),
        "network",
    ),
    (
        re.compile(
            r"(detected CUDA version .*mismatch.*PyTorch|"
            r"CUDA version .*used to compile PyTorch|"
            r"torch.*CUDA.*mismatch|PyTorch.*CUDA.*mismatch)",
            re.IGNORECASE | re.DOTALL,
        ),
        "torch-cuda-mismatch",
    ),
    (
        re.compile(
            r"(CUDA driver version is insufficient|driver .*too old|"
            r"requires .*newer .*driver)",
            re.IGNORECASE,
        ),
        "driver-too-old",
    ),
    (
        re.compile(
            r"(no kernel image .*available|cutlass_moe_mm_sm100|"
            r"undefined symbol: .*sm100)",
            re.IGNORECASE,
        ),
        "arch-mismatch",
    ),
    (
        re.compile(
            r"(Killed signal terminated program (cc1plus|nvcc)|"
            r"(cc1plus|nvcc).*killed|out of memory|cannot allocate memory)",
            re.IGNORECASE,
        ),
        "compile-oom",
    ),
    (
        re.compile(
            r"(ninja.*failed|subcommand failed|failed building wheel|"
            r"building wheel .* failed|nvcc.*failed|cmake.*error|compilation failed)",
            re.IGNORECASE,
        ),
        "build-failed",
    ),
)
VLLM_COMMIT_INDEX_BASE = "https://wheels.vllm.ai"
VLLM_NIGHTLY_INDEX_BASE = "https://wheels.vllm.ai/nightly"


@dataclass(frozen=True)
class BuildInstallRequest:
    method: str
    installer: str
    provenance: dict[str, Any]
    venv_argv: list[str]
    install_argv: list[str]
    pre_install_argvs: list[list[str]] = field(default_factory=list)
    env_overrides: dict[str, str] = field(default_factory=dict)


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
    completed_mono: float | None = None


@dataclass(frozen=True)
class LocalCommandPreparation:
    result: CommandBuildResult
    preflight_config: ModelConfig
    model_handoff: ModelHandoff | None = None


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
        sweep_stale_creating_builds(self._builds_root)
        self._build_job_runner = build_job_runner or self._default_build_job_runner
        self._model_job_runner = model_job_runner or self._default_model_job_runner
        self._jobs: dict[str, LocalJob] = {}
        self._expired_jobs: dict[str, dict[str, Any]] = {}
        self._expired_job_order: list[str] = []
        self._job_retention_limit = _env_int(
            JOB_RETENTION_LIMIT_ENV,
            DEFAULT_JOB_RETENTION_LIMIT,
            minimum=0,
        )
        self._job_retention_seconds = _env_float(
            JOB_RETENTION_SECONDS_ENV,
            DEFAULT_JOB_RETENTION_SECONDS,
            minimum=0.0,
        )
        self._detached_runs: dict[str, LocalDetachedRun] = {}
        self._detached_sidecar_paths: dict[str, Path] = {}
        self._known_runs_dirs: set[Path] = {default_run_artifacts_dir()}
        self._event_sequences: dict[str, int] = {}
        self._event_buffers: dict[str, list[dict[str, Any]]] = {}
        self._event_buffer_size = 5000
        self._subscribers: dict[str, list[asyncio.Queue[dict[str, Any]]]] = {}
        self._all_subscribers: list[asyncio.Queue[dict[str, Any]]] = []
        self._gpu_stream_tasks: dict[str, asyncio.Task[None]] = {}
        self._post_ready_probes: dict[str, asyncio.Task[None]] = {}
        self._post_ready_registry_refreshed: set[str] = set()
        self._start_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self._controller_version: str | None = None
        # Freeze the source revision at construction (daemon start) so the handshake
        # reports the commit the daemon was launched from even after the tree moves
        # on — the month-stale trap (bug-238). Local import: daemon imports us.
        from vela.agent.daemon import source_revision

        self._source_revision = source_revision()

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
        if method == "update_config_flags":
            return self._update_config_flags(payload)
        if method == "set_config_build":
            return self._set_config_build(payload)
        if method == "compose_config":
            return self._compose_config(payload)
        if method == "suggest_deployment_defaults":
            return self._suggest_deployment_defaults(payload)
        if method == "allocate_port":
            return self._allocate_port(payload)
        if method == "list_presets":
            return self._list_presets()
        if method == "list_deployment_recipes":
            return self._list_deployment_recipes(payload)
        if method == "validate_config":
            return self._validate_config(payload)
        if method == "save_config":
            return self._save_config(payload)
        if method == "edit_config":
            return self._edit_config(payload)
        if method == "clone_config":
            return self._clone_config(payload)
        if method == "delete_config":
            return self._delete_config(payload)
        if method == "migrate_wrapper_config":
            return self._migrate_wrapper_config(payload)
        if method == "write_agent_token":
            return self._write_agent_token(payload)
        if method == "list_config_files":
            return self._list_config_files(payload)
        if method == "pull_config":
            return self._pull_config(payload)
        if method == "push_config":
            return self._push_config(payload)
        if method == "lint_config":
            return self._lint_config(payload)
        if method == "export_config":
            return self._export_config(payload)
        if method == "preview":
            return self._preview(payload)
        if method == "preflight":
            return self._preflight(payload)
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
        if method == "restart":
            return self._restart(payload)
        if method == "diagnose":
            return self._diagnose(payload)
        if method == "status":
            return self._status(payload)
        if method == "health":
            return self._health(payload)
        if method == "probe_until_ready":
            return self._probe_until_ready(payload)
        if method == "tail_detached":
            return self._tail_detached(payload)
        if method == "read_run_artifact":
            return self._read_run_artifact(payload)
        if method in {"discover_runs", "discover_runs_no_paths", "discover_detached"}:
            return self._discover_detached(payload)
        if method in {"reattach", "reattach_detached"}:
            return self._reattach_detached(payload)
        if method == "list_builds":
            return asyncio.to_thread(self._list_builds, payload)
        if method == "adopt_build":
            return asyncio.to_thread(self._adopt_build, payload)
        if method == "inspect_venv":
            return asyncio.to_thread(self._inspect_venv, payload)
        if method == "discover_venvs":
            return asyncio.to_thread(self._discover_venvs, payload)
        if method == "inspect_build":
            return asyncio.to_thread(self._inspect_build, payload)
        if method == "select_build":
            return asyncio.to_thread(self._select_build, payload)
        if method == "verify_build":
            return asyncio.to_thread(self._verify_build, payload)
        if method == "repair_build":
            return asyncio.to_thread(self._repair_build, payload)
        if method == "check_build_prerequisites":
            return self._check_build_prerequisites(payload)
        if method == "remove_build":
            return asyncio.to_thread(self._remove_build, payload)
        if method == "run_build":
            return self._run_build(payload)
        if method == "list_models":
            return asyncio.to_thread(self._list_models, payload)
        if method == "pin_model":
            return asyncio.to_thread(self._pin_model, payload)
        if method == "refresh_models":
            return asyncio.to_thread(self._refresh_models)
        if method == "inspect_model":
            return asyncio.to_thread(self._inspect_model, payload)
        if method == "verify_model":
            return asyncio.to_thread(self._verify_model, payload)
        if method == "remove_model":
            return asyncio.to_thread(self._remove_model, payload)
        if method == "create_build":
            return self._create_build(payload)
        if method == "install_uv":
            return self._install_uv(payload)
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
        self._require_capability_token(params)
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
            "agent_revision": self._source_revision,
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
                "vela_version": __version__,
            },
            "capabilities": list(AGENT_CAPABILITIES),
        }

    @staticmethod
    def _require_capability_token(params: dict[str, Any]) -> None:
        expected = configured_agent_token()
        if expected is None:
            if agent_token_required():
                raise TargetCallError(
                    "agent-auth-required",
                    "target agent is configured to require a capability token "
                    "but none is installed",
                    {"reason": "capability-token-required"},
                )
            return
        supplied = params.get("capability_token")
        if not isinstance(supplied, str) or not hmac.compare_digest(supplied, expected):
            raise TargetCallError(
                "agent-auth-required",
                "target agent requires a valid capability token",
                {"reason": "capability-token-required"},
            )

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

    @property
    def known_runs_dirs(self) -> tuple[Path, ...]:
        return tuple(sorted(self._known_runs_dirs))

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

    def _set_config_build(self, params: dict[str, Any]) -> dict[str, Any]:
        """Pin (or unpin, build=None) a build on an existing config (J31)."""
        name = params.get("name")
        if not isinstance(name, str) or not name.strip():
            raise TargetCallError("invalid-params", "set_config_build requires config name")
        build = params.get("build")
        if build is not None and (not isinstance(build, str) or not build.strip()):
            raise TargetCallError(
                "invalid-params", "set_config_build build must be a string or null"
            )
        registry = load_registry(_configs_dir(params))
        self._remember_registry_runs_dirs(registry)
        item = _valid_config_item_by_name(registry, name)
        try:
            raw = yaml.safe_load(item.path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise TargetCallError(
                "invalid-config",
                f"unable to read config {name}: {exc}",
                {"name": name, "path": str(item.path)},
            ) from exc
        if not isinstance(raw, dict):
            raise TargetCallError(
                "invalid-config",
                f"config root must be a mapping: {name}",
                {"name": name, "path": str(item.path)},
            )
        updated = dict(raw)
        command = dict(updated.get("command") or {})
        if build is None:
            command.pop("build", None)
        else:
            command["build"] = build.strip()
        if command:
            updated["command"] = command
        else:
            updated.pop("command", None)
        try:
            ModelConfig.model_validate(updated)
        except Exception as exc:
            raise TargetCallError(
                "invalid-config",
                f"config would become invalid: {exc}",
                {"name": name},
            ) from exc
        _write_private_text_atomic(item.path, yaml.safe_dump(updated, sort_keys=False))
        return {"name": name, "build": build.strip() if isinstance(build, str) else None}

    def _update_config_flags(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or not name.strip():
            raise TargetCallError("invalid-params", "update_config_flags requires config name")
        registry = load_registry(_configs_dir(params))
        self._remember_registry_runs_dirs(registry)
        item = _valid_config_item_by_name(registry, name)
        try:
            raw = yaml.safe_load(item.path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise TargetCallError(
                "invalid-config",
                f"unable to read config {name}: {exc}",
                {"name": name, "path": str(item.path)},
            ) from exc
        if not isinstance(raw, dict):
            raise TargetCallError(
                "invalid-config",
                f"config root must be a mapping: {name}",
                {"name": name, "path": str(item.path)},
            )
        updated = dict(raw)
        engine_updates = params.get("engine")
        if engine_updates is not None:
            if not isinstance(engine_updates, dict):
                raise TargetCallError(
                    "invalid-params",
                    "update_config_flags engine must be a mapping",
                )
            current_engine = updated.get("engine")
            engine = dict(current_engine) if isinstance(current_engine, dict) else {}
            for key, value in engine_updates.items():
                field = str(key)
                if value is None:
                    engine.pop(field, None)
                else:
                    engine[field] = value
            if engine:
                updated["engine"] = engine
            else:
                updated.pop("engine", None)
        if "extra_args" in params:
            extra_args = params.get("extra_args")
            if not isinstance(extra_args, list) or not all(
                isinstance(item, str) for item in extra_args
            ):
                raise TargetCallError(
                    "invalid-params",
                    "update_config_flags extra_args must be a list of strings",
                )
            if extra_args:
                updated["extra_args"] = list(extra_args)
            else:
                updated.pop("extra_args", None)
        try:
            cfg = ModelConfig.model_validate(updated)
        except Exception as exc:
            raise TargetCallError(
                "invalid-config",
                f"updated config is invalid: {exc}",
                {"name": name, "path": str(item.path)},
            ) from exc
        _write_private_text_atomic(
            item.path,
            yaml.safe_dump(updated, sort_keys=False),
        )
        payload = _valid_config_payload(ValidConfig(item.path, cfg, item.warnings))
        payload["updated"] = True
        return payload

    def _compose_config(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            occupied_container_names = (
                self._occupied_docker_container_names()
                if _params_runtime_is_docker(params)
                else None
            )
            result = compose_config(
                params,
                configs_dir=_configs_dir(params),
                models_registry_path=self._models_registry_path,
                occupied_ports=self._occupied_port_sources(),
                occupied_container_names=occupied_container_names,
            )
        except Exception as exc:
            raise TargetCallError("compose-invalid", str(exc)) from exc
        warnings = list(result.warnings)
        advisory = self._world_size_advisory(result.config)
        if advisory is not None:
            warnings.append(advisory)
        return {
            "config": result.config.model_dump(mode="json"),
            "warnings": warnings,
            "derived": list(result.derived),
        }

    def _world_size_advisory(self, cfg: ModelConfig) -> str | None:
        """Compose-time TP×PP vs visible-GPU advisory (J29) — never raises.

        Sampling is bounded to 0.5s in a worker thread: compose_config is a
        synchronous handler, and the nvidia-smi fallback could otherwise
        block the in-process agent's event loop for seconds. A slow sampler
        just skips the advisory (preflight still catches the mismatch).
        """
        try:
            tp = int(cfg.engine.tensor_parallel_size or 1)
            pp = int(getattr(cfg.engine, "pipeline_parallel_size", None) or 1)
            world = tp * pp
            if world <= 1:
                return None
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            try:
                sample = executor.submit(self.sample_gpus).result(timeout=0.5)
            except concurrent.futures.TimeoutError:
                return None
            finally:
                executor.shutdown(wait=False, cancel_futures=True)
            if getattr(sample, "unavailable", False):
                return None
            count = len(sample.samples)
            if count and world > count:
                plural = "" if count == 1 else "s"
                return (
                    f"tensor_parallel_size×pipeline_parallel_size={world} "
                    f"exceeds {count} visible GPU{plural} on this target"
                )
        except Exception:
            return None
        return None

    def _suggest_deployment_defaults(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            occupied_container_names = (
                self._occupied_docker_container_names()
                if _params_runtime_is_docker(params)
                else None
            )
            return suggest_deployment_defaults(
                params,
                configs_dir=_configs_dir(params),
                models_registry_path=self._models_registry_path,
                occupied_ports=self._occupied_port_sources(),
                occupied_container_names=occupied_container_names,
            )
        except Exception as exc:
            raise TargetCallError("compose-invalid", str(exc)) from exc

    def _allocate_port(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            preferred = params.get("preferred")
            return allocate_port(
                preferred=int(preferred) if preferred is not None else None,
                configs_dir=_configs_dir(params),
                occupied_ports=self._occupied_port_sources(),
            )
        except Exception as exc:
            raise TargetCallError("compose-invalid", str(exc)) from exc

    def _list_presets(self) -> dict[str, Any]:
        return {"presets": list_presets()}

    def _list_deployment_recipes(self, params: dict[str, Any]) -> dict[str, Any]:
        target = params.get("target")
        return {
            "recipes": list_deployment_recipes(
                str(target) if isinstance(target, str) and target.strip() else None
            )
        }

    def _validate_config(self, params: dict[str, Any]) -> dict[str, Any]:
        config = params.get("config")
        if not isinstance(config, dict):
            raise TargetCallError("invalid-params", "validate_config requires config mapping")
        return validate_config_payload(config)

    def _save_config(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or not name.strip():
            raise TargetCallError("invalid-params", "save_config requires config name")
        config = params.get("config")
        if not isinstance(config, dict):
            raise TargetCallError("invalid-params", "save_config requires config mapping")
        cfg = ModelConfig.model_validate(config)
        if cfg.name != name:
            raise TargetCallError(
                "compose-invalid",
                "save_config name must match config.name",
                {"name": name, "config_name": cfg.name},
            )
        normalized = cfg.model_dump(mode="json", exclude_none=True)
        validation = validate_config_payload(normalized)
        if validation.get("ok") is not True:
            raise TargetCallError(
                "invalid-config",
                "saved config is invalid",
                {"name": name, "validation": validation},
            )
        configs_dir = _configs_dir(params) or Path.cwd() / "configs"
        config_path = Path(configs_dir).expanduser() / f"{_safe_config_file_stem(name)}.yaml"
        if config_path.exists() and not bool(params.get("overwrite")):
            raise TargetCallError(
                "config-exists",
                f"config already exists: {name}",
                {"name": name, "path": str(config_path)},
            )
        _write_public_text_atomic(
            config_path,
            yaml.safe_dump(normalized, sort_keys=False),
        )
        return {"path": str(config_path), "name": cfg.name, "config": cfg.model_dump(mode="json")}

    def _edit_config(self, params: dict[str, Any]) -> dict[str, Any]:
        name = _config_name_param(params, method="edit_config")
        configs_dir = _configs_dir(params) or Path.cwd() / "configs"
        registry = load_registry(configs_dir)
        self._remember_registry_runs_dirs(registry)
        item = _valid_config_item_by_name(registry, name)
        overrides = _mapping_param(params.get("overrides"), field_name="overrides")
        payload = item.config.model_dump(mode="json", exclude_none=True)
        _apply_config_overrides(payload, overrides)
        try:
            cfg = ModelConfig.model_validate(payload)
        except Exception as exc:
            raise TargetCallError(
                "invalid-config",
                f"edited config is invalid: {exc}",
                {"name": name, "path": str(item.path)},
            ) from exc
        validation = validate_config_payload(cfg.model_dump(mode="json", exclude_none=True))
        if validation.get("ok") is not True:
            raise TargetCallError(
                "invalid-config",
                "edited config is invalid",
                {"name": name, "path": str(item.path), "validation": validation},
            )
        if not bool(params.get("dry_run")):
            _write_public_text_atomic(
                item.path,
                yaml.safe_dump(
                    cfg.model_dump(mode="json", exclude_none=True),
                    sort_keys=False,
                ),
            )
        return {
            "name": cfg.name,
            "path": str(item.path),
            "config": cfg.model_dump(mode="json"),
            "warnings": list(validation.get("warnings") or []),
            "updated": not bool(params.get("dry_run")),
        }

    def _clone_config(self, params: dict[str, Any]) -> dict[str, Any]:
        src_name = _required_param_name(params, "src_name", method="clone_config")
        new_name = _required_param_name(params, "new_name", method="clone_config")
        configs_dir = _configs_dir(params) or Path.cwd() / "configs"
        registry = load_registry(configs_dir)
        self._remember_registry_runs_dirs(registry)
        source = _valid_config_item_by_name(registry, src_name)
        overrides = _mapping_param(params.get("overrides"), field_name="overrides")
        occupied_container_names = (
            self._occupied_docker_container_names()
            if source.config.command.runtime is RuntimeKind.DOCKER
            and not _override_has(overrides, "command", "docker", "container_name")
            else None
        )
        payload = source.config.model_dump(mode="json", exclude_none=True)
        payload["name"] = new_name
        derived = _prepare_clone_payload(
            payload,
            source.config,
            new_name,
            overrides,
            configs_dir,
            occupied_ports=self._occupied_port_sources(),
            occupied_container_names=occupied_container_names,
        )
        _apply_config_overrides(payload, overrides)
        try:
            cfg = ModelConfig.model_validate(payload)
        except Exception as exc:
            raise TargetCallError(
                "invalid-config",
                f"cloned config is invalid: {exc}",
                {"src_name": src_name, "new_name": new_name},
            ) from exc
        normalized = cfg.model_dump(mode="json", exclude_none=True)
        validation = validate_config_payload(normalized)
        if validation.get("ok") is not True:
            raise TargetCallError(
                "invalid-config",
                "cloned config is invalid",
                {
                    "src_name": src_name,
                    "new_name": new_name,
                    "validation": validation,
                },
            )
        config_path = Path(configs_dir).expanduser() / f"{_safe_config_file_stem(new_name)}.yaml"
        if config_path.exists() and not bool(params.get("overwrite")):
            raise TargetCallError(
                "config-exists",
                f"config already exists: {new_name}",
                {"name": new_name, "path": str(config_path)},
            )
        _write_public_text_atomic(
            config_path,
            yaml.safe_dump(
                normalized,
                sort_keys=False,
            ),
        )
        return {
            "name": cfg.name,
            "path": str(config_path),
            "config": cfg.model_dump(mode="json"),
            "derived": derived,
            "warnings": list(validation.get("warnings") or []),
        }

    def _delete_config(self, params: dict[str, Any]) -> dict[str, Any]:
        name = _config_name_param(params, method="delete_config")
        configs_dir = _configs_dir(params)
        registry = load_registry(configs_dir)
        self._remember_registry_runs_dirs(registry)
        item = _valid_config_item_by_name(registry, name)
        if self._config_has_live_run(name):
            raise TargetCallError(
                "config-in-use",
                f"config has an active run: {name}",
                {"name": name},
            )
        item.path.unlink()
        return {"name": name, "path": str(item.path), "deleted": True}

    def _migrate_wrapper_config(self, params: dict[str, Any]) -> dict[str, Any]:
        src_name = _required_param_name(
            params,
            "src_name",
            method="migrate_wrapper_config",
        )
        new_name = _optional_str(params.get("new_name")) or f"{src_name}-docker"
        configs_dir = _configs_dir(params) or Path.cwd() / "configs"
        registry = load_registry(configs_dir)
        self._remember_registry_runs_dirs(registry)
        source = _valid_config_item_by_name(registry, src_name)
        cfg, derived = _native_config_from_known_wrapper(source.config, new_name)
        normalized = cfg.model_dump(mode="json", exclude_none=True)
        validation = validate_config_payload(normalized)
        if validation.get("ok") is not True:
            raise TargetCallError(
                "invalid-config",
                "migrated wrapper config is invalid",
                {"src_name": src_name, "new_name": new_name, "validation": validation},
            )
        warnings = [
            "wrapper-migration-review-required",
            *list(validation.get("warnings") or []),
        ]
        path = Path(configs_dir).expanduser() / f"{_safe_config_file_stem(new_name)}.yaml"
        written = False
        if not bool(params.get("dry_run")):
            if path.exists() and not bool(params.get("overwrite")):
                raise TargetCallError(
                    "config-exists",
                    f"config already exists: {new_name}",
                    {"name": new_name, "path": str(path)},
                )
            _write_public_text_atomic(path, yaml.safe_dump(normalized, sort_keys=False))
            written = True
        return {
            "name": cfg.name,
            "path": str(path),
            "config": cfg.model_dump(mode="json"),
            "derived": derived,
            "warnings": warnings,
            "source_name": source.config.name,
            "source_path": str(source.path),
            "written": written,
        }

    def _write_agent_token(self, params: dict[str, Any]) -> dict[str, Any]:
        token = _required_param_name(params, "token", method="write_agent_token")
        install_path = _optional_str(params.get("path"))
        try:
            path, _token = install_agent_token(token, path=install_path)
        except AgentTokenError as exc:
            raise TargetCallError(
                "invalid-agent-token",
                str(exc),
                {"reason": "invalid-agent-token"},
            ) from exc
        return {"path": str(path), "mode": "0600"}

    def _list_config_files(self, params: dict[str, Any]) -> dict[str, Any]:
        registry = load_registry(_configs_dir(params))
        self._remember_registry_runs_dirs(registry)
        return {
            "valid": [_valid_config_payload(item) for item in registry.valid],
            "invalid": [_invalid_config_payload(item) for item in registry.invalid],
        }

    def _pull_config(self, params: dict[str, Any]) -> dict[str, Any]:
        name = _config_name_param(params, method="pull_config")
        registry = load_registry(_configs_dir(params))
        self._remember_registry_runs_dirs(registry)
        item = _valid_config_item_by_name(registry, name)
        try:
            yaml_text = item.path.read_text(encoding="utf-8")
        except Exception as exc:
            raise TargetCallError(
                "invalid-config",
                f"unable to read config {name}: {exc}",
                {"name": name, "path": str(item.path)},
            ) from exc
        payload = _valid_config_payload(item)
        payload["yaml"] = yaml_text
        return payload

    def _push_config(self, params: dict[str, Any]) -> dict[str, Any]:
        payload = _config_payload_from_params(params, method="push_config")
        try:
            cfg = ModelConfig.model_validate(payload)
        except Exception as exc:
            raise TargetCallError("invalid-config", f"pushed config is invalid: {exc}") from exc
        expected_name = params.get("name")
        if isinstance(expected_name, str) and expected_name.strip() and cfg.name != expected_name:
            raise TargetCallError(
                "compose-invalid",
                "push_config name must match config.name",
                {"name": expected_name, "config_name": cfg.name},
            )
        configs_dir = _configs_dir(params) or Path.cwd() / "configs"
        config_path = Path(configs_dir).expanduser() / f"{_safe_config_file_stem(cfg.name)}.yaml"
        if config_path.exists() and not bool(params.get("overwrite")):
            raise TargetCallError(
                "config-exists",
                f"config already exists: {cfg.name}",
                {"name": cfg.name, "path": str(config_path)},
            )
        normalized = cfg.model_dump(mode="json", exclude_none=True)
        linted = validate_config_payload(normalized)
        if linted.get("ok") is not True:
            raise TargetCallError(
                "invalid-config",
                "pushed config is invalid",
                {"name": cfg.name, "path": str(config_path), "validation": linted},
            )
        _write_public_text_atomic(
            config_path,
            yaml.safe_dump(normalized, sort_keys=False),
        )
        return {
            "name": cfg.name,
            "path": str(config_path),
            "config": cfg.model_dump(mode="json"),
            "warnings": list(linted.get("warnings") or []),
        }

    def _lint_config(self, params: dict[str, Any]) -> dict[str, Any]:
        payload = _config_payload_from_params(params, method="lint_config")
        return validate_config_payload(payload)

    def _export_config(self, params: dict[str, Any]) -> dict[str, Any]:
        draft_config = params.get("config")
        if isinstance(draft_config, dict):
            cfg = ModelConfig.model_validate(draft_config)
            self._remember_run_config(cfg)
        else:
            name = _config_name_param(params, method="export_config")
            registry = load_registry(_configs_dir(params))
            self._remember_registry_runs_dirs(registry)
            cfg = self._config_with_request_overrides(
                _config_by_name(registry, name, configs_dir=_configs_dir(params)), params
            )
        if cfg.command.runtime is not RuntimeKind.DOCKER:
            raise TargetCallError(
                "invalid-config",
                "standalone export requires command.runtime: docker",
                {"name": cfg.name, "runtime": cfg.command.runtime.value},
            )
        try:
            result = self._build_command_for_config(cfg)
        except VllmProfileError as exc:
            raise TargetCallError("profile-error", str(exc)) from exc
        script = render_standalone_docker_script(result, name=cfg.name)
        payload: dict[str, Any] = {
            "name": cfg.name,
            "script": script,
            "warnings": list(result.warnings),
        }
        output_path = params.get("output_path")
        if output_path is not None:
            if not isinstance(output_path, str) or not output_path.strip():
                raise TargetCallError(
                    "invalid-params",
                    "export_config output_path must be a non-empty string",
                )
            path = Path(output_path).expanduser()
            if path.exists() and not bool(params.get("overwrite")):
                raise TargetCallError(
                    "export-exists",
                    f"export already exists: {path}",
                    {"path": str(path)},
                )
            _write_executable_text_atomic(path, script)
            payload["path"] = str(path)
        return payload

    def _config_has_live_run(self, name: str) -> bool:
        for run in self._detached_runs.values():
            if run.sidecar.config_name != name:
                continue
            if _detached_run_alive(run):
                return True
        for run_id, sidecar_path in list(self._detached_sidecar_paths.items()):
            if run_id in self._detached_runs:
                continue
            try:
                sidecar = load_sidecar(sidecar_path)
            except Exception:
                continue
            if sidecar.config_name != name:
                continue
            try:
                if verify_sidecar_from_system(sidecar_path):
                    return True
            except Exception:
                continue
        for summary in self._discover_detached_sidecars(sorted(self._known_runs_dirs)):
            if summary.config_name == name:
                return True
        return False

    def _preview(self, params: dict[str, Any]) -> dict[str, Any]:
        draft_config = params.get("config")
        if isinstance(draft_config, dict):
            cfg = ModelConfig.model_validate(draft_config)
            self._remember_run_config(cfg)
        else:
            name = _config_name_param(params, method="preview")
            registry = load_registry(_configs_dir(params))
            self._remember_registry_runs_dirs(registry)
            cfg = self._config_with_request_overrides(
                _config_by_name(registry, name, configs_dir=_configs_dir(params)), params
            )
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
        name = _config_name_param(params, method="prepare_launch")
        registry = load_registry(_configs_dir(params))
        self._remember_registry_runs_dirs(registry)
        cfg = self._config_with_request_overrides(
            _config_by_name(registry, name, configs_dir=_configs_dir(params)), params
        )
        self._remember_run_config(cfg)
        self._check_build_launch_integrity(cfg)
        try:
            preparation = self._prepare_command_for_config(
                cfg, validate_model_handoff=True
            )
        except VllmProfileError as exc:
            raise TargetCallError("profile-error", str(exc)) from exc
        result = preparation.result
        cache = _model_not_cached_descriptor(cfg, preparation.model_handoff)
        if cache is not None and cache["gateable"] and cfg.launch.require_cached_models:
            raise TargetCallError(
                "preflight-failed",
                cache["detail"],
                {"kind": cache["kind"], "detail": cache["detail"]},
            )
        failure = check_launch_preflight(
            preparation.preflight_config,
            cwd=result.cwd,
            **self._hf_download_disk_kwargs(cfg, cache),
        )
        if failure is not None:
            raise TargetCallError(
                "preflight-failed",
                failure.detail,
                {"kind": failure.kind.value, "detail": failure.detail},
            )
        launch_warnings: list[dict[str, Any]] = []
        if cache is not None:
            launch_warnings.append(_model_not_cached_wire(cache))
            # Promote the human-readable form onto the command-builder warnings so
            # the TUI banner path (_record_warnings) renders it with no new plumbing.
            result = replace(result, warnings=[*result.warnings, cache["detail"]])
        for warning in _docker_launch_warnings(cfg, preparation.model_handoff):
            launch_warnings.append(warning)
            result = replace(result, warnings=[*result.warnings, warning["detail"]])
        return {
            "config": cfg.model_dump(mode="json"),
            "build": _build_payload(result),
            "preflight": None,
            "launch_warnings": launch_warnings,
        }

    def _preflight(self, params: dict[str, Any]) -> dict[str, Any]:
        draft_config = params.get("config")
        if isinstance(draft_config, dict):
            cfg = ModelConfig.model_validate(draft_config)
        else:
            name = _config_name_param(params, method="preflight")
            registry = load_registry(_configs_dir(params))
            self._remember_registry_runs_dirs(registry)
            cfg = self._config_with_request_overrides(
                _config_by_name(registry, name, configs_dir=_configs_dir(params)), params
            )
        self._remember_run_config(cfg)
        self._check_build_launch_integrity(cfg)
        try:
            preparation = self._prepare_command_for_config(
                cfg, validate_model_handoff=True
            )
        except TargetCallError as exc:
            if exc.code == "hf-auth-required":
                return {
                    "ok": False,
                    "failures": [{"kind": ErrorKind.HF_AUTH.value, "detail": exc.message}],
                    "warnings": [],
                }
            if exc.code == "model-unavailable":
                return {
                    "ok": False,
                    "failures": [
                        {"kind": ErrorKind.MODEL_NOT_FOUND.value, "detail": exc.message}
                    ],
                    "warnings": [],
                }
            raise
        except VllmProfileError as exc:
            raise TargetCallError("profile-error", str(exc)) from exc
        failures: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        cache = _model_not_cached_descriptor(cfg, preparation.model_handoff)
        if cache is not None:
            if cache["gateable"] and cfg.launch.require_cached_models:
                failures.append({"kind": cache["kind"], "detail": cache["detail"]})
            else:
                # Non-gating cache warnings (uncached-but-allowed, unpinned) surface
                # as preflight warnings so `deploy create` prints them without blocking.
                warnings.append(_model_not_cached_wire(cache))
        failure = check_launch_preflight(
            preparation.preflight_config,
            cwd=preparation.result.cwd,
            **self._hf_download_disk_kwargs(cfg, cache),
        )
        if failure is not None:
            failures.append({"kind": failure.kind.value, "detail": failure.detail})
        warnings.extend(_docker_launch_warnings(cfg, preparation.model_handoff))
        return {"ok": not failures, "failures": failures, "warnings": warnings}

    def _config_with_request_overrides(
        self, cfg: ModelConfig, params: dict[str, Any]
    ) -> ModelConfig:
        payload = cfg.model_dump(mode="python")
        engine_updates = params.get("engine")
        if engine_updates is not None:
            if not isinstance(engine_updates, dict):
                raise TargetCallError(
                    "invalid-params",
                    "engine overrides must be a mapping",
                )
            engine = dict(payload.get("engine") or {})
            for key, value in engine_updates.items():
                field = str(key)
                if value is None:
                    engine.pop(field, None)
                else:
                    engine[field] = value
            payload["engine"] = engine
        if "extra_args" in params:
            extra_args = params.get("extra_args")
            if not isinstance(extra_args, list) or not all(
                isinstance(item, str) for item in extra_args
            ):
                raise TargetCallError(
                    "invalid-params",
                    "extra_args overrides must be a list of strings",
                )
            payload["extra_args"] = list(extra_args)
        build_ref = _optional_param_str(params.get("build_id")) or _optional_param_str(
            params.get("build")
        )
        if build_ref is not None:
            command = dict(payload.get("command") or {})
            command["build"] = build_ref
            command["executable"] = None
            payload["command"] = command
        model_ref = _optional_param_str(params.get("model_ref"))
        if model_ref is not None:
            payload["model_ref"] = model_ref
        revision = _optional_param_str(params.get("revision"))
        if revision is not None:
            payload["revision"] = revision
        require_cached = _optional_param_str(params.get("require_cached"))
        if require_cached is not None and require_cached.lower() in {"1", "true", "yes", "on"}:
            launch = dict(payload.get("launch") or {})
            launch["require_cached_models"] = True
            payload["launch"] = launch
        return ModelConfig.model_validate(payload)

    def _build_command_for_config(self, cfg: ModelConfig) -> CommandBuildResult:
        return self._prepare_command_for_config(cfg).result

    def _check_build_launch_integrity(self, cfg: ModelConfig) -> None:
        # A docker-runtime config launches from the image, not a managed venv build,
        # so it must not be gated on the resolved active/default venv build's integrity.
        if cfg.command.runtime is RuntimeKind.DOCKER:
            return
        # M6: a build-less config launches the ACTIVE/default build, so recheck the
        # build that will actually run — not only an explicit command.build.
        build_ref = cfg.command.build or active_build_id(self._builds_root)
        if build_ref is None:
            return
        try:
            check_build_launch_integrity(build_ref, self._builds_root)
        except BuildRegistryError as exc:
            raise TargetCallError(exc.code, exc.message, exc.details) from exc

    def _resolved_hf_cache_dir(self, cfg: ModelConfig) -> Path:
        """The host dir a launch download lands in: the docker mount, else HF hub."""
        docker = cfg.command.docker
        if docker is not None and docker.hf_cache:
            return Path(docker.hf_cache).expanduser()
        return default_hf_hub_cache_dir()

    def _hf_download_disk_kwargs(
        self, cfg: ModelConfig, cache: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Disk-precheck kwargs for check_launch_preflight when a download is expected."""
        if cache is None:
            return {}
        size = cache.get("size_bytes")
        if not isinstance(size, int) or size <= 0:
            return {}
        return {
            "hf_cache_dir": self._resolved_hf_cache_dir(cfg),
            "expected_download_bytes": size,
        }

    def _prepare_command_for_config(
        self, cfg: ModelConfig, *, validate_model_handoff: bool = False
    ) -> LocalCommandPreparation:
        resolved_cfg, model_handoff = self._resolve_model_handoff_config(
            cfg, validate=validate_model_handoff
        )
        model_env: dict[str, str] = {}
        if model_handoff is not None:
            model_env = model_handoff.env_contribution()
            if model_env:
                resolved_cfg = resolved_cfg.model_copy(
                    update={"env": {**resolved_cfg.env, **model_env}}
                )
        result = self._build_command_for_resolved_config(resolved_cfg)
        if model_handoff is not None:
            model_metadata = model_handoff.metadata()
            if resolved_cfg.revision is not None:
                model_metadata["model_revision"] = resolved_cfg.revision
            result_env = {**result.env, **model_env}
            if model_env:
                model_metadata["model_env_keys"] = sorted(model_env)
            result = replace(
                result,
                env=result_env,
                metadata={
                    **result.metadata,
                    **model_metadata,
                },
                preview=render_preview(result.argv, result_env, result.cwd),
            )
        return LocalCommandPreparation(
            result=result,
            preflight_config=resolved_cfg,
            model_handoff=model_handoff,
        )

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
        self, cfg: ModelConfig, *, validate: bool = False
    ) -> tuple[ModelConfig, ModelHandoff | None]:
        try:
            handoff = resolve_model_handoff(cfg.model_ref, self._models_registry_path)
        except ModelRegistryError as exc:
            raise TargetCallError(exc.code, exc.message, exc.details) from exc
        if handoff is None:
            return cfg, None
        _validate_model_ref_repo(cfg, handoff)
        if validate:
            _validate_model_handoff_prelaunch(cfg, handoff)
        extra_args = _extra_args_with_model_handoff(cfg.extra_args, handoff)
        resolved_cfg = cfg.model_copy(
            update={
                "model": handoff.model_arg,
                "revision": cfg.revision or handoff.revision,
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
        loaded_run = self._load_detached_run(launch.sidecar_path, verify=False)
        loaded_run.config = cfg
        self._record_build_ref(prepared, loaded_run)
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

    async def _restart(self, params: dict[str, Any]) -> dict[str, Any]:
        run_id = _run_id_param(params)
        new_run_id_value = params.get("new_run_id")
        if not isinstance(new_run_id_value, str) or not new_run_id_value.strip():
            raise TargetCallError("invalid-params", "new_run_id is required")
        new_run_id = new_run_id_value.strip()
        config_name = _config_name_param(params, method="restart")
        stop_params: dict[str, Any] = {"run_id": run_id}
        for key in ("interrupt_timeout", "terminate_timeout"):
            if key in params:
                stop_params[key] = params[key]
        stop_result = self._stop(stop_params)
        wait_result = await self._await_run_exit_payload(run_id)
        launch_params = {
            key: value
            for key, value in params.items()
            if key
            not in {
                "run_id",
                "new_run_id",
                "config_name",
                "interrupt_timeout",
                "terminate_timeout",
            }
        }
        launch_params["name"] = config_name
        launch_params["run_id"] = new_run_id
        launch_result = self._launch(launch_params)
        return {
            "run_id": run_id,
            "new_run_id": str(launch_result.get("run_id") or new_run_id),
            "status": str(launch_result.get("status") or "started"),
            "stop": stop_result,
            "wait": wait_result,
            "launch": launch_result,
        }

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
            # FR-18: do NOT cancel the probe at READY — probe_loop keeps
            # publishing post-READY health events (DEGRADED / recovery) and
            # exits on its own once the run's process is gone.
            self._track_post_ready_probe(run_id, probe_task)
            if fsm.phase is Phase.READY:
                # H2: vLLM downloaded any missing weights to reach READY; re-scan
                # so the registry learns the entry is now cached. Best-effort — a
                # scan failure must never disturb the run.
                await self._refresh_model_registry_after_ready(run_id, run_config)
        else:
            completed_task.cancel()
        return {"run_id": run_id, **last_event}

    async def _refresh_model_registry_after_ready(
        self, run_id: str, run_config: ModelConfig
    ) -> None:
        if run_id in self._post_ready_registry_refreshed:
            return
        model_ref = _optional_param_str(run_config.model_ref)
        if model_ref is None:
            return
        self._post_ready_registry_refreshed.add(run_id)
        try:
            # Registry helpers are blocking (file I/O; refresh_models is a full
            # HF-cache walk plus an atomic rewrite under an exclusive flock with
            # no timeout), so keep them off the event loop — the same dispatch
            # the refresh_models RPC uses.
            handoff = await asyncio.to_thread(
                resolve_model_handoff, model_ref, self._models_registry_path
            )
            if handoff is None or handoff.source != "hf_repo":
                return
            if (handoff.cache_state or "").lower() == "cached":
                # Already cached: nothing to learn, and no full-scan tax on the
                # happy path. H2 only cares about the uncached-to-cached move.
                return
            await asyncio.to_thread(refresh_models, self._models_registry_path)
        except Exception:
            # Best-effort registry learning: never let a scan failure disturb the run.
            self._post_ready_registry_refreshed.discard(run_id)

    def _track_post_ready_probe(self, run_id: str, task: asyncio.Task[None]) -> None:
        previous = self._post_ready_probes.pop(run_id, None)
        if previous is not None and previous is not task and not previous.done():
            previous.cancel()
        if task.done():
            return
        self._post_ready_probes[run_id] = task
        task.add_done_callback(lambda done_task: self._reap_post_ready_probe(run_id, done_task))

    def _reap_post_ready_probe(self, run_id: str, task: asyncio.Task[None]) -> None:
        if self._post_ready_probes.get(run_id) is task:
            self._post_ready_probes.pop(run_id, None)
        if not task.cancelled():
            # Surface nothing: a failed health probe must never crash the agent.
            task.exception()

    async def _health(self, params: dict[str, Any]) -> dict[str, Any]:
        run_id = _run_id_param(params)
        run_config, fsm = self._run_config_and_fsm_or_error(run_id)
        event = await check_once(run_config)
        payload = _health_payload(event, run_config)
        self._publish_health_events(run_id, run_config, fsm, event)
        payload["phase"] = fsm.phase.value
        if fsm.error_excerpt is not None:
            payload["error_excerpt"] = fsm.error_excerpt
        return {"run_id": run_id, **payload}

    def _spawn_detached_supervisor(
        self, prepared: dict[str, Any], *, run_id: str | None = None
    ) -> DetachedLaunch:
        cfg = ModelConfig.model_validate(prepared["config"])
        build = _build_result_from_payload(prepared["build"])
        secrets = _dedupe_secret_values(
            [
                cfg.server.api_key or "",
                cfg.env.get("HF_TOKEN", ""),
                build.env.get("HF_TOKEN", ""),
            ]
        )
        launch_kwargs: dict[str, Any] = {}
        if run_id is not None:
            launch_kwargs["run_id"] = run_id
        try:
            return start_detached(
                cfg,
                build,
                secrets=secrets,
                build_id=_metadata_str(build.metadata.get("build_id")),
                build_label=_metadata_str(build.metadata.get("build_label")),
                model_ref=_metadata_str(build.metadata.get("model_ref")),
                model_entry_id=_metadata_str(build.metadata.get("model_entry_id")),
                model_repo_id=_metadata_str(build.metadata.get("model_repo_id")),
                model_revision=_metadata_str(build.metadata.get("model_revision")),
                model_commit_sha=_metadata_str(build.metadata.get("model_commit_sha")),
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

    def _record_build_ref(
        self, prepared: dict[str, Any], run: LocalDetachedRun
    ) -> None:
        build_payload = prepared.get("build")
        if not isinstance(build_payload, dict):
            return
        metadata = build_payload.get("metadata")
        if not isinstance(metadata, dict):
            return
        build_id = _metadata_str(metadata.get("build_id"))
        if build_id is None:
            return
        try:
            record_build_ref(
                build_id,
                run.run_id,
                run.sidecar_path,
                pid=run.sidecar.pid,
                process_create_time=run.sidecar.process_create_time,
                root=self._builds_root,
            )
        except BuildRegistryError as exc:
            raise TargetCallError(exc.code, exc.message, exc.details) from exc

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

    def has_active_runs(self) -> bool:
        for run_id in self._detached_runs:
            if self.is_run_alive(run_id):
                return True
        for run_id, sidecar_path in self._detached_sidecar_paths.items():
            if run_id in self._detached_runs:
                continue
            try:
                if verify_sidecar_from_system(sidecar_path):
                    return True
            except Exception:
                continue
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
        try:
            stop_sidecar_from_system(
                detached.sidecar_path,
                interrupt_timeout=interrupt_timeout,
                terminate_timeout=terminate_timeout,
            )
        except TrackedProcessMismatch as exc:
            raise TargetCallError(
                "identity-verification-failed",
                str(exc),
                {
                    "run_id": run_id,
                    "sidecar_path": str(detached.sidecar_path),
                },
            ) from exc

    def _request_kill_signal(self, run_id: str) -> None:
        detached = self._detached_run_or_error(run_id)
        detached.intentional_shutdown = True
        try:
            signal_sidecar_from_system(detached.sidecar_path, signal.SIGKILL)
        except TrackedProcessMismatch as exc:
            raise TargetCallError(
                "identity-verification-failed",
                str(exc),
                {
                    "run_id": run_id,
                    "sidecar_path": str(detached.sidecar_path),
                },
            ) from exc

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

    def _read_run_artifact(self, params: dict[str, Any]) -> dict[str, Any]:
        run_id = _run_id_param(params)
        config_name = params.get("config_name")
        candidate_dirs: list[Path] = []
        if isinstance(config_name, str) and config_name.strip():
            registry = load_registry(_configs_dir(params))
            cfg = _config_by_name(
                registry, config_name.strip(), configs_dir=_configs_dir(params)
            )
            self._remember_run_config(cfg)
            candidate_dirs.append(cfg.run_artifacts_dir)
        sidecar_path = self._run_artifact_sidecar_path(run_id, candidate_dirs)
        sidecar = load_sidecar(sidecar_path)
        if sidecar.run_id != run_id:
            raise TargetCallError(
                "identity-verification-failed",
                "sidecar run_id does not match requested run_id",
                {
                    "run_id": run_id,
                    "sidecar_run_id": sidecar.run_id,
                    "sidecar_path": str(sidecar_path),
                },
            )
        manifest = load_manifest(sidecar.manifest_path)
        log_path = Path(manifest.active_log.path)
        try:
            log_text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise TargetCallError(
                "run-artifact-unavailable",
                f"unable to read run log: {exc}",
                {"run_id": run_id, "log_path": str(log_path)},
            ) from exc
        config = (
            dict(sidecar.config_snapshot)
            if isinstance(sidecar.config_snapshot, dict)
            else _config_from_detached_sidecar(sidecar).model_dump(mode="json")
        )
        return {
            "run_id": run_id,
            "config": config,
            "log_text": log_text,
        }

    def _run_artifact_sidecar_path(
        self, run_id: str, candidate_dirs: list[Path]
    ) -> Path:
        if Path(run_id).name != run_id:
            raise TargetCallError("invalid-params", "run_id must be a filename-safe id")
        sidecar_path = self._detached_sidecar_paths.get(run_id)
        if sidecar_path is not None and sidecar_path.exists():
            return sidecar_path
        seen: set[Path] = set()
        for runs_dir in [*candidate_dirs, *sorted(self._known_runs_dirs)]:
            path = Path(runs_dir) / f"{run_id}.json"
            if path in seen:
                continue
            seen.add(path)
            if path.exists():
                self._detached_sidecar_paths[run_id] = path
                return path
        raise TargetCallError("run-not-found", f"unknown run artifact: {run_id}")

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
            try:
                run = self._load_verified_detached_run(sidecar_path)
            except TrackedProcessMismatch as exc:
                raise TargetCallError(
                    "identity-verification-failed",
                    str(exc),
                    {"run_id": run_id, "sidecar_path": str(sidecar_path)},
                ) from exc
        return _detached_run_payload(run)

    def _status(self, params: dict[str, Any]) -> dict[str, Any]:
        run_id = _run_id_param(params)
        run = self._detached_runs.get(run_id)
        if run is None:
            sidecar_path = self._detached_sidecar_paths.get(run_id)
            if sidecar_path is None:
                raise TargetCallError("run-not-found", f"unknown run: {run_id}")
            try:
                run = self._load_verified_detached_run(sidecar_path)
            except TrackedProcessMismatch as exc:
                raise TargetCallError(
                    "identity-verification-failed",
                    str(exc),
                    {"run_id": run_id, "sidecar_path": str(sidecar_path)},
                ) from exc
        return _detached_run_payload(run)

    def _diagnose(self, params: dict[str, Any]) -> dict[str, Any]:
        uv_path = _find_uv_executable()
        gpu_poll = _diagnose_gpu_poll(self.sample_gpus)
        # Function-level import dodges the local <-> daemon import cycle (mirrors the 6.2
        # source_revision pattern); default_agent_socket_path honours the D5 precedence
        # (VELA_AGENT_RUNTIME_DIR / XDG_STATE_HOME) the pre-D5 local helper missed.
        from vela.agent.daemon import default_agent_socket_path

        return {
            "host": {
                "hostname": platform.node(),
                "platform": platform.platform(),
                "driver": _driver_version(),
                "vela_version": __version__,
            },
            "paths": {
                "config_dir": str(_default_config_dir()),
                "runs_dir": str(default_run_artifacts_dir()),
                "builds_dir": str(self._builds_root),
                "models_registry": str(self._models_registry_path),
                "socket_path": str(default_agent_socket_path()),
                "agent_token_file": str(default_agent_token_file()),
            },
            "toolchain": {
                "python": sys.executable,
                "uv_available": uv_path is not None,
                "uv": uv_path,
                "cuda": _cuda_toolkit_version(),
            },
            "gpu": _diagnose_gpu_payload(gpu_poll),
            "active": _diagnose_active_state(
                self._builds_root,
                self._verified_live_sidecars(),
            ),
            "auth": _diagnose_auth_status(),
        }

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

    def _list_builds(self, params: dict[str, Any]) -> dict[str, Any]:
        payload = list_builds(self._builds_root)
        configs_dir = _configs_dir(params)
        if configs_dir is None:
            return payload
        config_registry = self._load_config_registry(configs_dir)
        for build in payload.get("builds", []):
            if not isinstance(build, dict):
                continue
            refs = _configs_pinning_build(config_registry, _build_payload_aliases(build))
            build["config_refs"] = refs
            build["config_ref_count"] = len(refs)
        return payload

    def _adopt_build(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            return adopt_build(params, self._builds_root)
        except BuildRegistryError as exc:
            raise TargetCallError(exc.code, exc.message, exc.details) from exc

    def _discover_venvs(self, params: dict[str, Any]) -> dict[str, Any]:
        roots = params.get("roots")
        root_list = (
            [str(item) for item in roots if isinstance(item, str)]
            if isinstance(roots, list)
            else None
        )
        return {"venvs": discover_venvs(root_list)}

    def _inspect_venv(self, params: dict[str, Any]) -> dict[str, Any]:
        venv_path = params.get("venv_path")
        if not isinstance(venv_path, str) or not venv_path.strip():
            return {"ok": False, "reason": "venv_path required"}
        return inspect_venv(venv_path.strip())

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

    def _repair_build(self, params: dict[str, Any]) -> dict[str, Any]:
        reference = params.get("build")
        if not isinstance(reference, str) or not reference.strip():
            raise TargetCallError("invalid-params", "repair_build requires build")
        try:
            return repair_build(reference, self._builds_root)
        except BuildRegistryError as exc:
            raise TargetCallError(exc.code, exc.message, exc.details) from exc

    def _check_build_prerequisites(self, params: dict[str, Any]) -> dict[str, Any]:
        method = _optional_param_str(params.get("method"))
        if method is None:
            raise TargetCallError("invalid-params", "check_build_prerequisites requires method")
        uv_path = _find_uv_executable()
        if method in {"nightly", "commit"} and uv_path is None:
            raise TargetCallError(
                "feature-unavailable",
                f"create_build method={method} requires uv",
                {"reason": "uv-required", "method": method},
            )
        return {
            "ok": True,
            "method": method,
            "uv_available": uv_path is not None,
        }

    def _remove_build(self, params: dict[str, Any]) -> dict[str, Any]:
        reference = params.get("build")
        if not isinstance(reference, str) or not reference.strip():
            raise TargetCallError("invalid-params", "remove_build requires build")
        try:
            aliases = build_reference_aliases(reference, self._builds_root)
            inspected = inspect_build(reference, self._builds_root)
        except BuildRegistryError as exc:
            raise TargetCallError(exc.code, exc.message, exc.details) from exc
        config_registry = self._load_config_registry(_configs_dir(params))
        pinned_configs = _configs_pinning_build(config_registry, aliases)
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
        sidecar = _sidecar_using_build(inspected["manifest"], self._verified_live_sidecars())
        if sidecar is not None:
            raise TargetCallError(
                "resource-in-use",
                "build is used by a live run",
                _live_run_resource_details(
                    sidecar,
                    resource_key="build",
                    resource_value=reference,
                ),
            )
        try:
            return remove_build(reference, self._builds_root)
        except BuildRegistryError as exc:
            raise TargetCallError(exc.code, exc.message, exc.details) from exc

    def _list_models(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        result = list_models(
            self._models_registry_path,
            cached_only=_param_bool(params.get("cached_only")),
            pinned_only=_param_bool(params.get("pinned_only")),
        )
        configs_dir = params.get("configs_dir")
        if configs_dir:
            try:
                registry = load_registry(Path(str(configs_dir)))
            except Exception:
                return result
            models = result.get("models")
            if isinstance(models, list):
                for entry in models:
                    if isinstance(entry, dict):
                        entry["config_refs"] = _configs_referencing_model(registry, entry)
        return result

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
            return verify_model(
                reference,
                self._models_registry_path,
                deep=_param_bool(params.get("deep")),
            )
        except ModelRegistryError as exc:
            raise TargetCallError(exc.code, exc.message, exc.details) from exc

    def _remove_model(self, params: dict[str, Any]) -> dict[str, Any]:
        reference = params.get("model_ref")
        if not isinstance(reference, str) or not reference.strip():
            raise TargetCallError("invalid-params", "remove_model requires model_ref")
        try:
            aliases = model_reference_aliases(reference, self._models_registry_path)
            inspected = inspect_model(reference, self._models_registry_path)
        except ModelRegistryError as exc:
            raise TargetCallError(exc.code, exc.message, exc.details) from exc
        force = _param_bool(params.get("force"))
        config_registry = self._load_config_registry(_configs_dir(params))
        pinned_configs = _configs_pinning_model(
            config_registry,
            aliases,
            inspected["entry"],
        )
        if pinned_configs and not force:
            raise TargetCallError(
                "resource-in-use",
                "model is pinned by one or more configs",
                {
                    "model_ref": reference,
                    "reason": "config-pin",
                    "configs": pinned_configs,
                },
            )
        sidecar = _sidecar_using_model(
            inspected["entry"],
            aliases,
            self._verified_live_sidecars(),
        )
        if sidecar is not None:
            raise TargetCallError(
                "resource-in-use",
                "model is used by a live run",
                _live_run_resource_details(
                    sidecar,
                    resource_key="model_ref",
                    resource_value=reference,
                ),
            )
        try:
            return remove_model(reference, self._models_registry_path)
        except ModelRegistryError as exc:
            raise TargetCallError(exc.code, exc.message, exc.details) from exc

    def _load_config_registry(self, configs_dir: Path | None) -> ConfigRegistry:
        registry = load_registry(configs_dir)
        self._remember_registry_runs_dirs(registry)
        return registry

    def _verified_live_sidecars(self) -> list[Sidecar]:
        paths = list(discover_active_sidecars(sorted(self._known_runs_dirs)))
        paths.extend(self._detached_sidecar_paths.values())
        verified: list[Sidecar] = []
        seen: set[Path] = set()
        for path in sorted((Path(item) for item in paths), key=str):
            if path in seen:
                continue
            seen.add(path)
            try:
                if verify_sidecar_from_system(path):
                    verified.append(load_sidecar(path))
            except Exception:
                continue
        return verified

    def _occupied_port_sources(self) -> dict[str, list[int]]:
        return {
            "live_sidecar_ports": sorted(
                {sidecar.port for sidecar in self._verified_live_sidecars() if sidecar.port > 0}
            ),
            "listener_ports": sorted(_listening_ports()),
        }

    def _occupied_docker_container_names(self) -> list[str]:
        names = {
            str(sidecar.docker_container_name).strip().lstrip("/")
            for sidecar in self._verified_live_sidecars()
            if sidecar.runtime == "docker" and sidecar.docker_container_name
        }
        names.update(_docker_container_names())
        return sorted(name for name in names if name)

    async def _create_build(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._start_job("create_build", params, self._build_job_runner)

    async def _install_uv(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._start_job("install_uv", params, self._install_uv_job_runner)

    async def _install_uv_job_runner(
        self,
        params: dict[str, Any],
        emit: JobProgressEmitter,
        cancel_event: asyncio.Event,
    ) -> dict[str, Any]:
        emit(
            {
                "kind": "committed",
                "text": "Installing uv: " + " ".join(_INSTALL_UV_ARGV),
                "level": "INFO",
            }
        )
        process = await asyncio.create_subprocess_exec(
            *_INSTALL_UV_ARGV,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert process.stdout is not None
        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").rstrip()
                if text:
                    emit({"kind": "committed", "text": text, "level": "INFO"})
                if cancel_event.is_set():
                    break
            if cancel_event.is_set():
                await _terminate_build_subprocess(process)
                return {
                    "ok": False,
                    "detail": "uv install cancelled",
                    "error_kind": "cancelled",
                }
            returncode = await process.wait()
        except asyncio.CancelledError:
            # The job task itself was cancelled — never orphan pip.
            await _terminate_build_subprocess(process)
            raise
        if returncode == 0:
            return {"ok": True, "detail": "uv installed"}
        return {
            "ok": False,
            "detail": f"uv install failed (exit {returncode})",
            "error_kind": "install-failed",
        }

    async def _download_model(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._start_job("download_model", params, self._model_job_runner)

    async def _run_build(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._start_job("run_build", params, self._run_build_job_runner)

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
        self._prune_terminal_jobs()
        expired = self._expired_jobs.get(job_id)
        if expired is not None:
            return self._expired_job_payload(job_id, kind, params, expired)
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
        job_secrets = _job_secret_values(params, _env_overrides(params.get("env")))

        def emit(payload: dict[str, Any]) -> None:
            safe_payload = _scrub_job_payload(dict(payload), secrets=job_secrets)
            self._publish_event(AgentEvent("job_progress", job_id, safe_payload))

        try:
            result = await runner(params, emit, cancel_event)
        except asyncio.CancelledError:
            job.status = "cancelled"
            result = _cancelled_job_result(kind, params)
        except Exception as exc:
            job.status = "failed"
            result = {
                "ok": False,
                "error_kind": "agent-internal",
                "detail": str(exc),
            }
        else:
            if result.get("error_kind") == "cancelled":
                job.status = "cancelled"
            else:
                job.status = "succeeded" if bool(result.get("ok")) else "failed"
        safe_result = _scrub_job_payload(dict(result), secrets=job_secrets)
        job.result = safe_result
        job.completed_mono = time.monotonic()
        self._publish_event(AgentEvent("job_done", job_id, safe_result))
        self._prune_terminal_jobs()

    def _prune_terminal_jobs(self) -> None:
        terminal_jobs = [
            (job.completed_mono or 0.0, job_id, job)
            for job_id, job in self._jobs.items()
            if job.task.done()
        ]
        if not terminal_jobs:
            return
        keep_by_count = {
            job_id
            for _completed, job_id, _job in sorted(terminal_jobs, reverse=True)[
                : self._job_retention_limit
            ]
        }
        now = time.monotonic()
        for completed_mono, job_id, job in terminal_jobs:
            keep_by_age = (
                self._job_retention_seconds > 0
                and now - completed_mono <= self._job_retention_seconds
            )
            if job_id in keep_by_count or keep_by_age:
                continue
            self._jobs.pop(job_id, None)
            self._event_buffers.pop(job_id, None)
            self._event_sequences.pop(job_id, None)
            self._remember_expired_job(job_id, job)

    def _remember_expired_job(self, job_id: str, job: LocalJob) -> None:
        if job_id not in self._expired_jobs:
            self._expired_job_order.append(job_id)
        self._expired_jobs[job_id] = {
            "kind": job.kind,
            "status": job.status,
            "result": dict(job.result),
            "expired_mono": time.monotonic(),
        }
        while len(self._expired_job_order) > MAX_EXPIRED_JOB_TOMBSTONES:
            oldest = self._expired_job_order.pop(0)
            self._expired_jobs.pop(oldest, None)

    def _expired_job_payload(
        self,
        job_id: str,
        kind: str,
        params: dict[str, Any],
        expired: dict[str, Any],
    ) -> dict[str, Any]:
        expired_kind = str(expired.get("kind") or kind)
        expired_status = str(expired.get("status") or "expired")
        expired_result = _dict_or_empty(expired.get("result"))
        if expired_kind == "create_build":
            build_id = _optional_param_str(expired_result.get("build_id")) or _optional_param_str(
                params.get("build_id")
            )
            if build_id is not None:
                try:
                    inspected = inspect_build(build_id, self._builds_root)
                except BuildRegistryError as exc:
                    raise self._job_expired_error(
                        job_id,
                        expired_kind,
                        expired_status,
                    ) from exc
                manifest = _dict_or_empty(inspected.get("manifest"))
                build_status = str(manifest.get("status") or expired_status)
                return {
                    "job_id": job_id,
                    "kind": expired_kind,
                    "status": expired_status,
                    "result": {
                        "ok": build_status in {"ready", "adopted"},
                        "detail": "job result pruned; build record retained",
                        "build_id": build_id,
                        "status": build_status,
                        "manifest": manifest,
                        "pruned": True,
                    },
                }
        raise self._job_expired_error(job_id, expired_kind, expired_status)

    def _job_expired_error(
        self,
        job_id: str,
        kind: str,
        status: str,
    ) -> TargetCallError:
        return TargetCallError(
            "job-expired",
            f"job result expired: {job_id}",
            {
                "job_id": job_id,
                "kind": kind,
                "status": status,
            },
        )

    async def _run_pip_build_job(
        self,
        params: dict[str, Any],
        emit: JobProgressEmitter,
        cancel_event: asyncio.Event,
    ) -> dict[str, Any]:
        requested_build_id = _optional_param_str(params.get("build_id"))
        build_id = mint_build_id(self._builds_root)
        params["build_id"] = build_id
        label = _optional_param_str(params.get("label")) or requested_build_id or build_id
        method = str(params.get("method") or "pip").strip().lower()
        build_dir = self._builds_root / build_id
        venv_path = build_dir / "venv"
        env_overrides = _env_overrides(params.get("env"))
        job_secrets = _job_secret_values(params, env_overrides)
        try:
            install_request = _build_install_request(
                method,
                params,
                venv_path=venv_path,
            )
        except BuildRegistryError as exc:
            result = {
                "ok": False,
                "error_kind": exc.code,
                "detail": exc.message,
                "build_id": build_id,
            }
            result.update(exc.details)
            return result
        effective_env_overrides = {
            **env_overrides,
            **install_request.env_overrides,
        }
        if build_dir.exists():
            return {
                "ok": False,
                "error_kind": "resource-in-use",
                "detail": f"build already exists: {build_id}",
                "build_id": build_id,
                "reason": "build-exists",
            }

        emit(
            {
                "kind": "committed",
                "text": f"Creating build {label}",
                "level": "INFO",
                "phase": BuildPhase.RESOLVING.value,
            }
        )
        build_dir.mkdir(parents=True)
        build_lock = _acquire_build_install_lock(build_dir)
        current_log_payload: dict[str, Any] = {}
        install_output_tail: list[str] = []

        def emit_log_record(record: LogRecord) -> None:
            install_output_tail.append(record.text)
            if len(install_output_tail) > 50:
                del install_output_tail[:-50]
            payload = dict(current_log_payload)
            payload["kind"] = record.kind
            payload["text"] = record.text
            payload["level"] = record.level or payload.get("level")
            emit(payload)

        log_sink = LogSink(
            build_dir / "install.log",
            secrets=job_secrets,
            emit=emit_log_record,
        )

        def emit_install(payload: dict[str, Any]) -> None:
            nonlocal current_log_payload
            text = _optional_param_str(payload.get("text"))
            if text is not None:
                current_log_payload = {
                    key: value for key, value in payload.items() if key != "text"
                }
                log_sink.feed((text + "\n").encode("utf-8"))
                current_log_payload = {}
                return
            emit(payload)

        install_payload: dict[str, Any] = {
            "method": install_request.method,
            "installer": install_request.installer,
            "python_requested": _optional_param_str(params.get("python")),
            "provenance": _scrub_job_payload(
                {
                    **install_request.provenance,
                    "env_overrides": dict(effective_env_overrides),
                },
                secrets=job_secrets,
            ),
            "exit_code": None,
        }
        try:
            _write_build_manifest(
                build_dir,
                _managed_build_manifest(
                    build_id=build_id,
                    label=label,
                    status="creating",
                    install=install_payload,
                    resolved={},
                ),
            )
            env = {**os.environ, **effective_env_overrides}
            venv_exit = await _build_subprocess_exec(
                install_request.venv_argv,
                env=env,
                cwd=build_dir,
                emit=emit_install,
                phase=BuildPhase.RESOLVING.value,
                cancel_event=cancel_event,
            )
            if venv_exit != 0:
                error_kind = _classify_build_install_failure(
                    install_output_tail,
                    default="venv-create-failed",
                )
                return _failed_build_result(
                    build_dir,
                    build_id=build_id,
                    label=label,
                    install=install_payload,
                    error_kind=error_kind,
                    detail="build virtualenv creation failed",
                    exit_code=venv_exit,
                )
            for preinstall_argv in install_request.pre_install_argvs:
                preinstall_exit = await _build_subprocess_exec(
                    preinstall_argv,
                    env=env,
                    cwd=build_dir,
                    emit=emit_install,
                    phase=BuildPhase.DOWNLOADING.value,
                    cancel_event=cancel_event,
                )
                if preinstall_exit != 0:
                    error_kind = _classify_build_install_failure(
                        install_output_tail,
                        default="source-prepare-failed",
                    )
                    return _failed_build_result(
                        build_dir,
                        build_id=build_id,
                        label=label,
                        install=install_payload,
                        error_kind=error_kind,
                        detail="build source preparation failed",
                        exit_code=preinstall_exit,
                    )
            install_exit = await _build_subprocess_exec(
                install_request.install_argv,
                env=env,
                cwd=build_dir,
                emit=emit_install,
                phase=BuildPhase.INSTALLING.value,
                cancel_event=cancel_event,
            )
            install_payload["exit_code"] = install_exit
            if install_exit != 0:
                error_kind = _classify_build_install_failure(
                    install_output_tail,
                    default="install-failed",
                )
                return _failed_build_result(
                    build_dir,
                    build_id=build_id,
                    label=label,
                    install=install_payload,
                    error_kind=error_kind,
                    detail="build package install failed",
                    exit_code=install_exit,
                )

            emit(
                {
                    "kind": "committed",
                    "text": f"Verifying build {label}",
                    "level": "INFO",
                    "phase": BuildPhase.VERIFYING.value,
                }
            )
            try:
                _write_managed_build_artifacts(build_dir, venv_path)
                resolved = _managed_build_resolved_versions(venv_path)
                integrity = _managed_build_integrity(build_dir / "bin" / "vllm")
            except BuildRegistryError as exc:
                result = _failed_build_result(
                    build_dir,
                    build_id=build_id,
                    label=label,
                    install=install_payload,
                    error_kind=exc.code,
                    detail=exc.message,
                    exit_code=int(install_payload.get("exit_code") or 0),
                )
                result.update(exc.details)
                return result
            except OSError:
                return _failed_build_result(
                    build_dir,
                    build_id=build_id,
                    label=label,
                    install=install_payload,
                    error_kind="invalid-config",
                    detail="unable to prepare managed build artifacts",
                    exit_code=int(install_payload.get("exit_code") or 0),
                )
            manifest = _managed_build_manifest(
                build_id=build_id,
                label=label,
                status="ready",
                install=install_payload,
                resolved=resolved,
            )
            manifest["integrity"] = integrity
            _write_build_manifest(build_dir, manifest)
            return {
                "ok": True,
                "detail": "build ready",
                "build_id": build_id,
                "label": label,
                "status": "ready",
                "manifest": _build_job_manifest_payload(manifest),
            }
        except asyncio.CancelledError:
            install_payload["cancelled"] = True
            _failed_build_result(
                build_dir,
                build_id=build_id,
                label=label,
                install=install_payload,
                error_kind="cancelled",
                detail="build install cancelled",
                exit_code=130,
            )
            raise
        finally:
            log_sink.close()
            _release_build_install_lock(build_lock)

    async def _default_build_job_runner(
        self,
        params: dict[str, Any],
        emit: JobProgressEmitter,
        _cancel_event: asyncio.Event,
    ) -> dict[str, Any]:
        method = str(params.get("method") or "").strip().lower()
        if method in {"pip", "nightly", "commit", "wheel", "git"}:
            return await self._run_pip_build_job(params, emit, _cancel_event)
        if method in {"adopt", "adopt-existing", "adopt-existing-venv"}:
            requested_build_id = _optional_param_str(params.get("build_id"))
            adopt_params = {
                "label": params.get("label") or requested_build_id,
                "venv_path": params.get("venv_path") or params.get("path"),
                "vllm_version": params.get("vllm_version"),
                "vllm_version_profile": params.get("vllm_version_profile"),
                "notes": params.get("notes"),
                "copy": params.get("copy"),
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
                    "phase": BuildPhase.VERIFYING.value,
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
                "phase": BuildPhase.FAILED.value,
            }
        )
        return {
            "ok": False,
            "error_kind": "feature-unavailable",
            "detail": f"create_build method is not implemented: {method or 'unknown'}",
        }

    async def _run_build_job_runner(
        self,
        params: dict[str, Any],
        emit: JobProgressEmitter,
        cancel_event: asyncio.Event,
    ) -> dict[str, Any]:
        build_ref = _optional_param_str(params.get("build"))
        if build_ref is None:
            return {
                "ok": False,
                "error_kind": "invalid-params",
                "detail": "run_build requires build",
            }
        argv = params.get("argv")
        if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
            return {
                "ok": False,
                "error_kind": "invalid-params",
                "detail": "run_build requires argv as a list of strings",
            }
        try:
            handoff = resolve_build_handoff(build_ref, self._builds_root)
        except BuildRegistryError as exc:
            result = {
                "ok": False,
                "error_kind": exc.code,
                "detail": exc.message,
            }
            result.update(exc.details)
            return result
        if handoff is None:
            return {
                "ok": False,
                "error_kind": "build-not-found",
                "detail": f"unknown build: {build_ref}",
                "build": build_ref,
            }
        env = _env_with_build_overlay(os.environ, handoff.env_overlay)
        command = [str(handoff.executable), *argv]
        returncode = await _build_subprocess_exec(
            command,
            env=env,
            cwd=handoff.executable.parent.parent,
            emit=emit,
            phase="RUNNING",
            cancel_event=cancel_event,
        )
        return {
            "ok": returncode == 0,
            "detail": f"build command exited {returncode}",
            "build_id": handoff.build_id,
            "label": handoff.label,
            "returncode": returncode,
            **({} if returncode == 0 else {"error_kind": "process-exited"}),
        }

    async def _default_model_job_runner(
        self,
        params: dict[str, Any],
        emit: JobProgressEmitter,
        cancel_event: asyncio.Event,
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
                "phase": DownloadPhase.RESOLVING.value,
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

        entry = verified.get("entry") if isinstance(verified.get("entry"), dict) else {}
        source = str(entry.get("source") or "")
        # L3: a URL model is launch-time-only. Verify now reports ok for it, so this
        # must be handled BEFORE the cached short-circuit — otherwise a URL entry
        # would report the misleading "model cached" instead of its own wording.
        if source == "url":
            emit(
                {
                    "kind": "committed",
                    "text": "URL model is launch-time-only",
                    "level": "INFO",
                    "phase": DownloadPhase.READY.value,
                }
            )
            return {
                "ok": True,
                "detail": "url model is launch-time-only; no pre-download needed",
                "entry_id": verified.get("entry_id"),
                "cache_state": "remote_only",
                "entry": entry,
            }

        requested_revision = _optional_param_str(params.get("revision"))
        # M2: honour an explicit differing revision instead of no-opping a cached
        # pin. A bare download (revision None ≡ main) never counts as divergent.
        revision_override = revision_diverges_from_pin(entry, requested_revision)
        if verified.get("ok") and not revision_override:
            return {
                "ok": True,
                "detail": "model cached",
                "entry_id": verified.get("entry_id"),
                "cache_state": verified.get("cache_state"),
                "entry": verified.get("entry"),
            }
        if source == "hf_repo":
            repo_id = str(entry.get("repo_id") or model_ref)
            entry_id = (
                _optional_param_str(verified.get("entry_id"))
                or _optional_param_str(entry.get("entry_id"))
                or str(model_ref)
            )
            allow_patterns = _optional_str_list(params.get("allow_patterns"))
            ignore_patterns = _optional_str_list(params.get("ignore_patterns"))
            # A side download of a different revision must not mark the pin
            # partial (M2); download_hf_model records last_download_* instead.
            if not revision_override:
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
            download_log = LogSink(
                _model_download_log_path(self._models_registry_path, entry_id),
                secrets=_job_secret_values(params, _env_overrides(params.get("env"))),
            )
            try:
                def emit_download(payload: dict[str, Any]) -> None:
                    text = _optional_param_str(payload.get("text"))
                    if text is not None:
                        download_log.feed((text + "\n").encode("utf-8"))
                    emit(payload)

                emit_download(
                    {
                        "kind": "committed",
                        "text": f"Downloading model {repo_id}",
                        "level": "INFO",
                        "phase": DownloadPhase.DOWNLOADING.value,
                    }
                )
                loop = asyncio.get_running_loop()

                def emit_download_progress(progress: dict[str, Any]) -> None:
                    if cancel_event.is_set():
                        raise ModelRegistryError(
                            "cancelled",
                            "model download cancelled",
                            {
                                "model_ref": str(model_ref),
                                "repo_id": repo_id,
                                "cache_state": "partial",
                            },
                        )
                    payload = _model_download_progress_payload(repo_id, progress)
                    loop.call_soon_threadsafe(emit_download, payload)

                download_task = asyncio.create_task(
                    asyncio.to_thread(
                        download_hf_model,
                        model_ref,
                        self._models_registry_path,
                        revision=_optional_param_str(params.get("revision")),
                        allow_patterns=allow_patterns,
                        ignore_patterns=ignore_patterns,
                        progress_callback=emit_download_progress,
                    )
                )
                try:
                    downloaded = await asyncio.shield(download_task)
                except asyncio.CancelledError:
                    cancel_event.set()
                    try:
                        await asyncio.wait_for(asyncio.shield(download_task), timeout=2)
                    except ModelRegistryError as exc:
                        if exc.code != "cancelled":
                            return _cancelled_model_download_result(model_ref, repo_id)
                        result = {
                            "ok": False,
                            "error_kind": exc.code,
                            "detail": exc.message,
                        }
                        result.update(exc.details)
                        return result
                    except asyncio.TimeoutError:
                        raise
                    return _cancelled_model_download_result(model_ref, repo_id)
                except ModelRegistryError as exc:
                    result = {
                        "ok": False,
                        "error_kind": exc.code,
                        "detail": exc.message,
                    }
                    result.update(exc.details)
                    return result
                emit_download(
                    {
                        "kind": "committed",
                        "text": f"Verifying model {repo_id}",
                        "level": "INFO",
                        "phase": DownloadPhase.VERIFYING.value,
                    }
                )
                return {
                    "ok": True,
                    # 6a: surface download_hf_model's honest detail (e.g. a divergent
                    # "downloaded revision X; pin unchanged"), not a fixed "model cached".
                    "detail": str(downloaded.get("detail") or "model cached"),
                    "entry_id": downloaded.get("entry_id"),
                    "cache_state": downloaded.get("cache_state"),
                    "entry": downloaded.get("entry"),
                    "snapshot_path": downloaded.get("snapshot_path"),
                }
            finally:
                download_log.close()

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
        all_runs: bool = False,
    ) -> AsyncIterator[dict[str, Any]]:
        selected_run_ids = None if all_runs else {str(run_id) for run_id in run_ids}
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        if selected_run_ids is None:
            self._all_subscribers.append(queue)
        else:
            for run_id in selected_run_ids:
                self._subscribers.setdefault(run_id, []).append(queue)

        async def iterator() -> AsyncIterator[dict[str, Any]]:
            try:
                for event in self._replay_events(selected_run_ids, resume_from):
                    yield event
                while True:
                    yield await queue.get()
            finally:
                if selected_run_ids is None:
                    if queue in self._all_subscribers:
                        self._all_subscribers.remove(queue)
                else:
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
        for queue in list(self._all_subscribers):
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
        self, run_ids: set[str] | None, resume_from: object
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
        replay_run_ids = set(self._event_buffers) if run_ids is None else run_ids
        for run_id in replay_run_ids:
            for event in self._event_buffers.get(run_id, []):
                if event["seq"] > min_seq:
                    events.append(event)
        return sorted(
            events,
            key=lambda item: (_event_stream_id(item) or "", int(item["seq"])),
        )

    def _replay_durable_log_events(
        self, run_ids: set[str] | None, resume_from: object
    ) -> list[dict[str, Any]]:
        assert isinstance(resume_from, dict)
        expected_inode = int(resume_from["log_inode"])
        start_position = max(0, int(resume_from["byte_offset"]))
        events: list[dict[str, Any]] = []
        replay_run_ids = set(self._detached_runs) if run_ids is None else run_ids
        for run_id in replay_run_ids:
            run = self._detached_run_or_error(run_id)
            active_log = run.manifest.active_log
            if active_log.inode != expected_inode:
                if not _manifest_has_rotated_inode(run.manifest, expected_inode):
                    if run_ids is None:
                        continue
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


def _default_config_dir() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "vela"


def _diagnose_gpu_poll(
    sampler: Callable[[], GpuPollResult],
) -> GpuPollResult:
    try:
        return sampler()
    except Exception as exc:
        return GpuPollResult(
            [],
            note=f"GPU stats unavailable: {exc}",
            unavailable=True,
        )


def _diagnose_gpu_payload(result: GpuPollResult) -> dict[str, Any]:
    names = [sample.name for sample in result.samples if sample.name]
    note = result.note or None
    return {
        "available": not result.unavailable and bool(result.samples),
        "count": len(result.samples),
        "names": names,
        "architecture": _gpu_architecture_from_names(names),
        "note": note,
    }


def _gpu_architecture_from_names(names: list[str]) -> str | None:
    joined = " ".join(names).lower()
    if "blackwell" in joined:
        return "Blackwell"
    if "hopper" in joined or "h100" in joined or "h200" in joined:
        return "Hopper"
    if "ada" in joined or "rtx 6000" in joined or "l40" in joined:
        return "Ada"
    if "ampere" in joined or "a100" in joined or "a40" in joined or "a6000" in joined:
        return "Ampere"
    if "turing" in joined or "t4" in joined:
        return "Turing"
    if "volta" in joined or "v100" in joined:
        return "Volta"
    return None


def _cuda_toolkit_version() -> str | None:
    env_version = os.environ.get("CUDA_VERSION")
    if env_version and env_version.strip():
        return env_version.strip()
    for env_name in ("CUDA_HOME", "CUDA_PATH"):
        root = os.environ.get(env_name)
        if root:
            version = _cuda_version_from_file(Path(root).expanduser() / "version.txt")
            if version is not None:
                return version
    version = _cuda_version_from_file(Path("/usr/local/cuda/version.txt"))
    if version is not None:
        return version
    nvcc = shutil.which("nvcc")
    if nvcc is None:
        return None
    try:
        proc = subprocess.run(
            [nvcc, "--version"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except Exception:
        return None
    return _cuda_version_from_text(proc.stdout + "\n" + proc.stderr)


def _cuda_version_from_file(path: Path) -> str | None:
    try:
        return _cuda_version_from_text(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None


def _cuda_version_from_text(text: str) -> str | None:
    match = re.search(r"release\s+([0-9]+(?:\.[0-9]+)*)", text, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    match = re.search(r"CUDA\s+Version\s+([0-9]+(?:\.[0-9]+)*)", text, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    return None


def _diagnose_active_state(
    builds_root: Path,
    live_sidecars: list[Sidecar] | None = None,
) -> dict[str, Any]:
    return {
        "build": _diagnose_active_build(builds_root),
        "model": _diagnose_active_model(live_sidecars or []),
    }


def _diagnose_active_build(builds_root: Path) -> dict[str, Any] | None:
    try:
        payload = list_builds(builds_root)
    except Exception:
        return None
    default_build_id = payload.get("default_build_id")
    if not isinstance(default_build_id, str) or not default_build_id:
        return None
    builds = payload.get("builds")
    if not isinstance(builds, list):
        return None
    for build in builds:
        if not isinstance(build, dict) or build.get("build_id") != default_build_id:
            continue
        resolved = build.get("resolved") if isinstance(build.get("resolved"), dict) else {}
        return {
            "build_id": default_build_id,
            "label": str(build.get("label") or ""),
            "status": str(build.get("status") or "unknown"),
            "vllm": _optional_str(resolved.get("vllm")),
            "cuda": _optional_str(resolved.get("cuda")),
        }
    return None


def _diagnose_active_model(live_sidecars: list[Sidecar]) -> dict[str, Any] | None:
    for sidecar in live_sidecars:
        served_model_name = sidecar.served_model_names[0] if sidecar.served_model_names else None
        return {
            "run_id": sidecar.run_id,
            "config_name": sidecar.config_name,
            "served_model_name": served_model_name,
            "model_ref": sidecar.model_ref,
            "entry_id": sidecar.model_entry_id,
            "repo_id": sidecar.model_repo_id,
            "revision": sidecar.model_commit_sha or sidecar.model_revision,
            "runtime": sidecar.runtime,
            "host": sidecar.host,
            "port": sidecar.port,
        }
    return None


def _diagnose_auth_status() -> dict[str, str]:
    try:
        token = configured_agent_token()
    except AgentTokenError as exc:
        return {"status": "malformed-token", "detail": str(exc)}
    if token is None:
        return {"status": "none", "detail": "no agent token required"}
    return {"status": "required+provided", "detail": "agent token accepted"}


def _config_payload_from_params(params: dict[str, Any], *, method: str) -> dict[str, Any]:
    config = params.get("config")
    if isinstance(config, dict):
        return dict(config)
    yaml_text = params.get("yaml")
    if not isinstance(yaml_text, str) or not yaml_text.strip():
        raise TargetCallError("invalid-params", f"{method} requires config mapping or yaml text")
    try:
        payload = yaml.safe_load(yaml_text)
    except Exception as exc:
        raise TargetCallError("invalid-config", f"unable to parse YAML: {exc}") from exc
    if not isinstance(payload, dict):
        raise TargetCallError("invalid-config", "config root must be a mapping")
    return dict(payload)


def _listening_ports() -> set[int]:
    try:
        proc = subprocess.run(
            ["ss", "-ltn"],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, ValueError):
        return set()
    if proc.returncode != 0:
        return set()
    ports: set[int] = set()
    for line in proc.stdout.splitlines():
        port = _listening_port_from_ss_line(line)
        if port is not None:
            ports.add(port)
    return ports


def _docker_container_names() -> set[str]:
    docker = shutil.which("docker")
    if docker is None:
        return set()
    try:
        proc = subprocess.run(
            [docker, "ps", "-a", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return set()
    if proc.returncode != 0:
        return set()
    return {
        line.strip().lstrip("/")
        for line in proc.stdout.splitlines()
        if line.strip()
    }


def _fresh_docker_container_name(
    preferred: str,
    occupied_container_names: list[str] | None,
) -> str:
    occupied = {
        str(name).strip().lstrip("/")
        for name in (occupied_container_names or [])
        if str(name).strip()
    }
    if preferred not in occupied:
        return preferred
    suffix = 2
    while f"{preferred}-{suffix}" in occupied:
        suffix += 1
    return f"{preferred}-{suffix}"


def _listening_port_from_ss_line(line: str) -> int | None:
    parts = line.split()
    if len(parts) < 4 or parts[0] != "LISTEN":
        return None
    return _port_from_sockaddr(parts[-2])


def _port_from_sockaddr(value: str) -> int | None:
    candidate = value.rsplit(":", 1)[-1]
    if candidate == "*" or not candidate:
        return None
    try:
        port = int(candidate)
    except ValueError:
        return None
    return port if port > 0 else None


async def _build_subprocess_exec(
    argv: list[str],
    *,
    env: dict[str, str],
    cwd: Path,
    emit: JobProgressEmitter,
    phase: str,
    cancel_event: asyncio.Event,
) -> int:
    if cancel_event.is_set():
        return 130
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    assert process.stdout is not None
    try:
        while True:
            chunk = await process.stdout.readline()
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace").rstrip("\r\n")
            if text:
                line_phase = _build_install_phase_for_line(text, phase)
                emit(
                    {
                        "kind": "committed",
                        "text": text,
                        "level": level_for_line(text),
                        "phase": line_phase,
                    }
                )
            if cancel_event.is_set() and process.returncode is None:
                process.terminate()
        return int(await process.wait())
    except asyncio.CancelledError:
        await _terminate_build_subprocess(process)
        raise


def _build_install_phase_for_line(text: str, default_phase: str) -> str:
    for pattern, phase in BUILD_INSTALL_PHASE_RULES:
        if pattern.search(text):
            return phase.value
    return default_phase


def _classify_build_install_failure(lines: list[str], *, default: str) -> str:
    text = "\n".join(lines)
    for pattern, error_kind in BUILD_INSTALL_ERROR_RULES:
        if pattern.search(text):
            return error_kind
    return default


async def _terminate_build_subprocess(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=2)
    except asyncio.TimeoutError:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        await process.wait()


def _managed_build_resolved_versions(venv_path: Path) -> dict[str, str]:
    python_bin = venv_path / "bin" / "python"
    vllm_bin = venv_path / "bin" / "vllm"
    import_version = _run_build_probe(
        [
            str(python_bin),
            "-c",
            "import vllm; print(vllm.__version__)",
        ],
        "vLLM import failed",
    )
    vllm_version_output = _run_build_probe(
        [str(vllm_bin), "--version"],
        "vLLM version probe failed",
    )
    if import_version not in vllm_version_output:
        raise BuildRegistryError(
            "invalid-config",
            "vLLM executable version and Python import version disagree",
            {
                "reason": "vllm-version-mismatch",
                "executable_version": vllm_version_output,
                "import_version": import_version,
            },
        )
    python_version = _run_build_probe(
        [str(python_bin), "--version"],
        "Python version probe failed",
    )
    profile = select_profile(import_version)
    return {
        "vllm": import_version,
        "vllm_version_profile": profile.version,
        "python": python_version.replace("Python ", "", 1),
    }


def _run_build_probe(argv: list[str], failure_message: str) -> str:
    try:
        result = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BuildRegistryError(
            "invalid-config",
            failure_message,
            {"reason": "build-probe-failed", "command": argv},
        ) from exc
    output = (result.stdout or result.stderr or "").strip()
    if result.returncode != 0 or not output:
        raise BuildRegistryError(
            "invalid-config",
            failure_message,
            {
                "reason": "build-probe-failed",
                "command": argv,
                "returncode": result.returncode,
                "output": output,
            },
        )
    return output.splitlines()[-1].strip()


def _write_managed_build_artifacts(build_dir: Path, venv_path: Path) -> None:
    bin_dir = build_dir / "bin"
    bin_dir.mkdir(exist_ok=True)
    _replace_symlink(bin_dir / "vllm", venv_path / "bin" / "vllm")
    _write_venv_python_wrapper(bin_dir / "python", build_dir)
    _replace_symlink(build_dir / "activate", venv_path / "bin" / "activate")
    run_script = build_dir / "run.sh"
    run_script.write_text(
        "\n".join(
            [
                "#!/bin/sh",
                "set -eu",
                f'BUILD_ROOT="{build_dir}"',
                'export VIRTUAL_ENV="${BUILD_ROOT}/venv"',
                'export PATH="${VIRTUAL_ENV}/bin:${PATH}"',
                'exec "${BUILD_ROOT}/bin/vllm" "$@"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    run_script.chmod(0o755)


def _replace_symlink(link_path: Path, target_path: Path) -> None:
    link_path.unlink(missing_ok=True)
    link_path.symlink_to(target_path)


def _write_venv_python_wrapper(path: Path, build_dir: Path) -> None:
    path.unlink(missing_ok=True)
    path.write_text(
        "\n".join(
            [
                "#!/bin/sh",
                "set -eu",
                f'BUILD_ROOT="{build_dir}"',
                'export VIRTUAL_ENV="${BUILD_ROOT}/venv"',
                'export PATH="${VIRTUAL_ENV}/bin:${PATH}"',
                'exec "${BUILD_ROOT}/venv/bin/python" "$@"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    path.chmod(0o755)


def _managed_build_manifest(
    *,
    build_id: str,
    label: str,
    status: str,
    install: dict[str, Any],
    resolved: dict[str, str],
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "schema_version": 1,
        "build_id": build_id,
        "label": label,
        "status": status,
        "install": dict(install),
        "resolved": dict(resolved),
        "paths": {
            "root": "",
            "venv": "venv",
            "executable": "bin/vllm",
            "python": "bin/python",
            "activate": "activate",
            "run_script": "run.sh",
        },
        "created_at": now,
        "last_used_at": None,
        "notes": "",
    }


def _managed_build_integrity(executable: Path) -> dict[str, Any]:
    return {
        "strategy": "executable_sha256",
        "executable_sha256": _file_sha256_uri(executable),
    }


def _acquire_build_install_lock(build_dir: Path) -> Any:
    lock_path = build_dir / "build.lock"
    handle = lock_path.open("a", encoding="utf-8")
    fcntl.flock(handle, fcntl.LOCK_EX)
    return handle


def _release_build_install_lock(handle: Any) -> None:
    try:
        fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        handle.close()


def _file_sha256_uri(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _write_build_manifest(build_dir: Path, manifest: dict[str, Any]) -> None:
    payload = dict(manifest)
    paths = dict(payload.get("paths") if isinstance(payload.get("paths"), dict) else {})
    paths["root"] = str(build_dir)
    payload["paths"] = paths
    manifest["paths"] = paths
    _write_private_text_atomic(
        build_dir / "build.json",
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
    )


def _write_private_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            fd = -1
            file.write(text)
    finally:
        if fd >= 0:
            os.close(fd)
    os.replace(tmp, path)


def _write_public_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            fd = -1
            file.write(text)
    finally:
        if fd >= 0:
            os.close(fd)
    os.replace(tmp, path)
    path.chmod(0o644)


def _write_executable_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o755)
    try:
        os.fchmod(fd, 0o755)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            fd = -1
            file.write(text)
    finally:
        if fd >= 0:
            os.close(fd)
    os.replace(tmp, path)
    path.chmod(0o755)


def _safe_config_file_stem(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "-", name.strip()).strip("-")
    if not stem or stem in {".", ".."}:
        raise TargetCallError("invalid-params", "config name cannot be used as a file name")
    return stem


def _failed_build_result(
    build_dir: Path,
    *,
    build_id: str,
    label: str,
    install: dict[str, Any],
    error_kind: str,
    detail: str,
    exit_code: int,
) -> dict[str, Any]:
    failed_install = dict(install)
    failed_install["exit_code"] = exit_code
    manifest = _managed_build_manifest(
        build_id=build_id,
        label=label,
        status="failed",
        install=failed_install,
        resolved={},
    )
    _write_build_manifest(build_dir, manifest)
    return {
        "ok": False,
        "error_kind": error_kind,
        "detail": detail,
        "build_id": build_id,
        "label": label,
        "status": "failed",
        "exit_code": exit_code,
        "manifest": _build_job_manifest_payload(manifest),
    }


def _cancelled_job_result(kind: str, params: dict[str, Any]) -> dict[str, Any]:
    if kind == "create_build":
        build_id = _optional_param_str(params.get("build_id")) or _job_id_param(params)
        return {
            "ok": False,
            "error_kind": "cancelled",
            "detail": "build install cancelled",
            "build_id": build_id,
            "status": "failed",
            "exit_code": 130,
        }
    return {"ok": False, "error_kind": "cancelled", "detail": "cancelled"}


def _model_download_progress_payload(
    repo_id: str,
    progress: dict[str, Any],
) -> dict[str, Any]:
    percent = _optional_int(progress.get("percent"))
    bytes_done = _optional_int(progress.get("bytes_done"))
    bytes_total = _optional_int(progress.get("bytes_total"))
    text = f"Downloading model {repo_id}"
    if percent is not None:
        text = f"{text} {percent}%"
    payload: dict[str, Any] = {
        "kind": "transient",
        "text": text,
        "level": "INFO",
        "phase": DownloadPhase.DOWNLOADING.value,
    }
    if percent is not None:
        payload["percent"] = percent
    if bytes_done is not None:
        payload["bytes_done"] = bytes_done
    if bytes_total is not None:
        payload["bytes_total"] = bytes_total
    return payload


def _model_download_log_path(registry_path: Path, entry_id: str) -> Path:
    safe_entry_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", entry_id).strip("._")
    if not safe_entry_id:
        safe_entry_id = "model"
    return registry_path.parent / "downloads" / f"{safe_entry_id}.log"


def _cancelled_model_download_result(model_ref: object, repo_id: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error_kind": "cancelled",
        "detail": "model download cancelled",
        "model_ref": str(model_ref),
        "repo_id": repo_id,
        "cache_state": "partial",
    }


def _build_job_manifest_payload(manifest: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(manifest))


def _env_overrides(value: object) -> dict[str, str]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return {
            str(key): str(item)
            for key, item in value.items()
            if str(key) and str(item)
        }
    if not isinstance(value, list):
        return {}
    env: dict[str, str] = {}
    for item in value:
        text = str(item)
        if "=" not in text:
            continue
        key, env_value = text.split("=", 1)
        if key:
            env[key] = env_value
    return env


def _env_with_build_overlay(
    base_env: Mapping[str, str], overlay: Mapping[str, str]
) -> dict[str, str]:
    env = dict(base_env)
    virtual_env = overlay.get("VIRTUAL_ENV")
    if virtual_env:
        env["VIRTUAL_ENV"] = str(virtual_env)
    path_prepend = overlay.get("PATH_PREPEND")
    if path_prepend:
        existing_path = env.get("PATH", "")
        env["PATH"] = (
            f"{path_prepend}{os.pathsep}{existing_path}"
            if existing_path
            else str(path_prepend)
        )
    env.pop("PATH_PREPEND", None)
    return env


def _build_install_request(
    method: str,
    params: dict[str, Any],
    *,
    venv_path: Path,
) -> BuildInstallRequest:
    python_requested = _optional_param_str(params.get("python"))
    uv_path = _find_uv_executable()
    if method == "pip":
        pip_spec = _optional_param_str(params.get("spec"))
        if pip_spec is None:
            raise BuildRegistryError(
                "invalid-params",
                "create_build method=pip requires spec",
                {"reason": "missing-spec"},
            )
        return _pip_install_request(
            pip_spec,
            uv_path=uv_path,
            python_requested=python_requested,
            venv_path=venv_path,
        )
    if method == "nightly":
        if uv_path is None:
            raise BuildRegistryError(
                "feature-unavailable",
                "create_build method=nightly requires uv",
                {"reason": "uv-required", "method": "nightly"},
            )
        channel = _optional_param_str(
            params.get("channel")
            or params.get("nightly_channel")
            or params.get("variant")
        )
        index_url = _nightly_index_url(channel)
        return _uv_install_request(
            method="nightly",
            uv_path=uv_path,
            python_requested=python_requested,
            venv_path=venv_path,
            install_args=[
                "-U",
                "vllm",
                "--torch-backend=auto",
                "--extra-index-url",
                index_url,
            ],
            provenance={
                "pip_spec": None,
                "nightly_channel": channel,
                "index_url": index_url,
                "torch_backend": "auto",
            },
        )
    if method == "commit":
        if uv_path is None:
            raise BuildRegistryError(
                "feature-unavailable",
                "create_build method=commit requires uv",
                {"reason": "uv-required", "method": "commit"},
            )
        commit_sha = _optional_param_str(
            params.get("commit") or params.get("sha") or params.get("vllm_commit")
        )
        if commit_sha is None:
            raise BuildRegistryError(
                "invalid-params",
                "create_build method=commit requires commit",
                {"reason": "missing-commit", "method": "commit"},
            )
        channel = _optional_param_str(params.get("channel") or params.get("variant"))
        index_url = _commit_index_url(commit_sha, channel)
        return _uv_install_request(
            method="commit",
            uv_path=uv_path,
            python_requested=python_requested,
            venv_path=venv_path,
            install_args=[
                "vllm",
                "--torch-backend=auto",
                "--extra-index-url",
                index_url,
            ],
            provenance={
                "pip_spec": None,
                "vllm_commit": commit_sha,
                "nightly_channel": channel,
                "index_url": index_url,
                "torch_backend": "auto",
            },
        )
    if method == "wheel":
        wheel_path = _wheel_path_param(params)
        extra_index_url = _optional_param_str(
            params.get("extra_index_url")
            or params.get("torch_index")
            or params.get("index_url")
        )
        return _wheel_install_request(
            wheel_path,
            extra_index_url=extra_index_url,
            uv_path=uv_path,
            python_requested=python_requested,
            venv_path=venv_path,
        )
    if method == "git":
        return _git_install_request(
            params,
            uv_path=uv_path,
            python_requested=python_requested,
            venv_path=venv_path,
        )
    raise BuildRegistryError(
        "feature-unavailable",
        f"create_build method is not implemented: {method or 'unknown'}",
        {"method": method or "unknown"},
    )


def _pip_install_request(
    pip_spec: str,
    *,
    uv_path: str | None,
    python_requested: str | None,
    venv_path: Path,
) -> BuildInstallRequest:
    if uv_path is not None:
        return _uv_install_request(
            method="pip",
            uv_path=uv_path,
            python_requested=python_requested,
            venv_path=venv_path,
            install_args=[pip_spec, "--torch-backend=auto"],
            provenance={"pip_spec": pip_spec, "torch_backend": "auto"},
        )
    return BuildInstallRequest(
        method="pip",
        installer="pip",
        provenance={"pip_spec": pip_spec},
        venv_argv=_pip_fallback_venv_argv(venv_path),
        install_argv=[
            str(venv_path / "bin" / "python"),
            "-m",
            "pip",
            "install",
            pip_spec,
        ],
        pre_install_argvs=[_pip_bootstrap_argv(venv_path)],
    )


def _uv_install_request(
    *,
    method: str,
    uv_path: str,
    python_requested: str | None,
    venv_path: Path,
    install_args: list[str],
    provenance: dict[str, Any],
    pre_install_argvs: list[list[str]] | None = None,
    env_overrides: dict[str, str] | None = None,
) -> BuildInstallRequest:
    venv_argv = [uv_path, "venv"]
    if python_requested is not None:
        venv_argv.extend(["--python", python_requested])
    venv_argv.append(str(venv_path))
    return BuildInstallRequest(
        method=method,
        installer="uv",
        provenance=provenance,
        venv_argv=venv_argv,
        install_argv=[
            uv_path,
            "pip",
            "install",
            "--python",
            str(venv_path / "bin" / "python"),
            *install_args,
        ],
        pre_install_argvs=list(pre_install_argvs or []),
        env_overrides=dict(env_overrides or {}),
    )


def _wheel_install_request(
    wheel_path: Path,
    *,
    extra_index_url: str | None,
    uv_path: str | None,
    python_requested: str | None,
    venv_path: Path,
) -> BuildInstallRequest:
    install_args = [str(wheel_path)]
    provenance = {
        "local_wheel_path": str(wheel_path),
        "index_url": extra_index_url,
    }
    if extra_index_url is not None:
        install_args.extend(["--extra-index-url", extra_index_url])
    if uv_path is not None:
        return _uv_install_request(
            method="wheel",
            uv_path=uv_path,
            python_requested=python_requested,
            venv_path=venv_path,
            install_args=[str(wheel_path), "--torch-backend=auto"]
            + (["--extra-index-url", extra_index_url] if extra_index_url is not None else []),
            provenance={**provenance, "torch_backend": "auto"},
        )
    return BuildInstallRequest(
        method="wheel",
        installer="pip",
        provenance=provenance,
        venv_argv=_pip_fallback_venv_argv(venv_path),
        install_argv=[
            str(venv_path / "bin" / "python"),
            "-m",
            "pip",
            "install",
            *install_args,
        ],
        pre_install_argvs=[_pip_bootstrap_argv(venv_path)],
    )


def _wheel_path_param(params: dict[str, Any]) -> Path:
    value = _optional_param_str(params.get("path") or params.get("wheel_path"))
    if value is None:
        raise BuildRegistryError(
            "invalid-params",
            "create_build method=wheel requires path",
            {"reason": "missing-wheel-path", "method": "wheel"},
        )
    path = Path(value).expanduser()
    if not path.is_file():
        raise BuildRegistryError(
            "invalid-config",
            f"wheel path does not exist: {path}",
            {"reason": "missing-wheel", "path": str(path), "method": "wheel"},
        )
    return path


def _git_install_request(
    params: dict[str, Any],
    *,
    uv_path: str | None,
    python_requested: str | None,
    venv_path: Path,
) -> BuildInstallRequest:
    git_url = _optional_param_str(params.get("url") or params.get("git_url"))
    if git_url is None:
        raise BuildRegistryError(
            "invalid-params",
            "create_build method=git requires url",
            {"reason": "missing-git-url", "method": "git"},
        )
    git_ref = _optional_param_str(params.get("ref") or params.get("git_ref"))
    precompiled = _param_bool(params.get("precompiled"))
    source_dir = venv_path.parent / "source"
    preinstall_argvs = [["git", "clone", git_url, str(source_dir)]]
    if git_ref is not None:
        preinstall_argvs.append(["git", "-C", str(source_dir), "checkout", git_ref])
    provenance = {
        "git_url": git_url,
        "git_ref": git_ref,
        "precompiled": precompiled,
    }
    env_overrides = {"VLLM_USE_PRECOMPILED": "1"} if precompiled else {}
    if uv_path is not None:
        install_args = ["-e", str(source_dir)]
        if not precompiled:
            install_args.append("--torch-backend=auto")
            provenance["torch_backend"] = "auto"
        return _uv_install_request(
            method="git",
            uv_path=uv_path,
            python_requested=python_requested,
            venv_path=venv_path,
            install_args=install_args,
            provenance=provenance,
            pre_install_argvs=preinstall_argvs,
            env_overrides=env_overrides,
        )
    return BuildInstallRequest(
        method="git",
        installer="pip",
        provenance=provenance,
        venv_argv=_pip_fallback_venv_argv(venv_path),
        install_argv=[
            str(venv_path / "bin" / "python"),
            "-m",
            "pip",
            "install",
            "-e",
            str(source_dir),
        ],
        pre_install_argvs=[_pip_bootstrap_argv(venv_path), *preinstall_argvs],
        env_overrides=env_overrides,
    )


def _pip_fallback_venv_argv(venv_path: Path) -> list[str]:
    return [sys.executable, "-m", "venv", "--without-pip", str(venv_path)]


def _pip_bootstrap_argv(venv_path: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "pip",
        "--python",
        str(venv_path / "bin" / "python"),
        "install",
        "pip",
    ]


def _nightly_index_url(channel: str | None) -> str:
    if channel is None:
        return VLLM_NIGHTLY_INDEX_BASE
    return f"{VLLM_NIGHTLY_INDEX_BASE}/{channel.strip('/')}"


def _commit_index_url(commit_sha: str, channel: str | None) -> str:
    base = f"{VLLM_COMMIT_INDEX_BASE}/{commit_sha}"
    if channel is None:
        return base
    return f"{base}/{channel.strip('/')}"


def _find_uv_executable() -> str | None:
    return shutil.which("uv")


def _job_secret_values(
    params: dict[str, Any], env_overrides: dict[str, str]
) -> list[str]:
    secrets: list[str] = []
    _collect_job_param_secret_values(params, secrets)
    for mapping in (env_overrides, os.environ):
        for key, value in mapping.items():
            text = _optional_param_str(value)
            if text is None:
                continue
            if _job_secret_key(key):
                _append_secret_value(secrets, text)
            _append_url_userinfo_secrets(secrets, text)
    return secrets


def _dedupe_secret_values(values: list[str]) -> list[str]:
    secrets: list[str] = []
    for value in values:
        if value and value not in secrets:
            secrets.append(value)
    return secrets


def _collect_job_param_secret_values(value: object, secrets: list[str], key: str = "") -> None:
    if isinstance(value, dict):
        for item_key, item_value in value.items():
            item_key_text = str(item_key)
            if _job_secret_key(item_key_text):
                for text in _iter_string_values(item_value):
                    _append_secret_value(secrets, text)
            _collect_job_param_secret_values(item_value, secrets, item_key_text)
        return
    if isinstance(value, list):
        for item in value:
            _collect_job_param_secret_values(item, secrets, key)
        return
    text = _optional_param_str(value)
    if text is None:
        return
    if _job_secret_key(key):
        _append_secret_value(secrets, text)
    _append_url_userinfo_secrets(secrets, text)


def _iter_string_values(value: object) -> list[str]:
    if isinstance(value, dict):
        strings: list[str] = []
        for item in value.values():
            strings.extend(_iter_string_values(item))
        return strings
    if isinstance(value, list):
        strings = []
        for item in value:
            strings.extend(_iter_string_values(item))
        return strings
    text = _optional_param_str(value)
    return [text] if text is not None else []


def _job_secret_key(key: str) -> bool:
    upper = key.upper()
    return any(marker in upper for marker in JOB_SECRET_ENV_MARKERS)


def _append_url_userinfo_secrets(secrets: list[str], text: str) -> None:
    for match in URL_TEXT_RE.finditer(text):
        url = match.group(0).rstrip(".,;)]}")
        try:
            parsed = urlsplit(url)
        except ValueError:
            continue
        if "@" not in parsed.netloc:
            continue
        raw_userinfo = parsed.netloc.rsplit("@", 1)[0]
        _append_secret_value(secrets, raw_userinfo)
        decoded_userinfo = unquote(raw_userinfo)
        _append_secret_value(secrets, decoded_userinfo)
        if ":" in decoded_userinfo:
            _append_secret_value(secrets, decoded_userinfo.split(":", 1)[1])
        try:
            password = parsed.password
        except ValueError:
            password = None
        if password is not None:
            _append_secret_value(secrets, password)
            _append_secret_value(secrets, unquote(password))


def _append_secret_value(secrets: list[str], value: object) -> None:
    text = _optional_param_str(value)
    if text is None or len(text) < 4:
        return
    if text.lower() in {"true", "false", "none", "null"}:
        return
    if text not in secrets:
        secrets.append(text)


def _scrub_job_payload(value: Any, *, secrets: list[str]) -> Any:
    if isinstance(value, str):
        return scrub_secret_text(value, secrets=secrets)
    if isinstance(value, list):
        return [_scrub_job_payload(item, secrets=secrets) for item in value]
    if isinstance(value, tuple):
        return [_scrub_job_payload(item, secrets=secrets) for item in value]
    if isinstance(value, dict):
        return {
            key: _scrub_job_payload(item, secrets=secrets)
            for key, item in value.items()
        }
    return value


def _configs_pinning_model(
    registry: ConfigRegistry, aliases: set[str], entry: dict[str, Any]
) -> list[str]:
    pinned = []
    for item in registry.valid:
        cfg = item.config
        if cfg.model_ref is not None and cfg.model_ref in aliases:
            pinned.append(cfg.name)
        elif _config_pins_hf_model_revision(cfg, entry):
            pinned.append(cfg.name)
    return sorted(pinned)


def _config_pins_hf_model_revision(cfg: ModelConfig, entry: dict[str, Any]) -> bool:
    if cfg.model_ref is not None or not cfg.revision:
        return False
    if _optional_param_str(entry.get("source")) != "hf_repo":
        return False
    repo_id = _optional_param_str(entry.get("repo_id"))
    if repo_id is None or cfg.model != repo_id:
        return False
    pinned_revisions = {
        value
        for value in (
            _optional_param_str(entry.get("commit_sha")),
            _optional_param_str(entry.get("revision")),
        )
        if value is not None
    }
    return cfg.revision in pinned_revisions


def _configs_referencing_model(
    registry: ConfigRegistry, entry: dict[str, Any]
) -> list[str]:
    aliases = {
        str(entry.get("entry_id") or ""),
        str(entry.get("repo_id") or ""),
        str(entry.get("display_name") or ""),
    } - {""}
    refs = [
        item.config.name
        for item in registry.valid
        if (item.config.model_ref and str(item.config.model_ref) in aliases)
        or str(item.config.model) in aliases
    ]
    return sorted(refs)


def _configs_pinning_build(registry: ConfigRegistry, aliases: set[str]) -> list[str]:
    pinned = [
        item.config.name
        for item in registry.valid
        if item.config.command.build is not None and item.config.command.build in aliases
    ]
    return sorted(pinned)


def _build_payload_aliases(build: dict[str, Any]) -> set[str]:
    aliases: set[str] = set()
    for key in ("build_id", "label"):
        value = build.get(key)
        if isinstance(value, str) and value:
            aliases.add(value)
    return aliases


def _sidecar_using_build(manifest: dict[str, Any], sidecars: list[Sidecar]) -> Sidecar | None:
    aliases = _build_payload_aliases(manifest)
    candidates = _build_executable_path_keys(manifest)
    if not candidates and not aliases:
        return None
    for sidecar in sidecars:
        sidecar_build_id = _optional_param_str(sidecar.build_id)
        if sidecar_build_id is not None and sidecar_build_id in aliases:
            return sidecar
        if candidates.intersection(_sidecar_executable_path_keys(sidecar)):
            return sidecar
    return None


def _build_executable_path_keys(manifest: dict[str, Any]) -> set[str]:
    paths = _dict_or_empty(manifest.get("paths"))
    root_value = _optional_param_str(paths.get("root"))
    if root_value is None:
        return set()
    root = Path(root_value).expanduser()
    keys: set[str] = set()
    for value in (paths.get("executable"), paths.get("python")):
        text = _optional_param_str(value)
        if text is not None:
            keys.add(_path_key(text, base=root))
    return keys


def _sidecar_executable_path_keys(sidecar: Sidecar) -> set[str]:
    base = Path(sidecar.cwd).expanduser() if sidecar.cwd else None
    keys: set[str] = set()
    executable = _optional_param_str(sidecar.executable)
    if executable is not None:
        keys.add(_path_key(executable, base=base))
    if sidecar.command_argv:
        keys.add(_path_key(sidecar.command_argv[0], base=base))
    return keys


def _sidecar_using_model(
    entry: dict[str, Any], aliases: set[str], sidecars: list[Sidecar]
) -> Sidecar | None:
    for sidecar in sidecars:
        if _sidecar_matches_model(sidecar, entry, aliases):
            return sidecar
    return None


def _sidecar_matches_model(
    sidecar: Sidecar, entry: dict[str, Any], aliases: set[str]
) -> bool:
    typed_refs = {
        value
        for value in (
            _optional_param_str(sidecar.model_ref),
            _optional_param_str(sidecar.model_entry_id),
        )
        if value is not None
    }
    if typed_refs.intersection(aliases):
        return True

    snapshot = _dict_or_empty(sidecar.config_snapshot)
    snapshot_model_ref = _optional_param_str(snapshot.get("model_ref"))
    if snapshot_model_ref is not None and snapshot_model_ref in aliases:
        return True

    source = _optional_param_str(entry.get("source"))
    if source == "local_path":
        return _sidecar_matches_local_model(sidecar, entry, snapshot)
    if source == "hf_repo":
        return _sidecar_matches_hf_model(sidecar, entry, snapshot)
    return False


def _sidecar_matches_local_model(
    sidecar: Sidecar, entry: dict[str, Any], snapshot: dict[str, Any]
) -> bool:
    local_path = _optional_param_str(entry.get("local_path"))
    if local_path is None:
        return False
    target = _path_key(local_path)
    for value in (
        snapshot.get("model"),
        snapshot.get("local_path"),
        _argv_model_value(sidecar.command_argv),
    ):
        text = _optional_param_str(value)
        if text is not None and _path_key(text, base=Path(sidecar.cwd)) == target:
            return True
    return False


def _sidecar_matches_hf_model(
    sidecar: Sidecar, entry: dict[str, Any], snapshot: dict[str, Any]
) -> bool:
    repo_id = _optional_param_str(entry.get("repo_id"))
    if repo_id is None:
        return False
    model_values = {
        value
        for value in (
            _optional_param_str(sidecar.model_repo_id),
            _optional_param_str(snapshot.get("model")),
            _optional_param_str(snapshot.get("repo_id")),
            _argv_model_value(sidecar.command_argv),
        )
        if value is not None
    }
    if repo_id not in model_values:
        return False
    target_revisions = {
        value
        for value in (
            _optional_param_str(entry.get("commit_sha")),
            _optional_param_str(entry.get("revision")),
        )
        if value is not None
    }
    sidecar_revisions = {
        value
        for value in (
            _optional_param_str(sidecar.model_commit_sha),
            _optional_param_str(sidecar.model_revision),
            _optional_param_str(snapshot.get("revision")),
            _argv_revision_value(sidecar.command_argv),
        )
        if value is not None
    }
    return not target_revisions or not sidecar_revisions or bool(
        target_revisions.intersection(sidecar_revisions)
    )


def _argv_model_value(argv: list[str]) -> str | None:
    option_value = _argv_option_value(argv, {"--model"})
    if option_value is not None:
        return option_value
    for index, item in enumerate(argv[:-1]):
        if item == "serve":
            candidate = argv[index + 1]
            if candidate and not candidate.startswith("-"):
                return candidate
    return None


def _argv_revision_value(argv: list[str]) -> str | None:
    return _argv_option_value(argv, {"--revision", "--model-revision"})


def _argv_option_value(argv: list[str], options: set[str]) -> str | None:
    for index, item in enumerate(argv):
        if item in options and index + 1 < len(argv):
            return argv[index + 1]
        for option in options:
            prefix = option + "="
            if item.startswith(prefix):
                return item[len(prefix) :]
    return None


def _live_run_resource_details(
    sidecar: Sidecar, *, resource_key: str, resource_value: str
) -> dict[str, Any]:
    return {
        resource_key: resource_value,
        "reason": "live-run",
        "run_id": sidecar.run_id,
        "config_name": sidecar.config_name,
    }


def _dict_or_empty(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _path_key(value: str, *, base: Path | None = None) -> str:
    path = Path(value).expanduser()
    if base is not None and not path.is_absolute():
        path = base / path
    try:
        return str(path.resolve(strict=False))
    except OSError:
        return str(path.absolute())


def _manifest_has_rotated_inode(manifest: Manifest, inode: int) -> bool:
    return any(pointer.inode == inode for pointer in manifest.rotated)


def _extra_args_with_model_handoff(
    extra_args: list[str], handoff: ModelHandoff
) -> list[str]:
    resolved = list(extra_args)
    if handoff.tokenizer and not _extra_args_include_tokenizer(resolved):
        resolved.extend(["--tokenizer", handoff.tokenizer])
    return resolved


def _validate_model_handoff_prelaunch(cfg: ModelConfig, handoff: ModelHandoff) -> None:
    if handoff.source != "hf_repo":
        return
    if handoff.commit_sha is None:
        raise TargetCallError(
            "model-unavailable",
            (
                f"model {handoff.display_name} is missing an immutable Hugging Face "
                "commit sha; re-pin the model before launch"
            ),
            {
                "model_ref": handoff.entry_id,
                "repo_id": handoff.repo_id,
                "revision": handoff.revision,
                "reason": "missing-commit",
            },
        )
    if (handoff.cache_state or "").lower() == "remote_only" and _cfg_env_truthy(
        cfg, "HF_HUB_OFFLINE"
    ):
        raise TargetCallError(
            "model-unavailable",
            (
                f"model {handoff.display_name} is remote-only, "
                "but offline mode is enabled via HF_HUB_OFFLINE"
            ),
            {
                "model_ref": handoff.entry_id,
                "repo_id": handoff.repo_id,
                "cache_state": handoff.cache_state,
                "reason": "offline-remote-only",
            },
        )
    if handoff.token_required and not _cfg_env_value(cfg, "HF_TOKEN"):
        raise TargetCallError(
            "hf-auth-required",
            (
                f"model {handoff.display_name} requires HF_TOKEN; "
                "accept the model license and set HF_TOKEN "
                "(agent env or config env: block)"
            ),
            {
                "model_ref": handoff.entry_id,
                "repo_id": handoff.repo_id,
                "reason": "missing-hf-token",
            },
        )


def _model_not_cached_descriptor(
    cfg: ModelConfig, handoff: ModelHandoff | None
) -> dict[str, Any] | None:
    """Describe an uncached-model launch condition (H2), or None when cache is fine.

    Returns a structured descriptor with a ``gateable`` flag: a pinned hf_repo whose
    cache_state is not ``cached`` is gateable (``require_cached_models`` can upgrade the
    warning to a preflight failure); a bare unpinned model is never gateable (the
    registry cannot answer, and an unpinned model is a deliberate escape hatch).
    Detail text carries only model identity/size — never an agent-local path (bug-225).
    """
    if handoff is not None and handoff.source == "hf_repo":
        state = (handoff.cache_state or "").lower()
        if state == "cached":
            return None
        detail = (
            f"model {handoff.display_name} ({handoff.entry_id}) is not cached "
            f"(cache_state={state or 'unknown'}); vLLM will download it during "
            "startup, which can silently consume the ready timeout"
        )
        descriptor: dict[str, Any] = {
            "kind": ErrorKind.MODEL_NOT_CACHED.value,
            "entry_id": handoff.entry_id,
            "cache_state": state or None,
            "gateable": True,
        }
        if handoff.size_bytes:
            descriptor["size_bytes"] = handoff.size_bytes
            detail = f"{detail} (~{_format_handoff_size(handoff.size_bytes)})"
        descriptor["detail"] = detail
        return descriptor
    if (
        handoff is None
        and cfg.launch.require_cached_models
        and not is_local_model_reference(cfg.model)
    ):
        detail = (
            f"model {cfg.model} is not pinned to a registry entry, so its cache "
            "state cannot be verified; require_cached_models does not gate an "
            "unpinned model"
        )
        return {
            "kind": ErrorKind.MODEL_NOT_CACHED.value,
            "entry_id": None,
            "unpinned": True,
            "gateable": False,
            "detail": detail,
        }
    return None


def _docker_missing_hf_cache_mount_descriptor(
    cfg: ModelConfig, handoff: ModelHandoff | None
) -> dict[str, Any] | None:
    """Warn when a docker + hf_repo launch has no HF cache mount (H3).

    Registry downloads land in the agent's HF cache. A container with no
    ``command.docker.hf_cache`` (and no volume already covering that cache)
    cannot see it, so vLLM re-downloads the model into the container on every
    fresh start and any pre-download was wasted. The composer sets the mount by
    default; this catches hand-written YAML. Warning only (never a failure). The
    detail names the model identity only — never an agent-local path (bug-225).
    """
    docker = cfg.command.docker
    if cfg.command.runtime is not RuntimeKind.DOCKER or docker is None:
        return None
    if not _launch_uses_hf_repo(cfg, handoff):
        return None
    if docker.hf_cache is not None:
        return None
    if _volume_covers_hf_cache(docker.volumes):
        return None
    detail = (
        f"docker deployment for Hugging Face model {cfg.model} has no HF cache "
        "mount (command.docker.hf_cache is unset and no volume covers it), so the "
        "container cannot see the target HF cache and vLLM will re-download the "
        "model into the container on every fresh start. Set "
        "command.docker.hf_cache to the target's HF cache (the composer sets it "
        "by default)."
    )
    return {"kind": "docker-no-hf-cache-mount", "detail": detail}


def _docker_launch_warnings(
    cfg: ModelConfig, handoff: ModelHandoff | None
) -> list[dict[str, Any]]:
    """Non-blocking docker HF-cache launch warnings, shared by prepare + preflight."""
    warnings: list[dict[str, Any]] = []
    mount = _docker_missing_hf_cache_mount_descriptor(cfg, handoff)
    if mount is not None:
        warnings.append(mount)
    env_mismatch = _docker_hf_cache_env_mismatch_descriptor(cfg, handoff)
    if env_mismatch is not None:
        warnings.append(env_mismatch)
    return warnings


def _docker_hf_cache_env_mismatch_descriptor(
    cfg: ModelConfig, handoff: ModelHandoff | None
) -> dict[str, Any] | None:
    """Warn when the agent HF_HUB_CACHE is relocated outside HF_HOME (H4 follow-up).

    Registry downloads land in HF_HUB_CACHE, but the default docker mount is
    HF_HOME. When HF_HUB_CACHE sits outside ``HF_HOME/hub``, a deployment relying
    on the default HF_HOME mount (``hf_cache`` unset or set to HF_HOME) cannot see
    the agent's downloads. Warning only; names no agent-local path (bug-225).
    """
    docker = cfg.command.docker
    if cfg.command.runtime is not RuntimeKind.DOCKER or docker is None:
        return None
    if not _launch_uses_hf_repo(cfg, handoff):
        return None
    if _realpath(default_hf_hub_cache_dir()) == _realpath(default_hf_home_dir() / "hub"):
        return None
    # An explicit volume that mounts the hub cache directly already covers the
    # agent downloads, so it is not a mismatch (5.4 routed follow-up).
    if _volume_covers_hf_hub_cache(docker.volumes):
        return None
    if docker.hf_cache is not None and not _path_is_hf_home(docker.hf_cache):
        return None
    detail = (
        "agent HF_HUB_CACHE is outside HF_HOME; the default mount will not contain "
        "agent downloads — set command.docker.hf_cache explicitly"
    )
    return {"kind": "docker-hf-cache-env-mismatch", "detail": detail}


def _launch_uses_hf_repo(cfg: ModelConfig, handoff: ModelHandoff | None) -> bool:
    if handoff is not None:
        return handoff.source == "hf_repo"
    return not is_local_model_reference(cfg.model) and "://" not in cfg.model


def _realpath(path: Path | str) -> str:
    return os.path.realpath(Path(path).expanduser())


def _path_is_hf_home(value: str) -> bool:
    return _realpath(value) == _realpath(default_hf_home_dir())


def _volume_covers_hf_cache(volumes: list[str]) -> bool:
    # A source that resolves to either HF_HOME or the hub cache directly covers
    # the agent HF cache; realpath comparison tolerates symlinked cache trees.
    covering = {_realpath(default_hf_home_dir()), _realpath(default_hf_hub_cache_dir())}
    for volume in volumes:
        source = volume.split(":", 1)[0].strip()
        if source and _realpath(source) in covering:
            return True
    return False


def _volume_covers_hf_hub_cache(volumes: list[str]) -> bool:
    # Specifically the hub cache (where downloads land): a volume mounting it
    # means a relocated HF_HUB_CACHE is still visible to the container, so the
    # env-mismatch warning must not fire.
    hub = _realpath(default_hf_hub_cache_dir())
    for volume in volumes:
        source = volume.split(":", 1)[0].strip()
        if source and _realpath(source) == hub:
            return True
    return False


def _model_not_cached_wire(descriptor: dict[str, Any]) -> dict[str, Any]:
    wire = {"kind": descriptor["kind"], "detail": descriptor["detail"]}
    for key in ("entry_id", "cache_state", "size_bytes", "unpinned"):
        if key in descriptor and descriptor[key] is not None:
            wire[key] = descriptor[key]
    return wire


def _format_handoff_size(size_bytes: int) -> str:
    amount = float(size_bytes)
    for unit in ("B", "KB", "MB"):
        if amount < 1000:
            return f"{amount:.0f} {unit}"
        amount /= 1000
    return f"{amount:.1f} GB"


def _validate_model_ref_repo(cfg: ModelConfig, handoff: ModelHandoff) -> None:
    if handoff.source != "hf_repo" or handoff.repo_id is None:
        return
    if cfg.model == handoff.repo_id:
        return
    raise TargetCallError(
        "invalid-config",
        (
            f"model_ref {handoff.entry_id} resolves to {handoff.repo_id}, "
            f"but config model is {cfg.model}"
        ),
        {
            "model": cfg.model,
            "model_ref": handoff.entry_id,
            "repo_id": handoff.repo_id,
            "reason": "model-ref-repo-mismatch",
        },
    )


def _extra_args_include_tokenizer(extra_args: list[str]) -> bool:
    return any(arg == "--tokenizer" or arg.startswith("--tokenizer=") for arg in extra_args)


def _cfg_env_value(cfg: ModelConfig, key: str) -> str | None:
    if key in cfg.env:
        value = cfg.env.get(key)
    else:
        value = os.environ.get(key)
    return _optional_param_str(value)


def _cfg_env_truthy(cfg: ModelConfig, key: str) -> bool:
    value = _cfg_env_value(cfg, key)
    return value is not None and value.lower() in {"1", "true", "yes", "on"}


def _optional_param_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _env_int(name: str, default: int, *, minimum: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return max(minimum, parsed)


def _env_float(name: str, default: float, *, minimum: float) -> float:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError:
        return default
    return max(minimum, parsed)


def _param_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _params_runtime_is_docker(params: dict[str, Any]) -> bool:
    runtime = params.get("runtime")
    if isinstance(runtime, dict):
        return str(runtime.get("kind", "")).strip().lower() == "docker"
    return str(runtime or "").strip().lower() == "docker"


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
    env_version = os.environ.get("NVIDIA_DRIVER_VERSION")
    if env_version and env_version.strip():
        return env_version.strip()
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=driver_version",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        version = line.strip()
        if version:
            return version.split(",", maxsplit=1)[0].strip() or None
    return None


def _run_id_param(params: dict[str, Any]) -> str:
    value = params.get("run_id")
    if not isinstance(value, str) or not value.strip():
        raise TargetCallError("invalid-params", "run_id is required")
    return value


def _config_name_param(params: dict[str, Any], *, method: str) -> str:
    value = params.get("name", params.get("config_name"))
    if not isinstance(value, str) or not value.strip():
        raise TargetCallError("invalid-params", f"{method} requires config name")
    return value.strip()


def _required_param_name(params: dict[str, Any], key: str, *, method: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value.strip():
        raise TargetCallError("invalid-params", f"{method} requires {key}")
    return value.strip()


def _mapping_param(value: object, *, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TargetCallError("invalid-params", f"{field_name} must be a mapping")
    return dict(value)


def _prepare_clone_payload(
    payload: dict[str, Any],
    source: ModelConfig,
    new_name: str,
    overrides: dict[str, Any],
    configs_dir: Path,
    *,
    occupied_ports: dict[str, list[int]] | None = None,
    occupied_container_names: list[str] | None = None,
) -> list[dict[str, str]]:
    derived: list[dict[str, str]] = []
    if not _override_has(overrides, "server", "port"):
        allocation = allocate_port(configs_dir=configs_dir, occupied_ports=occupied_ports)
        server = dict(payload.get("server") if isinstance(payload.get("server"), dict) else {})
        server["port"] = allocation["port"]
        payload["server"] = server
        derived.append(
            {"field": "server.port", "value": str(allocation["port"]), "source": "allocate_port"}
        )
    if not _override_has(overrides, "launch", "runs_dir"):
        launch = dict(payload.get("launch") if isinstance(payload.get("launch"), dict) else {})
        runs_dir = _clone_runs_dir(source, new_name)
        launch["runs_dir"] = str(runs_dir)
        payload["launch"] = launch
        derived.append({"field": "launch.runs_dir", "value": str(runs_dir), "source": "clone"})
    if source.command.runtime is RuntimeKind.DOCKER and not _override_has(
        overrides, "command", "docker", "container_name"
    ):
        command = dict(payload.get("command") if isinstance(payload.get("command"), dict) else {})
        docker = dict(command.get("docker") if isinstance(command.get("docker"), dict) else {})
        preferred_container_name = f"vela-{_safe_config_file_stem(new_name)}"
        container_name = _fresh_docker_container_name(
            preferred_container_name,
            occupied_container_names,
        )
        docker["container_name"] = container_name
        command["docker"] = docker
        payload["command"] = command
        source_name = (
            "docker_container_name_collision"
            if container_name != preferred_container_name
            else "clone"
        )
        derived.append(
            {
                "field": "command.docker.container_name",
                "value": container_name,
                "source": source_name,
            }
        )
    return derived


def _override_has(overrides: dict[str, Any], *path: str) -> bool:
    current: Any = overrides
    for part in path:
        if not isinstance(current, dict) or part not in current:
            return False
        current = current[part]
    return True


def _clone_runs_dir(source: ModelConfig, new_name: str) -> Path:
    if source.launch.runs_dir is not None:
        return source.launch.runs_dir.parent / _safe_config_file_stem(new_name)
    return default_run_artifacts_dir() / _safe_config_file_stem(new_name)


BLACKBIRD_WRAPPER_RECIPE_BY_SCRIPT = {
    "blackbird_qwen36_vllm_foreground.sh": "blackbird-qwen36-27b-fp8-rp6000",
    "blackbird_qwen36_bf16_vllm_foreground.sh": "blackbird-qwen36-27b-bf16-rp6000",
}


def _native_config_from_known_wrapper(
    source: ModelConfig,
    new_name: str,
) -> tuple[ModelConfig, list[dict[str, str]]]:
    if source.command.runtime is RuntimeKind.DOCKER:
        raise TargetCallError(
            "invalid-config",
            f"config already uses command.runtime: docker: {source.name}",
            {"name": source.name},
        )
    executable = _optional_str(source.command.executable)
    if executable is None:
        raise TargetCallError(
            "invalid-config",
            "wrapper migration requires command.executable",
            {"name": source.name},
        )
    recipe_key = _wrapper_recipe_key_for_source(source, executable)
    recipe = _deployment_recipe_payload(recipe_key)
    target = _optional_str(source.target) or str(recipe["target"])
    if target.lower() != str(recipe["target"]).lower():
        raise TargetCallError(
            "invalid-config",
            "wrapper migration only supports the known Blackbird wrapper target",
            {"name": source.name, "target": source.target, "recipe_target": recipe["target"]},
        )
    expected_model = str(recipe["model"])
    if source.model != expected_model:
        raise TargetCallError(
            "invalid-config",
            "wrapper config model does not match the known Blackbird recipe",
            {"name": source.name, "model": source.model, "recipe_model": expected_model},
        )
    payload: dict[str, Any] = {
        "name": new_name,
        "target": recipe["target"],
        "description": (
            f"Migrated by Vela from wrapper config {source.name}; review before launch."
        ),
        "model": expected_model,
        "served_model_name": recipe["served_model_name"],
        "command": {
            "entrypoint": "serve",
            "runtime": "docker",
            "docker": dict(recipe["docker"]),
        },
        "engine": dict(recipe["engine"]),
        "server": dict(recipe["server"]),
        "logging": {
            "request_logging": False,
            "suppress_access_log_for": ["/health"],
        },
        "extra_args": list(recipe["extra_args"]),
        "launch": dict(recipe["launch"]),
        "vllm": dict(recipe.get("vllm") or {}),
    }
    if source.revision is not None:
        payload["revision"] = source.revision
    if source.model_ref is not None:
        payload["model_ref"] = source.model_ref
    cfg = ModelConfig.model_validate(payload)
    return cfg, [
        {
            "field": "deployment.recipe",
            "value": recipe_key,
            "source": "known_wrapper",
        },
        {
            "field": "command.runtime",
            "value": "docker",
            "source": "wrapper_migration",
        },
        {
            "field": "command.docker",
            "value": str(recipe["docker"].get("container_name") or ""),
            "source": Path(executable).name,
        },
    ]


def _wrapper_recipe_key_for_source(source: ModelConfig, executable: str) -> str:
    script_name = Path(executable).name
    recipe_key = BLACKBIRD_WRAPPER_RECIPE_BY_SCRIPT.get(script_name)
    if recipe_key is not None:
        return recipe_key
    raise TargetCallError(
        "invalid-config",
        "unsupported wrapper script; migration is limited to known Blackbird recipes",
        {
            "name": source.name,
            "executable": executable,
            "supported_scripts": sorted(BLACKBIRD_WRAPPER_RECIPE_BY_SCRIPT),
        },
    )


def _deployment_recipe_payload(key: str) -> dict[str, Any]:
    for recipe in list_deployment_recipes(None):
        if recipe.get("key") == key:
            return dict(recipe)
    raise TargetCallError(
        "invalid-config",
        f"unknown deployment recipe: {key}",
        {"recipe": key},
    )


def _apply_config_overrides(payload: dict[str, Any], overrides: dict[str, Any]) -> None:
    for section in ("engine", "server", "launch", "env"):
        section_overrides = overrides.get(section)
        if section_overrides is None:
            continue
        if not isinstance(section_overrides, dict):
            raise TargetCallError("invalid-params", f"overrides.{section} must be a mapping")
        current = dict(payload.get(section) if isinstance(payload.get(section), dict) else {})
        current.update(section_overrides)
        payload[section] = current
    if "extra_args" in overrides:
        extra_args = overrides["extra_args"]
        if not isinstance(extra_args, list) or not all(
            isinstance(item, str) for item in extra_args
        ):
            raise TargetCallError(
                "invalid-params",
                "overrides.extra_args must be a list of strings",
            )
        payload["extra_args"] = [*list(payload.get("extra_args") or []), *extra_args]


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
    if cfg is not None:
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


def _valid_config_item_by_name(
    registry: ConfigRegistry, name: str, *, configs_dir: str | Path | None = None
) -> ValidConfig:
    for item in registry.valid:
        if item.config.name == name:
            return item
    try:
        registry.by_name(name)
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
        raise _unknown_config_error(name, registry, configs_dir) from exc
    raise _unknown_config_error(name, registry, configs_dir)


def _unknown_config_error(
    name: str, registry: ConfigRegistry, configs_dir: str | Path | None
) -> TargetCallError:
    # PLAN-MANDATED bug-225 exception (bug-238): searched_dirs + the agent cwd are
    # intentional diagnostic surface in the unknown-config ERROR payload — the
    # daemon keeps its first working directory, so naming what it searched (and from
    # where) is the difference between "config not found" and a debuggable message.
    searched = discover_config_dirs(configs_dir)
    return TargetCallError(
        "unknown-config",
        f"unknown config: {name}",
        {
            "name": name,
            "available": [item.config.name for item in registry.valid],
            "searched_dirs": [_home_relative(str(path)) for path in searched],
            "cwd": os.getcwd(),
        },
    )


def _home_relative(path: str) -> str:
    """Collapse a home-prefixed path to ``~/…`` for display (best-effort)."""
    try:
        home = str(Path.home())
    except (OSError, RuntimeError):
        return path
    if path == home:
        return "~"
    prefix = home.rstrip("/") + "/"
    if path.startswith(prefix):
        return "~/" + path[len(prefix) :]
    return path


def _config_by_name(
    registry: ConfigRegistry, name: str, *, configs_dir: str | Path | None = None
) -> ModelConfig:
    return _valid_config_item_by_name(registry, name, configs_dir=configs_dir).config


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
    sidecar_payload: dict[str, Any] = {
        "config_name": sidecar.config_name,
        "host": sidecar.host,
        "port": sidecar.port,
        "exposure": sidecar.exposure,
        "served_model_names": list(sidecar.served_model_names),
        "launch_mode": sidecar.launch_mode,
        "vllm_version_profile": sidecar.vllm_version_profile,
        "reachable_url": _reachable_url(run.config),
    }
    for key in (
        "build_id",
        "build_label",
        "model_ref",
        "model_entry_id",
        "model_repo_id",
        "model_revision",
        "model_commit_sha",
    ):
        value = getattr(sidecar, key)
        if value is not None:
            sidecar_payload[key] = value
    return {
        "run_id": run.run_id,
        "config": run.config.model_dump(mode="json"),
        "sidecar": sidecar_payload,
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
