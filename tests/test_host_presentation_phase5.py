"""Phase 5 policy-aware live presentation contracts.

These tests bind the RED deliverables for tasks 5.1--5.5 of
``add-configurable-network-url-display``: interactive ``redacted`` and
``host-path`` request-count grouping, ordered boundary finalization, the
``lines`` one-second request-count window, and exact unaggregated presentation.

They construct the internal :class:`HostDiagnosticEnvelope` directly (the
collection-to-presentation interface) while asserting that the external
:class:`HostStructuredDiagnostic` contract stays unchanged.  Terminal
rendering is observed through a recording renderer so the exact ordered call
sequence is visible.
"""
from __future__ import annotations

import dataclasses
import io
import unittest

from docker.versioning.diagnostic_identity import (
    DiagnosticDisposition,
    PresentationMode,
)
from docker.versioning.fetch_identity import FetchGroupKey
from docker.versioning.host_presentation import (
    LINES_WINDOW_SECONDS,
    HostPresentationMode,
    HostPresentationState,
    TerminalHostRenderer,
)
from docker.versioning.host_progress import (
    HostBuildEvent,
    HostDiagnosticClassification,
    HostDiagnosticEnvelope,
    HostDiagnosticPrefix,
    HostDiagnosticStream,
    HostHeartbeatEvent,
    HostPhase,
    HostPhaseEvent,
    HostPhaseState,
    HostStep,
    HostStepEvent,
    HostStepState,
    HostStructuredDiagnostic,
)
from docker.versioning.model import NetworkUrlDisplay
from docker.versioning.npm_fetch import (
    FetchGroupKey as ReExportedFetchGroupKey,
    canonical_fetch_text,
    fetch_group_identity,
    parse_npm_fetch_line,
)

RESEARCH_URL = (
    "https://registry.npmjs.org/npm-http-research-fixture/-/"
    "npm-http-research-fixture-1.0.0.tgz"
)
RESEARCH_SAFE = (
    "registry.npmjs.org/npm-http-research-fixture/-/"
    "npm-http-research-fixture-1.0.0.tgz"
)
OTHER_URL = "https://registry.npmjs.org/other-pkg/-/other-pkg-2.0.0.tgz"
OTHER_SAFE = "registry.npmjs.org/other-pkg/-/other-pkg-2.0.0.tgz"

REDACTED_CANON = "npm http fetch GET 200 <redacted> (cache miss)"
HOST_PATH_CANON = f"npm http fetch GET 200 {RESEARCH_SAFE} (cache miss)"
REQUEST_SEPARATOR = " — "


def _diagnostic(text: str, **overrides: object) -> HostStructuredDiagnostic:
    values: dict[str, object] = {
        "phase": HostPhase.LOCKED_ASSEMBLY,
        "step": HostStep.NPM_EXECUTION,
        "stream": HostDiagnosticStream.STDOUT,
        "classification": HostDiagnosticClassification.STATUS,
        "text": text,
    }
    values.update(overrides)
    return HostStructuredDiagnostic(**values)  # type: ignore[arg-type]


def _fetch_line(
    url: str = RESEARCH_URL,
    *,
    latency: int = 15,
    method: str = "GET",
    status: int = 200,
    attempt: int | None = None,
    cache: str | None = "miss",
) -> str:
    body = f"npm http fetch {method} {status} {url} {latency}ms"
    if attempt is not None:
        body += f" attempt #{attempt}"
    if cache is not None:
        body += f" (cache {cache})"
    return body


def _fetch_envelope(
    line: str,
    display: NetworkUrlDisplay,
    *,
    hostnames: tuple[str, ...] = (),
) -> HostDiagnosticEnvelope:
    record = parse_npm_fetch_line(line)
    assert record is not None, line
    key = fetch_group_identity(record, display)
    canonical = canonical_fetch_text(record, display)
    assert key is not None and canonical is not None
    external = _diagnostic(canonical, hostnames=hostnames)
    return HostDiagnosticEnvelope.for_diagnostic(
        external, text=canonical, fetch_key=key, fetch_text=canonical
    )


def _source_envelope(text: str) -> HostDiagnosticEnvelope:
    external = _diagnostic("<redacted>")
    return HostDiagnosticEnvelope.for_diagnostic(external, text=text)


def _canonical(display: NetworkUrlDisplay) -> str:
    if display is NetworkUrlDisplay.HOST_PATH:
        return HOST_PATH_CANON
    return REDACTED_CANON


def _heartbeat() -> HostHeartbeatEvent:
    return HostHeartbeatEvent(
        phase=HostPhase.LOCKED_ASSEMBLY,
        step=HostStep.NPM_EXECUTION,
        elapsed_seconds=1,
        expects_diagnostic_stream=True,
    )


class RecordingRenderer:
    def __init__(self, *, fail_on: str | None = None) -> None:
        self.calls: list[tuple[object, ...]] = []
        self._fail_on = fail_on

    def _record(self, *call: object) -> None:
        self.calls.append(call)
        if self._fail_on is not None and call[0] == self._fail_on:
            raise RuntimeError("renderer failed")

    def set_status(self, text: str) -> None:
        self._record("status", text)

    def set_slot(self, text: str) -> None:
        self._record("slot", text)

    def clear_slot(self) -> None:
        self._record("clear_slot")

    def clear_status(self) -> None:
        self._record("clear_status")

    def clear_all(self) -> None:
        self._record("clear_all")

    def durable(self, text: str) -> None:
        self._record("durable", text)

    def finalize_diagnostic(self, text: str, *, restore_status: str | None) -> None:
        self._record("finalize", text)

    def slot_texts(self) -> list[str]:
        return [call[1] for call in self.calls if call[0] == "slot"]  # type: ignore[misc]

    def durable_texts(self) -> list[str]:
        return [call[1] for call in self.calls if call[0] == "durable"]  # type: ignore[misc]

    def finalize_texts(self) -> list[str]:
        return [call[1] for call in self.calls if call[0] == "finalize"]  # type: ignore[misc]


def _state(
    renderer: RecordingRenderer,
    mode: HostPresentationMode = HostPresentationMode.INTERACTIVE,
    display: NetworkUrlDisplay = NetworkUrlDisplay.REDACTED,
) -> HostPresentationState:
    return HostPresentationState(renderer, mode=mode, network_url_display=display)


class TestInteractiveRedactedFetchGrouping(unittest.TestCase):
    """Task 5.1: first canonical slot, in-place ``— N requests`` totals."""

    def test_first_fetch_shows_canonical_line_immediately(self):
        renderer = RecordingRenderer()
        _state(renderer).admit_diagnostic(_fetch_envelope(_fetch_line(), NetworkUrlDisplay.REDACTED), now=0.0)
        self.assertEqual([REDACTED_CANON], renderer.slot_texts())

    def test_later_members_replace_slot_in_place_with_admitted_total(self):
        renderer = RecordingRenderer()
        state = _state(renderer)
        state.admit_diagnostic(_fetch_envelope(_fetch_line(latency=15), NetworkUrlDisplay.REDACTED), now=0.0)
        state.admit_diagnostic(_fetch_envelope(_fetch_line(latency=25), NetworkUrlDisplay.REDACTED), now=0.1)
        state.admit_diagnostic(_fetch_envelope(_fetch_line(latency=35), NetworkUrlDisplay.REDACTED), now=0.2)
        self.assertEqual(
            [
                REDACTED_CANON,
                f"{REDACTED_CANON}{REQUEST_SEPARATOR}2 requests",
                f"{REDACTED_CANON}{REQUEST_SEPARATOR}3 requests",
            ],
            renderer.slot_texts(),
        )

    def test_admitted_total_includes_the_first_request(self):
        renderer = RecordingRenderer()
        state = _state(renderer)
        state.admit_diagnostic(_fetch_envelope(_fetch_line(), NetworkUrlDisplay.REDACTED), now=0.0)
        state.admit_diagnostic(_fetch_envelope(_fetch_line(latency=99), NetworkUrlDisplay.REDACTED), now=0.1)
        self.assertEqual(f"{REDACTED_CANON}{REQUEST_SEPARATOR}2 requests", renderer.slot_texts()[-1])

    def test_no_observed_qualifier_or_durable_duplicates(self):
        renderer = RecordingRenderer()
        state = _state(renderer)
        for index in range(4):
            state.admit_diagnostic(
                _fetch_envelope(_fetch_line(latency=10 + index), NetworkUrlDisplay.REDACTED),
                now=float(index),
            )
        self.assertNotIn("observed", " ".join(renderer.slot_texts()))
        self.assertEqual([], renderer.durable_texts())
        self.assertEqual([], renderer.finalize_texts())

    def test_external_structured_diagnostic_shape_is_unchanged(self):
        field_names = {field.name for field in dataclasses.fields(HostStructuredDiagnostic)}
        self.assertEqual(
            {
                "phase",
                "step",
                "stream",
                "classification",
                "text",
                "hostnames",
                "logical_resource",
                "url_fingerprints",
            },
            field_names,
        )
        self.assertNotIn("fetch_key", field_names)
        self.assertNotIn("fetch_text", field_names)
        self.assertNotIn("path", field_names)
        self.assertFalse(hasattr(_diagnostic("x"), "fetch_key"))

    def test_internal_envelope_is_absent_from_the_external_event_union(self):
        envelope = _fetch_envelope(_fetch_line(), NetworkUrlDisplay.REDACTED)
        self.assertFalse(isinstance(envelope, HostBuildEvent))

    def test_npm_parser_still_reexports_the_identity_type(self):
        self.assertIs(ReExportedFetchGroupKey, FetchGroupKey)


class TestInteractiveHostPathFetchGrouping(unittest.TestCase):
    """Task 5.2: host/path grouping and delimiter separation."""

    def test_latency_variants_for_one_host_path_aggregate(self):
        renderer = RecordingRenderer()
        state = _state(renderer, display=NetworkUrlDisplay.HOST_PATH)
        state.admit_diagnostic(_fetch_envelope(_fetch_line(latency=15), NetworkUrlDisplay.HOST_PATH), now=0.0)
        state.admit_diagnostic(_fetch_envelope(_fetch_line(latency=42), NetworkUrlDisplay.HOST_PATH), now=0.1)
        self.assertEqual(
            [HOST_PATH_CANON, f"{HOST_PATH_CANON}{REQUEST_SEPARATOR}2 requests"],
            renderer.slot_texts(),
        )

    def test_canonical_text_bypasses_legacy_bracketed_hostname_attachment(self):
        renderer = RecordingRenderer()
        state = _state(renderer, display=NetworkUrlDisplay.HOST_PATH)
        state.admit_diagnostic(
            _fetch_envelope(
                _fetch_line(),
                NetworkUrlDisplay.HOST_PATH,
                hostnames=("registry.npmjs.org",),
            ),
            now=0.0,
        )
        self.assertEqual([HOST_PATH_CANON], renderer.slot_texts())
        self.assertNotIn("[registry.npmjs.org]", renderer.slot_texts()[0])

    def test_different_group_key_fields_delimit_groups(self):
        base = _fetch_line(url=RESEARCH_URL, latency=15)
        base_canonical = canonical_fetch_text(
            parse_npm_fetch_line(base), NetworkUrlDisplay.HOST_PATH
        )
        variants = {
            "hostname/path": _fetch_line(url=OTHER_URL, latency=15),
            "attempt": _fetch_line(url=RESEARCH_URL, latency=15, attempt=2),
            "method": _fetch_line(url=RESEARCH_URL, latency=15, method="HEAD"),
            "status": _fetch_line(url=RESEARCH_URL, latency=15, status=201),
            "cache": _fetch_line(url=RESEARCH_URL, latency=15, cache="hit"),
        }
        for name, line in variants.items():
            with self.subTest(field=name):
                expected = canonical_fetch_text(
                    parse_npm_fetch_line(line), NetworkUrlDisplay.HOST_PATH
                )
                renderer = RecordingRenderer()
                state = _state(renderer, display=NetworkUrlDisplay.HOST_PATH)
                state.admit_diagnostic(
                    _fetch_envelope(base, NetworkUrlDisplay.HOST_PATH), now=0.0
                )
                state.admit_diagnostic(
                    _fetch_envelope(line, NetworkUrlDisplay.HOST_PATH), now=0.1
                )
                self.assertEqual([base_canonical], renderer.finalize_texts())
                self.assertEqual(expected, renderer.slot_texts()[-1])

    def test_redacted_ignores_hostname_and_path_while_host_path_delimits(self):
        renderer = RecordingRenderer()
        redacted = _state(renderer, display=NetworkUrlDisplay.REDACTED)
        redacted.admit_diagnostic(_fetch_envelope(_fetch_line(), NetworkUrlDisplay.REDACTED), now=0.0)
        redacted.admit_diagnostic(
            _fetch_envelope(_fetch_line(url=OTHER_URL), NetworkUrlDisplay.REDACTED), now=0.1
        )
        self.assertEqual(
            f"{REDACTED_CANON}{REQUEST_SEPARATOR}2 requests", renderer.slot_texts()[-1]
        )

        other = RecordingRenderer()
        host_path = _state(other, display=NetworkUrlDisplay.HOST_PATH)
        host_path.admit_diagnostic(
            _fetch_envelope(_fetch_line(), NetworkUrlDisplay.HOST_PATH), now=0.0
        )
        host_path.admit_diagnostic(
            _fetch_envelope(_fetch_line(url=OTHER_URL), NetworkUrlDisplay.HOST_PATH),
            now=0.1,
        )
        self.assertEqual(
            f"npm http fetch GET 200 {OTHER_SAFE} (cache miss)",
            other.slot_texts()[-1],
        )


class TestFetchBoundaryFinalization(unittest.TestCase):
    """Task 5.3: ordered finalization at every boundary."""

    def _grouped(self) -> tuple[RecordingRenderer, HostPresentationState]:
        renderer = RecordingRenderer()
        state = _state(renderer)
        state.admit_diagnostic(_fetch_envelope(_fetch_line(latency=15), NetworkUrlDisplay.REDACTED), now=0.0)
        state.admit_diagnostic(_fetch_envelope(_fetch_line(latency=25), NetworkUrlDisplay.REDACTED), now=0.1)
        state.admit_diagnostic(_fetch_envelope(_fetch_line(latency=35), NetworkUrlDisplay.REDACTED), now=0.2)
        return renderer, state

    def _summary(self) -> str:
        return f"{REDACTED_CANON}{REQUEST_SEPARATOR}3 requests"

    def test_classification_and_malformed_boundaries_finalize_in_order(self):
        for classification in (
            HostDiagnosticClassification.WARNING,
            HostDiagnosticClassification.ERROR,
            HostDiagnosticClassification.RETRY,
            HostDiagnosticClassification.TIMEOUT,
            HostDiagnosticClassification.STATUS,
        ):
            with self.subTest(classification=classification):
                renderer, state = self._grouped()
                renderer.calls.clear()
                state.admit_diagnostic(
                    _diagnostic("npm warn retry", classification=classification),
                    now=0.3,
                )
                self.assertEqual([self._summary()], renderer.finalize_texts())

        renderer, state = self._grouped()
        renderer.calls.clear()
        state.admit_diagnostic(_diagnostic("a malformed ordinary line"), now=0.3)
        self.assertEqual([self._summary()], renderer.finalize_texts())

    def test_omission_notice_finalizes_then_is_emitted_non_coalesced(self):
        renderer, state = self._grouped()
        renderer.calls.clear()
        state.admit_diagnostic(
            _diagnostic("next"),
            now=0.3,
            omission_notice="[2 diagnostics omitted]",
        )
        self.assertEqual(
            [
                ("finalize", self._summary()),
                ("durable", "[2 diagnostics omitted]"),
                ("slot", "next"),
            ],
            renderer.calls,
        )

    def test_lifecycle_and_terminal_events_finalize(self):
        renderer, state = self._grouped()
        renderer.calls.clear()
        state.observe_step(
            HostStepEvent(
                HostPhase.LOCKED_ASSEMBLY,
                HostStep.NPM_EXECUTION,
                HostStepState.SUCCEEDED,
                True,
            ),
            now=0.3,
        )
        self.assertEqual([self._summary()], renderer.finalize_texts())

        renderer, state = self._grouped()
        renderer.calls.clear()
        state.observe_phase(
            HostPhaseEvent(HostPhase.LOCKED_ASSEMBLY, HostPhaseState.FAILED), now=0.3
        )
        self.assertEqual([self._summary()], renderer.finalize_texts())


class TestLinesFetchGrouping(unittest.TestCase):
    """Task 5.4: immediate first line and one counted window summary."""

    def test_first_line_is_durable_immediately(self):
        renderer = RecordingRenderer()
        state = _state(renderer, mode=HostPresentationMode.LINES)
        state.admit_diagnostic(_fetch_envelope(_fetch_line(), NetworkUrlDisplay.REDACTED), now=0.0)
        self.assertEqual([REDACTED_CANON], renderer.durable_texts())

    def test_counted_summary_at_the_one_second_window(self):
        renderer = RecordingRenderer()
        state = _state(renderer, mode=HostPresentationMode.LINES)
        state.admit_diagnostic(_fetch_envelope(_fetch_line(latency=15), NetworkUrlDisplay.REDACTED), now=0.0)
        state.admit_diagnostic(_fetch_envelope(_fetch_line(latency=25), NetworkUrlDisplay.REDACTED), now=0.1)
        state.admit_diagnostic(_fetch_envelope(_fetch_line(latency=35), NetworkUrlDisplay.REDACTED), now=0.2)
        self.assertEqual([REDACTED_CANON], renderer.durable_texts())
        state.due(now=LINES_WINDOW_SECONDS)
        self.assertEqual(
            [REDACTED_CANON, f"{REDACTED_CANON}{REQUEST_SEPARATOR}3 requests"],
            renderer.durable_texts(),
        )

    def test_fresh_group_after_deadline_finalization(self):
        renderer = RecordingRenderer()
        state = _state(renderer, mode=HostPresentationMode.LINES)
        state.admit_diagnostic(_fetch_envelope(_fetch_line(latency=15), NetworkUrlDisplay.REDACTED), now=0.0)
        state.admit_diagnostic(_fetch_envelope(_fetch_line(latency=25), NetworkUrlDisplay.REDACTED), now=0.1)
        state.due(now=LINES_WINDOW_SECONDS)
        state.admit_diagnostic(_fetch_envelope(_fetch_line(latency=55), NetworkUrlDisplay.REDACTED), now=1.1)
        state.admit_diagnostic(_fetch_envelope(_fetch_line(latency=65), NetworkUrlDisplay.REDACTED), now=1.2)
        state.due(now=2.1)
        self.assertEqual(
            [
                REDACTED_CANON,
                f"{REDACTED_CANON}{REQUEST_SEPARATOR}2 requests",
                REDACTED_CANON,
                f"{REDACTED_CANON}{REQUEST_SEPARATOR}2 requests",
            ],
            renderer.durable_texts(),
        )


class TestExactPresentation(unittest.TestCase):
    """Task 5.5: every terminal-safe source line stays visible."""

    def test_interactive_exact_finalizes_every_source_line(self):
        renderer = RecordingRenderer()
        state = _state(renderer, display=NetworkUrlDisplay.EXACT)
        state.admit_diagnostic(_source_envelope(_fetch_line(latency=15)), now=0.0)
        state.admit_diagnostic(_source_envelope(_fetch_line(latency=25)), now=0.1)
        self.assertEqual(
            [_fetch_line(latency=15), _fetch_line(latency=25)],
            renderer.finalize_texts(),
        )
        self.assertEqual([], renderer.durable_texts())
        self.assertEqual([], renderer.slot_texts())

    def test_exact_disables_repeat_fetch_and_numeric_aggregation(self):
        renderer = RecordingRenderer()
        state = _state(renderer, mode=HostPresentationMode.LINES, display=NetworkUrlDisplay.EXACT)
        for index in range(3):
            state.admit_diagnostic(_source_envelope("added 1 package"), now=float(index))
        for index in range(3):
            state.admit_diagnostic(_source_envelope(f"progress {index}"), now=3.0 + index)
        state.due(now=10.0)
        texts = renderer.durable_texts()
        self.assertEqual(
            ["added 1 package"] * 3 + ["progress 0", "progress 1", "progress 2"],
            texts,
        )
        self.assertNotIn("repeated", " ".join(texts))
        self.assertNotIn("requests", " ".join(texts))

    def test_exact_lines_ignores_fetch_identity_and_source_latency(self):
        renderer = RecordingRenderer()
        state = _state(renderer, mode=HostPresentationMode.LINES, display=NetworkUrlDisplay.EXACT)
        # An exact envelope carries source text and no fetch key, so two
        # otherwise compatible fetches remain distinct source lines.
        state.admit_diagnostic(_source_envelope(_fetch_line(latency=15)), now=0.0)
        state.admit_diagnostic(_source_envelope(_fetch_line(latency=15)), now=0.1)
        self.assertEqual([_fetch_line(latency=15)] * 2, renderer.durable_texts())


class TestEnvelopeContract(unittest.TestCase):
    """Task 5.6: the internal envelope shape and validation."""

    def test_presentation_text_selects_canonical_for_fetch(self):
        fetch = _fetch_envelope(_fetch_line(), NetworkUrlDisplay.REDACTED)
        self.assertEqual(REDACTED_CANON, fetch.presentation_text)
        self.assertTrue(fetch.is_fetch)
        plain = _source_envelope("npm warn plain")
        self.assertEqual("npm warn plain", plain.presentation_text)
        self.assertFalse(plain.is_fetch)

    def test_fetch_key_and_canonical_text_must_appear_together(self):
        record = parse_npm_fetch_line(_fetch_line())
        assert record is not None
        key = fetch_group_identity(record, NetworkUrlDisplay.REDACTED)
        assert key is not None
        base = {
            "phase": HostPhase.LOCKED_ASSEMBLY,
            "step": HostStep.NPM_EXECUTION,
            "stream": HostDiagnosticStream.STDOUT,
            "classification": HostDiagnosticClassification.STATUS,
            "text": "local",
        }
        with self.assertRaises(ValueError):
            HostDiagnosticEnvelope(**base, fetch_key=key)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            HostDiagnosticEnvelope(**base, fetch_text="canonical")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            HostDiagnosticEnvelope(**base, fetch_key="not-a-key", fetch_text="c")  # type: ignore[arg-type]

    def test_for_diagnostic_preserves_presentation_metadata(self):
        dto = _diagnostic(
            "safe", hostnames=("registry.example.com",), url_fingerprints=("a" * 64,)
        )
        envelope = HostDiagnosticEnvelope.for_diagnostic(dto)
        self.assertEqual(dto.phase, envelope.phase)
        self.assertEqual(dto.step, envelope.step)
        self.assertEqual(dto.stream, envelope.stream)
        self.assertEqual(dto.classification, envelope.classification)
        self.assertEqual(dto.hostnames, envelope.hostnames)
        self.assertEqual(dto.url_fingerprints, envelope.url_fingerprints)
        self.assertEqual(dto.text, envelope.text)
        self.assertIsNone(envelope.fetch_key)
        self.assertIsNone(envelope.fetch_text)

    def test_envelope_rejects_an_exact_fetch_key(self):
        exact_key = FetchGroupKey(
            display=NetworkUrlDisplay.EXACT,
            method="GET",
            status=200,
            attempt=None,
            cache_outcome="miss",
        )
        with self.assertRaises(ValueError):
            HostDiagnosticEnvelope(
                phase=HostPhase.LOCKED_ASSEMBLY,
                step=HostStep.NPM_EXECUTION,
                stream=HostDiagnosticStream.STDOUT,
                classification=HostDiagnosticClassification.STATUS,
                text=REDACTED_CANON,
                fetch_key=exact_key,
                fetch_text=REDACTED_CANON,
            )

    def test_exact_mode_envelopes_stay_unkeyed_source_text(self):
        source = "npm http fetch GET 200 <redacted> 15ms (cache miss)"
        envelope = HostDiagnosticEnvelope.for_diagnostic(
            _diagnostic("<redacted>"), text=source
        )
        self.assertIsNone(envelope.fetch_key)
        self.assertIsNone(envelope.fetch_text)
        self.assertFalse(envelope.is_fetch)
        self.assertEqual(source, envelope.presentation_text)


class TestPresentationPolicyConsistency(unittest.TestCase):
    """A fetch identity is policy-specific: mismatches are rejected up front."""

    def _assert_no_presentation_state(self, state, renderer):
        self.assertEqual([], renderer.calls)
        self.assertIsNone(state._coalescer.group)  # white-box: no pending group
        self.assertIsNone(state._refresh_deadline)
        self.assertIsNone(state._window_deadline)
        self.assertIsNone(state.next_deadline)

    def test_redacted_state_rejects_a_host_path_fetch_envelope(self):
        renderer = RecordingRenderer()
        state = _state(renderer, display=NetworkUrlDisplay.REDACTED)
        envelope = _fetch_envelope(_fetch_line(), NetworkUrlDisplay.HOST_PATH)
        with self.assertRaises(ValueError):
            state.admit_diagnostic(envelope, now=0.0)
        self._assert_no_presentation_state(state, renderer)

    def test_host_path_state_rejects_a_redacted_fetch_envelope(self):
        renderer = RecordingRenderer()
        state = _state(renderer, display=NetworkUrlDisplay.HOST_PATH)
        envelope = _fetch_envelope(_fetch_line(), NetworkUrlDisplay.REDACTED)
        with self.assertRaises(ValueError):
            state.admit_diagnostic(envelope, now=0.0)
        self._assert_no_presentation_state(state, renderer)

    def test_lines_mode_also_rejects_a_mismatched_policy(self):
        renderer = RecordingRenderer()
        state = _state(
            renderer, mode=HostPresentationMode.LINES, display=NetworkUrlDisplay.REDACTED
        )
        envelope = _fetch_envelope(_fetch_line(), NetworkUrlDisplay.HOST_PATH)
        with self.assertRaises(ValueError):
            state.admit_diagnostic(envelope, now=0.0)
        self._assert_no_presentation_state(state, renderer)

    def test_matching_policy_envelopes_are_still_accepted(self):
        for display in (NetworkUrlDisplay.REDACTED, NetworkUrlDisplay.HOST_PATH):
            with self.subTest(display=display):
                renderer = RecordingRenderer()
                state = _state(renderer, display=display)
                state.admit_diagnostic(_fetch_envelope(_fetch_line(), display), now=0.0)
                self.assertEqual(1, len(renderer.slot_texts()))

    def test_exact_envelopes_stay_unkeyed_and_unaggregated(self):
        renderer = RecordingRenderer()
        state = _state(renderer, display=NetworkUrlDisplay.EXACT)
        state.admit_diagnostic(_source_envelope("line one"), now=0.0)
        state.admit_diagnostic(_source_envelope("line two"), now=0.1)
        self.assertEqual(["line one", "line two"], renderer.finalize_texts())
        self.assertEqual([], renderer.slot_texts())
        self.assertIsNone(state.next_deadline)


class TestOrdinaryGroupingUnchanged(unittest.TestCase):
    """Task 5.9: ordinary exact-repeat and numeric-variant behavior stays."""

    def test_ordinary_exact_repeat_uses_canonical_suffix(self):
        renderer = RecordingRenderer()
        state = _state(renderer, mode=HostPresentationMode.LINES)
        state.admit_diagnostic(_diagnostic("npm warn retry"), now=0.0)
        state.admit_diagnostic(_diagnostic("npm warn retry"), now=0.1)
        state.due(now=LINES_WINDOW_SECONDS)
        self.assertEqual(
            ["npm warn retry", "npm warn retry (repeated 2 times)"],
            renderer.durable_texts(),
        )

    def test_ordinary_numeric_variant_replaces_interactive_slot(self):
        renderer = RecordingRenderer()
        state = _state(renderer)
        state.admit_diagnostic(_diagnostic("progress 1"), now=0.0)
        state.admit_diagnostic(_diagnostic("progress 2"), now=0.1)
        self.assertEqual(["progress 1", "progress 2"], renderer.slot_texts())

    def test_legacy_hostname_attachment_is_kept_for_ordinary_diagnostics(self):
        renderer = RecordingRenderer()
        state = _state(renderer, mode=HostPresentationMode.LINES, display=NetworkUrlDisplay.HOST_PATH)
        state.admit_diagnostic(
            _diagnostic("npm warn see <redacted>", hostnames=("registry.example.com",)),
            now=0.0,
        )
        self.assertEqual(
            ["npm warn see <redacted> [registry.example.com]"],
            renderer.durable_texts(),
        )

    def test_ordinary_exact_repeat_in_host_path_mode(self):
        renderer = RecordingRenderer()
        state = _state(
            renderer, mode=HostPresentationMode.LINES, display=NetworkUrlDisplay.HOST_PATH
        )
        for index in range(2):
            state.admit_diagnostic(
                _diagnostic("npm warn retry", hostnames=("registry.example.com",)),
                now=float(index) * 0.1,
            )
        state.due(now=LINES_WINDOW_SECONDS)
        self.assertEqual(
            [
                "npm warn retry [registry.example.com]",
                "npm warn retry (repeated 2 times) [registry.example.com]",
            ],
            renderer.durable_texts(),
        )

    def test_ordinary_numeric_variant_in_host_path_mode(self):
        renderer = RecordingRenderer()
        state = _state(renderer, display=NetworkUrlDisplay.HOST_PATH)
        state.admit_diagnostic(
            _diagnostic("npm warn progress 1", hostnames=("registry.example.com",)),
            now=0.0,
        )
        state.admit_diagnostic(
            _diagnostic("npm warn progress 2", hostnames=("registry.example.com",)),
            now=0.1,
        )
        self.assertEqual(
            [
                "npm warn progress 1 [registry.example.com]",
                "npm warn progress 2 [registry.example.com]",
            ],
            renderer.slot_texts(),
        )

    def test_fetch_identity_ignores_url_fingerprints(self):
        from docker.versioning.diagnostic_identity import identity_for

        def envelope(fingerprint: str) -> HostDiagnosticEnvelope:
            record = parse_npm_fetch_line(_fetch_line())
            assert record is not None
            key = fetch_group_identity(record, NetworkUrlDisplay.REDACTED)
            canonical = canonical_fetch_text(record, NetworkUrlDisplay.REDACTED)
            assert key is not None and canonical is not None
            external = _diagnostic(canonical, url_fingerprints=(fingerprint,))
            return HostDiagnosticEnvelope.for_diagnostic(
                external, text=canonical, fetch_key=key, fetch_text=canonical
            )

        left = identity_for(envelope("a" * 64))
        right = identity_for(envelope("b" * 64))
        self.assertEqual(left.presentation_metadata, right.presentation_metadata)
        self.assertEqual((), left.url_fingerprints)

    def test_fetch_counts_never_claim_events_dropped_before_admission(self):
        renderer = RecordingRenderer()
        state = _state(renderer)
        state.admit_diagnostic(_fetch_envelope(_fetch_line(latency=15), NetworkUrlDisplay.REDACTED), now=0.0)
        state.admit_diagnostic(_fetch_envelope(_fetch_line(latency=25), NetworkUrlDisplay.REDACTED), now=0.1)
        state.admit_diagnostic(
            _diagnostic("npm warn post-saturation"),
            now=0.2,
            omission_notice="[5 diagnostics omitted]",
        )
        self.assertEqual(
            [f"{REDACTED_CANON}{REQUEST_SEPARATOR}2 requests"],
            renderer.finalize_texts(),
        )
        self.assertNotIn("7 requests", " ".join(renderer.finalize_texts()))

    def test_external_dto_cannot_carry_fetch_or_source_metadata(self):
        for field in dataclasses.fields(HostStructuredDiagnostic):
            self.assertNotIn(
                field.name,
                {"fetch_key", "fetch_text", "path", "safe_path", "source"},
            )
        self.assertIsNot(HostDiagnosticEnvelope, HostStructuredDiagnostic)
        self.assertFalse(hasattr(_diagnostic("x"), "is_fetch"))
        self.assertFalse(hasattr(_diagnostic("x"), "presentation_text"))

    def test_fetch_disposition_is_distinct_from_ordinary_repeat(self):
        self.assertIn("FETCH_REQUEST", {member.name for member in DiagnosticDisposition})
        self.assertNotEqual(
            DiagnosticDisposition.FETCH_REQUEST, DiagnosticDisposition.EXACT_REPEAT
        )
        self.assertEqual(
            "interactive", PresentationMode.INTERACTIVE.value
        )


class TestFetchLifecycleBoundaryFlush(unittest.TestCase):
    """A STARTED phase/step flushes a pending fetch group in order."""

    def _grouped(
        self, mode: HostPresentationMode, display: NetworkUrlDisplay
    ) -> tuple[RecordingRenderer, HostPresentationState]:
        renderer = RecordingRenderer()
        state = _state(renderer, mode=mode, display=display)
        state.admit_diagnostic(_fetch_envelope(_fetch_line(latency=15), display), now=0.0)
        state.admit_diagnostic(_fetch_envelope(_fetch_line(latency=25), display), now=0.1)
        renderer.calls.clear()
        return renderer, state

    def _summary(self, display: NetworkUrlDisplay) -> str:
        return f"{_canonical(display)}{REQUEST_SEPARATOR}2 requests"

    def _step_started(self) -> HostStepEvent:
        return HostStepEvent(
            HostPhase.LOCKED_ASSEMBLY,
            HostStep.NPM_EXECUTION,
            HostStepState.STARTED,
            True,
        )

    def _phase_started(self) -> HostPhaseEvent:
        return HostPhaseEvent(HostPhase.DOCKER_TRANSITION, HostPhaseState.STARTED)

    def test_interactive_step_start_flushes_only_the_fetch_group(self):
        for display in (NetworkUrlDisplay.REDACTED, NetworkUrlDisplay.HOST_PATH):
            with self.subTest(display=display):
                renderer, state = self._grouped(
                    HostPresentationMode.INTERACTIVE, display
                )
                state.observe_step(self._step_started(), now=0.2)
                self.assertEqual(
                    [
                        ("finalize", self._summary(display)),
                        ("status", "npm execution…"),
                    ],
                    renderer.calls,
                )
                state.admit_diagnostic(
                    _fetch_envelope(_fetch_line(latency=99), display), now=0.3
                )
                self.assertEqual(_canonical(display), renderer.slot_texts()[-1])

    def test_interactive_phase_start_flushes_only_the_fetch_group(self):
        for display in (NetworkUrlDisplay.REDACTED, NetworkUrlDisplay.HOST_PATH):
            with self.subTest(display=display):
                renderer, state = self._grouped(
                    HostPresentationMode.INTERACTIVE, display
                )
                state.observe_phase(self._phase_started(), now=0.2)
                self.assertEqual(
                    [
                        ("finalize", self._summary(display)),
                        ("status", "docker transition: started"),
                    ],
                    renderer.calls,
                )
                state.admit_diagnostic(
                    _fetch_envelope(_fetch_line(latency=99), display), now=0.3
                )
                self.assertEqual(_canonical(display), renderer.slot_texts()[-1])

    def test_lines_step_start_flushes_then_starts_a_fresh_count(self):
        renderer, state = self._grouped(
            HostPresentationMode.LINES, NetworkUrlDisplay.REDACTED
        )
        state.observe_step(self._step_started(), now=0.2)
        self.assertEqual(
            [
                ("finalize", self._summary(NetworkUrlDisplay.REDACTED)),
                ("durable", "npm execution: started"),
            ],
            renderer.calls,
        )
        state.admit_diagnostic(_fetch_envelope(_fetch_line(latency=35), NetworkUrlDisplay.REDACTED), now=0.3)
        state.admit_diagnostic(_fetch_envelope(_fetch_line(latency=45), NetworkUrlDisplay.REDACTED), now=0.4)
        state.due(now=1.5)
        self.assertEqual(
            [
                "npm execution: started",
                REDACTED_CANON,
                f"{REDACTED_CANON}{REQUEST_SEPARATOR}2 requests",
            ],
            renderer.durable_texts(),
        )

    def test_lines_phase_start_flushes_then_starts_a_fresh_count(self):
        renderer, state = self._grouped(
            HostPresentationMode.LINES, NetworkUrlDisplay.REDACTED
        )
        state.observe_phase(self._phase_started(), now=0.2)
        self.assertEqual(
            [
                ("finalize", self._summary(NetworkUrlDisplay.REDACTED)),
                ("durable", "docker transition: started"),
            ],
            renderer.calls,
        )
        state.admit_diagnostic(_fetch_envelope(_fetch_line(latency=35), NetworkUrlDisplay.REDACTED), now=0.3)
        state.admit_diagnostic(_fetch_envelope(_fetch_line(latency=45), NetworkUrlDisplay.REDACTED), now=0.4)
        state.due(now=1.5)
        self.assertEqual(
            [
                "docker transition: started",
                REDACTED_CANON,
                f"{REDACTED_CANON}{REQUEST_SEPARATOR}2 requests",
            ],
            renderer.durable_texts(),
        )

    def test_ordinary_grouping_is_not_flushed_by_a_started_event(self):
        renderer = RecordingRenderer()
        state = _state(renderer, mode=HostPresentationMode.LINES)
        state.admit_diagnostic(_diagnostic("npm warn retry"), now=0.0)
        state.admit_diagnostic(_diagnostic("npm warn retry"), now=0.1)
        renderer.calls.clear()
        state.observe_step(self._step_started(), now=0.2)
        self.assertEqual(
            [("durable", "npm execution: started")], renderer.calls
        )


class TestExactInteractiveSlotClearing(unittest.TestCase):
    """Exact interactive finalization clears the provisional slot."""

    def test_completed_exact_line_clears_prefix_and_heartbeat_does_not_redraw_it(self):
        stream = io.StringIO()
        renderer = TerminalHostRenderer(stream, terminal_width=lambda: 80)
        state = HostPresentationState(
            renderer,
            mode=HostPresentationMode.INTERACTIVE,
            network_url_display=NetworkUrlDisplay.EXACT,
        )
        completed = _fetch_line(latency=15)
        prefix = completed[: -len("(cache miss)")]
        state.admit_prefix(
            HostDiagnosticPrefix(
                HostPhase.LOCKED_ASSEMBLY,
                HostStep.NPM_EXECUTION,
                HostDiagnosticStream.STDOUT,
                prefix,
            )
        )
        state.admit_diagnostic(_source_envelope(completed), now=0.0)
        # The provisional slot must be gone once the complete line finalizes.
        self.assertIsNone(renderer._slot)  # white-box: real renderer slot
        state.observe_heartbeat(_heartbeat(), now=1.0)
        output = stream.getvalue()
        self.assertEqual(1, output.count(completed))
        tail = output[output.index(completed) + len(completed) :]
        self.assertNotIn(prefix, tail)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
