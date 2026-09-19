"""Deterministic assembler and assembler-input identities.

An :class:`AssemblerIdentity` captures everything that defines the standalone
assembler itself (pinned image, asserted tools, script/policy bytes, target
platform).  An :class:`AssemblerInputIdentity` binds canonical roots, the
exact lockfile bytes, and the assembler identity into one content-derived
digest.  It describes assembly *inputs* only: it is a stable input key, not
an output-tree or evidence content address.

All digests are SHA-256 hex over a canonical, key-sorted JSON encoding, so
identity is stable across JSON key order and Python process runs.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .errors import LockedNpmError
from .model import RootSpec, ValidatedAssemblyInput

#: Length of the canonical digest prefix in the assembler container name.
#:
#: The standalone assembler runs in a container named
#: ``npm-assembler-<digest[:ASSEMBLER_DIGEST_PREFIX_LENGTH]>``.  This single
#: constant keeps the producer (:mod:`docker.npm_environment.execution`) and
#: every validator of that name (for example
#: :mod:`docker.versioning.diagnostic_projection`) in agreement.
ASSEMBLER_DIGEST_PREFIX_LENGTH = 16


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _assembler_digest(
    *,
    image_digest: str,
    node_version: str,
    npm_version: str,
    script_digest: str,
    policy_digest: str,
    platform: str,
) -> str:
    """Return the canonical SHA-256 digest over the six assembler fields."""
    payload = _canonical_json(
        {
            "image_digest": image_digest,
            "node_version": node_version,
            "npm_version": npm_version,
            "script_digest": script_digest,
            "policy_digest": policy_digest,
            "platform": platform,
        }
    ).encode("utf-8")
    return _sha256_hex(payload)


@dataclass(frozen=True)
class AssemblerIdentity:
    """Immutable identity of the standalone assembler implementation."""

    image_digest: str
    node_version: str
    npm_version: str
    script_digest: str
    policy_digest: str
    platform: str
    digest: str
    """Canonical SHA-256 over the six component fields."""


def compute_assembler_identity(
    *,
    image_digest: str,
    node_version: str,
    npm_version: str,
    script_digest: str,
    policy_digest: str,
    platform: str,
) -> AssemblerIdentity:
    """Derive an :class:`AssemblerIdentity` and its canonical digest."""
    digest = _assembler_digest(
        image_digest=image_digest,
        node_version=node_version,
        npm_version=npm_version,
        script_digest=script_digest,
        policy_digest=policy_digest,
        platform=platform,
    )
    return AssemblerIdentity(
        image_digest=image_digest,
        node_version=node_version,
        npm_version=npm_version,
        script_digest=script_digest,
        policy_digest=policy_digest,
        platform=platform,
        digest=digest,
    )


@dataclass(frozen=True)
class AssemblerInputIdentity:
    """Immutable identity of one assembler *input* bundle.

    Identifies only the canonical reviewed roots, the exact lockfile-byte
    digest, and the assembler identity — never assembled output bytes, a
    canonical tree digest, or evidence.  It is a stable input key, not a
    content address for assembled output.
    """

    roots: tuple[RootSpec, ...]
    """Canonical (name-sorted) exact roots."""

    lockfile_digest: str
    """SHA-256 of the exact ``package-lock.json`` bytes."""

    assembler: AssemblerIdentity
    """The assembler identity the inputs are assembled with."""

    digest: str
    """Canonical SHA-256 over roots, lockfile digest, assembler digest, and
    (when reviewed) the install-package digest."""

    package_digest: str | None = None
    """SHA-256 of the exact ``pi-coding-agent-install-package.json`` bytes,
    or ``None`` when no install-package manifest was reviewed."""


def input_identity_digest(
    *,
    roots: tuple[RootSpec, ...],
    lockfile_digest: str,
    assembler_digest: str,
    package_digest: str | None = None,
) -> str:
    """Return the canonical input-identity digest from its components.

    This is the pure, preflight-free form of the digest: it re-derives the
    canonical payload from the name-sorted exact roots, the exact lockfile
    digest, the already-verified assembler digest, and the exact
    install-package digest (or ``null`` when no package was reviewed).
    Evidence verification uses it to re-check a serialized input identity
    without holding the original lockfile bytes.
    """
    ordered_roots = tuple(sorted(roots))
    payload = _canonical_json(
        {
            "roots": [{"name": r.name, "version": r.version} for r in ordered_roots],
            "lockfile_digest": lockfile_digest,
            "assembler_digest": assembler_digest,
            "package_digest": package_digest,
        }
    ).encode("utf-8")
    return _sha256_hex(payload)


def compute_assembler_input_identity(
    validated: ValidatedAssemblyInput,
    assembler: AssemblerIdentity,
) -> AssemblerInputIdentity:
    """Derive an :class:`AssemblerInputIdentity` for a validated input.

    Two supplied values are re-verified, not trusted: the assembler
    identity's digest is recomputed from its six components and must match
    ``assembler.digest``; and the :class:`ValidatedAssemblyInput` is
    re-derived by re-running preflight from its bytes and must match the
    supplied value field-for-field (after its lockfile digest is rechecked
    against its bytes).  The assembler's platform and reviewed Node/npm
    versions must also match the validated input.  Any mismatch is rejected
    before a digest is produced.
    """
    # Re-verify the supplied assembler identity: its components must hash to
    # its claimed digest.  This rejects a stale or forged ``digest`` before
    # it can contribute to the input-identity payload.
    if _assembler_digest(
        image_digest=assembler.image_digest,
        node_version=assembler.node_version,
        npm_version=assembler.npm_version,
        script_digest=assembler.script_digest,
        policy_digest=assembler.policy_digest,
        platform=assembler.platform,
    ) != assembler.digest:
        raise LockedNpmError(
            "assembler_identity_mismatch",
            "the assembler components do not match its digest",
        )
    lockfile_digest = _sha256_hex(validated.lockfile_bytes)
    if lockfile_digest != validated.lockfile_digest:
        raise LockedNpmError(
            "lockfile_bytes_mismatch",
            "the validated lockfile bytes do not match the validated "
            "lockfile digest; preflight must bind the exact bytes",
        )
    package_digest = (
        _sha256_hex(validated.package_bytes)
        if validated.package_bytes is not None else None
    )
    if package_digest != validated.package_digest:
        raise LockedNpmError(
            "package_bytes_mismatch",
            "the validated install-package bytes do not match the validated "
            "package digest; preflight must bind the exact bytes",
        )

    # Re-run preflight from the exact bytes and claimed roots/tools, then
    # require the fresh result to equal the supplied value field-for-field.
    # This re-validates the closure, omissions, integrity-less records,
    # reviewed-root metadata, and install-package binding instead of trusting
    # the supplied dataclass.
    from .preflight import preflight

    revalidated = preflight(
        validated.lockfile_bytes,
        roots=validated.roots,
        platform=validated.platform,
        node_version=validated.node_version,
        npm_version=validated.npm_version,
        package_bytes=validated.package_bytes,
    )
    if revalidated != validated:
        raise LockedNpmError(
            "validated_input_mismatch",
            "the supplied validated input does not match the preflight "
            "result derived from its bytes; obtain the input from preflight, "
            "not by reconstructing or modifying its fields",
        )
    if validated.platform != assembler.platform:
        raise LockedNpmError(
            "platform_mismatch",
            f"validated platform {validated.platform!r} does not match "
            f"assembler platform {assembler.platform!r}",
        )
    if validated.node_version != assembler.node_version:
        raise LockedNpmError(
            "node_version_mismatch",
            f"validated Node version {validated.node_version!r} does not "
            f"match assembler Node version {assembler.node_version!r}",
        )
    if validated.npm_version != assembler.npm_version:
        raise LockedNpmError(
            "npm_version_mismatch",
            f"validated npm version {validated.npm_version!r} does not "
            f"match assembler npm version {assembler.npm_version!r}",
        )
    roots = tuple(sorted(validated.roots))
    return AssemblerInputIdentity(
        roots=roots,
        lockfile_digest=lockfile_digest,
        assembler=assembler,
        digest=input_identity_digest(
            roots=roots,
            lockfile_digest=lockfile_digest,
            assembler_digest=assembler.digest,
            package_digest=validated.package_digest,
        ),
        package_digest=validated.package_digest,
    )
