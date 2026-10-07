"""Phase 2 — assembler cache/staging security (task 2.1).

The assembler cache namespace is owner-private and keyed by assembler
identity.  It prepares an opaque, disposable npm download cache, private
identity-lock files, and contained owner-private staging beneath the
resolved constructor cache without ever chmod-ing a pre-existing ancestor.
Foreign ownership, symlinked control paths, non-directory entries, and
staging escapes are rejected before any container execution or publication.
"""

from __future__ import annotations

import ast
import contextlib
import errno
import os
import stat
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

_THIS_DIR = Path(__file__).resolve().parent

from docker.filesystem.descriptors import (  # noqa: E402
    DescriptorError,
    _STAGE_CREATE,
    _STAGE_REOPEN,
)
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


def _raw_cause(exc: BaseException) -> BaseException | None:
    """Return the causal POSIX failure regardless of the failing layer.

    After the Phase 6 migration the domain error chains from a foundation
    ``DescriptorError``; before it, the domain error chained directly from the
    raw ``OSError``.  Normalizing here lets one exact-detail characterization
    pass against both implementations (task 6.4).
    """
    cause = exc.__cause__
    if isinstance(cause, DescriptorError):
        return cause.cause
    return cause


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


# ── Phase 6 — descriptor-foundation migration contracts ────────────────


class _LedgerOps(InjectedOps):
    """An injecting backend that tracks live descriptors for leak detection.

    ``openat`` records every returned descriptor as live; ``close`` flags an
    attempt on a descriptor that is not live (a double close) and clears it on
    the way out.  This distinguishes a leaked descriptor from a retried close
    across descriptor-number reuse.
    """

    def __init__(self) -> None:
        super().__init__()
        self.open_order: list[int] = []
        self.open_paths: list[tuple[int, str | None]] = []
        self.close_order: list[int] = []
        self.close_paths: list[str | None] = []
        self.live: set[int] = set()
        self.double_closes: list[int] = []

    def fd_for(self, path: Path) -> int:
        """Return the descriptor recorded for the successful open of *path*."""
        norm = os.path.normpath(str(path))
        matches = [
            fd for fd, resolved in self.open_paths if resolved == norm
        ]
        if not matches:
            raise AssertionError(f"no descriptor opened for {path!r}")
        return matches[0]

    def openat(self, dir_fd, name, flags, mode=0o777):
        fd = super().openat(dir_fd, name, flags, mode)
        self.open_order.append(fd)
        self.open_paths.append((fd, _fd_path(fd)))
        self.live.add(fd)
        return fd

    def mkdirat(self, dir_fd, name, mode):
        self._record("mkdirat", dir_fd, name, mode)
        return super().mkdirat(dir_fd, name, mode)

    def statat(self, dir_fd, name, *, follow_symlinks):
        self._record("statat", dir_fd, name)
        return super().statat(dir_fd, name, follow_symlinks=follow_symlinks)

    def listdir(self, fd):
        self._record("listdir", fd)
        return super().listdir(fd)

    def rmdirat(self, dir_fd, name):
        self._record("rmdirat", dir_fd, name)
        return super().rmdirat(dir_fd, name)

    def close(self, fd):
        self.close_order.append(fd)
        self.close_paths.append(_fd_path(fd))
        if fd not in self.live:
            self.double_closes.append(fd)
        try:
            return super().close(fd)
        finally:
            self.live.discard(fd)


class TestSecureWalkHandoff(_StorageTestCase):
    """Task 6.1 — secure-walk parent-close failure is not retried."""

    def test_parent_close_failure_is_not_retried_and_child_is_released(self):
        root = self._cache_root()
        ops = _LedgerOps()
        failure = OSError(errno.EIO, "injected parent close failure")
        ops.failures["close"] = lambda count: failure if count == 1 else None
        with self.assertRaises(OSError) as ctx:
            storage_module.prepare_assembler_namespace(
                root, _ASSEMBLER_DIGEST, ops=ops
            )
        # The handoff close failure keeps its raw identity rather than being
        # retried or demoted to a diagnostic.
        self.assertIs(ctx.exception, failure)
        parent_fd, child_fd = ops.open_order[0], ops.open_order[1]
        self.assertEqual(ops.close_order.count(parent_fd), 1)
        self.assertIn(child_fd, ops.close_order)
        self.assertEqual(ops.double_closes, [])
        self.assertEqual(ops.live, set())
        # The namespace was never created past the failing walk.
        self.assertFalse(
            (root / storage_module.NPM_ENVIRONMENTS_CHILD).exists()
        )


class TestNamespaceHandoffOwnership(_StorageTestCase):
    """Task 6.1 — nested handoff cannot strand a descendant descriptor.

    ``prepare_assembler_namespace`` nests each child capability inside its
    parent's ``with`` block.  If closing ``env`` (after the assembler/digest
    subtree exists) or ``assembler`` (after the digest exists) fails, the
    already-created descendants must still be released before the failure
    propagates.
    """

    def _inject_close_failure_on(
        self, ops: _LedgerOps, target: Path, failure: OSError
    ) -> str:
        """Arm *failure* for the close of the descriptor at *target*.

        The predicate keys off the descriptor's resolved path, so it is
        immune to file-descriptor number reuse during the secure walk.
        """
        norm = os.path.normpath(str(target))

        def spec(count):
            if ops.close_order and _fd_path(ops.close_order[-1]) == norm:
                return failure
            return None

        ops.failures["close"] = spec
        return norm

    def _assert_no_descriptor_leaked(self, ops: _LedgerOps) -> None:
        """Every descriptor was released at most once and none is live."""
        self.assertEqual(ops.double_closes, [])
        self.assertEqual(ops.live, set())
        for _, opened in ops.open_paths:
            if opened is not None:
                self.assertLessEqual(
                    ops.close_paths.count(opened), 1, opened
                )

    def test_env_close_failure_releases_assembler_and_digest(self):
        root = self._cache_root()
        ops = _LedgerOps()
        failure = OSError(errno.EIO, "injected env close failure")
        env_path = root / storage_module.NPM_ENVIRONMENTS_CHILD
        self._inject_close_failure_on(ops, env_path, failure)

        with self.assertRaises(OSError) as ctx:
            storage_module.prepare_assembler_namespace(
                root, _ASSEMBLER_DIGEST, ops=ops
            )

        self.assertIs(ctx.exception, failure)
        assembler_norm = os.path.normpath(
            str(env_path / storage_module.ASSEMBLER_CHILD)
        )
        digest_norm = os.path.normpath(
            str(
                env_path
                / storage_module.ASSEMBLER_CHILD
                / _ASSEMBLER_DIGEST
            )
        )
        self.assertIn(
            ops.fd_for(env_path / storage_module.ASSEMBLER_CHILD),
            ops.close_order,
        )
        self.assertIn(
            ops.fd_for(
                env_path
                / storage_module.ASSEMBLER_CHILD
                / _ASSEMBLER_DIGEST
            ),
            ops.close_order,
        )
        self.assertEqual(ops.close_paths.count(assembler_norm), 1)
        self.assertEqual(ops.close_paths.count(digest_norm), 1)
        self._assert_no_descriptor_leaked(ops)

    def test_assembler_close_failure_releases_digest(self):
        root = self._cache_root()
        ops = _LedgerOps()
        failure = OSError(errno.EIO, "injected assembler close failure")
        assembler_path = (
            root
            / storage_module.NPM_ENVIRONMENTS_CHILD
            / storage_module.ASSEMBLER_CHILD
        )
        self._inject_close_failure_on(ops, assembler_path, failure)

        with self.assertRaises(OSError) as ctx:
            storage_module.prepare_assembler_namespace(
                root, _ASSEMBLER_DIGEST, ops=ops
            )

        self.assertIs(ctx.exception, failure)
        digest_path = assembler_path / _ASSEMBLER_DIGEST
        self.assertIn(ops.fd_for(digest_path), ops.close_order)
        self.assertEqual(
            ops.close_paths.count(os.path.normpath(str(digest_path))), 1
        )
        self._assert_no_descriptor_leaked(ops)


class TestStagingPreparationPrecedence(_StorageTestCase):
    """Task 6.2 — staging preparation failure precedence."""

    def _namespace(self):
        return self._prepare()

    def _staging_ops(self, *, arm_on, close_error, **injections):
        """Backend whose close failures are armed only after *arm_on* runs.

        The secure walk to the staging parent releases intermediate
        descriptors, so close faults must not fire until the workspace has
        been reached; otherwise the walk itself would fail before staging
        preparation is exercised.
        """
        ops = _LedgerOps()
        armed = {"on": False}
        ops.hooks[arm_on] = lambda *args: armed.__setitem__("on", True)
        for name, exc in injections.items():
            ops.failures[name] = exc
        ops.failures["close"] = (
            lambda count: close_error if armed["on"] else None
        )
        return ops

    def test_fchmod_failure_is_primary_with_close_secondary(self):
        ns = self._namespace()
        fchmod_error = OSError(errno.EIO, "injected fchmod failure")
        close_error = OSError(errno.EIO, "injected close failure")
        ops = self._staging_ops(
            arm_on="fchmod", close_error=close_error, fchmod=fchmod_error
        )
        with self.assertRaises(LockedNpmError) as ctx:
            storage_module.prepare_staging_workspace(ns, "run-1", ops=ops)
        self.assertEqual(ctx.exception.reason, "unsafe_staging_path")
        foundation = ctx.exception.__cause__
        self.assertIsInstance(foundation, DescriptorError)
        self.assertIs(foundation.cause, fchmod_error)
        self.assertIn(
            close_error, getattr(foundation, "_transaction_secondary", [])
        )
        self.assertEqual(ops.double_closes, [])
        # The exclusive create reached the filesystem before fchmod failed, so
        # the residue is left in place for cancellation cleanup to observe.
        self.assertTrue((ns.staging / "run-1").is_dir())

    def test_mkdir_failure_is_primary_with_close_secondary(self):
        ns = self._namespace()
        mkdir_error = PermissionError(errno.EACCES, "injected mkdir failure")
        close_error = OSError(errno.EIO, "injected close failure")
        ops = self._staging_ops(
            arm_on="mkdirat", close_error=close_error, mkdirat=mkdir_error
        )
        with self.assertRaises(LockedNpmError) as ctx:
            storage_module.prepare_staging_workspace(ns, "run-1", ops=ops)
        self.assertEqual(ctx.exception.reason, "unsafe_staging_path")
        foundation = ctx.exception.__cause__
        self.assertIsInstance(foundation, DescriptorError)
        self.assertIs(foundation.cause, mkdir_error)
        self.assertTrue(
            any("injected close failure" in note for note in ctx.exception.__notes__),
            ctx.exception.__notes__,
        )
        self.assertEqual(ops.double_closes, [])

    def test_open_failure_is_primary_with_close_secondary(self):
        ns = self._namespace()
        open_error = OSError(errno.EIO, "injected open failure")
        close_error = OSError(errno.EIO, "injected close failure")
        ops = self._staging_ops(arm_on="mkdirat", close_error=close_error)
        original_openat = ops.openat

        def failing_openat(dir_fd, name, flags, mode=0o777):
            if name == "run-1":
                raise open_error
            return original_openat(dir_fd, name, flags, mode)

        ops.openat = failing_openat
        with self.assertRaises(LockedNpmError) as ctx:
            storage_module.prepare_staging_workspace(ns, "run-1", ops=ops)
        self.assertEqual(ctx.exception.reason, "unsafe_staging_path")
        foundation = ctx.exception.__cause__
        self.assertIsInstance(foundation, DescriptorError)
        self.assertIs(foundation.cause, open_error)
        self.assertTrue(
            any("injected close failure" in note for note in ctx.exception.__notes__),
            ctx.exception.__notes__,
        )
        self.assertEqual(ops.double_closes, [])


    def test_existing_workspace_is_rejected_without_reuse(self):
        ns = self._namespace()
        first = storage_module.prepare_staging_workspace(ns, "run-1")
        marker = first / "residue"
        marker.mkdir()
        with self.assertRaises(LockedNpmError) as ctx:
            storage_module.prepare_staging_workspace(ns, "run-1")
        self.assertEqual(ctx.exception.reason, "unsafe_staging_path")
        self.assertIn("already exists", ctx.exception.detail)
        self.assertTrue(marker.is_dir())


class TestCreateVersusReopenDiagnostics(_StorageTestCase):
    """Tasks 6.6/6.8 — creation and post-create reopen keep distinct wording.

    ``DirectoryDescriptor.create_directory`` performs an exclusive ``mkdirat``
    and then reopens the entry; ``open_or_create_directory`` additionally
    opens an existing entry first.  These are three operations producing
    ``DescriptorError`` from the same ``OSError`` type, so the module pins
    ``DescriptorError`` stages to keep the pre-migration ``LockedNpmError``
    diagnostics instead of collapsing them into one message.
    """

    def _armed_close_ops(self, arm_on: str, close_error: OSError) -> _LedgerOps:
        """Ledger backend whose close failures arm only after *arm_on* runs."""
        ops = _LedgerOps()
        armed = {"on": False}
        ops.hooks[arm_on] = lambda *args: armed.__setitem__("on", True)
        ops.failures["close"] = (
            lambda count: close_error if armed["on"] else None
        )
        return ops

    def _fail_open_attempt(
        self, ops: _LedgerOps, target: str, error: OSError, *, attempt: int
    ) -> None:
        """Make the *attempt*-th ``openat(target)`` raise *error*."""
        original = ops.openat
        seen = {"count": 0}

        def wrapper(dir_fd, name, flags, mode=0o777):
            if name == target:
                seen["count"] += 1
                if seen["count"] == attempt:
                    raise error
            return original(dir_fd, name, flags, mode)

        ops.openat = wrapper

    def _assert_secondary_close(self, primary, close_error: OSError) -> None:
        """The active failure stays primary; the close failure is a note."""
        notes = getattr(primary, "__notes__", [])
        self.assertTrue(
            any(str(close_error) in note for note in notes), notes
        )

    # -- namespace child (open_or_create_directory) ---------------------

    def test_namespace_child_create_failure_keeps_cannot_create_wording(self):
        root = self._cache_root()
        mkdir_error = PermissionError(errno.EACCES, "injected mkdir failure")
        close_error = OSError(errno.EIO, "injected close failure")
        ops = self._armed_close_ops("mkdirat", close_error)
        ops.failures["mkdirat"] = mkdir_error

        with self.assertRaises(LockedNpmError) as ctx:
            storage_module.prepare_assembler_namespace(
                root, _ASSEMBLER_DIGEST, ops=ops
            )

        label = root / storage_module.NPM_ENVIRONMENTS_CHILD
        self.assertEqual(ctx.exception.reason, "unsafe_cache_path")
        self.assertEqual(
            ctx.exception.detail,
            f"cannot create cache directory {label!r}: {mkdir_error}",
        )
        foundation = ctx.exception.__cause__
        self.assertIsInstance(foundation, DescriptorError)
        self.assertEqual(foundation.stage, _STAGE_CREATE)
        self.assertIs(foundation.cause, mkdir_error)
        self.assertIs(foundation.__cause__, mkdir_error)
        self._assert_secondary_close(ctx.exception, close_error)
        self.assertEqual(ops.double_closes, [])
        self.assertEqual(ops.live, set())

    def test_namespace_child_reopen_failure_keeps_unsafe_cache_entry_wording(
        self,
    ):
        root = self._cache_root()
        reopen_error = OSError(errno.EIO, "injected reopen failure")
        close_error = OSError(errno.EIO, "injected close failure")
        ops = self._armed_close_ops("mkdirat", close_error)
        # The first open reports ENOENT and triggers the exclusive create; the
        # second open is the post-create reopen.
        self._fail_open_attempt(
            ops,
            storage_module.NPM_ENVIRONMENTS_CHILD,
            reopen_error,
            attempt=2,
        )

        with self.assertRaises(LockedNpmError) as ctx:
            storage_module.prepare_assembler_namespace(
                root, _ASSEMBLER_DIGEST, ops=ops
            )

        label = root / storage_module.NPM_ENVIRONMENTS_CHILD
        self.assertEqual(ctx.exception.reason, "unsafe_cache_path")
        self.assertEqual(
            ctx.exception.detail,
            f"unsafe cache entry {label!r}: {reopen_error}",
        )
        foundation = ctx.exception.__cause__
        self.assertIsInstance(foundation, DescriptorError)
        self.assertEqual(foundation.stage, _STAGE_REOPEN)
        self.assertIs(foundation.cause, reopen_error)
        self.assertIs(foundation.__cause__, reopen_error)
        self._assert_secondary_close(ctx.exception, close_error)
        self.assertEqual(ops.double_closes, [])
        self.assertEqual(ops.live, set())

    # -- staging workspace (create_directory) ---------------------------

    def test_staging_create_failure_keeps_cannot_create_wording(self):
        ns = self._prepare()
        mkdir_error = PermissionError(errno.EACCES, "injected mkdir failure")
        close_error = OSError(errno.EIO, "injected close failure")
        ops = self._armed_close_ops("mkdirat", close_error)
        ops.failures["mkdirat"] = mkdir_error

        with self.assertRaises(LockedNpmError) as ctx:
            storage_module.prepare_staging_workspace(ns, "run-1", ops=ops)

        self.assertEqual(ctx.exception.reason, "unsafe_staging_path")
        self.assertEqual(
            ctx.exception.detail,
            f"cannot create staging workspace 'run-1': {mkdir_error}",
        )
        foundation = ctx.exception.__cause__
        self.assertIsInstance(foundation, DescriptorError)
        self.assertEqual(foundation.stage, _STAGE_CREATE)
        self.assertIs(foundation.cause, mkdir_error)
        self.assertIs(foundation.__cause__, mkdir_error)
        self._assert_secondary_close(ctx.exception, close_error)
        self.assertEqual(ops.double_closes, [])
        self.assertEqual(ops.live, set())

    def test_staging_reopen_failure_keeps_unsafe_staging_workspace_wording(
        self,
    ):
        ns = self._prepare()
        reopen_error = OSError(errno.EIO, "injected reopen failure")
        close_error = OSError(errno.EIO, "injected close failure")
        ops = self._armed_close_ops("mkdirat", close_error)
        # ``create_directory`` issues a single open after the exclusive mkdir.
        self._fail_open_attempt(ops, "run-1", reopen_error, attempt=1)

        with self.assertRaises(LockedNpmError) as ctx:
            storage_module.prepare_staging_workspace(ns, "run-1", ops=ops)

        self.assertEqual(ctx.exception.reason, "unsafe_staging_path")
        self.assertEqual(
            ctx.exception.detail,
            f"unsafe staging workspace 'run-1': {reopen_error}",
        )
        foundation = ctx.exception.__cause__
        self.assertIsInstance(foundation, DescriptorError)
        self.assertEqual(foundation.stage, _STAGE_REOPEN)
        self.assertIs(foundation.cause, reopen_error)
        self.assertIs(foundation.__cause__, reopen_error)
        self._assert_secondary_close(ctx.exception, close_error)
        self.assertEqual(ops.double_closes, [])
        self.assertEqual(ops.live, set())


class TestRecursiveRemoval(_StorageTestCase):
    """Task 6.3 — recursive removal precedence and absence policy."""

    def _workspace_with_nested_entry(self):
        ns = self._prepare()
        workspace = storage_module.prepare_staging_workspace(ns, "run-1")
        inner = workspace / "inner"
        inner.mkdir()
        (inner / "leaf.txt").write_text("data")
        return ns, workspace

    def test_removal_failure_is_primary_and_each_directory_closes_once(self):
        ns, workspace = self._workspace_with_nested_entry()
        ops = _LedgerOps()
        error = OSError(errno.EIO, "injected unlink failure")
        ops.failures["unlinkat"] = error
        with self.assertRaises(OSError) as ctx:
            storage_module.remove_staging_workspace(ns, "run-1", ops=ops)
        self.assertIs(ctx.exception, error)
        self.assertEqual(ops.double_closes, [])
        self.assertEqual(ops.live, set())
        self.assertTrue(workspace.is_dir())

    def test_traversal_failure_is_primary_and_directories_close(self):
        ns, workspace = self._workspace_with_nested_entry()
        ops = _LedgerOps()
        error = OSError(errno.EIO, "injected listdir failure")
        ops.failures["listdir"] = error
        with self.assertRaises(OSError) as ctx:
            storage_module.remove_staging_workspace(ns, "run-1", ops=ops)
        self.assertIs(ctx.exception, error)
        self.assertEqual(ops.double_closes, [])
        self.assertEqual(ops.live, set())
        self.assertTrue(workspace.is_dir())

    def test_missing_workspace_is_a_noop(self):
        ns = self._prepare()
        storage_module.remove_staging_workspace(ns, "absent")
        self.assertFalse((ns.staging / "absent").exists())

    def test_full_tree_is_removed_with_one_close_per_directory(self):
        ns, workspace = self._workspace_with_nested_entry()
        ops = _LedgerOps()
        storage_module.remove_staging_workspace(ns, "run-1", ops=ops)
        self.assertFalse(workspace.exists())
        self.assertEqual(ops.double_closes, [])
        self.assertEqual(ops.live, set())


class TestPreMigrationCharacterization(_StorageTestCase):
    """Task 6.4 — stable paths, modes, diagnostics, and operation order."""

    def test_namespace_children_are_created_in_sequence(self):
        root = self._cache_root()
        created: list[str] = []
        real_mkdir = os.mkdir

        def recording_mkdir(path, mode=0o777, *, dir_fd=None):
            created.append(os.fspath(path))
            return real_mkdir(path, mode, dir_fd=dir_fd)

        with mock.patch("os.mkdir", side_effect=recording_mkdir):
            storage_module.prepare_assembler_namespace(root, _ASSEMBLER_DIGEST)
        self.assertEqual(
            created,
            [
                storage_module.NPM_ENVIRONMENTS_CHILD,
                storage_module.ASSEMBLER_CHILD,
                _ASSEMBLER_DIGEST,
                storage_module.NPM_CACHE_CHILD,
                storage_module.LOCKS_CHILD,
                storage_module.STAGING_CHILD,
                storage_module.OUTPUTS_CHILD,
                storage_module.INDEX_CHILD,
            ],
        )

    def test_symlinked_cache_root_detail_is_pinned(self):
        target = self.base / "real"
        target.mkdir()
        link = self.base / "link"
        link.symlink_to(target, target_is_directory=True)
        with self.assertRaises(LockedNpmError) as ctx:
            storage_module.prepare_assembler_namespace(link, _ASSEMBLER_DIGEST)
        raw = _raw_cause(ctx.exception)
        self.assertEqual(ctx.exception.reason, "unsafe_cache_path")
        self.assertEqual(
            ctx.exception.detail,
            f"unsafe cache component 'link' in "
            f"{os.path.abspath(str(link))!r} "
            f"(symlink or non-directory): {raw}",
        )
        self.assertEqual(list(target.iterdir()), [])

    def test_symlinked_namespace_component_detail_is_pinned(self):
        root = self._cache_root()
        external = self.base / "external"
        external.mkdir()
        component = root / storage_module.NPM_ENVIRONMENTS_CHILD
        component.symlink_to(external, target_is_directory=True)
        with self.assertRaises(LockedNpmError) as ctx:
            storage_module.prepare_assembler_namespace(root, _ASSEMBLER_DIGEST)
        raw = _raw_cause(ctx.exception)
        self.assertEqual(ctx.exception.reason, "unsafe_cache_path")
        self.assertEqual(
            ctx.exception.detail,
            f"unsafe cache entry {component!r} "
            f"(symlink or non-directory): {raw}",
        )
        self.assertEqual(list(external.iterdir()), [])

    def test_symlinked_staging_component_detail_is_pinned(self):
        ns = self._prepare()
        ns.staging.rmdir()
        external = self.base / "external"
        external.mkdir()
        ns.staging.symlink_to(external, target_is_directory=True)
        with self.assertRaises(LockedNpmError) as ctx:
            storage_module.prepare_staging_workspace(ns, "run-1")
        raw = _raw_cause(ctx.exception)
        self.assertEqual(ctx.exception.reason, "unsafe_cache_path")
        self.assertEqual(
            ctx.exception.detail,
            f"unsafe cache component 'staging' in "
            f"{os.path.abspath(str(ns.staging))!r} "
            f"(symlink or non-directory): {raw}",
        )
        self.assertEqual(list(external.iterdir()), [])

    def test_foreign_owned_cache_root_detail_is_pinned(self):
        root = self._cache_root()
        foreign_uid = os.getuid() + 1
        with mock.patch(
            "os.fstat", side_effect=_foreign_uid_fstat(root, foreign_uid)
        ):
            with self.assertRaises(LockedNpmError) as ctx:
                storage_module.prepare_assembler_namespace(
                    root, _ASSEMBLER_DIGEST
                )
        self.assertEqual(ctx.exception.reason, "unsafe_cache_path")
        self.assertEqual(
            ctx.exception.detail,
            f"{root} is not owned by the invoking user; "
            "restore ownership or remove the entry",
        )

    def test_foreign_owned_relative_cache_root_uses_caller_path(self):
        root = self._cache_root()
        foreign_uid = os.getuid() + 1
        with contextlib.chdir(self.base):
            with mock.patch(
                "os.fstat", side_effect=_foreign_uid_fstat(root, foreign_uid)
            ):
                with self.assertRaises(LockedNpmError) as ctx:
                    storage_module.prepare_assembler_namespace(
                        Path("cache-root"), _ASSEMBLER_DIGEST
                    )
        self.assertEqual(
            ctx.exception.detail,
            "cache-root is not owned by the invoking user; "
            "restore ownership or remove the entry",
        )

    def test_staging_workspace_is_pinned_and_owner_only(self):
        ns = self._prepare()
        workspace = storage_module.prepare_staging_workspace(ns, "run-1")
        self.assertEqual(workspace, ns.staging / "run-1")
        self.assertEqual(_mode(workspace), 0o700)

    def test_namespace_paths_and_modes_are_pinned(self):
        ns = self._prepare()
        self.assertEqual(
            ns.root,
            self._cache_root_path()
            / storage_module.NPM_ENVIRONMENTS_CHILD
            / storage_module.ASSEMBLER_CHILD
            / _ASSEMBLER_DIGEST,
        )
        for path in (ns.root, ns.npm_cache, ns.locks, ns.staging,
                     ns.outputs, ns.index):
            self.assertTrue(path.is_dir(), path)
            self.assertEqual(_mode(path), 0o700, path)

    def _cache_root_path(self) -> Path:
        # ``_prepare`` created the root beneath ``self.base``.
        return self.base / "cache-root"


class TestStorageFoundationBoundary(unittest.TestCase):
    """Tasks 6.10 and 6.11 — foundation dependency and domain ownership."""

    def _source(self) -> str:
        return Path(storage_module.__file__).read_text()

    def test_storage_has_no_raw_owned_directory_close(self):
        source = self._source()
        tree = ast.parse(source)
        raw_close = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "close"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "os"
        ]
        self.assertEqual(raw_close, [])
        self.assertNotIn("os.close", source)

    def test_storage_imports_only_foundation_and_shared_error(self):
        modules = self._imported_modules(self._source())
        self.assertIn("docker.filesystem.descriptors", modules)
        self.assertIn("docker.filesystem.operations", modules)
        self.assertNotIn("docker.transactions", modules)
        for module in modules:
            if module.startswith("docker"):
                self.assertTrue(
                    module.startswith("docker.filesystem."),
                    f"unexpected foundation import {module!r}",
                )

    @staticmethod
    def _imported_modules(source: str) -> set[str]:
        modules: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
        return modules

    def test_foundation_owns_no_npm_domain_tokens(self):
        foundation = Path(__file__).resolve().parents[1] / "docker" / "filesystem"
        forbidden = (
            "LockedNpmError",
            "npm-environments",
            "npm-cache",
            "unsafe_cache_path",
            "unsafe_staging_path",
            "AssemblerNamespace",
            "prepare_assembler_namespace",
            "prepare_staging_workspace",
            "remove_staging_workspace",
            "remove_tree",
            "rmtree",
            "assembler",
        )
        for path in sorted(foundation.glob("*.py")):
            source = path.read_text()
            for token in forbidden:
                self.assertNotIn(token, source, f"{path.name} contains {token!r}")


if __name__ == "__main__":
    unittest.main()
