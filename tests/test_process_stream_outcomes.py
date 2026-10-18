"""Typed outcomes and once-only notifications for a single collection."""

import inspect
import threading
from dataclasses import FrozenInstanceError
from unittest import mock

import pytest

import docker.process as process
from docker.process import OwnedProcess, ProcessResult


class Pipe:
    def __init__(self, chunks=(), *, gate=None, error=None):
        self.chunks = list(chunks)
        self.gate = gate
        self.error = error
        self.read_entered = threading.Event()
        self.read_finished = threading.Event()
        self.close_attempts = 0
        self.read_attempts = 0

    def read(self, size):
        self.read_attempts += 1
        if self.chunks:
            return self.chunks.pop(0)
        self.read_entered.set()
        if self.gate is not None:
            assert self.gate.wait(5), "test-controlled reader was not released"
        self.read_finished.set()
        if self.error is not None:
            raise self.error
        return b""

    def close(self):
        self.close_attempts += 1


class Child:
    def __init__(self, stdout=None, stderr=None):
        self.stdout = stdout if stdout is not None else Pipe([b"out"])
        self.stderr = stderr if stderr is not None else Pipe([b"err"])
        self.returncode = 7
        self.wait_attempts = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.wait_attempts += 1
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


def enums():
    assert hasattr(process, "ProcessStream"), "missing typed stream identities"
    assert hasattr(process, "StreamOutcome"), "missing retained stream outcomes"
    return process.ProcessStream, process.StreamOutcome


def launch(child, **kwargs):
    return OwnedProcess.launch(
        ["child"],
        factory=lambda *a, **k: child,
        grace_seconds=0.1,
        capture_limit=8,
        **kwargs,
    )


def test_exact_enum_members_and_keyword_only_end_callback():
    stream, outcome = enums()
    assert tuple(stream.__members__) == ("STDOUT", "STDERR")
    assert tuple(outcome.__members__) == ("PENDING", "EOF", "READ_FAILED", "ABORTED")
    signature = inspect.signature(OwnedProcess.stream)
    assert tuple(signature.parameters) == ("self", "callback", "on_stream_end")
    end = signature.parameters["on_stream_end"]
    assert end.kind is inspect.Parameter.KEYWORD_ONLY and end.default is None
    assert tuple(inspect.signature(OwnedProcess.capture).parameters) == ("self",)


@pytest.mark.parametrize("method", ["capture", "stream"])
def test_success_has_immutable_ordered_terminal_outcomes(method):
    stream, outcome = enums()
    child = Child()
    owner = launch(child)
    chunks, ends = [], []
    try:
        result = (
            owner.capture()
            if method == "capture"
            else owner.stream(
                lambda *args: chunks.append(args),
                on_stream_end=lambda *args: ends.append(args),
            )
        )
        assert type(result) is ProcessResult
        expected = ((stream.STDOUT, outcome.EOF), (stream.STDERR, outcome.EOF))
        assert (
            result.stream_outcomes == expected and type(result.stream_outcomes) is tuple
        )
        with pytest.raises(FrozenInstanceError):
            result.stream_outcomes = ()
        assert (result.stdout, result.stderr, result.returncode) == (b"out", b"err", 7)
        if method == "stream":
            assert sorted(chunks, key=lambda item: item[0].name) == [
                (stream.STDERR, b"err"),
                (stream.STDOUT, b"out"),
            ]
            assert set(ends) == set(expected) and len(ends) == 2
        assert child.stdout.close_attempts == child.stderr.close_attempts == 1
    finally:
        owner.shutdown()
        assert owner.wait_cleanup(1).complete


@pytest.mark.parametrize("completion_first", [False, True])
@pytest.mark.parametrize("fail_end", [False, True])
def test_registration_completion_race_and_chunk_before_end(completion_first, fail_end):
    stream, outcome = enums()
    gate = threading.Event()
    child = Child(stdout=Pipe([b"out"], gate=gate))
    reports = []
    owner = launch(
        child, reporter=lambda exc, **context: reports.append((exc, context))
    )
    events, errors = [], []
    callback_error = ValueError("racing end callback")

    def end(identity, terminal):
        events.append(("end", identity, terminal))
        if fail_end:
            raise callback_error

    if completion_first:
        gate.set()
        assert child.stdout.read_finished.wait(1)

    def collect():
        try:
            owner.stream(
                lambda identity, data: events.append(("chunk", identity, data)),
                on_stream_end=end,
            )
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=collect, name="test-outcome-registration")
    try:
        worker.start()
        assert child.stdout.read_entered.wait(1)
        gate.set()
        worker.join(2)
        assert not worker.is_alive() and not errors
        assert len(reports) == (2 if fail_end else 0)
        assert all(
            exc is callback_error and context["callback_kind"] == "end"
            for exc, context in reports
        )
        for identity in stream:
            local = [event for event in events if event[1] is identity]
            assert local == [
                ("chunk", identity, b"out" if identity is stream.STDOUT else b"err"),
                ("end", identity, outcome.EOF),
            ]
    finally:
        gate.set()
        if worker.ident is not None:
            worker.join(2)
        owner.shutdown()
        assert owner.wait_cleanup(1).complete


def test_clean_eof_is_prompt_and_survives_later_sibling_read_failure():
    stream, outcome = enums()
    gate = threading.Event()
    error = OSError("stderr read")
    child = Child(stdout=Pipe([b"to"]), stderr=Pipe(gate=gate, error=error))
    owner = launch(child)
    stdout_end = threading.Event()
    events, errors, results = [], [], []

    def end(identity, terminal):
        events.append(("end", identity, terminal))
        if identity is stream.STDOUT:
            stdout_end.set()

    def collect():
        try:
            results.append(
                owner.stream(
                    lambda identity, data: events.append(("chunk", identity, data)),
                    on_stream_end=end,
                )
            )
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=collect, name="test-independent-eof")
    try:
        worker.start()
        assert stdout_end.wait(1), "clean EOF must not wait for sibling completion"
        assert not child.stderr.read_finished.is_set()
        gate.set()
        worker.join(2)
        assert not worker.is_alive() and errors == [] and len(results) == 1
        assert results[0].lifecycle.failures[0].error is error
        assert events == [
            ("chunk", stream.STDOUT, b"to"),
            ("end", stream.STDOUT, outcome.EOF),
            ("end", stream.STDERR, outcome.READ_FAILED),
        ]
        assert results[0].stream_outcomes == (
            (stream.STDOUT, outcome.EOF),
            (stream.STDERR, outcome.READ_FAILED),
        )
        assert results[0].stdout == b"to" and not hasattr(error, "process_result")
    finally:
        gate.set()
        if worker.ident is not None:
            worker.join(2)
        owner.shutdown()
        assert owner.wait_cleanup(1).complete


@pytest.mark.parametrize("callback_kind", ["chunk", "end"])
def test_ordinary_callback_error_is_isolated_without_read_failure_or_retry(
    callback_kind,
):
    stream, outcome = enums()
    error = ValueError("callback")
    child = Child()
    reports = []
    owner = launch(
        child, reporter=lambda exc, **context: reports.append((exc, context))
    )
    calls = []

    def callback(identity, data):
        calls.append(identity)
        raise error

    try:
        result = owner.stream(
            callback if callback_kind == "chunk" else lambda *args: None,
            on_stream_end=callback if callback_kind == "end" else None,
        )
        assert set(calls) == set(stream) and len(calls) == 2
        assert not hasattr(result, "callback_errors")
        assert [exc for exc, _ in reports] == [error, error]
        assert {context["stream"] for _, context in reports} == set(stream)
        assert all(context["callback_kind"] == callback_kind for _, context in reports)
        assert result.stream_outcomes == (
            (stream.STDOUT, outcome.EOF),
            (stream.STDERR, outcome.EOF),
        )
        assert result.lifecycle.failures == ()
    finally:
        owner.shutdown()
        assert owner.wait_cleanup(1).complete


@pytest.mark.parametrize("callback_kind", ["chunk", "end"])
def test_isolated_callback_failure_survives_cleanup_failure_value(callback_kind):
    stream, outcome = enums()
    callback_error, close_error = ValueError("callback"), OSError("release")
    child = Child()
    close = child.stdout.close

    def failing_close():
        close()
        raise close_error

    child.stdout.close = failing_close
    reports, calls = [], []
    owner = launch(child, reporter=lambda exc, **context: reports.append(exc))

    def callback(identity, data):
        calls.append(identity)
        raise callback_error

    try:
        result = owner.stream(
            callback if callback_kind == "chunk" else lambda *args: None,
            on_stream_end=callback if callback_kind == "end" else None,
        )
        assert result.lifecycle.failures[0].error is close_error
        assert not hasattr(result, "callback_errors") and not hasattr(
            close_error, "process_result"
        )
        assert reports == [callback_error, callback_error]
        assert result.stream_outcomes == (
            (stream.STDOUT, outcome.EOF),
            (stream.STDERR, outcome.EOF),
        )
        assert all(f.error is not callback_error for f in result.lifecycle.failures)
        assert set(calls) == set(stream) and len(calls) == 2
        assert owner.wait_cleanup(1).complete and len(calls) == 2
        assert child.stdout.close_attempts == child.stderr.close_attempts == 1
    finally:
        owner.shutdown()
        assert owner.wait_cleanup(1).complete


@pytest.mark.parametrize("fail_end", [False, True])
def test_concurrent_invocations_reject_loser_without_callbacks_or_replay(fail_end):
    stream, outcome = enums()
    reports = []
    owner = launch(
        Child(),
        reporter=lambda exc, **context: reports.append(
            (threading.current_thread().name, exc)
        ),
    )
    callback_error = ValueError("concurrent end")
    first_entered, release_first, second_done = (threading.Event() for _ in range(3))
    events = {"first": [], "second": []}
    errors = []

    def collect(name):
        def chunk(identity, data):
            events[name].append(("chunk", identity, data))
            if name == "first":
                first_entered.set()
                assert release_first.wait(5)

        def end(*args):
            events[name].append(("end", *args))
            if fail_end:
                raise callback_error

        try:
            owner.stream(chunk, on_stream_end=end)
        except BaseException as exc:
            errors.append((name, exc))
        finally:
            if name == "second":
                second_done.set()

    first = threading.Thread(target=collect, args=("first",), name="first-stream")
    second = threading.Thread(target=collect, args=("second",), name="second-stream")
    try:
        first.start()
        assert first_entered.wait(1)
        second.start()
        assert second_done.wait(1), "loser waited on the collecting caller"
        assert events["second"] == []
        release_first.set()
        first.join(2)
        second.join(2)
        assert not first.is_alive() and not second.is_alive()
        assert (
            len(errors) == 1
            and errors[0][0] == "second"
            and isinstance(errors[0][1], RuntimeError)
        )
        assert reports == ([("first-stream", callback_error)] * 2 if fail_end else [])
        for identity in stream:
            assert sum(event[:2] == ("end", identity) for event in events["first"]) == 1
        assert events["second"] == []
    finally:
        release_first.set()
        for thread in (first, second):
            if thread.ident is not None:
                thread.join(2)
        owner.shutdown()
        assert owner.wait_cleanup(1).complete


@pytest.mark.parametrize("first_error_kind", [OSError, KeyboardInterrupt])
def test_later_failure_subscription_is_rejected_without_mutation_or_replay(
    first_error_kind,
):
    enums()
    error = first_error_kind("read")
    child = Child(stderr=Pipe(error=error))
    owner = launch(child)
    try:
        if first_error_kind is KeyboardInterrupt:
            with pytest.raises(KeyboardInterrupt) as caught:
                owner.stream(lambda *args: None)
            assert caught.value is error
        else:
            assert owner.stream(lambda *args: None).lifecycle.failures[0].error is error
        status = owner.cleanup_status
        notes = tuple(getattr(error, "__notes__", ()))
        before = (
            child.stdout.read_attempts,
            child.stderr.read_attempts,
            child.wait_attempts,
        )
        chunks, ends = [], []
        with (
            mock.patch.object(
                owner, "_read_chunk", side_effect=AssertionError("reread")
            ),
            mock.patch.object(
                owner, "_shutdown", side_effect=AssertionError("budget restart")
            ),
        ):
            with pytest.raises(RuntimeError):
                owner.stream(
                    lambda *args: chunks.append(args),
                    on_stream_end=lambda *args: ends.append(args),
                )
        assert owner.cleanup_status == status and not hasattr(error, "process_result")
        assert tuple(getattr(error, "__notes__", ())) == notes and chunks == ends == []
        assert before == (
            child.stdout.read_attempts,
            child.stderr.read_attempts,
            child.wait_attempts,
        )
        assert child.stdout.close_attempts == child.stderr.close_attempts == 1
    finally:
        owner.shutdown()
        assert owner.wait_cleanup(1).complete


@pytest.mark.parametrize("kind", [KeyboardInterrupt, SystemExit])
def test_end_interruption_preserves_identity_and_attempts_independent_notifications(
    kind,
):
    stream, outcome = enums()
    error = kind("end notification")
    owner = launch(Child())
    calls = []

    def end(identity, terminal):
        calls.append((identity, terminal))
        if len(calls) == 1:
            raise error

    try:
        assert all(event.wait(1) for event in owner._stopped)
        with pytest.raises(kind) as caught:
            owner.stream(lambda *args: None, on_stream_end=end)
        assert caught.value is error and not hasattr(error, "process_result")
        assert len(calls) == 2 and set(calls) == {
            (identity, outcome.EOF) for identity in stream
        }
        selected = owner.cleanup_status.failures[0]
        assert (
            selected.error is error and selected.phase is process.FailurePhase.CALLBACK
        )
        assert selected.stream is calls[0][0]
        assert owner.wait_cleanup(1).complete and len(calls) == 2
    finally:
        owner.shutdown()
        assert owner.wait_cleanup(1).complete


def test_later_explicit_subscription_is_rejected_without_replay_or_restart():
    stream, outcome = enums()
    child = Child()
    owner = launch(child)
    try:
        first = owner.capture()
        before = (
            child.stdout.read_attempts,
            child.stderr.read_attempts,
            child.wait_attempts,
        )
        chunks, ends = [], []
        with (
            mock.patch.object(
                owner, "_read_chunk", side_effect=AssertionError("reread")
            ),
            mock.patch.object(
                owner, "_shutdown", side_effect=AssertionError("budget restart")
            ),
        ):
            with pytest.raises(RuntimeError):
                owner.stream(
                    lambda *args: chunks.append(args),
                    on_stream_end=lambda *args: ends.append(args),
                )
        assert chunks == ends == []
        assert first.stream_outcomes == (
            (stream.STDOUT, outcome.EOF),
            (stream.STDERR, outcome.EOF),
        )
        assert before == (
            child.stdout.read_attempts,
            child.stderr.read_attempts,
            child.wait_attempts,
        )
        assert child.stdout.close_attempts == child.stderr.close_attempts == 1
    finally:
        owner.shutdown()
        assert owner.wait_cleanup(1).complete
