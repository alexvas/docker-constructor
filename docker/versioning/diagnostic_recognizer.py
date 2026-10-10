"""Compact, bounded raw recognizer for separator-based diagnostic URL candidates.

Phase 2 (P2) of ``linearize-diagnostic-url-detection`` replaces the legacy
unanchored ``_URL_START_RE.search`` hotspot with a linear recognizer that
produces the P1 :class:`~docker.versioning.diagnostic_recognition.RecognitionResult`
contract.  It does not rewire the projector (that is P3); it is the raw
recognition engine the projector will consume.

Algorithm
---------

The legacy detector decodes the raw text in bounded percent passes and runs a
single regular expression on each decoded view, taking the smallest raw source
index over the views.  This module computes the same answer without ever
materialising a decoded view and without rescanning a growing suffix:

* ``_Transducer`` nodes form a fixed chain ``L0 -> L1 -> L2 -> L3``.  Each
  transducer consumes one layer's characters and emits the characters of the
  next layer, decoding a complete ``%XX`` into one character and copying
  everything else verbatim -- exactly the legacy per-layer decode -- while
  carrying each emitted character's inclusive raw start and exclusive raw end
  plus whether it descends from a non-terminator (token) character.
* ``_MatchObserver`` is attached to every layer.  It groups incoming
  characters into *scheme atoms* (a literal scheme character or a valid
  ``%XX`` unit) and runs the small ``FIRST UNIT* COLON SLASH SLASH`` automaton
  in O(1) state, recording the leftmost raw start (and the colon's raw source)
  the layer produces.  The recognizer selects the minimum raw start over the
  four layers.
* ``_PrefixObserver`` is attached to every layer and tracks the
  ``_url_prefix_state_single`` state of the current raw token from its first
  character, so unresolved-prefix (``NEED_MORE``) reporting matches the legacy
  integer prefix state without copying decoded text.

The cascade is bounded: every transducer holds at most three pending
characters, every match observer holds at most a handful of queued atoms, and
each raw character visits a constant number of layers.  Recognition is O(N) in
the raw input length with bounded state, and no input-sized decoded string,
source map or encoded-variant table is ever built.

Coordinates follow the P1 contract: ``start``/``end``/``retain_from`` are
absolute raw Python character offsets within the supplied text plus the
optional ``offset``.  ``start`` is inclusive, ``end`` exclusive, and
``end=None`` marks a detected-but-unterminated candidate.

Fixed-depth transition/state bound
----------------------------------

The recognition cascade has fixed depth and fixed state:

* ``_LAYERS`` is a constant (four); a raw character is consumed by layer 0 and
  every decoded character it produces is consumed by at most the remaining
  layers, so each raw character causes a constant number of transitions.
* Every ``_Transducer`` retains at most three pending characters, so the
  pending state is O(1) independently of input length and never a decoded
  buffer.
* Every ``_MatchObserver`` queues at most three atoms and runs a constant
  number of state updates per atom; every ``_PrefixObserver`` retains at most
  two pending characters and stops at the first terminal character.
* Therefore recognition is O(N) in the raw input with bounded state.  The
  design's branching case (an encoded unit such as ``%3A`` being either a
  conservative scheme unit or a colon, and ``%2F`` a scheme unit or a slash)
  is handled by checking the ``COLON SLASH SLASH`` triple at every atom while
  the run state is updated only for committed atoms, so the extra branch still
  costs a constant per atom and does not enumerate decoded variants.

Detection versus interpretation depth
-------------------------------------

The effective *detection* depth is four: the observer at layer ``k`` accepts
one more encoded structural unit on top of ``k`` decoding passes, which is the
legacy ``URL_DECODE_LAYER_LIMIT`` three-pass-plus-encoded-separator language.
Detection is deliberately separate from *interpretation*: this module only
finds a candidate's raw extent and classifies its scheme against the fixed
whitelist.  It never parses a URL, validates an authority or path, or derives
metadata, so a depth-four detection match never authorises an additional
whole-URL interpretation pass.  A longer scheme is still a candidate but
classifies ``UNKNOWN``; the classification copy is bounded by
``_CLASSIFY_BOUND``, which is derived from the longest whitelist scheme and
the maximum supported percent-encoding expansion (``3 ** _LAYERS``), so every
supported spelling of a known scheme is still classified while an unsupported
scheme length is never copied.  Scheme length is never a performance-only
rejection rule.

When several layers expose the same raw start, the selected layer's colon raw
source is retained so the decoded scheme span is classified consistently;
start-index dominance is used only because the per-layer automata compute the
same conservative language, not as a substitute for classification state.
"""
from __future__ import annotations

from collections import namedtuple
from dataclasses import dataclass

from docker.versioning.diagnostic_recognition import (
    KNOWN_SCHEMES,
    CandidateKind,
    DiagnosticCandidate,
    RecognitionResult,
    classify_scheme,
)

#: Number of decoded views the legacy detector searches: one literal view plus
#: ``URL_DECODE_LAYER_LIMIT`` decoding passes.
_LAYERS = 4

#: Bound on the raw scheme span copied for classification.  The classifier
#: defines ``KNOWN`` by decoding up to ``_LAYERS`` percent layers, so every
#: supported spelling of a whitelist scheme expands by at most ``3 ** _LAYERS``
#: (81) raw characters per decoded character, including separately encoded
#: escape characters.  The longest whitelist scheme therefore fixes the bound:
#: anything longer cannot decode to a known scheme, so it is classified
#: ``UNKNOWN`` before any scheme-sized copy is made.
_CLASSIFY_BOUND = max(len(scheme) for scheme in KNOWN_SCHEMES) * (3 ** _LAYERS)

_TERMINATOR_CHARS = frozenset(" \t\r\n\f\v\"'`<>|\\^{}")
_HEX = frozenset("0123456789abcdefABCDEF")
_SCHEME_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789"
    "+.-"
)

#: Single-layer prefix states, ordered so ``max`` selects the furthest.
_PREFIX_NONE = 0
_PREFIX_SCHEME = 1
_PREFIX_SEPARATOR = 2


@dataclass(frozen=True, slots=True)
class RecognitionStats:
    """Deterministic structural counters for one recognition pass.

    These counters are structural, never timed: ``layer_char_visits`` counts
    characters presented to layer observers, ``emissions`` counts transducer
    outputs, ``atoms`` counts scheme atoms grouped by match observers,
    ``transitions`` counts committed atom state transitions, and the two peak
    values bound the retained state.  They exist so tests can assert bounded
    work and state without measuring elapsed time.
    """

    chars: int
    layer_char_visits: int
    emissions: int
    atoms: int
    transitions: int
    peak_pending_chars: int
    peak_atom_queue: int


class _MutableStats:
    __slots__ = (
        "chars",
        "layer_char_visits",
        "emissions",
        "atoms",
        "transitions",
        "peak_pending_chars",
        "peak_atom_queue",
    )

    def __init__(self) -> None:
        self.chars = 0
        self.layer_char_visits = 0
        self.emissions = 0
        self.atoms = 0
        self.transitions = 0
        self.peak_pending_chars = 0
        self.peak_atom_queue = 0

    def freeze(self) -> RecognitionStats:
        return RecognitionStats(
            chars=self.chars,
            layer_char_visits=self.layer_char_visits,
            emissions=self.emissions,
            atoms=self.atoms,
            transitions=self.transitions,
            peak_pending_chars=self.peak_pending_chars,
            peak_atom_queue=self.peak_atom_queue,
        )


def _is_terminator(character: str) -> bool:
    """Return whether *character* ends a raw token (legacy terminator rule)."""
    return (
        character in _TERMINATOR_CHARS
        or ord(character) < 0x20
        or ord(character) == 0x7F
    )


def _find_terminator(text: str, start: int = 0) -> int | None:
    """Return the first raw token terminator at or after *start*, or ``None``."""
    for index in range(start, len(text)):
        if _is_terminator(text[index]):
            return index
    return None


def _last_terminator_index(text: str) -> int:
    """Return the index of the last raw token terminator, or ``-1``."""
    for index in range(len(text) - 1, -1, -1):
        if _is_terminator(text[index]):
            return index
    return -1


def _decode_one_layer(text: str) -> str:
    """Decode one bounded percent pass exactly like the legacy decoder."""
    out: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        character = text[index]
        if character == "%" and index + 2 < length:
            pair = text[index + 1 : index + 3]
            if pair[0] in _HEX and pair[1] in _HEX:
                out.append(chr(int(pair, 16)))
                index += 3
                continue
        out.append(character)
        index += 1
    return "".join(out)


def _classify_raw_scheme(raw_scheme: str) -> CandidateKind:
    """Classify a raw scheme span with bounded repeated percent decoding.

    Callers guard ``len(raw_scheme) <= _CLASSIFY_BOUND`` before slicing the
    scheme out of the input, so the bounded copies made here are
    constant-size and never input-sized.  ``_CLASSIFY_BOUND`` covers every
    supported spelling of a whitelist scheme (longest scheme times the
    ``3 ** _LAYERS`` percent-encoding expansion); the length check is retained
    only as a defensive bound.  Classification is a policy query: it never
    authorises URL interpretation.
    """
    if not raw_scheme or len(raw_scheme) > _CLASSIFY_BOUND:
        return CandidateKind.UNKNOWN
    current = raw_scheme
    for _ in range(_LAYERS):
        if "%" not in current:
            break
        decoded = _decode_one_layer(current)
        if decoded == current:
            break
        current = decoded
    return classify_scheme(current)


#: Inclusive raw ``start`` and exclusive raw ``end`` of one scheme atom.
_Atom = namedtuple(
    "_Atom", ("unit", "first", "colon", "slash", "source_start", "source_end")
)


def _literal_atom(character: str, source_start: int, source_end: int) -> _Atom:
    return _Atom(
        unit=character in _SCHEME_CHARS,
        first=character.isascii() and character.isalpha(),
        colon=character == ":",
        slash=character == "/",
        source_start=source_start,
        source_end=source_end,
    )


def _escape_atom(decoded: str, source_start: int, source_end: int) -> _Atom:
    # Any complete ``%XX`` is both a unit and a valid first unit, exactly as
    # the legacy ``_SCHEME_UNIT`` / ``_SCHEME_FIRST_UNIT`` grammar allows.
    return _Atom(
        unit=True,
        first=True,
        colon=decoded == ":",
        slash=decoded == "/",
        source_start=source_start,
        source_end=source_end,
    )


class _Transducer:
    """One layer of the percent-decoding chain with raw range provenance.

    Each emitted character carries the inclusive ``start`` and exclusive
    ``end`` of the raw characters that produced it: a decoded ``%XX`` spans
    all three raw characters it consumed (and, when the escape itself came from
    an earlier layer, spans the contributing ranges of its three inputs), a
    copied literal spans exactly its one raw character, and a malformed or
    incomplete escape is copied with the literal ranges of its characters.
    """

    __slots__ = ("_sink", "_stats", "_pending")

    def __init__(self, sink, stats: _MutableStats) -> None:
        self._sink = sink
        self._stats = stats
        self._pending: list[tuple[str, int, int, bool]] = []

    @property
    def pending(self) -> int:
        return len(self._pending)

    def _emit(
        self,
        character: str,
        source_start: int,
        source_end: int,
        from_token: bool,
    ) -> None:
        self._stats.emissions += 1
        self._sink(character, source_start, source_end, from_token)

    def feed(
        self,
        character: str,
        source_start: int,
        source_end: int,
        from_token: bool,
    ) -> None:
        self._pending.append((character, source_start, source_end, from_token))
        self._flush_ready()

    def _flush_ready(self) -> None:
        pending = self._pending
        while pending:
            character, source_start, source_end, from_token = pending[0]
            if character != "%":
                pending.pop(0)
                self._emit(character, source_start, source_end, from_token)
                continue
            if len(pending) < 3:
                return
            first, second = pending[1][0], pending[2][0]
            if first in _HEX and second in _HEX:
                decoded = chr(int(first + second, 16))
                span_end = pending[2][2]
                del pending[:3]
                self._emit(decoded, source_start, span_end, from_token)
                continue
            pending.pop(0)
            self._emit(character, source_start, source_end, from_token)

    def flush(self) -> None:
        # Emit every pending character literally.  At end of input a trailing
        # incomplete or invalid escape is copied verbatim; at an internal token
        # boundary the pending escape belongs to the token that just ended, so
        # flushing it before the terminator keeps it out of the next token.
        while self._pending:
            character, source_start, source_end, from_token = self._pending.pop(0)
            self._emit(character, source_start, source_end, from_token)


class _MatchObserver:
    """Leftmost ``FIRST UNIT* COLON SLASH SLASH`` start for one layer."""

    __slots__ = (
        "_stats",
        "_buf",
        "_queue",
        "_run_units",
        "_run_first",
        "_run_first_end",
        "_done",
        "match_start",
        "match_start_end",
        "match_colon",
        "match_colon_end",
    )

    def __init__(self, stats: _MutableStats) -> None:
        self._stats = stats
        self._buf: list[tuple[str, int, int]] = []
        self._queue: list[_Atom] = []
        self._run_units = 0
        self._run_first: int | None = None
        self._run_first_end: int | None = None
        self._done = False
        self.match_start: int | None = None
        self.match_start_end: int | None = None
        self.match_colon: int | None = None
        self.match_colon_end: int | None = None

    def feed(self, character: str, source_start: int, source_end: int) -> None:
        if self._done:
            return
        self._buf.append((character, source_start, source_end))
        self._emit_ready()

    def _emit_ready(self) -> None:
        buf = self._buf
        while buf:
            character = buf[0][0]
            if character != "%":
                _, source_start, source_end = buf.pop(0)
                self._atom(_literal_atom(character, source_start, source_end))
                continue
            if len(buf) < 3:
                return
            first, second = buf[1][0], buf[2][0]
            if first in _HEX and second in _HEX:
                decoded = chr(int(first + second, 16))
                source_start = buf[0][1]
                source_end = buf[2][2]
                del buf[:3]
                self._atom(_escape_atom(decoded, source_start, source_end))
                continue
            _, source_start, source_end = buf.pop(0)
            self._atom(_literal_atom("%", source_start, source_end))

    def _atom(self, atom: _Atom) -> None:
        self._stats.atoms += 1
        queue = self._queue
        queue.append(atom)
        if len(queue) > self._stats.peak_atom_queue:
            self._stats.peak_atom_queue = len(queue)
        while queue:
            head = queue[0]
            if (
                head.colon
                and self._run_units >= 1
                and self._run_first is not None
            ):
                if len(queue) < 3:
                    return
                if queue[1].slash and queue[2].slash:
                    self.match_start = self._run_first
                    self.match_start_end = self._run_first_end
                    self.match_colon = head.source_start
                    self.match_colon_end = head.source_end
                    self._done = True
                    queue.clear()
                    return
            queue.pop(0)
            self._commit(head)

    def _commit(self, atom: _Atom) -> None:
        self._stats.transitions += 1
        if atom.unit:
            if self._run_units == 0:
                self._run_first = None
                self._run_first_end = None
            if self._run_first is None and atom.first:
                self._run_first = atom.source_start
                self._run_first_end = atom.source_end
            self._run_units += 1
        else:
            self._run_units = 0
            self._run_first = None
            self._run_first_end = None


class _PrefixObserver:
    """Legacy single-layer ``_url_prefix_state_single`` over one layer's token.

    It consumes only the current raw token's characters for its layer (the
    recognizer resets every prefix observer at a raw terminator) and stops as
    soon as the state is terminal, so its retained state is a couple of
    characters.
    """

    __slots__ = ("_state", "_started", "_pend", "_done")

    def __init__(self) -> None:
        self._state = _PREFIX_NONE
        self._started = False
        self._pend: list[str] = []
        self._done = False

    def feed(self, character: str) -> None:
        if self._done:
            return
        if self._pend:
            self._pend.append(character)
            if len(self._pend) == 3:
                self._resolve()
            return
        if character == "%":
            self._pend = ["%"]
            return
        self._literal(character)

    def _resolve(self) -> None:
        first, second = self._pend[1], self._pend[2]
        self._pend = []
        if first in _HEX and second in _HEX:
            self._decoded(chr(int(first + second, 16)))
        else:
            self._stop()

    def _decoded(self, character: str) -> None:
        if character in ":/":
            self._state = _PREFIX_SEPARATOR
            self._done = True
        elif character in _SCHEME_CHARS:
            if not self._started and not character.isalpha():
                self._stop()
            else:
                self._started = True
                self._state = _PREFIX_SCHEME
        else:
            self._stop()

    def _literal(self, character: str) -> None:
        if character in ":/":
            self._state = _PREFIX_SEPARATOR
            self._done = True
        elif character in _SCHEME_CHARS:
            if not self._started and not character.isalpha():
                self._stop()
            else:
                self._started = True
                self._state = _PREFIX_SCHEME
        else:
            self._stop()

    def _stop(self) -> None:
        self._state = _PREFIX_NONE
        self._done = True

    def finish(self) -> int:
        if self._pend and not self._done:
            # A trailing ``%`` or ``%X`` is an incomplete escape.
            self._state = _PREFIX_SCHEME
        self._pend = []
        return self._state


class _Pipeline:
    """The fixed transducer/observer cascade for one recognition pass."""

    def __init__(self, stats: _MutableStats) -> None:
        self._stats = stats
        self._match = [_MatchObserver(stats) for _ in range(_LAYERS)]
        self._prefix = [_PrefixObserver() for _ in range(_LAYERS)]
        self._transducers = [
            _Transducer(self._sink_for(layer + 1), stats)
            for layer in range(_LAYERS - 1)
        ]

    def _sink_for(self, layer: int):
        def sink(
            character: str,
            source_start: int,
            source_end: int,
            from_token: bool,
        ) -> None:
            self._consume(layer, character, source_start, source_end, from_token)

        return sink

    def _consume(
        self,
        layer: int,
        character: str,
        source_start: int,
        source_end: int,
        from_token: bool,
    ) -> None:
        self._stats.layer_char_visits += 1
        self._match[layer].feed(character, source_start, source_end)
        if from_token:
            self._prefix[layer].feed(character)
        if layer < _LAYERS - 1:
            self._transducers[layer].feed(
                character, source_start, source_end, from_token
            )
            pending = self._transducers[layer].pending
            if pending > self._stats.peak_pending_chars:
                self._stats.peak_pending_chars = pending

    def _reset_prefix(self) -> None:
        self._prefix = [_PrefixObserver() for _ in range(_LAYERS)]

    def feed(self, text: str) -> None:
        self._stats.chars += len(text)
        for index, character in enumerate(text):
            from_token = not _is_terminator(character)
            if not from_token:
                # The pending escape of the token that is ending belongs to
                # that token, so flush it before crossing the boundary.  This
                # keeps a trailing ``%`` from leaking into the next token's
                # prefix observers.
                self._flush_token()
            self._consume(0, character, index, index + 1, from_token)
            if not from_token:
                self._reset_prefix()

    def _flush_token(self) -> None:
        for transducer in self._transducers:
            transducer.flush()

    def finish(self) -> int:
        self._flush_token()
        return max(observer.finish() for observer in self._prefix)

    def best_match(self) -> tuple[int, int] | None:
        best: tuple[int, int] | None = None
        for observer in self._match:
            if observer.match_start is None:
                continue
            assert observer.match_colon is not None
            candidate = (observer.match_start, observer.match_colon)
            if best is None or candidate < best:
                best = candidate
        return best


def _recognize(text: str, offset: int) -> tuple[RecognitionResult, RecognitionStats]:
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise TypeError("offset must be an integer")
    if offset < 0:
        raise ValueError("offset must be non-negative")

    stats = _MutableStats()
    pipeline = _Pipeline(stats)
    pipeline.feed(text)
    prefix_state = pipeline.finish()

    match = pipeline.best_match()
    if match is not None:
        start, colon = match
        end = _find_terminator(text, start)
        # Guard the span length *before* slicing so an arbitrarily long scheme
        # is never copied; it stays detectable but classifies ``UNKNOWN``.
        if colon - start > _CLASSIFY_BOUND:
            kind = CandidateKind.UNKNOWN
        else:
            kind = _classify_raw_scheme(text[start:colon])
        candidate = DiagnosticCandidate(
            start=start + offset,
            end=None if end is None else end + offset,
            kind=kind,
        )
        return RecognitionResult.matched(candidate), stats.freeze()

    if prefix_state != _PREFIX_NONE:
        retain_from = _last_terminator_index(text) + 1
        return (
            RecognitionResult.need_more(retain_from + offset),
            stats.freeze(),
        )
    return RecognitionResult.no_match(), stats.freeze()


def recognize(text: str, *, offset: int = 0) -> RecognitionResult:
    """Return the earliest separator-based candidate in *text*.

    ``MATCH`` carries the earliest raw candidate, whose ``end`` is ``None``
    when no raw terminator follows it.  With no candidate, ``NEED_MORE``
    reports the trailing token's retention start when that token is still a
    viable scheme/separator prefix, and ``NO_MATCH`` otherwise.  All offsets
    are absolute raw character coordinates within *text* plus *offset*.
    """
    result, _stats = _recognize(text, offset)
    return result


def recognize_with_stats(
    text: str, *, offset: int = 0
) -> tuple[RecognitionResult, RecognitionStats]:
    """Like :func:`recognize`, additionally returning structural work counters.

    The counters are deterministic and timing-free; they exist so automated
    tests can assert bounded work and state rather than elapsed time.
    """
    return _recognize(text, offset)


__all__ = [
    "RecognitionStats",
    "recognize",
    "recognize_with_stats",
]
