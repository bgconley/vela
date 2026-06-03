from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def default_builds_root() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME")
    root = Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
    return root / "vllm-loader" / "builds"


def list_builds(root: str | Path | None = None) -> dict[str, Any]:
    builds_root = Path(root).expanduser() if root is not None else default_builds_root()
    default_build_id = _active_build_id(builds_root)
    builds: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    if not builds_root.exists():
        return {"builds": builds, "default_build_id": default_build_id, "skipped": skipped}

    for manifest_path in sorted(builds_root.glob("*/build.json")):
        build_dir = manifest_path.parent
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            skipped.append({"build_id": build_dir.name, "reason": "invalid-json"})
            continue
        except OSError:
            skipped.append({"build_id": build_dir.name, "reason": "unreadable"})
            continue
        if not isinstance(manifest, dict):
            skipped.append({"build_id": build_dir.name, "reason": "invalid-manifest"})
            continue
        build_id = manifest.get("build_id")
        if not isinstance(build_id, str) or not build_id:
            skipped.append({"build_id": build_dir.name, "reason": "missing-build-id"})
            continue
        builds.append(_build_payload(manifest, default_build_id))

    return {"builds": builds, "default_build_id": default_build_id, "skipped": skipped}


def _active_build_id(root: Path) -> str | None:
    active_path = root / "active.json"
    try:
        data = json.loads(active_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    build_id = data.get("build_id")
    return build_id if isinstance(build_id, str) and build_id else None


def _build_payload(manifest: dict[str, Any], default_build_id: str | None) -> dict[str, Any]:
    build_id = str(manifest["build_id"])
    return {
        "build_id": build_id,
        "label": str(manifest.get("label") or ""),
        "status": str(manifest.get("status") or "unknown"),
        "default": build_id == default_build_id,
        "install": _dict_or_empty(manifest.get("install")),
        "resolved": _dict_or_empty(manifest.get("resolved")),
        "paths": _dict_or_empty(manifest.get("paths")),
        "created_at": _optional_str(manifest.get("created_at")),
        "last_used_at": _optional_str(manifest.get("last_used_at")),
        "notes": str(manifest.get("notes") or ""),
    }


def _dict_or_empty(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)
