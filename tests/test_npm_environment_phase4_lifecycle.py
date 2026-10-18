"""Legacy lifecycle characterization through the single generic owner."""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from docker.filesystem.descriptors import OwnedDescriptor
from docker.filesystem.operations import PosixDescriptorOps
from docker.npm_environment.lifecycle import LifecyclePolicy, run_captured
from docker.process import (
    ActionResult,
    FailureKind,
    FailurePhase,
    OwnedProcess,
    StopPolicy,
    StopStage,
    StreamOutcome,
)
from tests.test_process_lifecycle import workers


class _Pipe(io.StringIO):
    def __init__(self, value: str = "") -> None:
        super().__init__(value)
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        super().close()


class _Process:
    """Supported wait-only captured factory, including an unreapable seam."""

    def __init__(self, *, never_exit=False, return_code=0, wait_failure=None):
        self.never_exit, self.return_code = never_exit, return_code
        self.wait_failure, self._wait_failed = wait_failure, False
        self.terminated = self.killed = False
        self.stdout, self.stderr = _Pipe("captured stdout"), _Pipe("captured stderr")

    def wait(self, timeout=None):
        if self.wait_failure is not None and not self._wait_failed:
            self._wait_failed = True
            raise self.wait_failure
        if self.never_exit and timeout is not None:
            raise subprocess.TimeoutExpired(("client",), timeout)
        return self.return_code

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class _RunningProcess(_Process):
    def __init__(self):
        super().__init__()
        self.returncode = None
        self.terminate_calls = 0

    def poll(self):
        return self.returncode

    def terminate(self):
        super().terminate()
        self.terminate_calls += 1
        self.returncode = -15

    def kill(self):
        super().kill()
        self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired(("client",), timeout)
        return self.returncode


@contextlib.contextmanager
def _observed_children():
    # Observe only after escrow/transfer/readers are protected. The observer
    # neither acquires nor releases a pipe and no raw Popen is returned publicly.
    children = []
    original = OwnedProcess._start_supervisor

    def observe(owner):
        children.append(owner._proc)
        return original(owner)

    with mock.patch.object(OwnedProcess, "_start_supervisor", observe):
        yield children


class TestDeadlineSupervisor(unittest.TestCase):
    """The five historical supervisor cases now bind unified coordination."""

    def test_deadline_claims_cleanup_and_publishes_outcome(self):
        proc = _RunningProcess()
        called = threading.Event()
        cleanup_error = OSError("cleanup failed")
        close = proc.stdout.close

        def fail_close():
            called.set()
            close()
            raise cleanup_error

        proc.stdout.close = fail_close
        owner = OwnedProcess.launch(
            ["client"],
            factory=lambda *a, **k: proc,
            deadline_seconds=0.01,
            grace_seconds=0.05,
        )
        result = owner.capture()
        self.assertTrue(called.is_set() and result.timed_out)
        self.assertTrue(result.lifecycle.complete)
        self.assertIs(result.lifecycle.failures[0].error, cleanup_error)
        self.assertIs(result.lifecycle.failures[0].phase, FailurePhase.RELEASE)
        self.assertEqual(proc.stdout.close_calls, 1)
        self.assertEqual(proc.stderr.close_calls, 1)
        self.assertFalse(workers())

    def test_interruption_preserves_poll_failure_before_cleanup_publication(self):
        proc = _RunningProcess()
        error = OSError("poll failed")
        original_poll, original_publish = proc.poll, OwnedProcess._publish
        published, resume, merged = (threading.Event() for _ in range(3))
        raised = False

        def poll():
            nonlocal raised
            if not raised:
                raised = True
                raise error
            return original_poll()

        def publish(owner, complete):
            published.set()
            self.assertTrue(resume.wait(2))
            original_publish(owner, complete)

        proc.poll = poll
        owner = None
        caller = None
        results = []
        primary = KeyboardInterrupt("caller")
        try:
            with mock.patch.object(OwnedProcess, "_publish", publish):
                owner = OwnedProcess.launch(
                    ["client"],
                    factory=lambda *a, **k: proc,
                    deadline_seconds=10,
                    grace_seconds=1,
                )
                self.assertTrue(published.wait(1))
                original_wait = owner._phase_done.wait

                def wait(timeout=None):
                    if threading.current_thread().name == "phase4-merge":
                        merged.set()
                    return original_wait(timeout)

                with mock.patch.object(owner._phase_done, "wait", wait):
                    caller = threading.Thread(
                        target=lambda: results.append(owner.shutdown(primary=primary)),
                        name="phase4-merge",
                    )
                    caller.start()
                    self.assertTrue(merged.wait(1))
                    resume.set()
                    caller.join(2)
            self.assertFalse(caller.is_alive())
            self.assertIs(results[0].failures[0].error, primary)
            self.assertTrue(any(f.error is error for f in results[0].failures))
            self.assertTrue(proc.terminated)
            self.assertEqual(proc.stdout.close_calls, 1)
            self.assertEqual(proc.stderr.close_calls, 1)
        finally:
            resume.set()
            if caller is not None:
                caller.join(2)
            if owner is not None:
                owner.shutdown()
                self.assertTrue(owner.wait_cleanup(1).complete)
        self.assertFalse(workers())

    def test_deadline_shorter_than_poll_interval_is_not_delayed(self):
        proc = _RunningProcess()
        started = time.monotonic()
        owner = OwnedProcess.launch(
            ["client"],
            factory=lambda *a, **k: proc,
            deadline_seconds=0.02,
            grace_seconds=0.05,
        )
        result = owner.capture()
        self.assertTrue(result.timed_out and proc.terminated)
        self.assertLess(time.monotonic() - started, 0.12)
        self.assertTrue(owner.wait_cleanup(1).complete)
        self.assertFalse(workers())

    def test_cancellation_atomically_takes_cleanup_ownership(self):
        proc = _RunningProcess()
        owner = OwnedProcess.launch(
            ["client"],
            factory=lambda *a, **k: proc,
            deadline_seconds=10,
            grace_seconds=0.05,
        )
        primary = KeyboardInterrupt("cancel")
        result = owner.shutdown(primary=primary)
        self.assertIs(result.failures[0].error, primary)
        self.assertTrue(result.complete)
        self.assertFalse(owner._timed_out)
        self.assertEqual(proc.terminate_calls, 1)
        self.assertEqual(owner.shutdown(), result)
        self.assertEqual(proc.terminate_calls, 1)
        self.assertFalse(workers())

    def test_cleanup_hook_failure_is_published_not_lost(self):
        cleanup_error = RuntimeError("cleanup hook crashed")
        calls = []

        class Action:
            def prepare(self):
                pass

            def start(self, context):
                calls.append("start")
                raise cleanup_error

            def observe(self):
                return ActionResult(True, True)

            def cancel(self, deadline):
                calls.append("cancel")

        proc = _RunningProcess()
        policy = StopPolicy((StopStage(0.01, Action()),), 0.02)
        owner = OwnedProcess.launch(
            ["client"],
            factory=lambda *a, **k: proc,
            deadline_seconds=0.01,
            grace_seconds=0.05,
            stop_policy=policy,
        )
        result = owner.capture()
        self.assertTrue(result.timed_out and result.lifecycle.complete)
        self.assertIs(result.lifecycle.failures[0].error, cleanup_error)
        self.assertIs(result.lifecycle.failures[0].kind, FailureKind.UNEXPECTED)
        self.assertIs(result.lifecycle.failures[0].phase, FailurePhase.ACTION)
        self.assertEqual(calls, ["start", "cancel"])
        self.assertEqual(proc.stdout.close_calls, 1)
        self.assertEqual(proc.stderr.close_calls, 1)
        self.assertFalse(workers())


class TestLifecyclePrimitives(unittest.TestCase):
    def test_reap_closes_descriptors_for_unreapable_client(self):
        proc = _Process(never_exit=True)
        owner = None
        try:
            with mock.patch(
                "docker.npm_environment.lifecycle.subprocess.Popen", return_value=proc
            ):
                owner, outcome = run_captured(("client",), policy=LifecyclePolicy(0.01))
            self.assertTrue(proc.killed)
            self.assertEqual(proc.stdout.close_calls, 1)
            self.assertEqual(proc.stderr.close_calls, 1)
            self.assertTrue(
                any(
                    isinstance(error, subprocess.TimeoutExpired)
                    for error in outcome.errors
                )
            )
            self.assertFalse(owner.cleanup_status.complete)
        finally:
            proc.never_exit = False
            if owner is not None:
                self.assertTrue(owner.wait_cleanup(1).complete)

    def test_captured_runner_reuses_bounded_lifecycle_and_closes_pipes(self):
        proc = _Process(never_exit=True)
        owner = None
        try:
            with mock.patch(
                "docker.npm_environment.lifecycle.subprocess.Popen", return_value=proc
            ):
                owner, outcome = run_captured(
                    ("docker", "rm", "-f", "named-container"),
                    policy=LifecyclePolicy(0.01, process_label="docker rm -f client"),
                )
            self.assertIsNone(outcome.return_code)
            self.assertTrue(proc.terminated and proc.killed)
            self.assertEqual(proc.stdout.close_calls, 1)
            self.assertEqual(proc.stderr.close_calls, 1)
            self.assertEqual(outcome.stdout, "captured stdout")
            self.assertEqual(outcome.stderr, "captured stderr")
            self.assertIn(
                "docker rm -f client did not exit after SIGKILL", str(outcome.errors[0])
            )
        finally:
            proc.never_exit = False
            if owner is not None:
                self.assertTrue(owner.wait_cleanup(1).complete)

    def test_captured_runner_drains_large_dual_stream_output_without_timeout(self):
        program = "import os; data=b'x'*262144; os.write(1,data); os.write(2,data)"
        with _observed_children() as children:
            owner, outcome = run_captured(
                (sys.executable, "-c", program),
                policy=LifecyclePolicy(2, captured_output_bytes=1024),
            )
        self.assertIsInstance(owner, OwnedProcess)
        self.assertEqual(outcome.return_code, 0)
        self.assertEqual(outcome.errors, [])
        self.assertLessEqual(len(outcome.stdout.encode()), 1024)
        self.assertLessEqual(len(outcome.stderr.encode()), 1024)
        self.assertTrue(owner.cleanup_status.complete)
        self.assertEqual(len(children), 1)
        self.assertTrue(children[0].stdout.closed and children[0].stderr.closed)
        self.assertFalse(workers())

    def test_parent_exit_with_descendant_holding_pipes_is_bounded(self):
        # Report the descendant PID through captured output, not another
        # unmanaged file descriptor. Its finite life also bounds failed tests.
        program = (
            "import subprocess,sys; "
            "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(1)']); "
            "print(child.pid,flush=True)"
        )
        drain_started = []
        with _observed_children() as children:
            start_supervisor = OwnedProcess._start_supervisor

            def after_parent_exit(owner):
                # Establish the test's exited-parent/live-pipes precondition
                # through the sole owner's observation, before it activates
                # the short operation deadline. Interpreter startup is not
                # the pipe-drain behavior under test. Five seconds is only a
                # fixture-startup watchdog, not a changed shutdown allowance.
                startup_end = time.monotonic() + 5
                while owner._poll_child() is None:
                    self.assertLess(time.monotonic(), startup_end)
                    time.sleep(0.001)
                self.assertEqual(owner.returncode, 0)
                self.assertEqual(
                    owner._outcomes, [StreamOutcome.PENDING, StreamOutcome.PENDING]
                )
                drain_started.append(time.monotonic())
                start_supervisor(owner)

            with mock.patch.object(
                OwnedProcess, "_start_supervisor", after_parent_exit
            ):
                owner, outcome = run_captured(
                    (sys.executable, "-c", program),
                    policy=LifecyclePolicy(0.1, captured_output_bytes=128),
                )
        elapsed = time.monotonic() - drain_started[0]
        try:
            if outcome.stdout.strip():
                os.kill(int(outcome.stdout.strip()), 15)
        except ProcessLookupError:
            pass
        self.assertIsInstance(owner, OwnedProcess)
        self.assertEqual(outcome.return_code, 0)
        self.assertLess(elapsed, 0.5)
        self.assertTrue(owner.cleanup_status.complete)
        self.assertTrue(children[0].stdout.closed and children[0].stderr.closed)
        self.assertLessEqual(len(outcome.stdout.encode()), 128)
        self.assertLessEqual(len(outcome.stderr.encode()), 128)
        self.assertFalse(workers())

    def test_small_grace_repeated_runs_do_not_affect_unrelated_descriptors(self):
        ops, read_label, write_label = (
            PosixDescriptorOps(),
            "unrelated read",
            "unrelated write",
        )
        read_fd, write_fd = os.pipe()
        with OwnedDescriptor(ops, read_fd, label=read_label) as read_owner:
            with OwnedDescriptor(ops, write_fd, label=write_label) as write_owner:
                with _observed_children() as children:
                    for _ in range(20):
                        owner, outcome = run_captured(
                            (sys.executable, "-c", "print('ok')"),
                            policy=LifecyclePolicy(0.1, captured_output_bytes=16),
                        )
                        self.assertIsInstance(owner, OwnedProcess)
                        self.assertEqual(outcome.return_code, 0)
                        self.assertTrue(owner.cleanup_status.complete)
                        self.assertTrue(
                            children[-1].stdout.closed and children[-1].stderr.closed
                        )
                        os.write(write_owner.fd, b"x")
                        self.assertEqual(os.read(read_owner.fd, 1), b"x")
                        self.assertFalse(workers())

    def test_wrappers_close_once_after_descriptor_readers_stop(self):
        proc = _Process()
        with mock.patch(
            "docker.npm_environment.lifecycle.subprocess.Popen", return_value=proc
        ):
            owner, outcome = run_captured(("client",), policy=LifecyclePolicy(0.001))
        self.assertEqual(outcome.return_code, 0)
        self.assertEqual(proc.stdout.close_calls, 1)
        self.assertEqual(proc.stderr.close_calls, 1)
        self.assertTrue(owner.cleanup_status.complete)
        self.assertFalse(workers())

    def test_unexpected_wait_failure_is_primary_and_still_reaps(self):
        wait_error = RuntimeError("unexpected wait failure")
        proc = _Process(wait_failure=wait_error)
        with mock.patch(
            "docker.npm_environment.lifecycle.subprocess.Popen", return_value=proc
        ):
            owner, outcome = run_captured(("client",), policy=LifecyclePolicy(0.01))
        self.assertIs(outcome.errors[0], wait_error)
        # A later bounded wait proves this wait-only fixture already exited;
        # the coordinator need not signal an observably exited child.
        self.assertTrue(proc._wait_failed)
        self.assertEqual(outcome.return_code, 0)
        self.assertTrue(owner.cleanup_status.complete)
        self.assertIs(owner.cleanup_status.failures[0].phase, FailurePhase.WAIT)
        self.assertIs(owner.cleanup_status.failures[0].kind, FailureKind.UNEXPECTED)
        self.assertTrue(proc.stdout.closed and proc.stderr.closed)
        self.assertFalse(workers())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
