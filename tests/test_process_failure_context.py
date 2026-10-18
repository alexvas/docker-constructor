"""Phase 6BC.8: authority identity, boundary context and independent cleanup."""

import threading

import pytest

import docker.process as process
from tests.test_process_single_collection import Child, collect, launch, settle


def entries(result):
    return result.failures if hasattr(result, "complete") else result.lifecycle.failures


@pytest.mark.parametrize("position", [0, 1])
@pytest.mark.parametrize("primary_name", [None, "ordinary", "interrupt"])
@pytest.mark.parametrize("cleanup_name", ["ordinary", "unexpected", "interrupt"])
def test_operation_plus_release_and_repeat_cleanup(
    position, primary_name, cleanup_name
):
    primary = {
        None: None,
        "ordinary": ValueError("operation"),
        "interrupt": KeyboardInterrupt("operation"),
    }[primary_name]
    error = {
        "ordinary": OSError("release"),
        "unexpected": RuntimeError("release"),
        "interrupt": SystemExit(9),
    }[cleanup_name]
    child = Child(error=error, position=position)
    owner = launch(child)
    try:
        status = owner.shutdown(primary=primary)
        failures = entries(status)
        winner = (
            primary
            if primary_name == "interrupt"
            or (primary_name == "ordinary" and cleanup_name == "ordinary")
            else error
        )
        assert failures[0].error is winner
        assert len({id(f.error) for f in failures}) == len(failures)
        assert {id(f.error) for f in failures} == {
            id(e) for e in (primary, error) if e is not None
        }
        failure = next(f for f in failures if f.error is error)
        assert failure.phase.name == "RELEASE"
        assert failure.stream is tuple(process.ProcessStream)[position]
        assert (
            failure.kind.name
            == {
                "ordinary": "ORDINARY",
                "unexpected": "UNEXPECTED",
                "interrupt": "INTERRUPTION",
            }[cleanup_name]
        )
        if primary is not None:
            original = next(f for f in failures if f.error is primary)
            assert original.phase.name == "COORDINATION" and original.stream is None
        assert status.complete
        before = [(f.error, tuple(getattr(f.error, "__notes__", ()))) for f in failures]
        for _ in range(2):
            assert entries(owner.shutdown()) == failures
            assert entries(owner.wait_cleanup(0.1)) == failures
        assert before == [
            (f.error, tuple(getattr(f.error, "__notes__", ()))) for f in failures
        ]
        assert child.stdout.close_calls == child.stderr.close_calls == 1
        assert child.wait_calls > 0
    finally:
        settle(owner)


@pytest.mark.parametrize("position", [0, 1])
@pytest.mark.parametrize("primary_name", ["ordinary", "interrupt"])
def test_join_oserror_is_unexpected_and_never_prevents_reaping(
    monkeypatch, position, primary_name
):
    primary = (
        ValueError("operation")
        if primary_name == "ordinary"
        else KeyboardInterrupt("operation")
    )
    error = OSError("join defect")
    child = Child()
    owner = launch(child)
    original = process.OwnedProcess._join_reader
    joins = []

    def join(self, index, timeout):
        joins.append(index)
        original(self, index, timeout)
        if index == position:
            raise error

    monkeypatch.setattr(process.OwnedProcess, "_join_reader", join)
    try:
        status = owner.shutdown(primary=primary)
        failures = entries(status)
        assert failures[0].error is (primary if primary_name == "interrupt" else error)
        info = next(f for f in failures if f.error is error)
        assert info.kind.name == "UNEXPECTED" and info.phase.name == "JOIN"
        assert info.stream is tuple(process.ProcessStream)[position]
        assert joins == [0, 1]
        assert child.wait_calls > 0
        assert child.stdout.close_calls == child.stderr.close_calls == 1
    finally:
        settle(owner)


def test_wait_oserror_is_ordinary_wait_context_with_independent_release(monkeypatch):
    error = OSError("wait")
    original = process.OwnedProcess._wait_child
    calls = []

    def wait(self, timeout):
        calls.append(timeout)
        if len(calls) == 1:
            raise error
        return original(self, timeout)

    monkeypatch.setattr(process.OwnedProcess, "_wait_child", wait)
    child = Child()
    owner = launch(child)
    try:
        result = owner.capture()
        selected = entries(result)[0]
        assert selected.error is error
        assert (
            selected.kind.name == "ORDINARY"
            and selected.phase.name == "WAIT"
            and selected.stream is None
        )
        assert child.stdout.close_calls == child.stderr.close_calls == 1
        assert owner.wait_cleanup(1).complete
    finally:
        settle(owner)


@pytest.mark.parametrize("first", [0, 1])
@pytest.mark.parametrize("reuse", [False, True])
def test_competing_readers_and_same_object_join_follow_winning_occurrence(
    monkeypatch, first, reuse
):
    recorded = threading.Event()
    errors = [OSError("stdout read"), OSError("stderr read")]
    join_error = errors[first]
    original_shutdown = process.OwnedProcess._shutdown
    original_join = process.OwnedProcess._join_reader

    def read(self, index):
        if index != first:
            assert recorded.wait(2)
        raise errors[index]

    def shutdown(self, **kwargs):
        if kwargs.get("reason") == "reader":
            recorded.set()  # Reader publication preceded this coordinator call.
        return original_shutdown(self, **kwargs)

    def join(self, index, timeout):
        original_join(self, index, timeout)
        if reuse and index == first:
            raise join_error

    monkeypatch.setattr(process.OwnedProcess, "_read_chunk", read)
    monkeypatch.setattr(process.OwnedProcess, "_shutdown", shutdown)
    monkeypatch.setattr(process.OwnedProcess, "_join_reader", join)
    child = Child()
    owner = launch(child)
    try:
        result = owner.capture()
        failures = entries(result)
        selected = failures[0]
        assert selected.error is errors[first]
        assert selected.phase.name == ("JOIN" if reuse else "READ")
        assert selected.kind.name == ("UNEXPECTED" if reuse else "ORDINARY")
        assert selected.stream is tuple(process.ProcessStream)[first]
        assert len(failures) == 2 and {id(f.error) for f in failures} == {
            id(e) for e in errors
        }
        assert child.stdout.close_calls == child.stderr.close_calls == 1
    finally:
        recorded.set()
        settle(owner)


@pytest.mark.parametrize(
    "error",
    [
        KeyboardInterrupt("launch"),
        SystemExit(4),
        GeneratorExit(),
        BaseException("launch"),
    ],
)
def test_launch_control_flow_keeps_identity_without_snapshot(monkeypatch, error):
    owners = []
    original = process._make_continuation

    def prepare(action):
        owners.append(action.__self__)
        return original(action)

    def interrupt():
        raise error

    monkeypatch.setattr(process, "_make_continuation", prepare)
    child = Child()
    with pytest.raises(BaseException) as caught:
        launch(child, on_launched=interrupt)
    assert caught.value is error
    assert not hasattr(error, "process_result")
    assert child.stdout.close_calls == child.stderr.close_calls == 1
    assert owners[0].wait_cleanup(1).complete
    assert not owners[0]._continuation.is_alive()


@pytest.mark.parametrize("method", ["capture", "stream"])
@pytest.mark.parametrize("position", [0, 1])
@pytest.mark.parametrize(
    "error",
    [KeyboardInterrupt("wait"), SystemExit(8), GeneratorExit(), BaseException("wait")],
)
def test_coordinator_wait_keeps_first_interruption_and_typed_secondaries(
    monkeypatch, method, position, error
):
    later = KeyboardInterrupt("later")
    close_error = RuntimeError("release defect")
    child = Child(error=close_error, position=position)
    owner = launch(child)
    original = owner._phase_done.wait
    waits = []

    def wait(timeout=None):
        waits.append(timeout)
        if len(waits) == 1:
            raise error
        if len(waits) == 2:
            raise later
        return original(timeout)

    monkeypatch.setattr(owner._phase_done, "wait", wait)
    try:
        with pytest.raises(BaseException) as caught:
            collect(owner, method)
        assert caught.value is error and not hasattr(error, "process_result")
        status = owner.wait_cleanup(1)
        failures = entries(status)
        assert failures[0].error is error
        for interrupt in (error, later):
            info = next(f for f in failures if f.error is interrupt)
            assert (
                info.kind.name == "INTERRUPTION"
                and info.phase.name == "COORDINATION"
                and info.stream is None
            )
        assert any(f.error is close_error for f in failures)
        assert child.stdout.close_calls == child.stderr.close_calls == 1
        assert child.wait_calls > 0
    finally:
        settle(owner)


@pytest.mark.parametrize(
    "operation", [ValueError("operation"), KeyboardInterrupt("operation")]
)
def test_publication_gap_refreshes_authority_without_mutating_old_value(
    monkeypatch, operation
):
    close_error = OSError("release")
    child = Child(error=close_error)
    owner = launch(child)
    published, resume, merged = threading.Event(), threading.Event(), threading.Event()
    real_set, real_wait = owner._phase_done.set, owner._phase_done.wait
    results, errors = [], []

    def set_done():
        published.set()
        assert resume.wait(3)
        real_set()

    def wait_done(timeout=None):
        if threading.current_thread().name == "orthogonal-gap":
            merged.set()
        return real_wait(timeout)

    def deliver():
        try:
            results.append(owner.shutdown(primary=operation))
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(owner._phase_done, "set", set_done)
    monkeypatch.setattr(owner._phase_done, "wait", wait_done)
    caller = threading.Thread(target=deliver, name="orthogonal-gap")
    try:
        owner._deadline = 0.01
        assert published.wait(1)
        old = owner.cleanup_status
        assert old.complete and entries(old)[0].error is close_error
        caller.start()
        assert merged.wait(1)
        assert not owner._phase_done.is_set()
        resume.set()
        caller.join(2)
        assert errors == [] and len(results) == 1
        assert entries(results[0])[0].error is operation
        assert entries(results[0])[0].phase.name == "COORDINATION"
        assert [f.error for f in entries(results[0])] == [operation, close_error]
        assert [f.error for f in entries(old)] == [close_error]
    finally:
        resume.set()
        if caller.ident is not None:
            caller.join(2)
        settle(owner)
        assert not caller.is_alive()


def test_interruptederror_release_is_not_control_flow():
    error = InterruptedError("ordinary interrupted syscall")
    child = Child(error=error)
    owner = launch(child)
    try:
        result = owner.capture()
        info = entries(result)[0]
        assert info.error is error and info.kind.name == "ORDINARY"
        assert info.phase.name == "RELEASE"
    finally:
        settle(owner)


@pytest.mark.parametrize("error", [OSError("ordinary"), RuntimeError("defect")])
def test_context_does_not_rethrow_returned_failure(error):
    child = Child(error=error)
    owner = launch(child)
    try:
        with owner:
            result = owner.capture()
            assert entries(result)[0].error is error
        assert not hasattr(error, "process_result")
    finally:
        settle(owner)
