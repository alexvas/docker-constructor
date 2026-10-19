"""Startup interruption and scheduler-independent CI fixture regressions."""

import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import pytest

import docker.process as process
from docker.npm_environment import lifecycle, streaming
from tests import test_npm_environment_phase4_lifecycle as lifecycle_fixture
from tests import test_process_compatibility_action_gate as action_fixture
from tests import test_retained_tail_phase6 as renderer_fixture


@pytest.mark.parametrize(
    "target, primary_type",
    [
        (target, primary)
        for target in ("npm-stdout-reader", "npm-stderr-reader")
        for primary in (None, RuntimeError, KeyboardInterrupt)
    ]
    + [
        ("npm-sink-dispatcher", primary)
        for primary in (RuntimeError, KeyboardInterrupt)
    ],
)
@pytest.mark.parametrize("join_type", [OSError, KeyboardInterrupt])
@pytest.mark.parametrize("observation_interrupt", [False, True])
def test_live_join_interruption_settles_before_buffers_and_capture(
    target, primary_type, join_type, observation_interrupt, monkeypatch
):
    primary = primary_type("operation startup") if primary_type is not None else None
    join_error = join_type("join interrupted while live")
    entered, release = threading.Event(), threading.Event()
    start = threading.Thread.start
    bootstrap = threading.Thread._bootstrap_inner
    join = threading.Thread.join
    coordinated_join = streaming._ThreadStartup.join
    finish = streaming.SinkDispatcher.finish
    attach = streaming._attach_retained_capture
    created, joins, owners, observations, finalized, tails, captures, hooks = (
        [],
        [],
        [],
        [],
        [],
        [],
        [],
        [],
    )

    def delayed_exit(thread):
        bootstrap(thread)
        if thread.name == target:
            entered.set()
            assert release.wait(5), "test did not release live worker"

    def start_worker(thread):
        if thread.name in (
            "npm-stdout-reader",
            "npm-stderr-reader",
            "npm-sink-dispatcher",
        ):
            created.append(thread)
        start(thread)
        if thread.name == target and primary is not None:
            raise primary

    def interrupted_join(thread, *args, **kwargs):
        if thread.name == target:
            joins.append(thread)
            assert entered.wait(5)
            assert thread.is_alive()
            assert not thread._os_thread_handle.is_done()
            raise join_error
        return join(thread, *args, **kwargs)

    def observe_owner(owner, *args, **kwargs):
        if owner._thread.name == target and owner not in owners:
            owners.append(owner)
            poll = getattr(owner, "_completion_poll", None)
            if poll is not None:
                wait = poll.wait

                def observe(timeout=None):
                    observations.append(True)
                    if observation_interrupt and len(observations) == 1:
                        raise KeyboardInterrupt("completion observation")
                    release.set()
                    return wait(timeout)

                monkeypatch.setattr(poll, "wait", observe)
        return coordinated_join(owner, *args, **kwargs)

    def reader(data):
        chunks = iter((data, b""))

        def read(_size):
            if primary is not None and threading.current_thread().name == target:
                entered.set()
                assert release.wait(5)
            return next(chunks, b"")

        return read

    def stream_factory(tag):
        stream = streaming.RedactingStream(())

        def tail():
            assert all(t._os_thread_handle.is_done() for t in created)
            tails.append(tag)
            return stream.tail()

        return SimpleNamespace(
            feed_bytes=stream.feed_bytes, finish=stream.finish, tail=tail
        )

    def finalize(dispatcher):
        finalized.append(dispatcher)
        return finish(dispatcher)

    def attach_settled(exc, capture):
        assert all(t._os_thread_handle.is_done() for t in created)
        assert len(finalized) == 1
        assert len(tails) == 2
        captures.append(capture)
        attach(exc, capture)

    monkeypatch.setattr(threading.Thread, "_bootstrap_inner", delayed_exit)
    monkeypatch.setattr(threading.Thread, "start", start_worker)
    monkeypatch.setattr(threading.Thread, "join", interrupted_join)
    monkeypatch.setattr(streaming._ThreadStartup, "join", observe_owner)
    monkeypatch.setattr(streaming.SinkDispatcher, "finish", finalize)
    monkeypatch.setattr(streaming, "_attach_retained_capture", attach_settled)
    try:
        with pytest.raises(BaseException) as raised:
            streaming.collect_streams(
                stdout_read=reader(b"late stdout\n"),
                stderr_read=reader(b"late stderr\n"),
                sink=lambda chunk: None,
                stream_factory=stream_factory,
                on_interruption=lambda: hooks.append(True),
            )
        assert raised.value is (primary if primary is not None else join_error)
        assert len(joins) == len(owners) == len(captures) == 1
        assert observations
        assert hooks == ([] if primary is None else [True])
        if primary is not None:
            assert any(
                "join interrupted while live" in n or join_type.__name__ in n
                for n in primary.__notes__
            )
        if target != "npm-sink-dispatcher":
            assert captures[0].stdout_tail == "late stdout\n"
        if target == "npm-stderr-reader":
            assert captures[0].stderr_tail == "late stderr\n"
        before = (len(joins), len(observations))
        failures = owners[0].join()
        assert failures[0] is join_error
        assert len(failures) == (2 if observation_interrupt else 1)
        if observation_interrupt:
            assert isinstance(failures[1], KeyboardInterrupt)
        if target == "npm-sink-dispatcher" and primary is not None:
            for kind in {type(error) for error in failures}:
                assert sum(f"({kind.__name__})" in n for n in primary.__notes__) >= sum(
                    isinstance(error, kind) for error in failures
                )
        assert owners[0].join() == failures
        finalized[0].finish()
        assert (len(joins), len(observations)) == before
        assert all(t._os_thread_handle.is_done() for t in created)
    finally:
        release.set()
        for thread in created:
            if not thread._os_thread_handle.is_done():
                join(thread, 5)
                assert thread._os_thread_handle.is_done()


@pytest.mark.parametrize("primary_type", [None, RuntimeError])
@pytest.mark.parametrize("join_type", [OSError, KeyboardInterrupt])
def test_normal_dispatcher_interrupted_join_keeps_original_bound(
    primary_type, join_type, monkeypatch
):
    primary = primary_type("operation") if primary_type is not None else None
    failure = join_type("bounded join interruption")
    callback_entered, release = threading.Event(), threading.Event()
    clock = [0.0]
    start, join = threading.Thread.start, threading.Thread.join
    initialize = streaming.SinkDispatcher.__init__
    coordinated_join = streaming._ThreadStartup.join
    finish = streaming.SinkDispatcher.finish
    owners, threads, attempts, waits, dispatchers = [], [], [], [], []

    def initialize_bounded(dispatcher, sink, **kwargs):
        initialize(dispatcher, sink, drain_budget=0.04, callback_budget=0.01, **kwargs)

    def sink(chunk):
        callback_entered.set()
        release.wait(5)

    def start_worker(thread):
        start(thread)
        if thread.name == "npm-sink-dispatcher":
            threads.append(thread)
        if primary is not None and thread.name == "npm-stderr-reader":
            assert callback_entered.wait(5)
            raise primary

    def interrupt_join(thread, *args, **kwargs):
        if thread.name == "npm-sink-dispatcher":
            attempts.append(True)
            assert callback_entered.wait(5)
            assert thread.is_alive()
            clock[0] += 0.02  # Time already spent in the initial join attempt.
            raise failure
        return join(thread, *args, **kwargs)

    def observe_owner(owner, *args, **kwargs):
        if owner._thread.name == "npm-sink-dispatcher" and not owners:
            owners.append(owner)
            poll = getattr(owner, "_completion_poll", None)
            if poll is not None:
                wait = poll.wait

                def bounded_wait(timeout=None):
                    assert timeout is not None and timeout > 0
                    waits.append(timeout)
                    result = wait(timeout)
                    clock[0] += timeout
                    return result

                monkeypatch.setattr(poll, "wait", bounded_wait)
        return coordinated_join(owner, *args, **kwargs)

    def finalize(dispatcher):
        dispatchers.append(dispatcher)
        return finish(dispatcher)

    monkeypatch.setattr(streaming, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(streaming.SinkDispatcher, "__init__", initialize_bounded)
    monkeypatch.setattr(streaming.SinkDispatcher, "finish", finalize)
    monkeypatch.setattr(streaming._ThreadStartup, "join", observe_owner)
    monkeypatch.setattr(threading.Thread, "start", start_worker)
    monkeypatch.setattr(threading.Thread, "join", interrupt_join)
    chunks = iter((b"callback payload\n", b""))
    try:
        with pytest.raises(BaseException) as raised:
            streaming.collect_streams(
                stdout_read=lambda size: next(chunks, b""),
                stderr_read=lambda size: b"",
                sink=sink,
            )
        assert raised.value is (primary if primary is not None else failure)
        if primary is not None:
            assert any("bounded join interruption" in n for n in primary.__notes__)
        assert waits and sum(waits) == pytest.approx(0.03)
        assert clock[0] == pytest.approx(0.05)
        assert dispatchers[0].is_alive()  # Arbitrary callback remains blocked.
        assert dispatchers[0].truncation_notice() == streaming.TRUNCATION_NOTICE
        before = (len(attempts), len(waits))
        assert owners[0].join(100) == (failure,)  # No new deadline on observation.
        dispatchers[0].finish()
        assert (len(attempts), len(waits)) == before == (1, len(waits))
        assert hasattr(raised.value, "retained_capture")
    finally:
        release.set()
        for thread in threads:
            join(thread, 5)
            assert thread._os_thread_handle.is_done()


@pytest.mark.parametrize("native_completed", [False, True])
def test_startup_states_distinguish_no_native_from_completed_before_python_start(
    native_completed,
):
    native = SimpleNamespace(
        ident=0, is_done=mock.Mock(return_value=False), join=mock.Mock()
    )
    failure = KeyboardInterrupt("startup")

    def start():
        if native_completed:
            native.ident = 123
            native.is_done.return_value = True
        raise failure

    thread = SimpleNamespace(
        _os_thread_handle=native,
        _started=threading.Event(),
        start=start,
        join=mock.Mock(side_effect=AssertionError("invalid Python join")),
    )
    startup = streaming._ThreadStartup(thread)
    assert startup.state is streaming._StartupState.PREPARED
    with pytest.raises(KeyboardInterrupt) as raised:
        startup.start()
    assert raised.value is failure
    expected = (
        streaming._StartupState.COMPLETED
        if native_completed
        else streaming._StartupState.NO_NATIVE_WORKER
    )
    assert startup.state is expected
    assert startup.join() == startup.join() == ()
    thread.join.assert_not_called()
    assert native.join.call_count == int(native_completed)


def test_started_worker_completion_and_repeated_join_are_observations(monkeypatch):
    release = threading.Event()
    thread = threading.Thread(target=lambda: release.wait(5), name="startup-state")
    startup = streaming._ThreadStartup(thread)
    join = thread.join
    joined = []

    def observed_join(*args, **kwargs):
        joined.append(True)
        return join(*args, **kwargs)

    monkeypatch.setattr(thread, "join", observed_join)
    try:
        assert startup.state is streaming._StartupState.PREPARED
        startup.start()
        assert startup.state is streaming._StartupState.STARTED
        release.set()
        assert startup.join() == ()
        assert startup.state is streaming._StartupState.COMPLETED
        assert startup.join(0) == ()
        assert joined == [True]
    finally:
        release.set()
        startup.join()


@pytest.mark.parametrize(
    "target", ["npm-stdout-reader", "npm-stderr-reader", "npm-sink-dispatcher"]
)
@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt])
@pytest.mark.parametrize("wait_interrupt", [False, True])
@pytest.mark.parametrize("hook_error", [False, True])
def test_native_worker_pending_before_ident_is_settled_before_capture(
    target, error_type, wait_interrupt, hook_error, monkeypatch
):
    failure = error_type("native startup interrupted")
    bootstrap_entered = threading.Event()
    allow_bootstrap = threading.Event()
    release_readers = threading.Event()
    start = threading.Thread.start
    bootstrap = threading.Thread._bootstrap_inner
    join = threading.Thread.join
    attach = streaming._attach_retained_capture
    finish = streaming.SinkDispatcher.finish
    coordinated_join = streaming._ThreadStartup.join
    pending_states = []
    created, joined, finalized, hooks, captures = [], [], [], [], []
    original_waits = {}
    waits = []

    def delayed_bootstrap(thread):
        if thread.name == target:
            bootstrap_entered.set()
            assert allow_bootstrap.wait(5), "test did not release native bootstrap"
        bootstrap(thread)

    def interrupt_start(thread):
        if thread.name in (
            "npm-stdout-reader",
            "npm-stderr-reader",
            "npm-sink-dispatcher",
        ):
            created.append(thread)
            original_waits[thread] = thread._started.wait
        if thread.name == target:
            event_wait = thread._started.wait

            def interrupted_wait(timeout=None):
                waits.append(True)
                if len(waits) == 1:
                    assert bootstrap_entered.wait(5)
                    assert thread.ident is None
                    assert thread._os_thread_handle.ident != 0
                    raise failure
                if wait_interrupt and len(waits) == 2:
                    raise KeyboardInterrupt("startup settlement interrupted")
                allow_bootstrap.set()
                return event_wait(timeout)

            monkeypatch.setattr(thread._started, "wait", interrupted_wait)
        start(thread)

    def observed_join(thread, *args, **kwargs):
        if thread in created:
            joined.append(thread)
        return join(thread, *args, **kwargs)

    def unblock():
        assert not hasattr(failure, "retained_capture")
        hooks.append(True)
        release_readers.set()
        if hook_error:
            raise OSError("unblock failure")

    def reader(data):
        chunks = iter((data, b""))

        def read(_size):
            assert release_readers.wait(5)
            return next(chunks, b"")

        return read

    def finalized_dispatcher(dispatcher):
        finalized.append(dispatcher)
        if target == "npm-sink-dispatcher" and len(finalized) == 1:
            assert (
                dispatcher._startup.state is streaming._StartupState.BOOTSTRAP_PENDING
            )
            assert dispatcher.is_alive()
        return finish(dispatcher)

    def settle_pending(startup, *args, **kwargs):
        if startup._thread.name == target:
            pending_states.append(startup.state)
        return coordinated_join(startup, *args, **kwargs)

    def attach_after_settlement(exc, capture):
        assert created
        assert all(t._os_thread_handle.is_done() for t in created)
        assert all(not t.is_alive() for t in created)
        assert all(joined.count(t) == 1 for t in created)
        assert len(finalized) == 1
        captures.append(capture)
        attach(exc, capture)

    monkeypatch.setattr(streaming._ThreadStartup, "join", settle_pending)
    monkeypatch.setattr(threading.Thread, "_bootstrap_inner", delayed_bootstrap)
    monkeypatch.setattr(threading.Thread, "start", interrupt_start)
    monkeypatch.setattr(threading.Thread, "join", observed_join)
    monkeypatch.setattr(streaming, "_attach_retained_capture", attach_after_settlement)
    monkeypatch.setattr(streaming.SinkDispatcher, "finish", finalized_dispatcher)
    try:
        with pytest.raises(error_type) as raised:
            streaming.collect_streams(
                stdout_read=reader(b"settled stdout\n"),
                stderr_read=reader(b"settled stderr\n"),
                sink=lambda chunk: None,
                on_interruption=unblock,
            )
        assert raised.value is failure
        assert hooks == [True]
        assert pending_states == [streaming._StartupState.BOOTSTRAP_PENDING]
        assert len(captures) == 1
        if target != "npm-sink-dispatcher":
            assert captures[0].stdout_tail == "settled stdout\n"
        if target == "npm-stderr-reader":
            assert captures[0].stderr_tail == "settled stderr\n"
        if wait_interrupt:
            assert any("KeyboardInterrupt" in note for note in failure.__notes__)
        if hook_error:
            assert any("unblock failure" in note for note in failure.__notes__)
        before = tuple(joined)
        dispatcher = finalized[0]
        assert dispatcher.finish() == dispatcher.truncation_notice()
        assert tuple(joined) == before
        assert all(t._os_thread_handle.is_done() for t in created)
    finally:
        # Also settle genuine pending native workers on a RED assertion. Python
        # join is not valid until the delayed bootstrap sets its started event.
        allow_bootstrap.set()
        release_readers.set()
        for thread in created:
            if (
                thread._os_thread_handle.ident
                and not thread._os_thread_handle.is_done()
            ):
                assert original_waits[thread](5)
                join(thread, 5)
                assert thread._os_thread_handle.is_done()


@pytest.mark.parametrize("target", ["npm-stdout-reader", "npm-stderr-reader"])
@pytest.mark.parametrize("after_start", [False, True])
@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt])
@pytest.mark.parametrize(
    "cleanup_error",
    [None, OSError("unblock failure"), KeyboardInterrupt("unblock failure")],
)
def test_partial_reader_start_preserves_failure_and_finishes_every_worker(
    target, after_start, error_type, cleanup_error
):
    failure = error_type("reader startup")
    release = threading.Event()
    created = []
    finish_calls = []
    callback_calls = []
    start = threading.Thread.start
    finish = streaming.SinkDispatcher.finish

    def read(_size):
        if not release.is_set():
            release.wait(5)
        return b""

    def fail_start(thread):
        if thread.name in ("npm-stdout-reader", "npm-stderr-reader"):
            created.append(thread)
        if thread.name == target and not after_start:
            raise failure
        start(thread)
        if thread.name == target:
            raise failure

    def unblock():
        callback_calls.append(True)
        release.set()
        if cleanup_error is not None:
            raise cleanup_error

    def finish_dispatcher(dispatcher):
        finish_calls.append(True)
        return finish(dispatcher)

    try:
        with (
            mock.patch.object(threading.Thread, "start", fail_start),
            mock.patch.object(streaming.SinkDispatcher, "finish", finish_dispatcher),
            pytest.raises(error_type) as raised,
        ):
            streaming.collect_streams(
                stdout_read=read,
                stderr_read=read,
                sink=lambda chunk: None,
                on_interruption=unblock,
            )
        assert raised.value is failure
        assert hasattr(failure, "retained_capture")
        assert callback_calls == finish_calls == [True]
        assert all(not thread.is_alive() for thread in created)
        if cleanup_error is not None:
            assert any("unblock failure" in note for note in failure.__notes__)
    finally:
        release.set()
        for thread in created:
            if thread.ident is not None and thread.is_alive():
                thread.join(5)


@pytest.mark.parametrize("after_start", [False, True])
@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt])
@pytest.mark.parametrize("cleanup_error", [False, True])
def test_dispatcher_partial_start_is_owned_and_cleanup_is_not_retried(
    after_start, error_type, cleanup_error, monkeypatch
):
    failure = error_type("dispatcher startup")
    start = threading.Thread.start
    join = threading.Thread.join
    finish = streaming.SinkDispatcher.finish
    finalized = []
    callbacks = []
    joins = []

    def counted_join(thread, *args, **kwargs):
        if thread.name == "npm-sink-dispatcher":
            joins.append(thread)
        return join(thread, *args, **kwargs)

    monkeypatch.setattr(threading.Thread, "join", counted_join)

    def fail_start(thread):
        if thread.name == "npm-sink-dispatcher" and not after_start:
            raise failure
        start(thread)
        if thread.name == "npm-sink-dispatcher":
            raise failure

    def finalize(dispatcher):
        finalized.append(dispatcher)
        result = finish(dispatcher)
        if cleanup_error:
            raise OSError("dispatcher cleanup")
        return result

    try:
        with (
            mock.patch.object(threading.Thread, "start", fail_start),
            mock.patch.object(streaming.SinkDispatcher, "finish", finalize),
            pytest.raises(error_type) as raised,
        ):
            streaming.collect_streams(
                stdout_read=lambda size: b"",
                stderr_read=lambda size: b"",
                sink=lambda chunk: None,
                on_interruption=lambda: callbacks.append(True),
            )
        assert raised.value is failure
        assert hasattr(failure, "retained_capture")
        assert len(finalized) == 1
        assert callbacks == [True]
        dispatcher = finalized[0]
        assert not dispatcher.is_alive()
        assert dispatcher.finish() == dispatcher.truncation_notice()
        assert not dispatcher.is_alive()
        assert len(joins) == (1 if after_start else 0)
        if cleanup_error:
            assert any(
                "dispatcher startup cleanup failed" in n for n in failure.__notes__
            )
    finally:
        # RED diagnostics must also leave no dispatcher worker behind.
        for dispatcher in finalized:
            finish(dispatcher)


@pytest.mark.parametrize("join_error", [OSError, KeyboardInterrupt])
@pytest.mark.parametrize("startup_error", [None, RuntimeError])
def test_join_failure_does_not_skip_sibling_or_dispatcher(join_error, startup_error):
    failure = join_error("join observation")
    primary = startup_error("startup") if startup_error is not None else None
    start = threading.Thread.start
    join = threading.Thread.join
    finish = streaming.SinkDispatcher.finish
    joined = []
    finished = []

    def start_reader(thread):
        start(thread)
        if primary is not None and thread.name == "npm-stderr-reader":
            raise primary

    def join_reader(thread, *args, **kwargs):
        result = join(thread, *args, **kwargs)
        if thread.name in ("npm-stdout-reader", "npm-stderr-reader"):
            joined.append(thread.name)
            if thread.name == "npm-stdout-reader":
                raise failure
        return result

    def finish_dispatcher(dispatcher):
        finished.append(True)
        return finish(dispatcher)

    with (
        mock.patch.object(threading.Thread, "start", start_reader),
        mock.patch.object(threading.Thread, "join", join_reader),
        mock.patch.object(streaming.SinkDispatcher, "finish", finish_dispatcher),
        pytest.raises(BaseException) as raised,
    ):
        streaming.collect_streams(
            stdout_read=lambda size: b"",
            stderr_read=lambda size: b"",
            sink=lambda chunk: None,
        )
    assert raised.value is (primary if primary is not None else failure)
    assert joined == ["npm-stdout-reader", "npm-stderr-reader"]
    assert finished == [True]
    assert hasattr(raised.value, "retained_capture")


@pytest.mark.parametrize("finish_error", [OSError, KeyboardInterrupt])
@pytest.mark.parametrize("startup_error", [None, RuntimeError])
def test_dispatcher_finish_failure_is_reported_without_replacing_startup(
    finish_error, startup_error
):
    failure = finish_error("dispatcher finish")
    primary = startup_error("startup") if startup_error is not None else None
    start = threading.Thread.start
    finish = streaming.SinkDispatcher.finish
    threads, finished = [], []

    def start_reader(thread):
        start(thread)
        if thread.name in ("npm-stdout-reader", "npm-stderr-reader"):
            threads.append(thread)
        if primary is not None and thread.name == "npm-stderr-reader":
            raise primary

    def finish_dispatcher(dispatcher):
        finish(dispatcher)
        finished.append(True)
        raise failure

    with (
        mock.patch.object(threading.Thread, "start", start_reader),
        mock.patch.object(streaming.SinkDispatcher, "finish", finish_dispatcher),
        pytest.raises(BaseException) as raised,
    ):
        streaming.collect_streams(
            stdout_read=lambda size: b"",
            stderr_read=lambda size: b"",
            sink=lambda chunk: None,
        )
    assert raised.value is (primary if primary is not None else failure)
    assert finished == [True]
    assert all(not thread.is_alive() for thread in threads)
    assert hasattr(raised.value, "retained_capture")
    if primary is not None:
        assert any("dispatcher finish" in note for note in primary.__notes__)


def test_startup_sigint_retains_report_through_the_original_cli_boundary():
    start = threading.Thread.start
    reader = renderer_fixture._ServeOnceThenBlock
    releases, threads = [], []

    def observed_reader(data, released, served):
        releases.append(released)
        return reader(data, released, served)

    def delayed_start(thread):
        start(thread)
        if thread.name in ("npm-stdout-reader", "npm-stderr-reader"):
            threads.append(thread)
        if thread.name == "npm-stderr-reader":
            threading.Event().wait(2)

    result = unittest.TestResult()
    try:
        with (
            mock.patch.object(renderer_fixture, "_ServeOnceThenBlock", observed_reader),
            mock.patch.object(threading.Thread, "start", delayed_start),
        ):
            renderer_fixture.TestInterruptionPresentationOwnership(
                "test_failed_renderer_never_falls_back_or_retries"
            ).run(result)
        assert result.wasSuccessful(), (result.failures, result.errors)
    finally:
        for release in releases:
            release.set()
        for thread in threads:
            thread.join(5)
            assert not thread.is_alive()


def test_action_authority_fixture_does_not_charge_scheduler_pause_to_stage_cap():
    start = action_fixture.Action.start

    def delayed_start(action, context):
        start(action, context)
        time.sleep(0.15)

    with mock.patch.object(action_fixture.Action, "start", delayed_start):
        action_fixture.test_poll_and_action_failure_preserve_authority_and_independent_cleanup(
            process, RuntimeError, OSError, None
        )


@pytest.mark.parametrize("setup_failure", [False, True])
@pytest.mark.parametrize("primary_kind", [ValueError, KeyboardInterrupt])
@pytest.mark.parametrize("helper_kind", [OSError, RuntimeError, KeyboardInterrupt])
def test_captured_fault_fixture_observes_final_cleanup_after_early_return(
    setup_failure, primary_kind, helper_kind
):
    from tests import test_process_captured_action_faults as fixture

    release, entered = threading.Event(), threading.Event()
    start = fixture.CapturedAction.start
    prepare = fixture.CapturedAction.prepare
    shutdown = process.OwnedProcess._shutdown
    snapshots = []

    def held_start(action, context):
        start(action, context)
        entered.set()
        assert release.wait(5)

    def prepared(action):
        prepare(action)
        wait = action.done.wait

        def release_and_wait(timeout=None):
            try:
                return wait(timeout)
            finally:
                release.set()

        action.done.wait = release_and_wait

    def early_shutdown(owner, *args, **kwargs):
        if owner._policy is None or release.is_set():
            return shutdown(owner, *args, **kwargs)

        # Admit the real action before returning an unsettled snapshot. Hold
        # local cleanup until the fixture observes action completion.
        def phase_wait(timeout=None):
            assert entered.wait(5)
            return False

        with mock.patch.object(owner._phase_done, "wait", phase_wait):
            result = shutdown(owner, *args, **kwargs)
        assert not result.complete
        if not snapshots:
            snapshots.append(result)
        return result

    try:
        with (
            mock.patch.object(fixture.CapturedAction, "start", held_start),
            mock.patch.object(process.OwnedProcess, "_shutdown", early_shutdown),
            mock.patch.object(fixture.CapturedAction, "prepare", prepared),
        ):
            fixture.test_main_and_captured_helper_faults_share_precedence_and_single_owners(
                setup_failure, "transfer", 1, helper_kind, primary_kind
            )
        assert len(snapshots) == 1
        assert not snapshots[0].complete  # Old snapshots do not mutate.
        assert len(snapshots[0].failures) == 1
        assert type(snapshots[0].failures[0].error) is primary_kind
    finally:
        release.set()


def test_parent_exit_fixture_establishes_exit_before_short_drain_budget():
    popen = lifecycle.subprocess.Popen

    def delayed_start(argv, **kwargs):
        command = list(argv)
        assert command[1] == "-c"
        command[2] = "import time;time.sleep(0.2);" + command[2]
        # Only called as a factory under OwnedProcess.launch.
        return popen(command, **kwargs)

    result = unittest.TestResult()
    with mock.patch.object(lifecycle.subprocess, "Popen", delayed_start):
        lifecycle_fixture.TestLifecyclePrimitives(
            "test_parent_exit_with_descendant_holding_pipes_is_bounded"
        ).run(result)
    assert result.wasSuccessful(), (result.failures, result.errors)
