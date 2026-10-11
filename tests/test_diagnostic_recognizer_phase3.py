"""Revised P3: source ownership, irreversible rendering and lifetime work.

Historical UNKNOWN hiding / unrestricted suffix batch equivalence are not
contracts. These tests intentionally require persistent overlap states.
"""
from __future__ import annotations

import importlib
import unittest

from docker.versioning.diagnostic_identity import SessionUrlIdentity
from docker.versioning.diagnostic_projection import (
    DiagnosticProjector, INCOMPLETE_TOKEN_MARKER, OVERSIZED_TOKEN_MARKER,
    PENDING_LIMIT_BYTES,
)
from docker.versioning.model import NetworkUrlDisplay


class Phase3Tests(unittest.TestCase):
    partition_identity = SessionUrlIdentity()

    def module(self):
        try:
            return importlib.import_module("docker.versioning.diagnostic_recognizer")
        except ModuleNotFoundError:
            self.fail("P3 requires the persistent diagnostic recognizer")

    def projector(self, secrets=(), *, host_path=False, url_identity=None):
        if not hasattr(DiagnosticProjector, "recognition_stats"):
            self.fail("P3 requires projector lifetime recognition counters")
        return DiagnosticProjector(secrets, url_identity=url_identity, url_display=(
            NetworkUrlDisplay.HOST_PATH if host_path else NetworkUrlDisplay.REDACTED))

    def project(self, text, secrets=(), cuts=(), *, host_path=False, bytewise=False):
        p = self.projector(secrets, host_path=host_path, url_identity=self.partition_identity)
        out = []
        if bytewise:
            for value in text.encode("utf-8"):
                out.extend(p.feed_bytes(bytes((value,))))
                self.assertLessEqual(p.pending_size, PENDING_LIMIT_BYTES)
        else:
            start = 0
            for end in (*cuts, len(text)):
                out.extend(p.feed_text(text[start:end]))
                start = end
                self.assertLessEqual(p.pending_size, PENDING_LIMIT_BYTES)
        out.extend(p.finish())
        self.assertEqual(len(text), p.recognition_stats.chars)
        return "".join(out), p.hostnames, p.host_paths, p.url_fingerprints

    def test_exact_retention_and_independent_prefix_delivery(self):
        for prefix in ("https:", "https:/", "https%", "git%3", "h%252574tps:"):
            p = self.projector()
            self.assertEqual("safe ", "".join(p.feed_text("safe " + prefix)))
            self.assertEqual(prefix, p._pending)
            self.assertEqual(len(prefix.encode()), p.pending_size)
            self.assertEqual(5, p._buffer_start)

    def test_unknown_text_is_not_an_unbounded_candidate(self):
        p = self.projector()
        text = "custom+transport://private/x "
        self.assertEqual(text, "".join(p.feed_text(text)))
        self.assertEqual(0, p.pending_size)
        self.assertEqual((), p.hostnames)

    def test_complete_separator_is_distinct_from_pending_prefix(self):
        r = self.module().IncrementalRecognizer()
        r.feed("https:/")
        self.assertIsNone(r.earliest_match())
        r.feed("/host")
        self.assertEqual(0, r.earliest_match().start)
        self.assertIsNone(r.result("https://host").candidate.end)

    def test_completed_url_precedes_ambiguous_tail(self):
        p = self.projector()
        self.assertEqual("<redacted> ", "".join(p.feed_text("https://one.example/a git:")))
        self.assertEqual(("one.example",), p.hostnames)
        self.assertEqual("git:", p._pending)
        self.assertEqual("<redacted> ", "".join(p.feed_text("//two.example/b ")))
        self.assertEqual(("one.example", "two.example"), p.hostnames)
        self.assertEqual((), p.finish())

    def test_secret_prefix_commit_preserves_overlapping_url_state(self):
        p = self.projector(("TOKENht",))
        self.assertEqual("safe <secret>", "".join(p.feed_text("safe TOKENht")))
        cursor = p._recognizer
        self.assertEqual("<redacted>", "".join(p.feed_text("tps://host")))
        self.assertIs(cursor, p._recognizer)
        self.assertEqual(" done", "".join(p.feed_text(" done")) + "".join(p.finish()))
        self.assertEqual((), p.hostnames)

    def test_secret_crossing_url_end_never_leaks_suffix(self):
        p = self.projector(("password done",))
        self.assertEqual("<redacted>", "".join(p.feed_text("https://host/password do")))
        self.assertEqual(" next", "".join(p.feed_text("ne next")) + "".join(p.finish()))
        self.assertNotIn("done", p._pending)

    def test_every_split_and_utf8_byte_partition(self):
        cases = (
            ("before TOKEN after", ("TOKEN",), "before <secret> after"),
            ("before TOKENhttps://host/a end", ("TOKEN",), "before <secret><redacted> end"),
            ("safe TOKENhttps://host end", ("TOKENht",), "safe <secret><redacted> end"),
            ("https://host/password done after", ("password done",), "<redacted> after"),
            ("xx h%252574tps%253A%252F%252Fbücher.example/x end", (), "xx <redacted> end"),
            ("%54%4f%4b%45%4e text", ("TOKEN",), "%54%4f%4b%45%4e text"),
            ("https://host/%54%4f%4b%45%4e end", ("TOKEN",), "<redacted> end"),
            ("https://one.example/a https://two.example/b end", (), "<redacted> <redacted> end"),
        )
        for text, secrets, expected in cases:
            baseline = self.project(text, secrets)
            self.assertEqual(expected, baseline[0])
            for split in range(len(text) + 1):
                self.assertEqual(baseline, self.project(text, secrets, (split,)))
            self.assertEqual(baseline, self.project(text, secrets, bytewise=True))

    def test_host_and_path_commit_independently_without_early_facts(self):
        p = self.projector(host_path=True)
        self.assertEqual("host", "".join(p.feed_text("https://host/pa")))
        self.assertEqual((), p.hostnames)
        self.assertEqual("/path", "".join(p.feed_text("th?query")))
        self.assertEqual((), p.hostnames)
        self.assertEqual(" end", "".join(p.feed_text(" end")) + "".join(p.finish()))
        self.assertEqual(("host",), p.hostnames)

    def test_authority_waits_for_userinfo_ipv6_and_port(self):
        for first, last, host in (("https://user", "@host/pa", "host"),
                                 ("https://[2001:db8::", "1]:443/pa", "2001:db8::1"),
                                 ("https://host:", "8443/pa", "host")):
            p = self.projector(host_path=True)
            self.assertEqual((), p.feed_text(first))
            self.assertEqual(host, "".join(p.feed_text(last)))
            self.assertEqual("/pa", "".join(p.finish()))

    def test_component_secret_settlement_and_crossing_coverage(self):
        p = self.projector(("host/path",), host_path=True)
        self.assertEqual((), p.feed_text("https://host/pa"))
        self.assertEqual("<redacted-host>", "".join(p.feed_text("th")))
        self.assertEqual("/<redacted-path>", "".join(p.finish()))
        self.assertEqual((), p.hostnames)

    def test_host_path_partition_invariance(self):
        for text, secrets in (("https://host/public?q=x end", ()),
                              ("https://TOKEN.example/public end", ("TOKEN",)),
                              ("https://host/TOKEN/private end", ("TOKEN",)),
                              ("https://user:TOKEN@host/public end", ("TOKEN",)),
                              ("https%253A%252F%252Fhost/public end", ())):
            baseline = self.project(text, secrets, host_path=True)
            for split in range(len(text) + 1):
                self.assertEqual(baseline, self.project(text, secrets, (split,), host_path=True))
            self.assertEqual(baseline, self.project(text, secrets, host_path=True, bytewise=True))

    def test_redacted_early_marker_survives_eof_abort_and_overflow(self):
        for ending in ("eof", "abort", "overflow"):
            p = self.projector()
            self.assertEqual("<redacted>", "".join(p.feed_text("https://ho")))
            if ending == "overflow":
                self.assertEqual((), p.feed_text("x" * (PENDING_LIMIT_BYTES + 50)))
            self.assertEqual((), p.finish(abort=ending == "abort"))
            if ending != "eof":
                self.assertEqual((), p.hostnames)

    def test_host_commit_survives_abort_and_overflow(self):
        for ending, marker in (("abort", INCOMPLETE_TOKEN_MARKER),
                               ("overflow", OVERSIZED_TOKEN_MARKER)):
            p = self.projector(host_path=True)
            self.assertEqual("host", "".join(p.feed_text("https://host/pa")))
            out = []
            if ending == "overflow":
                out.extend(p.feed_text("x" * (PENDING_LIMIT_BYTES + 50)))
            out.extend(p.finish(abort=True))
            self.assertEqual(marker, "".join(out))
            self.assertEqual((), p.hostnames)

    def test_completed_display_has_no_abort_token_marker(self):
        p = self.projector(host_path=True)
        self.assertEqual("host/path", "".join(p.feed_text("https://host/path?unfinished")))
        self.assertEqual((), p.finish(abort=True))
        self.assertEqual((), p.hostnames)

    def test_partial_separators_keep_fail_closed_rules(self):
        for abort in (False, True):
            p = self.projector()
            self.assertEqual((), p.feed_text("https:/"))
            self.assertEqual((INCOMPLETE_TOKEN_MARKER,), p.finish(abort=abort))
        p = self.projector()
        self.assertEqual((), p.feed_text("git"))
        self.assertEqual(("git",), p.finish())

    def test_overflow_recovery_observes_boundary_without_replay(self):
        p = self.projector(("password done",))
        r = p._recognizer
        text = "https://host/" + "x" * (PENDING_LIMIT_BYTES + 1) + "password done next"
        out = []
        for character in text:
            out.extend(p.feed_text(character))
            self.assertLessEqual(p.pending_size, PENDING_LIMIT_BYTES)
        out.extend(p.finish())
        self.assertEqual("<redacted> next", "".join(out))
        self.assertIs(r, p._recognizer)
        self.assertEqual(len(text), p.recognition_stats.chars)

    def test_lifetime_work_state_and_single_payload_owner(self):
        work = []
        for count in (100, 200):
            p = self.projector(("TOKENht", "%", "password done"))
            r = p._recognizer
            text = "pre TOKENhttps://host/%25/password done next " * count
            for character in text:
                p.feed_text(character)
                self.assertIs(r, p._recognizer)
                self.assertFalse(hasattr(r, "_tail"))
                self.assertLessEqual(p.pending_size, PENDING_LIMIT_BYTES)
                p.take_facts()
                p.take_host_paths()
            p.finish()
            s = p.recognition_stats
            self.assertEqual(len(text), s.chars)
            self.assertEqual(1, p.work_stats.recognition_instances)
            self.assertEqual(len(text.encode()), p.work_stats.encoded_bytes)
            w = p.work_stats
            work.append(s.layer_char_visits + s.speculative_record_visits
                        + s.secret_ordering_work + s.secret_record_transfers
                        + s.candidate_retirement_visits + s.prefix_checks
                        + s.failure_traversals + s.automaton_outputs
                        + w.buffer_appends + w.buffer_pops
                        + w.materialized_chars + w.provenance_visits)
        self.assertLessEqual(work[1], 2 * work[0] + 64)

    def test_fingerprint_order_waits_for_completion(self):
        p = self.projector()
        p._url_identity = SessionUrlIdentity()
        self.assertEqual("<redacted>", "".join(p.feed_text("https://host/a")))
        self.assertEqual((), p.url_fingerprints)
        p.feed_text(" https://host/b ")
        self.assertEqual(2, len(p.url_fingerprints))
        self.assertNotEqual(*p.url_fingerprints)

    def test_utf8_pending_limit_applies_to_whole_buffer(self):
        p = self.projector(host_path=True)
        out = list(p.feed_text("https://host/" + "界" * PENDING_LIMIT_BYTES))
        self.assertLessEqual(p.pending_size, PENDING_LIMIT_BYTES)
        self.assertEqual("host" + OVERSIZED_TOKEN_MARKER, "".join(out))
        self.assertEqual((), p.finish())

    def test_literal_secret_crossing_url_start_is_prompt_and_ordered(self):
        for split in range(20):
            text = "pre Xhttps://host end"
            expected = self.project(text, ("Xhttps",))
            self.assertEqual("pre <secret><redacted> end", expected[0])
            self.assertEqual(expected, self.project(text, ("Xhttps",), (split,)))

    def test_delayed_host_path_activation_recovers_crossed_boundaries(self):
        p = self.projector(("Xhttps://host/pathZ",), host_path=True)
        cursor = p._recognizer
        self.assertEqual((), p.feed_text("Xhttps://host/path"))
        self.assertEqual("Xhttps://host/path", p._pending)
        self.assertEqual("Xhost/path", "".join(p.feed_text("?q")))
        self.assertEqual(9, p._url.detected)
        self.assertEqual(13, p._url.authority_end)
        self.assertEqual(18, p._url.path_end)
        self.assertEqual((), p.hostnames)
        self.assertTrue(p._url.host_done)
        self.assertTrue(p._url.path_done)
        self.assertEqual(" done", "".join(p.feed_text(" done")))
        self.assertEqual(("host",), p.hostnames)
        self.assertEqual("host/path", p.host_paths[0].text)
        self.assertEqual((), p.finish())
        self.assertIs(cursor, p._recognizer)
        self.assertEqual(len("Xhttps://host/path?q done"), p.recognition_stats.chars)

    def test_delayed_activation_preserves_encoded_raw_separator_coordinates(self):
        for first in ("Xhttps%3A%2F%2Fhost/path", "Xhttps%253A%252F%252Fhost/path"):
            p = self.projector((first + "Z",), host_path=True)
            self.assertEqual((), p.feed_text(first))
            self.assertEqual("Xhost/path", "".join(p.feed_text("?q")))
            self.assertEqual(first.index("host"), p._url.detected)
            self.assertEqual(first.index("/path"), p._url.authority_end)
            self.assertEqual(len(first), p._url.path_end)
            self.assertEqual(" done", "".join(p.feed_text(" done")))
            self.assertEqual((), p.finish())
            text = first + "?q done"
            baseline = self.project(text, (first + "Z",), host_path=True)
            self.assertEqual("Xhost/path done", baseline[0])
            for split in range(len(text) + 1):
                self.assertEqual(baseline, self.project(text, (first + "Z",), (split,), host_path=True))
            self.assertEqual(baseline, self.project(text, (first + "Z",), host_path=True, bytewise=True))

    def test_delayed_activation_non_match_keeps_other_component_secrets(self):
        first = "Xhttps://host/path"
        for other, expected in (("host", "X<redacted-host>/path"),
                                ("/path", "Xhost/<redacted-path>")):
            p = self.projector((first + "Z", other), host_path=True)
            self.assertEqual((), p.feed_text(first))
            self.assertEqual(expected, "".join(p.feed_text("?q")))
            self.assertEqual(" done", "".join(p.feed_text(" done")))
            self.assertEqual((), p.finish())

    def test_pending_secret_at_eof_cannot_release_unsettled_components(self):
        p = self.projector(("host/path",), host_path=True)
        self.assertEqual((), p.feed_text("https://host/pa"))
        self.assertEqual((INCOMPLETE_TOKEN_MARKER,), p.finish())
        self.assertEqual((), p.hostnames)

    def test_commits_before_and_after_raw_terminators_are_replay_free(self):
        module = self.module()
        for cut in (14, 15):
            r = module.IncrementalRecognizer(("password done",))
            r.feed("https://host/x ")
            before = r.stats
            r.consume(cut)
            self.assertEqual(before.chars, r.stats.chars)
            self.assertEqual(before.transitions, r.stats.transitions)
            r.feed("https://next/y ")
            self.assertEqual(15, r.earliest_match().start)
            r.consume(r.consumed)
            self.assertIsNone(r.earliest_match())
            self.assertEqual(30, r.stats.chars)

    def test_single_percent_finalization_and_idempotence(self):
        from unittest.mock import patch
        module = self.module()
        p = self.projector(("%",))
        p.feed_text("https://host/%")
        original = module._Pipeline.finish
        with patch.object(module._Pipeline, "finish", autospec=True, side_effect=original) as finish:
            p.finish()
            p.finish()
            self.assertEqual(1, finish.call_count)
        self.assertEqual(14, p.recognition_stats.chars)

    def test_two_products_share_one_collector_raw_owner_and_line_overflow(self):
        from docker.versioning.npm_diagnostic_stream import NpmDiagnosticStream
        from docker.versioning.host_progress import HostDiagnosticStream
        from docker.npm_environment.streaming import OVERSIZED_DIAGNOSTIC_MARKER as LINE_OVERFLOW_MARKER
        stream = NpmDiagnosticStream(HostDiagnosticStream.STDERR,
                                     network_url_display=NetworkUrlDisplay.HOST_PATH)
        self.assertIs(stream._projector, stream._retained_projector)
        first = stream.feed_bytes(b"https://host/pa")
        self.assertEqual("host", "".join(c.local_text or "" for c in first))
        self.assertEqual("<redacted>", "".join(c.text for c in first))
        chunks = list(stream.feed_bytes(b"x" * (64 * 1024 + 100)))
        chunks.extend(stream.feed_bytes(b"\nnext\n"))
        chunks.extend(stream.finish())
        records = [c for c in chunks if c.finalized]
        self.assertEqual(2, len(records))
        self.assertEqual(1, records[0].text.count(LINE_OVERFLOW_MARKER))
        self.assertNotIn(OVERSIZED_TOKEN_MARKER, records[0].text)
        self.assertEqual("next", records[1].text)
        self.assertEqual(1, stream.tail().count(OVERSIZED_TOKEN_MARKER))
        self.assertEqual(1, stream.tail().count(LINE_OVERFLOW_MARKER))

    def test_cumulative_work_includes_repeated_overflow_recovery(self):
        work = []
        for count in (2, 4):
            p = self.projector(("password done",))
            recognizer = p._recognizer
            text = ("https://host/" + "x" * (PENDING_LIMIT_BYTES + 1)
                    + "password done next ") * count
            output = []
            for start in range(0, len(text), 257):
                output.extend(p.feed_text(text[start:start + 257]))
                self.assertIs(recognizer, p._recognizer)
                self.assertLessEqual(p.pending_size, PENDING_LIMIT_BYTES)
                p.take_facts()
                p.take_host_paths()
            output.extend(p.finish())
            self.assertEqual("<redacted> next " * count, "".join(output))
            s, w = p.recognition_stats, p.work_stats
            self.assertEqual(len(text), s.chars)
            self.assertEqual(1, w.recognition_instances)
            work.append(s.layer_char_visits + s.speculative_record_visits
                        + s.prefix_checks + s.failure_traversals + s.automaton_outputs
                        + s.secret_ordering_work + s.secret_record_transfers
                        + s.candidate_retirement_visits + w.materialized_chars
                        + w.buffer_appends + w.buffer_pops + w.provenance_visits)
        self.assertLessEqual(work[1], 2 * work[0] + 64)

    def test_all_search_instances_are_counted_even_across_recovery(self):
        from unittest.mock import patch
        module = self.module()
        original = module._Pipeline
        instances = []
        def tracked(*args, **kwargs):
            result = original(*args, **kwargs)
            instances.append(result)
            return result
        text = ("TOKENhttps://host/x " * 30 + "https://host/"
                + "x" * (PENDING_LIMIT_BYTES + 10) + " next ")
        with patch.object(module, "_Pipeline", side_effect=tracked):
            p = self.projector(("TOKENht",))
            for character in text:
                p.feed_text(character)
            p.finish()
        self.assertEqual(1, len(instances))
        self.assertEqual(len(text), sum(item._stats.chars for item in instances))
        self.assertEqual(len(instances), p.work_stats.recognition_instances)

    def test_legacy_commit_fixtures_preserve_continuation_without_batch_replay(self):
        module = self.module()
        fixtures = ("xhttps://h", "xgit", "xhttps://h trailing",
                    "custom%3A%2F%2Fhost", "x%6 a://h", "!https%3A//host",
                    "%68%74%74%70%73%253A//host", "%6Attps://h", "123https://h",
                    "%78%67%69%74", "https://host")
        for text in fixtures:
            for cut in range(len(text) + 1):
                expected = None
                for chunk in (1, 2, 3, 5, len(text)):
                    r = module.IncrementalRecognizer()
                    for start in range(0, len(text), chunk):
                        r.feed(text[start:start + chunk])
                    states = tuple(layer._cursor.state for layer in r._pipeline._layer)
                    pending = tuple(tuple(d._pending) for d in r._pipeline._transducers)
                    before = r.stats
                    r.consume(cut)
                    self.assertEqual(states, tuple(layer._cursor.state for layer in r._pipeline._layer))
                    self.assertEqual(pending, tuple(tuple(d._pending) for d in r._pipeline._transducers))
                    self.assertEqual(before.chars, r.stats.chars)
                    self.assertEqual(before.transitions, r.stats.transitions)
                    match = r.earliest_match()
                    outcome = (None if match is None else match.start,
                               r.retain_from(), r.prefix_state())
                    if expected is None:
                        expected = outcome
                    self.assertEqual(expected, outcome)
                    self.assertGreaterEqual(r.retain_from(), r.committed)
                    if match is not None:
                        self.assertGreaterEqual(match.start, r.committed)

    def test_legacy_partial_escape_and_separator_continuations(self):
        module = self.module()
        cases = (("!https%3", 1, "A//host", 1),
                 ("https%3", 5, "A//host", 0),
                 ("junk https:", 5, "//host", 5),
                 ("xhttps", 1, "://h", 1),
                 ("x%68%74%74%70%73", 1, "%3A//h", 1),
                 ("%", 1, "68://h https://h", 8))
        for first, cut, rest, expected in cases:
            for split in range(len(first) + 1):
                r = module.IncrementalRecognizer()
                r.feed(first[:split])
                r.feed(first[split:])
                r.consume(cut)
                before = r.stats.chars
                r.feed(rest)
                self.assertEqual(expected, r.earliest_match().start)
                self.assertEqual(before + len(rest), r.stats.chars)

    def test_secret_ending_in_raw_boundary_does_not_suppress_unrelated_facts(self):
        result = self.project("TOKEN https://host/public end", ("TOKEN ",))
        self.assertEqual("<secret><redacted> end", result[0])
        self.assertEqual(("host",), result[1])

    def test_legacy_long_run_copy_and_encoding_workloads_are_linear(self):
        for prefix, symbol, secrets, small, large in (
            ("", "x", (), 3000, 6000), ("", "z", (), 2000, 4000),
            ("", "z", ("secret",), 2000, 4000),
            ("https://example.com/", "a", (), 2000, 4000),
        ):
            work = []
            for length in (small, large):
                text = prefix + symbol * length
                p = self.projector(secrets)
                out = []
                for character in text:
                    out.extend(p.feed_text(character))
                out.extend(p.finish())
                s, w = p.recognition_stats, p.work_stats
                self.assertEqual(len(text), s.chars)
                self.assertEqual(len(text), w.buffer_appends)
                self.assertEqual(w.buffer_appends, w.buffer_pops)
                self.assertEqual(len(text.encode()), w.encoded_bytes)
                self.assertEqual("<redacted>" if prefix else text, "".join(out))
                work.append(s.layer_char_visits + s.prefix_checks + s.failure_traversals
                            + w.materialized_chars + w.buffer_appends + w.buffer_pops)
            self.assertLessEqual(work[1], 2 * work[0] + 64)
        for text in ("z" * (2 * PENDING_LIMIT_BYTES) + " end",
                     "z" * (PENDING_LIMIT_BYTES + 512)):
            p = self.projector()
            out = []
            for character in text:
                out.extend(p.feed_text(character))
                self.assertLessEqual(p.pending_size, PENDING_LIMIT_BYTES)
            out.extend(p.finish())
            self.assertEqual(text, "".join(out))
            self.assertEqual(len(text), p.work_stats.buffer_appends)
            self.assertEqual(len(text), p.work_stats.buffer_pops)
        p = self.projector()
        p.feed_text("https://example.com/" + "é" * 50)
        self.assertEqual(120, p.pending_size)
        self.assertEqual(70, p.work_stats.buffer_appends)
        self.assertEqual(120, p.work_stats.encoded_bytes)

    def test_legacy_regex_and_payload_map_helpers_are_not_production_fallbacks(self):
        from unittest.mock import patch
        import docker.versioning.diagnostic_projection as boundary
        for host_path in (False, True):
            with patch.object(boundary, "_find_url_start", side_effect=AssertionError("regex replay")), \
                 patch.object(boundary, "_decode_percent_layer", side_effect=AssertionError("source map")), \
                 patch.object(boundary, "_leftmost_secret_match", side_effect=AssertionError("secret replay")):
                result = self.project("pre h%74tps%3A%2F%2Fh.example/x end", host_path=host_path)
                self.assertEqual("pre h.example/x end" if host_path else "pre <redacted> end", result[0])

    def test_leftmost_longest_selection_survives_pending_and_overlapping_patterns(self):
        cases = (("abcd bc", ("abcd", "bc"), "<secret> <secret>"),
                 ("AAAA ", ("AAA", "AA"), "<secret>A "),
                 ("AAAA", ("AA",), "<secret><secret>"),
                 ("abc", ("abcd", "bc"), INCOMPLETE_TOKEN_MARKER))
        for text, secrets, expected in cases:
            self.assertEqual(expected, self.project(text, secrets)[0])
            for split in range(len(text) + 1):
                self.assertEqual(expected, self.project(text, secrets, (split,))[0])
            self.assertEqual(expected, self.project(text, secrets, bytewise=True)[0])

    def test_early_url_marker_with_earlier_unsettled_crossing_secret(self):
        for continuation, expected in (("st done", "<secret><redacted> done"),
                                       ("r done", "X<redacted> done")):
            with self.subTest(continuation=continuation):
                p = self.projector(("Xhttps://host",))
                self.assertEqual((), p.feed_text("Xhttps://ho"))
                self.assertEqual((), p.hostnames)
                cursor = p._recognizer
                head, tail = continuation.split(" ", 1)
                committed = "".join(p.feed_text(head))
                self.assertEqual(expected.removesuffix(" done"), committed)
                self.assertEqual((), p.hostnames)
                out = committed + "".join(p.feed_text(" " + tail)) + "".join(p.finish())
                self.assertEqual(expected, out)
                self.assertIs(cursor, p._recognizer)
                self.assertEqual(len("Xhttps://ho" + continuation), p.recognition_stats.chars)

    def test_clean_eof_resolves_earlier_crossing_secret_as_non_match(self):
        p = self.projector(("Xhttps://host",))
        self.assertEqual((), p.feed_text("Xhttps://ho"))
        before = p.recognition_stats.chars
        self.assertEqual("X<redacted>", "".join(p.finish()))
        self.assertEqual((), p.finish())
        self.assertEqual(before, p.recognition_stats.chars)
        self.assertEqual(0, p.pending_size)

    def test_crossing_secret_outcomes_are_partition_invariant(self):
        for text, expected in (("Xhttps://host done", "<secret><redacted> done"),
                               ("Xhttps://hor done", "X<redacted> done"),
                               ("Xhttps://ho", "X<redacted>")):
            baseline = self.project(text, ("Xhttps://host",))
            self.assertEqual(expected, baseline[0])
            for split in range(len(text) + 1):
                self.assertEqual(baseline, self.project(text, ("Xhttps://host",), (split,)))
            self.assertEqual(baseline, self.project(text, ("Xhttps://host",), bytewise=True))

    def test_crossing_secret_clean_eof_keeps_other_secret_coverage(self):
        # An internal completed secret must not force the earlier non-match
        # back into incomplete-prefix handling or manufacture another marker.
        for secrets in (("Xhttps://host", "ho"), ("Xhttps://host", "https")):
            p = self.projector(secrets)
            self.assertEqual((), p.feed_text("Xhttps://ho"))
            self.assertEqual("X<redacted>", "".join(p.finish()))
            self.assertEqual((), p.hostnames)
        p = self.projector(("Xhttps://host",))
        self.assertEqual((), p.feed_text("Xhttps:/"))
        self.assertEqual(INCOMPLETE_TOKEN_MARKER, "".join(p.finish()))
        p = self.projector(("Xhttps://host",))
        self.assertEqual((), p.feed_text("Xhttps"))
        self.assertEqual(INCOMPLETE_TOKEN_MARKER, "".join(p.finish()))

    def test_sdk_crossing_secret_delivery_uses_shared_continuation(self):
        from docker.versioning.npm_diagnostic_stream import NpmDiagnosticStream
        for mode in NetworkUrlDisplay:
            for continuation, expected in (("st done", "<secret><redacted> done"),
                                           ("r done", "X<redacted> done"),
                                           ("", "X<redacted>")):
                with self.subTest(mode=mode, continuation=continuation):
                    stream = NpmDiagnosticStream("stderr", ("Xhttps://host",),
                                                 network_url_display=mode)
                    initial = stream.feed_bytes(b"Xhttps://ho")
                    self.assertEqual("", "".join(c.text for c in initial))
                    owner = stream._projector._recognizer
                    chunks = list(stream.feed_bytes(continuation.encode()))
                    chunks.extend(stream.finish())
                    finalized = [c for c in chunks if c.finalized]
                    self.assertEqual([expected], [c.text for c in finalized])
                    self.assertEqual(expected, "".join(c.text for c in chunks if not c.finalized))
                    self.assertIs(owner, stream._projector._recognizer)
                    if mode is NetworkUrlDisplay.EXACT:
                        self.assertEqual("Xhttps://ho" + continuation, stream.tail())
                    elif mode is NetworkUrlDisplay.REDACTED:
                        self.assertEqual(expected, stream.tail())
                    else:
                        self.assertIs(stream._projector, stream._retained_projector)

    def test_crossing_secret_abort_and_overflow_remain_fail_closed(self):
        p = self.projector(("Xhttps://host",))
        self.assertEqual((), p.feed_text("Xhttps://ho"))
        self.assertEqual(INCOMPLETE_TOKEN_MARKER + "<redacted>", "".join(p.finish(abort=True)))
        self.assertEqual((), p.hostnames)
        self.assertEqual((), p.finish())
        # Keep a crossing secret unresolved until the whole-pending bound.
        secret = "Xhttps://" + "h" * (PENDING_LIMIT_BYTES + 50)
        p = self.projector((secret,))
        self.assertEqual((), p.feed_text("Xhttps://" + "h" * (PENDING_LIMIT_BYTES - len("Xhttps://"))))
        self.assertEqual(OVERSIZED_TOKEN_MARKER, "".join(p.feed_text("h" * 60)))
        self.assertEqual(" next", "".join(p.feed_text(" next")) + "".join(p.finish()))
        self.assertEqual((), p.hostnames)


if __name__ == "__main__":
    unittest.main()
