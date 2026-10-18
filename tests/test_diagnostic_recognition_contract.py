"""Phase 1 (P1) contract tests for the diagnostic URL recognition results.

These tests bind the revised RED obligations for Phase 1 of
``linearize-diagnostic-url-detection``:

* **supported-only detection** -- only the fixed case-insensitive scheme set
  is supported; unknown schemes are ordinary text and are never a candidate or
  a metadata source (``SUPPORTED_SCHEMES`` / ``is_supported_scheme``);
* one immutable raw-coordinate candidate carrying an inclusive raw ``start``
  and an exclusive raw ``end`` (or ``None`` while unterminated), with a
  supported default classification and no public/persisted DTO;
* one closed recognition outcome (``NO_MATCH``, ``MATCH(candidate)``,
  ``NEED_MORE(retain_from)``) separate from any candidate classification;
* an immutable :class:`SecretCoverage` raw source span for registered literal
  secrets; and
* absolute stream coordinates that convert to a retained-buffer index with
  ``buffer_index = stream_offset - retained_buffer_start``.

Superseded obligations
----------------------

The pre-revision P1 contract made "arbitrary UNKNOWN scheme classification" a
first-class recognition obligation and required the whitelist classifier to
report ``CandidateKind.UNKNOWN`` for arbitrary schemes.  That obligation is
intentionally superseded: detection is now supported-only, so an unknown
scheme is a ``NO_MATCH`` ordinary-text outcome rather than a detected URL.
``CandidateKind`` and ``classify_scheme`` remain available only as
compatibility helpers for callers that have not yet moved to
:func:`is_supported_scheme`; they must never make an unsupported scheme a
detected URL or authorise hostname/host-path/fingerprint facts.  This file no
longer asserts that arbitrary schemes are a recognised candidate class.

The contract module is greenfield, so before it exists every test fails with
an explicit assertion naming the missing contract rather than a collection
error.  The test names themselves enumerate the P1 obligations.
"""

from __future__ import annotations

import dataclasses
import importlib
import unittest

_MODULE_NAME = "docker.versioning.diagnostic_recognition"

#: The single authoritative supported-scheme set from the delta requirement
#: ``Recognize only supported diagnostic URL schemes``.
EXPECTED_SUPPORTED_SCHEMES = frozenset(
    {
        "http",
        "https",
        "ftp",
        "ftps",
        "ssh",
        "git",
        "git+ssh",
        "git+http",
        "git+https",
        "file",
        "ws",
        "wss",
        "socks",
        "socks4",
        "socks4a",
        "socks5",
        "socks5h",
    }
)

#: Schemes that are RFC-valid or scheme-shaped but deliberately unsupported.
#: They must remain ordinary text with no URL facts.
UNSUPPORTED_SCHEMES = (
    "",
    "httpx",
    "https+",
    "ssh2",
    "git+file",
    "git+",
    "npm",
    "custom+transport",
    "gopher",
    "mailto",
    "data",
    "socks6",
    "wss2",
    "HTTPX",
    "Custom+Transport",
)


def _load_contract():
    try:
        return importlib.import_module(_MODULE_NAME)
    except ModuleNotFoundError:
        return None


class _ContractCase(unittest.TestCase):
    """Base case that fails cleanly while the contract module is absent."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.contract_module = _load_contract()

    def contract(self):
        module = type(self).contract_module
        if module is None:
            self.fail(f"{_MODULE_NAME} must define the P1 recognition result contract")
        return module


class SupportedSchemeSetTests(_ContractCase):
    """Supported-only detection: the fixed scheme set is authoritative."""

    def test_supported_scheme_set_matches_the_delta_requirement_exactly(self):
        module = self.contract()
        self.assertEqual(EXPECTED_SUPPORTED_SCHEMES, set(module.SUPPORTED_SCHEMES))
        self.assertEqual(17, len(module.SUPPORTED_SCHEMES))

    def test_legacy_known_schemes_name_is_the_same_supported_set(self):
        module = self.contract()
        self.assertEqual(set(module.SUPPORTED_SCHEMES), set(module.KNOWN_SCHEMES))

    def test_every_supported_scheme_is_supported_case_insensitively(self):
        module = self.contract()
        for scheme in sorted(EXPECTED_SUPPORTED_SCHEMES):
            for variant in (
                scheme,
                scheme.upper(),
                scheme.capitalize(),
                scheme.title(),
                "".join(
                    character.upper() if index % 2 else character
                    for index, character in enumerate(scheme)
                ),
            ):
                with self.subTest(scheme=scheme, variant=variant):
                    self.assertTrue(module.is_supported_scheme(variant))

    def test_unsupported_schemes_are_not_supported(self):
        module = self.contract()
        for scheme in UNSUPPORTED_SCHEMES:
            with self.subTest(scheme=scheme):
                self.assertFalse(module.is_supported_scheme(scheme))

    def test_supported_check_rejects_non_string_input(self):
        module = self.contract()
        with self.assertRaises(TypeError):
            module.is_supported_scheme(None)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            module.is_supported_scheme(b"https")  # type: ignore[arg-type]

    def test_npm_is_not_a_supported_separator_scheme(self):
        module = self.contract()
        self.assertNotIn("npm", module.SUPPORTED_SCHEMES)
        self.assertFalse(module.is_supported_scheme("npm"))

    def test_compatibility_classifier_still_marks_unsupported_schemes_unknown(
        self,
    ):
        # Superseded as a detection obligation, retained as a compatibility
        # helper only.  ``UNKNOWN`` here is not a detected URL.
        module = self.contract()
        self.assertIs(module.CandidateKind.KNOWN, module.classify_scheme("https"))
        for scheme in UNSUPPORTED_SCHEMES:
            with self.subTest(scheme=scheme):
                self.assertIs(
                    module.CandidateKind.UNKNOWN,
                    module.classify_scheme(scheme),
                )


class SupportedOnlyDetectionContractTests(_ContractCase):
    """Detection-only model assertions that do not require a recognizer."""

    def test_candidate_defaults_to_the_supported_classification(self):
        module = self.contract()
        candidate = module.DiagnosticCandidate(start=0, end=5)
        self.assertIs(module.CandidateKind.KNOWN, candidate.kind)

    def test_supported_scheme_embedded_in_longer_text_is_a_candidate(self):
        module = self.contract()
        raw = "TOKENhttps://host/public"
        start = raw.index("https")
        candidate = module.DiagnosticCandidate(start=start, end=len(raw))
        # The candidate starts at the supported scheme, not at the prefix.
        self.assertEqual("https://host/public", raw[candidate.start : candidate.end])
        self.assertTrue(module.is_supported_scheme(raw[start : start + 5]))

    def test_unknown_text_has_no_supported_scheme_start(self):
        module = self.contract()
        raw = "custom+transport://user:password@internal.example/private"
        scheme = raw.split(":", 1)[0]
        self.assertFalse(module.is_supported_scheme(scheme))
        # No supported start means the recognizer reports ordinary text.  The
        # contract model itself exposes no candidate kind that would make an
        # unsupported scheme publishable, so an unknown scheme cannot become a
        # metadata source through this module.
        self.assertNotIn(scheme, module.SUPPORTED_SCHEMES)

    def test_candidate_model_has_no_unsupported_detection_constructor(self):
        module = self.contract()
        candidate = module.DiagnosticCandidate(start=0, end=4)
        self.assertFalse(
            hasattr(candidate, "is_url"),
            "a candidate must not claim URL validity",
        )


class DiagnosticCandidateContractTests(_ContractCase):
    def test_candidate_carries_raw_inclusive_start_and_exclusive_end(self):
        module = self.contract()
        candidate = module.DiagnosticCandidate(start=4, end=16)
        self.assertEqual(4, candidate.start)
        self.assertEqual(16, candidate.end)
        self.assertIs(module.CandidateKind.KNOWN, candidate.kind)

    def test_candidate_end_is_exclusive_of_the_raw_terminator(self):
        module = self.contract()
        raw = "x https://host y"
        start = raw.index("https")
        end = raw.index(" y")
        candidate = module.DiagnosticCandidate(start=start, end=end)
        self.assertEqual("https://host", raw[candidate.start : candidate.end])

    def test_candidate_end_none_marks_an_unterminated_candidate(self):
        module = self.contract()
        candidate = module.DiagnosticCandidate(start=0, end=None)
        self.assertIsNone(candidate.end)

    def test_candidate_offsets_are_raw_character_indices(self):
        module = self.contract()
        raw = "\u00e9https://host"  # one two-byte character, then the token
        candidate = module.DiagnosticCandidate(start=1, end=len(raw))
        self.assertEqual(1, candidate.start)
        self.assertEqual("https://host", raw[candidate.start : candidate.end])

    def test_candidate_is_immutable(self):
        module = self.contract()
        candidate = module.DiagnosticCandidate(start=0, end=5)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            candidate.start = 1  # type: ignore[misc]

    def test_candidate_rejects_negative_start(self):
        module = self.contract()
        with self.assertRaises((TypeError, ValueError)):
            module.DiagnosticCandidate(start=-1, end=5)

    def test_candidate_rejects_end_not_strictly_after_start(self):
        module = self.contract()
        for end in (0, 4):
            with self.subTest(end=end):
                with self.assertRaises((TypeError, ValueError)):
                    module.DiagnosticCandidate(start=4, end=end)

    def test_candidate_rejects_non_integer_offsets(self):
        module = self.contract()
        with self.assertRaises((TypeError, ValueError)):
            module.DiagnosticCandidate(start="0", end=5)  # type: ignore[arg-type]
        with self.assertRaises((TypeError, ValueError)):
            module.DiagnosticCandidate(start=0, end="5")  # type: ignore[arg-type]

    def test_candidate_requires_a_closed_kind(self):
        module = self.contract()
        with self.assertRaises((TypeError, ValueError)):
            module.DiagnosticCandidate(start=0, end=5, kind="known")  # type: ignore[arg-type]


class SecretCoverageContractTests(_ContractCase):
    """Source secret coverage is an immutable raw source span."""

    def test_coverage_carries_inclusive_start_and_exclusive_end(self):
        module = self.contract()
        coverage = module.SecretCoverage(start=3, end=8)
        self.assertEqual(3, coverage.start)
        self.assertEqual(8, coverage.end)

    def test_coverage_selects_the_covered_source(self):
        module = self.contract()
        raw = "before TOKEN after"
        start = raw.index("TOKEN")
        coverage = module.SecretCoverage(start=start, end=start + 5)
        self.assertEqual("TOKEN", raw[coverage.start : coverage.end])

    def test_coverage_offsets_are_raw_character_indices(self):
        module = self.contract()
        raw = "\u20acTOKEN"
        coverage = module.SecretCoverage(start=1, end=6)
        self.assertEqual("TOKEN", raw[coverage.start : coverage.end])

    def test_coverage_is_immutable(self):
        module = self.contract()
        coverage = module.SecretCoverage(start=0, end=5)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            coverage.start = 1  # type: ignore[misc]

    def test_coverage_rejects_invalid_offsets(self):
        module = self.contract()
        with self.assertRaises((TypeError, ValueError)):
            module.SecretCoverage(start=-1, end=5)
        with self.assertRaises((TypeError, ValueError)):
            module.SecretCoverage(start=5, end=5)
        with self.assertRaises((TypeError, ValueError)):
            module.SecretCoverage(start=5, end=4)
        with self.assertRaises((TypeError, ValueError)):
            module.SecretCoverage(start="0", end=5)  # type: ignore[arg-type]
        with self.assertRaises((TypeError, ValueError)):
            module.SecretCoverage(start=0, end=None)  # type: ignore[arg-type]


class RecognitionOutcomeContractTests(_ContractCase):
    def test_outcome_kind_is_closed_no_match_match_need_more(self):
        module = self.contract()
        names = {member.name for member in module.RecognitionKind}
        self.assertEqual({"NO_MATCH", "MATCH", "NEED_MORE"}, names)

    def test_no_match_carries_no_candidate_and_no_retention_point(self):
        module = self.contract()
        result = module.RecognitionResult.no_match()
        self.assertIs(module.RecognitionKind.NO_MATCH, result.kind)
        self.assertIsNone(result.candidate)
        self.assertIsNone(result.retain_from)

    def test_unknown_text_is_not_a_matched_candidate(self):
        # Supported-only detection: an unsupported scheme yields NO_MATCH, not
        # a MATCH carrying a compatibility UNKNOWN classification.
        module = self.contract()
        result = module.RecognitionResult.no_match()
        self.assertIs(module.RecognitionKind.NO_MATCH, result.kind)
        self.assertIsNone(result.candidate)

    def test_match_carries_a_candidate_and_no_retention_point(self):
        module = self.contract()
        candidate = module.DiagnosticCandidate(start=2, end=9)
        result = module.RecognitionResult.matched(candidate)
        self.assertIs(module.RecognitionKind.MATCH, result.kind)
        self.assertIs(candidate, result.candidate)
        self.assertIsNone(result.retain_from)

    def test_match_may_carry_an_unterminated_candidate(self):
        module = self.contract()
        candidate = module.DiagnosticCandidate(start=0, end=None)
        result = module.RecognitionResult.matched(candidate)
        self.assertIs(module.RecognitionKind.MATCH, result.kind)
        self.assertIsNone(result.candidate.end)

    def test_need_more_carries_a_retention_start_and_no_candidate(self):
        module = self.contract()
        result = module.RecognitionResult.need_more(4)
        self.assertIs(module.RecognitionKind.NEED_MORE, result.kind)
        self.assertIsNone(result.candidate)
        self.assertEqual(4, result.retain_from)

    def test_match_and_need_more_are_distinct_from_no_match(self):
        module = self.contract()
        candidate = module.DiagnosticCandidate(start=0, end=5)
        outcomes = {
            module.RecognitionKind.NO_MATCH,
            module.RecognitionKind.MATCH,
            module.RecognitionKind.NEED_MORE,
        }
        self.assertEqual(3, len(outcomes))
        self.assertIs(
            module.RecognitionKind.MATCH,
            module.RecognitionResult.matched(candidate).kind,
        )
        self.assertIs(
            module.RecognitionKind.NEED_MORE,
            module.RecognitionResult.need_more(0).kind,
        )
        self.assertIs(
            module.RecognitionKind.NO_MATCH,
            module.RecognitionResult.no_match().kind,
        )

    def test_outcome_is_immutable(self):
        module = self.contract()
        result = module.RecognitionResult.no_match()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.kind = module.RecognitionKind.MATCH  # type: ignore[misc]

    def test_outcome_rejects_inconsistent_field_combinations(self):
        module = self.contract()
        candidate = module.DiagnosticCandidate(start=0, end=5)
        with self.assertRaises((TypeError, ValueError)):
            module.RecognitionResult(
                module.RecognitionKind.NO_MATCH,
                candidate=candidate,
            )
        with self.assertRaises((TypeError, ValueError)):
            module.RecognitionResult(
                module.RecognitionKind.MATCH,
            )
        with self.assertRaises((TypeError, ValueError)):
            module.RecognitionResult(
                module.RecognitionKind.MATCH,
                candidate=candidate,
                retain_from=0,
            )
        with self.assertRaises((TypeError, ValueError)):
            module.RecognitionResult(
                module.RecognitionKind.NEED_MORE,
                candidate=candidate,
                retain_from=0,
            )

    def test_need_more_rejects_a_negative_retention_start(self):
        module = self.contract()
        with self.assertRaises((TypeError, ValueError)):
            module.RecognitionResult.need_more(-1)

    def test_outcome_requires_a_closed_kind(self):
        module = self.contract()
        with self.assertRaises((TypeError, ValueError)):
            module.RecognitionResult("match")  # type: ignore[arg-type]


class StreamCoordinateContractTests(_ContractCase):
    """Candidate/coverage/outcome offsets are absolute stream coordinates.

    The conversion exercised here is the documented retention-boundary
    formula ``buffer_index = stream_offset - retained_buffer_start``.  The
    module defines the coordinate space but performs no conversion itself;
    executable chunk-continuation behavior belongs to P3.
    """

    #: A consumed prefix with a multibyte character, so a UTF-8 byte offset
    #: would disagree with the character offset.
    _CONSUMED_PREFIX = "\u00e9!"
    #: A retained buffer that begins with a multibyte character and contains
    #: safe text before the candidate, so the converted index is nonzero.
    _RETAINED_BUFFER = "\u20ac note https://host tail"

    def _stream_fixture(self):
        module = self.contract()
        retained_buffer_start = len(self._CONSUMED_PREFIX)
        stream = self._CONSUMED_PREFIX + self._RETAINED_BUFFER
        candidate_text = "https://host"
        candidate_start = retained_buffer_start + self._RETAINED_BUFFER.index(
            candidate_text
        )
        candidate_end = candidate_start + len(candidate_text)
        candidate = module.DiagnosticCandidate(
            start=candidate_start,
            end=candidate_end,
        )
        return retained_buffer_start, stream, candidate_text, candidate

    def test_stream_offsets_convert_to_a_retained_buffer_index(self):
        retained_buffer_start, stream, candidate_text, candidate = (
            self._stream_fixture()
        )
        # Absolute stream coordinates select the candidate from the whole
        # stream, not from the most recent feed alone.
        self.assertEqual(candidate_text, stream[candidate.start : candidate.end])
        self.assertGreater(candidate.start, retained_buffer_start)
        # Conversion at the retention boundary (documented formula).
        buffer_index = candidate.start - retained_buffer_start
        self.assertEqual(self._RETAINED_BUFFER.index(candidate_text), buffer_index)
        self.assertGreater(buffer_index, 0)
        self.assertEqual(
            candidate_text,
            self._RETAINED_BUFFER[
                buffer_index : buffer_index + (candidate.end - candidate.start)
            ],
        )

    def test_offsets_count_python_characters_not_utf8_bytes(self):
        retained_buffer_start, _stream, candidate_text, candidate = (
            self._stream_fixture()
        )
        buffer_index = candidate.start - retained_buffer_start
        # A UTF-8 byte offset into the multibyte prefix is larger than the
        # character offset, and slicing the character string with it would not
        # select the candidate text.
        byte_index = len(self._RETAINED_BUFFER[:buffer_index].encode("utf-8"))
        self.assertGreater(byte_index, buffer_index)
        self.assertNotEqual(
            candidate_text,
            self._RETAINED_BUFFER[byte_index : byte_index + len(candidate_text)],
        )

    def test_need_more_retain_from_is_an_absolute_stream_offset(self):
        module = self.contract()
        retained_buffer_start = len(self._CONSUMED_PREFIX)
        retained_buffer = "\u20ac note https:"
        retain_stream = retained_buffer_start + retained_buffer.index("https:")
        outcome = module.RecognitionResult.need_more(retain_stream)
        buffer_index = outcome.retain_from - retained_buffer_start
        self.assertGreater(buffer_index, 0)
        self.assertEqual("https:", retained_buffer[buffer_index:])

    def test_secret_coverage_uses_the_same_stream_coordinates(self):
        module = self.contract()
        retained_buffer_start = len(self._CONSUMED_PREFIX)
        retained_buffer = "\u20ac note TOKEN tail"
        coverage_start = retained_buffer_start + retained_buffer.index("TOKEN")
        coverage = module.SecretCoverage(
            start=coverage_start, end=coverage_start + len("TOKEN")
        )
        buffer_index = coverage.start - retained_buffer_start
        self.assertGreater(buffer_index, 0)
        self.assertEqual("TOKEN", retained_buffer[buffer_index : buffer_index + 5])


if __name__ == "__main__":
    unittest.main()
