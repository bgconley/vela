from __future__ import annotations

from dataclasses import dataclass

from vllm_loader.engine.log_sink import LogRecord
from vllm_loader.engine.phases import ErrorKind, Phase
from vllm_loader.monitoring.gpu import GpuPollResult


@dataclass(frozen=True)
class LogLineCommitted:
    text: str
    level: str | None = None


@dataclass(frozen=True)
class LogLineTransient:
    text: str


@dataclass(frozen=True)
class PhaseChanged:
    phase: Phase


@dataclass(frozen=True)
class ServerReady:
    models: list[str]


@dataclass(frozen=True)
class ProcessExited:
    returncode: int | None


@dataclass(frozen=True)
class EngineError:
    kind: ErrorKind
    detail: str


@dataclass(frozen=True)
class GpuStatsUpdated:
    result: GpuPollResult


def from_log_record(record: LogRecord) -> LogLineCommitted | LogLineTransient:
    if record.kind == "committed":
        return LogLineCommitted(record.text, record.level)
    return LogLineTransient(record.text)
