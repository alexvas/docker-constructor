"""Phase 9A task 9A.4 — cleanup precedence audit and regression tests.

These tests pin the required precedence, continuation, exactly-once, and
exception-identity behavior of the audited cleanup sites after migration.
The two named intentional hardenings are ``locking._release_descriptor`` and
``regular._discard``.
"""
from __future__ import annotations

import errno
import os
import tempfile
import unittest

from docker.transactions.capabilities import DirectoryCapability
from docker.transactions.locking import _release_descriptor
from docker.transactions.regular import RegularFileContracts, _Publication
from docker.transactions.errors import STAGE_CLOSE, LockError
from tests.transactions_test_support import InjectedOps


class _Cancellation(BaseException):
    """A cancellation-style, non-I/O process-control exception."""


def _secondary_of(exc: BaseException) -> list[BaseException]:
    return getattr(exc, "_transaction_secondary", [])


class ReleaseDescriptorTests(unittest.TestCase):
    """``locking._release_descriptor`` preserves an authoritative primary."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.ops = InjectedOps()
        self.addCleanup(self.ops.failures.clear)
        self.fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)

    def test_original_non_exception_primary_survives_ordinary_close_failure(self) -> None:
        primary = _Cancellation("original interruption")
        close_failure = OSError(errno.EIO, "close failed")
        self.ops.failures["close"] = close_failure
        with self.assertRaises(_Cancellation) as ctx:
            _release_descriptor(self.ops, self.fd, primary)
        self.assertIs(ctx.exception, primary)
        self.assertEqual(_secondary_of(primary), [close_failure])
        self.assertEqual(self.ops.counts["close"], 1)

    def test_original_non_exception_primary_survives_later_cleanup_interruption(self) -> None:
        primary = _Cancellation("original interruption")
        later = _Cancellation("later interruption")
        self.ops.failures["flock"] = later
        with self.assertRaises(_Cancellation) as ctx:
            _release_descriptor(self.ops, self.fd, primary)
        self.assertIs(ctx.exception, primary)
        self.assertEqual(_secondary_of(primary), [later])
        # The descriptor is still closed exactly once even though unlock
        # raised a process-control interruption.
        self.assertEqual(self.ops.counts["close"], 1)

    def test_original_non_exception_primary_survives_unexpected_close_defect(self) -> None:
        primary = _Cancellation("original interruption")
        defect = RuntimeError("programmer defect")
        self.ops.failures["close"] = defect
        with self.assertRaises(_Cancellation) as ctx:
            _release_descriptor(self.ops, self.fd, primary)
        self.assertIs(ctx.exception, primary)
        self.assertEqual(_secondary_of(primary), [defect])

    def test_original_exception_primary_is_displaced_by_cleanup_interruption(self) -> None:
        primary = ValueError("original exception")
        interruption = _Cancellation("cleanup interruption")
        self.ops.failures["flock"] = interruption
        with self.assertRaises(_Cancellation) as ctx:
            _release_descriptor(self.ops, self.fd, primary)
        self.assertIs(ctx.exception, interruption)
        self.assertEqual(_secondary_of(interruption), [primary])
        self.assertEqual(self.ops.counts["close"], 1)

    def test_no_primary_maps_first_ordinary_failure_to_lock_error(self) -> None:
        first = OSError(errno.EIO, "unlock failed")
        second = OSError(errno.EIO, "close failed")
        self.ops.failures["flock"] = first
        self.ops.failures["close"] = second
        with self.assertRaises(LockError) as ctx:
            _release_descriptor(self.ops, self.fd, None)
        self.assertEqual(ctx.exception.stage, "unlock")
        self.assertIs(ctx.exception.cause, first)
        self.assertEqual(ctx.exception.secondary, [second])

    def test_each_release_action_is_attempted_exactly_once(self) -> None:
        primary = _Cancellation("original")
        self.ops.failures["flock"] = OSError(errno.EIO, "unlock")
        self.ops.failures["close"] = OSError(errno.EIO, "close")
        with self.assertRaises(_Cancellation):
            _release_descriptor(self.ops, self.fd, primary)
        self.assertEqual(self.ops.counts["flock"], 1)
        self.assertEqual(self.ops.counts["close"], 1)


class DiscardTests(unittest.TestCase):
    """``regular._discard`` keeps an original interruption authoritative."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.ops = InjectedOps()
        self.addCleanup(self.ops.failures.clear)
        self.contracts = RegularFileContracts(self.ops)
        self.directory = DirectoryCapability.from_path(self.ops, self.root)
        self.addCleanup(self.directory.close)
        self.ops.reset()

    def tearDown(self) -> None:
        # Drop injected failures before the registered directory-close cleanup
        # runs so a deliberate close fault cannot escape into teardown.
        self.ops.failures.clear()

    def _state(self, name: str = "temp") -> _Publication:
        path = os.path.join(self.root, name)
        with open(path, "wb") as stream:
            stream.write(b"temporary")
        fd = self.ops.openat(self.directory.fd, name, os.O_RDONLY, 0)
        return _Publication(fd=fd, temp_name=name)

    def test_close_defect_does_not_skip_temporary_unlink(self) -> None:
        state = self._state()
        primary = _Cancellation("original interruption")
        defect = RuntimeError("close defect")
        self.ops.failures["close"] = defect
        with self.assertRaises(_Cancellation) as ctx:
            self.contracts._discard(self.directory, state, primary)
        self.assertIs(ctx.exception, primary)
        self.assertEqual(_secondary_of(primary), [defect])
        self.assertFalse(os.path.exists(os.path.join(self.root, "temp")))
        self.assertEqual(self.ops.counts["unlinkat"], 1)
        self.assertEqual(self.ops.counts["close"], 1)
        self.assertIsNone(state.temp_name)

    def test_later_close_interruption_does_not_skip_temporary_unlink(self) -> None:
        state = self._state()
        primary = KeyboardInterrupt("original interruption")
        later = _Cancellation("later interruption")
        self.ops.failures["close"] = later
        with self.assertRaises(KeyboardInterrupt) as ctx:
            self.contracts._discard(self.directory, state, primary)
        self.assertIs(ctx.exception, primary)
        self.assertEqual(_secondary_of(primary), [later])
        self.assertFalse(os.path.exists(os.path.join(self.root, "temp")))
        self.assertEqual(self.ops.counts["unlinkat"], 1)

    def test_ordinary_close_failure_is_secondary_to_interruption(self) -> None:
        state = self._state()
        primary = _Cancellation("original interruption")
        close_failure = OSError(errno.EIO, "close failed")
        self.ops.failures["close"] = close_failure
        with self.assertRaises(_Cancellation) as ctx:
            self.contracts._discard(self.directory, state, primary)
        self.assertIs(ctx.exception, primary)
        self.assertEqual(_secondary_of(primary), [close_failure])
        self.assertFalse(os.path.exists(os.path.join(self.root, "temp")))

    def test_cleanup_interruption_displaces_exception_primary(self) -> None:
        state = self._state()
        primary = ValueError("original exception")
        interruption = KeyboardInterrupt("cleanup interruption")
        self.ops.failures["close"] = interruption
        with self.assertRaises(KeyboardInterrupt) as ctx:
            self.contracts._discard(self.directory, state, primary)
        self.assertIs(ctx.exception, interruption)
        self.assertEqual(_secondary_of(interruption), [primary])
        self.assertFalse(os.path.exists(os.path.join(self.root, "temp")))
        self.assertEqual(self.ops.counts["unlinkat"], 1)

    def test_absent_temporary_is_idempotent_and_not_retried(self) -> None:
        state = _Publication(fd=-1, temp_name="missing")
        primary = ValueError("original")
        self.contracts._discard(self.directory, state, primary)
        self.assertIsNone(state.temp_name)


if __name__ == "__main__":
    unittest.main()
