from __future__ import annotations

from enum import Enum


class BuildPhase(str, Enum):
    RESOLVING = "RESOLVING"
    DOWNLOADING = "DOWNLOADING"
    BUILDING = "BUILDING"
    INSTALLING = "INSTALLING"
    VERIFYING = "VERIFYING"
    READY = "READY"
    FAILED = "FAILED"


class DownloadPhase(str, Enum):
    RESOLVING = "RESOLVING"
    DOWNLOADING = "DOWNLOADING"
    VERIFYING = "VERIFYING"
    READY = "READY"
    FAILED = "FAILED"
