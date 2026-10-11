"""Dependency-free Aho-Corasick streaming core for literal pattern matching.

Phase 1A (P1A) of ``linearize-diagnostic-url-detection`` introduces this
standalone core as the prerequisite for literal secret matching.  It is a
generic multi-literal matcher: it recognises configured literal patterns in a
stream of text and reports every occurrence, including overlapping and
simultaneous matches.  It performs **no** percent decoding, URL recognition,
secret selection, redaction or projector coordination -- those remain P2/P3
work.  Nothing here is a public SDK, persistence or presentation contract; the
module is internal to :mod:`docker.versioning`.

The core is a classic Aho-Corasick automaton with two deliberately separate
halves:

* :class:`AhoCorasick` is the **immutable compiled registration**.  It copies
  and de-duplicates the configured patterns, builds a trie, and adds one
  breadth-first failure link and one output link per node.  It is built once
  and shared by every stream; search never mutates it.
* :class:`AhoCorasickCursor` is the **per-stream live search state**.  It holds
  only the current node, the absolute input offset and cumulative counters.  It
  never retains the consumed source text, so its state is independent of the
  stream length.

Registration versus live-state ownership
----------------------------------------

Registration storage depends only on the configured pattern set: the automaton
has at most ``1 + sum(len(pattern))`` nodes and stores, per node, a transition
map, a failure link, an own-output pattern index and an output link.  Because a
node stores only its *own* output and a single output link, the complete
inherited output list is never copied onto every node, so registration storage
does not multiply with the number of patterns that are suffixes of one another.
A cursor stores no trie structure, no pattern text and no source payload; it
holds only integer state and references the shared automaton.  Independent
cursors over one automaton are therefore fully isolated.

Compiled state is genuinely immutable, not merely left untouched by search.
Copying the caller's input list and never mutating registration while matching
are *isolation* properties: on their own they would not stop a caller or a
future code path from rewriting a transition or replacing an attribute.
Instead, construction keeps mutable dictionaries local and publishes each node
only as a :class:`types.MappingProxyType` read-only view, and
:class:`AhoCorasick` refuses to reassign or delete any compiled attribute after
initialization.  Attempted transition or attribute mutation therefore raises
instead of silently changing matching for cursors already sharing the trie.

Pattern identity and output ordering
------------------------------------

Patterns are de-duplicated while preserving first-occurrence order, and each
retained pattern has a stable ``pattern_index`` into :attr:`AhoCorasick.patterns`
(empty patterns are ignored).  A cursor reports a :class:`PatternMatch` with
the ``pattern_index``, an inclusive ``start`` and an exclusive ``end``.  At one
input position, matches are reported from the current node outward along the
output-link chain, i.e. **longest first**, and matches from successive input
characters are reported in increasing end order.  Every occurrence is reported
exactly once.

Coordinates and Unicode
-----------------------

``start`` and ``end`` are absolute Python ``str`` character offsets within the
cursor's stream: ``start`` is inclusive and ``end`` is exclusive, so
``text[start:end]`` is exactly the matched literal.  Offsets count Python
characters, never UTF-8 bytes and never decoded-layer characters, so a
multibyte prefix shifts an offset by its character length.  A cursor may be
created with an initial ``offset`` so its matches carry absolute coordinates in
a larger stream; a match start is always ``end - len(pattern)``.

Continuation and reset
-----------------------

:meth:`AhoCorasickCursor.feed` consumes a chunk and :meth:`AhoCorasickCursor.advance`
consumes one symbol.  Feeding a text in any sequence of chunks (including
single characters) produces the same matches, state and counters as one feed:
chunk boundaries need no overlap text and no retained payload.  The offset
advances monotonically with the characters consumed.

:meth:`AhoCorasickCursor.reset` discards any in-flight partial-match state by
returning the cursor to the root, but it preserves both the absolute offset and
the cumulative counters.  A reset models dropping obsolete recognition state at
finalization or overflow recovery; it never replays or re-reads input, so it
cannot hide work and a later character still gets its true stream coordinate.

Work bounds
-----------

Let ``N`` be the number of consumed characters.  Each character performs one
direct transition (``transitions == N``).  Failure links are followed only
while resolving a character and are amortised across the stream, so the total
failure traversals are ``O(N)``.  Each emitted match costs one own-output check
plus one output-link step, so total output work is ``O(matches)``.  Search work
is therefore ``O(N + matches)`` and deterministic; the counters expose the
three components (:class:`SearchCounters`) so tests can assert the bound
without measuring elapsed time.  Live search state is ``O(1)`` in the stream
length: one node index, one offset and three integers.

Scope and usage example
-----------------------

This core matches literal patterns only.  It does **not** coordinate the URL
recognizer, select leftmost-longest secrets, apply percent decoding, replace
or render secrets, or own a projector tail; those remain P2/P3 integration
work.  The example below is executed verbatim by the P1A tests so the
documented API cannot drift from behavior::

    matcher = AhoCorasick(["he", "she", "hers"])
    matches = matcher.find_all("ushers")
    observed = [
        (match.pattern_index, match.start, match.end) for match in matches
    ]
    assert observed == [(1, 1, 4), (0, 2, 4), (2, 2, 6)]
"""
from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

__all__ = [
    "AhoCorasick",
    "AhoCorasickCursor",
    "PatternMatch",
    "SearchCounters",
]

#: Root node index.  Every failure and missing transition falls back here.
_ROOT = 0

#: Compiled attributes that may be assigned only during construction.  Every
#: other name is already rejected by ``__slots__``.
_COMPILED_SLOTS = frozenset(
    {"_patterns", "_goto", "_fail", "_output", "_output_link"}
)


@dataclass(frozen=True, slots=True)
class PatternMatch:
    """One detected literal occurrence in absolute stream coordinates.

    ``pattern_index`` identifies the registered pattern (an index into
    :attr:`AhoCorasick.patterns`), ``start`` is the inclusive and ``end`` the
    exclusive Python-character offset of the occurrence.  The value is frozen
    and slotted.
    """

    pattern_index: int
    start: int
    end: int

    def __post_init__(self) -> None:
        for name in ("pattern_index", "start", "end"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
        if self.pattern_index < 0:
            raise ValueError("pattern_index must be non-negative")
        if self.start < 0:
            raise ValueError("start must be non-negative")
        if self.end <= self.start:
            raise ValueError("end must be exclusive and greater than start")


@dataclass(frozen=True, slots=True)
class SearchCounters:
    """Cumulative deterministic work counters for one cursor.

    ``transitions`` counts consumed input symbols, ``failures`` counts
    failure-link traversals and ``outputs`` counts emitted matches (own-output
    checks plus output-link steps).  Counters are cumulative for the cursor's
    lifetime and survive :meth:`AhoCorasickCursor.reset`.
    """

    transitions: int
    failures: int
    outputs: int


def _dedupe_patterns(patterns: Iterable[str]) -> tuple[str, ...]:
    """Return non-empty *patterns* de-duplicated, preserving first occurrence.

    Patterns must be strings; empty patterns are ignored because a literal
    empty match has no meaningful source span.
    """
    seen: set[str] = set()
    result: list[str] = []
    for pattern in patterns:
        if not isinstance(pattern, str):
            raise TypeError("each pattern must be a string")
        if pattern and pattern not in seen:
            seen.add(pattern)
            result.append(pattern)
    return tuple(result)


class AhoCorasick:
    """Immutable compiled multi-literal automaton shared by stream cursors.

    Construction copies and de-duplicates the configured patterns, builds the
    trie, and completes breadth-first failure and output links.  The finished
    state is genuinely immutable: each node's transition map is published only
    as a read-only :class:`types.MappingProxyType` view of a dictionary kept
    local to construction, and the compiled attributes themselves are frozen
    against reassignment and deletion.  Any number of
    :class:`AhoCorasickCursor` instances can therefore share one automaton
    concurrently and independently.  No new runtime dependency is used.
    """

    __slots__ = ("_patterns", "_goto", "_fail", "_output", "_output_link")

    _patterns: tuple[str, ...]
    _goto: tuple[Mapping[str, int], ...]
    _fail: tuple[int, ...]
    _output: tuple[int | None, ...]
    _output_link: tuple[int | None, ...]

    def __init__(self, patterns: Iterable[str]) -> None:
        if isinstance(patterns, (str, bytes)):
            raise TypeError(
                "patterns must be an iterable of strings, not a single string"
            )
        registered = _dedupe_patterns(patterns)

        goto: list[dict[str, int]] = [{}]
        output: list[int | None] = [None]
        for index, pattern in enumerate(registered):
            state = _ROOT
            for character in pattern:
                nxt = goto[state].get(character)
                if nxt is None:
                    nxt = len(goto)
                    goto[state][character] = nxt
                    goto.append({})
                    output.append(None)
                state = nxt
            output[state] = index

        fail = [_ROOT] * len(goto)
        output_link: list[int | None] = [None] * len(goto)
        queue: deque[int] = deque(goto[_ROOT].values())
        while queue:
            state = queue.popleft()
            for character, nxt in goto[state].items():
                queue.append(nxt)
                fallback = fail[state]
                while fallback != _ROOT and character not in goto[fallback]:
                    fallback = fail[fallback]
                candidate = goto[fallback].get(character)
                if candidate is not None and candidate != nxt:
                    fail[nxt] = candidate
                else:
                    fail[nxt] = _ROOT
                suffix = fail[nxt]
                output_link[nxt] = (
                    suffix if output[suffix] is not None else output_link[suffix]
                )

        # The mutable dictionaries stay local; publishing read-only wrappers
        # makes the compiled transitions immutable in fact, not by convention.
        self._patterns = registered
        self._goto = tuple(MappingProxyType(node) for node in goto)
        self._fail = tuple(fail)
        self._output = tuple(output)
        self._output_link = tuple(output_link)

    def __setattr__(self, name: str, value: object) -> None:
        """Allow each compiled attribute to be assigned only at construction."""
        if name in _COMPILED_SLOTS and hasattr(self, name):
            raise AttributeError(
                f"{type(self).__name__}.{name} is frozen after construction"
            )
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        """Refuse to delete compiled state; it has no valid absent form."""
        if name in _COMPILED_SLOTS:
            raise AttributeError(
                f"{type(self).__name__}.{name} is frozen after construction"
            )
        object.__delattr__(self, name)

    @property
    def patterns(self) -> tuple[str, ...]:
        """Registered patterns in first-occurrence order (empty removed)."""
        return self._patterns

    @property
    def node_count(self) -> int:
        """Number of trie nodes, at most ``1 + sum(len(pattern))``."""
        return len(self._goto)

    def cursor(self, *, offset: int = 0) -> "AhoCorasickCursor":
        """Return a new independent cursor over this shared automaton.

        *offset* is the absolute stream coordinate of the first character the
        cursor will consume, so reported match coordinates are absolute.
        """
        if isinstance(offset, bool) or not isinstance(offset, int):
            raise TypeError("offset must be an integer")
        if offset < 0:
            raise ValueError("offset must be non-negative")
        return AhoCorasickCursor(self, offset)

    def find_all(self, text: str, *, offset: int = 0) -> tuple[PatternMatch, ...]:
        """Return every literal occurrence in *text* as one batch tuple."""
        cursor = self.cursor(offset=offset)
        return cursor.feed(text)


class AhoCorasickCursor:
    """Per-stream live Aho-Corasick search state over one shared automaton.

    The cursor retains only the current node, the absolute input offset and
    cumulative counters -- never the consumed source text, a decoded copy or an
    input-sized history -- so its state is independent of stream length.
    """

    __slots__ = (
        "_automaton",
        "_state",
        "_offset",
        "_transitions",
        "_failures",
        "_outputs",
    )

    def __init__(self, automaton: AhoCorasick, offset: int) -> None:
        self._automaton = automaton
        self._state = _ROOT
        self._offset = offset
        self._transitions = 0
        self._failures = 0
        self._outputs = 0

    @property
    def state(self) -> int:
        """Current automaton node index (``0`` is the root)."""
        return self._state

    @property
    def offset(self) -> int:
        """Absolute stream coordinate of the next character to consume."""
        return self._offset

    @property
    def counters(self) -> SearchCounters:
        """Cumulative deterministic work counters for this cursor."""
        return SearchCounters(
            transitions=self._transitions,
            failures=self._failures,
            outputs=self._outputs,
        )

    def reset(self) -> None:
        """Return to the root, preserving offset and lifetime counters.

        Any in-flight partial match is discarded, but the absolute offset and
        the cumulative counters are kept, so a reset models dropping obsolete
        recognition state without replaying input or hiding work.
        """
        self._state = _ROOT

    def feed(self, text: str) -> tuple[PatternMatch, ...]:
        """Consume a chunk, returning the matches emitted within it."""
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        matches: list[PatternMatch] = []
        for character in text:
            self._step(character, matches)
        return tuple(matches)

    def advance(self, character: str) -> tuple[PatternMatch, ...]:
        """Consume exactly one symbol, returning the matches it emits."""
        if not isinstance(character, str) or len(character) != 1:
            raise TypeError("character must be a single-character string")
        matches: list[PatternMatch] = []
        self._step(character, matches)
        return tuple(matches)

    def _step(self, character: str, matches: list[PatternMatch]) -> None:
        goto = self._automaton._goto
        fail = self._automaton._fail
        state = self._state
        while state != _ROOT and character not in goto[state]:
            state = fail[state]
            self._failures += 1
        nxt = goto[state].get(character)
        self._state = _ROOT if nxt is None else nxt
        self._transitions += 1
        self._offset += 1
        self._emit(self._state, matches)

    def _emit(self, state: int, matches: list[PatternMatch]) -> None:
        output = self._automaton._output
        output_link = self._automaton._output_link
        patterns = self._automaton._patterns
        pattern_index = output[state]
        if pattern_index is not None:
            end = self._offset
            matches.append(
                PatternMatch(
                    pattern_index=pattern_index,
                    start=end - len(patterns[pattern_index]),
                    end=end,
                )
            )
            self._outputs += 1
        link = output_link[state]
        while link is not None:
            pattern_index = output[link]
            # An output link always targets a node with its own output, so the
            # pattern index is never ``None`` here; the assertion narrows the
            # optional stored type without changing runtime behavior.
            assert pattern_index is not None
            end = self._offset
            matches.append(
                PatternMatch(
                    pattern_index=pattern_index,
                    start=end - len(patterns[pattern_index]),
                    end=end,
                )
            )
            self._outputs += 1
            link = output_link[link]
