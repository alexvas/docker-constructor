"""Phase 6BC RED: domain collection delegates ownership without raw replay."""

import functools
import subprocess
import sys
import threading
from unittest import mock

import pytest

from docker.filesystem.streams import OwnedStream
from docker.process import OwnedProcess
from docker.npm_environment import execution, publication
from docker.npm_environment.execution import DockerRunExecutor
from docker.npm_environment.streaming import (
    REDACTED,
    RETAINED_CAPTURE_ATTR,
    STREAM_STDOUT,
    STREAM_STDERR,
    READER_FAILURE_DETAIL_BYTES,
    RedactingStream,
)
from tests.test_process_failure_snapshot import Child
from tests.test_process_stream_outcomes import Child as OutcomeChild, Pipe
from tests.test_npm_environment_phase3_deadline import _validated, _assembler


@pytest.mark.parametrize("join_defect", [False, True])
def test_reader_oserror_projection_must_not_disguise_join_oserror_defect(join_defect):
    """A retained READ_FAILED alone cannot identify the winning error's source."""
    from docker.npm_environment.streaming import StreamReaderFailure

    reader_error = OSError("reader operation failed")
    join_error = OSError("join operation failed")
    child = OutcomeChild(
        stdout=Pipe([b"stdout"], error=reader_error), stderr=Pipe([b"stderr"])
    )
    original_join = OwnedProcess._join_reader
    original_supervisor = OwnedProcess._start_supervisor
    owners = []
    injected = False

    def supervise(owner):
        owners.append(owner)
        original_supervisor(owner)

    def join(owner, index, timeout):
        nonlocal injected
        if join_defect and index == 0 and not injected:
            injected = True
            raise join_error
        original_join(owner, index, timeout)

    try:
        with (
            mock.patch.object(execution.subprocess, "Popen", return_value=child),
            mock.patch.object(OwnedProcess, "_start_supervisor", supervise),
            mock.patch.object(OwnedProcess, "_join_reader", join),
        ):
            with pytest.raises(Exception) as caught:
                DockerRunExecutor().run_streaming(("client",), grace_seconds=0.1)
        assert len(owners) == 1
        status = owners[0].cleanup_status
        assert status.failures[0].error is (join_error if join_defect else reader_error)
        assert any(f.error is reader_error for f in status.failures)
        if join_defect:
            assert caught.value is join_error
            assert not isinstance(caught.value, StreamReaderFailure)
        else:
            assert isinstance(caught.value, StreamReaderFailure)
            assert caught.value.stream == STREAM_STDOUT
        assert child.stdout.close_attempts == child.stderr.close_attempts == 1
    finally:
        for owner in owners:
            owner.shutdown()
            assert owner.wait_cleanup(1).complete
        assert child.stdout.close_attempts == child.stderr.close_attempts == 1


@pytest.mark.parametrize("source", ["release", "join"])
def test_timeout_projection_preserves_unexpected_join_authority_but_not_ordinary_release(
    source,
):
    from docker.npm_environment.errors import AssemblyTimeoutError
    from tests.test_npm_environment_phase3_deadline import _DeadlineProc, _DeadlinePipe

    error = OSError("cleanup operation failed")
    child = _DeadlineProc()
    if source == "release":
        child.stdout = _DeadlinePipe(close_failure=error)
    owners, snapshots = [], []
    original_supervisor = OwnedProcess._start_supervisor
    original_join = OwnedProcess._join_reader
    original_stream = OwnedProcess.stream
    injected = False

    def stream(owner, *args, **kwargs):
        value = original_stream(owner, *args, **kwargs)
        snapshots.append(value)
        return value

    def supervise(owner):
        owners.append(owner)
        original_supervisor(owner)

    def join(owner, index, timeout):
        nonlocal injected
        original_join(owner, index, timeout)
        if source == "join" and index == 0 and not injected:
            injected = True
            raise error

    try:
        with (
            mock.patch.object(execution.subprocess, "Popen", return_value=child),
            mock.patch.object(OwnedProcess, "_start_supervisor", supervise),
            mock.patch.object(OwnedProcess, "_join_reader", join),
            mock.patch.object(
                OwnedProcess, "stream", autospec=True, side_effect=stream
            ),
        ):
            with pytest.raises(
                AssemblyTimeoutError if source == "release" else OSError
            ) as caught:
                DockerRunExecutor().run_streaming(
                    ("client",), deadline_seconds=0.02, grace_seconds=0.1
                )
        assert len(owners) == 1
        assert owners[0].cleanup_status.failures[0].error is error
        assert len(snapshots) == 1 and snapshots[0].timed_out
        assert snapshots[0].lifecycle.failures[0].phase.name == source.upper()
        assert not hasattr(error, "process_result")
        if source == "join":
            assert caught.value is error
        else:
            notes = "\n".join(getattr(caught.value, "__notes__", ()))
            assert notes.count("stdout release failed: cleanup operation failed") == 1
        assert child.stdout.close_attempts == child.stderr.close_attempts == 1
    finally:
        for owner in owners:
            owner.shutdown()
            assert owner.wait_cleanup(1).complete


def test_streaming_delegates_once_and_preserves_domain_output_and_launch_callback():
    child = Child()
    owners = []
    original_launch, original_stream = OwnedProcess.launch, OwnedProcess.stream

    def launch(*args, **kwargs):
        owner = original_launch(*args, **kwargs)
        owners.append(owner)
        return owner

    def stream(owner, *args, **kwargs):
        assert callable(kwargs.get("on_stream_end")), (
            "independent finalization needs retained outcomes"
        )
        return original_stream(owner, *args, **kwargs)

    callback = mock.Mock()
    with (
        mock.patch(
            "docker.npm_environment.execution.subprocess.Popen", return_value=child
        ),
        mock.patch.object(OwnedProcess, "launch", side_effect=launch),
        mock.patch.object(
            OwnedProcess, "stream", autospec=True, side_effect=stream
        ) as collect,
    ):
        result = DockerRunExecutor().run_streaming(
            ("client",), secrets=("retained",), grace_seconds=0.1, on_launched=callback
        )
    assert len(owners) == 1 and collect.call_count == 1
    assert collect.call_args.args[0] is owners[0]
    assert result.argv == ("client",) and result.return_code == 7
    assert (result.stdout, result.stderr) == ("<redacted> stdout", "<redacted> stderr")
    callback.assert_called_once_with()
    assert owners[0].wait_cleanup(0.1).complete
    assert child.stdout.attempts == child.stderr.attempts == 1


def test_clean_stdout_eof_then_stderr_failure_finalizes_independently_without_raw_replay():
    clean, stderr_fed, release = (threading.Event() for _ in range(3))
    read_error = OSError("https://private.invalid/" + "token" * 2000)
    child = OutcomeChild(
        stdout=Pipe([b"to"]), stderr=Pipe([b"to"], gate=release, error=read_error)
    )
    streams, finishes, chunks, raised, snapshots = {}, [], [], [], []
    original = OwnedProcess.stream

    class DomainStream(RedactingStream):
        def __init__(self, tag):
            super().__init__(("token",))
            self.tag = tag

        def feed_bytes(self, data):
            result = super().feed_bytes(data)
            if self.tag == STREAM_STDERR:
                stderr_fed.set()
            return result

        def finish(self, *, abort=False):
            finishes.append((self.tag, abort))
            result = super().finish(abort=abort)
            if self.tag == STREAM_STDOUT:
                clean.set()
            return result

    def make(tag):
        streams[tag] = DomainStream(tag)
        return streams[tag]

    def stream(owner, *args, **kwargs):
        value = original(owner, *args, **kwargs)
        snapshots.append(value)
        return value

    def run():
        try:
            DockerRunExecutor().run_streaming(
                ("client",),
                secrets=("token",),
                sink=chunks.append,
                stream_factory=make,
                grace_seconds=0.1,
            )
        except BaseException as exc:
            raised.append(exc)

    worker = threading.Thread(target=run, name="test-domain-independent-eof")
    try:
        with (
            mock.patch(
                "docker.npm_environment.execution.subprocess.Popen", return_value=child
            ),
            mock.patch.object(
                OwnedProcess, "stream", autospec=True, side_effect=stream
            ) as collect,
        ):
            worker.start()
            assert clean.wait(1), "clean stdout must finalize before sibling failure"
            assert stderr_fed.wait(1), "prefix must be assigned before abort"
            assert not child.stderr.read_finished.is_set()
            assert streams[STREAM_STDOUT].tail() == "to"
            release.set()
            worker.join(2)
            assert not worker.is_alive() and len(raised) == 1
            assert finishes == [(STREAM_STDOUT, False), (STREAM_STDERR, True)]
            retained = getattr(raised[0], RETAINED_CAPTURE_ATTR)
            assert (retained.stdout_tail, retained.stderr_tail) == ("to", REDACTED)
            assert [(chunk.stream, chunk.text) for chunk in chunks] == [
                (STREAM_STDOUT, "to"),
                (STREAM_STDERR, REDACTED),
            ]
            public = str(raised[0]) + "\n".join(getattr(raised[0], "__notes__", ()))
            assert (
                "token" not in public
                and "private.invalid" not in public
                and "https://" not in public
            )
            assert len(str(raised[0]).encode()) <= READER_FAILURE_DETAIL_BYTES + 512
            assert collect.call_count == 1 and len(snapshots) == 1
            assert snapshots[0].stdout == b"to" and snapshots[0].stderr == b"to"
            assert child.stdout.close_attempts == child.stderr.close_attempts == 1
            before = (
                tuple(chunks),
                tuple(finishes),
                retained.stdout_tail,
                retained.stderr_tail,
            )
            # No collector resubscription/raw tail projection is needed to
            # finish the adapter; delivery has already finalized both streams.
            assert before == (
                tuple(chunks),
                tuple(finishes),
                retained.stdout_tail,
                retained.stderr_tail,
            )
    finally:
        release.set()
        if worker.ident is not None:
            worker.join(2)
        assert not worker.is_alive()


def test_incomplete_streaming_keeps_abort_safe_tails_and_sanitized_delivered_notes():
    release, reader_stopped, fed = (threading.Event() for _ in range(3))
    read_error = OSError("https://private.invalid/" + "token" * 2000)
    late_error = OSError("https://late.invalid/" + "token" * 2000)
    original_supervisor = OwnedProcess._start_supervisor
    original_stream, original_close = OwnedProcess.stream, OwnedStream.close
    owners, snapshots, chunks = [], [], []
    reads = 0

    class DomainStream(RedactingStream):
        def __init__(self, tag):
            super().__init__(("token",))
            self.tag = tag

        def feed_bytes(self, data):
            result = super().feed_bytes(data)
            if self.tag == STREAM_STDOUT:
                fed.set()
            return result

    def supervise(owner):
        owners.append(owner)
        original_supervisor(owner)

    def read(owner, index):
        nonlocal reads
        if index == 0:
            reads += 1
            if reads == 1:
                return b"to"
            assert release.wait(5), "test-controlled streaming reader leaked"
            reader_stopped.set()
            return b""
        assert fed.wait(1), "stdout prefix must be assigned before sibling failure"
        raise read_error

    def close(stream_owner):
        if stream_owner.label == "stdout":
            assert reader_stopped.is_set(), "live descriptor reader lost its owner"
        original_close(stream_owner)
        if stream_owner.label == "stdout":
            raise late_error

    def stream(owner, *args, **kwargs):
        value = original_stream(owner, *args, **kwargs)
        snapshots.append(value)
        return value

    try:
        with (
            mock.patch.object(OwnedProcess, "_read_chunk", read),
            mock.patch.object(OwnedProcess, "_start_supervisor", supervise),
            mock.patch.object(
                OwnedProcess, "stream", autospec=True, side_effect=stream
            ),
            mock.patch.object(OwnedStream, "close", close),
        ):
            with pytest.raises(Exception) as caught:
                DockerRunExecutor().run_streaming(
                    (sys.executable, "-c", "import time; time.sleep(30)"),
                    secrets=("token",),
                    stream_factory=DomainStream,
                    sink=chunks.append,
                    grace_seconds=0.05,
                    deadline_seconds=0.05,
                )
            assert len(owners) == len(snapshots) == 1
            owner, snapshot = owners[0], snapshots[0]
            assert not owner.cleanup_status.complete and not snapshot.lifecycle.complete
            assert not owner._proc.stdout.closed and owner._proc.returncode is not None
            error = caught.value
            retained = getattr(error, RETAINED_CAPTURE_ATTR)
            assert retained.stdout_tail == REDACTED
            assert [(chunk.stream, chunk.text) for chunk in chunks] == [
                (STREAM_STDOUT, REDACTED)
            ]
            public = str(error) + "\n".join(getattr(error, "__notes__", ()))
            assert (
                "token" not in public
                and "https://" not in public
                and ".invalid" not in public
            )
            assert "incomplete" in public.lower()
            notes, delivered = tuple(getattr(error, "__notes__", ())), tuple(chunks)
            release.set()
            assert owner.wait_cleanup(1).complete
            assert any(f.error is late_error for f in owner.cleanup_status.failures)
            assert not snapshot.lifecycle.complete and snapshot.stdout == b"to"
            assert tuple(getattr(error, "__notes__", ())) == notes
            assert tuple(chunks) == delivered and retained.stdout_tail == REDACTED
    finally:
        release.set()
        for owner in owners:
            owner.shutdown()
            assert owner.wait_cleanup(1).complete


@pytest.mark.parametrize("assembly", [False, True])
def test_local_failure_does_not_skip_removal_or_staging_cleanup_and_notes_are_safe(
    tmp_path, assembly, request
):
    primary = ValueError("domain operation")
    terminate_error = OSError("https://private.invalid/" + "token" * 2000)
    helper_close = OSError("https://helper.invalid/" + "token" * 2000)
    main, helper = Child(), Child(helper_close)
    helper_launched = False

    def release_unlaunched_fixture():
        # These descriptor-less fixture objects remain caller-owned when the
        # RED adapter skips removal. This is not a rejected-adoption mock or
        # production release fallback; launched pipes keep their existing owner.
        if not helper_launched:
            for pipe in (helper.stdout, helper.stderr):
                fixture_owner = OwnedStream(pipe, label="unlaunched helper fixture")
                try:
                    fixture_owner.close()
                except OSError as exc:
                    assert exc is helper_close
                fixture_owner.close()
                assert fixture_owner.released

    request.addfinalizer(release_unlaunched_fixture)
    main.returncode = None
    helper.returncode = 0
    order = []
    cleanup = execution._StreamingCleanupOutcome()

    def terminate():
        order.append("terminate")
        raise terminate_error

    def wait(timeout=None):
        if timeout == 0 and main.returncode is None:
            raise subprocess.TimeoutExpired("client", 0)
        order.append("main-wait")
        main.returncode = 0
        return 0

    main.terminate, main.wait = terminate, wait

    def factory(argv, **kwargs):
        nonlocal helper_launched
        if tuple(argv[:3]) == ("docker", "rm", "-f"):
            order.append("remove")
            helper_launched = True
            return helper
        return main

    def fail_launch():
        raise primary

    class Executor(DockerRunExecutor):
        @functools.wraps(DockerRunExecutor.run_streaming)
        def run_streaming(self, argv, **kwargs):
            nonlocal cleanup
            cleanup = kwargs.get("cleanup_outcome", cleanup)
            original = kwargs.get("on_launched")

            def fail_after_launch():
                if original is not None:
                    original()
                fail_launch()

            kwargs["on_launched"] = fail_after_launch
            kwargs["grace_seconds"] = 0.1
            kwargs["cleanup_outcome"] = cleanup
            return super().run_streaming(argv, **kwargs)

    def run(argv, **kwargs):
        order.append("outer-remove")
        return subprocess.CompletedProcess(argv, 0, "", "")

    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    with (
        mock.patch.object(execution.subprocess, "Popen", side_effect=factory),
        mock.patch.object(execution.subprocess, "run", side_effect=run),
    ):
        with pytest.raises(
            execution.LockedNpmError if assembly else ValueError
        ) as caught:
            if assembly:
                execution.assemble(
                    validated=_validated(),
                    assembler=_assembler(),
                    cache_root=cache_root,
                    executor=Executor(),
                    secrets=("token",),
                    uid=1000,
                    gid=1000,
                )
            else:
                DockerRunExecutor().run_streaming(
                    ("client",),
                    container_name="named-container",
                    secrets=("token",),
                    on_launched=fail_launch,
                    grace_seconds=0.1,
                    cleanup_outcome=cleanup,
                )
    if assembly:
        assert caught.value.reason == "executor_failure"
        assert caught.value.__cause__ is None
    else:
        assert caught.value is primary
    assert order == ["terminate", "remove", "main-wait"]
    assert cleanup.container_removal_attempted.is_set()
    assert main.stdout.attempts == main.stderr.attempts == 1
    assert helper.stdout.attempts == helper.stderr.attempts == 1
    public = str(primary) + "\n".join(getattr(primary, "__notes__", ()))
    assert (
        "token" not in public and "https://" not in public and ".invalid" not in public
    )
    assert all(
        len(note.encode()) <= READER_FAILURE_DETAIL_BYTES + 512
        for note in getattr(primary, "__notes__", ())
    )
    assert getattr(primary, "__notes__", ())
    if assembly:
        assert tuple(getattr(caught.value, "__notes__", ())) == tuple(
            getattr(primary, "__notes__", ())
        )
        rendered = str(caught.value) + "\n".join(getattr(caught.value, "__notes__", ()))
        assert (
            "token" not in rendered
            and "https://" not in rendered
            and ".invalid" not in rendered
        )
        namespace = publication.prepare_assembler_namespace(
            cache_root, _assembler().digest
        )
        assert list(namespace.staging.iterdir()) == []
        assert namespace.npm_cache.exists() and list(namespace.outputs.iterdir()) == []


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize(
    "kind", [KeyboardInterrupt, SystemExit, GeneratorExit, BaseException]
)
def test_assembly_preserves_every_control_flow_family_and_cleans_staging(
    tmp_path, streaming, kind
):
    error = kind("control flow")
    calls = []

    class Executor:
        def run(self, argv):
            calls.append(argv)
            if argv[1] == "run":
                raise error
            return execution.ProcessResult(argv, 0)

    executor = Executor()
    if streaming:

        def run_streaming(argv, *, on_launched=None, **kwargs):
            calls.append(argv)
            on_launched()
            raise error

        executor.run_streaming = run_streaming
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    with pytest.raises(BaseException) as caught:
        execution.assemble(
            validated=_validated(),
            assembler=_assembler(),
            cache_root=cache_root,
            executor=executor,
            uid=1000,
            gid=1000,
        )
    assert caught.value is error and not hasattr(error, "process_result")
    assert sum(argv[1] == "rm" for argv in calls) == 1
    namespace = publication.prepare_assembler_namespace(cache_root, _assembler().digest)
    assert (
        list(namespace.staging.iterdir()) == []
        and list(namespace.outputs.iterdir()) == []
    )


def test_domain_end_failure_is_safely_reported_without_retention_retry_or_read_reclassification(
    caplog,
):
    child = Child()
    error = ValueError("domain finish https://private.invalid/" + "token" * 2000)
    results, attempts = [], []
    original = OwnedProcess.stream

    class DomainStream(RedactingStream):
        def __init__(self, tag):
            super().__init__(())
            self.tag = tag

        def finish(self, *, abort=False):
            attempts.append((self.tag, abort))
            super().finish(abort=abort)
            raise error

    def stream(owner, *args, **kwargs):
        result = original(owner, *args, **kwargs)
        results.append(result)
        return result

    with (
        caplog.at_level("WARNING"),
        mock.patch(
            "docker.npm_environment.execution.subprocess.Popen", return_value=child
        ),
        mock.patch(
            "docker.npm_environment.streaming.collect_streams",
            side_effect=AssertionError("legacy collector used"),
        ),
        mock.patch.object(OwnedProcess, "stream", autospec=True, side_effect=stream),
    ):
        try:
            DockerRunExecutor().run_streaming(
                ("client",),
                secrets=("token",),
                stream_factory=DomainStream,
                grace_seconds=0.1,
            )
        except Exception:
            # The adapter may map a domain finalization error through its
            # existing channel; generic end callbacks must still be isolated.
            pass
    assert len(results) == 1
    assert not hasattr(results[0], "callback_errors")
    assert len(caplog.records) == 2
    assert all(
        record.exc_info is None and record.stack_info is None
        for record in caplog.records
    )
    assert (
        "token" not in caplog.text
        and "https://" not in caplog.text
        and ".invalid" not in caplog.text
    )
    assert all(
        len(record.getMessage().encode()) <= READER_FAILURE_DETAIL_BYTES + 512
        for record in caplog.records
    )
    assert len(attempts) == 2 and set(attempts) == {
        (STREAM_STDOUT, False),
        (STREAM_STDERR, False),
    }
    assert all(terminal.name == "EOF" for _, terminal in results[0].stream_outcomes)
    assert child.stdout.attempts == child.stderr.attempts == 1
