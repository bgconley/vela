from __future__ import annotations

import codecs
import errno
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

RecordKind = Literal["committed", "transient"]


@dataclass(frozen=True)
class LogRecord:
    kind: RecordKind
    text: str
    level: str | None = None


TOKEN_RE = re.compile(r"\bsk-\S+")
BEARER_RE = re.compile(r"Authorization:\s*Bearer\s+\S+", re.IGNORECASE)


class LogSink:
    def __init__(
        self,
        path: Path,
        *,
        secrets: list[str] | tuple[str, ...],
        emit: Callable[[LogRecord], None] | None = None,
        max_pending: int = 1 << 20,
    ) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        self._file = os.fdopen(fd, "wb")
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._pending = ""
        self._secrets = [secret for secret in secrets if secret]
        self._emit = emit or (lambda _record: None)
        self._max_pending = max_pending

    def feed(self, chunk: bytes) -> None:
        self._pending += self._decoder.decode(chunk)
        self._process_pending()
        self._flush_if_too_large()

    def close(self) -> None:
        final = self._decoder.decode(b"", final=True)
        if final:
            self._pending += final
        if self._pending:
            self._commit(self._pending)
            self._pending = ""
        self._file.close()

    def rotate_to(self, path: Path) -> None:
        self._file.flush()
        self._file.close()
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        self._file = os.fdopen(fd, "wb")

    def _flush_if_too_large(self) -> None:
        while len(self._pending) > self._max_pending:
            truncated = self._pending[: self._max_pending]
            self._commit(f"{truncated} […line truncated at {self._max_pending} bytes…]")
            self._pending = self._pending[self._max_pending :]

    def _process_pending(self) -> None:
        index = 0
        while index < len(self._pending):
            next_cr = self._pending.find("\r", index)
            next_lf = self._pending.find("\n", index)
            positions = [pos for pos in (next_cr, next_lf) if pos != -1]
            if not positions:
                break
            pos = min(positions)
            segment = self._pending[index:pos]
            terminator = self._pending[pos]
            if terminator == "\n":
                self._commit(segment)
                index = pos + 1
                continue
            if pos + 1 >= len(self._pending):
                break
            if self._pending[pos + 1] == "\n":
                self._commit(segment)
                index = pos + 2
                continue
            text = self.scrub(segment)
            self._emit(LogRecord("transient", text, level=level_for_line(text)))
            index = pos + 1
        self._pending = self._pending[index:]

    def _commit(self, segment: str) -> None:
        text = self.scrub(segment)
        self._file.write((text + "\n").encode("utf-8", errors="replace"))
        self._file.flush()
        self._emit(LogRecord("committed", text, level=level_for_line(text)))

    def scrub(self, text: str) -> str:
        scrubbed = text
        for secret in self._secrets:
            scrubbed = scrubbed.replace(secret, "••••")
        scrubbed = BEARER_RE.sub("Authorization: Bearer ••••", scrubbed)
        scrubbed = TOKEN_RE.sub("••••", scrubbed)
        return scrubbed


def level_for_line(text: str) -> str | None:
    match = re.match(r"\s*(CRITICAL|ERROR|WARNING|WARN|INFO|DEBUG)\b", text)
    if not match:
        return None
    level = match.group(1)
    return "WARNING" if level == "WARN" else level


def is_pty_eof(exc: OSError) -> bool:
    return exc.errno == errno.EIO


class OSErrorByteReader:
    def __init__(self, exc: OSError) -> None:
        self.exc = exc

    def read(self, _size: int = 4096) -> bytes:
        if is_pty_eof(self.exc):
            return b""
        raise self.exc
