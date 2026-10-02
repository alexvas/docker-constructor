"""Phase 1 tasks 1.2–1.3 — L1 capabilities and validated reads.

Directory and regular-file capabilities retain live descriptors and carry
basename-only authority.  Released and cross-directory capabilities are
rejected, paths are never reconstructed, and validated reads reject every
unsafe leaf through one retained no-follow descriptor.
"""
from __future__ import annotations

import errno
import inspect
import os
import stat
import tempfile
import unittest

from docker.transactions import capabilities as capabilities_module
from docker.transactions.capabilities import DirectoryCapability, FileCapability
from docker.transactions.errors import (
    STAGE_VALIDATE,
    CapabilityError,
    DestinationExists,
    TransactionError,
    UnsafeFileError,
)
from docker.transactions.posix import PosixFileOps
from docker.transactions.regular import RegularFileContracts
from tests.transactions_test_support import InjectedOps


class _Cancellation(BaseException):
    """A cancellation-style, non-I/O exception."""


class DirectoryCapabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.ops = InjectedOps()
        self.directory = DirectoryCapability.from_path(self.ops, self.root)
        self.addCleanup(self.directory.close)

    def test_retains_a_live_directory_descriptor(self) -> None:
        fd = self.directory.fd
        self.assertIsInstance(fd, int)
        self.assertTrue(stat.S_ISDIR(os.fstat(fd).st_mode))

    def test_released_directory_capability_is_rejected(self) -> None:
        self.directory.close()
        with self.assertRaises(CapabilityError):
            _ = self.directory.fd

    def test_close_failure_still_releases_and_is_not_retried(self) -> None:
        self.ops.reset()
        self.ops.failures["close"] = OSError(errno.EIO, "injected close")
        with self.assertRaises(OSError):
            self.directory.close()
        # The state transition means "close attempted", so the capability is
        # already unusable even though the close failed.
        self.assertTrue(self.directory.closed)
        with self.assertRaises(CapabilityError):
            _ = self.directory.fd
        self.assertEqual(self.ops.counts.get("close"), 1)
        # A second close is a no-op and never issues another system call.
        self.directory.close()
        self.assertEqual(self.ops.counts.get("close"), 1)

    def test_direct_construction_is_rejected(self) -> None:
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        self.ops.reset()
        with self.assertRaises(CapabilityError):
            DirectoryCapability(self.ops, fd, "direct")
        # The rejected construction must neither close nor mutate the
        # caller-owned descriptor.
        self.assertNotIn("close", self.ops.order)
        self.assertTrue(stat.S_ISDIR(os.fstat(fd).st_mode))

    def test_forged_authority_is_rejected(self) -> None:
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        self.ops.reset()
        with self.assertRaises(CapabilityError):
            DirectoryCapability(self.ops, fd, "direct", authority=object())
        self.assertNotIn("close", self.ops.order)
        self.assertTrue(stat.S_ISDIR(os.fstat(fd).st_mode))

    def test_accepts_canonical_basenames(self) -> None:
        for name in ("entry", "a.b-c_d", ".hidden", "0"):
            self.assertEqual(self.directory.child_basename(name), name)

    def test_rejects_non_canonical_basenames(self) -> None:
        for name in ("", ".", "..", "a/b", "/absolute", "a\x00b", None, 7):
            with self.assertRaises(CapabilityError):
                self.directory.child_basename(name)

    def test_never_reconstructs_paths(self) -> None:
        source = inspect.getsource(capabilities_module)
        for forbidden in ("os.path.join", "os.path.realpath", ".resolve(", "os.path.abspath"):
            self.assertNotIn(forbidden, source)


class DirectoryContextManagerTests(unittest.TestCase):
    """Context-manager exit preserves an active exception over a close failure."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.ops = InjectedOps()
        self.addCleanup(self.ops.failures.clear)

    def _directory(self) -> DirectoryCapability:
        return DirectoryCapability.from_path(self.ops, self.root)

    def test_close_failure_does_not_replace_active_exception(self) -> None:
        directory = self._directory()
        primary = TransactionError("body", "body failed")
        close_error = OSError(errno.EIO, "injected close")
        self.ops.failures["close"] = close_error
        with self.assertRaises(TransactionError) as ctx:
            with directory:
                raise primary
        # The body exception stays primary and the close error is secondary.
        self.assertIs(ctx.exception, primary)
        self.assertEqual(primary.secondary, [close_error])
        self.assertTrue(directory.closed)

    def test_close_failure_without_active_exception_propagates(self) -> None:
        directory = self._directory()
        close_error = OSError(errno.EIO, "injected close")
        self.ops.failures["close"] = close_error
        with self.assertRaises(OSError) as ctx:
            with directory:
                pass
        self.assertIs(ctx.exception, close_error)
        self.assertTrue(directory.closed)

    def test_close_keyboard_interrupt_propagates_over_active_exception(self) -> None:
        directory = self._directory()
        primary = TransactionError("body", "body failed")
        interruption = KeyboardInterrupt()
        self.ops.failures["close"] = interruption
        with self.assertRaises(KeyboardInterrupt) as ctx:
            with directory:
                raise primary
        self.assertIs(ctx.exception, interruption)
        self.assertTrue(directory.closed)

    def test_close_cancellation_propagates_over_active_exception(self) -> None:
        directory = self._directory()
        primary = TransactionError("body", "body failed")
        cancellation = _Cancellation()
        self.ops.failures["close"] = cancellation
        with self.assertRaises(_Cancellation) as ctx:
            with directory:
                raise primary
        self.assertIs(ctx.exception, cancellation)
        self.assertTrue(directory.closed)


class FileCapabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.other_root = os.path.join(self.root, "other")
        os.mkdir(self.other_root, 0o700)
        with open(os.path.join(self.root, "leaf"), "wb") as stream:
            stream.write(b"leaf-bytes")
        self.ops = InjectedOps()
        self.directory = DirectoryCapability.from_path(self.ops, self.root)
        self.other = DirectoryCapability.from_path(self.ops, self.other_root)
        self.addCleanup(self.directory.close)
        self.addCleanup(self.other.close)

    def test_retains_a_live_regular_file_descriptor(self) -> None:
        capability = self.directory.open_regular("leaf")
        try:
            self.assertIsInstance(capability.fd, int)
            self.assertTrue(stat.S_ISREG(os.fstat(capability.fd).st_mode))
            self.assertEqual(capability.read_all(), b"leaf-bytes")
        finally:
            capability.close()

    def test_released_file_capability_is_rejected(self) -> None:
        capability = self.directory.open_regular("leaf")
        capability.close()
        with self.assertRaises(CapabilityError):
            _ = capability.fd

    def test_close_failure_still_releases_and_is_not_retried(self) -> None:
        self.addCleanup(self.ops.failures.clear)
        capability = self.directory.open_regular("leaf")
        self.ops.reset()
        self.ops.failures["close"] = OSError(errno.EIO, "injected close")
        with self.assertRaises(OSError):
            capability.close()
        # The state transition means "close attempted", so the capability is
        # already unusable even though the close failed.
        self.assertTrue(capability.closed)
        with self.assertRaises(CapabilityError):
            _ = capability.fd
        self.assertEqual(self.ops.counts.get("close"), 1)
        # A second close is a no-op and never issues another system call.
        capability.close()
        self.assertEqual(self.ops.counts.get("close"), 1)

    def test_direct_construction_is_rejected(self) -> None:
        fd = os.open(os.path.join(self.root, "leaf"), os.O_RDONLY)
        self.addCleanup(os.close, fd)
        info = os.fstat(fd)
        self.ops.reset()
        with self.assertRaises(CapabilityError):
            FileCapability(self.ops, fd, self.directory, info, "direct")
        # The rejected construction must neither close nor mutate the
        # caller-owned descriptor.
        self.assertNotIn("close", self.ops.order)
        self.assertIsInstance(os.fstat(fd), os.stat_result)

    def test_released_file_capability_ownership_is_rejected(self) -> None:
        capability = self.directory.open_regular("leaf")
        capability.close()
        with self.assertRaises(CapabilityError):
            capability.assert_owned_by(self.directory)

    def test_released_directory_capability_ownership_is_rejected(self) -> None:
        capability = self.directory.open_regular("leaf")
        self.addCleanup(capability.close)
        self.directory.close()
        with self.assertRaises(CapabilityError):
            capability.assert_owned_by(self.directory)

    def test_cross_directory_capability_is_rejected(self) -> None:
        capability = self.directory.open_regular("leaf")
        try:
            with self.assertRaises(CapabilityError):
                capability.assert_owned_by(self.other)
            capability.assert_owned_by(self.directory)
        finally:
            capability.close()

    def test_open_regular_uses_no_follow_flags(self) -> None:
        capability = self.directory.open_regular("leaf")
        capability.close()
        flags = self.ops.arg_pairs("openat")[-1][2]
        self.assertTrue(flags & getattr(os, "O_NOFOLLOW", 0))


class ValidatedReadTests(unittest.TestCase):
    """One retained no-follow descriptor rejects every unsafe leaf."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.ops = InjectedOps()
        self.contracts = RegularFileContracts(self.ops)
        self.directory = DirectoryCapability.from_path(self.ops, self.root)
        self.addCleanup(self.directory.close)

    def _write(self, name: str, data: bytes, mode: int = 0o600) -> str:
        path = os.path.join(self.root, name)
        with open(path, "wb") as stream:
            stream.write(data)
        os.chmod(path, mode)
        return path

    def test_reads_safe_leaf(self) -> None:
        self._write("safe", b"contents", 0o600)
        self.assertEqual(
            self.contracts.validated_read(self.directory, "safe", allowed_mode=0o600),
            b"contents",
        )

    def test_rejects_symlink_without_mutating_target(self) -> None:
        target = self._write("target", b"original", 0o600)
        os.symlink(target, os.path.join(self.root, "link"))
        with self.assertRaises(UnsafeFileError):
            self.contracts.validated_read(self.directory, "link")
        with open(target, "rb") as stream:
            self.assertEqual(stream.read(), b"original")
        self.assertTrue(os.path.islink(os.path.join(self.root, "link")))

    def test_rejects_non_regular_leaf(self) -> None:
        os.mkdir(os.path.join(self.root, "subdir"), 0o700)
        with self.assertRaises(UnsafeFileError):
            self.contracts.validated_read(self.directory, "subdir")

    def test_rejects_fifo_leaf_without_blocking(self) -> None:
        os.mkfifo(os.path.join(self.root, "pipe"), 0o600)
        with self.assertRaises(UnsafeFileError):
            self.contracts.validated_read(self.directory, "pipe")

    def test_rejects_foreign_owned_leaf(self) -> None:
        self._write("foreign", b"data", 0o600)

        def foreign(info: os.stat_result) -> os.stat_result:
            return os.stat_result((
                info.st_mode, info.st_ino, info.st_dev, info.st_nlink,
                4242, info.st_gid, info.st_size,
                info.st_atime, info.st_mtime, info.st_ctime,
            ))

        self.ops.fstat_override = foreign
        with self.assertRaises(UnsafeFileError):
            self.contracts.validated_read(self.directory, "foreign")

    def test_rejects_multiply_linked_leaf(self) -> None:
        path = self._write("first", b"data", 0o600)
        os.link(path, os.path.join(self.root, "second"))
        with self.assertRaises(UnsafeFileError):
            self.contracts.validated_read(self.directory, "first")

    def test_rejects_forbidden_mode_leaf(self) -> None:
        self._write("wide", b"data", 0o644)
        with self.assertRaises(UnsafeFileError):
            self.contracts.validated_read(self.directory, "wide", allowed_mode=0o600)

    def test_missing_leaf_fails(self) -> None:
        from docker.transactions.errors import TransactionError

        with self.assertRaises(TransactionError):
            self.contracts.validated_read(self.directory, "absent")

    def test_fstat_failure_is_staged_with_cause_and_closes_descriptor(self) -> None:
        self._write("entry", b"data", 0o600)
        cause = OSError(errno.EIO, "injected fstat")
        self.ops.failures["fstat"] = cause
        with self.assertRaises(TransactionError) as ctx:
            self.contracts.validated_read(self.directory, "entry")
        self.assertEqual(ctx.exception.stage, STAGE_VALIDATE)
        self.assertIs(ctx.exception.cause, cause)
        self.assertEqual(ctx.exception.cause.errno, errno.EIO)
        self.assertIn("close", self.ops.order)


class CapabilityCleanupTests(unittest.TestCase):
    """A primary validation failure survives a secondary close failure."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.ops = InjectedOps()
        self.addCleanup(self.ops.failures.clear)

    def _write_leaf(self, name: str = "entry") -> str:
        path = os.path.join(self.root, name)
        with open(path, "wb") as stream:
            stream.write(b"data")
        os.chmod(path, 0o600)
        return path

    @staticmethod
    def _non_regular(info):
        values = list(info)
        values[0] = stat.S_IFDIR | 0o755
        return os.stat_result(values)

    def _validated_directory(self) -> DirectoryCapability:
        directory = DirectoryCapability.from_path(self.ops, self.root)
        self.addCleanup(directory.close)
        # Clear injected failures before the directory is closed at cleanup.
        self.addCleanup(self.ops.failures.clear)
        return directory

    def test_from_path_fstat_failure_survives_close_failure(self) -> None:
        fstat_error = OSError(errno.EIO, "injected fstat")
        close_error = OSError(errno.EIO, "injected close")
        self.ops.failures["fstat"] = fstat_error
        self.ops.failures["close"] = close_error
        with self.assertRaises(OSError) as ctx:
            DirectoryCapability.from_path(self.ops, self.root)
        self.assertIs(ctx.exception, fstat_error)
        self.assertEqual(getattr(ctx.exception, "_transaction_secondary"), [close_error])
        self.assertNotIsInstance(ctx.exception, DestinationExists)

    def test_open_regular_fstat_failure_survives_close_failure(self) -> None:
        self._write_leaf()
        directory = self._validated_directory()
        fstat_error = OSError(errno.EIO, "injected leaf fstat")
        close_error = OSError(errno.EIO, "injected close")
        self.ops.failures["fstat"] = fstat_error
        self.ops.failures["close"] = close_error
        with self.assertRaises(TransactionError) as ctx:
            directory.open_regular("entry")
        self.assertEqual(ctx.exception.stage, STAGE_VALIDATE)
        self.assertIs(ctx.exception.cause, fstat_error)
        self.assertEqual(ctx.exception.secondary, [close_error])
        self.assertNotIsInstance(ctx.exception, DestinationExists)

    def test_open_regular_validation_failure_survives_close_failure(self) -> None:
        self._write_leaf()
        directory = self._validated_directory()
        close_error = OSError(errno.EIO, "injected close")
        self.ops.fstat_override = self._non_regular
        self.ops.failures["close"] = close_error
        with self.assertRaises(UnsafeFileError) as ctx:
            directory.open_regular("entry")
        self.assertEqual(ctx.exception.stage, STAGE_VALIDATE)
        self.assertEqual(ctx.exception.secondary, [close_error])
        self.assertNotIsInstance(ctx.exception, DestinationExists)

    def test_from_path_cleanup_close_interruption_propagates(self) -> None:
        interruption = KeyboardInterrupt()
        self.ops.failures["fstat"] = OSError(errno.EIO, "injected fstat")
        self.ops.failures["close"] = interruption
        with self.assertRaises(KeyboardInterrupt) as ctx:
            DirectoryCapability.from_path(self.ops, self.root)
        # The interruption escapes instead of the earlier validation error.
        self.assertIs(ctx.exception, interruption)

    def test_open_regular_cleanup_close_cancellation_propagates(self) -> None:
        self._write_leaf()
        directory = self._validated_directory()
        cancellation = _Cancellation()
        self.ops.fstat_override = self._non_regular
        self.ops.failures["close"] = cancellation
        with self.assertRaises(_Cancellation) as ctx:
            directory.open_regular("entry")
        # The cancellation escapes instead of the earlier validation error.
        self.assertIs(ctx.exception, cancellation)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
