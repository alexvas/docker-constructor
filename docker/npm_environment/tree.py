"""Canonical hashed tree manifest and no-follow verification.

``build_tree_manifest`` deterministically describes every file, directory,
and contained symlink beneath a tree root in canonical path order, hashing
each regular file with SHA-256.  ``verify_tree`` re-validates a stored
manifest against the live filesystem without ever following a symlink and
detects every corruption class: extra, missing, ordering, type, hash,
permission, ownership, and escaping symlink.  The canonical tree digest
covers only content-bearing fields (path, kind, file hash, symlink target),
so it is independent of ownership and permission metadata.

Neither primitive assumes a published output identity or an
assembler-evidence digest; those remain Phase 5 contracts.

Directory ownership, no-follow child traversal, and primary-preserving
at-most-once release are delegated to the lightweight
``docker.filesystem.descriptors`` foundation.  This module keeps the
canonical path layout, recursive traversal, manifest bytes, containment
rules, and ``LockedNpmError`` mapping.  Regular-file hashing stays a raw
POSIX boundary because the foundation deliberately owns no regular-file
authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import stat as _stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from docker.filesystem.descriptors import (
    _STAGE_VALIDATE,
    DescriptorError,
    DirectoryDescriptor,
    OwnedDescriptor,
)
from docker.filesystem.operations import DescriptorOps, PosixDescriptorOps

from .errors import LockedNpmError

_NOFOLLOW_RDONLY = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)

#: Production descriptor backend.  Tests inject an alternative ``DescriptorOps``
#: through the explicit ``ops`` keyword on each public entry point.
_PRODUCTION_OPS: DescriptorOps = PosixDescriptorOps()


def _resolve_ops(ops: DescriptorOps | None) -> DescriptorOps:
    """Return the injected backend or the production POSIX adapter."""
    return _PRODUCTION_OPS if ops is None else ops


def _foundation_cause(exc: DescriptorError) -> str:
    """Render a foundation failure's raw cause for domain diagnostics."""
    return str(exc.cause) if exc.cause is not None else str(exc)


def _root_is_not_a_directory(exc: DescriptorError) -> bool:
    """Return True when the foundation rejected the root as a non-directory.

    Only a genuine validation-stage type rejection -- an adopted descriptor
    that the foundation proved is not a directory -- selects the historical
    domain wording.  Open-stage failures, including the raw no-follow
    ``ENOTDIR`` reported for a regular file or a symlink, keep the operational
    ``unsafe tree root`` diagnostic with the original ``OSError`` reachable
    through ``DescriptorError.cause``.  No errno inspection or filesystem
    probe happens here.
    """
    return exc.stage == _STAGE_VALIDATE and "is not a directory" in str(exc)


_KIND_FILE = "file"
_KIND_DIRECTORY = "directory"
_KIND_SYMLINK = "symlink"
_KIND_SPECIAL = "special"


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _contained_target(rel_path: str, target: str) -> bool:
    """Return True when a symlink target resolves inside the tree root."""
    if not target or os.path.isabs(target) or "\\" in target:
        return False
    resolved = posixpath.normpath(
        posixpath.join(posixpath.dirname(rel_path), target)
    )
    return not (resolved == ".." or resolved.startswith("../"))


@dataclass(frozen=True, order=True)
class TreeEntry:
    """One canonical entry in a tree manifest."""

    path: str
    """POSIX relative path beneath the tree root."""

    kind: str
    """``"file"``, ``"directory"``, or ``"symlink"``."""

    digest: str
    """SHA-256 hex for files, empty otherwise."""

    target: str
    """Raw symlink target for symlinks, empty otherwise."""

    mode: int
    """Permission bits (``stat.S_IMODE``)."""

    uid: int
    """Owner UID."""

    gid: int
    """Owner GID."""


@dataclass(frozen=True)
class TreeManifest:
    """Immutable canonical tree manifest and its content digest."""

    entries: tuple[TreeEntry, ...]
    """Entries in canonical path order."""

    digest: str
    """Canonical SHA-256 over content fields only (no mode/ownership)."""


@dataclass(frozen=True)
class _Found:
    path: str
    kind: str
    digest: str
    target: str
    mode: int
    uid: int
    gid: int


def canonical_tree_digest(entries: Iterable[TreeEntry]) -> str:
    """Return the canonical SHA-256 digest over sorted content fields."""
    ordered = sorted(entries)
    payload = _canonical_json(
        [
            {
                "path": e.path,
                "kind": e.kind,
                "digest": e.digest,
                "target": e.target,
            }
            for e in ordered
        ]
    ).encode("utf-8")
    return _sha256_hex(payload)


_ENTRY_FIELDS = frozenset({"path", "kind", "digest", "target", "mode", "uid", "gid"})


def _entry_to_jsonable(entry: TreeEntry) -> dict:
    return {
        "path": entry.path,
        "kind": entry.kind,
        "digest": entry.digest,
        "target": entry.target,
        "mode": entry.mode,
        "uid": entry.uid,
        "gid": entry.gid,
    }


def _entry_from_jsonable(value: object, context: str) -> TreeEntry:
    if not isinstance(value, dict):
        raise LockedNpmError("tree_manifest_malformed", f"{context}: not an object")
    extra = sorted(set(value) - _ENTRY_FIELDS)
    if extra:
        raise LockedNpmError(
            "tree_manifest_malformed", f"{context}: unexpected field(s) {extra}"
        )
    for key in _ENTRY_FIELDS:
        if key not in value:
            raise LockedNpmError(
                "tree_manifest_malformed", f"{context}: missing field {key!r}"
            )
    if not isinstance(value["path"], str) or not isinstance(value["kind"], str):
        raise LockedNpmError("tree_manifest_malformed", f"{context}: bad path/kind")
    if not isinstance(value["digest"], str) or not isinstance(value["target"], str):
        raise LockedNpmError("tree_manifest_malformed", f"{context}: bad digest/target")
    for key in ("mode", "uid", "gid"):
        if not isinstance(value[key], int) or isinstance(value[key], bool):
            raise LockedNpmError(
                "tree_manifest_malformed", f"{context}: {key!r} must be an integer"
            )
    return TreeEntry(
        path=value["path"],
        kind=value["kind"],
        digest=value["digest"],
        target=value["target"],
        mode=value["mode"],
        uid=value["uid"],
        gid=value["gid"],
    )


def serialize_manifest(manifest: TreeManifest) -> bytes:
    """Return the canonical, deterministic tree-manifest bytes."""
    payload = {
        "digest": manifest.digest,
        "entries": [_entry_to_jsonable(e) for e in manifest.entries],
    }
    return _canonical_json(payload).encode("utf-8")


def parse_manifest(data: bytes) -> TreeManifest:
    """Parse and verify a serialized tree manifest, rejecting substitution."""
    try:
        text = data.decode("utf-8")
        raw = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LockedNpmError(
            "tree_manifest_malformed", "manifest is not valid UTF-8 JSON"
        ) from exc
    if not isinstance(raw, dict):
        raise LockedNpmError("tree_manifest_malformed", "manifest is not an object")
    extra = sorted(set(raw) - {"digest", "entries"})
    if extra:
        raise LockedNpmError(
            "tree_manifest_malformed", f"unexpected manifest field(s) {extra}"
        )
    digest = raw.get("digest")
    if not isinstance(digest, str):
        raise LockedNpmError("tree_manifest_malformed", "manifest digest must be a string")
    entries_raw = raw.get("entries")
    if not isinstance(entries_raw, list):
        raise LockedNpmError("tree_manifest_malformed", "manifest entries must be a list")
    entries = tuple(
        _entry_from_jsonable(item, f"entries[{i}]")
        for i, item in enumerate(entries_raw)
    )
    ordered = tuple(sorted(entries))
    if entries != ordered:
        raise LockedNpmError(
            "tree_order_mismatch", "tree manifest entries are not in canonical path order"
        )
    if len(ordered) != len({e.path for e in ordered}):
        raise LockedNpmError(
            "tree_order_mismatch", "tree manifest contains duplicate paths"
        )
    if canonical_tree_digest(ordered) != digest:
        raise LockedNpmError(
            "tree_manifest_digest_mismatch",
            "tree manifest digest does not match its entries",
        )
    return TreeManifest(entries=ordered, digest=digest)


def _hash_file_entry(
    directory: DirectoryDescriptor, name: str, rel: str
) -> _Found:
    """Hash one regular-file entry from a single no-follow descriptor.

    Regular files stay a raw POSIX boundary: the lightweight capability
    foundation deliberately owns no regular-file authority, so the file is
    opened once with ``O_NOFOLLOW`` and transferred immediately into an
    ``OwnedDescriptor``.  The owner is the sole release authority, so an
    active stat/type/read/hash failure stays primary over an ordinary close
    failure and a successful release is terminal and at-most-once.
    """
    ops = PosixDescriptorOps()
    label = f"tree file entry {rel}"
    try:
        fd = os.open(name, _NOFOLLOW_RDONLY, dir_fd=directory.fd)
    except OSError as exc:
        raise LockedNpmError(
            "tree_type_mismatch", f"cannot open file entry {rel!r}: {exc}"
        ) from exc
    owner = OwnedDescriptor(ops, fd, label=label)
    with owner:
        fst = os.fstat(owner.fd)
        if not _stat.S_ISREG(fst.st_mode):
            raise LockedNpmError(
                "tree_type_mismatch",
                f"file entry {rel!r} changed type while being read",
            )
        hasher = hashlib.sha256()
        while True:
            chunk = os.read(owner.fd, 64 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
        return _Found(
            rel,
            _KIND_FILE,
            hasher.hexdigest(),
            "",
            _stat.S_IMODE(fst.st_mode),
            fst.st_uid,
            fst.st_gid,
        )


def _walk_entries(
    directory: DirectoryDescriptor,
    prefix: str,
    visit: Callable[[_Found], None],
) -> None:
    """Visit canonical entries beneath *directory* without following symlinks.

    Children are processed in sorted name order; child directories are opened
    descriptor-relatively by the capability (``O_DIRECTORY | O_NOFOLLOW``),
    and files are opened once with ``O_NOFOLLOW`` and hashed from that same
    descriptor.  Each child-directory capability is released at most once, and
    an ordinary close failure stays secondary to any active traversal failure.

    *visit* is invoked inline and its result is never retained, so an
    exception it raises propagates synchronously through every active
    ``with sub:`` block instead of being observed as a ``GeneratorExit`` when a
    suspended generator is closed.  That is what lets a containing directory's
    close failure attach to the real verification error.
    """
    for name in sorted(directory.list_names()):
        rel = f"{prefix}/{name}" if prefix else name
        st = directory.stat_child(name, follow_symlinks=False)
        mode = _stat.S_IMODE(st.st_mode)
        uid = st.st_uid
        gid = st.st_gid
        if _stat.S_ISLNK(st.st_mode):
            target = os.readlink(name, dir_fd=directory.fd)
            visit(_Found(rel, _KIND_SYMLINK, "", target, mode, uid, gid))
        elif _stat.S_ISDIR(st.st_mode):
            visit(_Found(rel, _KIND_DIRECTORY, "", "", mode, uid, gid))
            try:
                sub = directory.open_directory(
                    name, label=rel, require_owner=False
                )
            except DescriptorError as exc:
                raise LockedNpmError(
                    "tree_type_mismatch",
                    f"cannot open directory entry {rel!r}: "
                    f"{_foundation_cause(exc)}",
                ) from exc
            with sub:
                _walk_entries(sub, rel, visit)
        elif _stat.S_ISREG(st.st_mode):
            visit(_hash_file_entry(directory, name, rel))
        else:
            visit(_Found(rel, _KIND_SPECIAL, "", "", mode, uid, gid))


def _open_tree_root(ops: DescriptorOps, root: Path) -> DirectoryDescriptor:
    """Open *root* as a no-follow directory capability.

    The supplied components are preserved verbatim: a relative path is made
    absolute by prefixing the current working directory instead of collapsing
    it lexically, so ``missing/../tree``, ``regular-file/../tree``, and
    ``symlink/../tree`` still walk -- and must reject -- every component the
    caller supplied rather than silently inspecting an equivalent-looking but
    different ``tree``.  Lexical normalization (``abspath``/``resolve``/
    ``normpath``) would erase components before the secure walker could
    validate them.

    Component walking, parent/child handoff, directory and ownership
    validation, and at-most-once release are delegated to
    ``DirectoryDescriptor.open_secure_path``.  Only a validation-stage type
    rejection (the foundation proved the adopted root is not a directory) maps
    to the historical ``tree root <root> is not a directory`` detail.  Every
    open-stage failure -- including the raw no-follow ``ENOTDIR``/``ENOENT``
    reported for a regular-file, symlinked, or missing component -- maps to
    ``unsafe tree root <root>: <cause>``.  Either way the ``DescriptorError``
    is retained as the direct cause so the original ``OSError`` and cleanup
    diagnostics stay reachable.
    """
    absolute = str(root)
    if not os.path.isabs(absolute):
        # Preserve the caller's components (including ``..``) so the secure
        # walker validates each one; ``os.path.join`` never collapses them.
        absolute = os.path.join(os.getcwd(), absolute)
    try:
        return DirectoryDescriptor.open_secure_path(
            ops, absolute, label=str(root), require_owner=False
        )
    except DescriptorError as exc:
        if _root_is_not_a_directory(exc):
            raise LockedNpmError(
                "unsafe_cache_path",
                f"tree root {root} is not a directory",
            ) from exc
        raise LockedNpmError(
            "unsafe_cache_path",
            f"unsafe tree root {root}: {_foundation_cause(exc)}",
        ) from exc


def build_tree_manifest(
    root: str | Path, *, ops: DescriptorOps | None = None
) -> TreeManifest:
    """Build a canonical tree manifest for the directory tree at *root*.

    Rejects a symlinked root, special files, and symlinks whose target is
    absolute, contains a backslash, or resolves outside the tree root.
    """
    root_path = Path(root)
    descriptor_ops = _resolve_ops(ops)
    with _open_tree_root(descriptor_ops, root_path) as root_capability:
        found: list[_Found] = []
        _walk_entries(root_capability, "", found.append)
        # Manifest validation runs while the root capability is still owned so
        # its release can retain a root-close failure as secondary diagnostic
        # context rather than letting it replace the domain error.
        for entry in found:
            if entry.kind == _KIND_SPECIAL:
                raise LockedNpmError(
                    "unsupported_entry_type",
                    f"unsupported special filesystem entry at {entry.path!r}",
                )
            if entry.kind == _KIND_SYMLINK and not _contained_target(
                entry.path, entry.target
            ):
                raise LockedNpmError(
                    "unsafe_symlink_target",
                    f"symlink {entry.path!r} targets outside the tree: "
                    f"{entry.target!r}",
                )

    entries = tuple(
        sorted(
            TreeEntry(
                path=e.path,
                kind=e.kind,
                digest=e.digest,
                target=e.target,
                mode=e.mode,
                uid=e.uid,
                gid=e.gid,
            )
            for e in found
        )
    )
    return TreeManifest(entries=entries, digest=canonical_tree_digest(entries))


def _compare(found: _Found, expected: TreeEntry) -> None:
    if found.kind == _KIND_SYMLINK and not _contained_target(
        found.path, found.target
    ):
        raise LockedNpmError(
            "tree_symlink_escape",
            f"symlink {found.path!r} targets outside the tree: "
            f"{found.target!r}",
        )
    if found.kind != expected.kind:
        raise LockedNpmError(
            "tree_type_mismatch",
            f"{found.path!r}: expected {expected.kind}, found {found.kind}",
        )
    if found.kind == _KIND_FILE and found.digest != expected.digest:
        raise LockedNpmError(
            "tree_hash_mismatch",
            f"{found.path!r}: file hash does not match the manifest",
        )
    if found.kind == _KIND_SYMLINK and found.target != expected.target:
        raise LockedNpmError(
            "tree_symlink_target_mismatch",
            f"{found.path!r}: symlink target {found.target!r} does not match "
            f"manifest target {expected.target!r}",
        )
    if found.mode != expected.mode:
        raise LockedNpmError(
            "tree_permission_mismatch",
            f"{found.path!r}: mode {found.mode:04o} does not match manifest "
            f"mode {expected.mode:04o}",
        )
    if found.uid != expected.uid or found.gid != expected.gid:
        raise LockedNpmError(
            "tree_ownership_mismatch",
            f"{found.path!r}: ownership ({found.uid}:{found.gid}) does not "
            f"match the manifest ({expected.uid}:{expected.gid})",
        )


def verify_tree(
    root: str | Path,
    manifest: TreeManifest,
    *,
    ops: DescriptorOps | None = None,
) -> None:
    """Re-validate *root* against *manifest* without following symlinks.

    First the manifest itself is checked for canonical ordering, unique
    paths, and digest consistency; then every live entry is compared for
    type, hash, target, permission, and ownership.  Extra and missing
    entries are rejected.  Raises :class:`LockedNpmError` on any corruption.
    """
    ordered = tuple(sorted(manifest.entries))
    if manifest.entries != ordered:
        raise LockedNpmError(
            "tree_order_mismatch",
            "tree manifest entries are not in canonical path order",
        )
    if len(ordered) != len({e.path for e in ordered}):
        raise LockedNpmError(
            "tree_order_mismatch", "tree manifest contains duplicate paths"
        )
    if canonical_tree_digest(manifest.entries) != manifest.digest:
        raise LockedNpmError(
            "tree_manifest_digest_mismatch",
            "tree manifest digest does not match its entries",
        )

    by_path = {e.path: e for e in manifest.entries}
    seen: set[str] = set()
    descriptor_ops = _resolve_ops(ops)

    def visit(found: _Found) -> None:
        expected = by_path.get(found.path)
        if expected is None:
            raise LockedNpmError(
                "tree_extra_entry",
                f"unexpected tree entry {found.path!r}",
            )
        seen.add(found.path)
        _compare(found, expected)

    with _open_tree_root(descriptor_ops, Path(root)) as root_capability:
        _walk_entries(root_capability, "", visit)

    missing = [path for path in by_path if path not in seen]
    if missing:
        raise LockedNpmError(
            "tree_missing_entry",
            f"tree entry {missing[0]!r} is missing from the filesystem",
        )
