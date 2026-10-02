"""Deterministic JSON byte encoding and generic JSON value decoding.

The codec orders mapping keys, emits compact UTF-8 bytes, and rejects
non-finite numbers and unsupported values.  It does not interpret a
record's fields beyond generic JSON.
"""
from __future__ import annotations

import json
import math
from typing import Any


def _check(value: Any) -> None:
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite numbers cannot be encoded")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("mapping keys must be strings")
            _check(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _check(item)
        return
    raise TypeError(f"unsupported value cannot be encoded: {type(value).__name__}")


def encode(value: Any) -> bytes:
    """Return deterministic compact UTF-8 bytes for *value*."""
    _check(value)
    text = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return text.encode("utf-8")


def _reject_constant(token: str) -> Any:
    raise ValueError(f"non-finite numbers cannot be decoded: {token}")


def _parse_finite_float(token: str) -> float:
    """Parse a JSON float token, rejecting numeric overflow to infinity.

    ``json``'s default float parser accepts tokens such as ``1e400`` and
    returns ``inf`` without consulting ``parse_constant``; this callback closes
    that gap so overflow is rejected like any other non-finite value.
    """
    value = float(token)
    if not math.isfinite(value):
        raise ValueError(f"non-finite numbers cannot be decoded: {token}")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build an object from *pairs*, rejecting any repeated key.

    Python's default parser silently keeps the last value for a repeated JSON
    object key.  Ambiguous records must fail closed instead of depending on
    first-wins or last-wins ordering, so repeating a key raises ``ValueError``.
    """
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate object key cannot be decoded: {key!r}")
        result[key] = value
    return result


def decode(data: bytes | bytearray | memoryview) -> Any:
    """Decode *data* into ordinary Python values without field interpretation.

    Non-standard ``NaN``, ``Infinity``, and ``-Infinity`` tokens are rejected
    even though Python's default parser would accept them, float tokens that
    overflow to infinity (for example ``1e400``) are rejected as well, and a
    repeated key in any JSON object is rejected rather than resolved by
    first-wins or last-wins behavior.
    """
    return json.loads(
        bytes(data).decode("utf-8"),
        parse_constant=_reject_constant,
        parse_float=_parse_finite_float,
        object_pairs_hook=_reject_duplicate_keys,
    )
