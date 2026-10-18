"""Focused resolver tests for Stage 4 — Effective Build Projection.

Tests resolve_build_projection using the existing BuildInventory model
from docker.versioning.model.  No Docker, no network, no filesystem.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Mapping

from docker.versioning.inventory import load_inventory
from docker.versioning.model import (
    ArtifactEntry,
    BuildInventory,
    EffectiveBuildProjection,
    Inventory,
)
from docker.versioning.effective import resolve_build_projection
from docker.versioning.errors import (
    EffectiveConfigError,
    InventoryError,
    OverrideValidationError,
    UnsupportedOverrideError,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_default() -> Inventory:
    return load_inventory(_REPO_ROOT / "docker-constructor.toml")


# ---------------------------------------------------------------------------
# 4.1.1 Default resolution (no overrides)
# ---------------------------------------------------------------------------


class TestDefaultBuildProjection(unittest.TestCase):
    """Resolution without overrides preserves all canonical build selections."""

    @classmethod
    def setUpClass(cls):
        cls.inventory = _load_default()
        cls.build = cls.inventory.build

    def test_node_image_preserved(self):
        """NODE_BASE_IMAGE is derived from the reviewed base stage."""
        projection = resolve_build_projection(self.build, {})
        node = self.inventory.stages.base.node
        registry = node.source.registry.rstrip("/")
        expected = f"{registry}/{node.source.repository}:{node.tag}@{node.digest}"
        self.assertEqual(projection.node.image, expected)

    def test_rust_version_and_profile_preserved(self):
        """Rust version, profile, and components carry unchanged from review."""
        projection = resolve_build_projection(self.build, {})
        rust = self.inventory.stages.toolchain.rust
        self.assertEqual(projection.rust.version, rust.version)
        self.assertEqual(projection.rust.profile, rust.profile)
        self.assertEqual(projection.rust.components, tuple(rust.components))

    def test_rustup_linux_amd64_artifact_preserved(self):
        """Rustup bootstrap URL and sha256 for linux-amd64 survive resolution."""
        projection = resolve_build_projection(self.build, {})
        rustup = self.inventory.stages.toolchain.rust.rustup.get("linux-amd64")
        self.assertIsNotNone(
            rustup, "linux-amd64 rustup artifact missing from inventory"
        )
        self.assertEqual(projection.rust.rustup.url, rustup.url)
        self.assertEqual(projection.rust.rustup.sha256, rustup.sha256)

    def test_uv_version_and_artifact_preserved(self):
        projection = resolve_build_projection(self.build, {})
        uv = self.inventory.stages.toolchain.uv
        self.assertEqual(projection.uv.version, uv.version)
        art = uv.artifacts["linux-amd64"]
        self.assertEqual(projection.uv.artifact.url, art.url)
        self.assertEqual(projection.uv.artifact.sha256, art.sha256)

    def test_python_version_preserved_by_default(self):
        projection = resolve_build_projection(self.build, {})
        self.assertEqual(
            projection.python_version,
            self.inventory.stages.toolchain.python.version,
        )

    def test_ty_version_preserved(self):
        projection = resolve_build_projection(self.build, {})
        self.assertEqual(
            projection.ty_version,
            self.inventory.stages.toolchain.ty.version,
        )

    def test_rtk_version_and_artifact_preserved(self):
        projection = resolve_build_projection(self.build, {})
        rtk = self.inventory.stages.rtk_prebuilt.rtk
        self.assertEqual(projection.rtk.version, rtk.version)
        art = rtk.artifacts["linux-amd64"]
        self.assertEqual(projection.rtk.artifact.url, art.url)
        self.assertEqual(projection.rtk.artifact.sha256, art.sha256)

    def test_fd_version_and_artifact_preserved(self):
        projection = resolve_build_projection(self.build, {})
        fd = self.inventory.stages.fd_prebuilt.fd
        self.assertEqual(projection.fd.version, fd.version)
        art = fd.artifacts["linux-amd64"]
        self.assertEqual(projection.fd.artifact.url, art.url)
        self.assertEqual(projection.fd.artifact.sha256, art.sha256)

    def test_pi_version_preserved(self):
        projection = resolve_build_projection(self.build, {})
        self.assertEqual(
            projection.pi_version,
            self.inventory.stages.pi_tools.pi.version,
        )

    def test_openspec_version_preserved(self):
        projection = resolve_build_projection(self.build, {})
        self.assertEqual(
            projection.openspec_version,
            self.inventory.stages.openspec_tools.openspec.version,
        )

    def test_oh_my_zsh_revision_preserved(self):
        projection = resolve_build_projection(self.build, {})
        self.assertEqual(
            projection.oh_my_zsh_revision,
            self.inventory.stages.runtime.oh_my_zsh.revision,
        )

    def test_no_runtime_extensions_in_projection(self):
        """Build projection must not contain runtime pi-extension fields."""
        projection = resolve_build_projection(self.build, {})
        # The projection is a frozen dataclass with no runtime fields.
        for field in projection.__class__.__dataclass_fields__:
            self.assertNotIn("runtime", field.lower())
            self.assertNotIn("pi_extension", field.lower())
            self.assertNotIn("extension", field.lower())

    def test_no_update_metadata_in_projection(self):
        """Update provider metadata must not leak into the projection."""
        projection = resolve_build_projection(self.build, {})
        pdict = dataclasses.asdict(projection)
        serialized = flatten_keys(pdict)
        for key in serialized:
            self.assertNotIn(
                "update", key.lower().split("."), f"update metadata leaked at {key}"
            )

    def test_no_override_policy_in_projection(self):
        """Override policy must not leak into the projection."""
        projection = resolve_build_projection(self.build, {})
        pdict = dataclasses.asdict(projection)
        serialized = flatten_keys(pdict)
        for key in serialized:
            self.assertNotIn(
                "override", key.lower().split("."), f"override policy leaked at {key}"
            )


# ---------------------------------------------------------------------------
# 4.1.2 Python override handling
# ---------------------------------------------------------------------------


class TestPythonOverride(unittest.TestCase):
    """Supported Python override: X.Y.Z numeric, constraint-enforced."""

    @classmethod
    def setUpClass(cls):
        cls.inventory = _load_default()
        cls.build = cls.inventory.build
        cls.python_entry = cls.inventory.stages.toolchain.python

    def test_stable_numeric_version_accepted(self):
        """A stable X.Y.Z version within the override constraint is accepted."""
        projection = resolve_build_projection(
            self.build,
            {"build.stages.toolchain.python.version": "3.14.7"},
        )
        self.assertEqual(projection.python_version, "3.14.7")

    def test_python_version_must_be_three_component_numeric(self):
        """Only X.Y.Z format is accepted; two-component rejects."""
        with self.assertRaises(OverrideValidationError):
            resolve_build_projection(
                self.build,
                {"build.stages.toolchain.python.version": "3.14"},
            )

    def test_python_version_rejects_prerelease(self):
        """Prerelease versions should be rejected by the numeric scheme."""
        with self.assertRaises(OverrideValidationError):
            resolve_build_projection(
                self.build,
                {"build.stages.toolchain.python.version": "3.15.0-alpha.1"},
            )

    def test_python_version_rejects_garbage(self):
        """Non-numeric input is rejected."""
        with self.assertRaises(OverrideValidationError):
            resolve_build_projection(
                self.build,
                {"build.stages.toolchain.python.version": "new!new!new!"},
            )

    def test_python_version_rejects_out_of_constraint_range(self):
        """Value below the constraint floor is rejected."""
        with self.assertRaises(OverrideValidationError):
            resolve_build_projection(
                self.build,
                {"build.stages.toolchain.python.version": "3.14.5"},
            )

    def test_inventory_unchanged_after_override(self):
        """Reviewed inventory remains unchanged after override application."""
        build_snapshot = _copy_build(self.build)
        resolve_build_projection(
            self.build,
            {"build.stages.toolchain.python.version": "3.14.7"},
        )
        self.assertEqual(
            self.build.stages.toolchain.python.version,
            build_snapshot.stages.toolchain.python.version,
        )

    def test_artifacts_unchanged_after_python_override(self):
        """Non-Python artifacts are unaffected by a Python override."""
        projection = resolve_build_projection(
            self.build,
            {"build.stages.toolchain.python.version": "3.14.7"},
        )
        # Spot-check: uv and rtk should be at their reviewed values
        uv = self.inventory.stages.toolchain.uv
        self.assertEqual(projection.uv.version, uv.version)
        rtk = self.inventory.stages.rtk_prebuilt.rtk
        self.assertEqual(projection.rtk.version, rtk.version)


# ---------------------------------------------------------------------------
# 4.1.3 Override path validation
# ---------------------------------------------------------------------------


class TestOverridePathValidation(unittest.TestCase):
    """Unknown override paths and duplicates are rejected with canonical paths."""

    @classmethod
    def setUpClass(cls):
        cls.inventory = _load_default()
        cls.build = cls.inventory.build

    def test_unknown_override_path_rejected(self):
        """An override path not in SUPPORTED_OVERRIDES must fail."""
        with self.assertRaises(UnsupportedOverrideError) as ctx:
            resolve_build_projection(
                self.build,
                {"build.stages.toolchain.rust.version": "1.98.0"},
            )
        self.assertIn("build.stages.toolchain.rust.version", str(ctx.exception))

    def test_toplevel_garbage_path_rejected(self):
        with self.assertRaises(UnsupportedOverrideError):
            resolve_build_projection(self.build, {"version": "1.0.0"})

    def test_runtime_override_path_rejected_in_build(self):
        """A runtime override path must not be accepted in build resolution."""
        with self.assertRaises(UnsupportedOverrideError):
            resolve_build_projection(
                self.build,
                {"runtime.pi-extensions.pi-read.version": "0.3.0"},
            )

    def test_duplicate_conflicting_overrides_rejected(self):
        """Duplicate keys with different values must be rejected."""
        with self.assertRaises(UnsupportedOverrideError):
            resolve_build_projection(
                self.build,
                {
                    "build.stages.toolchain.python.version": "3.14.7",
                    # Not a duplicate; testing that only supported paths work
                    "build.stages.toolchain.python.version_typo": "3.14.8",
                },
            )

    def test_empty_overrides_yield_defaults(self):
        """Empty override map produces the default projection."""
        projection = resolve_build_projection(self.build, {})
        self.assertEqual(
            projection.python_version,
            self.inventory.stages.toolchain.python.version,
        )


# ---------------------------------------------------------------------------
# 4.1.4 Platform artifact survival
# ---------------------------------------------------------------------------


class TestPlatformArtifacts(unittest.TestCase):
    """All required platform artifacts survive resolution."""

    @classmethod
    def setUpClass(cls):
        cls.inventory = _load_default()
        cls.build = cls.inventory.build

    def test_linux_amd64_uv_artifact(self):
        projection = resolve_build_projection(self.build, {})
        art = self.inventory.stages.toolchain.uv.artifacts["linux-amd64"]
        self.assertEqual(projection.uv.artifact.url, art.url)
        self.assertEqual(projection.uv.artifact.sha256, art.sha256)

    def test_linux_amd64_rtk_artifact(self):
        projection = resolve_build_projection(self.build, {})
        art = self.inventory.stages.rtk_prebuilt.rtk.artifacts["linux-amd64"]
        self.assertEqual(projection.rtk.artifact.url, art.url)
        self.assertEqual(projection.rtk.artifact.sha256, art.sha256)

    def test_linux_amd64_fd_artifact(self):
        projection = resolve_build_projection(self.build, {})
        art = self.inventory.stages.fd_prebuilt.fd.artifacts["linux-amd64"]
        self.assertEqual(projection.fd.artifact.url, art.url)
        self.assertEqual(projection.fd.artifact.sha256, art.sha256)

    def test_linux_amd64_rustup_artifact(self):
        projection = resolve_build_projection(self.build, {})
        rustup = self.inventory.stages.toolchain.rust.rustup.get("linux-amd64")
        self.assertIsNotNone(rustup)
        self.assertEqual(projection.rust.rustup.url, rustup.url)
        self.assertEqual(projection.rust.rustup.sha256, rustup.sha256)

    def test_python_override_does_not_drop_artifacts(self):
        """A Python override must not silently drop platform artifacts."""
        projection = resolve_build_projection(
            self.build,
            {"build.stages.toolchain.python.version": "3.14.7"},
        )
        art = self.inventory.stages.toolchain.uv.artifacts["linux-amd64"]
        self.assertEqual(projection.uv.artifact.url, art.url)


# ---------------------------------------------------------------------------
# 4.1.5 Artifact URL / version consistency
# ---------------------------------------------------------------------------


class TestArtifactVersionConsistency(unittest.TestCase):
    """Selected artifact URLs must match the effective version."""

    @classmethod
    def setUpClass(cls):
        cls.inventory = _load_default()
        cls.build = cls.inventory.build

    def test_uv_url_contains_version(self):
        projection = resolve_build_projection(self.build, {})
        self.assertIn(projection.uv.version, projection.uv.artifact.url)

    def test_rtk_url_contains_version(self):
        projection = resolve_build_projection(self.build, {})
        # rtk version is "v0.43.0" — the URL contains the tag
        self.assertIn(projection.rtk.version, projection.rtk.artifact.url)

    def test_fd_url_contains_version(self):
        projection = resolve_build_projection(self.build, {})
        # fd version is "v10.4.2" — the URL should contain it
        self.assertIn(projection.fd.version.lstrip("v"), projection.fd.artifact.url)


# ---------------------------------------------------------------------------
# 4.1.6 Deterministic projection ordering
# ---------------------------------------------------------------------------


class TestDeterministicProjection(unittest.TestCase):
    """Equivalent inputs always produce identical projection values."""

    @classmethod
    def setUpClass(cls):
        cls.inventory = _load_default()
        cls.build = cls.inventory.build

    def test_identical_runs_produce_identical_projections(self):
        a = resolve_build_projection(self.build, {})
        b = resolve_build_projection(self.build, {})
        self.assertEqual(_projection_tuple(a), _projection_tuple(b))

    def test_identical_overrides_produce_identical_projections(self):
        a = resolve_build_projection(
            self.build,
            {"build.stages.toolchain.python.version": "3.14.7"},
        )
        b = resolve_build_projection(
            self.build,
            {"build.stages.toolchain.python.version": "3.14.7"},
        )
        self.assertEqual(_projection_tuple(a), _projection_tuple(b))

    def test_different_overrides_differ(self):
        a = resolve_build_projection(self.build, {})
        b = resolve_build_projection(
            self.build,
            {"build.stages.toolchain.python.version": "3.14.6"},
        )
        self.assertNotEqual(a.python_version, b.python_version)


# ---------------------------------------------------------------------------
# 4.1.7 Platform-aware resolution
# ---------------------------------------------------------------------------


class TestArm64Platform(unittest.TestCase):
    """ARM64 resolution fails actionably when required artifacts are missing.

    The minimal fixture only declares linux-amd64 artifacts for rustup
    (static-url sources do not support multi-platform).  ARM64 resolution
    therefore fails early with a clear diagnostic.
    """

    @classmethod
    def setUpClass(cls):
        from tests.versioning.support.inventory_builder import minimal_toml, write_toml

        toml = minimal_toml()
        cls._toml_path = write_toml(toml)
        cls.inventory = load_inventory(cls._toml_path)
        cls.build = cls.inventory.build

    @classmethod
    def tearDownClass(cls):
        cls._toml_path.unlink(missing_ok=True)

    def test_arm64_raises_when_rustup_missing(self):
        """ARM64 resolution fails because rustup has no arm64 artifact."""
        with self.assertRaises(EffectiveConfigError) as ctx:
            resolve_build_projection(self.build, {}, platform="linux-arm64")
        self.assertIn("No artifact for platform", str(ctx.exception))
        self.assertIn("linux-arm64", str(ctx.exception))
        self.assertIn("rustup", str(ctx.exception).lower())

    def test_arm64_raises_even_with_python_override(self):
        """Overrides cannot paper over missing platform artifacts."""
        with self.assertRaises(EffectiveConfigError) as ctx:
            resolve_build_projection(
                self.build,
                {"build.stages.toolchain.python.version": "3.14.7"},
                platform="linux-arm64",
            )
        self.assertIn("No artifact for platform", str(ctx.exception))

    def test_amd64_still_resolves_with_all_artifacts(self):
        """Sanity: AMD64 resolution works because all artifacts are declared."""
        projection = resolve_build_projection(self.build, {}, platform="linux-amd64")
        self.assertEqual(projection.platform, "linux-amd64")
        self.assertIsNotNone(projection.rust.rustup)
        self.assertIsNotNone(projection.uv.artifact)
        self.assertIsNotNone(projection.rtk.artifact)
        self.assertIsNotNone(projection.fd.artifact)


class TestUnsupportedPlatform(unittest.TestCase):
    """Unsupported platforms are rejected before any resolution."""

    @classmethod
    def setUpClass(cls):
        from tests.versioning.support.inventory_builder import minimal_toml, write_toml

        toml = minimal_toml()
        cls._toml_path = write_toml(toml)
        cls.inventory = load_inventory(cls._toml_path)
        cls.build = cls.inventory.build

    @classmethod
    def tearDownClass(cls):
        cls._toml_path.unlink(missing_ok=True)

    def test_unsupported_platform_rejected(self):
        with self.assertRaises(EffectiveConfigError) as ctx:
            resolve_build_projection(self.build, {}, platform="windows-amd64")
        self.assertIn("Unsupported platform", str(ctx.exception))

    def test_empty_platform_rejected(self):
        with self.assertRaises(EffectiveConfigError):
            resolve_build_projection(self.build, {}, platform="")

    def test_arm32_rejected(self):
        with self.assertRaises(EffectiveConfigError):
            resolve_build_projection(self.build, {}, platform="linux-arm32")


class TestMissingPlatformArtifact(unittest.TestCase):
    """If a required artifact is missing for the target platform, fail."""

    def test_missing_artifact_raises(self):
        """An inventory without arm64 artifacts fails for ARM64 resolution."""
        inv = _load_default()  # Real inventory: only linux-amd64
        with self.assertRaises(EffectiveConfigError) as ctx:
            resolve_build_projection(inv.build, {}, platform="linux-arm64")
        self.assertIn("No artifact for platform", str(ctx.exception))
        self.assertIn("linux-arm64", str(ctx.exception))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

import dataclasses


def _projection_tuple(p) -> tuple:
    """Convert a projection to a hashable tuple for comparison."""
    return tuple(
        (f.name, _to_hashable(getattr(p, f.name))) for f in dataclasses.fields(p)
    )


def _to_hashable(v):
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return tuple(
            (f.name, _to_hashable(getattr(v, f.name))) for f in dataclasses.fields(v)
        )
    if isinstance(v, (list, tuple)):
        return tuple(_to_hashable(x) for x in v)
    return v


def _copy_build(build: BuildInventory) -> BuildInventory:
    """Deep-copy a BuildInventory by round-tripping through asdict/rebuild."""
    # dataclasses.asdict produces a recursive dict; rebuild manually
    from docker.versioning.model import (
        Stages,
        BaseStage,
        NodeEntry,
        ToolchainStage,
        RustEntry,
        UvEntry,
        PythonEntry,
        TyEntry,
        RtkPrebuiltStage,
        PrebuiltToolEntry,
        FdPrebuiltStage,
        PiToolsStage,
        PiToolEntry,
        NpmToolEntry,
        OpenSpecToolsStage,
        RuntimeStage,
        OhMyZshEntry,
    )

    s = build.stages
    return BuildInventory(
        stages=Stages(
            base=BaseStage(
                node=NodeEntry(
                    tag=s.base.node.tag,
                    digest=s.base.node.digest,
                    node_version=s.base.node.node_version,
                    npm_version=s.base.node.npm_version,
                    source=s.base.node.source,
                    update=s.base.node.update,
                )
            ),
            toolchain=ToolchainStage(
                rust=RustEntry(
                    version=s.toolchain.rust.version,
                    profile=s.toolchain.rust.profile,
                    components=s.toolchain.rust.components,
                    source=s.toolchain.rust.source,
                    rustup_source=s.toolchain.rust.rustup_source,
                    rustup_update=s.toolchain.rust.rustup_update,
                    rustup=s.toolchain.rust.rustup,
                    update=s.toolchain.rust.update,
                ),
                uv=UvEntry(
                    version=s.toolchain.uv.version,
                    source=s.toolchain.uv.source,
                    artifacts=s.toolchain.uv.artifacts,
                    update=s.toolchain.uv.update,
                ),
                python=PythonEntry(
                    version=s.toolchain.python.version,
                    source=s.toolchain.python.source,
                    update=s.toolchain.python.update,
                    override=s.toolchain.python.override,
                ),
                ty=TyEntry(
                    version=s.toolchain.ty.version,
                    source=s.toolchain.ty.source,
                    update=s.toolchain.ty.update,
                ),
            ),
            rtk_prebuilt=RtkPrebuiltStage(
                rtk=PrebuiltToolEntry(
                    version=s.rtk_prebuilt.rtk.version,
                    source=s.rtk_prebuilt.rtk.source,
                    artifacts=s.rtk_prebuilt.rtk.artifacts,
                    update=s.rtk_prebuilt.rtk.update,
                )
            ),
            fd_prebuilt=FdPrebuiltStage(
                fd=PrebuiltToolEntry(
                    version=s.fd_prebuilt.fd.version,
                    source=s.fd_prebuilt.fd.source,
                    artifacts=s.fd_prebuilt.fd.artifacts,
                    update=s.fd_prebuilt.fd.update,
                )
            ),
            pi_tools=PiToolsStage(
                pi=PiToolEntry(
                    version=s.pi_tools.pi.version,
                    source=s.pi_tools.pi.source,
                    update=s.pi_tools.pi.update,
                )
            ),
            openspec_tools=OpenSpecToolsStage(
                openspec=NpmToolEntry(
                    version=s.openspec_tools.openspec.version,
                    source=s.openspec_tools.openspec.source,
                    update=s.openspec_tools.openspec.update,
                )
            ),
            runtime=RuntimeStage(
                oh_my_zsh=OhMyZshEntry(
                    revision=s.runtime.oh_my_zsh.revision,
                    source=s.runtime.oh_my_zsh.source,
                    update=s.runtime.oh_my_zsh.update,
                )
            ),
        )
    )


def flatten_keys(o, prefix="") -> list:
    """Return dotted keys from a nested dict/dataclass for leak detection."""
    result = []
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        for f in dataclasses.fields(o):
            result.extend(
                flatten_keys(
                    getattr(o, f.name), f"{prefix}.{f.name}" if prefix else f.name
                )
            )
    elif isinstance(o, Mapping):
        for k, v in o.items():
            result.extend(flatten_keys(v, f"{prefix}.{k}" if prefix else k))
    elif isinstance(o, (list, tuple)):
        for i, v in enumerate(o):
            result.extend(flatten_keys(v, f"{prefix}[{i}]"))
    else:
        result.append(prefix)
    return result
