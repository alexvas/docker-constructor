"""Prepared ownership of one direct child and its captured byte streams.

All pipes use the filesystem's single terminal owner. A prepared per-process
continuation retains resources until the internal reader barriers and winning
release outcomes are published. No domain policy or external-reader adoption.
"""

from __future__ import annotations

import logging
import math
import os
import queue
import select
import subprocess
import threading
import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Callable, Protocol, Self

from docker.filesystem.cleanup import CleanupFailures
from docker.filesystem.streams import OwnedStream, _bind_prepared, _prepare_stream


def _new_event() -> threading.Event:
    return threading.Event()


def _new_lock() -> Any:
    return threading.Lock()


def _make_continuation(action: Callable[[], None]) -> threading.Thread:
    return threading.Thread(target=action, name="owned-process-cleanup", daemon=True)


def _policy_pause(seconds: float) -> None:
    time.sleep(seconds)


class ProcessStream(Enum):
    STDOUT = auto()
    STDERR = auto()


class StreamOutcome(Enum):
    PENDING = auto()
    EOF = auto()
    READ_FAILED = auto()
    ABORTED = auto()


class FailureKind(Enum):
    ORDINARY = auto()
    UNEXPECTED = auto()
    INTERRUPTION = auto()


class FailurePhase(Enum):
    SETUP = auto()
    READ = auto()
    JOIN = auto()
    RELEASE = auto()
    WAIT = auto()
    ACTION = auto()
    CALLBACK = auto()
    REPORTER = auto()
    COORDINATION = auto()


@dataclass(frozen=True)
class FailureInfo:
    error: BaseException
    kind: FailureKind
    phase: FailurePhase
    stream: ProcessStream | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.error, BaseException):
            raise TypeError("failure error must be BaseException")
        if not isinstance(self.kind, FailureKind) or not isinstance(
            self.phase, FailurePhase
        ):
            raise TypeError("failure kind and phase must be declared enums")
        if self.stream is not None and not isinstance(self.stream, ProcessStream):
            raise TypeError("failure stream must be ProcessStream or None")
        if (self.kind is FailureKind.INTERRUPTION) != (
            not isinstance(self.error, Exception)
        ):
            raise ValueError("failure kind cannot fabricate or demote control flow")
        if (
            self.phase
            in (FailurePhase.READ, FailurePhase.CALLBACK, FailurePhase.REPORTER)
            and self.stream is None
        ):
            raise ValueError("this failure phase requires a stream")
        if (
            self.phase
            in (FailurePhase.WAIT, FailurePhase.ACTION, FailurePhase.COORDINATION)
            and self.stream is not None
        ):
            raise ValueError("this failure phase cannot identify a stream")


def _validate_failures(
    failures: tuple[FailureInfo, ...], *, action: bool = False
) -> None:
    if not isinstance(failures, tuple):
        raise TypeError("failures must be a tuple of FailureInfo")
    for failure in failures:
        if not isinstance(failure, FailureInfo):
            raise TypeError("failures must contain FailureInfo")
        FailureInfo.__post_init__(failure)
        if action and (
            failure.phase is not FailurePhase.ACTION or failure.stream is not None
        ):
            raise ValueError("action failures require ACTION/None context")


@dataclass(frozen=True)
class LifecycleResult:
    complete: bool
    failures: tuple[FailureInfo, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.complete, bool):
            raise TypeError("complete must be boolean")
        _validate_failures(self.failures)


@dataclass(frozen=True)
class StopContext:
    """Read-only main-child observation and an absolute action deadline."""

    reason: str
    returncode: int | None
    deadline: float


@dataclass(frozen=True)
class ActionResult:
    """Request settlement is separate from settlement of owned local resources."""

    done: bool
    cleanup_complete: bool
    failures: tuple[FailureInfo, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.done, bool) or not isinstance(
            self.cleanup_complete, bool
        ):
            raise TypeError("action progress must be boolean")
        _validate_failures(self.failures, action=True)


class ManagedAction(Protocol):
    """Supported implementations are nonblocking at every protocol boundary.

    prepare establishes readiness before main acquisition. start/observe/cancel
    must return promptly; cancel is a once-only cleanup request, not a wait.
    An auxiliary child must have its own default-policy OwnedProcess and a
    deadline/grace allocation fitting StopContext.deadline. Never capture the
    main Popen or borrowed pipes in an action.
    """

    def prepare(self) -> None: ...
    def start(self, context: StopContext) -> None: ...
    def observe(self) -> ActionResult: ...
    def cancel(self, deadline: float) -> None: ...


@dataclass(frozen=True)
class StopStage:
    seconds: float
    action: ManagedAction | None = None
    wait_child: bool = False
    run_on_normal: bool = False
    local_stop_before: bool = False


@dataclass(frozen=True)
class StopPolicy:
    stages: tuple[StopStage, ...]
    local_cleanup_seconds: float
    drain: bool = False


def _validate_policy(policy: StopPolicy | None, total: float) -> None:
    if policy is None:
        return
    if not isinstance(policy, StopPolicy):
        raise TypeError("stop_policy must be StopPolicy")
    if not isinstance(policy.stages, tuple) or not isinstance(policy.drain, bool):
        raise TypeError("policy stages must be a tuple and drain must be boolean")
    caps = [policy.local_cleanup_seconds]
    actions = []
    for stage in policy.stages:
        if not isinstance(stage, StopStage):
            raise TypeError("policy stages must be StopStage")
        if not all(
            isinstance(v, bool)
            for v in (stage.wait_child, stage.run_on_normal, stage.local_stop_before)
        ):
            raise TypeError("stage flags must be boolean")
        caps.append(stage.seconds)
        if stage.action is not None:
            if any(stage.action is a for a in actions):
                raise ValueError("a managed action must have one stage owner")
            actions.append(stage.action)
            if not all(
                callable(getattr(stage.action, n, None))
                for n in ("prepare", "start", "observe", "cancel")
            ):
                raise TypeError(
                    "managed action must implement prepare/start/observe/cancel"
                )
    if any(
        not isinstance(v, (int, float))
        or isinstance(v, bool)
        or not math.isfinite(v)
        or v <= 0
        for v in caps
    ):
        raise ValueError(
            "stage caps and local cleanup reserve must be finite and positive"
        )
    if math.fsum(caps) > total:
        raise ValueError(
            "stage caps and local cleanup reserve exceed total shutdown budget"
        )


def _fallback_callback_diagnostic() -> None:
    logging.getLogger(__name__).warning("Process callback diagnostic reporting failed")


@dataclass(frozen=True)
class ProcessResult:
    stdout: bytes
    stderr: bytes
    returncode: int | None
    timed_out: bool
    lifecycle: LifecycleResult
    stream_outcomes: tuple[tuple[ProcessStream, StreamOutcome], ...] = ()


class CleanupIncomplete(OSError):
    """Controlled incomplete context cleanup, carrying observation access."""

    def __init__(self, cleanup: OwnedProcess) -> None:
        super().__init__("process shutdown incomplete")
        self.cleanup = cleanup


def _record(
    failures: CleanupFailures,
    error: BaseException,
    *,
    ordinary: tuple[type[Exception], ...] = (OSError,),
) -> None:
    def raise_error() -> None:
        raise error

    failures.run(raise_error, ordinary=ordinary)


def _entries(failures: CleanupFailures | _FailureLedger) -> tuple[BaseException, ...]:
    items = (
        *failures.interruptions,
        *failures.unexpected,
        *(
            (failures.original_primary,)
            if failures.original_primary is not None
            else ()
        ),
        *failures.ordinary_failures,
    )
    unique: list[BaseException] = []
    for item in items:
        if not any(item is other for other in unique):
            unique.append(item)
    return tuple(unique)


class _FailureLedger:
    """Shared precedence plus occurrence context; publication never edits errors."""

    def __init__(self, primary: BaseException | None = None) -> None:
        self.shared = CleanupFailures(primary)
        self.contexts: dict[tuple[int, int], FailureInfo] = {}
        if primary is not None:
            rank = 2 if isinstance(primary, Exception) else 0
            self.contexts[(id(primary), rank)] = FailureInfo(
                primary,
                FailureKind.ORDINARY if rank == 2 else FailureKind.INTERRUPTION,
                FailurePhase.COORDINATION,
            )

    @property
    def original_primary(self) -> BaseException | None:
        return self.shared.original_primary

    @property
    def interruptions(self) -> tuple[BaseException, ...]:
        return self.shared.interruptions

    @property
    def unexpected(self) -> tuple[Exception, ...]:
        return self.shared.unexpected

    @property
    def ordinary_failures(self) -> tuple[Exception, ...]:
        return self.shared.ordinary_failures

    def add(self, failure: FailureInfo) -> None:
        # Returned helper classification is explicit, never inferred from type.
        rank = {
            FailureKind.INTERRUPTION: 0,
            FailureKind.UNEXPECTED: 1,
            FailureKind.ORDINARY: 3,
        }[failure.kind]
        if (id(failure.error), rank) in self.contexts:
            return
        _record(
            self.shared,
            failure.error,
            ordinary=(Exception,) if failure.kind is FailureKind.ORDINARY else (),
        )
        self.contexts[(id(failure.error), rank)] = failure

    def record(
        self,
        error: BaseException,
        *,
        ordinary: tuple[type[Exception], ...] = (OSError,),
        phase: FailurePhase = FailurePhase.COORDINATION,
        stream: ProcessStream | None = None,
    ) -> None:
        classified = CleanupFailures(None)
        _record(classified, error, ordinary=ordinary)
        kind = (
            FailureKind.INTERRUPTION
            if classified.interruptions
            else FailureKind.UNEXPECTED
            if classified.unexpected
            else FailureKind.ORDINARY
        )
        self.add(FailureInfo(error, kind, phase, stream))

    def primary(
        self, error: BaseException, *, phase: FailurePhase = FailurePhase.COORDINATION
    ) -> None:
        if (
            any(error is item for item in (*self.interruptions, *self.unexpected))
            or error is self.original_primary
        ):
            return
        original = self.original_primary
        if original is None and isinstance(error, Exception):
            previous = self.shared
            self.shared = CleanupFailures(error)
            self.contexts[(id(error), 2)] = FailureInfo(
                error, FailureKind.ORDINARY, phase
            )
            for item in (*previous.interruptions, *previous.unexpected):
                _record(self.shared, item, ordinary=())
            for item in previous.ordinary_failures:
                _record(self.shared, item, ordinary=(Exception,))
        else:
            self.record(error, ordinary=(Exception,), phase=phase)

    def result(self, complete: bool) -> LifecycleResult:
        ordered: list[FailureInfo] = []
        seen: set[int] = set()
        groups = (
            self.interruptions,
            self.unexpected,
            (self.original_primary,) if self.original_primary is not None else (),
            self.ordinary_failures,
        )
        for rank, group in enumerate(groups):
            for error in group:
                if id(error) not in seen:
                    seen.add(id(error))
                    ordered.append(self.contexts[(id(error), rank)])
        return LifecycleResult(complete, tuple(ordered))


def _outcome(failures: _FailureLedger, complete: bool) -> LifecycleResult:
    return failures.result(complete)


class OwnedProcess:
    """Launch-centered process ownership; callers never receive pipe authority."""

    _owners: list[OwnedStream]
    _ledger: _FailureLedger
    _status: LifecycleResult
    _cancel: threading.Event
    _stop: threading.Event
    _policy: StopPolicy | None
    _action_prepared: list[bool]
    _action_started: list[bool]
    _action_cancelled: list[bool]
    _action_results: list[ActionResult]
    _action_failures: list[_FailureLedger]
    _observed_error_kinds: list[set[str]]
    _request: threading.Event
    _ready: threading.Event
    _finished: threading.Event
    _phase_done: threading.Event
    _stopped: list[threading.Event]
    _setup_done: threading.Event
    _lock: Any
    _collection_claim: Any
    _output: queue.Queue[tuple[int, bytes]]
    _tails: list[bytearray]
    _outcomes: list[StreamOutcome]
    _reader_errors: list[BaseException | None]
    _queued: list[int]
    _reporter: Callable[..., None] | None
    _limit: int
    _grace: float
    _deadline: float | None
    _proc: Any
    _known_returncode: int | None
    _readers: list[threading.Thread | None]
    _started: list[bool]
    _fds: list[int | None]
    _descriptorless: list[bool]
    _acquired: list[bool]
    _transferred: list[bool]
    _continuation: threading.Thread
    _idle_stop: bool
    _duplicate_pipe: bool

    def __init__(self) -> None:
        raise TypeError("use OwnedProcess.launch to acquire a child")

    @classmethod
    def launch(
        cls,
        argv: Any,
        *,
        grace_seconds: float = 1.0,
        deadline_seconds: float | None = None,
        capture_limit: int = 65536,
        factory: Callable[..., Any] = subprocess.Popen,
        popen_kwargs: dict[str, Any] | None = None,
        on_launched: Callable[[], None] | None = None,
        stop_policy: StopPolicy | None = None,
        reporter: Callable[..., None] | None = None,
    ) -> Self:
        if (
            not isinstance(argv, (list, tuple))
            or not argv
            or not all(isinstance(s, str) and s for s in argv)
        ):
            raise ValueError("argv must be a non-empty string sequence")
        if (
            not isinstance(grace_seconds, (int, float))
            or not math.isfinite(grace_seconds)
            or grace_seconds <= 0
        ):
            raise ValueError("grace_seconds must be finite and positive")
        if deadline_seconds is not None and (
            not isinstance(deadline_seconds, (int, float))
            or not math.isfinite(deadline_seconds)
            or deadline_seconds <= 0
        ):
            raise ValueError("deadline_seconds must be finite and positive")
        if (
            not isinstance(capture_limit, int)
            or isinstance(capture_limit, bool)
            or capture_limit < 0
        ):
            raise ValueError("capture_limit must be a nonnegative integer")
        if not callable(factory) or (
            on_launched is not None and not callable(on_launched)
        ):
            raise TypeError("factory and callback must be callable")
        if reporter is not None and not callable(reporter):
            raise TypeError("reporter must be callable")
        options = dict(popen_kwargs or {})
        if any(
            k in options
            for k in (
                "stdout",
                "stderr",
                "stdin",
                "text",
                "universal_newlines",
                "encoding",
                "errors",
            )
        ):
            raise ValueError("only binary captured stdout/stderr are supported")
        _validate_policy(stop_policy, float(grace_seconds))
        self = cls.__new__(cls)
        # Everything that can allocate synchronization/ownership/continuation
        # capacity is prepared before Popen. No existing-process entry point.
        self._owners = [
            _prepare_stream(label="stdout"),
            _prepare_stream(label="stderr"),
        ]
        self._cancel = _new_event()
        self._stop = _new_event()
        self._request = _new_event()
        self._ready = _new_event()
        self._finished = _new_event()
        self._phase_done = _new_event()
        self._stopped = [_new_event(), _new_event()]
        self._setup_done = _new_event()
        self._lock = _new_lock()
        self._collection_claim = _new_lock()
        self._output = queue.Queue(maxsize=32)
        self._tails = [bytearray(), bytearray()]
        self._outcomes = [StreamOutcome.PENDING, StreamOutcome.PENDING]
        self._reader_errors = [None, None]
        self._queued = [0, 0]
        self._reporter = reporter
        self._collected = False
        self._collection_delivered = False
        self._dispatch_closed = False
        self._limit = capture_limit
        self._grace = float(grace_seconds)
        self._policy = stop_policy
        self._reason = "cancel"
        self._action_prepared = [False] * len(stop_policy.stages) if stop_policy else []
        self._action_started = [False] * len(self._action_prepared)
        self._action_cancelled = [False] * len(self._action_prepared)
        self._action_results = [ActionResult(True, True) for _ in self._action_prepared]
        self._action_failures = [_FailureLedger() for _ in self._action_prepared]
        self._observed_error_kinds = [set() for _ in self._action_prepared]
        self._deadline = deadline_seconds
        self._launched_at = time.monotonic()
        self._proc = None
        self._known_returncode = None
        self._readers = [None, None]
        self._started = [False, False]
        self._fds = [None, None]
        self._descriptorless = [False, False]
        self._acquired = [False, False]
        self._transferred = [False, False]
        self._duplicate_pipe = False
        self._supervising = False
        self._streaming = False
        self._requested = False
        self._idle_stop = False
        self._reaped = False
        self._terminate_attempted = False
        self._late_reap_recorded = False
        self._timed_out = False
        self._delivered = False
        self._ledger = _FailureLedger()
        self._status = LifecycleResult(False)
        self._continuation = _make_continuation(self._continue_cleanup)
        prepared = False
        try:
            self._continuation.start()
            if not self._ready.wait(self._grace):
                raise RuntimeError("cleanup continuation did not become ready")
            if stop_policy:
                for index, stage in enumerate(stop_policy.stages):
                    if stage.action is not None:
                        # Claim before preparation: partial readiness must also
                        # retain its one action owner on failure.
                        self._action_prepared[index] = True
                        self._action_results[index] = ActionResult(False, False)
                        if stage.action.prepare() is not None:
                            raise TypeError(
                                "action prepare must acknowledge readiness with None"
                            )
            prepared = True
            self._proc = factory(
                argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options
            )
        except BaseException as exc:
            self._idle_stop = True
            if self._ready.is_set():
                self._reaped = True  # no main child was acquired
                self._ledger.record(exc, phase=FailurePhase.SETUP)
                result = self._shutdown(reason="setup", deliver=False)
                if prepared:
                    self._raise_interruption(result)
                    return self
                self._raise_result(result)
            else:
                self._request.set()
                failures = CleanupFailures(exc)
                if self._continuation.ident is not None:
                    failures.run(
                        lambda: self._continuation.join(self._grace), ordinary=()
                    )
                result = failures.complete()
                if result:
                    raise result
            raise
        # Escrow both results before any fallible transfer validation. Binding
        # uses preallocated slots and cannot construct a replacement authority.
        first_pipe = None
        for index, name in enumerate(("stdout", "stderr")):
            pipe = getattr(self._proc, name, None)
            if index == 0:
                first_pipe = pipe
            elif pipe is not None and pipe is first_pipe:
                # An incompatible factory must not give one stream two owners.
                self._duplicate_pipe = True
                continue
            if pipe is not None:
                _bind_prepared(self._owners[index], pipe)
                self._acquired[index] = True
        for index in range(2):
            try:
                self._transfer(index)
            except BaseException as exc:
                self._ledger.record(
                    exc, phase=FailurePhase.SETUP, stream=tuple(ProcessStream)[index]
                )
        setup_stream = None
        launch_callback = False
        try:
            errors = _entries(self._ledger)
            if errors:
                raise errors[0]
            for index in range(2):
                setup_stream = tuple(ProcessStream)[index]
                self._readers[index] = self._make_reader(index)
                self._configure_reader(index)
                self._start_reader(index)
            setup_stream = None
            self._start_supervisor()
            if on_launched:
                launch_callback = True
                on_launched()
            self._setup_done.set()
        except BaseException as exc:
            self._setup_done.set()
            with self._lock:
                if launch_callback:
                    self._ledger.primary(exc, phase=FailurePhase.SETUP)
                elif not any(exc is error for error in _entries(self._ledger)):
                    self._ledger.record(
                        exc, phase=FailurePhase.SETUP, stream=setup_stream
                    )
            result = self._shutdown(reason="setup", deliver=False)
            self._raise_interruption(result)
        return self

    def _transfer(self, index: int) -> None:
        if index == 1 and self._duplicate_pipe:
            raise TypeError("captured streams must be independent")
        if not self._acquired[index]:
            raise OSError("missing captured stream")
        stream = self._owners[index].stream
        if not callable(getattr(stream, "close", None)) or not callable(
            getattr(stream, "read", None)
        ):
            raise TypeError("captured stream must expose read and close")
        self._transferred[index] = True

    def _make_reader(self, index: int) -> threading.Thread:
        return threading.Thread(
            target=self._reader,
            args=(index,),
            name=f"owned-process-reader-{index}",
            daemon=True,
        )

    def _configure_reader(self, index: int) -> None:
        stream = self._owners[index].stream
        try:
            fd = stream.fileno()
        except AttributeError, OSError, ValueError:
            self._descriptorless[index] = True
            return
        os.set_blocking(fd, False)
        self._fds[index] = fd  # borrowed number, never a release authority

    def _start_reader(self, index: int) -> None:
        thread = self._readers[index]
        assert thread is not None
        try:
            thread.start()
        finally:
            # Includes a start seam that starts and then raises.
            self._started[index] = thread.ident is not None

    def _start_supervisor(self) -> None:
        # The prepared continuation also supervises deadlines, without a new
        # fallible post-launch worker or separate cleanup authority.
        self._launched_at = time.monotonic()
        self._supervising = True

    def _poll_child(self) -> int | None:
        poll = getattr(self._proc, "poll", None)
        if callable(poll):
            return poll()
        # Legacy injected clients implement bounded wait rather than poll.
        # This remains coordinator-owned observation, never adapter control.
        try:
            return self._proc.wait(timeout=0)
        except subprocess.TimeoutExpired:
            return None

    def _read_chunk(self, index: int) -> bytes | None:
        fd = self._fds[index]
        if fd is None:
            data = self._owners[index].stream.read(65536)
            # Legacy descriptor-less captured factories may serve text. The
            # generic output/callback boundary still supplies bytes only.
            return data.encode() if isinstance(data, str) else data
        ready, _, _ = select.select([fd], [], [], 0.02)
        if not ready:
            return None
        try:
            return os.read(fd, 65536)
        except BlockingIOError:
            return None

    def _read(self, index: int) -> None:
        exited_at: float | None = None
        first = True
        while not self._cancel.is_set() or (first and not self._delivered):
            # Each tracked reader may finish its initial acquisition even if
            # its sibling failed immediately after startup. No read begins for
            # a result already delivered to the caller.
            first = False
            try:
                chunk = self._read_chunk(index)
            except BaseException as exc:
                with self._lock:
                    self._reader_errors[index] = exc
                raise
            if chunk == b"":
                self._set_outcome(
                    index,
                    StreamOutcome.ABORTED
                    if self._cancel.is_set()
                    else StreamOutcome.EOF,
                )
                return
            if chunk:
                with self._lock:
                    tail = self._tails[index]
                    tail.extend(chunk)
                    if len(tail) > self._limit:
                        del tail[: len(tail) - self._limit]
                while True:
                    try:
                        with self._lock:
                            if self._dispatch_closed or self._delivered:
                                break
                            self._output.put_nowait((index, chunk))
                            self._queued[index] += 1
                        break
                    except queue.Full:
                        # Bounded draining never waits for consumer progress
                        # after stopping. Buffered assigned chunks are not raw
                        # tail replay and may finish before terminal delivery.
                        if self._stop.is_set():
                            break
                        self._cancel.wait(0.02)
            if self._poll_child() is not None:
                if exited_at is None:
                    exited_at = time.monotonic()
                # Descendants holding write ends cannot keep these readers
                # alive forever; output already available is drained first.
                if chunk is None and time.monotonic() - exited_at >= self._grace:
                    return

    def _set_outcome(self, index: int, outcome: StreamOutcome) -> None:
        with self._lock:
            if self._outcomes[index] is StreamOutcome.PENDING:
                self._outcomes[index] = outcome

    def _reader(self, index: int) -> None:
        try:
            self._read(index)
        except BaseException as exc:
            is_read = self._reader_errors[index] is exc
            self._set_outcome(
                index, StreamOutcome.READ_FAILED if is_read else StreamOutcome.ABORTED
            )
            with self._lock:
                self._ledger.record(
                    exc,
                    phase=FailurePhase.READ if is_read else FailurePhase.WAIT,
                    stream=tuple(ProcessStream)[index] if is_read else None,
                )
            # Launch still owns reader construction/start tracking. Do not
            # release its sibling or publish cleanup until setup has settled.
            self._setup_done.wait()
            self._shutdown(reason="reader")
        finally:
            self._set_outcome(index, StreamOutcome.ABORTED)
            self._stopped[index].set()

    def _join_reader(self, index: int, timeout: float) -> None:
        thread = self._readers[index]
        if self._started[index] and thread is not None:
            thread.join(timeout)

    def _safe(self, index: int) -> bool:
        # The explicit completion barrier, not a timed join or is_alive(), is
        # the authority to release a descriptor-backed reader's stream.
        return not self._started[index] or self._stopped[index].is_set()

    def _action(
        self,
        action: Callable[[], Any],
        *,
        ordinary: tuple[type[Exception], ...],
        phase: FailurePhase = FailurePhase.COORDINATION,
        stream: ProcessStream | None = None,
    ) -> None:
        failures = CleanupFailures(None)
        failures.run(action, ordinary=ordinary)
        with self._lock:
            for error in _entries(failures):
                self._ledger.record(
                    error, ordinary=ordinary, phase=phase, stream=stream
                )

    def _release_safe(self) -> None:
        for index in range(2):
            owner = self._owners[index]
            if self._acquired[index] and not owner.released:

                def release(index: int = index, owner: OwnedStream = owner) -> None:
                    # Descriptor-less compatibility streams may need release
                    # to unblock read. Unknown/real descriptors remain retained.
                    if (
                        self._safe(index)
                        or self._descriptorless[index]
                        or not callable(getattr(owner.stream, "fileno", None))
                    ):
                        owner.close()

                self._action(
                    release,
                    ordinary=(OSError,),
                    phase=FailurePhase.RELEASE,
                    stream=tuple(ProcessStream)[index],
                )

    def _wait_child(self, timeout: float) -> None:
        self._known_returncode = self._proc.wait(timeout=timeout)
        self._reaped = True

    def _reap(self, end: float) -> None:
        self._action(self._terminate_if_live, ordinary=(OSError,))

        def wait_for_termination() -> None:
            try:
                self._wait_child(max(0, (end - time.monotonic()) / 2))
            except subprocess.TimeoutExpired:
                # Expiry of the terminate grace is a normal transition to kill,
                # not a cleanup failure if the subsequent reap succeeds.
                pass

        self._action(wait_for_termination, ordinary=(OSError,), phase=FailurePhase.WAIT)
        if not self._reaped:
            self._action(lambda: self._proc.kill(), ordinary=(OSError,))
            self._action(
                lambda: self._wait_child(max(0, end - time.monotonic())),
                ordinary=(OSError, subprocess.TimeoutExpired),
                phase=FailurePhase.WAIT,
            )

    def _record_action_failure(self, index: int, failure: FailureInfo) -> None:
        with self._lock:
            self._action_failures[index].add(failure)
            self._ledger.add(failure)
            previous = self._action_results[index]
            self._action_results[index] = ActionResult(
                previous.done,
                previous.cleanup_complete,
                self._action_failures[index].result(False).failures,
            )

    def _record_action_error(self, index: int, error: BaseException) -> None:
        classified = _FailureLedger()
        classified.record(error, phase=FailurePhase.ACTION)
        self._record_action_failure(index, classified.result(False).failures[0])

    def _observe_action(self, index: int) -> ActionResult:
        assert self._policy is not None
        action = self._policy.stages[index].action
        assert action is not None
        previous = self._action_results[index]
        if previous.done and previous.cleanup_complete:
            return previous
        observation = None
        try:
            observation = action.observe()
            if not isinstance(observation, ActionResult):
                raise TypeError("action observation must return ActionResult")
            ActionResult.__post_init__(observation)
            for failure in observation.failures:
                self._record_action_failure(index, failure)
        except BaseException as exc:
            # Preserve the original raised interruption before probing any
            # malformed fields, which can themselves execute user code.
            classified = _FailureLedger()
            classified.record(
                exc,
                ordinary=(OSError,) if observation is None else (),
                phase=FailurePhase.ACTION,
            )
            failure = classified.result(False).failures[0]
            with self._lock:
                first_kind = failure.kind.name not in self._observed_error_kinds[index]
                self._observed_error_kinds[index].add(failure.kind.name)
            if first_kind:
                self._record_action_failure(index, failure)
            recovered: list[BaseException] = []
            try:
                supplied = getattr(observation, "failures", ())
            except BaseException as field_error:
                recovered.append(field_error)
                supplied = ()
            if isinstance(supplied, (tuple, list)):
                items = (
                    tuple.__iter__(supplied)
                    if isinstance(supplied, tuple)
                    else list.__iter__(supplied)
                )
                for item in items:
                    try:
                        error = (
                            item
                            if isinstance(item, BaseException)
                            else getattr(item, "error", None)
                        )
                    except BaseException as field_error:
                        error = field_error
                    if isinstance(error, BaseException):
                        recovered.append(error)
            for error in recovered:
                kind = (
                    FailureKind.UNEXPECTED
                    if isinstance(error, Exception)
                    else FailureKind.INTERRUPTION
                )
                self._record_action_failure(
                    index, FailureInfo(error, kind, FailurePhase.ACTION)
                )
            observation = ActionResult(False, False)
        with self._lock:
            result = ActionResult(
                observation.done,
                observation.cleanup_complete,
                self._action_failures[index].result(False).failures,
            )
            self._action_results[index] = result
        return result

    def _observe_actions(self) -> None:
        for index, prepared in enumerate(self._action_prepared):
            if prepared:
                self._observe_action(index)

    def _cancel_action(self, index: int, end: float) -> None:
        assert self._policy is not None
        action = self._policy.stages[index].action
        if (
            action is not None
            and self._action_prepared[index]
            and not self._action_cancelled[index]
        ):
            self._action_cancelled[index] = True
            failures = CleanupFailures(None)

            def cancel() -> None:
                if action.cancel(end) is not None:
                    raise TypeError("action cancel must return None")

            failures.run(cancel, ordinary=(OSError,))
            for error in _entries(failures):
                self._record_action_error(index, error)

    def _cancel_actions(self, end: float) -> None:
        for index in range(len(self._action_prepared)):
            self._cancel_action(index, end)

    def _run_policy(self, end: float) -> None:
        policy = self._policy
        if policy is None:
            return
        for index, stage in enumerate(policy.stages):
            if self._reason == "natural" and not stage.run_on_normal:
                continue
            # Stage caps never borrow the local reserve or restart the total.
            stage_end = min(
                time.monotonic() + stage.seconds, end - policy.local_cleanup_seconds
            )
            if time.monotonic() >= stage_end:
                break
            with self._lock:
                if self._delivered:
                    break
                self._action_started[index] = True
            if stage.local_stop_before:
                self._action(self._terminate_if_live, ordinary=(OSError,))
            if stage.action is not None:
                # Child observation and the independent cleanup request are
                # separate actions. A failed poll retains shared authority but
                # cannot suppress cleanup of externally owned work. None is
                # unknown returncode, not proof of a running or exited child.
                returncode: int | None = None

                def observe_returncode() -> None:
                    nonlocal returncode
                    returncode = self._poll_child()

                self._action(
                    observe_returncode, ordinary=(OSError,), phase=FailurePhase.WAIT
                )

                def start_action(action: ManagedAction = stage.action) -> None:
                    context = StopContext(self._reason, returncode, stage_end)
                    if action.start(context) is not None:
                        raise TypeError("action start must return None")

                failures = CleanupFailures(None)
                failures.run(start_action, ordinary=(OSError,))
                for error in _entries(failures):
                    self._record_action_error(index, error)
                if _entries(failures):
                    self._cancel_action(index, end)
                    continue
            while time.monotonic() < stage_end:
                observation = (
                    self._observe_action(index)
                    if stage.action
                    else ActionResult(True, True)
                )
                if observation.failures:
                    break
                if observation.done:
                    if not stage.wait_child:
                        break
                    child_done: bool | None = None

                    def observe_child() -> None:
                        nonlocal child_done
                        child_done = self._poll_child() is not None

                    self._action(
                        observe_child, ordinary=(OSError,), phase=FailurePhase.WAIT
                    )
                    # None denotes failed observation, not a live child. Advance
                    # rather than retrying a broken poll or losing the worker.
                    if child_done is None or child_done:
                        break
                _policy_pause(min(0.005, max(0, stage_end - time.monotonic())))
            self._cancel_action(index, end)

    def _terminate_if_live(self) -> None:
        if not self._terminate_attempted and self._poll_child() is None:
            self._terminate_attempted = True
            self._proc.terminate()

    def _complete(self) -> bool:
        return (
            self._reaped
            and all(self._safe(i) for i in range(2))
            and all(not self._acquired[i] or self._owners[i].released for i in range(2))
            and all(
                not prepared or (result.done and result.cleanup_complete)
                for prepared, result in zip(self._action_prepared, self._action_results)
            )
        )

    def _publish(self, complete: bool) -> None:
        with self._lock:
            self._status = _outcome(self._ledger, complete)
        if complete:
            self._finished.set()

    def _cancel_readers_locked(self) -> None:
        self._cancel.set()
        for index in range(2):
            if self._acquired[index] and self._outcomes[index] is StreamOutcome.PENDING:
                self._outcomes[index] = StreamOutcome.ABORTED

    def _continue_cleanup(self) -> None:
        self._ready.set()
        while not self._request.wait(0.01):
            if (
                self._supervising
                and self._deadline is not None
                and time.monotonic() - self._launched_at >= self._deadline
            ):
                self._timed_out = True
                self._shutdown(reason="deadline")  # reentrant request
        if self._idle_stop and not self._requested:
            self._finished.set()
            return
        end = self._cleanup_end if self._requested else time.monotonic() + self._grace
        if not self._idle_stop:
            self._action(lambda: self._run_policy(end), ordinary=())
        with self._lock:
            self._cancel_readers_locked()
        self._cancel_actions(end)
        # Joins are protected independent actions. Reaping always follows,
        # including when a join defect/interruption occurred.
        for index in range(2):
            self._action(
                lambda index=index: self._join_reader(
                    index, max(0, end - time.monotonic()) / 3
                ),
                ordinary=(),
                phase=FailurePhase.JOIN,
                stream=tuple(ProcessStream)[index],
            )
        if self._proc is not None:
            self._reap(end)
        self._observe_actions()
        self._release_safe()
        # Cancellation can wake a prepared idle action asynchronously. Use
        # the remaining local reserve to observe settlement, not a fresh
        # budget or a false incomplete result merely due to scheduling.
        while (
            any(
                prepared and not (result.done and result.cleanup_complete)
                for prepared, result in zip(self._action_prepared, self._action_results)
            )
            or any(
                self._descriptorless[i]
                and self._owners[i].released
                and not self._safe(i)
                for i in range(2)
            )
        ) and time.monotonic() < end:
            # Descriptor-less close may wake its reader asynchronously. Confirm
            # that stop barrier within the existing reserve, without another
            # close/join/reap request or a new budget.
            _policy_pause(min(0.001, max(0, end - time.monotonic())))
            self._observe_actions()
            self._release_safe()
        self._publish(self._complete())
        self._phase_done.set()
        # Prepared continuation retains exactly these owners/readers until the
        # barriers hold. No GC release, registry, re-adoption or close retry.
        while not self._finished.is_set():
            for index in range(2):
                if not self._safe(index):
                    self._stopped[index].wait(0.01)
            self._release_safe()
            if not self._reaped:
                try:
                    self._wait_child(0)
                except BaseException as exc:
                    # Poll expiry is observation, not a new shutdown budget.
                    # Retain the first new late observation defect once, rather
                    # than accumulating failures forever for a broken factory.
                    if (
                        not isinstance(exc, subprocess.TimeoutExpired)
                        and not self._late_reap_recorded
                    ):
                        with self._lock:
                            if not any(
                                exc is error for error in _entries(self._ledger)
                            ):
                                self._ledger.record(exc, phase=FailurePhase.WAIT)
                        self._late_reap_recorded = True
                    time.sleep(0.01)
            self._observe_actions()
            self._publish(self._complete())
            _policy_pause(0.001)

    @property
    def cleanup_status(self) -> LifecycleResult:
        with self._lock:
            return self._status

    @property
    def returncode(self) -> int | None:
        code = getattr(self._proc, "returncode", None)
        return self._known_returncode if code is None else code

    def shutdown(self, *, primary: BaseException | None = None) -> LifecycleResult:
        return self._shutdown(primary=primary, reason="cancel")

    def _shutdown(
        self, *, primary: BaseException | None = None, reason: str, deliver: bool = True
    ) -> LifecycleResult:
        with self._lock:
            previously_delivered = self._delivered
            if primary is not None:
                self._ledger.primary(primary)
            if not self._requested:
                self._requested = True
                self._reason = reason
                self._cleanup_end = time.monotonic() + self._grace
                self._shutdown_end = self._cleanup_end + 0.05
                self._stop.set()
                if self._policy is None or not self._policy.drain:
                    self._cancel_readers_locked()
                self._request.set()
        if (
            threading.current_thread() is self._continuation
            or threading.current_thread() in self._readers
        ):
            return self.cleanup_status
        # One initial coordination budget, not a new termination sequence.
        interruption = None
        while True:
            try:
                self._phase_done.wait(max(0, self._shutdown_end - time.monotonic()))
                break
            except BaseException as exc:
                # An interruption while awaiting coordination belongs to this
                # operation too. Retain it before further independent cleanup
                # and resume only the original remaining allowance.
                with self._lock:
                    self._ledger.record(exc, ordinary=())
                if not isinstance(exc, Exception) and interruption is None:
                    interruption = exc
                if isinstance(exc, Exception) or time.monotonic() >= self._shutdown_end:
                    break
        with self._lock:
            # Publication and phase notification are separate steps. Refresh
            # this observation even after delivery: a paused continuation may
            # not yet have published control flow encountered by this call.
            # Previously delivered immutable results remain unchanged.
            self._status = _outcome(self._ledger, self._status.complete)
            if deliver:
                self._delivered = True
            result = self._status
        if result.complete:

            def join_continuation() -> None:
                nonlocal interruption
                try:
                    self._continuation.join(
                        max(0, self._shutdown_end - time.monotonic())
                    )
                except BaseException as exc:
                    if not isinstance(exc, Exception) and interruption is None:
                        interruption = exc
                    raise

            self._action(join_continuation, ordinary=(), phase=FailurePhase.JOIN)
            with self._lock:
                self._status = _outcome(self._ledger, self._status.complete)
                result = self._status
        if deliver and interruption is not None:
            # Suppress already-delivered authority, not control flow encountered
            # by this call. Before first delivery retain global first-interruption
            # precedence; afterwards raise the first interruption from this call.
            pending = (
                result
                if not previously_delivered
                else LifecycleResult(
                    result.complete,
                    tuple(f for f in result.failures if f.error is interruption),
                )
            )
            self._raise_interruption(pending)
        return result

    def wait_cleanup(self, timeout: float) -> LifecycleResult:
        if (
            not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout < 0
        ):
            raise ValueError("timeout must be finite and nonnegative")
        end = time.monotonic() + timeout
        self._finished.wait(timeout)
        if (
            self._finished.is_set()
            and threading.current_thread() is not self._continuation
        ):
            self._continuation.join(max(0, end - time.monotonic()))
        return self.cleanup_status

    def _raise_interruption(self, result: LifecycleResult) -> None:
        if result.failures and result.failures[0].kind is FailureKind.INTERRUPTION:
            error = result.failures[0].error
            with self._lock:
                self._delivered = True
            try:
                setattr(error, "cleanup", self)
            except BaseException as observation_error:
                # Observation only, never output. Preserve first interruption
                # even for error classes refusing normal attribute assignment.
                with self._lock:
                    self._ledger.record(observation_error, ordinary=())
                    self._status = _outcome(self._ledger, self._status.complete)
                try:
                    object.__setattr__(error, "cleanup", self)
                except BaseException as marker_error:
                    with self._lock:
                        self._ledger.record(marker_error, ordinary=())
                        self._status = _outcome(self._ledger, self._status.complete)
            raise error

    def _raise_result(self, result: LifecycleResult) -> None:
        error = result.failures[0].error if result.failures else None
        if error is None and not result.complete:
            error = CleanupIncomplete(self)
            with self._lock:
                self._ledger.record(error)
                self._status = _outcome(self._ledger, self._status.complete)
        if error is not None:
            with self._lock:
                self._delivered = True
            if not isinstance(error, Exception):
                self._raise_interruption(self.cleanup_status)
            if not result.complete:
                setattr(error, "cleanup", self)
            raise error

    def _invoke_callback(
        self,
        callback: Callable[..., Any],
        args: tuple[Any, ...],
        kind: str,
        identity: ProcessStream,
        failures: _FailureLedger,
    ) -> None:
        def interrupted(error: BaseException, phase: FailurePhase) -> None:
            failure = FailureInfo(error, FailureKind.INTERRUPTION, phase, identity)
            failures.add(failure)
            with self._lock:
                self._ledger.add(failure)

        try:
            callback(*args)
        except Exception as exc:
            try:
                if self._reporter is None:
                    logging.getLogger(__name__).warning("Process callback failed")
                else:
                    self._reporter(exc, stream=identity, callback_kind=kind)
            except Exception:
                try:
                    _fallback_callback_diagnostic()
                except Exception:
                    pass
                except BaseException as interruption:
                    interrupted(interruption, FailurePhase.REPORTER)
            except BaseException as interruption:
                interrupted(interruption, FailurePhase.REPORTER)
        except BaseException as interruption:
            interrupted(interruption, FailurePhase.CALLBACK)

    def _snapshot_locked(self, lifecycle: LifecycleResult) -> ProcessResult:
        return ProcessResult(
            bytes(self._tails[0]),
            bytes(self._tails[1]),
            self.returncode,
            self._timed_out,
            lifecycle,
            tuple(zip(ProcessStream, self._outcomes)),
        )

    def _collect(
        self,
        callback: Callable[[ProcessStream, bytes], Any] | None,
        on_stream_end: Callable[[ProcessStream, StreamOutcome], Any] | None = None,
    ) -> ProcessResult:
        failures = _FailureLedger()
        notified = [False, False]

        def notify(*, force: bool = False) -> None:
            for index, identity in enumerate(ProcessStream):
                with self._lock:
                    terminal = self._outcomes[index]
                    ready = force or (
                        self._queued[index] == 0
                        and (terminal is not StreamOutcome.ABORTED or self._safe(index))
                    )
                if (
                    not notified[index]
                    and ready
                    and terminal is not StreamOutcome.PENDING
                ):
                    notified[index] = True
                    if on_stream_end is not None:
                        self._invoke_callback(
                            on_stream_end,
                            (identity, terminal),
                            "end",
                            identity,
                            failures,
                        )

        boundary = FailurePhase.COORDINATION
        try:
            while (
                not all(
                    self._stopped[i].is_set() or not self._started[i] for i in range(2)
                )
                or not self._output.empty()
            ):
                try:
                    index, chunk = self._output.get(timeout=0.02)
                except queue.Empty:
                    notify()
                    if self._stop.is_set() or _entries(failures):
                        break
                    continue
                if callback is not None and not self._delivered:
                    self._invoke_callback(
                        callback,
                        (tuple(ProcessStream)[index], chunk),
                        "chunk",
                        tuple(ProcessStream)[index],
                        failures,
                    )
                with self._lock:
                    self._queued[index] -= 1
                notify()
                if _entries(failures):
                    break
            notify()
            boundary = FailurePhase.WAIT
            while (
                not _entries(failures)
                and not self._stop.is_set()
                and self._poll_child() is None
            ):
                time.sleep(0.01)
        except BaseException as exc:
            failures.record(exc, phase=boundary)
        with self._lock:
            for failure in failures.result(False).failures:
                self._ledger.add(failure)
        result = self._shutdown(
            reason="natural"
            if not _entries(failures) and not self._stop.is_set()
            else self._reason,
            deliver=False,
        )
        with self._lock:
            self._dispatch_closed = True
        # Flush only the already assigned bounded spool, never a raw snapshot
        # tail. An interrupted invocation attempts ends but no further chunks.
        while not _entries(failures) and not (
            result.failures and result.failures[0].kind is FailureKind.INTERRUPTION
        ):
            try:
                index, chunk = self._output.get_nowait()
            except queue.Empty:
                break
            if callback is not None and not self._delivered:
                self._invoke_callback(
                    callback,
                    (tuple(ProcessStream)[index], chunk),
                    "chunk",
                    tuple(ProcessStream)[index],
                    failures,
                )
            with self._lock:
                self._queued[index] -= 1
            notify()
        for index in range(2):
            if self._acquired[index]:
                self._set_outcome(index, StreamOutcome.ABORTED)
        notify(force=True)
        with self._lock:
            for failure in failures.result(False).failures:
                self._ledger.add(failure)
            if not result.complete and not _entries(self._ledger):
                self._ledger.record(CleanupIncomplete(self))
            self._status = _outcome(self._ledger, self._status.complete)
            result = self._status
            snapshot = self._snapshot_locked(result)
            self._collection_delivered = True
            self._delivered = True
        self._raise_interruption(result)
        return snapshot

    def _claim_collection(self) -> None:
        # A once-only latch, never a serialization queue. Losing callers do
        # not wait on the winner or on lifecycle-state publication locks.
        if not self._collection_claim.acquire(blocking=False):
            raise RuntimeError("process collection attempt already claimed")
        self._collected = True

    def capture(self) -> ProcessResult:
        self._claim_collection()
        return self._collect(None)

    def stream(
        self,
        callback: Callable[[ProcessStream, bytes], Any],
        *,
        on_stream_end: Callable[[ProcessStream, StreamOutcome], Any] | None = None,
    ) -> ProcessResult:
        if not callable(callback) or (
            on_stream_end is not None and not callable(on_stream_end)
        ):
            raise TypeError("callbacks must be callable")
        self._claim_collection()
        self._streaming = True
        return self._collect(callback, on_stream_end)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> None:
        reason = "context"
        if self._collection_delivered:
            try:
                self._shutdown(reason=reason)
            except BaseException:
                if exc is not None and not isinstance(exc, Exception):
                    raise exc
                raise
            return
        if exc is None:

            def observe_exit() -> None:
                nonlocal reason
                if self._poll_child() is not None:
                    reason = "natural"

            # Reason selection is cleanup observation, not an escape hatch
            # around the coordinator. Failure retains the context stop plan.
            self._action(observe_exit, ordinary=(OSError,), phase=FailurePhase.WAIT)
        self._raise_result(self._shutdown(primary=exc, reason=reason))


__all__ = [
    "OwnedProcess",
    "ProcessResult",
    "ProcessStream",
    "StreamOutcome",
    "LifecycleResult",
    "CleanupIncomplete",
    "FailureInfo",
    "FailureKind",
    "FailurePhase",
    "StopPolicy",
    "StopStage",
    "StopContext",
    "ManagedAction",
    "ActionResult",
]
