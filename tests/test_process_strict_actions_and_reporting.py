"""Phase 6BC.9 strict action transport and sole-invocation reporting."""

import sys
import threading
from types import SimpleNamespace

import pytest

import docker.process as process
from tests.test_process_single_collection import Child, collect, launch, settle


def info(error, kind="ORDINARY", phase="ACTION", stream=None):
    assert hasattr(process, "FailureInfo"), "missing explicit failure transport"
    return process.FailureInfo(
        error,
        getattr(process.FailureKind, kind),
        getattr(process.FailurePhase, phase),
        stream,
    )


class Action:
    def __init__(self, observations):
        self.observations = list(observations)
        self.prepares = self.starts = self.cancels = self.observes = 0
        self.finish = False

    def prepare(self):
        self.prepares += 1

    def start(self, context):
        self.starts += 1

    def observe(self):
        self.observes += 1
        if self.finish:
            return process.ActionResult(True, True)
        if len(self.observations) > 1:
            return self.observations.pop(0)
        return self.observations[0]

    def cancel(self, deadline):
        self.cancels += 1


def action_owner(action):
    policy = process.StopPolicy(
        (process.StopStage(0.05, action, run_on_normal=True),), 0.1
    )
    return launch(Child(), stop_policy=policy)


@pytest.mark.parametrize("kind", ["ORDINARY", "UNEXPECTED"])
@pytest.mark.parametrize(
    "error", [OSError("helper"), ValueError("helper"), RuntimeError("helper")]
)
def test_action_kind_is_explicit_not_inferred_from_error_type(kind, error):
    failure = info(error, kind)
    action = Action([process.ActionResult(True, True, (failure,))])
    owner = action_owner(action)
    primary = ValueError("main operation")
    try:
        status = owner.shutdown(primary=primary)
        assert status.complete
        assert status.failures[0].error is (primary if kind == "ORDINARY" else error)
        transported = next(f for f in status.failures if f.error is error)
        assert transported == failure
        assert not hasattr(status, "actions") and not hasattr(status, "error")
        assert action.prepares == action.starts == action.cancels == 1
    finally:
        action.finish = True
        settle(owner)


@pytest.mark.parametrize("method", ["start", "cancel"])
@pytest.mark.parametrize(
    "error",
    [None, OSError("method"), RuntimeError("method"), KeyboardInterrupt("method")],
)
def test_direct_action_boundary_and_void_return_validation(method, error):
    action = Action([process.ActionResult(True, True)])
    original = getattr(action, method)

    def invoke(*args):
        original(*args)
        if error is not None:
            raise error
        return 17  # start/cancel are void; ActionResult belongs only to observe.

    setattr(action, method, invoke)
    owner = action_owner(action)
    try:
        if error is not None and not isinstance(error, Exception):
            with pytest.raises(BaseException) as caught:
                owner.capture()
            assert caught.value is error and not hasattr(error, "process_result")
            failures = owner.wait_cleanup(1).failures
        else:
            failures = owner.capture().lifecycle.failures
        selected = failures[0]
        assert selected.phase.name == "ACTION" and selected.stream is None
        if error is None:
            assert (
                isinstance(selected.error, TypeError)
                and selected.kind.name == "UNEXPECTED"
            )
        else:
            assert selected.error is error
            expected = (
                "INTERRUPTION"
                if not isinstance(error, Exception)
                else "ORDINARY"
                if isinstance(error, OSError)
                else "UNEXPECTED"
            )
            assert selected.kind.name == expected
    finally:
        action.finish = True
        settle(owner)


def test_cumulative_action_failures_are_not_duplicated_or_erased():
    error = OSError("known before done")
    failure = info(error)
    action = Action(
        [
            process.ActionResult(False, False, (failure,)),
            process.ActionResult(True, False, (failure,)),
            process.ActionResult(True, True),
        ]
    )
    owner = action_owner(action)
    try:
        result = owner.capture()
        # Capture may return an incomplete snapshot under scheduling pressure.
        # Observe the same retained owners/continuation, not a new shutdown.
        status = owner.wait_cleanup(1)
        assert status.complete
        action_failures = tuple(
            entry
            for entry in status.failures
            if entry.phase is process.FailurePhase.ACTION
        )
        assert action_failures == (failure,)
        # An initial coordination timeout is a real reported failure. Keep it;
        # completion must not erase earlier diagnostics or duplicate the action.
        assert all(entry in status.failures for entry in result.lifecycle.failures)
        assert all(
            entry is failure
            or (
                isinstance(entry.error, process.CleanupIncomplete)
                and entry.phase is process.FailurePhase.COORDINATION
            )
            for entry in status.failures
        )
        if result.lifecycle.complete:
            assert result.lifecycle.failures == (failure,)
        for _ in range(3):
            repeated = owner.wait_cleanup(0.1)
            assert repeated.complete
            assert repeated.failures == status.failures
        assert action.prepares == action.cancels == 1
        # An expired stage cap may prevent start, but may never start twice.
        assert action.starts in (0, 1)
    finally:
        action.finish = True
        settle(owner)


def test_helper_snapshot_and_parent_context_are_orthogonal():
    error = OSError("helper read")
    helper_failure = info(error, "UNEXPECTED", "JOIN", process.ProcessStream.STDERR)
    helper = process.ProcessResult(
        b"helper", b"stderr", 1, False, process.LifecycleResult(True, (helper_failure,))
    )
    action_failure = info(helper_failure.error, helper_failure.kind.name)
    action = Action([process.ActionResult(True, True, (action_failure,))])
    owner = action_owner(action)
    try:
        result = owner.capture()
        assert result.lifecycle.failures == (action_failure,)
        assert helper.lifecycle.failures == (helper_failure,)
        assert result.lifecycle.failures[0].error is helper.lifecycle.failures[0].error
        assert helper.lifecycle.failures[0].phase.name == "JOIN"
        assert result.lifecycle.failures[0].phase.name == "ACTION"
        assert result.lifecycle.failures[0].stream is None
        assert not hasattr(error, "process_result")
    finally:
        action.finish = True
        settle(owner)


@pytest.mark.parametrize(
    "malformed", ["bare", "wrong_kind", "wrong_phase", "wrong_stream"]
)
@pytest.mark.parametrize(
    "interrupt",
    [
        KeyboardInterrupt("actual"),
        SystemExit(2),
        GeneratorExit(),
        BaseException("actual"),
    ],
)
def test_malformed_observation_cannot_discard_supplied_control_flow(
    malformed, interrupt
):
    # Deliberately bypass record validation to exercise the consumer boundary,
    # as opposed to constructor rejection tested separately in 6BC.6.
    bad = (
        interrupt
        if malformed == "bare"
        else SimpleNamespace(
            error=interrupt,
            kind="ORDINARY" if malformed == "wrong_kind" else None,
            phase="READ" if malformed == "wrong_phase" else None,
            stream="stdout" if malformed == "wrong_stream" else None,
        )
    )
    observation = object.__new__(process.ActionResult)
    for name, value in dict(done=True, cleanup_complete=True, failures=(bad,)).items():
        object.__setattr__(observation, name, value)
    action = Action([observation])
    child = Child()
    policy = process.StopPolicy(
        (process.StopStage(0.05, action, run_on_normal=True),), 0.1
    )
    owner = launch(child, stop_policy=policy)
    try:
        with pytest.raises(BaseException) as caught:
            owner.capture()
        assert caught.value is interrupt
        assert not hasattr(interrupt, "process_result")
        failures = owner.wait_cleanup(1).failures
        assert failures[0].error is interrupt
        assert (
            failures[0].kind.name == "INTERRUPTION"
            and failures[0].phase.name == "ACTION"
        )
        assert failures[0].stream is None
        assert any(
            isinstance(f.error, (TypeError, ValueError)) and f.kind.name == "UNEXPECTED"
            for f in failures[1:]
        )
        assert child.stdout.close_calls == child.stderr.close_calls == 1
    finally:
        action.finish = True
        settle(owner)


@pytest.mark.parametrize("source", ["callback", "reporter", "fallback"])
@pytest.mark.parametrize("position", [0, 1])
@pytest.mark.parametrize(
    "interrupt",
    [
        KeyboardInterrupt("first"),
        SystemExit(3),
        GeneratorExit(),
        BaseException("first"),
    ],
)
def test_callback_reporter_fallback_control_flow_has_exact_boundary(
    monkeypatch, source, position, interrupt
):
    identity = tuple(process.ProcessStream)[position]
    diagnostic_error = OSError("callback diagnostic")
    reports, endings = [], []
    raised = []

    def callback(stream, chunk):
        if stream is identity:
            if source == "callback":
                raised.append(interrupt)
                raise interrupt
            raise diagnostic_error

    def reporter(error, **context):
        reports.append((error, context))
        if source == "reporter":
            raise interrupt
        if source == "fallback":
            raise RuntimeError("reporter defect")

    def fallback():
        raise interrupt

    monkeypatch.setattr(process, "_fallback_callback_diagnostic", fallback)
    child = Child()
    owner = launch(child, reporter=reporter)
    try:
        with pytest.raises(BaseException) as caught:
            owner.stream(callback, on_stream_end=lambda *event: endings.append(event))
        assert caught.value is interrupt and not hasattr(interrupt, "process_result")
        selected = owner.wait_cleanup(1).failures[0]
        assert selected.error is interrupt and selected.kind.name == "INTERRUPTION"
        assert selected.phase.name == (
            "CALLBACK" if source == "callback" else "REPORTER"
        )
        assert selected.stream is identity
        assert {event[0] for event in endings} == set(process.ProcessStream)
        assert len(endings) == 2
        assert child.stdout.close_calls == child.stderr.close_calls == 1
    finally:
        settle(owner)


@pytest.mark.parametrize("later_site", ["callback", "reporter", "fallback"])
@pytest.mark.parametrize("position", [0, 1])
def test_later_notification_interruption_does_not_replace_first(
    monkeypatch, later_site, position
):
    first, later = KeyboardInterrupt("first chunk"), SystemExit("later end")
    identity = tuple(process.ProcessStream)[position]
    seen, endings = [], []

    def chunk(stream, data):
        if stream is identity:
            seen.append(first)
            raise first

    def end(stream, terminal):
        endings.append((stream, terminal))
        if seen:
            if later_site == "callback":
                raise later
            raise OSError("ordinary end diagnostic")

    def reporter(*args, **kwargs):
        if later_site == "reporter":
            raise later
        raise ValueError("reporter diagnostic")

    def fallback():
        raise later

    monkeypatch.setattr(process, "_fallback_callback_diagnostic", fallback)
    child = Child()
    owner = launch(child, reporter=reporter)
    try:
        with pytest.raises(BaseException) as caught:
            owner.stream(chunk, on_stream_end=end)
        assert caught.value is first and not hasattr(first, "process_result")
        failures = owner.wait_cleanup(1).failures
        assert failures[0].error is first
        assert failures[0].phase.name == "CALLBACK" and failures[0].stream is identity
        assert sum(f.error is later for f in failures) == 1
        assert len(endings) == 2
        assert child.stdout.close_calls == child.stderr.close_calls == 1
    finally:
        settle(owner)


@pytest.mark.parametrize("failure_site", ["chunk", "end"])
def test_ordinary_callback_reporting_is_outside_locks_without_error_store(failure_site):
    error = ValueError("diagnostic only")
    reports, endings = [], []
    owner_ref = []

    def reporter(actual, **context):
        owner = owner_ref[0]
        assert owner._lock.acquire(blocking=False), "reporting held lifecycle lock"
        owner._lock.release()
        reports.append((actual, context))

    def chunk(*args):
        if failure_site == "chunk":
            raise error

    def end(*args):
        endings.append(args)
        if failure_site == "end":
            raise error

    owner = launch(Child(), reporter=reporter)
    owner_ref.append(owner)
    try:
        result = owner.stream(chunk, on_stream_end=end)
        assert result.lifecycle.complete and result.lifecycle.failures == ()
        assert len(reports) == 2 and all(actual is error for actual, _ in reports)
        assert {c["stream"] for _, c in reports} == set(process.ProcessStream)
        assert {c["callback_kind"] for _, c in reports} == {failure_site}
        assert len(endings) == 2
        assert not hasattr(result, "callback_errors")
        before = list(reports)
        with pytest.raises(RuntimeError):
            collect(owner, "stream")
        assert reports == before
    finally:
        settle(owner)


@pytest.mark.parametrize("site", ["default", "fallback", "broken_fallback"])
def test_reporting_defaults_are_fixed_safe_and_nonrecursive(monkeypatch, caplog, site):
    secret = "https://user:credential@example.test/private"
    error = ValueError(secret)
    reports, fallbacks = [], []

    def chunk(*args):
        raise error

    def reporter(actual, **context):
        reports.append(actual)
        raise RuntimeError(secret)

    if site == "broken_fallback":

        def fallback():
            fallbacks.append(True)
            raise OSError(secret)

        monkeypatch.setattr(process, "_fallback_callback_diagnostic", fallback)
    owner = launch(Child(), reporter=None if site == "default" else reporter)
    try:
        result = owner.stream(chunk)
        assert result.lifecycle.complete and result.lifecycle.failures == ()
        assert secret not in caplog.text and "credential" not in caplog.text
        if site == "default":
            assert len(caplog.records) == 2
            assert {r.getMessage() for r in caplog.records} == {
                "Process callback failed"
            }
        else:
            assert reports == [error, error]
            if site == "fallback":
                assert len(caplog.records) == 2
                assert {r.getMessage() for r in caplog.records} == {
                    "Process callback diagnostic reporting failed"
                }
            else:
                assert fallbacks == [True, True] and caplog.records == []
    finally:
        settle(owner)


def test_clean_eof_is_not_deferred_or_revised_by_sibling_failure(monkeypatch):
    ended = threading.Event()
    failure = OSError("stderr read")
    original = process.OwnedProcess._read_chunk
    events = []

    def read(self, index):
        if index == 1:
            assert ended.wait(2), "stdout EOF waited for its sibling"
            raise failure
        return original(self, index)

    def end(identity, outcome):
        events.append((identity, outcome))
        if identity is process.ProcessStream.STDOUT:
            ended.set()

    monkeypatch.setattr(process.OwnedProcess, "_read_chunk", read)
    owner = launch(Child())
    try:
        result = owner.stream(lambda *a: None, on_stream_end=end)
        assert events == [
            (process.ProcessStream.STDOUT, process.StreamOutcome.EOF),
            (process.ProcessStream.STDERR, process.StreamOutcome.READ_FAILED),
        ]
        assert result.stream_outcomes == tuple(events)
        assert result.lifecycle.failures[0].error is failure
        assert result.lifecycle.failures[0].stream is process.ProcessStream.STDERR
    finally:
        ended.set()
        settle(owner)


def test_aborted_live_descriptor_reader_retains_owner_until_barrier(monkeypatch):
    entered, resume = threading.Event(), threading.Event()
    original = process.OwnedProcess._read_chunk
    events = []

    def read(self, index):
        if index == 0:
            entered.set()
            assert resume.wait(3)
        return original(self, index)

    monkeypatch.setattr(process.OwnedProcess, "_read_chunk", read)
    owner = process.OwnedProcess.launch(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        grace_seconds=0.2,
        deadline_seconds=0.02,
    )
    try:
        assert entered.wait(1)
        result = owner.stream(
            lambda *a: events.append(a), on_stream_end=lambda *a: events.append(a)
        )
        assert not result.lifecycle.complete
        assert (
            dict(result.stream_outcomes)[process.ProcessStream.STDOUT]
            is process.StreamOutcome.ABORTED
        )
        assert not owner._owners[0].released and not owner._stopped[0].is_set()
        before = list(events)
        resume.set()
        assert owner.wait_cleanup(2).complete
        assert owner._owners[0].released and owner._stopped[0].is_set()
        assert events == before
        assert (
            dict(result.stream_outcomes)[process.ProcessStream.STDOUT]
            is process.StreamOutcome.ABORTED
        )
    finally:
        resume.set()
        settle(owner)


@pytest.mark.parametrize("publish_first", [False, True])
def test_completion_racing_dispatch_has_once_only_end_attempt(
    monkeypatch, publish_first
):
    ready, publish = threading.Event(), threading.Event()
    original = process.OwnedProcess._set_outcome
    events = []

    def outcome(self, index, terminal):
        if index == 1 and terminal is process.StreamOutcome.EOF:
            ready.set()
            assert publish.wait(3)
        return original(self, index, terminal)

    monkeypatch.setattr(process.OwnedProcess, "_set_outcome", outcome)
    child = Child()
    child.stderr.seek(0, 2)
    owner = launch(child)

    def chunk(identity, data):
        events.append(("chunk", identity, data))
        assert ready.wait(1)
        publish.set()
        if publish_first:
            assert owner._stopped[1].wait(1)

    try:
        result = owner.stream(
            chunk, on_stream_end=lambda *a: events.append(("end", *a))
        )
        assert result.lifecycle.complete
        for identity in process.ProcessStream:
            end = [event for event in events if event[:2] == ("end", identity)]
            assert end == [("end", identity, process.StreamOutcome.EOF)]
        assert events[0][:2] == ("chunk", process.ProcessStream.STDOUT)
    finally:
        publish.set()
        settle(owner)


@pytest.mark.parametrize("site", ["failures", "error"])
@pytest.mark.parametrize(
    "interrupt",
    [
        KeyboardInterrupt("field"),
        SystemExit(4),
        GeneratorExit(),
        BaseException("field"),
    ],
)
def test_malformed_field_access_cannot_escape_independent_cleanup(site, interrupt):
    class Field:
        @property
        def failures(self):
            raise interrupt

        @property
        def error(self):
            raise interrupt

    observation = Field()
    if site == "error":
        observation = object.__new__(process.ActionResult)
        for name, value in dict(
            done=True, cleanup_complete=True, failures=(Field(),)
        ).items():
            object.__setattr__(observation, name, value)
    action = Action([observation])
    child = Child()
    policy = process.StopPolicy(
        (process.StopStage(0.05, action, run_on_normal=True),), 0.1
    )
    owner = launch(child, stop_policy=policy)
    try:
        with pytest.raises(BaseException) as caught:
            owner.capture()
        assert caught.value is interrupt and not hasattr(interrupt, "process_result")
        selected = owner.cleanup_status.failures[0]
        assert (
            selected.error is interrupt
            and selected.phase is process.FailurePhase.ACTION
        )
        assert selected.kind is process.FailureKind.INTERRUPTION
        assert child.stdout.close_calls == child.stderr.close_calls == 1
    finally:
        action.finish = True
        settle(owner)


@pytest.mark.parametrize(
    "fault",
    [
        OSError("coordination defect"),
        RuntimeError("coordination defect"),
        KeyboardInterrupt("policy"),
        SystemExit(3),
        GeneratorExit(),
        BaseException("policy"),
    ],
)
@pytest.mark.parametrize(
    "close_error",
    [OSError("release"), RuntimeError("release"), KeyboardInterrupt("release")],
)
def test_policy_coordination_failure_never_skips_reap_or_independent_release(
    monkeypatch, fault, close_error
):
    def policy(self, deadline):
        raise fault

    monkeypatch.setattr(process.OwnedProcess, "_run_policy", policy)
    child = Child(error=close_error)
    owner = launch(child)
    expected = (
        fault
        if not isinstance(fault, Exception) or isinstance(close_error, Exception)
        else close_error
    )
    try:
        if isinstance(expected, Exception):
            status = owner.capture().lifecycle
        else:
            with pytest.raises(BaseException) as caught:
                owner.capture()
            assert caught.value is expected
            status = owner.cleanup_status
        assert status.complete and status.failures[0].error is expected
        assert any(
            f.error is fault and f.phase is process.FailurePhase.COORDINATION
            for f in status.failures
        )
        assert any(
            f.error is close_error and f.phase is process.FailurePhase.RELEASE
            for f in status.failures
        )
        assert (
            child.wait_calls > 0
            and child.stdout.close_calls == child.stderr.close_calls == 1
        )
    finally:
        settle(owner)


@pytest.mark.parametrize("source", ["launch", "callback"])
@pytest.mark.parametrize(
    "close_error",
    [None, OSError("release"), RuntimeError("release"), KeyboardInterrupt("release")],
)
@pytest.mark.parametrize(
    "setter_error",
    [RuntimeError("observation setter"), SystemExit("observation setter")],
)
def test_refused_cleanup_attribute_cannot_replace_first_interruption(
    monkeypatch, source, close_error, setter_error
):
    attempts = []

    class RefusingInterrupt(KeyboardInterrupt):
        def __setattr__(self, name, value):
            if name == "cleanup":
                attempts.append(name)
                raise setter_error
            return super().__setattr__(name, value)

    first = RefusingInterrupt("first")
    action = Action([process.ActionResult(False, False)])
    policy = process.StopPolicy(
        (process.StopStage(0.05, action, run_on_normal=True),), 0.1
    )
    child = Child(error=close_error)
    prepared = []
    original = process._make_continuation

    def continuation(callback):
        prepared.append(callback.__self__)
        return original(callback)

    def interrupt(*args):
        raise first

    monkeypatch.setattr(process, "_make_continuation", continuation)
    owner = None
    try:
        with pytest.raises(RefusingInterrupt) as caught:
            owner = launch(
                child,
                stop_policy=policy,
                on_launched=interrupt if source == "launch" else None,
            )
            owner.stream(interrupt)
        assert caught.value is first and not hasattr(first, "process_result")
        owner = prepared[0]
        assert owner._delivered and attempts == ["cleanup"]
        status = owner.cleanup_status
        assert status.failures[0].error is first
        assert any(
            f.error is setter_error and f.phase is process.FailurePhase.COORDINATION
            for f in status.failures
        )
        if close_error is not None:
            assert any(f.error is close_error for f in status.failures)
        assert child.stdout.close_calls == child.stderr.close_calls == 1
        owner.shutdown()
        assert attempts == ["cleanup"]
    finally:
        action.finish = True
        if prepared:
            settle(prepared[0])


def test_completion_before_registration_preserves_chunk_then_end_once():
    owner = launch(Child())
    events = []
    try:
        assert all(event.wait(1) for event in owner._stopped)
        result = owner.stream(
            lambda *a: events.append(("chunk", *a)),
            on_stream_end=lambda *a: events.append(("end", *a)),
        )
        assert result.lifecycle.complete
        for identity in process.ProcessStream:
            local = [event for event in events if event[1] is identity]
            assert local == [
                (
                    "chunk",
                    identity,
                    b"stdout"
                    if identity is process.ProcessStream.STDOUT
                    else b"stderr",
                ),
                ("end", identity, process.StreamOutcome.EOF),
            ]
        before = list(events)
        with pytest.raises(RuntimeError):
            owner.stream(
                lambda *a: events.append(a), on_stream_end=lambda *a: events.append(a)
            )
        assert events == before
    finally:
        settle(owner)
