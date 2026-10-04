"""Phase 8 task 8.3 — npm specialization protocols stay unchanged.

Migrating the input-identity lock to the shared ``BLOCK`` capability must not
move any npm L3 authority into the shared substrate.  These tests pin the
unchanged specialization protocols: immutable-output collision handling,
recursive fsync/sealing, evidence authority, advisory-index best-effort
failure, corrupt-output quarantine, cancellation, workspace cleanup, and
primary-error preservation.
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from docker.npm_environment import LockedNpmError, publication
from docker.transactions.errors import TransactionError
from tests.test_npm_environment_publication import (
    _PublicationTestCase,
    _no_write_bits,
)


class SpecializationBoundaryTests(unittest.TestCase):
    def test_shared_substrate_gains_no_tree_or_evidence_authority(self) -> None:
        import inspect

        from docker.transactions import regular

        source = inspect.getsource(regular)
        # The shared L2 contracts expose only individual regular-file
        # operations; they never own npm tree, evidence, collision, or
        # quarantine authority.
        for forbidden in (
            "quarantine", "evidence", "TreeManifest", "tree_digest",
            "output_identity",
        ):
            self.assertNotIn(forbidden, source)
        # The npm module still owns the complete publication surface.
        for name in (
            "_atomic_publish", "_fsync_tree", "_quarantine_corrupt_output",
            "_remove_redundant_tree", "publish_environment", "verify_output",
            "find_cached_result",
        ):
            self.assertTrue(callable(getattr(publication, name)))

    def test_tree_commit_remains_an_npm_rename(self) -> None:
        import inspect

        source = inspect.getsource(publication._atomic_publish)
        self.assertIn("os.rename(str(tmp), str(final))", source)
        self.assertIn("os.fsync(outputs_fd)", source)


class ImmutablePublicationSpecializationTests(_PublicationTestCase):
    def test_recursive_sealing_strips_every_write_bit(self) -> None:
        result = self._publish(b"seal")
        env_root = result.environment_root
        self.assertTrue(_no_write_bits(env_root))
        self.assertTrue(_no_write_bits(env_root.parent))
        self.assertTrue(_no_write_bits(env_root / "node_modules" / "a"))
        self.assertTrue(
            _no_write_bits(env_root / "node_modules" / "a" / "marker.txt")
        )
        self.assertTrue(_no_write_bits(result.evidence_path))

    def test_verified_collision_never_overwrites_committed_bytes(self) -> None:
        first = self._publish(b"same")
        second = self._publish(b"same")
        self.assertEqual(second.output_identity, first.output_identity)
        self.assertEqual(second.environment_root, first.environment_root)
        self.assertEqual(
            (
                first.environment_root / "node_modules" / "a" / "marker.txt"
            ).read_bytes(),
            b"same",
        )

    def test_evidence_authority_binds_the_input_identity(self) -> None:
        result = self._publish(b"evidence")
        self.assertIsNotNone(
            publication.verify_output(
                self.namespace,
                result.output_identity,
                input_identity=self.input_identity,
            )
        )
        # Replacing the stored evidence makes the candidate a cache miss.
        evidence_path = self.namespace.outputs / result.output_identity / "evidence.json"
        os.chmod(evidence_path, 0o600)
        evidence_path.write_bytes(b"{}")
        self.assertIsNone(
            publication.verify_output(
                self.namespace,
                result.output_identity,
                input_identity=self.input_identity,
            )
        )

    def test_advisory_index_failure_does_not_fail_publication(self) -> None:
        error = TransactionError("replace", "index write exploded")
        with mock.patch.object(
            publication.RegularFileContracts,
            "durable_replace",
            side_effect=error,
        ):
            result = self._publish(b"index-best-effort")
        # Publication still succeeds; only the advisory index write was lost.
        self.assertIsNotNone(
            publication.verify_output(
                self.namespace,
                result.output_identity,
                input_identity=self.input_identity,
            )
        )

    def test_corrupt_output_is_quarantined_and_reconstructed(self) -> None:
        first = self._publish(b"same")
        os.chmod(first.environment_root, 0o700)
        (first.environment_root / "SENTINEL").write_text("corruption")

        second = self._publish(b"same")

        self.assertEqual(second.output_identity, first.output_identity)
        self.assertFalse((second.environment_root / "SENTINEL").exists())
        quarantined = [
            p
            for p in self.namespace.outputs.iterdir()
            if p.name.startswith(".corrupt-")
        ]
        self.assertEqual(len(quarantined), 1)
        self.assertTrue((quarantined[0] / "tree" / "SENTINEL").exists())

    def test_quarantine_failure_preserves_corrupt_output(self) -> None:
        first = self._publish(b"one")
        os.chmod(first.environment_root, 0o700)
        (first.environment_root / "SENTINEL").write_text("corruption")
        with mock.patch.object(
            publication, "_quarantine_corrupt_output", return_value=None
        ):
            with self.assertRaises(LockedNpmError) as ctx:
                self._publish(b"one")
        self.assertEqual(ctx.exception.reason, "output_collision_corrupt")
        self.assertTrue((first.environment_root / "SENTINEL").exists())


class CancellationPreservationTests(_PublicationTestCase):
    """A cancellation from publication stays primary and cleans staging."""

    def _run_failing(self, publication_exc: BaseException):
        from docker.npm_environment import assemble_environment
        from tests.test_npm_environment_publication_cleanup import FakeExecutor

        with mock.patch.object(
            publication, "publish_environment", side_effect=publication_exc
        ):
            return assemble_environment(
                validated=self.validated,
                assembler=self.assembler,
                cache_root=self.cache_root,
                executor=FakeExecutor(),
            )

    def test_publication_cancellation_preserves_primary_and_removes_staging(self) -> None:
        cancellation = KeyboardInterrupt("publication cancelled")
        staging_path = self.namespace.staging / self.input_identity.digest
        with self.assertRaises(KeyboardInterrupt) as ctx:
            self._run_failing(cancellation)
        self.assertIs(ctx.exception, cancellation)
        # The matching staging workspace was removed; no output committed.
        self.assertFalse(staging_path.exists())
        self.assertEqual(
            [p for p in self.namespace.outputs.iterdir()], []
        )

    def test_operational_publication_failure_preserves_primary(self) -> None:
        failure = LockedNpmError("output_validation_failed", "boom")
        staging_path = self.namespace.staging / self.input_identity.digest
        with self.assertRaises(LockedNpmError) as ctx:
            self._run_failing(failure)
        self.assertIs(ctx.exception, failure)
        self.assertFalse(staging_path.exists())
        self.assertEqual([p for p in self.namespace.outputs.iterdir()], [])


if __name__ == "__main__":
    unittest.main()
