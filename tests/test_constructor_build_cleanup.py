"""Phase 4 RED contracts — sequential build cleanup and recovery.

These tests pin the build-domain cleanup/recovery surface added by Phase 4:

* candidate derivation as exactly canonical ``previous - current``;
* authoritative-generation marker reconciliation with one shared
  marker-directory synchronization, including when every marker is already
  absent;
* all-candidate superseded cleanup batched by containing directory, with one
  post-batch synchronization per affected existing blob directory and one for
  the shared marker directory, aggregate diagnostics, predecessor preservation,
  and durable predecessor removal only after every batch is durable;
* deterministic restart recovery under the checkout lock, including retries
  that find a batch already absent and interruptions at every boundary.

The module under test does not exist until the Phase 4 GREEN tasks.
"""
from __future__ import annotations

import errno
import hashlib
import os
import stat
import tempfile
import unittest
from pathlib import Path

from docker.transactions.capabilities import DirectoryCapability
from docker.transactions.errors import CapabilityError
from docker.versioning.build_cleanup import (
    ALGORITHM_DIRECTORY_MODE,
    BLOB_SUFFIX,
    MARKER_SUFFIX,
    BuildCleanupError,
    BuildStorage,
    blob_name,
    cleanup_superseded,
    marker_name,
    reconcile_authoritative_markers,
    recover_generations,
    superseded_candidates,
)
from docker.versioning.build_generations import (
    GENERATION_MODE,
    BuildGeneration,
    BuildGenerationError,
    BuildManifest,
    GenerationInventory,
    GenerationState,
    acquire_build_generation_lock,
    canonical_blob_key,
    format_generation_name,
)
from docker.versioning.digest_identity import DigestIdentity
from tests.transactions_test_support import InjectedOps


class _Cancellation(BaseException):
    """A cancellation-style, non-I/O exception."""


def identity(payload: bytes) -> DigestIdentity:
    return DigestIdentity.from_hex("sha256", hashlib.sha256(payload).hexdigest())


def identity_alg(algorithm: str, payload: bytes) -> DigestIdentity:
    return DigestIdentity.from_hex(algorithm, hashlib.new(algorithm, payload).hexdigest())


class RecordingOps(InjectedOps):
    """Injected L0 backend that also records opened descriptors."""

    def __init__(self) -> None:
        super().__init__()
        self.opened: list[tuple[object, str, int]] = []
        self.opened_names: dict[int, str] = {}
        # name -> callable(real stat_result) -> stat_result, applied by fstat
        # for descriptors opened under that basename.  This lets a test make
        # exactly one leaf appear foreign-owned without disturbing the
        # algorithm-directory validation.
        self.fstat_by_name: dict[str, object] = {}
        # name -> exception raised by fstat for descriptors opened under that
        # basename, used to inject unexpected (non-OSError/UnsafeFileError)
        # failures during leaf validation.
        self.fstat_raises_by_name: dict[str, BaseException] = {}
        # name -> {occurrence_index: exception}, raised by fstat for the Nth
        # (zero-based) fstat issued against a descriptor under that basename.
        # Used to target the mode-check fstat that follows capability
        # adoption, after the initial validation fstat has succeeded.
        self.fstat_raises_on: dict[str, dict[int, BaseException]] = {}
        self.fstat_call_index: dict[str, int] = {}
        # Descriptors opened through this backend that have not been closed.
        self.live_fds: set[int] = set()

    def openat(self, dir_fd, name, flags, mode=0o777):
        fd = super().openat(dir_fd, name, flags, mode)
        self.opened.append((dir_fd, name, fd))
        self.opened_names[fd] = name
        self.live_fds.add(fd)
        return fd

    def close(self, fd):
        try:
            super().close(fd)
        finally:
            self.live_fds.discard(fd)

    def fstat(self, fd):
        name = self.opened_names.get(fd)
        index = self.fstat_call_index.get(name, 0)
        self.fstat_call_index[name] = index + 1
        scheduled = self.fstat_raises_on.get(name)
        if scheduled is not None and index in scheduled:
            raise scheduled[index]
        raised = self.fstat_raises_by_name.get(name)
        if raised is not None:
            raise raised
        info = super().fstat(fd)
        override = self.fstat_by_name.get(name)
        if override is not None:
            return override(info)
        return info

    def fd_for(self, name: str) -> int:
        matches = [fd for _, entry, fd in self.opened if entry == name]
        if len(matches) != 1:
            raise AssertionError(f"expected exactly one open of {name!r}, got {matches}")
        return matches[0]


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


class _CleanupTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ops = RecordingOps()
        self.generations = DirectoryCapability.from_path(self.ops, self.root)
        self.addCleanup(self._close, self.generations)
        (self.root / "blobs").mkdir(mode=ALGORITHM_DIRECTORY_MODE)
        (self.root / "markers").mkdir(mode=ALGORITHM_DIRECTORY_MODE)
        self.blobs = DirectoryCapability.from_path(self.ops, self.root / "blobs")
        self.markers = DirectoryCapability.from_path(self.ops, self.root / "markers")
        self.addCleanup(self._close, self.blobs)
        self.addCleanup(self._close, self.markers)
        self.storage = BuildStorage(
            generations=self.generations, blobs=self.blobs, markers=self.markers
        )
        self.lock = acquire_build_generation_lock(self.ops, self.generations)
        self.addCleanup(self._close, self.lock)
        self.ops.reset()

    def _close(self, capability) -> None:
        try:
            if not capability.closed:
                capability.close()
        except OSError:
            pass

    # -- fixtures --------------------------------------------------------
    def write_generation(self, number: int, blobs=()) -> str:
        name = format_generation_name(number)
        target = self.root / name
        target.write_bytes(BuildManifest.from_blobs(blobs).encode())
        os.chmod(target, GENERATION_MODE)
        return name

    def add_blob(self, blob: DigestIdentity) -> Path:
        directory = self.root / "blobs" / blob.algorithm
        directory.mkdir(mode=ALGORITHM_DIRECTORY_MODE, exist_ok=True)
        os.chmod(directory, ALGORITHM_DIRECTORY_MODE)
        target = directory / (blob.hex_digest() + BLOB_SUFFIX)
        if target.exists():
            return target
        target.write_bytes(b"payload")
        os.chmod(target, 0o444)
        return target

    def add_marker(self, blob: DigestIdentity) -> Path:
        target = self.root / "markers" / marker_name(blob)
        target.write_bytes(b'{"verified_at":1}')
        os.chmod(target, 0o600)
        return target

    def blob_path(self, blob: DigestIdentity) -> Path:
        return self.root / "blobs" / blob.algorithm / (blob.hex_digest() + BLOB_SUFFIX)

    def marker_path(self, blob: DigestIdentity) -> Path:
        return self.root / "markers" / marker_name(blob)

    def inventory(self, previous_blobs, current_blobs) -> GenerationInventory:
        previous = BuildGeneration(
            1, format_generation_name(1), BuildManifest.from_blobs(previous_blobs)
        )
        current = BuildGeneration(
            2, format_generation_name(2), BuildManifest.from_blobs(current_blobs)
        )
        return GenerationInventory(
            state=GenerationState.RECOVERABLE, generations=(previous, current)
        )

    def fsync_fd_set(self) -> set[int]:
        return {args[0] for args in self.ops.arg_pairs("fsync")}

    def fail_unlink_for(self, name: str, error: BaseException) -> None:
        def hook(dir_fd, target) -> None:  # noqa: ANN001
            if target == name:
                raise error

        existing = self.ops.hooks.get("unlinkat")

        def combined(dir_fd, target) -> None:  # noqa: ANN001
            if existing is not None:
                existing(dir_fd, target)
            hook(dir_fd, target)

        self.ops.hooks["unlinkat"] = combined


# ═══════════════════════════════════════════════════════════════════════
# 4.1 candidate derivation
# ═══════════════════════════════════════════════════════════════════════


class CandidateDerivationTests(_CleanupTestCase):
    def test_candidates_are_exactly_previous_minus_current(self) -> None:
        first = identity(b"first")
        shared = identity(b"shared")
        latest = identity(b"latest")
        inventory = self.inventory([first, shared], [shared, latest])
        self.assertEqual(superseded_candidates(inventory), (first,))

    def test_candidates_are_canonical_order(self) -> None:
        a = identity(b"a")
        b = identity(b"b")
        c = identity(b"c")
        inventory = self.inventory([c, a, b], [identity(b"current")])
        expected = tuple(sorted((a, b, c), key=canonical_blob_key))
        self.assertEqual(superseded_candidates(inventory), expected)

    def test_single_generation_has_no_candidates(self) -> None:
        current = BuildGeneration(
            1, format_generation_name(1), BuildManifest.from_blobs([identity(b"only")])
        )
        inventory = GenerationInventory(
            state=GenerationState.STABLE, generations=(current,)
        )
        self.assertEqual(superseded_candidates(inventory), ())

    def test_empty_inventory_has_no_candidates(self) -> None:
        inventory = GenerationInventory(state=GenerationState.EMPTY, generations=())
        self.assertEqual(superseded_candidates(inventory), ())

    def test_current_identities_are_never_candidates(self) -> None:
        shared = identity(b"shared")
        current_only = identity(b"current")
        previous_only = identity(b"previous")
        inventory = self.inventory([previous_only, shared], [shared, current_only])
        candidates = superseded_candidates(inventory)
        self.assertNotIn(shared, candidates)
        self.assertNotIn(current_only, candidates)
        self.assertEqual(candidates, (previous_only,))

    def test_naming_helpers_use_canonical_identities(self) -> None:
        blob = identity(b"x")
        self.assertEqual(blob_name(blob), blob.hex_digest() + BLOB_SUFFIX)
        self.assertEqual(
            marker_name(blob),
            f"{blob.algorithm}:{blob.hex_digest()}{MARKER_SUFFIX}",
        )


# ═══════════════════════════════════════════════════════════════════════
# 4.3 / 4.6 authoritative-generation marker reconciliation
# ═══════════════════════════════════════════════════════════════════════


class MarkerReconciliationTests(_CleanupTestCase):
    def test_removes_committed_markers_and_fsyncs_marker_directory_once(self) -> None:
        first = identity(b"first")
        second = identity(b"second")
        stale = identity(b"stale")
        self.add_marker(first)
        self.add_marker(second)
        self.add_marker(stale)
        self.add_blob(stale)
        current = BuildGeneration(
            1,
            format_generation_name(1),
            BuildManifest.from_blobs([first, second]),
        )
        self.ops.reset()
        reconcile_authoritative_markers(self.ops, self.storage, current, lock=self.lock)
        self.assertFalse(self.marker_path(first).exists())
        self.assertFalse(self.marker_path(second).exists())
        self.assertTrue(self.marker_path(stale).exists())
        self.assertTrue(self.blob_path(stale).exists())
        self.assertEqual(self.ops.counts.get("fsync"), 1)
        self.assertEqual(self.ops.counts.get("unlinkat"), 2)

    def test_fsyncs_marker_directory_when_all_markers_already_absent(self) -> None:
        current = BuildGeneration(
            1, format_generation_name(1), BuildManifest.from_blobs([identity(b"x")])
        )
        self.ops.reset()
        reconcile_authoritative_markers(self.ops, self.storage, current, lock=self.lock)
        self.assertEqual(self.ops.counts.get("fsync"), 1)
        self.assertEqual(self.fsync_fd_set(), {self.markers.fd})
        # A genuinely absent marker is idempotent success: it is never opened
        # for unlink, so no unlinkat is issued, but the directory is still
        # synchronized once.
        self.assertEqual(self.ops.counts.get("unlinkat", 0), 0)

    def test_marker_unlink_failure_is_aggregated_and_recovery_still_fsyncs(self) -> None:
        first = identity(b"first")
        second = identity(b"second")
        self.add_marker(first)
        self.add_marker(second)
        self.fail_unlink_for(marker_name(first), OSError(errno.EIO, "injected marker failure"))
        current = BuildGeneration(
            1,
            format_generation_name(1),
            BuildManifest.from_blobs([first, second]),
        )
        self.ops.reset()
        with self.assertRaises(BuildCleanupError) as ctx:
            reconcile_authoritative_markers(self.ops, self.storage, current, lock=self.lock)
        self.assertEqual(len(ctx.exception.failures), 1)
        self.assertTrue(self.marker_path(first).exists())
        self.assertFalse(self.marker_path(second).exists())
        self.assertEqual(self.ops.counts.get("fsync"), 1)

    def test_marker_directory_fsync_failure_is_reported(self) -> None:
        blob = identity(b"x")
        self.add_marker(blob)
        self.ops.hooks["fsync"] = lambda fd: (
            (_ for _ in ()).throw(OSError(errno.EIO, "injected marker fsync"))
            if fd == self.markers.fd
            else None
        )
        current = BuildGeneration(
            1, format_generation_name(1), BuildManifest.from_blobs([blob])
        )
        self.ops.reset()
        with self.assertRaises(BuildCleanupError) as ctx:
            reconcile_authoritative_markers(self.ops, self.storage, current, lock=self.lock)
        self.assertEqual(len(ctx.exception.failures), 1)
        self.assertFalse(self.marker_path(blob).exists())

    def test_reconciliation_requires_a_live_matching_lock(self) -> None:
        current = BuildGeneration(
            1, format_generation_name(1), BuildManifest.from_blobs([identity(b"x")])
        )
        self.lock.close()
        with self.assertRaises(CapabilityError):
            reconcile_authoritative_markers(self.ops, self.storage, current, lock=self.lock)

    def test_unsafe_authoritative_marker_is_preserved(self) -> None:
        blob = identity(b"authoritative")
        target = self.root / "authoritative-marker-target.bin"
        target.write_bytes(b"marker symlink target")
        self.marker_path(blob).symlink_to(target)
        current = BuildGeneration(
            1, format_generation_name(1), BuildManifest.from_blobs([blob])
        )
        self.ops.reset()
        with self.assertRaises(BuildCleanupError) as ctx:
            reconcile_authoritative_markers(
                self.ops, self.storage, current, lock=self.lock
            )
        self.assertEqual(len(ctx.exception.failures), 1)
        # The unsafe marker and its symlink target are untouched; the
        # generation stays committed and the marker directory is still synced.
        self.assertTrue(self.marker_path(blob).is_symlink())
        self.assertTrue(target.exists())
        self.assertEqual(target.read_bytes(), b"marker symlink target")
        self.assertEqual(self.ops.counts.get("unlinkat", 0), 0)
        self.assertEqual(self.ops.counts.get("fsync"), 1)

    def test_forbidden_mode_authoritative_marker_is_preserved(self) -> None:
        blob = identity(b"authoritative-mode")
        self.add_marker(blob)
        os.chmod(self.marker_path(blob), 0o666)
        current = BuildGeneration(
            1, format_generation_name(1), BuildManifest.from_blobs([blob])
        )
        with self.assertRaises(BuildCleanupError) as ctx:
            reconcile_authoritative_markers(
                self.ops, self.storage, current, lock=self.lock
            )
        self.assertEqual(len(ctx.exception.failures), 1)
        self.assertTrue(self.marker_path(blob).exists())
        self.assertEqual(self.ops.counts.get("unlinkat", 0), 0)

    def test_authority_sync_precedes_marker_removal(self) -> None:
        blob = identity(b"x")
        self.add_blob(blob)
        self.write_generation(1, [blob])
        self.add_marker(blob)
        self.ops.reset()
        recover_generations(self.ops, self.storage, lock=self.lock)
        order = self.ops.order
        generation_sync = next(
            index
            for index, (name, args) in enumerate(self.ops.calls)
            if name == "fsync" and args[0] == self.generations.fd
        )
        marker_unlink = next(
            index
            for index, (name, args) in enumerate(self.ops.calls)
            if name == "unlinkat" and args[1] == marker_name(blob)
        )
        self.assertLess(generation_sync, marker_unlink)


# ═══════════════════════════════════════════════════════════════════════
# 4.2 / 4.3 superseded candidate cleanup
# ═══════════════════════════════════════════════════════════════════════


class SupersededCleanupTests(_CleanupTestCase):
    def _two_generation_state(self, previous_blobs, current_blobs):
        for blob in previous_blobs:
            self.add_blob(blob)
        for blob in current_blobs:
            self.add_blob(blob)
        self.write_generation(1, previous_blobs)
        self.write_generation(2, current_blobs)
        return self.inventory(previous_blobs, current_blobs)

    def test_removes_only_candidates_and_preserves_current_blobs(self) -> None:
        shared = identity(b"shared")
        previous_only = identity(b"previous")
        current_only = identity(b"current")
        inventory = self._two_generation_state(
            [previous_only, shared], [shared, current_only]
        )
        self.add_marker(shared)
        cleanup_superseded(self.ops, self.storage, inventory, lock=self.lock)
        self.assertFalse(self.blob_path(previous_only).exists())
        self.assertTrue(self.blob_path(shared).exists())
        self.assertTrue(self.blob_path(current_only).exists())
        self.assertTrue(self.marker_path(shared).exists())
        self.assertFalse((self.root / format_generation_name(1)).exists())
        self.assertTrue((self.root / format_generation_name(2)).exists())

    def test_batches_unlinks_then_fsyncs_each_directory_once(self) -> None:
        a1 = identity_alg("sha256", b"a1")
        a2 = identity_alg("sha256", b"a2")
        b1 = identity_alg("sha512", b"b1")
        inventory = self._two_generation_state([a1, a2, b1], [])
        for blob in (a1, a2, b1):
            self.add_marker(blob)
        self.ops.reset()
        cleanup_superseded(self.ops, self.storage, inventory, lock=self.lock)
        # Three blobs and three markers are unlinked, then each affected blob
        # directory and the shared marker directory, then the predecessor.
        self.assertEqual(self.ops.counts.get("unlinkat"), 7)
        fsync_fds = [args[0] for args in self.ops.arg_pairs("fsync")]
        self.assertEqual(len(fsync_fds), 4)
        self.assertEqual(len(set(fsync_fds)), 4)
        self.assertEqual(self.ops.counts.get("fsync"), 4)

    def test_every_directory_is_fsynced_after_all_its_unlinks(self) -> None:
        a1 = identity_alg("sha256", b"a1")
        a2 = identity_alg("sha256", b"a2")
        inventory = self._two_generation_state([a1, a2], [])
        predecessor = format_generation_name(1)
        self.ops.reset()
        cleanup_superseded(self.ops, self.storage, inventory, lock=self.lock)
        order = self.ops.order
        first_fsync = order.index("fsync")
        candidate_unlinks = [
            index
            for index, (name, args) in enumerate(self.ops.calls)
            if name == "unlinkat" and args[1] != predecessor
        ]
        self.assertEqual(len(candidate_unlinks), 2)
        self.assertLess(max(candidate_unlinks), first_fsync)

    def test_all_candidates_are_attempted_after_a_failure(self) -> None:
        a = identity_alg("sha256", b"a")
        b = identity_alg("sha256", b"b")
        c = identity_alg("sha256", b"c")
        inventory = self._two_generation_state([a, b, c], [])
        for blob in (a, b, c):
            self.add_marker(blob)
        self.fail_unlink_for(blob_name(a), OSError(errno.EACCES, "injected blob failure"))
        self.ops.reset()
        with self.assertRaises(BuildCleanupError) as ctx:
            cleanup_superseded(self.ops, self.storage, inventory, lock=self.lock)
        self.assertEqual(len(ctx.exception.failures), 1)
        self.assertTrue(self.blob_path(a).exists())
        self.assertFalse(self.blob_path(b).exists())
        self.assertFalse(self.blob_path(c).exists())
        self.assertFalse(self.marker_path(b).exists())
        self.assertFalse(self.marker_path(c).exists())
        # Successful removals are still synchronized after another failed;
        # the predecessor is retained, so no generation-directory fsync runs.
        self.assertEqual(self.ops.counts.get("fsync"), 2)
        self.assertTrue((self.root / format_generation_name(1)).exists())

    def test_blob_and_marker_failures_are_aggregated(self) -> None:
        a = identity_alg("sha256", b"a")
        b = identity_alg("sha256", b"b")
        inventory = self._two_generation_state([a, b], [])
        self.add_marker(a)
        self.add_marker(b)
        self.fail_unlink_for(blob_name(a), OSError(errno.EIO, "blob failure"))
        self.fail_unlink_for(marker_name(b), OSError(errno.EIO, "marker failure"))
        with self.assertRaises(BuildCleanupError) as ctx:
            cleanup_superseded(self.ops, self.storage, inventory, lock=self.lock)
        self.assertEqual(len(ctx.exception.failures), 2)
        self.assertTrue((self.root / format_generation_name(1)).exists())

    def test_marker_directory_post_batch_fsync_failure_retains_predecessor(self) -> None:
        blob = identity_alg("sha256", b"only")
        inventory = self._two_generation_state([blob], [])
        self.add_marker(blob)
        self.ops.hooks["fsync"] = lambda fd: (
            (_ for _ in ()).throw(OSError(errno.EIO, "injected marker fsync"))
            if fd == self.markers.fd
            else None
        )
        with self.assertRaises(BuildCleanupError) as ctx:
            cleanup_superseded(self.ops, self.storage, inventory, lock=self.lock)
        self.assertEqual(len(ctx.exception.failures), 1)
        self.assertTrue((self.root / format_generation_name(1)).exists())

    def test_fsync_count_scales_with_directories_not_candidates(self) -> None:
        blobs = [identity_alg("sha256", f"c{index}".encode()) for index in range(5)]
        inventory = self._two_generation_state(blobs, [])
        self.ops.reset()
        cleanup_superseded(self.ops, self.storage, inventory, lock=self.lock)
        # Five candidates share one algorithm directory, so the fsync count is
        # the algorithm directory, the marker directory, and the generation
        # directory — never one per candidate.
        self.assertEqual(self.ops.counts.get("fsync"), 3)
        # Five blob unlinks plus the durable predecessor removal; no markers
        # were created, so an absent marker is cleared by its failed open and
        # issues no unlinkat.
        self.assertEqual(self.ops.counts.get("unlinkat"), 6)

    def test_multiple_directory_fsync_failures_are_aggregated(self) -> None:
        a = identity_alg("sha256", b"a")
        b = identity_alg("sha512", b"b")
        inventory = self._two_generation_state([a, b], [])
        for blob in (a, b):
            self.add_marker(blob)
        # Fsync calls 1 and 2 are the two affected blob directories; call 3 is
        # the shared marker directory.
        self.ops.failures["fsync"] = lambda count: (
            OSError(errno.EIO, "injected fsync") if count <= 3 else None
        )
        with self.assertRaises(BuildCleanupError) as ctx:
            cleanup_superseded(self.ops, self.storage, inventory, lock=self.lock)
        self.assertEqual(len(ctx.exception.failures), 3)
        self.assertTrue((self.root / format_generation_name(1)).exists())

    def test_blob_directory_post_batch_fsync_failure_retains_predecessor(self) -> None:
        blob = identity_alg("sha256", b"only")
        inventory = self._two_generation_state([blob], [])
        # Fsync call 1 synchronizes the affected blob directory.
        self.ops.failures["fsync"] = lambda count: (
            OSError(errno.EIO, "injected blob dir fsync") if count == 1 else None
        )
        with self.assertRaises(BuildCleanupError) as ctx:
            cleanup_superseded(self.ops, self.storage, inventory, lock=self.lock)
        self.assertEqual(len(ctx.exception.failures), 1)
        self.assertTrue((self.root / format_generation_name(1)).exists())

    def test_missing_algorithm_directory_still_removes_marker(self) -> None:
        blob = identity_alg("sha256", b"only")
        self.write_generation(1, [blob])
        self.write_generation(2, [])
        self.add_marker(blob)
        inventory = self.inventory([blob], [])
        cleanup_superseded(self.ops, self.storage, inventory, lock=self.lock)
        self.assertFalse(self.marker_path(blob).exists())
        self.assertFalse((self.root / format_generation_name(1)).exists())

    def test_unsafe_algorithm_directory_fails_closed_and_preserves_marker(self) -> None:
        blob = identity_alg("sha256", b"only")
        inventory = self._two_generation_state([blob], [])
        self.add_marker(blob)
        # Replace the real algorithm directory with a symlink.
        real = self.root / "blobs" / "sha256"
        real.rename(self.root / "elsewhere")
        (self.root / "blobs" / "sha256").symlink_to(self.root / "elsewhere")
        with self.assertRaises(BuildCleanupError) as ctx:
            cleanup_superseded(self.ops, self.storage, inventory, lock=self.lock)
        self.assertEqual(len(ctx.exception.failures), 1)
        self.assertTrue(self.marker_path(blob).exists())
        self.assertTrue((self.root / format_generation_name(1)).exists())

    def test_permissive_algorithm_directory_fails_closed(self) -> None:
        blob = identity_alg("sha256", b"only")
        inventory = self._two_generation_state([blob], [])
        os.chmod(self.root / "blobs" / "sha256", 0o755)
        with self.assertRaises(BuildCleanupError) as ctx:
            cleanup_superseded(self.ops, self.storage, inventory, lock=self.lock)
        self.assertEqual(len(ctx.exception.failures), 1)
        self.assertTrue(self.blob_path(blob).exists())
        self.assertTrue((self.root / format_generation_name(1)).exists())

    def test_predecessor_is_removed_only_after_candidate_batches_are_durable(self) -> None:
        blob = identity_alg("sha256", b"only")
        inventory = self._two_generation_state([blob], [])
        predecessor = format_generation_name(1)
        self.ops.reset()
        cleanup_superseded(self.ops, self.storage, inventory, lock=self.lock)
        order = self.ops.order
        predecessor_unlink = None
        for index, (name, args) in enumerate(self.ops.calls):
            if name == "unlinkat" and args[1] == predecessor:
                predecessor_unlink = index
        self.assertIsNotNone(predecessor_unlink)
        # Every candidate blob/marker unlink and every candidate-directory
        # fsync precedes the predecessor unlink; the generation-directory
        # fsync that completes the predecessor removal follows it.
        candidate_fsyncs = [
            index for index, (name, _args) in enumerate(self.ops.calls) if name == "fsync"
        ]
        self.assertGreaterEqual(len(candidate_fsyncs), 2)
        for index in candidate_fsyncs[:-1]:
            self.assertLess(index, predecessor_unlink)
        self.assertLess(predecessor_unlink, candidate_fsyncs[-1])
        for index, (name, _args) in enumerate(self.ops.calls):
            if name == "unlinkat" and index != predecessor_unlink:
                self.assertLess(index, predecessor_unlink)

    def test_no_candidates_still_removes_the_predecessor(self) -> None:
        shared = identity(b"shared")
        extra = identity(b"extra")
        self.write_generation(1, [shared])
        self.write_generation(2, [shared, extra])
        inventory = self.inventory([shared], [shared, extra])
        cleanup_superseded(self.ops, self.storage, inventory, lock=self.lock)
        self.assertFalse((self.root / format_generation_name(1)).exists())
        self.assertTrue((self.root / format_generation_name(2)).exists())

    def test_generation_directory_fsync_failure_after_unlink_reports_failure(self) -> None:
        blob = identity_alg("sha256", b"only")
        inventory = self._two_generation_state([blob], [])
        predecessor = format_generation_name(1)
        unlinked = {"done": False}
        original = self.ops.hooks.get("unlinkat")

        def unlink_hook(dir_fd, name):
            if original is not None:
                original(dir_fd, name)
            if name == predecessor:
                unlinked["done"] = True

        self.ops.hooks["unlinkat"] = unlink_hook
        self.ops.hooks["fsync"] = lambda fd: (
            (_ for _ in ()).throw(OSError(errno.EIO, "injected generation fsync"))
            if unlinked["done"] and fd == self.generations.fd
            else None
        )
        with self.assertRaises(BuildCleanupError) as ctx:
            cleanup_superseded(self.ops, self.storage, inventory, lock=self.lock)
        self.assertEqual(len(ctx.exception.failures), 1)
        self.assertEqual(ctx.exception.failures[0].target, "predecessor")
        self.assertNotIn("remain", str(ctx.exception))
        self.assertFalse((self.root / predecessor).exists())
        self.assertTrue((self.root / format_generation_name(2)).exists())

    def test_cleanup_requires_a_live_matching_lock(self) -> None:
        blob = identity_alg("sha256", b"only")
        inventory = self._two_generation_state([blob], [])
        self.lock.close()
        with self.assertRaises(CapabilityError):
            cleanup_superseded(self.ops, self.storage, inventory, lock=self.lock)


# ═══════════════════════════════════════════════════════════════════════
# unsafe leaf validation
# ═══════════════════════════════════════════════════════════════════════


class AlgorithmDirectoryLifecycleTests(_CleanupTestCase):
    """Algorithm-directory open/validate failures must not abort cleanup."""

    def _two_generation_state(self, previous_blobs, current_blobs):
        for blob in previous_blobs:
            self.add_blob(blob)
        for blob in current_blobs:
            self.add_blob(blob)
        self.write_generation(1, previous_blobs)
        self.write_generation(2, current_blobs)
        return self.inventory(previous_blobs, current_blobs)

    def test_multi_algorithm_oserror_is_aggregated_and_other_candidate_cleaned(
        self,
    ) -> None:
        failing = identity_alg("sha256", b"failing-algorithm")
        healthy = identity_alg("sha512", b"healthy-algorithm")
        self.add_blob(failing)
        self.add_blob(healthy)
        self.add_marker(failing)
        self.add_marker(healthy)
        self.write_generation(1, [failing, healthy])
        self.write_generation(2, [])
        inventory = self.inventory([failing, healthy], [])
        boom = OSError(errno.EIO, "cannot stat algorithm directory")
        self.ops.reset()
        # The failure is injected at the initial validation fstat inside
        # DirectoryCapability.from_fd, before the mode-check fstat.
        self.ops.fstat_raises_on["sha256"] = {0: boom}
        live_before = set(self.ops.live_fds)
        with self.assertRaises(BuildCleanupError) as ctx:
            cleanup_superseded(self.ops, self.storage, inventory, lock=self.lock)
        self.assertEqual(
            [failure.target for failure in ctx.exception.failures], ["sha256"]
        )
        self.assertIs(ctx.exception.failures[0].error, boom)
        # The healthy algorithm's candidate and marker are durably removed.
        self.assertFalse(self.blob_path(healthy).exists())
        self.assertFalse(self.marker_path(healthy).exists())
        # The failing algorithm's candidate and marker are preserved.
        self.assertTrue(self.blob_path(failing).exists())
        self.assertTrue(self.marker_path(failing).exists())
        # One sync for the healthy blob directory and one for the markers.
        self.assertEqual(self.ops.counts.get("fsync", 0), 2)
        self.assertTrue((self.root / format_generation_name(1)).exists())
        self.assertEqual(self.ops.live_fds, live_before)

    def test_mode_check_unexpected_exception_propagates_without_leak(self) -> None:
        blob = identity_alg("sha256", b"mode-check")
        inventory = self._two_generation_state([blob], [])
        boom = RuntimeError("mode-check defect")
        self.ops.reset()
        # Occurrence 1 is the mode-check fstat that follows adoption.
        self.ops.fstat_raises_on["sha256"] = {1: boom}
        live_before = set(self.ops.live_fds)
        with self.assertRaises(RuntimeError) as ctx:
            cleanup_superseded(self.ops, self.storage, inventory, lock=self.lock)
        self.assertIs(ctx.exception, boom)
        self.assertEqual(self.ops.live_fds, live_before)
        self.assertTrue(self.blob_path(blob).exists())
        self.assertTrue((self.root / format_generation_name(1)).exists())

    def test_mode_check_interruption_propagates_without_leak(self) -> None:
        blob = identity_alg("sha256", b"mode-check-interrupt")
        inventory = self._two_generation_state([blob], [])
        boom = KeyboardInterrupt()
        self.ops.reset()
        self.ops.fstat_raises_on["sha256"] = {1: boom}
        live_before = set(self.ops.live_fds)
        with self.assertRaises(KeyboardInterrupt) as ctx:
            cleanup_superseded(self.ops, self.storage, inventory, lock=self.lock)
        self.assertIs(ctx.exception, boom)
        self.assertEqual(self.ops.live_fds, live_before)
        self.assertTrue(self.blob_path(blob).exists())

    def test_mode_check_primary_exception_attaches_close_failure(self) -> None:
        blob = identity_alg("sha256", b"mode-check-close")
        inventory = self._two_generation_state([blob], [])
        primary = RuntimeError("mode-check defect")
        close_error = OSError(errno.EIO, "cannot close algorithm directory")
        self.ops.reset()
        self.ops.fstat_raises_on["sha256"] = {1: primary}
        self.ops.failures["close"] = close_error
        with self.assertRaises(RuntimeError) as ctx:
            cleanup_superseded(self.ops, self.storage, inventory, lock=self.lock)
        self.ops.failures.pop("close", None)
        self.assertIs(ctx.exception, primary)
        self.assertIn(
            close_error, getattr(primary, "_transaction_secondary", [])
        )
        self.assertTrue(self.blob_path(blob).exists())


class UnsafeLeafTests(_CleanupTestCase):
    """Unsafe superseded leaves fail closed without bypassing owned authority."""

    def _two_generation_state(self, previous_blobs, current_blobs):
        for blob in previous_blobs:
            self.add_blob(blob)
        for blob in current_blobs:
            self.add_blob(blob)
        self.write_generation(1, previous_blobs)
        self.write_generation(2, current_blobs)
        return self.inventory(previous_blobs, current_blobs)

    def _replace_blob_with_symlink(self, blob: DigestIdentity) -> Path:
        target = self.root / f"target-{blob.hex_digest()}.bin"
        target.write_bytes(b"symlink target")
        os.unlink(self.blob_path(blob))
        self.blob_path(blob).symlink_to(target)
        return target

    def _replace_blob_with_fifo(self, blob: DigestIdentity) -> None:
        os.unlink(self.blob_path(blob))
        os.mkfifo(self.blob_path(blob))

    def _replace_marker_with_symlink(self, blob: DigestIdentity) -> Path:
        target = self.root / f"marker-target-{blob.hex_digest()}.bin"
        target.write_bytes(b"marker symlink target")
        self.marker_path(blob).symlink_to(target)
        return target

    def _replace_marker_with_fifo(self, blob: DigestIdentity) -> None:
        os.mkfifo(self.marker_path(blob))

    def _assert_failed_closed(self):
        previous = self.root / format_generation_name(1)
        self.assertTrue(previous.exists())

    def test_symlinked_blob_is_preserved_with_its_target(self) -> None:
        blob = identity_alg("sha256", b"blob-symlink")
        inventory = self._two_generation_state([blob], [])
        self.add_marker(blob)
        target = self._replace_blob_with_symlink(blob)
        self.ops.reset()
        with self.assertRaises(BuildCleanupError) as ctx:
            cleanup_superseded(self.ops, self.storage, inventory, lock=self.lock)
        self.assertEqual(len(ctx.exception.failures), 1)
        self.assertTrue(self.blob_path(blob).is_symlink())
        self.assertTrue(target.exists())
        self.assertEqual(target.read_bytes(), b"symlink target")
        self.assertTrue(self.marker_path(blob).exists())
        self.assertEqual(self.ops.counts.get("unlinkat", 0), 0)
        self._assert_failed_closed()

    def test_non_regular_blob_is_preserved(self) -> None:
        blob = identity_alg("sha256", b"blob-fifo")
        inventory = self._two_generation_state([blob], [])
        self.add_marker(blob)
        self._replace_blob_with_fifo(blob)
        with self.assertRaises(BuildCleanupError) as ctx:
            cleanup_superseded(self.ops, self.storage, inventory, lock=self.lock)
        self.assertEqual(len(ctx.exception.failures), 1)
        self.assertTrue(stat.S_ISFIFO(os.stat(self.blob_path(blob)).st_mode))
        self.assertTrue(self.marker_path(blob).exists())
        self._assert_failed_closed()

    def test_foreign_owned_blob_is_preserved(self) -> None:
        blob = identity_alg("sha256", b"blob-foreign")
        inventory = self._two_generation_state([blob], [])
        self.add_marker(blob)
        self.ops.fstat_by_name[blob_name(blob)] = lambda info: _with_uid(
            info, os.geteuid() + 1
        )
        with self.assertRaises(BuildCleanupError) as ctx:
            cleanup_superseded(self.ops, self.storage, inventory, lock=self.lock)
        self.assertEqual(len(ctx.exception.failures), 1)
        self.assertTrue(self.blob_path(blob).exists())
        self.assertEqual(os.stat(self.blob_path(blob)).st_uid, os.geteuid())
        self.assertTrue(self.marker_path(blob).exists())
        self._assert_failed_closed()

    def test_multiply_linked_blob_is_preserved(self) -> None:
        blob = identity_alg("sha256", b"blob-linked")
        inventory = self._two_generation_state([blob], [])
        self.add_marker(blob)
        second_link = self.root / "blob-hard-link"
        os.link(self.blob_path(blob), second_link)
        with self.assertRaises(BuildCleanupError) as ctx:
            cleanup_superseded(self.ops, self.storage, inventory, lock=self.lock)
        self.assertEqual(len(ctx.exception.failures), 1)
        self.assertTrue(self.blob_path(blob).exists())
        self.assertTrue(second_link.exists())
        self.assertEqual(os.stat(self.blob_path(blob)).st_nlink, 2)
        self.assertTrue(self.marker_path(blob).exists())
        self._assert_failed_closed()

    def test_forbidden_mode_blob_is_preserved(self) -> None:
        blob = identity_alg("sha256", b"blob-mode")
        inventory = self._two_generation_state([blob], [])
        self.add_marker(blob)
        os.chmod(self.blob_path(blob), 0o600)
        with self.assertRaises(BuildCleanupError) as ctx:
            cleanup_superseded(self.ops, self.storage, inventory, lock=self.lock)
        self.assertEqual(len(ctx.exception.failures), 1)
        self.assertTrue(self.blob_path(blob).exists())
        self.assertEqual(
            stat.S_IMODE(os.stat(self.blob_path(blob)).st_mode), 0o600
        )
        self.assertTrue(self.marker_path(blob).exists())
        self._assert_failed_closed()

    def test_symlinked_marker_is_preserved_with_its_target(self) -> None:
        blob = identity_alg("sha256", b"marker-symlink")
        inventory = self._two_generation_state([blob], [])
        target = self._replace_marker_with_symlink(blob)
        with self.assertRaises(BuildCleanupError) as ctx:
            cleanup_superseded(self.ops, self.storage, inventory, lock=self.lock)
        self.assertEqual(len(ctx.exception.failures), 1)
        # The blob was removable, so it is gone; the unsafe marker and its
        # target survive and the predecessor is retained.
        self.assertFalse(self.blob_path(blob).exists())
        self.assertTrue(self.marker_path(blob).is_symlink())
        self.assertTrue(target.exists())
        self.assertEqual(target.read_bytes(), b"marker symlink target")
        self._assert_failed_closed()

    def test_non_regular_marker_is_preserved(self) -> None:
        blob = identity_alg("sha256", b"marker-fifo")
        inventory = self._two_generation_state([blob], [])
        self._replace_marker_with_fifo(blob)
        with self.assertRaises(BuildCleanupError) as ctx:
            cleanup_superseded(self.ops, self.storage, inventory, lock=self.lock)
        self.assertEqual(len(ctx.exception.failures), 1)
        self.assertTrue(stat.S_ISFIFO(os.stat(self.marker_path(blob)).st_mode))
        self._assert_failed_closed()

    def test_foreign_owned_marker_is_preserved(self) -> None:
        blob = identity_alg("sha256", b"marker-foreign")
        inventory = self._two_generation_state([blob], [])
        self.add_marker(blob)
        self.ops.fstat_by_name[marker_name(blob)] = lambda info: _with_uid(
            info, os.geteuid() + 1
        )
        with self.assertRaises(BuildCleanupError) as ctx:
            cleanup_superseded(self.ops, self.storage, inventory, lock=self.lock)
        self.assertEqual(len(ctx.exception.failures), 1)
        self.assertTrue(self.marker_path(blob).exists())
        self.assertEqual(os.stat(self.marker_path(blob)).st_uid, os.geteuid())
        self._assert_failed_closed()

    def test_multiply_linked_marker_is_preserved(self) -> None:
        blob = identity_alg("sha256", b"marker-linked")
        inventory = self._two_generation_state([blob], [])
        self.add_marker(blob)
        second_link = self.root / "marker-hard-link"
        os.link(self.marker_path(blob), second_link)
        with self.assertRaises(BuildCleanupError) as ctx:
            cleanup_superseded(self.ops, self.storage, inventory, lock=self.lock)
        self.assertEqual(len(ctx.exception.failures), 1)
        self.assertTrue(self.marker_path(blob).exists())
        self.assertTrue(second_link.exists())
        self.assertEqual(os.stat(self.marker_path(blob)).st_nlink, 2)
        self._assert_failed_closed()

    def test_forbidden_mode_marker_is_preserved(self) -> None:
        blob = identity_alg("sha256", b"marker-mode")
        inventory = self._two_generation_state([blob], [])
        self.add_marker(blob)
        os.chmod(self.marker_path(blob), 0o666)
        with self.assertRaises(BuildCleanupError) as ctx:
            cleanup_superseded(self.ops, self.storage, inventory, lock=self.lock)
        self.assertEqual(len(ctx.exception.failures), 1)
        self.assertTrue(self.marker_path(blob).exists())
        self.assertEqual(
            stat.S_IMODE(os.stat(self.marker_path(blob)).st_mode), 0o666
        )
        self._assert_failed_closed()

    def test_unsafe_leaf_does_not_block_other_candidates_or_spawn_fsyncs(self) -> None:
        unsafe = identity_alg("sha256", b"unsafe")
        safe_one = identity_alg("sha256", b"safe-one")
        safe_two = identity_alg("sha256", b"safe-two")
        inventory = self._two_generation_state([unsafe, safe_one, safe_two], [])
        for blob in (unsafe, safe_one, safe_two):
            self.add_marker(blob)
        self._replace_blob_with_symlink(unsafe)
        self.ops.reset()
        with self.assertRaises(BuildCleanupError) as ctx:
            cleanup_superseded(self.ops, self.storage, inventory, lock=self.lock)
        self.assertEqual(len(ctx.exception.failures), 1)
        self.assertFalse(self.blob_path(safe_one).exists())
        self.assertFalse(self.blob_path(safe_two).exists())
        self.assertFalse(self.marker_path(safe_one).exists())
        self.assertFalse(self.marker_path(safe_two).exists())
        self.assertTrue(self.blob_path(unsafe).is_symlink())
        self.assertTrue(self.marker_path(unsafe).exists())
        self._assert_failed_closed()
        # One fsync for the single affected blob directory and one for the
        # shared marker directory: batching is preserved, not per candidate.
        self.assertEqual(self.ops.counts.get("fsync"), 2)


# ═══════════════════════════════════════════════════════════════════════
# unexpected validation failures
# ═══════════════════════════════════════════════════════════════════════


class UnexpectedValidationFailureTests(_CleanupTestCase):
    """Unexpected exceptions must not be masked as recoverable diagnostics."""

    def _two_generation_state(self, previous_blobs, current_blobs):
        for blob in previous_blobs:
            self.add_blob(blob)
        for blob in current_blobs:
            self.add_blob(blob)
        self.write_generation(1, previous_blobs)
        self.write_generation(2, current_blobs)
        return self.inventory(previous_blobs, current_blobs)

    def test_reconciliation_propagates_unexpected_validation_error(self) -> None:
        first = identity(b"first-marker")
        second = identity(b"second-marker")
        ordered = sorted((first, second), key=canonical_blob_key)
        for blob in ordered:
            self.add_blob(blob)
            self.add_marker(blob)
        current = BuildGeneration(
            2,
            format_generation_name(2),
            BuildManifest.from_blobs([first, second]),
        )
        boom = RuntimeError("unexpected validation defect")
        self.ops.reset()
        self.ops.fstat_raises_by_name[marker_name(ordered[0])] = boom
        live_before = set(self.ops.live_fds)
        with self.assertRaises(RuntimeError) as ctx:
            reconcile_authoritative_markers(
                self.ops, self.storage, current, lock=self.lock
            )
        self.assertIs(ctx.exception, boom)
        # Neither the failing marker nor the later one is removed, and the
        # marker directory is never synchronized.
        self.assertTrue(self.marker_path(ordered[0]).exists())
        self.assertTrue(self.marker_path(ordered[1]).exists())
        self.assertEqual(self.ops.counts.get("unlinkat", 0), 0)
        self.assertEqual(self.ops.counts.get("fsync", 0), 0)
        # The retained descriptor is released; no descriptor leaks.
        self.assertEqual(self.ops.live_fds, live_before)

    def test_cleanup_propagates_unexpected_validation_error_and_stops(self) -> None:
        first = identity_alg("sha256", b"candidate-first")
        second = identity_alg("sha256", b"candidate-second")
        ordered = sorted((first, second), key=canonical_blob_key)
        inventory = self._two_generation_state(list(ordered), [])
        for blob in ordered:
            self.add_marker(blob)
        boom = RuntimeError("unexpected cleanup defect")
        self.ops.reset()
        self.ops.fstat_raises_by_name[blob_name(ordered[0])] = boom
        live_before = set(self.ops.live_fds)
        with self.assertRaises(RuntimeError) as ctx:
            cleanup_superseded(self.ops, self.storage, inventory, lock=self.lock)
        self.assertIs(ctx.exception, boom)
        # No later candidate is deleted and no directory is synchronized.
        for blob in ordered:
            self.assertTrue(self.blob_path(blob).exists())
            self.assertTrue(self.marker_path(blob).exists())
        self.assertEqual(self.ops.counts.get("unlinkat", 0), 0)
        self.assertEqual(self.ops.counts.get("fsync", 0), 0)
        # The predecessor manifest is retained and every descriptor is closed.
        self.assertTrue((self.root / format_generation_name(1)).exists())
        self.assertEqual(self.ops.live_fds, live_before)


# ═══════════════════════════════════════════════════════════════════════
# 4.4 / 4.7 restart recovery
# ═══════════════════════════════════════════════════════════════════════


class RecoveryTests(_CleanupTestCase):
    def _state(self, previous_blobs, current_blobs):
        for blob in previous_blobs + current_blobs:
            self.add_blob(blob)
        self.write_generation(1, previous_blobs)
        self.write_generation(2, current_blobs)

    def test_recovery_reconciles_then_cleans_up_leaving_one_generation(self) -> None:
        shared = identity(b"shared")
        stale = identity(b"stale")
        previous_only = identity(b"previous")
        self._state([previous_only, shared], [shared, stale])
        self.add_marker(shared)
        self.add_marker(stale)
        self.add_marker(previous_only)
        inventory = recover_generations(self.ops, self.storage, lock=self.lock)
        self.assertIs(inventory.state, GenerationState.RECOVERABLE)
        self.assertFalse(self.marker_path(shared).exists())
        self.assertFalse(self.marker_path(stale).exists())
        self.assertFalse(self.marker_path(previous_only).exists())
        self.assertFalse(self.blob_path(previous_only).exists())
        self.assertFalse((self.root / format_generation_name(1)).exists())
        self.assertTrue((self.root / format_generation_name(2)).exists())

    def test_recovery_fsyncs_marker_directory_when_all_markers_absent(self) -> None:
        previous_only = identity(b"previous")
        current_only = identity(b"current")
        self._state([previous_only], [current_only])
        self.ops.reset()
        recover_generations(self.ops, self.storage, lock=self.lock)
        markers_fsyncs = [
            args for args in self.ops.arg_pairs("fsync") if args[0] == self.markers.fd
        ]
        # One for authoritative-marker reconciliation and one for the
        # superseded marker batch.
        self.assertEqual(len(markers_fsyncs), 2)

    def test_recovery_is_idempotent_when_a_batch_is_already_absent(self) -> None:
        previous_only = identity(b"previous")
        current_only = identity(b"current")
        # The candidate was already unlinked but its directory was never
        # synchronized; only the predecessor manifest remains.
        (self.root / "blobs" / "sha256").mkdir(mode=ALGORITHM_DIRECTORY_MODE)
        os.chmod(self.root / "blobs" / "sha256", ALGORITHM_DIRECTORY_MODE)
        self.write_generation(1, [previous_only])
        self.write_generation(2, [current_only])
        self.ops.reset()
        recover_generations(self.ops, self.storage, lock=self.lock)
        self.assertFalse((self.root / format_generation_name(1)).exists())
        # One fresh fsync of the already-clean algorithm directory is required.
        algorithm_fsyncs = [
            args
            for args in self.ops.arg_pairs("fsync")
            if args[0] not in (self.markers.fd, self.generations.fd)
        ]
        self.assertEqual(len(algorithm_fsyncs), 1)

    def test_recovery_failure_preserves_the_predecessor(self) -> None:
        previous_only = identity(b"previous")
        self._state([previous_only], [identity(b"current")])
        self.fail_unlink_for(
            blob_name(previous_only), OSError(errno.EIO, "injected failure")
        )
        with self.assertRaises(BuildCleanupError):
            recover_generations(self.ops, self.storage, lock=self.lock)
        self.assertTrue(self.blob_path(previous_only).exists())
        self.assertTrue((self.root / format_generation_name(1)).exists())

    def test_recovery_blocks_cleanup_when_marker_reconciliation_fails(self) -> None:
        previous_only = identity(b"previous")
        current = identity(b"current")
        self._state([previous_only], [current])
        self.add_marker(current)
        self.fail_unlink_for(
            marker_name(current), OSError(errno.EIO, "injected reconciliation failure")
        )
        with self.assertRaises(BuildCleanupError):
            recover_generations(self.ops, self.storage, lock=self.lock)
        # The authoritative generation remains committed; only the failed
        # reconciliation and its blocking of superseded cleanup are visible.
        self.assertTrue((self.root / format_generation_name(2)).exists())
        # Authoritative reconciliation failed, so superseded cleanup never ran.
        self.assertTrue(self.blob_path(previous_only).exists())
        self.assertTrue((self.root / format_generation_name(1)).exists())

    def test_recovery_requires_generation_sync_before_accepting_one_generation(self) -> None:
        current = identity(b"current")
        self.add_blob(current)
        self.write_generation(1, [current])
        self.add_marker(current)
        self.ops.hooks["fsync"] = lambda fd: (
            (_ for _ in ()).throw(OSError(errno.EIO, "injected discovery fsync"))
            if fd == self.generations.fd
            else None
        )
        with self.assertRaises(Exception):
            recover_generations(self.ops, self.storage, lock=self.lock)
        # No reconciliation happened before the generation directory synced.
        self.assertTrue(self.marker_path(current).exists())

    def test_recovery_completes_a_two_generation_state(self) -> None:
        previous_only = identity(b"previous")
        current_only = identity(b"current")
        self._state([previous_only], [current_only])
        recover_generations(self.ops, self.storage, lock=self.lock)
        self.assertFalse((self.root / format_generation_name(1)).exists())
        self.assertTrue((self.root / format_generation_name(2)).exists())

    def test_recovery_blocks_cleanup_when_marker_fsync_fails(self) -> None:
        previous_only = identity(b"previous")
        current = identity(b"current")
        self._state([previous_only], [current])
        self.add_marker(current)
        self.ops.hooks["fsync"] = lambda fd: (
            (_ for _ in ()).throw(OSError(errno.EIO, "injected marker fsync"))
            if fd == self.markers.fd
            else None
        )
        with self.assertRaises(BuildCleanupError):
            recover_generations(self.ops, self.storage, lock=self.lock)
        self.assertTrue(self.blob_path(previous_only).exists())
        self.assertTrue((self.root / format_generation_name(1)).exists())

    def test_restart_after_durable_removal_observes_one_stable_generation(self) -> None:
        previous_only = identity(b"previous")
        current_only = identity(b"current")
        self._state([previous_only], [current_only])
        recover_generations(self.ops, self.storage, lock=self.lock)
        second = recover_generations(self.ops, self.storage, lock=self.lock)
        self.assertIs(second.state, GenerationState.STABLE)
        self.assertTrue(self.blob_path(current_only).exists())
        self.assertFalse((self.root / format_generation_name(1)).exists())

    def test_restart_with_two_generations_completes_idempotent_recovery(self) -> None:
        previous_only = identity(b"previous")
        current_only = identity(b"current")
        self._state([previous_only], [current_only])
        inventory = recover_generations(self.ops, self.storage, lock=self.lock)
        self.assertIs(inventory.state, GenerationState.RECOVERABLE)
        self.assertFalse((self.root / format_generation_name(1)).exists())
        self.assertTrue((self.root / format_generation_name(2)).exists())
        self.assertTrue(self.blob_path(current_only).exists())

    def test_one_generation_state_after_failed_generation_fsync_preserves_current(self) -> None:
        current_only = identity(b"current")
        self.add_blob(current_only)
        self.write_generation(1, [current_only])
        # The predecessor was unlinked but its directory fsync did not
        # complete; a restart observes one visible generation.
        self.ops.hooks["fsync"] = lambda fd: (
            (_ for _ in ()).throw(OSError(errno.EIO, "injected discovery fsync"))
            if fd == self.generations.fd
            else None
        )
        with self.assertRaises(BuildGenerationError):
            recover_generations(self.ops, self.storage, lock=self.lock)
        self.assertTrue(self.blob_path(current_only).exists())
        self.ops.hooks.clear()
        inventory = recover_generations(self.ops, self.storage, lock=self.lock)
        self.assertIs(inventory.state, GenerationState.STABLE)
        self.assertTrue(self.blob_path(current_only).exists())

    def test_empty_state_is_a_successful_noop(self) -> None:
        inventory = recover_generations(self.ops, self.storage, lock=self.lock)
        self.assertIs(inventory.state, GenerationState.EMPTY)


# ═══════════════════════════════════════════════════════════════════════
# 4.4 interruptions
# ═══════════════════════════════════════════════════════════════════════


class CleanupInterruptionTests(_CleanupTestCase):
    def _state(self, previous_blobs, current_blobs):
        for blob in previous_blobs + current_blobs:
            self.add_blob(blob)
        self.write_generation(1, previous_blobs)
        self.write_generation(2, current_blobs)

    def test_interruption_during_candidate_unlink_leaves_recoverable_state(self) -> None:
        previous_only = identity(b"previous")
        self._state([previous_only], [identity(b"current")])
        cancellation = _Cancellation()
        self.fail_unlink_for(blob_name(previous_only), cancellation)
        with self.assertRaises(_Cancellation) as ctx:
            recover_generations(self.ops, self.storage, lock=self.lock)
        self.assertIs(ctx.exception, cancellation)
        self.assertTrue((self.root / format_generation_name(1)).exists())
        # A clean retry completes the recovery.
        self.ops.hooks.clear()
        recover_generations(self.ops, self.storage, lock=self.lock)
        self.assertFalse((self.root / format_generation_name(1)).exists())

    def test_interruption_during_predecessor_unlink_leaves_recoverable_state(self) -> None:
        previous_only = identity(b"previous")
        self._state([previous_only], [identity(b"current")])
        cancellation = _Cancellation()
        self.fail_unlink_for(format_generation_name(1), cancellation)
        with self.assertRaises(_Cancellation) as ctx:
            recover_generations(self.ops, self.storage, lock=self.lock)
        self.assertIs(ctx.exception, cancellation)
        self.assertTrue((self.root / format_generation_name(1)).exists())
        self.assertFalse(self.blob_path(previous_only).exists())
        self.ops.hooks.clear()
        recover_generations(self.ops, self.storage, lock=self.lock)
        self.assertFalse((self.root / format_generation_name(1)).exists())

    def test_interruption_during_marker_unlink_leaves_predecessor(self) -> None:
        current = identity(b"current")
        self._state([identity(b"previous")], [current])
        self.add_marker(current)
        cancellation = _Cancellation()
        self.fail_unlink_for(marker_name(current), cancellation)
        with self.assertRaises(_Cancellation) as ctx:
            recover_generations(self.ops, self.storage, lock=self.lock)
        self.assertIs(ctx.exception, cancellation)
        self.assertTrue((self.root / format_generation_name(1)).exists())

    def test_interruption_during_marker_fsync_leaves_predecessor(self) -> None:
        current = identity(b"current")
        self._state([identity(b"previous")], [current])
        cancellation = _Cancellation()
        self.ops.hooks["fsync"] = lambda fd: (
            (_ for _ in ()).throw(cancellation) if fd == self.markers.fd else None
        )
        with self.assertRaises(_Cancellation) as ctx:
            recover_generations(self.ops, self.storage, lock=self.lock)
        self.assertIs(ctx.exception, cancellation)
        self.assertTrue((self.root / format_generation_name(1)).exists())

    def test_interruption_during_candidate_directory_fsync_leaves_predecessor(self) -> None:
        previous_only = identity_alg("sha256", b"previous")
        self._state([previous_only], [identity(b"current")])
        cancellation = _Cancellation()
        # Fsync call 1 is the generation-directory discovery sync; call 2 is
        # authoritative-marker reconciliation; call 3 is the affected
        # candidate blob directory.
        self.ops.failures["fsync"] = lambda count: (
            cancellation if count == 3 else None
        )
        with self.assertRaises(_Cancellation) as ctx:
            recover_generations(self.ops, self.storage, lock=self.lock)
        self.assertIs(ctx.exception, cancellation)
        self.assertTrue((self.root / format_generation_name(1)).exists())
        self.ops.failures.clear()
        recover_generations(self.ops, self.storage, lock=self.lock)
        self.assertFalse((self.root / format_generation_name(1)).exists())

    def test_interruption_during_generation_fsync_propagates(self) -> None:
        previous_only = identity(b"previous")
        self._state([previous_only], [identity(b"current")])
        cancellation = _Cancellation()
        unlinked = {"done": False}
        original = self.ops.hooks.get("unlinkat")

        def unlink_hook(dir_fd, name):
            if original is not None:
                original(dir_fd, name)
            if name == format_generation_name(1):
                unlinked["done"] = True

        self.ops.hooks["unlinkat"] = unlink_hook

        def fsync_hook(fd):
            if unlinked["done"] and fd == self.generations.fd:
                raise cancellation

        self.ops.hooks["fsync"] = fsync_hook
        with self.assertRaises(_Cancellation) as ctx:
            recover_generations(self.ops, self.storage, lock=self.lock)
        self.assertIs(ctx.exception, cancellation)
        self.assertFalse((self.root / format_generation_name(1)).exists())
        self.assertTrue((self.root / format_generation_name(2)).exists())


if __name__ == "__main__":
    unittest.main()
