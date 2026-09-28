"""Phase 4 tests — conservative npm successful-fetch parsing and identity.

These bind the deliverables of tasks 4.1-4.7 for
``add-configurable-network-url-display``:

* recognition of the complete npm 11.16.0 successful-fetch source-line grammar
  ``npm http fetch <METHOD> <STATUS> <URL> <ASCII digits>ms [attempt #<ASCII
  digits>] [(cache <OUTCOME>)]`` including the real cache-miss fixture from
  ``docs/research/npm-http-logging.md``;
* conservative rejection of unrelated, malformed, non-2xx, retry/failure,
  unknown-unit, and unknown-variant lines, which remain ordinary diagnostics;
* policy-specific ``redacted`` and ``host-path`` fetch group identities and
  canonical text that omit URL/latency respectively while preserving method,
  exact status, attempt presence/value, and cache outcome.

Nothing here renders, schedules a timer, reads a mailbox, or mutates
presentation state.
"""
from __future__ import annotations

import random
import unittest

from docker.versioning.diagnostic_projection import (
    REDACTED_PATH_MARKER,
    SafeHostPath,
    normalized_url_host,
    sanitize_host_paths,
)
from docker.versioning.model import NetworkUrlDisplay
from docker.versioning.npm_fetch import (
    NpmFetchRecord,
    NpmFetchRecognizer,
    SafeFetchRecord,
    canonical_fetch_text,
    fetch_group_identity,
    fetch_source_fields_text,
    parse_npm_fetch_line,
)

#: The real successful cache-miss fixture captured in the research report.
CACHE_MISS = (
    "npm http fetch GET 200 "
    "https://registry.npmjs.org/npm-http-research-fixture/-/"
    "npm-http-research-fixture-1.0.0.tgz 15ms (cache miss)"
)
#: The distinct `npm http cache` fixture, which is out of this parser's scope.
CACHE_HIT = (
    "npm http cache npm-http-research-fixture@"
    "https://registry.npmjs.org/npm-http-research-fixture/-/"
    "npm-http-research-fixture-1.0.0.tgz 0ms (cache hit)"
)
#: The research retry/failure form, which is not a successful fetch.
RETRY = (
    "npm http fetch GET "
    "https://registry.npmjs.org/npm-http-research-fixture/-/"
    "npm-http-research-fixture-1.0.0.tgz attempt 1 failed with 503"
)
RESOURCE_URL = (
    "https://registry.npmjs.org/npm-http-research-fixture/-/"
    "npm-http-research-fixture-1.0.0.tgz"
)


def _line(
    *,
    method: str = "GET",
    status: str = "200",
    url: str = RESOURCE_URL,
    latency: str = "15ms",
    suffix: str = "",
) -> str:
    return f"npm http fetch {method} {status} {url} {latency}{suffix}"


def _state_text(value) -> str:
    """Concatenate every string reachable from a recognizer attribute.

    Container items are concatenated *without* a separator so that a raw
    buffer stored as a ``list[str]`` of characters is reconstructed: joining
    with a separator would hide exactly the fragmented credential buffer this
    test exists to detect.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple, set, frozenset)):
        return "".join(_state_text(item) for item in value)
    if isinstance(value, dict):
        return "".join(
            _state_text(key) + _state_text(item)
            for key, item in value.items()
        )
    if hasattr(value, "__dict__"):
        return "".join(_state_text(item) for item in vars(value).values())
    return ""


def _retained_state_strings(recognizer) -> str:
    # One concatenated string per top-level attribute so unrelated attributes
    # are not spliced into a false positive.
    return "\x00".join(
        _state_text(value) for value in vars(recognizer).values()
    )


class TestFetchGrammarRecognition(unittest.TestCase):
    """4.1 — the complete successful-fetch grammar is recognized."""

    def test_research_cache_miss_fixture_is_recognized(self):
        record = parse_npm_fetch_line(CACHE_MISS)
        self.assertIsInstance(record, NpmFetchRecord)
        assert record is not None
        self.assertEqual("GET", record.method)
        self.assertEqual(200, record.status)
        self.assertEqual(RESOURCE_URL, record.url)
        self.assertEqual(15, record.latency_ms)
        self.assertIsNone(record.attempt)
        self.assertEqual("miss", record.cache_outcome)

    def test_zero_latency_is_recognized(self):
        record = parse_npm_fetch_line(_line(latency="0ms"))
        assert record is not None
        self.assertEqual(0, record.latency_ms)

    def test_ordinary_latency_value_is_recognized(self):
        record = parse_npm_fetch_line(_line(latency="250ms"))
        assert record is not None
        self.assertEqual(250, record.latency_ms)

    def test_leading_zero_latency_is_recognized_as_its_value(self):
        record = parse_npm_fetch_line(_line(latency="007ms"))
        assert record is not None
        self.assertEqual(7, record.latency_ms)

    def test_large_integer_values_are_recognized(self):
        record = parse_npm_fetch_line(
            _line(
                status="299",
                latency="999999999999ms",
                suffix=" attempt #123456789012 (cache stale)",
            )
        )
        assert record is not None
        self.assertEqual(299, record.status)
        self.assertEqual(999999999999, record.latency_ms)
        self.assertEqual(123456789012, record.attempt)
        self.assertEqual("stale", record.cache_outcome)

    def test_optional_attempt_is_recognized(self):
        record = parse_npm_fetch_line(_line(suffix=" attempt #2"))
        assert record is not None
        self.assertEqual(2, record.attempt)
        self.assertIsNone(record.cache_outcome)

    def test_optional_cache_outcome_is_recognized(self):
        record = parse_npm_fetch_line(_line(suffix=" (cache miss)"))
        assert record is not None
        self.assertIsNone(record.attempt)
        self.assertEqual("miss", record.cache_outcome)

    def test_optional_attempt_and_cache_outcome_together(self):
        record = parse_npm_fetch_line(_line(suffix=" attempt #3 (cache hit)"))
        assert record is not None
        self.assertEqual(3, record.attempt)
        self.assertEqual("hit", record.cache_outcome)

    def test_every_method_token_is_accepted(self):
        for method in ("GET", "HEAD", "POST", "PUT", "DELETE", "PATCH"):
            with self.subTest(method=method):
                self.assertIsNotNone(parse_npm_fetch_line(_line(method=method)))

    def test_every_2xx_status_is_accepted(self):
        accepted = {str(value) for value in range(200, 300)}
        recognized = set()
        for value in range(100, 600):
            if parse_npm_fetch_line(_line(status=str(value))) is not None:
                recognized.add(str(value))
        self.assertEqual(accepted, recognized)

    def test_one_trailing_newline_is_tolerated(self):
        self.assertIsNotNone(parse_npm_fetch_line(CACHE_MISS + "\n"))

    def test_non_string_input_is_rejected_by_type(self):
        with self.assertRaises(TypeError):
            parse_npm_fetch_line(b"npm http fetch GET 200 https://x 15ms")  # type: ignore[arg-type]


class TestFetchGrammarRejection(unittest.TestCase):
    """4.2 — every near-match remains an ordinary diagnostic."""

    def _reject(self, *lines: str) -> None:
        for line in lines:
            with self.subTest(line=line):
                self.assertIsNone(parse_npm_fetch_line(line))

    def test_research_cache_hit_form_is_rejected(self):
        self._reject(CACHE_HIT)

    def test_research_retry_failure_form_is_rejected(self):
        self._reject(RETRY)

    def test_unrelated_prefixes_are_rejected(self):
        self._reject(
            "some other line GET 200 https://registry.npmjs.org/a 15ms",
            "http fetch GET 200 https://registry.npmjs.org/a 15ms",
            "npm http fetch GET 200 https://registry.npmjs.org/a 15ms (cache)",
            "npm warn deprecated npm-http-research-fixture@1.0.0",
            "npm error network timeout at: https://registry.npmjs.org/a",
        )

    def test_malformed_prefixes_are_rejected(self):
        self._reject(
            "npm http fetxh GET 200 https://registry.npmjs.org/a 15ms",
            "npm http fetchGET 200 https://registry.npmjs.org/a 15ms",
            "npm  http fetch GET 200 https://registry.npmjs.org/a 15ms",
            "npm http fetch  GET 200 https://registry.npmjs.org/a 15ms",
            "NPM HTTP FETCH GET 200 https://registry.npmjs.org/a 15ms",
        )

    def test_bare_method_without_prefix_is_rejected(self):
        self._reject(
            "GET 200 https://registry.npmjs.org/a 15ms",
            "GET 200 https://registry.npmjs.org/a 15ms (cache miss)",
        )

    def test_non_success_statuses_are_rejected(self):
        self._reject(
            _line(status="199"),
            _line(status="300"),
            _line(status="301"),
            _line(status="404"),
            _line(status="500"),
            _line(status="503"),
            _line(status="099"),
            _line(status="1000"),
        )

    def test_missing_url_is_rejected(self):
        self._reject(
            "npm http fetch GET 200 15ms",
            "npm http fetch GET 200 (cache miss)",
            "npm http fetch GET 200 attempt #2",
        )

    def test_non_url_shaped_url_field_is_rejected(self):
        self._reject(
            _line(url="not-a-url"),
            _line(url="registry.npmjs.org/a"),
            _line(url="ftp:/registry.npmjs.org/a"),
            _line(url="https://"),
        )

    def test_malformed_attempt_is_rejected(self):
        self._reject(
            _line(suffix=" attempt 2"),
            _line(suffix=" attempt #"),
            _line(suffix=" attempt #x"),
            _line(suffix=" attempt #2x"),
            _line(suffix=" attempt -2"),
            _line(suffix=" #2"),
            _line(suffix=" attempt #2 #3"),
            _line(suffix=" attempts #2"),
            _line(suffix=" attempt#2"),
        )

    def test_malformed_latency_is_rejected(self):
        self._reject(
            _line(latency="15mks"),
            _line(latency="15s"),
            _line(latency="15m"),
            _line(latency="15h"),
            _line(latency="15"),
            _line(latency="ms"),
            _line(latency="-15ms"),
            _line(latency="+15ms"),
            _line(latency="1.5ms"),
            _line(latency="MS"),
        )

    def test_malformed_latency_units_are_rejected_with_cache_clause(self):
        self._reject(
            _line(latency="15mks", suffix=" (cache miss)"),
            _line(latency="15s", suffix=" (cache hit)"),
        )

    def test_unknown_variants_are_rejected(self):
        self._reject(
            _line(suffix=" (cache)"),
            _line(suffix=" (cache miss extra)"),
            _line(suffix=" (cache miss) extra"),
            _line(suffix=" extra"),
            _line(suffix=" attempt #2 (cache miss) extra"),
            _line(suffix=" (cache MISS)"),
            _line(suffix=" (cache miss))"),
            _line(suffix="  (cache miss)"),
        )

    def test_reversed_optional_clauses_are_rejected(self):
        self._reject(_line(suffix=" (cache miss) attempt #2"))

    def test_lowercase_method_is_rejected(self):
        self._reject(_line(method="get"), _line(method="Get"))

    def test_trailing_whitespace_and_carriage_return_are_rejected(self):
        self._reject(CACHE_MISS + " ", " " + CACHE_MISS, CACHE_MISS + "\r", CACHE_MISS + "\n\n")

    def test_embedded_newline_is_rejected(self):
        self._reject(_line() + "\nnpm warn extra")


class TestFetchGroupIdentity(unittest.TestCase):
    """4.3 — policy-specific fetch group identities."""

    def _recognized(self, line: str = CACHE_MISS) -> NpmFetchRecord:
        record = parse_npm_fetch_line(line)
        assert record is not None
        return record

    def test_redacted_identity_excludes_url_and_latency(self):
        left = self._recognized()
        right = self._recognized(
            _line(
                url="https://registry.npmjs.org/other-package/-/other-package-9.9.9.tgz",
                latency="9999ms",
                suffix=" (cache miss)",
            )
        )
        self.assertEqual(
            fetch_group_identity(left, NetworkUrlDisplay.REDACTED),
            fetch_group_identity(right, NetworkUrlDisplay.REDACTED),
        )

    def test_redacted_identity_includes_method(self):
        left = fetch_group_identity(self._recognized(), NetworkUrlDisplay.REDACTED)
        right = fetch_group_identity(
            self._recognized(_line(method="HEAD")), NetworkUrlDisplay.REDACTED
        )
        self.assertNotEqual(left, right)

    def test_redacted_identity_includes_exact_status(self):
        left = fetch_group_identity(self._recognized(), NetworkUrlDisplay.REDACTED)
        right = fetch_group_identity(
            self._recognized(_line(status="204")), NetworkUrlDisplay.REDACTED
        )
        self.assertNotEqual(left, right)

    def test_redacted_identity_includes_cache_outcome(self):
        left = fetch_group_identity(self._recognized(), NetworkUrlDisplay.REDACTED)
        right = fetch_group_identity(
            self._recognized(_line(suffix=" (cache hit)")), NetworkUrlDisplay.REDACTED
        )
        self.assertNotEqual(left, right)
        absent = fetch_group_identity(
            self._recognized(_line()), NetworkUrlDisplay.REDACTED
        )
        self.assertNotEqual(left, absent)

    def test_redacted_identity_includes_attempt_presence_and_value(self):
        attempt_two = fetch_group_identity(
            self._recognized(_line(suffix=" attempt #2")), NetworkUrlDisplay.REDACTED
        )
        attempt_three = fetch_group_identity(
            self._recognized(_line(suffix=" attempt #3")), NetworkUrlDisplay.REDACTED
        )
        absent = fetch_group_identity(
            self._recognized(_line()), NetworkUrlDisplay.REDACTED
        )
        self.assertNotEqual(attempt_two, attempt_three)
        self.assertNotEqual(attempt_two, absent)

    def test_host_path_identity_includes_normalized_hostname_and_path(self):
        same = fetch_group_identity(
            self._recognized(_line(latency="999ms", suffix=" (cache miss)")),
            NetworkUrlDisplay.HOST_PATH,
        )
        base = fetch_group_identity(
            self._recognized(), NetworkUrlDisplay.HOST_PATH
        )
        self.assertEqual(base, same)

        other_path = fetch_group_identity(
            self._recognized(
                _line(
                    url="https://registry.npmjs.org/npm-http-research-fixture/-/"
                    "npm-http-research-fixture-2.0.0.tgz"
                )
            ),
            NetworkUrlDisplay.HOST_PATH,
        )
        other_host = fetch_group_identity(
            self._recognized(_line(url="https://example.org/a/b")),
            NetworkUrlDisplay.HOST_PATH,
        )
        self.assertNotEqual(base, other_path)
        self.assertNotEqual(base, other_host)

    def test_host_path_identity_excludes_latency(self):
        left = fetch_group_identity(self._recognized(), NetworkUrlDisplay.HOST_PATH)
        right = fetch_group_identity(
            self._recognized(_line(latency="12345ms", suffix=" (cache miss)")),
            NetworkUrlDisplay.HOST_PATH,
        )
        self.assertEqual(left, right)

    def test_host_path_identity_excludes_credentials_port_query_and_fragment(self):
        base = fetch_group_identity(
            self._recognized(_line(url="https://registry.npmjs.org/a/b")),
            NetworkUrlDisplay.HOST_PATH,
        )
        noisy = fetch_group_identity(
            self._recognized(
                _line(url="https://user:pass@registry.npmjs.org:443/a/b?x=1#frag")
            ),
            NetworkUrlDisplay.HOST_PATH,
        )
        self.assertEqual(base, noisy)

    def test_host_path_identity_includes_method_status_attempt_and_cache(self):
        base = fetch_group_identity(
            self._recognized(_line(suffix=" attempt #2 (cache miss)")),
            NetworkUrlDisplay.HOST_PATH,
        )
        for override in (
            _line(method="HEAD", suffix=" attempt #2 (cache miss)"),
            _line(status="206", suffix=" attempt #2 (cache miss)"),
            _line(suffix=" attempt #3 (cache miss)"),
            _line(suffix=" attempt #2 (cache hit)"),
            _line(suffix=" attempt #2"),
        ):
            with self.subTest(line=override):
                self.assertNotEqual(
                    base,
                    fetch_group_identity(
                        self._recognized(override), NetworkUrlDisplay.HOST_PATH
                    ),
                )

    def test_host_path_identity_falls_back_for_unsafe_path(self):
        record = self._recognized(
            _line(url="https://registry.npmjs.org/a/secretpath/b")
        )
        key = fetch_group_identity(
            record, NetworkUrlDisplay.HOST_PATH, secrets=("secretpath",)
        )
        assert key is not None
        self.assertEqual("registry.npmjs.org/<redacted-path>", key.host_path)

    def test_host_path_identity_fails_closed_for_secret_host(self):
        record = self._recognized(_line(url="https://secret.example.org/a/b"))
        key = fetch_group_identity(
            record, NetworkUrlDisplay.HOST_PATH, secrets=("secret",)
        )
        assert key is not None
        self.assertIn(key.host_path, (None, REDACTED_PATH_MARKER))

    def test_exact_mode_has_no_fetch_group_identity(self):
        self.assertIsNone(
            fetch_group_identity(self._recognized(), NetworkUrlDisplay.EXACT)
        )

    def test_redacted_and_host_path_identities_are_distinct_types_of_key(self):
        redacted = fetch_group_identity(self._recognized(), NetworkUrlDisplay.REDACTED)
        host_path = fetch_group_identity(
            self._recognized(), NetworkUrlDisplay.HOST_PATH
        )
        assert redacted is not None and host_path is not None
        self.assertNotEqual(redacted, host_path)

    def test_identity_rejects_a_non_member_display_value(self):
        with self.assertRaises((TypeError, ValueError)):
            fetch_group_identity(
                self._recognized(), "redacted"  # type: ignore[arg-type]
            )


class TestCanonicalFetchText(unittest.TestCase):
    """4.5 — canonical redacted and host-path rendering omits latency."""

    def _recognized(self, line: str = CACHE_MISS) -> NpmFetchRecord:
        record = parse_npm_fetch_line(line)
        assert record is not None
        return record

    def test_redacted_canonical_text_omits_url_and_latency(self):
        self.assertEqual(
            "npm http fetch GET 200 <redacted> (cache miss)",
            canonical_fetch_text(self._recognized(), NetworkUrlDisplay.REDACTED),
        )

    def test_redacted_canonical_text_preserves_attempt(self):
        self.assertEqual(
            "npm http fetch GET 200 <redacted> attempt #2 (cache miss)",
            canonical_fetch_text(
                self._recognized(_line(suffix=" attempt #2 (cache miss)")),
                NetworkUrlDisplay.REDACTED,
            ),
        )

    def test_redacted_canonical_text_without_optional_clauses(self):
        self.assertEqual(
            "npm http fetch GET 200 <redacted>",
            canonical_fetch_text(self._recognized(_line()), NetworkUrlDisplay.REDACTED),
        )

    def test_host_path_canonical_text_shows_normalized_host_and_path(self):
        self.assertEqual(
            "npm http fetch GET 200 registry.npmjs.org/"
            "npm-http-research-fixture/-/npm-http-research-fixture-1.0.0.tgz"
            " (cache miss)",
            canonical_fetch_text(self._recognized(), NetworkUrlDisplay.HOST_PATH),
        )

    def test_host_path_canonical_text_uses_redacted_path_marker(self):
        text = canonical_fetch_text(
            self._recognized(_line(url="https://registry.npmjs.org/a/secretpath/b")),
            NetworkUrlDisplay.HOST_PATH,
            secrets=("secretpath",),
        )
        self.assertEqual(
            "npm http fetch GET 200 registry.npmjs.org/<redacted-path>", text
        )

    def test_host_path_canonical_text_fails_closed_for_secret_host(self):
        text = canonical_fetch_text(
            self._recognized(_line(url="https://secret.example.org/a/b")),
            NetworkUrlDisplay.HOST_PATH,
            secrets=("secret",),
        )
        self.assertNotIn("secret.example.org", text)
        self.assertIn("<redacted>", text)

    def test_host_path_canonical_text_preserves_attempt_and_cache(self):
        self.assertEqual(
            "npm http fetch GET 200 registry.npmjs.org/a/b attempt #4 (cache hit)",
            canonical_fetch_text(
                self._recognized(
                    _line(url="https://registry.npmjs.org/a/b",
                          suffix=" attempt #4 (cache hit)")
                ),
                NetworkUrlDisplay.HOST_PATH,
            ),
        )

    def test_canonical_text_never_contains_latency(self):
        for display in (NetworkUrlDisplay.REDACTED, NetworkUrlDisplay.HOST_PATH):
            with self.subTest(display=display):
                text = canonical_fetch_text(
                    self._recognized(_line(latency="987654ms")), display
                )
                assert text is not None
                self.assertNotIn("987654", text)
                self.assertNotIn("ms", text.replace("npm ", ""))

    def test_exact_mode_has_no_canonical_fetch_text(self):
        self.assertIsNone(
            canonical_fetch_text(self._recognized(), NetworkUrlDisplay.EXACT)
        )


class TestDelimiterCompleteHostPathProjection(unittest.TestCase):
    """4.5 — the URL field is projected as a delimiter-complete field.

    The parser knows the URL ends before the latency token, so host/path
    projection must not apply the streaming projector's end-of-input handling
    for a potentially truncated token.  Projecting the bare URL drops
    single-label hosts and loses the hostname when the path is unsafe.
    """

    def _recognized(self, url: str) -> NpmFetchRecord:
        record = parse_npm_fetch_line(_line(url=url))
        assert record is not None
        return record

    def test_single_label_host_paths_are_distinct(self):
        first = fetch_group_identity(
            self._recognized("http://localhost/a"),
            NetworkUrlDisplay.HOST_PATH,
        )
        second = fetch_group_identity(
            self._recognized("http://localhost/b"),
            NetworkUrlDisplay.HOST_PATH,
        )
        assert first is not None and second is not None
        self.assertEqual("localhost/a", first.host_path)
        self.assertEqual("localhost/b", second.host_path)
        self.assertNotEqual(first, second)

    def test_single_label_host_path_canonical_text(self):
        self.assertEqual(
            "npm http fetch GET 200 localhost/a",
            canonical_fetch_text(
                self._recognized("http://localhost/a"),
                NetworkUrlDisplay.HOST_PATH,
            ),
        )

    def test_single_label_hosts_share_the_redacted_identity(self):
        first = fetch_group_identity(
            self._recognized("http://localhost/a"),
            NetworkUrlDisplay.REDACTED,
        )
        second = fetch_group_identity(
            self._recognized("http://localhost/b"),
            NetworkUrlDisplay.REDACTED,
        )
        assert first is not None
        self.assertEqual(first, second)
        self.assertIsNone(first.host_path)

    def test_malformed_path_falls_back_but_keeps_the_hostname(self):
        record = self._recognized("https://example.com/%")
        key = fetch_group_identity(record, NetworkUrlDisplay.HOST_PATH)
        assert key is not None
        self.assertEqual(
            "example.com" + REDACTED_PATH_MARKER, key.host_path
        )
        text = canonical_fetch_text(record, NetworkUrlDisplay.HOST_PATH)
        self.assertEqual(
            "npm http fetch GET 200 example.com" + REDACTED_PATH_MARKER, text
        )
        assert text is not None
        self.assertNotIn("<redacted>", text)


class TestFetchParserBoundaries(unittest.TestCase):
    """4.6 — table-driven boundary, large-integer, and near-match coverage."""

    def test_whitespace_between_tokens_must_be_exactly_one_space(self):
        for line in (
            "npm http fetch  GET 200 https://registry.npmjs.org/a 15ms",
            "npm http fetch GET  200 https://registry.npmjs.org/a 15ms",
            "npm http fetch GET 200  https://registry.npmjs.org/a 15ms",
            "npm http fetch GET 200 https://registry.npmjs.org/a  15ms",
            "npm http fetch\tGET 200 https://registry.npmjs.org/a 15ms",
        ):
            with self.subTest(line=line):
                self.assertIsNone(parse_npm_fetch_line(line))

    def test_url_must_not_be_empty_or_truncated(self):
        for line in (
            "npm http fetch GET 200 https:// 15ms",
            "npm http fetch GET 200 15ms (cache miss)",
            "npm http fetch GET 200 https://registry.npmjs.org 15ms extra",
        ):
            with self.subTest(line=line):
                self.assertIsNone(parse_npm_fetch_line(line))

    def test_oversized_latency_and_attempt_are_rejected_without_overflow(self):
        # Twenty-four- and twenty-digit fields exceed the deliberate bound and
        # must be rejected conservatively instead of being converted, so that
        # Python's decimal conversion limit can never raise out of the parser.
        record = parse_npm_fetch_line(
            _line(latency="9" * 24 + "ms", suffix=" attempt #" + "8" * 20)
        )
        self.assertIsNone(record)

    def test_optional_clause_order_is_fixed(self):
        self.assertIsNotNone(
            parse_npm_fetch_line(_line(suffix=" attempt #1 (cache miss)"))
        )
        self.assertIsNone(
            parse_npm_fetch_line(_line(suffix=" (cache miss) attempt #1"))
        )

    def test_partial_parse_never_returns_a_record(self):
        near_matches = (
            "npm http fetch GET 200 https://registry.npmjs.org/a 15ms attempt",
            "npm http fetch GET 200 https://registry.npmjs.org/a 15ms attempt #",
            "npm http fetch GET 200 https://registry.npmjs.org/a 15ms (cache",
            "npm http fetch GET 200 https://registry.npmjs.org/a 15ms (cache ",
            "npm http fetch GET 200 https://registry.npmjs.org/a 15mss",
            "npm http fetch GET 200 https://registry.npmjs.org/a 15ms (cache miss",
        )
        for line in near_matches:
            with self.subTest(line=line):
                self.assertIsNone(parse_npm_fetch_line(line))

    def test_encoded_and_dot_segment_paths_use_the_projector_result(self):
        encoded = parse_npm_fetch_line(
            _line(url="https://registry.npmjs.org/a/b/%2e%2e/c")
        )
        plain = parse_npm_fetch_line(_line(url="https://registry.npmjs.org/a/c"))
        assert encoded is not None and plain is not None
        self.assertEqual(
            fetch_group_identity(encoded, NetworkUrlDisplay.HOST_PATH),
            fetch_group_identity(plain, NetworkUrlDisplay.HOST_PATH),
        )

    def test_percent_encoded_host_path_is_terminal_safe(self):
        record = parse_npm_fetch_line(_line(url="https://registry.npmjs.org/caf\u00e9"))
        assert record is not None
        text = canonical_fetch_text(record, NetworkUrlDisplay.HOST_PATH)
        assert text is not None
        self.assertIn("/caf%C3%A9", text)

    def test_url_query_and_fragment_do_not_reach_canonical_text(self):
        record = parse_npm_fetch_line(
            _line(url="https://registry.npmjs.org/a/b?token=supersecret#frag")
        )
        assert record is not None
        text = canonical_fetch_text(record, NetworkUrlDisplay.HOST_PATH)
        assert text is not None
        self.assertNotIn("supersecret", text)
        self.assertNotIn("token", text)
        self.assertNotIn("frag", text)

    def test_identity_keys_are_hashable_for_grouping(self):
        left = fetch_group_identity(
            parse_npm_fetch_line(_line()), NetworkUrlDisplay.REDACTED
        )
        right = fetch_group_identity(
            parse_npm_fetch_line(_line(latency="1ms")), NetworkUrlDisplay.REDACTED
        )
        self.assertEqual(len({left, right}), 1)

    def test_identity_does_not_expose_the_source_url_or_secrets(self):
        for display in (NetworkUrlDisplay.REDACTED, NetworkUrlDisplay.HOST_PATH):
            key = fetch_group_identity(
                parse_npm_fetch_line(
                    _line(url="https://registry.npmjs.org/a/b?token=supersecret#f")
                ),
                display,
            )
            with self.subTest(display=display):
                rendered = repr(key)
                self.assertNotIn("supersecret", rendered)
                self.assertNotIn("token=", rendered)


class TestFetchUrlValidation(unittest.TestCase):
    """A malformed URL field must stay an ordinary diagnostic."""

    MALFORMED = (
        "https://%",
        "https://%2e",
        "https://[",
        "https://]",
        "https://?x",
        "https:///x",
        "https://",
        "http://:80/x",
        "https://:8080/x",
        "https://user@/x",
        "http://127.0.0.1:70000/x",
        "http://127.0.0.1:0x50/x",
        "https://exa%20mple.org/x",
        "not-a-url",
        "ftp:/registry.npmjs.org/a",
    )

    VALID = (
        "https://registry.npmjs.org/a/b",
        "http://localhost:4873/a",
        "https://user:pw@registry.npmjs.org/a/b",
        "https://registry.npmjs.org:443/a/b",
        "https://registry.npmjs.org./a/b",
        "http://127.0.0.1:8080/a",
        "http://[::1]:8080/a",
        "https://xn--caf-dma.org/a",
    )

    def test_malformed_url_fields_are_rejected(self):
        for url in self.MALFORMED:
            with self.subTest(url=url):
                self.assertIsNone(parse_npm_fetch_line(_line(url=url)))

    def test_complete_url_fields_are_recognized(self):
        for url in self.VALID:
            with self.subTest(url=url):
                record = parse_npm_fetch_line(_line(url=url))
                self.assertIsNotNone(record)
                assert record is not None
                self.assertEqual(url, record.url)

    def test_malformed_lines_cannot_receive_a_fetch_group_identity(self):
        for url in self.MALFORMED:
            with self.subTest(url=url):
                record = parse_npm_fetch_line(_line(url=url))
                self.assertIsNone(record)
                # No record means no key and no canonical rendering: the
                # malformed line remains an ordinary diagnostic downstream.
                with self.assertRaises(TypeError):
                    fetch_group_identity(record, NetworkUrlDisplay.REDACTED)
                with self.assertRaises(TypeError):
                    canonical_fetch_text(record, NetworkUrlDisplay.HOST_PATH)


class TestFetchUrlControlRejection(unittest.TestCase):
    """Literal C0 controls and DEL in the URL field are ordinary diagnostics.

    Projection would otherwise discard the control-bearing suffix and group the
    malformed line with a shorter path, so the parser rejects the line outright
    rather than stripping or neutralizing the control.
    """

    EXAMPLES = (
        "https://example.com/a\x00junk",
        "https://example.com/a\x1b[31mjunk",
        "https://example.com/a\x7fjunk",
    )

    def test_every_c0_control_and_del_is_rejected(self):
        for code in (*range(0x20), 0x7F):
            with self.subTest(code=code):
                url = f"https://example.com/a{chr(code)}junk"
                self.assertIsNone(parse_npm_fetch_line(_line(url=url)))

    def test_named_control_examples_are_rejected(self):
        for url in self.EXAMPLES:
            with self.subTest(url=url):
                self.assertIsNone(parse_npm_fetch_line(_line(url=url)))

    def test_rejected_control_lines_cannot_receive_an_identity(self):
        for url in self.EXAMPLES:
            with self.subTest(url=url):
                record = parse_npm_fetch_line(_line(url=url))
                self.assertIsNone(record)
                with self.assertRaises(TypeError):
                    fetch_group_identity(record, NetworkUrlDisplay.HOST_PATH)
                with self.assertRaises(TypeError):
                    canonical_fetch_text(record, NetworkUrlDisplay.HOST_PATH)

    def test_encoded_controls_are_accepted_and_render_inertly(self):
        cases = {
            "https://example.com/a%00junk": "example.com/a%00junk",
            "https://example.com/a%1Bjunk": "example.com/a%1Bjunk",
            "https://example.com/a%7Fjunk": "example.com/a%7Fjunk",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                record = parse_npm_fetch_line(_line(url=url))
                self.assertIsNotNone(record)
                assert record is not None
                key = fetch_group_identity(record, NetworkUrlDisplay.HOST_PATH)
                assert key is not None
                self.assertEqual(expected, key.host_path)
                text = canonical_fetch_text(record, NetworkUrlDisplay.HOST_PATH)
                assert text is not None
                self.assertEqual(
                    f"npm http fetch GET 200 {expected}", text
                )
                for control in ("\x00", "\x1b", "\x7f"):
                    self.assertNotIn(control, text)


class TestFetchNumericBoundaries(unittest.TestCase):
    """Latency and attempt digits are bounded conservatively."""

    BELOW = 17
    AT = 18
    ABOVE = 19
    BEYOND_PYTHON_LIMIT = 4_301
    FAR_BEYOND = 20_000

    def test_latency_below_and_at_the_bound_parse(self):
        for digits in (self.BELOW, self.AT):
            text = "7" * digits
            with self.subTest(digits=digits):
                record = parse_npm_fetch_line(_line(latency=text + "ms"))
                assert record is not None
                self.assertEqual(int(text), record.latency_ms)

    def test_latency_above_the_bound_is_rejected(self):
        self.assertIsNone(
            parse_npm_fetch_line(_line(latency="7" * self.ABOVE + "ms"))
        )

    def test_attempt_below_and_at_the_bound_parse(self):
        for digits in (self.BELOW, self.AT):
            text = "3" * digits
            with self.subTest(digits=digits):
                record = parse_npm_fetch_line(_line(suffix=f" attempt #{text}"))
                assert record is not None
                self.assertEqual(int(text), record.attempt)

    def test_attempt_above_the_bound_is_rejected(self):
        self.assertIsNone(
            parse_npm_fetch_line(_line(suffix=f" attempt #{'3' * self.ABOVE}"))
        )

    def test_oversized_values_return_none_without_raising(self):
        oversized = (self.ABOVE, self.BEYOND_PYTHON_LIMIT, self.FAR_BEYOND)
        for digits in oversized:
            with self.subTest(field="latency", digits=digits):
                try:
                    result = parse_npm_fetch_line(
                        _line(latency="9" * digits + "ms")
                    )
                except Exception as exc:  # pragma: no cover - failure path
                    self.fail(f"latency parser raised {exc!r}")
                self.assertIsNone(result)
            with self.subTest(field="attempt", digits=digits):
                try:
                    result = parse_npm_fetch_line(
                        _line(suffix=f" attempt #{'9' * digits}")
                    )
                except Exception as exc:  # pragma: no cover - failure path
                    self.fail(f"attempt parser raised {exc!r}")
                self.assertIsNone(result)

    def test_leading_zeros_are_insignificant(self):
        record = parse_npm_fetch_line(
            _line(
                latency="0" * self.FAR_BEYOND + "7ms",
                suffix=f" attempt #{'0' * self.FAR_BEYOND}5",
            )
        )
        assert record is not None
        self.assertEqual(7, record.latency_ms)
        self.assertEqual(5, record.attempt)

    def test_all_zero_fields_are_recognized(self):
        record = parse_npm_fetch_line(
            _line(latency="0" * self.FAR_BEYOND + "ms")
        )
        assert record is not None
        self.assertEqual(0, record.latency_ms)

    def test_ordinary_and_moderately_large_values_still_parse(self):
        record = parse_npm_fetch_line(
            _line(
                latency="999999999999ms",
                suffix=" attempt #123456789012 (cache stale)",
            )
        )
        assert record is not None
        self.assertEqual(999999999999, record.latency_ms)
        self.assertEqual(123456789012, record.attempt)
        self.assertEqual("stale", record.cache_outcome)


class TestFetchParserIntrospection(unittest.TestCase):
    """4.6 — table-driven coverage across the whole grammar surface."""

    _METHODS = ("GET", "HEAD", "POST", "DELETE")
    _STATUSES = ("200", "201", "204", "299")
    _LATENCIES = ("0ms", "15ms", "999999ms")
    _ATTEMPTS = (None, 0, 1, 42)
    _CACHES = (None, "miss", "hit", "stale")

    def _build(
        self,
        *,
        method: str = "GET",
        status: str = "200",
        url: str = RESOURCE_URL,
        latency: str = "15ms",
        attempt: int | None = None,
        cache: str | None = None,
        extra: str = "",
    ) -> str:
        suffix = ""
        if attempt is not None:
            suffix += f" attempt #{attempt}"
        if cache is not None:
            suffix += f" (cache {cache})"
        return _line(
            method=method,
            status=status,
            url=url,
            latency=latency,
            suffix=suffix + extra,
        )

    def test_valid_cross_product_parses_exactly(self):
        for method in self._METHODS:
            for status in self._STATUSES:
                for latency in self._LATENCIES:
                    for attempt in self._ATTEMPTS:
                        for cache in self._CACHES:
                            line = self._build(
                                method=method,
                                status=status,
                                latency=latency,
                                attempt=attempt,
                                cache=cache,
                            )
                            with self.subTest(line=line):
                                record = parse_npm_fetch_line(line)
                                self.assertIsNotNone(record)
                                assert record is not None
                                self.assertEqual(method, record.method)
                                self.assertEqual(int(status), record.status)
                                self.assertEqual(int(latency[:-2]), record.latency_ms)
                                self.assertEqual(attempt, record.attempt)
                                self.assertEqual(cache, record.cache_outcome)

    def test_each_single_field_corruption_is_rejected(self):
        corruptions: tuple[tuple[str, str], ...] = (
            *(("method", value) for value in
              ("get", "Get", "GET2", "G3T", "", "G" * 17)),
            *(("status", value) for value in
              ("199", "300", "099", "99", "2000", "2OO", "abc")),
            *(("url", value) for value in
              ("not-a-url", "https://", "ftp:/x", "", "x",
               "https://%", "https://%2e", "https://[", "https://?x",
               "https:///x", "http://:80/x", "http://127.0.0.1:70000/x",
               "https://user@/x")),
            *(("latency", value) for value in
              ("15", "ms", "15s", "15mks", "15m", "15h", "-15ms", "+15ms",
               "1.5ms", "15MS", "15 ms")),
            *(("suffix", value) for value in
              (" attempt 2", " attempt #", " attempt #x", " attempt #2x",
               " attempt -1", " attempts #2", " #2", " attempt #2 #3")),
            *(("suffix", value) for value in
              (" (cache)", " (cache MISS)", " (cache miss", " (cache m1ss)",
               " (cache )", " (cache miss) extra")),
            ("suffix", " (cache miss) attempt #2"),
        )
        for field, value in corruptions:
            if field == "suffix":
                line = self._build(extra=value)
            else:
                line = self._build(**{field: value})  # type: ignore[arg-type]
            with self.subTest(field=field, value=value, line=line):
                self.assertIsNone(parse_npm_fetch_line(line))

    def test_flat_corruptions_never_partially_parse_or_group(self):
        valid = parse_npm_fetch_line(CACHE_MISS)
        assert valid is not None
        base_key = fetch_group_identity(valid, NetworkUrlDisplay.REDACTED)
        # A longer but still valid latency is a complete line whose redacted
        # identity must not be split by the changed value.
        for tolerated in (CACHE_MISS.replace(" 15ms", " 015ms", 1),):
            with self.subTest(tolerated=tolerated):
                record = parse_npm_fetch_line(tolerated)
                assert record is not None
                self.assertEqual(
                    base_key,
                    fetch_group_identity(record, NetworkUrlDisplay.REDACTED),
                )
        for rejected in (
            CACHE_MISS + "x",
            CACHE_MISS + " ",
            CACHE_MISS + " ms",
            CACHE_MISS + "0x",
            CACHE_MISS.replace(" 15ms", " 15msx", 1),
            CACHE_MISS.replace(" 15ms", " 15ms ", 1),
        ):
            with self.subTest(rejected=rejected):
                self.assertIsNone(parse_npm_fetch_line(rejected))

    def test_redacted_identity_is_injective_over_its_key_fields(self):
        keys = set()
        expected = 0
        for method in self._METHODS:
            for status in self._STATUSES:
                for attempt in self._ATTEMPTS:
                    for cache in self._CACHES:
                        expected += 1
                        record = parse_npm_fetch_line(
                            self._build(
                                method=method,
                                status=status,
                                attempt=attempt,
                                cache=cache,
                            )
                        )
                        assert record is not None
                        keys.add(
                            fetch_group_identity(record, NetworkUrlDisplay.REDACTED)
                        )
        self.assertEqual(expected, len(keys))

    def test_latency_and_url_never_split_a_redacted_group(self):
        keys = set()
        for latency in ("0ms", "15ms", "999999ms"):
            for url in (
                RESOURCE_URL,
                "https://registry.npmjs.org/other/-/other-9.9.9.tgz",
                "https://cdn.example.org/a/b",
            ):
                record = parse_npm_fetch_line(
                    self._build(latency=latency, url=url, cache="miss")
                )
                assert record is not None
                keys.add(fetch_group_identity(record, NetworkUrlDisplay.REDACTED))
        self.assertEqual(1, len(keys))

    def test_host_path_identity_normalizes_url_variants(self):
        equivalent = (
            "https://registry.npmjs.org/a/b",
            "HTTPS://REGISTRY.NPMJS.ORG/a/b",
            "https://registry.npmjs.org:443/a/b",
            "https://registry.npmjs.org./a/b",
            "https://user:pw@registry.npmjs.org/a/b?q=1#f",
        )
        keys = set()
        for url in equivalent:
            record = parse_npm_fetch_line(self._build(url=url))
            with self.subTest(url=url):
                assert record is not None
                keys.add(fetch_group_identity(record, NetworkUrlDisplay.HOST_PATH))
        self.assertEqual(1, len(keys))
        for url in (
            "https://registry.npmjs.org/a/b%2Fc",
            "https://registry.npmjs.org/a/b/",
            "https://other.example.org/a/b",
        ):
            record = parse_npm_fetch_line(self._build(url=url))
            with self.subTest(distinct=url):
                assert record is not None
                self.assertNotIn(
                    fetch_group_identity(record, NetworkUrlDisplay.HOST_PATH), keys
                )

    def test_secret_bearing_variants_never_leak_or_partially_parse(self):
        record = parse_npm_fetch_line(
            self._build(url="https://registry.npmjs.org/a/secretpath/b")
        )
        assert record is not None
        key = fetch_group_identity(
            record, NetworkUrlDisplay.HOST_PATH, secrets=("secretpath",)
        )
        text = canonical_fetch_text(
            record, NetworkUrlDisplay.HOST_PATH, secrets=("secretpath",)
        )
        self.assertNotIn("secretpath", repr(key))
        self.assertNotIn("secretpath", text or "")
        self.assertIn(REDACTED_PATH_MARKER, text or "")


class TestParserProjectorAlignment(unittest.TestCase):
    """Parser host acceptance must delegate to the shared projector.

    The parser must not own an independent DNS-label grammar: every hostname
    the projector normalizes (uppercase, trailing FQDN dot, IPv4/IPv6,
    punycode, raw IDNA, percent-encoded, underscore reg-name) must be
    recognized, and the two must agree on the normalized hostname and path.
    """

    #: Hosts the projector normalizes and emits a host/path fact for.
    ACCEPTED = (
        RESOURCE_URL,
        "https://cdn.example.org/a/b",
        "HTTPS://REGISTRY.NPMJS.ORG/a/b",
        "https://registry.npmjs.org./a/b",
        "http://127.0.0.1:8080/a",
        "http://[::1]:8080/a",
        "http://[2001:db8::1]/a/b",
        "https://xn--caf-dma.org/a/b",
        "https://caf\u00e9.example.org/a/b",
        "https://%72egistry.npmjs.org/a/b",
        "https://%65xample.com/x",
        "https://a_b.org/x",
        "https://registry.npmjs.org:443/a/b",
        "https://registry.npmjs.org:0/a",
        "https://registry.npmjs.org:65535/a",
        "https://user:pw@registry.npmjs.org/a/b?q=1#f",
        "https://example..org/x",
        "https://-bad.org/x",
        "https://bad-.org/x",
    )

    #: Hosts whose authority/hostname cannot be normalized.
    MALFORMED = (
        "https://%",
        "https://%2e",
        "https://[",
        "https://]",
        "https://?x",
        "https:///x",
        "https://",
        "http://:80/x",
        "https://user@/x",
        "https://exa%20mple.org/x",
        "https://example.com:/x",
        "https://example.com:",
        "https://[2001:db8::1]:/x",
        "https://example.com%3A/x",
        "https://example.com%3Aabc/x",
        "https://example.com%3A99999/x",
        "http://127.0.0.1:70000/x",
        "http://127.0.0.1:0x50/x",
        "http://registry.npmjs.org:70000/a",
    )

    #: A generated host matrix for the projector-oracle property check.
    HOSTS = (
        "registry.npmjs.org",
        "REGISTRY.NPMJS.ORG",
        "registry.npmjs.org.",
        "127.0.0.1",
        "[::1]",
        "[2001:db8::1]",
        "xn--caf-dma.org",
        "caf\u00e9.example.org",
        "%72egistry.npmjs.org",
        "a_b.org",
        "example..org",
        "-bad.org",
        "bad-.org",
        "registry.npmjs.org:443",
        "localhost",
        "n",
    )

    def test_alignment_fixtures_are_accepted_by_the_projector(self):
        for url in self.ACCEPTED:
            with self.subTest(url=url):
                self.assertTrue(
                    sanitize_host_paths(url),
                    "alignment fixture is no longer accepted by the projector",
                )

    def test_every_projector_accepted_url_is_recognized(self):
        for url in self.ACCEPTED:
            with self.subTest(url=url):
                self.assertIsNotNone(parse_npm_fetch_line(_line(url=url)))

    def test_host_path_identity_matches_the_projector_fact(self):
        for url in self.ACCEPTED:
            with self.subTest(url=url):
                record = parse_npm_fetch_line(_line(url=url))
                assert record is not None
                facts = sanitize_host_paths(url)
                key = fetch_group_identity(record, NetworkUrlDisplay.HOST_PATH)
                assert key is not None
                self.assertEqual(facts[0].text, key.host_path)

    def test_normalized_url_host_matches_the_projector_hostname(self):
        for url in self.ACCEPTED:
            with self.subTest(url=url):
                self.assertEqual(
                    sanitize_host_paths(url)[0].hostname,
                    normalized_url_host(url),
                )

    def test_malformed_authorities_have_no_fact_and_no_record(self):
        for url in self.MALFORMED:
            with self.subTest(url=url):
                self.assertEqual((), sanitize_host_paths(url))
                self.assertIsNone(parse_npm_fetch_line(_line(url=url)))

    def test_generated_host_forms_agree_with_the_projector(self):
        for host in self.HOSTS:
            url = f"http://{host}/a/b"
            with self.subTest(host=host):
                facts = sanitize_host_paths(url)
                record = parse_npm_fetch_line(_line(url=url))
                if facts:
                    self.assertIsNotNone(record)
                    assert record is not None
                    key = fetch_group_identity(
                        record, NetworkUrlDisplay.HOST_PATH
                    )
                    assert key is not None
                    self.assertEqual(facts[0].text, key.host_path)


class TestFetchPathologicalInputs(unittest.TestCase):
    """Deterministic complexity regressions: no exception, correct verdict.

    These assert the recognition postcondition (a recognized line always has a
    shared-projector-normalizable host) and unambiguous rejections, plus the
    absence of an exception.  They deliberately avoid wall-clock thresholds,
    which are environment-sensitive.
    """

    def test_pathological_inputs_are_handled_without_error(self):
        huge_url = "npm http fetch GET 200 https://registry.npmjs.org/" + "a" * 200_000
        many_segments = "/".join("a" * 10 for _ in range(20_000))
        lines = (
            huge_url,  # no latency terminator
            "npm http fetch GET 200 " + "a" * 100_000 + "://x 15ms",
            "npm http fetch GET 200 https://x/" + many_segments + " 15ms",
            "npm http fetch GET 200 https://x/" + "." * 100_000 + " 15ms",
            "npm http fetch GET 200 https://x 15ms",
            "npm http fetch " + "GET 200 " * 5_000,
            _line(latency="9" * 5_000 + "ms"),
            _line(url="https://" + "%" * 5_000),
            _line(url="https://" + "9" * 5_000),
            _line(url="https://" + ".".join(["9" * 5_000] * 4)),
            _line(url="https://" + "a" * 300 + "." + "b" * 300),
        )
        for line in lines:
            with self.subTest(size=len(line)):
                try:
                    record = parse_npm_fetch_line(line)
                except Exception as exc:  # pragma: no cover - failure path
                    self.fail(f"parser raised {type(exc).__name__}: {exc}")
                if record is not None:
                    # Recognition is always backed by the shared projector.
                    self.assertIsNotNone(normalized_url_host(record.url))

    def test_unambiguously_pathological_lines_are_rejected(self):
        huge_url = "npm http fetch GET 200 https://registry.npmjs.org/" + "a" * 200_000
        for line in (
            huge_url,
            "npm http fetch " + "GET 200 " * 5_000,
            _line(latency="9" * 5_000 + "ms"),
            _line(url="https://" + "%" * 5_000),
        ):
            with self.subTest(size=len(line)):
                self.assertIsNone(parse_npm_fetch_line(line))


class TestIncrementalFetchRecognizer(unittest.TestCase):
    """The streaming recognizer matches the pure parser without the URL.

    :class:`NpmFetchRecognizer` validates the successful-fetch grammar
    incrementally and returns a parser-minimal :class:`SafeFetchRecord` that
    carries no source URL, so the collector never retains a complete source
    line in ``redacted`` or ``host-path``.
    """

    #: Lines exercising every accepted shape and the common near misses.
    CORPUS = (
        CACHE_MISS,
        _line(),
        _line(suffix=" attempt #2"),
        _line(suffix=" attempt #3 (cache hit)"),
        _line(latency="0ms"),
        _line(latency="000ms"),
        _line(method="HEAD", status="204"),
        _line(url="https://user:pass@example.com:443/a/b?x=1#f"),
        _line(url="https://[::1]:8080/p"),
        _line(method="get"),
        _line(status="404"),
        _line(status="1000"),
        _line(url="https://%"),
        _line(url="https:///x"),
        _line(url="http://:80/x"),
        _line(url="https://example.com:/x"),
        _line(url="https://example.com:99999/x"),
        _line(url="https://"),
        _line(latency="15"),
        _line(latency="15s"),
        _line(suffix=" attempt #"),
        _line(suffix=" (cache )"),
        _line(suffix=" (cache miss"),
        _line(suffix=" (cache miss) extra"),
        _line(suffix=" (cache MISS)"),
        _line(suffix="  (cache miss)"),
        _line() + " ",
    )

    def _recognize(self, line: str, chunk_size: int | None = None):
        recognizer = NpmFetchRecognizer()
        if chunk_size is None:
            recognizer.feed(line)
        else:
            for start in range(0, len(line), chunk_size):
                recognizer.feed(line[start : start + chunk_size])
        return recognizer, recognizer.finish()

    def test_matches_the_pure_parser_for_every_shape(self):
        for line in self.CORPUS:
            with self.subTest(line=line):
                expected = parse_npm_fetch_line(line)
                _recognizer, record = self._recognize(line)
                self.assertEqual(expected is None, record is None)
                if expected is not None and record is not None:
                    self.assertEqual(expected.method, record.method)
                    self.assertEqual(expected.status, record.status)
                    self.assertEqual(expected.attempt, record.attempt)
                    self.assertEqual(expected.cache_outcome, record.cache_outcome)

    def test_recognition_is_chunk_boundary_independent(self):
        for line in self.CORPUS:
            expected = parse_npm_fetch_line(line)
            for chunk_size in (1, 2, 3, 5, 13):
                with self.subTest(line=line, chunk_size=chunk_size):
                    _recognizer, record = self._recognize(line, chunk_size)
                    self.assertEqual(expected is None, record is None)

    def test_recognizer_retains_no_source_line_or_url(self):
        line = _line(
            url="https://user:pass@example.com/secretpath?token=abc#frag"
        )
        recognizer, record = self._recognize(line, chunk_size=1)
        self.assertIsNotNone(record)
        self.assertFalse(hasattr(record, "url"))
        retained = repr(vars(recognizer))
        for fragment in (
            "user:pass",
            "secretpath",
            "token=abc",
            "#frag",
            "https://",
            "example.com",
        ):
            self.assertNotIn(fragment, retained)

    def test_long_authorities_and_cache_outcomes_match_the_pure_parser(self):
        # A long URL path streams through without being retained.
        long_path = _line(url="https://example.com/" + "a" * 100_000)
        _recognizer, record = self._recognize(long_path)
        self.assertIsNotNone(record)
        # There is no undocumented authority-length grammar limit: an
        # authority longer than any internal parser buffer is recognized
        # exactly when the authoritative complete-line parser recognizes it.
        long_authority = "a" * 5_000 + ".com"
        for url in (
            f"https://{long_authority}/x",
            f"https://user:pass@{long_authority}/x",
            f"https://{long_authority}:8080/x",
        ):
            with self.subTest(shape=url[:32]):
                line = _line(url=url)
                expected = parse_npm_fetch_line(line)
                self.assertIsNotNone(expected)
                for chunk_size in (None, 1, 7, 4096):
                    _recognizer, record = self._recognize(line, chunk_size)
                    self.assertIsNotNone(record)
        # A long cache outcome is likewise not artificially bounded.
        line = _line(suffix=" (cache " + "a" * 200 + ")")
        self.assertIsNotNone(parse_npm_fetch_line(line))
        _recognizer, record = self._recognize(line)
        self.assertIsNotNone(record)
        self.assertEqual("a" * 200, record.cache_outcome)

    def test_canonical_unsafe_matches_source_field_matching(self):
        for secrets in ((), ("miss",), ("GET",), ("200",), ("GET 200",), ("#3",), ("sec",)):
            for line in (
                CACHE_MISS,
                _line(suffix=" attempt #3 (cache miss)"),
                _line(suffix=" attempt #3"),
                _line(url="https://example.com/x"),
            ):
                with self.subTest(secrets=secrets, line=line):
                    pure = parse_npm_fetch_line(line)
                    recognizer = NpmFetchRecognizer(secrets)
                    # Feed one character at a time so a secret spanning two
                    # fields or two input chunks is still detected.
                    for character in line:
                        recognizer.feed(character)
                    recognizer.finish()
                    expected = any(
                        secret and secret in fetch_source_fields_text(pure)
                        for secret in secrets
                    )
                    self.assertEqual(expected, recognizer.canonical_unsafe)

    def test_safe_record_carries_no_url_and_groups_identically(self):
        pure = parse_npm_fetch_line(CACHE_MISS)
        facts = sanitize_host_paths(pure.url + " ")
        self.assertTrue(facts)
        safe = SafeFetchRecord(
            method="GET",
            status=200,
            attempt=None,
            cache_outcome="miss",
            host_path=facts[0],
        )
        for display in (NetworkUrlDisplay.REDACTED, NetworkUrlDisplay.HOST_PATH):
            with self.subTest(display=display):
                self.assertEqual(
                    canonical_fetch_text(pure, display),
                    canonical_fetch_text(safe, display),
                )
                self.assertEqual(
                    fetch_group_identity(pure, display),
                    fetch_group_identity(safe, display),
                )

    def test_safe_record_host_path_uses_the_supplied_fact(self):
        fact = SafeHostPath(
            hostname="registry.npmjs.org",
            path="/npm-http-research-fixture/-/npm-http-research-fixture-1.0.0.tgz",
        )
        safe = SafeFetchRecord(
            method="GET",
            status=200,
            attempt=None,
            cache_outcome="miss",
            host_path=fact,
        )
        self.assertEqual(
            f"npm http fetch GET 200 {fact.text} (cache miss)",
            canonical_fetch_text(safe, NetworkUrlDisplay.HOST_PATH),
        )
        key = fetch_group_identity(safe, NetworkUrlDisplay.HOST_PATH)
        self.assertIsNotNone(key)
        self.assertEqual(fact.text, key.host_path)


class TestIncrementalFetchRecognizerEquivalence(unittest.TestCase):
    """The recognizer matches ``parse_npm_fetch_line`` accept/reject.

    Removing the internal authority buffer must not change which lines are
    recognized.  The recognizer validates the host from its parser-minimal,
    user-information-free candidate and, when user information could decode
    into a structural token, fails closed, so it never admits a line the
    complete-line parser rejects.
    """

    HOSTS = (
        "example.com",
        "EXAMPLE.com.",
        "m\u00fcnchen.de",
        "xn--mnchen-3ya.de",
        "%65xample.com",
        "example%2Ecom",
        "exam_ple.com",
        "127.0.0.1",
        "[::1]",
        "[2001:db8::1]",
        "localhost",
        "registry.npmjs.org:8080",
        "registry.npmjs.org:0",
        "registry.npmjs.org:65535",
        "[::1]:443",
    )
    USERINFOS = (
        "",
        "user@",
        "user:password@",
        "user:password@",
        "user%40name@",
        "\u00fcser@",
        "u" * 64 + "@",
    )
    BODIES = (
        "",
        "/",
        "/a/b",
        "/a/b%20c",
        "?q=1",
        "#frag",
        "?q=1#frag",
        "/path?x=1#y",
    )
    MALFORMED = (
        "https://%",
        "https://%2e",
        "https://[",
        "https://]",
        "https://?x",
        "https:///x",
        "https://",
        "http://:80/x",
        "https://user@/x",
        "https://example.com:/x",
        "https://example.com:",
        "https://[2001:db8::1]:/x",
        "https://example.com%3A/x",
        "http://127.0.0.1:70000/x",
        "http://registry.npmjs.org:70000/a",
    )

    def _recognize(self, line: str, chunk_size: int | None = None):
        recognizer = NpmFetchRecognizer()
        if chunk_size is None:
            recognizer.feed(line)
        else:
            for start in range(0, len(line), chunk_size):
                recognizer.feed(line[start : start + chunk_size])
        return recognizer, recognizer.finish()

    def _assert_equivalent(self, line: str, chunk_sizes=(None, 1, 3, 17)):
        expected = parse_npm_fetch_line(line)
        for chunk_size in chunk_sizes:
            with self.subTest(line=line[:64], chunk_size=chunk_size):
                _recognizer, record = self._recognize(line, chunk_size)
                self.assertEqual(expected is None, record is None)
                if expected is not None and record is not None:
                    self.assertEqual(expected.method, record.method)
                    self.assertEqual(expected.status, record.status)
                    self.assertEqual(expected.attempt, record.attempt)
                    self.assertEqual(
                        expected.cache_outcome, record.cache_outcome
                    )

    def test_matches_the_pure_parser_for_realistic_authorities(self):
        for userinfo in self.USERINFOS:
            for host in self.HOSTS:
                for body in self.BODIES:
                    url = f"https://{userinfo}{host}{body}"
                    self._assert_equivalent(
                        _line(url=url), chunk_sizes=(None, 1, 5)
                    )

    def test_matches_the_pure_parser_for_known_malformed_urls(self):
        for url in self.MALFORMED:
            with self.subTest(url=url):
                self.assertIsNone(parse_npm_fetch_line(_line(url=url)))
                self._assert_equivalent(_line(url=url), chunk_sizes=(None, 1, 2))

    def test_matches_the_pure_parser_at_every_split_point(self):
        # Boundaries immediately before and after @, :, [, ], and the URL
        # delimiters must not change recognition or the parsed fields.
        for url in (
            "https://user:password@example.com:8080/a/b?q=1#f",
            "https://[2001:db8::1]:443/p",
            "https://user@[::1]:8443/",
            "https://%65xample.com%2E/x",
            "https://user:pass@example.com",
            "https://example.com?x#y",
        ):
            line = _line(url=url)
            expected = parse_npm_fetch_line(line)
            for split in range(len(line) + 1):
                with self.subTest(url=url, split=split):
                    recognizer = NpmFetchRecognizer()
                    recognizer.feed(line[:split])
                    recognizer.feed(line[split:])
                    record = recognizer.finish()
                    self.assertEqual(expected is None, record is None)
                    if expected is not None and record is not None:
                        self.assertEqual(expected.method, record.method)
                        self.assertEqual(expected.status, record.status)
                        self.assertEqual(expected.attempt, record.attempt)
                        self.assertEqual(
                            expected.cache_outcome, record.cache_outcome
                        )

    def test_long_authorities_match_across_chunk_boundaries(self):
        long_label = "a" * 3_000
        for url in (
            f"https://{long_label}.example.com/x",
            f"https://user:pass@{long_label}.example.com/x",
            f"https://{long_label}.example.com:8080/x",
            f"https://{'u' * 3_000}:pass@example.com/x",
        ):
            with self.subTest(shape=url[:48]):
                self.assertIsNotNone(parse_npm_fetch_line(_line(url=url)))
                self._assert_equivalent(
                    _line(url=url), chunk_sizes=(None, 1, 64, 4096)
                )

    def test_recognizer_never_over_accepts_a_generated_authority(self):
        rng = random.Random(20240607)
        atoms = list("abc019._-:%[]@") + [
            "%2F", "%3F", "%23", "%40", "%3A", "%5B", "%5D", "%25",
            "%2E", "%00", "%09", "%C3", "%BC",
        ]
        checked = 0
        for _ in range(2_000):
            authority = "".join(
                rng.choice(atoms) for _ in range(rng.randint(1, 10))
            )
            if any(character in authority for character in " /?#"):
                continue
            line = _line(url=f"https://{authority}")
            if parse_npm_fetch_line(line) is not None:
                continue
            checked += 1
            with self.subTest(authority=authority):
                _recognizer, record = self._recognize(line, chunk_size=1)
                self.assertIsNone(record, authority)
        self.assertGreater(checked, 100)

    def test_recognizer_state_never_retains_credentials_per_chunk(self):
        url = (
            "https://credname:credsecret@example.com/credpath"
            "?token=credq#credf"
        )
        line = _line(url=url)
        always_forbidden = (
            "credsecret",
            "credname:credsecret",
            "credname:credsecret@example.com",
            url,
            "credpath",
            "token=credq",
            "#credf",
        )
        for chunk_size in (1, 2, 3):
            recognizer = NpmFetchRecognizer()
            for start in range(0, len(line), chunk_size):
                recognizer.feed(line[start : start + chunk_size])
                retained = _retained_state_strings(recognizer)
                for fragment in always_forbidden:
                    self.assertNotIn(
                        fragment, retained, (chunk_size, start)
                    )
                # A single-label username is indistinguishable from a host
                # candidate until user information is recognized, so it may
                # only be asserted gone once the ``@`` has been consumed.
                if "credname:credsecret@" in line[: start + chunk_size]:
                    self.assertNotIn("credname", retained)
            recognizer.finish()
            retained = _retained_state_strings(recognizer)
            for fragment in always_forbidden + ("credname",):
                self.assertNotIn(fragment, retained)


if __name__ == "__main__":
    unittest.main()
