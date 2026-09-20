"""Phase 7 exact-repeat and conservative numeric-variant coalescing contracts.

These tests bind the RED deliverables for tasks 7.4-7.5 of
``improve-host-build-observability``: the pure strict-numeric-token matcher,
the mode-independent group state transitions, interactive mutable-value
replacement, and `lines`-mode separation of every changed numeric value.  The
facade worker, timers, mailbox, and renderer belong to later phases and must
not appear here.
"""
from __future__ import annotations

import unittest

from docker.versioning.diagnostic_identity import (
    AdmissionDecision,
    DiagnosticCoalescer,
    DiagnosticComparison,
    DiagnosticDisposition,
    PresentationMode,
    compare_diagnostics,
    format_repetition,
    identity_for,
    numeric_template_match,
    strict_numeric_tokens,
)
from docker.versioning.diagnostic_projection import sanitize_diagnostic_text
from docker.versioning.host_progress import (
    HostDiagnosticClassification,
    HostDiagnosticStream,
    HostPhase,
    HostStep,
    HostStructuredDiagnostic,
)


def _diagnostic(text: str, **overrides: object) -> HostStructuredDiagnostic:
    values: dict[str, object] = {
        "phase": HostPhase.LOCKED_ASSEMBLY,
        "step": HostStep.NPM_EXECUTION,
        "stream": HostDiagnosticStream.STDOUT,
        "classification": HostDiagnosticClassification.STATUS,
        "text": text,
    }
    values.update(overrides)
    return HostStructuredDiagnostic(**values)  # type: ignore[arg-type]


class TestStrictNumericTokens(unittest.TestCase):
    def test_valid_tokens_are_extracted(self):
        self.assertEqual(("1",), strict_numeric_tokens("progress 1"))
        self.assertEqual(("1.2.3",), strict_numeric_tokens("version 1.2.3"))
        self.assertEqual(("42", "10.20.30"), strict_numeric_tokens("a 42 b 10.20.30 c"))
        self.assertEqual(("1.2",), strict_numeric_tokens("(1.2)"))

    def test_signed_incomplete_malformed_and_embedded_tokens_are_rejected(self):
        for text in (
            "value -1",
            "value +1",
            "value -1.2",
            "value 1.",
            "value .1",
            "value 1..2",
            "item1",
            "value1",
            "1value",
            "1_2",
            "1-2",
            "1+2",
        ):
            with self.subTest(text=text):
                self.assertEqual((), strict_numeric_tokens(text))

    def test_matching_accepts_one_changed_token(self):
        self.assertTrue(numeric_template_match("progress 1", "progress 2"))
        self.assertTrue(numeric_template_match("version 1.2.3", "version 1.2.4"))
        self.assertTrue(numeric_template_match("progress 5", "progress 2"))

    def test_matching_rejects_invalid_numeric_sequences(self):
        self.assertFalse(numeric_template_match("value -1", "value -2"))
        self.assertFalse(numeric_template_match("value +1", "value +2"))
        self.assertFalse(numeric_template_match("value 1.", "value 2."))
        self.assertFalse(numeric_template_match("value .1", "value .2"))
        self.assertFalse(numeric_template_match("value 1..2", "value 1..3"))
        self.assertFalse(numeric_template_match("item1", "item2"))

    def test_matching_rejects_zero_or_multiple_changed_tokens_and_template_drift(self):
        self.assertFalse(numeric_template_match("progress 1", "progress 1"))
        self.assertFalse(numeric_template_match("1 and 2", "3 and 4"))
        self.assertFalse(numeric_template_match("progress 1", "progress 2 3"))
        self.assertFalse(numeric_template_match("progress 1", "other 2"))
        self.assertFalse(numeric_template_match("no digits", "still none"))


class TestDiagnosticComparison(unittest.TestCase):
    def test_numeric_variant_requires_matching_metadata(self):
        left = identity_for(_diagnostic("progress 1"))
        right = identity_for(_diagnostic("progress 2"))
        self.assertEqual(
            DiagnosticComparison.NUMERIC_VARIANT, compare_diagnostics(left, right)
        )

    def test_url_fingerprint_mismatch_is_not_a_numeric_variant(self):
        left = identity_for(_diagnostic("GET <redacted> 1", url_fingerprints=("a" * 64,)))
        right = identity_for(_diagnostic("GET <redacted> 2", url_fingerprints=("b" * 64,)))
        self.assertEqual(DiagnosticComparison.DIFFERENT, compare_diagnostics(left, right))

    def test_identity_mismatch_is_not_a_numeric_variant(self):
        left = identity_for(_diagnostic("progress 1"))
        right = identity_for(
            _diagnostic("progress 2", classification=HostDiagnosticClassification.ERROR)
        )
        self.assertEqual(DiagnosticComparison.DIFFERENT, compare_diagnostics(left, right))

    def test_format_repetition_uses_the_canonical_suffix(self):
        self.assertEqual("line", format_repetition("line", 1))
        self.assertEqual("line (repeated 2 times)", format_repetition("line", 2))
        self.assertEqual("line (repeated 9 times)", format_repetition("line", 9))


class TestInteractiveGroupState(unittest.TestCase):
    def setUp(self):
        self.coalescer = DiagnosticCoalescer(PresentationMode.INTERACTIVE)

    def test_first_occurrence_populates_the_slot_without_a_durable_write(self):
        decision = self.coalescer.admit(_diagnostic("added 1 package"))
        self.assertIs(DiagnosticDisposition.NEW_GROUP, decision.disposition)
        self.assertEqual("added 1 package", decision.slot_text)
        self.assertIsNone(decision.durable_text)
        self.assertIsNone(decision.finalized_text)
        self.assertEqual(1, decision.admitted_count)

    def test_exact_repeats_increment_the_slot_count(self):
        self.coalescer.admit(_diagnostic("added 1 package"))
        decision = self.coalescer.admit(_diagnostic("added 1 package"))
        self.assertIs(DiagnosticDisposition.EXACT_REPEAT, decision.disposition)
        self.assertEqual("added 1 package (repeated 2 times)", decision.slot_text)
        self.assertIsNone(decision.durable_text)
        self.assertEqual(2, decision.admitted_count)

    def test_numeric_variant_replaces_the_slot_and_resets_the_count(self):
        self.coalescer.admit(_diagnostic("progress 1"))
        variant = self.coalescer.admit(_diagnostic("progress 2"))
        self.assertIs(DiagnosticDisposition.NUMERIC_VARIANT, variant.disposition)
        self.assertEqual("progress 2", variant.slot_text)
        self.assertNotIn("repeated", variant.slot_text or "")
        self.assertEqual(1, variant.admitted_count)
        repeat = self.coalescer.admit(_diagnostic("progress 2"))
        self.assertEqual("progress 2 (repeated 2 times)", repeat.slot_text)
        self.assertEqual(2, repeat.admitted_count)

    def test_numeric_variant_finalizes_only_the_latest_value(self):
        self.coalescer.admit(_diagnostic("progress 1"))
        self.coalescer.admit(_diagnostic("progress 2"))
        self.coalescer.admit(_diagnostic("progress 3"))
        self.assertEqual("progress 3", self.coalescer.finalize())
        self.assertIsNone(self.coalescer.group)

    def test_mismatch_finalizes_the_group_and_starts_a_new_one(self):
        self.coalescer.admit(_diagnostic("progress 1"))
        self.coalescer.admit(_diagnostic("progress 2"))
        decision = self.coalescer.admit(_diagnostic("unrelated line"))
        self.assertEqual("progress 2", decision.finalized_text)
        self.assertEqual("unrelated line", decision.slot_text)
        self.assertIs(DiagnosticDisposition.NEW_GROUP, decision.disposition)

    def test_omission_notice_or_terminal_finalizes_the_count_then_restarts(self):
        self.coalescer.admit(_diagnostic("warning"))
        self.coalescer.admit(_diagnostic("warning"))
        self.assertEqual("warning (repeated 2 times)", self.coalescer.finalize())
        restarted = self.coalescer.admit(_diagnostic("warning"))
        self.assertIs(DiagnosticDisposition.NEW_GROUP, restarted.disposition)
        self.assertEqual(1, restarted.admitted_count)
        self.assertEqual("warning", restarted.slot_text)


class TestLinesGroupState(unittest.TestCase):
    def setUp(self):
        self.coalescer = DiagnosticCoalescer(PresentationMode.LINES)

    def test_first_occurrence_is_a_durable_line(self):
        decision = self.coalescer.admit(_diagnostic("added 1 package"))
        self.assertEqual("added 1 package", decision.durable_text)
        self.assertIsNone(decision.slot_text)

    def test_every_changed_numeric_value_is_a_separate_durable_line(self):
        self.coalescer.admit(_diagnostic("progress 1"))
        second = self.coalescer.admit(_diagnostic("progress 2"))
        third = self.coalescer.admit(_diagnostic("progress 3"))
        self.assertEqual("progress 2", second.durable_text)
        self.assertEqual("progress 3", third.durable_text)
        self.assertIsNone(second.slot_text)
        self.assertIsNone(third.slot_text)

    def test_exact_repeat_window_still_summarizes_at_finalization(self):
        self.coalescer.admit(_diagnostic("retrying"))
        repeat = self.coalescer.admit(_diagnostic("retrying"))
        self.assertIsNone(repeat.durable_text)
        self.assertEqual(2, repeat.admitted_count)
        self.assertEqual("retrying (repeated 2 times)", self.coalescer.finalize())

    def test_single_occurrence_group_has_no_summary(self):
        self.coalescer.admit(_diagnostic("only once"))
        self.assertIsNone(self.coalescer.finalize())

    def test_group_change_flushes_the_prior_summary_before_the_new_line(self):
        self.coalescer.admit(_diagnostic("retrying"))
        self.coalescer.admit(_diagnostic("retrying"))
        decision = self.coalescer.admit(_diagnostic("done"))
        self.assertEqual("retrying (repeated 2 times)", decision.finalized_text)
        self.assertEqual("done", decision.durable_text)


class TestRetainedTailsRemainUngrouped(unittest.TestCase):
    def test_sanitized_tail_keeps_each_occurrence_without_grouping(self):
        for text in ("progress 1", "progress 2", "progress 2"):
            retained, _ = sanitize_diagnostic_text(text)
            self.assertEqual(text, retained)
            self.assertNotIn("repeated", retained)

    def test_coalescing_module_owns_no_worker_timer_or_renderer(self):
        coalescer = DiagnosticCoalescer(PresentationMode.INTERACTIVE)
        for attribute in ("start", "stop", "join", "render", "worker", "timer"):
            self.assertFalse(hasattr(coalescer, attribute), attribute)

    def test_admission_decision_is_immutable(self):
        decision = DiagnosticCoalescer(PresentationMode.INTERACTIVE).admit(
            _diagnostic("line")
        )
        self.assertIsInstance(decision, AdmissionDecision)
        with self.assertRaises(Exception):
            decision.slot_text = "changed"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
