#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import contextlib
import subprocess
import sys
import uuid
from typing import Any

from vela.config.targets import TargetConfig, TransportKind, load_targets_file
from vela.transport.client import TargetClient
from vela.transport.factory import target_client_for_config


class _CleanupContext:
    def __init__(self) -> None:
        self.runs_dirs: list[str] = []
        self.launch_attempted = False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run real-model disconnect, resume, and recovery validation."
    )
    parser.add_argument("config_name")
    parser.add_argument("--target", default="local")
    parser.add_argument(
        "--configs-dir",
        help="Target config directory override (isolates the resume config).",
    )
    parser.add_argument("--build")
    parser.add_argument("--model-ref")
    parser.add_argument("--revision")
    parser.add_argument("--timeout", type=float, default=1800.0)
    return parser.parse_args()


def _new_client(target_name: str) -> tuple[TargetConfig, TargetClient]:
    target = load_targets_file().by_name(target_name)
    return target, target_client_for_config(target)


def _runs_dirs_from_prepared(prepared: dict[str, Any]) -> list[str]:
    config = prepared.get("config")
    if not isinstance(config, dict):
        return []
    launch = config.get("launch")
    if not isinstance(launch, dict):
        return []
    runs_dir = launch.get("runs_dir")
    return [str(runs_dir)] if runs_dir else []


def _reject_fake_config(prepared: dict[str, Any]) -> None:
    config = prepared.get("config")
    if not isinstance(config, dict):
        return
    model = str(config.get("model") or "")
    command = config.get("command")
    executable = ""
    if isinstance(command, dict):
        executable = str(command.get("executable") or "")
    if model == "fake/model" or executable.endswith("fake_vllm_child.py"):
        raise RuntimeError(
            "real resume validation requires a real model config, not fake-child"
        )


def _launch_params(
    config_name: str,
    *,
    build: str | None,
    model_ref: str | None,
    revision: str | None,
    configs_dir: str | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {"name": config_name}
    if build:
        params["build"] = build
    if model_ref:
        params["model_ref"] = model_ref
    if revision:
        params["revision"] = revision
    if configs_dir:
        params["configs_dir"] = configs_dir
    return params


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
            raise RuntimeError("timed out waiting for replayed real-model logs")
        event = await asyncio.wait_for(events.__anext__(), timeout=remaining)
        if event.get("event") != "log":
            continue
        text = str(event.get("text") or "")
        if text == first_text:
            raise RuntimeError("resume replay duplicated the pre-disconnect cursor")
        logs.append(text)
        return logs


async def _close_events(events) -> None:
    if events is None:
        return
    with contextlib.suppress(Exception):
        await events.aclose()


def _restart_target_agent(target: TargetConfig) -> str:
    if target.transport is TransportKind.SSH:
        # An SSH target creates one `vela agent connect` process per client.
        # Recreating the client below is the recovery boundary. Restarting a
        # host-level daemon here would disrupt unrelated runs on a shared GPU.
        return "ssh-reconnect"
    subprocess.run(
        [sys.executable, "-m", "vela.cli", "agent", "restart"],
        check=True,
    )
    return "local-daemon-restart"


async def _run_validation(
    config_name: str,
    *,
    run_id: str,
    cleanup_context: _CleanupContext,
    target_name: str,
    timeout: float,
    build: str | None,
    model_ref: str | None,
    revision: str | None,
    configs_dir: str | None = None,
) -> None:
    target, client = _new_client(target_name)
    base_params = _launch_params(
        config_name,
        build=build,
        model_ref=model_ref,
        revision=revision,
        configs_dir=configs_dir,
    )
    events = None
    tail_task = None
    await client.connect()
    try:
        prepared = await client.call("prepare_launch", base_params)
        _reject_fake_config(prepared)
        runs_dirs = _runs_dirs_from_prepared(prepared)
        cleanup_context.runs_dirs = list(runs_dirs)
        # Set this before awaiting the launch response: the target may own the
        # run even if the response is lost or the controller is cancelled.
        cleanup_context.launch_attempted = True
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
    finally:
        await _close_events(events)
        await client.disconnect()
        if tail_task is not None:
            with contextlib.suppress(Exception):
                await tail_task

    await asyncio.sleep(5.0)

    _, client = _new_client(target_name)
    events = None
    await client.connect()
    try:
        discover_params = {"runs_dirs": runs_dirs} if runs_dirs else {}
        await client.call("discover_runs", discover_params)
        await client.call("reattach", {"run_id": run_id})
        cursor = {
            "log_inode": int(first_log["log_inode"]),
            "byte_offset": int(first_log["byte_offset"]),
        }
        events = client.subscribe([run_id], resume_from=cursor)
        resumed_logs = await _collect_resume_logs(
            events,
            first_text=first_text,
            timeout=max(30.0, min(timeout / 4.0, 180.0)),
        )
        health = await _wait_ready(client, run_id, timeout=timeout)
    finally:
        await _close_events(events)
        await client.disconnect()

    print(
        "REAL_MODEL_RESUME_OK "
        f"run_id={run_id} logs={len(resumed_logs)} "
        f"resume_inode={first_log['log_inode']} "
        f"resume_offset={first_log['byte_offset']} "
        f"url={health.get('reachable_url')}"
    )

    recovery_mode = _restart_target_agent(target)

    _, client = _new_client(target_name)
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
            raise RuntimeError(
                f"run {run_id} not rediscovered after {recovery_mode}: {discovered}"
            )
        await client.call("reattach", {"run_id": run_id})
        health_after_restart = await _wait_ready(client, run_id, timeout=120.0)
        await client.call(
            "stop",
            {
                "run_id": run_id,
                "interrupt_timeout": 2,
                "terminate_timeout": 2,
            },
        )
        waited = await client.call("wait", {"run_id": run_id})
    finally:
        if client.connected:
            with contextlib.suppress(Exception):
                await client.disconnect()

    print(
        "REAL_MODEL_RECOVERY_OK "
        f"mode={recovery_mode} run_id={run_id} "
        f"url={health_after_restart.get('reachable_url')} "
        f"returncode={waited.get('returncode')}"
    )


async def _cleanup_failed_run(
    target_name: str,
    run_id: str,
    *,
    runs_dirs: list[str],
    launch_attempted: bool,
) -> str:
    """Best-effort, identity-safe cleanup for a run owned by this probe."""
    if not launch_attempted:
        return "not-launched"
    client: TargetClient | None = None
    try:
        _, client = _new_client(target_name)
        await client.connect()
        discover_params = {"runs_dirs": list(runs_dirs)} if runs_dirs else {}
        discovered = await client.call("discover_runs", discover_params)
        discovered_ids = {
            str(run.get("run_id"))
            for run in discovered.get("runs", [])
            if isinstance(run, dict)
        }
        if run_id not in discovered_ids:
            # Absence is not proof of cleanup once launch was attempted: a
            # custom runs_dir may be temporarily unavailable or discovery may
            # lag a target that accepted the launch.
            return "not-found-after-launch"
        await client.call("reattach", {"run_id": run_id})
        await client.call(
            "stop",
            {
                "run_id": run_id,
                "interrupt_timeout": 2,
                "terminate_timeout": 2,
            },
        )
        waited = await client.call("wait", {"run_id": run_id})
        return f"stopped:returncode={waited.get('returncode')}"
    except Exception as exc:
        return f"cleanup-failed:{type(exc).__name__}"
    finally:
        if client is not None and client.connected:
            with contextlib.suppress(Exception):
                await client.disconnect()


async def _run(
    config_name: str,
    *,
    target_name: str,
    timeout: float,
    build: str | None,
    model_ref: str | None,
    revision: str | None,
    configs_dir: str | None = None,
) -> None:
    run_id = f"real-resume-{uuid.uuid4().hex}"
    cleanup_context = _CleanupContext()
    completed = False
    try:
        await _run_validation(
            config_name,
            run_id=run_id,
            cleanup_context=cleanup_context,
            target_name=target_name,
            timeout=timeout,
            build=build,
            model_ref=model_ref,
            revision=revision,
            configs_dir=configs_dir,
        )
        completed = True
    finally:
        if not completed:
            result = await asyncio.shield(
                _cleanup_failed_run(
                    target_name,
                    run_id,
                    runs_dirs=cleanup_context.runs_dirs,
                    launch_attempted=cleanup_context.launch_attempted,
                )
            )
            marker = (
                "REAL_MODEL_CLEANUP_OK"
                if result == "not-launched" or result.startswith("stopped:")
                else "REAL_MODEL_CLEANUP_WARNING"
            )
            print(f"{marker} run_id={run_id} result={result}", file=sys.stderr)


def main() -> int:
    args = parse_args()
    asyncio.run(
        _run(
            args.config_name,
            target_name=args.target,
            timeout=args.timeout,
            build=args.build,
            model_ref=args.model_ref,
            revision=args.revision,
            configs_dir=args.configs_dir,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
