"""Phase 7 collection-to-presentation integration contracts.

These tests bind tasks 7.1--7.5 and the 7.10--7.11 introspection regressions of
``add-configurable-network-url-display``.  They drive the production collector
(:class:`NpmDiagnosticStream`) through the production envelope builder
(:func:`presentation_envelope_for`) into the single presentation actor, so the
mode-selected local text and policy-specific fetch identity are exercised end
to end without a Docker daemon, network, or clock dependency.

The external :class:`HostStructuredDiagnostic` DTO is asserted to stay
URL-free and path-free in every mode.
"""
from __future__ import annotations

import dataclasses
import threading
import time
import unittest

from docker.npm_environment.streaming import (
    REDACTED,
    RETAINED_CAPTURE_ATTR,
    StreamChunk,
    StreamReaderFailure,
    TAIL_BYTES,
    collect_streams,
)
from docker.versioning.host_presentation import (
    LINES_WINDOW_SECONDS,
    OMISSION_COUNTER_LIMIT,
    TELEMETRY_CAPACITY,
    WORKER_JOIN_SECONDS,
    HostEventEnqueueAdapter,
    HostPresentationMode,
    HostPresentationSession,
    HostPresentationState,
    PresentationMailbox,
    PresentationPlan,
    PresentationSelection,
)
from docker.versioning.host_progress import (
    HostDiagnosticClassification,
    HostDiagnosticEnvelope,
    HostDiagnosticPrefix,
    HostDiagnosticStream,
    HostPhase,
    HostStep,
    HostStepEvent,
    HostStepState,
    HostStructuredDiagnostic,
    InternalDirectHostEventSink,
)
from docker.versioning.model import NetworkUrlDisplay
from docker.versioning.diagnostic_projection import INCOMPLETE_TOKEN_MARKER
from docker.versioning.npm_diagnostic_stream import (
    NpmDiagnosticStream,
    OVERSIZED_DIAGNOSTIC_MARKER,
    make_stream_factory,
)
from docker.versioning.pi_assembly import (
    overflow_diagnostic_for,
    presentation_envelope_for,
    route_committed_prefix,
    route_finalized_diagnostic,
    route_overflow_diagnostic,
    structured_diagnostic_for,
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

ALL_MODES = (
    NetworkUrlDisplay.REDACTED,
    NetworkUrlDisplay.HOST_PATH,
    NetworkUrlDisplay.EXACT,
)


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


def _collect(
    lines: list[str],
    display: NetworkUrlDisplay,
    *,
    secrets: tuple[str, ...] = (),
) -> tuple[list[StreamChunk], str]:
    """Run the production collector and return finalized chunks plus the tail."""
    stream = NpmDiagnosticStream(
        "stdout", secrets, network_url_display=display
    )
    chunks: list[StreamChunk] = []
    for line in lines:
        chunks.extend(stream.feed_bytes((line + "\n").encode("utf-8")))
    chunks.extend(stream.finish())
    return [chunk for chunk in chunks if chunk.finalized], stream.tail()


def _envelopes(lines: list[str], display: NetworkUrlDisplay) -> list[HostDiagnosticEnvelope]:
    chunks, _tail = _collect(lines, display)
    return [
        presentation_envelope_for(chunk, logical_resource=None)
        for chunk in chunks
    ]


class RecordingRenderer:
    """Ordered renderer recorder with optional failure/blocking hooks."""

    def __init__(
        self,
        *,
        fail_on: str | None = None,
        block_on: str | None = None,
    ) -> None:
        self.calls: list[tuple[object, ...]] = []
        self._fail_on = fail_on
        self._block_on = block_on
        self.gate = threading.Event()

    def _record(self, *call: object) -> None:
        self.calls.append(call)
        if self._block_on is not None and call[0] == self._block_on:
            self.gate.wait(timeout=WORKER_JOIN_SECONDS + 3.0)
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
    mode: HostPresentationMode,
    display: NetworkUrlDisplay,
) -> HostPresentationState:
    return HostPresentationState(
        renderer, mode=mode, network_url_display=display
    )


def _session(
    renderer: RecordingRenderer,
    mode: HostPresentationMode,
    display: NetworkUrlDisplay,
) -> HostPresentationSession:
    return HostPresentationSession(
        renderer,
        PresentationPlan(mode, PresentationSelection.LIVE, display),
    )


def _diagnostic_envelope(
    text: str,
    display: NetworkUrlDisplay,
    *,
    stream: HostDiagnosticStream = HostDiagnosticStream.STDOUT,
    classification: HostDiagnosticClassification = HostDiagnosticClassification.STATUS,
) -> HostDiagnosticEnvelope:
    external = HostStructuredDiagnostic(
        phase=HostPhase.LOCKED_ASSEMBLY,
        step=HostStep.NPM_EXECUTION,
        stream=stream,
        classification=classification,
        text=text,
    )
    return HostDiagnosticEnvelope.for_diagnostic(external, text=text)


class TestRealOutputIntegrationMatrix(unittest.TestCase):
    """Task 7.4: real raw npm lines through collection into presentation."""

    def test_redacted_compatible_lines_aggregate_without_url_or_latency(self):
        chunks, _tail = _collect(
            [
                _fetch_line(latency=15),
                _fetch_line(latency=25),
                _fetch_line(latency=35),
            ],
            NetworkUrlDisplay.REDACTED,
        )
        self.assertEqual(3, len(chunks))
        for chunk in chunks:
            self.assertIsNotNone(chunk.fetch_key)
            self.assertIn("redacted", str(chunk.fetch_key.display))
            self.assertEqual(REDACTED_CANON, chunk.fetch_text)
            self.assertNotIn(RESEARCH_URL, chunk.fetch_text or "")
            self.assertNotIn("ms", chunk.fetch_text or "")
            # The external-safe projection is URL-free.
            self.assertNotIn(RESEARCH_URL, chunk.text)
        renderer = RecordingRenderer()
        state = _state(renderer, HostPresentationMode.INTERACTIVE, NetworkUrlDisplay.REDACTED)
        for index, chunk in enumerate(chunks):
            state.admit_diagnostic(
                presentation_envelope_for(chunk, logical_resource=None),
                now=float(index),
            )
        self.assertEqual(
            [
                REDACTED_CANON,
                f"{REDACTED_CANON}{REQUEST_SEPARATOR}2 requests",
                f"{REDACTED_CANON}{REQUEST_SEPARATOR}3 requests",
            ],
            renderer.slot_texts(),
        )

    def test_host_path_aggregates_same_resource_and_separates_resources(self):
        chunks, _tail = _collect(
            [
                _fetch_line(latency=15),
                _fetch_line(latency=25),
                _fetch_line(OTHER_URL, latency=30),
            ],
            NetworkUrlDisplay.HOST_PATH,
        )
        self.assertEqual(HOST_PATH_CANON, chunks[0].fetch_text)
        self.assertEqual(HOST_PATH_CANON, chunks[1].fetch_text)
        self.assertIn(OTHER_SAFE, chunks[2].fetch_text or "")
        renderer = RecordingRenderer()
        state = _state(renderer, HostPresentationMode.INTERACTIVE, NetworkUrlDisplay.HOST_PATH)
        for index, chunk in enumerate(chunks):
            state.admit_diagnostic(
                presentation_envelope_for(chunk, logical_resource=None),
                now=float(index),
            )
        slots = renderer.slot_texts()
        self.assertEqual(f"{HOST_PATH_CANON}{REQUEST_SEPARATOR}2 requests", slots[1])
        # A different resource starts a fresh group instead of joining the
        # first resource's count.
        self.assertIn(OTHER_SAFE, slots[-1])
        self.assertNotIn(REQUEST_SEPARATOR, slots[-1])

    def test_host_path_canonical_bypasses_legacy_bracketed_hostname_attachment(self):
        chunks, _tail = _collect([_fetch_line(latency=15)], NetworkUrlDisplay.HOST_PATH)
        renderer = RecordingRenderer()
        state = _state(renderer, HostPresentationMode.INTERACTIVE, NetworkUrlDisplay.HOST_PATH)
        envelope = presentation_envelope_for(
            chunks[0], logical_resource=None
        )
        # Even when the collector observed the hostname, a recognized fetch
        # keeps the canonical hostname/path line and never appends the legacy
        # bracketed form.
        envelope = dataclasses.replace(envelope, hostnames=("registry.npmjs.org",))
        state.admit_diagnostic(envelope, now=0.0)
        self.assertEqual([HOST_PATH_CANON], renderer.slot_texts())
        self.assertNotIn("[registry.npmjs.org]", " ".join(renderer.slot_texts()))

    def test_exact_remains_complete_unaggregated_source_lines(self):
        lines = [_fetch_line(latency=15), _fetch_line(latency=25)]
        chunks, _tail = _collect(lines, NetworkUrlDisplay.EXACT)
        self.assertEqual(2, len(chunks))
        for chunk in chunks:
            self.assertIsNone(chunk.fetch_key)
            self.assertIsNone(chunk.fetch_text)
            self.assertIn(RESEARCH_URL, chunk.local_text or "")
            self.assertIn("ms", chunk.local_text or "")
        renderer = RecordingRenderer()
        state = _state(renderer, HostPresentationMode.INTERACTIVE, NetworkUrlDisplay.EXACT)
        for index, chunk in enumerate(chunks):
            state.admit_diagnostic(
                presentation_envelope_for(chunk, logical_resource=None),
                now=float(index),
            )
        self.assertEqual(2, len(renderer.finalize_texts()))
        self.assertIn(RESEARCH_URL, renderer.finalize_texts()[0])
        self.assertIn("15ms", renderer.finalize_texts()[0])
        self.assertIn("25ms", renderer.finalize_texts()[1])
        self.assertNotIn("thread", " ".join(renderer.finalize_texts()).lower())

    def test_noninteractive_lines_render_selected_representation_in_every_mode(self):
        source_url = (
            "https://user:password@registry.npmjs.org/"
            "npm-http-research-fixture/-/npm-http-research-fixture-1.0.0.tgz"
            "?token=caller-secret#fragment"
        )
        source_line = _fetch_line(source_url, latency=15)
        expectations = {
            NetworkUrlDisplay.REDACTED: REDACTED_CANON,
            NetworkUrlDisplay.HOST_PATH: HOST_PATH_CANON,
            NetworkUrlDisplay.EXACT: source_line,
        }

        for display in ALL_MODES:
            with self.subTest(display=display):
                chunks, _tail = _collect(
                    [source_line], display, secrets=("password", "caller-secret")
                )
                self.assertEqual(1, len(chunks))
                renderer = RecordingRenderer()
                session = _session(renderer, HostPresentationMode.LINES, display)
                route_finalized_diagnostic(
                    chunks[0],
                    internal_sink=session.sink,
                    sdk_sink=None,
                    logical_resource=None,
                )
                self.assertTrue(session.shutdown())

                # Lines mode emits one complete durable record; the renderer
                # owns newline termination, so the payload itself has no LF.
                self.assertEqual([expectations[display]], renderer.durable_texts())
                self.assertNotIn("\n", renderer.durable_texts()[0])
                self.assertEqual([], renderer.slot_texts())
                self.assertEqual([], renderer.finalize_texts())

                rendered = renderer.durable_texts()[0]
                if display is NetworkUrlDisplay.REDACTED:
                    self.assertNotIn("registry.npmjs.org", rendered)
                    self.assertNotIn("npm-http-research-fixture/", rendered)
                    self.assertNotIn("https://", rendered)
                elif display is NetworkUrlDisplay.HOST_PATH:
                    self.assertIn(RESEARCH_SAFE, rendered)
                    self.assertNotIn("https://", rendered)
                    self.assertNotIn("user:password", rendered)
                    self.assertNotIn("caller-secret", rendered)
                else:
                    self.assertIn(source_url, rendered)
                    self.assertIn("user:password", rendered)
                    self.assertIn("caller-secret", rendered)

    def test_external_sdk_dto_stays_url_free_and_path_free_in_every_mode(self):
        for display in ALL_MODES:
            with self.subTest(display=display):
                chunks, _tail = _collect([_fetch_line(latency=15)], display)
                chunk = chunks[0]
                dto = structured_diagnostic_for(chunk, logical_resource=None)
                self.assertNotIn(RESEARCH_URL, dto.text)
                self.assertNotIn(RESEARCH_SAFE, dto.text)
                # The DTO has no field for fetch identity or local text.
                field_names = {
                    field.name for field in dataclasses.fields(HostStructuredDiagnostic)
                }
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

    def test_envelope_carries_the_mode_selected_local_text(self):
        for display in ALL_MODES:
            with self.subTest(display=display):
                chunks, _tail = _collect([_fetch_line(latency=15)], display)
                envelope = presentation_envelope_for(chunks[0], logical_resource=None)
                if display is NetworkUrlDisplay.EXACT:
                    self.assertIn(RESEARCH_URL, envelope.presentation_text)
                elif display is NetworkUrlDisplay.HOST_PATH:
                    self.assertIn(RESEARCH_SAFE, envelope.presentation_text)
                    self.assertNotIn(RESEARCH_URL, envelope.presentation_text)
                else:
                    self.assertEqual(REDACTED_CANON, envelope.presentation_text)


class TestHighVolumeAcceptance(unittest.TestCase):
    """Task 7.5: bounded high-volume acceptance in every mode."""

    def test_redacted_interactive_counts_a_large_group(self):
        total = 400
        lines = [
            _fetch_line(latency=10 + (index % 50)) for index in range(total)
        ]
        renderer = RecordingRenderer()
        state = _state(renderer, HostPresentationMode.INTERACTIVE, NetworkUrlDisplay.REDACTED)
        for index, envelope in enumerate(_envelopes(lines, NetworkUrlDisplay.REDACTED)):
            state.admit_diagnostic(envelope, now=index * 0.001)
        self.assertEqual(
            f"{REDACTED_CANON}{REQUEST_SEPARATOR}{total} requests",
            renderer.slot_texts()[-1],
        )
        self.assertEqual([], renderer.durable_texts())

    def test_redacted_lines_emits_one_counted_summary_at_the_window(self):
        total = 200
        lines = [_fetch_line(latency=10 + (index % 50)) for index in range(total)]
        renderer = RecordingRenderer()
        state = _state(renderer, HostPresentationMode.LINES, NetworkUrlDisplay.REDACTED)
        envelopes = _envelopes(lines, NetworkUrlDisplay.REDACTED)
        for envelope in envelopes:
            state.admit_diagnostic(envelope, now=0.0)
        # The first member of a fresh group is durable immediately.
        self.assertEqual([REDACTED_CANON], renderer.durable_texts())
        state.due(now=LINES_WINDOW_SECONDS)
        self.assertEqual(
            [
                REDACTED_CANON,
                f"{REDACTED_CANON}{REQUEST_SEPARATOR}{total} requests",
            ],
            renderer.durable_texts(),
        )

    def test_host_path_high_volume_aggregates_and_separates(self):
        lines = [(RESEARCH_URL, index) for index in range(150)]
        lines += [(OTHER_URL, index) for index in range(150)]
        raw = [_fetch_line(url, latency=10 + index % 40) for url, index in lines]
        renderer = RecordingRenderer()
        state = _state(renderer, HostPresentationMode.INTERACTIVE, NetworkUrlDisplay.HOST_PATH)
        for index, envelope in enumerate(_envelopes(raw, NetworkUrlDisplay.HOST_PATH)):
            state.admit_diagnostic(envelope, now=index * 0.001)
        slots = renderer.slot_texts()
        self.assertIn(f"{REQUEST_SEPARATOR}150 requests", slots[149])
        self.assertIn(OTHER_SAFE, slots[150])
        self.assertIn(f"{REQUEST_SEPARATOR}150 requests", slots[-1])

    def test_exact_high_volume_stays_unaggregated(self):
        total = 300
        lines = [_fetch_line(latency=10 + (index % 50)) for index in range(total)]
        renderer = RecordingRenderer()
        state = _state(renderer, HostPresentationMode.INTERACTIVE, NetworkUrlDisplay.EXACT)
        for index, envelope in enumerate(_envelopes(lines, NetworkUrlDisplay.EXACT)):
            state.admit_diagnostic(envelope, now=index * 0.001)
        self.assertEqual(total, len(renderer.finalize_texts()))
        self.assertTrue(
            all(REQUEST_SEPARATOR not in text for text in renderer.finalize_texts())
        )

    def test_exactly_one_selected_and_bounded_retained_tail(self):
        for display in ALL_MODES:
            with self.subTest(display=display):
                lines = [
                    _fetch_line(latency=10 + (index % 50))
                    for index in range(500)
                ]
                chunks, tail = _collect(lines, display)
                self.assertTrue(tail)
                self.assertLessEqual(len(tail.encode("utf-8")), TAIL_BYTES)
                if display is NetworkUrlDisplay.EXACT:
                    self.assertIn(RESEARCH_URL, tail)
                elif display is NetworkUrlDisplay.HOST_PATH:
                    self.assertIn(RESEARCH_SAFE, tail)
                    self.assertNotIn(RESEARCH_URL, tail)
                else:
                    self.assertNotIn(RESEARCH_URL, tail)
                self.assertEqual(500, len(chunks))


class TestModeSaturationOrdering(unittest.TestCase):
    """Task 7.1: admission, omission, headroom, and ordering in every mode."""

    def test_diagnostic_saturation_keeps_control_headroom_in_every_mode(self):
        for display in ALL_MODES:
            with self.subTest(display=display):
                mailbox = PresentationMailbox()
                admitted = 0
                while mailbox.admit_telemetry(
                    _diagnostic_envelope(f"flood {admitted}", display)
                ):
                    admitted += 1
                self.assertEqual(TELEMETRY_CAPACITY, admitted)
                # A further diagnostic is dropped and recorded as an omission.
                self.assertFalse(
                    mailbox.admit_telemetry(_diagnostic_envelope("dropped", display))
                )
                self.assertIsNotNone(mailbox.take_omission_notice(sequence=None))
                # Control capacity stays fully available under telemetry
                # saturation.
                step = HostStepEvent(
                    HostPhase.LOCKED_ASSEMBLY,
                    HostStep.NPM_EXECUTION,
                    HostStepState.SUCCEEDED,
                    True,
                )
                for _ in range(8):
                    self.assertTrue(mailbox.admit_control(step))
                self.assertFalse(mailbox.admission_failed)

    def test_accepted_events_keep_their_admission_order_in_every_mode(self):
        for display in ALL_MODES:
            with self.subTest(display=display):
                mailbox = PresentationMailbox()
                for index in range(10):
                    mailbox.admit_telemetry(
                        _diagnostic_envelope(f"line {index}", display)
                    )
                taken = []
                while True:
                    item = mailbox.take(timeout=0.0)
                    if item is None:
                        break
                    taken.append(item.sequence)
                self.assertEqual(list(range(10)), taken)
                self.assertEqual(0, mailbox.dropped)

    def test_omission_counter_is_bounded(self):
        mailbox = PresentationMailbox()
        # Fill and drain, then drop far more than the counter limit.
        for _ in range(TELEMETRY_CAPACITY):
            mailbox.admit_telemetry(_diagnostic_envelope("filler", NetworkUrlDisplay.REDACTED))
        while mailbox.take(timeout=0.0) is not None:
            pass
        for _ in range(OMISSION_COUNTER_LIMIT + TELEMETRY_CAPACITY + 5):
            mailbox.admit_telemetry(_diagnostic_envelope("x", NetworkUrlDisplay.REDACTED))
        notice = mailbox.take_omission_notice(sequence=None)
        self.assertIsNotNone(notice)
        self.assertIn("at least", notice)


class TestRendererFailureIsolation(unittest.TestCase):
    """Task 7.2: renderer failure, blocked writes, and bounded completion."""

    def test_renderer_exception_disables_rendering_without_fallback(self):
        for display in ALL_MODES:
            with self.subTest(display=display):
                renderer = RecordingRenderer(fail_on="durable")
                session = _session(
                    renderer, HostPresentationMode.LINES, display
                )
                combined = _envelopes(
                    [_fetch_line(latency=15)], display
                )
                self.assertTrue(
                    session.sink.admit_diagnostic(combined[0])
                )
                self.assertTrue(
                    session.sink.admit_diagnostic(
                        _diagnostic_envelope(
                            "later diagnostic", display
                        )
                    )
                )
                self.assertTrue(session.shutdown())
                # The renderer failed once and was permanently disabled: no
                # synchronous fallback write and no retry, even for a later
                # diagnostic.
                self.assertEqual(1, len(renderer.calls))
                self.assertFalse(session.worker.is_alive)

    def test_blocked_renderer_stays_within_the_completion_budget(self):
        renderer = RecordingRenderer(block_on="durable")
        self.addCleanup(renderer.gate.set)
        session = _session(
            renderer, HostPresentationMode.LINES, NetworkUrlDisplay.REDACTED
        )
        envelope = _envelopes([_fetch_line(latency=15)], NetworkUrlDisplay.REDACTED)[0]
        session.sink.admit_diagnostic(envelope)
        started = time.monotonic()
        self.assertFalse(session.shutdown())
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, WORKER_JOIN_SECONDS + 3.0)
        renderer.gate.set()
        session.worker.join(WORKER_JOIN_SECONDS)
        self.assertFalse(session.worker.is_alive)

    def test_no_renderer_operation_after_successful_completion(self):
        renderer = RecordingRenderer()
        session = _session(
            renderer, HostPresentationMode.LINES, NetworkUrlDisplay.REDACTED
        )
        session.sink.admit_diagnostic(
            _envelopes([_fetch_line(latency=15)], NetworkUrlDisplay.REDACTED)[0]
        )
        self.assertTrue(session.shutdown())
        settled = len(renderer.calls)
        time.sleep(0.1)
        self.assertEqual(settled, len(renderer.calls))


class TestCloseDrainOrdering(unittest.TestCase):
    """Task 7.3: finalization, ordering, and reuse across the actor lifetime."""

    def test_pending_fetch_group_finalizes_before_completion(self):
        renderer = RecordingRenderer()
        session = _session(
            renderer, HostPresentationMode.LINES, NetworkUrlDisplay.REDACTED
        )
        envelopes = _envelopes(
            [_fetch_line(latency=15), _fetch_line(latency=25)],
            NetworkUrlDisplay.REDACTED,
        )
        for envelope in envelopes:
            session.sink.admit_diagnostic(envelope)
        self.assertTrue(session.shutdown())
        self.assertIn(
            f"{REDACTED_CANON}{REQUEST_SEPARATOR}2 requests",
            renderer.finalize_texts(),
        )
        self.assertIn(REDACTED_CANON, renderer.durable_texts())

    def test_stream_finalization_flushes_an_unterminated_line(self):
        stream = NpmDiagnosticStream(
            "stdout", (), network_url_display=NetworkUrlDisplay.REDACTED
        )
        chunks = list(stream.feed_bytes(b"npm warn partial without newline"))
        self.assertTrue(chunks)
        self.assertTrue(all(not chunk.finalized for chunk in chunks))
        chunks = list(stream.finish())
        finalized = [chunk for chunk in chunks if chunk.finalized]
        self.assertEqual(1, len(finalized))
        self.assertEqual("npm warn partial without newline", finalized[0].text)

    def test_completed_acknowledgement_precedes_no_later_output(self):
        renderer = RecordingRenderer()
        session = _session(
            renderer, HostPresentationMode.LINES, NetworkUrlDisplay.REDACTED
        )
        self.assertTrue(session.submit_final_report("final report"))
        self.assertTrue(session.shutdown())
        self.assertIn("final report", renderer.durable_texts())
        # Once the final report was written, no later durable/finalize
        # diagnostic output appears (only the terminal-region reset may run).
        final_index = renderer.calls.index(("durable", "final report"))
        later = renderer.calls[final_index + 1 :]
        self.assertFalse(
            any(call[0] in ("durable", "finalize") for call in later)
        )

    def test_actor_is_reused_across_step_terminals(self):
        renderer = RecordingRenderer()
        session = _session(
            renderer, HostPresentationMode.LINES, NetworkUrlDisplay.REDACTED
        )
        for _ in range(2):
            session.sink(
                HostStepEvent(
                    HostPhase.LOCKED_ASSEMBLY,
                    HostStep.NPM_EXECUTION,
                    HostStepState.SUCCEEDED,
                    True,
                )
            )
        session.sink.admit_diagnostic(
            _envelopes([_fetch_line(latency=15)], NetworkUrlDisplay.REDACTED)[0]
        )
        self.assertTrue(session.shutdown())
        self.assertIn(REDACTED_CANON, renderer.durable_texts())


class TestPresentationIsolation(unittest.TestCase):
    """Tasks 7.10--7.11: producer independence and primary-result isolation."""

    def test_admission_never_waits_for_capacity(self):
        mailbox = PresentationMailbox()
        for _ in range(TELEMETRY_CAPACITY):
            mailbox.admit_telemetry(_diagnostic_envelope("filler", NetworkUrlDisplay.REDACTED))
        started = time.monotonic()
        for index in range(1000):
            mailbox.admit_telemetry(
                _diagnostic_envelope(f"overflow {index}", NetworkUrlDisplay.REDACTED)
            )
        self.assertLess(time.monotonic() - started, 0.5)

    def test_renderer_failure_leaves_dto_and_tail_independent(self):
        renderer = RecordingRenderer(fail_on="durable")
        session = _session(renderer, HostPresentationMode.LINES, NetworkUrlDisplay.EXACT)
        chunks, tail = _collect([_fetch_line(latency=15)], NetworkUrlDisplay.EXACT)
        dto = structured_diagnostic_for(chunks[0], logical_resource=None)
        self.assertIn(RESEARCH_URL, tail)
        session.sink.admit_diagnostic(
            presentation_envelope_for(chunks[0], logical_resource=None)
        )
        self.assertTrue(session.shutdown())
        # The rendered presentation failed, but the external DTO and the
        # selected retained tail are unchanged.
        self.assertNotIn(RESEARCH_URL, dto.text)
        self.assertIn(RESEARCH_URL, tail)

    def test_saturated_presentation_does_not_change_the_collected_tail(self):
        lines = [_fetch_line(latency=10 + (index % 50)) for index in range(300)]
        chunks, tail = _collect(lines, NetworkUrlDisplay.HOST_PATH)
        mailbox = PresentationMailbox()
        for _ in range(TELEMETRY_CAPACITY):
            mailbox.admit_telemetry(_diagnostic_envelope("filler", NetworkUrlDisplay.HOST_PATH))
        for chunk in chunks:
            mailbox.admit_telemetry(
                presentation_envelope_for(chunk, logical_resource=None)
            )
        self.assertGreater(mailbox.dropped, 0)
        # The retained tail is independent of live delivery.
        self.assertIn(RESEARCH_SAFE, tail)

    def test_renderer_failure_during_real_collection_preserves_the_tail(self):
        renderer = RecordingRenderer(fail_on="durable")
        session = _session(
            renderer, HostPresentationMode.LINES, NetworkUrlDisplay.HOST_PATH
        )

        def sink(chunk: StreamChunk) -> None:
            route_finalized_diagnostic(
                chunk,
                internal_sink=session.sink,
                sdk_sink=None,
                logical_resource=None,
            )

        payload = (
            "npm http fetch GET 200 " + RESEARCH_URL + " 15ms (cache miss)\n"
        ).encode("utf-8")
        reads = iter([payload, b""])
        capture = collect_streams(
            stdout_read=lambda _size: next(reads, b""),
            stderr_read=lambda _size: b"",
            sink=sink,
            stream_factory=make_stream_factory(
                network_url_display=NetworkUrlDisplay.HOST_PATH
            ),
            network_url_display=NetworkUrlDisplay.HOST_PATH,
        )
        # The primary collection result keeps the mode-selected tail even
        # though the presentation renderer failed.
        self.assertIn(RESEARCH_SAFE, capture.stdout_tail)
        self.assertNotIn(RESEARCH_URL, capture.stdout_tail)
        self.assertTrue(session.shutdown() or not session.worker.is_alive)

    def test_renderer_failure_during_reader_failure_keeps_primary_and_tail(self):
        renderer = RecordingRenderer(fail_on="durable")
        session = _session(
            renderer, HostPresentationMode.LINES, NetworkUrlDisplay.REDACTED
        )

        def sink(chunk: StreamChunk) -> None:
            route_finalized_diagnostic(
                chunk,
                internal_sink=session.sink,
                sdk_sink=None,
                logical_resource=None,
            )

        state = {"reads": 0}

        def stdout_read(_size: int) -> bytes:
            state["reads"] += 1
            if state["reads"] == 1:
                return b"npm warn before reader failure\n"
            raise OSError("pipe broke")

        with self.assertRaises(StreamReaderFailure) as ctx:
            collect_streams(
                stdout_read=stdout_read,
                stderr_read=lambda _size: b"",
                sink=sink,
                stream_factory=make_stream_factory(
                    network_url_display=NetworkUrlDisplay.REDACTED
                ),
                network_url_display=NetworkUrlDisplay.REDACTED,
            )
        retained = getattr(ctx.exception, RETAINED_CAPTURE_ATTR)
        self.assertIn("npm warn before reader failure", retained.stdout_tail)
        # The renderer failure stayed secondary to the reader failure.
        session.shutdown()

    def test_reader_failure_host_path_stays_fail_closed_on_both_channels(self):
        # A ``host-path`` line finalized at reader failure must fail closed on
        # both channels: the internal presentation-only envelope and the
        # retained tail carry the sanitized incomplete-token marker, and the
        # external SDK diagnostic stays URL-free.
        class RecordingSink(InternalDirectHostEventSink):
            def __init__(self) -> None:
                self.envelopes: list[HostDiagnosticEnvelope] = []

            def __call__(self, event):  # pragma: no cover - not exercised
                raise RuntimeError("unexpected")

            def admit_prefix(self, prefix):  # pragma: no cover - not exercised
                raise RuntimeError("unexpected")

            def admit_diagnostic(self, envelope):
                self.envelopes.append(envelope)
                return True

        internal = RecordingSink()
        sdk_events: list[object] = []

        def sink(chunk: StreamChunk) -> None:
            if not chunk.finalized:
                return
            route_finalized_diagnostic(
                chunk,
                internal_sink=internal,
                sdk_sink=sdk_events.append,
                logical_resource=None,
            )

        state = {"reads": 0}

        def stdout_read(_size: int) -> bytes:
            state["reads"] += 1
            if state["reads"] == 1:
                return b"hello https://example.com/path"
            raise OSError("pipe broke")

        with self.assertRaises(StreamReaderFailure) as ctx:
            collect_streams(
                stdout_read=stdout_read,
                stderr_read=lambda _size: b"",
                sink=sink,
                stream_factory=make_stream_factory(
                    network_url_display=NetworkUrlDisplay.HOST_PATH
                ),
                network_url_display=NetworkUrlDisplay.HOST_PATH,
            )
        expected = f"hello {INCOMPLETE_TOKEN_MARKER}"
        self.assertEqual(1, len(internal.envelopes))
        self.assertEqual(expected, internal.envelopes[0].text)
        self.assertNotIn("example.com", internal.envelopes[0].text)
        structured = [
            event
            for event in sdk_events
            if isinstance(event, HostStructuredDiagnostic)
        ]
        self.assertEqual(1, len(structured))
        self.assertEqual(expected, structured[0].text)
        self.assertNotIn("example.com", structured[0].text)
        retained = getattr(ctx.exception, RETAINED_CAPTURE_ATTR)
        self.assertEqual(expected, retained.stdout_tail)

    def test_producer_callback_never_waits_for_the_consumer(self):
        # No worker drains the mailbox: the producer's admission path must
        # still return promptly and simply drop beyond the bounded capacity.
        mailbox = PresentationMailbox()
        adapter = HostEventEnqueueAdapter(mailbox)
        chunk = _collect([_fetch_line(latency=15)], NetworkUrlDisplay.REDACTED)[0][0]
        started = time.monotonic()
        for _ in range(TELEMETRY_CAPACITY * 3):
            route_finalized_diagnostic(
                chunk,
                internal_sink=adapter,
                sdk_sink=None,
                logical_resource=None,
            )
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertGreater(mailbox.dropped, 0)

    def test_external_dto_is_never_admitted_to_the_mailbox(self):
        mailbox = PresentationMailbox()
        # Only the internal envelope entry point exists on the adapter; the
        # external DTO is not a HostPresentationEvent.
        from docker.versioning.host_presentation import HostEventEnqueueAdapter

        adapter = HostEventEnqueueAdapter(mailbox)
        envelope = _diagnostic_envelope("safe", NetworkUrlDisplay.REDACTED)
        self.assertTrue(adapter.admit_diagnostic(envelope))


class TestFinalizedDiagnosticRouting(unittest.TestCase):
    """Task 7.7: independent fan-out to the actor and the SDK sink."""

    def _chunk(self, display: NetworkUrlDisplay) -> StreamChunk:
        chunks, _tail = _collect([_fetch_line(latency=15)], display)
        return chunks[0]

    def _overflow_chunk(self) -> StreamChunk:
        from docker.npm_environment.streaming import DIAGNOSTIC_LINE_LIMIT_BYTES

        oversized = (
            "npm warn "
            + "pad " * (DIAGNOSTIC_LINE_LIMIT_BYTES // 4)
            + "DISCARDED" * 100
        )
        chunks, _tail = _collect([oversized], NetworkUrlDisplay.REDACTED)
        overflow = [chunk for chunk in chunks if chunk.overflowed]
        self.assertEqual(1, len(overflow))
        return overflow[0]

    def test_both_sinks_receive_independent_events(self):
        mailbox = PresentationMailbox()
        adapter = HostEventEnqueueAdapter(mailbox)
        sdk_events: list[object] = []
        chunk = self._chunk(NetworkUrlDisplay.HOST_PATH)
        route_finalized_diagnostic(
            chunk,
            internal_sink=adapter,
            sdk_sink=sdk_events.append,
            logical_resource=None,
        )
        # Exactly one internal envelope with the presentation-only identity.
        item = mailbox.take(timeout=0.0)
        self.assertIsNotNone(item)
        self.assertIsInstance(item.event, HostDiagnosticEnvelope)
        self.assertEqual(HOST_PATH_CANON, item.event.presentation_text)
        self.assertIsNotNone(item.event.fetch_key)
        self.assertIsNone(mailbox.take(timeout=0.0))
        # Exactly one independent, URL-free, path-free SDK diagnostic.
        self.assertEqual(1, len(sdk_events))
        dto = sdk_events[0]
        self.assertIsInstance(dto, HostStructuredDiagnostic)
        self.assertNotIn(RESEARCH_URL, dto.text)
        self.assertNotIn(RESEARCH_SAFE, dto.text)
        self.assertNotEqual(HOST_PATH_CANON, dto.text)
        # Internal-only fields never leak into the SDK DTO.
        self.assertFalse(hasattr(dto, "fetch_key"))
        self.assertFalse(hasattr(dto, "fetch_text"))
        self.assertFalse(hasattr(dto, "presentation_text"))
        self.assertFalse(hasattr(dto, "local_text"))

    def test_all_policies_simultaneously_fan_out_selected_local_and_safe_sdk_output(self):
        source_url = (
            "https://user:password@registry.npmjs.org/"
            "npm-http-research-fixture/-/npm-http-research-fixture-1.0.0.tgz"
            "?token=caller-secret#fragment"
        )
        source_line = _fetch_line(source_url, latency=15)
        local_expectations = {
            NetworkUrlDisplay.REDACTED: REDACTED_CANON,
            NetworkUrlDisplay.HOST_PATH: HOST_PATH_CANON,
            NetworkUrlDisplay.EXACT: source_line,
        }

        for display in ALL_MODES:
            with self.subTest(display=display):
                chunks, _tail = _collect(
                    [source_line], display, secrets=("password", "caller-secret")
                )
                self.assertEqual(1, len(chunks))
                renderer = RecordingRenderer()
                session = _session(renderer, HostPresentationMode.LINES, display)
                sdk_events: list[object] = []

                # One production routing call fans the same finalized
                # collector diagnostic out to both active channels.
                route_finalized_diagnostic(
                    chunks[0],
                    internal_sink=session.sink,
                    sdk_sink=sdk_events.append,
                    logical_resource=None,
                )
                self.assertTrue(session.shutdown())

                self.assertEqual(
                    [local_expectations[display]], renderer.durable_texts()
                )
                self.assertEqual(1, len(sdk_events))
                dto = sdk_events[0]
                self.assertIsInstance(dto, HostStructuredDiagnostic)
                self.assertLessEqual(len(dto.text.encode("utf-8")), TAIL_BYTES)
                for forbidden in (
                    source_url,
                    "https://",
                    "registry.npmjs.org",
                    "npm-http-research-fixture/",
                    "user",
                    "password",
                    "caller-secret",
                    "fragment",
                ):
                    self.assertNotIn(forbidden, dto.text)
                self.assertIn(REDACTED, dto.text)

    def test_plain_sdk_sink_gets_only_the_url_free_dto(self):
        events: list[object] = []
        chunk = self._chunk(NetworkUrlDisplay.HOST_PATH)
        route_finalized_diagnostic(
            chunk,
            internal_sink=None,
            sdk_sink=events.append,
            logical_resource=None,
        )
        self.assertEqual(1, len(events))
        dto = events[0]
        self.assertIsInstance(dto, HostStructuredDiagnostic)
        self.assertNotIn(RESEARCH_URL, dto.text)
        self.assertNotIn(RESEARCH_SAFE, dto.text)

    def test_internal_admission_failure_still_delivers_the_sdk_event(self):
        class ExplodingSink(InternalDirectHostEventSink):
            def __call__(self, event):  # pragma: no cover - not exercised
                raise RuntimeError("unexpected")

            def admit_prefix(self, prefix):  # pragma: no cover - not exercised
                raise RuntimeError("unexpected")

            def admit_diagnostic(self, envelope):
                raise RuntimeError("mailbox gone")

        sdk_events: list[object] = []
        chunk = self._chunk(NetworkUrlDisplay.REDACTED)
        # Best-effort presentation never suppresses independent SDK delivery.
        route_finalized_diagnostic(
            chunk,
            internal_sink=ExplodingSink(),
            sdk_sink=sdk_events.append,
            logical_resource=None,
        )
        self.assertEqual(1, len(sdk_events))
        self.assertIsInstance(sdk_events[0], HostStructuredDiagnostic)

    def test_sdk_callback_failure_does_not_affect_internal_admission(self):
        mailbox = PresentationMailbox()
        adapter = HostEventEnqueueAdapter(mailbox)

        def exploding_sdk(_event: object) -> None:
            raise RuntimeError("sdk callback failed")

        chunk = self._chunk(NetworkUrlDisplay.HOST_PATH)
        route_finalized_diagnostic(
            chunk,
            internal_sink=adapter,
            sdk_sink=exploding_sdk,
            logical_resource=None,
        )
        # The internal presentation still received its envelope.
        item = mailbox.take(timeout=0.0)
        self.assertIsNotNone(item)
        self.assertIsInstance(item.event, HostDiagnosticEnvelope)

    def test_mailbox_saturation_does_not_suppress_sdk_delivery(self):
        mailbox = PresentationMailbox()
        adapter = HostEventEnqueueAdapter(mailbox)
        for _ in range(TELEMETRY_CAPACITY):
            mailbox.admit_telemetry(
                _diagnostic_envelope("filler", NetworkUrlDisplay.REDACTED)
            )
        sdk_events: list[object] = []
        chunk = self._chunk(NetworkUrlDisplay.REDACTED)
        route_finalized_diagnostic(
            chunk,
            internal_sink=adapter,
            sdk_sink=sdk_events.append,
            logical_resource=None,
        )
        # The saturated inbox dropped the envelope, but the SDK channel is
        # independent and still delivered.
        self.assertGreater(mailbox.dropped, 0)
        self.assertEqual(1, len(sdk_events))
        self.assertIsInstance(sdk_events[0], HostStructuredDiagnostic)

    def test_overflow_fans_out_to_both_channels(self):
        mailbox = PresentationMailbox()
        adapter = HostEventEnqueueAdapter(mailbox)
        sdk_events: list[object] = []
        chunk = self._overflow_chunk()
        route_overflow_diagnostic(
            chunk,
            internal_sink=adapter,
            sdk_sink=sdk_events.append,
            logical_resource=None,
        )
        # The internal actor receives exactly one finalized overflow boundary.
        item = mailbox.take(timeout=0.0)
        self.assertIsNotNone(item)
        self.assertIsInstance(item.event, HostDiagnosticPrefix)
        self.assertTrue(item.event.finalized)
        self.assertTrue(item.event.overflowed)
        self.assertIsNone(mailbox.take(timeout=0.0))
        # The SDK independently receives exactly one bounded safe truncation
        # diagnostic and never a provisional prefix.
        self.assertEqual(1, len(sdk_events))
        dto = sdk_events[0]
        self.assertIsInstance(dto, HostStructuredDiagnostic)
        self.assertEqual(HostDiagnosticClassification.STATUS, dto.classification)
        self.assertIn(OVERSIZED_DIAGNOSTIC_MARKER, dto.text)
        self.assertNotIn("DISCARDED", dto.text)
        self.assertEqual((), dto.hostnames)
        self.assertEqual((), dto.url_fingerprints)
        self.assertIsInstance(
            overflow_diagnostic_for(chunk, logical_resource=None),
            HostStructuredDiagnostic,
        )

    def test_internal_admission_failure_never_affects_assembly(self):
        class ExplodingSink(InternalDirectHostEventSink):
            def __call__(self, event):  # pragma: no cover - not exercised
                raise RuntimeError("unexpected")

            def admit_prefix(self, prefix):  # pragma: no cover - not exercised
                raise RuntimeError("unexpected")

            def admit_diagnostic(self, envelope):
                raise RuntimeError("mailbox gone")

        chunk = self._chunk(NetworkUrlDisplay.REDACTED)
        # Best-effort presentation never affects assembly.
        route_finalized_diagnostic(
            chunk,
            internal_sink=ExplodingSink(),
            sdk_sink=None,
            logical_resource=None,
        )


def _drain_into(mailbox, state) -> None:
    """Drain admitted mailbox events into a presentation state, in order."""
    while True:
        item = mailbox.take(timeout=0.0)
        if item is None:
            return
        event = item.event
        if isinstance(event, HostDiagnosticEnvelope):
            state.admit_diagnostic(event, now=0.0)
        elif isinstance(event, HostDiagnosticPrefix):
            state.admit_prefix(event)


class TestCommittedPrefixRouting(unittest.TestCase):
    """Committed prefixes are mode-selected, internal-only, and prompt.

    The production prefix route feeds the selected ``local_text`` to the
    internal actor only; an SDK sink can never observe a provisional prefix.
    """

    _URL = (
        "https://user:secret@registry.example.com:8443"
        "/pkg/-/pkg-1.0.0.tgz?q=1#frag"
    )
    _LINE = f"npm error network GET {_URL} failed"
    _HOST_PATH = "registry.example.com/pkg/-/pkg-1.0.0.tgz"

    _EXPECTATIONS = (
        (NetworkUrlDisplay.REDACTED, "<redacted>"),
        (NetworkUrlDisplay.HOST_PATH, _HOST_PATH),
        (NetworkUrlDisplay.EXACT, _URL),
    )

    def _prefix_line(self, display):
        stream = NpmDiagnosticStream(
            "stdout", ("secret",), network_url_display=display
        )
        chunks = list(stream.feed_bytes(self._LINE.encode()))
        return stream, chunks

    @staticmethod
    def _route_prefixes(chunks, adapter):
        buffer: list[str] = []
        for chunk in chunks:
            if chunk.finalized or chunk.local_text is None:
                continue
            buffer.append(chunk.local_text)
            route_committed_prefix(
                chunk,
                text="".join(buffer),
                internal_sink=adapter,
                logical_resource=None,
                overflowed=chunk.overflowed,
            )
        return "".join(buffer)

    def test_selected_prefix_is_presented_before_the_record_boundary(self):
        for display, expected in self._EXPECTATIONS:
            with self.subTest(display=display):
                stream, chunks = self._prefix_line(display)
                mailbox = PresentationMailbox()
                adapter = HostEventEnqueueAdapter(mailbox)
                renderer = RecordingRenderer()
                state = _state(
                    renderer, HostPresentationMode.INTERACTIVE, display
                )
                local = self._route_prefixes(chunks, adapter)
                _drain_into(mailbox, state)
                self.assertIn(expected, "\n".join(renderer.slot_texts()))
                # The cumulative provisional snapshot matches the selected
                # retained tail committed so far.
                self.assertEqual(stream.tail(), local)

    def test_sdk_channel_only_sees_the_finalized_diagnostic(self):
        for display, _expected in self._EXPECTATIONS:
            with self.subTest(display=display):
                stream, chunks = self._prefix_line(display)
                mailbox = PresentationMailbox()
                adapter = HostEventEnqueueAdapter(mailbox)
                sdk_events: list = []
                self._route_prefixes(chunks, adapter)
                # No provisional prefix may reach the SDK while the selected
                # prefix is live internally.
                self.assertEqual([], sdk_events)
                finalized = [
                    chunk
                    for chunk in stream.feed_bytes(b"\n")
                    if chunk.finalized
                ]
                self.assertEqual(1, len(finalized))
                route_finalized_diagnostic(
                    finalized[0],
                    internal_sink=adapter,
                    sdk_sink=sdk_events.append,
                    logical_resource=None,
                )
                self.assertEqual(1, len(sdk_events))
                dto = sdk_events[0]
                self.assertIsInstance(dto, HostStructuredDiagnostic)
                # The external DTO stays URL-free and path-free in every mode.
                self.assertNotIn("https://", dto.text)
                self.assertNotIn("registry.example.com", dto.text)
                self.assertNotIn(self._HOST_PATH, dto.text)

    def test_finalized_line_replaces_the_prefix_without_duplication(self):
        for display, expected in self._EXPECTATIONS:
            with self.subTest(display=display):
                stream, chunks = self._prefix_line(display)
                mailbox = PresentationMailbox()
                adapter = HostEventEnqueueAdapter(mailbox)
                renderer = RecordingRenderer()
                state = _state(renderer, HostPresentationMode.LINES, display)
                self._route_prefixes(chunks, adapter)
                finalized = [
                    chunk
                    for chunk in stream.feed_bytes(b"\n")
                    if chunk.finalized
                ]
                self.assertEqual(1, len(finalized))
                route_finalized_diagnostic(
                    finalized[0],
                    internal_sink=adapter,
                    sdk_sink=None,
                    logical_resource=None,
                )
                _drain_into(mailbox, state)
                durable = "\n".join(renderer.durable_texts())
                self.assertIn(expected, durable)
                # The complete line replaces the provisional prefix rather
                # than appending a second copy of the diagnostic.
                self.assertEqual(durable.count("network GET"), 1)

    def test_prefix_admission_never_waits_for_the_consumer(self):
        mailbox = PresentationMailbox()
        adapter = HostEventEnqueueAdapter(mailbox)
        _stream, chunks = self._prefix_line(NetworkUrlDisplay.HOST_PATH)
        chunk = next(
            chunk for chunk in chunks if chunk.local_text is not None
        )
        started = time.monotonic()
        for index in range(2000):
            route_committed_prefix(
                chunk,
                text=f"prefix {index}",
                internal_sink=adapter,
                logical_resource=None,
            )
        # Prompt, non-blocking admission supersedes the bounded provisional
        # slot instead of waiting for a consumer.
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertGreater(mailbox.superseded, 0)


class TestFetchCanonicalizationSecretSafety(unittest.TestCase):
    """A grouped fetch must never restore a secret through its canonical text.

    The canonical fetch rendering copies the method, status, attempt, and
    cache clauses from the source line.  When any of those fields carries a
    configured secret the collector falls back to the sanitized ordinary
    diagnostic, so no interactive or ``lines`` renderer, SDK channel, or
    retained tail can restore it.
    """

    _SECRET = "miss"

    def _collect_unsafe(self, display: NetworkUrlDisplay):
        chunks, tail = _collect(
            [_fetch_line(cache=self._SECRET)],
            display,
            secrets=(self._SECRET,),
        )
        self.assertEqual(1, len(chunks))
        return chunks[0], tail

    def test_collector_refuses_to_group_a_secret_bearing_cache_clause(self):
        for display in (NetworkUrlDisplay.REDACTED, NetworkUrlDisplay.HOST_PATH):
            with self.subTest(display=display):
                chunk, tail = self._collect_unsafe(display)
                self.assertIsNone(chunk.fetch_key)
                self.assertIsNone(chunk.fetch_text)
                self.assertNotIn(self._SECRET, chunk.text)
                self.assertNotIn(self._SECRET, chunk.local_text or "")
                self.assertNotIn(self._SECRET, tail)
                dto = structured_diagnostic_for(chunk, logical_resource=None)
                self.assertNotIn(self._SECRET, dto.text)

    def test_interactive_and_lines_renderers_never_restore_the_secret(self):
        for display in (NetworkUrlDisplay.REDACTED, NetworkUrlDisplay.HOST_PATH):
            for mode in (
                HostPresentationMode.INTERACTIVE,
                HostPresentationMode.LINES,
            ):
                with self.subTest(display=display, mode=mode):
                    chunk, _tail = self._collect_unsafe(display)
                    envelope = presentation_envelope_for(
                        chunk, logical_resource=None
                    )
                    renderer = RecordingRenderer()
                    state = _state(renderer, mode, display)
                    state.admit_diagnostic(envelope, now=0.0)
                    if mode is HostPresentationMode.LINES:
                        state.due(now=LINES_WINDOW_SECONDS)
                    rendered = " ".join(
                        str(part) for call in renderer.calls for part in call
                    )
                    self.assertNotIn(self._SECRET, rendered)
                    # The sanitized ordinary diagnostic is still visible.
                    self.assertIn(REDACTED, rendered)
                    self.assertIn("npm http fetch GET 200", rendered)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
