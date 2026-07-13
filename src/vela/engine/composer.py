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
from vela.engine.command_builder import build_command, is_local_model_reference
from vela.engine.model_registry import (
    ModelRegistryError,
    default_hf_home_dir,
    inspect_model,
)
from vela.engine.profile import VllmProfileError, select_profile_for_config
from vela.engine.redaction import MASK, is_secret_key, scrub_text

DEFAULT_PORT_RANGE = (18000, 18999)
MUTABLE_PROCESS_REPRODUCIBILITY_WARNING = (
    "bare Process runtime uses the target's current mutable agent environment and "
    "cannot promise exact reinstantiation; select an immutable build_id for a "
    "reproducible deployment"
)
BLACKBIRD_QWEN36_IMAGE = (
    "vllm/vllm-openai@sha256:"
    "b13d6e5fda0785f3d41752df8513ff832f67cb231a216c76b6b4f2a515bf0046"
)
BLACKBIRD_QWEN36_VLLM_STACK = {
    "version_profile": "current",
    "version": "0.20.2rc1.dev9+g01d4d1ad3",
    "transformers_version": "5.7.0",
    "torch_version": "2.11.0+cu130",
    "cuda_version": "13.0",
}


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
    name: str | None = None
    description: str | None = None
    model_ref: str | None = None
    revision: str | None = None
    logging: dict[str, Any] = field(default_factory=dict)
    required_hostname: str | None = None
    source_artifacts: tuple[str, ...] = ()
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

OXCART_QWEN36_FP8_MTP_VL_EXTRA_ARGS = (
    "--trust-remote-code",
    "--attention-backend",
    "FLASHINFER",
    "--mm-encoder-attn-backend",
    "FLASHINFER",
    "--safetensors-load-strategy",
    "prefetch",
    "--max-num-batched-tokens",
    "8192",
    "--enable-prefix-caching",
    "--reasoning-parser",
    "qwen3",
    "--enable-auto-tool-choice",
    "--tool-call-parser",
    "qwen3_coder",
    "--limit-mm-per-prompt",
    '{"image":16,"video":1}',
    "--media-io-kwargs",
    '{"video":{"num_frames":-1}}',
    "--default-chat-template-kwargs",
    '{"enable_thinking":true,"preserve_thinking":true}',
    "--override-generation-config",
    '{"temperature":1.0,"top_p":0.95,"top_k":20,"min_p":0.0}',
    "--speculative-config",
    '{"method":"mtp","num_speculative_tokens":2}',
    "--compilation-config",
    '{"cudagraph_capture_sizes":[1,2,3,6,9,12],"cudagraph_num_of_warmups":1}',
    "--cudagraph-metrics",
    "--disable-uvicorn-access-log",
)

BLACKBIRD_QWEN36_EVICT = (
    "qwen36-27b-fp8-kvbf16-rp6000-server",
    "qwen36-27b-fp8-kvfp8-rp6000-server",
    "qwen36-27b-fp8-kvfp8-rp6000-vela",
    "qwen36-27b-fp8-rp6000-server",
    "qwen36-27b-bf16-rp6000-server",
    "qwen36-27b-bf16-rp6000-vela",
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
            "container_name": "qwen36-27b-fp8-kvfp8-rp6000-vela",
            "gpus": "all",
            "ipc_host": True,
            "shm_size": "32g",
            "network": "host",
            "restart": "no",
            "stop_grace_seconds": 90,
            "pull": "never",
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
        source_artifacts=(
            "infx/qwen36-27b-test/start-qwen36-27b-fp8-rp6000-blackbird.sh",
            (
                "infx/qwen36-27b-test/"
                "qwen36-27b-fp8-bf16-stack-redeploy-blackbird-20260528.md"
            ),
        ),
        vllm=BLACKBIRD_QWEN36_VLLM_STACK,
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
            "container_name": "qwen36-27b-bf16-rp6000-vela",
            "gpus": "all",
            "ipc_host": True,
            "shm_size": "32g",
            "network": "host",
            "restart": "no",
            "stop_grace_seconds": 90,
            "pull": "never",
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
        source_artifacts=(
            "infx/qwen36-27b-test/start-qwen36-bf16-rp6000-blackbird.sh",
            "infx/qwen36-27b-test/qwen-bf16-rp6000-blackbird-reload-20260509.md",
        ),
        vllm=BLACKBIRD_QWEN36_VLLM_STACK,
    ),
    DeploymentRecipe(
        key="oxcart-qwen36-27b-fp8-mtp-vl",
        label="Oxcart Qwen3.6 27B FP8 MTP + Vision",
        name="oxcart-qwen36-27b-fp8-mtp-vl",
        description=(
            "Oxcart-local Qwen3.6 27B FP8 validation profile, immutable cached "
            "revision, MTP + vision."
        ),
        target="local",
        runtime=RuntimeKind.DOCKER,
        models=("Qwen/Qwen3.6-27B-FP8",),
        model_ref="Qwen/Qwen3.6-27B-FP8",
        revision="e89b16ebf1988b3d6befa7de50abc2d76f26eb09",
        served_model_name="qwen36-27b-fp8-oxcart",
        server={
            "host": "127.0.0.1",
            "port": 18004,
            "exposure": "local",
            "api_key": "EMPTY",
        },
        engine={
            "gpu_memory_utilization": 0.955,
            "max_model_len": 262144,
            "dtype": "auto",
            "kv_cache_dtype": "auto",
            "max_num_seqs": 4,
        },
        extra_args=OXCART_QWEN36_FP8_MTP_VL_EXTRA_ARGS,
        launch={
            "mode": "attached",
            "ready_timeout_seconds": 1800,
            "require_cached_models": True,
            "required_hostname": "oxcart",
            "health": {"interval_seconds": 2},
            "runs_dir": (
                "/tank/ai/models/qwen36-27b-fp8/vllm-rp6000-mtp-vl/vela-runs"
            ),
        },
        docker={
            "image": BLACKBIRD_QWEN36_IMAGE,
            "container_name": "vela-oxcart-qwen36-27b-fp8-mtp-vl",
            "gpus": "all",
            "ipc_host": True,
            "shm_size": "32g",
            "network": "host",
            "restart": "no",
            "auto_remove": True,
            "stop_grace_seconds": 90,
            "pull": "never",
            "hf_cache": "/tank/ai/models/qwen36-27b-fp8/hf-cache",
            "hf_cache_target": "/root/.cache/huggingface",
            "volumes": [
                (
                    "/tank/ai/models/qwen36-27b-fp8/vllm-rp6000-mtp-vl/"
                    "vllm-cache:/root/.cache/vllm"
                ),
                (
                    "/tank/ai/models/qwen36-27b-fp8/vllm-rp6000-mtp-vl/"
                    "triton-cache:/root/.cache/triton"
                ),
                (
                    "/tank/ai/models/qwen36-27b-fp8/vllm-rp6000-mtp-vl/"
                    "torch-compile-cache:/root/.cache/torch"
                ),
                (
                    "/tank/ai/models/qwen36-27b-fp8/vllm-rp6000-mtp-vl/"
                    "flashinfer-cache:/root/.cache/flashinfer"
                ),
                (
                    "/tank/ai/models/qwen36-27b-fp8/vllm-rp6000-mtp-vl/"
                    "tmp:/tmp/qwen36-27b-fp8-mtp-vl"
                ),
            ],
            "env": {
                "HF_HOME": "/root/.cache/huggingface",
                "HF_HUB_CACHE": "/root/.cache/huggingface/hub",
                "HF_HUB_OFFLINE": "1",
                "VLLM_CACHE_ROOT": "/root/.cache/vllm",
                "TRITON_CACHE_DIR": "/root/.cache/triton",
                "TORCHINDUCTOR_CACHE_DIR": "/root/.cache/torch",
                "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True",
                "SAFETENSORS_FAST_GPU": "1",
            },
            "extra_run_args": [
                "--label",
                "ai.vela.managed=true",
                "--label",
                "ai.vela.profile=oxcart-qwen36-27b-fp8-mtp-vl",
            ],
        },
        logging={"request_logging": False},
        required_hostname="oxcart",
        source_artifacts=("configs/oxcart-qwen36-27b-fp8-mtp-vl.yaml",),
        vllm=BLACKBIRD_QWEN36_VLLM_STACK,
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
        "extra_args": ["--enable-prefix-caching", "--enable-chunked-prefill"],
        "applies_to": ["all"],
    },
    {
        "name": "throughput",
        "description": "Favor higher batch volume when memory headroom exists.",
        "engine": {"gpu_memory_utilization": 0.92, "dtype": "auto", "max_num_seqs": 32},
        "extra_args": [
            "--enable-prefix-caching",
            "--enable-chunked-prefill",
            "--max-num-batched-tokens",
            "8192",
            "--compilation-config",
            '{"cudagraph_capture_sizes":[1,2,4,8,16],"cudagraph_num_of_warmups":1}',
        ],
        "applies_to": ["all"],
    },
    {
        "name": "long-context",
        "description": "Reserve memory for longer prompts and conservative concurrency.",
        "engine": {
            "gpu_memory_utilization": 0.9,
            "dtype": "auto",
            "max_num_seqs": 4,
            "max_model_len": 131072,
        },
        "extra_args": ["--enable-prefix-caching"],
        "applies_to": ["all"],
    },
    {
        "name": "low-memory",
        "description": "Conservative memory profile for tight cards or experiments.",
        "engine": {
            "gpu_memory_utilization": 0.85,
            "dtype": "auto",
            "max_num_seqs": 2,
            "enforce_eager": True,
        },
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


def list_deployment_recipes(
    target: str | None = None, *, hostname: str | None = None
) -> list[dict[str, Any]]:
    target_key = target.lower() if isinstance(target, str) and target.strip() else None
    hostname_key = _normalized_hostname(hostname)
    recipes: list[dict[str, Any]] = []
    for recipe in LAB_RECIPES:
        if target_key is not None and recipe.target.lower() != target_key:
            continue
        if recipe.required_hostname is not None and not _hostname_matches(
            recipe.required_hostname, hostname_key
        ):
            continue
        model = recipe.models[0] if recipe.models else ""
        name = recipe.name or f"{recipe.served_model_name}-{recipe.target}"
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
                "model_ref": recipe.model_ref,
                "revision": recipe.revision,
                "served_model_name": recipe.served_model_name,
                "description": recipe.description,
                "image": docker.get("image", ""),
                "server": dict(recipe.server),
                "engine": dict(recipe.engine),
                "extra_args": list(recipe.extra_args),
                "launch": dict(recipe.launch),
                "docker": docker,
                "logging": dict(recipe.logging),
                "required_hostname": recipe.required_hostname,
                "source_artifacts": list(recipe.source_artifacts),
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
    occupied_container_names: Iterable[str] | None = None,
    hostname: str | None = None,
) -> ComposeResult:
    model_context = _model_context(spec, models_registry_path=models_registry_path)
    model = model_context.model
    target = _optional_str(spec.get("target"))
    runtime = _runtime_kind(spec.get("runtime"))
    overrides = _mapping(spec.get("overrides"), field_name="overrides")
    preset_name = _optional_str(spec.get("preset")) or "balanced"
    preset = _preset_by_name(preset_name)
    recipe = _resolve_recipe(
        spec,
        target=target,
        runtime=runtime,
        model=model,
        hostname=hostname,
    )
    name = _deployment_name(
        spec.get("name") or (recipe.name if recipe is not None else None), model
    )
    suggestions = _engine_suggestions(model_context)
    port = allocate_port(
        preferred=_preferred_port(overrides) or _recipe_port(recipe),
        configs_dir=configs_dir,
        occupied_ports=occupied_ports,
        exclude_config_name=name,
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
        "description": (
            recipe.description
            if recipe is not None and recipe.description is not None
            else f"Generated by Vela deployment composer using preset {preset_name}."
        ),
        "command": _runtime_command(runtime, spec, name, recipe, model_context),
        "engine": engine,
        "server": server,
        "extra_args": _seed_extra_args(preset, recipe),
        "launch": launch,
    }
    model_ref, revision = _selected_recipe_model_identity(model_context, recipe)
    if model_ref:
        payload["model_ref"] = model_ref
    if revision:
        payload["revision"] = revision
    if recipe is not None and recipe.logging:
        payload["logging"] = dict(recipe.logging)
    if recipe is not None and recipe.vllm:
        payload["vllm"] = dict(recipe.vllm)
    if target is None:
        payload.pop("target")

    _merge_overrides(payload, overrides)
    _merge_extra_args(payload, overrides)
    container_name_result = _avoid_docker_container_collision(
        payload,
        recipe=recipe,
        occupied_container_names=occupied_container_names,
    )
    cfg = ModelConfig.model_validate(payload)
    unsafe_runtime_warnings = _blackwell_fp8_runtime_warnings(
        target=cfg.target,
        runtime=cfg.command.runtime,
        recipe=recipe,
        model_context=model_context,
        suggestions=suggestions,
        config=cfg,
    )
    if unsafe_runtime_warnings:
        raise ValueError(
            "; ".join(unsafe_runtime_warnings)
            + ": Blackwell FP8 Docker deployments require a matched local lab recipe"
        )
    warnings = [
        *suggestions.warnings,
        *_recipe_runtime_warnings(spec, recipe),
        *(recipe.warnings if recipe is not None else ()),
    ]
    warnings.extend(port["warnings"])
    warnings.extend(container_name_result["warnings"])
    warnings.extend(_compose_config_warnings(cfg))
    return ComposeResult(
        config=cfg,
        warnings=warnings,
        derived=_compose_provenance(
            cfg,
            spec=spec,
            model_context=model_context,
            recipe=recipe,
            overrides=overrides,
            preset_name=preset_name,
            port_allocation=port,
            engine_sources=engine_sources,
            container_name_result=container_name_result,
        ),
    )


def allocate_port(
    *,
    preferred: int | None = None,
    configs_dir: str | Path | None = None,
    port_range: tuple[int, int] = DEFAULT_PORT_RANGE,
    occupied_ports: Mapping[str, Iterable[int]] | None = None,
    exclude_config_name: str | None = None,
) -> dict[str, Any]:
    configured_ports = _configured_ports(configs_dir, exclude_config_name=exclude_config_name)
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
    occupied_container_names: Iterable[str] | None = None,
    hostname: str | None = None,
) -> dict[str, Any]:
    model_context = _model_context(params, models_registry_path=models_registry_path)
    model = model_context.model
    target = _optional_str(params.get("target"))
    runtime = _runtime_kind(params.get("runtime"))
    recipe = _resolve_recipe(
        params,
        target=target,
        runtime=runtime,
        model=model,
        hostname=hostname,
    )
    name = _deployment_name(
        params.get("name") or (recipe.name if recipe is not None else None), model
    )
    suggestions = _engine_suggestions(model_context)
    allocation = allocate_port(
        preferred=_optional_int(params.get("preferred_port")) or _recipe_port(recipe),
        configs_dir=configs_dir,
        occupied_ports=occupied_ports,
        exclude_config_name=name,
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
        "warnings": [
            *suggestions.warnings,
            *_blackwell_fp8_runtime_warnings(
                target=target,
                runtime=runtime,
                recipe=recipe,
                model_context=model_context,
                suggestions=suggestions,
            ),
            *allocation["warnings"],
        ],
    }
    model_ref, revision = _selected_recipe_model_identity(model_context, recipe)
    if model_ref:
        payload["model_ref"] = model_ref
    if revision:
        payload["revision"] = revision
    if recipe is not None:
        payload["recipe"] = {"key": recipe.key, "label": recipe.label}
    if runtime is RuntimeKind.DOCKER:
        container_name = _suggested_container_name(
            f"vela-{name}",
            recipe=recipe,
            occupied_container_names=occupied_container_names,
        )
        payload["container_name"] = container_name["name"]
        payload["warnings"].extend(container_name["warnings"])
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
    errors.extend(_secret_literal_errors(cfg))
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
    model_context: ModelContext,
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
    requested_hf_cache = _requested_docker_hf_cache(spec)
    if requested_hf_cache is not None:
        # The compose input's runtime mapping is the Docker spec. Apply this
        # after recipe seeding so an operator-selected target cache has the
        # same explicit-over-derived precedence as the rest of the composer.
        docker["hf_cache"] = requested_hf_cache
    # A generic docker deployment of an hf_repo model gets the agent's HF cache
    # mounted by default (H3): registry downloads land in that cache, so without
    # the mount every fresh container re-downloads the weights. A matched lab
    # recipe keeps its own mounts byte-identically; local-path/url models need no
    # HF cache; and an explicit value always wins (setdefault).
    if recipe is None and _model_uses_hf_cache(model_context):
        docker.setdefault("hf_cache", str(default_hf_home_dir()))
    return {
        "runtime": "docker",
        "docker": docker,
    }


def _model_uses_hf_cache(model_context: ModelContext) -> bool:
    """True when the deployment's model is a Hugging Face repo (not local/url)."""
    entry = model_context.entry
    if isinstance(entry, dict):
        return _optional_str(entry.get("source")) == "hf_repo"
    model = model_context.model
    if is_local_model_reference(model):
        return False
    return "://" not in model


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
    served = overrides.get("served_model_name")
    if served is not None:
        if not isinstance(served, str) or not served.strip():
            raise ValueError("overrides.served_model_name must be a non-empty string")
        payload["served_model_name"] = served.strip()
    container = overrides.get("container_name")
    if container is not None:
        if not isinstance(container, str) or not container.strip():
            raise ValueError("overrides.container_name must be a non-empty string")
        command = payload.get("command")
        if isinstance(command, dict) and command.get("runtime") == "docker":
            docker = dict(command.get("docker") or {})
            docker["container_name"] = container.strip()
            command["docker"] = docker


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


def _avoid_docker_container_collision(
    payload: dict[str, Any],
    *,
    recipe: DeploymentRecipe | None,
    occupied_container_names: Iterable[str] | None,
) -> dict[str, list[Any]]:
    command = payload.get("command")
    if not isinstance(command, dict) or command.get("runtime") != "docker":
        return {"warnings": [], "derived": []}
    docker = command.get("docker")
    if not isinstance(docker, dict):
        return {"warnings": [], "derived": []}
    current = _optional_str(docker.get("container_name"))
    if current is None or recipe is not None:
        return {"warnings": [], "derived": []}
    fresh = _fresh_container_name(current, occupied_container_names)
    if fresh == current:
        return {"warnings": [], "derived": []}
    updated_docker = dict(docker)
    updated_docker["container_name"] = fresh
    updated_command = dict(command)
    updated_command["docker"] = updated_docker
    payload["command"] = updated_command
    return {
        "warnings": ["container-name-reassigned"],
        "derived": [
            {
                "field": "command.docker.container_name",
                "value": fresh,
                "source": "docker_container_name_collision",
            }
        ],
    }


def _suggested_container_name(
    preferred: str,
    *,
    recipe: DeploymentRecipe | None,
    occupied_container_names: Iterable[str] | None,
) -> dict[str, Any]:
    if recipe is not None:
        return {"name": preferred, "warnings": []}
    fresh = _fresh_container_name(preferred, occupied_container_names)
    warnings = ["container-name-reassigned"] if fresh != preferred else []
    return {"name": fresh, "warnings": warnings}


def _fresh_container_name(preferred: str, occupied_container_names: Iterable[str] | None) -> str:
    occupied = {
        str(name).strip().lstrip("/")
        for name in (occupied_container_names or ())
        if str(name).strip()
    }
    if preferred not in occupied:
        return preferred
    suffix = 2
    while f"{preferred}-{suffix}" in occupied:
        suffix += 1
    return f"{preferred}-{suffix}"


def _recipe_derived(recipe: DeploymentRecipe | None) -> list[dict[str, str]]:
    if recipe is None:
        return []
    derived = [
        {
            "field": "deployment.recipe",
            "value": recipe.key,
            "source": "lab_recipe",
        }
    ]
    for artifact in recipe.source_artifacts:
        derived.append(
            {
                "field": "deployment.recipe_source",
                "value": artifact,
                "source": "local_recipe_artifact",
            }
        )
    return derived


def _compose_provenance(
    cfg: ModelConfig,
    *,
    spec: dict[str, Any],
    model_context: ModelContext,
    recipe: DeploymentRecipe | None,
    overrides: dict[str, Any],
    preset_name: str,
    port_allocation: dict[str, Any],
    engine_sources: dict[str, str],
    container_name_result: dict[str, list[Any]],
) -> list[dict[str, str]]:
    """Describe where every review-visible launch value came from.

    ``derived`` predates the guided review screen, but it is now the wire-level
    provenance payload.  Values are taken from the validated final config while
    sources are determined from the inputs that won precedence.  Secret-bearing
    fields are masked here, before the payload can cross the agent boundary.
    """

    rows = _recipe_derived(recipe)
    recipe_source = f"lab_recipe:{recipe.key}" if recipe is not None else None

    def add(field: str, value: object, source: str) -> None:
        rows.append(
            {
                "field": field,
                "value": provenance_value(field, value),
                "source": source,
            }
        )

    if cfg.target is not None:
        add("target", cfg.target, recipe_source or "operator_input")

    if model_context.entry is not None:
        model_source = "model_registry"
    else:
        model_source = recipe_source or "operator_input"
    add("model", cfg.model, model_source)
    if cfg.model_ref is not None:
        model_ref_source = (
            "model_registry:selected_pin"
            if model_context.model_ref is not None
            else recipe_source or "operator_input"
        )
        add("model_ref", cfg.model_ref, model_ref_source)
    if cfg.revision is not None:
        if model_context.model_ref is not None and model_context.entry is not None:
            revision_source = "model_registry:resolved_commit"
        else:
            revision_source = recipe_source or "operator_input"
        add("revision", cfg.revision, revision_source)

    served_override = overrides.get("served_model_name")
    if (
        served_override is not None
        and recipe is not None
        and served_override == recipe.served_model_name
    ):
        served_source = recipe_source or "operator_override"
    elif served_override is not None:
        served_source = "operator_override"
    else:
        served_source = _served_model_source(model_context, recipe)
    add("served_model_name", cfg.served_model_name or "", served_source)

    add("command.runtime", cfg.command.runtime.value, recipe_source or "operator_input")
    add("command.entrypoint", cfg.command.entrypoint.value, "schema_default")
    if cfg.command.build is not None:
        add("command.build", cfg.command.build, "operator_input")
    if cfg.command.executable is not None:
        add("command.executable", cfg.command.executable, "operator_input")
    if cfg.command.cwd is not None:
        add("command.cwd", cfg.command.cwd, "operator_input")

    server = cfg.server.model_dump(mode="json")
    server_overrides = _mapping_or_empty(overrides.get("server"))
    recipe_server = recipe.server if recipe is not None else {}
    for key in ("host", "exposure", "api_key", "probe_host"):
        value = server.get(key)
        if value is None:
            continue
        source = _field_source(
            key,
            value=value,
            overrides=server_overrides,
            recipe_values=recipe_server,
            recipe_source=recipe_source,
        )
        add(f"server.{key}", value, source)

    requested_port = _preferred_port(overrides)
    recipe_port = _recipe_port(recipe)
    port_reassigned = "port-reassigned" in port_allocation.get("warnings", [])
    if (
        requested_port is not None
        and cfg.server.port == requested_port
        and not port_reassigned
    ):
        port_source = (
            recipe_source
            if recipe_port is not None and requested_port == recipe_port
            else "operator_override"
        ) or "operator_override"
    elif recipe_port is not None and cfg.server.port == recipe_port and not port_reassigned:
        port_source = recipe_source or "port_allocator"
    else:
        # This includes both ordinary allocation and collision reassignment.  The
        # allocator warnings retain the more specific collision explanation.
        port_source = "port_allocator"
    add("server.port", cfg.server.port, port_source)

    engine = cfg.engine.model_dump(mode="json", exclude_none=True)
    engine_overrides = _mapping_or_empty(overrides.get("engine"))
    for key, value in engine.items():
        if key in engine_overrides:
            source = (
                recipe_source
                if recipe is not None and recipe.engine.get(key) == value
                else "operator_override"
            ) or "operator_override"
        elif key in engine_sources:
            source = engine_sources[key]
        else:
            source = recipe_source or f"preset:{preset_name}"
        add(f"engine.{key}", value, source)

    base_extra_source = recipe_source or f"preset:{preset_name}"
    extra_source = (
        f"{base_extra_source} + operator_override"
        if "extra_args" in overrides
        else base_extra_source
    )
    add("extra_args", cfg.extra_args, extra_source)

    config_env_source = "operator_override" if "env" in overrides else "schema_default"
    add("env", cfg.env, config_env_source)

    launch = cfg.launch.model_dump(mode="json")
    launch_overrides = _mapping_or_empty(overrides.get("launch"))
    recipe_launch = recipe.launch if recipe is not None else {}
    health = _mapping_or_empty(launch.pop("health", None))
    for key, value in launch.items():
        if key == "runs_dir" and key not in launch_overrides and key not in recipe_launch:
            source = "generated_runs_dir"
        else:
            source = _field_source(
                key,
                value=value,
                overrides=launch_overrides,
                recipe_values=recipe_launch,
                recipe_source=recipe_source,
            )
        add(f"launch.{key}", value, source)
    recipe_health = _mapping_or_empty(recipe_launch.get("health"))
    override_health = _mapping_or_empty(launch_overrides.get("health"))
    for key, value in health.items():
        add(
            f"launch.health.{key}",
            value,
            _field_source(
                key,
                value=value,
                overrides=override_health,
                recipe_values=recipe_health,
                recipe_source=recipe_source,
            ),
        )

    logging = cfg.logging.model_dump(mode="json")
    recipe_logging = recipe.logging if recipe is not None else {}
    for key, value in logging.items():
        source = recipe_source if key in recipe_logging and recipe_source else "schema_default"
        add(f"logging.{key}", value, source)

    vllm = cfg.vllm.model_dump(mode="json", exclude_none=True)
    recipe_vllm = recipe.vllm if recipe is not None else {}
    for key, value in vllm.items():
        source = recipe_source if key in recipe_vllm and recipe_source else "schema_default"
        add(f"vllm.{key}", value, source)

    docker = cfg.command.docker
    if cfg.command.runtime is RuntimeKind.DOCKER and docker is not None:
        docker_values = docker.model_dump(mode="json")
        recipe_docker = recipe.docker if recipe is not None else {}
        collision_source = _container_collision_source(container_name_result)
        requested_hf_cache = _requested_docker_hf_cache(spec)
        for key, value in docker_values.items():
            if key == "image":
                source = recipe_source or "operator_input"
            elif key == "container_name":
                if "container_name" in overrides:
                    source = (
                        recipe_source
                        if recipe_docker.get("container_name") == value
                        else "operator_override"
                    ) or "operator_override"
                elif collision_source is not None:
                    source = collision_source
                elif recipe_source is not None:
                    source = recipe_source
                else:
                    source = "generated_container_name"
            elif key == "hf_cache" and requested_hf_cache is not None:
                source = (
                    recipe_source
                    if recipe_docker.get("hf_cache") == value
                    else "operator_input"
                ) or "operator_input"
            elif key == "hf_cache" and recipe_source is None and value is not None:
                source = "agent_hf_cache_default"
            elif key in recipe_docker and recipe_source is not None:
                source = recipe_source
            else:
                source = "schema_default"
            add(f"command.docker.{key}", value, source)

    return rows


def _field_source(
    key: str,
    *,
    value: object,
    overrides: Mapping[str, Any],
    recipe_values: Mapping[str, Any],
    recipe_source: str | None,
) -> str:
    if key in overrides:
        if key in recipe_values and recipe_values[key] == value and recipe_source is not None:
            return recipe_source
        return "operator_override"
    if key in recipe_values and recipe_source is not None:
        return recipe_source
    return "schema_default"


def _mapping_or_empty(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _container_collision_source(result: dict[str, list[Any]]) -> str | None:
    for item in result.get("derived", []):
        if not isinstance(item, dict):
            continue
        if item.get("field") == "command.docker.container_name":
            source = item.get("source")
            if isinstance(source, str) and source:
                return source
    return None


def provenance_value(field: str, value: object) -> str:
    """Return a stable human-readable value that is safe to send to Review."""
    if field == "server.api_key" or _provenance_field_is_secret(field):
        return MASK
    if field == "env" or field.endswith(".env"):
        mapping = value if isinstance(value, dict) else {}
        redacted = {str(key): MASK for key in sorted(mapping, key=str)}
        return json.dumps(redacted, ensure_ascii=False, sort_keys=True)
    if field in {"extra_args", "command.docker.extra_run_args"} and isinstance(value, list):
        value = _redacted_cli_args(value)
    if value is None:
        return "none"
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, Path):
        return scrub_text(str(value))
    try:
        rendered = json.dumps(value, default=str, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        rendered = str(value)
    return scrub_text(rendered)


def _provenance_field_is_secret(field: str) -> bool:
    leaf = field.rsplit(".", 1)[-1].upper()
    return leaf in {"TOKEN", "API_KEY", "PASSWORD", "SECRET", "AUTHORIZATION"}


def _redacted_cli_args(args: list[Any]) -> list[str]:
    redacted: list[str] = []
    redact_next = False
    for raw in args:
        item = str(raw)
        if redact_next:
            redacted.append(MASK)
            redact_next = False
            continue
        flag, separator, inline_value = item.partition("=")
        if _secret_cli_flag(flag):
            if separator:
                redacted.append(f"{flag}={MASK}")
            else:
                redacted.append(scrub_text(item))
                redact_next = True
            continue
        redacted.append(scrub_text(item if not separator else f"{flag}={inline_value}"))
    return redacted


def _secret_cli_flag(flag: str) -> bool:
    normalized = flag.lstrip("-").lower().replace("-", "_")
    parts = set(normalized.split("_"))
    return bool(parts & {"token", "secret", "password", "authorization"}) or normalized in {
        "api_key",
        "access_key",
        "private_key",
    }


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


def _configured_ports(
    configs_dir: str | Path | None,
    *,
    exclude_config_name: str | None = None,
) -> set[int]:
    registry = load_registry(configs_dir)
    excluded = _optional_str(exclude_config_name)
    return {
        item.config.server.port
        for item in registry.valid
        if excluded is None or item.config.name != excluded
    }


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
    resolved_model = _saved_model_identity(entry, model_ref, requested=model)
    revision: str | None = None
    if entry.get("source") == "hf_repo":
        requested_revision = _optional_str(spec.get("revision"))
        commit_sha = _optional_str(entry.get("commit_sha"))
        pinned_revision = _optional_str(entry.get("revision"))
        if commit_sha is not None:
            if requested_revision not in {None, commit_sha, pinned_revision}:
                raise ValueError(
                    f"model_ref {model_ref} resolves to commit {commit_sha}, "
                    f"but requested revision is {requested_revision}"
                )
            revision = commit_sha
        else:
            revision = requested_revision or pinned_revision
    return ModelContext(
        model=resolved_model,
        model_ref=model_ref,
        revision=revision,
        display_name=_optional_str(entry.get("display_name")),
        entry=entry,
    )


def _saved_model_identity(
    entry: dict[str, Any], model_ref: str, *, requested: str | None
) -> str:
    """Return a portable config identity; handoff supplies target-local paths."""
    if entry.get("source") != "local_path":
        return requested or _model_arg_from_entry(entry, model_ref)

    local_path = _optional_str(entry.get("local_path"))
    if local_path is None:
        raise ValueError(f"model_ref {model_ref} does not have a launchable model source")
    if requested is not None and requested.startswith(("/", "./", "../", "~")):
        if Path(requested).expanduser() != Path(local_path).expanduser():
            raise ValueError(
                f"model_ref {model_ref} resolves to local path {local_path}, "
                f"but requested model path is {requested}"
            )
        requested = None
    for candidate in (
        requested,
        _optional_str(entry.get("display_name")),
        _optional_str(entry.get("entry_id")),
        model_ref,
    ):
        if candidate and not candidate.startswith(("/", "./", "../", "~")):
            return candidate
    raise ValueError(f"model_ref {model_ref} has no portable registry identity")


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


def _selected_recipe_model_identity(
    model_context: ModelContext, recipe: DeploymentRecipe | None
) -> tuple[str | None, str | None]:
    """Keep a selected immutable pin instead of replacing it with a repo alias."""
    if recipe is None:
        return model_context.model_ref, model_context.revision
    if model_context.model_ref is None:
        return recipe.model_ref, recipe.revision

    entry = model_context.entry or {}
    expected_repo = recipe.model_ref
    actual_repo = _optional_str(entry.get("repo_id"))
    if expected_repo is not None and actual_repo != expected_repo:
        raise ValueError(
            f"model_ref {model_context.model_ref} resolves to {actual_repo or '<unknown>'}, "
            f"but recipe {recipe.key} requires repo {expected_repo}"
        )
    expected_commit = recipe.revision
    actual_commit = _optional_str(entry.get("commit_sha"))
    if expected_commit is not None and actual_commit != expected_commit:
        raise ValueError(
            f"model_ref {model_context.model_ref} resolves to commit "
            f"{actual_commit or '<unknown>'}, but recipe {recipe.key} requires "
            f"{expected_commit}"
        )
    return model_context.model_ref, recipe.revision or model_context.revision


def _resolve_recipe(
    params: dict[str, Any],
    *,
    target: str | None,
    runtime: RuntimeKind,
    model: str,
    hostname: str | None,
) -> DeploymentRecipe | None:
    if "recipe" not in params:
        return _matching_recipe(
            target=target,
            runtime=runtime,
            model=model,
            hostname=hostname,
        )
    requested = params.get("recipe")
    if requested is None or requested == "__custom__":
        return None
    if not isinstance(requested, str) or not requested.strip():
        raise ValueError("recipe must be a recipe key, __custom__, or null")
    key = requested.strip()
    recipe = next((candidate for candidate in LAB_RECIPES if candidate.key == key), None)
    if recipe is None:
        raise ValueError(f"unknown deployment recipe: {key}")
    if target is None or recipe.target.lower() != target.lower():
        raise ValueError(
            f"recipe {key} requires target {recipe.target}, got {target or '<none>'}"
        )
    if recipe.runtime is not runtime:
        raise ValueError(
            f"recipe {key} requires runtime {recipe.runtime.value}, got {runtime.value}"
        )
    if model not in recipe.models:
        raise ValueError(
            f"recipe {key} does not support model {model}; expected one of "
            + ", ".join(recipe.models)
        )
    if recipe.required_hostname is not None and not _hostname_matches(
        recipe.required_hostname, _normalized_hostname(hostname)
    ):
        raise ValueError(
            f"recipe {key} requires hostname {recipe.required_hostname}, "
            f"got {hostname or '<unknown>'}"
        )
    return recipe


def _matching_recipe(
    *, target: str | None, runtime: RuntimeKind, model: str, hostname: str | None
) -> DeploymentRecipe | None:
    if target is None:
        return None
    target_key = target.lower()
    hostname_key = _normalized_hostname(hostname)
    for recipe in LAB_RECIPES:
        if recipe.runtime is not runtime:
            continue
        if recipe.target.lower() != target_key:
            continue
        if recipe.required_hostname is not None and not _hostname_matches(
            recipe.required_hostname, hostname_key
        ):
            continue
        if model in recipe.models:
            return recipe
    return None


def _normalized_hostname(value: str | None) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().split(".", 1)[0].lower()


def _hostname_matches(required: str, actual: str | None) -> bool:
    return actual is not None and required.strip().lower() == actual


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
    if recipe is not None:
        recipe_image = recipe.docker.get("image")
        if recipe_image:
            return str(recipe_image)
    requested = _requested_docker_image(spec)
    if requested:
        return requested
    raise ValueError("docker runtime requires image")


def _requested_docker_image(spec: dict[str, Any]) -> str | None:
    runtime = spec.get("runtime")
    if isinstance(runtime, dict):
        image = _optional_str(runtime.get("image"))
        if image:
            return image
    return _optional_str(spec.get("image"))


def _requested_docker_hf_cache(spec: dict[str, Any]) -> str | None:
    runtime = spec.get("runtime")
    if not isinstance(runtime, dict):
        return None
    return _optional_str(runtime.get("hf_cache"))


def _recipe_runtime_warnings(
    spec: dict[str, Any], recipe: DeploymentRecipe | None
) -> list[str]:
    if recipe is None:
        return []
    recipe_image = _optional_str(recipe.docker.get("image"))
    requested_image = _requested_docker_image(spec)
    if requested_image is not None and requested_image != recipe_image:
        return ["recipe-image-override-ignored"]
    return []


def _blackwell_fp8_runtime_warnings(
    *,
    target: str | None,
    runtime: RuntimeKind,
    recipe: DeploymentRecipe | None,
    model_context: ModelContext,
    suggestions: EngineSuggestions,
    config: ModelConfig | None = None,
) -> list[str]:
    if recipe is not None or runtime is not RuntimeKind.DOCKER:
        return []
    target_key = (target or "").lower()
    if target_key not in {"blackbird", "p620-01", "p620"}:
        return []
    if config is not None:
        uses_fp8 = _config_uses_fp8_runtime_shape(config, model_context, suggestions)
    else:
        uses_fp8 = _looks_like_fp8_model(model_context, suggestions)
    if not uses_fp8:
        return []
    return ["blackwell-fp8-runtime-recipe-required"]


def _config_uses_fp8_runtime_shape(
    cfg: ModelConfig, model_context: ModelContext, suggestions: EngineSuggestions
) -> bool:
    extra_kv_dtype = _last_extra_arg_value(cfg.extra_args, "--kv-cache-dtype")
    if extra_kv_dtype:
        return extra_kv_dtype.lower() == "fp8"
    if cfg.engine.kv_cache_dtype:
        return cfg.engine.kv_cache_dtype.lower() == "fp8"
    return _looks_like_fp8_model(model_context, suggestions)


def _last_extra_arg_value(args: list[str], flag: str) -> str | None:
    value: str | None = None
    prefix = f"{flag}="
    for index, item in enumerate(args):
        if item == flag and index + 1 < len(args):
            value = args[index + 1]
        elif item.startswith(prefix):
            value = item[len(prefix) :]
    return value


def _looks_like_fp8_model(
    model_context: ModelContext, suggestions: EngineSuggestions
) -> bool:
    if str(suggestions.engine.get("kv_cache_dtype") or "").lower() == "fp8":
        return True
    if "fp8" in model_context.model.lower():
        return True
    entry = model_context.entry
    if isinstance(entry, dict):
        return "fp8" in str(entry.get("quant_format") or "").lower()
    return False


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
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"expected integer value: {value!r}") from exc


def _optional_int_or_none(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)  # type: ignore[call-overload]
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
    if cfg.command.runtime is RuntimeKind.DOCKER and cfg.command.docker is not None:
        warnings.extend(_docker_lint_warnings(cfg))
    warnings.extend(_exposure_lint_warnings(cfg))
    return warnings


def _secret_literal_errors(cfg: ModelConfig) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    if _literal_secret(cfg.server.api_key):
        errors.append(
            {
                "field": "server.api_key",
                "message": "contains a literal secret; prefer target env injection",
            }
        )
    for key, value in sorted(cfg.env.items()):
        if _secretish_key(key) and _literal_secret(value):
            errors.append(
                {
                    "field": f"env.{key}",
                    "message": "contains a literal secret; prefer target env injection",
                }
            )
    docker = cfg.command.docker
    if docker is not None:
        for key, value in sorted(docker.env.items()):
            if _secretish_key(key) and _literal_secret(value):
                errors.append(
                    {
                        "field": f"command.docker.env.{key}",
                        "message": "contains a literal secret; prefer target env injection",
                    }
                )
    return errors


def _compose_config_warnings(cfg: ModelConfig) -> list[str]:
    warnings: list[str] = []
    if (
        cfg.command.runtime is RuntimeKind.PROCESS
        and cfg.command.build is None
        and cfg.command.executable is None
    ):
        warnings.append(MUTABLE_PROCESS_REPRODUCIBILITY_WARNING)
    warnings.extend(_docker_lint_warnings(cfg))
    warnings.extend(_canonical_bind_warnings(cfg))
    return warnings


def _docker_lint_warnings(cfg: ModelConfig) -> list[str]:
    docker = cfg.command.docker
    if cfg.command.runtime is not RuntimeKind.DOCKER or docker is None:
        return []
    warnings: list[str] = []
    image = docker.image.strip()
    if image.endswith(":latest") or ":latest@" in image:
        warnings.append(
            "command.docker.image uses :latest; pin a version tag or sha256 digest "
            "for reproducible Docker deployments"
        )
    if "@sha256:" not in image:
        warnings.append(
            "command.docker.image is not digest-pinned; prefer a sha256 image digest "
            "for reproducible Docker deployments"
        )
    if docker.gpus is not None and not str(docker.gpus).strip():
        warnings.append(
            "command.docker.gpus is blank; Docker will launch without an explicit GPU reservation"
        )
    return warnings


def _exposure_lint_warnings(cfg: ModelConfig) -> list[str]:
    if cfg.server.host not in {"127.0.0.1", "localhost", "::1"}:
        return []
    if cfg.server.exposure is Exposure.LOCAL:
        return []
    return [
        (
            f"server.exposure is {cfg.server.exposure.value} but server.host is loopback; "
            "bind 0.0.0.0 or set exposure: local"
        )
    ]


def _canonical_bind_warnings(cfg: ModelConfig) -> list[str]:
    if cfg.server.host in {"127.0.0.1", "localhost", "::1"}:
        return []
    return [
        "non-local-bind: server.host exposes beyond loopback; verify target firewall/API key"
    ]


def _secretish_key(key: str) -> bool:
    return is_secret_key(key)


def _literal_secret(value: str | None) -> bool:
    if value is None:
        return False
    stripped = str(value).strip()
    if not stripped or stripped == "EMPTY":
        return False
    if stripped.startswith("$") or stripped.startswith("${"):
        return False
    return True
