"""Phase 1 tasks 1.4–1.5 — atomic no-clobber publication and temp allocation.

Atomic no-clobber makes complete bytes with the final mode visible without
replacing an existing entry, performs no parent-directory durability fsync,
and reserves the typed ``DESTINATION_EXISTS`` outcome for the final commit.
Temporary allocation tries a fresh private name at most three times; a
temporary ``EEXIST`` is never surfaced as destination contention.
"""
from __future__ import annotations

import errno
import os
import stat
import tempfile
import unittest

from docker.transactions.capabilities import DirectoryCapability
from docker.transactions.errors import (
    STAGE_ALLOCATE,
    STAGE_CLOSE,
    STAGE_COMMIT,
    STAGE_MODE,
    STAGE_WRITE,
    DestinationExists,
    TransactionError,
)
from docker.transactions.regular import RegularFileContracts
from tests.transactions_test_support import InjectedOps, temporary_entries


class AtomicNoClobberTests(unittest.TestCase):
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

    def test_publishes_complete_bytes(self) -> None:
        self.contracts.atomic_no_clobber(self.directory, "entry", b"payload", 0o600)
        with open(os.path.join(self.root, "entry"), "rb") as stream:
            self.assertEqual(stream.read(), b"payload")
        self.assertEqual(temporary_entries(self.root), [])

    def test_establishes_final_mode_before_visibility(self) -> None:
        observed: list[int] = []

        def before_link(src_dir_fd, src, dst_dir_fd, dst):
            observed.append(stat.S_IMODE(os.stat(src, dir_fd=src_dir_fd).st_mode))

        self.ops.hooks["linkat"] = before_link
        self.contracts.atomic_no_clobber(self.directory, "entry", b"payload", 0o640)
        self.assertEqual(observed, [0o640])
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.root, "entry")).st_mode), 0o640)

    def test_partial_writes_still_publish_complete_bytes(self) -> None:
        self.ops.partial_writes = 2
        self.contracts.atomic_no_clobber(self.directory, "entry", b"abcdefgh", 0o600)
        with open(os.path.join(self.root, "entry"), "rb") as stream:
            self.assertEqual(stream.read(), b"abcdefgh")

    def test_existing_destination_is_typed_collision_and_preserved(self) -> None:
        with open(os.path.join(self.root, "entry"), "wb") as stream:
            stream.write(b"original")
        with self.assertRaises(DestinationExists) as ctx:
            self.contracts.atomic_no_clobber(self.directory, "entry", b"replacement", 0o600)
        self.assertEqual(ctx.exception.name, "entry")
        with open(os.path.join(self.root, "entry"), "rb") as stream:
            self.assertEqual(stream.read(), b"original")
        self.assertEqual(temporary_entries(self.root), [])

    def test_claims_no_parent_directory_durability(self) -> None:
        self.contracts.atomic_no_clobber(self.directory, "entry", b"payload", 0o600)
        self.assertNotIn("fsync", self.ops.order)

    def test_write_failure_removes_temporary_and_preserves_destination(self) -> None:
        self.ops.failures["write"] = OSError(errno.EIO, "injected write")
        with self.assertRaises(TransactionError) as ctx:
            self.contracts.atomic_no_clobber(self.directory, "entry", b"payload", 0o600)
        self.assertNotIn("entry", os.listdir(self.root))
        self.assertEqual(temporary_entries(self.root), [])
        self.assertEqual(ctx.exception.stage, "write")

    def test_publish_failure_removes_temporary_entry(self) -> None:
        self.ops.failures["linkat"] = PermissionError(errno.EACCES, "injected link")
        with self.assertRaises(TransactionError):
            self.contracts.atomic_no_clobber(self.directory, "entry", b"payload", 0o600)
        self.assertEqual(temporary_entries(self.root), [])


class TemporaryAllocationTests(unittest.TestCase):
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

    def test_each_collision_chooses_a_fresh_name(self) -> None:
        self.ops.failures["openat"] = (
            lambda count: FileExistsError(errno.EEXIST, "injected collision")
            if count == 1 else None
        )
        self.contracts.atomic_no_clobber(self.directory, "entry", b"payload", 0o600)
        names = self.ops.names("openat")
        self.assertEqual(len(names), 2)
        self.assertNotEqual(names[0], names[1])

    def test_three_failed_attempts_are_an_allocation_failure(self) -> None:
        self.ops.failures["openat"] = (
            lambda count: FileExistsError(errno.EEXIST, "injected collision")
            if count <= 3 else None
        )
        with self.assertRaises(TransactionError) as ctx:
            self.contracts.atomic_no_clobber(self.directory, "entry", b"payload", 0o600)
        self.assertEqual(ctx.exception.stage, STAGE_ALLOCATE)
        self.assertEqual(self.ops.counts.get("openat"), 3)
        names = self.ops.names("openat")
        self.assertEqual(len(names), 3)
        self.assertEqual(len(set(names)), 3)
        self.assertNotIsInstance(ctx.exception, DestinationExists)

    def test_collided_entries_remain_unread_and_untouched(self) -> None:
        os.mkdir(os.path.join(self.root, "occupied"))
        marker = os.path.join(self.root, "occupied", "keep")
        with open(marker, "wb") as stream:
            stream.write(b"untouched")
        self.ops.failures["openat"] = (
            lambda count: FileExistsError(errno.EEXIST, "injected collision")
            if count == 1 else None
        )
        self.contracts.atomic_no_clobber(self.directory, "entry", b"payload", 0o600)
        with open(marker, "rb") as stream:
            self.assertEqual(stream.read(), b"untouched")
        self.assertNotIn("read", self.ops.order)

    def test_temporary_collision_is_not_destination_exists(self) -> None:
        self.ops.failures["openat"] = (
            lambda count: FileExistsError(errno.EEXIST, "injected collision")
            if count <= 3 else None
        )
        try:
            self.contracts.atomic_no_clobber(self.directory, "entry", b"payload", 0o600)
        except DestinationExists:  # pragma: no cover - must not happen
            self.fail("temporary collision was conflated with destination contention")
        except TransactionError as exc:
            self.assertEqual(exc.stage, STAGE_ALLOCATE)


class FileExistsIsolationTests(unittest.TestCase):
    """An injected ``FileExistsError`` outside the final commit is not a collision."""

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

    def _assert_staged_not_collision(self, stage: str) -> TransactionError:
        with self.assertRaises(TransactionError) as ctx:
            self.contracts.atomic_no_clobber(self.directory, "entry", b"payload", 0o600)
        self.assertNotIsInstance(ctx.exception, DestinationExists)
        self.assertEqual(ctx.exception.stage, stage)
        return ctx.exception

    def test_fchmod_file_exists_is_a_staged_mode_failure(self) -> None:
        self.ops.failures["fchmod"] = FileExistsError(errno.EEXIST, "injected fchmod")
        error = self._assert_staged_not_collision(STAGE_MODE)
        self.assertIsInstance(error.cause, FileExistsError)
        self.assertEqual(error.cause.errno, errno.EEXIST)

    def test_write_file_exists_is_a_staged_write_failure(self) -> None:
        self.ops.failures["write"] = FileExistsError(errno.EEXIST, "injected write")
        self._assert_staged_not_collision(STAGE_WRITE)

    def test_close_file_exists_is_a_staged_close_failure(self) -> None:
        self.ops.failures["close"] = FileExistsError(errno.EEXIST, "injected close")
        self._assert_staged_not_collision(STAGE_CLOSE)

    def test_temporary_cleanup_file_exists_is_a_staged_commit_failure(self) -> None:
        self.ops.failures["unlinkat"] = FileExistsError(errno.EEXIST, "injected unlink")
        self._assert_staged_not_collision(STAGE_COMMIT)


class TemporaryCleanupRetryTests(unittest.TestCase):
    """A transient temporary-unlink failure is retried without masking the primary."""

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

    def test_first_temporary_unlink_failure_is_retried_and_succeeds(self) -> None:
        self.ops.failures["unlinkat"] = (
            lambda count: OSError(errno.EIO, "injected unlink")
            if count == 1 else None
        )
        with self.assertRaises(TransactionError) as ctx:
            self.contracts.atomic_no_clobber(self.directory, "entry", b"payload", 0o600)
        self.assertEqual(ctx.exception.stage, STAGE_COMMIT)
        self.assertEqual(ctx.exception.secondary, [])
        self.assertTrue(os.path.exists(os.path.join(self.root, "entry")))
        self.assertEqual(temporary_entries(self.root), [])
        self.assertGreaterEqual(self.ops.counts.get("unlinkat", 0), 2)

    def test_persistent_temporary_unlink_failure_stays_primary(self) -> None:
        cause = OSError(errno.EIO, "injected unlink")
        self.ops.failures["unlinkat"] = cause
        with self.assertRaises(TransactionError) as ctx:
            self.contracts.atomic_no_clobber(self.directory, "entry", b"payload", 0o600)
        self.assertEqual(ctx.exception.stage, STAGE_COMMIT)
        self.assertIs(ctx.exception.cause, cause)
        self.assertEqual(len(ctx.exception.secondary), 1)
        self.assertEqual(temporary_entries(self.root), [os.path.basename(self.ops.names("openat")[0])])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
