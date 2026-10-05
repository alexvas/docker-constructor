"""Phase 9A — npm staging-tree cleanup precedence and observability.

``_atomic_publish`` must make an owned read-only temporary tree removable and
delete it without ever masking the in-flight publication failure.  Every
cleanup step is an independent action accumulated through ``CleanupFailures``;
only ``FileNotFoundError`` is idempotent absence, and unexpected defects or
process-control interruptions follow accumulator precedence.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR.parent))

from docker.npm_environment import (
    RootSpec,
    assembler_script_digest,
    compute_assembler_identity,
    compute_assembler_input_identity,
    npm_policy_digest,
    preflight,
    publication,
    publish_environment,
)
from docker.npm_environment.tree import build_tree_manifest
from docker.transactions.cleanup import CleanupFailures

_IMAGE = "sha256:" + "a" * 64
_NODE = "24.18.0"
_NPM = "11.16.0"
_PLATFORM = "linux-x64"
_SECONDARY_SLOT = "_transaction_secondary"


def _sri() -> str:
    return "sha512-" + base64.b64encode(bytes([0xAB]) * 64).decode()


def _url(name: str, version: str) -> str:
    stem = name.rsplit("/", 1)[-1]
    return f"https://registry.npmjs.org/{name}/-/{stem}-{version}.tgz"


def _lock() -> bytes:
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


def _secondary(exc: BaseException) -> tuple[BaseException, ...]:
    return tuple(getattr(exc, _SECONDARY_SLOT, ()) or ())


class _StagingCleanupTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="npm-env-phase9a-")
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
        self._counter = 0

    def _new_tree(self, marker: bytes = b"payload") -> Path:
        self._counter += 1
        root = self.base / f"tree-{self._counter}"
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

    def _tmp_leftovers(self) -> list[str]:
        return [
            p.name
            for p in self.namespace.outputs.iterdir()
            if p.name.startswith(".tmp-")
        ]

    def _interrupt_final_rename(self, exc: BaseException | None = None):
        """Fail the final publication rename (``.tmp-*`` -> final)."""
        real_rename = os.rename
        failure = exc if exc is not None else OSError("simulated interruption")

        def fake_rename(src, dst, *args, **kwargs):
            if os.path.basename(str(src)).startswith(".tmp-"):
                raise failure
            return real_rename(src, dst, *args, **kwargs)

        return mock.patch.object(publication.os, "rename", side_effect=fake_rename)


# ── _remove_owned_tree / _restore_write_access helper contracts ──────────


class TestOwnedTreeHelper(_StagingCleanupTestCase):
    def test_absence_is_idempotent(self):
        publication._remove_owned_tree(self.base / "already-gone")

    def test_file_not_found_from_rmtree_is_idempotent(self):
        with mock.patch.object(
            publication.shutil, "rmtree", side_effect=FileNotFoundError("gone")
        ):
            publication._remove_owned_tree(self.base / "whatever")

    def test_permission_failure_propagates(self):
        with mock.patch.object(
            publication.shutil, "rmtree", side_effect=PermissionError("denied")
        ):
            with self.assertRaises(PermissionError):
                publication._remove_owned_tree(self.base / "whatever")

    def test_only_the_given_root_is_removed(self):
        owned = self.base / "owned"
        owned.mkdir()
        (owned / "child.txt").write_text("x")
        sibling = self.base / "sibling"
        sibling.mkdir()
        (sibling / "keep.txt").write_text("keep")

        publication._remove_owned_tree(owned)

        self.assertFalse(owned.exists())
        self.assertTrue(sibling.exists())
        self.assertEqual((sibling / "keep.txt").read_text(), "keep")


class TestRestoreWriteAccessHelper(_StagingCleanupTestCase):
    def test_absence_is_idempotent(self):
        publication._restore_write_access(self.base / "missing", 0o300)

    def test_lstat_failure_propagates(self):
        with mock.patch.object(
            publication.os, "lstat", side_effect=PermissionError("lstat denied")
        ):
            with self.assertRaises(PermissionError):
                publication._restore_write_access(self.base, 0o300)

    def test_chmod_failure_propagates(self):
        target = self.base / "entry"
        target.write_text("x")
        with mock.patch.object(
            publication.os, "chmod", side_effect=PermissionError("chmod denied")
        ):
            with self.assertRaises(PermissionError):
                publication._restore_write_access(target, 0o200)

    def test_success_restores_owner_write_bits(self):
        target = self.base / "entry"
        target.write_text("x")
        os.chmod(target, 0o400)

        publication._restore_write_access(target, 0o200)

        self.assertEqual(os.stat(target).st_mode & 0o777, 0o600)


class TestMakeWritableActions(_StagingCleanupTestCase):
    def test_each_entry_is_an_independent_exactly_once_action(self):
        root = self.base / "readonly-tree"
        _write_tree(root, b"payload")
        manifest = build_tree_manifest(root)
        expected = {
            str(e.path)
            for e in manifest.entries
            if e.kind in ("file", "directory")
        } | {"."}

        seen: list[str] = []
        real = publication._restore_write_access

        def fake_restore(path, add):
            seen.append(str(Path(path).relative_to(root)))
            return real(path, add)

        with mock.patch.object(
            publication, "_restore_write_access", side_effect=fake_restore
        ):
            failures = CleanupFailures(None)
            publication._make_writable(root, manifest, failures)
            self.assertIsNone(failures.complete())

        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(set(seen), expected)

    def test_remaining_actions_run_after_one_entry_fails(self):
        root = self.base / "readonly-tree"
        _write_tree(root, b"payload")
        manifest = build_tree_manifest(root)
        expected = {
            str(e.path)
            for e in manifest.entries
            if e.kind in ("file", "directory")
        } | {"."}

        attempted: list[str] = []
        real = publication._restore_write_access

        def fake_restore(path, add):
            attempted.append(str(Path(path).relative_to(root)))
            if len(attempted) == 1:
                raise PermissionError("first chmod denied")
            return real(path, add)

        with mock.patch.object(
            publication, "_restore_write_access", side_effect=fake_restore
        ):
            failures = CleanupFailures(None)
            publication._make_writable(root, manifest, failures)
            result = failures.complete()

        self.assertIsInstance(result, PermissionError)
        self.assertEqual(len(attempted), len(expected))
        self.assertEqual(set(attempted), expected)


# ── _atomic_publish staging-tree cleanup precedence ─────────────────────


class TestPublicationFailurePrecedence(_StagingCleanupTestCase):
    def test_publication_failure_survives_successful_cleanup(self):
        with self._interrupt_final_rename():
            with self.assertRaises(OSError) as ctx:
                self._publish(b"payload")

        self.assertEqual(str(ctx.exception), "simulated interruption")
        self.assertEqual(_secondary(ctx.exception), ())
        self.assertEqual(self._tmp_leftovers(), [])

    def test_prior_committed_output_is_untouched(self):
        prior = self._publish(b"prior")
        prior_bytes = (
            prior.environment_root / "node_modules" / "a" / "marker.txt"
        ).read_bytes()

        with self._interrupt_final_rename():
            with self.assertRaises(OSError):
                self._publish(b"next")

        self.assertTrue(prior.environment_root.exists())
        self.assertEqual(
            (
                prior.environment_root / "node_modules" / "a" / "marker.txt"
            ).read_bytes(),
            prior_bytes,
        )


class TestCleanupAbsence(_StagingCleanupTestCase):
    def test_missing_owned_tree_during_cleanup_is_ignored(self):
        def fake_rmtree(path, *args, **kwargs):
            raise FileNotFoundError(path)

        with mock.patch.object(
            publication.shutil, "rmtree", side_effect=fake_rmtree
        ):
            with self._interrupt_final_rename():
                with self.assertRaises(OSError) as ctx:
                    self._publish(b"payload")

        self.assertEqual(str(ctx.exception), "simulated interruption")
        self.assertEqual(_secondary(ctx.exception), ())


class TestCleanupObservability(_StagingCleanupTestCase):
    def test_rmtree_permission_failure_is_secondary(self):
        with mock.patch.object(
            publication.shutil, "rmtree", side_effect=PermissionError("rmtree denied")
        ):
            with self._interrupt_final_rename():
                with self.assertRaises(OSError) as ctx:
                    self._publish(b"payload")

        self.assertEqual(str(ctx.exception), "simulated interruption")
        self.assertTrue(
            any("rmtree denied" in str(f) for f in _secondary(ctx.exception)),
            _secondary(ctx.exception),
        )

    def test_chmod_io_failure_is_secondary(self):
        with mock.patch.object(
            publication,
            "_restore_write_access",
            side_effect=OSError("chmod i/o denied"),
        ):
            with self._interrupt_final_rename():
                with self.assertRaises(OSError) as ctx:
                    self._publish(b"payload")

        self.assertEqual(str(ctx.exception), "simulated interruption")
        self.assertTrue(
            any("chmod i/o denied" in str(f) for f in _secondary(ctx.exception)),
            _secondary(ctx.exception),
        )


class TestAccumulatorPrecedence(_StagingCleanupTestCase):
    def test_unexpected_cleanup_defect_takes_precedence(self):
        with mock.patch.object(
            publication.shutil,
            "rmtree",
            side_effect=ValueError("unexpected cleanup defect"),
        ):
            with self._interrupt_final_rename():
                with self.assertRaises(ValueError) as ctx:
                    self._publish(b"payload")

        self.assertEqual(str(ctx.exception), "unexpected cleanup defect")
        # The displaced publication failure stays observable as secondary.
        self.assertTrue(
            any(isinstance(f, OSError) for f in _secondary(ctx.exception)),
            _secondary(ctx.exception),
        )

    def test_cleanup_interruption_takes_precedence(self):
        with mock.patch.object(
            publication.shutil, "rmtree", side_effect=KeyboardInterrupt()
        ):
            with self._interrupt_final_rename():
                with self.assertRaises(KeyboardInterrupt):
                    self._publish(b"payload")

    def test_publication_interruption_survives_cleanup_io_failure(self):
        interrupt = KeyboardInterrupt()
        with mock.patch.object(
            publication.shutil, "rmtree", side_effect=PermissionError("rmtree denied")
        ):
            with self._interrupt_final_rename(interrupt):
                with self.assertRaises(KeyboardInterrupt) as ctx:
                    self._publish(b"payload")

        self.assertIs(ctx.exception, interrupt)
        self.assertTrue(
            any("rmtree denied" in str(f) for f in _secondary(interrupt)),
            _secondary(interrupt),
        )


class TestCleanupActionIndependence(_StagingCleanupTestCase):
    def test_remove_still_runs_after_a_restore_action_fails(self):
        real_restore = publication._restore_write_access
        real_remove = publication._remove_owned_tree
        restore_paths: list[str] = []
        remove_paths: list[Path] = []

        def fake_restore(path, add):
            restore_paths.append(str(path))
            if len(restore_paths) == 1:
                raise PermissionError("first restore denied")
            return real_restore(path, add)

        def fake_remove(path):
            remove_paths.append(Path(path))
            return real_remove(path)

        with mock.patch.object(
            publication, "_restore_write_access", side_effect=fake_restore
        ), mock.patch.object(
            publication, "_remove_owned_tree", side_effect=fake_remove
        ):
            with self._interrupt_final_rename():
                with self.assertRaises(OSError) as ctx:
                    self._publish(b"payload")

        self.assertEqual(str(ctx.exception), "simulated interruption")
        # Every action ran once; the failure did not abort the sequence.
        self.assertEqual(len(restore_paths), len(set(restore_paths)))
        self.assertEqual(len(remove_paths), 1)
        self.assertTrue(remove_paths[0].name.startswith(".tmp-"))
        self.assertTrue(
            any("first restore denied" in str(f) for f in _secondary(ctx.exception)),
            _secondary(ctx.exception),
        )
