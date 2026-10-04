"""Phase 7 task 7.1 — runtime artifact lock on the shared BLOCK capability.

The runtime artifact cache coordinates content-addressed downloads per SRI
digest identity.  Phase 7 replaces the hand-rolled per-identity ``flock``
loop with the shared ``LockCapability`` acquired under an explicit
``BLOCK`` policy while preserving:

* the identity-derived namespace (one private lock entry per SRI string),
* independent concurrency for different identities,
* the existing containment diagnostics for an unsafe lock entry,
* release/error parity (release is unconditional and a failed release is
  never retried or silently swallowed).

These tests pin the migrated behavior.  The lock entry name remains the
identity-derived private name so the on-disk namespace is unchanged.
"""
from __future__ import annotations

import base64
import errno
import os
import stat
import tempfile
import threading
import unittest

from docker.versioning.artifact_cache import (
    FileIdentityLock,
    FileIdentityLockFactory,
)
from tests.transactions_test_support import InjectedOps

_SHA512 = "sha512-" + "A" * 86 + "=="
_OTHER = "sha512-" + "B" * 86 + "=="


def _lock_name(identity: str) -> str:
    return base64.urlsafe_b64encode(identity.encode()).decode().rstrip("=") + ".lock"


class _RuntimeLockCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "locks")
        os.makedirs(self.root, mode=0o700)

    def _lock_path(self, identity: str) -> str:
        return os.path.join(self.root, _lock_name(identity))


class DigestScopeTests(_RuntimeLockCase):
    def test_identity_selects_a_distinct_private_lock_entry(self) -> None:
        lock = FileIdentityLock(self.root)
        self.addCleanup(lambda: lock.release(_SHA512))
        self.assertTrue(lock.acquire(_SHA512))
        self.assertTrue(os.path.isfile(self._lock_path(_SHA512)))
        self.assertFalse(os.path.exists(self._lock_path(_OTHER)))

    def test_lock_entry_is_owner_private_after_acquisition(self) -> None:
        lock = FileIdentityLock(self.root)
        self.addCleanup(lambda: lock.release(_SHA512))
        lock.acquire(_SHA512)
        info = os.lstat(self._lock_path(_SHA512))
        self.assertTrue(stat.S_ISREG(info.st_mode))
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
        self.assertEqual(info.st_uid, os.geteuid())
        self.assertEqual(info.st_nlink, 1)

    def test_wrong_mode_is_repaired_after_exclusive_acquisition(self) -> None:
        path = self._lock_path(_SHA512)
        with open(path, "wb"):
            pass
        os.chmod(path, 0o644)
        lock = FileIdentityLock(self.root)
        self.addCleanup(lambda: lock.release(_SHA512))
        lock.acquire(_SHA512)
        self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o600)


class ConcurrencyScopeTests(_RuntimeLockCase):
    def test_different_identities_are_independently_concurrent(self) -> None:
        first = FileIdentityLock(self.root)
        second = FileIdentityLock(self.root)
        self.addCleanup(lambda: first.release(_SHA512))
        self.addCleanup(lambda: second.release(_OTHER))
        self.assertTrue(first.acquire(_SHA512))
        self.assertTrue(second.acquire(_OTHER))

    def test_same_identity_blocks_until_release(self) -> None:
        holder = FileIdentityLock(self.root)
        self.assertTrue(holder.acquire(_SHA512))

        contender = FileIdentityLock(self.root)
        acquired = threading.Event()
        released = threading.Event()

        def _contend() -> None:
            contender.acquire(_SHA512)
            acquired.set()
            contender.release(_SHA512)
            released.set()

        thread = threading.Thread(target=_contend, daemon=True)
        thread.start()
        try:
            # The contender must not enter while the holder owns the identity.
            self.assertFalse(acquired.wait(0.3), "same-identity lock did not block")
            holder.release(_SHA512)
            self.assertTrue(acquired.wait(5.0), "contender never acquired after release")
            self.assertTrue(released.wait(5.0))
        finally:
            holder.release(_SHA512)
            thread.join(timeout=5.0)


class UnsafeLockEntryTests(_RuntimeLockCase):
    def test_symlinked_lock_entry_is_rejected_without_mutation(self) -> None:
        target = os.path.join(self.tmp.name, "outside")
        with open(target, "wb") as stream:
            stream.write(b"unchanged")
        path = self._lock_path(_SHA512)
        os.symlink(target, path)
        lock = FileIdentityLock(self.root)
        from docker.versioning.artifact_cache import ArtifactMaterializationError

        with self.assertRaises(ArtifactMaterializationError) as ctx:
            lock.acquire(_SHA512)
        self.assertEqual(ctx.exception.reason, "containment")
        self.assertTrue(os.path.islink(path))
        with open(target, "rb") as stream:
            self.assertEqual(stream.read(), b"unchanged")

    def test_non_regular_lock_entry_is_rejected_without_mutation(self) -> None:
        path = self._lock_path(_SHA512)
        os.mkdir(path, 0o700)
        lock = FileIdentityLock(self.root)
        from docker.versioning.artifact_cache import ArtifactMaterializationError

        with self.assertRaises(ArtifactMaterializationError) as ctx:
            lock.acquire(_SHA512)
        self.assertEqual(ctx.exception.reason, "containment")
        self.assertTrue(os.path.isdir(path))

    def test_multiply_linked_lock_entry_is_rejected(self) -> None:
        path = self._lock_path(_SHA512)
        with open(path, "wb"):
            pass
        os.link(path, path + ".alias")
        lock = FileIdentityLock(self.root)
        from docker.versioning.artifact_cache import ArtifactMaterializationError

        with self.assertRaises(ArtifactMaterializationError) as ctx:
            lock.acquire(_SHA512)
        self.assertEqual(ctx.exception.reason, "containment")
        self.assertEqual(os.lstat(path).st_nlink, 2)

    def test_symlinked_lock_directory_is_rejected(self) -> None:
        outside = os.path.join(self.tmp.name, "outside-dir")
        os.makedirs(outside, mode=0o700)
        os.rmdir(self.root)
        os.symlink(outside, self.root)
        lock = FileIdentityLock(self.root)
        from docker.versioning.artifact_cache import ArtifactMaterializationError

        with self.assertRaises(ArtifactMaterializationError) as ctx:
            lock.acquire(_SHA512)
        self.assertEqual(ctx.exception.reason, "containment")
        self.assertEqual(os.listdir(outside), [])


class OperationalLockFailureTests(_RuntimeLockCase):
    """Operational descriptor-stat failures keep their raw ``OSError``.

    A failure while statting the lock-file descriptor is an I/O failure, not
    an unsafe-entry containment rejection: the runtime lock must re-raise the
    exact ``OSError`` (carrying any cleanup diagnostics) rather than report a
    containment error, while still releasing the acquired lock and directory.
    """

    def _install_ops(self, lock: FileIdentityLock, ops: InjectedOps) -> None:
        # ``FileIdentityLock`` owns its L0 backend; replace it with an
        # injecting backend that fails only when the lock-file descriptor is
        # statted (the directory descriptor still validates normally).
        lock._ops = ops

    def _fail_regular_stat(self, ops: InjectedOps, error: OSError) -> None:
        def hook(fd: int) -> None:
            if stat.S_ISREG(os.fstat(fd).st_mode):
                raise error

        ops.hooks["fstat"] = hook

    def test_lock_entry_stat_failure_reraises_raw_oserror(self) -> None:
        error = OSError(errno.EIO, "injected lock-entry stat failure")
        lock = FileIdentityLock(self.root)
        ops = InjectedOps()
        self._fail_regular_stat(ops, error)
        self._install_ops(lock, ops)

        with self.assertRaises(OSError) as ctx:
            lock.acquire(_SHA512)

        # The exact operational failure is raised, not a containment error.
        self.assertIs(ctx.exception, error)
        # A failed acquisition retains neither the lock nor the directory.
        self.assertIsNone(lock._capability)
        self.assertIsNone(lock._directory)
        # Both the lock-file descriptor and the directory descriptor are
        # released, so a fresh acquisition immediately takes the lock again.
        self.assertGreaterEqual(ops.counts["close"], 2)
        again = FileIdentityLock(self.root)
        self.addCleanup(lambda: again.release(_SHA512))
        self.assertTrue(again.acquire(_SHA512))

    def test_lock_entry_stat_failure_preserves_cleanup_secondary(self) -> None:
        stat_error = OSError(errno.EIO, "injected lock-entry stat failure")
        close_error = OSError(errno.EIO, "injected directory close failure")

        def close_spec(count: int) -> OSError | None:
            # The first close releases the lock-file descriptor; fail the
            # subsequent directory close instead.
            return close_error if count == 2 else None

        lock = FileIdentityLock(self.root)
        ops = InjectedOps()
        self._fail_regular_stat(ops, stat_error)
        ops.failures["close"] = close_spec
        self._install_ops(lock, ops)

        with self.assertRaises(OSError) as ctx:
            lock.acquire(_SHA512)

        # The operational stat failure stays primary; the failed directory
        # cleanup is attached as a secondary diagnostic instead of replacing
        # or masking it.
        self.assertIs(ctx.exception, stat_error)
        self.assertEqual(
            getattr(ctx.exception, "_transaction_secondary", []), [close_error],
        )

    def test_adoption_stat_failure_preserves_primary_and_closes_raw_descriptor(
        self,
    ) -> None:
        # When adoption fails the directory descriptor is still caller-owned;
        # its cleanup must not replace the primary stat failure.
        stat_error = OSError(errno.EIO, "injected directory stat failure")
        close_error = OSError(errno.EIO, "injected directory close failure")

        def close_spec(count: int) -> OSError | None:
            return close_error if count == 1 else None

        lock = FileIdentityLock(self.root)
        ops = InjectedOps()
        ops.failures["fstat"] = stat_error
        ops.failures["close"] = close_spec
        lock._ops = ops

        with self.assertRaises(OSError) as ctx:
            lock.acquire(_SHA512)

        # The adoption stat failure stays primary; the raw-descriptor cleanup
        # failure is attached as a secondary diagnostic instead of replacing
        # or masking the primary failure.
        self.assertIs(ctx.exception, stat_error)
        self.assertEqual(
            getattr(ctx.exception, "_transaction_secondary", []), [close_error],
        )
        # No capability was adopted, so the raw descriptor is closed exactly
        # once and no lock is retained.
        self.assertEqual(ops.counts["close"], 1)
        self.assertIsNone(lock._capability)
        self.assertIsNone(lock._directory)

    def test_validate_stage_failure_is_not_unwrapped(self) -> None:
        # An identity-probe failure can indicate an unsafe namespace change, so
        # a validate-stage failure must stay a containment error even when it
        # carries an ``OSError`` cause -- it must not be blindly unwrapped.
        from docker.transactions.errors import STAGE_LOCK_VALIDATE, LockError
        from docker.versioning.artifact_cache import (
            ArtifactMaterializationError,
            _identity_lock_failure,
        )

        probe_error = OSError(errno.EIO, "injected identity probe failure")
        exc = LockError(
            STAGE_LOCK_VALIDATE,
            "lock 'x' changed while acquiring",
            cause=probe_error,
        )
        mapped = _identity_lock_failure(exc)
        self.assertIsInstance(mapped, ArtifactMaterializationError)
        self.assertEqual(mapped.reason, "containment")


class ReleaseParityTests(_RuntimeLockCase):
    def test_release_is_idempotent_and_allows_reacquisition(self) -> None:
        lock = FileIdentityLock(self.root)
        self.assertTrue(lock.acquire(_SHA512))
        lock.release(_SHA512)
        lock.release(_SHA512)  # idempotent no-op
        again = FileIdentityLock(self.root)
        self.addCleanup(lambda: again.release(_SHA512))
        self.assertTrue(again.acquire(_SHA512))

    def test_release_without_acquire_is_a_noop(self) -> None:
        lock = FileIdentityLock(self.root)
        lock.release(_SHA512)

    def test_factory_returns_a_fresh_identity_lock(self) -> None:
        factory = FileIdentityLockFactory(self.root)
        first = factory(_SHA512)
        second = factory(_SHA512)
        self.assertIsInstance(first, FileIdentityLock)
        self.assertIsNot(first, second)


class ValidHitFastPathTests(_RuntimeLockCase):
    def test_cache_hit_never_creates_the_lock_entry(self) -> None:
        # A valid cache hit must not touch the identity lock namespace at all.
        from docker.versioning.artifact_cache import _try_cache_hit

        from tests.test_constructor_materialization import _make_integrity_for

        data = b"already-verified"
        integrity = _make_integrity_for(data)
        blob_dir = os.path.join(self.tmp.name, "cache", "sha512")
        os.makedirs(blob_dir, mode=0o700)
        blob = os.path.join(blob_dir, "blob.tgz")
        with open(blob, "wb") as stream:
            stream.write(data)
        os.chmod(blob, 0o444)
        from docker.versioning.artifact_cache import LocalCacheFilesystem

        hit = _try_cache_hit(
            integrity, "sha512", blob, LocalCacheFilesystem(),
            os.path.join(self.tmp.name, "cache"),
        )
        self.assertIsNotNone(hit)
        self.assertEqual(os.listdir(self.root), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
