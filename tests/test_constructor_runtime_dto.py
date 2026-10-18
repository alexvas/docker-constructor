"""Stage 5.2 — Closed-DTO tests for EffectiveRuntimeProjection."""

import io
import unittest

from docker.versioning.model import (
    EffectivePiExtensionEntry,
    EffectiveRuntimeProjection,
    NpmArtifact,
)

# ═══════════════════════════════════════════════════════════════════════
# Task 4.1 — Replace downloadable URL with canonical mounted-artifact identity
# ═══════════════════════════════════════════════════════════════════════


class TestNarrowedProjectionDTO(unittest.TestCase):
    """EffectivePiExtensionEntry carries a canonical mounted-artifact
    identifier (artifact_id) alongside the SRI integrity string instead
    of a downloadable URL.

    These tests assert the DESIRED contract and FAIL until
    NpmArtifact is replaced with artifact_id + integrity."""

    _INTEGRITY = (
        "sha512-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=="
    )
    # Derived canonical artifact_id for _INTEGRITY.
    # algo=sha512, safe digest replaces +→- and /→_.
    _ARTIFACT_ID = (
        "sha512/"
        + _INTEGRITY.split("-", 1)[1].replace("+", "-").replace("/", "_")
        + ".tgz"
    )

    @staticmethod
    def _artifact_id_from(integrity: str) -> str:
        """Derive the canonical relative path from an SRI integrity.

        The artifact_id is ``<algorithm>/<digest>.tgz`` where *digest*
        is the URL-safe base64 encoding (``+`` → ``-``, ``/`` → ``_``).
        It is the deterministic suffix that, joined with the fixed
        container root ``/run/pi-cli/runtime-artifacts/``, forms the
        full container-side mount target.
        """
        algo, raw = integrity.split("-", 1)
        safe = raw.replace("+", "-").replace("/", "_")
        return f"{algo}/{safe}.tgz"

    # ── positive construction: artifact_id + integrity ────────────

    def test_artifact_id_and_integrity_both_accepted(self) -> None:
        """RED — the narrowed entry requires both a canonical
        artifact_id and the SRI integrity string, replacing
        NpmArtifact(url=..., integrity=...)."""
        expected_id = self._ARTIFACT_ID
        entry = EffectivePiExtensionEntry(
            package="@scope/pkg",
            version="2.3.4",
            artifact_id=expected_id,
            integrity=self._INTEGRITY,
            metadata_file="package.json",
        )
        self.assertEqual(entry.package, "@scope/pkg")
        self.assertEqual(entry.version, "2.3.4")
        self.assertEqual(entry.artifact_id, expected_id)
        self.assertEqual(entry.integrity, self._INTEGRITY)
        self.assertEqual(entry.metadata_file, "package.json")

    def test_artifact_id_is_canonical_for_its_integrity(self) -> None:
        """RED — artifact_id is deterministically derived from
        integrity: ``<algorithm>/<safe_base64>.tgz``."""
        expected_id = self._ARTIFACT_ID
        self.assertTrue(expected_id.startswith("sha512/"))
        self.assertTrue(expected_id.endswith(".tgz"))
        # Prove the derivation is deterministic.
        derived = self._artifact_id_from(self._INTEGRITY)
        self.assertEqual(derived, expected_id)
        entry = EffectivePiExtensionEntry(
            package="p",
            version="1.0.0",
            artifact_id=expected_id,
            integrity=self._INTEGRITY,
            metadata_file="package.json",
        )
        self.assertEqual(entry.artifact_id, expected_id)
        self.assertEqual(entry.integrity, self._INTEGRITY)

    # ── identity agreement: artifact_id must match integrity ─────

    def test_rejects_artifact_id_with_wrong_algorithm(self) -> None:
        """RED — an artifact_id whose algorithm prefix does not
        match the SRI algorithm is rejected."""
        wrong_id = "sha256/" + self._ARTIFACT_ID.split("/", 1)[1]
        with self.assertRaises(ValueError):
            EffectivePiExtensionEntry(
                package="p",
                version="1.0.0",
                artifact_id=wrong_id,
                integrity=self._INTEGRITY,  # sha512
                metadata_file="package.json",
            )

    def test_rejects_artifact_id_with_wrong_digest(self) -> None:
        """RED — an artifact_id whose digest bytes do not match
        integrity is rejected even when the algorithm agrees."""
        # Same algorithm, different digest.
        other_integrity = (
            "sha512-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
            "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB=="
        )
        wrong_id = self._artifact_id_from(other_integrity)
        with self.assertRaises(ValueError):
            EffectivePiExtensionEntry(
                package="p",
                version="1.0.0",
                artifact_id=wrong_id,
                integrity=self._INTEGRITY,
                metadata_file="package.json",
            )

    # ── old artifact=NpmArtifact(...) rejected ───────────────────

    def test_artifact_field_rejected(self) -> None:
        """RED — the old artifact=NpmArtifact(...) keyword is no
        longer accepted on the narrowed entry."""
        valid_url = "https://registry.npmjs.org/@scope/pkg/-/pkg-1.0.0.tgz"
        with self.assertRaises(TypeError):
            EffectivePiExtensionEntry(
                package="@scope/pkg",
                version="1.0.0",
                artifact=NpmArtifact(
                    url=valid_url,
                    integrity=self._INTEGRITY,
                ),
                metadata_file="package.json",
            )

    def test_url_field_not_present_on_entry(self) -> None:
        """RED — the entry exposes no downloadable URL."""
        entry = EffectivePiExtensionEntry(
            package="p",
            version="1.0.0",
            artifact_id=self._ARTIFACT_ID,
            integrity=self._INTEGRITY,
            metadata_file="package.json",
        )
        self.assertFalse(hasattr(entry, "url"))
        self.assertFalse(hasattr(entry, "artifact"))

    # ── each field is independently preserved ───────────────────

    def test_package_preserved(self) -> None:
        """RED — package identity survives the transition."""
        entry = EffectivePiExtensionEntry(
            package="@scope/pkg",
            version="1.0.0",
            artifact_id=self._ARTIFACT_ID,
            integrity=self._INTEGRITY,
            metadata_file="package.json",
        )
        self.assertEqual(entry.package, "@scope/pkg")

    def test_version_preserved(self) -> None:
        """RED — exact version survives the transition."""
        entry = EffectivePiExtensionEntry(
            package="p",
            version="4.5.6",
            artifact_id=self._ARTIFACT_ID,
            integrity=self._INTEGRITY,
            metadata_file="package.json",
        )
        self.assertEqual(entry.version, "4.5.6")

    def test_integrity_preserved_independently(self) -> None:
        """RED — the SRI integrity string is preserved as its own
        field, independent of artifact_id."""
        entry = EffectivePiExtensionEntry(
            package="p",
            version="1.0.0",
            artifact_id=self._ARTIFACT_ID,
            integrity=self._INTEGRITY,
            metadata_file="package.json",
        )
        self.assertEqual(entry.integrity, self._INTEGRITY)
        self.assertTrue(
            entry.integrity.startswith("sha512-"),
            "integrity must be a valid SRI string",
        )

    def test_metadata_file_preserved(self) -> None:
        """RED — safe metadata validation survives the transition."""
        entry = EffectivePiExtensionEntry(
            package="p",
            version="1.0.0",
            artifact_id=self._ARTIFACT_ID,
            integrity=self._INTEGRITY,
            metadata_file="nested/meta.json",
        )
        self.assertEqual(entry.metadata_file, "nested/meta.json")

    # ── integrity validation at construction ─────────────────────

    def test_rejects_malformed_integrity(self) -> None:
        """RED — the entry rejects an obviously invalid integrity
        string at construction time."""
        with self.assertRaises(ValueError):
            EffectivePiExtensionEntry(
                package="p",
                version="1.0.0",
                artifact_id=self._ARTIFACT_ID,
                integrity="not-an-sri-string",
                metadata_file="package.json",
            )


# ═══════════════════════════════════════════════════════════════════════
# Task 4.2 — Closed-schema rejection of host-only fields
# ═══════════════════════════════════════════════════════════════════════


class TestClosedSchemaRejection(unittest.TestCase):
    """The narrowed runtime projection DTO SHALL reject downloadable
    URLs, host cache roots/paths, absolute or traversal artifact
    identifiers, and other host-only fields.

    All tests use the DESIRED ``artifact_id`` + ``integrity``
    structure and FAIL until the DTO is updated in task 4.3."""

    _INTEGRITY = (
        "sha512-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=="
    )
    _ARTIFACT_ID = (
        "sha512/"
        + _INTEGRITY.split("-", 1)[1].replace("+", "-").replace("/", "_")
        + ".tgz"
    )

    def _entry(self, **kw: object) -> EffectivePiExtensionEntry:
        defaults: dict[str, object] = {
            "package": "p",
            "version": "1.0.0",
            "artifact_id": self._ARTIFACT_ID,
            "integrity": self._INTEGRITY,
            "metadata_file": "package.json",
        }
        defaults.update(kw)
        return EffectivePiExtensionEntry(**defaults)  # type: ignore[arg-type]

    def _proj(self, **kw: object) -> EffectiveRuntimeProjection:
        defaults: dict[str, object] = {"extensions": {"p": self._entry()}}
        defaults.update(kw)
        return EffectiveRuntimeProjection(**defaults)  # type: ignore[arg-type]

    # ── reject downloadable URL ──────────────────────────────────

    def test_rejects_url_on_entry(self) -> None:
        """RED — the narrowed entry MUST NOT accept a downloadable
        URL field."""
        with self.assertRaises(TypeError):
            self._entry(url="https://registry.npmjs.org/p/-/p-1.0.0.tgz")

    # ── reject host cache root / path ────────────────────────────

    def test_rejects_cache_root_on_entry(self) -> None:
        """RED — the entry MUST NOT carry a host cache root."""
        with self.assertRaises(TypeError):
            self._entry(cache_root="/tmp/cache")

    def test_rejects_blob_path_on_entry(self) -> None:
        """RED — the entry MUST NOT carry a host blob path."""
        with self.assertRaises(TypeError):
            self._entry(blob_path="/tmp/cache/blobs/sha512/abc.tgz")

    def test_rejects_host_blob_path_on_entry(self) -> None:
        """RED — the entry MUST NOT carry a host blob path under
        any name."""
        with self.assertRaises(TypeError):
            self._entry(host_path="/tmp/cache/blobs/sha512/abc.tgz")

    # ── reject container mount target ────────────────────────────

    def test_rejects_container_target_on_entry(self) -> None:
        """RED — the entry MUST NOT dictate its container mount
        target; that is the renderer's responsibility."""
        with self.assertRaises(TypeError):
            self._entry(container_target="/run/pi-cli/runtime-artifacts/sha512/abc.tgz")

    # ── reject source / update / override / validation / catalog ─

    def test_rejects_source_field_on_entry(self) -> None:
        """RED — the narrowed entry MUST NOT carry provider-specific
        source metadata (NpmSource, GitHubReleaseSource, etc.)."""
        with self.assertRaises(TypeError):
            self._entry(source={"type": "npm", "package": "p"})

    def test_rejects_update_field_on_entry(self) -> None:
        """RED — the narrowed entry MUST NOT carry update-policy
        metadata."""
        with self.assertRaises(TypeError):
            self._entry(update={"provider": "npm", "stable_only": True})

    def test_rejects_override_field_on_entry(self) -> None:
        """RED — the narrowed entry MUST NOT carry override-policy
        metadata."""
        with self.assertRaises(TypeError):
            self._entry(override={"constraint": ">=1.0.0"})

    def test_rejects_validation_field_on_entry(self) -> None:
        """RED — the narrowed entry MUST NOT carry the compound
        RuntimeValidation wrapper; only the bare metadata_file is
        preserved."""
        with self.assertRaises(TypeError):
            self._entry(validation={"metadata_file": "package.json"})

    def test_rejects_artifacts_catalog_on_entry(self) -> None:
        """RED — the narrowed entry MUST NOT carry the full
        reviewed artifact catalog (unselected alternatives)."""
        with self.assertRaises(TypeError):
            self._entry(artifacts={"1.0.0": {"url": "https://x.test/p.tgz"}})

    # ── reject build fields on projection ────────────────────────

    def test_rejects_stages_field_on_projection(self) -> None:
        """RED — the runtime projection MUST NOT carry build-stage
        configuration."""
        with self.assertRaises(TypeError):
            self._proj(stages={})

    def test_rejects_build_field_on_projection(self) -> None:
        """RED — the runtime projection MUST NOT carry a build
        field."""
        with self.assertRaises(TypeError):
            self._proj(build={})

    def test_rejects_platform_field_on_projection(self) -> None:
        """RED — the runtime projection MUST NOT carry build
        platform metadata."""
        with self.assertRaises(TypeError):
            self._proj(platform="linux/amd64")

    def test_rejects_node_field_on_projection(self) -> None:
        """RED — the runtime projection MUST NOT carry build
        Node.js metadata."""
        with self.assertRaises(TypeError):
            self._proj(node={"image": "node:20"})

    def test_rejects_rust_field_on_projection(self) -> None:
        """RED — the runtime projection MUST NOT carry build
        Rust metadata."""
        with self.assertRaises(TypeError):
            self._proj(rust={"version": "1.80"})

    def test_rejects_uv_field_on_projection(self) -> None:
        """RED — the runtime projection MUST NOT carry build
        uv metadata."""
        with self.assertRaises(TypeError):
            self._proj(uv={"version": "0.12"})

    def test_rejects_python_version_field_on_projection(self) -> None:
        """RED — the runtime projection MUST NOT carry build
        Python version metadata."""
        with self.assertRaises(TypeError):
            self._proj(python_version="3.11")

    def test_rejects_ty_version_field_on_projection(self) -> None:
        """RED — the runtime projection MUST NOT carry build
        ty version metadata."""
        with self.assertRaises(TypeError):
            self._proj(ty_version="2.0")

    def test_rejects_rtk_field_on_projection(self) -> None:
        """RED — the runtime projection MUST NOT carry build
        rtk metadata."""
        with self.assertRaises(TypeError):
            self._proj(rtk={"version": "0.5"})

    def test_rejects_fd_field_on_projection(self) -> None:
        """RED — the runtime projection MUST NOT carry build
        fd metadata."""
        with self.assertRaises(TypeError):
            self._proj(fd={"version": "10.0"})

    def test_rejects_pi_version_field_on_projection(self) -> None:
        """RED — the runtime projection MUST NOT carry build
        pi version metadata."""
        with self.assertRaises(TypeError):
            self._proj(pi_version="3.1.0")

    def test_rejects_openspec_version_field_on_projection(self) -> None:
        """RED — the runtime projection MUST NOT carry build
        openspec version metadata."""
        with self.assertRaises(TypeError):
            self._proj(openspec_version="1.0")

    def test_rejects_oh_my_zsh_revision_field_on_projection(self) -> None:
        """RED — the runtime projection MUST NOT carry build
        oh-my-zsh revision metadata."""
        with self.assertRaises(TypeError):
            self._proj(oh_my_zsh_revision="abc123")

    # ── reject absolute artifact_id ──────────────────────────────

    def test_rejects_absolute_artifact_id(self) -> None:
        """RED — artifact_id must be a relative path; an absolute
        path is rejected."""
        with self.assertRaises(ValueError):
            self._entry(artifact_id="/tmp/p.tgz")

    def test_rejects_leading_slash_artifact_id(self) -> None:
        """RED — artifact_id must not start with ``/``, preventing
        alternative-root attacks."""
        with self.assertRaises(ValueError):
            self._entry(artifact_id="/sha512/abc.tgz")

    # ── reject traversal artifact_id ─────────────────────────────

    def test_rejects_traversal_in_artifact_id(self) -> None:
        """RED — artifact_id must not contain ``..`` segments."""
        with self.assertRaises(ValueError):
            self._entry(artifact_id="sha512/../../etc/hostname.tgz")

    def test_rejects_empty_segment_in_artifact_id(self) -> None:
        """RED — artifact_id must not contain empty path segments."""
        with self.assertRaises(ValueError):
            self._entry(artifact_id="sha512//double-slash.tgz")


if __name__ == "__main__":
    unittest.main()
