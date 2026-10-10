"""Phase 2 (P2) tests for the compact raw diagnostic URL recognizer.

These tests bind the RED obligations for Phase 2 of
``linearize-diagnostic-url-detection``:

* raw recognition of literal separators, negative ``:/``/``npm:`` forms,
  arbitrary UNKNOWN scheme lengths, raw end boundaries and earliest-source
  selection, expressed through the P1 :class:`RecognitionResult` model and
  absolute raw source coordinates (task 2.1);
* encoded recognition at detection depths zero through four, mixed depths,
  hex case, separately encoded escape characters and malformed/incomplete
  escapes, preserving the legacy three-pass-plus-encoded-separator language
  without granting a fourth interpretation pass (task 2.2);
* a bounded deterministic differential corpus against the legacy
  ``_find_url_start`` / ``_find_terminator`` / ``_url_prefix_state`` detector
  (task 2.3); and
* deterministic bounded work/state checks with no elapsed-time measurement,
  input-sized search map or encoded-variant enumeration (task 2.4).

The recognizer module is greenfield, so before it exists every test fails with
an explicit assertion naming the missing recognizer rather than a collection
error.  The test names enumerate the P2 obligations.

The legacy detector is used only as an oracle for the differential corpus and
is not imported by the production recognizer.
"""
from __future__ import annotations

import importlib
import random
import unittest
from unittest import mock

from docker.versioning.diagnostic_projection import (
    _find_terminator,
    _find_url_start,
    _trailing_token,
    _url_prefix_state,
)
from docker.versioning.diagnostic_recognition import (
    CandidateKind,
    KNOWN_SCHEMES,
    RecognitionKind,
    RecognitionResult,
)

_MODULE_NAME = "docker.versioning.diagnostic_recognizer"

#: Legacy "no viable prefix" integer state (``_URL_PREFIX_NONE``).
_PREFIX_NONE = 0


def _load_recognizer():
    try:
        return importlib.import_module(_MODULE_NAME)
    except ModuleNotFoundError:
        return None


def _legacy_expectation(text: str):
    """Return the legacy detector's expectation for *text*.

    ``("match", start, end)`` mirrors ``_find_url_start`` plus the raw
    terminator from that start; ``("need_more", retain_from)`` mirrors the
    trailing-token prefix state; ``("no_match",)`` is neither.
    """
    start = _find_url_start(text)
    if start is not None:
        return ("match", start, _find_terminator(text, start))
    token = _trailing_token(text)
    if _url_prefix_state(token) != _PREFIX_NONE:
        return ("need_more", len(text) - len(token))
    return ("no_match",)


def _percent_encode(text: str, *, uppercase: bool = True) -> str:
    """Percent-encode every character of *text* one layer deeper."""
    template = "%{:02X}" if uppercase else "%{:02x}"
    return "".join(template.format(ord(character)) for character in text)


def _encode_depth(text: str, depth: int, *, uppercase: bool = True) -> str:
    """Apply *depth* percent-encoding passes, re-encoding earlier escapes."""
    for _ in range(depth):
        text = _percent_encode(text, uppercase=uppercase)
    return text


class _RecognizerCase(unittest.TestCase):
    """Base case that fails cleanly while the recognizer module is absent."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.recognizer_module = _load_recognizer()

    def recognizer(self):
        module = type(self).recognizer_module
        if module is None:
            self.fail(
                f"{_MODULE_NAME} must define the P2 raw URL recognizer"
            )
        return module

    def recognize(self, text: str, *, offset: int = 0) -> RecognitionResult:
        return self.recognizer().recognize(text, offset=offset)


class RawRecognitionTests(_RecognizerCase):
    """Task 2.1 -- raw literal recognition and source coordinates."""

    def test_literal_separator_is_a_candidate_at_its_raw_start(self):
        text = "note https://registry.example.com/pkg.tgz tail"
        result = self.recognize(text)
        self.assertIs(RecognitionKind.MATCH, result.kind)
        self.assertEqual(text.index("https"), result.candidate.start)
        self.assertEqual(
            "https://registry.example.com/pkg.tgz",
            text[result.candidate.start : result.candidate.end],
        )

    def test_candidate_end_is_the_exclusive_raw_terminator(self):
        text = "GET https://host/path 12ms"
        result = self.recognize(text)
        self.assertIs(RecognitionKind.MATCH, result.kind)
        candidate = result.candidate
        self.assertEqual("https://host/path", text[candidate.start : candidate.end])
        self.assertTrue(text[candidate.end].isspace())

    def test_unterminated_candidate_has_end_none(self):
        text = "see https://host/path"
        result = self.recognize(text)
        self.assertIs(RecognitionKind.MATCH, result.kind)
        self.assertIsNone(result.candidate.end)

    def test_terminator_variants_end_the_candidate(self):
        self.recognizer()
        for separator in (" ", "\t", "\n", "\r", "<", "|", '"', "\x01"):
            with self.subTest(separator=repr(separator)):
                text = f"https://host{separator}tail"
                result = self.recognize(text)
                self.assertIs(RecognitionKind.MATCH, result.kind)
                self.assertEqual(len("https://host"), result.candidate.end)

    def test_single_slash_does_not_complete_the_separator(self):
        text = "note:/section"
        result = self.recognize(text)
        self.assertIsNot(RecognitionKind.MATCH, result.kind)

    def test_npm_colon_form_does_not_complete_the_separator(self):
        text = "npm:package"
        result = self.recognize(text)
        self.assertIsNot(RecognitionKind.MATCH, result.kind)

    def test_arbitrarily_long_unknown_scheme_is_detected(self):
        scheme = "custom" + "x" * 4096
        text = f"{scheme}://internal.example/private"
        result = self.recognize(text)
        self.assertIs(RecognitionKind.MATCH, result.kind)
        self.assertEqual(0, result.candidate.start)
        self.assertIs(CandidateKind.UNKNOWN, result.candidate.kind)

    def test_known_scheme_is_classified_known(self):
        self.recognizer()
        for scheme in ("http", "https", "ftp", "git+ssh", "socks5h"):
            with self.subTest(scheme=scheme):
                result = self.recognize(f"{scheme}://host")
                self.assertIs(RecognitionKind.MATCH, result.kind)
                self.assertIs(CandidateKind.KNOWN, result.candidate.kind)

    def test_unknown_scheme_is_classified_unknown(self):
        self.recognizer()
        for scheme in ("custom+transport", "gopher", "npm", "httpx"):
            with self.subTest(scheme=scheme):
                result = self.recognize(f"{scheme}://host")
                self.assertIs(RecognitionKind.MATCH, result.kind)
                self.assertIs(CandidateKind.UNKNOWN, result.candidate.kind)

    def test_earliest_source_selection_prefers_earlier_encoded_candidate(self):
        # A nested-encoded UNKNOWN candidate precedes a literal KNOWN one.
        text = "custom%253A%252F%252Finternal https://registry.example.com"
        result = self.recognize(text)
        self.assertIs(RecognitionKind.MATCH, result.kind)
        self.assertEqual(0, result.candidate.start)
        self.assertIs(CandidateKind.UNKNOWN, result.candidate.kind)

    def test_earliest_source_selection_prefers_earlier_literal_candidate(self):
        text = "custom://internal https://registry.example.com"
        result = self.recognize(text)
        self.assertIs(RecognitionKind.MATCH, result.kind)
        self.assertEqual(0, result.candidate.start)

    def test_offset_is_added_to_absolute_coordinates(self):
        text = "https://host tail"
        result = self.recognize(text, offset=100)
        self.assertIs(RecognitionKind.MATCH, result.kind)
        self.assertEqual(100, result.candidate.start)
        self.assertEqual(100 + len("https://host"), result.candidate.end)

    def test_ordinary_tokens_without_a_viable_prefix_return_no_match(self):
        self.recognizer()
        # A trailing token that starts with a digit or a non-scheme character
        # is not a viable scheme/separator prefix.
        for text in ("ordinary 42", "GET 200 3", "a b 9"):
            with self.subTest(text=text):
                self.assertIs(RecognitionKind.NO_MATCH, self.recognize(text).kind)

    def test_plain_scheme_word_reports_need_more(self):
        result = self.recognize("git")
        self.assertIs(RecognitionKind.NEED_MORE, result.kind)
        self.assertEqual(0, result.retain_from)

    def test_started_separator_prefix_reports_need_more(self):
        self.recognizer()
        for text in ("https:", "https:/", "note:/section"):
            with self.subTest(text=text):
                result = self.recognize(text)
                self.assertIs(RecognitionKind.NEED_MORE, result.kind)
                self.assertEqual(0, result.retain_from)

    def test_trailing_token_is_the_retention_start(self):
        text = "alpha beta git+"
        result = self.recognize(text)
        self.assertIs(RecognitionKind.NEED_MORE, result.kind)
        self.assertEqual(text.index("git"), result.retain_from)

    def test_multibyte_prefix_offsets_are_character_indices(self):
        text = "\u00e9\u20ac https://host"
        result = self.recognize(text)
        self.assertIs(RecognitionKind.MATCH, result.kind)
        self.assertEqual(3, result.candidate.start)
        self.assertEqual("https://host", text[3:])


class BoundedClassificationAllocationTests(_RecognizerCase):
    """Task 2.10 -- classification copies only a pre-checked bounded span."""

    def test_oversized_scheme_is_detected_but_not_classified(self):
        module = self.recognizer()
        bound = module._CLASSIFY_BOUND
        scheme = "custom" + "x" * (bound * 20)
        text = "note " + scheme + "://internal.example/private tail"
        start = text.index("custom")
        original = module._classify_raw_scheme
        with mock.patch.object(
            module, "_classify_raw_scheme", side_effect=original
        ) as spy:
            result = module.recognize(text)
        # Detection is preserved: MATCH with exact raw boundaries.
        self.assertIs(RecognitionKind.MATCH, result.kind)
        self.assertEqual(start, result.candidate.start)
        self.assertEqual(
            scheme + "://internal.example/private",
            text[result.candidate.start : result.candidate.end],
        )
        # Only the classification degrades to UNKNOWN, with no oversized copy.
        self.assertIs(CandidateKind.UNKNOWN, result.candidate.kind)
        spy.assert_not_called()

    def test_scheme_at_classify_bound_is_classified(self):
        module = self.recognizer()
        bound = module._CLASSIFY_BOUND
        scheme = "a" * bound
        text = scheme + "://host"
        original = module._classify_raw_scheme
        with mock.patch.object(
            module, "_classify_raw_scheme", side_effect=original
        ) as spy:
            result = module.recognize(text)
        self.assertIs(RecognitionKind.MATCH, result.kind)
        self.assertEqual(0, result.candidate.start)
        self.assertIs(CandidateKind.UNKNOWN, result.candidate.kind)
        spy.assert_called_once()
        span = spy.call_args.args[0]
        self.assertIsInstance(span, str)
        self.assertEqual(bound, len(span))

    def test_scheme_just_above_classify_bound_is_not_classified(self):
        module = self.recognizer()
        bound = module._CLASSIFY_BOUND
        scheme = "a" * (bound + 1)
        text = "prefix " + scheme + "://host"
        original = module._classify_raw_scheme
        with mock.patch.object(
            module, "_classify_raw_scheme", side_effect=original
        ) as spy:
            result = module.recognize(text)
        self.assertIs(RecognitionKind.MATCH, result.kind)
        self.assertEqual(len("prefix "), result.candidate.start)
        self.assertIs(CandidateKind.UNKNOWN, result.candidate.kind)
        spy.assert_not_called()


class WhitelistClassificationTests(_RecognizerCase):
    """Task 2.10 -- supported spellings of every whitelist scheme are KNOWN.

    The P2 classification bound is derived from the longest whitelist scheme
    and the maximum supported percent-encoding expansion, so encoding a known
    scheme up to ``_LAYERS`` times must still classify ``KNOWN``.
    """

    def _assert_known_with_boundaries(self, raw_scheme: str) -> None:
        module = self.recognizer()
        text = raw_scheme + "://host tail"
        result = module.recognize(text)
        self.assertIs(RecognitionKind.MATCH, result.kind)
        self.assertIs(CandidateKind.KNOWN, result.candidate.kind)
        self.assertEqual(0, result.candidate.start)
        candidate_end = result.candidate.end
        self.assertIsNotNone(candidate_end)
        self.assertEqual(raw_scheme + "://host", text[0:candidate_end])

    def test_every_whitelist_scheme_is_known_at_every_supported_depth(self):
        module = self.recognizer()
        for scheme in sorted(KNOWN_SCHEMES):
            for depth in range(module._LAYERS + 1):
                with self.subTest(scheme=scheme, depth=depth):
                    self._assert_known_with_boundaries(
                        _encode_depth(scheme, depth)
                    )

    def test_lowercase_hex_spellings_are_known(self):
        module = self.recognizer()
        for scheme in sorted(KNOWN_SCHEMES):
            for depth in range(module._LAYERS + 1):
                with self.subTest(scheme=scheme, depth=depth):
                    self._assert_known_with_boundaries(
                        _encode_depth(scheme, depth, uppercase=False)
                    )

    def test_uppercase_scheme_spellings_are_known(self):
        module = self.recognizer()
        for scheme in sorted(KNOWN_SCHEMES):
            for depth in range(module._LAYERS + 1):
                with self.subTest(scheme=scheme, depth=depth):
                    self._assert_known_with_boundaries(
                        _encode_depth(scheme.upper(), depth)
                    )

    def test_mixed_depth_spellings_are_known(self):
        module = self.recognizer()
        for scheme in sorted(KNOWN_SCHEMES):
            depths = [index % (module._LAYERS + 1) for index in range(len(scheme))]
            raw = "".join(
                _encode_depth(character, depth)
                for character, depth in zip(scheme, depths)
            )
            with self.subTest(scheme=scheme, depths=depths):
                self._assert_known_with_boundaries(raw)

    def test_separately_encoded_escape_characters_are_known(self):
        # A depth-2 spelling re-encodes the ``%`` inside the depth-1 escape as
        # well as its hex digits, so earlier escapes are encoded separately.
        depth_two = _encode_depth("h", 2)
        self.assertEqual("%25%36%38", depth_two)
        self._assert_known_with_boundaries(depth_two + "ttps")
        self._assert_known_with_boundaries(_encode_depth("https", 2))

    def test_four_times_encoded_https_is_known(self):
        module = self.recognizer()
        raw = _encode_depth("https", module._LAYERS)
        self.assertEqual(5 * (3 ** module._LAYERS), len(raw))
        self.assertEqual(405, len(raw))
        # 405 raw characters is a supported spelling, so the derived bound must
        # cover it (the previous fixed 256 bound classified it UNKNOWN).
        self.assertGreaterEqual(module._CLASSIFY_BOUND, len(raw))
        self._assert_known_with_boundaries(raw)


class EncodedRecognitionTests(_RecognizerCase):
    """Task 2.2 -- encoded detection at depths zero through four."""

    def test_depth_zero_literal_separator(self):
        result = self.recognize("https://host")
        self.assertIs(RecognitionKind.MATCH, result.kind)

    def test_depth_one_encoded_separator(self):
        result = self.recognize("custom%3A%2F%2Fhost")
        self.assertIs(RecognitionKind.MATCH, result.kind)
        self.assertEqual(0, result.candidate.start)

    def test_depth_two_encoded_separator(self):
        result = self.recognize("custom%253A%252F%252Fhost")
        self.assertIs(RecognitionKind.MATCH, result.kind)

    def test_depth_three_encoded_separator(self):
        result = self.recognize("custom%25253A%25252F%25252Fhost")
        self.assertIs(RecognitionKind.MATCH, result.kind)

    def test_depth_four_encoded_separator(self):
        result = self.recognize("custom%2525253A%2525252F%2525252Fhost")
        self.assertIs(RecognitionKind.MATCH, result.kind)

    def test_depth_five_separator_is_not_recognized(self):
        result = self.recognize("custom%252525253A%252525252F%252525252Fhost")
        self.assertIsNot(RecognitionKind.MATCH, result.kind)

    def test_mixed_depth_separator(self):
        self.recognizer()
        for separator in ("%253A%2F/", ":%2f/", "%3A%2F/"):
            with self.subTest(separator=separator):
                result = self.recognize(f"custom{separator}host")
                self.assertIs(RecognitionKind.MATCH, result.kind)

    def test_separately_encoded_escape_characters(self):
        result = self.recognize("custom%25%33%41%25%32%46/host")
        self.assertIs(RecognitionKind.MATCH, result.kind)

    def test_hex_letter_case_is_recognized(self):
        self.recognizer()
        for separator in ("%3A%2F%2F", "%3a%2f%2f", "%3A%2f%2F"):
            with self.subTest(separator=separator):
                result = self.recognize(f"custom{separator}host")
                self.assertIs(RecognitionKind.MATCH, result.kind)

    def test_encoded_known_scheme_classifies_known(self):
        self.recognizer()
        for text in (
            "h%74tps%3A%2F%2Fexample.com/path",
            "%68%74%74%70%73%3A%2F%2Fexample.com",
        ):
            with self.subTest(text=text):
                result = self.recognize(text)
                self.assertIs(RecognitionKind.MATCH, result.kind)
                self.assertIs(CandidateKind.KNOWN, result.candidate.kind)

    def test_encoded_unknown_scheme_classifies_unknown(self):
        result = self.recognize("%63ustom%253A%252F%252Fhost")
        self.assertIs(RecognitionKind.MATCH, result.kind)
        self.assertIs(CandidateKind.UNKNOWN, result.candidate.kind)

    def test_conservative_encoded_scheme_units_remain_candidates(self):
        # ``%3A``/``%2F`` units can be scheme units and separators; the legacy
        # detector accepts ``%20abc://`` as a candidate start.
        self.recognizer()
        for text in ("%20abc://host", "%31abc://host"):
            with self.subTest(text=text):
                result = self.recognize(text)
                self.assertIs(RecognitionKind.MATCH, result.kind)

    def test_malformed_escape_is_copied_literally(self):
        # ``%zz`` is not an escape, so the run is broken at the ``%`` and the
        # following literal ``zz://`` is still a candidate starting at ``z``.
        text = "custom%zz://host"
        result = self.recognize(text)
        self.assertIs(RecognitionKind.MATCH, result.kind)
        self.assertEqual(text.index("zz"), result.candidate.start)

    def test_malformed_escape_does_not_form_a_scheme_unit(self):
        # Without a following literal scheme, the broken run yields no match.
        self.assertIsNot(RecognitionKind.MATCH, self.recognize("a%zz").kind)
        self.assertIs(RecognitionKind.NO_MATCH, self.recognize("a%zz").kind)

    def test_incomplete_trailing_escape_reports_need_more(self):
        self.recognizer()
        for text in ("https%", "https%3", "git%"):
            with self.subTest(text=text):
                result = self.recognize(text)
                self.assertIs(RecognitionKind.NEED_MORE, result.kind)

    def test_encoded_separator_without_scheme_is_not_a_candidate(self):
        self.recognizer()
        for text in ("%3A%2F%2Fhost", "%253A%2F%2Fhost"):
            with self.subTest(text=text):
                self.assertIsNot(
                    RecognitionKind.MATCH, self.recognize(text).kind
                )


class DifferentialCorpusTests(_RecognizerCase):
    """Task 2.3 -- bounded deterministic differential corpus."""

    #: Curated raw/encoded inputs that exercise overlapping units, multibyte
    #: prefixes and earlier encoded versus later literal starts.
    _CURATED = (
        "",
        "a",
        "a b",
        "https://host",
        "https://host tail",
        "https://host\n",
        "note:/section",
        "npm:package",
        "git",
        "git+",
        "https:",
        "https:/",
        "https%",
        "https%3",
        "custom%3A%2F%2Fhost",
        "custom%253A%252F%252Fhost",
        "custom%25253A%25252F%25252Fhost",
        "custom%2525253A%2525252F%2525252Fhost",
        "custom%252525253A%252525252F%252525252Fhost",
        "custom:%2f/host",
        "custom%253A%2F/host",
        "custom%25%33%41%25%32%46/host",
        "custom%3a%2f%2fhost",
        "h%74tps%3A%2F%2Fexample.com/path",
        "%68%74%74%70%73%3A%2F%2Fexample.com",
        "%253A%2F%2Fhost",
        "%3A%2F%2Fhost",
        "abc%3A%2F%2Fa%253A%252F%252Fb",
        "%2525253A%2525252F%2525252Fhost",
        "a%253A",
        "a%zz",
        "a%z",
        "%",
        "%zz",
        "%31abc://x",
        "%20abc://x",
        "1a://x",
        "\u00e9https://host",
        "\u20acx%3A%2F%2Fhost",
        "a%2F%2Fb://c",
        "a%3A%3A%2F%2Fb",
        "://x",
        "a%2F%2Fb",
        "xy%25%33%41%25%32%46%25%32%46z",
    )

    #: Deterministic alphabet: scheme characters, escapes, separators,
    #: terminators and a multibyte character.
    _ALPHABET = (
        "a",
        "b",
        "H",
        "1",
        "2",
        "F",
        "A",
        ":",
        "/",
        ".",
        "+",
        "%",
        " ",
        "<",
        "\n",
        "\u00e9",
    )

    def assert_matches_legacy(self, text: str) -> None:
        expectation = _legacy_expectation(text)
        result = self.recognize(text)
        if expectation[0] == "match":
            self.assertIs(
                RecognitionKind.MATCH,
                result.kind,
                msg=f"legacy matched {text!r} at {expectation[1]}",
            )
            self.assertEqual(expectation[1], result.candidate.start)
            self.assertEqual(expectation[2], result.candidate.end)
        elif expectation[0] == "need_more":
            self.assertIs(
                RecognitionKind.NEED_MORE,
                result.kind,
                msg=f"legacy needs more for {text!r}",
            )
            self.assertEqual(expectation[1], result.retain_from)
        else:
            self.assertIs(
                RecognitionKind.NO_MATCH,
                result.kind,
                msg=f"legacy found nothing in {text!r}",
            )

    def test_curated_corpus_matches_legacy_detector(self):
        self.recognizer()
        for text in self._CURATED:
            with self.subTest(text=text):
                self.assert_matches_legacy(text)

    def test_deterministic_random_corpus_matches_legacy_detector(self):
        self.recognizer()
        rng = random.Random(20240607)
        for _ in range(4000):
            length = rng.randrange(0, 19)
            text = "".join(rng.choice(self._ALPHABET) for _ in range(length))
            with self.subTest(text=text):
                self.assert_matches_legacy(text)

    def test_deterministic_random_long_corpus_matches_legacy_detector(self):
        self.recognizer()
        rng = random.Random(987654321)
        for _ in range(400):
            length = rng.randrange(20, 60)
            text = "".join(rng.choice(self._ALPHABET) for _ in range(length))
            with self.subTest(text=text):
                self.assert_matches_legacy(text)


class WorkStateRegressionTests(_RecognizerCase):
    """Task 2.4 -- deterministic bounded work and state, no timing."""

    def test_work_grows_linearly_for_uninterrupted_scheme_runs(self):
        module = self.recognizer()
        small_result, small = module.recognize_with_stats("a" * 2000)
        large_result, large = module.recognize_with_stats("a" * 4000)
        self.assertIs(RecognitionKind.NEED_MORE, small_result.kind)
        self.assertIs(RecognitionKind.NEED_MORE, large_result.kind)
        self.assertEqual(2000, small.chars)
        self.assertEqual(4000, large.chars)
        self.assertGreater(small.emissions, 0)
        self.assertGreater(large.emissions, 0)
        self.assertLessEqual(
            large.layer_char_visits, 2 * small.layer_char_visits + 8
        )
        self.assertLessEqual(large.emissions, 2 * small.emissions + 8)
        self.assertLessEqual(large.atoms, 2 * small.atoms + 8)

    def test_work_grows_linearly_for_separated_runs(self):
        module = self.recognizer()
        small_text = "ab " * 1000
        large_text = "ab " * 2000
        _, small = module.recognize_with_stats(small_text)
        _, large = module.recognize_with_stats(large_text)
        self.assertLessEqual(
            large.layer_char_visits, 2 * small.layer_char_visits + 8
        )
        self.assertLessEqual(large.transitions, 2 * small.transitions + 8)

    def test_pending_state_is_bounded_for_a_long_token(self):
        module = self.recognizer()
        _, stats = module.recognize_with_stats("a" * 100_000)
        self.assertLessEqual(stats.peak_pending_chars, 4)
        self.assertLessEqual(stats.peak_atom_queue, 4)

    def test_long_escaped_token_state_is_bounded(self):
        module = self.recognizer()
        _, stats = module.recognize_with_stats("%41" * 20_000)
        self.assertLessEqual(stats.peak_pending_chars, 4)
        self.assertLessEqual(stats.peak_atom_queue, 4)

    def test_stats_report_only_bounded_structural_counters(self):
        module = self.recognizer()
        _, stats = module.recognize_with_stats("https://host tail")
        self.assertGreaterEqual(stats.chars, 16)
        self.assertGreaterEqual(stats.layer_char_visits, stats.chars)
        self.assertGreaterEqual(stats.atoms, 1)
        self.assertGreater(stats.emissions, 0)
        self.assertLess(stats.peak_pending_chars, 8)
        self.assertLess(stats.peak_atom_queue, 8)

    def test_representative_inputs_report_nonzero_emissions(self):
        module = self.recognizer()
        for text in (
            "a",
            "https://host",
            "%41",
            "a b c",
            "custom%3A%2F%2Fhost",
        ):
            with self.subTest(text=text):
                _, stats = module.recognize_with_stats(text)
                self.assertGreater(stats.emissions, 0)

    def test_empty_input_reports_no_emissions(self):
        module = self.recognizer()
        _, stats = module.recognize_with_stats("")
        self.assertEqual(0, stats.emissions)


class SourceRangeProvenanceTests(_RecognizerCase):
    """Task 2.6 -- emissions carry inclusive raw start and exclusive raw end."""

    def _single_layer_emissions(self, text: str):
        module = self.recognizer()
        stats = module._MutableStats()
        emissions = []

        def sink(character, source_start, source_end, from_token):
            emissions.append((character, source_start, source_end, from_token))

        transducer = module._Transducer(sink, stats)
        for index, character in enumerate(text):
            transducer.feed(character, index, index + 1, True)
        transducer.flush()
        return emissions

    def _nested_emissions(self, text: str):
        module = self.recognizer()
        stats = module._MutableStats()
        emissions = []

        def sink(character, source_start, source_end, from_token):
            emissions.append((character, source_start, source_end))

        inner = module._Transducer(sink, stats)
        outer = module._Transducer(
            lambda character, start, end, from_token: inner.feed(
                character, start, end, from_token
            ),
            stats,
        )
        for index, character in enumerate(text):
            outer.feed(character, index, index + 1, True)
        outer.flush()
        inner.flush()
        return emissions

    def test_literal_emissions_carry_single_character_ranges(self):
        self.assertEqual(
            [("a", 0, 1, True), ("b", 1, 2, True), ("c", 2, 3, True)],
            self._single_layer_emissions("abc"),
        )

    def test_decoded_escape_spans_all_three_raw_characters(self):
        self.assertEqual(
            [("A", 0, 3, True)], self._single_layer_emissions("%41")
        )

    def test_malformed_escape_preserves_literal_raw_ranges(self):
        self.assertEqual(
            [("%", 0, 1, True), ("z", 1, 2, True), ("z", 2, 3, True)],
            self._single_layer_emissions("%zz"),
        )

    def test_invalid_hex_escape_preserves_literal_raw_ranges(self):
        self.assertEqual(
            [("%", 0, 1, True), ("4", 1, 2, True), ("G", 2, 3, True)],
            self._single_layer_emissions("%4G"),
        )

    def test_trailing_incomplete_escape_preserves_literal_raw_ranges(self):
        self.assertEqual(
            [("%", 0, 1, True), ("4", 1, 2, True)],
            self._single_layer_emissions("%4"),
        )

    def test_nested_decode_range_spans_all_contributing_raw_characters(self):
        self.assertEqual([(":", 0, 5)], self._nested_emissions("%253A"))

    def test_match_observer_retains_unit_and_colon_raw_ranges(self):
        module = self.recognizer()
        pipeline = module._Pipeline(module._MutableStats())
        pipeline.feed("https://host")
        pipeline.finish()
        observer = pipeline._match[0]
        self.assertEqual(
            (0, 1), (observer.match_start, observer.match_start_end)
        )
        self.assertEqual(
            (5, 6), (observer.match_colon, observer.match_colon_end)
        )

    def test_encoded_separator_range_covers_all_three_raw_characters(self):
        module = self.recognizer()
        pipeline = module._Pipeline(module._MutableStats())
        pipeline.feed("custom%3A%2F%2Fhost")
        pipeline.finish()
        observer = pipeline._match[0]
        self.assertEqual(
            (0, 1), (observer.match_start, observer.match_start_end)
        )
        self.assertEqual(
            (6, 9), (observer.match_colon, observer.match_colon_end)
        )

    def test_range_provenance_keeps_candidate_coordinates(self):
        cases = {
            "https://host tail": len("https://host"),
            "custom%3A%2F%2Fhost tail": len("custom%3A%2F%2Fhost"),
            "%68ttps://host tail": len("%68ttps://host"),
        }
        for text, end in cases.items():
            with self.subTest(text=text):
                result = self.recognize(text)
                self.assertIs(RecognitionKind.MATCH, result.kind)
                self.assertEqual(0, result.candidate.start)
                self.assertEqual(end, result.candidate.end)


class ContractIntegrationTests(_RecognizerCase):
    """The recognizer emits the P1 contract types with raw coordinates."""

    def test_match_emits_a_p1_recognition_result(self):
        result = self.recognize("https://host")
        self.assertIsInstance(result, RecognitionResult)
        self.assertIs(RecognitionKind.MATCH, result.kind)

    def test_recognizer_exposes_a_callable_batch_entry_point(self):
        module = self.recognizer()
        self.assertTrue(callable(module.recognize))
        self.assertTrue(callable(module.recognize_with_stats))

    def test_recognizer_has_no_legacy_regex_or_detector_fallback(self):
        # P2 allocation/fallback audit (task 2.10): the recognizer must not
        # depend on the legacy unanchored regex, the legacy detector helper or
        # a variant table.
        module = self.recognizer()
        self.assertFalse(hasattr(module, "re"))
        self.assertNotIn("_URL_START_RE", vars(module))
        self.assertNotIn("_find_url_start", vars(module))
        self.assertNotIn("_decode_percent_layer", vars(module))
        for name in vars(module):
            self.assertNotIn("VARIANT", name.upper())


if __name__ == "__main__":
    unittest.main()
