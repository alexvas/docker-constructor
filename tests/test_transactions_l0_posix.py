"""Phase 1 task 1.1 — L0 PosixFileOps partial-write and raw-failure behavior.

The L0 backend must delegate to descriptor-relative POSIX calls and must
preserve the raised ``OSError`` subclass, ``errno``, and chaining inputs
unchanged.  A short write is completed by :meth:`PosixFileOps.write_all`
and a zero-byte write is an I/O failure.
"""
from __future__ import annotations

import errno
import os
import stat
import tempfile
import unittest

from docker.transactions.posix import PosixFileOps
from tests.transactions_test_support import InjectedOps


class WriteAllTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "payload")

    def test_partial_writes_are_completed(self) -> None:
        ops = InjectedOps()
        ops.partial_writes = 3
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            ops.write_all(fd, b"abcdefghij")
        finally:
            os.close(fd)
        with open(self.path, "rb") as stream:
            self.assertEqual(stream.read(), b"abcdefghij")
        self.assertGreaterEqual(ops.counts.get("write", 0), 4)

    def test_zero_byte_write_is_an_io_failure(self) -> None:
        ops = InjectedOps()
        ops.partial_writes = 0
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with self.assertRaises(OSError) as ctx:
                ops.write_all(fd, b"abcdef")
        finally:
            os.close(fd)
        self.assertEqual(ctx.exception.errno, errno.EIO)

    def test_write_all_delegates_to_real_backend(self) -> None:
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            PosixFileOps().write_all(fd, b"complete-bytes")
        finally:
            os.close(fd)
        with open(self.path, "rb") as stream:
            self.assertEqual(stream.read(), b"complete-bytes")


class RawFailurePropagationTests(unittest.TestCase):
    """Every L0 surface propagates the injected raw failure unchanged."""

    def _assert_raw(self, method: str, exc: OSError, call) -> None:
        ops = InjectedOps()
        ops.failures[method] = exc
        with self.assertRaises(type(exc)) as ctx:
            call(ops)
        self.assertIs(ctx.exception, exc)
        self.assertEqual(ctx.exception.errno, exc.errno)

    def test_openat_failure_is_raw(self) -> None:
        self._assert_raw(
            "openat",
            FileNotFoundError(errno.ENOENT, "injected openat"),
            lambda ops: ops.openat(None, "/missing", os.O_RDONLY),
        )

    def test_read_failure_is_raw(self) -> None:
        self._assert_raw(
            "read",
            BlockingIOError(errno.EAGAIN, "injected read"),
            lambda ops: ops.read(0, 8),
        )

    def test_write_failure_is_raw(self) -> None:
        self._assert_raw(
            "write",
            BrokenPipeError(errno.EPIPE, "injected write"),
            lambda ops: ops.write(0, b"x"),
        )

    def test_fstat_failure_is_raw(self) -> None:
        self._assert_raw(
            "fstat",
            OSError(errno.EBADF, "injected fstat"),
            lambda ops: ops.fstat(0),
        )

    def test_fsync_failure_is_raw(self) -> None:
        self._assert_raw(
            "fsync",
            OSError(errno.EIO, "injected fsync"),
            lambda ops: ops.fsync(0),
        )

    def test_linkat_failure_is_raw(self) -> None:
        self._assert_raw(
            "linkat",
            PermissionError(errno.EACCES, "injected linkat"),
            lambda ops: ops.linkat(0, "a", 0, "b"),
        )

    def test_renameat_failure_is_raw(self) -> None:
        self._assert_raw(
            "renameat",
            PermissionError(errno.EXDEV, "injected renameat"),
            lambda ops: ops.renameat(0, "a", 0, "b"),
        )

    def test_unlinkat_failure_is_raw(self) -> None:
        self._assert_raw(
            "unlinkat",
            PermissionError(errno.EACCES, "injected unlinkat"),
            lambda ops: ops.unlinkat(0, "a"),
        )

    def test_chmod_failure_is_raw(self) -> None:
        self._assert_raw(
            "chmod",
            PermissionError(errno.EPERM, "injected chmod"),
            lambda ops: ops.chmod(0, "a", 0o600),
        )

    def test_close_failure_is_raw(self) -> None:
        self._assert_raw(
            "close",
            OSError(errno.EBADF, "injected close"),
            lambda ops: ops.close(0),
        )


class RealDelegationTests(unittest.TestCase):
    """The production backend performs real descriptor-relative syscalls."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ops = PosixFileOps()
        self.dir_fd = os.open(self.tmp.name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))

    def tearDown(self) -> None:
        os.close(self.dir_fd)

    def test_open_link_rename_unlink_round_trip(self) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        tmp_fd = self.ops.openat(self.dir_fd, ".tmp", flags, 0o600)
        self.ops.write_all(tmp_fd, b"data")
        self.ops.fsync(tmp_fd)
        self.ops.close(tmp_fd)
        self.ops.linkat(self.dir_fd, ".tmp", self.dir_fd, "final")
        self.ops.unlinkat(self.dir_fd, ".tmp")
        self.ops.renameat(self.dir_fd, "final", self.dir_fd, "renamed")
        self.ops.chmod(self.dir_fd, "renamed", 0o644, follow_symlinks=False)
        self.ops.unlinkat(self.dir_fd, "renamed")
        self.assertNotIn("renamed", os.listdir(self.tmp.name))

    def test_chmod_establishes_requested_mode(self) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = self.ops.openat(self.dir_fd, "mode-target", flags, 0o600)
        self.ops.close(fd)
        self.ops.chmod(self.dir_fd, "mode-target", 0o640, follow_symlinks=False)
        info = os.stat(os.path.join(self.tmp.name, "mode-target"))
        self.assertEqual(info.st_mode & 0o777, 0o640)


class KeywordCompatibilityTests(unittest.TestCase):
    """The historical ``dir_fd`` keyword is used by every single-descriptor method.

    ``PosixFileOps.openat``, ``unlinkat``, ``mkdirat``, ``statat``, and
    ``rmdirat`` accepted ``dir_fd=...`` (or its historical equivalent) before
    the descriptor-capability migration; keyword callers must keep working.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ops = PosixFileOps()
        self.dir_fd = os.open(
            self.tmp.name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        self.addCleanup(os.close, self.dir_fd)

    def test_openat_accepts_dir_fd_keyword(self) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = self.ops.openat(
            dir_fd=self.dir_fd, name="keyword", flags=flags, mode=0o600
        )
        try:
            self.ops.write_all(fd, b"payload")
        finally:
            self.ops.close(fd)
        with open(os.path.join(self.tmp.name, "keyword"), "rb") as stream:
            self.assertEqual(stream.read(), b"payload")

    def test_unlinkat_accepts_dir_fd_keyword(self) -> None:
        path = os.path.join(self.tmp.name, "doomed")
        with open(path, "wb") as stream:
            stream.write(b"x")
        self.ops.unlinkat(dir_fd=self.dir_fd, name="doomed")
        self.assertFalse(os.path.exists(path))

    def test_mkdirat_accepts_dir_fd_keyword(self) -> None:
        self.ops.mkdirat(dir_fd=self.dir_fd, name="created", mode=0o700)
        self.assertTrue(os.path.isdir(os.path.join(self.tmp.name, "created")))

    def test_statat_accepts_dir_fd_keyword(self) -> None:
        path = os.path.join(self.tmp.name, "entry")
        with open(path, "wb") as stream:
            stream.write(b"x")
        info = self.ops.statat(
            dir_fd=self.dir_fd, name="entry", follow_symlinks=False
        )
        self.assertTrue(stat.S_ISREG(info.st_mode))

    def test_rmdirat_accepts_dir_fd_keyword(self) -> None:
        path = os.path.join(self.tmp.name, "removable")
        os.mkdir(path, mode=0o700)
        self.ops.rmdirat(dir_fd=self.dir_fd, name="removable")
        self.assertFalse(os.path.exists(path))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
