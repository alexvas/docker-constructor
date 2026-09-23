"""Facade presentation: modes, mailbox, worker, coalescing, and formatting.

Phase 9 of ``improve-host-build-observability``.  This module owns the single
facade presentation actor and the single ordered inbox every producer enqueues
into. It is deliberately presentation-neutral about *what* is drawn: all
terminal escapes and stream writes live behind :class:`HostPresentationRenderer`.
The worker and its pure state machine only decide grouping, cadence, hostname
policy, and ordering, so they can be exercised with a recording renderer and an
injected monotonic clock.

Nothing here prints directly, reads Docker/process/network state, or derives a
domain failure from human-readable text.  URL fingerprints and session keys
never enter rendered text; only a normalized hostname tuple is passed on when
the facade's hostname-display policy is enabled.
"""
from __future__ import annotations

import os
import threading
import time
import unicodedata
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from typing import Callable, Protocol, TextIO

from docker.versioning.diagnostic_identity import (
    DiagnosticCoalescer,
    DiagnosticDisposition,
    PresentationMode,
)
from docker.versioning.host_progress import (
    HostBuildEvent,
    HostDiagnosticEvent,
    HostDiagnosticStream,
    HostHeartbeatEvent,
    InternalDirectHostEventSink,
    HostLastActivityKind,
    HostPhase,
    HostPhaseEvent,
    HostPhaseState,
    HostStep,
    HostStepEvent,
    HostStepState,
    HostStructuredDiagnostic,
    HostTransportProgressEvent,
)

#: Seconds after operation start before the coordinator's first heartbeat.
FIRST_HEARTBEAT_SECONDS = 3.0

#: Upper bound on the interactive transient-status refresh interval.
INTERACTIVE_REFRESH_SECONDS = 1.0

#: Seconds between durable ordinary status lines in ``lines`` mode.
LINES_STATUS_INTERVAL_SECONDS = 30.0

#: Fixed monotonic window for ``lines``-mode exact-repeat summarization.
LINES_WINDOW_SECONDS = 1.0

#: Seconds without stdout/stderr before diagnostic silence is reported.
DIAGNOSTIC_SILENCE_SECONDS = 120.0

#: Fixed-width saturating omission counter ceiling.
OMISSION_COUNTER_LIMIT = 9999

#: Best-effort telemetry lane capacity (diagnostics, heartbeats, progress).
TELEMETRY_CAPACITY = 256

#: Bounded wait, in seconds, for shutdown acknowledgement or worker join.
WORKER_JOIN_SECONDS = 5.0


class HostPresentationMode(StrEnum):
    """Closed heartbeat presentation modes."""

    INTERACTIVE = "interactive"
    LINES = "lines"
    OFF = "off"


class PresentationSelection(StrEnum):
    """Whether a live host-event sink is authorized for the run."""

    LIVE = "live"
    NONE = "none"


@dataclass(frozen=True)
class PresentationPlan:
    """Immutable facade decision: mode plus whether a live sink exists."""

    mode: HostPresentationMode
    selection: PresentationSelection
    show_network_hosts: bool = False

    @property
    def live_sink(self) -> bool:
        return self.selection is PresentationSelection.LIVE


def select_presentation(
    host_heartbeat: str,
    *,
    text_output: bool,
    stderr_is_tty: bool,
    show_network_hosts: bool = False,
) -> PresentationPlan:
    """Resolve the facade presentation plan from the local output policy.

    JSON/structured output never creates a live sink.  Interactive text gets a
    live sink for every mode (``off`` still renders actionable diagnostics and
    lifecycle terminal states).  Noninteractive text gets a live sink only when
    ``lines`` was explicitly configured; otherwise failures retain bounded
    diagnostics only.
    """
    if not isinstance(host_heartbeat, str):
        raise TypeError("host_heartbeat must be a string")
    try:
        mode = HostPresentationMode(host_heartbeat)
    except ValueError as exc:
        raise ValueError(
            "host_heartbeat must be one of interactive, lines, or off"
        ) from exc
    if not text_output:
        return PresentationPlan(mode, PresentationSelection.NONE, show_network_hosts)
    if stderr_is_tty:
        return PresentationPlan(mode, PresentationSelection.LIVE, show_network_hosts)
    if mode is HostPresentationMode.LINES:
        return PresentationPlan(mode, PresentationSelection.LIVE, show_network_hosts)
    return PresentationPlan(mode, PresentationSelection.NONE, show_network_hosts)


def control_reservation() -> int:
    """Maximum supported build transcript while the consumer is stalled.

    Four reviewed artifact cache hits each emit acquisition start/terminal plus
    cache reuse (12); three Pi release assets emit start/terminal (6); seven
    locked-assembly steps emit start/terminal (14); and four facade phases emit
    start/terminal (8).  A failure may follow the same 40-control prefix and
    additionally enqueue the facade-owned final report, for 41 total controls.
    """
    artifact_cache_hit_controls = 4 * 3
    release_acquisition_controls = 3 * 2
    assembly_step_controls = 7 * 2
    phase_controls = 4 * 2
    final_failure_report_controls = 1
    return (
        artifact_cache_hit_controls
        + release_acquisition_controls
        + assembly_step_controls
        + phase_controls
        + final_failure_report_controls
    )


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------


def format_elapsed(seconds: int) -> str:
    """Human-readable whole-second duration."""
    if not isinstance(seconds, int) or isinstance(seconds, bool) or seconds < 0:
        raise ValueError("elapsed seconds must be a nonnegative whole number")
    if seconds < 60:
        return f"{seconds}s"
    minutes, remainder = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {remainder:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def _step_label(step: HostStep) -> str:
    return step.value.replace("_", " ")


def _phase_label(phase: HostPhase) -> str:
    return phase.value.replace("_", " ")


def _activity_label(kind: HostLastActivityKind) -> str:
    if kind is HostLastActivityKind.TRANSPORT_PROGRESS:
        return "byte progress"
    return "diagnostic"


def format_heartbeat(
    event: HostHeartbeatEvent,
    *,
    received_bytes: int | None = None,
) -> str:
    """Render presentation-neutral heartbeat facts without activity claims.

    Human-readable elapsed time is always present.  Diagnostic silence, last
    activity, remaining deadline, and observed byte progress appear only when
    the corresponding fact exists.  No wording ever claims that npm or its
    network is idle or downloading.
    """
    parts = [
        f"{_step_label(event.step)}: {format_elapsed(event.elapsed_seconds)}"
    ]
    if event.diagnostic_silence_seconds is not None:
        parts.append(
            f"no diagnostics for "
            f"{format_elapsed(event.diagnostic_silence_seconds)}"
        )
    if (
        event.last_activity_kind is not None
        and event.last_activity_age_seconds is not None
    ):
        parts.append(
            f"last {_activity_label(event.last_activity_kind)} "
            f"{format_elapsed(event.last_activity_age_seconds)} ago"
        )
    if received_bytes is not None:
        parts.append(f"{received_bytes} bytes received")
    if event.remaining_deadline_seconds is not None:
        parts.append(
            f"{format_elapsed(event.remaining_deadline_seconds)} remaining"
        )
    return ", ".join(parts)


def format_progress(event: HostTransportProgressEvent) -> str:
    """Render cumulative observed transport progress for one step."""
    return (
        f"{_step_label(event.step)}: {event.received_bytes} bytes received"
    )


def format_diagnostic_text(
    diagnostic: HostStructuredDiagnostic,
    *,
    show_network_hosts: bool,
) -> str:
    """Apply the facade hostname policy to one structured safe diagnostic.

    The already URL-free sanitized text is unchanged; hostnames are appended
    only when the display policy enables them.
    """
    text = diagnostic.text
    if show_network_hosts and diagnostic.hostnames:
        return f"{text} [{' '.join(diagnostic.hostnames)}]"
    return text


def format_failure_report(
    phase: HostPhase,
    step: HostStep,
    *,
    summary: str = "host operation failed",
    tail: str = "",
    tail_stream: HostDiagnosticStream | None = None,
    timeout_retained_context: bool = False,
    logical_resource: str | None = None,
    hostnames: tuple[str, ...] = (),
    show_network_hosts: bool = False,
    exception_types: tuple[str, ...] = (),
) -> str:
    """Render one contextual host failure, reusing the bounded redacted tail.

    The active phase and operational step always appear.  The retained
    diagnostic tail section is emitted only when nonempty, so a failure report
    never renders an empty section or replays unrelated output.
    """
    lines = [
        f"host materialization failed during {_phase_label(phase)} "
        f"({_step_label(step)}): {summary}"
    ]
    if logical_resource:
        lines.append(f"logical asset: {logical_resource}")
    if exception_types:
        lines.append(f"error types: {' -> '.join(exception_types)}")
    if show_network_hosts and hostnames:
        lines.append(f"network hosts: {', '.join(hostnames)}")
    if tail:
        lines.append("Retained diagnostics (may repeat live output):")
        lines.append(tail)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Mailbox
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PresentationFinalReport:
    """Facade-only immutable command for the actor's final durable report."""

    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text:
            raise TypeError("text must be a nonempty string")


class PresentationLane(StrEnum):
    """Independently bounded mailbox lanes."""

    CONTROL = "control"
    TELEMETRY = "telemetry"


@dataclass(frozen=True)
class AdmittedEvent:
    """One admitted mailbox event with its monotonic sequence number."""

    sequence: int
    lane: PresentationLane
    event: HostBuildEvent | PresentationFinalReport
    acknowledgement: threading.Event | None = None


_CONTROL_TYPES = (HostPhaseEvent, HostStepEvent, PresentationFinalReport)


def _is_control(event: object) -> bool:
    return isinstance(event, _CONTROL_TYPES)


def _is_barrier(event: object) -> bool:
    if isinstance(event, HostStepEvent):
        return event.state is not HostStepState.STARTED
    if isinstance(event, HostPhaseEvent):
        return event.state is not HostPhaseState.STARTED
    return False


def _is_diagnostic(event: object) -> bool:
    """Whether *event* is a diagnostic whose drop warrants an omission notice.

    Heartbeats and transport-progress updates are replaceable telemetry: losing
    them never means a diagnostic line was suppressed, so they must not inflate
    the diagnostic omission counter.
    """
    return isinstance(event, (HostStructuredDiagnostic, HostDiagnosticEvent))


def _consumes_omission_notice(event: object) -> bool:
    """Whether *event* advances past and renders a pending omission notice.

    A notice survives telemetry, progress, and started events, so it is only
    consumed by the next admitted diagnostic or a terminal step/phase state.
    """
    if _is_diagnostic(event):
        return True
    if isinstance(event, HostStepEvent) and event.state is not HostStepState.STARTED:
        return True
    if isinstance(event, HostPhaseEvent) and event.state is not HostPhaseState.STARTED:
        return True
    return False


def _supersede_key(event: object) -> tuple[object, ...] | None:
    """Telemetry replacement key, or ``None`` when the event is not replaceable."""
    if isinstance(event, HostHeartbeatEvent):
        return ("heartbeat", event.phase, event.step)
    if isinstance(event, HostTransportProgressEvent):
        return ("progress", event.phase, event.step)
    return None


class PresentationMailbox:
    """One bounded FIFO with independent control and telemetry budgets."""

    def __init__(
        self,
        capacity: int = TELEMETRY_CAPACITY,
        *,
        control_capacity: int | None = None,
        cancellation: threading.Event | None = None,
    ) -> None:
        if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity <= 0:
            raise ValueError("mailbox capacity must be a positive integer")
        if control_capacity is None:
            control_capacity = control_reservation()
        if not isinstance(control_capacity, int) or isinstance(control_capacity, bool) or control_capacity <= 0:
            raise ValueError("control capacity must be a positive integer")
        self._queue: deque[AdmittedEvent] = deque()
        self._capacity = capacity
        self._control_capacity = control_capacity
        self._control_count = 0
        self._telemetry_count = 0
        self._condition = threading.Condition(threading.Lock())
        self._sequence = 0
        self._closed = False
        self._aborted = False
        self._completed = threading.Event()
        self._worker_stopped = threading.Event()
        self._cancellation = cancellation or threading.Event()
        self._omissions = 0
        self._omission_uncertain = False
        self._omission_boundary: int | None = None
        self.dropped = 0
        self.superseded = 0

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def control_capacity(self) -> int:
        return self._control_capacity

    @property
    def emergency_signalled(self) -> bool:
        return self._aborted

    @property
    def admission_failed(self) -> bool:
        return self._aborted

    @property
    def worker_stopped(self) -> threading.Event:
        return self._worker_stopped

    @property
    def completed(self) -> threading.Event:
        return self._completed

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    def _next_sequence(self) -> int:
        value = self._sequence
        self._sequence += 1
        return value

    def admit_control(
        self,
        event: HostBuildEvent | PresentationFinalReport,
        *,
        acknowledgement: threading.Event | None = None,
    ) -> bool:
        with self._condition:
            if self._closed or self._aborted:
                return False
            if self._control_count >= self._control_capacity:
                self._aborted = True
                self._closed = True
                self._cancellation.set()
                self._condition.notify_all()
                return False
            self._queue.append(AdmittedEvent(self._next_sequence(), PresentationLane.CONTROL, event, acknowledgement))
            self._control_count += 1
            self._condition.notify()
            return True

    def _record_omission_locked(self) -> None:
        if self._omission_boundary is None:
            self._omission_boundary = self._sequence
        if self._omissions < OMISSION_COUNTER_LIMIT:
            self._omissions += 1
        else:
            self._omission_uncertain = True

    def admit_telemetry(self, event: HostBuildEvent) -> bool:
        diagnostic = _is_diagnostic(event)
        with self._condition:
            if self._closed or self._aborted:
                return False
            if self._telemetry_count >= self._capacity:
                self.dropped += 1
                if diagnostic:
                    self._record_omission_locked()
                return False
            self._queue.append(AdmittedEvent(self._next_sequence(), PresentationLane.TELEMETRY, event))
            self._telemetry_count += 1
            self._condition.notify()
            return True

    def try_admit(self, event: HostBuildEvent) -> bool:
        return self.admit_control(event) if _is_control(event) else self.admit_telemetry(event)

    def consume_omission_notice(self) -> str | None:
        return self.take_omission_notice(None)

    def take_omission_notice(self, sequence: int | None) -> str | None:
        with self._condition:
            if self._omissions == 0:
                return None
            if sequence is not None and self._omission_boundary is not None and sequence < self._omission_boundary:
                return None
            count = self._omissions
            uncertain = self._omission_uncertain
            self._omissions = 0
            self._omission_uncertain = False
            self._omission_boundary = None
        if uncertain:
            return f"[at least {count} diagnostics omitted]"
        return f"[{count} diagnostics omitted]"

    def take(self, timeout: float | None = None) -> AdmittedEvent | None:
        if timeout is not None and timeout < 0:
            timeout = 0.0
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while not self._queue and not self._closed and not self._aborted:
                if deadline is None:
                    self._condition.wait()
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    self._condition.wait(remaining)
            if self._aborted:
                return None
            if not self._queue:
                return None
            item = self._queue.popleft()
            if item.lane is PresentationLane.CONTROL:
                self._control_count -= 1
            else:
                self._telemetry_count -= 1
            return item

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def signal_emergency(self) -> None:
        with self._condition:
            self._aborted = True
            self._closed = True
            self._cancellation.set()
            self._condition.notify_all()

    def mark_completed(self) -> None:
        self._completed.set()

    def mark_worker_stopped(self) -> None:
        self._worker_stopped.set()
        self._completed.set()

    def drain(self) -> tuple[AdmittedEvent, ...]:
        with self._condition:
            items = tuple(self._queue)
            self._queue.clear()
            self._control_count = 0
            self._telemetry_count = 0
            return items


class HostEventEnqueueAdapter(InternalDirectHostEventSink):
    """Facade sink adapter: bounded non-blocking lane admission and nothing else."""

    def __init__(self, mailbox: PresentationMailbox) -> None:
        self._mailbox = mailbox

    @property
    def mailbox(self) -> PresentationMailbox:
        return self._mailbox

    def __call__(self, event: HostBuildEvent) -> None:
        self._mailbox.try_admit(event)


# --------------------------------------------------------------------------
# Renderer protocol
# --------------------------------------------------------------------------


class HostPresentationRenderer(Protocol):
    """Single-worker-owned renderer interface.

    All methods are called only from the presentation worker, so an
    implementation never needs cross-thread locking of its own mutable state.
    """

    def set_status(self, text: str) -> None: ...

    def set_slot(self, text: str) -> None: ...

    def clear_slot(self) -> None: ...

    def clear_status(self) -> None: ...

    def clear_all(self) -> None: ...

    def durable(self, text: str) -> None: ...

    def finalize_diagnostic(
        self, text: str, *, restore_status: str | None
    ) -> None: ...


# --------------------------------------------------------------------------
# Pure presentation state
# --------------------------------------------------------------------------


class HostPresentationState:
    """Deterministic, renderer-driven state for one presentation session.

    The state owns mode rules, coalescing deadlines, hostname policy, and the
    ordered finalization sequence.  It performs no scheduling and holds no
    thread; :class:`PresentationWorker` drives it from the mailbox and an
    injected monotonic clock.
    """

    def __init__(
        self,
        renderer: HostPresentationRenderer,
        *,
        mode: HostPresentationMode,
        show_network_hosts: bool = False,
        cancellation: threading.Event | None = None,
    ) -> None:
        if not isinstance(mode, HostPresentationMode):
            raise TypeError("mode must be a HostPresentationMode member")
        self._renderer = renderer
        self._cancellation = cancellation or threading.Event()
        self._mode = mode
        self._show = bool(show_network_hosts)
        self._coalescer = DiagnosticCoalescer(
            PresentationMode.INTERACTIVE
            if mode is HostPresentationMode.INTERACTIVE
            else PresentationMode.LINES
        )
        self._status: str | None = None
        self._status_since: float | None = None
        self._refresh_deadline: float | None = None
        self._window_deadline: float | None = None
        self._slot_applied_count = 0
        self._silence_announced = False
        self._received_bytes: int | None = None
        self._group_hostnames: tuple[str, ...] = ()
        self._rendering_failed = False

    @property
    def mode(self) -> HostPresentationMode:
        return self._mode

    @property
    def show_network_hosts(self) -> bool:
        return self._show

    @property
    def rendering_failed(self) -> bool:
        return self._rendering_failed

    @property
    def status(self) -> str | None:
        return self._status

    @property
    def next_deadline(self) -> float | None:
        deadlines = [
            value
            for value in (self._refresh_deadline, self._window_deadline)
            if value is not None
        ]
        return min(deadlines) if deadlines else None

    def _safe(self, action: Callable[[], None]) -> bool:
        if self._rendering_failed or self._cancellation.is_set():
            return False
        try:
            action()
            return True
        except Exception:
            # Renderer failure is secondary: disable rendering, discard
            # pending presentation state, and keep servicing control barriers.
            self._rendering_failed = True
            self._coalescer.finalize()
            self._group_hostnames = ()
            self._status = None
            self._refresh_deadline = None
            self._window_deadline = None
        return False

    def _durable_diagnostic(self, text: str) -> None:
        self._safe(partial(self._renderer.durable, text))

    def _finalize_diagnostic(
        self,
        text: str,
        *,
        restore_status: str | None,
    ) -> None:
        self._safe(
            partial(
                self._renderer.finalize_diagnostic,
                text,
                restore_status=restore_status,
            )
        )

    # -- diagnostics -------------------------------------------------------

    def admit_diagnostic(
        self,
        diagnostic: HostStructuredDiagnostic,
        *,
        now: float,
        omission_notice: str | None = None,
    ) -> None:
        previous_hostnames = self._group_hostnames
        if omission_notice is not None:
            # An omission notice finalizes the current group and is emitted
            # non-coalesced before the next admitted diagnostic.
            finalized = self._coalescer.finalize()
            self._reset_deadlines()
            if finalized is not None:
                if self._mode is HostPresentationMode.INTERACTIVE:
                    text = self._attach_hosts(finalized, previous_hostnames)
                    if text is not None:
                        self._finalize_diagnostic(text, restore_status=None)
                else:
                    attached = self._attach_hosts(finalized, previous_hostnames)
                    if attached is not None:
                        self._durable_diagnostic(attached)
            self._safe(partial(self._renderer.durable, omission_notice))
        decision = self._coalescer.admit(diagnostic)
        self._group_hostnames = diagnostic.hostnames
        if self._mode is HostPresentationMode.INTERACTIVE:
            if decision.finalized_text is not None:
                finalized = self._attach_hosts(
                    decision.finalized_text, previous_hostnames
                )
                if finalized is not None:
                    self._finalize_diagnostic(
                        finalized,
                        restore_status=self._status,
                    )
            slot = self._attach_hosts(decision.slot_text, diagnostic.hostnames)
            if decision.disposition is not DiagnosticDisposition.EXACT_REPEAT:
                if slot is not None:
                    self._safe(partial(self._renderer.set_slot, slot))
                    self._slot_applied_count = decision.admitted_count
                if self._refresh_deadline is None:
                    self._refresh_deadline = now + INTERACTIVE_REFRESH_SECONDS
        else:
            if decision.finalized_text is not None:
                attached = self._attach_hosts(
                    decision.finalized_text, previous_hostnames
                )
                if attached is not None:
                    self._durable_diagnostic(attached)
            durable = self._attach_hosts(decision.durable_text, diagnostic.hostnames)
            if durable is not None:
                self._durable_diagnostic(durable)
            if decision.disposition is not DiagnosticDisposition.EXACT_REPEAT:
                # The window starts when its group begins and stays fixed, so
                # continuous exact repeats cannot postpone the summary.
                self._window_deadline = now + LINES_WINDOW_SECONDS

    def _attach_hosts(
        self, text: str | None, hostnames: tuple[str, ...]
    ) -> str | None:
        if text is None:
            return text
        if self._show and hostnames:
            return f"{text} [{' '.join(hostnames)}]"
        return text

    def _reset_deadlines(self) -> None:
        self._refresh_deadline = None
        self._window_deadline = None
        self._slot_applied_count = 0

    # -- heartbeats and progress ------------------------------------------

    def observe_heartbeat(self, event: HostHeartbeatEvent, *, now: float) -> None:
        if self._mode is HostPresentationMode.OFF:
            return
        text = format_heartbeat(event, received_bytes=self._received_bytes)
        if self._mode is HostPresentationMode.INTERACTIVE:
            self._status = text
            self._safe(lambda: self._renderer.set_status(text))
            return
        silence = event.diagnostic_silence_seconds
        if silence is None or silence < DIAGNOSTIC_SILENCE_SECONDS:
            # Diagnostic output resumed (or never fell silent past the
            # threshold): a later silence cycle must be able to announce its
            # own transition.  The line interval is intentionally not
            # restarted here, so ordinary statuses keep their own cadence.
            self._silence_announced = False
        elif not self._silence_announced:
            self._silence_announced = True
            notice = (
                f"{_step_label(event.step)}: diagnostic silence "
                f"{format_elapsed(silence)}"
            )
            self._safe(lambda: self._renderer.durable(notice))
            self._status = text
            self._status_since = now
            return
        if (
            self._status_since is None
            or now - self._status_since >= LINES_STATUS_INTERVAL_SECONDS
        ):
            self._safe(lambda: self._renderer.durable(text))
            self._status = text
            self._status_since = now

    def observe_progress(
        self, event: HostTransportProgressEvent, *, now: float
    ) -> None:
        self._received_bytes = event.received_bytes
        if self._mode is HostPresentationMode.INTERACTIVE:
            text = format_progress(event)
            self._status = text
            self._safe(lambda: self._renderer.set_status(text))

    # -- lifecycle ---------------------------------------------------------

    def observe_step(
        self,
        event: HostStepEvent,
        *,
        now: float,
        omission_notice: str | None = None,
    ) -> None:
        if event.state is HostStepState.STARTED:
            # Each operational step owns its transport progress and its
            # diagnostic-silence transition: byte counts observed by an earlier
            # step must never surface in a heartbeat for a step that has not
            # observed progress yet, and a new step may announce its own
            # silence.
            self._received_bytes = None
            self._silence_announced = False
            # Each step's first heartbeat status must be eligible at that
            # step's three-second mark: a preceding step's cadence must not
            # suppress it.  The subsequent 30-second cadence and the
            # silence-transition restart continue to apply.
            self._status_since = None
            if self._mode is HostPresentationMode.INTERACTIVE:
                self._status = f"{_step_label(event.step)}…"
                self._safe(partial(self._renderer.set_status, self._status))
            elif self._mode is HostPresentationMode.LINES:
                self._safe(
                    partial(
                        self._renderer.durable,
                        f"{_step_label(event.step)}: started",
                    )
                )
            return
        self._terminalize(
            f"{_step_label(event.step)}: {event.state.value}",
            omission_notice=omission_notice,
        )

    def observe_phase(
        self,
        event: HostPhaseEvent,
        *,
        now: float,
        omission_notice: str | None = None,
    ) -> None:
        text = f"{_phase_label(event.phase)}: {event.state.value}"
        if event.state is HostPhaseState.STARTED:
            if self._mode is HostPresentationMode.INTERACTIVE:
                self._status = text
                self._safe(partial(self._renderer.set_status, text))
            else:
                # Lifecycle started states stay visible in noninteractive
                # presentations, matching the historical durable line.
                self._safe(partial(self._renderer.durable, text))
            return
        self._terminalize(text, omission_notice=omission_notice)

    def observe_diagnostic_event(
        self,
        event: HostDiagnosticEvent,
        *,
        now: float,
        omission_notice: str | None = None,
    ) -> None:
        """Render the legacy typed diagnostic through the durable path."""
        text = f"{_phase_label(event.phase)} [{event.stream.value}]: {event.text}"
        self._finalize_group(restore_status=None)
        if omission_notice is not None:
            self._safe(partial(self._renderer.durable, omission_notice))
        # Legacy diagnostics have no operational step and therefore cannot
        # structurally match a retained host-operation tail.
        self._durable_diagnostic(text)

    def _terminalize(
        self, text: str, *, omission_notice: str | None = None
    ) -> None:
        self._finalize_group(restore_status=None)
        if omission_notice is not None:
            # A pending notice is rendered as its own durable line after the
            # finalized group and before the terminal state.
            self._safe(partial(self._renderer.durable, omission_notice))
        self._status = None
        self._safe(lambda: self._renderer.clear_all())
        self._safe(lambda: self._renderer.durable(text))

    # -- deadlines and finalization ---------------------------------------

    def due(self, *, now: float) -> None:
        if self._mode is HostPresentationMode.INTERACTIVE:
            if self._refresh_deadline is None or now < self._refresh_deadline:
                return
            group = self._coalescer.group
            if group is not None and group.exact_count > self._slot_applied_count:
                text = self._attach_hosts(group.final_text(), self._group_hostnames)
                if text is not None:
                    self._safe(partial(self._renderer.set_slot, text))
                self._slot_applied_count = group.exact_count
            self._refresh_deadline = (
                now + INTERACTIVE_REFRESH_SECONDS if group is not None else None
            )
            return
        if self._window_deadline is None or now < self._window_deadline:
            return
        group = self._coalescer.group
        if group is not None and group.exact_count >= 2:
            text = self._attach_hosts(group.final_text(), self._group_hostnames)
            if text is not None:
                self._durable_diagnostic(text)
        # The window is complete: clearing the group here (not merely the
        # deadline) means terminal events and shutdown find nothing to replay,
        # and the next diagnostic starts a fresh group.
        self._coalescer.finalize()
        self._group_hostnames = ()
        self._window_deadline = None

    def finalize_group(self) -> None:
        self._finalize_group(restore_status=None)

    def _finalize_group(self, *, restore_status: str | None) -> None:
        finalized = self._coalescer.finalize()
        hostnames = self._group_hostnames
        self._reset_deadlines()
        if finalized is None:
            return
        text = self._attach_hosts(finalized, hostnames)
        if text is None:
            return
        self._finalize_diagnostic(text, restore_status=restore_status)

    def write_final_report(self, text: str) -> None:
        self._safe(partial(self._renderer.durable, text))

    def finish(self) -> None:
        self._status = None
        self._reset_deadlines()
        self._safe(lambda: self._renderer.clear_all())

    def cancel(self) -> None:
        self._rendering_failed = True
        self._status = None
        self._reset_deadlines()

    def discard(self) -> None:
        self._coalescer.finalize()
        self._status = None
        self._reset_deadlines()
        self._safe(lambda: self._renderer.clear_all())


# --------------------------------------------------------------------------
# Worker
# --------------------------------------------------------------------------


class PresentationWorker:
    """Single facade worker draining the mailbox and driving one state."""

    def __init__(
        self,
        mailbox: PresentationMailbox,
        state: HostPresentationState,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._mailbox = mailbox
        self._state = state
        self._clock = clock
        self._thread = threading.Thread(
            target=self._run, name="host-presentation", daemon=True
        )

    @property
    def state(self) -> HostPresentationState:
        return self._state

    @property
    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def start(self) -> None:
        self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    def _dispatch(self, event: object, omission_notice: str | None, now: float) -> None:
        if isinstance(event, HostStructuredDiagnostic):
            self._state.admit_diagnostic(
                event, now=now, omission_notice=omission_notice
            )
        elif isinstance(event, HostHeartbeatEvent):
            self._state.observe_heartbeat(event, now=now)
        elif isinstance(event, HostTransportProgressEvent):
            self._state.observe_progress(event, now=now)
        elif isinstance(event, HostStepEvent):
            self._state.observe_step(
                event, now=now, omission_notice=omission_notice
            )
        elif isinstance(event, HostPhaseEvent):
            self._state.observe_phase(
                event, now=now, omission_notice=omission_notice
            )
        elif isinstance(event, HostDiagnosticEvent):
            self._state.observe_diagnostic_event(
                event, now=now, omission_notice=omission_notice
            )
        elif isinstance(event, PresentationFinalReport):
            self._state.finalize_group()
            self._state.write_final_report(event.text)

    def _stop_on_emergency(self) -> bool:
        """Discard queued events and pending state once emergency is set.

        Checked before and after mailbox waiting, so an already-signalled
        emergency never emits a pending deadline summary and an emergency
        signalled while parked is observed promptly.  Cleanup stays on the
        worker thread, outside guarded producer callbacks, and runs before the
        worker-stopped acknowledgement.  Returns whether to terminate.
        """
        if not self._mailbox.emergency_signalled:
            return False
        self._mailbox.drain()
        self._state.cancel()
        return True

    def _run(self) -> None:
        try:
            while True:
                # Emergency may already be signalled (for example a failed
                # terminal or shutdown admission): discard before servicing a
                # deadline so no pending summary is rendered.
                if self._stop_on_emergency():
                    return
                now = self._clock()
                # Service an expired deadline before waiting again: a mailbox
                # that is never empty must not be able to starve the fixed
                # lines window or the interactive refresh.
                self._state.due(now=now)
                deadline = self._state.next_deadline
                timeout = None if deadline is None else max(0.0, deadline - now)
                item = self._mailbox.take(timeout)
                # Retain the emergency check after waiting so a stop signalled
                # while parked is observed immediately.
                if self._stop_on_emergency():
                    return
                if item is None:
                    if self._mailbox.closed:
                        notice = self._mailbox.consume_omission_notice()
                        self._state.finalize_group()
                        if notice is not None:
                            self._state.write_final_report(notice)
                        self._state.finish()
                        self._mailbox.mark_completed()
                        return
                    self._state.due(now=self._clock())
                    continue
                event = item.event
                # A pending omission notice survives telemetry, progress, and
                # started events; only the next admitted diagnostic or a
                # terminal lifecycle state whose sequence has passed the drop
                # boundary consumes and renders it.
                notice = (
                    self._mailbox.take_omission_notice(item.sequence)
                    if _consumes_omission_notice(event)
                    else None
                )
                self._dispatch(event, notice, self._clock())
                if _is_barrier(event) and item.acknowledgement is not None:
                    item.acknowledgement.set()
        finally:
            self._mailbox.mark_worker_stopped()


# --------------------------------------------------------------------------
# Facade session
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Production terminal renderer
# --------------------------------------------------------------------------


def _display_width(text: str) -> int:
    """Return terminal-cell width without counting combining marks."""
    width = 0
    for character in text:
        if unicodedata.combining(character):
            continue
        width += 2 if unicodedata.east_asian_width(character) in ("W", "F") else 1
    return width


def _clip_display(text: str, width: int) -> str:
    """Clip to fewer than *width* cells so the terminal cannot auto-wrap."""
    available = max(0, width - 1)
    if _display_width(text) <= available:
        return text
    if available == 0:
        return ""
    ellipsis = "…"
    target = max(0, available - _display_width(ellipsis))
    cells = 0
    clipped: list[str] = []
    for character in text:
        char_width = (
            0
            if unicodedata.combining(character)
            else 2 if unicodedata.east_asian_width(character) in ("W", "F") else 1
        )
        if cells + char_width > target:
            break
        clipped.append(character)
        cells += char_width
    return f"{''.join(clipped)}{ellipsis}"


class TerminalHostRenderer:
    """Plain-stream renderer for the single facade presentation worker.

    Durable records are newline-terminated and contain no terminal escapes, so
    redirected/CI ``lines`` output stays machine-readable.  Interactive status
    and the mutable diagnostic slot are the only replaceable output, drawn on
    one transient region guarded by ``\r`` and an erase-to-end-of-line
    sequence; nothing is emitted once the session is finalized.
    """

    _ERASE_LINE = "\r\x1b[K"

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        terminal_width: Callable[[], int] | None = None,
        cancellation: threading.Event | None = None,
    ) -> None:
        if stream is None:
            import sys

            stream = sys.stderr
        self._stream = stream
        self._terminal_width = terminal_width or self._stream_terminal_width
        self._cancellation = cancellation or threading.Event()
        self._status: str | None = None
        self._slot: str | None = None
        self._active = False

    def _stream_terminal_width(self) -> int:
        try:
            return os.get_terminal_size(self._stream.fileno()).columns
        except (AttributeError, OSError, TypeError, ValueError):
            return 80

    @property
    def cancellation(self) -> threading.Event:
        return self._cancellation

    def _write(self, text: str) -> bool:
        if self._cancellation.is_set():
            return False
        self._stream.write(text)
        if self._cancellation.is_set():
            return False
        self._stream.flush()
        return not self._cancellation.is_set()

    def _redraw(self) -> None:
        if self._cancellation.is_set():
            return
        parts = [part for part in (self._status, self._slot) if part]
        if not parts:
            if self._active and self._write(self._ERASE_LINE):
                self._active = False
            return
        rendered = _clip_display(
            "  ".join(parts), max(1, int(self._terminal_width()))
        )
        if self._write(f"{self._ERASE_LINE}{rendered}"):
            self._active = True

    def set_status(self, text: str) -> None:
        self._status = text
        self._redraw()

    def set_slot(self, text: str) -> None:
        self._slot = text
        self._redraw()

    def clear_slot(self) -> None:
        self._slot = None
        self._redraw()

    def clear_status(self) -> None:
        self._status = None
        self._redraw()

    def clear_all(self) -> None:
        self._status = None
        self._slot = None
        self._redraw()

    def durable(self, text: str) -> None:
        if self._cancellation.is_set():
            return
        if self._active:
            if not self._write(self._ERASE_LINE):
                return
            self._active = False
        self._write(f"{text}\n")

    def finalize_diagnostic(
        self, text: str, *, restore_status: str | None
    ) -> None:
        # Exactly one atomic sequence: clear the transient region, write the
        # finished diagnostic durably, drop the slot, then restore only a
        # still-applicable status.
        if self._cancellation.is_set():
            return
        self._slot = None
        self._status = None
        if self._active:
            if not self._write(self._ERASE_LINE):
                return
            self._active = False
        if not self._write(f"{text}\n"):
            return
        if restore_status is not None and not self._cancellation.is_set():
            self._status = restore_status
            self._redraw()


class HostPresentationSession:
    """Facade-owned actor session with capacity-independent bounded close."""

    def __init__(self, renderer: HostPresentationRenderer, plan: PresentationPlan, *, clock: Callable[[], float] = time.monotonic, completion_clock: Callable[[], float] = time.monotonic) -> None:
        if not isinstance(plan, PresentationPlan):
            raise TypeError("plan must be a PresentationPlan")
        if not plan.live_sink:
            raise ValueError("a presentation session requires a live sink plan")
        self._cancellation = (
            renderer.cancellation
            if isinstance(renderer, TerminalHostRenderer)
            else threading.Event()
        )
        self._mailbox = PresentationMailbox(cancellation=self._cancellation)
        self._state = HostPresentationState(
            renderer,
            mode=plan.mode,
            show_network_hosts=plan.show_network_hosts,
            cancellation=self._cancellation,
        )
        self._worker = PresentationWorker(self._mailbox, self._state, clock=clock)
        self._adapter = HostEventEnqueueAdapter(self._mailbox)
        self._sink = self._adapter
        self._completion_clock = completion_clock
        self._shutdown_lock = threading.Lock()
        self._shutdown_deadline: float | None = None
        self._shutdown_result: bool | None = None
        self._worker.start()

    @property
    def sink(self) -> HostEventEnqueueAdapter:
        return self._sink

    @property
    def worker(self) -> PresentationWorker:
        return self._worker

    @property
    def mailbox(self) -> PresentationMailbox:
        return self._mailbox

    @property
    def state(self) -> HostPresentationState:
        return self._state

    def submit_final_report(self, text: str) -> bool:
        return self._mailbox.admit_control(PresentationFinalReport(text))

    def shutdown(self) -> bool:
        with self._shutdown_lock:
            if self._shutdown_deadline is None:
                self._shutdown_deadline = self._completion_clock() + WORKER_JOIN_SECONDS
                self._mailbox.close()
            deadline = self._shutdown_deadline
            if self._shutdown_result is not None:
                return self._shutdown_result
        remaining = max(0.0, deadline - self._completion_clock())
        completed = self._mailbox.completed.wait(remaining)
        remaining = max(0.0, deadline - self._completion_clock())
        if completed and remaining > 0:
            self._worker.join(remaining)
        healthy = completed and not self._worker.is_alive and not self._mailbox.admission_failed
        if not healthy:
            self._state.cancel()
            self._mailbox.signal_emergency()
        with self._shutdown_lock:
            self._shutdown_result = healthy
        return healthy


__all__ = [
    "AdmittedEvent",
    "DIAGNOSTIC_SILENCE_SECONDS",
    "FIRST_HEARTBEAT_SECONDS",
    "HostEventEnqueueAdapter",
    "HostPresentationMode",
    "HostPresentationRenderer",
    "HostPresentationSession",
    "HostPresentationState",
    "INTERACTIVE_REFRESH_SECONDS",
    "LINES_STATUS_INTERVAL_SECONDS",
    "LINES_WINDOW_SECONDS",
    "OMISSION_COUNTER_LIMIT",
    "PresentationFinalReport",
    "PresentationLane",
    "PresentationMailbox",
    "PresentationPlan",
    "PresentationSelection",
    "PresentationWorker",
    "TELEMETRY_CAPACITY",
    "TerminalHostRenderer",
    "WORKER_JOIN_SECONDS",
    "control_reservation",
    "format_diagnostic_text",
    "format_elapsed",
    "format_failure_report",
    "format_heartbeat",
    "format_progress",
    "select_presentation",
]
