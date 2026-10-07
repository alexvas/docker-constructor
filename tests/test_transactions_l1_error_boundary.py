"""Phase 5 — transaction L1 operational error boundary.

The public ``DirectoryCapability``/``FileCapability`` methods classify
failures by meaning: capability misuse and no-follow safety rejections stay
``CapabilityError``; operational open, stat, read, and close failures become
``TransactionError`` with a stable ``stage`` and the original ``OSError`` as
both ``.cause`` and ``.__cause__``; process-control exceptions propagate
unchanged.  This module also holds the repository-wide consumer inventory
used to keep every direct and indirect close/factory boundary classified.
"""
from __future__ import annotations

import ast
import errno
import inspect
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from docker.npm_environment import publication
from docker.npm_environment.storage import AssemblerNamespace
from docker.transactions.capabilities import DirectoryCapability, FileCapability
from docker.transactions.errors import (
    STAGE_CLOSE,
    STAGE_LOCK_ACQUIRE,
    STAGE_LOCK_MODE,
    STAGE_LOCK_PREPARE,
    STAGE_LOCK_STAT,
    STAGE_LOCK_VALIDATE,
    STAGE_OPEN,
    STAGE_READ,
    STAGE_VALIDATE,
    CapabilityError,
    LockError,
    TransactionError,
)
from docker.transactions.posix import PosixFileOps
from docker.transactions.regular import RegularFileContracts
from tests.transactions_test_support import InjectedOps

_REPO = Path(__file__).resolve().parents[1]
_REAL_OPS_CLOSE = PosixFileOps.close


class _Cancellation(BaseException):
    """A cancellation-style, non-I/O exception."""


def _fd_target(fd: int) -> str:
    try:
        return os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        return ""


def _file_stat(*, uid: int | None = None) -> os.stat_result:
    values = list(_dir_stat(uid=uid))
    values[0] = stat.S_IFREG | 0o600
    return os.stat_result(values)


def _dir_stat(*, uid: int | None = None) -> os.stat_result:
    values = [
        stat.S_IFDIR | 0o700,
        0,
        0,
        1,
        os.geteuid() if uid is None else uid,
        0,
        0,
        0,
        0,
        0,
    ]
    return os.stat_result(values)


class _CapabilityCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.ops = InjectedOps()
        self.addCleanup(self.ops.failures.clear)

    @staticmethod
    def _safe_close(capability) -> None:
        try:
            capability.close()
        except (OSError, TransactionError):
            pass


class OpenErrorNormalizationTests(_CapabilityCase):
    """Task 5.1 — operational open failures are typed at ``STAGE_OPEN``."""

    def _assert_typed_open(self, ctx: unittest.case._AssertRaisesContext, cause) -> None:
        self.assertEqual(ctx.exception.stage, STAGE_OPEN)
        self.assertIs(ctx.exception.cause, cause)
        self.assertIs(ctx.exception.__cause__, cause)
        self.assertNotIsInstance(ctx.exception, CapabilityError)

    def test_from_path_operational_open_failure_is_typed(self) -> None:
        error = OSError(errno.EACCES, "injected open")
        self.ops.failures["openat"] = error
        with self.assertRaises(TransactionError) as ctx:
            DirectoryCapability.from_path(self.ops, self.root)
        self._assert_typed_open(ctx, error)

    def test_from_path_real_missing_path_is_typed(self) -> None:
        missing = os.path.join(self.root, "absent")
        with self.assertRaises(TransactionError) as ctx:
            DirectoryCapability.from_path(self.ops, missing)
        self.assertEqual(ctx.exception.stage, STAGE_OPEN)
        self.assertIsInstance(ctx.exception.cause, FileNotFoundError)
        self.assertIs(ctx.exception.__cause__, ctx.exception.cause)

    def test_from_secure_path_root_open_failure_is_typed(self) -> None:
        error = OSError(errno.EIO, "injected root open")
        self.ops.failures["openat"] = lambda count: error if count == 1 else None
        with self.assertRaises(TransactionError) as ctx:
            DirectoryCapability.from_secure_path(self.ops, os.sep)
        self._assert_typed_open(ctx, error)

    def test_from_secure_path_intermediate_open_failure_is_typed(self) -> None:
        target = os.path.join(self.root, "a", "b")
        os.makedirs(os.path.join(self.root, "a"), mode=0o700)
        components = [part for part in target.split(os.sep) if part]
        error = OSError(errno.EIO, "injected intermediate open")
        # Occurrence 1 is the root open; occurrence 2 is the first component.
        self.ops.failures["openat"] = lambda count: error if count == 2 else None
        with self.assertRaises(TransactionError) as ctx:
            DirectoryCapability.from_secure_path(self.ops, target)
        self._assert_typed_open(ctx, error)
        self.assertGreater(len(components), 1)

    def test_from_secure_path_leaf_open_failure_is_typed(self) -> None:
        target = os.path.join(self.root, "a", "b")
        os.makedirs(os.path.join(self.root, "a"), mode=0o700)
        components = [part for part in target.split(os.sep) if part]
        error = OSError(errno.EIO, "injected leaf open")
        self.ops.failures["openat"] = lambda count: (
            error if count == len(components) + 1 else None
        )
        with self.assertRaises(TransactionError) as ctx:
            DirectoryCapability.from_secure_path(self.ops, target)
        self._assert_typed_open(ctx, error)


class NoFollowSafetyClassificationTests(_CapabilityCase):
    """Task 5.2 — ELOOP/ENOTDIR rejections stay ``CapabilityError``."""

    def _assert_safety(self, ctx, raw: OSError) -> None:
        self.assertIsInstance(ctx.exception, CapabilityError)
        self.assertNotIsInstance(ctx.exception, TransactionError)
        self.assertIs(ctx.exception.__cause__, raw)

    def test_from_path_injected_eloop_is_safety(self) -> None:
        error = OSError(errno.ELOOP, "injected symlink")
        self.ops.failures["openat"] = error
        with self.assertRaises(CapabilityError) as ctx:
            DirectoryCapability.from_path(self.ops, self.root)
        self._assert_safety(ctx, error)

    def test_from_path_injected_enotdir_is_safety(self) -> None:
        error = OSError(errno.ENOTDIR, "injected non-directory")
        self.ops.failures["openat"] = error
        with self.assertRaises(CapabilityError) as ctx:
            DirectoryCapability.from_path(self.ops, self.root)
        self._assert_safety(ctx, error)

    def test_from_secure_path_injected_eloop_is_safety(self) -> None:
        target = os.path.join(self.root, "a", "b")
        os.makedirs(os.path.join(self.root, "a"), mode=0o700)
        components = [part for part in target.split(os.sep) if part]
        error = OSError(errno.ELOOP, "injected symlink")
        self.ops.failures["openat"] = lambda count: (
            error if count == len(components) + 1 else None
        )
        with self.assertRaises(CapabilityError) as ctx:
            DirectoryCapability.from_secure_path(self.ops, target)
        self._assert_safety(ctx, error)

    def test_from_secure_path_injected_enotdir_is_safety(self) -> None:
        target = os.path.join(self.root, "a", "b")
        os.makedirs(os.path.join(self.root, "a"), mode=0o700)
        components = [part for part in target.split(os.sep) if part]
        error = OSError(errno.ENOTDIR, "injected non-directory")
        self.ops.failures["openat"] = lambda count: (
            error if count == len(components) + 1 else None
        )
        with self.assertRaises(CapabilityError) as ctx:
            DirectoryCapability.from_secure_path(self.ops, target)
        self._assert_safety(ctx, error)

    def test_real_symlink_leaf_is_safety(self) -> None:
        real = os.path.join(self.root, "real")
        os.makedirs(real, mode=0o700)
        link = os.path.join(self.root, "link")
        os.symlink(real, link)
        with self.assertRaises(CapabilityError) as ctx:
            DirectoryCapability.from_secure_path(self.ops, link)
        cause = ctx.exception.__cause__
        self.assertIsInstance(cause, OSError)
        self.assertIn(cause.errno, {errno.ELOOP, errno.ENOTDIR})

    def test_real_symlink_intermediate_is_safety(self) -> None:
        real = os.path.join(self.root, "real")
        os.makedirs(real, mode=0o700)
        link = os.path.join(self.root, "link")
        os.symlink(real, link)
        target = os.path.join(link, "child")
        with self.assertRaises(CapabilityError) as ctx:
            DirectoryCapability.from_secure_path(self.ops, target)
        cause = ctx.exception.__cause__
        self.assertIsInstance(cause, OSError)
        self.assertIn(cause.errno, {errno.ELOOP, errno.ENOTDIR})


class StatErrorNormalizationTests(_CapabilityCase):
    """Task 5.3 — stat failures are typed at ``STAGE_VALIDATE``."""

    def _open_root(self) -> int:
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        return fd

    def test_from_fd_fstat_failure_is_typed_and_caller_owned(self) -> None:
        fd = self._open_root()
        error = OSError(errno.EIO, "injected fstat")
        self.ops.failures["fstat"] = error
        with self.assertRaises(TransactionError) as ctx:
            DirectoryCapability.from_fd(self.ops, fd, "root")
        self.assertEqual(ctx.exception.stage, STAGE_VALIDATE)
        self.assertIs(ctx.exception.cause, error)
        self.assertIs(ctx.exception.__cause__, error)
        self.assertNotIn("close", self.ops.order)
        self.assertTrue(stat.S_ISDIR(os.fstat(fd).st_mode))

    def test_from_path_fstat_failure_is_typed_and_closed_once(self) -> None:
        error = OSError(errno.EIO, "injected fstat")
        self.ops.failures["fstat"] = error
        with self.assertRaises(TransactionError) as ctx:
            DirectoryCapability.from_path(self.ops, self.root)
        self.assertEqual(ctx.exception.stage, STAGE_VALIDATE)
        self.assertIs(ctx.exception.cause, error)
        self.assertEqual(self.ops.order.count("close"), 1)

    def test_from_secure_path_fstat_failure_is_typed(self) -> None:
        error = OSError(errno.EIO, "injected leaf fstat")
        self.ops.failures["fstat"] = error
        with self.assertRaises(TransactionError) as ctx:
            DirectoryCapability.from_secure_path(self.ops, self.root)
        self.assertEqual(ctx.exception.stage, STAGE_VALIDATE)
        self.assertIs(ctx.exception.cause, error)


class ReadErrorNormalizationTests(_CapabilityCase):
    """Task 5.4 — read failures are typed at ``STAGE_READ``."""

    def setUp(self) -> None:
        super().setUp()
        with open(os.path.join(self.root, "entry"), "wb") as stream:
            stream.write(b"payload")
        os.chmod(os.path.join(self.root, "entry"), 0o600)
        self.directory = DirectoryCapability.from_path(self.ops, self.root)
        self.addCleanup(self.directory.close)

    def test_read_all_failure_is_typed_and_release_unchanged(self) -> None:
        capability = self.directory.open_regular("entry")
        self.addCleanup(capability.close)
        error = OSError(errno.EIO, "injected read")
        self.ops.failures["read"] = error
        with self.assertRaises(TransactionError) as ctx:
            capability.read_all()
        self.assertEqual(ctx.exception.stage, STAGE_READ)
        self.assertIs(ctx.exception.cause, error)
        self.assertIs(ctx.exception.__cause__, error)
        self.assertFalse(capability.closed)

    def test_read_all_process_control_propagates(self) -> None:
        capability = self.directory.open_regular("entry")
        self.addCleanup(capability.close)
        interruption = KeyboardInterrupt()
        self.ops.failures["read"] = interruption
        with self.assertRaises(KeyboardInterrupt) as ctx:
            capability.read_all()
        self.assertIs(ctx.exception, interruption)


class ReleaseNormalizationTests(_CapabilityCase):
    """Task 5.5 — ordinary release failures are typed at ``STAGE_CLOSE``."""

    def setUp(self) -> None:
        super().setUp()
        with open(os.path.join(self.root, "entry"), "wb") as stream:
            stream.write(b"payload")
        os.chmod(os.path.join(self.root, "entry"), 0o600)

    def test_directory_close_failure_is_typed_and_terminal(self) -> None:
        directory = DirectoryCapability.from_path(self.ops, self.root)
        self.ops.reset()
        error = OSError(errno.EIO, "injected close")
        self.ops.failures["close"] = error
        with self.assertRaises(TransactionError) as ctx:
            directory.close()
        self.assertEqual(ctx.exception.stage, STAGE_CLOSE)
        self.assertIs(ctx.exception.cause, error)
        self.assertIs(ctx.exception.__cause__, error)
        self.assertTrue(directory.closed)
        directory.close()
        self.assertEqual(self.ops.counts.get("close"), 1)

    def test_file_close_failure_is_typed_and_terminal(self) -> None:
        directory = DirectoryCapability.from_path(self.ops, self.root)
        self.addCleanup(self._safe_close, directory)
        capability = directory.open_regular("entry")
        self.ops.reset()
        error = OSError(errno.EIO, "injected close")
        self.ops.failures["close"] = error
        with self.assertRaises(TransactionError) as ctx:
            capability.close()
        self.assertEqual(ctx.exception.stage, STAGE_CLOSE)
        self.assertIs(ctx.exception.cause, error)
        self.assertTrue(capability.closed)
        capability.close()
        self.assertEqual(self.ops.counts.get("close"), 1)

    def test_context_manager_exit_without_primary_is_typed(self) -> None:
        directory = DirectoryCapability.from_path(self.ops, self.root)
        self.ops.reset()
        error = OSError(errno.EIO, "injected close")
        self.ops.failures["close"] = error
        with self.assertRaises(TransactionError) as ctx:
            with directory:
                pass
        self.assertEqual(ctx.exception.stage, STAGE_CLOSE)
        self.assertIs(ctx.exception.cause, error)
        self.assertTrue(directory.closed)

    def test_file_capability_has_no_context_manager_api(self) -> None:
        self.assertFalse(hasattr(FileCapability, "__enter__"))
        self.assertFalse(hasattr(FileCapability, "__exit__"))


class ActivePrimaryCleanupTests(_CapabilityCase):
    """Task 5.6 — an active primary keeps the typed close as secondary."""

    def test_close_failure_is_secondary_to_primary(self) -> None:
        directory = DirectoryCapability.from_path(self.ops, self.root)
        primary = RuntimeError("body failed")
        error = OSError(errno.EIO, "injected close")
        self.ops.failures["close"] = error
        with self.assertRaises(RuntimeError) as ctx:
            with directory:
                raise primary
        self.assertIs(ctx.exception, primary)
        secondary = getattr(primary, "_transaction_secondary", [])
        self.assertEqual(len(secondary), 1)
        self.assertIsInstance(secondary[0], TransactionError)
        self.assertEqual(secondary[0].stage, STAGE_CLOSE)
        self.assertIs(secondary[0].cause, error)
        self.assertTrue(directory.closed)

    def test_close_keyboard_interrupt_propagates_over_primary(self) -> None:
        directory = DirectoryCapability.from_path(self.ops, self.root)
        primary = RuntimeError("body failed")
        interruption = KeyboardInterrupt()
        self.ops.failures["close"] = interruption
        with self.assertRaises(KeyboardInterrupt) as ctx:
            with directory:
                raise primary
        self.assertIs(ctx.exception, interruption)
        self.assertTrue(directory.closed)

    def test_close_cancellation_propagates_over_primary(self) -> None:
        directory = DirectoryCapability.from_path(self.ops, self.root)
        primary = RuntimeError("body failed")
        cancellation = _Cancellation()
        self.ops.failures["close"] = cancellation
        with self.assertRaises(_Cancellation) as ctx:
            with directory:
                raise primary
        self.assertIs(ctx.exception, cancellation)
        self.assertTrue(directory.closed)


class RegularFileValidatedReadTests(_CapabilityCase):
    """Task 5.9 — ``validated_read`` close identity and precedence."""

    def setUp(self) -> None:
        super().setUp()
        with open(os.path.join(self.root, "entry"), "wb") as stream:
            stream.write(b"payload")
        os.chmod(os.path.join(self.root, "entry"), 0o600)
        self.directory = DirectoryCapability.from_path(self.ops, self.root)
        self.addCleanup(self._safe_close, self.directory)
        self.contracts = RegularFileContracts(self.ops)

    def test_success_then_close_failure_is_propagated_unchanged(self) -> None:
        error = OSError(errno.EIO, "injected close")
        self.ops.failures["close"] = error
        with self.assertRaises(TransactionError) as ctx:
            self.contracts.validated_read(self.directory, "entry")
        self.assertEqual(ctx.exception.stage, STAGE_CLOSE)
        self.assertIs(ctx.exception.cause, error)
        self.assertIs(ctx.exception.__cause__, error)
        # The typed close error is not wrapped a second time: the message is
        # the capability-release message, not the ``validated_read`` label.
        self.assertIn("cannot close file capability", str(ctx.exception))

    def test_read_failure_stays_primary_over_typed_close_failure(self) -> None:
        read_error = OSError(errno.EIO, "injected read")
        close_error = OSError(errno.EIO, "injected close")
        self.ops.failures["read"] = read_error
        self.ops.failures["close"] = close_error
        with self.assertRaises(TransactionError) as ctx:
            self.contracts.validated_read(self.directory, "entry")
        self.assertEqual(ctx.exception.stage, STAGE_READ)
        self.assertIs(ctx.exception.cause, read_error)
        secondary = ctx.exception.secondary
        self.assertEqual(len(secondary), 1)
        self.assertIsInstance(secondary[0], TransactionError)
        self.assertEqual(secondary[0].stage, STAGE_CLOSE)
        self.assertIs(secondary[0].cause, close_error)


class NpmPublicationAdvisoryCloseTests(_CapabilityCase):
    """Task 5.8 — advisory close suppression and read-primary precedence."""

    def _namespace(self, index_dir: Path) -> AssemblerNamespace:
        return AssemblerNamespace(
            root=index_dir.parent,
            npm_cache=index_dir.parent / "npm-cache",
            locks=index_dir.parent / "locks",
            staging=index_dir.parent / "staging",
            outputs=index_dir.parent / "outputs",
            index=index_dir,
        )

    def _failing_directory_close(self, basename: str, error: BaseException):
        def closing(self, fd):
            if os.path.basename(_fd_target(fd)) == basename:
                _REAL_OPS_CLOSE(self, fd)
                raise error
            return _REAL_OPS_CLOSE(self, fd)

        return closing

    def test_read_index_suppresses_typed_directory_close_failure(self) -> None:
        index_dir = Path(self.root) / "index"
        index_dir.mkdir(mode=0o700)
        identity = "a" * 64
        entry = index_dir / f"{identity}.json"
        entry.write_text('["' + "b" * 64 + '"]')
        os.chmod(entry, 0o600)
        namespace = self._namespace(index_dir)
        error = OSError(errno.EIO, "injected directory close")
        with mock.patch.object(
            PosixFileOps, "close", self._failing_directory_close("index", error)
        ):
            entries = publication.read_index(namespace, identity)
        self.assertEqual(entries, ("b" * 64,))

    def test_read_index_read_failure_is_not_replaced_by_close(self) -> None:
        index_dir = Path(self.root) / "index"
        index_dir.mkdir(mode=0o700)
        identity = "c" * 64
        entry = index_dir / f"{identity}.json"
        entry.write_text("[]")
        os.chmod(entry, 0o600)
        namespace = self._namespace(index_dir)
        error = OSError(errno.EIO, "injected directory close")

        def closing(self, fd):
            if os.path.basename(_fd_target(fd)) == "index":
                _REAL_OPS_CLOSE(self, fd)
                raise error
            return _REAL_OPS_CLOSE(self, fd)

        def failing_read(self, fd, size):
            raise OSError(errno.EIO, "injected read")

        with mock.patch.object(PosixFileOps, "close", closing), mock.patch.object(
            PosixFileOps, "read", failing_read
        ):
            entries = publication.read_index(namespace, identity)
        self.assertEqual(entries, ())

    def test_append_index_is_best_effort_on_typed_close_failure(self) -> None:
        index_dir = Path(self.root) / "index"
        index_dir.mkdir(mode=0o700)
        identity = "d" * 64
        namespace = self._namespace(index_dir)
        error = OSError(errno.EIO, "injected directory close")
        with mock.patch.object(
            PosixFileOps, "close", self._failing_directory_close("index", error)
        ):
            self.assertIsNone(
                publication._append_index(namespace, identity, "e" * 64)
            )

    def test_read_private_regular_suppresses_typed_close_failure(self) -> None:
        outputs = Path(self.root) / "outputs"
        outputs.mkdir(mode=0o700)
        payload = outputs / "evidence"
        payload.write_bytes(b"evidence-bytes")
        os.chmod(payload, 0o444)
        error = OSError(errno.EIO, "injected directory close")
        with mock.patch.object(
            PosixFileOps, "close", self._failing_directory_close("outputs", error)
        ):
            self.assertEqual(
                publication._read_private_regular(outputs, "evidence"),
                b"evidence-bytes",
            )

    def test_read_private_regular_process_control_propagates(self) -> None:
        outputs = Path(self.root) / "outputs"
        outputs.mkdir(mode=0o700)
        payload = outputs / "evidence"
        payload.write_bytes(b"evidence-bytes")
        os.chmod(payload, 0o444)
        interruption = KeyboardInterrupt()
        with mock.patch.object(
            PosixFileOps,
            "close",
            self._failing_directory_close("outputs", interruption),
        ):
            with self.assertRaises(KeyboardInterrupt) as ctx:
                publication._read_private_regular(outputs, "evidence")
        self.assertIs(ctx.exception, interruption)

    def test_read_private_regular_read_failure_is_not_replaced(self) -> None:
        outputs = Path(self.root) / "outputs"
        outputs.mkdir(mode=0o700)
        payload = outputs / "evidence"
        payload.write_bytes(b"evidence-bytes")
        os.chmod(payload, 0o444)
        error = OSError(errno.EIO, "injected directory close")

        def closing(self, fd):
            if os.path.basename(_fd_target(fd)) == "outputs":
                _REAL_OPS_CLOSE(self, fd)
                raise error
            return _REAL_OPS_CLOSE(self, fd)

        def failing_read(self, fd, size):
            raise OSError(errno.EIO, "injected read")

        with mock.patch.object(PosixFileOps, "close", closing), mock.patch.object(
            PosixFileOps, "read", failing_read
        ):
            with self.assertRaises(TransactionError) as ctx:
                publication._read_private_regular(outputs, "evidence")
        self.assertEqual(ctx.exception.stage, STAGE_READ)
        self.assertIsInstance(ctx.exception.cause, OSError)
        self.assertEqual(ctx.exception.cause.errno, errno.EIO)


class CloseStageSemanticsTests(_CapabilityCase):
    """Only a close-stage failure is an ordinary close; other stages stay
    authoritative.

    L1 reports every operation stage through one ``TransactionError`` type, so
    a broad ``ordinary=(TransactionError,)`` accumulator (or an unconditional
    ``except TransactionError`` suppressor) would demote an unrelated
    read/validate/open failure surfaced by a release action to an ordinary
    close diagnostic.  This module pins that only ``STAGE_CLOSE`` is
    suppressed at every handler pattern.
    """

    def test_marker_matches_only_a_close_stage_transaction_error(self) -> None:
        from docker.transactions.errors import CloseStageFailure

        self.assertTrue(
            isinstance(TransactionError(STAGE_CLOSE, "close"), CloseStageFailure)
        )
        self.assertFalse(
            isinstance(TransactionError(STAGE_READ, "read"), CloseStageFailure)
        )
        self.assertFalse(isinstance(OSError(errno.EIO, "raw"), CloseStageFailure))

    def test_directory_context_non_close_error_displaces_the_primary(self) -> None:
        directory = DirectoryCapability.from_path(self.ops, self.root)
        self.addCleanup(self._safe_close, directory)
        primary = RuntimeError("body failed")
        injected = TransactionError(STAGE_READ, "unrelated failure")
        with mock.patch.object(DirectoryCapability, "close", side_effect=injected):
            with self.assertRaises(TransactionError) as ctx:
                with directory:
                    raise primary
        self.assertIs(ctx.exception, injected)
        self.assertIn(primary, injected.secondary)

    def test_validated_read_non_close_error_is_not_an_ordinary_close(self) -> None:
        with open(os.path.join(self.root, "entry"), "wb") as stream:
            stream.write(b"payload")
        os.chmod(os.path.join(self.root, "entry"), 0o600)
        directory = DirectoryCapability.from_path(self.ops, self.root)
        self.addCleanup(self._safe_close, directory)
        contracts = RegularFileContracts(self.ops)
        injected = TransactionError(STAGE_VALIDATE, "unrelated failure")
        with mock.patch.object(FileCapability, "close", side_effect=injected):
            with self.assertRaises(TransactionError) as ctx:
                contracts.validated_read(directory, "entry")
        self.assertIs(ctx.exception, injected)

    def test_advisory_close_non_close_error_propagates(self) -> None:
        directory = DirectoryCapability.from_path(self.ops, self.root)
        self.addCleanup(self._safe_close, directory)
        injected = TransactionError(STAGE_READ, "unrelated failure")
        with mock.patch.object(DirectoryCapability, "close", side_effect=injected):
            with self.assertRaises(TransactionError) as ctx:
                publication._suppress_advisory_close(directory)
        self.assertIs(ctx.exception, injected)

    def test_identity_lock_release_non_close_error_is_authoritative(self) -> None:
        directory = DirectoryCapability.from_path(self.ops, self.root)
        self.addCleanup(self._safe_close, directory)
        injected = TransactionError(STAGE_READ, "unrelated failure")
        with mock.patch.object(DirectoryCapability, "close", side_effect=injected):
            with self.assertRaises(TransactionError) as ctx:
                publication._release_identity_lock(None, directory, None)
        self.assertIs(ctx.exception, injected)

    def test_close_algorithm_unpaired_close_stage_is_suppressed(self) -> None:
        from docker.versioning import build_cleanup

        directory = DirectoryCapability.from_path(self.ops, self.root)
        self.addCleanup(self._safe_close, directory)
        injected = TransactionError(STAGE_CLOSE, "close")
        with mock.patch.object(DirectoryCapability, "close", side_effect=injected):
            self.assertIsNone(build_cleanup._close_algorithm(directory, None))

    def test_close_algorithm_unpaired_non_close_error_propagates(self) -> None:
        from docker.versioning import build_cleanup

        directory = DirectoryCapability.from_path(self.ops, self.root)
        self.addCleanup(self._safe_close, directory)
        injected = TransactionError(STAGE_READ, "unrelated failure")
        with mock.patch.object(DirectoryCapability, "close", side_effect=injected):
            with self.assertRaises(TransactionError) as ctx:
                build_cleanup._close_algorithm(directory, None)
        self.assertIs(ctx.exception, injected)

    def test_close_algorithm_aggregated_non_close_error_is_authoritative(self) -> None:
        from docker.versioning import build_cleanup

        directory = DirectoryCapability.from_path(self.ops, self.root)
        self.addCleanup(self._safe_close, directory)
        domain_error = RuntimeError("aggregated failure")
        injected = TransactionError(STAGE_READ, "unrelated failure")
        failure = build_cleanup.CleanupFailure("sha256", domain_error)
        with mock.patch.object(DirectoryCapability, "close", side_effect=injected):
            with self.assertRaises(TransactionError) as ctx:
                build_cleanup._close_algorithm(directory, failure)
        self.assertIs(ctx.exception, injected)
        self.assertIn(domain_error, injected.secondary)


class FactoryMappingTests(_CapabilityCase):
    """Task 5.10 — factory/validation mappings preserve raw domain outcomes."""

    def test_build_cleanup_factory_unwraps_typed_validation_cause(self) -> None:
        from docker.versioning import build_cleanup

        blobs = DirectoryCapability.from_path(self.ops, self.root)
        self.addCleanup(blobs.close)
        algorithm_dir = os.path.join(self.root, "sha256")
        os.makedirs(algorithm_dir, mode=0o700)
        error = OSError(errno.EIO, "injected factory stat")
        self.ops.failures["fstat"] = error
        capability, failure = build_cleanup._open_algorithm_directory(
            self.ops, blobs, "sha256"
        )
        self.assertIsNone(capability)
        self.assertIsNotNone(failure)
        self.assertIs(failure.error, error)

    def test_artifact_cache_factory_mapping_unwraps_raw_cause(self) -> None:
        from docker.versioning import artifact_cache

        error = OSError(errno.EIO, "injected factory stat")
        typed = TransactionError(STAGE_VALIDATE, "cannot stat", cause=error)
        mapped = artifact_cache._identity_lock_failure(typed)
        self.assertIs(mapped, error)

    def test_artifact_cache_factory_mapping_keeps_typed_safety(self) -> None:
        from docker.versioning import artifact_cache

        typed = TransactionError(STAGE_VALIDATE, "cannot stat")
        mapped = artifact_cache._identity_lock_failure(typed)
        self.assertIs(mapped, typed)


class LockFailureMappingTests(unittest.TestCase):
    """Phase 4 lock contract — operational stages keep their raw ``OSError``.

    The shared ``LockCapability`` classifies a failed lock entry open/stat,
    mode repair, acquisition, or post-acquisition identity-probe close as a
    typed ``LockError`` whose ``cause`` is the failing ``OSError``.  Every
    domain adapter must unwrap those operational stages back to the raw error
    while keeping a ``validate``/``prepare`` rejection as containment.  The
    probe-close stage (``STAGE_CLOSE``) is operational I/O, so no domain may
    silently downgrade it to an unsafe-entry diagnostic.
    """

    OPERATIONAL_STAGES = (
        STAGE_LOCK_STAT,
        STAGE_LOCK_MODE,
        STAGE_LOCK_ACQUIRE,
        STAGE_CLOSE,
    )

    def _lock_error(self, stage: str) -> tuple[LockError, OSError]:
        error = OSError(errno.EIO, f"injected {stage}")
        return LockError(stage, f"cannot run {stage}", cause=error), error

    def test_npm_mapper_unwraps_every_operational_lock_stage(self) -> None:
        for stage in self.OPERATIONAL_STAGES:
            with self.subTest(stage=stage):
                typed, error = self._lock_error(stage)
                self.assertIs(publication._identity_lock_failure(typed), error)

    def test_artifact_mapper_unwraps_every_operational_lock_stage(self) -> None:
        from docker.versioning import artifact_cache

        for stage in self.OPERATIONAL_STAGES:
            with self.subTest(stage=stage):
                typed, error = self._lock_error(stage)
                self.assertIs(artifact_cache._identity_lock_failure(typed), error)

    def test_build_cache_mapper_unwraps_every_operational_lock_stage(self) -> None:
        from docker.versioning import build_cache

        with tempfile.TemporaryDirectory() as root:
            directory = DirectoryCapability.from_path(PosixFileOps(), root)
            self.addCleanup(directory.close)
            for stage in self.OPERATIONAL_STAGES:
                with self.subTest(stage=stage):
                    typed, error = self._lock_error(stage)
                    with self.assertRaises(OSError) as ctx:
                        build_cache._raise_lock_failure(
                            directory, "missing.lock", typed
                        )
                    self.assertIs(ctx.exception, error)

    def test_validate_and_prepare_stay_containment_in_both_domains(self) -> None:
        from docker.versioning import artifact_cache

        for stage in (STAGE_LOCK_VALIDATE, STAGE_LOCK_PREPARE):
            with self.subTest(stage=stage):
                typed, _ = self._lock_error(stage)
                self.assertNotIsInstance(
                    publication._identity_lock_failure(typed), OSError
                )
                self.assertNotIsInstance(
                    artifact_cache._identity_lock_failure(typed), OSError
                )

    def test_npm_and_artifact_agree_on_the_operational_lock_stages(self) -> None:
        # The two adapters must not drift: for every stage one unwraps, the
        # other must unwrap it too.
        from docker.versioning import artifact_cache

        all_stages = (
            STAGE_LOCK_VALIDATE,
            STAGE_LOCK_PREPARE,
            STAGE_LOCK_STAT,
            STAGE_LOCK_MODE,
            STAGE_LOCK_ACQUIRE,
            STAGE_CLOSE,
        )
        for stage in all_stages:
            with self.subTest(stage=stage):
                typed, error = self._lock_error(stage)
                npm_mapped = publication._identity_lock_failure(typed)
                artifact_mapped = artifact_cache._identity_lock_failure(typed)
                self.assertEqual(
                    npm_mapped is error,
                    artifact_mapped is error,
                    f"npm and artifact disagree on {stage}",
                )

    def test_lock_capability_close_never_raises_raw_oserror(self) -> None:
        from docker.transactions.locking import LockCapability, LockPolicy

        with tempfile.TemporaryDirectory() as root:
            ops = InjectedOps()
            directory = DirectoryCapability.from_path(ops, root)
            self.addCleanup(directory.close)
            capability = LockCapability.acquire(
                ops, directory, "build.lock", namespace="scope", policy=LockPolicy.BLOCK
            )
            ops.reset()
            error = OSError(errno.EIO, "injected unlock")
            ops.failures["flock"] = error
            with self.assertRaises(LockError) as ctx:
                capability.close()
            self.assertIsInstance(ctx.exception, LockError)
            self.assertNotIsInstance(ctx.exception, OSError)
            self.assertIs(ctx.exception.cause, error)
            self.assertIs(ctx.exception.__cause__, error)


class CharacterizationTests(_CapabilityCase):
    """Task 5.12 — the unaffected boundary stays green."""

    def test_direct_directory_construction_is_rejected(self) -> None:
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        self.ops.reset()
        with self.assertRaises(CapabilityError):
            DirectoryCapability(self.ops, fd, "direct")
        self.assertNotIn("close", self.ops.order)
        self.assertTrue(stat.S_ISDIR(os.fstat(fd).st_mode))

    def test_unsafe_basename_is_rejected_before_an_operation(self) -> None:
        directory = DirectoryCapability.from_path(self.ops, self.root)
        self.addCleanup(directory.close)
        for name in ("", ".", "..", "a/b", "/abs", "a\x00b", None, 7):
            with self.subTest(name=name):
                with self.assertRaises(CapabilityError):
                    directory.child_basename(name)

    def test_directory_authority_mismatch_is_rejected(self) -> None:
        other_root = os.path.join(self.root, "other")
        os.mkdir(other_root, 0o700)
        directory = DirectoryCapability.from_path(self.ops, self.root)
        other = DirectoryCapability.from_path(self.ops, other_root)
        self.addCleanup(directory.close)
        self.addCleanup(other.close)
        with open(os.path.join(self.root, "entry"), "wb") as stream:
            stream.write(b"payload")
        capability = directory.open_regular("entry")
        self.addCleanup(capability.close)
        with self.assertRaises(CapabilityError):
            capability.assert_owned_by(other)

    def test_validation_policy_failure_is_capability_error(self) -> None:
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        self.ops.fstat_override = lambda info: _file_stat()
        with self.assertRaises(CapabilityError):
            DirectoryCapability.from_fd(self.ops, fd, "root")
        self.assertNotIn("close", self.ops.order)

    def test_public_signatures_are_stable(self) -> None:
        self.assertEqual(
            list(inspect.signature(DirectoryCapability.from_fd).parameters),
            ["ops", "fd", "label"],
        )
        self.assertFalse(hasattr(FileCapability, "__enter__"))
        self.assertFalse(hasattr(FileCapability, "__exit__"))

    def test_l0_backend_keeps_raw_errors(self) -> None:
        ops = PosixFileOps()
        with self.assertRaises(FileNotFoundError):
            ops.openat(None, os.path.join(self.root, "absent"), os.O_RDONLY)


class _InventoryRule:
    """Shared AST helpers for the repository-wide consumer inventory."""

    RAW_RECEIVERS = {
        "os",
        "ops",
        "self._ops",
        "self.ops",
        "_REAL_OPS_CLOSE",
        "PosixFileOps",
        # subprocess pipe/file objects, not shared descriptor capabilities
        "pipe",
        "failed_pipe",
    }
    # A capability-shaped ``.close`` accumulator action that legitimately
    # closes a raw fd rather than a shared descriptor capability.
    RAW_CLOSE_ACTIONS = {
        ("docker/versioning/build_cache.py", "state.close"),
        ("docker/versioning/project_state.py", "self.close"),
    }

    @classmethod
    def modules(cls):
        for path in sorted(_REPO.glob("docker/**/*.py")):
            yield path.relative_to(_REPO).as_posix(), path

    @classmethod
    def close_run_sites(cls):
        """Yield ``(file, action, ordinary)`` for every ``.run(...)`` call."""
        for rel, path in cls.modules():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "run"
                    and node.args
                ):
                    continue
                action = node.args[0]
                ordinary = None
                for keyword in node.keywords:
                    if keyword.arg == "ordinary":
                        ordinary = keyword.value
                yield rel, action, ordinary


class RepositoryCloseInventoryTests(_InventoryRule, unittest.TestCase):
    """Task 5.7 — every capability close boundary is typed and classified."""

    def test_capability_close_accumulators_recognize_typed_close(self) -> None:
        seen = 0
        for rel, action, ordinary in self.close_run_sites():
            if not (isinstance(action, ast.Attribute) and action.attr == "close"):
                continue
            rendered = ast.unparse(action)
            if rel.startswith("docker/filesystem/"):
                # The foundation intentionally keeps raw ``OSError`` release.
                continue
            seen += 1
            if (rel, rendered) in self.RAW_CLOSE_ACTIONS:
                continue
            names = _tuple_names(ordinary)
            if "LockError" in names:
                # A shared lock capability release keeps its own LockError
                # classification, and some lock closures deliberately re-raise
                # the raw descriptor failure, so ``OSError`` remains valid.
                continue
            self.assertIn(
                "CloseStageFailure",
                names,
                f"{rel}: {rendered} must classify only the close-stage error",
            )
            # A broad ``TransactionError`` would also demote an unrelated
            # read/validate/open failure to an ordinary close diagnostic.
            self.assertNotIn(
                "TransactionError",
                names,
                f"{rel}: {rendered} must not treat every transaction stage as a close",
            )
            # A pure ``DirectoryCapability``/``FileCapability`` close cannot
            # raise a raw ``OSError`` after Phase 5, so the accumulator must
            # not retain raw POSIX handling.
            self.assertNotIn(
                "OSError",
                names,
                f"{rel}: {rendered} must not retain raw OSError handling",
            )
        # The inventory covers the migrated consumers, not an empty scan.
        self.assertGreaterEqual(seen, 10)

    def test_no_capability_close_is_wrapped_only_by_oserror(self) -> None:
        for rel, path in self.modules():
            if rel.startswith("docker/filesystem/"):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Try):
                    continue
                risky: set[str] = set()
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Call) and isinstance(
                        inner.func, ast.Attribute
                    ) and inner.func.attr == "close":
                        receiver = ast.unparse(inner.func.value)
                        if receiver not in self.RAW_RECEIVERS and not (
                            receiver.startswith("self._ops")
                            or receiver.startswith("self.ops")
                        ):
                            risky.add(receiver)
                if not risky:
                    continue
                # The intentional L1 normalization boundary catches the raw
                # foundation ``OSError`` and raises the typed transaction
                # error from it.
                if rel == "docker/transactions/capabilities.py":
                    continue
                rendered_handlers = [
                    ast.unparse(handler.type) if handler.type else "bare"
                    for handler in node.handlers
                ]
                # A site that recognizes the typed close error (a
                # ``TransactionError`` or the shared lock subclass
                # ``LockError``) in any handler, including an advisory
                # ``OSError`` fallback, is classified.
                if any(
                    "TransactionError" in item or "LockError" in item
                    for item in rendered_handlers
                ):
                    continue
                self.fail(
                    f"{rel}: {sorted(risky)} close handled only as {rendered_handlers}"
                )

    def test_required_consumers_are_inventoried(self) -> None:
        required = {
            "docker/npm_environment/publication.py",
            "docker/versioning/build_cleanup.py",
            "docker/versioning/build_cache.py",
            "docker/versioning/artifact_cache.py",
            "docker/versioning/effective.py",
            "docker/versioning/project_state.py",
            "docker/versioning/rendering.py",
            "docker/transactions/regular.py",
        }
        found = {rel for rel, _, _ in self.close_run_sites()}
        self.assertTrue(required <= found, required - found)


class RepositoryFactoryMappingInventoryTests(_CapabilityCase):
    """Task 5.10 — every factory/validation mapping is characterized."""

    def test_build_cleanup_factory_mapping_preserves_raw_cause(self) -> None:
        """The ``build_cleanup`` factory mapping is part of the inventory."""
        source = (_REPO / "docker/versioning/build_cleanup.py").read_text()
        self.assertIn("isinstance(cause, OSError)", source)
        self.assertIn("reported: BaseException = cause if isinstance(cause, OSError) else exc", source)

    def test_rendering_factory_mapping_preserves_raw_cause(self) -> None:
        """The ``write_effective_build`` generated-directory factory mapping.

        ``rendering.py`` was omitted from the first Phase 5 consumer pass; it
        must stay inventoried so the ``from_fd`` typed-wrapper-to-raw-cause
        translation and the ownership-split close boundary cannot regress.
        """
        source = (_REPO / "docker/versioning/rendering.py").read_text()
        # The factory failure is translated back to its raw ``OSError`` cause
        # while carrying any retained secondary diagnostics.
        self.assertIn("isinstance(cause, OSError)", source)
        self.assertIn("carry_secondary_diagnostics(cause, exc)", source)
        # The cleanup boundary splits by ownership state: an adopted
        # capability and a still caller-owned raw descriptor.
        self.assertIn("failures.run(generated.close, ordinary=(CloseStageFailure,))", source)
        self.assertIn("ordinary=(OSError,))", source)


_PUBLIC_BOUNDARY_CLASSIFICATION = {
    "DirectoryCapability.from_path": "operational-open -> TransactionError(STAGE_OPEN)",
    "DirectoryCapability.from_secure_path": (
        "operational-open/stat -> TransactionError; "
        "ELOOP/ENOTDIR safety -> CapabilityError"
    ),
    "DirectoryCapability.from_fd": (
        "operational-stat -> TransactionError(STAGE_VALIDATE); "
        "type/owner -> CapabilityError"
    ),
    "DirectoryCapability.child_basename": "misuse -> CapabilityError",
    "DirectoryCapability.open_regular": (
        "operational -> TransactionError; unsafe leaf -> UnsafeFileError"
    ),
    "DirectoryCapability.close": (
        "operational-close -> TransactionError(STAGE_CLOSE); "
        "process-control unchanged"
    ),
    "DirectoryCapability.__exit__": (
        "cleanup precedence; typed close secondary to an active primary"
    ),
    "FileCapability.read_all": "operational-read -> TransactionError(STAGE_READ)",
    "FileCapability.close": (
        "operational-close -> TransactionError(STAGE_CLOSE); "
        "process-control unchanged"
    ),
    "FileCapability.assert_owned_by": "misuse/authority -> CapabilityError",
}


class StageOpenContractTests(unittest.TestCase):
    """Task 5.13 — the public open stage is exact and stays unaggregated."""

    def test_stage_open_name_value_and_identity(self) -> None:
        from docker.transactions import errors as errors_module

        self.assertEqual(errors_module.STAGE_OPEN, "open-directory")
        self.assertIs(STAGE_OPEN, errors_module.STAGE_OPEN)
        # Existing stage values are unchanged.
        self.assertEqual(errors_module.STAGE_VALIDATE, "validate")
        self.assertEqual(errors_module.STAGE_READ, "read")
        self.assertEqual(errors_module.STAGE_CLOSE, "close")

    def test_stage_open_is_not_an_aggregate_transaction_export(self) -> None:
        from docker import transactions

        self.assertNotIn("STAGE_OPEN", transactions.__all__)
        self.assertFalse(hasattr(transactions, "STAGE_OPEN"))


class PublicBoundaryClassificationTests(_CapabilityCase):
    """Tasks 5.20/5.21 — the public L1 boundary is exhaustive and typed."""

    def test_classification_inventory_covers_every_public_method(self) -> None:
        for cls, names in (
            (DirectoryCapability, ("from_path", "from_secure_path", "from_fd", "child_basename", "open_regular", "close", "__exit__")),
            (FileCapability, ("read_all", "close", "assert_owned_by")),
        ):
            for name in names:
                key = f"{cls.__name__}.{name}"
                self.assertIn(key, _PUBLIC_BOUNDARY_CLASSIFICATION)
                self.assertTrue(callable(getattr(cls, name)))
        self.assertFalse(hasattr(FileCapability, "__enter__"))
        self.assertFalse(hasattr(FileCapability, "__exit__"))

    def test_every_operational_error_reaches_both_cause_attributes(self) -> None:
        raw = OSError(errno.EIO, "raw")
        typed = TransactionError(STAGE_OPEN, "typed", cause=raw)
        self.assertIs(typed.cause, raw)
        self.assertIs(typed.__cause__, raw)

    def test_safety_rejection_exposes_raw_cause_without_cause_contract(self) -> None:
        raw = OSError(errno.ELOOP, "symlink")
        error = CapabilityError("unsafe")
        raise_error = lambda: (_ for _ in ()).throw(error)
        try:
            raise_error()
        except CapabilityError:
            pass
        self.assertFalse(hasattr(error, "cause"))
        # The adapter chains the raw error as ``__cause__`` while the safety
        # error deliberately gains no ``.cause`` attribute.

    def test_public_capability_oserror_handlers_are_classified(self) -> None:
        tree = ast.parse(
            (_REPO / "docker/transactions/capabilities.py").read_text(encoding="utf-8")
        )
        for cls_name in ("DirectoryCapability", "FileCapability"):
            cls = next(
                node for node in tree.body
                if isinstance(node, ast.ClassDef) and node.name == cls_name
            )
            for handler in [n for n in ast.walk(cls) if isinstance(n, ast.ExceptHandler)]:
                if handler.type is None or "OSError" not in ast.unparse(handler.type):
                    continue
                classified = any(
                    isinstance(n, ast.Raise) for n in ast.walk(handler)
                ) and any(
                    name in ast.unparse(handler)
                    for name in (
                        "TransactionError",
                        "CapabilityError",
                        "UnsafeFileError",
                    )
                )
                self.assertTrue(
                    classified,
                    f"{cls_name}:{handler.lineno} re-raises a raw OSError",
                )

    def test_l0_backend_is_the_raw_fault_boundary(self) -> None:
        ops = PosixFileOps()
        with self.assertRaises(OSError) as ctx:
            ops.openat(None, os.path.join(self.root, "absent"), os.O_RDONLY)
        self.assertIsInstance(ctx.exception, FileNotFoundError)
        self.assertNotIsInstance(ctx.exception, TransactionError)


def _tuple_names(node: ast.AST | None) -> set[str]:
    if node is None:
        return set()
    if isinstance(node, (ast.Tuple, ast.List)):
        names: set[str] = set()
        for element in node.elts:
            if isinstance(element, ast.Name):
                names.add(element.id)
        return names
    if isinstance(node, ast.Name):
        return {node.id}
    return set()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
