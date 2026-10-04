"""Owner-private assembler cache namespace, locks, and staging.

The resolved constructor cache hosts an owner-private ``npm-environments``
subtree keyed by assembler identity.  Beneath each assembler namespace the
npm download cache is kept opaque and disposable, identity-lock files are
private ``0600`` regular files, and staging workspaces are created
owner-private ``0700`` and contained inside the namespace.  Every directory
is opened descriptor-relatively with ``O_DIRECTORY | O_NOFOLLOW`` so
symlinked control paths, non-directory entries, and foreign ownership are
rejected before any container execution or publication.  Pre-existing
ancestors of the cache root are never chmod-ed.

This module is a leaf: it imports only the shared error type and no
cache, transport, Docker, or CLI module.
"""

from __future__ import annotations

import os
import stat as _stat
from dataclasses import dataclass
from pathlib import Path

from .errors import LockedNpmError

NPM_ENVIRONMENTS_CHILD = "npm-environments"
ASSEMBLER_CHILD = "assembler"
NPM_CACHE_CHILD = "npm-cache"
LOCKS_CHILD = "locks"
STAGING_CHILD = "staging"
OUTPUTS_CHILD = "outputs"
INDEX_CHILD = "index"

_DIR_FLAGS = (
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
)
_HEX = frozenset("0123456789abcdef")


def _require_hex_digest(value: str, *, reason: str) -> None:
    """Require a 64-char lowercase hex digest token usable as a path name."""
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(c not in _HEX for c in value)
    ):
        raise LockedNpmError(
            reason, f"expected a 64-character hex digest, got {value!r}"
        )


def _require_entry_name(name: str, *, reason: str) -> None:
    """Require *name* to be a single, non-empty, non-special basename."""
    if (
        not name
        or name in (".", "..")
        or os.path.isabs(name)
        or "/" in name
        or "\\" in name
        or Path(name).name != name
    ):
        raise LockedNpmError(reason, f"unsafe entry name: {name!r}")


def _open_directory_no_follow(path: Path, *, check_owner: bool) -> int:
    """Open *path* no-follow, walking every component from the filesystem root.

    Symlinked or non-directory components are rejected.  When *check_owner*
    is true the final directory must be owned by the invoking effective UID.
    Returns the open directory descriptor (caller closes it).
    """
    absolute = os.path.abspath(str(path))
    components = [part for part in absolute.split(os.sep) if part]
    current_fd = os.open(os.sep, _DIR_FLAGS)
    try:
        for index, component in enumerate(components):
            final = index == len(components) - 1
            try:
                next_fd = os.open(component, _DIR_FLAGS, dir_fd=current_fd)
            except OSError as exc:
                raise LockedNpmError(
                    "unsafe_cache_path",
                    f"unsafe cache component {component!r} in {absolute!r} "
                    f"(symlink or non-directory): {exc}",
                ) from exc
            os.close(current_fd)
            current_fd = next_fd
            if final and check_owner:
                st = os.fstat(current_fd)
                if st.st_uid != os.geteuid():
                    raise LockedNpmError(
                        "unsafe_cache_path",
                        f"{path} is not owned by the invoking user; restore "
                        "ownership or remove the entry",
                    )
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _create_or_open_child(parent_fd: int, name: str, *, label: str | Path) -> int:
    """Create (``0700``) or open an owned directory beneath *parent_fd*.

    The child is opened with ``O_DIRECTORY | O_NOFOLLOW``, so a symlink or
    non-directory entry is rejected without following it.  Missing children
    are created with ``os.mkdir`` and an explicit ``0700`` mode; existing
    owned children are secured to ``0700`` with ``fchmod`` on the opened
    descriptor.  Returns the open child descriptor (caller closes it).
    """
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except OSError as exc:
            raise LockedNpmError(
                "unsafe_cache_path", f"cannot create cache directory {label!r}: {exc}"
            ) from exc
        try:
            fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
        except OSError as exc:
            raise LockedNpmError(
                "unsafe_cache_path", f"unsafe cache entry {label!r}: {exc}"
            ) from exc
    except OSError as exc:
        raise LockedNpmError(
            "unsafe_cache_path",
            f"unsafe cache entry {label!r} (symlink or non-directory): {exc}",
        ) from exc
    try:
        st = os.fstat(fd)
        if not _stat.S_ISDIR(st.st_mode):
            raise LockedNpmError(
                "unsafe_cache_path", f"{label} is not a directory; remove it"
            )
        if st.st_uid != os.geteuid():
            raise LockedNpmError(
                "unsafe_cache_path",
                f"{label} is not owned by the invoking user; restore "
                "ownership or remove the entry",
            )
        os.fchmod(fd, 0o700)
    except BaseException:
        os.close(fd)
        raise
    return fd


@dataclass(frozen=True)
class AssemblerNamespace:
    """Prepared owner-private storage paths for one assembler identity."""

    root: Path
    """``<cache-root>/npm-environments/assembler/<assembler-digest>``."""

    npm_cache: Path
    """Opaque, disposable npm download cache (``npm-cache``)."""

    locks: Path
    """Private input-identity coordination lock directory (``locks``)."""

    staging: Path
    """Owner-private staging workspace parent (``staging``)."""

    outputs: Path
    """Immutable assembled-output storage (``outputs``)."""

    index: Path
    """Non-authoritative input-identity lookup index (``index``)."""


def assembler_namespace_path(
    cache_root: str | Path, assembler_digest: str
) -> Path:
    """Return the lexical namespace path for *assembler_digest*.

    Pure and deterministic: no filesystem access.  The digest token is
    validated as a safe single path component.
    """
    _require_hex_digest(assembler_digest, reason="unsafe_cache_path")
    return (
        Path(cache_root)
        / NPM_ENVIRONMENTS_CHILD
        / ASSEMBLER_CHILD
        / assembler_digest
    )


def prepare_assembler_namespace(
    cache_root: str | Path, assembler_digest: str
) -> AssemblerNamespace:
    """Create (or secure) the owner-private assembler namespace.

    *cache_root* is the already-resolved constructor cache root.  It is
    opened no-follow and must be a directory owned by the invoking user; it
    is never chmod-ed, and no ancestor is ever chmod-ed.  The
    ``npm-environments/assembler/<digest>`` subtree and its ``npm-cache``,
    ``locks``, and ``staging`` children are created ``0700`` and secured to
    ``0700``.  Symlinked components, non-directory entries, and foreign
    ownership are rejected before any mutation outside the new subtree.
    """
    _require_hex_digest(assembler_digest, reason="unsafe_cache_path")
    root_fd = _open_directory_no_follow(Path(cache_root), check_owner=True)
    try:
        env_fd = _create_or_open_child(
            root_fd, NPM_ENVIRONMENTS_CHILD,
            label=Path(cache_root) / NPM_ENVIRONMENTS_CHILD,
        )
        try:
            asm_fd = _create_or_open_child(
                env_fd, ASSEMBLER_CHILD,
                label=Path(cache_root) / NPM_ENVIRONMENTS_CHILD / ASSEMBLER_CHILD,
            )
        finally:
            os.close(env_fd)
        namespace_root = (
            Path(cache_root) / NPM_ENVIRONMENTS_CHILD / ASSEMBLER_CHILD
            / assembler_digest
        )
        try:
            digest_fd = _create_or_open_child(
                asm_fd, assembler_digest, label=namespace_root
            )
        finally:
            os.close(asm_fd)
        try:
            npm_fd = _create_or_open_child(
                digest_fd, NPM_CACHE_CHILD, label=namespace_root / NPM_CACHE_CHILD
            )
            os.close(npm_fd)
            locks_fd = _create_or_open_child(
                digest_fd, LOCKS_CHILD, label=namespace_root / LOCKS_CHILD
            )
            os.close(locks_fd)
            staging_fd = _create_or_open_child(
                digest_fd, STAGING_CHILD, label=namespace_root / STAGING_CHILD
            )
            os.close(staging_fd)
            outputs_fd = _create_or_open_child(
                digest_fd, OUTPUTS_CHILD, label=namespace_root / OUTPUTS_CHILD
            )
            os.close(outputs_fd)
            index_fd = _create_or_open_child(
                digest_fd, INDEX_CHILD, label=namespace_root / INDEX_CHILD
            )
            os.close(index_fd)
        finally:
            os.close(digest_fd)
    finally:
        os.close(root_fd)

    return AssemblerNamespace(
        root=namespace_root,
        npm_cache=namespace_root / NPM_CACHE_CHILD,
        locks=namespace_root / LOCKS_CHILD,
        staging=namespace_root / STAGING_CHILD,
        outputs=namespace_root / OUTPUTS_CHILD,
        index=namespace_root / INDEX_CHILD,
    )


def prepare_staging_workspace(
    namespace: AssemblerNamespace, name: str
) -> Path:
    """Create a fresh owner-private ``0700`` staging workspace.

    *name* must be a single safe basename and must not already exist, so a
    symlink or cancellation residue is rejected rather than reused.  The
    workspace is created descriptor-relatively beneath ``namespace.staging``
    and therefore stays contained inside the namespace root.
    """
    _require_entry_name(name, reason="unsafe_staging_path")
    staging_fd = _open_directory_no_follow(namespace.staging, check_owner=True)
    try:
        try:
            os.mkdir(name, 0o700, dir_fd=staging_fd)
        except FileExistsError:
            raise LockedNpmError(
                "unsafe_staging_path",
                f"staging workspace {name!r} already exists; clean residue "
                "before reuse",
            ) from None
        except OSError as exc:
            raise LockedNpmError(
                "unsafe_staging_path",
                f"cannot create staging workspace {name!r}: {exc}",
            ) from exc
        try:
            fd = os.open(name, _DIR_FLAGS, dir_fd=staging_fd)
        except OSError as exc:
            raise LockedNpmError(
                "unsafe_staging_path", f"unsafe staging workspace {name!r}: {exc}"
            ) from exc
        try:
            os.fchmod(fd, 0o700)
        finally:
            os.close(fd)
    finally:
        os.close(staging_fd)
    return namespace.staging / name


def _remove_entry(parent_fd: int, name: str) -> None:
    """Remove one filesystem entry beneath *parent_fd* without following
    symlinks, recursing into real directories."""
    try:
        st = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if _stat.S_ISDIR(st.st_mode) and not _stat.S_ISLNK(st.st_mode):
        try:
            fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
        except OSError as exc:
            raise LockedNpmError(
                "unsafe_staging_path",
                f"cannot open staging entry {name!r} for removal: {exc}",
            ) from exc
        try:
            for child in os.listdir(fd):
                _remove_entry(fd, child)
        finally:
            os.close(fd)
        os.rmdir(name, dir_fd=parent_fd)
    else:
        os.unlink(name, dir_fd=parent_fd)


def remove_staging_workspace(namespace: AssemblerNamespace, name: str) -> None:
    """Remove a staging workspace (and its residue) without following
    symlinks.

    *name* must be a single safe basename; removal is descriptor-relative
    beneath ``namespace.staging`` and never follows a symlinked entry.  A
    missing workspace is a no-op so cancellation cleanup is idempotent.
    """
    _require_entry_name(name, reason="unsafe_staging_path")
    staging_fd = _open_directory_no_follow(namespace.staging, check_owner=True)
    try:
        _remove_entry(staging_fd, name)
    finally:
        os.close(staging_fd)
