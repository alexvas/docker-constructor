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
from docker.versioning.host_presentation import (
    HostPresentationMode,
    HostPresentationSession,
    PresentationPlan,
    PresentationSelection,
)
from docker.versioning.host_progress import (
    GuardedHostEventSink, HostDiagnosticClassification, HostDiagnosticStream,
    HostPhase, HostPhaseEvent, HostPhaseState, HostStep,
    HostStructuredDiagnostic, InternalDirectHostEventSink,
    lookup_host_failure,
)
from docker.npm_environment.streaming import StreamChunk
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
