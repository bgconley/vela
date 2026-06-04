from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
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


def resolve_build_handoff(
    reference: str | None, root: str | Path | None = None
) -> BuildHandoff | None:
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


def select_build(
    reference: str,
    root: str | Path | None = None,
    *,
    updated_at: str | None = None,
) -> dict[str, Any]:
    builds_root = Path(root).expanduser() if root is not None else default_builds_root()
    handoff = resolve_build_handoff(reference, builds_root)
    if handoff is None:
        raise BuildRegistryError(
            "build-not-found",
            f"unknown build: {reference}",
            {"build": reference},
        )
    payload = {
        "schema_version": 1,
        "build_id": handoff.build_id,
        "label": handoff.label,
        "updated_at": updated_at or _utc_now(),
    }
    _write_json_atomic(builds_root / "active.json", payload)
    return {
        "build_id": handoff.build_id,
        "label": handoff.label,
        "active": True,
    }


def verify_build(reference: str, root: str | Path | None = None) -> dict[str, Any]:
    builds_root = Path(root).expanduser() if root is not None else default_builds_root()
    manifest, build_dir = _manifest_for_reference(builds_root, reference)
    result = _verify_build_manifest(manifest, build_dir)
    _write_json_atomic(build_dir / "build.json", manifest)
    return result


def inspect_build(reference: str, root: str | Path | None = None) -> dict[str, Any]:
    builds_root = Path(root).expanduser() if root is not None else default_builds_root()
    manifest, _build_dir = _manifest_for_reference(builds_root, reference)
    return {"manifest": _build_payload(manifest, _active_build_id(builds_root))}


def adopt_build(params: dict[str, Any], root: str | Path | None = None) -> dict[str, Any]:
    builds_root = Path(root).expanduser() if root is not None else default_builds_root()
    build_id = _required_param(params, "build_id")
    venv_path = Path(_required_param(params, "venv_path")).expanduser()
    executable = venv_path / "bin" / "vllm"
    python = venv_path / "bin" / "python"
    reason = _missing_build_path_reason(executable, python)
    if reason is not None:
        raise BuildRegistryError(
            "invalid-config",
            f"external build verification failed: {reason}",
            {"reason": reason, "venv_path": str(venv_path)},
        )

    build_dir = builds_root / build_id
    if build_dir.exists():
        raise BuildRegistryError(
            "resource-in-use",
            f"build already exists: {build_id}",
            {"build": build_id, "reason": "build-exists"},
        )

    try:
        _write_adopted_build_artifacts(build_dir, venv_path)
    except OSError as exc:
        shutil.rmtree(build_dir, ignore_errors=True)
        raise BuildRegistryError(
            "invalid-config",
            "unable to prepare adopted build artifacts",
            {"reason": "artifact-write-failed", "build": build_id, "path": str(build_dir)},
        ) from exc

    build_executable = build_dir / "bin" / "vllm"
    build_python = build_dir / "venv" / "bin" / "python"
    now = _utc_now()
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "build_id": build_id,
        "label": _optional_str(params.get("label")) or venv_path.name,
        "status": "adopted",
        "install": {
            "method": "adopt",
            "source": str(venv_path),
        },
        "resolved": {
            "vllm": _optional_str(params.get("vllm_version")),
            "vllm_version_profile": _optional_str(params.get("vllm_version_profile")),
        },
        "paths": {
            "root": str(build_dir),
            "venv": "venv",
            "executable": "bin/vllm",
            "python": "venv/bin/python",
            "activate": "activate",
            "run_script": "run.sh",
        },
        "created_at": now,
        "last_used_at": None,
        "notes": str(params.get("notes") or ""),
    }
    verify_payload = {
        "checked_at": now,
        "ok": True,
        "reason": None,
        "executable": str(build_executable),
        "python": str(build_python),
    }
    manifest["verify"] = verify_payload
    _write_json_atomic(build_dir / "build.json", manifest)
    return {
        "build_id": build_id,
        "label": str(manifest["label"]),
        "status": "adopted",
        "manifest": _build_payload(manifest, _active_build_id(builds_root)),
    }


def build_reference_aliases(reference: str, root: str | Path | None = None) -> set[str]:
    builds_root = Path(root).expanduser() if root is not None else default_builds_root()
    manifest, _build_dir = _manifest_for_reference(builds_root, reference)
    aliases = {reference}
    for field in ("build_id", "label"):
        value = manifest.get(field)
        if isinstance(value, str) and value:
            aliases.add(value)
    return aliases


def remove_build(reference: str, root: str | Path | None = None) -> dict[str, Any]:
    builds_root = Path(root).expanduser() if root is not None else default_builds_root()
    manifest, build_dir = _manifest_for_reference(builds_root, reference)
    build_id = str(manifest["build_id"])
    if build_id == _active_build_id(builds_root):
        raise BuildRegistryError(
            "resource-in-use",
            "build is the active default",
            {"build": reference, "reason": "active-build", "build_id": build_id},
        )
    if not _is_agent_owned_build_dir(builds_root, build_dir):
        raise BuildRegistryError(
            "invalid-config",
            "build path is outside the agent build registry",
            {"build": reference, "reason": "outside-build-root", "path": str(build_dir)},
        )
    shutil.rmtree(build_dir)
    return {
        "build_id": build_id,
        "label": str(manifest.get("label") or ""),
        "removed": True,
        "removed_path": str(build_dir),
    }


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


def _verify_build_manifest(manifest: dict[str, Any], build_dir: Path) -> dict[str, Any]:
    paths = _dict_or_empty(manifest.get("paths"))
    root_path = Path(str(paths.get("root") or build_dir)).expanduser()
    executable = _resolve_build_path(root_path, paths.get("executable") or "bin/vllm")
    python = _resolve_build_path(root_path, paths.get("python") or "bin/python")
    reason = _missing_build_path_reason(executable, python)
    now = _utc_now()
    verify_payload: dict[str, Any] = {
        "checked_at": now,
        "executable": str(executable),
        "python": str(python),
    }
    if reason is None:
        verify_payload.update({"ok": True, "reason": None})
        manifest["verify"] = verify_payload
        if str(manifest.get("status") or "") == "broken":
            manifest["status"] = "ready"
        return {
            "build_id": str(manifest["build_id"]),
            "ok": True,
            "status": str(manifest.get("status") or "ready"),
            "detail": "build verified",
            "manifest": _build_payload(manifest, None),
        }
    verify_payload.update({"ok": False, "reason": reason})
    manifest["status"] = "broken"
    manifest["verify"] = verify_payload
    return {
        "build_id": str(manifest["build_id"]),
        "ok": False,
        "status": "broken",
        "reason": reason,
        "detail": f"build verification failed: {reason}",
        "manifest": _build_payload(manifest, None),
    }


def _missing_build_path_reason(executable: Path, python: Path) -> str | None:
    if not executable.exists():
        return "missing-executable"
    if not python.exists():
        return "missing-python"
    return None


def _write_adopted_build_artifacts(build_dir: Path, venv_path: Path) -> None:
    build_dir.mkdir(parents=True, exist_ok=False)
    bin_dir = build_dir / "bin"
    bin_dir.mkdir()
    (build_dir / "venv").symlink_to(venv_path)
    (bin_dir / "vllm").symlink_to(venv_path / "bin" / "vllm")
    (build_dir / "activate").symlink_to(venv_path / "bin" / "activate")
    run_script = build_dir / "run.sh"
    run_script.write_text(
        "\n".join(
            [
                "#!/bin/sh",
                "set -eu",
                f'BUILD_ROOT="{build_dir}"',
                'export VIRTUAL_ENV="${BUILD_ROOT}/venv"',
                'export PATH="${VIRTUAL_ENV}/bin:${PATH}"',
                'exec "${BUILD_ROOT}/bin/vllm" "$@"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    run_script.chmod(0o755)


def _required_param(params: dict[str, Any], field: str) -> str:
    value = params.get(field)
    if not isinstance(value, str) or not value.strip():
        raise BuildRegistryError(
            "invalid-config",
            f"adopt_build requires {field}",
            {"reason": f"missing-{field}"},
        )
    return value


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


def _is_agent_owned_build_dir(root: Path, build_dir: Path) -> bool:
    try:
        return build_dir.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


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


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)
