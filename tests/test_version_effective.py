"""Domain-level tests for effective configuration.

These tests import the effective module directly, no subprocess.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import MappingProxyType

from docker.versioning.effective import (
    EffectiveConfiguration,

    apply_overrides,
    get_path,
    serialize_effective_inventory,
    to_plain_data,
)
from docker.versioning.rendering import render_build_environment
from docker.versioning.errors import (
    OverrideValidationError,
    UnknownPathError,
    UnsupportedOverrideError,
)
from docker.versioning.inventory import load_inventory

_REPO_ROOT = Path(__file__).resolve().parent.parent
_INVENTORY_TOML = _REPO_ROOT / "docker-constructor.toml"


def _default_inventory():
    return load_inventory(_INVENTORY_TOML)


# ---------------------------------------------------------------------------
# Default selection
# ---------------------------------------------------------------------------

class TestDefaultSelection(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inventory = _default_inventory()
        cls.effective = apply_overrides(cls.inventory, {})

    def test_exact_default_python(self):
        self.assertEqual(
            self.effective.inventory.stages.toolchain.python.version,
            "3.14.7",
        )

    def test_not_resolved_to_newest(self):
        # Default must be exactly the TOML value, not resolved
        self.assertEqual(
            self.effective.inventory.stages.toolchain.python.version,
            self.inventory.stages.toolchain.python.version,
        )

    def test_all_values_equal_inventory(self):
        # All other values unchanged
        self.assertEqual(
            self.effective.inventory.stages.toolchain.rust.version,
            self.inventory.stages.toolchain.rust.version,
        )
        self.assertEqual(
            self.effective.inventory.stages.toolchain.uv.version,
            self.inventory.stages.toolchain.uv.version,
        )

    def test_source_metadata_preserved(self):
        python = self.effective.inventory.stages.toolchain.python
        self.assertIsNotNone(python.source)
        self.assertEqual(python.source.type, "uv-python")

    def test_update_metadata_preserved(self):
        python = self.effective.inventory.stages.toolchain.python
        self.assertIsNotNone(python.update)
        self.assertEqual(python.update.provider, "uv-python")

    def test_artifacts_preserved(self):
        artifacts = self.effective.inventory.stages.toolchain.uv.artifacts
        self.assertIn("linux-amd64", artifacts)

    def test_pi_extensions_preserved(self):
        extensions = self.effective.inventory.runtime_pi_extensions
        self.assertIn("pi-read", extensions)
        self.assertEqual(extensions["pi-read"].version, "0.2.1")


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------

class TestImmutability(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inventory = _default_inventory()
        cls.effective = apply_overrides(cls.inventory, {})

    def test_cannot_set_version(self):
        inv = self.effective.inventory
        with self.assertRaises((TypeError, AttributeError)):
            inv.stages.toolchain.python.version = "4.0.0"  # type: ignore[misc]

    def test_cannot_set_extension(self):
        inv = self.effective.inventory
        with self.assertRaises((TypeError, AttributeError)):
            inv.runtime_pi_extensions["pi-read"] = None  # type: ignore[index]

    def test_cannot_set_artifact(self):
        inv = self.effective.inventory
        with self.assertRaises((TypeError, AttributeError)):
            inv.stages.toolchain.uv.artifacts["linux-amd64"] = None  # type: ignore[index]


# ---------------------------------------------------------------------------
# No mutation of source inventory
# ---------------------------------------------------------------------------


class TestEffectiveImmutability(unittest.TestCase):
    """EffectiveConfiguration and render_build_environment must be truly immutable."""

    @classmethod
    def setUpClass(cls):
        cls.inventory = _default_inventory()
        cls.effective = apply_overrides(
            cls.inventory, {"build.stages.toolchain.python.version": "3.14.7"}
        )

    def test_overrides_is_mappingproxy(self):
        from types import MappingProxyType
        self.assertIsInstance(self.effective.overrides, MappingProxyType)

    def test_cannot_mutate_overrides(self):
        with self.assertRaises((TypeError, AttributeError)):
            self.effective.overrides["new"] = "value"  # type: ignore[index]

    def test_env_is_mappingproxy(self):
        from types import MappingProxyType
        env = render_build_environment(self.effective)
        self.assertIsInstance(env, MappingProxyType)

    def test_cannot_mutate_env(self):
        env = render_build_environment(self.effective)
        with self.assertRaises((TypeError, AttributeError)):
            env["NEW_VAR"] = "value"  # type: ignore[index]


class TestSourceInventoryImmutability(unittest.TestCase):
    def test_source_inventory_unchanged_after_override(self):
        inv = _default_inventory()
        original_version = inv.stages.toolchain.python.version
        apply_overrides(inv, {"build.stages.toolchain.python.version": "3.14.7"})
        self.assertEqual(inv.stages.toolchain.python.version, original_version)

    def test_effective_differs_from_source(self):
        inv = _default_inventory()
        eff = apply_overrides(inv, {"build.stages.toolchain.python.version": "3.14.6"})
        self.assertNotEqual(
            eff.inventory.stages.toolchain.python.version,
            inv.stages.toolchain.python.version,
        )
        self.assertEqual(eff.inventory.stages.toolchain.python.version, "3.14.6")
        self.assertEqual(inv.stages.toolchain.python.version, "3.14.7")


# ---------------------------------------------------------------------------
# Override policy
# ---------------------------------------------------------------------------

class TestOverridePolicy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inventory = _default_inventory()

    def test_3146_accepted(self):
        eff = apply_overrides(
            self.inventory, {"build.stages.toolchain.python.version": "3.14.6"}
        )
        self.assertEqual(
            eff.inventory.stages.toolchain.python.version, "3.14.6"
        )

    def test_3147_accepted(self):
        eff = apply_overrides(
            self.inventory, {"build.stages.toolchain.python.version": "3.14.7"}
        )
        self.assertEqual(
            eff.inventory.stages.toolchain.python.version, "3.14.7"
        )

    def test_400_accepted_by_constraint(self):
        eff = apply_overrides(
            self.inventory, {"build.stages.toolchain.python.version": "4.0.0"}
        )
        self.assertEqual(
            eff.inventory.stages.toolchain.python.version, "4.0.0"
        )

    def test_3145_rejected(self):
        with self.assertRaises(OverrideValidationError) as ctx:
            apply_overrides(
                self.inventory, {"build.stages.toolchain.python.version": "3.14.5"}
            )
        self.assertIn("does not satisfy", str(ctx.exception))

    def test_314_rejected(self):
        with self.assertRaises(OverrideValidationError) as ctx:
            apply_overrides(
                self.inventory, {"build.stages.toolchain.python.version": "3.14"}
            )
        self.assertIn("not a valid", str(ctx.exception).lower())

    def test_prerelease_rejected(self):
        with self.assertRaises(OverrideValidationError) as ctx:
            apply_overrides(
                self.inventory,
                {"build.stages.toolchain.python.version": "3.14.7-rc.1"},
            )
        self.assertIn("not a valid", str(ctx.exception).lower())

    def test_latest_rejected(self):
        with self.assertRaises(OverrideValidationError) as ctx:
            apply_overrides(
                self.inventory,
                {"build.stages.toolchain.python.version": "latest"},
            )
        self.assertIn("not a valid", str(ctx.exception).lower())


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

class TestSerialization(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inventory = _default_inventory()
        cls.effective = apply_overrides(cls.inventory, {})

    def test_dataclass_to_dict(self):
        data = to_plain_data(self.effective.inventory)
        self.assertIsInstance(data, dict)
        self.assertIn("schema", data)
        self.assertIn("build", data)
        self.assertIn("stages", data["build"])

    def test_mappingproxy_to_dict(self):
        raw = serialize_effective_inventory(self.effective)
        self.assertIsInstance(raw["build"]["stages"]["toolchain"]["uv"]["artifacts"], dict)

    def test_tuple_to_array(self):
        data = serialize_effective_inventory(self.effective)
        components = data["build"]["stages"]["toolchain"]["rust"]["components"]
        self.assertIsInstance(components, list)

    def test_json_serializable(self):
        raw = serialize_effective_inventory(self.effective)
        json.dumps(raw)

    def test_deterministic_key_ordering(self):
        raw1 = serialize_effective_inventory(self.effective)
        raw2 = serialize_effective_inventory(self.effective)
        json1 = json.dumps(raw1, sort_keys=True, separators=(",", ":"))
        json2 = json.dumps(raw2, sort_keys=True, separators=(",", ":"))
        self.assertEqual(json1, json2)

    def test_no_object_repr(self):
        raw = serialize_effective_inventory(self.effective)
        json_str = json.dumps(raw, sort_keys=True)
        self.assertNotIn("__repr__", json_str)
        self.assertNotIn("object at 0x", json_str)

    def test_default_vs_override_differs_only_in_effective(self):
        inv = _default_inventory()
        default_eff = apply_overrides(inv, {})
        override_eff = apply_overrides(
            inv, {"build.stages.toolchain.python.version": "3.14.9"}
        )

        default_raw = serialize_effective_inventory(default_eff)
        override_raw = serialize_effective_inventory(override_eff)

        # Only python version differs
        self.assertNotEqual(
            default_raw["build"]["stages"]["toolchain"]["python"]["version"],
            override_raw["build"]["stages"]["toolchain"]["python"]["version"],
        )
        self.assertEqual(
            default_raw["build"]["stages"]["toolchain"]["python"]["source"],
            override_raw["build"]["stages"]["toolchain"]["python"]["source"],
        )

    def test_toml_style_names(self):
        raw = serialize_effective_inventory(self.effective)
        stages = raw["build"]["stages"]
        # TOML names, not Python attr names
        self.assertIn("rtk-prebuilt", stages)
        self.assertIn("fd-prebuilt", stages)
        self.assertIn("pi-tools", stages)
        self.assertIn("openspec-tools", stages)
        self.assertNotIn("rtk_prebuilt", stages)
        self.assertNotIn("fd_prebuilt", stages)
        self.assertIn("pi-extensions", raw["runtime"])


# ---------------------------------------------------------------------------
# Environment mapping
# ---------------------------------------------------------------------------

class TestEnvironmentMapping(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inventory = _default_inventory()
        cls.effective = apply_overrides(cls.inventory, {})

    def test_default_python_env(self):
        env = render_build_environment(self.effective)
        self.assertEqual(env["PYTHON_VERSION"], "3.14.7")

    def test_overridden_python_env(self):
        eff = apply_overrides(
            self.inventory,
            {"build.stages.toolchain.python.version": "3.14.7"},
        )
        env = render_build_environment(eff)
        self.assertEqual(env["PYTHON_VERSION"], "3.14.7")


    def test_node_base_image_from_source_metadata(self):
        """NODE_BASE_IMAGE is derived from source.registry/repository, not hardcoded."""
        env = render_build_environment(self.effective)
        image = env["NODE_BASE_IMAGE"]
        node = self.effective.inventory.stages.base.node
        self.assertIn(node.source.registry.rstrip("/"), image)
        self.assertIn(node.source.repository, image)
        self.assertIn(node.tag, image)
        self.assertIn(node.digest, image)
        self.assertTrue(image.endswith(node.digest))
        # Verify not hardcoded: contains registry, not just "node:"
        self.assertIn("/", image)  # registry/repository separator


    def test_source_metadata_change_affects_node_image(self):
        """Changing node source metadata changes NODE_BASE_IMAGE."""
        from tests.versioning.support.inventory_builder import minimal_toml, write_toml
        from docker.versioning.inventory import load_inventory as load_inv

        # Default fixture
        toml_default = minimal_toml()
        path_default = write_toml(toml_default)
        inv_default = load_inv(path_default)
        eff_default = apply_overrides(inv_default, {})
        env_default = render_build_environment(eff_default)

        # Modified fixture with different registry
        toml_modified = minimal_toml(**{
            "build.stages.base.node.source": (
                'type = "docker-registry"\n'
                'registry = "quay.io"\n'
                'repository = "custom/node"\n'
            )
        })
        path_modified = write_toml(toml_modified)
        try:
            inv_modified = load_inv(path_modified)
            eff_modified = apply_overrides(inv_modified, {})
            env_modified = render_build_environment(eff_modified)

            # Default should contain docker.io/library/node
            self.assertIn("docker.io/library/node", env_default["NODE_BASE_IMAGE"])
            # Modified should contain quay.io/custom/node
            self.assertIn("quay.io/custom/node", env_modified["NODE_BASE_IMAGE"])
            # They should differ
            self.assertNotEqual(
                env_default["NODE_BASE_IMAGE"],
                env_modified["NODE_BASE_IMAGE"],
            )
        finally:
            path_default.unlink()
            path_modified.unlink()

    def test_all_values_are_strings(self):
        env = render_build_environment(self.effective)
        for name, value in env.items():
            self.assertIsInstance(value, str, f"{name} is not str: {type(value)}")

    def test_deterministic_ordering(self):
        env1 = render_build_environment(self.effective)
        env2 = render_build_environment(self.effective)
        self.assertEqual(list(env1.keys()), list(env2.keys()))

    def test_no_missing_extensions(self):
        env = render_build_environment(self.effective)
        self.assertIn("PI_READ_VERSION", env)
        self.assertIn("PI_USAGE_VERSION", env)
        self.assertIn("PI_PROXY_VERSION", env)
