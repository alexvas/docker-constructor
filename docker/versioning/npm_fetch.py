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

Nothing here renders, schedules a timer, reads a mailbox, mutates state, or
touches transport, Docker, or the filesystem.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence
from urllib.parse import urlsplit

from docker.npm_environment.streaming import REDACTED
from docker.versioning.diagnostic_projection import (
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


def _safe_host_path_text(
    record: NpmFetchRecord, secrets: Sequence[str]
) -> str | None:
    """Return the projector's normalized host plus canonical safe path.

    The parser already knows the URL field ends before the latency field, so
    the URL is projected as a delimiter-complete field (a trailing space)
    rather than as an EOF fragment.  Passing the bare URL would trigger the
    projector's conservative end-of-input handling for potentially truncated
    tokens, dropping single-label hosts and hostname recovery on an unsafe
    path; the shared projector's completeness checks are deliberately left
    unchanged for callers that really may receive truncated URLs.
    """
    facts = sanitize_host_paths(record.url + " ", secrets)
    if not facts:
        return None
    return facts[0].text


def fetch_group_identity(
    record: NpmFetchRecord,
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
    if not isinstance(record, NpmFetchRecord):
        raise TypeError("record must be an NpmFetchRecord")
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
    record: NpmFetchRecord,
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
    """
    if not isinstance(record, NpmFetchRecord):
        raise TypeError("record must be an NpmFetchRecord")
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


__all__ = [
    "NPM_FETCH_PREFIX",
    "FetchGroupKey",
    "NpmFetchRecord",
    "canonical_fetch_text",
    "fetch_group_identity",
    "parse_npm_fetch_line",
]
