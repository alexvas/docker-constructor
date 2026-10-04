"""Phase 7 task 7.4 — runtime artifact publication stays a specialized L3 protocol.

The runtime content-addressed blob publisher is *not* migrated to the L2
regular-file contracts.  Digest (SRI) identity, content-addressed path
derivation, collision/revalidation, quarantine, permission policy, and
cleanup remain owned by the runtime artifact cache; only the per-identity
``BLOCK`` lock is shared.

These tests pin the specialization boundary: the pipeline validates SRI
before any publication, publishes through the domain ``CacheFilesystem``
boundary rather than ``RegularFileContracts``, revalidates the published
blob, quarantines corrupt entries, and cleans temporary state — all without
granting L2 any blob identity, collision, or lifecycle authority.
"""
from __future__ import annotations

import inspect
import os
import tempfile
import unittest

from docker.versioning import artifact_cache
from docker.versioning.artifact_cache import (
    CacheFilesystem,
    LocalCacheFilesystem,
    SelectedArtifact,
    derive_cache_path,
    materialize_selected_artifacts,
)
from docker.versioning.digest_identity import DigestIdentity
from tests.test_constructor_materialization import (
    _FakeFilesystem,
    _FakeLockFactory,
    _FakeTempDir,
    _FakeTransport,
    _make_integrity_for,
)

_PAYLOAD = b"runtime-artifact-specialization-payload"
_INTEGRITY = _make_integrity_for(_PAYLOAD)
_URL = "https://x.test/pkg.tgz"


class SpecializationBoundaryTests(unittest.TestCase):
    def test_blob_publication_is_not_the_l2_regular_file_contract(self) -> None:
        # The runtime artifact cache must not adopt the shared regular-file
        # contract as its blob publisher.
        self.assertFalse(hasattr(artifact_cache, "RegularFileContracts"))
        source = inspect.getsource(LocalCacheFilesystem.atomic_publish)
        self.assertIn("os.replace", source)
        self.assertNotIn("atomic_no_clobber", source)
        # The domain protocol still owns the complete publication surface.
        required = {
            "atomic_publish", "quarantine_or_remove", "cleanup_temp",
            "finalize_temp", "inspect_and_digest", "set_permissions",
        }
        self.assertTrue(required.issubset(set(dir(CacheFilesystem))))

    def test_blob_identity_is_derived_from_sri(self) -> None:
        identity = DigestIdentity.from_sri(_INTEGRITY)
        path = derive_cache_path(
            identity.algorithm, identity.runtime_safe_digest(), root="/cache",
        )
        self.assertTrue(path.startswith("/cache/sha512/"))
        self.assertTrue(path.endswith(".tgz"))


class MaterializationSpecializationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache_root = os.path.join(self.tmp.name, "cache")
        os.makedirs(self.cache_root, mode=0o700)
        self.temp_root = os.path.join(self.tmp.name, "tmp")
        os.makedirs(self.temp_root, mode=0o700)

    def _materialize(
        self,
        *,
        filesystem: CacheFilesystem | None = None,
        transport=None,
    ):
        return materialize_selected_artifacts(
            [SelectedArtifact(_URL, _INTEGRITY)],
            transport=transport or _FakeTransport({_URL: _PAYLOAD}),
            filesystem=filesystem or _FakeFilesystem(),
            lock_factory=_FakeLockFactory(),
            temp_dir=_FakeTempDir(),
            cache_root=self.cache_root,
            temp_root=self.temp_root,
        )

    def test_malformed_integrity_is_rejected_before_publication(self) -> None:
        filesystem = _FakeFilesystem()
        transport = _FakeTransport({})
        from docker.versioning.artifact_cache import ArtifactMaterializationError

        with self.assertRaises(ArtifactMaterializationError) as ctx:
            materialize_selected_artifacts(
                [SelectedArtifact(_URL, "not-an-sri")],
                transport=transport,
                filesystem=filesystem,
                lock_factory=_FakeLockFactory(),
                temp_dir=_FakeTempDir(),
                cache_root=self.cache_root,
                temp_root=self.temp_root,
            )
        self.assertEqual(ctx.exception.reason, "integrity")
        self.assertEqual(transport.calls, [])
        self.assertEqual(filesystem.published, [])

    def test_publication_is_content_addressed_and_revalidated(self) -> None:
        from unittest import mock

        filesystem = _FakeFilesystem()
        transport = _FakeTransport({_URL: _PAYLOAD})
        with mock.patch.object(
            artifact_cache, "_validate_published_blob", wraps=artifact_cache._validate_published_blob,
        ) as revalidated:
            results = self._materialize(filesystem=filesystem, transport=transport)
        blob = results[_INTEGRITY]
        self.assertEqual(blob.integrity, _INTEGRITY)
        self.assertEqual(len(filesystem.published), 1)
        temp_path, final_path = filesystem.published[0]
        self.assertNotEqual(temp_path, final_path)
        self.assertIn(blob.digest, final_path)
        # The domain revalidates the published entry through its own boundary.
        self.assertEqual(revalidated.call_count, 1)
        self.assertEqual(revalidated.call_args.args[0], final_path)

    def test_corrupt_entry_is_quarantined_then_replaced(self) -> None:
        identity = DigestIdentity.from_sri(_INTEGRITY)
        path = derive_cache_path(
            identity.algorithm, identity.runtime_safe_digest(), root=self.cache_root,
        )
        filesystem = _FakeFilesystem(
            exists={path}, contents={path: b"corrupt"},
        )
        self._materialize(filesystem=filesystem)
        self.assertEqual(filesystem.quarantined, [path])
        self.assertEqual(len(filesystem.published), 1)

    def test_cleanup_remains_domain_owned(self) -> None:
        from docker.versioning.artifact_cache import ArtifactMaterializationError

        filesystem = _FakeFilesystem()
        transport = _FakeTransport({_URL: b"other-bytes"})
        with self.assertRaises(ArtifactMaterializationError) as ctx:
            self._materialize(filesystem=filesystem, transport=transport)
        self.assertEqual(ctx.exception.reason, "integrity")
        self.assertEqual(filesystem.published, [])

    def test_production_publication_still_uses_path_replace(self) -> None:
        # The production blob publisher remains a path-based domain protocol.
        source = inspect.getsource(LocalCacheFilesystem.atomic_publish)
        self.assertIn("_open_parent", source)
        self.assertIn("os.replace", source)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
