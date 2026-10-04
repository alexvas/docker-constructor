"""Phase 2 — validation under normal and ``0700`` parent paths (task 2.8).

The cache-security, tree-evidence, and corruption primitives are exercised
end-to-end beneath a normal (``0755``) and an owner-only (``0700``) parent
so ambient parent permissions cannot change assembler namespace privacy,
tree-manifest agreement, or corruption detection.
"""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from docker.npm_environment import LockedNpmError
from docker.npm_environment import storage, tree
from docker.npm_environment.publication import identity_coordination_lock


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


class TestPhase2Validation(unittest.TestCase):
    def _run_under(self, parent_mode: int) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="npm-env-validate-")
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        os.chmod(base, parent_mode)

        cache_root = base / "cache"
        cache_root.mkdir()
        os.chmod(cache_root, 0o700)

        ns = storage.prepare_assembler_namespace(cache_root, "a" * 64)
        self.assertEqual(_mode(ns.root), 0o700)
        self.assertEqual(_mode(ns.npm_cache), 0o700)
        self.assertEqual(_mode(ns.locks), 0o700)
        self.assertEqual(_mode(ns.staging), 0o700)

        lock = ns.locks / ("b" * 64 + ".lock")
        with identity_coordination_lock(ns, "b" * 64):
            self.assertEqual(_mode(lock), 0o600)

        workspace = storage.prepare_staging_workspace(ns, "run")
        self.assertEqual(_mode(workspace), 0o700)

        (workspace / "f").write_text("payload")
        manifest = tree.build_tree_manifest(workspace)
        tree.verify_tree(workspace, manifest)

        (workspace / "f").write_text("tampered")
        with self.assertRaises(LockedNpmError) as ctx:
            tree.verify_tree(workspace, manifest)
        self.assertEqual(ctx.exception.reason, "tree_hash_mismatch")

    def test_normal_parent_path(self) -> None:
        self._run_under(0o755)

    def test_owner_only_parent_path(self) -> None:
        self._run_under(0o700)


if __name__ == "__main__":
    unittest.main()
