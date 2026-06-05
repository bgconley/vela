from __future__ import annotations

import secrets
import time

CROCKFORD_BASE32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def mint_ulid() -> str:
    timestamp_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    random_bits = int.from_bytes(secrets.token_bytes(10), "big")
    value = (timestamp_ms << 80) | random_bits
    chars = []
    for shift in range(125, -1, -5):
        chars.append(CROCKFORD_BASE32[(value >> shift) & 0b11111])
    return "".join(chars)
