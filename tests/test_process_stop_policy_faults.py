"""Readiness/action/descriptor matrices migrated to typed lifecycle values."""
from __future__ import annotations

import os
import sys
import threading
import time
from unittest import mock

import pytest

from docker.filesystem.descriptors import OwnedDescriptor
from docker.filesystem.operations import PosixDescriptorOps
from tests.test_process_stop_policy import api, Action, launch, plan
from tests.test_process_lifecycle import Child, workers, secondary


def errors(result):
    return tuple(f.error for f in result.failures)


def authority(result):
    return result.failures[0].error if result.failures else None


@pytest.mark.parametrize("boundary", ["start", "observe", "cancel"])
@pytest.mark.parametrize("kind", [OSError, RuntimeError, KeyboardInterrupt])
def test_each_action_boundary_preserves_primary_and_independent_release(api, boundary, kind):
    error = kind("action boundary")
    action = Action(api)
    original = getattr(action, boundary)

    def fail(*args):
        original(*args)
        raise error

    setattr(action, boundary, fail)
    other = Action(api)
    policy = api.StopPolicy((api.StopStage(.01, action), api.StopStage(.01, other)), .025)
    child = Child((OSError("release"), None))
    primary = ValueError("domain operation")
    owner = launch(api, policy, child)
    try:
        result = owner.shutdown(primary=primary)
        assert authority(result) is (primary if kind is OSError else error)
        assert other.calls == ["prepare", "start", "cancel"]
        assert child.stdout.attempts == child.stderr.attempts == 1
        assert action.calls.count("start") == action.calls.count("cancel") == 1
        if boundary == "observe":
            assert not result.complete
            action.observe = original  # Repair observation, never retry requests.
        assert owner.wait_cleanup(1).complete
    finally:
        if boundary == "observe":
            action.observe = original
        owner.wait_cleanup(1)
    assert not workers()


@pytest.mark.parametrize("next_stage", [False, True])
@pytest.mark.parametrize("boundary", ["context", "wait", "wait_without_action"])
@pytest.mark.parametrize("kind", [OSError, RuntimeError, KeyboardInterrupt])
@pytest.mark.parametrize("primary_kind", [None, ValueError, KeyboardInterrupt])
def test_stage_child_poll_failure_advances_and_preserves_cleanup(api, boundary, kind, primary_kind, next_stage):
    error = kind("stage poll")
    primary = primary_kind("operation") if primary_kind else None
    release_error = OSError("independent release")
    child = Child((release_error, None))
    first, following = Action(api), Action(api)
    first_action = None if boundary == "wait_without_action" else first
    stages = (api.StopStage(.01, first_action, wait_child=boundary != "context"),)
    if next_stage:
        stages += (api.StopStage(.01, following),)
    policy = api.StopPolicy(stages, .025)
    original_poll = child.poll
    calls = []
    failure_at = 2 if boundary == "wait" else 1

    def poll():
        calls.append(True)
        if len(calls) == failure_at:
            raise error
        return original_poll()

    child.poll = poll
    owner = launch(api, policy, child)
    try:
        result = owner.shutdown(primary=primary)
        winner = primary if isinstance(primary, KeyboardInterrupt) or (primary is not None and kind is OSError) else error
        assert result.complete and authority(result) is winner
        assert error in errors(result) and release_error in errors(result)
        assert following.calls == (["prepare", "start", "cancel"] if next_stage else [])
        if first_action is not None:
            assert first.calls == ["prepare", "start", "cancel"]
            if boundary == "context":
                assert first.contexts[0].returncode is None
        assert child.actions == ["terminate", "wait"]
        assert child.stdout.attempts == child.stderr.attempts == 1
        before = (list(calls), list(child.actions), list(following.calls), tuple(secondary(winner)))
        assert authority(owner.shutdown()) is winner
        assert owner.wait_cleanup(1).complete
        assert before == (calls, child.actions, following.calls, tuple(secondary(winner)))
    finally:
        child.poll = original_poll
        owner.wait_cleanup(.01)
    assert not workers()


@pytest.mark.parametrize("kind", [OSError, RuntimeError, KeyboardInterrupt])
def test_persistent_stage_poll_failure_still_reaps_and_releases(api, kind):
    error = kind("persistent poll")
    child = Child()
    child.poll = mock.Mock(side_effect=error)
    first, second = Action(api), Action(api)
    owner = launch(api, api.StopPolicy((api.StopStage(.01, first), api.StopStage(.01, second)), .025), child)
    result = owner.shutdown()
    assert result.complete and authority(result) is error
    assert first.calls == second.calls == ["prepare", "start", "cancel"]
    assert first.contexts[0].returncode is second.contexts[0].returncode is None
    assert first.calls.count("start") == second.calls.count("start") == 1
    assert child.actions == ["wait", "kill", "wait"]
    assert child.stdout.attempts == child.stderr.attempts == 1
    calls = child.poll.call_count
    assert authority(owner.shutdown()) is error and owner.wait_cleanup(1).complete
    assert child.poll.call_count == calls
    assert not workers()


@pytest.mark.parametrize("poll_kind", [OSError, RuntimeError, KeyboardInterrupt])
@pytest.mark.parametrize("release_kind", [OSError, RuntimeError, KeyboardInterrupt])
def test_context_exit_poll_failure_always_cleans_up(api, poll_kind, release_kind):
    poll_error, release_error = poll_kind("exit poll"), release_kind("release")
    sibling_error = OSError("sibling release")
    child = Child((release_error, sibling_error))
    original = child.poll
    owner = launch(api, None, child)

    def poll_once():
        child.poll = original
        raise poll_error

    child.poll = poll_once
    expected = (poll_error if poll_kind is KeyboardInterrupt else release_error if release_kind is KeyboardInterrupt
                else poll_error if poll_kind is RuntimeError else release_error if release_kind is RuntimeError else poll_error)
    try:
        with pytest.raises(type(expected)) as caught:
            owner.__exit__(None, None, None)
        assert caught.value is expected
        status = owner.wait_cleanup(1)
        assert status.complete and authority(status) is expected
        assert all(e in errors(status) for e in (poll_error, release_error, sibling_error))
        assert child.stdout.attempts == child.stderr.attempts == 1
        assert child.actions == ["terminate", "wait"]
        assert authority(owner.shutdown()) is expected
    finally:
        owner.shutdown()
        owner.wait_cleanup(1)
    assert not workers()


@pytest.mark.parametrize("primary_kind", [ValueError, KeyboardInterrupt])
def test_context_exit_active_primary_skips_poll_and_keeps_identity(api, primary_kind):
    primary = primary_kind("body")
    child = Child((OSError("release"), None))
    owner = launch(api, None, child)
    child.poll = mock.Mock(wraps=child.poll)
    with mock.patch.object(owner, "_shutdown", wraps=owner._shutdown) as shutdown:
        with pytest.raises(primary_kind) as caught:
            owner.__exit__(primary_kind, primary, None)
    assert caught.value is primary and owner.wait_cleanup(1).complete
    assert shutdown.call_args.kwargs == {"primary": primary, "reason": "context"}
    assert child.poll.call_count == 1
    assert child.stdout.attempts == child.stderr.attempts == 1
    assert not workers()


@pytest.mark.parametrize("after_delivery", [False, True])
@pytest.mark.parametrize("with_defect", [False, True])
def test_raised_observer_errors_escalate_by_class_without_delivery_mutation(api, after_delivery, with_defect):
    ordinary, defect, interruption = OSError("observe"), RuntimeError("observe"), KeyboardInterrupt("observe")
    action = Action(api)
    original_observe = action.observe
    current = [ordinary]

    def observe():
        raise current[0]

    action.observe = observe
    owner = launch(api, plan(api, action))

    def escalate():
        for error in ([defect, interruption] if with_defect else [interruption]):
            current[0] = error
            owner._observe_actions()
            for _ in range(5):
                current[0] = type(error)("duplicate")
                owner._observe_actions()
            current[0] = error

    publish = owner._publish

    def publish_escalated(complete):
        escalate()
        publish(complete)

    try:
        if not after_delivery:
            owner._publish = publish_escalated
        initial = owner.shutdown()
        owner._publish = publish
        assert authority(initial) is (ordinary if after_delivery else interruption)
        before = tuple(secondary(authority(initial)))
        saved = initial.failures
        if after_delivery:
            escalate()
        action.observe = original_observe
        observed = owner.wait_cleanup(1)
        assert observed.complete and authority(observed) is interruption
        assert interruption in errors(observed) and ordinary in errors(observed)
        if with_defect:
            assert defect in errors(observed)
        assert len(observed.failures) == (3 if with_defect else 2)
        assert authority(initial) is (ordinary if after_delivery else interruption)
        assert tuple(secondary(authority(initial))) == before and initial.failures == saved
        assert not hasattr(initial, "actions")
        assert all(f.phase is api.FailurePhase.ACTION for f in initial.failures)
        assert action.calls == ["prepare", "start", "cancel"]
    finally:
        owner._publish = publish
        action.observe = original_observe
        owner.wait_cleanup(1)
    assert not workers()


@pytest.mark.parametrize("boundary", ["start", "cancel"])
@pytest.mark.parametrize("kind", [OSError, RuntimeError, KeyboardInterrupt])
@pytest.mark.parametrize("pending", [False, True])
def test_action_boundary_failure_survives_successful_and_late_observation(api, boundary, kind, pending):
    error = kind("boundary")
    primary = ValueError("domain primary")
    action = Action(api, pending=pending)
    original = getattr(action, boundary)

    def fail(*args):
        original(*args)
        raise error

    setattr(action, boundary, fail)
    child = Child()
    owner = launch(api, plan(api, action), child)
    try:
        initial = owner.shutdown(primary=primary)
        assert authority(initial) is (primary if kind is OSError else error)
        assert any(f.error is error and f.phase is api.FailurePhase.ACTION for f in initial.failures)
        assert initial.complete is not pending
        assert child.stdout.attempts == child.stderr.attempts == 1
        before = (initial.failures, tuple(secondary(authority(initial))))
        if pending:
            late = KeyboardInterrupt("late observation")
            action.error = late
            action.release.set()
            observed = owner.wait_cleanup(1)
            winner = error if kind is KeyboardInterrupt else late
            assert observed.complete and authority(observed) is winner
            assert error in errors(observed) and late in errors(observed)
        else:
            observed = owner.wait_cleanup(1)
            assert error in errors(observed)
        assert before == (initial.failures, tuple(secondary(authority(initial))))
        assert authority(owner.shutdown()) is authority(observed)
        assert not hasattr(initial, "actions")
        assert action.calls == ["prepare", "start", "cancel"]
    finally:
        action.release.set()
        owner.wait_cleanup(1)
    assert not workers()


@pytest.mark.parametrize("start_kind", [OSError, RuntimeError, KeyboardInterrupt])
@pytest.mark.parametrize("cancel_kind", [OSError, RuntimeError, KeyboardInterrupt])
def test_start_and_cancel_action_errors_use_shared_precedence(api, start_kind, cancel_kind):
    start_error, cancel_error = start_kind("start"), cancel_kind("cancel")
    action = Action(api)

    def start(context):
        action.calls.append("start")
        raise start_error

    def cancel(deadline):
        action.calls.append("cancel")
        raise cancel_error

    action.start, action.cancel = start, cancel
    owner = launch(api, plan(api, action))
    result = owner.shutdown()
    winner = (start_error if start_kind is KeyboardInterrupt else cancel_error if cancel_kind is KeyboardInterrupt
              else start_error if start_kind is RuntimeError else cancel_error if cancel_kind is RuntimeError else start_error)
    assert result.complete and authority(result) is winner
    assert all(f.phase is api.FailurePhase.ACTION for f in result.failures)
    assert start_error in errors(result) and cancel_error in errors(result)
    assert owner.shutdown() == result and not hasattr(result, "actions")
    assert action.calls == ["prepare", "start", "cancel"]
    assert not workers()


def test_prepare_interruption_and_independent_cleanup_defect(api):
    first = Action(api)
    interrupt = KeyboardInterrupt("readiness")
    second = Action(api, prepare_error=interrupt)
    defect = RuntimeError("idle cleanup")
    first.cancel = mock.Mock(side_effect=defect)
    factory = mock.Mock()
    policy = api.StopPolicy((api.StopStage(.01, first), api.StopStage(.01, second)), .02)
    prepared = []
    original = api._make_continuation

    def continuation(callback):
        prepared.append(callback.__self__)
        return original(callback)

    with mock.patch.object(api, "_make_continuation", continuation):
        with pytest.raises(KeyboardInterrupt) as caught:
            api.OwnedProcess.launch(["child"], grace_seconds=.08, stop_policy=policy, factory=factory)
    assert caught.value is interrupt and not hasattr(interrupt, "process_result")
    status = prepared[0].cleanup_status
    assert authority(status) is interrupt and defect in errors(status)
    assert secondary(interrupt) == []
    first.cancel.assert_called_once()
    assert second.calls.count("cancel") == 1
    factory.assert_not_called()
    assert not workers()


def test_failed_readiness_retains_pending_prepared_resources(api):
    error = OSError("readiness")
    action = Action(api, pending=True, prepare_error=error)
    factory = mock.Mock()
    try:
        with pytest.raises(OSError) as caught:
            api.OwnedProcess.launch(["child"], grace_seconds=.08, stop_policy=plan(api, action), factory=factory)
        assert caught.value is error
        owner = caught.value.cleanup
        assert not owner.wait_cleanup(.01).complete
        assert "start" not in action.calls
        factory.assert_not_called()
    finally:
        action.release.set()
    assert owner.wait_cleanup(1).complete
    assert action.calls.count("cancel") == 1
    assert not workers()


def test_invalid_readiness_acknowledgement_is_not_success(api):
    action = Action(api)
    action.prepare = mock.Mock(return_value=False)
    factory = mock.Mock()
    with pytest.raises(TypeError, match="prepare"):
        api.OwnedProcess.launch(["child"], grace_seconds=.08, stop_policy=plan(api, action), factory=factory)
    factory.assert_not_called()
    assert action.calls == ["cancel"]
    assert not workers()


def test_live_reader_and_pending_action_keep_exact_owners(api):
    action = Action(api, pending=True)
    blocked, release = threading.Event(), threading.Event()
    child = Child()
    child.stdout.fileno = lambda: -1  # Unknown descriptor, never the descriptor-less seam.

    def configure(self, index):
        pass

    def read(self, index):
        if index == 0:
            blocked.set()
            release.wait()

    with mock.patch.object(api.OwnedProcess, "_configure_reader", configure), mock.patch.object(api.OwnedProcess, "_read", read):
        owner = launch(api, plan(api, action, drain=True, wait=True), child)
        owners = tuple(owner._owners)
        try:
            assert blocked.wait(1)
            result = owner.shutdown()
            assert not result.complete
            assert child.stdout.attempts == 0 and child.stderr.attempts == 1
            assert tuple(owner._owners) == owners
            action.release.set()
            assert not owner.wait_cleanup(.01).complete
        finally:
            release.set()
            action.release.set()
            assert owner.wait_cleanup(1).complete
    assert child.stdout.attempts == child.stderr.attempts == 1
    assert action.calls == ["prepare", "start", "cancel"]
    assert not workers()


def test_action_done_and_resource_completion_are_both_required(api):
    action = Action(api)
    settled = threading.Event()
    action.observe = lambda: api.ActionResult(settled.is_set(), True)
    owner = launch(api, plan(api, action))
    try:
        initial = owner.shutdown()
        assert not initial.complete
        # Progress is coordinator-private, not nested in the public result.
        progress = owner._action_results[0]
        assert progress.cleanup_complete and not progress.done
        assert not hasattr(initial, "actions")
        assert not owner.wait_cleanup(.01).complete
    finally:
        settled.set()
        assert owner.wait_cleanup(1).complete
    assert not progress.done and not initial.complete
    assert action.calls.count("start") == action.calls.count("cancel") == 1
    assert not workers()


def test_late_action_interruption_preserves_delivered_error_and_diagnostics(api):
    ordinary = OSError("control failure")
    action = Action(api, pending=True, error=ordinary)
    owner = launch(api, plan(api, action))
    try:
        initial = owner.shutdown()
        assert authority(initial) is ordinary and not initial.complete
        before = tuple(secondary(ordinary))
        interruption = KeyboardInterrupt("late interruption")
        action.error = interruption
        action.release.set()
        observed = owner.wait_cleanup(1)
        assert observed.complete and authority(observed) is interruption
        assert ordinary in errors(observed)
        assert authority(initial) is ordinary and tuple(secondary(ordinary)) == before
        assert initial.failures[0].phase is api.FailurePhase.ACTION
        assert not hasattr(initial, "actions")
        assert action.calls == ["prepare", "start", "cancel"]
    finally:
        action.release.set()
        owner.wait_cleanup(1)
    assert not workers()


def test_real_descendant_pipe_deadline_race_and_unrelated_descriptor(api):
    ops, label = PosixDescriptorOps(), "unrelated descriptor"
    fd = ops.openat(None, os.devnull, os.O_RDONLY)
    unrelated = OwnedDescriptor(ops, fd, label=label)
    action = Action(api)
    owner = None
    callers = []
    try:
        owner = api.OwnedProcess.launch([sys.executable, "-c",
            "import subprocess,sys,time; "
            "subprocess.Popen([sys.executable,'-c','import time;time.sleep(.4)']);"
            "print('ready',flush=True);time.sleep(10)"],
            grace_seconds=.15, deadline_seconds=.03,
            stop_policy=plan(api, action, cap=.04, reserve=.05, wait=True, drain=True))
        assert action.started.wait(1)
        delivered = []
        callers = [threading.Thread(target=lambda: delivered.append(owner.shutdown())) for _ in range(4)]
        begin = time.monotonic()
        for caller in callers:
            caller.start()
        for caller in callers:
            caller.join(1)
        assert len(delivered) == 4 and all(r.complete for r in delivered)
        assert time.monotonic() - begin < .3
        assert action.calls.count("start") == action.calls.count("cancel") == 1
        assert all(owner._safe(i) for i in range(2))
        assert os.read(unrelated.fd, 1) == b""
        assert not workers()
    finally:
        for caller in callers:
            caller.join(1)
        if owner:
            owner.shutdown()
            owner.wait_cleanup(1)
        unrelated.close()
        unrelated.close()
