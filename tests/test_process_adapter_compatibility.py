"""Generic compatibility corrections required by the captured/stream adapters."""

import io
import threading
from unittest import mock

import pytest

from docker.process import OwnedProcess, ProcessStream
from tests.test_process_failure_snapshot import Child


@pytest.mark.parametrize("method", ["capture", "stream"])
@pytest.mark.parametrize("kind", [KeyboardInterrupt, SystemExit, GeneratorExit])
@pytest.mark.parametrize("close_kind", [None, OSError, RuntimeError, KeyboardInterrupt])
@pytest.mark.parametrize("position", [0, 1])
def test_coordination_wait_interruption_retains_lifecycle_and_independent_cleanup(
    method, kind, close_kind, position
):
    error = kind("coordinator interrupted")
    close_error = close_kind("release failure") if close_kind is not None else None
    child = Child()
    pipe = (child.stdout, child.stderr)[position]
    pipe.error = close_error
    release = threading.Event()
    original_close = pipe.close

    def close():
        assert release.wait(2), "test-controlled cleanup was not released"
        original_close()

    pipe.close = close
    owner = OwnedProcess.launch(
        ["child"], factory=lambda *a, **k: child, grace_seconds=0.5
    )
    original_wait = owner._phase_done.wait
    interrupted = False

    def wait(timeout=None):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise error
        release.set()
        return original_wait(timeout)

    try:
        with mock.patch.object(owner._phase_done, "wait", side_effect=wait):
            with pytest.raises(kind) as caught:
                if method == "capture":
                    owner.capture()
                else:
                    owner.stream(lambda *args: None)
        assert caught.value is error
        status = owner.cleanup_status
        assert status.complete and status.failures[0].error is error
        assert (
            status.failures[0].phase.name == "COORDINATION"
            and status.failures[0].stream is None
        )
        if close_error is not None:
            assert sum(item.error is close_error for item in status.failures) == 1
        assert owner.shutdown().failures[0].error is error
        assert not hasattr(error, "process_result")
    finally:
        release.set()
        owner.shutdown()
        assert owner.wait_cleanup(1).complete
        assert child.stdout.attempts == child.stderr.attempts == 1


class DescriptorlessPipe:
    def __init__(self, error=None):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.close_attempts = 0
        self.error = error

    def fileno(self):
        raise io.UnsupportedOperation("no descriptor")

    def read(self, count):
        self.entered.set()
        assert self.release.wait(5), "controlled descriptor-less reader leaked"
        return b""

    def close(self):
        self.close_attempts += 1
        self.release.set()
        if self.error is not None:
            raise self.error


@pytest.mark.parametrize("primary_kind", [None, ValueError, KeyboardInterrupt])
@pytest.mark.parametrize("close_kind", [None, OSError, RuntimeError, KeyboardInterrupt])
@pytest.mark.parametrize("position", [0, 1])
def test_known_descriptorless_fileno_rejection_allows_sole_unblocking_release(
    primary_kind, close_kind, position
):
    error = close_kind("release failure") if close_kind is not None else None
    primary = primary_kind("operation failure") if primary_kind is not None else None
    pipes = [DescriptorlessPipe(), DescriptorlessPipe()]
    pipes[position].error = error
    child = Child()
    child.stdout, child.stderr = pipes
    owner = OwnedProcess.launch(
        ["child"], factory=lambda *a, **k: child, grace_seconds=0.1
    )
    try:
        assert all(pipe.entered.wait(1) for pipe in pipes)
        result = owner.shutdown(primary=primary)
        assert owner.wait_cleanup(1).complete
        assert child.stdout.close_attempts == child.stderr.close_attempts == 1
        selected = result.failures[0].error if result.failures else None
        if primary_kind is KeyboardInterrupt:
            assert selected is primary
        elif close_kind in (RuntimeError, KeyboardInterrupt):
            assert selected is error
        else:
            assert selected is (primary if primary is not None else error)
        if primary is not None:
            assert any(f.error is primary for f in result.failures)
        if error is not None:
            assert any(f.error is error for f in result.failures)
        repeated = owner.shutdown()
        assert (repeated.failures[0].error if repeated.failures else None) is selected
        assert all(pipe.close_attempts == 1 for pipe in pipes)
    finally:
        for pipe in pipes:
            pipe.release.set()
        owner.shutdown()
        assert owner.wait_cleanup(1).complete


def test_first_reader_failure_cannot_release_sibling_during_launch_setup():
    child = Child()
    error = OSError("first reader failed")
    entered = threading.Event()
    original_start = OwnedProcess._start_reader
    original_read = OwnedProcess._read_chunk

    def read(owner, index):
        if index == 0:
            entered.set()
            raise error
        return original_read(owner, index)

    def start(owner, index):
        original_start(owner, index)
        if index == 0:
            assert entered.wait(1)
            assert not owner._request.wait(0.1), (
                "reader cleanup raced unfinished sibling setup"
            )
            assert not owner._owners[1].released

    owner = None
    try:
        with (
            mock.patch.object(OwnedProcess, "_read_chunk", read),
            mock.patch.object(OwnedProcess, "_start_reader", start),
        ):
            owner = OwnedProcess.launch(
                ["child"], factory=lambda *a, **k: child, grace_seconds=0.3
            )
            result = owner.capture()
        assert result.lifecycle.failures[0].error is error
        assert result.lifecycle.failures[0].phase.name == "READ"
        assert result.lifecycle.failures[0].stream is not None
        assert not hasattr(error, "process_result")
    finally:
        if owner is not None:
            owner.shutdown()
            assert owner.wait_cleanup(1).complete
        assert child.stdout.attempts == child.stderr.attempts == 1


def test_descriptorless_text_factory_seam_is_normalized_to_bounded_bytes():
    child = Child()
    child.stdout, child.stderr = io.StringIO("stdout"), io.StringIO("stderr")
    owner = OwnedProcess.launch(
        ["child"],
        factory=lambda *a, **k: child,
        grace_seconds=0.1,
        deadline_seconds=0.1,
    )
    try:
        result = owner.capture()
        assert result.stdout == b"stdout" and result.stderr == b"stderr"
        assert not result.timed_out and result.lifecycle.complete
        assert child.stdout.closed and child.stderr.closed
    finally:
        owner.shutdown()
        assert owner.wait_cleanup(1).complete


def test_wait_and_release_errors_expose_distinct_typed_diagnostic_context():
    """Equal error text must not collapse WAIT and RELEASE provenance."""
    observations = []
    for source in ("wait", "release"):
        error = OSError("same operational failure")
        child = Child(error if source == "release" else None)
        original_wait = child.wait
        failed = False

        def wait(timeout=None):
            nonlocal failed
            if source == "wait" and not failed:
                failed = True
                raise error
            return original_wait(timeout)

        child.wait = wait
        child.kill = lambda: None  # fixture child has already exited with 7
        owner = OwnedProcess.launch(
            ["child"], factory=lambda *a, **k: child, grace_seconds=0.1
        )
        try:
            snapshot = owner.capture()
            assert snapshot.lifecycle.failures[0].error is error
            selected = snapshot.lifecycle.failures[0]
            assert selected.phase.name == ("WAIT" if source == "wait" else "RELEASE")
            assert selected.stream is (
                None if source == "wait" else ProcessStream.STDOUT
            )
            observations.append(
                (
                    snapshot.stdout,
                    snapshot.stderr,
                    snapshot.returncode,
                    snapshot.timed_out,
                    snapshot.stream_outcomes,
                    snapshot.lifecycle.complete,
                    tuple(
                        (type(item.error), item.error.args, item.kind)
                        for item in snapshot.lifecycle.failures
                    ),
                )
            )
            assert owner.shutdown().failures[0].error is error
        finally:
            owner.shutdown()
            assert owner.wait_cleanup(1).complete
            assert child.stdout.attempts == child.stderr.attempts == 1
    assert observations[0] == observations[1]


@pytest.mark.parametrize("kind", [None, OSError, RuntimeError, KeyboardInterrupt])
def test_wait_only_factory_observation_preserves_failure_and_independent_cleanup(kind):
    child = Child()
    child.poll = None
    error = kind("observation failure") if kind is not None else None
    original = child.wait
    raised = False

    def wait(timeout=None):
        nonlocal raised
        if error is not None and timeout == 0 and not raised:
            raised = True
            raise error
        return original(timeout)

    child.wait = wait
    owner = OwnedProcess.launch(
        ["child"], factory=lambda *a, **k: child, grace_seconds=0.1
    )
    try:
        if error is None:
            assert owner.capture().returncode == 7
        else:
            if isinstance(error, Exception):
                status = owner.capture().lifecycle
            else:
                with pytest.raises(BaseException) as caught:
                    owner.capture()
                assert caught.value is error
                status = owner.cleanup_status
            assert status.failures[0].error is error
            assert (
                status.failures[0].phase.name == "WAIT"
                and status.failures[0].stream is None
            )
            assert not hasattr(error, "process_result")
        assert owner.wait_cleanup(1).complete
        assert child.stdout.attempts == child.stderr.attempts == 1
        owner.shutdown()
        assert child.stdout.attempts == child.stderr.attempts == 1
    finally:
        owner.shutdown()
        assert owner.wait_cleanup(1).complete
