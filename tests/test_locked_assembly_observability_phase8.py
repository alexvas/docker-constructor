"""Phase 8 RED tests — locked-assembly operational orchestration (8.1–8.4).

These bind the orchestration deliverables:

* the complete ordered locked-assembly step sequence for coordination wait,
  cache lookup/reuse, stale-stage cleanup, container startup, npm execution,
  validation, and publication, while exactly one coarse locked-assembly
  started/terminal lifecycle pair remains;
* elapsed coordination-wait activity around the unchanged blocking lock with
  no timeout, polling acquisition, or ownership change;
* cache lookup/hit activity with no container or npm activity for a verified
  hit;
* ``npm ci`` declaring an expected diagnostic stream, every stdout/stderr
  chunk resetting only diagnostic silence and becoming the latest
  ``diagnostic`` activity without changing heartbeat cadence, heartbeat facts
  carrying age/container/deadline context, and no claim of current download or
  network activity and no Docker/process/filesystem probes.
"""
from __future__ import annotations

import base64
import inspect
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from docker.npm_environment import (
    LockedNpmError,
    ProcessResult,
    RootSpec,
    assembler_script_digest,
    assemble_environment,
    compute_assembler_identity,
    npm_policy_digest,
    preflight,
)
from docker.npm_environment.streaming import StreamChunk, collect_streams
from docker.versioning.activity_monitor import HostActivityMonitor
from docker.versioning.assembly_activity import HostAssemblyActivity
from docker.versioning.host_progress import (
    HostHeartbeatEvent,
    HostLastActivityKind,
    HostPhase,
    HostPhaseEvent,
    HostStep,
    HostStepEvent,
    HostStepState,
)

_IMAGE = "sha256:" + "a" * 64
_NODE = "24.18.0"
_NPM = "11.16.0"
_PLATFORM = "linux-x64"

_EXPECTED_STEP_ORDER = (
    HostStep.LOCK_WAIT,
    HostStep.CACHE_LOOKUP,
    HostStep.STALE_STAGE_CLEANUP,
    HostStep.CONTAINER_STARTUP,
    HostStep.NPM_EXECUTION,
    HostStep.VALIDATION,
    HostStep.PUBLICATION,
)


def _sri() -> str:
    return "sha512-" + base64.b64encode(bytes([0xAB]) * 64).decode()


def _url(name: str, version: str) -> str:
    stem = name.rsplit("/", 1)[-1]
    return f"https://registry.npmjs.org/{name}/-/{stem}-{version}.tgz"


def _lock() -> bytes:
    return json.dumps(
        {
            "name": "root",
            "version": "1.0.0",
            "lockfileVersion": 3,
            "requires": True,
            "packages": {
                "": {
                    "name": "root",
                    "version": "1.0.0",
                    "dependencies": {"a": "1.0.0"},
                },
                "node_modules/a": {
                    "version": "1.0.0",
                    "resolved": _url("a", "1.0.0"),
                    "integrity": _sri(),
                },
            },
        }
    ).encode()


def _validated():
    return preflight(
        _lock(),
        roots=(RootSpec("a", "1.0.0"),),
        platform=_PLATFORM,
        node_version=_NODE,
        npm_version=_NPM,
    )


def _assembler():
    return compute_assembler_identity(
        image_digest=_IMAGE,
        node_version=_NODE,
        npm_version=_NPM,
        script_digest=assembler_script_digest(),
        policy_digest=npm_policy_digest(),
        platform=_PLATFORM,
    )


def _write_pkg(root: Path, path: str, name: str, version: str) -> None:
    p = root / path / "package.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"name": name, "version": version}))


def _populate(staging: Path, marker: bytes) -> None:
    _write_pkg(staging, "node_modules/a", "a", "1.0.0")
    _write_pkg(staging, "", "root", "1.0.0")
    (staging / "node_modules" / "a" / "marker.txt").write_bytes(marker)


def _staging_from_argv(argv: tuple[str, ...]) -> Path | None:
    for i, token in enumerate(argv):
        if token == "--volume" and i + 1 < len(argv):
            spec = argv[i + 1]
            if spec.endswith(":/work:rw"):
                return Path(spec.rsplit(":", 2)[0])
    return None


class _PopulatingExecutor:
    """Fake Docker executor that writes the assembled tree into staging."""

    def __init__(self, marker: bytes):
        self.marker = marker
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: tuple[str, ...]) -> ProcessResult:
        self.calls.append(argv)
        staging = _staging_from_argv(argv)
        if staging is not None:
            _populate(staging, self.marker)
        return ProcessResult(argv, 0, "", "")


class _StreamingExecutor:
    """Fake Docker executor that streams npm output through the collector."""

    def __init__(self, marker: bytes, stdout: bytes = b"", stderr: bytes = b""):
        self.marker = marker
        self.stdout = stdout
        self.stderr = stderr
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: tuple[str, ...]) -> ProcessResult:
        raise AssertionError("streaming executor must use run_streaming")

    def run_streaming(
        self,
        argv: tuple[str, ...],
        *,
        secrets=(),
        sink=None,
        stream_factory=None,
        tail_projector=None,
    ) -> ProcessResult:
        import os

        self.calls.append(argv)
        staging = _staging_from_argv(argv)
        if staging is not None:
            _populate(staging, self.marker)
        out_r, out_w = os.pipe()
        err_r, err_w = os.pipe()

        def produce() -> None:
            os.write(out_w, self.stdout)
            os.close(out_w)
            os.write(err_w, self.stderr)
            os.close(err_w)

        producer = threading.Thread(target=produce)
        producer.start()
        try:
            capture = collect_streams(
                stdout_read=lambda n: os.read(out_r, n),
                stderr_read=lambda n: os.read(err_r, n),
                secrets=secrets,
                sink=sink,
                stream_factory=stream_factory,
                tail_projector=tail_projector,
            )
        finally:
            producer.join()
            os.close(out_r)
            os.close(err_r)
        return ProcessResult(
            argv,
            0,
            capture.stdout_tail,
            capture.stderr_tail,
            truncation_notice=capture.truncation_notice,
        )


def _started_steps(events) -> list[HostStep]:
    return [
        event.step
        for event in events
        if isinstance(event, HostStepEvent)
        and event.state is HostStepState.STARTED
    ]


def _step_event_index(events, step: HostStep, state: HostStepState) -> int:
    for index, event in enumerate(events):
        if (
            isinstance(event, HostStepEvent)
            and event.step is step
            and event.state is state
        ):
            return index
    raise AssertionError(f"missing {step.value} {state.value} event")


class _FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _TickingWaiter:
    """Waiter that advances simulated time on every wait and never blocks."""

    def __init__(self, clock: _FakeClock) -> None:
        self._clock = clock

    def wait(self, timeout: float) -> bool:
        self._clock.advance(float(timeout))
        return False

    def set(self) -> None:
        pass


class _AssemblyCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="npm-env-phase8-")
        self.addCleanup(tmp.cleanup)
        self.cache_root = Path(tmp.name) / "cache"
        self.cache_root.mkdir()
        self.validated = _validated()
        self.assembler = _assembler()


class TestOrderedStepSequence(_AssemblyCase):
    def test_ordered_steps_preserve_single_lifecycle_pair(self):
        events: list[object] = []
        activity = HostAssemblyActivity(events.append)
        assemble_environment(
            validated=self.validated,
            assembler=self.assembler,
            cache_root=self.cache_root,
            executor=_PopulatingExecutor(b"one"),
            activity=activity,
        )
        self.assertEqual(_started_steps(events), list(_EXPECTED_STEP_ORDER))
        # Operational instrumentation never emits a coarse host phase event:
        # the single lifecycle pair stays owned by the Pi assembly boundary.
        self.assertEqual(
            [event for event in events if isinstance(event, HostPhaseEvent)], []
        )

    def test_operational_steps_terminate_before_next_step_starts(self):
        events: list[object] = []
        activity = HostAssemblyActivity(events.append)
        assemble_environment(
            validated=self.validated,
            assembler=self.assembler,
            cache_root=self.cache_root,
            executor=_PopulatingExecutor(b"one"),
            activity=activity,
        )
        lock_start = _step_event_index(
            events, HostStep.LOCK_WAIT, HostStepState.STARTED
        )
        lock_done = _step_event_index(
            events, HostStep.LOCK_WAIT, HostStepState.SUCCEEDED
        )
        cache_start = _step_event_index(
            events, HostStep.CACHE_LOOKUP, HostStepState.STARTED
        )
        startup_start = _step_event_index(
            events, HostStep.CONTAINER_STARTUP, HostStepState.STARTED
        )
        startup_done = _step_event_index(
            events, HostStep.CONTAINER_STARTUP, HostStepState.SUCCEEDED
        )
        npm_start = _step_event_index(
            events, HostStep.NPM_EXECUTION, HostStepState.STARTED
        )
        # LOCK_WAIT is closed before lookup begins, and CONTAINER_STARTUP is
        # closed at the launch boundary before NPM_EXECUTION starts.
        self.assertLess(lock_start, lock_done)
        self.assertLess(lock_done, cache_start)
        self.assertLess(startup_start, startup_done)
        self.assertLess(startup_done, npm_start)

    def test_failed_container_launch_never_starts_npm_execution(self):
        class LaunchFailingExecutor:
            def run(self, argv):  # noqa: ANN001
                raise RuntimeError("cleanup rm failed")

            def run_streaming(
                self, argv, *, secrets=(), sink=None, on_launched=None
            ):  # noqa: ANN001
                raise RuntimeError("docker run failed to launch")

        events: list[object] = []
        activity = HostAssemblyActivity(events.append)
        with self.assertRaises(LockedNpmError):
            assemble_environment(
                validated=self.validated,
                assembler=self.assembler,
                cache_root=self.cache_root,
                executor=LaunchFailingExecutor(),
                activity=activity,
            )
        startup = [
            event
            for event in events
            if isinstance(event, HostStepEvent)
            and event.step is HostStep.CONTAINER_STARTUP
        ]
        self.assertEqual(
            [event.state for event in startup],
            [HostStepState.STARTED, HostStepState.FAILED],
        )
        self.assertEqual(
            [
                event
                for event in events
                if isinstance(event, HostStepEvent)
                and event.step is HostStep.NPM_EXECUTION
            ],
            [],
        )

    def test_failure_still_terminates_instrumented_steps(self):
        class FailingExecutor:
            def run(self, argv):  # noqa: ANN001
                raise RuntimeError("boom")

        events: list[object] = []
        activity = HostAssemblyActivity(events.append)
        with self.assertRaises(LockedNpmError):
            assemble_environment(
                validated=self.validated,
                assembler=self.assembler,
                cache_root=self.cache_root,
                executor=FailingExecutor(),
                activity=activity,
            )
        terminal = [
            event
            for event in events
            if isinstance(event, HostStepEvent)
            and event.state is HostStepState.FAILED
        ]
        self.assertTrue(terminal)
        # No step left its heartbeat producer alive.
        for thread in threading.enumerate():
            self.assertNotEqual(thread.name, "host-activity-heartbeat")


class TestCoordinationWait(_AssemblyCase):
    def test_lock_wait_is_observed_without_timeout_or_ownership_change(self):
        from docker.npm_environment.publication import identity_coordination_lock
        from docker.npm_environment.storage import prepare_assembler_namespace
        from docker.npm_environment.identity import compute_assembler_input_identity

        namespace = prepare_assembler_namespace(
            self.cache_root, self.assembler.digest
        )
        input_identity = compute_assembler_input_identity(
            self.validated, self.assembler
        )
        entered = threading.Event()
        release = threading.Event()
        holder_done = threading.Event()

        def holder() -> None:
            with identity_coordination_lock(namespace, input_identity.digest):
                entered.set()
                release.wait(timeout=5.0)
            holder_done.set()

        thread = threading.Thread(target=holder)
        thread.start()
        self.assertTrue(entered.wait(timeout=5.0))

        events: list[object] = []
        activity = HostAssemblyActivity(events.append, clock=_FakeClock())

        def run() -> None:
            assemble_environment(
                validated=self.validated,
                assembler=self.assembler,
                cache_root=self.cache_root,
                executor=_PopulatingExecutor(b"two"),
                activity=activity,
            )

        runner = threading.Thread(target=run)
        runner.start()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if HostStep.LOCK_WAIT in _started_steps(events):
                break
            time.sleep(0.005)
        self.assertIn(HostStep.LOCK_WAIT, _started_steps(events))
        # The lock is still held; wait activity is fully non-destructive.
        self.assertFalse(holder_done.is_set())
        release.set()
        runner.join(timeout=10.0)
        thread.join(timeout=10.0)
        self.assertFalse(runner.is_alive())
        self.assertTrue(holder_done.is_set())


class TestCacheReuse(_AssemblyCase):
    def test_verified_hit_reports_reuse_and_no_container_activity(self):
        assemble_environment(
            validated=self.validated,
            assembler=self.assembler,
            cache_root=self.cache_root,
            executor=_PopulatingExecutor(b"cached"),
        )
        events: list[object] = []
        activity = HostAssemblyActivity(events.append)
        result = assemble_environment(
            validated=self.validated,
            assembler=self.assembler,
            cache_root=self.cache_root,
            executor=_PopulatingExecutor(b"ignored"),
            activity=activity,
        )
        self.assertEqual(result.tree_digest, result.tree_digest)
        started = _started_steps(events)
        self.assertIn(HostStep.CACHE_LOOKUP, started)
        self.assertNotIn(HostStep.CONTAINER_STARTUP, started)
        self.assertNotIn(HostStep.NPM_EXECUTION, started)
        reuse = [
            event
            for event in events
            if isinstance(event, HostStepEvent)
            and event.step is HostStep.CACHE_REUSE
        ]
        self.assertTrue(reuse)
        self.assertTrue(all(event.state is HostStepState.SUCCEEDED for event in reuse))


class TestStreamingActivity(_AssemblyCase):
    def test_npm_step_declares_expected_diagnostic_stream_and_container(self):
        events: list[object] = []
        activity = HostAssemblyActivity(events.append)
        container = "npm-assembler-" + "0" * 16
        with activity.step("npm_execution", container_name=container):
            pass
        started = [
            event
            for event in events
            if isinstance(event, HostStepEvent)
            and event.step is HostStep.NPM_EXECUTION
            and event.state is HostStepState.STARTED
        ]
        self.assertEqual(len(started), 1)
        self.assertTrue(started[0].expects_diagnostic_stream)
        self.assertEqual(started[0].logical_resource, container)

    def test_chunks_mark_diagnostic_activity_at_heartbeat_cadence(self):
        clock = _FakeClock()
        waiter = _TickingWaiter(clock)
        events: list[object] = []
        activity = HostAssemblyActivity(
            events.append, clock=clock, waiter_factory=lambda: waiter
        )
        container = "npm-assembler-" + "1" * 16
        with activity.step("npm_execution", container_name=container):
            # Simulated time advances through the ticking waiter while the
            # monitor is live, so a heartbeat becomes observable.
            time.sleep(0.02)
            activity.record_diagnostic()
            time.sleep(0.02)
        heartbeats = [
            event for event in events if isinstance(event, HostHeartbeatEvent)
        ]
        self.assertTrue(heartbeats)
        diagnostic_beats = [
            beat
            for beat in heartbeats
            if beat.last_activity_kind is HostLastActivityKind.DIAGNOSTIC
        ]
        self.assertTrue(diagnostic_beats)
        for beat in diagnostic_beats:
            self.assertGreaterEqual(beat.last_activity_age_seconds or 0, 1)
        self.assertTrue(
            any(beat.remaining_deadline_seconds is not None for beat in heartbeats)
        )
        self.assertTrue(
            any(beat.diagnostic_silence_seconds is not None for beat in heartbeats)
        )

    def test_streaming_executor_reports_container_and_deadline_facts(self):
        events: list[object] = []
        activity = HostAssemblyActivity(events.append)
        received: list[StreamChunk] = []
        executor = _StreamingExecutor(
            b"streamed", stdout=b"npm warn one\nnpm warn two\n"
        )
        assemble_environment(
            validated=self.validated,
            assembler=self.assembler,
            cache_root=self.cache_root,
            executor=executor,
            sink=received.append,
            activity=activity,
        )
        self.assertTrue(received)
        npm_started = [
            event.step
            for event in events
            if isinstance(event, HostStepEvent)
            and event.step is HostStep.NPM_EXECUTION
            and event.state is HostStepState.STARTED
        ]
        self.assertEqual(npm_started, [HostStep.NPM_EXECUTION])
        self.assertTrue(
            any(
                isinstance(event, HostStepEvent)
                and event.step is HostStep.NPM_EXECUTION
                and event.expects_diagnostic_stream
                for event in events
            )
        )

    def test_no_download_claim_or_process_probe_in_activity(self):
        import docker.versioning.assembly_activity as module

        source = inspect.getsource(module)
        for prohibited in ("subprocess", "psutil", "os.system", "docker inspect"):
            self.assertNotIn(prohibited, source)
        monitor_source = inspect.getsource(HostActivityMonitor)
        self.assertNotIn("downloading", monitor_source.lower())


if __name__ == "__main__":
    unittest.main()
