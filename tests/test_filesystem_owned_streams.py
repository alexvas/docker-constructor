"""Phase 6B task 6B.2/6B.6 — owned stream lifecycle.

``docker.filesystem.streams.OwnedStream`` adopts one already-open fd-owning
file object and becomes its sole release authority.  These contracts pin the
bounded surface (labeled adoption, borrowed live stream access, terminal-state
inspection, ``close()``, and context-manager cleanup) plus the synchronized
terminal claim, outside-the-lock invocation, at-most-once release, shared
cleanup precedence, and failure-safe invalid-input handling the design
requires.  A ``fileno()`` accessor, generic proxy API, detach/retry, or
process-aware factory must not exist.
"""

from __future__ import annotations

import inspect
import io
import threading
import time
import unittest
from unittest import mock

from docker.filesystem.streams import OwnedStream

_POK = inspect.Parameter.POSITIONAL_OR_KEYWORD
_KO = inspect.Parameter.KEYWORD_ONLY
_EMPTY = inspect.Parameter.empty


class _AggregateError(Exception):
    """A primary following the generic secondary-diagnostic protocol."""

    def __init__(self, message: str = "aggregate") -> None:
        super().__init__(message)
        self.secondary: list[BaseException] = []

    def add_secondary(self, exc: BaseException) -> None:
        if not any(item is exc for item in self.secondary):
            self.secondary.append(exc)


class _Cancellation(BaseException):
    """A cancellation-style, non-I/O process-control exception."""


class _FakeStream:
    """A descriptor-less stream double recording every close attempt."""

    def __init__(self, *, close_error: BaseException | None = None) -> None:
        self.close_calls = 0
        self.close_error = close_error
        self.closed = False
        self.before_close = None

    def read(self, size: int = -1) -> str:  # pragma: no cover - unused
        return ""

    def close(self) -> None:
        self.close_calls += 1
        if self.before_close is not None:
            self.before_close()
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


def _owned(stream=None, label: str = "stdout"):
    return OwnedStream(stream if stream is not None else _FakeStream(), label=label)


class OwnedStreamConstructionTests(unittest.TestCase):
    def test_accepts_a_closeable_stream_and_non_empty_label(self) -> None:
        stream = _FakeStream()
        owned = OwnedStream(stream, label="stdout")
        self.assertIs(owned.stream, stream)
        self.assertEqual(owned.label, "stdout")
        self.assertFalse(owned.released)
        owned.close()

    def test_construction_performs_no_operation(self) -> None:
        stream = _FakeStream()
        OwnedStream(stream, label="stderr")
        self.assertEqual(stream.close_calls, 0)

    def test_rejects_stream_without_callable_close_before_adoption(self) -> None:
        # A candidate with no callable close has no release authority and is
        # left untouched; there is nothing an owner could release.
        for bad in (None, object(), 7, "text"):
            with self.subTest(stream=bad):
                with self.assertRaises(TypeError):
                    OwnedStream(bad, label="stdout")

    def test_rejects_empty_or_non_string_label_before_transfer(self) -> None:
        for bad in ("", None, 7, object()):
            with self.subTest(label=bad):
                stream = _FakeStream()
                before = vars(stream).copy()
                with self.assertRaises(ValueError):
                    OwnedStream(stream, label=bad)
                self.assertEqual(vars(stream), before)

    def test_rejection_never_invokes_a_failing_close(self) -> None:
        for failure in (OSError("close"), RuntimeError("defect"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                stream = _FakeStream(close_error=failure)
                before = vars(stream).copy()
                with self.assertRaises(ValueError) as ctx:
                    OwnedStream(stream, label="")
                self.assertEqual(vars(stream), before)
                self.assertEqual(
                    getattr(ctx.exception, "_transaction_secondary", []), []
                )

    def test_interface_rejection_leaves_candidate_untouched(self) -> None:
        stream = _FakeStream()
        stream.close = None
        before = vars(stream).copy()
        with self.assertRaises(TypeError):
            OwnedStream(stream, label="stdout")
        self.assertEqual(vars(stream), before)

    def test_validation_precedes_lock_preparation(self) -> None:
        for stream, label, error in (
            (_FakeStream(), "", ValueError),
            (object(), "stdout", TypeError),
        ):
            with self.subTest(error=error):
                with mock.patch("docker.filesystem.streams.threading.Lock") as lock:
                    with self.assertRaises(error):
                        OwnedStream(stream, label=label)
                    lock.assert_not_called()

    def test_lock_preparation_failure_leaves_caller_stream_untouched(self) -> None:
        for failure in (OSError("lock"), RuntimeError("lock"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                stream = _FakeStream()
                before = vars(stream).copy()
                with mock.patch(
                    "docker.filesystem.streams.threading.Lock", side_effect=failure
                ):
                    with self.assertRaises(type(failure)) as ctx:
                        OwnedStream(stream, label="stdout")
                self.assertIs(ctx.exception, failure)
                self.assertEqual(vars(stream), before)

    def test_label_keyword_is_required(self) -> None:
        with self.assertRaises(TypeError):
            OwnedStream(_FakeStream())  # type: ignore[call-arg]


class OwnedStreamStateTests(unittest.TestCase):
    def test_stream_access_is_borrowed_and_live(self) -> None:
        stream = _FakeStream()
        owned = OwnedStream(stream, label="stdout")
        self.assertIs(owned.stream, stream)
        owned.close()

    def test_label_stays_accessible_after_release(self) -> None:
        owned = _owned()
        owned.close()
        self.assertEqual(owned.label, "stdout")

    def test_stream_access_fails_after_release(self) -> None:
        owned = _owned()
        owned.close()
        with self.assertRaises(ValueError):
            _ = owned.stream

    def test_close_marks_release_before_invoking_close(self) -> None:
        stream = _FakeStream()
        owned = OwnedStream(stream, label="stdout")
        observed: list[bool] = []
        stream.before_close = lambda: observed.append(owned.released)
        owned.close()
        self.assertEqual(observed, [True])
        self.assertTrue(owned.released)

    def test_close_is_not_retried_after_success(self) -> None:
        stream = _FakeStream()
        owned = OwnedStream(stream, label="stdout")
        owned.close()
        owned.close()
        self.assertEqual(stream.close_calls, 1)

    def test_close_is_not_retried_after_failure(self) -> None:
        failure = OSError("injected close")
        stream = _FakeStream(close_error=failure)
        owned = OwnedStream(stream, label="stdout")
        with self.assertRaises(OSError) as ctx:
            owned.close()
        self.assertIs(ctx.exception, failure)
        self.assertTrue(owned.released)
        owned.close()
        self.assertEqual(stream.close_calls, 1)

    def test_reentrant_close_does_not_deadlock_or_close_twice(self) -> None:
        stream = _FakeStream()
        owned = OwnedStream(stream, label="stdout")
        reentrant_errors: list[BaseException] = []

        def reenter() -> None:
            try:
                owned.close()
            except BaseException as exc:  # pragma: no cover - fail path
                reentrant_errors.append(exc)

        stream.before_close = reenter
        started = time.monotonic()
        owned.close()
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(reentrant_errors, [])
        self.assertEqual(stream.close_calls, 1)

    def test_concurrent_close_invokes_underlying_close_exactly_once(self) -> None:
        stream = _FakeStream()
        owned = OwnedStream(stream, label="stdout")
        entered = threading.Event()
        release = threading.Event()

        def blocking_close() -> None:
            entered.set()
            release.wait(2.0)

        stream.before_close = blocking_close
        errors: list[BaseException] = []

        def closer() -> None:
            try:
                owned.close()
            except BaseException as exc:  # pragma: no cover - fail path
                errors.append(exc)

        threads = [threading.Thread(target=closer) for _ in range(2)]
        for thread in threads:
            thread.start()
        # The winner is inside the underlying close while the loser must have
        # already observed the terminal claim and returned.
        self.assertTrue(entered.wait(1.0))
        release.set()
        for thread in threads:
            thread.join(2.0)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(stream.close_calls, 1)


class OwnedStreamCleanupPrecedenceTests(unittest.TestCase):
    def test_close_failure_without_active_primary_is_sole(self) -> None:
        failure = OSError("injected close")
        owned = _owned(_FakeStream(close_error=failure))
        with self.assertRaises(OSError) as ctx:
            with owned:
                pass
        self.assertIs(ctx.exception, failure)
        self.assertTrue(owned.released)

    def test_close_failure_is_secondary_to_an_active_primary(self) -> None:
        failure = OSError("injected close")
        owned = _owned(_FakeStream(close_error=failure))
        primary = _AggregateError("body failed")
        with self.assertRaises(_AggregateError) as ctx:
            with owned:
                raise primary
        self.assertIs(ctx.exception, primary)
        self.assertEqual(primary.secondary, [failure])
        self.assertTrue(owned.released)

    def test_process_control_close_failure_is_authoritative(self) -> None:
        for interruption in (KeyboardInterrupt(), _Cancellation()):
            with self.subTest(interruption=type(interruption).__name__):
                owned = _owned(_FakeStream(close_error=interruption))
                primary = _AggregateError("body failed")
                with self.assertRaises(type(interruption)) as ctx:
                    with owned:
                        raise primary
                self.assertIs(ctx.exception, interruption)
                self.assertTrue(owned.released)

    def test_context_manager_enter_returns_the_owner(self) -> None:
        owned = _owned()
        with owned as entered:
            self.assertIs(entered, owned)
        self.assertTrue(owned.released)

    def test_entering_a_released_owner_is_rejected(self) -> None:
        owned = _owned()
        owned.close()
        with self.assertRaises(ValueError):
            with owned:
                pass


class OwnedStreamSurfaceTests(unittest.TestCase):
    """Task 6B.6 — the bounded surface exposes no raw-fd or proxy API."""

    def test_public_surface_is_exact(self) -> None:
        self.assertEqual(
            {name for name in vars(OwnedStream) if not name.startswith("_")},
            {"stream", "label", "released", "close"},
        )

    def test_constructor_signature_is_exact(self) -> None:
        expected = [
            ("self", _POK, _EMPTY),
            ("stream", _POK, _EMPTY),
            ("label", _KO, _EMPTY),
        ]
        self.assertEqual(
            [
                (p.name, p.kind, p.default)
                for p in inspect.signature(OwnedStream.__init__).parameters.values()
            ],
            expected,
        )

    def test_lifecycle_method_signatures_are_exact(self) -> None:
        for method in (
            OwnedStream.close,
            OwnedStream.__enter__,
        ):
            with self.subTest(method=method.__name__):
                self.assertEqual(
                    [
                        (p.name, p.kind, p.default)
                        for p in inspect.signature(method).parameters.values()
                    ],
                    [("self", _POK, _EMPTY)],
                )
        self.assertEqual(
            [
                (p.name, p.kind, p.default)
                for p in inspect.signature(OwnedStream.__exit__).parameters.values()
            ],
            [
                ("self", _POK, _EMPTY),
                ("exc_type", _POK, _EMPTY),
                ("exc", _POK, _EMPTY),
                ("tb", _POK, _EMPTY),
            ],
        )

    def test_properties_are_read_only_inspections(self) -> None:
        for name in ("stream", "label", "released"):
            with self.subTest(property=name):
                descriptor = getattr(OwnedStream, name)
                self.assertIsInstance(descriptor, property)
                self.assertIsNotNone(descriptor.fget)
                self.assertIsNone(descriptor.fset)

    def test_no_raw_fd_access_detach_or_proxy_api(self) -> None:
        forbidden = (
            "fileno",
            "fd",
            "detach",
            "read",
            "write",
            "flush",
            "release",
        )
        for name in forbidden:
            with self.subTest(name=name):
                self.assertFalse(hasattr(OwnedStream, name))

    def test_no_varargs_or_kwargs_on_constructor(self) -> None:
        kinds = {
            parameter.kind
            for parameter in inspect.signature(OwnedStream.__init__).parameters.values()
        }
        self.assertNotIn(inspect.Parameter.VAR_KEYWORD, kinds)
        self.assertNotIn(inspect.Parameter.VAR_POSITIONAL, kinds)


class OwnedStreamRealFileTests(unittest.TestCase):
    def test_adopts_and_closes_a_real_buffered_stream_once(self) -> None:
        stream = io.BytesIO(b"data")
        owned = OwnedStream(stream, label="real")
        self.assertEqual(owned.stream.read(), b"data")
        owned.close()
        self.assertTrue(stream.closed)
        owned.close()
        self.assertTrue(stream.closed)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
