from __future__ import annotations

import os

AGENT_TOKEN_ENV = "VLLM_LOADER_AGENT_TOKEN"


def configured_agent_token() -> str | None:
    token = os.environ.get(AGENT_TOKEN_ENV)
    return token if token else None
