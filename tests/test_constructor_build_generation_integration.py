"""Phase 5 integration contracts for the generation-based build cache.

These tests pin the migrated consumer behavior required by Phase 5:

* the checkout build lock is the shared fail-fast capability, rejects a
  competing build before any mutation, stays bound to the canonical project
  and cache namespace, and is released on every outcome (task 5.1);
* markers are retained before generation commit, removed for admitted blobs,
  fixed-TTL collected for uncommitted blobs, and kept independent of shared
  XDG state and the constructor project (task 5.2);
* a post-commit marker reconciliation or superseded-cleanup failure preserves
  the new generation, the predecessor, and the successfully built image, and
  surfaces as an operational build-cache error that maps to exit code 4
  (tasks 5.4-5.5).
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from docker.transactions.capabilities import CapabilityError, DirectoryCapability
from docker.transactions.errors import STAGE_CLOSE, LockError, TransactionError, UnsafeFileError
from docker.transactions.locking import LockPolicy
from docker.transactions.posix import PosixFileOps
from docker.transactions.regular import RegularFileContracts
from docker.versioning import build_cache as build_cache_module
from docker.versioning.build_cache import (
    UNCOMMITTED_TTL_SECONDS,
    BuildCacheError,
    BuildTransactionError,
    PostCommitBuildError,
    acquire_constructor_project_build_lock,
    build_blob_path,
    commit_build_set,
    maintain_uncommitted_blobs,
    mark_uncommitted_blob,
    prepare_build_cache,
    publish_uncommitted_blob,
    publish_verified_blob,
    recover_abandoned_snapshots,
    recover_build_generations,
)
from docker.versioning.build_cleanup import BuildCleanupError
from docker.versioning.digest_identity import DigestIdentity


def _generation_files(paths: Path) -> list[str]:
    return sorted(
        entry.name
        for entry in paths.iterdir()
        if entry.name.startswith("committed-build-") and entry.name.endswith(".json")
    )


def _generation_blobs(paths: Path, name: str) -> list[str]:
    return json.loads((paths / name).read_text())["blobs"]


# Raw L0 operations, captured before any test patches the class so failure
# injectors can wrap and delegate to the real implementation.
_REAL_OPENAT = PosixFileOps.openat
_REAL_FSTAT = PosixFileOps.fstat
_REAL_FCHMOD = PosixFileOps.fchmod
_REAL_FSYNC = PosixFileOps.fsync
_REAL_FLOCK = PosixFileOps.flock
_REAL_RENAMEAT = PosixFileOps.renameat
_REAL_UNLINKAT = PosixFileOps.unlinkat
_REAL_OPS_CLOSE = PosixFileOps.close


def _fd_target(fd: int) -> str:
    try:
        return os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        return ""


def _open_fd_count() -> int:
    return len(os.listdir("/proc/self/fd"))


def _failing_lock_openat(self, dir_fd, name, flags, mode=0o777):
    if name == "build.lock":
        raise OSError(errno.EIO, "injected lock open failure")
    return _REAL_OPENAT(self, dir_fd, name, flags, mode)


def _failing_lock_fstat(self, fd):
    target = _fd_target(fd)
    info = _REAL_FSTAT(self, fd)
    if os.path.basename(target) == "build.lock":
        raise OSError(errno.EIO, "injected lock stat failure")
    return info


def _failing_lock_fchmod(self, fd, mode):
    if os.path.basename(_fd_target(fd)) == "build.lock":
        raise OSError(errno.EIO, "injected lock mode-repair failure")
    return _REAL_FCHMOD(self, fd, mode)


def _foreign_lock_fstat(self, fd):
    info = _REAL_FSTAT(self, fd)
    if os.path.basename(_fd_target(fd)) == "build.lock":
        return types.SimpleNamespace(
            st_mode=info.st_mode,
            st_uid=os.geteuid() + 1,
            st_gid=info.st_gid,
            st_nlink=info.st_nlink,
        )
    return info


def _contracts_spy(records: list[tuple]):
    """Return a ``RegularFileContracts`` subclass recording L2 invocations."""

    class _Spy(RegularFileContracts):
        def durable_replace(self, directory, name, data, mode):
            records.append(("durable_replace", directory, name, bytes(data), mode))
            return super().durable_replace(directory, name, data, mode)

        def validated_read(
            self, directory, name, *, allowed_mode=None, require_single_link=True
        ):
            records.append(("validated_read", directory, name, allowed_mode))
            return super().validated_read(
                directory,
                name,
                allowed_mode=allowed_mode,
                require_single_link=require_single_link,
            )

    return _Spy


class _BuildTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.repo = self.base / "constructor"
        self.repo.mkdir()
        self.cache = self.base / "cache"
        self.cache.mkdir(mode=0o700)
        os.chmod(self.cache, 0o700)

    def identity(self, payload: bytes) -> DigestIdentity:
        return DigestIdentity.from_hex("sha256", hashlib.sha256(payload).hexdigest())

    def publish(self, payload: bytes) -> DigestIdentity:
        identity = self.identity(payload)
        publish_verified_blob(
            identity, payload, constructor_project_root=self.repo, cache_root=self.cache
        )
        return identity

    def paths(self):
        return prepare_build_cache(self.repo, cache_root=self.cache)


# ═══════════════════════════════════════════════════════════════════════
# 5.1 checkout-wide fail-fast lock parity
# ═══════════════════════════════════════════════════════════════════════


def _contender(checkout: str, start, results) -> None:
    start.wait()
    try:
        with acquire_constructor_project_build_lock(checkout):
            results.put("acquired")
            time.sleep(0.2)
    except BuildTransactionError as exc:
        results.put(str(exc))
    except BaseException as exc:  # pragma: no cover - diagnostic only
        results.put(f"unexpected: {type(exc).__name__}: {exc}")


class BuildLockParityTests(_BuildTestCase):
    def test_lock_is_the_shared_fail_fast_capability(self) -> None:
        with acquire_constructor_project_build_lock(self.repo, cache_root=self.cache) as lock:
            self.assertIs(lock.capability.policy, LockPolicy.FAIL_FAST)
            self.assertEqual(lock.capability.name, "build.lock")
            self.assertEqual(lock.capability.namespace, "build-generation")

    def test_competing_build_is_rejected_before_any_mutation(self) -> None:
        paths = self.paths()
        with acquire_constructor_project_build_lock(self.repo, cache_root=self.cache):
            with self.assertRaisesRegex(BuildTransactionError, "active build"):
                acquire_constructor_project_build_lock(self.repo, cache_root=self.cache)
            # The contender never validated, repaired, or created cache state.
            self.assertEqual(_generation_files(paths.persistent_root), [])
        with acquire_constructor_project_build_lock(self.repo, cache_root=self.cache):
            pass

    def test_competing_process_is_rejected_without_mutation(self) -> None:
        paths = self.paths()
        with acquire_constructor_project_build_lock(self.repo, cache_root=self.cache):
            command = (
                "from docker.versioning.build_cache import acquire_constructor_project_build_lock; "
                f"acquire_constructor_project_build_lock({str(self.repo)!r}, "
                f"cache_root={str(self.cache)!r})"
            )
            result = subprocess.run(
                [sys.executable, "-c", command], text=True, capture_output=True
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("active build", result.stderr)
        self.assertEqual(_generation_files(paths.persistent_root), [])

    def test_simultaneous_pristine_bootstrap_serializes(self) -> None:
        checkout = self.base / "pristine"
        checkout.mkdir()
        context = multiprocessing.get_context("fork")
        start = context.Event()
        results = context.Queue()
        workers = [
            context.Process(target=_contender, args=(str(checkout), start, results))
            for _ in range(4)
        ]
        for worker in workers:
            worker.start()
        start.set()
        for worker in workers:
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
        outcomes = [results.get(timeout=1) for _ in workers]
        self.assertIn("acquired", outcomes)
        self.assertTrue(
            all(value == "acquired" or "active build" in value for value in outcomes),
            outcomes,
        )

    def test_release_failure_preserves_typed_lock_error(self) -> None:
        lock = acquire_constructor_project_build_lock(self.repo, cache_root=self.cache)
        boom = OSError(errno.EIO, "injected release close failure")
        with mock.patch.object(PosixFileOps, "close", side_effect=boom):
            with self.assertRaises(LockError) as ctx:
                lock.release()
        # The L2 close wrapper preserves the typed lock failure; the exact
        # descriptor failure stays reachable through its stored cause.
        self.assertIs(ctx.exception.cause, boom)
        self.assertIs(ctx.exception.__cause__, boom)

    def test_body_exception_remains_primary_when_unlock_fails(self) -> None:
        primary = RuntimeError("body failed")
        unlock = OSError(errno.EIO, "injected unlock failure")
        calls = {"unlock": 0}

        def failing_unlock(self, fd, operation):
            if operation == fcntl.LOCK_UN:
                calls["unlock"] += 1
                raise unlock
            return _REAL_FLOCK(self, fd, operation)

        with mock.patch.object(PosixFileOps, "flock", failing_unlock):
            with self.assertRaises(RuntimeError) as ctx:
                with acquire_constructor_project_build_lock(
                    self.repo, cache_root=self.cache
                ):
                    raise primary
        self.assertIs(ctx.exception, primary)
        secondary = list(getattr(primary, "_transaction_secondary", []))
        typed = [exc for exc in secondary if isinstance(exc, LockError)]
        self.assertEqual(len(typed), 1)
        self.assertIs(typed[0].cause, unlock)
        self.assertEqual(1, calls["unlock"])

    def test_body_interruption_remains_primary_when_release_fails(self) -> None:
        primary = KeyboardInterrupt("body interrupted")
        unlock = OSError(errno.EIO, "injected unlock failure")

        def failing_unlock(self, fd, operation):
            if operation == fcntl.LOCK_UN:
                raise unlock
            return _REAL_FLOCK(self, fd, operation)

        with mock.patch.object(PosixFileOps, "flock", failing_unlock):
            with self.assertRaises(KeyboardInterrupt) as ctx:
                with acquire_constructor_project_build_lock(
                    self.repo, cache_root=self.cache
                ):
                    raise primary
        self.assertIs(ctx.exception, primary)
        secondary = list(getattr(primary, "_transaction_secondary", []))
        typed = [exc for exc in secondary if isinstance(exc, LockError)]
        self.assertEqual(len(typed), 1)
        self.assertIs(typed[0].cause, unlock)

    def test_simultaneous_unlock_and_close_failures_are_retained_once(self) -> None:
        lock = acquire_constructor_project_build_lock(
            self.repo, cache_root=self.cache
        )
        unlock = OSError(errno.EIO, "injected unlock failure")
        lock_close = OSError(errno.EBADF, "injected lock close failure")
        directory_close = OSError(errno.ENOSPC, "injected directory close failure")
        calls = {"unlock": 0, "lock_close": 0, "directory_close": 0}

        def failing_unlock(self, fd, operation):
            if operation == fcntl.LOCK_UN:
                calls["unlock"] += 1
                raise unlock
            return _REAL_FLOCK(self, fd, operation)

        def failing_close(self, fd):
            target = os.path.basename(_fd_target(fd))
            if target == "build.lock":
                calls["lock_close"] += 1
                raise lock_close
            calls["directory_close"] += 1
            raise directory_close

        with mock.patch.object(PosixFileOps, "flock", failing_unlock), \
                mock.patch.object(PosixFileOps, "close", failing_close):
            with self.assertRaises(LockError) as ctx:
                lock.release()
            lock.release()
        failure = ctx.exception
        # The typed lock release failure keeps the first raw cause and the
        # later unlock/close failures as secondary diagnostics.
        self.assertIs(failure.cause, unlock)
        secondaries = list(failure.secondary)
        self.assertIn(lock_close, secondaries)
        directory_secondary = next(
            item
            for item in secondaries
            if isinstance(item, TransactionError) and item.stage == STAGE_CLOSE
        )
        self.assertIs(directory_secondary.cause, directory_close)
        self.assertEqual(
            {"unlock": 1, "lock_close": 1, "directory_close": 1}, calls
        )

    def test_release_failure_without_body_exception_propagates(self) -> None:
        unlock = OSError(errno.EIO, "injected unlock failure")
        calls = {"unlock": 0}

        def failing_unlock(self, fd, operation):
            if operation == fcntl.LOCK_UN:
                calls["unlock"] += 1
                raise unlock
            return _REAL_FLOCK(self, fd, operation)

        with mock.patch.object(PosixFileOps, "flock", failing_unlock):
            with self.assertRaises(LockError) as ctx:
                with acquire_constructor_project_build_lock(
                    self.repo, cache_root=self.cache
                ):
                    pass
        self.assertIs(ctx.exception.cause, unlock)
        self.assertIs(ctx.exception.__cause__, unlock)
        self.assertEqual(1, calls["unlock"])

    def test_canonical_project_and_cache_binding(self) -> None:
        link = self.base / "project-link"
        link.symlink_to(self.repo, target_is_directory=True)
        other = self.base / "other"
        other.mkdir()
        other_cache = self.base / "other-cache"
        other_cache.mkdir(mode=0o700)
        with acquire_constructor_project_build_lock(link, cache_root=self.cache) as lock:
            self.assertEqual(lock.constructor_project_root, self.repo.resolve())
            lock.assert_held_for(self.repo, cache_root=self.cache)
            with self.assertRaises(BuildTransactionError):
                lock.assert_held_for(other, cache_root=self.cache)
            with self.assertRaises(BuildTransactionError):
                lock.assert_held_for(self.repo, cache_root=other_cache)

    def test_release_on_every_outcome(self) -> None:
        with self.assertRaises(RuntimeError):
            with acquire_constructor_project_build_lock(self.repo, cache_root=self.cache) as lock:
                raise RuntimeError("interrupted owner")
        # A released capability can be re-acquired, and release is idempotent.
        lock = acquire_constructor_project_build_lock(self.repo, cache_root=self.cache)
        lock.release()
        lock.release()
        with self.assertRaises(BuildTransactionError):
            lock.assert_held_for(self.repo, cache_root=self.cache)
        with acquire_constructor_project_build_lock(self.repo, cache_root=self.cache):
            pass


# ═══════════════════════════════════════════════════════════════════════
# 5.2 marker / TTL / snapshot / XDG parity
# ═══════════════════════════════════════════════════════════════════════


class BuildCacheParityTests(_BuildTestCase):
    def test_markers_retained_before_commit_and_removed_after(self) -> None:
        paths = self.paths()
        identity = self.identity(b"marker-lifecycle")
        marker = paths.markers_root / f"sha256:{identity.hex_digest()}.json"
        with acquire_constructor_project_build_lock(self.repo, cache_root=self.cache) as lock:
            publish_uncommitted_blob(
                identity, b"marker-lifecycle", constructor_project_root=self.repo,
                lock=lock, verified_at=0, cache_root=self.cache,
            )
            # Before generation commit the marker is the only retention evidence.
            self.assertTrue(marker.exists())
            commit_build_set(self.repo, {identity}, lock=lock, cache_root=self.cache)
        self.assertFalse(marker.exists())
        self.assertTrue(build_blob_path(paths.blobs_root, identity).exists())
        self.assertEqual(
            _generation_blobs(paths.persistent_root, _generation_files(paths.persistent_root)[0]),
            [f"sha256:{identity.hex_digest()}"],
        )

    def test_fixed_ttl_for_uncommitted_blobs(self) -> None:
        paths = self.paths()
        committed = self.publish(b"ttl-committed")
        stale = self.publish(b"ttl-stale")
        marker = paths.markers_root / f"sha256:{stale.hex_digest()}.json"
        with acquire_constructor_project_build_lock(self.repo, cache_root=self.cache) as lock:
            mark_uncommitted_blob(committed, self.repo, lock=lock, verified_at=0, cache_root=self.cache)
            mark_uncommitted_blob(stale, self.repo, lock=lock, verified_at=0, cache_root=self.cache)
            commit_build_set(self.repo, {committed}, lock=lock, cache_root=self.cache)
            mark_uncommitted_blob(stale, self.repo, lock=lock, verified_at=0, cache_root=self.cache)
            maintain_uncommitted_blobs(
                self.repo, lock=lock, now=UNCOMMITTED_TTL_SECONDS, cache_root=self.cache
            )
            self.assertTrue(build_blob_path(paths.blobs_root, stale).exists())
            maintain_uncommitted_blobs(
                self.repo, lock=lock, now=UNCOMMITTED_TTL_SECONDS + 1, cache_root=self.cache
            )
        # Committed blobs are TTL-immune; uncommitted blobs expire exactly past TTL.
        self.assertTrue(build_blob_path(paths.blobs_root, committed).exists())
        self.assertFalse(build_blob_path(paths.blobs_root, stale).exists())
        self.assertFalse(marker.exists())

    def test_snapshot_recovery_and_content_addressed_publication(self) -> None:
        paths = self.paths()
        snapshot = paths.generated_root / "abandoned"
        snapshot.mkdir()
        (snapshot / "partial").write_text("partial")
        identity = self.publish(b"content-addressed")
        with acquire_constructor_project_build_lock(self.repo, cache_root=self.cache) as lock:
            recover_build_generations(self.repo, lock=lock, cache_root=self.cache)
            recover_abandoned_snapshots(self.repo, lock=lock, cache_root=self.cache)
        self.assertFalse(snapshot.exists())
        self.assertTrue(build_blob_path(paths.blobs_root, identity).exists())

    def test_restart_fsyncs_marker_directory_when_markers_absent(self) -> None:
        paths = self.paths()
        identity = self.publish(b"marker-absent-restart")
        with acquire_constructor_project_build_lock(self.repo, cache_root=self.cache) as lock:
            mark_uncommitted_blob(identity, self.repo, lock=lock, verified_at=0, cache_root=self.cache)
            commit_build_set(self.repo, {identity}, lock=lock, cache_root=self.cache)
        marker = paths.markers_root / f"sha256:{identity.hex_digest()}.json"
        self.assertFalse(marker.exists())
        from docker.versioning import build_cleanup as build_cleanup_module

        recorded: list[str] = []
        real_sync = build_cleanup_module._sync_directory

        def recording_sync(ops, directory, failures, *, target):
            recorded.append(target)
            return real_sync(ops, directory, failures, target=target)

        with acquire_constructor_project_build_lock(self.repo, cache_root=self.cache) as lock:
            with mock.patch.object(build_cleanup_module, "_sync_directory", recording_sync):
                recover_build_generations(self.repo, lock=lock, cache_root=self.cache)
        # Recovery must synchronize the shared marker directory even though
        # every authoritative-generation marker is already absent.
        self.assertIn("markers", recorded)

    def test_shared_xdg_and_constructor_project_are_untouched(self) -> None:
        xdg_blob = self.cache / "runtime-artifacts" / "blobs" / "sentinel"
        xdg_blob.parent.mkdir(parents=True)
        xdg_blob.write_bytes(b"shared-xdg")
        identity = self.publish(b"xdg-independent")
        with acquire_constructor_project_build_lock(self.repo, cache_root=self.cache) as lock:
            mark_uncommitted_blob(identity, self.repo, lock=lock, verified_at=0, cache_root=self.cache)
            commit_build_set(self.repo, {identity}, lock=lock, cache_root=self.cache)
        self.assertEqual(xdg_blob.read_bytes(), b"shared-xdg")
        self.assertFalse((self.repo / ".docker-cache").exists())
        self.assertFalse((self.repo / "build.lock").exists())


# ═══════════════════════════════════════════════════════════════════════
# 5.4 / 5.5 post-commit failure and operational mapping
# ═══════════════════════════════════════════════════════════════════════


class PostCommitFailureTests(_BuildTestCase):
    def _stage_predecessor(self) -> tuple[DigestIdentity, object]:
        """Publish and commit generation 1 with one blob; return (blob, paths)."""
        paths = self.paths()
        old = self.publish(b"predecessor-blob")
        with acquire_constructor_project_build_lock(self.repo, cache_root=self.cache) as lock:
            mark_uncommitted_blob(old, self.repo, lock=lock, verified_at=0, cache_root=self.cache)
            commit_build_set(self.repo, {old}, lock=lock, cache_root=self.cache)
        return old, paths

    def test_marker_reconciliation_failure_preserves_generation_and_predecessor(self) -> None:
        old, paths = self._stage_predecessor()
        new = self.publish(b"successor-blob")
        real_unlinkat = PosixFileOps.unlinkat

        def failing_unlinkat(self, dir_fd, name):
            if isinstance(name, str) and name.endswith(".json") and not name.startswith(
                "committed-build-"
            ):
                raise OSError(errno.EIO, "injected marker unlink failure")
            return real_unlinkat(self, dir_fd, name)

        with acquire_constructor_project_build_lock(self.repo, cache_root=self.cache) as lock:
            publish_uncommitted_blob(
                new, b"successor-blob", constructor_project_root=self.repo,
                lock=lock, verified_at=0, cache_root=self.cache,
            )
            with mock.patch.object(PosixFileOps, "unlinkat", failing_unlinkat):
                with self.assertRaises(PostCommitBuildError) as ctx:
                    commit_build_set(self.repo, {new}, lock=lock, cache_root=self.cache)
        self.assertIsInstance(ctx.exception.__cause__, BuildCleanupError)
        self.assertTrue(ctx.exception.__cause__.failures)
        # The new generation is authoritative, the predecessor is retained as
        # cleanup evidence, and both blobs survive.
        generations = _generation_files(paths.persistent_root)
        self.assertEqual(len(generations), 2)
        self.assertEqual(
            _generation_blobs(paths.persistent_root, generations[-1]),
            [f"sha256:{new.hex_digest()}"],
        )
        self.assertEqual(
            _generation_blobs(paths.persistent_root, generations[0]),
            [f"sha256:{old.hex_digest()}"],
        )
        self.assertTrue(build_blob_path(paths.blobs_root, old).exists())
        self.assertTrue(build_blob_path(paths.blobs_root, new).exists())

    def test_superseded_cleanup_failure_preserves_predecessor(self) -> None:
        old, paths = self._stage_predecessor()
        new = self.publish(b"cleanup-successor")
        real_unlinkat = PosixFileOps.unlinkat

        def failing_unlinkat(self, dir_fd, name):
            if isinstance(name, str) and name.endswith(".blob"):
                raise OSError(errno.EIO, "injected blob unlink failure")
            return real_unlinkat(self, dir_fd, name)

        with acquire_constructor_project_build_lock(self.repo, cache_root=self.cache) as lock:
            mark_uncommitted_blob(new, self.repo, lock=lock, verified_at=0, cache_root=self.cache)
            with mock.patch.object(PosixFileOps, "unlinkat", failing_unlinkat):
                with self.assertRaises(PostCommitBuildError) as ctx:
                    commit_build_set(self.repo, {new}, lock=lock, cache_root=self.cache)
        self.assertIsInstance(ctx.exception.__cause__, BuildCleanupError)
        generations = _generation_files(paths.persistent_root)
        self.assertEqual(len(generations), 2)
        self.assertTrue(build_blob_path(paths.blobs_root, old).exists())
        self.assertTrue(build_blob_path(paths.blobs_root, new).exists())

    def test_marker_directory_fsync_failure_blocks_cleanup(self) -> None:
        old, paths = self._stage_predecessor()
        new = self.publish(b"fsync-successor")
        real_fsync = PosixFileOps.fsync
        real_unlinkat = PosixFileOps.unlinkat
        state = {"unlinked_marker": False}

        def recording_unlinkat(self, dir_fd, name):
            if isinstance(name, str) and name.endswith(".json") and not name.startswith(
                "committed-build-"
            ):
                state["unlinked_marker"] = True
            return real_unlinkat(self, dir_fd, name)

        def failing_fsync(self, fd):
            if state["unlinked_marker"]:
                raise OSError(errno.EIO, "injected marker directory fsync failure")
            return real_fsync(self, fd)

        with acquire_constructor_project_build_lock(self.repo, cache_root=self.cache) as lock:
            publish_uncommitted_blob(
                new, b"fsync-successor", constructor_project_root=self.repo,
                lock=lock, verified_at=0, cache_root=self.cache,
            )
            with mock.patch.object(PosixFileOps, "unlinkat", recording_unlinkat), mock.patch.object(
                PosixFileOps, "fsync", failing_fsync
            ):
                with self.assertRaises(PostCommitBuildError) as ctx:
                    commit_build_set(self.repo, {new}, lock=lock, cache_root=self.cache)
        self.assertIsInstance(ctx.exception.__cause__, BuildCleanupError)
        self.assertEqual(len(_generation_files(paths.persistent_root)), 2)
        self.assertTrue(build_blob_path(paths.blobs_root, old).exists())

    def test_snapshot_cleanup_failure_leaves_uncommitted_markers_and_ttl(self) -> None:
        paths = self.paths()
        identity = self.publish(b"snapshot-cleanup-failure")
        marker = paths.markers_root / f"sha256:{identity.hex_digest()}.json"
        # A snapshot-cleanup failure prevents commit_build_set, so the newly
        # materialized blob stays uncommitted under its durable marker.
        with acquire_constructor_project_build_lock(self.repo, cache_root=self.cache) as lock:
            mark_uncommitted_blob(identity, self.repo, lock=lock, verified_at=0, cache_root=self.cache)
            self.assertTrue(marker.exists())
            maintain_uncommitted_blobs(
                self.repo, lock=lock, now=UNCOMMITTED_TTL_SECONDS, cache_root=self.cache
            )
            self.assertTrue(build_blob_path(paths.blobs_root, identity).exists())
            maintain_uncommitted_blobs(
                self.repo, lock=lock, now=UNCOMMITTED_TTL_SECONDS + 1, cache_root=self.cache
            )
        self.assertFalse(marker.exists())
        self.assertFalse(build_blob_path(paths.blobs_root, identity).exists())

    def test_recovery_failure_blocks_before_any_recovery_mutation(self) -> None:
        old, paths = self._stage_predecessor()
        # A malformed second generation makes discovery fail closed.
        (paths.persistent_root / ("committed-build-" + "0" * 19 + "2.json")).write_text(
            "not-json"
        )
        with acquire_constructor_project_build_lock(self.repo, cache_root=self.cache) as lock:
            with self.assertRaises(BuildCacheError):
                recover_build_generations(self.repo, lock=lock, cache_root=self.cache)
        # Nothing was deleted or repaired.
        self.assertTrue(build_blob_path(paths.blobs_root, old).exists())
        self.assertEqual(len(_generation_files(paths.persistent_root)), 2)


class LockDiagnosticParityTests(_BuildTestCase):
    """Operational lock failures keep their raw errno; unsafe entries stay typed."""

    def _lock_path(self) -> Path:
        return self.paths().persistent_root / "build.lock"

    def test_lock_open_failure_preserves_typed_lock_error(self) -> None:
        with mock.patch.object(PosixFileOps, "openat", _failing_lock_openat):
            with self.assertRaises(LockError) as ctx:
                acquire_constructor_project_build_lock(self.repo, cache_root=self.cache)
        self.assertNotIsInstance(ctx.exception, BuildTransactionError)
        self.assertEqual(ctx.exception.cause.errno, errno.EIO)

    def test_lock_stat_failure_preserves_typed_lock_error(self) -> None:
        with mock.patch.object(PosixFileOps, "fstat", _failing_lock_fstat):
            with self.assertRaises(LockError) as ctx:
                acquire_constructor_project_build_lock(self.repo, cache_root=self.cache)
        self.assertNotIsInstance(ctx.exception, BuildTransactionError)
        self.assertEqual(ctx.exception.cause.errno, errno.EIO)

    def test_lock_mode_repair_failure_preserves_typed_lock_error(self) -> None:
        lock = self._lock_path()
        lock.write_bytes(b"")
        os.chmod(lock, 0o640)
        with mock.patch.object(PosixFileOps, "fchmod", _failing_lock_fchmod):
            with self.assertRaises(LockError) as ctx:
                acquire_constructor_project_build_lock(self.repo, cache_root=self.cache)
        self.assertNotIsInstance(ctx.exception, BuildTransactionError)
        self.assertEqual(ctx.exception.cause.errno, errno.EIO)

    def test_symlink_lock_entry_is_unsafe(self) -> None:
        target = self.base / "lock-target"
        target.write_bytes(b"")
        self._lock_path().symlink_to(target)
        with self.assertRaisesRegex(BuildTransactionError, "unsafe constructor-project build lock"):
            acquire_constructor_project_build_lock(self.repo, cache_root=self.cache)

    def test_directory_lock_entry_is_unsafe(self) -> None:
        self._lock_path().mkdir()
        with self.assertRaisesRegex(BuildTransactionError, "unsafe constructor-project build lock"):
            acquire_constructor_project_build_lock(self.repo, cache_root=self.cache)

    def test_non_regular_lock_entry_is_unsafe(self) -> None:
        os.mkfifo(self._lock_path())
        with self.assertRaisesRegex(BuildTransactionError, "unsafe constructor-project build lock"):
            acquire_constructor_project_build_lock(self.repo, cache_root=self.cache)

    def test_foreign_owned_lock_entry_is_unsafe(self) -> None:
        lock = self._lock_path()
        lock.write_bytes(b"")
        os.chmod(lock, 0o600)
        with mock.patch.object(PosixFileOps, "fstat", _foreign_lock_fstat):
            with self.assertRaisesRegex(
                BuildTransactionError, "unsafe constructor-project build lock"
            ):
                acquire_constructor_project_build_lock(self.repo, cache_root=self.cache)

    def test_multiply_linked_lock_entry_is_unsafe(self) -> None:
        lock = self._lock_path()
        lock.write_bytes(b"")
        os.chmod(lock, 0o600)
        os.link(lock, self.base / "lock-link")
        with self.assertRaisesRegex(BuildTransactionError, "unsafe constructor-project build lock"):
            acquire_constructor_project_build_lock(self.repo, cache_root=self.cache)

    def test_contention_still_reports_active_build(self) -> None:
        with acquire_constructor_project_build_lock(self.repo, cache_root=self.cache):
            with self.assertRaisesRegex(BuildTransactionError, "already has an active build"):
                acquire_constructor_project_build_lock(self.repo, cache_root=self.cache)

    def test_cleanup_failure_does_not_replace_primary(self) -> None:
        primary = OSError(errno.EIO, "injected lock open failure")
        close_error = OSError(errno.EBADF, "injected directory close failure")

        def failing_openat(self, dir_fd, name, flags, mode=0o777):
            if name == "build.lock":
                raise primary
            return _REAL_OPENAT(self, dir_fd, name, flags, mode)

        def failing_close(self, fd):
            if os.path.basename(_fd_target(fd)) == "build-artifacts":
                _REAL_OPS_CLOSE(self, fd)
                raise close_error
            return _REAL_OPS_CLOSE(self, fd)

        with mock.patch.object(PosixFileOps, "openat", failing_openat), mock.patch.object(
            PosixFileOps, "close", failing_close
        ):
            with self.assertRaises(LockError) as ctx:
                acquire_constructor_project_build_lock(self.repo, cache_root=self.cache)
        # The typed lock failure keeps the operational open failure as its
        # direct cause; the directory close failure is a secondary diagnostic.
        self.assertIs(ctx.exception.cause, primary)
        secondary = list(ctx.exception.secondary)
        self.assertEqual(len(secondary), 1)
        self.assertIsInstance(secondary[0], TransactionError)
        self.assertEqual(secondary[0].stage, STAGE_CLOSE)
        self.assertIs(secondary[0].cause, close_error)


class StorageLifecycleTests(_BuildTestCase):
    """``_BuildStorageHandle`` closes every adopted descriptor on every path."""

    def _open_lock(self):
        lock = acquire_constructor_project_build_lock(self.repo, cache_root=self.cache)
        self.addCleanup(lock.release)
        return lock

    def test_blob_directory_adoption_failure_closes_candidate_fd(self) -> None:
        lock = self._open_lock()
        before = _open_fd_count()
        boom = CapabilityError("injected blob adoption failure")
        real_from_fd = DirectoryCapability.from_fd

        def fake_from_fd(ops, fd, label):
            if label.endswith("/blobs"):
                raise boom
            return real_from_fd(ops, fd, label)

        with mock.patch.object(DirectoryCapability, "from_fd", staticmethod(fake_from_fd)):
            with self.assertRaises(CapabilityError) as ctx:
                with lock.open_storage():
                    pass
        self.assertIs(ctx.exception, boom)
        self.assertEqual(_open_fd_count(), before)

    def test_marker_directory_adoption_failure_closes_opened_blobs(self) -> None:
        lock = self._open_lock()
        before = _open_fd_count()
        boom = CapabilityError("injected marker adoption failure")
        real_from_fd = DirectoryCapability.from_fd

        def fake_from_fd(ops, fd, label):
            if label.endswith("/uncommitted"):
                raise boom
            return real_from_fd(ops, fd, label)

        with mock.patch.object(DirectoryCapability, "from_fd", staticmethod(fake_from_fd)):
            with self.assertRaises(CapabilityError) as ctx:
                with lock.open_storage():
                    pass
        self.assertIs(ctx.exception, boom)
        self.assertEqual(_open_fd_count(), before)

    def test_marker_directory_open_failure_closes_opened_blobs(self) -> None:
        lock = self._open_lock()
        before = _open_fd_count()
        boom = OSError(errno.EIO, "injected marker open failure")

        def failing_openat(self, dir_fd, name, flags, mode=0o777):
            if name == "uncommitted":
                raise boom
            return _REAL_OPENAT(self, dir_fd, name, flags, mode)

        with mock.patch.object(PosixFileOps, "openat", failing_openat):
            with self.assertRaises(OSError) as ctx:
                with lock.open_storage():
                    pass
        self.assertIs(ctx.exception, boom)
        self.assertEqual(_open_fd_count(), before)

    def test_marker_close_failure_still_closes_blobs(self) -> None:
        lock = self._open_lock()
        before = _open_fd_count()
        closed: list[str] = []

        def failing_close(self, fd):
            target = os.path.basename(_fd_target(fd))
            _REAL_OPS_CLOSE(self, fd)
            closed.append(target)
            if target == "uncommitted":
                raise OSError(errno.EIO, "injected marker close failure")
            return None

        with mock.patch.object(PosixFileOps, "close", failing_close):
            with self.assertRaises(TransactionError) as ctx:
                with lock.open_storage():
                    pass
        self.assertEqual(ctx.exception.stage, STAGE_CLOSE)
        self.assertEqual(ctx.exception.cause.errno, errno.EIO)
        self.assertEqual(closed, ["uncommitted", "blobs"])
        self.assertEqual(_open_fd_count(), before)

    def test_multiple_close_failures_preserve_primary(self) -> None:
        lock = self._open_lock()
        before = _open_fd_count()

        def failing_close(self, fd):
            target = os.path.basename(_fd_target(fd))
            _REAL_OPS_CLOSE(self, fd)
            if target in ("uncommitted", "blobs"):
                raise OSError(errno.EIO, f"injected {target} close failure")
            return None

        with mock.patch.object(PosixFileOps, "close", failing_close):
            with self.assertRaises(TransactionError) as ctx:
                with lock.open_storage():
                    pass
        primary = ctx.exception
        self.assertEqual(primary.stage, STAGE_CLOSE)
        self.assertIsInstance(primary.cause, OSError)
        self.assertEqual(primary.cause.errno, errno.EIO)
        self.assertIn("uncommitted", str(primary.cause))
        secondaries = primary.secondary
        self.assertEqual(len(secondaries), 1)
        self.assertIn("blobs", str(secondaries[0].cause))
        self.assertEqual(_open_fd_count(), before)


class MarkerL2AdapterTests(_BuildTestCase):
    """Marker reads/replacements route through the shared L2 contracts.

    Tasks 5.7/5.10: the marker control-file read and durable-replacement
    mechanics are delegated to ``RegularFileContracts`` while marker naming,
    timestamp validation, JSON interpretation, and retention decisions stay
    in the build domain.  Diagnostics, raw causes, chaining, interruption
    passthrough, and secondary cleanup behavior are pinned here.
    """

    def _marker_name(self, identity: DigestIdentity) -> str:
        return f"sha256:{identity.hex_digest()}.json"

    def _marker_path(self, identity: DigestIdentity) -> Path:
        return self.paths().markers_root / self._marker_name(identity)

    def _mark(self, lock, identity: DigestIdentity) -> None:
        mark_uncommitted_blob(
            identity, self.repo, lock=lock, verified_at=0, cache_root=self.cache
        )

    def test_replacement_routes_through_l2_durable_replace(self) -> None:
        identity = self.identity(b"l2-replace")
        records: list[tuple] = []
        with acquire_constructor_project_build_lock(self.repo, cache_root=self.cache) as lock:
            with mock.patch.object(
                build_cache_module, "RegularFileContracts", _contracts_spy(records)
            ):
                self._mark(lock, identity)
        replacements = [record for record in records if record[0] == "durable_replace"]
        self.assertEqual(len(replacements), 1)
        _, directory, name, data, mode = replacements[0]
        self.assertEqual(name, self._marker_name(identity))
        self.assertEqual(mode, 0o600)
        self.assertEqual(json.loads(data), {"verified_at": 0})
        self.assertTrue(directory.label.endswith("uncommitted"))
        self.assertTrue(self._marker_path(identity).exists())

    def test_missing_marker_publishes_successfully(self) -> None:
        identity = self.identity(b"missing-marker-publication")
        with acquire_constructor_project_build_lock(self.repo, cache_root=self.cache) as lock:
            self._mark(lock, identity)
        self.assertEqual(
            json.loads(self._marker_path(identity).read_bytes()), {"verified_at": 0}
        )
        self.assertEqual(self._marker_path(identity).stat().st_mode & 0o777, 0o600)

    def test_existing_private_marker_publishes_successfully(self) -> None:
        identity = self.identity(b"existing-private-marker")
        with acquire_constructor_project_build_lock(self.repo, cache_root=self.cache) as lock:
            self._mark(lock, identity)
            mark_uncommitted_blob(
                identity, self.repo, lock=lock, verified_at=1, cache_root=self.cache
            )
        self.assertEqual(
            json.loads(self._marker_path(identity).read_bytes()), {"verified_at": 1}
        )
        self.assertEqual(self._marker_path(identity).stat().st_mode & 0o777, 0o600)

    def test_read_routes_through_l2_validated_read(self) -> None:
        identity = self.identity(b"l2-read")
        records: list[tuple] = []
        with acquire_constructor_project_build_lock(self.repo, cache_root=self.cache) as lock:
            self._mark(lock, identity)
            with mock.patch.object(
                build_cache_module, "RegularFileContracts", _contracts_spy(records)
            ):
                maintain_uncommitted_blobs(
                    self.repo, lock=lock, now=0, cache_root=self.cache
                )
        reads = [record for record in records if record[0] == "validated_read"]
        self.assertEqual(len(reads), 1)
        _, directory, name, allowed_mode = reads[0]
        self.assertEqual(name, self._marker_name(identity))
        self.assertEqual(allowed_mode, 0o600)
        self.assertTrue(directory.label.endswith("uncommitted"))

    def test_superseded_marker_helpers_are_removed(self) -> None:
        for name in (
            "_atomic_json_at",
            "_read_json_no_follow_at",
            "_validate_control_destination",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(build_cache_module, name))

    def test_unsafe_marker_read_is_a_build_transaction_error(self) -> None:
        identity = self.identity(b"unsafe-read")
        with acquire_constructor_project_build_lock(self.repo, cache_root=self.cache) as lock:
            self._mark(lock, identity)
            os.chmod(self._marker_path(identity), 0o644)
            with lock.open_storage() as storage:
                with self.assertRaises(BuildTransactionError) as ctx:
                    build_cache_module._read_marker_json(
                        lock.ops, storage.markers, self._marker_name(identity)
                    )
        self.assertIn("unsafe transaction state file", str(ctx.exception))
        self.assertIsInstance(ctx.exception.__cause__, UnsafeFileError)

    def test_missing_marker_read_is_typed_with_absent_cause(self) -> None:
        missing = self.identity(b"missing-read")
        with acquire_constructor_project_build_lock(self.repo, cache_root=self.cache) as lock:
            with lock.open_storage() as storage:
                with self.assertRaises(TransactionError) as ctx:
                    build_cache_module._read_marker_json(
                        lock.ops, storage.markers, self._marker_name(missing)
                    )
        # The typed read failure is preserved; the absence outcome stays
        # inspectable through its exact ``FileNotFoundError`` cause.
        self.assertIsInstance(ctx.exception.cause, FileNotFoundError)
        self.assertEqual(ctx.exception.cause.errno, errno.ENOENT)
        self.assertIs(ctx.exception.__cause__, ctx.exception.cause)

    def test_malformed_marker_json_remains_a_value_error(self) -> None:
        identity = self.identity(b"malformed-read")
        with acquire_constructor_project_build_lock(self.repo, cache_root=self.cache) as lock:
            self._mark(lock, identity)
            marker = self._marker_path(identity)
            marker.write_text("not-json")
            with lock.open_storage() as storage:
                with self.assertRaises(json.JSONDecodeError):
                    build_cache_module._read_marker_json(
                        lock.ops, storage.markers, self._marker_name(identity)
                    )

    def test_forbidden_mode_marker_blocks_replacement_without_mutation(self) -> None:
        identity = self.identity(b"forbidden-mode-destination")
        with acquire_constructor_project_build_lock(self.repo, cache_root=self.cache) as lock:
            self._mark(lock, identity)
            marker = self._marker_path(identity)
            original = marker.read_bytes()
            os.chmod(marker, 0o644)
            with self.assertRaises(BuildTransactionError) as ctx:
                mark_uncommitted_blob(
                    identity, self.repo, lock=lock, verified_at=1,
                    cache_root=self.cache,
                )
        self.assertIn("unsafe transaction state file", str(ctx.exception))
        self.assertIsInstance(ctx.exception.__cause__, UnsafeFileError)
        self.assertEqual(marker.read_bytes(), original)
        self.assertEqual(marker.stat().st_mode & 0o777, 0o644)

    def test_unsafe_marker_destination_blocks_replacement(self) -> None:
        identity = self.identity(b"unsafe-destination")
        with acquire_constructor_project_build_lock(self.repo, cache_root=self.cache) as lock:
            marker = self._marker_path(identity)
            marker.symlink_to(marker.parent / "unrelated-target")
            with self.assertRaises(BuildTransactionError) as ctx:
                self._mark(lock, identity)
        self.assertIn("unsafe transaction state file", str(ctx.exception))
        self.assertIsInstance(ctx.exception.__cause__, UnsafeFileError)

    def test_replacement_failure_preserves_raw_error_and_secondary(self) -> None:
        identity = self.identity(b"replace-failure")
        boom = OSError(errno.EIO, "injected marker replace failure")
        cleanup = OSError(errno.EACCES, "injected marker cleanup failure")

        def failing_renameat(self, old_dir_fd, old, new_dir_fd, new):
            if isinstance(new, str) and new.endswith(".json"):
                raise boom
            return _REAL_RENAMEAT(self, old_dir_fd, old, new_dir_fd, new)

        def failing_unlinkat(self, dir_fd, name):
            if isinstance(name, str) and name.startswith(".transaction-"):
                raise cleanup
            return _REAL_UNLINKAT(self, dir_fd, name)

        with acquire_constructor_project_build_lock(self.repo, cache_root=self.cache) as lock:
            with mock.patch.object(PosixFileOps, "renameat", failing_renameat), \
                    mock.patch.object(PosixFileOps, "unlinkat", failing_unlinkat):
                with self.assertRaises(TransactionError) as ctx:
                    self._mark(lock, identity)
        # The typed replacement failure is preserved with the raw replacement
        # error as its cause and the cleanup failure as secondary context.
        self.assertIs(ctx.exception.cause, boom)
        self.assertIs(ctx.exception.__cause__, boom)
        self.assertEqual(ctx.exception.cause.errno, errno.EIO)
        self.assertEqual(list(ctx.exception.secondary), [cleanup])

    def test_directory_fsync_failure_preserves_raw_error(self) -> None:
        identity = self.identity(b"directory-fsync-failure")
        boom = OSError(errno.EIO, "injected marker directory fsync failure")

        def failing_fsync(self, fd):
            if os.path.basename(_fd_target(fd)) == "uncommitted":
                raise boom
            return _REAL_FSYNC(self, fd)

        with acquire_constructor_project_build_lock(self.repo, cache_root=self.cache) as lock:
            with mock.patch.object(PosixFileOps, "fsync", failing_fsync):
                with self.assertRaises(TransactionError) as ctx:
                    self._mark(lock, identity)
        self.assertIs(ctx.exception.cause, boom)
        self.assertIs(ctx.exception.__cause__, boom)
        self.assertEqual(ctx.exception.cause.errno, errno.EIO)
        self.assertEqual(
            str(ctx.exception.cause), "[Errno 5] injected marker directory fsync failure"
        )

    def test_interruption_during_replacement_propagates_unchanged(self) -> None:
        identity = self.identity(b"interrupt-replace")
        interrupt = KeyboardInterrupt()

        def interrupting_fchmod(self, fd, mode):
            if os.path.basename(_fd_target(fd)).startswith(".transaction-"):
                raise interrupt
            return _REAL_FCHMOD(self, fd, mode)

        with acquire_constructor_project_build_lock(self.repo, cache_root=self.cache) as lock:
            with mock.patch.object(PosixFileOps, "fchmod", interrupting_fchmod):
                with self.assertRaises(KeyboardInterrupt) as ctx:
                    self._mark(lock, identity)
        self.assertIs(ctx.exception, interrupt)
        self.assertFalse(self._marker_path(identity).exists())

    def test_unsafe_marker_close_failure_is_secondary(self) -> None:
        identity = self.identity(b"secondary-close")
        with acquire_constructor_project_build_lock(self.repo, cache_root=self.cache) as lock:
            self._mark(lock, identity)
            marker = self._marker_path(identity)
            os.chmod(marker, 0o644)
            boom = OSError(errno.EIO, "injected marker close failure")

            def failing_close(self, fd):
                if os.path.basename(_fd_target(fd)) == marker.name:
                    raise boom
                return _REAL_OPS_CLOSE(self, fd)

            with lock.open_storage() as storage:
                with mock.patch.object(PosixFileOps, "close", failing_close):
                    with self.assertRaises(BuildTransactionError) as ctx:
                        build_cache_module._read_marker_json(
                            lock.ops, storage.markers, marker.name
                        )
        cause = ctx.exception.__cause__
        self.assertIsInstance(cause, UnsafeFileError)
        self.assertTrue(any("injected marker close failure" in str(exc) for exc in cause.secondary))


class MaintenanceDurableUnlinkTests(_BuildTestCase):
    """Uncommitted maintenance deletes through retained L2 capabilities."""

    def _prepare(self, payload: bytes):
        identity = self.publish(payload)
        marker = self.paths().markers_root / f"sha256:{identity.hex_digest()}.json"
        blob = build_blob_path(self.paths().blobs_root, identity)
        lock = acquire_constructor_project_build_lock(
            self.repo, cache_root=self.cache
        )
        self.addCleanup(lock.release)
        mark_uncommitted_blob(
            identity, self.repo, lock=lock, verified_at=0, cache_root=self.cache
        )
        return lock, identity, blob, marker

    def _expire(self, lock) -> None:
        maintain_uncommitted_blobs(
            self.repo, lock=lock, now=UNCOMMITTED_TTL_SECONDS + 1,
            cache_root=self.cache,
        )

    def test_marker_unlink_failure_preserves_typed_error(self) -> None:
        lock, _identity, blob, marker = self._prepare(b"marker-unlink-failure")
        boom = OSError(errno.EIO, "injected maintenance marker unlink failure")

        def failing_unlinkat(self, dir_fd, name):
            if name == marker.name:
                raise boom
            return _REAL_UNLINKAT(self, dir_fd, name)

        with mock.patch.object(PosixFileOps, "unlinkat", failing_unlinkat):
            with self.assertRaises(TransactionError) as ctx:
                self._expire(lock)
        self.assertIs(ctx.exception.cause, boom)
        self.assertIs(ctx.exception.__cause__, boom)
        self.assertFalse(blob.exists())
        self.assertTrue(marker.exists())

    def test_blob_unlink_failure_preserves_marker(self) -> None:
        lock, _identity, blob, marker = self._prepare(b"blob-unlink-failure")
        boom = OSError(errno.EIO, "injected maintenance blob unlink failure")

        def failing_unlinkat(self, dir_fd, name):
            if name == blob.name:
                raise boom
            return _REAL_UNLINKAT(self, dir_fd, name)

        with mock.patch.object(PosixFileOps, "unlinkat", failing_unlinkat):
            with self.assertRaises(TransactionError) as ctx:
                self._expire(lock)
        self.assertIs(ctx.exception.cause, boom)
        self.assertTrue(blob.exists())
        self.assertTrue(marker.exists())

    def test_marker_directory_fsync_failure_is_fatal(self) -> None:
        lock, _identity, blob, marker = self._prepare(b"marker-fsync-failure")
        boom = OSError(errno.EIO, "injected maintenance marker fsync failure")

        def failing_fsync(self, fd):
            if os.path.basename(_fd_target(fd)) == "uncommitted":
                raise boom
            return _REAL_FSYNC(self, fd)

        with mock.patch.object(PosixFileOps, "fsync", failing_fsync):
            with self.assertRaises(TransactionError) as ctx:
                self._expire(lock)
        self.assertIs(ctx.exception.cause, boom)
        self.assertFalse(blob.exists())
        self.assertFalse(marker.exists())

    def test_blob_directory_fsync_failure_retains_marker(self) -> None:
        lock, _identity, blob, marker = self._prepare(b"blob-fsync-failure")
        boom = OSError(errno.EIO, "injected maintenance blob fsync failure")

        def failing_fsync(self, fd):
            if os.path.basename(_fd_target(fd)) == "sha256":
                raise boom
            return _REAL_FSYNC(self, fd)

        with mock.patch.object(PosixFileOps, "fsync", failing_fsync):
            with self.assertRaises(TransactionError) as ctx:
                self._expire(lock)
        self.assertIs(ctx.exception.cause, boom)
        self.assertFalse(blob.exists())
        self.assertTrue(marker.exists())

    def test_already_absent_entries_are_idempotent_and_synchronized(self) -> None:
        lock, identity, blob, marker = self._prepare(b"already-absent")
        blob.unlink()
        marker.unlink()
        synced: list[str] = []

        def recording_fsync(self, fd):
            synced.append(os.path.basename(_fd_target(fd)))
            return _REAL_FSYNC(self, fd)

        with lock.open_storage() as storage:
            with mock.patch.object(PosixFileOps, "fsync", recording_fsync):
                build_cache_module._durably_remove_blob_and_marker(
                    lock.ops, storage.blobs, storage.markers, identity
                )
        self.assertIn("sha256", synced)
        self.assertIn("uncommitted", synced)

    def test_unsafe_marker_and_blob_entries_fail_closed(self) -> None:
        for unsafe in ("marker", "blob"):
            with self.subTest(unsafe=unsafe):
                lock, _identity, blob, marker = self._prepare(
                    f"unsafe-{unsafe}".encode()
                )
                target = marker if unsafe == "marker" else blob
                target.chmod(0o644)
                with self.assertRaises(BuildTransactionError):
                    self._expire(lock)
                self.assertTrue(blob.exists())
                self.assertTrue(marker.exists())
                lock.release()

    def test_close_failure_is_secondary_to_unlink_failure(self) -> None:
        lock, _identity, blob, marker = self._prepare(b"unlink-close-failure")
        primary = OSError(errno.EIO, "injected maintenance unlink failure")
        secondary = OSError(errno.EBADF, "injected maintenance close failure")

        def failing_unlinkat(self, dir_fd, name):
            if name == blob.name:
                raise primary
            return _REAL_UNLINKAT(self, dir_fd, name)

        blob_closes = {"count": 0}

        def failing_close(self, fd):
            if os.path.basename(_fd_target(fd)) == blob.name:
                blob_closes["count"] += 1
                if blob_closes["count"] == 2:
                    raise secondary
            return _REAL_OPS_CLOSE(self, fd)

        with mock.patch.object(PosixFileOps, "unlinkat", failing_unlinkat), \
                mock.patch.object(PosixFileOps, "close", failing_close):
            with self.assertRaises(TransactionError) as ctx:
                self._expire(lock)
        # The typed unlink failure keeps the raw unlink cause; the close
        # failure is attached as a secondary diagnostic.
        self.assertIs(ctx.exception.cause, primary)
        self.assertIn(secondary, list(ctx.exception.secondary))
        self.assertTrue(marker.exists())

    def test_interruption_during_unlink_propagates_unchanged(self) -> None:
        lock, _identity, blob, marker = self._prepare(b"unlink-interruption")
        interrupt = KeyboardInterrupt("interrupted maintenance unlink")

        def interrupting_unlinkat(self, dir_fd, name):
            if name == blob.name:
                raise interrupt
            return _REAL_UNLINKAT(self, dir_fd, name)

        with mock.patch.object(PosixFileOps, "unlinkat", interrupting_unlinkat):
            with self.assertRaises(KeyboardInterrupt) as ctx:
                self._expire(lock)
        self.assertIs(ctx.exception, interrupt)
        self.assertTrue(blob.exists())
        self.assertTrue(marker.exists())


class ExitCodeMappingTests(unittest.TestCase):
    def test_operational_cleanup_failure_is_build_cache_error(self) -> None:
        # Orchestration maps BuildCacheError to OPERATIONAL, and OPERATIONAL to 4.
        from docker.constructor_cli import _EXIT_CODES
        from docker.versioning.dispatch_types import ExitKind

        self.assertTrue(issubclass(BuildCleanupError, BuildCacheError))
        self.assertEqual(_EXIT_CODES[ExitKind.OPERATIONAL], 4)


if __name__ == "__main__":
    unittest.main()
