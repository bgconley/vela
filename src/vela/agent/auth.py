from __future__ import annotations

import os
import secrets
from pathlib import Path

AGENT_TOKEN_ENV = "VELA_AGENT_TOKEN"
AGENT_TOKEN_FILE_ENV = "VELA_AGENT_TOKEN_FILE"
AGENT_REQUIRE_TOKEN_ENV = "VELA_AGENT_REQUIRE_TOKEN"
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
        token_path = _configured_agent_token_file()
        if not token_path.exists():
            return None
        try:
            token = token_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise AgentTokenError(f"unable to read agent token file {token_path}: {exc}") from exc
    return validate_agent_token(token)


def agent_token_required() -> bool:
    """Whether a capability token is mandatory (``VELA_AGENT_REQUIRE_TOKEN``).

    Shared-host deployments set this so the agent fails closed when no token is
    installed, instead of accepting any same-uid (or unverifiable) caller.
    """
    value = os.environ.get(AGENT_REQUIRE_TOKEN_ENV)
    return value is not None and value.strip().lower() in {"1", "true", "yes", "on"}


def default_agent_token_file() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "vela" / "agent-token"


def install_agent_token(
    token: str | None = None,
    *,
    path: str | Path | None = None,
) -> tuple[Path, str]:
    installed_token = validate_agent_token(token or generate_agent_token())
    token_path = Path(path).expanduser() if path is not None else default_agent_token_file()
    token_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(installed_token)
        handle.write("\n")
    token_path.chmod(0o600)
    return token_path, installed_token


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


def _configured_agent_token_file() -> Path:
    token_file = os.environ.get(AGENT_TOKEN_FILE_ENV)
    if token_file:
        return Path(token_file).expanduser()
    return default_agent_token_file()
