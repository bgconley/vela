#!/usr/bin/env python3

import argparse
import asyncio
import os
import re
import sys
from dataclasses import dataclass
from typing import Any

from vela.config.targets import load_targets_file
from vela.transport.factory import target_client_for_config

BLACKBIRD_QWEN36_IMAGE = (
    "vllm/vllm-openai@sha256:"
    "b13d6e5fda0785f3d41752df8513ff832f67cb231a216c76b6b4f2a515bf0046"
)
OXCART_QWEN36_PROFILE = "oxcart-qwen36-27b-fp8-mtp-vl"
OXCART_QWEN36_MODEL = "Qwen/Qwen3.6-27B-FP8"
OXCART_QWEN36_REVISION = "e89b16ebf1988b3d6befa7de50abc2d76f26eb09"
OXCART_QWEN36_CONTAINER = "vela-oxcart-qwen36-27b-fp8-mtp-vl"
OXCART_QWEN36_HF_CACHE = "/tank/ai/models/qwen36-27b-fp8/hf-cache"
OXCART_QWEN36_RUNS_DIR = (
    "/tank/ai/models/qwen36-27b-fp8/vllm-rp6000-mtp-vl/vela-runs"
)
OXCART_QWEN36_VOLUMES = (
    "/tank/ai/models/qwen36-27b-fp8/vllm-rp6000-mtp-vl/"
    "vllm-cache:/root/.cache/vllm",
    "/tank/ai/models/qwen36-27b-fp8/vllm-rp6000-mtp-vl/"
    "triton-cache:/root/.cache/triton",
    "/tank/ai/models/qwen36-27b-fp8/vllm-rp6000-mtp-vl/"
    "torch-compile-cache:/root/.cache/torch",
    "/tank/ai/models/qwen36-27b-fp8/vllm-rp6000-mtp-vl/"
    "flashinfer-cache:/root/.cache/flashinfer",
    "/tank/ai/models/qwen36-27b-fp8/vllm-rp6000-mtp-vl/"
    "tmp:/tmp/qwen36-27b-fp8-mtp-vl",
)
OXCART_QWEN36_DOCKER_ENV = {
    "HF_HOME": "/root/.cache/huggingface",
    "HF_HUB_CACHE": "/root/.cache/huggingface/hub",
    "HF_HUB_OFFLINE": "1",
    "VLLM_CACHE_ROOT": "/root/.cache/vllm",
    "TRITON_CACHE_DIR": "/root/.cache/triton",
    "TORCHINDUCTOR_CACHE_DIR": "/root/.cache/torch",
    "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True",
    "SAFETENSORS_FAST_GPU": "1",
}
OXCART_QWEN36_DOCKER_ARGS = (
    "--label",
    "ai.vela.managed=true",
    "--label",
    f"ai.vela.profile={OXCART_QWEN36_PROFILE}",
)
OXCART_QWEN36_EXTRA_ARGS = (
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


class BackendEvidenceError(RuntimeError):
    pass


@dataclass(frozen=True)
class BackendEvidenceRule:
    config_name: str
    expected_image: str | None
    expected_flashinfer_arch: str | None
    expected_kv_cache_dtype: str | None
    expected_kv_cache_memory_bytes: str | None
    expected_attention_backend: str | None
    required_patterns: dict[str, str]
    forbidden_patterns: dict[str, str]
    forbidden_docker_env_keys: tuple[str, ...] = ()
    forbidden_extra_arg_options: tuple[str, ...] = ()


BLACKBIRD_QWEN36_FP8_RULE = BackendEvidenceRule(
    config_name="qwen36-27b-fp8-kvfp8-rp6000-blackbird",
    expected_image=BLACKBIRD_QWEN36_IMAGE,
    expected_flashinfer_arch="12.0f",
    expected_kv_cache_dtype="fp8",
    expected_kv_cache_memory_bytes="64424509440",
    expected_attention_backend="FLASHINFER",
    required_patterns={
        "cutlass_fp8": r"Selected CutlassFp8BlockScaledMMKernel",
        "flashinfer_attention": (
            r"Using FLASHINFER attention backend|"
            r"Using AttentionBackendEnum\.FLASHINFER backend"
        ),
    },
    forbidden_patterns={
        "marlin_fallback": r"(Selected|Using).*MARLIN|MARLIN.*fallback|fallback.*MARLIN",
    },
)

BLACKBIRD_QWEN36_BF16_RULE = BackendEvidenceRule(
    config_name="qwen36-27b-bf16-rp6000-blackbird",
    expected_image=BLACKBIRD_QWEN36_IMAGE,
    expected_flashinfer_arch=None,
    expected_kv_cache_dtype="bfloat16",
    expected_kv_cache_memory_bytes=None,
    expected_attention_backend=None,
    required_patterns={},
    forbidden_patterns={},
    forbidden_docker_env_keys=("FLASHINFER_CUDA_ARCH_LIST",),
    forbidden_extra_arg_options=("--kv-cache-memory-bytes",),
)

BLACKBIRD_TINY_RESUME_RULE = BackendEvidenceRule(
    config_name="tiny-random-llama-detached-blackbird",
    expected_image=BLACKBIRD_QWEN36_IMAGE,
    expected_flashinfer_arch=None,
    expected_kv_cache_dtype=None,
    expected_kv_cache_memory_bytes=None,
    expected_attention_backend=None,
    required_patterns={},
    forbidden_patterns={},
)

OXCART_QWEN36_FP8_MTP_VL_RULE = BackendEvidenceRule(
    config_name=OXCART_QWEN36_PROFILE,
    # Oxcart has a stricter host-specific shape check below so its diagnostics
    # cannot be confused with the older Blackbird recipe.
    expected_image=None,
    expected_flashinfer_arch=None,
    expected_kv_cache_dtype="auto",
    expected_kv_cache_memory_bytes=None,
    expected_attention_backend="FLASHINFER",
    required_patterns={
        "cutlass_fp8": r"Selected CutlassFp8BlockScaledMMKernel",
        "flashinfer_attention": (
            r"Using FLASHINFER attention backend|"
            r"Using AttentionBackendEnum\.FLASHINFER backend"
        ),
    },
    forbidden_patterns={
        "marlin_fallback": r"(Selected|Using).*MARLIN|MARLIN.*fallback|fallback.*MARLIN",
    },
    forbidden_docker_env_keys=("FLASHINFER_CUDA_ARCH_LIST",),
    forbidden_extra_arg_options=("--language-model-only", "--kv-cache-memory-bytes"),
)

BACKEND_EVIDENCE_RULES = {
    BLACKBIRD_QWEN36_FP8_RULE.config_name: BLACKBIRD_QWEN36_FP8_RULE,
    BLACKBIRD_QWEN36_BF16_RULE.config_name: BLACKBIRD_QWEN36_BF16_RULE,
    BLACKBIRD_TINY_RESUME_RULE.config_name: BLACKBIRD_TINY_RESUME_RULE,
    OXCART_QWEN36_FP8_MTP_VL_RULE.config_name: OXCART_QWEN36_FP8_MTP_VL_RULE,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate profile-specific vLLM backend evidence from a Vela run log."
    )
    parser.add_argument("config_name")
    parser.add_argument("run_id")
    parser.add_argument("--target", default="local")
    parser.add_argument("--timeout", type=float, default=120.0)
    return parser.parse_args()


def validate_backend_evidence(
    config_name: str,
    config: dict[str, Any],
    log_text: str,
    *,
    identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rule = BACKEND_EVIDENCE_RULES.get(config_name)
    if rule is None:
        if _looks_like_oxcart_config(config_name, config):
            raise BackendEvidenceError(
                f"unregistered backend evidence rule for Oxcart config: {config_name}"
            )
        if _looks_like_blackbird_fp8_config(config) or _looks_like_blackbird_bf16_config(
            config
        ):
            raise BackendEvidenceError(
                f"unregistered backend evidence rule for Blackbird config: {config_name}"
            )
        if _looks_like_unproven_blackbird_bf16_config(config_name, config):
            return _unproven_recipe_result(
                config_name,
                "unproven-bf16-recipe-image",
            )
        if _looks_like_unproven_blackbird_fp8_config(config_name, config):
            return _unproven_recipe_result(
                config_name,
                "unproven-fp8-recipe-anchors",
            )
        return {
            "checked": False,
            "config_name": config_name,
            "reason": "no-backend-evidence-rule",
        }

    run_config_name = str(config.get("name") or "")
    if run_config_name and run_config_name != config_name:
        raise BackendEvidenceError(
            f"backend config name mismatch: expected {config_name}, got {run_config_name}"
        )

    config_errors = _config_shape_errors(config, rule)
    if rule.config_name == OXCART_QWEN36_PROFILE:
        config_errors.extend(_oxcart_config_shape_errors(config, identity=identity))
    required = {
        name: re.search(pattern, log_text, flags=re.IGNORECASE) is not None
        for name, pattern in rule.required_patterns.items()
    }
    forbidden = {
        name: re.search(pattern, log_text, flags=re.IGNORECASE) is not None
        for name, pattern in rule.forbidden_patterns.items()
    }

    missing_required = [name for name, found in required.items() if not found]
    found_forbidden = [name for name, found in forbidden.items() if found]
    if config_errors:
        raise BackendEvidenceError(
            "invalid backend config shape: " + ", ".join(config_errors)
        )
    if missing_required:
        raise BackendEvidenceError(
            "missing required backend evidence: " + ", ".join(missing_required)
        )
    if found_forbidden:
        raise BackendEvidenceError(
            "forbidden backend evidence detected: " + ", ".join(found_forbidden)
        )

    return {
        "checked": True,
        "config_name": config_name,
        "required": required,
        "forbidden": forbidden,
    }


def _config_shape_errors(config: dict[str, Any], rule: BackendEvidenceRule) -> list[str]:
    errors: list[str] = []
    command = _dict(config.get("command"))
    docker = _dict(command.get("docker"))
    engine = _dict(config.get("engine"))
    extra_args = [str(item) for item in config.get("extra_args") or []]

    if command.get("runtime") != "docker":
        errors.append("command.runtime must be docker")
    if rule.expected_image is not None and docker.get("image") != rule.expected_image:
        errors.append("command.docker.image does not match pinned Blackbird image")
    docker_env = _dict(docker.get("env"))
    if (
        rule.expected_flashinfer_arch is not None
        and str(docker_env.get("FLASHINFER_CUDA_ARCH_LIST") or "")
        != rule.expected_flashinfer_arch
    ):
        errors.append("command.docker.env.FLASHINFER_CUDA_ARCH_LIST must be 12.0f")
    for key in rule.forbidden_docker_env_keys:
        if str(docker_env.get(key) or ""):
            errors.append(f"command.docker.env.{key} must be omitted")
    if (
        rule.expected_kv_cache_dtype is not None
        and str(engine.get("kv_cache_dtype") or "").lower()
        != rule.expected_kv_cache_dtype.lower()
    ):
        errors.append(f"engine.kv_cache_dtype must be {rule.expected_kv_cache_dtype}")
    if rule.expected_kv_cache_memory_bytes is not None and not _argv_has_value(
        extra_args,
        "--kv-cache-memory-bytes",
        rule.expected_kv_cache_memory_bytes,
    ):
        errors.append("extra_args must include --kv-cache-memory-bytes 64424509440")
    if rule.expected_attention_backend is not None and not _argv_has_value(
        extra_args,
        "--attention-backend",
        rule.expected_attention_backend,
    ):
        errors.append("extra_args must include --attention-backend FLASHINFER")
    for option in rule.forbidden_extra_arg_options:
        if _argv_has_option(extra_args, option):
            errors.append(f"extra_args must omit {option}")
    return errors


def _looks_like_blackbird_fp8_config(config: dict[str, Any]) -> bool:
    command = _dict(config.get("command"))
    docker = _dict(command.get("docker"))
    engine = _dict(config.get("engine"))
    docker_env = _dict(docker.get("env"))
    return (
        command.get("runtime") == "docker"
        and str(engine.get("kv_cache_dtype") or "").lower() == "fp8"
        and (
            docker.get("image") == BLACKBIRD_QWEN36_IMAGE
            or str(docker_env.get("FLASHINFER_CUDA_ARCH_LIST") or "") == "12.0f"
        )
    )


def _looks_like_oxcart_config(config_name: str, config: dict[str, Any]) -> bool:
    names = (
        config_name,
        str(config.get("name") or ""),
        str(config.get("target") or ""),
    )
    command = _dict(config.get("command"))
    docker = _dict(command.get("docker"))
    return (
        any("oxcart" in name.lower() for name in names)
        and command.get("runtime") == "docker"
        and (
            str(config.get("model") or "") == OXCART_QWEN36_MODEL
            or str(docker.get("container_name") or "").startswith("vela-oxcart-")
        )
    )


def _oxcart_config_shape_errors(
    config: dict[str, Any],
    *,
    identity: dict[str, Any] | None = None,
) -> list[str]:
    errors: list[str] = []
    command = _dict(config.get("command"))
    docker = _dict(command.get("docker"))
    docker_env = _dict(docker.get("env"))
    engine = _dict(config.get("engine"))
    server = _dict(config.get("server"))
    launch = _dict(config.get("launch"))
    logging = _dict(config.get("logging"))
    vllm = _dict(config.get("vllm"))
    extra_args = [str(item) for item in config.get("extra_args") or []]

    if command.get("entrypoint") != "serve":
        errors.append("command.entrypoint must be serve")
    if config.get("target") != "local":
        errors.append("target must be local for Oxcart controller-and-target validation")
    if config.get("model") != OXCART_QWEN36_MODEL:
        errors.append(f"model must be {OXCART_QWEN36_MODEL}")
    errors.extend(_oxcart_model_identity_errors(config, identity))
    if config.get("revision") != OXCART_QWEN36_REVISION:
        errors.append("revision must be the immutable Oxcart model commit")
    if config.get("served_model_name") != "qwen36-27b-fp8-oxcart":
        errors.append("served_model_name must be qwen36-27b-fp8-oxcart")

    if docker.get("image") != BLACKBIRD_QWEN36_IMAGE:
        errors.append("command.docker.image does not match pinned Oxcart image")
    if docker.get("container_name") != OXCART_QWEN36_CONTAINER:
        errors.append("command.docker.container_name must use the validation-only name")
    if list(docker.get("evict") or []):
        errors.append("command.docker.evict must be omitted")
    if docker.get("auto_remove") is not True:
        errors.append("command.docker.auto_remove must be true")
    if list(docker.get("extra_run_args") or []) != list(OXCART_QWEN36_DOCKER_ARGS):
        errors.append(
            "command.docker.extra_run_args must contain only the two ownership labels"
        )
    expected_docker_scalars = {
        "gpus": "all",
        "ipc_host": True,
        "shm_size": "32g",
        "network": "host",
        "restart": "no",
        "stop_grace_seconds": 90,
        "pull": "never",
        "hf_cache": OXCART_QWEN36_HF_CACHE,
        "hf_cache_target": "/root/.cache/huggingface",
    }
    for key, expected in expected_docker_scalars.items():
        if docker.get(key) != expected:
            errors.append(f"command.docker.{key} must be {expected}")
    if tuple(str(item) for item in docker.get("volumes") or []) != OXCART_QWEN36_VOLUMES:
        errors.append("command.docker.volumes do not match the proven Oxcart cache layout")
    for key, expected in OXCART_QWEN36_DOCKER_ENV.items():
        if str(docker_env.get(key) or "") != expected:
            errors.append(f"command.docker.env.{key} must be {expected}")

    expected_engine = {
        "gpu_memory_utilization": 0.955,
        "max_model_len": 262144,
        "dtype": "auto",
        "kv_cache_dtype": "auto",
        "max_num_seqs": 4,
    }
    for key, expected in expected_engine.items():
        if engine.get(key) != expected:
            errors.append(f"engine.{key} must be {expected}")
    if (
        server.get("host") != "127.0.0.1"
        or server.get("port") != 18004
        or server.get("exposure") != "local"
    ):
        errors.append("server must be local-only on 127.0.0.1:18004")
    # Run artifacts intentionally scrub api_key to null; the checked-in profile test
    # separately pins the sentinel value to EMPTY.
    if server.get("api_key") not in {None, "EMPTY"}:
        errors.append("server.api_key must be EMPTY (or scrubbed in the run artifact)")
    if logging.get("request_logging") is not False:
        errors.append("logging.request_logging must be false")
    if _argv_has_option(extra_args, "--language-model-only"):
        errors.append("extra_args must omit --language-model-only")
    if tuple(extra_args) != OXCART_QWEN36_EXTRA_ARGS:
        errors.append("extra_args do not match the proven Oxcart MTP + vision shape")

    if launch.get("mode") != "attached":
        errors.append("launch.mode must be attached")
    if launch.get("ready_timeout_seconds") != 1800:
        errors.append("launch.ready_timeout_seconds must be 1800")
    if launch.get("require_cached_models") is not True:
        errors.append("launch.require_cached_models must be true")
    if launch.get("required_hostname") != "oxcart":
        errors.append("launch.required_hostname must be oxcart")
    health = _dict(launch.get("health"))
    if health.get("interval_seconds") != 2:
        errors.append("launch.health.interval_seconds must be 2")
    if str(launch.get("runs_dir") or "") != OXCART_QWEN36_RUNS_DIR:
        errors.append("launch.runs_dir does not match the Oxcart validation evidence path")

    expected_versions = {
        "version_profile": "current",
        "version": "0.20.2rc1.dev9+g01d4d1ad3",
        "transformers_version": "5.7.0",
        "torch_version": "2.11.0+cu130",
        "cuda_version": "13.0",
    }
    for key, expected in expected_versions.items():
        if vllm.get(key) != expected:
            errors.append(f"vllm.{key} must be {expected}")
    return errors


def _oxcart_model_identity_errors(
    config: dict[str, Any],
    identity: dict[str, Any] | None,
) -> list[str]:
    errors: list[str] = []
    artifact_identity = _dict(identity)
    config_ref = str(config.get("model_ref") or "")
    stable_repo_alias = config_ref == OXCART_QWEN36_MODEL
    concrete_entry_ref = bool(
        config_ref
        and _is_minted_model_entry_id(config_ref)
        and str(artifact_identity.get("model_ref") or "") == config_ref
        and str(artifact_identity.get("model_entry_id") or "") == config_ref
    )
    if not stable_repo_alias and not concrete_entry_ref:
        return ["model_ref must use the stable Oxcart repo-id alias"]

    artifact_ref = str(artifact_identity.get("model_ref") or "")
    if stable_repo_alias and artifact_ref and artifact_ref != OXCART_QWEN36_MODEL:
        errors.append("artifact model_ref does not match the stable Oxcart repo-id alias")

    artifact_repo = str(artifact_identity.get("model_repo_id") or "")
    artifact_commit = str(artifact_identity.get("model_commit_sha") or "")
    if concrete_entry_ref:
        # A wizard persists the registry entry id, not the repo-id alias. Only
        # the target-side sidecar can bind that opaque id back to immutable model
        # identity, so both fields are mandatory for this acceptance path.
        if artifact_repo != OXCART_QWEN36_MODEL:
            errors.append(f"artifact model_repo_id must be {OXCART_QWEN36_MODEL}")
        if artifact_commit != OXCART_QWEN36_REVISION:
            errors.append(
                "artifact model_commit_sha must be the immutable Oxcart model commit"
            )
    else:
        # The checked-in repo-id alias remains self-describing and compatible
        # with older sidecars, but any projected identity that is present must
        # not contradict it.
        if artifact_repo and artifact_repo != OXCART_QWEN36_MODEL:
            errors.append(f"artifact model_repo_id must be {OXCART_QWEN36_MODEL}")
        if artifact_commit and artifact_commit != OXCART_QWEN36_REVISION:
            errors.append(
                "artifact model_commit_sha must be the immutable Oxcart model commit"
            )

    artifact_revision = str(artifact_identity.get("model_revision") or "")
    if artifact_revision and artifact_revision != OXCART_QWEN36_REVISION:
        errors.append("artifact model_revision must be the immutable Oxcart model commit")
    artifact_runtime = str(artifact_identity.get("runtime") or "")
    if artifact_runtime and artifact_runtime != "docker":
        errors.append("artifact runtime must be docker")
    artifact_container = str(artifact_identity.get("docker_container_name") or "")
    if artifact_container and artifact_container != OXCART_QWEN36_CONTAINER:
        errors.append("artifact docker_container_name must use the validation-only name")
    artifact_image = str(artifact_identity.get("docker_image_digest") or "")
    expected_image_digest = BLACKBIRD_QWEN36_IMAGE.split("@", 1)[1]
    if artifact_image and artifact_image != expected_image_digest:
        errors.append("artifact docker_image_digest does not match pinned Oxcart image")
    served_names = artifact_identity.get("served_model_names")
    if isinstance(served_names, list) and served_names:
        rendered_names = [str(item) for item in served_names]
        if "qwen36-27b-fp8-oxcart" not in rendered_names:
            errors.append(
                "artifact served_model_names must include qwen36-27b-fp8-oxcart"
            )
    return errors


def _is_minted_model_entry_id(value: str) -> bool:
    # Vela registry entries are 26-character Crockford-base32 ULIDs. Requiring
    # that concrete shape prevents a display-name alias from becoming trusted
    # merely because two projected fields repeat the same arbitrary string.
    return re.fullmatch(r"[0-7][0-9A-HJKMNPQRSTVWXYZ]{25}", value) is not None


def _looks_like_blackbird_bf16_config(config: dict[str, Any]) -> bool:
    command = _dict(config.get("command"))
    docker = _dict(command.get("docker"))
    engine = _dict(config.get("engine"))
    return (
        command.get("runtime") == "docker"
        and docker.get("image") == BLACKBIRD_QWEN36_IMAGE
        and str(engine.get("kv_cache_dtype") or "").lower() == "bfloat16"
    )


def _looks_like_unproven_blackbird_bf16_config(
    config_name: str,
    config: dict[str, Any],
) -> bool:
    command = _dict(config.get("command"))
    docker = _dict(command.get("docker"))
    engine = _dict(config.get("engine"))
    return (
        _names_blackbird(config_name, config)
        and command.get("runtime") == "docker"
        and str(engine.get("kv_cache_dtype") or "").lower() == "bfloat16"
        and docker.get("image") != BLACKBIRD_QWEN36_IMAGE
    )


def _looks_like_unproven_blackbird_fp8_config(
    config_name: str,
    config: dict[str, Any],
) -> bool:
    command = _dict(config.get("command"))
    docker = _dict(command.get("docker"))
    engine = _dict(config.get("engine"))
    docker_env = _dict(docker.get("env"))
    return (
        _names_blackbird(config_name, config)
        and command.get("runtime") == "docker"
        and str(engine.get("kv_cache_dtype") or "").lower() == "fp8"
        and docker.get("image") != BLACKBIRD_QWEN36_IMAGE
        and str(docker_env.get("FLASHINFER_CUDA_ARCH_LIST") or "") != "12.0f"
    )


def _unproven_recipe_result(config_name: str, reason: str) -> dict[str, Any]:
    if _env_truthy("BACKEND_EVIDENCE_ALLOW_UNPROVEN"):
        return {
            "checked": False,
            "config_name": config_name,
            "reason": reason,
        }
    raise BackendEvidenceError(reason)


def _names_blackbird(config_name: str, config: dict[str, Any]) -> bool:
    names = [
        config_name,
        str(config.get("name") or ""),
        str(config.get("target") or ""),
    ]
    return any("blackbird" in name.lower() for name in names)


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _argv_has_value(argv: list[str], option: str, expected: str) -> bool:
    for index, item in enumerate(argv):
        if item == option and index + 1 < len(argv):
            return argv[index + 1].upper() == expected.upper()
        prefix = option + "="
        if item.startswith(prefix):
            return item[len(prefix) :].upper() == expected.upper()
    return False


def _argv_has_option(argv: list[str], option: str) -> bool:
    prefix = option + "="
    return any(item == option or item.startswith(prefix) for item in argv)


def _dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


async def _run(config_name: str, run_id: str, *, target_name: str, timeout: float) -> int:
    target = load_targets_file().by_name(target_name)
    client = target_client_for_config(target)
    try:
        try:
            artifact = await asyncio.wait_for(
                _connect_and_read_artifact(client, config_name, run_id),
                timeout=timeout,
            )
        except TimeoutError as exc:
            raise BackendEvidenceError(
                f"timed out after {timeout:g} seconds reading run artifact"
            ) from exc
        config = _dict(artifact.get("config"))
        identity = _dict(artifact.get("identity"))
        log_text = str(artifact.get("log_text") or "")
        result = validate_backend_evidence(
            config_name,
            config,
            log_text,
            identity=identity,
        )
    finally:
        await _disconnect_cancellation_safe(client)

    if not result.get("checked"):
        print(
            "BACKEND_EVIDENCE_SKIPPED "
            f"config={config_name} run_id={run_id} reason={result.get('reason')}"
        )
        return 0
    print(f"BACKEND_EVIDENCE_OK config={config_name} run_id={run_id}")
    return 0


async def _connect_and_read_artifact(
    client: Any,
    config_name: str,
    run_id: str,
) -> dict[str, Any]:
    await client.connect()
    return await client.call(
        "read_run_artifact",
        {
            "run_id": run_id,
            "config_name": config_name,
        },
    )


async def _disconnect_cancellation_safe(client: Any) -> None:
    """Finish transport cleanup before propagating an operation cancellation."""

    disconnect_task = asyncio.create_task(client.disconnect())
    try:
        await asyncio.shield(disconnect_task)
    except asyncio.CancelledError:
        await disconnect_task
        raise


def main() -> int:
    args = parse_args()
    try:
        return asyncio.run(
            _run(
                args.config_name,
                args.run_id,
                target_name=args.target,
                timeout=args.timeout,
            )
        )
    except BackendEvidenceError as exc:
        print(
            "BACKEND_EVIDENCE_FAILED "
            f"config={args.config_name} run_id={args.run_id} detail={exc}",
            file=sys.stderr,
        )
        return 2
    except Exception as exc:
        print(
            "BACKEND_EVIDENCE_ERROR "
            f"config={args.config_name} run_id={args.run_id} detail={exc}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
