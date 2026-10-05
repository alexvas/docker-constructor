"""Content-addressed cache contracts for selected runtime artifacts.

The host resolver already knows the exact reviewed URL and SRI integrity
for every selected runtime artifact before ``docker run``.  This module
provides the typed DTOs, protocol boundaries, and deterministic cache-key
derivation so the launcher can materialize, verify, and atomically publish
each selected blob on the host — without importing facade, parser,
presentation, Docker execution, or container-installer modules.
"""
from __future__ import annotations

import base64
import dataclasses
import os
import stat as stat_module
import urllib.request
from typing import Iterator, Protocol

from docker.versioning.digest_identity import DigestIdentity, DigestIdentityError
from docker.transactions.capabilities import CapabilityError, DirectoryCapability
from docker.transactions.cleanup import CleanupFailures
from docker.transactions.errors import (
    STAGE_LOCK_ACQUIRE,
    STAGE_LOCK_MODE,
    STAGE_LOCK_STAT,
    STAGE_LOCK_VALIDATE,
    LockError,
    attach_secondary,
)
from docker.transactions.locking import LockCapability, LockPolicy
from docker.transactions.posix import PosixFileOps


# ═══════════════════════════════════════════════════════════════════════
# Error hierarchy
# ═══════════════════════════════════════════════════════════════════════


@dataclasses.dataclass(frozen=True)
class ArtifactMaterializationError(Exception):
    """Structured failure during artifact materialization.

    Every failure mode carries a machine-readable *reason* so callers
    can distinguish transient transport errors from integrity or
    publication failures without parsing message strings.
    """

    reason: str
    """Machine-readable reason code (e.g. ``"cache_miss"``,
    ``"transport"``, ``"integrity"``, ``"publication"``,
    ``"corruption"``, ``"cancellation"``, ``"interruption"``)."""

    detail: str
    """Human-readable detail suitable for diagnostics."""

    def __str__(self) -> str:
        return self.detail


# ── typed materialization DTOs ────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class SelectedArtifact:
    """Minimal host-side materialization input for one selected artifact.

    The *url* and *integrity* come from the reviewed
    ``docker-constructor.toml`` artifact catalog entry.  No package
    name, version, source metadata, update policy, or override config
    is carried — those belong in the effective runtime projection, not
    the cache layer.
    """

    url: str
    """Exact reviewed download URL."""

    integrity: str
    """SRI integrity string (e.g.
    ``"sha512-VO9pV15P..."``)."""


@dataclasses.dataclass(frozen=True)
class VerifiedCacheBlob:
    """Immutable result after successful materialization and verification.

    *host_path* is the absolute path to the verified, atomically
    published regular file in the content-addressed cache.  The path
    is derivable solely from the validated algorithm and digest.
    """

    algorithm: str
    """Lower-case hash algorithm (e.g. ``"sha512"``)."""

    digest: str
    """Filesystem-safe digest string suitable as a path component."""

    integrity: str
    """Full SRI integrity string (``<algorithm>-<digest-base64>``)."""

    host_path: str
    """Absolute path to the verified blob in the cache."""


# ═══════════════════════════════════════════════════════════════════════
# Filesystem status DTO
# ═══════════════════════════════════════════════════════════════════════


@dataclasses.dataclass(frozen=True)
class CacheBlobStat:
    """Result of a filesystem inspection through the boundary.

    All fields mirror what the real filesystem reports without
    requiring the implementation to call ``os`` directly.
    """

    exists: bool
    """True when ``lstat`` succeeds (symlinks included)."""

    is_symlink: bool
    """True when the entry is a symbolic link."""

    is_regular_file: bool
    """True when the entry is a regular file (follows symlinks)."""

    mode_bits: int
    """Full ``st_mode`` integer (e.g. ``0o100400``)."""

    size: int
    """File size in bytes.  Undefined for non-regular entries."""


@dataclasses.dataclass(frozen=True)
class CacheBlobInspection:
    """Metadata and digest obtained from one opened cache descriptor."""

    stat: CacheBlobStat
    digest: str | None


# ── deterministic cache-key derivation ─────────────────────────────────

# The cache root is a module-level constant owned by the constructor,
# not exposed on RunRequest.  Every non-dry-run launch MUST materialize
# selected artifacts under this root before publishing the projection
# or invoking Docker.


def derive_cache_path(algorithm: str, digest: str, *, root: str) -> str:
    """Legacy runtime path adapter for an encoded digest component."""
    return DigestIdentity.runtime_cache_path_from_component(algorithm, digest, root)


def derive_cache_path_from_integrity(integrity: str, *, root: str) -> tuple[str, str]:
    """Derive the legacy URL-safe-base64 ``.tgz`` runtime path."""
    identity = DigestIdentity.from_sri(integrity)
    return derive_cache_path(identity.algorithm, identity.runtime_safe_digest(), root=root), identity.algorithm


# ── protocol boundaries ────────────────────────────────────────────────


class CacheFilesystem(Protocol):
    """Filesystem boundary beneath the cache root.

    Every operation that touches the disk goes through this protocol
    so the materialization pipeline never imports ``os``, ``shutil``,
    or ``stat`` directly for cache-side effects.
    """

    # ── existence / type inspection ──────────────────────────────

    def blob_exists(self, path: str) -> bool: ...

    def is_regular_file(self, path: str) -> bool: ...

    def is_symlink(self, path: str) -> bool: ...

    def stat_blob(self, path: str) -> CacheBlobStat: ...

    # ── reading ──────────────────────────────────────────────────

    def read_bytes(self, path: str) -> bytes: ...

    def digest_file(self, path: str, algorithm: str) -> str: ...

    def inspect_and_digest(
        self, path: str, algorithm: str,
    ) -> CacheBlobInspection: ...

    # ── secure directory creation ────────────────────────────────

    def ensure_secure_dir(self, path: str) -> None: ...

    # ── temporary state ────────────────────────────────────────

    def create_temp(self, path: str) -> None: ...

    def append_temp(self, path: str, chunk: bytes) -> None: ...

    def finalize_temp(self, path: str, mode: int) -> None: ...

    def cleanup_temp(self, root: str) -> None: ...

    # ── corrupt-entry removal ────────────────────────────────────

    def quarantine_or_remove(self, path: str) -> None: ...

    # ── permission inspection and changes ────────────────────────

    def get_permissions(self, path: str) -> int: ...

    def set_permissions(self, path: str, mode: int) -> None: ...

    # ── atomic publication ───────────────────────────────────────

    def atomic_publish(self, temp_path: str, final_path: str) -> None: ...


class StreamingTransport(Protocol):
    """Streaming download boundary.

    Yields chunks of raw bytes for the given URL.  Implementations
    yield one or more ``bytes`` objects.  The caller must hash chunks
    as they arrive and write them incrementally so the full artifact
    is never held in memory.
    """

    def fetch_chunks(self, url: str) -> Iterator[bytes]: ...


class IdentityLock(Protocol):
    """Per-integrity-identity coordination primitive.

    A lock is acquired for the duration of a cache-miss publication.
    ``acquire()`` returns ``True`` when the lock was successfully
    obtained.  The caller MUST track whether acquisition succeeded
    and only call ``release()`` when it did.

    Concurrent contenders MUST recheck the cache after acquiring the
    lock.  On success the lock is released; on failure, cancellation,
    or interruption temporary state is cleaned and the lock released.

    A **Factory** is a callable ``(identity: str) -> IdentityLock``
    that creates a new lock instance for the given integrity identity.
    """

    def acquire(self, identity: str) -> bool: ...

    def release(self, identity: str) -> None: ...


    class Factory(Protocol):
        """Callable that returns an :class:`IdentityLock` for a
        given integrity identity string."""

        def __call__(self, identity: str) -> IdentityLock: ...


class TemporaryDirectory(Protocol):
    """Injectable temporary-directory factory.

    All temporary download state lives on the same filesystem as the
    cache so that atomic rename is always same-device.
    """

    def mkdtemp(self, prefix: str, parent: str) -> str: ...
    """Create a temporary directory beneath *parent* and return its
    absolute path.

    Parameters
    ----------
    prefix:
        Name prefix for the temporary directory.
    parent:
        Parent directory under which to create the directory.
        For cache-materialization this should be the algorithm
        subdirectory of the cache root.
    """


# ── digest computation ─────────────────────────────────────────────────


def _compute_digest(data: bytes, algorithm: str) -> str:
    """Return the standard-base64 digest of *data* for *algorithm*.

    The result uses the standard base64 alphabet (RFC 4648 § 4),
    not the URL-safe variant."""
    import hashlib

    h = hashlib.new(algorithm, data)
    return base64.b64encode(h.digest()).decode("ascii")


def inspect_verified_blob_readonly(
    integrity: str,
    *,
    cache_root: str,
) -> bool:
    """Read-only, no-follow, descriptor-relative cache inspection.

    Verifies that a content-addressed blob for *integrity* exists
    under *cache_root*, is a regular file reached without following
    any symlink component (root, algorithm directory, or leaf), has
    has no group/world write bits, and its bytes match the declared SRI
    digest.

    **No mutation** — the function never creates, removes, renames,
    ``chmod``, locks, quarantines, or repairs anything on disk.
    Every file descriptor is closed before returning.

    Returns ``True`` for a valid cache hit.  Returns ``False`` for
    missing, corrupt, symlinked, non-regular, overly-permissive, or
    otherwise unsafe entries.  No exception escapes.
    """
    import hashlib
    import stat as _stat

    try:
        identity = DigestIdentity.from_sri(integrity)
    except (DigestIdentityError, ValueError):
        return False
    algo = identity.algorithm
    raw_expected = identity.sri().split("-", 1)[1]
    blob_name = f"{identity.runtime_safe_digest()}.tgz"

    # ── open cache root (O_NOFOLLOW: reject symlinked root) ──
    root_rdonly = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    try:
        root_fd = os.open(cache_root, root_rdonly)
    except OSError:
        return False

    primary_root: BaseException | None = None
    try:
        # ── open algorithm subdirectory ──
        try:
            algo_fd = os.open(algo, root_rdonly, dir_fd=root_fd)
        except OSError:
            return False
        primary_algo: BaseException | None = None
        try:
            # Verify algorithm component is really a directory.
            try:
                algo_st = os.fstat(algo_fd)
            except OSError:
                return False
            if not _stat.S_ISDIR(algo_st.st_mode):
                return False

            # ── open blob leaf (O_NOFOLLOW + O_NOATIME) ──
            # O_NOATIME is required to keep dry-run genuinely
            # non-mutating.  When the flag is unavailable at the
            # OS level or denied (PermissionError — caller does
            # not own the file), treat the blob as unreachable.
            blob_flags: int = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            _noatime: int = getattr(os, "O_NOATIME", 0)
            if not _noatime:
                return False
            try:
                blob_fd = os.open(
                    blob_name, blob_flags | _noatime, dir_fd=algo_fd,
                )
            except PermissionError:
                return False
            except OSError:
                return False
            primary_blob: BaseException | None = None
            try:
                try:
                    blob_st = os.fstat(blob_fd)
                except OSError:
                    return False

                # ── regular file only ──
                if not _stat.S_ISREG(blob_st.st_mode):
                    return False

                # ── no group/world write ──
                if blob_st.st_mode & 0o022:
                    return False

                # ── hash and compare ──
                hasher = hashlib.new(algo)
                while True:
                    chunk = os.read(blob_fd, 64 * 1024)
                    if not chunk:
                        break
                    hasher.update(chunk)
                actual_raw = base64.b64encode(
                    hasher.digest(),
                ).decode("ascii")
                return actual_raw == raw_expected
            except BaseException as exc:
                primary_blob = exc
                raise
            finally:
                _close_acc = CleanupFailures(primary_blob)
                _close_acc.run(lambda: os.close(blob_fd), ordinary=(OSError,))
                _close_result = _close_acc.complete()
                if _close_result is not None:
                    raise _close_result
        except BaseException as exc:
            primary_algo = exc
            raise
        finally:
            _close_acc = CleanupFailures(primary_algo)
            _close_acc.run(lambda: os.close(algo_fd), ordinary=(OSError,))
            _close_result = _close_acc.complete()
            if _close_result is not None:
                raise _close_result
    except BaseException as exc:
        primary_root = exc
        raise
    finally:
        _close_acc = CleanupFailures(primary_root)
        _close_acc.run(lambda: os.close(root_fd), ordinary=(OSError,))
        _close_result = _close_acc.complete()
        if _close_result is not None:
            raise _close_result


# ── cache safety validation ───────────────────────────────────────────


def validate_cache_blob(
    host_path: str,
    *,
    cache_root: str,
) -> None:
    """Validate that *host_path* is a regular file, not a symlink,
    contained within *cache_root*, and has no group/world write bits.

    Raises :class:`ArtifactMaterializationError` on any violation.
    """
    root = os.path.realpath(cache_root)
    real_path = os.path.realpath(host_path)

    # ── containment ──────────────────────────────────────────────
    if not (
        real_path == root
        or real_path.startswith(root + os.sep)
    ):
        raise ArtifactMaterializationError(
            reason="containment",
            detail=(
                f"resolved path {real_path!r} is outside "
                f"cache root {root!r}"
            ),
        )

    # ── existence ────────────────────────────────────────────────
    if not os.path.lexists(host_path):
        raise ArtifactMaterializationError(
            reason="missing",
            detail=f"blob path {host_path!r} does not exist",
        )

    # ── no symlink ───────────────────────────────────────────────
    if os.path.islink(host_path):
        raise ArtifactMaterializationError(
            reason="symlink",
            detail=f"blob path {host_path!r} must not be a symlink",
        )

    # ── regular file only ────────────────────────────────────────
    if not os.path.isfile(host_path):
        raise ArtifactMaterializationError(
            reason="not_regular_file",
            detail=f"blob path {host_path!r} is not a regular file",
        )

    # ── no group/world write ───────────────────────────────────
    st = os.stat(host_path)
    if st.st_mode & 0o022:
        raise ArtifactMaterializationError(
            reason="permissions",
            detail=(
                f"blob at {host_path!r} has group/other write bits "
                f"(mode {oct(st.st_mode)})"
            ),
        )


# ── cache-directory permission enforcement ────────────────────────────




# ── materialization pipeline ──────────────────────────────────────────


def materialize_selected_artifacts(
    selected: list[SelectedArtifact],
    *,
    transport: StreamingTransport,
    filesystem: CacheFilesystem,
    lock_factory: IdentityLock.Factory,
    temp_dir: TemporaryDirectory,
    cache_root: str,
    temp_root: str | None = None,
) -> dict[str, VerifiedCacheBlob]:
    """Materialize and verify every *selected* artifact in the
    content-addressed host cache.

    Each artifact is validated, downloaded (cache-miss), verified,
    and atomically published under per-identity coordination.
    Cache hits skip network access.  Every blob is revalidated
    after publication before returning.

    Returns a mapping from SRI integrity string to
    :class:`VerifiedCacheBlob`.

    Raises :class:`ArtifactMaterializationError` on transport
    failure, integrity mismatch, publication error, or
    cancellation.
    """
    root = cache_root
    if temp_root is None:
        temp_root = os.path.join(os.path.dirname(root), "tmp")

    # ── 0. validate and deduplicate ──────────────────────────────
    _validate_selected(selected)

    unique: dict[str, SelectedArtifact] = {}
    for art in selected:
        if art.integrity not in unique:
            unique[art.integrity] = art

    # ── 1. materialize each unique integrity ─────────────────────
    results: dict[str, VerifiedCacheBlob] = {}
    for integrity, artifact in unique.items():
        blob = _materialize_one(
            integrity=integrity,
            artifact=artifact,
            transport=transport,
            filesystem=filesystem,
            lock_factory=lock_factory,
            temp_dir=temp_dir,
            temp_root=temp_root,
            root=root,
        )
        results[integrity] = blob

    return results


def _validate_selected(selected: list[SelectedArtifact]) -> None:
    """Reject any selected artifact with an unsupported algorithm
    or malformed integrity before any IO."""
    for art in selected:
        try:
            DigestIdentity.from_sri(art.integrity)
        except (DigestIdentityError, ValueError) as exc:
            raise ArtifactMaterializationError(
                reason="integrity",
                detail=f"malformed or unsupported integrity: {art.integrity!r}",
            ) from exc


def _try_cache_hit(
    integrity: str,
    algorithm: str,
    path: str,
    filesystem: CacheFilesystem,
    root: str,
) -> VerifiedCacheBlob | None:
    """Return a :class:`VerifiedCacheBlob` when *path* is safe and
    its digest matches *integrity*.  Return ``None`` otherwise."""
    try:
        inspection = filesystem.inspect_and_digest(path, algorithm)
        status = inspection.stat
        if (
            not status.exists
            or status.is_symlink
            or not status.is_regular_file
            or status.mode_bits & 0o022
            or inspection.digest is None
        ):
            return None
        if os.path.commonpath((os.path.abspath(root), os.path.abspath(path))) != os.path.abspath(root):
            return None
    except (ArtifactMaterializationError, FileNotFoundError, OSError):
        return None

    identity = DigestIdentity.from_sri(integrity)
    expected_raw = identity.base64_digest()
    actual_raw = inspection.digest
    if actual_raw != expected_raw:
        return None

    safe_digest = identity.hex_digest()
    return VerifiedCacheBlob(
        algorithm=algorithm,
        digest=identity.runtime_safe_digest(),
        integrity=integrity,
        host_path=path,
    )


def _materialize_one(
    integrity: str,
    artifact: SelectedArtifact,
    *,
    transport: StreamingTransport,
    filesystem: CacheFilesystem,
    lock_factory: IdentityLock.Factory,
    temp_dir: TemporaryDirectory,
    temp_root: str,
    root: str,
) -> VerifiedCacheBlob:
    """Materialize a single integrity identity.

    The pipeline:

    1. Fast path — cache hit with no lock.
    2. Lock acquisition.
    3. Post-lock recheck.
    4. Corrupt-entry quarantining.
    5. Streaming download + hash.
    6. Atomic publication.
    7. Post-publication revalidation.
    8. Unconditional cleanup and lock release (with exception
       chaining — the primary error is always preserved).
    """
    identity = DigestIdentity.from_sri(integrity)
    path = derive_cache_path(identity.algorithm, identity.runtime_safe_digest(), root=root)
    algorithm = identity.algorithm

    # ── 0. harden private subtrees before any access ───────────
    cache_root = os.path.dirname(os.path.dirname(path))
    algorithm_dir = os.path.dirname(path)
    _ensure_dir_private(algorithm_dir)

    # ── 1. fast path: valid cache hit ────────────────────────────
    hit = _try_cache_hit(integrity, algorithm, path, filesystem, root)
    if hit is not None:
        return hit

    # ── 2. coordinated download ──────────────────────────────────
    lock = lock_factory(identity.sri())
    tmp_root: str | None = None
    acquired: bool = False
    primary_exc: BaseException | None = None

    try:
        acquired = lock.acquire(identity.sri())

        # ── 3. post-lock recheck ─────────────────────────────────
        hit = _try_cache_hit(integrity, algorithm, path, filesystem, root)
        if hit is not None:
            return hit

        # ── 4. quarantine any corrupt entry ──────────────────────
        filesystem.quarantine_or_remove(path)

        # ── 5. streaming download + hash ─────────────────────────
        filesystem.ensure_secure_dir(algorithm_dir)
        expected_raw = identity.base64_digest()

        tmp_root = temp_dir.mkdtemp(
            prefix="materialize-", parent=temp_root,
        )
        filesystem.ensure_secure_dir(tmp_root)

        tmp_blob = os.path.join(tmp_root, "blob.tgz")

        # Stream chunks: hash on-the-fly, write incrementally
        # through the filesystem boundary.
        import hashlib

        h = hashlib.new(algorithm)
        filesystem.create_temp(tmp_blob)
        try:
            for chunk in transport.fetch_chunks(artifact.url):
                if not isinstance(chunk, bytes):
                    raise TypeError(
                        f"transport yielded {type(chunk).__name__}, not bytes"
                    )
                h.update(chunk)
                filesystem.append_temp(tmp_blob, chunk)
        except ArtifactMaterializationError:
            raise
        except Exception as exc:
            raise ArtifactMaterializationError(
                reason="transport",
                detail=f"download of {artifact.url!r} failed: {exc}",
            ) from exc
        filesystem.finalize_temp(tmp_blob, 0o444)

        actual_raw = base64.b64encode(h.digest()).decode("ascii")
        if actual_raw != expected_raw:
            raise ArtifactMaterializationError(
                reason="integrity",
                detail=(
                    f"digest mismatch for {artifact.url!r}: "
                    f"expected {integrity}, got "
                    f"{algorithm}-{actual_raw}"
                ),
            )

        # ── 6. atomic publication ────────────────────────────────
        filesystem.ensure_secure_dir(os.path.dirname(path))
        filesystem.atomic_publish(tmp_blob, path)
        tmp_blob = None  # owned by the cache now; don't delete
        filesystem.set_permissions(path, 0o444)

        # ── 7. post-publication revalidation ─────────────────────
        try:
            _validate_published_blob(
                path, integrity, algorithm, root, filesystem,
            )
        except BaseException as validation_exc:
            failures = CleanupFailures(validation_exc)
            failures.run(
                lambda: filesystem.quarantine_or_remove(path),
                ordinary=(Exception,),
            )
            # ``complete`` preserves an ``Exception`` validation failure and
            # attaches any cleanup failure; a process-control interruption is
            # authoritative either way, and no cleanup action is skipped.
            result = failures.complete()
            if result is not None:
                raise result
            raise

        safe_digest = identity.hex_digest()
        return VerifiedCacheBlob(
            algorithm=algorithm,
            digest=identity.runtime_safe_digest(),
            integrity=integrity,
            host_path=path,
        )

    except BaseException as exc:
        primary_exc = exc
        raise
    finally:
        # ── 8. unconditional cleanup ─────────────────────────────
        # Both temp cleanup and lock release are attempted exactly once even
        # if one fails.  Ordinary failures are attached to an existing
        # primary; a process-control interruption stays authoritative.
        failures = CleanupFailures(primary_exc)

        def cleanup_temporary_root() -> None:
            if tmp_root is not None:
                filesystem.cleanup_temp(tmp_root)

        def release_identity_lock() -> None:
            if acquired:
                lock.release(integrity)

        failures.run(cleanup_temporary_root, ordinary=(Exception,))
        failures.run(release_identity_lock, ordinary=(Exception,))
        result = failures.complete()
        if result is not None:
            # No primary: the first cleanup/release failure is mapped to the
            # runtime publication diagnostic, preserving the raw cause.
            raise ArtifactMaterializationError(
                reason="publication",
                detail=f"cleanup or release failed: {result}",
            ) from result


def _validate_published_blob(
    path: str,
    integrity: str,
    algorithm: str,
    root: str,
    filesystem: CacheFilesystem,
) -> None:
    """Revalidate a published blob through the filesystem boundary.

    Rechecks containment, no-symlink, regular-file, permissions,
    and digest.  Raises :class:`ArtifactMaterializationError` on any
    failure.
    """
    # Metadata and digest come from the same no-follow file descriptor.
    inspection = filesystem.inspect_and_digest(path, algorithm)
    stat = inspection.stat

    if not stat.exists:
        raise ArtifactMaterializationError(
            reason="publication",
            detail=f"published blob {path!r} does not exist",
        )
    if stat.is_symlink:
        raise ArtifactMaterializationError(
            reason="publication",
            detail=f"published blob {path!r} is a symlink",
        )
    if not stat.is_regular_file:
        raise ArtifactMaterializationError(
            reason="publication",
            detail=f"published blob {path!r} is not a regular file",
        )

    mode = stat.mode_bits
    if mode & 0o022:
        raise ArtifactMaterializationError(
            reason="publication",
            detail=(
                f"published blob {path!r} has group/other write bits "
                f"(mode {oct(mode)})"
            ),
        )

    # Re-check lexical containment; component safety was established by the
    # descriptor-relative stat and permission operations above.
    if os.path.commonpath((os.path.abspath(root), os.path.abspath(path))) != os.path.abspath(root):
        raise ArtifactMaterializationError(
            reason="containment", detail=f"published blob {path!r} escaped cache root",
        )

    # ── digest re-check ──────────────────────────────────────────
    identity = DigestIdentity.from_sri(integrity)
    expected_raw = identity.base64_digest()
    actual_raw = inspection.digest
    if actual_raw is None:
        raise ArtifactMaterializationError(
            reason="publication", detail=f"published blob {path!r} was not readable",
        )
    if actual_raw != expected_raw:
        raise ArtifactMaterializationError(
            reason="integrity",
            detail=(
                f"post-publication digest mismatch for {path!r}: "
                f"expected {integrity}, got {algorithm}-{actual_raw}"
            ),
        )



class HttpStreamingTransport:
    """Production HTTP transport that never buffers the response body."""

    def __init__(self, *, chunk_size: int = 64 * 1024, timeout: float = 30.0):
        self._chunk_size = chunk_size
        self._timeout = timeout

    def fetch_chunks(self, url: str) -> Iterator[bytes]:
        request = urllib.request.Request(url, headers={"User-Agent": "pi-cli/1"})
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            while True:
                chunk = response.read(self._chunk_size)
                if not chunk:
                    return
                yield chunk


def _ensure_dir_private(path: str) -> None:
    """Fix permissions on *path* to ``0o700`` using descriptor-relative,
    no-follow operations.

    *path* must be a known-private cache subtree — ``blobs/``,
    ``locks/``, an algorithm directory, or a temporary directory.
    Shared parent containers (``.docker-generated/``,
    ``runtime-artifacts/``) are intentionally **not** passed here.

    Opens the parent and target without following symlinks, validates
    the target is a directory via ``fstat``, and corrects the mode with
    ``fchmod`` — all on the same descriptor.

    Silently returns when the directory or its lead-up does not exist.
    Raises :class:`ArtifactMaterializationError` with reason
    ``"permissions"`` when the process cannot access a parent
    component, open the target, or ``fchmod`` an existing entry.
    Raises with reason ``"containment"`` for symlinks and
    non-directory entries.
    """
    import stat as _stat
    try:
        parent_fd, name = _open_parent(path)
    except FileNotFoundError:
        return
    except (PermissionError, OSError) as exc:
        raise ArtifactMaterializationError(
            "permissions",
            f"cannot access parent of cache root {path!r}: {exc}",
        ) from exc
    primary_parent: BaseException | None = None
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        # FileNotFoundError is the only benign outcome — the entry
        # simply does not exist yet.  All other failures must be
        # surfaced as structured errors.
        try:
            fd = os.open(name, flags, dir_fd=parent_fd)
        except FileNotFoundError:
            return
        except PermissionError as exc:
            raise ArtifactMaterializationError(
                "permissions",
                f"cannot open cache root entry at {path!r}: {exc}",
            ) from exc
        except (NotADirectoryError, OSError) as exc:
            raise ArtifactMaterializationError(
                "containment",
                f"unsafe cache root entry at {path!r}: {exc}",
            ) from exc
        primary_fd: BaseException | None = None
        try:
            current_mode = os.fstat(fd).st_mode
            if not _stat.S_ISDIR(current_mode):
                raise ArtifactMaterializationError(
                    "containment",
                    f"expected directory at {path!r}, found non-directory entry",
                )
            if current_mode & 0o077:
                try:
                    os.fchmod(fd, _stat.S_IRWXU)
                except PermissionError as exc:
                    raise ArtifactMaterializationError(
                        "permissions",
                        f"cannot secure cache root {path!r}: {exc}",
                    ) from exc
        except BaseException as exc:
            primary_fd = exc
            raise
        finally:
            _close_acc = CleanupFailures(primary_fd)
            _close_acc.run(lambda: os.close(fd), ordinary=(OSError,))
            _close_result = _close_acc.complete()
            if _close_result is not None:
                raise _close_result
    except BaseException as exc:
        primary_parent = exc
        raise
    finally:
        _close_acc = CleanupFailures(primary_parent)
        _close_acc.run(lambda: os.close(parent_fd), ordinary=(OSError,))
        _close_result = _close_acc.complete()
        if _close_result is not None:
            raise _close_result


def _open_directory_chain(path: str, *, create: bool) -> int:
    """Open an absolute directory without following any symlink component.

    Missing components are created descriptor-relatively when *create* is
    true.  Newly created directories use ``0o777`` (subject to umask)
    so that parent containers like ``.docker-generated/`` and
    ``runtime-artifacts/`` retain the calling process's default
    permission bits.  Private subtrees (``blobs/``, ``locks/``,
    algorithm dirs, temp dirs) are hardened separately by their
    callers.

    The returned descriptor owns the final directory and must be
    closed by the caller.
    """
    absolute = os.path.abspath(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    current_fd = os.open(os.sep, flags)
    parent_fd = -1
    try:
        for component in (part for part in absolute.split(os.sep) if part):
            try:
                value = os.stat(component, dir_fd=current_fd, follow_symlinks=False)
            except FileNotFoundError:
                if not create:
                    raise
                # Let umask dictate the default mode for parent
                # directories; private subtrees are hardened later.
                os.mkdir(component, 0o777, dir_fd=current_fd)
                value = os.stat(component, dir_fd=current_fd, follow_symlinks=False)
            if stat_module.S_ISLNK(value.st_mode) or not stat_module.S_ISDIR(value.st_mode):
                raise ArtifactMaterializationError(
                    "containment",
                    f"unsafe cache path component {component!r} in {absolute!r}",
                )
            next_fd = os.open(component, flags, dir_fd=current_fd)
            # Transfer ownership of the child before releasing the parent, so a
            # failed parent close cannot leak the child or be retried.
            parent_fd, current_fd = current_fd, next_fd
            releasing_fd, parent_fd = parent_fd, -1
            os.close(releasing_fd)
        return current_fd
    except BaseException as exc:
        failures = CleanupFailures(exc)
        child, current_fd = current_fd, -1
        if child >= 0:
            failures.run(lambda: os.close(child), ordinary=(OSError,))
        parent, parent_fd = parent_fd, -1
        if parent >= 0:
            failures.run(lambda: os.close(parent), ordinary=(OSError,))
        result = failures.complete()
        if result is not None:
            raise result
        raise


def _open_parent(path: str, *, create: bool = False) -> tuple[int, str]:
    absolute = os.path.abspath(path)
    name = os.path.basename(absolute)
    if not name or name in {".", ".."}:
        raise ArtifactMaterializationError("containment", f"unsafe cache path {path!r}")
    return _open_directory_chain(os.path.dirname(absolute), create=create), name


def _remove_tree_at(parent_fd: int, name: str) -> None:
    """Remove one directory tree without resolving path components."""
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(name, flags, dir_fd=parent_fd)
    primary: BaseException | None = None
    try:
        for child in os.listdir(directory_fd):
            value = os.stat(child, dir_fd=directory_fd, follow_symlinks=False)
            if stat_module.S_ISDIR(value.st_mode) and not stat_module.S_ISLNK(value.st_mode):
                _remove_tree_at(directory_fd, child)
            else:
                os.unlink(child, dir_fd=directory_fd)
    except BaseException as exc:
        primary = exc
        raise
    finally:
        _close_acc = CleanupFailures(primary)
        _close_acc.run(lambda: os.close(directory_fd), ordinary=(OSError,))
        _close_result = _close_acc.complete()
        if _close_result is not None:
            raise _close_result
    os.rmdir(name, dir_fd=parent_fd)


class LocalCacheFilesystem:
    """Production cache filesystem using descriptor-relative operations."""

    def blob_exists(self, path: str) -> bool:
        return self.stat_blob(path).exists

    def is_regular_file(self, path: str) -> bool:
        return self.stat_blob(path).is_regular_file

    def is_symlink(self, path: str) -> bool:
        return self.stat_blob(path).is_symlink

    def stat_blob(self, path: str) -> CacheBlobStat:
        try:
            parent_fd, name = _open_parent(path)
        except FileNotFoundError:
            return CacheBlobStat(False, False, False, 0, 0)
        primary: BaseException | None = None
        try:
            try:
                value = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return CacheBlobStat(False, False, False, 0, 0)
        except BaseException as exc:
            primary = exc
            raise
        finally:
            _close_acc = CleanupFailures(primary)
            _close_acc.run(lambda: os.close(parent_fd), ordinary=(OSError,))
            _close_result = _close_acc.complete()
            if _close_result is not None:
                raise _close_result
        return CacheBlobStat(
            True,
            stat_module.S_ISLNK(value.st_mode),
            stat_module.S_ISREG(value.st_mode),
            value.st_mode,
            value.st_size,
        )

    def read_bytes(self, path: str) -> bytes:
        parent_fd, name = _open_parent(path)
        primary_parent: BaseException | None = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(name, flags, dir_fd=parent_fd)
            primary_fd: BaseException | None = None
            try:
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    return stream.read()
            except BaseException as exc:
                primary_fd = exc
                raise
            finally:
                _close_acc = CleanupFailures(primary_fd)
                _close_acc.run(lambda: os.close(fd), ordinary=(OSError,))
                _close_result = _close_acc.complete()
                if _close_result is not None:
                    raise _close_result
        except BaseException as exc:
            primary_parent = exc
            raise
        finally:
            _close_acc = CleanupFailures(primary_parent)
            _close_acc.run(lambda: os.close(parent_fd), ordinary=(OSError,))
            _close_result = _close_acc.complete()
            if _close_result is not None:
                raise _close_result

    def digest_file(self, path: str, algorithm: str) -> str:
        inspection = self.inspect_and_digest(path, algorithm)
        if inspection.digest is None:
            raise ArtifactMaterializationError("containment", "cache blob is not regular")
        return inspection.digest

    def inspect_and_digest(
        self, path: str, algorithm: str,
    ) -> CacheBlobInspection:
        import hashlib
        parent_fd, name = _open_parent(path)
        primary_parent: BaseException | None = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            try:
                fd = os.open(name, flags, dir_fd=parent_fd)
            except FileNotFoundError:
                return CacheBlobInspection(
                    CacheBlobStat(False, False, False, 0, 0), None,
                )
            except OSError:
                value = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                return CacheBlobInspection(
                    CacheBlobStat(
                        True,
                        stat_module.S_ISLNK(value.st_mode),
                        stat_module.S_ISREG(value.st_mode),
                        value.st_mode,
                        value.st_size,
                    ),
                    None,
                )
            primary_fd: BaseException | None = None
            try:
                value = os.fstat(fd)
                status = CacheBlobStat(
                    True,
                    False,
                    stat_module.S_ISREG(value.st_mode),
                    value.st_mode,
                    value.st_size,
                )
                if not status.is_regular_file:
                    return CacheBlobInspection(status, None)
                digest = hashlib.new(algorithm)
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    for chunk in iter(lambda: stream.read(64 * 1024), b""):
                        digest.update(chunk)
                return CacheBlobInspection(
                    status,
                    base64.b64encode(digest.digest()).decode("ascii"),
                )
            except BaseException as exc:
                primary_fd = exc
                raise
            finally:
                _close_acc = CleanupFailures(primary_fd)
                _close_acc.run(lambda: os.close(fd), ordinary=(OSError,))
                _close_result = _close_acc.complete()
                if _close_result is not None:
                    raise _close_result
        except BaseException as exc:
            primary_parent = exc
            raise
        finally:
            _close_acc = CleanupFailures(primary_parent)
            _close_acc.run(lambda: os.close(parent_fd), ordinary=(OSError,))
            _close_result = _close_acc.complete()
            if _close_result is not None:
                raise _close_result

    def ensure_secure_dir(self, path: str) -> None:
        fd = _open_directory_chain(path, create=True)
        primary: BaseException | None = None
        try:
            os.fchmod(fd, 0o700)
        except BaseException as exc:
            primary = exc
            raise
        finally:
            _close_acc = CleanupFailures(primary)
            _close_acc.run(lambda: os.close(fd), ordinary=(OSError,))
            _close_result = _close_acc.complete()
            if _close_result is not None:
                raise _close_result

    def create_temp(self, path: str) -> None:
        parent_fd, name = _open_parent(path)
        primary: BaseException | None = None
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(name, flags, 0o600, dir_fd=parent_fd)
            os.close(fd)
        except BaseException as exc:
            primary = exc
            raise
        finally:
            _close_acc = CleanupFailures(primary)
            _close_acc.run(lambda: os.close(parent_fd), ordinary=(OSError,))
            _close_result = _close_acc.complete()
            if _close_result is not None:
                raise _close_result

    def append_temp(self, path: str, chunk: bytes) -> None:
        parent_fd, name = _open_parent(path)
        primary_parent: BaseException | None = None
        try:
            flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(name, flags, dir_fd=parent_fd)
            primary_fd: BaseException | None = None
            try:
                if not stat_module.S_ISREG(os.fstat(fd).st_mode):
                    raise ArtifactMaterializationError("containment", "temporary blob is not regular")
                view = memoryview(chunk)
                while view:
                    view = view[os.write(fd, view):]
            except BaseException as exc:
                primary_fd = exc
                raise
            finally:
                _close_acc = CleanupFailures(primary_fd)
                _close_acc.run(lambda: os.close(fd), ordinary=(OSError,))
                _close_result = _close_acc.complete()
                if _close_result is not None:
                    raise _close_result
        except BaseException as exc:
            primary_parent = exc
            raise
        finally:
            _close_acc = CleanupFailures(primary_parent)
            _close_acc.run(lambda: os.close(parent_fd), ordinary=(OSError,))
            _close_result = _close_acc.complete()
            if _close_result is not None:
                raise _close_result

    def finalize_temp(self, path: str, mode: int) -> None:
        parent_fd, name = _open_parent(path)
        primary_parent: BaseException | None = None
        try:
            fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
            primary_fd: BaseException | None = None
            try:
                os.fchmod(fd, mode)
            except BaseException as exc:
                primary_fd = exc
                raise
            finally:
                _close_acc = CleanupFailures(primary_fd)
                _close_acc.run(lambda: os.close(fd), ordinary=(OSError,))
                _close_result = _close_acc.complete()
                if _close_result is not None:
                    raise _close_result
        except BaseException as exc:
            primary_parent = exc
            raise
        finally:
            _close_acc = CleanupFailures(primary_parent)
            _close_acc.run(lambda: os.close(parent_fd), ordinary=(OSError,))
            _close_result = _close_acc.complete()
            if _close_result is not None:
                raise _close_result

    def cleanup_temp(self, root: str) -> None:
        parent_fd, name = _open_parent(root)
        primary: BaseException | None = None
        try:
            value = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if not stat_module.S_ISDIR(value.st_mode) or stat_module.S_ISLNK(value.st_mode):
                raise ArtifactMaterializationError("containment", "temporary root is unsafe")
            _remove_tree_at(parent_fd, name)
        except BaseException as exc:
            primary = exc
            raise
        finally:
            _close_acc = CleanupFailures(primary)
            _close_acc.run(lambda: os.close(parent_fd), ordinary=(OSError,))
            _close_result = _close_acc.complete()
            if _close_result is not None:
                raise _close_result

    def quarantine_or_remove(self, path: str) -> None:
        try:
            parent_fd, name = _open_parent(path)
        except FileNotFoundError:
            return
        primary: BaseException | None = None
        try:
            try:
                value = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return
            if stat_module.S_ISDIR(value.st_mode) and not stat_module.S_ISLNK(value.st_mode):
                _remove_tree_at(parent_fd, name)
            else:
                os.unlink(name, dir_fd=parent_fd)
        except BaseException as exc:
            primary = exc
            raise
        finally:
            _close_acc = CleanupFailures(primary)
            _close_acc.run(lambda: os.close(parent_fd), ordinary=(OSError,))
            _close_result = _close_acc.complete()
            if _close_result is not None:
                raise _close_result

    def get_permissions(self, path: str) -> int:
        parent_fd, name = _open_parent(path)
        primary: BaseException | None = None
        try:
            return stat_module.S_IMODE(
                os.stat(name, dir_fd=parent_fd, follow_symlinks=False).st_mode
            )
        except BaseException as exc:
            primary = exc
            raise
        finally:
            _close_acc = CleanupFailures(primary)
            _close_acc.run(lambda: os.close(parent_fd), ordinary=(OSError,))
            _close_result = _close_acc.complete()
            if _close_result is not None:
                raise _close_result

    def set_permissions(self, path: str, mode: int) -> None:
        parent_fd, name = _open_parent(path)
        primary_parent: BaseException | None = None
        try:
            fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
            primary_fd: BaseException | None = None
            try:
                if not stat_module.S_ISREG(os.fstat(fd).st_mode):
                    raise ArtifactMaterializationError("containment", "cache blob is not regular")
                os.fchmod(fd, mode)
            except BaseException as exc:
                primary_fd = exc
                raise
            finally:
                _close_acc = CleanupFailures(primary_fd)
                _close_acc.run(lambda: os.close(fd), ordinary=(OSError,))
                _close_result = _close_acc.complete()
                if _close_result is not None:
                    raise _close_result
        except BaseException as exc:
            primary_parent = exc
            raise
        finally:
            _close_acc = CleanupFailures(primary_parent)
            _close_acc.run(lambda: os.close(parent_fd), ordinary=(OSError,))
            _close_result = _close_acc.complete()
            if _close_result is not None:
                raise _close_result

    def atomic_publish(self, temp_path: str, final_path: str) -> None:
        source_fd, source_name = _open_parent(temp_path)
        target_fd, target_name = _open_parent(final_path)
        primary: BaseException | None = None
        try:
            source = os.stat(source_name, dir_fd=source_fd, follow_symlinks=False)
            target_dir = os.fstat(target_fd)
            if not stat_module.S_ISREG(source.st_mode):
                raise ArtifactMaterializationError("publication", "temporary blob is not regular")
            if source.st_dev != target_dir.st_dev:
                raise ArtifactMaterializationError(
                    "publication", "temporary and final paths are on different filesystems"
                )
            os.replace(
                source_name, target_name,
                src_dir_fd=source_fd, dst_dir_fd=target_fd,
            )
        except BaseException as exc:
            primary = exc
            raise
        finally:
            failures = CleanupFailures(primary)
            failures.run(lambda: os.close(source_fd), ordinary=(OSError,))
            failures.run(lambda: os.close(target_fd), ordinary=(OSError,))
            result = failures.complete()
            if result is not None:
                raise result


class LocalTemporaryDirectory:
    def mkdtemp(self, prefix: str, parent: str) -> str:
        import secrets
        parent_fd = _open_directory_chain(parent, create=False)
        primary: BaseException | None = None
        try:
            for _ in range(100):
                name = prefix + secrets.token_hex(8)
                try:
                    os.mkdir(name, 0o700, dir_fd=parent_fd)
                except FileExistsError:
                    continue
                return os.path.join(os.path.abspath(parent), name)
        except BaseException as exc:
            primary = exc
            raise
        finally:
            _close_acc = CleanupFailures(primary)
            _close_acc.run(lambda: os.close(parent_fd), ordinary=(OSError,))
            _close_result = _close_acc.complete()
            if _close_result is not None:
                raise _close_result
        raise FileExistsError("could not allocate unique temporary directory")


def _identity_lock_name(identity: str) -> str:
    """Return the private lock-entry basename for *identity*.

    The identity is the domain-selected SRI scope.  The basename is unchanged
    from the previous hand-rolled implementation so the on-disk lock namespace
    stays identical.
    """
    safe = base64.urlsafe_b64encode(identity.encode()).decode().rstrip("=")
    return safe + ".lock"


def _identity_lock_failure(exc: BaseException) -> BaseException:
    """Map a shared lock failure to the runtime domain diagnostic.

    A validate-stage failure (symlink, non-regular, foreign-owned, multiply
    linked, owner-inaccessible, or an in-place replacement) becomes the
    containment diagnostic ``"identity lock is not a private regular file"``;
    a prepare-stage failure (open/create) becomes ``"identity lock path is
    unsafe"``; an operational descriptor-stat, mode-repair, or acquisition
    failure re-raises its raw ``OSError``; and a capability failure becomes
    ``"identity lock path is unsafe"``.  Process-control interruptions are
    returned unchanged.

    Only a stat failure of the locked descriptor (``STAGE_LOCK_STAT``) is
    treated as operational.  A validate-stage failure is never blindly
    unwrapped even when it carries an ``OSError`` cause: an identity probe
    that fails while verifying the entry can indicate an unsafe namespace
    change, so it stays a containment error.
    """
    if isinstance(exc, LockError):
        if exc.stage == STAGE_LOCK_VALIDATE:
            return ArtifactMaterializationError(
                "containment", "identity lock is not a private regular file",
            )
        if exc.stage in (
            STAGE_LOCK_STAT, STAGE_LOCK_MODE, STAGE_LOCK_ACQUIRE,
        ) and isinstance(exc.cause, OSError):
            # An operational descriptor-stat, mode-repair, or acquisition
            # failure previously propagated as its raw ``OSError``; preserve
            # that parity while carrying any attached cleanup diagnostics.
            cause = exc.cause
            attach_secondary(cause, list(exc.secondary))
            return cause
        return ArtifactMaterializationError(
            "containment", "identity lock path is unsafe",
        )
    if isinstance(exc, CapabilityError):
        return ArtifactMaterializationError(
            "containment", "identity lock path is unsafe",
        )
    return exc


class FileIdentityLock(IdentityLock):
    """Per-integrity-identity blocking lock over the shared lock capability.

    The lock scope is the SRI identity: one private ``.lock`` entry beneath the
    private ``locks/`` directory.  The shared :class:`LockCapability` owns entry
    validation (symlink, non-regular, foreign-owned, multiply linked), atomic
    exclusive creation, the mandatory :attr:`LockPolicy.BLOCK` policy,
    post-acquisition mode repair, and release.  The runtime domain keeps only
    the identity-derived namespace and its existing containment diagnostics.
    """

    def __init__(self, lock_root: str):
        self._lock_root = lock_root
        self._ops = PosixFileOps()
        self._directory: DirectoryCapability | None = None
        self._capability: LockCapability | None = None

    def acquire(self, identity: str) -> bool:
        lock_dir_fd = _open_directory_chain(self._lock_root, create=True)
        # ``raw_fd`` tracks the caller-owned directory descriptor until
        # ``DirectoryCapability.from_fd`` adopts it.  Adoption failure leaves
        # the descriptor caller-owned, so it must still be released here;
        # after successful adoption the capability owns it exclusively.
        raw_fd: int | None = lock_dir_fd
        directory: DirectoryCapability | None = None
        capability: LockCapability | None = None
        try:
            directory = DirectoryCapability.from_fd(
                self._ops, lock_dir_fd, "runtime-artifact locks",
            )
            # Ownership of the directory descriptor transferred to the
            # capability; the raw descriptor must no longer be closed here.
            raw_fd = None
            self._ops.fchmod(directory.fd, 0o700)
            capability = LockCapability.acquire(
                self._ops,
                directory,
                _identity_lock_name(identity),
                namespace=identity,
                policy=LockPolicy.BLOCK,
            )
        except BaseException as exc:
            # Release the caller-owned descriptor and any adopted directory
            # exactly once each, attempting both even when the first fails.  An
            # ordinary close failure is attached to the primary; a process-
            # control interruption stays authoritative over an ordinary primary
            # and is never converted into a containment error.
            failures = CleanupFailures(exc)
            descriptor, raw_fd = raw_fd, None
            if descriptor is not None:
                failures.run(lambda: self._ops.close(descriptor), ordinary=(OSError,))
            if directory is not None:
                failures.run(directory.close, ordinary=(OSError,))
            result = failures.complete()
            if result is not None:
                raise result
            mapped = _identity_lock_failure(exc)
            if mapped is exc:
                raise
            if isinstance(mapped, ArtifactMaterializationError):
                raise mapped from exc
            raise mapped
        self._directory = directory
        self._capability = capability
        return True

    def release(self, identity: str) -> None:
        capability, self._capability = self._capability, None
        directory, self._directory = self._directory, None
        failures = CleanupFailures(None)

        def release_capability() -> None:
            if capability is None:
                return
            try:
                capability.close()
            except LockError as exc:
                # Preserve the raw descriptor failure across the shared
                # boundary so a release failure stays an ordinary OSError at
                # the runtime edge; carry any cleanup diagnostics with it.
                cause = exc.cause
                if isinstance(cause, OSError):
                    attach_secondary(cause, list(exc.secondary))
                    raise cause
                raise

        def release_directory() -> None:
            if directory is not None:
                directory.close()

        failures.run(release_capability, ordinary=(OSError, LockError))
        failures.run(release_directory, ordinary=(OSError,))
        result = failures.complete()
        if result is not None:
            raise result


class FileIdentityLockFactory:
    def __init__(self, cache_root: str, *, lock_root: str | None = None):
        self._lock_root = lock_root or os.path.join(
            os.path.dirname(cache_root), "locks",
        )

    def __call__(self, identity: str) -> IdentityLock:
        return FileIdentityLock(self._lock_root)


