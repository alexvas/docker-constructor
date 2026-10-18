"""Selected occurrence context replaces the obsolete parallel reader source."""

import threading
from dataclasses import FrozenInstanceError
from unittest import mock

import pytest

from docker.process import OwnedProcess, ProcessStream, StreamOutcome
from tests.test_process_stream_outcomes import Child, Pipe


@pytest.mark.parametrize("method", ["capture", "stream"])
@pytest.mark.parametrize("position", [0, 1])
@pytest.mark.parametrize("kind", [OSError, RuntimeError, KeyboardInterrupt])
def test_winning_read_error_preserves_identity_and_records_stream(
    method, position, kind
):
    error = kind("read failure")
    ready = threading.Event()
    pipes = [Pipe([b"stdout"], gate=ready), Pipe([b"stderr"], gate=ready)]
    pipes[position].error = error
    child = Child(stdout=pipes[0], stderr=pipes[1])
    owner = OwnedProcess.launch(
        ["child"],
        factory=lambda *a, **k: child,
        grace_seconds=0.1,
        on_launched=ready.set,
    )
    try:

        def collect():
            return (
                owner.capture()
                if method == "capture"
                else owner.stream(lambda *args: None)
            )

        if kind is KeyboardInterrupt:
            with pytest.raises(KeyboardInterrupt) as caught:
                collect()
            assert caught.value is error
            lifecycle = owner.cleanup_status
        else:
            result = collect()
            lifecycle = result.lifecycle
            assert not hasattr(result, "reader_failure_stream")
            assert (
                dict(result.stream_outcomes)[tuple(ProcessStream)[position]]
                is StreamOutcome.READ_FAILED
            )
        selected = lifecycle.failures[0]
        assert selected.error is error
        assert (
            selected.phase.name == "READ"
            and selected.stream is tuple(ProcessStream)[position]
        )
        assert (
            selected.kind.name
            == {
                OSError: "ORDINARY",
                RuntimeError: "UNEXPECTED",
                KeyboardInterrupt: "INTERRUPTION",
            }[kind]
        )
        with pytest.raises(FrozenInstanceError):
            selected.stream = None
        with pytest.raises(RuntimeError):
            owner.stream(lambda *args: pytest.fail("no byte replay"))
        assert owner.cleanup_status.failures[0] == selected
        calls = []
        interruption = KeyboardInterrupt("rejected subscriber")

        def interrupt(*args):
            calls.append(True)
            raise interruption

        with pytest.raises(RuntimeError):
            owner.stream(lambda *args: None, on_stream_end=interrupt)
        assert calls == [] and not hasattr(interruption, "process_result")
        assert not hasattr(error, "process_result")
    finally:
        owner.shutdown()
        assert owner.wait_cleanup(1).complete
        assert all(pipe.close_attempts == 1 for pipe in pipes)


@pytest.mark.parametrize("same_object", [False, True])
@pytest.mark.parametrize("position", [0, 1])
def test_join_defect_has_join_context_even_if_exception_object_is_reused(
    position, same_object
):
    reader_error = OSError("read failure")
    join_error = reader_error if same_object else OSError("join defect")
    ready = threading.Event()
    child = Child(
        stdout=Pipe([b"stdout"], error=reader_error, gate=ready),
        stderr=Pipe([b"stderr"], gate=ready),
    )
    original = OwnedProcess._join_reader
    injected = False

    def join(owner, index, timeout):
        nonlocal injected
        if index == position and not injected:
            injected = True
            raise join_error
        original(owner, index, timeout)

    owner = None
    try:
        with mock.patch.object(OwnedProcess, "_join_reader", join):
            owner = OwnedProcess.launch(
                ["child"],
                factory=lambda *a, **k: child,
                grace_seconds=0.1,
                on_launched=ready.set,
            )
            result = owner.capture()
        selected = result.lifecycle.failures[0]
        assert selected.error is join_error and selected.kind.name == "UNEXPECTED"
        assert (
            selected.phase.name == "JOIN"
            and selected.stream is tuple(ProcessStream)[position]
        )
        assert any(f.error is reader_error for f in owner.cleanup_status.failures)
        assert not hasattr(join_error, "process_result")
    finally:
        if owner is not None:
            owner.shutdown()
            assert owner.wait_cleanup(1).complete
        assert child.stdout.close_attempts == child.stderr.close_attempts == 1


@pytest.mark.parametrize("winner_position", [0, 1])
def test_competing_read_errors_identify_selected_reader_not_stream_order(
    winner_position,
):
    ready, recorded = threading.Event(), threading.Barrier(2)
    errors = [OSError("ordinary read"), OSError("ordinary read")]
    errors[winner_position] = RuntimeError("unexpected read defect")
    pipes = [Pipe([b"data"], gate=ready, error=error) for error in errors]
    child = Child(stdout=pipes[0], stderr=pipes[1])
    original = OwnedProcess._shutdown

    def shutdown(owner, *args, **kwargs):
        if (
            threading.current_thread() in owner._readers
            and kwargs.get("reason") == "reader"
        ):
            recorded.wait(1)
        return original(owner, *args, **kwargs)

    owner = None
    try:
        with mock.patch.object(OwnedProcess, "_shutdown", shutdown):
            owner = OwnedProcess.launch(
                ["child"],
                factory=lambda *a, **k: child,
                grace_seconds=0.1,
                on_launched=ready.set,
            )
            result = owner.capture()
        selected = result.lifecycle.failures[0]
        assert (
            selected.error is errors[winner_position] and selected.phase.name == "READ"
        )
        assert selected.stream is tuple(ProcessStream)[winner_position]
        assert all(
            any(f.error is error for f in owner.cleanup_status.failures)
            for error in errors
        )
    finally:
        ready.set()
        recorded.abort()
        if owner is not None:
            owner.shutdown()
            assert owner.wait_cleanup(1).complete
        assert all(pipe.close_attempts == 1 for pipe in pipes)


def test_successful_result_has_no_selected_failure_or_parallel_reader_source():
    child = Child(stdout=Pipe([b"stdout"]), stderr=Pipe([b"stderr"]))
    owner = OwnedProcess.launch(
        ["child"], factory=lambda *a, **k: child, grace_seconds=0.1
    )
    try:
        result = owner.capture()
        assert result.lifecycle.failures == () and not hasattr(
            result, "reader_failure_stream"
        )
    finally:
        owner.shutdown()
        assert owner.wait_cleanup(1).complete
        assert child.stdout.close_attempts == child.stderr.close_attempts == 1
