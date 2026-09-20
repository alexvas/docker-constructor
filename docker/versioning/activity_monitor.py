"""Orchestration-owned monotonic activity monitoring and heartbeat facts.

Phase 3 of ``improve-host-build-observability``. One :class:`HostActivityMonitor`
wraps exactly one host pipeline step, owns its bounded heartbeat producer
thread, and emits only presentation-neutral immutable facts:

* one step-started transition when the step begins, and exactly one
  succeeded/failed transition once heartbeat production is confirmed stopped
  when it terminally ends, each carrying the optional closed
  ``logical_resource`` of a single reviewed asset step,
* an activity-independent heartbeat beginning exactly 3 seconds after
  operation start and thereafter scheduled one second apart from the previous
  *scheduled* deadline, so sink or callback latency can never stretch
  start-to-start intervals,
* at most one cumulative ``HostTransportProgressEvent`` per heartbeat when new
  body bytes were observed, plus one immediately before the terminal fact, so
  every already-yielded chunk contributes to cumulative bytes without emitting
  a presentation event per chunk, and
* heartbeat facts carrying elapsed whole seconds, an applicability-gated
  diagnostic-silence duration, the latest observed ``diagnostic`` or
  ``transport_progress`` activity kind with its whole monotonic age, and
  remaining time only when the wrapped operation already owns a fixed
  deadline.

The monitor never renders, inspects Docker/process/network state, records a
wall-clock timestamp, or represents diagnostic silence as evidence that
execution or networking is inactive. It keeps two independent clocks: the
heartbeat schedule, which no activity resets, and the diagnostic-silence
clock, which only stdout/stderr resets and which exists only for a step that
declared an expected diagnostic stream. Transport progress updates general
last activity without touching either the silence clock or the schedule.

Terminal handling is atomic: the monitor marks the step terminal and rejects
later activity, raises a stop signal it owns itself under its own lock,
requests the waiter stop, confirms the bounded heartbeat producer has stopped,
and then emits exactly one step-terminal event. The monitor-owned stop signal
is rechecked around every bounded wait, and the producer additionally reports
its own completion, so production ends inside the shutdown bound for every
waiter that honours its timeout, without depending solely on an injected
waiter honouring ``set()``. A monitor, clock, waiter, or sink failure is always
secondary to the wrapped operation's result, is contained inside the monitor,
and never emits a terminal event before production is confirmed stopped.
"""
from __future__ import annotations

import math
import threading
import time
from typing import Callable, Protocol

from docker.versioning.host_progress import (
    HostEventSink,
    HostHeartbeatEvent,
    HostLastActivityKind,
    HostPhase,
    HostStep,
    HostStepEvent,
    HostStepState,
    HostTransportProgressEvent,
    emit,
)
from docker.versioning.logical_resource import require_approved_logical_resource

#: Seconds after operation start before the first heartbeat fact.
FIRST_HEARTBEAT_SECONDS = 3.0
#: Upper bound on the interval between subsequent heartbeat facts.
HEARTBEAT_INTERVAL_SECONDS = 1.0
#: Seconds without stdout/stderr before diagnostic silence is exposed.
DIAGNOSTIC_SILENCE_SECONDS = 120.0
#: Whole seconds of activity age required before last-activity is projected.
MINIMUM_LAST_ACTIVITY_AGE_SECONDS = 1
#: One shared shutdown budget, in seconds, for the whole stop-confirmation
#: sequence. Must exceed :data:`FIRST_HEARTBEAT_SECONDS`, the longest single
#: wait the producer can be blocked in, so every waiter that honours its
#: timeout lets the producer observe the monitor-owned stop signal well inside
#: this bound. Each confirmation step receives only the time still left on this
#: single deadline, so the sequence can never multiply the bound.
HEARTBEAT_JOIN_TIMEOUT_SECONDS = 5.0


def _next_heartbeat_deadline(previous: float, observed: float) -> float:
    """Next scheduled deadline, measured from *previous*, not from *observed*.

    ``observed`` is the clock read after publishing, so presentation latency
    can never move the operation-based cadence. If publication overran one or
    more deadlines, every expired slot is skipped in a single step instead of
    emitting a catch-up burst, and the result is always strictly after
    ``observed`` and within one interval of it.
    """
    deadline = previous + HEARTBEAT_INTERVAL_SECONDS
    if deadline <= observed:
        missed = (
            math.floor((observed - deadline) / HEARTBEAT_INTERVAL_SECONDS) + 1
        )
        deadline += missed * HEARTBEAT_INTERVAL_SECONDS
    return deadline


class HeartbeatWaiter(Protocol):
    """``threading.Event``-compatible interruptible waiter.

    This contract is authoritative: ``set`` requests interruption, and
    ``wait`` returns ``True`` once a stop has been requested and ``False``
    once its timeout elapsed, so a conforming waiter always lets the producer
    observe the monitor-owned stop signal inside
    :data:`HEARTBEAT_JOIN_TIMEOUT_SECONDS`. A waiter that ignores both
    ``set()`` and its own timeout is outside this contract: it is never
    accommodated by granting another timeout or by raising out of the monitor.
    A failed bounded confirmation defers terminal delivery until a later bounded
    retry confirms the producer has stopped. Injectability lets tests drive
    simulated time without real sleeps.
    """

    def wait(self, timeout: float) -> bool: ...

    def set(self) -> None: ...


class HostActivityMonitor:
    """Monitor one host step and publish its heartbeat and terminal facts."""

    def __init__(
        self,
        *,
        phase: HostPhase,
        step: HostStep,
        expects_diagnostic_stream: bool,
        sink: HostEventSink | None,
        clock: Callable[[], float] = time.monotonic,
        waiter: HeartbeatWaiter | None = None,
        deadline_seconds: float | None = None,
        shutdown_clock: Callable[[], float] = time.monotonic,
        logical_resource: str | None = None,
    ) -> None:
        if not isinstance(phase, HostPhase):
            raise TypeError("phase must be a HostPhase member")
        if not isinstance(step, HostStep):
            raise TypeError("step must be a HostStep member")
        if not isinstance(expects_diagnostic_stream, bool):
            raise TypeError("expects_diagnostic_stream must be a boolean")
        require_approved_logical_resource(logical_resource, "logical_resource")
        if sink is not None and not callable(sink):
            raise TypeError("sink must be callable or None")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if not callable(shutdown_clock):
            raise TypeError("shutdown_clock must be callable")
        if waiter is None:
            stop_wait: HeartbeatWaiter = threading.Event()
        elif callable(getattr(waiter, "wait", None)) and callable(
            getattr(waiter, "set", None)
        ):
            stop_wait = waiter
        else:
            raise TypeError("waiter must provide callable wait() and set()")
        if deadline_seconds is not None:
            if isinstance(deadline_seconds, bool) or not isinstance(
                deadline_seconds, (int, float)
            ):
                raise TypeError("deadline_seconds must be a number or None")
            if deadline_seconds < 0:
                raise ValueError("deadline_seconds must be nonnegative")

        self._phase = phase
        self._step = step
        self._expects_diagnostic_stream = expects_diagnostic_stream
        self._logical_resource = logical_resource
        self._sink = sink
        self._clock = clock
        # The shutdown bound is enforced with its own real monotonic clock:
        # the injected scheduling clock may be frozen or advanced by tests, and
        # that must never weaken how long shutdown may take.
        self._shutdown_clock = shutdown_clock
        self._waiter = stop_wait
        self._lock = threading.Lock()
        self._started_at = float(clock())
        self._deadline_at = (
            None
            if deadline_seconds is None
            else self._started_at + float(deadline_seconds)
        )
        # Diagnostic silence belongs only to a step that declared an expected
        # diagnostic stream; it starts counting from operation start.
        self._last_diagnostic_at: float | None = (
            self._started_at if expects_diagnostic_stream else None
        )
        self._last_activity_kind: HostLastActivityKind | None = None
        self._last_activity_at: float | None = None
        self._received_bytes = 0
        self._published_bytes = 0
        self._terminal_requested = False
        self._terminal_delivered = False
        self._requested_terminal_state: HostStepState | None = None
        self._stop = threading.Event()
        #: Independent confirmation that production ended, so shutdown never
        #: depends on a join that could fail. Set from the producer's own
        #: ``finally`` block, however it exits.
        self._producer_done = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="host-activity-heartbeat", daemon=True
        )

        emit(
            self._sink,
            HostStepEvent(
                phase, step, HostStepState.STARTED, expects_diagnostic_stream,
                logical_resource,
            ),
        )
        self._thread.start()

    @property
    def is_alive(self) -> bool:
        """Whether bounded heartbeat production is still running."""
        return self._thread.is_alive()

    @property
    def received_bytes(self) -> int:
        """Latest cumulative transport bytes observed for the wrapped step.

        The value is recorded here so a later step can publish it at
        heartbeat cadence; this phase neither derives rates nor emits one
        event per observed chunk.
        """
        with self._lock:
            return self._received_bytes

    def record_diagnostic(self) -> bool:
        """Observe a stdout/stderr chunk.

        Resets the separate diagnostic-silence clock and becomes the latest
        ``diagnostic`` activity. Returns ``False`` once the step is terminal,
        where later activity is rejected without changing any fact.
        """
        with self._lock:
            if self._terminal_requested:
                return False
            try:
                now = float(self._clock())
            except Exception:
                return False
            self._last_diagnostic_at = now
            self._last_activity_kind = HostLastActivityKind.DIAGNOSTIC
            self._last_activity_at = now
            return True

    def record_transport_progress(self, received_bytes: int) -> bool:
        """Observe transport progress with the latest cumulative byte count.

        Becomes the latest ``transport_progress`` activity and never resets
        diagnostic silence or the heartbeat schedule. Returns ``False`` once
        the step is terminal.
        """
        if (
            not isinstance(received_bytes, int)
            or isinstance(received_bytes, bool)
            or received_bytes < 0
        ):
            raise ValueError("received_bytes must be a nonnegative whole number")
        with self._lock:
            if self._terminal_requested:
                return False
            try:
                now = float(self._clock())
            except Exception:
                return False
            self._received_bytes = received_bytes
            self._last_activity_kind = HostLastActivityKind.TRANSPORT_PROGRESS
            self._last_activity_at = now
            return True

    def finish(self, state: HostStepState) -> None:
        """Terminally end the step and deliver its required terminal event.

        Terminal request and delivery are distinct: the first request stores
        its state, rejects later activity, and raises the monitor-owned stop
        signal. Each call may retry bounded shutdown confirmation; once it
        succeeds, exactly one event with the first requested state is emitted.
        Timeout and cancellation are failure terminals at this boundary and pass
        :attr:`HostStepState.FAILED`.

        Waiter and join failures are secondary to the wrapped operation: they
        are contained here and never replace its result. They also cannot be
        mistaken for successful confirmation, so a terminal event is delivered
        only after production is confirmed stopped.
        """
        if not isinstance(state, HostStepState):
            raise TypeError("state must be a HostStepState member")
        if state is HostStepState.STARTED:
            raise ValueError("terminal state must be succeeded or failed")
        with self._lock:
            if self._terminal_delivered:
                return
            if not self._terminal_requested:
                self._terminal_requested = True
                self._requested_terminal_state = state
                # The monitor owns this signal: producer shutdown must not depend
                # solely on the injected waiter honouring ``set()``.
                self._stop.set()
        try:
            self._waiter.set()
        except Exception:
            # A failing waiter is secondary; the producer observes the
            # monitor-owned stop signal around every bounded wait.
            pass
        if not self._stop_producer():
            return
        with self._lock:
            if self._terminal_delivered:
                return
            requested_state = self._requested_terminal_state
            if requested_state is None:
                return
            self._terminal_delivered = True
        # The final cumulative byte count is published exactly once before the
        # terminal fact so a short download still exposes observed bytes.
        self._publish_progress_event()
        emit(
            self._sink,
            HostStepEvent(
                self._phase, self._step, requested_state,
                self._expects_diagnostic_stream, self._logical_resource,
            ),
        )

    def _publish_progress_event(self) -> None:
        """Publish pending cumulative bytes outside a heartbeat cycle."""
        with self._lock:
            self._publish_progress_locked()

    def _publish_progress_locked(self) -> None:
        """Emit the latest cumulative bytes once; the caller holds the lock."""
        received = self._received_bytes
        if received <= self._published_bytes:
            return
        self._published_bytes = received
        emit(
            self._sink,
            HostTransportProgressEvent(
                self._phase, self._step, received, self._logical_resource
            ),
        )

    def _stop_producer(self) -> bool:
        """Confirm bounded heartbeat production ended within one deadline.

        Every join and producer-completion wait receives only the time left
        on one fixed shutdown attempt, capped by an independent real-monotonic
        deadline. Failures are contained so they do
        not replace the wrapped operation result, but they are not treated as
        confirmation: ``False`` defers :meth:`finish` terminal delivery until
        a later bounded retry succeeds.
        """
        real_deadline = time.monotonic() + HEARTBEAT_JOIN_TIMEOUT_SECONDS
        try:
            deadline: float | None = (
                float(self._shutdown_clock()) + HEARTBEAT_JOIN_TIMEOUT_SECONDS
            )
        except Exception:
            deadline = None
        return self._confirm_producer_stopped(deadline, real_deadline)

    def _confirm_producer_stopped(
        self, deadline: float | None, real_deadline: float
    ) -> bool:
        """Confirm inside the shared budget that heartbeat production ended.

        ``join`` is attempted first with the time left on the fixed shutdown
        attempt, capped by the independent real-monotonic deadline.
        The producer's own completion report, set from ``_run``'s ``finally``
        block however it exits, is an independent confirmation that reuses the
        same remaining time instead of a fresh timeout.
        """
        while True:
            remaining = self._shutdown_remaining(deadline, real_deadline)
            if remaining <= 0.0:
                return False
            try:
                if self.join(timeout=remaining):
                    return True
            except Exception:
                pass
            if self._producer_done.is_set():
                return self._confirm_thread_exit(deadline, real_deadline)
            remaining = self._shutdown_remaining(deadline, real_deadline)
            if remaining <= 0.0:
                return False
            try:
                self._producer_done.wait(timeout=remaining)
            except Exception:
                return False

    def _confirm_thread_exit(
        self, deadline: float | None, real_deadline: float
    ) -> bool:
        """Confirm the producer thread is gone within the shared deadline."""
        remaining = self._shutdown_remaining(deadline, real_deadline)
        if remaining <= 0.0:
            return False
        try:
            self._thread.join(timeout=remaining)
        except Exception:
            return False
        return not self._thread.is_alive()

    def _shutdown_remaining(
        self, deadline: float | None, real_deadline: float
    ) -> float:
        """Return the remaining shared budget without mixing clock epochs."""
        real_remaining = max(0.0, real_deadline - time.monotonic())
        if deadline is None:
            return real_remaining
        try:
            injected_remaining = max(0.0, deadline - float(self._shutdown_clock()))
        except Exception:
            return real_remaining
        return min(injected_remaining, real_remaining)

    def join(self, timeout: float | None = None) -> bool:
        """Bound heartbeat production and report whether it has stopped.

        The wait is always bounded, so a terminal transition can never block
        indefinitely. Returns ``True`` once the heartbeat producer thread is no
        longer alive.
        """
        if timeout is None:
            timeout = HEARTBEAT_JOIN_TIMEOUT_SECONDS
        self._thread.join(timeout=timeout)
        return not self._thread.is_alive()

    def _run(self) -> None:
        try:
            next_at = self._started_at + FIRST_HEARTBEAT_SECONDS
            while True:
                if self._await_deadline(next_at):
                    return
                if not self._publish_heartbeat():
                    return
                next_at = _next_heartbeat_deadline(
                    next_at, float(self._clock())
                )
        except BaseException:
            # Monitor, clock, waiter, or sink failure must never escape the
            # heartbeat producer or change the wrapped operation's result.
            return
        finally:
            # Always reported, so shutdown confirmation never depends on a
            # join that could fail.
            self._producer_done.set()

    def _await_deadline(self, deadline: float) -> bool:
        """Sleep interruptibly until *deadline*; report whether to stop.

        The monitor-owned stop signal is rechecked before and after every
        bounded wait, so production ends without relying on the injected
        waiter honouring ``set()``. Each wait is bounded by the remaining
        interval, so a conforming waiter always lets the producer terminate
        inside :data:`HEARTBEAT_JOIN_TIMEOUT_SECONDS`.
        """
        while True:
            if self._stop.is_set():
                return True
            remaining = deadline - float(self._clock())
            if remaining <= 0.0:
                return False
            if self._waiter.wait(remaining):
                return True

    def _publish_heartbeat(self) -> bool:
        with self._lock:
            if self._terminal_requested:
                return False
            # Byte progress is published at heartbeat cadence, never once per
            # observed chunk, and immediately before the heartbeat fact.
            self._publish_progress_locked()
            emit(self._sink, self._heartbeat_event(float(self._clock())))
            return True

    def _heartbeat_event(self, now: float) -> HostHeartbeatEvent:
        silence: int | None = None
        if self._expects_diagnostic_stream and self._last_diagnostic_at is not None:
            silent_for = now - self._last_diagnostic_at
            if silent_for >= DIAGNOSTIC_SILENCE_SECONDS:
                silence = math.floor(silent_for)

        kind: HostLastActivityKind | None = None
        age: int | None = None
        if self._last_activity_at is not None:
            whole_age = math.floor(now - self._last_activity_at)
            if whole_age >= MINIMUM_LAST_ACTIVITY_AGE_SECONDS:
                kind = self._last_activity_kind
                age = whole_age

        remaining: int | None = None
        if self._deadline_at is not None:
            remaining = max(0, math.floor(self._deadline_at - now))

        return HostHeartbeatEvent(
            phase=self._phase,
            step=self._step,
            elapsed_seconds=max(0, math.floor(now - self._started_at)),
            expects_diagnostic_stream=self._expects_diagnostic_stream,
            diagnostic_silence_seconds=silence,
            last_activity_kind=kind,
            last_activity_age_seconds=age,
            remaining_deadline_seconds=remaining,
        )
