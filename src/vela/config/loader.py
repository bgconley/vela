from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from vela.config.schema import ModelConfig


@dataclass(frozen=True)
class ValidConfig:
    path: Path
    config: ModelConfig
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class InvalidConfig:
    path: Path
    errors: list[str]
    raw_name: str | None = None


@dataclass
class ConfigRegistry:
    valid: list[ValidConfig] = field(default_factory=list)
    invalid: list[InvalidConfig] = field(default_factory=list)

    def by_name(self, name: str) -> ModelConfig:
        for item in self.valid:
            if item.config.name == name:
                return item.config
        raise KeyError(f"unknown config {name!r}")


def discover_config_dirs(
    configs_dir: str | Path | None = None,
    *,
    cwd: str | Path | None = None,
    home: str | Path | None = None,
) -> list[Path]:
    cwd_path = Path(cwd or Path.cwd())
    home_path = Path(home or Path.home())
    if configs_dir is not None:
        return [Path(configs_dir).expanduser()]
    env_dir = os.environ.get("VELA_CONFIGS")
    if env_dir:
        return [Path(env_dir).expanduser()]
    # $XDG_CONFIG_HOME wins over ~/.config (docs/configuration.md already promises
    # this); the /vela/configs suffix is unchanged.
    config_home = os.environ.get("XDG_CONFIG_HOME")
    config_base = Path(config_home).expanduser() if config_home else home_path / ".config"
    return [cwd_path / "configs", config_base / "vela" / "configs"]


def load_registry(configs_dir: str | Path | None = None) -> ConfigRegistry:
    registry = ConfigRegistry()
    root = Path(configs_dir).expanduser() if configs_dir is not None else first_existing_dir()
    if not root.exists():
        return registry

    seen: dict[str, ValidConfig] = {}
    duplicate_names: set[str] = set()
    duplicate_originals_reported: set[str] = set()
    valid_candidates: list[ValidConfig] = []

    for path in sorted(root.glob("*.y*ml")):
        loaded = load_config_file(path)
        if isinstance(loaded, ValidConfig):
            name = loaded.config.name
            if name in seen:
                duplicate_names.add(name)
                if name not in duplicate_originals_reported:
                    registry.invalid.append(
                        InvalidConfig(
                            seen[name].path, [f"duplicate config name: {name}"], raw_name=name
                        )
                    )
                    duplicate_originals_reported.add(name)
                registry.invalid.append(
                    InvalidConfig(loaded.path, [f"duplicate config name: {name}"], raw_name=name)
                )
            else:
                seen[name] = loaded
                valid_candidates.append(loaded)
        else:
            registry.invalid.append(loaded)

    registry.valid = [item for item in valid_candidates if item.config.name not in duplicate_names]
    return registry


def first_existing_dir() -> Path:
    for candidate in discover_config_dirs():
        if candidate.exists():
            return candidate
    return discover_config_dirs()[0]


def load_config_file(path: Path) -> ValidConfig | InvalidConfig:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return InvalidConfig(path, [str(exc)])
    if not isinstance(data, dict):
        return InvalidConfig(path, ["config root must be a mapping"])

    try:
        return ValidConfig(path, ModelConfig.model_validate(data))
    except ValidationError as exc:
        return InvalidConfig(path, _format_validation_errors(exc), raw_name=_raw_name(data))
    except ValueError as exc:
        return InvalidConfig(path, [str(exc)], raw_name=_raw_name(data))


def _raw_name(data: dict[str, Any]) -> str | None:
    value = data.get("name")
    return str(value) if value is not None else None


def _format_validation_errors(exc: ValidationError) -> list[str]:
    errors: list[str] = []
    for error in exc.errors(include_url=False):
        loc = ".".join(str(part) for part in error["loc"]) or "<root>"
        errors.append(f"{loc}: {error['msg']}")
    return errors
