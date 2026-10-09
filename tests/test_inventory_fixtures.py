"""Self-tests for the stable test-owned inventory fixtures.

These tests pin the contract of ``tests.inventory_fixtures`` itself:
positive fixtures load through the real production loader without the
repository configuration, independent copies cannot contaminate one
another, locally derived SRI matches the synthetic artifact bytes, and
named mutations fail on the intended field rather than an unrelated one.
"""
from __future__ import annotations

import base64
import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from docker.versioning.inventory import load_inventory

from tests.inventory_fixtures import (
    STABLE_INVENTORY_PATH,
    artifact_bytes,
    artifact_integrity,
    remove_extension,
    rewrite_artifact_integrities,
    stable_inventory_text,
    with_empty_pi_extensions,
    write_stable_inventory,
    write_stable_project,
)

_PI_READ_URL = "https://registry.npmjs.org/@arcanemachine/pi-read/-/pi-read-0.2.1.tgz"


class TestStableFixtureBaseline(unittest.TestCase):
    def test_committed_fixture_exists(self):
        self.assertTrue(STABLE_INVENTORY_PATH.is_file())

    def test_positive_baseline_loads_through_real_loader(self):
        inventory = load_inventory(STABLE_INVENTORY_PATH)
        self.assertEqual(
            sorted(inventory.runtime_pi_extensions),
            ["pi-proxy", "pi-read", "pi-usage"],
        )
        self.assertEqual(inventory.runtime_pi_extensions["pi-read"].version, "0.2.1")
        self.assertEqual(inventory.runtime_pi_extensions["pi-usage"].version, "0.60.7")
        self.assertEqual(inventory.runtime_pi_extensions["pi-proxy"].version, "1.0.0")

    def test_alternate_runtime_artifact_is_declared(self):
        inventory = load_inventory(STABLE_INVENTORY_PATH)
        self.assertEqual(
            sorted(inventory.runtime_pi_extensions["pi-read"].artifacts),
            ["0.2.1", "0.3.0"],
        )

    def test_override_policies_accept_own_and_reject_neighbours(self):
        from docker.versioning.constraints import parse_numeric_version
        inventory = load_inventory(STABLE_INVENTORY_PATH)
        for name, accepted, rejected in (
            ("pi-read", "0.2.1", "0.1.9"),
            ("pi-usage", "0.60.7", "0.51.9"),
            ("pi-proxy", "1.0.0", "0.9.9"),
        ):
            with self.subTest(name=name):
                policy = inventory.runtime_pi_extensions[name].override.constraint
                self.assertTrue(policy.matches(parse_numeric_version(accepted)))
                self.assertFalse(policy.matches(parse_numeric_version(rejected)))

    def test_loads_without_repository_configuration(self):
        # Change into an unrelated directory: the fixture must not depend
        # on the repository's local companion configuration.
        cwd = os.getcwd()
        try:
            with tempfile.TemporaryDirectory() as td:
                os.chdir(td)
                inventory = load_inventory(STABLE_INVENTORY_PATH)
                self.assertIn("pi-proxy", inventory.runtime_pi_extensions)
        finally:
            os.chdir(cwd)


class TestFreshDocumentsAreIndependent(unittest.TestCase):
    def test_two_stable_projects_cannot_contaminate_one_another(self):
        with tempfile.TemporaryDirectory() as td:
            first_dir = Path(td) / "first"
            second_dir = Path(td) / "second"
            first = write_stable_project(first_dir)
            second = write_stable_project(second_dir)

            self.assertNotEqual(first, second)

            # Mutating the first copy must not affect the second.
            original_second = second.read_text()
            first.write_text(first.read_text().replace("0.2.1", "9.9.9"))
            self.assertEqual(original_second, second.read_text())

            inventory = load_inventory(second)
            self.assertEqual(inventory.runtime_pi_extensions["pi-read"].version, "0.2.1")

    def test_write_stable_inventory_writes_fresh_file(self):
        with tempfile.TemporaryDirectory() as td:
            inventory_path = write_stable_inventory(td)
            self.assertEqual(inventory_path.read_text(), stable_inventory_text())


class TestNamedMutations(unittest.TestCase):
    def test_remove_optional_extension_only_removes_that_extension(self):
        stripped = remove_extension(stable_inventory_text(), "pi-read")
        self.assertNotIn("[runtime.pi-extensions.pi-read]", stripped)
        self.assertIn("[runtime.pi-extensions.pi-usage]", stripped)
        self.assertIn("[runtime.pi-extensions.pi-proxy]", stripped)
        with tempfile.TemporaryDirectory() as td:
            path = write_stable_inventory(td)
            path.write_text(stripped)
            inventory = load_inventory(path)
            self.assertEqual(sorted(inventory.runtime_pi_extensions), ["pi-proxy", "pi-usage"])

    def test_remove_unknown_extension_raises(self):
        with self.assertRaises(KeyError):
            remove_extension(stable_inventory_text(), "not-an-extension")

    def test_empty_optional_extensions_variant_loads(self):
        empty = with_empty_pi_extensions(stable_inventory_text())
        self.assertIn("[runtime.pi-extensions]", empty)
        with tempfile.TemporaryDirectory() as td:
            path = write_stable_inventory(td)
            path.write_text(empty)
            inventory = load_inventory(path)
        self.assertEqual(dict(inventory.runtime_pi_extensions), {})

    def test_invalid_field_mutation_rejects_intended_field(self):
        # Introduce exactly one defect: a non-numeric override constraint.
        mutated = stable_inventory_text().replace(
            'constraint = ">=0.2.0"\nscheme = "numeric"',
            'constraint = ">=0.2.0"\nscheme = "not-a-scheme"',
            1,
        )
        with tempfile.TemporaryDirectory() as td:
            path = write_stable_inventory(td)
            path.write_text(mutated)
            from docker.versioning.errors import InventoryError
            with self.assertRaises(InventoryError) as ctx:
                load_inventory(path)
        self.assertIn("scheme", str(ctx.exception).lower())

    def test_removed_mandatory_artifact_rejects_intended_field(self):
        # Remove the pi-proxy artifact table: the intended field is the
        # missing artifact section, not an unrelated missing top-level key.
        stripped = stable_inventory_text().replace(
            '[runtime.pi-extensions.pi-proxy.artifacts."1.0.0"]\n'
            'url = "https://registry.npmjs.org/pi-proxy/-/pi-proxy-1.0.0.tgz"\n'
            'integrity = "sha512-UHr/AQV2S0rISwRsD5jmKAo9ZQlZxU9Csh72sGYYDhbkSo44P+XzfRG96OuYYy2G3Iis0a75w3Cp9KYtjpbZxw=="\n',
            "",
        )
        self.assertNotEqual(stripped, stable_inventory_text())
        with tempfile.TemporaryDirectory() as td:
            path = write_stable_inventory(td)
            path.write_text(stripped)
            from docker.versioning.errors import InventoryError
            with self.assertRaises(InventoryError) as ctx:
                load_inventory(path)
        self.assertIn("pi-proxy.artifacts", str(ctx.exception).lower())


class TestLocalIntegrityDerivation(unittest.TestCase):
    def test_artifact_bytes_are_deterministic(self):
        self.assertEqual(artifact_bytes("u"), artifact_bytes("u"))
        self.assertNotEqual(artifact_bytes("u"), artifact_bytes("v"))

    def test_artifact_integrity_matches_local_bytes(self):
        integrity = artifact_integrity("https://example.invalid/a.tgz")
        algorithm, _, encoded = integrity.partition("-")
        self.assertEqual(algorithm, "sha512")
        expected = base64.b64encode(
            hashlib.sha512(artifact_bytes("https://example.invalid/a.tgz")).digest()
        ).decode("ascii")
        self.assertEqual(encoded, expected)

    def test_rewrite_handles_both_key_orderings(self):
        text = (
            '[a]\nurl = "https://example.invalid/a.tgz"\nintegrity = "sha512-OLD"\n'
            '\n[b]\nintegrity = "sha512-OLD"\nurl = "https://example.invalid/b.tgz"\n'
        )
        rewritten = rewrite_artifact_integrities(text)
        self.assertIn(
            f'url = "https://example.invalid/a.tgz"\nintegrity = "{artifact_integrity("https://example.invalid/a.tgz")}"',
            rewritten,
        )
        self.assertIn(
            f'url = "https://example.invalid/b.tgz"\nintegrity = "{artifact_integrity("https://example.invalid/b.tgz")}"',
            rewritten,
        )
        self.assertNotIn("sha512-OLD", rewritten)

    def test_rewrite_honours_shared_identity_override(self):
        text = (
            '[a]\nurl = "https://example.invalid/a.tgz"\nintegrity = "sha512-OLD"\n'
            '\n[b]\nurl = "https://example.invalid/b.tgz"\nintegrity = "sha512-OLD"\n'
        )
        shared = artifact_integrity("https://example.invalid/a.tgz")
        rewritten = rewrite_artifact_integrities(
            text, overrides={"https://example.invalid/b.tgz": shared},
        )
        self.assertEqual(rewritten.count(shared), 2)


if __name__ == "__main__":
    unittest.main()
