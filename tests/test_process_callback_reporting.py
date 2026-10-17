"""Diagnostic policy is isolated from the sole collection's authority."""
import inspect
import logging
import threading
from unittest import mock

import pytest

import docker.process as process
from docker.process import OwnedProcess, ProcessResult
from tests.test_process_stream_outcomes import Child, Pipe, enums, launch


class Unrenderable(ValueError):
    def __str__(self):
        raise AssertionError("generic layer rendered exception text")

    def __repr__(self):
        raise AssertionError("generic layer rendered exception repr")


def test_reporter_validation_precedes_all_acquisition():
    signature = inspect.signature(OwnedProcess.launch)
    assert "reporter" in signature.parameters
    assert signature.parameters["reporter"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["reporter"].default is None
    assert tuple(inspect.signature(OwnedProcess.capture).parameters) == ("self",)
    factory = mock.Mock()
    with mock.patch.object(process, "_prepare_stream", side_effect=AssertionError("prepared too early")):
        with pytest.raises(TypeError):
            OwnedProcess.launch(["child"], reporter=object(), factory=factory)
    factory.assert_not_called()


@pytest.mark.parametrize("kind", ["chunk", "end"])
@pytest.mark.parametrize("mode", ["success", "cleanup_failure"])
def test_structured_reporting_outside_locks_without_rendering_or_retention(kind, mode):
    stream, outcome = enums()
    error, close_error = Unrenderable(), OSError("release")
    child = Child()
    reports, callbacks, owners = [], [], []
    if mode == "cleanup_failure":
        close = child.stdout.close

        def fail_close():
            close()
            raise close_error

        child.stdout.close = fail_close

    def report(exc, **context):
        owner = owners[0]
        assert owner._lock.acquire(blocking=False), "reporter called under state lock"
        owner._lock.release()
        assert threading.current_thread() is coordinating_thread
        assert set(context) == {"stream", "callback_kind"}
        assert context["stream"] in stream and context["callback_kind"] == kind
        reports.append((exc, context))

    def fail(identity, value):
        callbacks.append(identity)
        raise error

    coordinating_thread = threading.current_thread()
    owner = launch(child, reporter=report)
    owners.append(owner)
    try:
        result = owner.stream(fail if kind == "chunk" else lambda *a: None,
                              on_stream_end=fail if kind == "end" else None)
        assert type(result) is ProcessResult and not hasattr(result, "callback_errors")
        assert set(callbacks) == set(stream) and len(callbacks) == 2
        assert len(reports) == 2 and all(exc is error for exc, _ in reports)
        assert result.stream_outcomes == ((stream.STDOUT, outcome.EOF), (stream.STDERR, outcome.EOF))
        assert all(f.error is not error for f in result.lifecycle.failures)
        if mode == "cleanup_failure":
            assert result.lifecycle.failures[0].error is close_error
        else:
            assert result.lifecycle.failures == ()
        assert child.stdout.close_attempts == child.stderr.close_attempts == 1
    finally:
        owner.shutdown()
        assert owner.wait_cleanup(1).complete


@pytest.mark.parametrize("reporter_mode", ["default", "raw", "masked", "broken"])
def test_diagnostic_policy_and_fixed_safe_fallback(caplog, reporter_mode):
    enums()
    text = "token https://private.invalid/token"
    error = ValueError(text)
    reporter_error = ValueError("reporter-secret https://reporter.invalid/")
    reports = []
    logger = logging.getLogger("test.explicit.callback.policy")

    def report(exc, **context):
        reports.append(exc)
        if reporter_mode == "broken":
            raise reporter_error
        logger.warning(str(exc) if reporter_mode == "raw" else "<redacted>")

    owner = launch(Child(), reporter=None if reporter_mode == "default" else report)
    calls = []

    def end(*args):
        calls.append(args)
        raise error

    try:
        with caplog.at_level(logging.WARNING):
            result = owner.stream(lambda *a: None, on_stream_end=end)
        assert len(calls) == len(caplog.records) == 2
        assert len(reports) == (0 if reporter_mode == "default" else 2)
        assert not hasattr(result, "callback_errors")
        assert all(record.exc_info is None and record.stack_info is None for record in caplog.records)
        if reporter_mode == "raw":
            assert text in caplog.text
        else:
            assert "token" not in caplog.text and ".invalid" not in caplog.text
            assert "reporter-secret" not in caplog.text and "https://" not in caplog.text
            assert all(len(record.getMessage()) < 256 for record in caplog.records)
    finally:
        owner.shutdown()
        assert owner.wait_cleanup(1).complete


@pytest.mark.parametrize("fallback_error", [None, OSError("sink failed")])
@pytest.mark.parametrize("operation_failure", [False, True])
def test_reporter_failure_uses_independent_once_only_nonrecursive_fallback(fallback_error, operation_failure):
    enums()
    original, report_error = ValueError("callback secret"), ValueError("reporter secret")
    reporter = mock.Mock(side_effect=report_error)
    child = Child()
    operation_error = OSError("wait operation")
    if operation_failure:
        def fail_wait(timeout=None):
            child.wait_attempts += 1
            raise operation_error
        child.wait = fail_wait
    owner = launch(child, reporter=reporter)

    def end(*args):
        raise original

    try:
        with mock.patch.object(process, "_fallback_callback_diagnostic", side_effect=fallback_error) as fallback:
            result = owner.stream(lambda *a: None, on_stream_end=end)
            if operation_failure:
                child.wait = lambda timeout=None: child.returncode
        assert reporter.call_count == fallback.call_count == 2
        assert all(call.args == () and call.kwargs == {} for call in fallback.call_args_list)
        selected = result.lifecycle.failures[0].error if result.lifecycle.failures else None
        assert selected is (operation_error if operation_failure else None)
        assert all(f.error is operation_error for f in result.lifecycle.failures)
        assert not hasattr(result, "callback_errors")
    finally:
        child.wait = lambda timeout=None: child.returncode
        owner.shutdown()
        assert owner.wait_cleanup(1).complete


@pytest.mark.parametrize("source", ["callback", "reporter", "fallback"])
@pytest.mark.parametrize("kind", [KeyboardInterrupt, SystemExit, GeneratorExit])
@pytest.mark.parametrize("prior_kind", [OSError, KeyboardInterrupt])
def test_rejected_late_interruption_callbacks_cannot_change_prior_delivery(source, kind, prior_kind):
    enums()
    prior = prior_kind("earlier operation")
    first, second = kind("rejected interruption"), SystemExit("rejected second interruption")
    child = Child(stderr=Pipe(error=prior))
    reports, calls = [], []
    armed = False

    def reporter(exc, **context):
        reports.append(exc)
        if armed and source == "reporter":
            raise first
        if armed and source == "fallback":
            raise ValueError("reporter ordinary failure")

    owner = launch(child, reporter=reporter)
    try:
        original = None
        if prior_kind is KeyboardInterrupt:
            with pytest.raises(KeyboardInterrupt) as caught:
                owner.capture()
            assert caught.value is prior
        else:
            original = owner.capture()
            assert original.lifecycle.failures[0].error is prior
        assert not hasattr(prior, "process_result")
        assert owner.wait_cleanup(1).complete
        status, ledger = owner.cleanup_status, owner._ledger
        notes = tuple(getattr(prior, "__notes__", ()))
        counters = (child.stdout.read_attempts, child.stderr.read_attempts, child.wait_attempts)
        armed = True

        def end(identity, terminal):
            calls.append((identity, terminal))
            if source == "callback":
                raise first
            raise ValueError("ordinary callback")

        with mock.patch.object(owner, "_shutdown", side_effect=AssertionError("cleanup restarted")), \
             mock.patch.object(owner, "shutdown", side_effect=AssertionError("shutdown restarted")), \
             mock.patch.object(owner, "_read_chunk", side_effect=AssertionError("reread")), \
             mock.patch.object(process, "_fallback_callback_diagnostic", side_effect=[first, second]) as fallback:
            with pytest.raises(RuntimeError):
                owner.stream(lambda *a: pytest.fail("replayed output"), on_stream_end=end)
        assert calls == reports == [] and fallback.call_count == 0
        assert not hasattr(first, "process_result") and not hasattr(prior, "process_result")
        if original is not None:
            assert original.lifecycle == status
        assert tuple(getattr(prior, "__notes__", ())) == notes
        assert owner.cleanup_status == status and owner._ledger is ledger
        assert counters == (child.stdout.read_attempts, child.stderr.read_attempts, child.wait_attempts)
        assert child.stdout.close_attempts == child.stderr.close_attempts == 1
    finally:
        owner.shutdown()
        assert owner.wait_cleanup(1).complete


@pytest.mark.parametrize("prior_kind", [OSError, KeyboardInterrupt])
def test_rejected_late_ordinary_callbacks_do_not_report_or_return_fresh_output(prior_kind):
    enums()
    prior = prior_kind("earlier read")
    child = Child(stderr=Pipe(error=prior))
    reporter = mock.Mock()
    owner = launch(child, reporter=reporter)
    try:
        if prior_kind is KeyboardInterrupt:
            with pytest.raises(KeyboardInterrupt):
                owner.capture()
        else:
            assert owner.capture().lifecycle.failures[0].error is prior
        status = owner.cleanup_status
        notes = tuple(getattr(prior, "__notes__", ()))
        error = ValueError("late callback")

        def end(*args):
            raise error

        with mock.patch.object(owner, "_shutdown", side_effect=AssertionError("cleanup restarted")):
            with pytest.raises(RuntimeError):
                owner.stream(lambda *a: pytest.fail("output replay"), on_stream_end=end)
        reporter.assert_not_called()
        assert owner.cleanup_status == status and not hasattr(prior, "process_result")
        assert tuple(getattr(prior, "__notes__", ())) == notes
    finally:
        owner.shutdown()
        assert owner.wait_cleanup(1).complete
