"""Phase 9 renderer, formatting, mailbox, and worker lifecycle contracts.

These tests bind the RED deliverables for tasks 9.1-9.10 of
``improve-host-build-observability``: facade mode selection, human-readable
formatting without activity claims, hostname policy, interactive/``lines``
coalescing, the ordered bounded inbox, the single presentation actor, and
contextual failure reports.  Terminal rendering is exercised through a
recording renderer so the exact ordered call sequence is observable; real
writes live behind :class:`TerminalHostRenderer`.
"""
from __future__ import annotations

import io
import os
import threading
import time
import unittest
from unittest import mock

from docker.versioning.host_presentation import (
    DIAGNOSTIC_SILENCE_SECONDS,
    FIRST_HEARTBEAT_SECONDS,
    INTERACTIVE_REFRESH_SECONDS,
    LINES_STATUS_INTERVAL_SECONDS,
    OMISSION_COUNTER_LIMIT,
    AdmittedEvent,
    PresentationLane,
    PresentationMailbox,
    PresentationPlan,
    PresentationSelection,
    PresentationWorker,
    HostEventEnqueueAdapter,
    HostPresentationMode,
    HostPresentationSession,
    HostPresentationState,
    TerminalHostRenderer,
    control_reservation,
    format_elapsed,
    format_failure_report,
    format_heartbeat,
    select_presentation,
)
from docker.versioning.host_progress import (
    HostBuildEvent,
    HostDiagnosticEvent,
    HostDiagnosticStream,
    HostHeartbeatEvent,
    InternalDirectHostEventSink,
    HostLastActivityKind,
    HostPhase,
    HostPhaseEvent,
    HostPhaseState,
    HostStep,
    HostStepEvent,
    HostStepState,
    HostStructuredDiagnostic,
    HostTransportProgressEvent,
    HostDiagnosticClassification,
)


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


def _heartbeat(**overrides: object) -> HostHeartbeatEvent:
    values: dict[str, object] = {
        "phase": HostPhase.LOCKED_ASSEMBLY,
        "step": HostStep.NPM_EXECUTION,
        "elapsed_seconds": 3,
        "expects_diagnostic_stream": True,
    }
    values.update(overrides)
    return HostHeartbeatEvent(**values)  # type: ignore[arg-type]


class RecordingRenderer:
    """Renderer that records the exact ordered worker call sequence."""

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
        self._record("finalize", text, restore_status)

    def kinds(self) -> list[object]:
        return [call[0] for call in self.calls]


def _assert_no_worker(case: unittest.TestCase) -> None:
    live = {thread.name for thread in threading.enumerate()}
    case.assertNotIn("host-presentation", live)


class TestPresentationSelection(unittest.TestCase):
    def test_json_never_authorizes_a_live_sink(self):
        for mode in ("interactive", "lines", "off"):
            plan = select_presentation(mode, text_output=False, stderr_is_tty=True)
            self.assertEqual(PresentationSelection.NONE, plan.selection)
            self.assertEqual(HostPresentationMode(mode), plan.mode)

    def test_interactive_text_authorizes_a_live_sink_for_every_mode(self):
        for mode in ("interactive", "lines", "off"):
            plan = select_presentation(mode, text_output=True, stderr_is_tty=True)
            self.assertTrue(plan.live_sink)
            self.assertEqual(HostPresentationMode(mode), plan.mode)

    def test_noninteractive_text_needs_explicit_lines(self):
        self.assertTrue(
            select_presentation(
                "lines", text_output=True, stderr_is_tty=False
            ).live_sink
        )
        self.assertFalse(
            select_presentation(
                "interactive", text_output=True, stderr_is_tty=False
            ).live_sink
        )
        self.assertFalse(
            select_presentation(
                "off", text_output=True, stderr_is_tty=False
            ).live_sink
        )

    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            select_presentation("verbose", text_output=True, stderr_is_tty=True)


class TestElapsedFormatting(unittest.TestCase):
    def test_human_readable_boundaries(self):
        self.assertEqual("3s", format_elapsed(3))
        self.assertEqual("59s", format_elapsed(59))
        self.assertEqual("1m 00s", format_elapsed(60))
        self.assertEqual("2m 05s", format_elapsed(125))
        self.assertEqual("1h 01m", format_elapsed(3660))

    def test_negative_is_rejected(self):
        with self.assertRaises(ValueError):
            format_elapsed(-1)


class TestHeartbeatFormatting(unittest.TestCase):
    def test_reports_only_present_facts(self):
        text = format_heartbeat(_heartbeat())
        self.assertIn("npm execution: 3s", text)

    def test_includes_silence_activity_and_deadline(self):
        text = format_heartbeat(
            _heartbeat(
                elapsed_seconds=130,
                diagnostic_silence_seconds=127,
                last_activity_kind=HostLastActivityKind.DIAGNOSTIC,
                last_activity_age_seconds=127,
                remaining_deadline_seconds=1700,
            )
        )
        self.assertIn("no diagnostics for 2m 07s", text)
        self.assertIn("last diagnostic 2m 07s ago", text)
        self.assertIn("28m 20s remaining", text)

    def test_reports_byte_progress_without_claiming_downloading(self):
        text = format_heartbeat(
            _heartbeat(
                expects_diagnostic_stream=False,
                diagnostic_silence_seconds=None,
                last_activity_kind=HostLastActivityKind.TRANSPORT_PROGRESS,
                last_activity_age_seconds=2,
            ),
            received_bytes=4096,
        )
        self.assertIn("4096 bytes received", text)
        self.assertIn("last byte progress 2s ago", text)
        self.assertNotIn("download", text.lower())
        self.assertNotIn("network", text.lower())

    def test_bytes_are_omitted_when_unobserved(self):
        self.assertNotIn("bytes", format_heartbeat(_heartbeat()))


class TestFailureReport(unittest.TestCase):
    def test_identifies_phase_step_and_nonempty_tail_only(self):
        without = format_failure_report(
            HostPhase.LOCKED_ASSEMBLY, HostStep.NPM_EXECUTION
        )
        self.assertIn("locked assembly", without)
        self.assertIn("npm execution", without)
        self.assertNotIn("Last diagnostics", without)
        with_tail = format_failure_report(
            HostPhase.LOCKED_ASSEMBLY,
            HostStep.NPM_EXECUTION,
            tail="npm error boom",
            logical_resource="pi assembler",
        )
        self.assertIn("logical asset: pi assembler", with_tail)
        self.assertIn(
            "Retained diagnostics (may repeat live output):\nnpm error boom",
            with_tail,
        )

    def test_hostnames_only_when_enabled(self):
        hidden = format_failure_report(
            HostPhase.RELEASE_ACQUISITION,
            HostStep.ARTIFACT_ACQUISITION,
            hostnames=("registry.example.com",),
            show_network_hosts=False,
        )
        shown = format_failure_report(
            HostPhase.RELEASE_ACQUISITION,
            HostStep.ARTIFACT_ACQUISITION,
            hostnames=("registry.example.com",),
            show_network_hosts=True,
        )
        self.assertNotIn("registry.example.com", hidden)
        self.assertIn("registry.example.com", shown)




class TestOrderedInbox(unittest.TestCase):
    def test_single_fifo_preserves_control_and_telemetry_admission_order(self):
        mailbox = PresentationMailbox(capacity=2, control_capacity=2)
        first = _diagnostic("first")
        terminal = HostStepEvent(
            HostPhase.LOCKED_ASSEMBLY,
            HostStep.NPM_EXECUTION,
            HostStepState.SUCCEEDED,
            True,
        )
        second = _diagnostic("second")
        self.assertTrue(mailbox.admit_telemetry(first))
        self.assertTrue(mailbox.admit_control(terminal))
        self.assertTrue(mailbox.admit_telemetry(second))
        self.assertEqual([first, terminal, second], [mailbox.take(0).event for _ in range(3)])

    def test_diagnostics_cannot_consume_control_reservation(self):
        mailbox = PresentationMailbox(capacity=1, control_capacity=1)
        self.assertTrue(mailbox.admit_telemetry(_diagnostic("kept")))
        self.assertFalse(mailbox.admit_telemetry(_diagnostic("dropped")))
        terminal = HostStepEvent(
            HostPhase.LOCKED_ASSEMBLY,
            HostStep.NPM_EXECUTION,
            HostStepState.SUCCEEDED,
            True,
        )
        self.assertTrue(mailbox.admit_control(terminal))
        self.assertEqual("[1 diagnostics omitted]", mailbox.consume_omission_notice())

    def test_saturated_omission_count_uses_a_lower_bound(self):
        mailbox = PresentationMailbox(capacity=1, control_capacity=1)
        self.assertTrue(mailbox.admit_telemetry(_diagnostic("kept")))
        for index in range(OMISSION_COUNTER_LIMIT + 1):
            self.assertFalse(mailbox.admit_telemetry(_diagnostic(f"drop {index}")))
        self.assertEqual(
            f"[at least {OMISSION_COUNTER_LIMIT} diagnostics omitted]",
            mailbox.consume_omission_notice(),
        )

    def test_supported_success_and_failure_transcripts_fit_while_stalled(self):
        from docker.versioning.host_presentation import PresentationFinalReport

        controls: list[object] = []
        for _ in range(4):
            controls.extend((
                HostStepEvent(
                    HostPhase.RELEASE_ACQUISITION,
                    HostStep.ARTIFACT_ACQUISITION,
                    HostStepState.STARTED,
                    False,
                ),
                HostStepEvent(
                    HostPhase.RELEASE_ACQUISITION,
                    HostStep.CACHE_REUSE,
                    HostStepState.SUCCEEDED,
                    False,
                ),
                HostStepEvent(
                    HostPhase.RELEASE_ACQUISITION,
                    HostStep.ARTIFACT_ACQUISITION,
                    HostStepState.SUCCEEDED,
                    False,
                ),
            ))
        for _ in range(3):
            controls.extend((
                HostStepEvent(
                    HostPhase.RELEASE_ACQUISITION,
                    HostStep.RELEASE_ACQUISITION,
                    HostStepState.STARTED,
                    False,
                ),
                HostStepEvent(
                    HostPhase.RELEASE_ACQUISITION,
                    HostStep.RELEASE_ACQUISITION,
                    HostStepState.SUCCEEDED,
                    False,
                ),
            ))
        assembly_steps = (
            HostStep.LOCK_WAIT,
            HostStep.CACHE_LOOKUP,
            HostStep.STALE_STAGE_CLEANUP,
            HostStep.CONTAINER_STARTUP,
            HostStep.NPM_EXECUTION,
            HostStep.VALIDATION,
            HostStep.PUBLICATION,
        )
        for step in assembly_steps:
            controls.extend((
                HostStepEvent(
                    HostPhase.LOCKED_ASSEMBLY, step, HostStepState.STARTED,
                    step is HostStep.NPM_EXECUTION,
                ),
                HostStepEvent(
                    HostPhase.LOCKED_ASSEMBLY, step, HostStepState.SUCCEEDED,
                    step is HostStep.NPM_EXECUTION,
                ),
            ))
        for phase in HostPhase:
            controls.extend((
                HostPhaseEvent(phase, HostPhaseState.STARTED),
                HostPhaseEvent(phase, HostPhaseState.SUCCEEDED),
            ))

        self.assertEqual(40, len(controls))
        success_mailbox = PresentationMailbox(capacity=1)
        for event in controls:
            self.assertTrue(success_mailbox.admit_control(event))

        failure_controls = [
            *controls[:-1],
            HostPhaseEvent(HostPhase.DOCKER_TRANSITION, HostPhaseState.FAILED),
            PresentationFinalReport("final failure report"),
        ]
        self.assertEqual(41, len(failure_controls))
        failure_mailbox = PresentationMailbox(capacity=1)
        for event in failure_controls:
            self.assertTrue(failure_mailbox.admit_control(event))
        self.assertEqual(41, control_reservation())

    def test_control_exhaustion_aborts_without_changing_domain_result(self):
        mailbox = PresentationMailbox(capacity=1, control_capacity=1)
        started = HostPhaseEvent(HostPhase.LOCKED_ASSEMBLY, HostPhaseState.STARTED)
        failed = HostPhaseEvent(HostPhase.LOCKED_ASSEMBLY, HostPhaseState.FAILED)
        self.assertTrue(mailbox.admit_control(started))
        self.assertFalse(mailbox.admit_control(failed))
        self.assertTrue(mailbox.admission_failed)
        mailbox.close()

    def test_close_is_capacity_independent_idempotent_and_rejects_late_admission(self):
        mailbox = PresentationMailbox(capacity=1, control_capacity=1)
        event = _diagnostic("kept")
        self.assertTrue(mailbox.admit_telemetry(event))
        mailbox.close()
        mailbox.close()
        self.assertFalse(mailbox.admit_telemetry(_diagnostic("late")))
        self.assertIs(event, mailbox.take(0).event)
        self.assertIsNone(mailbox.take(0))

    def test_short_mutex_contention_waits_for_lock_not_emergency(self):
        mailbox = PresentationMailbox(capacity=1, control_capacity=1)
        terminal = HostPhaseEvent(HostPhase.LOCKED_ASSEMBLY, HostPhaseState.SUCCEEDED)
        acquired = threading.Event()
        release = threading.Event()
        def holder():
            with mailbox._condition:
                acquired.set()
                release.wait(1)
        thread = threading.Thread(target=holder)
        thread.start(); self.assertTrue(acquired.wait(1))
        result = {}
        producer = threading.Thread(target=lambda: result.setdefault("ok", mailbox.admit_control(terminal)))
        producer.start(); time.sleep(0.01)
        self.assertTrue(producer.is_alive())
        release.set(); producer.join(1); thread.join(1)
        self.assertTrue(result["ok"])
        self.assertFalse(mailbox.admission_failed)


class TestCapacityIndependentSessionClose(unittest.TestCase):
    class GatedStream:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str | None]] = []
            self.write_entered = threading.Event()
            self.flush_entered = threading.Event()
            self.release_write = threading.Event()
            self.release_flush = threading.Event()
            self._gate_write = False
            self._gate_flush = False

        def gate_next_write(self) -> None:
            self._gate_write = True

        def gate_next_flush(self) -> None:
            self._gate_flush = True

        def write(self, text: str) -> int:
            self.calls.append(("write", text))
            if self._gate_write:
                self._gate_write = False
                self.write_entered.set()
                self.release_write.wait(2)
            return len(text)

        def flush(self) -> None:
            self.calls.append(("flush", None))
            if self._gate_flush:
                self._gate_flush = False
                self.flush_entered.set()
                self.release_flush.wait(2)

    def test_sink_is_direct_thread_safe_mailbox_adapter(self):
        session = HostPresentationSession(
            RecordingRenderer(),
            PresentationPlan(HostPresentationMode.LINES, PresentationSelection.LIVE),
        )
        self.assertIsInstance(session.sink, HostEventEnqueueAdapter)
        self.assertIsInstance(session.sink, InternalDirectHostEventSink)

        entered = 0
        peak_concurrency = 0
        calls: list[HostBuildEvent] = []
        counter_lock = threading.Lock()
        concurrent_calls = threading.Barrier(2)
        try_admit = session.mailbox.try_admit

        def observe_admission(event: HostBuildEvent) -> bool:
            nonlocal entered, peak_concurrency
            with counter_lock:
                entered += 1
                peak_concurrency = max(peak_concurrency, entered)
            try:
                concurrent_calls.wait(1)
                calls.append(event)
                return try_admit(event)
            finally:
                with counter_lock:
                    entered -= 1

        with mock.patch.object(session.mailbox, "try_admit", observe_admission):
            events = (_diagnostic("producer one"), _diagnostic("producer two"))
            producers = [
                threading.Thread(target=session.sink, args=(event,))
                for event in events
            ]
            for producer in producers:
                producer.start()
            for producer in producers:
                producer.join(2)

        self.assertTrue(all(not producer.is_alive() for producer in producers))
        self.assertEqual(2, peak_concurrency)
        self.assertCountEqual(events, calls)
        self.assertTrue(session.shutdown())

    def test_normal_close_drains_and_joins(self):
        renderer = RecordingRenderer()
        session = HostPresentationSession(
            renderer,
            PresentationPlan(HostPresentationMode.LINES, PresentationSelection.LIVE),
        )
        session.sink(_diagnostic("before close"))
        self.assertTrue(session.shutdown())
        self.assertFalse(session.worker.is_alive)
        self.assertIn(("durable", "before close"), renderer.calls)
        self.assertTrue(session.shutdown())

    def test_renderer_exception_does_not_change_completion(self):
        session = HostPresentationSession(
            RecordingRenderer(fail_on="durable"),
            PresentationPlan(HostPresentationMode.LINES, PresentationSelection.LIVE),
        )
        session.sink(_diagnostic("failure"))
        self.assertTrue(session.shutdown())
        self.assertFalse(session.worker.is_alive)

    def test_control_exhaustion_cancels_after_blocked_clear(self):
        clear_entered = threading.Event()
        release_clear = threading.Event()
        calls: list[tuple[str, str | None]] = []

        class GatedClearRenderer(RecordingRenderer):
            def clear_all(self) -> None:
                calls.append(("clear_all", None))
                clear_entered.set()
                release_clear.wait(2)

            def durable(self, text: str) -> None:
                calls.append(("durable", text))

        session = HostPresentationSession(
            GatedClearRenderer(),
            PresentationPlan(HostPresentationMode.INTERACTIVE, PresentationSelection.LIVE),
        )
        terminal = HostPhaseEvent(
            HostPhase.LOCKED_ASSEMBLY, HostPhaseState.SUCCEEDED
        )
        try:
            session.sink(terminal)
            self.assertTrue(clear_entered.wait(1))
            for _ in range(session.mailbox.control_capacity):
                self.assertTrue(session.mailbox.admit_control(terminal))
            self.assertFalse(session.mailbox.admit_control(terminal))
            self.assertTrue(session.mailbox.admission_failed)
            self.assertTrue(session._cancellation.is_set())
        finally:
            release_clear.set()
            session.worker.join(1)

        self.assertFalse(session.worker.is_alive)
        self.assertEqual([("clear_all", None)], calls)

    def test_production_renderer_does_not_flush_after_blocked_write_is_cancelled(self):
        stream = self.GatedStream()
        stream.gate_next_write()
        renderer = TerminalHostRenderer(stream)
        with mock.patch(
            "docker.versioning.host_presentation.WORKER_JOIN_SECONDS", 0.05
        ):
            session = HostPresentationSession(
                renderer,
                PresentationPlan(HostPresentationMode.LINES, PresentationSelection.LIVE),
            )
            try:
                session.sink(_diagnostic("blocked production write"))
                self.assertTrue(stream.write_entered.wait(1))
                self.assertFalse(session.shutdown())
                calls_at_timeout = list(stream.calls)
                self.assertEqual(1, len(calls_at_timeout))
            finally:
                stream.release_write.set()
                stream.release_flush.set()
                session.worker.join(1)

        self.assertFalse(session.worker.is_alive)
        self.assertEqual(calls_at_timeout, stream.calls)
        self.assertFalse(session.shutdown())

    def test_production_renderer_stops_after_inflight_clear_flush(self):
        stream = self.GatedStream()
        renderer = TerminalHostRenderer(stream, terminal_width=lambda: 80)
        renderer.set_status("active status")
        session = HostPresentationSession(
            renderer,
            PresentationPlan(
                HostPresentationMode.INTERACTIVE, PresentationSelection.LIVE
            ),
        )
        try:
            session.sink(_diagnostic("first diagnostic"))
            deadline = time.monotonic() + 1
            while len(stream.calls) < 4 and time.monotonic() < deadline:
                time.sleep(0.001)
            self.assertGreaterEqual(len(stream.calls), 4)

            stream.gate_next_flush()
            session.sink(_diagnostic("second diagnostic"))
            self.assertTrue(stream.flush_entered.wait(1))
            with mock.patch(
                "docker.versioning.host_presentation.WORKER_JOIN_SECONDS", 0.05
            ):
                self.assertFalse(session.shutdown())
            calls_at_timeout = list(stream.calls)
        finally:
            stream.release_write.set()
            stream.release_flush.set()
            session.worker.join(1)

        self.assertFalse(session.worker.is_alive)
        self.assertEqual(calls_at_timeout, stream.calls)
        self.assertFalse(
            any(
                call == ("write", "first diagnostic\n")
                or call == ("write", "active status\n")
                for call in stream.calls
            )
        )

    def test_stalled_renderer_uses_one_budget_and_starts_no_later_write(self):
        entered = threading.Event()
        release = threading.Event()

        class GatedRenderer(RecordingRenderer):
            def durable(self, text: str) -> None:
                self._record("durable", text)
                entered.set()
                release.wait(2)

        renderer = GatedRenderer()
        with mock.patch(
            "docker.versioning.host_presentation.WORKER_JOIN_SECONDS", 0.05
        ):
            session = HostPresentationSession(
                renderer,
                PresentationPlan(
                    HostPresentationMode.LINES, PresentationSelection.LIVE
                ),
            )
            session.sink(_diagnostic("blocked"))
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            self.assertFalse(session.shutdown())
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertTrue(session.worker.is_alive)
            calls_at_return = list(renderer.calls)
            release.set()
            session.worker.join(1)
            self.assertFalse(session.worker.is_alive)
            self.assertEqual(calls_at_return, renderer.calls)
            started = time.monotonic()
            self.assertFalse(session.shutdown())
            self.assertLess(time.monotonic() - started, 0.05)

class TestInteractiveState(unittest.TestCase):
    def _state(self, renderer: RecordingRenderer) -> HostPresentationState:
        return HostPresentationState(
            renderer, mode=HostPresentationMode.INTERACTIVE
        )

    def test_first_diagnostic_is_mutable_only(self):
        renderer = RecordingRenderer()
        state = self._state(renderer)
        state.admit_diagnostic(_diagnostic("npm warn slow"), now=0.0)
        self.assertEqual([("slot", "npm warn slow")], renderer.calls)
        self.assertNotIn("durable", renderer.kinds())

    def test_identical_repeats_update_slot_at_refresh(self):
        renderer = RecordingRenderer()
        state = self._state(renderer)
        state.admit_diagnostic(_diagnostic("retry"), now=0.0)
        state.admit_diagnostic(_diagnostic("retry"), now=0.1)
        # Not durable before the refresh deadline.
        self.assertNotIn("durable", renderer.kinds())
        state.due(now=1.1)
        self.assertIn(
            ("slot", "retry (repeated 2 times)"), renderer.calls
        )
        self.assertNotIn("durable", renderer.kinds())

    def test_numeric_variant_replaces_slot_without_suffix(self):
        renderer = RecordingRenderer()
        state = self._state(renderer)
        state.admit_diagnostic(_diagnostic("downloaded 1 files"), now=0.0)
        state.admit_diagnostic(_diagnostic("downloaded 2 files"), now=0.1)
        self.assertIn(("slot", "downloaded 2 files"), renderer.calls)
        self.assertNotIn("durable", renderer.kinds())

    def test_continuous_repeats_do_not_postpone_the_interactive_refresh(self):
        renderer = RecordingRenderer()
        state = self._state(renderer)
        state.admit_diagnostic(_diagnostic("retry"), now=0.0)
        fixed = state._refresh_deadline
        self.assertEqual(INTERACTIVE_REFRESH_SECONDS, fixed)
        for index in range(1, 10):
            state.admit_diagnostic(_diagnostic("retry"), now=0.05 * index)
            # The already-scheduled refresh is preserved, never postponed.
            self.assertEqual(fixed, state._refresh_deadline)
        state.due(now=fixed)
        self.assertIn(("slot", "retry (repeated 10 times)"), renderer.calls)

    def test_different_diagnostic_finalizes_once_then_new_slot(self):
        renderer = RecordingRenderer()
        state = self._state(renderer)
        state.admit_diagnostic(_diagnostic("alpha"), now=0.0)
        renderer.calls.clear()
        state.observe_heartbeat(_heartbeat(elapsed_seconds=3), now=3.0)
        renderer.calls.clear()
        state.admit_diagnostic(_diagnostic("beta"), now=3.1)
        self.assertEqual(
            [
                ("finalize", "alpha", "npm execution: 3s"),
                ("slot", "beta"),
            ],
            renderer.calls,
        )

    def test_terminal_does_not_restore_obsolete_status(self):
        renderer = RecordingRenderer()
        state = self._state(renderer)
        state.admit_diagnostic(_diagnostic("alpha"), now=0.0)
        state.observe_heartbeat(_heartbeat(elapsed_seconds=3), now=3.0)
        renderer.calls.clear()
        state.observe_step(
            HostStepEvent(
                HostPhase.LOCKED_ASSEMBLY,
                HostStep.NPM_EXECUTION,
                HostStepState.SUCCEEDED,
                True,
            ),
            now=4.0,
        )
        self.assertEqual(
            [
                ("finalize", "alpha", None),
                ("clear_all",),
                ("durable", "npm execution: succeeded"),
            ],
            renderer.calls,
        )

    def test_renderer_failure_disables_rendering_without_raising(self):
        renderer = RecordingRenderer(fail_on="slot")
        state = self._state(renderer)
        state.admit_diagnostic(_diagnostic("alpha"), now=0.0)
        self.assertTrue(state.rendering_failed)
        # Later events never call the failed renderer again.
        renderer.calls.clear()
        state.admit_diagnostic(_diagnostic("beta"), now=0.1)
        self.assertEqual([], renderer.calls)


class TestLinesState(unittest.TestCase):
    def _state(self, renderer: RecordingRenderer) -> HostPresentationState:
        return HostPresentationState(renderer, mode=HostPresentationMode.LINES)

    def test_first_status_at_three_seconds_then_thirty(self):
        renderer = RecordingRenderer()
        state = self._state(renderer)
        state.observe_heartbeat(_heartbeat(elapsed_seconds=3), now=3.0)
        state.observe_heartbeat(_heartbeat(elapsed_seconds=4), now=4.0)
        state.observe_heartbeat(_heartbeat(elapsed_seconds=33), now=33.0)
        durables = [call for call in renderer.calls if call[0] == "durable"]
        self.assertEqual(2, len(durables))

    def test_silence_transition_is_distinct_and_restarts_interval(self):
        renderer = RecordingRenderer()
        state = self._state(renderer)
        state.observe_heartbeat(_heartbeat(elapsed_seconds=3), now=3.0)
        state.observe_heartbeat(
            _heartbeat(
                elapsed_seconds=120,
                diagnostic_silence_seconds=120,
            ),
            now=120.0,
        )
        self.assertIn("diagnostic silence 2m 00s", renderer.calls[-1][1])
        state.observe_heartbeat(_heartbeat(elapsed_seconds=125), now=125.0)
        # The 30s interval restarts from the transition.
        self.assertNotIn(
            "npm execution: 2m 05s",
            [call[1] for call in renderer.calls if call[0] == "durable"],
        )

    def test_step_start_discards_previous_step_byte_progress(self):
        renderer = RecordingRenderer()
        state = self._state(renderer)
        # Step A observes streamed transport progress.
        state.observe_progress(
            HostTransportProgressEvent(
                HostPhase.LOCKED_ASSEMBLY,
                HostStep.RELEASE_ACQUISITION,
                4096,
            ),
            now=0.0,
        )
        state.observe_heartbeat(
            _heartbeat(step=HostStep.RELEASE_ACQUISITION, elapsed_seconds=3),
            now=3.0,
        )
        # Step B starts and heartbeats before observing any progress.
        state.observe_step(
            HostStepEvent(
                HostPhase.LOCKED_ASSEMBLY,
                HostStep.NPM_EXECUTION,
                HostStepState.STARTED,
                True,
            ),
            now=4.0,
        )
        state.observe_heartbeat(
            _heartbeat(step=HostStep.NPM_EXECUTION, elapsed_seconds=35),
            now=35.0,
        )
        durables = [call[1] for call in renderer.calls if call[0] == "durable"]
        # Step A's heartbeat did carry the bytes it observed.
        self.assertTrue(
            any("4096 bytes received" in line for line in durables), durables
        )
        # Step B never observed bytes, so none of its output may show them.
        step_b = [line for line in durables if line.startswith("npm execution:")]
        self.assertTrue(step_b, durables)
        for line in step_b:
            self.assertNotIn("4096 bytes received", line)

    def test_step_start_resets_silence_transition(self):
        renderer = RecordingRenderer()
        state = self._state(renderer)
        state.observe_heartbeat(
            _heartbeat(
                step=HostStep.RELEASE_ACQUISITION,
                elapsed_seconds=120,
                diagnostic_silence_seconds=120,
            ),
            now=120.0,
        )
        state.observe_step(
            HostStepEvent(
                HostPhase.LOCKED_ASSEMBLY,
                HostStep.NPM_EXECUTION,
                HostStepState.STARTED,
                True,
            ),
            now=121.0,
        )
        state.observe_heartbeat(
            _heartbeat(
                step=HostStep.NPM_EXECUTION,
                elapsed_seconds=200,
                diagnostic_silence_seconds=150,
            ),
            now=200.0,
        )
        silence_lines = [
            call[1]
            for call in renderer.calls
            if call[0] == "durable" and "diagnostic silence" in call[1]
        ]
        # Each consecutive step announces its own silence transition.
        self.assertEqual(
            [
                "release acquisition: diagnostic silence 2m 00s",
                "npm execution: diagnostic silence 2m 30s",
            ],
            silence_lines,
        )

    def test_repeated_silence_transitions_each_render_once(self):
        renderer = RecordingRenderer()
        state = self._state(renderer)
        # First silence cycle reaches the threshold.
        state.observe_heartbeat(
            _heartbeat(elapsed_seconds=120, diagnostic_silence_seconds=120),
            now=120.0,
        )
        # Diagnostic output resumes: silence is absent again.
        state.observe_heartbeat(_heartbeat(elapsed_seconds=121), now=121.0)
        # A later silence cycle reaches the threshold again.
        state.observe_heartbeat(
            _heartbeat(elapsed_seconds=300, diagnostic_silence_seconds=120),
            now=300.0,
        )
        silence_lines = [
            call[1]
            for call in renderer.calls
            if call[0] == "durable" and "diagnostic silence" in call[1]
        ]
        self.assertEqual(
            ["npm execution: diagnostic silence 2m 00s"] * 2, silence_lines
        )
        # The second transition restarted the 30s line interval at now=300.
        before = len([call for call in renderer.calls if call[0] == "durable"])
        state.observe_heartbeat(_heartbeat(elapsed_seconds=325), now=325.0)
        self.assertEqual(
            before,
            len([call for call in renderer.calls if call[0] == "durable"]),
        )
        state.observe_heartbeat(_heartbeat(elapsed_seconds=331), now=331.0)
        self.assertEqual(
            before + 1,
            len([call for call in renderer.calls if call[0] == "durable"]),
        )

    def test_changed_numeric_values_are_separate_durable_lines(self):
        renderer = RecordingRenderer()
        state = self._state(renderer)
        state.admit_diagnostic(_diagnostic("fetched 1 files"), now=0.0)
        state.admit_diagnostic(_diagnostic("fetched 2 files"), now=0.1)
        durables = [call[1] for call in renderer.calls if call[0] == "durable"]
        self.assertEqual(["fetched 1 files", "fetched 2 files"], durables)

    def test_exact_repeats_share_one_window_summary(self):
        renderer = RecordingRenderer()
        state = self._state(renderer)
        state.admit_diagnostic(_diagnostic("retry"), now=0.0)
        state.admit_diagnostic(_diagnostic("retry"), now=0.1)
        state.due(now=1.1)
        durables = [call[1] for call in renderer.calls if call[0] == "durable"]
        self.assertEqual(
            ["retry", "retry (repeated 2 times)"], durables
        )

    def test_continuous_repeats_do_not_postpone_the_lines_window(self):
        renderer = RecordingRenderer()
        state = self._state(renderer)
        state.admit_diagnostic(_diagnostic("retry"), now=0.0)
        fixed = state.next_deadline
        self.assertIsNotNone(fixed)
        for index in range(1, 10):
            state.admit_diagnostic(_diagnostic("retry"), now=0.05 * index)
            # The window started when the group began and stays fixed.
            self.assertEqual(fixed, state.next_deadline)
        state.due(now=fixed)
        durables = [call[1] for call in renderer.calls if call[0] == "durable"]
        self.assertEqual(["retry", "retry (repeated 10 times)"], durables)

    def test_window_expiry_then_another_diagnostic_does_not_replay_summary(self):
        renderer = RecordingRenderer()
        state = self._state(renderer)
        state.admit_diagnostic(_diagnostic("retry"), now=0.0)
        state.admit_diagnostic(_diagnostic("retry"), now=0.1)
        state.due(now=1.1)
        # The completed group was cleared, so a later diagnostic starts fresh
        # and cannot finalize the already-summarized group a second time.
        state.admit_diagnostic(_diagnostic("other"), now=1.2)
        durables = [call[1] for call in renderer.calls if call[0] == "durable"]
        self.assertEqual(
            ["retry", "retry (repeated 2 times)", "other"], durables
        )

    def test_window_expiry_summary_is_not_replayed_at_terminal_or_shutdown(self):
        renderer = RecordingRenderer()
        state = self._state(renderer)
        state.admit_diagnostic(_diagnostic("retry"), now=0.0)
        state.admit_diagnostic(_diagnostic("retry"), now=0.1)
        state.due(now=1.1)
        state.observe_step(
            HostStepEvent(
                HostPhase.LOCKED_ASSEMBLY,
                HostStep.NPM_EXECUTION,
                HostStepState.SUCCEEDED,
                True,
            ),
            now=1.2,
        )
        # Shutdown/terminal group finalization must find nothing to replay.
        state.finalize_group()
        replayed = [
            call
            for call in renderer.calls
            if len(call) > 1 and call[1] == "retry (repeated 2 times)"
        ]
        self.assertEqual(1, len(replayed))
        durables = [call[1] for call in renderer.calls if call[0] == "durable"]
        self.assertIn("npm execution: succeeded", durables)

    def test_consecutive_steps_each_render_first_status_at_three_seconds(self):
        renderer = RecordingRenderer()
        state = self._state(renderer)
        state.observe_step(
            HostStepEvent(
                HostPhase.LOCKED_ASSEMBLY,
                HostStep.RELEASE_ACQUISITION,
                HostStepState.STARTED,
                True,
            ),
            now=0.5,
        )
        state.observe_heartbeat(
            _heartbeat(step=HostStep.RELEASE_ACQUISITION, elapsed_seconds=3),
            now=3.0,
        )
        renderer.calls.clear()
        state.observe_step(
            HostStepEvent(
                HostPhase.LOCKED_ASSEMBLY,
                HostStep.NPM_EXECUTION,
                HostStepState.STARTED,
                True,
            ),
            now=3.2,
        )
        state.observe_heartbeat(
            _heartbeat(step=HostStep.NPM_EXECUTION, elapsed_seconds=3),
            now=6.2,
        )
        durables = [call[1] for call in renderer.calls if call[0] == "durable"]
        self.assertIn("npm execution: 3s", durables)
        # The new step's own 30-second cadence still governs later statuses.
        before = len([call for call in renderer.calls if call[0] == "durable"])
        state.observe_heartbeat(
            _heartbeat(step=HostStep.NPM_EXECUTION, elapsed_seconds=4),
            now=7.2,
        )
        self.assertEqual(
            before,
            len([call for call in renderer.calls if call[0] == "durable"]),
        )
        state.observe_heartbeat(
            _heartbeat(step=HostStep.NPM_EXECUTION, elapsed_seconds=33),
            now=36.2,
        )
        self.assertEqual(
            before + 1,
            len([call for call in renderer.calls if call[0] == "durable"]),
        )


class TestOffState(unittest.TestCase):
    def test_off_ignores_heartbeats_but_renders_diagnostics(self):
        renderer = RecordingRenderer()
        state = HostPresentationState(renderer, mode=HostPresentationMode.OFF)
        state.observe_heartbeat(_heartbeat(elapsed_seconds=3), now=3.0)
        self.assertEqual([], renderer.calls)
        state.admit_diagnostic(_diagnostic("npm error x"), now=3.1)
        self.assertEqual([("durable", "npm error x")], renderer.calls)


class TestStreamingListener(unittest.TestCase):
    def test_interactive_width_uses_renderer_stream_descriptor(self):
        class NarrowStderr(io.StringIO):
            def fileno(self) -> int:
                return 19

        stderr = NarrowStderr()
        with (
            mock.patch("sys.stdout", io.StringIO()),
            mock.patch(
                "docker.versioning.host_presentation.os.get_terminal_size",
                return_value=os.terminal_size((8, 24)),
            ) as terminal_size,
        ):
            renderer = TerminalHostRenderer(stderr)
            renderer.set_status("status that would wrap")
            renderer.clear_all()

        terminal_size.assert_called_with(19)
        output = stderr.getvalue()
        self.assertNotIn("status that would wrap", output)
        transient_rows = [row for row in output.split("\r\x1b[K") if row]
        self.assertTrue(transient_rows)
        self.assertTrue(all(len(row) < 8 for row in transient_rows))
        self.assertTrue(output.endswith("\r\x1b[K"))

    def test_lines_renderer_emits_no_terminal_escapes(self):
        stream = io.StringIO()
        renderer = TerminalHostRenderer(stream)
        state = HostPresentationState(
            renderer, mode=HostPresentationMode.LINES
        )
        state.observe_heartbeat(_heartbeat(elapsed_seconds=3), now=3.0)
        state.admit_diagnostic(_diagnostic("npm warn one"), now=3.1)
        rendered = stream.getvalue()
        self.assertNotIn("\x1b", rendered)
        self.assertTrue(rendered.endswith("\n"))




class _NeverEmptyMailbox(PresentationMailbox):
    """Mailbox that always has an event until explicitly released.

    Proves the worker services expired deadlines even when ``take`` never
    returns ``None`` for an idle mailbox.
    """

    def __init__(self, event: object, release: threading.Event) -> None:
        super().__init__(capacity=1, control_capacity=1)
        self._event = event
        self._release = release
        self._sequence = 0

    def take(self, timeout: float | None = None) -> AdmittedEvent | None:
        if self._release.is_set():
            return None
        item = AdmittedEvent(
            self._sequence, PresentationLane.TELEMETRY, self._event
        )
        self._sequence += 1
        return item

    def take_omission_notice(self, sequence: int | None) -> str | None:
        return None


class TestWorkerDeadlineServicing(unittest.TestCase):
    """A mailbox that is never empty must not starve state deadlines."""

    def test_continuously_busy_mailbox_does_not_starve_the_window(self):
        renderer = RecordingRenderer()
        state = HostPresentationState(renderer, mode=HostPresentationMode.LINES)
        release = threading.Event()
        mailbox = _NeverEmptyMailbox(_diagnostic("retry"), release)
        clock_value = [0.0]
        worker = PresentationWorker(mailbox, state, clock=lambda: clock_value[0])
        worker.start()
        try:
            # Let the first event start the fixed window at t=0.
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not renderer.calls:
                time.sleep(0.001)
            # Advance past the window while take() still never returns None,
            # so only per-iteration deadline servicing can emit the summary.
            clock_value[0] = 2.0
            summary = "retry (repeated"
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                if any(
                    call[0] == "durable" and summary in str(call[1])
                    for call in list(renderer.calls)
                ):
                    break
                time.sleep(0.005)
            self.assertTrue(
                any(
                    call[0] == "durable" and summary in str(call[1])
                    for call in list(renderer.calls)
                ),
                "the fixed window summary was starved by a busy mailbox",
            )
        finally:
            mailbox.signal_emergency()
            release.set()
            worker.join(5.0)
            self.assertFalse(worker.is_alive)
            _assert_no_worker(self)


class TestAdmittedDiagnosticEventRendering(unittest.TestCase):
    def test_legacy_typed_diagnostic_is_durable(self):
        renderer = RecordingRenderer()
        state = HostPresentationState(
            renderer, mode=HostPresentationMode.INTERACTIVE
        )
        state.observe_diagnostic_event(
            HostDiagnosticEvent(
                HostPhase.LOCKED_ASSEMBLY,
                HostDiagnosticStream.STDERR,
                "warning",
            ),
            now=0.0,
        )
        self.assertEqual(
            [("durable", "locked assembly [stderr]: warning")], renderer.calls
        )


class TestHostnamePolicy(unittest.TestCase):
    def test_disabled_and_enabled_receive_identical_facts(self):
        diagnostic = _diagnostic(
            "npm warn see <redacted>",
            hostnames=("registry.example.com",),
        )
        hidden = RecordingRenderer()
        shown = RecordingRenderer()
        HostPresentationState(
            hidden, mode=HostPresentationMode.LINES, show_network_hosts=False
        ).admit_diagnostic(diagnostic, now=0.0)
        HostPresentationState(
            shown, mode=HostPresentationMode.LINES, show_network_hosts=True
        ).admit_diagnostic(diagnostic, now=0.0)
        self.assertEqual(
            [("durable", "npm warn see <redacted>")], hidden.calls
        )
        self.assertEqual(
            [("durable", "npm warn see <redacted> [registry.example.com]")],
            shown.calls,
        )


class TestLinesHostnameFinalization(unittest.TestCase):
    """Every lines finalization path applies the hostname policy."""

    HOSTS = ("registry.example.com",)
    SUMMARY = "retry (repeated 2 times)"
    PATHS = ("deadline", "different", "terminal", "omission")

    def _run(self, path: str, *, show: bool) -> list[tuple[object, ...]]:
        renderer = RecordingRenderer()
        state = HostPresentationState(
            renderer,
            mode=HostPresentationMode.LINES,
            show_network_hosts=show,
        )
        diagnostic = _diagnostic("retry", hostnames=self.HOSTS)
        state.admit_diagnostic(diagnostic, now=0.0)
        state.admit_diagnostic(diagnostic, now=0.1)
        if path == "deadline":
            state.due(now=1.2)
        elif path == "different":
            state.admit_diagnostic(
                _diagnostic("other", hostnames=self.HOSTS), now=0.2
            )
        elif path == "terminal":
            state.observe_step(
                HostStepEvent(
                    HostPhase.LOCKED_ASSEMBLY,
                    HostStep.NPM_EXECUTION,
                    HostStepState.SUCCEEDED,
                    True,
                ),
                now=0.2,
            )
        elif path == "omission":
            state.admit_diagnostic(
                _diagnostic("next", hostnames=self.HOSTS),
                now=0.2,
                omission_notice="[1 diagnostics omitted]",
            )
        else:  # pragma: no cover - guards a typo in PATHS
            raise AssertionError(path)
        return renderer.calls

    def _summary_texts(self, calls: list[tuple[object, ...]]) -> list[str]:
        return [
            call[1]
            for call in calls
            if call[0] in ("durable", "finalize")
            and isinstance(call[1], str)
            and call[1].startswith(self.SUMMARY)
        ]

    def test_hostnames_are_consistent_across_every_finalization_path(self):
        for path in self.PATHS:
            with self.subTest(path=path):
                texts = self._summary_texts(self._run(path, show=True))
                self.assertEqual(
                    [f"{self.SUMMARY} [registry.example.com]"], texts, path
                )

    def test_hostnames_are_hidden_when_disabled_on_every_path(self):
        for path in self.PATHS:
            with self.subTest(path=path):
                texts = self._summary_texts(self._run(path, show=False))
                self.assertEqual([self.SUMMARY], texts, path)


class TestLinesClassificationCoalescing(unittest.TestCase):
    def _state(self) -> HostPresentationState:
        return HostPresentationState(
            self.renderer, mode=HostPresentationMode.LINES
        )

    def test_warning_classification_does_not_bypass_coalescing(self):
        self.renderer = RecordingRenderer()
        state = self._state()
        warning = _diagnostic(
            "npm warn retry", classification=HostDiagnosticClassification.WARNING
        )
        state.admit_diagnostic(warning, now=0.0)
        state.admit_diagnostic(warning, now=0.1)
        state.due(now=1.2)
        durables = [call[1] for call in self.renderer.calls if call[0] == "durable"]
        self.assertEqual(
            ["npm warn retry", "npm warn retry (repeated 2 times)"], durables
        )

    def test_single_occurrence_group_emits_no_summary(self):
        self.renderer = RecordingRenderer()
        state = self._state()
        state.admit_diagnostic(_diagnostic("once"), now=0.0)
        state.due(now=1.5)
        durables = [call[1] for call in self.renderer.calls if call[0] == "durable"]
        self.assertEqual(["once"], durables)

    def test_different_diagnostic_flushes_pending_summary_first(self):
        self.renderer = RecordingRenderer()
        state = self._state()
        state.admit_diagnostic(_diagnostic("retry"), now=0.0)
        state.admit_diagnostic(_diagnostic("retry"), now=0.1)
        state.admit_diagnostic(_diagnostic("other"), now=0.2)
        durables = [call[1] for call in self.renderer.calls if call[0] == "durable"]
        self.assertEqual(
            ["retry", "retry (repeated 2 times)", "other"], durables
        )


class TestInteractiveUrlIdentity(unittest.TestCase):
    def test_distinct_hidden_urls_do_not_share_a_group(self):
        renderer = RecordingRenderer()
        state = HostPresentationState(
            renderer, mode=HostPresentationMode.INTERACTIVE
        )
        first = _diagnostic("fetched <redacted>", url_fingerprints=("a" * 64,))
        second = _diagnostic("fetched <redacted>", url_fingerprints=("b" * 64,))
        state.admit_diagnostic(first, now=0.0)
        renderer.calls.clear()
        state.admit_diagnostic(second, now=0.1)
        self.assertEqual(
            [
                ("finalize", "fetched <redacted>", None),
                ("slot", "fetched <redacted>"),
            ],
            renderer.calls,
        )


class TestNoLiveSink(unittest.TestCase):
    def test_json_and_default_noninteractive_create_no_session(self):
        from docker import constructor_cli
        from docker.versioning.model import LocalOutputPolicy

        for policy, text_output, tty in (
            (LocalOutputPolicy("interactive", False), False, True),
            (LocalOutputPolicy("lines", False), False, True),
            (LocalOutputPolicy("interactive", False), True, False),
            (LocalOutputPolicy("off", False), True, False),
        ):
            with self.subTest(policy=policy, text_output=text_output, tty=tty):
                session = constructor_cli._make_presentation_session(
                    policy, text_output=text_output, stderr_is_tty=tty
                )
                self.assertIsNone(session)
        _assert_no_worker(self)


    def test_worker_persists_across_step_terminals(self):
        renderer = RecordingRenderer()
        session = HostPresentationSession(
            renderer,
            PresentationPlan(HostPresentationMode.LINES, PresentationSelection.LIVE),
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
        session.sink(_diagnostic("npm warn after terminal"))
        self.assertTrue(session.shutdown())
        self.assertIn(
            ("durable", "npm warn after terminal"), renderer.calls
        )
        _assert_no_worker(self)

    def test_prompt_return_under_telemetry_saturation(self):
        renderer = RecordingRenderer()
        session = HostPresentationSession(
            renderer,
            PresentationPlan(HostPresentationMode.LINES, PresentationSelection.LIVE),
        )
        for index in range(5000):
            started = time.monotonic()
            session.sink(_diagnostic(f"flood {index}"))
            self.assertLess(time.monotonic() - started, 0.5)
        # Under a telemetry flood a transiently contended reliable admission
        # legitimately escalates to the emergency stop; either way the worker
        # must be gone when shutdown returns.
        session.shutdown()
        self.assertFalse(session.worker.is_alive)
        _assert_no_worker(self)


def _attach(
    exc: BaseException,
    phase: HostPhase,
    step: HostStep,
    *,
    logical_resource: str | None = None,
    hostnames: tuple[str, ...] = (),
) -> BaseException:
    """Attach structural phase/step context to *exc* at its boundary."""
    from docker.versioning.host_progress import attach_host_failure

    attach_host_failure(
        exc,
        phase=phase,
        step=step,
        logical_resource=logical_resource,
        hostnames=hostnames,
    )
    return exc


class TestStructuralFailureAttribution(unittest.TestCase):
    """Failure reports come from the tracked active step, never exception type."""

    def _context(self, exc: BaseException):
        from docker.versioning.build_orchestration import _host_failure_context

        return _host_failure_context(exc)

    def test_each_failure_boundary_reports_its_actual_phase_and_step(self):
        from docker.npm_environment.errors import LockedNpmError

        boundaries = (
            (HostPhase.RELEASE_ACQUISITION, HostStep.ARTIFACT_ACQUISITION),
            (HostPhase.RELEASE_ACQUISITION, HostStep.RELEASE_ACQUISITION),
            (HostPhase.LOCKED_ASSEMBLY, HostStep.LOCK_WAIT),
            (HostPhase.LOCKED_ASSEMBLY, HostStep.CACHE_LOOKUP),
            (HostPhase.LOCKED_ASSEMBLY, HostStep.STALE_STAGE_CLEANUP),
            (HostPhase.LOCKED_ASSEMBLY, HostStep.CONTAINER_STARTUP),
            (HostPhase.LOCKED_ASSEMBLY, HostStep.NPM_EXECUTION),
            (HostPhase.DERIVED_VALIDATION, HostStep.VALIDATION),
            (HostPhase.DERIVED_VALIDATION, HostStep.PUBLICATION),
        )
        for phase, step in boundaries:
            with self.subTest(step=step):
                context = self._context(
                    _attach(
                        LockedNpmError("timeout", "tail"), phase, step
                    )
                )
                self.assertIs(phase, context.phase)
                self.assertIs(step, context.step)

    def test_attribution_ignores_exception_class_and_message_wording(self):
        from docker.npm_environment.errors import LockedNpmError

        context = self._context(
            _attach(
                LockedNpmError("validation failed during acquisition", "boom"),
                HostPhase.LOCKED_ASSEMBLY,
                HostStep.PUBLICATION,
            )
        )
        self.assertIs(HostPhase.LOCKED_ASSEMBLY, context.phase)
        self.assertIs(HostStep.PUBLICATION, context.step)

    def test_wrapped_failure_preserves_context_and_bounded_tail(self):
        from docker.npm_environment.errors import LockedNpmError
        from docker.versioning.build_snapshot import SnapshotError

        inner = _attach(
            LockedNpmError(
                "executor_failure",
                "npm tail",
                diagnostic_tail="npm tail",
                diagnostic_stream="stderr",
            ),
            HostPhase.LOCKED_ASSEMBLY,
            HostStep.CONTAINER_STARTUP,
        )
        wrapped = SnapshotError("Pi materialization failed")
        wrapped.__cause__ = inner
        context = self._context(wrapped)
        self.assertIs(HostStep.CONTAINER_STARTUP, context.step)
        self.assertEqual("npm tail", context.tail)
        self.assertIn("SnapshotError", context.exception_types)
        self.assertIn("LockedNpmError", context.exception_types)

    def test_logical_resource_and_normalized_hosts_are_preserved(self):
        context = self._context(
            _attach(
                OSError("boom"),
                HostPhase.RELEASE_ACQUISITION,
                HostStep.ARTIFACT_ACQUISITION,
                logical_resource="rustup",
                hostnames=("registry.example.com",),
            )
        )
        self.assertEqual("rustup", context.logical_resource)
        self.assertEqual(("registry.example.com",), context.hostnames)

    def test_untracked_failure_defaults_without_a_tail_section(self):
        from docker.versioning.build_orchestration import _host_failure_context

        context = _host_failure_context(OSError("boom"))
        self.assertIs(HostPhase.RELEASE_ACQUISITION, context.phase)
        self.assertIs(HostStep.ARTIFACT_ACQUISITION, context.step)
        self.assertEqual("", context.tail)
        rendered = format_failure_report(
            context.phase, context.step, tail=context.tail
        )
        self.assertNotIn("Last diagnostics", rendered)

    def test_context_renders_phase_step_and_tail_without_replay(self):
        from docker.npm_environment.errors import LockedNpmError

        context = self._context(
            _attach(
                LockedNpmError(
                    "timeout",
                    "npm tail",
                    diagnostic_tail="npm tail",
                    diagnostic_stream="stderr",
                ),
                HostPhase.LOCKED_ASSEMBLY,
                HostStep.NPM_EXECUTION,
            )
        )
        rendered = format_failure_report(
            context.phase,
            context.step,
            tail=context.tail,
            exception_types=context.exception_types,
        )
        self.assertIn("locked assembly", rendered)
        self.assertIn("npm execution", rendered)
        self.assertIn("error types: LockedNpmError", rendered)
        self.assertEqual(1, rendered.count("npm tail"))












class TestFailureBoundaryAttachment(unittest.TestCase):
    """A real boundary attaches its actual active step to the failure."""

    def _run_boundary(self, step_name: str, *, container_name: str | None = None):
        from docker.npm_environment.errors import LockedNpmError
        from docker.versioning.assembly_activity import HostAssemblyActivity

        activity = HostAssemblyActivity(None)
        try:
            with activity.step(step_name, container_name=container_name):
                raise LockedNpmError("step_failed", "boom")
        except LockedNpmError as exc:
            return exc
        raise AssertionError("boundary did not propagate the failure")

    def test_npm_execution_boundary_is_attributed_structurally(self):
        from docker.versioning.host_progress import lookup_host_failure

        exc = self._run_boundary(
            "npm_execution", container_name="npm-assembler-0123456789abcdef"
        )
        marker = lookup_host_failure(exc)
        self.assertIsNotNone(marker)
        self.assertIs(HostPhase.LOCKED_ASSEMBLY, marker.phase)
        self.assertIs(HostStep.NPM_EXECUTION, marker.step)
        self.assertEqual(
            "npm-assembler-0123456789abcdef", marker.logical_resource
        )

    def test_container_startup_boundary_is_not_labelled_npm_execution(self):
        from docker.versioning.host_progress import lookup_host_failure

        exc = self._run_boundary("container_startup")
        marker = lookup_host_failure(exc)
        self.assertIsNotNone(marker)
        self.assertIs(HostStep.CONTAINER_STARTUP, marker.step)

    def test_validation_boundary_is_attributed_structurally(self):
        from docker.versioning.host_progress import lookup_host_failure

        exc = self._run_boundary("validation")
        marker = lookup_host_failure(exc)
        self.assertIsNotNone(marker)
        self.assertIs(HostStep.VALIDATION, marker.step)


if __name__ == "__main__":
    unittest.main()
