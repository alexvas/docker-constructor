"""Phase 5 — atomic immutable publication contracts (task 5.3).

Publication must be durable-before-rename, publish immutable (write-bit-free)
trees, never overwrite a pre-existing output identity, clean up its
temporary directory on interruption, and preserve every prior committed
generation.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR.parent))

from docker.npm_environment import (
    LockedNpmError,
    RootSpec,
    assembler_script_digest,
    compute_assembler_identity,
    compute_assembler_input_identity,
    npm_policy_digest,
    preflight,
    publication,
    publish_environment,
)

_IMAGE = "sha256:" + "a" * 64
_NODE = "24.18.0"
_NPM = "11.16.0"
_PLATFORM = "linux-x64"


def _sri() -> str:
    return "sha512-" + base64.b64encode(bytes([0xAB]) * 64).decode()


def _url(name: str, version: str) -> str:
    stem = name.rsplit("/", 1)[-1]
    return f"https://registry.npmjs.org/{name}/-/{stem}-{version}.tgz"


def _lock(marker_name: str = "a") -> bytes:
    return json.dumps(
        {
            "name": "root",
            "version": "1.0.0",
            "lockfileVersion": 3,
            "requires": True,
            "packages": {
                "": {
                    "name": "root",
                    "version": "1.0.0",
                    "dependencies": {"a": "1.0.0"},
                },
                "node_modules/a": {
                    "version": "1.0.0",
                    "resolved": _url("a", "1.0.0"),
                    "integrity": _sri(),
                },
            },
        }
    ).encode()


def _validated():
    return preflight(
        _lock(),
        roots=(RootSpec("a", "1.0.0"),),
        platform=_PLATFORM,
        node_version=_NODE,
        npm_version=_NPM,
    )


def _assembler():
    return compute_assembler_identity(
        image_digest=_IMAGE,
        node_version=_NODE,
        npm_version=_NPM,
        script_digest=assembler_script_digest(),
        policy_digest=npm_policy_digest(),
        platform=_PLATFORM,
    )


def _write_pkg(root: Path, path: str, name: str, version: str) -> None:
    p = root / path / "package.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"name": name, "version": version}))


def _write_tree(root: Path, marker: bytes) -> None:
    _write_pkg(root, "node_modules/a", "a", "1.0.0")
    _write_pkg(root, "", "root", "1.0.0")
    (root / "node_modules" / "a" / "marker.txt").write_bytes(marker)


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def _no_write_bits(path: Path) -> bool:
    return _mode(path) & 0o222 == 0


class _PublicationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="npm-env-publish-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.cache_root = self.base / "cache"
        self.cache_root.mkdir()
        self.validated = _validated()
        self.assembler = _assembler()
        self.namespace = publication.prepare_assembler_namespace(
            self.cache_root, self.assembler.digest
        )
        self.input_identity = compute_assembler_input_identity(
            self.validated, self.assembler
        )
        self._tree_counter = 0

    def _new_tree(self, marker: bytes = b"payload") -> Path:
        self._tree_counter += 1
        root = self.base / f"tree-{self._tree_counter}"
        root.mkdir()
        _write_tree(root, marker)
        return root

    def _publish(self, marker: bytes = b"payload"):
        return publish_environment(
            validated=self.validated,
            tree_root=self._new_tree(marker),
            namespace=self.namespace,
            input_identity=self.input_identity,
        )


class TestImmutableModes(_PublicationTestCase):
    def test_published_tree_and_output_are_read_only(self):
        result = self._publish(b"immutable")
        env_root = result.environment_root
        self.assertTrue(_no_write_bits(env_root))
        self.assertTrue(_no_write_bits(env_root.parent))  # outputs/<identity>
        self.assertTrue(_no_write_bits(env_root / "node_modules" / "a"))
        self.assertTrue(_no_write_bits(env_root / "node_modules" / "a" / "marker.txt"))
        self.assertTrue(_no_write_bits(result.evidence_path))


class TestCollisionHandling(_PublicationTestCase):
    def test_identical_content_returns_existing_without_overwrite(self):
        first = self._publish(b"same")
        second = self._publish(b"same")
        self.assertEqual(second.output_identity, first.output_identity)
        self.assertEqual(second.environment_root, first.environment_root)
        # The prior generation's bytes are untouched.
        self.assertEqual(
            (first.environment_root / "node_modules" / "a" / "marker.txt").read_bytes(),
            b"same",
        )

    def test_corrupt_collision_is_recovered_by_reconstruction(self):
        first = self._publish(b"same")
        os.chmod(first.environment_root, 0o700)
        (first.environment_root / "SENTINEL").write_text("corruption")

        second = self._publish(b"same")

        # The reconstruction republishes at the same identity, fully verified.
        self.assertEqual(second.output_identity, first.output_identity)
        self.assertIsNotNone(
            publication.verify_output(
                self.namespace,
                second.output_identity,
                input_identity=self.input_identity,
            )
        )
        # The corrupt candidate was not reused: the replacement is clean.
        self.assertFalse((second.environment_root / "SENTINEL").exists())
        self.assertEqual(
            (
                second.environment_root / "node_modules" / "a" / "marker.txt"
            ).read_bytes(),
            b"same",
        )
        # The corrupt bytes are preserved in a quarantine directory.
        quarantined = [
            p
            for p in self.namespace.outputs.iterdir()
            if p.name.startswith(".corrupt-")
        ]
        self.assertEqual(len(quarantined), 1)
        self.assertTrue((quarantined[0] / "tree" / "SENTINEL").exists())

    def test_corrupt_recovery_preserves_unrelated_output(self):
        one = self._publish(b"one")
        two = self._publish(b"two")
        two_evidence = (
            self.namespace.outputs / two.output_identity / "evidence.json"
        ).read_bytes()

        os.chmod(one.environment_root, 0o700)
        (one.environment_root / "SENTINEL").write_text("corruption")

        recovered = self._publish(b"one")

        self.assertEqual(recovered.output_identity, one.output_identity)
        # The unrelated, previously valid output is untouched.
        self.assertEqual(
            (two.environment_root / "node_modules" / "a" / "marker.txt").read_bytes(),
            b"two",
        )
        self.assertEqual(
            (
                self.namespace.outputs / two.output_identity / "evidence.json"
            ).read_bytes(),
            two_evidence,
        )

    def test_quarantine_failure_preserves_corrupt_output(self):
        one = self._publish(b"one")
        two = self._publish(b"two")
        two_evidence = (
            self.namespace.outputs / two.output_identity / "evidence.json"
        ).read_bytes()

        os.chmod(one.environment_root, 0o700)
        (one.environment_root / "SENTINEL").write_text("corruption")

        with mock.patch.object(
            publication, "_quarantine_corrupt_output", return_value=None
        ):
            with self.assertRaises(LockedNpmError) as ctx:
                self._publish(b"one")

        self.assertEqual(ctx.exception.reason, "output_collision_corrupt")
        # The corrupt output is preserved in place, never overwritten.
        self.assertTrue((one.environment_root / "SENTINEL").exists())
        # The unrelated valid output is untouched.
        self.assertEqual(
            (
                self.namespace.outputs / two.output_identity / "evidence.json"
            ).read_bytes(),
            two_evidence,
        )

    def test_replacement_failure_preserves_quarantined_output(self):
        one = self._publish(b"one")
        two = self._publish(b"two")
        two_evidence = (
            self.namespace.outputs / two.output_identity / "evidence.json"
        ).read_bytes()

        os.chmod(one.environment_root, 0o700)
        (one.environment_root / "SENTINEL").write_text("corruption")

        with mock.patch.object(
            publication,
            "_atomic_publish",
            side_effect=[publication._AlreadyPublished(), OSError("replace failed")],
        ):
            with self.assertRaises(OSError):
                self._publish(b"one")

        # The corrupt bytes survive at the quarantine path.
        quarantined = [
            p
            for p in self.namespace.outputs.iterdir()
            if p.name.startswith(".corrupt-")
        ]
        self.assertEqual(len(quarantined), 1)
        self.assertTrue((quarantined[0] / "tree" / "SENTINEL").exists())
        # The unrelated valid output is untouched.
        self.assertEqual(
            (
                self.namespace.outputs / two.output_identity / "evidence.json"
            ).read_bytes(),
            two_evidence,
        )


class TestDurabilityBeforeRename(_PublicationTestCase):
    def test_fsync_precedes_final_rename(self):
        events: list[str] = []
        real_fsync = os.fsync
        real_rename = os.rename

        def fake_fsync(fd):
            events.append("fsync")
            return real_fsync(fd)

        tree = self._new_tree(b"durable")
        # Interrupt only the final publication rename (the second ``os.rename``
        # inside ``_atomic_publish``).
        rename_count = 0

        def fake_rename(src, dst, *args, **kwargs):
            nonlocal rename_count
            rename_count += 1
            src_name = os.path.basename(str(src))
            if src_name.startswith(".tmp-") and rename_count == 2:
                events.append("rename-final")
                raise OSError("simulated interruption")
            events.append("rename-tree")
            return real_rename(src, dst, *args, **kwargs)

        with (
            mock.patch.object(publication.os, "fsync", side_effect=fake_fsync),
            mock.patch.object(publication.os, "rename", side_effect=fake_rename),
        ):
            with self.assertRaises(OSError):
                publish_environment(
                    validated=self.validated,
                    tree_root=tree,
                    namespace=self.namespace,
                    input_identity=self.input_identity,
                )

        self.assertIn("fsync", events)
        self.assertIn("rename-final", events)
        self.assertLess(events.index("fsync"), events.index("rename-final"))


class TestInterruptionCleanup(_PublicationTestCase):
    def test_interrupted_publication_cleans_temp_and_preserves_prior(self):
        prior = self._publish(b"prior")
        prior_bytes = (
            prior.environment_root / "node_modules" / "a" / "marker.txt"
        ).read_bytes()

        tree = self._new_tree(b"next")
        real_rename = os.rename
        rename_count = 0

        def fake_rename(src, dst, *args, **kwargs):
            nonlocal rename_count
            rename_count += 1
            if os.path.basename(str(src)).startswith(".tmp-") and rename_count == 2:
                raise OSError("simulated interruption")
            return real_rename(src, dst, *args, **kwargs)

        with mock.patch.object(publication.os, "rename", side_effect=fake_rename):
            with self.assertRaises(OSError):
                publish_environment(
                    validated=self.validated,
                    tree_root=tree,
                    namespace=self.namespace,
                    input_identity=self.input_identity,
                )

        # No temporary publication directory remains.
        leftovers = [
            p.name
            for p in self.namespace.outputs.iterdir()
            if p.name.startswith(".tmp-")
        ]
        self.assertEqual(leftovers, [])
        # The prior committed generation is untouched.
        self.assertTrue(prior.environment_root.exists())
        self.assertEqual(
            (prior.environment_root / "node_modules" / "a" / "marker.txt").read_bytes(),
            prior_bytes,
        )


class TestPriorGenerationPreservation(_PublicationTestCase):
    def test_new_generation_preserves_prior_generation(self):
        first = self._publish(b"first")
        second = self._publish(b"second")
        self.assertNotEqual(first.output_identity, second.output_identity)
        self.assertEqual(
            (first.environment_root / "node_modules" / "a" / "marker.txt").read_bytes(),
            b"first",
        )
        self.assertEqual(
            (
                second.environment_root / "node_modules" / "a" / "marker.txt"
            ).read_bytes(),
            b"second",
        )
        self.assertTrue(first.environment_root.exists())


class TestPartialWritePublication(_PublicationTestCase):
    def test_partial_writes_publish_complete_files(self):
        # Simulate a writer that accepts at most 7 bytes per ``os.write``
        # call, forcing ``_durable_write``'s loop to keep writing until the
        # complete manifest/evidence payloads reach the filesystem.
        real_write = os.write

        def partial_write(fd, data):
            return real_write(fd, data[:7])

        tree = self._new_tree(b"payload")
        with mock.patch.object(publication.os, "write", side_effect=partial_write):
            result = publish_environment(
                validated=self.validated,
                tree_root=tree,
                namespace=self.namespace,
                input_identity=self.input_identity,
            )

        out = self.namespace.outputs / result.output_identity
        manifest_bytes = (out / "manifest.json").read_bytes()
        evidence_bytes = (out / "evidence.json").read_bytes()

        parsed_manifest = publication.parse_manifest(manifest_bytes)
        self.assertEqual(parsed_manifest.digest, result.tree_digest)
        self.assertEqual(
            publication.serialize_manifest(parsed_manifest), manifest_bytes
        )

        parsed_evidence = publication.parse_evidence(evidence_bytes)
        self.assertEqual(parsed_evidence.output_identity, result.output_identity)
        self.assertEqual(
            publication.serialize_evidence(parsed_evidence), evidence_bytes
        )


if __name__ == "__main__":
    unittest.main()
