from __future__ import annotations

import pytest

from vllm_loader.transport.ndjson import NdjsonFrameError, decode_frame, encode_frame
from vllm_loader.transport.subprocess import SubprocessTargetClient


def test_ndjson_frame_round_trips_json_object() -> None:
    frame = {"id": "r1", "method": "handshake", "params": {"protocol_version": 1}}

    encoded = encode_frame(frame)

    assert encoded.endswith(b"\n")
    assert decode_frame(encoded) == frame


def test_ndjson_frame_rejects_oversized_payload() -> None:
    with pytest.raises(NdjsonFrameError, match="exceeds"):
        encode_frame({"payload": "x" * 8}, max_bytes=4)


def test_ndjson_frame_rejects_non_object_payload() -> None:
    with pytest.raises(NdjsonFrameError, match="object"):
        decode_frame(b'["not", "an", "object"]\n')


@pytest.mark.asyncio
async def test_subprocess_target_client_requires_connection() -> None:
    client = SubprocessTargetClient(["python", "-c", "pass"])

    with pytest.raises(RuntimeError, match="not connected"):
        await client.call("handshake")
