from __future__ import annotations

import re
from collections.abc import Iterable

MASK = "••••"
BEARER_RE = re.compile(r"(Authorization:\s*Bearer\s+)\S+", re.IGNORECASE)
TOKEN_RE = re.compile(r"\b(?:sk-|hf_)[^\s\"'&;,\]})]+")
SECRET_KEY_MARKERS = (
    "TOKEN",
    "KEY",
    "SECRET",
    "AUTH",
    "PASSWORD",
    "PASS",
    "CREDENTIAL",
)


def is_secret_key(key: str) -> bool:
    """Return whether an environment/config key conventionally carries a secret."""

    upper = key.upper()
    return any(marker in upper for marker in SECRET_KEY_MARKERS)


def scrub_text(text: str, *, secrets: Iterable[str] = ()) -> str:
    scrubbed = text
    for secret in secrets:
        if secret:
            scrubbed = scrubbed.replace(secret, MASK)
    scrubbed = BEARER_RE.sub(r"\1" + MASK, scrubbed)
    scrubbed = TOKEN_RE.sub(MASK, scrubbed)
    return scrubbed
