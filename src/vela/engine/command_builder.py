from __future__ import annotations

import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vela.config.schema import EntryPoint, ModelConfig
from vela.engine.profile import VllmProfile, select_profile_for_config
from vela.engine.redaction import MASK, scrub_text


@dataclass(frozen=True)
class CommandBuildResult:
    argv: list[str]
    env: dict[str, str]
    cwd: Path
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    preview: str = ""


ENGINE_VALUE_FIELDS = (
    "tensor_parallel_size",
    "pipeline_parallel_size",
    "gpu_memory_utilization",
    "max_model_len",
    "dtype",
    "kv_cache_dtype",
    "quantization",
    "load_format",
    "swap_space",
    "block_size",
    "seed",
    "max_num_seqs",
)


def build_command(
    cfg: ModelConfig, profile: VllmProfile | None = None, *, cwd: Path | None = None
) -> CommandBuildResult:
    profile = profile or select_profile_for_config(cfg)
    cwd = _command_cwd(cfg, cwd)
    warnings = list(profile.soft_validate(cfg))
    argv = _base_argv(cfg)

    if cfg.revision is not None:
        _append_value(argv, profile, "revision", cfg.revision)
    _append_value(argv, profile, "served_model_name", cfg.served_model_name)
    _append_value(argv, profile, "host", cfg.server.host)
    _append_value(argv, profile, "port", cfg.server.port)

    for field_name in ENGINE_VALUE_FIELDS:
        value = getattr(cfg.engine, field_name)
        if value is not None:
            _append_value(argv, profile, field_name, value)

    if cfg.engine.enforce_eager is True:
        _append_flag(argv, profile, "enforce_eager")

    argv.extend(_request_logging_flags(cfg, profile, warnings))
    if cfg.logging.suppress_access_log_for:
        flag = profile.flag_for("disable_access_log_for_endpoints")
        if flag:
            argv.extend([flag, ",".join(cfg.logging.suppress_access_log_for)])
        else:
            warnings.append("profile cannot suppress targeted access logs")
    if cfg.logging.max_log_len is not None:
        _append_value(argv, profile, "max_log_len", cfg.logging.max_log_len)

    argv.extend(cfg.extra_args)

    env = {"PYTHONUNBUFFERED": "1", **cfg.env}
    if cfg.server.api_key:
        env["VLLM_API_KEY"] = cfg.server.api_key

    warnings.extend(_network_warnings(cfg))
    preview = render_preview(argv, env, cwd)
    metadata = {
        "vllm_version_profile": cfg.vllm.version_profile or profile.version,
        "known_flags": sorted(profile.known_flags),
        "flag_map": dict(sorted(profile.flag_map.items())),
    }
    return CommandBuildResult(
        argv=argv,
        env=env,
        cwd=cwd,
        warnings=warnings,
        metadata=metadata,
        preview=preview,
    )


def _command_cwd(cfg: ModelConfig, cwd: Path | None) -> Path:
    path = Path(cwd).expanduser() if cwd is not None else cfg.command.cwd
    if path is None:
        return Path.cwd()
    path = Path(path).expanduser()
    return path if path.is_absolute() else Path.cwd() / path


def _base_argv(cfg: ModelConfig) -> list[str]:
    if cfg.command.entrypoint is EntryPoint.MODULE:
        executable = cfg.command.executable or sys.executable
        return [executable, "-m", "vllm.entrypoints.openai.api_server", "--model", cfg.model]
    executable = cfg.command.executable or "vllm"
    return [executable, "serve", cfg.model]


def _append_value(argv: list[str], profile: VllmProfile, key: str, value: object) -> None:
    flag = profile.flag_for(key)
    if flag:
        argv.extend([flag, str(value)])


def _append_flag(argv: list[str], profile: VllmProfile, key: str) -> None:
    flag = profile.flag_for(key)
    if flag:
        argv.append(flag)


def _request_logging_flags(
    cfg: ModelConfig, profile: VllmProfile, warnings: list[str]
) -> list[str]:
    desired = cfg.logging.request_logging
    default = profile.defaults.request_logging
    if desired == default:
        return []
    key = "enable_request_logging" if desired else "disable_request_logging"
    flag = profile.flag_for(key)
    if flag:
        return [flag]
    warnings.append("request logging policy could not be enforced for this vLLM profile")
    return []


def _network_warnings(cfg: ModelConfig) -> list[str]:
    if cfg.server.host in {"127.0.0.1", "localhost", "::1"}:
        return []
    return [
        (
            f"Binds vLLM to {cfg.server.host}, reachable beyond localhost. "
            "`--api-key` does not protect all endpoints, including `/invocations`; "
            "put it behind a reverse proxy or firewall."
        )
    ]


def is_local_model_reference(model: str, *, cwd: str | Path | None = None) -> bool:
    if model.startswith(("/", "./", "../", "~")):
        return True
    base = Path(cwd or Path.cwd())
    return (base / model).exists()


def mask_preview_value(key: str, value: str) -> str:
    upper = key.upper()
    if "TOKEN" in upper or "KEY" in upper or upper in {"AUTHORIZATION"}:
        return MASK
    return scrub_text(value)


def render_preview(argv: list[str], env: dict[str, str], cwd: Path) -> str:
    env_text = " ".join(
        f"{key}={shlex.quote(mask_preview_value(key, value))}" for key, value in sorted(env.items())
    )
    argv_text = " ".join(shlex.quote(scrub_text(part)) for part in argv)
    if env_text:
        return f"cwd={cwd}\n{env_text} {argv_text}"
    return f"cwd={cwd}\n{argv_text}"
