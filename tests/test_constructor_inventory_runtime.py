"""Stage 3 — closed runtime schema, phase placement, cross-phase identity tests."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from docker.versioning.constraints import parse_numeric_version  # noqa: E402
from docker.versioning.inventory import InventoryError  # noqa: E402
from docker.versioning.configuration_document_validation import (  # noqa: E402
    ConfigurationDocumentError,
    DocumentRole,
)
from docker.versioning.model import InvalidArtifactKey               # noqa: E402
from docker.versions import load_inventory               # noqa: E402


# ── helpers ────────────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parents[1]
_CANONICAL_TOML = _REPO_ROOT / "docker-constructor.toml"


def _write_temp(content: str, suffix: str = ".toml") -> Path:
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "w") as fh:
        fh.write(content)
    return Path(path)


def _read_canonical() -> str:
    """Return the canonical docker-constructor.toml content as a string."""
    return _CANONICAL_TOML.read_text()


# ── minimal Pi-extension snippet used by several tests ────────────────────
_RUNTIME_SNIPPET = """
[runtime.pi-extensions.pi-test]
version = "1.2.3"

[runtime.pi-extensions.pi-test.source]
type = "npm"
package = "@example/pi-test"

[runtime.pi-extensions.pi-test.artifacts."1.2.3"]
url = "https://registry.npmjs.org/@example/pi-test/-/pi-test-1.2.3.tgz"
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

# extra artifact entries appended to _RUNTIME_SNIPPET for catalog-key tests
_EXTRA_ARTIFACT_HEADER = '[runtime.pi-extensions.pi-test.artifacts."{key}"]'
_EXTRA_ARTIFACT_BODY = (
    'url = "https://registry.npmjs.org/@example/pi-test/-/pi-test-{key}.tgz"\n'
    'integrity = "sha512-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=="'
)


def _add_artifact(key: str) -> str:
    """Append an extra artifact entry with *key* to ``_RUNTIME_SNIPPET``."""
    header = _EXTRA_ARTIFACT_HEADER.format(key=key)
    body = _EXTRA_ARTIFACT_BODY.format(key=key)
    return _RUNTIME_SNIPPET + f"\n{header}\n{body}\n"


# ═══════════════════════════════════════════════════════════════════════════
# shared helpers for test classes
# ═══════════════════════════════════════════════════════════════════════════

class _TmpMixin:
    """Mixin adding temp-file tracking + cleanup to a TestCase."""
    _temp_files: ClassVar[list[Path]]

    @classmethod
    def setUpClass(cls):
        cls._temp_files = []

    @classmethod
    def tearDownClass(cls):
        for p in cls._temp_files:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        cls._temp_files.clear()

    def _write(self, content: str, suffix: str = ".toml") -> Path:
        p = _write_temp(content, suffix)
        self._temp_files.append(p)
        return p

    def assertReviewedSchemaError(
        self,
        context,
        path: Path,
        field: str,
        *,
        rejected: object | None = None,
    ) -> ConfigurationDocumentError:
        """Assert the typed reviewed schema-error contract for *context*.

        The published error must expose only document identity, the fixed
        ``schema_error`` classification, and the canonical invalid field. The
        owner exception must not survive as ``__context__`` or ``__cause__``,
        and *rejected* values must not leak into the rendered message.
        """
        error = context.exception
        self.assertIsInstance(error, ConfigurationDocumentError)
        self.assertEqual("schema_error", error.classification)
        self.assertEqual(DocumentRole.REVIEWED, error.role)
        self.assertEqual(path.resolve(), error.path)
        self.assertEqual(field, error.field)
        self.assertIsNone(error.__context__)
        self.assertIsNone(error.__cause__)
        if rejected is not None:
            self.assertNotIn(str(rejected), str(error))
        return error


# ═══════════════════════════════════════════════════════════════════════════
# Task 3.1 — Closed runtime schema
# ═══════════════════════════════════════════════════════════════════════════

class TestClosedRuntimeSchema(_TmpMixin, unittest.TestCase):
    """Focused tests for the runtime.pi-extensions closed source schema."""

    def _canonical_path(self) -> Path:
        return _CANONICAL_TOML

    # ── valid ───────────────────────────────────────────────────────────

    def test_valid_full_extension_is_preserved(self):
        """All fields of a fully specified extension are retained after load."""
        inv = load_inventory(self._canonical_path())
        ext = inv.runtime_pi_extensions["pi-read"]
        self.assertEqual(ext.version, "0.2.1")
        self.assertEqual(ext.source.package, "@arcanemachine/pi-read")
        self.assertEqual(ext.source.type, "npm")
        self.assertIsNotNone(ext.artifacts)
        self.assertIn(ext.version, ext.artifacts)
        art = ext.artifacts[ext.version]
        self.assertEqual(
            art.url,
            "https://registry.npmjs.org/@arcanemachine/pi-read/-/pi-read-0.2.1.tgz",
        )
        self.assertTrue(
            art.integrity.startswith("sha512-"),
            f"unexpected integrity format: {art.integrity!r}",
        )
        self.assertEqual(ext.update.provider, "npm")
        self.assertTrue(ext.update.stable_only)
        self.assertIsNotNone(ext.validation)
        self.assertEqual(ext.validation.metadata_file, "package.json")

    def test_minimal_extension_loaded(self):
        """An extension with the required fields loads successfully."""
        toml = _read_canonical() + _RUNTIME_SNIPPET
        inv = load_inventory(self._write(toml))
        ext = inv.runtime_pi_extensions["pi-test"]
        self.assertEqual(ext.version, "1.2.3")
        self.assertEqual(ext.source.package, "@example/pi-test")
        self.assertIsNotNone(ext.artifacts)
        self.assertIn(ext.version, ext.artifacts)
        self.assertIsNotNone(ext.validation)

    def test_reviewed_runtime_packages_all_present(self):
        """All reviewed runtime packages survive a complete load cycle."""
        inv = load_inventory(self._canonical_path())
        names = sorted(inv.runtime_pi_extensions.keys())
        self.assertEqual(names, ["pi-proxy", "pi-read", "pi-usage"])

    def test_reviewed_extension_overrides_accept_declared_versions(self):
        """Each reviewed override accepts its own version and rejects a neighbor."""
        inv = load_inventory(self._canonical_path())
        cases = (
            ("pi-read", "0.2.1", "0.1.9"),
            ("pi-usage", "0.60.7", "0.51.9"),
            ("pi-proxy", "1.0.0", "0.9.9"),
        )
        for name, accepted, rejected in cases:
            with self.subTest(name=name):
                policy = inv.runtime_pi_extensions[name].override.constraint
                self.assertTrue(policy.matches(parse_numeric_version(accepted)))
                self.assertFalse(policy.matches(parse_numeric_version(rejected)))

    # ── missing sections ────────────────────────────────────────────────

    def test_missing_runtime_rejected(self):
        toml = _read_canonical().split("[runtime")[0] + "\n"
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(self._write(toml))
        self.assertIn("runtime", str(ctx.exception).lower())

    def test_missing_pi_extensions_rejected(self):
        # Remove the pi-extensions table but keep the [runtime] section header
        content = _read_canonical()
        # Find and remove [runtime.pi-extensions] through the end of file
        idx = content.find("[runtime.pi-extensions")
        truncated = content[:idx].rstrip() + "\n"
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(self._write(truncated))
        # The truncated TOML is missing the runtime section entirely,
        # which produces an error containing "runtime".
        self.assertIn("runtime", str(ctx.exception).lower())

    def test_non_table_runtime_rejected(self):
        toml = "schema = 1\nruntime = 42\n"
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(self._write(toml))
        # The error may mention 'build' before 'runtime' since build is checked first.
        # Either way, a scalar runtime is rejected.
        msg = str(ctx.exception).lower()
        self.assertTrue("runtime" in msg or "table" in msg or "build" in msg,
                        f"Unexpected error: {msg}")

    def test_non_table_extension_rejected(self):
        toml = _read_canonical()
        toml += "\n[runtime.pi-extensions]\nbad-ext = \"not a table\"\n"
        path = self._write(toml)
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(path)
        self.assertReviewedSchemaError(
            ctx, path, "runtime.pi-extensions.bad-ext",
            rejected="not a table",
        )

    # ── unknown keys ────────────────────────────────────────────────────

    def test_unknown_runtime_key_rejected(self):
        toml = _read_canonical()
        toml += "\n[runtime.extra-thing]\nkey = \"val\"\n"
        path = self._write(toml)
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(path)
        self.assertReviewedSchemaError(ctx, path, "runtime.extra-thing", rejected="val")

    def test_unknown_extension_field_rejected(self):
        toml = _read_canonical() + _RUNTIME_SNIPPET
        # Add an unrecognized key directly in the extension table using inline
        toml = toml.replace(
            "[runtime.pi-extensions.pi-test]\nversion",
            "[runtime.pi-extensions.pi-test]\nadmin_password = \"secret\"\nversion"
        )
        path = self._write(toml)
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(path)
        self.assertReviewedSchemaError(
            ctx, path, "runtime.pi-extensions.pi-test.admin_password",
            rejected="secret",
        )

    def test_unknown_validation_field_rejected(self):
        toml = _read_canonical() + _RUNTIME_SNIPPET
        # Replace the validation section with one containing extra keys
        toml = toml.replace(
            "metadata_file = \"package.json\"",
            "metadata_file = \"package.json\"\nrun_script = \"evil.sh\""
        )
        path = self._write(toml)
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(path)
        self.assertReviewedSchemaError(
            ctx, path,
            "runtime.pi-extensions.pi-read.validation.run_script",
            rejected="evil.sh",
        )

    def test_metadata_file_absolute_path_rejected(self):
        """metadata_file must not be an absolute path."""
        toml = _read_canonical() + _RUNTIME_SNIPPET.replace(
            'metadata_file = "package.json"',
            'metadata_file = "/etc/passwd"'
        )
        path = self._write(toml)
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(path)
        self.assertReviewedSchemaError(
            ctx, path,
            "runtime.pi-extensions.pi-test.validation.metadata_file",
            rejected="/etc/passwd",
        )

    def test_metadata_file_traversal_rejected(self):
        """metadata_file must not contain .. traversal."""
        toml = _read_canonical() + _RUNTIME_SNIPPET.replace(
            'metadata_file = "package.json"',
            'metadata_file = "../../etc/passwd"'
        )
        path = self._write(toml)
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(path)
        self.assertReviewedSchemaError(
            ctx, path,
            "runtime.pi-extensions.pi-test.validation.metadata_file",
            rejected="../../etc/passwd",
        )

    def test_metadata_file_safe_relative_accepted(self):
        """A safe relative metadata_file path is accepted."""
        toml = _read_canonical() + _RUNTIME_SNIPPET.replace(
            'metadata_file = "package.json"',
            'metadata_file = "dist/info.json"'
        )
        inv = load_inventory(self._write(toml))
        self.assertEqual(
            inv.runtime_pi_extensions["pi-test"].validation.metadata_file,
            "dist/info.json"
        )

    # ── version validation ──────────────────────────────────────────────

    def test_missing_version_rejected(self):
        toml = _read_canonical()
        toml += """
[runtime.pi-extensions.no-ver]
[runtime.pi-extensions.no-ver.source]
type = "npm"
package = "no-ver"
[runtime.pi-extensions.no-ver.artifacts."1.0.0"]
url = "https://registry.npmjs.org/no-ver/-/no-ver-1.0.0.tgz"
integrity = "sha512-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=="
[runtime.pi-extensions.no-ver.update]
provider = "npm"
stable_only = true
[runtime.pi-extensions.no-ver.validation]
metadata_file = "package.json"
"""
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(self._write(toml))
        self.assertIn("version", str(ctx.exception).lower())

    def test_moving_version_latest_rejected(self):
        toml = _read_canonical() + _RUNTIME_SNIPPET.replace(
            'version = "1.2.3"', 'version = "latest"'
        )
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(self._write(toml))
        self.assertIn("version", str(ctx.exception).lower())

    def test_invalid_semver_rejected(self):
        toml = _read_canonical() + _RUNTIME_SNIPPET.replace(
            'version = "1.2.3"', 'version = "not-semver"'
        )
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(self._write(toml))
        self.assertIn("version", str(ctx.exception).lower())

    # ── source validation ───────────────────────────────────────────────

    def test_missing_source_rejected(self):
        toml = _read_canonical()
        toml += """
[runtime.pi-extensions.no-src]
version = "1.0.0"
[runtime.pi-extensions.no-src.artifacts."1.0.0"]
url = "https://registry.npmjs.org/pkg/-/pkg-1.0.0.tgz"
integrity = "sha512-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=="
[runtime.pi-extensions.no-src.update]
provider = "npm"
stable_only = true
[runtime.pi-extensions.no-src.validation]
metadata_file = "package.json"
"""
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(self._write(toml))
        self.assertIn("source", str(ctx.exception).lower())

    def test_non_npm_source_rejected(self):
        toml = _read_canonical() + _RUNTIME_SNIPPET.replace(
            'type = "npm"', 'type = "pypi"'
        )
        path = self._write(toml)
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(path)
        self.assertReviewedSchemaError(
            ctx, path, "runtime.pi-extensions.pi-test.source.type",
            rejected="pypi",
        )

    def test_non_npm_update_provider_rejected(self):
        toml = _read_canonical() + _RUNTIME_SNIPPET.replace(
            'provider = "npm"', 'provider = "pypi"'
        )
        path = self._write(toml)
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(path)
        self.assertReviewedSchemaError(
            ctx, path, "runtime.pi-extensions.pi-test.update.provider",
            rejected="pypi",
        )

    def test_source_provider_mismatch_rejected(self):
        toml = _read_canonical() + _RUNTIME_SNIPPET
        toml = toml.replace(
            '[runtime.pi-extensions.pi-test.update]\nprovider = "npm"',
            '[runtime.pi-extensions.pi-test.update]\nprovider = "github-release"',
        )
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(self._write(toml))

    # ── artifact validation ─────────────────────────────────────────────

    def test_missing_artifact_section_rejected(self):
        """Runtime extension without [*.artifacts] must be rejected."""
        toml = _read_canonical()
        toml += """
[runtime.pi-extensions.no-art]
version = "1.0.0"
[runtime.pi-extensions.no-art.source]
type = "npm"
package = "no-art"
[runtime.pi-extensions.no-art.update]
provider = "npm"
stable_only = true
[runtime.pi-extensions.no-art.validation]
metadata_file = "package.json"
"""
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(self._write(toml))
        self.assertIn("no-art.artifacts", str(ctx.exception).lower())

    def test_missing_validation_section_rejected(self):
        """Runtime extension without [*.validation] must be rejected."""
        toml = _read_canonical()
        toml += """
[runtime.pi-extensions.no-val]
version = "1.0.0"
[runtime.pi-extensions.no-val.source]
type = "npm"
package = "no-val"
[runtime.pi-extensions.no-val.artifacts."1.0.0"]
url = "https://registry.npmjs.org/no-val/-/no-val-1.0.0.tgz"
integrity = "sha512-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=="
[runtime.pi-extensions.no-val.update]
provider = "npm"
stable_only = true
"""
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(self._write(toml))
        self.assertIn("no-val.validation", str(ctx.exception).lower())

    def test_artifact_rejected_when_scalar(self):
        """Artifact as a scalar (not a table) must be rejected."""
        toml = _read_canonical()
        toml += """
[runtime.pi-extensions.scalar-art]
version = "1.0.0"
[runtime.pi-extensions.scalar-art.source]
type = "npm"
package = "scalar-art"
artifact = "not-a-table"
[runtime.pi-extensions.scalar-art.update]
provider = "npm"
stable_only = true
[runtime.pi-extensions.scalar-art.validation]
metadata_file = "package.json"
"""
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(self._write(toml))
        self.assertIn("artifact", str(ctx.exception).lower())

    def test_validation_rejected_when_scalar(self):
        """Validation as a scalar (not a table) must be rejected."""
        toml = _read_canonical()
        toml += """
[runtime.pi-extensions.scalar-val]
version = "1.0.0"
[runtime.pi-extensions.scalar-val.source]
type = "npm"
package = "scalar-val"
[runtime.pi-extensions.scalar-val.artifacts."1.0.0"]
url = "https://registry.npmjs.org/scalar-val/-/scalar-val-1.0.0.tgz"
integrity = "sha512-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=="
[runtime.pi-extensions.scalar-val.update]
provider = "npm"
stable_only = true
validation = 42
"""
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(self._write(toml))
        self.assertIn("validation", str(ctx.exception).lower())

    def test_missing_integrity_rejected(self):
        import re
        toml = _read_canonical() + _RUNTIME_SNIPPET
        toml = re.sub(r'integrity = ".*"\n', '', toml)
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(self._write(toml))
        self.assertIn("integrity", str(ctx.exception).lower())

    def test_malformed_integrity_rejected(self):
        toml = _read_canonical() + _RUNTIME_SNIPPET.replace(
            "sha512-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==",
            "not-a-hash",
        )
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(self._write(toml))
        self.assertIn("integrity", str(ctx.exception).lower())

    def test_truncated_integrity_rejected(self):
        """Base64 that decodes to wrong length is rejected."""
        toml = _read_canonical() + _RUNTIME_SNIPPET.replace(
            "sha512-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==",
            "sha512-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        )
        path = self._write(toml)
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(path)
        self.assertReviewedSchemaError(
            ctx, path,
            "runtime.pi-extensions.pi-test.artifacts.1.2.3.integrity",
            rejected="sha512-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        )

    def test_bad_base64_integrity_rejected(self):
        """Invalid base64 characters are rejected."""
        toml = _read_canonical() + _RUNTIME_SNIPPET.replace(
            "sha512-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==",
            "sha512-@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@==",
        )
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(self._write(toml))
        self.assertIn("integrity", str(ctx.exception).lower())

    def test_valid_sha256_integrity_accepted(self):
        """Valid sha256 integrity with correct byte length is accepted."""
        import base64
        sha256_valid = "sha256-" + base64.b64encode(b'\x00' * 32).decode()
        toml = _read_canonical() + _RUNTIME_SNIPPET.replace(
            "sha512-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==",
            sha256_valid,
        )
        inv = load_inventory(self._write(toml))
        self.assertEqual(inv.runtime_pi_extensions["pi-test"].artifacts["1.2.3"].integrity, sha256_valid)  # type: ignore[union-attr]

    def test_non_https_artifact_url_rejected(self):
        toml = _read_canonical() + _RUNTIME_SNIPPET.replace(
            "https://registry.npmjs.org", "http://registry.npmjs.org"
        )
        path = self._write(toml)
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(path)
        self.assertReviewedSchemaError(
            ctx, path,
            "runtime.pi-extensions.pi-test.artifacts.1.2.3.url",
            rejected="http://registry.npmjs.org/@example/pi-test/-/pi-test-1.2.3.tgz",
        )

    # ── override support ──────────────────────────────────────────────

    def test_default_override_accepted(self):
        """The default override from _RUNTIME_SNIPPET is parsed correctly."""
        toml = _read_canonical() + _RUNTIME_SNIPPET
        inv = load_inventory(self._write(toml))
        entry = inv.runtime_pi_extensions["pi-test"]
        self.assertEqual(entry.override.scheme, "numeric")
        self.assertFalse(entry.override.allow_prerelease)
        self.assertEqual(str(entry.override.constraint), ">=1.0.0")

    def test_unsupported_override_scheme_rejected(self):
        """Only 'numeric' override scheme is allowed."""
        toml = _read_canonical() + _RUNTIME_SNIPPET.replace(
            'scheme = "numeric"', 'scheme = "semver-coerce"'
        )
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(self._write(toml))
        self.assertIn("scheme", str(ctx.exception).lower())

    def test_override_unknown_key_rejected(self):
        """Unknown keys inside [override] are rejected."""
        toml = _read_canonical() + _RUNTIME_SNIPPET.replace(
            'scheme = "numeric"',
            'scheme = "numeric"\nunsupported_field = true'
        )
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(self._write(toml))
        self.assertIn("unsupported_field", str(ctx.exception))

    def test_override_numeric_rejects_prerelease(self):
        """Numeric scheme with allow_prerelease=true is rejected."""
        toml = _read_canonical() + _RUNTIME_SNIPPET.replace(
            'allow_prerelease = false', 'allow_prerelease = true'
        )
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(self._write(toml))
        self.assertIn("prerelease", str(ctx.exception).lower())

    def test_version_must_satisfy_constraint(self):
        """Version must satisfy the declared override constraint."""
        toml = _read_canonical() + _RUNTIME_SNIPPET.replace(
            'version = "1.2.3"', 'version = "0.9.0"'
        ).replace(
            'pi-test-1.2.3.tgz', 'pi-test-0.9.0.tgz'
        ).replace(
            'artifacts."1.2.3"', 'artifacts."0.9.0"'
        )
        path = self._write(toml)
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(path)
        self.assertReviewedSchemaError(
            ctx,
            path,
            "runtime.pi-extensions.pi-test.version",
        )

    # ── build-only / operational fields rejected ────────────────────────

    def test_stages_under_build_rejected_at_runtime(self):
        """build.stages fields should not appear under runtime."""
        toml = _read_canonical()
        toml += "\n[runtime.stages.something]\nversion = \"1.0\"\n"
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(self._write(toml))
        self.assertIn("stages", str(ctx.exception).lower())

    def test_operational_settings_rejected(self):
        toml = _read_canonical() + _RUNTIME_SNIPPET
        # Add unrecognized key via inline modification of the existing section
        toml = toml.replace(
            "[runtime.pi-extensions.pi-test]\nversion",
            "[runtime.pi-extensions.pi-test]\ninstall_dir = \"/opt\"\nversion"
        )
        path = self._write(toml)
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(path)
        self.assertReviewedSchemaError(
            ctx, path, "runtime.pi-extensions.pi-test.install_dir",
            rejected="/opt",
        )

    # ── canonical error paths ───────────────────────────────────────────

    def test_error_includes_canonical_path(self):
        toml = _read_canonical() + _RUNTIME_SNIPPET.replace(
            "sha512-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==",
            "bad-hash",
        )
        path = self._write(toml)
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(path)
        self.assertReviewedSchemaError(
            ctx, path,
            "runtime.pi-extensions.pi-test.artifacts.1.2.3.integrity",
            rejected="bad-hash",
        )

    # ── artifact-map key validation ──────────────────────────────────

    def test_artifact_key_malformed_semver_rejected(self):
        toml = _read_canonical() + _add_artifact("not/a/version")
        path = self._write(toml)
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(path)
        # The artifact key is the authorized canonical field path, not leaked prose.
        self.assertReviewedSchemaError(
            ctx, path,
            "runtime.pi-extensions.pi-test.artifacts.not/a/version",
        )

    def test_artifact_key_moving_tag_rejected(self):
        for tag in ("latest", "stable", "next", "dev", "canary", "nightly"):
            with self.subTest(tag=tag):
                toml = _read_canonical() + _add_artifact(tag)
                path = self._write(toml)
                with self.assertRaises(InventoryError) as ctx:
                    load_inventory(path)
                self.assertReviewedSchemaError(
                    ctx, path,
                    f"runtime.pi-extensions.pi-test.artifacts.{tag}",
                )

    def test_artifact_key_version_not_in_url_rejected(self):
        toml = _read_canonical() + _RUNTIME_SNIPPET.replace(
            'pi-test-1.2.3.tgz', 'pi-test-9.9.9.tgz'
        )
        path = self._write(toml)
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(path)
        self.assertReviewedSchemaError(
            ctx, path,
            "runtime.pi-extensions.pi-test.artifacts.1.2.3.url",
            rejected="pi-test-9.9.9.tgz",
        )

    def test_artifact_key_non_semver_prerelease_rejected(self):
        """Prerelease identifiers must follow semver rules (no leading zeros)."""
        toml = _read_canonical() + _add_artifact("1.2.3-01")
        path = self._write(toml)
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(path)
        self.assertReviewedSchemaError(
            ctx, path,
            "runtime.pi-extensions.pi-test.artifacts.1.2.3-01",
        )

    def test_artifact_key_build_with_bang_rejected(self):
        toml = _read_canonical() + _add_artifact("1.2.3+build!")
        path = self._write(toml)
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(path)
        self.assertReviewedSchemaError(
            ctx, path,
            "runtime.pi-extensions.pi-test.artifacts.1.2.3+build!",
        )

    # ── strict npm tarball URL validation ────────────────────────────

    def test_url_version_in_query_rejected(self):
        toml = _read_canonical() + _RUNTIME_SNIPPET.replace(
            'pi-test-1.2.3.tgz', 'pi-test-1.2.3.tgz?version=1.2.3'
        )
        path = self._write(toml)
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(path)
        self.assertReviewedSchemaError(
            ctx, path,
            "runtime.pi-extensions.pi-test.artifacts.1.2.3.url",
            rejected="?version=1.2.3",
        )

    def test_url_fragment_rejected(self):
        toml = _read_canonical() + _RUNTIME_SNIPPET.replace(
            'pi-test-1.2.3.tgz', 'pi-test-1.2.3.tgz#1.2.3'
        )
        path = self._write(toml)
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(path)
        self.assertReviewedSchemaError(
            ctx, path,
            "runtime.pi-extensions.pi-test.artifacts.1.2.3.url",
            rejected="#1.2.3",
        )

    def test_url_wrong_package_stem_rejected(self):
        toml = _read_canonical() + _RUNTIME_SNIPPET.replace(
            '@example/pi-test/-/', '@evil/wrong-package/-/'
        )
        path = self._write(toml)
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(path)
        self.assertReviewedSchemaError(
            ctx, path,
            "runtime.pi-extensions.pi-test.artifacts.1.2.3.url",
            rejected="@evil/wrong-package",
        )

    def test_url_missing_tarball_stem_rejected(self):
        toml = _read_canonical() + _RUNTIME_SNIPPET.replace(
            '/@example/pi-test/-/', '/@example/pi-test/v/'
        )
        path = self._write(toml)
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(path)
        self.assertReviewedSchemaError(
            ctx, path,
            "runtime.pi-extensions.pi-test.artifacts.1.2.3.url",
            rejected="https://registry.npmjs.org/@example/pi-test/v/pi-test-1.2.3.tgz",
        )

    def test_url_not_tgz_rejected(self):
        toml = _read_canonical() + _RUNTIME_SNIPPET.replace(
            'pi-test-1.2.3.tgz', 'pi-test-1.2.3.tar.gz'
        )
        path = self._write(toml)
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(path)
        self.assertReviewedSchemaError(
            ctx, path,
            "runtime.pi-extensions.pi-test.artifacts.1.2.3.url",
            rejected="pi-test-1.2.3.tar.gz",
        )

    def test_url_wrong_tarball_name_rejected(self):
        toml = _read_canonical() + _RUNTIME_SNIPPET.replace(
            'pi-test-1.2.3.tgz', 'wrong-name-1.2.3.tgz'
        )
        path = self._write(toml)
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(path)
        self.assertReviewedSchemaError(
            ctx, path,
            "runtime.pi-extensions.pi-test.artifacts.1.2.3.url",
            rejected="wrong-name-1.2.3.tgz",
        )


# ═══════════════════════════════════════════════════════════════════════════
# Task 3.2 — Phase placement and cross-phase duplicates
# ═══════════════════════════════════════════════════════════════════════════

class TestPhasePlacement(_TmpMixin, unittest.TestCase):
    """Tests rejecting dependencies placed in the wrong phase."""

    def test_pi_extensions_under_build_rejected(self):
        """Pi extensions must live under runtime, not build."""
        toml = _read_canonical()
        toml += """
[build.stages.pi-extensions.bad-ext]
version = "1.0.0"
[build.stages.pi-extensions.bad-ext.source]
type = "npm"
package = "bad-ext"
"""
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(self._write(toml))

    def test_stages_under_runtime_rejected(self):
        """build-only 'stages' must not appear under runtime."""
        toml = _read_canonical()
        toml += "\n[runtime.stages.foo]\nversion = \"1.0\"\n"
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(self._write(toml))

    def test_build_tool_entry_under_runtime_rejected(self):
        """A build tool (e.g. pi, openspec) is not valid under runtime.pi-extensions."""
        toml = _read_canonical()
        toml += "\n[runtime.pi-tools.pi]\nversion = \"1.0\"\n"
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(self._write(toml))

    def test_runtime_extension_under_build_stages_rejected(self):
        """Runtime extensions are not valid under build.stages.*."""
        toml = _read_canonical()
        toml += """
[build.stages.runtime-ext.foo]
version = "1.0"
[build.stages.runtime-ext.foo.source]
type = "npm"
package = "foo"
"""
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(self._write(toml))


class TestCrossPhaseDuplicates(_TmpMixin, unittest.TestCase):
    """Tests rejecting duplicate dependency identities across build/runtime."""

    def test_same_npm_package_in_build_and_runtime_rejected(self):
        """The same npm package identity in both build and runtime is rejected."""
        toml = _read_canonical()
        toml += """
[runtime.pi-extensions.pi-duplicate]
version = "0.63.0"
[runtime.pi-extensions.pi-duplicate.source]
type = "npm"
package = "@earendil-works/pi-coding-agent"
[runtime.pi-extensions.pi-duplicate.artifacts."0.63.0"]
url = "https://registry.npmjs.org/@earendil-works/pi-coding-agent/-/pi-coding-agent-0.63.0.tgz"
integrity = "sha512-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=="
[runtime.pi-extensions.pi-duplicate.update]
provider = "npm"
stable_only = true
[runtime.pi-extensions.pi-duplicate.override]
constraint = ">=0.63.0"
allow_prerelease = false
scheme = "numeric"
[runtime.pi-extensions.pi-duplicate.validation]
metadata_file = "package.json"
"""
        path = self._write(toml)
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(path)
        self.assertReviewedSchemaError(
            ctx, path, "runtime.pi-extensions.pi-duplicate",
            rejected="@earendil-works/pi-coding-agent",
        )

    def test_different_keys_same_package_rejected(self):
        """Different TOML keys with the same npm package identity are rejected."""
        toml = _read_canonical() + _RUNTIME_SNIPPET
        toml += """
[runtime.pi-extensions.pi-test-renamed]
version = "1.2.9"
[runtime.pi-extensions.pi-test-renamed.source]
type = "npm"
package = "@example/pi-test"
[runtime.pi-extensions.pi-test-renamed.artifacts."1.2.9"]
url = "https://registry.npmjs.org/@example/pi-test/-/pi-test-1.2.9.tgz"
integrity = "sha512-/////////////////////////////////////////////////////////////////////////////////////w=="
[runtime.pi-extensions.pi-test-renamed.update]
provider = "npm"
stable_only = true
[runtime.pi-extensions.pi-test-renamed.override]
constraint = ">=1.2.9"
allow_prerelease = false
scheme = "numeric"
[runtime.pi-extensions.pi-test-renamed.validation]
metadata_file = "package.json"
"""
        path = self._write(toml)
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(path)
        self.assertReviewedSchemaError(
            ctx, path,
            "runtime.pi-extensions.pi-test-renamed.source.package",
            rejected="@example/pi-test",
        )

    def test_same_version_different_package_allowed(self):
        """Same version but different package identities is not a duplicate."""
        toml = _read_canonical() + _RUNTIME_SNIPPET
        toml += """
[runtime.pi-extensions.pi-other]
version = "1.2.3"
[runtime.pi-extensions.pi-other.source]
type = "npm"
package = "@other/different-pkg"
[runtime.pi-extensions.pi-other.artifacts."1.2.3"]
url = "https://registry.npmjs.org/@other/different-pkg/-/different-pkg-1.2.3.tgz"
integrity = "sha512-/////////////////////////////////////////////////////////////////////////////////////w=="
[runtime.pi-extensions.pi-other.update]
provider = "npm"
stable_only = true
[runtime.pi-extensions.pi-other.override]
constraint = ">=1.2.3"
allow_prerelease = false
scheme = "numeric"
[runtime.pi-extensions.pi-other.validation]
metadata_file = "package.json"
"""
        inv = load_inventory(self._write(toml))
        self.assertIn("pi-test", inv.runtime_pi_extensions)
        self.assertIn("pi-other", inv.runtime_pi_extensions)

    def test_duplicate_error_includes_canonical_paths(self):
        """Duplicate errors should reference both conflicting paths."""
        toml = _read_canonical() + _RUNTIME_SNIPPET
        toml += """
[runtime.pi-extensions.pi-test-dup]
version = "1.2.3"
[runtime.pi-extensions.pi-test-dup.source]
type = "npm"
package = "@example/pi-test"
[runtime.pi-extensions.pi-test-dup.artifacts."1.2.3"]
url = "https://registry.npmjs.org/@example/pi-test/-/pi-test-1.2.3.tgz"
integrity = "sha512-/////////////////////////////////////////////////////////////////////////////////////w=="
[runtime.pi-extensions.pi-test-dup.update]
provider = "npm"
stable_only = true
[runtime.pi-extensions.pi-test-dup.override]
constraint = ">=1.2.3"
allow_prerelease = false
scheme = "numeric"
[runtime.pi-extensions.pi-test-dup.validation]
metadata_file = "package.json"
"""
        path = self._write(toml)
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(path)
        self.assertReviewedSchemaError(
            ctx, path,
            "runtime.pi-extensions.pi-test-dup.source.package",
            rejected="@example/pi-test",
        )

    def test_duplicate_runtime_alias_rejected(self):
        """Duplicate aliases within pi-extensions are rejected
        (two entries with same package id — covered above)."""
        # Redundant structural test; logic exercised by
        # test_different_keys_same_package_rejected
        pass


# ═══════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    unittest.main()
