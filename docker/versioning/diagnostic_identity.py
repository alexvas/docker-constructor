"""Ephemeral URL identity and pure diagnostic coalescing.

Phase 7 of ``improve-host-build-observability`` owns two pure,
presentation-independent concerns:

* a fresh per-presentation-session keyed URL fingerprint that distinguishes
  diagnostics which render the same URL-free safe text but refer to different
  hidden URLs, while remaining opaque, fixed-width, and non-presentable; and
* the exact-repeat / conservative numeric-variant matcher and mode-independent
  group state transitions that the facade presentation worker (Phase 9)
  drives.

Nothing here renders, schedules timers, owns a mailbox, reads an output
policy, or touches transport, Docker, or the filesystem.  Session keys and
fingerprints never enter rendered text, retained tails, failures, evidence,
persistence, or policy identity.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from enum import StrEnum

from docker.versioning.host_progress import (
    URL_FINGERPRINT_LENGTH,
    HostDiagnosticClassification,
    HostDiagnosticStream,
    HostPhase,
    HostStep,
    HostStructuredDiagnostic,
    require_approved_url_fingerprints,
)
from docker.versioning.logical_resource import require_approved_logical_resource

#: Length of a fresh presentation-session key in bytes.
SESSION_KEY_BYTES = 32

#: Minimum accepted explicitly supplied test key length in bytes.
MINIMUM_SESSION_KEY_BYTES = 16

#: Canonical suffix appended to a finalized repeated diagnostic.
REPETITION_SUFFIX_TEMPLATE = " (repeated {count} times)"


class SessionUrlIdentity:
    """Fresh cryptographic key for one presentation session's URL fingerprints.

    Unless a test supplies an explicit *key*, the key is generated with
    :func:`secrets.token_bytes`.  The fingerprint is an HMAC-SHA256 over the
    canonical digest input, so it is not an unkeyed fast/stable hash of a
    predictable or secret-bearing URL and cannot be recomputed offline.  A new
    session always gets a fresh key, so identical URLs never correlate across
    sessions.
    """

    def __init__(self, key: bytes | None = None) -> None:
        if key is None:
            key = secrets.token_bytes(SESSION_KEY_BYTES)
        if not isinstance(key, (bytes, bytearray)) or isinstance(key, bool):
            raise TypeError("URL identity keys must be bytes")
        if len(key) < MINIMUM_SESSION_KEY_BYTES:
            raise ValueError(
                f"URL identity keys must be at least {MINIMUM_SESSION_KEY_BYTES} bytes"
            )
        self._key = bytes(key)

    def fingerprint(self, digest_input: str) -> str:
        """Return the fixed-width keyed fingerprint of canonical *digest_input*.

        *digest_input* is the already-normalized scheme/hostname/path/explicit
        port tuple built by the diagnostic projector; this method never
        parses a URL, accepts no output policy, and reveals no key material.
        """
        if not isinstance(digest_input, str):
            raise TypeError("digest input must be a string")
        return hmac.new(
            self._key, digest_input.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def __repr__(self) -> str:  # noqa: D105 - deliberately key-free
        return "SessionUrlIdentity(<ephemeral key>)"

    __str__ = __repr__


@dataclass(frozen=True)
class DiagnosticIdentity:
    """Pure presentation identity for one structured safe diagnostic.

    The identity carries the closed presentation metadata, the final safe
    rendered text, and the ordered ephemeral URL-fingerprint tuple.  Normalized
    hostnames are intentionally absent: they are a facade hostname-display
    input, not a post-render coalescing-key component.
    """

    phase: HostPhase
    step: HostStep
    stream: HostDiagnosticStream
    classification: HostDiagnosticClassification
    text: str
    url_fingerprints: tuple[str, ...] = ()
    logical_resource: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.phase, HostPhase):
            raise TypeError("phase must be a HostPhase member")
        if not isinstance(self.step, HostStep):
            raise TypeError("step must be a HostStep member")
        if not isinstance(self.stream, HostDiagnosticStream):
            raise TypeError("stream must be a HostDiagnosticStream member")
        if not isinstance(self.classification, HostDiagnosticClassification):
            raise TypeError("classification must be a HostDiagnosticClassification member")
        if not isinstance(self.text, str):
            raise TypeError("diagnostic identity text must be a string")
        require_approved_url_fingerprints(self.url_fingerprints)
        require_approved_logical_resource(self.logical_resource, "logical_resource")

    @property
    def presentation_metadata(self) -> tuple[object, ...]:
        """Identity components compared before text or numeric grouping."""
        return (
            self.phase,
            self.step,
            self.stream,
            self.classification,
            self.logical_resource,
            self.url_fingerprints,
        )


def identity_for(diagnostic: HostStructuredDiagnostic) -> DiagnosticIdentity:
    """Return the pure presentation identity for *diagnostic*.

    The normalized ``hostnames`` tuple is deliberately dropped; only the final
    safe text and ordered fingerprint tuple participate in grouping.
    """
    return DiagnosticIdentity(
        phase=diagnostic.phase,
        step=diagnostic.step,
        stream=diagnostic.stream,
        classification=diagnostic.classification,
        text=diagnostic.text,
        url_fingerprints=diagnostic.url_fingerprints,
        logical_resource=diagnostic.logical_resource,
    )


#: Maximal run of ASCII digits and dots; the whole run must be valid before a
#: token is accepted, so no valid-looking substring is extracted from
#: ``1..2``, ``1.``, or ``.1``.
_NUMERIC_RUN_RE = re.compile(r"[0-9.]+")

#: One valid numeric token: digits followed by zero or more dot-separated
#: digit groups.
_STRICT_NUMERIC_TOKEN_RE = re.compile(r"[0-9]+(?:\.[0-9]+)*")

#: Characters that, when adjacent to a numeric run, make it identifier
#: embedded, signed, or otherwise not a standalone numeric token.
_NUMERIC_BOUNDARY_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.+-"
)


def strict_numeric_spans(text: str) -> tuple[tuple[int, int], ...]:
    """Return the spans of valid standalone numeric tokens in *text*.

    A valid token is a maximal ``[0-9.]+`` run that fully matches
    ``digits ( "." digits )*`` and whose adjacent characters are not ASCII
    letters, digits, underscores, dots, plus, or minus.
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    spans: list[tuple[int, int]] = []
    for match in _NUMERIC_RUN_RE.finditer(text):
        value = match.group()
        if _STRICT_NUMERIC_TOKEN_RE.fullmatch(value) is None:
            continue
        start, end = match.span()
        before = text[start - 1] if start > 0 else ""
        after = text[end] if end < len(text) else ""
        if before in _NUMERIC_BOUNDARY_CHARS or after in _NUMERIC_BOUNDARY_CHARS:
            continue
        spans.append((start, end))
    return tuple(spans)


def strict_numeric_tokens(text: str) -> tuple[str, ...]:
    """Return the valid standalone numeric tokens of *text*, in order."""
    return tuple(text[start:end] for start, end in strict_numeric_spans(text))


def _numeric_partition(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    spans = strict_numeric_spans(text)
    parts: list[str] = []
    tokens: list[str] = []
    cursor = 0
    for start, end in spans:
        parts.append(text[cursor:start])
        tokens.append(text[start:end])
        cursor = end
    parts.append(text[cursor:])
    return tuple(parts), tuple(tokens)


def numeric_template_match(left: str, right: str) -> bool:
    """Whether *left* and *right* differ in exactly one numeric token.

    Both texts must carry the same number of valid numeric tokens, every
    inter-token nonnumeric segment must be byte-identical, and exactly one
    token must change.  Monotonicity is never required.
    """
    left_parts, left_tokens = _numeric_partition(left)
    right_parts, right_tokens = _numeric_partition(right)
    if not left_tokens or len(left_tokens) != len(right_tokens):
        return False
    if left_parts != right_parts:
        return False
    changed = sum(1 for a, b in zip(left_tokens, right_tokens) if a != b)
    return changed == 1


class DiagnosticComparison(StrEnum):
    """How a candidate diagnostic relates to the current presentation group."""

    DIFFERENT = "different"
    EXACT_REPEAT = "exact_repeat"
    NUMERIC_VARIANT = "numeric_variant"


def compare_diagnostics(
    current: DiagnosticIdentity, candidate: DiagnosticIdentity
) -> DiagnosticComparison:
    """Classify *candidate* against *current* using the shared identity rules."""
    if not isinstance(current, DiagnosticIdentity) or not isinstance(
        candidate, DiagnosticIdentity
    ):
        raise TypeError("comparison requires DiagnosticIdentity values")
    if current.presentation_metadata != candidate.presentation_metadata:
        return DiagnosticComparison.DIFFERENT
    if current.text == candidate.text:
        return DiagnosticComparison.EXACT_REPEAT
    if numeric_template_match(current.text, candidate.text):
        return DiagnosticComparison.NUMERIC_VARIANT
    return DiagnosticComparison.DIFFERENT


def format_repetition(text: str, count: int) -> str:
    """Return *text* with the canonical suffix for an admitted total *count*."""
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise ValueError("count must be a positive integer")
    if count < 2:
        return text
    return text + REPETITION_SUFFIX_TEMPLATE.format(count=count)


@dataclass
class DiagnosticGroup:
    """Mutable state for one admitted diagnostic group."""

    identity: DiagnosticIdentity
    exact_count: int = 1

    @property
    def text(self) -> str:
        return self.identity.text

    def final_text(self) -> str:
        return format_repetition(self.text, self.exact_count)


class PresentationMode(StrEnum):
    """Closed heartbeat presentation modes that affect grouping output."""

    INTERACTIVE = "interactive"
    LINES = "lines"


class DiagnosticDisposition(StrEnum):
    """What the coalescer decided for one newly admitted diagnostic."""

    NEW_GROUP = "new_group"
    EXACT_REPEAT = "exact_repeat"
    NUMERIC_VARIANT = "numeric_variant"


@dataclass(frozen=True)
class AdmissionDecision:
    """Pure description of one admitted diagnostic for the facade worker.

    ``finalized_text`` is a durable write that must precede the new item (the
    previous group's latest admitted value or canonical summary).
    ``durable_text`` is a durable write for the new item in `lines` mode;
    ``slot_text`` is the mutable interactive slot content.  Neither field is a
    renderer call.
    """

    disposition: DiagnosticDisposition
    finalized_text: str | None = None
    durable_text: str | None = None
    slot_text: str | None = None
    admitted_count: int = 1


class DiagnosticCoalescer:
    """Pure per-session group state driven by the later facade worker.

    The optional *mode* selects only how an admitted diagnostic is emitted:
    ``INTERACTIVE`` keeps one mutable slot and an exact-repeat count, while
    ``LINES`` emits each changed numeric value as its own durable line and
    relies on the worker's one-second window for exact-repeat summaries.  This
    class owns no clock, mailbox, renderer, or thread.
    """

    def __init__(self, mode: PresentationMode = PresentationMode.INTERACTIVE) -> None:
        if not isinstance(mode, PresentationMode):
            raise TypeError("mode must be a PresentationMode member")
        self._mode = mode
        self._group: DiagnosticGroup | None = None

    @property
    def mode(self) -> PresentationMode:
        return self._mode

    @property
    def group(self) -> DiagnosticGroup | None:
        return self._group

    def admit(self, diagnostic: HostStructuredDiagnostic) -> AdmissionDecision:
        """Admit *diagnostic* and return the pure state transition."""
        candidate = identity_for(diagnostic)
        if self._group is None:
            self._group = DiagnosticGroup(candidate)
            return self._new_group_decision()
        comparison = compare_diagnostics(self._group.identity, candidate)
        if comparison is DiagnosticComparison.EXACT_REPEAT:
            self._group.exact_count += 1
            return self._exact_repeat_decision()
        if (
            comparison is DiagnosticComparison.NUMERIC_VARIANT
            and self._mode is PresentationMode.INTERACTIVE
        ):
            self._group = DiagnosticGroup(candidate)
            return AdmissionDecision(
                disposition=DiagnosticDisposition.NUMERIC_VARIANT,
                slot_text=candidate.text,
                admitted_count=1,
            )
        finalized = self._finalize_current()
        self._group = DiagnosticGroup(candidate)
        decision = self._new_group_decision()
        return AdmissionDecision(
            disposition=decision.disposition,
            finalized_text=finalized,
            durable_text=decision.durable_text,
            slot_text=decision.slot_text,
            admitted_count=decision.admitted_count,
        )

    def finalize(self) -> str | None:
        """Finalize the current group and clear it for a later new group."""
        finalized = self._finalize_current()
        self._group = None
        return finalized

    def _new_group_decision(self) -> AdmissionDecision:
        assert self._group is not None
        if self._mode is PresentationMode.INTERACTIVE:
            return AdmissionDecision(
                disposition=DiagnosticDisposition.NEW_GROUP,
                slot_text=self._group.text,
                admitted_count=1,
            )
        return AdmissionDecision(
            disposition=DiagnosticDisposition.NEW_GROUP,
            durable_text=self._group.text,
            admitted_count=1,
        )

    def _exact_repeat_decision(self) -> AdmissionDecision:
        assert self._group is not None
        if self._mode is PresentationMode.INTERACTIVE:
            return AdmissionDecision(
                disposition=DiagnosticDisposition.EXACT_REPEAT,
                slot_text=self._group.final_text(),
                admitted_count=self._group.exact_count,
            )
        return AdmissionDecision(
            disposition=DiagnosticDisposition.EXACT_REPEAT,
            admitted_count=self._group.exact_count,
        )

    def _finalize_current(self) -> str | None:
        if self._group is None:
            return None
        if self._mode is PresentationMode.INTERACTIVE:
            return self._group.final_text()
        if self._group.exact_count >= 2:
            return self._group.final_text()
        return None


__all__ = [
    "AdmissionDecision",
    "DiagnosticCoalescer",
    "DiagnosticComparison",
    "DiagnosticDisposition",
    "DiagnosticGroup",
    "DiagnosticIdentity",
    "PresentationMode",
    "SESSION_KEY_BYTES",
    "SessionUrlIdentity",
    "URL_FINGERPRINT_LENGTH",
    "compare_diagnostics",
    "format_repetition",
    "identity_for",
    "numeric_template_match",
    "strict_numeric_spans",
    "strict_numeric_tokens",
]
