"""Consolidated live-inventory contract lane.

This module is the single place for explicit checks against the
repository's real ``docker-constructor.toml``.  It asserts only structural
and cross-field invariants — successful loading, selected-artifact /
platform agreement, Pi source contract, effective-state / environment
propagation, and declared-owner visual headers.  It never pins dependency
versions or optional-extension membership, so a routine bump needs no
change here.

It also proves that the same contract accepts test-owned *valid* variants
(a bumped reviewed artifact version and a removed optional extension) and
rejects deliberately inconsistent declarations.
"""
from __future__ import annotations

import tempfile
import tomllib
import unittest
from pathlib import Path

from docker.versioning.build_materialization import select_build_artifacts
from docker.versioning.effective import apply_overrides, resolve_build_projection
from docker.versioning.errors import InventoryError
from docker.versioning.integrity import is_valid_integrity
from docker.versioning.inventory import load_inventory, validate_inventory
from docker.versioning.rendering import render_build_environment

from tests.inventory_fixtures import (
    remove_extension,
    rewrite_artifact_integrities,
    stable_inventory_text,
    with_dotted_extension,
    with_empty_pi_extensions,
)
from tests.live_owner_audit import (
    independent_update_owners,
    owner_header_violations,
    production_update_owners,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_INVENTORY = REPO_ROOT / "docker-constructor.toml"

_PI_PACKAGE = "@earendil-works/pi-coding-agent"
_PI_REPOSITORY = "earendil-works/pi"
_PI_TAG_PREFIX = "v"
_PLATFORM = "linux-amd64"


def _load_text(text: str):
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "docker-constructor.toml"
        path.write_text(text)
        return load_inventory(path)


def _declared_platform_artifacts(inventory) -> dict[str, tuple[str, str]]:
    """Return ``name -> (url, sha256)`` for the selected platform."""
    stages = inventory.stages
    return {
        "rustup": (
            stages.toolchain.rust.rustup[_PLATFORM].url,
            stages.toolchain.rust.rustup[_PLATFORM].sha256,
        ),
        "uv": (
            stages.toolchain.uv.artifacts[_PLATFORM].url,
            stages.toolchain.uv.artifacts[_PLATFORM].sha256,
        ),
        "rtk": (
            stages.rtk_prebuilt.rtk.artifacts[_PLATFORM].url,
            stages.rtk_prebuilt.rtk.artifacts[_PLATFORM].sha256,
        ),
        "fd": (
            stages.fd_prebuilt.fd.artifacts[_PLATFORM].url,
            stages.fd_prebuilt.fd.artifacts[_PLATFORM].sha256,
        ),
    }


class _LiveContractMixin:
    """Reusable structural / cross-field contract assertions.

    Methods are named ``assert_*`` so they are not collected as tests when
    mixed into a ``unittest.TestCase``.
    """

    def assert_load_contract(self, inventory) -> None:
        # An inventory may legitimately declare zero optional extensions;
        # per-extension assertions naturally run only for declared ones.
        for name, extension in inventory.runtime_pi_extensions.items():
            with self.subTest(extension=name):
                self.assertEqual(extension.source.type, "npm")
                self.assertEqual(extension.update.provider, "npm")
                self.assertIn(extension.version, extension.artifacts)
                artifact = extension.artifacts[extension.version]
                self.assertTrue(artifact.url)
                self.assertTrue(
                    is_valid_integrity(artifact.integrity),
                    f"unexpected integrity format: {artifact.integrity!r}",
                )
                self.assertIsNotNone(extension.validation)
                self.assertTrue(extension.validation.metadata_file)
                if extension.override is not None:
                    from docker.versioning.constraints import parse_numeric_version
                    self.assertTrue(
                        extension.override.constraint.matches(
                            parse_numeric_version(extension.version)
                        )
                    )

    def assert_artifact_platform_agreement(self, inventory) -> None:
        projection = resolve_build_projection(inventory.build, {}, platform=_PLATFORM)
        selected = select_build_artifacts(projection)
        declared = _declared_platform_artifacts(inventory)
        self.assertEqual(
            {artifact.name for artifact in selected}, set(declared),
        )
        for artifact in selected:
            with self.subTest(artifact=artifact.name):
                url, sha256 = declared[artifact.name]
                self.assertEqual(artifact.url, url)
                self.assertEqual(artifact.identity.hex_digest(), sha256)

    def assert_pi_source_contract(self, inventory) -> None:
        pi = inventory.build.stages.pi_tools.pi
        self.assertEqual(pi.source.type, "pi-release")
        self.assertEqual(pi.source.package, _PI_PACKAGE)
        self.assertEqual(pi.source.release_repository, _PI_REPOSITORY)
        self.assertEqual(pi.source.release_tag_prefix, _PI_TAG_PREFIX)
        self.assertTrue(pi.version)
        projection = resolve_build_projection(inventory.build, {}, platform=_PLATFORM)
        self.assertEqual(projection.pi_release.package, _PI_PACKAGE)
        self.assertEqual(projection.pi_release.release_repository, _PI_REPOSITORY)
        self.assertEqual(projection.pi_release.release_tag_prefix, _PI_TAG_PREFIX)

    def assert_effective_environment_propagation(self, inventory) -> None:
        effective = apply_overrides(inventory, {})
        env = render_build_environment(effective)

        # Declared tool versions propagate to the build environment.
        self.assertEqual(
            env["PYTHON_VERSION"], inventory.stages.toolchain.python.version,
        )
        self.assertEqual(
            env["UV_VERSION"], inventory.stages.toolchain.uv.version,
        )
        # Node base image is derived from declared source metadata.
        node = inventory.stages.base.node
        image = env["NODE_BASE_IMAGE"]
        self.assertIn(node.source.registry.rstrip("/"), image)
        self.assertIn(node.source.repository, image)
        self.assertIn(node.tag, image)
        self.assertIn(node.digest, image)
        # Every declared optional extension propagates, whatever its name.
        for name, extension in inventory.runtime_pi_extensions.items():
            key = name.upper().replace("-", "_") + "_VERSION"
            self.assertEqual(env[key], extension.version)

    def assert_owner_membership(self, document_text: str, *, omit_paths=()) -> set[str]:
        """Assert production owner membership equals the independent set.

        *omit_paths* simulates a production enumeration dropping an owner
        so tests can prove the independent enumeration still catches it.
        """
        raw = tomllib.loads(document_text)
        inventory = validate_inventory(raw)
        independent = set(independent_update_owners(raw))
        production = set(
            production_update_owners(inventory, omit_paths=omit_paths)
        )
        self.assertEqual(independent, production)
        return independent

    def assert_owner_headers(self, document_text: str) -> None:
        independent = self.assert_owner_membership(document_text)
        self.assertTrue(independent)
        self.assertEqual(
            [], owner_header_violations(document_text, independent),
        )


class TestLiveInventoryContract(_LiveContractMixin, unittest.TestCase):
    """Structural contract against the repository's reviewed inventory."""

    def test_successful_load(self):
        inventory = load_inventory(REAL_INVENTORY)
        self.assert_load_contract(inventory)

    def test_selected_artifacts_agree_with_declared_platform(self):
        inventory = load_inventory(REAL_INVENTORY)
        self.assert_artifact_platform_agreement(inventory)

    def test_pi_source_contract(self):
        inventory = load_inventory(REAL_INVENTORY)
        self.assert_pi_source_contract(inventory)

    def test_effective_environment_propagation(self):
        inventory = load_inventory(REAL_INVENTORY)
        self.assert_effective_environment_propagation(inventory)

    def test_declared_owner_visual_headers(self):
        inventory = load_inventory(REAL_INVENTORY)
        # Independent parsed view for the header check.
        raw = tomllib.loads(REAL_INVENTORY.read_text())
        validate_inventory(raw)
        self.assert_owner_headers(REAL_INVENTORY.read_text())

    def test_owner_membership_omission_is_detected(self):
        with self.assertRaises(AssertionError):
            self.assert_owner_membership(
                REAL_INVENTORY.read_text(),
                omit_paths=("build.stages.toolchain.ty",),
            )


class TestValidVariants(_LiveContractMixin, unittest.TestCase):
    """The contract accepts test-owned valid variants."""

    def test_bumped_reviewed_version_variant_passes(self):
        # pi-read already declares a reviewed 0.3.0 artifact, so bumping the
        # selected version needs no new table.
        bumped = stable_inventory_text().replace(
            '[runtime.pi-extensions.pi-read]\nversion = "0.2.1"',
            '[runtime.pi-extensions.pi-read]\nversion = "0.3.0"',
            1,
        )
        self.assertNotEqual(bumped, stable_inventory_text())
        inventory = _load_text(bumped)
        self.assertEqual(inventory.runtime_pi_extensions["pi-read"].version, "0.3.0")
        self.assert_load_contract(inventory)
        self.assert_artifact_platform_agreement(inventory)
        self.assert_effective_environment_propagation(inventory)

    def test_removed_optional_extension_variant_passes(self):
        stripped = remove_extension(stable_inventory_text(), "pi-read")
        inventory = _load_text(stripped)
        self.assertEqual(
            sorted(inventory.runtime_pi_extensions), ["pi-proxy", "pi-usage"],
        )
        self.assert_load_contract(inventory)
        self.assert_effective_environment_propagation(inventory)

    def test_empty_optional_extensions_variant_passes(self):
        empty = with_empty_pi_extensions(stable_inventory_text())
        inventory = _load_text(empty)
        self.assertEqual(dict(inventory.runtime_pi_extensions), {})
        self.assert_load_contract(inventory)
        self.assert_effective_environment_propagation(inventory)
        # An empty mapping must not invent extension environment entries.
        env = render_build_environment(apply_overrides(inventory, {}))
        for key in ("PI_READ_VERSION", "PI_USAGE_VERSION", "PI_PROXY_VERSION"):
            self.assertNotIn(key, env)

    def test_changed_node_metadata_variant_passes(self):
        changed = (
            stable_inventory_text()
            .replace('tag = "24.18.0-trixie-slim"', 'tag = "25.1.0-trixie-slim"', 1)
            .replace('node_version = "24.18.0"', 'node_version = "25.1.0"', 1)
            .replace('npm_version = "11.16.0"', 'npm_version = "12.0.0"', 1)
            .replace(
                "sha256:ae91dcc111a68c9d2d81ff2a17bda61be126426176fde6fe7d08ab13b7f50573",
                "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
                1,
            )
        )
        inventory = _load_text(changed)
        self.assertEqual(
            inventory.build.stages.base.node.node_version, "25.1.0",
        )
        self.assert_effective_environment_propagation(inventory)

    def test_sri_algorithm_variants_pass(self):
        # Production accepts sha256/sha384/sha512 SRI; each valid variant must
        # load and satisfy the full live contract.
        for algorithm in ("sha256", "sha384", "sha512"):
            with self.subTest(algorithm=algorithm):
                variant = rewrite_artifact_integrities(
                    stable_inventory_text(), algorithm=algorithm,
                )
                inventory = _load_text(variant)
                self.assert_load_contract(inventory)

    def test_dotted_extension_variant_passes(self):
        variant = with_dotted_extension(stable_inventory_text())
        inventory = _load_text(variant)
        self.assertIn("foo.bar", inventory.runtime_pi_extensions)
        self.assert_load_contract(inventory)
        self.assert_effective_environment_propagation(inventory)

    def test_valid_variant_owner_headers_pass(self):
        stripped = remove_extension(stable_inventory_text(), "pi-read")
        self.assert_owner_headers(stripped)

    def test_empty_extension_variant_owner_headers_pass(self):
        empty = with_empty_pi_extensions(stable_inventory_text())
        self.assert_owner_headers(empty)

    def test_dotted_extension_owner_headers_pass(self):
        # The quoted key ``runtime.pi-extensions."foo.bar"`` must match the
        # owner and carry its ``# --- pi-extensions.foo.bar ---`` header.
        variant = with_dotted_extension(stable_inventory_text())
        self.assert_owner_headers(variant)

    def test_dotted_extension_missing_header_is_detected(self):
        variant = with_dotted_extension(stable_inventory_text())
        without_header = variant.replace(
            "# --- pi-extensions.foo.bar ---\n", "", 1,
        )
        self.assertNotEqual(without_header, variant)
        with self.assertRaises(AssertionError):
            self.assert_owner_headers(without_header)

    def test_dotted_extension_misplaced_header_is_detected(self):
        variant = with_dotted_extension(stable_inventory_text())
        misplaced = variant.replace(
            '# --- pi-extensions.foo.bar ---\n'
            '[runtime.pi-extensions."foo.bar"]',
            '[runtime.pi-extensions."foo.bar"]\n'
            '# --- pi-extensions.foo.bar ---',
            1,
        )
        self.assertNotEqual(misplaced, variant)
        with self.assertRaises(AssertionError):
            self.assert_owner_headers(misplaced)


class TestInconsistentVariantsRejected(_LiveContractMixin, unittest.TestCase):
    """Deliberately inconsistent declarations fail loudly."""

    def test_bumped_version_without_artifact_is_rejected(self):
        inconsistent = stable_inventory_text().replace(
            '[runtime.pi-extensions.pi-read]\nversion = "0.2.1"',
            '[runtime.pi-extensions.pi-read]\nversion = "0.9.9"',
            1,
        )
        with self.assertRaises(InventoryError) as ctx:
            _load_text(inconsistent)
        self.assertIn("pi-read", str(ctx.exception))

    def test_mismatched_artifact_url_is_rejected(self):
        inconsistent = stable_inventory_text().replace(
            "https://github.com/astral-sh/uv/releases/download/0.12.5/"
            "uv-x86_64-unknown-linux-gnu.tar.gz",
            "https://evil.invalid/uv-x86_64-unknown-linux-gnu.tar.gz",
            1,
        )
        with self.assertRaises(InventoryError) as ctx:
            _load_text(inconsistent)
        self.assertIn("uv", str(ctx.exception))

    def test_plain_npm_pi_source_is_rejected(self):
        inconsistent = stable_inventory_text().replace(
            'type = "pi-release"',
            'type = "npm"',
            1,
        )
        with self.assertRaises(InventoryError) as ctx:
            _load_text(inconsistent)
        self.assertIn("pi-tools.pi.source.type", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
