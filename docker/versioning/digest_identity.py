"""Canonical digest identity shared by hex SHA-256 and SRI callers.

A :class:`DigestIdentity` is the single content-address authority for a
verified blob: a lower-case hash algorithm plus the raw digest bytes.
Hex SHA-256 build callers and SRI runtime callers are only *encodings* of
the same value; equality, hashing, deduplication, and cache-path derivation
therefore never depend on which encoding produced the identity.

Only the reviewed algorithms (``sha256``, ``sha384``, ``sha512``) with
their fixed byte lengths are accepted.  Malformed hex, malformed SRI,
unsupported algorithms, and wrong byte lengths are rejected before any path
can be derived or any cache work can begin.

The module imports only the shared SRI format validator so SRI parsing is
not duplicated here or in the runtime cache.
"""

from __future__ import annotations

import base64
import os
import re
from dataclasses import dataclass

from docker.versioning.integrity import IntegrityError, validate_integrity

# Expected raw digest byte length for each supported algorithm.
_DIGEST_BYTE_LENGTHS: dict[str, int] = {
    "sha256": 32,
    "sha384": 48,
    "sha512": 64,
}


class DigestIdentityError(ValueError):
    """A digest identity string or byte sequence is malformed or unsupported."""


@dataclass(frozen=True)
class DigestIdentity:
    """Immutable algorithm + digest-bytes content identity.

    Two identities are equal exactly when their algorithm and raw digest
    bytes are equal, regardless of the hex or SRI encoding that produced
    them.  Instances are hashable and safe as dict keys / set members.
    """

    algorithm: str
    """Lower-case hash algorithm (``sha256``, ``sha384``, or ``sha512``)."""

    digest_bytes: bytes
    """Raw digest bytes; length is fixed by the algorithm."""

    def __post_init__(self) -> None:
        if not isinstance(self.algorithm, str):
            raise DigestIdentityError(
                f"algorithm must be a string, got {type(self.algorithm).__name__}"
            )
        algorithm = self.algorithm.lower()
        if algorithm not in _DIGEST_BYTE_LENGTHS:
            raise DigestIdentityError(
                f"unsupported algorithm {self.algorithm!r}; must be one of "
                f"{sorted(_DIGEST_BYTE_LENGTHS)}"
            )
        object.__setattr__(self, "algorithm", algorithm)
        if not isinstance(self.digest_bytes, bytes):
            raise DigestIdentityError(
                f"digest_bytes must be bytes, got {type(self.digest_bytes).__name__}"
            )
        expected = _DIGEST_BYTE_LENGTHS[algorithm]
        if len(self.digest_bytes) != expected:
            raise DigestIdentityError(
                f"expected {expected} digest bytes for {algorithm}, "
                f"got {len(self.digest_bytes)}"
            )

    # ── encodings ───────────────────────────────────────────────────

    @classmethod
    def from_hex(cls, algorithm: str, hex_digest: str) -> "DigestIdentity":
        """Build an identity from a hex digest string.

        The algorithm name is normalized to lower case; the hex string must
        decode to exactly the algorithm's expected byte length.
        """
        if not isinstance(algorithm, str):
            raise DigestIdentityError(
                f"algorithm must be a string, got {type(algorithm).__name__}"
            )
        normalized = algorithm.lower()
        if normalized not in _DIGEST_BYTE_LENGTHS:
            raise DigestIdentityError(
                f"unsupported algorithm {algorithm!r}; must be one of "
                f"{sorted(_DIGEST_BYTE_LENGTHS)}"
            )
        if not isinstance(hex_digest, str):
            raise DigestIdentityError(
                f"hex_digest must be a string, got {type(hex_digest).__name__}"
            )
        expected_chars = _DIGEST_BYTE_LENGTHS[normalized] * 2
        if len(hex_digest) != expected_chars:
            raise DigestIdentityError(
                f"expected {expected_chars} hex characters for {normalized}, "
                f"got {len(hex_digest)}"
            )
        try:
            digest_bytes = bytes.fromhex(hex_digest)
        except ValueError as exc:
            raise DigestIdentityError(
                f"invalid hex digest for {normalized}: {hex_digest!r}"
            ) from exc
        return cls(normalized, digest_bytes)

    @classmethod
    def from_sri(cls, integrity: str) -> "DigestIdentity":
        """Build an identity from an SRI integrity string.

        Reuses the shared SRI validator so format and byte-length rules are
        identical to the runtime cache.  ``sha256-``, ``sha384-``, and
        ``sha512-`` with standard base64 are accepted.
        """
        if not isinstance(integrity, str):
            raise DigestIdentityError(
                f"integrity must be a string, got {type(integrity).__name__}"
            )
        try:
            validate_integrity(integrity)
        except IntegrityError as exc:
            raise DigestIdentityError(str(exc)) from exc
        algorithm, payload = integrity.split("-", 1)
        try:
            digest_bytes = base64.b64decode(payload, validate=True)
        except Exception as exc:  # pragma: no cover - validate_integrity guards
            raise DigestIdentityError(f"invalid base64 in {integrity!r}") from exc
        return cls(algorithm.lower(), digest_bytes)

    def hex_digest(self) -> str:
        """Return the lower-case hex encoding of the digest bytes."""
        return self.digest_bytes.hex()

    def base64_digest(self) -> str:
        """Return the standard-base64 digest payload."""
        return base64.b64encode(self.digest_bytes).decode("ascii")

    def sri(self) -> str:
        """Return the SRI encoding (``<algorithm>-<standard base64>``)."""
        return f"{self.algorithm}-{self.base64_digest()}"

    def filesystem_safe_digest(self) -> str:
        """Return the canonical, filesystem-safe digest path component.

        Hex is always safe (no ``/``, ``\\``, ``+``, or padding) and is the
        single canonical encoding used for cache paths, so hex and SRI
        callers derive the *same* path.
        """
        return self.hex_digest()

    def runtime_safe_digest(self) -> str:
        """Return the legacy runtime-cache URL-safe base64 component."""
        return self.base64_digest().replace("+", "-").replace("/", "_")

    @classmethod
    def runtime_cache_path_from_component(
        cls, algorithm: str, digest: str, root: str
    ) -> str:
        """Build a legacy runtime path from an encoded component."""
        if algorithm not in _DIGEST_BYTE_LENGTHS:
            raise DigestIdentityError(f"unsupported algorithm {algorithm!r}")
        if not re.fullmatch(r"[A-Za-z0-9._-]+={0,2}", digest) or digest in (".", ".."):
            raise DigestIdentityError("invalid runtime digest component")
        return os.path.join(root, algorithm, digest + ".tgz")

    # ── canonical cache path ────────────────────────────────────────

    def cache_path(self, root: str, *, extension: str = ".blob") -> str:
        """Return the deterministic content-addressed cache path.

        The path is ``<root>/<algorithm>/<hex digest><extension>`` and
        depends solely on the algorithm and digest bytes, never on the
        caller's encoding, URL, package name, or version.
        """
        return os.path.join(root, self.algorithm, self.hex_digest() + extension)
