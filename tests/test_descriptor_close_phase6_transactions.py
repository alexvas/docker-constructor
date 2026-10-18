"""Phase 6 (tasks 6.2–6.4) — transaction release-site ownership.

RED before migration: ``DirectoryCapability._adopt``, ``open_regular``,
``_verify_unchanged``, ``_release_descriptor``, ``_close_fd``, and
``_discard.close_descriptor`` released raw descriptors with a direct
``ops.close``.  Precedence was already preserved, but the releases had no
shared terminal owner, so the migration contract (terminal-before-close +
at-most-once) is the RED part.  The capability ``close()`` methods already
carry their own terminal, at-most-once state and stay passing.
"""

from __future__ import annotations

import errno
import os
import stat
import tempfile
import unittest
from pathlib import Path

from docker.transactions import capabilities as capabilities_module
from docker.transactions import locking as locking_module
from docker.transactions import regular as regular_module
from docker.transactions.capabilities import (
    CapabilityError,
    DirectoryCapability,
    UnsafeFileError,
)
from docker.transactions.errors import TransactionError
from docker.transactions.locking import LockCapability, LockPolicy
from docker.transactions.regular import RegularFileContracts
from tests.descriptor_ownership_support import recording
from tests.transactions_test_support import InjectedOps


def _non_directory(info: os.stat_result) -> os.stat_result:
    return os.stat_result(
        (
            stat.S_IFREG | 0o600,
            info.st_ino,
            info.st_dev,
            info.st_nlink,
            info.st_uid,
            info.st_gid,
            info.st_size,
            info.st_atime,
            info.st_mtime,
            info.st_ctime,
        )
    )


class _CapabilityCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="phase6-tx-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.ops = InjectedOps()
        self.directory = DirectoryCapability.from_path(self.ops, str(self.root))
        self.addCleanup(self.directory.close)


class AdoptCleanupOwnershipTests(_CapabilityCase):
    def test_adopt_validation_failure_releases_through_shared_owner(self):
        self.ops.fstat_override = _non_directory
        fault = OSError(errno.EIO, "injected close")
        with recording(capabilities_module, close_faults=[fault]) as rec:
            with self.assertRaises(CapabilityError):
                DirectoryCapability.from_path(self.ops, str(self.root))
        rec.assert_terminal_and_at_most_once(self)


class OpenRegularCleanupOwnershipTests(_CapabilityCase):
    def test_open_regular_validation_failure_releases_through_shared_owner(self):
        (self.root / "entry").write_bytes(b"payload")
        fault = OSError(errno.EIO, "injected close")
        with recording(capabilities_module, close_faults=[fault]) as rec:
            with self.assertRaises(UnsafeFileError):
                self.directory.open_regular("entry", allowed_mode=0o600)
        rec.assert_terminal_and_at_most_once(self)


class CapabilityCloseCharacterizationTests(_CapabilityCase):
    def test_directory_close_is_terminal_and_at_most_once(self):
        DirectoryCapability.from_path(self.ops, str(self.root)).close()
        self.assertEqual(self.ops.counts.get("close"), 1)

    def test_file_capability_close_is_terminal_and_at_most_once(self):
        (self.root / "entry").write_bytes(b"payload")
        file_capability = self.directory.open_regular("entry")
        self.ops.reset()
        file_capability.close()
        self.assertTrue(file_capability.closed)
        self.assertEqual(self.ops.counts.get("close"), 1)
        file_capability.close()
        self.assertEqual(self.ops.counts.get("close"), 1)


class LockProbeOwnershipTests(_CapabilityCase):
    def test_verify_unchanged_probe_releases_through_shared_owner(self):
        fault = OSError(errno.EIO, "injected probe close")
        with recording(locking_module, close_faults=[fault]) as rec:
            with self.assertRaises(TransactionError):
                LockCapability.acquire(
                    self.ops,
                    self.directory,
                    "lockfile",
                    namespace="phase6",
                    policy=LockPolicy.BLOCK,
                )
        rec.assert_terminal_and_at_most_once(self)


class ReleaseDescriptorOwnershipTests(_CapabilityCase):
    def test_acquire_stat_failure_release_uses_shared_owner(self):
        def fail_second_fstat(fd):
            raise OSError(errno.EIO, "injected stat")

        fault = OSError(errno.EIO, "injected close")
        with recording(locking_module, close_faults=[fault]) as rec:
            self.ops.failures["fstat"] = fail_second_fstat
            with self.assertRaises(TransactionError):
                LockCapability.acquire(
                    self.ops,
                    self.directory,
                    "lockfile",
                    namespace="phase6",
                    policy=LockPolicy.BLOCK,
                )
        rec.assert_terminal_and_at_most_once(self)


class RegularDiscardOwnershipTests(_CapabilityCase):
    def test_atomic_no_clobber_releases_temporary_through_shared_owner(self):
        contracts = RegularFileContracts(self.ops)
        with recording(regular_module) as rec:
            contracts.atomic_no_clobber(self.directory, "out", b"payload", 0o600)
        rec.assert_terminal_and_at_most_once(self)

    def test_discard_on_failure_releases_temporary_through_shared_owner(self):
        contracts = RegularFileContracts(self.ops)
        self.ops.failures["fchmod"] = OSError(errno.EIO, "injected mode")
        with recording(regular_module) as rec:
            with self.assertRaises(TransactionError):
                contracts.atomic_no_clobber(self.directory, "out", b"payload", 0o600)
        rec.assert_terminal_and_at_most_once(self)
