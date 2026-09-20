"""Bounded redacted streaming for the npm assembler executor.

Phase 2 of ``bound-pi-assembly-execution`` replaces all-at-once assembler
capture with concurrent stdout/stderr draining.  This module owns:

* the deterministic, caller-order-independent leftmost-longest multi-pattern
  redactor;
* the incremental UTF-8 decoder (split multibyte characters stay intact);
* the bounded 64 KiB diagnostic tail retained independently per stream; and
* the single serialized sink dispatcher behind a non-blocking 64-chunk queue.

The redactor is order-independent: at the leftmost position where any
configured secret matches, it selects the *longest* complete match and
replaces it with exactly one marker.  Streaming never emits a candidate
secret prefix or suffix before the full match can be determined: it retains
``longest_secret - 1`` decoded characters of overlap and only commits a
match once a full ``longest_secret`` window follows its start (or at EOF).
"""

from __future__ import annotations

import codecs
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Iterable, Protocol, Sequence

REDACTED = "<redacted>"

#: Fixed literal recording that live output was not fully delivered.  It is
#: redaction-safe by construction: it contains no caller-supplied value.
TRUNCATION_NOTICE = "<live output truncated>"

STREAM_STDOUT = "stdout"
STREAM_STDERR = "stderr"

#: Maximum bytes read from one pipe at a time.
READ_CHUNK_BYTES = 16 * 1024

#: Maximum retained diagnostic tail per stream (bytes of UTF-8).
TAIL_BYTES = 64 * 1024

#: Maximum retained bytes for a reader-failure diagnostic.
READER_FAILURE_DETAIL_BYTES = 4 * 1024

#: Maximum number of queued chunks awaiting sink delivery.
SINK_QUEUE_CAPACITY = 64

#: Supported maximum duration of one constructor-owned sink invocation.
SINK_CALLBACK_BUDGET_SECONDS = 0.100

#: Supported maximum total dispatcher draining after reader completion.
DISPATCHER_DRAIN_BUDGET_SECONDS = 10.0

#: Internal sentinel terminating the dispatcher queue.
_SENTINEL = object()


def dedupe_secrets(secrets: Sequence[str]) -> tuple[str, ...]:
    """Return non-empty *secrets* deduplicated, preserving first occurrence."""
    seen: set[str] = set()
    result: list[str] = []
    for secret in secrets:
        if secret and secret not in seen:
            seen.add(secret)
            result.append(secret)
    return tuple(result)


def longest_secret_length(secrets: Sequence[str]) -> int:
    """Return the length of the longest non-empty secret (0 when none)."""
    return max((len(secret) for secret in secrets if secret), default=0)


def redact_text(text: str, secrets: Sequence[str]) -> str:
    """Deterministically redact *text* with leftmost-longest matching.

    Caller-order-independent: reordering *secrets* never changes the result.
    At the leftmost position where any secret matches, the longest complete
    match is replaced by exactly one ``REDACTED`` marker and the match is
    consumed whole, so no suffix of a shorter alternative is exposed.
    """
    clean = dedupe_secrets(secrets)
    if not clean:
        return text
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        match_len = 0
        for secret in clean:
            if text.startswith(secret, i) and len(secret) > match_len:
                match_len = len(secret)
        if match_len:
            out.append(REDACTED)
            i += match_len
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


class RedactingStream:
    """Incrementally decodes and redacts one byte stream.

    Feeds raw bytes through an incremental UTF-8 decoder, redacts the decoded
    characters with the deterministic leftmost-longest matcher, emits safe
    prefixes immediately (no newline dependence), flushes decoder state and
    the final partial record at EOF, and retains a bounded redacted tail.
    """

    def __init__(self, secrets: Sequence[str], *, tail_bytes: int = TAIL_BYTES):
        self._secrets = dedupe_secrets(secrets)
        self._longest = longest_secret_length(self._secrets)
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._pending = ""
        self._eof = False
        self._tail_bytes_limit = tail_bytes
        self._tail: deque[str] = deque()
        self._tail_byte_count = 0
        self._redact_unresolved = False

    # -- public surface -------------------------------------------------

    def feed_bytes(self, data: bytes) -> tuple[str, ...]:
        """Feed raw bytes; return the emitted safe redacted chunks."""
        if not data:
            return ()
        text = self._decoder.decode(data, final=False)
        return self._feed_text(text)

    def finish(self, *, abort: bool = False) -> tuple[str, ...]:
        """Flush the decoder and any pending overlap; return final chunks.

        With *abort* (reader failure rather than clean EOF), any pending
        overlap that cannot be resolved to a complete non-secret is replaced
        by one redaction marker so a candidate secret prefix or suffix is
        never emitted during decoder flushing.
        """
        if self._eof:
            return ()
        self._eof = True
        self._redact_unresolved = abort
        text = self._decoder.decode(b"", final=True)
        return self._feed_text(text)

    def tail(self) -> str:
        """Return the retained (bounded, redacted) diagnostic tail."""
        return "".join(self._tail)

    @property
    def pending_size(self) -> int:
        """Number of decoded characters currently held as overlap."""
        return len(self._pending)

    # -- internals ------------------------------------------------------

    def _feed_text(self, text: str) -> tuple[str, ...]:
        if text:
            self._pending += text
        chunks: list[str] = []
        while self._pending:
            match = self._leftmost_longest(self._pending)
            if match is None:
                if self._eof:
                    chunks.append(
                        REDACTED if self._redact_unresolved else self._pending
                    )
                    self._pending = ""
                    break
                keep = max(0, self._longest - 1)
                if keep == 0:
                    chunks.append(self._pending)
                    self._pending = ""
                    break
                if len(self._pending) <= keep:
                    break
                emit = self._pending[:-keep]
                chunks.append(emit)
                self._pending = self._pending[-keep:]
                break
            start, end = match
            prefix = self._pending[:start]
            if prefix:
                chunks.append(prefix)
            committed = self._eof or (
                len(self._pending) - start >= self._longest
            )
            if committed:
                chunks.append(REDACTED)
                self._pending = self._pending[end:]
                continue
            self._pending = self._pending[start:]
            break
        for chunk in chunks:
            self._append_tail(chunk)
        return tuple(chunks)

    def _leftmost_longest(self, text: str) -> tuple[int, int] | None:
        for i in range(len(text)):
            matched_len = 0
            for secret in self._secrets:
                if text.startswith(secret, i) and len(secret) > matched_len:
                    matched_len = len(secret)
            if matched_len:
                return i, i + matched_len
        return None

    def _append_tail(self, text: str) -> None:
        for ch in text:
            self._tail.append(ch)
            self._tail_byte_count += len(ch.encode("utf-8"))
        while self._tail_byte_count > self._tail_bytes_limit and self._tail:
            ch = self._tail.popleft()
            self._tail_byte_count -= len(ch.encode("utf-8"))


def redact_tail(
    text: str, secrets: Sequence[str], *, tail_bytes: int = TAIL_BYTES
) -> str:
    """Redact *text* and return its bounded diagnostic tail.

    Uses the exact same streaming redaction and tail retention as live
    streaming so the non-streaming fallback path is byte-for-byte
    consistent with the streaming path.
    """
    stream = RedactingStream(secrets, tail_bytes=tail_bytes)
    stream.feed_bytes(text.encode("utf-8"))
    stream.finish()
    return stream.tail()


@dataclass(frozen=True)
class StreamChunk:
    """One already-projected, stream-tagged diagnostic chunk.

    ``text`` is always URL-free safe text.  ``hostnames`` and
    ``url_fingerprints`` are populated only by the structured collector branch;
    the retained tail never carries them.
    """

    stream: str
    """``"stdout"`` or ``"stderr"``."""

    text: str
    """URL-free safe text chunk (never a complete secret or URL)."""

    hostnames: tuple[str, ...] = ()
    """Normalized hostnames removed from ``text`` (structured branch only)."""

    url_fingerprints: tuple[str, ...] = ()
    """Ordered ephemeral URL fingerprints (structured branch only)."""


class DiagnosticSink(Protocol):
    """Constructor-owned prompt-returning diagnostic callback."""

    def __call__(self, chunk: StreamChunk) -> None:
        """Handle one redacted chunk; return promptly (no blocking I/O)."""
        ...


class DiagnosticStream(Protocol):
    """Per-stream incremental decoder/projector consumed by the collector.

    ``feed_bytes`` and ``finish`` may yield either plain ``str`` fragments
    (the default :class:`RedactingStream`) or fully formed
    :class:`StreamChunk` items (the structured projection branch).  ``tail``
    returns the retained, already-projected tail for that stream.
    """

    def feed_bytes(self, data: bytes) -> Iterable[str | StreamChunk]: ...

    def finish(self, *, abort: bool = False) -> Iterable[str | StreamChunk]: ...

    def tail(self) -> str: ...


@dataclass(frozen=True)
class StreamingCapture:
    """Bounded outcome of draining two byte pipes."""

    stdout_tail: str
    """Retained (bounded, redacted) stdout tail."""

    stderr_tail: str
    """Retained (bounded, redacted) stderr tail."""

    truncation_notice: str | None
    """``None`` when every accepted chunk was delivered; otherwise the fixed
    redacted truncation notice recording live-delivery loss."""


class StreamReaderFailure(Exception):
    """Structured failure raised by :func:`collect_streams` after cleanup.

    Raised only after both reader threads have joined and the dispatcher has
    been finalized: the sibling stream was still drained, live delivery was
    stopped, and no reader or dispatcher worker remains.  ``detail`` is
    bounded and redacted; ``truncation_notice`` records that live output was
    truncated.
    """

    stream: str
    """``"stdout"`` or ``"stderr"`` — the stream whose reader failed."""

    exception_type: str
    """The reader exception class name (e.g. ``"OSError"``)."""

    detail: str
    """Bounded, redacted diagnostic describing the reader exception."""

    truncation_notice: str
    """The fixed truncation notice recording lost live output."""

    def __init__(
        self,
        stream: str,
        exception_type: str,
        detail: str,
        truncation_notice: str,
    ) -> None:
        super().__init__(detail)
        self.stream = stream
        self.exception_type = exception_type
        self.detail = detail
        self.truncation_notice = truncation_notice

    def __str__(self) -> str:
        return (
            f"{self.stream} reader failed ({self.exception_type}): "
            f"{self.detail} {self.truncation_notice}"
        )


class SinkDispatcher:
    """Serializes sink invocations through a bounded queue on one thread.

    Pipe readers never invoke the sink directly; they submit chunks with
    non-blocking writes.  One dispatcher thread invokes the sink for every
    accepted chunk in queue-acceptance order.  A slow-but-returning sink may
    lag only within the queue bound; dropping is restricted to queue
    overflow and sink-contract failure, and any loss is recorded exactly once
    in the retained truncation notice.

    A sink callback that returns later than the callback budget is a
    sink-contract failure and disables further live delivery.  A callback
    that never returns is unsupported: Python cannot safely cancel a running
    callback thread, so Constructor does not promise lifecycle cleanup for
    an arbitrary indefinitely blocking callback.
    """

    def __init__(
        self,
        sink: DiagnosticSink,
        *,
        queue_capacity: int = SINK_QUEUE_CAPACITY,
        callback_budget: float = SINK_CALLBACK_BUDGET_SECONDS,
        drain_budget: float = DISPATCHER_DRAIN_BUDGET_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        queue_factory: Callable[[int], queue.Queue] = queue.Queue,
    ):
        if sink is None:
            raise ValueError("sink must not be None")
        self._sink = sink
        self._queue: queue.Queue = queue_factory(queue_capacity)
        self._callback_budget = callback_budget
        self._drain_budget = drain_budget
        self._clock = clock
        self._lock = threading.Lock()
        self._failed = False
        self._truncation_notice: str | None = None
        self._accepting = True
        self._draining = False
        self._drain_start = 0.0
        #: Observable seam: set once finalization begins draining.
        self.draining_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="npm-sink-dispatcher", daemon=True
        )
        self._thread.start()

    # -- reader side ----------------------------------------------------

    def submit(self, chunk: StreamChunk) -> bool:
        """Non-blocking submit.  Returns ``False`` when the chunk is dropped
        (queue overflow or an already-failed sink).

        The acceptance decision and the queue insertion share one critical
        section: once acceptance passes, the chunk is enqueued atomically, so
        ``finish`` cannot close acceptance and enqueue its sentinel in
        between.  A ``True`` return therefore guarantees the chunk precedes
        the sentinel and is drained during normal finalization.
        """
        with self._lock:
            if self._failed or not self._accepting:
                self._record_truncation_locked()
                return False
            try:
                self._queue.put_nowait(chunk)
            except queue.Full:
                self._record_truncation_locked()
                return False
            return True

    # -- finalization ---------------------------------------------------

    def finish(self) -> str | None:
        """Stop accepting, drain accepted chunks, join, return the notice.

        Normal finalization begins only after both readers finish; every
        accepted chunk is then drained in order to a compliant sink before
        this method returns.  The 100 ms per-callback contract and the total
        drain budget are enforced; violation becomes sink failure with one
        retained truncation notice.
        """
        with self._lock:
            if not self._accepting:
                return self._truncation_notice
            self._accepting = False
            self._draining = True
            self._drain_start = self._clock()
        self.draining_event.set()
        # Bounded finalization: signal no-more-chunks, then join.  The
        # dispatcher checks the drain budget between callbacks; the join
        # allows one final compliant callback so the thread exits without a
        # live callback outliving executor return.
        try:
            self._queue.put(_SENTINEL, timeout=self._drain_budget)
        except queue.Full:
            self._fail()
        self._thread.join(
            timeout=self._drain_budget + self._callback_budget
        )
        if self._thread.is_alive():
            self._fail()
        return self._truncation_notice

    def truncation_notice(self) -> str | None:
        """Return the retained truncation notice, or ``None``."""
        with self._lock:
            return self._truncation_notice

    def is_alive(self) -> bool:
        """Return whether the dispatcher thread is still running."""
        return self._thread.is_alive()

    # -- dispatcher thread ----------------------------------------------

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is _SENTINEL:
                return
            with self._lock:
                if self._failed:
                    # Already failed; discard this chunk silently.
                    continue
                draining = self._draining
                drain_start = self._drain_start
            if draining and (
                self._clock() - drain_start >= self._drain_budget
            ):
                self._fail()
                self._discard_remaining()
                return
            self._deliver(item)
            with self._lock:
                failed = self._failed
            if failed:
                self._discard_remaining()
                return

    def _deliver(self, chunk: StreamChunk) -> None:
        begin = self._clock()
        try:
            self._sink(chunk)
        except BaseException:
            self._fail()
            return
        if self._clock() - begin > self._callback_budget:
            self._fail()

    def _discard_remaining(self) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            if item is _SENTINEL:
                return

    def _fail(self) -> None:
        with self._lock:
            self._failed = True
            if self._truncation_notice is None:
                self._truncation_notice = TRUNCATION_NOTICE

    def _record_truncation_locked(self) -> None:
        # Caller holds self._lock.
        if self._truncation_notice is None:
            self._truncation_notice = TRUNCATION_NOTICE


def _redacted_exception_detail(
    exc: BaseException, secrets: Sequence[str], tail_projector=None
) -> str:
    """Render *exc* — including any attached notes — as bounded safe detail."""
    parts = [str(exc) or repr(exc)]
    notes = getattr(exc, "__notes__", ())
    parts.extend(str(note) for note in notes)
    project = tail_projector if tail_projector is not None else redact_tail
    return project(
        "; ".join(parts), secrets, tail_bytes=READER_FAILURE_DETAIL_BYTES
    )


def collect_streams(
    *,
    stdout_read: Callable[[int], bytes],
    stderr_read: Callable[[int], bytes],
    secrets: Sequence[str] = (),
    sink: DiagnosticSink | None = None,
    on_reader_failure: Callable[[str], None] | None = None,
    on_interruption: Callable[[], None] | None = None,
    stream_factory: Callable[[str], DiagnosticStream] | None = None,
    tail_projector: Callable[..., str] | None = None,
    abort_event: threading.Event | None = None,
) -> StreamingCapture:
    """Drain two byte pipes concurrently through redacting streams.

    *stdout_read*/*stderr_read* are blocking readers accepting a maximum
    byte count and returning ``b""`` at EOF.  Both pipes are drained
    concurrently so a producer cannot deadlock on either pipe; redacted safe
    prefixes are emitted immediately; retained tails are bounded.  When
    *sink* is not ``None``, redacted chunks are dispatched through one
    serialized queue; otherwise no live output is produced.

    A failed reader records a bounded, redacted :class:`StreamReaderFailure`
    and reports it to the caller *immediately* — before the sibling reader
    is joined — so a blocked sibling can be unblocked rather than waited on
    indefinitely.  *on_reader_failure*, when given, is invoked on the calling
    thread with the failing stream name (``STREAM_STDOUT`` or
    ``STREAM_STDERR``) as soon as the failure is recorded; a process-backed
    caller should close the failed pipe and terminate/reap the producer
    there, which lets the sibling reader reach EOF and drain its remaining
    EOF-safe data.  The sibling is then joined normally, the dispatcher
    finalizes, and only then is the failure raised — so the exception is
    never surfaced before worker cleanup.  If *on_reader_failure* raises,
    its error is preserved as bounded, redacted secondary context on the
    raised failure rather than displacing it, and reader and dispatcher
    cleanup still complete first.

    A control-flow interruption (``KeyboardInterrupt``/``SystemExit``)
    delivered to this coordinating thread is handled the same way:
    *on_interruption*, when given, is invoked to terminate/reap the producer
    so the blocked readers reach EOF, both readers are joined, the dispatcher
    is finalized, and only then is the interruption re-raised.  A raising
    *on_interruption* hook — including one that raises its own
    ``KeyboardInterrupt``/``SystemExit`` — never masks the interruption; its
    error becomes a bounded, redacted note.  Callers without
    *on_interruption* must ensure the readers are independently unblocked,
    otherwise the interruption handler cannot join them.

    *abort_event* is the shared cancellation signal visible to both reader
    workers.  A caller that can force the readers to EOF (a deadline
    supervisor terminating the producer, for example) receives the same event
    and sets it before terminating the process or closing the pipes; the
    collector itself sets it before unblocking a blocked sibling on reader
    failure and before invoking *on_interruption*.  Any reader that reaches
    EOF while the signal is set finalizes with ``abort=True`` so a forced EOF
    can never flush an ambiguous secret/URL prefix as ordinary text, while a
    reader that genuinely reaches a clean, unsignalled EOF still finalizes
    unchanged.  When ``None``, a private event is created.
    """
    if abort_event is None:
        abort_event = threading.Event()
    stdout_stream = (
        stream_factory(STREAM_STDOUT)
        if stream_factory is not None
        else RedactingStream(secrets)
    )
    stderr_stream = (
        stream_factory(STREAM_STDERR)
        if stream_factory is not None
        else RedactingStream(secrets)
    )
    dispatcher = SinkDispatcher(sink) if sink is not None else None

    failures: list[StreamReaderFailure] = []
    failures_lock = threading.Lock()
    #: Set on the first reader failure or once both readers have finished.
    terminal = threading.Event()
    done = 0
    done_lock = threading.Lock()

    def record_failure(stream: str, exc: Exception) -> None:
        detail = _redacted_exception_detail(exc, secrets, tail_projector)
        # Signal cancellation before the coordinator unblocks a blocked
        # sibling (via *on_reader_failure*): the forced EOF must finalize
        # with ``abort=True`` rather than flushing an ambiguous prefix.
        abort_event.set()
        with failures_lock:
            failures.append(
                StreamReaderFailure(
                    stream, type(exc).__name__, detail, TRUNCATION_NOTICE
                )
            )
        terminal.set()

    def note_done() -> None:
        nonlocal done
        with done_lock:
            done += 1
            if done == 2:
                terminal.set()

    def submit_chunk(tag: str, chunk: str | StreamChunk) -> None:
        if dispatcher is not None:
            if isinstance(chunk, StreamChunk):
                dispatcher.submit(chunk)
            else:
                dispatcher.submit(StreamChunk(tag, chunk))

    def drain(
        read_fn: Callable[[int], bytes], stream: DiagnosticStream, tag: str
    ) -> None:
        failed = False
        try:
            while True:
                data = read_fn(READ_CHUNK_BYTES)
                if not data:
                    break
                for chunk in stream.feed_bytes(data):
                    submit_chunk(tag, chunk)
        except Exception as exc:
            # Record the failure once, signal the coordinator immediately,
            # and stop reading this stream; the sibling reader keeps
            # draining.  Do not raise from this worker: the coordinator
            # reports the failure, the caller unblocks the sibling, and both
            # readers are joined before the structured failure is surfaced.
            failed = True
            record_failure(tag, exc)
        # A clean EOF uses ``abort=False``; a reader failure or any shared
        # cancellation (interruption, deadline termination) uses
        # ``abort=True`` so a pending candidate fails closed instead of being
        # flushed as ordinary text.
        for chunk in stream.finish(abort=failed or abort_event.is_set()):
            submit_chunk(tag, chunk)
        note_done()

    threads = (
        threading.Thread(
            target=drain,
            args=(stdout_read, stdout_stream, STREAM_STDOUT),
            name="npm-stdout-reader",
            daemon=True,
        ),
        threading.Thread(
            target=drain,
            args=(stderr_read, stderr_stream, STREAM_STDERR),
            name="npm-stderr-reader",
            daemon=True,
        ),
    )
    for thread in threads:
        thread.start()

    # Report the first failure (or full completion) immediately rather than
    # waiting for the sibling reader to reach EOF; the callback unblocks a
    # sibling that would otherwise never finish.
    first_failure: StreamReaderFailure | None = None
    cleanup_failures: list[BaseException] = []
    interrupted: BaseException | None = None
    try:
        terminal.wait()
        with failures_lock:
            first_failure = failures[0] if failures else None
        if first_failure is not None and on_reader_failure is not None:
            try:
                on_reader_failure(first_failure.stream)
            except Exception as exc:
                # A raising callback must not escape before the reader and
                # dispatcher workers are cleaned up, nor may it displace the
                # structured reader failure; preserve it as secondary context.
                cleanup_failures.append(exc)
    except BaseException as exc:
        # Control-flow interruption delivered to this coordinating thread:
        # unblock the readers via the caller hook so they can finish, then
        # re-raise after reader join and dispatcher finalization.  A raising
        # hook — even one that raises its own KeyboardInterrupt/SystemExit —
        # never masks the interruption; it is recorded as secondary context.
        interrupted = exc
        # Signal cancellation before the hook terminates/closes so a reader
        # forced to EOF by the unblocking finalizes with ``abort=True``.
        abort_event.set()
        if on_interruption is not None:
            try:
                on_interruption()
            except BaseException as cb_exc:
                cleanup_failures.append(cb_exc)
    finally:
        # Guarantee worker cleanup even when a callback raised (e.g. pipe
        # close or client termination failed): always join both readers and
        # always finalize the dispatcher.
        for thread in threads:
            thread.join()
        notice = dispatcher.finish() if dispatcher is not None else None

    if interrupted is not None:
        for exc in cleanup_failures:
            interrupted.add_note(
                "interruption cleanup failed "
                f"({type(exc).__name__}): "
                f"{_redacted_exception_detail(exc, secrets, tail_projector)}"
            )
        raise interrupted

    if failures:
        primary = failures[0]
        for extra in failures[1:]:
            primary.add_note(f"also failed ({extra.stream}): {extra.detail}")
        for exc in cleanup_failures:
            primary.add_note(
                "on_reader_failure cleanup failed "
                f"({type(exc).__name__}): "
                f"{_redacted_exception_detail(exc, secrets, tail_projector)}"
            )
        raise primary
    return StreamingCapture(
        stdout_tail=stdout_stream.tail(),
        stderr_tail=stderr_stream.tail(),
        truncation_notice=notice,
    )


__all__ = [
    "DISPATCHER_DRAIN_BUDGET_SECONDS",
    "DiagnosticSink",
    "DiagnosticStream",
    "READ_CHUNK_BYTES",
    "READER_FAILURE_DETAIL_BYTES",
    "REDACTED",
    "RedactingStream",
    "SINK_CALLBACK_BUDGET_SECONDS",
    "SINK_QUEUE_CAPACITY",
    "STREAM_STDERR",
    "STREAM_STDOUT",
    "SinkDispatcher",
    "StreamChunk",
    "StreamReaderFailure",
    "StreamingCapture",
    "TAIL_BYTES",
    "TRUNCATION_NOTICE",
    "collect_streams",
    "dedupe_secrets",
    "longest_secret_length",
    "redact_tail",
    "redact_text",
]
