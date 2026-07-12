from __future__ import annotations

import json
import os
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator


class TransportKind(str, Enum):
    LOCAL = "local"
    SSH = "ssh"


class LocalTransportKind(str, Enum):
    IN_PROCESS = "in_process"
    SOCKET = "socket"


class TargetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    transport: TransportKind = TransportKind.LOCAL
    host: str | None = None
    ssh_key: Path | None = None
    workdir: Path | None = None
    venv: Path | None = None
    agent_command: list[str] | None = None
    ssh_opts_env: str | None = None
    local_transport: LocalTransportKind = LocalTransportKind.SOCKET
    socket_path: Path | None = None

    @field_validator("name")
    @classmethod
    def name_must_not_be_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be empty")
        return value

    @field_validator("agent_command", mode="before")
    @classmethod
    def agent_command_from_shell_string(cls, value: object) -> list[str] | None:
        if value is None:
            return None
        if isinstance(value, str):
            command = shlex.split(value)
        elif isinstance(value, list | tuple):
            command = [str(part) for part in value]
        else:
            raise ValueError("agent_command must be a shell string or list of strings")
        if not command or any(not part for part in command):
            raise ValueError("agent_command must not be empty")
        return command

    @model_validator(mode="after")
    def validate_transport_fields(self) -> TargetConfig:
        if self.transport is TransportKind.SSH and not self.host:
            raise ValueError("ssh target requires host")
        return self


@dataclass(frozen=True)
class TargetsRegistry:
    _targets: tuple[TargetConfig, ...]

    @property
    def targets(self) -> list[TargetConfig]:
        return list(self._targets)

    def by_name(self, name: str) -> TargetConfig:
        for target in self._targets:
            if target.name == name:
                return target
        raise KeyError(f"unknown target {name!r}")


def default_targets_path() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "vela" / "targets.yaml"


def load_targets_file(path: str | Path | None = None) -> TargetsRegistry:
    target_path = _resolve_targets_path(path)
    if not target_path.exists():
        return TargetsRegistry((_local_target(),))

    try:
        data = _load_targets_payload(target_path)
    except Exception as exc:
        raise ValueError(f"failed to load targets file {target_path}: {exc}") from exc
    if data is None:
        return TargetsRegistry((_local_target(),))
    if not isinstance(data, dict):
        raise ValueError("targets file root must be a mapping")

    targets_data = data.get("targets", {})
    if not isinstance(targets_data, dict):
        raise ValueError("targets must be a mapping")

    targets = [_local_target()]
    seen = {"local"}
    for name, raw_target in targets_data.items():
        target_name = str(name)
        if target_name == "local":
            continue
        if target_name in seen:
            raise ValueError(f"duplicate target {target_name!r}")
        if not isinstance(raw_target, dict):
            raise ValueError(f"target {target_name}: entry must be a mapping")
        targets.append(_parse_target(target_name, raw_target))
        seen.add(target_name)

    return TargetsRegistry(tuple(targets))


def save_targets_file(
    registry: TargetsRegistry, path: str | Path | None = None
) -> Path:
    target_path = _resolve_targets_path(path)
    payload: dict[str, Any] = {
        "targets": {
            target.name: target.model_dump(
                mode="json",
                exclude={"name"},
                exclude_none=True,
            )
            for target in registry.targets
            if target.name != "local"
        }
    }
    # Preserve a previously-saved default across a targets rewrite (e.g. upsert/remove).
    existing_default = load_default_target(target_path)
    if existing_default is not None:
        payload["default_target"] = existing_default
    target_path.parent.mkdir(parents=True, exist_ok=True)
    _write_targets_payload(target_path, payload)
    return target_path


def load_default_target(path: str | Path | None = None) -> str | None:
    """The persisted default target name (``default_target`` in targets.yaml), or None."""
    target_path = _resolve_targets_path(path)
    if not target_path.exists():
        return None
    try:
        data = _load_targets_payload(target_path)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("default_target")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def save_default_target(name: str | None, path: str | Path | None = None) -> Path:
    """Persist (``name``) or clear (``None``) the default target, keeping targets intact."""
    target_path = _resolve_targets_path(path)
    payload = _existing_raw_payload(target_path)
    payload.setdefault("targets", {})
    if name is None:
        payload.pop("default_target", None)
    else:
        payload["default_target"] = name
    target_path.parent.mkdir(parents=True, exist_ok=True)
    _write_targets_payload(target_path, payload)
    return target_path


def _existing_raw_payload(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = _load_targets_payload(path)
    except Exception:
        return {}
    return dict(data) if isinstance(data, dict) else {}


def upsert_target_file(target: TargetConfig, path: str | Path | None = None) -> Path:
    if target.name == "local":
        raise ValueError("local target is implicit and cannot be added")
    registry = load_targets_file(path)
    targets = [
        item
        for item in registry.targets
        if item.name not in {"local", target.name}
    ]
    targets.append(target)
    return save_targets_file(TargetsRegistry(tuple((_local_target(), *targets))), path)


def remove_target_file(name: str, path: str | Path | None = None) -> Path:
    if name == "local":
        raise ValueError("local target is implicit and cannot be removed")
    registry = load_targets_file(path)
    if all(target.name != name for target in registry.targets):
        raise ValueError(f"unknown target {name!r}")
    targets = [
        target
        for target in registry.targets
        if target.name not in {"local", name}
    ]
    return save_targets_file(TargetsRegistry(tuple((_local_target(), *targets))), path)


def _local_target() -> TargetConfig:
    return TargetConfig(name="local", transport=TransportKind.LOCAL)


def _resolve_targets_path(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path).expanduser()
    yaml_path = default_targets_path()
    json_path = yaml_path.with_suffix(".json")
    if not yaml_path.exists() and json_path.exists():
        return json_path
    return yaml_path


def _load_targets_payload(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(text)
    return yaml.safe_load(text)


def _write_targets_payload(path: Path, payload: dict[str, Any]) -> None:
    if path.suffix.lower() == ".json":
        path.write_text(
            f"{json.dumps(payload, indent=2, sort_keys=True)}\n",
            encoding="utf-8",
        )
        return
    path.write_text(
        yaml.safe_dump(payload, sort_keys=True),
        encoding="utf-8",
    )


def _parse_target(name: str, data: dict[str, Any]) -> TargetConfig:
    if "name" in data and str(data["name"]) != name:
        raise ValueError(f"target {name}: name field must match mapping key")
    payload = {**data, "name": name}
    try:
        return TargetConfig.model_validate(payload)
    except ValidationError as exc:
        details = "; ".join(
            _format_validation_error(error) for error in exc.errors(include_url=False)
        )
        raise ValueError(f"target {name}: {details}") from exc
    except ValueError as exc:
        raise ValueError(f"target {name}: {exc}") from exc


def _format_validation_error(error: Mapping[str, Any]) -> str:
    loc = ".".join(str(part) for part in error["loc"]) or "<root>"
    return f"{loc}: {error['msg']}"
