from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vllm_loader import __version__
from vllm_loader.config.loader import ConfigRegistry, InvalidConfig, ValidConfig, load_registry
from vllm_loader.engine.command_builder import build_command
from vllm_loader.engine.profile import VllmProfileError, select_profile_for_config

PROTOCOL_VERSION = 1


@dataclass(frozen=True)
class TargetCallError(RuntimeError):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message


class LocalAgent:
    def __init__(self, *, target_name: str = "local") -> None:
        self.target_name = target_name

    def handle(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = params or {}
        if method == "handshake":
            return self._handshake()
        if method == "list_configs":
            return self._list_configs(payload)
        if method == "preview":
            return self._preview(payload)
        raise TargetCallError("method-not-found", f"unknown agent method: {method}")

    def _handshake(self) -> dict[str, Any]:
        return {
            "agent_version": __version__,
            "protocol_version": PROTOCOL_VERSION,
            "target": self.target_name,
            "capabilities": [
                "handshake",
                "list_configs",
                "preview",
            ],
        }

    def _list_configs(self, params: dict[str, Any]) -> dict[str, Any]:
        registry = load_registry(_configs_dir(params))
        return {
            "valid": [_valid_config_payload(item) for item in registry.valid],
            "invalid": [_invalid_config_payload(item) for item in registry.invalid],
        }

    def _preview(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or not name.strip():
            raise TargetCallError("invalid-params", "preview requires config name")
        registry = load_registry(_configs_dir(params))
        cfg = _config_by_name(registry, name)
        try:
            result = build_command(cfg, select_profile_for_config(cfg))
        except VllmProfileError as exc:
            raise TargetCallError("profile-error", str(exc)) from exc
        return {
            "preview": result.preview,
            "warnings": list(result.warnings),
            "metadata": dict(result.metadata),
        }


def _configs_dir(params: dict[str, Any]) -> Path | None:
    value = params.get("configs_dir")
    if value is None:
        return None
    return Path(str(value))


def _config_by_name(registry: ConfigRegistry, name: str):
    try:
        return registry.by_name(name)
    except KeyError as exc:
        invalid_matches = [
            item
            for item in registry.invalid
            if item.raw_name == name or (item.raw_name is None and item.path.stem == name)
        ]
        if invalid_matches:
            matches = [_invalid_config_payload(item) for item in invalid_matches]
            errors = "; ".join(
                f"{item.path.name}: {', '.join(item.errors)}" for item in invalid_matches
            )
            raise TargetCallError(
                "invalid-config",
                errors,
                {"name": name, "matches": matches},
            ) from exc
        available = [item.config.name for item in registry.valid]
        raise TargetCallError(
            "unknown-config",
            f"unknown config: {name}",
            {"name": name, "available": available},
        ) from exc


def _valid_config_payload(item: ValidConfig) -> dict[str, Any]:
    return {
        "path": str(item.path),
        "name": item.config.name,
        "model": item.config.model,
        "target": item.config.target,
        "warnings": list(item.warnings),
        "config": item.config.model_dump(mode="json"),
    }


def _invalid_config_payload(item: InvalidConfig) -> dict[str, Any]:
    return {
        "path": str(item.path),
        "errors": list(item.errors),
        "raw_name": item.raw_name,
    }
