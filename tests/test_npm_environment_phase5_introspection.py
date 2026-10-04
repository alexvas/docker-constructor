"""Phase 5 — publication state-machine introspection (task 5.8).

The full state machine is audited for races, partial publication, lock
inversion, deletion of committed data, and consumer leakage:

* a single exclusive input-identity lock serializes lookup/assembly/
  validation/publication and is always released;
* evidence and manifest bytes are written and fsync-ed before the one atomic
  rename that creates a committed output;
* cleanup only ever removes temporary ``.tmp-`` directories, never a
  committed output, and collisions never overwrite;
* no second lock is acquired while holding the identity lock; and
* result/evidence DTOs expose no consumer-specific field.
"""

from __future__ import annotations

import dataclasses
import inspect
import unittest

from docker.npm_environment import evidence as evidence_module
from docker.npm_environment import publication as publication_module


class TestNoRaces(unittest.TestCase):
    def test_single_exclusive_identity_lock(self):
        src = inspect.getsource(publication_module)
        # The lock composes the shared blocking capability; there is no
        # private flock loop left in the npm module.
        self.assertIn("LockCapability.acquire", src)
        self.assertIn("LockPolicy.BLOCK", src)
        self.assertNotIn("fcntl.flock", src)

    def test_lock_is_always_released(self):
        src = inspect.getsource(publication_module.identity_coordination_lock)
        self.assertIn("_release_identity_lock", src)
        release = inspect.getsource(publication_module._release_identity_lock)
        self.assertIn("capability.close", release)
        self.assertIn("directory.close", release)

    def test_lookup_happens_under_the_lock(self):
        src = inspect.getsource(publication_module.assemble_environment)
        with_lock = src.index("identity_coordination_lock")
        lookup = src.index("find_cached_result")
        assemble_call = src.index("assemble(")
        self.assertLess(with_lock, lookup)
        self.assertLess(lookup, assemble_call)


class TestNoPartialPublication(unittest.TestCase):
    def test_manifest_and_evidence_written_before_final_rename(self):
        src = inspect.getsource(publication_module._atomic_publish)
        rename = src.index("os.rename(str(tmp), str(final))")
        self.assertLess(src.index("_durable_write(tmp / MANIFEST_FILE"), rename)
        self.assertLess(src.index("_durable_write(tmp / EVIDENCE_FILE"), rename)

    def test_tree_fsync_before_final_rename(self):
        src = inspect.getsource(publication_module._atomic_publish)
        rename = src.index("os.rename(str(tmp), str(final))")
        self.assertLess(src.index("_fsync_tree(tmp / TREE_CHILD"), rename)
        self.assertLess(src.index("_fsync_dir(tmp)"), rename)

    def test_committed_output_created_only_by_atomic_rename(self):
        src = inspect.getsource(publication_module._atomic_publish)
        # The final path is never mkdir-ed or written incrementally.
        self.assertNotIn("os.mkdir(str(final)", src)
        self.assertIn("os.rename(str(tmp), str(final))", src)


class TestNoDeletionOfCommittedData(unittest.TestCase):
    def test_cleanup_removes_only_the_temporary_directory(self):
        src = inspect.getsource(publication_module._atomic_publish)
        self.assertIn("shutil.rmtree(str(tmp), ignore_errors=True)", src)
        self.assertNotIn("shutil.rmtree(str(final)", src)
        self.assertNotIn("os.unlink(str(final", src)
        self.assertNotIn("os.rmdir(str(final", src)

    def test_collision_is_verified_never_overwritten(self):
        src = inspect.getsource(publication_module._atomic_publish)
        self.assertIn("raise _AlreadyPublished()", src)
        publish = inspect.getsource(publication_module.publish_environment)
        self.assertIn("verify_output", publish)
        self.assertIn("output_collision_corrupt", publish)


class TestNoLockInversion(unittest.TestCase):
    def test_only_one_lock_is_acquired(self):
        src = inspect.getsource(publication_module)
        # The identity-coordination lock is the only lock, and it is the shared
        # capability rather than a second private flock.
        self.assertEqual(src.count("fcntl.flock("), 0)
        self.assertEqual(src.count("LockCapability.acquire("), 1)


class TestNoConsumerLeakage(unittest.TestCase):
    def test_result_and_evidence_fields_are_consumer_neutral(self):
        result_fields = {
            f.name for f in dataclasses.fields(evidence_module.AssemblyResult)
        }
        evidence_fields = {
            f.name for f in dataclasses.fields(evidence_module.AssemblerEvidence)
        }
        forbidden = (
            "launcher", "extension", "settings", "build_context",
            "cli_guard", "consumer", "runtime", "pi",
        )
        for name in result_fields | evidence_fields:
            lowered = name.lower()
            self.assertFalse(
                any(word in lowered for word in forbidden), name
            )


if __name__ == "__main__":
    unittest.main()
