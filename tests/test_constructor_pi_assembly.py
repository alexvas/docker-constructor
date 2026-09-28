"""Phase 6 task 6.5 — side-effect-free preflight before Docker-backed assembly.

The orchestration acquires the exact release assets, runs the side-effect-free
preflight with the reviewed root and caller-owned Node/npm versions, and only
then invokes Docker-backed assembly with the exact preflight-produced validated
input.  The assembler identity binds the reviewed image reference, tool
versions, script/policy digests, and platform.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR.parent))

from docker.npm_environment import streaming as npm_streaming
from docker.versioning import pi_assembly
from docker.versioning.build_orchestration import BuildRequest, _host_failure_context
from docker.versioning.effective import resolve_build_projection
from docker.versioning.inventory import load_inventory
from docker.versioning.pi_assembly import (
    PiAssemblyError, PiAssemblyRequest, materialize_pi,
)
from docker.versioning.pi_release import PiReleaseSource, derive_pi_release_urls
from docker.versioning.model import NetworkUrlDisplay
from docker.versioning.host_presentation import (
    HostPresentationMode,
    HostPresentationSession,
    PresentationPlan,
    PresentationSelection,
)
from docker.versioning.host_progress import (
    GuardedHostEventSink, HostDiagnosticClassification, HostDiagnosticPrefix,
    HostDiagnosticStream, HostPhase, HostPhaseEvent, HostPhaseState, HostStep,
    HostStructuredDiagnostic, InternalDirectHostEventSink,
    lookup_host_failure,
)
from docker.npm_environment.streaming import StreamChunk
from docker.npm_environment.streaming import (
    DIAGNOSTIC_LINE_LIMIT_BYTES,
    OVERSIZED_DIAGNOSTIC_MARKER,
)
from docker.npm_environment.errors import LockedNpmError
from docker.npm_environment.preflight import _parse_install_package
from docker.npm_environment import (
    RootSpec,
    assembler_script_digest,
    build_tree_manifest,
    compute_assembler_identity,
    compute_assembler_input_identity,
    npm_policy_digest,
    npm_policy_flags,
    preflight,
)
from tests.pi_fixtures import (
    PI_BIN_TARGET,
    PI_PACKAGE,
    FakePiReleaseTransport,
    assembled_pi_tree,
    pi_install_lock_bytes,
    pi_install_package_bytes,
)

_REPO = _THIS_DIR.parent


def _projection():
    inv = load_inventory(_REPO / "docker-constructor.toml")
    return resolve_build_projection(inv.build, {}, platform="linux-amd64")


def _evidence_path() -> Path:
    path = Path(tempfile.mkdtemp()) / "evidence.json"
    path.write_bytes(b"serialized assembler evidence")
    return path


def _release_fixture_for_projection() -> tuple[str, bytes, bytes]:
    """Return synthetic release assets matching the reviewed Pi version."""
    version = _projection().pi_version
    package = pi_install_package_bytes().replace(b"0.84.4", version.encode())
    lock = pi_install_lock_bytes().replace(b"0.84.4", version.encode())
    return version, package, lock


class TestMaterializePiOrchestration(unittest.TestCase):
    def test_preflight_precedes_assembly_and_binds_exact_inputs(self):
        version, package, lock = _release_fixture_for_projection()
        source = PiReleaseSource(
            package=PI_PACKAGE,
            release_repository="earendil-works/pi",
            release_tag_prefix="v",
        )
        urls = derive_pi_release_urls(source, version)
        transport = FakePiReleaseTransport(urls, package=package, lock=lock)

        calls: list[dict] = []

        def fake_assemble(*, validated, assembler, cache_root, executor, **kwargs):
            calls.append({
                "validated": validated,
                "assembler": assembler,
            })
            env_root = assembled_pi_tree()
            return SimpleNamespace(
                environment_root=env_root,
                tree_digest=build_tree_manifest(env_root).digest,
                evidence_digest="e" * 64,
                output_identity="o" * 64,
                evidence_path=_evidence_path(),
            )

        projection = _projection()
        request = PiAssemblyRequest(
            projection=projection,
            transport=transport,
            cache_root=Path(tempfile.mkdtemp()),
            executor=SimpleNamespace(run=lambda argv: None),
        )
        with patch.object(pi_assembly, "assemble_environment", side_effect=fake_assemble):
            result = materialize_pi(request)

        # Downloads happened before assembly and covered exactly three assets.
        self.assertEqual(len(transport.downloads), 3)
        self.assertTrue(transport.downloads[0].endswith("SHA256SUMS"))
        self.assertEqual(len(calls), 1)

        validated = calls[0]["validated"]
        self.assertEqual(validated.lockfile_digest, hashlib.sha256(lock).hexdigest())
        self.assertEqual(validated.roots, (RootSpec(PI_PACKAGE, version),))
        self.assertEqual(validated.platform, "linux-x64")
        self.assertEqual(validated.node_version, "24.18.0")
        self.assertEqual(validated.npm_version, "11.16.0")
        # The install-package manifest is a bound input, not a discard.
        self.assertEqual(validated.package_bytes, package)
        self.assertEqual(
            validated.package_digest, hashlib.sha256(package).hexdigest(),
        )

        assembler = calls[0]["assembler"]
        self.assertEqual(assembler.image_digest, projection.node.image)
        self.assertEqual(assembler.node_version, "24.18.0")
        self.assertEqual(assembler.npm_version, "11.16.0")
        self.assertEqual(assembler.script_digest, assembler_script_digest())
        self.assertEqual(assembler.policy_digest, npm_policy_digest())
        self.assertEqual(assembler.platform, "linux-x64")

        # Attestation bindings are carried through to the result.
        self.assertEqual(result.output_identity, "o" * 64)
        self.assertEqual(result.assembler_evidence_digest, "e" * 64)
        self.assertEqual(
            result.assembler_evidence_bytes_digest,
            hashlib.sha256(b"serialized assembler evidence").hexdigest(),
        )
        self.assertEqual(result.tree_digest, result.result.tree_digest)
        self.assertEqual(len(result.launcher_evidence_digest), 64)
        self.assertIn(PI_BIN_TARGET, result.launcher_plan.contents.decode())

    def test_materialization_emits_ordered_host_phase_events(self):
        version, package, lock = _release_fixture_for_projection()
        source = PiReleaseSource(PI_PACKAGE, "earendil-works/pi", "v")
        transport = FakePiReleaseTransport(
            derive_pi_release_urls(source, version), package=package, lock=lock
        )
        events = []

        def fake_assemble(**kwargs):
            env_root = assembled_pi_tree()
            return SimpleNamespace(
                environment_root=env_root,
                tree_digest=build_tree_manifest(env_root).digest,
                evidence_digest="e" * 64,
                output_identity="o" * 64,
                evidence_path=_evidence_path(),
            )

        with patch.object(pi_assembly, "assemble_environment", side_effect=fake_assemble):
            materialize_pi(PiAssemblyRequest(
                projection=_projection(), transport=transport,
                cache_root=Path(tempfile.mkdtemp()), executor=SimpleNamespace(),
                event_sink=events.append,
            ))

        phases = [event for event in events if isinstance(event, HostPhaseEvent)]
        self.assertEqual(phases, [
            HostPhaseEvent(HostPhase.RELEASE_ACQUISITION, HostPhaseState.STARTED),
            HostPhaseEvent(HostPhase.RELEASE_ACQUISITION, HostPhaseState.SUCCEEDED),
            HostPhaseEvent(HostPhase.LOCKED_ASSEMBLY, HostPhaseState.STARTED),
            HostPhaseEvent(HostPhase.LOCKED_ASSEMBLY, HostPhaseState.SUCCEEDED),
            HostPhaseEvent(HostPhase.DERIVED_VALIDATION, HostPhaseState.STARTED),
            HostPhaseEvent(HostPhase.DERIVED_VALIDATION, HostPhaseState.SUCCEEDED),
        ])

    def test_diagnostics_are_typed_and_precede_assembly_terminal(self):
        version, package, lock = _release_fixture_for_projection()
        source = PiReleaseSource(PI_PACKAGE, "earendil-works/pi", "v")
        transport = FakePiReleaseTransport(
            derive_pi_release_urls(source, version), package=package, lock=lock
        )
        events = []

        def fake_assemble(**kwargs):
            kwargs["sink"](StreamChunk("stderr", "already-redacted EOF partial"))
            env_root = assembled_pi_tree()
            return SimpleNamespace(
                environment_root=env_root,
                tree_digest=build_tree_manifest(env_root).digest,
                evidence_digest="e" * 64,
                output_identity="o" * 64,
                evidence_path=_evidence_path(),
            )

        with patch.object(pi_assembly, "assemble_environment", side_effect=fake_assemble):
            materialize_pi(PiAssemblyRequest(
                projection=_projection(), transport=transport,
                cache_root=Path(tempfile.mkdtemp()), executor=SimpleNamespace(),
                event_sink=events.append,
            ))

        diagnostic = HostStructuredDiagnostic(
            phase=HostPhase.LOCKED_ASSEMBLY,
            step=HostStep.NPM_EXECUTION,
            stream=HostDiagnosticStream.STDERR,
            classification=HostDiagnosticClassification.STATUS,
            text="already-redacted EOF partial",
        )
        self.assertIn(diagnostic, events)
        self.assertLess(events.index(diagnostic), events.index(HostPhaseEvent(
            HostPhase.LOCKED_ASSEMBLY, HostPhaseState.SUCCEEDED,
        )))

    def test_no_sink_release_failure_keeps_asset_context(self):
        class FailingTransport:
            policy = None

            def stream(self, url):
                raise OSError("release unavailable")
                yield b""  # pragma: no cover

        with self.assertRaises(PiAssemblyError) as caught:
            materialize_pi(PiAssemblyRequest(
                projection=_projection(),
                transport=FailingTransport(),
                cache_root=Path(tempfile.mkdtemp()),
                executor=SimpleNamespace(),
                event_sink=None,
            ))
        marker = lookup_host_failure(caught.exception)
        self.assertIsNotNone(marker)
        assert marker is not None
        self.assertIs(HostPhase.RELEASE_ACQUISITION, marker.phase)
        self.assertIs(HostStep.RELEASE_ACQUISITION, marker.step)
        self.assertEqual("SHA256SUMS", marker.logical_resource)

    def test_no_sink_npm_failure_keeps_step_resource_and_tail(self):
        version, package, lock = _release_fixture_for_projection()
        source = PiReleaseSource(PI_PACKAGE, "earendil-works/pi", "v")
        transport = FakePiReleaseTransport(
            derive_pi_release_urls(source, version), package=package, lock=lock
        )
        failure = LockedNpmError(
            "npm_exit_nonzero",
            "npm failed",
            summary="locked npm execution failed",
            diagnostic_tail="retained npm tail",
            diagnostic_stream="stderr",
        )

        def fail_assemble(*, activity, assembler, **kwargs):
            with activity.step(
                "npm_execution", container_name="npm-assembler-0123456789abcdef"
            ):
                raise failure

        with patch.object(
            pi_assembly, "assemble_environment", side_effect=fail_assemble
        ):
            with self.assertRaises(LockedNpmError) as caught:
                materialize_pi(PiAssemblyRequest(
                    projection=_projection(),
                    transport=transport,
                    cache_root=Path(tempfile.mkdtemp()),
                    executor=SimpleNamespace(),
                    event_sink=None,
                ))
        marker = lookup_host_failure(caught.exception)
        self.assertIsNotNone(marker)
        assert marker is not None
        self.assertIs(HostPhase.LOCKED_ASSEMBLY, marker.phase)
        self.assertIs(HostStep.NPM_EXECUTION, marker.step)
        self.assertEqual(
            "npm-assembler-0123456789abcdef", marker.logical_resource
        )
        context = _host_failure_context(caught.exception)
        self.assertEqual("locked npm execution failed", context.summary)
        self.assertEqual("retained npm tail", context.tail)
        self.assertEqual("npm-assembler-0123456789abcdef", context.logical_resource)

    def test_failed_assembly_has_one_terminal_and_starts_no_later_phase(self):
        version, package, lock = _release_fixture_for_projection()
        source = PiReleaseSource(PI_PACKAGE, "earendil-works/pi", "v")
        transport = FakePiReleaseTransport(
            derive_pi_release_urls(source, version), package=package, lock=lock
        )
        events = []
        with patch.object(
            pi_assembly, "assemble_environment", side_effect=RuntimeError("secret")
        ):
            with self.assertRaises(RuntimeError):
                materialize_pi(PiAssemblyRequest(
                    projection=_projection(), transport=transport,
                    cache_root=Path(tempfile.mkdtemp()), executor=SimpleNamespace(),
                    event_sink=events.append,
                ))
        self.assertEqual(1, events.count(HostPhaseEvent(
            HostPhase.LOCKED_ASSEMBLY, HostPhaseState.FAILED,
        )))
        self.assertFalse(any(
            isinstance(event, HostPhaseEvent)
            and event.phase is HostPhase.DERIVED_VALIDATION
            for event in events
        ))
        self.assertNotIn("secret", repr(events))

    def test_npm_policy_never_enables_bin_links_or_engine_strict(self):
        self.assertIn("--no-bin-links", npm_policy_flags())
        self.assertNotIn("--engine-strict", npm_policy_flags())
        self.assertIn("--ignore-scripts", npm_policy_flags())

    def test_no_network_transport_rejects_any_request(self):
        from tests.pi_fixtures import NoNetworkTransport

        transport = NoNetworkTransport()
        with self.assertRaises(AssertionError) as ctx:
            list(transport.stream("https://github.com/earendil-works/pi/anything"))
        self.assertIn("unexpected outbound network request", str(ctx.exception))
        self.assertIn("github.com", str(ctx.exception))


class TestInstallPackageBinding(unittest.TestCase):
    """The install-package manifest is a bound assembler input, not a discard."""

    def _preflight(self, package_bytes):
        return preflight(
            pi_install_lock_bytes(),
            package_bytes=package_bytes,
            roots=(RootSpec(PI_PACKAGE, "0.84.4"),),
            platform="linux-x64",
            node_version="24.18.0",
            npm_version="11.16.0",
        )

    def _assembler(self):
        return compute_assembler_identity(
            image_digest="sha256:" + "0" * 64,
            node_version="24.18.0",
            npm_version="11.16.0",
            script_digest=assembler_script_digest(),
            policy_digest=npm_policy_digest(),
            platform="linux-x64",
        )

    def test_malformed_package_json_rejected(self):
        with self.assertRaises(LockedNpmError) as ctx:
            self._preflight(b"not-json")
        self.assertEqual(ctx.exception.reason, "malformed_package")

    def test_wrong_package_name_rejected(self):
        bad = json.dumps({
            "name": "@earendil-works/pi-coding-agent-wrong",
            "version": "0.84.4",
            "dependencies": {PI_PACKAGE: "0.84.4"},
        }).encode()
        with self.assertRaises(LockedNpmError) as ctx:
            self._preflight(bad)
        self.assertEqual(ctx.exception.reason, "package_name_mismatch")

    def test_wrong_package_version_rejected(self):
        bad = json.dumps({
            "name": "@earendil-works/pi-coding-agent-install",
            "version": "9.9.9",
            "dependencies": {PI_PACKAGE: "0.84.4"},
        }).encode()
        with self.assertRaises(LockedNpmError) as ctx:
            self._preflight(bad)
        self.assertEqual(ctx.exception.reason, "package_version_mismatch")

    def test_package_lock_root_disagreement_rejected(self):
        bad = json.dumps({
            "name": "@earendil-works/pi-coding-agent-install",
            "version": "0.84.4",
            "dependencies": {PI_PACKAGE: "0.84.3"},
        }).encode()
        with self.assertRaises(LockedNpmError) as ctx:
            self._preflight(bad)
        self.assertEqual(ctx.exception.reason, "package_lock_root_disagreement")

    def test_package_bytes_changed_after_preflight_rejected(self):
        package = pi_install_package_bytes()
        validated = self._preflight(package)
        tampered = dataclasses.replace(validated, package_bytes=package + b"\n")
        with self.assertRaises(LockedNpmError) as ctx:
            compute_assembler_input_identity(tampered, self._assembler())
        self.assertEqual(ctx.exception.reason, "package_bytes_mismatch")

    def test_package_substitution_changes_identity(self):
        assembler = self._assembler()
        first = compute_assembler_input_identity(
            self._preflight(pi_install_package_bytes()), assembler,
        )
        # Same semantic manifest, different exact bytes (no trailing newline).
        substituted = json.dumps({
            "name": "@earendil-works/pi-coding-agent-install",
            "version": "0.84.4",
            "dependencies": {PI_PACKAGE: "0.84.4"},
        }, sort_keys=True).encode()
        second = compute_assembler_input_identity(
            self._preflight(substituted), assembler,
        )
        self.assertNotEqual(first.package_digest, second.package_digest)
        self.assertNotEqual(first.digest, second.digest)

    def test_package_substitution_invalidates_identity(self):
        substituted = json.dumps({
            "name": "@earendil-works/pi-coding-agent-install",
            "version": "0.84.4",
            "dependencies": {PI_PACKAGE: "0.84.3"},
        }).encode()
        with self.assertRaises(LockedNpmError) as ctx:
            self._preflight(substituted)
        self.assertEqual(ctx.exception.reason, "package_lock_root_disagreement")


class TestFacadeStreamingNormalization(unittest.TestCase):
    def _exercise(self, event_sink):
        version, package, lock = _release_fixture_for_projection()
        source = PiReleaseSource(
            package=PI_PACKAGE,
            release_repository="earendil-works/pi",
            release_tag_prefix="v",
        )
        urls = derive_pi_release_urls(source, version)
        transport = FakePiReleaseTransport(urls, package=package, lock=lock)
        build_request = BuildRequest(
            inventory_path="docker-constructor.toml",
            project_root=_REPO,
            event_sink=event_sink,
        )
        request = PiAssemblyRequest(
            projection=_projection(),
            transport=transport,
            cache_root=Path(tempfile.mkdtemp()),
            executor=SimpleNamespace(run=lambda argv: None),
            event_sink=build_request.event_sink,
        )

        normalized_sink = request.event_sink
        self.assertIsNotNone(normalized_sink)
        streamed: list[str] = []

        def fake_assemble(*, sink, **kwargs):
            stdout = iter((b"streamed status\n", b""))
            stderr = iter((b"",))
            npm_streaming.collect_streams(
                stdout_read=lambda _size: next(stdout),
                stderr_read=lambda _size: next(stderr),
                secrets=(),
                sink=sink,
            )
            streamed.append("complete")
            env_root = assembled_pi_tree()
            return SimpleNamespace(
                environment_root=env_root,
                tree_digest=build_tree_manifest(env_root).digest,
                evidence_digest="e" * 64,
                output_identity="o" * 64,
                evidence_path=_evidence_path(),
            )

        with (
            patch.object(pi_assembly, "assemble_environment", side_effect=fake_assemble),
            patch.object(
                npm_streaming,
                "SinkDispatcher",
                wraps=npm_streaming.SinkDispatcher,
            ) as dispatcher,
        ):
            materialize_pi(request)

        self.assertEqual(["complete"], streamed)
        return normalized_sink, dispatcher.call_count

    def test_facade_sink_keeps_direct_enqueue_through_both_requests(self):
        renderer = SimpleNamespace(
            set_status=lambda text: None,
            set_slot=lambda text: None,
            clear_slot=lambda: None,
            clear_status=lambda: None,
            clear_all=lambda: None,
            durable=lambda text: None,
            finalize_diagnostic=lambda text, restore_status=None: None,
        )
        session = HostPresentationSession(
            renderer,
            PresentationPlan(HostPresentationMode.LINES, PresentationSelection.LIVE),
        )
        try:
            normalized_sink, dispatcher_calls = self._exercise(session.sink)
            self.assertIs(normalized_sink, session.sink)
            self.assertIsInstance(normalized_sink, InternalDirectHostEventSink)
            self.assertNotIsInstance(normalized_sink, GuardedHostEventSink)
            self.assertEqual(0, dispatcher_calls)
        finally:
            session.shutdown()

    def test_external_callback_stays_dispatched_after_both_requests(self):
        delivered = []
        normalized_sink, dispatcher_calls = self._exercise(delivered.append)
        self.assertIsInstance(normalized_sink, GuardedHostEventSink)
        self.assertNotIsInstance(normalized_sink, InternalDirectHostEventSink)
        self.assertEqual(1, dispatcher_calls)
        self.assertTrue(
            any(isinstance(event, HostStructuredDiagnostic) for event in delivered)
        )


class _RecordingPresentationRenderer:
    """Renderer recording worker calls and signalling provisional visibility."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.slot_visible = threading.Event()
        self.marker_visible = threading.Event()
        self.marker_durable = threading.Event()

    def set_status(self, text: str) -> None:
        self.calls.append(("status", text))

    def set_slot(self, text: str) -> None:
        self.calls.append(("slot", text))
        self.slot_visible.set()
        if OVERSIZED_DIAGNOSTIC_MARKER in text:
            self.marker_visible.set()

    def clear_slot(self) -> None:
        self.calls.append(("clear_slot",))

    def clear_status(self) -> None:
        self.calls.append(("clear_status",))

    def clear_all(self) -> None:
        self.calls.append(("clear_all",))

    def durable(self, text: str) -> None:
        self.calls.append(("durable", text))
        if OVERSIZED_DIAGNOSTIC_MARKER in text:
            self.marker_durable.set()

    def finalize_diagnostic(
        self, text: str, *, restore_status: str | None
    ) -> None:
        self.calls.append(("finalize", text))


def _durable_text(calls: list[tuple[object, ...]]) -> str:
    return "\n".join(
        str(call[1]) for call in calls if call[0] == "durable"
    )


class TestFacadeProvisionalPrefixRoute(unittest.TestCase):
    """Task 2.10/2.11 -- the production presentation route shows prefixes.

    Exercises the real path
    ``NpmDiagnosticStream -> collect_streams -> pi_assembly._route_internal
    -> host event sink -> presentation actor -> renderer``.
    """

    def _materialize(
        self,
        event_sink,
        stdout_read,
        captures,
        sdk_event_sink=None,
        network_url_display=NetworkUrlDisplay.REDACTED,
        after_collect=None,
    ):
        version, package, lock = _release_fixture_for_projection()
        source = PiReleaseSource(
            package=PI_PACKAGE,
            release_repository="earendil-works/pi",
            release_tag_prefix="v",
        )
        urls = derive_pi_release_urls(source, version)
        transport = FakePiReleaseTransport(urls, package=package, lock=lock)
        build_request = BuildRequest(
            inventory_path="docker-constructor.toml",
            project_root=_REPO,
            event_sink=event_sink,
            sdk_event_sink=sdk_event_sink,
            network_url_display=network_url_display,
        )
        request = PiAssemblyRequest(
            projection=_projection(),
            transport=transport,
            cache_root=Path(tempfile.mkdtemp()),
            executor=SimpleNamespace(run=lambda argv: None),
            event_sink=build_request.event_sink,
            sdk_event_sink=build_request.sdk_event_sink,
            network_url_display=build_request.network_url_display,
        )

        def fake_assemble(*, sink, stream_factory=None, **kwargs):
            captures.append(
                npm_streaming.collect_streams(
                    stdout_read=stdout_read,
                    stderr_read=lambda _size: b"",
                    secrets=(),
                    sink=sink,
                    stream_factory=stream_factory,
                )
            )
            if after_collect is not None:
                after_collect()
            env_root = assembled_pi_tree()
            return SimpleNamespace(
                environment_root=env_root,
                tree_digest=build_tree_manifest(env_root).digest,
                evidence_digest="e" * 64,
                output_identity="o" * 64,
                evidence_path=_evidence_path(),
            )

        with patch.object(
            pi_assembly, "assemble_environment", side_effect=fake_assemble
        ):
            return materialize_pi(request)

    def test_no_newline_prefix_is_visible_before_the_record_boundary(self):
        renderer = _RecordingPresentationRenderer()
        session = HostPresentationSession(
            renderer,
            PresentationPlan(
                HostPresentationMode.INTERACTIVE,
                PresentationSelection.LIVE,
            ),
        )
        captures: list = []
        sent = {"prefix": False, "newline": False}
        snapshot: dict = {}

        def stdout_read(_size):
            if not sent["prefix"]:
                sent["prefix"] = True
                return b"npm warn partial"
            if not sent["newline"]:
                # The committed prefix must reach the renderer before the
                # record boundary is released.
                self.assertTrue(renderer.slot_visible.wait(5.0))
                snapshot["calls"] = list(renderer.calls)
                sent["newline"] = True
                return b"\n"
            return b""

        try:
            self._materialize(session.sink, stdout_read, captures)
        finally:
            session.shutdown()

        self.assertTrue(renderer.slot_visible.is_set())
        # The diagnostic line itself must not have been written durably or
        # finalized before its record boundary; lifecycle lines may appear.
        before_durable = _durable_text(snapshot["calls"])
        self.assertNotIn("npm warn partial", before_durable)
        self.assertFalse(
            any(call[0] == "finalize" for call in snapshot["calls"])
        )
        # The complete line is finalized exactly once and the retained tail
        # carries it exactly once.
        finalizes = [
            call[1] for call in renderer.calls if call[0] == "finalize"
        ]
        self.assertEqual(["npm warn partial"], finalizes)
        self.assertEqual(
            captures[0].stdout_tail.count("npm warn partial"), 1
        )

    _PREFIX_URL = (
        "https://user:secret@registry.example.com:8443"
        "/pkg/-/pkg-1.0.0.tgz?q=1#frag"
    )
    _PREFIX_LINE = f"npm error network GET {_PREFIX_URL} failed"
    _PREFIX_HOST_PATH = "registry.example.com/pkg/-/pkg-1.0.0.tgz"

    def _mode_prefix_expectations(self):
        return {
            NetworkUrlDisplay.REDACTED: ("<redacted>", "registry.example.com"),
            NetworkUrlDisplay.HOST_PATH: (self._PREFIX_HOST_PATH, "https://"),
            NetworkUrlDisplay.EXACT: (self._PREFIX_URL, None),
        }

    def _session_for(self, display, renderer):
        return HostPresentationSession(
            renderer,
            PresentationPlan(
                HostPresentationMode.INTERACTIVE,
                PresentationSelection.LIVE,
                display,
            ),
        )

    def _slots(self, renderer):
        return [
            str(call[1]) for call in renderer.calls if call[0] == "slot"
        ]

    def test_mode_selected_prefix_is_visible_before_the_record_boundary(self):
        for display, (expected, forbidden) in (
            self._mode_prefix_expectations().items()
        ):
            with self.subTest(display=display):
                renderer = _RecordingPresentationRenderer()
                session = self._session_for(display, renderer)
                captures: list = []
                sent = {"prefix": False, "newline": False}
                snapshot: dict = {}

                def stdout_read(_size):
                    if not sent["prefix"]:
                        sent["prefix"] = True
                        return self._PREFIX_LINE.encode()
                    if not sent["newline"]:
                        # Hold the record boundary until the selected prefix
                        # is observable, then snapshot presentation.
                        deadline = time.monotonic() + 5.0
                        while time.monotonic() < deadline:
                            if any(
                                expected in slot
                                for slot in self._slots(renderer)
                            ):
                                break
                            time.sleep(0.01)
                        snapshot["slots"] = self._slots(renderer)
                        snapshot["durable"] = _durable_text(renderer.calls)
                        sent["newline"] = True
                        return b"\n"
                    return b""

                try:
                    self._materialize(
                        session.sink,
                        stdout_read,
                        captures,
                        network_url_display=display,
                    )
                finally:
                    session.shutdown()

                before = "\n".join(snapshot["slots"])
                self.assertIn(expected, before)
                if forbidden is not None:
                    self.assertNotIn(forbidden, before)
                # The line is not durably written before its record boundary.
                self.assertNotIn(expected, snapshot["durable"])
                # The complete line finalizes exactly once with the selected
                # representation and never duplicates the prefix.
                finalizes = [
                    call[1]
                    for call in renderer.calls
                    if call[0] == "finalize"
                ]
                self.assertEqual(1, len(finalizes))
                self.assertIn(expected, str(finalizes[0]))

    def test_combined_sinks_fan_out_mode_prefix_internally_and_no_sdk_prefix(self):
        for display, (expected, forbidden) in (
            self._mode_prefix_expectations().items()
        ):
            with self.subTest(display=display):
                renderer = _RecordingPresentationRenderer()
                session = self._session_for(display, renderer)
                delivered: list = []
                captures: list = []
                sent = {"prefix": False, "newline": False}
                snapshot: dict = {}

                def stdout_read(_size):
                    if not sent["prefix"]:
                        sent["prefix"] = True
                        return self._PREFIX_LINE.encode()
                    if not sent["newline"]:
                        deadline = time.monotonic() + 5.0
                        while time.monotonic() < deadline:
                            if any(
                                expected in slot
                                for slot in self._slots(renderer)
                            ):
                                break
                            time.sleep(0.01)
                        # No provisional prefix may have reached the SDK while
                        # the selected prefix was live internally.
                        snapshot["sdk"] = list(delivered)
                        sent["newline"] = True
                        return b"\n"
                    return b""

                try:
                    self._materialize(
                        session.sink,
                        stdout_read,
                        captures,
                        sdk_event_sink=delivered.append,
                        network_url_display=display,
                    )
                finally:
                    session.shutdown()

                self.assertFalse(
                    any(
                        isinstance(event, HostDiagnosticPrefix)
                        for event in snapshot["sdk"]
                    )
                )
                self.assertFalse(
                    any(
                        isinstance(event, HostStructuredDiagnostic)
                        for event in snapshot["sdk"]
                    )
                )
                structured = [
                    event
                    for event in delivered
                    if isinstance(event, HostStructuredDiagnostic)
                ]
                self.assertEqual(1, len(structured))
                # The external SDK diagnostic stays URL-free and path-free in
                # every mode, including the selected local representation.
                self.assertNotIn("https://", structured[0].text)
                self.assertNotIn("registry.example.com", structured[0].text)
                self.assertNotIn(
                    self._PREFIX_HOST_PATH, structured[0].text
                )

    def test_combined_sink_saturation_keeps_internal_records_prompt_and_bounded(
        self,
    ):
        """A backlogged SDK channel can never damage internal presentation.

        A one-slot SDK queue and a callback blocked on the first finalized
        record force later records to be dropped externally.  Internal
        presentation must stay prompt, its provisional snapshots must never
        combine two records, and internal finalized records must still all
        arrive.
        """
        renderer = _RecordingPresentationRenderer()
        session = self._session_for(NetworkUrlDisplay.REDACTED, renderer)
        sdk_events: list = []
        callback_entered = threading.Event()
        release_callback = threading.Event()
        drained: dict = {}

        def slow_sdk(event) -> None:
            sdk_events.append(event)
            callback_entered.set()
            release_callback.wait(5.0)

        markers = [f"record-{i}" for i in range(6)]
        # Each line ends with a completed token and a trailing space, so the
        # record marker is committed to the provisional prefix instead of
        # being withheld as a possible URL scheme.
        lines = [
            f"npm error network GET {self._PREFIX_URL} failed {marker} "
            for marker in markers
        ]
        # Fragment each record into a provisional prefix and its newline.  The
        # newline is released only after that record's prefix has been rendered,
        # which happens while the SDK callback is blocked on an earlier record.
        steps: list[tuple[bytes, str | None]] = []
        for line, marker in zip(lines, markers):
            steps.append((line.encode(), None))
            steps.append((b"\n", marker))
        state = {"index": 0}
        prompt: dict = {}

        def stdout_read(_size):
            index = state["index"]
            if index >= len(steps):
                # Every record has been read; release the blocked callback so
                # the dispatcher can finalize without hitting its drain budget.
                release_callback.set()
                return b""
            value, wait_marker = steps[index]
            state["index"] = index + 1
            if wait_marker is not None and wait_marker != markers[0]:
                # The first record already has the SDK callback blocked; this
                # later provisional prefix must still reach the renderer
                # before its record boundary is released.
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    if any(
                        wait_marker in slot
                        for slot in self._slots(renderer)
                    ):
                        prompt[wait_marker] = True
                        break
                    time.sleep(0.005)
            return value

        real_dispatcher = npm_streaming.SinkDispatcher
        captures: list = []

        def after_collect() -> None:
            # The SDK dispatcher is drained inside collect_streams, before the
            # operation's terminal event is admitted; snapshot the delivered
            # count at that point.
            drained["count"] = len(sdk_events)

        try:
            with patch.object(
                npm_streaming,
                "SinkDispatcher",
                side_effect=lambda sink, **kwargs: real_dispatcher(
                    sink, queue_capacity=1, **kwargs
                ),
            ):
                self._materialize(
                    session.sink,
                    stdout_read,
                    captures,
                    sdk_event_sink=slow_sdk,
                    network_url_display=NetworkUrlDisplay.REDACTED,
                    after_collect=after_collect,
                )
        finally:
            release_callback.set()
            session.shutdown()

        # Internal prefixes stayed prompt while the SDK callback was blocked.
        self.assertTrue(callback_entered.is_set())
        self.assertEqual(set(prompt), set(markers[1:]))

        # No provisional snapshot combined two records, and each stayed
        # within the bound of its own line.
        slot_texts = self._slots(renderer)
        self.assertTrue(slot_texts)
        max_line = max(len(line) for line in lines)
        for text in slot_texts:
            self.assertLessEqual(
                sum(marker in text for marker in markers), 1
            )
            self.assertLessEqual(len(text), max_line)

        # Every internal finalized record still arrived.
        finalizes = [
            call[1] for call in renderer.calls if call[0] == "finalize"
        ]
        for marker in markers:
            self.assertTrue(
                any(marker in text for text in finalizes), marker
            )

        # The SDK channel saw only safe, finalized structured diagnostics and
        # at least one was dropped by the one-slot queue.
        self.assertTrue(sdk_events)
        for event in sdk_events:
            self.assertIsInstance(event, HostStructuredDiagnostic)
            self.assertNotIn("https://", event.text)
            self.assertNotIn("registry.example.com", event.text)
            self.assertNotIn("secret", event.text)
        self.assertLess(len(sdk_events), len(markers))

        # No SDK callback ran after the dispatcher finished, so none can be
        # admitted after the assembly terminal event.
        self.assertEqual(drained["count"], len(sdk_events))

    def test_overflow_record_resets_the_internal_prefix_buffer(self):
        """An overflow record is a real boundary for the prefix buffer.

        Even when the external SDK channel is dropping nothing here, the
        following record's provisional and finalized snapshots must never
        carry the earlier overflow record's committed text.
        """
        renderer = _RecordingPresentationRenderer()
        session = self._session_for(NetworkUrlDisplay.REDACTED, renderer)
        captures: list = []
        limit = DIAGNOSTIC_LINE_LIMIT_BYTES
        oversized = (
            "npm error network GET "
            + self._PREFIX_URL
            + " failed overflow-marker "
            + "pad " * (limit // 4)
            + "DISCARDED" * 100
        )
        normal = (
            f"npm error network GET {self._PREFIX_URL} "
            "failed record-after "
        )
        reads = iter(
            [oversized.encode(), b"\n", normal.encode(), b"\n", b""]
        )

        def stdout_read(_size):
            return next(reads, b"")

        try:
            self._materialize(
                session.sink,
                stdout_read,
                captures,
                network_url_display=NetworkUrlDisplay.REDACTED,
            )
        finally:
            session.shutdown()

        after = [
            str(call[1])
            for call in renderer.calls
            if call[0] in ("slot", "finalize", "durable")
            and "record-after" in str(call[1])
        ]
        self.assertTrue(after)
        for text in after:
            self.assertNotIn("overflow-marker", text)

    def test_overflow_prefix_and_marker_use_the_selected_representation(self):
        expectations = {
            NetworkUrlDisplay.REDACTED: "<redacted>",
            NetworkUrlDisplay.HOST_PATH: self._PREFIX_HOST_PATH,
            NetworkUrlDisplay.EXACT: self._PREFIX_URL,
        }
        limit = DIAGNOSTIC_LINE_LIMIT_BYTES
        oversized = (
            "npm error network GET "
            + self._PREFIX_URL
            + " failed "
            + "pad " * (limit // 4)
            + "DISCARDED" * 100
        ).encode()
        for display, expected in expectations.items():
            with self.subTest(display=display):
                renderer = _RecordingPresentationRenderer()
                session = self._session_for(display, renderer)
                captures: list = []
                sent = {"oversized": False, "newline": False}
                snapshot: dict = {}

                def stdout_read(_size):
                    if not sent["oversized"]:
                        sent["oversized"] = True
                        return oversized
                    if not sent["newline"]:
                        deadline = time.monotonic() + 5.0
                        while time.monotonic() < deadline:
                            if any(
                                expected in slot
                                and OVERSIZED_DIAGNOSTIC_MARKER in slot
                                for slot in self._slots(renderer)
                            ):
                                break
                            time.sleep(0.01)
                        snapshot["slots"] = self._slots(renderer)
                        sent["newline"] = True
                        return b"\n"
                    return b""

                try:
                    self._materialize(
                        session.sink,
                        stdout_read,
                        captures,
                        network_url_display=display,
                    )
                finally:
                    session.shutdown()

                before = "\n".join(snapshot["slots"])
                self.assertIn(expected, before)
                self.assertIn(OVERSIZED_DIAGNOSTIC_MARKER, before)
                # The selected committed prefix precedes the overflow marker.
                self.assertLess(
                    before.index(expected),
                    before.index(OVERSIZED_DIAGNOSTIC_MARKER),
                )
                # The discarded overflow suffix never reaches live
                # presentation in any mode.
                self.assertNotIn(
                    "DISCARDED", "\n".join(self._slots(renderer))
                )
                # The record boundary closes the provisional state with the
                # complete mode-selected truncated line.
                finalized = "\n".join(
                    str(call[1])
                    for call in renderer.calls
                    if call[0] == "finalize"
                )
                self.assertIn(expected, finalized)
                self.assertIn(OVERSIZED_DIAGNOSTIC_MARKER, finalized)
                self.assertNotIn("DISCARDED", finalized)

    def test_finalized_line_replaces_the_provisional_prefix_without_duplication(self):
        for display in NetworkUrlDisplay:
            with self.subTest(display=display):
                renderer = _RecordingPresentationRenderer()
                session = self._session_for(display, renderer)
                captures: list = []
                chunks = iter((self._PREFIX_LINE.encode() + b"\n", b""))

                def stdout_read(_size):
                    return next(chunks)

                try:
                    self._materialize(
                        session.sink,
                        stdout_read,
                        captures,
                        network_url_display=display,
                    )
                finally:
                    session.shutdown()

                finalizes = [
                    call[1]
                    for call in renderer.calls
                    if call[0] == "finalize"
                ]
                self.assertEqual(1, len(finalizes))
                # The finalized complete line is written once and the visible
                # provisional prefix is replaced, never appended twice.
                self.assertEqual(
                    str(finalizes[0]).count("npm error network GET"), 1
                )

    def test_external_callback_receives_only_the_finalized_diagnostic(self):
        delivered: list = []
        chunks = iter((b"npm warn partial\n", b""))

        def stdout_read(_size):
            return next(chunks)

        self._materialize(delivered.append, stdout_read, [])

        structured = [
            event
            for event in delivered
            if isinstance(event, HostStructuredDiagnostic)
        ]
        self.assertEqual(1, len(structured))
        self.assertEqual("npm warn partial", structured[0].text)
        # A committed prefix must never reach an external SDK callback.
        self.assertFalse(
            any(
                isinstance(event, HostDiagnosticPrefix)
                for event in delivered
            )
        )

    def test_external_overflow_is_one_bounded_safe_truncation_after_the_boundary(self):
        delivered: list = []
        limit = DIAGNOSTIC_LINE_LIMIT_BYTES
        oversized = (
            b"npm warn leading "
            + b"pad " * (limit // 4)
            + b"DISCARDED" * 100
        )
        state = {"stage": 0}
        before: list = []

        def stdout_read(_size):
            if state["stage"] == 0:
                state["stage"] = 1
                return oversized
            if state["stage"] == 1:
                # The committed prefix and marker were emitted provisionally;
                # no external event may arrive before the record boundary.
                before.extend(delivered)
                state["stage"] = 2
                return b"\n"
            if state["stage"] == 2:
                state["stage"] = 3
                return b"npm warn recovered\n"
            return b""

        self._materialize(delivered.append, stdout_read, [])

        # No provisional prefix and no provisional line reached the SDK before
        # the oversized line was finalized.
        self.assertEqual(
            [],
            [
                event
                for event in before
                if isinstance(event, HostStructuredDiagnostic)
            ],
        )
        self.assertFalse(
            any(
                isinstance(event, HostDiagnosticPrefix)
                for event in delivered
            )
        )
        structured = [
            event
            for event in delivered
            if isinstance(event, HostStructuredDiagnostic)
        ]
        overflow = [
            event
            for event in structured
            if OVERSIZED_DIAGNOSTIC_MARKER in event.text
        ]
        self.assertEqual(1, len(overflow))
        event = overflow[0]
        self.assertIn("npm warn leading", event.text)
        self.assertTrue(event.text.endswith(OVERSIZED_DIAGNOSTIC_MARKER))
        self.assertEqual(1, event.text.count(OVERSIZED_DIAGNOSTIC_MARKER))
        self.assertNotIn("DISCARDED", event.text)
        # No discarded-suffix metadata and no incomplete-line inference: the
        # truncation is delivered as a neutral bounded status diagnostic.
        self.assertEqual((), event.hostnames)
        self.assertEqual((), event.url_fingerprints)
        self.assertEqual(
            HostDiagnosticClassification.STATUS, event.classification
        )
        # A complete line still produces exactly one finalized diagnostic.
        recovered = [
            item for item in structured if "recovered" in item.text
        ]
        self.assertEqual(1, len(recovered))

    def test_external_overflow_finalizes_once_at_eof_without_a_second_marker(self):
        delivered: list = []
        limit = DIAGNOSTIC_LINE_LIMIT_BYTES
        oversized = b"npm warn " + b"pad " * (limit // 4) + b"DISCARDED" * 100
        chunks = iter((oversized, b""))

        def stdout_read(_size):
            return next(chunks)

        self._materialize(delivered.append, stdout_read, [])

        structured = [
            event
            for event in delivered
            if isinstance(event, HostStructuredDiagnostic)
        ]
        self.assertEqual(1, len(structured))
        self.assertEqual(
            1, structured[0].text.count(OVERSIZED_DIAGNOSTIC_MARKER)
        )
        self.assertNotIn("DISCARDED", structured[0].text)

    def test_overflow_marker_is_visible_in_lines_mode_without_the_suffix(self):
        renderer = _RecordingPresentationRenderer()
        session = HostPresentationSession(
            renderer,
            PresentationPlan(
                HostPresentationMode.LINES,
                PresentationSelection.LIVE,
            ),
        )
        captures: list = []
        limit = DIAGNOSTIC_LINE_LIMIT_BYTES
        oversized = b"pad " * (limit // 4) + b"DISCARDED" * 100
        sent = {"oversized": False, "newline": False}
        snapshot: dict = {}

        def stdout_read(_size):
            if not sent["oversized"]:
                sent["oversized"] = True
                return oversized
            if not sent["newline"]:
                self.assertTrue(renderer.marker_durable.wait(5.0))
                snapshot["calls"] = list(renderer.calls)
                sent["newline"] = True
                return b"\nrecovered line\n"
            return b""

        try:
            self._materialize(session.sink, stdout_read, captures)
        finally:
            session.shutdown()

        before = _durable_text(snapshot["calls"])
        self.assertIn(OVERSIZED_DIAGNOSTIC_MARKER, before)
        self.assertNotIn("DISCARDED", before)
        joined = _durable_text(renderer.calls)
        self.assertEqual(joined.count(OVERSIZED_DIAGNOSTIC_MARKER), 1)
        self.assertNotIn("DISCARDED", joined)
        self.assertIn("recovered line", joined)
        tail = captures[0].stdout_tail
        self.assertEqual(tail.count(OVERSIZED_DIAGNOSTIC_MARKER), 1)
        self.assertNotIn("DISCARDED", tail)

    def test_internal_overflow_provisional_finalization_is_not_duplicated(self):
        renderer = _RecordingPresentationRenderer()
        session = HostPresentationSession(
            renderer,
            PresentationPlan(
                HostPresentationMode.LINES,
                PresentationSelection.LIVE,
            ),
        )
        captures: list = []
        limit = DIAGNOSTIC_LINE_LIMIT_BYTES
        oversized = b"npm warn " + b"pad " * (limit // 4) + b"DISCARDED" * 100 + b"\n"
        chunks = iter((oversized, b""))

        def stdout_read(_size):
            return next(chunks)

        try:
            self._materialize(session.sink, stdout_read, captures)
        finally:
            session.shutdown()

        durable = _durable_text(renderer.calls)
        self.assertEqual(durable.count(OVERSIZED_DIAGNOSTIC_MARKER), 1)
        self.assertEqual(durable.count("npm warn"), 1)
        self.assertNotIn("DISCARDED", durable)
        # The internal actor received the provisional finalization only; it
        # never also renders an ordinary structured duplicate.
        self.assertFalse(
            any(
                call[0] == "finalize"
                and OVERSIZED_DIAGNOSTIC_MARKER in str(call[1])
                for call in renderer.calls
            )
        )
        self.assertEqual(
            captures[0].stdout_tail.count(OVERSIZED_DIAGNOSTIC_MARKER), 1
        )

    def test_internal_overflow_interactive_finalizes_once(self):
        renderer = _RecordingPresentationRenderer()
        session = HostPresentationSession(
            renderer,
            PresentationPlan(
                HostPresentationMode.INTERACTIVE,
                PresentationSelection.LIVE,
            ),
        )
        captures: list = []
        limit = DIAGNOSTIC_LINE_LIMIT_BYTES
        oversized = b"npm warn " + b"pad " * (limit // 4) + b"DISCARDED" * 100 + b"\n"
        chunks = iter((oversized, b""))

        def stdout_read(_size):
            return next(chunks)

        try:
            self._materialize(session.sink, stdout_read, captures)
        finally:
            session.shutdown()

        finalized = [
            call[1]
            for call in renderer.calls
            if call[0] == "finalize"
        ]
        self.assertEqual(1, len(finalized))
        self.assertIn("npm warn", finalized[0])
        self.assertTrue(finalized[0].endswith(OVERSIZED_DIAGNOSTIC_MARKER))
        self.assertNotIn("DISCARDED", finalized[0])
        self.assertEqual(
            captures[0].stdout_tail.count(OVERSIZED_DIAGNOSTIC_MARKER), 1
        )

    def test_combined_sinks_fan_out_overflow_without_provisional_sdk_prefix(self):
        renderer = _RecordingPresentationRenderer()
        session = HostPresentationSession(
            renderer,
            PresentationPlan(
                HostPresentationMode.LINES,
                PresentationSelection.LIVE,
            ),
        )
        sdk_events: list = []
        captures: list = []
        limit = DIAGNOSTIC_LINE_LIMIT_BYTES
        oversized = b"npm warn " + b"pad " * (limit // 4) + b"DISCARDED" * 100
        state = {"stage": 0}
        before: list = []

        def stdout_read(_size):
            if state["stage"] == 0:
                state["stage"] = 1
                return oversized
            if state["stage"] == 1:
                # The committed prefix and marker are already visible to the
                # internal actor provisionally; no external structured event
                # may arrive before the record boundary.
                before.extend(sdk_events)
                state["stage"] = 2
                return b"\n"
            return b""

        try:
            self._materialize(
                session.sink,
                stdout_read,
                captures,
                sdk_event_sink=sdk_events.append,
            )
        finally:
            session.shutdown()

        self.assertEqual(
            [],
            [
                event
                for event in before
                if isinstance(event, HostStructuredDiagnostic)
            ],
        )
        self.assertFalse(
            any(isinstance(event, HostDiagnosticPrefix) for event in sdk_events)
        )
        structured = [
            event
            for event in sdk_events
            if isinstance(event, HostStructuredDiagnostic)
        ]
        overflow = [
            event
            for event in structured
            if OVERSIZED_DIAGNOSTIC_MARKER in event.text
        ]
        self.assertEqual(1, len(overflow))
        self.assertNotIn("DISCARDED", overflow[0].text)
        self.assertEqual((), overflow[0].hostnames)
        self.assertEqual((), overflow[0].url_fingerprints)
        self.assertEqual(
            HostDiagnosticClassification.STATUS, overflow[0].classification
        )
        # The internal actor still finalized the overflow boundary exactly
        # once; the SDK fan-out never duplicated the provisional prefix.
        durable = _durable_text(renderer.calls)
        self.assertEqual(durable.count(OVERSIZED_DIAGNOSTIC_MARKER), 1)


class TestInstallPackageDependencies(unittest.TestCase):
    """An absent ``dependencies`` field differs from a present falsey value."""

    _NAME = "@earendil-works/pi-coding-agent-install"
    _VERSION = "0.84.4"

    def _parse(self, package_body: dict, *, root_dependencies=()):
        """Parse a manifest against a lockfile root with the given deps."""
        package = json.dumps(package_body, sort_keys=True).encode()
        lockfile = SimpleNamespace(
            root=SimpleNamespace(
                name=self._NAME,
                version=self._VERSION,
                dependencies=root_dependencies,
            ),
        )
        return _parse_install_package(package, lockfile)

    def _manifest(self, **overrides) -> dict:
        body = {"name": self._NAME, "version": self._VERSION}
        body.update(overrides)
        return body

    def test_absent_dependencies_treated_as_empty(self):
        # The lockfile root has no dependencies and the field is absent: an
        # absent field is normalized to {} and accepted.
        parsed = self._parse(self._manifest())
        self.assertNotIn("dependencies", parsed)

    def test_explicit_empty_dependencies_accepted(self):
        # An explicit {} is valid when the lockfile root has no dependencies.
        parsed = self._parse(self._manifest(dependencies={}))
        self.assertEqual(parsed["dependencies"], {})

    def test_present_falsey_dependencies_rejected(self):
        # Against a dependency-free lockfile root, a present falsey value must
        # be rejected as malformed rather than silently treated as absent.
        for bad in (None, [], "", False, 0):
            with self.subTest(dependencies=bad):
                with self.assertRaises(LockedNpmError) as ctx:
                    self._parse(self._manifest(dependencies=bad))
                self.assertEqual(
                    ctx.exception.reason, "invalid_package_dependencies",
                )

    def test_non_empty_dependencies_rejected_against_empty_lock_root(self):
        with self.assertRaises(LockedNpmError) as ctx:
            self._parse(self._manifest(dependencies={PI_PACKAGE: "0.84.4"}))
        self.assertEqual(
            ctx.exception.reason, "package_lock_root_disagreement",
        )

    def test_non_empty_dependencies_accepted_when_exactly_matching(self):
        root_dependencies = ((PI_PACKAGE, "0.84.4"),)
        parsed = self._parse(
            self._manifest(dependencies={PI_PACKAGE: "0.84.4"}),
            root_dependencies=root_dependencies,
        )
        self.assertEqual(parsed["dependencies"], {PI_PACKAGE: "0.84.4"})


if __name__ == "__main__":
    unittest.main()
