"""Domain-neutral stop-policy contracts (6BB.14–6BB.17)."""
from __future__ import annotations

import importlib
import subprocess
import sys
import threading
import time
from unittest import mock

import pytest

from tests.test_process_lifecycle import Child, workers, secondary


@pytest.fixture
def api():
    return importlib.import_module("docker.process")


class Action:
    """Nonblocking action with distinct request and owned-resource settlement."""
    def __init__(self, api, *, error=None, pending=False, prepare_error=None):
        self.api = api
        self.error = error
        self.pending = pending
        self.prepare_error = prepare_error
        self.calls = []
        self.contexts = []
        self.started = threading.Event()
        self.release = threading.Event()

    def prepare(self):
        self.calls.append("prepare")
        if self.prepare_error:
            raise self.prepare_error

    def start(self, context):
        self.calls.append("start")
        self.contexts.append(context)
        self.started.set()

    def observe(self):
        settled = not self.pending or self.release.is_set()
        return self.api.ActionResult(settled, settled, self.error)

    def cancel(self, deadline):
        self.calls.append("cancel")


def plan(api, action=None, *, cap=.015, reserve=.025, drain=False, wait=False, normal=False, local=False):
    return api.StopPolicy((api.StopStage(cap, action, wait, normal, local),), reserve, drain)


def launch(api, policy, child=None, **kwargs):
    child = child or Child()
    return api.OwnedProcess.launch(["child"], grace_seconds=.08,
        stop_policy=policy, factory=lambda *a, **k: child, **kwargs)


@pytest.mark.parametrize("bad", [0, -1, float("inf"), float("nan")])
@pytest.mark.parametrize("field", ["cap", "reserve"])
def test_invalid_budgets_precede_factory(api, bad, field):
    factory = mock.Mock()
    options = {field: bad}
    with pytest.raises(ValueError):
        api.OwnedProcess.launch(["child"], grace_seconds=.08,
            stop_policy=plan(api, **options), factory=factory)
    factory.assert_not_called()
    assert not workers()


@pytest.mark.parametrize("policy", [object(), "policy", 1])
def test_invalid_policy_never_acquires(api, policy):
    factory = mock.Mock()
    with pytest.raises(TypeError):
        api.OwnedProcess.launch(["child"], stop_policy=policy, factory=factory)
    factory.assert_not_called()


def test_over_total_and_invalid_action_precede_preparation(api):
    action = Action(api)
    for policy in (plan(api, action, cap=.07, reserve=.02), plan(api, object())):
        factory = mock.Mock()
        with pytest.raises((ValueError, TypeError)):
            api.OwnedProcess.launch(["child"], grace_seconds=.08, stop_policy=policy, factory=factory)
        factory.assert_not_called()
    assert action.calls == []


@pytest.mark.parametrize("position", [0, 1])
def test_prepare_failure_cleans_each_prepared_slot_and_idle_worker(api, position):
    failure = OSError("readiness")
    actions = [Action(api), Action(api)]
    actions[position].prepare_error = failure
    policy = api.StopPolicy(tuple(api.StopStage(.01, a) for a in actions), .02)
    factory = mock.Mock()
    with pytest.raises(OSError) as caught:
        api.OwnedProcess.launch(["child"], grace_seconds=.08, stop_policy=policy, factory=factory)
    assert caught.value is failure
    factory.assert_not_called()
    assert all(a.calls.count("cancel") == 1 for a in actions[:position + 1])
    assert all("start" not in a.calls for a in actions)
    assert not workers()


def test_factory_failure_cleans_policy_preparation(api):
    action = Action(api)
    error = OSError("factory")
    with pytest.raises(OSError) as caught:
        api.OwnedProcess.launch(["child"], grace_seconds=.08,
            stop_policy=plan(api, action), factory=mock.Mock(side_effect=error))
    assert caught.value is error
    assert action.calls == ["prepare", "cancel"]
    assert not workers()


@pytest.mark.parametrize("trigger", ["explicit", "deadline", "context", "setup", "reader"])
def test_all_triggers_use_prepared_policy_without_main_authority(api, trigger):
    action = Action(api)
    child = Child()
    options = {}
    error = OSError("trigger")
    if trigger == "deadline":
        options["deadline_seconds"] = .01
    if trigger == "setup":
        options["on_launched"] = mock.Mock(side_effect=error)
    patches = mock.patch.object(api.OwnedProcess, "_read_chunk", side_effect=error) if trigger == "reader" else mock.patch.object(time, "perf_counter", wraps=time.perf_counter)
    with patches:
        if trigger == "setup":
            with pytest.raises(OSError) as caught:
                launch(api, plan(api, action), child, **options)
            assert caught.value is error
        else:
            owner = launch(api, plan(api, action), child, **options)
            if trigger in ("deadline", "reader"):
                assert action.started.wait(1)
            if trigger == "context":
                owner.__exit__(None, None, None)
            else:
                owner.shutdown()
            assert owner.wait_cleanup(1).complete
    assert action.calls.count("prepare") == action.calls.count("start") == action.calls.count("cancel") == 1
    context = action.contexts[0]
    assert context.reason == trigger or (trigger == "explicit" and context.reason == "cancel")
    assert set(context.__dataclass_fields__) == {"reason", "returncode", "deadline"}
    assert not hasattr(context, "process") and not hasattr(context, "stdout")
    assert child.stdout.attempts == child.stderr.attempts == 1
    assert not workers()


def test_natural_completion_skips_stop_but_runs_required_finalization(api):
    actions = [Action(api), Action(api)]
    policy = api.StopPolicy((api.StopStage(.01, actions[0]),
                            api.StopStage(.01, actions[1], run_on_normal=True)), .02)
    child = Child()
    child.returncode = 0
    result = launch(api, policy, child).capture()
    assert result.cleanup.complete
    assert "start" not in actions[0].calls
    assert actions[1].contexts[0].reason == "natural"
    assert all(a.calls.count("cancel") == 1 for a in actions)


class Clock:
    def __init__(self):
        self.now = 100.
    def monotonic(self):
        return self.now
    def sleep(self, seconds):
        self.now += seconds


def test_stage_clock_starts_before_initiation_and_wait_shares_cap(api):
    clock = Clock()
    actions = [Action(api), Action(api), Action(api)]
    child = Child()
    policy = api.StopPolicy(tuple(api.StopStage(.01, a, wait_child=True) for a in actions), .02)
    owner = launch(api, policy, child)
    for action in actions:
        original = action.start
        def start(context, original=original):
            assert context.deadline == pytest.approx(clock.now + .01)
            original(context)
            clock.now += .006
        action.start = start
    with mock.patch.object(api.time, "monotonic", clock.monotonic), mock.patch.object(api, "_policy_pause", clock.sleep):
        owner.shutdown()
        assert owner.wait_cleanup(1).complete
    assert [a.contexts[0].deadline for a in actions] == pytest.approx([100.01, 100.02, 100.03])
    assert child.returncode is not None
    assert not workers()


def test_failure_advances_immediately_and_local_order_is_coordinator_owned(api):
    child = Child()
    first = Action(api, error=OSError("request"))
    second = Action(api)
    def start(context):
        assert child.actions == ["terminate"]
        second.contexts.append(context)
        second.calls.append("start")
    second.start = start
    policy = api.StopPolicy((api.StopStage(.03, first, wait_child=True),
                            api.StopStage(.01, second, local_stop_before=True)), .02)
    begin = time.monotonic()
    owner = launch(api, policy, child)
    result = owner.shutdown()
    assert time.monotonic() - begin < .08
    assert result.error is first.error and result.complete
    assert second.calls.count("start") == 1


@pytest.mark.parametrize("primary_type", [None, ValueError, KeyboardInterrupt])
@pytest.mark.parametrize("action_type", [OSError, RuntimeError, KeyboardInterrupt])
@pytest.mark.parametrize("position", [0, 1])
def test_action_fault_precedence_independent_cleanup_and_repeat(api, primary_type, action_type, position):
    primary = primary_type("operation") if primary_type else None
    error = action_type("action")
    actions = [Action(api), Action(api)]
    actions[position].error = error
    child = Child()
    policy = api.StopPolicy(tuple(api.StopStage(.01, a) for a in actions), .025)
    clock = Clock()
    # This is a precedence/once-only proof, not a wall-clock scheduling test.
    # Pin its budget so loaded workers cannot skip an otherwise eligible stage.
    with mock.patch.object(api.time, "monotonic", clock.monotonic):
        owner = launch(api, policy, child)
        result = owner.shutdown(primary=primary)
    expected = primary if primary and (primary_type is KeyboardInterrupt or action_type is OSError) else error
    assert result.error is expected and result.complete
    assert child.stdout.attempts == child.stderr.attempts == 1
    assert all(a.calls.count("start") == a.calls.count("cancel") == 1 for a in actions)
    assert owner.shutdown() is result
    assert len(result.failures) == len({id(e) for e in result.failures})


def test_pending_action_retains_owner_and_late_error_is_observation_only(api):
    action = Action(api, pending=True)
    owner = launch(api, plan(api, action))
    try:
        result = owner.shutdown()
        assert result.shutdown_incomplete
        assert not owner.wait_cleanup(.01).complete
        action.error = RuntimeError("late")
        action.release.set()
        observed = owner.wait_cleanup(1)
        assert observed.complete and observed.error is action.error
        assert result.error is None and result.shutdown_incomplete
        assert action.calls == ["prepare", "start", "cancel"]
    finally:
        action.release.set()
        owner.wait_cleanup(1)
    assert not workers()


def test_primary_merge_in_policy_publication_gap(api):
    action = Action(api)
    owner = launch(api, plan(api, action))
    published, resume = threading.Event(), threading.Event()
    original = owner._publish
    first, later = KeyboardInterrupt("first"), KeyboardInterrupt("later")
    results = []
    def publish(complete):
        original(complete)
        published.set()
        assert resume.wait(1)
    owner._publish = publish
    caller = threading.Thread(target=lambda: results.append(owner.shutdown(primary=first)))
    caller.start()
    try:
        assert published.wait(1)
        merger = threading.Thread(target=lambda: results.append(owner.shutdown(primary=later)))
        merger.start()
        time.sleep(.01)
        resume.set()
        caller.join(1)
        merger.join(1)
        assert len(results) == 2
        assert all(r.error is first for r in results)
        assert later in secondary(first)
        assert action.calls.count("start") == 1
    finally:
        resume.set()
        caller.join(1)
        owner.wait_cleanup(1)


class Helper(Action):
    """A separately owned, default-policy auxiliary process."""
    def __init__(self, api, code):
        super().__init__(api)
        self.code = code
        self.owner = None
    def start(self, context):
        super().start(context)
        remaining = context.deadline - time.monotonic()
        self.owner = self.api.OwnedProcess.launch([sys.executable, "-c", self.code],
            grace_seconds=max(.001, remaining / 3), deadline_seconds=max(.001, remaining / 3))
    def observe(self):
        if self.owner is None:
            return self.api.ActionResult(True, True)
        result = self.owner.cleanup_status
        return self.api.ActionResult(result.complete, result.complete, result.error)
    def cancel(self, deadline):
        super().cancel(deadline)
        # Nonblocking: the helper's own configured deadline drives cleanup.


@pytest.mark.parametrize("code", ["import time; time.sleep(10)", "raise SystemExit(7)"])
def test_real_pipe_helper_drain_and_fallback(api, code):
    helper = Helper(api, code)
    policy = plan(api, helper, cap=.15, reserve=.15, drain=True, wait=True)
    owner = api.OwnedProcess.launch([sys.executable, "-c",
        "import sys,time; print('ready',flush=True); time.sleep(.04); "
        "sys.stdout.write('x'*100000);sys.stdout.flush();sys.stderr.write('y'*100000);sys.stderr.flush();time.sleep(10)"],
        stop_policy=policy, grace_seconds=.4, capture_limit=128)
    try:
        result = owner.shutdown()
        assert owner.wait_cleanup(1).complete
        assert bytes(owner._tails[0]) == b"x" * 128
        assert bytes(owner._tails[1]) == b"y" * 128
        assert result.complete
        assert helper.owner.wait_cleanup(1).complete
        assert not workers()
    finally:
        owner.shutdown()
        owner.wait_cleanup(1)
        if helper.owner:
            helper.owner.shutdown()
            helper.owner.wait_cleanup(1)


def test_drain_without_consumer_does_not_stall_full_spool(api):
    action = Action(api)
    owner = api.OwnedProcess.launch([sys.executable, "-c",
        "import os,time;os.write(1,b'a'*3000000);os.write(2,b'b'*3000000);time.sleep(10)"],
        stop_policy=plan(api, action, cap=.25, reserve=.1, drain=True, wait=True),
        grace_seconds=.4, capture_limit=64)
    result = owner.shutdown()
    assert result.complete
    assert bytes(owner._tails[0]) == b"a"*64 and bytes(owner._tails[1]) == b"b"*64
    assert owner._output.qsize() <= 32
    assert not workers()


@pytest.mark.parametrize("fault", ["startup", "reader"])
def test_real_pipe_faults_keep_reader_barrier_and_independent_action(api, fault):
    action = Action(api)
    error = OSError("reader setup")
    seam = "_start_reader" if fault == "startup" else "_read_chunk"
    original = getattr(api.OwnedProcess, seam)
    def fail(self, index):
        if index == 1:
            raise error
        return original(self, index)
    with mock.patch.object(api.OwnedProcess, seam, fail):
        if fault == "startup":
            with pytest.raises(OSError) as caught:
                api.OwnedProcess.launch([sys.executable, "-c", "import time;time.sleep(10)"],
                    stop_policy=plan(api, action), grace_seconds=.08)
            assert caught.value is error
        else:
            owner = api.OwnedProcess.launch([sys.executable, "-c", "import time;time.sleep(10)"],
                stop_policy=plan(api, action), grace_seconds=.08)
            assert action.started.wait(1)
            assert owner.wait_cleanup(1).complete
            assert all(owner._safe(i) for i in range(2))
    assert action.calls.count("start") == 1
    assert not workers()
