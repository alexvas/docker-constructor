"""Conservative npm successful-fetch parsing and policy-aware identity.

Phase 4 of ``add-configurable-network-url-display`` introduces one pure,
presentation-independent parser for the complete npm 11.16.0 successful-fetch
source-line grammar::

    npm http fetch <METHOD> <STATUS> <URL> <ASCII digits>ms
        [attempt #<ASCII digits>] [(cache <OUTCOME>)]

The ``npm http fetch`` prefix is part of the grammar and is consumed
explicitly; no upstream prefix stripping is assumed.  The dedicated
``[0-9]+ms`` latency field is matched independently of the generic
numeric-token boundary rules used for numeric-variant grouping, and any other
latency unit is rejected as an unknown shape.  A bare ``<METHOD> ...`` line,
an unrelated or malformed prefix, a non-2xx status, a retry/failure form, and
the distinct ``npm http cache ...`` form are not recognized and therefore
remain ordinary diagnostics.

A line is recognized only when its URL field is a *complete* URL: it must
carry a scheme, a non-empty authority, and a port that is absent or an
in-range integer, and its hostname must normalize under the shared projector
rule (see
:func:`docker.versioning.diagnostic_projection.normalized_url_host`).
Malformed near-matches such as ``https://%``, ``https://[``, ``https://?x``,
``https:///x``, and ``http://:80/x`` are rejected as ordinary diagnostics
rather than becoming fetch-group members.  Hostname acceptance is *not*
reimplemented here; it delegates to the projector so the parser and host/path
projection cannot diverge.

Numeric fields are bounded deliberately.  A digit run whose significant
digits exceed :data:`_MAX_NUMERIC_DIGITS` is rejected conservatively instead
of being converted, so a pathological field can neither raise Python's
decimal integer-conversion error nor produce a meaningless record.

For a recognized record the module derives two policy-specific group
identities and canonical renderings:

* ``redacted`` keys on method, exact status, optional attempt number or its
  absence, and cache outcome; the URL and latency are excluded from both the
  key and the visible text, which renders the URL as ``<redacted>``.
* ``host-path`` additionally keys on the normalized hostname and canonical
  safe path derived by the shared projector, so latency variants for one
  visible resource aggregate while different resources stay separate; the
  safe path is terminal-safe and fails closed to ``/<redacted-path>``.

Both canonical forms omit latency and preserve ``attempt #N`` when present.
``exact`` deliberately bypasses fetch grouping entirely (both helpers return
``None``) so source lines remain unaggregated.

The streaming collector does not retain a complete source line or raw URL.  It
feeds terminal-safe source fragments to :class:`NpmFetchRecognizer`, which
validates the grammar incrementally and retains only bounded parser state
(method, status, attempt, cache, an unsafe-canonicalization flag, and a
user-information-free host candidate while the authority is being read).  User
information is discarded at ``@`` and the host candidate is normalized and
discarded at the authority delimiter, so no credential-bearing substring is
kept.  The parser-minimal :class:`SafeFetchRecord` carries no URL; the
sanitized host/path needed by ``host-path`` grouping is supplied by the shared
projector that already computed it.  :func:`parse_npm_fetch_line` and
:class:`NpmFetchRecord` remain for isolated callers and tests.

Nothing here renders, schedules a timer, reads a mailbox, mutates state, or
touches transport, Docker, or the filesystem.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Sequence
from urllib.parse import urlsplit

from docker.npm_environment.streaming import REDACTED
from docker.versioning.diagnostic_projection import (
    SafeHostPath,
    literal_authority_host,
    normalized_authority_host,
    normalized_url_host,
    sanitize_host_paths,
)
from docker.versioning.fetch_identity import FetchGroupKey
from docker.versioning.model import NetworkUrlDisplay

#: Literal source prefix that every recognized successful fetch must carry.
NPM_FETCH_PREFIX = "npm http fetch"

#: Complete successful-fetch grammar after the consumed prefix.
_LINE_RE = re.compile(
    r"(?P<method>[A-Z]{1,16}) "
    r"(?P<status>[0-9]{3}) "
    r"(?P<url>[A-Za-z][A-Za-z0-9+.\-]*://\S+) "
    r"(?P<latency>[0-9]+)ms"
    r"(?: attempt #(?P<attempt>[0-9]+))?"
    r"(?: \(cache (?P<cache>[a-z]+)\))?"
)

_METHOD_RE = re.compile(r"[A-Z]{1,16}")
_STATUS_RANGE = range(200, 300)

#: URL scheme grammar (RFC 3986 ``scheme``).
_SCHEME_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*")

#: Maximum number of *significant* digits accepted in a latency or attempt
#: field.  This is far beyond any meaningful millisecond latency or retry
#: count while staying safely inside Python's decimal conversion limit, so an
#: oversized field is rejected rather than converted.
_MAX_NUMERIC_DIGITS = 18

#: Maximum retained method length (the grammar caps it at 16).
_MAX_METHOD_CHARS = 16

#: Method characters (uppercase ASCII).
_METHOD_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ")

#: Scheme characters after the mandatory leading ASCII letter.
_SCHEME_REST_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+.-"
)

#: Lowercase ASCII letters permitted in a cache outcome.
_ASCII_LOWER_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz")

#: Characters that :func:`urllib.parse.urlsplit` treats as structural when
#: they appear in a netloc under Unicode NFKC normalization.
_NETLOC_DELIMITERS = frozenset("/?#@:")

#: Hexadecimal digits accepted in a percent escape.
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")

#: Bytes that a decoded percent escape may produce and that change how the
#: complete-line parser splits or validates the authority.  A decoded byte
#: above ``0x7F`` is structural because it can trigger the netloc NFKC check.
_PERCENT_STRUCTURAL_CHARS = frozenset("/?#[]%\t\r\n")


def _nfkc_introduces_delimiter(character: str) -> bool:
    """Whether NFKC normalization of *character* introduces a netloc
    structural delimiter, which is what makes ``urlsplit`` reject a
    non-ASCII netloc."""
    normalized = unicodedata.normalize("NFKC", character)
    return any(delimiter in normalized for delimiter in _NETLOC_DELIMITERS)


def _percent_escape_is_structural(escape: str) -> bool:
    """Whether the two hex digits of *escape* decode to a structural byte."""
    value = int(escape, 16)
    if value >= 0x80:
        return True
    return chr(value) in _PERCENT_STRUCTURAL_CHARS


@dataclass(frozen=True, slots=True)
class NpmFetchRecord:
    """One conservatively recognized successful npm HTTP fetch.

    ``url`` is the complete source URL as emitted by npm; it is consumed only
    to derive a policy-specific identity or canonical text and never enters an
    external SDK event, evidence, persistence, or semantic identity.
    ``latency_ms`` is an integer millisecond value decoupled from the generic
    numeric-token rules.  ``attempt`` and ``cache_outcome`` are optional and
    preserve presence versus absence.
    """

    method: str
    status: int
    url: str
    latency_ms: int
    attempt: int | None
    cache_outcome: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.method, str) or _METHOD_RE.fullmatch(self.method) is None:
            raise ValueError("method must be an uppercase ASCII HTTP method token")
        if (
            not isinstance(self.status, int)
            or isinstance(self.status, bool)
            or self.status not in _STATUS_RANGE
        ):
            raise ValueError("status must be an integer successful 2xx status")
        if not isinstance(self.url, str) or not self.url:
            raise ValueError("url must be a non-empty string")
        if (
            not isinstance(self.latency_ms, int)
            or isinstance(self.latency_ms, bool)
            or self.latency_ms < 0
        ):
            raise ValueError("latency_ms must be a non-negative integer")
        if self.attempt is not None and (
            not isinstance(self.attempt, int)
            or isinstance(self.attempt, bool)
            or self.attempt < 0
        ):
            raise ValueError("attempt must be a non-negative integer or None")
        if self.cache_outcome is not None and not isinstance(self.cache_outcome, str):
            raise ValueError("cache_outcome must be a string or None")


@dataclass(frozen=True, slots=True)
class SafeFetchRecord:
    """Parser-minimal recognized fetch with no retained source URL.

    The streaming recognizer produces this record so that a complete source
    line and its raw URL never have to be retained.  It carries only what
    grouping and canonical rendering need: the method, status, optional
    attempt, optional cache outcome, and -- for ``host-path`` only -- the
    already-sanitized :class:`SafeHostPath` supplied by the projector that
    extracted it.  Credentials, query, fragment, scheme, and the raw path never
    appear here.
    """

    method: str
    status: int
    attempt: int | None
    cache_outcome: str | None
    host_path: SafeHostPath | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.method, str) or _METHOD_RE.fullmatch(self.method) is None:
            raise ValueError("method must be an uppercase ASCII HTTP method token")
        if (
            not isinstance(self.status, int)
            or isinstance(self.status, bool)
            or self.status not in _STATUS_RANGE
        ):
            raise ValueError("status must be an integer successful 2xx status")
        if self.attempt is not None and (
            not isinstance(self.attempt, int)
            or isinstance(self.attempt, bool)
            or self.attempt < 0
        ):
            raise ValueError("attempt must be a non-negative integer or None")
        if self.cache_outcome is not None and not isinstance(self.cache_outcome, str):
            raise ValueError("cache_outcome must be a string or None")
        if self.host_path is not None and not isinstance(self.host_path, SafeHostPath):
            raise ValueError("host_path must be a SafeHostPath or None")


def _bounded_numeric(text: str) -> int | None:
    """Convert a digit run, rejecting oversized significant values.

    Leading zeros are insignificant and ignored, so an all-zero run (however
    long) and a zero-padded value convert normally.  A run whose significant
    digits exceed :data:`_MAX_NUMERIC_DIGITS` returns ``None`` without ever
    calling :func:`int` on the oversized text, so Python's decimal conversion
    limit cannot raise out of the parser.
    """
    significant = text.lstrip("0")
    if len(significant) > _MAX_NUMERIC_DIGITS:
        return None
    return int(significant) if significant else 0


def _is_complete_url(url: str) -> bool:
    """Return whether *url* is a complete, structurally valid URL.

    Only structural checks stay local: a valid scheme, a non-empty authority,
    and an absent or in-range port.  A literal C0 control (U+0000--U+001F) or
    DEL (U+007F) is rejected outright, before any parsing, because projection
    would otherwise discard the control-bearing suffix and silently group the
    diagnostic with a shorter path; the encoded forms (``%00``, ``%1B``,
    ``%7F``) stay valid.  The field is never stripped or neutralized here, so
    a control-bearing line remains an ordinary diagnostic.  Hostname acceptance
    delegates to the shared projector helper
    :func:`docker.versioning.diagnostic_projection.normalized_url_host`, so the
    parser and host/path projection cannot disagree about which hosts are
    valid -- encoded hosts, raw IDN hosts, underscore-bearing reg-names, and
    IPv4/IPv6 literals are all accepted exactly when the projector normalizes
    them.  Malformed URL-shaped fields (``https://%``, ``https://[``,
    ``https://?x``, ``https:///x``, ``http://:80/x``, and the like) return
    ``False``.  The standard library's splitter raises :class:`ValueError` for
    an invalid IPv6 literal or an out-of-range port; that is contained here
    rather than escaping.
    """
    if not isinstance(url, str) or not url:
        return False
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in url):
        return False
    try:
        parts = urlsplit(url)
        parts.port
    except ValueError:
        return False
    if _SCHEME_RE.fullmatch(parts.scheme) is None:
        return False
    if not parts.netloc:
        return False
    return normalized_url_host(url) is not None


def parse_npm_fetch_line(text: str) -> NpmFetchRecord | None:
    """Parse one complete source line, or return ``None`` for a near-match.

    The match is anchored at both ends: a bare ``<METHOD> ...`` line, a
    malformed or unrelated prefix, a non-2xx status, a retry/failure form, an
    unknown latency unit, a missing or incomplete URL, extra or reordered
    clauses, and the distinct ``npm http cache ...`` form all return ``None``
    rather than a partially parsed record.  At most one trailing newline is
    tolerated so a framed line can be parsed directly.  Oversized latency or
    attempt fields are rejected conservatively without being converted.
    """
    if not isinstance(text, str):
        raise TypeError("npm fetch line must be a string")
    if text.endswith("\n"):
        text = text[:-1]
    prefix = NPM_FETCH_PREFIX + " "
    if not text.startswith(prefix):
        return None
    match = _LINE_RE.fullmatch(text[len(prefix):])
    if match is None:
        return None
    status = int(match.group("status"))
    if status not in _STATUS_RANGE:
        return None
    url = match.group("url")
    if not _is_complete_url(url):
        return None
    latency_ms = _bounded_numeric(match.group("latency"))
    if latency_ms is None:
        return None
    attempt_text = match.group("attempt")
    attempt = None
    if attempt_text is not None:
        attempt = _bounded_numeric(attempt_text)
        if attempt is None:
            return None
    return NpmFetchRecord(
        method=match.group("method"),
        status=status,
        url=url,
        latency_ms=latency_ms,
        attempt=attempt,
        cache_outcome=match.group("cache"),
    )


class _NumericField:
    """Bounded significant-digit accumulator mirroring :func:`_bounded_numeric`.

    Leading zeros are insignificant, so an arbitrarily long zero run (or a
    zero-padded value) stays within the bound and converts normally, while a
    run whose significant digits exceed :data:`_MAX_NUMERIC_DIGITS` is marked
    invalid without ever accumulating or converting the oversized text.
    """

    def __init__(self) -> None:
        self._significant: list[str] = []
        self._seen = False
        self._overflow = False

    def feed(self, digit: str) -> bool:
        """Consume one ASCII digit; return ``False`` when the field overflows."""
        self._seen = True
        if not self._significant and digit == "0":
            return True
        if len(self._significant) >= _MAX_NUMERIC_DIGITS:
            self._overflow = True
            return False
        self._significant.append(digit)
        return True

    @property
    def valid(self) -> bool:
        return self._seen and not self._overflow

    def value(self) -> int:
        return int("".join(self._significant)) if self._significant else 0


class _SecretFieldMatcher:
    """Bounded incremental matcher for configured secrets in source fields.

    It is fed the canonical source-field text (the fixed prefix and the
    parsed method, status, attempt, and cache clauses) as each field completes.
    Only the trailing ``longest_secret - 1`` characters are retained, so a
    secret spanning two adjacent fields and a secret split across input chunks
    are both detected while the retained window stays bounded.
    """

    def __init__(self, secrets: Sequence[str]) -> None:
        self._secrets = tuple(secret for secret in secrets if secret)
        self._longest = max((len(secret) for secret in self._secrets), default=0)
        self._tail = ""
        self._hit = False

    @property
    def hit(self) -> bool:
        return self._hit

    def reset(self) -> None:
        self._tail = ""
        self._hit = False

    def feed(self, text: str) -> None:
        if self._hit or not self._secrets or not text:
            return
        window = self._tail + text
        for secret in self._secrets:
            if secret in window:
                self._hit = True
                self._tail = ""
                return
        if self._longest > 1:
            self._tail = window[-(self._longest - 1) :]
        else:
            self._tail = ""


# -- incremental recognizer states ----------------------------------
_ST_PREFIX = 0
_ST_METHOD = 1
_ST_STATUS = 2
_ST_URL_SCHEME = 3
_ST_URL_SLASH1 = 4
_ST_URL_SLASH2 = 5
_ST_URL_AUTHORITY = 6
_ST_URL_BODY = 7
_ST_LATENCY = 8
_ST_LATENCY_MS = 9
_ST_AFTER_LATENCY = 10
_ST_TAIL_SPACE = 11
_ST_ATTEMPT_LITERAL = 12
_ST_ATTEMPT_DIGITS = 13
_ST_CACHE_LITERAL = 14
_ST_CACHE_CHARS = 15
_ST_DONE = 16

_PREFIX_SPACED = NPM_FETCH_PREFIX + " "
_ATTEMPT_LITERAL = "ttempt #"
_CACHE_LITERAL = "cache "


class NpmFetchRecognizer:
    """Incremental recognizer for the successful-fetch source grammar.

    It consumes terminal-safe source fragments while a line is streaming and
    validates the grammar::

        npm http fetch <METHOD> <STATUS> <URL> <DIGITS>ms
            [attempt #<DIGITS>] [(cache <OUTCOME>)]

    Only bounded parser state is retained: the grammar position, the method
    (at most 16 characters), the three-digit status, bounded significant-digit
    latency and attempt state, and the cache outcome.  The complete source
    line and the complete raw URL are never retained.  The URL authority is
    parsed incrementally without ever holding the raw authority: user
    information is consumed and discarded the moment an ``@`` is seen, and a
    candidate that can no longer be a valid ``host[:port]`` is dropped
    immediately (a later ``@`` would have made it user information anyway).
    Only the user-information-free host candidate (the host plus an optional
    numeric port) is retained until the authority delimiter, then normalized
    through
    :func:`docker.versioning.diagnostic_projection.normalized_authority_host`
    and discarded.

    Recognition matches the complete-line :func:`parse_npm_fetch_line` for
    every authority whose user information does not itself percent-decode to a
    URL structural byte or NFKC-expand to a structural delimiter.  For that
    pathological case the recognizer fails closed (rejects) rather than
    retain the user information it would need to reproduce the complete-line
    result, so it never admits a line the complete-line parser rejects.

    At the record boundary :meth:`finish` returns a parser-minimal
    :class:`SafeFetchRecord` (with ``host_path=None``) or ``None``.  The caller
    supplies the sanitized host/path fact needed for ``host-path`` grouping.
    Canonical source-field secret matching runs incrementally through
    :class:`_SecretFieldMatcher`; :attr:`canonical_unsafe` reports whether a
    configured secret occurs in a source-derived canonical field.
    """

    def __init__(self, secrets: Sequence[str] = ()) -> None:
        self._matcher = _SecretFieldMatcher(secrets)
        self.reset()

    def reset(self) -> None:
        """Discard all per-line parser state for the next record."""
        self._state = _ST_PREFIX
        self._literal_index = 0
        self._literal = ""
        self._method: list[str] = []
        self._status: list[str] = []
        self._status_value: int | None = None
        self._scheme: list[str] = []
        self._host_candidate: list[str] = []
        self._host_invalid = False
        self._host_bracketed = False
        self._host_port = False
        self._userinfo_percent_structural = False
        self._userinfo_bracket = False
        self._userinfo_unicode = False
        self._segment_percent_structural = False
        self._segment_bracket = False
        self._segment_unicode = False
        self._segment_escape: str | None = None
        self._latency = _NumericField()
        self._attempt = _NumericField()
        self._attempt_seen = False
        self._cache: list[str] = []
        self._malformed = False
        self._overflowed = False
        self._matcher.reset()

    def mark_overflowed(self) -> None:
        """Stop retaining token content after the line was truncated."""
        self._overflowed = True
        self._malformed = True

    @property
    def canonical_unsafe(self) -> bool:
        """Whether a configured secret occurs in a source-derived field."""
        return self._matcher.hit

    def feed(self, text: str) -> None:
        """Consume one terminal-safe source fragment (without its newline)."""
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        if self._malformed or self._overflowed:
            return
        for character in text:
            self._consume(character)
            if self._malformed:
                return

    def finish(self) -> SafeFetchRecord | None:
        """Return the parser-minimal record at the record boundary, or ``None``."""
        if self._malformed or self._overflowed:
            return None
        state = self._state
        if state == _ST_ATTEMPT_DIGITS:
            if not self._attempt.valid:
                return None
            self._matcher.feed(f" attempt #{self._attempt.value()}")
            self._attempt_seen = True
        elif state not in (_ST_AFTER_LATENCY, _ST_DONE):
            return None
        if not self._latency.valid or self._status_value is None:
            return None
        if not self._method:
            return None
        cache = "".join(self._cache)
        return SafeFetchRecord(
            method="".join(self._method),
            status=self._status_value,
            attempt=self._attempt.value() if self._attempt_seen else None,
            cache_outcome=cache if cache else None,
        )

    # -- internals ------------------------------------------------------

    def _consume(self, character: str) -> None:
        state = self._state
        if state == _ST_PREFIX:
            self._consume_prefix(character)
        elif state == _ST_METHOD:
            self._consume_method(character)
        elif state == _ST_STATUS:
            self._consume_status(character)
        elif state == _ST_URL_SCHEME:
            self._consume_url_scheme(character)
        elif state == _ST_URL_SLASH1:
            if character == "/":
                self._state = _ST_URL_SLASH2
            else:
                self._malformed = True
        elif state == _ST_URL_SLASH2:
            if character == "/":
                self._state = _ST_URL_AUTHORITY
            else:
                self._malformed = True
        elif state == _ST_URL_AUTHORITY:
            self._consume_url_authority(character)
        elif state == _ST_URL_BODY:
            self._consume_url_body(character)
        elif state == _ST_LATENCY:
            self._consume_latency(character)
        elif state == _ST_LATENCY_MS:
            if character == "s":
                self._state = _ST_AFTER_LATENCY
            else:
                self._malformed = True
        elif state == _ST_AFTER_LATENCY:
            if character == " ":
                self._state = _ST_TAIL_SPACE
            else:
                self._malformed = True
        elif state == _ST_TAIL_SPACE:
            self._consume_tail_space(character)
        elif state == _ST_ATTEMPT_LITERAL:
            self._consume_literal(character, _ST_ATTEMPT_DIGITS)
        elif state == _ST_ATTEMPT_DIGITS:
            self._consume_attempt_digits(character)
        elif state == _ST_CACHE_LITERAL:
            self._consume_literal(character, _ST_CACHE_CHARS)
        elif state == _ST_CACHE_CHARS:
            self._consume_cache_chars(character)
        else:  # _ST_DONE
            self._malformed = True

    def _consume_prefix(self, character: str) -> None:
        if (
            self._literal_index < len(_PREFIX_SPACED)
            and character == _PREFIX_SPACED[self._literal_index]
        ):
            self._literal_index += 1
            if self._literal_index == len(_PREFIX_SPACED):
                self._literal_index = 0
                self._matcher.feed(NPM_FETCH_PREFIX)
                self._state = _ST_METHOD
        else:
            self._malformed = True

    def _consume_method(self, character: str) -> None:
        if character in _METHOD_CHARS:
            if len(self._method) >= _MAX_METHOD_CHARS:
                self._malformed = True
            else:
                self._method.append(character)
        elif character == " ":
            if not self._method:
                self._malformed = True
            else:
                self._matcher.feed(" " + "".join(self._method))
                self._state = _ST_STATUS
        else:
            self._malformed = True

    def _consume_status(self, character: str) -> None:
        if "0" <= character <= "9":
            if len(self._status) >= 3:
                self._malformed = True
            else:
                self._status.append(character)
        elif character == " ":
            if len(self._status) != 3:
                self._malformed = True
                return
            value = int("".join(self._status))
            if not 200 <= value <= 299:
                self._malformed = True
                return
            self._status_value = value
            self._matcher.feed(" " + "".join(self._status))
            self._state = _ST_URL_SCHEME
        else:
            self._malformed = True

    def _consume_url_scheme(self, character: str) -> None:
        if not self._scheme:
            if character.isascii() and character.isalpha():
                self._scheme.append(character)
            else:
                self._malformed = True
        elif character == ":":
            self._state = _ST_URL_SLASH1
        elif character in _SCHEME_REST_CHARS:
            self._scheme.append(character)
        else:
            self._malformed = True

    def _consume_url_authority(self, character: str) -> None:
        if character in (" ", "/", "?", "#"):
            if not self._finish_host():
                self._malformed = True
                return
            self._state = (
                _ST_LATENCY if character == " " else _ST_URL_BODY
            )
            return
        if ord(character) < 0x20 or ord(character) == 0x7F:
            self._malformed = True
            return
        if character == "@":
            # Everything before the last ``@`` is user information.  Discard
            # the accumulated candidate immediately and restart host parsing
            # for the actual host portion, so no credential-bearing substring
            # survives this call.  A user-information segment that could
            # percent-decode or normalize into a structural token is recorded
            # so the recognizer can fail closed rather than diverge from the
            # complete-line parser.
            self._userinfo_percent_structural = (
                self._userinfo_percent_structural
                or self._segment_percent_structural
            )
            self._userinfo_bracket = (
                self._userinfo_bracket or self._segment_bracket
            )
            self._userinfo_unicode = (
                self._userinfo_unicode or self._segment_unicode
            )
            self._segment_percent_structural = False
            self._segment_bracket = False
            self._segment_unicode = False
            self._segment_escape = None
            self._reset_host_candidate()
            return
        self._note_segment_character(character)
        if self._host_invalid:
            # The candidate can no longer be a valid ``host[:port]``.  It is
            # retained only long enough to be discarded at ``@`` or to fail
            # closed at the delimiter, so no further source is accumulated.
            return
        if self._host_bracketed:
            self._host_candidate.append(character)
            if character == "]":
                self._host_bracketed = False
            return
        if character == "[":
            if self._host_candidate:
                # Data before a bracket is never a valid bracketed host.
                self._reset_host_candidate()
                self._host_invalid = True
            else:
                self._host_bracketed = True
                self._host_candidate.append(character)
            return
        if self._host_port:
            if "0" <= character <= "9":
                self._host_candidate.append(character)
            else:
                # A non-numeric port is never valid.  Drop the candidate; a
                # later ``@`` would make it user information anyway.
                self._reset_host_candidate()
                self._host_invalid = True
            return
        if character == ":":
            self._host_port = True
            self._host_candidate.append(character)
            return
        self._host_candidate.append(character)

    def _reset_host_candidate(self) -> None:
        """Discard the accumulated host candidate and its validation flags."""
        self._host_candidate = []
        self._host_invalid = False
        self._host_bracketed = False
        self._host_port = False

    def _note_segment_character(self, character: str) -> None:
        """Track whether the current authority segment can become structural.

        The segment becomes user information if a later ``@`` is seen, so a
        literal bracket, an NFKC-expanding non-ASCII character, or a percent
        escape that decodes to ``/?#[]%``, a control removed by the URL
        splitter, or a non-ASCII byte is recorded here.  Escape tracking is
        purely local (at most a ``%`` plus two hex digits) and never retains
        the segment text.
        """
        if character in "[]":
            self._segment_bracket = True
        elif not character.isascii() and _nfkc_introduces_delimiter(
            character
        ):
            self._segment_unicode = True
        escape = self._segment_escape
        if escape is None:
            if character == "%":
                self._segment_escape = "%"
            return
        if character in _HEX_DIGITS:
            candidate = escape + character
            if len(candidate) == 3:
                self._segment_escape = None
                if _percent_escape_is_structural(candidate[1:]):
                    self._segment_percent_structural = True
            else:
                self._segment_escape = candidate
            return
        # An invalid escape leaves the ``%`` literal, which does not change
        # how the authority is split.
        self._segment_escape = "%" if character == "%" else None

    def _consume_url_body(self, character: str) -> None:
        if character == " ":
            self._state = _ST_LATENCY
        elif ord(character) < 0x20 or ord(character) == 0x7F:
            self._malformed = True

    def _finish_host(self) -> bool:
        # ``_host_candidate`` is already free of user information: every ``@``
        # discarded the candidate accumulated before it.  The scheme and the
        # host candidate are discarded immediately after validation, so a
        # credential-bearing authority substring never survives the URL field.
        hostinfo = "".join(self._host_candidate)
        scheme = "".join(self._scheme)
        invalid = self._host_invalid or self._host_bracketed
        userinfo_percent = self._userinfo_percent_structural
        userinfo_bracket = self._userinfo_bracket
        userinfo_unicode = self._userinfo_unicode
        self._reset_host_candidate()
        self._userinfo_percent_structural = False
        self._userinfo_bracket = False
        self._userinfo_unicode = False
        self._segment_percent_structural = False
        self._segment_bracket = False
        self._segment_unicode = False
        self._segment_escape = None
        self._scheme = []
        if invalid or not hostinfo:
            return False
        if userinfo_bracket or userinfo_unicode:
            # Literal brackets or an NFKC-expanding non-ASCII user information
            # character can make the complete-line parser's literal
            # ``urlsplit`` raise even when the host candidate normalizes; fail
            # closed rather than risk a divergence.
            return False
        if literal_authority_host(scheme, hostinfo) is not None:
            # The literal layer is already valid, so decoding user information
            # could not change which layer the complete-line parser selects.
            return True
        if userinfo_percent:
            # A percent escape inside user information decodes into ``/``,
            # ``?``, ``#``, ``[``, ``]``, ``%``, a control or a non-ASCII byte
            # and so can shift the decoded authority; without retaining that
            # user information the only safe answer is to fail closed.
            return False
        return normalized_authority_host(scheme, hostinfo) is not None

    def _consume_latency(self, character: str) -> None:
        if "0" <= character <= "9":
            if not self._latency.feed(character):
                self._malformed = True
        elif character == "m":
            if not self._latency.valid:
                self._malformed = True
            else:
                self._state = _ST_LATENCY_MS
        else:
            self._malformed = True

    def _consume_tail_space(self, character: str) -> None:
        if character == "(":
            self._literal = _CACHE_LITERAL
            self._literal_index = 0
            self._state = _ST_CACHE_LITERAL
        elif character == "a" and not self._attempt_seen:
            self._literal = _ATTEMPT_LITERAL
            self._literal_index = 0
            self._state = _ST_ATTEMPT_LITERAL
        else:
            self._malformed = True

    def _consume_literal(self, character: str, next_state: int) -> None:
        if (
            self._literal_index < len(self._literal)
            and character == self._literal[self._literal_index]
        ):
            self._literal_index += 1
            if self._literal_index == len(self._literal):
                self._literal_index = 0
                self._state = next_state
        else:
            self._malformed = True

    def _consume_attempt_digits(self, character: str) -> None:
        if "0" <= character <= "9":
            if not self._attempt.feed(character):
                self._malformed = True
        elif character == " ":
            if not self._attempt.valid:
                self._malformed = True
                return
            self._matcher.feed(f" attempt #{self._attempt.value()}")
            self._attempt_seen = True
            self._state = _ST_TAIL_SPACE
        else:
            self._malformed = True

    def _consume_cache_chars(self, character: str) -> None:
        if character == ")":
            if not self._cache:
                self._malformed = True
                return
            self._matcher.feed(f" (cache {''.join(self._cache)})")
            self._state = _ST_DONE
        elif character in _ASCII_LOWER_CHARS:
            self._cache.append(character)
        else:
            self._malformed = True


def _safe_host_path_text(
    record: NpmFetchRecord | SafeFetchRecord, secrets: Sequence[str]
) -> str | None:
    """Return the projector's normalized host plus canonical safe path.

    The parser already knows the URL field ends before the latency field, so
    the URL is projected as a delimiter-complete field (a trailing space)
    rather than as an EOF fragment.  Passing the bare URL would trigger the
    projector's conservative end-of-input handling for potentially truncated
    tokens, dropping single-label hosts and hostname recovery on an unsafe
    path; the shared projector's completeness checks are deliberately left
    unchanged for callers that really may receive truncated URLs.

    A :class:`SafeFetchRecord` already carries the sanitized
    :class:`SafeHostPath` supplied by the streaming projector, so it is used
    directly without re-deriving or re-retaining the URL.
    """
    if isinstance(record, SafeFetchRecord):
        return record.host_path.text if record.host_path is not None else None
    facts = sanitize_host_paths(record.url + " ", secrets)
    if not facts:
        return None
    return facts[0].text


def fetch_group_identity(
    record: NpmFetchRecord | SafeFetchRecord,
    display: NetworkUrlDisplay,
    *,
    secrets: Sequence[str] = (),
) -> FetchGroupKey | None:
    """Return the request-count group identity for *record* and *display*.

    ``redacted`` excludes the URL and latency; ``host-path`` additionally
    includes the normalized hostname and canonical safe path but still
    excludes latency.  Both include the method, exact status, attempt
    presence/value, and cache outcome.  ``exact`` returns ``None`` because it
    disables aggregation.
    """
    if not isinstance(record, (NpmFetchRecord, SafeFetchRecord)):
        raise TypeError("record must be an NpmFetchRecord or SafeFetchRecord")
    if not isinstance(display, NetworkUrlDisplay):
        raise TypeError("display must be a NetworkUrlDisplay member")
    if display is NetworkUrlDisplay.EXACT:
        return None
    host_path = None
    if display is NetworkUrlDisplay.HOST_PATH:
        host_path = _safe_host_path_text(record, secrets)
    return FetchGroupKey(
        display=display,
        method=record.method,
        status=record.status,
        attempt=record.attempt,
        cache_outcome=record.cache_outcome,
        host_path=host_path,
    )


def canonical_fetch_text(
    record: NpmFetchRecord | SafeFetchRecord,
    display: NetworkUrlDisplay,
    *,
    secrets: Sequence[str] = (),
) -> str | None:
    """Return the canonical rendered line for *record* and *display*.

    The canonical text omits latency and preserves ``attempt #N`` when
    present.  ``redacted`` renders the URL as ``<redacted>``.  ``host-path``
    renders the normalized hostname and terminal-safe path; an unsafe path
    falls back to ``/<redacted-path>`` and a host that cannot be safely
    derived falls back to ``<redacted>``.  ``exact`` returns ``None``.

    Only the URL-derived portion is sanitized with *secrets*.  The method,
    exact status, and optional attempt and cache clauses are reproduced from
    the source line; a caller that renders them must first prove with
    :func:`fetch_source_fields_text` that the canonical form cannot restore a
    configured secret the projector removed.
    """
    if not isinstance(record, (NpmFetchRecord, SafeFetchRecord)):
        raise TypeError("record must be an NpmFetchRecord or SafeFetchRecord")
    if not isinstance(display, NetworkUrlDisplay):
        raise TypeError("display must be a NetworkUrlDisplay member")
    if display is NetworkUrlDisplay.EXACT:
        return None
    if display is NetworkUrlDisplay.HOST_PATH:
        rendered_url = _safe_host_path_text(record, secrets) or REDACTED
    else:
        rendered_url = REDACTED
    clauses = [NPM_FETCH_PREFIX, record.method, str(record.status), rendered_url]
    if record.attempt is not None:
        clauses.append(f"attempt #{record.attempt}")
    if record.cache_outcome is not None:
        clauses.append(f"(cache {record.cache_outcome})")
    return " ".join(clauses)


def fetch_source_fields_text(record: NpmFetchRecord | SafeFetchRecord) -> str:
    """Return the canonical text of the fetch fields copied from the source.

    The fixed prefix, method, exact status, and the optional ``attempt #N``
    and ``(cache …)`` clauses are reproduced from the source line without
    passing through the URL projector.  That is what makes them a potential
    disclosure path: a configured secret inside any of them was redacted from
    the sanitized selected diagnostic but would be restored by
    :func:`canonical_fetch_text`.  The URL-derived portion is deliberately
    excluded because the shared projector already sanitizes it.  A caller
    rejects a canonical rendering when any configured secret occurs in this
    text.
    """
    if not isinstance(record, (NpmFetchRecord, SafeFetchRecord)):
        raise TypeError("record must be an NpmFetchRecord or SafeFetchRecord")
    clauses = [NPM_FETCH_PREFIX, record.method, str(record.status)]
    if record.attempt is not None:
        clauses.append(f"attempt #{record.attempt}")
    if record.cache_outcome is not None:
        clauses.append(f"(cache {record.cache_outcome})")
    return " ".join(clauses)


__all__ = [
    "NPM_FETCH_PREFIX",
    "FetchGroupKey",
    "NpmFetchRecord",
    "NpmFetchRecognizer",
    "SafeFetchRecord",
    "canonical_fetch_text",
    "fetch_group_identity",
    "fetch_source_fields_text",
    "parse_npm_fetch_line",
]
