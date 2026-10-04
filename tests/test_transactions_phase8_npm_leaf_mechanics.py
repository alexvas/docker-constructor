"""Phase 8 task 8.4 — npm selects the highest compatible shared contract.

The npm assembler migration must reuse the highest shared contract whose
*complete* semantics fit and justify every descent below L2.  The npm domain
keeps its own schema, identity, tree-commit, evidence, collision, quarantine,
and cancellation authority; no shared layer becomes a generic tree publisher.

Contract selection pinned here:

* the input-identity lock composes the shared L2 ``LockCapability`` (``BLOCK``)
  over a shared L1 ``DirectoryCapability`` -- no private ``flock`` loop;
* the non-authoritative index read reuses the shared L2 validated read;
* the non-authoritative index write reuses the shared L2 durable replacement;
* manifest/evidence writing and the immutable-tree commit deliberately descend
  to L0/L3: the L2 durability boundary (immediate parent-directory fsync) and
  the L2 temporary/link commit do not fit npm's explicit recursive-seal
  ordering, and tree commit/recursive fsync/sealing remain npm L3 authority.
"""
from __future__ import annotations

import inspect
import os
import stat
import tempfile
import unittest
from pathlib import Path

from docker.npm_environment import publication
from docker.npm_environment.publication import identity_coordination_lock
from docker.npm_environment.storage import prepare_assembler_namespace
from tests.test_npm_environment_publication import _PublicationTestCase

_ASSEMBLER = "a" * 64
_INPUT = "b" * 64


class ContractSelectionTests(unittest.TestCase):
    def test_module_composes_the_shared_lock_and_capabilities(self) -> None:
        src = inspect.getsource(publication)
        self.assertIn("LockCapability", src)
        self.assertIn("LockPolicy.BLOCK", src)
        self.assertIn("DirectoryCapability", src)
        # The private flock loop is gone: contention is owned by the shared
        # capability.
        self.assertNotIn("fcntl.flock(", src)

    def test_index_read_uses_the_shared_validated_read(self) -> None:
        src = inspect.getsource(publication.read_index)
        self.assertIn("validated_read", src)

    def test_index_write_uses_the_shared_durable_replacement(self) -> None:
        src = inspect.getsource(publication._append_index)
        self.assertIn("durable_replace", src)

    def test_manifest_and_evidence_read_uses_the_shared_validated_read(self) -> None:
        helper = inspect.getsource(publication._read_private_regular)
        self.assertIn("validated_read", helper)
        verify = inspect.getsource(publication.verify_output)
        self.assertIn("_read_private_regular", verify)

    def test_tree_commit_is_not_delegated_to_a_shared_tree_publisher(self) -> None:
        publish = inspect.getsource(publication._atomic_publish)
        # The immutable-tree commit stays a single npm-owned rename.
        self.assertIn("os.rename(str(tmp), str(final))", publish)
        # No shared regular-file contract publishes the tree.
        self.assertNotIn("atomic_no_clobber", publish)
        self.assertNotIn("durable_no_clobber", publish)
        self.assertFalse(hasattr(publication, "RegularFileContracts") and
                         "RegularFileContracts" in inspect.getsource(publication.publish_environment))

    def test_manifest_and_evidence_write_descends_to_l0_with_justification(self) -> None:
        src = inspect.getsource(publication._durable_write)
        # The complete-bytes exclusive write stays a descriptor-relative L0
        # mechanic because the L2 commit/durability boundaries do not fit the
        # npm recursive-seal ordering.
        self.assertNotIn("RegularFileContracts", src)
        self.assertNotIn("durable_no_clobber", src)
        self.assertIn("os.fsync", src)
        # The descent is documented in the module, not silently taken.
        module = inspect.getsource(publication)
        self.assertIn("descend", module.lower())


class IndexLeafReuseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="npm-env-phase8-leaf-")
        self.addCleanup(self.tmp.cleanup)
        self.cache_root = Path(self.tmp.name) / "cache"
        self.cache_root.mkdir()
        self.namespace = prepare_assembler_namespace(self.cache_root, _ASSEMBLER)

    def test_read_index_does_not_follow_a_symlink(self) -> None:
        target = Path(self.tmp.name) / "outside.json"
        target.write_text('["' + "d" * 64 + '"]')
        index = self.namespace.index / f"{_INPUT}.json"
        index.symlink_to(target)
        # The shared validated read refuses to follow the symlink, so the
        # advisory index reads as absent rather than trusting the target.
        self.assertEqual(publication.read_index(self.namespace, _INPUT), ())
        self.assertEqual(target.read_text(), '["' + "d" * 64 + '"]')

    def test_index_write_publishes_a_private_regular_file(self) -> None:
        publication._append_index(self.namespace, _INPUT, "e" * 64)
        index = self.namespace.index / f"{_INPUT}.json"
        info = os.lstat(index)
        self.assertTrue(stat.S_ISREG(info.st_mode))
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
        self.assertEqual(info.st_uid, os.geteuid())
        self.assertEqual(publication.read_index(self.namespace, _INPUT), ("e" * 64,))

    def test_read_index_rejects_a_wrong_mode_index(self) -> None:
        # The npm-owned index mode is 0600.  A more permissive index entry
        # (here 0644) is not trusted and reads as absent, even though its
        # bytes are valid.
        index = self.namespace.index / f"{_INPUT}.json"
        index.write_text('["' + "d" * 64 + '"]')
        os.chmod(index, 0o644)
        self.assertEqual(publication.read_index(self.namespace, _INPUT), ())
        # The mode is the discriminator: the same bytes read once the
        # npm-owned mode is restored.
        os.chmod(index, 0o600)
        self.assertEqual(
            publication.read_index(self.namespace, _INPUT), ("d" * 64,)
        )


class ImmutableReadModeTests(_PublicationTestCase):
    """The shared validated read enforces the npm immutable 0444 mode."""

    def _path(self, output_identity: str, name: str) -> Path:
        return self.namespace.outputs / output_identity / name

    def test_verify_output_accepts_the_immutable_modes(self) -> None:
        result = self._publish(b"mode-ok")
        for name in (publication.EVIDENCE_FILE, publication.MANIFEST_FILE):
            info = os.lstat(self._path(result.output_identity, name))
            self.assertEqual(stat.S_IMODE(info.st_mode), 0o444)
        self.assertIsNotNone(
            publication.verify_output(
                self.namespace,
                result.output_identity,
                input_identity=self.input_identity,
            )
        )

    def test_published_manifest_and_evidence_modes_survive_a_restrictive_umask(self) -> None:
        original = os.umask(0o077)
        try:
            result = self._publish(b"restrictive-umask")
        finally:
            os.umask(original)
        for name in (publication.MANIFEST_FILE, publication.EVIDENCE_FILE):
            info = os.lstat(self._path(result.output_identity, name))
            # The mode is established explicitly, so the restrictive umask
            # (which would otherwise leave these at 0o400) does not reduce it.
            self.assertEqual(stat.S_IMODE(info.st_mode), 0o444)
        # The exact-mode leaves remain reusable after the umask is restored.
        self.assertIsNotNone(
            publication.verify_output(
                self.namespace,
                result.output_identity,
                input_identity=self.input_identity,
            )
        )

    def test_verify_output_rejects_a_wrong_mode_evidence(self) -> None:
        result = self._publish(b"mode-evidence")
        os.chmod(self._path(result.output_identity, publication.EVIDENCE_FILE), 0o644)
        self.assertIsNone(
            publication.verify_output(
                self.namespace,
                result.output_identity,
                input_identity=self.input_identity,
            )
        )

    def test_verify_output_rejects_a_wrong_mode_manifest(self) -> None:
        result = self._publish(b"mode-manifest")
        os.chmod(self._path(result.output_identity, publication.MANIFEST_FILE), 0o644)
        self.assertIsNone(
            publication.verify_output(
                self.namespace,
                result.output_identity,
                input_identity=self.input_identity,
            )
        )


if __name__ == "__main__":
    unittest.main()
