from __future__ import annotations

from dataclasses import dataclass

from textual.message import Message

from vllm_loader.engine.log_sink import LogRecord
from vllm_loader.engine.phases import ErrorKind, Phase
from vllm_loader.monitoring.gpu import GpuPollResult


class LoaderMessage(Message):
    def __post_init__(self) -> None:
        Message.__post_init__(self)


@dataclass
class LogLineCommitted(LoaderMessage):
    text: str
    level: str | None = None
    feed_phase: bool = True


@dataclass
class ProgressUpdated(LoaderMessage):
    text: str


@dataclass
class LogLineTransient(ProgressUpdated):
    pass


@dataclass
class PhaseChanged(LoaderMessage):
    phase: Phase
    error_kind: ErrorKind | None = None
    error_excerpt: str | None = None
    agent_mono: float | None = None


@dataclass
class ServerReady(LoaderMessage):
    models: list[str]
    reachable_url: str | None = None
    feed_phase: bool = True


@dataclass
class HealthChanged(LoaderMessage):
    ready: bool
    detail: str
    models: list[str] | None = None
    error_kind: ErrorKind | None = None
    reachable_url: str | None = None
    feed_phase: bool = True


@dataclass
class ProcessExited(LoaderMessage):
    returncode: int | None


@dataclass
class EngineError(LoaderMessage):
    kind: ErrorKind
    detail: str


@dataclass
class AgentError(LoaderMessage):
    detail: str
    fatal: bool = False


@dataclass
class GpuStatsUpdated(LoaderMessage):
    result: GpuPollResult


@dataclass
class GpuStatsUnavailable(LoaderMessage):
    detail: str


def from_log_record(record: LogRecord) -> LogLineCommitted | ProgressUpdated:
    if record.kind == "committed":
        return LogLineCommitted(record.text, record.level)
    return ProgressUpdated(record.text)
