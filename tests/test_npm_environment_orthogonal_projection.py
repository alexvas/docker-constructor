"""Phase 6BC.10 adapter projections over the approved returned-value API."""
from dataclasses import fields
from unittest.mock import Mock

import pytest

import docker.process as process
from docker.npm_environment import execution, lifecycle
from docker.npm_environment.errors import AssemblyTimeoutError
from docker.npm_environment.streaming import REDACTED, RETAINED_CAPTURE_ATTR
from tests.test_process_single_collection import Child


OPERATIONS = {
    "SETUP": "setup", "READ": "read", "JOIN": "join", "RELEASE": "release",
    "WAIT": "wait", "ACTION": "action", "CALLBACK": "callback",
    "REPORTER": "reporter", "COORDINATION": "coordination",
}
CONTEXTS = [
    (phase, stream)
    for phase in OPERATIONS
    for stream in (None, "STDOUT", "STDERR")
    if ((stream is not None) if phase in ("READ", "CALLBACK", "REPORTER") else
        (stream is None) if phase in ("WAIT", "ACTION", "COORDINATION") else True)
]


def info(error, kind, phase, stream=None):
    assert hasattr(process, "FailureInfo"), "adapter requires typed returned failures"
    return process.FailureInfo(error, getattr(process.FailureKind, kind), getattr(process.FailurePhase, phase),
                               getattr(process.ProcessStream, stream) if stream else None)


def value(failures=(), *, timed_out=False):
    assert hasattr(process, "LifecycleResult"), "adapter requires lifecycle value results"
    return process.ProcessResult(b"stdout", b"stderr", 0, timed_out, process.LifecycleResult(True, failures),
                                 tuple((s, process.StreamOutcome.EOF) for s in process.ProcessStream))


def install_owner(monkeypatch, result, *, interrupt=None, partial=False):
    owner = Mock(spec=process.OwnedProcess)
    owner.cleanup_status = result.lifecycle
    owner.returncode = result.returncode
    owner.wait_cleanup.return_value = result.lifecycle
    owner.shutdown.return_value = result.lifecycle
    owner.capture.side_effect = interrupt
    owner.capture.return_value = result

    def stream(callback, *, on_stream_end=None):
        for identity, data in ((process.ProcessStream.STDOUT, b"to" if partial else b"stdout"),
                               (process.ProcessStream.STDERR, b"stderr")):
            callback(identity, data)
            if on_stream_end is not None:
                on_stream_end(identity, process.StreamOutcome.ABORTED if interrupt is not None else process.StreamOutcome.EOF)
        if interrupt is not None:
            raise interrupt
        return result

    owner.stream.side_effect = stream

    def launch(*args, **kwargs):
        callback = kwargs.get("on_launched")
        if callback is not None:
            callback()
        return owner

    factory = Mock(side_effect=launch)
    monkeypatch.setattr(process.OwnedProcess, "launch", factory)
    return owner, factory


@pytest.mark.parametrize("phase,stream", CONTEXTS)
@pytest.mark.parametrize("kind", ["ORDINARY", "UNEXPECTED"])
@pytest.mark.parametrize("error_type", [OSError, ValueError])
@pytest.mark.parametrize("timed_out", [False, True])
def test_secondary_prefix_is_only_phase_and_stream(monkeypatch, phase, stream, kind, error_type, timed_out):
    primary = RuntimeError("selected authority")
    secret = "credential"
    secondary = error_type("https://user:credential@private.invalid/path " + secret * 2000)
    failures = (info(primary, "UNEXPECTED", "COORDINATION"), info(secondary, kind, phase, stream))
    result = value(failures, timed_out=timed_out)
    owner, factory = install_owner(monkeypatch, result)
    with pytest.raises(RuntimeError) as caught:
        execution.DockerRunExecutor().run_streaming(("client",), secrets=(secret,), deadline_seconds=1 if timed_out else None)
    assert caught.value is primary
    notes = getattr(primary, "__notes__", ())
    expected = f"{stream.lower() if stream else 'process'} {OPERATIONS[phase]} failed: "
    assert len(notes) == 1 and notes[0].startswith(expected)
    assert secret not in notes[0] and "private.invalid" not in notes[0] and "https://" not in notes[0]
    assert len(notes[0].encode()) <= execution.READER_FAILURE_DETAIL_BYTES + 128
    assert "stream cleanup failed" not in notes[0]
    assert not hasattr(primary, "process_result")
    assert not hasattr(secondary, "process_result")
    assert owner.stream.call_count == factory.call_count == 1 and owner.capture.call_count == 0


@pytest.mark.parametrize("phase", ["JOIN", "ACTION", "RELEASE"])
@pytest.mark.parametrize("error_shape", ["simple_oserror", "rich_oserror", "runtime"])
def test_masking_unexpected_authority_preserves_exact_identity(monkeypatch, phase, error_shape):
    text = "credential https://private.invalid/path"
    error = (OSError(5, text, "/private/credential") if error_shape == "rich_oserror" else
             OSError(text) if error_shape == "simple_oserror" else RuntimeError(text))
    failure = info(error, "UNEXPECTED", phase, None if phase == "ACTION" else "STDOUT")
    result = value((failure,))
    install_owner(monkeypatch, result)
    with pytest.raises(type(error)) as caught:
        execution.DockerRunExecutor().run_streaming(("client",), secrets=("credential",))
    assert caught.value is error and result.lifecycle.failures[0] is failure
    assert failure.kind is process.FailureKind.UNEXPECTED and failure.phase.name == phase
    public = str(error) + "\n".join(getattr(error, "__notes__", ()))
    assert "credential" not in public and "private.invalid" not in public and "https://" not in public
    if error_shape == "rich_oserror":
        assert error.errno == 5
    assert not hasattr(error, "process_result")


@pytest.mark.parametrize("phase,kind", [("RELEASE", "ORDINARY"), ("JOIN", "UNEXPECTED")])
def test_timeout_uses_selected_kind_before_timeout_flag(monkeypatch, phase, kind):
    error = OSError("cleanup operation")
    result = value((info(error, kind, phase, "STDOUT"),), timed_out=True)
    install_owner(monkeypatch, result)
    with pytest.raises(AssemblyTimeoutError if kind == "ORDINARY" else OSError) as caught:
        execution.DockerRunExecutor().run_streaming(("client",), deadline_seconds=1)
    if kind == "UNEXPECTED":
        assert caught.value is error
    else:
        notes = getattr(caught.value, "__notes__", ())
        assert notes == ["stdout release failed: cleanup operation"]
    assert not hasattr(error, "process_result")


@pytest.mark.parametrize("phase", ["READ", "JOIN", "RELEASE", "ACTION"])
def test_only_local_read_is_projected_to_domain_reader_failure(monkeypatch, phase):
    error = OSError("operation")
    result = value((info(error, "UNEXPECTED" if phase == "JOIN" else "ORDINARY", phase,
                         None if phase == "ACTION" else "STDERR"),))
    install_owner(monkeypatch, result)
    with pytest.raises(Exception) as caught:
        execution.DockerRunExecutor().run_streaming(("client",))
    if phase == "READ":
        assert isinstance(caught.value, execution.StreamReaderFailure)
        assert caught.value.stream == execution.STREAM_STDERR
    else:
        assert caught.value is error


@pytest.mark.parametrize("phase", ["SETUP", "READ", "JOIN", "RELEASE", "ACTION"])
def test_captured_projects_returned_errors_and_same_owner(monkeypatch, phase):
    error = OSError("captured failure")
    result = value((info(error, "UNEXPECTED" if phase == "JOIN" else "ORDINARY", phase,
                         "STDOUT" if phase in ("READ", "JOIN", "RELEASE") else None),))
    owner, factory = install_owner(monkeypatch, result)
    actual, outcome = lifecycle.run_captured(("client",), policy=lifecycle.LifecyclePolicy(.1))
    assert actual is owner
    assert (outcome.return_code, outcome.stdout, outcome.stderr) == (0, "stdout", "stderr")
    assert outcome.errors == [error]
    assert tuple(f.name for f in fields(outcome)) == ("return_code", "errors", "stdout", "stderr")
    assert owner.capture.call_count == factory.call_count == 1 and owner.stream.call_count == 0
    assert not hasattr(error, "process_result")


@pytest.mark.parametrize("method", ["captured", "streaming"])
@pytest.mark.parametrize("interrupt", [KeyboardInterrupt("first"), SystemExit(2), GeneratorExit(), BaseException("first")])
def test_domain_interruption_needs_no_generic_snapshot(monkeypatch, method, interrupt):
    secondary = OSError("cleanup credential https://private.invalid/")
    result = value((info(interrupt, "INTERRUPTION", "COORDINATION"), info(secondary, "ORDINARY", "RELEASE", "STDERR")), timed_out=True)
    owner, _ = install_owner(monkeypatch, result, interrupt=interrupt, partial=True)
    with pytest.raises(BaseException) as caught:
        if method == "captured":
            lifecycle.run_captured(("client",), policy=lifecycle.LifecyclePolicy(.1))
        else:
            execution.DockerRunExecutor().run_streaming(("client",), secrets=("token", "credential"), deadline_seconds=1)
    assert caught.value is interrupt and not hasattr(interrupt, "process_result")
    if method == "streaming":
        from docker.constructor_cli import _interruption_host_failure
        retained = getattr(interrupt, RETAINED_CAPTURE_ATTR)
        assert retained.stdout_tail == REDACTED and retained.stderr_tail == "stderr"
        assert _interruption_host_failure(interrupt) is not None
        notes = getattr(interrupt, "__notes__", ())
        assert len(notes) == 1 and notes[0].startswith("stderr release failed: ")
        assert "credential" not in notes[0] and "private.invalid" not in notes[0]
    assert owner.capture.call_count + owner.stream.call_count == 1


@pytest.mark.parametrize("missing", ["stdout", "stderr", "both"])
def test_missing_streams_are_controlled_setup_failure_with_original_owner(monkeypatch, missing):
    child = Child()
    present = []
    for name in ("stdout", "stderr"):
        if missing in (name, "both"):
            setattr(child, name, None)
        else:
            present.append(getattr(child, name))
    monkeypatch.setattr(lifecycle.subprocess, "Popen", lambda *a, **kw: child)
    owner, outcome = lifecycle.run_captured(("client",), policy=lifecycle.LifecyclePolicy(.1))
    assert isinstance(owner, process.OwnedProcess)
    assert outcome.errors and owner.wait_cleanup(1).complete
    assert all(pipe.close_calls == 1 for pipe in present)
    assert child.wait_calls > 0
    assert owner.cleanup_status.failures[0].phase.name == "SETUP"


@pytest.mark.parametrize("phase,kind,labelled", [("WAIT", "ORDINARY", True), ("WAIT", "UNEXPECTED", False), ("SETUP", "ORDINARY", False)])
def test_captured_final_reap_label_preserves_error_identity_and_declared_kind(monkeypatch, phase, kind, labelled):
    import subprocess

    error = subprocess.TimeoutExpired(("helper",), .01)
    failure = info(error, kind, phase)
    result = process.ProcessResult(b"", b"", None, True, process.LifecycleResult(False, (failure,)))
    owner, _ = install_owner(monkeypatch, result)
    actual, outcome = lifecycle.run_captured(("helper",), policy=lifecycle.LifecyclePolicy(.1, process_label="bounded helper"))
    assert actual is owner and outcome.errors[0] is error
    assert result.lifecycle.failures[0] is failure and failure.kind.name == kind
    assert ("bounded helper did not exit after SIGKILL" in str(error)) is labelled
    assert not hasattr(error, "process_result")
