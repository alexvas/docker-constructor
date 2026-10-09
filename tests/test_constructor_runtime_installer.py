"""RED — Protected Runtime Extension Installer (Stage 10.1–10.2).

Trust boundary (corrected):
  - ArtifactDownloader.fetch(url, dest_dir) → unverified local path
  - Integrity verification is installer-owned (over file bytes)
  - PackageInstaller.install(package, artifact_path) → from verified file

10.1: DTO validation, closed-schema projection rejection, exact
      artifact resolution, installer-owned integrity verification,
      interrupted installation, actionable failure diagnostics.

10.2: Idempotent installation, post-install identity/version
      validation, mount-prefix guard, dry-run, ownership, and
      source-contract tests.

All tests use injectable fakes — no Docker daemon, network,
subprocess, systemd, filesystem writes, or interactive stdin.
"""

from __future__ import annotations

import base64
import errno
import hashlib
import inspect
import json
import os
import shutil
import stat
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

import docker

from docker.runtime_installer import (
    ExtensionResult,
    InstallContext,
    InstallError,
    InstallResult,
    InstallStatus,
    IntegrityError,
    MetadataNotFoundError,
    MetadataReader,
    MetadataValidationError,
    MountChecker,
    MountedBlobReader,
    PackageInstaller,
    PrivilegeContext,
    ProjectionEntry,
    ProjectionError,
    RuntimeArtifactReader,
    _mounted_artifact_path,
    _MOUNTED_ARTIFACT_ROOT,
    _MountInspection,
    _StatvfsMountInspection,
    exit_code_for,
    install_extensions,
    read_projection,
)


# ═══════════════════════════════════════════════════════════════════════
# 10.1 — immutable DTOs
# ═══════════════════════════════════════════════════════════════════════


class TestProjectionEntryFieldValidation(unittest.TestCase):
    """Full trust contract for each field of ProjectionEntry is
    enforced at construction time (``__post_init__``), covering both
    ``read_projection`` and manually-supplied DTO inputs."""

    # ── helpers ──────────────────────────────────────────────────

    @staticmethod
    def _ok(**overrides: str) -> None:
        kwargs = {
            "package": "p", "version": "1.0.0",
            "artifact_id": _VALID_ARTIFACT_ID,
            "artifact_integrity": _VALID_SHA256,
            "metadata_file": "package.json",
        }
        kwargs.update(overrides)
        ProjectionEntry(**kwargs)  # type: ignore[arg-type]

    @staticmethod
    def _fail(**overrides: str) -> None:
        with unittest.TestCase().assertRaises(
            (ProjectionError, MetadataValidationError),
        ):
            TestProjectionEntryFieldValidation._ok(**overrides)

    # ── version ──────────────────────────────────────────────────

    def test_version_must_not_be_empty(self) -> None:
        self._fail(version="")

    def test_version_must_not_be_whitespace(self) -> None:
        self._fail(version="   ")

    def test_version_must_not_contain_slash(self) -> None:
        self._fail(version="1/2")

    def test_version_must_not_contain_backslash(self) -> None:
        self._fail(version="1\\2")

    def test_valid_version_accepted(self) -> None:
        self._ok(version="1.2.3-beta.1",
                 artifact_id=_VALID_ARTIFACT_ID)

    def test_prerelease_version_accepted(self) -> None:
        self._ok(version="1.0.0-rc.2",
                 artifact_id=_VALID_ARTIFACT_ID)

    def test_build_metadata_version_accepted(self) -> None:
        self._ok(version="1.2.3+build.20250101",
                 artifact_id=_VALID_ARTIFACT_ID)

    def test_prerelease_with_build_accepted(self) -> None:
        self._ok(version="1.2.3-beta.1+exp.sha.5114f85",
                 artifact_id=_VALID_ARTIFACT_ID)

    def test_version_latest_rejected(self) -> None:
        self._fail(version="latest")

    def test_version_stable_rejected(self) -> None:
        self._fail(version="stable")

    def test_version_star_range_rejected(self) -> None:
        self._fail(version="*")

    def test_version_caret_range_rejected(self) -> None:
        self._fail(version="^1.0.0")

    def test_version_tilde_range_rejected(self) -> None:
        self._fail(version="~1.2.3")

    def test_version_gte_range_rejected(self) -> None:
        self._fail(version=">=1.0.0")

    def test_version_v_prefix_rejected(self) -> None:
        self._fail(version="v1.2.3")

    def test_version_partial_rejected(self) -> None:
        self._fail(version="1.2")

    def test_version_malformed_prerelease_rejected(self) -> None:
        self._fail(version="1.2.3-!!!")

    # ── artifact_id ─────────────────────────────────────────────

    def test_artifact_id_rejects_traversal(self) -> None:
        self._fail(artifact_id="../sha256/hash.tgz")

    def test_artifact_id_rejects_absolute(self) -> None:
        self._fail(artifact_id="/sha256/hash.tgz")

    def test_artifact_id_rejects_empty(self) -> None:
        self._fail(artifact_id="")

    def test_artifact_id_rejects_missing_algo(self) -> None:
        self._fail(artifact_id="hash.tgz")

    def test_artifact_id_rejects_no_slash(self) -> None:
        self._fail(artifact_id="just-a-hash.tgz")

    def test_valid_artifact_id_accepted(self) -> None:
        self._ok(artifact_id=_VALID_ARTIFACT_ID)

    # ── artifact_integrity ───────────────────────────────────────

    def test_integrity_must_have_algo_prefix(self) -> None:
        self._fail(artifact_integrity="AAAA")

    def test_integrity_unsupported_algo_rejected(self) -> None:
        self._fail(artifact_integrity="md5-AAAA")

    def test_integrity_invalid_base64_rejected(self) -> None:
        self._fail(artifact_integrity="sha256-!!!bad!!!")

    def test_integrity_wrong_length_rejected(self) -> None:
        # sha256 needs 32 bytes → 44 base64 chars; give 16 bytes
        import base64
        short = base64.b64encode(b"x" * 16).decode()
        self._fail(artifact_integrity=f"sha256-{short}")

    def test_integrity_sha256_accepted(self) -> None:
        self._ok(artifact_integrity=_VALID_SHA256)

    # ── metadata_file ────────────────────────────────────────────

    def test_metadata_file_must_not_be_absolute(self) -> None:
        self._fail(metadata_file="/etc/passwd")

    def test_metadata_file_must_not_contain_parent_segment(self) -> None:
        """``..`` as a standalone path segment is rejected."""
        self._fail(metadata_file="../etc/passwd")

    def test_metadata_file_must_not_contain_dot_segment(self) -> None:
        """``.`` as a standalone path segment is rejected."""
        self._fail(metadata_file="./package.json")

    def test_metadata_file_must_not_contain_backslash(self) -> None:
        self._fail(metadata_file="pkg\\passwd")

    def test_metadata_file_must_not_be_empty(self) -> None:
        self._fail(metadata_file="")

    def test_metadata_file_must_not_be_whitespace(self) -> None:
        self._fail(metadata_file="   ")

    def test_metadata_file_rejects_empty_segment(self) -> None:
        """``//`` produces an empty segment — rejected."""
        self._fail(metadata_file="pkg//passwd")

    def test_metadata_file_rejects_trailing_slash(self) -> None:
        self._fail(metadata_file="pkg/")

    def test_double_dot_in_metadata_name_not_traversal(self) -> None:
        """``a..b`` contains ``..`` inside a longer name — accepted."""
        self._ok(metadata_file="a..b.json")

    def test_metadata_file_normalized_escape_rejected(self) -> None:
        """``a/../../b`` normalizes above root even though no single
        segment is ``..`` at the start."""
        self._fail(metadata_file="a/b/../../c")

    def test_nested_metadata_file_accepted(self) -> None:
        self._ok(metadata_file="sub/deep/pkg.json")

    # ── package name ─────────────────────────────────────────────

    def test_package_rejects_absolute(self) -> None:
        self._fail(package="/bad",
                   artifact_id=_VALID_ARTIFACT_ID)

    def test_package_rejects_backslash(self) -> None:
        self._fail(package="bad\\name",
                   artifact_id=_VALID_ARTIFACT_ID)

    def test_package_rejects_parent_segment(self) -> None:
        self._fail(package="..")

    def test_double_dot_in_package_name_accepted(self) -> None:
        """``a..b`` as package name has no traversal — accepted."""
        self._ok(package="a..b",
                 artifact_id=_VALID_ARTIFACT_ID)


class TestProjectionEntryDto(unittest.TestCase):
    """ProjectionEntry is a frozen, field-complete value object."""

    def test_all_fields_present(self) -> None:
        e = ProjectionEntry(
            package="@scope/pkg",
            version="1.2.3",
            artifact_id=_VALID_ARTIFACT_ID,
            artifact_integrity=_VALID_SHA512,
            metadata_file="package.json",
        )
        self.assertEqual("@scope/pkg", e.package)
        self.assertEqual("1.2.3", e.version)
        self.assertIn("sha256/", e.artifact_id)
        self.assertIn(".tgz", e.artifact_id)
        self.assertIn("sha512-", e.artifact_integrity)
        self.assertEqual("package.json", e.metadata_file)

    def test_frozen(self) -> None:
        e = ProjectionEntry(
            package="p", version="1.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="package.json",
        )
        with self.assertRaises(Exception):
            e.package = "q"  # type: ignore[misc]

    def test_integrity_always_present(self) -> None:
        # Every entry must carry integrity so the installer can verify.
        e = ProjectionEntry(
            package="p", version="1.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA512,
            metadata_file="package.json",
        )
        self.assertTrue(len(e.artifact_integrity) > 0)


class TestInstallStatusEnum(unittest.TestCase):
    """InstallStatus covers all per-extension outcomes."""

    def test_four_states(self) -> None:
        self.assertEqual(4, len(InstallStatus))

    def test_values_are_distinct(self) -> None:
        values = list(InstallStatus)
        for i in range(len(values)):
            for j in range(i + 1, len(values)):
                self.assertNotEqual(values[i], values[j])


class TestExtensionResultDto(unittest.TestCase):
    """ExtensionResult is a frozen per-extension outcome."""

    def test_success_has_no_detail(self) -> None:
        r = ExtensionResult(package="p", version="1", status=InstallStatus.OK)
        self.assertIsNone(r.detail)

    def test_failure_carries_structured_detail(self) -> None:
        r = ExtensionResult(
            package="p", version="1",
            status=InstallStatus.FAILED,
            detail="integrity mismatch: expected deadbeef, got cafebabe",
        )
        self.assertIn("integrity", r.detail or "")

    def test_already_installed_has_no_detail(self) -> None:
        r = ExtensionResult(
            package="p", version="1",
            status=InstallStatus.ALREADY_INSTALLED,
        )
        self.assertIsNone(r.detail)

    def test_frozen(self) -> None:
        r = ExtensionResult(package="p", version="1", status=InstallStatus.OK)
        with self.assertRaises(Exception):
            r.status = InstallStatus.FAILED  # type: ignore[misc]


class TestInstallResultDto(unittest.TestCase):
    """InstallResult aggregates per-extension outcomes."""

    def test_ok_when_all_succeed(self) -> None:
        result = InstallResult(results=(
            ExtensionResult(package="a", version="1", status=InstallStatus.OK),
            ExtensionResult(package="b", version="2", status=InstallStatus.OK),
        ))
        self.assertTrue(result.ok)

    def test_not_ok_when_any_fails(self) -> None:
        result = InstallResult(results=(
            ExtensionResult(package="a", version="1", status=InstallStatus.OK),
            ExtensionResult(package="b", version="2",
                            status=InstallStatus.FAILED, detail="boom"),
        ))
        self.assertFalse(result.ok)

    def test_dry_run_flag_preserved(self) -> None:
        result = InstallResult(results=(), dry_run=True)
        self.assertTrue(result.dry_run)
        self.assertTrue(result.ok)


class TestInstallErrorHierarchy(unittest.TestCase):
    """Domain errors carry structured detail for actionable diagnostics."""

    def test_integrity_error_extends_install_error(self) -> None:
        self.assertTrue(issubclass(IntegrityError, InstallError))

    def test_integrity_error_carries_algorithm_expected_actual(self) -> None:
        exc = IntegrityError(
            "checksum mismatch",
            algorithm="sha256",
            expected="deadbeef",
            actual="cafebabe",
        )
        self.assertEqual("sha256", exc.algorithm)
        self.assertEqual("deadbeef", exc.expected)
        self.assertEqual("cafebabe", exc.actual)

    def test_projection_error_is_install_error(self) -> None:
        self.assertTrue(issubclass(ProjectionError, InstallError))

    def test_metadata_validation_error_is_install_error(self) -> None:
        self.assertTrue(issubclass(MetadataValidationError, InstallError))

    def test_metadata_not_found_error_is_install_error(self) -> None:
        self.assertTrue(issubclass(MetadataNotFoundError, InstallError))


# ═══════════════════════════════════════════════════════════════════════
# 10.1 — read_projection: valid TOML
# ═══════════════════════════════════════════════════════════════════════


class TestReadProjectionValid(unittest.TestCase):
    """read_projection parses the Stage 5 effective runtime TOML."""

    _VALID = """\
[extensions."pi-read"]
package = "pi-read"
version = "1.0.0"
artifact = { artifact_id = "sha256/ypeBEsobvcr6wjGzmiPcTaeG7_gUfE5yuYB3ha_uSLs=.tgz", integrity = "sha256-ypeBEsobvcr6wjGzmiPcTaeG7/gUfE5yuYB3ha/uSLs=" }
metadata_file = "package.json"

[extensions."pi-green-loop"]
package = "pi-green-loop"
version = "2.0.0"
artifact = { artifact_id = "sha256/ypeBEsobvcr6wjGzmiPcTaeG7_gUfE5yuYB3ha_uSLs=.tgz", integrity = "sha256-ypeBEsobvcr6wjGzmiPcTaeG7/gUfE5yuYB3ha/uSLs=" }
metadata_file = "package.json"
"""

    def test_parse_two_extensions(self) -> None:
        entries = read_projection(self._tmp(self._VALID))
        self.assertEqual(2, len(entries))

    def test_fields_fully_populated(self) -> None:
        entries = read_projection(self._tmp(self._VALID))
        for e in entries:
            self.assertTrue(e.package)
            self.assertTrue(e.version)
            self.assertIn("/", e.artifact_id)
            self.assertTrue(e.artifact_id.endswith(".tgz"))
            self.assertTrue(e.artifact_integrity.startswith("sha"))
            self.assertTrue(e.metadata_file)

    def test_parses_flat_host_generated_artifact_fields(self) -> None:
        """The installer must consume the flat schema emitted by
        ``create_runtime_projection`` rather than reject it before
        mounted-artifact validation can begin."""
        flat = """\
[extensions."pi-read"]
package = "pi-read"
version = "1.0.0"
artifact_id = "sha256/ypeBEsobvcr6wjGzmiPcTaeG7_gUfE5yuYB3ha_uSLs=.tgz"
integrity = "sha256-ypeBEsobvcr6wjGzmiPcTaeG7/gUfE5yuYB3ha/uSLs="
metadata_file = "package.json"
"""
        entries = read_projection(self._tmp(flat))
        self.assertEqual(1, len(entries))
        self.assertEqual("pi-read", entries[0].package)
        self.assertEqual(
            "sha256/ypeBEsobvcr6wjGzmiPcTaeG7_gUfE5yuYB3ha_uSLs=.tgz",
            entries[0].artifact_id,
        )

    def test_rejects_mixed_flat_and_legacy_artifact_fields(self) -> None:
        mixed = self._VALID.replace(
            'metadata_file = "package.json"',
            'artifact_id = "sha256/other.tgz"\n'
            'integrity = "sha256-ypeBEsobvcr6wjGzmiPcTaeG7/gUfE5yuYB3ha/uSLs="\n'
            'metadata_file = "package.json"',
            1,
        )
        with self.assertRaises(ProjectionError):
            read_projection(self._tmp(mixed))

    @staticmethod
    def _tmp(content: str) -> str:
        import tempfile
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".toml", delete=False,
        )
        try:
            tmp.write(content)
        finally:
            tmp.close()
        return tmp.name


# ═══════════════════════════════════════════════════════════════════════
# 10.1 — read_projection: deterministic ordering
# ═══════════════════════════════════════════════════════════════════════


class TestDeterministicOrdering(unittest.TestCase):
    """Extension entries MUST be returned in name-sorted order,
    not insertion/Toml-layout order."""

    _UNSORTED_TOML = """\
[extensions.z]
package = "z"
version = "1.0.0"
artifact = { artifact_id = "sha256/ypeBEsobvcr6wjGzmiPcTaeG7_gUfE5yuYB3ha_uSLs=.tgz", integrity = "sha256-ypeBEsobvcr6wjGzmiPcTaeG7/gUfE5yuYB3ha/uSLs=" }
metadata_file = "package.json"

[extensions.a]
package = "a"
version = "1.0.0"
artifact = { artifact_id = "sha256/ypeBEsobvcr6wjGzmiPcTaeG7_gUfE5yuYB3ha_uSLs=.tgz", integrity = "sha256-ypeBEsobvcr6wjGzmiPcTaeG7/gUfE5yuYB3ha/uSLs=" }
metadata_file = "package.json"

[extensions.m]
package = "m"
version = "1.0.0"
artifact = { artifact_id = "sha256/ypeBEsobvcr6wjGzmiPcTaeG7_gUfE5yuYB3ha_uSLs=.tgz", integrity = "sha256-ypeBEsobvcr6wjGzmiPcTaeG7/gUfE5yuYB3ha/uSLs=" }
metadata_file = "package.json"
"""

    def test_entries_sorted_by_name(self) -> None:
        path = _tmp_toml(self._UNSORTED_TOML)
        entries = read_projection(path)
        names = [e.package for e in entries]
        self.assertEqual(["a", "m", "z"], names)
        self.assertEqual(names, sorted(names))


# ═══════════════════════════════════════════════════════════════════════
# 10.1 — read_projection: closed-schema rejection
# ═══════════════════════════════════════════════════════════════════════


class TestClosedSchemaRejection(unittest.TestCase):
    """read_projection must reject fields that belong to the reviewed
    source or build projection (host-only / update / override metadata)."""

    _BASE = """\
[extensions.p]
package = "p"
version = "1.0.0"
artifact = { artifact_id = "sha256/ypeBEsobvcr6wjGzmiPcTaeG7_gUfE5yuYB3ha_uSLs=.tgz", integrity = "sha256-ypeBEsobvcr6wjGzmiPcTaeG7/gUfE5yuYB3ha/uSLs=" }
metadata_file = "package.json"
"""

    def test_rejects_unknown_root_field(self) -> None:
        path = _tmp_toml(self._BASE + 'build = "garbage"\n')
        with self.assertRaises(ProjectionError):
            read_projection(path)

    def test_rejects_unknown_extension_field(self) -> None:
        path = _tmp_toml("""\
[extensions.p]
package = "p"
version = "1.0.0"
artifact = { artifact_id = "sha256/ypeBEsobvcr6wjGzmiPcTaeG7_gUfE5yuYB3ha_uSLs=.tgz", integrity = "sha256-ypeBEsobvcr6wjGzmiPcTaeG7/gUfE5yuYB3ha/uSLs=" }
metadata_file = "package.json"
source = { package = "p", registry = "https://x" }
""")
        with self.assertRaises(ProjectionError):
            read_projection(path)

    def test_rejects_update_metadata(self) -> None:
        path = _tmp_toml("""\
[extensions.p]
package = "p"
version = "1.0.0"
artifact = { artifact_id = "sha256/ypeBEsobvcr6wjGzmiPcTaeG7_gUfE5yuYB3ha_uSLs=.tgz", integrity = "sha256-ypeBEsobvcr6wjGzmiPcTaeG7/gUfE5yuYB3ha/uSLs=" }
metadata_file = "package.json"
update = { provider = "npm", max_age_seconds = 3600 }
""")
        with self.assertRaises(ProjectionError):
            read_projection(path)

    def test_rejects_override_policy(self) -> None:
        path = _tmp_toml("""\
[extensions.p]
package = "p"
version = "1.0.0"
artifact = { artifact_id = "sha256/ypeBEsobvcr6wjGzmiPcTaeG7_gUfE5yuYB3ha_uSLs=.tgz", integrity = "sha256-ypeBEsobvcr6wjGzmiPcTaeG7/gUfE5yuYB3ha/uSLs=" }
metadata_file = "package.json"
override = { allow = false, message = "no" }
""")
        with self.assertRaises(ProjectionError):
            read_projection(path)

    def test_rejects_unselected_artifact_catalogs(self) -> None:
        # Only the selected artifact (matching version) is present;
        # other version entries must not appear.
        path = _tmp_toml("""\
[extensions.p]
package = "p"
version = "1.0.0"
artifact = { artifact_id = "sha256/ypeBEsobvcr6wjGzmiPcTaeG7_gUfE5yuYB3ha_uSLs=.tgz", integrity = "sha256-ypeBEsobvcr6wjGzmiPcTaeG7/gUfE5yuYB3ha/uSLs=" }
metadata_file = "package.json"
artifacts = { "1.0.0" = { artifact_id = "https://x", integrity = "sha512-A" }, "2.0.0" = { artifact_id = "https://x", integrity = "sha512-B" } }
""")
        with self.assertRaises(ProjectionError):
            read_projection(path)


# ═══════════════════════════════════════════════════════════════════════
# 10.1 — read_projection: required fields
# ═══════════════════════════════════════════════════════════════════════


class TestRequiredFields(unittest.TestCase):
    """Every projection field required by Stage 5 must be validated."""

    def test_empty_extensions_are_valid(self) -> None:
        path = _tmp_toml("[extensions]\n")
        self.assertEqual([], read_projection(path))

    def test_missing_package_identity(self) -> None:
        path = _tmp_toml("""\
[extensions.p]
version = "1.0.0"
artifact = { artifact_id = "sha256/ypeBEsobvcr6wjGzmiPcTaeG7_gUfE5yuYB3ha_uSLs=.tgz", integrity = "sha256-ypeBEsobvcr6wjGzmiPcTaeG7/gUfE5yuYB3ha/uSLs=" }
metadata_file = "package.json"
""")
        with self.assertRaises(ProjectionError):
            read_projection(path)

    def test_missing_version(self) -> None:
        path = _tmp_toml("""\
[extensions.p]
package = "p"
artifact = { artifact_id = "sha256/ypeBEsobvcr6wjGzmiPcTaeG7_gUfE5yuYB3ha_uSLs=.tgz", integrity = "sha256-ypeBEsobvcr6wjGzmiPcTaeG7/gUfE5yuYB3ha/uSLs=" }
metadata_file = "package.json"
""")
        with self.assertRaises(ProjectionError):
            read_projection(path)

    def test_missing_artifact_url(self) -> None:
        path = _tmp_toml("""\
[extensions.p]
package = "p"
version = "1.0.0"
artifact = { integrity = "sha256-ypeBEsobvcr6wjGzmiPcTaeG7/gUfE5yuYB3ha/uSLs=" }
metadata_file = "package.json"
""")
        with self.assertRaises(ProjectionError):
            read_projection(path)

    def test_missing_integrity(self) -> None:
        path = _tmp_toml("""\
[extensions.p]
package = "p"
version = "1.0.0"
artifact = { artifact_id = "sha256/ypeBEsobvcr6wjGzmiPcTaeG7_gUfE5yuYB3ha_uSLs=.tgz" }
metadata_file = "package.json"
""")
        with self.assertRaises(ProjectionError):
            read_projection(path)

    def test_missing_metadata_file(self) -> None:
        path = _tmp_toml("""\
[extensions.p]
package = "p"
version = "1.0.0"
artifact = { artifact_id = "sha256/ypeBEsobvcr6wjGzmiPcTaeG7_gUfE5yuYB3ha_uSLs=.tgz", integrity = "sha256-ypeBEsobvcr6wjGzmiPcTaeG7/gUfE5yuYB3ha/uSLs=" }
""")
        with self.assertRaises(ProjectionError):
            read_projection(path)


class TestReadProjectionWrongTypes(unittest.TestCase):
    """Each projected field must already be a TOML string;
    :func:`read_projection` rejects int, float, bool, etc."""

    _TOML_TEMPLATE = """\
[extensions.p]
package = "p"
version = "1.0.0"
artifact = {{ artifact_id = "sha256/ypeBEsobvcr6wjGzmiPcTaeG7_gUfE5yuYB3ha_uSLs=.tgz", integrity = "{_VALID_SHA256}" }}
metadata_file = "package.json"
"""

    def _make_toml(self, **overrides: object) -> str:
        """Produce TOML with fields overridden by raw TOML fragments."""
        lines = self._TOML_TEMPLATE.splitlines(keepends=True)
        result: list[str] = []
        for line in lines:
            stripped = line.strip()
            for key, frag in overrides.items():
                if stripped.startswith(key):
                    line = f"{key} = {frag}\n"
                    break
            result.append(line)
        return "".join(result)

    def test_package_must_be_string(self) -> None:
        path = _tmp_toml(self._make_toml(package="123"))
        with self.assertRaises(ProjectionError):
            read_projection(path)

    def test_version_must_be_string(self) -> None:
        path = _tmp_toml(self._make_toml(version="1.0"))
        with self.assertRaises(ProjectionError):
            read_projection(path)

    def test_url_must_be_string(self) -> None:
        # Replace the entire artifact inline table with a variant
        # where url is an unquoted int.
        toml = """\
[extensions.p]
package = "p"
version = "1.0.0"
artifact = { url = 42, integrity = "sha256-ypeBEsobvcr6wjGzmiPcTaeG7/gUfE5yuYB3ha/uSLs=" }
metadata_file = "package.json"
"""
        path = _tmp_toml(toml)
        with self.assertRaises(ProjectionError):
            read_projection(path)

    def test_integrity_must_be_string(self) -> None:
        toml = """\
[extensions.p]
package = "p"
version = "1.0.0"
artifact = { artifact_id = "sha256/ypeBEsobvcr6wjGzmiPcTaeG7_gUfE5yuYB3ha_uSLs=.tgz", integrity = 0 }
metadata_file = "package.json"
"""
        path = _tmp_toml(toml)
        with self.assertRaises(ProjectionError):
            read_projection(path)

    def test_metadata_file_must_be_string(self) -> None:
        path = _tmp_toml(self._make_toml(metadata_file="true"))
        with self.assertRaises(ProjectionError):
            read_projection(path)

    def test_raises_projection_error_not_type_error(self) -> None:
        """Wrong-type rejections are always ProjectionError, never TypeError."""
        toml = """\
[extensions.p]
package = "p"
version = "1.0.0"
artifact = { url = 1, integrity = "sha256-ypeBEsobvcr6wjGzmiPcTaeG7/gUfE5yuYB3ha/uSLs=" }
metadata_file = "package.json"
"""
        path = _tmp_toml(toml)
        with self.assertRaises(ProjectionError):
            read_projection(path)


class TestReadProjectionDuplicatePackage(unittest.TestCase):
    """Duplicate package identities in the projection must be rejected
    before any installation mutations occur."""

    @staticmethod
    def _dup_base() -> str:
        return f"""\
[extensions.a]
package = "dup"
version = "1.0.0"
artifact = {{ artifact_id = "sha256/ypeBEsobvcr6wjGzmiPcTaeG7_gUfE5yuYB3ha_uSLs=.tgz", integrity = "{_VALID_SHA256}" }}
metadata_file = "package.json"

[extensions.b]
package = "dup"
version = "2.0.0"
artifact = {{ artifact_id = "sha256/ypeBEsobvcr6wjGzmiPcTaeG7_gUfE5yuYB3ha_uSLs=.tgz", integrity = "{_VALID_SHA256}" }}
metadata_file = "package.json"
"""

    def test_duplicate_package_rejected(self) -> None:
        path = _tmp_toml(self._dup_base())
        with self.assertRaises(ProjectionError) as ctx:
            read_projection(path)
        self.assertIn("dup", str(ctx.exception))

    def test_unique_packages_accepted(self) -> None:
        toml = f"""\
[extensions.a]
package = "x"
version = "1.0.0"
artifact = {{ artifact_id = "sha256/ypeBEsobvcr6wjGzmiPcTaeG7_gUfE5yuYB3ha_uSLs=.tgz", integrity = "{_VALID_SHA256}" }}
metadata_file = "package.json"

[extensions.b]
package = "y"
version = "2.0.0"
artifact = {{ artifact_id = "sha256/ypeBEsobvcr6wjGzmiPcTaeG7_gUfE5yuYB3ha_uSLs=.tgz", integrity = "{_VALID_SHA256}" }}
metadata_file = "package.json"
"""
        path = _tmp_toml(toml)
        entries = read_projection(path)
        self.assertEqual(2, len(entries))
        self.assertEqual(["x", "y"], [e.package for e in entries])


# ═══════════════════════════════════════════════════════════════════════
# 10.1 — read_projection: unsafe values
# ═══════════════════════════════════════════════════════════════════════


class TestUnsafeMetadataPath(unittest.TestCase):
    """metadata_file must be a safe relative path (no traversal, no absolute)."""

    def test_traversal_rejected(self) -> None:
        path = _tmp_toml("""\
[extensions.p]
package = "p"
version = "1.0.0"
artifact = { artifact_id = "sha256/ypeBEsobvcr6wjGzmiPcTaeG7_gUfE5yuYB3ha_uSLs=.tgz", integrity = "sha256-ypeBEsobvcr6wjGzmiPcTaeG7/gUfE5yuYB3ha/uSLs=" }
metadata_file = "../etc/passwd"
""")
        with self.assertRaises(MetadataValidationError):
            read_projection(path)

    def test_absolute_path_rejected(self) -> None:
        path = _tmp_toml("""\
[extensions.p]
package = "p"
version = "1.0.0"
artifact = { artifact_id = "sha256/ypeBEsobvcr6wjGzmiPcTaeG7_gUfE5yuYB3ha_uSLs=.tgz", integrity = "sha256-ypeBEsobvcr6wjGzmiPcTaeG7/gUfE5yuYB3ha/uSLs=" }
metadata_file = "/etc/passwd"
""")
        with self.assertRaises(MetadataValidationError):
            read_projection(path)

    def test_relative_safe_accepted(self) -> None:
        path = _tmp_toml("""\
[extensions.p]
package = "p"
version = "1.0.0"
artifact = { artifact_id = "sha256/ypeBEsobvcr6wjGzmiPcTaeG7_gUfE5yuYB3ha_uSLs=.tgz", integrity = "sha256-ypeBEsobvcr6wjGzmiPcTaeG7/gUfE5yuYB3ha/uSLs=" }
metadata_file = "pkg/package.json"
""")
        entries = read_projection(path)
        self.assertEqual("pkg/package.json", entries[0].metadata_file)


class TestPackageNameSafety(unittest.TestCase):
    """Package names become path components under node_modules.
    Reject traversal, absolute paths, backslashes, malformed scoped
    names, and extra slash components.  Accept only valid unscoped
    names or exactly ``@scope/name``.

    Validation is checked at :class:`ProjectionEntry` construction
    time, so it covers both ``read_projection`` and manually
    constructed DTO inputs."""

    # ── helpers ──────────────────────────────────────────────────

    @staticmethod
    def _entry(package: str) -> ProjectionEntry:
        return ProjectionEntry(
            package=package, version="1.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="package.json",
        )

    @staticmethod
    def _toml_entry(package: str) -> str:
        return f"""\
[extensions.p]
package = "{package}"
version = "1.0.0"
artifact = {{ artifact_id = "sha256/ypeBEsobvcr6wjGzmiPcTaeG7_gUfE5yuYB3ha_uSLs=.tgz", integrity = "{_VALID_SHA256}" }}
metadata_file = "package.json"
"""

    def _assert_rejects_direct(self, package: str) -> None:
        with self.assertRaises(ProjectionError):
            self._entry(package)

    def _assert_rejects_toml(self, package: str) -> None:
        path = _tmp_toml(self._toml_entry(package))
        with self.assertRaises(ProjectionError):
            read_projection(path)

    def _assert_accepts_direct(self, package: str) -> None:
        self._entry(package)  # must not raise

    def _assert_accepts_toml(self, package: str) -> None:
        path = _tmp_toml(self._toml_entry(package))
        entries = read_projection(path)
        self.assertEqual(1, len(entries))

    # ── traversal (both paths) ───────────────────────────────────

    def test_rejects_dot_dot_direct(self) -> None:
        self._assert_rejects_direct("../etc/passwd")

    def test_rejects_dot_dot_toml(self) -> None:
        self._assert_rejects_toml("../etc/passwd")

    def test_rejects_encoded_traversal_direct(self) -> None:
        self._assert_rejects_direct("pkg/../../root")

    def test_rejects_encoded_traversal_toml(self) -> None:
        self._assert_rejects_toml("pkg/../../root")

    # ── absolute paths ───────────────────────────────────────────

    def test_rejects_absolute_path_direct(self) -> None:
        self._assert_rejects_direct("/etc/passwd")

    def test_rejects_absolute_path_toml(self) -> None:
        self._assert_rejects_toml("/etc/passwd")

    def test_rejects_relative_path_direct(self) -> None:
        self._assert_rejects_direct("./hidden")

    def test_rejects_relative_path_toml(self) -> None:
        self._assert_rejects_toml("./hidden")

    # ── backslashes ──────────────────────────────────────────────

    def test_rejects_backslash_direct(self) -> None:
        self._assert_rejects_direct("pkg\\..\\etc")

    def test_rejects_backslash_toml(self) -> None:
        self._assert_rejects_toml("pkg\\..\\etc")

    # ── malformed scoped names ───────────────────────────────────

    def test_rejects_at_only_direct(self) -> None:
        self._assert_rejects_direct("@")

    def test_rejects_scope_no_slash_direct(self) -> None:
        self._assert_rejects_direct("@scope")

    def test_rejects_scope_trailing_slash_direct(self) -> None:
        self._assert_rejects_direct("@scope/")

    def test_rejects_double_at_direct(self) -> None:
        self._assert_rejects_direct("@@scope/name")

    def test_rejects_empty_scope_direct(self) -> None:
        self._assert_rejects_direct("@/name")

    def test_rejects_empty_name_scoped_direct(self) -> None:
        self._assert_rejects_direct("@scope/")

    def test_rejects_double_slash_scoped_direct(self) -> None:
        self._assert_rejects_direct("@scope//name")

    def test_rejects_malformed_scoped_toml(self) -> None:
        self._assert_rejects_toml("@scope//evil")

    # ── extra slash components in unscoped name ──────────────────

    def test_rejects_triple_component_direct(self) -> None:
        self._assert_rejects_direct("a/b/c")

    def test_rejects_double_component_direct(self) -> None:
        self._assert_rejects_direct("scope/name")

    def test_rejects_extra_slash_toml(self) -> None:
        self._assert_rejects_toml("scope/name")

    # ── empty name ───────────────────────────────────────────────

    def test_rejects_empty_direct(self) -> None:
        self._assert_rejects_direct("")

    def test_rejects_empty_toml(self) -> None:
        self._assert_rejects_toml("")

    # ── valid unscoped names ─────────────────────────────────────

    def test_accepts_simple_unscoped_direct(self) -> None:
        self._assert_accepts_direct("pi-read")

    def test_accepts_simple_unscoped_toml(self) -> None:
        self._assert_accepts_toml("pi-read")

    def test_accepts_unscoped_with_dash_direct(self) -> None:
        self._assert_accepts_direct("my-package")

    def test_accepts_unscoped_with_underscore_direct(self) -> None:
        self._assert_accepts_direct("my_package")

    def test_accepts_unscoped_with_dot_direct(self) -> None:
        self._assert_accepts_direct("my.package")

    def test_accepts_single_char_direct(self) -> None:
        self._assert_accepts_direct("a")

    # ── valid scoped names ───────────────────────────────────────

    def test_accepts_scoped_direct(self) -> None:
        self._assert_accepts_direct("@scope/name")

    def test_accepts_scoped_toml(self) -> None:
        self._assert_accepts_toml("@scope/name")

    def test_accepts_scoped_with_dashes_direct(self) -> None:
        self._assert_accepts_direct("@earendil-works/pi-green-loop")

    def test_accepts_scoped_with_dots_direct(self) -> None:
        self._assert_accepts_direct("@scope.dot/pkg.name")

    def test_accepts_scoped_single_char_direct(self) -> None:
        self._assert_accepts_direct("@s/p")


# ═══════════════════════════════════════════════════════════════════════
# Shared semver validation
# ═══════════════════════════════════════════════════════════════════════


class TestSharedSemverValidation(unittest.TestCase):
    """The shared :mod:`docker.versioning.semver` validator rejects
    moving tags, ranges, and malformed versions; accepts exact
    semver including prerelease and build metadata."""

    @staticmethod
    def _ok(version: str) -> None:
        from docker.versioning.semver import validate
        validate(version)

    @staticmethod
    def _fail(version: str) -> None:
        from docker.versioning.semver import SemverError, validate
        try:
            validate(version)
        except SemverError:
            return
        raise AssertionError(f"expected SemverError for {version!r}")

    # ── valid ────────────────────────────────────────────────────

    def test_stable(self) -> None:
        self._ok("1.2.3")

    def test_zero_major(self) -> None:
        self._ok("0.1.0")

    def test_prerelease(self) -> None:
        self._ok("1.0.0-alpha.1")

    def test_rc(self) -> None:
        self._ok("2.0.0-rc.2")

    def test_build_metadata(self) -> None:
        self._ok("1.2.3+build.20250101")

    def test_prerelease_with_build(self) -> None:
        self._ok("1.2.3-beta.1+exp.sha.5114f85")

    def test_multi_digit(self) -> None:
        self._ok("123.456.789")

    # ── moving tags ──────────────────────────────────────────────

    def test_latest_rejected(self) -> None:
        self._fail("latest")

    def test_stable_rejected(self) -> None:
        self._fail("stable")

    def test_next_rejected(self) -> None:
        self._fail("next")

    def test_dev_rejected(self) -> None:
        self._fail("dev")

    def test_canary_rejected(self) -> None:
        self._fail("canary")

    def test_nightly_rejected(self) -> None:
        self._fail("nightly")

    def test_moving_tag_case_insensitive(self) -> None:
        self._fail("LATEST")
        self._fail("Latest")

    # ── empty / whitespace ───────────────────────────────────────

    def test_empty_rejected(self) -> None:
        self._fail("")

    def test_whitespace_rejected(self) -> None:
        self._fail("   ")

    # ── ranges ───────────────────────────────────────────────────

    def test_star_rejected(self) -> None:
        self._fail("*")

    def test_caret_rejected(self) -> None:
        self._fail("^1.0.0")

    def test_tilde_rejected(self) -> None:
        self._fail("~1.2.3")

    def test_gte_rejected(self) -> None:
        self._fail(">=1.0.0")

    def test_lt_rejected(self) -> None:
        self._fail("<2.0.0")

    def test_x_range_rejected(self) -> None:
        self._fail("1.x")

    # ── malformed ────────────────────────────────────────────────

    def test_v_prefix_rejected(self) -> None:
        self._fail("v1.2.3")

    def test_partial_rejected(self) -> None:
        self._fail("1.2")

    def test_malformed_prerelease_rejected(self) -> None:
        self._fail("1.2.3-!!!")

    def test_leading_zero_prerelease_rejected(self) -> None:
        self._fail("1.2.3-01")

    def test_slash_rejected(self) -> None:
        self._fail("1/2")

    def test_backslash_rejected(self) -> None:
        self._fail("1\\2")

    def test_arbitrary_text_rejected(self) -> None:
        self._fail("not-a-version")


class TestUnsafeIntegrity(unittest.TestCase):
    """SRI integrity must be well-formed and use a supported algorithm."""

    def _entry_for(self, integrity: str) -> str:
        return f"""\
[extensions.p]
package = "p"
version = "1.0.0"
artifact = {{ artifact_id = "sha256/ypeBEsobvcr6wjGzmiPcTaeG7_gUfE5yuYB3ha_uSLs=.tgz", integrity = "{integrity}" }}
metadata_file = "package.json"
"""

    def test_malformed_sri_no_dash(self) -> None:
        path = _tmp_toml(self._entry_for("sha512AAAA"))
        with self.assertRaises(ProjectionError):
            read_projection(path)

    def test_unsupported_algorithm(self) -> None:
        path = _tmp_toml(self._entry_for("md5-AAAA"))
        with self.assertRaises(ProjectionError):
            read_projection(path)

    def test_invalid_base64(self) -> None:
        path = _tmp_toml(self._entry_for("sha512-!!!invalid!!!"))
        with self.assertRaises(ProjectionError):
            read_projection(path)

    def test_wrong_digest_length_for_algorithm(self) -> None:
        # sha256 needs 32 bytes = 44 base64 chars
        too_short = base64.b64encode(b"x" * 16).decode()
        path = _tmp_toml(self._entry_for(f"sha256-{too_short}"))
        with self.assertRaises(ProjectionError):
            read_projection(path)


_VALID_SHA256 = "sha256-" + base64.b64encode(
    hashlib.sha256(b"a").digest()
).decode()
_VALID_ARTIFACT_ID = (
    "sha256/"
    + _VALID_SHA256.split("-", 1)[1]
    .replace("+", "-").replace("/", "_")
    + ".tgz"
)
_VALID_SHA384 = "sha384-" + base64.b64encode(
    hashlib.sha384(b"a").digest()
).decode()
_VALID_SHA512 = "sha512-" + base64.b64encode(
    hashlib.sha512(b"a").digest()
).decode()


class TestValidIntegrityAlgorithms(unittest.TestCase):
    """sha256, sha384, sha512 are accepted."""

    def _entry(self, integrity: str) -> str:
        return f"""\
[extensions.p]
package = "p"
version = "1.0.0"
artifact = {{ artifact_id = "sha256/ypeBEsobvcr6wjGzmiPcTaeG7_gUfE5yuYB3ha_uSLs=.tgz", integrity = "{integrity}" }}
metadata_file = "package.json"
"""

    def test_sha256_accepted(self) -> None:
        entries = read_projection(_tmp_toml(self._entry(_VALID_SHA256)))
        self.assertEqual(1, len(entries))

    def test_sha384_accepted(self) -> None:
        entries = read_projection(_tmp_toml(self._entry(_VALID_SHA384)))
        self.assertEqual(1, len(entries))

    def test_sha512_accepted(self) -> None:
        entries = read_projection(_tmp_toml(self._entry(_VALID_SHA512)))
        self.assertEqual(1, len(entries))


# ═══════════════════════════════════════════════════════════════════════
# 10.1 — Exact artifact resolution
# ═══════════════════════════════════════════════════════════════════════


class TestPackageProcessFailures(unittest.TestCase):
    """Installer process failures produce diagnostics with bounded output."""

    def setUp(self) -> None:
        self.ctx = _make_fake_context()

    def test_executable_missing_diagnostic(self) -> None:
        self.ctx.installer._fail_with(  # type: ignore[attr-defined]
            InstallError("executable 'pi' not found on PATH"),
        )
        entries = [ProjectionEntry(
            package="p", version="1.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="package.json",
        )]
        result = install_extensions(
            self.ctx, entries=entries, pi_home="/mnt/pi",
        )
        self.assertIn("pi", (result.results[0].detail or "").lower())

    def test_nonzero_status_diagnostic(self) -> None:
        self.ctx.installer._fail_with(  # type: ignore[attr-defined]
            InstallError("pi install returned exit code 2: stderr output"),
        )
        entries = [ProjectionEntry(
            package="p", version="1.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="package.json",
        )]
        result = install_extensions(
            self.ctx, entries=entries, pi_home="/mnt/pi",
        )
        self.assertIn("exit code", (result.results[0].detail or "").lower())

    def test_diagnostics_include_package_and_version(self) -> None:
        self.ctx.installer._fail_with(  # type: ignore[attr-defined]
            InstallError("installation failed"),
        )
        entries = [ProjectionEntry(
            package="@x/y", version="3.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="package.json",
        )]
        result = install_extensions(
            self.ctx, entries=entries, pi_home="/mnt/pi",
        )
        detail = result.results[0].detail or ""
        self.assertIn("@x/y", detail)
        self.assertIn("3.0.0", detail)


# ═══════════════════════════════════════════════════════════════════════
# 10.2 — Idempotent installation
# ═══════════════════════════════════════════════════════════════════════


class TestIdempotentInstallation(unittest.TestCase):
    """Already-correctly-installed packages must be skipped entirely."""

    def setUp(self) -> None:
        self.ctx = _make_fake_context()

    def test_already_installed_returns_already_status(self) -> None:
        self.ctx.metadata._set_installed(  # type: ignore[attr-defined]
            package="p", version="1.0.0",
        )
        entries = [ProjectionEntry(
            package="p", version="1.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="package.json",
        )]
        result = install_extensions(
            self.ctx, entries=entries, pi_home="/mnt/pi",
        )
        self.assertTrue(result.ok)
        self.assertEqual(
            InstallStatus.ALREADY_INSTALLED, result.results[0].status,
        )

    def test_already_installed_skips_download_and_install(self) -> None:
        self.ctx.metadata._set_installed(  # type: ignore[attr-defined]
            package="p", version="1.0.0",
        )
        entries = [ProjectionEntry(
            package="p", version="1.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="package.json",
        )]
        install_extensions(self.ctx, entries=entries, pi_home="/mnt/pi")
        self.assertEqual(0, self.ctx.blob_reader.call_count)  # type: ignore[attr-defined]
        self.assertEqual(0, self.ctx.installer.call_count)  # type: ignore[attr-defined]


# ═══════════════════════════════════════════════════════════════════════
# 10.2 cont. — Already-matching package (spec 23)
# ═══════════════════════════════════════════════════════════════════════


class TestAlreadyMatchingPackage(unittest.TestCase):
    """When metadata name+version match the projection exactly,
    no download/install occurs and ALREADY_INSTALLED is reported."""

    def setUp(self) -> None:
        self.ctx = _make_fake_context()

    def test_metadata_name_equals_projected_package(self) -> None:
        self.ctx.metadata._set_installed(  # type: ignore[attr-defined]
            package="p", version="1.0.0",
        )
        entries = [ProjectionEntry(
            package="p", version="1.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="package.json",
        )]
        result = install_extensions(
            self.ctx, entries=entries, pi_home="/mnt/pi",
        )
        self.assertEqual(
            InstallStatus.ALREADY_INSTALLED, result.results[0].status,
        )

    def test_metadata_version_equals_projected_version(self) -> None:
        self.ctx.metadata._set_installed(  # type: ignore[attr-defined]
            package="p", version="3.2.1",
        )
        entries = [ProjectionEntry(
            package="p", version="3.2.1",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="package.json",
        )]
        result = install_extensions(
            self.ctx, entries=entries, pi_home="/mnt/pi",
        )
        self.assertEqual(
            InstallStatus.ALREADY_INSTALLED, result.results[0].status,
        )

    def test_already_matching_triggers_no_download(self) -> None:
        self.ctx.metadata._set_installed(  # type: ignore[attr-defined]
            package="p", version="1.0.0",
        )
        entries = [ProjectionEntry(
            package="p", version="1.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="package.json",
        )]
        install_extensions(self.ctx, entries=entries, pi_home="/mnt/pi")
        self.assertEqual(0, self.ctx.blob_reader.call_count)  # type: ignore[attr-defined]

    def test_already_matching_triggers_no_install(self) -> None:
        self.ctx.metadata._set_installed(  # type: ignore[attr-defined]
            package="p", version="1.0.0",
        )
        entries = [ProjectionEntry(
            package="p", version="1.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="package.json",
        )]
        install_extensions(self.ctx, entries=entries, pi_home="/mnt/pi")
        self.assertEqual(0, self.ctx.installer.call_count)  # type: ignore[attr-defined]

    def test_already_matching_outcome_is_already_installed(self) -> None:
        self.ctx.metadata._set_installed(  # type: ignore[attr-defined]
            package="p", version="1.0.0",
        )
        entries = [ProjectionEntry(
            package="p", version="1.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="package.json",
        )]
        result = install_extensions(
            self.ctx, entries=entries, pi_home="/mnt/pi",
        )
        self.assertEqual(
            InstallStatus.ALREADY_INSTALLED, result.results[0].status,
        )


# ═══════════════════════════════════════════════════════════════════════
# 10.2 — Mismatched installed packages
# ═══════════════════════════════════════════════════════════════════════


class TestMismatchedInstalledPackages(unittest.TestCase):
    """Version/name mismatch after install must be reported as FAILED."""

    def setUp(self) -> None:
        self.ctx = _make_fake_context()
        # Disable id/integrity agreement check — these tests validate
        # metadata mismatch behaviour, not identity derivation.
        self.ctx.blob_reader._disable_id_check()  # type: ignore[attr-defined]

    def test_version_mismatch_is_failure(self) -> None:
        self.ctx.metadata._set_installed(  # type: ignore[attr-defined]
            package="p", version="0.9.0",
        )
        entries = [ProjectionEntry(
            package="p", version="1.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="package.json",
        )]
        result = install_extensions(
            self.ctx, entries=entries, pi_home="/mnt/pi",
        )
        self.assertFalse(result.ok)
        self.assertEqual(InstallStatus.FAILED, result.results[0].status)

    def test_name_mismatch_is_always_failure(self) -> None:
        self.ctx.metadata._set_installed(  # type: ignore[attr-defined]
            package="good", version="1.0.0",
        )
        self.ctx.metadata._set_spoof_name("evil")  # type: ignore[attr-defined]
        entries = [ProjectionEntry(
            package="good", version="1.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="package.json",
        )]
        result = install_extensions(
            self.ctx, entries=entries, pi_home="/mnt/pi",
        )
        self.assertFalse(result.ok)


# ═══════════════════════════════════════════════════════════════════════
# 10.2 cont. — Mismatched-package edge cases (specs 24–25)
# ═══════════════════════════════════════════════════════════════════════


class TestMismatchedPackageEdgeCases(unittest.TestCase):
    """Every mismatch triggers a protected reinstall; presence alone
    is never treated as success."""

    def setUp(self) -> None:
        self.ctx = _make_fake_context()
        # Disable id/integrity agreement check — these tests validate
        # metadata edge cases, not identity derivation.
        self.ctx.blob_reader._disable_id_check()  # type: ignore[attr-defined]

    def test_missing_package_triggers_install(self) -> None:
        entries = [ProjectionEntry(
            package="p", version="1.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="package.json",
        )]
        result = install_extensions(
            self.ctx, entries=entries, pi_home="/mnt/pi",
        )
        self.assertTrue(result.ok)
        self.assertGreater(self.ctx.blob_reader.call_count, 0)  # type: ignore[attr-defined]
        self.assertGreater(self.ctx.installer.call_count, 0)  # type: ignore[attr-defined]

    def test_wrong_package_name_triggers_reinstall(self) -> None:
        self.ctx.metadata._set_installed(  # type: ignore[attr-defined]
            package="good", version="1.0.0",
        )
        self.ctx.metadata._set_spoof_name("evil")  # type: ignore[attr-defined]
        entries = [ProjectionEntry(
            package="good", version="1.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="package.json",
        )]
        result = install_extensions(
            self.ctx, entries=entries, pi_home="/mnt/pi",
        )
        self.assertFalse(result.ok)
        self.assertEqual(InstallStatus.FAILED, result.results[0].status)

    def test_wrong_version_triggers_reinstall(self) -> None:
        self.ctx.metadata._set_installed(  # type: ignore[attr-defined]
            package="p", version="0.9.0",
        )
        entries = [ProjectionEntry(
            package="p", version="1.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="package.json",
        )]
        result = install_extensions(
            self.ctx, entries=entries, pi_home="/mnt/pi",
        )
        self.assertGreater(self.ctx.blob_reader.call_count, 0)  # type: ignore[attr-defined]
        self.assertGreater(self.ctx.installer.call_count, 0)  # type: ignore[attr-defined]
        self.assertFalse(result.ok)

    def test_malformed_package_json_yields_failure(self) -> None:
        self.ctx.metadata._set_malformed(True)  # type: ignore[attr-defined]
        entries = [ProjectionEntry(
            package="p", version="1.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="package.json",
        )]
        result = install_extensions(
            self.ctx, entries=entries, pi_home="/mnt/pi",
        )
        self.assertFalse(result.ok)
        self.assertEqual(InstallStatus.FAILED, result.results[0].status)

    def test_metadata_path_is_directory(self) -> None:
        self.ctx.metadata._set_is_directory(True)  # type: ignore[attr-defined]
        entries = [ProjectionEntry(
            package="p", version="1.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="package.json",
        )]
        result = install_extensions(
            self.ctx, entries=entries, pi_home="/mnt/pi",
        )
        self.assertFalse(result.ok)

    def test_mismatch_policy_never_treats_presence_as_success(self) -> None:
        self.ctx.metadata._set_installed(  # type: ignore[attr-defined]
            package="target", version="1.0.0",
        )
        self.ctx.metadata._set_spoof_name("other")  # type: ignore[attr-defined]
        entries = [ProjectionEntry(
            package="target", version="1.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="package.json",
        )]
        result = install_extensions(
            self.ctx, entries=entries, pi_home="/mnt/pi",
        )
        self.assertEqual(InstallStatus.FAILED, result.results[0].status)


class TestMetadataPreCheckErrorDiscrimination(unittest.TestCase):
    """Only :class:`MetadataNotFoundError` triggers a fresh install.
    Read/parse/permission failures surface immediately without download."""

    def setUp(self) -> None:
        self.ctx = _make_fake_context()

    def _single_entry(self) -> ProjectionEntry:
        return ProjectionEntry(
            package="p", version="1.0.0",
            artifact_id=_VALID_ARTIFACT_ID,
            artifact_integrity=_VALID_SHA256,
            metadata_file="package.json",
        )

    def test_metadata_not_found_triggers_install(self) -> None:
        """MetadataNotFoundError → blob read + install proceeds."""
        entries = [self._single_entry()]
        result = install_extensions(
            self.ctx, entries=entries, pi_home="/mnt/pi",
        )
        self.assertTrue(result.ok)
        self.assertEqual(InstallStatus.OK, result.results[0].status)
        self.assertIn(
            "blob_reader.open_verified",
            self.ctx.blob_reader._call_log,  # type: ignore[attr-defined]
        )

    def test_malformed_metadata_surfaces_without_install(self) -> None:
        """Parse/malformed error → surfaced as FAILED; no blob read."""
        self.ctx.metadata._malformed = True  # type: ignore[attr-defined]
        entries = [self._single_entry()]
        result = install_extensions(
            self.ctx, entries=entries, pi_home="/mnt/pi",
        )
        self.assertFalse(result.ok)
        self.assertEqual(InstallStatus.FAILED, result.results[0].status)
        self.assertNotIn(
            "blob_reader.open_verified",
            self.ctx.blob_reader._call_log,  # type: ignore[attr-defined]
            "malformed metadata must not trigger a blob read",
        )

    def test_read_failure_surfaces_without_install(self) -> None:
        """Generic InstallError (e.g. permission) → FAILED; no blob read."""
        self.ctx.metadata._fail_on_next_read(  # type: ignore[attr-defined]
            InstallError("EACCES: permission denied"),
        )
        entries = [self._single_entry()]
        result = install_extensions(
            self.ctx, entries=entries, pi_home="/mnt/pi",
        )
        self.assertFalse(result.ok)
        self.assertEqual(InstallStatus.FAILED, result.results[0].status)
        self.assertNotIn(
            "blob_reader.open_verified",
            self.ctx.blob_reader._call_log,  # type: ignore[attr-defined]
            "read permission error must not trigger a blob read",
        )


# ═══════════════════════════════════════════════════════════════════════
# 10.2 cont. — Reinstall + identity/version validation (specs 26–29)
# ═══════════════════════════════════════════════════════════════════════


class TestReinstallAndValidate(unittest.TestCase):
    """Successful reinstall followed by exact identity/version check."""

    def setUp(self) -> None:
        self.ctx = _make_fake_context()

    def test_successful_reinstall_with_exact_match(self) -> None:
        self.ctx.metadata._set_installed(  # type: ignore[attr-defined]
            package="p", version="1.0.0",
        )
        entries = [ProjectionEntry(
            package="p", version="1.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="package.json",
        )]
        result = install_extensions(
            self.ctx, entries=entries, pi_home="/mnt/pi",
        )
        self.assertTrue(result.ok)

    def test_successful_install_missing_post_install_metadata(self) -> None:
        self.ctx.metadata._fail_on_all_reads(  # type: ignore[attr-defined]
            InstallError("package.json not found after install"),
        )
        entries = [ProjectionEntry(
            package="p", version="1.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="package.json",
        )]
        result = install_extensions(
            self.ctx, entries=entries, pi_home="/mnt/pi",
        )
        self.assertFalse(result.ok)
        self.assertEqual(InstallStatus.FAILED, result.results[0].status)

    def test_successful_install_wrong_post_install_identity(self) -> None:
        self.ctx.metadata._set_installed(  # type: ignore[attr-defined]
            package="genuine", version="1.0.0",
        )
        self.ctx.metadata._set_spoof_name("impostor")  # type: ignore[attr-defined]
        entries = [ProjectionEntry(
            package="genuine", version="1.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="package.json",
        )]
        result = install_extensions(
            self.ctx, entries=entries, pi_home="/mnt/pi",
        )
        self.assertFalse(result.ok)

    def test_successful_install_wrong_post_install_version(self) -> None:
        self.ctx.metadata._set_installed(  # type: ignore[attr-defined]
            package="p", version="0.0.0",
        )
        entries = [ProjectionEntry(
            package="p", version="1.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="package.json",
        )]
        result = install_extensions(
            self.ctx, entries=entries, pi_home="/mnt/pi",
        )
        self.assertFalse(result.ok)


# ═══════════════════════════════════════════════════════════════════════
# 10.2 cont. — Multiple extensions (spec 30)
# ═══════════════════════════════════════════════════════════════════════


class TestMultipleExtensions(unittest.TestCase):
    """Multiple extensions: matches skipped, mismatches repaired,
    first failure stops subsequent mutations, prior successes reported."""

    def setUp(self) -> None:
        self.ctx = _make_fake_context()

    def test_matching_entries_skipped(self) -> None:
        self.ctx.metadata._set_installed(  # type: ignore[attr-defined]
            package="skip-me", version="1.0.0",
        )
        entries = [
            ProjectionEntry(
                package="skip-me", version="1.0.0",
                artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
                metadata_file="package.json",
            ),
            ProjectionEntry(
                package="install-me", version="2.0.0",
                artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
                metadata_file="package.json",
            ),
        ]
        self.ctx.installer._expect_version("install-me", "2.0.0")  # type: ignore[attr-defined]
        result = install_extensions(
            self.ctx, entries=entries, pi_home="/mnt/pi",
        )
        self.assertTrue(result.ok)
        self.assertEqual(
            InstallStatus.ALREADY_INSTALLED, result.results[0].status,
        )
        self.assertEqual(
            InstallStatus.OK, result.results[1].status,
        )

    def test_mismatches_repaired(self) -> None:
        self.ctx.metadata._set_installed(  # type: ignore[attr-defined]
            package="stale", version="0.9.0",
        )
        entries = [ProjectionEntry(
            package="stale", version="1.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="package.json",
        )]
        result = install_extensions(
            self.ctx, entries=entries, pi_home="/mnt/pi",
        )
        self.assertGreater(self.ctx.blob_reader.call_count, 0)  # type: ignore[attr-defined]

    def test_first_failure_stops_subsequent_mutations(self) -> None:
        self.ctx.blob_reader._set_failure(  # type: ignore[attr-defined]
            InstallError("network unreachable"),
        )
        entries = [
            ProjectionEntry(
                package="first", version="1.0.0",
                artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
                metadata_file="package.json",
            ),
            ProjectionEntry(
                package="second", version="2.0.0",
                artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
                metadata_file="package.json",
            ),
        ]
        result = install_extensions(
            self.ctx, entries=entries, pi_home="/mnt/pi",
        )
        self.assertFalse(result.ok)
        self.assertEqual(1, self.ctx.blob_reader.call_count)  # type: ignore[attr-defined]

    def test_prior_successful_entries_accurately_reported(self) -> None:
        self.ctx.metadata._set_installed(  # type: ignore[attr-defined]
            package="ok-pkg", version="1.0.0",
        )
        self.ctx.blob_reader._set_failure(  # type: ignore[attr-defined]
            InstallError("download failed after N"),
        )
        entries = [
            ProjectionEntry(
                package="ok-pkg", version="1.0.0",
                artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
                metadata_file="package.json",
            ),
            ProjectionEntry(
                package="fail-pkg", version="2.0.0",
                artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
                metadata_file="package.json",
            ),
        ]
        result = install_extensions(
            self.ctx, entries=entries, pi_home="/mnt/pi",
        )
        self.assertEqual(
            InstallStatus.ALREADY_INSTALLED, result.results[0].status,
        )
        self.assertEqual(
            InstallStatus.FAILED, result.results[1].status,
        )
        self.assertFalse(result.ok)


# ═══════════════════════════════════════════════════════════════════════
# 10.2 — Post-install identity/version validation
# ═══════════════════════════════════════════════════════════════════════


class TestPostInstallValidation(unittest.TestCase):
    """After installation, metadata must be verified against the projection."""

    def setUp(self) -> None:
        self.ctx = _make_fake_context()
        # Disable id/integrity agreement check — these tests validate
        # post-install metadata verification, not identity derivation.
        self.ctx.blob_reader._disable_id_check()  # type: ignore[attr-defined]

    def test_post_install_match_is_ok(self) -> None:
        self.ctx.metadata._set_installed(  # type: ignore[attr-defined]
            package="p", version="1.0.0",
        )
        entries = [ProjectionEntry(
            package="p", version="1.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="package.json",
        )]
        result = install_extensions(
            self.ctx, entries=entries, pi_home="/mnt/pi",
        )
        self.assertTrue(result.ok)

    def test_post_install_name_mismatch_is_failure(self) -> None:
        self.ctx.metadata._set_installed(  # type: ignore[attr-defined]
            package="right", version="1.0.0",
        )
        self.ctx.metadata._set_spoof_name("evil")  # type: ignore[attr-defined]
        entries = [ProjectionEntry(
            package="right", version="1.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="package.json",
        )]
        result = install_extensions(
            self.ctx, entries=entries, pi_home="/mnt/pi",
        )
        self.assertFalse(result.ok)


class TestAlreadyInstalledOwnerValidation(unittest.TestCase):
    """A matching name/version must NOT bypass ``validate_owner`` —
    root- or foreign-owned installed metadata must be rejected."""

    def setUp(self) -> None:
        self.ctx = _make_fake_context()

    def _matching_entry(self) -> ProjectionEntry:
        return ProjectionEntry(
            package="p", version="1.0.0",
            artifact_id=_VALID_ARTIFACT_ID,
            artifact_integrity=_VALID_SHA256,
            metadata_file="package.json",
        )

    def test_wrong_owner_is_reported_as_failed(self) -> None:
        """Name + version match, but owner is not dev:dev → FAILED."""
        self.ctx.metadata._set_installed(  # type: ignore[attr-defined]
            package="p", version="1.0.0",
        )
        self.ctx.privilege._fail_validate = True  # type: ignore[attr-defined]
        entries = [self._matching_entry()]
        result = install_extensions(
            self.ctx, entries=entries, pi_home="/mnt/pi",
        )
        self.assertFalse(result.ok)
        self.assertEqual(InstallStatus.FAILED, result.results[0].status)

    def test_wrong_owner_calls_validate_owner(self) -> None:
        """validate_owner is invoked even for matching installed packages."""
        self.ctx.metadata._set_installed(  # type: ignore[attr-defined]
            package="p", version="1.0.0",
        )
        self.ctx.privilege._fail_validate = True  # type: ignore[attr-defined]
        entries = [self._matching_entry()]
        install_extensions(self.ctx, entries=entries, pi_home="/mnt/pi")
        self.assertIn(
            "privilege.validate_owner",
            self.ctx.privilege._call_log,  # type: ignore[attr-defined]
            "validate_owner must be called even for ALREADY_INSTALLED",
        )

    def test_correct_owner_still_reports_already_installed(self) -> None:
        """Correct owner (dev:dev) with matching metadata → ALREADY_INSTALLED."""
        self.ctx.metadata._set_installed(  # type: ignore[attr-defined]
            package="p", version="1.0.0",
        )
        entries = [self._matching_entry()]
        result = install_extensions(
            self.ctx, entries=entries, pi_home="/mnt/pi",
        )
        self.assertTrue(result.ok)
        self.assertEqual(
            InstallStatus.ALREADY_INSTALLED, result.results[0].status,
        )
        self.assertIn(
            "privilege.validate_owner",
            self.ctx.privilege._call_log,  # type: ignore[attr-defined]
        )


# ═══════════════════════════════════════════════════════════════════════
# 10.2 — metadata_file passthrough
# ═══════════════════════════════════════════════════════════════════════


def _npm_metadata_path(pi_home: str, package: str, metadata_file: str) -> str:
    """Resolve the installed metadata path in the Pi CLI npm layout."""
    import os
    return os.path.join(
        pi_home, "agent", "npm", "node_modules", package, metadata_file,
    )


class TestMetadataPathResolution(unittest.TestCase):
    """Installed metadata MUST resolve to the exact npm layout:
    ``<pi_home>/agent/npm/node_modules/<package>/<metadata_file>``.
    Unscoped, scoped, and nested metadata_file variants must all
    produce the correct path."""

    def setUp(self) -> None:
        self.ctx = _make_fake_context()

    # ── unscoped, default metadata_file ──────────────────────────

    def test_unscoped_default_metadata_path(self) -> None:
        entries = [ProjectionEntry(
            package="pi-read", version="1.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="package.json",
        )]
        install_extensions(
            self.ctx, entries=entries, pi_home="/mnt/pi",
        )
        expected = "/mnt/pi/agent/npm/node_modules/pi-read/package.json"
        self._assert_validate_owner_called_for(expected)

    # ── unscoped, nested metadata_file ───────────────────────────

    def test_unscoped_nested_metadata_path(self) -> None:
        self.ctx.installer._expect_version("my-pkg", "2.0.0")  # type: ignore[attr-defined]
        entries = [ProjectionEntry(
            package="my-pkg", version="2.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="dist/package.json",
        )]
        install_extensions(
            self.ctx, entries=entries, pi_home="/mnt/pi",
        )
        expected = "/mnt/pi/agent/npm/node_modules/my-pkg/dist/package.json"
        self._assert_validate_owner_called_for(expected)

    # ── scoped, default metadata_file ────────────────────────────

    def test_scoped_default_metadata_path(self) -> None:
        self.ctx.installer._expect_version("@earendil-works/pi", "3.0.0")  # type: ignore[attr-defined]
        entries = [ProjectionEntry(
            package="@earendil-works/pi", version="3.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="package.json",
        )]
        install_extensions(
            self.ctx, entries=entries, pi_home="/mnt/pi",
        )
        expected = (
            "/mnt/pi/agent/npm/node_modules/"
            "@earendil-works/pi/package.json"
        )
        self._assert_validate_owner_called_for(expected)

    # ── scoped, nested metadata_file ─────────────────────────────

    def test_scoped_nested_metadata_path(self) -> None:
        self.ctx.installer._expect_version("@scope/pkg", "2.0.0")  # type: ignore[attr-defined]
        entries = [ProjectionEntry(
            package="@scope/pkg", version="2.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="sub/package.json",
        )]
        install_extensions(
            self.ctx, entries=entries, pi_home="/mnt/pi",
        )
        expected = (
            "/mnt/pi/agent/npm/node_modules/@scope/pkg/sub/package.json"
        )
        self._assert_validate_owner_called_for(expected)

    # ── different pi_home ────────────────────────────────────────

    def test_different_pi_home_changes_prefix(self) -> None:
        entries = [ProjectionEntry(
            package="p", version="1.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="package.json",
        )]
        install_extensions(
            self.ctx, entries=entries, pi_home="/opt/pi-home",
        )
        expected = "/opt/pi-home/agent/npm/node_modules/p/package.json"
        self._assert_validate_owner_called_for(expected)

    # ── reject paths not inside the npm layout ───────────────────

    def test_rejects_path_outside_npm_layout(self) -> None:
        """A bare path like /mnt/pi/package.json is never valid."""
        entries = [ProjectionEntry(
            package="p", version="1.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="package.json",
        )]
        install_extensions(
            self.ctx, entries=entries, pi_home="/mnt/pi",
        )
        for path, _ in self.ctx.privilege._owner_validations:  # type: ignore[attr-defined]
            self.assertIn("/agent/npm/node_modules/", path,
                          "metadata path must be inside npm node_modules")

    # ── helpers ──────────────────────────────────────────────────

    def _assert_validate_owner_called_for(self, expected: str) -> None:
        changes = self.ctx.privilege._owner_validations  # type: ignore[attr-defined]
        self.assertGreater(len(changes), 0,
                           f"expected validate_owner({expected!r})")
        paths = [p for p, _ in changes]
        self.assertIn(expected, paths,
                      f"expected {expected!r} in validate_owner paths {paths!r}")


class TestMetadataFilePassthrough(unittest.TestCase):
    """The projected metadata_file is passed to every MetadataReader.read
    call (pre-check and post-install) and the resolved path is passed to
    PrivilegeContext.validate_owner.  Non-default nested paths must work
    end-to-end."""

    def setUp(self) -> None:
        self.ctx = _make_fake_context()

    # ── pre-check read receives metadata_file ────────────────────

    def test_pre_check_read_receives_default_metadata_file(self) -> None:
        entries = [ProjectionEntry(
            package="p", version="1.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="package.json",
        )]
        install_extensions(
            self.ctx, entries=entries, pi_home="/mnt/pi",
        )
        first_read = self.ctx.metadata._last_read  # type: ignore[attr-defined]
        self.assertEqual("package.json", first_read["metadata_file"])
        self.assertEqual("p", first_read["package"])
        self.assertEqual("/mnt/pi", first_read["pi_home"])

    def test_pre_check_read_receives_nested_metadata_file(self) -> None:
        entries = [ProjectionEntry(
            package="@scope/pkg", version="3.2.1",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="nested/deep/package.json",
        )]
        install_extensions(
            self.ctx, entries=entries, pi_home="/mnt/pi",
        )
        first_read = self.ctx.metadata._last_read  # type: ignore[attr-defined]
        self.assertEqual("nested/deep/package.json",
                         first_read["metadata_file"])

    # ── post-install read receives the same metadata_file ────────

    def test_post_install_read_receives_metadata_file(self) -> None:
        self.ctx.metadata._set_installed(  # type: ignore[attr-defined]
            package="p", version="1.0.0",
        )
        entries = [ProjectionEntry(
            package="p", version="1.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="sub/pkg.json",
        )]
        result = install_extensions(
            self.ctx, entries=entries, pi_home="/mnt/pi",
        )
        self.assertEqual(
            InstallStatus.ALREADY_INSTALLED, result.results[0].status,
        )
        self.assertEqual(
            "sub/pkg.json",
            self.ctx.metadata._last_read["metadata_file"],  # type: ignore[attr-defined]
        )

    # ── validate_owner receives resolved metadata path ─────────────

    def test_validate_owner_receives_resolved_default_path(self) -> None:
        """Unscoped package → /mnt/pi/agent/npm/node_modules/pkg/package.json."""
        entries = [ProjectionEntry(
            package="p", version="1.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="package.json",
        )]
        install_extensions(
            self.ctx, entries=entries, pi_home="/mnt/pi",
        )
        expected = "/mnt/pi/agent/npm/node_modules/p/package.json"
        paths = [p for p, _ in self.ctx.privilege._owner_validations]  # type: ignore[attr-defined]
        self.assertIn(expected, paths,
                      f"expected {expected!r} in {paths!r}")

    def test_validate_owner_receives_resolved_nested_path(self) -> None:
        """Scoped package, nested metadata → full npm layout path."""
        self.ctx.installer._expect_version("@s/p", "2.0.0")  # type: ignore[attr-defined]
        entries = [ProjectionEntry(
            package="@s/p", version="2.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="dist/pkg.json",
        )]
        install_extensions(
            self.ctx, entries=entries, pi_home="/mnt/pi",
        )
        expected = "/mnt/pi/agent/npm/node_modules/@s/p/dist/pkg.json"
        paths = [p for p, _ in self.ctx.privilege._owner_validations]  # type: ignore[attr-defined]
        self.assertIn(expected, paths,
                      f"expected {expected!r} in {paths!r}")

    # ── different entries carry different metadata_file ──────────

    def test_two_entries_different_metadata_files(self) -> None:
        self.ctx.metadata._set_installed(  # type: ignore[attr-defined]
            package="a", version="1.0.0",
        )
        entries = [
            ProjectionEntry(
                package="a", version="1.0.0",
                artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
                metadata_file="a/package.json",
            ),
            ProjectionEntry(
                package="b", version="2.0.0",
                artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
                metadata_file="b/custom.json",
            ),
        ]
        install_extensions(
            self.ctx, entries=entries, pi_home="/mnt/pi",
        )
        # The fake records the *last* read; for "b" it should be b/custom.json
        last = self.ctx.metadata._last_read  # type: ignore[attr-defined]
        self.assertEqual("b/custom.json", last["metadata_file"])
        self.assertEqual("b", last["package"])


# ═══════════════════════════════════════════════════════════════════════
# 10.2 — Mount check guard
# ═══════════════════════════════════════════════════════════════════════


class TestPiHomeMountCheck(unittest.TestCase):
    """Installation refused when pi_home is not a mount point."""

    def setUp(self) -> None:
        self.ctx = _make_fake_context()

    def test_not_mount_raises_install_error(self) -> None:
        self.ctx.mount_check._set_mount(False)  # type: ignore[attr-defined]
        entries = [ProjectionEntry(
            package="p", version="1.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="package.json",
        )]
        with self.assertRaises(InstallError):
            install_extensions(
                self.ctx, entries=entries, pi_home="/mnt/pi",
            )

    def test_mount_failure_before_any_io(self) -> None:
        self.ctx.mount_check._set_mount(False)  # type: ignore[attr-defined]
        entries = [ProjectionEntry(
            package="p", version="1.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="package.json",
        )]
        try:
            install_extensions(
                self.ctx, entries=entries, pi_home="/mnt/pi",
            )
        except InstallError:
            pass
        self.assertEqual(0, self.ctx.blob_reader.call_count)  # type: ignore[attr-defined]
        self.assertEqual(0, self.ctx.installer.call_count)  # type: ignore[attr-defined]

    def test_is_mount_allows_proceeding(self) -> None:
        self.ctx.mount_check._set_mount(True)  # type: ignore[attr-defined]
        entries = [ProjectionEntry(
            package="p", version="1.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="package.json",
        )]
        result = install_extensions(
            self.ctx, entries=entries, pi_home="/mnt/pi",
        )
        self.assertIsInstance(result, InstallResult)


# ═══════════════════════════════════════════════════════════════════════
# 10.2 — Dry-run
# ═══════════════════════════════════════════════════════════════════════


class TestDryRun(unittest.TestCase):
    """Dry-run reports what *would* happen without side-effects."""

    def setUp(self) -> None:
        self.ctx = _make_fake_context()

    def test_dry_run_does_not_install(self) -> None:
        entries = [ProjectionEntry(
            package="p", version="1.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="package.json",
        )]
        result = install_extensions(
            self.ctx, entries=entries, pi_home="/mnt/pi", dry_run=True,
        )
        self.assertTrue(result.dry_run)
        self.assertEqual(0, self.ctx.installer.call_count)  # type: ignore[attr-defined]

    def test_dry_run_does_not_download(self) -> None:
        entries = [ProjectionEntry(
            package="p", version="1.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="package.json",
        )]
        install_extensions(
            self.ctx, entries=entries, pi_home="/mnt/pi", dry_run=True,
        )
        self.assertEqual(0, self.ctx.blob_reader.call_count)  # type: ignore[attr-defined]

    def test_dry_run_still_checks_mount(self) -> None:
        self.ctx.mount_check._set_mount(False)  # type: ignore[attr-defined]
        entries = [ProjectionEntry(
            package="p", version="1.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="package.json",
        )]
        with self.assertRaises(InstallError):
            install_extensions(
                self.ctx, entries=entries, pi_home="/mnt/pi",
                dry_run=True,
            )


# ═══════════════════════════════════════════════════════════════════════
# 10.2 cont. — Pi-home safety (spec 31)
# ═══════════════════════════════════════════════════════════════════════


class TestPiHomeSafety(unittest.TestCase):
    """Pi-home must be a real, mounted, writable directory.
    Metadata must not resolve outside Pi home."""

    def setUp(self) -> None:
        self.ctx = _make_fake_context()

    def test_absent_pi_home_raises(self) -> None:
        self.ctx.mount_check._set_exists(False)  # type: ignore[attr-defined]
        entries = [ProjectionEntry(
            package="p", version="1.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="package.json",
        )]
        with self.assertRaises(InstallError):
            install_extensions(
                self.ctx, entries=entries, pi_home="/nonexistent",
            )

    def test_pi_home_not_mounted_raises(self) -> None:
        self.ctx.mount_check._set_mount(False)  # type: ignore[attr-defined]
        entries = [ProjectionEntry(
            package="p", version="1.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="package.json",
        )]
        with self.assertRaises(InstallError):
            install_extensions(
                self.ctx, entries=entries, pi_home="/mnt/pi",
            )

    def test_pi_home_is_symlink_raises(self) -> None:
        self.ctx.mount_check._set_is_symlink(True)  # type: ignore[attr-defined]
        entries = [ProjectionEntry(
            package="p", version="1.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="package.json",
        )]
        with self.assertRaises(InstallError):
            install_extensions(
                self.ctx, entries=entries, pi_home="/mnt/pi",
            )

    def test_metadata_resolves_outside_pi_home_raises(self) -> None:
        with self.assertRaises(MetadataValidationError):
            ProjectionEntry(
                package="p", version="1.0.0",
                artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
                metadata_file="../../etc/passwd",
            )

    def test_non_writable_pi_home_raises(self) -> None:
        self.ctx.mount_check._set_writable(False)  # type: ignore[attr-defined]
        entries = [ProjectionEntry(
            package="p", version="1.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="package.json",
        )]
        with self.assertRaises(InstallError):
            install_extensions(
                self.ctx, entries=entries, pi_home="/mnt/pi",
            )


# ═══════════════════════════════════════════════════════════════════════
# 10.2 cont. — Ownership expectations (spec 32)
# ═══════════════════════════════════════════════════════════════════════


class TestOwnershipExpectations(unittest.TestCase):
    """Installer enforces dev identity before mutation; ensures
    installed files are owned dev:dev; no ownership changes outside
    Pi home."""

    def setUp(self) -> None:
        self.ctx = _make_fake_context()
        self.p = ProjectionEntry(
            package="p", version="1.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="package.json",
        )

    # ── verify_user boundary ──────────────────────────────────────

    def test_verifies_running_as_dev_before_mutation(self) -> None:
        install_extensions(
            self.ctx, entries=[self.p], pi_home="/mnt/pi",
        )
        verified = self.ctx.privilege._verified_user  # type: ignore[attr-defined]
        self.assertEqual("dev", verified,
                         "must verify current user is dev before any mutation")

    def test_wrong_user_blocks_all_mutations(self) -> None:
        self.ctx.privilege._verified_user = None  # type: ignore[attr-defined]
        self.ctx.privilege._fail_verify = True     # type: ignore[attr-defined]
        with self.assertRaises(InstallError):
            install_extensions(
                self.ctx, entries=[self.p], pi_home="/mnt/pi",
            )
        self.assertEqual(0, self.ctx.blob_reader.call_count)  # type: ignore[attr-defined]
        self.assertEqual(0, self.ctx.installer.call_count)  # type: ignore[attr-defined]

    # ── validate_owner boundary ─────────────────────────────────────

    def test_validates_owner_on_installed_metadata(self) -> None:
        install_extensions(
            self.ctx, entries=[self.p], pi_home="/mnt/pi",
        )
        changes = self.ctx.privilege._owner_validations  # type: ignore[attr-defined]
        self.assertGreater(len(changes), 0,
                           "must call validate_owner after install")

    def test_validates_dev_colon_dev_ownership(self) -> None:
        install_extensions(
            self.ctx, entries=[self.p], pi_home="/mnt/pi",
        )
        for _, owner in self.ctx.privilege._owner_validations:  # type: ignore[attr-defined]
            self.assertEqual("dev:dev", owner)

    def test_no_validations_outside_npm_layout(self) -> None:
        install_extensions(
            self.ctx, entries=[self.p], pi_home="/mnt/pi",
        )
        npm_prefix = "/mnt/pi/agent/npm/node_modules/"
        for path, _ in self.ctx.privilege._owner_validations:  # type: ignore[attr-defined]
            self.assertTrue(
                path.startswith(npm_prefix),
                f"validate_owner path {path!r} must be under {npm_prefix!r}",
            )

    def test_dry_run_skips_ownership_validation(self) -> None:
        install_extensions(
            self.ctx, entries=[self.p], pi_home="/mnt/pi",
            dry_run=True,
        )
        self.assertEqual(
            [], self.ctx.privilege._owner_validations,  # type: ignore[attr-defined]
        )


# ═══════════════════════════════════════════════════════════════════════
# 10.2 cont. — Dry-run contract (spec 33)
# ═══════════════════════════════════════════════════════════════════════


class TestDryRunContract(unittest.TestCase):
    """Dry-run: projection loaded + validated, metadata inspected
    read-only, planned operations reported, no mutation."""

    def setUp(self) -> None:
        self.ctx = _make_fake_context()
        self.p = ProjectionEntry(
            package="p", version="1.0.0",
            artifact_id=_VALID_ARTIFACT_ID, artifact_integrity=_VALID_SHA256,
            metadata_file="package.json",
        )

    def test_dry_run_loads_and_validates_projection(self) -> None:
        result = install_extensions(
            self.ctx, entries=[self.p], pi_home="/mnt/pi", dry_run=True,
        )
        self.assertTrue(result.dry_run)
        self.assertIsInstance(result, InstallResult)

    def test_dry_run_inspects_metadata_read_only(self) -> None:
        self.ctx.metadata._set_installed(  # type: ignore[attr-defined]
            package="p", version="1.0.0",
        )
        result = install_extensions(
            self.ctx, entries=[self.p], pi_home="/mnt/pi", dry_run=True,
        )
        self.assertGreater(self.ctx.metadata.call_count, 0)  # type: ignore[attr-defined]

    def test_dry_run_reports_planned_downloads(self) -> None:
        result = install_extensions(
            self.ctx, entries=[self.p], pi_home="/mnt/pi", dry_run=True,
        )
        self.assertTrue(result.dry_run)

    def test_dry_run_no_network(self) -> None:
        install_extensions(
            self.ctx, entries=[self.p], pi_home="/mnt/pi", dry_run=True,
        )
        self.assertEqual(0, self.ctx.blob_reader.call_count)  # type: ignore[attr-defined]

    def test_dry_run_no_package_execution(self) -> None:
        install_extensions(
            self.ctx, entries=[self.p], pi_home="/mnt/pi", dry_run=True,
        )
        self.assertEqual(0, self.ctx.installer.call_count)  # type: ignore[attr-defined]

    def test_dry_run_no_filesystem_mutation(self) -> None:
        install_extensions(
            self.ctx, entries=[self.p], pi_home="/mnt/pi", dry_run=True,
        )
        self.assertEqual(0, self.ctx.installer.call_count)  # type: ignore[attr-defined]

    def test_dry_run_absent_package_no_ownership_check(self) -> None:
        """When the package is not installed, ownership is never checked."""
        install_extensions(
            self.ctx, entries=[self.p], pi_home="/mnt/pi", dry_run=True,
        )
        self.assertEqual(
            [], self.ctx.privilege._owner_validations,  # type: ignore[attr-defined]
        )

    def test_dry_run_matching_package_validates_ownership(self) -> None:
        """Dry-run with a matching installed package MUST validate ownership."""
        self.ctx.metadata._set_installed(  # type: ignore[attr-defined]
            package="p", version="1.0.0",
        )
        result = install_extensions(
            self.ctx, entries=[self.p], pi_home="/mnt/pi", dry_run=True,
        )
        self.assertEqual(InstallStatus.ALREADY_INSTALLED, result.results[0].status)
        self.assertIn(
            "privilege.validate_owner",
            self.ctx.privilege._call_log,  # type: ignore[attr-defined]
            "dry-run must validate ownership for ALREADY_INSTALLED",
        )

    def test_dry_run_absent_package_reports_planned(self) -> None:
        """Package not installed → PLANNED, not ALREADY_INSTALLED."""
        result = install_extensions(
            self.ctx, entries=[self.p], pi_home="/mnt/pi", dry_run=True,
        )
        self.assertTrue(result.dry_run)
        self.assertEqual(InstallStatus.PLANNED, result.results[0].status)

    def test_dry_run_mismatched_version_reports_planned(self) -> None:
        """Installed version differs from projected → PLANNED (reinstall)."""
        self.ctx.metadata._set_installed(  # type: ignore[attr-defined]
            package="p", version="0.9.0",
        )
        result = install_extensions(
            self.ctx, entries=[self.p], pi_home="/mnt/pi", dry_run=True,
        )
        self.assertEqual(InstallStatus.PLANNED, result.results[0].status)

    def test_dry_run_malformed_metadata_surfaces_error(self) -> None:
        """Malformed installed metadata → raise, do not swallow."""
        self.ctx.metadata._malformed = True  # type: ignore[attr-defined]
        with self.assertRaises(InstallError):
            install_extensions(
                self.ctx, entries=[self.p], pi_home="/mnt/pi",
                dry_run=True,
            )

    def test_dry_run_permission_failure_surfaces_error(self) -> None:
        """Read permission failure → raise, do not swallow."""
        self.ctx.metadata._fail_on_next_read(  # type: ignore[attr-defined]
            InstallError("EACCES: permission denied"),
        )
        with self.assertRaises(InstallError):
            install_extensions(
                self.ctx, entries=[self.p], pi_home="/mnt/pi",
                dry_run=True,
            )


# ═══════════════════════════════════════════════════════════════════════
# 10.2 cont. — Source contract (spec 34)
# ═══════════════════════════════════════════════════════════════════════


class TestSourceContract(unittest.TestCase):
    """Installer references only the runtime projection; never reads
    reviewed inventory, build projection, update providers, or
    override policy."""

    def test_install_extensions_receives_only_projection_entries(self) -> None:
        sig = inspect.signature(install_extensions)
        param_names = set(sig.parameters.keys())
        allowed = {"ctx", "entries", "pi_home", "dry_run"}
        self.assertEqual(
            param_names - {"args", "kwargs"},
            allowed,
            "install_extensions must only accept ctx, entries, pi_home, dry_run",
        )

    def test_read_projection_only_needs_path(self) -> None:
        sig = inspect.signature(read_projection)
        param_names = set(sig.parameters.keys())
        self.assertEqual(
            {"path"},
            param_names - {"args", "kwargs"},
            "read_projection must only accept a path",
        )

    def test_module_does_not_import_inventory(self) -> None:
        import ast
        mod_path = os.path.join(
            os.path.dirname(__file__), "..", "docker",
            "runtime_installer.py",
        )
        with open(mod_path) as fh:
            tree = ast.parse(fh.read())
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.add(node.module)
        forbidden = {
            "docker.versioning.inventory",
            "docker.versioning.effective",
            "docker.versioning.updates",
            "docker.versioning.model",
            "docker.versions",
        }
        intersection = imports & forbidden
        self.assertEqual(
            set(), intersection,
            f"runtime_installer must not import {sorted(intersection)}",
        )
        # The shared semver module is the only allowed versioning
        # import — it is a focused, dependency-free validator
        # used by both model and installer.
        for mod in ("docker.versioning.semver",):
            self.assertIn(
                mod, imports,
                f"runtime_installer must import the shared {mod} validator",
            )

    def test_installer_never_receives_inventory_path(self) -> None:
        fields = {f.name for f in ProjectionEntry.__dataclass_fields__.values()}
        self.assertNotIn("inventory_path", fields)
        self.assertNotIn("build_projection_path", fields)
        self.assertNotIn("source", fields)
        self.assertNotIn("update", fields)
        self.assertNotIn("override", fields)

    def test_entrypoint_invokes_protected_installer_not_wrapper(self) -> None:
        entrypoint = os.path.join(
            os.path.dirname(__file__), "..", "docker", "entrypoint.sh",
        )
        with open(entrypoint) as fh:
            content = fh.read()
        self.assertIn(
            "python3 -m docker.runtime_installer install", content,
            "entrypoint must invoke the protected Python installer, "
            "not a legacy shell wrapper",
        )

    def test_dockerfile_copies_installer_module_not_wrapper(self) -> None:
        dockerfile = os.path.join(
            os.path.dirname(__file__), "..", "Dockerfile",
        )
        with open(dockerfile) as fh:
            content = fh.read()
        self.assertIn(
            "COPY docker/runtime_installer.py", content,
            "Dockerfile must copy the protected Python installer",
        )
        self.assertNotIn(
            "COPY docker/install-pi-extensions.sh", content,
            "Dockerfile must NOT copy the legacy shell wrapper",
        )

    def test_dockerfile_entrypoint_is_sole_startup_path(self) -> None:
        dockerfile = os.path.join(
            os.path.dirname(__file__), "..", "Dockerfile",
        )
        with open(dockerfile) as fh:
            content = fh.read()
        self.assertIn(
            'ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]',
            content,
            "Dockerfile must define the entrypoint as the sole startup path",
        )
        # The entrypoint is the only supported runtime extension-installation
        # path; no other scripts should be copied as startup surface.
        self.assertNotIn(
            "COPY docker/install-pi-extensions.sh", content,
            "Dockerfile must NOT copy the obsolete shell wrapper",
        )


# ═══════════════════════════════════════════════════════════════════════
# 10.2 — Exit codes
# ═══════════════════════════════════════════════════════════════════════


class TestExitCodeMapping(unittest.TestCase):
    """exit_code_for maps structured outcomes to shell exit codes."""

    def test_ok_to_zero(self) -> None:
        result = InstallResult(results=(
            ExtensionResult(package="p", version="1", status=InstallStatus.OK),
        ))
        self.assertEqual(0, exit_code_for(result))

    def test_already_installed_to_zero(self) -> None:
        result = InstallResult(results=(
            ExtensionResult(
                package="p", version="1",
                status=InstallStatus.ALREADY_INSTALLED,
            ),
        ))
        self.assertEqual(0, exit_code_for(result))

    def test_failed_to_nonzero(self) -> None:
        result = InstallResult(results=(
            ExtensionResult(
                package="p", version="1",
                status=InstallStatus.FAILED, detail="boom",
            ),
        ))
        self.assertNotEqual(0, exit_code_for(result))

    def test_install_error_to_nonzero(self) -> None:
        self.assertNotEqual(0, exit_code_for(InstallError("mount")))


# ═══════════════════════════════════════════════════════════════════════
# Fake boundary implementations
# ═══════════════════════════════════════════════════════════════════════



def _pkg_url(package: str, version: str) -> str:
    """Valid npm tarball URL for the given package and version.

    Npm tarball filenames never include ``+build`` metadata — the
    model strips it.  This helper mirrors that contract."""
    basename = package.split("/")[-1]
    base_ver = version.split("+", 1)[0]
    return f"https://registry.npmjs.org/{package}/-/{basename}-{base_ver}.tgz"


_dummy_bytes = b"a"  # matches _VALID_SHA256


class _CallRecorder:
    """Mixin that records every method call in ``_call_log``."""

    def __init__(self) -> None:
        self._call_log: list[str] = []


class _FakeMountChecker(_CallRecorder, MountChecker):
    def __init__(self) -> None:
        _CallRecorder.__init__(self)
        self._is_mount = True
        self._exists = True
        self._is_symlink = False
        self._metadata_outside = False
        self._writable = True
        self.call_count = 0

    def is_mount(self, path: str) -> bool:
        self._call_log.append("mount_check")
        self.call_count += 1
        if not self._exists:
            raise InstallError(f"{path}: does not exist")
        if self._is_symlink:
            raise InstallError(f"{path}: is a symlink")
        if not self._writable:
            raise InstallError(f"{path}: not writable")
        return self._is_mount

    def _set_mount(self, value: bool) -> None:
        self._is_mount = value

    def _set_exists(self, value: bool) -> None:
        self._exists = value

    def _set_is_symlink(self, value: bool) -> None:
        self._is_symlink = value

    def _set_metadata_outside(self, value: bool) -> None:
        self._metadata_outside = value

    def _set_writable(self, value: bool) -> None:
        self._writable = value


class _FakeArtifactDownloader(_CallRecorder):
    """Returns a local path — never verifies or returns bytes."""

    def __init__(self, fs: "_FakeArtifactFilesystem | None" = None) -> None:
        _CallRecorder.__init__(self)
        self._failure: BaseException | None = None
        self._fail_after: int | None = None
        self._fs = fs
        self.call_count = 0
        self._last_call: dict[str, object] = {}

    def fetch(self, *, url: str, dest_dir: str) -> str:
        self._call_log.append("download")
        self.call_count += 1
        self._last_call = {"url": url, "dest_dir": dest_dir}
        if self._fail_after is not None and self.call_count > self._fail_after:
            raise (self._failure or InstallError("download failed after N"))
        if self._failure:
            raise self._failure
        artifact_path = f"{dest_dir}/artifact.tgz"
        if self._fs is not None:
            self._fs._register_file(artifact_path)
        return artifact_path

    def _fail_with(self, exc: BaseException) -> None:
        """Raise *exc* on the next (or every) :meth:`fetch` call."""
        self._failure = exc

    def _fail_after_n(self, n: int) -> None:
        self._fail_after = n


class _FakeArtifactFilesystem(_CallRecorder):
    """Holds bytes in memory keyed by path; tracks removals;
    provides fake stat/realpath/absolute operations."""

    def __init__(self) -> None:
        _CallRecorder.__init__(self)
        self._bytes: dict[str, bytes] = {}
        self._default_bytes: bytes = _dummy_bytes
        self._removed: set[str] = set()
        self._fail_read: bool = False
        self._fail_remove: bool = False
        self.call_count = 0
        self._last_read_path: str | None = None
        # path → (st_mode, realpath_result)
        self._stat_map: dict[str, tuple[int, str]] = {}

    # ── new protocol methods ────────────────────────────────────

    def is_absolute(self, path: str) -> bool:
        return path.startswith("/")

    def lstat_mode(self, path: str) -> int:
        self._call_log.append("file.lstat_mode")
        try:
            return self._stat_map[path][0]
        except KeyError:
            raise FileNotFoundError(f"fake: no entry for {path!r}")

    def realpath(self, path: str) -> str:
        self._call_log.append("file.realpath")
        try:
            return self._stat_map[path][1]
        except KeyError:
            return path  # workspace_dir etc.

    def _register_file(
        self, path: str, *, realpath: str | None = None,
    ) -> None:
        """Register a regular file entry at *path*."""
        import stat
        self._stat_map[path] = (
            stat.S_IFREG | 0o644,
            realpath if realpath is not None else path,
        )

    def _register_symlink(self, path: str, *, real_target: str) -> None:
        """Register a symlink entry at *path*."""
        import stat
        self._stat_map[path] = (
            stat.S_IFLNK | 0o777,
            real_target,
        )

    # ── existing methods ────────────────────────────────────────

    def read_bytes(self, path: str) -> bytes:
        self._call_log.append("file.read_bytes")
        self.call_count += 1
        self._last_read_path = path
        if self._fail_read:
            raise InstallError(f"cannot read {path}")
        return self._bytes.get(path, self._default_bytes)

    def remove(self, path: str) -> None:
        self._call_log.append("file.remove")
        if self._fail_remove:
            raise InstallError(f"cannot remove {path}")
        self._removed.add(path)

    def _set_bytes(self, value: bytes) -> None:
        """Bytes returned by read_bytes for any path (via _default_bytes)."""
        self._default_bytes = value


class _FakeTempWorkspace(_CallRecorder):
    """Creates unique workspace paths and tracks cleanups."""

    def __init__(self) -> None:
        _CallRecorder.__init__(self)
        self._counter = 0
        self._created: list[str] = []
        self._cleaned: set[str] = set()
        self._fail_create: bool = False
        call_count = 0  # noqa: F841

    def create(self) -> str:
        self._call_log.append("workspace.create")
        if self._fail_create:
            raise InstallError("cannot create temp workspace")
        self._counter += 1
        path = f"/tmp/pi-install-{self._counter:04d}"
        self._created.append(path)
        return path

    def cleanup(self, path: str) -> None:
        self._call_log.append("workspace.cleanup")
        self._cleaned.add(path)


class _FakePackageInstaller(_CallRecorder, PackageInstaller):
    """Receives only verified artifact paths — never constructs URLs."""

    def __init__(self, metadata: "_FakeMetadataReader | None" = None) -> None:
        _CallRecorder.__init__(self)
        self._failure: BaseException | None = None
        self._metadata = metadata
        self._expected_versions: dict[str, str] = {}
        self.call_count = 0
        self._last_call: dict[str, object] = {}

    def install(self, *, package: str, artifact_bytes: bytes) -> None:
        self._call_log.append("install")
        self.call_count += 1
        self._last_call = {
            "package": package,
            "artifact_bytes": artifact_bytes,
            "artifact_bytes_len": len(artifact_bytes),
        }
        if self._failure:
            raise self._failure
        if self._metadata is not None and package not in self._metadata._installed:
            # Simulate fresh install: metadata becomes readable.
            version = self._expected_versions.get(package, "1.0.0")
            self._metadata._installed[package] = {"name": package, "version": version}

    def _fail_with(self, exc: InstallError) -> None:
        self._failure = exc

    def _expect_version(self, package: str, version: str) -> None:
        self._expected_versions[package] = version


class _FakeMetadataReader(_CallRecorder, MetadataReader):
    def __init__(self) -> None:
        _CallRecorder.__init__(self)
        self._installed: dict[str, dict[str, object]] = {}
        self._next_read_failure: InstallError | None = None
        self._keep_failing: bool = False
        self._spoof_name: str | None = None
        self._malformed = False
        self._is_directory = False
        self.call_count = 0

    def read(self, *, pi_home: str, metadata_file: str, package: str) -> dict[str, object]:
        self._call_log.append("metadata.read")
        self.call_count += 1
        self._last_read: dict[str, str] = {
            "pi_home": pi_home,
            "metadata_file": metadata_file,
            "package": package,
        }
        if self._next_read_failure:
            exc = self._next_read_failure
            if not self._keep_failing:
                self._next_read_failure = None
            raise exc
        if self._malformed:
            raise InstallError(f"{package}: malformed package.json")
        if self._is_directory:
            raise InstallError(
                f"{package}: metadata path is a directory",
            )
        if package in self._installed:
            result = dict(self._installed[package])
            if self._spoof_name is not None:
                result["name"] = self._spoof_name
            return result
        raise MetadataNotFoundError(f"{package}: not installed")

    def _set_installed(self, *, package: str, version: str) -> None:
        self._installed[package] = {"name": package, "version": version}

    def _fail_on_next_read(self, exc: InstallError) -> None:
        self._next_read_failure = exc

    def _fail_on_all_reads(self, exc: InstallError) -> None:
        """Repeatedly fail every read until cleared."""
        self._next_read_failure = exc
        self._keep_failing = True

    def _set_spoof_name(self, name: str) -> None:
        """Make reads for ANY package return this name (simulates
        a corrupt package.json with the wrong identity)."""
        self._spoof_name = name

    def _set_malformed(self, value: bool) -> None:
        self._malformed = value

    def _set_is_directory(self, value: bool) -> None:
        self._is_directory = value


class _FakePrivilegeContext(_CallRecorder, PrivilegeContext):
    def __init__(self) -> None:
        _CallRecorder.__init__(self)
        self._verified_user: str | None = None
        self._fail_verify: bool = False
        self._owner_validations: list[tuple[str, str]] = []
        self._fail_validate: bool = False

    def verify_user(self, expected: str) -> None:
        self._call_log.append("privilege.verify_user")
        if self._fail_verify:
            raise InstallError(f"not running as {expected}")
        self._verified_user = expected

    def validate_owner(self, path: str, owner: str) -> None:
        self._call_log.append("privilege.validate_owner")
        if self._fail_validate:
            raise InstallError(f"ownership validation failed: expected {owner} for {path}")
        self._owner_validations.append((path, owner))

    call_count = 0  # unused; _CallRecorder._call_log covers it


class _FakeInstallContext(InstallContext):
    """InstallContext whose fakes expose internal state for assertions."""

    def __init__(
        self,
        mount_check: _FakeMountChecker,
        workspace: _FakeTempWorkspace,
        download: _FakeArtifactDownloader,
        file: _FakeArtifactFilesystem,
        installer: _FakePackageInstaller,
        metadata: _FakeMetadataReader,
        privilege: _FakePrivilegeContext,
        blob_reader: _FakeMountedBlobReader | None = None,
    ) -> None:
        object.__setattr__(self, "mount_check", mount_check)
        object.__setattr__(self, "workspace", workspace)
        object.__setattr__(self, "download", download)
        object.__setattr__(self, "file", file)
        object.__setattr__(self, "installer", installer)
        object.__setattr__(self, "metadata", metadata)
        object.__setattr__(self, "privilege", privilege)
        object.__setattr__(
            self, "blob_reader",
            blob_reader or _FakeMountedBlobReader(),
        )
        self._call_log: list[str] = []


def _make_fake_context() -> _FakeInstallContext:
    meta = _FakeMetadataReader()
    fs = _FakeArtifactFilesystem()
    ctx = _FakeInstallContext(
        mount_check=_FakeMountChecker(),
        workspace=_FakeTempWorkspace(),
        download=_FakeArtifactDownloader(fs=fs),
        file=fs,
        installer=_FakePackageInstaller(metadata=meta),
        metadata=meta,
        privilege=_FakePrivilegeContext(),
    )
    # Share single call log across all fakes and the context.
    shared = ctx._call_log
    for attr in ("mount_check", "workspace", "download", "file",
                 "installer", "metadata", "privilege"):
        obj = getattr(ctx, attr)
        if hasattr(obj, "_call_log"):
            obj._call_log = shared
    return ctx


def _tmp_toml(content: str) -> str:
    import tempfile
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".toml", delete=False,
    )
    try:
        tmp.write(content)
    finally:
        tmp.close()
    return tmp.name


# ═══════════════════════════════════════════════════════════════════════
# 10.3 — Interruption coverage: KeyboardInterrupt / SystemExit
# ═══════════════════════════════════════════════════════════════════════


class TestRealPackageInstallerTarballExtraction(unittest.TestCase):
    """The real installer extracts npm tarball bytes directly
    into node_modules via :mod:`tarfile` — no subprocess, no
    memfd, no mutable staging."""

    def setUp(self) -> None:
        import tempfile
        self._installer = InstallContext.real_installer()
        self._tmp = tempfile.mkdtemp(prefix="test-npm-install-")
        # Point _FIXED_PI_HOME at a temp so extraction is isolated.
        self._home_patch = mock.patch.object(
            docker.runtime_installer,
            "_FIXED_PI_HOME",
            self._tmp,
        )
        self._home_patch.start()

    def tearDown(self) -> None:
        self._home_patch.stop()
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    @staticmethod
    def _make_tgz(files: dict[str, bytes]) -> bytes:
        """Build a gzipped npm-style tarball rooted at ``package/``."""
        import io
        import tarfile
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for name, content in files.items():
                info = tarfile.TarInfo(f"package/{name}")
                info.size = len(content)
                tf.addfile(info, io.BytesIO(content))
        return buf.getvalue()

    def test_extracts_package_json_to_node_modules(self) -> None:
        tgz = self._make_tgz({
            "package.json": b'{"name":"pkg","version":"1.0.0"}',
        })
        self._installer.install("@scope/pkg", tgz)
        target = os.path.join(
            self._tmp, "agent", "npm", "node_modules",
            "@scope", "pkg", "package.json",
        )
        self.assertTrue(os.path.isfile(target),
                        f"expected {target} to exist after extraction")

    def test_strips_npm_package_prefix(self) -> None:
        tgz = self._make_tgz({"index.js": b"module.exports=1;"})
        self._installer.install("plain-pkg", tgz)
        target = os.path.join(
            self._tmp, "agent", "npm", "node_modules",
            "plain-pkg", "index.js",
        )
        self.assertTrue(os.path.isfile(target),
                        f"expected {target} to exist, package/ prefix "
                        "must be stripped")

    def test_empty_tarball_succeeds(self) -> None:
        self._installer.install("empty", self._make_tgz({}))
        target = os.path.join(
            self._tmp, "agent", "npm", "node_modules", "empty",
        )
        self.assertTrue(os.path.isdir(target))

    def test_corrupt_tarball_raises_install_error(self) -> None:
        with self.assertRaises(InstallError):
            self._installer.install("bad", b"not a tarball")

    def test_pi_usage_resolves_tui_kit_and_highlight_js(self) -> None:
        """The real pi-tui-kit entry path resolves its reviewed dependency."""
        import subprocess

        highlight = self._make_tgz({
            "package.json": (
                b'{"name":"highlight.js","version":"10.7.3",'
                b'"main":"./index.js"}'
            ),
            "index.js": b'module.exports = {marker: "highlight-js-resolved"};',
        })
        kit = self._make_tgz({
            "package.json": (
                b'{"name":"@narumitw/pi-tui-kit","version":"0.49.1",'
                b'"type":"module","exports":"./dist/index.js",'
                b'"dependencies":{"highlight.js":"10.7.3"}}'
            ),
            "dist/index.js": (
                b'export { syntaxMarker as marker } from '
                b'"./components/syntax-highlighting.js";'
            ),
            "dist/components/syntax-highlighting.js": (
                b'import hljs from "highlight.js"; '
                b'export const syntaxMarker = hljs.marker;'
            ),
        })
        usage = self._make_tgz({
            "package.json": (
                b'{"name":"@narumitw/pi-usage","version":"0.52.3",'
                b'"type":"module","exports":"./index.js",'
                b'"dependencies":{"@narumitw/pi-tui-kit":"^0.49.1"}}'
            ),
            "index.js": (
                b'export async function usage() {'
                b' const kit = await import("@narumitw/pi-tui-kit");'
                b' return kit.marker; }'
            ),
        })

        self._installer.install("highlight.js", highlight)
        self._installer.install("@narumitw/pi-tui-kit", kit)
        self._installer.install("@narumitw/pi-usage", usage)
        usage_entry = os.path.join(
            self._tmp, "agent", "npm", "node_modules",
            "@narumitw", "pi-usage", "index.js",
        )
        script = (
            "import(process.argv[1]).then(async m => {"
            " const value = await m.usage();"
            " if (value !== 'highlight-js-resolved') process.exit(2);"
            "})"
        )
        completed = subprocess.run(
            ["node", "-e", script, usage_entry],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode, 0,
            completed.stderr or completed.stdout,
        )


class TestArchitectureHostURLBoundary(unittest.TestCase):
    """RED — runtime URLs must be host-only; the container projection and
    installer must not expose or depend on downloadable URLs or download-
    transport/workspace production boundaries."""

    def test_projection_dto_has_no_url_field(self) -> None:
        from docker.runtime_installer import ProjectionEntry
        annotations = getattr(ProjectionEntry, "__annotations__", {})
        self.assertNotIn(
            "artifact_url", annotations,
            "ProjectionEntry must not expose artifact_url; URLs are host-only",
        )

    def test_projection_dto_has_canonical_identity_field(self) -> None:
        from docker.runtime_installer import ProjectionEntry
        annotations = getattr(ProjectionEntry, "__annotations__", {})
        self.assertIn(
            "artifact_id", annotations,
            "ProjectionEntry must carry a canonical artifact identity "
            "(not a downloadable URL)",
        )

    def test_installer_source_has_no_download_transport_imports(self) -> None:
        """The container installer source must never import urllib,
        requests, http.client, socket, or curl — whether top-level
        or inside a function body.

        Checking only ``vars(module)`` misses ``from X import Y``
        inside a function (the name is local, not module-level).
        This test scans the actual source text for import
        statements that reference forbidden download-transport
        modules."""
        import re

        import docker.runtime_installer
        src = inspect.getsource(docker.runtime_installer)
        forbidden = re.compile(
            r"^\s*(?:from|import)\s+"
            r"(?:urllib|requests|http\.client|socket|curl)"
            r"(?:\b|\s)",
            re.MULTILINE,
        )
        violations = [
            m.group(0).strip()
            for m in forbidden.finditer(src)
        ]
        self.assertEqual(
            [], violations,
            "installer source imports forbidden download transport:\n"
            + "\n".join(f"  {v}" for v in violations),
        )

    def test_installer_has_no_artifact_downloader_boundary(self) -> None:
        import docker.runtime_installer as mod
        self.assertFalse(
            hasattr(mod, "ArtifactDownloader"),
            "ArtifactDownloader protocol must not exist — the container "
            "receives pre-materialized read-only mounts",
        )

    def test_installer_has_no_temp_workspace_boundary(self) -> None:
        import docker.runtime_installer as mod
        self.assertFalse(
            hasattr(mod, "TempWorkspace"),
            "TempWorkspace must not exist — no mutable download workspace "
            "inside the container",
        )

    def test_installer_has_no_download_factory_function(self) -> None:
        import docker.runtime_installer as mod
        self.assertFalse(
            hasattr(mod, "real_workspace"),
            "real_workspace factory must not exist in the production module",
        )
        self.assertFalse(
            hasattr(mod, "real_download"),
            "real_download factory must not exist in the production module",
        )


# ═══════════════════════════════════════════════════════════════════════
# Mounted-artifact installer contract (failing tests)
# ═══════════════════════════════════════════════════════════════════════

# ── MountedBlobReader protocol imported from production ─────────


# ── Fake MountedBlobReader ────────────────────────────────────────

class _FakeMountedBlobReader(_CallRecorder, MountedBlobReader):
    """Configurable fake that enforces identity agreement and
    returns verified bytes (or raises :class:`InstallError` to
    simulate blob-level failures).

    The fake delegates path derivation to the production
    :func:`_mounted_artifact_path` — the root is not injectable.

    By default the reader validates that *artifact_id* equals the
    canonical derivation from *integrity* — a mismatched ID raises
    :class:`ProjectionError`.  Call :meth:`_disable_id_check` to
    suppress this (for tests that need to simulate a corrupt
    projection).
    """

    def __init__(self) -> None:
        _CallRecorder.__init__(self)
        self._bytes: bytes = _dummy_bytes
        self._fail_with: InstallError | None = None
        self._calls: list[dict[str, object]] = []
        self._validate_id: bool = True
        self.call_count = 0

    def open_verified(
        self, *, artifact_id: str, integrity: str,
    ) -> bytes:
        self._call_log.append("blob_reader.open_verified")
        self.call_count += 1

        # ── identity agreement ───────────────────────────────
        if self._validate_id:
            expected_id = _derived_artifact_id(integrity)
            if artifact_id != expected_id:
                raise ProjectionError(
                    f"artifact_id {artifact_id!r} does not match "
                    f"integrity {integrity!r} (expected "
                    f"{expected_id!r})",
                )

        self._calls.append({
            "artifact_id": artifact_id,
            "integrity": integrity,
        })
        if self._fail_with is not None:
            raise self._fail_with
        return self._bytes

    def _set_bytes(self, value: bytes) -> None:
        self._bytes = value

    def _set_failure(self, exc: InstallError) -> None:
        self._fail_with = exc

    def _disable_id_check(self) -> None:
        """Allow id/integrity mismatch to proceed (for tests
        that simulate a corrupt projection)."""
        self._validate_id = False


# ── InstallContext wired with MountedBlobReader ───────────────────

class _MountedInstallContext(InstallContext):
    """InstallContext that carries :class:`MountedBlobReader`
    instead of download / workspace boundaries."""

    def __init__(
        self,
        mount_check: _FakeMountChecker,
        blob_reader: _FakeMountedBlobReader,
        installer: _FakePackageInstaller,
        metadata: _FakeMetadataReader,
        privilege: _FakePrivilegeContext,
    ) -> None:
        object.__setattr__(self, "mount_check", mount_check)
        object.__setattr__(self, "blob_reader", blob_reader)
        object.__setattr__(self, "installer", installer)
        object.__setattr__(self, "metadata", metadata)
        object.__setattr__(self, "privilege", privilege)
        self._call_log: list[str] = []


def _make_mounted_context() -> _MountedInstallContext:
    meta = _FakeMetadataReader()
    ctx = _MountedInstallContext(
        mount_check=_FakeMountChecker(),
        blob_reader=_FakeMountedBlobReader(),
        installer=_FakePackageInstaller(metadata=meta),
        metadata=meta,
        privilege=_FakePrivilegeContext(),
    )
    shared = ctx._call_log
    for attr in ("mount_check", "blob_reader", "installer",
                 "metadata", "privilege"):
        obj = getattr(ctx, attr)
        if hasattr(obj, "_call_log"):
            obj._call_log = shared
    return ctx


def _derived_artifact_id(integrity: str) -> str:
    """Derive the canonical ``<algo>/<digest>.tgz`` path from a
    ``shaNNN-<base64>`` integrity string."""
    import base64
    algo, b64 = integrity.split("-", 1)
    safe = b64.replace("+", "-").replace("/", "_")
    return f"{algo}/{safe}.tgz"


# ── Test orchestrator: calls install_extensions after faking out ─
#    the download path into a mounted-blob-read path.                ─

class _MountedInstallTestBase:
    """Shared setup for tests that exercise the installer through
    a mounted-artifact reader rather than a downloader."""

    ctx: _MountedInstallContext

    def setUp(self) -> None:  # type: ignore[override]
        self.ctx = _make_mounted_context()

    def _integ_for(self, content: bytes,
                   *, algo: str = "sha256") -> str:
        h = hashlib.new(algo, content)
        return f"{algo}-" + base64.b64encode(h.digest()).decode("ascii")

    def _install(
        self,
        *,
        entries: list[dict[str, str]] | None = None,
        pi_home: str = "/mnt/pi",
        dry_run: bool = False,
    ) -> InstallResult:
        """Build :class:`ProjectionEntry` instances (with the
        *artifact_id* / *artifact_integrity* fields required by
        the new DTO), wire them into :func:`install_extensions`,
        and return the result.

        This function will raise :exc:`TypeError` until
        :class:`ProjectionEntry` drops *artifact_url* and gains
        *artifact_id* (task 7.2).
        """
        proj_entries = [
            ProjectionEntry(
                package=e["package"],
                version=e["version"],
                artifact_id=e["artifact_id"],
                artifact_integrity=e["integrity"],
                metadata_file=e.get("metadata_file", "package.json"),
            )
            for e in (entries or [])
        ]
        return install_extensions(
            self.ctx, entries=proj_entries, pi_home=pi_home,
            dry_run=dry_run,  # type: ignore[call-arg]
        )

    def _entry(self, *,
               package: str = "p",
               version: str = "1.0.0",
               content: bytes = _dummy_bytes,
               ) -> dict[str, str]:
        return {
            "package": package,
            "version": version,
            "artifact_id": _derived_artifact_id(
                self._integ_for(content),
            ),
            "integrity": self._integ_for(content),
            "metadata_file": "package.json",
        }


# ═══════════════════════════════════════════════════════════════════════
# Mounted-blob validation: the reader (not the OS) rejects
#        symlinks, dirs, writable files, and empty / corrupt blobs
# ═══════════════════════════════════════════════════════════════════════

class TestMountedBlobValidation(_MountedInstallTestBase, unittest.TestCase):
    """The installer must receive its verified bytes through
    :class:`MountedBlobReader`.  When the reader raises
    :class:`InstallError`, the installer must surface that failure
    and must NOT call :class:`PackageInstaller.install`."""

    # ── valid blob ────────────────────────────────────────────

    def test_valid_blob_proceeds_to_install(self) -> None:
        content = b"good blob\n"
        integ = self._integ_for(content)
        self.ctx.blob_reader._set_bytes(content)  # type: ignore[attr-defined]
        result = self._install(entries=[{
            "package": "p", "version": "1.0.0",
            "artifact_id": _derived_artifact_id(integ),
            "integrity": integ,
        }])
        self.assertEqual(1, len(result.results))
        self.assertEqual(
            InstallStatus.OK,
            result.results[0].status,
            "valid blob must result in INSTALLED",
        )
        # Package installer was called with verified bytes.
        last = self.ctx.installer._last_call  # type: ignore[attr-defined]
        self.assertEqual("p", last.get("package"))
        self.assertIn("artifact_bytes_len", last)

    # ── symlink rejection ─────────────────────────────────────

    def test_symlinked_blob_rejected(self) -> None:
        self.ctx.blob_reader._set_failure(  # type: ignore[attr-defined]
            InstallError("artifact is a symlink"),
        )
        result = self._install(entries=[self._entry()])
        self.assertEqual(
            InstallStatus.FAILED,
            result.results[0].status,
        )
        self.assertNotIn(
            "install", self.ctx._call_log,
            "PackageInstaller.install must not be called "
            "when the blob reader rejects a symlink",
        )

    # ── non-regular file rejection ────────────────────────────

    def test_non_regular_blob_rejected(self) -> None:
        self.ctx.blob_reader._set_failure(  # type: ignore[attr-defined]
            InstallError("artifact is not a regular file"),
        )
        result = self._install(entries=[self._entry()])
        self.assertEqual(
            InstallStatus.FAILED,
            result.results[0].status,
        )
        self.assertNotIn(
            "install", self.ctx._call_log,
            "directory / FIFO / device must not reach install",
        )

    # ── overly-permissive rejection ───────────────────────────

    def test_writable_blob_rejected(self) -> None:
        self.ctx.blob_reader._set_failure(  # type: ignore[attr-defined]
            InstallError("artifact has group/world write bits"),
        )
        result = self._install(entries=[self._entry()])
        self.assertEqual(
            InstallStatus.FAILED,
            result.results[0].status,
        )
        self.assertNotIn("install", self.ctx._call_log)

    # ── empty blob rejection ──────────────────────────────────

    def test_empty_blob_rejected(self) -> None:
        self.ctx.blob_reader._set_failure(  # type: ignore[attr-defined]
            InstallError("artifact is empty"),
        )
        result = self._install(entries=[self._entry()])
        self.assertEqual(
            InstallStatus.FAILED,
            result.results[0].status,
        )
        self.assertNotIn("install", self.ctx._call_log)

    # ── integrity mismatch ────────────────────────────────────

    def test_corrupt_bytes_rejected(self) -> None:
        self.ctx.blob_reader._set_failure(  # type: ignore[attr-defined]
            IntegrityError(
                "digest mismatch",
                algorithm="sha256",
                expected="deadbeef", actual="cafebabe",
            ),
        )
        result = self._install(entries=[self._entry()])
        self.assertEqual(
            InstallStatus.FAILED,
            result.results[0].status,
        )
        self.assertNotIn(
            "install", self.ctx._call_log,
            "integrity mismatch must abort before package execution",
        )

    # ── missing blob ──────────────────────────────────────────

    def test_missing_blob_rejected(self) -> None:
        self.ctx.blob_reader._set_failure(  # type: ignore[attr-defined]
            InstallError("no blob found for artifact"),
        )
        result = self._install(entries=[self._entry()])
        self.assertEqual(
            InstallStatus.FAILED,
            result.results[0].status,
        )
        self.assertNotIn("install", self.ctx._call_log)

    # ── missing algorithm directory ───────────────────────────

    def test_missing_algorithm_dir_rejected(self) -> None:
        self.ctx.blob_reader._set_failure(  # type: ignore[attr-defined]
            InstallError("algorithm directory sha512/ not found"),
        )
        result = self._install(entries=[self._entry()])
        self.assertEqual(
            InstallStatus.FAILED,
            result.results[0].status,
        )
        self.assertNotIn("install", self.ctx._call_log)

    # ── failure detail is surfaced ────────────────────────────

    def test_failure_detail_surfaces_reader_message(self) -> None:
        msg = "blob at sha512/abc.tgz is a symlink"
        self.ctx.blob_reader._set_failure(  # type: ignore[attr-defined]
            InstallError(msg),
        )
        result = self._install(entries=[self._entry()])
        self.assertIsNotNone(result.results[0].detail)
        self.assertIn(msg, result.results[0].detail or "")


# ═══════════════════════════════════════════════════════════════════════
# Exact-byte verification ordering
# ═══════════════════════════════════════════════════════════════════════

class TestMountedArtifactByteVerificationOrder(
    _MountedInstallTestBase, unittest.TestCase,
):
    """Integrity verification must happen through a single open
    descriptor — open → hash → compare — before any bytes reach
    :class:`PackageInstaller.install`."""

    def test_blob_opened_before_install(self) -> None:
        """The blob reader's ``open_verified`` call must appear
        in the call log before ``install``."""
        content = b"ordered byte stream\n"
        self.ctx.blob_reader._set_bytes(content)  # type: ignore[attr-defined]
        integ = self._integ_for(content)
        self._install(entries=[{
            "package": "p", "version": "1.0.0",
            "artifact_id": _derived_artifact_id(integ),
            "integrity": integ,
        }])
        log = self.ctx._call_log
        open_idx = log.index("blob_reader.open_verified")
        inst_idx = log.index("install")
        self.assertLess(
            open_idx, inst_idx,
            "open_verified must precede install",
        )

    def test_verified_bytes_not_fetched_twice(self) -> None:
        """When the blob reader succeeds, the installer must
        reuse the exact verified bytes — not call the reader
        again."""
        content = b"one read\n"
        self.ctx.blob_reader._set_bytes(content)  # type: ignore[attr-defined]
        integ = self._integ_for(content)
        self._install(entries=[{
            "package": "p", "version": "1.0.0",
            "artifact_id": _derived_artifact_id(integ),
            "integrity": integ,
        }])
        self.assertEqual(
            1,
            self.ctx._call_log.count("blob_reader.open_verified"),
            "bytes must be opened and verified exactly once per extension",
        )

    def test_install_receives_exact_verified_bytes(self) -> None:
        content = b"specific verified payload\n"
        self.ctx.blob_reader._set_bytes(content)  # type: ignore[attr-defined]
        integ = self._integ_for(content)
        self._install(entries=[{
            "package": "p", "version": "1.0.0",
            "artifact_id": _derived_artifact_id(integ),
            "integrity": integ,
        }])
        last = self.ctx.installer._last_call  # type: ignore[attr-defined]
        self.assertEqual(
            content,
            last.get("artifact_bytes"),
            "install must receive the exact verified bytes — "
            "same-length different content is a TOCTOU gap",
        )


# ═══════════════════════════════════════════════════════════════════════
# Fixed-root lookup + identity/integrity agreement
# ═══════════════════════════════════════════════════════════════════════

class TestMountedArtifactIdentityAgreement(
    _MountedInstallTestBase, unittest.TestCase,
):
    """The production reader MUST use :data:`_MOUNTED_ARTIFACT_ROOT`
    as the single fixed root with no injection points.  The
    *artifact_id* MUST agree with the canonical derivation from
    *integrity* — a mismatched pair means the projection was
    tampered with."""

    # ── production path derivation ──────────────────────────

    def test_production_path_uses_fixed_root(self) -> None:
        """``_mounted_artifact_path(artifact_id)`` returns
        ``/run/pi-cli/runtime-artifacts/<artifact_id>``."""
        result = _mounted_artifact_path("sha256/abc.tgz")
        self.assertEqual(
            "/run/pi-cli/runtime-artifacts/sha256/abc.tgz",
            result,
        )

    def test_production_path_rejects_traversal_in_id(self) -> None:
        """*artifact_id* with ``..`` or absolute path MUST
        raise :class:`ProjectionError` before any join."""
        for bad_id in (
            "sha256/../../../etc/passwd.tgz",
            "../etc/passwd",
            "sha256/x/../../etc/passwd.tgz",
        ):
            with self.subTest(artifact_id=bad_id):
                with self.assertRaises(ProjectionError):
                    _mounted_artifact_path(bad_id)

    def test_production_path_rejects_absolute_id(self) -> None:
        """An absolute *artifact_id* is a path injection
        attempt — must be rejected."""
        with self.assertRaises(ProjectionError):
            _mounted_artifact_path("/etc/passwd")

    def test_production_path_rejects_empty_id(self) -> None:
        """An empty *artifact_id* must be rejected — an empty
        join produces the root directory itself."""
        with self.assertRaises(ProjectionError):
            _mounted_artifact_path("")

    # ── production reader has no root injection ──────────────

    def test_production_reader_has_no_root_parameter(self) -> None:
        """``RuntimeArtifactReader`` accepts an optional
        *mount_inspection* boundary but SHALL NOT accept any
        form of root override."""
        # mount_inspection is an injectable boundary for testing.
        reader = RuntimeArtifactReader(
            mount_inspection=_StatvfsMountInspection(),
        )
        self.assertIsInstance(reader, MountedBlobReader)
        # Passing an artifact root must still be rejected.
        with self.assertRaises(TypeError):
            RuntimeArtifactReader(artifact_root="/tmp/x")  # type: ignore[call-arg]

    def test_production_reader_has_no_root_property(self) -> None:
        """The reader exposes no attribute that could be
        patched to redirect the root at runtime."""
        reader = RuntimeArtifactReader()
        self.assertFalse(
            hasattr(reader, "_root"),
            "no private _root attribute to monkey-patch",
        )
        self.assertFalse(
            hasattr(reader, "artifact_root"),
            "no public artifact_root attribute",
        )

    def test_production_reader_not_redirectable_by_env(self) -> None:
        """The root is a module-level string literal, not read
        from an environment variable."""
        import docker.runtime_installer as _prod
        source = inspect.getsource(_prod)
        self.assertNotIn(
            "environ", source,
            "production module must not read environment "
            "variables for the artifact root",
        )
        self.assertNotIn(
            "getenv", source,
            "production module must not call getenv/os.getenv",
        )

    # ── identity agreement (through fake, exercises protocol) ─

    def test_artifact_id_agrees_with_integrity(self) -> None:
        """When artifact_id matches the canonical derivation
        from integrity, the reader proceeds to return bytes."""
        content = b"matching\n"
        self.ctx.blob_reader._set_bytes(content)  # type: ignore[attr-defined]
        integ = self._integ_for(content)
        expected_id = _derived_artifact_id(integ)
        result = self._install(entries=[{
            "package": "p", "version": "1.0.0",
            "artifact_id": expected_id,
            "integrity": integ,
        }])
        self.assertEqual(
            InstallStatus.OK, result.results[0].status,
        )
        self.assertIn(
            "install", self.ctx._call_log,
            "matching id/integrity must reach PackageInstaller",
        )

    def test_artifact_id_mismatch_rejected(self) -> None:
        """When artifact_id differs from the derivation, the
        reader raises ProjectionError — install is never called."""
        content = b"mismatch test\n"
        self.ctx.blob_reader._set_bytes(content)  # type: ignore[attr-defined]
        integ = self._integ_for(content)
        wrong_id = "sha512/nope.tgz"
        result = self._install(entries=[{
            "package": "p", "version": "1.0.0",
            "artifact_id": wrong_id,
            "integrity": integ,
        }])
        self.assertEqual(
            InstallStatus.FAILED, result.results[0].status,
            "mismatched artifact_id must be FAILED",
        )
        self.assertIn(
            "artifact_id",
            result.results[0].detail or "",
            "detail must mention the artifact_id mismatch",
        )
        self.assertNotIn(
            "install", self.ctx._call_log,
            "PackageInstaller must not receive bytes from a "
            "mismatched identity",
        )


# ═══════════════════════════════════════════════════════════════════════
# Production RuntimeArtifactReader — filesystem safety & SRI
# ═══════════════════════════════════════════════════════════════════════

class TestRuntimeArtifactReader(unittest.TestCase):
    """Direct tests against the production :class:`RuntimeArtifactReader`.

    Each test creates real filesystem fixtures inside a temporary
    directory and patches ``_MOUNTED_ARTIFACT_ROOT`` so the reader
    resolves blobs from the fixture.  The tests exercise every
    safety and correctness property required of the production
    reader — missing / symlinked / non-regular / writable / empty /
    corrupt-blob rejection, identity agreement, and successful
    verified-byte return.

    These tests SHALL NOT use the fake reader or
    :func:`install_extensions` — they call
    :meth:`RuntimeArtifactReader.open_verified` directly.
    """

    _tmp: str

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="test-runtime-artifacts-")
        mock.patch.object(
            docker.runtime_installer,
            "_MOUNTED_ARTIFACT_ROOT",
            self._tmp,
        ).start()

    def tearDown(self) -> None:
        mock.patch.stopall()
        shutil.rmtree(self._tmp, ignore_errors=True)

    # ── helpers ──────────────────────────────────────────────

    def _integ_for(self, content: bytes,
                   *, algo: str = "sha256") -> str:
        h = hashlib.new(algo, content)
        return f"{algo}-" + base64.b64encode(h.digest()).decode("ascii")

    def _write_blob(
        self,
        artifact_id: str,
        content: bytes,
        mode: int = 0o600,
    ) -> str:
        """Create a blob at ``<tmp>/<artifact_id>`` and return
        its full path."""
        path = os.path.join(self._tmp, artifact_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, mode)
        try:
            os.write(fd, content)
        finally:
            os.close(fd)
        return path

    def _make_symlink_blob(
        self, artifact_id: str, target: str,
    ) -> str:
        """Create a symlink at ``<tmp>/<artifact_id>`` pointing
        to *target*."""
        path = os.path.join(self._tmp, artifact_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.symlink(target, path)
        return path

    def _reader(self) -> RuntimeArtifactReader:
        """Return a reader that passes the mount check (read-only
        mount inspection fake) so tests exercise their specific
        property, not the mount guard."""
        return RuntimeArtifactReader(
            mount_inspection=_FakeMountInspection(read_only=True),
        )

    # ── valid blob ───────────────────────────────────────────

    def test_valid_blob_returns_verified_bytes(self) -> None:
        content = b"production verified payload\n"
        integ = self._integ_for(content)
        art_id = _derived_artifact_id(integ)
        self._write_blob(art_id, content)
        reader = self._reader()
        result = reader.open_verified(
            artifact_id=art_id, integrity=integ,
        )
        self.assertEqual(content, result)
        self.assertIsInstance(result, bytes)

    # ── missing blob ─────────────────────────────────────────

    def test_missing_blob_raises(self) -> None:
        integ = self._integ_for(b"never written")
        art_id = _derived_artifact_id(integ)
        reader = self._reader()
        with self.assertRaises(InstallError):
            reader.open_verified(
                artifact_id=art_id, integrity=integ,
            )

    # ── symlinked blob ───────────────────────────────────────

    def test_symlinked_blob_raises(self) -> None:
        content = b"real content\n"
        integ = self._integ_for(content)
        art_id = _derived_artifact_id(integ)
        real_path = self._write_blob("real.tgz", content)
        self._make_symlink_blob(art_id, real_path)
        reader = self._reader()
        with self.assertRaises(InstallError):
            reader.open_verified(
                artifact_id=art_id, integrity=integ,
            )

    # ── non-regular blob (directory) ─────────────────────────

    def test_directory_blob_raises(self) -> None:
        integ = self._integ_for(b"dir instead of file")
        art_id = _derived_artifact_id(integ)
        dir_path = os.path.join(self._tmp, art_id)
        os.makedirs(dir_path, exist_ok=True)
        reader = self._reader()
        with self.assertRaises(InstallError):
            reader.open_verified(
                artifact_id=art_id, integrity=integ,
            )

    # ── group/world-writable blob ────────────────────────────

    def test_group_writable_blob_raises(self) -> None:
        content = b"group writable\n"
        integ = self._integ_for(content)
        art_id = _derived_artifact_id(integ)
        path = self._write_blob(art_id, content)
        os.chmod(path, 0o660)  # ensure group-write survives umask
        reader = self._reader()
        with self.assertRaises(InstallError):
            reader.open_verified(
                artifact_id=art_id, integrity=integ,
            )

    def test_world_writable_blob_raises(self) -> None:
        content = b"world writable\n"
        integ = self._integ_for(content)
        art_id = _derived_artifact_id(integ)
        path = self._write_blob(art_id, content)
        os.chmod(path, 0o666)  # ensure world-write survives umask
        reader = self._reader()
        with self.assertRaises(InstallError):
            reader.open_verified(
                artifact_id=art_id, integrity=integ,
            )

    # ── empty blob ───────────────────────────────────────────

    def test_empty_blob_raises(self) -> None:
        integ = self._integ_for(b"")
        art_id = _derived_artifact_id(integ)
        self._write_blob(art_id, b"")
        reader = self._reader()
        with self.assertRaises(InstallError):
            reader.open_verified(
                artifact_id=art_id, integrity=integ,
            )

    # ── digest mismatch (corrupt blob) ───────────────────────

    def test_digest_mismatch_raises(self) -> None:
        written_content = b"what was written\n"
        claimed_content = b"what integrity claims\n"
        integ = self._integ_for(claimed_content)
        art_id = _derived_artifact_id(integ)
        self._write_blob(art_id, written_content)
        reader = self._reader()
        with self.assertRaises(InstallError):
            reader.open_verified(
                artifact_id=art_id, integrity=integ,
            )

    # ── identity/integrity mismatch ──────────────────────────

    def test_identity_mismatch_raises(self) -> None:
        content = b"matching content\n"
        integ = self._integ_for(content)
        wrong_id = "sha512/unrelated.tgz"
        self._write_blob(wrong_id, content)
        reader = self._reader()
        with self.assertRaises(ProjectionError):
            reader.open_verified(
                artifact_id=wrong_id, integrity=integ,
            )


# ── Fake MountInspection for mount-safety tests ─────────────────

class _FakeMountInspection:
    """Configurable mount-inspection boundary for testing
    mount read-only enforcement without requiring root to
    create actual bind mounts."""

    def __init__(self, *, read_only: bool) -> None:
        self._read_only = read_only
        self._calls: list[str] = []

    def is_read_only_mount(self, path: str) -> bool:
        self._calls.append(path)
        return self._read_only


# ═══════════════════════════════════════════════════════════════════════
# RuntimeArtifactReader — mount read-only enforcement
# ═══════════════════════════════════════════════════════════════════════

class TestRuntimeArtifactReaderMountSafety(unittest.TestCase):
    """Beyond permission bits: the blob MUST reside on a read-only
    filesystem mount.  A ``0o600`` regular file on a writable mount
    can still be mutated by its owner — only a read-only mount
    (e.g. a bind-mount with ``ro``) prevents post-materialization
    tampering.

    These tests inject a :class:`_FakeMountInspection` boundary
    into :class:`RuntimeArtifactReader` to control mount status
    without requiring ``mount(8)`` privileges.
    """

    _tmp: str

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="test-mount-safety-")
        mock.patch.object(
            docker.runtime_installer,
            "_MOUNTED_ARTIFACT_ROOT",
            self._tmp,
        ).start()

    def tearDown(self) -> None:
        mock.patch.stopall()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write_blob(
        self, artifact_id: str, content: bytes,
        mode: int = 0o600,
    ) -> str:
        path = os.path.join(self._tmp, artifact_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, mode)
        try:
            os.write(fd, content)
        finally:
            os.close(fd)
        return path

    def _integ_for(self, content: bytes,
                   *, algo: str = "sha256") -> str:
        h = hashlib.new(algo, content)
        return f"{algo}-" + base64.b64encode(h.digest()).decode("ascii")

    # ── writable mount is rejected ───────────────────────────

    def test_writable_mount_raises(self) -> None:
        """A regular, non-symlinked, ``0o600`` blob with a
        matching digest MUST still be rejected if the filesystem
        mount is writable — permission bits alone do not prevent
        owner mutation."""
        content = b"valid blob on writable mount\n"
        integ = self._integ_for(content)
        art_id = _derived_artifact_id(integ)
        self._write_blob(art_id, content)
        reader = RuntimeArtifactReader(
            mount_inspection=_FakeMountInspection(read_only=False),
        )
        with self.assertRaises(InstallError):
            reader.open_verified(
                artifact_id=art_id, integrity=integ,
            )

    # ── read-only mount proceeds ─────────────────────────────

    def test_read_only_mount_returns_bytes(self) -> None:
        """A regular, non-symlinked, ``0o600`` blob with a
        matching digest on a read-only mount MUST return the
        verified bytes."""
        content = b"valid blob on read-only mount\n"
        integ = self._integ_for(content)
        art_id = _derived_artifact_id(integ)
        self._write_blob(art_id, content)
        reader = RuntimeArtifactReader(
            mount_inspection=_FakeMountInspection(read_only=True),
        )
        result = reader.open_verified(
            artifact_id=art_id, integrity=integ,
        )
        self.assertEqual(content, result)

    # ── boundary receives the correct path ───────────────────

    def test_mount_inspection_receives_artifact_path(self) -> None:
        """The mount-inspection boundary is called with the
        resolved artifact path — not the root directory or some
        other path."""
        content = b"path check\n"
        integ = self._integ_for(content)
        art_id = _derived_artifact_id(integ)
        expected_path = os.path.join(self._tmp, art_id)
        self._write_blob(art_id, content)
        inspection = _FakeMountInspection(read_only=True)
        reader = RuntimeArtifactReader(
            mount_inspection=inspection,
        )
        try:
            reader.open_verified(
                artifact_id=art_id, integrity=integ,
            )
        except NotImplementedError:
            pass  # expected until task 7.3
        self.assertEqual(
            [expected_path], inspection._calls,
            "mount inspection must be called with the exact "
            "resolved artifact path",
        )


# ═══════════════════════════════════════════════════════════════════════
# 10.6 — Behavioral entrypoint / module-CLI startup contract
# ═══════════════════════════════════════════════════════════════════════


class TestMainFailurePaths(unittest.TestCase):
    """Behavioral coverage of :func:`main`: force mounted-artifact
    and package-validation failures and assert nonzero exit codes.
    Each test sets up a real projection TOML and pi-home directory
    in a temp tree, patches module-level fixed-path constants,
    and exercises ``main(["install"])`` through the actual error
    paths."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        self._proj_path = os.path.join(self._tmp, "projection.toml")
        self._pi_home = os.path.join(self._tmp, "pi-home")
        os.makedirs(self._pi_home)
        self._artifact_root = os.path.join(
            self._tmp, "runtime-artifacts",
        )
        os.makedirs(self._artifact_root)

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    # ── helpers ──────────────────────────────────────────────

    @staticmethod
    def _make_integrity(content: bytes) -> str:
        d = hashlib.sha256(content).digest()
        return f"sha256-{base64.b64encode(d).decode('ascii')}"

    @staticmethod
    def _canonical_id(integrity: str) -> str:
        algo, raw = integrity.split("-", 1)
        safe = raw.replace("+", "-").replace("/", "_")
        return f"{algo}/{safe}.tgz"

    def _write_projection(self, artifact_id: str, integrity: str) -> None:
        toml = (
            f'[extensions.test-pkg]\n'
            f'package = "test-pkg"\n'
            f'version = "1.0.0"\n'
            f'metadata_file = "package.json"\n'
            f'\n'
            f'[extensions.test-pkg.artifact]\n'
            f'artifact_id = "{artifact_id}"\n'
            f'integrity = "{integrity}"\n'
        )
        with open(self._proj_path, "w") as fh:
            fh.write(toml)

    def _write_blob(self, artifact_id: str, content: bytes) -> None:
        blob_path = os.path.join(self._artifact_root, artifact_id)
        os.makedirs(os.path.dirname(blob_path), exist_ok=True)
        with open(blob_path, "wb") as fh:
            fh.write(content)
        os.chmod(blob_path, 0o400)

    # ── projection-level failures ────────────────────────────

    def test_missing_projection_returns_exit_projection(self) -> None:
        """When the runtime projection file does not exist,
        ``main()`` returns ``_EXIT_PROJECTION`` and never
        reaches the installer."""
        import docker.runtime_installer as mod

        with mock.patch.object(mod, "_FIXED_PROJECTION",
                               self._proj_path):
            rc = mod.main(["install"])
        self.assertEqual(
            rc, mod._EXIT_PROJECTION,
            "missing projection must return _EXIT_PROJECTION",
        )

    def test_malformed_projection_returns_exit_projection(self) -> None:
        """When the projection TOML is syntactically invalid,
        ``main()`` returns ``_EXIT_PROJECTION``."""
        import docker.runtime_installer as mod

        with open(self._proj_path, "w") as fh:
            fh.write("this is not valid toml {{{[[[")

        with mock.patch.object(mod, "_FIXED_PROJECTION",
                               self._proj_path):
            rc = mod.main(["install"])
        self.assertEqual(
            rc, mod._EXIT_PROJECTION,
            "malformed projection must return _EXIT_PROJECTION",
        )

    # ── mounted-artifact failures ────────────────────────────

    @staticmethod
    def _real_subprocess_run() -> Any:
        import subprocess
        return subprocess.run

    def test_missing_blob_returns_exit_install(self) -> None:
        """When the projection references a valid artifact_id
        but no blob exists at that path, ``main()`` returns
        ``_EXIT_INSTALL``.  The mount check and statvfs
        boundaries are patched so the pipeline reaches
        ``open_verified``, which fails."""
        import docker.runtime_installer as mod

        content = b"artifact body"
        integ = self._make_integrity(content)
        art_id = self._canonical_id(integ)
        self._write_projection(art_id, integ)
        # Do NOT write the blob — it's missing.

        real_run = self._real_subprocess_run()

        def _fake_run(cmd, **_kw):
            if isinstance(cmd, list) and cmd[0] == "mountpoint":
                return real_run(["true"], capture_output=True)
            return real_run(cmd, **_kw)

        with mock.patch.object(mod, "_FIXED_PROJECTION",
                               self._proj_path):
            with mock.patch.object(mod, "_FIXED_PI_HOME",
                                   self._pi_home):
                with mock.patch.object(
                    mod, "_MOUNTED_ARTIFACT_ROOT",
                    self._artifact_root,
                ), mock.patch.object(
                    mod._StatvfsMountInspection,
                    "is_read_only_mount",
                    return_value=True,
                ), mock.patch(
                    "subprocess.run", side_effect=_fake_run,
                ):
                    rc = mod.main(["install"])

        self.assertEqual(
            rc, mod._EXIT_INSTALL,
            "missing blob must return _EXIT_INSTALL",
        )

    def test_corrupt_blob_returns_exit_install(self) -> None:
        """When a blob exists but its bytes do not match the
        declared SRI integrity, ``main()`` returns
        ``_EXIT_INSTALL``."""
        import docker.runtime_installer as mod

        content = b"correct artifact bytes"
        integ = self._make_integrity(content)
        art_id = self._canonical_id(integ)
        self._write_projection(art_id, integ)
        # Write wrong bytes — integrity will fail.
        self._write_blob(art_id, b"corrupted!")

        real_run = self._real_subprocess_run()

        def _fake_run(cmd, **_kw):
            if isinstance(cmd, list) and cmd[0] == "mountpoint":
                return real_run(["true"], capture_output=True)
            return real_run(cmd, **_kw)

        with mock.patch.object(mod, "_FIXED_PROJECTION",
                               self._proj_path):
            with mock.patch.object(mod, "_FIXED_PI_HOME",
                                   self._pi_home):
                with mock.patch.object(
                    mod, "_MOUNTED_ARTIFACT_ROOT",
                    self._artifact_root,
                ), mock.patch.object(
                    mod._StatvfsMountInspection,
                    "is_read_only_mount",
                    return_value=True,
                ), mock.patch(
                    "subprocess.run", side_effect=_fake_run,
                ):
                    rc = mod.main(["install"])

        self.assertEqual(
            rc, mod._EXIT_INSTALL,
            "integrity mismatch must return _EXIT_INSTALL",
        )


class TestEntrypointExecution(unittest.TestCase):
    """Behavioral entrypoint coverage: drive the full ``main()``
    pipeline and assert exit codes.  Also verify the entrypoint
    shell script's failure branch structurally gates ``rtk`` and
    ``exec gosu dev:dev`` behind installer success."""

    _ENTRYPOINT = os.path.join(
        os.path.dirname(__file__), "..", "docker", "entrypoint.sh",
    )

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    # ── helpers ──────────────────────────────────────────────

    @staticmethod
    def _make_integrity(content: bytes) -> str:
        d = hashlib.sha256(content).digest()
        return f"sha256-{base64.b64encode(d).decode('ascii')}"

    @staticmethod
    def _canonical_id(integrity: str) -> str:
        algo, raw = integrity.split("-", 1)
        safe = raw.replace("+", "-").replace("/", "_")
        return f"{algo}/{safe}.tgz"

    @staticmethod
    def _make_integrity_from_file(path: str) -> str:
        with open(path, "rb") as fh:
            return TestEntrypointExecution._make_integrity(fh.read())

    @staticmethod
    def _write_npm_blob(path: str, name: str, version: str) -> None:
        """Write an npm-style tarball to *path*."""
        import io
        import tarfile
        meta = {"name": name, "version": version}
        meta_bytes = json.dumps(meta).encode("utf-8")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            info = tarfile.TarInfo("package/package.json")
            info.size = len(meta_bytes)
            tf.addfile(info, io.BytesIO(meta_bytes))
        os.chmod(path, 0o644)  # make writable for overwrite
        with open(path, "wb") as fh:
            fh.write(buf.getvalue())
        os.chmod(path, 0o444)

    @staticmethod
    def _rewrite_projection(
        proj_path: str, art_id: str, integ: str,
        pkg_name: str, pkg_version: str,
    ) -> None:
        toml = (
            f'[extensions."{pkg_name}"]\n'
            f'package = "{pkg_name}"\n'
            f'version = "{pkg_version}"\n'
            f'metadata_file = "package.json"\n'
            f'artifact_id = "{art_id}"\n'
            f'integrity = "{integ}"\n'
        )
        with open(proj_path, "w") as fh:
            fh.write(toml)

    def _setup_pipeline(
        self, pkg_name: str = "test-pkg",
        pkg_version: str = "1.0.0",
    ) -> tuple[str, str, str, str, str, bytes]:
        """Create a temp projection, pi-home, and artifact root
        with a valid npm-tarball blob.  Returns (proj_path, pi_home,
        art_root, art_id, integ, tarball_bytes)."""
        import io
        import tarfile

        # Build a minimal npm-style tarball.
        meta = {"name": pkg_name, "version": pkg_version}
        meta_bytes = json.dumps(meta).encode("utf-8")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            info = tarfile.TarInfo("package/package.json")
            info.size = len(meta_bytes)
            tf.addfile(info, io.BytesIO(meta_bytes))
        tarball = buf.getvalue()

        integ = self._make_integrity(tarball)
        art_id = self._canonical_id(integ)

        proj_path = os.path.join(self._tmp, "projection.toml")
        pi_home = os.path.join(self._tmp, "pi-home")
        os.makedirs(pi_home)
        art_root = os.path.join(self._tmp, "runtime-artifacts")
        os.makedirs(art_root)

        toml = (
            f'[extensions."{pkg_name}"]\n'
            f'package = "{pkg_name}"\n'
            f'version = "{pkg_version}"\n'
            f'metadata_file = "package.json"\n'
            f'artifact_id = "{art_id}"\n'
            f'integrity = "{integ}"\n'
        )
        with open(proj_path, "w") as fh:
            fh.write(toml)

        blob_path = os.path.join(art_root, art_id)
        os.makedirs(os.path.dirname(blob_path), exist_ok=True)
        with open(blob_path, "wb") as fh:
            fh.write(tarball)
        os.chmod(blob_path, 0o444)

        return proj_path, pi_home, art_root, art_id, integ, tarball

    def _patch_and_run(
        self, proj_path: str, pi_home: str, art_root: str,
    ) -> int:
        """Patch fixed paths + statvfs + mountpoint, then run
        ``main()``.  Returns the exit code."""
        import docker.runtime_installer as mod
        import subprocess as sp

        real_run = sp.run

        def _fake_mountpoint(cmd, **_kw):
            if isinstance(cmd, list) and cmd[0] == "mountpoint":
                return real_run(["true"], capture_output=True)
            return real_run(cmd, **_kw)

        with mock.patch.object(mod, "_FIXED_PROJECTION", proj_path), \
             mock.patch.object(mod, "_FIXED_PI_HOME", pi_home), \
             mock.patch.object(mod, "_MOUNTED_ARTIFACT_ROOT", art_root), \
             mock.patch.object(
                 mod.InstallContext, "real_privilege",
                 return_value=_FakePrivilegeContext(),
             ), \
             mock.patch.object(
                 mod._StatvfsMountInspection,
                 "is_read_only_mount",
                 return_value=True,
             ), \
             mock.patch("subprocess.run", side_effect=_fake_mountpoint):
            return mod.main(["install"])

    # ── success ──────────────────────────────────────────────

    def test_successful_install_returns_exit_ok(self) -> None:
        """Valid projection + matching tarball blob + correct
        metadata → ``main()`` returns ``_EXIT_OK``."""
        import docker.runtime_installer as mod

        proj_path, pi_home, art_root, art_id, integ, _ = \
            self._setup_pipeline()
        rc = self._patch_and_run(proj_path, pi_home, art_root)
        self.assertEqual(
            rc, mod._EXIT_OK,
            "successful install must return _EXIT_OK",
        )

    # ── mounted-artifact failure ─────────────────────────────

    def test_missing_blob_returns_exit_install(self) -> None:
        """No blob backs the declared artifact_id → ``main()``
        returns ``_EXIT_INSTALL``."""
        import docker.runtime_installer as mod

        integ = self._make_integrity(b"will be missing")
        art_id = self._canonical_id(integ)

        proj_path = os.path.join(self._tmp, "projection.toml")
        pi_home = os.path.join(self._tmp, "pi-home")
        os.makedirs(pi_home)
        art_root = os.path.join(self._tmp, "runtime-artifacts")
        os.makedirs(art_root)

        toml = (
            f'[extensions."test-pkg"]\n'
            f'package = "test-pkg"\n'
            f'version = "1.0.0"\n'
            f'metadata_file = "package.json"\n'
            f'artifact_id = "{art_id}"\n'
            f'integrity = "{integ}"\n'
        )
        with open(proj_path, "w") as fh:
            fh.write(toml)
        # NO blob written — intentionally missing.

        rc = self._patch_and_run(proj_path, pi_home, art_root)
        self.assertEqual(
            rc, mod._EXIT_INSTALL,
            "missing blob must return _EXIT_INSTALL",
        )

    # ── package-validation failure ───────────────────────────

    def test_package_validation_failure_returns_exit_install(self) -> None:
        """Tarball with mismatched package name → post-install
        validation fails → ``main()`` returns ``_EXIT_INSTALL``."""
        import docker.runtime_installer as mod

        proj_path, pi_home, art_root, art_id, integ, _ = \
            self._setup_pipeline(pkg_name="test-pkg", pkg_version="1.0.0")
        # Overwrite the blob with a tarball that has the wrong name.
        old_blob_path = os.path.join(art_root, art_id)
        self._write_npm_blob(old_blob_path, "wrong-pkg", "1.0.0")
        # Recompute integrity, derive the new canonical art_id,
        # and rename the blob so the installer can find it.
        integ = self._make_integrity_from_file(old_blob_path)
        art_id = self._canonical_id(integ)
        new_blob_path = os.path.join(art_root, art_id)
        os.makedirs(os.path.dirname(new_blob_path), exist_ok=True)
        os.rename(old_blob_path, new_blob_path)
        self._rewrite_projection(proj_path, art_id, integ,
                                 pkg_name="test-pkg", pkg_version="1.0.0")
        rc = self._patch_and_run(proj_path, pi_home, art_root)
        self.assertEqual(
            rc, mod._EXIT_INSTALL,
            "package name mismatch must return _EXIT_INSTALL",
        )

    def test_version_mismatch_returns_exit_install(self) -> None:
        """Tarball with mismatched version → ``main()`` returns
        ``_EXIT_INSTALL``."""
        import docker.runtime_installer as mod

        proj_path, pi_home, art_root, art_id, integ, _ = \
            self._setup_pipeline(pkg_name="test-pkg", pkg_version="1.0.0")
        # Overwrite the blob with a tarball that has the wrong version.
        old_blob_path = os.path.join(art_root, art_id)
        self._write_npm_blob(old_blob_path, "test-pkg", "9.9.9")
        # Recompute integrity, derive the new canonical art_id,
        # and rename the blob so the installer can find it.
        integ = self._make_integrity_from_file(old_blob_path)
        art_id = self._canonical_id(integ)
        new_blob_path = os.path.join(art_root, art_id)
        os.makedirs(os.path.dirname(new_blob_path), exist_ok=True)
        os.rename(old_blob_path, new_blob_path)
        self._rewrite_projection(proj_path, art_id, integ,
                                 pkg_name="test-pkg", pkg_version="1.0.0")
        rc = self._patch_and_run(proj_path, pi_home, art_root)
        self.assertEqual(
            rc, mod._EXIT_INSTALL,
            "version mismatch must return _EXIT_INSTALL",
        )

    # ── entrypoint harness integration ───────────────────────

    _HARNESS = os.path.join(
        os.path.dirname(__file__), "entrypoint_harness.sh",
    )

    def _run_harness(
        self, scenario: str, installer_fail: bool = True,
    ) -> "subprocess.CompletedProcess[str]":
        """Run the entrypoint harness with a temp projection
        and the given scenario."""
        import subprocess as sp

        # Write a minimal valid projection.
        content = b"harness blob"
        integ = self._make_integrity(content)
        art_id = self._canonical_id(integ)
        proj_file = os.path.join(self._tmp, "proj.toml")
        toml = (
            f'[extensions.test-pkg]\n'
            f'package = "test-pkg"\n'
            f'version = "1.0.0"\n'
            f'metadata_file = "package.json"\n'
            f'\n'
            f'[extensions.test-pkg.artifact]\n'
            f'artifact_id = "{art_id}"\n'
            f'integrity = "{integ}"\n'
        )
        with open(proj_file, "w") as fh:
            fh.write(toml)

        env = {
            **os.environ,
            "_SCENARIO": scenario,
            "_INSTALLER_FAIL": "1" if installer_fail else "0",
            "_ID_MODE": "root",
            "_PI_HOME_SANDBOX": self._tmp,
            "_PROJECTION_FILE": proj_file,
        }
        return sp.run(
            ["bash", self._HARNESS],
            capture_output=True,
            text=True,
            env=env,
        )

    def _assert_trace_no_rtk_no_exec(self, trace: str) -> None:
        """Assert the harness trace reflects correct startup
        ordering and failure gating.

        Required behavior:

        1. The installer is launched via ``gosu dev:dev``.
        2. Pi home is not traversed or repaired because it is not a workspace.

        Failure gating (none of these must appear):

        - ``rtk init``
        - ``rtk telemetry``
        - ``exec gosu dev:dev``

        And the trace must end with a non-zero ``exit_code``."""
        lines = [l.strip() for l in trace.splitlines()]

        # ── locate key events ──────────────────────────────────
        def _idx_containing(sub: str) -> int:
            for i, line in enumerate(lines):
                if sub in line:
                    return i
            return -1

        def _idx_match(*needles: str) -> int:
            """Return the index of the first line containing
            *all* of *needles*, or -1."""
            for i, line in enumerate(lines):
                if all(n in line for n in needles):
                    return i
            return -1

        installer_idx = _idx_containing(
            "gosu dev:dev env PYTHONPATH=/usr/local/lib/pi-cli"
            " python3 -m docker.runtime_installer install",
        )
        chown_idx = _idx_match("find ", "chown dev:dev")
        chmod_idx = _idx_match("find ", "chmod ug+rwX")

        # ── installer launch ───────────────────────────────────
        self.assertGreater(
            installer_idx, -1,
            "trace must contain the installer launched via"
            " gosu dev:dev",
        )

        # ── no non-workspace Pi-home repair ────────────────────
        self.assertEqual(chown_idx, -1, "Pi home must not be ownership-repaired")
        self.assertEqual(chmod_idx, -1, "Pi home must not be permission-repaired")

        # ── installer exits non-zero ───────────────────────────
        exit_lines = [l for l in lines if "exit_code" in l]
        self.assertTrue(
            exit_lines,
            "trace must contain exit_code",
        )
        self.assertTrue(
            any("exit_code 1" in l for l in exit_lines),
            "installer failure must produce exit_code 1;"
            " got: " + " | ".join(exit_lines),
        )

        # ── no rtk, no exec gosu ───────────────────────────────
        self.assertNotIn(
            "rtk init", trace,
            "trace must NOT contain rtk init"
            " when installation fails",
        )
        self.assertNotIn(
            "rtk telemetry", trace,
            "trace must NOT contain rtk telemetry"
            " when installation fails",
        )
        self.assertNotIn(
            'exec gosu dev:dev', trace,
            "trace must NOT contain exec gosu dev:dev"
            " when installation fails",
        )

    def test_harness_mounted_artifact_failure_aborts(self) -> None:
        """Entrypoint harness with ``_INSTALLER_FAIL=1`` under
        the mounted-artifact-failure scenario: the trace must
        show the installer ran, exited non-zero, and neither
        ``rtk`` nor ``exec gosu dev:dev`` was reached."""
        result = self._run_harness("mounted-artifact-failure")
        trace = result.stdout
        self._assert_trace_no_rtk_no_exec(trace)

    def test_harness_package_validation_failure_aborts(self) -> None:
        """Entrypoint harness with ``_INSTALLER_FAIL=1`` under
        the package-validation-failure scenario: same guard —
        installer fails, no rtk, no exec."""
        result = self._run_harness("package-validation-failure")
        trace = result.stdout
        self._assert_trace_no_rtk_no_exec(trace)


# ═══════════════════════════════════════════════════════════════════════
# Phase 2 — runtime artifact descriptor lifecycle (tasks 2.1–2.3)
# ═══════════════════════════════════════════════════════════════════════

def _fd_owner_path(fd: int) -> str | None:
    """Return the normalized path an open descriptor refers to, or ``None``."""
    try:
        return os.path.normpath(os.readlink(f"/proc/self/fd/{fd}"))
    except OSError:
        return None


def _secondary_exceptions(exc: BaseException) -> list[object]:
    """Return the secondary cleanup diagnostics retained by *exc*."""
    secondary = getattr(exc, "secondary", None)
    if isinstance(secondary, list):
        return list(secondary)
    slot = getattr(exc, "_transaction_secondary", None)
    if isinstance(slot, list):
        return list(slot)
    return list(getattr(exc, "__notes__", []))


def _carries_secondary(exc: BaseException, needle: BaseException) -> bool:
    """True when *exc* retains *needle* as secondary diagnostic context."""
    for item in _secondary_exceptions(exc):
        if item is needle:
            return True
        if isinstance(item, str) and str(needle) in item:
            return True
    return False


class TestRuntimeArtifactReaderLifecycle(unittest.TestCase):
    """Phase 2 tasks 2.1–2.3.

    ``RuntimeArtifactReader.open_verified`` owns its single mounted-artifact
    descriptor through ``OwnedDescriptor`` so an active validation, read, or
    integrity failure stays primary over an ordinary close failure, the
    descriptor is released exactly once, and a sole close failure is mapped
    at the ``open_verified`` domain boundary to ``InstallError``.
    """

    _tmp: str

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="test-runtime-lifecycle-")
        mock.patch.object(
            docker.runtime_installer,
            "_MOUNTED_ARTIFACT_ROOT",
            self._tmp,
        ).start()

    def tearDown(self) -> None:
        mock.patch.stopall()
        shutil.rmtree(self._tmp, ignore_errors=True)

    # ── helpers ──────────────────────────────────────────────

    def _integ_for(self, content: bytes, *, algo: str = "sha256") -> str:
        h = hashlib.new(algo, content)
        return f"{algo}-" + base64.b64encode(h.digest()).decode("ascii")

    def _write_blob(
        self, artifact_id: str, content: bytes, mode: int = 0o600,
    ) -> str:
        path = os.path.join(self._tmp, artifact_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, mode)
        try:
            os.write(fd, content)
        finally:
            os.close(fd)
        return path

    def _reader(self) -> RuntimeArtifactReader:
        return RuntimeArtifactReader(
            mount_inspection=_FakeMountInspection(read_only=True),
        )

    def _fail_close(
        self, target_path: str, error: BaseException,
    ) -> tuple[object, list[int]]:
        """Fail ``os.close`` for the blob fd, counting attempts."""
        real_close = os.close
        attempts: list[int] = []

        def fake_close(fd, *args, **kwargs):
            if _fd_owner_path(fd) == target_path:
                attempts.append(fd)
                real_close(fd, *args, **kwargs)
                raise error
            return real_close(fd, *args, **kwargs)

        return fake_close, attempts

    def _noatime_denied(
        self, target_path: str,
    ) -> tuple[object, list[int]]:
        """Deny the first ``O_NOATIME`` open, allowing the fallback."""
        real_open = os.open
        noatime = getattr(os, "O_NOATIME", 0)
        fallback_opens: list[int] = []

        def fake_open(path, flags, *args, **kwargs):
            if (
                noatime
                and flags & noatime
                and os.path.normpath(str(path)) == target_path
            ):
                raise PermissionError(errno.EPERM, "injected noatime denial")
            fallback_opens.append(flags)
            return real_open(path, flags, *args, **kwargs)

        return fake_open, fallback_opens

    # ── 2.1: O_NOATIME compatibility fallback ────────────────

    def test_foreign_owned_noatime_fallback_returns_bytes(self) -> None:
        content = b"foreign owned payload\n"
        integ = self._integ_for(content)
        art_id = _derived_artifact_id(integ)
        path = self._write_blob(art_id, content)
        fake_open, fallback_opens = self._noatime_denied(
            os.path.normpath(path),
        )
        with mock.patch("os.open", side_effect=fake_open):
            result = self._reader().open_verified(
                artifact_id=art_id, integrity=integ,
            )
        self.assertEqual(content, result)
        self.assertEqual(len(fallback_opens), 1)

    def test_fallback_stat_failure_primary_over_close(self) -> None:
        content = b"stat failure payload\n"
        integ = self._integ_for(content)
        art_id = _derived_artifact_id(integ)
        path = self._write_blob(art_id, content)
        target = os.path.normpath(path)
        stat_error = OSError(errno.EIO, "injected stat failure")
        close_error = OSError(errno.EIO, "injected close failure")
        fake_open, _ = self._noatime_denied(target)
        fake_close, attempts = self._fail_close(target, close_error)
        real_fstat = os.fstat

        def fake_fstat(fd):
            if _fd_owner_path(fd) == target:
                raise stat_error
            return real_fstat(fd)

        with mock.patch("os.open", side_effect=fake_open), mock.patch(
            "os.fstat", side_effect=fake_fstat,
        ), mock.patch("os.close", side_effect=fake_close):
            with self.assertRaises(InstallError) as ctx:
                self._reader().open_verified(
                    artifact_id=art_id, integrity=integ,
                )
        self.assertIn("cannot stat artifact", str(ctx.exception))
        self.assertTrue(_carries_secondary(ctx.exception, close_error))
        self.assertEqual(len(attempts), 1)

    def test_fallback_non_regular_type_primary_over_close(self) -> None:
        integ = self._integ_for(b"directory blob")
        art_id = _derived_artifact_id(integ)
        path = os.path.join(self._tmp, art_id)
        os.makedirs(path, exist_ok=True)
        target = os.path.normpath(path)
        close_error = OSError(errno.EIO, "injected close failure")
        fake_open, _ = self._noatime_denied(target)
        fake_close, attempts = self._fail_close(target, close_error)
        with mock.patch("os.open", side_effect=fake_open), mock.patch(
            "os.close", side_effect=fake_close,
        ):
            with self.assertRaises(InstallError) as ctx:
                self._reader().open_verified(
                    artifact_id=art_id, integrity=integ,
                )
        self.assertIn("is not a regular file", str(ctx.exception))
        self.assertTrue(_carries_secondary(ctx.exception, close_error))
        self.assertEqual(len(attempts), 1)

    def test_fallback_group_writable_primary_over_close(self) -> None:
        content = b"group writable\n"
        integ = self._integ_for(content)
        art_id = _derived_artifact_id(integ)
        path = self._write_blob(art_id, content)
        os.chmod(path, 0o660)
        target = os.path.normpath(path)
        close_error = OSError(errno.EIO, "injected close failure")
        fake_open, _ = self._noatime_denied(target)
        fake_close, attempts = self._fail_close(target, close_error)
        with mock.patch("os.open", side_effect=fake_open), mock.patch(
            "os.close", side_effect=fake_close,
        ):
            with self.assertRaises(InstallError) as ctx:
                self._reader().open_verified(
                    artifact_id=art_id, integrity=integ,
                )
        self.assertIn("group/world write bits", str(ctx.exception))
        self.assertTrue(_carries_secondary(ctx.exception, close_error))
        self.assertEqual(len(attempts), 1)

    # ── 2.2: active read/empty/integrity failures ────────────

    def test_read_failure_primary_over_close(self) -> None:
        content = b"read failure payload\n"
        integ = self._integ_for(content)
        art_id = _derived_artifact_id(integ)
        path = self._write_blob(art_id, content)
        read_error = OSError(errno.EIO, "injected read failure")
        close_error = OSError(errno.EIO, "injected close failure")
        fake_close, attempts = self._fail_close(
            os.path.normpath(path), close_error,
        )
        with mock.patch("os.read", side_effect=read_error), mock.patch(
            "os.close", side_effect=fake_close,
        ):
            with self.assertRaises(InstallError) as ctx:
                self._reader().open_verified(
                    artifact_id=art_id, integrity=integ,
                )
        self.assertIn("read error on artifact", str(ctx.exception))
        self.assertTrue(_carries_secondary(ctx.exception, close_error))
        self.assertEqual(len(attempts), 1)

    def test_empty_artifact_primary_over_close(self) -> None:
        integ = self._integ_for(b"")
        art_id = _derived_artifact_id(integ)
        path = self._write_blob(art_id, b"")
        close_error = OSError(errno.EIO, "injected close failure")
        fake_close, attempts = self._fail_close(
            os.path.normpath(path), close_error,
        )
        with mock.patch("os.close", side_effect=fake_close):
            with self.assertRaises(InstallError) as ctx:
                self._reader().open_verified(
                    artifact_id=art_id, integrity=integ,
                )
        self.assertIn("is empty", str(ctx.exception))
        self.assertTrue(_carries_secondary(ctx.exception, close_error))
        self.assertEqual(len(attempts), 1)

    def test_integrity_failure_primary_over_close(self) -> None:
        written = b"what was written\n"
        claimed = b"what integrity claims\n"
        integ = self._integ_for(claimed)
        art_id = _derived_artifact_id(integ)
        path = self._write_blob(art_id, written)
        close_error = OSError(errno.EIO, "injected close failure")
        fake_close, attempts = self._fail_close(
            os.path.normpath(path), close_error,
        )
        with mock.patch("os.close", side_effect=fake_close):
            with self.assertRaises(IntegrityError) as ctx:
                self._reader().open_verified(
                    artifact_id=art_id, integrity=integ,
                )
        self.assertTrue(_carries_secondary(ctx.exception, close_error))
        self.assertEqual(len(attempts), 1)

    # ── 2.3: sole close failure boundary mapping ─────────────

    def test_sole_close_failure_maps_to_install_error(self) -> None:
        content = b"sole close payload\n"
        integ = self._integ_for(content)
        art_id = _derived_artifact_id(integ)
        path = self._write_blob(art_id, content)
        close_error = OSError(errno.EIO, "injected close failure")
        fake_close, attempts = self._fail_close(
            os.path.normpath(path), close_error,
        )
        with mock.patch("os.close", side_effect=fake_close):
            with self.assertRaises(InstallError) as ctx:
                self._reader().open_verified(
                    artifact_id=art_id, integrity=integ,
                )
        self.assertNotIsInstance(ctx.exception, IntegrityError)
        self.assertEqual(
            str(ctx.exception),
            f"cannot close artifact at {path}: {close_error}",
        )
        self.assertIs(ctx.exception.__cause__, close_error)
        self.assertEqual(len(attempts), 1)

    def test_sole_close_reports_controlled_install_failure(self) -> None:
        content = b"controlled failure payload\n"
        integ = self._integ_for(content)
        art_id = _derived_artifact_id(integ)
        path = self._write_blob(art_id, content)
        close_error = OSError(errno.EIO, "injected close failure")
        fake_close, attempts = self._fail_close(
            os.path.normpath(path), close_error,
        )
        ctx = _make_fake_context()
        object.__setattr__(ctx, "blob_reader", self._reader())
        entries = [ProjectionEntry(
            package="p", version="1.0.0",
            artifact_id=art_id, artifact_integrity=integ,
            metadata_file="package.json",
        )]
        with mock.patch("os.close", side_effect=fake_close):
            result = install_extensions(
                ctx, entries=entries, pi_home="/mnt/pi",
            )
        self.assertEqual(
            InstallStatus.FAILED, result.results[0].status,
        )
        self.assertIn(
            "cannot close artifact", result.results[0].detail or "",
        )
        self.assertEqual(len(attempts), 1)

    def test_close_interruption_identity_preserved(self) -> None:
        content = b"interruption payload\n"
        integ = self._integ_for(content)
        art_id = _derived_artifact_id(integ)
        path = self._write_blob(art_id, content)
        interruption = KeyboardInterrupt()
        fake_close, attempts = self._fail_close(
            os.path.normpath(path), interruption,
        )
        with mock.patch("os.close", side_effect=fake_close):
            with self.assertRaises(KeyboardInterrupt) as ctx:
                self._reader().open_verified(
                    artifact_id=art_id, integrity=integ,
                )
        self.assertIs(ctx.exception, interruption)
        self.assertEqual(len(attempts), 1)
