from __future__ import annotations

import os
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EntryPoint(str, Enum):
    SERVE = "serve"
    MODULE = "module"


class LaunchMode(str, Enum):
    ATTACHED = "attached"
    DETACHED = "detached"


class Exposure(str, Enum):
    LOCAL = "local"
    LAN = "lan"
    PUBLIC = "public"


DType = Literal["auto", "half", "float16", "bfloat16", "float", "float32"]


class CommandConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entrypoint: EntryPoint = EntryPoint.SERVE
    executable: str | None = None
    build: str | None = None
    cwd: Path | None = None

    @model_validator(mode="after")
    def executable_and_build_are_mutually_exclusive(self) -> CommandConfig:
        if self.executable and self.build:
            raise ValueError("command.build cannot be set with command.executable")
        return self


class EngineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tensor_parallel_size: int | None = None
    pipeline_parallel_size: int | None = None
    gpu_memory_utilization: float | None = None
    max_model_len: int | None = None
    dtype: DType | None = None
    quantization: str | None = None
    kv_cache_dtype: str | None = None
    load_format: str | None = None
    enforce_eager: bool | None = None
    swap_space: int | None = None
    block_size: int | None = None
    seed: int | None = None
    max_num_seqs: int | None = None


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = "127.0.0.1"
    port: int = 8000
    exposure: Exposure = Exposure.LOCAL
    api_key: str | None = None
    probe_host: str | None = None

    @model_validator(mode="after")
    def require_explicit_exposure_for_non_loopback(self) -> ServerConfig:
        if self.host not in {"127.0.0.1", "localhost", "::1"} and self.host not in {
            "0.0.0.0",
            "::",
        }:
            if self.exposure is Exposure.LOCAL:
                raise ValueError("non-loopback host requires exposure: lan or public")
        if self.host in {"0.0.0.0", "::"} and self.exposure is Exposure.LOCAL:
            raise ValueError("wildcard host requires exposure: lan or public")
        return self


class LoggingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_logging: bool = False
    suppress_access_log_for: list[str] = Field(default_factory=list)
    max_log_len: int | None = None


class VllmConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version_profile: str | None = None
    require_flags: list[str] = Field(default_factory=list)


class HealthConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = "/health"
    interval_seconds: float = 2.0


class LaunchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: LaunchMode = LaunchMode.ATTACHED
    ready_timeout_seconds: int = 900
    health: HealthConfig = Field(default_factory=HealthConfig)
    runs_dir: Path | None = None


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    target: str | None = None
    description: str | None = None
    model: str
    revision: str | None = None
    model_ref: str | None = None
    served_model_name: str | None = None
    command: CommandConfig = Field(default_factory=CommandConfig)
    engine: EngineConfig = Field(default_factory=EngineConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    env: dict[str, str] = Field(default_factory=dict)
    extra_args: list[str] = Field(default_factory=list)
    vllm: VllmConfig = Field(default_factory=VllmConfig)
    launch: LaunchConfig = Field(default_factory=LaunchConfig)

    @field_validator("name")
    @classmethod
    def name_must_not_be_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be empty")
        return value

    @field_validator("env")
    @classmethod
    def env_values_are_strings(cls, value: dict[str, str]) -> dict[str, str]:
        return {str(key): str(item) for key, item in value.items()}

    @model_validator(mode="after")
    def fill_served_model_name(self) -> ModelConfig:
        if not self.served_model_name:
            self.served_model_name = model_basename(self.model)
        return self

    @model_validator(mode="after")
    def model_ref_must_not_shadow_local_path(self) -> ModelConfig:
        if self.model_ref and _looks_like_explicit_local_model_path(self.model):
            raise ValueError("model_ref cannot be set with an explicit local model path")
        return self

    @property
    def run_artifacts_dir(self) -> Path:
        if self.launch.runs_dir is not None:
            return self.launch.runs_dir
        return default_run_artifacts_dir()


def default_run_artifacts_dir() -> Path:
    return Path(os.path.expanduser("~/.local/state/vela/runs"))


def model_basename(model: str) -> str:
    expanded = os.path.expanduser(model)
    if model.startswith(("/", "./", "../", "~")):
        return Path(expanded).name
    if "/" in model:
        return model.rstrip("/").split("/")[-1]
    return Path(model).name


def _looks_like_explicit_local_model_path(model: str) -> bool:
    return model.startswith(("/", "./", "../", "~"))
