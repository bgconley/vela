from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from vela.agent.local import LocalAgent, TargetCallError
from vela.engine import model_registry as model_registry_module
from vela.transport.inprocess import InProcessTargetClient

REPO_ID = "org/exact-model"
PINNED_SHA = "a" * 40
STALE_SHA = "b" * 40


def _write_hf_snapshot(
    hub_cache: Path,
    *,
    commit_sha: str,
    filenames: tuple[str, ...],
    revision: str = "main",
) -> None:
    repo = hub_cache / "models--org--exact-model"
    (repo / "refs").mkdir(parents=True)
    (repo / "refs" / revision).write_text(commit_sha, encoding="utf-8")
    (repo / "blobs").mkdir()
    snapshot = repo / "snapshots" / commit_sha
    snapshot.mkdir(parents=True)
    for index, filename in enumerate(filenames):
        blob = repo / "blobs" / f"blob-{index}"
        blob.write_bytes(b"{}" if filename == "config.json" else b"weights")
        target = snapshot / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(blob)


def _install_remote_manifest(
    monkeypatch: pytest.MonkeyPatch,
    *,
    commit_sha: str = PINNED_SHA,
) -> None:
    siblings = (
        SimpleNamespace(rfilename="config.json", size=2),
        SimpleNamespace(rfilename="model-00001-of-00002.safetensors", size=7),
        SimpleNamespace(rfilename="model-00002-of-00002.safetensors", size=7),
    )
    monkeypatch.setattr(
        model_registry_module,
        "_hf_model_info",
        lambda repo_id, revision=None: SimpleNamespace(
            sha=commit_sha,
            gated=False,
            siblings=siblings,
        ),
    )


def _configure_real_cache_scan(
    monkeypatch: pytest.MonkeyPatch, hub_cache: Path
) -> None:
    monkeypatch.setattr(
        model_registry_module,
        "default_hf_hub_cache_dir",
        lambda: hub_cache,
    )


def _write_cached_config(
    configs_dir: Path,
    *,
    model_ref: str,
    port: int,
) -> None:
    configs_dir.mkdir()
    (configs_dir / "cached.yaml").write_text(
        "\n".join(
            (
                "name: cached",
                f"model: {REPO_ID}",
                f"model_ref: {model_ref}",
                f"revision: {PINNED_SHA}",
                "server:",
                f"  port: {port}",
                "launch:",
                "  require_cached_models: true",
                "",
            )
        ),
        encoding="utf-8",
    )


async def _pin_exact_model(client: InProcessTargetClient) -> dict[str, object]:
    return await client.call(
        "pin_model",
        {
            "entry_id": "human-selected-pin",
            "display_name": "exact-model",
            "repo_id": REPO_ID,
            "revision": PINNED_SHA,
            "commit_sha": PINNED_SHA,
        },
    )


@pytest.mark.asyncio
async def test_exact_preexisting_hf_snapshot_is_persisted_and_launch_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unused_tcp_port: int,
) -> None:
    hub_cache = tmp_path / "hf-home" / "hub"
    _write_hf_snapshot(
        hub_cache,
        commit_sha=PINNED_SHA,
        filenames=(
            "config.json",
            "model-00001-of-00002.safetensors",
            "model-00002-of-00002.safetensors",
        ),
    )
    _configure_real_cache_scan(monkeypatch, hub_cache)
    _install_remote_manifest(monkeypatch)
    monkeypatch.setattr(
        model_registry_module,
        "_snapshot_download",
        lambda **_kwargs: pytest.fail("preexisting cache must not download"),
    )

    registry_path = tmp_path / "state" / "vela" / "models" / "registry.json"
    configs_dir = tmp_path / "configs"
    configs_dir.mkdir()
    client = InProcessTargetClient(LocalAgent(models_registry_path=registry_path))
    await client.connect()
    try:
        pinned = await _pin_exact_model(client)
        entry = pinned["entry"]
        assert isinstance(entry, dict)
        composed = await client.call(
            "compose_config",
            {
                "configs_dir": str(configs_dir),
                "name": "cached",
                "runtime": "process",
                "model_ref": str(entry["entry_id"]),
                "recipe": "__custom__",
                "overrides": {
                    "server": {"port": unused_tcp_port},
                    "launch": {"require_cached_models": True},
                },
            },
        )
        await client.call(
            "save_config",
            {
                "configs_dir": str(configs_dir),
                "name": "cached",
                "config": composed["config"],
            },
        )
        preview = await client.call(
            "preview", {"name": "cached", "configs_dir": str(configs_dir)}
        )
        preflight = await client.call(
            "preflight", {"name": "cached", "configs_dir": str(configs_dir)}
        )
        prepared = await client.call(
            "prepare_launch", {"name": "cached", "configs_dir": str(configs_dir)}
        )
    finally:
        await client.disconnect()

    stored = json.loads(registry_path.read_text(encoding="utf-8"))["entries"][0]
    assert entry["cache_state"] == "cached"
    assert stored["cache_state"] == "cached"
    assert stored["commit_sha"] == PINNED_SHA
    assert stored["expected_files"] == [
        "config.json",
        "model-00001-of-00002.safetensors",
        "model-00002-of-00002.safetensors",
    ]
    assert stored["files"]["weights_format"] == "safetensors"
    assert composed["config"]["model_ref"] == entry["entry_id"]
    assert composed["config"]["revision"] == PINNED_SHA
    assert preview["metadata"]["model_cache_state"] == "cached"
    assert preflight == {"ok": True, "failures": [], "warnings": []}
    assert prepared["launch_warnings"] == []


@pytest.mark.asyncio
async def test_pin_does_not_adopt_stale_revision_snapshot_for_another_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub_cache = tmp_path / "hf-home" / "hub"
    _write_hf_snapshot(
        hub_cache,
        commit_sha=STALE_SHA,
        filenames=(
            "config.json",
            "model-00001-of-00002.safetensors",
            "model-00002-of-00002.safetensors",
        ),
    )
    _configure_real_cache_scan(monkeypatch, hub_cache)
    _install_remote_manifest(monkeypatch)
    registry_path = tmp_path / "state" / "vela" / "models" / "registry.json"
    client = InProcessTargetClient(LocalAgent(models_registry_path=registry_path))
    await client.connect()
    try:
        pinned = await client.call(
            "pin_model",
            {
                "repo_id": REPO_ID,
                "revision": "main",
                "commit_sha": PINNED_SHA,
            },
        )
        listed = await client.call("list_models")
    finally:
        await client.disconnect()

    assert pinned["entry"]["commit_sha"] == PINNED_SHA
    assert pinned["entry"]["cache_state"] == "remote_only"
    pinned_rows = [row for row in listed["models"] if row["pinned"]]
    assert len(pinned_rows) == 1
    assert pinned_rows[0]["commit_sha"] == PINNED_SHA
    assert pinned_rows[0]["cache_state"] == "remote_only"
    assert any(
        not row["pinned"]
        and row["commit_sha"] == STALE_SHA
        and row["cache_state"] == "cached"
        for row in listed["models"]
    )


@pytest.mark.asyncio
async def test_pin_marks_exact_but_incomplete_snapshot_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unused_tcp_port: int,
) -> None:
    hub_cache = tmp_path / "hf-home" / "hub"
    _write_hf_snapshot(
        hub_cache,
        commit_sha=PINNED_SHA,
        filenames=("config.json", "model-00001-of-00002.safetensors"),
    )
    _configure_real_cache_scan(monkeypatch, hub_cache)
    _install_remote_manifest(monkeypatch)
    registry_path = tmp_path / "state" / "vela" / "models" / "registry.json"
    configs_dir = tmp_path / "configs"
    client = InProcessTargetClient(LocalAgent(models_registry_path=registry_path))
    await client.connect()
    try:
        pinned = await _pin_exact_model(client)
        entry = pinned["entry"]
        assert isinstance(entry, dict)
        _write_cached_config(
            configs_dir,
            model_ref=str(entry["entry_id"]),
            port=unused_tcp_port,
        )
        with pytest.raises(TargetCallError) as exc_info:
            await client.call(
                "prepare_launch", {"name": "cached", "configs_dir": str(configs_dir)}
            )
    finally:
        await client.disconnect()

    stored = json.loads(registry_path.read_text(encoding="utf-8"))["entries"][0]
    assert entry["cache_state"] == "partial"
    assert stored["cache_state"] == "partial"
    assert stored["files"]["count"] == 2
    assert exc_info.value.code == "preflight-failed"
    assert exc_info.value.details["kind"] == "model-not-cached"
