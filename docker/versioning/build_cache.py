"""External constructor-project build-cache boundary.

Persistent build artifact state and transaction snapshots live beneath the
invoking user's private external namespace, keyed by the canonical selected
constructor-project path.  Constructor state is never created in a constructor project
or workspace.

The module provides:

* lexical, deterministic path resolution (no filesystem reads);
* host-owner traversal validation for the constructor-project path (no repair);
* no-follow preparation of constructor-owned private ``0700`` subtrees that
  rejects symlinked, non-directory, or foreign-owned entries *before* any
  mutation;
* atomic publication of a verified blob at its canonical content-addressed
  path with mode ``0444``, re-verified before it is returned.

The constructor never chmods or chowns the constructor-project root, any ancestor, the
user's home directory, or unrelated cache paths.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat as _stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

if TYPE_CHECKING:
    from docker.versioning.build_cleanup import BuildStorage

from docker.transactions.capabilities import CapabilityError, DirectoryCapability
from docker.transactions.cleanup import CleanupFailures
from docker.transactions.errors import (
    STAGE_LOCK_VALIDATE,
    STAGE_VALIDATE,
    STAGE_VALIDATE_DESTINATION,
    LockContention,
    LockError,
    TransactionError,
    UnsafeFileError,
    carry_secondary_diagnostics,
)
from docker.transactions.locking import LockCapability
from docker.transactions.posix import PosixFileOps
from docker.transactions.regular import RegularFileContracts
from docker.versioning.cache_storage import resolve_default_root
from docker.versioning.digest_identity import DigestIdentity
from docker.versioning.project_state import ProjectState, ProjectStateError, resolve_project_state

BLOB_EXTENSION = ".blob"
"""Content-addressed build-blob filename extension."""

UNCOMMITTED_TTL_SECONDS = 2_592_000
"""Fixed retention period for verified blobs not in the live build set."""


class BuildCacheError(ValueError):
    """Invalid or unsafe external constructor-project build-cache configuration."""


class PostCommitBuildError(BuildCacheError):
    """Generation publication succeeded, but later recovery work failed.

    The original operational failure is retained as ``__cause__``. Process-control
    exceptions are never converted to this build-domain boundary.
    """


@dataclass(frozen=True)
class BuildCachePaths:
    """Resolved project identity and prepared external project-state roots."""

    constructor_project_root: Path
    """Normalized selected constructor-project path; never a cache path."""

    namespace_root: Path
    """Verified external namespace for descriptor-relative cache operations."""

    persistent_root: Path
    """``<namespace>/build-artifacts``."""

    blobs_root: Path
    """``<namespace>/build-artifacts/blobs``."""

    tmp_root: Path
    """``<namespace>/build-artifacts/tmp``."""

    generated_root: Path
    """``<namespace>/transactions``."""

    markers_root: Path
    """Owner-private markers for uncommitted verified blobs."""


_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_STORAGE_DIRECTORY_FLAGS = _DIR_FLAGS


# ═══════════════════════════════════════════════════════════════════════
# Lexical path resolution (no filesystem access)
# ═══════════════════════════════════════════════════════════════════════


def _normalize_constructor_project(constructor_project_root: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(constructor_project_root)))


def _resolve_cache_root(cache_root: str | Path | None) -> Path:
    """Return the cache root exactly as ``resolve_project_state`` derives it."""
    if cache_root is not None:
        return Path(cache_root)
    return resolve_default_root(os.environ.get("XDG_CACHE_HOME"), home=Path.home())


def resolve_build_cache_root(constructor_project_root: str | Path, *, cache_root: str | Path | None = None) -> Path:
    """Return the selected project's external persistent artifact root."""
    try:
        return resolve_project_state(constructor_project_root, cache_root=cache_root, create=False).build_artifacts_root
    except ProjectStateError as exc:
        raise BuildCacheError(str(exc)) from exc


def resolve_build_blobs_root(constructor_project_root: str | Path, *, cache_root: str | Path | None = None) -> Path:
    return resolve_build_cache_root(constructor_project_root, cache_root=cache_root) / "blobs"


def resolve_build_tmp_root(constructor_project_root: str | Path, *, cache_root: str | Path | None = None) -> Path:
    return resolve_build_cache_root(constructor_project_root, cache_root=cache_root) / "tmp"


def resolve_build_generated_root(constructor_project_root: str | Path, *, cache_root: str | Path | None = None) -> Path:
    """Return the selected project's external transaction root."""
    try:
        return resolve_project_state(constructor_project_root, cache_root=cache_root, create=False).transactions_root
    except ProjectStateError as exc:
        raise BuildCacheError(str(exc)) from exc


def build_blob_path(blobs_root: str | Path, identity: DigestIdentity) -> Path:
    """Return the canonical content-addressed blob path for *identity*."""
    return Path(identity.cache_path(os.fspath(blobs_root), extension=BLOB_EXTENSION))


# ═══════════════════════════════════════════════════════════════════════
# Host-owner traversal validation (strict no-repair)
# ═══════════════════════════════════════════════════════════════════════


def validate_host_owner_traversal(path: str | Path) -> None:
    """Require the invoking host user to traverse every ancestor of *path*.

    Walks from the filesystem root to *path* (following symlinked ancestors
    as the OS would) and requires each component to be a directory with
    search permission for the invoking user.  It never chmods or chowns
    anything: an inaccessible ancestor is reported with its path and left
    untouched.
    """
    absolute = os.path.abspath(os.fspath(path))
    current = os.sep
    for part in (component for component in absolute.split(os.sep) if component):
        current = os.path.join(current, part)
        try:
            st = os.stat(current)
        except OSError as exc:
            raise BuildCacheError(
                f"cannot access constructor-project traversal component {current}: {exc}"
            ) from exc
        if not _stat.S_ISDIR(st.st_mode):
            raise BuildCacheError(
                f"constructor-project traversal component {current} is not a directory"
            )
        if not os.access(current, os.X_OK):
            raise BuildCacheError(
                f"cannot traverse {current}; the invoking user lacks search "
                "permission and the constructor will not repair it"
            )


# ═══════════════════════════════════════════════════════════════════════
# Descriptor-relative, no-follow directory helpers
# ═══════════════════════════════════════════════════════════════════════


def _open_namespace_fd(namespace: Path) -> int:
    """Open the external project-state namespace without following symlinks."""
    try:
        fd = os.open(os.fspath(namespace), _DIR_FLAGS)
    except FileNotFoundError as exc:
        raise BuildCacheError(f"project-state namespace {namespace} does not exist") from exc
    except PermissionError as exc:
        raise BuildCacheError(f"cannot access project-state namespace {namespace}: {exc}") from exc
    except OSError as exc:
        raise BuildCacheError(
            f"project-state namespace {namespace} is a symlink or not a directory: {exc}"
        ) from exc
    try:
        st = os.fstat(fd)
        if not _stat.S_ISDIR(st.st_mode):
            raise BuildCacheError(f"project-state namespace {namespace} is not a directory")
        if not os.access(namespace, os.W_OK | os.X_OK):
            raise BuildCacheError(
                f"project-state namespace {namespace} is not writable by the invoking user"
            )
        return fd
    except BaseException as exc:
        failures = CleanupFailures(exc)
        failures.run(lambda: os.close(fd), ordinary=(OSError,))
        result = failures.complete()
        if result is not None:
            raise result
        raise


def _open_relative_dir(
    namespace_fd: int,
    parts: tuple[str, ...],
    *,
    create: bool,
) -> int | None:
    """Open ``parts`` relative to *namespace_fd* without following symlinks.

    Returns the opened descriptor of the final component, or ``None`` when a
    component is missing and *create* is false.  Missing components are
    created descriptor-relatively with ``0700`` when *create* is true.
    Symlinked or non-directory components raise :class:`BuildCacheError`.
    """
    current_fd = os.dup(namespace_fd)
    parent_fd = -1
    try:
        for component in parts:
            created = False
            try:
                value = os.stat(
                    component, dir_fd=current_fd, follow_symlinks=False,
                )
            except FileNotFoundError:
                if not create:
                    # Relinquish ownership of the retained descriptor before
                    # closing it so a failed close is never retried by the
                    # handler and cannot leak the descriptor.
                    releasing_fd, current_fd = current_fd, -1
                    os.close(releasing_fd)
                    return None
                try:
                    os.mkdir(component, 0o700, dir_fd=current_fd)
                except OSError as exc:
                    raise BuildCacheError(
                        f"cannot create build-cache component {component!r}: {exc}"
                    ) from exc
                created = True
                # Set the exact owner-only mode before opening; os.mkdir's
                # mode is subject to the ambient umask, which could otherwise
                # strip the owner bits and make the new entry unopenable.
                os.chmod(component, 0o700, dir_fd=current_fd, follow_symlinks=False)
                value = os.stat(
                    component, dir_fd=current_fd, follow_symlinks=False,
                )
            except OSError as exc:
                raise BuildCacheError(
                    f"cannot access build-cache component {component!r}: {exc}"
                ) from exc
            if _stat.S_ISLNK(value.st_mode) or not _stat.S_ISDIR(value.st_mode):
                raise BuildCacheError(
                    f"unsafe build-cache component {component!r}; remove the "
                    "entry or restore it as a directory"
                )
            try:
                next_fd = os.open(component, _DIR_FLAGS, dir_fd=current_fd)
            except OSError as exc:
                raise BuildCacheError(
                    f"cannot open build-cache component {component!r}: {exc}"
                ) from exc
            # Transfer ownership of the child before any operation that can
            # fail, so the parent and child are each released exactly once.
            parent_fd, current_fd = current_fd, next_fd
            if created:
                # Explicit mode, independent of the process umask.
                os.fchmod(next_fd, 0o700)
            # Release the parent last, relinquishing ownership first so a
            # failed close is never retried; the child is still released by
            # the handler.
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


def _require_private_dir(
    fd: int,
    label: str,
    *,
    check_owner: bool,
) -> None:
    """Require an opened directory to be a directory (and optionally owned).

    Never creates, chmods, or chowns anything; this is a read-only check.
    """
    st = os.fstat(fd)
    if not _stat.S_ISDIR(st.st_mode):
        raise BuildCacheError(f"{label} is not a directory")
    if check_owner and st.st_uid != os.geteuid():
        raise BuildCacheError(
            f"{label} is not owned by the invoking user; restore ownership "
            "or remove the stale entry"
        )


def _require_algorithm_dir(algorithm_fd: int, algorithm: str) -> None:
    """Require an opened blob algorithm directory to be a private ``0700`` dir.

    Rejects a non-directory, foreign-owned, or otherwise incorrect (including
    permissive) mode without repairing it; the caller must already hold the
    descriptor opened no-follow.
    """
    st = os.fstat(algorithm_fd)
    if not _stat.S_ISDIR(st.st_mode):
        raise BuildCacheError(
            f"build blob algorithm {algorithm!r} entry is not a directory")
    if st.st_uid != os.geteuid():
        raise BuildCacheError(
            f"build blob algorithm {algorithm!r} directory is not owned by the invoking user")
    if _stat.S_IMODE(st.st_mode) != 0o700:
        raise BuildCacheError(
            f"build blob algorithm {algorithm!r} directory has mode "
            f"{oct(_stat.S_IMODE(st.st_mode))}, expected 0o700")


def _validate_relative_dir(
    namespace_fd: int,
    parts: tuple[str, ...],
    *,
    label: str,
    check_owner: bool,
) -> None:
    """Validate an existing build-cache component without mutating it."""
    fd = _open_relative_dir(namespace_fd, parts, create=False)
    if fd is None:
        return
    primary: BaseException | None = None
    try:
        _require_private_dir(fd, label, check_owner=check_owner)
    except BaseException as exc:
        primary = exc
        raise
    finally:
        failures = CleanupFailures(primary)
        failures.run(lambda: os.close(fd), ordinary=(OSError,))
        result = failures.complete()
        if result is not None:
            raise result


def _ensure_relative_dir(
    namespace_fd: int,
    parts: tuple[str, ...],
    *,
    label: str,
    check_owner: bool,
    chmod_existing: bool = True,
) -> None:
    """Create (if missing) and secure a build-cache component."""
    fd = _open_relative_dir(namespace_fd, parts, create=True)
    if fd is None:
        raise BuildCacheError(f"cannot open build-cache component {label}")
    primary: BaseException | None = None
    try:
        st = os.fstat(fd)
        if not _stat.S_ISDIR(st.st_mode):
            raise BuildCacheError(f"{label} is not a directory")
        if check_owner and st.st_uid != os.geteuid():
            raise BuildCacheError(
                f"{label} is not owned by the invoking user; restore ownership "
                "or remove the stale entry"
            )
        if chmod_existing and _stat.S_IMODE(st.st_mode) != 0o700:
            os.fchmod(fd, 0o700)
    except BaseException as exc:
        primary = exc
        raise
    finally:
        failures = CleanupFailures(primary)
        failures.run(lambda: os.close(fd), ordinary=(OSError,))
        result = failures.complete()
        if result is not None:
            raise result


# Relative component paths within the verified external namespace.
_BUILD_ARTIFACTS_NAME = "build-artifacts"
_BLOBS_NAME = "blobs"
_TMP_NAME = "tmp"
_MARKERS_NAME = "uncommitted"
_TRANSACTIONS_NAME = "transactions"

_CACHE_PARTS = (_BUILD_ARTIFACTS_NAME,)
_PERSISTENT_PARTS = _CACHE_PARTS
_BLOBS_PARTS = (_BUILD_ARTIFACTS_NAME, _BLOBS_NAME)
_TMP_PARTS = (_BUILD_ARTIFACTS_NAME, _TMP_NAME)
_GENERATED_PARENT_PARTS = (_TRANSACTIONS_NAME,)
_GENERATED_PARTS = _GENERATED_PARENT_PARTS
_MARKERS_PARTS = (_BUILD_ARTIFACTS_NAME, _MARKERS_NAME)


def prepare_build_cache(constructor_project_root: str | Path, *, cache_root: str | Path | None = None,
                        project_state: ProjectState | None = None) -> BuildCachePaths:
    """Prepare private external state for the selected constructor project.

    The source project is read/traversed only; every created component is
    relative to the verified external namespace. Existing unsafe state fails
    closed before any child is created.
    """
    project = _normalize_constructor_project(constructor_project_root)
    validate_host_owner_traversal(project)
    try:
        state = project_state or resolve_project_state(project, cache_root=cache_root, create=True)
        if state.project_path != project.resolve(strict=True):
            raise ProjectStateError("project state belongs to a different constructor project")
    except ProjectStateError as exc:
        raise BuildCacheError(str(exc)) from exc
    namespace = state.namespace
    namespace_fd = _open_namespace_fd(namespace)
    primary: BaseException | None = None
    try:
        for parts in (_CACHE_PARTS, _BLOBS_PARTS, _TMP_PARTS,
                      _GENERATED_PARENT_PARTS, _MARKERS_PARTS):
            _validate_relative_dir(namespace_fd, parts, label=str(namespace.joinpath(*parts)), check_owner=True)
        for parts in (_CACHE_PARTS, _BLOBS_PARTS, _TMP_PARTS,
                      _GENERATED_PARENT_PARTS, _MARKERS_PARTS):
            _ensure_relative_dir(namespace_fd, parts, label=str(namespace.joinpath(*parts)), check_owner=True)
    except BaseException as exc:
        primary = exc
        raise
    finally:
        failures = CleanupFailures(primary)
        failures.run(lambda: os.close(namespace_fd), ordinary=(OSError,))
        result = failures.complete()
        if result is not None:
            raise result
    return BuildCachePaths(
        constructor_project_root=project,
        namespace_root=namespace,
        persistent_root=state.build_artifacts_root,
        blobs_root=state.build_artifacts_root / "blobs",
        tmp_root=state.build_artifacts_root / "tmp",
        generated_root=state.transactions_root,
        markers_root=state.build_artifacts_root / "uncommitted",
    )


@dataclass
class BuildCacheState:
    """Verified descriptors retained for descriptor-relative operations.

    The descriptors are opened no-follow from the verified namespace after
    ``prepare_build_cache`` has validated and created every child.  Mutable
    control state is always addressed through these retained descriptors,
    never by reconstructing a ``Path`` from a previously validated path.
    """

    paths: BuildCachePaths
    persistent_fd: int
    blobs_fd: int
    tmp_fd: int
    markers_fd: int
    transactions_fd: int

    def close(self) -> None:
        for fd in (self.transactions_fd, self.markers_fd, self.tmp_fd,
                   self.blobs_fd, self.persistent_fd):
            os.close(fd)


def _open_validated_child(parent_fd: int, name: str, *, label: str) -> int:
    """Open *name* beneath *parent_fd* no-follow and require a private dir."""
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise BuildCacheError(f"unsafe build-cache {label} directory: {exc}") from exc
    try:
        st = os.fstat(fd)
        if (not _stat.S_ISDIR(st.st_mode)
                or st.st_uid != os.geteuid()
                or _stat.S_IMODE(st.st_mode) != 0o700):
            raise BuildCacheError(f"unsafe build-cache {label} directory")
        return fd
    except BaseException as exc:
        failures = CleanupFailures(exc)
        failures.run(lambda: os.close(fd), ordinary=(OSError,))
        result = failures.complete()
        if result is not None:
            raise result
        raise


def open_build_cache_state(
    constructor_project_root: str | Path,
    *,
    cache_root: str | Path | None = None,
    project_state: ProjectState | None = None,
) -> BuildCacheState:
    """Prepare the namespace and retain verified no-follow child descriptors."""
    paths = prepare_build_cache(constructor_project_root, cache_root=cache_root, project_state=project_state)
    namespace_fd = _open_namespace_fd(paths.namespace_root)
    opened: list[int] = []
    try:
        persistent_fd = _open_validated_child(
            namespace_fd, _BUILD_ARTIFACTS_NAME, label=_BUILD_ARTIFACTS_NAME)
        opened.append(persistent_fd)
        blobs_fd = _open_validated_child(
            persistent_fd, _BLOBS_NAME, label=_BLOBS_NAME)
        opened.append(blobs_fd)
        tmp_fd = _open_validated_child(
            persistent_fd, _TMP_NAME, label=_TMP_NAME)
        opened.append(tmp_fd)
        markers_fd = _open_validated_child(
            persistent_fd, _MARKERS_NAME, label=_MARKERS_NAME)
        opened.append(markers_fd)
        transactions_fd = _open_validated_child(
            namespace_fd, _TRANSACTIONS_NAME, label=_TRANSACTIONS_NAME)
        opened.append(transactions_fd)
    except BaseException as exc:
        # One accumulator owns every release on the failed-open path: the
        # already-opened children (reverse order) and the namespace descriptor.
        # A child release that raises an interruption or unexpected defect is
        # authoritative over the original open failure and any later ordinary
        # release failure, and the namespace descriptor is released under the
        # same precedence instead of a stale primary.
        failures = CleanupFailures(exc)
        for child in reversed(opened):
            failures.run(lambda child=child: os.close(child), ordinary=(OSError,))
        closing, namespace_fd = namespace_fd, None
        failures.run(lambda fd=closing: os.close(fd), ordinary=(OSError,))
        result = failures.complete()
        if result is not None:
            raise result
        raise

    # Success: the verified children are handed to the returned state, so only
    # the namespace descriptor is released here.  If that release fails for any
    # reason — ordinary error, unexpected defect, or process-control
    # interruption — every retained child is still released exactly once before
    # the failure propagates, with the namespace failure as the primary.
    failures = CleanupFailures(None)
    closing, namespace_fd = namespace_fd, None
    failures.run(lambda fd=closing: os.close(fd), ordinary=(OSError,))
    try:
        namespace_failure: BaseException | None = failures.complete()
    except BaseException as exc:
        namespace_failure = exc
    if namespace_failure is not None:
        child_failures = CleanupFailures(namespace_failure)
        for child in reversed(opened):
            child_failures.run(lambda child=child: os.close(child), ordinary=(OSError,))
        child_failures.complete()
        raise namespace_failure
    return BuildCacheState(
        paths=paths,
        persistent_fd=persistent_fd,
        blobs_fd=blobs_fd,
        tmp_fd=tmp_fd,
        markers_fd=markers_fd,
        transactions_fd=transactions_fd,
    )


# ═══════════════════════════════════════════════════════════════════════
# Verified-blob publication
# ═══════════════════════════════════════════════════════════════════════


def _unlink_absent_ok(name: str, dir_fd: int) -> None:
    """Unlink *name* beneath *dir_fd*, treating absence as idempotent.

    Only the domain-declared absence outcome (``FileNotFoundError``) is
    suppressed; every other failure (permission, I/O) propagates to the owning
    accumulator so it remains observable.
    """
    try:
        os.unlink(name, dir_fd=dir_fd)
    except FileNotFoundError:
        pass


def _validate_publication_inputs(identity: DigestIdentity, data: bytes) -> None:
    """Reject invalid publication inputs before any cache state is changed."""
    if not isinstance(identity, DigestIdentity):
        raise BuildCacheError("identity must be a DigestIdentity")
    if not isinstance(data, bytes):
        raise BuildCacheError("data must be bytes")
    actual = hashlib.new(identity.algorithm, data).digest()
    if actual != identity.digest_bytes:
        raise BuildCacheError(
            f"digest mismatch: expected {identity.sri()}, got "
            f"{identity.algorithm}-{actual.hex()}"
        )


def publish_verified_blob(
    identity: DigestIdentity,
    data: bytes,
    *,
    constructor_project_root: str | Path,
    cache_root: str | Path | None = None,
    project_state: ProjectState | None = None,
) -> Path:
    """Verify *data* and atomically publish it as an immutable ``0444`` blob.

    The payload digest is verified **before** any filesystem work, so a
    mismatch leaves the external constructor-project cache completely untouched.  After
    atomic publication the blob is re-verified for containment, type,
    permissions, and digest before its path is returned.
    """
    _validate_publication_inputs(identity, data)

    paths = prepare_build_cache(constructor_project_root, cache_root=cache_root, project_state=project_state)
    blob_path = build_blob_path(paths.blobs_root, identity)
    namespace_fd = _open_namespace_fd(paths.namespace_root)
    blobs_fd = tmp_fd = algorithm_fd = fd = None
    temp_name = f".publish-{os.urandom(16).hex()}"
    # Ownership of ``temp_name`` is established only once the exclusive create
    # succeeds; a pre-existing colliding entry is never owned and must never be
    # unlinked by this publication's cleanup.
    temporary_owned = False
    try:
        blobs_fd = _open_relative_dir(namespace_fd, _BLOBS_PARTS, create=False)
        tmp_fd = _open_relative_dir(namespace_fd, _TMP_PARTS, create=False)
        if blobs_fd is None or tmp_fd is None:
            raise BuildCacheError("prepared build-cache directories disappeared")
        try:
            algorithm_fd = os.open(identity.algorithm, _DIR_FLAGS, dir_fd=blobs_fd)
        except FileNotFoundError:
            os.mkdir(identity.algorithm, 0o700, dir_fd=blobs_fd)
            # Pin the freshly created algorithm directory to exactly 0700
            # before opening; os.mkdir's mode is masked by the ambient umask.
            os.chmod(identity.algorithm, 0o700, dir_fd=blobs_fd, follow_symlinks=False)
            try:
                algorithm_fd = os.open(identity.algorithm, _DIR_FLAGS, dir_fd=blobs_fd)
            except OSError as exc:
                raise BuildCacheError(
                    f"build blob algorithm directory {identity.algorithm!r} is unsafe: {exc}"
                ) from exc
        except OSError as exc:
            raise BuildCacheError(
                f"build blob algorithm directory {identity.algorithm!r} is unsafe: {exc}"
            ) from exc
        _require_algorithm_dir(algorithm_fd, identity.algorithm)
        fd = os.open(temp_name, os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=tmp_fd)
        temporary_owned = True
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.fchmod(fd, 0o444)
        os.lseek(fd, 0, os.SEEK_SET)
        check = hashlib.new(identity.algorithm)
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            check.update(chunk)
        st = os.fstat(fd)
        if not _stat.S_ISREG(st.st_mode) or (st.st_mode & 0o777) != 0o444:
            raise BuildCacheError("temporary published blob has unsafe type or mode")
        if check.digest() != identity.digest_bytes:
            raise BuildCacheError("temporary published blob digest mismatch")
        os.replace(temp_name, blob_path.name, src_dir_fd=tmp_fd, dst_dir_fd=algorithm_fd)
        # The temporary entry no longer exists under ``temp_name``; it is owned
        # by the published destination now.
        temporary_owned = False
    except BaseException as exc:
        # Every failure-path release — the temporary payload descriptor, the
        # temporary entry, and each descriptor opened for the publication —
        # runs through one accumulator so each action is attempted exactly once
        # and the in-flight publication failure keeps precedence.  Each
        # ownership slot is dropped *before* its action so a failed close is
        # never retried by the success-path release below.
        failures = CleanupFailures(exc)
        if fd is not None:
            descriptor, fd = fd, None
            failures.run(lambda: os.close(descriptor), ordinary=(OSError,))
        if temporary_owned and tmp_fd is not None:
            # Only a temporary entry this publication exclusively created is
            # removed; an ``O_EXCL`` name collision never reaches here with the
            # flag set, so a pre-existing entry is left untouched.  Ownership is
            # dropped before the action so a failed removal is never retried.
            temporary_owned = False
            unlink_dir_fd = tmp_fd
            failures.run(
                lambda: _unlink_absent_ok(temp_name, unlink_dir_fd),
                ordinary=(OSError,),
            )
        closing = (fd, algorithm_fd, blobs_fd, tmp_fd, namespace_fd)
        fd = algorithm_fd = blobs_fd = tmp_fd = namespace_fd = None
        for value in closing:
            if value is not None:
                failures.run(lambda value=value: os.close(value), ordinary=(OSError,))
        result = failures.complete()
        if result is not None:
            raise result
        raise
    finally:
        failures = CleanupFailures(None)
        for value in (fd, algorithm_fd, blobs_fd, tmp_fd, namespace_fd):
            if value is not None:
                failures.run(lambda value=value: os.close(value), ordinary=(OSError,))
        result = failures.complete()
        if result is not None:
            raise result
    _verify_published_blob(identity, paths)
    return blob_path


def _validate_blob_descriptor(alg_fd: int, filename: str, identity: DigestIdentity) -> None:
    """Validate one immutable blob descriptor-relatively without following links.

    Requires a regular file owned by the invoking user with mode exactly
    ``0444`` whose content digest matches *identity*.  *alg_fd* must be the
    verified algorithm directory descriptor; the blob is opened no-follow by
    name beneath it.
    """
    try:
        blob_fd = os.open(
            filename,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=alg_fd,
        )
    except OSError as exc:
        raise BuildCacheError(
            f"published blob {identity.algorithm}/{filename} is unsafe or missing: {exc}"
        ) from exc
    primary: BaseException | None = None
    try:
        blob_stat = os.fstat(blob_fd)
        if not _stat.S_ISREG(blob_stat.st_mode):
            raise BuildCacheError(f"published blob {identity.algorithm}/{filename} is not a regular file")
        if _stat.S_IMODE(blob_stat.st_mode) != 0o444:
            raise BuildCacheError(
                f"published blob {identity.algorithm}/{filename} has mode "
                f"{oct(_stat.S_IMODE(blob_stat.st_mode))}, expected 0o444"
            )
        if blob_stat.st_uid != os.geteuid():
            raise BuildCacheError("published blob is not owned by the invoking user")
        digest = hashlib.new(identity.algorithm)
        while chunk := os.read(blob_fd, 64 * 1024):
            digest.update(chunk)
        if digest.digest() != identity.digest_bytes:
            raise BuildCacheError(f"published blob {identity.algorithm}/{filename} digest mismatch")
    except BaseException as exc:
        primary = exc
        raise
    finally:
        failures = CleanupFailures(primary)
        failures.run(lambda: os.close(blob_fd), ordinary=(OSError,))
        result = failures.complete()
        if result is not None:
            raise result


def _verify_published_blob(identity: DigestIdentity, paths: BuildCachePaths) -> None:
    """Re-verify one published blob through stable no-follow descriptors.

    Every component is derived from *identity* and opened relative to the
    namespace descriptor.  The verification therefore cannot be redirected by
    a replacement of a pathname between a preliminary check and file read.
    """
    namespace_fd = blobs_fd = algorithm_fd = None
    algorithm = identity.algorithm
    filename = identity.hex_digest() + BLOB_EXTENSION
    primary: BaseException | None = None
    try:
        namespace_fd = _open_namespace_fd(paths.namespace_root)
        blobs_fd = _open_relative_dir(namespace_fd, _BLOBS_PARTS, create=False)
        if blobs_fd is None:
            raise BuildCacheError("published blob directory disappeared")
        try:
            algorithm_fd = os.open(algorithm, _DIR_FLAGS, dir_fd=blobs_fd)
        except OSError as exc:
            raise BuildCacheError(
                f"published blob algorithm directory {algorithm!r} is unsafe or missing: {exc}"
            ) from exc
        _require_algorithm_dir(algorithm_fd, algorithm)
        _validate_blob_descriptor(algorithm_fd, filename, identity)
    except BaseException as exc:
        primary = exc
        raise
    finally:
        failures = CleanupFailures(primary)
        for fd in (algorithm_fd, blobs_fd, namespace_fd):
            if fd is not None:
                failures.run(lambda fd=fd: os.close(fd), ordinary=(OSError,))
        result = failures.complete()
        if result is not None:
            raise result


class BuildTransactionError(BuildCacheError):
    """An external constructor-project build transaction cannot safely proceed."""


class _BuildStorageHandle:
    """Context manager yielding a build-domain storage view for recovery.

    ``blobs`` and ``markers`` are opened descriptor-relatively no-follow from
    the already-validated generation directory and released on exit, so no
    path is reconstructed from a validated path and the persistent generation
    capability stays owned by the lock.
    """

    def __init__(self, ops: PosixFileOps, generations: DirectoryCapability) -> None:
        self._ops = ops
        self._generations = generations
        self._blobs: DirectoryCapability | None = None
        self._markers: DirectoryCapability | None = None

    def _open(self, name: str) -> DirectoryCapability:
        base = self._generations.child_basename(name)
        fd = self._ops.openat(self._generations.fd, base, _STORAGE_DIRECTORY_FLAGS, 0)
        try:
            return DirectoryCapability.from_fd(self._ops, fd, f"build-artifacts/{name}")
        except BaseException as exc:
            # ``from_fd`` transfers ownership only on success, so the rejected
            # descriptor is still caller-owned.  Release it exactly once and
            # attach a close failure without replacing the adoption failure.
            failures = CleanupFailures(exc)
            failures.run(lambda: self._ops.close(fd), ordinary=(OSError,))
            result = failures.complete()
            if result is not None:
                raise result
            raise

    def __enter__(self) -> "BuildStorage":
        from docker.versioning.build_cleanup import BuildStorage

        self._blobs = self._open(_BLOBS_NAME)
        try:
            self._markers = self._open(_MARKERS_NAME)
        except BaseException as exc:
            # Opening the marker view failed after the blob view was adopted;
            # release the blob capability exactly once and keep the opening
            # failure primary over any ordinary close failure.
            blobs, self._blobs = self._blobs, None
            failures = CleanupFailures(exc)
            if blobs is not None:
                failures.run(blobs.close, ordinary=(OSError,))
            result = failures.complete()
            if result is not None:
                raise result
            raise
        return BuildStorage(self._generations, self._blobs, self._markers)

    def __exit__(self, exc_type: object, exc: BaseException | None, tb: object) -> bool:
        failures = CleanupFailures(exc)
        for capability in (self._markers, self._blobs):
            if capability is None:
                continue
            # Attempt every close even when an earlier one failed so no
            # descriptor is abandoned because of a sibling close failure; a
            # process-control interruption does not skip the remaining close.
            failures.run(capability.close, ordinary=(OSError,))
        result = failures.complete()
        if result is not None:
            raise result
        return False


class ConstructorProjectBuildLock:
    """The exclusive owner token for one constructor-project build transaction.

    Ownership is bound to the canonical constructor-project identity and the
    selected external cache namespace.  The lock cannot be reused for another
    project or another cache root.  The token wraps the shared fail-fast
    :class:`~docker.transactions.locking.LockCapability` over the generation
    directory so the contention policy, lock-entry validation, and post-
    acquisition mode repair are owned by the shared layer while the observable
    constructor-project diagnostics stay unchanged.
    """

    def __init__(self, ops: PosixFileOps, capability: LockCapability,
                 generation_directory: DirectoryCapability,
                 constructor_project_root: Path, cache_root: Path,
                 namespace: Path, project_state: ProjectState) -> None:
        self._ops = ops
        self._capability = capability
        self._generation_directory = generation_directory
        self._constructor_project_root = constructor_project_root
        self._cache_root = cache_root
        self._namespace = namespace
        self._project_state = project_state
        self._released = False

    @property
    def constructor_project_root(self) -> Path:
        return self._constructor_project_root

    @property
    def cache_root(self) -> Path:
        return self._cache_root

    @property
    def namespace(self) -> Path:
        return self._namespace

    @property
    def project_state(self) -> ProjectState:
        return self._project_state

    @property
    def ops(self) -> PosixFileOps:
        return self._ops

    @property
    def capability(self) -> LockCapability:
        return self._capability

    @property
    def generation_directory(self) -> DirectoryCapability:
        return self._generation_directory

    def open_storage(self) -> _BuildStorageHandle:
        """Open blob and marker capabilities for generation cleanup/recovery."""
        return _BuildStorageHandle(self._ops, self._generation_directory)

    def assert_held_for(self, constructor_project_root: str | Path, *,
                        cache_root: str | Path | None = None) -> None:
        if self._released:
            raise BuildTransactionError("a live constructor-project build lock is required")
        try:
            canonical = Path(constructor_project_root).resolve(strict=True)
        except OSError as exc:
            raise BuildTransactionError(
                "the constructor-project build lock belongs to a different constructor project"
            ) from exc
        if canonical != self._constructor_project_root:
            raise BuildTransactionError(
                "the constructor-project build lock belongs to a different constructor project"
            )
        if _resolve_cache_root(cache_root) != self._cache_root:
            raise BuildTransactionError(
                "the constructor-project build lock belongs to a different cache namespace"
            )
        from docker.versioning.build_generations import BUILD_LOCK_NAMESPACE
        try:
            self._capability.assert_authorizes(
                directory=self._generation_directory,
                namespace=BUILD_LOCK_NAMESPACE,
            )
        except CapabilityError as exc:
            raise BuildTransactionError(
                "a live constructor-project build lock is required"
            ) from exc

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        failures = CleanupFailures(None)

        def close_capability() -> None:
            try:
                self._capability.close()
            except LockError as exc:
                # Preserve the raw descriptor failure across the L2 boundary so
                # a release failure stays an ordinary OSError at the build edge,
                # exactly as the previous direct unlock/close did.  Shared
                # unlock cleanup diagnostics follow the raw cause.
                cause = exc.cause
                if isinstance(cause, OSError):
                    carry_secondary_diagnostics(cause, exc)
                    raise cause
                raise

        failures.run(close_capability, ordinary=(OSError, LockError))
        failures.run(self._generation_directory.close, ordinary=(OSError,))
        result = failures.complete()
        if result is not None:
            raise result

    def __enter__(self) -> "ConstructorProjectBuildLock":
        return self

    def __exit__(
        self,
        exc_type: object,
        exc: BaseException | None,
        tb: object,
    ) -> None:
        # One release attempt via the shared accumulator: an ordinary release
        # failure is returned to the caller only when no body exception is in
        # flight, otherwise it stays secondary to the body exception.  A
        # process-control interruption propagates unchanged.
        failures = CleanupFailures(exc)
        failures.run(self.release, ordinary=(Exception,))
        result = failures.complete()
        if result is not None:
            raise result


def _bootstrap_constructor_project_lock_parent(constructor_project_root: str | Path, *, cache_root: str | Path | None = None) -> tuple[ProjectState, int]:
    """Open the lock parent, creating only missing private path components.

    Existing cache entries are deliberately not chmodded or otherwise
    repaired here.  Contenders must be rejected by the lock before cache
    validation/mutation is attempted; the owner validates them afterwards.
    """
    constructor_project = _normalize_constructor_project(constructor_project_root)
    validate_host_owner_traversal(constructor_project)
    try:
        state = resolve_project_state(constructor_project, cache_root=cache_root, create=True, validate_children=False)
    except ProjectStateError as exc:
        raise BuildTransactionError(str(exc)) from exc
    namespace_fd = _open_namespace_fd(state.namespace)
    current_fd = namespace_fd
    primary: BaseException | None = None
    try:
        for part in _PERSISTENT_PARTS:
            try:
                child_fd = os.open(part, _DIR_FLAGS, dir_fd=current_fd)
            except FileNotFoundError:
                try:
                    os.mkdir(part, 0o700, dir_fd=current_fd)
                except FileExistsError:
                    # Another first build won component creation; reopen it
                    # no-follow and validate it exactly like an existing one.
                    pass
                child_fd = os.open(part, _DIR_FLAGS, dir_fd=current_fd)
            info = os.fstat(child_fd)
            if not _stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                os.close(child_fd)
                raise BuildTransactionError(f"unsafe lock parent component: {part}")
            if current_fd != namespace_fd:
                os.close(current_fd)
            current_fd = child_fd
        return state, current_fd
    except BaseException as exc:
        primary = exc
        if current_fd != namespace_fd:
            failures = CleanupFailures(exc)
            failures.run(lambda: os.close(current_fd), ordinary=(OSError,))
            result = failures.complete()
            if result is not None:
                primary = result
                raise result
        raise
    finally:
        failures = CleanupFailures(primary)
        failures.run(lambda: os.close(namespace_fd), ordinary=(OSError,))
        result = failures.complete()
        if result is not None:
            raise result


def _lock_entry_is_unsafe(directory: DirectoryCapability, base: str) -> bool:
    """Classify the lock entry itself, independent of the failed operation.

    A failed open/stat/mode repair is operational only when the entry at the
    lock pathname is still a safe owner-private single-linked regular file.
    Symlinks, directories, FIFOs, foreign-owned entries, and multiply-linked
    entries are unsafe by inspection of the entry, not by the errno the failed
    operation happened to report.
    """
    try:
        info = os.stat(base, dir_fd=directory.fd, follow_symlinks=False)
    except OSError:
        return False
    return (
        not _stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
    )


def _raise_lock_failure(directory: DirectoryCapability, base: str, exc: LockError) -> NoReturn:
    """Re-raise a shared lock failure with the legacy build diagnostic.

    Only a genuine unsafe lock entry (or an indistinguishable in-place
    replacement) becomes the build-domain "unsafe constructor-project build
    lock" error.  Operational open, stat, and mode-repair failures keep their
    underlying ``OSError`` as the primary exception so the previous raw-errno
    behavior is preserved, with any attached cleanup failures carried over.
    """
    if _lock_entry_is_unsafe(directory, base) or (
        exc.stage == STAGE_LOCK_VALIDATE and exc.cause is None
    ):
        raise BuildTransactionError("unsafe constructor-project build lock") from exc
    cause = exc.cause
    if isinstance(cause, OSError):
        carry_secondary_diagnostics(cause, exc)
        raise cause
    raise exc


def acquire_constructor_project_build_lock(constructor_project_root: str | Path, *, cache_root: str | Path | None = None) -> ConstructorProjectBuildLock:
    """Acquire the single non-blocking constructor-project transaction lock first.

    The lock file lives under the selected project's external namespace and
    is never created inside the constructor project.  The returned token is bound to the
    canonical project identity and the selected cache namespace.  Acquisition
    is delegated to the shared fail-fast lock capability so the contention
    policy, lock-entry validation, and post-acquisition mode repair are owned
    by the shared layer, while a competing build is rejected before any cache
    validation or mutation and the existing diagnostics are preserved.
    """
    from docker.versioning.build_generations import BUILD_LOCK_NAME, acquire_build_generation_lock

    state, parent_fd = _bootstrap_constructor_project_lock_parent(constructor_project_root, cache_root=cache_root)
    ops = PosixFileOps()
    directory: DirectoryCapability | None = None
    capability: LockCapability | None = None
    try:
        directory = DirectoryCapability.from_fd(ops, parent_fd, "constructor-project build")
        parent_fd = -1
        try:
            capability = acquire_build_generation_lock(ops, directory)
        except LockContention as exc:
            raise BuildTransactionError(
                "constructor project already has an active build"
            ) from exc
        except LockError as exc:
            _raise_lock_failure(directory, BUILD_LOCK_NAME, exc)
        # The lock now serializes all validation and permitted cache repair.
        prepare_build_cache(state.project_path, cache_root=cache_root, project_state=state)
        return ConstructorProjectBuildLock(
            ops, capability, directory, state.project_path, state.cache_root,
            state.namespace, state,
        )
    except BaseException as exc:
        # Release the acquired lock capability, the adopted directory, and the
        # caller-owned parent descriptor exactly once each, attempting every
        # independent close even when an earlier one fails.  Ordinary close
        # failures stay secondary to the primary; a process-control
        # interruption is never converted or suppressed.
        failures = CleanupFailures(exc)
        if capability is not None:
            failures.run(capability.close, ordinary=(OSError, LockError))
        if directory is not None:
            failures.run(directory.close, ordinary=(OSError,))
        parent, parent_fd = parent_fd, -1
        if parent >= 0:
            failures.run(lambda: os.close(parent), ordinary=(OSError,))
        result = failures.complete()
        if result is not None:
            raise result
        raise


def _key(identity: DigestIdentity) -> str:
    return f"{identity.algorithm}:{identity.hex_digest()}"


def _marker_name(identity: DigestIdentity) -> str:
    return _key(identity) + ".json"


def _require_control_name(name: object) -> str:
    """Require a single, non-empty, non-special basename for a control file."""
    if (
        not isinstance(name, str)
        or not name
        or name in (".", "..")
        or os.path.isabs(name)
        or "/" in name
        or (os.sep != "/" and os.sep in name)
        or Path(name).name != name
    ):
        raise BuildTransactionError(f"unsafe transaction state file name: {name!r}")
    return name


_MARKER_MODE = 0o600
"""Private mode required of every marker control file."""


def _replace_marker_json(
    ops: PosixFileOps,
    directory: DirectoryCapability,
    name: str,
    value: object,
) -> None:
    """Durably replace one marker control file through the shared L2 contract.

    Marker naming, timestamp validation, JSON interpretation, and retention
    policy remain in the build domain; only the complete durable-replacement
    mechanics are delegated to :class:`RegularFileContracts`.  Unsafe-entry
    validation retains the build-domain diagnostic, while operational failures
    re-raise the original ``OSError`` object with shared cleanup failures
    attached.  Process-control interruptions propagate unchanged.
    """
    _require_control_name(name)
    _validate_existing_marker(directory, name)
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    try:
        RegularFileContracts(ops).durable_replace(
            directory, name, payload, _MARKER_MODE
        )
    except TransactionError as exc:
        _raise_marker_failure(name, exc)


def _validate_existing_marker(directory: DirectoryCapability, name: str) -> None:
    """Validate an existing marker's legacy mode before L2 replacement."""
    try:
        marker = directory.open_regular(name, allowed_mode=_MARKER_MODE)
    except TransactionError as exc:
        if isinstance(exc.cause, FileNotFoundError):
            return
        _raise_marker_failure(name, exc)
    try:
        marker.close()
    except OSError:
        # No earlier failure exists, so preserve the raw operational close error.
        raise


def _raise_marker_failure(name: str, exc: TransactionError) -> NoReturn:
    """Map validation failures while preserving legacy operational errors."""
    if isinstance(exc, UnsafeFileError) and exc.stage in (
        STAGE_VALIDATE,
        STAGE_VALIDATE_DESTINATION,
    ):
        raise BuildTransactionError(f"unsafe transaction state file: {name}") from exc
    cause = exc.cause
    if isinstance(cause, OSError):
        carry_secondary_diagnostics(cause, exc)
        raise cause
    raise exc


def _read_marker_json(
    ops: PosixFileOps,
    directory: DirectoryCapability,
    name: str,
) -> object:
    """Read one marker control file through the shared validated-read contract.

    The secure no-follow read and leaf validation are delegated to
    :class:`RegularFileContracts`; JSON interpretation stays in the build
    domain so a malformed marker still raises ``ValueError``.  Unsafe-entry
    validation retains the build-domain diagnostic, while operational failures
    re-raise the original ``OSError`` object with shared close failures
    attached.  Process-control interruptions propagate unchanged.
    """
    _require_control_name(name)
    try:
        data = RegularFileContracts(ops).validated_read(
            directory, name, allowed_mode=_MARKER_MODE
        )
    except TransactionError as exc:
        _raise_marker_failure(name, exc)
    return json.loads(data)


def publish_uncommitted_blob(
    identity: DigestIdentity,
    data: bytes,
    *,
    constructor_project_root: str | Path,
    lock: ConstructorProjectBuildLock,
    verified_at: float | None = None,
    cache_root: str | Path | None = None,
    project_state: ProjectState | None = None,
) -> Path:
    """Atomically mark then publish a verified uncommitted blob under one lock.

    The marker is deliberately durable before publication: an interruption
    cannot strand a verified blob without retention state.  Every failure
    retains the marker: maintenance later removes it when no safe blob exists.
    """
    lock.assert_held_for(constructor_project_root, cache_root=cache_root)
    _validate_publication_inputs(identity, data)
    mark_uncommitted_blob(identity, constructor_project_root, lock=lock, verified_at=verified_at,
                          cache_root=cache_root, project_state=project_state)
    return publish_verified_blob(identity, data, constructor_project_root=constructor_project_root,
                                 cache_root=cache_root, project_state=project_state)


def _validate_marker_timestamp(value: object, *, label: str) -> float | int:
    """Require finite numeric marker and fake-clock timestamps."""
    if isinstance(value, bool):
        raise BuildCacheError(f"{label} must be a finite int or float")
    if isinstance(value, int):
        # Do not pass arbitrary JSON integers to math.isfinite(): conversion
        # to float raises OverflowError beyond the finite float range.
        if value.bit_length() > sys.float_info.max_exp:
            raise BuildCacheError(f"{label} is outside the finite timestamp range")
        return value
    if not isinstance(value, float) or not math.isfinite(value):
        raise BuildCacheError(f"{label} must be a finite int or float")
    return value


def mark_uncommitted_blob(
    identity: DigestIdentity,
    constructor_project_root: str | Path,
    *,
    lock: ConstructorProjectBuildLock,
    verified_at: float | None = None,
    cache_root: str | Path | None = None,
    project_state: ProjectState | None = None,
) -> None:
    """Atomically record the publication time of a verified uncommitted blob."""
    lock.assert_held_for(constructor_project_root, cache_root=cache_root)
    prepare_build_cache(
        constructor_project_root, cache_root=cache_root, project_state=project_state
    )
    timestamp = _validate_marker_timestamp(
        time.time() if verified_at is None else verified_at, label="verified_at",
    )
    with lock.open_storage() as storage:
        _replace_marker_json(
            lock.ops,
            storage.markers,
            _marker_name(identity),
            {"verified_at": timestamp},
        )


def _identity_from_key(key: object) -> DigestIdentity:
    """Parse one manifest key and reject every noncanonical path-like form."""
    if not isinstance(key, str) or key.count(":") != 1:
        raise ValueError("blob key must be one canonical algorithm:digest string")
    algorithm, digest = key.split(":")
    if not algorithm or not digest or any(token in key for token in ("/", "\\", "..")):
        raise ValueError("blob key contains a path component")
    identity = DigestIdentity.from_hex(algorithm, digest)
    if key != _key(identity):
        raise ValueError("blob key is not canonical")
    return identity


def _read_authoritative_blobs(lock: "ConstructorProjectBuildLock") -> set[DigestIdentity]:
    """Return the committed blob set of the durable authoritative generation.

    Discovery synchronizes the generation directory before the visible newest
    generation is trusted, so a marker for a committed blob is only removed
    after its generation is durable.  The legacy mutable manifest is never
    inspected: ``committed-build.json`` is outside generation discovery.
    """
    from docker.versioning.build_generations import discover_generations

    inventory = discover_generations(
        lock.ops, lock.generation_directory, lock=lock.capability
    )
    current = inventory.current
    return set(current.blobs) if current is not None else set()


def _durably_remove_marker(
    ops: PosixFileOps,
    markers: DirectoryCapability,
    name: str,
) -> None:
    """Durably remove one build-owned marker through the shared L2 contract."""
    _require_control_name(name)
    try:
        RegularFileContracts(ops).durable_unlink(
            markers,
            name,
            allow_absent=True,
            allowed_mode=_MARKER_MODE,
        )
    except TransactionError as exc:
        _raise_marker_failure(name, exc)


def _open_blob_algorithm(
    ops: PosixFileOps,
    blobs: DirectoryCapability,
    algorithm: str,
) -> DirectoryCapability | None:
    """Open one validated private blob-algorithm directory, or return absent."""
    base = blobs.child_basename(algorithm)
    try:
        fd = ops.openat(blobs.fd, base, _STORAGE_DIRECTORY_FLAGS, 0)
    except FileNotFoundError:
        return None
    try:
        info = ops.fstat(fd)
        if (
            not _stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or _stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise BuildCacheError(
                f"unsafe blob algorithm directory {algorithm!r}"
            )
        return DirectoryCapability.from_fd(
            ops, fd, f"build-artifacts/blobs/{algorithm}"
        )
    except BaseException as exc:
        # The rejected descriptor is still caller-owned after ``from_fd``
        # fails; release it exactly once, keeping the validation or
        # interruption primary over any ordinary close failure.
        failures = CleanupFailures(exc)
        failures.run(lambda: ops.close(fd), ordinary=(OSError,))
        result = failures.complete()
        if result is not None:
            raise result
        raise


def _validate_existing_blob(
    directory: DirectoryCapability,
    identity: DigestIdentity,
) -> None:
    """Validate an existing immutable blob before any paired deletion."""
    filename = identity.hex_digest() + BLOB_EXTENSION
    try:
        blob = directory.open_regular(filename, allowed_mode=0o444)
    except TransactionError as exc:
        if isinstance(exc.cause, FileNotFoundError):
            return
        if isinstance(exc, UnsafeFileError):
            raise BuildTransactionError(
                f"unsafe build blob: {_key(identity)}"
            ) from exc
        cause = exc.cause
        if isinstance(cause, OSError):
            carry_secondary_diagnostics(cause, exc)
            raise cause
        raise
    blob.close()


def _durably_remove_blob_and_marker(
    ops: PosixFileOps,
    blobs: DirectoryCapability,
    markers: DirectoryCapability,
    identity: DigestIdentity,
) -> None:
    """Durably remove only entries derived from one canonical identity."""
    marker_name = _marker_name(identity)
    _validate_existing_marker(markers, marker_name)
    algorithm_directory = _open_blob_algorithm(ops, blobs, identity.algorithm)
    if algorithm_directory is not None:
        with algorithm_directory:
            filename = identity.hex_digest() + BLOB_EXTENSION
            _validate_existing_blob(algorithm_directory, identity)
            try:
                RegularFileContracts(ops).durable_unlink(
                    algorithm_directory,
                    filename,
                    allow_absent=True,
                    allowed_mode=0o444,
                )
            except UnsafeFileError as exc:
                raise BuildTransactionError(
                    f"unsafe build blob: {_key(identity)}"
                ) from exc
            except TransactionError as exc:
                cause = exc.cause
                if isinstance(cause, OSError):
                    carry_secondary_diagnostics(cause, exc)
                    raise cause
                raise
    _durably_remove_marker(ops, markers, marker_name)


def recover_build_generations(
    constructor_project_root: str | Path,
    *,
    lock: ConstructorProjectBuildLock,
    cache_root: str | Path | None = None,
) -> None:
    """Recover committed generation state under *lock* before any build work.

    Discovery synchronizes the generation directory before accepting any
    visible newest generation as authoritative, then authoritative-generation
    markers are reconciled and any retained predecessor is cleaned up
    idempotently.  This must run before artifact materialization, snapshot
    work, Docker execution, or superseded cleanup; any failure blocks every
    such side effect and is reported as an operational build-cache error.
    """
    from docker.versioning.build_cleanup import recover_generations

    lock.assert_held_for(constructor_project_root, cache_root=cache_root)
    with lock.open_storage() as storage:
        recover_generations(lock.ops, storage, lock=lock.capability)


def commit_build_set(
    constructor_project_root: str | Path,
    identities: set[DigestIdentity],
    *,
    lock: ConstructorProjectBuildLock,
    cache_root: str | Path | None = None,
    project_state: ProjectState | None = None,
) -> None:
    """Durably publish the committed set as the next immutable generation.

    After publication the newest generation is authoritative and the previous
    generation is retained only as cleanup evidence.  Markers for every blob
    admitted to the authoritative generation are reconciled as one batch with
    one marker-directory synchronization, then every ``previous - current``
    candidate is durably removed before the predecessor manifest is unlinked.
    A reconciliation or cleanup failure preserves the predecessor and is
    reported as an operational build-cache error without rolling back the
    successful image or newest generation.
    """
    from docker.versioning.build_cleanup import (
        cleanup_superseded,
        reconcile_authoritative_markers,
    )
    from docker.versioning.build_generations import (
        inspect_generations,
        publish_generation,
    )

    lock.assert_held_for(constructor_project_root, cache_root=cache_root)
    paths = prepare_build_cache(
        constructor_project_root, cache_root=cache_root, project_state=project_state
    )
    live = set(identities)
    try:
        for identity in live:
            _verify_published_blob(identity, paths)
    except BuildCacheError as exc:
        raise BuildTransactionError("cannot commit an unsafe or invalid build blob") from exc
    publish_generation(lock.ops, lock.generation_directory, live, lock=lock.capability)
    try:
        inventory = inspect_generations(lock.ops, lock.generation_directory)
        with lock.open_storage() as storage:
            current = inventory.current
            if current is not None:
                reconcile_authoritative_markers(
                    lock.ops, storage, current, lock=lock.capability
                )
            cleanup_superseded(lock.ops, storage, inventory, lock=lock.capability)
    except (BuildCacheError, CapabilityError, OSError) as exc:
        raise PostCommitBuildError(
            "post-publication marker reconciliation or cleanup failed"
        ) from exc


def maintain_uncommitted_blobs(
    constructor_project_root: str | Path,
    *,
    lock: ConstructorProjectBuildLock,
    now: float | None = None,
    cache_root: str | Path | None = None,
    project_state: ProjectState | None = None,
) -> None:
    """Remove corrupt/partial and expired uncommitted state under the lock."""
    lock.assert_held_for(constructor_project_root, cache_root=cache_root)
    paths = prepare_build_cache(
        constructor_project_root,
        cache_root=cache_root,
        project_state=project_state,
    )
    current_time = _validate_marker_timestamp(
        time.time() if now is None else now, label="now",
    )
    live = _read_authoritative_blobs(lock)
    with lock.open_storage() as storage:
        markers = storage.markers
        for name in sorted(os.listdir(markers.fd)):
            if not name.endswith(".json"):
                continue
            stem = name[: -len(".json")]
            try:
                identity = _identity_from_key(stem)
            except (ValueError, TypeError):
                # A malformed marker cannot authorize any blob deletion.
                _durably_remove_marker(lock.ops, markers, name)
                continue
            if identity in live:
                # Committed blobs are TTL-immune; remove only their stale marker.
                _durably_remove_marker(lock.ops, markers, name)
                continue
            try:
                value = _read_marker_json(lock.ops, markers, name)
                if not isinstance(value, dict):
                    raise ValueError("marker is not an object")
                verified_at = _validate_marker_timestamp(
                    value["verified_at"], label="verified_at"
                )
            except (OSError, ValueError, KeyError, TypeError):
                _durably_remove_blob_and_marker(
                    lock.ops, storage.blobs, markers, identity
                )
                continue
            try:
                _verify_published_blob(identity, paths)
            except BuildCacheError:
                _durably_remove_blob_and_marker(
                    lock.ops, storage.blobs, markers, identity
                )
                continue
            if current_time - verified_at > UNCOMMITTED_TTL_SECONDS:
                _durably_remove_blob_and_marker(
                    lock.ops, storage.blobs, markers, identity
                )


_MAX_SNAPSHOT_DEPTH = 64


def _remove_snapshot_tree(parent_fd: int, name: str, *, depth: int) -> None:
    """Remove one abandoned snapshot entry without chmodding payload files.

    Symlinks and regular files are unlinked directly: unlinking requires
    write permission on the parent directory, never on the payload itself, so
    hard-linked snapshot payloads are never chmodded or otherwise mutated.
    Directories are reopened no-follow, their write permission is restored
    (directories only), and their children are removed recursively before the
    now-empty directory is removed.
    """
    _require_control_name(name)
    if depth <= 0:
        raise BuildTransactionError("abandoned snapshot tree is too deep")
    try:
        st = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if _stat.S_ISLNK(st.st_mode) or not _stat.S_ISDIR(st.st_mode):
        try:
            os.unlink(name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        return
    fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    primary: BaseException | None = None
    try:
        os.fchmod(fd, 0o700)
        for child in sorted(os.listdir(fd)):
            _remove_snapshot_tree(fd, child, depth=depth - 1)
    except BaseException as exc:
        primary = exc
        raise
    finally:
        failures = CleanupFailures(primary)
        failures.run(lambda: os.close(fd), ordinary=(OSError,))
        result = failures.complete()
        if result is not None:
            raise result
    try:
        os.rmdir(name, dir_fd=parent_fd)
    except FileNotFoundError:
        pass


def recover_abandoned_snapshots(
    constructor_project_root: str | Path, *, lock: ConstructorProjectBuildLock,
    cache_root: str | Path | None = None,
    project_state: ProjectState | None = None,
) -> None:
    """Remove snapshots found after exclusive ownership has been acquired."""
    lock.assert_held_for(constructor_project_root, cache_root=cache_root)
    state = open_build_cache_state(constructor_project_root, cache_root=cache_root, project_state=project_state)
    primary: BaseException | None = None
    try:
        for name in sorted(os.listdir(state.transactions_fd)):
            _remove_snapshot_tree(state.transactions_fd, name, depth=_MAX_SNAPSHOT_DEPTH)
    except BaseException as exc:
        primary = exc
        raise
    finally:
        failures = CleanupFailures(primary)
        failures.run(state.close, ordinary=(OSError,))
        result = failures.complete()
        if result is not None:
            raise result
