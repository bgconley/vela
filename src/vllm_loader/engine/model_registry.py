from __future__ import annotations

import json
import os
from dataclasses import dataclass
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


def list_models(registry_path: str | Path | None = None) -> dict[str, Any]:
    path = (
        Path(registry_path).expanduser()
        if registry_path is not None
        else default_models_registry_path()
    )
    skipped: list[dict[str, str]] = []
    if not path.exists():
        return {
            "models": [],
            "default_cache": "hf",
            "app_download_dir": None,
            "skipped": skipped,
        }

    try:
        registry = json.loads(path.read_text(encoding="utf-8"))
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
    if not isinstance(registry, dict):
        return {
            "models": [],
            "default_cache": "hf",
            "app_download_dir": None,
            "skipped": [{"entry_id": "", "reason": "invalid-registry"}],
        }

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
        models.append(_model_payload(entry))

    return {
        "models": models,
        "default_cache": str(registry.get("default_cache") or "hf"),
        "app_download_dir": _optional_str(registry.get("app_download_dir")),
        "skipped": skipped,
    }


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


def _required_str(entry: dict[str, Any], field: str, reference: str) -> str:
    value = entry.get(field)
    if not isinstance(value, str) or not value:
        raise ModelRegistryError(
            "model-not-found",
            f"model {reference} is missing {field}",
            {"model_ref": reference, "reason": f"missing-{field}"},
        )
    return value


def _model_payload(entry: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for field in MODEL_ENTRY_FIELDS:
        value = entry.get(field)
        if field == "files":
            payload[field] = dict(value) if isinstance(value, dict) else {}
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
