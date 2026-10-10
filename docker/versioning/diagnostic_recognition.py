"""Internal typed recognition contract for diagnostic URLs.

Phase 1 (P1) of ``linearize-diagnostic-url-detection`` defines the immutable
result model shared by the diagnostic URL recognizer and its callers.  It
contains no recognizer algorithm (that lands in P2) and deliberately
introduces **no** public SDK, persistence, or output contract: the types below
are internal to :mod:`docker.versioning`.

Recognition produces a :class:`RecognitionResult`, which is one of three
closed outcomes:

``NO_MATCH``
    No complete separator-based candidate was detected in the input.  Any
    remaining ambiguous suffix is a separate concern and is reported by
    ``NEED_MORE``, never folded into ``NO_MATCH``.

``MATCH(candidate)``
    A separator-based candidate was detected.  Its :class:`DiagnosticCandidate`
    carries the raw ``start`` and the exclusive raw ``end``.  ``end`` is
    ``None`` when the candidate has been detected but its raw terminator has
    not arrived yet (**detected-but-unterminated**).  ``MATCH`` means only that
    a candidate exists; it is *not* proof that the candidate is a valid URL.

``NEED_MORE(retain_from)``
    The text ends inside an ambiguous prefix that may still grow into a
    candidate -- an **incomplete separator**, a partial percent escape, or a
    scheme/layer prefix whose next character could complete ``://``.
    ``retain_from`` is the absolute stream raw character offset from which the
    caller must retain the unresolved suffix.  ``NEED_MORE`` is not a candidate
    and does not itself prove a URL.

The two "incomplete" concepts MUST NOT be conflated:

* an **incomplete separator** is ``NEED_MORE`` and has no candidate;
* an **incomplete URL** is ``MATCH`` with ``candidate.end is None`` and has a
  candidate whose start and classification are already known.

Coordinates
-----------

All offsets are **absolute raw character offsets within one recognizer
stream**: the monotonically growing sequence of Python ``str`` characters the
recognizer consumes across successive feeds.  They are *not* indices into each
individually supplied chunk, *not* UTF-8 byte offsets, and *not* coordinates
in any percent-decoded layer.  ``start`` is inclusive; ``end`` is exclusive,
following the usual Python slice convention, so ``stream[start:end]`` is
exactly the candidate text.

An incremental caller that retains only a bounded suffix must convert a
stream offset to an index into its retained buffer at the retention boundary::

    buffer_index = stream_offset - retained_buffer_start

where ``retained_buffer_start`` is the stream offset of the first character
in the retained buffer.  The conversion is character-based, so it is exact for
multibyte text.  This module defines the coordinate space but performs no such
conversion.

Candidate validity and classification
-------------------------------------

:class:`CandidateKind` is a closed classification of a **detected** candidate:
``KNOWN`` when the candidate's scheme is a member of the fixed, internal,
case-insensitive :data:`KNOWN_SCHEMES` whitelist, and ``UNKNOWN`` otherwise.
Classification says nothing about URL validity.  ``KNOWN`` candidates remain
subject to the existing safe-URL validation before any metadata is derived;
whitelist membership never makes a URL safe.  ``UNKNOWN`` candidates are
conservatively hidden in safe channels and contribute no hostname, host/path,
or fingerprint metadata.  :func:`classify_scheme` classifies an
already-decoded scheme string; percent-encoded scheme recognition is a
recognizer concern (P2), not a classification concern.

A classifier may retain bounded scheme/layer information to avoid
reclassification, but that is not an external data contract.  Nothing in this
module is serialized.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

#: The fixed scheme whitelist.  Membership is evaluated case-insensitively.
#: ``npm`` is intentionally absent because ``npm:`` is not a
#: separator-based (``://``) candidate form.
KNOWN_SCHEMES: frozenset[str] = frozenset(
    {
        "http",
        "https",
        "ftp",
        "ftps",
        "ssh",
        "git",
        "git+ssh",
        "git+http",
        "git+https",
        "file",
        "ws",
        "wss",
        "socks",
        "socks4",
        "socks4a",
        "socks5",
        "socks5h",
    }
)


class CandidateKind(Enum):
    """Closed classification of a detected separator-based candidate.

    ``KNOWN`` means the detected scheme is on :data:`KNOWN_SCHEMES`;
    ``UNKNOWN`` means it is not.  This is a classification only: neither value
    proves URL validity, and ``UNKNOWN`` candidates are hidden without
    metadata.
    """

    KNOWN = "known"
    UNKNOWN = "unknown"


class RecognitionKind(Enum):
    """Closed discriminator for the three :class:`RecognitionResult` outcomes."""

    NO_MATCH = "no_match"
    MATCH = "match"
    NEED_MORE = "need_more"


@dataclass(frozen=True, slots=True)
class DiagnosticCandidate:
    """Immutable raw-coordinate URL candidate.

    Attributes:
        start: Inclusive **absolute stream** raw character offset of the
            candidate start (see the module ``Coordinates`` section).
        end: Exclusive **absolute stream** raw character offset of the
            candidate's raw terminator, or ``None`` while the candidate has
            been detected but is not yet terminated.  ``end=None`` does not
            mean the candidate is a valid complete URL.

    Offsets count Python characters, never UTF-8 bytes or decoded-layer
    characters, and are absolute within one recognizer stream rather than
    relative to any individual feed.
        kind: Closed :class:`CandidateKind` classification.  The candidate is
            valid data for any combination of ``start``/``end``; it is never
            proof of URL validity.

    The value is frozen and slotted: a caller cannot mutate an emitted
    candidate, and illegal offsets are rejected at construction.
    """

    start: int
    end: int | None
    kind: CandidateKind

    def __post_init__(self) -> None:
        if isinstance(self.start, bool) or not isinstance(self.start, int):
            raise TypeError("start must be an integer raw character index")
        if self.start < 0:
            raise ValueError("start must be a non-negative raw character index")
        if self.end is not None:
            if isinstance(self.end, bool) or not isinstance(self.end, int):
                raise TypeError(
                    "end must be an integer raw character index or None"
                )
            if self.end <= self.start:
                raise ValueError(
                    "end must be exclusive and strictly greater than start"
                )
        if not isinstance(self.kind, CandidateKind):
            raise TypeError("kind must be a CandidateKind")


@dataclass(frozen=True, slots=True)
class RecognitionResult:
    """Immutable closed recognition outcome over raw input text.

    Exactly one shape is valid, selected by :attr:`kind`, and
    :meth:`no_match`, :meth:`matched`, and :meth:`need_more` are the supported
    constructors:

    * ``NO_MATCH``: no candidate and no retention point.
    * ``MATCH``: a :class:`DiagnosticCandidate` and no retention point.  The
      candidate's ``end`` may be ``None`` (detected but unterminated).
    * ``NEED_MORE``: a non-negative ``retain_from`` **absolute stream** raw
      character offset and no candidate.

    The owning caller decides what to do with an outcome: it processes a
    ``MATCH`` candidate once in source order, retains from the absolute stream
    offset ``retain_from`` on ``NEED_MORE`` (converting it to a retained-buffer
    index as documented in module ``Coordinates``), and commits independently
    safe text on ``NO_MATCH``.
    """

    kind: RecognitionKind
    candidate: DiagnosticCandidate | None = None
    retain_from: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, RecognitionKind):
            raise TypeError("kind must be a RecognitionKind")
        if self.kind is RecognitionKind.NO_MATCH:
            if self.candidate is not None or self.retain_from is not None:
                raise ValueError(
                    "NO_MATCH carries neither a candidate nor a retention point"
                )
        elif self.kind is RecognitionKind.MATCH:
            if not isinstance(self.candidate, DiagnosticCandidate):
                raise ValueError("MATCH requires a DiagnosticCandidate")
            if self.retain_from is not None:
                raise ValueError("MATCH carries no retention point")
        elif self.candidate is not None:
            raise ValueError("NEED_MORE carries no candidate")
        elif (
            isinstance(self.retain_from, bool)
            or not isinstance(self.retain_from, int)
            or self.retain_from < 0
        ):
            raise ValueError(
                "NEED_MORE requires a non-negative raw retain_from index"
            )

    @classmethod
    def no_match(cls) -> "RecognitionResult":
        """Return the ``NO_MATCH`` outcome."""
        return cls(RecognitionKind.NO_MATCH)

    @classmethod
    def matched(cls, candidate: DiagnosticCandidate) -> "RecognitionResult":
        """Return the ``MATCH(candidate)`` outcome."""
        if not isinstance(candidate, DiagnosticCandidate):
            raise TypeError("candidate must be a DiagnosticCandidate")
        return cls(RecognitionKind.MATCH, candidate=candidate)

    @classmethod
    def need_more(cls, retain_from: int) -> "RecognitionResult":
        """Return the ``NEED_MORE(retain_from)`` outcome."""
        return cls(RecognitionKind.NEED_MORE, retain_from=retain_from)


def classify_scheme(scheme: str) -> CandidateKind:
    """Classify an already-decoded *scheme* case-insensitively.

    A scheme is ``KNOWN`` exactly when its lowercase form is a member of
    :data:`KNOWN_SCHEMES`; every other value -- including the empty string and
    non-whitelist near-matches such as ``httpx`` or ``custom+transport`` -- is
    ``UNKNOWN``.  The scheme is expected to be decoded; percent-encoded
    scheme recognition belongs to the recognizer.
    """
    if not isinstance(scheme, str):
        raise TypeError("scheme must be a string")
    if scheme.lower() in KNOWN_SCHEMES:
        return CandidateKind.KNOWN
    return CandidateKind.UNKNOWN


__all__ = [
    "KNOWN_SCHEMES",
    "CandidateKind",
    "DiagnosticCandidate",
    "RecognitionKind",
    "RecognitionResult",
    "classify_scheme",
]
