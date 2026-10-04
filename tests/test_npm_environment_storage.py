"""Phase 2 — assembler cache/staging security (task 2.1).

The assembler cache namespace is owner-private and keyed by assembler
identity.  It prepares an opaque, disposable npm download cache, private
identity-lock files, and contained owner-private staging beneath the
resolved constructor cache without ever chmod-ing a pre-existing ancestor.
Foreign ownership, symlinked control paths, non-directory entries, and
staging escapes are rejected before any container execution or publication.
"""

from __future__ import annotations

import os
import stat
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

_THIS_DIR = Path(__file__).resolve().parent

from docker.npm_environment import LockedNpmError  # noqa: E402
from docker.npm_environment import storage as storage_module  # noqa: E402
from docker.npm_environment.publication import identity_coordination_lock  # noqa: E402
from tests.transactions_test_support import InjectedOps  # noqa: E402


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def _fd_path(fd: int) -> str | None:
    """Return the normalized path an open descriptor refers to, or ``None``."""
    try:
        return os.path.normpath(os.readlink(f"/proc/self/fd/{fd}"))
    except OSError:
        return None


def _foreign_uid_fstat(target: Path, foreign_uid: int):
    """Build an ``os.fstat`` fake that reports *foreign_uid* for *target*."""
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


_ASSEMBLER_DIGEST = "a" * 64
_INPUT_IDENTITY_DIGEST = "b" * 64


class _StorageTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="npm-env-storage-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)

    def _cache_root(self, mode: int | None = None) -> Path:
        root = self.base / "cache-root"
        root.mkdir(parents=True, exist_ok=True)
        if mode is not None:
            os.chmod(root, mode)
        return root

    def _prepare(self, cache_root: Path | None = None) -> storage_module.AssemblerNamespace:
        return storage_module.prepare_assembler_namespace(
            cache_root or self._cache_root(), _ASSEMBLER_DIGEST
        )


class TestAssemblerNamespacePath(unittest.TestCase):
    def test_path_is_lexical_and_deterministic(self):
        root = Path("/var/cache/docker-constructor")
        first = storage_module.assembler_namespace_path(root, _ASSEMBLER_DIGEST)
        second = storage_module.assembler_namespace_path(root, _ASSEMBLER_DIGEST)
        self.assertEqual(first, second)
        self.assertEqual(
            first,
            root / "npm-environments" / "assembler" / _ASSEMBLER_DIGEST,
        )
        self.assertEqual(first.parent, root / "npm-environments" / "assembler")

    def test_digest_token_is_validated(self):
        root = Path("/var/cache/docker-constructor")
        for bad in ("../escape", "a/b", "short", "Z" * 64, "x" * 63, ""):
            with self.subTest(digest=bad):
                with self.assertRaises(LockedNpmError) as ctx:
                    storage_module.assembler_namespace_path(root, bad)
                self.assertEqual(ctx.exception.reason, "unsafe_cache_path")


class TestOwnerPrivateNamespace(_StorageTestCase):
    def test_namespace_and_children_are_0700(self):
        ns = self._prepare()
        for path in (ns.root, ns.npm_cache, ns.locks, ns.staging):
            self.assertTrue(path.is_dir(), f"{path} must be a directory")
            self.assertEqual(
                _mode(path), 0o700, f"{path} must be owner-only 0700"
            )

    def test_npm_cache_is_separate_and_disposable(self):
        ns = self._prepare()
        self.assertNotEqual(ns.npm_cache, ns.staging)
        self.assertNotEqual(ns.npm_cache, ns.locks)
        self.assertEqual(ns.npm_cache.name, "npm-cache")
        self.assertEqual(ns.npm_cache.parent, ns.root)

    def test_assembler_namespaces_are_isolated(self):
        root = self._cache_root()
        ns_a = storage_module.prepare_assembler_namespace(root, "a" * 64)
        ns_b = storage_module.prepare_assembler_namespace(root, "b" * 64)
        self.assertNotEqual(ns_a.root, ns_b.root)
        self.assertEqual(ns_a.root.parent, ns_b.root.parent)

    def test_prepare_is_idempotent_and_preserves_0700(self):
        ns = self._prepare()
        ns.staging.joinpath("probe").write_text("x")
        again = self._prepare()
        self.assertEqual(again.root, ns.root)
        self.assertEqual(_mode(ns.root), 0o700)
        self.assertTrue(ns.staging.joinpath("probe").is_file())


class TestUnchangedAncestors(_StorageTestCase):
    def test_cache_root_mode_unchanged(self):
        root = self._cache_root(mode=0o755)
        self._prepare(root)
        self.assertEqual(_mode(root), 0o755)

    def test_grandparent_mode_unchanged(self):
        root = self._cache_root(mode=0o755)
        self._prepare(root)
        self.assertEqual(_mode(self.base), 0o700)  # tmpdir default

    def test_ancestors_unchanged_under_0700_parent(self):
        root = self._cache_root()
        os.chmod(self.base, 0o700)
        os.chmod(root, 0o700)
        self._prepare(root)
        self.assertEqual(_mode(self.base), 0o700)
        self.assertEqual(_mode(root), 0o700)

    def test_existing_namespace_component_secured_but_ancestor_kept(self):
        root = self._cache_root(mode=0o755)
        pre = root / "npm-environments"
        pre.mkdir()
        os.chmod(pre, 0o755)
        self._prepare(root)
        self.assertEqual(_mode(pre), 0o700)
        self.assertEqual(_mode(root), 0o755)


class TestUnsafeStorageRejected(_StorageTestCase):
    def test_symlinked_cache_root_rejected(self):
        target = self.base / "real"
        target.mkdir()
        link = self.base / "link"
        link.symlink_to(target, target_is_directory=True)
        with self.assertRaises(LockedNpmError) as ctx:
            storage_module.prepare_assembler_namespace(link, _ASSEMBLER_DIGEST)
        self.assertEqual(ctx.exception.reason, "unsafe_cache_path")
        self.assertEqual(list(target.iterdir()), [])

    def test_non_directory_cache_root_rejected(self):
        root = self.base / "file"
        root.write_text("not a directory")
        with self.assertRaises(LockedNpmError) as ctx:
            storage_module.prepare_assembler_namespace(root, _ASSEMBLER_DIGEST)
        self.assertEqual(ctx.exception.reason, "unsafe_cache_path")

    def test_foreign_owned_cache_root_rejected(self):
        root = self._cache_root()
        foreign_uid = os.getuid() + 1
        with mock.patch(
            "os.fstat", side_effect=_foreign_uid_fstat(root, foreign_uid)
        ):
            with self.assertRaises(LockedNpmError) as ctx:
                storage_module.prepare_assembler_namespace(root, _ASSEMBLER_DIGEST)
        self.assertEqual(ctx.exception.reason, "unsafe_cache_path")

    def test_symlinked_namespace_component_rejected(self):
        root = self._cache_root()
        external = self.base / "external"
        external.mkdir()
        (root / "npm-environments").symlink_to(external, target_is_directory=True)
        with self.assertRaises(LockedNpmError) as ctx:
            storage_module.prepare_assembler_namespace(root, _ASSEMBLER_DIGEST)
        self.assertEqual(ctx.exception.reason, "unsafe_cache_path")
        self.assertEqual(list(external.iterdir()), [])

    def test_symlinked_staging_rejected(self):
        root = self._cache_root()
        ns = storage_module.prepare_assembler_namespace(root, _ASSEMBLER_DIGEST)
        ns.staging.rmdir()
        external = self.base / "external"
        external.mkdir()
        ns.staging.symlink_to(external, target_is_directory=True)
        with self.assertRaises(LockedNpmError) as ctx:
            storage_module.prepare_assembler_namespace(root, _ASSEMBLER_DIGEST)
        self.assertEqual(ctx.exception.reason, "unsafe_cache_path")
        self.assertEqual(list(external.iterdir()), [])


class TestIdentityLocks(_StorageTestCase):
    def test_lock_file_is_owner_only(self):
        ns = self._prepare()
        with identity_coordination_lock(ns, _INPUT_IDENTITY_DIGEST):
            path = ns.locks / (_INPUT_IDENTITY_DIGEST + ".lock")
            self.assertTrue(path.is_file())
            self.assertEqual(_mode(path), 0o600)
            self.assertEqual(path.parent, ns.locks)

    def test_lock_rejects_symlink(self):
        ns = self._prepare()
        target = self.base / "target-file"
        target.write_text("x")
        lock = ns.locks / (_INPUT_IDENTITY_DIGEST + ".lock")
        lock.symlink_to(target)
        with self.assertRaises(LockedNpmError) as ctx:
            with identity_coordination_lock(ns, _INPUT_IDENTITY_DIGEST):
                pass
        self.assertEqual(ctx.exception.reason, "unsafe_lock_path")
        self.assertTrue(target.is_file())
        self.assertTrue(lock.is_symlink())

    def test_lock_rejects_directory(self):
        ns = self._prepare()
        (ns.locks / (_INPUT_IDENTITY_DIGEST + ".lock")).mkdir()
        with self.assertRaises(LockedNpmError) as ctx:
            with identity_coordination_lock(ns, _INPUT_IDENTITY_DIGEST):
                pass
        self.assertEqual(ctx.exception.reason, "unsafe_lock_path")

    def test_lock_rejects_foreign_ownership(self):
        ns = self._prepare()
        lock = ns.locks / (_INPUT_IDENTITY_DIGEST + ".lock")
        lock.write_text("owned")
        foreign_uid = os.getuid() + 1
        ops = InjectedOps()

        def override(info):
            if not stat.S_ISREG(info.st_mode):
                return info
            fields = list(info)
            fields[4] = foreign_uid
            return os.stat_result(tuple(fields))

        ops.fstat_override = override
        with self.assertRaises(LockedNpmError) as ctx:
            with identity_coordination_lock(
                ns, _INPUT_IDENTITY_DIGEST, ops=ops
            ):
                pass
        self.assertEqual(ctx.exception.reason, "unsafe_lock_path")
        # The foreign-owned file must not be truncated or rewritten.
        self.assertEqual(lock.read_text(), "owned")

    def test_lock_requires_valid_digest(self):
        ns = self._prepare()
        for bad in ("../x", "a/b", "short", ""):
            with self.subTest(digest=bad):
                with self.assertRaises(LockedNpmError) as ctx:
                    with identity_coordination_lock(ns, bad):
                        pass
                self.assertEqual(ctx.exception.reason, "unsafe_lock_path")


class TestStagingContainment(_StorageTestCase):
    def test_staging_workspace_is_owner_only_and_contained(self):
        ns = self._prepare()
        path = storage_module.prepare_staging_workspace(ns, "run-1")
        self.assertTrue(path.is_dir())
        self.assertEqual(_mode(path), 0o700)
        self.assertEqual(path.parent, ns.staging)
        self.assertEqual(path, ns.staging / "run-1")

    def test_staging_escape_rejected(self):
        ns = self._prepare()
        for bad in ("../escape", "a/b", "/abs", "", ".", ".."):
            with self.subTest(name=bad):
                with self.assertRaises(LockedNpmError) as ctx:
                    storage_module.prepare_staging_workspace(ns, bad)
                self.assertEqual(ctx.exception.reason, "unsafe_staging_path")
        # Nothing escaped beneath the namespace root.
        self.assertFalse((ns.root / "escape").exists())
        self.assertFalse((self.base / "escape").exists())

    def test_staging_rejects_symlink(self):
        ns = self._prepare()
        external = self.base / "external"
        external.mkdir()
        (ns.staging / "run-1").symlink_to(external, target_is_directory=True)
        with self.assertRaises(LockedNpmError) as ctx:
            storage_module.prepare_staging_workspace(ns, "run-1")
        self.assertEqual(ctx.exception.reason, "unsafe_staging_path")
        self.assertEqual(list(external.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
