"""Phase 8 tasks 8.1-8.2 — npm input-identity lock on the shared BLOCK capability.

The npm environment assembler coordinates one input identity across
non-authoritative lookup, assembly, validation, and publication.  Phase 8
replaces the hand-rolled ``prepare_identity_lock`` + ``fcntl.flock`` pair with
the shared L2 ``LockCapability`` acquired under an explicit ``BLOCK`` policy
over a descriptor-validated lock directory.

These tests pin the migrated contract:

* input-identity scope (one private ``<digest>.lock`` entry per identity),
* same-identity blocking across the whole protected scope,
* independent concurrency for different identities,
* post-lock lookup/publication ordering,
* release parity (unconditional release, primary failure preserved),
* the shared entry-safety checks (symlink, non-regular, foreign-owned,
  hard-linked, wrong-mode, bootstrap race, prepare/reopen replacement) mapped
  to the npm ``unsafe_lock_path`` containment diagnostic without mutating the
  target or any ancestor.

The lock exposes an injectable L0 ``ops`` seam so these contracts are
verified through the shared substrate rather than a private ``flock`` loop.
"""
from __future__ import annotations

import errno
import fcntl
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path

from docker.npm_environment.publication import identity_coordination_lock
from docker.npm_environment.storage import prepare_assembler_namespace
from docker.npm_environment.errors import LockedNpmError
from docker.transactions import locking
from tests.transactions_test_support import InjectedOps

_ASSEMBLER = "a" * 64
_INPUT = "b" * 64
_OTHER = "c" * 64


class _NpmLockCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="npm-env-phase8-lock-")
        self.addCleanup(self.tmp.cleanup)
        self.cache_root = Path(self.tmp.name) / "cache"
        self.cache_root.mkdir()
        self.namespace = prepare_assembler_namespace(self.cache_root, _ASSEMBLER)

    def _lock_path(self, identity: str) -> Path:
        return self.namespace.locks / f"{identity}.lock"


class InputIdentityScopeTests(_NpmLockCase):
    def test_input_identity_selects_a_private_lock_entry(self) -> None:
        with identity_coordination_lock(self.namespace, _INPUT):
            path = self._lock_path(_INPUT)
            self.assertTrue(path.is_file())
            info = os.lstat(path)
            self.assertTrue(stat.S_ISREG(info.st_mode))
            self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
            self.assertEqual(info.st_uid, os.geteuid())
            self.assertEqual(info.st_nlink, 1)
            self.assertFalse(self._lock_path(_OTHER).exists())

    def test_foreign_owned_lock_entry_is_rejected(self) -> None:
        path = self._lock_path(_INPUT)
        path.write_text("owned")
        foreign_uid = os.geteuid() + 1

        ops = InjectedOps()

        def override(info: os.stat_result) -> os.stat_result:
            if not stat.S_ISREG(info.st_mode):
                return info
            fields = list(info)
            fields[4] = foreign_uid
            return os.stat_result(tuple(fields))

        ops.fstat_override = override
        with self.assertRaises(LockedNpmError) as ctx:
            with identity_coordination_lock(self.namespace, _INPUT, ops=ops):
                pass
        self.assertEqual(ctx.exception.reason, "unsafe_lock_path")
        # A foreign-owned entry must never be truncated or rewritten.
        self.assertEqual(path.read_text(), "owned")


class ConcurrencyScopeTests(_NpmLockCase):
    def test_different_identities_are_independently_concurrent(self) -> None:
        with identity_coordination_lock(self.namespace, _INPUT):
            with identity_coordination_lock(self.namespace, _OTHER):
                self.assertTrue(self._lock_path(_INPUT).exists())
                self.assertTrue(self._lock_path(_OTHER).exists())

    def test_same_identity_blocks_until_release(self) -> None:
        entered = threading.Event()
        released = threading.Event()

        def holder() -> None:
            with identity_coordination_lock(self.namespace, _INPUT):
                entered.set()
                released.wait(timeout=5.0)

        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        try:
            self.assertTrue(entered.wait(5.0))
            acquired = threading.Event()

            def contender() -> None:
                with identity_coordination_lock(self.namespace, _INPUT):
                    acquired.set()

            other = threading.Thread(target=contender, daemon=True)
            other.start()
            # The same identity must stay blocked while the holder owns it.
            self.assertFalse(acquired.wait(0.3), "same-identity lock did not block")
            released.set()
            self.assertTrue(acquired.wait(5.0), "contender never acquired")
            other.join(timeout=5.0)
        finally:
            released.set()
            thread.join(timeout=5.0)

    def test_release_is_unconditional_and_reacquirable(self) -> None:
        with identity_coordination_lock(self.namespace, _INPUT):
            pass
        # A second acquisition immediately takes the lock again.
        with identity_coordination_lock(self.namespace, _INPUT):
            self.assertTrue(self._lock_path(_INPUT).exists())

    def test_primary_failure_is_preserved_and_lock_released(self) -> None:
        marker = RuntimeError("assembly failed")

        with self.assertRaises(RuntimeError) as ctx:
            with identity_coordination_lock(self.namespace, _INPUT):
                raise marker
        self.assertIs(ctx.exception, marker)
        # The lock is released despite the failure.
        with identity_coordination_lock(self.namespace, _INPUT):
            pass


class PostLockOrderingTests(_NpmLockCase):
    def test_lookup_and_publication_happen_inside_the_lock(self) -> None:
        # ``assemble_environment`` acquires the coordination lock before the
        # non-authoritative lookup and retains it through publication.
        import inspect

        from docker.npm_environment import publication

        src = inspect.getsource(publication.assemble_environment)
        lock = src.index("identity_coordination_lock")
        lookup = src.index("find_cached_result")
        assemble_call = src.index("assemble(")
        publish = src.index("publish_environment")
        self.assertLess(lock, lookup)
        self.assertLess(lookup, assemble_call)
        self.assertLess(assemble_call, publish)


class LockEntrySafetyTests(_NpmLockCase):
    def _assert_unsafe(self, path: Path) -> LockedNpmError:
        with self.assertRaises(LockedNpmError) as ctx:
            with identity_coordination_lock(self.namespace, _INPUT):
                pass
        self.assertEqual(ctx.exception.reason, "unsafe_lock_path")
        return ctx.exception

    def test_symlinked_lock_entry_is_rejected_without_mutation(self) -> None:
        target = Path(self.tmp.name) / "target-file"
        target.write_text("unchanged")
        path = self._lock_path(_INPUT)
        path.symlink_to(target)
        self._assert_unsafe(path)
        self.assertTrue(path.is_symlink())
        self.assertEqual(target.read_text(), "unchanged")

    def test_non_regular_lock_entry_is_rejected_without_mutation(self) -> None:
        path = self._lock_path(_INPUT)
        path.mkdir()
        self._assert_unsafe(path)
        self.assertTrue(path.is_dir())

    def test_hard_linked_lock_entry_is_rejected_without_mutation(self) -> None:
        path = self._lock_path(_INPUT)
        path.write_text("linked")
        os.link(path, path.with_name(path.name + ".alias"))
        self._assert_unsafe(path)
        self.assertEqual(os.lstat(path).st_nlink, 2)
        self.assertEqual(path.read_text(), "linked")

    def test_wrong_mode_is_repaired_after_exclusive_acquisition(self) -> None:
        path = self._lock_path(_INPUT)
        path.write_text("")
        os.chmod(path, 0o644)
        with identity_coordination_lock(self.namespace, _INPUT):
            self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o600)

    def test_replaced_entry_is_rejected(self) -> None:
        # Replacing the entry between the open and the exclusive acquisition
        # is rejected rather than adopting an unrelated inode.
        path = self._lock_path(_INPUT)
        ops = InjectedOps()
        state = {"done": False}

        def hook(fd: int, operation: int) -> None:
            if state["done"]:
                return
            state["done"] = True
            os.unlink(str(path))
            os.close(os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))

        ops.hooks["flock"] = hook
        with self.assertRaises(LockedNpmError) as ctx:
            with identity_coordination_lock(self.namespace, _INPUT, ops=ops):
                pass
        self.assertEqual(ctx.exception.reason, "unsafe_lock_path")

    def test_bootstrap_race_acquires_the_winner_inode(self) -> None:
        # Two assemblers race to create a missing lock entry.  The loser's
        # O_EXCL create fails with EEXIST and it must lock the winner's inode
        # rather than adopting or deleting it.
        path = self._lock_path(_INPUT)
        ops = InjectedOps()
        state = {"raced": False}
        original_name = path.name

        def hook(dir_fd, name, flags, mode=0o777):
            if (
                name == original_name
                and flags & os.O_EXCL
                and not state["raced"]
            ):
                state["raced"] = True
                winner = os.open(
                    str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
                os.close(winner)
                raise FileExistsError(errno.EEXIST, "simulated bootstrap race", name)

        ops.hooks["openat"] = hook
        with identity_coordination_lock(self.namespace, _INPUT, ops=ops):
            self.assertTrue(path.is_file())
        # The winner inode is never removed by the loser.
        self.assertTrue(path.is_file())

    def test_minimal_unlink_happens_after_release(self) -> None:
        # Release unlocks but never unlinks the entry: a later acquisition
        # reuses the same inode.
        with identity_coordination_lock(self.namespace, _INPUT):
            first = os.lstat(self._lock_path(_INPUT)).st_ino
        self.assertTrue(self._lock_path(_INPUT).exists())
        with identity_coordination_lock(self.namespace, _INPUT):
            second = os.lstat(self._lock_path(_INPUT)).st_ino
        self.assertEqual(first, second)


class OperationalFailureParityTests(_NpmLockCase):
    def test_operational_acquisition_failure_keeps_raw_oserror(self) -> None:
        error = OSError(errno.EIO, "injected acquisition failure")
        ops = InjectedOps()
        ops.failures["flock"] = error
        with self.assertRaises(OSError) as ctx:
            with identity_coordination_lock(self.namespace, _INPUT, ops=ops):
                pass
        self.assertIs(ctx.exception, error)

    def test_operational_failure_releases_the_lock_and_directory(self) -> None:
        error = OSError(errno.EIO, "injected acquisition failure")
        ops = InjectedOps()
        ops.failures["flock"] = error
        with self.assertRaises(OSError):
            with identity_coordination_lock(self.namespace, _INPUT, ops=ops):
                pass
        # A fresh acquisition with no injected fault immediately succeeds.
        with identity_coordination_lock(self.namespace, _INPUT):
            pass

    def test_probe_close_failure_keeps_raw_oserror(self) -> None:
        # A valid existing lock entry is required so acquisition reaches the
        # post-acquisition identity probe whose close is faulted.
        with identity_coordination_lock(self.namespace, _INPUT):
            pass

        error = OSError(errno.EIO, "injected probe close failure")
        ops = InjectedOps()
        probe: dict[str, int | None] = {"fd": None}
        original_openat = ops.openat

        def tracking_openat(dir_fd, name, flags, mode=0o777):
            fd = original_openat(dir_fd, name, flags, mode)
            if flags == locking._PROBE_FLAGS:
                probe["fd"] = fd
            return fd

        ops.openat = tracking_openat

        last: dict[str, int | None] = {"fd": None}

        def close_hook(fd: int) -> None:
            last["fd"] = fd

        def close_failure(count: int):
            if last["fd"] is not None and last["fd"] == probe["fd"]:
                return error
            return None

        ops.hooks["close"] = close_hook
        ops.failures["close"] = close_failure

        with self.assertRaises(OSError) as ctx:
            with identity_coordination_lock(self.namespace, _INPUT, ops=ops):
                pass
        # A failed lock-probe close is operational I/O: the raw OSError is
        # preserved rather than being reported as unsafe containment.
        self.assertIs(ctx.exception, error)
        self.assertNotIsInstance(ctx.exception, LockedNpmError)
        self.assertEqual(ctx.exception.errno, errno.EIO)
        self.assertIsNotNone(probe["fd"])
        # The lock descriptor is still unlocked and closed, and the directory
        # capability is still released, despite the probe-close failure.
        unlocked = [args[1] for name, args in ops.calls if name == "flock"]
        self.assertIn(fcntl.LOCK_UN, unlocked)
        self.assertGreaterEqual(ops.counts.get("close", 0), 2)
        # A fresh acquisition with no injected fault immediately succeeds.
        with identity_coordination_lock(self.namespace, _INPUT):
            pass


class ReleaseInterruptionTests(_NpmLockCase):
    """Release must not leak the directory when the lock release is interrupted.

    ``LockCapability.close`` already attempts its own unlock and descriptor
    close exactly once and preserves a process-control interruption.  The npm
    release wrapper must nevertheless always release the directory capability
    afterwards, re-raising the original interruption unchanged and attaching
    any ordinary directory-close failure as a secondary diagnostic.
    """

    @staticmethod
    def _close_fd(fd: int | None) -> None:
        if fd is None:
            return
        try:
            os.close(fd)
        except OSError:
            pass

    def _existing_entry(self) -> None:
        # A pre-existing valid entry lets acquisition reach the release path
        # without re-creating the entry under the injected backend.
        with identity_coordination_lock(self.namespace, _INPUT):
            pass

    def _tracking_ops(self):
        """Return an ``InjectedOps`` that records directory and lock fds.

        ``directory`` is updated on every ``O_DIRECTORY`` open so its final
        value is the leaf descriptor retained by the directory capability;
        ``lock`` records the descriptor acquired for the identity entry;
        ``flock_op`` records the most recent ``flock`` operation so a failure
        spec can distinguish acquisition from unlock.  Faults are installed
        only after entry (and after ``ops.reset()``) so descriptor-number
        reuse during the secure walk cannot confuse the checks.
        """
        ops = InjectedOps()
        directory: dict[str, int | None] = {"fd": None}
        lock: dict[str, int | None] = {"fd": None}
        flock_op: dict[str, int | None] = {"op": None}
        original_openat = ops.openat

        def tracking_openat(dir_fd, name, flags, mode=0o777):
            fd = original_openat(dir_fd, name, flags, mode)
            if flags & getattr(os, "O_DIRECTORY", 0):
                directory["fd"] = fd
            return fd

        def flock_hook(fd: int, operation: int) -> None:
            flock_op["op"] = operation
            if operation == fcntl.LOCK_EX:
                lock["fd"] = fd

        ops.openat = tracking_openat
        ops.hooks["flock"] = flock_hook
        return ops, directory, lock, flock_op

    def _directory_close_count(self, ops: InjectedOps, fd: int) -> int:
        return sum(1 for name, args in ops.calls if name == "close" and args[0] == fd)

    def test_interrupted_unlock_still_closes_directory(self) -> None:
        self._existing_entry()
        interruption = KeyboardInterrupt("injected unlock interruption")
        ops, directory, lock, flock_op = self._tracking_ops()

        def unlock_failure(count: int):
            return interruption if flock_op["op"] == fcntl.LOCK_UN else None

        with self.assertRaises(KeyboardInterrupt) as ctx:
            with identity_coordination_lock(self.namespace, _INPUT, ops=ops):
                ops.reset()
                ops.failures["flock"] = unlock_failure
        self.assertIs(ctx.exception, interruption)
        self.assertIsNotNone(directory["fd"])
        # The directory is released exactly once despite the interruption, and
        # the interrupted lock descriptor is still really closed once.
        self.assertEqual(self._directory_close_count(ops, directory["fd"]), 1)
        self.assertEqual(self._directory_close_count(ops, lock["fd"]), 1)
        with identity_coordination_lock(self.namespace, _INPUT):
            pass

    def test_interrupted_unlock_with_primary_error_still_closes_directory(self) -> None:
        self._existing_entry()
        interruption = KeyboardInterrupt("injected unlock interruption")
        marker = RuntimeError("assembly failed")
        ops, directory, lock, flock_op = self._tracking_ops()

        def unlock_failure(count: int):
            return interruption if flock_op["op"] == fcntl.LOCK_UN else None

        with self.assertRaises(KeyboardInterrupt) as ctx:
            with identity_coordination_lock(self.namespace, _INPUT, ops=ops):
                ops.reset()
                ops.failures["flock"] = unlock_failure
                raise marker
        # The release interruption is preserved unchanged and still releases
        # the directory exactly once.
        self.assertIs(ctx.exception, interruption)
        self.assertEqual(self._directory_close_count(ops, directory["fd"]), 1)

    def test_interrupted_lock_close_still_closes_directory(self) -> None:
        self._existing_entry()
        interruption = KeyboardInterrupt("injected lock close interruption")
        ops, directory, lock, flock_op = self._tracking_ops()
        self.addCleanup(lambda: self._close_fd(lock["fd"]))
        last_close: dict[str, int | None] = {"fd": None}

        def close_hook(fd: int) -> None:
            last_close["fd"] = fd

        def close_failure(count: int):
            if last_close["fd"] is not None and last_close["fd"] == lock["fd"]:
                return interruption
            return None

        ops.hooks["close"] = close_hook
        with self.assertRaises(KeyboardInterrupt) as ctx:
            with identity_coordination_lock(self.namespace, _INPUT, ops=ops):
                ops.reset()
                ops.failures["close"] = close_failure
        self.assertIs(ctx.exception, interruption)
        self.assertIsNotNone(directory["fd"])
        self.assertEqual(self._directory_close_count(ops, directory["fd"]), 1)

    def test_directory_close_failure_is_secondary_to_interruption(self) -> None:
        self._existing_entry()
        interruption = KeyboardInterrupt("injected unlock interruption")
        directory_error = OSError(errno.EIO, "injected directory close")
        ops, directory, lock, flock_op = self._tracking_ops()
        self.addCleanup(lambda: self._close_fd(directory["fd"]))
        last_close: dict[str, int | None] = {"fd": None}

        def unlock_failure(count: int):
            return interruption if flock_op["op"] == fcntl.LOCK_UN else None

        def close_hook(fd: int) -> None:
            last_close["fd"] = fd

        def close_failure(count: int):
            if last_close["fd"] is not None and last_close["fd"] == directory["fd"]:
                return directory_error
            return None

        ops.hooks["close"] = close_hook
        with self.assertRaises(KeyboardInterrupt) as ctx:
            with identity_coordination_lock(self.namespace, _INPUT, ops=ops):
                ops.reset()
                ops.failures["flock"] = unlock_failure
                ops.failures["close"] = close_failure
        self.assertIs(ctx.exception, interruption)
        self.assertEqual(self._directory_close_count(ops, directory["fd"]), 1)
        # The ordinary directory-close failure is secondary, never a
        # replacement for the preserved interruption.
        self.assertEqual(
            getattr(interruption, "_transaction_secondary", None), [directory_error]
        )

    def test_directory_close_failure_is_secondary_to_primary_error(self) -> None:
        self._existing_entry()
        marker = RuntimeError("assembly failed")
        directory_error = OSError(errno.EIO, "injected directory close")
        ops, directory, lock, flock_op = self._tracking_ops()
        self.addCleanup(lambda: self._close_fd(directory["fd"]))
        last_close: dict[str, int | None] = {"fd": None}

        def close_hook(fd: int) -> None:
            last_close["fd"] = fd

        def close_failure(count: int):
            if last_close["fd"] is not None and last_close["fd"] == directory["fd"]:
                return directory_error
            return None

        ops.hooks["close"] = close_hook
        with self.assertRaises(RuntimeError) as ctx:
            with identity_coordination_lock(self.namespace, _INPUT, ops=ops):
                ops.reset()
                ops.failures["close"] = close_failure
                raise marker
        self.assertIs(ctx.exception, marker)
        self.assertEqual(self._directory_close_count(ops, directory["fd"]), 1)
        self.assertEqual(getattr(marker, "_transaction_secondary", None), [directory_error])


if __name__ == "__main__":
    unittest.main()
