"""Stage 2 tests — canonical build section under build.stages.

Task 2.1 — Canonical Build-Schema Tests
Task 2.2 — Migration-Contract Tests
"""
from __future__ import annotations

import sys
import tomllib

from docker.versioning.configuration_document_validation import ConfigurationDocumentError
import unittest
from pathlib import Path
from types import MappingProxyType

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR.parent))
sys.path.insert(0, str(_THIS_DIR))

from docker.versions import InventoryError, load_inventory
from versioning.support.inventory_builder import write_toml


# ── canonical build TOML generator ──────────────────────────────────

def _canonical_build_toml(
    *,
    include_build: bool = True,
    include_runtime: bool = True,
) -> str:
    """Minimal canonical TOML with build.stages + runtime.pi-extensions."""
    parts: list[str] = ["schema = 1\n"]

    if include_build:
        parts.append("""\
[build.stages.base.node]
tag = "24-trixie-slim"
node_version = "24.18.0"
npm_version = "11.16.0"
digest = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

[build.stages.base.node.source]
type = "docker-registry"
registry = "docker.io"
repository = "library/node"

[build.stages.base.node.update]
provider = "docker-registry"
stable_only = true
track = "tag-digest"

[build.stages.toolchain.rust]
version = "1.0.0"
profile = "minimal"
components = ["rustfmt", "clippy"]

[build.stages.toolchain.rust.source]
type = "rust-channel"
manifest = "https://static.rust-lang.org/dist/channel-rust-1.0.0.toml"

[build.stages.toolchain.rust.update]
provider = "rust-channel"
channel = "stable"
stable_only = true

[build.stages.toolchain.rust.rustup.source]
type = "static-url"
checksum_url = "https://static.rust-lang.org/rustup/dist/x86_64-unknown-linux-gnu/rustup-init.sha256"

[build.stages.toolchain.rust.rustup.update]
provider = "static-url"
stable_only = true

[build.stages.toolchain.rust.rustup.artifacts.linux-amd64]
url = "https://static.rust-lang.org/rustup/dist/x86_64-unknown-linux-gnu/rustup-init"
sha256 = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

[build.stages.toolchain.uv]
version = "0.1.0"

[build.stages.toolchain.uv.source]
type = "github-release"
repository = "astral-sh/uv"
tag = "0.1.0"

[build.stages.toolchain.uv.artifacts.linux-amd64]
url = "https://github.com/astral-sh/uv/releases/download/0.1.0/uv-x86_64-unknown-linux-gnu.tar.gz"
sha256 = "0a1b2c3d4e5f67890a1b2c3d4e5f67890a1b2c3d4e5f67890a1b2c3d4e5f6789"

[build.stages.toolchain.uv.update]
provider = "github-release"
stable_only = true
required_platforms = ["linux-amd64"]

[build.stages.toolchain.python]
version = "3.14.6"

[build.stages.toolchain.python.source]
type = "uv-python"
implementation = "cpython"

[build.stages.toolchain.python.update]
provider = "uv-python"
implementation = "cpython"
stable_only = true

[build.stages.toolchain.ty]
version = "0.0.61"

[build.stages.toolchain.ty.source]
type = "pypi"
package = "ty"

[build.stages.toolchain.ty.update]
provider = "pypi"
stable_only = true

[build.stages.rtk-prebuilt.rtk]
version = "v0.43.0"

[build.stages.rtk-prebuilt.rtk.source]
type = "github-release"
repository = "rtk-ai/rtk"
tag = "v0.43.0"

[build.stages.rtk-prebuilt.rtk.artifacts.linux-amd64]
url = "https://github.com/rtk-ai/rtk/releases/download/v0.43.0/rtk_amd64.deb"
sha256 = "eb571d784b3269521722ebe2f0dc2409e89da6bd70bf097ddb21e9d4b3b240b9"

[build.stages.rtk-prebuilt.rtk.update]
provider = "github-release"
stable_only = true
tag_prefix = "v"
required_platforms = ["linux-amd64"]

[build.stages.fd-prebuilt.fd]
version = "v10.4.2"

[build.stages.fd-prebuilt.fd.source]
type = "github-release"
repository = "sharkdp/fd"
tag = "v10.4.2"

[build.stages.fd-prebuilt.fd.artifacts.linux-amd64]
url = "https://github.com/sharkdp/fd/releases/download/v10.4.2/fd_10.4.2_amd64.deb"
sha256 = "0e44eb5fca93f09bc6f5430b90acdf44c8e069d0a903700aeb4820629337b67b"

[build.stages.fd-prebuilt.fd.update]
provider = "github-release"
stable_only = true
tag_prefix = "v"
required_platforms = ["linux-amd64"]

[build.stages.pi-tools.pi]
version = "0.80.10"

[build.stages.pi-tools.pi.source]
type = "pi-release"
package = "@earendil-works/pi-coding-agent"
release_repository = "earendil-works/pi"
release_tag_prefix = "v"

[build.stages.pi-tools.pi.update]
provider = "npm"
stable_only = true

[build.stages.openspec-tools.openspec]
version = "1.6.0"

[build.stages.openspec-tools.openspec.source]
type = "npm"
package = "@fission-ai/openspec"

[build.stages.openspec-tools.openspec.update]
provider = "npm"
stable_only = true

[build.stages.runtime.oh-my-zsh]
revision = "70ad5e3df8f7bed68aa6672029496926e632aedd"

[build.stages.runtime.oh-my-zsh.source]
type = "git"
repository = "https://github.com/ohmyzsh/ohmyzsh.git"

[build.stages.runtime.oh-my-zsh.update]
provider = "git-ref"
ref = "master"
""")

    if include_runtime:
        parts.append("""\
[runtime.pi-extensions.pi-read]
version = "0.2.0"

[runtime.pi-extensions.pi-read.source]
type = "npm"
package = "@arcanemachine/pi-read"

[runtime.pi-extensions.pi-read.artifacts]
[runtime.pi-extensions.pi-read.artifacts."0.2.0"]
url = "https://registry.npmjs.org/@arcanemachine/pi-read/-/pi-read-0.2.0.tgz"
integrity = "sha512-VO9pV15PFTBOfcNq9hgKJ3K6k4Bb0ndDlX6N5ReNTOa/r66/Ppfc9N/hexsK5veMHGl1YbjCo3wOnL5jJu17/Q=="

[runtime.pi-extensions.pi-read.update]
provider = "npm"
stable_only = true

[runtime.pi-extensions.pi-read.override]
constraint = ">=0.2.0"
allow_prerelease = false
scheme = "numeric"

[runtime.pi-extensions.pi-read.validation]
metadata_file = "package.json"
""")

    return "".join(parts)


def _legacy_stages_toml() -> str:
    """Valid TOML using the legacy [stages.*] top-level format."""
    return _canonical_build_toml().replace("[build.", "[")


# ===================================================================
# Task 2.1 — Canonical Build-Schema Tests
# ===================================================================

class TestCanonicalBuildSchema(unittest.TestCase):
    """build.stages.* is the canonical build path."""

    _temp_files: list[Path] = []

    @classmethod
    def setUpClass(cls):
        cls._temp_files = []
        cls.canonical_path = cls._write(_canonical_build_toml())
        cls.canonical_inv = load_inventory(cls.canonical_path)

    @classmethod
    def tearDownClass(cls):
        for p in cls._temp_files:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

    @classmethod
    def _write(cls, content: str) -> Path:
        p = write_toml(content)
        cls._temp_files.append(p)
        return p

    # ── structural requirements ─────────────────────────────────

    def test_build_is_required(self):
        """build table is required."""
        t = _canonical_build_toml(include_build=False)
        p = self._write(t)
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(p)
        self.assertIn("build", str(ctx.exception))

    def test_build_must_be_table(self):
        """build must be a table, not a scalar."""
        t = """\
schema = 1
build = 42

[runtime.pi-extensions.pi-test]
version = "1.0.0"

[runtime.pi-extensions.pi-test.source]
type = "npm"
package = "pi-test"

[runtime.pi-extensions.pi-test.artifacts]
[runtime.pi-extensions.pi-test.artifacts."1.0.0"]
url = "https://registry.npmjs.org/pi-test/-/pi-test-1.0.0.tgz"
integrity = "sha512-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=="

[runtime.pi-extensions.pi-test.update]
provider = "npm"
stable_only = true

[runtime.pi-extensions.pi-test.override]
constraint = ">=1.0.0"
allow_prerelease = false
scheme = "numeric"

[runtime.pi-extensions.pi-test.validation]
metadata_file = "package.json"
"""
        p = self._write(t)
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(p)
        self.assertIn("build", str(ctx.exception))

    def test_build_stages_is_required(self):
        """build.stages is required."""
        t = _canonical_build_toml().replace(
            "[build.stages.base.node]", "build = 42\n[build.stages.base.node]")
        p = self._write(t)
        with self.assertRaises(ConfigurationDocumentError):
            load_inventory(p)

    def test_build_stages_must_be_table(self):
        """build.stages must be a table, not a scalar."""
        t = """\
schema = 1

[build]
stages = true

[runtime.pi-extensions.pi-test]
version = "1.0.0"

[runtime.pi-extensions.pi-test.source]
type = "npm"
package = "pi-test"

[runtime.pi-extensions.pi-test.artifacts]
[runtime.pi-extensions.pi-test.artifacts."1.0.0"]
url = "https://registry.npmjs.org/pi-test/-/pi-test-1.0.0.tgz"
integrity = "sha512-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=="

[runtime.pi-extensions.pi-test.update]
provider = "npm"
stable_only = true

[runtime.pi-extensions.pi-test.override]
constraint = ">=1.0.0"
allow_prerelease = false
scheme = "numeric"

[runtime.pi-extensions.pi-test.validation]
metadata_file = "package.json"
"""
        p = self._write(t)
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(p)
        self.assertIn("stages", str(ctx.exception))

    def test_legacy_top_level_stages_rejected(self):
        """[stages.*] top-level format MUST be rejected."""
        t = _legacy_stages_toml()
        p = self._write(t)
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(p)
        self.assertIn("stages", str(ctx.exception))

    def test_canonical_and_legacy_cannot_coexist(self):
        """build + top-level stages together is rejected."""
        t = _canonical_build_toml()
        # Inject a legacy stages section after the build section
        t += "\n[stages.extra.something]\nversion = \"1.0\"\n"
        p = self._write(t)
        with self.assertRaises(ConfigurationDocumentError) as ctx:
            load_inventory(p)
        self.assertEqual("stages", ctx.exception.field)

    # ── key & schema checks ──────────────────────────────────────

    def test_unknown_keys_under_build_rejected(self):
        """Unknown top-level keys under build are rejected."""
        t = _canonical_build_toml()
        t = t.replace("[build.stages.base.node]", "[build.extra_section]\nkey = true\n[build.stages.base.node]")
        p = self._write(t)
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(p)
        self.assertIn("build", str(ctx.exception))

    def test_unknown_build_stage_rejected(self):
        """Unknown build stage named 'fantasy' is rejected."""
        t = _canonical_build_toml()
        t += "\n[build.stages.fantasy.widget]\nversion = \"1.0\"\n"
        p = self._write(t)
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(p)
        self.assertIn("fantasy", str(ctx.exception).lower())

    # ── canonical error paths ────────────────────────────────────

    def test_misspelled_field_reports_canonical_path(self):
        """Misspelled field 'vesrion' under build reports build.stages.* path."""
        t = _canonical_build_toml().replace(
            'version = "3.14.6"',
            'vesrion = "3.14.6"',
        )
        p = self._write(t)
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(p)
        msg = str(ctx.exception)
        self.assertIn("build.stages.toolchain.python", msg)
        self.assertIn("vesrion", msg)

    def test_missing_field_reports_canonical_path(self):
        """Missing digest field reports build.stages.* path."""
        t = _canonical_build_toml().replace(
            'digest = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"',
            "",
        )
        p = self._write(t)
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(p)
        self.assertIn("build.stages.base.node.digest", str(ctx.exception))

    # ── phase separation ─────────────────────────────────────────

    def test_pi_extensions_under_build_rejected(self):
        """Pi extensions may not appear under build."""
        t = _canonical_build_toml()
        t += "\n[build.stages.pi-extensions.something]\nversion = \"1.0\"\n"
        p = self._write(t)
        with self.assertRaises(InventoryError):
            load_inventory(p)

    def test_operational_fields_under_build_rejected(self):
        """Operational fields (gateway, uid, gid, launcher, etc.) may not
        appear under build."""
        for field in ("gateway", "uid", "gid", "launcher",
                       "project_path", "ownership"):
            t = _canonical_build_toml()
            t = t.replace("[build.stages.base.node]",
                          f"[build.{field}]\nval = true\n[build.stages.base.node]")
            p = self._write(t)
            with self.subTest(field=field):
                with self.assertRaises(InventoryError) as ctx:
                    load_inventory(p)
                self.assertIn("build", str(ctx.exception))

    # ── existing validation still works with canonical paths ─────

    def test_invalid_source_update_combination_reports_canonical_path(self):
        """Mismatched source/update provider reports build.stages.* path."""
        t = _canonical_build_toml().replace(
            'provider = "docker-registry"',
            'provider = "npm"',
            1,  # first occurrence (base node update)
        )
        p = self._write(t)
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(p)
        self.assertIn("build.stages.base.node", str(ctx.exception))

    def test_invalid_artifact_reports_canonical_path(self):
        """Invalid artifact checksum reports build.stages.* path."""
        t = _canonical_build_toml().replace(
            "0a1b2c3d4e5f67890a1b2c3d4e5f67890a1b2c3d4e5f67890a1b2c3d4e5f6789",
            "not-a-sha256",
        )
        p = self._write(t)
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(p)
        self.assertIn("build.stages.toolchain.uv.artifacts", str(ctx.exception))


# ===================================================================
# Task 2.2 — Migration-Contract Tests
# ===================================================================

class TestMigrationContract(unittest.TestCase):
    """Pre-migration snapshot to protect dependency values across the rename."""

    _temp_files: list[Path] = []

    @classmethod
    def setUpClass(cls):
        cls._temp_files = []
        # Load the authoritative file (will work through legacy adapter
        # before migration; directly after migration)
        cls._repo_toml = Path(__file__).resolve().parents[1] / "docker-constructor.toml"
        cls.pre_inv = load_inventory(cls._repo_toml)

    @classmethod
    def tearDownClass(cls):
        for p in cls._temp_files:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

    @classmethod
    def _write(cls, content: str) -> Path:
        p = write_toml(content)
        cls._temp_files.append(p)
        return p

    def test_node_tag_and_digest_preserved(self):
        node = self.pre_inv.stages.base.node
        self.assertIsInstance(node.tag, str)
        self.assertTrue(len(node.tag) > 0)
        self.assertIsInstance(node.digest, str)
        self.assertTrue(node.digest.startswith("sha256:"))
        self.assertEqual(len(node.digest), len("sha256:") + 64)

    def test_rust_version_profile_components_preserved(self):
        rust = self.pre_inv.stages.toolchain.rust
        self.assertIsInstance(rust.version, str)
        self.assertRegex(rust.version, r"^\d+\.\d+\.\d+$")
        self.assertIn(rust.profile, ("minimal", "default", "complete"))
        self.assertIsInstance(rust.components, tuple)
        self.assertIn("rustfmt", rust.components)

    def test_rustup_artifact_preserved(self):
        rust = self.pre_inv.stages.toolchain.rust
        self.assertIsInstance(rust.rustup, MappingProxyType)
        self.assertIn("linux-amd64", rust.rustup)
        art = rust.rustup["linux-amd64"]
        self.assertTrue(art.url.startswith("https://"))
        self.assertEqual(len(art.sha256), 64)

    def test_uv_version_repository_artifacts_preserved(self):
        uv = self.pre_inv.stages.toolchain.uv
        self.assertIsInstance(uv.version, str)
        self.assertIsInstance(uv.source.repository, str)
        self.assertIsInstance(uv.artifacts, MappingProxyType)
        self.assertIn("linux-amd64", uv.artifacts)
        art = uv.artifacts["linux-amd64"]
        self.assertEqual(len(art.sha256), 64)

    def test_python_version_source_preserved(self):
        py = self.pre_inv.stages.toolchain.python
        self.assertIsInstance(py.version, str)
        self.assertRegex(py.version, r"^\d+\.\d+\.\d+$")
        self.assertEqual(py.source.implementation, "cpython")

    def test_ty_version_and_package_preserved(self):
        ty = self.pre_inv.stages.toolchain.ty
        self.assertIsInstance(ty.version, str)
        self.assertEqual(ty.source.type, "pypi")
        self.assertEqual(ty.source.package, "ty")

    def test_rtk_version_artifacts_preserved(self):
        rtk = self.pre_inv.stages.rtk_prebuilt.rtk
        self.assertIsInstance(rtk.version, str)
        self.assertTrue(rtk.version.startswith("v"))
        self.assertIsInstance(rtk.artifacts, MappingProxyType)
        self.assertIn("linux-amd64", rtk.artifacts)
        art = rtk.artifacts["linux-amd64"]
        self.assertTrue(art.url.endswith(".deb"))

    def test_fd_version_artifacts_preserved(self):
        fd = self.pre_inv.stages.fd_prebuilt.fd
        self.assertIsInstance(fd.version, str)
        self.assertTrue(fd.version.startswith("v"))
        self.assertIsInstance(fd.artifacts, MappingProxyType)
        art = fd.artifacts["linux-amd64"]
        self.assertTrue(art.url.endswith(".deb"))

    def test_pi_package_and_version_preserved(self):
        pi = self.pre_inv.stages.pi_tools.pi
        self.assertIsInstance(pi.version, str)
        self.assertEqual(pi.source.type, "pi-release")
        self.assertEqual(pi.source.package, "@earendil-works/pi-coding-agent")
        self.assertEqual(pi.source.release_repository, "earendil-works/pi")
        self.assertEqual(pi.source.release_tag_prefix, "v")

    def test_openspec_package_and_version_preserved(self):
        openspec = self.pre_inv.stages.openspec_tools.openspec
        self.assertIsInstance(openspec.version, str)
        self.assertEqual(openspec.source.type, "npm")
        self.assertEqual(openspec.source.package, "@fission-ai/openspec")

    def test_oh_my_zsh_revision_and_repository_preserved(self):
        omz = self.pre_inv.stages.runtime.oh_my_zsh
        self.assertIsInstance(omz.revision, str)
        self.assertEqual(len(omz.revision), 40)
        self.assertEqual(omz.source.type, "git")

    def test_all_build_dependencies_under_build_stages(self):
        """Every build dependency path begins with build.stages."""
        st = self.pre_inv.stages
        for stage_name in (
            "base", "toolchain", "rtk_prebuilt", "fd_prebuilt",
            "pi_tools", "openspec_tools", "runtime",
        ):
            self.assertTrue(
                hasattr(st, stage_name),
                f"build.stages must contain {stage_name}",
            )

    def test_no_build_dependency_at_top_level_stages(self):
        """When the file is parsed after migration, parsing starts from
        build.stages, not top-level stages."""
        p = self._write(_canonical_build_toml())
        inv = load_inventory(p)
        self.assertIsNotNone(inv.build)
        self.assertIsNotNone(inv.runtime)
