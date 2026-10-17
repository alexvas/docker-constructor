"""Process-adapter ownership matrices through prepared generic acquisition.

Historical case names/dimensions remain, but private reader/reap/supervisor
helpers are replaced by actual OwnedProcess boundaries and typed observations.
"""
from __future__ import annotations

import contextlib
import inspect
import os
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

import docker.npm_environment.execution as execution_module
import docker.npm_environment.lifecycle as lifecycle_module
import docker.process as process_module
import docker.filesystem.streams as streams_module
from docker.filesystem.descriptors import OwnedDescriptor
from docker.filesystem.operations import PosixDescriptorOps
from docker.process import OwnedProcess
from docker.npm_environment import AssemblyTimeoutError, DockerRunExecutor, StreamReaderFailure
from docker.npm_environment.lifecycle import LifecyclePolicy, run_captured
from tests.test_npm_environment_phase4_lifecycle import _observed_children


class _Phase6bPipe:
    def __init__(self, data=b"", *, fail_read=False, eof=False, blocked_event=None, close_error=None):
        self._data, self._fail_read, self._eof = data, fail_read, eof
        self._blocked_event, self._close_error = blocked_event, close_error
        self._terminated = threading.Event()
        self.close_attempts = 0
        self.closed = False
        self._release_observer = None

    def read(self, n):
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

    def close(self):
        if self._release_observer is not None:
            self._release_observer()
        self.close_attempts += 1
        self.closed = True
        self._terminated.set()
        if self._close_error is not None:
            raise self._close_error

    def unblock(self):
        self._terminated.set()


class _Phase6bProc:
    def __init__(self, stdout_data=b"", stderr_data=b"", *, stdout_fail_read=False,
                 resist_terminate=False, unblock_pipes_on_terminate=True, never_exit=False,
                 pre_exited=False, pipes_eof=False, stdout_close_error=None, stderr_close_error=None):
        self.stdout = _Phase6bPipe(stdout_data, fail_read=stdout_fail_read, eof=pipes_eof, close_error=stdout_close_error)
        self.stderr = _Phase6bPipe(stderr_data, eof=pipes_eof, close_error=stderr_close_error)
        self.returncode = 0 if pre_exited else None
        self.reaped = self.terminated = self.killed = False
        self.wait_calls = 0
        self._resist_terminate = resist_terminate
        self._unblock_pipes_on_terminate = unblock_pipes_on_terminate
        self._never_exit = never_exit
        self._exited = threading.Event()
        if pre_exited:
            self._exited.set()
            self.stdout.unblock()
            self.stderr.unblock()

    def poll(self):
        if self._exited.is_set() and self.returncode is None:
            self.returncode = -15
        return self.returncode

    def terminate(self):
        self.terminated = True
        if self._unblock_pipes_on_terminate:
            self.stdout.unblock()
            self.stderr.unblock()
        if not self._resist_terminate:
            self._exited.set()

    def kill(self):
        self.killed = True
        if self._unblock_pipes_on_terminate:
            self.stdout.unblock()
            self.stderr.unblock()
        self._exited.set()

    def wait(self, timeout=None):
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


class _Recorder:
    def __init__(self):
        self.owners, self.processes, self.close_calls = [], [], []

    def assert_terminal_and_at_most_once(self, case):
        case.assertTrue(self.close_calls, "shared owner must perform release")
        case.assertTrue(all(released for released, _ in self.close_calls))
        ids = [id(owner) for _, owner in self.close_calls]
        case.assertEqual(len(ids), len(set(ids)))


@contextlib.contextmanager
def _record_process_ownership():
    recorder = _Recorder()
    bind, continuation = process_module._bind_prepared, process_module._make_continuation

    def prepared(callback):
        recorder.processes.append(callback.__self__)
        return continuation(callback)

    def bound(owner, stream):
        bind(owner, stream)  # The real preallocated owner acquires first.
        recorder.owners.append(owner)
        if isinstance(stream, _Phase6bPipe):
            stream._release_observer = lambda: recorder.close_calls.append((owner.released, owner))

    with mock.patch.object(process_module, "_make_continuation", prepared), \
         mock.patch.object(process_module, "_bind_prepared", bound):
        yield recorder


def _assert_no_supported_workers(case):
    live = {thread.name for thread in threading.enumerate()}
    for name in ("npm-stdout-reader", "npm-stderr-reader", "npm-sink-dispatcher",
                 "npm-deadline-supervisor", "npm-removal-action"):
        case.assertNotIn(name, live)
    case.assertFalse(any(name.startswith("owned-process-") for name in live))


def _run_streaming(proc, *, deadline=None, grace=1, **options):
    result = {}

    def run():
        try:
            result["capture"] = DockerRunExecutor().run_streaming(
                ("docker", "run", "--rm", "alpine", "true"), deadline_seconds=deadline,
                grace_seconds=grace, **options)
        except BaseException as exc:
            result["error"] = exc

    try:
        with mock.patch.object(execution_module.subprocess, "Popen", return_value=proc):
            thread = threading.Thread(target=run, name="test-phase6b-run")
            thread.start()
            thread.join(10)
        if thread.is_alive():
            raise AssertionError("run_streaming hung past deadline")
        return result
    finally:
        # These cases intentionally model an unreapable child. Observe its
        # incomplete result, repair the fixture and settle the SAME owner.
        if proc._never_exit:
            owner = getattr(result.get("error"), "cleanup", None)
            if isinstance(owner, OwnedProcess):
                result["initial_lifecycle"] = owner.cleanup_status
            proc._never_exit = False
            if isinstance(owner, OwnedProcess):
                assert owner.wait_cleanup(2).complete


def _owned(proc, **kwargs):
    return OwnedProcess.launch(["fixture"], factory=lambda *a, **k: proc, grace_seconds=.2, **kwargs)


class ConsumerSignatureCharacterizationTests(unittest.TestCase):
    def test_run_streaming_signature_is_stable(self):
        params = inspect.signature(DockerRunExecutor.run_streaming).parameters
        for name in ("self", "argv", "secrets", "sink", "deadline_seconds", "container_name", "grace_seconds",
                     "cleanup_outcome", "stream_factory", "tail_projector", "network_url_display", "on_launched"):
            self.assertIn(name, params)

    def test_lifecycle_primitive_signatures_are_stable(self):
        self.assertEqual(list(inspect.signature(run_captured).parameters), ["argv", "policy"])
        self.assertEqual(list(inspect.signature(OwnedProcess.capture).parameters), ["self"])
        for obsolete in ("close_descriptors", "reap_process", "terminate_and_reap", "DeadlineSupervisor"):
            self.assertFalse(hasattr(lifecycle_module, obsolete), obsolete)


class CloseStreamOwnerInternalsTests(unittest.TestCase):
    def test_close_stream_owners_attempts_each_independently(self):
        proc = _Phase6bProc(pre_exited=True)
        with _record_process_ownership() as recorder:
            owner = _owned(proc)
            self.assertTrue(all(event.wait(1) for event in owner._stopped))
            owner._owners[0].close()  # Simulate an already successful terminal release.
            status = owner.shutdown()
            self.assertTrue(status.complete and not status.failures)
            self.assertEqual(owner.shutdown(), status)
        recorder.assert_terminal_and_at_most_once(self)
        self.assertEqual(proc.stdout.close_attempts, 1)
        self.assertEqual(proc.stderr.close_attempts, 1)

    def test_close_stream_owners_collects_every_failure_once(self):
        first, second = OSError("stdout close failed"), OSError("stderr close failed")
        proc = _Phase6bProc(pre_exited=True, stdout_close_error=first, stderr_close_error=second)
        owner = _owned(proc)
        status = owner.shutdown()
        self.assertEqual(tuple(f.error for f in status.failures), (first, second))
        self.assertTrue(status.complete)
        self.assertEqual(owner.shutdown(), status)
        self.assertEqual(proc.stdout.close_attempts, 1)
        self.assertEqual(proc.stderr.close_attempts, 1)

    def test_reader_failure_closes_each_stream_exactly_once(self):
        proc = _Phase6bProc(stdout_fail_read=True, resist_terminate=True,
                             unblock_pipes_on_terminate=False, never_exit=True)
        with _record_process_ownership() as recorder:
            result = _run_streaming(proc, grace=.3)
        self.assertIsInstance(result.get("error"), StreamReaderFailure)
        self.assertFalse(result["initial_lifecycle"].complete)
        self.assertEqual(len(recorder.owners), 2)
        recorder.assert_terminal_and_at_most_once(self)
        self.assertEqual(proc.stdout.close_attempts, 1)
        self.assertEqual(proc.stderr.close_attempts, 1)
        self.assertTrue(proc.terminated)
        _assert_no_supported_workers(self)

    def test_one_stream_close_failure_does_not_skip_the_other(self):
        proc = _Phase6bProc(stdout_fail_read=True, never_exit=True, stdout_close_error=OSError("stdout close failed"))
        with _record_process_ownership() as recorder:
            result = _run_streaming(proc, grace=.3)
        self.assertIsInstance(result.get("error"), StreamReaderFailure)
        recorder.assert_terminal_and_at_most_once(self)
        self.assertEqual(proc.stderr.close_attempts, 1)
        self.assertTrue(proc.stderr.closed)
        _assert_no_supported_workers(self)

    def test_repeated_cleanup_hooks_issue_no_second_close(self):
        proc = _Phase6bProc(stdout_fail_read=True, resist_terminate=True,
                             unblock_pipes_on_terminate=False, never_exit=True)
        with _record_process_ownership() as recorder:
            result = _run_streaming(proc, grace=.3)
            owner = recorder.processes[0]
            owner.shutdown()
            owner.wait_cleanup(1)
        self.assertIsInstance(result.get("error"), StreamReaderFailure)
        recorder.assert_terminal_and_at_most_once(self)
        _assert_no_supported_workers(self)

    def test_deadline_cleanup_closes_each_stream_once(self):
        proc = _Phase6bProc(never_exit=True)
        with _record_process_ownership() as recorder:
            result = _run_streaming(proc, deadline=.2, grace=.2)
        self.assertIsInstance(result.get("error"), AssemblyTimeoutError)
        self.assertFalse(result["initial_lifecycle"].complete)
        self.assertEqual(len(recorder.owners), 2)
        recorder.assert_terminal_and_at_most_once(self)
        self.assertEqual(proc.stdout.close_attempts, 1)
        self.assertEqual(proc.stderr.close_attempts, 1)
        _assert_no_supported_workers(self)


class PostLaunchSetupFaultTests(unittest.TestCase):
    def _transfer_fault(self, position, *, close_error=None):
        proc = _Phase6bProc(pre_exited=True, stderr_close_error=close_error)
        error = OSError("stream transfer failed")
        original = OwnedProcess._transfer
        attempts = []

        def transfer(owner, index):
            attempts.append(index)
            if index == position:
                raise error
            return original(owner, index)

        with _record_process_ownership() as recorder, mock.patch.object(OwnedProcess, "_transfer", transfer):
            result = _run_streaming(proc, grace=.3)
        self.assertEqual(attempts, [0, 1])
        self.assertIs(result.get("error"), close_error or error)
        if close_error is not None:
            self.assertTrue(any(f.error is error for f in recorder.processes[0].cleanup_status.failures))
        self.assertEqual(proc.stdout.close_attempts, 1)
        self.assertEqual(proc.stderr.close_attempts, 1)
        self.assertTrue(proc.reaped)
        _assert_no_supported_workers(self)

    def test_second_stream_adoption_failure_releases_first_and_reaps(self):
        self._transfer_fault(1)

    def test_first_stream_adoption_failure_accounts_for_both_streams(self):
        self._transfer_fault(0)

    def test_adoption_failure_with_simultaneous_release_defect(self):
        self._transfer_fault(0, close_error=RuntimeError("release defect"))

    def _construction(self, position):
        proc = _Phase6bProc(pre_exited=True)
        error = RuntimeError("reader construction failed")
        original = OwnedProcess._make_reader

        def construct(owner, index):
            if index == position:
                raise error
            return original(owner, index)

        with mock.patch.object(OwnedProcess, "_make_reader", construct):
            result = _run_streaming(proc, grace=.3)
        self.assertIs(result.get("error"), error)
        self.assertEqual(proc.stdout.close_attempts, 1)
        self.assertEqual(proc.stderr.close_attempts, 1)
        self.assertTrue(proc.stdout.closed and proc.stderr.closed and proc.reaped)
        self.assertFalse(proc.terminated)  # Observably exited before setup.
        _assert_no_supported_workers(self)

    def test_first_reader_construction_failure_releases_owners_and_reaps(self):
        self._construction(0)

    def test_second_reader_construction_failure_releases_owners_and_reaps(self):
        self._construction(1)

    def test_supervisor_start_failure_releases_owners_and_reaps(self):
        proc = _Phase6bProc()
        with mock.patch.object(OwnedProcess, "_start_supervisor", side_effect=RuntimeError("supervisor start failed")):
            with _record_process_ownership() as recorder:
                result = _run_streaming(proc, deadline=5, grace=.3)
        self.assertIsInstance(result.get("error"), RuntimeError)
        self.assertEqual(len(recorder.owners), 2)
        recorder.assert_terminal_and_at_most_once(self)
        self.assertTrue(proc.terminated and proc.reaped)
        _assert_no_supported_workers(self)

    def test_launch_callback_failure_closes_owners_and_reaps(self):
        proc = _Phase6bProc()
        error = RuntimeError("launch callback failed")

        def callback():
            raise error

        with _record_process_ownership() as recorder:
            result = _run_streaming(proc, grace=.3, on_launched=callback)
        self.assertIs(result.get("error"), error)
        self.assertEqual(len(recorder.owners), 2)
        recorder.assert_terminal_and_at_most_once(self)
        self.assertTrue(proc.terminated and proc.reaped)
        _assert_no_supported_workers(self)


class StreamCleanupPrecedenceTests(unittest.TestCase):
    def _run_reader_failure(self, proc):
        with _record_process_ownership() as recorder:
            result = _run_streaming(proc, grace=.3)
        recorder.assert_terminal_and_at_most_once(self)
        self.assertTrue(proc.stdout.closed and proc.stderr.closed and proc.terminated)
        _assert_no_supported_workers(self)
        return result

    def test_oserror_close_in_either_stream_releases_sibling(self):
        for position in ("stdout", "stderr"):
            with self.subTest(position=position):
                proc = _Phase6bProc(stdout_fail_read=True, never_exit=True,
                    **{position + "_close_error": OSError(position + " close failed")})
                self.assertIsInstance(self._run_reader_failure(proc).get("error"), StreamReaderFailure)

    def test_unexpected_close_defect_in_either_stream_releases_sibling(self):
        for position in ("stdout", "stderr"):
            with self.subTest(position=position):
                error = RuntimeError("close defect")
                proc = _Phase6bProc(stdout_fail_read=True, never_exit=True, **{position + "_close_error": error})
                self.assertIs(self._run_reader_failure(proc).get("error"), error)

    def test_reader_failure_with_close_defect_retains_defect_once(self):
        error = RuntimeError("close defect")
        proc = _Phase6bProc(stdout_fail_read=True, never_exit=True, stderr_close_error=error)
        result = self._run_reader_failure(proc)
        self.assertIs(result.get("error"), error)
        notes = getattr(error, "__notes__", ())
        self.assertEqual(sum(note.startswith("stdout read failed:") for note in notes), 1)
        status = error.cleanup.cleanup_status
        self.assertEqual(sum(f.error is error for f in status.failures), 1)

    def test_close_interruption_in_either_stream_releases_sibling(self):
        for position in ("stdout", "stderr"):
            with self.subTest(position=position):
                error = KeyboardInterrupt("close")
                proc = _Phase6bProc(stdout_fail_read=True, never_exit=True, **{position + "_close_error": error})
                self.assertIs(self._run_reader_failure(proc).get("error"), error)

    def test_setup_failure_with_cleanup_defect_keeps_defect_authority(self):
        defect, primary = RuntimeError("cleanup defect"), RuntimeError("setup failure")
        proc = _Phase6bProc(pre_exited=True, stdout_close_error=defect)

        def callback():
            raise primary  # Original caller operation, not a reader-construction bug.

        with _record_process_ownership() as recorder:
            result = _run_streaming(proc, grace=.3, on_launched=callback)
        self.assertIs(result.get("error"), defect)
        self.assertTrue(any(f.error is primary for f in recorder.processes[0].cleanup_status.failures))
        self.assertEqual(proc.stdout.close_attempts, 1)
        self.assertEqual(proc.stderr.close_attempts, 1)
        self.assertTrue(proc.reaped)
        _assert_no_supported_workers(self)


class CloseAuthorityPrecedenceTests(unittest.TestCase):
    def test_operation_interruption_survives_each_close_failure(self):
        for position in ("stdout", "stderr"):
            for close_error in (OSError("ordinary close failure"), RuntimeError("unexpected close defect"),
                                KeyboardInterrupt("close interruption")):
                with self.subTest(position=position, close=type(close_error).__name__):
                    sibling = OSError("sibling close failure")
                    faults = {"stdout_close_error": sibling, "stderr_close_error": sibling}
                    faults[position + "_close_error"] = close_error
                    proc = _Phase6bProc(pre_exited=True, **faults)
                    owner = _owned(proc)
                    primary = KeyboardInterrupt("operation interrupted")
                    status = owner.shutdown(primary=primary)
                    self.assertIs(status.failures[0].error, primary)
                    self.assertTrue(any(f.error is close_error for f in status.failures))
                    self.assertTrue(any(f.error is sibling for f in status.failures))
                    self.assertEqual(proc.stdout.close_attempts, 1)
                    self.assertEqual(proc.stderr.close_attempts, 1)

    def test_close_interruption_outranks_an_ordinary_operation_failure(self):
        primary, interruption = RuntimeError("operation failed"), KeyboardInterrupt("close interrupted")
        proc = _Phase6bProc(pre_exited=True, stdout_close_error=interruption)
        owner = _owned(proc)
        status = owner.shutdown(primary=primary)
        self.assertIs(status.failures[0].error, interruption)
        self.assertTrue(any(f.error is primary for f in status.failures))
        self.assertTrue(status.complete)


class ReapingBeforeCloseAuthorityTests(unittest.TestCase):
    def test_collection_setup_failure_reaps_before_close_defect(self):
        proc = _Phase6bProc(stderr_close_error=RuntimeError("close defect"))

        def factory(name):
            raise RuntimeError("stream factory failed")

        result = _run_streaming(proc, grace=.3, stream_factory=factory)
        self.assertIsInstance(result.get("error"), RuntimeError)
        self.assertIn("close defect", str(result["error"]))
        self.assertTrue(proc.terminated and proc.reaped)
        _assert_no_supported_workers(self)

    def test_normal_path_reaps_live_child_before_close_defect(self):
        proc = _Phase6bProc(pipes_eof=True, stderr_close_error=RuntimeError("close defect"))
        timer = threading.Timer(.1, proc.terminate)
        timer.start()
        try:
            result = _run_streaming(proc, grace=.3)
        finally:
            timer.join(2)
        self.assertIsInstance(result.get("error"), RuntimeError)
        self.assertIn("close defect", str(result["error"]))
        self.assertTrue(proc.reaped)
        _assert_no_supported_workers(self)


class RealPipeSafetyTests(unittest.TestCase):
    def _streaming_real(self, program, *, deadline=None, grace=1):
        # Acquisition itself belongs to launch, never a prelaunched Popen mock.
        return DockerRunExecutor().run_streaming((sys.executable, "-c", program),
            deadline_seconds=deadline, grace_seconds=grace)

    def test_large_simultaneous_output_is_drained(self):
        result = self._streaming_real("import os;data=b'y'*262144;os.write(1,data);os.write(2,data)", grace=3)
        self.assertEqual(result.return_code, 0)
        self.assertTrue(result.stdout and result.stderr)
        _assert_no_supported_workers(self)

    def test_descendant_holding_write_end_completes_within_bounds(self):
        program = ("import subprocess,sys; "
                   "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(5)']); "
                   "print(child.pid,flush=True);print('parent done',flush=True)")
        started = time.monotonic()
        result = self._streaming_real(program, grace=.2)
        try:
            self.assertEqual(result.return_code, 0)
            self.assertLess(time.monotonic() - started, 3)
        finally:
            if result.stdout.strip():
                try:
                    os.kill(int(result.stdout.splitlines()[0]), 15)
                except ProcessLookupError:
                    pass
        _assert_no_supported_workers(self)

    def test_real_pipe_deadline_terminates_within_bounds(self):
        started = time.monotonic()
        with self.assertRaises(AssemblyTimeoutError):
            self._streaming_real("import time;time.sleep(30)", deadline=.3, grace=.2)
        self.assertLess(time.monotonic() - started, 3)
        _assert_no_supported_workers(self)

    def test_repeated_real_runs_do_not_corrupt_unrelated_fd(self):
        ops, read_label, write_label = PosixDescriptorOps(), "unrelated read", "unrelated write"
        read_fd, write_fd = os.pipe()
        with OwnedDescriptor(ops, read_fd, label=read_label) as read_owner:
            with OwnedDescriptor(ops, write_fd, label=write_label) as write_owner:
                for _ in range(5):
                    self.assertEqual(self._streaming_real("print('ok')").return_code, 0)
                    os.write(write_owner.fd, b"x")
                    self.assertEqual(os.read(read_owner.fd, 1), b"x")
                _assert_no_supported_workers(self)


class _FilenoPipe(_Phase6bPipe):
    def fileno(self):
        return 999


class PreparedAdapterFaultTests(unittest.TestCase):
    def _invoke(self, client, proc):
        if client == "captured":
            return run_captured(("client",), policy=LifecyclePolicy(.2))
        return DockerRunExecutor().run_streaming(("client",), grace_seconds=.2)

    def test_first_and_second_real_owner_lock_failure_precede_main_launch(self):
        original = process_module._prepare_stream
        for client in ("captured", "streaming"):
            for position in (0, 1):
                with self.subTest(client=client, position=position):
                    error = RuntimeError("owner lock construction")
                    calls = []
                    proc = _Phase6bProc(pre_exited=True)

                    def prepare(*, label):
                        calls.append(label)
                        if len(calls) - 1 == position:
                            with mock.patch.object(streams_module.threading, "Lock", side_effect=error):
                                return original(label=label)
                        return original(label=label)

                    failure = None
                    with mock.patch.object(process_module, "_prepare_stream", prepare), \
                         mock.patch.object(execution_module.subprocess, "Popen", return_value=proc) as factory:
                        try:
                            result = self._invoke(client, proc)
                            if client == "captured":
                                failure = result[1].errors[0] if result[1].errors else None
                        except BaseException as exc:
                            failure = exc
                    self.assertEqual(factory.call_count, 0)
                    self.assertIs(failure, error)
                    self.assertEqual(proc.stdout.close_attempts, 0)
                    self.assertEqual(proc.stderr.close_attempts, 0)

    def test_policy_preparation_failure_precedes_streaming_main_launch(self):
        error = RuntimeError("policy preparation")
        proc = _Phase6bProc(pre_exited=True)

        class Action:
            def prepare(self): raise error
            def start(self, context): raise AssertionError("unprepared action started")
            def observe(self): return process_module.ActionResult(True, True)
            def cancel(self, deadline): pass

        original = OwnedProcess.launch

        def launch(*args, **kwargs):
            kwargs["stop_policy"] = process_module.StopPolicy((process_module.StopStage(.01, Action()),), .01)
            return original(*args, **kwargs)

        with mock.patch.object(OwnedProcess, "launch", side_effect=launch), \
             mock.patch.object(execution_module.subprocess, "Popen", return_value=proc) as factory:
            with self.assertRaises(RuntimeError) as caught:
                self._invoke("streaming", proc)
        self.assertIs(caught.exception, error)
        self.assertEqual(factory.call_count, 0)
        self.assertEqual(proc.stdout.close_attempts, 0)
        self.assertEqual(proc.stderr.close_attempts, 0)

    def test_first_ordinary_transfer_then_second_interruption_identity(self):
        for client in ("captured", "streaming"):
            with self.subTest(client=client):
                ordinary, interruption = OSError("first transfer"), KeyboardInterrupt("second transfer")
                proc = _Phase6bProc(pre_exited=True)
                attempts = []

                def transfer(owner, index):
                    attempts.append(index)
                    raise ordinary if index == 0 else interruption

                with mock.patch.object(OwnedProcess, "_transfer", transfer), \
                     mock.patch.object(execution_module.subprocess, "Popen", return_value=proc):
                    with self.assertRaises(KeyboardInterrupt) as caught:
                        self._invoke(client, proc)
                self.assertIs(caught.exception, interruption)
                self.assertEqual(attempts, [0, 1])
                self.assertTrue(any(f.error is ordinary for f in interruption.cleanup.cleanup_status.failures))
                self.assertEqual(proc.stdout.close_attempts, 1)
                self.assertEqual(proc.stderr.close_attempts, 1)
                self.assertTrue(proc.reaped)


class CapturedSetupFaultTests(unittest.TestCase):
    def _run(self, proc):
        with mock.patch.object(lifecycle_module.subprocess, "Popen", return_value=proc):
            return run_captured(("client",), policy=LifecyclePolicy(.2))

    def _assert_clean(self, proc, outcome=None):
        self.assertEqual(proc.stdout.close_attempts, 1)
        self.assertEqual(proc.stderr.close_attempts, 1)
        self.assertTrue(proc.stdout.closed and proc.stderr.closed and proc.reaped)
        _assert_no_supported_workers(self)

    def _transfer_fault(self, position):
        proc = _Phase6bProc(pre_exited=True)
        error = OSError("captured transfer failed")
        original = OwnedProcess._transfer
        attempts = []

        def transfer(owner, index):
            attempts.append(index)
            if index == position:
                raise error
            return original(owner, index)

        with mock.patch.object(OwnedProcess, "_transfer", transfer):
            owner, outcome = self._run(proc)
        self.assertEqual(attempts, [0, 1])
        self.assertIsInstance(owner, OwnedProcess)
        self.assertIs(outcome.errors[0], error)
        self.assertTrue(owner.wait_cleanup(.5).complete)
        self._assert_clean(proc, outcome)

    def test_first_pipe_adoption_failure_stops_and_reaps(self):
        self._transfer_fault(0)

    def test_second_pipe_adoption_failure_stops_and_reaps(self):
        self._transfer_fault(1)

    def test_descriptor_configuration_failure_stops_and_reaps(self):
        proc = _Phase6bProc(pre_exited=True)
        proc.stdout, proc.stderr = _FilenoPipe(), _FilenoPipe()
        with mock.patch.object(process_module.os, "set_blocking", side_effect=RuntimeError("descriptor configuration failed")):
            _, outcome = self._run(proc)
        self.assertIsInstance(outcome.errors[0], RuntimeError)
        self._assert_clean(proc, outcome)

    def _start_fault(self, proc, index, error):
        original = OwnedProcess._start_reader

        def start(owner, position):
            if position == index:
                raise error
            return original(owner, position)

        with mock.patch.object(OwnedProcess, "_start_reader", start):
            return self._run(proc)

    def test_first_reader_start_failure_stops_and_reaps(self):
        proc = _Phase6bProc(pre_exited=True)
        _, outcome = self._start_fault(proc, 0, RuntimeError("reader start failed"))
        self.assertIsInstance(outcome.errors[0], RuntimeError)
        self._assert_clean(proc, outcome)

    def test_second_reader_start_failure_stops_first_reader_and_reaps(self):
        proc = _Phase6bProc(pre_exited=True)
        _, outcome = self._start_fault(proc, 1, RuntimeError("reader start failed"))
        self.assertIsInstance(outcome.errors[0], RuntimeError)
        self._assert_clean(proc, outcome)

    def test_real_pipe_partial_reader_startup_is_stopped_and_reaped(self):
        original_start, original_transfer = OwnedProcess._start_reader, OwnedProcess._transfer
        children = []

        def transfer(owner, index):
            original_transfer(owner, index)
            if index == 0:
                children.append(owner._proc)  # Already escrow-owned, observation only.

        def start(owner, index):
            if index == 1:
                raise RuntimeError("reader start failed")
            return original_start(owner, index)

        with mock.patch.object(OwnedProcess, "_transfer", transfer), mock.patch.object(OwnedProcess, "_start_reader", start):
            owner, outcome = run_captured((sys.executable, "-c", "import time;time.sleep(30)"), policy=LifecyclePolicy(.3))
        self.assertIsInstance(outcome.errors[0], RuntimeError)
        self.assertTrue(owner.cleanup_status.complete)
        self.assertTrue(children[0].stdout.closed and children[0].stderr.closed)
        self.assertIsNotNone(children[0].returncode)
        _assert_no_supported_workers(self)

    def test_setup_failure_with_ordinary_close_failure_keeps_setup_primary(self):
        proc = _Phase6bProc(pre_exited=True, stderr_close_error=OSError("ordinary close failure"))
        _, outcome = self._start_fault(proc, 0, RuntimeError("reader start failed"))
        self.assertEqual(str(outcome.errors[0]), "reader start failed")
        self.assertTrue(any(isinstance(e, OSError) and "ordinary close failure" in str(e) for e in outcome.errors[1:]))
        self._assert_clean(proc, outcome)

    def test_setup_failure_with_unexpected_close_defect_takes_authority(self):
        proc = _Phase6bProc(pre_exited=True, stderr_close_error=RuntimeError("close defect"))
        _, outcome = self._start_fault(proc, 0, OSError("reader start failed"))
        self.assertIn("close defect", str(outcome.errors[0]))
        self.assertTrue(any("reader start failed" in str(e) for e in outcome.errors[1:]))
        self._assert_clean(proc, outcome)

    def test_setup_failure_with_close_interruption_takes_authority(self):
        interruption = KeyboardInterrupt("close interrupted")
        proc = _Phase6bProc(pre_exited=True, stderr_close_error=interruption)
        with self.assertRaises(KeyboardInterrupt) as caught:
            self._start_fault(proc, 0, RuntimeError("reader start failed"))
        self.assertIs(caught.exception, interruption)
        status = interruption.cleanup.cleanup_status
        self.assertTrue(any("reader start failed" in str(f.error) for f in status.failures))
        self._assert_clean(proc)

    def test_partial_reader_startup_with_close_defect_stops_first_reader(self):
        proc = _Phase6bProc(pre_exited=True, stderr_close_error=RuntimeError("close defect"))
        _, outcome = self._start_fault(proc, 1, RuntimeError("reader start failed"))
        # Equal-rank unexpected defects retain the first occurrence.
        self.assertEqual(str(outcome.errors[0]), "reader start failed")
        self.assertTrue(any("close defect" in str(e) for e in outcome.errors[1:]))
        self._assert_clean(proc, outcome)


class LifecyclePrimitiveOwnerTests(unittest.TestCase):
    def test_close_descriptors_uses_owned_stream_release(self):
        proc = _Phase6bProc(pre_exited=True)
        owner = _owned(proc)
        self.assertTrue(owner.shutdown().complete)
        self.assertTrue(owner.shutdown().complete)
        self.assertEqual(proc.stdout.close_attempts, 1)
        self.assertEqual(proc.stderr.close_attempts, 1)

    def test_close_descriptors_attempts_independent_streams_once(self):
        proc = _Phase6bProc(pre_exited=True, stdout_close_error=OSError("one"))
        owner = _owned(proc)
        status = owner.shutdown()
        self.assertEqual(len(status.failures), 1)
        self.assertTrue(status.complete)
        self.assertEqual(proc.stdout.close_attempts, 1)
        self.assertEqual(proc.stderr.close_attempts, 1)

    def test_reap_process_closes_owned_pipes_once(self):
        proc = _Phase6bProc(never_exit=True)
        owner = None
        try:
            with mock.patch.object(lifecycle_module.subprocess, "Popen", return_value=proc):
                owner, outcome = run_captured(("client",), policy=LifecyclePolicy(.01))
            self.assertEqual(proc.stdout.close_attempts, 1)
            self.assertEqual(proc.stderr.close_attempts, 1)
            self.assertFalse(owner.cleanup_status.complete)
            self.assertTrue(any(isinstance(e, subprocess.TimeoutExpired) for e in outcome.errors))
        finally:
            proc._never_exit = False
            if owner is not None:
                self.assertTrue(owner.wait_cleanup(1).complete)

    def test_run_captured_reuses_one_owner_pair(self):
        with _observed_children() as children:
            owner, outcome = run_captured((sys.executable, "-c", "print('captured')"), policy=LifecyclePolicy(2))
        self.assertIsInstance(owner, OwnedProcess)
        self.assertEqual(outcome.return_code, 0)
        self.assertEqual(outcome.errors, [])
        self.assertTrue(owner.cleanup_status.complete)
        self.assertTrue(children[0].stdout.closed and children[0].stderr.closed)
        _assert_no_supported_workers(self)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
