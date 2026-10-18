"""One collection claim and launch/value-failure boundaries (Phase 6BC.7)."""

import io
import threading

import pytest

import docker.process as process


class Pipe(io.BytesIO):
    def __init__(self, data=b"", *, close_error=None):
        super().__init__(data)
        self.close_calls = 0
        self.close_error = close_error

    def close(self):
        self.close_calls += 1
        super().close()
        if self.close_error is not None:
            raise self.close_error


class Child:
    def __init__(self, *, error=None, position=0):
        self.stdout = Pipe(b"stdout", close_error=error if position == 0 else None)
        self.stderr = Pipe(b"stderr", close_error=error if position == 1 else None)
        self.returncode = 0
        self.wait_calls = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.wait_calls += 1
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


def launch(child, **kwargs):
    return process.OwnedProcess.launch(
        ["fixture"], factory=lambda *a, **kw: child, grace_seconds=0.2, **kwargs
    )


def collect(owner, method, events=None):
    if method == "capture":
        return owner.capture()
    events = [] if events is None else events
    return owner.stream(
        lambda *event: events.append(event),
        on_stream_end=lambda *event: events.append(event),
    )


def settle(owner):
    owner.shutdown()
    status = owner.wait_cleanup(1)
    assert status.complete


@pytest.mark.parametrize("first", ["capture", "stream"])
@pytest.mark.parametrize("second", ["capture", "stream"])
def test_reject_repeated_and_mixed_collection_without_work(first, second):
    child = Child()
    owner = launch(child)
    try:
        collect(owner, first)
        events = []
        waits = child.wait_calls
        with pytest.raises(RuntimeError, match="collect|capture|stream|attempt"):
            collect(owner, second, events)
        assert events == []
        assert child.wait_calls == waits
        assert child.stdout.close_calls == child.stderr.close_calls == 1
        assert not hasattr(owner, "result")
    finally:
        settle(owner)


@pytest.mark.parametrize("method", ["capture", "stream"])
@pytest.mark.parametrize("position", [0, 1])
@pytest.mark.parametrize(
    "error", [OSError("ordinary release"), RuntimeError("unexpected release")]
)
def test_failure_is_returned_as_value_and_cannot_be_recollected(
    method, position, error
):
    child = Child(error=error, position=position)
    owner = launch(child)
    try:
        result = collect(owner, method)
        assert isinstance(result, process.ProcessResult)
        assert result.stdout == b"stdout" and result.stderr == b"stderr"
        assert result.returncode == 0 and not result.timed_out
        assert result.lifecycle.complete
        selected = result.lifecycle.failures[0]
        assert selected.error is error
        assert selected.kind.name == (
            "ORDINARY" if isinstance(error, OSError) else "UNEXPECTED"
        )
        assert selected.phase.name == "RELEASE"
        assert selected.stream is tuple(process.ProcessStream)[position]
        assert not hasattr(error, "process_result")
        notes = tuple(getattr(error, "__notes__", ()))
        with pytest.raises(RuntimeError):
            collect(owner, method)
        late = owner.wait_cleanup(0.1)
        assert late.complete and late.failures[0].error is error
        assert tuple(getattr(error, "__notes__", ())) == notes
        assert child.stdout.close_calls == child.stderr.close_calls == 1
    finally:
        settle(owner)


@pytest.mark.parametrize("method", ["capture", "stream"])
@pytest.mark.parametrize(
    "error",
    [
        KeyboardInterrupt("first"),
        SystemExit(3),
        GeneratorExit(),
        BaseException("control"),
    ],
)
def test_interruption_claim_is_terminal_and_has_no_generic_output_snapshot(
    method, error
):
    child = Child(error=error)
    owner = launch(child)
    try:
        with pytest.raises(BaseException) as caught:
            collect(owner, method)
        assert caught.value is error
        assert not hasattr(error, "process_result")
        assert not hasattr(owner, "result")
        events = []
        with pytest.raises(RuntimeError):
            collect(owner, "stream", events)
        assert events == []
        assert child.stdout.close_calls == child.stderr.close_calls == 1
        status = owner.wait_cleanup(0.1)
        assert status.complete and status.failures[0].error is error
    finally:
        settle(owner)


@pytest.mark.parametrize("bad_args", ["chunk", "end"])
def test_callback_validation_does_not_consume_claim(bad_args):
    child = Child()
    owner = launch(child)
    try:
        with pytest.raises(TypeError):
            if bad_args == "chunk":
                owner.stream(None)
            else:
                owner.stream(lambda *args: None, on_stream_end=object())
        result = owner.capture()
        assert result.stdout == b"stdout"
    finally:
        settle(owner)


def test_late_cleanup_is_a_new_observation_not_a_changed_collection_value():
    started, resume = threading.Event(), threading.Event()
    error = OSError("late release")
    child = Child(error=error)
    close = child.stdout.close

    def blocked_close():
        started.set()
        assert resume.wait(3)
        close()

    child.stdout.close = blocked_close
    owner = launch(child)
    try:
        result = owner.capture()
        assert started.is_set()
        assert not result.lifecycle.complete
        saved = (
            result.stdout,
            result.stderr,
            result.returncode,
            result.lifecycle,
            result.stream_outcomes,
        )
        failures = result.lifecycle.failures
        assert all(f.error is not error for f in failures)
        notes = [tuple(getattr(f.error, "__notes__", ())) for f in failures]
        resume.set()
        late = owner.wait_cleanup(1)
        assert late.complete and any(f.error is error for f in late.failures)
        assert saved == (
            result.stdout,
            result.stderr,
            result.returncode,
            result.lifecycle,
            result.stream_outcomes,
        )
        assert notes == [tuple(getattr(f.error, "__notes__", ())) for f in failures]
        with pytest.raises(RuntimeError):
            owner.capture()
        assert child.stdout.close_calls == child.stderr.close_calls == 1
    finally:
        resume.set()
        settle(owner)


@pytest.mark.parametrize("method", ["capture", "stream"])
def test_factory_failure_after_preparation_returns_same_owner(monkeypatch, method):
    error = OSError("factory unavailable")
    prepared = []
    real = process._make_continuation

    def continuation(action):
        prepared.append(action.__self__)
        return real(action)

    def factory(*args, **kwargs):
        raise error

    monkeypatch.setattr(process, "_make_continuation", continuation)
    owner = process.OwnedProcess.launch(["fixture"], factory=factory, grace_seconds=0.2)
    try:
        assert owner is prepared[0]
        result = collect(owner, method)
        assert result.stdout == result.stderr == b"" and result.returncode is None
        assert result.lifecycle.complete
        assert result.lifecycle.failures[0].error is error
        assert result.lifecycle.failures[0].phase.name == "SETUP"
        assert not hasattr(error, "process_result")
        assert not owner._continuation.is_alive()
    finally:
        settle(owner)


@pytest.mark.parametrize("position", [0, 1])
def test_setup_failure_returns_prepared_owner_and_releases_independently(
    monkeypatch, position
):
    error = OSError("transfer")
    prepared = []
    transfer = process.OwnedProcess._transfer

    def fail(self, index):
        if not prepared:
            prepared.append(self)
        if index == position:
            raise error
        return transfer(self, index)

    monkeypatch.setattr(process.OwnedProcess, "_transfer", fail)
    child = Child()
    owner = launch(child)
    try:
        assert owner is prepared[0]
        result = owner.capture()
        assert result.lifecycle.failures[0].error is error
        assert result.lifecycle.failures[0].phase.name == "SETUP"
        assert (
            result.lifecycle.failures[0].stream
            is tuple(process.ProcessStream)[position]
        )
        assert child.stdout.close_calls == child.stderr.close_calls == 1
    finally:
        settle(owner)


@pytest.mark.parametrize("seam", ["_new_lock", "_new_event", "_make_continuation"])
def test_preparation_failure_raises_before_acquisition(monkeypatch, seam):
    error = RuntimeError("preparation")
    launched = []

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(process, seam, fail)
    with pytest.raises(RuntimeError) as caught:
        process.OwnedProcess.launch(
            ["fixture"], factory=lambda *a, **kw: launched.append(True)
        )
    assert caught.value is error
    assert not hasattr(error, "process_result")
    assert launched == []


@pytest.mark.parametrize(
    "seam,position",
    [
        ("_make_reader", 0),
        ("_make_reader", 1),
        ("_configure_reader", 0),
        ("_configure_reader", 1),
        ("_start_reader", 0),
        ("_start_reader", 1),
        ("_start_supervisor", None),
    ],
)
def test_ordinary_reader_setup_failure_is_a_value(monkeypatch, seam, position):
    error = OSError("reader setup")
    original = getattr(process.OwnedProcess, seam)

    def fail(self, *args):
        if position is None or args[0] == position:
            raise error
        return original(self, *args)

    monkeypatch.setattr(process.OwnedProcess, seam, fail)
    child = Child()
    owner = launch(child)
    try:
        result = owner.capture()
        assert result.lifecycle.complete
        failure = result.lifecycle.failures[0]
        assert failure.error is error and failure.phase.name == "SETUP"
        expected = (
            tuple(process.ProcessStream)[position] if position is not None else None
        )
        assert failure.stream is expected
        assert child.stdout.close_calls == child.stderr.close_calls == 1
        assert not hasattr(error, "process_result")
    finally:
        settle(owner)


def test_validation_failure_does_not_launch_or_fabricate_owner():
    called = []
    with pytest.raises(ValueError):
        process.OwnedProcess.launch([], factory=lambda *a, **kw: called.append(True))
    assert called == []


@pytest.mark.parametrize(
    "first,second",
    [
        ("capture", "capture"),
        ("capture", "stream"),
        ("stream", "capture"),
        ("stream", "stream"),
    ],
)
def test_racing_loser_is_rejected_before_waiting_or_callbacks(
    monkeypatch, first, second
):
    entered, resume, loser_done = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    child = Child()
    owner = launch(child)
    results, errors, loser_errors, events = [], [], [], []
    original = process.OwnedProcess._collect

    def blocked(self, *args, **kwargs):
        entered.set()
        assert resume.wait(2)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(process.OwnedProcess, "_collect", blocked)

    def winner():
        try:
            results.append(collect(owner, first))
        except BaseException as exc:
            errors.append(exc)

    def loser():
        try:
            collect(owner, second, events)
        except BaseException as exc:
            loser_errors.append(exc)
        finally:
            loser_done.set()

    one = threading.Thread(target=winner)
    two = threading.Thread(target=loser)
    try:
        one.start()
        assert entered.wait(1)
        two.start()
        # The loser must reject while the winner remains blocked, not queue
        # behind an operation lock and later become another subscription.
        assert loser_done.wait(0.2), "racing collection waited instead of rejecting"
        assert len(loser_errors) == 1 and isinstance(loser_errors[0], RuntimeError)
        assert events == []
    finally:
        resume.set()
        one.join(3)
        if two.ident is not None:
            two.join(3)
        settle(owner)
        assert not one.is_alive() and not two.is_alive()
    assert errors == [] and len(results) == 1


def test_racing_loser_does_not_wait_on_lifecycle_state_lock(monkeypatch):
    entered, resume, rejected = threading.Event(), threading.Event(), threading.Event()
    owner = launch(Child())
    original = process.OwnedProcess._collect
    failures = []

    def blocked(self, *args, **kwargs):
        entered.set()
        assert resume.wait(3)
        return original(self, *args, **kwargs)

    def first():
        try:
            owner.capture()
        except BaseException as exc:
            failures.append(exc)

    def second():
        try:
            owner.capture()
        except RuntimeError:
            rejected.set()
        except BaseException as exc:
            failures.append(exc)

    monkeypatch.setattr(process.OwnedProcess, "_collect", blocked)
    winner, loser = threading.Thread(target=first), threading.Thread(target=second)
    locked = False
    try:
        winner.start()
        assert entered.wait(1)
        owner._lock.acquire()
        locked = True
        loser.start()
        assert rejected.wait(0.2), "usage rejection waited on lifecycle publication"
    finally:
        if locked:
            owner._lock.release()
        resume.set()
        for worker in (winner, loser):
            if worker.ident is not None:
                worker.join(3)
        settle(owner)
    assert failures == [] and not winner.is_alive() and not loser.is_alive()


@pytest.mark.parametrize(
    "primary", [ValueError("active body"), KeyboardInterrupt("active body")]
)
@pytest.mark.parametrize("close_error", [OSError("release"), RuntimeError("release")])
def test_context_before_collection_delivery_still_installs_active_primary(
    monkeypatch, primary, close_error
):
    entered, resume = threading.Event(), threading.Event()
    child = Child(error=close_error)
    owner = launch(child)
    original = process.OwnedProcess._collect
    results, failures = [], []

    def blocked(self, *args, **kwargs):
        entered.set()
        assert resume.wait(3)
        return original(self, *args, **kwargs)

    def collect_in_thread():
        try:
            results.append(owner.capture())
        except BaseException as exc:
            failures.append(exc)

    monkeypatch.setattr(process.OwnedProcess, "_collect", blocked)
    worker = threading.Thread(target=collect_in_thread)
    expected = (
        primary
        if not isinstance(primary, Exception) or isinstance(close_error, OSError)
        else close_error
    )
    try:
        worker.start()
        assert entered.wait(1) and not owner._collection_delivered
        with pytest.raises(BaseException) as caught:
            owner.__exit__(type(primary), primary, None)
        assert caught.value is expected
        assert owner.cleanup_status.failures[0].error is expected
    finally:
        resume.set()
        worker.join(3)
        settle(owner)
    assert not worker.is_alive()
    if isinstance(expected, Exception):
        assert failures == [] and len(results) == 1
        assert results[0].lifecycle.failures[0].error is expected
    else:
        assert failures == [expected] and results == []
    assert child.stdout.close_calls == child.stderr.close_calls == 1
