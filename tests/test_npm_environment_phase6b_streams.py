"""Phase 6B — subprocess stream ownership and cleanup contracts.

Tasks 6B.1, 6B.3, 6B.4, and 6B.5 characterize and then bind the stdout/stderr
file objects returned by ``Popen(..., PIPE)`` to one filesystem ``OwnedStream``
per stream for the whole process lifecycle.  The tests cover consumer
signatures, descriptor-less injected streams, close-fault precedence, the
exactly-once terminal release replacing the former repeat-close baseline, and
real-pipe bounded shutdown safety.
"""
from __future__ import annotations

import inspect
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR))

import docker.npm_environment.execution as execution_module
import docker.npm_environment.lifecycle as lifecycle_module
from docker.filesystem.streams import OwnedStream
from docker.npm_environment import (
    AssemblyTimeoutError,
    DockerRunExecutor,
    StreamReaderFailure,
)
from docker.npm_environment.lifecycle import (
    LifecyclePolicy,
    close_descriptors,
    reap_process,
    run_captured,
    terminate_and_reap,
)
from descriptor_ownership_support import recording_streams


class _Phase6bPipe:
    """Pipe double that serves buffered data then blocks until released."""

    def __init__(
        self,
        data: bytes = b"",
        *,
        fail_read: bool = False,
        eof: bool = False,
        blocked_event: threading.Event | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        self._data = data
        self._fail_read = fail_read
        self._eof = eof
        self._blocked_event = blocked_event
        self._close_error = close_error
        self._terminated = threading.Event()
        self.close_attempts = 0
        self.closed = False

    def read(self, n: int) -> bytes:
        if self._fail_read:
            raise OSError("injected reader failure")
        if self._data:
            chunk, self._data = self._data[:n], self._data[n:]
            return chunk
        if self._eof:
            return b""
        if self._blocked_event is not None:
            self._blocked_event.set()
        self._terminated.wait()
        return b""

    def close(self) -> None:
        self.close_attempts += 1
        self.closed = True
        self._terminated.set()
        if self._close_error is not None:
            raise self._close_error

    def unblock(self) -> None:
        self._terminated.set()


class _Phase6bProc:
    """``subprocess.Popen`` double whose pipes block until termination."""

    def __init__(
        self,
        stdout_data: bytes = b"",
        stderr_data: bytes = b"",
        *,
        stdout_fail_read: bool = False,        resist_terminate: bool = False,
        unblock_pipes_on_terminate: bool = True,
        never_exit: bool = False,
        pre_exited: bool = False,
        pipes_eof: bool = False,
        stdout_close_error: BaseException | None = None,
        stderr_close_error: BaseException | None = None,
    ) -> None:
        self.stdout = _Phase6bPipe(
            stdout_data,
            fail_read=stdout_fail_read,
            eof=pipes_eof,
            close_error=stdout_close_error,
        )
        self.stderr = _Phase6bPipe(
            stderr_data, eof=pipes_eof, close_error=stderr_close_error
        )
        self.returncode: int | None = 0 if pre_exited else None
        self.reaped = False
        self.terminated = False
        self.killed = False
        self.wait_calls = 0
        self._resist_terminate = resist_terminate
        self._unblock_pipes_on_terminate = unblock_pipes_on_terminate
        self._never_exit = never_exit
        self._exited = threading.Event()
        if pre_exited:
            self._exited.set()
            self.stdout.unblock()
            self.stderr.unblock()

    def terminate(self) -> None:
        self.terminated = True
        if self._unblock_pipes_on_terminate:
            self.stdout.unblock()
            self.stderr.unblock()
        if not self._resist_terminate:
            self._exited.set()

    def kill(self) -> None:
        self.killed = True
        if self._unblock_pipes_on_terminate:
            self.stdout.unblock()
            self.stderr.unblock()
        self._exited.set()

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        if self._never_exit and timeout is not None:
            raise subprocess.TimeoutExpired(("docker",), timeout)
        if timeout is None:
            self._exited.wait()
        elif not self._exited.wait(timeout):
            raise subprocess.TimeoutExpired(("docker",), timeout)
        self.reaped = True
        if self.returncode is None:
            self.returncode = -15
        return self.returncode


def _assert_no_supported_workers(testcase: unittest.TestCase) -> None:
    live = {thread.name for thread in threading.enumerate()}
    for name in (
        "npm-stdout-reader",
        "npm-stderr-reader",
        "npm-sink-dispatcher",
        "npm-deadline-supervisor",
    ):
        testcase.assertNotIn(name, live)


def _run_streaming(proc, *, deadline=None, grace=1.0):
    result: dict = {}

    def run() -> None:
        try:
            result["capture"] = DockerRunExecutor().run_streaming(
                ("docker", "run", "--rm", "alpine", "true"),
                deadline_seconds=deadline,
                grace_seconds=grace,
            )
        except BaseException as exc:  # pragma: no cover - test aid
            result["error"] = exc

    with mock.patch.object(execution_module.subprocess, "Popen", return_value=proc):
        thread = threading.Thread(target=run, name="test-phase6b-run")
        thread.start()
        thread.join(10.0)
    if thread.is_alive():
        raise AssertionError("run_streaming hung past deadline")
    return result


class ConsumerSignatureCharacterizationTests(unittest.TestCase):
    """Task 6B.1 — pre/post consumer signatures stay stable."""

    def test_run_streaming_signature_is_stable(self) -> None:
        parameters = inspect.signature(
            DockerRunExecutor.run_streaming
        ).parameters
        for name in (
            "self",
            "argv",
            "secrets",
            "sink",
            "deadline_seconds",
            "container_name",
            "grace_seconds",
            "cleanup_outcome",
            "stream_factory",
            "tail_projector",
            "network_url_display",
            "on_launched",
        ):
            self.assertIn(name, parameters)

    def test_lifecycle_primitive_signatures_are_stable(self) -> None:
        self.assertEqual(
            list(inspect.signature(close_descriptors).parameters), ["pipes"]
        )
        for func in (reap_process, terminate_and_reap, run_captured):
            self.assertIn(
                "proc" if func is not run_captured else "argv",
                inspect.signature(func).parameters,
            )


class CloseStreamOwnerInternalsTests(unittest.TestCase):
    """Task 6B.3 — release goes through one shared owner per stream."""

    def test_close_stream_owners_attempts_each_independently(self) -> None:
        self.assertTrue(
            hasattr(execution_module, "_close_stream_owners"),
            "run_streaming must release through a shared stream-owner helper",
        )
        first_pipe = _Phase6bPipe()
        second_pipe = _Phase6bPipe()
        first = OwnedStream(first_pipe, label="stdout")
        second = OwnedStream(second_pipe, label="stderr")
        first.close()
        errors = execution_module._close_stream_owners((first, second))
        self.assertEqual(errors, [])
        self.assertEqual(first_pipe.close_attempts, 1)
        self.assertEqual(second_pipe.close_attempts, 1)

    def test_close_stream_owners_collects_every_failure_once(self) -> None:
        first = OwnedStream(
            _Phase6bPipe(close_error=OSError("stdout close failed")), label="stdout"
        )
        second = OwnedStream(
            _Phase6bPipe(close_error=OSError("stderr close failed")), label="stderr"
        )
        errors = execution_module._close_stream_owners((first, second))
        self.assertEqual(len(errors), 2)
        self.assertTrue(first.released and second.released)

    def test_reader_failure_closes_each_stream_exactly_once(self) -> None:
        proc = _Phase6bProc(
            stdout_fail_read=True,
            resist_terminate=True,
            unblock_pipes_on_terminate=False,
            never_exit=True,
        )
        with recording_streams(execution_module) as recorder:
            result = _run_streaming(proc, deadline=None, grace=0.3)
        exc = result.get("error")
        self.assertIsInstance(exc, StreamReaderFailure)
        self.assertEqual(len(recorder.owners), 2)
        recorder.assert_terminal_and_at_most_once(self)
        # Exactly one underlying close per stream: the former ">= 2"
        # idempotent-repeat baseline is replaced by a terminal claim.
        self.assertEqual(proc.stdout.close_attempts, 1)
        self.assertEqual(proc.stderr.close_attempts, 1)
        self.assertTrue(proc.terminated)
        _assert_no_supported_workers(self)

    def test_one_stream_close_failure_does_not_skip_the_other(self) -> None:
        proc = _Phase6bProc(
            stdout_fail_read=True,
            resist_terminate=False,
            never_exit=True,
            stdout_close_error=OSError("stdout close failed"),
        )
        with recording_streams(execution_module) as recorder:
            result = _run_streaming(proc, deadline=None, grace=0.3)
        exc = result.get("error")
        self.assertIsInstance(exc, StreamReaderFailure)
        recorder.assert_terminal_and_at_most_once(self)
        self.assertEqual(proc.stderr.close_attempts, 1)
        self.assertTrue(proc.stderr.closed)

    def test_repeated_cleanup_hooks_issue_no_second_close(self) -> None:
        proc = _Phase6bProc(
            stdout_fail_read=True,
            resist_terminate=True,
            unblock_pipes_on_terminate=False,
            never_exit=True,
        )
        with recording_streams(execution_module) as recorder:
            result = _run_streaming(proc, deadline=None, grace=0.3)
        self.assertIsInstance(result.get("error"), StreamReaderFailure)
        counts: dict[int, int] = {}
        for _, owner in recorder.close_calls:
            counts[id(owner)] = counts.get(id(owner), 0) + 1
        self.assertTrue(all(count == 1 for count in counts.values()), counts)

    def test_deadline_cleanup_closes_each_stream_once(self) -> None:
        proc = _Phase6bProc(never_exit=True)
        with recording_streams(execution_module) as recorder:
            result = _run_streaming(proc, deadline=0.2, grace=0.2)
        self.assertIsInstance(result.get("error"), AssemblyTimeoutError)
        self.assertEqual(len(recorder.owners), 2)
        recorder.assert_terminal_and_at_most_once(self)
        self.assertEqual(proc.stdout.close_attempts, 1)
        self.assertEqual(proc.stderr.close_attempts, 1)
        _assert_no_supported_workers(self)


class PostLaunchSetupFaultTests(unittest.TestCase):
    """Task 6B.4 — every acquired stream is accounted for after setup faults."""

    @staticmethod
    def _reject_after_release(stream, *, label):
        """Emulate the foundation contract: reject after releasing.

        ``OwnedStream`` acquires a candidate before it validates, so a
        rejected adoption releases the stream through the owner before it
        raises.  The double reproduces that contract for the injected class.
        """
        OwnedStream(stream, label=label).close()

    def test_second_stream_adoption_failure_releases_first_and_reaps(self) -> None:
        proc = _Phase6bProc(pre_exited=True)
        original = OwnedStream
        adopted: list[OwnedStream] = []
        calls = {"n": 0}

        def fake_owner(stream, *, label):
            calls["n"] += 1
            if calls["n"] == 1:
                owner = original(stream, label=label)
                adopted.append(owner)
                return owner
            self._reject_after_release(stream, label=label)
            raise RuntimeError("second stream adoption failed")

        with mock.patch.object(
            execution_module, "OwnedStream", side_effect=fake_owner
        ):
            result = _run_streaming(proc, deadline=None, grace=0.3)
        self.assertIsInstance(result.get("error"), RuntimeError)
        self.assertEqual(len(adopted), 1)
        self.assertTrue(adopted[0].released)
        # Both streams are released exactly once: the adopted stdout owner by
        # the domain cleanup, the rejected stderr by the foundation owner.
        self.assertEqual(proc.stdout.close_attempts, 1)
        self.assertEqual(proc.stderr.close_attempts, 1)
        self.assertTrue(proc.stdout.closed and proc.stderr.closed)
        self.assertTrue(proc.terminated)
        _assert_no_supported_workers(self)

    def test_first_stream_adoption_failure_accounts_for_both_streams(self) -> None:
        proc = _Phase6bProc(pre_exited=True)
        original = OwnedStream
        calls = {"n": 0}

        def fake_owner(stream, *, label):
            calls["n"] += 1
            if calls["n"] == 1:
                self._reject_after_release(stream, label=label)
                raise RuntimeError("first stream adoption failed")
            return original(stream, label=label)

        with mock.patch.object(
            execution_module, "OwnedStream", side_effect=fake_owner
        ):
            result = _run_streaming(proc, deadline=None, grace=0.3)
        self.assertIsInstance(result.get("error"), RuntimeError)
        # stdout is released by the foundation owner that rejected it and
        # stderr is still attempted, adopted, and released by the domain.
        self.assertEqual(proc.stdout.close_attempts, 1)
        self.assertEqual(proc.stderr.close_attempts, 1)
        self.assertTrue(proc.stdout.closed and proc.stderr.closed)
        self.assertTrue(proc.terminated)
        _assert_no_supported_workers(self)

    def test_adoption_failure_with_simultaneous_release_defect(self) -> None:
        proc = _Phase6bProc(
            pre_exited=True, stderr_close_error=RuntimeError("release defect")
        )
        original = OwnedStream
        calls = {"n": 0}

        def fake_owner(stream, *, label):
            calls["n"] += 1
            if calls["n"] == 1:
                # stdout is rejected and released by the foundation owner;
                # stderr is adopted, but its domain release then fails.
                self._reject_after_release(stream, label=label)
                raise RuntimeError("adoption failed")
            return original(stream, label=label)

        with mock.patch.object(
            execution_module, "OwnedStream", side_effect=fake_owner
        ):
            result = _run_streaming(proc, deadline=None, grace=0.3)
        exc = result.get("error")
        # The release defect is authoritative; the displaced adoption failure
        # is retained as secondary context.
        self.assertIsInstance(exc, RuntimeError)
        self.assertIn("release defect", str(exc))
        self.assertTrue(
            any(
                "adoption failed" in str(item)
                for item in getattr(exc, "_transaction_secondary", [])
            )
        )
        self.assertEqual(proc.stdout.close_attempts, 1)
        self.assertEqual(proc.stderr.close_attempts, 1)
        self.assertTrue(proc.terminated)
        _assert_no_supported_workers(self)

    def test_first_reader_construction_failure_releases_owners_and_reaps(self) -> None:
        proc = _Phase6bProc(pre_exited=True)
        with mock.patch.object(
            execution_module,
            "_make_streaming_reader",
            side_effect=RuntimeError("reader construction failed"),
        ):
            result = _run_streaming(proc, deadline=None, grace=0.3)
        self.assertIsInstance(result.get("error"), RuntimeError)
        self.assertEqual(proc.stdout.close_attempts, 1)
        self.assertEqual(proc.stderr.close_attempts, 1)
        self.assertTrue(proc.stdout.closed and proc.stderr.closed)
        self.assertTrue(proc.terminated)
        _assert_no_supported_workers(self)

    def test_second_reader_construction_failure_releases_owners_and_reaps(self) -> None:
        proc = _Phase6bProc(pre_exited=True)
        original = execution_module._make_streaming_reader
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("second reader construction failed")
            return original(*args, **kwargs)

        with mock.patch.object(
            execution_module, "_make_streaming_reader", side_effect=flaky
        ):
            result = _run_streaming(proc, deadline=None, grace=0.3)
        self.assertIsInstance(result.get("error"), RuntimeError)
        self.assertEqual(proc.stdout.close_attempts, 1)
        self.assertEqual(proc.stderr.close_attempts, 1)
        self.assertTrue(proc.stdout.closed and proc.stderr.closed)
        self.assertTrue(proc.terminated)
        _assert_no_supported_workers(self)

    def test_supervisor_start_failure_releases_owners_and_reaps(self) -> None:
        proc = _Phase6bProc(never_exit=False)
        with mock.patch.object(
            execution_module.DeadlineSupervisor,
            "start",
            side_effect=RuntimeError("supervisor start failed"),
        ):
            with recording_streams(execution_module) as recorder:
                result = _run_streaming(proc, deadline=5.0, grace=0.3)
        self.assertIsInstance(result.get("error"), RuntimeError)
        self.assertEqual(len(recorder.owners), 2)
        recorder.assert_terminal_and_at_most_once(self)
        self.assertTrue(proc.terminated)
        _assert_no_supported_workers(self)

    def test_launch_callback_failure_closes_owners_and_reaps(self) -> None:
        proc = _Phase6bProc()
        result: dict = {}
        with recording_streams(execution_module) as recorder:
            with mock.patch.object(
                execution_module.subprocess, "Popen", return_value=proc
            ):
                thread = threading.Thread(
                    target=lambda: _call_streaming(
                        proc, result, on_launched=_raise_launch_failure
                    )
                )
                thread.start()
                thread.join(10.0)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(result.get("error"), RuntimeError)
        self.assertEqual(len(recorder.owners), 2)
        recorder.assert_terminal_and_at_most_once(self)
        self.assertTrue(proc.terminated)
        _assert_no_supported_workers(self)


def _raise_launch_failure() -> None:
    raise RuntimeError("launch callback failed")


def _call_streaming(proc, result: dict, **kwargs) -> None:
    try:
        result["capture"] = DockerRunExecutor().run_streaming(
            ("docker", "run", "--rm", "alpine", "true"),
            deadline_seconds=None,
            grace_seconds=0.3,
            **kwargs,
        )
    except BaseException as exc:  # pragma: no cover - test aid
        result["error"] = exc


class StreamCleanupPrecedenceTests(unittest.TestCase):
    """Task 6B.10/item 4 — a raising release never skips sibling cleanup."""

    def _run_reader_failure(self, proc) -> dict:
        with recording_streams(execution_module) as recorder:
            result = _run_streaming(proc, deadline=None, grace=0.3)
        recorder.assert_terminal_and_at_most_once(self)
        self.assertTrue(proc.stdout.closed and proc.stderr.closed)
        self.assertTrue(proc.terminated)
        _assert_no_supported_workers(self)
        return result

    def test_oserror_close_in_either_stream_releases_sibling(self) -> None:
        for position in ("stdout", "stderr"):
            proc = _Phase6bProc(
                stdout_fail_read=True,
                never_exit=True,
                stdout_close_error=(
                    OSError("stdout close failed")
                    if position == "stdout"
                    else None
                ),
                stderr_close_error=(
                    OSError("stderr close failed")
                    if position == "stderr"
                    else None
                ),
            )
            with self.subTest(position=position):
                result = self._run_reader_failure(proc)
                self.assertIsInstance(result.get("error"), StreamReaderFailure)

    def test_unexpected_close_defect_in_either_stream_releases_sibling(self) -> None:
        for position in ("stdout", "stderr"):
            proc = _Phase6bProc(
                stdout_fail_read=True,
                never_exit=True,
                stdout_close_error=(
                    RuntimeError("close defect") if position == "stdout" else None
                ),
                stderr_close_error=(
                    RuntimeError("close defect") if position == "stderr" else None
                ),
            )
            with self.subTest(position=position):
                self._run_reader_failure(proc)

    def test_reader_failure_with_close_defect_retains_defect_once(self) -> None:
        proc = _Phase6bProc(
            stdout_fail_read=True,
            never_exit=True,
            stderr_close_error=RuntimeError("close defect"),
        )
        with recording_streams(execution_module) as recorder:
            result = _run_streaming(proc, deadline=None, grace=0.3)
        exc = result.get("error")
        self.assertIsInstance(exc, StreamReaderFailure)
        matching = [note for note in getattr(exc, "__notes__", []) if "RuntimeError" in note]
        self.assertEqual(len(matching), 1)
        recorder.assert_terminal_and_at_most_once(self)
        self.assertTrue(proc.stdout.closed and proc.stderr.closed)
        self.assertTrue(proc.terminated)
        _assert_no_supported_workers(self)

    def test_close_interruption_in_either_stream_releases_sibling(self) -> None:
        for position in ("stdout", "stderr"):
            proc = _Phase6bProc(
                stdout_fail_read=True,
                never_exit=True,
                stdout_close_error=(
                    KeyboardInterrupt() if position == "stdout" else None
                ),
                stderr_close_error=(
                    KeyboardInterrupt() if position == "stderr" else None
                ),
            )
            with self.subTest(position=position):
                result = self._run_reader_failure(proc)
                self.assertIsInstance(result.get("error"), KeyboardInterrupt)

    def test_setup_failure_with_cleanup_defect_keeps_defect_authority(self) -> None:
        proc = _Phase6bProc(
            pre_exited=True, stdout_close_error=RuntimeError("cleanup defect")
        )
        setup_failure = RuntimeError("setup failure")
        with mock.patch.object(
            execution_module,
            "_make_streaming_reader",
            side_effect=setup_failure,
        ):
            result = _run_streaming(proc, deadline=None, grace=0.3)
        exc = result.get("error")
        self.assertIsInstance(exc, RuntimeError)
        self.assertIn("cleanup defect", str(exc))
        # The displaced setup failure is retained exactly once as secondary
        # context; no duplicate diagnostic is attached.
        self.assertEqual(
            getattr(exc, "_transaction_secondary", []), [setup_failure]
        )
        self.assertEqual(proc.stdout.close_attempts, 1)
        self.assertEqual(proc.stderr.close_attempts, 1)
        self.assertTrue(proc.terminated)
        _assert_no_supported_workers(self)


class CloseAuthorityPrecedenceTests(unittest.TestCase):
    """Item 3 — an operation interruption keeps authority over close failures."""

    def test_operation_interruption_survives_each_close_failure(self) -> None:
        for position in ("stdout", "stderr"):
            for close_exc in (
                OSError("ordinary close failure"),
                RuntimeError("unexpected close defect"),
                KeyboardInterrupt("close interruption"),
            ):
                with self.subTest(
                    position=position, close=type(close_exc).__name__
                ):
                    sibling = OSError("sibling close failure")
                    failures = (
                        [sibling, close_exc]
                        if position == "stderr"
                        else [close_exc, sibling]
                    )
                    operation = KeyboardInterrupt("operation interrupted")
                    try:
                        execution_module._raise_close_authority(
                            failures, operation
                        )
                    except BaseException as caught:
                        self.assertIs(caught, operation)
                    else:
                        self.fail(
                            "an operation interruption must stay authoritative"
                        )
                    secondary = getattr(
                        operation, "_transaction_secondary", []
                    )
                    self.assertIn(close_exc, secondary)
                    self.assertIn(sibling, secondary)

    def test_close_interruption_outranks_an_ordinary_operation_failure(self) -> None:
        operation = RuntimeError("operation failed")
        interruption = KeyboardInterrupt("close interrupted")
        try:
            execution_module._raise_close_authority([interruption], operation)
        except BaseException as caught:
            self.assertIs(caught, interruption)
        else:  # pragma: no cover - failure path
            self.fail("a close interruption is authoritative")
        self.assertIn(
            operation, getattr(interruption, "_transaction_secondary", [])
        )


class ReapingBeforeCloseAuthorityTests(unittest.TestCase):
    """Item 4 — a close defect never bypasses reaping a live child."""

    def test_collection_setup_failure_reaps_before_close_defect(self) -> None:
        proc = _Phase6bProc(
            stderr_close_error=RuntimeError("close defect")
        )

        def factory(name):
            raise RuntimeError("stream factory failed")

        with mock.patch.object(
            execution_module.subprocess, "Popen", return_value=proc
        ):
            with self.assertRaises(RuntimeError) as ctx:
                DockerRunExecutor().run_streaming(
                    ("docker", "run", "--rm", "alpine", "true"),
                    deadline_seconds=None,
                    grace_seconds=0.3,
                    stream_factory=factory,
                )
        # The close defect takes authority, but only after the live client was
        # terminated and reaped.
        self.assertIn("close defect", str(ctx.exception))
        self.assertTrue(proc.terminated)
        self.assertTrue(proc.reaped)
        _assert_no_supported_workers(self)

    def test_normal_path_reaps_live_child_before_close_defect(self) -> None:
        proc = _Phase6bProc(
            pipes_eof=True, stderr_close_error=RuntimeError("close defect")
        )

        def exit_soon() -> None:
            time.sleep(0.1)
            proc.terminate()

        timer = threading.Thread(target=exit_soon, daemon=True)
        timer.start()
        try:
            with mock.patch.object(
                execution_module.subprocess, "Popen", return_value=proc
            ):
                with self.assertRaises(RuntimeError) as ctx:
                    DockerRunExecutor().run_streaming(
                        ("docker", "run", "--rm", "alpine", "true"),
                        deadline_seconds=None,
                        grace_seconds=0.3,
                    )
        finally:
            timer.join(2.0)
        self.assertIn("close defect", str(ctx.exception))
        self.assertTrue(proc.reaped)
        _assert_no_supported_workers(self)


class RealPipeSafetyTests(unittest.TestCase):
    """Task 6B.5 — real pipes retain bounded, safe shutdown."""

    def _streaming_real(self, program: str, *, deadline=None, grace=1.0):
        real = subprocess.Popen(
            [sys.executable, "-c", program],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        with mock.patch.object(
            execution_module.subprocess, "Popen", return_value=real
        ):
            return DockerRunExecutor().run_streaming(
                ("python", "-c", program),
                deadline_seconds=deadline,
                grace_seconds=grace,
            )

    def test_large_simultaneous_output_is_drained(self) -> None:
        program = (
            "import os; data=b'y'*262144; os.write(1, data); os.write(2, data)"
        )
        result = self._streaming_real(program, grace=3.0)
        self.assertEqual(result.return_code, 0)
        self.assertTrue(result.stdout)
        self.assertTrue(result.stderr)
        _assert_no_supported_workers(self)

    def test_descendant_holding_write_end_completes_within_bounds(self) -> None:
        program = (
            "import subprocess, sys; "
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
            "print('parent done')"
        )
        started = time.monotonic()
        result = self._streaming_real(program, grace=0.2)
        elapsed = time.monotonic() - started
        self.assertEqual(result.return_code, 0)
        self.assertLess(elapsed, 3.0)
        _assert_no_supported_workers(self)

    def test_real_pipe_deadline_terminates_within_bounds(self) -> None:
        program = "import time; time.sleep(30)"
        started = time.monotonic()
        with self.assertRaises(AssemblyTimeoutError):
            self._streaming_real(program, deadline=0.3, grace=0.2)
        self.assertLess(time.monotonic() - started, 3.0)
        _assert_no_supported_workers(self)

    def test_repeated_real_runs_do_not_corrupt_unrelated_fd(self) -> None:
        read_fd, write_fd = os.pipe()
        try:
            for _ in range(5):
                result = self._streaming_real("print('ok')", grace=1.0)
                self.assertEqual(result.return_code, 0)
                os.write(write_fd, b"x")
                self.assertEqual(os.read(read_fd, 1), b"x")
            _assert_no_supported_workers(self)
        finally:
            os.close(read_fd)
            os.close(write_fd)


class _FilenoPipe(_Phase6bPipe):
    """Pipe double that exposes a descriptor number for configuration faults."""

    def fileno(self) -> int:
        return 999


class CapturedSetupFaultTests(unittest.TestCase):
    """Item 3 — captured-client setup failures cannot strand a pipe or child."""

    def _run(self, proc):
        with mock.patch.object(
            lifecycle_module.subprocess, "Popen", return_value=proc
        ):
            return run_captured(("client",), policy=LifecyclePolicy(0.2))

    def _assert_clean(self, proc, outcome) -> None:
        self.assertEqual(proc.stdout.close_attempts, 1)
        self.assertEqual(proc.stderr.close_attempts, 1)
        self.assertTrue(proc.stdout.closed and proc.stderr.closed)
        self.assertTrue(proc.terminated)
        self.assertFalse(
            any(
                thread.name.startswith("captured-")
                for thread in threading.enumerate()
            )
        )

    def test_first_pipe_adoption_failure_stops_and_reaps(self) -> None:
        proc = _Phase6bProc(pre_exited=True)
        calls = {"n": 0}

        def flaky(stream, *, label):
            calls["n"] += 1
            if calls["n"] == 1:
                # Foundation contract: the rejected pipe is released through
                # its owner before the constructor raises.
                OwnedStream(stream, label=label).close()
                raise RuntimeError("first pipe adoption failed")
            return OwnedStream(stream, label=label)

        with mock.patch.object(
            lifecycle_module, "OwnedStream", side_effect=flaky
        ):
            _, outcome = self._run(proc)
        self.assertIsInstance(outcome.errors[0], RuntimeError)
        self._assert_clean(proc, outcome)

    def test_second_pipe_adoption_failure_stops_and_reaps(self) -> None:
        proc = _Phase6bProc(pre_exited=True)
        calls = {"n": 0}

        def flaky(stream, *, label):
            calls["n"] += 1
            if calls["n"] == 2:
                OwnedStream(stream, label=label).close()
                raise RuntimeError("second pipe adoption failed")
            return OwnedStream(stream, label=label)

        with mock.patch.object(
            lifecycle_module, "OwnedStream", side_effect=flaky
        ):
            _, outcome = self._run(proc)
        self.assertIsInstance(outcome.errors[0], RuntimeError)
        self._assert_clean(proc, outcome)

    def test_descriptor_configuration_failure_stops_and_reaps(self) -> None:
        proc = _Phase6bProc(pre_exited=True)
        proc.stdout = _FilenoPipe()
        proc.stderr = _FilenoPipe()
        with mock.patch.object(
            lifecycle_module.os,
            "set_blocking",
            side_effect=RuntimeError("descriptor configuration failed"),
        ):
            _, outcome = self._run(proc)
        self.assertIsInstance(outcome.errors[0], RuntimeError)
        self._assert_clean(proc, outcome)

    def test_first_reader_start_failure_stops_and_reaps(self) -> None:
        proc = _Phase6bProc(pre_exited=True)

        def flaky_start(thread):
            raise RuntimeError("reader start failed")

        with mock.patch.object(threading.Thread, "start", flaky_start):
            _, outcome = self._run(proc)
        self.assertIsInstance(outcome.errors[0], RuntimeError)
        self._assert_clean(proc, outcome)

    def test_second_reader_start_failure_stops_first_reader_and_reaps(self) -> None:
        proc = _Phase6bProc(pre_exited=True)
        real_start = threading.Thread.start
        calls = {"n": 0}

        def flaky_start(thread):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("reader start failed")
            return real_start(thread)

        with mock.patch.object(threading.Thread, "start", flaky_start):
            _, outcome = self._run(proc)
        self.assertIsInstance(outcome.errors[0], RuntimeError)
        self._assert_clean(proc, outcome)

    def test_real_pipe_partial_reader_startup_is_stopped_and_reaped(self) -> None:
        real_proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        real_start = threading.Thread.start
        calls = {"n": 0}

        def flaky_start(thread):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("reader start failed")
            return real_start(thread)

        with mock.patch.object(threading.Thread, "start", flaky_start):
            with mock.patch.object(
                lifecycle_module.subprocess, "Popen", return_value=real_proc
            ):
                _, outcome = run_captured(
                    ("client",), policy=LifecyclePolicy(0.3)
                )
        self.assertIsInstance(outcome.errors[0], RuntimeError)
        self.assertTrue(real_proc.stdout.closed and real_proc.stderr.closed)
        self.assertIsNotNone(real_proc.returncode)
        self.assertFalse(
            any(
                thread.name.startswith("captured-")
                for thread in threading.enumerate()
            )
        )


    def test_setup_failure_with_ordinary_close_failure_keeps_setup_primary(
        self,
    ) -> None:
        proc = _Phase6bProc(
            pre_exited=True,
            stderr_close_error=OSError("ordinary close failure"),
        )

        def flaky_start(thread):
            raise RuntimeError("reader start failed")

        with mock.patch.object(threading.Thread, "start", flaky_start):
            _, outcome = self._run(proc)
        self.assertIsInstance(outcome.errors[0], RuntimeError)
        self.assertEqual(str(outcome.errors[0]), "reader start failed")
        self.assertTrue(
            any(
                isinstance(exc, OSError)
                and "ordinary close failure" in str(exc)
                for exc in outcome.errors[1:]
            )
        )
        self._assert_clean(proc, outcome)

    def test_setup_failure_with_unexpected_close_defect_takes_authority(
        self,
    ) -> None:
        proc = _Phase6bProc(
            pre_exited=True, stderr_close_error=RuntimeError("close defect")
        )

        def flaky_start(thread):
            raise RuntimeError("reader start failed")

        with mock.patch.object(threading.Thread, "start", flaky_start):
            _, outcome = self._run(proc)
        self.assertIn("close defect", str(outcome.errors[0]))
        self.assertTrue(
            any(
                "reader start failed" in str(exc)
                for exc in outcome.errors[1:]
            )
        )
        self._assert_clean(proc, outcome)

    def test_setup_failure_with_close_interruption_takes_authority(self) -> None:
        interruption = KeyboardInterrupt("close interrupted")
        proc = _Phase6bProc(pre_exited=True, stderr_close_error=interruption)

        def flaky_start(thread):
            raise RuntimeError("reader start failed")

        with mock.patch.object(threading.Thread, "start", flaky_start):
            _, outcome = self._run(proc)
        self.assertIs(outcome.errors[0], interruption)
        self.assertTrue(
            any(
                "reader start failed" in str(exc)
                for exc in outcome.errors[1:]
            )
        )
        self._assert_clean(proc, outcome)

    def test_partial_reader_startup_with_close_defect_stops_first_reader(
        self,
    ) -> None:
        proc = _Phase6bProc(
            pre_exited=True, stderr_close_error=RuntimeError("close defect")
        )
        real_start = threading.Thread.start
        calls = {"n": 0}

        def flaky_start(thread):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("reader start failed")
            return real_start(thread)

        with mock.patch.object(threading.Thread, "start", flaky_start):
            _, outcome = self._run(proc)
        self.assertIn("close defect", str(outcome.errors[0]))
        self._assert_clean(proc, outcome)


class LifecyclePrimitiveOwnerTests(unittest.TestCase):
    """Task 6B.8 — lifecycle cleanup delegates to shared stream owners."""

    def test_close_descriptors_uses_owned_stream_release(self) -> None:
        pipe = _Phase6bPipe()
        owner = OwnedStream(pipe, label="stdout")
        errors = close_descriptors((owner,))
        self.assertEqual(errors, [])
        self.assertTrue(owner.released)
        self.assertEqual(pipe.close_attempts, 1)
        self.assertEqual(close_descriptors((owner,)), [])

    def test_close_descriptors_attempts_independent_streams_once(self) -> None:
        first_pipe = _Phase6bPipe(close_error=OSError("one"))
        second_pipe = _Phase6bPipe()
        first = OwnedStream(first_pipe, label="stdout")
        second = OwnedStream(second_pipe, label="stderr")
        errors = close_descriptors((first, second))
        self.assertEqual(len(errors), 1)
        self.assertEqual(second_pipe.close_attempts, 1)

    def test_reap_process_closes_owned_pipes_once(self) -> None:
        proc = _Phase6bProc(never_exit=True)
        owners = (
            OwnedStream(proc.stdout, label="stdout"),
            OwnedStream(proc.stderr, label="stderr"),
        )
        outcome = reap_process(
            proc, policy=LifecyclePolicy(0.01), pipes=owners
        )
        self.assertEqual(proc.stdout.close_attempts, 1)
        self.assertEqual(proc.stderr.close_attempts, 1)
        self.assertTrue(any(isinstance(e, TimeoutError) for e in outcome.errors))

    def test_run_captured_reuses_one_owner_pair(self) -> None:
        program = "print('captured')"
        proc, outcome = run_captured(
            (sys.executable, "-c", program), policy=LifecyclePolicy(2.0)
        )
        self.assertIsNotNone(proc)
        self.assertEqual(outcome.return_code, 0)
        self.assertEqual(outcome.errors, [])
        self.assertTrue(proc.stdout.closed)
        self.assertTrue(proc.stderr.closed)
        self.assertFalse(
            any(
                thread.name in {"captured-stdout-reader", "captured-stderr-reader"}
                for thread in threading.enumerate()
            )
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
