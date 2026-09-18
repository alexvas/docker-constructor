"""Presentation-neutral host materialization events."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock
from typing import Any, Callable


class HostPhase(StrEnum):
    RELEASE_ACQUISITION = "release_acquisition"
    LOCKED_ASSEMBLY = "locked_assembly"
    DERIVED_VALIDATION = "derived_validation"
    DOCKER_TRANSITION = "docker_transition"


class HostPhaseState(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class HostDiagnosticStream(StrEnum):
    STDOUT = "stdout"
    STDERR = "stderr"


class HostStep(StrEnum):
    """Closed set of observable operational steps in the host pipeline."""

    ARTIFACT_ACQUISITION = "artifact_acquisition"
    RELEASE_ACQUISITION = "release_acquisition"
    LOCK_WAIT = "lock_wait"
    CACHE_LOOKUP = "cache_lookup"
    CACHE_REUSE = "cache_reuse"
    STALE_STAGE_CLEANUP = "stale_stage_cleanup"
    CONTAINER_STARTUP = "container_startup"
    NPM_EXECUTION = "npm_execution"
    VALIDATION = "validation"
    PUBLICATION = "publication"
    DOCKER_TRANSITION = "docker_transition"


class HostStepState(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class HostLastActivityKind(StrEnum):
    """Closed last-activity kinds; never a wall-clock timestamp."""

    DIAGNOSTIC = "diagnostic"
    TRANSPORT_PROGRESS = "transport_progress"


class HostDiagnosticClassification(StrEnum):
    WARNING = "warning"
    ERROR = "error"
    RETRY = "retry"
    TIMEOUT = "timeout"
    STATUS = "status"


def _require_member(value: object, member_type: type, label: str) -> None:
    if not isinstance(value, member_type):
        raise TypeError(f"{label} must be a {member_type.__name__} member")


def _require_flag(value: object, label: str) -> None:
    if not isinstance(value, bool):
        raise TypeError(f"{label} must be a boolean")


def _require_int(value: object, label: str, *, minimum: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise TypeError(f"{label} must be a whole number >= {minimum}")


def _require_optional_int(value: object, label: str, *, minimum: int) -> None:
    if value is None:
        return
    _require_int(value, label, minimum=minimum)


@dataclass(frozen=True)
class HostPhaseEvent:
    phase: HostPhase
    state: HostPhaseState

    def __post_init__(self) -> None:
        if not isinstance(self.phase, HostPhase) or not isinstance(self.state, HostPhaseState):
            raise TypeError("host phase events require fixed phase and state members")


@dataclass(frozen=True)
class HostDiagnosticEvent:
    phase: HostPhase
    stream: HostDiagnosticStream
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.phase, HostPhase) or not isinstance(self.stream, HostDiagnosticStream):
            raise TypeError("host diagnostic events require fixed phase and stream members")
        if not isinstance(self.text, str):
            raise TypeError("host diagnostic text must be a string")


@dataclass(frozen=True)
class HostStepEvent:
    """Operational step transition; never a lifecycle event."""

    phase: HostPhase
    step: HostStep
    state: HostStepState
    expects_diagnostic_stream: bool

    def __post_init__(self) -> None:
        _require_member(self.phase, HostPhase, "phase")
        _require_member(self.step, HostStep, "step")
        _require_member(self.state, HostStepState, "state")
        _require_flag(self.expects_diagnostic_stream, "expects_diagnostic_stream")


@dataclass(frozen=True)
class HostTransportProgressEvent:
    """Observed streamed transport progress with cumulative whole bytes."""

    phase: HostPhase
    step: HostStep
    received_bytes: int

    def __post_init__(self) -> None:
        _require_member(self.phase, HostPhase, "phase")
        _require_member(self.step, HostStep, "step")
        _require_int(self.received_bytes, "received_bytes", minimum=0)


@dataclass(frozen=True)
class HostHeartbeatEvent:
    """Monotonic heartbeat facts with no wall-clock activity timestamp."""

    phase: HostPhase
    step: HostStep
    elapsed_seconds: int
    expects_diagnostic_stream: bool
    diagnostic_silence_seconds: int | None = None
    last_activity_kind: HostLastActivityKind | None = None
    last_activity_age_seconds: int | None = None
    remaining_deadline_seconds: int | None = None

    def __post_init__(self) -> None:
        _require_member(self.phase, HostPhase, "phase")
        _require_member(self.step, HostStep, "step")
        _require_int(self.elapsed_seconds, "elapsed_seconds", minimum=0)
        _require_flag(self.expects_diagnostic_stream, "expects_diagnostic_stream")
        _require_optional_int(
            self.remaining_deadline_seconds, "remaining_deadline_seconds", minimum=0
        )

        if not self.expects_diagnostic_stream:
            if self.diagnostic_silence_seconds is not None:
                raise TypeError(
                    "diagnostic silence is only valid when a diagnostic stream is expected"
                )
        else:
            _require_optional_int(
                self.diagnostic_silence_seconds, "diagnostic_silence_seconds", minimum=120
            )

        if (self.last_activity_kind is None) != (self.last_activity_age_seconds is None):
            raise TypeError(
                "last-activity kind and age must be jointly present or jointly absent"
            )
        if self.last_activity_kind is not None:
            _require_member(self.last_activity_kind, HostLastActivityKind, "last_activity_kind")
            _require_int(
                self.last_activity_age_seconds, "last_activity_age_seconds", minimum=1
            )


@dataclass(frozen=True)
class HostStructuredDiagnostic:
    """Structured safe diagnostic: URL-free text plus normalized host facts."""

    phase: HostPhase
    step: HostStep
    stream: HostDiagnosticStream
    classification: HostDiagnosticClassification
    text: str
    hostnames: tuple[str, ...] = ()
    logical_resource: str | None = None

    def __post_init__(self) -> None:
        _require_member(self.phase, HostPhase, "phase")
        _require_member(self.step, HostStep, "step")
        _require_member(self.stream, HostDiagnosticStream, "stream")
        _require_member(self.classification, HostDiagnosticClassification, "classification")
        if not isinstance(self.text, str):
            raise TypeError("structured diagnostic text must be a string")
        if not isinstance(self.hostnames, tuple) or not all(
            isinstance(hostname, str) for hostname in self.hostnames
        ):
            raise TypeError("normalized hostnames must be a tuple of strings")
        if self.logical_resource is not None and not isinstance(self.logical_resource, str):
            raise TypeError("logical resource must be a string or None")


HostOperationalEvent = (
    HostStepEvent
    | HostTransportProgressEvent
    | HostHeartbeatEvent
    | HostStructuredDiagnostic
)
HostBuildEvent = HostPhaseEvent | HostDiagnosticEvent | HostOperationalEvent
HostEventSink = Callable[[HostBuildEvent], None]


class GuardedHostEventSink:
    """Serialize presentation and permanently disable it after its first error.

    ``lock`` is injectable so tests can instrument the guarded serialization
    boundary; production callers omit it. The facade adapter installed as
    ``sink`` performs only bounded non-blocking admission and returns: it never
    renders, waits for a presentation barrier, flushes, or stops/joins
    presentation machinery while this lock is held.
    """

    def __init__(self, sink: HostEventSink, lock: Any | None = None) -> None:
        self._sink = sink
        self._lock = lock if lock is not None else Lock()
        self._enabled = True

    def __call__(self, event: HostBuildEvent) -> None:
        with self._lock:
            if not self._enabled:
                return
            try:
                self._sink(event)
            except Exception:
                self._enabled = False


def guard_sink(sink: HostEventSink | None) -> HostEventSink | None:
    if sink is None or isinstance(sink, GuardedHostEventSink):
        return sink
    return GuardedHostEventSink(sink)


def emit(sink: HostEventSink | None, event: HostBuildEvent) -> None:
    """Present an event; guarded facade/orchestration sinks cannot affect work."""
    if sink is not None:
        try:
            sink(event)
        except Exception:
            # Direct SDK callers remain insulated; orchestration uses the
            # stateful guard above so one failure also disables later calls.
            pass


class HostEventMailbox:
    """Bounded non-blocking producer-facing mailbox for facade presentation.

    Phase 1 owns only the prompt-returning admission boundary. Independent
    reliable/best-effort lane reservation, sequencing, omission accounting,
    the single presentation worker, and worker lifecycle are introduced with
    facade rendering; this boundary admits or drops without ever blocking a
    producer that holds ``GuardedHostEventSink`` serialization.
    """

    def __init__(self, capacity: int = 256) -> None:
        if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity <= 0:
            raise ValueError("mailbox capacity must be a positive integer")
        self._events: deque[HostBuildEvent] = deque()
        self._capacity = capacity
        self._lock = Lock()
        self.dropped = 0

    @property
    def capacity(self) -> int:
        return self._capacity

    def try_admit(self, event: HostBuildEvent) -> bool:
        """Admit without ever blocking; drop and report ``False`` otherwise.

        Lock acquisition is explicitly non-blocking: a producer that already
        holds ``GuardedHostEventSink`` serialization must return promptly even
        when the mailbox lock is contended, so contention is itself an
        admission failure. ``dropped`` counts only capacity drops observed
        while holding the admission lock; the contention path therefore touches
        no shared counter and stays non-blocking.
        """
        if not self._lock.acquire(blocking=False):
            return False
        try:
            if len(self._events) >= self._capacity:
                self.dropped += 1
                return False
            self._events.append(event)
            return True
        finally:
            self._lock.release()

    def drain(self) -> tuple[HostBuildEvent, ...]:
        with self._lock:
            events = tuple(self._events)
            self._events.clear()
            return events

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)


class HostEventEnqueueAdapter:
    """Facade sink adapter: bounded non-blocking enqueue and nothing else."""

    def __init__(self, mailbox: HostEventMailbox) -> None:
        self._mailbox = mailbox

    @property
    def mailbox(self) -> HostEventMailbox:
        return self._mailbox

    def __call__(self, event: HostBuildEvent) -> None:
        self._mailbox.try_admit(event)
