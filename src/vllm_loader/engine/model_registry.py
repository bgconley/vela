from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ModelRegistryError(RuntimeError):
    code: str
    message: str
    details: dict[str, Any]


@dataclass(frozen=True)
class ModelHandoff:
    reference: str
    entry_id: str
    display_name: str
    source: str
    model_arg: str
    revision: str | None
    tokenizer: str | None
    repo_id: str | None
    local_path: str | None
    commit_sha: str | None
    cache_state: str | None
    gated: bool
    token_required: bool

    def metadata(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model_ref": self.reference,
            "model_entry_id": self.entry_id,
            "model_display_name": self.display_name,
            "model_source": self.source,
            "model_revision": self.revision,
            "model_cache_state": self.cache_state,
            "model_gated": self.gated,
            "model_token_required": self.token_required,
        }
        if self.repo_id is not None:
            payload["model_repo_id"] = self.repo_id
        if self.local_path is not None:
            payload["model_local_path"] = self.local_path
        if self.commit_sha is not None:
            payload["model_commit_sha"] = self.commit_sha
        if self.tokenizer is not None:
            payload["model_tokenizer"] = self.tokenizer
        return payload


MODEL_ENTRY_FIELDS = (
    "entry_id",
    "display_name",
    "source",
    "repo_id",
    "revision",
    "commit_sha",
    "local_path",
    "url",
    "quant_format",
    "tokenizer",
    "files",
    "size_bytes",
    "cache_state",
    "gated",
    "token_required",
    "integrity",
    "created_at",
    "last_used_at",
    "notes",
)


def default_models_registry_path() -> Path:
    state_home = os.environ.get("XDG_STATE_HOME")
    root = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return root / "vllm-loader" / "models" / "registry.json"


def resolve_model_handoff(
    reference: str | None, registry_path: str | Path | None = None
) -> ModelHandoff | None:
    if reference is None or not str(reference).strip():
        return None
    selected = str(reference)
    path = (
        Path(registry_path).expanduser()
        if registry_path is not None
        else default_models_registry_path()
    )
    registry = _load_registry_or_raise(path, selected)
    entry = _entry_for_reference(registry, selected)
    return _handoff_from_entry(selected, entry)


def pin_model(params: dict[str, Any], registry_path: str | Path | None = None) -> dict[str, Any]:
    path = (
        Path(registry_path).expanduser()
        if registry_path is not None
        else default_models_registry_path()
    )
    registry = _load_registry_for_write(path)
    entry = _pin_entry_from_params(params)
    entries = registry.get("entries") or []
    if not isinstance(entries, list):
        entries = []
    entries = [
        item
        for item in entries
        if not isinstance(item, dict) or item.get("entry_id") != entry["entry_id"]
    ]
    entries.append(entry)
    registry["schema_version"] = 1
    registry["default_cache"] = str(registry.get("default_cache") or "hf")
    registry.setdefault("app_download_dir", None)
    registry["entries"] = entries
    _write_registry_atomic(path, registry)
    return {"entry": _model_payload(entry)}


def verify_model(reference: str, registry_path: str | Path | None = None) -> dict[str, Any]:
    path = (
        Path(registry_path).expanduser()
        if registry_path is not None
        else default_models_registry_path()
    )
    registry = _load_registry_for_write(path)
    entry = _entry_for_reference(registry, reference)
    if entry.get("source") == "local_path":
        result = _verify_local_model_entry(entry)
    else:
        result = _verify_metadata_model_entry(entry)
    _write_registry_atomic(path, registry)
    return result


def download_hf_model(
    reference: str,
    registry_path: str | Path | None = None,
    *,
    revision: str | None = None,
    allow_patterns: list[str] | None = None,
    ignore_patterns: list[str] | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    path = (
        Path(registry_path).expanduser()
        if registry_path is not None
        else default_models_registry_path()
    )
    registry = _load_registry_for_write(path)
    entry = _entry_for_reference(registry, reference)
    if entry.get("source") != "hf_repo":
        raise ModelRegistryError(
            "invalid-config",
            f"model {reference} is not a Hugging Face repo entry",
            {"model_ref": reference, "source": _optional_str(entry.get("source"))},
        )

    repo_id = _required_str(entry, "repo_id", reference)
    selected_revision = (
        _optional_str(revision)
        or _optional_str(entry.get("commit_sha"))
        or _optional_str(entry.get("revision"))
    )
    download_kwargs: dict[str, Any] = {"repo_id": repo_id}
    if selected_revision:
        download_kwargs["revision"] = selected_revision
    if allow_patterns is not None:
        download_kwargs["allow_patterns"] = allow_patterns
        entry["allow_patterns"] = list(allow_patterns)
    if ignore_patterns is not None:
        download_kwargs["ignore_patterns"] = ignore_patterns
        entry["ignore_patterns"] = list(ignore_patterns)
    token = os.environ.get("HF_TOKEN")
    if token:
        download_kwargs["token"] = token
    if progress_callback is not None:
        download_kwargs["tqdm_class"] = _snapshot_progress_tqdm_class(progress_callback)

    entry["cache_state"] = "partial"
    registry["schema_version"] = 1
    registry["default_cache"] = str(registry.get("default_cache") or "hf")
    registry.setdefault("app_download_dir", None)
    _write_registry_atomic(path, registry)

    try:
        snapshot_path = _snapshot_download(**download_kwargs)
    except ModelRegistryError:
        raise
    except ImportError as exc:
        raise ModelRegistryError(
            "feature-unavailable",
            "huggingface_hub is required to download remote models",
            {"model_ref": reference, "repo_id": repo_id},
        ) from exc
    except Exception as exc:
        raise _snapshot_download_error(
            exc,
            model_ref=reference,
            repo_id=repo_id,
            revision=selected_revision,
        ) from exc

    cached = _matching_hf_cache_payload(repo_id, selected_revision)
    if cached is None:
        raise ModelRegistryError(
            "model-not-found",
            f"downloaded model {repo_id} was not found in the Hugging Face cache",
            {
                "model_ref": reference,
                "repo_id": repo_id,
                "snapshot_path": str(snapshot_path),
            },
        )
    _apply_cached_model_payload(entry, cached)
    registry["schema_version"] = 1
    registry["default_cache"] = str(registry.get("default_cache") or "hf")
    registry.setdefault("app_download_dir", None)
    _write_registry_atomic(path, registry)
    return {
        "entry_id": str(entry.get("entry_id") or ""),
        "ok": True,
        "cache_state": "cached",
        "detail": "model cached",
        "snapshot_path": str(snapshot_path),
        "entry": _model_payload(entry),
    }


def mark_hf_model_partial(
    reference: str,
    registry_path: str | Path | None = None,
    *,
    allow_patterns: list[str] | None = None,
    ignore_patterns: list[str] | None = None,
) -> dict[str, Any]:
    path = (
        Path(registry_path).expanduser()
        if registry_path is not None
        else default_models_registry_path()
    )
    registry = _load_registry_for_write(path)
    entry = _entry_for_reference(registry, reference)
    if entry.get("source") != "hf_repo":
        raise ModelRegistryError(
            "invalid-config",
            f"model {reference} is not a Hugging Face repo entry",
            {"model_ref": reference, "source": _optional_str(entry.get("source"))},
        )
    entry["cache_state"] = "partial"
    if allow_patterns is not None:
        entry["allow_patterns"] = list(allow_patterns)
    if ignore_patterns is not None:
        entry["ignore_patterns"] = list(ignore_patterns)
    registry["schema_version"] = 1
    registry["default_cache"] = str(registry.get("default_cache") or "hf")
    registry.setdefault("app_download_dir", None)
    _write_registry_atomic(path, registry)
    return {"entry": _model_payload(entry)}


def refresh_models(registry_path: str | Path | None = None) -> dict[str, Any]:
    path = (
        Path(registry_path).expanduser()
        if registry_path is not None
        else default_models_registry_path()
    )
    registry = _load_registry_for_write(path)
    entries = registry.get("entries") or []
    if not isinstance(entries, list):
        entries = []

    refreshed = 0
    cached_hf_payloads = [_model_payload(entry) for entry in _cached_model_entries_from_hf_scan()]
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry_id = entry.get("entry_id")
        if not isinstance(entry_id, str) or not entry_id:
            continue
        if entry.get("source") == "local_path":
            _verify_local_model_entry(entry)
            refreshed += 1
        elif entry.get("source") == "hf_repo":
            cached = _matching_hf_payload_for_entry(entry, cached_hf_payloads)
            if cached is not None:
                _apply_cached_model_payload(entry, cached)
                refreshed += 1
            elif str(entry.get("cache_state") or "") == "cached":
                entry["cache_state"] = "missing"
                refreshed += 1

    registry["schema_version"] = 1
    registry["default_cache"] = str(registry.get("default_cache") or "hf")
    registry.setdefault("app_download_dir", None)
    registry["entries"] = entries
    _write_registry_atomic(path, registry)

    result = list_models(path)
    result["refreshed"] = refreshed
    return result


def inspect_model(reference: str, registry_path: str | Path | None = None) -> dict[str, Any]:
    path = (
        Path(registry_path).expanduser()
        if registry_path is not None
        else default_models_registry_path()
    )
    registry = _load_registry_or_raise(path, reference)
    entry = _entry_for_reference(registry, reference)
    return {"entry": _model_payload(entry)}


def model_reference_aliases(
    reference: str, registry_path: str | Path | None = None
) -> set[str]:
    path = (
        Path(registry_path).expanduser()
        if registry_path is not None
        else default_models_registry_path()
    )
    registry = _load_registry_or_raise(path, reference)
    entry = _entry_for_reference(registry, reference)
    aliases = {reference}
    for field in ("entry_id", "display_name"):
        value = entry.get(field)
        if isinstance(value, str) and value:
            aliases.add(value)
    return aliases


def remove_model(reference: str, registry_path: str | Path | None = None) -> dict[str, Any]:
    path = (
        Path(registry_path).expanduser()
        if registry_path is not None
        else default_models_registry_path()
    )
    registry = _load_registry_for_write(path)
    entries = registry.get("entries") or []
    if not isinstance(entries, list):
        raise ModelRegistryError(
            "model-not-found",
            f"model registry has no valid entries for {reference}",
            {"model_ref": reference, "reason": "invalid-entries"},
        )
    entry = _entry_for_reference(registry, reference)
    removed_id = entry.get("entry_id")
    removal = _remove_model_weights(entry, reference)
    registry["entries"] = [
        item
        for item in entries
        if not isinstance(item, dict) or item.get("entry_id") != removed_id
    ]
    registry["schema_version"] = 1
    registry["default_cache"] = str(registry.get("default_cache") or "hf")
    registry.setdefault("app_download_dir", None)
    _write_registry_atomic(path, registry)
    return {
        "entry_id": str(removed_id),
        "source": str(entry.get("source") or ""),
        "removed_weights": bool(removal["removed_weights"]),
        "expected_freed_size": int(removal["expected_freed_size"]),
        "entry": _model_payload(entry),
    }


def list_models(registry_path: str | Path | None = None) -> dict[str, Any]:
    path = (
        Path(registry_path).expanduser()
        if registry_path is not None
        else default_models_registry_path()
    )
    skipped: list[dict[str, str]] = []
    registry: dict[str, Any] = {
        "schema_version": 1,
        "default_cache": "hf",
        "app_download_dir": None,
        "entries": [],
    }
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {
                "models": [],
                "default_cache": "hf",
                "app_download_dir": None,
                "skipped": [{"entry_id": "", "reason": "invalid-json"}],
            }
        except OSError:
            return {
                "models": [],
                "default_cache": "hf",
                "app_download_dir": None,
                "skipped": [{"entry_id": "", "reason": "unreadable"}],
            }
        if not isinstance(loaded, dict):
            return {
                "models": [],
                "default_cache": "hf",
                "app_download_dir": None,
                "skipped": [{"entry_id": "", "reason": "invalid-registry"}],
            }
        registry = loaded

    models: list[dict[str, Any]] = []
    entries = registry.get("entries") or []
    if not isinstance(entries, list):
        skipped.append({"entry_id": "", "reason": "invalid-entries"})
        entries = []
    for entry in entries:
        if not isinstance(entry, dict):
            skipped.append({"entry_id": "", "reason": "invalid-entry"})
            continue
        entry_id = entry.get("entry_id")
        if not isinstance(entry_id, str) or not entry_id:
            skipped.append({"entry_id": "", "reason": "missing-entry-id"})
            continue
        try:
            models.append(_model_payload(entry))
        except (TypeError, ValueError):
            skipped.append({"entry_id": entry_id, "reason": "invalid-entry"})
    _merge_hf_cache_models(models)

    return {
        "models": models,
        "default_cache": str(registry.get("default_cache") or "hf"),
        "app_download_dir": _optional_str(registry.get("app_download_dir")),
        "skipped": skipped,
    }


def _merge_hf_cache_models(models: list[dict[str, Any]]) -> None:
    commit_index: dict[tuple[str, str], int] = {}
    revision_index: dict[tuple[str, str], int] = {}
    for index, model in enumerate(models):
        if model.get("source") != "hf_repo":
            continue
        repo_id = model.get("repo_id")
        if not isinstance(repo_id, str) or not repo_id:
            continue
        commit_sha = model.get("commit_sha")
        if isinstance(commit_sha, str) and commit_sha:
            commit_index[(repo_id, commit_sha)] = index
        revision = model.get("revision")
        if isinstance(revision, str) and revision:
            revision_index[(repo_id, revision)] = index

    for scanned in _cached_model_entries_from_hf_scan():
        payload = _model_payload(scanned)
        repo_id = payload.get("repo_id")
        commit_sha = payload.get("commit_sha")
        revision = payload.get("revision")
        index = None
        if isinstance(repo_id, str) and isinstance(commit_sha, str):
            index = commit_index.get((repo_id, commit_sha))
        if index is None and isinstance(repo_id, str) and isinstance(revision, str):
            index = revision_index.get((repo_id, revision))
        if index is None:
            index = len(models)
            models.append(payload)
            if isinstance(repo_id, str) and isinstance(commit_sha, str):
                commit_index[(repo_id, commit_sha)] = index
            if isinstance(repo_id, str) and isinstance(revision, str):
                revision_index[(repo_id, revision)] = index
            continue
        existing = models[index]
        existing["cache_state"] = "cached"
        existing["files"] = payload["files"]
        existing["size_bytes"] = payload["size_bytes"]
        if not existing.get("commit_sha"):
            existing["commit_sha"] = payload["commit_sha"]
        if not existing.get("revision"):
            existing["revision"] = payload["revision"]


def _matching_hf_cache_payload(
    repo_id: str, revision_or_commit: str | None
) -> dict[str, Any] | None:
    fallback: dict[str, Any] | None = None
    for entry in _cached_model_entries_from_hf_scan():
        payload = _model_payload(entry)
        if payload.get("repo_id") != repo_id:
            continue
        if fallback is None:
            fallback = payload
        if payload.get("commit_sha") == revision_or_commit:
            return payload
        if payload.get("revision") == revision_or_commit:
            return payload
    return fallback if not revision_or_commit else None


def _matching_hf_payload_for_entry(
    entry: dict[str, Any], payloads: list[dict[str, Any]]
) -> dict[str, Any] | None:
    repo_id = _optional_str(entry.get("repo_id"))
    if repo_id is None:
        return None
    commit_sha = _optional_str(entry.get("commit_sha"))
    revision = _optional_str(entry.get("revision"))
    fallback: dict[str, Any] | None = None
    for payload in payloads:
        if payload.get("repo_id") != repo_id:
            continue
        if fallback is None:
            fallback = payload
        if commit_sha is not None and payload.get("commit_sha") == commit_sha:
            return payload
        if revision is not None and payload.get("revision") == revision:
            return payload
    return fallback if commit_sha is None and revision is None else None


def _apply_cached_model_payload(entry: dict[str, Any], payload: dict[str, Any]) -> None:
    entry["cache_state"] = "cached"
    entry["files"] = dict(payload.get("files")) if isinstance(payload.get("files"), dict) else {}
    entry["size_bytes"] = int(payload.get("size_bytes") or 0)
    if payload.get("commit_sha"):
        entry["commit_sha"] = payload["commit_sha"]
    if not entry.get("revision") and payload.get("revision"):
        entry["revision"] = payload["revision"]


def _cached_model_entries_from_hf_scan() -> list[dict[str, Any]]:
    cache_info = _scan_hf_cache_info()
    if cache_info is None:
        return []

    entries: list[dict[str, Any]] = []
    for repo in getattr(cache_info, "repos", ()) or ():
        repo_type = _optional_str(getattr(repo, "repo_type", "model")) or "model"
        if repo_type != "model":
            continue
        repo_id = _optional_str(getattr(repo, "repo_id", None))
        if not repo_id:
            continue
        for revision in getattr(repo, "revisions", ()) or ():
            commit_sha = _optional_str(getattr(revision, "commit_hash", None))
            if not commit_sha:
                continue
            refs = sorted(
                str(ref)
                for ref in (getattr(revision, "refs", ()) or ())
                if str(ref)
            )
            files = list(getattr(revision, "files", ()) or ())
            size_bytes = _safe_int(getattr(revision, "size_on_disk", 0))
            entry_id = f"{repo_id}@{commit_sha[:12]}"
            entries.append(
                {
                    "entry_id": entry_id,
                    "display_name": repo_id,
                    "source": "hf_repo",
                    "repo_id": repo_id,
                    "revision": refs[0] if refs else commit_sha,
                    "commit_sha": commit_sha,
                    "local_path": None,
                    "url": None,
                    "quant_format": "none",
                    "tokenizer": None,
                    "files": _hf_cache_files_payload(files, size_bytes),
                    "size_bytes": size_bytes,
                    "cache_state": "cached",
                    "gated": False,
                    "token_required": False,
                    "created_at": None,
                    "last_used_at": None,
                    "notes": "",
                }
            )
    return entries


def _scan_hf_cache_info() -> object | None:
    try:
        from huggingface_hub import scan_cache_dir
    except ImportError:
        return None
    try:
        return scan_cache_dir()
    except Exception:
        return None


def _remove_model_weights(entry: dict[str, Any], reference: str) -> dict[str, object]:
    if entry.get("source") != "hf_repo":
        return {"removed_weights": False, "expected_freed_size": 0}
    if str(entry.get("cache_state") or "") != "cached":
        return {"removed_weights": False, "expected_freed_size": 0}
    commit_sha = _optional_str(entry.get("commit_sha"))
    if commit_sha is None:
        return {"removed_weights": False, "expected_freed_size": 0}
    cache_info = _scan_hf_cache_info()
    if cache_info is None:
        raise ModelRegistryError(
            "feature-unavailable",
            "huggingface_hub is required to remove cached model weights",
            {"model_ref": reference, "commit_sha": commit_sha},
        )
    delete_revisions = getattr(cache_info, "delete_revisions", None)
    if not callable(delete_revisions):
        raise ModelRegistryError(
            "feature-unavailable",
            "Hugging Face cache deletion is unavailable",
            {"model_ref": reference, "commit_sha": commit_sha},
        )
    strategy = delete_revisions(commit_sha)
    expected_freed_size = _safe_int(getattr(strategy, "expected_freed_size", 0))
    execute = getattr(strategy, "execute", None)
    if not callable(execute):
        raise ModelRegistryError(
            "feature-unavailable",
            "Hugging Face cache deletion execute hook is unavailable",
            {"model_ref": reference, "commit_sha": commit_sha},
        )
    execute()
    return {"removed_weights": True, "expected_freed_size": expected_freed_size}


def _snapshot_download(**kwargs: Any) -> str:
    from huggingface_hub import snapshot_download

    return str(snapshot_download(**kwargs))


def _snapshot_progress_tqdm_class(
    progress_callback: Callable[[dict[str, Any]], None],
) -> type:
    from tqdm.auto import tqdm

    class SnapshotProgressTqdm(tqdm):
        def update(self, n: int | float = 1) -> bool | None:
            result = super().update(n)
            _emit_snapshot_progress(self, progress_callback)
            return result

        def close(self) -> None:
            try:
                _emit_snapshot_progress(self, progress_callback)
            except Exception:
                pass
            super().close()

    return SnapshotProgressTqdm


def _emit_snapshot_progress(
    progress: object,
    progress_callback: Callable[[dict[str, Any]], None],
) -> None:
    bytes_done = _safe_int(getattr(progress, "n", 0))
    bytes_total_raw = getattr(progress, "total", None)
    bytes_total = _safe_int(bytes_total_raw)
    payload: dict[str, Any] = {"bytes_done": bytes_done}
    if bytes_total > 0:
        payload["bytes_total"] = bytes_total
        payload["percent"] = max(0, min(100, int(bytes_done * 100 / bytes_total)))
    progress_callback(payload)


def _snapshot_download_error(
    exc: Exception,
    *,
    model_ref: str,
    repo_id: str,
    revision: str | None,
) -> ModelRegistryError:
    kind = _classify_snapshot_download_error(exc)
    details: dict[str, Any] = {
        "model_ref": model_ref,
        "repo_id": repo_id,
    }
    if revision is not None:
        details["revision"] = revision
    if kind == "gated-auth":
        message = (
            f"Hugging Face access denied for {repo_id}; accept the model license "
            "and set HF_TOKEN if required"
        )
    elif kind == "revision-not-found":
        selected = revision or "requested revision"
        message = f"Hugging Face revision not found for {repo_id}: {selected}"
    elif kind == "disk-full":
        message = f"not enough disk space to download {repo_id}"
    elif kind == "network":
        message = f"network error downloading {repo_id}: {exc}"
    elif kind == "integrity-mismatch":
        message = f"integrity mismatch while downloading {repo_id}: {exc}"
    else:
        message = f"unable to download model {repo_id}: {exc}"
    return ModelRegistryError(kind, message, details)


def _classify_snapshot_download_error(exc: Exception) -> str:
    text = f"{type(exc).__module__}.{type(exc).__name__} {exc}".lower()
    if any(
        marker in text
        for marker in (
            "gatedrepoerror",
            "gated repo",
            "access denied",
            "unauthorized",
            "forbidden",
            "401",
            "403",
        )
    ):
        return "gated-auth"
    if "revisionnotfound" in text or (
        "revision" in text and ("not found" in text or "does not exist" in text)
    ):
        return "revision-not-found"
    if any(
        marker in text
        for marker in (
            "errno 28",
            "no space left",
            "disk full",
            "not enough space",
        )
    ):
        return "disk-full"
    if any(
        marker in text
        for marker in (
            "hash mismatch",
            "integrity",
            "checksum",
            "sha256",
            "etag mismatch",
        )
    ):
        return "integrity-mismatch"
    if any(
        marker in text
        for marker in (
            "connection",
            "connect timeout",
            "read timeout",
            "timed out",
            "timeout",
            "network",
            "temporarily unavailable",
            "max retries",
        )
    ):
        return "network"
    return "model-download-failed"


def _hf_cache_files_payload(files: list[object], total_size: int) -> dict[str, Any]:
    file_names = [
        str(getattr(file_info, "file_name", "") or "")
        for file_info in files
        if str(getattr(file_info, "file_name", "") or "")
    ]
    counted_size = sum(_safe_int(getattr(file_info, "size_on_disk", 0)) for file_info in files)
    return {
        "count": len(file_names),
        "total_bytes": total_size or counted_size,
        "weights_format": _weights_format_from_names(file_names),
    }


def _weights_format_from_names(names: list[str]) -> str:
    suffixes = {Path(name).suffix for name in names}
    if ".safetensors" in suffixes:
        return "safetensors"
    if ".gguf" in suffixes:
        return "gguf"
    if ".bin" in suffixes:
        return "bin"
    return "unknown"


def _safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _load_registry_or_raise(path: Path, reference: str) -> dict[str, Any]:
    try:
        registry = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ModelRegistryError(
            "model-not-found",
            f"invalid model registry for {reference}",
            {"model_ref": reference, "reason": "invalid-json"},
        ) from exc
    except OSError as exc:
        raise ModelRegistryError(
            "model-not-found",
            f"unable to read model registry for {reference}",
            {"model_ref": reference, "reason": "unreadable"},
        ) from exc
    if not isinstance(registry, dict):
        raise ModelRegistryError(
            "model-not-found",
            f"invalid model registry for {reference}",
            {"model_ref": reference, "reason": "invalid-registry"},
        )
    return registry


def _load_registry_for_write(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": 1, "default_cache": "hf", "app_download_dir": None, "entries": []}
    try:
        registry = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ModelRegistryError(
            "invalid-config",
            "invalid model registry",
            {"reason": "invalid-json"},
        ) from exc
    except OSError as exc:
        raise ModelRegistryError(
            "invalid-config",
            "unable to read model registry",
            {"reason": "unreadable"},
        ) from exc
    if not isinstance(registry, dict):
        raise ModelRegistryError(
            "invalid-config",
            "invalid model registry",
            {"reason": "invalid-registry"},
        )
    return registry


def _entry_for_reference(registry: dict[str, Any], reference: str) -> dict[str, Any]:
    entries = registry.get("entries") or []
    if not isinstance(entries, list):
        raise ModelRegistryError(
            "model-not-found",
            f"model registry has no valid entries for {reference}",
            {"model_ref": reference, "reason": "invalid-entries"},
        )
    entry_id_matches: list[dict[str, Any]] = []
    display_name_matches: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("entry_id") == reference:
            entry_id_matches.append(entry)
        if entry.get("display_name") == reference:
            display_name_matches.append(entry)
    if len(entry_id_matches) == 1:
        return entry_id_matches[0]
    if len(entry_id_matches) > 1:
        raise ModelRegistryError(
            "model-not-found",
            f"model entry id is ambiguous: {reference}",
            {"model_ref": reference},
        )
    if len(display_name_matches) == 1:
        return display_name_matches[0]
    if len(display_name_matches) > 1:
        raise ModelRegistryError(
            "model-not-found",
            f"model display name is ambiguous: {reference}",
            {"model_ref": reference},
        )
    raise ModelRegistryError(
        "model-not-found",
        f"unknown model reference: {reference}",
        {"model_ref": reference},
    )


def _handoff_from_entry(reference: str, entry: dict[str, Any]) -> ModelHandoff:
    entry_id = _required_str(entry, "entry_id", reference)
    source = _required_str(entry, "source", reference)
    display_name = _optional_str(entry.get("display_name")) or entry_id
    revision = _optional_str(entry.get("commit_sha")) or _optional_str(entry.get("revision"))
    tokenizer = _optional_str(entry.get("tokenizer"))
    repo_id: str | None = None
    local_path: str | None = None
    if source == "hf_repo":
        repo_id = _required_str(entry, "repo_id", reference)
        model_arg = repo_id
    elif source == "local_path":
        local_path = str(Path(_required_str(entry, "local_path", reference)).expanduser())
        model_arg = local_path
        revision = None
    else:
        raise ModelRegistryError(
            "model-not-found",
            f"model {reference} has unsupported source: {source}",
            {"model_ref": reference, "source": source},
        )
    return ModelHandoff(
        reference=reference,
        entry_id=entry_id,
        display_name=display_name,
        source=source,
        model_arg=model_arg,
        revision=revision,
        tokenizer=tokenizer,
        repo_id=repo_id,
        local_path=local_path,
        commit_sha=_optional_str(entry.get("commit_sha")),
        cache_state=_optional_str(entry.get("cache_state")),
        gated=bool(entry.get("gated")),
        token_required=bool(entry.get("token_required")),
    )


def _pin_entry_from_params(params: dict[str, Any]) -> dict[str, Any]:
    entry_id = _required_param(params, "entry_id")
    now = _utc_now()
    source = str(params.get("source") or "hf_repo")
    if source == "local_path":
        return _local_path_entry_from_params(params, entry_id, now)
    if source != "hf_repo":
        raise ModelRegistryError(
            "invalid-config",
            f"unsupported model source: {source}",
            {"reason": "unsupported-source", "source": source},
        )
    return _hf_repo_entry_from_params(params, entry_id, now)


def _hf_repo_entry_from_params(
    params: dict[str, Any], entry_id: str, now: str
) -> dict[str, Any]:
    repo_id = _required_param(params, "repo_id")
    revision = _optional_str(params.get("revision"))
    commit_sha = _optional_str(params.get("commit_sha"))
    info = (
        _hf_model_info(repo_id, revision)
        if revision is not None and commit_sha is None
        else None
    )
    if info is not None:
        commit_sha = _optional_str(getattr(info, "sha", None)) or commit_sha
    gated = bool(params.get("gated"))
    token_required = bool(params.get("token_required"))
    if info is not None and _hf_model_info_is_gated(getattr(info, "gated", None)):
        gated = True
        token_required = True
    entry: dict[str, Any] = {
        "entry_id": entry_id,
        "display_name": _optional_str(params.get("display_name")) or entry_id,
        "source": "hf_repo",
        "repo_id": repo_id,
        "revision": revision,
        "commit_sha": commit_sha,
        "local_path": None,
        "url": None,
        "quant_format": _optional_str(params.get("quant_format")) or "none",
        "tokenizer": _optional_str(params.get("tokenizer")),
        "files": {},
        "size_bytes": 0,
        "cache_state": _optional_str(params.get("cache_state")) or "remote_only",
        "gated": gated,
        "token_required": token_required,
        "created_at": _optional_str(params.get("created_at")) or now,
        "last_used_at": _optional_str(params.get("last_used_at")),
        "notes": str(params.get("notes") or ""),
    }
    return entry


def _hf_model_info(repo_id: str, revision: str | None = None) -> object | None:
    try:
        from huggingface_hub import HfApi
    except Exception:
        return None

    kwargs: dict[str, Any] = {"repo_id": repo_id}
    if revision is not None:
        kwargs["revision"] = revision
    try:
        return HfApi().model_info(**kwargs)
    except Exception:
        return None


def _hf_model_info_is_gated(value: Any) -> bool:
    if isinstance(value, str):
        return value.lower() not in {"", "0", "false", "none"}
    return bool(value)


def _local_path_entry_from_params(
    params: dict[str, Any], entry_id: str, now: str
) -> dict[str, Any]:
    local_path = _verified_local_model_path(_required_param(params, "local_path"))
    files = _local_model_files_payload(local_path)
    return {
        "entry_id": entry_id,
        "display_name": _optional_str(params.get("display_name")) or local_path.name,
        "source": "local_path",
        "repo_id": None,
        "revision": None,
        "commit_sha": None,
        "local_path": str(local_path),
        "url": None,
        "quant_format": _optional_str(params.get("quant_format")) or "none",
        "tokenizer": _optional_str(params.get("tokenizer")),
        "files": files,
        "size_bytes": 0,
        "cache_state": "cached",
        "gated": False,
        "token_required": False,
        "integrity": _local_model_integrity_payload(local_path),
        "created_at": _optional_str(params.get("created_at")) or now,
        "last_used_at": _optional_str(params.get("last_used_at")),
        "notes": str(params.get("notes") or ""),
    }


def _verified_local_model_path(value: str) -> Path:
    path = Path(value).expanduser()
    status = _local_model_status(path)
    if not status["ok"]:
        raise ModelRegistryError(
            "invalid-config",
            str(status["detail"]),
            {"reason": str(status["reason"]), "local_path": str(path)},
        )
    return path


def _verify_local_model_entry(entry: dict[str, Any]) -> dict[str, Any]:
    entry_id = str(entry.get("entry_id") or "")
    local_path = Path(str(entry.get("local_path") or "")).expanduser()
    status = _local_model_status(local_path)
    entry["cache_state"] = status["cache_state"]
    if status["ok"]:
        current_integrity = _local_model_integrity_payload(local_path)
        previous_integrity = entry.get("integrity")
        expected_files_sha256 = (
            previous_integrity.get("files_sha256")
            if isinstance(previous_integrity, dict)
            else None
        )
        current_files_sha256 = current_integrity["files_sha256"]
        if expected_files_sha256 and expected_files_sha256 != current_files_sha256:
            entry["cache_state"] = "partial"
            return {
                "entry_id": entry_id,
                "ok": False,
                "reason": "integrity-mismatch",
                "cache_state": "partial",
                "detail": "local model integrity mismatch",
                "integrity": {
                    "expected_files_sha256": expected_files_sha256,
                    "current_files_sha256": current_files_sha256,
                    "expected_file_count": _safe_int(previous_integrity.get("file_count"))
                    if isinstance(previous_integrity, dict)
                    else 0,
                    "current_file_count": _safe_int(current_integrity.get("file_count")),
                },
                "entry": _model_payload(entry),
            }
        entry["files"] = _local_model_files_payload(local_path)
        entry["integrity"] = current_integrity
    payload = {
        "entry_id": entry_id,
        "ok": bool(status["ok"]),
        "cache_state": str(status["cache_state"]),
        "detail": str(status["detail"]),
        "entry": _model_payload(entry),
    }
    if not status["ok"]:
        payload["reason"] = str(status["reason"])
    return payload


def _verify_metadata_model_entry(entry: dict[str, Any]) -> dict[str, Any]:
    entry_id = str(entry.get("entry_id") or "")
    status = _hf_model_status(entry)
    entry["cache_state"] = status["cache_state"]
    payload = {
        "entry_id": entry_id,
        "ok": bool(status["ok"]),
        "cache_state": str(status["cache_state"]),
        "detail": str(status["detail"]),
        "entry": _model_payload(entry),
    }
    if not status["ok"]:
        payload["reason"] = str(status["reason"])
    return payload


def _hf_model_status(entry: dict[str, Any]) -> dict[str, object]:
    cache_state = str(entry.get("cache_state") or "remote_only")
    if cache_state != "cached":
        return {
            "ok": False,
            "reason": cache_state,
            "cache_state": cache_state,
            "detail": f"model is {cache_state}",
        }
    if not _optional_str(entry.get("commit_sha")):
        return {
            "ok": False,
            "reason": "missing-commit",
            "cache_state": "partial",
            "detail": "cached model metadata is missing commit identity",
        }
    files = entry.get("files")
    if not isinstance(files, dict) or _safe_int(files.get("count")) <= 0:
        return {
            "ok": False,
            "reason": "missing-files",
            "cache_state": "partial",
            "detail": "cached model metadata is missing file inventory",
        }
    if str(files.get("weights_format") or "unknown") == "unknown":
        return {
            "ok": False,
            "reason": "missing-weights",
            "cache_state": "partial",
            "detail": "cached model metadata is missing weight files",
        }
    return {
        "ok": True,
        "reason": "cached",
        "cache_state": "cached",
        "detail": "model metadata is cached",
    }


def _local_model_status(path: Path) -> dict[str, object]:
    if not path.exists() or not path.is_dir():
        return {
            "ok": False,
            "reason": "missing-local-path",
            "cache_state": "missing",
            "detail": f"local model path not found: {path}",
        }
    if not (path / "config.json").exists():
        return {
            "ok": False,
            "reason": "missing-config",
            "cache_state": "partial",
            "detail": f"local model path is missing config.json: {path}",
        }
    if not _local_weight_files(path):
        return {
            "ok": False,
            "reason": "missing-weights",
            "cache_state": "partial",
            "detail": f"local model path is missing model weights: {path}",
        }
    if not _local_tokenizer_files(path):
        return {
            "ok": False,
            "reason": "missing-tokenizer",
            "cache_state": "partial",
            "detail": f"local model path is missing tokenizer files: {path}",
        }
    return {
        "ok": True,
        "reason": None,
        "cache_state": "cached",
        "detail": "local model verified",
    }


def _local_model_files_payload(path: Path) -> dict[str, Any]:
    weights = _local_weight_files(path)
    tokenizer_files = _local_tokenizer_files(path)
    return {
        "count": 1 + len(weights) + len(tokenizer_files),
        "weights_format": _weights_format(weights),
    }


def _local_model_integrity_payload(path: Path) -> dict[str, Any]:
    files = _local_model_integrity_files(path)
    digest = sha256()
    total_bytes = 0
    blob_hashes: dict[str, str] = {}
    for file in files:
        relative = file.relative_to(path).as_posix()
        data = file.read_bytes()
        total_bytes += len(data)
        blob_hashes[relative] = _sha256_uri(data)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(data)).encode("ascii"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    return {
        "strategy": "local_files_sha256",
        "files_sha256": f"sha256:{digest.hexdigest()}",
        "file_count": len(files),
        "total_bytes": total_bytes,
        "blob_hashes": blob_hashes,
    }


def _local_model_integrity_files(path: Path) -> list[Path]:
    files = [path / "config.json", *_local_weight_files(path), *_local_tokenizer_files(path)]
    unique = {file.resolve(): file for file in files}
    return sorted(unique.values(), key=lambda file: file.relative_to(path).as_posix())


def _local_weight_files(path: Path) -> list[Path]:
    patterns = ("*.safetensors", "*.bin", "*.gguf")
    return sorted(file for pattern in patterns for file in path.glob(pattern))


def _local_tokenizer_files(path: Path) -> list[Path]:
    names = (
        "tokenizer.json",
        "tokenizer.model",
        "vocab.json",
        "merges.txt",
        "special_tokens_map.json",
    )
    return [path / name for name in names if (path / name).exists()]


def _weights_format(weights: list[Path]) -> str:
    if any(path.suffix == ".safetensors" for path in weights):
        return "safetensors"
    if any(path.suffix == ".gguf" for path in weights):
        return "gguf"
    return "bin"


def _required_param(params: dict[str, Any], field: str) -> str:
    value = params.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ModelRegistryError(
            "invalid-config",
            f"pin_model requires {field}",
            {"reason": f"missing-{field}"},
        )
    return value


def _required_str(entry: dict[str, Any], field: str, reference: str) -> str:
    value = entry.get(field)
    if not isinstance(value, str) or not value:
        raise ModelRegistryError(
            "model-not-found",
            f"model {reference} is missing {field}",
            {"model_ref": reference, "reason": f"missing-{field}"},
        )
    return value


def _write_registry_atomic(path: Path, registry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(
        json.dumps(registry, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _model_payload(entry: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for field in MODEL_ENTRY_FIELDS:
        value = entry.get(field)
        if field == "files":
            payload[field] = dict(value) if isinstance(value, dict) else {}
        elif field == "integrity":
            if isinstance(value, dict) and value:
                payload[field] = dict(value)
        elif field in {"gated", "token_required"}:
            payload[field] = bool(value)
        elif field == "size_bytes":
            payload[field] = int(value or 0)
        else:
            payload[field] = _optional_str(value)
    return payload


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _sha256_uri(data: bytes) -> str:
    return f"sha256:{sha256(data).hexdigest()}"
