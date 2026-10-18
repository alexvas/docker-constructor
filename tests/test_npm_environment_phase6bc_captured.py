"""6BC.1 RED: captured input compatibility and returned ownership contracts."""

import dataclasses
import inspect
import sys
import threading
from unittest import mock

import pytest

from docker.filesystem.streams import OwnedStream
from docker.npm_environment import lifecycle
from docker.process import OwnedProcess
from tests.test_process_failure_snapshot import Child


def test_captured_inputs_and_outcome_fields_remain_unchanged():
    signature = inspect.signature(lifecycle.run_captured)
    assert tuple(signature.parameters) == ("argv", "policy")
    assert signature.parameters["policy"].kind is inspect.Parameter.KEYWORD_ONLY
    assert tuple(f.name for f in dataclasses.fields(lifecycle.LifecycleOutcome)) == (
        "return_code",
        "errors",
        "stdout",
        "stderr",
    )


def test_success_returns_same_generic_owner_and_bounded_decoded_output():
    child = Child()
    child.stdout.seek(0, 2)
    child.stdout.write(b"\xff")
    child.stdout.seek(0)
    owners = []
    original = OwnedProcess.launch

    def launch(*args, **kwargs):
        owner = original(*args, **kwargs)
        owners.append(owner)
        return owner

    with (
        mock.patch.object(lifecycle.subprocess, "Popen", return_value=child) as factory,
        mock.patch.object(OwnedProcess, "launch", side_effect=launch),
    ):
        owner, outcome = lifecycle.run_captured(
            ("client", "arg"),
            policy=lifecycle.LifecyclePolicy(0.1, captured_output_bytes=6),
        )
    assert len(owners) == 1 and owner is owners[0]
    assert isinstance(owner, OwnedProcess)
    assert outcome.return_code == 7 and outcome.errors == []
    assert (outcome.stdout, outcome.stderr) == ("tdout\ufffd", "stderr")
    assert owner.cleanup_status.complete
    assert child.stdout.attempts == child.stderr.attempts == 1
    assert factory.call_count == 1
    assert factory.call_args.kwargs["stdout"] is lifecycle.subprocess.PIPE
    assert factory.call_args.kwargs["stderr"] is lifecycle.subprocess.PIPE
    assert not hasattr(owner, "stdout") and not hasattr(owner, "stderr")


@pytest.mark.parametrize("kind", [OSError, RuntimeError, KeyboardInterrupt])
@pytest.mark.parametrize("source", ["release", "reader", "wait", "operation"])
def test_capture_failure_returns_same_owner_and_snapshot_without_recapture(
    kind, source
):
    error = kind(source)
    child = Child(error if source == "release" else None)
    if source == "reader":
        child.stderr.read = mock.Mock(side_effect=[b"retained stderr", error])
    if source == "wait":
        original_wait = child.wait
        failed = False

        def wait(timeout=None):
            nonlocal failed
            if not failed:
                failed = True
                raise error
            return original_wait(timeout)

        child.wait = wait
    owners = []
    original = OwnedProcess.launch

    def launch(*args, **kwargs):
        owner = original(*args, **kwargs)
        owners.append(owner)
        if source == "operation":
            assert all(event.wait(1) for event in owner._stopped)
            child.poll = mock.Mock(side_effect=error)
        return owner

    with (
        mock.patch.object(lifecycle.subprocess, "Popen", return_value=child),
        mock.patch.object(OwnedProcess, "launch", side_effect=launch),
        mock.patch.object(
            OwnedProcess, "capture", autospec=True, wraps=OwnedProcess.capture
        ) as capture,
    ):
        # autospec+wraps has no real method execution; bind its side effect to
        # the real implementation so the fault still comes from sole release.
        values = []

        def capture_once(owner):
            value = original_capture(owner)
            values.append(value)
            return value

        capture.side_effect = capture_once
        if kind is KeyboardInterrupt:
            with pytest.raises(KeyboardInterrupt) as caught:
                lifecycle.run_captured(
                    ("client",), policy=lifecycle.LifecyclePolicy(0.1)
                )
            assert caught.value is error
            owner = owners[0]
            assert values == [] and owner.cleanup_status.failures[0].error is error
        else:
            owner, outcome = lifecycle.run_captured(
                ("client",), policy=lifecycle.LifecyclePolicy(0.1)
            )
            assert outcome.errors[0] is error and len(values) == 1
            snapshot = values[0]
            assert outcome.return_code == snapshot.returncode == child.returncode
            assert (outcome.stdout, outcome.stderr) == (
                snapshot.stdout.decode(errors="replace"),
                snapshot.stderr.decode(errors="replace"),
            )
            assert outcome.stderr == "retained stderr"
            if source != "reader":
                assert outcome.stdout == "retained stdout"
            assert snapshot.lifecycle.complete
    assert len(owners) == 1 and owner is owners[0] and capture.call_count == 1
    assert not hasattr(error, "process_result")
    assert owner.wait_cleanup(0.1).complete
    assert child.stdout.attempts == child.stderr.attempts == 1


original_capture = OwnedProcess.capture


def test_incomplete_setup_failure_returns_original_generic_cleanup_handle():
    error = OSError("second reader setup")
    entered, release = threading.Event(), threading.Event()
    caught, returned = [], []
    original_launch = OwnedProcess.launch
    original_start = OwnedProcess._start_reader
    original_read = OwnedProcess._read_chunk
    owner = None

    def launch(*args, **kwargs):
        try:
            prepared = original_launch(*args, **kwargs)
            returned.append(prepared)
            return prepared
        except BaseException as exc:
            caught.append(exc)
            raise

    def start(owner, index):
        if index == 1:
            assert entered.wait(1)
            raise error
        original_start(owner, index)

    def read(owner, index):
        if index == 0:
            entered.set()
            assert release.wait(5), "test-controlled setup reader leaked"
            return b""
        return original_read(owner, index)

    try:
        with (
            mock.patch.object(OwnedProcess, "_start_reader", start),
            mock.patch.object(OwnedProcess, "_read_chunk", read),
            mock.patch.object(OwnedProcess, "launch", side_effect=launch),
        ):
            owner, outcome = lifecycle.run_captured(
                (sys.executable, "-c", "import time; time.sleep(30)"),
                policy=lifecycle.LifecyclePolicy(0.05),
            )
            assert caught == [] and returned == [owner]
            assert isinstance(owner, OwnedProcess)
            assert outcome.errors[0] is error
            assert not hasattr(error, "process_result")
            assert not owner.cleanup_status.complete
            release.set()
            assert owner.wait_cleanup(1).complete
    finally:
        release.set()
        handle = (
            owner
            if isinstance(owner, OwnedProcess)
            else getattr(error, "cleanup", None)
        )
        if handle is not None:
            handle.shutdown()
            assert handle.wait_cleanup(1).complete


def test_completed_setup_failure_returns_prepared_owner():
    child = Child()
    error = OSError("transfer")
    with (
        mock.patch.object(lifecycle.subprocess, "Popen", return_value=child),
        mock.patch.object(OwnedProcess, "_transfer", side_effect=error),
    ):
        owner, outcome = lifecycle.run_captured(
            ("client",), policy=lifecycle.LifecyclePolicy(0.1)
        )
    assert isinstance(owner, OwnedProcess) and owner.cleanup_status.complete
    assert not hasattr(error, "cleanup") and outcome.errors[0] is error
    assert not hasattr(error, "process_result")
    assert child.stdout.attempts == child.stderr.attempts == 1


def test_factory_failure_without_handle_returns_none_and_original_error():
    error = OSError("factory")
    with (
        mock.patch.object(lifecycle.subprocess, "Popen", side_effect=error),
        mock.patch.object(OwnedProcess, "launch", side_effect=error) as launch,
    ):
        owner, outcome = lifecycle.run_captured(
            ("client",), policy=lifecycle.LifecyclePolicy(0.1)
        )
    assert launch.call_count == 1
    assert owner is None and outcome.errors == [error]
    assert (outcome.return_code, outcome.stdout, outcome.stderr) == (None, "", "")
    assert not hasattr(error, "process_result")


def test_incomplete_capture_returns_same_owner_and_never_mutates_delivered_outcome():
    entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
    original_read = OwnedProcess._read_chunk
    original_close = OwnedStream.close
    late_error = OSError("https://private.invalid/" + "SECRET" * 2000)
    owner = None

    def close(stream_owner):
        if stream_owner.label == "stdout":
            assert stopped.is_set(), "live reader pipe released early"
        original_close(stream_owner)
        if stream_owner.label == "stdout":
            raise late_error

    def read(owner, index):
        if index == 0:
            entered.set()
            assert release.wait(5), "test-controlled reader leaked"
            stopped.set()
            return b""
        return original_read(owner, index)

    try:
        with (
            mock.patch.object(OwnedProcess, "_read_chunk", read),
            mock.patch.object(OwnedStream, "close", close),
        ):
            owner, outcome = lifecycle.run_captured(
                (sys.executable, "-c", "import time; time.sleep(30)"),
                policy=lifecycle.LifecyclePolicy(0.05),
            )
            assert entered.is_set(), "captured adapter must delegate internal readers"
            assert isinstance(owner, OwnedProcess)
            assert not owner.cleanup_status.complete
            assert outcome.errors and any(
                "incomplete" in str(e) for e in outcome.errors
            )
            assert not owner.wait_cleanup(0.01).complete
            before = (
                outcome.return_code,
                outcome.stdout,
                outcome.stderr,
                tuple(outcome.errors),
                tuple(tuple(getattr(e, "__notes__", ())) for e in outcome.errors),
            )
            release.set()
            assert owner.wait_cleanup(1).complete and stopped.is_set()
            assert any(f.error is late_error for f in owner.cleanup_status.failures)
            assert late_error not in outcome.errors
            assert all(
                "SECRET" not in str(e) and "private.invalid" not in str(e)
                for e in outcome.errors
            )
            assert before == (
                outcome.return_code,
                outcome.stdout,
                outcome.stderr,
                tuple(outcome.errors),
                tuple(tuple(getattr(e, "__notes__", ())) for e in outcome.errors),
            )
    finally:
        release.set()
        if isinstance(owner, OwnedProcess):
            owner.shutdown()
            assert owner.wait_cleanup(1).complete


def test_actual_factory_failure_returns_prepared_owner():
    error = OSError("actual factory")
    with mock.patch.object(lifecycle.subprocess, "Popen", side_effect=error):
        owner, outcome = lifecycle.run_captured(
            ("client",), policy=lifecycle.LifecyclePolicy(0.1)
        )
    assert isinstance(owner, OwnedProcess) and owner.cleanup_status.complete
    assert outcome.errors[0] is error and outcome.return_code is None
    assert outcome.stdout == outcome.stderr == ""
    assert not hasattr(error, "process_result")
