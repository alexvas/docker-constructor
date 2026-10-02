"""Immutable, monotonically numbered build-generation manifests.

Committed build state is a sequence of immutable no-clobber manifest files
named ``committed-build-<20 decimal digits>.json``.  The suffix is exactly
``GENERATION_DIGITS`` digits and strictly nonzero, so lexical order equals
numeric order.  The newest valid generation is authoritative; at most one
immediately previous generation may coexist as durable evidence that
superseded-blob cleanup is incomplete.

This module owns the *build-domain* schema and sequencing.  It is an L3
protocol: it composes the shared L2 durable no-clobber leaf contract and the
canonical JSON codec, but the manifest's closed field set, the generation
namespace, and the zero/one/two classification live here.  No shared layer
performs schema, version, identity, or authority validation.

Discovery is deliberately two-phase.  :func:`inspect_generations` lists,
reads, and classifies entries without granting authority.  Only
:func:`discover_generations` accepts a discovered newest generation as
authoritative, and only after successfully synchronizing the generation
directory: a no-clobber commit may leave a complete entry visible even when
its publishing parent-directory ``fsync`` failed, so filename discovery alone
is never proof of durable commit.  Ambiguous, corrupt, malformed, or
unsafe state fails closed without deleting or repairing anything.

Publication allocates ``max(valid) + 1`` under the checkout-wide exclusive
lock and publishes it through the durable no-clobber contract; it never
modifies or replaces an existing generation.  ``committed-build.json`` is the
legacy mutable name and is outside this namespace: discovery neither inspects
nor adopts nor deletes it.
"""
from __future__ import annotations

import enum
import os
from dataclasses import dataclass
from typing import Iterable

from docker.transactions.capabilities import DirectoryCapability
from docker.transactions.codec import decode as _decode_json
from docker.transactions.codec import encode as _encode_json
from docker.transactions.errors import TransactionError
from docker.transactions.locking import LockCapability, LockPolicy
from docker.transactions.posix import PosixFileOps
from docker.transactions.regular import RegularFileContracts
from docker.versioning.build_cache import BuildCacheError
from docker.versioning.digest_identity import DigestIdentity, DigestIdentityError

GENERATION_PREFIX = "committed-build-"
"""Filename prefix for immutable build-generation manifests."""

GENERATION_SUFFIX = ".json"
"""Filename suffix for immutable build-generation manifests."""

LEGACY_MANIFEST_NAME = "committed-build.json"
"""Legacy mutable manifest name; outside generation-state discovery."""

GENERATION_DIGITS = 20
"""Exact width of the zero-padded nonzero decimal generation suffix."""

MAX_GENERATION = 10**GENERATION_DIGITS - 1
"""Largest representable generation number; overflow fails before mutation."""

MANIFEST_VERSION = 1
"""The single supported build-owned manifest schema version."""

GENERATION_MODE = 0o600
"""Exact owner-private mode of a published generation manifest."""

BUILD_LOCK_NAME = "build.lock"
"""The checkout-wide build lock entry within the generation directory."""

BUILD_LOCK_NAMESPACE = "build-generation"
"""Logical scope of the checkout-wide build critical section."""

_MANIFEST_FIELDS = frozenset({"version", "blobs"})


class BuildGenerationError(BuildCacheError):
    """Build generation state is malformed, ambiguous, unsafe, or unusable."""


# ═══════════════════════════════════════════════════════════════════════
# Canonical generation naming
# ═══════════════════════════════════════════════════════════════════════


def format_generation_name(number: int) -> str:
    """Return the canonical filename for generation *number*.

    Rejects non-integers, booleans, zero, negatives, and overflow so a
    consumer cannot mint a name outside the fixed-width namespace.
    """
    if isinstance(number, bool) or not isinstance(number, int):
        raise BuildGenerationError("generation number must be an integer")
    if number < 1 or number > MAX_GENERATION:
        raise BuildGenerationError(f"generation number {number!r} is out of range")
    return f"{GENERATION_PREFIX}{number:0{GENERATION_DIGITS}d}{GENERATION_SUFFIX}"


def is_generation_candidate(name: str) -> bool:
    """Return whether *name* belongs to the generation namespace.

    A candidate is any entry carrying the generation prefix.  The legacy
    mutable name ``committed-build.json`` does not carry the ``-`` separator
    and is therefore not a candidate.  A candidate whose suffix is not
    canonical must fail closed, so callers classify with
    :func:`parse_generation_name` before use.
    """
    return isinstance(name, str) and name.startswith(GENERATION_PREFIX)


def parse_generation_name(name: str) -> int | None:
    """Return the generation number for a canonical name, else ``None``.

    Non-candidate names (including the legacy manifest and unrelated
    directory entries) and malformed candidates both yield ``None``; only a
    caller that already knows the name is a candidate can treat a ``None``
    result as a fail-closed condition.
    """
    if not is_generation_candidate(name):
        return None
    if not name.endswith(GENERATION_SUFFIX):
        return None
    digits = name[len(GENERATION_PREFIX) : -len(GENERATION_SUFFIX)]
    if len(digits) != GENERATION_DIGITS:
        return None
    if not digits.isascii() or not digits.isdigit():
        return None
    number = int(digits)
    if number < 1 or number > MAX_GENERATION:
        return None
    return number


# ═══════════════════════════════════════════════════════════════════════
# Build-owned closed manifest schema
# ═══════════════════════════════════════════════════════════════════════


def canonical_blob_key(identity: DigestIdentity) -> str:
    """Return the canonical ``algorithm:hex`` key for *identity*."""
    if not isinstance(identity, DigestIdentity):
        raise BuildGenerationError("blob identity must be a DigestIdentity")
    return f"{identity.algorithm}:{identity.hex_digest()}"


def identity_from_blob_key(key: object) -> DigestIdentity:
    """Parse one canonical manifest key, rejecting every unsafe form."""
    if not isinstance(key, str) or key.count(":") != 1:
        raise BuildGenerationError(
            "blob key must be one canonical algorithm:digest string"
        )
    algorithm, digest = key.split(":")
    if not algorithm or not digest or any(token in key for token in ("/", "\\", "..")):
        raise BuildGenerationError("blob key contains a path component")
    try:
        identity = DigestIdentity.from_hex(algorithm, digest)
    except DigestIdentityError as exc:
        raise BuildGenerationError(f"invalid blob key: {key!r}") from exc
    if canonical_blob_key(identity) != key:
        raise BuildGenerationError("blob key is not canonical")
    return identity


@dataclass(frozen=True)
class BuildManifest:
    """The closed, build-owned committed-manifest schema.

    The only fields are the explicit ``version`` and the canonical unique
    ``blobs`` identity list.  Unknown versions and unknown fields are
    rejected; the shared codec supplies deterministic bytes but performs no
    field interpretation.
    """

    blobs: tuple[DigestIdentity, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.blobs, tuple):
            raise BuildGenerationError("manifest blobs must be a tuple")
        for blob in self.blobs:
            if not isinstance(blob, DigestIdentity):
                raise BuildGenerationError("manifest blobs must be DigestIdentity values")
        keys = [canonical_blob_key(blob) for blob in self.blobs]
        if len(set(keys)) != len(keys):
            raise BuildGenerationError("manifest contains duplicate blob identities")
        if keys != sorted(keys):
            raise BuildGenerationError("manifest blobs are not in canonical order")

    @classmethod
    def from_blobs(cls, identities: Iterable[DigestIdentity]) -> "BuildManifest":
        """Build a canonical manifest, sorting and rejecting non-identities."""
        try:
            values = tuple(identities)
        except TypeError as exc:
            raise BuildGenerationError(
                "manifest blobs must be an iterable of DigestIdentity values"
            ) from exc
        for blob in values:
            if not isinstance(blob, DigestIdentity):
                raise BuildGenerationError("manifest blobs must be DigestIdentity values")
        return cls(tuple(sorted(set(values), key=canonical_blob_key)))

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(canonical_blob_key(blob) for blob in self.blobs)

    def encode(self) -> bytes:
        """Return deterministic canonical JSON bytes for this manifest."""
        return _encode_json(
            {"version": MANIFEST_VERSION, "blobs": list(self.keys)}
        )

    @classmethod
    def decode(cls, data: bytes | bytearray | memoryview) -> "BuildManifest":
        """Validate *data* against the closed schema and return a manifest."""
        try:
            value = _decode_json(data)
        except (ValueError, TypeError, UnicodeDecodeError) as exc:
            raise BuildGenerationError("generation manifest is not valid JSON") from exc
        if not isinstance(value, dict):
            raise BuildGenerationError("generation manifest is not an object")
        if set(value.keys()) != _MANIFEST_FIELDS:
            raise BuildGenerationError(
                "generation manifest has unknown or missing fields"
            )
        version = value["version"]
        if isinstance(version, bool) or not isinstance(version, int):
            raise BuildGenerationError("generation manifest version is not an integer")
        if version != MANIFEST_VERSION:
            raise BuildGenerationError(
                f"unsupported generation manifest version: {version!r}"
            )
        blobs = value["blobs"]
        if not isinstance(blobs, list):
            raise BuildGenerationError("generation manifest blobs is not a list")
        identities = [identity_from_blob_key(key) for key in blobs]
        keys = [canonical_blob_key(blob) for blob in identities]
        if len(set(keys)) != len(keys):
            raise BuildGenerationError(
                "generation manifest contains duplicate blob identities"
            )
        if keys != sorted(keys):
            raise BuildGenerationError(
                "generation manifest blobs are not in canonical order"
            )
        return cls(tuple(identities))


# ═══════════════════════════════════════════════════════════════════════
# Generation state
# ═══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class BuildGeneration:
    """One immutable manifest generation and its canonical position."""

    number: int
    name: str
    manifest: BuildManifest

    @property
    def blobs(self) -> tuple[DigestIdentity, ...]:
        return self.manifest.blobs


class GenerationState(enum.Enum):
    """Classification of the visible generation set."""

    EMPTY = "empty"
    """No generation exists yet; valid before the first successful build."""

    STABLE = "stable"
    """Exactly one generation is authoritative."""

    RECOVERABLE = "recoverable"
    """Exactly two generations coexist; the older is cleanup evidence."""


@dataclass(frozen=True)
class GenerationInventory:
    """A validated generation set with zero, one, or two entries."""

    state: GenerationState
    generations: tuple[BuildGeneration, ...]

    @property
    def current(self) -> BuildGeneration | None:
        """The authoritative (newest) generation, if any."""
        return self.generations[-1] if self.generations else None

    @property
    def previous(self) -> BuildGeneration | None:
        """The retained predecessor, if two generations coexist."""
        return self.generations[0] if len(self.generations) == 2 else None


# ═══════════════════════════════════════════════════════════════════════
# Discovery and publication
# ═══════════════════════════════════════════════════════════════════════


def _read_generation(
    contracts: RegularFileContracts,
    directory: DirectoryCapability,
    name: str,
) -> BuildManifest:
    try:
        data = contracts.validated_read(
            directory, name, allowed_mode=GENERATION_MODE
        )
    except TransactionError as exc:
        raise BuildGenerationError(f"unsafe build generation entry: {name!r}") from exc
    try:
        return BuildManifest.decode(data)
    except BuildGenerationError as exc:
        raise BuildGenerationError(f"corrupt build generation: {name!r}") from exc


def inspect_generations(
    ops: PosixFileOps, directory: DirectoryCapability
) -> GenerationInventory:
    """List, read, and classify generations without granting authority.

    Raises :class:`BuildGenerationError` for malformed names, unsafe entries,
    corrupt manifests, more than two generations, or a two-generation state
    whose generations are not consecutive.  It performs no mutation: nothing
    is deleted, repaired, or synchronized.
    """
    contracts = RegularFileContracts(ops)
    try:
        names = os.listdir(directory.fd)
    except OSError as exc:
        raise BuildGenerationError("cannot list the build generation directory") from exc
    generations: list[BuildGeneration] = []
    for name in sorted(names):
        if name == LEGACY_MANIFEST_NAME:
            # The legacy mutable name is outside this namespace: never
            # inspect, adopt, reject, or delete it.
            continue
        if not is_generation_candidate(name):
            continue
        number = parse_generation_name(name)
        if number is None:
            raise BuildGenerationError(f"malformed build generation name: {name!r}")
        manifest = _read_generation(contracts, directory, name)
        generations.append(BuildGeneration(number, name, manifest))
    generations.sort(key=lambda generation: generation.number)
    if len(generations) > 2:
        raise BuildGenerationError("more than two build generations are ambiguous")
    if not generations:
        state = GenerationState.EMPTY
    elif len(generations) == 1:
        state = GenerationState.STABLE
    else:
        # Publication is strictly max + 1, so a recoverable pair can only be
        # (n, n + 1).  A gap is an impossible state and must not be trusted.
        if generations[1].number != generations[0].number + 1:
            raise BuildGenerationError(
                "non-consecutive build generations are ambiguous"
            )
        state = GenerationState.RECOVERABLE
    return GenerationInventory(state=state, generations=tuple(generations))


def _synchronize_generation_directory(
    ops: PosixFileOps, directory: DirectoryCapability
) -> None:
    """Synchronize the generation directory to disk, or fail closed."""
    try:
        ops.fsync(directory.fd)
    except OSError as exc:
        raise BuildGenerationError(
            "cannot synchronize the build generation directory"
        ) from exc


def discover_generations(
    ops: PosixFileOps,
    directory: DirectoryCapability,
    *,
    lock: LockCapability,
) -> GenerationInventory:
    """Accept a discovered generation set as authoritative under *lock*.

    The generation directory is synchronized before any inventory is
    returned, completing a publication that became visible before its
    parent-directory ``fsync`` failed.  Synchronization failure preserves
    every generation and raises :class:`BuildGenerationError`.
    """
    lock.assert_authorizes(directory=directory, namespace=BUILD_LOCK_NAMESPACE)
    inventory = inspect_generations(ops, directory)
    _synchronize_generation_directory(ops, directory)
    return inventory


def publish_generation(
    ops: PosixFileOps,
    directory: DirectoryCapability,
    blobs: Iterable[DigestIdentity],
    *,
    lock: LockCapability,
) -> BuildGeneration:
    """Durably publish ``max(valid) + 1`` under *lock*.

    Publication first validates the lock and classifies state from a
    non-authoritative inspection: invalid input, ambiguous state, and counter
    overflow are rejected before any filesystem synchronization or mutation.
    Only after those preflight checks pass is the generation directory
    synchronized, at which point the inspected inventory is trusted and
    ``max(valid) + 1`` is written once through the durable no-clobber
    contract.  A generation left visible by a failed publication is therefore
    either completed by this synchronization or rejected, never silently
    adopted and completed by the retry's own directory ``fsync``.  An existing
    generation is never modified or replaced.
    """
    lock.assert_authorizes(directory=directory, namespace=BUILD_LOCK_NAMESPACE)
    inventory = inspect_generations(ops, directory)
    if inventory.state is GenerationState.RECOVERABLE:
        raise BuildGenerationError(
            "cannot publish while a previous generation awaits cleanup"
        )
    manifest = BuildManifest.from_blobs(blobs)
    current = inventory.current
    next_number = (current.number if current is not None else 0) + 1
    # format_generation_name rejects overflow before any filesystem change.
    name = format_generation_name(next_number)
    # The lock is held throughout, so the inventory is authoritative once the
    # generation directory is synchronized.
    _synchronize_generation_directory(ops, directory)
    contracts = RegularFileContracts(ops)
    try:
        contracts.durable_no_clobber(
            directory, name, manifest.encode(), GENERATION_MODE
        )
    except TransactionError as exc:
        raise BuildGenerationError(
            f"cannot publish build generation {name!r}"
        ) from exc
    return BuildGeneration(next_number, name, manifest)


def acquire_build_generation_lock(
    ops: PosixFileOps, directory: DirectoryCapability
) -> LockCapability:
    """Acquire the checkout-wide fail-fast build lock over *directory*."""
    return LockCapability.acquire(
        ops,
        directory,
        BUILD_LOCK_NAME,
        namespace=BUILD_LOCK_NAMESPACE,
        policy=LockPolicy.FAIL_FAST,
    )
