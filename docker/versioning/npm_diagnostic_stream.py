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
  semantics, preserves the selected representation's ordering and occurrences
  without grouping, and excludes discarded overflow fragments and host
  metadata.  Exactly one tail is retained: ``redacted`` retains the URL-free
  sanitized projection, ``host-path`` retains a safely rendered normalized
  hostname/path render of the same projection, and ``exact`` retains
  terminal-safe source.  The projected-safe form used for external SDK events
  is transient in ``host-path`` and ``exact`` and never becomes a second
  retained tail.

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
    SafeHostPath,
    sanitize_diagnostic_text,
)
from docker.versioning.host_progress import HostDiagnosticClassification
from docker.versioning.model import NetworkUrlDisplay


def _host_path_formatter(fact: SafeHostPath) -> str:
    """Render one validated safe host/path fact for the retained `host-path` tail."""
    return fact.text

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
    text: str,
    secrets=(),
    *,
    tail_bytes: int = TAIL_BYTES,
    network_url_display: NetworkUrlDisplay = NetworkUrlDisplay.REDACTED,
) -> str:
    """Return bounded text for the single retained tail under *network_url_display*.

    The signature remains compatible with
    :func:`docker.npm_environment.streaming.redact_tail` so callers can inject
    it wherever a retained tail is rendered.  The one selected representation
    is produced under the same decode/frame/neutralize order as the streaming
    collector: URL-free sanitized text for ``redacted``, safely rendered
    normalized hostname/path text for ``host-path``, or terminal-safe source
    text for ``exact``.  No non-selected representation is retained.
    """
    if not isinstance(network_url_display, NetworkUrlDisplay):
        raise ValueError("network_url_display must be a NetworkUrlDisplay")
    framer = TerminalSafeLineFramer()
    neutralized = "".join(framer.feed_text(text))
    neutralized += "".join(framer.finish())
    if network_url_display is NetworkUrlDisplay.EXACT:
        # Source content is exactly what the selected representation wants;
        # the projected-safe form is never retained in this mode.
        return _bounded_tail(neutralized, tail_bytes)
    if network_url_display is NetworkUrlDisplay.HOST_PATH:
        projector = DiagnosticProjector(
            secrets, url_formatter=_host_path_formatter
        )
        safe = "".join(projector.feed_text(neutralized))
        safe += "".join(projector.finish())
        return _bounded_tail(safe, tail_bytes)
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
    finalized lines.  ``tail`` returns the single mode-selected retained text
    (URL-free ``redacted``, normalized hostname/path ``host-path``, or
    terminal-safe ``exact``) with the existing per-stream byte bound; the
    finalized line never re-appends its text, so a prefix is retained exactly
    once.
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
        network_url_display: NetworkUrlDisplay = NetworkUrlDisplay.REDACTED,
    ) -> None:
        if stream not in ("stdout", "stderr"):
            raise ValueError("stream must be 'stdout' or 'stderr'")
        if tail_bytes <= 0:
            raise ValueError("tail_bytes must be positive")
        if line_limit <= 0:
            raise ValueError("line_limit must be positive")
        if on_chunk is not None and not callable(on_chunk):
            raise TypeError("on_chunk must be callable or None")
        if not isinstance(network_url_display, NetworkUrlDisplay):
            raise ValueError("network_url_display must be a NetworkUrlDisplay")
        self._stream = stream
        self._network_url_display = network_url_display
        self._framer = TerminalSafeLineFramer(line_limit_bytes=line_limit)
        self._projector = DiagnosticProjector(secrets, url_identity=fingerprinter)
        # The single retained tail is mode-selected.  ``redacted`` retains the
        # URL-free projection directly; ``host-path`` derives its render from
        # a dedicated projector over the same terminal-safe source; ``exact``
        # appends the neutralized source itself.  At most one of these feeds
        # the one retained buffer -- no second tail is created.
        self._retained_projector = (
            DiagnosticProjector(secrets, url_formatter=_host_path_formatter)
            if network_url_display is NetworkUrlDisplay.HOST_PATH
            else None
        )
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
            chunks.extend(
                self._emit_projected(
                    text,
                    retain=(
                        self._network_url_display is NetworkUrlDisplay.REDACTED
                    ),
                )
            )
        if self._retained_projector is not None:
            for retained in self._retained_projector.finish(abort=abort):
                self._append_tail(retained)
            self._drain_retained_facts()
        if (
            self._line_chars
            or self._line_hostnames
            or self._line_fingerprints
        ):
            chunks.append(self._finalize_line())
        return tuple(chunks)

    def tail(self) -> str:
        """Return the single retained, mode-selected, bounded diagnostic tail."""
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
            source_segment = text[start:end]
            projected = self._projector.feed_text(source_segment)
            self._absorb_projector_facts()
            for projected_text in projected:
                chunks.extend(
                    self._emit_projected(
                        projected_text,
                        retain=(
                            self._network_url_display
                            is NetworkUrlDisplay.REDACTED
                        ),
                    )
                )
            self._retain_source_segment(source_segment)
            start = end
        return chunks

    def _retain_source_segment(self, segment: str) -> None:
        """Feed the one retained buffer for ``host-path`` and ``exact``.

        ``redacted`` is already retained from the URL-free projection as it is
        emitted, so it is intentionally omitted here.  Each mode appends to the
        same single bounded tail; no non-selected representation is retained.
        """
        mode = self._network_url_display
        if mode is NetworkUrlDisplay.HOST_PATH:
            assert self._retained_projector is not None
            for retained in self._retained_projector.feed_text(segment):
                self._append_tail(retained)
            self._drain_retained_facts()
        elif mode is NetworkUrlDisplay.EXACT:
            # Terminal-safe source, bounded by the existing tail byte limit.
            self._append_tail(segment)

    def _drain_retained_facts(self) -> None:
        """Discard the alternate projector's facts; its text already carries them."""
        assert self._retained_projector is not None
        self._retained_projector.take_facts()
        self._retained_projector.take_host_paths()

    def _emit_projected(self, text: str, *, retain: bool) -> list[StreamChunk]:
        """Deliver committed safe prefixes and finalized line events.

        Projected text is delivered immediately as a non-finalized committed
        prefix as soon as the projector commits it.  A newline is the record
        boundary: it finalizes the accumulated logical line together with its
        metadata and resets line-local state.  When *retain* is set the
        ``redacted`` selected representation is appended to the one bounded
        tail; ``host-path`` and ``exact`` retain through
        :meth:`_retain_source_segment` instead.  ``_finalize_line`` never
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
                    chunks.append(self._emit_prefix(segment, retain=retain))
                break
            segment = text[start:newline]
            if segment:
                chunks.append(self._emit_prefix(segment, retain=retain))
            if retain:
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

    def _emit_prefix(self, segment: str, *, retain: bool) -> StreamChunk:
        """Accumulate, optionally retain, and return one committed prefix."""
        self._line_chars.append(segment)
        if retain:
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
        # Host/path facts are internal-presentation metadata; the selected
        # ``host-path`` representation already carries them in its rendered
        # text, so the fact list is drained and never retained separately.
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
    network_url_display: NetworkUrlDisplay = NetworkUrlDisplay.REDACTED,
) -> Callable[[str], NpmDiagnosticStream]:
    """Return a per-stream factory wired for the locked-assembly collector."""

    def factory(stream: str) -> NpmDiagnosticStream:
        return NpmDiagnosticStream(
            stream,
            secrets,
            fingerprinter=fingerprinter,
            on_chunk=on_chunk,
            network_url_display=network_url_display,
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
