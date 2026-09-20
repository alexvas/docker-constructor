"""Phase 5 host download observability contracts.

These tests bind the RED deliverables for Phase 5 of
``improve-host-build-observability``:

* every reviewed build artifact and Pi release asset exposes presentation-neutral
  start / verified-cache-hit / byte-progress / terminal activity under its closed
  safe logical name, in execution order;
* every body chunk already yielded by the existing streaming boundary updates
  cumulative received bytes and latest ``transport_progress`` activity for the
  next heartbeat, with diagnostic silence absent and heartbeat cadence unchanged,
  and an explicit compatibility allowance for transports that expose no chunks;
* host acquisition failures carry the logical asset and the bounded safe
  exception-type chain without leaking URLs, proxies, credentials, or messages.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from docker.versioning import pi_assembly
from docker.versioning.activity_monitor import (
    FIRST_HEARTBEAT_SECONDS,
    HostActivityMonitor,
)
from docker.versioning.build_materialization import (
    MaterializationError,
    SelectedBuildArtifact,
    UrllibStreamingTransport,
    build_blob_path,
    materialize_build_artifacts,
    prepare_build_cache,
)
from docker.versioning.diagnostic_projection import (
    DiagnosticLogicalResource,
    DiagnosticResourceKind,
    project_host_acquisition_failure,
)
from docker.versioning.digest_identity import DigestIdentity
from docker.versioning.effective import resolve_build_projection
from docker.versioning.host_progress import (
    HostDiagnosticClassification,
    HostHeartbeatEvent,
    HostLastActivityKind,
    HostPhase,
    HostStep,
    HostStepEvent,
    HostStepState,
    HostStructuredDiagnostic,
    HostTransportProgressEvent,
)
from docker.versioning.inventory import load_inventory
from docker.versioning.pi_assembly import (
    PiAssemblyError,
    PiAssemblyRequest,
    materialize_pi,
)
from docker.versioning.pi_release import (
    INSTALL_PACKAGE_FILENAME,
    INSTALL_PACKAGE_LOCK_FILENAME,
    SHA256SUMS_FILENAME,
    PiReleaseError,
    PiReleaseSource,
    acquire_install_assets,
    derive_pi_release_urls,
)
from docker.npm_environment import build_tree_manifest
from tests.pi_fixtures import (
    FakePiReleaseTransport,
    assembled_pi_tree,
    pi_install_lock_bytes,
    pi_install_package_bytes,
)

_REPO = Path(__file__).resolve().parents[1]
_REVIEWED_ARTIFACT_NAMES = ("rustup", "uv", "rtk", "fd")


def _projection():
    inventory = load_inventory(_REPO / "docker-constructor.toml")
    return resolve_build_projection(inventory.build, {}, platform="linux-amd64")


def _payload(name: str) -> bytes:
    return f"payload::{name}".encode()


def _selected(
    name: str, payload: bytes | None = None, *, url: str | None = None,
) -> SelectedBuildArtifact:
    data = payload if payload is not None else _payload(name)
    return SelectedBuildArtifact(
        name=name,
        url=url if url is not None else f"https://downloads.example/{name}?token=shhh",
        identity=DigestIdentity.from_hex("sha256", hashlib.sha256(data).hexdigest()),
    )


class _KeyedTransport:
    """Serves a payload per reviewed artifact URL and records requests."""

    def __init__(self, payloads: dict[str, bytes], *, fail_urls=(), error=None):
        self.payloads = dict(payloads)
        self.fail_urls = set(fail_urls)
        self.error = error
        self.calls: list[str] = []

    def stream(self, url: str):
        self.calls.append(url)
        if url in self.fail_urls and self.error is not None:
            raise self.error
        payload = self.payloads[_artifact_name(url)]
        if payload:
            yield payload


def _artifact_name(url: str) -> str:
    return url.split("/")[-1].split("?")[0]


def _of(records, kind):
    return [event for event in records if isinstance(event, kind)]


def _artifact_steps(records):
    return [
        event
        for event in _of(records, HostStepEvent)
        if event.step is HostStep.ARTIFACT_ACQUISITION
    ]


class _FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _ManualWaiter:
    """``threading.Event``-compatible waiter driven by a fake clock."""

    def __init__(self, clock: _FakeClock) -> None:
        self._clock = clock
        self._condition = threading.Condition()
        self._stopped = False

    def wait(self, timeout: float) -> bool:
        with self._condition:
            deadline = self._clock() + float(timeout)
            self._condition.notify_all()
            while not self._stopped:
                if self._clock() >= deadline:
                    return False
                self._condition.wait(timeout=5.0)
            return True

    def set(self) -> None:
        with self._condition:
            self._stopped = True
            self._condition.notify_all()

    def release(self) -> None:
        with self._condition:
            self._condition.notify_all()


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class _ProjectFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.project = self.base / "project"
        self.project.mkdir()
        self.cache = self.base / "cache"
        self.cache.mkdir(mode=0o700)

    def materialize(
        self, artifacts, transport, *, sink=None, monitor_factory=None,
        failure_secrets=(),
    ):
        with patch(
            "docker.versioning.build_materialization.select_build_artifacts",
            return_value=tuple(artifacts),
        ):
            return materialize_build_artifacts(
                object(), constructor_project_root=self.project,
                cache_root=self.cache, transport=transport, event_sink=sink,
                monitor_factory=monitor_factory, failure_secrets=failure_secrets,
            )


class TestReviewedBuildArtifactActivity(_ProjectFixture):
    def test_streamed_miss_exposes_ordered_start_and_terminal_per_artifact(self):
        artifacts = tuple(_selected(name) for name in _REVIEWED_ARTIFACT_NAMES)
        transport = _KeyedTransport(
            {name: _payload(name) for name in _REVIEWED_ARTIFACT_NAMES}
        )
        records: list[object] = []
        paths = self.materialize(artifacts, transport, sink=records.append)
        self.assertEqual(4, len(paths))

        steps = _artifact_steps(records)
        self.assertEqual(
            [
                entry
                for name in _REVIEWED_ARTIFACT_NAMES
                for entry in (
                    (HostStepState.STARTED, name),
                    (HostStepState.SUCCEEDED, name),
                )
            ],
            [(step.state, step.logical_resource) for step in steps],
            "each artifact must start and finish once, in reviewed order",
        )
        for step in steps:
            self.assertIs(HostPhase.RELEASE_ACQUISITION, step.phase)
            self.assertFalse(step.expects_diagnostic_stream)

        progress = _of(records, HostTransportProgressEvent)
        self.assertEqual(
            [(name, len(_payload(name))) for name in _REVIEWED_ARTIFACT_NAMES],
            [(event.logical_resource, event.received_bytes) for event in progress],
        )

    def test_verified_cache_hit_reports_reuse_without_a_request(self):
        artifacts = tuple(_selected(name) for name in _REVIEWED_ARTIFACT_NAMES)
        self.materialize(
            artifacts,
            _KeyedTransport(
                {name: _payload(name) for name in _REVIEWED_ARTIFACT_NAMES}
            ),
        )
        records: list[object] = []
        bomb = _KeyedTransport(
            {name: _payload(name) for name in _REVIEWED_ARTIFACT_NAMES},
            fail_urls={f"https://downloads.example/{name}?token=shhh"
                       for name in _REVIEWED_ARTIFACT_NAMES},
            error=AssertionError("network on verified hit"),
        )
        self.materialize(artifacts, bomb, sink=records.append)
        self.assertEqual([], bomb.calls)
        reuse = [
            event for event in _of(records, HostStepEvent)
            if event.step is HostStep.CACHE_REUSE
        ]
        self.assertEqual(
            list(_REVIEWED_ARTIFACT_NAMES),
            [event.logical_resource for event in reuse],
        )
        self.assertTrue(
            all(event.state is HostStepState.SUCCEEDED for event in reuse)
        )
        self.assertEqual([], _of(records, HostTransportProgressEvent))
        self.assertFalse(
            any(
                event.step is HostStep.ARTIFACT_ACQUISITION
                and event.state is HostStepState.FAILED
                for event in _of(records, HostStepEvent)
            )
        )
        for name in _REVIEWED_ARTIFACT_NAMES:
            started = records.index(HostStepEvent(
                HostPhase.RELEASE_ACQUISITION, HostStep.ARTIFACT_ACQUISITION,
                HostStepState.STARTED, False, name,
            ))
            reused = records.index(HostStepEvent(
                HostPhase.RELEASE_ACQUISITION, HostStep.CACHE_REUSE,
                HostStepState.SUCCEEDED, False, name,
            ))
            terminal = records.index(HostStepEvent(
                HostPhase.RELEASE_ACQUISITION, HostStep.ARTIFACT_ACQUISITION,
                HostStepState.SUCCEEDED, False, name,
            ))
            self.assertLess(started, reused, f"{name}: reuse precedes start")
            self.assertLess(reused, terminal, f"{name}: reuse precedes success")

    def test_no_observability_sink_stays_silent(self):
        artifacts = tuple(_selected(name) for name in _REVIEWED_ARTIFACT_NAMES)
        records: list[object] = []
        self.materialize(
            artifacts,
            _KeyedTransport(
                {name: _payload(name) for name in _REVIEWED_ARTIFACT_NAMES}
            ),
        )
        self.assertEqual([], records)

    def test_empty_body_transport_still_reports_start_and_terminal(self):
        artifact = _selected("fd", b"")
        transport = _KeyedTransport({"fd": b""})
        records: list[object] = []
        self.materialize((artifact,), transport, sink=records.append)
        self.assertEqual(
            [HostStepState.STARTED, HostStepState.SUCCEEDED],
            [step.state for step in _artifact_steps(records)],
        )
        self.assertEqual([], _of(records, HostTransportProgressEvent))


class TestTransportProgressFacts(_ProjectFixture):
    def test_every_chunk_updates_cumulative_bytes_for_the_next_heartbeat(self):
        clock = _FakeClock(100.0)
        waiter = _ManualWaiter(clock)
        records: list[object] = []
        monitor = HostActivityMonitor(
            phase=HostPhase.RELEASE_ACQUISITION,
            step=HostStep.ARTIFACT_ACQUISITION,
            expects_diagnostic_stream=False,
            sink=records.append,
            clock=clock,
            waiter=waiter,
            logical_resource="uv",
        )
        monitor.record_transport_progress(1000)
        monitor.record_transport_progress(2500)
        self.assertEqual(2500, monitor.received_bytes)

        clock.advance(FIRST_HEARTBEAT_SECONDS)
        waiter.release()
        self.assertTrue(
            _wait_until(lambda: _of(records, HostHeartbeatEvent)),
            "the first heartbeat must publish at three seconds",
        )

        progress = _of(records, HostTransportProgressEvent)
        self.assertEqual(
            [("uv", 2500)],
            [(event.logical_resource, event.received_bytes) for event in progress],
        )
        heartbeat = _of(records, HostHeartbeatEvent)[0]
        self.assertIs(
            HostLastActivityKind.TRANSPORT_PROGRESS, heartbeat.last_activity_kind
        )
        self.assertIsNone(heartbeat.diagnostic_silence_seconds)
        self.assertFalse(heartbeat.expects_diagnostic_stream)
        self.assertLess(
            records.index(progress[0]), records.index(heartbeat),
            "progress must precede the heartbeat that publishes it",
        )

        monitor.finish(HostStepState.SUCCEEDED)
        terminal = _artifact_steps(records)[-1]
        self.assertIs(HostStepState.SUCCEEDED, terminal.state)
        self.assertEqual(
            len(records) - 1,
            next(
                index for index, event in enumerate(records)
                if event is terminal
            ),
            "no progress or heartbeat may follow the terminal fact",
        )

    def test_transport_activity_does_not_change_the_heartbeat_cadence(self):
        clock = _FakeClock(0.0)
        waiter = _ManualWaiter(clock)
        records: list[object] = []
        monitor = HostActivityMonitor(
            phase=HostPhase.RELEASE_ACQUISITION,
            step=HostStep.ARTIFACT_ACQUISITION,
            expects_diagnostic_stream=False,
            sink=records.append,
            clock=clock,
            waiter=waiter,
            logical_resource="rtk",
        )
        for second in (3.0, 4.0, 5.0):
            target = int(second)
            clock.advance(second - clock.now)
            waiter.release()
            self.assertTrue(
                _wait_until(
                    lambda target=target: any(
                        beat.elapsed_seconds == target
                        for beat in _of(records, HostHeartbeatEvent)
                    )
                ),
                f"heartbeat at {target} seconds must publish",
            )
            monitor.record_transport_progress(target * 512)
        beats = _of(records, HostHeartbeatEvent)
        self.assertEqual([3, 4, 5], [beat.elapsed_seconds for beat in beats])
        self.assertTrue(
            all(beat.diagnostic_silence_seconds is None for beat in beats)
        )
        monitor.finish(HostStepState.SUCCEEDED)

    def test_single_chunk_transport_reports_one_cumulative_update(self):
        artifact = _selected("uv", b"x" * 4096)
        transport = _KeyedTransport({"uv": b"x" * 4096})
        records: list[object] = []
        self.materialize((artifact,), transport, sink=records.append)
        self.assertEqual(1, len(transport.calls))
        self.assertEqual(
            [4096],
            [event.received_bytes
             for event in _of(records, HostTransportProgressEvent)],
        )


class _GatedWriter:
    """A file-like wrapper whose ``write`` waits on a gate or raises."""

    def __init__(self, inner, *, gate=None, error=None) -> None:
        self._inner = inner
        self._gate = gate
        self._error = error

    def write(self, data):
        if self._gate is not None:
            self._gate.wait()
        if self._error is not None:
            raise self._error
        return self._inner.write(data)

    def flush(self):
        return self._inner.flush()

    def fileno(self):
        return self._inner.fileno()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)


class TestWriteBoundaryProgress(_ProjectFixture):
    """Task 5 — received bytes are counted before the disk write boundary."""

    def test_blocked_write_still_exposes_received_bytes_to_the_next_heartbeat(self):
        chunk = b"z" * 4096
        artifact = _selected("uv", chunk)
        transport = _KeyedTransport({"uv": chunk})
        clock = _FakeClock(10.0)
        waiter = _ManualWaiter(clock)
        records: list[object] = []
        monitor = HostActivityMonitor(
            phase=HostPhase.RELEASE_ACQUISITION,
            step=HostStep.ARTIFACT_ACQUISITION,
            expects_diagnostic_stream=False,
            sink=records.append,
            clock=clock,
            waiter=waiter,
            logical_resource="uv",
        )
        gate = threading.Event()
        errors: list[BaseException] = []
        real_fdopen = os.fdopen

        def gated_fdopen(fd, mode="r", *args, **kwargs):
            return _GatedWriter(real_fdopen(fd, mode, *args, **kwargs), gate=gate)

        def run() -> None:
            try:
                self.materialize(
                    (artifact,), transport, monitor_factory=lambda name: monitor,
                )
            except BaseException as exc:  # surfaced by the assertion below
                errors.append(exc)

        with patch(
            "docker.versioning.build_materialization.os.fdopen",
            side_effect=gated_fdopen,
        ):
            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            self.assertTrue(
                _wait_until(lambda: monitor.received_bytes == len(chunk)),
                "the yielded chunk must be counted before the write completes",
            )
            self.assertTrue(worker.is_alive(), "the write is still blocked")
            clock.advance(FIRST_HEARTBEAT_SECONDS)
            waiter.release()
            self.assertTrue(
                _wait_until(lambda: _of(records, HostHeartbeatEvent)),
                "a heartbeat must publish while the write is blocked",
            )
            heartbeat = _of(records, HostHeartbeatEvent)[0]
            self.assertIs(
                HostLastActivityKind.TRANSPORT_PROGRESS,
                heartbeat.last_activity_kind,
            )
            progress = _of(records, HostTransportProgressEvent)
            self.assertEqual(
                [("uv", len(chunk))],
                [(event.logical_resource, event.received_bytes) for event in progress],
            )
            self.assertLess(
                records.index(progress[0]), records.index(heartbeat),
                "progress must precede the heartbeat that exposes it",
            )
            gate.set()
            worker.join(timeout=5.0)
            self.assertFalse(worker.is_alive())
        self.assertEqual([], errors)

    def test_write_failure_keeps_final_progress_and_publishes_no_artifact(self):
        chunk = b"y" * 2048
        artifact = _selected("uv", chunk)
        transport = _KeyedTransport({"uv": chunk})
        records: list[object] = []
        paths = prepare_build_cache(self.project, cache_root=self.cache)
        blob = build_blob_path(paths.blobs_root, artifact.identity)
        real_fdopen = os.fdopen

        def failing_fdopen(fd, mode="r", *args, **kwargs):
            return _GatedWriter(
                real_fdopen(fd, mode, *args, **kwargs),
                error=OSError(28, "No space left on device"),
            )

        with patch(
            "docker.versioning.build_materialization.os.fdopen",
            side_effect=failing_fdopen,
        ):
            with self.assertRaises(MaterializationError):
                self.materialize((artifact,), transport, sink=records.append)

        progress = _of(records, HostTransportProgressEvent)
        self.assertEqual(
            [("uv", len(chunk))],
            [(event.logical_resource, event.received_bytes) for event in progress],
            "the yielded chunk must be reported even though the write failed",
        )
        failed = [
            event for event in _artifact_steps(records)
            if event.state is HostStepState.FAILED
        ]
        self.assertEqual(["uv"], [event.logical_resource for event in failed])
        self.assertEqual(
            [], list(paths.tmp_root.iterdir()),
            "temporary data must be cleaned up after a failed write",
        )
        self.assertFalse(blob.exists(), "no artifact may be published")


class TestHostAcquisitionFailureContext(_ProjectFixture):
    def test_failure_identifies_logical_artifact_without_leaking_secrets(self):
        artifact = _selected(
            "uv", url="https://user:pass@downloads.example/uv.tar.gz?token=topsecret"
        )
        inner = ValueError("https://user:pass@downloads.example/uv.tar.gz?token=topsecret")
        error = RuntimeError("proxy http://proxy.internal:3128/path?key=leaky")
        error.__cause__ = inner
        transport = _KeyedTransport({"uv": b""}, fail_urls={artifact.url}, error=error)
        records: list[object] = []
        with self.assertRaises(MaterializationError):
            self.materialize((artifact,), transport, sink=records.append)

        diagnostics = _of(records, HostStructuredDiagnostic)
        self.assertEqual(1, len(diagnostics))
        diagnostic = diagnostics[0]
        self.assertIs(HostDiagnosticClassification.ERROR, diagnostic.classification)
        self.assertEqual("uv", diagnostic.logical_resource)
        self.assertIs(HostPhase.RELEASE_ACQUISITION, diagnostic.phase)
        self.assertIs(HostStep.ARTIFACT_ACQUISITION, diagnostic.step)
        self.assertEqual(("downloads.example",), diagnostic.hostnames)

        text = diagnostic.text
        self.assertNotIn("downloads.example", text)
        for leaked in (
            "user", "pass", "topsecret", "proxy.internal", "3128", "leaky",
            "token=",
        ):
            self.assertNotIn(leaked, text, f"{leaked!r} leaked into failure text")
            self.assertNotIn(leaked, repr(diagnostic))
        self.assertIn("RuntimeError", text)
        self.assertIn("ValueError", text)

        failed = [
            event for event in _artifact_steps(records)
            if event.state is HostStepState.FAILED
        ]
        self.assertEqual(["uv"], [event.logical_resource for event in failed])
        self.assertLess(
            records.index(diagnostic),
            records.index(failed[0]),
            "the safe failure context precedes the failed terminal fact",
        )

    def test_registered_failure_secrets_are_redacted_from_text(self):
        artifact = _selected("rtk", url="https://downloads.example/rtk.deb")
        transport = _KeyedTransport(
            {"rtk": b""}, fail_urls={artifact.url},
            error=RuntimeError("proxy http://proxy.internal:3128"),
        )
        records: list[object] = []
        with self.assertRaises(MaterializationError):
            self.materialize(
                (artifact,), transport, sink=records.append,
                failure_secrets=("http://proxy.internal:3128",),
            )
        text = _of(records, HostStructuredDiagnostic)[0].text
        self.assertNotIn("proxy.internal", text)
        self.assertNotIn("3128", text)

    def test_exception_messages_are_never_evaluated(self):
        class _MessageBomb(Exception):
            def __str__(self) -> str:
                raise AssertionError("exception message evaluated")

        resource = DiagnosticLogicalResource(
            DiagnosticResourceKind.REVIEWED_ARTIFACT, "fd"
        )
        diagnostic = project_host_acquisition_failure(
            phase=HostPhase.RELEASE_ACQUISITION,
            step=HostStep.ARTIFACT_ACQUISITION,
            logical_resource=resource,
            reason=_MessageBomb(),
            url="https://downloads.example/fd?token=abc",
        )
        self.assertIs(HostDiagnosticClassification.ERROR, diagnostic.classification)
        self.assertEqual("fd", diagnostic.logical_resource)
        self.assertEqual(("downloads.example",), diagnostic.hostnames)


class _FailingOpener:
    """Stands in for urllib's request boundary and fails like a timeout."""

    def open(self, url, *args, **kwargs):
        raise urllib.error.URLError(TimeoutError("private detail"))


class TestUrllibTransportFailureDiagnostics(_ProjectFixture):
    """Task 5 — urllib's wrapped ``reason`` reaches the safe diagnostic chain."""

    def _urllib_transport(self) -> UrllibStreamingTransport:
        with patch("urllib.request.build_opener", return_value=_FailingOpener()):
            return UrllibStreamingTransport()

    def test_artifact_acquisition_reports_url_error_and_reason(self):
        artifact = _selected("uv")
        transport = self._urllib_transport()
        records: list[object] = []
        paths = prepare_build_cache(self.project, cache_root=self.cache)
        blob = build_blob_path(paths.blobs_root, artifact.identity)
        with self.assertRaises(MaterializationError):
            self.materialize((artifact,), transport, sink=records.append)

        diagnostics = _of(records, HostStructuredDiagnostic)
        self.assertEqual(1, len(diagnostics))
        diagnostic = diagnostics[0]
        self.assertEqual("uv", diagnostic.logical_resource)
        self.assertEqual(("downloads.example",), diagnostic.hostnames)
        self.assertIn("URLError", diagnostic.text)
        self.assertIn("TimeoutError", diagnostic.text)
        self.assertNotIn("private detail", diagnostic.text)
        self.assertNotIn("downloads.example", diagnostic.text)
        self.assertNotIn("token", diagnostic.text)
        self.assertEqual(
            ["uv"],
            [
                event.logical_resource for event in _artifact_steps(records)
                if event.state is HostStepState.FAILED
            ],
        )
        self.assertEqual(
            [], list(paths.tmp_root.iterdir()),
            "temporary data must be cleaned up after an acquisition failure",
        )
        self.assertFalse(blob.exists(), "no artifact may be published")

    def test_pi_acquisition_reports_url_error_and_reason(self):
        transport = self._urllib_transport()
        records: list[object] = []
        with patch.object(
            pi_assembly, "assemble_environment",
            side_effect=AssertionError("assembly must not run"),
        ):
            with self.assertRaises(PiAssemblyError):
                materialize_pi(PiAssemblyRequest(
                    projection=_projection(), transport=transport,
                    cache_root=Path(tempfile.mkdtemp()),
                    executor=SimpleNamespace(), event_sink=records.append,
                ))

        diagnostics = _of(records, HostStructuredDiagnostic)
        self.assertEqual(1, len(diagnostics))
        diagnostic = diagnostics[0]
        self.assertEqual(SHA256SUMS_FILENAME, diagnostic.logical_resource)
        self.assertEqual(("github.com",), diagnostic.hostnames)
        self.assertIn("URLError", diagnostic.text)
        self.assertIn("TimeoutError", diagnostic.text)
        self.assertNotIn("private detail", diagnostic.text)
        self.assertNotIn("github.com", diagnostic.text)


class _HostileRelationshipError(Exception):
    """Exception that raises when one relationship attribute is read."""

    _RELATIONSHIPS = frozenset({"reason", "__cause__", "__context__"})

    def __init__(self, hostile: str) -> None:
        super().__init__("hostile message must never be evaluated")
        object.__setattr__(self, "_hostile", hostile)

    def __getattribute__(self, name: str):
        if name in _HostileRelationshipError._RELATIONSHIPS:
            if name == object.__getattribute__(self, "_hostile"):
                raise RuntimeError("hostile relationship access")
        return object.__getattribute__(self, name)

    def __str__(self) -> str:
        raise AssertionError("exception message must never be evaluated")


class TestHostileExceptionDiagnostics(_ProjectFixture):
    """Hostile relationship access must not replace the acquisition failure."""

    def test_hostile_relationship_access_preserves_original_failure(self):
        artifact = _selected("uv")
        hostile = _HostileRelationshipError("reason")
        transport = _KeyedTransport(
            {"uv": b""}, fail_urls={artifact.url}, error=hostile,
        )
        records: list[object] = []
        with self.assertRaises(MaterializationError) as caught:
            self.materialize((artifact,), transport, sink=records.append)
        self.assertIsInstance(caught.exception.__cause__, _HostileRelationshipError)

        diagnostics = _of(records, HostStructuredDiagnostic)
        self.assertEqual(1, len(diagnostics))
        diagnostic = diagnostics[0]
        self.assertEqual("uv", diagnostic.logical_resource)
        self.assertIn("MaterializationError", diagnostic.text)
        self.assertIn("_HostileRelationshipError", diagnostic.text)
        self.assertNotIn("hostile relationship access", diagnostic.text)
        self.assertNotIn("hostile message", diagnostic.text)

        failed = [
            event for event in _artifact_steps(records)
            if event.state is HostStepState.FAILED
        ]
        self.assertEqual(
            ["uv"], [event.logical_resource for event in failed],
            "the monitor must still emit its terminal FAILED fact",
        )


class _ScopeRecorder:
    """Context-manager factory recording per-asset enter/exit/fail ordering."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.progress: dict[str, list[int]] = {}

    @contextmanager
    def __call__(self, name: str):
        self.events.append(("enter", name))
        self.progress.setdefault(name, [])
        try:
            yield (lambda received, name=name: self.progress[name].append(received))
        except BaseException:
            self.events.append(("fail", name))
            raise
        else:
            self.events.append(("exit", name))


def _pi_urls() -> tuple[PiReleaseSource, str, object]:
    version = _projection().pi_version
    source = PiReleaseSource(
        package="@earendil-works/pi-coding-agent",
        release_repository="earendil-works/pi",
        release_tag_prefix="v",
    )
    return source, version, derive_pi_release_urls(source, version)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class TestPiReleaseActivity(unittest.TestCase):
    def test_three_assets_observe_distinct_scopes_in_acquisition_order(self):
        _source, _version, urls = _pi_urls()
        package = b'{"name":"install-package"}'
        lock = b'{"lockfileVersion":3}'
        served = {
            urls.sha256sums: (
                f"{_sha256(package)}  {INSTALL_PACKAGE_FILENAME}\n"
                f"{_sha256(lock)}  {INSTALL_PACKAGE_LOCK_FILENAME}\n"
            ).encode(),
            urls.install_package: package,
            urls.install_package_lock: lock,
        }
        scopes = _ScopeRecorder()
        progress_forwarded = {
            SHA256SUMS_FILENAME: False,
            INSTALL_PACKAGE_FILENAME: False,
            INSTALL_PACKAGE_LOCK_FILENAME: False,
        }
        name_for_url = {
            urls.sha256sums: SHA256SUMS_FILENAME,
            urls.install_package: INSTALL_PACKAGE_FILENAME,
            urls.install_package_lock: INSTALL_PACKAGE_LOCK_FILENAME,
        }

        def download(url: str, progress=None) -> bytes:
            data = served[url]
            if progress is not None:
                progress_forwarded[name_for_url[url]] = True
                for index in range(0, len(data), 4):
                    progress(min(len(data), index + 4))
            return data

        acquire_install_assets(urls, download, activity=scopes)
        self.assertEqual(
            {
                SHA256SUMS_FILENAME: True,
                INSTALL_PACKAGE_FILENAME: True,
                INSTALL_PACKAGE_LOCK_FILENAME: True,
            },
            progress_forwarded,
            "the activity scope's progress callback reaches every asset",
        )
        self.assertEqual(
            [
                ("enter", SHA256SUMS_FILENAME),
                ("exit", SHA256SUMS_FILENAME),
                ("enter", INSTALL_PACKAGE_FILENAME),
                ("exit", INSTALL_PACKAGE_FILENAME),
                ("enter", INSTALL_PACKAGE_LOCK_FILENAME),
                ("exit", INSTALL_PACKAGE_LOCK_FILENAME),
            ],
            scopes.events,
        )
        self.assertEqual(
            [len(served[urls.sha256sums])],
            scopes.progress[SHA256SUMS_FILENAME][-1:],
        )
        self.assertEqual(
            [len(package)], scopes.progress[INSTALL_PACKAGE_FILENAME][-1:]
        )
        self.assertEqual(
            [len(lock)], scopes.progress[INSTALL_PACKAGE_LOCK_FILENAME][-1:]
        )

    def test_verification_failure_is_attributed_to_that_asset(self):
        _source, _version, urls = _pi_urls()
        package = b'{"name":"install-package"}'
        lock = b'{"lockfileVersion":3}'
        served = {
            urls.sha256sums: (
                f"{'0' * 64}  {INSTALL_PACKAGE_FILENAME}\n"
                f"{_sha256(lock)}  {INSTALL_PACKAGE_LOCK_FILENAME}\n"
            ).encode(),
            urls.install_package: package,
            urls.install_package_lock: lock,
        }
        scopes = _ScopeRecorder()
        with self.assertRaises(PiReleaseError):
            acquire_install_assets(
                urls, lambda url, progress=None: served[url], activity=scopes,
            )
        self.assertEqual(
            [
                ("enter", SHA256SUMS_FILENAME),
                ("exit", SHA256SUMS_FILENAME),
                ("enter", INSTALL_PACKAGE_FILENAME),
                ("fail", INSTALL_PACKAGE_FILENAME),
            ],
            scopes.events,
            "the lock is never acquired when the package digest mismatches",
        )

    def test_no_activity_preserves_the_one_argument_download_contract(self):
        _source, _version, urls = _pi_urls()
        package = b'{"name":"install-package"}'
        lock = b'{"lockfileVersion":3}'
        served = {
            urls.sha256sums: (
                f"{_sha256(package)}  {INSTALL_PACKAGE_FILENAME}\n"
                f"{_sha256(lock)}  {INSTALL_PACKAGE_LOCK_FILENAME}\n"
            ).encode(),
            urls.install_package: package,
            urls.install_package_lock: lock,
        }
        calls: list[str] = []

        def download(url: str) -> bytes:
            # One argument only: a second positional argument is a TypeError.
            calls.append(url)
            return served[url]

        got_package, got_lock = acquire_install_assets(urls, download)
        self.assertEqual((package, lock), (got_package, got_lock))
        self.assertEqual(
            [urls.sha256sums, urls.install_package, urls.install_package_lock],
            calls,
            "legacy callbacks are invoked once per asset in acquisition order",
        )

    def test_materialize_pi_success_exposes_three_distinct_logical_assets(self):
        source, version, urls = _pi_urls()
        package = pi_install_package_bytes().replace(b"0.84.4", version.encode())
        lock = pi_install_lock_bytes().replace(b"0.84.4", version.encode())
        transport = FakePiReleaseTransport(
            derive_pi_release_urls(source, version), package=package, lock=lock
        )

        def fake_assemble(**_kwargs):
            env_root = assembled_pi_tree()
            evidence = Path(tempfile.mkdtemp()) / "evidence.json"
            evidence.write_bytes(b"serialized assembler evidence")
            return SimpleNamespace(
                environment_root=env_root,
                tree_digest=build_tree_manifest(env_root).digest,
                evidence_digest="e" * 64,
                output_identity="o" * 64,
                evidence_path=evidence,
            )

        records: list[object] = []
        with patch.object(pi_assembly, "assemble_environment", side_effect=fake_assemble):
            materialize_pi(PiAssemblyRequest(
                projection=_projection(), transport=transport,
                cache_root=Path(tempfile.mkdtemp()), executor=SimpleNamespace(),
                event_sink=records.append,
            ))
        asset_steps = [
            event for event in _of(records, HostStepEvent)
            if event.step is HostStep.RELEASE_ACQUISITION
        ]
        self.assertEqual(
            [
                (HostStepState.STARTED, SHA256SUMS_FILENAME),
                (HostStepState.SUCCEEDED, SHA256SUMS_FILENAME),
                (HostStepState.STARTED, INSTALL_PACKAGE_FILENAME),
                (HostStepState.SUCCEEDED, INSTALL_PACKAGE_FILENAME),
                (HostStepState.STARTED, INSTALL_PACKAGE_LOCK_FILENAME),
                (HostStepState.SUCCEEDED, INSTALL_PACKAGE_LOCK_FILENAME),
            ],
            [(event.state, event.logical_resource) for event in asset_steps],
        )
        self.assertEqual(
            [urls.sha256sums, urls.install_package, urls.install_package_lock],
            transport.downloads,
            "observability must add no request and must preserve order",
        )
        self.assertEqual(
            list((SHA256SUMS_FILENAME, INSTALL_PACKAGE_FILENAME,
                  INSTALL_PACKAGE_LOCK_FILENAME)),
            [event.logical_resource
             for event in _of(records, HostTransportProgressEvent)],
        )

    def test_materialize_pi_attributes_transport_failure_to_the_asset(self):
        source, version, urls = _pi_urls()
        package = pi_install_package_bytes().replace(b"0.84.4", version.encode())
        lock = pi_install_lock_bytes().replace(b"0.84.4", version.encode())
        inner = FakePiReleaseTransport(
            derive_pi_release_urls(source, version), package=package, lock=lock
        )

        class FailingAfterSums:
            def stream(self, url: str):
                if url == urls.install_package:
                    raise RuntimeError(
                        "boom http://user:pass@github.example/releases?token=abc"
                    )
                yield from inner.stream(url)

        records: list[object] = []
        with self.assertRaises(Exception):
            materialize_pi(PiAssemblyRequest(
                projection=_projection(), transport=FailingAfterSums(),
                cache_root=Path(tempfile.mkdtemp()), executor=SimpleNamespace(),
                event_sink=records.append,
            ))
        diagnostics = _of(records, HostStructuredDiagnostic)
        self.assertEqual(1, len(diagnostics))
        diagnostic = diagnostics[0]
        self.assertEqual(INSTALL_PACKAGE_FILENAME, diagnostic.logical_resource)
        self.assertIs(HostDiagnosticClassification.ERROR, diagnostic.classification)
        self.assertNotIn("github.example", diagnostic.text)
        self.assertNotIn("user", diagnostic.text)
        self.assertNotIn("abc", diagnostic.text)
        urls_seen = [url for url in inner.downloads]
        self.assertEqual([urls.sha256sums], urls_seen)


if __name__ == "__main__":
    unittest.main()
