"""Phase 9 downstream-plan ownership checks."""
from __future__ import annotations

import unittest
from pathlib import Path

from docker import transactions
from docker.transactions import codec

_REPO = Path(__file__).resolve().parents[1]
_CHANGES = _REPO / "openspec/changes"


def _combined_text(change: str) -> str:
    root = _CHANGES / change
    return "\n".join(
        (root / name).read_text(encoding="utf-8")
        for name in ("proposal.md", "design.md", "tasks.md")
        if (root / name).exists()
    )


class PiExtensionPlanBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = _combined_text("add-locked-image-owned-pi-extensions").lower()

    def test_owns_sync_lock_and_settings_sidecar_schemas(self) -> None:
        self.assertIn("sync-lock journal", self.text)
        self.assertIn("settings sidecar", self.text)
        self.assertIn("complete closed versioned schemas", self.text)

    def test_owns_recovery_for_both_records(self) -> None:
        self.assertIn("recovery protocols", self.text)
        self.assertIn("domain-owned multi-target", self.text)
        self.assertIn("compare-and-swap adapters", self.text)

    def test_does_not_delegate_a_generic_envelope_or_shared_recovery(self) -> None:
        for forbidden in ("journal-envelope layer", "journal envelope layer",
                          "single-authority recovery substrate"):
            self.assertNotIn(forbidden, self.text)
        self.assertIn("do not treat shared primitives as generic envelope", self.text)


class MetadataPlanBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = _combined_text("revalidate-update-metadata").lower()

    def test_owns_the_cache_record_envelope_schema(self) -> None:
        self.assertIn("envelope schema validation", self.text)
        self.assertIn("metadata domain", self.text)
        self.assertIn("shared layer receives bytes", self.text)

    def test_adopts_no_journal_or_broad_transaction_lock(self) -> None:
        self.assertIn("does not adopt recoverable journals or a broad transaction lock", self.text)
        self.assertIn("no metadata authority", self.text)

    def test_does_not_delegate_a_generic_envelope(self) -> None:
        self.assertNotIn("shared envelope schema", self.text)
        self.assertNotIn("shared recovery contract", self.text)


class SharedSubstrateSeamTests(unittest.TestCase):
    def test_no_generic_envelope_or_recovery_api(self) -> None:
        exported = set(transactions.__all__)
        forbidden = ("Envelope", "Journal", "Recovery", "Recover", "Schema")
        self.assertFalse(any(any(token in name for token in forbidden) for name in exported))

    def test_codec_grants_no_schema_or_version_authority(self) -> None:
        public_callables = {
            name for name, value in vars(codec).items()
            if not name.startswith("_") and callable(value) and getattr(value, "__module__", None) == codec.__name__
        }
        self.assertEqual({"encode", "decode"}, public_callables)
        self.assertFalse(hasattr(codec, "version"))
        self.assertFalse(hasattr(codec, "migrate"))

    def test_no_immutable_generation_authority_in_the_substrate(self) -> None:
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((_REPO / "docker/transactions").glob("*.py"))
        ).lower()
        for token in ("committed-build", "buildmanifest", "generation"):
            self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
