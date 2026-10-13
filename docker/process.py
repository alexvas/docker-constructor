"""Prepared ownership of one direct child and its captured byte streams.

All pipes use the filesystem's single terminal owner. A prepared per-process
continuation retains resources until the internal reader barriers and winning
release outcomes are published. No domain policy or external-reader adoption.
"""
from __future__ import annotations

import math
import os
import queue
import select
import subprocess
import threading
import time
from dataclasses import dataclass
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
    error: BaseException | None = None


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
        if not all(isinstance(v, bool) for v in (stage.wait_child, stage.run_on_normal, stage.local_stop_before)):
            raise TypeError("stage flags must be boolean")
        caps.append(stage.seconds)
        if stage.action is not None:
            if any(stage.action is a for a in actions):
                raise ValueError("a managed action must have one stage owner")
            actions.append(stage.action)
            if not all(callable(getattr(stage.action, n, None)) for n in ("prepare", "start", "observe", "cancel")):
                raise TypeError("managed action must implement prepare/start/observe/cancel")
    if any(not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or v <= 0 for v in caps):
        raise ValueError("stage caps and local cleanup reserve must be finite and positive")
    if math.fsum(caps) > total:
        raise ValueError("stage caps and local cleanup reserve exceed total shutdown budget")


@dataclass(frozen=True)
class CleanupResult:
    complete: bool
    error: BaseException | None
    failures: tuple[BaseException, ...]
    actions: tuple[ActionResult, ...] = ()

    @property
    def shutdown_incomplete(self) -> bool:
        return not self.complete


@dataclass(frozen=True)
class ProcessResult:
    stdout: bytes
    stderr: bytes
    returncode: int | None
    timed_out: bool
    cleanup: CleanupResult
    callback_errors: tuple[BaseException, ...] = ()


class CleanupIncomplete(OSError):
    """Controlled incomplete context cleanup, carrying observation access."""

    def __init__(self, cleanup: OwnedProcess) -> None:
        super().__init__("process shutdown incomplete")
        self.cleanup = cleanup


def _record(
    failures: CleanupFailures, error: BaseException,
    *, ordinary: tuple[type[Exception], ...] = (OSError,),
) -> None:
    def raise_error() -> None:
        raise error
    failures.run(raise_error, ordinary=ordinary)


def _entries(failures: CleanupFailures) -> tuple[BaseException, ...]:
    items = (*failures.interruptions, *failures.unexpected,
             *((failures.original_primary,) if failures.original_primary else ()),
             *failures.ordinary_failures)
    unique: list[BaseException] = []
    for item in items:
        if not any(item is other for other in unique):
            unique.append(item)
    return tuple(unique)


def _outcome(failures: CleanupFailures, complete: bool,
             actions: tuple[ActionResult, ...] = ()) -> CleanupResult:
    # Complete a snapshot, not the retained mutable ledger. Delivered exceptions
    # receive diagnostics once; late failures never revisit that snapshot.
    copied = CleanupFailures(failures.original_primary)
    for item in (*failures.interruptions, *failures.unexpected):
        _record(copied, item, ordinary=())
    for item in failures.ordinary_failures:
        _record(copied, item, ordinary=(Exception,))
    error = failures.original_primary
    try:
        error = copied.complete() or error
    except BaseException as exc:
        error = exc
    return CleanupResult(complete, error, _entries(failures), actions)


class OwnedProcess:
    """Launch-centered process ownership; callers never receive pipe authority."""

    _owners: list[OwnedStream]
    _cancel: threading.Event
    _stop: threading.Event
    _policy: StopPolicy | None
    _action_prepared: list[bool]
    _action_started: list[bool]
    _action_cancelled: list[bool]
    _action_results: list[ActionResult]
    _action_failures: list[CleanupFailures]
    _observed_error_kinds: list[set[str]]
    _request: threading.Event
    _ready: threading.Event
    _finished: threading.Event
    _phase_done: threading.Event
    _stopped: list[threading.Event]
    _lock: Any
    _operation_lock: Any
    _output: queue.Queue[tuple[int, bytes]]
    _tails: list[bytearray]
    _limit: int
    _grace: float
    _deadline: float | None
    _proc: Any
    _readers: list[threading.Thread | None]
    _started: list[bool]
    _fds: list[int | None]
    _acquired: list[bool]
    _transferred: list[bool]
    _continuation: threading.Thread
    _idle_stop: bool
    _duplicate_pipe: bool

    def __init__(self) -> None:
        raise TypeError("use OwnedProcess.launch to acquire a child")

    @classmethod
    def launch(
        cls, argv: Any, *, grace_seconds: float = 1.0,
        deadline_seconds: float | None = None, capture_limit: int = 65536,
        factory: Callable[..., Any] = subprocess.Popen,
        popen_kwargs: dict[str, Any] | None = None,
        on_launched: Callable[[], None] | None = None,
        stop_policy: StopPolicy | None = None,
    ) -> Self:
        if not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(s, str) and s for s in argv):
            raise ValueError("argv must be a non-empty string sequence")
        if not isinstance(grace_seconds, (int, float)) or not math.isfinite(grace_seconds) or grace_seconds <= 0:
            raise ValueError("grace_seconds must be finite and positive")
        if deadline_seconds is not None and (not isinstance(deadline_seconds, (int, float)) or not math.isfinite(deadline_seconds) or deadline_seconds <= 0):
            raise ValueError("deadline_seconds must be finite and positive")
        if not isinstance(capture_limit, int) or isinstance(capture_limit, bool) or capture_limit < 0:
            raise ValueError("capture_limit must be a nonnegative integer")
        if not callable(factory) or (on_launched is not None and not callable(on_launched)):
            raise TypeError("factory and callback must be callable")
        options = dict(popen_kwargs or {})
        if any(k in options for k in ("stdout", "stderr", "stdin", "text", "universal_newlines", "encoding", "errors")):
            raise ValueError("only binary captured stdout/stderr are supported")
        _validate_policy(stop_policy, float(grace_seconds))
        self = cls.__new__(cls)
        # Everything that can allocate synchronization/ownership/continuation
        # capacity is prepared before Popen. No existing-process entry point.
        self._owners = [_prepare_stream(label="stdout"), _prepare_stream(label="stderr")]
        self._cancel = _new_event()
        self._stop = _new_event()
        self._request = _new_event()
        self._ready = _new_event()
        self._finished = _new_event()
        self._phase_done = _new_event()
        self._stopped = [_new_event(), _new_event()]
        self._lock = _new_lock()
        self._operation_lock = _new_lock()
        self._output = queue.Queue(maxsize=32)
        self._tails = [bytearray(), bytearray()]
        self._limit = capture_limit
        self._grace = float(grace_seconds)
        self._policy = stop_policy
        self._reason = "cancel"
        self._action_prepared = [False] * len(stop_policy.stages) if stop_policy else []
        self._action_started = [False] * len(self._action_prepared)
        self._action_cancelled = [False] * len(self._action_prepared)
        self._action_results = [ActionResult(True, True) for _ in self._action_prepared]
        self._action_failures = [CleanupFailures(None) for _ in self._action_prepared]
        self._observed_error_kinds = [set() for _ in self._action_prepared]
        self._deadline = deadline_seconds
        self._launched_at = time.monotonic()
        self._proc = None
        self._readers = [None, None]
        self._started = [False, False]
        self._fds = [None, None]
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
        self._ledger = CleanupFailures(None)
        self._status = CleanupResult(False, None, ())
        self._continuation = _make_continuation(self._continue_cleanup)
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
                            raise TypeError("action prepare must acknowledge readiness with None")
            self._proc = factory(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options)
        except BaseException as exc:
            self._idle_stop = True
            if self._ready.is_set():
                self._reaped = True  # no main child was acquired
                result = self._shutdown(primary=exc, reason="setup")
                self._raise_result(result)
            else:
                self._request.set()
                failures = CleanupFailures(exc)
                if self._continuation.ident is not None:
                    failures.run(lambda: self._continuation.join(self._grace), ordinary=())
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
        setup = CleanupFailures(None)
        for index in range(2):
            setup.run(lambda index=index: self._transfer(index), ordinary=(OSError,))
        errors = _entries(setup)
        try:
            if errors:
                first = errors[0]
                # Feed both attempts to shared precedence, preserving exact
                # identities including a later transfer interruption.
                self._ledger = setup
                raise first
            for index in range(2):
                self._readers[index] = self._make_reader(index)
                self._configure_reader(index)
                self._start_reader(index)
            self._start_supervisor()
            if on_launched:
                on_launched()
        except BaseException as exc:
            result = self._shutdown(primary=exc, reason="setup")
            self._raise_result(result)
            raise
        return self

    def _transfer(self, index: int) -> None:
        if index == 1 and self._duplicate_pipe:
            raise TypeError("captured streams must be independent")
        if not self._acquired[index]:
            raise OSError("missing captured stream")
        stream = self._owners[index].stream
        if not callable(getattr(stream, "close", None)) or not callable(getattr(stream, "read", None)):
            raise TypeError("captured stream must expose read and close")
        self._transferred[index] = True

    def _make_reader(self, index: int) -> threading.Thread:
        return threading.Thread(target=self._reader, args=(index,),
                                name=f"owned-process-reader-{index}", daemon=True)

    def _configure_reader(self, index: int) -> None:
        stream = self._owners[index].stream
        try:
            fd = stream.fileno()
        except (AttributeError, OSError, ValueError):
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

    def _read_chunk(self, index: int) -> bytes | None:
        fd = self._fds[index]
        if fd is None:
            return self._owners[index].stream.read(65536)
        ready, _, _ = select.select([fd], [], [], .02)
        if not ready:
            return None
        try:
            return os.read(fd, 65536)
        except BlockingIOError:
            return None

    def _read(self, index: int) -> None:
        exited_at: float | None = None
        while not self._cancel.is_set():
            chunk = self._read_chunk(index)
            if chunk == b"":
                return
            if chunk:
                tail = self._tails[index]
                tail.extend(chunk)
                if len(tail) > self._limit:
                    del tail[:len(tail) - self._limit]
                while not self._cancel.is_set():
                    # After a stop request, retain bounded tails but do not
                    # depend on a consumer draining the pre-delivery spool.
                    if self._stop.is_set():
                        break
                    try:
                        self._output.put((index, chunk), timeout=.02)
                        break
                    except queue.Full:
                        pass
            if self._proc.poll() is not None:
                if exited_at is None:
                    exited_at = time.monotonic()
                # Descendants holding write ends cannot keep these readers
                # alive forever; output already available is drained first.
                if chunk is None and time.monotonic() - exited_at >= self._grace:
                    return

    def _reader(self, index: int) -> None:
        try:
            self._read(index)
        except BaseException as exc:
            with self._lock:
                _record(self._ledger, exc)
            self._shutdown(reason="reader")
        finally:
            self._stopped[index].set()

    def _join_reader(self, index: int, timeout: float) -> None:
        thread = self._readers[index]
        if self._started[index] and thread is not None:
            thread.join(timeout)

    def _safe(self, index: int) -> bool:
        # The explicit completion barrier, not a timed join or is_alive(), is
        # the authority to release a descriptor-backed reader's stream.
        return not self._started[index] or self._stopped[index].is_set()

    def _action(self, action: Callable[[], Any], *, ordinary: tuple[type[Exception], ...]) -> None:
        failures = CleanupFailures(None)
        failures.run(action, ordinary=ordinary)
        with self._lock:
            for error in _entries(failures):
                _record(self._ledger, error, ordinary=ordinary)

    def _release_safe(self) -> None:
        for index in range(2):
            owner = self._owners[index]
            if self._acquired[index] and not owner.released:
                def release(index: int = index, owner: OwnedStream = owner) -> None:
                    # Descriptor-less compatibility streams may need release
                    # to unblock read. Unknown/real descriptors remain retained.
                    if self._safe(index) or not callable(getattr(owner.stream, "fileno", None)):
                        owner.close()
                self._action(release, ordinary=(OSError,))

    def _wait_child(self, timeout: float) -> None:
        self._proc.wait(timeout=timeout)
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
        self._action(wait_for_termination, ordinary=(OSError,))
        if not self._reaped:
            self._action(self._proc.kill, ordinary=(OSError,))
            self._action(lambda: self._wait_child(max(0, end - time.monotonic())),
                         ordinary=(OSError, subprocess.TimeoutExpired))

    def _record_action_error(self, index: int, error: BaseException) -> None:
        with self._lock:
            _record(self._action_failures[index], error)
            _record(self._ledger, error)
            previous = self._action_results[index]
            self._action_results[index] = ActionResult(
                previous.done, previous.cleanup_complete, _entries(self._action_failures[index])[0])

    def _observe_action(self, index: int) -> ActionResult:
        assert self._policy is not None
        action = self._policy.stages[index].action
        assert action is not None
        previous = self._action_results[index]
        if previous.done and previous.cleanup_complete:
            return previous
        try:
            result = action.observe()
            if not isinstance(result, ActionResult) or not isinstance(result.done, bool) or not isinstance(result.cleanup_complete, bool):
                raise TypeError("action observation must return ActionResult")
            if result.error is not None and not isinstance(result.error, BaseException):
                raise TypeError("action error must be an exception")
        except BaseException as exc:
            result = ActionResult(False, False, exc)
            # Bound raised diagnostics by precedence class, not by the first
            # exception of any kind: later defects/interruptions must escalate.
            kind = "ordinary" if isinstance(exc, OSError) else "defect" if isinstance(exc, Exception) else "interruption"
            if kind in self._observed_error_kinds[index]:
                return previous
        if result.error is not None:
            kind = "ordinary" if isinstance(result.error, OSError) else "defect" if isinstance(result.error, Exception) else "interruption"
            # One cumulative action outcome per precedence class, not an
            # unbounded stream of new exception objects from a broken observer.
            if kind not in self._observed_error_kinds[index]:
                self._observed_error_kinds[index].add(kind)
                self._record_action_error(index, result.error)
        # Resource settlement advances independently of the cumulative action
        # failure. A successful observation cannot erase start/cancel errors.
        with self._lock:
            errors = _entries(self._action_failures[index])
            result = ActionResult(result.done, result.cleanup_complete, errors[0] if errors else None)
            self._action_results[index] = result
        return result

    def _observe_actions(self) -> None:
        for index, prepared in enumerate(self._action_prepared):
            if prepared:
                self._observe_action(index)

    def _cancel_action(self, index: int, end: float) -> None:
        assert self._policy is not None
        action = self._policy.stages[index].action
        if action is not None and self._action_prepared[index] and not self._action_cancelled[index]:
            self._action_cancelled[index] = True
            failures = CleanupFailures(None)
            failures.run(lambda: action.cancel(end), ordinary=(OSError,))
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
            stage_end = min(time.monotonic() + stage.seconds, end - policy.local_cleanup_seconds)
            if time.monotonic() >= stage_end:
                break
            with self._lock:
                if self._delivered:
                    break
                self._action_started[index] = True
            if stage.local_stop_before:
                self._action(self._terminate_if_live, ordinary=(OSError,))
            if stage.action is not None:
                def start_action(action: ManagedAction = stage.action) -> None:
                    context = StopContext(self._reason, self._proc.poll(), stage_end)
                    action.start(context)
                failures = CleanupFailures(None)
                # Context observation is part of initiation: a failed poll
                # must settle this stage and still permit later/local cleanup.
                failures.run(start_action, ordinary=(OSError,))
                for error in _entries(failures):
                    self._record_action_error(index, error)
                if _entries(failures):
                    self._cancel_action(index, end)
                    continue
            while time.monotonic() < stage_end:
                observation = self._observe_action(index) if stage.action else ActionResult(True, True)
                if observation.error is not None:
                    break
                if observation.done:
                    if not stage.wait_child:
                        break
                    child_done: bool | None = None
                    def observe_child() -> None:
                        nonlocal child_done
                        child_done = self._proc.poll() is not None
                    self._action(observe_child, ordinary=(OSError,))
                    # None denotes failed observation, not a live child. Advance
                    # rather than retrying a broken poll or losing the worker.
                    if child_done is None or child_done:
                        break
                _policy_pause(min(.005, max(0, stage_end - time.monotonic())))
            self._cancel_action(index, end)

    def _terminate_if_live(self) -> None:
        if not self._terminate_attempted and self._proc.poll() is None:
            self._terminate_attempted = True
            self._proc.terminate()

    def _complete(self) -> bool:
        return self._reaped and all(self._safe(i) for i in range(2)) and all(
            not self._acquired[i] or self._owners[i].released for i in range(2)) and all(
            not prepared or (result.done and result.cleanup_complete)
            for prepared, result in zip(self._action_prepared, self._action_results))

    def _publish(self, complete: bool) -> None:
        with self._lock:
            if not self._delivered:
                self._status = _outcome(self._ledger, complete, tuple(self._action_results))
            else:
                # Late outcomes are data only: never attach to delivered errors.
                entries = _entries(self._ledger)
                self._status = CleanupResult(complete, entries[0] if entries else None, entries,
                                             tuple(self._action_results))
        if complete:
            self._finished.set()

    def _continue_cleanup(self) -> None:
        self._ready.set()
        while not self._request.wait(.01):
            if self._supervising and self._deadline is not None and time.monotonic() - self._launched_at >= self._deadline:
                self._timed_out = True
                self._shutdown(reason="deadline")  # reentrant request
        if self._idle_stop and not self._requested:
            self._finished.set()
            return
        end = self._cleanup_end if self._requested else time.monotonic() + self._grace
        if not self._idle_stop:
            self._run_policy(end)
        self._cancel.set()
        self._cancel_actions(end)
        # Joins are protected independent actions. Reaping always follows,
        # including when a join defect/interruption occurred.
        for index in range(2):
            self._action(lambda index=index: self._join_reader(index, max(0, end - time.monotonic()) / 3), ordinary=())
        if self._proc is not None:
            self._reap(end)
        self._observe_actions()
        self._release_safe()
        self._publish(self._complete())
        self._phase_done.set()
        # Prepared continuation retains exactly these owners/readers until the
        # barriers hold. No GC release, registry, re-adoption or close retry.
        while not self._finished.is_set():
            for index in range(2):
                if not self._safe(index):
                    self._stopped[index].wait(.01)
            self._release_safe()
            if not self._reaped:
                try:
                    self._wait_child(0)
                except BaseException as exc:
                    # Poll expiry is observation, not a new shutdown budget.
                    # Retain the first new late observation defect once, rather
                    # than accumulating failures forever for a broken factory.
                    if not isinstance(exc, subprocess.TimeoutExpired) and not self._late_reap_recorded:
                        with self._lock:
                            if not any(exc is error for error in _entries(self._ledger)):
                                _record(self._ledger, exc)
                        self._late_reap_recorded = True
                    time.sleep(.01)
            self._observe_actions()
            self._publish(self._complete())
            _policy_pause(.001)

    @property
    def cleanup_status(self) -> CleanupResult:
        with self._lock:
            return self._status

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode

    def shutdown(self, *, primary: BaseException | None = None) -> CleanupResult:
        return self._shutdown(primary=primary, reason="cancel")

    def _shutdown(self, *, primary: BaseException | None = None, reason: str) -> CleanupResult:
        with self._lock:
            if primary is not None and not self._delivered and not any(primary is e for e in _entries(self._ledger)):
                old = self._ledger
                # Install an operation primary without moving a later
                # interruption ahead of an already-recorded interruption.
                original = old.original_primary
                if original is None and isinstance(primary, Exception):
                    original = primary
                self._ledger = CleanupFailures(original)
                for error in (*old.interruptions, *old.unexpected):
                    _record(self._ledger, error, ordinary=())
                if primary is not original:
                    _record(self._ledger, primary, ordinary=(Exception,))
                for error in old.ordinary_failures:
                    _record(self._ledger, error, ordinary=(Exception,))
            if not self._requested:
                self._requested = True
                self._reason = reason
                self._cleanup_end = time.monotonic() + self._grace
                self._shutdown_end = self._cleanup_end + .05
                self._stop.set()
                if self._policy is None or not self._policy.drain:
                    self._cancel.set()
                self._request.set()
        if threading.current_thread() is self._continuation or threading.current_thread() in self._readers:
            return self.cleanup_status
        # One initial coordination budget, not a new termination sequence.
        self._phase_done.wait(max(0, self._shutdown_end - time.monotonic()))
        with self._lock:
            if not self._delivered:
                # Publication and phase notification are separate steps. Merge
                # authority from the current ledger at the delivery boundary,
                # under the same lock, even if publication preceded the merge.
                self._status = _outcome(self._ledger, self._status.complete, tuple(self._action_results))
            self._delivered = True
            result = self._status
        if result.complete:
            self._continuation.join(max(0, self._shutdown_end - time.monotonic()))
        return result

    def wait_cleanup(self, timeout: float) -> CleanupResult:
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0:
            raise ValueError("timeout must be finite and nonnegative")
        end = time.monotonic() + timeout
        self._finished.wait(timeout)
        if self._finished.is_set() and threading.current_thread() is not self._continuation:
            self._continuation.join(max(0, end - time.monotonic()))
        return self.cleanup_status

    def _raise_result(self, result: CleanupResult) -> None:
        error = result.error
        if error is None and result.shutdown_incomplete:
            error = CleanupIncomplete(self)
        if error is not None:
            if result.shutdown_incomplete:
                setattr(error, "cleanup", self)
            raise error

    def _collect(self, callback: Callable[[str, bytes], Any] | None) -> ProcessResult:
        callback_errors: list[BaseException] = []
        try:
            while not all(self._stopped[i].is_set() or not self._started[i] for i in range(2)) or not self._output.empty():
                try:
                    index, chunk = self._output.get(timeout=.02)
                except queue.Empty:
                    if self._stop.is_set():
                        break
                    continue
                if callback is not None and not self._stop.is_set():
                    try:
                        callback(("stdout", "stderr")[index], chunk)
                    except Exception as exc:
                        if len(callback_errors) < 32:
                            callback_errors.append(exc)
            # Reader EOF may precede child exit; a deadline/cancel wins safely.
            while self._proc.poll() is None and not self._stop.is_set():
                time.sleep(.01)
        except BaseException as exc:
            result = self.shutdown(primary=exc)
            self._raise_result(result)
            raise
        result = self._shutdown(reason="natural" if not self._stop.is_set() else self._reason)
        self._raise_result(result)
        return ProcessResult(bytes(self._tails[0]), bytes(self._tails[1]),
                             self.returncode, self._timed_out, result, tuple(callback_errors))

    def capture(self) -> ProcessResult:
        with self._operation_lock:
            return self._collect(None)

    def stream(self, callback: Callable[[str, bytes], Any]) -> ProcessResult:
        if not callable(callback):
            raise TypeError("callback must be callable")
        # The bounded spool exists before launch so early chunks are preserved.
        with self._operation_lock:
            self._streaming = True
            return self._collect(callback)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> None:
        reason = "context"
        if exc is None:
            def observe_exit() -> None:
                nonlocal reason
                if self._proc.poll() is not None:
                    reason = "natural"
            # Reason selection is cleanup observation, not an escape hatch
            # around the coordinator. Failure retains the context stop plan.
            self._action(observe_exit, ordinary=(OSError,))
        self._raise_result(self._shutdown(primary=exc, reason=reason))


__all__ = ["OwnedProcess", "ProcessResult", "CleanupResult", "CleanupIncomplete",
           "StopPolicy", "StopStage", "StopContext", "ManagedAction", "ActionResult"]
