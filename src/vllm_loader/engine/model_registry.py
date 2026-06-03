from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


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
