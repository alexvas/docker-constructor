"""Phase 9B task 9B.1 - contract tests for ``carry_secondary_diagnostics``.

The narrow diagnostic-carry operation exists so a domain adapter can replace a
transaction/capability wrapper with its raw operational cause while preserving
only the secondary diagnostics the wrapper already retained.  It accepts no
caller-supplied iterable, consumes nothing from the source, and changes no
chaining, cause, context, or unrelated notes.
"""
from __future__ import annotations

import inspect
import unittest

from docker.transactions.errors import (
    _MAX_SECONDARY_NOTES,
    TransactionError,
    carry_secondary_diagnostics,
)


class _NoAttachment(Exception):
    """An exception that refuses attribute storage but supports notes."""

    def __setattr__(self, name: str, value: object) -> None:
        if name == "__notes__":
            object.__setattr__(self, name, value)
            return
        raise AttributeError("attachment unavailable")


def _secondary_of(exc: BaseException) -> list[BaseException]:
    return getattr(exc, "_transaction_secondary", [])


def _wrapper_with(*secondaries: BaseException) -> TransactionError:
    wrapper = TransactionError("stage", "wrapper")
    for exc in secondaries:
        wrapper.add_secondary(exc)
    return wrapper


class CarrySecondaryDiagnosticsTests(unittest.TestCase):
    def test_transaction_error_target_receives_source_order(self) -> None:
        first = OSError("first")
        second = OSError("second")
        source = _wrapper_with(first, second)
        target = TransactionError("stage", "target")
        carry_secondary_diagnostics(target, source)
        self.assertEqual(target.secondary, [first, second])

    def test_generic_target_receives_retained_objects_in_source_order(self) -> None:
        first = OSError("first")
        second = OSError("second")
        source = _wrapper_with(first, second)
        target = ValueError("target")
        carry_secondary_diagnostics(target, source)
        self.assertEqual(_secondary_of(target), [first, second])
        self.assertIs(_secondary_of(target)[0], first)
        self.assertIs(_secondary_of(target)[1], second)

    def test_generic_source_slot_diagnostics_are_carried(self) -> None:
        first = OSError("first")
        second = OSError("second")
        source = ValueError("source")
        source._transaction_secondary = [first, second]  # type: ignore[attr-defined]
        target = TransactionError("stage", "target")
        carry_secondary_diagnostics(target, source)
        self.assertEqual(target.secondary, [first, second])

    def test_no_diagnostics_is_a_noop(self) -> None:
        source = TransactionError("stage", "wrapper")
        target = ValueError("target")
        self.assertIsNone(carry_secondary_diagnostics(target, source))
        self.assertFalse(hasattr(target, "_transaction_secondary"))
        self.assertFalse(hasattr(target, "__notes__"))

    def test_repeated_carry_deduplicates_identity_for_object_targets(self) -> None:
        first = OSError("first")
        second = OSError("second")
        source = _wrapper_with(first, second)
        target = ValueError("target")
        carry_secondary_diagnostics(target, source)
        carry_secondary_diagnostics(target, source)
        carry_secondary_diagnostics(target, source)
        self.assertEqual(_secondary_of(target), [first, second])

    def test_transaction_error_target_deduplicates_across_repeated_carries(self) -> None:
        duplicate = OSError("cleanup")
        source = _wrapper_with(duplicate, duplicate)
        target = TransactionError("stage", "target")
        carry_secondary_diagnostics(target, source)
        carry_secondary_diagnostics(target, source)
        self.assertEqual(target.secondary, [duplicate])

    def test_fallback_target_observes_diagnostics_as_bounded_text(self) -> None:
        first = OSError("first cleanup")
        second = OSError("second cleanup")
        source = _wrapper_with(first, second)
        target = _NoAttachment("target")
        carry_secondary_diagnostics(target, source)
        self.assertFalse(hasattr(target, "_transaction_secondary"))
        notes = list(target.__notes__)
        self.assertEqual(len(notes), 2)
        self.assertIn("secondary cleanup failure", notes[0])
        self.assertIn("first cleanup", notes[0])
        self.assertIn("second cleanup", notes[1])
        self.assertTrue(all(len(note) <= 240 for note in notes))

    def test_fallback_repeated_carry_stays_bounded_without_identity_guarantee(self) -> None:
        source = _wrapper_with(OSError("cleanup"))
        target = _NoAttachment("target")
        for _ in range(_MAX_SECONDARY_NOTES * 3):
            carry_secondary_diagnostics(target, source)
        notes = list(target.__notes__)
        self.assertLessEqual(len(notes), _MAX_SECONDARY_NOTES)

    def test_target_identity_is_preserved(self) -> None:
        source = _wrapper_with(OSError("cleanup"))
        target = ValueError("target")
        result = carry_secondary_diagnostics(target, source)
        self.assertIsNone(result)
        self.assertIsInstance(target, ValueError)
        self.assertEqual(str(target), "target")

    def test_source_is_not_mutated(self) -> None:
        first = OSError("first")
        second = OSError("second")
        source = _wrapper_with(first, second)
        target = ValueError("target")
        carry_secondary_diagnostics(target, source)
        self.assertEqual(source.secondary, [first, second])
        self.assertIs(source.secondary[0], first)

    def test_source_itself_is_not_carried(self) -> None:
        source = _wrapper_with(OSError("cleanup"))
        target = ValueError("target")
        carry_secondary_diagnostics(target, source)
        self.assertNotIn(source, _secondary_of(target))

    def test_cause_and_context_are_not_carried(self) -> None:
        cause = KeyError("cause")
        context = RuntimeError("context")
        source = _wrapper_with(OSError("cleanup"))
        source.__cause__ = cause
        source.__context__ = context
        target = ValueError("target")
        carry_secondary_diagnostics(target, source)
        self.assertIsNone(target.__cause__)
        self.assertIsNone(target.__context__)
        self.assertNotIn(cause, _secondary_of(target))
        self.assertNotIn(context, _secondary_of(target))

    def test_unrelated_source_notes_are_not_carried(self) -> None:
        source = _wrapper_with(OSError("cleanup"))
        source.add_note("unrelated source note")
        target = ValueError("target")
        target.add_note("target own note")
        carry_secondary_diagnostics(target, source)
        notes = list(target.__notes__)
        self.assertIn("target own note", notes)
        self.assertNotIn("unrelated source note", notes)

    def test_call_is_bounded_to_target_and_source(self) -> None:
        source = _wrapper_with(OSError("cleanup"))
        target = ValueError("target")
        with self.assertRaises(TypeError):
            carry_secondary_diagnostics(target, source, [OSError("injected")])  # type: ignore[misc]
        signature = inspect.signature(carry_secondary_diagnostics)
        self.assertEqual(list(signature.parameters), ["target", "source"])

    def test_operation_grants_no_filesystem_or_mapping_authority(self) -> None:
        import docker.transactions.errors as errors

        public = {
            name for name, value in vars(errors).items()
            if not name.startswith("_") and callable(value)
            and getattr(value, "__module__", None) == errors.__name__
        }
        self.assertIn("carry_secondary_diagnostics", public)
        source = errors.__dict__["carry_secondary_diagnostics"]
        self.assertNotIn("path", inspect.getsource(source))
        self.assertNotIn("unlink", inspect.getsource(source))
        self.assertNotIn("remove", inspect.getsource(source))


if __name__ == "__main__":
    unittest.main()
