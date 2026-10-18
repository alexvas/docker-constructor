"""Phase 6 (tasks 6.14 / 6.32) — cache-storage descriptor ownership.

RED before migration: ``_release_file_and_capability`` and
``_release_file_keeping_primary`` each released a raw ``os.open``/``openat``
descriptor with a direct ``ops.close`` (inside a lambda).  Operation-primary
precedence, at-most-once release, and completion of both independent releases
were already correct in ``tests/versioning/test_cache_storage_capabilities.py``,
but there was no shared terminal owner.  These tests require the migrated
contract: the file descriptor is adopted into an ``OwnedDescriptor`` and every
release delegates to ``owner.close`` (terminal before the syscall, never
retried), while the existing precedence and completion guarantees hold.
"""

from __future__ import annotations

import errno
import os
import tempfile
import unittest
from pathlib import Path

from docker.filesystem.descriptors import DirectoryDescriptor
from docker.filesystem.operations import PosixDescriptorOps
from docker.versioning import cache_storage as cache_storage_module
from tests.descriptor_ownership_support import recording, secondary_diagnostics


def _fd_path(fd: int) -> str | None:
    """Return the normalized path an open descriptor refers to, or ``None``."""
    try:
        return os.path.normpath(os.readlink(f"/proc/self/fd/{fd}"))
    except OSError:
        return None


class _PathKeyedOps(PosixDescriptorOps):
    """A real POSIX backend that records closes and injects faults by path."""

    def __init__(self) -> None:
        self.closed_paths: list[str | None] = []
        self.close_errors: dict[str, BaseException] = {}

    def close(self, fd: int) -> None:
        path = _fd_path(fd)
        self.closed_paths.append(path)
        error = self.close_errors.get(path) if path is not None else None
        if error is not None:
            raise error
        super().close(fd)


class _FchmodFailOps(PosixDescriptorOps):
    """A real POSIX backend whose ``fchmod`` always fails."""

    def fchmod(self, fd: int, mode: int) -> None:
        raise OSError(errno.EIO, "injected fchmod failure")


class _CacheStoragePhase6Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="phase6-cache-storage-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.versioning = self.root / "versioning"
        self.versioning.mkdir(mode=0o700)
        os.chmod(self.versioning, 0o700)
        self.entry = self.versioning / "entry"

    def _open_entry_fd(self) -> int:
        return os.open(self.entry, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)

    def _capability(self, ops: PosixDescriptorOps) -> DirectoryDescriptor:
        return DirectoryDescriptor.open_secure_path(
            ops, str(self.versioning), label=str(self.versioning), require_owner=True
        )


class ReleaseFileAndCapabilityTests(_CacheStoragePhase6Case):
    def test_releases_file_through_shared_owner_before_capability(self) -> None:
        ops = _PathKeyedOps()
        capability = self._capability(ops)
        fd = self._open_entry_fd()
        primary = RuntimeError("primary operation failure")
        with recording(cache_storage_module) as rec:
            owner = cache_storage_module.OwnedDescriptor(ops, fd, label="entry")
            cache_storage_module._release_file_and_capability(
                owner, capability, primary
            )

        rec.assert_terminal_and_at_most_once(self)
        self.assertIn(os.path.normpath(str(self.entry)), ops.closed_paths)
        self.assertIn(os.path.normpath(str(self.versioning)), ops.closed_paths)
        self.assertLess(
            ops.closed_paths.index(os.path.normpath(str(self.entry))),
            ops.closed_paths.index(os.path.normpath(str(self.versioning))),
            "the file owner must be released before the directory capability",
        )

    def test_ordinary_close_failures_are_secondary_and_both_attempted(self) -> None:
        ops = _PathKeyedOps()
        capability = self._capability(ops)
        fd = self._open_entry_fd()
        file_error = OSError(errno.EIO, "injected file close failure")
        dir_error = OSError(errno.EIO, "injected directory close failure")
        ops.close_errors[os.path.normpath(str(self.entry))] = file_error
        ops.close_errors[os.path.normpath(str(self.versioning))] = dir_error
        primary = cache_storage_module.CacheStorageError("cannot secure")
        with recording(cache_storage_module) as rec:
            owner = cache_storage_module.OwnedDescriptor(ops, fd, label="entry")
            cache_storage_module._release_file_and_capability(
                owner, capability, primary
            )

        rec.assert_terminal_and_at_most_once(self)
        self.assertIn(os.path.normpath(str(self.entry)), ops.closed_paths)
        self.assertIn(os.path.normpath(str(self.versioning)), ops.closed_paths)
        secondary = secondary_diagnostics(primary)
        self.assertIn(file_error, secondary)
        self.assertIn(dir_error, secondary)

    def test_unadopted_file_releases_only_the_capability(self) -> None:
        ops = _PathKeyedOps()
        capability = self._capability(ops)
        primary = RuntimeError("open failed before adoption")
        cache_storage_module._release_file_and_capability(None, capability, primary)

        self.assertIn(os.path.normpath(str(self.versioning)), ops.closed_paths)
        self.assertNotIn(os.path.normpath(str(self.entry)), ops.closed_paths)


class ReleaseFileKeepingPrimaryTests(_CacheStoragePhase6Case):
    def test_releases_file_through_shared_owner(self) -> None:
        ops = _PathKeyedOps()
        fd = self._open_entry_fd()
        primary = OSError(errno.EIO, "directory close failure")
        with recording(cache_storage_module) as rec:
            owner = cache_storage_module.OwnedDescriptor(ops, fd, label="entry")
            cache_storage_module._release_file_keeping_primary(owner, primary)

        rec.assert_terminal_and_at_most_once(self)
        self.assertIn(os.path.normpath(str(self.entry)), ops.closed_paths)

    def test_ordinary_file_close_failure_is_secondary(self) -> None:
        ops = _PathKeyedOps()
        fd = self._open_entry_fd()
        file_error = OSError(errno.EIO, "injected file close failure")
        ops.close_errors[os.path.normpath(str(self.entry))] = file_error
        primary = OSError(errno.EIO, "directory close failure")
        with recording(cache_storage_module) as rec:
            owner = cache_storage_module.OwnedDescriptor(ops, fd, label="entry")
            cache_storage_module._release_file_keeping_primary(owner, primary)

        rec.assert_terminal_and_at_most_once(self)
        self.assertIn(file_error, secondary_diagnostics(primary))


class OpenPrivateEntryOwnershipTests(_CacheStoragePhase6Case):
    def test_success_transfers_the_file_owner_to_the_caller(self) -> None:
        with recording(cache_storage_module) as rec:
            fd = cache_storage_module.open_private_entry(self.versioning, "entry")
        try:
            self.assertEqual(len(rec.owners), 1)
            self.assertFalse(rec.owners[0].released)
            self.assertEqual(rec.close_calls, [])
        finally:
            os.close(fd)

    def test_secure_failure_releases_the_file_through_a_shared_owner(self) -> None:
        ops = _FchmodFailOps()
        with recording(cache_storage_module) as rec:
            with self.assertRaises(cache_storage_module.CacheStorageError) as ctx:
                cache_storage_module.open_private_entry(
                    self.versioning, "entry", ops=ops
                )
        self.assertIn("cannot secure", str(ctx.exception))
        rec.assert_terminal_and_at_most_once(self)
