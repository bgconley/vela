from __future__ import annotations

from typing import Any

from vllm_loader.agent.local import TargetCallError

ERROR_CODE_BY_NAME = {
    "parse-error": -32700,
    "invalid-request": -32600,
    "method-not-found": -32601,
    "invalid-params": -32602,
    "internal-error": -32000,
    "agent-internal": -32000,
    "run-not-found": -32001,
    "identity-verification-failed": -32002,
    "not-stoppable": -32003,
    "unknown-config": -32004,
    "config-not-found": -32004,
    "invalid-config": -32004,
    "preflight-failed": -32005,
    "version-mismatch": -32006,
    "build-not-found": -32007,
    "model-not-found": -32008,
    "resource-in-use": -32009,
    "job-already-running": -32010,
    "feature-unavailable": -32011,
    "agent-unreachable": -32012,
    "command-not-found": -32013,
}

ERROR_NAME_BY_CODE = {
    code: name
    for name, code in ERROR_CODE_BY_NAME.items()
    if name
    not in {
        "agent-internal",
        "config-not-found",
        "invalid-config",
    }
}


def rpc_error_payload(code: str, message: str, details: dict[str, Any] | None) -> dict:
    data = dict(details or {})
    wire_code = ERROR_CODE_BY_NAME.get(code, ERROR_CODE_BY_NAME["internal-error"])
    if code not in ERROR_CODE_BY_NAME:
        data.setdefault("target_error_code", code)
    return {
        "code": wire_code,
        "message": message,
        "data": data,
    }


def target_call_error_from_rpc_payload(payload: dict[str, Any]) -> TargetCallError:
    data = _payload_data(payload)
    code = _target_error_code(payload.get("code"), data)
    message = str(payload.get("message") or code)
    return TargetCallError(code, message, data)


def _payload_data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if not isinstance(data, dict):
        data = payload.get("details")
    return dict(data) if isinstance(data, dict) else {}


def _target_error_code(value: object, data: dict[str, Any]) -> str:
    if isinstance(value, int):
        mapped = ERROR_NAME_BY_CODE.get(value)
        if mapped is not None:
            return mapped
        fallback = data.get("target_error_code")
        return str(fallback) if fallback else str(value)
    if isinstance(value, str) and value:
        return value
    return "target-error"
