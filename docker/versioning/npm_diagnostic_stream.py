"""Bounded URL-free npm diagnostic collection for locked assembly.

Phase 8 of ``improve-host-build-observability`` bridges the existing npm
stdout/stderr collector to the shared safe diagnostic projector:

* every raw stdout/stderr chunk is observed (for diagnostic-silence reset and
  latest-activity tracking) before any redaction, URL projection, line
  assembly, or mailbox admission;
* the shared framer decodes UTF-8 incrementally, frames bounded source lines
  at 64 KiB of decoded source text (before any control expansion), neutralizes
  terminal controls per line, and only then is the URL/secret projector
  applied, so a sensitive token split across decoder/input chunks never
  reaches the structured live branch or the retained tail;
* normalized hostnames are attached to every structured line that contains
  that host (not only its first occurrence), and ephemeral ordered URL
  fingerprints are attached with multiplicity preserved; both live only on
  the finalized structured line, and the collector drains the projector's
  fact buffer after each line segment so completed-line metadata is never
  retained;
* projected text is released as a non-finalized committed prefix as soon as
  the decoder, neutralizer, and projector commit it -- before any newline --
  and the retained per-stream tail is appended from those committed prefixes
  immediately; the record boundary then emits one finalized line carrying the
  complete text and the accumulated metadata, without re-appending the text;
* the retained per-stream tail keeps its existing byte bound and truncation
  semantics, preserves the URL-free sanitized ordering and occurrences without
  grouping, and excludes discarded overflow fragments and host metadata.

Nothing here renders, coalesces diagnostics, schedules a timer, or owns a
presentation worker.  ``classify_npm_diagnostic`` is a conservative,
message-wording classifier used only to select a closed diagnostic priority;
it never derives a Constructor lifecycle state.
"""
from __future__ import annotations

import re
from collections import deque
from typing import Callable

from docker.npm_environment.streaming import (
    DIAGNOSTIC_LINE_LIMIT_BYTES,
    OVERSIZED_DIAGNOSTIC_MARKER,
    StreamChunk,
    TAIL_BYTES,
    TerminalSafeLineFramer,
    _OverflowMarker,
)
from docker.versioning.diagnostic_identity import SessionUrlIdentity
from docker.versioning.diagnostic_projection import (
    DiagnosticProjector,
    sanitize_diagnostic_text,
)
from docker.versioning.host_progress import HostDiagnosticClassification

_WARNING_RE = re.compile(r"(?i)(?:^|\s)npm\s+warn\b|(?:^|\s)warn(?:ing)?\b")
_TIMEOUT_RE = re.compile(r"(?i)\btime(?:d)?[ _-]?out\b|\btimeout\b")
_RETRY_RE = re.compile(r"(?i)\bretry\b|\bretrying\b|\battempt\b")
_ERROR_RE = re.compile(r"(?i)(?:^|\s)npm\s+error\b|\berr!\b|\berror\b")


def classify_npm_diagnostic(text: str) -> HostDiagnosticClassification:
    """Conservatively map one URL-free npm line to a closed classification.

    Classification selects presentation priority only.  It never derives a
    Constructor lifecycle state, and unknown lines fall through to ``STATUS``
    rather than being treated as trusted or as a failure reason.  Timeout and
    retry wording take precedence over the generic ``npm error`` prefix so a
    timed-out network line is not collapsed into a plain error.
    """
    if not isinstance(text, str):
        raise TypeError("diagnostic text must be a string")
    if _WARNING_RE.search(text):
        return HostDiagnosticClassification.WARNING
    if _TIMEOUT_RE.search(text):
        return HostDiagnosticClassification.TIMEOUT
    if _RETRY_RE.search(text):
        return HostDiagnosticClassification.RETRY
    if _ERROR_RE.search(text):
        return HostDiagnosticClassification.ERROR
    return HostDiagnosticClassification.STATUS


def _bounded_tail(text: str, tail_bytes: int) -> str:
    """Return the last *tail_bytes* UTF-8 bytes of *text* on character edges."""
    if tail_bytes <= 0 or not text:
        return ""
    kept: list[str] = []
    size = 0
    for character in reversed(text):
        width = len(character.encode("utf-8"))
        if size + width > tail_bytes:
            break
        kept.append(character)
        size += width
    kept.reverse()
    return "".join(kept)


def project_tail(
    text: str, secrets=(), *, tail_bytes: int = TAIL_BYTES
) -> str:
    """Return bounded, secret-redacted, URL-free text for a retained tail.

    The signature matches :func:`docker.npm_environment.streaming.redact_tail`
    so callers can inject it wherever the merely secret-redacted tail was used
    and guarantee that no pre-projection text reaches a returned, attached,
    persisted, or failure representation.  The text is decoded, framed, and
    terminal-neutralized in the same order as the streaming collector.
    """
    framer = TerminalSafeLineFramer()
    neutralized = "".join(framer.feed_text(text))
    neutralized += "".join(framer.finish())
    safe, _hostnames = sanitize_diagnostic_text(neutralized, secrets)
    return _bounded_tail(safe, tail_bytes)


class NpmDiagnosticStream:
    """Incremental bounded framer, URL-free projector, and bounded tail.

    ``feed_bytes`` returns two kinds of :class:`StreamChunk`:

    * a committed safe prefix (``finalized=False``) as soon as the decoder,
      terminal neutralizer, and URL/secret projector commit projected text --
      without waiting for a newline -- which is also appended to the retained
      tail immediately;
    * a finalized diagnostic line (``finalized=True``) at a newline or at EOF,
      carrying the complete URL-free line text plus the normalized hostnames
      and ordered URL fingerprints gathered while that logical line was open.

    A committed prefix is never a complete npm diagnostic: complete-line
    parsing, classification, identity, and grouping must consume only
    finalized lines.  ``tail`` returns the retained URL-free text with the
    existing per-stream byte bound; the finalized line never re-appends its
    text, so a prefix is retained exactly once.
    """

    def __init__(
        self,
        stream: str,
        secrets=(),
        *,
        fingerprinter: SessionUrlIdentity | None = None,
        tail_bytes: int = TAIL_BYTES,
        line_limit: int = DIAGNOSTIC_LINE_LIMIT_BYTES,
        on_chunk: Callable[[], None] | None = None,
    ) -> None:
        if stream not in ("stdout", "stderr"):
            raise ValueError("stream must be 'stdout' or 'stderr'")
        if tail_bytes <= 0:
            raise ValueError("tail_bytes must be positive")
        if line_limit <= 0:
            raise ValueError("line_limit must be positive")
        if on_chunk is not None and not callable(on_chunk):
            raise TypeError("on_chunk must be callable or None")
        self._stream = stream
        self._framer = TerminalSafeLineFramer(line_limit_bytes=line_limit)
        self._projector = DiagnosticProjector(secrets, url_identity=fingerprinter)
        self._on_chunk = on_chunk
        self._tail_bytes_limit = tail_bytes
        self._line_chars: list[str] = []
        self._line_overflowed = False
        self._eof = False
        self._tail: deque[str] = deque()
        self._tail_byte_count = 0
        # Per-line facts consumed incrementally from the projector so the
        # projector never retains completed-line metadata.
        self._line_hostnames: list[str] = []
        self._line_fingerprints: list[str] = []

    # -- public surface -------------------------------------------------

    def feed_bytes(self, data: bytes) -> tuple[StreamChunk, ...]:
        """Observe one raw chunk and return committed prefixes and lines."""
        if self._eof:
            raise RuntimeError("stream is already finished")
        if not data:
            return ()
        if self._on_chunk is not None:
            self._on_chunk()
        chunks: list[StreamChunk] = []
        for text in self._framer.feed_bytes(data):
            chunks.extend(self._emit_neutralized(text))
        return tuple(chunks)

    def finish(self, *, abort: bool = False) -> tuple[StreamChunk, ...]:
        """Flush the framer and projector, finalizing any pending line."""
        if self._eof:
            return ()
        self._eof = True
        chunks: list[StreamChunk] = []
        for text in self._framer.finish(abort=abort):
            chunks.extend(self._emit_neutralized(text))
        projected = self._projector.finish(abort=abort)
        self._absorb_projector_facts()
        for text in projected:
            chunks.extend(self._emit_projected(text))
        if (
            self._line_chars
            or self._line_hostnames
            or self._line_fingerprints
        ):
            chunks.append(self._finalize_line())
        return tuple(chunks)

    def tail(self) -> str:
        """Return the retained URL-free, bounded diagnostic tail."""
        return "".join(self._tail)

    @property
    def pending_sanitizer_bytes(self) -> int:
        """Retained ambiguous URL/secret pending state in UTF-8 bytes."""
        return self._projector.pending_size

    @property
    def pending_line_bytes(self) -> int:
        """Retained decoded source-line bytes for the current line."""
        return self._framer.pending_line_bytes

    # -- internals ------------------------------------------------------

    def _emit_neutralized(self, text: str) -> list[StreamChunk]:
        """Project one already-neutralized source segment and assemble chunks."""
        if isinstance(text, _OverflowMarker):
            # The framer appended its one fixed marker for this line: every
            # chunk of the truncated line is tagged so complete-line consumers
            # refuse to classify or group it.  A source line that merely
            # contains the marker's characters is an ordinary ``str``.
            self._line_overflowed = True
        chunks: list[StreamChunk] = []
        start = 0

        while start < len(text):
            newline = text.find("\n", start)
            end = len(text) if newline == -1 else newline + 1
            projected = self._projector.feed_text(text[start:end])
            self._absorb_projector_facts()
            for projected_text in projected:
                chunks.extend(self._emit_projected(projected_text))
            start = end
        return chunks

    def _emit_projected(self, text: str) -> list[StreamChunk]:
        """Deliver committed safe prefixes and finalized line events.

        Projected text is delivered immediately as a non-finalized committed
        prefix -- and retained in the bounded tail -- as soon as the projector
        commits it.  A newline is the record boundary: it is appended to the
        retained tail, finalizes the accumulated logical line together with its
        metadata, and resets line-local state.  ``_finalize_line`` never
        re-appends the line text, so a prefix retained once is not retained
        twice when the line completes.
        """
        chunks: list[StreamChunk] = []
        start = 0
        while start < len(text):
            newline = text.find("\n", start)
            if newline == -1:
                segment = text[start:]
                if segment:
                    chunks.append(self._emit_prefix(segment))
                break
            segment = text[start:newline]
            if segment:
                chunks.append(self._emit_prefix(segment))
            self._append_tail("\n")
            if (
                self._line_chars
                or self._line_hostnames
                or self._line_fingerprints
            ):
                chunks.append(self._finalize_line())
            else:
                # An empty logical line preserves only its record boundary.
                self._resync_line_facts()
            start = newline + 1
        return chunks

    def _emit_prefix(self, segment: str) -> StreamChunk:
        """Accumulate, immediately retain, and return one committed prefix."""
        self._line_chars.append(segment)
        self._append_tail(segment)
        return StreamChunk(
            self._stream, segment, overflowed=self._line_overflowed,
            finalized=False,
        )

    def _finalize_line(self) -> StreamChunk:
        text = "".join(self._line_chars)
        hostnames = tuple(self._line_hostnames)
        fingerprints = tuple(self._line_fingerprints)
        overflowed = self._line_overflowed
        self._reset_line_facts()
        return StreamChunk(
            self._stream, text, hostnames, fingerprints, overflowed=overflowed
        )

    def _absorb_projector_facts(self) -> None:
        """Consume newly extracted facts for the current diagnostic line.

        The projector's facts are drained on every segment, so its buffers
        never retain metadata for completed lines.
        """
        hostnames, fingerprints = self._projector.take_facts()
        # Host/path facts are internal-presentation metadata.  Binding the one
        # selected local representation is Phase 6 work; draining them here
        # keeps the projector's per-line metadata bounded in the meantime.
        self._projector.take_host_paths()
        for hostname in hostnames:
            # Deduplicate within the line only; the same host is re-attached
            # on any subsequent line that contains it.
            if hostname not in self._line_hostnames:
                self._line_hostnames.append(hostname)
        if fingerprints:
            # Fingerprint order and multiplicity are preserved verbatim.
            self._line_fingerprints.extend(fingerprints)

    def _reset_line_facts(self) -> None:
        self._line_chars = []
        self._line_hostnames = []
        self._line_fingerprints = []
        self._line_overflowed = False

    def _resync_line_facts(self) -> None:
        self._line_hostnames = []
        self._line_fingerprints = []

    def _append_tail(self, text: str) -> None:
        for character in text:
            self._tail.append(character)
            self._tail_byte_count += len(character.encode("utf-8"))
        while (
            self._tail_byte_count > self._tail_bytes_limit and self._tail
        ):
            character = self._tail.popleft()
            self._tail_byte_count -= len(character.encode("utf-8"))


def make_stream_factory(
    secrets=(),
    fingerprinter: SessionUrlIdentity | None = None,
    *,
    on_chunk: Callable[[], None] | None = None,
) -> Callable[[str], NpmDiagnosticStream]:
    """Return a per-stream factory wired for the locked-assembly collector."""

    def factory(stream: str) -> NpmDiagnosticStream:
        return NpmDiagnosticStream(
            stream,
            secrets,
            fingerprinter=fingerprinter,
            on_chunk=on_chunk,
        )

    return factory


__all__ = [
    "DIAGNOSTIC_LINE_LIMIT_BYTES",
    "NpmDiagnosticStream",
    "OVERSIZED_DIAGNOSTIC_MARKER",
    "classify_npm_diagnostic",
    "make_stream_factory",
    "project_tail",
]
