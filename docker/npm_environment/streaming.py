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

import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Iterable, Protocol, Sequence

from docker.versioning.model import NetworkUrlDisplay

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

#: Maximum raw bytes retained while recognizing one terminal control sequence.
CONTROL_SEQUENCE_LIMIT_BYTES = 8 * 1024
INCOMPLETE_CONTROL_SEQUENCE = "[INCOMPLETE_CONTROL_SEQUENCE]"
OVERSIZED_CONTROL_SEQUENCE = "[OVERSIZED_CONTROL_SEQUENCE]"

#: Maximum decoded source-line bytes retained before a newline boundary.
DIAGNOSTIC_LINE_LIMIT_BYTES = 64 * 1024

#: Fixed fail-closed marker for a source line that exceeds the line limit.
OVERSIZED_DIAGNOSTIC_MARKER = "[sanitized oversized diagnostic]"


class _OverflowMarker(str):
    """Marker instance distinguishing a real line overflow from source text.

    It is a plain ``str`` for joining, equality, and redaction, but the
    structured collector can recognize the exact instance the framer appended.
    A source line that happens to contain the marker's characters is still
    ordinary text.
    """

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


def _visible_control(text: str) -> str:
    """Render C0/C1 controls as literal, terminal-inert escape notation."""
    return "".join(
        f"\\x{ord(character):02x}" if ord(character) < 32 or 127 <= ord(character) <= 159
        else character
        for character in text
    )


class _Utf8ByteDecoder:
    """Incremental UTF-8 decoder matching CPython's ``errors="replace"``.

    A malformed sequence is replaced by one U+FFFD per maximal subpart, and a
    truncated trailing sequence is withheld until more input arrives and
    replaced by exactly one U+FFFD at EOF.  Each decoded item carries the
    number of source bytes it consumed so callers can bound work by the
    original raw stream even when malformed bytes expand into replacement
    text, and every chunk split yields the same output as one unsplit feed.
    """

    def __init__(self) -> None:
        self._held = b""

    @staticmethod
    def _second_byte_range(first: int) -> tuple[int, int]:
        if first == 0xE0:
            return 0xA0, 0xBF
        if first == 0xED:
            return 0x80, 0x9F
        if first == 0xF0:
            return 0x90, 0xBF
        if first == 0xF4:
            return 0x80, 0x8F
        return 0x80, 0xBF

    def feed(self, data: bytes) -> tuple[tuple[str, int], ...]:
        buffer = self._held + bytes(data)
        self._held = b""
        decoded: list[tuple[str, int]] = []
        index = 0
        length = len(buffer)
        while index < length:
            first = buffer[index]
            if first < 0x80:
                decoded.append((chr(first), 1))
                index += 1
                continue
            if first < 0xC2:
                decoded.append(("\ufffd", 1))
                index += 1
                continue
            if first < 0xE0:
                need = 2
            elif first < 0xF0:
                need = 3
            elif first < 0xF5:
                need = 4
            else:
                decoded.append(("\ufffd", 1))
                index += 1
                continue
            if index + 1 >= length:
                self._held = buffer[index:]
                break
            low, high = self._second_byte_range(first)
            if not low <= buffer[index + 1] <= high:
                decoded.append(("\ufffd", 1))
                index += 1
                continue
            consumed = 2
            while (
                consumed < need
                and index + consumed < length
                and 0x80 <= buffer[index + consumed] <= 0xBF
            ):
                consumed += 1
            if consumed == need:
                decoded.append(
                    (buffer[index : index + need].decode("utf-8"), need)
                )
                index += need
            elif index + consumed < length:
                # An invalid continuation byte ends the maximal subpart.
                decoded.append(("\ufffd", consumed))
                index += consumed
            else:
                self._held = buffer[index:]
                break
        return tuple(decoded)

    def finish(self) -> tuple[tuple[str, int], ...]:
        held, self._held = self._held, b""
        if not held:
            return ()
        return (("\ufffd", len(held)),)


#: C1 introducer code points mapped to their control-string type.
_STRING_INTRODUCERS = {
    0x90: "dcs",
    0x98: "sos",
    0x9D: "osc",
    0x9E: "pm",
    0x9F: "apc",
}

#: ESC-introduced control-string introducers mapped to their type.
_STRING_ESCAPE_INTRODUCERS = {
    "]": "osc",
    "P": "dcs",
    "X": "sos",
    "^": "pm",
    "_": "apc",
}


class TerminalControlNeutralizer:
    """Incrementally decode and neutralize terminal-affecting controls.

    Complete CSI, OSC, DCS, SOS, PM, and APC sequences remain visible as
    literal, terminal-inert escapes.  BEL terminates only an OSC string; DCS,
    SOS, PM, and APC are collected or discarded through ST (``ESC \\`` or C1
    ST) so an embedded BEL never ends them.  Unterminated sequences fail
    closed to :data:`INCOMPLETE_CONTROL_SEQUENCE`; a sequence whose original
    raw byte size exceeds the configured limit fails closed to
    :data:`OVERSIZED_CONTROL_SEQUENCE` exactly once, its remaining bytes are
    discarded through the sequence terminator, and normal processing resumes.
    The limit is applied to the original raw bytes of the pending sequence,
    including malformed UTF-8 bytes, not to re-encoded replacement text.
    """

    #: States that hold an unfinished (non-discarding) sequence at EOF.
    _INCOMPLETE_STATES = frozenset({"esc", "csi", "string", "string_esc"})

    def __init__(self, *, limit_bytes: int = CONTROL_SEQUENCE_LIMIT_BYTES) -> None:
        if limit_bytes <= 0:
            raise ValueError("limit_bytes must be positive")
        self._decoder = _Utf8ByteDecoder()
        self._limit = limit_bytes
        self._state = "normal"
        self._pending = ""
        self._pending_bytes = 0
        self._string_kind: str | None = None
        self._eof = False

    def feed_bytes(self, data: bytes) -> tuple[str, ...]:
        if self._eof:
            raise RuntimeError("neutralizer is already finished")
        if not data:
            return ()
        return self._feed(self._decoder.feed(data))

    def feed_characters(
        self, characters: Iterable[tuple[str, int]]
    ) -> tuple[str, ...]:
        """Neutralize already-decoded ``(character, source_bytes)`` pairs."""
        if self._eof:
            raise RuntimeError("neutralizer is already finished")
        return self._feed(characters)

    def finish(self, *, abort: bool = False) -> tuple[str, ...]:
        del abort
        if self._eof:
            return ()
        self._eof = True
        chunks = list(self._feed(self._decoder.finish()))
        if self._state in self._INCOMPLETE_STATES:
            chunks.append(INCOMPLETE_CONTROL_SEQUENCE)
        self._state = "normal"
        self._pending = ""
        self._pending_bytes = 0
        self._string_kind = None
        return tuple(chunks)

    def discard_pending(self) -> None:
        """Drop any pending control sequence without emitting it.

        Used when a diagnostic line exceeds its byte bound while a control
        sequence is still pending: the pending sequence is discarded with the
        rest of the source suffix and the line-overflow marker is sufficient,
        so no ambiguous control content is released.
        """
        self._state = "normal"
        self._pending = ""
        self._pending_bytes = 0
        self._string_kind = None

    def _begin(self, state: str, character: str, width: int) -> bool:
        """Start a sequence; return whether the introducer alone is oversized."""
        if width > self._limit:
            self._state = "normal"
            self._pending = ""
            self._pending_bytes = 0
            return True
        self._state = state
        self._pending = character
        self._pending_bytes = width
        return False

    def _begin_string(self, kind: str, character: str, width: int) -> bool:
        """Extend the introducer with *character* and enter the string state."""
        self._string_kind = kind
        self._pending += character
        self._pending_bytes += width
        if self._pending_bytes > self._limit:
            self._state = "normal"
            self._pending = ""
            self._pending_bytes = 0
            return True
        self._state = "string"
        return False

    def _append(self, character: str, width: int) -> bool:
        """Append to the pending sequence; return whether it is now oversized."""
        self._pending += character
        self._pending_bytes += width
        if self._pending_bytes > self._limit:
            self._pending = ""
            self._pending_bytes = 0
            return True
        return False

    def _emit_pending(self, chunks: list[str]) -> None:
        chunks.append(_visible_control(self._pending))
        self._state = "normal"
        self._pending = ""
        self._pending_bytes = 0
        self._string_kind = None

    def _string_terminated(self, character: str, code: int) -> bool:
        """Return whether *character* terminates the active control string."""
        if code == 0x9C:
            return True
        return self._string_kind == "osc" and character == "\x07"

    def _feed(self, characters: Iterable[tuple[str, int]]) -> tuple[str, ...]:
        chunks: list[str] = []
        plain: list[str] = []

        def flush_plain() -> None:
            if plain:
                chunks.append("".join(plain))
                plain.clear()

        def oversize(state: str) -> None:
            flush_plain()
            chunks.append(OVERSIZED_CONTROL_SEQUENCE)
            self._state = state

        for character, width in characters:
            code = ord(character)
            state = self._state

            if state == "discard_csi":
                if 0x40 <= code <= 0x7E:
                    self._state = "normal"
                continue
            if state == "discard_string":
                if self._string_terminated(character, code):
                    self._state = "normal"
                    self._string_kind = None
                elif character == "\x1b":
                    self._state = "discard_string_esc"
                continue
            if state == "discard_string_esc":
                if character == "\\":
                    self._state = "normal"
                    self._string_kind = None
                elif self._string_terminated(character, code):
                    self._state = "normal"
                    self._string_kind = None
                elif character == "\x1b":
                    self._state = "discard_string_esc"
                else:
                    self._state = "discard_string"
                continue
            if state == "esc":
                if character == "[":
                    if self._append(character, width):
                        oversize("discard_csi")
                    else:
                        self._state = "csi"
                elif character in _STRING_ESCAPE_INTRODUCERS:
                    kind = _STRING_ESCAPE_INTRODUCERS[character]
                    if self._begin_string(kind, character, width):
                        oversize("discard_string")
                else:
                    self._emit_pending(chunks)
                    self._handle_normal(
                        character, code, width, chunks, plain, flush_plain
                    )
                continue
            if state == "csi":
                if 0x40 <= code <= 0x7E:
                    if self._append(character, width):
                        oversize("normal")
                    else:
                        flush_plain()
                        self._emit_pending(chunks)
                elif self._append(character, width):
                    oversize("discard_csi")
                continue
            if state == "string":
                if character == "\x1b":
                    if self._append(character, width):
                        oversize("discard_string_esc")
                    else:
                        self._state = "string_esc"
                elif self._string_terminated(character, code):
                    if self._append(character, width):
                        oversize("normal")
                        self._string_kind = None
                    else:
                        flush_plain()
                        self._emit_pending(chunks)
                elif self._append(character, width):
                    oversize("discard_string")
                continue
            if state == "string_esc":
                if character == "\\":
                    if self._append(character, width):
                        oversize("normal")
                        self._string_kind = None
                    else:
                        flush_plain()
                        self._emit_pending(chunks)
                elif self._string_terminated(character, code):
                    # The prior ESC was string content; this byte terminates.
                    if self._append(character, width):
                        oversize("normal")
                        self._string_kind = None
                    else:
                        flush_plain()
                        self._emit_pending(chunks)
                elif self._append(character, width):
                    oversize("discard_string")
                else:
                    self._state = "string"
                continue

            self._handle_normal(character, code, width, chunks, plain, flush_plain)
        flush_plain()
        return tuple(chunks)

    def _handle_normal(
        self,
        character: str,
        code: int,
        width: int,
        chunks: list[str],
        plain: list[str],
        flush_plain: Callable[[], None],
    ) -> None:
        if character == "\x1b":
            flush_plain()
            self._begin("esc", character, width)
        elif code == 0x9B:
            flush_plain()
            if self._begin("csi", character, width):
                chunks.append(OVERSIZED_CONTROL_SEQUENCE)
                self._state = "discard_csi"
        elif code in _STRING_INTRODUCERS:
            flush_plain()
            if self._begin_string(
                _STRING_INTRODUCERS[code], character, width
            ):
                chunks.append(OVERSIZED_CONTROL_SEQUENCE)
                self._state = "discard_string"
        elif character in {"\n", "\t"}:
            plain.append(character)
        elif code < 32 or 127 <= code <= 159:
            flush_plain()
            chunks.append(_visible_control(character))
        else:
            plain.append(character)


class TerminalSafeLineFramer:
    """Incremental decode, streaming bounded line accounting, neutralization.

    The pipeline order is fixed: raw bytes are decoded with CPython's
    ``errors="replace"`` semantics, neutralized incrementally, and counted as
    decoded *source* bytes per source line.  Committed terminal-safe prefixes
    are emitted promptly without waiting for a newline; a whole line is never
    atomically buffered before delivery.  A newline always ends the source
    line even while a control string is incomplete, so an embedded control
    payload cannot merge two source lines; the pending control state is
    finalized at that boundary and an unterminated sequence fails closed to
    :data:`INCOMPLETE_CONTROL_SEQUENCE`.  When the current line first exceeds
    *line_limit_bytes*, exactly one *oversized_marker* is appended after the
    committed prefix, the pending control state is discarded, and the
    remaining source content is discarded through the next newline (or stream
    termination); normal processing resumes after that newline and stream
    termination never emits a second marker.
    """

    def __init__(
        self,
        *,
        line_limit_bytes: int = DIAGNOSTIC_LINE_LIMIT_BYTES,
        oversized_marker: str = OVERSIZED_DIAGNOSTIC_MARKER,
    ) -> None:
        if line_limit_bytes <= 0:
            raise ValueError("line_limit_bytes must be positive")
        self._decoder = _Utf8ByteDecoder()
        self._line_limit = line_limit_bytes
        self._oversized_marker = oversized_marker
        self._neutralizer = TerminalControlNeutralizer()
        self._line_bytes = 0
        self._overflow = False
        self._eof = False

    def feed_bytes(self, data: bytes) -> tuple[str, ...]:
        """Decode, neutralize, and count raw bytes into source lines."""
        if self._eof:
            raise RuntimeError("framer is already finished")
        if not data:
            return ()
        return self._feed(self._decoder.feed(data))

    def feed_text(self, text: str) -> tuple[str, ...]:
        """Neutralize and count already-decoded text into source lines."""
        if self._eof:
            raise RuntimeError("framer is already finished")
        if not text:
            return ()
        pairs = tuple(
            (character, len(character.encode("utf-8"))) for character in text
        )
        return self._feed(pairs)

    def finish(self, *, abort: bool = False) -> tuple[str, ...]:
        del abort
        if self._eof:
            return ()
        self._eof = True
        out = list(self._feed(self._decoder.finish()))
        if self._overflow:
            # The marker was already appended when the line first exceeded
            # its bound; stream termination adds no second marker.
            self._reset_line()
        else:
            # Finalize any pending control sequence on the unterminated final
            # line; committed prefixes were already emitted.
            out.extend(self._neutralizer.finish())
            self._reset_line()
        return tuple(out)

    @property
    def pending_line_bytes(self) -> int:
        """Decoded source bytes consumed by the current unterminated line."""
        return self._line_bytes

    def _feed(self, characters: Iterable[tuple[str, int]]) -> tuple[str, ...]:
        out: list[str] = []
        pending: list[tuple[str, int]] = []
        for character, width in characters:
            if self._overflow:
                if character == "\n":
                    self._overflow = False
                    self._reset_line()
                    out.append("\n")
                continue
            if character == "\n":
                if pending:
                    out.extend(self._neutralizer.feed_characters(pending))
                    pending = []
                out.extend(self._neutralizer.finish())
                self._reset_line()
                out.append("\n")
                continue
            if self._line_bytes + width > self._line_limit:
                if pending:
                    out.extend(self._neutralizer.feed_characters(pending))
                    pending = []
                out.append(_OverflowMarker(self._oversized_marker))
                self._overflow = True
                self._neutralizer.discard_pending()
                self._line_bytes = 0
                continue
            self._line_bytes += width
            pending.append((character, width))
        if pending:
            out.extend(self._neutralizer.feed_characters(pending))
        return tuple(out)

    def _reset_line(self) -> None:
        self._line_bytes = 0
        self._neutralizer = TerminalControlNeutralizer()


class RedactingStream:
    """Incrementally decodes and redacts one byte stream.

    Feeds raw bytes through the shared bounded pipeline in the fixed order
    decode → 64 KiB source-line framing → terminal neutralization → secret
    redaction.  Committed terminal-safe, redacted prefixes are emitted
    promptly without waiting for a newline.  When a source line first exceeds
    the limit the stream appends exactly one
    :data:`OVERSIZED_DIAGNOSTIC_MARKER` after the prefixes already emitted,
    discards the remaining source content through the next newline, and
    resumes normal processing afterward; the marker is an appended
    notification, not an atomic whole-line replacement, and an unterminated
    final line is emitted at EOF.  Retains a bounded redacted tail.
    """

    def __init__(self, secrets: Sequence[str], *, tail_bytes: int = TAIL_BYTES):
        self._secrets = dedupe_secrets(secrets)
        self._longest = longest_secret_length(self._secrets)
        self._framer = TerminalSafeLineFramer()
        self._pending = ""
        self._eof = False
        self._tail_bytes_limit = tail_bytes
        self._tail: deque[str] = deque()
        self._tail_byte_count = 0
        self._redact_unresolved = False

    # -- public surface -------------------------------------------------

    def feed_bytes(self, data: bytes) -> tuple[str, ...]:
        """Feed raw bytes; return committed safe redacted prefixes promptly."""
        if not data:
            return ()
        chunks: list[str] = []
        for text in self._framer.feed_bytes(data):
            chunks.extend(self._feed_text(text))
        return tuple(chunks)

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
        chunks: list[str] = []
        for text in self._framer.finish(abort=abort):
            chunks.extend(self._feed_text(text))
        chunks.extend(self._feed_text(""))
        return tuple(chunks)

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
                    if self._redact_unresolved:
                        partial = self._partial_secret_suffix_len(self._pending)
                        if partial:
                            safe = self._pending[: len(self._pending) - partial]
                            if safe:
                                chunks.append(safe)
                            chunks.append(REDACTED)
                        else:
                            chunks.append(self._pending)
                    else:
                        chunks.append(self._pending)
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

    def _partial_secret_suffix_len(self, text: str) -> int:
        """Length of the longest suffix that is a proper secret prefix.

        Used on an aborted finalization to redact only the truncated secret
        candidate at the end of retained text, leaving already-safe content
        (including fixed neutralization markers) intact.
        """
        best = 0
        for secret in self._secrets:
            for length in range(min(len(secret) - 1, len(text)), best, -1):
                if text.endswith(secret[:length]):
                    best = length
                    break
        return best

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

    ``finalized`` distinguishes the two collector events.  A committed prefix
    (``finalized=False``) is terminal-safe projected text that was released as
    soon as the decoder, neutralizer, and projector committed it, before any
    newline; it carries no structured line metadata and is never a complete npm
    diagnostic.  A finalized line (``finalized=True``) marks the record
    boundary: it carries the complete URL-free line text plus the normalized
    hostnames and ordered URL fingerprints for that logical line.  Complete-line
    parsing, classification, identity, and grouping consume only finalized
    lines; live/tail capture may consume committed prefixes promptly.

    ``overflowed`` marks a line truncated by the bounded line limit.  A
    committed prefix released *before* the line exceeds its bound carries
    ``overflowed=False``; the fixed ``[sanitized oversized diagnostic]``
    marker chunk carries ``overflowed=True``, as does the finalized truncated
    line at the record boundary.  A truncated line is not a complete npm
    diagnostic: complete-line parsing, classification, identity, and grouping
    must not consume it.
    """

    stream: str
    """``"stdout"`` or ``"stderr"``."""

    text: str
    """URL-free safe text chunk (never a complete secret or URL)."""

    hostnames: tuple[str, ...] = ()
    """Normalized hostnames removed from ``text`` (structured branch only)."""

    url_fingerprints: tuple[str, ...] = ()
    """Ordered ephemeral URL fingerprints (structured branch only)."""

    finalized: bool = True
    """``True`` for a complete diagnostic line, ``False`` for a prefix."""

    overflowed: bool = False
    """``True`` when the line was truncated by the bounded line limit."""


class DiagnosticSink(Protocol):
    """Constructor-owned prompt-returning diagnostic callback."""

    def __call__(self, chunk: StreamChunk) -> None:
        """Handle one redacted chunk; return promptly (no blocking I/O)."""
        ...


class _InternalDirectEnqueueSink:
    """Nominal internal wrapper authorizing reader-thread sink invocation."""

    def __init__(self, sink: DiagnosticSink) -> None:
        self._sink = sink

    def __call__(self, chunk: StreamChunk) -> None:
        self._sink(chunk)


def _internal_direct_enqueue_sink(
    sink: DiagnosticSink,
) -> _InternalDirectEnqueueSink:
    """Wrap a facade-owned enqueue callback for direct collector admission."""
    return _InternalDirectEnqueueSink(sink)


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
    truncated.  The one selected retained tail per stream is attached to the
    raised exception as a :class:`StreamingCapture` (``retained_capture``)
    plus the already-selected ``diagnostic_tail``/``diagnostic_stream`` pair,
    so failure-context extraction reuses the existing buffers instead of
    re-projecting them.
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


#: Attribute under which a failed collection attaches the single bounded
#: retained tail per stream.  It references exactly the strings the
#: successful :class:`StreamingCapture` would carry, so no second retained
#: buffer is introduced; failure-context extraction reads the already
#: selected representation instead of re-projecting it.
RETAINED_CAPTURE_ATTR = "retained_capture"


def _attach_retained_capture(
    exc: BaseException, capture: StreamingCapture
) -> None:
    """Attach the already-selected retained capture to *exc* before raising.

    ``diagnostic_tail``/``diagnostic_stream`` mirror the assembler failure
    convention (stderr preferred, then stdout) so host failure-context
    extraction can reuse the selected representation verbatim.  The full
    capture keeps both streams' bounded tails and the truncation notice for
    the concurrent-timeout path.  The original exception, any cleanup notes,
    and the truncation notice are preserved.  Attaching context must never
    displace the primary failure, so an exception that refuses the attribute
    assignment is left unchanged.
    """
    stderr_tail = capture.stderr_tail.strip()
    stdout_tail = capture.stdout_tail.strip()
    if stderr_tail:
        selected_tail, selected_stream = stderr_tail, STREAM_STDERR
    elif stdout_tail:
        selected_tail, selected_stream = stdout_tail, STREAM_STDOUT
    else:
        selected_tail, selected_stream = "", None
    try:
        object.__setattr__(exc, RETAINED_CAPTURE_ATTR, capture)
        object.__setattr__(exc, "diagnostic_tail", selected_tail)
        object.__setattr__(exc, "diagnostic_stream", selected_stream)
    except Exception:
        # Preserve the primary failure even if context cannot be attached.
        pass


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
    network_url_display: NetworkUrlDisplay | None = None,
) -> StreamingCapture:
    """Drain two byte pipes concurrently through redacting streams.

    *stdout_read*/*stderr_read* are blocking readers accepting a maximum
    byte count and returning ``b""`` at EOF.  Both pipes are drained
    concurrently so a producer cannot deadlock on either pipe; the structured
    branch emits committed terminal-safe prefixes promptly without waiting for
    a newline and finalizes each line at its record boundary, and retained
    tails are bounded.  Complete-line consumers (classification, identity,
    grouping, and durable output) must consume only finalized
    :class:`StreamChunk` items.  When
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
    if network_url_display is None:
        network_url_display = NetworkUrlDisplay.REDACTED
    if not isinstance(network_url_display, NetworkUrlDisplay):
        raise ValueError("network_url_display must be a NetworkUrlDisplay")
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
    direct_sink = isinstance(sink, _InternalDirectEnqueueSink)
    dispatcher = (
        SinkDispatcher(sink)
        if sink is not None and not direct_sink
        else None
    )

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
        live_chunk = (
            chunk if isinstance(chunk, StreamChunk) else StreamChunk(tag, chunk)
        )
        if dispatcher is not None:
            dispatcher.submit(live_chunk)
        elif direct_sink and sink is not None:
            # The explicitly marked facade path admits directly into its own
            # thread-safe inbox. Arbitrary SDK callbacks retain dispatcher
            # isolation and are never inferred to be safe from callability.
            sink(live_chunk)

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

    # Capture the one selected retained tail per stream after the readers
    # have joined and the dispatcher has finalized.  Failure paths attach
    # these existing buffers to the raised exception instead of returning or
    # re-projecting them.
    retained = StreamingCapture(
        stdout_tail=stdout_stream.tail(),
        stderr_tail=stderr_stream.tail(),
        truncation_notice=notice,
    )

    if interrupted is not None:
        for exc in cleanup_failures:
            interrupted.add_note(
                "interruption cleanup failed "
                f"({type(exc).__name__}): "
                f"{_redacted_exception_detail(exc, secrets, tail_projector)}"
            )
        _attach_retained_capture(interrupted, retained)
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
        _attach_retained_capture(primary, retained)
        raise primary
    return retained


__all__ = [
    "DISPATCHER_DRAIN_BUDGET_SECONDS",
    "DiagnosticSink",
    "DiagnosticStream",
    "READ_CHUNK_BYTES",
    "READER_FAILURE_DETAIL_BYTES",
    "REDACTED",
    "RETAINED_CAPTURE_ATTR",
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
