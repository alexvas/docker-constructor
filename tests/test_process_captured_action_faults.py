"""6BC.3 RED: captured helper ownership inside the generic main coordinator.

The action is a test-only nonblocking prepared worker, not a domain wrapper.
Every helper acquisition still passes through the captured adapter; every main
acquisition is an OwnedProcess launch. Fault seams never close candidates.
"""

import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest import mock

import pytest

import docker.process as process
from docker.filesystem.streams import OwnedStream
from docker.npm_environment import lifecycle
from docker.process import (
    ActionResult,
    FailureInfo,
    FailureKind,
    FailurePhase,
    OwnedProcess,
    StopPolicy,
    StopStage,
)
from tests.test_process_failure_snapshot import Child
from tests.test_process_lifecycle import secondary, workers


class CapturedAction:
    def __init__(self, argv=("helper",)):
        self.argv = argv
        self.ready = threading.Event()
        self.request = threading.Event()
        self.done = threading.Event()
        self.started = False
        self.cancelled = False
        self.owner = None
        self.outcome = None
        self.failures = ()
        self.calls = []
        self.thread = threading.Thread(target=self._work, name="test-captured-action")

    def prepare(self):
        self.calls.append("prepare")
        self.thread.start()
        assert self.ready.wait(1)

    def start(self, context):
        self.calls.append("start")
        self.context = context
        self.started = True
        self.request.set()

    def observe(self):
        if not self.done.is_set():
            return ActionResult(False, False)
        if self.thread.ident is not None:
            self.thread.join(0)
        complete = self.thread.ident is None or not self.thread.is_alive()
        failures = list(self.failures)
        if isinstance(self.owner, OwnedProcess):
            status = self.owner.cleanup_status
            complete = complete and status.complete
            known = {(id(f.error), f.kind) for f in failures}
            failures.extend(
                FailureInfo(f.error, f.kind, FailurePhase.ACTION)
                for f in status.failures
                if (id(f.error), f.kind) not in known
            )
        return ActionResult(True, complete, tuple(failures))

    def cancel(self, deadline):
        self.calls.append("cancel")
        self.cancelled = True
        self.request.set()

    def _work(self):
        self.ready.set()
        try:
            assert self.request.wait(5)
            if self.started:
                # Three legacy intervals must fit even while the adapter is
                # RED; the migrated helper stays within this same allowance.
                remaining = self.context.deadline - time.monotonic()
                if remaining <= 0:
                    error = TimeoutError("helper stage expired")
                    self.outcome = lifecycle.LifecycleOutcome(errors=[error])
                    self.failures = (
                        FailureInfo(error, FailureKind.ORDINARY, FailurePhase.ACTION),
                    )
                    return
                grace = min(0.02, remaining / 3)
                operation = lifecycle._run_captured(
                    self.argv, policy=lifecycle.LifecyclePolicy(grace)
                )
                self.owner, self.outcome = operation.owner, operation.outcome
                self.failures = tuple(
                    FailureInfo(f.error, f.kind, FailurePhase.ACTION)
                    for f in operation.failures
                )
        except BaseException as exc:
            self.owner = getattr(exc, "cleanup", None)
            if isinstance(self.owner, OwnedProcess):
                self.failures = tuple(
                    FailureInfo(f.error, f.kind, FailurePhase.ACTION)
                    for f in self.owner.cleanup_status.failures
                )
            if not any(f.error is exc for f in self.failures):
                kind = (
                    FailureKind.INTERRUPTION
                    if not isinstance(exc, Exception)
                    else FailureKind.ORDINARY
                    if isinstance(exc, OSError)
                    else FailureKind.UNEXPECTED
                )
                self.failures += (FailureInfo(exc, kind, FailurePhase.ACTION),)
            self.outcome = lifecycle.LifecycleOutcome(
                errors=[f.error for f in self.failures]
            )
        finally:
            self.done.set()

    def finish(self):
        self.request.set()
        self.thread.join(2)
        assert not self.thread.is_alive()
        if isinstance(self.owner, OwnedProcess):
            assert self.owner.wait_cleanup(1).complete


@pytest.mark.parametrize("setup_failure", [False, True])
@pytest.mark.parametrize("helper_source", ["transfer", "join", "close"])
@pytest.mark.parametrize("helper_position", [0, 1])
@pytest.mark.parametrize("helper_kind", [OSError, RuntimeError, KeyboardInterrupt])
@pytest.mark.parametrize("primary_kind", [ValueError, KeyboardInterrupt])
def test_main_and_captured_helper_faults_share_precedence_and_single_owners(
    setup_failure,
    helper_source,
    helper_position,
    helper_kind,
    primary_kind,
):
    primary = primary_kind("domain operation")
    helper_error = helper_kind("helper fault")
    main_release = OSError("main release")
    main_join = OSError("main join")
    main, helper = Child(main_release), Child()
    if helper_source == "close":
        (helper.stdout if helper_position == 0 else helper.stderr).error = helper_error
    action = CapturedAction()
    tracked = {}
    join_failed = set()
    original_transfer, original_join = OwnedProcess._transfer, OwnedProcess._join_reader

    def transfer(owner, index):
        previous = tracked.setdefault(id(owner._proc), owner)
        assert previous is owner, "helper/main ownership was reconstructed"
        if (
            owner._proc is helper
            and helper_source == "transfer"
            and index == helper_position
        ):
            raise helper_error
        original_transfer(owner, index)

    def join(owner, index, timeout):
        child = owner._proc
        selected = (child is main and index == 0) or (
            child is helper and helper_source == "join" and index == helper_position
        )
        if selected and (id(child), index) not in join_failed:
            join_failed.add((id(child), index))
            raise main_join if child is main else helper_error
        original_join(owner, index, timeout)

    def fail_launch_hook():
        raise primary

    owner = None
    try:
        with (
            mock.patch.object(
                lifecycle.subprocess, "Popen", return_value=helper
            ) as helper_factory,
            mock.patch.object(OwnedProcess, "_transfer", transfer),
            mock.patch.object(OwnedProcess, "_join_reader", join),
        ):
            try:
                owner = OwnedProcess.launch(
                    ["main"],
                    factory=lambda *a, **k: main,
                    grace_seconds=0.3,
                    stop_policy=StopPolicy(
                        (StopStage(0.15, action, local_stop_before=True),), 0.1
                    ),
                    on_launched=fail_launch_hook if setup_failure else None,
                )
            except BaseException as exc:
                assert setup_failure
                owner = tracked[id(main)]
                raised = exc
            else:
                raised = None
            result = (
                (owner.capture().lifecycle if raised is None else owner.cleanup_status)
                if setup_failure
                else owner.shutdown(primary=primary)
            )
            assert action.done.wait(1)
            # Bounded shutdown/setup can return an immutable, incomplete
            # snapshot. Action.done alone proves neither helper nor main
            # cleanup settled: observe the SAME owner's final continuation.
            result = owner.wait_cleanup(1)
            assert id(helper) in tracked, (
                "captured helper must delegate to generic launch"
            )
            helper_owner = tracked[id(helper)]
            if helper_source == "transfer":
                # Ordinary failed setup returns its prepared owner; a launch
                # interruption need not return one, but owns no leaked pipes.
                assert action.owner is None or action.owner is helper_owner
            else:
                assert isinstance(action.owner, OwnedProcess)
                assert action.owner is helper_owner
            assert helper_owner._policy is None
            assert helper_factory.call_count == 1
            # Main reader joins have no ordinary-error allowance. Even an
            # OSError there is a cleanup defect, ahead of an ordinary primary
            # and ordinary managed-action/release errors, but never ahead of
            # an already active interruption.
            winner = (
                primary
                if primary_kind is KeyboardInterrupt
                else main_join
                if helper_kind is OSError and helper_source != "join"
                else helper_error
            )
            assert result.complete and result.failures[0].error is winner
            if setup_failure:
                if primary_kind is KeyboardInterrupt:
                    assert raised is primary
                elif helper_kind is KeyboardInterrupt:
                    # A helper interrupt discovered after launch returned is
                    # retained in the final result, not thrown retroactively.
                    assert raised is None or raised is helper_error
                else:
                    assert raised is None
            assert all(
                any(f.error is e for f in result.failures)
                for e in (primary, helper_error, main_join, main_release)
            )
            assert action.calls == ["prepare", "start", "cancel"]
            assert main.stdout.attempts == main.stderr.attempts == 1
            assert helper.stdout.attempts == helper.stderr.attempts == 1
            before = (tuple(action.calls), tuple(secondary(winner)))
            assert (
                owner.shutdown().failures[0].error is winner
                and owner.wait_cleanup(1).complete
            )
            assert helper_owner.wait_cleanup(1).complete
            assert before == (tuple(action.calls), tuple(secondary(winner)))
    finally:
        action.finish()
        if owner is not None:
            owner.shutdown()
            assert owner.wait_cleanup(1).complete
        assert not workers()


@pytest.mark.parametrize("primary_kind", [ValueError, KeyboardInterrupt])
@pytest.mark.parametrize("position", [0, 1])
def test_join_oserror_is_a_defect_but_never_displaces_active_interruption(
    primary_kind, position
):
    primary = primary_kind("active operation")
    join_error, close_error = OSError("reader join"), OSError("pipe release")
    child = Child(close_error)
    owner = OwnedProcess.launch(
        ["main"], factory=lambda *a, **k: child, grace_seconds=0.1
    )
    original_join = owner._join_reader
    joined = []

    def join(index, timeout):
        joined.append(index)
        if index == position:
            raise join_error
        original_join(index, timeout)

    try:
        assert all(event.wait(1) for event in owner._stopped)
        with (
            mock.patch.object(owner, "_join_reader", side_effect=join),
            mock.patch.object(child, "wait", wraps=child.wait) as reap,
        ):
            result = owner.shutdown(primary=primary)
        winner = primary if primary_kind is KeyboardInterrupt else join_error
        assert result.complete and result.failures[0].error is winner
        assert joined == [0, 1] and reap.call_count == 1
        assert all(
            any(f.error is error for f in result.failures)
            for error in (primary, join_error, close_error)
        )
        assert child.stdout.attempts == child.stderr.attempts == 1
        notes = tuple(secondary(winner))
        assert owner.shutdown().failures[0].error is winner
        assert owner.wait_cleanup(1).complete
        assert tuple(secondary(winner)) == notes
        assert joined == [0, 1]
        assert child.stdout.attempts == child.stderr.attempts == 1
    finally:
        owner.shutdown()
        assert owner.wait_cleanup(1).complete


@pytest.mark.parametrize("primary_kind", [ValueError, KeyboardInterrupt])
def test_captured_helper_failure_and_primary_merge_in_exact_publication_gap(
    primary_kind,
):
    helper_error = OSError("helper release")
    helper = Child(helper_error)
    action = CapturedAction()
    owner = None
    published, proceed, merged = (threading.Event() for _ in range(3))
    primary = primary_kind("domain operation")
    results = []
    caller = None
    try:
        with mock.patch.object(lifecycle.subprocess, "Popen", return_value=helper):
            owner = OwnedProcess.launch(
                ["main"],
                factory=lambda *a, **k: Child(),
                grace_seconds=1,
                stop_policy=StopPolicy(
                    (StopStage(0.2, action, local_stop_before=True),), 0.2
                ),
            )
            real_set, real_wait = owner._phase_done.set, owner._phase_done.wait

            def set_done():
                published.set()
                assert proceed.wait(5)
                real_set()

            def wait_done(timeout=None):
                if threading.current_thread().name == "helper-gap-shutdown":
                    merged.set()
                return real_wait(timeout)

            caller = threading.Thread(
                target=lambda: results.append(owner.shutdown(primary=primary)),
                name="helper-gap-shutdown",
            )
            with (
                mock.patch.object(owner._phase_done, "set", set_done),
                mock.patch.object(owner._phase_done, "wait", wait_done),
            ):
                owner._deadline = 0.01
                assert published.wait(1)
                old = owner.cleanup_status
                assert old.complete and old.failures[0].error is helper_error
                assert not owner._delivered and not owner._phase_done.is_set()
                caller.start()
                assert merged.wait(1)
                with owner._lock:
                    assert (
                        owner._ledger.original_primary is primary
                        or primary in owner._ledger.interruptions
                    )
                proceed.set()
                caller.join(2)
                assert not caller.is_alive()
            assert (
                results
                and results[0].complete
                and results[0].failures[0].error is primary
            )
            assert [f.error for f in old.failures] == [helper_error]
            assert [f.error for f in results[0].failures[1:]] == [helper_error]
            assert secondary(primary) == []
            assert isinstance(action.owner, OwnedProcess)
            assert action.calls == ["prepare", "start", "cancel"]
            assert helper.stdout.attempts == helper.stderr.attempts == 1
            assert owner.shutdown() == results[0]
    finally:
        proceed.set()
        if caller is not None and caller.ident is not None:
            caller.join(2)
        action.finish()
        if owner is not None:
            owner.shutdown()
            assert owner.wait_cleanup(1).complete
        assert not workers()


def test_real_main_retains_incomplete_captured_helper_and_late_release_outcome():
    entered, release, reader_stopped = (threading.Event() for _ in range(3))
    primary = ValueError("domain operation")
    late_error = OSError("late helper release")
    action = CapturedAction((sys.executable, "-c", "print('helper')"))
    children = []
    factory = subprocess.Popen
    read = OwnedProcess._read_chunk
    close = OwnedStream.close
    owner = None
    clock = [time.monotonic()]
    monotonic, pause = time.monotonic, process._policy_pause

    def policy_time():
        if owner is not None and threading.current_thread() is owner._continuation:
            return clock[0]
        return monotonic()

    def policy_pause(seconds):
        if owner is not None and threading.current_thread() is owner._continuation:
            # Establish real helper ownership before consuming logical stage
            # time; this fixture tests retained cleanup, not helper startup speed.
            assert entered.wait(5)
            assert action.done.wait(5)
            clock[0] += seconds
        else:
            pause(seconds)

    def spawn(*args, **kwargs):
        child = factory(*args, **kwargs)
        children.append(child)
        return child

    def blocked_read(owner, index):
        if len(children) == 2 and owner._proc is children[1] and index == 0:
            entered.set()
            assert release.wait(5), "test-controlled helper reader leaked"
            reader_stopped.set()
            return b""
        return read(owner, index)

    def late_close(stream_owner):
        is_helper = len(children) == 2 and stream_owner.stream is children[1].stdout
        if is_helper and entered.is_set():
            assert reader_stopped.is_set(), (
                "helper pipe closed under live descriptor reader"
            )
        close(stream_owner)
        if is_helper:
            raise late_error

    try:
        with (
            mock.patch.object(lifecycle.subprocess, "Popen", side_effect=spawn),
            mock.patch.object(OwnedProcess, "_read_chunk", blocked_read),
            mock.patch.object(OwnedStream, "close", late_close),
            mock.patch.object(
                process,
                "time",
                SimpleNamespace(monotonic=policy_time, sleep=time.sleep),
            ),
            mock.patch.object(process, "_policy_pause", policy_pause),
            mock.patch(__name__ + ".time", SimpleNamespace(monotonic=lambda: clock[0])),
        ):
            owner = OwnedProcess.launch(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                factory=spawn,
                grace_seconds=0.2,
                stop_policy=StopPolicy(
                    (StopStage(0.1, action, local_stop_before=True),), 0.08
                ),
            )
            clock[0] = monotonic()
            phase_wait = owner._phase_done.wait
            # Fixture-startup watchdog, not a new production shutdown allowance.
            with mock.patch.object(
                owner._phase_done, "wait", lambda timeout=None: phase_wait(5)
            ):
                result = owner.shutdown(primary=primary)
            assert entered.is_set(), "captured action must delegate real helper readers"
            assert isinstance(action.owner, OwnedProcess)
            assert not result.complete and result.failures[0].error is primary
            assert not action.owner.cleanup_status.complete
            assert len(children) == 2
            assert (
                children[0].returncode is not None
                and children[1].returncode is not None
            )
            assert children[0].stdout.closed and children[0].stderr.closed
            assert not children[1].stdout.closed
            before = (
                tuple(secondary(primary)),
                tuple(action.calls),
                action.outcome.stdout,
                tuple(action.outcome.errors),
            )
            assert not owner.wait_cleanup(0.01).complete
            release.set()
            assert action.owner.wait_cleanup(1).complete
            assert any(
                f.error is late_error for f in action.owner.cleanup_status.failures
            )
            assert owner.wait_cleanup(1).complete
            assert not result.complete and result.failures[0].error is primary
            assert before == (
                tuple(secondary(primary)),
                tuple(action.calls),
                action.outcome.stdout,
                tuple(action.outcome.errors),
            )
            assert children[1].stdout.closed and children[1].stderr.closed
    finally:
        release.set()
        action.finish()
        if owner is not None:
            owner.shutdown()
            assert owner.wait_cleanup(1).complete
        assert not workers()
