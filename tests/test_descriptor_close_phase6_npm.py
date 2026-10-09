"""Phase 6 (task 6.1) — npm durable-publication descriptor ownership.

RED before migration: ``_durable_write``, ``_fsync_dir``, ``_fsync_tree``, and
``_atomic_publish`` released their raw ``os.open`` descriptors with a bare
``os.close`` inside ``CleanupFailures``.  Precedence was already preserved, but
there was no explicit terminal ownership record, so the migration contract
(terminal-before-close + at-most-once release observed on a shared
``OwnedDescriptor``) is the RED part.  Characterization of bytes, mode, and
publication visibility remains passing.
"""
from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from docker.npm_environment import publication
from tests.descriptor_ownership_support import recording, secondary_diagnostics


class _PublicationCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="phase6-npm-pub-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def _namespace(self):
        cache_root = self.root / "cache"
        cache_root.mkdir()
        return publication.prepare_assembler_namespace(cache_root, "a" * 64)


class DurableWriteContractTests(_PublicationCase):
    def test_bytes_and_mode_are_unchanged(self):
        path = self.root / "manifest.json"
        publication._durable_write(path, b"payload")
        self.assertEqual(path.read_bytes(), b"payload")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o444)

    def test_release_is_terminal_and_at_most_once(self):
        path = self.root / "manifest.json"
        with recording(publication) as rec:
            publication._durable_write(path, b"payload")
        rec.assert_terminal_and_at_most_once(self)
        self.assertEqual(len(rec.owners), 1)
        owner = rec.owners[0]
        self.assertTrue(owner.released)
        before = len(rec.close_calls)
        owner.close()  # repeated cleanup is a no-op
        self.assertEqual(len(rec.close_calls), before)

    def test_sole_close_failure_is_the_only_failure(self):
        path = self.root / "manifest.json"
        fault = OSError("close failed")
        with recording(publication, close_faults=[fault]) as rec:
            with self.assertRaises(OSError) as ctx:
                publication._durable_write(path, b"payload")
        self.assertIs(ctx.exception, fault)
        self.assertTrue(rec.released_before_close[0])

    def test_operation_failure_stays_primary_with_close_secondary(self):
        path = self.root / "manifest.json"
        write_fault = OSError("write failed")
        close_fault = OSError("close failed")
        with mock.patch.object(publication.os, "write", side_effect=write_fault):
            with recording(publication, close_faults=[close_fault]) as rec:
                with self.assertRaises(OSError) as ctx:
                    publication._durable_write(path, b"payload")
        self.assertIs(ctx.exception, write_fault)
        self.assertIn(close_fault, secondary_diagnostics(write_fault))
        rec.assert_terminal_and_at_most_once(self)


class FsyncDirectoryContractTests(_PublicationCase):
    def test_release_is_terminal_and_at_most_once(self):
        with recording(publication) as rec:
            publication._fsync_dir(self.root)
        rec.assert_terminal_and_at_most_once(self)

    def test_sole_close_failure_after_successful_fsync_propagates(self):
        fault = OSError("close failed")
        with recording(publication, close_faults=[fault]) as rec:
            with self.assertRaises(OSError) as ctx:
                publication._fsync_dir(self.root)
        self.assertIs(ctx.exception, fault)
        self.assertTrue(rec.released_before_close[0])


class FsyncTreeContractTests(_PublicationCase):
    def test_every_entry_and_root_release_is_terminal(self):
        tree = self.root / "tree"
        tree.mkdir()
        (tree / "file.txt").write_bytes(b"x")
        (tree / "sub").mkdir()
        (tree / "sub" / "inner.txt").write_bytes(b"y")
        manifest = publication.build_tree_manifest(tree)
        with recording(publication) as rec:
            publication._fsync_tree(tree, manifest)
        rec.assert_terminal_and_at_most_once(self)
        # one owner per manifest file/directory entry plus the root directory
        self.assertGreaterEqual(len(rec.owners), 4)


class AtomicPublishContractTests(_PublicationCase):
    def test_release_is_terminal_and_at_most_once(self):
        namespace = self._namespace()
        tree = self.root / "tree"
        tree.mkdir()
        (tree / "file.txt").write_bytes(b"payload")
        manifest = publication.build_tree_manifest(tree)
        with recording(publication) as rec:
            final = publication._atomic_publish(
                namespace, "b" * 64, tree, manifest, b"evidence"
            )
        rec.assert_terminal_and_at_most_once(self)
        self.assertTrue(final.exists())

    def test_sole_close_failure_after_publication_propagates(self):
        namespace = self._namespace()
        tree = self.root / "tree"
        tree.mkdir()
        (tree / "file.txt").write_bytes(b"payload")
        manifest = publication.build_tree_manifest(tree)
        fault = OSError("close failed")

        def fault_outputs(fd, owner):
            return fault if owner.label.startswith("publication outputs") else None

        with recording(publication, close_fault=fault_outputs) as rec:
            with self.assertRaises(OSError) as ctx:
                publication._atomic_publish(
                    namespace, "c" * 64, tree, manifest, b"evidence"
                )
        self.assertIs(ctx.exception, fault)
        rec.assert_terminal_and_at_most_once(self)
