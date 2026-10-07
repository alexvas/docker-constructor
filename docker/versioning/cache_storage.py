"""Shared cache-path resolution and directory security.

The module provides two separate layers:

1. Pure path resolution (``resolve_default_root``, ``resolve_local_root``,
   and the named-child helpers).  These are lexical and deterministic:
   no filesystem reads or mutations and no implicit CWD dependence.
2. Filesystem validation and hardening (``prepare_default_root``,
   ``prepare_local_root``, and ``open_private_entry``).  These create
   missing constructor-owned directories with ``0700`` and secure
   existing invoking-user-owned directories to ``0700`` using no-follow,
   descriptor-relative operations.  They reject symlinks, non-directory
   entries, foreign ownership, and unsecurable paths with path-specific
   recovery guidance, and never chmod a parent directory.

Directory ownership, secure absolute-path walking, parent/child handoff,
and at-most-once release are delegated to the lightweight
``docker.filesystem.descriptors`` foundation through the injectable
``docker.filesystem.operations`` protocol.  This module keeps cache-root
resolution, XDG behavior, ``0700`` mode policy, recovery guidance, the
``CacheStorageError``/``InventoryError`` mapping, and the regular-file
publication policy.

The module deliberately imports no CLI, launcher, transport, HTTP
response-cache, artifact-verification, or provider module so it remains
an acyclic leaf that those consumers can depend on safely.
"""
from __future__ import annotations

import errno
import os
import stat as _stat
from pathlib import Path

from docker.filesystem.cleanup import CleanupFailures
from docker.filesystem.descriptors import (
    DescriptorError,
    DirectoryDescriptor,
    _STAGE_CREATE,
    _STAGE_OPEN,
    _STAGE_REOPEN,
)
from docker.filesystem.operations import DescriptorOps, PosixDescriptorOps

from .errors import InventoryError
from .model import LocalCacheConfig

_CACHE_ROOT_NAME = "docker-constructor"
_VERSIONING_CHILD = Path("versioning")
_RUNTIME_ARTIFACTS_BLOBS_CHILD = Path("runtime-artifacts") / "blobs"


class CacheStorageError(ValueError):
    """Invalid or unsafe cache-root configuration."""


def _normalize(path: str | Path) -> Path:
    """Lexically normalize *path* without touching the filesystem.

    ``os.path.normpath`` preserves an exact two-leading-slash prefix even
    though Linux resolves ``//x`` identically to ``/x``.  Collapse that
    prefix so unsafe-root comparison cannot be bypassed through ``//``,
    ``//xdg/cache``, or ``//xdg``.
    """
    value = os.path.normpath(str(path))
    if value.startswith("//") and not value.startswith("///"):
        value = "/" + value.lstrip("/")
    return Path(value)


def resolve_default_root(xdg_cache_home: str | None, *, home: Path) -> Path:
    """Return the default constructor cache root.

    Uses ``${XDG_CACHE_HOME}/docker-constructor`` only when
    ``XDG_CACHE_HOME`` is non-empty and absolute; otherwise falls back to
    ``~/.cache/docker-constructor`` under *home*.
    """
    if xdg_cache_home and os.path.isabs(xdg_cache_home):
        return _normalize(xdg_cache_home) / _CACHE_ROOT_NAME
    return _normalize(home) / ".cache" / _CACHE_ROOT_NAME


def resolve_local_root(
    value: str | None,
    *,
    xdg_cache_home: str | None,
    home: Path,
) -> Path | None:
    """Validate a local ``[cache].dir`` override and return its root.

    Returns ``None`` when no override is configured.  Rejects empty,
    relative, or ``~``-prefixed values, and rejects an absolute value that
    lexically normalizes to ``XDG_CACHE_HOME``, the invoking user's home
    directory, the filesystem root, or an ancestor of ``XDG_CACHE_HOME``.
    """
    if value is None:
        return None

    if not value or not os.path.isabs(value):
        raise CacheStorageError(
            "local.cache.dir must be an absolute path; set [cache].dir to a "
            "dedicated absolute directory"
        )

    root = _normalize(value)
    home_root = _normalize(home)
    filesystem_root = _normalize("/")

    xdg_root: Path | None = None
    if xdg_cache_home and os.path.isabs(xdg_cache_home):
        xdg_root = _normalize(xdg_cache_home)

    if xdg_root is not None and root == xdg_root:
        raise CacheStorageError(
            "local.cache.dir equals XDG_CACHE_HOME; choose a dedicated child "
            f"directory such as {xdg_root / 'docker-constructor-custom'}"
        )

    if root == home_root:
        raise CacheStorageError(
            "local.cache.dir equals the invoking user's home directory; "
            "choose a dedicated owned directory"
        )

    if root == filesystem_root:
        raise CacheStorageError(
            "local.cache.dir is the filesystem root; choose a dedicated owned "
            "directory"
        )

    if xdg_root is not None and xdg_root.is_relative_to(root):
        raise CacheStorageError(
            "local.cache.dir is an ancestor of XDG_CACHE_HOME; choose a "
            "dedicated owned directory"
        )

    return root


# ---------------------------------------------------------------------------
# Local [cache] table parsing (owned by user-cache-storage)
# ---------------------------------------------------------------------------


def parse_local_cache_config(
    cache_raw: object,
    host_access_mode: str | None,
) -> LocalCacheConfig:
    """Parse ``[cache]`` into the machine-local cache directory.

    Owns only the table's accepted field and type. An absent table (``None``)
    yields the owner-defined default. The configured directory's
    absolute/normalization/dangerous-root safety remains the cache-storage
    boundary's responsibility before any cache mutation.
    """
    del host_access_mode
    if cache_raw is None:
        return LocalCacheConfig()
    if not isinstance(cache_raw, dict):
        raise InventoryError("local.cache: expected table; use [cache].dir", field="local.cache")
    unknown_cache = set(cache_raw) - {"dir"}
    if unknown_cache:
        key = sorted(unknown_cache)[0]
        raise InventoryError(
            f"local.cache.{key}: unknown key; use only local.cache.dir",
            field=f"local.cache.{key}",
        )
    cache_dir = cache_raw.get("dir")
    if cache_dir is not None and not isinstance(cache_dir, str):
        raise InventoryError(
            "local.cache.dir: expected string; set [cache].dir to a filesystem path",
            field="local.cache.dir",
        )
    return LocalCacheConfig(cache_dir)


def versioning_child(root: Path) -> Path:
    """Return the HTTP cache child beneath *root*."""
    return Path(root) / _VERSIONING_CHILD


def runtime_artifacts_child(root: Path) -> Path:
    """Return the runtime-artifact state subtree beneath *root*."""
    return Path(root) / "runtime-artifacts"


def runtime_artifacts_blobs_child(root: Path) -> Path:
    """Return the verified artifact blob child beneath *root*."""
    return runtime_artifacts_child(root) / "blobs"


def runtime_artifacts_tmp_child(root: Path) -> Path:
    """Return the runtime-artifact temporary-state child beneath *root*."""
    return runtime_artifacts_child(root) / "tmp"


def runtime_artifacts_locks_child(root: Path) -> Path:
    """Return the runtime-artifact lock child beneath *root*."""
    return runtime_artifacts_child(root) / "locks"


# ---------------------------------------------------------------------------
# Filesystem validation and hardening
# ---------------------------------------------------------------------------

_DESCENDANT_PARTS = (
    ("versioning",),
    ("runtime-artifacts",),
    ("runtime-artifacts", "blobs"),
    ("runtime-artifacts", "locks"),
    ("runtime-artifacts", "tmp"),
)

#: Production descriptor backend.  Tests inject an alternative
#: ``DescriptorOps`` through the explicit ``ops`` keyword on each filesystem
#: entry point; the pure resolution layer never resolves a backend.
_PRODUCTION_OPS: DescriptorOps = PosixDescriptorOps()


def _resolve_ops(ops: DescriptorOps | None) -> DescriptorOps:
    """Return the injected backend or the production POSIX adapter."""
    return _PRODUCTION_OPS if ops is None else ops


def _foundation_cause(exc: DescriptorError) -> str:
    """Render a foundation failure's raw cause for domain diagnostics."""
    return str(exc.cause) if exc.cause is not None else str(exc)


def _errno_of(exc: DescriptorError) -> int | None:
    """Return the raw ``OSError.errno`` behind *exc*, if any."""
    cause = exc.cause
    return cause.errno if isinstance(cause, OSError) else None


def _entry_name(path: Path) -> str:
    """Return the basename of *path* after absolute normalization."""
    return os.path.basename(os.path.abspath(path))


def _entry_error(path: Path, exc: DescriptorError) -> CacheStorageError:
    """Map a foundation child-open/validation failure to its history wording."""
    cause = exc.cause
    errno_value = _errno_of(exc)
    if errno_value in (errno.EACCES, errno.EPERM):
        return CacheStorageError(
            f"cannot access {path}; restore ownership or remove it ({cause})"
        )
    if cause is not None:
        return CacheStorageError(
            f"unsafe cache entry at {path} (symlink or non-directory); "
            f"remove the entry ({cause})"
        )
    if "not owned by the invoking user" in str(exc):
        return CacheStorageError(
            f"{path} is not owned by the invoking user; restore ownership "
            "or remove the stale cache subtree"
        )
    return CacheStorageError(f"{path} is not a directory; remove it")


def _secure_error(path: Path, exc: DescriptorError) -> CacheStorageError:
    """Map a creation/securing failure while preserving its actionable text."""
    if exc.cause is not None and "cannot secure directory" in str(exc):
        return CacheStorageError(
            f"cannot secure {path}; restore ownership or remove it ({exc.cause})"
        )
    if exc.cause is not None and exc.stage == _STAGE_CREATE:
        return CacheStorageError(
            f"cannot create cache directory {path}: {exc.cause}"
        )
    if exc.cause is not None and exc.stage == _STAGE_REOPEN:
        return CacheStorageError(
            f"cannot open cache directory {path}: {exc.cause}"
        )
    return _entry_error(path, exc)


def _open_parent_fd(
    ops: DescriptorOps, path: Path, *, create_missing: bool
) -> DirectoryDescriptor | None:
    """Open the parent directory of *path* without following any symlink.

    Component walking is delegated to ``DirectoryDescriptor`` child
    operations; the retained parent/child handoff closes the previous
    component only after the next one is validated, so a failed parent
    release releases the opened child once and never retries the parent.
    Missing lead-up components are created descriptor-relatively with
    ``0700`` when *create_missing* is true; pre-existing components are
    never chmod-ed.  Returns the parent capability, or ``None`` when the
    lead-up does not exist and *create_missing* is false.
    """
    absolute = os.path.abspath(path)
    parent = os.path.dirname(absolute)
    try:
        current = DirectoryDescriptor.open_secure_path(
            ops, os.sep, label=os.sep, require_owner=False
        )
    except DescriptorError as exc:
        # The foundation owns the cleanup of a failed factory call; translate
        # the foundation error so callers keep their domain-level OSError and
        # ValueError handling instead of seeing a ``DescriptorError``.
        raise CacheStorageError(
            f"cannot access parent directory for {path}: "
            f"{_foundation_cause(exc)}; restore access or retry"
        ) from exc
    try:
        for component in (part for part in parent.split(os.sep) if part):
            try:
                info = current.stat_child(component, follow_symlinks=False)
            except FileNotFoundError:
                if not create_missing:
                    current.close()
                    return None
                try:
                    child = current.create_directory(
                        component, mode=0o700, label=component, require_owner=False
                    )
                except DescriptorError as exc:
                    raise CacheStorageError(
                        f"cannot create parent component {component!r} in "
                        f"{parent!r}: {_foundation_cause(exc)}"
                    ) from exc
            except OSError as exc:
                raise CacheStorageError(
                    f"cannot access parent component {component!r} in "
                    f"{parent!r}: {exc}; restore ownership or remove the entry"
                ) from exc
            else:
                if _stat.S_ISLNK(info.st_mode) or not _stat.S_ISDIR(info.st_mode):
                    raise CacheStorageError(
                        f"unsafe parent component {component!r} in {parent!r}; "
                        "remove the entry or restore ownership"
                    )
                try:
                    child = current.open_directory(
                        component, label=component, require_owner=False
                    )
                except DescriptorError as exc:
                    raise CacheStorageError(
                        f"cannot open parent component {component!r} in "
                        f"{parent!r}: {_foundation_cause(exc)}"
                    ) from exc
            previous, current = current, child
            previous.close()
        return current
    except BaseException as primary:
        _release_keeping_primary(current, primary)
        raise


def _open_entry_no_follow(
    parent: DirectoryDescriptor, path: Path
) -> DirectoryDescriptor | None:
    """Open *path* relative to *parent* with no-follow directory checks.

    Returns the opened capability, or ``None`` when the entry does not
    exist.  Raises :class:`CacheStorageError` for symlinks, non-directory
    entries, foreign ownership, or entries the process cannot access.
    """
    try:
        return parent.open_directory(
            _entry_name(path), label=str(path), require_owner=True
        )
    except DescriptorError as exc:
        # Only an initial open missing the entry is absence. An ``ENOENT``
        # raised by descriptor validation (for example ``fstat`` on an entry
        # removed after the open) is a real failure and must not be swallowed
        # and let preparation continue to mutate the tree.
        if exc.stage == _STAGE_OPEN and _errno_of(exc) == errno.ENOENT:
            return None
        raise _entry_error(path, exc) from exc


def _inspect_entry(ops: DescriptorOps, path: Path) -> None:
    """Validate an existing entry without mutating anything."""
    parent = _open_parent_fd(ops, path, create_missing=False)
    if parent is None:
        return
    with parent:
        entry = _open_entry_no_follow(parent, path)
        if entry is not None:
            entry.close()


def _create_and_secure(ops: DescriptorOps, path: Path) -> None:
    """Create (if missing) and secure a single directory to ``0700``."""
    parent = _open_parent_fd(ops, path, create_missing=True)
    if parent is None:
        raise CacheStorageError(f"cannot create parent directory for {path}")
    with parent:
        try:
            entry = parent.open_or_create_directory(
                _entry_name(path), mode=0o700, label=str(path), require_owner=True
            )
        except DescriptorError as exc:
            raise _secure_error(path, exc) from exc
        entry.close()


def _harden_tree(ops: DescriptorOps, root: Path) -> None:
    """Inspect every existing root/descendant, then create and secure all.

    All existing entries are validated before any mutation so that an
    unsafe root or descendant is rejected while leaving prior modes and
    absent descendants untouched.
    """
    _inspect_entry(ops, root)
    for parts in _DESCENDANT_PARTS:
        _inspect_entry(ops, root.joinpath(*parts))

    _create_and_secure(ops, root)
    for parts in _DESCENDANT_PARTS:
        _create_and_secure(ops, root.joinpath(*parts))


def _map_write_directory_error(
    directory: Path, exc: DescriptorError
) -> CacheStorageError:
    """Map a prepared-directory open failure to its history wording."""
    # Only an initial open that finds no entry is absence; a validation-stage
    # ``ENOENT`` (``fstat`` after a successful open) is not a missing directory
    # and must not be misclassified as one.
    if exc.stage == _STAGE_OPEN and _errno_of(exc) == errno.ENOENT:
        return CacheStorageError(
            f"cache directory {directory} does not exist; prepare the "
            "constructor cache root before writing cache entries"
        )
    return _entry_error(directory, exc)


def _release_keeping_primary(
    capability: DirectoryDescriptor, primary: BaseException
) -> None:
    """Release *capability* once, keeping *primary* authoritative.

    An ordinary close failure is retained on *primary* as secondary
    diagnostic context; a process-control interruption or unexpected
    release defect outranks the ordinary primary and propagates.  The
    capability records the release attempt before calling ``close``, so a
    failed release is never retried.
    """
    failures = CleanupFailures(primary)
    failures.run(capability.close, ordinary=(OSError,))
    result = failures.complete()
    if result is not None:
        raise result


def _release_file_and_capability(
    ops: DescriptorOps,
    fd: int,
    capability: DirectoryDescriptor,
    primary: BaseException,
) -> None:
    """Release an opened regular file and its directory capability once each.

    *primary* stays authoritative; an ordinary close failure from either
    release is retained on it as secondary diagnostic context.  ``fd`` is
    released only when it is live (``>= 0``), and each resource is attempted
    at most once, so a failed close is never retried.
    """
    failures = CleanupFailures(primary)
    if fd >= 0:
        failures.run(lambda: ops.close(fd), ordinary=(OSError,))
    failures.run(capability.close, ordinary=(OSError,))
    result = failures.complete()
    if result is not None:
        raise result


def _release_file_keeping_primary(
    ops: DescriptorOps, fd: int, primary: BaseException
) -> None:
    """Release an opened regular file once, keeping *primary* authoritative.

    Used after an owning directory-capability close failure: the capability
    has already recorded its single release attempt, so only the opened file
    is released here.  An ordinary file-close failure is retained on
    *primary* as secondary diagnostic context, and neither close is retried.
    """
    failures = CleanupFailures(primary)
    failures.run(lambda: ops.close(fd), ordinary=(OSError,))
    result = failures.complete()
    if result is not None:
        raise result


def _open_directory_for_write(
    ops: DescriptorOps, directory: Path
) -> DirectoryDescriptor:
    """Open a prepared cache directory no-follow and require privacy.

    The directory must already exist, be a directory, be owned by the
    invoking user, and be mode ``0700``.  It is never chmod-ed — an
    unprepared or non-private directory is rejected so DiskCache can
    fail closed instead of hardening an arbitrary caller-supplied path.
    """
    absolute = os.path.abspath(str(directory))
    try:
        capability = DirectoryDescriptor.open_secure_path(
            ops, absolute, label=str(directory), require_owner=True
        )
    except DescriptorError as exc:
        raise _map_write_directory_error(directory, exc) from exc
    try:
        mode = _stat.S_IMODE(ops.fstat(capability.fd).st_mode)
    except OSError as exc:
        primary = CacheStorageError(
            f"cannot access {directory}; restore ownership or remove it ({exc})"
        )
        _release_keeping_primary(capability, primary)
        raise primary from exc
    except BaseException as primary:
        # A cancellation or unexpected defect during the privacy check must
        # not leak the adopted directory descriptor, and must be re-raised
        # unchanged rather than remapped to ``CacheStorageError``.
        _release_keeping_primary(capability, primary)
        raise
    if mode != 0o700:
        primary = CacheStorageError(
            f"cache directory {directory} is not private (mode "
            f"{mode:04o}); the constructor "
            "cache directory must be mode 0700 and owned by the "
            "invoking user"
        )
        _release_keeping_primary(capability, primary)
        raise primary
    return capability


def _require_entry_name(name: str) -> None:
    """Require *name* to be a single, non-empty, non-special basename.

    Rejects ``""``, ``"."``, ``".."``, absolute paths, values containing
    a path separator, and any value whose ``Path(name).name`` does not
    round-trip to itself (e.g. ``"../outside"`` or ``"nested/file"``).
    """
    if (
        not name
        or name in (".", "..")
        or os.path.isabs(name)
        or "/" in name
        or (os.sep != "/" and os.sep in name)
        or Path(name).name != name
    ):
        raise CacheStorageError(f"unsafe cache entry name: {name!r}")


def open_private_entry(
    directory: Path, name: str, *, ops: DescriptorOps | None = None
) -> int:
    """Open (or create) a private ``0600`` file beneath a prepared directory.

    *directory* must already exist and be owned by the invoking user; it
    is opened no-follow through the descriptor foundation and is never
    chmod-ed.  *name* must be a single basename; it is then opened
    no-follow beneath that descriptor with ``O_WRONLY | O_CREAT |
    O_TRUNC`` and clamped to ``0600`` on its descriptor.  Returns the
    writable descriptor (the caller owns closing it).  Raises
    :class:`CacheStorageError` when the directory is missing, symlinked,
    foreign-owned, the name is unsafe, or the entry cannot be opened or
    secured.  The regular-file ownership and ``0600`` policy stay local to
    this module.
    """
    _require_entry_name(name)
    descriptor_ops = _resolve_ops(ops)
    capability = _open_directory_for_write(descriptor_ops, directory)
    fd = -1
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = descriptor_ops.openat(capability.fd, name, flags, 0o600)
        except OSError as exc:
            raise CacheStorageError(
                f"cannot open cache entry {Path(directory) / name}: {exc}"
            ) from exc
        try:
            descriptor_ops.fchmod(fd, 0o600)
        except OSError as exc:
            raise CacheStorageError(
                f"cannot secure cache entry {Path(directory) / name}: {exc}"
            ) from exc
    except BaseException as primary:
        # Opening or securing failed: keep the domain error primary while
        # releasing the file (if it was opened) and the directory once each.
        _release_file_and_capability(descriptor_ops, fd, capability, primary)
        raise
    # The file is open and secured.  Hand it to the caller only after the
    # directory capability closes cleanly, so a close failure can never leak
    # the regular-file descriptor.
    try:
        capability.close()
    except BaseException as primary:
        _release_file_keeping_primary(descriptor_ops, fd, primary)
        raise
    return fd


def publish_private_entry(
    directory: Path,
    tmp_name: str,
    final_name: str,
    *,
    ops: DescriptorOps | None = None,
) -> None:
    """Atomically publish *tmp_name* as *final_name* beneath *directory*.

    Both names must be single basenames.  The prepared directory is
    reopened no-follow through the descriptor foundation and the rename
    happens descriptor-relatively (``os.replace`` with ``src_dir_fd`` and
    ``dst_dir_fd``), never by reopening the path.  Failures map to
    :class:`CacheStorageError`; an ordinary directory-close failure stays
    secondary to a publication failure.
    """
    _require_entry_name(tmp_name)
    _require_entry_name(final_name)
    descriptor_ops = _resolve_ops(ops)
    capability = _open_directory_for_write(descriptor_ops, directory)
    with capability:
        try:
            os.replace(
                tmp_name,
                final_name,
                src_dir_fd=capability.fd,
                dst_dir_fd=capability.fd,
            )
        except OSError as exc:
            raise CacheStorageError(
                f"cannot publish cache entry {Path(directory) / final_name}: {exc}"
            ) from exc


def _prepare_explicit_xdg(ops: DescriptorOps, xdg: Path) -> None:
    """Create or validate an explicit ``XDG_CACHE_HOME`` safely.

    Walks every component from the filesystem root using the descriptor
    foundation's no-follow child operations.  Symlinked or non-directory
    components are rejected.  A missing component is created with
    ``0700``; an existing component is validated without being chmod-ed.
    The final existing directory must also be writable.
    """
    absolute = os.path.abspath(str(xdg))
    components = [part for part in absolute.split(os.sep) if part]
    if not components:
        # The XDG path is the filesystem root itself.
        if not _stat.S_ISDIR(os.stat(absolute).st_mode) or not os.access(
            absolute, os.W_OK
        ):
            raise CacheStorageError(
                f"XDG_CACHE_HOME is not a writable directory: {xdg}"
            )
        return

    try:
        current = DirectoryDescriptor.open_secure_path(
            ops, os.sep, label=os.sep, require_owner=False
        )
    except DescriptorError as exc:
        # The foundation owns the cleanup of a failed factory call; translate
        # the foundation error so preparation reports a domain-level
        # ``CacheStorageError`` and callers keep their existing handlers.
        raise CacheStorageError(
            f"cannot access filesystem root while preparing XDG_CACHE_HOME "
            f"{xdg}: {_foundation_cause(exc)}; restore access or retry"
        ) from exc
    try:
        for index, component in enumerate(components):
            final = index == len(components) - 1
            existed = True
            try:
                child = current.open_directory(
                    component, label=component, require_owner=False
                )
            except DescriptorError as exc:
                errno_value = _errno_of(exc)
                # Only an initial open that finds no entry may be created.
                # An ``ENOENT`` from validation (``fstat`` after a successful
                # open) is a real failure and must not trigger creation.
                if exc.stage == _STAGE_OPEN and errno_value == errno.ENOENT:
                    existed = False
                    try:
                        child = current.create_directory(
                            component,
                            mode=0o700,
                            label=component,
                            require_owner=False,
                        )
                    except DescriptorError as create_exc:
                        if create_exc.cause is not None and (
                            "cannot secure directory" in str(create_exc)
                        ):
                            raise CacheStorageError(
                                f"cannot secure XDG_CACHE_HOME {xdg}: "
                                f"{create_exc.cause}"
                            ) from create_exc
                        raise CacheStorageError(
                            f"cannot create XDG_CACHE_HOME component "
                            f"{component!r}: {_foundation_cause(create_exc)}"
                        ) from create_exc
                elif errno_value in (errno.ENOTDIR, errno.ELOOP):
                    raise CacheStorageError(
                        f"XDG_CACHE_HOME component {component!r} is not a "
                        "directory"
                    ) from exc
                else:
                    raise CacheStorageError(
                        f"cannot inspect XDG_CACHE_HOME component "
                        f"{component!r}: {_foundation_cause(exc)}"
                    ) from exc

            previous, current = current, child
            previous.close()
            if final and existed and not os.access(absolute, os.W_OK):
                raise CacheStorageError(f"XDG_CACHE_HOME is not writable: {xdg}")
    except BaseException as primary:
        _release_keeping_primary(current, primary)
        raise
    # The walk validated or created the final component; release it now so a
    # successful preparation never leaks the last XDG directory descriptor.
    current.close()


def resolve_effective_root(value: str | None, *, xdg_cache_home: str | None, home: Path) -> Path:
    """Select a local override or default root without filesystem effects."""
    if value is None:
        return resolve_default_root(xdg_cache_home, home=home)
    root = resolve_local_root(value, xdg_cache_home=xdg_cache_home, home=home)
    if root is None:  # defensive: a configured value cannot resolve to None
        raise CacheStorageError("configured cache root is missing")
    return root


def prepare_resolved_root(
    root: Path, *, ops: DescriptorOps | None = None
) -> Path:
    """Harden a previously resolved dedicated constructor cache root."""
    _harden_tree(_resolve_ops(ops), root)
    return root


def prepare_project_root(
    root: Path, *, ops: DescriptorOps | None = None
) -> Path:
    """Prepare only the shared root for project-scoped state.

    Project build retention must not inspect the independent runtime-artifact
    or versioning subtrees. The project-state resolver validates and creates
    only its own ``projects`` child after this root boundary is secured.
    """
    descriptor_ops = _resolve_ops(ops)
    _inspect_entry(descriptor_ops, root)
    _create_and_secure(descriptor_ops, root)
    return root


def prepare_default_root(
    xdg_cache_home: str | None,
    *,
    home: Path,
    ops: DescriptorOps | None = None,
) -> Path:
    """Resolve and harden the default constructor cache root.

    For a non-empty absolute ``XDG_CACHE_HOME`` a missing XDG directory
    is created with ``0700``, an existing one must be a writable
    directory (its mode is never changed), and a non-directory,
    symlinked, or unwritable XDG path fails without fallback.  Empty or
    non-absolute XDG values use ``~/.cache/docker-constructor`` under
    *home*.
    """
    descriptor_ops = _resolve_ops(ops)
    root = resolve_default_root(xdg_cache_home, home=home)
    if xdg_cache_home and os.path.isabs(xdg_cache_home):
        _prepare_explicit_xdg(descriptor_ops, _normalize(xdg_cache_home))
    _harden_tree(descriptor_ops, root)
    return root


def prepare_local_root(
    value: str | None,
    *,
    xdg_cache_home: str | None,
    home: Path,
    ops: DescriptorOps | None = None,
) -> Path:
    """Resolve and harden a dedicated local ``[cache].dir`` override."""
    descriptor_ops = _resolve_ops(ops)
    root = resolve_local_root(value, xdg_cache_home=xdg_cache_home, home=home)
    if root is None:
        raise CacheStorageError(
            "no local cache root configured; set [cache].dir to a dedicated "
            "absolute directory"
        )
    _harden_tree(descriptor_ops, root)
    return root
