"""Phase 3 host activity coordinator contracts.

These tests bind the RED deliverables for Phase 3 of
``improve-host-build-observability``: one orchestration-owned activity monitor
with an injected monotonic clock and waiter that

* emits an activity-independent heartbeat cadence beginning exactly 3 seconds
  after operation start and thereafter scheduled one second apart from the
  previous *scheduled* deadline, so sink or callback latency cannot stretch
  start-to-start intervals,
* exposes a separate applicability-gated diagnostic-silence duration exactly
  from 120 seconds and resets only that clock on stdout/stderr,
* records the latest ``diagnostic`` or ``transport_progress`` activity kind and
  its whole monotonic age without a wall-clock timestamp,
* includes remaining time only when the wrapped operation already owns a fixed
  deadline, and
* atomically marks terminal, rejects later activity, raises its own stop
  signal, confirms bounded heartbeat production has stopped inside one shared
  shutdown budget, and then delivers exactly one step-terminal event on every
  supported success, failure, timeout, cancellation, and waiter-failure path.

Shutdown plumbing failures are contained: no coordinator failure may escape or
replace the wrapped operation's result, and no terminal event is delivered
before production is confirmed stopped.

Heartbeat facts are wording-neutral by construction: the closed field set
contains only closed members, whole-second monotonic integers, and booleans, so
no fact can claim process or network inactivity.
"""
from __future__ import annotations

import dataclasses
import threading
import unittest
from unittest.mock import patch

from docker.versioning.activity_monitor import (
    DIAGNOSTIC_SILENCE_SECONDS,
    FIRST_HEARTBEAT_SECONDS,
    HEARTBEAT_INTERVAL_SECONDS,
    HEARTBEAT_JOIN_TIMEOUT_SECONDS,
    HeartbeatWaiter,
    HostActivityMonitor,
    _next_heartbeat_deadline,
)
from docker.versioning.host_progress import (
    HostHeartbeatEvent,
    HostLastActivityKind,
    HostPhase,
    HostStep,
    HostStepEvent,
    HostStepState,
)


class _FakeClock:
    """Injectable monotonic clock advanced only by the test."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def advance_to(self, when: float) -> None:
        if when > self.now:
            self.now = when


class _ToggleFailingClock(_FakeClock):
    """Fake clock that can fail after monitor construction."""

    def __init__(self, now: float = 0.0) -> None:
        super().__init__(now)
        self.failing = False

    def __call__(self) -> float:
        if self.failing:
            raise RuntimeError("clock failed")
        return super().__call__()


class _ManualWaiter:
    """``threading.Event``-compatible waiter driven by the injected clock.

    ``wait`` returns ``False`` only once the injected clock reaches that wait's
    absolute deadline and ``True`` once the monitor requests a stop, so tests
    advance simulated time deterministically without real sleeps.
    """

    def __init__(self, clock: _FakeClock) -> None:
        self._clock = clock
        self._condition = threading.Condition()
        self.timeouts: list[float] = []
        self.deadlines: list[float] = []
        self._stopped = False

    def wait(self, timeout: float) -> bool:
        with self._condition:
            timeout = float(timeout)
            deadline = self._clock() + timeout
            self.timeouts.append(timeout)
            self.deadlines.append(deadline)
            self._condition.notify_all()
            while not self._stopped:
                if self._clock() >= deadline:
                    return False
                self._condition.wait(timeout=5.0)
            return True

    def set(self) -> None:
        with self._condition:
            self._stopped = True
            self._condition.notify_all()

    def wait_for_deadline(self, count: int, timeout: float = 5.0) -> float:
        with self._condition:
            while len(self.deadlines) < count:
                if not self._condition.wait(timeout=timeout):
                    raise AssertionError(
                        f"expected at least {count} waits, observed "
                        f"{len(self.deadlines)}"
                    )
            return self.deadlines[count - 1]

    def release(self) -> None:
        with self._condition:
            self._condition.notify_all()


class _RecordingSink:
    """Ordered event recorder that can simulate a throwing or latent sink."""

    def __init__(
        self,
        *,
        raises: bool = False,
        clock: _FakeClock | None = None,
        latency: float = 0.0,
    ) -> None:
        self._condition = threading.Condition()
        self._events: list[object] = []
        self._raises = raises
        self._clock = clock
        self._latency = float(latency)
        self.monitor: HostActivityMonitor | None = None
        # Whether heartbeat production was still alive when the step terminal
        # event was delivered.
        self.thread_alive_at_terminal: bool | None = None

    def __call__(self, event: object) -> None:
        with self._condition:
            if (
                isinstance(event, HostStepEvent)
                and event.state is not HostStepState.STARTED
                and self.monitor is not None
            ):
                self.thread_alive_at_terminal = self.monitor.is_alive
            self._events.append(event)
            if (
                isinstance(event, HostHeartbeatEvent)
                and self._clock is not None
                and self._latency
            ):
                # Simulate presentation latency: accepting a heartbeat consumes
                # simulated time, which must not move the heartbeat schedule.
                self._clock.advance(self._latency)
            self._condition.notify_all()
        if self._raises:
            raise RuntimeError("presentation failed")

    def events(self) -> list[object]:
        with self._condition:
            return list(self._events)

    def heartbeats(self) -> list[HostHeartbeatEvent]:
        return [
            event
            for event in self.events()
            if isinstance(event, HostHeartbeatEvent)
        ]

    def step_events(self) -> list[HostStepEvent]:
        return [
            event
            for event in self.events()
            if isinstance(event, HostStepEvent)
        ]

    def terminal_events(self) -> list[HostStepEvent]:
        return [
            event
            for event in self.step_events()
            if event.state is not HostStepState.STARTED
        ]

    def wait_for_heartbeats(
        self, count: int, timeout: float = 5.0
    ) -> list[HostHeartbeatEvent]:
        with self._condition:
            while True:
                beats = [
                    event
                    for event in self._events
                    if isinstance(event, HostHeartbeatEvent)
                ]
                if len(beats) >= count:
                    return beats
                if not self._condition.wait(timeout=timeout):
                    raise AssertionError(
                        f"expected {count} heartbeats, observed {len(beats)}"
                    )


class _TickingWaiter:
    """Time-simulating waiter whose ``set()`` never wakes the producer.

    Each ``wait`` advances the injected clock by the requested timeout and
    returns ``False``, so simulated time passes without a real sleep. It still
    honours the authoritative ``HeartbeatWaiter`` contract, because every wait
    is bounded; only ``set`` is a no-op, so shutdown must rely on the
    monitor-owned stop signal rather than on the injected waiter.
    """

    def __init__(self, clock: _FakeClock) -> None:
        self._clock = clock
        self.stop_requests = 0

    def wait(self, timeout: float) -> bool:
        self._clock.advance(float(timeout))
        return False

    def set(self) -> None:
        self.stop_requests += 1


class _ClockFailingWaiter:
    """Makes the producer's scheduling-clock read fail after one wait."""

    def __init__(self, clock: _ToggleFailingClock) -> None:
        self._clock = clock

    def wait(self, timeout: float) -> bool:
        self._clock.failing = True
        return False

    def set(self) -> None:
        pass


class _SetRaisingWaiter(_TickingWaiter):
    """Ticking waiter whose ``set()`` fails."""

    def set(self) -> None:
        super().set()
        raise RuntimeError("stop signal failed")


class _FailingWaitWaiter:
    """Waiter whose ``wait`` always fails."""

    def wait(self, timeout: float) -> bool:
        raise RuntimeError("wait failed")

    def set(self) -> None:
        pass


class _FirstShutdownReadThenRaises:
    """A shutdown clock that fails after establishing its injected deadline."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> float:
        self.calls += 1
        if self.calls == 1:
            return 0.0
        raise RuntimeError("shutdown clock failed")


class _FakeShutdownClock:
    """Deterministic stand-in for the real monotonic shutdown clock.

    Deliberately separate from :class:`_FakeClock`: the heartbeat schedule may be
    frozen or advanced by a test, while the shutdown bound must stay real. The
    clock only moves when a shutdown step consumes the time it was granted, so
    the tests can measure the total budget the sequence used.
    """

    def __init__(self, now: float = 0.0) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


class _DeferredCompletionSignal:
    """Producer completion that is only reported once it is awaited.

    The real producer always reports completion from ``_run``'s ``finally``
    block; here the report only becomes observable through an explicit wait, so
    the shared-budget fallback path is exercised deterministically.
    """

    def __init__(
        self, shutdown: _FakeShutdownClock, grants: list[tuple[str, float | None]]
    ) -> None:
        self._shutdown = shutdown
        self._grants = grants
        self._awaited = False

    def set(self) -> None:
        """Record the producer's own completion report."""

    def is_set(self) -> bool:
        return self._awaited

    def wait(self, timeout: float | None = None) -> bool:
        self._grants.append(("completion", timeout))
        self._shutdown.advance(timeout or 0.0)
        self._awaited = True
        return True


class _ScheduledDeadlineProbe(HostActivityMonitor):
    """Monitor that records the scheduled deadline behind each heartbeat.

    ``_await_deadline`` is always called with the absolute scheduled deadline,
    so appending it at publication time exposes the operation-based timeline
    independent of how long the sink took to accept the previous fact.
    """

    def __init__(self, **parameters: object) -> None:
        self.scheduled: list[float] = []
        self._scheduled_deadline: float | None = None
        super().__init__(**parameters)  # type: ignore[arg-type]

    def _await_deadline(self, deadline: float) -> bool:
        self._scheduled_deadline = deadline
        return super()._await_deadline(deadline)

    def _publish_heartbeat(self) -> bool:
        if self._scheduled_deadline is not None:
            self.scheduled.append(self._scheduled_deadline)
        return super()._publish_heartbeat()


class _Harness:
    """Deterministic monitor harness; always shuts the producer thread down."""

    def __init__(self, *, clock_now: float = 0.0, **overrides: object) -> None:
        self.clock = _FakeClock(clock_now)
        self.waiter = _ManualWaiter(self.clock)
        latency = overrides.pop("latency", 0.0)
        self.sink = _RecordingSink(
            raises=bool(overrides.pop("raises", False)),
            clock=self.clock,
            latency=float(latency),  # type: ignore[arg-type]
        )
        self.operation_start = self.clock.now
        parameters: dict[str, object] = {
            "phase": HostPhase.LOCKED_ASSEMBLY,
            "step": HostStep.NPM_EXECUTION,
            "expects_diagnostic_stream": True,
            "sink": self.sink,
            "clock": self.clock,
            "waiter": self.waiter,
        }
        parameters.update(overrides)
        self.waiter = parameters["waiter"]  # type: ignore[assignment]
        self.monitor = _ScheduledDeadlineProbe(**parameters)  # type: ignore[arg-type]
        self.sink.monitor = self.monitor
        self._driven = 0

    @property
    def scheduled(self) -> list[float]:
        """Absolute scheduled deadlines, in publication order."""
        return self.monitor.scheduled  # type: ignore[attr-defined]

    def heartbeat(self) -> HostHeartbeatEvent:
        """Advance simulated time to the next scheduled heartbeat."""
        index = len(self.sink.heartbeats())
        deadline = self.waiter.wait_for_deadline(self._driven + 1)
        self._driven += 1
        self.clock.advance_to(deadline)
        self.waiter.release()
        return self.sink.wait_for_heartbeats(index + 1)[index]

    def pump(self, count: int) -> list[HostHeartbeatEvent]:
        while len(self.sink.heartbeats()) < count:
            self.heartbeat()
        return self.sink.heartbeats()

    def shutdown(self) -> None:
        self.monitor.finish(HostStepState.FAILED)
        release = getattr(self.waiter, "release", None)
        if callable(release):
            release()


class _MonitorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        # Registered first so it runs after every harness shutdown cleanup.
        self.addCleanup(self._assert_no_heartbeat_thread)

    def make(self, **overrides: object) -> _Harness:
        harness = _Harness(**overrides)
        self.addCleanup(harness.shutdown)
        return harness

    def assert_stopped_before_terminal(self, harness: _Harness) -> None:
        """Delivery requires exactly one terminal, after the producer stopped."""
        self.assertFalse(harness.monitor.is_alive)
        self.assertEqual(1, len(harness.sink.terminal_events()))
        self.assertIs(False, harness.sink.thread_alive_at_terminal)

    def assert_terminal_is_last(self, harness: _Harness) -> None:
        """The single step terminal is the last event; no heartbeat follows."""
        events = harness.sink.events()
        terminal_indexes = [
            index
            for index, event in enumerate(events)
            if isinstance(event, HostStepEvent)
            and event.state is not HostStepState.STARTED
        ]
        self.assertEqual(1, len(terminal_indexes))
        self.assertEqual(len(events) - 1, terminal_indexes[0])

    def assert_single_terminal_after_shutdown(
        self, harness: _Harness, state: HostStepState
    ) -> None:
        """Exactly one terminal, after the producer stopped, with no heartbeat after."""
        # Independent confirmation that the producer reported completion.
        self.assertTrue(harness.monitor._producer_done.is_set())
        self.assert_stopped_before_terminal(harness)
        self.assertEqual(
            [state], [event.state for event in harness.sink.terminal_events()]
        )
        self.assert_terminal_is_last(harness)

    def _assert_no_heartbeat_thread(self) -> None:
        lingering = [
            thread
            for thread in threading.enumerate()
            if thread.name == "host-activity-heartbeat"
        ]
        self.assertEqual([], lingering, "a coordinator thread survived the test")


class TestHeartbeatCadence(_MonitorTestCase):
    def test_first_heartbeat_is_exactly_three_seconds_after_operation_start(self):
        harness = self.make()
        first = harness.heartbeat()
        self.assertAlmostEqual(
            FIRST_HEARTBEAT_SECONDS,
            harness.scheduled[0] - harness.operation_start,
        )
        self.assertEqual(3, first.elapsed_seconds)

    def test_elapsed_is_relative_to_operation_start_not_the_clock_origin(self):
        harness = self.make(clock_now=1_000.0)
        first = harness.heartbeat()
        self.assertEqual(3, first.elapsed_seconds)

    def test_subsequent_intervals_are_no_greater_than_one_second(self):
        harness = self.make()
        beats = harness.pump(5)
        self.assertEqual([3, 4, 5, 6, 7], [beat.elapsed_seconds for beat in beats])
        for previous, current in zip(harness.scheduled, harness.scheduled[1:]):
            interval = current - previous
            self.assertGreater(interval, 0.0)
            self.assertLessEqual(interval, HEARTBEAT_INTERVAL_SECONDS)
            self.assertAlmostEqual(HEARTBEAT_INTERVAL_SECONDS, interval)
        self.assertAlmostEqual(FIRST_HEARTBEAT_SECONDS, harness.waiter.timeouts[0])
        for timeout in harness.waiter.timeouts[1:]:
            self.assertGreater(timeout, 0.0)
            self.assertLessEqual(timeout, HEARTBEAT_INTERVAL_SECONDS)

    def test_activity_never_changes_the_heartbeat_cadence(self):
        harness = self.make()
        harness.heartbeat()
        harness.monitor.record_diagnostic()
        harness.monitor.record_transport_progress(1024)
        second = harness.heartbeat()
        self.assertEqual(4, second.elapsed_seconds)
        self.assertAlmostEqual(
            HEARTBEAT_INTERVAL_SECONDS,
            harness.scheduled[1] - harness.scheduled[0],
        )

    def test_cadence_survives_a_saturated_observation_stream(self):
        harness = self.make()
        beats = []
        for _ in range(4):
            beats.append(harness.heartbeat())
            for _ in range(64):
                harness.monitor.record_diagnostic()
                harness.monitor.record_transport_progress(1)
        self.assertEqual([3, 4, 5, 6], [beat.elapsed_seconds for beat in beats])

    def test_sink_latency_does_not_extend_the_scheduled_intervals(self):
        harness = self.make(latency=0.4)
        beats = harness.pump(5)
        # Each accepted heartbeat consumes simulated time within the bounded
        # non-blocking sink contract, yet the schedule still advances from
        # operation start in fixed one-second steps.
        self.assertEqual([3, 4, 5, 6, 7], [beat.elapsed_seconds for beat in beats])
        self.assertEqual([3.0, 4.0, 5.0, 6.0, 7.0], harness.scheduled)
        for previous, current in zip(harness.scheduled, harness.scheduled[1:]):
            self.assertAlmostEqual(HEARTBEAT_INTERVAL_SECONDS, current - previous)

    def test_next_deadline_advances_from_the_previous_scheduled_deadline(self):
        for observed in (0.0, 2.5, 3.0):
            with self.subTest(observed=observed):
                self.assertAlmostEqual(4.0, _next_heartbeat_deadline(3.0, observed))

    def test_next_deadline_never_schedules_a_past_deadline_or_a_burst(self):
        # A sink callback that overruns the interval is outside the bounded
        # non-blocking sink contract; this asserts only that the scheduling
        # arithmetic skips every expired slot in one step, so no deadline can
        # ever be scheduled in the past and no catch-up burst is possible.
        for observed in (3.0, 3.5, 4.0, 9.0, 1_000.0):
            with self.subTest(observed=observed):
                deadline = _next_heartbeat_deadline(3.0, observed)
                self.assertGreater(deadline, observed)
                self.assertLessEqual(
                    deadline - observed, HEARTBEAT_INTERVAL_SECONDS
                )
                self.assertAlmostEqual(
                    0.0, (deadline - 3.0) % HEARTBEAT_INTERVAL_SECONDS
                )


class TestDiagnosticSilence(_MonitorTestCase):
    def test_silence_is_exposed_exactly_from_two_minutes_and_keeps_updating(self):
        harness = self.make()
        beats = harness.pump(121)
        by_elapsed = {beat.elapsed_seconds: beat for beat in beats}
        self.assertIsNone(by_elapsed[119].diagnostic_silence_seconds)
        self.assertEqual(
            DIAGNOSTIC_SILENCE_SECONDS,
            by_elapsed[120].diagnostic_silence_seconds,
        )
        self.assertEqual(121, by_elapsed[121].diagnostic_silence_seconds)
        self.assertEqual(123, by_elapsed[123].diagnostic_silence_seconds)

    def test_diagnostic_activity_resets_only_the_separate_silence_clock(self):
        harness = self.make()
        beats = harness.pump(121)
        harness.monitor.record_diagnostic()
        reset = harness.heartbeat()
        self.assertEqual(beats[-1].elapsed_seconds + 1, reset.elapsed_seconds)
        self.assertIsNone(reset.diagnostic_silence_seconds)

    def test_silence_is_omitted_without_an_expected_diagnostic_stream(self):
        harness = self.make(
            step=HostStep.ARTIFACT_ACQUISITION,
            expects_diagnostic_stream=False,
        )
        beats = harness.pump(121)
        self.assertTrue(
            all(beat.diagnostic_silence_seconds is None for beat in beats)
        )
        harness.monitor.record_diagnostic()
        beat = harness.heartbeat()
        self.assertIsNone(beat.diagnostic_silence_seconds)

    def test_expected_stream_heartbeats_declare_the_applicability_flag(self):
        applicable = self.make()
        self.assertTrue(
            all(
                beat.expects_diagnostic_stream
                for beat in applicable.pump(3)
            )
        )
        inapplicable = self.make(
            step=HostStep.ARTIFACT_ACQUISITION,
            expects_diagnostic_stream=False,
        )
        self.assertTrue(
            all(
                not beat.expects_diagnostic_stream
                for beat in inapplicable.pump(3)
            )
        )


class TestActivitySeparation(_MonitorTestCase):
    def test_transport_progress_updates_activity_without_resetting_silence(self):
        harness = self.make()
        beats = harness.pump(121)
        harness.monitor.record_transport_progress(2048)
        beat = harness.heartbeat()
        expected_elapsed = beats[-1].elapsed_seconds + 1
        self.assertEqual(expected_elapsed, beat.elapsed_seconds)
        self.assertEqual(expected_elapsed, beat.diagnostic_silence_seconds)
        self.assertEqual(
            HostLastActivityKind.TRANSPORT_PROGRESS, beat.last_activity_kind
        )

    def test_transport_progress_does_not_reset_the_heartbeat_schedule(self):
        harness = self.make()
        harness.pump(2)
        harness.monitor.record_transport_progress(1)
        beat = harness.heartbeat()
        self.assertEqual(5, beat.elapsed_seconds)
        self.assertAlmostEqual(
            HEARTBEAT_INTERVAL_SECONDS,
            harness.scheduled[2] - harness.scheduled[1],
        )


class TestLastActivityFacts(_MonitorTestCase):
    def test_activity_is_absent_before_the_first_observation(self):
        harness = self.make()
        for beat in harness.pump(3):
            self.assertIsNone(beat.last_activity_kind)
            self.assertIsNone(beat.last_activity_age_seconds)

    def test_newer_observation_replaces_kind_and_age(self):
        harness = self.make()
        harness.heartbeat()
        harness.monitor.record_diagnostic()
        diagnostic = harness.heartbeat()
        self.assertEqual(HostLastActivityKind.DIAGNOSTIC, diagnostic.last_activity_kind)
        self.assertEqual(1, diagnostic.last_activity_age_seconds)
        harness.monitor.record_transport_progress(4096)
        transport = harness.heartbeat()
        self.assertEqual(
            HostLastActivityKind.TRANSPORT_PROGRESS, transport.last_activity_kind
        )
        self.assertEqual(1, transport.last_activity_age_seconds)

    def test_age_is_floored_to_whole_seconds(self):
        harness = self.make()
        harness.heartbeat()
        harness.clock.advance(0.6)
        harness.monitor.record_diagnostic()
        below = harness.heartbeat()
        self.assertEqual(4, below.elapsed_seconds)
        self.assertIsNone(below.last_activity_kind)
        self.assertIsNone(below.last_activity_age_seconds)
        above = harness.heartbeat()
        self.assertEqual(HostLastActivityKind.DIAGNOSTIC, above.last_activity_kind)
        self.assertEqual(1, above.last_activity_age_seconds)

    def test_sub_second_age_is_omitted_without_delaying_the_heartbeat(self):
        harness = self.make()
        harness.heartbeat()
        harness.clock.advance(0.9)
        harness.monitor.record_diagnostic()
        beat = harness.heartbeat()
        self.assertEqual(4, beat.elapsed_seconds)
        self.assertIsNone(beat.last_activity_kind)
        self.assertIsNone(beat.last_activity_age_seconds)

    def test_heartbeat_protocol_rejects_a_sub_second_activity_age(self):
        with self.assertRaises(TypeError):
            HostHeartbeatEvent(
                HostPhase.LOCKED_ASSEMBLY,
                HostStep.NPM_EXECUTION,
                3,
                True,
                last_activity_kind=HostLastActivityKind.DIAGNOSTIC,
                last_activity_age_seconds=0,
            )


class TestDeadlineFacts(_MonitorTestCase):
    def test_remaining_deadline_is_absent_without_an_owned_deadline(self):
        harness = self.make()
        for beat in harness.pump(3):
            self.assertIsNone(beat.remaining_deadline_seconds)

    def test_remaining_deadline_is_nonnegative_and_counts_down(self):
        harness = self.make(deadline_seconds=10)
        beats = harness.pump(5)
        self.assertEqual([7, 6, 5, 4, 3], [b.remaining_deadline_seconds for b in beats])
        self.assertTrue(
            all(
                beat.remaining_deadline_seconds is not None
                and beat.remaining_deadline_seconds >= 0
                for beat in beats
            )
        )

    def test_remaining_deadline_clamps_to_zero_after_expiry(self):
        harness = self.make(deadline_seconds=3)
        beats = harness.pump(4)
        self.assertEqual(
            [0, 0, 0, 0], [beat.remaining_deadline_seconds for beat in beats]
        )

    def test_heartbeat_facts_are_wording_neutral(self):
        harness = self.make()
        beat = harness.heartbeat()
        self.assertEqual(
            {
                "phase",
                "step",
                "elapsed_seconds",
                "expects_diagnostic_stream",
                "diagnostic_silence_seconds",
                "last_activity_kind",
                "last_activity_age_seconds",
                "remaining_deadline_seconds",
            },
            {field.name for field in dataclasses.fields(beat)},
        )
        for absent in ("text", "message", "exception", "detail", "inactive"):
            self.assertFalse(hasattr(beat, absent))
        self.assertIsInstance(beat.phase, HostPhase)
        self.assertIsInstance(beat.step, HostStep)


class TestTerminalLifecycle(_MonitorTestCase):
    def test_start_emits_the_step_started_transition(self):
        harness = self.make()
        events = harness.sink.events()
        self.assertEqual(1, len(events))
        started = events[0]
        self.assertIsInstance(started, HostStepEvent)
        self.assertEqual(HostStepState.STARTED, started.state)
        self.assertEqual(HostStep.NPM_EXECUTION, started.step)
        self.assertTrue(started.expects_diagnostic_stream)

    def test_every_terminal_path_delivers_exactly_one_step_terminal(self):
        # Timeout and cancellation are failure terminals at this boundary; the
        # coordinator owns one atomic terminal transition used by every path.
        cases = (
            ("success", HostStepState.SUCCEEDED),
            ("failure", HostStepState.FAILED),
            ("timeout", HostStepState.FAILED),
            ("cancellation", HostStepState.FAILED),
        )
        for label, state in cases:
            with self.subTest(path=label):
                harness = self.make()
                harness.pump(2)
                harness.monitor.finish(state)
                self.assert_single_terminal_after_shutdown(harness, state)
                # Later activity is rejected and repeated finish() calls never
                # duplicate or replace the delivered terminal event.
                self.assertFalse(harness.monitor.record_diagnostic())
                self.assertFalse(harness.monitor.record_transport_progress(1))
                harness.clock.advance(600.0)
                harness.waiter.release()
                harness.monitor.finish(HostStepState.SUCCEEDED)
                self.assert_single_terminal_after_shutdown(harness, state)

    def test_waiter_failure_path_delivers_exactly_one_step_terminal(self):
        harness = self.make(waiter=_FailingWaitWaiter())
        harness.monitor.finish(HostStepState.FAILED)
        self.assert_single_terminal_after_shutdown(harness, HostStepState.FAILED)
        self.assertFalse(harness.monitor.record_diagnostic())

    def test_no_heartbeat_is_delivered_after_the_terminal_event(self):
        harness = self.make()
        harness.pump(2)
        harness.monitor.finish(HostStepState.SUCCEEDED)
        self.assert_stopped_before_terminal(harness)
        harness.clock.advance(600.0)
        harness.waiter.release()
        self.assert_terminal_is_last(harness)

    def test_later_activity_is_rejected_after_terminal(self):
        harness = self.make()
        harness.pump(1)
        harness.monitor.finish(HostStepState.FAILED)
        self.assert_stopped_before_terminal(harness)
        self.assertFalse(harness.monitor.record_diagnostic())
        self.assertFalse(harness.monitor.record_transport_progress(4096))

    def test_finish_is_idempotent(self):
        harness = self.make()
        harness.monitor.finish(HostStepState.FAILED)
        harness.monitor.finish(HostStepState.SUCCEEDED)
        self.assert_stopped_before_terminal(harness)
        self.assertEqual(
            [HostStepState.FAILED],
            [event.state for event in harness.sink.terminal_events()],
        )

    def test_concurrent_terminal_and_activity_is_race_safe(self):
        harness = self.make()
        harness.pump(2)
        stop = threading.Event()

        def observe() -> None:
            while not stop.is_set():
                harness.monitor.record_diagnostic()
                harness.monitor.record_transport_progress(1)

        observer = threading.Thread(target=observe, daemon=True)
        observer.start()
        try:
            harness.monitor.finish(HostStepState.FAILED)
        finally:
            stop.set()
            observer.join(timeout=5.0)
        self.assertFalse(observer.is_alive())
        events = harness.sink.events()
        self.assert_stopped_before_terminal(harness)
        self.assertIsInstance(events[-1], HostStepEvent)

    def test_absent_sink_preserves_the_primary_result(self):
        # No sink can observe delivery, so only the stop is asserted here.
        harness = self.make(sink=None)
        self.assertTrue(harness.monitor.record_diagnostic())
        harness.monitor.finish(HostStepState.SUCCEEDED)
        self.assertFalse(harness.monitor.is_alive)

    def test_throwing_sink_preserves_the_primary_result(self):
        harness = self.make(raises=True)
        harness.pump(2)
        harness.monitor.finish(HostStepState.FAILED)
        self.assert_stopped_before_terminal(harness)


class TestBoundedJoin(_MonitorTestCase):
    def test_join_bound_exceeds_the_longest_producer_wait(self):
        # Every waiter that honours its timeout therefore lets the producer
        # observe the monitor-owned stop signal inside the shutdown bound.
        self.assertGreater(HEARTBEAT_JOIN_TIMEOUT_SECONDS, FIRST_HEARTBEAT_SECONDS)
        self.assertGreater(HEARTBEAT_JOIN_TIMEOUT_SECONDS, HEARTBEAT_INTERVAL_SECONDS)

    def test_finish_stops_a_producer_whose_waiter_ignores_set(self):
        clock = _FakeClock()
        waiter = _TickingWaiter(clock)
        harness = self.make(clock=clock, waiter=waiter)
        harness.sink.wait_for_heartbeats(1)
        harness.monitor.finish(HostStepState.SUCCEEDED)
        # Shutdown relied on the monitor-owned stop signal, not on set().
        self.assertTrue(harness.monitor._producer_done.is_set())
        self.assert_stopped_before_terminal(harness)
        self.assert_terminal_is_last(harness)

    def test_set_failure_is_secondary_to_the_wrapped_result(self):
        clock = _FakeClock()
        waiter = _SetRaisingWaiter(clock)
        harness = self.make(clock=clock, waiter=waiter)
        harness.sink.wait_for_heartbeats(1)
        harness.monitor.finish(HostStepState.FAILED)
        self.assertEqual(1, waiter.stop_requests)
        self.assertTrue(harness.monitor._producer_done.is_set())
        self.assert_stopped_before_terminal(harness)
        self.assert_terminal_is_last(harness)

    def test_join_failure_still_delivers_the_terminal_event(self):
        clock = _FakeClock()
        harness = self.make(clock=clock, waiter=_TickingWaiter(clock))
        harness.sink.wait_for_heartbeats(1)

        def failing_join(self, timeout: float | None = None) -> bool:
            raise RuntimeError("join failed")

        with patch.object(_ScheduledDeadlineProbe, "join", failing_join):
            harness.monitor.finish(HostStepState.FAILED)
        # A failing join is shutdown plumbing, not a lifecycle outcome: the
        # producer's own completion signal confirms the stop, so the required
        # terminal event is still delivered exactly once, without raising.
        self.assertTrue(harness.monitor._producer_done.is_set())
        self.assert_single_terminal_after_shutdown(harness, HostStepState.FAILED)

    def test_budget_exhaustion_prevents_terminal_delivery(self):
        clock = _FakeClock()
        shutdown = _FakeShutdownClock()
        harness = self.make(
            clock=clock,
            waiter=_TickingWaiter(clock),
            shutdown_clock=shutdown,
        )
        harness.sink.wait_for_heartbeats(1)
        grants: list[tuple[str, float | None]] = []
        harness.monitor._producer_done = _DeferredCompletionSignal(shutdown, grants)  # type: ignore[assignment]

        def slow_join(self, timeout: float | None = None) -> bool:
            grants.append(("join", timeout))
            # The primary confirmation consumes part of the one budget and then
            # reports that production is not confirmed stopped.
            shutdown.advance(min(timeout or 0.0, 3.0))
            return False

        with patch.object(_ScheduledDeadlineProbe, "join", slow_join):
            harness.monitor.finish(HostStepState.SUCCEEDED)

        # The completion wait receives only the remainder of the same budget;
        # exhaustion permits neither a fresh timeout nor an unbounded fallback.
        self.assertEqual(["join", "completion"], [name for name, _ in grants])
        self.assertGreater(grants[0][1] or 0.0, 0.0)
        self.assertLessEqual(grants[0][1] or 0.0, HEARTBEAT_JOIN_TIMEOUT_SECONDS)
        self.assertEqual(2.0, grants[1][1])
        self.assertEqual(HEARTBEAT_JOIN_TIMEOUT_SECONDS, shutdown.now)
        self.assertEqual([], harness.sink.terminal_events())
        # Confirmation can be retried after the producer has stopped; it uses
        # the original requested state and delivers exactly once.
        harness.monitor.finish(HostStepState.FAILED)
        self.assert_single_terminal_after_shutdown(harness, HostStepState.SUCCEEDED)
        harness.monitor.finish(HostStepState.FAILED)
        self.assertEqual(1, len(harness.sink.terminal_events()))

    def test_confirmation_failure_prevents_terminal_delivery(self):
        clock = _FakeClock()
        shutdown = _FakeShutdownClock()
        harness = self.make(
            clock=clock,
            waiter=_TickingWaiter(clock),
            shutdown_clock=shutdown,
        )
        harness.sink.wait_for_heartbeats(1)
        grants: list[tuple[str, float | None]] = []
        harness.monitor._producer_done.set()

        def failing_join(self, timeout: float | None = None) -> bool:
            grants.append(("join", timeout))
            raise RuntimeError("join failed")

        producer_thread = harness.monitor._thread

        def failing_thread_join(timeout: float | None = None) -> None:
            grants.append(("thread.join", timeout))
            raise RuntimeError("thread exit confirmation failed")

        with (
            patch.object(_ScheduledDeadlineProbe, "join", failing_join),
            patch.object(harness.monitor._thread, "join", failing_thread_join),
        ):
            harness.monitor.finish(HostStepState.FAILED)

        # Both exceptions are contained, but neither counts as confirmation.
        self.assertEqual(["join", "thread.join"], [name for name, _ in grants])
        for _, timeout in grants:
            self.assertGreater(timeout or 0.0, 0.0)
            self.assertLessEqual(timeout or 0.0, HEARTBEAT_JOIN_TIMEOUT_SECONDS)
        self.assertEqual([], harness.sink.terminal_events())
        # Restore normal confirmation and retry: the first requested state is
        # retained and one terminal event follows the stopped producer.
        producer_thread.join(timeout=5.0)
        self.assertFalse(producer_thread.is_alive())
        harness.monitor.finish(HostStepState.SUCCEEDED)
        self.assert_single_terminal_after_shutdown(harness, HostStepState.FAILED)
        harness.monitor.finish(HostStepState.SUCCEEDED)
        self.assertEqual(1, len(harness.sink.terminal_events()))

    def test_concurrent_retries_deliver_the_first_requested_terminal_once(self):
        clock = _FakeClock()
        harness = self.make(clock=clock, waiter=_TickingWaiter(clock))
        harness.sink.wait_for_heartbeats(1)

        def unconfirmed_join(self, timeout: float | None = None) -> bool:
            return False

        def failing_thread_join(timeout: float | None = None) -> None:
            raise RuntimeError("thread confirmation failed")

        with (
            patch.object(_ScheduledDeadlineProbe, "join", unconfirmed_join),
            patch.object(harness.monitor._thread, "join", failing_thread_join),
        ):
            harness.monitor.finish(HostStepState.FAILED)
        self.assertEqual([], harness.sink.terminal_events())

        retries = [
            threading.Thread(target=harness.monitor.finish, args=(state,))
            for state in (HostStepState.SUCCEEDED, HostStepState.FAILED)
        ]
        for retry in retries:
            retry.start()
        for retry in retries:
            retry.join(timeout=5.0)
            self.assertFalse(retry.is_alive())
        self.assert_single_terminal_after_shutdown(harness, HostStepState.FAILED)


class TestClockFailureIsolation(_MonitorTestCase):
    def test_activity_clock_failures_are_contained_without_partial_updates(self):
        clock = _ToggleFailingClock()
        harness = self.make(clock=clock)
        clock.failing = True
        self.assertFalse(harness.monitor.record_diagnostic())
        self.assertFalse(harness.monitor.record_transport_progress(4096))
        self.assertEqual(0, harness.monitor.received_bytes)
        clock.failing = False
        harness.monitor.finish(HostStepState.SUCCEEDED)
        self.assert_single_terminal_after_shutdown(harness, HostStepState.SUCCEEDED)

    def test_heartbeat_clock_failure_is_contained(self):
        clock = _ToggleFailingClock()
        harness = self.make(clock=clock, waiter=_ClockFailingWaiter(clock))
        harness.monitor._thread.join(timeout=5.0)
        self.assertFalse(harness.monitor.is_alive)
        clock.failing = False
        harness.monitor.finish(HostStepState.SUCCEEDED)
        self.assert_single_terminal_after_shutdown(harness, HostStepState.SUCCEEDED)

    def test_shutdown_clock_failure_after_deadline_uses_real_budget(self):
        clock = _FakeClock()
        shutdown = _FirstShutdownReadThenRaises()
        harness = self.make(
            clock=clock,
            waiter=_TickingWaiter(clock),
            shutdown_clock=shutdown,
        )
        harness.sink.wait_for_heartbeats(1)
        grants: list[float | None] = []
        original_join = harness.monitor.join

        def recording_join(self, timeout: float | None = None) -> bool:
            grants.append(timeout)
            return original_join(timeout)

        with patch.object(_ScheduledDeadlineProbe, "join", recording_join):
            harness.monitor.finish(HostStepState.SUCCEEDED)

        self.assertGreaterEqual(shutdown.calls, 2)
        self.assertEqual(1, len(grants))
        self.assertIsNotNone(grants[0])
        self.assertGreater(float(grants[0]), 0.0)
        self.assertLessEqual(float(grants[0]), HEARTBEAT_JOIN_TIMEOUT_SECONDS)
        self.assert_single_terminal_after_shutdown(harness, HostStepState.SUCCEEDED)

    def test_shutdown_clock_failures_are_contained_at_deadline_and_remaining(self):
        clock = _FakeClock()
        shutdown = _ToggleFailingClock()
        harness = self.make(
            clock=clock,
            waiter=_TickingWaiter(clock),
            shutdown_clock=shutdown,
        )
        harness.sink.wait_for_heartbeats(1)
        shutdown.failing = True
        harness.monitor.finish(HostStepState.FAILED)
        self.assert_single_terminal_after_shutdown(harness, HostStepState.FAILED)


class TestMonitorValidation(_MonitorTestCase):
    def _base(self) -> dict[str, object]:
        return {
            "phase": HostPhase.LOCKED_ASSEMBLY,
            "step": HostStep.NPM_EXECUTION,
            "expects_diagnostic_stream": True,
            "sink": None,
            "clock": lambda: 0.0,
            "waiter": threading.Event(),
        }

    def test_rejects_invalid_configuration(self):
        cases = (
            ("phase", TypeError),
            ("step", TypeError),
            ("expects_diagnostic_stream", TypeError),
            ("sink", TypeError),
            ("clock", TypeError),
            ("shutdown_clock", TypeError),
            ("waiter", TypeError),
        )
        invalid = {
            "phase": "release_acquisition",
            "step": "npm_execution",
            "expects_diagnostic_stream": "yes",
            "sink": 5,
            "clock": 5,
            "shutdown_clock": 5,
            "waiter": 5,
        }
        for key, error in cases:
            with self.subTest(key=key):
                with self.assertRaises(error):
                    HostActivityMonitor(**{**self._base(), key: invalid[key]})  # type: ignore[arg-type]

    def test_rejects_invalid_deadlines(self):
        for value in (-1, -0.5, True, "10"):
            with self.subTest(value=value):
                with self.assertRaises((TypeError, ValueError)):
                    HostActivityMonitor(
                        **{**self._base(), "deadline_seconds": value}  # type: ignore[arg-type]
                    )

    def test_rejects_non_terminal_and_invalid_finish_states(self):
        harness = self.make()
        with self.assertRaises(ValueError):
            harness.monitor.finish(HostStepState.STARTED)
        with self.assertRaises(TypeError):
            harness.monitor.finish("succeeded")  # type: ignore[arg-type]

    def test_rejects_invalid_transport_byte_counts(self):
        harness = self.make()
        for value in (-1, True, 1.5, "8"):
            with self.subTest(value=value):
                with self.assertRaises((TypeError, ValueError)):
                    harness.monitor.record_transport_progress(value)  # type: ignore[arg-type]

    def test_waiter_contract_requires_bounded_waits_and_a_stop_request(self):
        # The contract is documented where it is declared: a waiter must honour
        # its timeout in ``wait`` and request interruption in ``set``. A waiter
        # that ignores both is out of contract and is never accommodated by
        # granting extra time, raising, or delivering the terminal event early.
        contract = HeartbeatWaiter.__doc__ or ""
        self.assertIn("timeout", contract)
        self.assertIn("set", contract)
        self.assertIn("outside this contract", contract)


if __name__ == "__main__":
    unittest.main()
