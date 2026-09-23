from __future__ import annotations

import re
import secrets

ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
BASE = len(ALPHABET)
_VALID = re.compile(r"^[0-9A-Za-z]{4,16}$")

# Paths the service owns. A user-supplied alias may never shadow one of them.
RESERVED = frozenset(
    {"api", "health", "healthz", "readyz", "docs", "redoc", "openapi", "metrics", "static"}
)


def generate(length: int = 7) -> str:
    """Random code from a CSPRNG.

    Random rather than a counter: sequential ids let anyone enumerate every link
    in the system. The cost is collisions, which the repository retries against
    the primary-key constraint. At 62^7 the birthday bound is far beyond the
    volume this service is sized for.
    """
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def encode(number: int) -> str:
    if number < 0:
        raise ValueError("number must be non-negative")
    if number == 0:
        return ALPHABET[0]
    out: list[str] = []
    while number:
        number, rem = divmod(number, BASE)
        out.append(ALPHABET[rem])
    return "".join(reversed(out))


def decode(code: str) -> int:
    total = 0
    for char in code:
        total = total * BASE + ALPHABET.index(char)
    return total


def is_valid(code: str) -> bool:
    return bool(_VALID.match(code)) and code.lower() not in RESERVED
