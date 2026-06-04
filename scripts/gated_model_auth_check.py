#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import contextlib
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from vllm_loader.transport.subprocess import SubprocessTargetClient


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate that a gated Hugging Face model reports gated-auth."
    )
    parser.add_argument("repo_id")
    parser.add_argument("--model-id", default="remote-gated-model")
    parser.add_argument("--revision")
    parser.add_argument("--timeout", type=float, default=120.0)
    return parser.parse_args()


def _agent_env(tmp_root: Path) -> dict[str, str]:
    hf_home = tmp_root / "hf-home"
    return {
        "XDG_STATE_HOME": str(tmp_root / "state"),
        "HF_HOME": str(hf_home),
        "HF_HUB_CACHE": str(hf_home / "hub"),
        "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
        "HF_TOKEN": "",
    }


async def _next_job_done(events, *, timeout: float) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise RuntimeError("timed out waiting for gated model auth result")
        event = await asyncio.wait_for(events.__anext__(), timeout=remaining)
        if event.get("event") == "job_done":
            return event


async def _close_events(events) -> None:
    if events is None:
        return
    with contextlib.suppress(Exception):
        await events.aclose()


async def _run(
    repo_id: str,
    *,
    model_id: str,
    revision: str | None,
    timeout: float,
) -> None:
    tmp_root = Path(tempfile.mkdtemp(prefix="vllm-loader-gated-auth-"))
    job_id = f"gated-auth-{uuid.uuid4().hex}"
    client = SubprocessTargetClient(
        [sys.executable, "-m", "vllm_loader.cli", "agent", "connect"],
        env=_agent_env(tmp_root),
    )
    events = None
    try:
        await client.connect()
        events = client.subscribe([job_id], resume_from="live")
        pin_params: dict[str, Any] = {
            "display_name": model_id,
            "repo_id": repo_id,
            "gated": True,
            "token_required": True,
        }
        if revision:
            pin_params["revision"] = revision
        pinned = await client.call("pin_model", pin_params)
        download_params: dict[str, Any] = {
            "job_id": job_id,
            "model_ref": model_id,
        }
        if revision:
            download_params["revision"] = revision
        await client.call("download_model", download_params)
        done = await _next_job_done(events, timeout=timeout)
    finally:
        await _close_events(events)
        if client.connected:
            await client.disconnect()
        shutil.rmtree(tmp_root, ignore_errors=True)

    if done.get("ok"):
        raise RuntimeError(f"expected gated-auth failure, got successful download: {done}")
    if done.get("error_kind") != "gated-auth":
        raise RuntimeError(f"expected gated-auth, got {done}")
    entry = pinned.get("entry") if isinstance(pinned.get("entry"), dict) else {}
    print(
        "GATED_MODEL_AUTH_OK "
        f"repo_id={repo_id} "
        f"entry_id={entry.get('entry_id', '')} "
        f"error_kind={done.get('error_kind')} "
        f"detail={done.get('detail', '')}"
    )


def main() -> None:
    args = parse_args()
    asyncio.run(
        _run(
            args.repo_id,
            model_id=args.model_id,
            revision=args.revision,
            timeout=args.timeout,
        )
    )


if __name__ == "__main__":
    main()
