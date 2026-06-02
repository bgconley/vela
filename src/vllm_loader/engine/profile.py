from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from shutil import which

from vllm_loader.config.schema import EntryPoint, ModelConfig


@dataclass(frozen=True)
class VllmDefaults:
    request_logging: bool | None


@dataclass(frozen=True)
class VllmProfile:
    version: str
    flag_map: dict[str, str]
    defaults: VllmDefaults
    known_kv_cache_dtypes: frozenset[str]
    known_quantizations: frozenset[str]
    known_load_formats: frozenset[str]
    phase_rules: tuple[tuple[re.Pattern[str], str], ...]
    error_rules: tuple[tuple[re.Pattern[str], str], ...]
    progress_re: re.Pattern[str]
    known_flags: frozenset[str]

    def flag_for(self, key: str) -> str | None:
        return self.flag_map.get(key)

    def without_flags(self, *logical_keys: str) -> VllmProfile:
        new_map = {key: value for key, value in self.flag_map.items() if key not in logical_keys}
        return replace(self, flag_map=new_map, known_flags=frozenset(new_map.values()))

    def soft_validate(self, cfg) -> list[str]:
        warnings: list[str] = []
        if cfg.engine.kv_cache_dtype and self.known_kv_cache_dtypes:
            if cfg.engine.kv_cache_dtype not in self.known_kv_cache_dtypes:
                value = cfg.engine.kv_cache_dtype
                warnings.append(f"unknown kv_cache_dtype for profile {self.version}: {value}")
        if cfg.engine.quantization and self.known_quantizations:
            if cfg.engine.quantization not in self.known_quantizations:
                warnings.append(
                    f"unknown quantization for profile {self.version}: {cfg.engine.quantization}"
                )
        if cfg.engine.load_format and self.known_load_formats:
            if cfg.engine.load_format not in self.known_load_formats:
                warnings.append(
                    f"unknown load_format for profile {self.version}: {cfg.engine.load_format}"
                )
        missing = [flag for flag in cfg.vllm.require_flags if flag not in self.known_flags]
        if missing:
            raise VllmProfileError(f"required vLLM flags are unavailable: {', '.join(missing)}")
        return warnings


class VllmProfileError(RuntimeError):
    pass


COMMON_FLAGS = {
    "served_model_name": "--served-model-name",
    "host": "--host",
    "port": "--port",
    "tensor_parallel_size": "--tensor-parallel-size",
    "pipeline_parallel_size": "--pipeline-parallel-size",
    "gpu_memory_utilization": "--gpu-memory-utilization",
    "max_model_len": "--max-model-len",
    "dtype": "--dtype",
    "kv_cache_dtype": "--kv-cache-dtype",
    "quantization": "--quantization",
    "load_format": "--load-format",
    "swap_space": "--swap-space",
    "block_size": "--block-size",
    "seed": "--seed",
    "max_num_seqs": "--max-num-seqs",
    "enforce_eager": "--enforce-eager",
    "enable_request_logging": "--enable-log-requests",
    "disable_request_logging": "--disable-log-requests",
    "disable_access_log_for_endpoints": "--disable-access-log-for-endpoints",
    "max_log_len": "--max-log-len",
}

HELP_FLAG_RE = re.compile(r"(?<![\w-])--[A-Za-z0-9][A-Za-z0-9-]*")


def bundled_profile(name: str = "current") -> VllmProfile:
    if name in {"current", "0.11", "stable"}:
        return _make_profile("0.11", request_logging=False, flags=COMMON_FLAGS)
    if name in {"older-request-logging-on", "0.9"}:
        return _make_profile("0.9", request_logging=True, flags=COMMON_FLAGS)
    if name == "unknown-default":
        return _make_profile("unknown", request_logging=None, flags=COMMON_FLAGS)
    return _make_profile(name, request_logging=False, flags=COMMON_FLAGS)


def select_profile(version_profile: str | None = None, executable: str = "vllm") -> VllmProfile:
    if version_profile:
        return _with_collected_help_flags(bundled_profile(version_profile), executable)
    version = detect_vllm_version(executable)
    if version and version.startswith("0.9"):
        return _with_collected_help_flags(bundled_profile("older-request-logging-on"), executable)
    return _with_collected_help_flags(bundled_profile("current"), executable)


def select_profile_for_config(cfg: ModelConfig) -> VllmProfile:
    executable = "vllm"
    if (
        cfg.command.entrypoint is EntryPoint.SERVE
        and cfg.command.executable
        and _looks_like_vllm_executable(cfg.command.executable)
    ):
        executable = cfg.command.executable or "vllm"
    return select_profile(cfg.vllm.version_profile, executable=executable)


def _looks_like_vllm_executable(executable: str) -> bool:
    name = Path(executable).name.lower()
    return name in {"vllm", "vllm.exe"} or name.startswith("vllm-") or name.endswith("-vllm")


def _with_collected_help_flags(profile: VllmProfile, executable: str) -> VllmProfile:
    help_flags = frozenset(HELP_FLAG_RE.findall(collect_serve_help(executable)))
    if not help_flags or not {"--host", "--port"}.issubset(help_flags):
        return profile
    return replace(
        profile,
        flag_map={key: flag for key, flag in profile.flag_map.items() if flag in help_flags},
        known_flags=help_flags,
    )


@lru_cache(maxsize=8)
def detect_vllm_version(executable: str = "vllm") -> str | None:
    if which(executable) is None:
        return None
    for args in ([executable, "--version"], [executable, "serve", "--help"]):
        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=5, check=False)
        except Exception:
            continue
        text = (proc.stdout or "") + (proc.stderr or "")
        match = re.search(r"vllm(?:\s+version)?\s+([0-9][^\s]+)", text, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


@lru_cache(maxsize=8)
def collect_serve_help(executable: str = "vllm") -> str:
    if which(executable) is None:
        return ""
    for args, timeout in (
        ([executable, "serve", "--help=all"], 20),
        ([executable, "serve", "--help"], 8),
    ):
        try:
            proc = subprocess.run(
                args, capture_output=True, text=True, timeout=timeout, check=False
            )
        except Exception:
            continue
        text = (proc.stdout or "") + (proc.stderr or "")
        if text:
            return text
    return ""


def _make_profile(version: str, request_logging: bool | None, flags: dict[str, str]) -> VllmProfile:
    phase_rules = (
        (re.compile(r"Initializing a .*LLM engine|world_size=", re.I), "STARTING"),
        (
            re.compile(
                r"Fetching \d+ files|[Rr]esolv(?:e|ing) .*model|cache miss|snapshot metadata"
            ),
            "RESOLVING_MODEL",
        ),
        (re.compile(r"[Dd]ownloading|hf_transfer"), "DOWNLOADING_MODEL"),
        (re.compile(r"Starting to load model|Loading .*checkpoint", re.I), "LOADING_WEIGHTS"),
        (re.compile(r"GPU KV cache size|# GPU blocks|Maximum concurrency", re.I), "PROFILING_KV"),
        (re.compile(r"Capturing (?:CUDA )?graph", re.I), "CAPTURING_GRAPHS"),
        (
            re.compile(r"(?:Uvicorn running|Starting vLLM server) on (https?://\S+)", re.I),
            "SERVER_STARTING",
        ),
    )
    error_rules = (
        (re.compile(r"CUDA out of memory|OutOfMemoryError", re.I), "OOM"),
        (re.compile(r"address already in use|Errno 98", re.I), "PORT_IN_USE"),
        (
            re.compile(
                r"RepositoryNotFoundError|EntryNotFoundError|does not exist|No such file", re.I
            ),
            "MODEL_NOT_FOUND",
        ),
        (
            re.compile(r"tensor_parallel|pipeline_parallel|world_size|TP.*mismatch", re.I),
            "TP_MISMATCH",
        ),
        (re.compile(r"GatedRepoError|Cannot access gated repo|401 Client Error", re.I), "HF_AUTH"),
    )
    known_kv = frozenset(
        {
            "auto",
            "bfloat16",
            "float16",
            "fp8",
            "fp8_ds_mla",
            "fp8_e4m3",
            "fp8_e5m2",
            "fp8_inc",
            "fp8_per_token_head",
            "int8_per_token_head",
            "nvfp4",
        }
    )
    return VllmProfile(
        version=version,
        flag_map=dict(flags),
        defaults=VllmDefaults(request_logging=request_logging),
        known_kv_cache_dtypes=known_kv,
        known_quantizations=frozenset({"awq", "gptq", "fp8", "bitsandbytes", "marlin"}),
        known_load_formats=frozenset(
            {"auto", "pt", "safetensors", "npcache", "dummy", "tensorizer"}
        ),
        phase_rules=phase_rules,
        error_rules=error_rules,
        progress_re=re.compile(r"(?P<label>.*?)(?P<pct>\d{1,3})%.*?(?P<done>\d+)/(?P<total>\d+)"),
        known_flags=frozenset(flags.values()),
    )
