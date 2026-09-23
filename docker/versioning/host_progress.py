"""Presentation-neutral host materialization events."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from threading import Lock
from typing import Any, Callable
import weakref

from docker.versioning.logical_resource import require_approved_logical_resource


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


#: Fixed width of every opaque URL fingerprint, in hexadecimal characters.
URL_FINGERPRINT_LENGTH = 64

#: Lowercase hexadecimal alphabet accepted in a URL fingerprint.
URL_FINGERPRINT_ALPHABET = frozenset("0123456789abcdef")


def require_approved_url_fingerprints(value: object) -> None:
    """Validate the shared ephemeral URL-fingerprint contract.

    Fingerprints cross the structured DTO boundary, so they must be a tuple of
    strings that are each exactly :data:`URL_FINGERPRINT_LENGTH` lowercase
    hexadecimal characters.  Invalid values are rejected rather than
    lowercased, truncated, padded, or otherwise normalized so unsafe data
    cannot reach the fingerprint surface.
    """
    if not isinstance(value, tuple):
        raise TypeError("url_fingerprints must be a tuple")
    for fingerprint in value:
        if not isinstance(fingerprint, str):
            raise TypeError("url fingerprints must be strings")
        if len(fingerprint) != URL_FINGERPRINT_LENGTH or not set(
            fingerprint
        ) <= URL_FINGERPRINT_ALPHABET:
            raise ValueError(
                "url fingerprints must be exactly "
                f"{URL_FINGERPRINT_LENGTH} lowercase hexadecimal characters"
            )


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
    """Operational step transition; never a lifecycle event.

    ``logical_resource`` carries the closed safe asset name when the step is
    scoped to one reviewed artifact or Pi release asset (`acquisition` steps);
    it is ``None`` for non-asset steps such as npm execution or validation.
    Only approved reviewed-artifact, Pi-release, and assembler-container names
    are accepted.
    """

    phase: HostPhase
    step: HostStep
    state: HostStepState
    expects_diagnostic_stream: bool
    logical_resource: str | None = None

    def __post_init__(self) -> None:
        _require_member(self.phase, HostPhase, "phase")
        _require_member(self.step, HostStep, "step")
        _require_member(self.state, HostStepState, "state")
        _require_flag(self.expects_diagnostic_stream, "expects_diagnostic_stream")
        require_approved_logical_resource(self.logical_resource, "logical_resource")


@dataclass(frozen=True)
class HostTransportProgressEvent:
    """Observed streamed transport progress with cumulative whole bytes.

    ``logical_resource`` identifies the approved reviewed or Pi-release asset
    whose body yielded the chunks; it is ``None`` only for callers that do not
    scope the progress to one closed logical asset.
    """

    phase: HostPhase
    step: HostStep
    received_bytes: int
    logical_resource: str | None = None

    def __post_init__(self) -> None:
        _require_member(self.phase, HostPhase, "phase")
        _require_member(self.step, HostStep, "step")
        _require_int(self.received_bytes, "received_bytes", minimum=0)
        require_approved_logical_resource(self.logical_resource, "logical_resource")


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
    """Structured safe diagnostic: URL-free text plus normalized host facts.

    ``url_fingerprints`` carries the ordered tuple of ephemeral, fixed-width,
    session-keyed opaque fingerprints for complete URLs that were removed from
    ``text``.  Multiplicity is preserved.  Fingerprints are presentation
    identity only: they MUST NOT enter rendered text, retained tails, failure
    reports, persistence, evidence, or policy identity.
    """

    phase: HostPhase
    step: HostStep
    stream: HostDiagnosticStream
    classification: HostDiagnosticClassification
    text: str
    hostnames: tuple[str, ...] = ()
    logical_resource: str | None = None
    url_fingerprints: tuple[str, ...] = ()

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
        require_approved_url_fingerprints(self.url_fingerprints)
        require_approved_logical_resource(self.logical_resource, "logical resource")


@dataclass(frozen=True)
class HostFailureContext:
    """Structured, presentation-neutral context for one host failure report.

    It identifies the active phase and operational step, an optional safe
    logical asset, normalized host facts, a bounded exception-type chain, and
    the existing bounded redacted diagnostic tail.  It carries no output
    policy: the facade alone decides hostname display when rendering.
    """

    phase: HostPhase
    step: HostStep
    summary: str = "host operation failed"
    tail: str = ""
    logical_resource: str | None = None
    hostnames: tuple[str, ...] = ()
    exception_types: tuple[str, ...] = ()
    tail_stream: HostDiagnosticStream | None = None
    timeout_retained_context: bool = False

    def __post_init__(self) -> None:
        _require_member(self.phase, HostPhase, "phase")
        _require_member(self.step, HostStep, "step")
        if not isinstance(self.summary, str) or not self.summary:
            raise TypeError("summary must be a nonempty string")
        if not isinstance(self.tail, str):
            raise TypeError("tail must be a string")
        if self.tail_stream is not None:
            _require_member(self.tail_stream, HostDiagnosticStream, "tail_stream")
        if not isinstance(self.timeout_retained_context, bool):
            raise TypeError("timeout_retained_context must be a bool")
        if not isinstance(self.hostnames, tuple) or not all(
            isinstance(hostname, str) for hostname in self.hostnames
        ):
            raise TypeError("hostnames must be a tuple of strings")
        if not isinstance(self.exception_types, tuple) or not all(
            isinstance(name, str) for name in self.exception_types
        ):
            raise TypeError("exception_types must be a tuple of strings")
        require_approved_logical_resource(self.logical_resource, "logical_resource")


HostOperationalEvent = (
    HostStepEvent
    | HostTransportProgressEvent
    | HostHeartbeatEvent
    | HostStructuredDiagnostic
)
HostBuildEvent = HostPhaseEvent | HostDiagnosticEvent | HostOperationalEvent
HostEventSink = Callable[[HostBuildEvent], None]


#: Private carrier attribute for structural host failure context.
_HOST_FAILURE_CONTEXT_ATTR = "_host_failure_context"


class _FailureContextRegistry:
    """Ephemeral structural host failure context carried on the exception.

    Context is attached at the boundary where the active phase and step are
    known and looked up while an exception propagates, including through
    exception wrapping.  It is stored on the exception object itself (via
    ``object.__setattr__`` so the locked assembler's frozen value fields stay
    frozen); exceptions without an instance ``__dict__`` fall back to a weak
    registry.  Re-attaching an already-described exception is a no-op so the
    innermost (most specific) boundary wins.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._contexts: weakref.WeakKeyDictionary[
            BaseException, HostFailureContext
        ] = weakref.WeakKeyDictionary()

    def attach(
        self, reason: BaseException, context: HostFailureContext
    ) -> None:
        if not isinstance(reason, BaseException):
            raise TypeError("reason must be an exception")
        if not isinstance(context, HostFailureContext):
            raise TypeError("context must be a HostFailureContext")
        if getattr(reason, _HOST_FAILURE_CONTEXT_ATTR, None) is not None:
            return
        try:
            object.__setattr__(reason, _HOST_FAILURE_CONTEXT_ATTR, context)
            return
        except (AttributeError, TypeError):
            pass
        with self._lock:
            if reason not in self._contexts:
                try:
                    self._contexts[reason] = context
                except TypeError:
                    # Non-weak-referenceable exception: context is best-effort.
                    pass

    def lookup(self, reason: BaseException) -> HostFailureContext | None:
        seen: set[int] = set()
        current: BaseException | None = reason
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            found = getattr(current, _HOST_FAILURE_CONTEXT_ATTR, None)
            if isinstance(found, HostFailureContext):
                return found
            with self._lock:
                try:
                    found = self._contexts.get(current)
                except TypeError:
                    found = None
            if found is not None:
                return found
            current = getattr(current, "__cause__", None) or getattr(
                current, "__context__", None
            )
        return None


#: Process-wide registry shared by every host failure boundary.
FAILURE_CONTEXT_REGISTRY = _FailureContextRegistry()


def attach_host_failure(
    reason: BaseException,
    *,
    phase: HostPhase,
    step: HostStep,
    logical_resource: str | None = None,
    hostnames: tuple[str, ...] = (),
) -> None:
    """Attach structural phase/step context to a propagating failure.

    Called at the boundary where the active phase and operational step are
    known.  The context survives later wrapping because it is retrieved while
    walking the exception's cause/context chain.
    """
    FAILURE_CONTEXT_REGISTRY.attach(
        reason,
        HostFailureContext(
            phase=phase,
            step=step,
            logical_resource=logical_resource,
            hostnames=hostnames,
        ),
    )


def lookup_host_failure(reason: BaseException) -> HostFailureContext | None:
    """Return the nearest attached structural context for *reason*, if any."""
    return FAILURE_CONTEXT_REGISTRY.lookup(reason)


class InternalDirectHostEventSink:
    """Nominal marker for the facade's thread-safe mailbox enqueue adapter.

    This intentionally has no attribute-based or structural equivalent:
    request normalization authorizes direct admission only for instances of
    this internal type.
    """


class GuardedHostEventSink:
    """Serialize presentation and permanently disable it after its first error.

    ``lock`` is injectable so tests can instrument the guarded serialization
    boundary; production callers omit it. The facade adapter installed as
    ``sink`` performs only bounded non-blocking admission and returns: it never
    renders, waits for a presentation barrier, flushes, or stops/joins
    presentation machinery while this lock is held.
    """

    def __init__(
        self,
        sink: HostEventSink,
        lock: Any | None = None,
    ) -> None:
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
    if sink is None or isinstance(
        sink, (InternalDirectHostEventSink, GuardedHostEventSink)
    ):
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
