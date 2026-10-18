"""External constructor-project build-cache path contracts."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from docker.versioning.build_cache import (
    BuildCacheError,
    build_blob_path,
    prepare_build_cache,
    resolve_build_blobs_root,
    resolve_build_cache_root,
    resolve_build_generated_root,
    resolve_build_tmp_root,
)
from docker.versioning.digest_identity import DigestIdentity
from docker.versioning.project_state import ProjectStateError, resolve_project_state


class Paths(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.checkout = self.base / "checkout"
        self.checkout.mkdir()
        self.cache = self.base / "cache"
        self.cache.mkdir(mode=0o700)
        self.state = resolve_project_state(self.checkout, cache_root=self.cache)

    def test_all_build_paths_are_children_of_resolved_project_state(self):
        self.assertEqual(
            resolve_build_cache_root(self.checkout, cache_root=self.cache),
            self.state.build_artifacts_root,
        )
        self.assertEqual(
            resolve_build_blobs_root(self.checkout, cache_root=self.cache),
            self.state.build_artifacts_root / "blobs",
        )
        self.assertEqual(
            resolve_build_tmp_root(self.checkout, cache_root=self.cache),
            self.state.build_artifacts_root / "tmp",
        )
        self.assertEqual(
            resolve_build_generated_root(self.checkout, cache_root=self.cache),
            self.state.transactions_root,
        )
        identity = DigestIdentity.from_hex(
            "sha256", hashlib.sha256(b"blob").hexdigest()
        )
        self.assertTrue(
            build_blob_path(
                self.state.build_artifacts_root / "blobs", identity
            ).is_relative_to(self.state.build_artifacts_root)
        )

    def test_preparation_creates_private_external_children_only(self):
        paths = prepare_build_cache(self.checkout, cache_root=self.cache)
        self.assertEqual(paths.constructor_project_root, self.checkout.resolve())
        self.assertEqual(paths.namespace_root, self.state.namespace)
        for path in (
            paths.namespace_root,
            paths.persistent_root,
            paths.blobs_root,
            paths.tmp_root,
            paths.generated_root,
            paths.markers_root,
        ):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
            self.assertTrue(path.is_relative_to(self.state.namespace))
        self.assertFalse((self.checkout / ".docker-cache").exists())
        self.assertFalse((self.checkout / ".docker-generated").exists())

    def test_external_symlink_and_foreign_ownership_are_rejected_before_mutation(self):
        self.state.build_artifacts_root.rmdir()
        outside = self.base / "outside"
        outside.mkdir()
        self.state.build_artifacts_root.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(BuildCacheError):
            prepare_build_cache(self.checkout, cache_root=self.cache)
        self.assertEqual(list(outside.iterdir()), [])
        self.state.build_artifacts_root.unlink()
        self.state.build_artifacts_root.mkdir(mode=0o700)
        real_fstat = os.fstat
        target = str(self.state.build_artifacts_root)

        def foreign(fd, *args, **kwargs):
            result = real_fstat(fd, *args, **kwargs)
            try:
                name = os.path.normpath(os.readlink(f"/proc/self/fd/{fd}"))
            except OSError:
                name = ""
            return (
                mock.Mock(st_mode=result.st_mode, st_uid=os.geteuid() + 1)
                if name == target
                else result
            )

        with mock.patch("os.fstat", foreign):
            with self.assertRaises(BuildCacheError):
                prepare_build_cache(self.checkout, cache_root=self.cache)

    def test_canonical_project_alias_uses_one_namespace_and_identity(self):
        alias = self.base / "checkout-alias"
        alias.symlink_to(self.checkout, target_is_directory=True)
        via_alias = resolve_project_state(alias, cache_root=self.cache)
        self.assertEqual(via_alias.project_path, self.checkout.resolve())
        self.assertEqual(via_alias.identity, self.state.identity)
        self.assertEqual(via_alias.namespace, self.state.namespace)
        self.assertEqual(
            via_alias.build_artifacts_root, self.state.build_artifacts_root
        )
        self.assertEqual(via_alias.transactions_root, self.state.transactions_root)

    def test_same_basename_projects_are_collision_isolated(self):
        other_parent = self.base / "other"
        other_parent.mkdir()
        other = other_parent / "checkout"
        other.mkdir()
        other_state = resolve_project_state(other, cache_root=self.cache)
        self.assertNotEqual(other_state.identity, self.state.identity)
        self.assertNotEqual(other_state.namespace, self.state.namespace)
        self.assertTrue(other_state.namespace.name.startswith("checkout-"))

    def test_malformed_or_mismatched_namespace_metadata_is_not_adopted(self):
        metadata = self.state.namespace / "project.json"
        for payload in (
            b"not json\n",
            b'{"version":1}\n',
            b'{"canonical_path":"/wrong","sha256":"bad","version":1}\n',
        ):
            metadata.write_bytes(payload)
            os.chmod(metadata, 0o600)
            before = metadata.read_bytes()
            with self.assertRaises(ProjectStateError):
                resolve_project_state(self.checkout, cache_root=self.cache)
            self.assertEqual(metadata.read_bytes(), before)
        # Restore the verified metadata for cleanup and later assertions.
        metadata.write_text(
            '{"canonical_path":"%s","sha256":"%s","version":1}\n'
            % (self.checkout.resolve(), self.state.identity)
        )
        os.chmod(metadata, 0o600)

    def test_unsupported_identity_is_rejected_before_path_derivation(self):
        with self.assertRaises(ValueError):
            DigestIdentity.from_hex("not-a-digest", "00" * 32)

    def test_equal_identities_deduplicate_to_one_external_blob_path(self):
        data = b"deduplicated"
        first = DigestIdentity.from_hex("sha256", hashlib.sha256(data).hexdigest())
        second = DigestIdentity.from_hex(
            "sha256", hashlib.sha256(data).hexdigest().upper()
        )
        paths = prepare_build_cache(self.checkout, cache_root=self.cache)
        self.assertEqual(
            build_blob_path(paths.blobs_root, first),
            build_blob_path(paths.blobs_root, second),
        )

    def test_constructor_project_root_round_trips_as_project_identity_without_nested_namespace(
        self,
    ):
        from docker.versioning.build_cache import publish_verified_blob

        paths = prepare_build_cache(self.checkout, cache_root=self.cache)
        data = b"round-trip"
        identity = DigestIdentity.from_hex("sha256", hashlib.sha256(data).hexdigest())
        blob = publish_verified_blob(
            identity,
            data,
            constructor_project_root=paths.constructor_project_root,
            cache_root=self.cache,
        )
        self.assertTrue(blob.is_relative_to(self.state.namespace))
        self.assertEqual(
            list((self.cache / "projects").iterdir()), [self.state.namespace]
        )
        self.assertFalse((self.state.namespace / "projects").exists())

    def test_one_project_yields_exactly_one_namespace_and_no_nested_namespace(self):
        from docker.versioning.build_cache import publish_verified_blob

        data = b"exactly-one-namespace"
        identity = DigestIdentity.from_hex("sha256", hashlib.sha256(data).hexdigest())
        publish_verified_blob(
            identity,
            data,
            constructor_project_root=self.checkout,
            cache_root=self.cache,
        )
        projects = self.cache / "projects"
        # One constructor project creates exactly one namespace under the
        # selected cache root (never split across two namespaces).
        self.assertEqual(list(projects.iterdir()), [self.state.namespace])
        self.assertEqual(self.state.namespace.parent, projects)
        # No namespace (or nested ``projects`` child) is created for
        # namespace_root itself.
        self.assertFalse((self.state.namespace / "projects").exists())
        self.assertEqual(
            {p.name for p in self.state.namespace.iterdir()},
            {
                "project.json",
                "generated",
                "runtime",
                "evidence",
                "build-artifacts",
                "transactions",
            },
        )

    def test_legacy_trees_are_never_read_adopted_modified_or_deleted(self):
        legacy_cache = self.checkout / ".docker-cache"
        legacy_generated = self.checkout / ".docker-generated"
        legacy_cache.mkdir()
        legacy_generated.mkdir()
        cache_sentinel = legacy_cache / "sentinel"
        generated_sentinel = legacy_generated / "sentinel"
        cache_sentinel.write_text("cache")
        generated_sentinel.write_text("generated")
        before = (cache_sentinel.stat().st_ino, generated_sentinel.stat().st_ino)
        prepare_build_cache(self.checkout, cache_root=self.cache)
        self.assertEqual(
            (cache_sentinel.read_text(), generated_sentinel.read_text()),
            ("cache", "generated"),
        )
        self.assertEqual(
            (cache_sentinel.stat().st_ino, generated_sentinel.stat().st_ino), before
        )


class LegacyAndWorkspaceNeutralityTests(unittest.TestCase):
    def test_renderer_rejects_checkout_local_runtime_projection(self):
        from docker.versioning.rendering import RunRenderInputs, render_run_vector

        with self.assertRaisesRegex(ValueError, "external project-state"):
            render_run_vector(
                RunRenderInputs(
                    image="image",
                    container_name="pi-1",
                    pi_home_host="/home/user/.pi",
                    projection_host_path="/work/constructor/.docker-generated/runtime/p.toml",
                    projection_container_path="/run/pi-cli/docker-constructor.runtime.toml",
                    workspace="/work/primary",
                    project_state_runtime_root="/cache/projects/selected/runtime",
                )
            )
