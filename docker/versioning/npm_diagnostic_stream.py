"""Bounded URL-free npm diagnostic collection for locked assembly.

Phase 8 of ``improve-host-build-observability`` bridges the existing npm
stdout/stderr collector to the shared safe diagnostic projector:

* every raw stdout/stderr chunk is observed (for diagnostic-silence reset and
  latest-activity tracking) before any redaction, URL projection, line
  assembly, or mailbox admission;
* decoded text is secret-redacted and URL-sanitized incrementally, so a
  sensitive token split across decoder/input chunks never reaches the
  structured live branch or the retained tail;
* normalized hostnames are attached to every structured line that contains
  that host (not only its first occurrence), and ephemeral ordered URL
  fingerprints are attached with multiplicity preserved; both live only on
  the structured chunks, and the collector drains the projector's fact buffer
  after each line so completed-line metadata is never retained;
* decoded diagnostic-line assembly is independently bounded at 64 KiB of
  UTF-8 text per unterminated line, emits exactly
  ``[sanitized oversized diagnostic]`` and discards through the next safe
  boundary, and recovers afterward;
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
    REDACTED,
    StreamChunk,
    TAIL_BYTES,
)
from docker.versioning.diagnostic_identity import SessionUrlIdentity
from docker.versioning.diagnostic_projection import (
    DiagnosticProjector,
    sanitize_diagnostic_text,
)
from docker.versioning.host_progress import HostDiagnosticClassification

#: Maximum decoded UTF-8 bytes retained while assembling one diagnostic line.
DIAGNOSTIC_LINE_LIMIT_BYTES = 64 * 1024

#: Fixed fail-closed marker for a diagnostic line that exceeds the line limit.
OVERSIZED_DIAGNOSTIC_MARKER = "[sanitized oversized diagnostic]"

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
    persisted, or failure representation.
    """
    safe, _hostnames = sanitize_diagnostic_text(text, secrets)
    return _bounded_tail(safe, tail_bytes)


class NpmDiagnosticStream:
    """Incremental URL-free projector, bounded line assembler, and bounded tail.

    ``feed_bytes`` returns one :class:`StreamChunk` per completed diagnostic
    line (or fixed safe replacement marker).  ``tail`` returns the retained
    URL-free text with the existing per-stream byte bound.
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
        self._projector = DiagnosticProjector(secrets, url_identity=fingerprinter)
        self._on_chunk = on_chunk
        self._tail_bytes_limit = tail_bytes
        self._line_limit = line_limit
        self._line_chars: list[str] = []
        self._line_bytes = 0
        self._overflow = False
        self._eof = False
        self._tail: deque[str] = deque()
        self._tail_byte_count = 0
        # Per-line facts consumed incrementally from the projector so the
        # projector never retains completed-line metadata.
        self._line_hostnames: list[str] = []
        self._line_fingerprints: list[str] = []

    # -- public surface -------------------------------------------------

    def feed_bytes(self, data: bytes) -> tuple[StreamChunk, ...]:
        """Observe one raw chunk and return any completed URL-free lines."""
        if self._eof:
            raise RuntimeError("stream is already finished")
        if not data:
            return ()
        if self._on_chunk is not None:
            self._on_chunk()
        # Feed the projector one newline-delimited segment at a time so each
        # removed URL's normalized host/fingerprint is associated with the
        # diagnostic line that contained it.  ``0x0A`` is never part of a
        # multibyte UTF-8 sequence, so splitting the raw bytes is safe.
        chunks: list[StreamChunk] = []
        start = 0
        length = len(data)
        while start < length:
            newline = data.find(b"\n", start)
            if newline == -1:
                chunks.extend(self._feed_segment(data[start:]))
                break
            chunks.extend(self._feed_segment(data[start : newline + 1]))
            start = newline + 1
        return tuple(chunks)

    def finish(self, *, abort: bool = False) -> tuple[StreamChunk, ...]:
        """Flush the projector and finalize any pending line or overflow."""
        if self._eof:
            return ()
        self._eof = True
        chunks: list[StreamChunk] = []
        projected = self._projector.finish(abort=abort)
        self._absorb_projector_facts()
        for text in projected:
            chunks.extend(self._feed_url_free(text))
        if self._overflow:
            chunks.append(self._finish_overflow())
        elif self._line_chars:
            chunks.append(self._finalize_line(newline=False))
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
        """Retained decoded diagnostic-line bytes for the current line."""
        return self._line_bytes

    # -- internals ------------------------------------------------------

    def _feed_segment(self, segment: bytes) -> list[StreamChunk]:
        """Project one newline-delimited raw segment and assemble its lines."""
        chunks: list[StreamChunk] = []
        projected = self._projector.feed_bytes(segment)
        self._absorb_projector_facts()
        for text in projected:
            chunks.extend(self._feed_url_free(text))
        return chunks

    def _feed_url_free(self, text: str) -> list[StreamChunk]:
        chunks: list[StreamChunk] = []
        for character in text:
            if self._overflow:
                if character == "\n":
                    chunks.append(self._finish_overflow())
                continue
            if character == "\n":
                if self._line_chars:
                    chunks.append(self._finalize_line(newline=True))
                else:
                    self._append_tail("\n")
                    self._reset_line_facts()
                continue
            width = len(character.encode("utf-8"))
            if self._line_bytes + width > self._line_limit:
                self._begin_overflow()
                continue
            self._line_chars.append(character)
            self._line_bytes += width
        return chunks

    def _begin_overflow(self) -> None:
        # Discard the already-assembled oversized fragment and everything up to
        # the next safe newline; the marker is emitted at that boundary.
        self._line_chars = []
        self._line_bytes = 0
        self._resync_line_facts()
        self._overflow = True

    def _finish_overflow(self) -> StreamChunk:
        self._overflow = False
        self._append_tail(OVERSIZED_DIAGNOSTIC_MARKER + "\n")
        self._reset_line_facts()
        return StreamChunk(self._stream, OVERSIZED_DIAGNOSTIC_MARKER)

    def _finalize_line(self, *, newline: bool) -> StreamChunk:
        text = "".join(self._line_chars)
        hostnames = tuple(self._line_hostnames)
        fingerprints = tuple(self._line_fingerprints)
        self._reset_line_facts()
        self._append_tail(text + ("\n" if newline else ""))
        return StreamChunk(self._stream, text, hostnames, fingerprints)

    def _absorb_projector_facts(self) -> None:
        """Consume newly extracted facts for the current diagnostic line.

        The projector's facts are drained on every segment, so its buffers
        never retain metadata for completed lines.  While the current line is
        overflowing, incoming facts are discarded alongside its text so the
        fixed oversized marker carries none of them; a later line resumes
        normal collection.
        """
        hostnames, fingerprints = self._projector.take_facts()
        if self._overflow:
            return
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
        self._line_bytes = 0
        self._line_hostnames = []
        self._line_fingerprints = []

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
