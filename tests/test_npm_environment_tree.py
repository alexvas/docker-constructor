"""Phase 2 — canonical hashed tree manifest and no-follow verification
(tasks 2.2 and 2.3).

``build_tree_manifest`` deterministically describes files, directories, and
contained symlinks beneath a tree root in path order and hashes every
regular file.  ``verify_tree`` re-validates a manifest against the live
filesystem without following symlinks and detects every corruption class:
type, hash, permission, ownership, ordering, extra, missing, and escape.
Neither primitive assumes a published output identity or an
assembler-evidence digest.
"""

from __future__ import annotations

import ast
import contextlib
import dataclasses
import errno
import hashlib
import os
import stat
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from docker.filesystem.descriptors import DescriptorError
from docker.filesystem.operations import PosixDescriptorOps
from docker.npm_environment import LockedNpmError
from docker.npm_environment import tree as tree_module
from docker.npm_environment.tree import TreeEntry, TreeManifest


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def _fd_path(fd: int) -> str | None:
    try:
        return os.path.normpath(os.readlink(f"/proc/self/fd/{fd}"))
    except OSError:
        return None


def _foreign_uid_fstat(target: Path, foreign_uid: int):
    real_fstat = os.fstat
    target_norm = os.path.normpath(str(target))

    def fake_fstat(fd, *args, **kwargs):
        st = real_fstat(fd, *args, **kwargs)
        if _fd_path(fd) == target_norm:
            return types.SimpleNamespace(
                st_mode=st.st_mode, st_uid=foreign_uid, st_gid=st.st_gid
            )
        return st

    return fake_fstat


class _TreeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="npm-env-tree-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "tree"
        self.root.mkdir()

    def _build(self, *, owner_safe: bool = True) -> TreeManifest:
        return tree_module.build_tree_manifest(self.root)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


#: Fixture manifest pins captured before the Phase 7 descriptor migration.  The
#: content digest excludes mode/ownership, so it is machine-independent; the
#: serialized bytes normalize uid/gid to :data:`_GOLDEN_UID` so canonical bytes
#: can be compared byte-for-byte on any machine.
_GOLDEN_TREE_DIGEST = "9aadfdf6456a4c36bd551b3581a41d71c5f667c389ef8bda780c45ff10c2847c"
_GOLDEN_UID = 4242
_GOLDEN_BYTES = (
    '{"digest":"9aadfdf6456a4c36bd551b3581a41d71c5f667c389ef8bda780c45ff10c2847c",'
    '"entries":['
    '{"digest":"","gid":4242,"kind":"directory","mode":493,"path":"a","target":"","uid":4242},'
    '{"digest":"33bf6fbd7cd8379785a21e233d8e09f824e7bab459168a96312c1c882c1d7e1f","gid":4242,"kind":"file","mode":420,"path":"a/inner.txt","target":"","uid":4242},'
    '{"digest":"","gid":4242,"kind":"directory","mode":493,"path":"m","target":"","uid":4242},'
    '{"digest":"","gid":4242,"kind":"symlink","mode":511,"path":"m/link","target":"../a/inner.txt","uid":4242},'
    '{"digest":"594e519ae499312b29433b7dd8a97ff068defcba9755b6d5d00e84c524d67b06","gid":4242,"kind":"file","mode":420,"path":"z.txt","target":"","uid":4242}'
    ']}'
)


def _normalized_manifest_bytes(manifest: TreeManifest) -> bytes:
    """Serialize *manifest* with uid/gid pinned to the golden sentinel."""
    normalized = dataclasses.replace(
        manifest,
        entries=tuple(
            dataclasses.replace(entry, uid=_GOLDEN_UID, gid=_GOLDEN_UID)
            for entry in manifest.entries
        ),
    )
    return tree_module.serialize_manifest(normalized)


def _secondary_exceptions(exc: BaseException) -> list[object]:
    """Return the secondary cleanup diagnostics retained by *exc*."""
    secondary = getattr(exc, "secondary", None)
    if isinstance(secondary, list):
        return list(secondary)
    slot = getattr(exc, "_transaction_secondary", None)
    if isinstance(slot, list):
        return list(slot)
    return list(getattr(exc, "__notes__", []))


def _carries_secondary(exc: BaseException, needle: BaseException) -> bool:
    """True when *exc* retains *needle* as secondary diagnostic context."""
    for item in _secondary_exceptions(exc):
        if item is needle:
            return True
        if isinstance(item, str) and str(needle) in item:
            return True
    return False


def _raise_when(predicate, error):
    """Build an ``ops`` hook that raises *error* when *predicate* holds."""

    def hook(*args):
        if predicate(*args):
            raise error

    return hook


class _LedgerOps(PosixDescriptorOps):
    """A descriptor backend that records live descriptors and injects faults.

    ``openat`` records every returned descriptor as live; ``close`` flags an
    attempt on a descriptor that is not live (a double close) and clears it on
    the way out, so a leaked descriptor is distinguishable from a retried
    close across descriptor-number reuse.  Deterministic faults are injected
    through ``hooks`` (called with the operation arguments before delegation).
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.counts: dict[str, int] = {}
        self.hooks: dict[str, object] = {}
        self.open_order: list[int] = []
        self.open_paths: list[tuple[int, str | None]] = []
        self.close_order: list[int] = []
        self.close_paths: list[str | None] = []
        self.live: set[int] = set()
        self.double_closes: list[int] = []

    def _record(self, name: str, *args: object) -> None:
        self.calls.append((name, args))
        self.counts[name] = self.counts.get(name, 0) + 1
        hook = self.hooks.get(name)
        if hook is not None:
            hook(*args)

    def openat(self, directory_fd, name, flags, mode=0):
        self._record("openat", directory_fd, name, flags, mode)
        fd = super().openat(directory_fd, name, flags, mode)
        self.open_order.append(fd)
        self.open_paths.append((fd, _fd_path(fd)))
        self.live.add(fd)
        return fd

    def close(self, fd: int) -> None:
        self.close_order.append(fd)
        self.close_paths.append(_fd_path(fd))
        if fd not in self.live:
            self.double_closes.append(fd)
        try:
            self._record("close", fd)
            super().close(fd)
        finally:
            self.live.discard(fd)

    def fstat(self, fd: int):
        self._record("fstat", fd)
        return super().fstat(fd)

    def statat(self, directory_fd, name, *, follow_symlinks):
        self._record("statat", directory_fd, name, follow_symlinks)
        return super().statat(directory_fd, name, follow_symlinks=follow_symlinks)

    def listdir(self, fd: int):
        self._record("listdir", fd)
        return super().listdir(fd)


def _fail_close_on(ops: _LedgerOps, target: Path, error: OSError) -> None:
    norm = os.path.normpath(str(target))
    ops.hooks["close"] = _raise_when(lambda fd: _fd_path(fd) == norm, error)


def _fail_listdir_on(ops: _LedgerOps, target: Path, error: OSError) -> None:
    norm = os.path.normpath(str(target))
    ops.hooks["listdir"] = _raise_when(lambda fd: _fd_path(fd) == norm, error)


def _fail_stat_on(
    ops: _LedgerOps, parent: Path, name: str, error: OSError
) -> None:
    parent_norm = os.path.normpath(str(parent))
    ops.hooks["statat"] = _raise_when(
        lambda dir_fd, entry, *_: _fd_path(dir_fd) == parent_norm
        and entry == name,
        error,
    )


class TestCanonicalTreeManifest(_TreeTestCase):
    """2.2 — canonical tree generation."""

    def test_describes_files_directories_and_symlinks_in_path_order(self):
        (self.root / "z.txt").write_text("z")
        (self.root / "a").mkdir()
        (self.root / "a" / "inner.txt").write_text("inner")
        (self.root / "m").mkdir()
        (self.root / "m" / "link").symlink_to("../a/inner.txt")

        manifest = self._build()
        paths = [e.path for e in manifest.entries]
        self.assertEqual(paths, sorted(paths))
        self.assertEqual(
            paths,
            ["a", "a/inner.txt", "m", "m/link", "z.txt"],
        )
        by_path = {e.path: e for e in manifest.entries}
        self.assertEqual(by_path["a"].kind, "directory")
        self.assertEqual(by_path["a"].digest, "")
        self.assertEqual(by_path["a/inner.txt"].kind, "file")
        self.assertEqual(by_path["a/inner.txt"].digest, _sha256(b"inner"))
        self.assertEqual(by_path["z.txt"].digest, _sha256(b"z"))
        self.assertEqual(by_path["m/link"].kind, "symlink")
        self.assertEqual(by_path["m/link"].target, "../a/inner.txt")
        self.assertEqual(by_path["m/link"].digest, "")

    def test_file_hashes_are_deterministic(self):
        (self.root / "f").write_bytes(b"\x00\x01\x02")
        first = self._build()
        second = self._build()
        self.assertEqual(first.digest, second.digest)
        self.assertEqual(first.entries, second.entries)

    def test_contained_relative_symlink_accepted(self):
        (self.root / "sub").mkdir()
        (self.root / "sub" / "target.txt").write_text("t")
        (self.root / "sub" / "up").symlink_to("../sub/target.txt")
        manifest = self._build()
        link = next(e for e in manifest.entries if e.path == "sub/up")
        self.assertEqual(link.target, "../sub/target.txt")

    def test_records_mode_and_ownership(self):
        path = self.root / "f"
        path.write_text("x")
        os.chmod(path, 0o640)
        manifest = self._build()
        entry = next(e for e in manifest.entries if e.path == "f")
        self.assertEqual(entry.mode, 0o640)
        self.assertEqual(entry.uid, os.geteuid())
        self.assertEqual(entry.gid, os.stat(path).st_gid)

    def test_empty_directory_manifest(self):
        manifest = self._build()
        self.assertEqual(manifest.entries, ())
        self.assertEqual(manifest.digest, tree_module.canonical_tree_digest(()))

    def test_digest_is_order_independent_and_content_only(self):
        (self.root / "a").write_text("a")
        (self.root / "b").write_text("b")
        manifest = self._build()
        reordered = dataclasses.replace(
            manifest, entries=tuple(reversed(manifest.entries))
        )
        # Canonical digest is computed over sorted content fields, so a
        # reordered entry tuple yields the same canonical digest.
        self.assertEqual(
            tree_module.canonical_tree_digest(reordered.entries),
            manifest.digest,
        )

    def test_content_change_changes_digest(self):
        (self.root / "f").write_text("one")
        first = self._build()
        (self.root / "f").write_text("two")
        second = self._build()
        self.assertNotEqual(first.digest, second.digest)


class TestCanonicalTreeRejects(_TreeTestCase):
    """2.2 — special files and escaping symlinks are rejected at build."""

    def test_special_file_rejected(self):
        fifo = self.root / "pipe"
        os.mkfifo(fifo)
        with self.assertRaises(LockedNpmError) as ctx:
            self._build()
        self.assertEqual(ctx.exception.reason, "unsupported_entry_type")

    def test_escaping_symlink_rejected(self):
        (self.root / "link").symlink_to("../outside")
        with self.assertRaises(LockedNpmError) as ctx:
            self._build()
        self.assertEqual(ctx.exception.reason, "unsafe_symlink_target")

    def test_absolute_symlink_rejected(self):
        (self.root / "link").symlink_to("/etc/passwd")
        with self.assertRaises(LockedNpmError) as ctx:
            self._build()
        self.assertEqual(ctx.exception.reason, "unsafe_symlink_target")

    def test_symlinked_root_rejected(self):
        real = self.root.parent / "real"
        real.mkdir()
        link = self.root.parent / "tree-link"
        link.symlink_to(real, target_is_directory=True)
        with self.assertRaises(LockedNpmError) as ctx:
            tree_module.build_tree_manifest(link)
        self.assertEqual(ctx.exception.reason, "unsafe_cache_path")


class TestNoFollowVerifier(_TreeTestCase):
    """2.3 — complete no-follow manifest/filesystem revalidation."""

    def _tree(self):
        (self.root / "file.txt").write_text("payload")
        (self.root / "dir").mkdir()
        (self.root / "dir" / "nested.txt").write_text("nested")
        (self.root / "dir" / "self").symlink_to("nested.txt")
        return tree_module.build_tree_manifest(self.root)

    def test_valid_tree_verifies(self):
        manifest = self._tree()
        tree_module.verify_tree(self.root, manifest)

    def test_extra_file_detected(self):
        manifest = self._tree()
        (self.root / "extra.txt").write_text("sneaky")
        with self.assertRaises(LockedNpmError) as ctx:
            tree_module.verify_tree(self.root, manifest)
        self.assertEqual(ctx.exception.reason, "tree_extra_entry")

    def test_extra_directory_detected(self):
        manifest = self._tree()
        (self.root / "extra-dir").mkdir()
        with self.assertRaises(LockedNpmError) as ctx:
            tree_module.verify_tree(self.root, manifest)
        self.assertEqual(ctx.exception.reason, "tree_extra_entry")

    def test_missing_file_detected(self):
        manifest = self._tree()
        (self.root / "file.txt").unlink()
        with self.assertRaises(LockedNpmError) as ctx:
            tree_module.verify_tree(self.root, manifest)
        self.assertEqual(ctx.exception.reason, "tree_missing_entry")

    def test_missing_directory_detected(self):
        manifest = self._tree()
        (self.root / "dir" / "nested.txt").unlink()
        (self.root / "dir" / "self").unlink()
        (self.root / "dir").rmdir()
        with self.assertRaises(LockedNpmError) as ctx:
            tree_module.verify_tree(self.root, manifest)
        self.assertEqual(ctx.exception.reason, "tree_missing_entry")

    def test_type_corruption_file_to_symlink(self):
        manifest = self._tree()
        (self.root / "file.txt").unlink()
        (self.root / "file.txt").symlink_to("dir/nested.txt")
        with self.assertRaises(LockedNpmError) as ctx:
            tree_module.verify_tree(self.root, manifest)
        self.assertEqual(ctx.exception.reason, "tree_type_mismatch")

    def test_type_corruption_directory_to_file(self):
        manifest = self._tree()
        (self.root / "dir" / "nested.txt").unlink()
        (self.root / "dir" / "self").unlink()
        (self.root / "dir").rmdir()
        (self.root / "dir").write_text("was a directory")
        with self.assertRaises(LockedNpmError) as ctx:
            tree_module.verify_tree(self.root, manifest)
        self.assertEqual(ctx.exception.reason, "tree_type_mismatch")

    def test_type_corruption_file_to_special(self):
        manifest = self._tree()
        (self.root / "file.txt").unlink()
        os.mkfifo(self.root / "file.txt")
        with self.assertRaises(LockedNpmError) as ctx:
            tree_module.verify_tree(self.root, manifest)
        self.assertEqual(ctx.exception.reason, "tree_type_mismatch")

    def test_hash_corruption_detected(self):
        manifest = self._tree()
        (self.root / "file.txt").write_text("tampered")
        with self.assertRaises(LockedNpmError) as ctx:
            tree_module.verify_tree(self.root, manifest)
        self.assertEqual(ctx.exception.reason, "tree_hash_mismatch")

    def test_permission_corruption_detected(self):
        manifest = self._tree()
        os.chmod(self.root / "file.txt", 0o600)
        with self.assertRaises(LockedNpmError) as ctx:
            tree_module.verify_tree(self.root, manifest)
        self.assertEqual(ctx.exception.reason, "tree_permission_mismatch")

    def test_ownership_corruption_detected(self):
        manifest = self._tree()
        foreign_uid = os.getuid() + 1
        target = self.root / "file.txt"
        with mock.patch(
            "os.fstat", side_effect=_foreign_uid_fstat(target, foreign_uid)
        ):
            with self.assertRaises(LockedNpmError) as ctx:
                tree_module.verify_tree(self.root, manifest)
        self.assertEqual(ctx.exception.reason, "tree_ownership_mismatch")

    def test_symlink_target_corruption_detected(self):
        manifest = self._tree()
        (self.root / "dir" / "self").unlink()
        (self.root / "dir" / "self").symlink_to("nested.txt.other")
        with self.assertRaises(LockedNpmError) as ctx:
            tree_module.verify_tree(self.root, manifest)
        self.assertEqual(ctx.exception.reason, "tree_symlink_target_mismatch")

    def test_symlink_escape_corruption_detected(self):
        manifest = self._tree()
        (self.root / "dir" / "self").unlink()
        (self.root / "dir" / "self").symlink_to("../../outside")
        with self.assertRaises(LockedNpmError) as ctx:
            tree_module.verify_tree(self.root, manifest)
        self.assertEqual(ctx.exception.reason, "tree_symlink_escape")

    def test_manifest_order_corruption_detected(self):
        manifest = self._tree()
        reordered = dataclasses.replace(
            manifest, entries=tuple(reversed(manifest.entries))
        )
        with self.assertRaises(LockedNpmError) as ctx:
            tree_module.verify_tree(self.root, reordered)
        self.assertEqual(ctx.exception.reason, "tree_order_mismatch")

    def test_manifest_digest_corruption_detected(self):
        manifest = self._tree()
        tampered = dataclasses.replace(manifest, digest="0" * 64)
        with self.assertRaises(LockedNpmError) as ctx:
            tree_module.verify_tree(self.root, tampered)
        self.assertEqual(ctx.exception.reason, "tree_manifest_digest_mismatch")

    def test_verifier_never_follows_symlinks(self):
        # A symlink whose target lives outside the tree must be reported as
        # a symlink escape, never followed to hash the external file.
        outside = self.root.parent / "outside.txt"
        outside.write_text("external")
        manifest = self._tree()
        (self.root / "file.txt").unlink()
        (self.root / "file.txt").symlink_to("../outside.txt")
        with self.assertRaises(LockedNpmError) as ctx:
            tree_module.verify_tree(self.root, manifest)
        self.assertEqual(ctx.exception.reason, "tree_symlink_escape")
        self.assertEqual(outside.read_text(), "external")


class TestVerifierManifestValidation(unittest.TestCase):
    def test_duplicate_path_rejected(self):
        entry = TreeEntry(
            path="a", kind="file", digest="", target="", mode=0, uid=0, gid=0
        )
        manifest = TreeManifest(
            entries=(entry, entry),
            digest=tree_module.canonical_tree_digest((entry, entry)),
        )
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(LockedNpmError) as ctx:
                tree_module.verify_tree(Path(tmp), manifest)
            self.assertEqual(ctx.exception.reason, "tree_order_mismatch")


class TestTreeTraversalPrecedence(unittest.TestCase):
    """Task 7.1 — traversal failures outrank ordinary directory-close failures."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="npm-env-tree-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "tree"
        self.sub = self.root / "sub"
        self.nested = self.sub / "nested"
        self.nested.mkdir(parents=True)

    def _assert_released_once(self, ops: _LedgerOps) -> None:
        self.assertEqual(ops.double_closes, [])
        self.assertEqual(ops.live, set())
        self.assertEqual(set(ops.close_order), set(ops.open_order))

    def test_enumeration_failure_primary_over_subdirectory_close(self):
        (self.nested / "keep").write_text("x")
        ops = _LedgerOps()
        enum_error = OSError(errno.EIO, "injected enumeration failure")
        close_error = OSError(errno.EIO, "injected nested close failure")
        _fail_listdir_on(ops, self.nested, enum_error)
        _fail_close_on(ops, self.nested, close_error)

        with self.assertRaises(OSError) as ctx:
            tree_module.build_tree_manifest(self.root, ops=ops)

        self.assertIs(ctx.exception, enum_error)
        self.assertTrue(_carries_secondary(ctx.exception, close_error))
        self.assertIn(os.path.normpath(str(self.nested)), ops.close_paths)
        self._assert_released_once(ops)

    def test_stat_failure_primary_over_subdirectory_close(self):
        (self.nested / "entry").write_text("x")
        ops = _LedgerOps()
        stat_error = OSError(errno.EIO, "injected stat failure")
        close_error = OSError(errno.EIO, "injected nested close failure")
        _fail_stat_on(ops, self.nested, "entry", stat_error)
        _fail_close_on(ops, self.nested, close_error)

        with self.assertRaises(OSError) as ctx:
            tree_module.build_tree_manifest(self.root, ops=ops)

        self.assertIs(ctx.exception, stat_error)
        self.assertTrue(_carries_secondary(ctx.exception, close_error))
        self._assert_released_once(ops)

    def test_hash_read_failure_primary_over_subdirectory_close(self):
        (self.nested / "f").write_text("payload")
        ops = _LedgerOps()
        read_error = OSError(errno.EIO, "injected hash read failure")
        close_error = OSError(errno.EIO, "injected nested close failure")
        _fail_close_on(ops, self.nested, close_error)

        with mock.patch("os.read", side_effect=read_error):
            with self.assertRaises(OSError) as ctx:
                tree_module.build_tree_manifest(self.root, ops=ops)

        self.assertIs(ctx.exception, read_error)
        self.assertTrue(_carries_secondary(ctx.exception, close_error))
        self._assert_released_once(ops)

    def test_directory_open_failure_primary_over_parent_close(self):
        (self.nested / "deeper").mkdir()
        ops = _LedgerOps()
        open_error = OSError(errno.ENOTDIR, "injected directory open failure")
        close_error = OSError(errno.EIO, "injected nested close failure")
        _fail_close_on(ops, self.nested, close_error)
        nested_norm = os.path.normpath(str(self.nested))
        original_openat = ops.openat

        def failing_openat(directory_fd, name, flags, mode=0):
            if _fd_path(directory_fd) == nested_norm and name == "deeper":
                raise open_error
            return original_openat(directory_fd, name, flags, mode)

        ops.openat = failing_openat

        with self.assertRaises(LockedNpmError) as ctx:
            tree_module.build_tree_manifest(self.root, ops=ops)

        self.assertEqual(ctx.exception.reason, "tree_type_mismatch")
        self.assertIn("cannot open directory entry 'sub/nested/deeper'", ctx.exception.detail)
        self.assertTrue(_carries_secondary(ctx.exception, close_error))
        self._assert_released_once(ops)

    def test_every_opened_directory_receives_one_close(self):
        (self.root / "a" / "b").mkdir(parents=True)
        (self.root / "a" / "b" / "f").write_text("x")
        (self.root / "c").mkdir()
        ops = _LedgerOps()
        tree_module.build_tree_manifest(self.root, ops=ops)
        self._assert_released_once(ops)


class TestTreeVerificationLifecycle(unittest.TestCase):
    """Nested verification failures keep the containing close diagnostic.

    ``verify_tree`` rejects a yielded entry *inside* the traversal.  The
    callback walker lets that error propagate through every active directory
    context, so a containing directory's ordinary close failure is retained as
    secondary diagnostic context instead of being observed as a discarded
    ``GeneratorExit``.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="npm-env-tree-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "tree"
        self.nested = self.root / "sub" / "nested"
        self.nested.mkdir(parents=True)

    def _assert_released_once(self, ops: _LedgerOps) -> None:
        self.assertEqual(ops.double_closes, [])
        self.assertEqual(ops.live, set())
        self.assertEqual(set(ops.close_order), set(ops.open_order))

    @staticmethod
    def _corrupt_digest(manifest: TreeManifest, path: str) -> TreeManifest:
        entries = tuple(
            dataclasses.replace(entry, digest="0" * 64)
            if entry.path == path
            else entry
            for entry in manifest.entries
        )
        return dataclasses.replace(
            manifest,
            entries=entries,
            digest=tree_module.canonical_tree_digest(entries),
        )

    def test_nested_hash_mismatch_keeps_containing_close_secondary(self) -> None:
        (self.nested / "f").write_text("payload")
        manifest = self._corrupt_digest(
            tree_module.build_tree_manifest(self.root), "sub/nested/f"
        )
        ops = _LedgerOps()
        close_error = OSError(errno.EIO, "injected nested close failure")
        _fail_close_on(ops, self.nested, close_error)

        with self.assertRaises(LockedNpmError) as ctx:
            tree_module.verify_tree(self.root, manifest, ops=ops)

        self.assertEqual(ctx.exception.reason, "tree_hash_mismatch")
        self.assertTrue(_carries_secondary(ctx.exception, close_error))
        self.assertEqual(
            ops.close_paths.count(os.path.realpath(self.nested)), 1
        )
        self._assert_released_once(ops)

    def test_nested_extra_entry_keeps_containing_close_secondary(self) -> None:
        (self.nested / "f").write_text("payload")
        manifest = tree_module.build_tree_manifest(self.root)
        (self.nested / "extra").write_text("surprise")
        ops = _LedgerOps()
        close_error = OSError(errno.EIO, "injected nested close failure")
        _fail_close_on(ops, self.nested, close_error)

        with self.assertRaises(LockedNpmError) as ctx:
            tree_module.verify_tree(self.root, manifest, ops=ops)

        self.assertEqual(ctx.exception.reason, "tree_extra_entry")
        self.assertTrue(_carries_secondary(ctx.exception, close_error))
        self.assertEqual(
            ops.close_paths.count(os.path.realpath(self.nested)), 1
        )
        self._assert_released_once(ops)


class TestTreeRootLifecycle(unittest.TestCase):
    """Task 7.2 — root validation precedence and at-most-once root release."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="npm-env-tree-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "tree"
        self.root.mkdir()

    def _empty_manifest(self) -> TreeManifest:
        return TreeManifest(
            entries=(), digest=tree_module.canonical_tree_digest(())
        )

    def test_root_validation_failure_keeps_primary_with_secondary_close(self):
        stat_error = OSError(errno.EIO, "injected root stat failure")
        close_error = OSError(errno.EIO, "injected root close failure")
        real_fstat = os.fstat
        real_close = os.close
        root_norm = os.path.normpath(str(self.root))

        def fake_fstat(fd, *args, **kwargs):
            if _fd_path(fd) == root_norm:
                raise stat_error
            return real_fstat(fd, *args, **kwargs)

        def fake_close(fd):
            if _fd_path(fd) == root_norm:
                raise close_error
            return real_close(fd)

        # Before the migration the raw root helper closed in a bare
        # ``finally``, so the close error replaced the validation error.  The
        # capability keeps validation primary and the close secondary.
        with mock.patch("os.fstat", side_effect=fake_fstat), mock.patch(
            "os.close", side_effect=fake_close
        ):
            with self.assertRaises(LockedNpmError) as ctx:
                tree_module.build_tree_manifest(self.root)

        self.assertEqual(ctx.exception.reason, "unsafe_cache_path")
        self.assertEqual(
            ctx.exception.detail, f"unsafe tree root {self.root}: {stat_error}"
        )
        foundation = ctx.exception.__cause__
        self.assertIsInstance(foundation, DescriptorError)
        self.assertTrue(_carries_secondary(foundation, close_error))

    def test_build_root_validation_failure_preserves_secondary_close(self):
        ops = _LedgerOps()
        stat_error = OSError(errno.EIO, "injected root stat failure")
        close_error = OSError(errno.EIO, "injected root close failure")
        root_norm = os.path.normpath(str(self.root))
        ops.hooks["fstat"] = _raise_when(
            lambda fd: _fd_path(fd) == root_norm, stat_error
        )
        _fail_close_on(ops, self.root, close_error)

        with self.assertRaises(LockedNpmError) as ctx:
            tree_module.build_tree_manifest(self.root, ops=ops)

        self.assertEqual(ctx.exception.reason, "unsafe_cache_path")
        foundation = ctx.exception.__cause__
        self.assertIsInstance(foundation, DescriptorError)
        self.assertTrue(_carries_secondary(foundation, close_error))
        self.assertEqual(ops.double_closes, [])
        self.assertEqual(ops.live, set())

    def test_verify_root_validation_failure_preserves_secondary_close(self):
        ops = _LedgerOps()
        stat_error = OSError(errno.EIO, "injected root stat failure")
        close_error = OSError(errno.EIO, "injected root close failure")
        root_norm = os.path.normpath(str(self.root))
        ops.hooks["fstat"] = _raise_when(
            lambda fd: _fd_path(fd) == root_norm, stat_error
        )
        _fail_close_on(ops, self.root, close_error)

        with self.assertRaises(LockedNpmError) as ctx:
            tree_module.verify_tree(self.root, self._empty_manifest(), ops=ops)

        self.assertEqual(ctx.exception.reason, "unsafe_cache_path")
        foundation = ctx.exception.__cause__
        self.assertIsInstance(foundation, DescriptorError)
        self.assertTrue(_carries_secondary(foundation, close_error))
        self.assertEqual(ops.double_closes, [])
        self.assertEqual(ops.live, set())

    def test_build_root_descriptor_closed_exactly_once(self):
        (self.root / "f").write_text("x")
        ops = _LedgerOps()
        tree_module.build_tree_manifest(self.root, ops=ops)
        self.assertEqual(ops.double_closes, [])
        self.assertEqual(ops.live, set())
        self.assertEqual(set(ops.close_order), set(ops.open_order))

    def test_verify_root_descriptor_closed_exactly_once(self):
        (self.root / "f").write_text("x")
        manifest = tree_module.build_tree_manifest(self.root)
        ops = _LedgerOps()
        tree_module.verify_tree(self.root, manifest, ops=ops)
        self.assertEqual(ops.double_closes, [])
        self.assertEqual(ops.live, set())
        self.assertEqual(set(ops.close_order), set(ops.open_order))

    def test_regular_file_root_keeps_unsafe_root_diagnostic(self):
        # A regular-file root is rejected by the no-follow ``O_DIRECTORY``
        # open with the raw ``ENOTDIR``, which is an open-stage failure: the
        # pre-migration ``unsafe tree root`` diagnostic is retained and the
        # raw cause stays reachable through the typed descriptor error.
        file_root = self.root.parent / "not-a-dir"
        file_root.write_text("i am a regular file")

        with self.assertRaises(LockedNpmError) as ctx:
            tree_module.build_tree_manifest(file_root)

        self.assertEqual(ctx.exception.reason, "unsafe_cache_path")
        self.assertTrue(
            ctx.exception.detail.startswith(f"unsafe tree root {file_root}:")
        )
        foundation = ctx.exception.__cause__
        self.assertIsInstance(foundation, DescriptorError)
        raw = foundation.cause
        self.assertIsInstance(raw, OSError)
        self.assertEqual(raw.errno, errno.ENOTDIR)

    def test_validate_stage_non_directory_root_maps_to_historical_detail(self):
        # The foundation's validate stage reports a non-directory root as a
        # distinct ``UnsafeDescriptorError``; it maps to the same historical
        # detail as the ``ENOTDIR`` open failure.
        real_fstat = os.fstat
        root_norm = os.path.normpath(str(self.root))

        def fake_fstat(fd, *args, **kwargs):
            st = real_fstat(fd, *args, **kwargs)
            if _fd_path(fd) == root_norm:
                return types.SimpleNamespace(
                    st_mode=stat.S_IFREG | 0o644,
                    st_uid=st.st_uid,
                    st_gid=st.st_gid,
                )
            return st

        with mock.patch("os.fstat", side_effect=fake_fstat):
            with self.assertRaises(LockedNpmError) as ctx:
                tree_module.build_tree_manifest(self.root)

        self.assertEqual(ctx.exception.reason, "unsafe_cache_path")
        self.assertEqual(
            ctx.exception.detail, f"tree root {self.root} is not a directory"
        )
        self.assertIsInstance(ctx.exception.__cause__, DescriptorError)
        self.assertIn("is not a directory", str(ctx.exception.__cause__))

    def test_special_entry_failure_keeps_primary_with_secondary_root_close(self):
        os.mkfifo(self.root / "pipe")
        ops = _LedgerOps()
        close_error = OSError(errno.EIO, "injected root close failure")
        _fail_close_on(ops, self.root, close_error)

        with self.assertRaises(LockedNpmError) as ctx:
            tree_module.build_tree_manifest(self.root, ops=ops)

        self.assertEqual(ctx.exception.reason, "unsupported_entry_type")
        self.assertEqual(
            ctx.exception.detail,
            "unsupported special filesystem entry at 'pipe'",
        )
        self.assertTrue(_carries_secondary(ctx.exception, close_error))
        self.assertEqual(ops.double_closes, [])
        self.assertEqual(ops.live, set())
        self.assertEqual(
            ops.close_paths.count(os.path.realpath(self.root)), 1
        )

    def test_escaping_symlink_failure_keeps_primary_with_secondary_root_close(
        self,
    ):
        (self.root / "link").symlink_to("../outside")
        ops = _LedgerOps()
        close_error = OSError(errno.EIO, "injected root close failure")
        _fail_close_on(ops, self.root, close_error)

        with self.assertRaises(LockedNpmError) as ctx:
            tree_module.build_tree_manifest(self.root, ops=ops)

        self.assertEqual(ctx.exception.reason, "unsafe_symlink_target")
        self.assertEqual(
            ctx.exception.detail,
            "symlink 'link' targets outside the tree: '../outside'",
        )
        self.assertTrue(_carries_secondary(ctx.exception, close_error))
        self.assertEqual(ops.double_closes, [])
        self.assertEqual(ops.live, set())
        self.assertEqual(
            ops.close_paths.count(os.path.realpath(self.root)), 1
        )

    def test_regular_file_root_operational_open_failure_keeps_unsafe_wording(
        self,
    ):
        # The root really is a regular file, but its open fails with an
        # operational EIO: the domain must keep the operational wording
        # instead of mapping the leaf to the non-directory detail.
        file_root = self.root.parent / "regular-root"
        file_root.write_text("data")
        ops = _LedgerOps()
        error = OSError(errno.EIO, "injected final root open failure")
        ops.hooks["openat"] = _raise_when(
            lambda directory_fd, name, flags, mode=0: name == file_root.name,
            error,
        )

        with self.assertRaises(LockedNpmError) as ctx:
            tree_module.build_tree_manifest(file_root, ops=ops)

        self.assertEqual(ctx.exception.reason, "unsafe_cache_path")
        self.assertEqual(
            ctx.exception.detail,
            f"unsafe tree root {file_root}: {error}",
        )
        self.assertNotIn("is not a directory", ctx.exception.detail)
        foundation = ctx.exception.__cause__
        self.assertIsInstance(foundation, DescriptorError)
        self.assertIs(foundation.cause, error)


class TestTreeRootPathComponents(unittest.TestCase):
    """Task 7.2 follow-up -- a ``..`` component must not hide a bad prefix.

    ``_open_tree_root`` preserves the caller's components instead of
    collapsing them with ``os.path.abspath``, so ``missing/../tree``,
    ``regular-file/../tree``, and ``symlink/../tree`` walk -- and reject --
    every supplied component instead of silently inspecting the sibling
    ``tree``.  A real-directory prefix before ``..`` still resolves to the
    expected tree.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="npm-env-tree-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = self.base / "tree"
        self.root.mkdir()
        (self.root / "f.txt").write_text("payload")
        self.existing = self.base / "existing-directory"
        self.existing.mkdir()
        self.regular = self.base / "regular-file"
        self.regular.write_text("not a directory")
        self.symlink = self.base / "symlink"
        self.symlink.symlink_to(self.root, target_is_directory=True)

    def _bad_relative(self) -> tuple[str, ...]:
        return (
            "missing/../tree",
            "regular-file/../tree",
            "symlink/../tree",
        )

    def _bad_absolute(self) -> tuple[str, ...]:
        return tuple(
            str(self.base / prefix / ".." / "tree")
            for prefix in ("missing", "regular-file", "symlink")
        )

    def test_build_tree_manifest_rejects_bad_parent_component(self) -> None:
        with contextlib.chdir(self.base):
            for supplied in self._bad_relative():
                with self.subTest(supplied=supplied):
                    with self.assertRaises(LockedNpmError) as ctx:
                        tree_module.build_tree_manifest(supplied)
                    self.assertEqual(
                        ctx.exception.reason, "unsafe_cache_path"
                    )
        for supplied in self._bad_absolute():
            with self.subTest(supplied=supplied):
                with self.assertRaises(LockedNpmError) as ctx:
                    tree_module.build_tree_manifest(supplied)
                self.assertEqual(ctx.exception.reason, "unsafe_cache_path")

    def test_verify_tree_rejects_bad_parent_component(self) -> None:
        manifest = tree_module.build_tree_manifest(self.root)
        with contextlib.chdir(self.base):
            for supplied in self._bad_relative():
                with self.subTest(supplied=supplied):
                    with self.assertRaises(LockedNpmError) as ctx:
                        tree_module.verify_tree(supplied, manifest)
                    self.assertEqual(
                        ctx.exception.reason, "unsafe_cache_path"
                    )
        for supplied in self._bad_absolute():
            with self.subTest(supplied=supplied):
                with self.assertRaises(LockedNpmError) as ctx:
                    tree_module.verify_tree(supplied, manifest)
                self.assertEqual(ctx.exception.reason, "unsafe_cache_path")

    def test_existing_directory_parent_component_still_addresses_tree(self):
        expected = tree_module.build_tree_manifest(self.root)
        with contextlib.chdir(self.base):
            relative = tree_module.build_tree_manifest("existing-directory/../tree")
            tree_module.verify_tree("existing-directory/../tree", expected)
        absolute = tree_module.build_tree_manifest(
            str(self.base / "existing-directory" / ".." / "tree")
        )
        tree_module.verify_tree(
            str(self.base / "existing-directory" / ".." / "tree"), expected
        )
        self.assertEqual(relative, expected)
        self.assertEqual(absolute, expected)


class TestTreePreMigrationCharacterization(_TreeTestCase):
    """Task 7.3 — behavior pinned before the descriptor migration."""

    def _golden_tree(self) -> TreeManifest:
        (self.root / "a").mkdir()
        (self.root / "a" / "inner.txt").write_text("inner")
        (self.root / "m").mkdir()
        (self.root / "m" / "link").symlink_to("../a/inner.txt")
        (self.root / "z.txt").write_text("z")
        for directory in (self.root, self.root / "a", self.root / "m"):
            os.chmod(directory, 0o755)
        for file in (self.root / "a" / "inner.txt", self.root / "z.txt"):
            os.chmod(file, 0o644)
        return self._build()

    def test_canonical_digest_is_pinned(self):
        self.assertEqual(self._golden_tree().digest, _GOLDEN_TREE_DIGEST)

    def test_canonical_manifest_bytes_are_pinned(self):
        manifest = self._golden_tree()
        self.assertEqual(
            _normalized_manifest_bytes(manifest), _GOLDEN_BYTES.encode("utf-8")
        )

    def test_entries_are_in_canonical_path_order(self):
        manifest = self._golden_tree()
        paths = [entry.path for entry in manifest.entries]
        self.assertEqual(paths, sorted(paths))
        self.assertEqual(paths, ["a", "a/inner.txt", "m", "m/link", "z.txt"])

    def test_symlinked_root_mapping_is_pinned(self):
        link = self.root.parent / "tree-link"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(LockedNpmError) as ctx:
            tree_module.build_tree_manifest(link)
        self.assertEqual(ctx.exception.reason, "unsafe_cache_path")
        self.assertTrue(
            ctx.exception.detail.startswith(f"unsafe tree root {link}:")
        )

    def test_special_entry_mapping_is_pinned(self):
        os.mkfifo(self.root / "pipe")
        with self.assertRaises(LockedNpmError) as ctx:
            self._build()
        self.assertEqual(ctx.exception.reason, "unsupported_entry_type")
        self.assertEqual(
            ctx.exception.detail,
            "unsupported special filesystem entry at 'pipe'",
        )

    def test_escaping_symlink_mapping_is_pinned(self):
        (self.root / "link").symlink_to("../outside")
        with self.assertRaises(LockedNpmError) as ctx:
            self._build()
        self.assertEqual(ctx.exception.reason, "unsafe_symlink_target")
        self.assertIn("targets outside the tree", ctx.exception.detail)

    def test_verify_hash_mismatch_mapping_is_pinned(self):
        (self.root / "f").write_text("payload")
        manifest = self._build()
        (self.root / "f").write_text("tampered")
        with self.assertRaises(LockedNpmError) as ctx:
            tree_module.verify_tree(self.root, manifest)
        self.assertEqual(ctx.exception.reason, "tree_hash_mismatch")


class TestTreeFoundationBoundary(unittest.TestCase):
    """Task 7.7 — tree.py owns no raw directory close and stays a leaf."""

    def _source(self) -> str:
        return Path(tree_module.__file__).read_text(encoding="utf-8")

    def test_no_raw_owned_directory_close(self):
        tree = ast.parse(self._source())
        closes: list[str] = []
        stack: list[str] = []

        class _Visitor(ast.NodeVisitor):
            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                stack.append(node.name)
                self.generic_visit(node)
                stack.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Call(self, node: ast.Call) -> None:
                if (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "close"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "os"
                ):
                    closes.append(".".join(stack))
                self.generic_visit(node)

        _Visitor().visit(tree)
        # The only raw close is the regular-file hash boundary; every owned
        # directory descriptor is released through the capability.
        self.assertEqual(set(closes), {"_hash_file_entry"})

    def test_imports_only_foundation_and_shared_error(self):
        modules: set[str] = set()
        for node in ast.walk(ast.parse(self._source())):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
        self.assertIn("docker.filesystem.descriptors", modules)
        self.assertIn("docker.filesystem.operations", modules)
        self.assertNotIn("docker.transactions", modules)
        for module in modules:
            if module.startswith("docker"):
                self.assertTrue(
                    module.startswith("docker.filesystem."),
                    f"unexpected foundation import {module!r}",
                )

    def test_tree_still_owns_recursive_traversal(self):
        source = self._source()
        self.assertIn("def _walk_entries", source)
        self.assertIn("_walk_entries(sub, rel, visit)", source)
        self.assertNotIn("_iter_entries", source)
        self.assertNotIn("remove_tree", source)
        self.assertNotIn("rmtree", source)


if __name__ == "__main__":
    unittest.main()
