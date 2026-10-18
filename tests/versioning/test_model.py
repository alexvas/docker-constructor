"""Unit tests for docker.versioning.model — validation helpers."""

from __future__ import annotations

import unittest

from docker.versioning.constraints import NumericVersion as ConstraintNumericVersion
from docker.versioning.model import NumericVersion, _validate_utc_rfc3339


class TestValidateUtcRfc3339(unittest.TestCase):
    """Direct validation of the RFC 3339 UTC timestamp validator."""

    # ── valid Z-suffix ────────────────────────────────────────────

    def test_z_suffix_roundtrips(self) -> None:
        self.assertEqual(
            _validate_utc_rfc3339("2025-01-01T00:00:00Z"),
            "2025-01-01T00:00:00Z",
        )

    def test_z_with_fraction(self) -> None:
        self.assertEqual(
            _validate_utc_rfc3339("2025-06-15T12:30:00.123Z"),
            "2025-06-15T12:30:00.123Z",
        )

    def test_z_trailing_zero_fraction_retained(self) -> None:
        """Fractional .000 is preserved exactly — offset-only normalisation."""
        self.assertEqual(
            _validate_utc_rfc3339("2025-06-15T12:30:00.000Z"),
            "2025-06-15T12:30:00.000Z",
        )

    def test_z_mixed_fraction(self) -> None:
        self.assertEqual(
            _validate_utc_rfc3339("2025-06-15T12:30:00.100Z"),
            "2025-06-15T12:30:00.100Z",
        )

    # ── valid +00:00 offset ───────────────────────────────────────

    def test_plus_00_00_normalised_to_z(self) -> None:
        self.assertEqual(
            _validate_utc_rfc3339("2025-07-15T08:00:00+00:00"),
            "2025-07-15T08:00:00Z",
        )

    def test_plus_00_00_with_fraction_normalised(self) -> None:
        self.assertEqual(
            _validate_utc_rfc3339("2025-07-15T08:00:00.456+00:00"),
            "2025-07-15T08:00:00.456Z",
        )

    # ── rejected: non-UTC offset ──────────────────────────────────

    def test_minus_00_00_rejected(self) -> None:
        """-00:00 is an unknown local offset, not authoritative UTC."""
        self.assertIsNone(_validate_utc_rfc3339("2025-01-01T00:00:00-00:00"))

    def test_non_utc_plus_rejected(self) -> None:
        self.assertIsNone(_validate_utc_rfc3339("2025-01-01T00:00:00+01:00"))

    def test_non_utc_minus_rejected(self) -> None:
        self.assertIsNone(_validate_utc_rfc3339("2025-01-01T00:00:00-05:00"))

    # ── rejected: timezone-less ───────────────────────────────────

    def test_timezone_less_rejected(self) -> None:
        self.assertIsNone(_validate_utc_rfc3339("2025-01-01T00:00:00"))

    # ── rejected: non-RFC-3339 forms ──────────────────────────────

    def test_space_separator_rejected(self) -> None:
        self.assertIsNone(_validate_utc_rfc3339("2025-01-01 00:00:00Z"))

    def test_compact_form_rejected(self) -> None:
        self.assertIsNone(_validate_utc_rfc3339("20250101T000000Z"))

    def test_bare_date_rejected(self) -> None:
        self.assertIsNone(_validate_utc_rfc3339("2025-01-01"))

    # ── rejected: invalid / absent inputs ─────────────────────────

    def test_none_rejected(self) -> None:
        self.assertIsNone(_validate_utc_rfc3339(None))

    def test_empty_string_rejected(self) -> None:
        self.assertIsNone(_validate_utc_rfc3339(""))

    def test_whitespace_only_rejected(self) -> None:
        self.assertIsNone(_validate_utc_rfc3339("   "))

    def test_garbage_rejected(self) -> None:
        self.assertIsNone(_validate_utc_rfc3339("not-a-timestamp"))

    def test_invalid_date_rejected(self) -> None:
        """Feb 30 is not a real calendar date."""
        self.assertIsNone(_validate_utc_rfc3339("2025-02-30T12:00:00Z"))

    def test_fraction_digits_up_to_six_accepted(self) -> None:
        """Up to 6 fractional digits (microsecond) accepted."""
        self.assertEqual(
            _validate_utc_rfc3339("2025-01-01T00:00:00.123456Z"),
            "2025-01-01T00:00:00.123456Z",
        )

    def test_fraction_digits_over_six_rejected(self) -> None:
        """>6 fractional digits exceed datetime resolution — rejected."""
        self.assertIsNone(_validate_utc_rfc3339("2025-01-01T00:00:00.1234567Z"))


class TestCompatibilityExports(unittest.TestCase):
    """Public compatibility aliases remain importable and identical."""

    def test_numeric_version_is_reexported_from_model(self) -> None:
        self.assertIs(NumericVersion, ConstraintNumericVersion)
