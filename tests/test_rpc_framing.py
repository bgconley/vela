from __future__ import annotations

import pytest

from vllm_loader.agent.local import TargetCallError
from vllm_loader.agent.stdio import _handle_frame
from vllm_loader.transport.ndjson import NdjsonFrameError, decode_frame, encode_frame
from vllm_loader.transport.socket import (
    _target_call_error_from_payload as socket_error_from_payload,
)
from vllm_loader.transport.subprocess import SubprocessTargetClient
from vllm_loader.transport.subprocess import (
    _target_call_error_from_payload as subprocess_error_from_payload,
)
from vllm_loader.transport.rpc_errors import rpc_error_payload


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


@pytest.mark.asyncio
async def test_subprocess_target_client_reports_command_startup_failure() -> None:
    client = SubprocessTargetClient(["definitely-missing-vllm-loader-agent"])

    with pytest.raises(TargetCallError) as exc_info:
        await client.connect()

    assert exc_info.value.code == "agent-unreachable"
    assert "definitely-missing-vllm-loader-agent" in exc_info.value.message


@pytest.mark.asyncio
async def test_subprocess_target_client_reports_bridge_exit_before_response() -> None:
    client = SubprocessTargetClient(["python", "-c", ""])

    with pytest.raises(TargetCallError) as exc_info:
        await client.connect()

    assert exc_info.value.code == "agent-unreachable"
    assert "target agent process exited" in exc_info.value.message


@pytest.mark.asyncio
async def test_stdio_agent_errors_use_json_rpc_integer_codes_and_data_key() -> None:
    class FailingAgent:
        def handle(self, method: str, _params=None):
            if method == "launch":
                raise TargetCallError(
                    "preflight-failed",
                    "preflight failed",
                    {"kind": "MODEL_NOT_FOUND"},
                )
            raise TargetCallError("method-not-found", f"unknown method: {method}")

    frames: list[dict] = []

    async def write_frame(frame: dict) -> None:
        frames.append(frame)

    await _handle_frame(
        FailingAgent(),
        {"id": "r1", "method": "launch", "params": {}},
        write_frame,
        {},
    )

    error = frames[-1]["error"]
    assert error["code"] == -32005
    assert error["message"] == "preflight failed"
    assert error["data"] == {"kind": "MODEL_NOT_FOUND"}
    assert "details" not in error

    await _handle_frame(
        FailingAgent(),
        {"id": "r2", "method": "missing", "params": {}},
        write_frame,
        {},
    )

    error = frames[-1]["error"]
    assert error["code"] == -32601
    assert error["data"] == {}

    await _handle_frame(FailingAgent(), {"id": "r3", "params": {}}, write_frame, {})

    error = frames[-1]["error"]
    assert error["code"] == -32600
    assert error["data"] == {}


def test_transport_clients_decode_json_rpc_error_data_to_target_call_error() -> None:
    payload = {
        "code": -32005,
        "message": "preflight failed",
        "data": {"kind": "MODEL_NOT_FOUND"},
    }

    for error_from_payload in (
        subprocess_error_from_payload,
        socket_error_from_payload,
    ):
        error = error_from_payload(payload)
        assert error.code == "preflight-failed"
        assert error.message == "preflight failed"
        assert error.details == {"kind": "MODEL_NOT_FOUND"}


def test_ambiguous_json_rpc_error_codes_preserve_symbolic_target_error_code() -> None:
    payload = rpc_error_payload(
        "invalid-config",
        "bad.yaml: model: Field required",
        {"name": "bad"},
    )

    assert payload["code"] == -32004
    assert payload["data"]["target_error_code"] == "invalid-config"
    for error_from_payload in (
        subprocess_error_from_payload,
        socket_error_from_payload,
    ):
        error = error_from_payload(payload)
        assert error.code == "invalid-config"
        assert error.details["name"] == "bad"
