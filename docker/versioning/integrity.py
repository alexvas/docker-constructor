"""Shared npm tarball SRI integrity validation.

Both the inventory loader and the npm update provider need to accept exactly
the same ``integrity`` strings, so the format check lives in one module with
no other dependencies.
"""

from __future__ import annotations

import base64
import re

_INTEGRITY_RE = re.compile(r"^sha(256|384|512)-([A-Za-z0-9+/]+=*)$")

# Expected decoded-byte lengths for each algorithm.
_INTEGRITY_LENGTHS: dict[str, int] = {"256": 32, "384": 48, "512": 64}


class IntegrityError(ValueError):
    """An SRI integrity string is malformed."""


def matches_integrity_format(value: str) -> bool:
    """Return ``True`` when *value* has valid SRI syntax (regex only).

    This checks only the ``sha256-/sha384-/sha512-<base64>`` shape, without
    decoding the payload or checking its length.  Callers that need the full
    byte-length validation should use :func:`validate_integrity`.
    """
    return _INTEGRITY_RE.match(value) is not None


def validate_integrity(value: str) -> None:
    """Raise :class:`IntegrityError` unless *value* is a valid SRI string.

    Accepts ``sha256-`` / ``sha384-`` / ``sha512-`` with valid base64 that
    decodes to the algorithm's expected byte length.  This is the exact check
    the inventory loader applies to ``[runtime.pi-extensions.<name>.artifacts]``
    entries.
    """
    m = _INTEGRITY_RE.match(value)
    if not m:
        raise IntegrityError(
            f"expected sha256-/sha384-/sha512- with base64, got {value!r}"
        )
    algorithm = m.group(1)
    payload = m.group(2)
    try:
        decoded = base64.b64decode(payload, validate=True)
    except Exception as exc:
        raise IntegrityError(f"invalid base64 ({exc})") from exc
    expected_len = _INTEGRITY_LENGTHS[algorithm]
    if len(decoded) != expected_len:
        raise IntegrityError(
            f"expected {expected_len} bytes for sha{algorithm}, got {len(decoded)}"
        )


def is_valid_integrity(value: str) -> bool:
    """Return ``True`` when *value* is a valid SRI integrity string."""
    try:
        validate_integrity(value)
        return True
    except IntegrityError:
        return False
