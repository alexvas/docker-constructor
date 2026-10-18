"""Phase 6 (tasks 6.13 / 6.31) — build-cleanup descriptor ownership.

RED before migration: ``_close_leaf``, ``_unlink_entry``, and the pre-adoption
release branches of ``_open_algorithm_directory`` released raw ``os.open``
descriptors with a direct ``ops.close`` (or an injected-backend close inside a
lambda).  Precedence, aggregation, and at-most-once behavior were already
correct, but there was no shared terminal owner, so the migration contract
(terminal-before-close + tracked transfer + at-most-once) is the RED part.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from docker.transactions.capabilities import DirectoryCapability
from docker.transactions.errors import CapabilityError
from docker.versioning import build_cleanup as build_cleanup_module
from docker.versioning.build_cleanup import (
    ALGORITHM_DIRECTORY_MODE,
    _open_algorithm_directory,
    _unlink_entry,
)
from tests.descriptor_ownership_support import recording
from tests.transactions_test_support import InjectedOps


class UnlinkEntryOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="phase6-build-cleanup-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.ops = InjectedOps()
        self.leafs = DirectoryCapability.from_path(self.ops, self.root, label="leafs")
        self.addCleanup(self.leafs.close)

    def _candidate(self) -> Path:
        target = self.root / "candidate.blob"
        target.write_bytes(b"payload")
        os.chmod(target, 0o444)
        return target

    def test_success_releases_through_shared_owner(self) -> None:
        target = self._candidate()
        failures: list[object] = []
        with recording(build_cleanup_module) as rec:
            self.assertTrue(
                _unlink_entry(
                    self.ops,
                    self.leafs,
                    "candidate.blob",
                    failures,
                    target="candidate.blob",
                    allowed_mode=0o444,
                )
            )
        rec.assert_terminal_and_at_most_once(self)
        self.assertEqual(failures, [])
        self.assertFalse(target.exists())

    def test_interruption_releases_owner_and_stays_authoritative(self) -> None:
        self._candidate()
        failures: list[object] = []
        interruption = KeyboardInterrupt()
        self.ops.failures["fstat"] = interruption
        try:
            with recording(build_cleanup_module) as rec:
                with self.assertRaises(KeyboardInterrupt) as caught:
                    _unlink_entry(
                        self.ops,
                        self.leafs,
                        "candidate.blob",
                        failures,
                        target="candidate.blob",
                        allowed_mode=0o444,
                    )
            self.assertIs(caught.exception, interruption)
            rec.assert_terminal_and_at_most_once(self)
        finally:
            self.ops.failures.pop("fstat", None)


class OpenAlgorithmDirectoryOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="phase6-build-cleanup-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.ops = InjectedOps()
        blobs_dir = self.root / "blobs"
        blobs_dir.mkdir(mode=ALGORITHM_DIRECTORY_MODE)
        os.chmod(blobs_dir, ALGORITHM_DIRECTORY_MODE)
        self.blobs = DirectoryCapability.from_path(self.ops, blobs_dir, label="blobs")
        self.addCleanup(self.blobs.close)
        algorithm = blobs_dir / "sha256"
        algorithm.mkdir(mode=ALGORITHM_DIRECTORY_MODE)
        os.chmod(algorithm, ALGORITHM_DIRECTORY_MODE)

    def test_success_transfers_live_descriptor_to_the_capability(self) -> None:
        with recording(build_cleanup_module) as rec:
            capability, failure = _open_algorithm_directory(
                self.ops, self.blobs, "sha256"
            )
        self.assertIsNone(failure)
        self.assertIsNotNone(capability)
        self.assertEqual(len(rec.owners), 1)
        self.assertFalse(rec.owners[0].released)
        capability.close()

    def test_adoption_failure_releases_through_shared_owner(self) -> None:
        with recording(build_cleanup_module) as rec:
            with mock.patch.object(
                build_cleanup_module.DirectoryCapability,
                "from_fd",
                side_effect=CapabilityError("adoption rejected"),
            ):
                capability, failure = _open_algorithm_directory(
                    self.ops, self.blobs, "sha256"
                )
        self.assertIsNone(capability)
        self.assertIsNotNone(failure)
        rec.assert_terminal_and_at_most_once(self)

    def test_missing_algorithm_constructs_no_owner(self) -> None:
        with recording(build_cleanup_module) as rec:
            capability, failure = _open_algorithm_directory(
                self.ops, self.blobs, "absent"
            )
        self.assertIsNone(capability)
        self.assertIsNone(failure)
        self.assertEqual(rec.owners, [])
