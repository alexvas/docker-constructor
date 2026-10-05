"""Phase 9A tasks 9A.1-9A.3 — primary-preserving cleanup accumulator.

These tests pin the full truth table of :class:`CleanupFailures`, its
mandatory keyword-only ordinary policy, and the strengthened
:func:`attach_secondary` object/notes diagnostics.
"""
from __future__ import annotations

import unittest

from docker.transactions.cleanup import CleanupFailures
from docker.transactions.errors import (
    _MAX_SECONDARY_NOTES,
    TransactionError,
    attach_secondary,
)


class _Cancellation(BaseException):
    """A cancellation-style, non-I/O process-control exception."""


class _NoAttachment(Exception):
    """An exception that refuses attribute storage but supports notes."""

    def __setattr__(self, name: str, value: object) -> None:
        if name == "__notes__":
            object.__setattr__(self, name, value)
            return
        raise AttributeError("attachment unavailable")


def _raiser(exc: BaseException):
    def action() -> None:
        raise exc

    return action


def _secondary_of(exc: BaseException) -> list[BaseException]:
    return getattr(exc, "_transaction_secondary", [])


class CleanupFailuresTruthTableTests(unittest.TestCase):
    def test_no_failure_returns_none_and_runs_action(self) -> None:
        calls: list[str] = []
        failures = CleanupFailures(None)
        failures.run(lambda: calls.append("only"), ordinary=(OSError,))
        self.assertIsNone(failures.complete())
        self.assertEqual(calls, ["only"])

    def test_original_exception_primary_stays_authoritative(self) -> None:
        primary = ValueError("primary")
        failures = CleanupFailures(primary)
        first = OSError("first cleanup")
        second = OSError("second cleanup")
        failures.run(_raiser(first), ordinary=(OSError,))
        failures.run(lambda: None, ordinary=(OSError,))
        failures.run(_raiser(second), ordinary=(OSError,))
        self.assertIsNone(failures.complete())
        self.assertEqual(_secondary_of(primary), [first, second])

    def test_original_non_exception_primary_precedes_later_interruptions(self) -> None:
        primary = _Cancellation("original interruption")
        failures = CleanupFailures(primary)
        later = _Cancellation("later interruption")
        defect = RuntimeError("unexpected defect")
        ordinary = OSError("ordinary cleanup")
        failures.run(_raiser(ordinary), ordinary=(OSError,))
        failures.run(_raiser(defect), ordinary=(OSError,))
        failures.run(_raiser(later), ordinary=(OSError,))
        with self.assertRaises(_Cancellation) as ctx:
            failures.complete()
        self.assertIs(ctx.exception, primary)
        self.assertEqual(_secondary_of(primary), [later, defect, ordinary])

    def test_single_ordinary_without_primary_is_returned(self) -> None:
        failures = CleanupFailures(None)
        ordinary = OSError("only cleanup")
        failures.run(_raiser(ordinary), ordinary=(OSError,))
        self.assertIs(failures.complete(), ordinary)

    def test_multiple_ordinary_without_primary_returns_first_and_attaches_rest(self) -> None:
        failures = CleanupFailures(None)
        first = OSError("one")
        second = OSError("two")
        third = OSError("three")
        failures.run(_raiser(first), ordinary=(OSError,))
        failures.run(_raiser(second), ordinary=(OSError,))
        failures.run(_raiser(third), ordinary=(OSError,))
        result = failures.complete()
        self.assertIs(result, first)
        self.assertEqual(_secondary_of(first), [second, third])

    def test_single_unexpected_defect_is_raised_unchanged(self) -> None:
        failures = CleanupFailures(None)
        defect = RuntimeError("programmer defect")
        failures.run(_raiser(defect), ordinary=(OSError,))
        with self.assertRaises(RuntimeError) as ctx:
            failures.complete()
        self.assertIs(ctx.exception, defect)

    def test_multiple_unexpected_defects_raise_first_and_attach_rest(self) -> None:
        failures = CleanupFailures(None)
        first = RuntimeError("one")
        second = ValueError("two")
        failures.run(_raiser(first), ordinary=(OSError,))
        failures.run(_raiser(second), ordinary=(OSError,))
        with self.assertRaises(RuntimeError) as ctx:
            failures.complete()
        self.assertIs(ctx.exception, first)
        self.assertEqual(_secondary_of(first), [second])

    def test_unexpected_defect_displaces_exception_primary_as_secondary(self) -> None:
        primary = ValueError("primary")
        failures = CleanupFailures(primary)
        defect = RuntimeError("defect")
        ordinary = OSError("ordinary")
        failures.run(_raiser(ordinary), ordinary=(OSError,))
        failures.run(_raiser(defect), ordinary=(OSError,))
        with self.assertRaises(RuntimeError) as ctx:
            failures.complete()
        self.assertIs(ctx.exception, defect)
        self.assertEqual(_secondary_of(defect), [primary, ordinary])

    def test_single_cleanup_interruption_is_raised_unchanged(self) -> None:
        failures = CleanupFailures(None)
        interruption = KeyboardInterrupt("cleanup interrupted")
        failures.run(_raiser(interruption), ordinary=(OSError,))
        with self.assertRaises(KeyboardInterrupt) as ctx:
            failures.complete()
        self.assertIs(ctx.exception, interruption)

    def test_multiple_cleanup_interruptions_raise_first_and_attach_rest(self) -> None:
        failures = CleanupFailures(None)
        first = KeyboardInterrupt("first")
        second = _Cancellation("second")
        failures.run(_raiser(first), ordinary=(OSError,))
        failures.run(_raiser(second), ordinary=(OSError,))
        with self.assertRaises(KeyboardInterrupt) as ctx:
            failures.complete()
        self.assertIs(ctx.exception, first)
        self.assertEqual(_secondary_of(first), [second])

    def test_cleanup_interruption_displaces_exception_primary(self) -> None:
        primary = ValueError("primary")
        failures = CleanupFailures(primary)
        defect = RuntimeError("defect")
        ordinary = OSError("ordinary")
        interruption = KeyboardInterrupt("cleanup interrupted")
        failures.run(_raiser(ordinary), ordinary=(OSError,))
        failures.run(_raiser(defect), ordinary=(OSError,))
        failures.run(_raiser(interruption), ordinary=(OSError,))
        with self.assertRaises(KeyboardInterrupt) as ctx:
            failures.complete()
        self.assertIs(ctx.exception, interruption)
        self.assertEqual(_secondary_of(interruption), [defect, primary, ordinary])

    def test_interruption_precedes_unexpected_and_ordinary(self) -> None:
        failures = CleanupFailures(None)
        ordinary = OSError("ordinary")
        defect = RuntimeError("defect")
        interruption = KeyboardInterrupt("interruption")
        failures.run(_raiser(ordinary), ordinary=(OSError,))
        failures.run(_raiser(defect), ordinary=(OSError,))
        failures.run(_raiser(interruption), ordinary=(OSError,))
        with self.assertRaises(KeyboardInterrupt) as ctx:
            failures.complete()
        self.assertIs(ctx.exception, interruption)
        self.assertEqual(_secondary_of(interruption), [defect, ordinary])

    def test_original_non_exception_primary_authoritative_over_everything(self) -> None:
        primary = KeyboardInterrupt("original")
        failures = CleanupFailures(primary)
        ordinary = OSError("ordinary")
        defect = RuntimeError("defect")
        later = _Cancellation("later")
        failures.run(_raiser(ordinary), ordinary=(OSError,))
        failures.run(_raiser(defect), ordinary=(OSError,))
        failures.run(_raiser(later), ordinary=(OSError,))
        with self.assertRaises(KeyboardInterrupt) as ctx:
            failures.complete()
        self.assertIs(ctx.exception, primary)
        self.assertEqual(_secondary_of(primary), [later, defect, ordinary])

    def test_every_action_runs_exactly_once_through_failures(self) -> None:
        calls: list[str] = []
        failures = CleanupFailures(ValueError("primary"))

        def record(tag: str, exc: BaseException | None = None):
            def action() -> None:
                calls.append(tag)
                if exc is not None:
                    raise exc

            return action

        failures.run(record("a", OSError("a")), ordinary=(OSError,))
        failures.run(record("b"), ordinary=(OSError,))
        failures.run(record("c", OSError("c")), ordinary=(OSError,))
        failures.run(record("d", OSError("d")), ordinary=(OSError,))
        cf = failures
        self.assertIsNone(cf.complete())
        self.assertEqual(calls, ["a", "b", "c", "d"])

    def test_transaction_error_primary_receives_secondary_list(self) -> None:
        primary = TransactionError("stage", "primary")
        first = OSError("one")
        second = OSError("two")
        failures = CleanupFailures(primary)
        failures.run(_raiser(first), ordinary=(OSError,))
        failures.run(_raiser(second), ordinary=(OSError,))
        self.assertIsNone(failures.complete())
        self.assertEqual(primary.secondary, [first, second])


class CleanupFailuresApiContractTests(unittest.TestCase):
    def test_ordinary_is_keyword_only(self) -> None:
        failures = CleanupFailures(None)
        with self.assertRaises(TypeError):
            failures.run(lambda: None, (OSError,))  # type: ignore[misc]

    def test_non_tuple_ordinary_is_rejected(self) -> None:
        failures = CleanupFailures(None)
        with self.assertRaises(TypeError):
            failures.run(lambda: None, ordinary=OSError)  # type: ignore[arg-type]

    def test_non_exception_ordinary_is_rejected(self) -> None:
        failures = CleanupFailures(None)
        with self.assertRaises(TypeError):
            failures.run(lambda: None, ordinary=(object,))  # type: ignore[arg-type]

    def test_empty_ordinary_policy_makes_exceptions_unexpected(self) -> None:
        failures = CleanupFailures(None)
        defect = OSError("treated as unexpected")
        failures.run(_raiser(defect), ordinary=())
        with self.assertRaises(OSError) as ctx:
            failures.complete()
        self.assertIs(ctx.exception, defect)

    def test_action_return_value_grants_no_authority(self) -> None:
        failures = CleanupFailures(None)
        result = failures.run(lambda: "not authority", ordinary=(OSError,))
        self.assertIsNone(result)
        self.assertIsNone(failures.complete())

    def test_run_after_completion_is_rejected(self) -> None:
        failures = CleanupFailures(None)
        failures.complete()
        with self.assertRaises(RuntimeError):
            failures.run(lambda: None, ordinary=(OSError,))

    def test_second_complete_is_rejected_without_duplicating(self) -> None:
        failures = CleanupFailures(None)
        ordinary = OSError("only")
        failures.run(_raiser(ordinary), ordinary=(OSError,))
        self.assertIs(failures.complete(), ordinary)
        with self.assertRaises(RuntimeError):
            failures.complete()
        self.assertEqual(_secondary_of(ordinary), [])


class AttachSecondaryTests(unittest.TestCase):
    def test_duplicate_identity_is_not_attached_twice(self) -> None:
        primary = ValueError("primary")
        duplicate = OSError("cleanup")
        attach_secondary(primary, [duplicate])
        attach_secondary(primary, [duplicate])
        self.assertEqual(_secondary_of(primary), [duplicate])

    def test_distinct_objects_with_equal_values_are_both_retained(self) -> None:
        primary = ValueError("primary")
        first = OSError("same")
        second = OSError("same")
        attach_secondary(primary, [first, second])
        self.assertEqual(_secondary_of(primary), [first, second])

    def test_transaction_error_deduplicates_by_identity(self) -> None:
        primary = TransactionError("stage", "primary")
        duplicate = OSError("cleanup")
        attach_secondary(primary, [duplicate, duplicate])
        attach_secondary(primary, [duplicate])
        self.assertEqual(primary.secondary, [duplicate])

    def test_add_note_fallback_is_observable_and_single_identity(self) -> None:
        primary = _NoAttachment("primary")
        first = OSError("first cleanup")
        second = OSError("second cleanup")
        attach_secondary(primary, [first, second])
        self.assertFalse(hasattr(primary, "_transaction_secondary"))
        notes = list(primary.__notes__)
        self.assertEqual(len(notes), 2)
        self.assertIn("secondary cleanup failure", notes[0])
        self.assertIn("first cleanup", notes[0])
        self.assertIn("second cleanup", notes[1])

    def test_add_note_fallback_does_not_replace_authoritative_identity(self) -> None:
        primary = _NoAttachment("primary")
        attach_secondary(primary, [OSError("cleanup")])
        self.assertIsInstance(primary, _NoAttachment)
        self.assertEqual(str(primary), "primary")

    def test_note_text_is_bounded(self) -> None:
        primary = _NoAttachment("primary")
        huge = OSError("x" * 5000)
        attach_secondary(primary, [huge])
        note = primary.__notes__[0]
        self.assertLessEqual(len(note), 240)

    def test_add_note_fallback_is_bounded_across_repeated_attachments(self) -> None:
        primary = _NoAttachment("primary")
        # Far more attachments than the cap; every one is a distinct object.
        for index in range(_MAX_SECONDARY_NOTES * 4):
            attach_secondary(primary, [OSError(f"cleanup {index}")])
        generated = [
            note
            for note in primary.__notes__
            if "secondary cleanup failure" in note
        ]
        self.assertEqual(len(generated), _MAX_SECONDARY_NOTES)

    def test_add_note_fallback_preserves_preexisting_notes(self) -> None:
        primary = _NoAttachment("primary")
        primary.add_note("unrelated diagnostic")
        for index in range(_MAX_SECONDARY_NOTES * 4):
            attach_secondary(primary, [OSError(f"cleanup {index}")])
        notes = list(primary.__notes__)
        # The unrelated note is neither removed nor overwritten and counts
        # toward the total budget.
        self.assertEqual(notes[0], "unrelated diagnostic")
        generated = [
            note
            for note in notes
            if "secondary cleanup failure" in note
        ]
        self.assertEqual(len(generated), _MAX_SECONDARY_NOTES - 1)


if __name__ == "__main__":
    unittest.main()
