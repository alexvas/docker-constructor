"""Phase 6 (tasks 6.12 / 6.30) — snapshot descriptor ownership.

RED before migration: ``_open_validated_source``,
``_validate_hard_link_destination``, and the source/staging releases inside
``create_artifact_snapshot`` released raw ``os.open`` descriptors with a direct
``os.close``.  Precedence was already correct, but there was no shared terminal
owner, so the migration contract (terminal-before-close + tracked transfer +
at-most-once) is the RED part.  ``_copy_source_fd_to_destination`` already owned
its destination descriptor in Phase 3 and stays passing.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import tempfile
import unittest
from pathlib import Path

from docker.versioning import build_snapshot as build_snapshot_module
from docker.versioning.build_materialization import SelectedBuildArtifact
from docker.versioning.build_snapshot import (
    SnapshotError,
    _open_validated_source,
    _validate_hard_link_destination,
    cleanup_artifact_snapshot,
    create_artifact_snapshot,
)
from docker.versioning.digest_identity import DigestIdentity
from tests.descriptor_ownership_support import recording, secondary_diagnostics


def _selected(root: Path):
    result = []
    for name, payload in (("rustup", b"r"), ("uv", b"u"), ("rtk", b"t"), ("fd", b"f")):
        blob = root / f"{name}.blob"
        blob.write_bytes(payload)
        blob.chmod(0o444)
        result.append(
            (
                SelectedBuildArtifact(
                    name,
                    "https://not-exposed.invalid/",
                    DigestIdentity.from_hex(
                        "sha256", hashlib.sha256(payload).hexdigest()
                    ),
                ),
                blob,
            )
        )
    return result


class _SnapshotCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="phase6-snapshot-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.blob = self.root / "blob"
        self.payload = b"snapshot payload\n"
        self.blob.write_bytes(self.payload)
        self.blob.chmod(0o444)
        self.expected = hashlib.sha256(self.payload).hexdigest()


class OpenValidatedSourceOwnershipTests(_SnapshotCase):
    def test_success_transfers_live_descriptor_to_the_caller(self) -> None:
        with recording(build_snapshot_module) as rec:
            fd = _open_validated_source(self.blob, self.expected)
        try:
            self.assertTrue(stat.S_ISREG(os.fstat(fd).st_mode))
        finally:
            os.close(fd)
        self.assertEqual(len(rec.owners), 1)
        self.assertFalse(rec.owners[0].released)

    def test_digest_failure_releases_through_shared_owner(self) -> None:
        with recording(build_snapshot_module) as rec:
            with self.assertRaises(SnapshotError):
                _open_validated_source(self.blob, "0" * 64)
        rec.assert_terminal_and_at_most_once(self)

    def test_digest_failure_primary_over_close_failure(self) -> None:
        close_fault = OSError(errno.EIO, "injected close")

        def fault(fd, owner):
            return close_fault

        with recording(build_snapshot_module, close_fault=fault) as rec:
            with self.assertRaises(SnapshotError) as caught:
                _open_validated_source(self.blob, "0" * 64)
        self.assertIn(close_fault, secondary_diagnostics(caught.exception))
        rec.assert_terminal_and_at_most_once(self)


class ValidateHardLinkDestinationOwnershipTests(_SnapshotCase):
    def setUp(self) -> None:
        super().setUp()
        self.directory = self.root / "dir"
        self.directory.mkdir()
        self.destination_name = "payload.deb"
        os.link(self.blob, self.directory / self.destination_name)
        self.dir_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, self.dir_fd)
        self.source_stat = os.stat(self.blob)

    def test_success_releases_through_shared_owner(self) -> None:
        with recording(build_snapshot_module) as rec:
            _validate_hard_link_destination(
                self.destination_name, self.dir_fd, self.source_stat, self.expected
            )
        rec.assert_terminal_and_at_most_once(self)

    def test_identity_mismatch_releases_through_shared_owner(self) -> None:
        other = self.root / "other"
        other.write_bytes(self.payload)
        other.chmod(0o444)
        other_stat = os.stat(other)
        with recording(build_snapshot_module) as rec:
            with self.assertRaises(SnapshotError):
                _validate_hard_link_destination(
                    self.destination_name, self.dir_fd, other_stat, self.expected
                )
        rec.assert_terminal_and_at_most_once(self)


class CreateArtifactSnapshotOwnershipTests(_SnapshotCase):
    def test_snapshot_releases_source_and_staging_owners(self) -> None:
        pairs = _selected(self.root)
        with recording(build_snapshot_module) as rec:
            snapshot = create_artifact_snapshot(
                (item for item, _ in pairs),
                (blob for _, blob in pairs),
                constructor_project_root=self.root,
            )
        rec.assert_terminal_and_at_most_once(self)
        # four source owners plus the staging-directory owner
        self.assertGreaterEqual(len(rec.owners), 5)
        cleanup_artifact_snapshot(snapshot)

    def test_snapshot_releases_owners_on_validation_failure(self) -> None:
        pairs = _selected(self.root)
        # Corrupt the second blob's mode so its validation fails after the
        # first source has already been processed and released.
        pairs[1][1].chmod(0o644)
        with recording(build_snapshot_module) as rec:
            with self.assertRaises(SnapshotError):
                create_artifact_snapshot(
                    (item for item, _ in pairs),
                    (blob for _, blob in pairs),
                    constructor_project_root=self.root,
                )
        rec.assert_terminal_and_at_most_once(self)
