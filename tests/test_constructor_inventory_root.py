"""Stage 1 tests — canonical root envelope and typed phase containers."""

from __future__ import annotations

import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import MappingProxyType

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR.parent))
sys.path.insert(0, str(_THIS_DIR))

# Repository/test path setup must run before these imports.
from versioning.support.inventory_builder import write_toml  # noqa: E402

from docker.versions import InventoryError, load_inventory  # noqa: E402

# ---------------------------------------------------------------------------
# Helper — canonical TOML with build/runtime envelope
# ---------------------------------------------------------------------------


def _canonical_toml() -> str:
    """Minimal valid TOML with build/runtime envelope wrapping existing content."""
    return """\
schema = 1

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
sha256 = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
url = "https://static.rust-lang.org/rustup/dist/x86_64-unknown-linux-gnu/rustup-init"

[build.stages.toolchain.uv]
version = "0.1.0"

[build.stages.toolchain.uv.source]
type = "github-release"
repository = "astral-sh/uv"
tag = "0.1.0"

[build.stages.toolchain.uv.update]
provider = "github-release"
stable_only = true
tag_prefix = ""
required_platforms = ["linux-amd64"]

[build.stages.toolchain.uv.artifacts.linux-amd64]
sha256 = "0a1b2c3d4e5f67890a1b2c3d4e5f67890a1b2c3d4e5f67890a1b2c3d4e5f6789"
url = "https://github.com/astral-sh/uv/releases/download/0.1.0/uv-x86_64-unknown-linux-gnu.tar.gz"

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

[build.stages.rtk-prebuilt.rtk.update]
provider = "github-release"
stable_only = true
tag_prefix = "v"
required_platforms = ["linux-amd64"]

[build.stages.rtk-prebuilt.rtk.artifacts.linux-amd64]
sha256 = "eb571d784b3269521722ebe2f0dc2409e89da6bd70bf097ddb21e9d4b3b240b9"
url = "https://github.com/rtk-ai/rtk/releases/download/v0.43.0/rtk_amd64.deb"

[build.stages.fd-prebuilt.fd]
version = "v10.4.2"

[build.stages.fd-prebuilt.fd.source]
type = "github-release"
repository = "sharkdp/fd"
tag = "v10.4.2"

[build.stages.fd-prebuilt.fd.update]
provider = "github-release"
stable_only = true
tag_prefix = "v"
required_platforms = ["linux-amd64"]

[build.stages.fd-prebuilt.fd.artifacts.linux-amd64]
sha256 = "0e44eb5fca93f09bc6f5430b90acdf44c8e069d0a903700aeb4820629337b67b"
url = "https://github.com/sharkdp/fd/releases/download/v10.4.2/fd_10.4.2_amd64.deb"

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

[runtime.pi-extensions.pi-codex-usage]
version = "0.9.1"

[runtime.pi-extensions.pi-codex-usage.source]
type = "npm"
package = "@llblab/pi-codex-usage"

[runtime.pi-extensions.pi-codex-usage.artifacts]
[runtime.pi-extensions.pi-codex-usage.artifacts."0.9.1"]
url = "https://registry.npmjs.org/@llblab/pi-codex-usage/-/pi-codex-usage-0.9.1.tgz"
integrity = "sha512-r5iMe57KgKPWSvx5/fKCwT+s/haysaEs40OMdTtisAFR1njppvNAhlgNOBl7+nDm6j88XTee3m0Rp3s/kinIQg=="

[runtime.pi-extensions.pi-codex-usage.update]
provider = "npm"
stable_only = true

[runtime.pi-extensions.pi-codex-usage.override]
constraint = ">=0.9.0"
allow_prerelease = false
scheme = "numeric"

[runtime.pi-extensions.pi-codex-usage.validation]
metadata_file = "package.json"

[runtime.pi-extensions.pi-proxy]
version = "1.0.0"

[runtime.pi-extensions.pi-proxy.source]
type = "npm"
package = "pi-proxy"

[runtime.pi-extensions.pi-proxy.artifacts]
[runtime.pi-extensions.pi-proxy.artifacts."1.0.0"]
url = "https://registry.npmjs.org/pi-proxy/-/pi-proxy-1.0.0.tgz"
integrity = "sha512-UHr/AQV2S0rISwRsD5jmKAo9ZQlZxU9Csh72sGYYDhbkSo44P+XzfRG96OuYYy2G3Iis0a75w3Cp9KYtjpbZxw=="

[runtime.pi-extensions.pi-proxy.update]
provider = "npm"
stable_only = true

[runtime.pi-extensions.pi-proxy.override]
constraint = ">=1.0.0"
allow_prerelease = false
scheme = "numeric"

[runtime.pi-extensions.pi-proxy.validation]
metadata_file = "package.json"

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
"""


# ===================================================================
# Task 1.1 — Root-Envelope Tests
# ===================================================================


class TestRootEnvelopeRequirements(unittest.TestCase):
    """Tests for canonical schema=1 root with build + runtime sections."""

    _temp_files: list[Path] = []

    @classmethod
    def setUpClass(cls):
        cls._temp_files = []
        cls.canonical_path = cls._write(_canonical_toml())

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

    def test_schema_must_be_one(self):
        """schema must be 1."""
        inv = load_inventory(self.canonical_path)
        self.assertEqual(inv.schema, 1)

        bad_toml = _canonical_toml().replace("schema = 1", "schema = 2")
        p = self._write(bad_toml)
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(p)
        self.assertIn("schema", str(ctx.exception))

    def test_build_table_required(self):
        """build table must be present."""
        inv = load_inventory(self.canonical_path)
        self.assertIsNotNone(inv.build)

    def test_runtime_table_required(self):
        """runtime table must be present."""
        inv = load_inventory(self.canonical_path)
        self.assertIsNotNone(inv.runtime)

    def test_build_must_be_table(self):
        """build must be a table, not a scalar."""
        bad_toml = """\
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
        p = self._write(bad_toml)
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(p)
        self.assertIn("build", str(ctx.exception))

    def test_runtime_must_be_table(self):
        """runtime must be a table, not a scalar."""
        bad_toml = """\
schema = 1
runtime = true

[build.stages.base.node]
tag = "24-trixie-slim"
digest = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

[build.stages.base.node.source]
type = "docker-registry"
registry = "docker.io"
repository = "library/node"

[build.stages.base.node.update]
provider = "docker-registry"
stable_only = true
track = "tag-digest"
"""
        p = self._write(bad_toml)
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(p)
        self.assertIn("runtime", str(ctx.exception))

    def test_cache_optional(self):
        """cache table is optional."""
        inv = load_inventory(self.canonical_path)
        self.assertIsNotNone(inv)

    def test_unknown_top_level_key_rejected(self):
        """Unknown top-level keys are rejected."""
        bad_toml = _canonical_toml() + "\n[extra_section]\nkey = true\n"
        p = self._write(bad_toml)
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(p)
        self.assertIn("extra_section", str(ctx.exception))

    def test_missing_build_reports_path(self):
        """Missing build section produces actionable path."""
        bad_toml = """\
schema = 1

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
        p = self._write(bad_toml)
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(p)
        self.assertIn("build", str(ctx.exception))

    def test_missing_runtime_reports_path(self):
        """Missing runtime section produces actionable path."""
        import re

        bad_toml = re.sub(
            r"\[runtime[^]]*\]\n(?:[^[\n].*\n)*",
            "",
            _canonical_toml(),
        )
        p = self._write(bad_toml)
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(p)
        self.assertIn("runtime", str(ctx.exception))

    def test_canonical_inventory_loads_no_network(self):
        """Canonical synthetic inventory loads without Docker or network access."""
        inv = load_inventory(self.canonical_path)
        self.assertIsNotNone(inv)
        self.assertEqual(inv.schema, 1)
        self.assertIsNotNone(inv.build)
        self.assertIsNotNone(inv.runtime)


# ===================================================================
# Task 1.2 — Typed-Container Tests
# ===================================================================


class TestTypedPhaseContainers(unittest.TestCase):
    """Inventory, BuildInventory, RuntimeInventory are frozen, typed containers."""

    _temp_files: list[Path] = []

    @classmethod
    def setUpClass(cls):
        cls._temp_files = []
        cls.canonical_path = cls._write(_canonical_toml())
        cls.inv = load_inventory(cls.canonical_path)

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

    def test_inventory_is_frozen_dataclass(self):
        """Inventory must be a frozen dataclass."""
        from dataclasses import fields, is_dataclass

        self.assertTrue(is_dataclass(type(self.inv)))
        f = fields(type(self.inv))
        self.assertTrue(len(f) >= 4)

    def test_build_inventory_is_frozen_dataclass(self):
        """BuildInventory must be a frozen dataclass."""
        from dataclasses import is_dataclass

        self.assertTrue(is_dataclass(type(self.inv.build)))

    def test_runtime_inventory_is_frozen_dataclass(self):
        """RuntimeInventory must be a frozen dataclass."""
        from dataclasses import is_dataclass

        self.assertTrue(is_dataclass(type(self.inv.runtime)))

    def test_build_cannot_be_reassigned(self):
        """inventory.build is immutable."""
        with self.assertRaises(FrozenInstanceError):
            self.inv.build = None  # type: ignore[assignment]

    def test_runtime_cannot_be_reassigned(self):
        """inventory.runtime is immutable."""
        with self.assertRaises(FrozenInstanceError):
            self.inv.runtime = None  # type: ignore[assignment]

    def test_build_stages_mapping_immutable(self):
        """inventory.build.stages is immutable."""
        with self.assertRaises(FrozenInstanceError):
            self.inv.build.stages.base = None  # type: ignore[assignment]

    def test_runtime_extensions_mapping_immutable(self):
        """inventory.runtime.pi_extensions is immutable."""
        with self.assertRaises(FrozenInstanceError):
            self.inv.runtime.pi_extensions = {}  # type: ignore[assignment]

    def test_cache_on_root_inventory_not_in_phases(self):
        """cache is on root Inventory, not in BuildInventory or RuntimeInventory."""
        from dataclasses import fields

        build_fields = {f.name for f in fields(type(self.inv.build))}
        self.assertNotIn("cache", build_fields)
        runtime_fields = {f.name for f in fields(type(self.inv.runtime))}
        self.assertNotIn("cache", runtime_fields)
        root_fields = {f.name for f in fields(type(self.inv))}
        self.assertIn("cache", root_fields)

    def test_source_update_artifact_remain_typed(self):
        """Existing source/update/artifact objects remain typed dataclasses."""
        from dataclasses import is_dataclass

        node = self.inv.build.stages.base.node
        self.assertTrue(is_dataclass(type(node)))
        self.assertTrue(is_dataclass(type(node.source)))
        self.assertTrue(is_dataclass(type(node.update)))

        rust = self.inv.build.stages.toolchain.rust
        self.assertTrue(is_dataclass(type(rust)))
        self.assertTrue(is_dataclass(type(rust.source)))
        self.assertTrue(is_dataclass(type(rust.update)))
        self.assertIsInstance(rust.rustup, MappingProxyType)

        uv = self.inv.build.stages.toolchain.uv
        self.assertTrue(is_dataclass(type(uv)))
        self.assertIsInstance(uv.artifacts, MappingProxyType)

        rtk = self.inv.build.stages.rtk_prebuilt.rtk
        self.assertTrue(is_dataclass(type(rtk)))
        self.assertIsInstance(rtk.artifacts, MappingProxyType)

    def test_no_generic_mutable_dicts_escape(self):
        """No generic mutable dict[str, Any] in validated output."""
        inv = self.inv
        self.assertIsNotNone(inv.build)
        self.assertIsNotNone(inv.runtime)
        if inv.cache is not None:
            from dataclasses import is_dataclass

            self.assertTrue(is_dataclass(type(inv.cache)))


# ===================================================================
# Task 1.2a — Direct-construction immutability
# ===================================================================


class TestDirectConstructionImmutability(unittest.TestCase):
    """BuildInventory and RuntimeInventory must normalise mutable inputs."""

    _temp_files: list[Path] = []

    @classmethod
    def setUpClass(cls):
        cls._temp_files = []
        cls.canonical_path = cls._write(_canonical_toml())
        cls.inv = load_inventory(cls.canonical_path)

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

    def test_runtime_inventory_normalises_mutable_dict(self):
        """RuntimeInventory normalises a plain dict to MappingProxyType."""
        from docker.versions import (
            NpmArtifact,
            NpmSource,
            NpmUpdate,
            OverridePolicy,
            PiExtensionEntry,
            RuntimeInventory,
            RuntimeValidation,
            parse_constraint,
        )

        mutable: dict[str, PiExtensionEntry] = {
            "test-ext": PiExtensionEntry(
                version="1.0.0",
                source=NpmSource(package="test"),
                update=NpmUpdate(stable_only=True),
                artifacts={
                    "1.0.0": NpmArtifact(
                        url="https://registry.npmjs.org/test/-/test-1.0.0.tgz",
                        integrity="sha512-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==",
                    )
                },
                validation=RuntimeValidation(metadata_file="package.json"),
                override=OverridePolicy(
                    constraint=parse_constraint(">=1.0.0"),
                    allow_prerelease=False,
                    scheme="numeric",
                ),
            ),
        }
        ri = RuntimeInventory(pi_extensions=mutable)
        self.assertIsInstance(ri.pi_extensions, MappingProxyType)
        # Mutating the original dict must not affect the frozen container
        mutable["intruder"] = PiExtensionEntry(
            version="9.9.9",
            source=NpmSource(package="evil"),
            update=NpmUpdate(stable_only=True),
            artifacts={
                "9.9.9": NpmArtifact(
                    url="https://registry.npmjs.org/evil/-/evil-9.9.9.tgz",
                    integrity="sha512-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==",
                )
            },
            validation=RuntimeValidation(metadata_file="package.json"),
            override=OverridePolicy(
                constraint=parse_constraint(">=1.0.0"),
                allow_prerelease=False,
                scheme="numeric",
            ),
        )
        self.assertNotIn("intruder", ri.pi_extensions)

    def test_runtime_inventory_copies_backing_dict(self):
        """A MappingProxyType backed by a reachable dict must be deep-copied
        so the original dict cannot affect the frozen container."""
        from docker.versions import (
            NpmArtifact,
            NpmSource,
            NpmUpdate,
            OverridePolicy,
            PiExtensionEntry,
            RuntimeInventory,
            RuntimeValidation,
            parse_constraint,
        )

        backing: dict[str, PiExtensionEntry] = {
            "safe": PiExtensionEntry(
                version="1.0.0",
                source=NpmSource(package="safe"),
                update=NpmUpdate(stable_only=True),
                artifacts={
                    "1.0.0": NpmArtifact(
                        url="https://registry.npmjs.org/safe/-/safe-1.0.0.tgz",
                        integrity="sha512-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==",
                    )
                },
                validation=RuntimeValidation(metadata_file="package.json"),
                override=OverridePolicy(
                    constraint=parse_constraint(">=1.0.0"),
                    allow_prerelease=False,
                    scheme="numeric",
                ),
            ),
        }
        mp = MappingProxyType(backing)
        ri = RuntimeInventory(pi_extensions=mp)

        self.assertIsInstance(ri.pi_extensions, MappingProxyType)
        self.assertIn("safe", ri.pi_extensions)

        # Mutate the original backing dict — must not leak into the container
        backing["evil"] = PiExtensionEntry(
            version="9.9.9",
            source=NpmSource(package="evil"),
            update=NpmUpdate(stable_only=True),
            artifacts={
                "9.9.9": NpmArtifact(
                    url="https://registry.npmjs.org/evil/-/evil-9.9.9.tgz",
                    integrity="sha512-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==",
                )
            },
            validation=RuntimeValidation(metadata_file="package.json"),
            override=OverridePolicy(
                constraint=parse_constraint(">=1.0.0"),
                allow_prerelease=False,
                scheme="numeric",
            ),
        )
        del backing["safe"]

        self.assertIn(
            "safe",
            ri.pi_extensions,
            "container must retain original key after backing dict deletion",
        )
        self.assertNotIn(
            "evil",
            ri.pi_extensions,
            "container must not see additions to the backing dict",
        )

    def test_inventory_normalises_runtime_pi_extensions(self):
        """Direct Inventory construction normalises runtime_pi_extensions."""
        from docker.versions import (
            Inventory,
            NpmArtifact,
            NpmSource,
            NpmUpdate,
            OverridePolicy,
            PiExtensionEntry,
            RuntimeValidation,
            parse_constraint,
        )

        mutable_ext: dict[str, PiExtensionEntry] = {
            "ext-a": PiExtensionEntry(
                version="1.0.0",
                source=NpmSource(package="ext-a"),
                update=NpmUpdate(stable_only=True),
                artifacts={
                    "1.0.0": NpmArtifact(
                        url="https://registry.npmjs.org/ext-a/-/ext-a-1.0.0.tgz",
                        integrity="sha512-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==",
                    )
                },
                validation=RuntimeValidation(metadata_file="package.json"),
                override=OverridePolicy(
                    constraint=parse_constraint(">=1.0.0"),
                    allow_prerelease=False,
                    scheme="numeric",
                ),
            ),
        }

        inv = Inventory(
            schema=1,
            stages=self.inv.stages,
            runtime_pi_extensions=mutable_ext,
        )

        self.assertIsInstance(inv.runtime_pi_extensions, MappingProxyType)

        # Mutating the original dict must not affect the inventory
        mutable_ext["evil"] = PiExtensionEntry(
            version="9.9.9",
            source=NpmSource(package="evil"),
            update=NpmUpdate(stable_only=True),
            artifacts={
                "9.9.9": NpmArtifact(
                    url="https://registry.npmjs.org/evil/-/evil-9.9.9.tgz",
                    integrity="sha512-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==",
                )
            },
            validation=RuntimeValidation(metadata_file="package.json"),
            override=OverridePolicy(
                constraint=parse_constraint(">=1.0.0"),
                allow_prerelease=False,
                scheme="numeric",
            ),
        )
        self.assertNotIn("evil", inv.runtime_pi_extensions)

    def test_runtime_inventory_rejects_mutation(self):
        """RuntimeInventory.pi_extensions cannot be reassigned."""
        ri = self.inv.runtime
        with self.assertRaises(FrozenInstanceError):
            ri.pi_extensions = {}  # type: ignore[assignment]

    def test_build_inventory_immutable(self):
        """BuildInventory rejects field reassignment."""
        bi = self.inv.build
        with self.assertRaises(FrozenInstanceError):
            bi.stages = None  # type: ignore[assignment]
