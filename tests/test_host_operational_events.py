"""Phase 1 operational host-event protocol contracts.

These tests bind the RED deliverables for Phase 1 of
``improve-host-build-observability``: a closed, immutable operational event
union that leaves the existing ``HostPhaseEvent`` lifecycle untouched, plus a
prompt-returning bounded non-blocking facade enqueue boundary that performs no
presentation work while ``GuardedHostEventSink`` serialization is held.
"""
from __future__ import annotations

import dataclasses
import datetime
import threading
import time
import unittest

from docker.versioning.host_progress import (
    GuardedHostEventSink,
    HostDiagnosticClassification,
    HostDiagnosticStream,
    HostEventEnqueueAdapter,
    HostEventMailbox,
    HostHeartbeatEvent,
    HostLastActivityKind,
    HostPhase,
    HostPhaseEvent,
    HostPhaseState,
    HostStep,
    HostStepEvent,
    HostStepState,
    HostStructuredDiagnostic,
    HostTransportProgressEvent,
    emit,
    guard_sink,
)


def _step_event() -> HostStepEvent:
    return HostStepEvent(
        HostPhase.LOCKED_ASSEMBLY, HostStep.NPM_EXECUTION, HostStepState.STARTED, True
    )


def _heartbeat(**overrides: object) -> HostHeartbeatEvent:
    values: dict[str, object] = {
        "phase": HostPhase.LOCKED_ASSEMBLY,
        "step": HostStep.NPM_EXECUTION,
        "elapsed_seconds": 5,
        "expects_diagnostic_stream": True,
    }
    values.update(overrides)
    return HostHeartbeatEvent(**values)  # type: ignore[arg-type]


def _structured_diagnostic(**overrides: object) -> HostStructuredDiagnostic:
    values: dict[str, object] = {
        "phase": HostPhase.RELEASE_ACQUISITION,
        "step": HostStep.ARTIFACT_ACQUISITION,
        "stream": HostDiagnosticStream.STDERR,
        "classification": HostDiagnosticClassification.RETRY,
        "text": "GET <url> failed",
        "hostnames": ("registry.npmjs.org",),
    }
    values.update(overrides)
    return HostStructuredDiagnostic(**values)  # type: ignore[arg-type]


class TestOperationalEventConstruction(unittest.TestCase):
    def test_step_event_requires_closed_members_and_declares_stream_applicability(self):
        event = _step_event()
        self.assertEqual(HostStep.NPM_EXECUTION, event.step)
        self.assertEqual(HostStepState.STARTED, event.state)
        self.assertIs(True, event.expects_diagnostic_stream)
        with self.assertRaises(TypeError):
            HostStepEvent("locked_assembly", HostStep.NPM_EXECUTION, HostStepState.STARTED, True)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            HostStepEvent(HostPhase.LOCKED_ASSEMBLY, "npm_execution", HostStepState.STARTED, True)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            HostStepEvent(HostPhase.LOCKED_ASSEMBLY, HostStep.NPM_EXECUTION, "started", True)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            HostStepEvent(HostPhase.LOCKED_ASSEMBLY, HostStep.NPM_EXECUTION, HostStepState.STARTED, "yes")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            HostStepEvent(HostPhase.LOCKED_ASSEMBLY, HostStep.NPM_EXECUTION, HostStepState.STARTED, 1)  # type: ignore[arg-type]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            event.state = HostStepState.FAILED  # type: ignore[misc]

    def test_closed_step_membership(self):
        self.assertEqual(
            {
                "artifact_acquisition",
                "release_acquisition",
                "lock_wait",
                "cache_lookup",
                "cache_reuse",
                "stale_stage_cleanup",
                "container_startup",
                "npm_execution",
                "validation",
                "publication",
                "docker_transition",
            },
            {step.value for step in HostStep},
        )

    def test_heartbeat_diagnostic_silence_requires_expected_stream(self):
        without_stream = _heartbeat(
            phase=HostPhase.RELEASE_ACQUISITION,
            step=HostStep.ARTIFACT_ACQUISITION,
            expects_diagnostic_stream=False,
        )
        self.assertIsNone(without_stream.diagnostic_silence_seconds)
        with self.assertRaises(TypeError):
            _heartbeat(
                phase=HostPhase.RELEASE_ACQUISITION,
                step=HostStep.ARTIFACT_ACQUISITION,
                elapsed_seconds=125,
                expects_diagnostic_stream=False,
                diagnostic_silence_seconds=120,
            )
        with_stream = _heartbeat(elapsed_seconds=125, diagnostic_silence_seconds=120)
        self.assertEqual(120, with_stream.diagnostic_silence_seconds)
        for negative_or_subsecond in (-1, 0.5, True, "120"):
            with self.subTest(silence=negative_or_subsecond), self.assertRaises(TypeError):
                _heartbeat(diagnostic_silence_seconds=negative_or_subsecond)

    def test_heartbeat_last_activity_jointly_absent_or_valid(self):
        absent = _heartbeat()
        self.assertIsNone(absent.last_activity_kind)
        self.assertIsNone(absent.last_activity_age_seconds)
        valid = _heartbeat(
            last_activity_kind=HostLastActivityKind.DIAGNOSTIC,
            last_activity_age_seconds=1,
        )
        self.assertEqual(HostLastActivityKind.DIAGNOSTIC, valid.last_activity_kind)
        self.assertEqual(1, valid.last_activity_age_seconds)
        with self.assertRaises(TypeError):
            _heartbeat(last_activity_kind=HostLastActivityKind.DIAGNOSTIC)
        with self.assertRaises(TypeError):
            _heartbeat(last_activity_age_seconds=3)
        with self.assertRaises(TypeError):
            _heartbeat(last_activity_kind="diagnostic", last_activity_age_seconds=3)
        for bad_age in (0, -1, 0.5, 1.25, True, "3"):
            with self.subTest(age=bad_age), self.assertRaises(TypeError):
                _heartbeat(
                    last_activity_kind=HostLastActivityKind.DIAGNOSTIC,
                    last_activity_age_seconds=bad_age,
                )
        with self.assertRaises(TypeError):
            _heartbeat(
                last_activity_kind=HostLastActivityKind.TRANSPORT_PROGRESS,
                last_activity_age_seconds=None,
            )

    def test_heartbeat_rejects_wall_clock_activity_timestamps(self):
        for timestamp in (
            datetime.datetime.now(),
            datetime.datetime.now(datetime.timezone.utc),
            time.time(),
            "2026-01-01T00:00:00Z",
        ):
            with self.subTest(timestamp=timestamp), self.assertRaises(TypeError):
                _heartbeat(
                    last_activity_kind=HostLastActivityKind.DIAGNOSTIC,
                    last_activity_age_seconds=timestamp,
                )

    def test_transport_progress_requires_cumulative_whole_bytes(self):
        event = HostTransportProgressEvent(
            HostPhase.RELEASE_ACQUISITION, HostStep.ARTIFACT_ACQUISITION, 4096
        )
        self.assertEqual(4096, event.received_bytes)
        for bad_bytes in (-1, 1.5, True, "4096"):
            with self.subTest(bytes=bad_bytes), self.assertRaises(TypeError):
                HostTransportProgressEvent(
                    HostPhase.RELEASE_ACQUISITION,
                    HostStep.ARTIFACT_ACQUISITION,
                    bad_bytes,  # type: ignore[arg-type]
                )

    def test_structured_diagnostic_carries_url_free_text_and_normalized_hosts(self):
        event = _structured_diagnostic()
        self.assertEqual("GET <url> failed", event.text)
        self.assertEqual(("registry.npmjs.org",), event.hostnames)
        self.assertEqual(HostDiagnosticStream.STDERR, event.stream)
        self.assertEqual(HostDiagnosticClassification.RETRY, event.classification)
        with self.assertRaises(TypeError):
            _structured_diagnostic(stream="stderr")
        with self.assertRaises(TypeError):
            _structured_diagnostic(classification="retry")
        with self.assertRaises(TypeError):
            _structured_diagnostic(hostnames=["registry.npmjs.org"])
        with self.assertRaises(TypeError):
            _structured_diagnostic(hostnames=("registry.npmjs.org", 5))

    def test_operational_events_carry_no_exception_or_formatting_objects(self):
        events = (
            _step_event(),
            HostTransportProgressEvent(
                HostPhase.RELEASE_ACQUISITION, HostStep.ARTIFACT_ACQUISITION, 1
            ),
            _heartbeat(),
            _structured_diagnostic(),
        )
        for event in events:
            with self.subTest(event=type(event).__name__):
                self.assertFalse(hasattr(event, "exception"))
                self.assertFalse(hasattr(event, "format"))
                for field in dataclasses.fields(event):
                    self.assertNotIsInstance(getattr(event, field.name), BaseException)


class TestLifecycleCompatibility(unittest.TestCase):
    def test_operational_events_neither_replace_nor_mutate_lifecycle_pair(self):
        started = HostPhaseEvent(HostPhase.LOCKED_ASSEMBLY, HostPhaseState.STARTED)
        failed = HostPhaseEvent(HostPhase.LOCKED_ASSEMBLY, HostPhaseState.FAILED)
        self.assertEqual(
            {"phase", "state"}, {field.name for field in dataclasses.fields(HostPhaseEvent)}
        )
        operational = (
            _step_event(),
            _heartbeat(),
            _structured_diagnostic(),
        )
        delivered: list[object] = []
        sink = GuardedHostEventSink(delivered.append)
        for event in (started, *operational, failed):
            sink(event)
        lifecycle = [event for event in delivered if isinstance(event, HostPhaseEvent)]
        self.assertEqual([HostPhaseState.STARTED, HostPhaseState.FAILED],
                         [event.state for event in lifecycle])
        self.assertEqual(HostPhase.LOCKED_ASSEMBLY, lifecycle[0].phase)
        self.assertIsNot(type(operational[0]), type(started))
        self.assertEqual(HostPhaseState.STARTED, started.state)
        self.assertEqual(HostPhaseState.FAILED, failed.state)


class _RecordingLock:
    """Instrumentable stand-in for the guarded serialization lock."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.depth = 0
        self.max_depth = 0

    def __enter__(self) -> "_RecordingLock":
        self._lock.acquire()
        self.depth += 1
        self.max_depth = max(self.max_depth, self.depth)
        return self

    def __exit__(self, *_exc: object) -> bool:
        self.depth -= 1
        self._lock.release()
        return False


class _ForbiddenPresentation:
    """Presentation machinery that must never run under guarded serialization."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def render(self, *_args: object) -> None:
        self.calls.append("render")

    def wait_barrier(self, *_args: object) -> None:
        self.calls.append("wait")

    def flush(self) -> None:
        self.calls.append("flush")

    def join(self, *_args: object) -> None:
        self.calls.append("join")

    def stop(self) -> None:
        self.calls.append("stop")


class TestFailureIsolatedSerializedDelivery(unittest.TestCase):
    def test_operational_and_lifecycle_events_share_serialized_delivery(self):
        delivered: list[object] = []
        guard_lock = _RecordingLock()
        guarded = GuardedHostEventSink(delivered.append, lock=guard_lock)
        events = [
            HostPhaseEvent(HostPhase.LOCKED_ASSEMBLY, HostPhaseState.STARTED),
            _step_event(),
            _heartbeat(),
            _structured_diagnostic(),
            HostPhaseEvent(HostPhase.LOCKED_ASSEMBLY, HostPhaseState.SUCCEEDED),
        ]
        for event in events:
            guarded(event)
        self.assertEqual(events, delivered)
        self.assertEqual(1, guard_lock.max_depth)

    def test_omitted_or_throwing_sink_never_affects_primary_operation(self):
        self.assertIsNone(guard_sink(None))
        emit(None, _step_event())

        attempts: list[object] = []

        def raising(event: object) -> None:
            attempts.append(event)
            raise RuntimeError("presentation failed")

        guarded = guard_sink(raising)
        assert guarded is not None
        guarded(HostPhaseEvent(HostPhase.LOCKED_ASSEMBLY, HostPhaseState.STARTED))
        guarded(_step_event())
        guarded(_heartbeat())
        self.assertEqual(1, len(attempts))

        def unguarded_raising(event: object) -> None:
            raise RuntimeError("presentation failed")

        emit(unguarded_raising, _step_event())

    def test_adapter_only_enqueues_without_render_wait_flush_or_join(self):
        presentation = _ForbiddenPresentation()
        mailbox = HostEventMailbox(capacity=8)
        adapter = HostEventEnqueueAdapter(mailbox)
        guard_lock = _RecordingLock()
        observed_depths: list[int] = []

        def instrumented(event: object) -> None:
            observed_depths.append(guard_lock.depth)
            adapter(event)  # type: ignore[arg-type]

        guarded = GuardedHostEventSink(instrumented, lock=guard_lock)
        flood = [_heartbeat() for _ in range(1000)]
        flood.append(HostPhaseEvent(HostPhase.LOCKED_ASSEMBLY, HostPhaseState.STARTED))
        flood.append(_structured_diagnostic())

        started_at = time.monotonic()
        for event in flood:
            guarded(event)
        elapsed = time.monotonic() - started_at

        self.assertEqual([], presentation.calls)
        self.assertTrue(observed_depths)
        self.assertTrue(all(depth == 1 for depth in observed_depths))
        self.assertEqual(1, guard_lock.max_depth)
        self.assertLess(elapsed, 2.0)
        self.assertLessEqual(len(mailbox), mailbox.capacity)
        self.assertGreater(mailbox.dropped, 0)

    def test_lock_contention_is_a_prompt_non_blocking_drop(self):
        """A held mailbox lock must not block a serialized producer callback."""
        presentation = _ForbiddenPresentation()
        mailbox = HostEventMailbox(capacity=8)
        adapter = HostEventEnqueueAdapter(mailbox)
        guard_lock = _RecordingLock()
        guarded = GuardedHostEventSink(adapter, lock=guard_lock)

        holding = threading.Event()
        release = threading.Event()

        def hold_mailbox_lock() -> None:
            with mailbox._lock:  # type: ignore[attr-defined]
                holding.set()
                release.wait(timeout=2.0)

        holder = threading.Thread(target=hold_mailbox_lock, daemon=True)
        holder.start()
        try:
            self.assertTrue(holding.wait(timeout=5.0), "holder must hold the mailbox lock")
            started_at = time.monotonic()
            guarded(_structured_diagnostic())
            elapsed = time.monotonic() - started_at
        finally:
            release.set()
            holder.join(timeout=5.0)

        self.assertFalse(holder.is_alive())
        self.assertEqual([], presentation.calls)
        self.assertLess(elapsed, 0.5)
        self.assertEqual(0, len(mailbox), "a contended event must not be admitted")
        self.assertEqual(0, mailbox.dropped, "contention is not counted as a capacity drop")

    def test_mailbox_rejects_nonpositive_capacity(self):
        for capacity in (0, -1, 1.5, True, "8"):
            with self.subTest(capacity=capacity), self.assertRaises((TypeError, ValueError)):
                HostEventMailbox(capacity=capacity)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
