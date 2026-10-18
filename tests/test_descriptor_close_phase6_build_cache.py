"""Phase 6 (tasks 6.8–6.9) — build-cache descriptor ownership.

RED before migration: the namespace/state, publication, verification, lock,
blob-algorithm, and snapshot-tree helpers in
``docker/versioning/build_cache.py`` released their ``os.open`` / ``os.dup`` /
``ops.openat`` descriptors with a direct ``os.close`` / ``ops.close`` (or an
anonymous ``lambda``) instead of a shared terminal owner.  Precedence and
at-most-once behavior were already preserved, but there was no shared owner, so
the migration contract (tracked handoff + terminal-before-close + at-most-once
release) is the RED part.  The capability-based ``_validate_existing_blob`` /
``_validate_existing_marker`` helpers already delegate to shared capabilities
and stay passing.
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

from docker.transactions.capabilities import DirectoryCapability
from docker.transactions.posix import PosixFileOps
from docker.versioning import build_cache
from docker.versioning.build_cache import (
    BuildCacheError,
    open_build_cache_state,
    prepare_build_cache,
)
from docker.versioning.digest_identity import DigestIdentity
from docker.versioning.project_state import resolve_project_state
from tests.descriptor_ownership_support import (
    file_object_faults,
    recording,
    secondary_diagnostics,
)


def _labels(rec) -> list[str]:
    return [owner.label for owner in rec.owners]


def _has_label(rec, needle: str) -> bool:
    return any(needle in label for label in _labels(rec))


class _BuildCacheCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="phase6-build-cache-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.checkout = self.base / "checkout"
        self.checkout.mkdir()
        self.cache = self.base / "cache"
        self.cache.mkdir(mode=0o700)
        self.state = resolve_project_state(self.checkout, cache_root=self.cache)
        self.paths = prepare_build_cache(self.checkout, cache_root=self.cache)
        self.namespace_fd = os.open(
            self.paths.namespace_root, os.O_RDONLY | os.O_DIRECTORY
        )
        self.addCleanup(self._close_namespace)

    def _close_namespace(self) -> None:
        try:
            os.close(self.namespace_fd)
        except OSError:
            pass

    def _publish(self, payload: bytes = b"blob") -> DigestIdentity:
        raw = hashlib.sha512(payload).digest()
        identity = DigestIdentity.from_sri("sha512-" + base64.b64encode(raw).decode())
        build_cache.publish_verified_blob(
            identity,
            payload,
            constructor_project_root=self.checkout,
            cache_root=self.cache,
        )
        return identity


class NamespaceStateOwnershipTests(_BuildCacheCase):
    def test_open_namespace_fd_hands_off_a_live_owner(self) -> None:
        with recording(build_cache) as rec:
            fd = build_cache._open_namespace_fd(self.paths.namespace_root)
        try:
            self.assertTrue(stat.S_ISDIR(os.fstat(fd).st_mode))
        finally:
            os.close(fd)
        self.assertEqual(len(rec.owners), 1)
        self.assertIn("namespace descriptor", _labels(rec)[0])
        # A returned live descriptor is detached, never closed by the helper.
        self.assertFalse(rec.owners[0].released)

    def test_open_namespace_fd_failure_releases_owner(self) -> None:
        with recording(build_cache) as rec:
            with mock.patch.object(build_cache.os, "access", return_value=False):
                with self.assertRaises(BuildCacheError):
                    build_cache._open_namespace_fd(self.paths.namespace_root)
        rec.assert_terminal_and_at_most_once(self)

    def test_validate_relative_dir_releases_wrapper_owner(self) -> None:
        with recording(build_cache) as rec:
            build_cache._validate_relative_dir(
                self.namespace_fd,
                ("build-artifacts", "blobs"),
                label="blobs",
                check_owner=True,
            )
        rec.assert_terminal_and_at_most_once(self)
        self.assertTrue(_has_label(rec, "validated directory"))

    def test_ensure_relative_dir_releases_wrapper_owner(self) -> None:
        with recording(build_cache) as rec:
            build_cache._ensure_relative_dir(
                self.namespace_fd,
                ("build-artifacts", "tmp"),
                label="tmp",
                check_owner=True,
            )
        rec.assert_terminal_and_at_most_once(self)
        self.assertTrue(_has_label(rec, "ensured directory"))

    def test_open_validated_child_hands_off_a_live_owner(self) -> None:
        with recording(build_cache) as rec:
            fd = build_cache._open_validated_child(
                self.namespace_fd, "build-artifacts", label="build-artifacts"
            )
        try:
            self.assertTrue(stat.S_ISDIR(os.fstat(fd).st_mode))
        finally:
            os.close(fd)
        self.assertEqual(len(rec.owners), 1)
        self.assertFalse(rec.owners[0].released)

    def test_open_validated_child_failure_releases_owner(self) -> None:
        insecured = self.paths.namespace_root / "insecure"
        insecured.mkdir()
        os.chmod(insecured, 0o755)
        with recording(build_cache) as rec:
            with self.assertRaises(BuildCacheError):
                build_cache._open_validated_child(
                    self.namespace_fd, "insecure", label="insecure"
                )
        rec.assert_terminal_and_at_most_once(self)

    def test_prepare_build_cache_releases_namespace_owner(self) -> None:
        with recording(build_cache) as rec:
            prepare_build_cache(self.checkout, cache_root=self.cache)
        rec.assert_terminal_and_at_most_once(self)
        self.assertTrue(_has_label(rec, "prepared namespace"))

    def test_open_build_cache_state_releases_namespace_owner(self) -> None:
        with recording(build_cache) as rec:
            state = open_build_cache_state(self.checkout, cache_root=self.cache)
        state.close()
        rec.assert_terminal_and_at_most_once(self)
        self.assertTrue(_has_label(rec, "state namespace"))

    def test_validate_existing_blob_delegates_to_shared_capability(self) -> None:
        identity = self._publish()
        ops = PosixFileOps()
        fd = os.open(
            self.paths.blobs_root / identity.algorithm,
            os.O_RDONLY | os.O_DIRECTORY,
        )
        directory = DirectoryCapability.from_fd(ops, fd, "blobs-algorithm")
        try:
            with recording(build_cache) as rec:
                build_cache._validate_existing_blob(directory, identity)
            # The validated file capability is a shared capability released
            # internally; the helper owns no extra build-cache descriptor.
            self.assertEqual(rec.owners, [])
        finally:
            directory.close()


class PublicationOwnershipTests(_BuildCacheCase):
    def test_publish_verified_blob_releases_all_owners(self) -> None:
        raw = hashlib.sha512(b"payload").digest()
        identity = DigestIdentity.from_sri("sha512-" + base64.b64encode(raw).decode())
        with recording(build_cache) as rec:
            path = build_cache.publish_verified_blob(
                identity,
                b"payload",
                constructor_project_root=self.checkout,
                cache_root=self.cache,
            )
        rec.assert_terminal_and_at_most_once(self)
        self.assertTrue(path.exists())
        self.assertTrue(_has_label(rec, "publication"))

    def test_validate_blob_descriptor_releases_owner(self) -> None:
        identity = self._publish()
        alg_fd = os.open(
            self.paths.blobs_root / identity.algorithm,
            os.O_RDONLY | os.O_DIRECTORY,
        )
        try:
            with recording(build_cache) as rec:
                build_cache._validate_blob_descriptor(
                    alg_fd,
                    identity.hex_digest() + build_cache.BLOB_EXTENSION,
                    identity,
                )
        finally:
            os.close(alg_fd)
        rec.assert_terminal_and_at_most_once(self)
        self.assertTrue(_has_label(rec, "blob descriptor"))

    def test_verify_published_blob_releases_owners(self) -> None:
        identity = self._publish()
        with recording(build_cache) as rec:
            build_cache._verify_published_blob(identity, self.paths)
        rec.assert_terminal_and_at_most_once(self)
        self.assertTrue(_has_label(rec, "verify namespace"))

    def test_open_blob_algorithm_hands_off_a_live_owner(self) -> None:
        identity = self._publish()
        ops = PosixFileOps()
        blobs_fd = os.open(self.paths.blobs_root, os.O_RDONLY | os.O_DIRECTORY)
        blobs = DirectoryCapability.from_fd(ops, blobs_fd, "blobs")
        try:
            with recording(build_cache) as rec:
                capability = build_cache._open_blob_algorithm(
                    ops, blobs, identity.algorithm
                )
        finally:
            blobs.close()
        try:
            self.assertIsNotNone(capability)
            self.assertEqual(len(rec.owners), 1)
            self.assertFalse(rec.owners[0].released)
        finally:
            if capability is not None:
                capability.close()

    def test_open_blob_algorithm_failure_releases_owner(self) -> None:
        insecured = self.paths.blobs_root / "insecure"
        insecured.mkdir()
        os.chmod(insecured, 0o755)
        ops = PosixFileOps()
        blobs_fd = os.open(self.paths.blobs_root, os.O_RDONLY | os.O_DIRECTORY)
        blobs = DirectoryCapability.from_fd(ops, blobs_fd, "blobs")
        try:
            with recording(build_cache) as rec:
                with self.assertRaises(BuildCacheError):
                    build_cache._open_blob_algorithm(ops, blobs, "insecure")
            rec.assert_terminal_and_at_most_once(self)
        finally:
            blobs.close()

    def test_remove_snapshot_tree_releases_owner(self) -> None:
        tree = self.paths.generated_root / "snapshot"
        (tree / "sub").mkdir(parents=True)
        (tree / "sub" / "file").write_bytes(b"x")
        parent_fd = os.open(self.paths.generated_root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with recording(build_cache) as rec:
                build_cache._remove_snapshot_tree(parent_fd, "snapshot", depth=64)
        finally:
            os.close(parent_fd)
        rec.assert_terminal_and_at_most_once(self)
        self.assertTrue(_has_label(rec, "snapshot directory"))
        self.assertFalse(tree.exists())

    def test_storage_handle_open_failure_releases_owner(self) -> None:
        ops = PosixFileOps()
        parent_fd = os.open(self.paths.persistent_root, os.O_RDONLY | os.O_DIRECTORY)

        class _FakeGenerations:
            fd = parent_fd

            def child_basename(self, name: str) -> str:
                return name

        handle = build_cache._BuildStorageHandle(ops, _FakeGenerations())
        try:
            with mock.patch.object(
                build_cache.DirectoryCapability,
                "from_fd",
                side_effect=build_cache.CapabilityError("adoption rejected"),
            ):
                with recording(build_cache) as rec:
                    with self.assertRaises(build_cache.CapabilityError):
                        handle._open("blobs")
            rec.assert_terminal_and_at_most_once(self)
            self.assertTrue(_has_label(rec, "storage"))
        finally:
            os.close(parent_fd)

    def test_acquire_lock_failure_releases_parent_owner(self) -> None:
        with mock.patch.object(
            build_cache.DirectoryCapability,
            "from_fd",
            side_effect=build_cache.CapabilityError("adoption rejected"),
        ):
            with recording(build_cache) as rec:
                with self.assertRaises(build_cache.CapabilityError):
                    build_cache.acquire_constructor_project_build_lock(
                        self.checkout, cache_root=self.cache
                    )
        rec.assert_terminal_and_at_most_once(self)
        self.assertTrue(_has_label(rec, "build lock parent"))


class PublicationStreamCleanupTests(_BuildCacheCase):
    """A write/flush failure must survive a simultaneous wrapper-close failure."""

    def _identity(self, payload: bytes = b"payload") -> DigestIdentity:
        raw = hashlib.sha512(payload).digest()
        return DigestIdentity.from_sri("sha512-" + base64.b64encode(raw).decode())

    @staticmethod
    def _fault_temporary_close(fault):
        def selector(fd, owner):
            return fault if owner.label.startswith("publication temporary ") else None

        return selector

    def _publish(self, identity: DigestIdentity) -> Path:
        return build_cache.publish_verified_blob(
            identity,
            b"payload",
            constructor_project_root=self.checkout,
            cache_root=self.cache,
        )

    def _assert_one_temporary_release(self, rec) -> None:
        temp_owners = [
            owner
            for owner in rec.owners
            if owner.label.startswith("publication temporary ")
        ]
        self.assertEqual(len(temp_owners), 1)
        temp_closes = [c for c in rec.close_calls if c[2] is temp_owners[0]]
        self.assertEqual(len(temp_closes), 1)
        self.assertTrue(temp_closes[0][1])
        rec.assert_terminal_and_at_most_once(self)

    def test_write_failure_primary_over_wrapper_and_descriptor_close(self) -> None:
        identity = self._identity()
        operation_fault = OSError(errno.EIO, "injected write failure")
        wrapper_fault = OSError(errno.EIO, "injected wrapper close failure")
        descriptor_fault = OSError(errno.EIO, "injected descriptor close failure")
        with recording(
            build_cache, close_fault=self._fault_temporary_close(descriptor_fault)
        ) as rec:
            with file_object_faults(
                build_cache, write_fault=operation_fault, close_fault=wrapper_fault
            ):
                with self.assertRaises(OSError) as caught:
                    self._publish(identity)
        self.assertIs(caught.exception, operation_fault)
        secondary = secondary_diagnostics(operation_fault)
        self.assertIn(wrapper_fault, secondary)
        self.assertIn(descriptor_fault, secondary)
        self._assert_one_temporary_release(rec)
        # No replacement/publication occurred after the failure.
        self.assertFalse(
            build_cache.build_blob_path(self.paths.blobs_root, identity).exists()
        )

    def test_sole_wrapper_close_failure_is_primary(self) -> None:
        identity = self._identity()
        wrapper_fault = OSError(errno.EIO, "injected wrapper close failure")
        with file_object_faults(build_cache, close_fault=wrapper_fault):
            with self.assertRaises(OSError) as caught:
                self._publish(identity)
        self.assertIs(caught.exception, wrapper_fault)
        self.assertFalse(
            build_cache.build_blob_path(self.paths.blobs_root, identity).exists()
        )

    def test_flush_failure_primary_over_wrapper_and_descriptor_close(self) -> None:
        identity = self._identity()
        operation_fault = OSError(errno.EIO, "injected flush failure")
        wrapper_fault = OSError(errno.EIO, "injected wrapper close failure")
        descriptor_fault = OSError(errno.EIO, "injected descriptor close failure")
        with recording(
            build_cache, close_fault=self._fault_temporary_close(descriptor_fault)
        ) as rec:
            with file_object_faults(
                build_cache, flush_fault=operation_fault, close_fault=wrapper_fault
            ):
                with self.assertRaises(OSError) as caught:
                    self._publish(identity)
        self.assertIs(caught.exception, operation_fault)
        secondary = secondary_diagnostics(operation_fault)
        self.assertIn(wrapper_fault, secondary)
        self.assertIn(descriptor_fault, secondary)
        self._assert_one_temporary_release(rec)
        self.assertFalse(
            build_cache.build_blob_path(self.paths.blobs_root, identity).exists()
        )

    def test_descriptor_close_interruption_precedes_wrapper_close(self) -> None:
        identity = self._identity()
        wrapper_fault = OSError(errno.EIO, "injected wrapper close failure")
        interruption = KeyboardInterrupt("injected descriptor close interruption")
        with recording(
            build_cache, close_fault=self._fault_temporary_close(interruption)
        ) as rec:
            with file_object_faults(build_cache, close_fault=wrapper_fault):
                with self.assertRaises(KeyboardInterrupt) as caught:
                    self._publish(identity)
        self.assertIs(caught.exception, interruption)
        self.assertIn(wrapper_fault, secondary_diagnostics(interruption))
        rec.assert_terminal_and_at_most_once(self)
        self.assertFalse(
            build_cache.build_blob_path(self.paths.blobs_root, identity).exists()
        )


class PublicationInitializationFailureTests(_BuildCacheCase):
    """Temporary-name generation must not leak the namespace descriptor.

    ``os.urandom`` runs before the namespace descriptor is acquired, so a
    failure or interruption while generating the temporary name releases no
    descriptor and leaves the cache untouched.
    """

    def _identity(self, payload: bytes = b"payload") -> DigestIdentity:
        raw = hashlib.sha512(payload).digest()
        return DigestIdentity.from_sri("sha512-" + base64.b64encode(raw).decode())

    def _publish(self, identity: DigestIdentity) -> Path:
        return build_cache.publish_verified_blob(
            identity,
            b"payload",
            constructor_project_root=self.checkout,
            cache_root=self.cache,
        )

    def _assert_no_publication(self, identity: DigestIdentity) -> None:
        self.assertFalse(
            build_cache.build_blob_path(self.paths.blobs_root, identity).exists()
        )

    def test_urandom_oserror_propagates_without_namespace_open(self) -> None:
        identity = self._identity()
        fault = OSError(errno.EIO, "injected urandom failure")
        with recording(build_cache) as rec:
            with mock.patch.object(build_cache.os, "urandom", side_effect=fault):
                with self.assertRaises(OSError) as caught:
                    self._publish(identity)
        # The exact injected failure stays primary; no descriptor is acquired
        # for the publication namespace before name generation can fail.
        self.assertIs(caught.exception, fault)
        self.assertFalse(_has_label(rec, "publication namespace"))
        rec.assert_terminal_and_at_most_once(self)
        self._assert_no_publication(identity)

    def test_urandom_interruption_propagates_without_namespace_open(self) -> None:
        identity = self._identity()
        interruption = KeyboardInterrupt("injected urandom interruption")
        with recording(build_cache) as rec:
            with mock.patch.object(build_cache.os, "urandom", side_effect=interruption):
                with self.assertRaises(KeyboardInterrupt) as caught:
                    self._publish(identity)
        self.assertIs(caught.exception, interruption)
        self.assertFalse(_has_label(rec, "publication namespace"))
        rec.assert_terminal_and_at_most_once(self)
        self._assert_no_publication(identity)

    def test_namespace_release_is_terminal_and_close_failure_is_secondary(self) -> None:
        identity = self._identity()
        primary_fault = BuildCacheError("injected algorithm validation failure")
        close_fault = OSError(errno.EIO, "injected namespace close failure")

        def selector(fd, owner):
            if owner.label == "publication namespace":
                return close_fault
            return None

        with recording(build_cache, close_fault=selector) as rec:
            with mock.patch.object(
                build_cache, "_require_algorithm_dir", side_effect=primary_fault
            ):
                with self.assertRaises(BuildCacheError) as caught:
                    self._publish(identity)
        # A later initialization failure keeps precedence while the namespace
        # release is attempted exactly once, terminally, and its close failure
        # is retained as a secondary diagnostic.
        self.assertIs(caught.exception, primary_fault)
        self.assertIn(close_fault, secondary_diagnostics(primary_fault))
        namespace_owners = [
            owner for owner in rec.owners if owner.label == "publication namespace"
        ]
        self.assertEqual(len(namespace_owners), 1)
        namespace_closes = [
            call for call in rec.close_calls if call[2] is namespace_owners[0]
        ]
        self.assertEqual(len(namespace_closes), 1)
        self.assertTrue(namespace_closes[0][1])
        rec.assert_terminal_and_at_most_once(self)
        self._assert_no_publication(identity)


if __name__ == "__main__":
    unittest.main()
