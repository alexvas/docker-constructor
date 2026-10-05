"""L3 build-domain sequential cleanup and recovery for immutable generations.

Phase 3 publishes immutable generations and lets at most one immediately
previous generation coexist as durable evidence that superseded-blob cleanup
is incomplete.  This module owns the *build-domain* sequencing that consumes
that evidence:

* :func:`superseded_candidates` derives exactly ``previous - current`` over
  validated canonical identities; a current-generation identity is never a
  candidate.
* :func:`reconcile_authoritative_markers` removes the uncommitted markers for
  every blob admitted to the authoritative generation as one batch and
  synchronizes the shared marker directory once, even when every marker is
  already absent.  It never precedes durable generation authority and never
  removes a marker for a blob that is not committed.
* :func:`cleanup_superseded` attempts every superseded candidate, batches
  removals by containing directory, synchronizes each affected existing blob
  directory and the shared marker directory once after their batches, reports
  every failure, and durably removes the previous manifest only after every
  candidate is durably absent.
* :func:`recover_generations` repeats discovery, authoritative-marker
  reconciliation, and idempotent cleanup under the checkout lock before any
  build side effect.

Only the shared L2 durable-unlink leaf contract is composed for the
predecessor manifest itself.  Batch candidate removals deliberately descend to
L0 ``unlinkat`` because the required commit unit is a whole directory batch:
performing one directory ``fsync`` per candidate would violate the batching
contract.  Each blob and marker is nonetheless validated through a retained
no-follow descriptor (regular, owned, single-linked, and domain mode where one
is required) before its basename is unlinked, so removal never follows a
symlink or mutates an unsafe entry.  The build domain keeps every identity,
authority, recovery, and retention decision; no shared layer deletes blobs,
markers, or generations.

A missing algorithm directory means the blob is already absent and still
authorizes removing its marker.  An unsafe (symlinked, foreign-owned,
non-directory, or non-``0700``) algorithm directory fails closed: the
candidate blob and its marker are both preserved, and the predecessor is
retained.  Ordinary ``OSError`` cleanup failures are aggregated into
:class:`BuildCleanupError`; process-control interruptions propagate unchanged.
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass

from docker.transactions.capabilities import DirectoryCapability
from docker.transactions.cleanup import CleanupFailures
from docker.transactions.errors import (
    STAGE_VALIDATE,
    CapabilityError,
    TransactionError,
    UnsafeFileError,
)
from docker.transactions.locking import LockCapability
from docker.transactions.posix import PosixFileOps
from docker.transactions.regular import RegularFileContracts
from docker.versioning.build_generations import (
    BUILD_LOCK_NAMESPACE,
    GENERATION_MODE,
    BuildGeneration,
    BuildGenerationError,
    GenerationInventory,
    canonical_blob_key,
    discover_generations,
)
from docker.versioning.digest_identity import DigestIdentity

MARKER_SUFFIX = ".json"
"""Filename suffix of an uncommitted-blob retention marker."""

BLOB_SUFFIX = ".blob"
"""Filename suffix of a content-addressed build blob."""

ALGORITHM_DIRECTORY_MODE = 0o700
"""Exact owner-private mode of a blob algorithm directory."""

BLOB_MODE = 0o444
"""Exact mode of an immutable content-addressed build blob."""

MARKER_FORBIDDEN_BITS = 0o022
"""Marker permission bits that must never be set (group/other write)."""

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
# O_PATH opens a leaf without requiring read permission and, combined with
# O_NOFOLLOW, returns a descriptor for a symlink itself instead of failing with
# ELOOP, so ``fstat`` can classify it.  The fallback is a documented non-Linux
# limitation: a symlink then fails the open, which still fails closed.
_O_PATH = getattr(os, "O_PATH", 0)

_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)

if _O_PATH:  # pragma: no cover - platform branch
    _LEAF_FLAGS = _O_PATH | _NOFOLLOW | _CLOEXEC
else:  # pragma: no cover - non-Linux fallback
    _LEAF_FLAGS = os.O_RDONLY | _NOFOLLOW | _CLOEXEC | _NONBLOCK


@dataclass(frozen=True)
class CleanupFailure:
    """One aggregated cleanup failure and the step that produced it."""

    target: str
    error: BaseException


class BuildCleanupError(BuildGenerationError):
    """One or more build cleanup or recovery steps failed; state is preserved.

    ``failures`` carries every aggregated step failure.  A predecessor-removal
    failure may report the predecessor as its target even though the manifest
    is already unlinked; callers must not assume the predecessor remains
    visible after this error.
    """

    def __init__(
        self, message: str, *, failures: tuple[CleanupFailure, ...] = ()
    ) -> None:
        super().__init__(message)
        self.failures = failures


@dataclass(frozen=True)
class BuildStorage:
    """The build-domain directories that cleanup and recovery operate on.

    ``generations`` is the directory holding immutable generation manifests
    (and the checkout lock).  ``blobs`` contains one ``<algorithm>``
    subdirectory per blob algorithm.  ``markers`` is the shared
    uncommitted-marker directory.
    """

    generations: DirectoryCapability
    blobs: DirectoryCapability
    markers: DirectoryCapability


# ═══════════════════════════════════════════════════════════════════════
# Canonical candidate derivation
# ═══════════════════════════════════════════════════════════════════════


def marker_name(blob: DigestIdentity) -> str:
    """Return the canonical marker basename for *blob*."""
    return f"{blob.algorithm}:{blob.hex_digest()}{MARKER_SUFFIX}"


def blob_name(blob: DigestIdentity) -> str:
    """Return the canonical blob basename for *blob* within its algorithm."""
    return f"{blob.hex_digest()}{BLOB_SUFFIX}"


def superseded_candidates(inventory: GenerationInventory) -> tuple[DigestIdentity, ...]:
    """Return exactly ``previous - current`` in canonical order.

    An identity present in the authoritative current generation is never a
    candidate, even when it also appears in the retained predecessor.
    """
    previous = inventory.previous
    if previous is None:
        return ()
    current = inventory.current
    if current is None:
        return ()
    current_keys = set(current.manifest.keys)
    candidates = [
        blob
        for blob in previous.blobs
        if canonical_blob_key(blob) not in current_keys
    ]
    return tuple(sorted(candidates, key=canonical_blob_key))


# ═══════════════════════════════════════════════════════════════════════
# Failure helpers
# ═══════════════════════════════════════════════════════════════════════


def _validate_leaf(
    info: os.stat_result,
    name: str,
    *,
    allowed_mode: int | None,
    forbidden_bits: int,
) -> None:
    """Reject a leaf that does not satisfy build-domain owned-entry authority."""
    if not stat.S_ISREG(info.st_mode):
        raise UnsafeFileError(
            STAGE_VALIDATE, f"build entry {name!r} is not a regular file"
        )
    if info.st_uid != os.geteuid():
        raise UnsafeFileError(
            STAGE_VALIDATE, f"build entry {name!r} is not owned by the invoking user"
        )
    if info.st_nlink != 1:
        raise UnsafeFileError(
            STAGE_VALIDATE, f"build entry {name!r} has multiple hard links"
        )
    if allowed_mode is not None and stat.S_IMODE(info.st_mode) != allowed_mode:
        raise UnsafeFileError(
            STAGE_VALIDATE, f"build entry {name!r} has a forbidden mode"
        )
    if forbidden_bits and info.st_mode & forbidden_bits:
        raise UnsafeFileError(
            STAGE_VALIDATE, f"build entry {name!r} has forbidden permission bits"
        )


def _close_leaf(
    ops: PosixFileOps,
    fd: int,
    failures: list[CleanupFailure],
    target: str,
) -> None:
    """Release a retained leaf descriptor, aggregating an ordinary close failure."""
    try:
        ops.close(fd)
    except OSError as exc:
        failures.append(CleanupFailure(target, exc))


def _unlink_entry(
    ops: PosixFileOps,
    directory: DirectoryCapability,
    name: str,
    failures: list[CleanupFailure],
    *,
    target: str,
    allowed_mode: int | None = None,
    forbidden_bits: int = 0,
) -> bool:
    """Remove one validated owned basename; absent counts as success.

    The leaf is opened descriptor-relatively with ``O_NOFOLLOW`` and validated
    through ``fstat`` as a regular, owned, single-linked entry (with the domain
    mode where one is required) before ``unlinkat`` is issued.  The validated
    descriptor is retained until the basename has been unlinked, so removal
    never follows a symlink, and an unsafe entry is never repaired, replaced,
    or deleted.  A genuinely absent entry is idempotent success; expected
    filesystem errors and unsafe-entry rejections are aggregated without
    replacing an earlier failure, while unexpected exceptions and
    process-control interruptions propagate unchanged.
    """
    base = directory.child_basename(name)
    try:
        fd = ops.openat(directory.fd, base, _LEAF_FLAGS, 0)
    except FileNotFoundError:
        return True
    except OSError as exc:
        failures.append(CleanupFailure(target, exc))
        return False
    closed = False
    try:
        try:
            info = ops.fstat(fd)
            _validate_leaf(
                info,
                base,
                allowed_mode=allowed_mode,
                forbidden_bits=forbidden_bits,
            )
        except (OSError, UnsafeFileError) as exc:
            failures.append(CleanupFailure(target, exc))
            return False
        try:
            ops.unlinkat(directory.fd, base)
        except FileNotFoundError:
            return True
        except OSError as exc:
            failures.append(CleanupFailure(target, exc))
            return False
        return True
    except BaseException as exc:
        # Process-control interruption: release the retained descriptor
        # exactly once, keeping the interruption authoritative over any
        # ordinary close failure.  The close is marked attempted before it is
        # issued so it is never retried.
        closed = True
        accumulator = CleanupFailures(exc)
        accumulator.run(lambda: ops.close(fd), ordinary=(OSError,))
        result = accumulator.complete()
        if result is not None:
            raise result
        raise
    finally:
        if not closed:
            closed = True
            _close_leaf(ops, fd, failures, target)


def _sync_directory(
    ops: PosixFileOps,
    directory: DirectoryCapability,
    failures: list[CleanupFailure],
    *,
    target: str,
) -> None:
    try:
        ops.fsync(directory.fd)
    except OSError as exc:
        failures.append(CleanupFailure(target, exc))


def _algorithm_failure(algorithm: str, error: BaseException) -> CleanupFailure:
    return CleanupFailure(algorithm, error)


def _open_algorithm_directory(
    ops: PosixFileOps, blobs: DirectoryCapability, algorithm: str
) -> tuple[DirectoryCapability | None, CleanupFailure | None]:
    """Open and validate one existing blob algorithm directory.

    Returns ``(None, None)`` when the directory does not exist, its capability
    when it is a safe owner-private directory, or ``(None, failure)`` when the
    entry is unsafe so the caller preserves every candidate under it.
    """
    base = blobs.child_basename(algorithm)
    try:
        fd = ops.openat(blobs.fd, base, _DIRECTORY_FLAGS, 0)
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, _algorithm_failure(algorithm, exc)
    try:
        capability = DirectoryCapability.from_fd(
            ops, fd, f"{blobs.label}/{algorithm}"
        )
    except (CapabilityError, OSError) as exc:
        # The descriptor is still caller-owned: release it, preserving any
        # close failure as a secondary diagnostic, and aggregate the failure
        # so cleanup still attempts candidates in other algorithm directories.
        accumulator = CleanupFailures(exc)
        accumulator.run(lambda: ops.close(fd), ordinary=(OSError,))
        result = accumulator.complete()
        if result is not None:
            raise result
        return None, _algorithm_failure(algorithm, exc)
    except BaseException as exc:
        # An unexpected exception or process-control interruption leaves the
        # rejected descriptor caller-owned; release it exactly once and keep
        # the primary authoritative over any ordinary close failure.
        accumulator = CleanupFailures(exc)
        accumulator.run(lambda: ops.close(fd), ordinary=(OSError,))
        result = accumulator.complete()
        if result is not None:
            raise result
        raise
    try:
        mode = stat.S_IMODE(ops.fstat(capability.fd).st_mode)
    except OSError as exc:
        failure = _algorithm_failure(algorithm, exc)
        _close_algorithm(capability, failure)
        return None, failure
    except BaseException as exc:
        # Unexpected exception or process-control interruption: release the
        # adopted capability exactly once and re-raise the primary unchanged,
        # attaching any close failure as a secondary diagnostic.
        accumulator = CleanupFailures(exc)
        accumulator.run(capability.close, ordinary=(OSError,))
        result = accumulator.complete()
        if result is not None:
            raise result
        raise
    if mode != ALGORITHM_DIRECTORY_MODE:
        failure = _algorithm_failure(
            algorithm,
            BuildCleanupError(
                f"build blob algorithm {algorithm!r} directory has mode "
                f"{oct(mode)}, expected {oct(ALGORITHM_DIRECTORY_MODE)}"
            ),
        )
        _close_algorithm(capability, failure)
        return None, failure
    return capability, None


def _close_algorithm(
    capability: DirectoryCapability, failure: CleanupFailure | None
) -> None:
    if failure is None:
        # Historical contract: an unpaired close failure has nowhere to be
        # aggregated, so it is suppressed.  Every current caller supplies an
        # aggregated failure.
        try:
            capability.close()
        except OSError:
            pass
        return
    accumulator = CleanupFailures(failure.error)
    accumulator.run(capability.close, ordinary=(OSError,))
    result = accumulator.complete()
    if result is not None:
        raise result


# ═══════════════════════════════════════════════════════════════════════
# Authoritative-generation marker reconciliation
# ═══════════════════════════════════════════════════════════════════════


def reconcile_authoritative_markers(
    ops: PosixFileOps,
    storage: BuildStorage,
    current: BuildGeneration,
    *,
    lock: LockCapability,
) -> None:
    """Remove uncommitted markers for every blob in *current*, then sync once.

    Every marker is attempted even after another fails; the shared marker
    directory is synchronized once regardless.  Any failure is reported as
    :class:`BuildCleanupError` so the caller preserves any predecessor and
    blocks superseded cleanup and build work.
    """
    lock.assert_authorizes(
        directory=storage.generations, namespace=BUILD_LOCK_NAMESPACE
    )
    failures: list[CleanupFailure] = []
    for blob in current.blobs:
        target = marker_name(blob)
        _unlink_entry(
            ops,
            storage.markers,
            target,
            failures,
            target=target,
            forbidden_bits=MARKER_FORBIDDEN_BITS,
        )
    _sync_directory(ops, storage.markers, failures, target="markers")
    if failures:
        raise BuildCleanupError(
            f"authoritative-generation marker reconciliation failed for "
            f"{len(failures)} step(s)",
            failures=tuple(failures),
        )


# ═══════════════════════════════════════════════════════════════════════
# Superseded candidate cleanup
# ═══════════════════════════════════════════════════════════════════════


def cleanup_superseded(
    ops: PosixFileOps,
    storage: BuildStorage,
    inventory: GenerationInventory,
    *,
    lock: LockCapability,
) -> None:
    """Durably remove every ``previous - current`` candidate, then the previous.

    Removals are batched: every applicable candidate blob and marker is
    unlinked first, then each affected existing blob directory and the shared
    marker directory are synchronized exactly once.  Every candidate is
    attempted even after another fails, so all failures are aggregated and the
    previous manifest is retained.  Only when every candidate is durably
    absent is the previous manifest removed through the durable-unlink leaf
    contract and the generation directory synchronized.
    """
    lock.assert_authorizes(
        directory=storage.generations, namespace=BUILD_LOCK_NAMESPACE
    )
    previous = inventory.previous
    if previous is None:
        return
    candidates = superseded_candidates(inventory)
    failures: list[CleanupFailure] = []
    directories: dict[str, DirectoryCapability] = {}
    unsafe: set[str] = set()
    primary: BaseException | None = None
    try:
        for algorithm in sorted({blob.algorithm for blob in candidates}):
            capability, failure = _open_algorithm_directory(
                ops, storage.blobs, algorithm
            )
            if failure is not None:
                failures.append(failure)
                unsafe.add(algorithm)
            elif capability is not None:
                directories[algorithm] = capability
        blob_removed: set[DigestIdentity] = set()
        for blob in candidates:
            if blob.algorithm in unsafe:
                continue
            capability = directories.get(blob.algorithm)
            if capability is None:
                # The algorithm directory is absent, so the blob is absent.
                blob_removed.add(blob)
                continue
            if _unlink_entry(
                ops,
                capability,
                blob_name(blob),
                failures,
                target=blob_name(blob),
                allowed_mode=BLOB_MODE,
            ):
                blob_removed.add(blob)
        if candidates:
            for blob in candidates:
                if blob.algorithm in unsafe or blob not in blob_removed:
                    continue
                target = marker_name(blob)
                _unlink_entry(
                    ops,
                    storage.markers,
                    target,
                    failures,
                    target=target,
                    forbidden_bits=MARKER_FORBIDDEN_BITS,
                )
            for algorithm in sorted(directories):
                _sync_directory(ops, directories[algorithm], failures, target=algorithm)
            _sync_directory(ops, storage.markers, failures, target="markers")
    except BaseException as exc:
        primary = exc
        raise
    finally:
        # Every adopted algorithm directory is released exactly once, even when
        # an earlier close fails or raises a process-control interruption.  An
        # ordinary close failure is attached to an existing primary or
        # aggregated into the domain cleanup report; a non-``Exception`` primary
        # stays authoritative over later close interruptions.
        close_failures = CleanupFailures(primary)
        for capability in directories.values():
            close_failures.run(capability.close, ordinary=(OSError,))
        close_failures.complete()
        # The terminal value is deliberately not raised.  With no primary the
        # first ordinary failure is returned; the full ``ordinary_failures``
        # list is aggregated into the domain report below, so raising only the
        # returned first failure would drop the remaining steps.
        if primary is None:
            for close_exc in close_failures.ordinary_failures:
                failures.append(CleanupFailure("close", close_exc))
    if failures:
        raise BuildCleanupError(
            f"superseded cleanup failed for {len(failures)} step(s)",
            failures=tuple(failures),
        )
    contracts = RegularFileContracts(ops)
    try:
        contracts.durable_unlink(
            storage.generations, previous.name, allowed_mode=GENERATION_MODE
        )
    except TransactionError as exc:
        raise BuildCleanupError(
            "cannot durably remove the previous build generation",
            failures=(CleanupFailure("predecessor", exc),),
        ) from exc


# ═══════════════════════════════════════════════════════════════════════
# Restart recovery
# ═══════════════════════════════════════════════════════════════════════


def recover_generations(
    ops: PosixFileOps,
    storage: BuildStorage,
    *,
    lock: LockCapability,
) -> GenerationInventory:
    """Recover committed build state under *lock* before any build side effect.

    Discovery synchronizes the generation directory and grants authority.
    Authoritative-generation markers are then reconciled, and a retained
    predecessor is cleaned up idempotently.  Reconciliation failure blocks
    superseded cleanup, preserving the predecessor.  Returns the inventory
    that was authoritative when recovery began.
    """
    inventory = discover_generations(ops, storage.generations, lock=lock)
    current = inventory.current
    if current is not None:
        reconcile_authoritative_markers(ops, storage, current, lock=lock)
    if inventory.previous is not None:
        cleanup_superseded(ops, storage, inventory, lock=lock)
    return inventory
