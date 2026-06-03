from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator


class TransportKind(str, Enum):
    LOCAL = "local"
    SSH = "ssh"


class TargetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    transport: TransportKind = TransportKind.LOCAL
    host: str | None = None
    workdir: Path | None = None
    venv: Path | None = None
    ssh_opts_env: str | None = None

    @field_validator("name")
    @classmethod
    def name_must_not_be_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be empty")
        return value

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
    return config_home / "vllm-loader" / "targets.yaml"


def load_targets_file(path: str | Path | None = None) -> TargetsRegistry:
    target_path = Path(path).expanduser() if path is not None else default_targets_path()
    if not target_path.exists():
        return TargetsRegistry((_local_target(),))

    try:
        data = yaml.safe_load(target_path.read_text(encoding="utf-8"))
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
    target_path = Path(path).expanduser() if path is not None else default_targets_path()
    payload = {
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
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(
        yaml.safe_dump(payload, sort_keys=True),
        encoding="utf-8",
    )
    return target_path


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


def _local_target() -> TargetConfig:
    return TargetConfig(name="local", transport=TransportKind.LOCAL)


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


def _format_validation_error(error: dict[str, Any]) -> str:
    loc = ".".join(str(part) for part in error["loc"]) or "<root>"
    return f"{loc}: {error['msg']}"
