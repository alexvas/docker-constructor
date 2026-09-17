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

The module deliberately imports no CLI, launcher, transport, HTTP
response-cache, artifact-verification, or provider module so it remains
an acyclic leaf that those consumers can depend on safely.
"""
from __future__ import annotations

import os
import stat as _stat
from pathlib import Path

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

_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)

_DESCENDANT_PARTS = (
    ("versioning",),
    ("runtime-artifacts",),
    ("runtime-artifacts", "blobs"),
    ("runtime-artifacts", "locks"),
    ("runtime-artifacts", "tmp"),
)


def _open_parent_fd(path: Path, *, create_missing: bool) -> int | None:
    """Open the parent directory of *path* without following any symlink.

    Missing lead-up components are created descriptor-relatively with
    ``0700`` when *create_missing* is true.  Pre-existing components are
    never chmod-ed.  Returns the descriptor of the parent, or ``None``
    when the lead-up does not exist and *create_missing* is false.
    """
    absolute = os.path.abspath(path)
    parent = os.path.dirname(absolute)
    current_fd = os.open(os.sep, _DIR_FLAGS)
    try:
        for component in (part for part in parent.split(os.sep) if part):
            try:
                value = os.stat(component, dir_fd=current_fd, follow_symlinks=False)
            except FileNotFoundError:
                if not create_missing:
                    os.close(current_fd)
                    return None
                try:
                    os.mkdir(component, 0o700, dir_fd=current_fd)
                except OSError as exc:
                    raise CacheStorageError(
                        f"cannot create parent component {component!r} in "
                        f"{parent!r}: {exc}"
                    ) from exc
                value = os.stat(component, dir_fd=current_fd, follow_symlinks=False)
            except OSError as exc:
                raise CacheStorageError(
                    f"cannot access parent component {component!r} in "
                    f"{parent!r}: {exc}; restore ownership or remove the entry"
                ) from exc
            if _stat.S_ISLNK(value.st_mode) or not _stat.S_ISDIR(value.st_mode):
                raise CacheStorageError(
                    f"unsafe parent component {component!r} in {parent!r}; "
                    "remove the entry or restore ownership"
                )
            try:
                next_fd = os.open(component, _DIR_FLAGS, dir_fd=current_fd)
            except OSError as exc:
                raise CacheStorageError(
                    f"cannot open parent component {component!r} in "
                    f"{parent!r}: {exc}"
                ) from exc
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _open_entry_no_follow(parent_fd: int, path: Path) -> int | None:
    """Open *path* relative to *parent_fd* with no-follow directory checks.

    Returns the opened descriptor, or ``None`` when the entry does not
    exist.  Raises :class:`CacheStorageError` for symlinks, non-directory
    entries, foreign ownership, or entries the process cannot access.
    """
    name = os.path.basename(os.path.abspath(path))
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    except PermissionError as exc:
        raise CacheStorageError(
            f"cannot access {path}; restore ownership or remove it ({exc})"
        ) from exc
    except OSError as exc:
        raise CacheStorageError(
            f"unsafe cache entry at {path} (symlink or non-directory); "
            f"remove the entry ({exc})"
        ) from exc
    try:
        value = os.fstat(fd)
        if not _stat.S_ISDIR(value.st_mode):
            raise CacheStorageError(f"{path} is not a directory; remove it")
        if value.st_uid != os.geteuid():
            raise CacheStorageError(
                f"{path} is not owned by the invoking user; "
                "restore ownership or remove the stale cache subtree"
            )
    except BaseException:
        os.close(fd)
        raise
    return fd


def _inspect_entry(path: Path) -> None:
    """Validate an existing entry without mutating anything."""
    parent_fd = _open_parent_fd(path, create_missing=False)
    if parent_fd is None:
        return
    try:
        fd = _open_entry_no_follow(parent_fd, path)
        if fd is not None:
            os.close(fd)
    finally:
        os.close(parent_fd)


def _create_and_secure(path: Path) -> None:
    """Create (if missing) and secure a single directory to ``0700``."""
    parent_fd = _open_parent_fd(path, create_missing=True)
    if parent_fd is None:
        raise CacheStorageError(f"cannot create parent directory for {path}")
    try:
        fd = _open_entry_no_follow(parent_fd, path)
        if fd is None:
            name = os.path.basename(os.path.abspath(path))
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            fd = _open_entry_no_follow(parent_fd, path)
        if fd is None:
            raise CacheStorageError(f"cannot open cache directory {path}")
        try:
            os.fchmod(fd, 0o700)
        except (OSError, PermissionError) as exc:
            raise CacheStorageError(
                f"cannot secure {path}; restore ownership or remove it ({exc})"
            ) from exc
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)


def _harden_tree(root: Path) -> None:
    """Inspect every existing root/descendant, then create and secure all.

    All existing entries are validated before any mutation so that an
    unsafe root or descendant is rejected while leaving prior modes and
    absent descendants untouched.
    """
    _inspect_entry(root)
    for parts in _DESCENDANT_PARTS:
        _inspect_entry(root.joinpath(*parts))

    _create_and_secure(root)
    for parts in _DESCENDANT_PARTS:
        _create_and_secure(root.joinpath(*parts))


def _open_directory_for_write(directory: Path) -> int:
    """Open a prepared cache directory no-follow and require privacy.

    The directory must already exist, be a directory, be owned by the
    invoking user, and be mode ``0700``.  It is never chmod-ed — an
    unprepared or non-private directory is rejected so DiskCache can
    fail closed instead of hardening an arbitrary caller-supplied path.
    """
    parent_fd = _open_parent_fd(directory, create_missing=False)
    if parent_fd is None:
        raise CacheStorageError(
            f"cache directory {directory} does not exist; prepare the "
            "constructor cache root before writing cache entries"
        )
    try:
        fd = _open_entry_no_follow(parent_fd, directory)
    finally:
        os.close(parent_fd)
    if fd is None:
        raise CacheStorageError(
            f"cache directory {directory} does not exist; prepare the "
            "constructor cache root before writing cache entries"
        )
    try:
        value = os.fstat(fd)
        if _stat.S_IMODE(value.st_mode) != 0o700:
            raise CacheStorageError(
                f"cache directory {directory} is not private (mode "
                f"{_stat.S_IMODE(value.st_mode):04o}); the constructor "
                "cache directory must be mode 0700 and owned by the "
                "invoking user"
            )
    except BaseException:
        os.close(fd)
        raise
    return fd


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


def open_private_entry(directory: Path, name: str) -> int:
    """Open (or create) a private ``0600`` file beneath a prepared directory.

    *directory* must already exist and be owned by the invoking user; it
    is opened no-follow and validated with ``fstat``, and is never
    chmod-ed.  *name* must be a single basename; it is then opened
    no-follow beneath that descriptor with ``O_WRONLY | O_CREAT |
    O_TRUNC`` and clamped to ``0600`` on its descriptor.  Returns the
    writable descriptor (the caller owns closing it).  Raises
    :class:`CacheStorageError` when the directory is missing, symlinked,
    foreign-owned, the name is unsafe, or the entry cannot be opened or
    secured.
    """
    _require_entry_name(name)
    dir_fd = _open_directory_for_write(directory)
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(name, flags, 0o600, dir_fd=dir_fd)
        except OSError as exc:
            raise CacheStorageError(
                f"cannot open cache entry {Path(directory) / name}: {exc}"
            ) from exc
        try:
            os.fchmod(fd, 0o600)
        except (OSError, PermissionError) as exc:
            os.close(fd)
            raise CacheStorageError(
                f"cannot secure cache entry {Path(directory) / name}: {exc}"
            ) from exc
        return fd
    finally:
        os.close(dir_fd)


def publish_private_entry(
    directory: Path, tmp_name: str, final_name: str,
) -> None:
    """Atomically publish *tmp_name* as *final_name* beneath *directory*.

    Both names must be single basenames.  The prepared directory is
    reopened no-follow and the rename happens descriptor-relatively
    (``os.replace`` with ``src_dir_fd`` and ``dst_dir_fd``), never by
    reopening the path.  Failures map to :class:`CacheStorageError`.
    """
    _require_entry_name(tmp_name)
    _require_entry_name(final_name)
    dir_fd = _open_directory_for_write(directory)
    try:
        try:
            os.replace(
                tmp_name, final_name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd,
            )
        except OSError as exc:
            raise CacheStorageError(
                f"cannot publish cache entry {Path(directory) / final_name}: {exc}"
            ) from exc
    finally:
        os.close(dir_fd)


def _prepare_explicit_xdg(xdg: Path) -> None:
    """Create or validate an explicit ``XDG_CACHE_HOME`` safely.

    Walks every component from the filesystem root using descriptor-
    relative, no-follow operations.  Symlinked or non-directory
    components are rejected.  A missing component is created with
    ``0700`` and secured with ``fchmod`` on its opened descriptor; an
    existing component is reopened with ``O_DIRECTORY | O_NOFOLLOW`` and
    validated with ``fstat`` without being chmod-ed.  The final existing
    directory must also be writable.
    """
    absolute = os.path.abspath(xdg)
    components = [part for part in absolute.split(os.sep) if part]
    if not components:
        # The XDG path is the filesystem root itself.
        if not _stat.S_ISDIR(os.stat(absolute).st_mode) or not os.access(absolute, os.W_OK):
            raise CacheStorageError(f"XDG_CACHE_HOME is not a writable directory: {xdg}")
        return

    current_fd = os.open(os.sep, _DIR_FLAGS)
    try:
        for index, component in enumerate(components):
            final = index == len(components) - 1
            try:
                value = os.stat(component, dir_fd=current_fd, follow_symlinks=False)
                existed = True
            except FileNotFoundError:
                existed = False
            except OSError as exc:
                raise CacheStorageError(
                    f"cannot inspect XDG_CACHE_HOME component {component!r}: {exc}"
                ) from exc

            if existed and (
                _stat.S_ISLNK(value.st_mode) or not _stat.S_ISDIR(value.st_mode)
            ):
                raise CacheStorageError(
                    f"XDG_CACHE_HOME component {component!r} is not a directory"
                )

            if not existed:
                try:
                    os.mkdir(component, 0o700, dir_fd=current_fd)
                except OSError as exc:
                    raise CacheStorageError(
                        f"cannot create XDG_CACHE_HOME component {component!r}: {exc}"
                    ) from exc

            try:
                fd = os.open(component, _DIR_FLAGS, dir_fd=current_fd)
            except OSError as exc:
                raise CacheStorageError(
                    f"cannot open XDG_CACHE_HOME component {component!r}: {exc}"
                ) from exc
            try:
                st = os.fstat(fd)
                if not _stat.S_ISDIR(st.st_mode):
                    raise CacheStorageError(
                        f"XDG_CACHE_HOME component {component!r} is not a directory"
                    )
                if not existed:
                    try:
                        os.fchmod(fd, 0o700)
                    except (OSError, PermissionError) as exc:
                        raise CacheStorageError(
                            f"cannot secure XDG_CACHE_HOME {xdg}: {exc}"
                        ) from exc
            except BaseException:
                os.close(fd)
                raise

            if final and existed and not os.access(absolute, os.W_OK):
                raise CacheStorageError(f"XDG_CACHE_HOME is not writable: {xdg}")

            os.close(current_fd)
            current_fd = fd
    finally:
        os.close(current_fd)


def resolve_effective_root(value: str | None, *, xdg_cache_home: str | None, home: Path) -> Path:
    """Select a local override or default root without filesystem effects."""
    if value is None:
        return resolve_default_root(xdg_cache_home, home=home)
    root = resolve_local_root(value, xdg_cache_home=xdg_cache_home, home=home)
    if root is None:  # defensive: a configured value cannot resolve to None
        raise CacheStorageError("configured cache root is missing")
    return root


def prepare_resolved_root(root: Path) -> Path:
    """Harden a previously resolved dedicated constructor cache root."""
    _harden_tree(root)
    return root


def prepare_project_root(root: Path) -> Path:
    """Prepare only the shared root for project-scoped state.

    Project build retention must not inspect the independent runtime-artifact
    or versioning subtrees. The project-state resolver validates and creates
    only its own ``projects`` child after this root boundary is secured.
    """
    _inspect_entry(root)
    _create_and_secure(root)
    return root


def prepare_default_root(xdg_cache_home: str | None, *, home: Path) -> Path:
    """Resolve and harden the default constructor cache root.

    For a non-empty absolute ``XDG_CACHE_HOME`` a missing XDG directory
    is created with ``0700``, an existing one must be a writable
    directory (its mode is never changed), and a non-directory,
    symlinked, or unwritable XDG path fails without fallback.  Empty or
    non-absolute XDG values use ``~/.cache/docker-constructor`` under
    *home*.
    """
    root = resolve_default_root(xdg_cache_home, home=home)
    if xdg_cache_home and os.path.isabs(xdg_cache_home):
        _prepare_explicit_xdg(_normalize(xdg_cache_home))
    _harden_tree(root)
    return root


def prepare_local_root(
    value: str | None,
    *,
    xdg_cache_home: str | None,
    home: Path,
) -> Path:
    """Resolve and harden a dedicated local ``[cache].dir`` override."""
    root = resolve_local_root(value, xdg_cache_home=xdg_cache_home, home=home)
    if root is None:
        raise CacheStorageError(
            "no local cache root configured; set [cache].dir to a dedicated "
            "absolute directory"
        )
    _harden_tree(root)
    return root
