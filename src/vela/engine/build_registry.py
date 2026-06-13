from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

from vela.engine.ids import mint_ulid
from vela.engine.sidecar import verify_sidecar_from_system


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
    return root / "vela" / "builds"


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


def check_build_launch_integrity(
    reference: str, root: str | Path | None = None
) -> dict[str, Any]:
    builds_root = Path(root).expanduser() if root is not None else default_builds_root()
    with _registry_lock(builds_root):
        manifest, build_dir = _manifest_for_reference(builds_root, reference)
        status = str(manifest.get("status") or "unknown")
        if status not in {"ready", "adopted"}:
            raise BuildRegistryError(
                "build-integrity-failed",
                f"build {reference} is not launchable: {status}",
                {"build": reference, "status": status, "reason": "status"},
            )
        paths = _dict_or_empty(manifest.get("paths"))
        root_path = Path(str(paths.get("root") or build_dir)).expanduser()
        executable = _resolve_build_path(root_path, paths.get("executable") or "bin/vllm")
        python = _resolve_build_path(root_path, paths.get("python") or "bin/python")
        reason = _missing_build_path_reason(executable, python)
        if reason is not None:
            raise BuildRegistryError(
                "build-integrity-failed",
                f"build {reference} failed prelaunch integrity check: {reason}",
                {
                    "build": reference,
                    "build_id": str(manifest.get("build_id") or ""),
                    "reason": reason,
                    "executable": str(executable),
                    "python": str(python),
                },
            )
        integrity = _dict_or_empty(manifest.get("integrity"))
        expected_executable_sha = _optional_str(integrity.get("executable_sha256"))
        if expected_executable_sha is not None:
            try:
                current_executable_sha = _sha256_file(executable)
            except OSError as exc:
                raise BuildRegistryError(
                    "build-integrity-failed",
                    "build executable hash failed during prelaunch check",
                    {
                        "build": reference,
                        "build_id": str(manifest.get("build_id") or ""),
                        "reason": "executable-hash-failed",
                        "executable": str(executable),
                        "hash_error": str(exc),
                    },
                ) from exc
            if current_executable_sha != expected_executable_sha:
                raise BuildRegistryError(
                    "build-integrity-failed",
                    "build executable changed since verification",
                    {
                        "build": reference,
                        "build_id": str(manifest.get("build_id") or ""),
                        "reason": "executable-integrity-mismatch",
                        "expected_executable_sha256": expected_executable_sha,
                        "current_executable_sha256": current_executable_sha,
                    },
                )
        return {
            "build": reference,
            "build_id": str(manifest.get("build_id") or ""),
            "ok": True,
            "status": status,
            "executable": str(executable),
            "python": str(python),
        }


def sweep_stale_creating_builds(root: str | Path | None = None) -> dict[str, Any]:
    builds_root = Path(root).expanduser() if root is not None else default_builds_root()
    demoted: list[dict[str, str]] = []
    if not builds_root.exists():
        return {"demoted": demoted, "count": 0}
    with _registry_lock(builds_root):
        for manifest_path in sorted(builds_root.glob("*/build.json")):
            build_dir = manifest_path.parent
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(manifest, dict):
                continue
            if str(manifest.get("status") or "") != "creating":
                continue
            if _build_lock_is_held(build_dir):
                continue
            build_id = str(manifest.get("build_id") or build_dir.name)
            label = str(manifest.get("label") or "")
            install = _dict_or_empty(manifest.get("install"))
            install["stale"] = True
            manifest["install"] = install
            manifest["status"] = "failed"
            manifest["verify"] = {
                "checked_at": _utc_now(),
                "ok": False,
                "reason": "stale-creating",
            }
            _write_json_atomic(manifest_path, manifest)
            demoted.append({"build_id": build_id, "label": label})
    return {"demoted": demoted, "count": len(demoted)}


def select_build(
    reference: str,
    root: str | Path | None = None,
    *,
    updated_at: str | None = None,
) -> dict[str, Any]:
    builds_root = Path(root).expanduser() if root is not None else default_builds_root()
    with _registry_lock(builds_root):
        return _select_build_locked(reference, builds_root, updated_at=updated_at)


def _select_build_locked(
    reference: str,
    builds_root: Path,
    *,
    updated_at: str | None = None,
) -> dict[str, Any]:
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
    with _registry_lock(builds_root):
        return _verify_build_locked(reference, builds_root)


def _verify_build_locked(reference: str, builds_root: Path) -> dict[str, Any]:
    manifest, build_dir = _manifest_for_reference(builds_root, reference)
    result = _verify_build_manifest(manifest, build_dir)
    _write_json_atomic(build_dir / "build.json", manifest)
    return result


def repair_build(reference: str, root: str | Path | None = None) -> dict[str, Any]:
    builds_root = Path(root).expanduser() if root is not None else default_builds_root()
    with _registry_lock(builds_root):
        manifest, build_dir = _manifest_for_reference(builds_root, reference)
        with _build_lock(build_dir):
            try:
                _repair_build_artifacts(manifest, build_dir)
            except BuildRegistryError as exc:
                manifest["status"] = "broken"
                manifest["verify"] = {
                    "checked_at": _utc_now(),
                    "ok": False,
                    "reason": exc.details.get("reason") or exc.code,
                }
                _write_json_atomic(build_dir / "build.json", manifest)
                raise
            result = _verify_build_manifest(manifest, build_dir)
            if result.get("ok"):
                result["detail"] = "build repaired"
            _write_json_atomic(build_dir / "build.json", manifest)
            return result


def inspect_build(reference: str, root: str | Path | None = None) -> dict[str, Any]:
    builds_root = Path(root).expanduser() if root is not None else default_builds_root()
    manifest, build_dir = _manifest_for_reference(builds_root, reference)
    return {
        "manifest": _build_payload(
            manifest,
            _active_build_id(builds_root),
            build_dir=build_dir,
        )
    }


_VENV_DISCOVERY_ROOTS = (
    "~/venvs",
    "~/.venvs",
    "~/.virtualenvs",
    "~/miniconda3/envs",
    "~/anaconda3/envs",
)


def discover_venvs(roots: list[str | Path] | None = None) -> list[dict[str, Any]]:
    """Scan common roots for python venvs, annotated via inspect_venv (J35)."""
    candidates = [
        Path(root).expanduser()
        for root in (roots if roots is not None else _VENV_DISCOVERY_ROOTS)
    ]
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for root in candidates:
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            if not child.is_dir() or not (child / "bin" / "python").exists():
                continue
            key = str(child)
            if key in seen:
                continue
            seen.add(key)
            results.append(inspect_venv(child))
    return results


def inspect_venv(venv_path: str | Path) -> dict[str, Any]:
    """Fast, filesystem-only probe of a venv for Adopt Build's live validation.

    Mirrors what adoption will enforce (``bin/python`` + ``bin/vllm`` present)
    and reports the vllm/torch/python versions read from ``site-packages``
    dist-info and ``pyvenv.cfg`` — no subprocess, no imports, safe to run per
    keystroke.
    """
    path = Path(venv_path).expanduser()
    base: dict[str, Any] = {"ok": False, "venv_path": str(path)}
    if not path.is_dir():
        return {**base, "reason": "path does not exist on the target"}
    if not (path / "bin" / "python").exists():
        return {**base, "reason": "bin/python not found — not a virtualenv"}
    if not (path / "bin" / "vllm").exists():
        return {**base, "reason": "bin/vllm not found in the venv"}
    versions = _venv_package_versions(path, ("vllm", "torch"))
    if "vllm" not in versions:
        return {**base, "reason": "vllm is not installed in this venv"}
    return {
        "ok": True,
        "venv_path": str(path),
        "vllm_version": versions.get("vllm"),
        "torch_version": versions.get("torch"),
        "python_version": _venv_python_version(path),
    }


def _venv_python_version(path: Path) -> str | None:
    cfg = path / "pyvenv.cfg"
    if not cfg.exists():
        return None
    try:
        lines = cfg.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        key, _, value = line.partition("=")
        if key.strip() in {"version", "version_info"} and value.strip():
            return value.strip()
    return None


def _venv_package_versions(path: Path, names: tuple[str, ...]) -> dict[str, str]:
    found: dict[str, str] = {}
    for site in sorted(path.glob("lib/python*/site-packages")):
        for name in names:
            if name in found:
                continue
            for dist in sorted(site.glob(f"{name}-*.dist-info")):
                version = dist.name[len(name) + 1 : -len(".dist-info")]
                if version:
                    found[name] = version
                    break
    return found


def adopt_build(params: dict[str, Any], root: str | Path | None = None) -> dict[str, Any]:
    builds_root = Path(root).expanduser() if root is not None else default_builds_root()
    with _registry_lock(builds_root):
        return _adopt_build_locked(params, builds_root)


def _adopt_build_locked(params: dict[str, Any], builds_root: Path) -> dict[str, Any]:
    build_id = _build_id_from_params(params, builds_root)
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

    copy_build = _truthy(params.get("copy"))
    try:
        if copy_build:
            _copy_adopted_build_artifacts(build_dir, venv_path)
        else:
            _write_adopted_build_artifacts(build_dir, venv_path)
    except OSError as exc:
        shutil.rmtree(build_dir, ignore_errors=True)
        raise BuildRegistryError(
            "invalid-config",
            "unable to prepare adopted build artifacts",
            {"reason": "artifact-write-failed", "build": build_id, "path": str(build_dir)},
        ) from exc

    now = _utc_now()
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "build_id": build_id,
        "label": _optional_str(params.get("label")) or venv_path.name,
        "status": "adopted",
        "install": {
            "method": "adopt",
            "source": str(venv_path),
            "copy": copy_build,
        },
        "resolved": {
            "vllm": _optional_str(params.get("vllm_version")),
            "vllm_version_profile": _optional_str(params.get("vllm_version_profile")),
        },
        "paths": {
            "root": str(build_dir),
            "venv": "venv",
            "executable": "bin/vllm",
            "python": "bin/python",
            "activate": "activate",
            "run_script": "run.sh",
        },
        "created_at": now,
        "last_used_at": None,
        "notes": str(params.get("notes") or ""),
    }
    verify_result = _verify_build_manifest(manifest, build_dir)
    if not verify_result.get("ok"):
        shutil.rmtree(build_dir, ignore_errors=True)
        reason = str(verify_result.get("reason") or "adopt-verify-failed")
        raise BuildRegistryError(
            "invalid-config",
            f"external build verification failed: {reason}",
            {"reason": reason, "venv_path": str(venv_path), "build": build_id},
        )
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


def record_build_ref(
    build_id: str,
    run_id: str,
    sidecar_path: str | Path,
    *,
    pid: int,
    process_create_time: float,
    root: str | Path | None = None,
) -> dict[str, Any]:
    builds_root = Path(root).expanduser() if root is not None else default_builds_root()
    with _registry_lock(builds_root):
        manifest, build_dir = _manifest_for_reference(builds_root, build_id)
        actual_build_id = str(manifest["build_id"])
        with _build_lock(build_dir):
            ref_path = build_dir / "refs" / _build_ref_filename(run_id)
            payload = {
                "schema_version": 1,
                "run_id": run_id,
                "sidecar_path": str(Path(sidecar_path)),
                "pid": int(pid),
                "process_create_time": float(process_create_time),
            }
            _write_json_atomic(ref_path, payload)
            return {
                "build_id": actual_build_id,
                "run_id": run_id,
                "ref_path": str(ref_path),
            }


def remove_build(reference: str, root: str | Path | None = None) -> dict[str, Any]:
    builds_root = Path(root).expanduser() if root is not None else default_builds_root()
    with _registry_lock(builds_root):
        return _remove_build_locked(reference, builds_root)


def _remove_build_locked(reference: str, builds_root: Path) -> dict[str, Any]:
    manifest, build_dir = _manifest_for_reference(builds_root, reference)
    with _build_lock(build_dir):
        build_id = str(manifest["build_id"])
        was_active = build_id == _active_build_id(builds_root)
        if not _is_agent_owned_build_dir(builds_root, build_dir):
            raise BuildRegistryError(
                "invalid-config",
                "build path is outside the agent build registry",
                {
                    "build": reference,
                    "reason": "outside-build-root",
                    "path": str(build_dir),
                },
            )
        live_refs = _verified_live_build_refs(build_dir)
        if live_refs:
            raise BuildRegistryError(
                "resource-in-use",
                "build is used by a live run",
                {
                    "build": reference,
                    "reason": "build-ref",
                    "build_id": build_id,
                    "refs": live_refs,
                },
            )
        shutil.rmtree(build_dir)
        payload = {
            "build_id": build_id,
            "label": str(manifest.get("label") or ""),
            "removed": True,
            "removed_path": str(build_dir),
        }
        if was_active:
            default_build_id, default_label = _repair_active_default_after_removal(
                builds_root,
                removed_build_id=build_id,
            )
            payload["default_build_id"] = default_build_id
            payload["default_label"] = default_label
        return payload


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
        builds.append(_build_payload(manifest, default_build_id, build_dir=build_dir))

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
    verify_output: dict[str, Any] = {}
    integrity: dict[str, Any] = {}
    if reason is None:
        reason, verify_output = _build_verify_output(executable, python)
    if verify_output:
        verify_payload["verify_output"] = verify_output
    if reason is None:
        reason, integrity = _build_integrity(executable, python, verify_output)
    if integrity:
        drift_reason, drift_payload = _build_integrity_drift(
            _dict_or_empty(manifest.get("integrity")),
            integrity,
        )
        if drift_reason is not None:
            reason = drift_reason
            verify_payload["integrity"] = drift_payload
    if reason is None:
        manifest["integrity"] = integrity
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


def _build_verify_output(executable: Path, python: Path) -> tuple[str | None, dict[str, Any]]:
    import_probe = _run_build_verify_command(
        [str(python), "-c", "import vllm; print(vllm.__version__)"],
    )
    output = {
        "python_import": import_probe["output"],
        "python_returncode": import_probe["returncode"],
    }
    if not import_probe["ok"]:
        return "vllm-import-probe-failed", output
    version_probe = _run_build_verify_command([str(executable), "--version"])
    output.update(
        {
            "vllm_version": version_probe["output"],
            "vllm_returncode": version_probe["returncode"],
        }
    )
    if not version_probe["ok"]:
        return "vllm-version-probe-failed", output
    import_version = str(import_probe["output"])
    executable_version = str(version_probe["output"])
    if import_version not in executable_version:
        return "vllm-version-mismatch", output
    return None, output


def _build_integrity(
    executable: Path,
    python: Path,
    verify_output: dict[str, Any],
) -> tuple[str | None, dict[str, Any]]:
    freeze_probe = _build_freeze_probe(python)
    if not freeze_probe["ok"]:
        return "pip-freeze-probe-failed", {
            "strategy": "pip_freeze_sha256",
            "freeze_returncode": freeze_probe["returncode"],
            "freeze_output": freeze_probe["output"],
        }
    try:
        executable_sha = _sha256_file(executable)
    except OSError as exc:
        return "executable-hash-failed", {
            "strategy": "pip_freeze_sha256",
            "hash_error": str(exc),
        }
    freeze_output = str(freeze_probe["output"])
    return None, {
        "strategy": "pip_freeze_sha256",
        "freeze_sha256": _sha256_uri(freeze_output.encode("utf-8")),
        "executable_sha256": executable_sha,
        "verify_command": ["bin/vllm", "--version"],
        "verify_output": str(verify_output.get("vllm_version") or ""),
    }


def _build_freeze_probe(python: Path) -> dict[str, Any]:
    pip_probe = _run_build_verify_command([str(python), "-m", "pip", "freeze"])
    if pip_probe["ok"]:
        return pip_probe
    uv_path = shutil.which("uv")
    if uv_path is None:
        return pip_probe
    uv_probe = _run_build_verify_command(
        [uv_path, "pip", "freeze", "--python", str(python)]
    )
    if uv_probe["ok"]:
        return uv_probe
    return {
        "ok": False,
        "returncode": uv_probe["returncode"],
        "output": "\n".join(
            [
                f"python -m pip freeze failed: {pip_probe['output']}",
                f"uv pip freeze failed: {uv_probe['output']}",
            ]
        ),
    }


def _build_integrity_drift(
    previous: dict[str, Any],
    current: dict[str, Any],
) -> tuple[str | None, dict[str, Any]]:
    if not previous:
        return None, {}
    checks = (
        (
            "executable_sha256",
            "executable-integrity-mismatch",
            "expected_executable_sha256",
            "current_executable_sha256",
        ),
        (
            "freeze_sha256",
            "freeze-integrity-mismatch",
            "expected_freeze_sha256",
            "current_freeze_sha256",
        ),
    )
    for field, reason, expected_key, current_key in checks:
        expected = _optional_str(previous.get(field))
        actual = _optional_str(current.get(field))
        if expected is not None and actual is not None and expected != actual:
            return reason, {
                expected_key: expected,
                current_key: actual,
            }
    return None, {}


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _sha256_uri(data: bytes) -> str:
    return f"sha256:{sha256(data).hexdigest()}"


def _run_build_verify_command(argv: list[str]) -> dict[str, Any]:
    try:
        result = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "returncode": None, "output": str(exc)}
    output = (result.stdout or result.stderr or "").strip()
    return {
        "ok": result.returncode == 0 and bool(output),
        "returncode": result.returncode,
        "output": output,
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
    _write_venv_python_wrapper(bin_dir / "python", build_dir)
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


def _copy_adopted_build_artifacts(build_dir: Path, venv_path: Path) -> None:
    build_dir.mkdir(parents=True, exist_ok=False)
    shutil.copytree(venv_path, build_dir / "venv", symlinks=True)
    _write_build_run_artifacts(build_dir, build_dir / "venv")


def _repair_build_artifacts(manifest: dict[str, Any], build_dir: Path) -> None:
    paths = _dict_or_empty(manifest.get("paths"))
    install = _dict_or_empty(manifest.get("install"))
    root_path = Path(str(paths.get("root") or build_dir)).expanduser()
    venv_path = _resolve_build_path(root_path, paths.get("venv") or "venv")
    if str(install.get("method") or "") == "adopt":
        source = _optional_str(install.get("source"))
        if source is not None and not venv_path.exists():
            venv_path = Path(source).expanduser()
            _replace_symlink(build_dir / "venv", venv_path)
    reason = _missing_build_path_reason(
        venv_path / "bin" / "vllm",
        venv_path / "bin" / "python",
    )
    if reason is not None:
        raise BuildRegistryError(
            "invalid-config",
            f"unable to repair build artifacts: {reason}",
            {"reason": reason, "build": str(manifest.get("build_id") or "")},
        )
    _write_build_run_artifacts(build_dir, venv_path)
    manifest["paths"] = {
        "root": str(build_dir),
        "venv": "venv",
        "executable": "bin/vllm",
        "python": "bin/python",
        "activate": "activate",
        "run_script": "run.sh",
    }


def _write_build_run_artifacts(build_dir: Path, venv_path: Path) -> None:
    build_dir.mkdir(parents=True, exist_ok=True)
    bin_dir = build_dir / "bin"
    bin_dir.mkdir(exist_ok=True)
    _replace_symlink(bin_dir / "vllm", venv_path / "bin" / "vllm")
    _write_venv_python_wrapper(bin_dir / "python", build_dir)
    _replace_symlink(build_dir / "activate", venv_path / "bin" / "activate")
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


def _replace_symlink(link_path: Path, target_path: Path) -> None:
    if link_path.is_dir() and not link_path.is_symlink():
        shutil.rmtree(link_path)
    else:
        link_path.unlink(missing_ok=True)
    link_path.symlink_to(target_path)


def _write_venv_python_wrapper(path: Path, build_dir: Path) -> None:
    path.unlink(missing_ok=True)
    path.write_text(
        "\n".join(
            [
                "#!/bin/sh",
                "set -eu",
                f'BUILD_ROOT="{build_dir}"',
                'export VIRTUAL_ENV="${BUILD_ROOT}/venv"',
                'export PATH="${VIRTUAL_ENV}/bin:${PATH}"',
                'exec "${BUILD_ROOT}/venv/bin/python" "$@"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    path.chmod(0o755)


def _required_param(params: dict[str, Any], field: str) -> str:
    value = params.get(field)
    if not isinstance(value, str) or not value.strip():
        raise BuildRegistryError(
            "invalid-config",
            f"adopt_build requires {field}",
            {"reason": f"missing-{field}"},
        )
    return value


def _build_id_from_params(_params: dict[str, Any], builds_root: Path) -> str:
    return mint_build_id(builds_root)


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def mint_build_id(builds_root: Path) -> str:
    for _attempt in range(16):
        candidate = mint_ulid()
        if not (builds_root / candidate).exists():
            return candidate
    raise BuildRegistryError(
        "resource-in-use",
        "unable to mint an unused build id",
        {"reason": "build-id-collision"},
    )


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


def _repair_active_default_after_removal(
    builds_root: Path,
    *,
    removed_build_id: str,
) -> tuple[str | None, str | None]:
    replacement = _replacement_default_build(builds_root, removed_build_id)
    active_path = builds_root / "active.json"
    if replacement is None:
        with contextlib.suppress(FileNotFoundError):
            active_path.unlink()
        return None, None
    payload = {
        "schema_version": 1,
        "build_id": str(replacement["build_id"]),
        "label": str(replacement.get("label") or ""),
        "updated_at": _utc_now(),
    }
    _write_json_atomic(active_path, payload)
    return str(payload["build_id"]), str(payload["label"])


def _replacement_default_build(
    builds_root: Path,
    removed_build_id: str,
) -> dict[str, Any] | None:
    for manifest_path in sorted(builds_root.glob("*/build.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(manifest, dict):
            continue
        build_id = manifest.get("build_id")
        if not isinstance(build_id, str) or build_id == removed_build_id:
            continue
        status = str(manifest.get("status") or "unknown")
        if status in {"ready", "adopted"}:
            return manifest
    return None


def _is_agent_owned_build_dir(root: Path, build_dir: Path) -> bool:
    try:
        return build_dir.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def _build_payload(
    manifest: dict[str, Any],
    default_build_id: str | None,
    *,
    build_dir: Path | None = None,
) -> dict[str, Any]:
    build_id = str(manifest["build_id"])
    payload = {
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
    verify = _dict_or_empty(manifest.get("verify"))
    if verify:
        payload["verify"] = verify
    integrity = _dict_or_empty(manifest.get("integrity"))
    if integrity:
        payload["integrity"] = integrity
    if build_dir is not None:
        live_refs = _verified_live_build_refs(build_dir)
        payload["in_use"] = bool(live_refs)
        payload["live_refs"] = live_refs
    return payload


def _dict_or_empty(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    _write_private_text_atomic(
        path,
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
    )


def _write_private_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            fd = -1
            file.write(text)
    finally:
        if fd >= 0:
            os.close(fd)
    os.replace(tmp, path)


def _registry_lock(root: Path) -> _ExclusiveFileLock:
    return _ExclusiveFileLock(root / "builds.lock")


def _build_lock(build_dir: Path) -> _ExclusiveFileLock:
    return _ExclusiveFileLock(build_dir / "build.lock")


def _build_lock_is_held(build_dir: Path) -> bool:
    lock_path = build_dir / "build.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False
    finally:
        handle.close()


def _verified_live_build_refs(build_dir: Path) -> list[dict[str, Any]]:
    refs_dir = build_dir / "refs"
    if not refs_dir.exists():
        return []
    live_refs: list[dict[str, Any]] = []
    for ref_path in sorted(refs_dir.glob("*.ref")):
        ref = _load_build_ref(ref_path)
        sidecar_path = _optional_str(ref.get("sidecar_path")) if ref is not None else None
        if sidecar_path is None:
            _unlink_ref(ref_path)
            continue
        try:
            live = bool(verify_sidecar_from_system(sidecar_path))
        except Exception:
            live = False
        if live:
            live_refs.append(_build_ref_payload(ref))
        else:
            _unlink_ref(ref_path)
    return live_refs


def _load_build_ref(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _build_ref_payload(ref: dict[str, Any]) -> dict[str, Any]:
    # Controller-facing identity only. Agent-local sidecar paths, PIDs, and
    # process timestamps must never cross the RPC boundary (see the authority
    # boundary in docs/agent-rpc.md); build-level liveness is conveyed by the
    # ``in_use`` flag and per-ref ``run_id``.
    return {"run_id": str(ref.get("run_id") or "")}


def _build_ref_filename(run_id: str) -> str:
    safe = "".join(
        char if char.isalnum() or char in "._-" else "_" for char in str(run_id)
    ).strip("._")
    if not safe:
        safe = sha256(str(run_id).encode("utf-8")).hexdigest()
    return f"{safe}.ref"


def _unlink_ref(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()


class _ExclusiveFileLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: Any | None = None

    def __enter__(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self._path.open("a", encoding="utf-8")
        fcntl.flock(self._handle, fcntl.LOCK_EX)

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        if self._handle is None:
            return False
        try:
            fcntl.flock(self._handle, fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None
        return False


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)
