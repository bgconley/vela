from __future__ import annotations

import json
from typing import Any

MAX_FRAME_BYTES = 2 * 1024 * 1024
FRAME_STREAM_LIMIT = MAX_FRAME_BYTES + 1


class NdjsonFrameError(ValueError):
    pass


def encode_frame(frame: dict[str, Any], *, max_bytes: int = MAX_FRAME_BYTES) -> bytes:
    data = json.dumps(frame, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(data) > max_bytes:
        raise NdjsonFrameError(f"frame exceeds {max_bytes} bytes")
    return data + b"\n"


def decode_frame(line: bytes, *, max_bytes: int = MAX_FRAME_BYTES) -> dict[str, Any]:
    if len(line) > max_bytes + 1:
        raise NdjsonFrameError(f"frame exceeds {max_bytes} bytes")
    try:
        value = json.loads(line)
    except json.JSONDecodeError as exc:
        raise NdjsonFrameError(str(exc)) from exc
    if not isinstance(value, dict):
        raise NdjsonFrameError("frame must be a JSON object")
    return value
