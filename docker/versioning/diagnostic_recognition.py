"""Internal typed recognition contract for diagnostic URLs and source secrets.

Phase 1 (P1) of ``linearize-diagnostic-url-detection`` defines the immutable
result model shared by the diagnostic URL recognizer and its callers.  It
contains no recognizer algorithm (that lands in P2) and deliberately
introduces **no** public SDK, persistence, or output contract: the types below
are internal to :mod:`docker.versioning`.

Supported-only detection
------------------------

Detection recognises **only** the fixed, case-insensitive scheme set in
:data:`SUPPORTED_SCHEMES` followed by a supported separator.  A scheme that is
not on that set -- ``npm``, ``custom+transport``, ``gopher`` or any other
arbitrary RFC-valid scheme -- is **not** a detected URL candidate.  Unknown
scheme text remains ordinary text: it is eligible for ordinary literal secret
handling, terminal safety and bounds, but it contributes no hostname,
host/path or fingerprint and does not become a URL fact.  Because detection
selects the earliest *supported* start, a supported scheme embedded in longer
text (for example ``TOKENhttps://host``) is still a candidate at the supported
scheme position.

``:data:`CandidateKind`` and :func:`classify_scheme` are retained only as
**compatibility helpers** for pre-revision callers.  They classify an
already-decoded scheme but must never be used to turn an unsupported scheme
into a detected URL or to authorise metadata.  New code should use
:func:`is_supported_scheme` and the supported-only outcomes below.

Recognition outcomes
--------------------

Recognition produces a :class:`RecognitionResult`, which is one of three
closed outcomes:

``NO_MATCH``
    No complete separator-based supported candidate was detected in the input.
    Any remaining ambiguous suffix is a separate concern and is reported by
    ``NEED_MORE``, never folded into ``NO_MATCH``.  Unknown-scheme text is
    ``NO_MATCH``; it is not a hidden URL.

``MATCH(candidate)``
    A separator-based **supported** candidate was detected.  Its
    :class:`DiagnosticCandidate` carries the raw ``start`` and the exclusive
    raw ``end``.  ``end`` is ``None`` when the candidate has been detected but
    its raw terminator has not arrived yet (**detected-but-unterminated**).
    ``MATCH`` means only that a candidate exists; it is *not* proof that the
    candidate is a valid URL.

``NEED_MORE(retain_from)``
    The text ends inside an ambiguous **supported** prefix that may still grow
    into a candidate -- an incomplete separator, a partial percent escape, or a
    scheme/layer prefix whose next character could complete ``://``.
    ``retain_from`` is the absolute stream raw character offset from which the
    caller must retain the unresolved suffix.  ``NEED_MORE`` is not a candidate
    and does not itself prove a URL.

The two "incomplete" concepts MUST NOT be conflated:

* an **incomplete separator** is ``NEED_MORE`` and has no candidate;
* an **incomplete URL** is ``MATCH`` with ``candidate.end is None`` and has a
  candidate whose start is already known.

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

Source secret coverage
----------------------

:class:`SecretCoverage` is the immutable model for a raw source range that a
registered literal secret covers.  It uses the same absolute raw character
coordinate space as candidates, with an inclusive ``start`` and exclusive
``end``, and is produced by the streaming secret matcher delivered in P1A.
The coordinator combines URL candidates and secret coverage over source
coordinates; coverage is deliberately **not** carried on
:class:`DiagnosticCandidate`, because URL and secret recognition are
independent and overlapping.  Neither a candidate nor a coverage span is
proof of URL validity or of a safe replacement boundary.

Candidate validity
------------------

Detection is deliberately separate from *interpretation*.  Detection finds a
candidate's raw extent; it never parses an authority or path and never proves
that a candidate is a valid URL.  A supported candidate remains subject to the
existing safe-URL validation before any metadata is derived, and rendering
markers are not validated hostname or fingerprint facts.  Classification says
nothing about URL validity.  This module performs no detection, decoding or
validation and nothing here is serialized.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

#: The fixed, case-insensitive set of supported diagnostic URL schemes.  A
#: scheme is supported exactly when its lowercase form is a member.  ``npm`` is
#: intentionally absent because ``npm:`` is not a separator-based candidate.
SUPPORTED_SCHEMES: frozenset[str] = frozenset(
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

#: Compatibility alias for the pre-revision name.  New code should use
#: :data:`SUPPORTED_SCHEMES`; the membership is identical.
KNOWN_SCHEMES: frozenset[str] = SUPPORTED_SCHEMES


def is_supported_scheme(scheme: str) -> bool:
    """Return whether *scheme* is on the fixed supported set.

    The check is case-insensitive.  A non-string argument raises
    :class:`TypeError`.  This is a policy query over an already-decoded scheme
    string; percent-encoded scheme recognition belongs to the recognizer.
    """
    if not isinstance(scheme, str):
        raise TypeError("scheme must be a string")
    return scheme.lower() in SUPPORTED_SCHEMES


class CandidateKind(Enum):
    """Compatibility classification of an already-decoded scheme.

    ``KNOWN`` means the scheme is on :data:`SUPPORTED_SCHEMES`; ``UNKNOWN``
    means it is not.  This enum is retained only for pre-revision callers and
    must not be used to make an unsupported scheme a detected URL or to
    authorise metadata.  Supported-only detection treats an ``UNKNOWN``
    scheme as ordinary text (a ``NO_MATCH`` outcome), never as a candidate.
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
    """Immutable raw-coordinate supported URL candidate.

    Attributes:
        start: Inclusive **absolute stream** raw character offset of the
            candidate start (see the module ``Coordinates`` section).
        end: Exclusive **absolute stream** raw character offset of the
            candidate's raw terminator, or ``None`` while the candidate has
            been detected but is not yet terminated.  ``end=None`` does not
            mean the candidate is a valid complete URL.
        kind: Compatibility classification, defaulting to
            :attr:`CandidateKind.KNOWN`.  Supported-only detection emits only
            supported candidates; the field exists so pre-revision callers
            keep working and is not an authorisation to detect or publish an
            unsupported scheme.

    Offsets count Python characters, never UTF-8 bytes or decoded-layer
    characters, and are absolute within one recognizer stream rather than
    relative to any individual feed.  The value is frozen and slotted: a caller
    cannot mutate an emitted candidate, and illegal offsets are rejected at
    construction.  A candidate is never proof of URL validity.
    """

    start: int
    end: int | None
    kind: CandidateKind = CandidateKind.KNOWN

    def __post_init__(self) -> None:
        if isinstance(self.start, bool) or not isinstance(self.start, int):
            raise TypeError("start must be an integer raw character index")
        if self.start < 0:
            raise ValueError("start must be a non-negative raw character index")
        if self.end is not None:
            if isinstance(self.end, bool) or not isinstance(self.end, int):
                raise TypeError("end must be an integer raw character index or None")
            if self.end <= self.start:
                raise ValueError(
                    "end must be exclusive and strictly greater than start"
                )
        if not isinstance(self.kind, CandidateKind):
            raise TypeError("kind must be a CandidateKind")


@dataclass(frozen=True, slots=True)
class SecretCoverage:
    """Immutable raw source range covered by a registered literal secret.

    ``start`` is an inclusive and ``end`` an exclusive **absolute stream** raw
    character offset in the same coordinate space as
    :class:`DiagnosticCandidate`.  Coverage is reported by the streaming secret
    matcher (P1A); the pattern identity that produced a span is reported
    separately by that matcher and is not needed to express coverage.  A
    coverage span says only that source characters are covered by a secret: it
    does not prove that surrounding text forms a valid URL and it does not by
    itself authorise or forbid a metadata fact.
    """

    start: int
    end: int

    def __post_init__(self) -> None:
        if isinstance(self.start, bool) or not isinstance(self.start, int):
            raise TypeError("start must be an integer raw character index")
        if self.start < 0:
            raise ValueError("start must be a non-negative raw character index")
        if isinstance(self.end, bool) or not isinstance(self.end, int):
            raise TypeError("end must be an integer raw character index")
        if self.end <= self.start:
            raise ValueError("end must be exclusive and strictly greater than start")


@dataclass(frozen=True, slots=True)
class RecognitionResult:
    """Immutable closed recognition outcome over raw input text.

    Exactly one shape is valid, selected by :attr:`kind`, and
    :meth:`no_match`, :meth:`matched`, and :meth:`need_more` are the supported
    constructors:

    * ``NO_MATCH``: no candidate and no retention point.  Unknown-scheme text
      is ``NO_MATCH`` rather than a hidden candidate.
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
            raise ValueError("NEED_MORE requires a non-negative raw retain_from index")

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
    """Compatibility classification of an already-decoded *scheme*.

    A scheme is ``KNOWN`` exactly when its lowercase form is a member of
    :data:`SUPPORTED_SCHEMES`; every other value -- including the empty string
    and non-supported near-matches such as ``httpx``, ``npm`` or
    ``custom+transport`` -- is ``UNKNOWN``.  This helper is retained for
    pre-revision callers; supported-only detection must not turn an
    ``UNKNOWN`` result into a candidate or metadata.  New code should prefer
    :func:`is_supported_scheme`.
    """
    return CandidateKind.KNOWN if is_supported_scheme(scheme) else CandidateKind.UNKNOWN


__all__ = [
    "SUPPORTED_SCHEMES",
    "KNOWN_SCHEMES",
    "CandidateKind",
    "DiagnosticCandidate",
    "RecognitionKind",
    "RecognitionResult",
    "SecretCoverage",
    "classify_scheme",
    "is_supported_scheme",
]
