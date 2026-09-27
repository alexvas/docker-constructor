"""Boundary-safe, output-policy-independent diagnostic projection.

Phase 2 of ``improve-host-build-observability`` introduces one shared
projection layer for host-pipeline diagnostics.  It:

* validates logical host-resource context against the closed reviewed asset
  names and the fixed ``npm-assembler-<digest-prefix>`` container form;
* applies the existing deterministic secret redaction and then incrementally
  identifies URL-shaped tokens across arbitrary decoder/chunk boundaries,
  including percent-encoded scheme characters (``h%74tps``,
  ``%68%74%74%70%73``) and percent-encoded scheme separators (``%3A%2F%2F``),
  applying percent-decoding in bounded repeated passes so nested-encoded forms
  such as ``https%253A%252F%252Fhost`` are detected as well, and withholding
  any syntactically valid scheme prefix (not only a fixed scheme list) until a
  safe boundary disambiguates it;
* removes each complete URL from text and records only its normalized hostname
  separately, discarding scheme, port, user information, path, query,
  fragment, and proxy details, and suppresses that hostname fact whenever it
  canonically matches a registered secret -- after the same bounded percent
  decoding applied to diagnostic URL candidates and with the same case,
  trailing-dot, and IDNA normalization -- so an uppercase, encoded, or IDN
  proxy secret cannot leak its host;
* additionally derives a bounded internal :class:`SafeHostPath` fact
  (normalized hostname plus a canonical safe encoded path) for local
  presentation only, excluding scheme, user information, explicit port, query,
  fragment, and proxy detail, never decoding percent-encoded controls into
  active text, percent-encoding every raw non-ASCII character (including bidi
  and format controls such as U+202E) from its UTF-8 bytes, and failing closed
  to :data:`REDACTED_PATH_MARKER` when the path is ambiguous, secret-bearing,
  or otherwise unsafe.  This fact is deliberately absent from the external SDK
  DTO, evidence, persistence, and semantic identities;
* keeps ambiguous trailing URL/secret state in a pending buffer capped at
  8 KiB, appending only UTF-8-byte-bounded slices so a multibyte feed cannot
  transiently exceed the bound, emitting the fixed
  ``sanitized oversized token`` marker and
  discarding through the next safe boundary, and finalizing unresolved
  candidates with the fixed ``sanitized incomplete token`` marker: at clean
  EOF a syntactically complete URL is still removed with its hostname fact
  while an entered separator or a partial secret fails closed, and a bare
  scheme-like word is flushed unchanged, whereas reader failure or
  cancellation fails closed for every ambiguous URL or encoded-scheme prefix;
* projects deterministic exception-type chains of at most four unique names
  without evaluating exception messages; and
* never accepts output policy, so presentation decisions stay in the facade.

The module is deliberately free of transport, Docker, filesystem, and
presentation dependencies.
"""
from __future__ import annotations

import codecs
import ipaddress
import json
import re
import urllib.parse
from dataclasses import dataclass
from typing import Sequence

from docker.npm_environment.streaming import (
    REDACTED,
    dedupe_secrets,
    longest_secret_length,
)
from docker.versioning.diagnostic_identity import SessionUrlIdentity
from docker.versioning.host_progress import (
    HostDiagnosticClassification,
    HostDiagnosticStream,
    HostPhase,
    HostStep,
    HostStructuredDiagnostic,
)
from docker.versioning.logical_resource import (
    PI_RELEASE_ASSET_NAMES,
    REVIEWED_ARTIFACT_NAMES,
    DiagnosticLogicalResource,
    DiagnosticResourceKind,
)

#: Maximum retained ambiguous URL/secret pending state (UTF-8 bytes).
PENDING_LIMIT_BYTES = 8 * 1024

#: Fixed fail-closed marker for a candidate that exceeds the pending limit.
OVERSIZED_TOKEN_MARKER = "[sanitized oversized token]"

#: Fixed fail-closed marker for an unresolved candidate at stream termination.
INCOMPLETE_TOKEN_MARKER = "[sanitized incomplete token]"

#: Fixed fail-closed path used when safe path derivation cannot be established.
#: It always starts with ``/`` so a fact renders as ``host/<redacted-path>``.
REDACTED_PATH_MARKER = "/<redacted-path>"

#: Maximum number of unique exception type names a projected chain may carry.
EXCEPTION_TYPE_LIMIT = 4


@dataclass(frozen=True, slots=True)
class SafeHostPath:
    """Bounded internal host/path presentation fact.

    ``hostname`` is the same normalized hostname carried by the hostname fact.
    ``path`` is either a canonical safe *ASCII* encoded path that always begins
    with ``/`` or :data:`REDACTED_PATH_MARKER`.  Scheme, user information,
    explicit port, query, fragment, proxy detail, credentials, and caller
    secrets are excluded by construction; percent-encoded content is never
    decoded into active text; and every raw non-ASCII character (including
    bidi and format controls such as U+202E) is UTF-8 percent-encoded, so no
    active Unicode control can appear in ``path`` or ``text``.  ``text`` is
    the renderable ``hostname + path`` form.

    The fact is local-presentation metadata only: it MUST NOT enter the
    external SDK DTO, verification or assembler evidence, persistence, or any
    semantic identity.
    """

    hostname: str
    path: str

    def __post_init__(self) -> None:
        if not isinstance(self.hostname, str) or not self.hostname:
            raise ValueError("hostname must be a nonempty string")
        if not isinstance(self.path, str) or not self.path.startswith("/"):
            raise ValueError("path must be an absolute encoded path")
        # A canonical safe path is ASCII: raw C0/DEL controls and every
        # non-ASCII character (including bidi and format controls such as
        # U+202E) must have been percent-encoded before the fact is built, so
        # active Unicode controls can never reach ``path`` or ``text``.
        if not self.path.isascii() or any(
            character < " " or character == "\x7f" for character in self.path
        ):
            raise ValueError("path must be a terminal-safe ASCII encoded path")

    @property
    def text(self) -> str:
        """Return the renderable ``hostname + path`` presentation value."""
        return f"{self.hostname}{self.path}"


_SCHEME_CHAR_CLASS = r"[A-Za-z0-9+.\-]"
_SCHEME_CHAR_RE = re.compile(_SCHEME_CHAR_CLASS)
_HEX_PAIR_RE = re.compile(r"[0-9A-Fa-f]{2}")
#: A scheme character may itself be percent-encoded (``h%74tps``), so the
#: scheme pattern accepts literal scheme characters or ``%XX`` units.
_SCHEME_UNIT = rf"(?:{_SCHEME_CHAR_CLASS}|%[0-9A-Fa-f]{{2}})"
_SCHEME_FIRST_UNIT = r"(?:[A-Za-z]|%[0-9A-Fa-f]{2})"
_SCHEME_PATTERN = rf"{_SCHEME_FIRST_UNIT}{_SCHEME_UNIT}*"
_COLON_UNIT = r"(?::|%3[Aa])"
_SLASH_UNIT = r"(?:/|%2[Ff])"

#: A URL start is a scheme -- fully or partially percent-encoded -- followed
#: by ``://`` or its percent-encoded form (``%3A%2F%2F``, case-insensitive and
#: arbitrarily mixed).
_URL_START_RE = re.compile(
    rf"{_SCHEME_PATTERN}{_COLON_UNIT}{_SLASH_UNIT}{_SLASH_UNIT}"
)
_HOSTNAME_OK_RE = re.compile(r"[a-z0-9._:\-]+")
_TERMINATOR_CHARS = frozenset(" \t\r\n\f\v\"'`<>|\\^{}")
_FEED_SLICE_BYTES = 4096

#: RFC 3986 unreserved bytes decode to text; every other percent escape stays
#: encoded (with uppercase hex) so percent-encoded controls never become active.
_UNRESERVED_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)
#: RFC 3986 ``pchar`` plus ``/``: the ASCII bytes a canonical safe path keeps
#: literal.  Every other ASCII byte is percent-encoded, and every non-ASCII
#: character -- including Unicode bidi and format controls such as U+202E -- is
#: UTF-8 percent-encoded with uppercase hex so it can never stay active.
_PATH_SAFE_ASCII = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789"
    "-._~"
    "!$&'()*+,;="
    ":@/"
)
#: Raw C0, DEL, and C1 controls are unsafe whenever they survive to a path.
_CONTROL_CODES = frozenset(
    [*range(0x00, 0x20), 0x7F, *range(0x80, 0xA0)]
)

#: Authority after any user information, with an optional numeric port.  A
#: trailing colon or a non-numeric port is malformed, not merely incomplete.
_AUTHORITY_RE = re.compile(r"(?:\[[0-9A-Fa-f:.]+\]|[^:\[\]]*)(?::[0-9]+)?")

#: Maximum number of additional percent-decoding passes applied when looking
#: for a URL.  Double-encoded forms such as ``https%253A%252F%252Fhost`` need
#: two passes; the extra pass keeps a small margin.  Decoding always stops
#: early once a pass no longer changes the text.
URL_DECODE_LAYER_LIMIT = 3


def _is_terminator(character: str) -> bool:
    return (
        character in _TERMINATOR_CHARS
        or ord(character) < 0x20
        or ord(character) == 0x7F
    )


def _find_terminator(text: str, start: int = 0) -> int | None:
    for index in range(start, len(text)):
        if _is_terminator(text[index]):
            return index
    return None


def _last_terminator_index(text: str) -> int:
    for index in range(len(text) - 1, -1, -1):
        if _is_terminator(text[index]):
            return index
    return -1


def _leftmost_secret_match(
    text: str, secrets: tuple[str, ...]
) -> tuple[int, int] | None:
    for index in range(len(text)):
        matched = 0
        for secret in secrets:
            if text.startswith(secret, index) and len(secret) > matched:
                matched = len(secret)
        if matched:
            return index, matched
    return None


#: URL-prefix classification for a token that has not ended a safe boundary.
#: ``NONE`` can never become a URL; ``SCHEME`` is only scheme characters so
#: far and may still be an ordinary word; ``SEPARATOR`` has begun a scheme
#: separator and is an unresolved URL candidate.
_URL_PREFIX_NONE = 0
_URL_PREFIX_SCHEME = 1
_URL_PREFIX_SEPARATOR = 2


def _url_prefix_state_single(token: str) -> int:
    """Classify how far *token* has advanced toward a single-layer URL start.

    Scheme characters may be percent-encoded (``h%74tps`` or
    ``%68%74%74%70%73``), so the token is decoded unit by unit.  Any valid
    RFC 3986-style scheme is accepted -- a letter followed by letters, digits,
    ``+``, ``.`` or ``-``, or their percent-encoded equivalents -- so
    non-listed schemes such as ``git`` or ``custom+transport`` are withheld
    exactly like ``https``.  A separator that has begun, literally or as
    ``%3A``/``%2F``, is always an unresolved URL candidate.
    """
    index = 0
    length = len(token)
    scheme: list[str] = []
    while index < length:
        char = token[index]
        if char == "%":
            if index + 2 >= length:
                # An incomplete escape may still become the next scheme
                # character or the start of an encoded separator.
                return _URL_PREFIX_SCHEME
            pair = token[index + 1 : index + 3]
            if _HEX_PAIR_RE.fullmatch(pair) is None:
                return _URL_PREFIX_NONE
            decoded = chr(int(pair, 16))
            if decoded in ":/":
                return _URL_PREFIX_SEPARATOR
            if _SCHEME_CHAR_RE.fullmatch(decoded) is None:
                return _URL_PREFIX_NONE
            if not scheme and not decoded.isalpha():
                return _URL_PREFIX_NONE
            scheme.append(decoded)
            index += 3
        elif _SCHEME_CHAR_RE.fullmatch(char) is not None:
            if not scheme and not char.isalpha():
                return _URL_PREFIX_NONE
            scheme.append(char)
            index += 1
        elif char in ":/":
            return _URL_PREFIX_SEPARATOR
        else:
            return _URL_PREFIX_NONE
    if not scheme:
        return _URL_PREFIX_NONE
    return _URL_PREFIX_SCHEME


def _decode_percent_layer(text: str) -> tuple[str, list[int]]:
    """Decode one percent-encoding pass, tracking raw source indices.

    Each complete ``%XX`` unit becomes the character with that code point and
    is attributed to the raw index of its ``%``.  Incomplete or invalid
    escapes are copied verbatim.  Unlike :func:`urllib.parse.unquote` this
    never raises and maps each decoded character back to its source, which is
    what makes nested-encoded URL detection boundary-safe.
    """
    out: list[str] = []
    sources: list[int] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "%" and index + 2 < length:
            pair = text[index + 1 : index + 3]
            if _HEX_PAIR_RE.fullmatch(pair) is not None:
                out.append(chr(int(pair, 16)))
                sources.append(index)
                index += 3
                continue
        out.append(char)
        sources.append(index)
        index += 1
    return "".join(out), sources


def _find_url_start(text: str) -> int | None:
    """Return the raw index of the leftmost (possibly nested) URL start.

    Every decoding layer is searched and the smallest raw index wins, so a
    nested-encoded URL earlier in the buffer cannot be masked by a literal URL
    later in it.  ``None`` means no layer revealed a URL start.
    """
    match = _URL_START_RE.search(text)
    best = match.start() if match is not None else None
    if best == 0:
        return 0
    current = text
    sources: list[int] | None = None
    for _ in range(URL_DECODE_LAYER_LIMIT):
        if "%" not in current:
            break
        decoded, decoded_sources = _decode_percent_layer(current)
        if decoded == current:
            break
        if sources is None:
            sources = decoded_sources
        else:
            sources = [sources[source] for source in decoded_sources]
        current = decoded
        match = _URL_START_RE.search(current)
        if match is None:
            continue
        raw = sources[match.start()]
        if best is None or raw < best:
            best = raw
            if raw == 0:
                return 0
    return best


def _url_prefix_state(token: str) -> int:
    """Return the furthest URL-prefix state across decoding layers."""
    state = _URL_PREFIX_NONE
    current = token
    for _ in range(URL_DECODE_LAYER_LIMIT + 1):
        layer_state = _url_prefix_state_single(current)
        if layer_state > state:
            state = layer_state
        if state == _URL_PREFIX_SEPARATOR:
            return state
        if "%" not in current:
            break
        decoded, _ = _decode_percent_layer(current)
        if decoded == current:
            break
        current = decoded
    return state


def _is_url_prefix_token(token: str) -> bool:
    """Return whether *token* could still grow into a URL start."""
    return _url_prefix_state(token) != _URL_PREFIX_NONE


def _secret_overlap_hold(pending: str, secrets: tuple[str, ...], longest: int) -> int:
    """Return the trailing length that could still complete a longer secret."""
    if longest <= 1:
        return 0
    max_k = min(longest - 1, len(pending))
    for k in range(max_k, 0, -1):
        tail = pending[-k:]
        if any(secret.startswith(tail) and len(secret) > k for secret in secrets):
            return k
    return 0


def _trailing_token(pending: str) -> str:
    """Return the text after the last safe boundary."""
    return pending[_last_terminator_index(pending) + 1 :]


def _ambiguous_hold(pending: str, secrets: tuple[str, ...], longest: int) -> int:
    """Return the trailing length that must stay pending as ambiguous."""
    hold = _secret_overlap_hold(pending, secrets, longest)
    token = _trailing_token(pending)
    if _is_url_prefix_token(token):
        hold = max(hold, len(token))
    return hold


def _unresolved_candidate(
    pending: str, secrets: tuple[str, ...], longest: int, *, abort: bool
) -> bool:
    """Whether *pending* is an unresolved URL or secret candidate.

    A detected URL or a trailing partial secret always fails closed.  For the
    remaining ambiguous scheme prefix, *abort* selects the policy: a clean EOF
    treats a bare but syntactically valid scheme prefix (``git``,
    ``custom+transport``) as ordinary text, whereas reader failure or
    cancellation fails closed for both ``_URL_PREFIX_SCHEME`` and
    ``_URL_PREFIX_SEPARATOR`` because the prefix may be a truncated URL --
    including percent-encoded prefixes such as ``%``, ``%6``, ``%67``,
    ``%67it``, or ``git%3``.
    """
    if _find_url_start(pending) is not None:
        return True
    if _secret_overlap_hold(pending, secrets, longest):
        return True
    state = _url_prefix_state(_trailing_token(pending))
    if abort:
        return state != _URL_PREFIX_NONE
    return state == _URL_PREFIX_SEPARATOR


def _normalize_hostname_text(value: str) -> str | None:
    """Canonicalize one hostname-like value, or ``None`` when unsafe.

    The same rules are applied to a projected hostname and to every
    hostname-like value derived from a registered secret: lowercase, drop a
    trailing dot, and convert a Unicode domain name to IDNA ASCII form.
    """
    text = value.strip()
    if not text:
        return None
    if text.isascii():
        normalized = text
    else:
        try:
            normalized = text.encode("idna").decode("ascii")
        except UnicodeError:
            return None
    normalized = normalized.lower().rstrip(".")
    if not normalized or _HOSTNAME_OK_RE.fullmatch(normalized) is None:
        return None
    return normalized


def _normalized_host(candidate: str) -> str | None:
    """Return the normalized hostname of *candidate*, or ``None`` if unsafe."""
    try:
        parsed = urllib.parse.urlsplit(candidate)
    except ValueError:
        return None
    host = parsed.hostname
    if not host:
        return None
    return _normalize_hostname_text(host)


def _take_utf8_prefix(text: str, start: int, max_bytes: int) -> tuple[str, int]:
    """Return the longest prefix of ``text[start:]`` fitting ``max_bytes``."""
    end = start
    used = 0
    length = len(text)
    while end < length:
        size = len(text[end].encode("utf-8"))
        if used + size > max_bytes:
            break
        used += size
        end += 1
    return text[start:end], end


def _decode_percent(text: str) -> str | None:
    """Percent-decode *text*, or ``None`` when it is not safely decodable."""
    try:
        return urllib.parse.unquote(text, errors="strict")
    except (UnicodeDecodeError, ValueError):
        return None


def _candidate_url_layer(candidate: str) -> str | None:
    """Return the first candidate layer whose host normalizes safely.

    Literal URLs are accepted directly.  Otherwise percent-decoding is applied
    in bounded repeated passes (the same bound as URL detection), and a layer
    is accepted only when its host normalizes safely.  Returning the layer --
    not just the host -- lets host/path derivation read the same encoded path
    that produced the host fact.
    """
    if _normalized_host(candidate) is not None:
        return candidate
    current = candidate
    for _ in range(URL_DECODE_LAYER_LIMIT):
        decoded = _decode_percent(current)
        if decoded is None or decoded == current:
            return None
        if _normalized_host(decoded) is not None:
            return decoded
        current = decoded
    return None


def _candidate_host(candidate: str) -> str | None:
    """Return the normalized host of *candidate*, decoding encoded URLs."""
    layer = _candidate_url_layer(candidate)
    if layer is None:
        return None
    return _normalized_host(layer)


def normalized_url_host(url: str) -> str | None:
    """Return the normalized hostname of a complete supported URL.

    This is the single shared authority/hostname rule used by host/path
    projection and by the npm fetch parser, so the two can never disagree
    about which hosts are valid.  It reuses the candidate-layer percent
    decoding and hostname normalization that produce a
    :class:`SafeHostPath`, so an uppercase host, a trailing FQDN dot, an
    IPv4/IPv6 literal, a punycode or raw IDNA host, a percent-encoded host,
    and an underscore-bearing reg-name normalize exactly as they do for a
    host fact.

    Returns ``None`` when *url* is not a string with an authority or when the
    *decoded* candidate layer's authority or hostname cannot be normalized
    safely; ``https://%``, ``https://[``, ``https://?x``, ``https:///x``,
    ``http://:80/x``, a trailing-colon authority such as
    ``https://example.com:/x``, an encoded trailing colon such as
    ``https://example.com%3A/x``, and a non-numeric or out-of-range port all
    return ``None``.  The caller is responsible for scheme structural checks.
    """
    if not isinstance(url, str) or not url:
        return None
    layer = _candidate_url_layer(url)
    if layer is None:
        return None
    try:
        parsed = urllib.parse.urlsplit(layer)
        parsed.port
    except ValueError:
        return None
    if not parsed.netloc:
        return None
    authority = parsed.netloc.rpartition("@")[2]
    if not authority or _AUTHORITY_RE.fullmatch(authority) is None:
        return None
    host = parsed.hostname
    if not host:
        return None
    return _normalize_hostname_text(host)


def _percent_encode_utf8(character: str) -> str | None:
    """Return the uppercase UTF-8 percent-encoding of *character*.

    Returns ``None`` for text that cannot be UTF-8 encoded (such as a lone
    surrogate), which fails the path closed.
    """
    try:
        payload = character.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return "".join(f"%{byte:02X}" for byte in payload)


def _normalize_percent_encoding(path: str) -> str | None:
    """Return *path* with canonical terminal-safe percent-encoding, or ``None``.

    Percent escapes must be complete ``%XX`` units.  An escape for an RFC 3986
    unreserved byte decodes to text; every other escape -- including
    percent-encoded controls -- stays encoded with uppercase hex digits so it
    can never become active terminal text.  A literal RFC 3986 path character
    is kept verbatim, while every other ASCII byte and every non-ASCII
    character (including Unicode bidi and format controls such as U+202E) is
    percent-encoded from its UTF-8 bytes with uppercase hex.  A trailing
    partial escape, an invalid escape, or unencodable text fails closed.
    """
    out: list[str] = []
    index = 0
    length = len(path)
    while index < length:
        character = path[index]
        if character == "%":
            if index + 2 >= length:
                return None
            pair = path[index + 1 : index + 3]
            if _HEX_PAIR_RE.fullmatch(pair) is None:
                return None
            decoded = chr(int(pair, 16))
            if decoded in _UNRESERVED_CHARS:
                out.append(decoded)
            else:
                out.append("%" + pair.upper())
            index += 3
            continue
        if character in _PATH_SAFE_ASCII:
            out.append(character)
        else:
            encoded = _percent_encode_utf8(character)
            if encoded is None:
                return None
            out.append(encoded)
        index += 1
    return "".join(out)


def _remove_dot_segments(path: str) -> str:
    """Apply RFC 3986 section 5.2.4 dot-segment removal to an encoded path."""
    output = ""
    while path:
        if path.startswith("../"):
            path = path[3:]
        elif path.startswith("./"):
            path = path[2:]
        elif path.startswith("/./"):
            path = "/" + path[3:]
        elif path == "/.":
            path = "/"
        elif path.startswith("/../"):
            path = "/" + path[4:]
            output = output.rpartition("/")[0]
        elif path == "/..":
            path = "/"
            output = output.rpartition("/")[0]
        elif path in (".", ".."):
            path = ""
        else:
            slash = path.find("/", 1) if path.startswith("/") else path.find("/")
            if slash == -1:
                output += path
                path = ""
            else:
                output += path[:slash]
                path = path[slash:]
    return output


def _path_contains_secret(
    path: str, secrets: tuple[str, ...]
) -> bool:
    """Whether *path* exposes *secrets* in any bounded decoded layer.

    The original encoded path and each bounded percent-decoded layer are
    compared case-insensitively against every registered secret, mirroring the
    conservative hostname suppression rules so a secret-bearing path can never
    be disclosed.  Over-suppression is acceptable; disclosure is not.
    """
    if not secrets:
        return False
    layers = {path.lower()}
    current = path
    for _ in range(URL_DECODE_LAYER_LIMIT):
        if "%" not in current:
            break
        decoded = _decode_percent(current)
        if decoded is None or decoded == current:
            break
        current = decoded
        layers.add(current.lower())
    for secret in secrets:
        if not secret:
            continue
        lowered = secret.lower()
        for layer in layers:
            if lowered in layer or layer in lowered:
                return True
    return False


def _canonical_safe_path(path: str, secrets: tuple[str, ...]) -> str | None:
    """Return the canonical safe encoded path, or ``None`` when unsafe.

    Raw terminal controls, incomplete or invalid percent escapes, and
    secret-bearing content all fail closed.  An empty path becomes ``/``.
    Raw non-ASCII text is UTF-8 percent-encoded rather than kept active, so the
    result is always ASCII.  Confidentiality is checked on the original path,
    each bounded decoded layer, *and* the canonicalized result, since
    canonicalization can both create and erase a secret.
    """
    if any(ord(character) in _CONTROL_CODES for character in path):
        return None
    normalized = _normalize_percent_encoding(path)
    if normalized is None:
        return None
    if _path_contains_secret(path, secrets):
        return None
    canonical = _remove_dot_segments(normalized)
    if not canonical.startswith("/"):
        canonical = "/" + canonical
    # Recheck the canonical path: dot-segment removal and unreserved percent
    # decoding can *create* a secret that neither the original encoded path nor
    # an intermediate layer contained (`/sec/x/../ret` -> `/sec/ret`), so the
    # confidentiality check must also run after canonicalization.  The check on
    # the original path is retained because canonicalization can equally erase
    # a secret-bearing segment that must still trigger redaction.
    if _path_contains_secret(canonical, secrets):
        return None
    return canonical


def _safe_host_path(
    layer: str, hostname: str, secrets: tuple[str, ...]
) -> SafeHostPath:
    """Derive the bounded host/path fact for one safe URL layer.

    The path always fails closed to :data:`REDACTED_PATH_MARKER` rather than
    omitting the fact, so a safe host remains visible when only its path is
    unsafe.
    """
    try:
        raw_path = urllib.parse.urlsplit(layer).path
    except ValueError:
        return SafeHostPath(hostname, REDACTED_PATH_MARKER)
    canonical = _canonical_safe_path(raw_path, secrets)
    return SafeHostPath(hostname, canonical or REDACTED_PATH_MARKER)


def _explicit_authority_port(authority: str) -> int | None:
    """Return the port explicitly written in *authority*, or ``None``.

    The authority must already have any user information removed.  An absent
    port stays absent so an implicit default port is never synthesized, while
    an explicit default port stays explicit.  A trailing colon, an empty
    port, or a non-numeric port is malformed and yields ``None``.
    """
    if authority.startswith("["):
        end = authority.find("]")
        if end == -1:
            return None
        remainder = authority[end + 1 :]
        if not remainder.startswith(":") or not remainder[1:].isdigit():
            return None
        return int(remainder[1:])
    _, separator, tail = authority.rpartition(":")
    if not separator or not tail.isdigit():
        return None
    return int(tail)


def _canonical_url_identity_literal(text: str) -> str | None:
    """Build the fingerprint digest input for one literal URL layer.

    The input preserves the lowercased scheme, the normalized hostname, the
    path exactly as written, and the port exactly when one was explicitly
    supplied -- including an explicit default port.  User information, query,
    fragment, and every other authority detail are excluded by construction,
    so they cannot contribute to the fingerprint.  An absent port is never
    synthesized.
    """
    try:
        parsed = urllib.parse.urlsplit(text)
    except ValueError:
        return None
    if not parsed.scheme or not parsed.netloc:
        return None
    host = _normalize_hostname_text(parsed.hostname or "")
    if host is None:
        return None
    authority = parsed.netloc.rpartition("@")[2]
    explicit_port = _explicit_authority_port(authority)
    try:
        parsed.port
    except ValueError:
        return None
    return json.dumps(
        [parsed.scheme.lower(), host, explicit_port, parsed.path],
        ensure_ascii=True,
        separators=(",", ":"),
    )


def _canonical_url_identity(candidate: str) -> str | None:
    """Return the canonical fingerprint input for *candidate*.

    Percent-encoded schemes, separators, and hosts are recognized through the
    same bounded decoding passes used for URL detection, so an encoded URL
    yields the same fingerprint as its literal form.
    """
    current = candidate
    for _ in range(URL_DECODE_LAYER_LIMIT + 1):
        identity = _canonical_url_identity_literal(current)
        if identity is not None:
            return identity
        if "%" not in current:
            return None
        decoded = _decode_percent(current)
        if decoded is None or decoded == current:
            return None
        current = decoded
    return None


def _is_escape_complete(text: str) -> bool:
    """Whether every ``%`` in *text* starts a complete ``%XX`` escape.

    A trailing partial escape (``https://example.com/%`` or ``.../%6``) or an
    invalid one means the token may still be growing or is malformed, so it
    must never be finalized as a complete URL.
    """
    index = 0
    length = len(text)
    while index < length:
        if text[index] != "%":
            index += 1
            continue
        if index + 2 >= length:
            return False
        if _HEX_PAIR_RE.fullmatch(text[index + 1 : index + 3]) is None:
            return False
        index += 3
    return True


def _is_complete_url_literal(text: str) -> bool:
    """Whether *text* is one absolute URL with a complete, usable authority.

    The host must normalize safely through the same rules used for a
    projected hostname and the authority must carry either no port or a
    complete numeric one, so a trailing colon or a non-numeric port is
    malformed rather than merely incomplete.  The authority must also be
    dot-qualified or an IP literal: a single-label authority such as
    ``https://exam`` may still be a truncated hostname, so it stays
    unresolved instead of yielding a hostname fact.
    """
    try:
        parsed = urllib.parse.urlsplit(text)
    except ValueError:
        return False
    if not parsed.scheme or not parsed.netloc:
        return False
    if _AUTHORITY_RE.fullmatch(parsed.netloc.rpartition("@")[2]) is None:
        return False
    try:
        parsed.port
    except ValueError:
        return False
    host = _normalize_hostname_text(parsed.hostname or "")
    if host is None:
        return False
    if "." in host:
        return True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _is_complete_url_at_eof(token: str) -> bool:
    """Whether *token* is one complete URL ending exactly at a clean EOF.

    Only the leftmost URL start counts, and it must cover the whole token so
    no unparsed remainder is silently discarded.  Percent-encoded scheme
    characters and separators are recognized through the same bounded
    decoding passes used elsewhere, and every pass must be escape-complete so
    a partial escape is never finalized as complete.
    """
    if _find_terminator(token) is not None:
        return False
    if _find_url_start(token) != 0:
        return False
    current = token
    for _ in range(URL_DECODE_LAYER_LIMIT + 1):
        if _is_escape_complete(current) and _is_complete_url_literal(current):
            return True
        if "%" not in current:
            return False
        decoded, _ = _decode_percent_layer(current)
        if decoded == current:
            return False
        current = decoded
    return False


def _canonical_hosts_in_text(value: str) -> set[str]:
    """Return the canonical hostnames carried by one *value* layer.

    A full URL secret (``https://PROXY.EXAMPLE:8080/path``) is normalized
    through URL parsing.  A value without a ``scheme://`` marker is also
    normalized as an authority-relative reference, which covers bare host,
    credentials, port, and trailing-dot forms (``PROXY.EXAMPLE``,
    ``user:pass@PROXY.EXAMPLE:8080``, ``PROXY.EXAMPLE.``).  Every extracted
    host goes through the same :func:`_normalize_hostname_text` rules as a
    projected diagnostic hostname.
    """
    hosts: set[str] = set()
    direct = _normalized_host(value)
    if direct is not None:
        hosts.add(direct)
    if "://" not in value:
        authority = _normalized_host(f"//{value}")
        if authority is not None:
            hosts.add(authority)
    return hosts


def _secret_canonical_hosts(secret: str) -> frozenset[str]:
    """Return the canonical hostnames carried by *secret*.

    The secret and each bounded percent-decoded layer are canonicalized, using
    the same :func:`_decode_percent` pass and
    :data:`URL_DECODE_LAYER_LIMIT` bound applied to diagnostic URL candidates,
    so an encoded secret such as ``https://b%C3%BCcher.example:8080`` or
    ``https://%70roxy.example:8080`` still suppresses its decoded
    ``xn--bcher-kva.example`` / ``proxy.example`` host fact.  Decoding stops on
    failure, on no change, or at the layer limit -- it is never unbounded.  A
    secret that yields no parseable host is handled by the conservative
    fallback in :func:`_host_contains_secret`.
    """
    hosts = _canonical_hosts_in_text(secret)
    current = secret
    for _ in range(URL_DECODE_LAYER_LIMIT):
        if "%" not in current:
            break
        decoded = _decode_percent(current)
        if decoded is None or decoded == current:
            break
        current = decoded
        hosts |= _canonical_hosts_in_text(current)
    return frozenset(hosts)


def _host_contains_secret(host: str, secrets: tuple[str, ...]) -> bool:
    """Whether a normalized *host* must be withheld because of *secrets*.

    *host* is always compared in canonical form: the raw, case-sensitive
    ``host in secret or secret in host`` test is gone, because it missed a
    registered secret such as ``https://PROXY.EXAMPLE:8080`` and still emitted
    the ``proxy.example`` host fact.  Each secret contributes the canonical
    hostnames of its original value and of every bounded percent-decoded layer
    (see :func:`_secret_canonical_hosts`), using the same hostname
    normalization as diagnostic candidates.  A secret that yields no canonical
    hostname -- or embeds a host inside its path or query -- is still covered
    by a conservative case-insensitive containment fallback so a matching host
    fact can never be exposed.  Only hostname-fact suppression uses these
    rules; text redaction stays deterministic and case-sensitive.
    """
    for secret in secrets:
        if not secret:
            continue
        if host in _secret_canonical_hosts(secret):
            return True
        lowered = secret.lower()
        if host in lowered or lowered in host:
            return True
    return False


class DiagnosticProjector:
    """Incremental, boundary-safe URL/secret projector for host diagnostics.

    The projector owns a pending buffer whose retained UTF-8 byte length never
    exceeds :data:`PENDING_LIMIT_BYTES`.  It decodes UTF-8 incrementally, so
    multibyte characters split across chunks stay intact, and it withholds
    only the shortest ambiguous trailing suffix needed by secret overlap and
    partial URL-scheme detection.  It never buffers an entire unbounded token
    while waiting for a delimiter: an oversized candidate is replaced by
    :data:`OVERSIZED_TOKEN_MARKER` and its remainder is discarded through the
    next safe token boundary before normal processing resumes.
    """

    def __init__(
        self,
        secrets: Sequence[str] = (),
        *,
        url_identity: SessionUrlIdentity | None = None,
    ) -> None:
        if url_identity is not None and not isinstance(url_identity, SessionUrlIdentity):
            raise TypeError("url_identity must be a SessionUrlIdentity or None")
        self._secrets = dedupe_secrets(secrets)
        self._longest = longest_secret_length(self._secrets)
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._pending = ""
        self._overflow = False
        self._eof = False
        self._hostnames: list[str] = []
        self._host_paths: list[SafeHostPath] = []
        self._url_identity = url_identity
        self._fingerprints: list[str] = []

    # -- public surface -------------------------------------------------

    def feed_bytes(self, data: bytes | bytearray | memoryview) -> tuple[str, ...]:
        """Feed raw bytes and return newly projected URL-free text chunks."""
        self._reject_if_finished()
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("data must be bytes-like")
        if not data:
            return ()
        return self._feed(self._decoder.decode(bytes(data), final=False))

    def feed_text(self, text: str) -> tuple[str, ...]:
        """Feed already-decoded text and return newly projected safe chunks."""
        self._reject_if_finished()
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        return self._feed(text)

    def finish(self, *, abort: bool = False) -> tuple[str, ...]:
        """Flush the decoder and finalize any pending candidate.

        A pending URL or secret candidate is never flushed as ordinary text;
        it becomes exactly :data:`INCOMPLETE_TOKEN_MARKER`.  The one
        exception is a syntactically complete URL that ends exactly at a
        clean EOF: it is removed like any other URL and keeps its normalized
        hostname fact, because it can no longer grow into a different token.
        Only at clean EOF may a bare, still syntactically valid scheme prefix
        (``git``) be ordinary text, and it is then flushed unchanged.  When
        *abort* is set the stream ended through reader failure or
        cancellation, so every pending candidate -- including a complete URL
        -- fails closed and no hostname fact is emitted.
        """
        if self._eof:
            return ()
        self._eof = True
        chunks = list(self._feed(self._decoder.decode(b"", final=True)))
        if self._overflow:
            # The oversized candidate was already replaced and its remainder
            # discarded; nothing further is emitted for it.
            self._pending = ""
            self._overflow = False
            return tuple(chunks)
        if self._pending:
            if (
                not abort
                and not _secret_overlap_hold(
                    self._pending, self._secrets, self._longest
                )
                and _is_complete_url_at_eof(self._pending)
            ):
                chunks.append(self._sanitize_candidate(self._pending))
            elif _unresolved_candidate(
                self._pending, self._secrets, self._longest, abort=abort
            ):
                chunks.append(INCOMPLETE_TOKEN_MARKER)
            else:
                # Clean EOF: a bare scheme-like suffix (``git``) is ordinary
                # text because it never became a URL.
                chunks.append(self._pending)
            self._pending = ""
        return tuple(chunks)

    @property
    def hostnames(self) -> tuple[str, ...]:
        """Unconsumed normalized hostnames, in first-occurrence order.

        A caller that projects a single complete diagnostic and never drains
        facts sees every hostname exactly once (the historical behavior).  A
        long-running streaming consumer drains facts per diagnostic line via
        :meth:`take_facts`, so only the line currently being assembled keeps
        hostname metadata.
        """
        return tuple(self._hostnames)

    @property
    def url_fingerprints(self) -> tuple[str, ...]:
        """Unconsumed ordered URL fingerprints, with multiplicity preserved."""
        return tuple(self._fingerprints)

    @property
    def host_paths(self) -> tuple[SafeHostPath, ...]:
        """Unconsumed normalized host/path facts, in first-occurrence order.

        These are internal-presentation metadata only.  A streaming consumer
        drains them with :meth:`take_host_paths` so the projector never retains
        completed-line facts, and they are deliberately absent from the
        external SDK DTO and from every evidence, persistence, or semantic
        identity path.
        """
        return tuple(self._host_paths)

    def take_host_paths(self) -> tuple[SafeHostPath, ...]:
        """Return and clear the host/path facts extracted since the last call."""
        facts = tuple(self._host_paths)
        self._host_paths = []
        return facts

    def take_facts(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Return and clear the facts extracted since the previous call.

        Yields ``(hostnames, fingerprints)``.  Hostnames are deduplicated
        within the accumulated window; fingerprints keep their order and
        multiplicity.  Host/path facts are drained separately with
        :meth:`take_host_paths`.  Draining keeps a long-running consumer from
        retaining the metadata history of already-completed diagnostics, while
        a caller that never drains (such as :func:`sanitize_diagnostic_text`
        and :func:`project_structured_diagnostic`) still observes the complete
        set for one diagnostic.
        """
        facts = (tuple(self._hostnames), tuple(self._fingerprints))
        self._hostnames = []
        self._fingerprints = []
        return facts

    @property
    def pending_size(self) -> int:
        """Retained ambiguous pending state in UTF-8 bytes."""
        return len(self._pending.encode("utf-8"))

    # -- internals ------------------------------------------------------

    def _reject_if_finished(self) -> None:
        if self._eof:
            raise RuntimeError("projector is already finished")

    def _feed(self, text: str) -> tuple[str, ...]:
        """Append *text* within the retained-state bound.

        Input is consumed in UTF-8 byte-bounded slices and never appended past
        :data:`PENDING_LIMIT_BYTES`, so a multibyte slice cannot transiently
        exceed the limit.  When the next character cannot fit, the unresolved
        candidate is oversized: it is replaced by
        :data:`OVERSIZED_TOKEN_MARKER` and its remainder is discarded through
        the next safe boundary before processing resumes.
        """
        chunks: list[str] = []
        offset = 0
        length = len(text)
        while offset < length:
            room = PENDING_LIMIT_BYTES - len(self._pending.encode("utf-8"))
            if room <= 0:
                chunks.extend(self._begin_overflow())
                continue
            piece, offset = _take_utf8_prefix(
                text, offset, min(_FEED_SLICE_BYTES, room)
            )
            if not piece:
                # The remaining room cannot hold the next character, so the
                # retained candidate cannot grow without breaking the bound.
                chunks.extend(self._begin_overflow())
                continue
            self._pending += piece
            chunks.extend(self._drain())
        return tuple(chunks)

    def _begin_overflow(self) -> list[str]:
        """Replace an unretainable candidate and discard through a boundary."""
        self._pending = ""
        self._overflow = True
        return [OVERSIZED_TOKEN_MARKER, *self._drain()]

    def _drain(self) -> list[str]:
        chunks: list[str] = []
        while self._pending:
            if self._overflow:
                boundary = _find_terminator(self._pending)
                if boundary is None:
                    self._pending = ""
                    break
                self._pending = self._pending[boundary:]
                self._overflow = False
                continue

            secret = _leftmost_secret_match(self._pending, self._secrets)
            url_index = _find_url_start(self._pending)
            secret_index = secret[0] if secret is not None else None

            if url_index is not None and (
                secret_index is None or url_index <= secret_index
            ):
                if url_index:
                    chunks.append(self._pending[:url_index])
                    self._pending = self._pending[url_index:]
                    continue
                boundary = _find_terminator(self._pending)
                if boundary is None:
                    break
                chunks.append(self._sanitize_candidate(self._pending[:boundary]))
                self._pending = self._pending[boundary:]
                continue

            if secret is not None:
                assert secret_index is not None
                if secret_index:
                    chunks.append(self._pending[:secret_index])
                    self._pending = self._pending[secret_index:]
                    continue
                chunks.append(REDACTED)
                self._pending = self._pending[secret[1] :]
                continue

            hold = _ambiguous_hold(self._pending, self._secrets, self._longest)
            if hold:
                emit = self._pending[:-hold]
                if emit:
                    chunks.append(emit)
                self._pending = self._pending[-hold:]
                break

            chunks.append(self._pending)
            self._pending = ""
        return chunks

    def _sanitize_candidate(self, candidate: str) -> str:
        layer = _candidate_url_layer(candidate)
        host = _normalized_host(layer) if layer is not None else None
        if (
            layer is not None
            and host is not None
            and not _host_contains_secret(host, self._secrets)
        ):
            if host not in self._hostnames:
                self._hostnames.append(host)
            fact = _safe_host_path(layer, host, self._secrets)
            if fact not in self._host_paths:
                self._host_paths.append(fact)
            if self._url_identity is not None:
                digest_input = _canonical_url_identity(candidate)
                if digest_input is not None:
                    self._fingerprints.append(
                        self._url_identity.fingerprint(digest_input)
                    )
        return REDACTED


def sanitize_diagnostic_text(
    text: str, secrets: Sequence[str] = ()
) -> tuple[str, tuple[str, ...]]:
    """Return ``(url_free_text, normalized_hostnames)`` for complete *text*.

    Pure and output-policy independent: identical inputs always produce
    identical output-policy-independent results.
    """
    projector = DiagnosticProjector(secrets)
    chunks = list(projector.feed_text(text))
    chunks.extend(projector.finish())
    return "".join(chunks), projector.hostnames


def sanitize_host_paths(
    text: str, secrets: Sequence[str] = ()
) -> tuple[SafeHostPath, ...]:
    """Return the ordered internal host/path facts for complete *text*.

    Pure and output-policy independent, like :func:`sanitize_diagnostic_text`.
    The result is local-presentation metadata only and is deliberately not part
    of any external SDK event, evidence, persistence, or semantic identity.
    """
    projector = DiagnosticProjector(secrets)
    chunks = list(projector.feed_text(text))
    chunks.extend(projector.finish())
    return projector.host_paths


def project_structured_diagnostic(
    *,
    phase: HostPhase,
    step: HostStep,
    stream: HostDiagnosticStream,
    classification: HostDiagnosticClassification,
    text: str,
    secrets: Sequence[str] = (),
    logical_resource: DiagnosticLogicalResource | None = None,
    url_identity: SessionUrlIdentity | None = None,
) -> HostStructuredDiagnostic:
    """Project *text* into one structured, URL-free safe diagnostic.

    Accepts only a validated :class:`DiagnosticLogicalResource` (or ``None``);
    raw resource labels are rejected so an arbitrary label cannot enter the
    event.  No output policy is accepted or consulted. When *url_identity* is
    supplied, every removed complete URL contributes an ordered ephemeral
    fingerprint for presentation identity only.
    """
    if logical_resource is not None and not isinstance(
        logical_resource, DiagnosticLogicalResource
    ):
        raise TypeError(
            "logical_resource must be a validated DiagnosticLogicalResource"
        )
    projector = DiagnosticProjector(secrets, url_identity=url_identity)
    chunks = list(projector.feed_text(text))
    chunks.extend(projector.finish())
    return HostStructuredDiagnostic(
        phase=phase,
        step=step,
        stream=stream,
        classification=classification,
        text="".join(chunks),
        hostnames=projector.hostnames,
        logical_resource=(
            logical_resource.name if logical_resource is not None else None
        ),
        url_fingerprints=projector.url_fingerprints,
    )


def _read_exception_relationship(
    current: BaseException, attribute: str
) -> BaseException | None:
    """Read one exception relationship without letting it break projection.

    Hostile exception objects may raise from attribute access or expose a
    non-exception value.  Either outcome yields ``None`` so traversal falls
    through to the next relationship instead of replacing the original
    acquisition failure.  The value and any caught exception are never
    converted to a string.
    """
    try:
        value = getattr(current, attribute)
    except BaseException:
        return None
    return value if isinstance(value, BaseException) else None


def project_exception_type_chain(
    reason: BaseException | None, *, limit: int = EXCEPTION_TYPE_LIMIT
) -> tuple[str, ...]:
    """Return at most *limit* unique exception type names for *reason*.

    Traversal is deterministic.  At each step the next exception is selected in
    this order: an exception-valued ``reason`` attribute (for example the
    ``URLError.reason`` that wraps the underlying transport failure), then
    ``__cause__`` when present, otherwise ``__context__``.  A non-exception
    ``reason`` value is ignored without ever being converted to a string.  A
    relationship read that raises (hostile exception object) is treated the
    same way and traversal continues to the next relationship, so projection
    stays non-throwing and the original acquisition failure is preserved.
    Object identity terminates cycles and duplicate class names are suppressed
    while traversal continues, so the result is a bounded chain of unique
    types.  Exception messages are never evaluated (``type(reason).__name__``
    is the only attribute read besides the relationship links).

    *limit* may lower the size of the returned chain but never raise it above
    :data:`EXCEPTION_TYPE_LIMIT`; requesting more is a validation error so the
    "at most four unique names" contract holds for every caller.
    """
    if reason is not None and not isinstance(reason, BaseException):
        raise TypeError("reason must be an exception or None")
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or limit <= 0
        or limit > EXCEPTION_TYPE_LIMIT
    ):
        raise ValueError(
            "limit must be a positive integer no greater than "
            f"EXCEPTION_TYPE_LIMIT ({EXCEPTION_TYPE_LIMIT})"
        )

    names: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = reason
    while current is not None and len(names) < limit:
        marker = id(current)
        if marker in seen:
            break
        seen.add(marker)
        name = type(current).__name__
        if name not in names:
            names.append(name)
        nested = _read_exception_relationship(current, "reason")
        if nested is None:
            nested = _read_exception_relationship(current, "__cause__")
        if nested is None:
            nested = _read_exception_relationship(current, "__context__")
        current = nested
    return tuple(names)


def project_host_acquisition_failure(
    *,
    phase: HostPhase,
    step: HostStep,
    logical_resource: DiagnosticLogicalResource,
    reason: BaseException | None,
    url: str | None = None,
    secrets: Sequence[str] = (),
) -> HostStructuredDiagnostic:
    """Project one host acquisition failure into a safe structured diagnostic.

    The reviewed logical asset name identifies the failure. The bounded
    exception-type chain is built from *reason* without evaluating any
    exception message. The optional *url* is passed through the shared URL
    projection: it is removed from the text and only its normalized hostname
    survives as a host fact. *secrets* are redacted first as defense in depth.
    """
    if not isinstance(logical_resource, DiagnosticLogicalResource):
        raise TypeError(
            "logical_resource must be a validated DiagnosticLogicalResource"
        )
    chain = project_exception_type_chain(reason)
    detail = "acquisition failed"
    if chain:
        detail += " (" + " -> ".join(chain) + ")"
    text = f"{logical_resource.name}: {detail}"
    if url is not None:
        text += f" {url}"
    return project_structured_diagnostic(
        phase=phase,
        step=step,
        stream=HostDiagnosticStream.STDERR,
        classification=HostDiagnosticClassification.ERROR,
        text=text,
        secrets=secrets,
        logical_resource=logical_resource,
    )


__all__ = [
    "EXCEPTION_TYPE_LIMIT",
    "INCOMPLETE_TOKEN_MARKER",
    "OVERSIZED_TOKEN_MARKER",
    "PENDING_LIMIT_BYTES",
    "PI_RELEASE_ASSET_NAMES",
    "REDACTED_PATH_MARKER",
    "REVIEWED_ARTIFACT_NAMES",
    "DiagnosticLogicalResource",
    "DiagnosticProjector",
    "DiagnosticResourceKind",
    "SafeHostPath",
    "SessionUrlIdentity",
    "normalized_url_host",
    "project_exception_type_chain",
    "project_host_acquisition_failure",
    "project_structured_diagnostic",
    "sanitize_diagnostic_text",
    "sanitize_host_paths",
]
