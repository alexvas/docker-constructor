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
import tempfile
import unittest
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR.parent))
sys.path.insert(0, str(_THIS_DIR))

from docker.versioning.inventory import load_inventory
from docker.versioning.effective import (
    apply_overrides,
    resolve_build_projection,
)
from docker.versioning.errors import InventoryError
from docker.versioning.rendering import render_build_environment
from docker.npm_environment import SMOKE_NODE_VERSION, SMOKE_NPM_VERSION
from tests.inventory_fixtures import (
    stable_inventory_path,
    stable_inventory_text,
)
from versioning.support.inventory_builder import minimal_toml, write_toml

_REPO = _THIS_DIR.parent
_FIXTURE_PI_VERSION = "0.85.1"
_FIXTURE_NODE_VERSION = "24.18.0"
_FIXTURE_NPM_VERSION = "11.16.0"
_FIXTURE_DIGEST = "sha256:ae91dcc111a68c9d2d81ff2a17bda61be126426176fde6fe7d08ab13b7f50573"


def _load_text(text: str):
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "docker-constructor.toml"
        path.write_text(text)
        return load_inventory(path)


def _changed_node_metadata() -> str:
    return (
        stable_inventory_text()
        .replace('tag = "24.18.0-trixie-slim"', 'tag = "25.1.0-trixie-slim"', 1)
        .replace('node_version = "24.18.0"', 'node_version = "25.1.0"', 1)
        .replace('npm_version = "11.16.0"', 'npm_version = "12.0.0"', 1)
        .replace(
            _FIXTURE_DIGEST,
            "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            1,
        )
    )


class TestStableNodeVersions(unittest.TestCase):
    """Behavioural lane: exact reviewed Node/npm metadata on the fixture.

    Pins the reviewed tool versions and image digest, so a routine bump of
    the repository inventory does not change these expectations.
    """

    def test_stable_inventory_records_reviewed_node_and_npm_versions(self):
        inv = load_inventory(stable_inventory_path())
        node = inv.build.stages.base.node
        self.assertEqual(node.node_version, _FIXTURE_NODE_VERSION)
        self.assertEqual(node.npm_version, _FIXTURE_NPM_VERSION)
        self.assertEqual(node.digest, _FIXTURE_DIGEST)

    def test_stable_fixture_digest_matches_pinned_image(self):
        inv = load_inventory(stable_inventory_path())
        self.assertEqual(inv.build.stages.base.node.digest, _FIXTURE_DIGEST)

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


class TestLiveNodeMetadataContract(unittest.TestCase):
    """Live lane: declared Node metadata loads and propagates.

    Expectations are derived from the declared inputs, never from fixed
    release literals, so a reviewed Node bump needs no change here.
    """

    def test_repository_node_metadata_loads(self):
        node = load_inventory(_REPO / "docker-constructor.toml").build.stages.base.node
        self.assertTrue(node.node_version)
        self.assertTrue(node.npm_version)
        self.assertTrue(node.tag)
        self.assertTrue(node.digest)

    def test_declared_node_metadata_propagates_to_environment(self):
        inv = load_inventory(_REPO / "docker-constructor.toml")
        node = inv.build.stages.base.node
        env = render_build_environment(apply_overrides(inv, {}))
        image = env["NODE_BASE_IMAGE"]
        self.assertIn(node.source.registry.rstrip("/"), image)
        self.assertIn(node.source.repository, image)
        self.assertIn(node.tag, image)
        self.assertIn(node.digest, image)


class TestSmokeToolConsistency(unittest.TestCase):
    """Standalone smoke runner and stable fixture agree on tool versions.

    ``docker.npm_environment.smoke`` accepts a caller-provided image and
    asserts its own reviewed ``SMOKE_NODE_VERSION`` / ``SMOKE_NPM_VERSION``
    against that image; the constants are not policy for the repository
    inventory.  This check compares them to the test-owned stable fixture,
    never the live inventory, so a reviewed Node bump does not break it.
    """

    def test_stable_fixture_matches_smoke_tool_expectations(self):
        node = load_inventory(stable_inventory_path()).build.stages.base.node
        self.assertEqual(node.node_version, SMOKE_NODE_VERSION)
        self.assertEqual(node.npm_version, SMOKE_NPM_VERSION)


class TestNodeMetadataVariant(unittest.TestCase):
    """A changed Node metadata variant is accepted by structural checks."""

    def test_changed_node_metadata_variant_loads_and_propagates(self):
        changed = _changed_node_metadata()
        self.assertNotEqual(changed, stable_inventory_text())
        inv = _load_text(changed)
        node = inv.build.stages.base.node
        self.assertEqual(node.node_version, "25.1.0")
        self.assertEqual(node.npm_version, "12.0.0")
        env = render_build_environment(apply_overrides(inv, {}))
        self.assertIn(node.tag, env["NODE_BASE_IMAGE"])
        self.assertIn(node.digest, env["NODE_BASE_IMAGE"])


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


class TestStablePiReleaseContract(unittest.TestCase):
    """Behavioural lane: exact Pi release contract on the stable fixture.

    Pins the reviewed release metadata and version, so a routine
    dependency bump of the repository inventory does not change it.
    """

    def test_stable_inventory_records_pi_release_contract(self):
        inv = load_inventory(stable_inventory_path())
        pi = inv.build.stages.pi_tools.pi
        self.assertEqual(pi.source.type, "pi-release")
        self.assertEqual(pi.source.package, "@earendil-works/pi-coding-agent")
        self.assertEqual(pi.source.release_repository, "earendil-works/pi")
        self.assertEqual(pi.source.release_tag_prefix, "v")
        self.assertEqual(pi.version, _FIXTURE_PI_VERSION)

    def test_stable_fixture_effective_projection_carries_pi_release(self):
        inv = load_inventory(stable_inventory_path())
        projection = resolve_build_projection(inv.build, {}, platform="linux-amd64")
        self.assertEqual(projection.pi_release.package, "@earendil-works/pi-coding-agent")
        self.assertEqual(projection.pi_release.release_repository, "earendil-works/pi")
        self.assertEqual(projection.pi_release.release_tag_prefix, "v")


class TestReviewedPiReleaseContract(unittest.TestCase):
    """Live-contract lane: repository Pi source contract smoke check.

    The current reviewed version is deliberately not pinned; only the
    structural Pi source contract and its propagation are asserted.
    """

    def test_real_inventory_records_pi_release_source_contract(self):
        inv = load_inventory(_REPO / "docker-constructor.toml")
        pi = inv.build.stages.pi_tools.pi
        self.assertEqual(pi.source.type, "pi-release")
        self.assertEqual(pi.source.package, "@earendil-works/pi-coding-agent")
        self.assertEqual(pi.source.release_repository, "earendil-works/pi")
        self.assertEqual(pi.source.release_tag_prefix, "v")
        self.assertTrue(pi.version)

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
