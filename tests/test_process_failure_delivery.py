"""Value delivery, real-reader retention and late observation isolation."""

import subprocess
import sys
import threading
from unittest import mock

import pytest

import docker.process as process
from docker.filesystem.streams import OwnedStream
from docker.process import CleanupIncomplete, OwnedProcess, ProcessResult
from tests.test_process_failure_snapshot import Child
from tests.test_process_stream_outcomes import Child as OutcomeChild, Pipe


def collect(owner, method, callback=None, end=None):
    return (
        owner.capture()
        if method == "capture"
        else owner.stream(callback or (lambda *a: None), on_stream_end=end)
    )


@pytest.mark.parametrize("method", ["capture", "stream"])
@pytest.mark.parametrize("kind", [OSError, ValueError, KeyboardInterrupt])
def test_operation_failure_identity_and_bounded_known_output(method, kind):
    child = Child()
    error = kind("operation observation")
    owner = OwnedProcess.launch(
        ["child"], factory=lambda *a, **k: child, capture_limit=6, grace_seconds=0.1
    )
    try:
        assert all(event.wait(1) for event in owner._stopped)
        with mock.patch.object(child, "poll", side_effect=error):
            if kind is KeyboardInterrupt:
                with pytest.raises(KeyboardInterrupt) as caught:
                    collect(owner, method)
                assert caught.value is error
                status = owner.cleanup_status
            else:
                result = collect(owner, method)
                assert type(result) is ProcessResult
                assert (result.stdout, result.stderr, result.returncode) == (
                    b"stdout",
                    b"stderr",
                    7,
                )
                status = result.lifecycle
        assert (
            status.failures[0].error is error
            and status.failures[0].phase is process.FailurePhase.WAIT
        )
        assert not hasattr(error, "process_result")
        assert child.stdout.attempts == child.stderr.attempts == 1
    finally:
        owner.shutdown()
        assert owner.wait_cleanup(1).complete


@pytest.mark.parametrize("method", ["capture", "stream"])
@pytest.mark.parametrize("kind", [OSError, RuntimeError, KeyboardInterrupt])
def test_reader_failure_retains_clean_sibling_output_and_exact_authority(method, kind):
    release = threading.Event()
    error = kind("read")
    child = OutcomeChild(
        stdout=Pipe([b"retained stdout"]), stderr=Pipe(gate=release, error=error)
    )
    owner = OwnedProcess.launch(
        ["child"], factory=lambda *a, **k: child, capture_limit=6, grace_seconds=0.1
    )
    try:
        assert owner._stopped[0].wait(1)
        release.set()
        if kind is KeyboardInterrupt:
            with pytest.raises(KeyboardInterrupt) as caught:
                collect(owner, method)
            assert caught.value is error
            status = owner.cleanup_status
        else:
            result = collect(owner, method)
            assert type(result) is ProcessResult
            assert (result.stdout, result.stderr, result.returncode) == (
                b"stdout",
                b"",
                7,
            )
            status = result.lifecycle
        assert status.failures[0].error is error
        assert status.failures[0].phase is process.FailurePhase.READ
        assert status.failures[0].stream is process.ProcessStream.STDERR
        assert not hasattr(error, "process_result")
        assert child.stdout.close_attempts == child.stderr.close_attempts == 1
        assert owner.wait_cleanup(1).complete
    finally:
        release.set()
        owner.shutdown()
        assert owner.wait_cleanup(1).complete


@pytest.mark.parametrize("method", ["capture", "stream"])
@pytest.mark.parametrize("kind", [OSError, RuntimeError, KeyboardInterrupt])
def test_wait_failure_retains_bounded_output_and_known_returncode(method, kind):
    child = Child()
    error = kind("wait")
    owner = OwnedProcess.launch(
        ["child"], factory=lambda *a, **k: child, capture_limit=6, grace_seconds=0.1
    )
    try:
        assert all(event.wait(1) for event in owner._stopped)
        with mock.patch.object(child, "wait", side_effect=[error, 7]):
            if kind is KeyboardInterrupt:
                with pytest.raises(KeyboardInterrupt) as caught:
                    collect(owner, method)
                assert caught.value is error
                status = owner.cleanup_status
            else:
                result = collect(owner, method)
                assert (result.stdout, result.stderr, result.returncode) == (
                    b"stdout",
                    b"stderr",
                    child.returncode,
                )
                assert result.returncode is not None
                status = result.lifecycle
        assert (
            status.failures[0].error is error
            and status.failures[0].phase is process.FailurePhase.WAIT
        )
        assert not hasattr(error, "process_result")
        assert child.stdout.attempts == child.stderr.attempts == 1
        assert owner.wait_cleanup(1).complete
    finally:
        owner.shutdown()
        assert owner.wait_cleanup(1).complete


@pytest.mark.parametrize("method", ["capture", "stream"])
def test_real_live_reader_value_and_late_release_are_isolated(method):
    entered, release_reader, reader_returned = (threading.Event() for _ in range(3))
    late_error = OSError("late release defect")
    original_read, original_close = OwnedProcess._read_chunk, OwnedStream.close
    children = []

    def factory(*args, **kwargs):
        child = subprocess.Popen(*args, **kwargs)
        children.append(child)
        return child

    def read(owner, index):
        if index == 0:
            entered.set()
            assert release_reader.wait(5), "test-controlled live reader leaked"
            reader_returned.set()
            return b"late output"
        return original_read(owner, index)

    def close(owner):
        if owner.label == "stdout":
            assert reader_returned.is_set(), "descriptor released beneath live reader"
            original_close(owner)
            raise late_error
        return original_close(owner)

    owner = None
    try:
        with (
            mock.patch.object(OwnedProcess, "_read_chunk", read),
            mock.patch.object(OwnedStream, "close", close),
        ):
            owner = OwnedProcess.launch(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                factory=factory,
                grace_seconds=0.05,
                deadline_seconds=0.05,
                capture_limit=6,
            )
            assert entered.wait(1)
            calls, ends = [], []
            snapshot = collect(
                owner,
                method,
                lambda *args: calls.append(args),
                lambda *args: ends.append(args),
            )
            assert type(snapshot) is ProcessResult and not snapshot.lifecycle.complete
            error = snapshot.lifecycle.failures[0].error
            assert isinstance(error, CleanupIncomplete) and not hasattr(
                error, "process_result"
            )
            assert (
                dict(snapshot.stream_outcomes)[process.ProcessStream.STDOUT]
                is process.StreamOutcome.ABORTED
            )
            if method == "stream":
                assert len(ends) == 2 and set(ends) == set(snapshot.stream_outcomes)
            assert not children[0].stdout.closed and children[0].returncode is not None
            assert not owner.wait_cleanup(0.01).complete
            delivered_notes = tuple(getattr(error, "__notes__", ()))
            delivered_calls, delivered_ends = tuple(calls), tuple(ends)
            release_reader.set()
            eventual = owner.wait_cleanup(1)
            assert eventual.complete and any(
                f.error is late_error for f in eventual.failures
            )
            assert children[0].stdout.closed and children[0].stderr.closed
            assert snapshot.stdout == b"" and not snapshot.lifecycle.complete
            assert tuple(getattr(error, "__notes__", ())) == delivered_notes
            assert tuple(calls) == delivered_calls and tuple(ends) == delivered_ends
            assert (
                dict(snapshot.stream_outcomes)[process.ProcessStream.STDOUT]
                is process.StreamOutcome.ABORTED
            )
            with pytest.raises(RuntimeError):
                collect(owner, method)
    finally:
        release_reader.set()
        if owner is not None:
            owner.shutdown()
            assert owner.wait_cleanup(1).complete
