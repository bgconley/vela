from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from vela.config.loader import load_registry
from vela.config.schema import (
    Exposure,
    ModelConfig,
    RuntimeKind,
    default_run_artifacts_dir,
    model_basename,
)
from vela.engine.command_builder import build_command
from vela.engine.model_registry import ModelRegistryError, inspect_model
from vela.engine.profile import VllmProfileError, select_profile_for_config

DEFAULT_PORT_RANGE = (18000, 18999)
BLACKBIRD_QWEN36_IMAGE = (
    "vllm/vllm-openai@sha256:"
    "b13d6e5fda0785f3d41752df8513ff832f67cb231a216c76b6b4f2a515bf0046"
)


@dataclass(frozen=True)
class ModelContext:
    model: str
    model_ref: str | None = None
    revision: str | None = None
    display_name: str | None = None
    entry: dict[str, Any] | None = None


@dataclass(frozen=True)
class DeploymentRecipe:
    key: str
    label: str
    target: str
    runtime: RuntimeKind
    models: tuple[str, ...]
    served_model_name: str
    server: dict[str, Any]
    engine: dict[str, Any]
    extra_args: tuple[str, ...]
    launch: dict[str, Any]
    docker: dict[str, Any]
    vllm: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class EngineSuggestions:
    engine: dict[str, Any] = field(default_factory=dict)
    field_sources: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)


QWEN36_FP8_EXTRA_ARGS = (
    "--kv-cache-memory-bytes",
    "64424509440",
    "--max-num-batched-tokens",
    "8192",
    "--max-num-partial-prefills",
    "1",
    "--max-long-partial-prefills",
    "1",
    "--attention-backend",
    "FLASHINFER",
    "--trust-remote-code",
    "--language-model-only",
    "--enable-chunked-prefill",
    "--enable-prefix-caching",
    "--enable-auto-tool-choice",
    "--reasoning-parser",
    "qwen3",
    "--tool-call-parser",
    "qwen3_coder",
    "--limit-mm-per-prompt",
    '{"image":0,"video":0}',
    "--compilation-config",
    '{"cudagraph_capture_sizes":[1,2,4,8,16],"cudagraph_num_of_warmups":1}',
    "--cudagraph-metrics",
    "--disable-uvicorn-access-log",
)

QWEN36_BF16_EXTRA_ARGS = (
    "--max-num-batched-tokens",
    "8192",
    "--trust-remote-code",
    "--language-model-only",
    "--enable-prefix-caching",
    "--enable-auto-tool-choice",
    "--reasoning-parser",
    "qwen3",
    "--tool-call-parser",
    "qwen3_coder",
)

BLACKBIRD_QWEN36_EVICT = (
    "vela-qwen36-27b-fp8-kvfp8-rp6000-blackbird",
    "vela-qwen36-27b-bf16-rp6000-blackbird",
    "qwen36-27b-fp8-kvbf16-rp6000-server",
    "qwen36-27b-fp8-kvfp8-rp6000-server",
    "qwen36-27b-fp8-kvfp8-rp6000-vela",
    "qwen36-27b-fp8-rp6000-server",
    "qwen36-27b-bf16-rp6000-server",
    "qwen3-coder-next-nvfp4-server",
    "qwen3-coder-next-fp8-server",
    "qwen36-dual-27b-fp8-vlm",
    "qwen36-dual-35b-fp8-vlm",
)

LAB_RECIPES: tuple[DeploymentRecipe, ...] = (
    DeploymentRecipe(
        key="blackbird-qwen36-27b-fp8-rp6000",
        label="Blackbird Qwen3.6 27B FP8 RP6000",
        target="blackbird",
        runtime=RuntimeKind.DOCKER,
        models=("Qwen/Qwen3.6-27B-FP8",),
        served_model_name="qwen36-27b-fp8-kvfp8-rp6000",
        server={"host": "0.0.0.0", "port": 18003, "exposure": "lan", "api_key": "EMPTY"},
        engine={
            "gpu_memory_utilization": 0.97,
            "max_model_len": 262144,
            "dtype": "auto",
            "kv_cache_dtype": "fp8",
            "max_num_seqs": 16,
        },
        extra_args=QWEN36_FP8_EXTRA_ARGS,
        launch={
            "mode": "attached",
            "ready_timeout_seconds": 1800,
            "health": {"interval_seconds": 2},
            "runs_dir": "/home/bgconley/models/qwen36-27b-fp8-rp6000/vela-runs",
        },
        docker={
            "image": BLACKBIRD_QWEN36_IMAGE,
            "gpus": "all",
            "ipc_host": True,
            "shm_size": "32g",
            "network": "host",
            "hf_cache": "/home/bgconley/models/qwen36-dual-fp8-vlm/hf-cache",
            "hf_cache_target": "/root/.cache/huggingface",
            "env": {
                "HF_HOME": "/root/.cache/huggingface",
                "HF_HUB_CACHE": "/root/.cache/huggingface/hub",
                "VLLM_CACHE_ROOT": "/root/.cache/vllm",
                "TRITON_CACHE_DIR": "/root/.cache/triton",
                "TORCHINDUCTOR_CACHE_DIR": "/root/.cache/torch",
                "FLASHINFER_CUDA_ARCH_LIST": "12.0f",
                "FLASHINFER_LOGLEVEL": "0",
                "FLASHINFER_JIT_VERBOSE": "0",
                "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True",
                "SAFETENSORS_FAST_GPU": "1",
            },
            "volumes": [
                "/home/bgconley/models/qwen36-27b-fp8-rp6000/vllm-cache:/root/.cache/vllm",
                "/home/bgconley/models/qwen36-27b-fp8-rp6000/triton-cache:/root/.cache/triton",
                "/home/bgconley/models/qwen36-27b-fp8-rp6000/torch-compile-cache:/root/.cache/torch",
                "/home/bgconley/models/qwen36-27b-fp8-rp6000/flashinfer-cache:/root/.cache/flashinfer",
                "/home/bgconley/models/qwen36-27b-fp8-rp6000/tmp:/tmp/qwen36-27b-fp8-rp6000",
            ],
            "evict": list(BLACKBIRD_QWEN36_EVICT),
            "extra_run_args": ["--ulimit", "memlock=-1", "--ulimit", "stack=67108864"],
        },
        vllm={"version_profile": "0.11"},
    ),
    DeploymentRecipe(
        key="blackbird-qwen36-27b-bf16-rp6000",
        label="Blackbird Qwen3.6 27B BF16 RP6000",
        target="blackbird",
        runtime=RuntimeKind.DOCKER,
        models=("Qwen/Qwen3.6-27B",),
        served_model_name="qwen36-27b-bf16-rp6000",
        server={"host": "0.0.0.0", "port": 18002, "exposure": "lan", "api_key": "EMPTY"},
        engine={
            "gpu_memory_utilization": 0.95,
            "max_model_len": 262144,
            "dtype": "bfloat16",
            "kv_cache_dtype": "bfloat16",
            "max_num_seqs": 4,
        },
        extra_args=QWEN36_BF16_EXTRA_ARGS,
        launch={
            "mode": "attached",
            "ready_timeout_seconds": 1800,
            "health": {"interval_seconds": 2},
            "runs_dir": "/home/bgconley/models/qwen36-27b-bf16/vela-runs",
        },
        docker={
            "image": BLACKBIRD_QWEN36_IMAGE,
            "gpus": "all",
            "ipc_host": True,
            "shm_size": "32g",
            "network": "host",
            "env": {
                "HF_HOME": "/home/bgconley/models/qwen36-27b-bf16/hf-cache",
                "HF_HUB_CACHE": "/home/bgconley/models/qwen36-27b-bf16/hf-cache/hub",
                "VLLM_CACHE_ROOT": "/home/bgconley/models/qwen36-27b-bf16/vllm-cache",
                "TRITON_CACHE_DIR": "/home/bgconley/models/qwen36-27b-bf16/triton-cache",
                "TORCHINDUCTOR_CACHE_DIR": (
                    "/home/bgconley/models/qwen36-27b-bf16/torch-compile-cache"
                ),
                "TMPDIR": "/home/bgconley/models/qwen36-27b-bf16/tmp",
                "CUDA_VISIBLE_DEVICES": "0",
                "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True",
                "SAFETENSORS_FAST_GPU": "1",
            },
            "volumes": [
                "/home/bgconley/models/qwen36-27b-bf16:/home/bgconley/models/qwen36-27b-bf16",
            ],
            "evict": list(BLACKBIRD_QWEN36_EVICT),
            "extra_run_args": ["--ulimit", "memlock=-1", "--ulimit", "stack=67108864"],
        },
        vllm={"version_profile": "0.11"},
    ),
)


@dataclass(frozen=True)
class ComposeResult:
    config: ModelConfig
    warnings: list[str] = field(default_factory=list)
    derived: list[dict[str, str]] = field(default_factory=list)


PRESETS: tuple[dict[str, Any], ...] = (
    {
        "name": "balanced",
        "description": "Default safe starting point for general serving.",
        "engine": {"gpu_memory_utilization": 0.9, "dtype": "auto"},
        "extra_args": ["--enable-prefix-caching"],
        "applies_to": ["all"],
    },
    {
        "name": "throughput",
        "description": "Favor higher batch volume when memory headroom exists.",
        "engine": {"gpu_memory_utilization": 0.92, "dtype": "auto", "max_num_seqs": 32},
        "extra_args": ["--enable-prefix-caching"],
        "applies_to": ["all"],
    },
    {
        "name": "long-context",
        "description": "Reserve memory for longer prompts and conservative concurrency.",
        "engine": {"gpu_memory_utilization": 0.9, "dtype": "auto", "max_num_seqs": 4},
        "extra_args": ["--enable-prefix-caching"],
        "applies_to": ["all"],
    },
    {
        "name": "low-memory",
        "description": "Conservative memory profile for tight cards or experiments.",
        "engine": {"gpu_memory_utilization": 0.85, "dtype": "auto", "max_num_seqs": 2},
        "extra_args": ["--enable-prefix-caching"],
        "applies_to": ["all"],
    },
    {
        "name": "qwen3-text",
        "description": "Qwen3 text serving with reasoning/tool parsers enabled.",
        "engine": {"gpu_memory_utilization": 0.9, "dtype": "auto"},
        "extra_args": [
            "--enable-prefix-caching",
            "--language-model-only",
            "--reasoning-parser",
            "qwen3",
            "--tool-call-parser",
            "qwen3_coder",
        ],
        "applies_to": ["qwen", "qwen3"],
    },
)


def list_presets() -> list[dict[str, Any]]:
    return [
        {
            "name": str(preset["name"]),
            "description": str(preset["description"]),
            "engine": dict(preset.get("engine") or {}),
            "extra_args": list(preset.get("extra_args") or []),
            "applies_to": list(preset.get("applies_to") or []),
        }
        for preset in PRESETS
    ]


def list_deployment_recipes(target: str | None = None) -> list[dict[str, Any]]:
    target_key = target.lower() if isinstance(target, str) and target.strip() else None
    recipes: list[dict[str, Any]] = []
    for recipe in LAB_RECIPES:
        if target_key is not None and recipe.target.lower() != target_key:
            continue
        model = recipe.models[0] if recipe.models else ""
        name = f"{recipe.served_model_name}-{recipe.target}"
        docker = dict(recipe.docker)
        recipes.append(
            {
                "key": recipe.key,
                "label": recipe.label,
                "name": name,
                "target": recipe.target,
                "runtime": recipe.runtime.value,
                "model": model,
                "models": list(recipe.models),
                "served_model_name": recipe.served_model_name,
                "image": docker.get("image", ""),
                "server": dict(recipe.server),
                "engine": dict(recipe.engine),
                "extra_args": list(recipe.extra_args),
                "launch": dict(recipe.launch),
                "docker": docker,
                "vllm": dict(recipe.vllm),
                "warnings": list(recipe.warnings),
            }
        )
    return recipes


def compose_config(
    spec: dict[str, Any],
    *,
    configs_dir: str | Path | None = None,
    models_registry_path: str | Path | None = None,
    occupied_ports: Mapping[str, Iterable[int]] | None = None,
) -> ComposeResult:
    model_context = _model_context(spec, models_registry_path=models_registry_path)
    model = model_context.model
    name = _deployment_name(spec.get("name"), model)
    target = _optional_str(spec.get("target"))
    runtime = _runtime_kind(spec.get("runtime"))
    overrides = _mapping(spec.get("overrides"), field_name="overrides")
    preset_name = _optional_str(spec.get("preset")) or "balanced"
    preset = _preset_by_name(preset_name)
    recipe = _matching_recipe(target=target, runtime=runtime, model=model)
    suggestions = _engine_suggestions(model_context)
    port = allocate_port(
        preferred=_preferred_port(overrides) or _recipe_port(recipe),
        configs_dir=configs_dir,
        occupied_ports=occupied_ports,
    )
    engine, engine_sources = _seed_engine(preset, recipe, suggestions)
    server = {
        "host": "127.0.0.1",
        "port": port["port"],
        "exposure": Exposure.LOCAL.value,
    }
    if recipe is not None:
        server.update(recipe.server)
        server["port"] = port["port"]
    launch = {"runs_dir": str(default_run_artifacts_dir() / name)}
    if recipe is not None:
        launch.update(recipe.launch)

    payload: dict[str, Any] = {
        "name": name,
        "target": target,
        "model": model,
        "served_model_name": _served_model_name(model_context, recipe),
        "description": f"Generated by Vela deployment composer using preset {preset_name}.",
        "command": _runtime_command(runtime, spec, name, recipe),
        "engine": engine,
        "server": server,
        "extra_args": _seed_extra_args(preset, recipe),
        "launch": launch,
    }
    if model_context.model_ref:
        payload["model_ref"] = model_context.model_ref
    if model_context.revision:
        payload["revision"] = model_context.revision
    if recipe is not None and recipe.vllm:
        payload["vllm"] = dict(recipe.vllm)
    if target is None:
        payload.pop("target")

    _merge_overrides(payload, overrides)
    _merge_extra_args(payload, overrides)
    cfg = ModelConfig.model_validate(payload)
    warnings = [*suggestions.warnings, *(recipe.warnings if recipe is not None else ())]
    warnings.extend(port["warnings"])
    return ComposeResult(
        config=cfg,
        warnings=warnings,
        derived=[
            *_recipe_derived(recipe),
            {
                "field": "served_model_name",
                "value": cfg.served_model_name or "",
                "source": _served_model_source(model_context, recipe),
            },
            {"field": "server.port", "value": str(cfg.server.port), "source": "allocate_port"},
            {"field": "launch.runs_dir", "value": str(cfg.launch.runs_dir), "source": "runs_root"},
            *_engine_derived(cfg, engine_sources, overrides),
            *_docker_derived(cfg),
        ],
    )


def allocate_port(
    *,
    preferred: int | None = None,
    configs_dir: str | Path | None = None,
    port_range: tuple[int, int] = DEFAULT_PORT_RANGE,
    occupied_ports: Mapping[str, Iterable[int]] | None = None,
) -> dict[str, Any]:
    configured_ports = _configured_ports(configs_dir)
    occupied = _occupied_ports_by_source(occupied_ports)
    used = set(configured_ports)
    for ports in occupied.values():
        used.update(ports)
    start, end = port_range
    scanned: dict[str, Any] = {"configured_ports": sorted(configured_ports), "range": [start, end]}
    scanned.update({source: sorted(ports) for source, ports in occupied.items()})
    if preferred is not None and preferred not in used:
        return {"port": preferred, "scanned": scanned, "warnings": []}
    for port in range(start, end + 1):
        if port not in used:
            warnings = ["port-reassigned"] if preferred is not None else []
            return {"port": port, "scanned": scanned, "warnings": warnings}
    raise ValueError(f"no free port in range {start}-{end}")


def suggest_deployment_defaults(
    params: dict[str, Any],
    *,
    configs_dir: str | Path | None = None,
    models_registry_path: str | Path | None = None,
    occupied_ports: Mapping[str, Iterable[int]] | None = None,
) -> dict[str, Any]:
    model_context = _model_context(params, models_registry_path=models_registry_path)
    model = model_context.model
    name = _deployment_name(params.get("name"), model)
    target = _optional_str(params.get("target"))
    runtime = _runtime_kind(params.get("runtime"))
    recipe = _matching_recipe(target=target, runtime=runtime, model=model)
    suggestions = _engine_suggestions(model_context)
    allocation = allocate_port(
        preferred=_optional_int(params.get("preferred_port")) or _recipe_port(recipe),
        configs_dir=configs_dir,
        occupied_ports=occupied_ports,
    )
    engine_suggestions = _recipe_engine(recipe) if recipe is not None else dict(suggestions.engine)
    sources = ["configured_ports", "defaults"]
    if recipe is not None:
        sources.append(f"lab_recipe:{recipe.key}")
    sources.extend(suggestions.sources)
    payload = {
        "model": model,
        "served_model_name": _served_model_name(model_context, recipe),
        "port": allocation["port"],
        "runs_dir": _recipe_runs_dir(recipe) or str(default_run_artifacts_dir() / name),
        "exposure": _recipe_exposure(recipe) or Exposure.LOCAL.value,
        "engine_suggestions": engine_suggestions,
        "sources": _dedupe(sources),
        "warnings": [*suggestions.warnings, *allocation["warnings"]],
    }
    if model_context.model_ref:
        payload["model_ref"] = model_context.model_ref
    if recipe is not None:
        payload["recipe"] = {"key": recipe.key, "label": recipe.label}
    if runtime is RuntimeKind.DOCKER:
        payload["container_name"] = f"vela-{name}"
        if recipe is not None:
            payload["runtime_suggestions"] = {"kind": "docker", "image": recipe.docker["image"]}
    return payload


def validate_config_payload(payload: dict[str, Any]) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    warnings: list[str] = []
    try:
        cfg = ModelConfig.model_validate(payload)
    except ValidationError as exc:
        return {
            "ok": False,
            "errors": [
                {"field": ".".join(str(part) for part in error["loc"]), "message": error["msg"]}
                for error in exc.errors()
            ],
            "warnings": [],
        }
    except Exception as exc:
        return {"ok": False, "errors": [{"field": "config", "message": str(exc)}], "warnings": []}
    warnings.extend(_lint_config(cfg))
    try:
        result = build_command(cfg, select_profile_for_config(cfg))
    except VllmProfileError as exc:
        errors.append({"field": "vllm", "message": str(exc)})
    else:
        warnings.extend(result.warnings)
    return {"ok": not errors, "errors": errors, "warnings": warnings}


def _runtime_command(
    runtime: RuntimeKind,
    spec: dict[str, Any],
    name: str,
    recipe: DeploymentRecipe | None,
) -> dict[str, Any]:
    runtime_payload = spec.get("runtime")
    if runtime is RuntimeKind.PROCESS:
        if isinstance(runtime_payload, dict) and runtime_payload.get("kind") == "executable":
            executable = _required_str(runtime_payload, "executable")
            return {"runtime": "process", "executable": executable}
        build_ref = spec.get("build") or (
            runtime_payload.get("build") if isinstance(runtime_payload, dict) else None
        )
        if build_ref:
            return {"runtime": "process", "build": str(build_ref)}
        return {"runtime": "process"}
    image = _docker_image(spec, recipe)
    docker = dict(recipe.docker) if recipe is not None else {}
    docker["image"] = image
    docker.setdefault("container_name", f"vela-{name}")
    return {
        "runtime": "docker",
        "docker": docker,
    }


def _merge_overrides(payload: dict[str, Any], overrides: dict[str, Any]) -> None:
    for section in ("engine", "server", "launch", "env"):
        section_overrides = overrides.get(section)
        if section_overrides is None:
            continue
        if not isinstance(section_overrides, dict):
            raise ValueError(f"overrides.{section} must be a mapping")
        current = dict(payload.get(section) or {})
        current.update(section_overrides)
        payload[section] = current


def _merge_extra_args(payload: dict[str, Any], overrides: dict[str, Any]) -> None:
    if "extra_args" not in overrides:
        return
    extra_args = overrides["extra_args"]
    if not isinstance(extra_args, list) or not all(isinstance(item, str) for item in extra_args):
        raise ValueError("overrides.extra_args must be a list of strings")
    payload["extra_args"] = [*list(payload.get("extra_args") or []), *extra_args]


def _docker_derived(cfg: ModelConfig) -> list[dict[str, str]]:
    if cfg.command.runtime is not RuntimeKind.DOCKER or cfg.command.docker is None:
        return []
    return [
        {
            "field": "command.docker.container_name",
            "value": cfg.command.docker.container_name or "",
            "source": "deployment_name",
        }
    ]


def _recipe_derived(recipe: DeploymentRecipe | None) -> list[dict[str, str]]:
    if recipe is None:
        return []
    return [
        {
            "field": "deployment.recipe",
            "value": recipe.key,
            "source": "lab_recipe",
        }
    ]


def _engine_derived(
    cfg: ModelConfig, engine_sources: dict[str, str], overrides: dict[str, Any]
) -> list[dict[str, str]]:
    overridden: set[str] = set()
    engine_overrides = overrides.get("engine")
    if isinstance(engine_overrides, dict):
        overridden = {str(key) for key in engine_overrides}
    derived: list[dict[str, str]] = []
    for key, source in sorted(engine_sources.items()):
        if key in overridden:
            continue
        value = getattr(cfg.engine, key, None)
        if value is not None:
            derived.append({"field": f"engine.{key}", "value": str(value), "source": source})
    return derived


def _configured_ports(configs_dir: str | Path | None) -> set[int]:
    registry = load_registry(configs_dir)
    return {item.config.server.port for item in registry.valid}


def _occupied_ports_by_source(
    occupied_ports: Mapping[str, Iterable[int]] | None,
) -> dict[str, set[int]]:
    occupied: dict[str, set[int]] = {}
    for source, values in (occupied_ports or {}).items():
        source_key = str(source)
        ports: set[int] = set()
        for value in values:
            try:
                port = int(value)
            except (TypeError, ValueError):
                continue
            if port > 0:
                ports.add(port)
        if ports:
            occupied[source_key] = ports
    return occupied


def _model_context(
    spec: dict[str, Any], *, models_registry_path: str | Path | None
) -> ModelContext:
    model_ref = _optional_str(spec.get("model_ref"))
    model = _optional_str(spec.get("model"))
    if model_ref is None:
        if model is None:
            raise ValueError("model is required")
        return ModelContext(model=model)
    try:
        inspected = inspect_model(model_ref, models_registry_path)
    except ModelRegistryError as exc:
        raise ValueError(exc.message) from exc
    entry = inspected.get("entry") if isinstance(inspected, dict) else None
    if not isinstance(entry, dict):
        raise ValueError(f"model_ref {model_ref} did not resolve to a model entry")
    resolved_model = model or _model_arg_from_entry(entry, model_ref)
    revision = _optional_str(spec.get("revision")) or _optional_str(entry.get("commit_sha"))
    if revision is None:
        revision = _optional_str(entry.get("revision"))
    return ModelContext(
        model=resolved_model,
        model_ref=model_ref,
        revision=revision,
        display_name=_optional_str(entry.get("display_name")),
        entry=entry,
    )


def _model_arg_from_entry(entry: dict[str, Any], model_ref: str) -> str:
    source = _optional_str(entry.get("source"))
    if source == "hf_repo":
        repo_id = _optional_str(entry.get("repo_id"))
        if repo_id:
            return repo_id
    if source == "local_path":
        local_path = _optional_str(entry.get("local_path"))
        if local_path:
            return local_path
    if source == "url":
        url = _optional_str(entry.get("url"))
        if url:
            return url
    raise ValueError(f"model_ref {model_ref} does not have a launchable model source")


def _matching_recipe(
    *, target: str | None, runtime: RuntimeKind, model: str
) -> DeploymentRecipe | None:
    if target is None:
        return None
    target_key = target.lower()
    for recipe in LAB_RECIPES:
        if recipe.runtime is not runtime:
            continue
        if recipe.target.lower() != target_key:
            continue
        if model in recipe.models:
            return recipe
    return None


def _served_model_name(
    model_context: ModelContext, recipe: DeploymentRecipe | None
) -> str:
    if recipe is not None:
        return recipe.served_model_name
    return model_context.display_name or model_basename(model_context.model)


def _served_model_source(
    model_context: ModelContext, recipe: DeploymentRecipe | None
) -> str:
    if recipe is not None:
        return f"lab_recipe:{recipe.key}"
    if model_context.display_name:
        return "model_registry"
    return "model_basename"


def _recipe_engine(recipe: DeploymentRecipe | None) -> dict[str, Any]:
    return dict(recipe.engine) if recipe is not None else {}


def _recipe_port(recipe: DeploymentRecipe | None) -> int | None:
    if recipe is None:
        return None
    return _optional_int(recipe.server.get("port"))


def _recipe_runs_dir(recipe: DeploymentRecipe | None) -> str | None:
    if recipe is None:
        return None
    return _optional_str(recipe.launch.get("runs_dir"))


def _recipe_exposure(recipe: DeploymentRecipe | None) -> str | None:
    if recipe is None:
        return None
    return _optional_str(recipe.server.get("exposure"))


def _seed_engine(
    preset: dict[str, Any],
    recipe: DeploymentRecipe | None,
    suggestions: EngineSuggestions,
) -> tuple[dict[str, Any], dict[str, str]]:
    engine = dict(preset.get("engine") or {})
    sources: dict[str, str] = {}
    if recipe is not None:
        for key, value in recipe.engine.items():
            engine[key] = value
            sources[key] = f"lab_recipe:{recipe.key}"
        return engine, sources
    for key, value in suggestions.engine.items():
        if key not in engine:
            engine[key] = value
            sources[key] = suggestions.field_sources.get(key, "model_registry")
    return engine, sources


def _seed_extra_args(preset: dict[str, Any], recipe: DeploymentRecipe | None) -> list[str]:
    if recipe is not None:
        return list(recipe.extra_args)
    return list(preset.get("extra_args") or [])


def _engine_suggestions(model_context: ModelContext) -> EngineSuggestions:
    suggestions = EngineSuggestions()
    entry = model_context.entry
    if entry is not None:
        suggestions.sources.append("model_registry")
        quant_format = _optional_str(entry.get("quant_format"))
        _merge_quant_suggestions(
            suggestions,
            quant_format=quant_format,
            source="model_registry",
        )
        if (entry.get("gated") or entry.get("token_required")) and not os.environ.get("HF_TOKEN"):
            suggestions.warnings.append("gated-needs-token")
        repo_id = _optional_str(entry.get("repo_id"))
        if repo_id:
            hf_config = _load_hf_model_config(repo_id, model_context.revision)
            if hf_config:
                suggestions.sources.append("hf_config")
                _merge_hf_config_suggestions(suggestions, hf_config)
    if "tensor_parallel_size" not in suggestions.engine and suggestions.engine:
        suggestions.engine["tensor_parallel_size"] = 1
        suggestions.field_sources["tensor_parallel_size"] = (
            suggestions.sources[-1] if suggestions.sources else "model_registry"
        )
    return suggestions


def _merge_quant_suggestions(
    suggestions: EngineSuggestions, *, quant_format: str | None, source: str
) -> None:
    if not quant_format:
        return
    quant = quant_format.lower()
    if "fp8" in quant:
        suggestions.engine.setdefault("dtype", "auto")
        suggestions.engine.setdefault("kv_cache_dtype", "fp8")
        suggestions.field_sources.setdefault("dtype", source)
        suggestions.field_sources.setdefault("kv_cache_dtype", source)
    elif quant in {"awq", "gptq", "marlin"}:
        suggestions.engine.setdefault("quantization", quant)
        suggestions.field_sources.setdefault("quantization", source)


def _merge_hf_config_suggestions(
    suggestions: EngineSuggestions, hf_config: dict[str, Any]
) -> None:
    quant = _hf_quantization_config(hf_config)
    quant_method = _optional_str(quant.get("quant_method")) if quant else None
    _merge_quant_suggestions(suggestions, quant_format=quant_method, source="hf_config")
    dtype = _hf_torch_dtype(hf_config)
    if dtype in {"float16", "bfloat16", "float32", "float"}:
        suggestions.engine.setdefault("dtype", dtype)
        suggestions.field_sources.setdefault("dtype", "hf_config")
    max_model_len = _hf_max_model_len(hf_config)
    if max_model_len is not None:
        suggestions.engine.setdefault("max_model_len", max_model_len)
        suggestions.field_sources.setdefault("max_model_len", "hf_config")


def _hf_quantization_config(hf_config: dict[str, Any]) -> dict[str, Any]:
    quant = hf_config.get("quantization_config")
    if isinstance(quant, dict):
        return quant
    text_config = hf_config.get("text_config")
    if isinstance(text_config, dict):
        quant = text_config.get("quantization_config")
        if isinstance(quant, dict):
            return quant
    return {}


def _hf_torch_dtype(hf_config: dict[str, Any]) -> str | None:
    dtype = _optional_str(hf_config.get("torch_dtype"))
    if dtype:
        return dtype
    text_config = hf_config.get("text_config")
    if isinstance(text_config, dict):
        return _optional_str(text_config.get("torch_dtype"))
    return None


def _hf_max_model_len(hf_config: dict[str, Any]) -> int | None:
    value = _optional_int_or_none(hf_config.get("max_position_embeddings"))
    if value is not None:
        return value
    text_config = hf_config.get("text_config")
    if isinstance(text_config, dict):
        return _optional_int_or_none(text_config.get("max_position_embeddings"))
    return None


def _load_hf_model_config(repo_id: str, revision: str | None = None) -> dict[str, Any]:
    try:
        from huggingface_hub import hf_hub_download

        path = hf_hub_download(repo_id, "config.json", revision=revision)
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _preset_by_name(name: str) -> dict[str, Any]:
    for preset in PRESETS:
        if preset["name"] == name:
            return preset
    raise ValueError(f"unknown deployment preset: {name}")


def _runtime_kind(value: object) -> RuntimeKind:
    if isinstance(value, dict):
        kind = value.get("kind", "process")
    elif value is None:
        kind = "process"
    else:
        kind = value
    if kind in {"build", "create_build", "adopt", "executable"}:
        return RuntimeKind.PROCESS
    return RuntimeKind(str(kind))


def _docker_image(spec: dict[str, Any], recipe: DeploymentRecipe | None) -> str:
    runtime = spec.get("runtime")
    if isinstance(runtime, dict):
        image = runtime.get("image")
        if image:
            return str(image)
    image = spec.get("image")
    if image:
        return str(image)
    if recipe is not None:
        recipe_image = recipe.docker.get("image")
        if recipe_image:
            return str(recipe_image)
    raise ValueError("docker runtime requires image")


def _preferred_port(overrides: dict[str, Any]) -> int | None:
    server = overrides.get("server")
    if isinstance(server, dict):
        return _optional_int(server.get("port"))
    return None


def _required_str(mapping: dict[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required")
    return value.strip()


def _optional_str(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"expected integer value: {value!r}") from exc


def _optional_int_or_none(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _mapping(value: object, *, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be a mapping")
    return dict(value)


def _deployment_name(value: object, model: str) -> str:
    explicit = _optional_str(value)
    if explicit:
        return _slug(explicit)
    return _slug(model_basename(model))


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip()).strip("-").lower()
    return slug or "deployment"


def _lint_config(cfg: ModelConfig) -> list[str]:
    warnings: list[str] = []
    if cfg.model.startswith(("/", "~")):
        warnings.append("model uses a host-local absolute path; prefer model_ref for portability")
    if cfg.command.executable and cfg.command.executable.startswith(("/", "~")):
        warnings.append("command.executable is host-local; prefer command.build or docker image")
    if cfg.command.cwd and str(cfg.command.cwd).startswith(("/", "~")):
        warnings.append("command.cwd is host-local; verify it exists on the target")
    if _literal_secret(cfg.server.api_key):
        warnings.append("server.api_key contains a literal secret; prefer target env injection")
    for key, value in sorted(cfg.env.items()):
        if _secretish_key(key) and _literal_secret(value):
            warnings.append(f"env.{key} contains a literal secret; prefer target env injection")
    return warnings


def _secretish_key(key: str) -> bool:
    normalized = key.upper()
    return any(part in normalized for part in ("TOKEN", "SECRET", "API_KEY", "PASSWORD"))


def _literal_secret(value: str | None) -> bool:
    if value is None:
        return False
    stripped = str(value).strip()
    if not stripped or stripped == "EMPTY":
        return False
    if stripped.startswith("$") or stripped.startswith("${"):
        return False
    return True
