from __future__ import annotations

from collections import deque
from enum import Enum

from vela.engine.profile import VllmProfile


class Phase(str, Enum):
    IDLE = "IDLE"
    STARTING = "STARTING"
    RESOLVING_MODEL = "RESOLVING_MODEL"
    DOWNLOADING_MODEL = "DOWNLOADING_MODEL"
    LOADING_WEIGHTS = "LOADING_WEIGHTS"
    PROFILING_KV = "PROFILING_KV"
    CAPTURING_GRAPHS = "CAPTURING_GRAPHS"
    SERVER_STARTING = "SERVER_STARTING"
    READY = "READY"
    DEGRADED = "DEGRADED"
    STOPPED = "STOPPED"
    ERROR = "ERROR"


class ErrorKind(str, Enum):
    OOM = "OOM"
    PORT_IN_USE = "PORT_IN_USE"
    IMAGE_NOT_FOUND = "IMAGE_NOT_FOUND"
    DAEMON_UNREACHABLE = "DAEMON_UNREACHABLE"
    NAME_CONFLICT = "NAME_CONFLICT"
    GPU_NOT_AVAILABLE = "GPU_NOT_AVAILABLE"
    MODEL_NOT_FOUND = "MODEL_NOT_FOUND"
    TP_MISMATCH = "TP_MISMATCH"
    HF_AUTH = "HF_AUTH"
    API_KEY_AUTH = "API_KEY_AUTH"
    DISK_FULL = "DISK_FULL"
    COMMAND_NOT_FOUND = "COMMAND_NOT_FOUND"
    CONFIG_INVALID = "CONFIG_INVALID"
    CRASHED = "CRASHED"
    TIMED_OUT = "TIMED_OUT"


PHASE_ORDER = {
    Phase.IDLE: 0,
    Phase.STARTING: 1,
    Phase.RESOLVING_MODEL: 2,
    Phase.DOWNLOADING_MODEL: 3,
    Phase.LOADING_WEIGHTS: 4,
    Phase.PROFILING_KV: 5,
    Phase.CAPTURING_GRAPHS: 6,
    Phase.SERVER_STARTING: 7,
}


class PhaseFSM:
    def __init__(self, profile: VllmProfile) -> None:
        self.profile = profile
        self.phase = Phase.IDLE
        self.error_kind: ErrorKind | None = None
        self.error_excerpt: str | None = None
        self.served_models: list[str] = []
        self.history: list[Phase] = []
        self.recent_lines: deque[str] = deque(maxlen=100)

    def feed_line(self, line: str) -> None:
        self.recent_lines.append(line)
        for pattern, kind in self.profile.error_rules:
            if pattern.search(line):
                self.error_kind = ErrorKind(kind)
                self.error_excerpt = line
                self._transition(Phase.ERROR)
                return
        for pattern, phase_name in self.profile.phase_rules:
            if pattern.search(line):
                self._advance_loading_phase(Phase(phase_name))
                return

    def health_ready(self, models: list[str]) -> None:
        self.served_models = models
        self.error_kind = None
        self.error_excerpt = None
        self._transition(Phase.READY)

    def health_failed(self, detail: str) -> None:
        if self.phase is Phase.READY:
            self.error_excerpt = detail
            self._transition(Phase.DEGRADED)

    def health_error(self, kind: ErrorKind, detail: str) -> None:
        self.error_kind = kind
        self.error_excerpt = detail
        self._transition(Phase.ERROR)

    def process_exited(self, returncode: int | None, *, intentional: bool = False) -> None:
        if self.phase is Phase.ERROR:
            return
        if intentional or (returncode == 0 and self.phase in {Phase.READY, Phase.DEGRADED}):
            self._transition(Phase.STOPPED)
            return
        if self.error_kind is None:
            self.error_kind = ErrorKind.CRASHED
        if self.error_excerpt is None and self.recent_lines:
            self.error_excerpt = self.recent_lines[-1]
        if self.error_excerpt is None:
            self.error_excerpt = (
                f"process exited with code {returncode}"
                if returncode is not None
                else "process exited unexpectedly"
            )
        self._transition(Phase.ERROR)

    def timeout(self) -> None:
        self.error_kind = ErrorKind.TIMED_OUT
        self._transition(Phase.ERROR)

    def _advance_loading_phase(self, next_phase: Phase) -> None:
        current_order = PHASE_ORDER.get(self.phase, -1)
        next_order = PHASE_ORDER.get(next_phase, -1)
        if next_order >= current_order and self.phase not in {
            Phase.READY,
            Phase.DEGRADED,
            Phase.ERROR,
        }:
            self._transition(next_phase)

    def _transition(self, phase: Phase) -> None:
        if self.phase is phase:
            return
        self.phase = phase
        self.history.append(phase)
