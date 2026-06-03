from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class BuildRegistryError(RuntimeError):
    code: str
    message: str
    details: dict[str, Any]


@dataclass(frozen=True)
class BuildHandoff:
    build_id: str
    label: str
    executable: Path
    python: Path
    env_overlay: dict[str, str]
    vllm_version: str | None
    vllm_version_profile: str | None

    def payload(self) -> dict[str, Any]:
        return {
            "build_id": self.build_id,
            "label": self.label,
            "executable": str(self.executable),
            "python": str(self.python),
            "env_overlay": dict(self.env_overlay),
            "vllm_version": self.vllm_version,
            "vllm_version_profile": self.vllm_version_profile,
        }


def default_builds_root() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME")
    root = Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
    return root / "vllm-loader" / "builds"


def resolve_build_handoff(reference: str | None, root: str | Path | None = None) -> BuildHandoff | None:
    builds_root = Path(root).expanduser() if root is not None else default_builds_root()
    selected = reference or _active_build_id(builds_root)
    if selected is None:
        return None
    manifest, build_dir = _manifest_for_reference(builds_root, selected)
    status = str(manifest.get("status") or "unknown")
    if status not in {"ready", "adopted"}:
        raise BuildRegistryError(
            "build-not-found",
            f"build {selected} is not launchable: {status}",
            {"build": selected, "status": status},
        )
    paths = _dict_or_empty(manifest.get("paths"))
    root_path = Path(str(paths.get("root") or build_dir)).expanduser()
    venv = _resolve_build_path(root_path, paths.get("venv") or "venv")
    executable = _resolve_build_path(root_path, paths.get("executable") or "bin/vllm")
    python = _resolve_build_path(root_path, paths.get("python") or "bin/python")
    resolved = _dict_or_empty(manifest.get("resolved"))
    return BuildHandoff(
        build_id=str(manifest["build_id"]),
        label=str(manifest.get("label") or ""),
        executable=executable,
        python=python,
        env_overlay={
            "VIRTUAL_ENV": str(venv),
            "PATH_PREPEND": str(venv / "bin"),
        },
        vllm_version=_optional_str(resolved.get("vllm")),
        vllm_version_profile=_optional_str(resolved.get("vllm_version_profile")),
    )


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


def _manifest_for_reference(root: Path, reference: str) -> tuple[dict[str, Any], Path]:
    direct_path = root / reference / "build.json"
    if direct_path.exists():
        return _load_manifest_or_raise(direct_path, reference), direct_path.parent

    matches: list[tuple[dict[str, Any], Path]] = []
    for manifest_path in sorted(root.glob("*/build.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(manifest, dict):
            continue
        if manifest.get("label") == reference:
            matches.append((manifest, manifest_path.parent))
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise BuildRegistryError(
            "build-not-found",
            f"build label is ambiguous: {reference}",
            {"build": reference},
        )
    raise BuildRegistryError(
        "build-not-found",
        f"unknown build: {reference}",
        {"build": reference},
    )


def _load_manifest_or_raise(path: Path, reference: str) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BuildRegistryError(
            "build-not-found",
            f"invalid build manifest: {reference}",
            {"build": reference, "reason": "invalid-json"},
        ) from exc
    except OSError as exc:
        raise BuildRegistryError(
            "build-not-found",
            f"unable to read build manifest: {reference}",
            {"build": reference, "reason": "unreadable"},
        ) from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("build_id"), str):
        raise BuildRegistryError(
            "build-not-found",
            f"invalid build manifest: {reference}",
            {"build": reference, "reason": "invalid-manifest"},
        )
    return manifest


def _resolve_build_path(root: Path, value: object) -> Path:
    path = Path(str(value)).expanduser()
    return path if path.is_absolute() else root / path


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
