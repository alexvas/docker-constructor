"""Phase 1 tasks 1.6–1.7 — durable no-clobber and durable replacement.

Durable no-clobber flushes the complete file before publication and the
parent directory before success.  Durable replacement flushes private
sibling state, replaces only an absent or validated owned destination, and
flushes the parent directory before success.  A post-publication parent
fsync failure remains a failure even though the destination is visible.
"""
from __future__ import annotations

import errno
import os
import stat
import tempfile
import unittest

from docker.transactions.capabilities import DirectoryCapability
from docker.transactions.errors import (
    STAGE_CLOSE,
    STAGE_FSYNC_DIRECTORY,
    STAGE_MODE,
    STAGE_REPLACE,
    STAGE_VALIDATE_DESTINATION,
    DestinationExists,
    TransactionError,
    UnsafeFileError,
)
from docker.transactions.regular import RegularFileContracts
from tests.transactions_test_support import InjectedOps, temporary_entries


class _Cancellation(BaseException):
    """A cancellation-style, non-I/O exception."""


class _DurableContractCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.ops = InjectedOps()
        self.contracts = RegularFileContracts(self.ops)
        self.directory = DirectoryCapability.from_path(self.ops, self.root)
        self.addCleanup(self.directory.close)
        self.ops.reset()
        self.addCleanup(self.ops.failures.clear)

    def _read(self, name: str) -> bytes:
        with open(os.path.join(self.root, name), "rb") as stream:
            return stream.read()


class DurableNoClobberTests(_DurableContractCase):
    def test_publishes_complete_bytes_and_mode(self) -> None:
        self.contracts.durable_no_clobber(self.directory, "entry", b"payload", 0o600)
        self.assertEqual(self._read("entry"), b"payload")
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.root, "entry")).st_mode), 0o600)

    def test_file_fsync_precedes_publication_and_parent_fsync_precedes_success(self) -> None:
        self.contracts.durable_no_clobber(self.directory, "entry", b"payload", 0o600)
        fsync_fds = [args[0] for args in self.ops.arg_pairs("fsync")]
        self.assertEqual(len(fsync_fds), 2)
        self.assertNotEqual(fsync_fds[0], self.directory.fd)
        self.assertEqual(fsync_fds[1], self.directory.fd)
        order = self.ops.order
        self.assertLess(order.index("fsync"), order.index("linkat"))
        self.assertGreater(order.index("linkat"), 0)
        self.assertEqual(order.count("fsync"), 2)

    def test_existing_destination_is_typed_collision_and_preserved(self) -> None:
        with open(os.path.join(self.root, "entry"), "wb") as stream:
            stream.write(b"original")
        with self.assertRaises(DestinationExists):
            self.contracts.durable_no_clobber(self.directory, "entry", b"replacement", 0o600)
        self.assertEqual(self._read("entry"), b"original")
        self.assertEqual(temporary_entries(self.root), [])

    def test_post_publication_parent_fsync_failure_remains_failure(self) -> None:
        self.ops.failures["fsync"] = (
            lambda count: OSError(errno.EIO, "injected directory fsync")
            if count == 2 else None
        )
        with self.assertRaises(TransactionError) as ctx:
            self.contracts.durable_no_clobber(self.directory, "entry", b"payload", 0o600)
        self.assertEqual(ctx.exception.stage, STAGE_FSYNC_DIRECTORY)
        self.assertTrue(os.path.exists(os.path.join(self.root, "entry")))
        self.assertEqual(self._read("entry"), b"payload")
        self.assertEqual(temporary_entries(self.root), [])

    def test_file_fsync_failure_prevents_publication(self) -> None:
        self.ops.failures["fsync"] = (
            lambda count: OSError(errno.EIO, "injected file fsync")
            if count == 1 else None
        )
        with self.assertRaises(TransactionError):
            self.contracts.durable_no_clobber(self.directory, "entry", b"payload", 0o600)
        self.assertFalse(os.path.exists(os.path.join(self.root, "entry")))
        self.assertEqual(temporary_entries(self.root), [])


class DurableReplacementTests(_DurableContractCase):
    def test_replaces_absent_destination(self) -> None:
        self.contracts.durable_replace(self.directory, "entry", b"payload", 0o600)
        self.assertEqual(self._read("entry"), b"payload")
        self.assertEqual(temporary_entries(self.root), [])

    def test_replaces_validated_owned_destination(self) -> None:
        with open(os.path.join(self.root, "entry"), "wb") as stream:
            stream.write(b"old")
        os.chmod(os.path.join(self.root, "entry"), 0o600)
        self.contracts.durable_replace(self.directory, "entry", b"new", 0o600)
        self.assertEqual(self._read("entry"), b"new")

    def test_ordering_of_complete_replacement(self) -> None:
        with open(os.path.join(self.root, "entry"), "wb") as stream:
            stream.write(b"old")
        os.chmod(os.path.join(self.root, "entry"), 0o600)
        self.contracts.durable_replace(self.directory, "entry", b"new", 0o600)
        order = self.ops.order
        fsync_fds = [args[0] for args in self.ops.arg_pairs("fsync")]
        self.assertNotEqual(fsync_fds[0], self.directory.fd)
        self.assertEqual(fsync_fds[1], self.directory.fd)
        self.assertLess(order.index("fsync"), order.index("renameat"))
        self.assertGreater(order.index("renameat"), order.index("fstat"))
        self.assertEqual(temporary_entries(self.root), [])

    def test_post_replacement_parent_fsync_failure_remains_failure(self) -> None:
        self.ops.failures["fsync"] = (
            lambda count: OSError(errno.EIO, "injected directory fsync")
            if count == 2 else None
        )
        with self.assertRaises(TransactionError) as ctx:
            self.contracts.durable_replace(self.directory, "entry", b"payload", 0o600)
        self.assertEqual(ctx.exception.stage, STAGE_FSYNC_DIRECTORY)
        self.assertEqual(self._read("entry"), b"payload")
        self.assertEqual(temporary_entries(self.root), [])

    def test_rejects_symlink_destination_without_mutating_target(self) -> None:
        target = os.path.join(self.root, "target")
        with open(target, "wb") as stream:
            stream.write(b"original")
        os.symlink(target, os.path.join(self.root, "entry"))
        with self.assertRaises(UnsafeFileError):
            self.contracts.durable_replace(self.directory, "entry", b"payload", 0o600)
        self.assertEqual(self._read("target"), b"original")
        self.assertTrue(os.path.islink(os.path.join(self.root, "entry")))
        self.assertEqual(temporary_entries(self.root), [])

    def test_destination_validation_precedes_replacement(self) -> None:
        with open(os.path.join(self.root, "entry"), "wb") as stream:
            stream.write(b"old")
        self.contracts.durable_replace(self.directory, "entry", b"new", 0o600)
        order = self.ops.order
        self.assertLess(order.index("fstat"), order.index("renameat"))
        self.assertLess(order.index("fsync"), order.index("renameat"))
        self.assertGreater(order.index("fsync", order.index("renameat")), order.index("renameat"))

    def test_fchmod_failure_is_a_staged_mode_error(self) -> None:
        cause = OSError(errno.EIO, "injected fchmod")
        self.ops.failures["fchmod"] = cause
        with self.assertRaises(TransactionError) as ctx:
            self.contracts.durable_replace(self.directory, "entry", b"new", 0o600)
        self.assertEqual(ctx.exception.stage, STAGE_MODE)
        self.assertIs(ctx.exception.cause, cause)
        self.assertEqual(temporary_entries(self.root), [])

    def test_destination_stat_failure_is_staged(self) -> None:
        with open(os.path.join(self.root, "entry"), "wb") as stream:
            stream.write(b"old")
        cause = OSError(errno.EIO, "injected fstat")
        self.ops.failures["fstat"] = cause
        with self.assertRaises(TransactionError) as ctx:
            self.contracts.durable_replace(self.directory, "entry", b"new", 0o600)
        self.assertEqual(ctx.exception.stage, STAGE_VALIDATE_DESTINATION)
        self.assertIs(ctx.exception.cause, cause)
        self.assertTrue(os.path.exists(os.path.join(self.root, "entry")))
        self.assertEqual(temporary_entries(self.root), [])

    def test_destination_validation_close_failure_is_staged(self) -> None:
        with open(os.path.join(self.root, "entry"), "wb") as stream:
            stream.write(b"old")
        os.chmod(os.path.join(self.root, "entry"), 0o600)
        # close runs for the private temp fd first (count 1) and for the
        # destination-validation descriptor second (count 2).
        self.ops.failures["close"] = (
            lambda count: OSError(errno.EIO, "injected close") if count == 2 else None
        )
        with self.assertRaises(TransactionError) as ctx:
            self.contracts.durable_replace(self.directory, "entry", b"new", 0o600)
        self.assertEqual(ctx.exception.stage, STAGE_CLOSE)
        self.assertEqual(self._read("entry"), b"old")
        self.assertEqual(temporary_entries(self.root), [])

    def test_destination_validation_close_interruption_propagates(self) -> None:
        with open(os.path.join(self.root, "entry"), "wb") as stream:
            stream.write(b"old")
        os.chmod(os.path.join(self.root, "entry"), 0o600)
        interruption = KeyboardInterrupt()
        self.ops.failures["close"] = (
            lambda count: interruption if count == 2 else None
        )
        with self.assertRaises(KeyboardInterrupt) as ctx:
            self.contracts.durable_replace(self.directory, "entry", b"new", 0o600)
        self.assertIs(ctx.exception, interruption)
        self.assertEqual(self._read("entry"), b"old")
        self.assertEqual(temporary_entries(self.root), [])

    def test_destination_validation_close_cancellation_propagates(self) -> None:
        with open(os.path.join(self.root, "entry"), "wb") as stream:
            stream.write(b"old")
        os.chmod(os.path.join(self.root, "entry"), 0o600)
        cancellation = _Cancellation()
        self.ops.failures["close"] = (
            lambda count: cancellation if count == 2 else None
        )
        with self.assertRaises(_Cancellation) as ctx:
            self.contracts.durable_replace(self.directory, "entry", b"new", 0o600)
        self.assertIs(ctx.exception, cancellation)
        self.assertEqual(self._read("entry"), b"old")
        self.assertEqual(temporary_entries(self.root), [])

    def test_existing_destination_is_continuously_present(self) -> None:
        dest = os.path.join(self.root, "entry")
        with open(dest, "wb") as stream:
            stream.write(b"old")
        os.chmod(dest, 0o600)
        observed: list[bool] = []

        def observe(*args):
            observed.append(os.path.exists(dest))

        for method in ("fchmod", "write", "fsync", "openat", "fstat", "close", "renameat"):
            self.ops.hooks[method] = observe
        self.contracts.durable_replace(self.directory, "entry", b"new", 0o600)
        self.assertTrue(observed)
        self.assertTrue(all(observed), "destination was observably absent during replacement")
        self.assertEqual(self._read("entry"), b"new")
        # A replacement never unlinks the destination, so the name is never absent.
        self.assertNotIn("unlinkat", self.ops.order)

    def test_replacement_has_a_single_atomic_commit_boundary(self) -> None:
        with open(os.path.join(self.root, "entry"), "wb") as stream:
            stream.write(b"old")
        os.chmod(os.path.join(self.root, "entry"), 0o600)
        self.contracts.durable_replace(self.directory, "entry", b"new", 0o600)
        renames = self.ops.arg_pairs("renameat")
        self.assertEqual(len(renames), 1)
        _old_dir_fd, temp_name, _new_dir_fd, new_name = renames[0]
        self.assertEqual(new_name, "entry")
        self.assertTrue(str(temp_name).startswith(".transaction-"))
        self.assertNotIn("linkat", self.ops.order)

    def test_private_name_collision_never_modifies_collided_entry(self) -> None:
        collided: list[str] = []

        def collide(dir_fd, name, flags, mode):
            if name.startswith(".transaction-") and not collided:
                collided.append(name)
                with open(os.path.join(self.root, name), "wb") as stream:
                    stream.write(b"collided")

        self.ops.hooks["openat"] = collide
        self.contracts.durable_replace(self.directory, "entry", b"new", 0o600)
        self.assertEqual(len(collided), 1)
        with open(os.path.join(self.root, collided[0]), "rb") as stream:
            self.assertEqual(stream.read(), b"collided")
        self.assertNotIn(collided[0], self.ops.names("unlinkat"))
        self.assertNotIn("read", self.ops.order)
        self.assertEqual(self._read("entry"), b"new")

    def test_replacement_failure_leaves_destination_in_place(self) -> None:
        with open(os.path.join(self.root, "entry"), "wb") as stream:
            stream.write(b"old")
        os.chmod(os.path.join(self.root, "entry"), 0o600)
        self.ops.failures["renameat"] = PermissionError(errno.EACCES, "injected rename")
        with self.assertRaises(TransactionError) as ctx:
            self.contracts.durable_replace(self.directory, "entry", b"new", 0o600)
        self.assertEqual(ctx.exception.stage, STAGE_REPLACE)
        self.assertEqual(self._read("entry"), b"old")
        self.assertEqual(temporary_entries(self.root), [])

    def test_failed_commit_never_overwrites_concurrent_creation(self) -> None:
        created: list[bool] = []

        def concurrent(old_dir_fd, old, new_dir_fd, new):
            if not created:
                created.append(True)
                with open(os.path.join(self.root, new), "wb") as stream:
                    stream.write(b"concurrent")

        self.ops.hooks["renameat"] = concurrent
        self.ops.failures["renameat"] = PermissionError(errno.EACCES, "injected rename")
        with self.assertRaises(TransactionError) as ctx:
            self.contracts.durable_replace(self.directory, "entry", b"new", 0o600)
        self.assertEqual(ctx.exception.stage, STAGE_REPLACE)
        self.assertEqual(self._read("entry"), b"concurrent")
        self.assertEqual(temporary_entries(self.root), [])

    def test_temporary_cleanup_failure_is_tracked_and_reported(self) -> None:
        with open(os.path.join(self.root, "entry"), "wb") as stream:
            stream.write(b"old")
        os.chmod(os.path.join(self.root, "entry"), 0o600)
        primary = PermissionError(errno.EACCES, "injected rename")
        cleanup = OSError(errno.EIO, "injected unlink")
        self.ops.failures["renameat"] = primary
        self.ops.failures["unlinkat"] = cleanup
        with self.assertRaises(TransactionError) as ctx:
            self.contracts.durable_replace(self.directory, "entry", b"new", 0o600)
        self.assertEqual(ctx.exception.stage, STAGE_REPLACE)
        self.assertIs(ctx.exception.cause, primary)
        self.assertIn(cleanup, ctx.exception.secondary)
        self.assertEqual(len(temporary_entries(self.root)), 1)

if __name__ == "__main__":  # pragma: no cover
    unittest.main()
