"""Phase 1A (P1A) tests for the standalone Aho-Corasick streaming core.

These tests bind the RED obligations for the standalone, dependency-free
Aho-Corasick core introduced by ``linearize-diagnostic-url-detection`` as the
P1A prerequisite for literal secret matching (tasks 1A.1--1A.3):

* **registration and matching** -- empty pattern sets, non-empty registered
  patterns, duplicate/empty patterns, prefix and suffix patterns, overlapping
  and simultaneous matches, and Unicode Python-character coordinates, all
  through one immutable compiled trie shared by independent stream cursors and
  without introducing a public SDK surface (task 1A.1);
* **compiled-state immutability** -- transition maps are read-only and compiled
  attributes cannot be reassigned or deleted, with rejected mutation attempts
  proven not to alter matching for cursors already sharing the trie (tasks
  1A.1/1A.6);
* **continuation and counters** -- one-feed versus every-split versus
  single-character feeds, deterministic transition/failure/output counters,
  reset semantics that preserve lifetime totals, and cursor state that retains
  no source payload and depends only on recent characters (task 1A.2); and
* **differential corpus** -- a bounded corpus against a simple literal-match
  oracle, adversarial suffix-heavy patterns, every-match-exactly-once,
  shared-compilation stability, output-sensitive work and live cursor state
  independent of stream length, with no elapsed-time assertion (task 1A.3).

The core module is greenfield, so before it exists every test fails with an
explicit assertion naming the missing interface rather than a collection error.
The test names enumerate the P1A obligations.  This core performs literal
matching only: percent decoding, URL semantics, secret selection and projector
coordination are P2/P3 work and are deliberately **not** asserted here.
"""
from __future__ import annotations

import importlib
import random
import unittest

_MODULE_NAME = "docker.versioning.aho_corasick"

#: Fixed seed so the bounded differential corpus is fully deterministic.
_CORPUS_SEED = 20240613


def _load_core():
    try:
        return importlib.import_module(_MODULE_NAME)
    except ModuleNotFoundError:
        return None


def _oracle_occurrences(
    patterns: tuple[str, ...], text: str
) -> list[tuple[int, int, int]]:
    """Return every literal occurrence as ``(pattern_index, start, end)``.

    The oracle is deliberately the naive all-occurrences search: at every
    source position it checks every registered pattern, so overlapping and
    simultaneous matches are all reported.  It is used only by tests and is
    never imported by the production core.
    """
    matches: list[tuple[int, int, int]] = []
    for start in range(len(text)):
        for index, pattern in enumerate(patterns):
            if text.startswith(pattern, start):
                matches.append((index, start, start + len(pattern)))
    return matches


class _CoreCase(unittest.TestCase):
    """Base case that fails cleanly while the core module is absent."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.core_module = _load_core()

    def core(self):
        module = type(self).core_module
        if module is None or not hasattr(module, "AhoCorasick"):
            self.fail(
                f"{_MODULE_NAME} must define the P1A Aho-Corasick core"
            )
        return module

    def matcher(self, patterns):
        return self.core().AhoCorasick(patterns)

    @staticmethod
    def triplets(matches) -> list[tuple[int, int, int]]:
        return [
            (match.pattern_index, match.start, match.end)
            for match in matches
        ]

    def feed_splits(self, patterns, text: str, split: int):
        """Feed *text* through one cursor split at *split*.

        Returns ``(cursor, collected_triplets)`` where the triplets are every
        match emitted by either feed, in emission order.
        """
        cursor = self.matcher(patterns).cursor()
        collected = list(self.triplets(cursor.feed(text[:split])))
        collected.extend(self.triplets(cursor.feed(text[split:])))
        return cursor, collected


class RegistrationAndMatchTests(_CoreCase):
    """Task 1A.1 -- registration, coordinates and shared immutable trie."""

    def test_empty_pattern_set_registers_nothing_and_matches_nothing(self):
        matcher = self.matcher(())
        self.assertEqual((), matcher.patterns)
        self.assertEqual(1, matcher.node_count)
        cursor = matcher.cursor()
        self.assertEqual((), cursor.feed("anything at all"))
        self.assertEqual(0, cursor.counters.outputs)

    def test_empty_and_duplicate_patterns_are_ignored(self):
        matcher = self.matcher(["", "TOKEN", "", "TOKEN", "OTHER", "OTHER"])
        self.assertEqual(("TOKEN", "OTHER"), matcher.patterns)

    def test_patterns_preserve_first_occurrence_order(self):
        matcher = self.matcher(["zeta", "alpha", "zeta", "beta"])
        self.assertEqual(("zeta", "alpha", "beta"), matcher.patterns)

    def test_single_pattern_reports_inclusive_start_exclusive_end(self):
        matcher = self.matcher(["TOKEN"])
        matches = matcher.find_all("aTOKENb")
        self.assertEqual([(0, 1, 6)], self.triplets(matches))

    def test_documented_usage_example_matches_the_api(self):
        # Executes the module-docstring example verbatim so the documented API
        # cannot drift from behavior.
        matcher = self.matcher(["he", "she", "hers"])
        matches = matcher.find_all("ushers")
        observed = [
            (match.pattern_index, match.start, match.end)
            for match in matches
        ]
        self.assertEqual([(1, 1, 4), (0, 2, 4), (2, 2, 6)], observed)

    def test_prefix_patterns_report_both_ends(self):
        matcher = self.matcher(["ab", "abc"])
        matches = matcher.find_all("abc")
        self.assertEqual(
            sorted([(0, 0, 2), (1, 0, 3)]),
            sorted(self.triplets(matches)),
        )

    def test_suffix_patterns_report_overlapping_matches(self):
        matcher = self.matcher(["bcd", "cd", "d"])
        matches = matcher.find_all("bcd")
        self.assertEqual(
            sorted([(0, 0, 3), (1, 1, 3), (2, 2, 3)]),
            sorted(self.triplets(matches)),
        )

    def test_simultaneous_matches_at_one_end_are_all_reported(self):
        matcher = self.matcher(["aba", "ba", "a"])
        matches = matcher.find_all("ababa")
        self.assertEqual(
            sorted(
                [
                    (0, 0, 3),
                    (1, 1, 3),
                    (0, 2, 5),
                    (1, 3, 5),
                    (2, 0, 1),
                    (2, 2, 3),
                    (2, 4, 5),
                ]
            ),
            sorted(self.triplets(matches)),
        )

    def test_unicode_coordinates_are_python_character_offsets(self):
        matcher = self.matcher(["TOKEN"])
        text = "\u20acTOKEN"  # one euro sign then TOKEN
        matches = matcher.find_all(text)
        self.assertEqual([(0, 1, 6)], self.triplets(matches))
        match = matches[0]
        self.assertEqual("TOKEN", text[match.start : match.end])
        # A UTF-8 byte offset into the multibyte prefix would be larger and
        # would not slice the same characters.
        byte_start = len(text[: match.start].encode("utf-8"))
        self.assertGreater(byte_start, match.start)
        self.assertNotEqual("TOKEN", text[byte_start : byte_start + 5])

    def test_cursor_offset_supplies_absolute_coordinates(self):
        matcher = self.matcher(["TOKEN"])
        cursor = matcher.cursor(offset=10)
        matches = cursor.feed("xTOKEN")
        self.assertEqual([(0, 11, 16)], self.triplets(matches))
        self.assertEqual(16, cursor.offset)

    def test_independent_cursors_share_one_compiled_trie(self):
        matcher = self.matcher(["ab", "bc"])
        first = matcher.cursor()
        second = matcher.cursor()
        self.assertEqual([(0, 0, 2)], self.triplets(first.feed("ab")))
        self.assertEqual([(1, 0, 2)], self.triplets(second.feed("bc")))
        # Neither cursor mutated the shared compiled registration.
        self.assertEqual(("ab", "bc"), matcher.patterns)

    def test_compiled_registration_does_not_alias_caller_input(self):
        patterns = ["ab"]
        matcher = self.matcher(patterns)
        patterns.append("cd")
        patterns[0] = "zz"
        self.assertEqual(("ab",), matcher.patterns)

    def test_pattern_match_is_immutable(self):
        matcher = self.matcher(["TOKEN"])
        match = matcher.find_all("TOKEN")[0]
        with self.assertRaises((AttributeError, TypeError)):
            match.start = 99  # type: ignore[misc]

    def test_node_storage_is_bounded_by_registered_patterns(self):
        matcher = self.matcher(["ab", "abc", "bcd", "z", "abcd"])
        self.assertEqual(5, len(matcher.patterns))
        self.assertLessEqual(
            matcher.node_count,
            1 + sum(len(pattern) for pattern in matcher.patterns),
        )
        # Duplicate patterns do not add trie nodes.
        duplicate = self.matcher(["ab", "abc", "bcd", "z", "abcd", "ab"])
        self.assertEqual(matcher.node_count, duplicate.node_count)

    def test_module_is_not_a_public_sdk_surface(self):
        import docker
        import docker.versioning

        self.assertFalse(hasattr(docker.versioning, "AhoCorasick"))
        self.assertFalse(hasattr(docker, "AhoCorasick"))
        module = self.core()
        self.assertTrue(hasattr(module, "__all__"))


class CompiledStateImmutabilityTests(_CoreCase):
    """Tasks 1A.1/1A.6 -- compiled state is immutable in fact, not convention.

    Copying caller input and never mutating registration during a search are
    *isolation* properties; on their own they do not prove that the compiled
    trie itself cannot be rewritten.  These tests drive mutation through the
    published objects and attributes and require an exception, then prove via
    shared cursors that matching is unchanged.
    """

    _COMPILED_ATTRIBUTES = (
        "_patterns",
        "_goto",
        "_fail",
        "_output",
        "_output_link",
    )

    def test_transition_maps_are_read_only(self):
        matcher = self.matcher(["ab", "bc"])
        node = matcher._goto[0]
        with self.assertRaises(TypeError):
            node["z"] = 0  # type: ignore[index]
        with self.assertRaises(TypeError):
            del node["a"]  # type: ignore[attr-defined]
        # Replacing a tuple element is refused by the tuple itself.
        with self.assertRaises(TypeError):
            matcher._goto[0] = {}  # type: ignore[index]
        self.assertIn("a", matcher._goto[0])

    def test_replacing_compiled_attributes_raises(self):
        matcher = self.matcher(["ab"])
        for name in self._COMPILED_ATTRIBUTES:
            with self.subTest(name=name):
                with self.assertRaises(AttributeError):
                    setattr(matcher, name, object())

    def test_deleting_compiled_attributes_raises(self):
        matcher = self.matcher(["ab"])
        for name in self._COMPILED_ATTRIBUTES:
            with self.subTest(name=name):
                with self.assertRaises(AttributeError):
                    delattr(matcher, name)

    def test_compiled_automaton_has_no_attribute_injection(self):
        matcher = self.matcher(["ab"])
        self.assertFalse(hasattr(matcher, "__dict__"))
        with self.assertRaises(AttributeError):
            matcher.injected = object()  # type: ignore[attr-defined]

    def test_rejected_mutation_cannot_alter_shared_cursor_matching(self):
        patterns = ["he", "she", "hers"]
        matcher = self.matcher(patterns)
        expected = sorted(self.triplets(matcher.find_all("ushers")))
        first = matcher.cursor()
        second = matcher.cursor()
        # Every attempt must be rejected, never silently applied; an accepted
        # transition rewrite would make the two shared cursors diverge.
        with self.assertRaises(TypeError):
            matcher._goto[0]["h"] = 0  # type: ignore[index]
        for name in self._COMPILED_ATTRIBUTES:
            with self.assertRaises(AttributeError):
                setattr(matcher, name, object())
        for name in self._COMPILED_ATTRIBUTES:
            with self.assertRaises(AttributeError):
                delattr(matcher, name)
        self.assertEqual(expected, sorted(self.triplets(first.feed("ushers"))))
        self.assertEqual(expected, sorted(self.triplets(second.feed("ushers"))))
        self.assertEqual(
            expected,
            sorted(
                self.triplets(
                    self.matcher(["he", "she", "hers"]).find_all("ushers")
                )
            ),
        )
        self.assertEqual(("he", "she", "hers"), matcher.patterns)


class ContinuationAndCountersTests(_CoreCase):
    """Task 1A.2 -- partition invariance, deterministic counters, ownership."""

    _CASES = (
        (("ab", "bab", "bc", "bca", "c", "caa"), "abccab"),
        (("TOKEN",), "pre TOKEN mid TOKEN end"),
        (("aa", "a"), "aaaa"),
        (("abc", "bc", "c"), "zabc"),
        (("xy", "xyz"), "wxyz"),
        ((), "no patterns here"),
    )

    def test_one_feed_every_split_and_single_character_agree(self):
        for patterns, text in self._CASES:
            with self.subTest(patterns=patterns, text=text):
                expected = sorted(
                    self.triplets(self.matcher(patterns).find_all(text))
                )
                for split in range(len(text) + 1):
                    _, collected = self.feed_splits(patterns, text, split)
                    self.assertEqual(expected, sorted(collected))
                single = self.matcher(patterns).cursor()
                collected = []
                for character in text:
                    collected.extend(self.triplets(single.feed(character)))
                self.assertEqual(expected, sorted(collected))

    def test_feed_and_advance_agree(self):
        patterns = ("ab", "bab")
        text = "abbab"
        chunked = self.matcher(patterns).cursor()
        chunked_matches = sorted(self.triplets(chunked.feed(text)))
        advanced = self.matcher(patterns).cursor()
        advanced_matches: list[tuple[int, int, int]] = []
        for character in text:
            advanced_matches.extend(self.triplets(advanced.advance(character)))
        self.assertEqual(chunked_matches, sorted(advanced_matches))
        self.assertEqual(chunked.state, advanced.state)
        self.assertEqual(chunked.counters, advanced.counters)

    def test_transitions_count_every_input_symbol(self):
        cursor = self.matcher(["ab"]).cursor()
        cursor.feed("abcde")
        self.assertEqual(5, cursor.counters.transitions)
        self.assertEqual(5, cursor.offset)

    def test_outputs_count_every_emitted_match(self):
        patterns = ("a", "aa")
        cursor = self.matcher(patterns).cursor()
        matches = cursor.feed("aaaa")
        self.assertEqual(len(matches), cursor.counters.outputs)
        self.assertEqual(7, cursor.counters.outputs)

    def test_failures_are_amortized_within_input_length(self):
        patterns = ("aab", "ab", "b")
        cursor = self.matcher(patterns).cursor()
        cursor.feed("a" * 500 + "b" * 500)
        counters = cursor.counters
        self.assertEqual(counters.transitions, 1000)
        self.assertLessEqual(counters.failures, counters.transitions)
        self.assertEqual(
            len(self.triplets(self.matcher(patterns).find_all("a" * 500 + "b" * 500))),
            counters.outputs,
        )

    def test_counters_are_deterministic_and_partition_independent(self):
        patterns = ("ab", "bab", "bc")
        text = "abbab" * 5
        reference = self.matcher(patterns).cursor()
        reference.feed(text)
        for split in (0, 1, 3, len(text) // 2, len(text) - 1):
            with self.subTest(split=split):
                cursor, _collected = self.feed_splits(patterns, text, split)
                self.assertEqual(reference.counters, cursor.counters)
                self.assertEqual(reference.state, cursor.state)

    def test_reset_clears_state_and_preserves_lifetime_totals(self):
        cursor = self.matcher(["ab"]).cursor()
        cursor.feed("ab")
        cursor.reset()
        self.assertEqual(0, cursor.state)
        before = cursor.counters
        cursor.feed("ab")
        self.assertEqual(before.transitions + 2, cursor.counters.transitions)
        self.assertEqual(2, cursor.counters.outputs)
        # The absolute stream coordinate is preserved across a reset so a
        # later commit does not renumber source positions.
        self.assertEqual(4, cursor.offset)

    def test_reset_does_not_replay_input(self):
        cursor = self.matcher(["ab"]).cursor()
        cursor.feed("ab")
        before = cursor.counters.outputs
        cursor.reset()
        self.assertEqual((), cursor.feed(""))
        self.assertEqual(before, cursor.counters.outputs)

    def test_cursor_state_depends_only_on_recent_characters(self):
        matcher = self.matcher(["ab"])
        long_cursor = matcher.cursor()
        long_cursor.feed("x" * 2000 + "ab")
        short_cursor = matcher.cursor()
        short_cursor.feed("ab")
        self.assertEqual(short_cursor.state, long_cursor.state)

    def test_cursor_retains_no_source_payload(self):
        cursor = self.matcher(["TOKEN"]).cursor()
        cursor.feed("x" * 500 + "TOKEN" + "y" * 500)
        for slot in type(cursor).__slots__:
            if slot == "_automaton":
                continue
            with self.subTest(slot=slot):
                self.assertNotIsInstance(getattr(cursor, slot), str)


class DifferentialCorpusTests(_CoreCase):
    """Task 1A.3 -- bounded differential corpus and output-sensitive work."""

    def _random_case(self, rng: random.Random):
        pattern_count = rng.randint(0, 5)
        patterns: list[str] = []
        for _ in range(pattern_count):
            length = rng.randint(1, 4)
            patterns.append("".join(rng.choice("abc") for _ in range(length)))
        text = "".join(rng.choice("abcd") for _ in range(rng.randint(0, 30)))
        return patterns, text

    def _assert_matches_oracle(self, patterns, text):
        matcher = self.matcher(patterns)
        registered = matcher.patterns
        expected = sorted(_oracle_occurrences(registered, text))
        actual = sorted(self.triplets(matcher.find_all(text)))
        self.assertEqual(expected, actual)
        return actual

    def test_bounded_differential_corpus_matches_literal_oracle(self):
        rng = random.Random(_CORPUS_SEED)
        for _ in range(80):
            patterns, text = self._random_case(rng)
            with self.subTest(patterns=patterns, text=text):
                self._assert_matches_oracle(patterns, text)

    def test_differential_corpus_agrees_across_every_chunk_split(self):
        rng = random.Random(_CORPUS_SEED + 1)
        for _ in range(25):
            patterns, text = self._random_case(rng)
            matcher = self.matcher(patterns)
            expected = sorted(self.triplets(matcher.find_all(text)))
            for split in range(len(text) + 1):
                _, collected = self.feed_splits(patterns, text, split)
                self.assertEqual(expected, sorted(collected))

    def test_adversarial_suffix_heavy_patterns_match_oracle(self):
        patterns = tuple("a" * length for length in range(1, 9))
        text = "a" * 24
        actual = self._assert_matches_oracle(patterns, text)
        self.assertEqual(
            sum(24 - length + 1 for length in range(1, 9)), len(actual)
        )

    def test_every_match_reported_exactly_once(self):
        patterns = ("a", "aa", "aaa", "b", "ab")
        text = "aaabaa"
        actual = self._assert_matches_oracle(patterns, text)
        self.assertEqual(len(actual), len(set(actual)))

    def test_shared_compilation_is_stable_across_repeated_runs(self):
        matcher = self.matcher(("ab", "bc", "abc", "c"))
        first = sorted(self.triplets(matcher.find_all("abcabc")))
        for _ in range(5):
            self.assertEqual(
                first, sorted(self.triplets(matcher.find_all("abcabc")))
            )
        self.assertEqual(("ab", "bc", "abc", "c"), matcher.patterns)

    def test_output_sensitive_work_counts_matches_separately(self):
        no_match = self.matcher(["z"]).cursor()
        no_match.feed("a" * 400)
        self.assertEqual(0, no_match.counters.outputs)
        self.assertEqual(400, no_match.counters.transitions)

        match_heavy = self.matcher(["a", "aa"]).cursor()
        matches = match_heavy.feed("a" * 400)
        self.assertEqual(len(matches), match_heavy.counters.outputs)
        self.assertEqual(799, match_heavy.counters.outputs)
        self.assertEqual(400, match_heavy.counters.transitions)
        self.assertLessEqual(
            match_heavy.counters.failures, match_heavy.counters.transitions
        )

    def test_live_cursor_state_is_independent_of_stream_length(self):
        matcher = self.matcher(["ab"])
        short = matcher.cursor()
        short.feed("ab")
        long = matcher.cursor()
        long.feed("ab" * 1000)
        self.assertEqual(short.state, long.state)
        # The long cursor's work is a function of the input it consumed, never
        # of retained history.
        self.assertEqual(2, short.counters.transitions)
        self.assertEqual(2000, long.counters.transitions)
        self.assertLess(long.state, matcher.node_count)


if __name__ == "__main__":
    unittest.main()
