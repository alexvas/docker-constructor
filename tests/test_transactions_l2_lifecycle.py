"""Phase 1 tasks 1.8–1.9 — durable unlink and exception lifecycle.

Durable unlink removes one validated owned entry and flushes the parent
directory before success; only an explicitly declared-absent entry is
idempotent.  Raw ``OSError`` subclasses and errno stay observable through
``__cause__`` chaining, ``KeyboardInterrupt`` and cancellation pass through
unchanged.  Ordinary cleanup/close failures never replace a primary failure,
while process-control interruptions raised by cleanup always propagate.
"""
from __future__ import annotations

import errno
import os
import tempfile
import unittest

from docker.transactions.capabilities import DirectoryCapability
from docker.transactions.errors import (
    STAGE_CLOSE,
    STAGE_FSYNC_DIRECTORY,
    STAGE_UNLINK,
    STAGE_WRITE,
    TransactionError,
    UnsafeFileError,
)
from docker.transactions.regular import RegularFileContracts
from tests.transactions_test_support import InjectedOps, temporary_entries


class _Cancellation(BaseException):
    """A cancellation-style, non-I/O exception."""


class _Case(unittest.TestCase):
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

    def _write(self, name: str = "entry", data: bytes = b"payload") -> str:
        path = os.path.join(self.root, name)
        with open(path, "wb") as stream:
            stream.write(data)
        os.chmod(path, 0o600)
        return path


class DurableUnlinkTests(_Case):
    def test_removes_present_entry_and_fsyncs_parent(self) -> None:
        path = self._write()
        self.contracts.durable_unlink(self.directory, "entry", allowed_mode=0o600)
        self.assertFalse(os.path.exists(path))
        fsync_fds = [args[0] for args in self.ops.arg_pairs("fsync")]
        self.assertIn(self.directory.fd, fsync_fds)

    def test_declared_absent_entry_is_idempotent(self) -> None:
        self.contracts.durable_unlink(self.directory, "entry", allow_absent=True)
        fsync_fds = [args[0] for args in self.ops.arg_pairs("fsync")]
        self.assertIn(self.directory.fd, fsync_fds)

    def test_undeclared_absent_entry_fails(self) -> None:
        with self.assertRaises(TransactionError):
            self.contracts.durable_unlink(self.directory, "entry")

    def test_unsafe_entry_is_rejected_without_removal(self) -> None:
        target = self._write("target")
        os.symlink(target, os.path.join(self.root, "entry"))
        with self.assertRaises(UnsafeFileError):
            self.contracts.durable_unlink(self.directory, "entry")
        self.assertTrue(os.path.islink(os.path.join(self.root, "entry")))
        self.assertTrue(os.path.exists(target))

    def test_failed_unlink_never_overwrites_concurrent_creation(self) -> None:
        # Removal is a single unlink of the validated name; a failure must not
        # run any restoration path that could overwrite a substitute entry.
        validated = self._write("entry", b"validated")
        self.ops.failures["unlinkat"] = PermissionError(errno.EACCES, "injected unlink")
        with self.assertRaises(TransactionError) as ctx:
            self.contracts.durable_unlink(self.directory, "entry")
        self.assertEqual(ctx.exception.stage, STAGE_UNLINK)
        with open(validated, "rb") as stream:
            self.assertEqual(stream.read(), b"validated")
        self.assertNotIn("renameat", self.ops.order)
        self.assertEqual(temporary_entries(self.root), [])

    def test_validated_descriptor_stays_open_until_unlink(self) -> None:
        self._write()
        events: list[tuple[str, object]] = []
        self.ops.hooks["close"] = lambda fd: events.append(("close", fd))
        self.ops.hooks["unlinkat"] = lambda dir_fd, name: events.append(("unlink", name))
        self.contracts.durable_unlink(self.directory, "entry")
        # The validated descriptor must stay open until after the unlink.
        self.assertEqual([event[0] for event in events], ["unlink", "close"])
        self.assertEqual(events[0][1], "entry")

    def test_unlink_failure_is_reported_and_entry_remains(self) -> None:
        self._write()
        self.ops.failures["unlinkat"] = PermissionError(errno.EACCES, "injected unlink")
        with self.assertRaises(TransactionError) as ctx:
            self.contracts.durable_unlink(self.directory, "entry")
        self.assertEqual(ctx.exception.stage, STAGE_UNLINK)
        self.assertTrue(os.path.exists(os.path.join(self.root, "entry")))
        self.assertEqual(temporary_entries(self.root), [])

    def test_parent_fsync_failure_is_reported(self) -> None:
        self._write()
        self.ops.failures["fsync"] = OSError(errno.EIO, "injected fsync")
        with self.assertRaises(TransactionError) as ctx:
            self.contracts.durable_unlink(self.directory, "entry")
        self.assertEqual(ctx.exception.stage, STAGE_FSYNC_DIRECTORY)
        self.assertFalse(os.path.exists(os.path.join(self.root, "entry")))

    def test_close_failure_is_reported_when_nothing_else_failed(self) -> None:
        self._write()
        self.ops.failures["close"] = OSError(errno.EIO, "injected close")
        with self.assertRaises(TransactionError) as ctx:
            self.contracts.durable_unlink(self.directory, "entry")
        self.assertEqual(ctx.exception.stage, STAGE_CLOSE)


class ExceptionLifecycleTests(_Case):
    def test_raw_oserror_is_observable_through_cause_and_errno(self) -> None:
        cause = PermissionError(errno.EACCES, "injected write")
        self.ops.failures["write"] = cause
        with self.assertRaises(TransactionError) as ctx:
            self.contracts.atomic_no_clobber(self.directory, "entry", b"payload", 0o600)
        error = ctx.exception
        self.assertIs(error.cause, cause)
        self.assertIs(error.__cause__, cause)
        self.assertIsInstance(error.cause, PermissionError)
        self.assertEqual(error.cause.errno, errno.EACCES)

    def test_keyboard_interrupt_passes_through_unchanged(self) -> None:
        interruption = KeyboardInterrupt()
        self.ops.failures["write"] = interruption
        with self.assertRaises(KeyboardInterrupt) as ctx:
            self.contracts.atomic_no_clobber(self.directory, "entry", b"payload", 0o600)
        self.assertIs(ctx.exception, interruption)
        self.assertEqual(temporary_entries(self.root), [])

    def test_cancellation_passes_through_unchanged(self) -> None:
        class Cancelled(BaseException):
            pass

        cancellation = Cancelled()
        self.ops.failures["write"] = cancellation
        with self.assertRaises(Cancelled) as ctx:
            self.contracts.atomic_no_clobber(self.directory, "entry", b"payload", 0o600)
        self.assertIs(ctx.exception, cancellation)
        self.assertEqual(temporary_entries(self.root), [])

    def test_close_failure_does_not_replace_primary_failure(self) -> None:
        primary = PermissionError(errno.EACCES, "injected write")
        close_error = OSError(errno.EIO, "injected close")
        self.ops.failures["write"] = primary
        self.ops.failures["close"] = close_error
        with self.assertRaises(TransactionError) as ctx:
            self.contracts.atomic_no_clobber(self.directory, "entry", b"payload", 0o600)
        error = ctx.exception
        self.assertEqual(error.stage, STAGE_WRITE)
        self.assertIs(error.cause, primary)
        self.assertEqual(len(error.secondary), 1)
        self.assertIs(error.secondary[0], close_error)

    def test_cleanup_failure_does_not_replace_primary_failure(self) -> None:
        primary = PermissionError(errno.EACCES, "injected write")
        cleanup_error = PermissionError(errno.EACCES, "injected unlink")
        self.ops.failures["write"] = primary
        self.ops.failures["unlinkat"] = cleanup_error
        with self.assertRaises(TransactionError) as ctx:
            self.contracts.atomic_no_clobber(self.directory, "entry", b"payload", 0o600)
        error = ctx.exception
        self.assertEqual(error.stage, STAGE_WRITE)
        self.assertIs(error.cause, primary)
        self.assertIn(cleanup_error, error.secondary)

    def test_read_close_failure_is_reported_when_nothing_else_failed(self) -> None:
        self._write()
        self.ops.failures["close"] = OSError(errno.EIO, "injected close")
        with self.assertRaises(TransactionError) as ctx:
            self.contracts.validated_read(self.directory, "entry")
        self.assertEqual(ctx.exception.stage, STAGE_CLOSE)

    def test_cleanup_close_interruption_propagates(self) -> None:
        primary = PermissionError(errno.EACCES, "injected write")
        interruption = KeyboardInterrupt()
        self.ops.failures["write"] = primary
        self.ops.failures["close"] = interruption
        with self.assertRaises(KeyboardInterrupt) as ctx:
            self.contracts.atomic_no_clobber(self.directory, "entry", b"payload", 0o600)
        self.assertIs(ctx.exception, interruption)

    def test_cleanup_unlink_cancellation_propagates(self) -> None:
        primary = PermissionError(errno.EACCES, "injected write")
        cancellation = _Cancellation()
        self.ops.failures["write"] = primary
        self.ops.failures["unlinkat"] = cancellation
        with self.assertRaises(_Cancellation) as ctx:
            self.contracts.atomic_no_clobber(self.directory, "entry", b"payload", 0o600)
        self.assertIs(ctx.exception, cancellation)


class CloseInterruptionTests(_Case):
    """A non-I/O close failure propagates unchanged, never as STAGE_CLOSE."""

    def test_validated_read_close_keyboard_interrupt_propagates(self) -> None:
        self._write()
        interruption = KeyboardInterrupt()
        self.ops.failures["close"] = interruption
        with self.assertRaises(KeyboardInterrupt) as ctx:
            self.contracts.validated_read(self.directory, "entry")
        self.assertIs(ctx.exception, interruption)

    def test_validated_read_close_cancellation_propagates(self) -> None:
        self._write()
        cancellation = _Cancellation()
        self.ops.failures["close"] = cancellation
        with self.assertRaises(_Cancellation) as ctx:
            self.contracts.validated_read(self.directory, "entry")
        self.assertIs(ctx.exception, cancellation)

    def test_durable_unlink_close_keyboard_interrupt_propagates(self) -> None:
        self._write()
        interruption = KeyboardInterrupt()
        self.ops.failures["close"] = interruption
        with self.assertRaises(KeyboardInterrupt) as ctx:
            self.contracts.durable_unlink(self.directory, "entry")
        self.assertIs(ctx.exception, interruption)
        self.assertFalse(os.path.exists(os.path.join(self.root, "entry")))

    def test_durable_unlink_close_cancellation_propagates(self) -> None:
        self._write()
        cancellation = _Cancellation()
        self.ops.failures["close"] = cancellation
        with self.assertRaises(_Cancellation) as ctx:
            self.contracts.durable_unlink(self.directory, "entry")
        self.assertIs(ctx.exception, cancellation)
        self.assertFalse(os.path.exists(os.path.join(self.root, "entry")))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
