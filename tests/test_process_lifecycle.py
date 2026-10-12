"""Generic process contracts; no consumer/domain dependency."""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
import threading
import time
from unittest import mock

import pytest


@pytest.fixture
def api():
    return importlib.import_module("docker.process")


class Pipe:
    def __init__(self, error=None):
        self.attempts = 0
        self.error = error
        self.hook = None

    def read(self, size):
        return b""

    def close(self):
        self.attempts += 1
        if self.hook:
            self.hook()
        if self.error:
            raise self.error


class Child:
    def __init__(self, errors=(None, None)):
        self.stdout, self.stderr = (Pipe(error) for error in errors)
        self.returncode = None
        self.actions = []

    def poll(self):
        return self.returncode

    def terminate(self):
        self.actions.append("terminate")
        self.returncode = -15

    def kill(self):
        self.actions.append("kill")
        self.returncode = -9

    def wait(self, timeout=None):
        self.actions.append("wait")
        if self.returncode is None:
            raise subprocess.TimeoutExpired("child", timeout)
        return self.returncode


def launch(api, child, **kw):
    return api.OwnedProcess.launch(["child"], factory=lambda *a, **k: child,
                                   grace_seconds=.04, **kw)


def secondary(error):
    return getattr(error, "_transaction_secondary", [])


@pytest.mark.parametrize("kw", [{"argv": []}, {"grace_seconds": -1},
                                  {"capture_limit": -1}, {"deadline_seconds": 0},
                                  {"popen_kwargs": {"stdout": None}},
                                  {"popen_kwargs": {"text": True}},
                                  {"popen_kwargs": {"stdin": subprocess.PIPE}}])
def test_invalid_config_never_launches(api, kw):
    factory = mock.Mock()
    options = {"argv": ["child"], "factory": factory, **kw}
    with pytest.raises((ValueError, TypeError)):
        api.OwnedProcess.launch(**options)
    factory.assert_not_called()


@pytest.mark.parametrize("seam", ["_prepare_stream", "_new_event", "_new_lock", "_make_continuation"])
@pytest.mark.parametrize("position", [1, 2])
def test_preparation_failure_before_factory(api, seam, position):
    # Continuation exists once; second position covers independent events/owners.
    if seam == "_make_continuation" and position == 2:
        position = 1
    factory = mock.Mock()
    real = getattr(api, seam)
    calls = 0
    error = RuntimeError("preparation")

    def fail(*a, **k):
        nonlocal calls
        calls += 1
        if calls == position:
            raise error
        return real(*a, **k)

    with mock.patch.object(api, seam, fail):
        with pytest.raises(RuntimeError) as caught:
            api.OwnedProcess.launch(["child"], factory=factory)
    assert caught.value is error
    factory.assert_not_called()
    assert not workers()


def workers():
    return [t for t in threading.enumerate() if t.name.startswith("owned-process-")]


@pytest.mark.parametrize("after_start", [False, True])
def test_continuation_start_failure_cleans_idle_worker(api, after_start):
    original = threading.Thread.start
    failure = RuntimeError("startup")
    factory = mock.Mock()

    def start(t):
        if t.name.endswith("cleanup"):
            if after_start:
                original(t)
            raise failure
        original(t)

    with mock.patch.object(threading.Thread, "start", start):
        with pytest.raises(RuntimeError) as caught:
            api.OwnedProcess.launch(["child"], factory=factory)
    assert caught.value is failure
    factory.assert_not_called()
    assert not workers()


def test_factory_failure_stops_prepared_continuation(api):
    failure = OSError("factory")
    with pytest.raises(OSError) as caught:
        api.OwnedProcess.launch(["child"], factory=mock.Mock(side_effect=failure))
    assert caught.value is failure
    assert not workers()


@pytest.mark.parametrize("seam", ["_transfer", "_make_reader", "_configure_reader", "_start_reader", "_start_supervisor"])
@pytest.mark.parametrize("position", [1, 2])
def test_setup_fault_accounts_for_both_pipes(api, seam, position):
    if seam == "_start_supervisor":
        position = 1
    child = Child()
    failure = OSError("setup")
    real = getattr(api.OwnedProcess, seam)
    calls = 0

    def fail(self, *a, **k):
        nonlocal calls
        calls += 1
        if calls == position:
            raise failure
        return real(self, *a, **k)

    with mock.patch.object(api.OwnedProcess, seam, fail):
        with pytest.raises(OSError) as caught:
            launch(api, child, deadline_seconds=1)
    assert caught.value is failure
    assert child.stdout.attempts == child.stderr.attempts == 1
    assert "terminate" in child.actions and "wait" in child.actions
    assert not workers()


def test_duplicate_factory_stream_gets_only_one_release_authority(api):
    child = Child()
    child.stderr = child.stdout
    with pytest.raises(TypeError, match="independent"):
        launch(api, child)
    assert child.stdout.attempts == 1 and child.returncode is not None
    assert not workers()


def test_supervision_cannot_race_prepared_transfer(api):
    child = Child()
    real_transfer = api.OwnedProcess._transfer

    def transfer(self, index):
        time.sleep(.025)
        assert not self._requested
        real_transfer(self, index)

    with mock.patch.object(api.OwnedProcess, "_transfer", transfer):
        owner = launch(api, child, deadline_seconds=.01)
    assert owner.shutdown().complete
    assert child.stdout.attempts == child.stderr.attempts == 1


def test_callback_failure_reaps_and_closes(api):
    child = Child()
    error = RuntimeError("callback")
    with pytest.raises(RuntimeError) as caught:
        launch(api, child, on_launched=mock.Mock(side_effect=error))
    assert caught.value is error
    assert child.stdout.attempts == child.stderr.attempts == 1
    assert child.returncode is not None


def test_second_transfer_interruption_outranks_first_ordinary(api):
    child = Child()
    first, second = OSError("first"), KeyboardInterrupt("second")
    seen = []

    def reject(self, index):
        seen.append(self)
        assert self._acquired == [True, True]
        assert self._transferred == [False, False]
        raise (first, second)[index]

    with mock.patch.object(api.OwnedProcess, "_transfer", reject):
        with pytest.raises(KeyboardInterrupt) as caught:
            launch(api, child)
    assert caught.value is second
    assert first in secondary(second)
    assert len(seen) == 2 and seen[0] is seen[1]
    assert child.stdout.attempts == child.stderr.attempts == 1
    assert child.returncode is not None


@pytest.mark.parametrize("position", [0, 1])
@pytest.mark.parametrize("error_type", [OSError, RuntimeError, KeyboardInterrupt])
@pytest.mark.parametrize("operation", [None, OSError, ValueError, RuntimeError, KeyboardInterrupt])
def test_shutdown_precedence_and_terminal_ledger(api, position, error_type, operation):
    close = error_type("close")
    primary = operation("operation") if operation else None
    errors = [None, None]
    errors[position] = close
    child = Child(errors)
    owner = launch(api, child)
    result = owner.shutdown(primary=primary)
    expected = primary if primary is not None and (not isinstance(primary, Exception) or error_type is OSError) else close
    assert result.error is expected
    assert result.complete
    assert child.stdout.attempts == child.stderr.attempts == 1
    assert owner.shutdown() is result
    assert owner.wait_cleanup(.01) is result
    assert len(result.failures) == len({id(e) for e in result.failures})


@pytest.mark.parametrize("close_type", [OSError, RuntimeError])
@pytest.mark.parametrize("position", [0, 1])
def test_domain_operation_preserves_identity_and_secondary_diagnostics(api, close_type, position):
    class DomainError(Exception):
        pass

    operation = DomainError("domain operation")
    prior = OSError("existing diagnostic")
    operation._transaction_secondary = [prior]
    close = close_type("close")
    errors = [None, None]
    errors[position] = close
    child = Child(errors)
    owner = launch(api, child)
    result = owner.shutdown(primary=operation)
    expected, displaced = (operation, close) if close_type is OSError else (close, operation)
    assert result.error is expected
    assert displaced in secondary(expected)
    assert secondary(operation) == ([prior, close] if close_type is OSError else [prior])
    assert operation.__cause__ is None
    assert result.complete
    assert child.stdout.attempts == child.stderr.attempts == 1
    assert owner.shutdown() is result
    assert secondary(expected).count(displaced) == 1


def test_both_close_failures_and_reentrant_concurrent_shutdown(api):
    first, second = OSError("first"), KeyboardInterrupt("second")
    child = Child((first, second))
    owner = launch(api, child)
    child.stdout.hook = lambda: owner.shutdown()
    results = []
    ts = [threading.Thread(target=lambda: results.append(owner.shutdown())) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(1)
        assert not t.is_alive()
    final = owner.wait_cleanup(1)
    assert final.error is second and first in final.failures
    assert child.stdout.attempts == child.stderr.attempts == 1
    assert all(r is final for r in results)


class StalledPipe(Pipe):
    def __init__(self, error=None):
        super().__init__(error)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.reader_done = threading.Event()

    def fileno(self):
        # Pretend descriptor-backed only for shutdown classification; actual
        # deterministic reader is installed below, not os.read on this number.
        return 123456

    def close(self):
        assert self.reader_done.is_set(), "close beneath a live reader"
        super().close()


def stalled(api, child, **kw):
    pipe = StalledPipe(kw.pop("late_error", None))
    child.stdout = pipe
    real = api.OwnedProcess._read

    def read(self, index):
        if index == 0:
            pipe.entered.set()
            pipe.release.wait()
            pipe.reader_done.set()
        else:
            real(self, index)

    with mock.patch.object(api.OwnedProcess, "_read", read), mock.patch.object(api.OwnedProcess, "_configure_reader"):
        owner = launch(api, child, **kw)
        assert pipe.entered.wait(1)
    return owner, pipe


@pytest.mark.parametrize("join_error", [OSError, RuntimeError, KeyboardInterrupt])
def test_join_failure_cannot_skip_safe_cleanup_and_reap(api, join_error):
    child = Child((None, OSError("sibling")))
    owner, pipe = stalled(api, child)
    failure = join_error("join")
    try:
        with mock.patch.object(owner, "_join_reader", side_effect=failure):
            result = owner.shutdown()
        assert result.shutdown_incomplete and result.error is failure
        assert pipe.attempts == 0 and child.stderr.attempts == 1
        assert child.returncode is not None
    finally:
        pipe.release.set()
        assert owner.wait_cleanup(1).complete
    assert pipe.attempts == 1


@pytest.mark.parametrize("late_error", [None, OSError, RuntimeError, KeyboardInterrupt])
def test_incomplete_retains_same_owner_and_late_outcome_without_mutation(api, late_error):
    child = Child()
    failure = late_error("late") if late_error else None
    owner, pipe = stalled(api, child, late_error=failure)
    primary = KeyboardInterrupt("operation")
    try:
        before = time.monotonic()
        delivered = owner.shutdown(primary=primary)
        assert time.monotonic() - before < .5
        assert delivered.shutdown_incomplete and delivered.error is primary
        notes = list(secondary(primary))
        assert pipe.attempts == 0
        assert owner.wait_cleanup(.01).shutdown_incomplete
        assert pipe.attempts == 0
        observations = []
        ts = [threading.Thread(target=lambda: observations.append(owner.wait_cleanup(.01))) for _ in range(3)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(1)
        assert all(r.shutdown_incomplete for r in observations)
    finally:
        pipe.release.set()
        final = owner.wait_cleanup(1)
    assert final.complete and pipe.attempts == child.stderr.attempts == 1
    assert delivered.shutdown_incomplete and delivered.error is primary
    assert secondary(primary) == notes
    if failure:
        assert failure in final.failures


@pytest.mark.parametrize("active", [False, True])
def test_incomplete_context_exit_exposes_observation(api, active):
    child = Child()
    owner, pipe = stalled(api, child)
    primary = KeyboardInterrupt("active") if active else None
    try:
        with pytest.raises(KeyboardInterrupt if active else api.CleanupIncomplete) as caught:
            with owner:
                if primary:
                    raise primary
        if primary:
            assert caught.value is primary
        assert caught.value.cleanup is owner
    finally:
        pipe.release.set()
        assert owner.wait_cleanup(1).complete


def test_failed_launch_exposes_retained_cleanup(api):
    child = Child()
    pipe = StalledPipe()
    child.stdout = pipe
    error = OSError("callback")

    def read(self, index):
        if index == 0:
            pipe.entered.set()
            pipe.release.wait()
            pipe.reader_done.set()

    def callback():
        assert pipe.entered.wait(1)
        raise error

    with mock.patch.object(api.OwnedProcess, "_read", read), mock.patch.object(api.OwnedProcess, "_configure_reader"):
        try:
            with pytest.raises(OSError) as caught:
                launch(api, child, on_launched=callback)
            assert caught.value is error
            assert error.cleanup.cleanup_status.shutdown_incomplete
            assert pipe.attempts == 0
        finally:
            pipe.release.set()
            assert error.cleanup.wait_cleanup(1).complete


def test_reap_failure_is_incomplete_and_does_not_skip_pipes(api):
    child = Child()
    error = OSError("wait")
    original = child.wait
    child.wait = mock.Mock(side_effect=error)
    owner = launch(api, child)
    try:
        result = owner.shutdown()
        assert result.shutdown_incomplete
        assert error in result.failures
        assert child.stdout.attempts == child.stderr.attempts == 1
    finally:
        child.wait = original
        assert owner.wait_cleanup(1).complete


def real(api, program, **kw):
    return api.OwnedProcess.launch([sys.executable, "-c", program], grace_seconds=.2, **kw)


def test_real_large_simultaneous_capture_is_bounded_and_drained(api):
    with real(api, "import os; os.write(1,b'x'*200000); os.write(2,b'y'*200000)", capture_limit=8192) as owner:
        result = owner.capture()
    assert result.stdout == b"x" * 8192 and result.stderr == b"y" * 8192
    assert result.returncode == 0 and result.cleanup.complete
    assert not workers()


def test_real_stream_callbacks_are_isolated_and_receive_only_bytes(api):
    calls = []

    def callback(name, chunk):
        assert name in ("stdout", "stderr") and isinstance(chunk, bytes)
        calls.append((name, chunk))
        raise ValueError("isolated callback")

    with real(api, "import os; os.write(1,b'out'); os.write(2,b'err')") as owner:
        result = owner.stream(callback)
    assert result.returncode == 0 and len(calls) == 2
    assert len(result.callback_errors) == 2
    assert result.cleanup.complete and not workers()


def test_real_descendant_held_pipe_and_deadline_are_bounded(api):
    start = time.monotonic()
    with real(api, "import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(.8)'])") as owner:
        result = owner.capture()
    assert time.monotonic() - start < .7
    assert result.cleanup.complete and not workers()
    with real(api, "import time; time.sleep(10)", deadline_seconds=.08) as owner:
        result = owner.capture()
    assert result.timed_out and result.returncode is not None
    assert not workers()


def test_real_partial_startup_and_reader_failure_do_not_corrupt_unrelated_fd(api):
    failure = OSError("reader")
    for seam in ("_start_reader", "_read_chunk"):
        original = getattr(api.OwnedProcess, seam)
        calls = 0

        def fail(self, *a, **k):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise failure
            return original(self, *a, **k)

        with mock.patch.object(api.OwnedProcess, seam, fail):
            with pytest.raises(OSError):
                with real(api, "import time; time.sleep(.3)") as owner:
                    owner.capture()
        fd = os.open(os.devnull, os.O_RDONLY)
        try:
            assert os.read(fd, 1) == b""
        finally:
            os.close(fd)
        assert not workers()


@pytest.mark.parametrize("position", [1, 2])
def test_actual_stream_lock_failure_is_prelaunch(api, position):
    from docker.filesystem import streams
    real_prepare = api._prepare_stream
    failure = KeyboardInterrupt("actual lock preparation")
    calls = 0
    factory = mock.Mock()

    def prepare(**kw):
        nonlocal calls
        calls += 1
        if calls == position:
            with mock.patch.object(streams.threading, "Lock", side_effect=failure):
                return real_prepare(**kw)
        return real_prepare(**kw)

    with mock.patch.object(api, "_prepare_stream", prepare):
        with pytest.raises(KeyboardInterrupt) as caught:
            api.OwnedProcess.launch(["child"], factory=factory)
    assert caught.value is failure
    factory.assert_not_called()
    assert not workers()


@pytest.mark.parametrize("position", [0, 1])
def test_missing_stream_releases_present_sibling(api, position):
    child = Child()
    sibling = child.stderr if position == 0 else child.stdout
    setattr(child, ("stdout", "stderr")[position], None)
    with pytest.raises(OSError, match="missing captured stream"):
        launch(api, child)
    assert sibling.attempts == 1 and child.returncode is not None
    assert not workers()


@pytest.mark.parametrize("position", [1, 2])
def test_start_then_raise_retains_started_worker(api, position):
    original = threading.Thread.start
    child = Child()
    error = RuntimeError("after start")
    calls = 0

    def start(t):
        nonlocal calls
        original(t)
        if "reader" in t.name:
            calls += 1
            if calls == position:
                raise error

    with mock.patch.object(threading.Thread, "start", start):
        with pytest.raises(RuntimeError) as caught:
            launch(api, child)
    assert caught.value is error
    assert child.stdout.attempts == child.stderr.attempts == 1
    assert not workers()


def test_blocking_close_retains_winning_outcome_without_unbounded_caller_wait(api):
    child = Child((OSError("late close"), None))
    owner = launch(api, child)
    entered, release = threading.Event(), threading.Event()

    def hook():
        entered.set()
        release.wait()

    child.stdout.hook = hook
    try:
        result = owner.shutdown()
        assert entered.is_set() and result.shutdown_incomplete
        assert owner.wait_cleanup(.01).shutdown_incomplete
        assert child.stdout.attempts == 1
    finally:
        release.set()
        final = owner.wait_cleanup(1)
    assert final.complete and child.stdout.attempts == child.stderr.attempts == 1
    assert child.stdout.error in final.failures
    assert result.shutdown_incomplete


def test_descriptorless_close_unblocks_internal_reader(api):
    child = Child()
    entered, released = threading.Event(), threading.Event()

    def read(size):
        entered.set()
        released.wait()
        return b""

    child.stdout.read = read
    child.stdout.hook = released.set
    owner = launch(api, child)
    try:
        assert entered.wait(1)
        owner.shutdown()
    finally:
        released.set()
    assert owner.wait_cleanup(1).complete
    assert child.stdout.attempts == child.stderr.attempts == 1
    assert not workers()


def test_both_ordinary_close_failures_retained_once(api):
    first, second = OSError("first"), OSError("second")
    child = Child((first, second))
    owner = launch(api, child)
    result = owner.shutdown()
    assert result.error is first
    assert secondary(first) == [second]
    assert result.failures == (first, second)
    for _ in range(3):
        assert owner.shutdown() is result
        assert owner.wait_cleanup(.01) is result
    assert secondary(first) == [second]


def test_no_callbacks_after_incomplete_delivery(api):
    child = Child()
    owner, pipe = stalled(api, child)
    callback = mock.Mock()
    try:
        delivered = owner.shutdown()
        assert delivered.shutdown_incomplete
        owner._output.put((0, b"late"))
        with pytest.raises(api.CleanupIncomplete):
            owner.stream(callback)
        callback.assert_not_called()
    finally:
        pipe.release.set()
        assert owner.wait_cleanup(1).complete


def test_real_ignored_termination_transitions_to_kill_without_cleanup_defect(api):
    with real(api, "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(10)", deadline_seconds=.15) as owner:
        result = owner.capture()
    assert result.timed_out and result.returncode == -9
    assert result.cleanup.complete and result.cleanup.error is None
    assert not workers()


def test_join_oserror_remains_a_defect_over_active_ordinary_failure(api):
    child = Child()
    owner = launch(api, child)
    primary, defect = OSError("operation"), OSError("join defect")
    with mock.patch.object(owner, "_join_reader", side_effect=defect):
        result = owner.shutdown(primary=primary)
    assert result.error is defect and primary in secondary(defect)
    assert result.complete and child.stdout.attempts == child.stderr.attempts == 1


def test_operation_interruption_installed_during_concurrent_shutdown(api):
    child = Child((RuntimeError("close"), None))
    owner = launch(api, child)
    entered, proceed = threading.Event(), threading.Event()
    real_join = owner._join_reader
    primary = KeyboardInterrupt("operation")

    def join(index, timeout):
        entered.set()
        proceed.wait(.5)
        real_join(index, timeout)

    results = []
    with mock.patch.object(owner, "_join_reader", join):
        requester = threading.Thread(target=lambda: results.append(owner.shutdown()))
        requester.start()
        assert entered.wait(1)
        # Install the active interruption before the continuation may release
        # streams. The request exists, but no outcome has yet been delivered.
        observer = threading.Thread(target=lambda: results.append(owner.shutdown(primary=primary)))
        observer.start()
        limit = time.monotonic() + 1
        while primary not in owner._ledger.interruptions and time.monotonic() < limit:
            time.sleep(.001)
        assert primary in owner._ledger.interruptions
        proceed.set()
        requester.join(1)
        observer.join(1)
        assert not requester.is_alive() and not observer.is_alive()
    final = owner.wait_cleanup(1)
    assert final.error is primary and child.stdout.error in final.failures
    assert child.stdout.attempts == child.stderr.attempts == 1


def test_concurrent_operation_interruptions_preserve_first_identity(api):
    child = Child()
    owner = api.OwnedProcess.launch(
        ["child"], factory=lambda *a, **k: child, grace_seconds=1,
    )
    first, second = KeyboardInterrupt("first"), KeyboardInterrupt("second")
    entered, proceed, merged = (threading.Event() for _ in range(3))
    real_join = owner._join_reader
    results = {}

    def join(index, timeout):
        entered.set()
        proceed.wait()
        real_join(index, timeout)

    def request(name, primary):
        results[name] = owner.shutdown(primary=primary)

    # Signal completion of the second caller's locked merge, without allowing
    # cleanup to finish or relying on sleeps to order the two interruptions.
    real_wait = owner._phase_done.wait

    def wait(timeout=None):
        if threading.current_thread().name == "second-shutdown":
            merged.set()
        return real_wait(timeout)

    threads = [
        threading.Thread(target=request, args=("first", first), name="first-shutdown"),
        threading.Thread(target=request, args=("second", second), name="second-shutdown"),
    ]
    with mock.patch.object(owner, "_join_reader", join), mock.patch.object(owner._phase_done, "wait", wait):
        try:
            threads[0].start()
            assert entered.wait(1)
            assert child.stdout.attempts == child.stderr.attempts == 0
            threads[1].start()
            assert merged.wait(1)
        finally:
            proceed.set()
            for thread in threads:
                if thread.ident is not None:
                    thread.join(2)
            owner.wait_cleanup(2)
    assert all(not thread.is_alive() for thread in threads)
    assert results["first"] is results["second"]
    for result in results.values():
        assert result.complete and result.error is first
        assert result.failures == (first, second)
    assert secondary(first) == [second]
    assert owner.shutdown() is results["first"]
    assert owner.wait_cleanup(.01) is results["first"]
    assert secondary(first) == [second]
    assert child.stdout.attempts == child.stderr.attempts == 1
    assert child.actions.count("terminate") == child.actions.count("wait") == 1
    assert not workers()


@pytest.mark.parametrize("operation_type", [ValueError, KeyboardInterrupt])
@pytest.mark.parametrize("close_type", [OSError, RuntimeError])
@pytest.mark.parametrize("position", [0, 1])
@pytest.mark.parametrize("context_exit", [False, True])
def test_deadline_completed_undelivered_primary_refreshes_outcome(
    api, operation_type, close_type, position, context_exit,
):
    operation, close = operation_type("operation"), close_type("close")
    errors = [None, None]
    errors[position] = close
    child = Child(errors)
    owner = launch(api, child, deadline_seconds=.01)
    try:
        assert owner._phase_done.wait(1)
        assert owner.wait_cleanup(1).complete
        assert not owner._delivered
        old_status = owner.cleanup_status
        assert old_status.error is close
        expected = operation if operation_type is KeyboardInterrupt or close_type is OSError else close
        displaced = close if expected is operation else operation
        if context_exit:
            with pytest.raises(type(expected)) as caught:
                with owner:
                    raise operation
            assert caught.value is expected
            result = owner.cleanup_status
        else:
            result = owner.shutdown(primary=operation)
        assert result.complete and result.error is expected
        assert displaced in secondary(expected)
        assert displaced in result.failures and expected in result.failures
        assert secondary(expected).count(displaced) == 1
        assert operation.__cause__ is None and close.__cause__ is None
        assert owner.shutdown() is result
        assert owner.wait_cleanup(.01) is result
        assert secondary(expected).count(displaced) == 1
        # Refresh creates a new snapshot, rather than changing the old one.
        assert old_status.complete and old_status.error is close
        assert old_status.failures == (close,)
        assert child.stdout.attempts == child.stderr.attempts == 1
        assert child.actions.count("terminate") == child.actions.count("wait") == 1
    finally:
        owner.shutdown()
        assert owner.wait_cleanup(1).complete
    assert not workers()


@pytest.mark.parametrize("operation_type", [ValueError, KeyboardInterrupt])
@pytest.mark.parametrize("context_exit", [False, True])
def test_primary_merged_between_status_publication_and_phase_completion(
    api, operation_type, context_exit,
):
    child = Child((OSError("close"), None))
    owner = api.OwnedProcess.launch(
        ["child"], factory=lambda *a, **k: child, grace_seconds=1,
    )
    operation = operation_type("operation")
    published, proceed, merged = (threading.Event() for _ in range(3))
    real_set, real_wait = owner._phase_done.set, owner._phase_done.wait
    results, raised = [], []

    def set_phase_done():
        # _publish() has already assigned _status and returned, but the phase
        # event remains unset until the operation primary has been merged.
        published.set()
        proceed.wait()
        real_set()

    def wait_phase_done(timeout=None):
        if threading.current_thread().name == "gap-shutdown":
            merged.set()
        return real_wait(timeout)

    def deliver():
        if context_exit:
            try:
                with owner:
                    raise operation
            except BaseException as exc:
                raised.append(exc)
            results.append(owner.cleanup_status)
        else:
            results.append(owner.shutdown(primary=operation))

    caller = threading.Thread(target=deliver, name="gap-shutdown")
    with mock.patch.object(owner._phase_done, "set", set_phase_done), mock.patch.object(owner._phase_done, "wait", wait_phase_done):
        try:
            # Exercise the deadline's autonomous, undelivered publication.
            owner._deadline = .01
            assert published.wait(1)
            old_status = owner.cleanup_status
            assert old_status.complete and old_status.error is child.stdout.error
            assert not owner._phase_done.is_set() and not owner._delivered
            caller.start()
            assert merged.wait(1)
            with owner._lock:
                assert operation in owner._ledger.interruptions or owner._ledger.original_primary is operation
            assert not owner._phase_done.is_set() and not owner._delivered
            proceed.set()
            caller.join(2)
        finally:
            proceed.set()
            if caller.ident is not None:
                caller.join(2)
            owner.shutdown()
            owner.wait_cleanup(2)
    assert not caller.is_alive()
    assert len(results) == 1
    result = results[0]
    assert result.complete and result.error is operation
    assert raised == ([operation] if context_exit else [])
    assert secondary(operation) == [child.stdout.error]
    assert result.failures == (operation, child.stdout.error)
    assert old_status.complete and old_status.error is child.stdout.error
    assert old_status.failures == (child.stdout.error,)
    assert owner.shutdown() is result and owner.wait_cleanup(.01) is result
    assert secondary(operation) == [child.stdout.error]
    assert child.stdout.attempts == child.stderr.attempts == 1
    assert child.actions.count("terminate") == child.actions.count("wait") == 1
    assert not workers()


def test_real_deadline_concurrent_shutdown(api):
    owner = real(api, "import time; time.sleep(10)", deadline_seconds=.05)
    ts = [threading.Thread(target=owner.shutdown) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(1)
        assert not t.is_alive()
    assert owner.wait_cleanup(1).complete
    assert not workers()
