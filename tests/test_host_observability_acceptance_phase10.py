"""Phase 10 integrated host-observability acceptance contracts.

The npm acceptance fixture drives the production assembler execution/streaming
entry point with an injected fake subprocess and monotonic clock. It performs no
real waiting, Docker execution, or registry access.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from docker.npm_environment import (
    DockerRunExecutor,
    RootSpec,
    assemble,
    assembler_script_digest,
    compute_assembler_identity,
    npm_policy_digest,
    preflight,
)
from docker.versioning.assembly_activity import HostAssemblyActivity
from docker.versioning.host_presentation import (
    HostPresentationMode,
    HostPresentationSession,
    PresentationPlan,
    PresentationSelection,
)
from docker.versioning.host_progress import (
    HostBuildEvent,
    HostDiagnosticStream,
    HostHeartbeatEvent,
    HostPhase,
    HostStep,
    HostStepEvent,
    HostStepState,
    HostStructuredDiagnostic,
)
from docker.versioning.npm_diagnostic_stream import (
    classify_npm_diagnostic,
    make_stream_factory,
    project_tail,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.condition = threading.Condition()

    def __call__(self) -> float:
        with self.condition:
            return self.now

    def advance_to(self, value: float) -> None:
        with self.condition:
            self.now = max(self.now, value)
            self.condition.notify_all()


class _HoldingWaiter:
    """Remain blocked until a short orchestration step finishes."""

    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.stopped = False

    def wait(self, _timeout: float) -> bool:
        with self.condition:
            while not self.stopped:
                self.condition.wait(timeout=1.0)
            return True

    def set(self) -> None:
        with self.condition:
            self.stopped = True
            self.condition.notify_all()


class _AutoWaiter:
    """Drive heartbeat time to 121s, then wait for operation completion."""

    def __init__(self, clock: _Clock, output_observed: threading.Event) -> None:
        self.clock = clock
        self.output_observed = output_observed
        self.condition = threading.Condition()
        self.stopped = False

    def wait(self, timeout: float) -> bool:
        deadline = self.clock() + timeout
        if deadline <= 121.0:
            if deadline == 121.0:
                self.output_observed.wait(timeout=1.0)
            self.clock.advance_to(deadline)
            return False
        with self.condition:
            while not self.stopped:
                self.condition.wait(timeout=1.0)
            return True

    def set(self) -> None:
        with self.condition:
            self.stopped = True
            self.condition.notify_all()


class _SilentThenSuccessfulPipe:
    def __init__(self, process: "_FakeProcess", *, emits: bool) -> None:
        self.process = process
        self.emits = emits
        self.delivered = False
        self.closed = False

    def read(self, _size: int = -1) -> bytes:
        if not self.emits:
            return b""
        with self.process.clock.condition:
            while (
                (self.process.clock.now < 120.0 or not self.process.output_allowed.is_set())
                and not self.closed
            ):
                self.process.clock.condition.wait(timeout=1.0)
            if self.closed or self.delivered:
                return b""
            self.delivered = True
            self.process.returncode = 0
            self.process.clock.condition.notify_all()
            return b"npm http fetch GET 200 [sanitized]\n"

    def close(self) -> None:
        self.closed = True
        with self.process.clock.condition:
            self.process.clock.condition.notify_all()


class _FakeProcess:
    """Popen-compatible silent process that writes once at monotonic 121s."""

    def __init__(self, clock: _Clock) -> None:
        self.clock = clock
        self.returncode: int | None = None
        self.output_allowed = threading.Event()
        self.stdout = _SilentThenSuccessfulPipe(self, emits=True)
        self.stderr = _SilentThenSuccessfulPipe(self, emits=False)
        self.terminated = False

    def wait(self, timeout: float | None = None) -> int:
        with self.clock.condition:
            if self.returncode is None:
                if timeout is not None:
                    raise subprocess.TimeoutExpired(("fake-npm",), timeout)
                while self.returncode is None:
                    self.clock.condition.wait(timeout=1.0)
            return self.returncode

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.terminated = True
        self.returncode = -9


class _Renderer:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def set_status(self, text: str) -> None:
        self.calls.append(("status", text))

    def set_slot(self, text: str) -> None:
        self.calls.append(("slot", text))

    def clear_slot(self) -> None:
        self.calls.append(("clear_slot",))

    def clear_status(self) -> None:
        self.calls.append(("clear_status",))

    def clear_all(self) -> None:
        self.calls.append(("clear_all",))

    def durable(self, text: str) -> None:
        self.calls.append(("durable", text))

    def finalize_diagnostic(self, text: str, *, restore_status: str | None) -> None:
        self.calls.append(("finalize", text, restore_status))


def _validated_input():
    integrity = "sha512-" + base64.b64encode(bytes([0xAB]) * 64).decode()
    lock = json.dumps({
        "name": "root",
        "version": "1.0.0",
        "lockfileVersion": 3,
        "requires": True,
        "packages": {
            "": {"name": "root", "version": "1.0.0", "dependencies": {"a": "1.0.0"}},
            "node_modules/a": {
                "version": "1.0.0",
                "resolved": "https://registry.npmjs.org/a/-/a-1.0.0.tgz",
                "integrity": integrity,
            },
        },
    }).encode()
    return preflight(
        lock,
        roots=(RootSpec("a", "1.0.0"),),
        platform="linux-x64",
        node_version="24.18.0",
        npm_version="11.16.0",
    )


def _assembler_identity():
    return compute_assembler_identity(
        image_digest="sha256:" + "a" * 64,
        node_version="24.18.0",
        npm_version="11.16.0",
        script_digest=assembler_script_digest(),
        policy_digest=npm_policy_digest(),
        platform="linux-x64",
    )


class TestAcceptanceMatrixContract(unittest.TestCase):
    """Keep every Phase 10 row mapped to passing executable coverage."""

    REQUIRED_ROWS = {
        "host_download_progress_failure", "coordination_wait", "cache_reuse",
        "npm_timeout", "validation_publication_failure", "interactive_lines_off",
        "hostname_hidden_shown", "json_isolation", "safe_cancellation",
        "final_report_ownership_native_handoff", "retained_replay_after_drop",
        "full_inbox_close", "control_exhaustion", "renderer_exception",
        "stalled_write_single_budget", "heartbeat_counts_not_timeout",
    }

    def test_every_revised_scenario_executes_and_passes(self) -> None:
        path = Path(__file__).parent / "data" / "host_observability_acceptance_matrix.json"
        matrix = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(self.REQUIRED_ROWS, set(matrix))
        loader = unittest.defaultTestLoader
        for row, test_ids in matrix.items():
            self.assertTrue(test_ids, row)
            for test_id in test_ids:
                suite = loader.loadTestsFromName(test_id)
                load_result = unittest.TestResult()
                suite.run(load_result)
                detail = "\n".join(text for _test, text in (*load_result.failures, *load_result.errors))
                self.assertEqual(
                    1, load_result.testsRun,
                    f"acceptance row {row!r} target {test_id!r} did not resolve exactly once: {detail}",
                )
                self.assertTrue(
                    load_result.wasSuccessful(),
                    f"acceptance row {row!r} target {test_id!r} failed:\n{detail}",
                )


class TestHostOutputDocumentation(unittest.TestCase):
    def test_user_contract_and_example_cover_phase10_behavior(self) -> None:
        root = Path(__file__).resolve().parents[1]
        doc = (root / "docs" / "host-build-output.md").read_text(encoding="utf-8")
        example = (root / "docker-constructor.local.example.toml").read_text(encoding="utf-8")
        for term in (
            "interactive", "lines", "off", "show_network_hosts", "3 seconds",
            "30 seconds", "120 seconds", "diagnostic silence", "execution timeout",
            "Last activity", "Ctrl-C", "retained-context", "may repeat", "best-effort",
            "numeric-token", "five-second", "daemon", "in flight", "final output may",
            "does not synchronously retry", "--loglevel=http", "npm 11.16.0",
        ):
            with self.subTest(term=term):
                self.assertIn(term.lower(), doc.lower())
        self.assertIn('host_heartbeat = "interactive"', example)
        self.assertIn("explicit noninteractive output", example)
        self.assertIn("show_network_hosts = false", example)


class TestIntegratedSilentNpmAcceptance(unittest.TestCase):
    def test_fake_subprocess_silence_resume_and_success_use_real_streaming_path(self) -> None:
        clock = _Clock()
        output_observed = threading.Event()
        waiter = _AutoWaiter(clock, output_observed)
        renderer = _Renderer()
        session = HostPresentationSession(
            renderer,
            PresentationPlan(HostPresentationMode.INTERACTIVE, PresentationSelection.LIVE),
            clock=clock,
        )
        observed: list[HostBuildEvent] = []

        fake_process = _FakeProcess(clock)

        def event_sink(event: HostBuildEvent) -> None:
            observed.append(event)
            session.sink(event)
            if isinstance(event, HostHeartbeatEvent) and event.elapsed_seconds == 120:
                fake_process.output_allowed.set()
                with clock.condition:
                    clock.condition.notify_all()

        waiter_count = 0

        def waiter_factory():
            nonlocal waiter_count
            waiter_count += 1
            return _HoldingWaiter() if waiter_count == 1 else waiter

        activity = HostAssemblyActivity(
            event_sink,
            clock=clock,
            waiter_factory=waiter_factory,
            deadline_seconds=180.0,
        )

        def diagnostic_sink(chunk) -> None:
            event_sink(HostStructuredDiagnostic(
                phase=HostPhase.LOCKED_ASSEMBLY,
                step=HostStep.NPM_EXECUTION,
                stream=HostDiagnosticStream(chunk.stream),
                classification=classify_npm_diagnostic(chunk.text),
                text=chunk.text,
                hostnames=chunk.hostnames,
                logical_resource=activity.current_container_name,
                url_fingerprints=chunk.url_fingerprints,
            ))

        started = time.monotonic()
        with tempfile.TemporaryDirectory() as cache_root, mock.patch(
            "docker.npm_environment.execution.subprocess.Popen",
            return_value=fake_process,
        ):
            run = assemble(
                validated=_validated_input(),
                assembler=_assembler_identity(),
                cache_root=cache_root,
                executor=DockerRunExecutor(),
                uid=1000,
                gid=1000,
                sink=diagnostic_sink,
                stream_factory=make_stream_factory(
                    on_chunk=lambda: (activity.record_diagnostic(), output_observed.set()),
                ),
                tail_projector=project_tail,
                activity=activity,
            )
        self.assertTrue(session.shutdown())
        wall_elapsed = time.monotonic() - started

        heartbeats = [event for event in observed if isinstance(event, HostHeartbeatEvent)]
        elapsed = [event.elapsed_seconds for event in heartbeats]
        self.assertEqual(3, elapsed[0])
        self.assertEqual(list(range(3, 122)), elapsed)
        self.assertTrue(all(b - a == 1 for a, b in zip(elapsed, elapsed[1:])))
        silence = [event for event in heartbeats if event.diagnostic_silence_seconds is not None]
        self.assertEqual([120], [event.diagnostic_silence_seconds for event in silence])
        self.assertIsNone(heartbeats[-1].diagnostic_silence_seconds)
        self.assertGreater(heartbeats[-1].remaining_deadline_seconds or 0, 0)
        self.assertEqual(0, fake_process.returncode)
        self.assertFalse(fake_process.terminated)
        self.assertIn("npm http fetch", run.stdout)
        self.assertLess(wall_elapsed, 2.0)
        terminals = [
            event for event in observed
            if isinstance(event, HostStepEvent)
            and event.step is HostStep.NPM_EXECUTION
            and event.state is not HostStepState.STARTED
        ]
        self.assertEqual([HostStepState.SUCCEEDED], [event.state for event in terminals])
        statuses = [str(call[1]) for call in renderer.calls if call[0] == "status"]
        self.assertTrue(any("no diagnostics for 2m 00s" in text for text in statuses))
        self.assertTrue(any("last diagnostic 1s ago" in text for text in statuses))


if __name__ == "__main__":
    unittest.main()
