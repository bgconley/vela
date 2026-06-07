#!/usr/bin/env python3

import argparse
import asyncio
import contextlib
import re
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from vela.config.targets import load_targets_file
from vela.transport.client import TargetClient
from vela.transport.factory import target_client_for_config

BLACKBIRD_QWEN36_IMAGE = (
    "vllm/vllm-openai@sha256:"
    "b13d6e5fda0785f3d41752df8513ff832f67cb231a216c76b6b4f2a515bf0046"
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

BACKEND_EVIDENCE_RULES = {
    BLACKBIRD_QWEN36_FP8_RULE.config_name: BLACKBIRD_QWEN36_FP8_RULE,
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
) -> dict[str, Any]:
    rule = BACKEND_EVIDENCE_RULES.get(config_name)
    if rule is None:
        if _looks_like_blackbird_fp8_config(config):
            raise BackendEvidenceError(
                f"unregistered backend evidence rule for Blackbird FP8 config: {config_name}"
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
    if (
        rule.expected_kv_cache_dtype is not None
        and str(engine.get("kv_cache_dtype") or "").lower()
        != rule.expected_kv_cache_dtype.lower()
    ):
        errors.append("engine.kv_cache_dtype must be fp8")
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


def _argv_has_value(argv: list[str], option: str, expected: str) -> bool:
    for index, item in enumerate(argv):
        if item == option and index + 1 < len(argv):
            return argv[index + 1].upper() == expected.upper()
        prefix = option + "="
        if item.startswith(prefix):
            return item[len(prefix) :].upper() == expected.upper()
    return False


def _dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


async def _collect_log_text(
    client: TargetClient,
    run_id: str,
    *,
    timeout: float,
) -> str:
    events = client.subscribe([run_id], resume_from="live")
    tail_task = asyncio.create_task(
        client.call(
            "tail_detached",
            {
                "run_id": run_id,
                "start_position": 0,
                "poll_interval": 0.05,
            },
        )
    )
    lines: list[str] = []
    try:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            if tail_task.done():
                await _drain_log_events(events, lines)
                await tail_task
                break
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                tail_task.cancel()
                raise RuntimeError(f"timed out collecting backend evidence for {run_id}")
            try:
                event = await asyncio.wait_for(
                    events.__anext__(),
                    timeout=min(0.5, remaining),
                )
            except asyncio.TimeoutError:
                continue
            _append_log_event(lines, event)
    finally:
        with contextlib.suppress(Exception):
            await events.aclose()
        if not tail_task.done():
            tail_task.cancel()
            with contextlib.suppress(Exception):
                await tail_task
    return "\n".join(lines)


async def _drain_log_events(
    events: AsyncIterator[dict[str, Any]],
    lines: list[str],
) -> None:
    while True:
        try:
            event = await asyncio.wait_for(events.__anext__(), timeout=0.1)
        except asyncio.TimeoutError:
            return
        _append_log_event(lines, event)


def _append_log_event(lines: list[str], event: dict[str, Any]) -> None:
    if event.get("event") != "log":
        return
    text = str(event.get("text") or "")
    if text:
        lines.append(text)


async def _run(config_name: str, run_id: str, *, target_name: str, timeout: float) -> int:
    target = load_targets_file().by_name(target_name)
    client = target_client_for_config(target)
    await client.connect()
    try:
        status = await client.call("reattach", {"run_id": run_id})
        config = _dict(status.get("config"))
        log_text = await _collect_log_text(client, run_id, timeout=timeout)
        result = validate_backend_evidence(config_name, config, log_text)
    finally:
        await client.disconnect()

    if not result.get("checked"):
        print(
            "BACKEND_EVIDENCE_SKIPPED "
            f"config={config_name} run_id={run_id} reason={result.get('reason')}"
        )
        return 0
    print(f"BACKEND_EVIDENCE_OK config={config_name} run_id={run_id}")
    return 0


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
