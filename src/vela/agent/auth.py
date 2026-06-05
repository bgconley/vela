from __future__ import annotations

import os
import secrets

AGENT_TOKEN_ENV = "VELA_AGENT_TOKEN"
MIN_AGENT_TOKEN_BYTES = 16
DEFAULT_AGENT_TOKEN_BYTES = 32
MIN_AGENT_TOKEN_CHARS = 22
MIN_AGENT_TOKEN_UNIQUE_CHARS = 4
MAX_AGENT_TOKEN_CHAR_FREQUENCY = 0.75


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
    if (
        len(token) < MIN_AGENT_TOKEN_CHARS
        or any(char.isspace() for char in token)
        or _looks_low_entropy(token)
    ):
        raise AgentTokenError(
            "VELA_AGENT_TOKEN must be a single high-entropy token with at least 128 bits "
            "of entropy; generate one with `vela agent gen-token`"
        )
    return token


def _looks_low_entropy(token: str) -> bool:
    counts = {char: token.count(char) for char in set(token)}
    if len(counts) < MIN_AGENT_TOKEN_UNIQUE_CHARS:
        return True
    most_common = max(counts.values(), default=0)
    return most_common / len(token) > MAX_AGENT_TOKEN_CHAR_FREQUENCY
