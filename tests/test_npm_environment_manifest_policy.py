"""Phase 3 — synthesized manifest dependency policy.

The assembler script builds a project manifest in sync with the mounted
lockfile so ``npm ci`` reconstructs the validated closure.  It copies only
the dependency classes closure validation treats as *installed* root edges
(``dependencies``, ``devDependencies``, ``optionalDependencies``).  Root
``peerDependencies`` are excluded: validation leaves root peers to the
consumer environment rather than installing them, so copying them into the
manifest would make ``npm ci`` install or require an unvalidated package.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR.parent))

# Repository/test path setup must run before these imports.
from docker.npm_environment import (  # noqa: E402
    MANIFEST_DEPENDENCY_KEYS,
    RootSpec,
    assembler_script_bytes,
    preflight,
)

_NODE = "24.18.0"
_NPM = "11.16.0"
_PLATFORM = "linux-x64"


def _sri() -> str:
    import base64

    return "sha512-" + base64.b64encode(bytes([0xAB]) * 64).decode()


def _url(name: str, version: str) -> str:
    stem = name.rsplit("/", 1)[-1]
    return f"https://registry.npmjs.org/{name}/-/{stem}-{version}.tgz"


def _fixture_with_root_peers() -> bytes:
    """A lock whose root declares an unvalidated (not-in-closure) peer.

    ``peer-only`` appears only in the root's ``peerDependencies`` map and
    has no ``packages`` entry, so it is neither validated nor installed.
    """
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
                    "devDependencies": {"devtool": "2.0.0"},
                    "optionalDependencies": {"opt": "3.0.0"},
                    "peerDependencies": {"peer-only": "9.9.9"},
                },
                "node_modules/a": {
                    "version": "1.0.0",
                    "resolved": _url("a", "1.0.0"),
                    "integrity": _sri(),
                },
                "node_modules/devtool": {
                    "version": "2.0.0",
                    "resolved": _url("devtool", "2.0.0"),
                    "integrity": _sri(),
                    "dev": True,
                },
                "node_modules/opt": {
                    "version": "3.0.0",
                    "resolved": _url("opt", "3.0.0"),
                    "integrity": _sri(),
                    "optional": True,
                },
            },
        }
    ).encode()


def _synthesize_manifest(root_node: dict) -> dict:
    """Mirror the assembler script's manifest synthesis in pure Python."""
    pkg = {
        "name": root_node.get("name") or "npm-assembler",
        "version": root_node.get("version") or "0.0.0",
        "private": True,
    }
    for key in MANIFEST_DEPENDENCY_KEYS:
        value = root_node.get(key)
        if value is not None and isinstance(value, dict):
            pkg[key] = value
    return pkg


class TestManifestDependencyPolicy(unittest.TestCase):
    def test_manifest_keys_exclude_peer_dependencies(self):
        self.assertEqual(
            MANIFEST_DEPENDENCY_KEYS,
            ("dependencies", "devDependencies", "optionalDependencies"),
        )
        self.assertNotIn("peerDependencies", MANIFEST_DEPENDENCY_KEYS)

    def test_script_never_copies_peer_dependencies(self):
        script = assembler_script_bytes().decode("utf-8")
        # The synthesized-manifest copy list matches the policy constant and
        # never mentions peerDependencies at all.
        self.assertIn(
            json.dumps(list(MANIFEST_DEPENDENCY_KEYS), separators=(",", ":")),
            script,
        )
        self.assertNotIn("peerDependencies", script)

    def test_root_peer_is_excluded_from_validated_closure(self):
        raw = _fixture_with_root_peers()
        validated = preflight(
            raw,
            roots=(
                RootSpec("a", "1.0.0"),
                RootSpec("devtool", "2.0.0"),
                RootSpec("opt", "3.0.0"),
            ),
            platform=_PLATFORM,
            node_version=_NODE,
            npm_version=_NPM,
        )
        names = {p.name for p in validated.packages}
        self.assertIn("a", names)
        self.assertIn("devtool", names)
        self.assertIn("opt", names)
        self.assertNotIn("peer-only", names)

    def test_synthesized_manifest_cannot_install_or_require_unvalidated_peer(self):
        raw = _fixture_with_root_peers()
        root_node = json.loads(raw.decode())["packages"][""]
        manifest = _synthesize_manifest(root_node)
        # The three installed-edge maps are copied verbatim...
        self.assertEqual(manifest["dependencies"], {"a": "1.0.0"})
        self.assertEqual(manifest["devDependencies"], {"devtool": "2.0.0"})
        self.assertEqual(manifest["optionalDependencies"], {"opt": "3.0.0"})
        # ...and the unvalidated peer is never declared, so npm cannot
        # install or require it.
        self.assertNotIn("peerDependencies", manifest)
        self.assertNotIn("peer-only", json.dumps(manifest))


if __name__ == "__main__":
    unittest.main()
