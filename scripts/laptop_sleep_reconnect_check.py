#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import contextlib
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vllm_loader.config.targets import load_targets_file
from vllm_loader.transport.client import TargetClient
from vllm_loader.transport.factory import target_client_for_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run an operator-gated physical controller sleep reconnect validation."
        )
    )
    parser.add_argument("config_name")
    parser.add_argument("--target", default="local")
    parser.add_argument("--build")
    parser.add_argument("--model-ref")
    parser.add_argument("--revision")
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument("--artifact-name")
    parser.add_argument(
        "--leave-running",
        action="store_true",
        help="Do not stop the launched run after reconnect validation.",
    )
    return parser.parse_args()


def _new_client(target_name: str) -> TargetClient:
    target = load_targets_file().by_name(target_name)
    return target_client_for_config(target)


def _launch_params(
    config_name: str,
    *,
    build: str | None,
    model_ref: str | None,
    revision: str | None,
) -> dict[str, Any]:
    params: dict[str, Any] = {"name": config_name}
    if build:
        params["build"] = build
    if model_ref:
        params["model_ref"] = model_ref
    if revision:
        params["revision"] = revision
    return params


def _runs_dirs_from_prepared(prepared: dict[str, Any]) -> list[str]:
    config = prepared.get("config")
    if not isinstance(config, dict):
        return []
    launch = config.get("launch")
    if not isinstance(launch, dict):
        return []
    runs_dir = launch.get("runs_dir")
    return [str(runs_dir)] if runs_dir else []


def _require_detached_real_config(prepared: dict[str, Any]) -> None:
    config = prepared.get("config")
    if not isinstance(config, dict):
        return
    model = str(config.get("model") or "")
    command = config.get("command")
    executable = ""
    if isinstance(command, dict):
        executable = str(command.get("executable") or "")
    launch = config.get("launch")
    mode = str(launch.get("mode") or "") if isinstance(launch, dict) else ""
    if mode != "detached":
        raise RuntimeError("laptop sleep validation requires a detached config")
    if model == "fake/model" or executable.endswith("fake_vllm_child.py"):
        raise RuntimeError("laptop sleep validation requires a real model config")


async def _wait_ready(
    client: TargetClient,
    run_id: str,
    *,
    timeout: float,
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    last: dict[str, Any] = {}
    while asyncio.get_running_loop().time() < deadline:
        last = await client.call("health", {"run_id": run_id})
        if last.get("ready"):
            return last
        if last.get("error_kind") or last.get("phase") in {"ERROR", "STOPPED"}:
            raise RuntimeError(f"run {run_id} did not become ready: {last}")
        await asyncio.sleep(2.0)
    raise RuntimeError(f"run {run_id} did not become ready: {last}")


async def _next_log_with_cursor(events, *, timeout: float) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise RuntimeError("timed out waiting for a log cursor")
        event = await asyncio.wait_for(events.__anext__(), timeout=remaining)
        if event.get("event") != "log":
            continue
        if "log_inode" in event and "byte_offset" in event:
            return event


async def _collect_resume_logs(
    events,
    *,
    first_text: str,
    timeout: float,
) -> list[str]:
    deadline = asyncio.get_running_loop().time() + timeout
    logs: list[str] = []
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise RuntimeError("timed out waiting for replayed sleep-gap logs")
        event = await asyncio.wait_for(events.__anext__(), timeout=remaining)
        if event.get("event") != "log":
            continue
        text = str(event.get("text") or "")
        if text == first_text:
            raise RuntimeError("resume replay duplicated the pre-sleep cursor")
        logs.append(text)
        return logs


async def _close_events(events) -> None:
    if events is None:
        return
    with contextlib.suppress(Exception):
        await events.aclose()


def _operator_pause(run_id: str) -> None:
    print(
        "LAPTOP_SLEEP_RECONNECT_READY "
        f"run_id={run_id} "
        "sleep this controller now, wake it, then press Enter to reconnect.",
        flush=True,
    )
    input("Press Enter after the physical sleep/wake cycle has completed: ")


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git_head() -> tuple[str, str]:
    try:
        short = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        full = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        return short, full
    except Exception:
        return "unknown", "unknown"


def _artifact_path(
    artifact_dir: Path | None,
    *,
    artifact_name: str | None,
    target_name: str,
    config_name: str,
) -> Path | None:
    if artifact_dir is None:
        return None
    artifact_dir.mkdir(parents=True, exist_ok=True)
    if artifact_name:
        return artifact_dir / artifact_name
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    safe_target = _slug(target_name)
    safe_config = _slug(config_name)
    return artifact_dir / f"{stamp}-{safe_target}-{safe_config}-laptop-sleep.md"


def _slug(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in value)
    return cleaned.strip("-") or "validation"


def _write_artifact(path: Path | None, lines: list[str]) -> None:
    if path is None:
        return
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"laptop sleep validation artifact: {path}", file=sys.stderr)


async def _run(
    config_name: str,
    *,
    target_name: str,
    timeout: float,
    build: str | None,
    model_ref: str | None,
    revision: str | None,
    artifact_dir: Path | None,
    artifact_name: str | None,
    leave_running: bool,
) -> None:
    started = _utc_now()
    head_short, head_full = _git_head()
    run_id = f"laptop-sleep-{uuid.uuid4().hex}"
    base_params = _launch_params(
        config_name,
        build=build,
        model_ref=model_ref,
        revision=revision,
    )
    artifact = _artifact_path(
        artifact_dir,
        artifact_name=artifact_name,
        target_name=target_name,
        config_name=config_name,
    )
    events = None
    tail_task = None
    client = _new_client(target_name)
    await client.connect()
    try:
        prepared = await client.call("prepare_launch", base_params)
        _require_detached_real_config(prepared)
        runs_dirs = _runs_dirs_from_prepared(prepared)
        await client.call("launch", {**base_params, "run_id": run_id})
        events = client.subscribe([run_id], resume_from="start")
        tail_task = asyncio.create_task(
            client.call(
                "tail_detached",
                {
                    "run_id": run_id,
                    "start_position": 0,
                    "poll_interval": 0.25,
                },
            )
        )
        first_log = await _next_log_with_cursor(
            events,
            timeout=max(30.0, min(timeout / 4.0, 180.0)),
        )
        first_text = str(first_log.get("text") or "")
        await asyncio.to_thread(_operator_pause, run_id)
    finally:
        await _close_events(events)
        if client.connected:
            with contextlib.suppress(Exception):
                await client.disconnect()
        if tail_task is not None:
            with contextlib.suppress(Exception):
                await tail_task

    cursor = {
        "log_inode": int(first_log["log_inode"]),
        "byte_offset": int(first_log["byte_offset"]),
    }
    client = _new_client(target_name)
    events = None
    waited: dict[str, Any] = {}
    await client.connect()
    try:
        discover_params = {"runs_dirs": runs_dirs} if runs_dirs else {}
        discovered = await client.call("discover_runs", discover_params)
        discovered_ids = {
            str(run.get("run_id"))
            for run in discovered.get("runs", [])
            if isinstance(run, dict)
        }
        if run_id not in discovered_ids:
            raise RuntimeError(f"run {run_id} not rediscovered after sleep: {discovered}")
        await client.call("reattach", {"run_id": run_id})
        events = client.subscribe([run_id], resume_from=cursor)
        resumed_logs = await _collect_resume_logs(
            events,
            first_text=first_text,
            timeout=max(30.0, min(timeout / 4.0, 180.0)),
        )
        health = await _wait_ready(client, run_id, timeout=timeout)
        if not leave_running:
            await client.call(
                "stop",
                {
                    "run_id": run_id,
                    "interrupt_timeout": 5,
                    "terminate_timeout": 5,
                },
            )
            waited = await client.call("wait", {"run_id": run_id})
    finally:
        await _close_events(events)
        if client.connected:
            await client.disconnect()

    marker = (
        "LAPTOP_SLEEP_RECONNECT_OK "
        f"run_id={run_id} "
        f"logs={len(resumed_logs)} "
        f"resume_inode={cursor['log_inode']} "
        f"resume_offset={cursor['byte_offset']} "
        f"url={health.get('reachable_url')} "
        f"returncode={waited.get('returncode', 'left-running')}"
    )
    print(marker)
    completed = _utc_now()
    _write_artifact(
        artifact,
        [
            "# vLLM Loader Laptop Sleep Validation",
            "",
            f"- Started: `{started}`",
            f"- Completed: `{completed}`",
            f"- Local commit: `{head_short}` (`{head_full}`)",
            f"- Target: `{target_name}`",
            f"- Config: `{config_name}`",
            f"- Run id: `{run_id}`",
            f"- Resume cursor: `inode={cursor['log_inode']} offset={cursor['byte_offset']}`",
            f"- Health URL: `{health.get('reachable_url')}`",
            f"- Returncode: `{waited.get('returncode', 'left-running')}`",
            "",
            "## Result",
            "",
            f"`{marker}`",
        ],
    )


def main() -> None:
    args = parse_args()
    asyncio.run(
        _run(
            args.config_name,
            target_name=args.target,
            timeout=args.timeout,
            build=args.build,
            model_ref=args.model_ref,
            revision=args.revision,
            artifact_dir=args.artifact_dir,
            artifact_name=args.artifact_name,
            leave_running=args.leave_running,
        )
    )


if __name__ == "__main__":
    main()
