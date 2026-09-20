"""Phase 3 — deadline, interruption, and mutable-state cleanup (tasks 3.1–3.6).

The constructor-owned total assembly deadline terminates the local docker
client, force-removes the deterministic named container, and reaps within a
bounded grace period before raising a distinct structured timeout failure.
Interruption (``KeyboardInterrupt``) is cleaned up the same way before it
propagates.  Failed paths preserve the opaque npm cache and prior immutable
outputs, publish nothing partial, and abandoned same-input staging is
securely replaced (or fails closed) under the input-identity lock.
"""

from __future__ import annotations

import base64
import io
import json
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR.parent))

import docker.npm_environment.execution as execution_module
from docker.npm_environment.lifecycle import DeadlineSupervisor
from docker.npm_environment import (
    AssemblyTimeoutError,
    DockerRunExecutor,
    LockedNpmError,
    ProcessResult,
    REDACTED,
    RootSpec,
    STREAM_STDOUT,
    StreamReaderFailure,
    TRUNCATION_NOTICE,
    assemble,
    assemble_environment,
    assembler_script_digest,
    collect_streams,
    compute_assembler_identity,
    compute_assembler_input_identity,
    npm_policy_digest,
    preflight,
    publication,
    publish_environment,
)
from docker.versioning.npm_diagnostic_stream import make_stream_factory, project_tail
from docker.versioning.diagnostic_projection import INCOMPLETE_TOKEN_MARKER

_IMAGE = "sha256:" + "a" * 64
_NODE = "24.18.0"
_NPM = "11.16.0"
_PLATFORM = "linux-x64"


def _sri() -> str:
    return "sha512-" + base64.b64encode(bytes([0xAB]) * 64).decode()


def _lock(roots: dict[str, str]) -> bytes:
    packages = {
        "": {"name": "root", "version": "1.0.0", "dependencies": dict(roots)}
    }
    for name, version in roots.items():
        packages[f"node_modules/{name}"] = {
            "version": version,
            "resolved": f"https://registry.npmjs.org/{name}/-/{name}-{version}.tgz",
            "integrity": _sri(),
        }
    return json.dumps(
        {
            "name": "root",
            "version": "1.0.0",
            "lockfileVersion": 3,
            "requires": True,
            "packages": packages,
        }
    ).encode()


def _validated(roots: dict[str, str] | None = None):
    roots = {"a": "1.0.0"} if roots is None else roots
    return preflight(
        _lock(roots),
        roots=tuple(RootSpec(n, v) for n, v in sorted(roots.items())),
        platform=_PLATFORM,
        node_version=_NODE,
        npm_version=_NPM,
    )


def _assembler():
    return compute_assembler_identity(
        image_digest=_IMAGE,
        node_version=_NODE,
        npm_version=_NPM,
        script_digest=assembler_script_digest(),
        policy_digest=npm_policy_digest(),
        platform=_PLATFORM,
    )


def _assert_no_workers(testcase: unittest.TestCase) -> None:
    live = {t.name for t in threading.enumerate()}
    for name in (
        "npm-stdout-reader",
        "npm-stderr-reader",
        "npm-sink-dispatcher",
        "npm-deadline-supervisor",
    ):
        testcase.assertNotIn(name, live)


class _DeadlinePipe:
    """Pipe stub that serves buffered data, then blocks until terminated."""

    def __init__(
        self,
        data: bytes = b"",
        blocked_event: threading.Event | None = None,
        fail_read: bool = False,
        eof: bool = False,
        close_failure: BaseException | None = None,
    ):
        self._data = data
        self._blocked_event = blocked_event
        self._fail_read = fail_read
        self._eof = eof
        self._close_failure = close_failure
        self._terminated = threading.Event()
        self.close_attempts = 0

    def read(self, n: int) -> bytes:
        if self._fail_read:
            raise OSError("stdout reader failure")
        if self._data:
            chunk, self._data = self._data[:n], self._data[n:]
            return chunk
        if self._eof:
            return b""  # already at EOF without waiting for termination
        if self._blocked_event is not None:
            self._blocked_event.set()
        self._terminated.wait()
        return b""

    def close(self) -> None:
        self.close_attempts += 1
        # Unblock the reader before any failure so a raising close can never
        # strand a blocked reader.
        self._terminated.set()
        if self._close_failure is not None:
            raise self._close_failure

    def unblock(self) -> None:
        self._terminated.set()


class _DeadlineProc:
    """``subprocess.Popen`` stub whose pipes block until termination."""

    def __init__(
        self,
        stdout_data: bytes = b"",
        stderr_data: bytes = b"",
        *,
        resist_terminate: bool = False,
        stdout_blocked_event: threading.Event | None = None,
        pre_exited: bool = False,
        wait_failure: BaseException | None = None,
        unblock_pipes_on_terminate: bool = True,
        never_exit: bool = False,
        stdout_fail_read: bool = False,
        pipes_eof: bool = False,
        terminate_failure: BaseException | None = None,
    ):
        self.stdout = _DeadlinePipe(
            stdout_data,
            stdout_blocked_event,
            fail_read=stdout_fail_read,
            eof=pipes_eof,
        )
        self.stderr = _DeadlinePipe(stderr_data, eof=pipes_eof)
        self.returncode: int | None = 0 if pre_exited else None
        self.reaped = False
        self.terminated = False
        self.killed = False
        self._resist_terminate = resist_terminate
        self._unblock_pipes_on_terminate = unblock_pipes_on_terminate
        self._never_exit = never_exit
        self._terminate_failure = terminate_failure
        self._exited = threading.Event()
        self._wait_failure = wait_failure
        self._wait_failure_raised = False
        self.wait_calls = 0
        if pre_exited:
            self._exited.set()
            self.stdout.unblock()
            self.stderr.unblock()

    def terminate(self) -> None:
        self.terminated = True
        if self._unblock_pipes_on_terminate:
            self.stdout.unblock()
            self.stderr.unblock()
        if self._terminate_failure is not None:
            raise self._terminate_failure
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
        if self._wait_failure is not None and not self._wait_failure_raised:
            self._wait_failure_raised = True
            raise self._wait_failure
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


class _CloseTrackingTextPipe(io.StringIO):
    """String pipe stub recording every close attempt."""

    def __init__(self, data: str = ""):
        super().__init__(data)
        self.close_attempts = 0

    def close(self) -> None:
        self.close_attempts += 1
        super().close()


class _RmProc:
    """Bounded ``docker rm -f`` client stub."""

    def __init__(
        self,
        *,
        return_code: int = 0,
        stdout: str = "",
        stderr: str = "",
        never_exit: bool = False,
    ):
        self.returncode: int | None = None
        self._return_code = return_code
        self.stdout = _CloseTrackingTextPipe(stdout)
        self.stderr = _CloseTrackingTextPipe(stderr)
        self._never_exit = never_exit
        self.terminated = False
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        if self._never_exit and timeout is not None:
            raise subprocess.TimeoutExpired(("docker", "rm"), timeout)
        self.returncode = self._return_code
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


class _BlockingRmProc(_RmProc):
    """rm client stub that waits for a test-controlled release."""

    def __init__(self, release: threading.Event):
        super().__init__()
        self._release = release

    def wait(self, timeout: float | None = None) -> int:
        if not self._release.wait(timeout):
            raise subprocess.TimeoutExpired(("docker", "rm"), timeout)
        return super().wait(timeout=0)


class _RecordingExecutor:
    """Non-streaming executor with a canned nonzero result."""

    def __init__(self, *, return_code: int, stdout: str = "", stderr: str = ""):
        self.return_code = return_code
        self.stdout = stdout
        self.stderr = stderr
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: tuple[str, ...]):
        self.calls.append(argv)
        if argv[1] == "run":
            return ProcessResult(argv, self.return_code, self.stdout, self.stderr)
        return ProcessResult(argv, 0, "", "")


class _PopulatingExecutor:
    """Fake Docker executor that writes the assembled tree into staging."""

    def __init__(self, marker: bytes = b"assembled"):
        self.marker = marker
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: tuple[str, ...]):
        self.calls.append(argv)
        if argv[1] == "run":
            staging: Path | None = None
            for i, token in enumerate(argv):
                if token == "--volume" and i + 1 < len(argv):
                    spec = argv[i + 1]
                    if spec.endswith(":/work:rw"):
                        staging = Path(spec.rsplit(":", 2)[0])
            if staging is not None:
                pkg = staging / "node_modules" / "a"
                pkg.mkdir(parents=True, exist_ok=True)
                (pkg / "package.json").write_text(
                    json.dumps({"name": "a", "version": "1.0.0"})
                )
                (pkg / "marker.txt").write_bytes(self.marker)
        return ProcessResult(argv, 0, "", "")


class DeadlineTestCase(unittest.TestCase):
    """Shared helpers for deadline/interruption/storage tests."""

    def _cache_root(self) -> Path:
        tmp = tempfile.TemporaryDirectory(prefix="npm-env-phase3-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "cache"
        root.mkdir()
        return root

    def _namespace(self, cache_root: Path):
        return publication.prepare_assembler_namespace(
            cache_root, _assembler().digest
        )

    def _run_streaming(
        self,
        proc,
        *,
        deadline,
        grace=1.0,
        sink=None,
        secrets=(),
        tail_projector=None,
        stream_factory=None,
    ):
        result: dict = {}

        def run() -> None:
            try:
                result["capture"] = DockerRunExecutor().run_streaming(
                    ("docker", "run", "--rm", "alpine", "true"),
                    secrets=secrets,
                    sink=sink,
                    deadline_seconds=deadline,
                    grace_seconds=grace,
                    tail_projector=tail_projector,
                    stream_factory=stream_factory,
                )
            except BaseException as exc:  # pragma: no cover - test aid
                result["error"] = exc

        with mock.patch.object(
            execution_module.subprocess, "Popen", return_value=proc
        ):
            thread = threading.Thread(target=run, name="test-deadline-run")
            thread.start()
            thread.join(10.0)
        self.assertFalse(thread.is_alive(), "run_streaming hung past deadline")
        return result


class TestDeadline(DeadlineTestCase):
    def test_deadline_terminates_and_reaps(self):
        proc = _DeadlineProc()
        result = self._run_streaming(proc, deadline=0.2)
        exc = result.get("error")
        self.assertIsInstance(exc, AssemblyTimeoutError)
        self.assertEqual(exc.reason, "assembly_timeout")
        self.assertTrue(proc.terminated)
        self.assertTrue(proc.reaped)
        _assert_no_workers(self)

    def test_deadline_sigkill_fallback_within_grace(self):
        proc = _DeadlineProc(resist_terminate=True)
        result = self._run_streaming(proc, deadline=0.2, grace=0.5)
        exc = result.get("error")
        self.assertIsInstance(exc, AssemblyTimeoutError)
        self.assertTrue(proc.terminated)
        # SIGTERM was insufficient, so the bounded grace period escalated to
        # SIGKILL and reaped the client.
        self.assertTrue(proc.killed)
        self.assertTrue(proc.reaped)
        _assert_no_workers(self)

    def test_deadline_delivers_accepted_chunks_redacted(self):
        delivered: list = []

        def sink(chunk) -> None:
            delivered.append(chunk)

        proc = _DeadlineProc(stdout_data=b"installing SUPERSECRET\nmore\n")
        result = self._run_streaming(
            proc, deadline=0.3, sink=sink, secrets=("SUPERSECRET",)
        )
        exc = result.get("error")
        self.assertIsInstance(exc, AssemblyTimeoutError)
        # Accepted pre-deadline chunks reached the sink, redacted.
        self.assertTrue(any(c.stream == STREAM_STDOUT for c in delivered))
        for chunk in delivered:
            self.assertNotIn("SUPERSECRET", chunk.text)
        # The dispatcher was joined before the timeout surfaced.
        _assert_no_workers(self)

    def test_deadline_preserves_truncation_notice_when_sink_fails(self):
        def sink(chunk) -> None:
            raise RuntimeError("sink exploded")

        proc = _DeadlineProc(stdout_data=b"out\n" * 200)
        result = self._run_streaming(proc, deadline=0.3, sink=sink)
        exc = result.get("error")
        self.assertIsInstance(exc, AssemblyTimeoutError)
        # Live-delivery loss is retained, without replacing the timeout.
        self.assertIn(TRUNCATION_NOTICE, exc.detail)
        _assert_no_workers(self)

    def test_stream_eof_before_process_exit_still_enforces_deadline(self):
        # Both pipes reach EOF immediately, but the client keeps running past
        # the deadline.  The supervisor must stay active after stream EOF —
        # closing the output pipes is not process completion — and then
        # terminate and reap the client and raise the timeout.
        proc = _DeadlineProc(pipes_eof=True)
        result = self._run_streaming(proc, deadline=0.2, grace=0.5)
        exc = result.get("error")
        self.assertIsInstance(exc, AssemblyTimeoutError)
        self.assertEqual(exc.reason, "assembly_timeout")
        self.assertTrue(proc.terminated)
        self.assertTrue(proc.reaped)
        _assert_no_workers(self)


class TestSupervisorFailure(DeadlineTestCase):
    """Unexpected ``proc.wait(timeout=...)`` failures must not strand work."""

    def test_proc_exiting_before_deadline_returns_normally(self):
        proc = _DeadlineProc(pre_exited=True)
        result = self._run_streaming(proc, deadline=0.5)
        # The compatible ``wait(timeout=...)`` accepted the deadline argument
        # and returned before it expired: a normal result, no timeout.
        self.assertNotIn("error", result)
        self.assertFalse(proc.terminated)
        self.assertTrue(proc.reaped)
        _assert_no_workers(self)

    def test_unexpected_wait_failure_is_primary_and_cleans_up(self):
        proc = _DeadlineProc(
            stdout_data=b"working\n",
            wait_failure=OSError("SUPERSECRET timed wait exploded"),
        )

        def fake_popen(argv, **kwargs):
            if argv[1] == "rm":
                return _RmProc(
                    return_code=1,
                    stderr="Error response from daemon: removal in progress",
                )
            return proc

        result: dict = {}

        def run() -> None:
            try:
                DockerRunExecutor().run_streaming(
                    ("docker", "run", "--rm", "alpine", "true"),
                    secrets=("SUPERSECRET",),
                    deadline_seconds=0.2,
                    container_name="npm-assembler-test",
                    grace_seconds=0.5,
                )
            except BaseException as exc:  # pragma: no cover - test aid
                result["error"] = exc

        with mock.patch.object(
            execution_module.subprocess, "Popen", side_effect=fake_popen
        ):
            thread = threading.Thread(target=run, name="test-deadline-run")
            thread.start()
            thread.join(10.0)
        self.assertFalse(thread.is_alive(), "run_streaming hung past the failure")

        exc = result.get("error")
        # The unexpected supervisor failure is primary, bounded, and redacted.
        self.assertIsInstance(exc, OSError)
        self.assertIn("timed wait exploded", str(exc))
        self.assertNotIn("SUPERSECRET", str(exc))
        # The supervisor still terminated/reaped the client and unblocked the
        # readers (the run above returned rather than hanging).
        self.assertTrue(proc.terminated)
        self.assertTrue(proc.reaped)
        # Cleanup failures (container removal) are preserved as redacted notes.
        notes = "\n".join(getattr(exc, "__notes__", ()))
        self.assertIn("supervisor cleanup failed", notes)
        self.assertNotIn("SUPERSECRET", notes)
        _assert_no_workers(self)


class TestUrlFreeFailureNotes(DeadlineTestCase):
    """URL-bearing cleanup/close failures never leak into attached notes."""

    _URL = "https://user:pass@registry.example.com/pkg?token=abc#frag"

    def _assert_url_free(self, text: str) -> None:
        for fragment in (
            self._URL,
            "https://",
            "registry.example.com",
            "user:pass",
            "token=abc",
            "#frag",
            "SUPERSECRET",
        ):
            self.assertNotIn(fragment, text)

    def test_timeout_cleanup_note_is_url_free_and_timeout_primary(self):
        proc = _DeadlineProc(
            terminate_failure=OSError(
                f"terminate failed fetching {self._URL} (SUPERSECRET)"
            )
        )
        result = self._run_streaming(
            proc,
            deadline=0.2,
            grace=0.3,
            secrets=("SUPERSECRET",),
            tail_projector=project_tail,
        )
        exc = result.get("error")
        # The constructor-owned timeout classification stays primary.
        self.assertIsInstance(exc, AssemblyTimeoutError)
        self.assertEqual("assembly_timeout", exc.reason)
        rendered = "\n".join([str(exc), *getattr(exc, "__notes__", ())])
        self.assertIn("deadline cleanup failed", rendered)
        self.assertIn(REDACTED, rendered)
        self._assert_url_free(rendered)
        _assert_no_workers(self)

    def test_timeout_pipe_close_note_is_url_free(self):
        proc = _DeadlineProc(stdout_data=b"working\n")
        proc.stdout = _DeadlinePipe(
            close_failure=OSError(f"close failed {self._URL} SUPERSECRET")
        )
        proc.stderr = _DeadlinePipe(
            close_failure=OSError(f"close failed {self._URL} SUPERSECRET")
        )
        result = self._run_streaming(
            proc,
            deadline=0.2,
            grace=0.3,
            secrets=("SUPERSECRET",),
            tail_projector=project_tail,
        )
        exc = result.get("error")
        self.assertIsInstance(exc, AssemblyTimeoutError)
        self.assertEqual("assembly_timeout", exc.reason)
        rendered = "\n".join([str(exc), *getattr(exc, "__notes__", ())])
        self.assertIn("pipe close failed", rendered)
        self.assertIn(REDACTED, rendered)
        self._assert_url_free(rendered)
        _assert_no_workers(self)

    def test_supervisor_wait_failure_detail_is_url_free(self):
        proc = _DeadlineProc(
            stdout_data=b"working\n",
            wait_failure=OSError(
                f"timed wait exploded {self._URL} SUPERSECRET"
            ),
        )
        result = self._run_streaming(
            proc,
            deadline=0.2,
            grace=0.3,
            secrets=("SUPERSECRET",),
            tail_projector=project_tail,
        )
        exc = result.get("error")
        self.assertIsInstance(exc, OSError)
        rendered = "\n".join([str(exc), *getattr(exc, "__notes__", ())])
        self.assertIn("timed wait exploded", rendered)
        self.assertIn(REDACTED, rendered)
        self._assert_url_free(rendered)
        _assert_no_workers(self)


class TestForcedEofFinalization(DeadlineTestCase):
    """A deadline-forced EOF finalizes pending candidates as aborted."""

    def test_timeout_forces_abort_finalization_of_pending_prefix(self):
        # The reader holds an ambiguous percent-encoded scheme prefix when the
        # deadline terminates the client; the retained timeout tail must fail
        # closed rather than flushing ``%68%74`` as ordinary text.
        proc = _DeadlineProc(stdout_data=b"npm status %68%74")
        result = self._run_streaming(
            proc,
            deadline=0.2,
            grace=0.3,
            stream_factory=make_stream_factory(()),
            tail_projector=project_tail,
        )
        exc = result.get("error")
        self.assertIsInstance(exc, AssemblyTimeoutError)
        self.assertEqual("assembly_timeout", exc.reason)
        rendered = "\n".join([str(exc), *getattr(exc, "__notes__", ())])
        self.assertIn(INCOMPLETE_TOKEN_MARKER, rendered)
        self.assertNotIn("%68%74", rendered)
        _assert_no_workers(self)

    def test_clean_unforced_eof_keeps_the_prefix(self):
        # Control: without a deadline/cancellation the same prefix is a clean
        # EOF and stays ordinary text (unchanged historical behavior).
        proc = _DeadlineProc(stdout_data=b"npm status %68%74", pre_exited=True)
        result = self._run_streaming(
            proc,
            deadline=5.0,
            grace=0.3,
            stream_factory=make_stream_factory(()),
            tail_projector=project_tail,
        )
        self.assertNotIn("error", result)
        capture = result["capture"]
        self.assertIn("%68%74", capture.stdout)
        self.assertNotIn(INCOMPLETE_TOKEN_MARKER, capture.stdout)
        _assert_no_workers(self)


class TestForcedCleanup(DeadlineTestCase):
    """Forced cleanup must unblock blocked readers within a finite time."""

    def test_kill_wait_timeout_is_bounded_and_recorded(self):
        proc = _DeadlineProc(stdout_data=b"working\n", never_exit=True)
        result = self._run_streaming(proc, deadline=0.2, grace=0.3)
        exc = result.get("error")
        # The client resisted SIGTERM and SIGKILL: the bounded post-kill wait
        # expired and was recorded as a cleanup error instead of hanging.
        self.assertIsInstance(exc, AssemblyTimeoutError)
        self.assertTrue(proc.terminated)
        self.assertTrue(proc.killed)
        self.assertFalse(proc.reaped)
        notes = "\n".join(getattr(exc, "__notes__", ()))
        self.assertIn("deadline cleanup failed", notes)
        self.assertIn("did not exit after SIGKILL", notes)
        _assert_no_workers(self)

    def test_timeout_rm_client_unreapable_is_bounded_and_secondary(self):
        proc = _DeadlineProc()
        rm_proc = _RmProc(never_exit=True)
        result: dict = {}

        def fake_popen(argv, **kwargs):
            return rm_proc if argv[1] == "rm" else proc

        def run() -> None:
            try:
                DockerRunExecutor().run_streaming(
                    ("docker", "run", "--rm", "alpine", "true"),
                    deadline_seconds=0.1,
                    container_name="npm-assembler-rm-timeout",
                    grace_seconds=0.1,
                )
            except BaseException as exc:  # pragma: no cover - test aid
                result["error"] = exc

        with mock.patch.object(
            execution_module.subprocess, "Popen", side_effect=fake_popen
        ):
            thread = threading.Thread(target=run, name="test-deadline-run")
            thread.start()
            thread.join(5.0)
        self.assertFalse(thread.is_alive(), "run_streaming exceeded cleanup budget")

        exc = result.get("error")
        self.assertIsInstance(exc, AssemblyTimeoutError)
        self.assertTrue(rm_proc.terminated)
        self.assertTrue(rm_proc.killed)
        self.assertGreaterEqual(rm_proc.stdout.close_attempts, 1)
        self.assertGreaterEqual(rm_proc.stderr.close_attempts, 1)
        notes = "\n".join(getattr(exc, "__notes__", ()))
        self.assertIn("docker rm -f client did not exit after SIGKILL", notes)
        _assert_no_workers(self)

    def test_deadline_reaps_before_closing_blocked_pipes(self):
        proc = _DeadlineProc(unblock_pipes_on_terminate=False)
        close_after_termination: list[bool] = []
        for pipe in (proc.stdout, proc.stderr):
            original_close = pipe.close

            def close(original_close=original_close) -> None:
                close_after_termination.append(proc.terminated)
                original_close()

            pipe.close = close  # type: ignore[method-assign]

        result = self._run_streaming(proc, deadline=0.05, grace=0.1)

        self.assertIsInstance(result.get("error"), AssemblyTimeoutError)
        self.assertTrue(proc.terminated)
        self.assertTrue(proc.reaped)
        self.assertTrue(close_after_termination)
        self.assertTrue(all(close_after_termination))
        _assert_no_workers(self)

    def test_timeout_closes_pipes_blocked_after_terminate(self):
        # terminate() reaps the client but leaves the pipes blocked; only the
        # forced pipe closure can unblock the readers.
        proc = _DeadlineProc(unblock_pipes_on_terminate=False)
        result = self._run_streaming(proc, deadline=0.2)
        exc = result.get("error")
        self.assertIsInstance(exc, AssemblyTimeoutError)
        self.assertTrue(proc.terminated)
        self.assertTrue(proc.reaped)
        self.assertGreaterEqual(proc.stdout.close_attempts, 1)
        self.assertGreaterEqual(proc.stderr.close_attempts, 1)
        _assert_no_workers(self)

    @unittest.skipUnless(
        hasattr(signal, "pthread_kill"), "requires signal.pthread_kill"
    )
    def test_interruption_closes_pipes_blocked_after_terminate(self):
        blocked = threading.Event()
        proc = _DeadlineProc(
            unblock_pipes_on_terminate=False, stdout_blocked_event=blocked
        )

        def interrupt() -> None:
            self.assertTrue(blocked.wait(5.0))
            time.sleep(0.1)
            signal.pthread_kill(threading.main_thread().ident, signal.SIGINT)

        timer = threading.Thread(target=interrupt, daemon=True)
        timer.start()
        with mock.patch.object(
            execution_module.subprocess, "Popen", return_value=proc
        ):
            with self.assertRaises(KeyboardInterrupt):
                DockerRunExecutor().run_streaming(
                    ("docker", "run", "--rm", "alpine", "true")
                )
        timer.join(5.0)

        # The interruption hook terminated/reaped the client and closed both
        # pipes to unblock the readers before the interruption propagated.
        self.assertTrue(proc.terminated)
        self.assertTrue(proc.reaped)
        self.assertGreaterEqual(proc.stdout.close_attempts, 1)
        self.assertGreaterEqual(proc.stderr.close_attempts, 1)
        _assert_no_workers(self)

    def test_reader_failure_with_unreapable_client_is_bounded(self):
        # A reader failure triggers _terminate_and_reap; a client that
        # ignores terminate and never exits after kill must be reaped with a
        # bounded wait (recording a cleanup error) instead of hanging.
        proc = _DeadlineProc(
            stdout_fail_read=True,
            resist_terminate=True,
            never_exit=True,
        )
        result = self._run_streaming(proc, deadline=None, grace=0.3)
        exc = result.get("error")
        # The structured reader failure stays primary.
        self.assertIsInstance(exc, StreamReaderFailure)
        assert isinstance(exc, StreamReaderFailure)
        self.assertEqual(exc.stream, STREAM_STDOUT)
        # The client was terminated and SIGKILLed, but never reaped.
        self.assertTrue(proc.terminated)
        self.assertTrue(proc.killed)
        self.assertFalse(proc.reaped)
        # The failed reap is retained as bounded redacted secondary context.
        notes = "\n".join(getattr(exc, "__notes__", ()))
        self.assertIn("on_reader_failure cleanup failed", notes)
        self.assertIn("did not exit after SIGKILL", notes)
        _assert_no_workers(self)

    def test_reader_failure_closes_both_pipes_when_client_survives_kill(self):
        # stdout fails while reading; stderr blocks and stays blocked even
        # after terminate and kill (no unblock-on-terminate), so the only
        # thing that can unblock the sibling reader is closing both pipes in
        # reader-failure cleanup.  run_streaming must still return within the
        # bounded grace period with StreamReaderFailure primary and the
        # failed reap as secondary context.
        proc = _DeadlineProc(
            stdout_fail_read=True,
            resist_terminate=True,
            unblock_pipes_on_terminate=False,
            never_exit=True,
        )
        result = self._run_streaming(proc, deadline=None, grace=0.3)
        exc = result.get("error")
        self.assertIsInstance(exc, StreamReaderFailure)
        assert isinstance(exc, StreamReaderFailure)
        self.assertEqual(exc.stream, STREAM_STDOUT)
        self.assertTrue(proc.terminated)
        self.assertTrue(proc.killed)
        self.assertFalse(proc.reaped)
        # Both pipes were closed by reader-failure cleanup: the failed pipe
        # immediately plus the post-reap close of both pipes (the exception
        # path re-close adds one more idempotent attempt each).
        self.assertGreaterEqual(proc.stdout.close_attempts, 2)
        self.assertGreaterEqual(proc.stderr.close_attempts, 1)
        notes = "\n".join(getattr(exc, "__notes__", ()))
        self.assertIn("on_reader_failure cleanup failed", notes)
        self.assertIn("did not exit after SIGKILL", notes)
        _assert_no_workers(self)


class TestDaemonLifecycle(DeadlineTestCase):
    """Timeout force-removal of the named container and mutable staging."""

    def _assemble_timeout(
        self,
        *,
        cache_root: Path,
        rm_result: int = 0,
        rm_stderr: str = "",
        rm_stdout: str = "",
    ):
        proc = _DeadlineProc(stdout_data=b"working\n")
        rm_calls: list[tuple[str, ...]] = []

        def fake_popen(argv, **kwargs):
            if argv[1] == "rm":
                rm_calls.append(argv)
                return _RmProc(
                    return_code=rm_result, stdout=rm_stdout, stderr=rm_stderr
                )
            return proc

        def fake_run(argv, **kwargs):
            if argv[1] == "info":
                return subprocess.CompletedProcess(argv, 1, "", "")
            raise AssertionError(argv)

        with mock.patch.object(
            execution_module.subprocess, "Popen", side_effect=fake_popen
        ), mock.patch.object(
            execution_module, "ASSEMBLY_TOTAL_TIMEOUT_SECONDS", 0.2
        ), mock.patch.object(
            execution_module.subprocess, "run", side_effect=fake_run
        ):
            try:
                assemble(
                    validated=_validated(),
                    assembler=_assembler(),
                    cache_root=cache_root,
                    executor=DockerRunExecutor(),
                    secrets=("SUPERSECRET",),
                )
            except BaseException as exc:  # pragma: no cover - test aid
                return exc, rm_calls, proc
            self.fail("assemble did not raise")

    def test_timeout_force_removes_container_and_staging(self):
        cache_root = self._cache_root()
        exc, rm_calls, proc = self._assemble_timeout(cache_root=cache_root)
        self.assertIsInstance(exc, AssemblyTimeoutError)
        # The deterministic named container was force-removed.
        self.assertTrue(
            any(argv[1] == "rm" and argv[3].startswith("npm-assembler-")
                for argv in rm_calls)
        )
        namespace = self._namespace(cache_root)
        # Mutable staging was removed.
        self.assertEqual(
            [p for p in namespace.staging.iterdir() if p.is_dir()], []
        )
        # The opaque npm cache and empty outputs are preserved untouched.
        self.assertTrue(namespace.npm_cache.exists())
        self.assertEqual(list(namespace.outputs.iterdir()), [])
        _assert_no_workers(self)

    def test_timeout_skips_duplicate_container_cleanup_that_would_block(self):
        cache_root = self._cache_root()
        proc = _DeadlineProc(stdout_data=b"working\n")
        rm_calls: list[tuple[str, ...]] = []

        def fake_popen(argv, **kwargs):
            if argv[1] == "rm":
                rm_calls.append(argv)
                return _RmProc()
            return proc

        def fake_run(argv, **kwargs):
            if argv[1] == "info":
                return subprocess.CompletedProcess(argv, 1, "", "")
            if argv[1] != "rm":
                raise AssertionError(argv)
            rm_calls.append(argv)
            # This models the later outer cleanup call, which used to go
            # through DockerRunExecutor.run() without a subprocess timeout.
            time.sleep(1.0)
            return subprocess.CompletedProcess(argv, 0, "", "")

        started = time.monotonic()
        with mock.patch.object(
            execution_module.subprocess, "Popen", side_effect=fake_popen
        ), mock.patch.object(
            execution_module, "ASSEMBLY_TOTAL_TIMEOUT_SECONDS", 0.2
        ), mock.patch.object(
            execution_module.subprocess, "run", side_effect=fake_run
        ):
            with self.assertRaises(AssemblyTimeoutError) as ctx:
                assemble(
                    validated=_validated(),
                    assembler=_assembler(),
                    cache_root=cache_root,
                    executor=DockerRunExecutor(),
                )
        elapsed = time.monotonic() - started

        self.assertEqual(ctx.exception.reason, "assembly_timeout")
        self.assertLess(elapsed, 0.8)
        self.assertEqual(len(rm_calls), 1)
        _assert_no_workers(self)

    def test_timeout_idempotent_when_container_already_absent(self):
        cache_root = self._cache_root()
        exc, rm_calls, proc = self._assemble_timeout(
            cache_root=cache_root,
            rm_result=1,
            rm_stderr="Error response from daemon: No such container: npm-assembler-x",
        )
        self.assertIsInstance(exc, AssemblyTimeoutError)
        # Already-absent removal is idempotent success: no spurious cleanup
        # note displaces the primary timeout result.
        notes = getattr(exc, "__notes__", [])
        self.assertFalse(any("container cleanup failed" in n for n in notes))
        _assert_no_workers(self)


class TestInterruption(DeadlineTestCase):
    @unittest.skipUnless(
        hasattr(signal, "pthread_kill"), "requires signal.pthread_kill"
    )
    def test_keyboard_interrupt_reaps_before_propagating(self):
        received = threading.Event()
        blocked = threading.Event()
        delivered: list = []

        def sink(chunk) -> None:
            delivered.append(chunk)
            received.set()

        proc = _DeadlineProc(
            stdout_data=b"working\n", stdout_blocked_event=blocked
        )

        def interrupt() -> None:
            self.assertTrue(blocked.wait(5.0))
            self.assertTrue(received.wait(5.0))
            time.sleep(0.2)
            signal.pthread_kill(threading.main_thread().ident, signal.SIGINT)

        timer = threading.Thread(target=interrupt, daemon=True)
        timer.start()
        with mock.patch.object(
            execution_module.subprocess, "Popen", return_value=proc
        ):
            with self.assertRaises(KeyboardInterrupt):
                DockerRunExecutor().run_streaming(
                    ("docker", "run", "--rm", "alpine", "true"), sink=sink
                )
        timer.join(5.0)

        # Local client was terminated and reaped before propagation.
        self.assertTrue(proc.terminated)
        self.assertTrue(proc.reaped)
        # Accepted chunks were delivered before interruption.
        self.assertTrue(delivered)
        _assert_no_workers(self)

    def test_interrupt_primary_preserves_poll_failure_as_secondary_note(self):
        poll_error = OSError("SUPERSECRET poll failed")
        reached_publication = threading.Event()
        interruption_claimed = threading.Event()
        release_publication = threading.Event()
        proc = _DeadlineProc(wait_failure=poll_error)

        class _GateLock:
            def __init__(self) -> None:
                self.lock = threading.Lock()

            def __enter__(self):
                if threading.current_thread().name == "npm-deadline-supervisor":
                    reached_publication.set()
                    release_publication.wait(5.0)
                self.lock.acquire()
                if threading.current_thread().name != "npm-deadline-supervisor":
                    interruption_claimed.set()
                return self

            def __exit__(self, exc_type, exc_value, traceback) -> None:
                self.lock.release()

        class _GatedSupervisor(DeadlineSupervisor):
            def __init__(self, **kwargs) -> None:
                super().__init__(**kwargs)
                self._lock = _GateLock()  # type: ignore[assignment]

        def interrupt(on_interruption) -> None:
            self.assertTrue(reached_publication.wait(5.0))

            def release() -> None:
                self.assertTrue(interruption_claimed.wait(5.0))
                release_publication.set()

            timer = threading.Thread(target=release, daemon=True)
            timer.start()
            on_interruption()
            timer.join(5.0)
            raise KeyboardInterrupt

        with mock.patch.object(
            execution_module.subprocess, "Popen", return_value=proc
        ), mock.patch.object(
            execution_module, "DeadlineSupervisor", _GatedSupervisor
        ), mock.patch.object(
            execution_module, "collect_streams",
            side_effect=lambda **kwargs: interrupt(kwargs["on_interruption"]),
        ):
            with self.assertRaises(KeyboardInterrupt) as ctx:
                DockerRunExecutor().run_streaming(
                    ("docker", "run", "--rm", "alpine", "true"),
                    secrets=("SUPERSECRET",),
                    deadline_seconds=1.0,
                    grace_seconds=0.1,
                )

        notes = "\n".join(getattr(ctx.exception, "__notes__", ()))
        self.assertIn("supervisor wait failed (OSError): <redacted> poll failed", notes)
        self.assertNotIn("SUPERSECRET", notes)
        self.assertTrue(proc.terminated)
        self.assertTrue(proc.reaped)
        _assert_no_workers(self)

    @unittest.skipUnless(
        hasattr(signal, "pthread_kill"), "requires signal.pthread_kill"
    )
    def test_interrupt_during_supervisor_cleanup_has_one_owner(self):
        # Keep both readers blocked until the deadline supervisor reaches its
        # final pipe-close, then stop it inside docker rm -f and interrupt the
        # coordinating thread.  The deadline already claimed cleanup, so the
        # interruption must not start a second terminate/remove sequence.
        rm_started = threading.Event()
        release_rm = threading.Event()
        rm_calls: list[tuple[str, ...]] = []
        proc = _DeadlineProc(
            resist_terminate=True, unblock_pipes_on_terminate=False
        )

        def fake_popen(argv, **kwargs):
            if argv[1] != "rm":
                return proc
            rm_calls.append(argv)
            rm_started.set()
            return _BlockingRmProc(release_rm)

        def interrupt() -> None:
            self.assertTrue(rm_started.wait(5.0))
            # The supervisor has claimed cleanup and is blocked in rm; the
            # main thread remains in collect_streams because pipe closure has
            # not yet occurred.
            signal.pthread_kill(threading.main_thread().ident, signal.SIGINT)
            time.sleep(0.2)
            release_rm.set()

        timer = threading.Thread(target=interrupt, daemon=True)
        timer.start()
        with mock.patch.object(
            execution_module.subprocess, "Popen", side_effect=fake_popen
        ):
            with self.assertRaises(AssemblyTimeoutError) as ctx:
                DockerRunExecutor().run_streaming(
                    ("docker", "run", "--rm", "alpine", "true"),
                    deadline_seconds=0.1,
                    container_name="npm-assembler-race",
                    grace_seconds=0.5,
                )
        timer.join(5.0)

        # The supervisor observed expiry and claimed cleanup first, so the
        # timeout remains primary even though SIGINT arrived during cleanup.
        # The cleanup outlives one grace interval (rm + graceful reap), so
        # joining with the complete cleanup budget is required before this
        # result can propagate without a live supervisor.
        self.assertEqual(ctx.exception.reason, "assembly_timeout")
        notes = "\n".join(getattr(ctx.exception, "__notes__", ()))
        self.assertIn("also interrupted by KeyboardInterrupt", notes)
        self.assertEqual(len(rm_calls), 1)
        self.assertTrue(proc.terminated)
        self.assertTrue(proc.killed)
        self.assertTrue(proc.reaped)
        _assert_no_workers(self)

    @unittest.skipUnless(
        hasattr(signal, "pthread_kill"), "requires signal.pthread_kill"
    )
    def test_interruption_hook_control_flow_failure_is_secondary(self):
        # A hook that raises its own KeyboardInterrupt/SystemExit must never
        # displace the original interruption: it is recorded as a bounded,
        # redacted note only after readers and the dispatcher are finalized.
        for hook_exc in (
            SystemExit(7),
            KeyboardInterrupt("hook re-interrupted"),
        ):
            with self.subTest(hook=type(hook_exc).__name__):
                blocked = threading.Event()
                unblock = threading.Event()

                def stdout_read(n: int) -> bytes:
                    blocked.set()
                    unblock.wait(5.0)
                    return b""

                def stderr_read(n: int) -> bytes:
                    unblock.wait(5.0)
                    return b""

                def sink(chunk) -> None:
                    pass

                def on_interruption() -> None:
                    # Unblock the readers so the coordinator can join them,
                    # then raise a control-flow exception of our own.
                    unblock.set()
                    raise hook_exc

                def interrupt() -> None:
                    self.assertTrue(blocked.wait(5.0))
                    time.sleep(0.1)
                    signal.pthread_kill(
                        threading.main_thread().ident, signal.SIGINT
                    )

                timer = threading.Thread(target=interrupt, daemon=True)
                timer.start()
                with self.assertRaises(KeyboardInterrupt) as ctx:
                    collect_streams(
                        stdout_read=stdout_read,
                        stderr_read=stderr_read,
                        sink=sink,
                        on_interruption=on_interruption,
                    )
                timer.join(5.0)

                # The original (message-less SIGINT) interruption is what
                # propagates; the hook's control-flow exception is only a
                # bounded, redacted secondary note.
                self.assertEqual(str(ctx.exception), "")
                notes = "\n".join(getattr(ctx.exception, "__notes__", ()))
                self.assertIn(
                    f"interruption cleanup failed ({type(hook_exc).__name__})",
                    notes,
                )
                _assert_no_workers(self)

    @unittest.skipUnless(
        hasattr(signal, "pthread_kill"), "requires signal.pthread_kill"
    )
    def test_assemble_interrupt_removes_container_and_staging(self):
        cache_root = self._cache_root()
        blocked = threading.Event()
        proc = _DeadlineProc(
            stdout_data=b"working\n", stdout_blocked_event=blocked
        )
        rm_calls: list[tuple[str, ...]] = []

        def fake_popen(argv, **kwargs):
            if argv[1] == "rm":
                rm_calls.append(argv)
                return _RmProc()
            return proc

        def fake_run(argv, **kwargs):
            if argv[1] == "info":
                return subprocess.CompletedProcess(argv, 1, "", "")
            raise AssertionError(argv)

        def interrupt() -> None:
            self.assertTrue(blocked.wait(5.0))
            time.sleep(0.2)
            signal.pthread_kill(threading.main_thread().ident, signal.SIGINT)

        timer = threading.Thread(target=interrupt, daemon=True)
        timer.start()
        with mock.patch.object(
            execution_module.subprocess, "Popen", side_effect=fake_popen
        ), mock.patch.object(
            execution_module.subprocess, "run", side_effect=fake_run
        ):
            with self.assertRaises(KeyboardInterrupt):
                assemble(
                    validated=_validated(),
                    assembler=_assembler(),
                    cache_root=cache_root,
                    executor=DockerRunExecutor(),
                )
        timer.join(5.0)

        # Container force-removed and staging removed before propagation.
        self.assertTrue(
            any(argv[1] == "rm" and argv[3].startswith("npm-assembler-")
                for argv in rm_calls)
        )
        namespace = self._namespace(cache_root)
        self.assertEqual(
            [p for p in namespace.staging.iterdir() if p.is_dir()], []
        )
        _assert_no_workers(self)

    @unittest.skipUnless(
        hasattr(signal, "pthread_kill"), "requires signal.pthread_kill"
    )
    def test_assemble_interrupt_cancels_supervisor_with_unreapable_client(self):
        # assemble() activates the real 1800-second deadline supervisor; the
        # Docker client resists terminate and remains unreapable even after
        # SIGKILL.  Interruption must signal the supervisor's cancellation
        # event so the supervisor stops promptly (instead of staying blocked
        # in one long proc.wait), then propagate the original KeyboardInterrupt
        # within the bounded cleanup period without leaking any worker thread.
        cache_root = self._cache_root()
        blocked = threading.Event()
        proc = _DeadlineProc(
            stdout_data=b"working\n",
            stdout_blocked_event=blocked,
            resist_terminate=True,
            never_exit=True,
        )
        rm_calls: list[tuple[str, ...]] = []

        def fake_popen(argv, **kwargs):
            if argv[1] == "rm":
                rm_calls.append(argv)
                return _RmProc()
            return proc

        def fake_run(argv, **kwargs):
            if argv[1] == "info":
                return subprocess.CompletedProcess(argv, 1, "", "")
            raise AssertionError(argv)

        def interrupt() -> None:
            self.assertTrue(blocked.wait(5.0))
            time.sleep(0.2)
            signal.pthread_kill(threading.main_thread().ident, signal.SIGINT)

        timer = threading.Thread(target=interrupt, daemon=True)
        timer.start()
        started = time.monotonic()
        with mock.patch.object(
            execution_module.subprocess, "Popen", side_effect=fake_popen
        ), mock.patch.object(
            execution_module.subprocess, "run", side_effect=fake_run
        ):
            with self.assertRaises(KeyboardInterrupt) as ctx:
                assemble(
                    validated=_validated(),
                    assembler=_assembler(),
                    cache_root=cache_root,
                    executor=DockerRunExecutor(),
                )
        elapsed = time.monotonic() - started
        timer.join(5.0)

        # The original (message-less SIGINT) interruption is primary: it was
        # not converted into an AssemblyTimeoutError, and it propagated well
        # within the cleanup bound instead of waiting out the 1800s deadline.
        self.assertEqual(str(ctx.exception), "")
        self.assertLess(elapsed, 10.0)

        # The client was terminated and SIGKILLed but never reaped; the
        # failed reap is retained as bounded redacted secondary context.
        self.assertTrue(proc.terminated)
        self.assertTrue(proc.killed)
        self.assertFalse(proc.reaped)
        self.assertGreaterEqual(proc.stdout.close_attempts, 1)
        self.assertGreaterEqual(proc.stderr.close_attempts, 1)
        notes = "\n".join(getattr(ctx.exception, "__notes__", ()))
        self.assertIn("interruption cleanup failed", notes)
        self.assertIn("did not exit after SIGKILL", notes)
        # Container force-removed and staging removed before propagation.
        self.assertTrue(
            any(argv[1] == "rm" and argv[3].startswith("npm-assembler-")
                for argv in rm_calls)
        )
        namespace = self._namespace(cache_root)
        self.assertEqual(
            [p for p in namespace.staging.iterdir() if p.is_dir()], []
        )
        _assert_no_workers(self)


class TestNonzeroExit(DeadlineTestCase):
    def test_nonzero_exit_has_no_network_classification(self):
        cache_root = self._cache_root()
        executor = _RecordingExecutor(
            return_code=1,
            stderr="npm ERR! request to https://registry.npmjs.org/a failed: ETIMEDOUT",
        )
        with self.assertRaises(LockedNpmError) as ctx:
            assemble(
                validated=_validated(),
                assembler=_assembler(),
                cache_root=cache_root,
                executor=executor,
            )
        # No network-specific reason is inferred from the npm exit code or
        # human-readable output.
        self.assertEqual(ctx.exception.reason, "npm_exit_nonzero")
        namespace = self._namespace(cache_root)
        self.assertEqual(list(namespace.outputs.iterdir()), [])
        self.assertEqual(
            [p for p in namespace.staging.iterdir() if p.is_dir()], []
        )

    def test_retry_exhaustion_is_structured_and_bounded(self):
        cache_root = self._cache_root()
        huge = "x" * (128 * 1024)
        executor = _RecordingExecutor(
            return_code=1,
            stderr=f"npm ERR! fetch failed after retries SUPERSECRET {huge}",
        )
        with self.assertRaises(LockedNpmError) as ctx:
            assemble(
                validated=_validated(),
                assembler=_assembler(),
                cache_root=cache_root,
                executor=executor,
                secrets=("SUPERSECRET",),
            )
        self.assertEqual(ctx.exception.reason, "npm_exit_nonzero")
        detail = ctx.exception.detail
        self.assertNotIn("SUPERSECRET", detail)
        self.assertLessEqual(len(detail.encode("utf-8")), 64 * 1024 + 128)


class TestStoragePreservation(DeadlineTestCase):
    def _write_tree(self, root: Path, name: str, version: str, marker: bytes):
        pkg = root / "node_modules" / name
        pkg.mkdir(parents=True, exist_ok=True)
        (pkg / "package.json").write_text(json.dumps({"name": name, "version": version}))
        (pkg / "marker.txt").write_bytes(marker)

    def test_failed_path_preserves_cache_and_prior_outputs(self):
        cache_root = self._cache_root()
        namespace = self._namespace(cache_root)
        # A prior immutable published environment.
        prior_validated = _validated({"prior": "1.0.0"})
        prior_tree = cache_root.parent / "prior-tree"
        prior_tree.mkdir()
        self._write_tree(prior_tree, "prior", "1.0.0", b"prior-marker")
        prior_identity = compute_assembler_input_identity(
            prior_validated, _assembler()
        )
        prior = publish_environment(
            validated=prior_validated,
            tree_root=prior_tree,
            namespace=namespace,
            input_identity=prior_identity,
        )
        # An opaque npm cache entry that must survive a failed run.
        (namespace.npm_cache / "tarball.tgz").write_bytes(b"cached-bytes")

        executor = _RecordingExecutor(return_code=1, stderr="boom")
        with self.assertRaises(LockedNpmError):
            assemble(
                validated=_validated(),
                assembler=_assembler(),
                cache_root=cache_root,
                executor=executor,
            )

        # The opaque npm cache entry and the prior immutable output survive.
        self.assertEqual(
            (namespace.npm_cache / "tarball.tgz").read_bytes(), b"cached-bytes"
        )
        self.assertTrue(prior.environment_root.exists())
        self.assertEqual(
            (prior.environment_root / "node_modules" / "prior" / "marker.txt").read_bytes(),
            b"prior-marker",
        )
        # No partial output was published: only the prior output remains.
        self.assertEqual(
            sorted(p.name for p in namespace.outputs.iterdir()),
            [prior.output_identity],
        )

    def test_failed_path_never_chmods_authoritative_cache(self):
        cache_root = self._cache_root()
        namespace = self._namespace(cache_root)
        cache_entry = namespace.npm_cache / "tarball.tgz"
        cache_entry.write_bytes(b"cached-bytes")
        cache_entry.chmod(0o444)

        executor = _RecordingExecutor(return_code=1, stderr="boom")
        with self.assertRaises(LockedNpmError):
            assemble(
                validated=_validated(),
                assembler=_assembler(),
                cache_root=cache_root,
                executor=executor,
            )
        # The authoritative cache entry is neither deleted nor chmod-ed.
        self.assertTrue(cache_entry.exists())
        self.assertEqual(cache_entry.read_bytes(), b"cached-bytes")
        self.assertEqual(cache_entry.stat().st_mode & 0o777, 0o444)


class TestAbandonedStaging(DeadlineTestCase):
    def _staging_name(self):
        return compute_assembler_input_identity(
            _validated(), _assembler()
        ).digest

    def test_abandoned_staging_is_replaced_not_adopted(self):
        cache_root = self._cache_root()
        namespace = self._namespace(cache_root)
        staging_name = self._staging_name()
        # Abandoned mutable staging left by a prior interrupted run.
        abandoned = namespace.staging / staging_name
        abandoned.mkdir()
        (abandoned / "stale-marker.txt").write_text("abandoned")

        result = assemble_environment(
            validated=_validated(),
            assembler=_assembler(),
            cache_root=cache_root,
            executor=_PopulatingExecutor(),
        )
        # The abandoned staging was removed, never adopted as output.
        self.assertFalse((abandoned / "stale-marker.txt").exists())
        self.assertIsNotNone(result.environment_root)
        self.assertFalse(
            (result.environment_root / "stale-marker.txt").exists()
        )

    def test_abandoned_staging_symlink_is_unlinked_not_followed(self):
        cache_root = self._cache_root()
        namespace = self._namespace(cache_root)
        staging_name = self._staging_name()
        external = cache_root.parent / "external"
        external.mkdir()
        (external / "keep.txt").write_text("keep")
        (namespace.staging / staging_name).symlink_to(
            external, target_is_directory=True
        )

        result = assemble_environment(
            validated=_validated(),
            assembler=_assembler(),
            cache_root=cache_root,
            executor=_PopulatingExecutor(),
        )
        # The symlink was unlinked (no follow), and the external target is
        # untouched.
        self.assertFalse((namespace.staging / staging_name).exists())
        self.assertEqual((external / "keep.txt").read_text(), "keep")
        self.assertIsNotNone(result.environment_root)


if __name__ == "__main__":
    unittest.main()
