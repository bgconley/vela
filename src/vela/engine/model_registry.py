from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from vela.engine.ids import mint_ulid

HASH_CHUNK_BYTES = 1024 * 1024


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
    url: str | None
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
        if self.url is not None:
            payload["model_url"] = self.url
        if self.commit_sha is not None:
            payload["model_commit_sha"] = self.commit_sha
        if self.tokenizer is not None:
            payload["model_tokenizer"] = self.tokenizer
        return payload

    def env_contribution(self) -> dict[str, str]:
        env: dict[str, str] = {}
        if self.source == "hf_repo" and self.token_required:
            token = os.environ.get("HF_TOKEN")
            if token:
                env["HF_TOKEN"] = token
        return env


MODEL_ENTRY_FIELDS = (
    "entry_id",
    "display_name",
    "aliases",
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
    "unique_size_bytes",
    "nominal_size_bytes",
    "cache_state",
    "gated",
    "token_required",
    "integrity",
    "allow_patterns",
    "ignore_patterns",
    "created_at",
    "last_used_at",
    "notes",
)


def default_models_registry_path() -> Path:
    state_home = os.environ.get("XDG_STATE_HOME")
    root = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return root / "vela" / "models" / "registry.json"


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
    for _attempt in range(16):
        entry = _pin_entry_from_params(params)
        entry_id = _required_str(entry, "entry_id", "new model entry")
        with _entry_lock(path, entry_id):
            with _registry_lock(path):
                registry = _load_registry_for_write(path)
                entries = registry.get("entries") or []
                if not isinstance(entries, list):
                    entries = []
                if any(
                    isinstance(item, dict) and item.get("entry_id") == entry_id
                    for item in entries
                ):
                    continue
                return _pin_model_entry_payload(entry, path, registry, entries)
    raise ModelRegistryError(
        "resource-in-use",
        "unable to mint an unused model entry id",
        {"reason": "model-entry-id-collision"},
    )


def _pin_model_entry_payload(
    entry: dict[str, Any],
    path: Path,
    registry: dict[str, Any],
    entries: list[object],
) -> dict[str, Any]:
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


def verify_model(
    reference: str, registry_path: str | Path | None = None, *, deep: bool = False
) -> dict[str, Any]:
    path = (
        Path(registry_path).expanduser()
        if registry_path is not None
        else default_models_registry_path()
    )
    entry_id = _entry_id_for_reference(path, reference)
    with _entry_lock(path, entry_id):
        with _registry_lock(path):
            return _verify_model_locked(reference, path, deep=deep)


def _verify_model_locked(reference: str, path: Path, *, deep: bool = False) -> dict[str, Any]:
    registry = _load_registry_for_write(path)
    entry = _entry_for_reference(registry, reference)
    if entry.get("source") == "local_path":
        result = _verify_local_model_entry(entry, deep=deep)
    else:
        result = _verify_metadata_model_entry(entry, deep=deep)
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
    entry_id = _entry_id_for_reference(path, reference)
    with _entry_lock(path, entry_id):
        with _registry_lock(path):
            download_kwargs, repo_id, selected_revision = _prepare_hf_model_download(
                reference,
                path,
                revision=revision,
                allow_patterns=allow_patterns,
                ignore_patterns=ignore_patterns,
                progress_callback=progress_callback,
            )

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
        with _registry_lock(path):
            registry = _load_registry_for_write(path)
            entry = _entry_for_reference(registry, reference)
            _apply_cached_model_payload(entry, cached)
            registry["schema_version"] = 1
            registry["default_cache"] = str(registry.get("default_cache") or "hf")
            registry.setdefault("app_download_dir", None)
            _write_registry_atomic(path, registry)
            entry_id = str(entry.get("entry_id") or "")
            entry_payload = _model_payload(entry)
        return {
            "entry_id": entry_id,
            "ok": True,
            "cache_state": "cached",
            "detail": "model cached",
            "snapshot_path": str(snapshot_path),
            "entry": entry_payload,
        }


def _prepare_hf_model_download(
    reference: str,
    path: Path,
    *,
    revision: str | None = None,
    allow_patterns: list[str] | None = None,
    ignore_patterns: list[str] | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[dict[str, Any], str, str | None]:
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
    return download_kwargs, repo_id, selected_revision


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
    entry_id = _entry_id_for_reference(path, reference)
    with _entry_lock(path, entry_id):
        with _registry_lock(path):
            return _mark_hf_model_partial_locked(
                reference,
                path,
                allow_patterns=allow_patterns,
                ignore_patterns=ignore_patterns,
            )


def _mark_hf_model_partial_locked(
    reference: str,
    path: Path,
    *,
    allow_patterns: list[str] | None = None,
    ignore_patterns: list[str] | None = None,
) -> dict[str, Any]:
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
    with _registry_lock(path):
        return _refresh_models_locked(path)


def _refresh_models_locked(path: Path) -> dict[str, Any]:
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
    aliases.update(_model_aliases_from_entry(entry))
    return aliases


def remove_model(reference: str, registry_path: str | Path | None = None) -> dict[str, Any]:
    path = (
        Path(registry_path).expanduser()
        if registry_path is not None
        else default_models_registry_path()
    )
    entry_id = _entry_id_for_reference(path, reference)
    with _entry_lock(path, entry_id):
        with _registry_lock(path):
            return _remove_model_locked(reference, path)


def _remove_model_locked(reference: str, path: Path) -> dict[str, Any]:
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


def list_models(
    registry_path: str | Path | None = None,
    *,
    cached_only: bool = False,
    pinned_only: bool = False,
) -> dict[str, Any]:
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
    pinned_entry_ids: set[str] = set()
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
            payload = _model_payload(entry)
            models.append(payload)
            pinned_entry_ids.add(str(payload.get("entry_id") or entry_id))
        except (TypeError, ValueError):
            skipped.append({"entry_id": entry_id, "reason": "invalid-entry"})
    _merge_hf_cache_models(models)
    if pinned_only:
        models = [
            model
            for model in models
            if str(model.get("entry_id") or "") in pinned_entry_ids
        ]
    if cached_only:
        models = [
            model
            for model in models
            if str(model.get("cache_state") or "") == "cached"
        ]

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
        existing["unique_size_bytes"] = payload.get("unique_size_bytes")
        existing["nominal_size_bytes"] = payload.get("nominal_size_bytes")
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
    entry["unique_size_bytes"] = int(payload.get("unique_size_bytes") or 0)
    entry["nominal_size_bytes"] = int(payload.get("nominal_size_bytes") or 0)
    if payload.get("commit_sha"):
        entry["commit_sha"] = payload["commit_sha"]
    if not entry.get("revision") and payload.get("revision"):
        entry["revision"] = payload["revision"]


def _cached_model_entries_from_hf_scan() -> list[dict[str, Any]]:
    cache_info = _scan_hf_cache_info()
    if cache_info is None:
        return []
    return _cached_model_entries_from_cache_info(cache_info)


def _cached_model_payloads_from_hf_scan() -> list[dict[str, Any]] | None:
    cache_info = _scan_hf_cache_info()
    if cache_info is None:
        return None
    return [_model_payload(entry) for entry in _cached_model_entries_from_cache_info(cache_info)]


def _cached_model_entries_from_cache_info(cache_info: object) -> list[dict[str, Any]]:
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
            unique_size_bytes = _safe_int(getattr(revision, "size_on_disk", 0))
            nominal_size_bytes = _hf_cache_nominal_size(files)
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
                    "files": _hf_cache_files_payload(
                        files,
                        unique_size_bytes=unique_size_bytes,
                        nominal_size_bytes=nominal_size_bytes,
                    ),
                    "size_bytes": unique_size_bytes,
                    "unique_size_bytes": unique_size_bytes,
                    "nominal_size_bytes": nominal_size_bytes,
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


def _hf_cache_nominal_size(files: list[object]) -> int:
    return sum(_safe_int(getattr(file_info, "size_on_disk", 0)) for file_info in files)


def _hf_cache_files_payload(
    files: list[object], *, unique_size_bytes: int, nominal_size_bytes: int
) -> dict[str, Any]:
    file_names = [
        str(getattr(file_info, "file_name", "") or "")
        for file_info in files
        if str(getattr(file_info, "file_name", "") or "")
    ]
    return {
        "count": len(file_names),
        "total_bytes": unique_size_bytes or nominal_size_bytes,
        "unique_bytes": unique_size_bytes,
        "nominal_bytes": nominal_size_bytes,
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
    alias_matches: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("entry_id") == reference:
            entry_id_matches.append(entry)
        if entry.get("display_name") == reference:
            display_name_matches.append(entry)
        if reference in _model_aliases_from_entry(entry):
            alias_matches.append(entry)
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
    if len(alias_matches) == 1:
        return alias_matches[0]
    if len(alias_matches) > 1:
        raise ModelRegistryError(
            "model-not-found",
            f"model alias is ambiguous: {reference}",
            {"model_ref": reference},
        )
    raise ModelRegistryError(
        "model-not-found",
        f"unknown model reference: {reference}",
        {"model_ref": reference},
    )


def _entry_id_for_reference(path: Path, reference: str) -> str:
    with _registry_lock(path):
        registry = _load_registry_or_raise(path, reference)
        entry = _entry_for_reference(registry, reference)
        return _required_str(entry, "entry_id", reference)


def _handoff_from_entry(reference: str, entry: dict[str, Any]) -> ModelHandoff:
    entry_id = _required_str(entry, "entry_id", reference)
    source = _required_str(entry, "source", reference)
    display_name = _optional_str(entry.get("display_name")) or entry_id
    revision = _optional_str(entry.get("commit_sha")) or _optional_str(entry.get("revision"))
    tokenizer = _optional_str(entry.get("tokenizer"))
    repo_id: str | None = None
    local_path: str | None = None
    url: str | None = None
    if source == "hf_repo":
        repo_id = _required_str(entry, "repo_id", reference)
        model_arg = repo_id
    elif source == "local_path":
        local_path = str(Path(_required_str(entry, "local_path", reference)).expanduser())
        model_arg = local_path
        revision = None
    elif source == "url":
        url = _required_str(entry, "url", reference)
        model_arg = url
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
        url=url,
        commit_sha=_optional_str(entry.get("commit_sha")),
        cache_state=_optional_str(entry.get("cache_state")),
        gated=bool(entry.get("gated")),
        token_required=bool(entry.get("token_required")),
    )


def _pin_entry_from_params(
    params: dict[str, Any], entries: list[object] | None = None
) -> dict[str, Any]:
    requested_entry_id = _optional_str(params.get("entry_id"))
    effective_params = dict(params)
    if requested_entry_id is not None and _optional_str(params.get("display_name")) is None:
        effective_params["display_name"] = requested_entry_id
    entry_id = _entry_id_from_params(entries or [])
    now = _utc_now()
    source = str(effective_params.get("source") or "hf_repo")
    if source == "local_path":
        return _local_path_entry_from_params(effective_params, entry_id, now)
    if source == "url":
        return _url_entry_from_params(effective_params, entry_id, now)
    if source != "hf_repo":
        raise ModelRegistryError(
            "invalid-config",
            f"unsupported model source: {source}",
            {"reason": "unsupported-source", "source": source},
        )
    return _hf_repo_entry_from_params(effective_params, entry_id, now)


def _hf_repo_entry_from_params(
    params: dict[str, Any], entry_id: str, now: str
) -> dict[str, Any]:
    repo_id = _required_param(params, "repo_id")
    revision = _optional_str(params.get("revision"))
    commit_sha = _optional_str(params.get("commit_sha"))
    info = _resolved_hf_model_info(params, entry_id, repo_id, revision, commit_sha)
    if info is not None:
        commit_sha = _optional_str(getattr(info, "sha", None)) or commit_sha
    if commit_sha is None:
        model_ref = _optional_str(params.get("entry_id")) or entry_id
        raise ModelRegistryError(
            "model-unavailable",
            (
                f"model {repo_id} is missing an immutable Hugging Face commit sha; "
                "re-pin the model before launch"
            ),
            {
                "model_ref": model_ref,
                "repo_id": repo_id,
                "revision": revision,
                "reason": "missing-commit",
            },
        )
    gated = bool(params.get("gated"))
    token_required = bool(params.get("token_required"))
    if info is not None and _hf_model_info_is_gated(getattr(info, "gated", None)):
        gated = True
        token_required = True
    entry: dict[str, Any] = {
        "entry_id": entry_id,
        "display_name": _optional_str(params.get("display_name")) or entry_id,
        "aliases": _model_aliases(params, entry_id),
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


def _resolved_hf_model_info(
    params: dict[str, Any],
    entry_id: str,
    repo_id: str,
    revision: str | None,
    commit_sha: str | None,
) -> object | None:
    if commit_sha is not None:
        return None
    model_ref = _optional_str(params.get("entry_id")) or entry_id
    try:
        return _hf_model_info(repo_id, revision)
    except Exception as exc:
        raise _hf_model_info_error(
            exc,
            model_ref=model_ref,
            repo_id=repo_id,
            revision=revision,
        ) from exc


def _hf_model_info(repo_id: str, revision: str | None = None) -> object | None:
    try:
        from huggingface_hub import HfApi
    except Exception as exc:
        raise ModelRegistryError(
            "feature-unavailable",
            "huggingface_hub is required to resolve Hugging Face model revisions",
            {"repo_id": repo_id, "revision": revision, "reason": "missing-huggingface-hub"},
        ) from exc

    kwargs: dict[str, Any] = {"repo_id": repo_id}
    if revision is not None:
        kwargs["revision"] = revision
    return HfApi().model_info(**kwargs)


def _hf_model_info_error(
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
        "reason": kind,
    }
    if revision is not None:
        details["revision"] = revision
    if kind == "gated-auth":
        message = (
            f"Hugging Face access denied for {repo_id}; accept the model license "
            "and set HF_TOKEN if required"
        )
    elif kind == "revision-not-found":
        selected = revision or "default revision"
        message = f"Hugging Face revision not found for {repo_id}: {selected}"
    elif kind == "network":
        message = f"network error resolving Hugging Face metadata for {repo_id}: {exc}"
    else:
        message = f"unable to resolve Hugging Face metadata for {repo_id}: {exc}"
    return ModelRegistryError(kind, message, details)



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
        "aliases": _model_aliases(params, entry_id),
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


def _url_entry_from_params(
    params: dict[str, Any], entry_id: str, now: str
) -> dict[str, Any]:
    url = _validated_model_url(_required_param(params, "url"))
    display_name = _optional_str(params.get("display_name")) or _model_url_basename(url)
    return {
        "entry_id": entry_id,
        "display_name": display_name or entry_id,
        "aliases": _model_aliases(params, entry_id),
        "source": "url",
        "repo_id": None,
        "revision": None,
        "commit_sha": None,
        "local_path": None,
        "url": url,
        "quant_format": _optional_str(params.get("quant_format")) or "none",
        "tokenizer": _optional_str(params.get("tokenizer")),
        "files": {},
        "size_bytes": 0,
        "cache_state": _optional_str(params.get("cache_state")) or "remote_only",
        "gated": bool(params.get("gated")),
        "token_required": bool(params.get("token_required")),
        "created_at": _optional_str(params.get("created_at")) or now,
        "last_used_at": _optional_str(params.get("last_used_at")),
        "notes": str(params.get("notes") or ""),
    }


def _validated_model_url(value: str) -> str:
    url = str(value).strip()
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ModelRegistryError(
            "invalid-config",
            f"invalid model URL: {value}",
            {"reason": "invalid-url", "url": value},
        )
    return url


def _model_url_basename(url: str) -> str:
    parsed = urlsplit(url)
    name = Path(parsed.path).name
    return name or parsed.netloc


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


def _verify_local_model_entry(entry: dict[str, Any], *, deep: bool = False) -> dict[str, Any]:
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
            payload = {
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
            if deep:
                payload["deep"] = True
            return payload
        if deep:
            current_integrity["deep"] = True
        entry["files"] = _local_model_files_payload(local_path)
        entry["integrity"] = current_integrity
    payload = {
        "entry_id": entry_id,
        "ok": bool(status["ok"]),
        "cache_state": str(status["cache_state"]),
        "detail": str(status["detail"]),
        "entry": _model_payload(entry),
    }
    if deep:
        payload["deep"] = True
        if payload["ok"]:
            payload["detail"] = "local model deep verified"
    if not status["ok"]:
        payload["reason"] = str(status["reason"])
    return payload


def _verify_metadata_model_entry(entry: dict[str, Any], *, deep: bool = False) -> dict[str, Any]:
    entry_id = str(entry.get("entry_id") or "")
    status = _verify_hf_model_status(entry)
    entry["cache_state"] = status["cache_state"]
    if bool(status["ok"]) and deep and entry.get("source") == "hf_repo":
        current_integrity = _hf_model_integrity_payload(entry)
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
                "detail": "hf model integrity mismatch",
                "deep": True,
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
        current_integrity["deep"] = True
        entry["integrity"] = current_integrity
    payload = {
        "entry_id": entry_id,
        "ok": bool(status["ok"]),
        "cache_state": str(status["cache_state"]),
        "detail": str(status["detail"]),
        "entry": _model_payload(entry),
    }
    if deep:
        payload["deep"] = True
        if payload["ok"] and entry.get("source") == "hf_repo":
            payload["detail"] = "hf model deep verified"
    if not status["ok"]:
        payload["reason"] = str(status["reason"])
    return payload


def _verify_hf_model_status(entry: dict[str, Any]) -> dict[str, object]:
    if str(entry.get("cache_state") or "") != "cached":
        return _hf_model_status(entry)
    cached_payloads = _cached_model_payloads_from_hf_scan()
    if cached_payloads is None:
        return _hf_model_status(entry)
    cached = _matching_hf_payload_for_entry(entry, cached_payloads)
    if cached is None:
        entry["cache_state"] = "missing"
        return {
            "ok": False,
            "reason": "missing-cache-entry",
            "cache_state": "missing",
            "detail": "cached model was not found in the Hugging Face cache",
        }
    _apply_cached_model_payload(entry, cached)
    return _hf_model_status(entry)


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


def _hf_model_integrity_payload(entry: dict[str, Any]) -> dict[str, Any]:
    revision = _hf_cache_revision_for_entry(entry)
    files = sorted(
        list(getattr(revision, "files", ()) or ()),
        key=lambda item: str(getattr(item, "file_name", "")),
    )
    digest = sha256()
    total_bytes = 0
    blob_hashes: dict[str, str] = {}
    for cached_file in files:
        relative = _optional_str(getattr(cached_file, "file_name", None))
        path = _hf_cache_file_path(cached_file)
        if relative is None or path is None:
            raise ModelRegistryError(
                "feature-unavailable",
                "Hugging Face cache file paths are required for deep verification",
                {"model_ref": str(entry.get("entry_id") or ""), "reason": "missing-file-path"},
            )
        if not path.exists() or not path.is_file():
            raise ModelRegistryError(
                "model-not-found",
                f"Hugging Face cache file is missing: {relative}",
                {
                    "model_ref": str(entry.get("entry_id") or ""),
                    "path": str(path),
                    "reason": "missing-cache-file",
                },
            )
        file_size = path.stat().st_size
        total_bytes += file_size
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(file_size).encode("ascii"))
        digest.update(b"\0")
        blob_hashes[relative] = _stream_file_sha256_uri(path, aggregate=digest)
        digest.update(b"\0")
    return {
        "strategy": "hf_cache_blob_sha256",
        "files_sha256": f"sha256:{digest.hexdigest()}",
        "file_count": len(files),
        "total_bytes": total_bytes,
        "blob_hashes": blob_hashes,
    }


def _hf_cache_revision_for_entry(entry: dict[str, Any]) -> object:
    cache_info = _scan_hf_cache_info()
    if cache_info is None:
        raise ModelRegistryError(
            "feature-unavailable",
            "Hugging Face cache scan is required for deep verification",
            {"model_ref": str(entry.get("entry_id") or ""), "reason": "cache-scan-unavailable"},
        )
    repo_id = _optional_str(entry.get("repo_id"))
    commit_sha = _optional_str(entry.get("commit_sha"))
    revision_ref = _optional_str(entry.get("revision"))
    for repo in getattr(cache_info, "repos", ()) or ():
        if _optional_str(getattr(repo, "repo_id", None)) != repo_id:
            continue
        for revision in getattr(repo, "revisions", ()) or ():
            cached_commit = _optional_str(getattr(revision, "commit_hash", None))
            refs = {
                str(ref)
                for ref in (getattr(revision, "refs", ()) or ())
                if str(ref)
            }
            if commit_sha is not None and cached_commit == commit_sha:
                return revision
            if revision_ref is not None and (
                cached_commit == revision_ref or revision_ref in refs
            ):
                return revision
    raise ModelRegistryError(
        "model-not-found",
        "cached Hugging Face model revision is missing",
        {
            "model_ref": str(entry.get("entry_id") or ""),
            "repo_id": repo_id,
            "commit_sha": commit_sha,
            "revision": revision_ref,
            "reason": "missing-cache-entry",
        },
    )


def _hf_cache_file_path(cached_file: object) -> Path | None:
    for attr in ("file_path", "path", "blob_path"):
        value = getattr(cached_file, attr, None)
        if value:
            return Path(value)
    return None


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
        file_size = file.stat().st_size
        total_bytes += file_size
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(file_size).encode("ascii"))
        digest.update(b"\0")
        blob_hashes[relative] = _stream_file_sha256_uri(file, aggregate=digest)
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


def _entry_id_from_params(entries: list[object]) -> str:
    used = {
        str(item.get("entry_id"))
        for item in entries
        if isinstance(item, dict) and item.get("entry_id")
    }
    for _attempt in range(16):
        candidate = mint_ulid()
        if candidate not in used:
            return candidate
    raise ModelRegistryError(
        "resource-in-use",
        "unable to mint an unused model entry id",
        {"reason": "model-entry-id-collision"},
    )


def _model_aliases(params: dict[str, Any], entry_id: str) -> list[str]:
    aliases: list[str] = []
    requested_entry_id = _optional_str(params.get("entry_id"))
    if requested_entry_id is not None and requested_entry_id != entry_id:
        aliases.append(requested_entry_id)
    return aliases


def _model_aliases_from_entry(entry: dict[str, Any]) -> set[str]:
    value = entry.get("aliases")
    if not isinstance(value, list):
        return set()
    return {str(item) for item in value if isinstance(item, str) and item}


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
    _write_private_text_atomic(
        path,
        json.dumps(registry, sort_keys=True, separators=(",", ":")) + "\n",
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


def _registry_lock(path: Path) -> _ExclusiveFileLock:
    return _ExclusiveFileLock(path.parent / "registry.lock")


def _entry_lock(path: Path, entry_id: str) -> _ExclusiveFileLock:
    return _ExclusiveFileLock(path.parent / "locks" / _entry_lock_filename(entry_id))


def _entry_lock_filename(entry_id: str) -> str:
    safe = "".join(
        char if char.isalnum() or char in "._-" else "_" for char in str(entry_id)
    ).strip("._")
    if not safe:
        safe = sha256(str(entry_id).encode("utf-8")).hexdigest()
    return f"{safe}.lock"


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


def _model_payload(entry: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for field in MODEL_ENTRY_FIELDS:
        value = entry.get(field)
        if field == "files":
            payload[field] = dict(value) if isinstance(value, dict) else {}
        elif field == "integrity":
            if isinstance(value, dict) and value:
                payload[field] = dict(value)
        elif field == "aliases":
            if isinstance(value, list):
                payload[field] = [
                    str(item) for item in value if isinstance(item, str) and item
                ]
            else:
                payload[field] = []
        elif field in {"gated", "token_required"}:
            payload[field] = bool(value)
        elif field == "size_bytes":
            payload[field] = int(value or 0)
        elif field in {"unique_size_bytes", "nominal_size_bytes"}:
            if value is not None:
                payload[field] = int(value or 0)
        elif field in {"allow_patterns", "ignore_patterns"}:
            if isinstance(value, list):
                payload[field] = [str(item) for item in value if isinstance(item, str)]
        else:
            payload[field] = _optional_str(value)
    return payload


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _sha256_uri(data: bytes) -> str:
    return f"sha256:{sha256(data).hexdigest()}"


def _stream_file_sha256_uri(path: Path, *, aggregate) -> str:
    digest = sha256()
    with path.open("rb") as file:
        while chunk := file.read(HASH_CHUNK_BYTES):
            digest.update(chunk)
            aggregate.update(chunk)
    return f"sha256:{digest.hexdigest()}"
