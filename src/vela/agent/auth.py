from __future__ import annotations

import os
import secrets

AGENT_TOKEN_ENV = "VELA_AGENT_TOKEN"
MIN_AGENT_TOKEN_BYTES = 16
DEFAULT_AGENT_TOKEN_BYTES = 32
MIN_AGENT_TOKEN_CHARS = 22


class AgentTokenError(ValueError):
    pass


def configured_agent_token() -> str | None:
    token = os.environ.get(AGENT_TOKEN_ENV)
    if not token:
        return None
    return validate_agent_token(token)


def generate_agent_token(nbytes: int = DEFAULT_AGENT_TOKEN_BYTES) -> str:
    if nbytes < MIN_AGENT_TOKEN_BYTES:
        raise AgentTokenError(
            f"agent capability tokens must use at least 128 bits of entropy; "
            f"request at least {MIN_AGENT_TOKEN_BYTES} random bytes"
        )
    return secrets.token_urlsafe(nbytes)


def validate_agent_token(token: str) -> str:
    if len(token) < MIN_AGENT_TOKEN_CHARS or any(char.isspace() for char in token):
        raise AgentTokenError(
            "VELA_AGENT_TOKEN must be a single token with at least 128 bits "
            "of entropy; generate one with `vela agent gen-token`"
        )
    return token
