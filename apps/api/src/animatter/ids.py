"""Stable, sortable identifiers.

Format: ``<prefix>_<6-byte-millisecond-base32><10 random base32 chars>``.
The time prefix keeps ids roughly sortable in logs; the random suffix keeps
them unguessable so a leaked id cannot be walked.
"""

from __future__ import annotations

import math
import secrets
import time

_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"


def _encode(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(_ALPHABET[value % 32])
        value //= 32
    return "".join(reversed(out))


def new_id(prefix: str) -> str:
    """Return a prefixed identifier such as ``prj_01j9x2...``."""
    ms = int(time.time() * 1000)
    return f"{prefix}_{_encode(ms, 9)}{''.join(secrets.choice(_ALPHABET) for _ in range(10))}"


def utcnow_iso() -> str:
    """UTC timestamp, second precision, always ``Z``-suffixed."""
    seconds = math.floor(time.time())
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))
