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
* a committed prefix is released before any newline: the mode-selected
  display fragment (URL-free ``redacted``, normalized hostname/path
  ``host-path``, or terminal-safe ``exact``) is delivered as ``local_text`` as
  soon as the decoder, neutralizer, and selected projector commit it, and the
  retained per-stream tail is appended from that same selected output
  immediately; the record boundary then emits one finalized line carrying the
  complete URL-free text and the accumulated metadata, without re-appending
  the text;
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
from dataclasses import replace
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
from docker.versioning.fetch_identity import FetchGroupKey
from docker.versioning.host_progress import HostDiagnosticClassification
from docker.versioning.model import NetworkUrlDisplay
from docker.versioning.npm_fetch import (
    NpmFetchRecognizer,
    canonical_fetch_text,
    fetch_group_identity,
)


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
    """Incremental bounded framer, URL-free projector, fetch recognizer, tail.

    ``feed_bytes`` returns two kinds of :class:`StreamChunk`:

    * a committed prefix (``finalized=False``) as soon as the decoder and
      terminal neutralizer commit source text -- without waiting for a
      newline.  Its mode-selected display fragment is released promptly as
      ``local_text`` (``redacted`` reuses the URL-free projection,
      ``host-path`` the normalized hostname/path rendering, ``exact`` the
      terminal-safe source), and the selected representation is appended to
      the retained tail immediately;
    * a finalized diagnostic line (``finalized=True``) at a newline or at EOF,
      carrying the complete URL-free line text plus the normalized hostnames
      and ordered URL fingerprints gathered while that logical line was open,
      and the complete mode-selected local representation.

    Retention between chunks is deliberately minimal.  ``redacted`` and
    ``host-path`` never retain a terminal-safe source line: the only per-line
    state they keep is the selected safe representation (the URL-free
    projection, or the ``host-path`` projector's sanitized host/path render)
    plus the recognizer's parser-minimal bounded fields.  ``exact`` is the
    only mode permitted to retain terminal-safe source content, because that
    source *is* its selected representation.  Fetch recognition is incremental
    -- :class:`~docker.versioning.npm_fetch.NpmFetchRecognizer` consumes the
    streamed source fields directly and never buffers the complete source line
    or raw URL -- and the sanitized host/path needed for ``host-path``
    grouping comes from the already-safe projector fact, not from a
    re-parsed URL.

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
        self._secrets = tuple(secrets)
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
        #: The mode-selected local representation for the current logical line.
        #: It holds only *selected safe* output: the URL-free projection for
        #: ``redacted``, the normalized host/path rendering produced by the
        #: selected projector for ``host-path``, and the terminal-safe source
        #: for ``exact``.  Terminal-safe source text is therefore never retained
        #: for ``redacted`` or ``host-path``: the collector keeps no second
        #: source-line buffer.
        self._selected_line_chars: list[str] = []
        #: Sanitized host/path facts for the current line, supplied by the
        #: ``host-path`` selected projector.  These are bounded, already-safe
        #: facts (never the raw URL), and they are the only URL-derived state the
        #: recognizer needs for ``host-path`` grouping.
        self._line_host_paths: list[SafeHostPath] = []
        #: Incremental grammar recognizer.  It retains only bounded parser state
        #: (method, status, attempt, cache, and an unsafe-canonicalization flag)
        #: and never the complete source line or raw URL.
        self._fetch_recognizer = NpmFetchRecognizer(secrets)
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
        """Flush the framer and projector, finalizing any pending line.

        *abort* is the fail-closed policy for reader failure and
        cancellation.  It is forwarded to every projector, including the
        selected ``host-path`` projector, so the finalized live representation
        never resolves an incomplete trailing token that the streaming
        sanitizer withheld.
        """
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
                self._selected_line_chars.append(retained)
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
            # The truncated line can never be a recognized fetch, so the
            # recognizer stops retaining token content and waits for the next
            # record boundary.
            self._fetch_recognizer.mark_overflowed()
        chunks: list[StreamChunk] = []
        start = 0

        while start < len(text):
            newline = text.find("\n", start)
            end = len(text) if newline == -1 else newline + 1
            source_segment = text[start:end]
            if not self._line_overflowed:
                # The recognizer consumes the terminal-safe source field text,
                # not the URL-free projection: it must see the URL field to
                # validate it.  The record terminator is not part of the line.
                if source_segment.endswith("\n"):
                    self._fetch_recognizer.feed(source_segment[:-1])
                else:
                    self._fetch_recognizer.feed(source_segment)
            # The mode-selected local prefix is released as soon as the
            # selected representation commits it, before the safe projection
            # finalizes the record at the newline.
            chunks.extend(self._emit_selected_prefix(source_segment))
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
            start = end
        return chunks

    def _emit_selected_prefix(self, source_segment: str) -> list[StreamChunk]:
        """Release the mode-selected committed prefix and feed the retained tail.

        ``redacted`` selects the URL-free projection released by
        :meth:`_emit_projected`, so nothing is emitted here.  ``host-path``
        selects the normalized hostname/path rendering committed by the
        dedicated selected projector -- the same output that feeds the one
        bounded retained tail, so the live and retained representations never
        diverge.  ``exact`` selects the terminal-safe neutralized source
        fragment itself.

        Each selected fragment is appended to the one retained tail and
        returned as a presentation-only prefix chunk carrying ``local_text``.
        Its ``text`` stays empty, so no selected local content can reach a
        structured/SDK consumer; a committed prefix is never a complete
        diagnostic.
        """
        mode = self._network_url_display
        if mode is NetworkUrlDisplay.REDACTED:
            return []
        if mode is NetworkUrlDisplay.EXACT:
            # Terminal-safe source, bounded by the existing tail byte limit.
            self._selected_line_chars.append(source_segment)
            self._append_tail(source_segment)
            if source_segment.endswith("\n"):
                return self._local_prefix_chunks(source_segment[:-1])
            return self._local_prefix_chunks(source_segment)
        assert self._retained_projector is not None
        chunks: list[StreamChunk] = []
        for retained in self._retained_projector.feed_text(source_segment):
            # The selected projector already produced the sanitized host/path
            # representation, so this is the only place the ``host-path`` local
            # text is accumulated; the raw source line is never kept.
            self._selected_line_chars.append(retained)
            self._append_tail(retained)
            chunks.extend(self._local_prefix_chunks(retained))
        self._drain_retained_facts()
        return chunks

    def _local_prefix_chunks(self, text: str) -> list[StreamChunk]:
        """Return presentation-only prefix chunks for one local fragment.

        The fragment may carry the record terminator at its end.  A
        terminator is never part of a provisional snapshot, so the text before
        each newline is released as its own cumulative prefix.  ``text`` stays
        empty so selected local content never reaches a structured/SDK
        consumer.
        """
        chunks: list[StreamChunk] = []
        start = 0
        while start < len(text):
            newline = text.find("\n", start)
            end = len(text) if newline == -1 else newline
            segment = text[start:end]
            if segment:
                chunks.append(
                    StreamChunk(
                        self._stream,
                        "",
                        overflowed=self._line_overflowed,
                        finalized=False,
                        local_text=segment,
                    )
                )
            if newline == -1:
                break
            start = newline + 1
        return chunks

    def _drain_retained_facts(self) -> None:
        """Collect the selected projector's safe host/path facts for the line.

        The alternate projector's text already carries the host/path rendering,
        so its hostname/fingerprint facts are discarded; only the bounded,
        sanitized :class:`SafeHostPath` fact is kept so ``host-path`` grouping
        never needs the raw URL.
        """
        assert self._retained_projector is not None
        self._retained_projector.take_facts()
        for fact in self._retained_projector.take_host_paths():
            if fact not in self._line_host_paths:
                self._line_host_paths.append(fact)

    def _emit_projected(self, text: str, *, retain: bool) -> list[StreamChunk]:
        """Deliver committed safe prefixes and finalized line events.

        Projected text is delivered immediately as a non-finalized committed
        prefix as soon as the projector commits it.  A newline is the record
        boundary: it finalizes the accumulated logical line together with its
        metadata and resets line-local state.  When *retain* is set the
        ``redacted`` selected representation is appended to the one bounded
        tail; ``host-path`` and ``exact`` release their selected representation
        and retain it through :meth:`_emit_selected_prefix` instead.
        ``_finalize_line`` never re-appends the line text, so a prefix retained
        once is not retained twice when the line completes.
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
                chunks.append(self._finalize_line(newline_terminated=True))
            else:
                # An empty logical line preserves only its record boundary.
                self._resync_line_facts()
            start = newline + 1
        return chunks

    def _emit_prefix(self, segment: str, *, retain: bool) -> StreamChunk:
        """Accumulate, optionally retain, and return one committed prefix.

        The chunk carries the URL-free safe projection as ``text`` and reuses
        it as the ``redacted`` mode-selected ``local_text``.  ``host-path``
        and ``exact`` prefixes are released separately by
        :meth:`_emit_selected_prefix`; their safe projection still accumulates
        here for the finalized line but is never displayed.
        """
        self._line_chars.append(segment)
        if retain:
            self._append_tail(segment)
        # Only ``redacted`` selects the URL-free projection as its local
        # display fragment.  ``host-path`` and ``exact`` release their
        # selected fragment separately by :meth:`_emit_selected_prefix`, so
        # their safe projection carries no local fragment and is never
        # displayed.
        local_text = (
            segment
            if self._network_url_display is NetworkUrlDisplay.REDACTED
            else None
        )
        return StreamChunk(
            self._stream, segment, overflowed=self._line_overflowed,
            finalized=False, local_text=local_text,
        )

    def _finalize_line(self, *, newline_terminated: bool = False) -> StreamChunk:
        text = "".join(self._line_chars)
        hostnames = tuple(self._line_hostnames)
        fingerprints = tuple(self._line_fingerprints)
        overflowed = self._line_overflowed
        local_text, fetch_key, fetch_text = self._select_presentation(
            text,
            overflowed,
            newline_terminated=newline_terminated,
        )
        self._reset_line_facts()
        return StreamChunk(
            self._stream, text, hostnames, fingerprints, overflowed=overflowed,
            local_text=local_text, fetch_key=fetch_key, fetch_text=fetch_text,
        )

    def _selected_local_text(
        self, projected: str, *, newline_terminated: bool
    ) -> str:
        """Return the accumulated mode-selected local representation.

        ``redacted`` selects the URL-free projection already carried in
        ``projected``; ``host-path`` and ``exact`` accumulate their selected
        output incrementally, including the record terminator, which is
        removed here (and only here) for a newline-terminated record so the
        finalized text matches the live prefix and retained tail.
        """
        mode = self._network_url_display
        if mode is NetworkUrlDisplay.REDACTED:
            return projected
        selected = "".join(self._selected_line_chars)
        if newline_terminated and selected.endswith("\n"):
            selected = selected[:-1]
        return selected

    def _select_presentation(
        self,
        projected: str,
        overflowed: bool,
        *,
        newline_terminated: bool = False,
    ) -> tuple[str, FetchGroupKey | None, str | None]:
        """Derive the mode-selected local text and recognized-fetch identity.

        Only a bounded, non-overflowed line can be a fetch: an oversized line
        was truncated, so it stays an ordinary diagnostic with no group
        identity.  ``exact`` never aggregates, so it carries the terminal-safe
        source line with no fetch key.  Recognition is incremental and retains
        no complete source line or raw URL; ``host-path`` grouping reuses the
        already-sanitized :class:`SafeHostPath` fact produced by the selected
        projector.
        """
        mode = self._network_url_display
        local = self._selected_local_text(
            projected, newline_terminated=newline_terminated
        )
        if mode is NetworkUrlDisplay.EXACT:
            return local, None, None
        if overflowed:
            return local, None, None
        record = self._fetch_recognizer.finish()
        if record is None or self._fetch_recognizer.canonical_unsafe:
            # A non-fetch line, or a fetch whose canonical rendering would
            # restore a configured secret from a source-derived field.  Either
            # way it stays the sanitized ordinary diagnostic with no group
            # identity, so no secret-bearing canonical text is ever presented.
            return local, None, None
        if mode is NetworkUrlDisplay.HOST_PATH and self._line_host_paths:
            record = replace(record, host_path=self._line_host_paths[0])
        key = fetch_group_identity(record, mode, secrets=self._secrets)
        canonical = canonical_fetch_text(record, mode, secrets=self._secrets)
        if key is None or not canonical:
            return local, None, None
        return local, key, canonical

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
        self._selected_line_chars = []
        self._line_host_paths = []
        self._line_hostnames = []
        self._line_fingerprints = []
        self._line_overflowed = False
        self._fetch_recognizer.reset()

    def _resync_line_facts(self) -> None:
        self._selected_line_chars = []
        self._line_host_paths = []
        self._line_hostnames = []
        self._line_fingerprints = []
        self._fetch_recognizer.reset()

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
