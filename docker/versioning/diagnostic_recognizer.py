"""Compact, bounded raw recognizer for supported diagnostic URLs and secrets.

Phase 2 (P2) of ``linearize-diagnostic-url-detection`` replaces the legacy
unanchored URL-start search with a linear recognizer that recognises **only**
the fixed supported scheme set, integrates the P1A Aho-Corasick core, and
matches registered literal secrets (plus, inside recognised URL views, up to
three percent-decoded secret views).  It does not rewire the projector (that is
P3); it is the raw recognition engine the projector will consume.

Supported-only detection
------------------------

Detection recognises exactly the case-insensitive schemes in
:data:`~docker.versioning.diagnostic_recognition.SUPPORTED_SCHEMES` followed by
a supported ``://`` spelling.  A scheme outside that set -- ``npm``,
``custom+transport``, ``gopher`` or any other arbitrary RFC-valid scheme -- is
**not** a candidate: it remains ordinary text and produces no URL metadata.
The recognizer selects the earliest *supported* start, including within longer
text, so ``TOKENhttps://host`` is a candidate at ``https`` rather than an
unknown candidate at ``TOKEN``.  A candidate is only a raw extent; it is never
proof of URL validity.

Encoding depth and raw provenance
---------------------------------

Scheme and secret recognition run over a fixed chain of percent transducers
``L0 -> L1 -> L2 -> L3``.  Supported **scheme** characters are interpreted
through at most three percent-decoding passes: the last matcher treats a
character that only its own atomisation would decode as a separator unit rather
than a scheme character, so a four-pass-encoded scheme is not a candidate.
The **separator** keeps its effective depth four, so a recognised (literal or
at-most-three-pass) scheme followed by ``%2525253A%2525252F%2525252F`` is still
detected.  A depth-five separator is not recognised and never authorises a
fourth whole-URL interpretation pass.  Every emitted character carries its
inclusive raw ``start`` and exclusive raw ``end`` so a match reports the raw
source range that produced it -- a decoded ``%XX`` spans all three (or, when
nested, all contributing) raw characters.

Secrets
-------

Registered literal secrets are matched with the P1A core against the original
raw stream (``depth == 0``) and, inside recognised URL spans, against each of
the first three percent-decoded views (``depth`` 1--3).  Ordinary text keeps
literal-only secret matching: decoded matches are reported only for a caller
whose span is a recognised URL.  A secret is never rejected for a space, a
Text/URL crossing, a raw terminator or a full/partial scheme suffix.  Matches
are de-duplicated by raw span (shallowest decode depth wins), and
:func:`select_secret_matches` performs the leftmost/longest selection used by
the integration.

Fixed-depth transition/state bounds
-----------------------------------

* ``_LAYERS`` is a constant (four); a raw character is consumed by layer 0 and
  each character it decodes to is consumed by at most the remaining layers, so
  each raw character causes a constant number of transitions.  Recognition is
  O(raw input) in work.
* Every transducer retains fewer than three pending escape characters, every
  layer matcher queues at most three atoms, and the supported-scheme prefix
  tracker keeps at most one active trie node per distinct supported prefix.
  These are bounded by the fixed scheme set, not by input length.
* Registration storage (the compiled scheme automaton and any registered secret
  automaton) depends only on the configured patterns.  Live search state --
  cursor node, prefix trie positions, bounded provenance windows -- never grows
  with the stream history; no input-sized decoded search copy, per-character
  source map or encoded-variant table is built.
* Decoded secret observers retain only matches that can still belong to a
  supported URL.  Once no supported prefix is viable in the current token and
  no candidate has been detected, ineligible decoded matches are discarded, so
  ordinary text (for example repeated ``%54OKEN``) never accumulates decoded
  speculation; literal secret matching is unaffected and spans all text and
  raw terminators. Open-token speculation uses one ordered deque per
  registered pattern. Fixed-length pattern emissions have increasing starts
  and ends, including decoded provenance. Finalization merges only these
  registration-many heads into an end-ordered finalized deque. No secret
  maintenance heap or input-sized sort is used. Pruning/retirement pop heads;
  straddlers stay in place. At P registered patterns, cumulative maintenance
  is O(P * (N + M + commits)), linear at fixed registration, including long
  single-token finalization. Outcome storage remains output-sensitive.
* Candidate terminator locations are cached per raw token, so every raw
  character is scanned at most once across all candidates in that token, and
  decoded-secret coverage merges candidate spans into disjoint intervals and
  sweeps them monotonically against start-ordered secrets.  Per-layer candidate
  matches are recorded in increasing raw start order, so ``retire`` pops
  committed candidates from the left rather than re-filtering the remaining
  deque on every commit.  ``RecognitionStats`` reports
  ``speculative_record_visits``, ``secret_record_transfers``,
  ``secret_ordering_work``, ``candidate_retirement_visits``,
  ``boundary_chars`` and ``coverage_checks`` so this maintenance, pruning and
  coverage work is visible and assertable without timing.

Coordinate semantics follow the P1 contract: ``start``/``retain_from`` are
absolute raw Python character offsets, ``start`` inclusive and ``end``
exclusive, and ``end=None`` marks a detected-but-unterminated candidate.

Incremental continuation
------------------------

:class:`IncrementalRecognizer` consumes consecutive raw chunks without
rescanning committed text.  It owns only registration structures and live
cursor/decoder state; the source payload stays with the caller.  A commit
retires only already-recorded outcomes whose raw span lies completely before
the committed offset, while preserving the Aho-Corasick cursors, the percent
decoder pending units and every supported URL prefix that overlaps the commit.
Committed text never resurfaces as a candidate, secret or prefix, and a commit
never re-feeds retained input.
"""
from __future__ import annotations

from collections import deque, namedtuple
from dataclasses import dataclass, replace

from docker.versioning.aho_corasick import AhoCorasick
from docker.versioning.diagnostic_recognition import (
    SUPPORTED_SCHEMES,
    DiagnosticCandidate,
    RecognitionResult,
)

#: Number of layer matchers: one literal view plus three decoding passes.  A
#: matcher atomises one further encoded level, so the deepest recognised
#: separator spelling is depth four.
_LAYERS = 4

#: Lower-case compiled scheme patterns for the shared P1A automaton.
_SCHEME_PATTERNS = tuple(sorted(SUPPORTED_SCHEMES))

#: Immutable shared scheme automaton (registration only; cursors are live).
_SCHEME_AUTOMATON = AhoCorasick(_SCHEME_PATTERNS)

#: Longest supported scheme; bounds the per-layer provenance window.
_MAX_SCHEME_LEN = max(len(pattern) for pattern in _SCHEME_PATTERNS)

_HEX = frozenset("0123456789abcdefABCDEF")
_TERMINATOR_CHARS = frozenset(" \t\r\n\f\v\"'`<>|\\^{}")

#: Integer prefix states (``NONE`` is ``0`` for compatibility with callers that
#: only test ``prefix_state() != 0``).
_PREFIX_NONE = 0
_PREFIX_SCHEME = 1


def _fold(character: str) -> str:
    """Return the ASCII lower-case form of *character* (length-preserving)."""
    return character.lower() if "A" <= character <= "Z" else character


def _is_terminator(character: str) -> bool:
    """Return whether *character* ends a raw token."""
    return (
        character in _TERMINATOR_CHARS
        or ord(character) < 0x20
        or ord(character) == 0x7F
    )


def _find_terminator(text: str, start: int = 0) -> int | None:
    """Return the first raw token terminator at or after *start*, or ``None``."""
    for index in range(max(start, 0), len(text)):
        if _is_terminator(text[index]):
            return index
    return None


def _last_terminator_index(text: str) -> int:
    """Return the index of the last raw terminator, or ``-1``."""
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


def _build_url_trie() -> dict:
    """Build the supported ``scheme://`` prefix trie (lower-cased)."""
    root: dict = {}
    for scheme in _SCHEME_PATTERNS:
        node = root
        for character in scheme + "://":
            node = node.setdefault(character, {})
    return root


#: Prefix trie of ``scheme://`` for the fixed supported set.  Its nodes are the
#: only states a supported-scheme prefix can occupy, so the tracker state is
#: bounded by the scheme set rather than by input length.
_SCHEME_URL_TRIE = _build_url_trie()


@dataclass(frozen=True, slots=True)
class RecognitionStats:
    """Deterministic structural counters for one recognition pass.

    These counters are structural, never timed: ``chars`` counts raw characters
    fed, ``layer_char_visits`` counts characters presented to layer matchers,
    ``emissions`` counts transducer outputs, ``atoms`` counts atomised layer
    characters, ``transitions`` counts committed atom state updates and
    ``secret_emissions`` counts characters fed to secret cursors.
    ``speculative_record_visits`` counts secret records examined while pruning
    ineligible speculation and while retiring speculative records,
    ``secret_record_transfers`` counts records promoted from open-token
    speculation to finalized outcomes, ``secret_ordering_work`` counts the
    bucket inspections and queue operations paid to keep finalized outcomes
    end-ordered; occupancy accounting is incremental, ``candidate_retirement_visits`` counts candidate matches
    popped off a matcher during a commit, ``boundary_chars`` counts raw
    characters examined while locating candidate terminators and
    ``coverage_checks`` counts secret versus interval comparisons in the
    coverage sweep; these make the otherwise-hidden maintenance, pruning and
    coverage work visible. ``prefix_checks`` is a deterministic bound on
    supported-prefix / capped-escape inspections; ``failure_traversals`` and
    ``automaton_outputs`` sum every URL/secret core cursor's lifetime work.
    Coordinated secret-record iteration charges ``coverage_checks`` and never
    calls the batch deduplication/sorting helpers. Registration-owned node
    lengths support secret settlement without retained source or replay.
    The ``peak_*`` values bound retained state and the
    ``retained_*`` values report the live unconsumed totals.  They exist so
    tests can assert bounded work and state without measuring elapsed time.
    """

    chars: int
    layer_char_visits: int
    emissions: int
    atoms: int
    transitions: int
    secret_emissions: int
    peak_pending_chars: int
    peak_atom_queue: int
    peak_retained_matches: int
    peak_retained_secrets: int
    retained_matches: int
    retained_secrets: int
    speculative_record_visits: int
    secret_record_transfers: int
    secret_ordering_work: int
    candidate_retirement_visits: int
    boundary_chars: int
    coverage_checks: int
    prefix_checks: int = 0
    failure_traversals: int = 0
    automaton_outputs: int = 0


class _MutableStats:
    __slots__ = (
        "chars",
        "layer_char_visits",
        "emissions",
        "atoms",
        "transitions",
        "secret_emissions",
        "peak_pending_chars",
        "peak_atom_queue",
        "peak_retained_matches",
        "peak_retained_secrets",
        "speculative_record_visits",
        "secret_record_transfers",
        "secret_ordering_work",
        "candidate_retirement_visits",
        "boundary_chars",
        "coverage_checks",
        "prefix_checks",
    )

    def __init__(self) -> None:
        self.chars = 0
        self.layer_char_visits = 0
        self.emissions = 0
        self.atoms = 0
        self.transitions = 0
        self.secret_emissions = 0
        self.peak_pending_chars = 0
        self.peak_atom_queue = 0
        self.peak_retained_matches = 0
        self.peak_retained_secrets = 0
        self.speculative_record_visits = 0
        self.secret_record_transfers = 0
        self.secret_ordering_work = 0
        self.candidate_retirement_visits = 0
        self.boundary_chars = 0
        self.coverage_checks = 0
        self.prefix_checks = 0

    def freeze(self) -> RecognitionStats:
        return RecognitionStats(
            chars=self.chars,
            layer_char_visits=self.layer_char_visits,
            emissions=self.emissions,
            atoms=self.atoms,
            transitions=self.transitions,
            secret_emissions=self.secret_emissions,
            peak_pending_chars=self.peak_pending_chars,
            peak_atom_queue=self.peak_atom_queue,
            peak_retained_matches=self.peak_retained_matches,
            peak_retained_secrets=self.peak_retained_secrets,
            retained_matches=0,
            retained_secrets=0,
            speculative_record_visits=self.speculative_record_visits,
            secret_record_transfers=self.secret_record_transfers,
            secret_ordering_work=self.secret_ordering_work,
            candidate_retirement_visits=self.candidate_retirement_visits,
            boundary_chars=self.boundary_chars,
            coverage_checks=self.coverage_checks,
            prefix_checks=self.prefix_checks,
        )


@dataclass(frozen=True, slots=True)
class SecretMatch:
    """One raw source span covered by a registered literal secret.

    ``start`` is inclusive and ``end`` exclusive, in the same absolute raw
    character coordinates as candidates.  ``depth`` is ``0`` for a literal
    (undecoded) match and ``1``--``3`` for a match found in that many
    percent-decoded URL views.  ``pattern_index`` is the stable index into the
    registered secret tuple.
    """

    pattern_index: int
    start: int
    end: int
    depth: int = 0


#: One decoded layer atom: its global atom index plus inclusive raw ``start``
#: and exclusive raw ``end``.
_Atom = namedtuple("_Atom", ("index", "decoded", "source_start", "source_end"))


class _Transducer:
    """One layer of the percent-decoding chain with raw range provenance.

    Each emitted character carries the inclusive ``start`` and exclusive
    ``end`` of the raw characters that produced it: a decoded ``%XX`` spans all
    three raw characters it consumed (spanning the contributing ranges of its
    inputs when the escape itself came from an earlier layer), a copied literal
    spans exactly its one raw character, and a malformed or incomplete escape
    is copied with the literal ranges of its characters.
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
        # Emit every pending character literally.  At an internal token boundary
        # the pending escape belongs to the token that just ended, so flushing
        # it before the terminator keeps it out of the next token's observers.
        while self._pending:
            character, source_start, source_end, from_token = self._pending.pop(0)
            self._emit(character, source_start, source_end, from_token)


class _LayerMatcher:
    """Supported-scheme matcher and prefix tracker for one decoding layer.

    It atomises the layer character stream (a literal or a complete ``%XX``
    unit), feeds the atom to the shared P1A scheme automaton, records every
    supported scheme immediately followed by ``:`` ``/`` ``/`` (each possibly
    one more encoded level), and tracks the supported ``scheme://`` prefix of
    the current token for ``NEED_MORE`` reporting.
    """

    __slots__ = (
        "_stats",
        "_incremental",
        "_scheme_cap",
        "_cursor",
        "_fed",
        "_raw_starts",
        "_raw_ends",
        "_escaped_atoms",
        "_queue",
        "_pending",
        "_active",
        "_buf",
        "_matches",
        "_token_match_start",
        "match_start",
        "match_start_end",
        "match_colon",
        "match_colon_end",
    )

    def __init__(
        self,
        stats: _MutableStats,
        *,
        incremental: bool = False,
        scheme_cap: bool = False,
    ) -> None:
        self._stats = stats
        self._incremental = incremental
        self._scheme_cap = scheme_cap
        self._cursor = _SCHEME_AUTOMATON.cursor()
        self._fed = 0
        self._raw_starts: dict[int, int] = {}
        self._raw_ends: dict[int, int] = {}
        self._escaped_atoms: set[int] = set()
        self._queue: deque[_Atom] = deque()
        self._pending: dict[int, tuple[int, int | None]] = {}
        self._active: list[tuple[int, dict]] = []
        self._buf: list[tuple[str, int, int]] = []
        self._matches: deque[tuple[int, int | None, int, int, int]] = deque()
        self._token_match_start: int | None = None
        self.match_start: int | None = None
        self.match_start_end: int | None = None
        self.match_colon: int | None = None
        self.match_colon_end: int | None = None

    @property
    def retained_matches(self) -> int:
        return len(self._matches)

    def feed(self, character: str, source_start: int, source_end: int) -> None:
        self._buf.append((character, source_start, source_end))
        self._emit_ready()

    def _emit_ready(self) -> None:
        buf = self._buf
        while buf:
            character = buf[0][0]
            if character != "%":
                _, source_start, source_end = buf.pop(0)
                self._atom(character, source_start, source_end, False)
                continue
            if len(buf) < 3:
                return
            first, second = buf[1][0], buf[2][0]
            if first in _HEX and second in _HEX:
                decoded = chr(int(first + second, 16))
                source_start = buf[0][1]
                source_end = buf[2][2]
                del buf[:3]
                self._atom(decoded, source_start, source_end, True)
                continue
            _, source_start, source_end = buf.pop(0)
            self._atom("%", source_start, source_end, False)

    def _atom(
        self,
        decoded: str,
        source_start: int,
        source_end: int,
        from_escape: bool,
    ) -> None:
        self._stats.atoms += 1
        self._stats.transitions += 1
        index = self._fed
        self._fed += 1
        self._raw_starts[index] = source_start
        self._raw_ends[index] = source_end
        if from_escape:
            self._escaped_atoms.add(index)
        self._feed_prefix(decoded, source_start, from_escape)
        for match in self._cursor.advance(_fold(decoded)):
            if not self._scheme_match_allowed(match.start, match.end):
                continue
            scheme_start = self._raw_starts.get(match.start)
            if scheme_start is None:
                continue
            existing = self._pending.get(match.end)
            if existing is None or scheme_start < existing[0]:
                self._pending[match.end] = (
                    scheme_start,
                    self._raw_ends.get(match.start),
                )
        self._queue.append(_Atom(index, decoded, source_start, source_end))
        if len(self._queue) > self._stats.peak_atom_queue:
            self._stats.peak_atom_queue = len(self._queue)
        self._drain_queue()
        # A scheme match is at most ``_MAX_SCHEME_LEN`` atoms deep and its
        # separator is verified within three more atoms, so provenance older
        # than that can never be referenced again.  This keeps the map bounded
        # by the fixed scheme set instead of by input length.
        limit = _MAX_SCHEME_LEN + 4
        while len(self._raw_starts) > limit:
            oldest = next(iter(self._raw_starts))
            del self._raw_starts[oldest]
            self._raw_ends.pop(oldest, None)
            self._escaped_atoms.discard(oldest)

    def _scheme_match_allowed(self, start: int, end: int) -> bool:
        """Reject schemes that use the separator layer's extra decode step.

        At the capped layer the matcher's own atomisation is the additional
        separator-decoding step; a supported scheme must already be literal in
        that layer's three-pass-decoded input.  A match that consumes an atom
        produced by decoding a ``%XX`` there would be a fourth schema-decoding
        pass and is refused, while the same decoded atoms remain available to
        complete a depth-four separator.
        """
        if not self._scheme_cap:
            return True
        self._stats.prefix_checks += end - start
        return not any(index in self._escaped_atoms for index in range(start, end))

    def _feed_prefix(
        self, decoded: str, source_start: int, from_escape: bool = False
    ) -> None:
        # The extra atomisation pass belongs only to the separator, never
        # to a tentative scheme. No scheme trie edge contains ':' or '/'.
        if self._scheme_cap and from_escape and decoded not in ":/":
            self._active = []
            return
        character = _fold(decoded)
        self._stats.prefix_checks += len(self._active) + 1
        active: list[tuple[int, dict]] = []
        for start, node in self._active:
            child = node.get(character)
            if child is not None:
                active.append((start, child))
        root_child = _SCHEME_URL_TRIE.get(character)
        if root_child is not None:
            active.append((source_start, root_child))
        if not active:
            self._active = []
            return
        best: dict[int, tuple[int, dict]] = {}
        self._stats.prefix_checks += len(active)
        for start, node in active:
            key = id(node)
            current = best.get(key)
            if current is None or start < current[0]:
                best[key] = (start, node)
        self._active = list(best.values())

    def _drain_queue(self) -> None:
        queue = self._queue
        while queue:
            index = queue[0].index
            if index in self._pending:
                if len(queue) < 3:
                    return
                if (
                    queue[0].decoded == ":"
                    and queue[1].decoded == "/"
                    and queue[2].decoded == "/"
                ):
                    scheme_start, scheme_start_end = self._pending[index]
                    self._record_match(
                        scheme_start,
                        scheme_start_end,
                        queue[0].source_start,
                        queue[0].source_end,
                        queue[2].source_end,
                    )
                del self._pending[index]
            queue.popleft()

    def _record_match(
        self,
        start: int,
        start_end: int | None,
        colon: int,
        colon_end: int,
        separator_end: int,
    ) -> None:
        if self._token_match_start is None or start < self._token_match_start:
            self._token_match_start = start
        if self.match_start is None:
            self.match_start = start
            self.match_start_end = start_end
            self.match_colon = colon
            self.match_colon_end = colon_end
        elif not self._incremental:
            return
        # ``_drain_queue`` records a candidate exactly when its ``://`` reaches
        # the front of the atom queue, so matches are appended in increasing
        # raw ``start`` order within this layer.  ``retire`` relies on it to
        # pop committed candidates from the left.
        self._matches.append((start, start_end, colon, colon_end, separator_end))
        if len(self._matches) > self._stats.peak_retained_matches:
            self._stats.peak_retained_matches = len(self._matches)

    def unconsumed_match(self) -> tuple[int, int | None, int, int, int] | None:
        if not self._matches:
            return None
        return self._matches[0]

    def all_matches(self) -> tuple[tuple[int, int | None, int, int, int], ...]:
        return tuple(self._matches)

    def reset_prefix(self) -> None:
        self._active = []
        self._token_match_start = None

    def token_url_start(self) -> int | None:
        """Earliest start from which a supported URL could still cover text.

        It is the minimum of the currently viable supported prefix and any
        candidate already detected in the current token.  ``None`` means the
        current token cannot contain (or continue) a supported URL, so decoded
        secret matches that reach here are ineligible.
        """
        prefix = self.prefix_state()
        if self._token_match_start is None:
            return prefix
        if prefix is None:
            return self._token_match_start
        return min(prefix, self._token_match_start)

    def retire(self, offset: int) -> None:
        """Drop committed outcomes incrementally, keeping continuation state.

        Candidates are appended to ``_matches`` in increasing raw start order
        for this matcher's own decoding layer (each layer owns a separate
        queue), so committed outcomes can be popped from the left instead of
        re-filtering the whole deque on every commit.  Only already-recorded
        candidates are inspected, so successive commits retire each candidate
        once.
        """
        while self._matches and self._matches[0][0] < offset:
            self._matches.popleft()
            self._stats.candidate_retirement_visits += 1
        # Coverage eligibility belongs to the token, not the display commit.
        # Moving this start to the cut would prune secrets straddling it.

    def prefix_state(self) -> int | None:
        """Earliest raw start of a viable supported ``scheme://`` prefix.

        ``None`` means the current token cannot grow into a supported
        candidate.  A trailing valid-prefix escape (``%`` or ``%<hex>``) can
        still supply the next needed character, so it counts as viable even
        though its atom has not been emitted yet.
        """
        buf = self._buf
        self._stats.prefix_checks += 3 * len(self._active) + 1
        if self._scheme_cap and buf and buf[0][0] == "%":
            # This extra decode step may only traverse separator edges of
            # an already permitted prefix, never begin or extend a scheme.
            # A partial hex nibble must also be capable of producing that
            # edge (':' is 3A, '/' is 2F).
            nibble = _fold(buf[1][0]) if len(buf) > 1 else None
            starts = [
                start for start, node in self._active
                if any(symbol in node and (nibble is None or nibble == hex_first)
                       for symbol, hex_first in ((":", "3"), ("/", "2")))
            ]
            return min(starts) if starts else None
        starts = [start for start, node in self._active if node]
        if buf and buf[0][0] == "%":
            if len(buf) == 1 or buf[1][0] in _HEX:
                starts.append(buf[0][1])
        return min(starts) if starts else None

    def finish(self) -> int | None:
        """Flush the atom buffer, returning the prefix start seen before it."""
        pending_start = self.prefix_state()
        while self._buf:
            character, source_start, source_end = self._buf.pop(0)
            self._atom(character, source_start, source_end, False)
        return pending_start



class _SecretObserver:
    """Literal or percent-decoded secret matcher for one decode depth.

    It feeds one decoded layer character at a time to a private P1A cursor and
    reports each match with the raw source span that produced it.  The
    provenance window is bounded by the longest registered secret, so live
    state depends on registration, never on stream length.

    Open-token records use one start/end-ordered deque per fixed-length
    registered pattern. Finalization merges registration-many queue heads
    without heaps or input-sized sorting; finalized records are end-ordered.
    Pruning and retirement remove heads only, preserving commit straddlers.
    Work is linear at fixed registration, including single long tokens.
    """

    __slots__ = (
        "_stats",
        "_depth",
        "_cursor",
        "_max_len",
        "_fed",
        "_spans",
        "_matches",
        "_speculative",
        "_retained",
        "_committed",
        "_prefix_lengths",
    )

    def __init__(
        self, automaton: AhoCorasick, stats: _MutableStats, depth: int
    ) -> None:
        self._stats = stats
        self._depth = depth
        self._cursor = automaton.cursor()
        depths = [0] * automaton.node_count
        for node, edges in enumerate(automaton._goto):
            for child in edges.values():
                depths[child] = depths[node] + 1
        self._prefix_lengths = tuple(depths)
        self._max_len = max((len(p) for p in automaton.patterns), default=0)
        self._fed = 0
        self._spans: dict[int, tuple[int, int]] = {}
        #: Finalized matches as an end-ordered deque of ``(end, start,
        #: pattern_index)``.  Batches are appended in increasing end order.
        self._matches: deque[tuple[int, int, int]] = deque()
        # Each fixed-pattern queue has increasing starts AND ends: emissions
        # advance one decoded symbol and pattern length is fixed. Queue count
        # depends only on registration, never the token length.
        self._retained = 0
        self._committed: int | None = None
        self._speculative: tuple[deque[tuple[int, int, int]], ...] = tuple(
            deque() for _ in automaton.patterns
        )

    def feed(self, character: str, source_start: int, source_end: int) -> None:
        self._stats.secret_emissions += 1
        index = self._fed
        self._fed += 1
        self._spans[index] = (source_start, source_end)
        for match in self._cursor.advance(character):
            span = self._spans.get(match.start)
            if span is None:
                continue
            # Advance the cursor/provenance even for delayed committed atoms:
            # they may begin a longer match crossing the cut. Only suppress
            # completed coverage whose raw end is already committed.
            if self._committed is not None and source_end <= self._committed:
                self._stats.speculative_record_visits += 1
                continue
            self._speculative[match.pattern_index].append(
                (span[0], source_end, match.pattern_index)
            )
            self._stats.speculative_record_visits += 1
            self._retained += 1
        while len(self._spans) > max(self._max_len, 1):
            self._spans.pop(next(iter(self._spans)))
        retained = self._retained
        if retained > self._stats.peak_retained_secrets:
            self._stats.peak_retained_secrets = retained

    def prefix_start(self, minimum_start: int | None = None, include_start: int | None = None) -> int | None:
        """Earliest source of a still-extendable secret, even across commits.

        Node lengths are registration-owned. Failure traversal here is bounded
        by registration, not by stream length, and all inspections are counted.
        """
        automaton = self._cursor._automaton
        node = self._cursor.state
        while node:
            self._stats.speculative_record_visits += 1
            if automaton._goto[node]:
                span = self._spans.get(self._fed - self._prefix_lengths[node])
                if span is not None and (minimum_start is None or
                    span[0] >= minimum_start or span[0] == include_start):
                    return span[0]
            node = automaton._fail[node]
        return None

    def matches(self) -> tuple[SecretMatch, ...]:
        finalized = (
            SecretMatch(pattern_index=pattern, start=start, end=end, depth=self._depth)
            for end, start, pattern in self._matches
        )
        speculative = (
            SecretMatch(pattern_index=pattern, start=start, end=end, depth=self._depth)
            for bucket in self._speculative
            for start, end, pattern in bucket
        )
        return tuple(finalized) + tuple(speculative)

    def retain_from(self, frontier: int | None) -> None:
        """Discard speculative matches that cannot belong to a supported URL.

        *frontier* is the earliest raw offset from which a supported URL could
        still cover later text.  ``None`` means the current token cannot
        contain a supported URL, so every speculative match is ineligible.
        Fixed-pattern emission order makes each bucket start-ordered.
        Only obsolete heads are popped; bucket inspections are counted too.
        """
        for bucket in self._speculative:
            self._stats.speculative_record_visits += 1
            while bucket:
                self._stats.speculative_record_visits += 1
                if frontier is not None and bucket[0][0] >= frontier:
                    break
                bucket.popleft()
                self._retained -= 1
                self._stats.speculative_record_visits += 1

    def finalize_token(self) -> None:
        """Promote the current token's eligible matches to final outcomes.

        Merge only registration-many ordered queue heads, never sort or
        heapify an input-sized collection. Work is O(P * (M + 1)) for P
        registered patterns and M promoted records, hence linear at fixed P.
        """
        while True:
            best = None
            for bucket in self._speculative:
                self._stats.secret_ordering_work += 1
                if bucket and (best is None or bucket[0][1] < best[0][1]):
                    best = bucket
            if best is None:
                return
            start, end, pattern = best.popleft()
            self._matches.append((end, start, pattern))
            self._stats.secret_record_transfers += 1
            self._stats.secret_ordering_work += 2

    def retire(self, offset: int) -> None:
        """Drop committed matches, keeping any that straddle the commit.

        Both finalized and per-pattern speculative queues are end-ordered.
        Straddlers remain at the head without being removed or reinserted.
        The watermark also filters later decoder emissions, without resetting
        the cursor or discarding provenance needed by crossing matches.
        """
        self._committed = offset if self._committed is None else max(self._committed, offset)
        for bucket in (self._matches, *self._speculative):
            self._stats.speculative_record_visits += 1
            while bucket:
                self._stats.speculative_record_visits += 1
                end = bucket[0][0] if bucket is self._matches else bucket[0][1]
                if end > offset:
                    break
                bucket.popleft()
                self._retained -= 1
                self._stats.speculative_record_visits += 1


class _Pipeline:
    """The fixed transducer/observer cascade for one recognition pass."""

    def __init__(
        self,
        stats: _MutableStats,
        *,
        incremental: bool = False,
        secrets: tuple[str, ...] = (),
    ) -> None:
        self._stats = stats
        self._incremental = incremental
        self._structure: list[tuple[str, int, int] | None] = [None] * _LAYERS
        self._layer = [
            _LayerMatcher(
                stats,
                incremental=incremental,
                scheme_cap=index == _LAYERS - 1,
            )
            for index in range(_LAYERS)
        ]
        self._transducers = [
            _Transducer(self._sink_for(layer + 1), stats)
            for layer in range(_LAYERS - 1)
        ]
        self._literal: _SecretObserver | None = None
        self._decoded: list[_SecretObserver] = []
        if secrets:
            automaton = AhoCorasick(secrets)
            self._literal = _SecretObserver(automaton, stats, 0)
            self._decoded = [
                _SecretObserver(automaton, stats, depth)
                for depth in range(1, _LAYERS)
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
        if character in "/?#":
            self._structure[layer] = (character, source_start, source_end)
        self._layer[layer].feed(character, source_start, source_end)
        if layer == 0:
            if self._literal is not None:
                self._literal.feed(character, source_start, source_end)
        elif layer - 1 < len(self._decoded):
            self._decoded[layer - 1].feed(character, source_start, source_end)
        if layer < _LAYERS - 1:
            self._transducers[layer].feed(
                character, source_start, source_end, from_token
            )
            pending = self._transducers[layer].pending
            if pending > self._stats.peak_pending_chars:
                self._stats.peak_pending_chars = pending

    def _flush_token(self) -> None:
        for transducer in self._transducers:
            transducer.flush()

    def _reset_prefix(self) -> None:
        for matcher in self._layer:
            matcher.reset_prefix()

    def feed(self, text: str, base: int = 0) -> None:
        self._stats.chars += len(text)
        for index, character in enumerate(text):
            self._structure = [None] * _LAYERS
            from_token = not _is_terminator(character)
            if not from_token:
                self._flush_token()
            self._consume(0, character, base + index, base + index + 1, from_token)
            self._prune_decoded_secrets()
            if not from_token:
                self._finalize_decoded_secrets()
                self._reset_prefix()

    def _active_url_start(self) -> int | None:
        starts = [
            start
            for start in (matcher.token_url_start() for matcher in self._layer)
            if start is not None
        ]
        return min(starts) if starts else None

    def _prune_decoded_secrets(self) -> None:
        if not self._decoded:
            return
        frontier = self._active_url_start()
        # A deeper matcher may not have seen the atom that establishes the
        # prefix yet. Keep coverage from the earliest pending decoder unit:
        # e.g. the first '%' in '%%25368ttps' waits while '%25' has already
        # emitted a secret in a shallower view. Decoder pending provenance is
        # fixed-depth/bounded, so this does not retain arbitrary token history.
        self._stats.prefix_checks += len(self._transducers)
        for transducer in self._transducers:
            if transducer._pending:
                pending_start = transducer._pending[0][1]
                frontier = pending_start if frontier is None else min(frontier, pending_start)
        for observer in self._decoded:
            observer.retain_from(frontier)

    def _finalize_decoded_secrets(self) -> None:
        for observer in self._decoded:
            observer.finalize_token()

    def retire(self, offset: int) -> None:
        """Drop outcomes committed before *offset* without replaying input."""
        for matcher in self._layer:
            matcher.retire(offset)
        if self._literal is not None:
            self._literal.retire(offset)
        for observer in self._decoded:
            observer.retire(offset)

    def finish(self) -> int | None:
        starts = [
            start
            for start in (matcher.prefix_state() for matcher in self._layer)
            if start is not None
        ]
        self._flush_token()
        self._prune_decoded_secrets()
        self._finalize_decoded_secrets()
        return min(starts) if starts else None

    def prefix_state(self) -> int | None:
        starts = [
            start
            for start in (matcher.prefix_state() for matcher in self._layer)
            if start is not None
        ]
        return min(starts) if starts else None

    def matches(self) -> tuple[int, int | None, int, int, int] | None:
        best: tuple[int, int | None, int, int, int] | None = None
        for matcher in self._layer:
            match = matcher.unconsumed_match()
            if match is None:
                continue
            if best is None or match[0] < best[0]:
                best = match
        return best

    def all_matches(self) -> tuple[tuple[int, int | None, int, int, int], ...]:
        matches: list[tuple[int, int | None, int, int, int]] = []
        for matcher in self._layer:
            matches.extend(matcher.all_matches())
        return tuple(matches)

    def retained_matches(self) -> int:
        return sum(matcher.retained_matches for matcher in self._layer)

    def literal_secrets(self) -> tuple[SecretMatch, ...]:
        if self._literal is None:
            return ()
        return self._literal.matches()

    def decoded_secrets(self) -> tuple[SecretMatch, ...]:
        matches: list[SecretMatch] = []
        for observer in self._decoded:
            matches.extend(observer.matches())
        return tuple(matches)

    def all_secrets(self) -> tuple[SecretMatch, ...]:
        return self.literal_secrets() + self.decoded_secrets()

    def stats(self) -> RecognitionStats:
        cursors = [matcher._cursor for matcher in self._layer]
        if self._literal is not None:
            cursors.append(self._literal._cursor)
        cursors.extend(observer._cursor for observer in self._decoded)
        return replace(
            self._stats.freeze(),
            failure_traversals=sum(cursor.counters.failures for cursor in cursors),
            automaton_outputs=sum(cursor.counters.outputs for cursor in cursors),
            retained_matches=self.retained_matches(),
            retained_secrets=len(self.all_secrets()),
        )


def _dedupe_secret_matches(
    matches: tuple[SecretMatch, ...],
) -> tuple[SecretMatch, ...]:
    """De-duplicate by raw span, keeping the shallowest decode depth."""
    unique: dict[tuple[int, int], SecretMatch] = {}
    for match in matches:
        key = (match.start, match.end)
        current = unique.get(key)
        if current is None or match.depth < current.depth:
            unique[key] = match
    return tuple(
        sorted(
            unique.values(),
            key=lambda m: (m.start, -(m.end - m.start), m.depth, m.pattern_index),
        )
    )


def select_secret_matches(
    matches: tuple[SecretMatch, ...],
) -> tuple[SecretMatch, ...]:
    """Return the leftmost/longest non-overlapping secret selection.

    Matches are de-duplicated by raw span first, then scanned left to right,
    preferring the longest match at each start position.  This is the selection
    the projector integration uses; the recognizer itself reports every raw
    span.
    """
    ordered = _dedupe_secret_matches(matches)
    selected: list[SecretMatch] = []
    cursor = -1
    for match in ordered:
        if match.start >= cursor:
            selected.append(match)
            cursor = match.end
    return tuple(selected)


def _candidate_spans(
    text: str,
    offset: int,
    matches: tuple[tuple[int, int | None, int, int, int], ...],
    stats: _MutableStats,
) -> tuple[tuple[int, int], ...]:
    """Absolute ``(start, end)`` spans for candidates, sharing token ends.

    Candidates that begin in the same raw token share its terminator, so the
    forward scan is cached and resumed only when a later candidate starts at or
    past the previous terminator.  Every raw character is examined at most once
    across all candidates instead of once per candidate, and unterminated
    candidates still report ``len(text) + offset``.
    """
    spans: list[tuple[int, int]] = []
    ordered = sorted(matches, key=lambda match: match[0])
    length = len(text)
    cached_end = 0
    cached_limit = -1
    has_cache = False
    for start, _start_end, _colon, _colon_end, _separator_end in ordered:
        relative = start - offset
        if relative < 0:
            relative = 0
        if has_cache and relative < cached_limit:
            spans.append((start, cached_end))
            continue
        end = relative
        while end < length:
            stats.boundary_chars += 1
            if _is_terminator(text[end]):
                break
            end += 1
        if end >= length:
            cached_end = length + offset
            cached_limit = length
        else:
            cached_end = end + offset
            cached_limit = end
        has_cache = True
        spans.append((start, cached_end))
    return tuple(spans)


def _cover_secret_matches(
    secrets: tuple[SecretMatch, ...],
    spans: tuple[tuple[int, int], ...],
    stats: _MutableStats,
) -> tuple[SecretMatch, ...]:
    """Return decoded secrets fully contained in a supported URL span.

    Candidate spans are merged into disjoint intervals once, then *secrets*
    and intervals are swept together with a monotonic index so each record and
    each interval is visited a bounded number of times instead of testing every
    secret against every span.
    """
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    covered: list[SecretMatch] = []
    index = 0
    count = len(merged)
    for secret in sorted(secrets, key=lambda match: (match.start, match.end)):
        stats.coverage_checks += 1
        while index < count and merged[index][1] <= secret.start:
            index += 1
            stats.coverage_checks += 1
        if index >= count:
            break
        stats.coverage_checks += 1
        interval_start, interval_end = merged[index]
        if interval_start <= secret.start and secret.end <= interval_end:
            covered.append(secret)
    return tuple(covered)


def recognize_with_stats(
    text: str, *, offset: int = 0
) -> tuple[RecognitionResult, RecognitionStats]:
    """Return the earliest supported candidate and deterministic counters."""
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise TypeError("offset must be an integer")
    if offset < 0:
        raise ValueError("offset must be non-negative")

    stats = _MutableStats()
    pipeline = _Pipeline(stats)
    pipeline.feed(text, offset)
    prefix_start = pipeline.finish()
    match = pipeline.matches()
    if match is not None:
        start = match[0]
        end = _find_terminator(text, start - offset)
        candidate = DiagnosticCandidate(
            start=start,
            end=None if end is None else end + offset,
        )
        result = RecognitionResult.matched(candidate)
    elif prefix_start is not None:
        result = RecognitionResult.need_more(prefix_start)
    else:
        result = RecognitionResult.no_match()
    return result, pipeline.stats()


def recognize(text: str, *, offset: int = 0) -> RecognitionResult:
    """Return the earliest supported ``scheme://`` candidate in *text*.

    ``MATCH`` carries the earliest supported raw candidate, whose ``end`` is
    ``None`` when no raw terminator follows it.  With no candidate,
    ``NEED_MORE`` reports the earliest raw start of a still-viable supported
    prefix and ``NO_MATCH`` covers ordinary and unknown-scheme text.  All
    offsets are absolute raw character coordinates within *text* plus *offset*.
    """
    result, _stats = recognize_with_stats(text, offset=offset)
    return result


def _recognize_with_secrets(
    text: str, secrets: tuple[str, ...], *, offset: int = 0
) -> tuple[RecognitionResult, tuple[SecretMatch, ...], RecognitionStats]:
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise TypeError("offset must be an integer")
    if offset < 0:
        raise ValueError("offset must be non-negative")

    stats = _MutableStats()
    pipeline = _Pipeline(stats, incremental=True, secrets=tuple(secrets))
    pipeline.feed(text, offset)
    prefix_start = pipeline.finish()
    match = pipeline.matches()
    if match is not None:
        start = match[0]
        end = _find_terminator(text, start - offset)
        candidate = DiagnosticCandidate(
            start=start,
            end=None if end is None else end + offset,
        )
        result = RecognitionResult.matched(candidate)
    elif prefix_start is not None:
        result = RecognitionResult.need_more(prefix_start)
    else:
        result = RecognitionResult.no_match()

    spans = _candidate_spans(text, offset, pipeline.all_matches(), stats)
    all_secrets = pipeline.all_secrets()
    literal = tuple(secret for secret in all_secrets if secret.depth == 0)
    decoded = tuple(secret for secret in all_secrets if secret.depth != 0)
    covered = literal + _cover_secret_matches(decoded, spans, stats)
    return result, _dedupe_secret_matches(covered), pipeline.stats()


def recognize_with_secrets(
    text: str, secrets: tuple[str, ...], *, offset: int = 0
) -> tuple[RecognitionResult, tuple[SecretMatch, ...]]:
    """Return the earliest supported candidate and its secret coverage.

    Literal secret matches are reported anywhere in the raw stream.  Decoded
    secret matches are reported only for raw spans inside a recognised
    supported URL, so ordinary text keeps literal-only matching.  Matches are
    de-duplicated by raw span (shallowest depth wins) and ordered by source
    position.
    """
    result, matches, _stats = _recognize_with_secrets(text, secrets, offset=offset)
    return result, matches


def recognize_with_secrets_and_stats(
    text: str, secrets: tuple[str, ...], *, offset: int = 0
) -> tuple[RecognitionResult, tuple[SecretMatch, ...], RecognitionStats]:
    """Like :func:`recognize_with_secrets`, also returning work counters."""
    return _recognize_with_secrets(text, secrets, offset=offset)


@dataclass(frozen=True, slots=True)
class RecognizedMatch:
    """One detected candidate's raw ranges in absolute stream coordinates.

    ``start``/``start_end`` bound the scheme's first unit and
    ``colon``/``colon_end`` bound the ``:`` atom; the candidate itself extends
    to the raw terminator, which the incremental caller locates in its own
    retained buffer. ``separator_end`` is the exclusive raw end of the full
    separator, independent of when a coordinator activates the candidate.
    All coordinates are absolute raw stream offsets.
    """

    start: int
    start_end: int | None
    colon: int
    colon_end: int
    separator_end: int


class IncrementalRecognizer:
    """Monotonic, stateful raw recognizer over one stream of texts.

    The recognizer consumes consecutive raw chunks with :meth:`feed` and, after
    the caller commits a prefix with :meth:`consume`, retires the outcomes that
    are fully covered by the commit while preserving every live automaton,
    percent-decoder and supported-prefix continuation state.  The source
    payload stays with the caller; the recognizer owns only registration
    structures and bounded cursor/decoder state.  A commit never re-feeds
    retained input, and a supported URL prefix that overlaps committed secret
    characters keeps growing.
    """

    __slots__ = ("_stats", "_secrets", "_pipeline", "_committed", "_consumed")

    def __init__(self, secrets: tuple[str, ...] = ()) -> None:
        self._secrets = tuple(secrets)
        self._stats = _MutableStats()
        self._pipeline = _Pipeline(
            self._stats, incremental=True, secrets=self._secrets
        )
        self._committed = 0
        self._consumed = 0

    @property
    def consumed(self) -> int:
        """Absolute raw offset of the next character to be fed."""
        return self._consumed

    @property
    def committed(self) -> int:
        """Absolute raw offset before which recognised state was consumed."""
        return self._committed

    @property
    def stats(self) -> RecognitionStats:
        """Deterministic structural work counters for this recognizer."""
        return self._pipeline.stats()

    @property
    def retained_matches(self) -> int:
        """Recorded-but-unconsumed candidate matches."""
        return self._pipeline.retained_matches()

    def feed(self, text: str) -> None:
        """Consume the next raw text chunk, advancing absolute coordinates."""
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        if not text:
            return
        self._pipeline.feed(text, self._consumed)
        self._consumed += len(text)

    def consume(self, offset: int) -> None:
        """Retire recognised outcomes fully covered by absolute *offset*.

        A regressing offset is ignored and an offset past the fed stream is
        clamped, so a caller can commit repeatedly without re-reading.  Only
        already-recorded candidates and secret matches whose raw span lies
        completely before *offset* are dropped: the Aho-Corasick cursors, the
        percent-decoder pending units and every supported prefix that overlaps
        the commit are preserved, so committed text never resurfaces and an
        overlapping continuation is never reset.
        """
        if isinstance(offset, bool) or not isinstance(offset, int):
            raise TypeError("offset must be an integer")
        if offset <= self._committed:
            return
        if offset > self._consumed:
            offset = self._consumed
        self._committed = offset
        self._pipeline.retire(offset)

    def prefix_state(self) -> int:
        """``_PREFIX_SCHEME`` when a viable supported prefix is pending."""
        return (
            _PREFIX_SCHEME
            if self._pipeline.prefix_state() is not None
            else _PREFIX_NONE
        )

    def earliest_match(self) -> RecognizedMatch | None:
        """Earliest detected candidate's raw ranges, or ``None``."""
        match = self._pipeline.matches()
        if match is None:
            return None
        return RecognizedMatch(*match)

    def retain_from(self) -> int:
        """Absolute raw start of the earliest still-viable supported prefix."""
        start = self._pipeline.prefix_state()
        if start is None:
            return self._committed
        return max(start, self._committed)

    def secret_prefix_start(self, *, decoded: bool = False,
                            minimum_start: int | None = None,
                            include_start: int | None = None) -> int | None:
        """Unsettled secret coverage start, preserving crossing continuations."""
        observers = list(self._pipeline._decoded) if decoded else []
        if self._pipeline._literal is not None:
            observers.append(self._pipeline._literal)
        starts = [start for observer in observers
                  if (start := observer.prefix_start(minimum_start, include_start)) is not None]
        return min(starts) if starts else None

    @property
    def structural_events(self) -> tuple[tuple[str, int, int] | None, ...]:
        """Fixed-depth structural emissions from the last fed source unit.

        This is bounded renderer lookahead, not a decoded payload/history.
        A coordinated caller feeds single source symbols before inspecting it.
        """
        return tuple(self._pipeline._structure)

    def secret_records(self, *, decoded: bool = False):
        """Iterate retained coverage without batch deduplication or sorting.

        Coordinated boundary selection needs only minima and intersections;
        duplicate decoded views are harmless to those operations. Charge each
        inspected coordinate record, rather than hiding work in a batch sort.
        """
        observers = list(self._pipeline._decoded) if decoded else []
        if self._pipeline._literal is not None:
            observers.append(self._pipeline._literal)
        for observer in observers:
            for record in observer.matches():
                self._stats.coverage_checks += 1
                yield record

    def literal_secrets(self) -> tuple[SecretMatch, ...]:
        """Literal (undecoded) secret matches still retained."""
        return _dedupe_secret_matches(self._pipeline.literal_secrets())

    def decoded_secrets(self) -> tuple[SecretMatch, ...]:
        """Decoded secret matches still retained (caller gates by URL)."""
        return _dedupe_secret_matches(self._pipeline.decoded_secrets())

    def result(self, text: str) -> RecognitionResult:
        """Resolve the current outcome against the caller's retained *text*.

        *text* must be the caller's unconsumed buffer, starting at absolute
        offset :attr:`committed`.  The returned ``end`` is absolute, so a
        ``MATCH`` with ``end is None`` is a detected-but-unterminated
        candidate.
        """
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        match = self._pipeline.matches()
        if match is not None:
            start = match[0]
            relative = max(start - self._committed, 0)
            end = _find_terminator(text, relative)
            return RecognitionResult.matched(
                DiagnosticCandidate(
                    start=start,
                    end=None if end is None else end + self._committed,
                )
            )
        start = self._pipeline.prefix_state()
        if start is not None:
            return RecognitionResult.need_more(max(start, self._committed))
        return RecognitionResult.no_match()

    def finalize(self) -> int | None:
        """Flush pending decoding state after the stream has ended."""
        return self._pipeline.finish()


__all__ = [
    "IncrementalRecognizer",
    "RecognitionStats",
    "RecognizedMatch",
    "SecretMatch",
    "recognize",
    "recognize_with_secrets",
    "recognize_with_secrets_and_stats",
    "recognize_with_stats",
    "select_secret_matches",
]
