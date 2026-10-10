"""Phase 1 (P1) contract tests for the diagnostic URL recognition results.

These tests bind the RED obligations for Phase 1 of
``linearize-diagnostic-url-detection``:

* one immutable raw-coordinate candidate carrying an inclusive raw ``start``,
  an exclusive raw ``end`` (or ``None`` while unterminated) and a closed
  ``KNOWN``/``UNKNOWN`` classification;
* one closed recognition outcome (``NO_MATCH``, ``MATCH(candidate)``,
  ``NEED_MORE(retain_from)``) that is separate from the candidate
  classification; and
* the fixed case-insensitive scheme whitelist.

The contract module is greenfield, so before it exists every test fails with
an explicit assertion naming the missing contract rather than a collection
error.  The test names themselves enumerate the P1 obligations.
"""
from __future__ import annotations

import dataclasses
import importlib
import unittest

_MODULE_NAME = "docker.versioning.diagnostic_recognition"

#: The single authoritative known-scheme whitelist from the delta requirement
#: ``Classify diagnostic URL candidates without interpreting unknown schemes``.
EXPECTED_KNOWN_SCHEMES = frozenset(
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
            self.fail(
                f"{_MODULE_NAME} must define the P1 recognition result contract"
            )
        return module


class CandidateKindContractTests(_ContractCase):
    def test_candidate_kind_is_closed_known_unknown(self):
        module = self.contract()
        names = {member.name for member in module.CandidateKind}
        self.assertEqual({"KNOWN", "UNKNOWN"}, names)

    def test_known_and_unknown_are_distinct_members(self):
        module = self.contract()
        self.assertIsNot(module.CandidateKind.KNOWN, module.CandidateKind.UNKNOWN)


class DiagnosticCandidateContractTests(_ContractCase):
    def test_candidate_carries_raw_inclusive_start_and_exclusive_end(self):
        module = self.contract()
        candidate = module.DiagnosticCandidate(
            start=4, end=16, kind=module.CandidateKind.KNOWN
        )
        self.assertEqual(4, candidate.start)
        self.assertEqual(16, candidate.end)
        self.assertIs(module.CandidateKind.KNOWN, candidate.kind)

    def test_candidate_end_is_exclusive_of_the_raw_terminator(self):
        module = self.contract()
        raw = "x https://host y"
        start = raw.index("https")
        end = raw.index(" y")
        candidate = module.DiagnosticCandidate(
            start=start, end=end, kind=module.CandidateKind.KNOWN
        )
        self.assertEqual("https://host", raw[candidate.start : candidate.end])

    def test_candidate_end_none_marks_an_unterminated_candidate(self):
        module = self.contract()
        candidate = module.DiagnosticCandidate(
            start=0, end=None, kind=module.CandidateKind.UNKNOWN
        )
        self.assertIsNone(candidate.end)

    def test_candidate_offsets_are_raw_character_indices(self):
        module = self.contract()
        raw = "\u00e9https://host"  # one two-byte character, then the token
        candidate = module.DiagnosticCandidate(
            start=1, end=len(raw), kind=module.CandidateKind.KNOWN
        )
        self.assertEqual(1, candidate.start)
        self.assertEqual("https://host", raw[candidate.start : candidate.end])

    def test_candidate_is_immutable(self):
        module = self.contract()
        candidate = module.DiagnosticCandidate(
            start=0, end=5, kind=module.CandidateKind.KNOWN
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            candidate.start = 1  # type: ignore[misc]

    def test_candidate_rejects_negative_start(self):
        module = self.contract()
        with self.assertRaises((TypeError, ValueError)):
            module.DiagnosticCandidate(
                start=-1, end=5, kind=module.CandidateKind.KNOWN
            )

    def test_candidate_rejects_end_not_strictly_after_start(self):
        module = self.contract()
        for end in (0, 4):
            with self.subTest(end=end):
                with self.assertRaises((TypeError, ValueError)):
                    module.DiagnosticCandidate(
                        start=4, end=end, kind=module.CandidateKind.KNOWN
                    )

    def test_candidate_rejects_non_integer_offsets(self):
        module = self.contract()
        with self.assertRaises((TypeError, ValueError)):
            module.DiagnosticCandidate(
                start="0", end=5, kind=module.CandidateKind.KNOWN  # type: ignore[arg-type]
            )
        with self.assertRaises((TypeError, ValueError)):
            module.DiagnosticCandidate(
                start=0, end="5", kind=module.CandidateKind.KNOWN  # type: ignore[arg-type]
            )

    def test_candidate_requires_a_closed_kind(self):
        module = self.contract()
        with self.assertRaises((TypeError, ValueError)):
            module.DiagnosticCandidate(start=0, end=5, kind="known")  # type: ignore[arg-type]


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

    def test_match_carries_a_candidate_and_no_retention_point(self):
        module = self.contract()
        candidate = module.DiagnosticCandidate(
            start=2, end=9, kind=module.CandidateKind.KNOWN
        )
        result = module.RecognitionResult.matched(candidate)
        self.assertIs(module.RecognitionKind.MATCH, result.kind)
        self.assertIs(candidate, result.candidate)
        self.assertIsNone(result.retain_from)

    def test_match_may_carry_an_unterminated_candidate(self):
        module = self.contract()
        candidate = module.DiagnosticCandidate(
            start=0, end=None, kind=module.CandidateKind.UNKNOWN
        )
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
        candidate = module.DiagnosticCandidate(
            start=0, end=5, kind=module.CandidateKind.KNOWN
        )
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
        candidate = module.DiagnosticCandidate(
            start=0, end=5, kind=module.CandidateKind.KNOWN
        )
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


class SchemeWhitelistContractTests(_ContractCase):
    def test_whitelist_matches_the_delta_requirement_exactly(self):
        module = self.contract()
        self.assertEqual(EXPECTED_KNOWN_SCHEMES, set(module.KNOWN_SCHEMES))
        self.assertEqual(17, len(module.KNOWN_SCHEMES))

    def test_every_whitelist_entry_classifies_as_known(self):
        module = self.contract()
        for scheme in sorted(EXPECTED_KNOWN_SCHEMES):
            with self.subTest(scheme=scheme):
                self.assertIs(
                    module.CandidateKind.KNOWN, module.classify_scheme(scheme)
                )

    def test_every_whitelist_entry_classifies_case_insensitively(self):
        module = self.contract()
        for scheme in sorted(EXPECTED_KNOWN_SCHEMES):
            for variant in (
                scheme.upper(),
                scheme.capitalize(),
                scheme.title(),
                "".join(
                    character.upper() if index % 2 else character
                    for index, character in enumerate(scheme)
                ),
            ):
                with self.subTest(scheme=scheme, variant=variant):
                    self.assertIs(
                        module.CandidateKind.KNOWN,
                        module.classify_scheme(variant),
                    )

    def test_non_whitelist_variants_classify_as_unknown(self):
        module = self.contract()
        for scheme in (
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
        ):
            with self.subTest(scheme=scheme):
                self.assertIs(
                    module.CandidateKind.UNKNOWN, module.classify_scheme(scheme)
                )

    def test_classifier_rejects_non_string_input(self):
        module = self.contract()
        with self.assertRaises(TypeError):
            module.classify_scheme(None)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            module.classify_scheme(b"https")  # type: ignore[arg-type]

    def test_npm_is_not_a_separator_based_known_scheme(self):
        module = self.contract()
        self.assertNotIn("npm", module.KNOWN_SCHEMES)
        self.assertIs(module.CandidateKind.UNKNOWN, module.classify_scheme("npm"))


class StreamCoordinateContractTests(_ContractCase):
    """Candidate/outcome offsets are absolute stream coordinates.

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
            kind=module.CandidateKind.KNOWN,
        )
        return retained_buffer_start, stream, candidate_text, candidate

    def test_stream_offsets_convert_to_a_retained_buffer_index(self):
        retained_buffer_start, stream, candidate_text, candidate = (
            self._stream_fixture()
        )
        # Absolute stream coordinates select the candidate from the whole
        # stream, not from the most recent feed alone.
        self.assertEqual(
            candidate_text, stream[candidate.start : candidate.end]
        )
        self.assertGreater(candidate.start, retained_buffer_start)
        # Conversion at the retention boundary (documented formula).
        buffer_index = candidate.start - retained_buffer_start
        self.assertEqual(
            self._RETAINED_BUFFER.index(candidate_text), buffer_index
        )
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


if __name__ == "__main__":
    unittest.main()
