"""Phase 2 tasks 2.1-2.4 — owner-private advisory locks.

The shared lock layer validates a no-follow, single-link, owner-owned regular
lock entry, acquires it under an explicit contention policy, and returns a
namespace-bound live capability.  Unsafe entries are rejected without
mutation, repair happens only after exclusive acquisition, and release is
unconditional with primary-error-preserving unlock/close handling.
"""
from __future__ import annotations

import errno
import fcntl
import multiprocessing
import os
import stat
import tempfile
import unittest

from docker.transactions.capabilities import DirectoryCapability
from docker.transactions.errors import (
    STAGE_CLOSE,
    STAGE_LOCK_ACQUIRE,
    STAGE_LOCK_PREPARE,
    STAGE_LOCK_VALIDATE,
    STAGE_UNLOCK,
    CapabilityError,
    LockContention,
    LockError,
    TransactionError,
)
from docker.transactions.locking import LockCapability, LockPolicy
from docker.transactions.posix import PosixFileOps
from tests.transactions_test_support import InjectedOps


class _Cancellation(BaseException):
    """A cancellation-style, non-I/O exception."""


def _with_uid(info: os.stat_result, uid: int) -> os.stat_result:
    """Return *info* with a replaced ``st_uid`` for ownership testing."""
    return os.stat_result(
        (
            info.st_mode,
            info.st_ino,
            info.st_dev,
            info.st_nlink,
            uid,
            info.st_gid,
            info.st_size,
            info.st_atime,
            info.st_mtime,
            info.st_ctime,
        )
    )


def _deny_access(name: str, *accesses: int):
    """Deny ``openat`` of *name* for the given access modes with ``EACCES``.

    Injecting the denial makes the restrictive-mode paths deterministic even
    when the suite runs with elevated privileges that would otherwise bypass
    the kernel permission check.
    """

    def hook(dir_fd, entry, flags, mode=0o777) -> None:
        if entry != name:
            return
        if getattr(os, "O_PATH", 0) and flags & os.O_PATH:
            return
        if (flags & os.O_ACCMODE) in accesses:
            raise OSError(errno.EACCES, "injected access denial")

    return hook


class _ElevatedOpenOps(InjectedOps):
    """Let the ``O_RDWR`` fast open succeed even on a mode-0000 lock.

    A root or ``CAP_DAC_OVERRIDE`` process can open a file whose mode denies
    its owner access, so the descriptor is genuinely read-write while ``fstat``
    still reports the authoritative ``0000`` mode.  The kernel bypass is
    simulated portably by momentarily granting owner read-write around the real
    ``openat`` and restoring the on-disk mode immediately afterwards.
    """

    def __init__(self, name: str) -> None:
        super().__init__()
        self._lock_name = name

    def openat(self, dir_fd, name, flags, mode=0o777):
        elevated = (
            name == self._lock_name
            and not (getattr(os, "O_PATH", 0) and flags & os.O_PATH)
            and (flags & os.O_ACCMODE) == os.O_RDWR
        )
        if not elevated:
            return super().openat(dir_fd, name, flags, mode)
        os.chmod(name, 0o600, dir_fd=dir_fd)
        try:
            return super().openat(dir_fd, name, flags, mode)
        finally:
            os.chmod(name, 0o000, dir_fd=dir_fd)


class _LockTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.ops = InjectedOps()
        self.ops.failures.clear()
        self.addCleanup(self.ops.failures.clear)
        self.directory = DirectoryCapability.from_path(self.ops, self.root)
        self.addCleanup(self._close_directory)

    def _close_directory(self) -> None:
        try:
            if not self.directory.closed:
                self.directory.close()
        except (OSError, TransactionError):
            pass

    def _close_capability(self, capability: LockCapability) -> None:
        try:
            if not capability.closed:
                capability.close()
        except (OSError, TransactionError):
            pass

    def path(self, name: str) -> str:
        return os.path.join(self.root, name)

    def acquire(
        self,
        name: str = "build.lock",
        *,
        namespace: str = "scope-a",
        policy: LockPolicy = LockPolicy.BLOCK,
    ) -> LockCapability:
        capability = LockCapability.acquire(
            self.ops, self.directory, name, namespace=namespace, policy=policy
        )
        self.addCleanup(self._close_capability, capability)
        return capability

    def make_lock_file(self, name: str = "build.lock", mode: int = 0o600) -> str:
        path = self.path(name)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(path, mode)
        return path


class LockEntryTests(_LockTestCase):
    def test_creates_missing_lock_file_owner_private(self) -> None:
        capability = self.acquire()
        info = os.stat(self.path("build.lock"))
        self.assertTrue(stat.S_ISREG(info.st_mode))
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
        self.assertEqual(info.st_uid, os.geteuid())
        self.assertEqual(info.st_nlink, 1)
        self.assertFalse(capability.closed)

    def test_repairs_safe_wrong_mode_only_after_acquisition(self) -> None:
        path = self.make_lock_file(mode=0o644)
        self.ops.reset()
        self.acquire()
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(self.ops.counts.get("fchmod"), 1)
        self.assertLess(self.ops.order.index("flock"), self.ops.order.index("fchmod"))

    # The following group pins *open* behavior: which access mode the fallback
    # chooses when the kernel denies read-write.  Owner accessibility itself is
    # authoritative from st_mode, covered by
    # test_mode_0000_is_rejected_even_when_fast_open_succeeds.
    def test_repairs_read_only_lock_mode(self) -> None:
        # A mode 0400 entry only fails O_RDWR; the owner can still open it for
        # read, so it must be locked and then repaired to 0600.
        path = self.make_lock_file(mode=0o400)
        self.ops.reset()
        capability = self.acquire()
        self.assertFalse(capability.closed)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(self.ops.counts.get("fchmod"), 1)
        self.assertLess(self.ops.order.index("flock"), self.ops.order.index("fchmod"))

    def test_repairs_read_only_mode_when_rdwr_is_denied(self) -> None:
        # Deterministic even under elevated privileges: force the EACCES an
        # unprivileged owner sees on the O_RDWR fast path and prove the
        # fallback still locks before repairing.
        path = self.make_lock_file(mode=0o400)
        self.ops.hooks["openat"] = _deny_access("build.lock", os.O_RDWR)
        self.ops.reset()
        capability = self.acquire()
        self.assertFalse(capability.closed)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(self.ops.counts.get("fchmod"), 1)
        self.assertLess(self.ops.order.index("flock"), self.ops.order.index("fchmod"))

    def test_repairs_write_only_mode_when_rdwr_and_rdonly_are_denied(self) -> None:
        path = self.make_lock_file(mode=0o200)
        self.ops.hooks["openat"] = _deny_access(
            "build.lock", os.O_RDWR, os.O_RDONLY
        )
        self.ops.reset()
        capability = self.acquire()
        self.assertFalse(capability.closed)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(self.ops.counts.get("fchmod"), 1)
        self.assertLess(self.ops.order.index("flock"), self.ops.order.index("fchmod"))

    def test_restrictive_mode_without_owner_access_fails_closed(self) -> None:
        # A mode granting neither read nor write cannot be locked before
        # repair, so the layer fails closed and never chmods it.  The denial is
        # injected so the outcome is the same under elevated privileges.
        path = self.make_lock_file(mode=0o000)
        self.ops.hooks["openat"] = _deny_access(
            "build.lock", os.O_RDWR, os.O_RDONLY, os.O_WRONLY
        )
        self.ops.reset()
        with self.assertRaises(LockError) as ctx:
            self.acquire()
        self.assertEqual(ctx.exception.stage, STAGE_LOCK_PREPARE)
        self.assertEqual(self.ops.counts.get("flock", 0), 0)
        self.assertEqual(self.ops.counts.get("fchmod", 0), 0)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o000)

    def test_real_mode_0000_fails_closed_for_unprivileged_owner(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("requires an unprivileged owner to observe EACCES")
        path = self.make_lock_file(mode=0o000)
        self.ops.reset()
        with self.assertRaises(LockError) as ctx:
            self.acquire()
        self.assertEqual(ctx.exception.stage, STAGE_LOCK_PREPARE)
        self.assertEqual(self.ops.counts.get("fchmod", 0), 0)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o000)

    def test_mode_0000_is_rejected_even_when_fast_open_succeeds(self) -> None:
        # Simulates root / CAP_DAC_OVERRIDE: the O_RDWR fast open succeeds on a
        # mode-0000 entry, so open success cannot be trusted.  Owner
        # accessibility must come from st_mode, and the entry must stay
        # unmodified even though a usable descriptor exists.  Runs under both
        # root and non-root without skipping.
        path = self.make_lock_file(mode=0o000)
        self.ops = _ElevatedOpenOps("build.lock")
        self.ops.reset()
        with self.assertRaises(LockError) as ctx:
            self.acquire()
        self.assertEqual(ctx.exception.stage, STAGE_LOCK_VALIDATE)
        self.assertEqual(self.ops.counts.get("fchmod", 0), 0)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o000)
        lock_operations = [args[1] for args in self.ops.arg_pairs("flock")]
        self.assertIn(fcntl.LOCK_EX, lock_operations)
        self.assertIn(fcntl.LOCK_UN, lock_operations)
        self.assertGreaterEqual(self.ops.counts.get("close", 0), 1)

    def test_creates_lock_repair_with_restrictive_umask(self) -> None:
        # A missing entry created by this acquisition is repaired to exactly
        # 0600 even when a restrictive umask stripped its owner bits.  The
        # known-new inode is created O_EXCL, so it is distinguishable from a
        # pre-existing inaccessible entry.
        path = self.path("build.lock")
        original = os.umask(0o777)
        try:
            self.ops.reset()
            capability = self.acquire()
        finally:
            os.umask(original)
        self.assertFalse(capability.closed)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(self.ops.counts.get("fchmod"), 1)
        self.assertLess(self.ops.order.index("flock"), self.ops.order.index("fchmod"))

    def test_restrictive_umask_does_not_repair_preexisting_inaccessible_lock(self) -> None:
        # The umask exception applies only to the inode created by the current
        # acquisition.  A pre-existing mode-0000 entry must still fail closed
        # without fchmod, under the same restrictive umask.
        path = self.make_lock_file(mode=0o000)
        original = os.umask(0o777)
        try:
            self.ops.reset()
            with self.assertRaises(LockError) as ctx:
                self.acquire()
        finally:
            os.umask(original)
        # Unprivileged open denial fails before flock; a privileged fast open is
        # rejected by st_mode validation after flock.  Neither repairs.
        self.assertIn(ctx.exception.stage, (STAGE_LOCK_PREPARE, STAGE_LOCK_VALIDATE))
        self.assertEqual(self.ops.counts.get("fchmod", 0), 0)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o000)

    def test_read_only_symlink_entry_is_rejected_without_repair(self) -> None:
        target = self.path("target")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("data")
        os.chmod(target, 0o400)
        link = self.path("build.lock")
        os.symlink(target, link)
        self.ops.reset()
        with self.assertRaises(LockError):
            self.acquire()
        self.assertTrue(os.path.islink(link))
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o400)
        self.assertEqual(self.ops.counts.get("fchmod", 0), 0)

    def test_read_only_foreign_owned_entry_is_rejected_without_repair(self) -> None:
        path = self.make_lock_file(mode=0o400)
        self.ops.fstat_override = lambda info: _with_uid(info, os.geteuid() + 1)
        self.ops.reset()
        with self.assertRaises(LockError):
            self.acquire()
        self.assertEqual(self.ops.counts.get("fchmod", 0), 0)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o400)

    def test_read_only_multiply_linked_entry_is_rejected_without_repair(self) -> None:
        path = self.make_lock_file(mode=0o400)
        os.link(path, self.path("build.lock.extra"))
        self.ops.reset()
        with self.assertRaises(LockError):
            self.acquire()
        self.assertEqual(self.ops.counts.get("fchmod", 0), 0)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o400)

    def test_repairs_read_only_mode_only_after_acquisition(self) -> None:
        # Ordering proof for the restrictive path: flock precedes fchmod.
        path = self.make_lock_file(mode=0o400)
        self.ops.reset()
        self.acquire()
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertLess(self.ops.order.index("flock"), self.ops.order.index("fchmod"))

    def test_symlink_entry_is_rejected_without_mutation(self) -> None:
        target = self.path("target")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("data")
        os.chmod(target, 0o644)
        link = self.path("build.lock")
        os.symlink(target, link)
        self.ops.reset()
        with self.assertRaises(LockError):
            self.acquire()
        self.assertTrue(os.path.islink(link))
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o644)
        self.assertEqual(self.ops.counts.get("fchmod", 0), 0)

    def test_directory_entry_is_rejected_without_mutation(self) -> None:
        path = self.path("build.lock")
        os.mkdir(path)
        os.chmod(path, 0o700)
        self.ops.reset()
        with self.assertRaises(LockError):
            self.acquire()
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o700)
        self.assertEqual(self.ops.counts.get("fchmod", 0), 0)

    def test_non_regular_fifo_entry_is_rejected_without_mutation(self) -> None:
        path = self.path("build.lock")
        os.mkfifo(path, 0o600)
        self.ops.reset()
        with self.assertRaises(LockError):
            self.acquire()
        self.assertTrue(stat.S_ISFIFO(os.stat(path).st_mode))
        self.assertEqual(self.ops.counts.get("fchmod", 0), 0)

    def test_foreign_owned_entry_is_rejected_without_mutation(self) -> None:
        path = self.make_lock_file(mode=0o644)
        foreign = os.geteuid() + 1
        self.ops.fstat_override = lambda info: _with_uid(info, foreign)
        self.ops.reset()
        with self.assertRaises(LockError):
            self.acquire()
        self.assertEqual(self.ops.counts.get("fchmod", 0), 0)
        self.assertEqual(os.stat(path).st_uid, os.geteuid())
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o644)

    def test_multiply_linked_entry_is_rejected_without_mutation(self) -> None:
        path = self.make_lock_file(mode=0o644)
        extra = self.path("build.lock.extra")
        os.link(path, extra)
        self.ops.reset()
        with self.assertRaises(LockError):
            self.acquire()
        self.assertEqual(os.stat(path).st_nlink, 2)
        self.assertTrue(os.path.exists(extra))
        self.assertEqual(self.ops.counts.get("fchmod", 0), 0)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o644)

    def test_bootstrap_race_replacement_is_rejected(self) -> None:
        path = self.make_lock_file(mode=0o644)

        def replace(fd: int, operation: int) -> None:
            os.unlink(path)
            new_fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(new_fd)
            os.chmod(path, 0o644)

        self.ops.hooks["flock"] = replace
        self.ops.reset()
        with self.assertRaises(LockError):
            self.acquire()
        # The replacement entry was never repaired.
        self.assertEqual(self.ops.counts.get("fchmod", 0), 0)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o644)

    def test_bootstrap_race_unlink_is_rejected(self) -> None:
        path = self.make_lock_file(mode=0o644)
        self.ops.hooks["flock"] = lambda *_: os.unlink(path)
        self.ops.reset()
        with self.assertRaises(LockError):
            self.acquire()
        self.assertEqual(self.ops.counts.get("fchmod", 0), 0)

    def test_link_added_after_validation_is_rejected(self) -> None:
        path = self.make_lock_file(mode=0o644)
        extra = self.path("build.lock.extra")

        def add_link_before_probe(dir_fd, name, flags, mode=0o777) -> None:
            # The first openat is the lock entry; the second is the probe.
            if self.ops.counts.get("openat") == 2:
                os.link(path, extra)

        self.ops.hooks["openat"] = add_link_before_probe
        self.ops.reset()
        with self.assertRaises(LockError):
            self.acquire()
        self.assertEqual(self.ops.counts.get("fchmod", 0), 0)
        self.assertEqual(os.stat(path).st_nlink, 2)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o644)

    def test_unsafe_entry_leaves_unrelated_ancestors_untouched(self) -> None:
        sibling = self.path("unrelated.txt")
        with open(sibling, "w", encoding="utf-8") as handle:
            handle.write("keep")
        os.chmod(sibling, 0o644)
        target = self.path("target")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("data")
        os.chmod(target, 0o640)
        before_dir = stat.S_IMODE(os.stat(self.root).st_mode)
        os.symlink(target, self.path("build.lock"))
        with self.assertRaises(LockError):
            self.acquire()
        self.assertEqual(stat.S_IMODE(os.stat(self.root).st_mode), before_dir)
        self.assertEqual(stat.S_IMODE(os.stat(sibling).st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o640)

    def test_repair_does_not_run_when_acquisition_is_interrupted(self) -> None:
        path = self.make_lock_file(mode=0o644)
        self.ops.failures["flock"] = lambda count: KeyboardInterrupt() if count == 1 else None
        self.ops.reset()
        with self.assertRaises(KeyboardInterrupt):
            self.acquire()
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o644)
        self.assertEqual(self.ops.counts.get("fchmod", 0), 0)

    def test_repair_does_not_run_when_restrictive_acquisition_is_interrupted(self) -> None:
        path = self.make_lock_file(mode=0o400)
        self.ops.failures["flock"] = lambda count: KeyboardInterrupt() if count == 1 else None
        self.ops.reset()
        with self.assertRaises(KeyboardInterrupt):
            self.acquire()
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o400)
        self.assertEqual(self.ops.counts.get("fchmod", 0), 0)

    def test_invalid_namespace_is_rejected_before_open(self) -> None:
        self.ops.reset()
        for bad in ("", None, 123, "a\x00b"):
            with self.assertRaises(LockError):
                LockCapability.acquire(
                    self.ops,
                    self.directory,
                    "build.lock",
                    namespace=bad,
                    policy=LockPolicy.BLOCK,
                )
        self.assertEqual(self.ops.counts.get("openat", 0), 0)

    def test_invalid_policy_value_is_rejected_before_open(self) -> None:
        self.ops.reset()
        for bad in (None, "block", 1):
            with self.assertRaises(LockError):
                LockCapability.acquire(
                    self.ops,
                    self.directory,
                    "build.lock",
                    namespace="scope-a",
                    policy=bad,
                )
        self.assertEqual(self.ops.counts.get("openat", 0), 0)

    def test_omitted_contention_policy_is_rejected(self) -> None:
        with self.assertRaises(TypeError):
            LockCapability.acquire(
                self.ops, self.directory, "build.lock", namespace="scope-a"
            )

    def test_unsafe_basename_is_rejected(self) -> None:
        for bad in ("", ".", "..", "a/b", "../x"):
            with self.assertRaises(CapabilityError):
                LockCapability.acquire(
                    self.ops,
                    self.directory,
                    bad,
                    namespace="scope-a",
                    policy=LockPolicy.BLOCK,
                )


class LockAuthorizationTests(_LockTestCase):
    """Task 2.3 — capability authorization before protected mutation."""

    def test_live_matching_capability_authorizes(self) -> None:
        capability = self.acquire(namespace="scope-a")
        capability.assert_authorizes(directory=self.directory, namespace="scope-a")

    def test_released_capability_is_rejected_before_mutation(self) -> None:
        capability = self.acquire(namespace="scope-a")
        capability.close()
        with self.assertRaises(CapabilityError):
            capability.assert_authorizes(directory=self.directory, namespace="scope-a")

    def test_cross_namespace_capability_is_rejected_before_mutation(self) -> None:
        capability = self.acquire(namespace="scope-a")
        with self.assertRaises(CapabilityError):
            capability.assert_authorizes(directory=self.directory, namespace="scope-b")

    def test_wrong_root_capability_is_rejected_before_mutation(self) -> None:
        subdir = self.path("sub")
        os.mkdir(subdir)
        other = DirectoryCapability.from_path(self.ops, subdir)
        self.addCleanup(other.close)
        capability = self.acquire(namespace="scope-a")
        with self.assertRaises(CapabilityError):
            capability.assert_authorizes(directory=other, namespace="scope-a")

    def test_released_directory_capability_is_rejected(self) -> None:
        capability = self.acquire(namespace="scope-a")
        self.directory.close()
        with self.assertRaises(CapabilityError):
            capability.assert_authorizes(directory=self.directory, namespace="scope-a")

    def test_failed_authorization_leaves_protected_state_unmutated(self) -> None:
        capability = self.acquire(namespace="scope-a")
        capability.close()
        state = {"value": "unchanged"}
        with self.assertRaises(CapabilityError):
            capability.assert_authorizes(directory=self.directory, namespace="scope-a")
            state["value"] = "mutated"
        self.assertEqual(state["value"], "unchanged")


class LockLifecycleTests(_LockTestCase):
    """Task 2.4 — deterministic lifecycle and primary-preserving release."""

    def test_context_manager_releases_on_success(self) -> None:
        capability = self.acquire()
        self.ops.reset()
        with capability:
            self.assertFalse(capability.closed)
        self.assertTrue(capability.closed)
        self.assertIn("flock", self.ops.order)
        self.assertIn("close", self.ops.order)

    def test_releases_on_ordinary_exception_and_preserves_primary(self) -> None:
        capability = self.acquire()
        self.ops.reset()
        primary = RuntimeError("body failed")
        with self.assertRaises(RuntimeError) as ctx:
            with capability:
                raise primary
        self.assertIs(ctx.exception, primary)
        self.assertTrue(capability.closed)
        self.assertIn("flock", self.ops.order)
        self.assertIn("close", self.ops.order)

    def test_releases_on_interruption_and_preserves_interruption(self) -> None:
        capability = self.acquire()
        self.ops.reset()
        interruption = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt) as ctx:
            with capability:
                raise interruption
        self.assertIs(ctx.exception, interruption)
        self.assertTrue(capability.closed)

    def test_releases_on_cancellation_and_preserves_cancellation(self) -> None:
        capability = self.acquire()
        self.ops.reset()
        cancellation = _Cancellation()
        with self.assertRaises(_Cancellation) as ctx:
            with capability:
                raise cancellation
        self.assertIs(ctx.exception, cancellation)
        self.assertTrue(capability.closed)

    def test_validation_failure_closes_descriptor_without_repair(self) -> None:
        path = self.make_lock_file(mode=0o644)
        os.link(path, self.path("build.lock.extra"))
        self.ops.reset()
        with self.assertRaises(LockError):
            LockCapability.acquire(
                self.ops,
                self.directory,
                "build.lock",
                namespace="scope-a",
                policy=LockPolicy.BLOCK,
            )
        self.assertGreaterEqual(self.ops.counts.get("close", 0), 1)
        self.assertEqual(self.ops.counts.get("fchmod", 0), 0)

    def test_contention_failure_closes_descriptor(self) -> None:
        self.acquire()
        self.ops.reset()
        with self.assertRaises(LockContention):
            LockCapability.acquire(
                self.ops,
                self.directory,
                "build.lock",
                namespace="scope-a",
                policy=LockPolicy.FAIL_FAST,
            )
        self.assertEqual(self.ops.counts.get("close"), 1)
        self.assertEqual(self.ops.counts.get("fchmod", 0), 0)

    def _inject_acquire_flock_error(self, error: OSError) -> None:
        # Fail the first flock (the nonblocking acquisition) but let the
        # release unlock proceed so the descriptor is actually freed.
        self.ops.failures["flock"] = lambda count: error if count == 1 else None

    def test_nonblocking_eacces_is_contention(self) -> None:
        self.acquire()
        self.ops.reset()
        denied = OSError(errno.EACCES, "injected nonblocking contention")
        self._inject_acquire_flock_error(denied)
        with self.assertRaises(LockContention) as ctx:
            LockCapability.acquire(
                self.ops,
                self.directory,
                "build.lock",
                namespace="scope-a",
                policy=LockPolicy.FAIL_FAST,
            )
        self.assertEqual(ctx.exception.stage, STAGE_LOCK_ACQUIRE)
        self.assertIs(ctx.exception.cause, denied)
        self.assertIs(ctx.exception.__cause__, denied)
        self.assertGreaterEqual(self.ops.counts.get("close", 0), 1)
        self.assertEqual(self.ops.counts.get("fchmod", 0), 0)

    def test_nonblocking_eagain_is_contention(self) -> None:
        self.acquire()
        self.ops.reset()
        blocked = OSError(errno.EAGAIN, "injected nonblocking contention")
        self._inject_acquire_flock_error(blocked)
        with self.assertRaises(LockContention) as ctx:
            LockCapability.acquire(
                self.ops,
                self.directory,
                "build.lock",
                namespace="scope-a",
                policy=LockPolicy.FAIL_FAST,
            )
        self.assertEqual(ctx.exception.stage, STAGE_LOCK_ACQUIRE)
        self.assertIs(ctx.exception.cause, blocked)
        self.assertIs(ctx.exception.__cause__, blocked)
        self.assertGreaterEqual(self.ops.counts.get("close", 0), 1)
        self.assertEqual(self.ops.counts.get("fchmod", 0), 0)

    def test_unrelated_acquire_error_is_lock_error(self) -> None:
        self.acquire()
        self.ops.reset()
        failure = OSError(errno.EIO, "injected flock failure")
        self._inject_acquire_flock_error(failure)
        with self.assertRaises(LockError) as ctx:
            LockCapability.acquire(
                self.ops,
                self.directory,
                "build.lock",
                namespace="scope-a",
                policy=LockPolicy.FAIL_FAST,
            )
        self.assertNotIsInstance(ctx.exception, LockContention)
        self.assertEqual(ctx.exception.stage, STAGE_LOCK_ACQUIRE)
        self.assertIs(ctx.exception.cause, failure)
        self.assertIs(ctx.exception.__cause__, failure)
        self.assertGreaterEqual(self.ops.counts.get("close", 0), 1)
        self.assertEqual(self.ops.counts.get("fchmod", 0), 0)

    def test_unlock_failure_propagates_when_no_primary(self) -> None:
        capability = self.acquire()
        self.ops.reset()
        unlock_error = OSError(errno.EIO, "injected unlock")
        self.ops.failures["flock"] = unlock_error
        with self.assertRaises(LockError) as ctx:
            capability.close()
        self.assertEqual(ctx.exception.stage, STAGE_UNLOCK)
        self.assertIs(ctx.exception.__cause__, unlock_error)
        self.assertEqual(self.ops.counts.get("close"), 1)
        self.assertTrue(capability.closed)

    def test_unlock_failure_is_secondary_to_primary(self) -> None:
        capability = self.acquire()
        self.ops.reset()
        unlock_error = OSError(errno.EIO, "injected unlock")
        self.ops.failures["flock"] = unlock_error
        primary = RuntimeError("body failed")
        with self.assertRaises(RuntimeError) as ctx:
            with capability:
                raise primary
        self.assertIs(ctx.exception, primary)
        self.assertEqual(getattr(primary, "_transaction_secondary", None), [unlock_error])
        self.assertEqual(self.ops.counts.get("close"), 1)

    def test_close_failure_propagates_when_no_primary(self) -> None:
        capability = self.acquire()
        self.ops.reset()
        close_error = OSError(errno.EIO, "injected close")
        self.ops.failures["close"] = close_error
        with self.assertRaises(LockError) as ctx:
            capability.close()
        self.assertEqual(ctx.exception.stage, STAGE_CLOSE)
        self.assertIs(ctx.exception.__cause__, close_error)
        self.assertTrue(capability.closed)

    def test_close_failure_is_secondary_to_primary(self) -> None:
        capability = self.acquire()
        close_error = OSError(errno.EIO, "injected close")
        self.ops.failures["close"] = close_error
        primary = RuntimeError("body failed")
        with self.assertRaises(RuntimeError) as ctx:
            with capability:
                raise primary
        self.assertIs(ctx.exception, primary)
        self.assertEqual(getattr(primary, "_transaction_secondary", None), [close_error])

    def test_close_is_attempted_even_when_unlock_fails(self) -> None:
        capability = self.acquire()
        self.ops.reset()
        unlock_error = OSError(errno.EIO, "injected unlock")
        close_error = OSError(errno.EIO, "injected close")
        self.ops.failures["flock"] = unlock_error
        self.ops.failures["close"] = close_error
        with self.assertRaises(LockError) as ctx:
            capability.close()
        self.assertEqual(self.ops.counts.get("close"), 1)
        self.assertEqual(ctx.exception.stage, STAGE_UNLOCK)
        self.assertEqual(ctx.exception.secondary, [close_error])

    def test_release_is_idempotent(self) -> None:
        capability = self.acquire()
        self.ops.reset()
        capability.close()
        first = dict(self.ops.counts)
        capability.close()
        self.assertEqual(self.ops.counts, first)

    def test_release_interruption_propagates_over_primary(self) -> None:
        capability = self.acquire()
        self.ops.reset()
        interruption = KeyboardInterrupt()
        self.ops.failures["flock"] = interruption
        primary = RuntimeError("body failed")
        with self.assertRaises(KeyboardInterrupt) as ctx:
            with capability:
                raise primary
        self.assertIs(ctx.exception, interruption)
        # Release is unconditional: an interrupted unlock still closes once.
        self.assertEqual(self.ops.counts.get("close"), 1)
        self.assertTrue(capability.closed)

    def test_release_cancellation_propagates_over_primary(self) -> None:
        capability = self.acquire()
        self.ops.reset()
        cancellation = _Cancellation()
        self.ops.failures["flock"] = cancellation
        primary = RuntimeError("body failed")
        with self.assertRaises(_Cancellation) as ctx:
            with capability:
                raise primary
        self.assertIs(ctx.exception, cancellation)
        self.assertEqual(self.ops.counts.get("close"), 1)
        self.assertTrue(capability.closed)

    def test_close_failure_is_secondary_to_interrupted_unlock(self) -> None:
        capability = self.acquire()
        self.ops.reset()
        interruption = KeyboardInterrupt()
        close_error = OSError(errno.EIO, "injected close")
        self.ops.failures["flock"] = interruption
        self.ops.failures["close"] = close_error
        with self.assertRaises(KeyboardInterrupt) as ctx:
            capability.close()
        self.assertIs(ctx.exception, interruption)
        self.assertEqual(self.ops.counts.get("close"), 1)
        self.assertEqual(
            getattr(ctx.exception, "_transaction_secondary", None), [close_error]
        )
        self.assertTrue(capability.closed)


def _run_child(
    directory_path: str,
    name: str,
    namespace: str,
    policy_value: str,
    started,
    acquired,
    results,
) -> None:
    """Child entry point: acquire one lock and report the outcome."""
    ops = PosixFileOps()
    directory = DirectoryCapability.from_path(ops, directory_path)
    started.set()
    try:
        capability = LockCapability.acquire(
            ops, directory, name, namespace=namespace, policy=LockPolicy(policy_value)
        )
    except LockContention:
        results.put("contention")
        directory.close()
        return
    except LockError as exc:  # pragma: no cover - defensive
        results.put(("error", str(exc)))
        directory.close()
        return
    acquired.set()
    results.put("acquired")
    capability.close()
    directory.close()


class LockProcessTests(unittest.TestCase):
    """Task 2.2 — process-level exclusion, blocking, and namespaces."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.ops = PosixFileOps()
        self.directory = DirectoryCapability.from_path(self.ops, self.root)
        self.addCleanup(self.directory.close)
        self.ctx = multiprocessing.get_context("fork")

    def _spawn(self, name: str, namespace: str, policy: str):
        started = self.ctx.Event()
        acquired = self.ctx.Event()
        results = self.ctx.Queue()
        process = self.ctx.Process(
            target=_run_child,
            args=(self.root, name, namespace, policy, started, acquired, results),
        )
        process.start()
        self.addCleanup(self._join, process)
        return process, started, acquired, results

    def _join(self, process) -> None:
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(5)

    def test_same_namespace_exclusion_fail_fast(self) -> None:
        holder = LockCapability.acquire(
            self.ops,
            self.directory,
            "build.lock",
            namespace="scope-a",
            policy=LockPolicy.BLOCK,
        )
        self.addCleanup(holder.close)
        process, started, acquired, results = self._spawn("build.lock", "scope-a", "fail-fast")
        self.assertTrue(started.wait(10))
        self.assertEqual(results.get(timeout=10), "contention")
        self.assertFalse(acquired.is_set())
        process.join(10)

    def test_same_namespace_block_waits_for_release(self) -> None:
        holder = LockCapability.acquire(
            self.ops,
            self.directory,
            "build.lock",
            namespace="scope-a",
            policy=LockPolicy.BLOCK,
        )
        process, started, acquired, results = self._spawn("build.lock", "scope-a", "block")
        self.assertTrue(started.wait(10))
        # The child must remain blocked while the parent holds the lock.
        self.assertFalse(acquired.wait(0.5))
        holder.close()
        self.assertTrue(acquired.wait(10))
        self.assertEqual(results.get(timeout=10), "acquired")
        process.join(10)

    def test_different_namespace_concurrency(self) -> None:
        holder = LockCapability.acquire(
            self.ops,
            self.directory,
            "a.lock",
            namespace="scope-a",
            policy=LockPolicy.BLOCK,
        )
        self.addCleanup(holder.close)
        process, started, acquired, results = self._spawn("b.lock", "scope-b", "block")
        self.assertTrue(started.wait(10))
        self.assertTrue(acquired.wait(10))
        self.assertEqual(results.get(timeout=10), "acquired")
        self.assertFalse(holder.closed)
        process.join(10)


if __name__ == "__main__":
    unittest.main()
