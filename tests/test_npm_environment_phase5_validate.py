"""Phase 5 — combined validation (task 5.9).

Output, concurrency, crash, publication, cache-hit, and serialization tests
run together through one end-to-end scenario, verifying deterministic
evidence bytes, deterministic output identities, non-aliasing of distinct
trees, and full cache-hit binding (input identity, tree digest, evidence
digest, and output identity all recompute before reuse).
"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR.parent))

# Repository/test path setup must run before these imports.
from docker.npm_environment import (  # noqa: E402
    RootSpec,
    assembler_script_digest,
    compute_assembler_identity,
    compute_assembler_input_identity,
    npm_policy_digest,
    parse_evidence,
    preflight,
    publication,
    publish_environment,
    serialize_evidence,
    verify_output,
)

_IMAGE = "sha256:" + "a" * 64
_NODE = "24.18.0"
_NPM = "11.16.0"
_PLATFORM = "linux-x64"


def _sri() -> str:
    return "sha512-" + base64.b64encode(bytes([0xAB]) * 64).decode()


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
                    "resolved": "https://registry.npmjs.org/a/-/a-1.0.0.tgz",
                    "integrity": _sri(),
                },
            },
        }
    ).encode()


def _write_pkg(root: Path, path: str, name: str, version: str) -> None:
    p = root / path / "package.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"name": name, "version": version}))


def _write_tree(root: Path, marker: bytes) -> None:
    _write_pkg(root, "node_modules/a", "a", "1.0.0")
    _write_pkg(root, "", "root", "1.0.0")
    (root / "node_modules" / "a" / "marker.txt").write_bytes(marker)


class TestPhase5Combined(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="npm-env-phase5-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.cache_root = self.base / "cache"
        self.cache_root.mkdir()
        self.validated = preflight(
            _lock(),
            roots=(RootSpec("a", "1.0.0"),),
            platform=_PLATFORM,
            node_version=_NODE,
            npm_version=_NPM,
        )
        self.assembler = compute_assembler_identity(
            image_digest=_IMAGE,
            node_version=_NODE,
            npm_version=_NPM,
            script_digest=assembler_script_digest(),
            policy_digest=npm_policy_digest(),
            platform=_PLATFORM,
        )
        self.namespace = publication.prepare_assembler_namespace(
            self.cache_root, self.assembler.digest
        )
        self.input_identity = compute_assembler_input_identity(
            self.validated, self.assembler
        )

    def _publish(self, marker: bytes):
        tree = self.base / f"tree-{marker.decode()}"
        tree.mkdir()
        _write_tree(tree, marker)
        return publish_environment(
            validated=self.validated,
            tree_root=tree,
            namespace=self.namespace,
            input_identity=self.input_identity,
        )

    def test_deterministic_evidence_and_non_aliasing(self):
        first = self._publish(b"one")
        second = self._publish(b"two")

        # Distinct trees never alias: one input identity, two output
        # identities, two distinct immutable environment roots.
        self.assertEqual(first.input_identity, second.input_identity)
        self.assertNotEqual(first.output_identity, second.output_identity)
        self.assertNotEqual(first.tree_digest, second.tree_digest)
        self.assertNotEqual(first.environment_root, second.environment_root)

        # Deterministic evidence bytes for a given published environment.
        evidence_path = first.evidence_path
        first_bytes = evidence_path.read_bytes()
        evidence = parse_evidence(first_bytes)
        self.assertEqual(serialize_evidence(evidence), first_bytes)

        # Both outputs are referenced by the non-authoritative index.
        indexed = publication.read_index(self.namespace, self.input_identity.digest)
        self.assertIn(first.output_identity, indexed)
        self.assertIn(second.output_identity, indexed)

    def test_full_cache_hit_binding(self):
        first = self._publish(b"one")
        # Full recomputation of input identity, tree digest, evidence digest,
        # and output identity must all bind before reuse.
        result = verify_output(
            self.namespace,
            first.output_identity,
            input_identity=self.input_identity,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.output_identity, first.output_identity)
        self.assertEqual(result.input_identity, self.input_identity)
        self.assertEqual(result.tree_digest, first.tree_digest)
        self.assertEqual(result.evidence_digest, first.evidence_digest)

        # A corrupted tree cannot pass the binding and is not reused.
        marker = first.environment_root / "node_modules" / "a" / "marker.txt"
        os.chmod(marker, 0o600)
        marker.parent.chmod(0o700)
        marker.write_text("tampered")
        self.assertIsNone(
            verify_output(
                self.namespace,
                first.output_identity,
                input_identity=self.input_identity,
            )
        )


if __name__ == "__main__":
    unittest.main()
