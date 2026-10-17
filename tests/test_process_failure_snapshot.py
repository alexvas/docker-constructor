"""Phase 6BC: failed collection retains supported bounded observations."""
from dataclasses import FrozenInstanceError
import io
from unittest import mock

import pytest

from docker.process import OwnedProcess, ProcessResult
from docker.npm_environment.lifecycle import LifecyclePolicy, run_captured


class Pipe(io.BytesIO):
    def __init__(self, data, error=None):
        super().__init__(data)
        self.error = error
        self.attempts = 0

    def close(self):
        self.attempts += 1
        super().close()
        if self.error is not None:
            raise self.error


class Child:
    def __init__(self, error=None):
        self.stdout = Pipe(b"retained stdout", error)
        self.stderr = Pipe(b"retained stderr")
        self.returncode = 7

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


@pytest.mark.parametrize("method", ["capture", "stream"])
@pytest.mark.parametrize("kind", [OSError, RuntimeError, KeyboardInterrupt])
def test_release_failure_returns_value_or_propagates_control_flow(method, kind):
    error = kind("release")
    child = Child(error)
    owner = OwnedProcess.launch(["child"], factory=lambda *a, **k: child,
                                capture_limit=6, grace_seconds=.1)
    calls = []

    def collect():
        return owner.stream(lambda name, chunk: calls.append((name, chunk))) if method == "stream" else owner.capture()

    if kind is KeyboardInterrupt:
        with pytest.raises(KeyboardInterrupt) as caught:
            collect()
        assert caught.value is error
        lifecycle = owner.cleanup_status
    else:
        result = collect()
        assert isinstance(result, ProcessResult)
        assert (result.stdout, result.stderr, result.returncode) == (b"stdout", b"stderr", 7)
        assert not result.timed_out
        with pytest.raises(FrozenInstanceError):
            result.stdout = b"changed"
        lifecycle = result.lifecycle
    assert lifecycle.complete and lifecycle.failures[0].error is error
    assert lifecycle.failures[0].phase.name == "RELEASE"
    assert not hasattr(error, "process_result")
    assert child.stdout.attempts == child.stderr.attempts == 1
    before = tuple(calls)
    assert owner.wait_cleanup(.1).complete
    owner.shutdown()
    assert not hasattr(error, "process_result")
    with pytest.raises(RuntimeError):
        collect()
    assert tuple(calls) == before
    assert child.stdout.attempts == child.stderr.attempts == 1


def test_prepared_factory_failure_returns_owner_without_exception_snapshot():
    error = OSError("launch")
    owner = OwnedProcess.launch(["child"], factory=mock.Mock(side_effect=error))
    result = owner.capture()
    assert result.lifecycle.failures[0].error is error
    assert result.lifecycle.failures[0].phase.name == "SETUP"
    assert result.returncode is None and result.stdout == result.stderr == b""
    assert owner.wait_cleanup(.1).complete
    assert not hasattr(error, "process_result")


def test_captured_adapter_returns_owner_and_preserves_failure_output():
    error = OSError("release")
    child = Child(error)
    with mock.patch("docker.npm_environment.lifecycle.subprocess.Popen", return_value=child):
        owner, outcome = run_captured(["child"], policy=LifecyclePolicy(.1))
    assert isinstance(owner, OwnedProcess)
    assert outcome.return_code == 7
    assert (outcome.stdout, outcome.stderr) == ("retained stdout", "retained stderr")
    assert outcome.errors[0] is error
    assert owner.cleanup_status.complete
    assert child.stdout.attempts == child.stderr.attempts == 1


@pytest.mark.parametrize("missing", ["stdout", "stderr", "both"])
def test_captured_adapter_rejects_missing_streams_and_releases_sibling(missing):
    child = Child()
    pipes = (child.stdout, child.stderr)
    if missing in ("stdout", "both"):
        child.stdout = None
    if missing in ("stderr", "both"):
        child.stderr = None
    try:
        with mock.patch("docker.npm_environment.lifecycle.subprocess.Popen", return_value=child):
            owner, outcome = run_captured(["child"], policy=LifecyclePolicy(.1))
        assert outcome.errors
        assert any("missing captured stream" in str(error) for error in outcome.errors)
        # Prepared-owner rejection returns the same owner, never Popen.
        assert isinstance(owner, OwnedProcess)
        assert owner.wait_cleanup(.1).complete
        for name, pipe in zip(("stdout", "stderr"), pipes):
            if getattr(child, name) is not None:
                assert pipe.attempts == 1
    finally:
        # Omitted fixture objects were never transferred to production.
        for name, pipe in zip(("stdout", "stderr"), pipes):
            if getattr(child, name) is None:
                pipe.close()
