from __future__ import annotations

from dataclasses import dataclass

from textual.message import Message

from vela.engine.log_sink import LogRecord
from vela.engine.phases import ErrorKind, Phase
from vela.monitoring.gpu import GpuPollResult


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
    # Spec §6.3: killed-by-signal. Derived from the POSIX negative returncode
    # when not given explicitly.
    signaled: bool | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.signaled is None:
            self.signaled = self.returncode is not None and self.returncode < 0


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
