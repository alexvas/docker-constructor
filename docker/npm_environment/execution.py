"""Docker execution boundary for the locked npm assembler.

``assemble`` is the effectful assembly operation: it rechecks the validated
input and assembler bindings before any filesystem effect, prepares an
owner-private staging workspace, writes the read-only lockfile input, renders
the deterministic run vector (including any resolved credential-free
corporate proxy/trust policy), and runs it through an injected executor.  A
nonzero container exit becomes a structured :class:`LockedNpmError` with
redacted stdout/stderr.  Every ``BaseException`` path — interruption,
executor failure, or npm failure — force-removes the container and removes
the staging workspace while preserving any prior committed environments.

Configured proxy endpoints and trust paths are never persisted: the
successful :class:`AssemblyRun` stores only redacted vector/argv/log copies,
and every failure detail is redacted before it is raised or attached.
"""

from __future__ import annotations

import contextlib
import dataclasses
import inspect
import os
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Callable, Protocol, Sequence

from .assembler import (
    ASSEMBLY_TOTAL_TIMEOUT_SECONDS,
    EXIT_NODE_VERSION_MISMATCH,
    EXIT_NPM_VERSION_MISMATCH,
)
from .errors import AssemblyTimeoutError, LockedNpmError
from .identity import (
    ASSEMBLER_DIGEST_PREFIX_LENGTH,
    AssemblerIdentity,
    compute_assembler_input_identity,
)
from .lifecycle import (
    DeadlineSupervisor,
    LifecyclePolicy,
    reap_process,
    run_captured,
    terminate_and_reap,
)
from .model import ValidatedAssemblyInput
from .network import CorporateNetworkPolicy
from .observability import NULL_ACTIVITY, AssemblyActivity
from .run_vector import (
    DockerRunVector,
    Mount,
    recheck_assembler_bindings,
    render_docker_argv,
    render_run_vector,
)
from .storage import (
    AssemblerNamespace,
    prepare_assembler_namespace,
    prepare_staging_workspace,
    remove_staging_workspace,
)
from .streaming import (
    READER_FAILURE_DETAIL_BYTES,
    REDACTED,
    DiagnosticSink,
    STREAM_STDERR,
    STREAM_STDOUT,
    StreamingCapture,
    collect_streams,
    redact_tail,
    redact_text,
)

#: Control-flow exceptions that must propagate unchanged from the executor
#: boundary (never converted into a structured assembler failure).
_CONTROL_FLOW_EXCEPTIONS = (KeyboardInterrupt, SystemExit, GeneratorExit)

#: How often the deadline supervisor wakes to check cancellation and the
#: remaining deadline (instead of blocking in one long ``proc.wait``).
_SUPERVISOR_POLL_SECONDS = 0.1


def redact(text: str, secrets: Sequence[str]) -> str:
    """Replace every non-empty *secret* in *text* with ``<redacted>``.

    Deterministic and caller-order-independent: at the leftmost position
    where any secret matches, the longest complete match is replaced with
    exactly one marker.
    """
    return redact_text(text, secrets)


def redact_docker_argv(
    argv: Sequence[str], secrets: Sequence[str]
) -> tuple[str, ...]:
    """Return a display-safe copy of *argv* with every secret redacted."""
    return tuple(redact(token, secrets) for token in argv)


def redact_run_vector(
    vector: DockerRunVector, secrets: Sequence[str]
) -> DockerRunVector:
    """Return a display-safe copy of *vector* with every secret redacted."""
    env = tuple((key, redact(value, secrets)) for key, value in vector.env)
    mounts = tuple(
        Mount(redact(mount.host, secrets), mount.container, mount.mode)
        for mount in vector.mounts
    )
    return dataclasses.replace(vector, env=env, mounts=mounts)


@dataclass(frozen=True)
class ProcessResult:
    """Captured subprocess outcome."""

    argv: tuple[str, ...]
    return_code: int
    stdout: str = ""
    stderr: str = ""
    truncation_notice: str | None = None
    """Fixed redacted notice when live output was not fully delivered."""


@dataclass
class _StreamingCleanupOutcome:
    """Thread-safe signal that streaming cleanup already handled ``docker rm``."""

    container_removal_attempted: threading.Event = dataclasses.field(
        default_factory=threading.Event
    )


class RunExecutor(Protocol):
    """Injected execution boundary for a rendered ``docker`` argument list."""

    def run(self, argv: tuple[str, ...]) -> ProcessResult:
        """Execute *argv* and return the captured result."""
        ...


def _default_container_user(
    uid: int | None, gid: int | None
) -> tuple[int, int]:
    """Return the invoking process UID/GID for unspecified identities."""
    return (os.getuid() if uid is None else uid, os.getgid() if gid is None else gid)


def _is_rootless_docker() -> bool:
    """Return whether ``docker info`` reports a rootless daemon.

    A missing ``docker`` binary or an unavailable daemon is treated as
    not-rootless; the subsequent ``docker run`` surfaces the real error
    with an actionable message.
    """
    try:
        proc = subprocess.run(
            ["docker", "info"], capture_output=True, text=True, check=False
        )
    except OSError:
        return False
    return proc.returncode == 0 and "rootless" in proc.stdout.lower()


def _resolve_container_user(
    executor: RunExecutor, uid: int | None, gid: int | None
) -> tuple[int, int]:
    """Resolve the numeric container identity for one assembler run.

    Explicit *uid*/*gid* values win.  A real :class:`DockerRunExecutor`
    resolves rootless Docker to container ``0:0`` (the invoking host user
    under rootless user namespaces); in-memory test executors that omit the
    optional ``resolve_user`` hook fall back to the invoking process UID/GID.
    """
    resolver = getattr(executor, "resolve_user", None)
    if resolver is not None:
        return resolver(uid, gid)
    return _default_container_user(uid, gid)


def _terminate_and_reap(
    proc: subprocess.Popen,
    *,
    grace_seconds: float,
) -> list[BaseException]:
    """Terminate and reap a local docker client left writing to a failed pipe.

    Mirrors the bounded reap used by the deadline/interruption cleanup
    (``_terminate_and_remove``): terminate, wait within *grace_seconds*,
    SIGKILL fallback, then one further bounded wait.  A client that still has
    not exited after SIGKILL is recorded as a bounded cleanup error so this
    function never blocks forever.  Every step is attempted independently so
    one failure cannot skip another.
    """
    return terminate_and_reap(
        proc, policy=LifecyclePolicy(grace_seconds=grace_seconds)
    ).errors


def _bounded_docker_rm(
    container_name: str,
    *,
    grace_seconds: float,
) -> list[BaseException]:
    """Force-remove one daemon container without leaving an rm client running.

    The ``docker rm -f`` client gets a bounded graceful wait, then bounded
    terminate and SIGKILL fallback waits.  Its stdout/stderr are inspected
    only after it exits; an already-absent container is idempotent success,
    while other nonzero exits and every failed cleanup step are returned for
    attachment to the original primary error.
    """
    _, outcome = run_captured(
        ("docker", "rm", "-f", container_name),
        policy=LifecyclePolicy(
            grace_seconds=grace_seconds,
            process_label="docker rm -f client",
        ),
    )
    if (
        outcome.return_code is not None
        and outcome.return_code != 0
        and not _is_container_absent(f"{outcome.stderr}\n{outcome.stdout}")
    ):
        outcome.errors.append(
            OSError(
                f"docker rm -f exited {outcome.return_code}: "
                f"{(outcome.stderr or outcome.stdout).strip()}"
            )
        )
    return outcome.errors


def _terminate_and_remove(
    proc: subprocess.Popen,
    container_name: str | None,
    *,
    grace_seconds: float,
) -> list[BaseException]:
    """Terminate the client, force-remove the container, reap with bounded grace.

    Deadline and interruption cleanup: every step is attempted independently
    so one failure cannot skip another.  The local docker client is
    terminated first (unblocking the pipe readers), the deterministic
    daemon-side container is force-removed independently, and the client is
    reaped within *grace_seconds* (SIGKILL fallback).  A client that still
    has not exited after SIGKILL is given one further bounded *grace_seconds*
    wait; if that also expires the expiry is recorded as a cleanup error so
    the function always returns within a finite time.  Any per-step failure
    is collected and returned; the caller decides how to surface it.
    """
    errors: list[BaseException] = []
    try:
        proc.terminate()
    except OSError as exc:
        errors.append(exc)
    if container_name is not None:
        errors.extend(
            _bounded_docker_rm(container_name, grace_seconds=grace_seconds)
        )
    errors.extend(
        reap_process(
            proc, policy=LifecyclePolicy(grace_seconds=grace_seconds)
        ).errors
    )
    return errors


def _timeout_error(
    deadline_seconds: float,
    *,
    stdout: str = "",
    stderr: str = "",
    truncation_notice: str | None = None,
) -> AssemblyTimeoutError:
    """Build the actionable structured timeout failure from redacted tails."""
    summary = f"assembly exceeded the {deadline_seconds:g}-second total deadline"
    stderr_tail = stderr.strip()
    stdout_tail = stdout.strip()
    tail = stderr_tail or stdout_tail
    if truncation_notice:
        tail = f"{tail} {truncation_notice}".strip()
    detail = f"{summary}: {tail}" if tail else summary
    return AssemblyTimeoutError(
        detail,
        summary=summary,
        diagnostic_tail=tail,
        diagnostic_stream=("stderr" if stderr_tail else "stdout" if stdout_tail else None),
    )


def _close_stream_pipes(pipes: Sequence[IO[bytes]]) -> list[Exception]:
    """Close every pipe independently, returning any close failures.

    Each pipe is closed in its own try/except so one close failure cannot
    skip another pipe's close; failures are collected, never raised here, so
    the caller can guarantee downstream cleanup (process reaping) first.
    """
    close_errors: list[Exception] = []
    for pipe in pipes:
        try:
            pipe.close()
        except Exception as exc:
            close_errors.append(exc)
    return close_errors


def _render_close_failure(
    exc: BaseException,
    secrets: Sequence[str],
    *,
    tail_projector: Callable[..., str] | None = None,
) -> str:
    """Render a close failure as a bounded, redacted message.

    When a URL-safe *tail_projector* is supplied (by the assembler), it
    replaces the secret-only :func:`redact_tail` fallback so a URL-bearing
    cleanup/close failure cannot leak the host, credentials, query, or
    fragment into an attached note.  The bounded
    :data:`READER_FAILURE_DETAIL_BYTES` limit and the fallback behavior are
    preserved.
    """
    detail = str(exc) or repr(exc)
    if tail_projector is None:
        return redact_tail(
            detail,
            secrets,
            tail_bytes=READER_FAILURE_DETAIL_BYTES,
        )
    return tail_projector(
        detail,
        secrets,
        tail_bytes=READER_FAILURE_DETAIL_BYTES,
    )


def _attach_close_failure_notes(
    target: BaseException,
    close_errors: Sequence[BaseException],
    secrets: Sequence[str],
    *,
    tail_projector: Callable[..., str] | None = None,
) -> None:
    """Attach bounded, redacted close failures to *target* as notes."""
    for exc in close_errors:
        target.add_note(
            f"pipe close failed ({type(exc).__name__}): "
            f"{_render_close_failure(exc, secrets, tail_projector=tail_projector)}"
        )


def _raise_close_failures(
    close_errors: Sequence[BaseException],
    secrets: Sequence[str],
    *,
    tail_projector: Callable[..., str] | None = None,
) -> None:
    """Raise the first close failure, redacted; note any further ones."""
    primary = close_errors[0]
    message = _render_close_failure(
        primary, secrets, tail_projector=tail_projector
    )
    try:
        raised: BaseException = type(primary)(message)
    except Exception:
        raised = RuntimeError(message)
    for exc in close_errors[1:]:
        raised.add_note(
            f"also ({type(exc).__name__}): "
            f"{_render_close_failure(exc, secrets, tail_projector=tail_projector)}"
        )
    raise raised


def _attach_bounded_cleanup_notes(
    target: BaseException,
    cleanup_errors: Sequence[BaseException],
    close_errors: Sequence[BaseException],
    secrets: Sequence[str],
    *,
    cleanup_prefix: str,
    tail_projector: Callable[..., str] | None = None,
) -> None:
    """Attach bounded, redacted cleanup and pipe-close failures as notes."""
    for exc in cleanup_errors:
        target.add_note(
            f"{cleanup_prefix} ({type(exc).__name__}): "
            f"{_render_close_failure(exc, secrets, tail_projector=tail_projector)}"
        )
    for exc in close_errors:
        target.add_note(
            f"pipe close failed ({type(exc).__name__}): "
            f"{_render_close_failure(exc, secrets, tail_projector=tail_projector)}"
        )


def _attach_deadline_notes(
    error: AssemblyTimeoutError,
    cleanup_errors: Sequence[BaseException],
    close_errors: Sequence[BaseException],
    secrets: Sequence[str],
    *,
    tail_projector: Callable[..., str] | None = None,
) -> None:
    """Attach deadline cleanup and pipe-close failures to a timeout error."""
    _attach_bounded_cleanup_notes(
        error,
        cleanup_errors,
        close_errors,
        secrets,
        cleanup_prefix="deadline cleanup failed",
        tail_projector=tail_projector,
    )


def _raise_supervisor_failure(
    wait_failure: BaseException,
    cleanup_errors: Sequence[BaseException],
    close_errors: Sequence[BaseException],
    secrets: Sequence[str],
    *,
    tail_projector: Callable[..., str] | None = None,
) -> None:
    """Raise an unexpected supervisor failure as the primary error.

    Operational failures are raised with a bounded, redacted message and the
    termination/reaping/container-removal/pipe-close failures attached as
    bounded redacted notes.  Control-flow exceptions propagate unchanged so
    interruption semantics are preserved.
    """
    if isinstance(wait_failure, _CONTROL_FLOW_EXCEPTIONS):
        raised = wait_failure
    else:
        message = _render_close_failure(
            wait_failure, secrets, tail_projector=tail_projector
        )
        try:
            raised = type(wait_failure)(message)
        except Exception:
            raised = RuntimeError(message)
    _attach_bounded_cleanup_notes(
        raised,
        cleanup_errors,
        close_errors,
        secrets,
        cleanup_prefix="supervisor cleanup failed",
        tail_projector=tail_projector,
    )
    raise raised


def _supervisor_cleanup_budget(grace_seconds: float) -> float:
    """Return the complete bounded cleanup budget for the deadline supervisor.

    The bounded ``docker rm -f`` client may consume three grace intervals
    (wait, terminate-wait, kill-wait); the local Docker client then gets a
    graceful and post-SIGKILL reap interval, followed by final pipe closure.
    Reserve one final grace interval for closure/scheduling, so a single
    grace interval is never enough to bound a join after the supervisor has
    entered cleanup.
    """
    return 6.0 * grace_seconds


def _join_supervisor(
    supervisor: DeadlineSupervisor | None,
    *,
    grace_seconds: float,
) -> list[BaseException]:
    """Cancel and join the reusable supervisor within its cleanup budget."""
    if supervisor is None:
        return []
    budget = _supervisor_cleanup_budget(grace_seconds)
    if not supervisor.join(budget, cancel=True):
        return [TimeoutError(f"deadline supervisor did not stop within {budget:g}s")]
    return []


def _wait_for_cleanup_owner(
    supervisor: DeadlineSupervisor | None,
    *,
    grace_seconds: float,
) -> list[BaseException]:
    """Wait for supervisor-owned cleanup without taking ownership from it."""
    if supervisor is None:
        return []
    budget = _supervisor_cleanup_budget(grace_seconds)
    if not supervisor.join(budget):
        return [
            TimeoutError(
                f"deadline supervisor cleanup did not finish within {budget:g}s"
            )
        ]
    return []


def _wait_for_supervisor(
    supervisor: DeadlineSupervisor | None,
    *,
    deadline_seconds: float,
    grace_seconds: float,
) -> list[BaseException]:
    """Wait for normal exit or deadline cleanup without cancelling supervision."""
    cleanup_budget = _supervisor_cleanup_budget(grace_seconds)
    if supervisor is None:
        return []
    if not supervisor.join(deadline_seconds + cleanup_budget):
        return [
            TimeoutError(
                "deadline supervisor did not finish within "
                f"{deadline_seconds:g}s deadline + {cleanup_budget:g}s cleanup grace"
            )
        ]
    return []


class DockerRunExecutor:
    """Real executor backed by the ``docker`` binary on ``PATH``."""

    def run(self, argv: tuple[str, ...]) -> ProcessResult:
        proc = subprocess.run(argv, capture_output=True, text=True)
        return ProcessResult(argv, proc.returncode, proc.stdout, proc.stderr)

    def run_streaming(
        self,
        argv: tuple[str, ...],
        *,
        secrets: Sequence[str] = (),
        sink: DiagnosticSink | None = None,
        deadline_seconds: float | None = None,
        container_name: str | None = None,
        grace_seconds: float = 5.0,
        cleanup_outcome: _StreamingCleanupOutcome | None = None,
        stream_factory=None,
        tail_projector=None,
        on_launched: Callable[[], None] | None = None,
    ) -> ProcessResult:
        """Run *argv*, draining stdout/stderr concurrently with redaction.

        Both pipes are drained without waiting for newlines; safe redacted
        prefixes reach *sink* (when provided) before process exit; retained
        stdout/stderr are bounded redacted tails.  A pipe-reader failure is
        reported immediately: the failed pipe is closed, the local docker
        client is terminated and reaped, and both pipes are then closed
        independently (the sibling pipe included) so the sibling reader
        reaches EOF and drains its remaining data even when the client
        resists termination; both readers are then joined and the sink
        dispatcher finalized before :class:`StreamReaderFailure` is raised.  The named daemon-side container stays owned by ``assemble``'s
        cleanup.

        When *deadline_seconds* is set, a supervisor polls the client in
        short slices (checking a cancellation event) instead of blocking in
        one long ``proc.wait(timeout=deadline_seconds)``.  Once the deadline
        expires it terminates the client, force-removes the named
        *container_name* (when given), reaps the client within
        *grace_seconds* (SIGKILL fallback), and closes both pipes so blocked
        readers always reach EOF; the dispatcher then finalizes, and a
        structured :class:`AssemblyTimeoutError` is raised.  An unexpected
        failure from a poll is captured by the supervisor, which still runs
        the same bounded termination/reaping/container removal and pipe
        closure to unblock the readers, and is surfaced as the primary error
        (with cleanup/close failures attached as bounded redacted notes)
        instead of crashing the supervisor thread.  During
        ``KeyboardInterrupt``/``SystemExit``, cleanup ownership is decided
        under a lock: when the supervisor has already started deadline
        cleanup, interruption waits for that one bounded cleanup instead of
        starting a concurrent terminate/remove sequence; otherwise
        interruption cancels the supervisor and owns cleanup itself.  The
        supervisor join accounts for its complete bounded cleanup budget
        (container removal, graceful reap, post-SIGKILL reap, and pipe
        closure), so no supported path returns or raises with the supervisor
        still alive.  Normal completion does not cancel the supervisor: both
        pipes reaching EOF does not mean the process exited, so the
        supervisor is joined without cancellation and keeps enforcing the
        deadline until it observes either a normal exit or the deadline
        expiry, reusing its bounded outcome instead of an unbounded
        ``proc.wait()``.
        """
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        assert proc.stdout is not None and proc.stderr is not None
        stdout_pipe = proc.stdout
        stderr_pipe = proc.stderr
        #: Shared cancellation signal handed to the collector.  It is set
        #: before any forced termination or pipe closure so a reader that
        #: reaches forced EOF finalizes with ``abort=True`` instead of
        #: flushing an ambiguous secret/URL prefix as ordinary text.
        abort_event = threading.Event()

        def bounded_streaming_cleanup() -> list[BaseException]:
            # Signal forced termination before terminating the client or
            # closing pipes so the readers finalize as aborted.
            abort_event.set()
            if cleanup_outcome is not None and container_name is not None:
                cleanup_outcome.container_removal_attempted.set()
            return _terminate_and_remove(
                proc, container_name, grace_seconds=grace_seconds
            )

        def react_to_reader_failure(failed_stream: str) -> None:
            # Attempt every cleanup step independently so a failure in one
            # (e.g. closing the failed pipe) cannot skip the others (e.g.
            # terminating the client, which unblocks the sibling reader).
            # Both pipes are closed after the bounded reap — independently,
            # so a close failure on one cannot skip the other — which
            # guarantees the sibling reader reaches EOF even when the client
            # ignores terminate and survives kill.  Collected errors are
            # re-raised so collect_streams records them as secondary context
            # without masking the reader failure; the run_streaming finally
            # block still guarantees both pipes close.
            failed_pipe = (
                stdout_pipe if failed_stream == STREAM_STDOUT else stderr_pipe
            )
            cleanup_errors: list[BaseException] = []
            try:
                failed_pipe.close()
            except Exception as exc:
                cleanup_errors.append(exc)
            cleanup_errors.extend(
                _terminate_and_reap(proc, grace_seconds=grace_seconds)
            )
            cleanup_errors.extend(
                _close_stream_pipes((stdout_pipe, stderr_pipe))
            )
            if cleanup_errors:
                primary = cleanup_errors[0]
                for extra in cleanup_errors[1:]:
                    primary.add_note(
                        f"also ({type(extra).__name__}): "
                        f"{str(extra) or repr(extra)}"
                    )
                raise primary

        supervisor: DeadlineSupervisor | None = None

        def react_to_interruption() -> None:
            # The reusable supervisor atomically decides whether deadline
            # cleanup already owns the process.  The domain layer still owns
            # Docker removal, pipe closure, and exception-note formatting.
            supervisor_owns_cleanup = (
                supervisor.claim_interruption() if supervisor is not None else False
            )
            if supervisor_owns_cleanup:
                shutdown_failures = _wait_for_cleanup_owner(
                    supervisor, grace_seconds=grace_seconds
                )
                if not shutdown_failures:
                    return
                errors = bounded_streaming_cleanup()
                errors.extend(_close_stream_pipes((stdout_pipe, stderr_pipe)))
                primary = shutdown_failures[0]
                for extra in list(shutdown_failures[1:]) + errors:
                    primary.add_note(
                        f"also ({type(extra).__name__}): "
                        f"{str(extra) or repr(extra)}"
                    )
                raise primary
            errors = bounded_streaming_cleanup()
            errors.extend(_close_stream_pipes((stdout_pipe, stderr_pipe)))
            if errors:
                primary = errors[0]
                for extra in errors[1:]:
                    primary.add_note(
                        f"also ({type(extra).__name__}): "
                        f"{str(extra) or repr(extra)}"
                    )
                raise primary

        if deadline_seconds is not None:
            def poll_process() -> bool:
                try:
                    proc.wait(timeout=0)
                except subprocess.TimeoutExpired:
                    return False
                return True

            def deadline_cleanup() -> tuple[
                list[BaseException], list[BaseException]
            ]:
                # Terminate/remove/reap first.  Closing pipes before that can
                # release readers while the Docker client is still running.
                cleanup_errors = bounded_streaming_cleanup()
                close_errors: list[BaseException] = list(
                    _close_stream_pipes((stdout_pipe, stderr_pipe))
                )
                return cleanup_errors, close_errors

            supervisor = DeadlineSupervisor(
                deadline_seconds=deadline_seconds,
                poll=poll_process,
                cleanup=deadline_cleanup,
                poll_seconds=_SUPERVISOR_POLL_SECONDS,
                thread_name="npm-deadline-supervisor",
            )
            supervisor.start()

        try:
            # Report the launch boundary now that the container client process
            # exists, so CONTAINER_STARTUP ends at launch and NPM_EXECUTION
            # spans stream collection and process completion.  The call is
            # inside the collection try so a callback failure still runs the
            # normal supervisor/pipe cleanup below.
            if on_launched is not None:
                on_launched()
            capture: StreamingCapture = collect_streams(
                stdout_read=stdout_pipe.read,
                stderr_read=stderr_pipe.read,
                secrets=secrets,
                sink=sink,
                on_reader_failure=react_to_reader_failure,
                on_interruption=react_to_interruption,
                stream_factory=stream_factory,
                tail_projector=tail_projector,
                abort_event=abort_event,
            )
        except BaseException as exc:
            supervisor_failures = _join_supervisor(
                supervisor, grace_seconds=grace_seconds
            )
            deadline_outcome = supervisor.snapshot() if supervisor is not None else None
            fired = bool(deadline_outcome and deadline_outcome.fired)
            wait_failure = (
                deadline_outcome.wait_failure if deadline_outcome is not None else None
            )
            timeout_errors = (
                deadline_outcome.cleanup_errors if deadline_outcome is not None else []
            )
            timeout_close_errors = (
                deadline_outcome.close_errors if deadline_outcome is not None else []
            )
            close_errors = timeout_close_errors + _close_stream_pipes(
                (stdout_pipe, stderr_pipe)
            )
            if fired:
                # The deadline won: the timeout is primary; the concurrent
                # reader/interruption failure becomes bounded secondary context.
                assert deadline_seconds is not None
                timeout = _timeout_error(deadline_seconds)
                _attach_deadline_notes(
                    timeout,
                    timeout_errors,
                    close_errors,
                    secrets,
                    tail_projector=tail_projector,
                )
                _attach_bounded_cleanup_notes(
                    timeout,
                    supervisor_failures,
                    (),
                    secrets,
                    cleanup_prefix="supervisor shutdown failed",
                    tail_projector=tail_projector,
                )
                timeout.add_note(
                    f"also interrupted by {type(exc).__name__}: "
                    f"{_render_close_failure(exc, secrets, tail_projector=tail_projector)}"
                )
                raise timeout
            # The reader/interruption failure stays primary; pipe-close, any
            # unexpected supervisor wait failure, and any supervisor-shutdown
            # failure are secondary context.
            _attach_close_failure_notes(
                exc, close_errors, secrets, tail_projector=tail_projector
            )
            if wait_failure is not None:
                exc.add_note(
                    f"supervisor wait failed ({type(wait_failure).__name__}): "
                    f"{_render_close_failure(wait_failure, secrets, tail_projector=tail_projector)}"
                )
            _attach_bounded_cleanup_notes(
                exc,
                supervisor_failures,
                (),
                secrets,
                cleanup_prefix="supervisor shutdown failed",
                tail_projector=tail_projector,
            )
            raise

        if supervisor is None:
            supervisor_failures: list[BaseException] = []
        else:
            assert deadline_seconds is not None
            supervisor_failures = _wait_for_supervisor(
                supervisor,
                deadline_seconds=deadline_seconds,
                grace_seconds=grace_seconds,
            )
        deadline_outcome = supervisor.snapshot() if supervisor is not None else None
        fired = bool(deadline_outcome and deadline_outcome.fired)
        wait_failure = (
            deadline_outcome.wait_failure if deadline_outcome is not None else None
        )
        timeout_errors = (
            deadline_outcome.cleanup_errors if deadline_outcome is not None else []
        )
        timeout_close_errors = (
            deadline_outcome.close_errors if deadline_outcome is not None else []
        )

        # Normal path: capture (do not raise) close failures so the
        # subprocess outcome is always known before any deferred cleanup
        # failure surfaces.  The supervisor may already have closed both
        # pipes to unblock readers; re-closing is idempotent and only
        # appends any fresh close failures.
        close_errors = timeout_close_errors + _close_stream_pipes(
            (stdout_pipe, stderr_pipe)
        )

        if fired:
            # The supervisor already terminated and reaped the client within
            # grace; the timeout is primary with the retained tails and any
            # cleanup/close failures attached as bounded redacted context.
            assert deadline_seconds is not None
            timeout = _timeout_error(
                deadline_seconds,
                stdout=capture.stdout_tail,
                stderr=capture.stderr_tail,
                truncation_notice=capture.truncation_notice,
            )
            _attach_deadline_notes(
                timeout,
                timeout_errors,
                close_errors,
                secrets,
                tail_projector=tail_projector,
            )
            _attach_bounded_cleanup_notes(
                timeout,
                supervisor_failures,
                (),
                secrets,
                cleanup_prefix="supervisor shutdown failed",
                tail_projector=tail_projector,
            )
            raise timeout

        if wait_failure is not None:
            # The supervisor's poll failed unexpectedly; it already ran the
            # bounded termination/reaping and container removal.  Surface
            # that operational/control-flow failure as primary without an
            # unbounded reaping wait.
            _raise_supervisor_failure(
                wait_failure,
                timeout_errors,
                close_errors,
                secrets,
                tail_projector=tail_projector,
            )

        if supervisor_failures:
            # The deadline supervisor never finished within the deadline +
            # cleanup-grace bound; surface that as primary (bounded,
            # redacted) with pipe-close failures as notes instead of an
            # unbounded proc.wait() that could block past the deadline.
            primary = supervisor_failures[0]
            message = _render_close_failure(
                primary, secrets, tail_projector=tail_projector
            )
            try:
                raised: BaseException = type(primary)(message)
            except Exception:
                raised = RuntimeError(message)
            for extra in supervisor_failures[1:]:
                raised.add_note(
                    f"also ({type(extra).__name__}): "
                    f"{_render_close_failure(extra, secrets, tail_projector=tail_projector)}"
                )
            _attach_close_failure_notes(
                raised, close_errors, secrets, tail_projector=tail_projector
            )
            raise raised

        if supervisor is None:
            # No deadline was configured: the client is expected to exit once
            # both pipes reach EOF, so wait for it (nothing bounds this case).
            try:
                return_code = proc.wait()
            except BaseException as wait_exc:
                _attach_close_failure_notes(
                    wait_exc,
                    close_errors,
                    secrets,
                    tail_projector=tail_projector,
                )
                raise
        else:
            # The supervisor observed a normal exit and reaped the client;
            # reuse its bounded outcome instead of a second (unbounded) wait.
            return_code = proc.returncode
            assert return_code is not None

        if close_errors:
            _raise_close_failures(
                close_errors, secrets, tail_projector=tail_projector
            )
        return ProcessResult(
            argv,
            return_code,
            capture.stdout_tail,
            capture.stderr_tail,
            truncation_notice=capture.truncation_notice,
        )

    def resolve_user(self, uid: int | None, gid: int | None) -> tuple[int, int]:
        """Resolve the numeric container identity for the invoking host user.

        Explicit *uid*/*gid* win.  When both are unspecified and the Docker
        daemon is rootless, the invoking host user maps to container
        UID/GID ``0``, so ``0:0`` is returned to keep the owner-private host
        bind mounts writable; otherwise the invoking process UID/GID.
        """
        if uid is not None and gid is not None:
            return uid, gid
        if uid is None and gid is None and _is_rootless_docker():
            return 0, 0
        return _default_container_user(uid, gid)


@dataclass(frozen=True)
class AssemblyRun:
    """Structured result of one successful assembler container execution.

    The stored vector, argv, stdout, and stderr are redacted copies: every
    configured proxy endpoint and trust path (plus any caller-supplied
    secret) is replaced with ``<redacted>``.  The exact executable argv is
    never persisted in the result.
    """

    run_vector: DockerRunVector
    """Redacted run vector actually executed (secrets replaced)."""

    argv: tuple[str, ...]
    """Redacted ``docker run`` argument list executed (secrets replaced)."""

    staging: Path
    """The populated staging workspace (retained for later validation)."""

    stdout: str
    """Redacted container stdout."""

    stderr: str
    """Redacted container stderr (bounded diagnostic tail)."""

    truncation_notice: str | None = None
    """Fixed redacted notice when live output was not fully delivered."""


@dataclass(frozen=True)
class CleanupFailure:
    """Structured record of one cleanup operation that failed.

    Cleanup failures never replace the primary assembly exception; they are
    attached to it as notes so the original failure stays identifiable and
    every cleanup failure stays observable.
    """

    operation: str
    """``"container"`` or ``"staging"``."""

    reason: str
    """Machine-readable reason (``docker_rm_nonzero``,
    ``docker_rm_exception``, or the underlying staging error reason)."""

    detail: str
    """Redacted human-readable detail, including the staging path when
    mutable residue may remain."""


def _exit_failure(
    return_code: int,
    *,
    stderr: str,
    stdout: str,
    truncation_notice: str | None = None,
) -> LockedNpmError:
    """Build a structured nonzero-exit failure from already-redacted output."""
    if return_code == EXIT_NODE_VERSION_MISMATCH:
        reason = "node_version_mismatch"
    elif return_code == EXIT_NPM_VERSION_MISMATCH:
        reason = "npm_version_mismatch"
    else:
        reason = "npm_exit_nonzero"

    stderr = stderr.strip()
    stdout = stdout.strip()
    summary = f"assembler exited {return_code}"
    tail = stderr or stdout
    if truncation_notice:
        tail = f"{tail} {truncation_notice}".strip()
    detail = f"{summary}: {tail}" if tail else summary
    return LockedNpmError(
        reason,
        detail,
        summary=summary,
        diagnostic_tail=tail,
        diagnostic_stream=("stderr" if stderr else "stdout" if stdout else None),
    )


def _write_lockfile(staging: Path, lockfile_bytes: bytes) -> None:
    """Write the exact lockfile bytes into the private staging workspace.

    The file is created ``0444`` with ``O_NOFOLLOW | O_EXCL`` so a symlink or
    pre-existing entry is never followed or overwritten.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    path = staging / "package-lock.json"
    try:
        fd = os.open(str(path), flags, 0o444)
    except OSError as exc:
        raise LockedNpmError(
            "unsafe_staging_path",
            f"cannot write lockfile input {path}: {exc}",
        ) from exc
    try:
        os.write(fd, lockfile_bytes)
    finally:
        os.close(fd)


def _is_container_absent(text: str) -> bool:
    """Return whether docker reported the container already gone."""
    lowered = text.lower()
    return "no such container" in lowered or "no such object" in lowered


def _cleanup_container(
    executor: RunExecutor,
    name: str,
    secrets: Sequence[str],
    *,
    grace_seconds: float = 5.0,
    tail_projector: Callable[..., str] | None = None,
) -> CleanupFailure | None:
    """Force-remove the assembler container and return any cleanup failure.

    Returns ``None`` when removal succeeds or the container is already
    absent (idempotent cleanup success).  A nonzero exit or a raised
    exception (including cancellation during cleanup) is reported as a
    structured :class:`CleanupFailure` rather than being swallowed.

    Exception and captured-output details are routed through the injected
    ``tail_projector`` (falling back to the secret-redacting ``redact_tail``)
    so Docker cleanup output can never leak a URL through the attached note.
    Container-absence detection uses the original output so sanitization
    never changes control flow.
    """
    project = tail_projector if tail_projector is not None else redact_tail
    try:
        if isinstance(executor, DockerRunExecutor):
            result = subprocess.run(
                ("docker", "rm", "-f", name),
                capture_output=True,
                text=True,
                check=False,
                timeout=grace_seconds,
            )
            return_code = result.returncode
            stdout = result.stdout
            stderr = result.stderr
        else:
            # Injected executors preserve the Phase 2 ``RunExecutor`` seam;
            # the real Docker path above is always bounded.
            result = executor.run(("docker", "rm", "-f", name))
            return_code = result.return_code
            stdout = result.stdout
            stderr = result.stderr
    except BaseException as exc:
        return CleanupFailure(
            operation="container",
            reason="docker_rm_exception",
            detail=project(f"{type(exc).__name__}: {exc}", secrets),
        )
    if return_code != 0:
        if _is_container_absent(f"{stderr}\n{stdout}"):
            return None
        detail = (
            project(stderr, secrets).strip()
            or project(stdout, secrets).strip()
            or f"exit {return_code}"
        )
        return CleanupFailure(
            operation="container",
            reason="docker_rm_nonzero",
            detail=f"container cleanup failed (exit {return_code}): {detail}",
        )
    return None


def _remove_staging_safely(
    namespace: AssemblerNamespace,
    name: str,
    staging_path: Path,
    secrets: Sequence[str],
    *,
    tail_projector: Callable[..., str] | None = None,
) -> CleanupFailure | None:
    """Remove one staging workspace and return any cleanup failure.

    Returns ``None`` on success.  Any raised failure (including
    cancellation during cleanup) is reported as a structured
    :class:`CleanupFailure` naming the staging path and noting that mutable
    residue may remain.  Prior committed environments are never touched.

    Exception-derived detail is routed through the injected
    ``tail_projector`` (falling back to the secret-redacting ``redact_tail``)
    so an underlying failure carrying a URL cannot leak it via the note.
    """
    project = tail_projector if tail_projector is not None else redact_tail
    try:
        remove_staging_workspace(namespace, name)
        return None
    except BaseException as exc:
        if isinstance(exc, LockedNpmError):
            reason = exc.reason
            detail = project(exc.detail, secrets)
        else:
            reason = type(exc).__name__
            detail = project(str(exc) or repr(exc), secrets)
        return CleanupFailure(
            operation="staging",
            reason=reason,
            detail=f"mutable staging residue may remain at {staging_path}: {detail}",
        )


def _attach_cleanup_notes(
    exc: BaseException, failures: Sequence[CleanupFailure]
) -> None:
    """Attach every cleanup failure as a note on the primary exception."""
    for failure in failures:
        exc.add_note(
            f"cleanup failure ({failure.operation}): "
            f"{failure.reason}: {failure.detail}"
        )


def _streaming_kwargs(
    runner,
    secrets: Sequence[str],
    sink: DiagnosticSink | None,
    container_name: str,
    cleanup_outcome: _StreamingCleanupOutcome,
    stream_factory=None,
    tail_projector=None,
    on_launched=None,
) -> dict:
    """Build the streaming call arguments the *runner* actually accepts.

    The real :class:`DockerRunExecutor.run_streaming` accepts the
    constructor-owned deadline, container name, and launch callback; injected
    test executors that only implement the Phase 2 signature receive just
    ``secrets`` and ``sink`` so they remain usable without a deadline.
    Structured projection arguments are forwarded only when the runner
    declares them.
    """
    kwargs: dict = {"secrets": secrets, "sink": sink}
    try:
        params = inspect.signature(runner).parameters
    except (TypeError, ValueError):
        return kwargs
    if "deadline_seconds" in params:
        kwargs["deadline_seconds"] = ASSEMBLY_TOTAL_TIMEOUT_SECONDS
    if "container_name" in params:
        kwargs["container_name"] = container_name
    if "cleanup_outcome" in params:
        kwargs["cleanup_outcome"] = cleanup_outcome
    if "stream_factory" in params:
        kwargs["stream_factory"] = stream_factory
    if "tail_projector" in params:
        kwargs["tail_projector"] = tail_projector
    if "on_launched" in params:
        kwargs["on_launched"] = on_launched
    return kwargs


def assemble(
    *,
    validated: ValidatedAssemblyInput,
    assembler: AssemblerIdentity,
    cache_root: str | Path,
    executor: RunExecutor,
    uid: int | None = None,
    gid: int | None = None,
    secrets: Sequence[str] = (),
    corporate_network: CorporateNetworkPolicy | None = None,
    sink: DiagnosticSink | None = None,
    stream_factory=None,
    tail_projector=None,
    activity: AssemblyActivity | None = None,
) -> AssemblyRun:
    """Run one standalone pinned assembler container.

    Rejects a mutable or malformed image reference, changed lock bytes,
    roots, platform, reviewed tool versions, or script/policy digests before
    any effect.  On success the populated staging workspace is returned; on
    any failure the container is force-removed and the staging workspace is
    removed, and any cleanup failure is attached as a note on the original
    error before it is re-raised.

    *corporate_network* carries the caller-resolved credential-free proxy
    and corporate trust policy.  Its values are automatically added to the
    redaction secrets, so the successful result and every raised or attached
    failure are free of the configured proxy endpoint and trust path.

    *sink* is an optional constructor-owned prompt-returning diagnostic
    callback.  Executors that support streaming deliver redacted output to
    it before process exit; an absent *sink* produces no live output and
    only bounded diagnostics are retained.
    """
    recheck_assembler_bindings(assembler)
    input_identity = compute_assembler_input_identity(validated, assembler)

    policy_secrets = (
        corporate_network.secrets() if corporate_network is not None else ()
    )
    effective_secrets = tuple(secrets) + policy_secrets

    uid, gid = _resolve_container_user(executor, uid, gid)

    namespace = prepare_assembler_namespace(cache_root, assembler.digest)
    staging_name = input_identity.digest
    container_name = (
        f"npm-assembler-{input_identity.digest[:ASSEMBLER_DIGEST_PREFIX_LENGTH]}"
    )

    staging: Path | None = None
    container_started = False
    streaming_cleanup_outcome = _StreamingCleanupOutcome()
    scoped = activity if activity is not None else NULL_ACTIVITY
    with contextlib.ExitStack() as activity_stack:
        activity_stack.enter_context(
            scoped.step("container_startup", container_name=container_name)
        )
        npm_execution_started = False

        def begin_npm_execution() -> None:
            nonlocal npm_execution_started
            if npm_execution_started:
                return
            # End CONTAINER_STARTUP at the launch boundary (success) and open
            # NPM_EXECUTION without disturbing any other scope.
            activity_stack.pop_all().close()
            npm_execution_started = True
            activity_stack.enter_context(
                scoped.step("npm_execution", container_name=container_name)
            )

        try:
            staging = prepare_staging_workspace(namespace, staging_name)
            _write_lockfile(staging, validated.lockfile_bytes)
            vector = render_run_vector(
                validated=validated,
                assembler=assembler,
                staging=staging,
                npm_cache=namespace.npm_cache,
                uid=uid,
                gid=gid,
                name=container_name,
                corporate_network=corporate_network,
            )
            argv = render_docker_argv(vector)
            container_started = True
            executor_failure_detail: str | None = None
            truncation_notice: str | None = None
            streaming_runner = getattr(executor, "run_streaming", None)
            project = (
                tail_projector if tail_projector is not None else redact_tail
            )
            try:
                if streaming_runner is not None:
                    streaming_kwargs = _streaming_kwargs(
                        streaming_runner,
                        effective_secrets,
                        sink,
                        container_name,
                        streaming_cleanup_outcome,
                        stream_factory,
                        tail_projector,
                        begin_npm_execution,
                    )
                    if "on_launched" not in streaming_kwargs:
                        # The runner cannot report the launch boundary, so the
                        # step is bracketed around its whole call.
                        begin_npm_execution()
                    result = streaming_runner(argv, **streaming_kwargs)
                    stdout = result.stdout
                    stderr = result.stderr
                    truncation_notice = getattr(result, "truncation_notice", None)
                else:
                    begin_npm_execution()
                    result = executor.run(argv)
                    stdout = project(result.stdout, effective_secrets)
                    stderr = project(result.stderr, effective_secrets)
            except _CONTROL_FLOW_EXCEPTIONS:
                raise
            except AssemblyTimeoutError:
                # Timeout classification is constructor-owned and distinct; the
                # outer boundary still force-removes the container and staging
                # before the error propagates.
                raise
            except BaseException as exc:
                # Capture only the sanitized detail here.  The structured error
                # is raised after leaving the except block so Python does not
                # assign the original exception to __context__.  The detail is
                # routed through the same URL-free tail projector used for
                # assembled output so an executor exception can never leak a
                # URL into the failure representation.
                executor_failure_detail = project(
                    f"{type(exc).__name__}: {str(exc) or repr(exc)}",
                    effective_secrets,
                )

            if executor_failure_detail is not None:
                raise LockedNpmError(
                    "executor_failure",
                    executor_failure_detail,
                ) from None
            if result.return_code != 0:
                raise _exit_failure(
                    result.return_code,
                    stderr=stderr,
                    stdout=stdout,
                    truncation_notice=truncation_notice,
                )
            return AssemblyRun(
                run_vector=redact_run_vector(vector, effective_secrets),
                argv=redact_docker_argv(argv, effective_secrets),
                staging=staging,
                stdout=stdout,
                stderr=stderr,
                truncation_notice=truncation_notice,
            )
        except BaseException as exc:
            failures: list[CleanupFailure] = []
            if (
                container_started
                and not streaming_cleanup_outcome.container_removal_attempted.is_set()
            ):
                container_failure = _cleanup_container(
                    executor,
                    container_name,
                    effective_secrets,
                    tail_projector=tail_projector,
                )
                if container_failure is not None:
                    failures.append(container_failure)
            if staging is not None:
                staging_failure = _remove_staging_safely(
                    namespace,
                    staging_name,
                    staging,
                    effective_secrets,
                    tail_projector=tail_projector,
                )
                if staging_failure is not None:
                    failures.append(staging_failure)
            _attach_cleanup_notes(exc, failures)
            raise

    raise AssertionError("assembly activity scope must return or raise")
