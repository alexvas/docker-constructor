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
import functools
import inspect
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, Sequence

from docker.filesystem.descriptors import OwnedDescriptor
from docker.filesystem.operations import PosixDescriptorOps
from docker.process import (
    ActionResult,
    FailureInfo,
    FailureKind,
    FailurePhase,
    OwnedProcess,
    ProcessStream,
    StopContext,
    StopPolicy,
    StopStage,
    StreamOutcome,
)
from docker.versioning.model import NetworkUrlDisplay

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
    LifecycleOutcome,
    LifecyclePolicy,
    _label_final_reap,
    _run_captured,
    run_captured,
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
    RETAINED_CAPTURE_ATTR,
    STREAM_STDERR,
    STREAM_STDOUT,
    TRUNCATION_NOTICE,
    DiagnosticSink,
    StreamingCapture,
    StreamReaderFailure,
    _attach_retained_capture,
    _DiagnosticCollector,
    redact_tail,
    redact_text,
)

#: Control-flow exceptions that must propagate unchanged from the executor
#: boundary (never converted into a structured assembler failure).
_CONTROL_FLOW_EXCEPTIONS = (KeyboardInterrupt, SystemExit, GeneratorExit)


def redact(text: str, secrets: Sequence[str]) -> str:
    """Replace every non-empty *secret* in *text* with ``<redacted>``.

    Deterministic and caller-order-independent: at the leftmost position
    where any secret matches, the longest complete match is replaced with
    exactly one marker.
    """
    return redact_text(text, secrets)


def redact_docker_argv(argv: Sequence[str], secrets: Sequence[str]) -> tuple[str, ...]:
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


def _mask_cleanup_urls(detail: str) -> str:
    return default_tail_projector(NetworkUrlDisplay.REDACTED)(
        detail, (), tail_bytes=READER_FAILURE_DETAIL_BYTES
    )


_FAILURE_SUBJECTS = {
    None: "process",
    ProcessStream.STDOUT: "stdout",
    ProcessStream.STDERR: "stderr",
}
_FAILURE_OPERATIONS = {
    FailurePhase.SETUP: "setup",
    FailurePhase.READ: "read",
    FailurePhase.JOIN: "join",
    FailurePhase.RELEASE: "release",
    FailurePhase.WAIT: "wait",
    FailurePhase.ACTION: "action",
    FailurePhase.CALLBACK: "callback",
    FailurePhase.REPORTER: "reporter",
    FailurePhase.COORDINATION: "coordination",
}


def _process_cleanup_prefix(failure: FailureInfo) -> str:
    return f"{_FAILURE_SUBJECTS[failure.stream]} {_FAILURE_OPERATIONS[failure.phase]} failed"


class _ContainerRemovalAction:
    """Prepared nonblocking domain request; generic ownership handles each client."""

    def __init__(self, name, grace, outcome):
        self.name, self.grace, self.outcome = name, grace, outcome
        self.lock = threading.Lock()
        self.request = threading.Event()
        self.ready = threading.Event()
        self.done = threading.Event()
        self.context: StopContext | None = None
        self.cancelled = False
        self.owner: OwnedProcess | None = None
        self.helper_policy: LifecyclePolicy | None = None
        self.failures: tuple[FailureInfo, ...] = ()
        self.thread = threading.Thread(
            target=self._work, name="npm-removal-action", daemon=True
        )

    def prepare(self):
        try:
            self.thread.start()
        except BaseException:
            if self.thread.ident is None:
                self.done.set()
            self.request.set()
            raise
        if not self.ready.wait(self.grace):
            raise RuntimeError("removal action did not become ready")

    def start(self, context):
        with self.lock:
            self.context = context
            self.request.set()

    def cancel(self, deadline):
        with self.lock:
            self.cancelled = True
            self.request.set()

    def observe(self):
        done = self.done.is_set()
        if done and self.thread.ident is not None:
            self.thread.join(0)
        complete = done and (self.thread.ident is None or not self.thread.is_alive())
        failures = list(self.failures)
        if self.owner is not None:
            status = self.owner.cleanup_status
            complete = complete and status.complete
            known = {(id(f.error), f.kind) for f in failures}
            failures.extend(
                FailureInfo(f.error, f.kind, FailurePhase.ACTION)
                for f in status.failures
                if (id(f.error), f.kind) not in known
            )
        return ActionResult(done, complete, tuple(failures))

    def _work(self):
        self.ready.set()
        try:
            self.request.wait()
            with self.lock:
                context = self.context
            if context is None or self.name is None:
                return
            if self.outcome is not None:
                self.outcome.container_removal_attempted.set()
            remaining = context.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("container removal stage expired")
            self.helper_policy = LifecyclePolicy(
                min(self.grace, remaining / 3), process_label="docker rm -f client"
            )
            operation = _run_captured(
                ("docker", "rm", "-f", self.name), policy=self.helper_policy
            )
            self.owner = operation.owner
            transferred = tuple(
                FailureInfo(f.error, f.kind, FailurePhase.ACTION)
                for f in operation.failures
            )
            known = {id(f.error) for f in transferred}
            # _removal_errors creates an ordinary domain error for a nonzero
            # non-absent exit; returned helper classifications remain intact.
            domain_errors = tuple(
                FailureInfo(error, FailureKind.ORDINARY, FailurePhase.ACTION)
                for error in _removal_errors(operation.outcome)
                if id(error) not in known
            )
            self.failures = transferred + domain_errors
        except BaseException as exc:
            handle = getattr(exc, "cleanup", None)
            if isinstance(handle, OwnedProcess):
                self.owner = handle
                helper_failures = handle.cleanup_status.failures
                if self.helper_policy is not None:
                    for failure in helper_failures:
                        _label_final_reap(failure, self.helper_policy)
                self.failures = tuple(
                    FailureInfo(f.error, f.kind, FailurePhase.ACTION)
                    for f in helper_failures
                )
            if not any(f.error is exc for f in self.failures):
                # This exception was raised at this action's own boundary,
                # rather than transported as an unclassified helper result.
                kind = (
                    FailureKind.INTERRUPTION
                    if not isinstance(exc, Exception)
                    else FailureKind.ORDINARY
                    if isinstance(exc, OSError)
                    else FailureKind.UNEXPECTED
                )
                self.failures += (FailureInfo(exc, kind, FailurePhase.ACTION),)
        finally:
            self.done.set()


class RunExecutor(Protocol):
    """Injected execution boundary for a rendered ``docker`` argument list."""

    def run(self, argv: tuple[str, ...]) -> ProcessResult:
        """Execute *argv* and return the captured result."""
        ...


def _default_container_user(uid: int | None, gid: int | None) -> tuple[int, int]:
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
    return _removal_errors(outcome)


def _removal_errors(outcome: LifecycleOutcome) -> list[BaseException]:
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


def _attached_retained_capture(
    reason: BaseException,
) -> StreamingCapture | None:
    """Return the bounded capture a failed collection attached, if any.

    The domain collector attaches one already-selected bounded capture.
    Facade error translation carries it verbatim, never reconstructing raw
    generic output or changing its per-stream EOF/abort finalization.
    """
    capture = getattr(reason, RETAINED_CAPTURE_ATTR, None)
    if isinstance(capture, StreamingCapture):
        return capture
    return None


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
        diagnostic_stream=(
            "stderr" if stderr_tail else "stdout" if stdout_tail else None
        ),
    )


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


def _streaming_cleanup_budget(grace_seconds: float) -> float:
    """Preserve six legacy grace intervals in the single generic budget.

    The removal action gets three intervals; local reap, reader barriers and
    winning pipe-release publication share the remaining three. No stage,
    helper or concurrent caller may start another total allowance.
    """
    return 6.0 * grace_seconds


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
        network_url_display: NetworkUrlDisplay | None = None,
        on_launched: Callable[[], None] | None = None,
    ) -> ProcessResult:
        """Collect domain diagnostics while one generic owner controls cleanup."""
        if network_url_display is not None and not isinstance(
            network_url_display, NetworkUrlDisplay
        ):
            raise ValueError("network_url_display must be a NetworkUrlDisplay")
        collector = None
        owner = None
        capture = StreamingCapture("", "", None)

        def report(error, *, stream, callback_kind):
            detail = _render_close_failure(
                error, secrets, tail_projector=tail_projector
            )
            # URL masking is required even without a caller-supplied projector.
            detail = _mask_cleanup_urls(detail)
            logging.getLogger(__name__).warning(
                "%s %s callback failed: %s", stream.name, callback_kind, detail
            )

        def launched():
            nonlocal collector
            if on_launched is not None:
                on_launched()
            collector = _DiagnosticCollector(
                secrets=secrets, sink=sink, stream_factory=stream_factory
            )

        def chunk(stream, data):
            if collector is not None:
                collector.feed(
                    STREAM_STDOUT if stream is ProcessStream.STDOUT else STREAM_STDERR,
                    data,
                )

        def end(stream, outcome):
            if collector is not None:
                collector.end(
                    STREAM_STDOUT if stream is ProcessStream.STDOUT else STREAM_STDERR,
                    abort=outcome is not StreamOutcome.EOF,
                )

        def fail(error, failures, *, timed_out=False, complete=True):
            for failure in failures:
                _label_final_reap(failure, LifecyclePolicy(grace_seconds))
            selected = failures[0] if failures else None
            if not isinstance(error, Exception):
                raised = error
            elif (
                timed_out
                and deadline_seconds is not None
                and (selected is None or selected.kind is FailureKind.ORDINARY)
            ):
                raised = _timeout_error(
                    deadline_seconds,
                    stdout=capture.stdout_tail,
                    stderr=capture.stderr_tail,
                    truncation_notice=capture.truncation_notice,
                )
            elif selected is not None and selected.phase is FailurePhase.READ:
                tag = (
                    STREAM_STDOUT
                    if selected.stream is ProcessStream.STDOUT
                    else STREAM_STDERR
                )
                detail = _mask_cleanup_urls(
                    _render_close_failure(error, secrets, tail_projector=tail_projector)
                )
                raised = StreamReaderFailure(
                    tag, type(error).__name__, detail, TRUNCATION_NOTICE
                )
            else:
                message = _mask_cleanup_urls(
                    _render_close_failure(error, secrets, tail_projector=tail_projector)
                )
                raised = error
                if message != str(error):
                    if selected is not None and selected.kind is FailureKind.UNEXPECTED:
                        # Retain exact defect authority even when its display
                        # needs masking. Result records freeze references, not
                        # exception internals; domain sanitization owns text.
                        if isinstance(error, OSError) and error.strerror is not None:
                            project = (
                                tail_projector
                                if tail_projector is not None
                                else redact_tail
                            )

                            def safe(value):
                                return _mask_cleanup_urls(
                                    project(
                                        str(value),
                                        secrets,
                                        tail_bytes=READER_FAILURE_DETAIL_BYTES // 3,
                                    )
                                )

                            error.strerror = safe(error.strerror)
                            for attribute in ("filename", "filename2"):
                                value = getattr(error, attribute, None)
                                if value is not None:
                                    setattr(error, attribute, safe(value))
                            error.args = (error.errno, error.strerror)
                        else:
                            object.__setattr__(error, "args", (message,))
                    else:
                        try:
                            raised = type(error)(message)
                        except Exception:
                            raised = RuntimeError(message)
            original_notes = (
                tuple(getattr(error, "__notes__", ())) if not failures else ()
            )
            raised.__notes__ = []
            project = tail_projector if tail_projector is not None else redact_tail
            for note in original_notes:
                raised.add_note(
                    _mask_cleanup_urls(
                        project(
                            str(note), secrets, tail_bytes=READER_FAILURE_DETAIL_BYTES
                        )
                    )
                )
            for failure in failures:
                if failure.kind is FailureKind.INTERRUPTION:
                    continue
                if failure.error is not error or isinstance(
                    raised, AssemblyTimeoutError
                ):
                    raised.add_note(
                        _process_cleanup_prefix(failure)
                        + ": "
                        + _mask_cleanup_urls(
                            _render_close_failure(
                                failure.error, secrets, tail_projector=tail_projector
                            )
                        )
                    )
            if not complete:
                raised.add_note("local process cleanup incomplete")
                if isinstance(owner, OwnedProcess):
                    try:
                        object.__setattr__(raised, "cleanup", owner)
                    except Exception:
                        pass
            _attach_retained_capture(raised, capture)
            raise raised from None

        action = _ContainerRemovalAction(container_name, grace_seconds, cleanup_outcome)
        policy = StopPolicy(
            (StopStage(3 * grace_seconds, action, local_stop_before=True),),
            3 * grace_seconds,
        )
        try:
            owner = OwnedProcess.launch(
                argv,
                factory=subprocess.Popen,
                deadline_seconds=deadline_seconds,
                grace_seconds=_streaming_cleanup_budget(grace_seconds),
                stop_policy=policy,
                reporter=report,
                on_launched=launched,
            )
            result = owner.stream(chunk, on_stream_end=end)
        except BaseException as exc:
            if collector is not None:
                capture = collector.finish()
            handle = owner if owner is not None else getattr(exc, "cleanup", None)
            status = handle.cleanup_status if isinstance(handle, OwnedProcess) else None
            if isinstance(handle, OwnedProcess):
                owner = handle
            if not isinstance(exc, Exception) and isinstance(handle, OwnedProcess):
                try:
                    object.__setattr__(exc, "cleanup", handle)
                except BaseException:
                    pass  # Preserve the actual interruption over context attachment.
            fail(
                exc,
                status.failures if status is not None else (),
                complete=status.complete if status is not None else True,
            )
        if collector is not None:
            capture = collector.finish()
        if result.lifecycle.failures:
            fail(
                result.lifecycle.failures[0].error,
                result.lifecycle.failures,
                timed_out=result.timed_out,
                complete=result.lifecycle.complete,
            )
        if result.timed_out:
            assert deadline_seconds is not None
            timeout = _timeout_error(
                deadline_seconds,
                stdout=capture.stdout_tail,
                stderr=capture.stderr_tail,
                truncation_notice=capture.truncation_notice,
            )
            _attach_retained_capture(timeout, capture)
            raise timeout
        assert result.returncode is not None
        return ProcessResult(
            argv,
            result.returncode,
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
    pre-existing entry is never followed or overwritten.  The descriptor is
    transferred immediately into an ``OwnedDescriptor``, which becomes the
    sole release authority: an active write failure stays primary over an
    ordinary close failure and a successful release is terminal and
    at-most-once.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    path = staging / "package-lock.json"
    ops = PosixDescriptorOps()
    label = f"lockfile input {path}"
    try:
        fd = os.open(str(path), flags, 0o444)
    except OSError as exc:
        raise LockedNpmError(
            "unsafe_staging_path",
            f"cannot write lockfile input {path}: {exc}",
        ) from exc
    owner = OwnedDescriptor(ops, fd, label=label)
    with owner:
        os.write(owner.fd, lockfile_bytes)


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
            f"cleanup failure ({failure.operation}): {failure.reason}: {failure.detail}"
        )


def default_stream_factory(
    secrets: Sequence[str],
    network_url_display: NetworkUrlDisplay,
):
    """Return the mode-selected per-stream factory for standalone assembly.

    The host-side Pi assembler injects its own fingerprinted factory; every
    other caller (direct, SDK, or injected) gets the same selected local
    representation so exactly one retained tail is mode-consistent at this
    boundary.  The import is deferred so the neutral assembler does not depend
    on the host presentation package at import time.
    """
    from docker.versioning.npm_diagnostic_stream import make_stream_factory

    return make_stream_factory(secrets, network_url_display=network_url_display)


def default_tail_projector(network_url_display: NetworkUrlDisplay):
    """Return the mode-selected bounded tail projector for standalone assembly."""
    from docker.versioning.npm_diagnostic_stream import project_tail

    return functools.partial(project_tail, network_url_display=network_url_display)


def _streaming_kwargs(
    runner,
    secrets: Sequence[str],
    sink: DiagnosticSink | None,
    container_name: str,
    cleanup_outcome: _StreamingCleanupOutcome,
    stream_factory=None,
    tail_projector=None,
    network_url_display=None,
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
    except TypeError, ValueError:
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
    if "network_url_display" in params:
        kwargs["network_url_display"] = network_url_display
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
    network_url_display: NetworkUrlDisplay | None = None,
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
    if network_url_display is None:
        network_url_display = NetworkUrlDisplay.REDACTED
    if not isinstance(network_url_display, NetworkUrlDisplay):
        raise ValueError("network_url_display must be a NetworkUrlDisplay")
    recheck_assembler_bindings(assembler)
    input_identity = compute_assembler_input_identity(validated, assembler)

    policy_secrets = (
        corporate_network.secrets() if corporate_network is not None else ()
    )
    effective_secrets = tuple(secrets) + policy_secrets
    # Bind the request selector into the collector configuration before any
    # stream capture.  An injected host-side factory/projector (Pi assembly)
    # wins; otherwise the selected local representation is built here so the
    # one retained tail and every failure detail are mode-consistent.
    if stream_factory is None:
        stream_factory = default_stream_factory(effective_secrets, network_url_display)
    if tail_projector is None:
        tail_projector = default_tail_projector(network_url_display)

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
            executor_failure_tail = ""
            executor_failure_stream: str | None = None
            executor_failure_notes: tuple[str, ...] = ()
            executor_failure_capture: StreamingCapture | None = None
            executor_failure_cleanup: OwnedProcess | None = None
            truncation_notice: str | None = None
            streaming_runner = getattr(executor, "run_streaming", None)
            project = tail_projector if tail_projector is not None else redact_tail
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
                        network_url_display,
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
                if not isinstance(exc, Exception):
                    raise
                # Capture only the sanitized detail here.  The structured error
                # is raised after leaving the except block so Python does not
                # assign the original exception to __context__.  The detail is
                # routed through the same URL-free tail projector used for
                # assembled output so an executor exception can never leak a
                # URL into the failure representation.  A failed collection's
                # already-selected retained capture is copied verbatim, never
                # re-projected, so host-path/exact content is preserved.
                executor_failure_detail = project(
                    f"{type(exc).__name__}: {str(exc) or repr(exc)}",
                    effective_secrets,
                )
                executor_failure_tail = getattr(exc, "diagnostic_tail", "")
                executor_failure_stream = getattr(exc, "diagnostic_stream", None)
                executor_failure_notes = tuple(
                    project(str(note), effective_secrets)
                    for note in getattr(exc, "__notes__", ())[:32]
                )
                executor_failure_capture = _attached_retained_capture(exc)
                handle = getattr(exc, "cleanup", None)
                if isinstance(handle, OwnedProcess):
                    executor_failure_cleanup = handle

            if executor_failure_detail is not None:
                failure = LockedNpmError(
                    "executor_failure",
                    executor_failure_detail,
                    diagnostic_tail=executor_failure_tail,
                    diagnostic_stream=executor_failure_stream,
                )
                for note in executor_failure_notes:
                    failure.add_note(note)
                if executor_failure_capture is not None:
                    _attach_retained_capture(failure, executor_failure_capture)
                if executor_failure_cleanup is not None:
                    object.__setattr__(failure, "cleanup", executor_failure_cleanup)
                raise failure from None
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
