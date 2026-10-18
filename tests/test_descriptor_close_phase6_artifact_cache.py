"""Phase 6 (tasks 6.5–6.7) — artifact-cache descriptor ownership.

RED before migration: the descriptor-relative traversal, inspection,
mutation, temporary-directory, and identity-lock helpers in
``docker/versioning/artifact_cache.py`` released raw ``os.open`` descriptors
with a direct ``os.close`` (and relied on ``os.fdopen(fd, closefd=False)``
around a raw fd).  Precedence was preserved, but there was no shared terminal
owner, so the migration contract (terminal-before-close + at-most-once) is the
RED part.  The capability-based ``release_capability`` / ``release_directory``
helpers already delegate to shared capabilities and stay passing.
"""

from __future__ import annotations

import base64
import errno
import hashlib
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from docker.versioning import artifact_cache
from docker.versioning.artifact_cache import (
    FileIdentityLock,
    LocalCacheFilesystem,
    LocalTemporaryDirectory,
)
from docker.versioning.digest_identity import DigestIdentity
from tests.descriptor_ownership_support import (
    file_object_faults,
    recording,
    secondary_diagnostics,
)


def _blob_payload(payload: bytes = b"payload") -> tuple[str, bytes]:
    raw = hashlib.sha512(payload).digest()
    return "sha512-" + base64.b64encode(raw).decode(), payload


class _ArtifactCacheCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="phase6-artifact-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.filesystem = LocalCacheFilesystem()

    def _write_blob(self, payload: bytes = b"payload", *, secure: bool = True):
        integrity, data = _blob_payload(payload)
        identity = DigestIdentity.from_sri(integrity)
        algorithm_dir = self.root / identity.algorithm
        algorithm_dir.mkdir(parents=True, exist_ok=True)
        blob = algorithm_dir / f"{identity.runtime_safe_digest()}.tgz"
        blob.write_bytes(data)
        if secure:
            os.chmod(blob, 0o444)
        return integrity, blob


class ReadonlyInspectionOwnershipTests(_ArtifactCacheCase):
    def test_inspect_verified_blob_readonly_releases_all_three_owners(self):
        integrity, blob = self._write_blob()
        with recording(artifact_cache) as rec:
            self.assertTrue(
                artifact_cache.inspect_verified_blob_readonly(
                    integrity, cache_root=str(self.root)
                )
            )
        rec.assert_terminal_and_at_most_once(self)
        self.assertEqual(len(rec.owners), 3)  # root + algorithm + blob leaf
        self.assertTrue(blob.exists())

    def test_inspect_verified_blob_readonly_miss_still_releases_owners(self):
        integrity, _ = self._write_blob()
        missing = (
            "sha512-" + base64.b64encode(hashlib.sha512(b"other").digest()).decode()
        )
        with recording(artifact_cache) as rec:
            self.assertFalse(
                artifact_cache.inspect_verified_blob_readonly(
                    missing, cache_root=str(self.root)
                )
            )
        rec.assert_terminal_and_at_most_once(self)


class DirectoryChainOwnershipTests(_ArtifactCacheCase):
    def test_open_directory_chain_hands_off_a_live_detached_fd(self):
        target = self.root / "a" / "b"
        with recording(artifact_cache) as rec:
            fd = artifact_cache._open_directory_chain(str(target), create=True)
        try:
            self.assertTrue(stat.S_ISDIR(os.fstat(fd).st_mode))
        finally:
            os.close(fd)
        rec.assert_terminal_and_at_most_once(self)
        # every intermediate parent owner was released; the leaf is detached
        self.assertGreaterEqual(len(rec.owners), 3)

    def test_open_directory_chain_missing_without_create_releases_root(self):
        target = self.root / "absent" / "leaf"
        with recording(artifact_cache) as rec:
            with self.assertRaises(FileNotFoundError):
                artifact_cache._open_directory_chain(str(target), create=False)
        rec.assert_terminal_and_at_most_once(self)

    def test_ensure_dir_private_releases_parent_and_child(self):
        target = self.root / "blobs"
        target.mkdir()
        os.chmod(target, 0o755)
        with recording(artifact_cache) as rec:
            artifact_cache._ensure_dir_private(str(target))
        rec.assert_terminal_and_at_most_once(self)
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o700)
        self.assertGreaterEqual(len(rec.owners), 2)

    def test_remove_tree_at_releases_owner(self):
        tree = self.root / "tree"
        (tree / "sub").mkdir(parents=True)
        (tree / "sub" / "file").write_bytes(b"x")
        parent_fd = os.open(str(self.root), os.O_RDONLY | os.O_DIRECTORY)
        try:
            with recording(artifact_cache) as rec:
                artifact_cache._remove_tree_at(parent_fd, "tree")
        finally:
            os.close(parent_fd)
        rec.assert_terminal_and_at_most_once(self)
        self.assertFalse(tree.exists())


class LocalFilesystemInspectionOwnershipTests(_ArtifactCacheCase):
    def test_stat_blob_releases_parent_owner(self):
        integrity, blob = self._write_blob()
        DigestIdentity.from_sri(integrity)
        with recording(artifact_cache) as rec:
            status = self.filesystem.stat_blob(str(blob))
        rec.assert_terminal_and_at_most_once(self)
        self.assertTrue(status.is_regular_file)

    def test_read_bytes_releases_parent_and_child_owners(self):
        integrity, blob = self._write_blob()
        with recording(artifact_cache) as rec:
            self.assertEqual(self.filesystem.read_bytes(str(blob)), b"payload")
        rec.assert_terminal_and_at_most_once(self)
        self.assertGreaterEqual(len(rec.owners), 2)

    def test_inspect_and_digest_releases_parent_and_child_owners(self):
        integrity, blob = self._write_blob()
        identity = DigestIdentity.from_sri(integrity)
        with recording(artifact_cache) as rec:
            inspection = self.filesystem.inspect_and_digest(
                str(blob), identity.algorithm
            )
        rec.assert_terminal_and_at_most_once(self)
        self.assertEqual(inspection.digest, identity.base64_digest())


class MutationOwnershipTests(_ArtifactCacheCase):
    def test_ensure_secure_dir_releases_owner(self):
        target = self.root / "locks"
        with recording(artifact_cache) as rec:
            self.filesystem.ensure_secure_dir(str(target))
        rec.assert_terminal_and_at_most_once(self)
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o700)

    def test_create_temp_releases_parent_and_child_owners(self):
        self.root.mkdir(exist_ok=True)
        with recording(artifact_cache) as rec:
            self.filesystem.create_temp(str(self.root / "temp"))
        rec.assert_terminal_and_at_most_once(self)
        # both the parent and the newly created child are shared owners
        self.assertGreaterEqual(len(rec.owners), 2)
        self.assertTrue((self.root / "temp").exists())

    def test_append_temp_releases_parent_and_child_owners(self):
        path = self.root / "temp"
        path.write_bytes(b"")
        with recording(artifact_cache) as rec:
            self.filesystem.append_temp(str(path), b"chunk")
        rec.assert_terminal_and_at_most_once(self)
        self.assertEqual(path.read_bytes(), b"chunk")

    def test_finalize_temp_releases_parent_and_child_owners(self):
        path = self.root / "temp"
        path.write_bytes(b"")
        with recording(artifact_cache) as rec:
            self.filesystem.finalize_temp(str(path), 0o600)
        rec.assert_terminal_and_at_most_once(self)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_cleanup_temp_releases_parent_owner(self):
        tree = self.root / "tmpdir"
        tree.mkdir()
        (tree / "f").write_bytes(b"x")
        with recording(artifact_cache) as rec:
            self.filesystem.cleanup_temp(str(tree))
        rec.assert_terminal_and_at_most_once(self)
        self.assertFalse(tree.exists())

    def test_quarantine_or_remove_releases_parent_owner(self):
        victim = self.root / "victim"
        victim.write_bytes(b"x")
        with recording(artifact_cache) as rec:
            self.filesystem.quarantine_or_remove(str(victim))
        rec.assert_terminal_and_at_most_once(self)
        self.assertFalse(victim.exists())

    def test_get_permissions_releases_parent_owner(self):
        path = self.root / "perm"
        path.write_bytes(b"x")
        with recording(artifact_cache) as rec:
            self.filesystem.get_permissions(str(path))
        rec.assert_terminal_and_at_most_once(self)

    def test_set_permissions_releases_parent_and_child_owners(self):
        path = self.root / "perm"
        path.write_bytes(b"x")
        with recording(artifact_cache) as rec:
            self.filesystem.set_permissions(str(path), 0o640)
        rec.assert_terminal_and_at_most_once(self)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o640)

    def test_atomic_publish_releases_source_and_target_owners(self):
        temp = self.root / "temp"
        temp.write_bytes(b"payload")
        final = self.root / "final"
        with recording(artifact_cache) as rec:
            self.filesystem.atomic_publish(str(temp), str(final))
        rec.assert_terminal_and_at_most_once(self)
        self.assertEqual(final.read_bytes(), b"payload")

    def _fail_target_parent_open(self):
        """Return an ``_open_parent`` double that fails on the target call.

        The source parent open (first call) succeeds, so the source fd is
        already adopted; the target parent open (second call) then fails.
        """
        real_open_parent = artifact_cache._open_parent
        calls = {"count": 0}

        def flaky_open_parent(path, **kwargs):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError(errno.EIO, "injected target parent open failure")
            return real_open_parent(path, **kwargs)

        return flaky_open_parent

    def test_atomic_publish_target_parent_failure_releases_source_once(self):
        temp = self.root / "temp"
        temp.write_bytes(b"payload")
        final = self.root / "final"
        with recording(artifact_cache) as rec:
            with mock.patch.object(
                artifact_cache,
                "_open_parent",
                side_effect=self._fail_target_parent_open(),
            ):
                with self.assertRaises(OSError) as caught:
                    self.filesystem.atomic_publish(str(temp), str(final))
        # The target-open failure stays primary, exactly one source owner was
        # adopted, and it received a single terminal release attempt.
        self.assertIn("injected target parent open failure", str(caught.exception))
        source_label = f"publish source {str(temp)!r}"
        source_owners = [owner for owner in rec.owners if owner.label == source_label]
        self.assertEqual(len(source_owners), 1)
        source_closes = [
            (released, owner)
            for _, released, owner in rec.close_calls
            if owner is source_owners[0]
        ]
        self.assertEqual(len(source_closes), 1)
        self.assertTrue(source_closes[0][0])
        # The target descriptor was never adopted (its parent open failed).
        self.assertEqual(
            [o for o in rec.owners if o.label == f"publish target {str(final)!r}"], []
        )
        # No publication occurred and the temporary entry was left intact.
        self.assertFalse(final.exists())
        self.assertTrue(temp.exists())

    def test_atomic_publish_target_parent_failure_keeps_close_failure_secondary(self):
        temp = self.root / "temp"
        temp.write_bytes(b"payload")
        final = self.root / "final"
        close_fault = OSError(errno.EIO, "injected source close failure")
        source_label = f"publish source {str(temp)!r}"

        def fault_source_close(fd, owner):
            return close_fault if owner.label == source_label else None

        with recording(artifact_cache, close_fault=fault_source_close) as rec:
            with mock.patch.object(
                artifact_cache,
                "_open_parent",
                side_effect=self._fail_target_parent_open(),
            ):
                with self.assertRaises(OSError) as caught:
                    self.filesystem.atomic_publish(str(temp), str(final))
        self.assertIn("injected target parent open failure", str(caught.exception))
        self.assertIn(close_fault, secondary_diagnostics(caught.exception))
        source_owners = [owner for owner in rec.owners if owner.label == source_label]
        self.assertEqual(len(source_owners), 1)
        source_closes = [
            (released, owner)
            for _, released, owner in rec.close_calls
            if owner is source_owners[0]
        ]
        self.assertEqual(len(source_closes), 1)
        self.assertTrue(source_closes[0][0])
        self.assertFalse(final.exists())
        self.assertTrue(temp.exists())

    def test_mkdtemp_releases_parent_owner(self):
        parent = self.root / "parent"
        parent.mkdir()
        temporary = LocalTemporaryDirectory()
        with recording(artifact_cache) as rec:
            created = temporary.mkdtemp("pref-", str(parent))
        rec.assert_terminal_and_at_most_once(self)
        self.assertTrue(Path(created).is_dir())


class StreamWrapperCleanupTests(_ArtifactCacheCase):
    """A read/hash failure must survive a simultaneous wrapper-close failure."""

    @staticmethod
    def _fault_blob_close(fault):
        def selector(fd, owner):
            return fault if owner.label.startswith("cache blob ") else None

        return selector

    def _assert_one_blob_release(self, rec) -> None:
        blob_owners = [o for o in rec.owners if o.label.startswith("cache blob ")]
        self.assertEqual(len(blob_owners), 1)
        blob_closes = [c for c in rec.close_calls if c[2] is blob_owners[0]]
        self.assertEqual(len(blob_closes), 1)
        self.assertTrue(blob_closes[0][1])
        rec.assert_terminal_and_at_most_once(self)

    def test_read_failure_primary_over_wrapper_and_descriptor_close(self):
        integrity, blob = self._write_blob()
        operation_fault = OSError(errno.EIO, "injected read failure")
        wrapper_fault = OSError(errno.EIO, "injected wrapper close failure")
        descriptor_fault = OSError(errno.EIO, "injected descriptor close failure")
        with recording(
            artifact_cache, close_fault=self._fault_blob_close(descriptor_fault)
        ) as rec:
            with file_object_faults(
                artifact_cache, read_fault=operation_fault, close_fault=wrapper_fault
            ):
                with self.assertRaises(OSError) as caught:
                    self.filesystem.read_bytes(str(blob))
        self.assertIs(caught.exception, operation_fault)
        secondary = secondary_diagnostics(operation_fault)
        self.assertIn(wrapper_fault, secondary)
        self.assertIn(descriptor_fault, secondary)
        self._assert_one_blob_release(rec)

    def test_read_sole_wrapper_close_failure_is_primary(self):
        integrity, blob = self._write_blob()
        wrapper_fault = OSError(errno.EIO, "injected wrapper close failure")
        with file_object_faults(artifact_cache, close_fault=wrapper_fault):
            with self.assertRaises(OSError) as caught:
                self.filesystem.read_bytes(str(blob))
        self.assertIs(caught.exception, wrapper_fault)

    def test_read_descriptor_close_interruption_precedes_wrapper_close(self):
        integrity, blob = self._write_blob()
        wrapper_fault = OSError(errno.EIO, "injected wrapper close failure")
        interruption = KeyboardInterrupt("injected descriptor close interruption")
        with recording(
            artifact_cache, close_fault=self._fault_blob_close(interruption)
        ) as rec:
            with file_object_faults(artifact_cache, close_fault=wrapper_fault):
                with self.assertRaises(KeyboardInterrupt) as caught:
                    self.filesystem.read_bytes(str(blob))
        self.assertIs(caught.exception, interruption)
        self.assertIn(wrapper_fault, secondary_diagnostics(interruption))
        self._assert_one_blob_release(rec)

    def test_digest_failure_primary_over_wrapper_and_descriptor_close(self):
        integrity, blob = self._write_blob()
        identity = DigestIdentity.from_sri(integrity)
        operation_fault = OSError(errno.EIO, "injected read failure")
        wrapper_fault = OSError(errno.EIO, "injected wrapper close failure")
        descriptor_fault = OSError(errno.EIO, "injected descriptor close failure")
        with recording(
            artifact_cache, close_fault=self._fault_blob_close(descriptor_fault)
        ) as rec:
            with file_object_faults(
                artifact_cache, read_fault=operation_fault, close_fault=wrapper_fault
            ):
                with self.assertRaises(OSError) as caught:
                    self.filesystem.inspect_and_digest(str(blob), identity.algorithm)
        self.assertIs(caught.exception, operation_fault)
        secondary = secondary_diagnostics(operation_fault)
        self.assertIn(wrapper_fault, secondary)
        self.assertIn(descriptor_fault, secondary)
        self._assert_one_blob_release(rec)

    def test_digest_sole_wrapper_close_failure_is_primary(self):
        integrity, blob = self._write_blob()
        identity = DigestIdentity.from_sri(integrity)
        wrapper_fault = OSError(errno.EIO, "injected wrapper close failure")
        with file_object_faults(artifact_cache, close_fault=wrapper_fault):
            with self.assertRaises(OSError) as caught:
                self.filesystem.inspect_and_digest(str(blob), identity.algorithm)
        self.assertIs(caught.exception, wrapper_fault)

    def test_digest_descriptor_close_interruption_precedes_wrapper_close(self):
        integrity, blob = self._write_blob()
        identity = DigestIdentity.from_sri(integrity)
        wrapper_fault = OSError(errno.EIO, "injected wrapper close failure")
        interruption = KeyboardInterrupt("injected descriptor close interruption")
        with recording(
            artifact_cache, close_fault=self._fault_blob_close(interruption)
        ) as rec:
            with file_object_faults(artifact_cache, close_fault=wrapper_fault):
                with self.assertRaises(KeyboardInterrupt) as caught:
                    self.filesystem.inspect_and_digest(str(blob), identity.algorithm)
        self.assertIs(caught.exception, interruption)
        self.assertIn(wrapper_fault, secondary_diagnostics(interruption))
        self._assert_one_blob_release(rec)


class IdentityLockOwnershipTests(_ArtifactCacheCase):
    def test_acquire_adopts_through_a_shared_owner(self):
        lock = FileIdentityLock(str(self.root / "locks"))
        with recording(artifact_cache) as rec:
            self.assertTrue(lock.acquire("sha512-identity"))
        try:
            # The pre-adoption descriptor is held by a shared owner and
            # detached (transferred) on successful capability adoption.
            lock_owners = [o for o in rec.owners if o.label == "runtime-artifact locks"]
            self.assertEqual(len(lock_owners), 1)
            self.assertFalse(lock_owners[0].released)
        finally:
            lock.release("sha512-identity")

    def test_acquire_failure_releases_raw_descriptor_through_shared_owner(self):
        lock = FileIdentityLock(str(self.root / "locks"))
        fault = OSError(5, "injected close")

        def fault_lock_owner(fd, owner):
            return fault if owner.label == "runtime-artifact locks" else None

        with recording(artifact_cache, close_fault=fault_lock_owner) as rec:
            with mock.patch.object(
                artifact_cache.DirectoryCapability,
                "from_fd",
                side_effect=artifact_cache.CapabilityError("adoption rejected"),
            ):
                with self.assertRaises(artifact_cache.ArtifactMaterializationError):
                    lock.acquire("sha512-identity")
        rec.assert_terminal_and_at_most_once(self)

    def test_release_capability_and_directory_are_terminal_and_at_most_once(self):
        lock = FileIdentityLock(str(self.root / "locks"))
        lock.acquire("sha512-identity")
        capability = lock._capability
        directory = lock._directory
        lock.release("sha512-identity")
        self.assertTrue(capability.closed)
        self.assertTrue(directory.closed)
        # A repeated release is a no-op.
        lock.release("sha512-identity")
