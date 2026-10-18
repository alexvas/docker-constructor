"""Phase 2 (P2) tests for the supported-scheme diagnostic URL recognizer.

These tests bind the revised RED obligations for Phase 2 of
``linearize-diagnostic-url-detection``:

* supported-only raw recognition: literal separators, negative ``:/``/``npm:``
  forms, unknown text, embedded supported starts, earliest supported source
  selection, raw terminator ends and absolute raw coordinates (task 2.1);
* encoded recognition at detection depths zero through four, mixed depths, hex
  case and separately encoded escapes, without granting a fourth
  interpretation pass (task 2.2);
* a bounded deterministic differential corpus against an independent
  decode-then-search oracle, plus literal/decoded secret selection,
  scheme-prefix suffix overlap and secrets crossing raw terminators with
  explicit expected coverage (task 2.3); and
* deterministic bounded work/state and raw-provenance checks with no
  elapsed-time measurement, input-sized search map or encoded-variant
  enumeration (task 2.4).

The recognizer module is greenfield, so before it exists every test fails with
an explicit assertion naming the missing recognizer rather than a collection
error.

The oracle is a deliberately naive, independent implementation (decode each
view up to three passes for the scheme and up to four for the separator, then
search for a supported scheme immediately followed by ``://``); the production
recognizer never imports it and never materialises a decoded view.
"""

from __future__ import annotations

import importlib
import random
import unittest

from docker.versioning.diagnostic_recognition import (
    RecognitionKind,
    RecognitionResult,
    SUPPORTED_SCHEMES,
)

_MODULE_NAME = "docker.versioning.diagnostic_recognizer"

_HEX = frozenset("0123456789abcdefABCDEF")
_SCHEMES = tuple(sorted(SUPPORTED_SCHEMES))

#: Legacy "no viable prefix" integer state for compatibility callers.
_PREFIX_NONE = 0


def _load_recognizer():
    try:
        return importlib.import_module(_MODULE_NAME)
    except ModuleNotFoundError:
        return None


def _decode_layer(text: str) -> tuple[str, list[tuple[int, int]]]:
    """Decode one percent pass, returning decoded text and raw ranges."""
    out: list[str] = []
    ranges: list[tuple[int, int]] = []
    index = 0
    length = len(text)
    while index < length:
        character = text[index]
        if (
            character == "%"
            and index + 2 < length
            and text[index + 1] in _HEX
            and text[index + 2] in _HEX
        ):
            out.append(chr(int(text[index + 1 : index + 3], 16)))
            ranges.append((index, index + 3))
            index += 3
        else:
            out.append(character)
            ranges.append((index, index + 1))
            index += 1
    return "".join(out), ranges


def _multi_layer(text: str, depth: int) -> tuple[str, list[tuple[int, int]]]:
    """Decode *depth* passes, mapping every decoded character to its raw span."""
    current = text
    current_ranges = [(index, index + 1) for index in range(len(text))]
    for _ in range(depth):
        decoded, decoded_ranges = _decode_layer(current)
        merged: list[tuple[int, int]] = []
        for start, end in decoded_ranges:
            merged.append((current_ranges[start][0], current_ranges[end - 1][1]))
        current, current_ranges = decoded, merged
    return current, current_ranges


def _ascii_fold(text: str) -> str:
    """ASCII-lowercase *text* without changing its length."""
    return "".join(
        chr(ord(character) + 32) if "A" <= character <= "Z" else character
        for character in text
    )


def _has_separator_at(
    deep: str, deep_ranges: list[tuple[int, int]], raw_end: int
) -> bool:
    """Return whether ``://`` begins at raw offset *raw_end* in *deep*."""
    for position, (start, _end) in enumerate(deep_ranges):
        if start == raw_end:
            return deep[position : position + 3] == "://"
        if start > raw_end:
            return False
    return False


def _oracle_start(text: str) -> int | None:
    """Earliest supported raw start with scheme <=3 and separator <=4 passes.

    A scheme and its separator are only combined when both are legible in the
    same decoding view: layers zero through two search the combined
    ``scheme://`` in the one-, two- and three-times-decoded views.  The last
    layer searches the scheme in the three-times-decoded view (a fourth pass
    would decode scheme characters) and locates only the separator in the
    four-times-decoded view.  This mirrors the recognizer's split between
    three-pass scheme interpretation and the one additional encoded-separator
    step rather than decoding the whole URL prefix four times.
    """
    best: int | None = None
    for depth in range(1, 4):
        decoded, ranges = _multi_layer(text, depth)
        folded = _ascii_fold(decoded)
        for scheme in _SCHEMES:
            index = folded.find(scheme + "://")
            while index != -1:
                raw_start = ranges[index][0]
                if best is None or raw_start < best:
                    best = raw_start
                index = folded.find(scheme + "://", index + 1)
    literal, literal_ranges = _multi_layer(text, 3)
    literal_folded = _ascii_fold(literal)
    deep, deep_ranges = _multi_layer(text, 4)
    for scheme in _SCHEMES:
        index = literal_folded.find(scheme)
        while index != -1:
            raw_end = literal_ranges[index + len(scheme) - 1][1]
            if _has_separator_at(deep, deep_ranges, raw_end):
                raw_start = literal_ranges[index][0]
                if best is None or raw_start < best:
                    best = raw_start
            index = literal_folded.find(scheme, index + 1)
    return best


def _percent_encode(text: str, *, uppercase: bool = True) -> str:
    template = "%{:02X}" if uppercase else "%{:02x}"
    return "".join(template.format(ord(character)) for character in text)


def _encode_depth(text: str, depth: int, *, uppercase: bool = True) -> str:
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
            self.fail(f"{_MODULE_NAME} must define the P2 recognizer")
        return module

    def recognize(self, text: str, *, offset: int = 0) -> RecognitionResult:
        return self.recognizer().recognize(text, offset=offset)

    def recognize_secrets(self, text: str, secrets, *, offset: int = 0):
        return self.recognizer().recognize_with_secrets(
            text, tuple(secrets), offset=offset
        )


class SupportedSchemeTests(_RecognizerCase):
    """Task 2.1 -- supported-only raw recognition and source coordinates."""

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
        result = self.recognize("see https://host/path")
        self.assertIs(RecognitionKind.MATCH, result.kind)
        self.assertIsNone(result.candidate.end)

    def test_terminator_variants_end_the_candidate(self):
        for separator in (" ", "\t", "\n", "\r", "<", "|", '"', "\x01"):
            with self.subTest(separator=repr(separator)):
                text = f"https://host{separator}tail"
                result = self.recognize(text)
                self.assertIs(RecognitionKind.MATCH, result.kind)
                self.assertEqual(len("https://host"), result.candidate.end)

    def test_single_slash_does_not_complete_the_separator(self):
        result = self.recognize("note:/section")
        self.assertIsNot(RecognitionKind.MATCH, result.kind)

    def test_npm_colon_form_does_not_complete_the_separator(self):
        result = self.recognize("npm:package")
        self.assertIsNot(RecognitionKind.MATCH, result.kind)

    def test_every_supported_scheme_is_detected(self):
        self.recognizer()
        for scheme in _SCHEMES:
            with self.subTest(scheme=scheme):
                result = self.recognize(f"{scheme}://host tail")
                self.assertIs(RecognitionKind.MATCH, result.kind)
                self.assertEqual(0, result.candidate.start)
                self.assertEqual(len(scheme) + len("://host"), result.candidate.end)

    def test_unknown_schemes_are_not_candidates(self):
        self.recognizer()
        for scheme in (
            "custom",
            "custom+transport",
            "gopher",
            "npm",
            "httpx",
            "ftp2",
            "socks6",
        ):
            with self.subTest(scheme=scheme):
                result = self.recognize(f"{scheme}://internal.example/private")
                self.assertIs(RecognitionKind.NO_MATCH, result.kind)

    def test_arbitrarily_long_unknown_scheme_is_not_detected(self):
        scheme = "custom" + "x" * 4096
        result = self.recognize(f"{scheme}://internal.example/private")
        self.assertIs(RecognitionKind.NO_MATCH, result.kind)

    def test_embedded_supported_start_is_selected(self):
        text = "TOKENhttps://host/public"
        result = self.recognize(text)
        self.assertIs(RecognitionKind.MATCH, result.kind)
        self.assertEqual(text.index("https"), result.candidate.start)

    def test_earliest_supported_start_wins_over_earlier_unknown_text(self):
        text = "custom://internal https://registry.example.com"
        result = self.recognize(text)
        self.assertIs(RecognitionKind.MATCH, result.kind)
        self.assertEqual(text.index("https"), result.candidate.start)

    def test_earliest_supported_start_wins_between_two_supported_urls(self):
        text = "ftp://a https://b"
        result = self.recognize(text)
        self.assertIs(RecognitionKind.MATCH, result.kind)
        self.assertEqual(0, result.candidate.start)

    def test_offset_is_added_to_absolute_coordinates(self):
        result = self.recognize("https://host tail", offset=100)
        self.assertIs(RecognitionKind.MATCH, result.kind)
        self.assertEqual(100, result.candidate.start)
        self.assertEqual(100 + len("https://host"), result.candidate.end)

    def test_ordinary_tokens_without_a_supported_prefix_return_no_match(self):
        self.recognizer()
        for text in ("ordinary 42", "GET 200 3", "a b 9", "héllo", "xyzzy"):
            with self.subTest(text=text):
                self.assertIs(RecognitionKind.NO_MATCH, self.recognize(text).kind)

    def test_supported_prefix_reports_need_more(self):
        self.recognizer()
        for text in ("h", "ht", "http", "https", "git+", "socks5"):
            with self.subTest(text=text):
                result = self.recognize(text)
                self.assertIs(RecognitionKind.NEED_MORE, result.kind)
                self.assertEqual(0, result.retain_from)

    def test_started_separator_prefix_reports_need_more(self):
        self.recognizer()
        for text in ("https:", "https:/", "git+ssh:"):
            with self.subTest(text=text):
                result = self.recognize(text)
                self.assertIs(RecognitionKind.NEED_MORE, result.kind)
                self.assertEqual(0, result.retain_from)

    def test_unsupported_prefix_is_not_need_more(self):
        self.recognizer()
        for text in ("note:/section", "custom", "npm:", "httpX", "socks6"):
            with self.subTest(text=text):
                self.assertIs(RecognitionKind.NO_MATCH, self.recognize(text).kind)

    def test_retention_restarts_at_a_trailing_supported_prefix(self):
        # ``git+g`` is not a supported prefix as a whole, but the trailing
        # ``g`` can still begin ``git``, so the earliest viable start is at 4.
        text = "git+g"
        result = self.recognize(text)
        self.assertIs(RecognitionKind.NEED_MORE, result.kind)
        self.assertEqual(4, result.retain_from)

    def test_trailing_token_retention_is_the_earliest_supported_start(self):
        result = self.recognize("alpha beta git+")
        self.assertIs(RecognitionKind.NEED_MORE, result.kind)
        self.assertEqual("alpha beta git+".index("git"), result.retain_from)

    def test_retention_skips_non_scheme_prefix_of_a_token(self):
        text = "xxhttps:"
        result = self.recognize(text)
        self.assertIs(RecognitionKind.NEED_MORE, result.kind)
        self.assertEqual(text.index("https"), result.retain_from)

    def test_multibyte_prefix_offsets_are_character_indices(self):
        text = "\u00e9\u20ac https://host"
        result = self.recognize(text)
        self.assertIs(RecognitionKind.MATCH, result.kind)
        self.assertEqual(3, result.candidate.start)
        self.assertEqual("https://host", text[3:])


class EncodedSchemeTests(_RecognizerCase):
    """Task 2.2 -- scheme passes <=3 and separator depth <=4."""

    def test_scheme_and_separator_at_every_permitted_scheme_depth(self):
        module = self.recognizer()
        for scheme in _SCHEMES:
            for depth in range(module._LAYERS):
                with self.subTest(scheme=scheme, depth=depth):
                    text = (
                        _encode_depth(scheme, depth)
                        + _encode_depth("://", depth)
                        + "host tail"
                    )
                    result = self.recognize(text)
                    self.assertIs(RecognitionKind.MATCH, result.kind)
                    self.assertEqual(0, result.candidate.start)
                    self.assertEqual(
                        len(text) - len(" tail"),
                        result.candidate.end,
                    )

    def test_scheme_encoded_at_every_permitted_depth_with_literal_separator(self):
        module = self.recognizer()
        for depth in range(module._LAYERS):
            with self.subTest(depth=depth):
                text = _encode_depth("https", depth) + "://host"
                result = self.recognize(text)
                self.assertIs(RecognitionKind.MATCH, result.kind)
                self.assertEqual(0, result.candidate.start)

    def test_four_pass_encoded_scheme_with_literal_separator_is_not_detected(self):
        module = self.recognizer()
        for scheme in _SCHEMES:
            with self.subTest(scheme=scheme):
                text = _encode_depth(scheme, module._LAYERS) + "://host"
                self.assertIs(RecognitionKind.NO_MATCH, self.recognize(text).kind)

    def test_four_pass_scheme_and_separator_is_not_detected(self):
        module = self.recognizer()
        depth = module._LAYERS
        text = _encode_depth("https", depth) + _encode_depth("://", depth) + "host"
        self.assertIs(RecognitionKind.NO_MATCH, self.recognize(text).kind)

    def test_separator_encoded_at_every_depth_over_literal_scheme(self):
        module = self.recognizer()
        for depth in range(module._LAYERS + 1):
            with self.subTest(depth=depth):
                text = "https" + _encode_depth("://", depth) + "host"
                result = self.recognize(text)
                self.assertIs(RecognitionKind.MATCH, result.kind)
                self.assertEqual(0, result.candidate.start)

    def test_depth_four_separator_over_permitted_encoded_scheme(self):
        module = self.recognizer()
        separator = _encode_depth("://", module._LAYERS)
        for depth in range(module._LAYERS):
            with self.subTest(depth=depth):
                text = _encode_depth("https", depth) + separator + "host"
                result = self.recognize(text)
                self.assertIs(RecognitionKind.MATCH, result.kind)
                self.assertEqual(0, result.candidate.start)

    def test_depth_five_separator_is_not_recognized(self):
        module = self.recognizer()
        text = "https" + _encode_depth("://", module._LAYERS + 1) + "host"
        self.assertIsNot(RecognitionKind.MATCH, self.recognize(text).kind)

    def test_mixed_depth_separators_are_recognized(self):
        self.recognizer()
        for separator in (":%2f/", "%3A%2F/", "%253A//", "%3A//"):
            with self.subTest(separator=separator):
                result = self.recognize(f"https{separator}host")
                self.assertIs(RecognitionKind.MATCH, result.kind)

    def test_separately_encoded_escape_characters(self):
        result = self.recognize("https%25%33%41%25%32%46/host")
        self.assertIs(RecognitionKind.MATCH, result.kind)

    def test_hex_letter_case_is_recognized(self):
        self.recognizer()
        for separator in ("%3A%2F%2F", "%3a%2f%2f", "%3A%2f%2F"):
            with self.subTest(separator=separator):
                result = self.recognize(f"https{separator}host")
                self.assertIs(RecognitionKind.MATCH, result.kind)

    def test_uppercase_scheme_spellings_are_recognized(self):
        self.recognizer()
        for text in ("HTTPS://host", "HtTpS://host", "GIT+SSH://host"):
            with self.subTest(text=text):
                result = self.recognize(text)
                self.assertIs(RecognitionKind.MATCH, result.kind)

    def test_encoded_uppercase_scheme_is_recognized(self):
        result = self.recognize(_encode_depth("HTTPS", 1) + "://host")
        self.assertIs(RecognitionKind.MATCH, result.kind)

    def test_encoded_unknown_scheme_is_not_a_candidate(self):
        result = self.recognize("%63ustom%253A%252F%252Fhost")
        self.assertIs(RecognitionKind.NO_MATCH, result.kind)

    def test_conservative_encoded_units_do_not_manufacture_a_scheme(self):
        # ``%20abc://`` / ``%31abc://`` decoded to `` abc://`` / ``1abc://``;
        # neither is a supported scheme, so neither is a candidate.
        self.recognizer()
        for text in ("%20abc://host", "%31abc://host"):
            with self.subTest(text=text):
                self.assertIs(RecognitionKind.NO_MATCH, self.recognize(text).kind)

    def test_malformed_escape_does_not_form_a_candidate(self):
        self.assertIs(RecognitionKind.NO_MATCH, self.recognize("a%zz").kind)

    def test_incomplete_trailing_escape_reports_need_more(self):
        self.recognizer()
        for text in ("https%", "https%3", "git%", "https%3A%2F%"):
            with self.subTest(text=text):
                result = self.recognize(text)
                self.assertIs(RecognitionKind.NEED_MORE, result.kind)

    def test_encoded_separator_without_supported_scheme_is_not_a_candidate(self):
        self.recognizer()
        for text in ("%3A%2F%2Fhost", "%253A%2F%2Fhost", "://x"):
            with self.subTest(text=text):
                self.assertIs(RecognitionKind.NO_MATCH, self.recognize(text).kind)


class DifferentialCorpusTests(_RecognizerCase):
    """Task 2.3 -- bounded deterministic differential corpus vs the oracle."""

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
        "custom://internal",
        "gopher://x",
        "npm://x",
        "httpx://x",
        "TOKENhttps://host",
        "xxhttps:/",
        "custom%3A%2F%2Fhost",
        "h%74tps%3A%2F%2Fexample.com/path",
        "%68%74%74%70%73%3A%2F%2Fexample.com",
        "https%253A%2F/host",
        "https:%2f/host",
        "https%3A%2F/host",
        "https%25%33%41%25%32%46/host",
        "%253A%2F%2Fhost",
        "%3A%2F%2Fhost",
        "abc%3A%2F%2Fa%253A%252F%252Fb",
        "https%2525253A%2525252F%2525252Fhost",
        "https%252525253A%252525252F%252525252Fhost",
        "%1%66%74%70%2525253A%2525252F%2525252Fhost",
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

    def assert_matches_oracle(self, text: str) -> None:
        expected = _oracle_start(text)
        result = self.recognize(text)
        if expected is None:
            if result.kind is RecognitionKind.MATCH:
                self.fail(
                    f"oracle found no candidate in {text!r} but got {result.candidate}"
                )
        else:
            self.assertIs(
                RecognitionKind.MATCH,
                result.kind,
                msg=f"oracle matched {text!r} at {expected}",
            )
            self.assertEqual(expected, result.candidate.start)

    def test_curated_corpus_matches_oracle(self):
        self.recognizer()
        for text in self._CURATED:
            with self.subTest(text=text):
                self.assert_matches_oracle(text)

    def test_deterministic_random_corpus_matches_oracle(self):
        self.recognizer()
        rng = random.Random(20240607)
        for _ in range(4000):
            length = rng.randrange(0, 19)
            text = "".join(rng.choice(self._ALPHABET) for _ in range(length))
            with self.subTest(text=text):
                self.assert_matches_oracle(text)

    def test_deterministic_random_long_corpus_matches_oracle(self):
        self.recognizer()
        rng = random.Random(987654321)
        for _ in range(400):
            length = rng.randrange(20, 60)
            text = "".join(rng.choice(self._ALPHABET) for _ in range(length))
            with self.subTest(text=text):
                self.assert_matches_oracle(text)

    def test_deterministic_supported_spelling_corpus_matches_oracle(self):
        self.recognizer()
        rng = random.Random(20240613)
        for _ in range(2000):
            prefix = "".join(
                rng.choice(self._ALPHABET) for _ in range(rng.randrange(0, 6))
            )
            scheme = rng.choice(_SCHEMES)
            scheme_depth = rng.randrange(0, 6)
            separator_depth = rng.randrange(0, 6)
            text = (
                prefix
                + _encode_depth(scheme, scheme_depth)
                + _encode_depth("://", separator_depth)
                + "host"
            )
            with self.subTest(text=text):
                self.assert_matches_oracle(text)


class LiteralSecretTests(_RecognizerCase):
    """Task 2.3 -- literal secret selection and cross-terminator coverage."""

    def test_literal_secret_outside_a_url_is_reported(self):
        _result, matches = self.recognize_secrets("before TOKEN after", ("TOKEN",))
        self.assertEqual(1, len(matches))
        match = matches[0]
        self.assertEqual(0, match.pattern_index)
        self.assertEqual(0, match.depth)
        self.assertEqual("TOKEN", "before TOKEN after"[match.start : match.end])

    def test_literal_secret_crosses_a_raw_terminator(self):
        text = "https://host/password done"
        _result, matches = self.recognize_secrets(text, ("password done",))
        self.assertEqual(1, len(matches))
        self.assertEqual(0, matches[0].depth)
        self.assertEqual("password done", text[matches[0].start : matches[0].end])

    def test_literal_secret_with_a_space_is_not_rejected(self):
        _result, matches = self.recognize_secrets(
            "x TOKEN with space y", ("TOKEN with space",)
        )
        self.assertEqual(1, len(matches))

    def test_literal_secret_that_is_a_full_scheme_suffix_is_not_rejected(self):
        text = "plain https://host/schemehttps tail"
        _result, matches = self.recognize_secrets(text, ("schemehttps",))
        self.assertEqual(1, len(matches))
        self.assertEqual("schemehttps", text[matches[0].start : matches[0].end])

    def test_secret_selects_leftmost_longest(self):
        module = self.recognizer()
        matches = (
            module.SecretMatch(0, 0, 3, 1),
            module.SecretMatch(1, 0, 5, 0),
            module.SecretMatch(2, 6, 9, 0),
        )
        selected = module.select_secret_matches(matches)
        self.assertEqual([(0, 5), (6, 9)], [(m.start, m.end) for m in selected])

    def test_overlapping_literal_secrets_are_deduped_by_span(self):
        text = "TOKEN"
        _result, matches = self.recognize_secrets(text, ("TOKEN", "TOKEN"))
        self.assertEqual(1, len(matches))

    def test_ordinary_text_without_a_url_stays_literal_only(self):
        text = "%54%4f%4b%45%4e"
        _result, matches = self.recognize_secrets(text, ("TOKEN",))
        self.assertEqual((), matches)


class UrlDecodedSecretTests(_RecognizerCase):
    """Task 2.8 -- decoded secret matching inside recognized URL views."""

    def test_decoded_secret_in_a_url_path_is_reported(self):
        text = "https://host/%54%4f%4b%45%4e/private"
        _result, matches = self.recognize_secrets(text, ("TOKEN",))
        self.assertEqual(1, len(matches))
        match = matches[0]
        self.assertEqual(1, match.depth)
        self.assertEqual("%54%4f%4b%45%4e", text[match.start : match.end])

    def test_secret_found_at_two_decoded_depths_is_reported_once(self):
        text = "https://host/%2554%254f%254b%2545%254e/private"
        _result, matches = self.recognize_secrets(text, ("TOKEN",))
        self.assertEqual(1, len(matches))
        self.assertEqual(2, matches[0].depth)

    def test_encoded_decoding_does_not_exceed_three_passes(self):
        text = "https://host/" + _encode_depth("TOKEN", 4) + "/private"
        _result, matches = self.recognize_secrets(text, ("TOKEN",))
        self.assertEqual((), matches)

    def test_literal_secret_inside_a_url_is_reported_at_depth_zero(self):
        text = "https://host/TOKEN/private"
        _result, matches = self.recognize_secrets(text, ("TOKEN",))
        self.assertEqual(1, len(matches))
        self.assertEqual(0, matches[0].depth)
        self.assertEqual("TOKEN", text[matches[0].start : matches[0].end])

    def test_encoded_secret_without_a_url_is_not_decoded(self):
        text = "%54%4f%4b%45%4e"
        result = self.recognize(text)
        self.assertIs(RecognitionKind.NO_MATCH, result.kind)
        _result, matches = self.recognize_secrets(text, ("TOKEN",))
        self.assertEqual((), matches)

    def test_decoded_secret_is_confined_to_the_recognized_span(self):
        text = "%54OKEN https://host/%54OKEN"
        _result, matches = self.recognize_secrets(text, ("TOKEN",))
        self.assertEqual(1, len(matches))
        self.assertEqual(1, matches[0].depth)
        # The only decoded match is the one after the URL's path separator; the
        # identical encoded text before the URL is ordinary text.
        self.assertEqual("/", text[matches[0].start - 1])
        self.assertEqual(text.rindex("/") + 1, matches[0].start)
        self.assertEqual("%54OKEN", text[matches[0].start : matches[0].end])

    def test_decoded_secret_ending_at_a_raw_terminator_is_reported(self):
        text = "https://host/%54OKEN tail"
        _result, matches = self.recognize_secrets(text, ("TOKEN",))
        self.assertEqual(1, len(matches))
        self.assertEqual("%54OKEN", text[matches[0].start : matches[0].end])


class SchemePrefixSuffixOverlapTests(_RecognizerCase):
    """Task 2.7 -- overlapping starts are retained, not replayed."""

    def test_secret_overlapping_a_scheme_prefix_is_covered_and_url_kept(self):
        text = "bla-bla-blaTOKENhttps://host"
        result = self.recognize(text)
        self.assertIs(RecognitionKind.MATCH, result.kind)
        self.assertEqual(text.index("https"), result.candidate.start)
        _result, matches = self.recognize_secrets(text, ("TOKENht",))
        self.assertEqual(1, len(matches))
        self.assertEqual("TOKENht", text[matches[0].start : matches[0].end])

    def test_url_start_inside_a_covered_secret_still_recognized(self):
        text = "TOKENhttps://host"
        _result, matches = self.recognize_secrets(text, ("TOKEN",))
        result = self.recognize(text)
        self.assertIs(RecognitionKind.MATCH, result.kind)
        self.assertEqual(text.index("https"), result.candidate.start)
        self.assertEqual("TOKEN", text[matches[0].start : matches[0].end])

    def test_partition_invariance_with_registered_secrets(self):
        module = self.recognizer()
        text = "token=supersecret https://a.example/x next"
        secrets = ("supersecret",)
        baseline_result, baseline = module.recognize_with_secrets(text, secrets)
        for split in range(len(text) + 1):
            with self.subTest(split=split):
                recognizer = module.IncrementalRecognizer(secrets)
                recognizer.feed(text[:split])
                recognizer.feed(text[split:])
                recognizer.finalize()
                result = recognizer.result(text)
                self.assertEqual(baseline_result, result)


class WorkStateRegressionTests(_RecognizerCase):
    """Task 2.4 -- deterministic bounded work and state, no timing."""

    def test_work_grows_linearly_for_uninterrupted_scheme_runs(self):
        module = self.recognizer()
        small_result, small = module.recognize_with_stats("h" * 2000)
        large_result, large = module.recognize_with_stats("h" * 4000)
        self.assertIs(RecognitionKind.NEED_MORE, small_result.kind)
        self.assertIs(RecognitionKind.NEED_MORE, large_result.kind)
        self.assertEqual(2000, small.chars)
        self.assertEqual(4000, large.chars)
        self.assertGreater(small.emissions, 0)
        self.assertGreater(large.emissions, 0)
        self.assertLessEqual(large.layer_char_visits, 2 * small.layer_char_visits + 8)
        self.assertLessEqual(large.emissions, 2 * small.emissions + 8)
        self.assertLessEqual(large.atoms, 2 * small.atoms + 8)

    def test_work_grows_linearly_for_separated_runs(self):
        module = self.recognizer()
        _, small = module.recognize_with_stats("ab " * 1000)
        _, large = module.recognize_with_stats("ab " * 2000)
        self.assertLessEqual(large.layer_char_visits, 2 * small.layer_char_visits + 8)
        self.assertLessEqual(large.atoms, 2 * small.atoms + 8)

    def test_pending_and_queue_state_is_bounded_for_long_tokens(self):
        module = self.recognizer()
        for text in ("a" * 100_000, "%41" * 20_000, "https" + "x" * 50_000):
            with self.subTest(text=text[:16]):
                _, stats = module.recognize_with_stats(text)
                self.assertLessEqual(stats.peak_pending_chars, 4)
                self.assertLessEqual(stats.peak_atom_queue, 4)

    def test_batch_recognition_retains_a_constant_number_of_matches(self):
        module = self.recognizer()
        for count in (1, 100, 5000):
            with self.subTest(count=count):
                result, stats = module.recognize_with_stats("https://h " * count)
                self.assertIs(RecognitionKind.MATCH, result.kind)
                self.assertLessEqual(stats.peak_retained_matches, 1)

    def test_secret_work_is_structural_and_nonzero(self):
        module = self.recognizer()
        _result, stats = module.recognize_with_stats("a" * 100)
        self.assertEqual(0, stats.secret_emissions)
        recognizer = module.IncrementalRecognizer(("TOKEN",))
        recognizer.feed("TOKEN")
        self.assertGreater(recognizer.stats.secret_emissions, 0)

    def test_registration_storage_is_bounded_by_the_scheme_set(self):
        module = self.recognizer()
        bound = 1 + sum(len(scheme) for scheme in _SCHEMES)
        self.assertLessEqual(module._SCHEME_AUTOMATON.node_count, bound)


class SecretRetentionWorkTests(_RecognizerCase):
    """Task 2.10 -- bounded decoded-secret speculation and coverage work."""

    def test_decoded_secret_retention_does_not_grow_with_ordinary_text(self):
        module = self.recognizer()
        peaks = []
        for repeats in (10, 1000, 20_000):
            with self.subTest(repeats=repeats):
                recognizer = module.IncrementalRecognizer(("TOKEN",))
                recognizer.feed("%54OKEN" * repeats)
                self.assertEqual((), recognizer.decoded_secrets())
                self.assertEqual(0, recognizer.stats.retained_secrets)
                peaks.append(recognizer.stats.peak_retained_secrets)
        self.assertEqual([peaks[0]] * len(peaks), peaks)

    def test_decoded_secret_inside_a_url_keeps_its_raw_span(self):
        module = self.recognizer()
        text = "https://host/%54OKEN/private"
        recognizer = module.IncrementalRecognizer(("TOKEN",))
        recognizer.feed(text)
        matches = recognizer.decoded_secrets()
        self.assertEqual(1, len(matches))
        self.assertEqual(1, matches[0].depth)
        self.assertEqual("%54OKEN", text[matches[0].start : matches[0].end])

    def test_decoded_secret_after_an_abandoned_prefix_is_discarded(self):
        module = self.recognizer()
        recognizer = module.IncrementalRecognizer(("TOKEN",))
        recognizer.feed("httpsx/%54OKEN")
        self.assertEqual((), recognizer.decoded_secrets())

    def test_coverage_selection_is_deterministic_for_many_urls(self):
        module = self.recognizer()
        text = " ".join(f"https://h{index:03d}/TOKEN" for index in range(50))
        result, matches = module.recognize_with_secrets(text, ("TOKEN",))
        self.assertIs(RecognitionKind.MATCH, result.kind)
        selected = module.select_secret_matches(matches)
        self.assertEqual(50, len(selected))
        self.assertEqual(50, len({(m.start, m.end) for m in selected}))
        _result, again = module.recognize_with_secrets(text, ("TOKEN",))
        self.assertEqual(
            [(m.start, m.end, m.depth) for m in matches],
            [(m.start, m.end, m.depth) for m in again],
        )

    def test_cumulative_work_is_linear_across_commits(self):
        module = self.recognizer()

        def run(count: int):
            recognizer = module.IncrementalRecognizer(("TOKEN",))
            for index in range(count):
                recognizer.feed(f"https://h{index:03d}/TOKEN ")
                recognizer.consume(recognizer.consumed)
            return recognizer.stats

        small = run(50)
        large = run(100)
        self.assertGreater(small.transitions, 0)
        self.assertLessEqual(large.layer_char_visits, 2 * small.layer_char_visits + 8)
        self.assertLessEqual(large.emissions, 2 * small.emissions + 8)
        self.assertLessEqual(large.transitions, 2 * small.transitions + 8)
        self.assertLessEqual(large.secret_emissions, 2 * small.secret_emissions + 8)

    def test_speculative_pruning_work_is_linear_for_long_url_paths(self):
        module = self.recognizer()
        visits = []
        for length in (100, 200):
            with self.subTest(length=length):
                text = "https://host/" + "a" * length
                recognizer = module.IncrementalRecognizer(("a",))
                recognizer.feed(text)
                decoded = recognizer.decoded_secrets()
                self.assertEqual(length, len(decoded))
                path = text.rindex("/") + 1
                self.assertEqual(
                    list(range(path, path + length)),
                    [match.start for match in decoded],
                )
                self.assertEqual(
                    [(match.start, match.start + 1) for match in decoded],
                    [(match.start, match.end) for match in decoded],
                )
                visits.append(recognizer.stats.speculative_record_visits)
        self.assertGreater(visits[0], 0)
        self.assertLessEqual(visits[1], 2 * visits[0] + 8)

    def test_speculative_pruning_work_is_linear_with_consumption_boundaries(self):
        module = self.recognizer()
        visits = []
        for length in (100, 200):
            with self.subTest(length=length):
                recognizer = module.IncrementalRecognizer(("a",))
                for character in "https://host/" + "a" * length:
                    recognizer.feed(character)
                    recognizer.consume(recognizer.consumed)
                self.assertGreater(recognizer.stats.secret_emissions, length)
                self.assertEqual(0, len(recognizer.decoded_secrets()))
                visits.append(recognizer.stats.speculative_record_visits)
        self.assertGreater(visits[0], 0)
        self.assertLessEqual(visits[1], 2 * visits[0] + 8)

    def test_pruning_counters_are_preserved_across_consumption(self):
        module = self.recognizer()
        recognizer = module.IncrementalRecognizer(("TOKEN",))
        recognizer.feed("https://host/%54OKEN ")
        before = recognizer.stats.speculative_record_visits
        self.assertGreater(before, 0)
        recognizer.consume(recognizer.consumed)
        self.assertGreaterEqual(recognizer.stats.speculative_record_visits, before)
        recognizer.feed("https://host/%54OKEN ")
        self.assertGreater(recognizer.stats.speculative_record_visits, before)


class CommittedDecoderEmissionTests(_RecognizerCase):
    """Delayed decoder outputs cannot resurrect already committed coverage."""

    def test_delayed_feed_cannot_resurrect_committed_percent(self):
        module = self.recognizer()
        inc = module.IncrementalRecognizer(("%",))
        inc.feed("https://host/%")
        before = inc.stats
        inc.consume(14)
        self.assertEqual(before.chars, inc.stats.chars)
        self.assertEqual(before.secret_emissions, inc.stats.secret_emissions)
        inc.feed("zz")
        self.assertEqual((), inc.literal_secrets())
        self.assertEqual((), inc.decoded_secrets())
        self.assertEqual(16, inc.stats.chars)
        inc.finalize()
        self.assertEqual((), inc.decoded_secrets())
        self.assertEqual(16, inc.stats.chars)

    def test_finalization_cannot_resurrect_committed_percent(self):
        module = self.recognizer()
        for cut in (13, 14):
            inc = module.IncrementalRecognizer(("%",))
            inc.feed("https://host/%")
            inc.consume(cut)
            inc.finalize()
            expected = {(13, 14, 1)} if cut == 13 else set()
            self.assertEqual(
                expected, {(m.start, m.end, m.depth) for m in inc.decoded_secrets()}
            )
            self.assertEqual(14, inc.stats.chars)
            inc.consume(14)
            self.assertEqual((), inc.decoded_secrets())

    def test_delayed_atoms_preserve_straddling_secret_continuations(self):
        module = self.recognizer()
        for suffix in ("zz", "z"):
            inc = module.IncrementalRecognizer(("%", "%z"))
            inc.feed("https://host/%")
            inc.consume(14)
            before = inc.stats
            for character in suffix:
                inc.feed(character)
            inc.finalize()
            self.assertEqual(
                {(13, 15, 1, 1)},
                {
                    (m.start, m.end, m.depth, m.pattern_index)
                    for m in inc.decoded_secrets()
                },
            )
            self.assertEqual(14 + len(suffix), inc.stats.chars)
            self.assertGreaterEqual(
                inc.stats.speculative_record_visits, before.speculative_record_visits
            )
            inc.consume(15)
            self.assertEqual((), inc.decoded_secrets())

    def test_encoded_secret_crossing_commit_is_not_suppressed(self):
        module = self.recognizer()
        inc = module.IncrementalRecognizer(("%",))
        inc.feed("https://host/%")
        inc.consume(14)
        inc.feed("25")
        inc.finalize()
        self.assertEqual(
            {(13, 16, 1)}, {(m.start, m.end, m.depth) for m in inc.decoded_secrets()}
        )
        inc.consume(16)
        self.assertEqual((), inc.decoded_secrets())
        self.assertEqual(16, inc.stats.chars)


class PendingTransducerSecretTests(_RecognizerCase):
    """Delayed deeper prefixes must not destroy shallower decoded coverage."""

    def test_batch_pending_transducer_preserves_complete_raw_coverage(self):
        module = self.recognizer()
        for ending in ("", " "):
            result, matches = module.recognize_with_secrets(
                "%%25368ttps://host" + ending, ("%",)
            )
            self.assertIs(RecognitionKind.MATCH, result.kind)
            self.assertEqual(0, result.candidate.start)
            self.assertEqual(
                {(0, 1, 0), (1, 2, 0), (1, 4, 1)},
                {(m.start, m.end, m.depth) for m in matches},
            )

    def test_every_split_pending_transducer_preserves_decoded_coverage(self):
        module = self.recognizer()
        text = "%%25368ttps://host"
        for split in range(len(text) + 1):
            inc = module.IncrementalRecognizer(("%",))
            inc.feed(text[:split])
            inc.feed(text[split:])
            inc.finalize()
            self.assertEqual(0, inc.result(text).candidate.start)
            self.assertEqual(
                {(0, 1, 1), (1, 4, 1)},
                {(m.start, m.end, m.depth) for m in inc.decoded_secrets()},
            )
            self.assertEqual(
                {(0, 1, 0), (1, 2, 0)},
                {(m.start, m.end, m.depth) for m in inc.literal_secrets()},
            )

    def test_pending_speculation_state_and_work_are_bounded(self):
        module = self.recognizer()
        work = []
        for count in (100, 200):
            inc = module.IncrementalRecognizer(("%",))
            for character in "%25x %%25368x " * count:
                inc.feed(character)
                for observer in inc._pipeline._decoded:
                    self.assertLessEqual(sum(map(len, observer._speculative)), 27)
                inc.consume(inc.consumed)
            inc.finalize()
            self.assertEqual((), inc.decoded_secrets())
            stats = inc.stats
            self.assertLessEqual(stats.peak_pending_chars, 2)
            work.append(
                stats.layer_char_visits
                + stats.speculative_record_visits
                + stats.secret_ordering_work
                + stats.secret_record_transfers
            )
        self.assertLessEqual(work[1], 2 * work[0] + 64)


class CappedPrefixAndLongTokenTests(_RecognizerCase):
    """P2 corrections: exact capped prefixes and registration-bounded ordering."""

    def test_four_pass_scheme_prefixes_are_not_retained(self):
        module = self.recognizer()
        for text in (
            "%25252568",
            "%25252568t",
            "h%25252574",
            "%25252568%25252574",
            "ht%25252574",
            "%25252568%25252574%25252574%25252570",
        ):
            with self.subTest(text=text):
                self.assertIs(RecognitionKind.NO_MATCH, module.recognize(text).kind)
                for split in range(len(text) + 1):
                    inc = module.IncrementalRecognizer()
                    inc.feed(text[:split])
                    inc.feed(text[split:])
                    inc.finalize()
                    self.assertIs(RecognitionKind.NO_MATCH, inc.result(text).kind)

    def test_incomplete_four_pass_scheme_escapes_are_not_retained(self):
        module = self.recognizer()
        for text in (
            "%252525",
            "%2525256",
            "h%252525",
            "h%2525256",
            "https%2525256",
            "https%2525253A%2525253",
        ):
            with self.subTest(text=text):
                self.assertIs(RecognitionKind.NO_MATCH, module.recognize(text).kind)
                for split in range(len(text) + 1):
                    inc = module.IncrementalRecognizer()
                    inc.feed(text[:split])
                    inc.feed(text[split:])
                    self.assertIs(RecognitionKind.NO_MATCH, inc.result(text).kind)
                later = text + "!https"
                self.assertEqual(len(text) + 1, module.recognize(later).retain_from)

    def test_pending_four_pass_separator_escapes_retain_permitted_scheme(self):
        module = self.recognizer()
        for suffix in (
            "%252525",
            "%2525253",
            "%2525253A%252525",
            "%2525253A%2525252",
            "%2525253A%2525252F%2525252",
        ):
            text = "h%252574tps" + suffix
            with self.subTest(text=text):
                result = module.recognize(text, offset=11)
                self.assertIs(RecognitionKind.NEED_MORE, result.kind)
                self.assertEqual(11, result.retain_from)
                inc = module.IncrementalRecognizer()
                for character in text:
                    inc.feed(character)
                self.assertEqual(0, inc.result(text).retain_from)

    def test_later_valid_prefix_has_exact_raw_retention(self):
        module = self.recognizer()
        for invalid in ("%25252568", "h%25252574", "%25252568t"):
            text = invalid + "!https"
            result = module.recognize(text, offset=17)
            self.assertIs(RecognitionKind.NEED_MORE, result.kind)
            self.assertEqual(17 + len(invalid) + 1, result.retain_from)
            inc = module.IncrementalRecognizer()
            for character in text:
                inc.feed(character)
            self.assertEqual(len(invalid) + 1, inc.result(text).retain_from)

    def test_three_pass_scheme_with_four_pass_separator_is_retained(self):
        module = self.recognizer()
        scheme = "h%252574tps"
        separator = "%2525253A%2525252F%2525252F"
        for split in range(len(separator)):
            result = module.recognize(scheme + separator[:split])
            self.assertIs(RecognitionKind.NEED_MORE, result.kind)
            self.assertEqual(0, result.retain_from)
        self.assertIs(RecognitionKind.MATCH, module.recognize(scheme + separator).kind)

    def _long_token_work(self, *, encoded=False, overlap=False, consume=False):
        module = self.recognizer()
        totals = []
        batch_totals = [[], []]
        for size in (100, 200):
            secrets = ("a", "aa", "aaa") if overlap else ("a",)
            unit = "%61" if encoded else "a"
            prefix = "https://host/"
            text = prefix + unit * size
            expected = {
                (
                    len(prefix) + i * len(unit),
                    len(prefix) + (i + len(secret)) * len(unit),
                    j,
                )
                for j, secret in enumerate(secrets)
                for i in range(size - len(secret) + 1)
            }
            # Batch path includes finalization, with and without a terminator.
            for ending_index, ending in enumerate(("", " ")):
                _, matches, stats = module.recognize_with_secrets_and_stats(
                    text + ending, secrets
                )
                self.assertEqual(
                    expected, {(m.start, m.end, m.pattern_index) for m in matches}
                )
                self.assertLessEqual(
                    stats.secret_ordering_work, 20 * size * len(secrets)
                )
                batch_totals[ending_index].append(
                    stats.secret_ordering_work
                    + stats.secret_record_transfers
                    + stats.speculative_record_visits
                )

            inc = module.IncrementalRecognizer(secrets)
            for character in text:
                inc.feed(character)
                if consume:
                    # Each cut crosses unfinished longer patterns; retain only
                    # outcomes ending after the cut, without replaying state.
                    inc.consume(inc.consumed - 1)
            inc.finalize()
            matches = inc.decoded_secrets() if encoded else inc.literal_secrets()
            if consume:
                expected = {record for record in expected if record[1] > len(text) - 1}
            self.assertEqual(
                expected, {(m.start, m.end, m.pattern_index) for m in matches}
            )
            before = inc.stats
            inc.consume(inc.consumed)
            after = inc.stats
            self.assertGreaterEqual(
                after.secret_ordering_work, before.secret_ordering_work
            )
            self.assertEqual(
                before.secret_record_transfers, after.secret_record_transfers
            )
            totals.append(
                after.speculative_record_visits
                + after.secret_ordering_work
                + after.secret_record_transfers
                + after.candidate_retirement_visits
            )
        self.assertLessEqual(totals[1], 2 * totals[0] + 64)
        for work in batch_totals:
            self.assertLessEqual(work[1], 2 * work[0] + 64)

    def test_long_literal_token_finalization_is_linear(self):
        self._long_token_work()

    def test_long_literal_overlapping_token_finalization_is_linear(self):
        self._long_token_work(overlap=True)

    def test_long_encoded_token_finalization_is_linear(self):
        self._long_token_work(encoded=True)

    def test_long_literal_token_crossing_consumption_is_linear(self):
        self._long_token_work(overlap=True, consume=True)

    def test_long_encoded_overlapping_token_finalization_is_linear(self):
        self._long_token_work(encoded=True, overlap=True)

    def test_long_token_crossing_consumption_is_linear(self):
        self._long_token_work(encoded=True, overlap=True, consume=True)


class FinalizationWorkTests(_RecognizerCase):
    """Task 2.10 -- finalization orders only the new token's records.

    ``finalize_token`` used to heapify historical records, then sorted only
    each new batch. Neither gave long-token linearity. It now merges only
    registration-many ordered queue heads; the long-token tests above cover
    the formerly missing large-batch case.
    """

    _TOKEN = "https://host/%61"
    _ESCAPE_OFFSET = len("https://host/")

    def _expected_spans(self, reps: int):
        stride = len(self._TOKEN) + 1
        return [
            (
                index * stride + self._ESCAPE_OFFSET,
                index * stride + self._ESCAPE_OFFSET + len("%61"),
            )
            for index in range(reps)
        ]

    def _assert_coverage(self, reps: int, matches) -> None:
        self.assertEqual(
            self._expected_spans(reps), [(m.start, m.end) for m in matches]
        )
        self.assertEqual([1] * reps, [m.depth for m in matches])

    def test_batch_finalization_work_is_linear(self):
        module = self.recognizer()
        work = []
        for reps in (100, 200):
            with self.subTest(reps=reps):
                text = " ".join([self._TOKEN] * reps)
                result, matches, stats = module.recognize_with_secrets_and_stats(
                    text, ("a",)
                )
                self.assertIs(RecognitionKind.MATCH, result.kind)
                self._assert_coverage(reps, matches)
                work.append(stats.secret_record_transfers + stats.secret_ordering_work)
        self.assertGreater(work[0], 0)
        self.assertLessEqual(work[1], 2 * work[0] + 16)

    def test_incremental_finalization_work_is_linear(self):
        module = self.recognizer()
        work = []
        for reps in (100, 200):
            with self.subTest(reps=reps):
                text = " ".join([self._TOKEN] * reps)
                recognizer = module.IncrementalRecognizer(("a",))
                recognizer.feed(text)
                self._assert_coverage(reps, recognizer.decoded_secrets())
                work.append(
                    recognizer.stats.secret_record_transfers
                    + recognizer.stats.secret_ordering_work
                )
        self.assertGreater(work[0], 0)
        self.assertLessEqual(work[1], 2 * work[0] + 16)


class AdversarialCoverageWorkTests(_RecognizerCase):
    """Task 2.11 -- ordered boundary and coverage sweeps stay output-sensitive."""

    def test_many_embedded_candidates_share_token_boundary_work(self):
        module = self.recognizer()
        work = []
        for count in (10, 20):
            with self.subTest(count=count):
                text = "".join(
                    f"https://h{index:03d}/%54OKEN" for index in range(count)
                )
                result, matches, stats = module.recognize_with_secrets_and_stats(
                    text, ("TOKEN",)
                )
                self.assertIs(RecognitionKind.MATCH, result.kind)
                self.assertIsNone(result.candidate.end)
                self.assertEqual(count, len(matches))
                self.assertEqual(
                    [match.start for match in matches],
                    sorted(match.start for match in matches),
                )
                self.assertEqual([match.depth for match in matches], [1] * count)
                work.append(stats.boundary_chars + stats.coverage_checks)
        self.assertGreater(work[0], 0)
        self.assertLessEqual(work[1], 2 * work[0] + 8)

    def test_many_separate_urls_have_exact_spans_and_linear_work(self):
        module = self.recognizer()

        def run(count):
            text = " ".join(
                f"https://host{index:03d}/%54OKEN" for index in range(count)
            )
            result, matches, stats = module.recognize_with_secrets_and_stats(
                text, ("TOKEN",)
            )
            expected = []
            cursor = 0
            for token in text.split(" "):
                start = cursor + token.index("%54OKEN")
                expected.append((start, start + len("%54OKEN")))
                cursor += len(token) + 1
            return result, matches, stats, expected

        result, matches, large, expected = run(24)
        self.assertIs(RecognitionKind.MATCH, result.kind)
        self.assertEqual(expected, [(match.start, match.end) for match in matches])
        self.assertEqual(24, len({(match.start, match.end) for match in matches}))
        self.assertEqual([1] * 24, [match.depth for match in matches])
        _result, _matches, small, _expected = run(12)
        self.assertLessEqual(large.boundary_chars, 2 * small.boundary_chars + 16)
        self.assertLessEqual(large.coverage_checks, 2 * small.coverage_checks + 16)
        self.assertLessEqual(
            large.speculative_record_visits,
            2 * small.speculative_record_visits + 16,
        )

    def test_coverage_work_is_deterministic_when_input_doubles(self):
        module = self.recognizer()

        def run(count):
            text = " ".join(
                f"https://host{index:03d}/%54OKEN" for index in range(count)
            )
            return module.recognize_with_secrets_and_stats(text, ("TOKEN",))

        _small_result, small_matches, small = run(20)
        _large_result, large_matches, large = run(40)
        self.assertEqual(len(small_matches) * 2, len(large_matches))
        self.assertEqual(
            [(match.start, match.end, match.depth) for match in small_matches],
            sorted((match.start, match.end, match.depth) for match in small_matches),
        )
        self.assertEqual(
            [(match.start, match.end) for match in small_matches],
            [(match.start, match.end) for match in large_matches[: len(small_matches)]],
        )
        self.assertLessEqual(
            large.speculative_record_visits,
            2 * small.speculative_record_visits + 16,
        )
        self.assertLessEqual(large.boundary_chars, 2 * small.boundary_chars + 16)
        self.assertLessEqual(large.coverage_checks, 2 * small.coverage_checks + 16)


class SourceRangeProvenanceTests(_RecognizerCase):
    """Task 2.4 -- emissions carry inclusive raw start and exclusive raw end."""

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
        self.assertEqual([("A", 0, 3, True)], self._single_layer_emissions("%41"))

    def test_malformed_and_incomplete_escapes_preserve_literal_ranges(self):
        self.assertEqual(
            [("%", 0, 1, True), ("z", 1, 2, True), ("z", 2, 3, True)],
            self._single_layer_emissions("%zz"),
        )
        self.assertEqual(
            [("%", 0, 1, True), ("4", 1, 2, True), ("G", 2, 3, True)],
            self._single_layer_emissions("%4G"),
        )
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
        observer = pipeline._layer[0]
        self.assertEqual((0, 1), (observer.match_start, observer.match_start_end))
        self.assertEqual((5, 6), (observer.match_colon, observer.match_colon_end))

    def test_encoded_separator_range_covers_all_three_raw_characters(self):
        module = self.recognizer()
        pipeline = module._Pipeline(module._MutableStats())
        pipeline.feed("https%3A%2F%2Fhost")
        pipeline.finish()
        observer = pipeline._layer[0]
        self.assertEqual((0, 1), (observer.match_start, observer.match_start_end))
        self.assertEqual((5, 8), (observer.match_colon, observer.match_colon_end))

    def test_range_provenance_keeps_candidate_coordinates(self):
        cases = {
            "https://host tail": len("https://host"),
            "https%3A%2F%2Fhost tail": len("https%3A%2F%2Fhost"),
            "%68ttps://host tail": len("%68ttps://host"),
        }
        for text, end in cases.items():
            with self.subTest(text=text):
                result = self.recognize(text)
                self.assertIs(RecognitionKind.MATCH, result.kind)
                self.assertEqual(0, result.candidate.start)
                self.assertEqual(end, result.candidate.end)


class IncrementalContinuationTests(_RecognizerCase):
    """Task 2.8 -- continuation across feeds and covered secret ranges."""

    def test_partition_invariance_for_supported_urls(self):
        module = self.recognizer()
        cases = (
            "note https://host/path tail",
            "h%74tps%3A%2F%2Fhost/path tail",
            "TOKENhttps://host tail",
        )
        for text in cases:
            baseline = module.recognize(text)
            for split in range(len(text) + 1):
                with self.subTest(text=text, split=split):
                    recognizer = module.IncrementalRecognizer()
                    recognizer.feed(text[:split])
                    recognizer.feed(text[split:])
                    recognizer.finalize()
                    self.assertEqual(baseline, recognizer.result(text))

    def test_prefix_state_and_retain_from_track_supported_prefix(self):
        module = self.recognizer()
        recognizer = module.IncrementalRecognizer()
        recognizer.feed("alpha beta git+")
        self.assertEqual(module._PREFIX_SCHEME, recognizer.prefix_state())
        self.assertEqual("alpha beta git+".index("git"), recognizer.retain_from())

    def test_recognition_continues_after_a_commit(self):
        module = self.recognizer()
        recognizer = module.IncrementalRecognizer(("TOKEN",))
        recognizer.feed("https://first/TOKEN ")
        first = recognizer.earliest_match()
        self.assertIsNotNone(first)
        recognizer.consume(len("https://first/TOKEN "))
        self.assertIsNone(recognizer.earliest_match())
        recognizer.feed("https://second/x")
        second = recognizer.earliest_match()
        self.assertIsNotNone(second)
        self.assertEqual(
            "https://second/x".index("https") + len("https://first/TOKEN "),
            second.start,
        )

    def test_committed_secret_does_not_resurface(self):
        module = self.recognizer()
        recognizer = module.IncrementalRecognizer(("TOKEN",))
        recognizer.feed("pre TOKEN post")
        recognizer.consume(11)
        self.assertEqual((), recognizer.literal_secrets())
        recognizer.feed("TOKEN x")
        matches = recognizer.literal_secrets()
        self.assertEqual(1, len(matches))
        self.assertEqual(14, matches[0].start)

    def test_overlapping_secret_and_scheme_prefix_survive_a_commit(self):
        module = self.recognizer()
        recognizer = module.IncrementalRecognizer(("TOKENht",))
        recognizer.feed("TOKENht")
        self.assertEqual(1, len(recognizer.literal_secrets()))
        recognizer.consume(7)
        # The committed secret is retired, but the overlapping ``ht`` URL
        # prefix is not reset.
        self.assertEqual((), recognizer.literal_secrets())
        recognizer.feed("tps://host")
        match = recognizer.earliest_match()
        self.assertIsNotNone(match)
        self.assertEqual(5, match.start)
        # The earlier secret is not reported a second time.
        self.assertEqual((), recognizer.literal_secrets())

    def test_literal_secret_straddling_a_commit_is_reported_once(self):
        module = self.recognizer()
        recognizer = module.IncrementalRecognizer(("TOKEN",))
        recognizer.feed("pre TOK")
        recognizer.consume(7)
        recognizer.feed("EN post")
        matches = recognizer.literal_secrets()
        self.assertEqual(1, len(matches))
        self.assertEqual(4, matches[0].start)
        self.assertEqual(9, matches[0].end)

    def test_literal_secret_spanning_a_commit_and_raw_terminator(self):
        module = self.recognizer()
        recognizer = module.IncrementalRecognizer(("password done",))
        recognizer.feed("x password")
        recognizer.consume(10)
        recognizer.feed(" done y")
        matches = recognizer.literal_secrets()
        self.assertEqual(1, len(matches))
        text = "x password done y"
        self.assertEqual("password done", text[matches[0].start : matches[0].end])

    def test_consumption_does_not_replay_input_processing(self):
        module = self.recognizer()
        recognizer = module.IncrementalRecognizer(("TOKEN",))
        recognizer.feed("pre TOKENhttps://host tail")
        before = recognizer.stats
        recognizer.consume(20)
        after = recognizer.stats
        self.assertEqual(before.chars, after.chars)
        self.assertEqual(before.layer_char_visits, after.layer_char_visits)
        self.assertEqual(before.emissions, after.emissions)
        self.assertEqual(before.transitions, after.transitions)
        self.assertEqual(before.secret_emissions, after.secret_emissions)


class SuccessiveConsumptionTests(_RecognizerCase):
    """Task 2.8/2.10 -- commits retire recorded candidates incrementally.

    ``_LayerMatcher.retire`` used to rebuild the remaining deque on every
    commit, which is quadratic across many candidates.  Each layer's candidate
    queue is recorded in raw-start order, so commits now pop committed
    candidates from the left, retiring each record exactly once.
    """

    @staticmethod
    def _consume_tokens(recognizer, tokens):
        offset = 0
        earliest = []
        for token in tokens:
            offset += len(token)
            recognizer.consume(offset)
            offset += 1
            match = recognizer.earliest_match()
            earliest.append(None if match is None else match.start)
        return earliest

    def test_successive_consumption_retires_each_candidate_once(self):
        module = self.recognizer()

        def run(count):
            recognizer = module.IncrementalRecognizer(("TOKEN",))
            tokens = [f"https://h{index:03d}/x" for index in range(count)]
            recognizer.feed(" ".join(tokens))
            before = recognizer.retained_matches
            self.assertGreater(before, 0)
            earliest = self._consume_tokens(recognizer, tokens)
            self.assertEqual(0, recognizer.retained_matches)
            self.assertEqual(before, recognizer.stats.candidate_retirement_visits)
            retained = [start for start in earliest if start is not None]
            self.assertEqual(retained, sorted(retained))
            return before

        small = run(50)
        large = run(100)
        self.assertLessEqual(large, 2 * small + 16)

    def test_successive_consumption_of_encoded_candidates_is_linear(self):
        module = self.recognizer()

        def run(count):
            recognizer = module.IncrementalRecognizer(("TOKEN",))
            tokens = [
                f"{'h%74tps' if index % 2 else 'https'}://h{index:03d}/TOKEN"
                for index in range(count)
            ]
            recognizer.feed(" ".join(tokens))
            before = recognizer.retained_matches
            self.assertGreater(len(recognizer.literal_secrets()), 0)
            earliest = self._consume_tokens(recognizer, tokens)
            self.assertEqual(0, recognizer.retained_matches)
            self.assertEqual((), recognizer.literal_secrets())
            self.assertEqual(before, recognizer.stats.candidate_retirement_visits)
            retained = [start for start in earliest if start is not None]
            self.assertEqual(retained, sorted(retained))
            return before

        small = run(50)
        large = run(100)
        self.assertLessEqual(large, 2 * small + 16)

    def test_successive_consumption_with_overlapping_secrets_is_linear(self):
        module = self.recognizer()

        def run(count):
            recognizer = module.IncrementalRecognizer(("TOKENht",))
            tokens = [
                f"{'TOKENh%74tps' if index % 2 else 'TOKENhttps'}://h{index:03d}/x"
                for index in range(count)
            ]
            recognizer.feed(" ".join(tokens))
            before = recognizer.retained_matches
            self.assertGreater(len(recognizer.literal_secrets()), 0)
            earliest = self._consume_tokens(recognizer, tokens)
            self.assertEqual(0, recognizer.retained_matches)
            self.assertEqual(before, recognizer.stats.candidate_retirement_visits)
            retained = [start for start in earliest if start is not None]
            self.assertEqual(retained, sorted(retained))
            return before

        small = run(50)
        large = run(100)
        self.assertLessEqual(large, 2 * small + 16)


class ContractIntegrationTests(_RecognizerCase):
    """The recognizer emits the P1 contract types with raw coordinates."""

    def test_match_emits_a_p1_recognition_result(self):
        result = self.recognize("https://host")
        self.assertIsInstance(result, RecognitionResult)
        self.assertIs(RecognitionKind.MATCH, result.kind)

    def test_recognizer_exposes_callable_batch_entry_points(self):
        module = self.recognizer()
        for name in (
            "recognize",
            "recognize_with_stats",
            "recognize_with_secrets",
            "IncrementalRecognizer",
        ):
            self.assertTrue(hasattr(module, name), name)

    def test_incremental_recognizer_owns_no_source_payload(self):
        module = self.recognizer()
        recognizer = module.IncrementalRecognizer(("TOKEN",))
        self.assertFalse(hasattr(recognizer, "_tail"))
        self.assertFalse(hasattr(recognizer, "_rebuild"))
        recognizer.feed("https://host/%54OKEN/path")
        self.assertFalse(hasattr(recognizer, "_tail"))

    def test_recognizer_uses_the_p1a_core_and_no_legacy_fallback(self):
        module = self.recognizer()
        self.assertFalse(hasattr(module, "re"))
        self.assertNotIn("_URL_START_RE", vars(module))
        self.assertNotIn("_find_url_start", vars(module))
        self.assertNotIn("_decode_percent_layer", vars(module))
        for name in vars(module):
            self.assertNotIn("VARIANT", name.upper())
        self.assertTrue(hasattr(module, "_SCHEME_AUTOMATON"))


if __name__ == "__main__":
    unittest.main()
