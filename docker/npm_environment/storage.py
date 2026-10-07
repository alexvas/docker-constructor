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

Directory ownership, secure absolute-path walking, parent/child handoff, and
at-most-once release are delegated to the lightweight
``docker.filesystem.descriptors`` foundation.  This module keeps the npm
namespace layout, staging exclusivity, recursive removal, absence policy, and
``LockedNpmError`` mapping.  It is a leaf: it imports the lightweight
foundation submodules, the shared error type, and no cache, transport,
Docker, CLI, or ``docker.transactions`` module.
"""

from __future__ import annotations

import os
import stat as _stat
from dataclasses import dataclass
from pathlib import Path

from docker.filesystem.descriptors import (
    DescriptorError,
    DirectoryDescriptor,
    _STAGE_CREATE,
    _STAGE_REOPEN,
)
from docker.filesystem.operations import DescriptorOps, PosixDescriptorOps

from .errors import LockedNpmError

NPM_ENVIRONMENTS_CHILD = "npm-environments"
ASSEMBLER_CHILD = "assembler"
NPM_CACHE_CHILD = "npm-cache"
LOCKS_CHILD = "locks"
STAGING_CHILD = "staging"
OUTPUTS_CHILD = "outputs"
INDEX_CHILD = "index"

_HEX = frozenset("0123456789abcdef")

#: Production descriptor backend.  Tests inject an alternative ``DescriptorOps``
#: through the explicit ``ops`` keyword on each public entry point.
_PRODUCTION_OPS: DescriptorOps = PosixDescriptorOps()

_NAMESPACE_CHILDREN = (
    NPM_CACHE_CHILD,
    LOCKS_CHILD,
    STAGING_CHILD,
    OUTPUTS_CHILD,
    INDEX_CHILD,
)


def _resolve_ops(ops: DescriptorOps | None) -> DescriptorOps:
    """Return the injected backend or the production POSIX adapter."""
    return _PRODUCTION_OPS if ops is None else ops


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


def _foundation_cause(exc: DescriptorError) -> str:
    """Render a foundation failure's raw cause for domain diagnostics."""
    return str(exc.cause) if exc.cause is not None else str(exc)


def _failing_component(exc: DescriptorError) -> str | None:
    """Recover the failing path component from a foundation open failure.

    ``PosixDescriptorOps.openat`` opens one component relative to its parent,
    so the raw ``OSError`` records the failing component name as ``filename``.
    """
    filename = getattr(exc.cause, "filename", None)
    return filename if isinstance(filename, str) and filename else None


def _open_failure_detail(path: Path, absolute: str, exc: DescriptorError) -> str:
    """Domain detail text for a foundation secure-walk failure.

    Reproduces the pre-migration assembler diagnostic exactly: a failed
    component names the component and the absolute path, while an
    ownership/type rejection names the caller-supplied *path*.
    """
    cause = exc.cause
    message = str(exc)
    if cause is not None:
        component = _failing_component(exc)
        if component is not None:
            return (
                f"unsafe cache component {component!r} in {absolute!r} "
                f"(symlink or non-directory): {cause}"
            )
        return (
            f"unsafe cache component in {absolute!r} (symlink or "
            f"non-directory): {cause}"
        )
    if "not owned by the invoking user" in message:
        return (
            f"{path} is not owned by the invoking user; restore ownership "
            "or remove the entry"
        )
    if "not a directory" in message:
        return f"{path} is not a directory; remove it"
    return f"unsafe cache path {absolute!r}: {exc}"


def _child_failure_detail(label: str | Path, exc: DescriptorError) -> str:
    """Domain detail text for a foundation child open/create failure.

    The foundation's generic stage records which operation failed, so creation,
    the initial open of an existing entry, and the reopen that follows a
    successful create keep their historical wording without inspecting the
    raw ``OSError`` (a create-stage ``FileExistsError`` and a reopen-stage
    ``FileExistsError`` would otherwise be indistinguishable).
    """
    cause = exc.cause
    message = str(exc)
    if cause is not None and exc.stage == _STAGE_CREATE:
        return f"cannot create cache directory {label!r}: {cause}"
    if cause is not None and exc.stage == _STAGE_REOPEN:
        return f"unsafe cache entry {label!r}: {cause}"
    if cause is not None:
        return f"unsafe cache entry {label!r} (symlink or non-directory): {cause}"
    if "not owned by the invoking user" in message:
        return (
            f"{label} is not owned by the invoking user; restore ownership "
            "or remove the entry"
        )
    if "not a directory" in message:
        return f"{label} is not a directory; remove it"
    return f"unsafe cache entry {label!r}: {exc}"


def _open_directory_no_follow(
    ops: DescriptorOps, path: Path, *, check_owner: bool
) -> DirectoryDescriptor:
    """Open *path* no-follow through the descriptor foundation.

    Component walking, parent/child handoff, and at-most-once release are
    owned by ``DirectoryDescriptor.open_secure_path``.  A foundation
    open/validation failure is mapped back to the assembler's
    ``unsafe_cache_path`` domain error; a raw descriptor-handoff close failure
    keeps its raw ``OSError`` identity and propagates unchanged.
    """
    absolute = os.path.abspath(str(path))
    try:
        return DirectoryDescriptor.open_secure_path(
            ops, absolute, label=absolute, require_owner=check_owner
        )
    except DescriptorError as exc:
        raise LockedNpmError(
            "unsafe_cache_path", _open_failure_detail(path, absolute, exc)
        ) from exc


def _create_or_open_child(
    parent: DirectoryDescriptor, name: str, *, label: str | Path
) -> DirectoryDescriptor:
    """Create (``0700``) or open an owned directory beneath *parent*.

    ``DirectoryDescriptor.open_or_create_directory`` performs the no-follow
    open, missing-child creation with an explicit ``0700`` mode, and
    post-open validation (directory type, effective owner, secured mode).  A
    foundation failure is mapped back to the assembler's ``unsafe_cache_path``
    domain error without changing the namespace sequencing.
    """
    try:
        return parent.open_or_create_directory(
            name, mode=0o700, label=str(label), require_owner=True
        )
    except DescriptorError as exc:
        raise LockedNpmError(
            "unsafe_cache_path", _child_failure_detail(label, exc)
        ) from exc


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
    cache_root: str | Path,
    assembler_digest: str,
    *,
    ops: DescriptorOps | None = None,
) -> AssemblerNamespace:
    """Create (or secure) the owner-private assembler namespace.

    *cache_root* is the already-resolved constructor cache root.  It is
    opened no-follow and must be a directory owned by the invoking user; it
    is never chmod-ed, and no ancestor is ever chmod-ed.  The
    ``npm-environments/assembler/<digest>`` subtree and its ``npm-cache``,
    ``locks``, and ``staging`` children are created ``0700`` and secured to
    ``0700``.  Symlinked components, non-directory entries, and foreign
    ownership are rejected before any mutation outside the new subtree.

    Descriptor ownership transfers to nested capabilities; each capability is
    released at most once, and an ordinary close failure stays secondary to
    any active operation failure.  The namespace sequencing and returned paths
    are unchanged from the raw-descriptor implementation.
    """
    _require_hex_digest(assembler_digest, reason="unsafe_cache_path")
    descriptor_ops = _resolve_ops(ops)
    cache_root_path = Path(cache_root)
    root = _open_directory_no_follow(
        descriptor_ops, cache_root_path, check_owner=True
    )
    namespace_root = (
        cache_root_path
        / NPM_ENVIRONMENTS_CHILD
        / ASSEMBLER_CHILD
        / assembler_digest
    )
    with root:
        env = _create_or_open_child(
            root,
            NPM_ENVIRONMENTS_CHILD,
            label=cache_root_path / NPM_ENVIRONMENTS_CHILD,
        )
        with env:
            assembler = _create_or_open_child(
                env,
                ASSEMBLER_CHILD,
                label=cache_root_path / NPM_ENVIRONMENTS_CHILD / ASSEMBLER_CHILD,
            )
            with assembler:
                digest = _create_or_open_child(
                    assembler, assembler_digest, label=namespace_root
                )
                with digest:
                    for child in _NAMESPACE_CHILDREN:
                        handle = _create_or_open_child(
                            digest, child, label=namespace_root / child
                        )
                        handle.close()

    return AssemblerNamespace(
        root=namespace_root,
        npm_cache=namespace_root / NPM_CACHE_CHILD,
        locks=namespace_root / LOCKS_CHILD,
        staging=namespace_root / STAGING_CHILD,
        outputs=namespace_root / OUTPUTS_CHILD,
        index=namespace_root / INDEX_CHILD,
    )


def _create_staging_directory(
    parent: DirectoryDescriptor, name: str, *, label: Path
) -> DirectoryDescriptor:
    """Exclusively create the ``0700`` staging workspace beneath *parent*.

    A pre-existing entry (including symlink or cancellation residue) is
    rejected rather than reused.  The exclusive create is owned by
    ``DirectoryDescriptor.create_directory``; its generic stage distinguishes
    the exclusive ``mkdirat`` from the open that follows it, so each operation
    keeps its historical ``unsafe_staging_path`` wording while chaining the
    original foundation failure.
    """
    try:
        return parent.create_directory(
            name, mode=0o700, label=str(label), require_owner=True
        )
    except DescriptorError as exc:
        cause = exc.cause
        if exc.stage == _STAGE_CREATE and isinstance(cause, FileExistsError):
            raise LockedNpmError(
                "unsafe_staging_path",
                f"staging workspace {name!r} already exists; clean residue "
                "before reuse",
            ) from exc
        if exc.stage == _STAGE_REOPEN:
            raise LockedNpmError(
                "unsafe_staging_path",
                f"unsafe staging workspace {name!r}: {_foundation_cause(exc)}",
            ) from exc
        raise LockedNpmError(
            "unsafe_staging_path",
            f"cannot create staging workspace {name!r}: {_foundation_cause(exc)}",
        ) from exc


def prepare_staging_workspace(
    namespace: AssemblerNamespace,
    name: str,
    *,
    ops: DescriptorOps | None = None,
) -> Path:
    """Create a fresh owner-private ``0700`` staging workspace.

    *name* must be a single safe basename and must not already exist, so a
    symlink or cancellation residue is rejected rather than reused.  The
    workspace is created descriptor-relatively beneath ``namespace.staging``
    and therefore stays contained inside the namespace root.

    The staging parent and the created workspace are nested capabilities; a
    failed creation leaves the workspace creation failure primary while an
    ordinary release failure remains secondary, and both descriptors are
    released at most once.
    """
    _require_entry_name(name, reason="unsafe_staging_path")
    descriptor_ops = _resolve_ops(ops)
    staging = _open_directory_no_follow(
        descriptor_ops, namespace.staging, check_owner=True
    )
    with staging:
        workspace = _create_staging_directory(
            staging, name, label=namespace.staging / name
        )
        workspace.close()
    return namespace.staging / name


def _open_child_for_removal(
    parent: DirectoryDescriptor, name: str
) -> DirectoryDescriptor:
    """Open one no-follow child directory for recursive removal.

    The removal walk does not apply the namespace owner policy to descendants;
    it only requires a real directory.  A foundation open failure is mapped to
    the assembler's ``unsafe_staging_path`` domain error.
    """
    try:
        return parent.open_directory(name, label=name, require_owner=False)
    except DescriptorError as exc:
        raise LockedNpmError(
            "unsafe_staging_path",
            f"cannot open staging entry {name!r} for removal: "
            f"{_foundation_cause(exc)}",
        ) from exc


def _remove_entry(parent: DirectoryDescriptor, name: str) -> None:
    """Remove one filesystem entry beneath *parent* without following
    symlinks, recursing into real directories.

    Domain-owned absence policy is unchanged: a missing entry is a no-op.
    Recursion stays in this module and composes the capability's single
    basename operations.  A traversal or removal failure remains primary over
    an ordinary child-directory release failure, and each opened directory is
    released at most once.
    """
    try:
        info = parent.stat_child(name, follow_symlinks=False)
    except FileNotFoundError:
        return
    if _stat.S_ISDIR(info.st_mode) and not _stat.S_ISLNK(info.st_mode):
        child = _open_child_for_removal(parent, name)
        with child:
            for grandchild in child.list_names():
                _remove_entry(child, grandchild)
        parent.remove_child_directory(name)
    else:
        parent.unlink_child(name)


def remove_staging_workspace(
    namespace: AssemblerNamespace,
    name: str,
    *,
    ops: DescriptorOps | None = None,
) -> None:
    """Remove a staging workspace (and its residue) without following
    symlinks.

    *name* must be a single safe basename; removal is descriptor-relative
    beneath ``namespace.staging`` and never follows a symlinked entry.  A
    missing workspace is a no-op so cancellation cleanup is idempotent.  The
    staging parent is released at most once and stays secondary to a removal
    failure.
    """
    _require_entry_name(name, reason="unsafe_staging_path")
    descriptor_ops = _resolve_ops(ops)
    staging = _open_directory_no_follow(
        descriptor_ops, namespace.staging, check_owner=True
    )
    with staging:
        _remove_entry(staging, name)
