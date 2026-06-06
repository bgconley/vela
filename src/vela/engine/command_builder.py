from __future__ import annotations

import re
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vela.config.schema import EntryPoint, ModelConfig, RuntimeKind
from vela.engine.docker_runtime import build_docker_run
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

SECRET_ENV_MARKERS = (
    "TOKEN",
    "KEY",
    "SECRET",
    "AUTH",
    "PASSWORD",
    "PASS",
    "CREDENTIAL",
)
NON_SECRET_SENTINELS = {"", "EMPTY"}
SHELL_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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

    metadata = {
        "vllm_version_profile": cfg.vllm.version_profile or profile.version,
        "known_flags": sorted(profile.known_flags),
        "flag_map": dict(sorted(profile.flag_map.items())),
    }
    if cfg.command.runtime is RuntimeKind.DOCKER:
        warnings.extend(_docker_warnings(cfg))
        docker_run = build_docker_run(cfg, argv[1:], env)
        argv = docker_run.argv
        env = docker_run.env
        metadata.update(docker_run.metadata)

    warnings.extend(_network_warnings(cfg))
    preview = render_preview(argv, env, cwd)
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


def _docker_warnings(cfg: ModelConfig) -> list[str]:
    docker = cfg.command.docker
    if cfg.command.runtime is not RuntimeKind.DOCKER or docker is None:
        return []
    warnings: list[str] = []
    if docker.gpus is not None and not str(docker.gpus).strip():
        warnings.append(
            "command.docker.gpus is blank; Docker will launch without an explicit GPU reservation"
        )
    return warnings


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


def render_standalone_docker_script(result: CommandBuildResult, *, name: str | None = None) -> str:
    if result.metadata.get("runtime") != "docker":
        raise ValueError("standalone docker export requires a docker runtime command")

    title = name or str(result.metadata.get("docker_container_name") or "vela-docker-run")
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        f"# Generated by Vela for {title}.",
        "# Secret values are intentionally omitted; set them in the environment before running.",
        f"cd {shlex.quote(str(result.cwd))}",
        "",
    ]
    env_lines = _standalone_env_lines(result.env)
    if env_lines:
        lines.extend(env_lines)
        lines.append("")
    lines.append(_standalone_exec_line(result.argv))
    lines.append("")
    return "\n".join(lines)


def _standalone_env_lines(env: dict[str, str]) -> list[str]:
    lines: list[str] = []
    for key in sorted(env):
        value = str(env[key])
        if _is_secret_env_value(key, value):
            if SHELL_IDENTIFIER_RE.match(key):
                lines.append(f': "${{{key}:?Set {key} before running}}"')
                lines.append(f"export {key}")
            else:
                quoted_key = shlex.quote(key)
                lines.append(f"# Set secret environment variable {quoted_key} before running.")
            continue
        if SHELL_IDENTIFIER_RE.match(key):
            lines.append(f"export {key}={shlex.quote(value)}")
        else:
            lines.append(f"# Unsupported environment variable name omitted: {shlex.quote(key)}")
    return lines


def _is_secret_env_value(key: str, value: str) -> bool:
    if value in NON_SECRET_SENTINELS:
        return False
    upper = key.upper()
    return any(marker in upper for marker in SECRET_ENV_MARKERS)


def _standalone_exec_line(argv: list[str]) -> str:
    redacted = [shlex.quote(_redact_standalone_arg(part)) for part in argv]
    if len(redacted) >= 2:
        head = "exec " + " ".join(redacted[:2])
        tail = redacted[2:]
        if tail:
            return head + " \\\n  " + " \\\n  ".join(tail)
        return head
    return "exec " + " \\\n  ".join(redacted)


def _redact_standalone_arg(part: str) -> str:
    return scrub_text(str(part)).replace(MASK, "REDACTED")
