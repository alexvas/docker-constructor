"""Phase 6 task 6.1 — reviewed Node/npm versions and Pi release contract.

Inventory tests requiring exact ``[build.stages.base.node].node_version`` and
``.npm_version`` alongside the reviewed image tag/digest, plus the Pi npm
package ``@earendil-works/pi-coding-agent`` with ``release_repository =
"earendil-works/pi"`` and ``release_tag_prefix = "v"``.  Closed-schema rejection
of missing, malformed, or unknown metadata, and tool versions never inferred
from the image tag.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR.parent))
sys.path.insert(0, str(_THIS_DIR))

from docker.versioning.inventory import load_inventory
from docker.versioning.effective import resolve_build_projection
from docker.versioning.errors import InventoryError
from docker.npm_environment import SMOKE_NODE_VERSION, SMOKE_NPM_VERSION
from versioning.support.inventory_builder import minimal_toml, write_toml

_REPO = _THIS_DIR.parent
_REVIEWED_NODE_VERSION = "24.18.0"
_REVIEWED_NPM_VERSION = "11.16.0"
_REVIEWED_DIGEST = "sha256:ae91dcc111a68c9d2d81ff2a17bda61be126426176fde6fe7d08ab13b7f50573"


class TestReviewedNodeVersions(unittest.TestCase):
    def test_real_inventory_records_reviewed_node_and_npm_versions(self):
        inv = load_inventory(_REPO / "docker-constructor.toml")
        node = inv.build.stages.base.node
        self.assertEqual(node.node_version, _REVIEWED_NODE_VERSION)
        self.assertEqual(node.npm_version, _REVIEWED_NPM_VERSION)
        self.assertEqual(node.digest, _REVIEWED_DIGEST)
        # The reviewed tool versions are the ones the standalone assembler
        # asserts against the digest-pinned image — never derived from the tag.
        self.assertEqual(node.node_version, SMOKE_NODE_VERSION)
        self.assertEqual(node.npm_version, SMOKE_NPM_VERSION)

    def test_reviewed_digest_matches_pinned_image(self):
        inv = load_inventory(_REPO / "docker-constructor.toml")
        self.assertEqual(inv.build.stages.base.node.digest, _REVIEWED_DIGEST)

    def test_node_version_never_inferred_from_tag(self):
        # A floating tag cannot change the reviewed tool versions.
        content = minimal_toml(**{
            "build.stages.base.node.source": (
                'type = "docker-registry"\n'
                'registry = "docker.io"\n'
                'repository = "library/node"\n'
            ),
        })
        content = content.replace('tag = "24-trixie-slim"', 'tag = "24-trixie-slim"')
        path = write_toml(content)
        inv = load_inventory(path)
        self.assertEqual(inv.build.stages.base.node.node_version, "24.18.0")
        self.assertEqual(inv.build.stages.base.node.npm_version, "11.16.0")


class TestReviewedNodeSchemaRejection(unittest.TestCase):
    def test_missing_node_version_rejected(self):
        content = minimal_toml(**{"base.node.node_version": ""})
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(write_toml(content))
        self.assertIn("node_version", str(ctx.exception))

    def test_missing_npm_version_rejected(self):
        content = minimal_toml(**{"base.node.npm_version": ""})
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(write_toml(content))
        self.assertIn("npm_version", str(ctx.exception))

    def test_malformed_node_version_rejected(self):
        content = minimal_toml(**{"base.node.node_version": 'node_version = "24"'})
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(write_toml(content))
        self.assertIn("node_version", str(ctx.exception))

    def test_unknown_node_metadata_rejected(self):
        content = minimal_toml(**{
            "base.node.node_version": 'node_version = "24.18.0"\nnode_flavor = "slim"',
        })
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(write_toml(content))
        self.assertIn("node_flavor", str(ctx.exception))


class TestReviewedPiReleaseContract(unittest.TestCase):
    def test_real_inventory_records_pi_release_contract(self):
        inv = load_inventory(_REPO / "docker-constructor.toml")
        pi = inv.build.stages.pi_tools.pi
        self.assertEqual(pi.source.type, "pi-release")
        self.assertEqual(pi.source.package, "@earendil-works/pi-coding-agent")
        self.assertEqual(pi.source.release_repository, "earendil-works/pi")
        self.assertEqual(pi.source.release_tag_prefix, "v")
        self.assertEqual(pi.version, "0.85.1")

    def test_effective_projection_carries_pi_release(self):
        inv = load_inventory(_REPO / "docker-constructor.toml")
        projection = resolve_build_projection(inv.build, {}, platform="linux-amd64")
        self.assertEqual(projection.pi_release.package, "@earendil-works/pi-coding-agent")
        self.assertEqual(projection.pi_release.release_repository, "earendil-works/pi")
        self.assertEqual(projection.pi_release.release_tag_prefix, "v")

    def test_missing_release_repository_rejected(self):
        content = minimal_toml(**{
            "build.stages.pi-tools.pi.source": (
                'type = "pi-release"\n'
                'package = "@earendil-works/pi-coding-agent"\n'
                'release_tag_prefix = "v"\n'
            ),
        })
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(write_toml(content))
        self.assertIn("release_repository", str(ctx.exception))

    def test_missing_release_tag_prefix_rejected(self):
        content = minimal_toml(**{
            "build.stages.pi-tools.pi.source": (
                'type = "pi-release"\n'
                'package = "@earendil-works/pi-coding-agent"\n'
                'release_repository = "earendil-works/pi"\n'
            ),
        })
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(write_toml(content))
        self.assertIn("release_tag_prefix", str(ctx.exception))

    def test_plain_npm_source_type_rejected_for_pi(self):
        content = minimal_toml(**{
            "build.stages.pi-tools.pi.source": (
                'type = "npm"\n'
                'package = "@earendil-works/pi-coding-agent"\n'
                'release_repository = "earendil-works/pi"\n'
                'release_tag_prefix = "v"\n'
            ),
        })
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(write_toml(content))
        self.assertEqual("build.stages.pi-tools.pi.source.type", ctx.exception.field)

    def test_unknown_pi_source_metadata_rejected(self):
        content = minimal_toml(**{
            "build.stages.pi-tools.pi.source": (
                'type = "pi-release"\n'
                'package = "@earendil-works/pi-coding-agent"\n'
                'release_repository = "earendil-works/pi"\n'
                'release_tag_prefix = "v"\n'
                'binary_archive = "pi-linux-x64.tar.gz"\n'
            ),
        })
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(write_toml(content))
        self.assertIn("binary_archive", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
