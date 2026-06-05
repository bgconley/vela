#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import contextlib
import shlex
import subprocess
import sys
import uuid
from typing import Any

from vllm_loader.config.targets import TargetConfig, TransportKind, load_targets_file
from vllm_loader.transport.client import TargetClient
from vllm_loader.transport.factory import (
    DEFAULT_SSH_CONTROL_OPTIONS,
    _ssh_option_present,
    _ssh_options_from_env,
    target_client_for_config,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run real-model disconnect/resume and daemon-restart validation."
    )
    parser.add_argument("config_name")
    parser.add_argument("--target", default="local")
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
) -> dict[str, Any]:
    params: dict[str, Any] = {"name": config_name}
    if build:
        params["build"] = build
    if model_ref:
        params["model_ref"] = model_ref
    if revision:
        params["revision"] = revision
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


def _restart_target_agent(target: TargetConfig) -> None:
    if target.transport is TransportKind.SSH:
        if target.host is None:
            raise RuntimeError(f"ssh target {target.name!r} has no host")
        ssh_cmd = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
        ]
        ssh_opts = _ssh_options_from_env(target)
        ssh_cmd.extend(ssh_opts)
        for key, value in DEFAULT_SSH_CONTROL_OPTIONS.items():
            if not _ssh_option_present(ssh_opts, key):
                ssh_cmd.extend(["-o", f"{key}={value}"])
        remote_command = "vllm-loader agent restart"
        if target.venv is not None:
            remote_command = (
                f"PATH={shlex.quote(str(target.venv / 'bin'))}:$PATH "
                f"{remote_command}"
            )
        if target.workdir is not None:
            remote_command = (
                f"cd {shlex.quote(str(target.workdir))} && {remote_command}"
            )
        subprocess.run([*ssh_cmd, target.host, remote_command], check=True)
        return
    subprocess.run(
        [sys.executable, "-m", "vllm_loader.cli", "agent", "restart"],
        check=True,
    )


async def _run(
    config_name: str,
    *,
    target_name: str,
    timeout: float,
    build: str | None,
    model_ref: str | None,
    revision: str | None,
) -> None:
    target, client = _new_client(target_name)
    run_id = f"real-resume-{uuid.uuid4().hex}"
    base_params = _launch_params(
        config_name,
        build=build,
        model_ref=model_ref,
        revision=revision,
    )
    events = None
    tail_task = None
    await client.connect()
    try:
        prepared = await client.call("prepare_launch", base_params)
        _reject_fake_config(prepared)
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

    _restart_target_agent(target)

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
                f"run {run_id} not rediscovered after daemon restart: {discovered}"
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
        "REAL_MODEL_DAEMON_RESTART_OK "
        f"run_id={run_id} url={health_after_restart.get('reachable_url')} "
        f"returncode={waited.get('returncode')}"
    )


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
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
