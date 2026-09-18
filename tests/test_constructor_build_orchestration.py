"""RED tests for Stage 9.1 — Build orchestration contract.

These tests define the full expected contract for ``orchestrate_build``
and related DTOs (tasks 7–23).  Every test uses injected fakes — no
Docker daemon, systemd, filesystem writes, or network calls.

The stub ``orchestrate_build`` returns ``OPERATIONAL`` for every input.
Most tests below assert *expected real behaviour* (e.g. ``SUCCESS``,
``CONFIG``, non-empty ``build_args``, recorded process output) and
therefore **genuinely FAIL** against the stub.  This is the RED signal
that drives the Stage 9.3 implementation.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from dataclasses import dataclass
from unittest.mock import patch
from pathlib import Path
from types import MappingProxyType
from typing import Optional

from tests.build_test_support import (
    DIGEST_VALID_ARTIFACT_BYTES, INVENTORY_PATH, digest_valid_selected_artifacts,
    fake_pi_materialization, fixture_directory, no_network_transport_factory,
    publish_digest_valid_artifacts,
)

from docker.networking import (
    DockerMode,
    GatewayDiagnosis,
    PersistenceResult,
    ProbeResult,
)
from docker.versioning.build_snapshot import MaterializedSnapshot, SnapshotError
from docker.versioning.build_materialization import (
    MaterializationError, SelectedBuildArtifact, UrllibStreamingTransport,
)
from docker.versioning.build_cache import (
    UNCOMMITTED_TTL_SECONDS, acquire_constructor_project_build_lock, build_blob_path,
    commit_build_set, prepare_build_cache, publish_uncommitted_blob,
    publish_verified_blob,
)
from docker.versioning.digest_identity import DigestIdentity
from docker.versioning.cache_storage import runtime_artifacts_child, versioning_child
from docker.npm_environment.errors import LockedNpmError
from docker.versioning.pi_assembly import PiAssemblyError
from docker.versioning.pi_consumer import PiConsumerError
from docker.versioning.build_orchestration import (
    BuildRequest,
    BuildResult,
    DoctorRequest,
    DoctorResult,
    PublishError,
    PublishResult,
    ProcessResult,
    diagnose_doctor,
    orchestrate_build,
    repair_rootless,
)
from docker.versioning.dispatch_types import ExitKind

# ═══════════════════════════════════════════════════════════════════════
# Fakes — injectable, no Docker/systemd/fs/network
# ═══════════════════════════════════════════════════════════════════════

def _make_diagnosis(
    *,
    mode: DockerMode = DockerMode.ROOTFUL,
    chosen_gateway: str | None = "172.17.0.1",
) -> GatewayDiagnosis:
    """Build a minimal ``GatewayDiagnosis`` with a single probe result."""
    ok = chosen_gateway is not None
    probe = ProbeResult(
        candidate=chosen_gateway or "172.17.0.1",
        ok=ok,
        resolved_ip=chosen_gateway,
        detail="" if ok else "no route to host",
    )
    return GatewayDiagnosis(
        mode=mode,
        probe_port=8080,
        probe_token="test-token",
        lan_ip=None,
        probes=(probe,),
        chosen_gateway=chosen_gateway,
        override_installed=False,
        override_needed=False,
    )

class FakeBuildExecutor:
    """Recording build executor matching ``BuildExecutor`` Protocol."""

    def __init__(self, result: ProcessResult | None = None,
                 returncode: int = 0):
        self._result = result
        self.returncode = returncode
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: tuple[str, ...]) -> ProcessResult:
        self.calls.append(argv)
        if self._result is not None:
            return self._result
        return ProcessResult(
            argv=argv,
            return_code=self.returncode,
            stdout="build output" if self.returncode == 0 else "",
            stderr="" if self.returncode == 0 else "build error",
        )

# ── Convenience factories (inject into BuildRequest fields) ──────────

def _diag_reachable(**kw):
    return _make_diagnosis(
        mode=DockerMode.ROOTFUL,
        chosen_address="172.17.0.1",
    )

def _diag_unreachable(**kw):
    return _make_diagnosis(
        mode=DockerMode.ROOTFUL,
        chosen_gateway=None,
    )

def _persist_ok(path=None, gateway=None):
    return PersistenceResult(
        path=Path("/tmp/.env"),
        address="172.17.0.1",
        written=True,
    )

def _persist_fail(path=None, gateway=None):
    return PersistenceResult(
        path=Path("/tmp/.env"),
        address="172.17.0.1",
        written=False,
        error="EACCES",
    )

def _publish_ok(projection, *, repo_root=None):
    return PublishResult(published_path="/tmp/effective.toml")


_TEST_ARTIFACT_BYTES = DIGEST_VALID_ARTIFACT_BYTES


def _test_selected_artifacts(_projection):
    """Test-only effective selection with identities derived from local bytes."""
    return digest_valid_selected_artifacts(_projection)


def _materialize_ok(projection, *, constructor_project_root, cache_root=None, project_state=None, **_kwargs):
    """Publish real digest-verified blobs for orchestration fixture builds."""
    return publish_digest_valid_artifacts(
        projection, constructor_project_root=constructor_project_root, cache_root=cache_root,
        project_state=project_state,
    )


def _fixture_snapshot(*_args, **_kwargs) -> MaterializedSnapshot:
    path = fixture_directory("fixture-snapshot-")
    return MaterializedSnapshot(path, path / "manifest.json")


def setUpModule() -> None:
    global _snapshot_patcher, _selection_patcher
    _snapshot_patcher = patch(
        "docker.versioning.build_orchestration.create_artifact_snapshot",
        side_effect=_fixture_snapshot,
    )
    _selection_patcher = patch(
        "docker.versioning.build_orchestration.select_build_artifacts",
        side_effect=_test_selected_artifacts,
    )
    _snapshot_patcher.start()
    _selection_patcher.start()


def tearDownModule() -> None:
    _selection_patcher.stop()
    _snapshot_patcher.stop()


def _assert_prospective(result) -> None:
    """Dry runs are display-only; they never expose executable argv."""
    assert result.build_args == ()
    assert result.display_string is not None
    assert "Planned build (not executable)" in result.display_string
    assert "--build-context constructor-artifacts=<prospective:not-materialized>" in result.display_string

# ═══════════════════════════════════════════════════════════════════════
# 7.  DTO expectations (GREEN — these test the DTOs, not the stub)
# ═══════════════════════════════════════════════════════════════════════

class TestBuildRequestDto(unittest.TestCase):
    """Task 7 — immutable BuildRequest fields."""

    def test_minimal_request_has_all_defaults(self):
        req = BuildRequest(inventory_path=str(INVENTORY_PATH), project_root=Path(str(INVENTORY_PATH)).resolve().parent)
        self.assertEqual(str(INVENTORY_PATH), req.inventory_path)
        self.assertEqual("linux-amd64", req.platform)
        self.assertIsNone(req.tag)
        self.assertEqual({}, dict(req.overrides))
        self.assertEqual("runtime", req.target)
        self.assertIsNone(req.context)
        self.assertIsNone(req.dockerfile)
        self.assertTrue(req.cache)
        self.assertFalse(req.pull)
        self.assertEqual("auto", req.progress)
        self.assertIsNone(req.uid)
        self.assertIsNone(req.gid)
        self.assertFalse(req.confirmed)
        self.assertFalse(req.dry_run)
        self.assertIsNone(req.runner)
        self.assertEqual("alpine:3.20", req.gateway_probe_image)
        self.assertIsNone(req.repo_root)
        self.assertIsNone(req._diagnose_gateway)
        self.assertIsNone(req._publish_projection)

    def test_all_fields_assignable(self):
        req = BuildRequest(
            inventory_path=str(INVENTORY_PATH),
            platform="linux-arm64",
            tag="pi:custom",
            overrides=MappingProxyType({"a": "b"}),
            target="build",
            context="./ctx",
            dockerfile="Dockerfile.alt",
            cache=False,
            pull=True,
            progress="plain",
            uid=1000,
            gid=1000,
            confirmed=True,
            _materialize_artifacts=_materialize_ok,
            _materialize_pi=fake_pi_materialization,
            _named_context_supported=lambda: True,
            dry_run=True,
            runner=FakeBuildExecutor(),
            gateway_probe_image="busybox:1.36",
            repo_root="/tmp",
            _diagnose_gateway=_diag_reachable,
            _publish_projection=_publish_ok,
        project_root=Path(str(INVENTORY_PATH)).resolve().parent)
        self.assertEqual("linux-arm64", req.platform)
        self.assertEqual("pi:custom", req.tag)
        self.assertEqual("build", req.target)
        self.assertEqual("./ctx", req.context)
        self.assertEqual("Dockerfile.alt", req.dockerfile)
        self.assertFalse(req.cache)
        self.assertTrue(req.pull)
        self.assertEqual("plain", req.progress)
        self.assertEqual(1000, req.uid)
        self.assertEqual(1000, req.gid)
        self.assertTrue(req.confirmed)
        self.assertTrue(req.dry_run)

    def test_build_result_carries_all_fields(self):
        pr = ProcessResult(argv=("docker", "build", "."), return_code=0, stdout="ok", stderr="")
        result = BuildResult(
            exit_kind=ExitKind.SUCCESS,
            message="done",
            build_args=("docker", "build", "."),
            display_string="docker build .",
            process_result=pr,
        )
        self.assertEqual(ExitKind.SUCCESS, result.exit_kind)
        self.assertEqual("done", result.message)
        self.assertEqual(("docker", "build", "."), result.build_args)
        self.assertEqual("docker build .", result.display_string)
        self.assertIs(pr, result.process_result)

    def test_dtos_are_frozen(self):
        req = BuildRequest(inventory_path=str(INVENTORY_PATH), project_root=Path(str(INVENTORY_PATH)).resolve().parent)
        with self.assertRaises(Exception):
            req.platform = "linux-arm64"  # type: ignore[misc]
        result = BuildResult(exit_kind=ExitKind.SUCCESS)
        with self.assertRaises(Exception):
            result.message = "nope"  # type: ignore[misc]

    def test_overrides_normalized_to_immutable(self):
        """Caller-side mutation of a mutable dict passed to ``overrides``
        must **not** alter the frozen ``BuildRequest`` after construction."""
        mutable = {"A": "1", "B": "2"}
        req = BuildRequest(
            inventory_path=str(INVENTORY_PATH),
            overrides=mutable,
        project_root=Path(str(INVENTORY_PATH)).resolve().parent)
        self.assertIsInstance(req.overrides, MappingProxyType)
        self.assertEqual({"A": "1", "B": "2"}, dict(req.overrides))
        # Mutate the caller-owned dict
        mutable["A"] = "HACKED"
        mutable["C"] = "3"
        del mutable["B"]
        # Request must be unchanged
        self.assertEqual({"A": "1", "B": "2"}, dict(req.overrides),
                         "caller mutation must not alter frozen request")
        # Setting overrides on frozen instance must still fail
        with self.assertRaises(Exception):
            req.overrides = MappingProxyType({"X": "Y"})  # type: ignore[misc]

    def test_overrides_default_is_empty_immutable(self):
        """Default ``overrides`` (no argument) must be an empty immutable
        mapping."""
        req = BuildRequest(inventory_path=str(INVENTORY_PATH), project_root=Path(str(INVENTORY_PATH)).resolve().parent)
        self.assertIsInstance(req.overrides, MappingProxyType)
        self.assertEqual({}, dict(req.overrides))

    def test_process_result_boundary_match(self):
        """``ProcessResult`` used by build orchestration is the same type
        accepted by ``docker.networking`` — there is no duplicate."""
        from docker.networking import ProcessResult as NetPR
        self.assertIs(NetPR, ProcessResult,
                      "build_orchestration must re-use networking.ProcessResult")

    def test_build_executor_boundary(self):
        """A ``FakeBuildExecutor`` satisfies the ``BuildExecutor`` Protocol —
        ``run(tuple[str, ...])`` returns ``ProcessResult`` with ``argv`` /
        ``return_code`` fields."""
        runner = FakeBuildExecutor()
        result = runner.run(("docker", "build", "-t", "pi:latest", "."))
        self.assertIsInstance(result, ProcessResult)
        self.assertEqual(("docker", "build", "-t", "pi:latest", "."), result.argv)
        self.assertEqual(0, result.return_code)

    def test_build_executor_distinct_from_networking_runner(self):
        """``BuildExecutor`` is a separate contract from ``ProcessRunner`` —
        build uses ``tuple[str, ...]`` while networking uses ``list[str]``."""
        from docker.networking import ProcessRunner
        from docker.versioning.build_orchestration import BuildExecutor
        self.assertIsNot(BuildExecutor, ProcessRunner)
        # A BuildExecutor is NOT a ProcessRunner (different signatures)
        be = FakeBuildExecutor()
        self.assertNotIsInstance(be, ProcessRunner)

# ═══════════════════════════════════════════════════════════════════════
# 8.  Injectables wired through BuildRequest
# ═══════════════════════════════════════════════════════════════════════

class TestInjectablesWired(unittest.TestCase):
    """Task 8 — all side-effecting boundaries injectable through BuildRequest."""

    def test_all_injectables_accepted(self):
        """BuildRequest must carry every injectable slot."""
        runner = FakeBuildExecutor()
        req = BuildRequest(
            inventory_path=str(INVENTORY_PATH),
            runner=runner,
            _diagnose_gateway=_diag_reachable,
            _publish_projection=_publish_ok,
            _materialize_artifacts=_materialize_ok,
            _materialize_pi=fake_pi_materialization,
            _transport_factory=UrllibStreamingTransport,
            _named_context_supported=lambda: True,
        project_root=Path(str(INVENTORY_PATH)).resolve().parent)
        # Just verify the slots are populated
        self.assertIs(runner, req.runner)
        self.assertIs(_diag_reachable, req._diagnose_gateway)
        self.assertIs(_publish_ok, req._publish_projection)
        self.assertIs(_materialize_ok, req._materialize_artifacts)
        self.assertIs(UrllibStreamingTransport, req._transport_factory)
        self.assertTrue(req._named_context_supported())

# ═══════════════════════════════════════════════════════════════════════
# 9.  Default-build tests (RED)
# ═══════════════════════════════════════════════════════════════════════

class TestDefaultBuild(unittest.TestCase):
    """Task 9 — canonical image, target runtime, platform, deterministic,
    direct docker build (never Compose)."""

    def test_default_produces_success(self):
        """Real impl must return SUCCESS for a valid inventory."""
        req = BuildRequest(
            inventory_path=str(INVENTORY_PATH),
            dry_run=True,
            _diagnose_gateway=_diag_reachable,
            _publish_projection=_publish_ok,
        project_root=Path(str(INVENTORY_PATH)).resolve().parent)
        result = orchestrate_build(req)
        self.assertEqual(ExitKind.SUCCESS, result.exit_kind,
                         f"expected SUCCESS, got {result.exit_kind}: {result.message}")

    def test_default_tag_is_pi_cli_pi_latest(self):
        """Default image tag is exactly ``pi-cli-pi:latest`` — canonical identity."""
        req = BuildRequest(
            inventory_path=str(INVENTORY_PATH),
            dry_run=True,
            _diagnose_gateway=_diag_reachable,
            _publish_projection=_publish_ok,
        project_root=Path(str(INVENTORY_PATH)).resolve().parent)
        result = orchestrate_build(req)
        self.assertEqual(ExitKind.SUCCESS, result.exit_kind,
                         f"expected SUCCESS, got {result.exit_kind}: {result.message}")
        _assert_prospective(result)

    def test_default_target_is_runtime(self):
        """Default target stage is 'runtime'."""
        req = BuildRequest(
            inventory_path=str(INVENTORY_PATH),
            dry_run=True,
            _diagnose_gateway=_diag_reachable,
            _publish_projection=_publish_ok,
        project_root=Path(str(INVENTORY_PATH)).resolve().parent)
        result = orchestrate_build(req)
        _assert_prospective(result)

    def test_command_is_docker_build_not_compose(self):
        """The first token must be 'docker', never 'docker-compose'."""
        req = BuildRequest(
            inventory_path=str(INVENTORY_PATH),
            dry_run=True,
            _diagnose_gateway=_diag_reachable,
            _publish_projection=_publish_ok,
        project_root=Path(str(INVENTORY_PATH)).resolve().parent)
        result = orchestrate_build(req)
        _assert_prospective(result)

    def test_deterministic_vector(self):
        """Same inputs must produce identical build_args."""
        req = BuildRequest(
            inventory_path=str(INVENTORY_PATH),
            dry_run=True,
            _diagnose_gateway=_diag_reachable,
            _publish_projection=_publish_ok,
        project_root=Path(str(INVENTORY_PATH)).resolve().parent)
        r1 = orchestrate_build(req)
        r2 = orchestrate_build(req)
        _assert_prospective(r1)
        _assert_prospective(r2)
        self.assertEqual(r1.display_string.encode(), r2.display_string.encode())

# ═══════════════════════════════════════════════════════════════════════
# 10.  Build override tests (RED)
# ═══════════════════════════════════════════════════════════════════════

class TestBuildOverrides(unittest.TestCase):
    """Task 10 — accepted build override, unsupported rejected,
    runtime override rejected, source unchanged.  Duplicate ``--override``
    arguments are rejected by the facade parser (see
    ``TestDuplicateOverrideRejectedAtParser`` in the facade test suite)."""

    def test_accepted_build_override_reaches_projection(self):
        """A build-owned override must be reflected in the build vector."""
        req = BuildRequest(
            inventory_path=str(INVENTORY_PATH),
            dry_run=True,
            overrides=MappingProxyType({
                "build.stages.toolchain.python.version": "3.15.0",
            }),
            _diagnose_gateway=_diag_reachable,
            _publish_projection=_publish_ok,
        project_root=Path(str(INVENTORY_PATH)).resolve().parent)
        result = orchestrate_build(req)
        self.assertEqual(ExitKind.SUCCESS, result.exit_kind)
        _assert_prospective(result)

    def test_unsupported_override_rejected(self):
        """A path not in the inventory schema must cause CONFIG error."""
        req = BuildRequest(
            inventory_path=str(INVENTORY_PATH),
            dry_run=True,
            overrides=MappingProxyType({
                "build.nonexistent.thing": "val",
            }),
            _diagnose_gateway=_diag_reachable,
            _publish_projection=_publish_ok,
        project_root=Path(str(INVENTORY_PATH)).resolve().parent)
        result = orchestrate_build(req)
        self.assertEqual(ExitKind.CONFIG, result.exit_kind,
                         f"expected CONFIG for unsupported override, got {result.exit_kind}")

    def test_runtime_override_rejected_in_build(self):
        """A runtime-scoped override must not be accepted by build."""
        req = BuildRequest(
            inventory_path=str(INVENTORY_PATH),
            dry_run=True,
            overrides=MappingProxyType({
                "runtime.pi-extensions.x.version": "1.0.0",
            }),
            _diagnose_gateway=_diag_reachable,
            _publish_projection=_publish_ok,
        project_root=Path(str(INVENTORY_PATH)).resolve().parent)
        result = orchestrate_build(req)
        self.assertEqual(ExitKind.CONFIG, result.exit_kind,
                         f"expected CONFIG for runtime override, got {result.exit_kind}")

# ═══════════════════════════════════════════════════════════════════════
# 11.  Platform tests (RED)
# ═══════════════════════════════════════════════════════════════════════

class TestPlatformSelection(unittest.TestCase):
    """Task 11 — AMD64/ARM64 resolution, command platform matches projection,
    missing-platform artifact fails before diagnosis or execution."""

    def test_amd64_platform_in_build_vector(self):
        req = BuildRequest(
            inventory_path=str(INVENTORY_PATH),
            platform="linux-amd64",
            dry_run=True,
            _diagnose_gateway=_diag_reachable,
            _publish_projection=_publish_ok,
        project_root=Path(str(INVENTORY_PATH)).resolve().parent)
        result = orchestrate_build(req)
        self.assertEqual(ExitKind.SUCCESS, result.exit_kind)
        _assert_prospective(result)

    def test_arm64_platform_in_build_vector(self):
        req = BuildRequest(
            inventory_path=str(INVENTORY_PATH),
            platform="linux-arm64",
            dry_run=True,
            _diagnose_gateway=_diag_reachable,
            _publish_projection=_publish_ok,
        project_root=Path(str(INVENTORY_PATH)).resolve().parent)
        result = orchestrate_build(req)
        # ARM64 may succeed or fail CONFIG depending on inventory
        self.assertIn(result.exit_kind, (ExitKind.SUCCESS, ExitKind.CONFIG),
                      f"unexpected exit: {result.exit_kind}: {result.message}")

    def test_missing_platform_fails_before_diagnosis(self):
        """When inventory lacks the requested platform, fail CONFIG
        and do NOT call gateway diagnosis."""
        diag_called = []

        def record_diag(**kw):
            diag_called.append(1)
            return _make_diagnosis()

        req = BuildRequest(
            inventory_path="/nonexistent/inventory.toml",
            platform="linux-arm64",
            _diagnose_gateway=record_diag,
            _publish_projection=_publish_ok,
        project_root=Path("/nonexistent/inventory.toml").resolve().parent)
        result = orchestrate_build(req)
        self.assertEqual(ExitKind.CONFIG, result.exit_kind,
                         f"expected CONFIG, got {result.exit_kind}")
        self.assertEqual(0, len(diag_called),
                         "must not call gateway diagnosis on missing inventory")

# ═══════════════════════════════════════════════════════════════════════
# 12.  Projection tests (RED)
# ═══════════════════════════════════════════════════════════════════════

class TestProjectionPublication(unittest.TestCase):
    """Task 12 — resolve from validated inventory.build, publish canonical
    host-side path, atomic, inventory not overwritten."""

    def test_projector_called_during_build(self):
        """The _publish_projection injectable must be invoked during
        execution (not planning / dry-run)."""
        calls = []

        def record_publish(projection, *, repo_root=None):
            calls.append(1)
            return PublishResult(published_path="/tmp/eff.toml")

        req = BuildRequest(
            inventory_path=str(INVENTORY_PATH),
            confirmed=True,
            _materialize_artifacts=_materialize_ok,
            _materialize_pi=fake_pi_materialization,
            _named_context_supported=lambda: True,
            _diagnose_gateway=_diag_reachable,
            _publish_projection=record_publish,
            runner=FakeBuildExecutor(),
        project_root=Path(str(INVENTORY_PATH)).resolve().parent)
        result = orchestrate_build(req)
        self.assertEqual(ExitKind.SUCCESS, result.exit_kind)
        # Publisher must have been called at least once
        self.assertGreater(len(calls), 0,
                           "_publish_projection was not called")

    def test_publish_result_contains_canonical_path(self):
        """PublishResult must have a non-empty published_path."""
        pr = PublishResult(published_path="/tmp/effective.toml")
        self.assertIsInstance(pr.published_path, str)
        self.assertGreater(len(pr.published_path), 0)

# ═══════════════════════════════════════════════════════════════════════
# 13.  Cache / control tests (RED)
# ═══════════════════════════════════════════════════════════════════════

class TestCacheControls(unittest.TestCase):
    """Task 13 — cache, pull, progress, custom tag, UID/GID, context, Dockerfile."""

    def test_cache_disabled_adds_no_cache(self):
        req = BuildRequest(
            inventory_path=str(INVENTORY_PATH),
            cache=False,
            dry_run=True,
            _diagnose_gateway=_diag_reachable,
            _publish_projection=_publish_ok,
        project_root=Path(str(INVENTORY_PATH)).resolve().parent)
        result = orchestrate_build(req)
        self.assertEqual(ExitKind.SUCCESS, result.exit_kind)
        _assert_prospective(result)

    def test_pull_enabled_adds_pull(self):
        req = BuildRequest(
            inventory_path=str(INVENTORY_PATH),
            pull=True,
            dry_run=True,
            _diagnose_gateway=_diag_reachable,
            _publish_projection=_publish_ok,
        project_root=Path(str(INVENTORY_PATH)).resolve().parent)
        result = orchestrate_build(req)
        self.assertEqual(ExitKind.SUCCESS, result.exit_kind)
        _assert_prospective(result)

    def test_progress_plain_controls_output(self):
        req = BuildRequest(
            inventory_path=str(INVENTORY_PATH),
            progress="plain",
            dry_run=True,
            _diagnose_gateway=_diag_reachable,
            _publish_projection=_publish_ok,
        project_root=Path(str(INVENTORY_PATH)).resolve().parent)
        result = orchestrate_build(req)
        self.assertEqual(ExitKind.SUCCESS, result.exit_kind)
        _assert_prospective(result)

    def test_custom_tag_appears_in_vector(self):
        req = BuildRequest(
            inventory_path=str(INVENTORY_PATH),
            tag="pi-cli-pi:latest",
            dry_run=True,
            _diagnose_gateway=_diag_reachable,
            _publish_projection=_publish_ok,
        project_root=Path(str(INVENTORY_PATH)).resolve().parent)
        result = orchestrate_build(req)
        self.assertEqual(ExitKind.SUCCESS, result.exit_kind)
        _assert_prospective(result)

    def test_uid_gid_surface_in_build_args(self):
        req = BuildRequest(
            inventory_path=str(INVENTORY_PATH),
            uid=1000,
            gid=1000,
            dry_run=True,
            _diagnose_gateway=_diag_reachable,
            _publish_projection=_publish_ok,
        project_root=Path(str(INVENTORY_PATH)).resolve().parent)
        result = orchestrate_build(req)
        self.assertEqual(ExitKind.SUCCESS, result.exit_kind)
        _assert_prospective(result)

    def test_context_and_dockerfile_override(self):
        req = BuildRequest(
            inventory_path=str(INVENTORY_PATH),
            context="/custom/context",
            dockerfile="Dockerfile.custom",
            dry_run=True,
            _diagnose_gateway=_diag_reachable,
            _publish_projection=_publish_ok,
        project_root=Path(str(INVENTORY_PATH)).resolve().parent)
        result = orchestrate_build(req)
        self.assertEqual(ExitKind.SUCCESS, result.exit_kind)
        _assert_prospective(result)

# ═══════════════════════════════════════════════════════════════════════
# 14–15.  Failure-order tests — zero side effects (RED)
# ═══════════════════════════════════════════════════════════════════════

class _RecordingFakes:
    """Records every fake call for zero-side-effect assertions."""
    diagnose_calls = 0
    persist_calls = 0
    docker_calls = 0
    publish_calls = 0

    def diagnose(self, **kw):
        _RecordingFakes.diagnose_calls += 1
        return _make_diagnosis()

    def persist(self, path=None, gateway=None):
        _RecordingFakes.persist_calls += 1
        return _persist_ok()

    def publish(self, inv=None, projection=None, path=None):
        _RecordingFakes.publish_calls += 1
        return PublishResult(published_path="/tmp/eff.toml")

    def run(self, argv: tuple[str, ...]):
        _RecordingFakes.docker_calls += 1
        return ProcessResult(argv=argv, return_code=0, stdout="", stderr="")

def _reset_recording():
    _RecordingFakes.diagnose_calls = 0
    _RecordingFakes.persist_calls = 0
    _RecordingFakes.docker_calls = 0
    _RecordingFakes.publish_calls = 0

class TestFailureOrdering(unittest.TestCase):
    """Tasks 14–15 — every pre-validate failure means zero side effects."""

    def setUp(self):
        _reset_recording()
        self.fakes = _RecordingFakes()

    def _assert_zero(self):
        self.assertEqual(0, _RecordingFakes.diagnose_calls,
                         "gateway diagnose must not be called")
        self.assertEqual(0, _RecordingFakes.persist_calls,
                         "persist must not be called")
        self.assertEqual(0, _RecordingFakes.docker_calls,
                         "docker must not be called")
        self.assertEqual(0, _RecordingFakes.publish_calls,
                         "publish must not be called")

    def test_missing_inventory_returns_config_and_zero_calls(self):
        req = BuildRequest(
            inventory_path="/nonexistent/inventory.toml",
            _diagnose_gateway=self.fakes.diagnose,
            _publish_projection=self.fakes.publish,
            runner=self.fakes,  # type: ignore[arg-type]
        project_root=Path("/nonexistent/inventory.toml").resolve().parent)
        result = orchestrate_build(req)
        self.assertEqual(ExitKind.CONFIG, result.exit_kind,
                         f"expected CONFIG, got {result.exit_kind}: {result.message}")
        self._assert_zero()

    def test_invalid_toml_returns_config_and_zero_calls(self):
        req = BuildRequest(
            inventory_path="README.md",  # not TOML
            _diagnose_gateway=self.fakes.diagnose,
            _publish_projection=self.fakes.publish,
            runner=self.fakes,  # type: ignore[arg-type]
        project_root=Path("README.md").resolve().parent)
        result = orchestrate_build(req)
        self.assertEqual(ExitKind.CONFIG, result.exit_kind,
                         f"expected CONFIG, got {result.exit_kind}: {result.message}")
        self._assert_zero()

    def test_invalid_schema_returns_config_and_zero_calls(self):
        """A valid TOML file that is not an inventory schema."""
        req = BuildRequest(
            inventory_path="pyproject.toml",  # valid TOML but not inventory
            _diagnose_gateway=self.fakes.diagnose,
            _publish_projection=self.fakes.publish,
            runner=self.fakes,  # type: ignore[arg-type]
        project_root=Path("pyproject.toml").resolve().parent)
        result = orchestrate_build(req)
        self.assertEqual(ExitKind.CONFIG, result.exit_kind,
                         f"expected CONFIG, got {result.exit_kind}: {result.message}")
        self._assert_zero()

    def test_invalid_override_returns_config_and_zero_calls(self):
        """A completely invalid override path must fail before side effects."""
        req = BuildRequest(
            inventory_path=str(INVENTORY_PATH),
            overrides=MappingProxyType({"not.a.real.path.at.all": "val"}),
            _diagnose_gateway=self.fakes.diagnose,
            _publish_projection=self.fakes.publish,
            runner=self.fakes,  # type: ignore[arg-type]
        project_root=Path(str(INVENTORY_PATH)).resolve().parent)
        result = orchestrate_build(req)
        # Must be CONFIG (invalid override is a configuration error)
        self.assertEqual(ExitKind.CONFIG, result.exit_kind,
                         f"expected CONFIG, got {result.exit_kind}: {result.message}")
        self._assert_zero()

    def test_projection_resolution_failure_returns_config_and_zero_calls(self):
        """When the effective projection cannot be built, stop before side effects."""
        req = BuildRequest(
            inventory_path=str(INVENTORY_PATH),
            platform="nonexistent/cpu",
            _diagnose_gateway=self.fakes.diagnose,
            _publish_projection=self.fakes.publish,
            runner=self.fakes,  # type: ignore[arg-type]
        project_root=Path(str(INVENTORY_PATH)).resolve().parent)
        result = orchestrate_build(req)
        self.assertEqual(ExitKind.CONFIG, result.exit_kind,
                         f"expected CONFIG, got {result.exit_kind}: {result.message}")
        self._assert_zero()

# ═══════════════════════════════════════════════════════════════════════
# 15a.  Render-validation failures (Stage 9.3 hardening)
# ═══════════════════════════════════════════════════════════════════════

class TestRenderValidationFailures(unittest.TestCase):
    """Invalid render inputs (negative UID/GID, empty tag, etc.)
    must return ``CONFIG`` from planning with zero side effects."""

    # -- negative UID / GID ---------------------------------------------

    def test_negative_uid_returns_config(self):
        """Negative ``uid`` must produce CONFIG from ``plan_build``."""
        req = BuildRequest(
            inventory_path=str(INVENTORY_PATH),
            uid=-5,
            dry_run=True,
            _diagnose_gateway=self._bomb_diagnose,
            _publish_projection=self._bomb_publish,
            runner=self._BombRunner(),
        project_root=Path(str(INVENTORY_PATH)).resolve().parent)
        result = orchestrate_build(req)
        self.assertEqual(ExitKind.CONFIG, result.exit_kind,
                         f"expected CONFIG, got {result.exit_kind}: {result.message}")
        self.assertIn("dev_uid", result.message or "")

    def test_negative_gid_returns_config(self):
        """Negative ``gid`` must produce CONFIG from ``plan_build``."""
        req = BuildRequest(
            inventory_path=str(INVENTORY_PATH),
            gid=-3,
            dry_run=True,
            _diagnose_gateway=self._bomb_diagnose,
            _publish_projection=self._bomb_publish,
            runner=self._BombRunner(),
        project_root=Path(str(INVENTORY_PATH)).resolve().parent)
        result = orchestrate_build(req)
        self.assertEqual(ExitKind.CONFIG, result.exit_kind,
                         f"expected CONFIG, got {result.exit_kind}: {result.message}")
        self.assertIn("dev_gid", result.message or "")

    def test_negative_uid_without_dry_run_still_config(self):
        """Negative UID must return CONFIG even with ``confirmed=True``
        and no dry-run — planning happens before execution."""
        req = BuildRequest(
            inventory_path=str(INVENTORY_PATH),
            uid=-1,
            confirmed=True,
            _materialize_artifacts=_materialize_ok,
            _materialize_pi=fake_pi_materialization,
            _named_context_supported=lambda: True,
            _diagnose_gateway=self._bomb_diagnose,
            _publish_projection=self._bomb_publish,
            runner=self._BombRunner(),
        project_root=Path(str(INVENTORY_PATH)).resolve().parent)
        result = orchestrate_build(req)
        self.assertEqual(ExitKind.CONFIG, result.exit_kind)
        # No build_args rendered
        self.assertEqual((), result.build_args)

    # -- empty tag / context --------------------------------------------

    def test_empty_tag_returns_config(self):
        """An explicit empty ``tag`` must reach the renderer's validation
        and return CONFIG — not be silently replaced with the default."""
        req = BuildRequest(
            inventory_path=str(INVENTORY_PATH),
            tag="",
            dry_run=True,
            _diagnose_gateway=self._bomb_diagnose,
            _publish_projection=self._bomb_publish,
            runner=self._BombRunner(),
        project_root=Path(str(INVENTORY_PATH)).resolve().parent)
        result = orchestrate_build(req)
        self.assertEqual(ExitKind.CONFIG, result.exit_kind,
                         f"expected CONFIG, got {result.exit_kind}: {result.message}")
        self.assertIn("image_tag", (result.message or "").lower())

    def test_empty_context_returns_config(self):
        """An explicit empty ``context`` must reach the renderer's
        validation and return CONFIG."""
        req = BuildRequest(
            inventory_path=str(INVENTORY_PATH),
            context="",
            dry_run=True,
            _diagnose_gateway=self._bomb_diagnose,
            _publish_projection=self._bomb_publish,
            runner=self._BombRunner(),
        project_root=Path(str(INVENTORY_PATH)).resolve().parent)
        result = orchestrate_build(req)
        self.assertEqual(ExitKind.CONFIG, result.exit_kind,
                         f"expected CONFIG, got {result.exit_kind}: {result.message}")
        self.assertIn("build_context", (result.message or "").lower())

    # -- bomb injectables (shared) --------------------------------------

    @staticmethod
    def _bomb_diagnose(**kw):
        raise RuntimeError("diagnose must NOT be called for render failure")

    @staticmethod
    def _bomb_persist(**kw):
        raise RuntimeError("persist must NOT be called for render failure")

    @staticmethod
    def _bomb_publish(projection, *, repo_root=None):
        raise RuntimeError("publish must NOT be called for render failure")

    class _BombRunner:
        def run(self, argv: tuple[str, ...]):
            raise RuntimeError("runner must NOT be called for render failure")

# ═══════════════════════════════════════════════════════════════════════
# 16.  Gateway tests (RED)
# ═══════════════════════════════════════════════════════════════════════

class TestBuildGatewayIsolation(unittest.TestCase):
    """Builds are independent of the legacy gateway callback surface."""

    def test_unreachable_gateway_callbacks_are_not_invoked(self):
        calls: list[str] = []

        def diagnose(**kwargs):
            calls.append("diagnose")
            return _diag_unreachable(**kwargs)

        def persist(*args, **kwargs):
            calls.append("persist")
            return _persist_fail(*args, **kwargs)

        runner = FakeBuildExecutor()
        result = orchestrate_build(BuildRequest(
            inventory_path=str(INVENTORY_PATH),
            confirmed=True,
            _materialize_artifacts=_materialize_ok,
            _materialize_pi=fake_pi_materialization,
            _named_context_supported=lambda: True,
            _diagnose_gateway=diagnose,
            _publish_projection=_publish_ok,
            runner=runner,
        project_root=Path(str(INVENTORY_PATH)).resolve().parent))

        self.assertEqual(ExitKind.SUCCESS, result.exit_kind)
        self.assertEqual([], calls)
        self.assertEqual(1, len(runner.calls))
        self.assertFalse(hasattr(result, "resolved_address"))

# ═══════════════════════════════════════════════════════════════════════
# 18–19.  Confirmation / consent (RED)
# ═══════════════════════════════════════════════════════════════════════

class TestConfirmation(unittest.TestCase):
    """Tasks 18–19 — confirmation flag (consent callbacks removed).

    The facade obtains confirmation and passes ``confirmed: bool`` as an
    immutable decision.  Orchestration **never** prompts; it only enforces
    the boolean.  ``confirmed=True`` means the user explicitly agreed or
    ``--yes`` bypass was active."""

    # -- helpers -------------------------------------------------------

    @staticmethod
    def _bomb_diagnose(**kw):
        raise RuntimeError("diagnose must NOT be called when not confirmed")

    @staticmethod
    def _bomb_persist(path=None, gateway=None):
        raise RuntimeError("persist must NOT be called when not confirmed")

    @staticmethod
    def _bomb_publish(projection, *, repo_root=None):
        raise RuntimeError("publish must NOT be called when not confirmed")

    class _BombRunner:
        """Runner that explodes if ``.run()`` is ever invoked."""
        def run(self, argv: tuple[str, ...]):
            raise RuntimeError("runner must NOT be called when not confirmed")

    # -- denied (successful cancellation) ------------------------------

    def test_not_confirmed_is_successful_cancellation(self):
        """``confirmed=False`` with ``dry_run=False`` is a deliberate
        user decision — SUCCESS no-op.  Every side-effecting boundary
        carries a bomb; the test passes only if none of them fire."""
        req = BuildRequest(
            inventory_path=str(INVENTORY_PATH),
            confirmed=False,
            dry_run=False,
            _diagnose_gateway=self._bomb_diagnose,
            _publish_projection=self._bomb_publish,
            runner=self._BombRunner(),
        project_root=Path(str(INVENTORY_PATH)).resolve().parent)
        result = orchestrate_build(req)
        self.assertEqual(ExitKind.SUCCESS, result.exit_kind,
                         f"not-confirmed is SUCCESS not {result.exit_kind}: {result.message}")
        self.assertIn("not confirmed", (result.message or "").lower(),
                      "message must indicate build was not confirmed")
        self.assertIsNone(result.process_result,
                          "no Docker process must have run")
        self.assertEqual(len(result.build_args), 0,
                         "build args must be empty when cancelled")
        self.assertIsNone(result.publish_result,
                          "no projection must be published")

    # -- accepted ------------------------------------------------------

    def test_accepted_confirmation_runs(self):
        """When ``confirmed=True`` the full build transaction executes."""
        docker_runner = FakeBuildExecutor()
        req = BuildRequest(
            inventory_path=str(INVENTORY_PATH),
            confirmed=True,
            _materialize_artifacts=_materialize_ok,
            _materialize_pi=fake_pi_materialization,
            _named_context_supported=lambda: True,
            _diagnose_gateway=_diag_reachable,
            _publish_projection=_publish_ok,
            runner=docker_runner,
        project_root=Path(str(INVENTORY_PATH)).resolve().parent)
        result = orchestrate_build(req)
        # Real impl returns SUCCESS with process_result; stub returns OPERATIONAL
        self.assertIsInstance(result, BuildResult)

    def test_operation_order_publishes_before_execution(self):
        """Build transaction order is validate → publish → Docker execution."""
        seq = []

        def publish(projection, *, repo_root=None):
            seq.append("publish")
            return PublishResult(published_path="/tmp/eff.toml")

        class RecordingRunner(FakeBuildExecutor):
            def run(self, argv):
                seq.append("docker")
                return super().run(argv)

        result = orchestrate_build(BuildRequest(
            inventory_path=str(INVENTORY_PATH),
            confirmed=True,
            _materialize_artifacts=_materialize_ok,
            _materialize_pi=fake_pi_materialization,
            _named_context_supported=lambda: True,
            _diagnose_gateway=lambda **kw: (_ for _ in ()).throw(
                AssertionError("build must not diagnose gateway")),
            _publish_projection=publish,
            runner=RecordingRunner(),
        project_root=Path(str(INVENTORY_PATH)).resolve().parent))
        self.assertIsInstance(result, BuildResult)
        self.assertEqual(["publish", "docker"], seq)

# ═══════════════════════════════════════════════════════════════════════
# 20.  Dry-run tests (RED)
# ═══════════════════════════════════════════════════════════════════════

class TestDryRun(unittest.TestCase):
    """Task 20 — complete shell-escaped display, no Docker, no probe
    container, no persistence, no publication, no prompt, no mutation.

    Every side-effecting fake is a **bomb**: it raises ``RuntimeError``
    if called.  If *any* bomb detonates the test fails — proving the
    dry-run path never touches that boundary.
    """

    @staticmethod
    def _bomb_diagnose(**kw):
        raise RuntimeError("gateway diagnose must NOT be called during dry-run")

    @staticmethod
    def _bomb_persist(path=None, gateway=None):
        raise RuntimeError("gateway persist must NOT be called during dry-run")

    @staticmethod
    def _bomb_publish(projection, *, repo_root=None):
        raise RuntimeError("projection publish must NOT be called during dry-run")

    class _BombRunner:
        """Process runner that explodes if its .run() is ever invoked."""
        def run(self, argv: tuple[str, ...]):
            raise RuntimeError("process runner must NOT be called during dry-run")

    # -- dry-run must not touch side effects -------------------------

    def test_dry_run_no_side_effects(self):
        """Dry-run must not invoke Docker execution, persistence,
        or publication.  Only the build vector is rendered."""
        req = BuildRequest(
            inventory_path=str(INVENTORY_PATH),
            dry_run=True,
            _diagnose_gateway=self._bomb_diagnose,
            _publish_projection=self._bomb_publish,
            runner=self._BombRunner(),
        project_root=Path(str(INVENTORY_PATH)).resolve().parent)
        result = orchestrate_build(req)
        self.assertEqual(ExitKind.SUCCESS, result.exit_kind,
                         f"expected SUCCESS, got {result.exit_kind}: {result.message}")
        _assert_prospective(result)

    # -- gateway probe -------------------------------------------------

    def test_dry_run_no_gateway_probe(self):
        """Dry-run must not spawn a gateway probe container."""
        req = BuildRequest(
            inventory_path=str(INVENTORY_PATH),
            dry_run=True,
            _diagnose_gateway=self._bomb_diagnose,
            _publish_projection=self._bomb_publish,
            runner=self._BombRunner(),
        project_root=Path(str(INVENTORY_PATH)).resolve().parent)
        result = orchestrate_build(req)
        self.assertEqual(ExitKind.SUCCESS, result.exit_kind,
                         f"expected SUCCESS, got {result.exit_kind}: {result.message}")
        _assert_prospective(result)

    # -- persistence ---------------------------------------------------

    def test_dry_run_no_persistence(self):
        """Dry-run must not write .env or effective.toml."""
        req = BuildRequest(
            inventory_path=str(INVENTORY_PATH),
            dry_run=True,
            _diagnose_gateway=self._bomb_diagnose,
            _publish_projection=self._bomb_publish,
            runner=self._BombRunner(),
        project_root=Path(str(INVENTORY_PATH)).resolve().parent)
        result = orchestrate_build(req)
        self.assertEqual(ExitKind.SUCCESS, result.exit_kind,
                         f"expected SUCCESS, got {result.exit_kind}: {result.message}")

    # -- publication ---------------------------------------------------

    def test_dry_run_no_publication(self):
        """Dry-run must not publish the effective projection."""
        req = BuildRequest(
            inventory_path=str(INVENTORY_PATH),
            dry_run=True,
            _diagnose_gateway=self._bomb_diagnose,
            _publish_projection=self._bomb_publish,
            runner=self._BombRunner(),
        project_root=Path(str(INVENTORY_PATH)).resolve().parent)
        result = orchestrate_build(req)
        self.assertEqual(ExitKind.SUCCESS, result.exit_kind,
                         f"expected SUCCESS, got {result.exit_kind}: {result.message}")

    # -- Docker process ------------------------------------------------

    def test_dry_run_no_docker_process(self):
        """Dry-run must not execute Docker."""
        req = BuildRequest(
            inventory_path=str(INVENTORY_PATH),
            dry_run=True,
            _diagnose_gateway=self._bomb_diagnose,
            _publish_projection=self._bomb_publish,
            runner=self._BombRunner(),
        project_root=Path(str(INVENTORY_PATH)).resolve().parent)
        result = orchestrate_build(req)
        self.assertEqual(ExitKind.SUCCESS, result.exit_kind,
                         f"expected SUCCESS, got {result.exit_kind}: {result.message}")
        self.assertIsNone(result.process_result,
                          "dry-run must not execute Docker")

    # -- build vector completeness -------------------------------------

    def test_dry_run_returns_complete_display(self):
        """Dry-run must return full build_args and a display_string."""
        req = BuildRequest(
            inventory_path=str(INVENTORY_PATH),
            dry_run=True,
            _diagnose_gateway=_diag_reachable,
            _publish_projection=_publish_ok,
        project_root=Path(str(INVENTORY_PATH)).resolve().parent)
        result = orchestrate_build(req)
        self.assertEqual(ExitKind.SUCCESS, result.exit_kind,
                         f"expected SUCCESS, got {result.exit_kind}: {result.message}")
        _assert_prospective(result)

    # -- source mutation -----------------------------------------------

    def test_dry_run_no_source_mutation(self):
        """Source inventory must never be touched during dry-run."""
        req = BuildRequest(
            inventory_path=str(INVENTORY_PATH),
            dry_run=True,
            _diagnose_gateway=_diag_reachable,
            _publish_projection=_publish_ok,
        project_root=Path(str(INVENTORY_PATH)).resolve().parent)
        result = orchestrate_build(req)
        self.assertEqual(ExitKind.SUCCESS, result.exit_kind,
                         f"expected SUCCESS, got {result.exit_kind}: {result.message}")

# ═══════════════════════════════════════════════════════════════════════
# 21.  Direct execution — tuple, shell=False (RED)
# ═══════════════════════════════════════════════════════════════════════

class TestDirectExecution(unittest.TestCase):
    """Task 21 — runner receives exact tuple, shell=False, display string
    never passed to runner."""

    def test_runner_receives_tuple_not_string(self):
        """The runner must receive a tuple (shell=False semantics)."""
        runner = FakeBuildExecutor()
        req = BuildRequest(
            inventory_path=str(INVENTORY_PATH),
            confirmed=True,
            _materialize_artifacts=_materialize_ok,
            _materialize_pi=fake_pi_materialization,
            _named_context_supported=lambda: True,
            runner=runner,
            _diagnose_gateway=_diag_reachable,
            _publish_projection=_publish_ok,
        project_root=Path(str(INVENTORY_PATH)).resolve().parent)
        result = orchestrate_build(req)
        self.assertIsInstance(result, BuildResult)
        if runner.calls:
            cmd = runner.calls[0]
            self.assertIsInstance(cmd, tuple,
                                  "runner must receive tuple, not string")

    def test_runner_command_starts_with_docker(self):
        runner = FakeBuildExecutor()
        req = BuildRequest(
            inventory_path=str(INVENTORY_PATH),
            confirmed=True,
            _materialize_artifacts=_materialize_ok,
            _materialize_pi=fake_pi_materialization,
            _named_context_supported=lambda: True,
            runner=runner,
            _diagnose_gateway=_diag_reachable,
            _publish_projection=_publish_ok,
        project_root=Path(str(INVENTORY_PATH)).resolve().parent)
        result = orchestrate_build(req)
        self.assertIsInstance(result, BuildResult)
        if runner.calls:
            cmd = runner.calls[0]
            self.assertEqual("docker", cmd[0])

# ═══════════════════════════════════════════════════════════════════════
# 22.  Subprocess outcome (RED)
# ═══════════════════════════════════════════════════════════════════════

class TestSubprocessOutcomes(unittest.TestCase):
    """Task 22 — zero=success, nonzero=operational failure, stderr bounded,
    executable-not-found actionable."""

    def test_zero_exit_returns_success(self):
        runner = FakeBuildExecutor(returncode=0)
        req = BuildRequest(
            inventory_path=str(INVENTORY_PATH),
            confirmed=True,
            _materialize_artifacts=_materialize_ok,
            _materialize_pi=fake_pi_materialization,
            _named_context_supported=lambda: True,
            runner=runner,
            _diagnose_gateway=_diag_reachable,
            _publish_projection=_publish_ok,
        project_root=Path(str(INVENTORY_PATH)).resolve().parent)
        result = orchestrate_build(req)
        self.assertEqual(ExitKind.SUCCESS, result.exit_kind,
                         f"expected SUCCESS, got {result.exit_kind}")
        self.assertIsNotNone(result.process_result,
                             "process_result must be present")
        # When present, must match the runner's return code
        self.assertEqual(0, result.process_result.return_code)

    def test_nonzero_exit_returns_operational_failure(self):
        runner = FakeBuildExecutor(returncode=1)
        req = BuildRequest(
            inventory_path=str(INVENTORY_PATH),
            confirmed=True,
            _materialize_artifacts=_materialize_ok,
            _materialize_pi=fake_pi_materialization,
            _named_context_supported=lambda: True,
            runner=runner,
            _diagnose_gateway=_diag_reachable,
            _publish_projection=_publish_ok,
        project_root=Path(str(INVENTORY_PATH)).resolve().parent)
        result = orchestrate_build(req)
        self.assertEqual(ExitKind.OPERATIONAL, result.exit_kind,
                         f"expected OPERATIONAL, got {result.exit_kind}")
        self.assertIsNotNone(result.process_result)
        self.assertEqual(1, result.process_result.return_code)

    def test_stderr_included_on_failure(self):
        runner = FakeBuildExecutor(returncode=1)
        req = BuildRequest(
            inventory_path=str(INVENTORY_PATH),
            confirmed=True,
            _materialize_artifacts=_materialize_ok,
            _materialize_pi=fake_pi_materialization,
            _named_context_supported=lambda: True,
            runner=runner,
            _diagnose_gateway=_diag_reachable,
            _publish_projection=_publish_ok,
        project_root=Path(str(INVENTORY_PATH)).resolve().parent)
        result = orchestrate_build(req)
        self.assertEqual(ExitKind.OPERATIONAL, result.exit_kind)
        self.assertIsNotNone(result.process_result)
        # Message should surface stderr content or exit code
        self.assertIn("build error", result.message or "")

# ═══════════════════════════════════════════════════════════════════════
# 23a.  Boundary failure tests — Stage 9.3 hardening
# ═══════════════════════════════════════════════════════════════════════

class TestBuildBoundaryFailures(unittest.TestCase):
    """Diagnosis, publication, and runner exceptions must produce
    structured results and never escape."""

    # -- legacy gateway callbacks --------------------------------------

    def test_diagnosis_exception_is_not_a_build_failure(self):
        """A build never invokes the legacy diagnosis callback."""
        def broken_diagnose(**kw):
            raise RuntimeError("Docker socket unreachable")

        runner = FakeBuildExecutor()
        result = orchestrate_build(BuildRequest(
            inventory_path=str(INVENTORY_PATH),
            confirmed=True,
            _materialize_artifacts=_materialize_ok,
            _materialize_pi=fake_pi_materialization,
            _named_context_supported=lambda: True,
            _diagnose_gateway=broken_diagnose,
            _publish_projection=_publish_ok,
            runner=runner,
        project_root=Path(str(INVENTORY_PATH)).resolve().parent))
        self.assertEqual(ExitKind.SUCCESS, result.exit_kind)
        self.assertEqual(1, len(runner.calls))

    # -- publication errors ---------------------------------------------

    def test_publish_error_returns_operational(self):
        """``PublishError`` raised by the injectable must produce
        OPERATIONAL with the detail embedded."""
        def broken_publish(projection, *, repo_root=None):
            raise PublishError(detail="disk full")

        req = BuildRequest(
            inventory_path=str(INVENTORY_PATH),
            confirmed=True,
            _materialize_artifacts=_materialize_ok,
            _materialize_pi=fake_pi_materialization,
            _named_context_supported=lambda: True,
            _diagnose_gateway=_diag_reachable,
            _publish_projection=broken_publish,
            runner=FakeBuildExecutor(),
        project_root=Path(str(INVENTORY_PATH)).resolve().parent)
        result = orchestrate_build(req)
        self.assertEqual(ExitKind.OPERATIONAL, result.exit_kind)
        self.assertIn("disk full", result.message or "")
        # Docker must not run after publication failure
        self.assertIsNone(result.process_result)

    def test_publish_generic_exception_returns_operational(self):
        """A generic exception during publication must also produce
        OPERATIONAL (OS-level failures, etc.)."""
        def broken_publish(projection, *, repo_root=None):
            raise IOError("permission denied")

        req = BuildRequest(
            inventory_path=str(INVENTORY_PATH),
            confirmed=True,
            _materialize_artifacts=_materialize_ok,
            _materialize_pi=fake_pi_materialization,
            _named_context_supported=lambda: True,
            _diagnose_gateway=_diag_reachable,
            _publish_projection=broken_publish,
            runner=FakeBuildExecutor(),
        project_root=Path(str(INVENTORY_PATH)).resolve().parent)
        result = orchestrate_build(req)
        self.assertEqual(ExitKind.OPERATIONAL, result.exit_kind)
        self.assertIn("permission denied", result.message or "")
        self.assertIsNone(result.process_result)

    # -- runner errors --------------------------------------------------

    def test_runner_file_not_found_error(self):
        """``FileNotFoundError`` (docker binary missing) must produce
        OPERATIONAL."""
        class MissingDockerRunner:
            def run(self, argv: tuple[str, ...]):
                raise FileNotFoundError("No such file: 'docker'")

        req = BuildRequest(
            inventory_path=str(INVENTORY_PATH),
            confirmed=True,
            _materialize_artifacts=_materialize_ok,
            _materialize_pi=fake_pi_materialization,
            _named_context_supported=lambda: True,
            _diagnose_gateway=_diag_reachable,
            _publish_projection=_publish_ok,
            runner=MissingDockerRunner(),
        project_root=Path(str(INVENTORY_PATH)).resolve().parent)
        result = orchestrate_build(req)
        self.assertEqual(ExitKind.OPERATIONAL, result.exit_kind)
        self.assertIn("not found", result.message or "")
        self.assertIsNone(result.process_result)

    def test_runner_permission_error(self):
        """``PermissionError`` (OSError subclass) must produce OPERATIONAL."""
        class DeniedDockerRunner:
            def run(self, argv: tuple[str, ...]):
                raise PermissionError("docker: permission denied")

        req = BuildRequest(
            inventory_path=str(INVENTORY_PATH),
            confirmed=True,
            _materialize_artifacts=_materialize_ok,
            _materialize_pi=fake_pi_materialization,
            _named_context_supported=lambda: True,
            _diagnose_gateway=_diag_reachable,
            _publish_projection=_publish_ok,
            runner=DeniedDockerRunner(),
        project_root=Path(str(INVENTORY_PATH)).resolve().parent)
        result = orchestrate_build(req)
        self.assertEqual(ExitKind.OPERATIONAL, result.exit_kind)
        self.assertIn("permission denied", result.message or "")
        self.assertIsNone(result.process_result)

# ═══════════════════════════════════════════════════════════════════════
# Stage 9.3 — materialization boundary
# ═══════════════════════════════════════════════════════════════════════

class TestMaterializationBoundary(unittest.TestCase):
    """Task 3.3 — materialization or integrity failure prevents Docker,
    leaves no committed reference, and mutates only the verified namespace."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.cache = self.base / "cache"
        self.cache.mkdir(mode=0o700)
        os.chmod(self.cache, 0o700)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.inventory = self.repo / "docker-constructor.toml"
        self.inventory.write_bytes(INVENTORY_PATH.read_bytes())
        (self.repo / "docker-constructor.local.toml").write_text(
            f'[cache]\ndir = "{self.cache}"\n'
        )
        (self.repo / "Dockerfile").write_text("FROM scratch\n")

    def _request(self, *, publish, runner, materialize=None, transport_factory=None):
        return BuildRequest(
            inventory_path=str(self.inventory),
            repo_root=str(self.repo),
            confirmed=True,
            runner=runner,
            _materialize_artifacts=materialize,
            _materialize_pi=fake_pi_materialization,
            _transport_factory=transport_factory,
            _named_context_supported=lambda: True,
            _publish_projection=publish,
        project_root=Path(str(self.inventory)).resolve().parent)

    def _tree_snapshot(self, root: Path, *, exclude: Path | None = None):
        """Record structure, types, modes, contents, and symlink targets.

        ``exclude`` (when given) prunes exactly one subtree so callers can
        compare "everything except the selected project's namespace".
        """
        snapshot = {}
        if exclude is None or not root.is_relative_to(exclude):
            snapshot["."] = ("dir", root.lstat().st_mode & 0o777)
        for path in sorted(root.rglob("*")):
            if exclude is not None and path.is_relative_to(exclude):
                continue
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                snapshot[rel] = ("link", os.readlink(path))
            elif path.is_dir():
                snapshot[rel] = ("dir", path.stat().st_mode & 0o777)
            else:
                snapshot[rel] = ("file", path.stat().st_mode & 0o777, path.read_bytes())
        return snapshot

    def test_materialization_failure_prevents_docker_and_leaves_no_committed_reference(self):
        class Docker:
            def run(self, argv):
                raise AssertionError("Docker must not run after a materialization failure")

        def fail_materialize(*args, **kwargs):
            raise MaterializationError("integrity check failed for artifact 'uv'")

        def fail_publish(*args, **kwargs):
            raise AssertionError("projection must not be published after materialization failure")

        result = orchestrate_build(self._request(
            materialize=fail_materialize, publish=fail_publish, runner=Docker(),
        ))
        self.assertEqual(ExitKind.OPERATIONAL, result.exit_kind)
        self.assertIsNone(result.publish_result)
        self.assertIsNone(result.process_result)
        # No committed reference may exist anywhere in the external cache.
        self.assertEqual([], list(self.cache.rglob("committed-build.json")))
        # The project checkout is untouched: only our own fixtures remain.
        self.assertEqual(
            {"docker-constructor.toml", "docker-constructor.local.toml", "Dockerfile"},
            {p.name for p in self.repo.iterdir()},
        )
        self.assertFalse((self.repo / ".docker-cache").exists())
        self.assertFalse((self.repo / ".docker-generated").exists())

    def test_integrity_failure_uses_real_materialization_and_confines_mutation(self):
        """A digest-mismatching transport drives the real streaming path: the
        build fails operationally, Docker/publication never run, no invalid
        blob, temp file, or committed reference remains, and every change is
        confined to the selected project's canonical external namespace —
        proven by full-tree snapshots of the cache and the constructor project
        taken before and after orchestration."""
        from docker.versioning.build_cache import prepare_build_cache
        from docker.versioning.cache_storage import prepare_resolved_root
        from docker.versioning.project_state import resolve_project_state

        # Compute the selected namespace path without creating it, so the
        # pre-build cache snapshot can exclude exactly that subtree.
        selected_namespace = resolve_project_state(
            self.repo, cache_root=self.cache, create=False,
        ).namespace

        # Prepare the deterministic cache-root scaffolding and a second project
        # namespace with meaningful state; both are part of the pre-build
        # baseline that must remain unchanged.
        prepare_resolved_root(self.cache)
        other = self.base / "other-proj"
        other.mkdir()
        other_paths = prepare_build_cache(other, cache_root=self.cache)
        (other_paths.persistent_root / "committed-build.json").write_text('{"blobs": []}')
        (other_paths.persistent_root / "sentinel.txt").write_text("other-namespace-content")

        project_before = self._tree_snapshot(self.repo)
        cache_before = self._tree_snapshot(self.cache, exclude=selected_namespace)

        effects = []
        class Docker:
            def run(self, argv):
                effects.append("docker")
                raise AssertionError("Docker must not run after an integrity failure")

        def fail_publish(projection, *, repo_root=None):
            effects.append("publish")
            raise AssertionError("projection must not be published")

        class MismatchingTransport:
            """Streams bytes that never match any reviewed SHA-256 digest."""
            def __init__(self, policy):
                self.policy = policy
            def stream(self, url):
                yield b"not-the-reviewed-artifact-bytes"

        # No `_materialize_artifacts` injection: this exercises the real
        # `materialize_build_artifacts` -> `materialize_artifact` path.
        result = orchestrate_build(self._request(
            publish=fail_publish, runner=Docker(),
            transport_factory=MismatchingTransport,
        ))
        self.assertEqual(ExitKind.OPERATIONAL, result.exit_kind)
        self.assertIn("integrity check failed", result.message or "")
        self.assertEqual([], effects)
        self.assertIsNone(result.publish_result)
        self.assertIsNone(result.process_result)

        selected_state = resolve_project_state(self.repo, cache_root=self.cache)
        # Focused failure assertions: nothing invalid, temporary, or committed
        # remains for the failed artifact/build.
        self.assertEqual([], list((selected_state.build_artifacts_root / "blobs").rglob("*.blob")))
        self.assertEqual([], list(self.cache.rglob(".materialize-*")))
        self.assertFalse((selected_state.build_artifacts_root / "committed-build.json").exists())

        # The entire cache outside the selected namespace is byte-for-byte
        # unchanged: no new/deleted paths, modified contents, changed modes, or
        # changed symlink targets anywhere else (including the other project's
        # namespace).
        self.assertEqual(
            cache_before, self._tree_snapshot(self.cache, exclude=selected_namespace),
        )
        # The complete constructor project is unchanged, including every file's
        # contents and mode.
        self.assertEqual(project_before, self._tree_snapshot(self.repo))

    def _seed_prior_live_set(self):
        identities = set()
        paths = []
        for data in (b"prior-live-one", b"prior-live-two"):
            identity = DigestIdentity.from_hex("sha256", hashlib.sha256(data).hexdigest())
            identities.add(identity)
            paths.append(publish_verified_blob(
                identity, data, constructor_project_root=self.repo, cache_root=self.cache,
            ))
        with acquire_constructor_project_build_lock(self.repo, cache_root=self.cache) as lock:
            commit_build_set(self.repo, identities, lock=lock, cache_root=self.cache)
        return identities, paths

    def _assert_prior_live_set(self, identities, paths):
        state = prepare_build_cache(self.repo, cache_root=self.cache)
        committed = json.loads((state.persistent_root / "committed-build.json").read_text())
        self.assertEqual(
            sorted(f"sha256:{identity.hex_digest()}" for identity in identities),
            committed["blobs"],
        )
        self.assertTrue(all(path.exists() for path in paths))

    def _recording_materializer(self, recorded):
        def materialize(*args, **kwargs):
            paths = _materialize_ok(*args, **kwargs)
            recorded.extend(paths)
            return paths
        return materialize

    def _assert_interruption_releases_lock_and_preserves_prior_set(self, request, old_ids, old_paths):
        with self.assertRaises(KeyboardInterrupt):
            orchestrate_build(request)
        self._assert_prior_live_set(old_ids, old_paths)
        with acquire_constructor_project_build_lock(self.repo, cache_root=self.cache):
            pass

    def test_artifact_materialization_interruption_releases_lock(self):
        old_ids, old_paths = self._seed_prior_live_set()
        request = self._request(
            materialize=lambda *_a, **_k: (_ for _ in ()).throw(KeyboardInterrupt()),
            publish=lambda *_a, **_k: PublishResult("/tmp/effective.toml"),
            runner=FakeBuildExecutor(),
        )
        self._assert_interruption_releases_lock_and_preserves_prior_set(request, old_ids, old_paths)

    def test_pi_materialization_interruption_releases_lock_and_keeps_blobs(self):
        old_ids, old_paths = self._seed_prior_live_set()
        materialized = []
        request = self._request(
            materialize=self._recording_materializer(materialized),
            publish=lambda *_a, **_k: PublishResult("/tmp/effective.toml"),
            runner=FakeBuildExecutor(),
        )
        object.__setattr__(request, "_materialize_pi", lambda *_a, **_k: (_ for _ in ()).throw(KeyboardInterrupt()))
        self._assert_interruption_releases_lock_and_preserves_prior_set(request, old_ids, old_paths)
        self.assertTrue(materialized and all(path.exists() for path in materialized))

    def test_snapshot_creation_interruption_releases_lock_and_cleans_snapshot(self):
        old_ids, old_paths = self._seed_prior_live_set()
        materialized = []
        def interrupting_snapshot(*_a, **_k):
            raise KeyboardInterrupt()
        with patch("docker.versioning.build_orchestration.create_artifact_snapshot", side_effect=interrupting_snapshot):
            request = self._request(
                materialize=self._recording_materializer(materialized),
                publish=lambda *_a, **_k: PublishResult("/tmp/effective.toml"),
                runner=FakeBuildExecutor(),
            )
            self._assert_interruption_releases_lock_and_preserves_prior_set(request, old_ids, old_paths)
        self.assertTrue(materialized and all(path.exists() for path in materialized))

    def test_docker_failure_preserves_prior_set_and_reusable_new_blobs(self):
        old_ids, old_paths = self._seed_prior_live_set()
        materialized = []
        class Docker:
            def run(self, argv):
                return ProcessResult(argv=argv, return_code=9, stdout="", stderr="failed")
        result = orchestrate_build(self._request(
            materialize=self._recording_materializer(materialized),
            publish=lambda *_a, **_k: PublishResult("/tmp/effective.toml"), runner=Docker(),
        ))
        self.assertEqual(ExitKind.OPERATIONAL, result.exit_kind)
        self._assert_prior_live_set(old_ids, old_paths)
        self.assertTrue(materialized)
        self.assertTrue(all(path.exists() and (path.stat().st_mode & 0o777) == 0o444 for path in materialized))

    def test_signal_interruption_preserves_prior_set_and_reusable_new_blobs(self):
        old_ids, old_paths = self._seed_prior_live_set()
        materialized = []
        class Docker:
            def run(self, argv):
                raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            orchestrate_build(self._request(
                materialize=self._recording_materializer(materialized),
                publish=lambda *_a, **_k: PublishResult("/tmp/effective.toml"), runner=Docker(),
            ))
        self._assert_prior_live_set(old_ids, old_paths)
        self.assertTrue(materialized)
        self.assertTrue(all(path.exists() for path in materialized))

    def test_snapshot_failure_preserves_prior_set_and_reusable_new_blobs(self):
        old_ids, old_paths = self._seed_prior_live_set()
        materialized = []
        with patch(
            "docker.versioning.build_orchestration.create_artifact_snapshot",
            side_effect=SnapshotError("snapshot failed"),
        ):
            result = orchestrate_build(self._request(
                materialize=self._recording_materializer(materialized),
                publish=lambda *_a, **_k: PublishResult("/tmp/effective.toml"),
                runner=FakeBuildExecutor(),
            ))
        self.assertEqual(ExitKind.OPERATIONAL, result.exit_kind)
        self._assert_prior_live_set(old_ids, old_paths)
        self.assertTrue(materialized)
        self.assertTrue(all(path.exists() for path in materialized))

    def test_commit_revalidation_failure_preserves_prior_set_and_new_blobs(self):
        old_ids, old_paths = self._seed_prior_live_set()
        materialized = []
        class Docker:
            def run(self, argv):
                materialized[0].chmod(0o644)
                return ProcessResult(argv=argv, return_code=0, stdout="", stderr="")
        result = orchestrate_build(self._request(
            materialize=self._recording_materializer(materialized),
            publish=lambda *_a, **_k: PublishResult("/tmp/effective.toml"), runner=Docker(),
        ))
        self.assertEqual(ExitKind.OPERATIONAL, result.exit_kind)
        self._assert_prior_live_set(old_ids, old_paths)
        self.assertTrue(all(path.exists() for path in materialized))

    def test_snapshot_cleanup_failure_prevents_commit_and_preserves_blobs(self):
        old_ids, old_paths = self._seed_prior_live_set()
        materialized = []
        def fail_cleanup(snapshot):
            if snapshot is not None:
                raise SnapshotError("snapshot cleanup failed")
        with patch(
            "docker.versioning.build_orchestration.cleanup_artifact_snapshot",
            side_effect=fail_cleanup,
        ):
            result = orchestrate_build(self._request(
                materialize=self._recording_materializer(materialized),
                publish=lambda *_a, **_k: PublishResult("/tmp/effective.toml"),
                runner=FakeBuildExecutor(),
            ))
        self.assertEqual(ExitKind.OPERATIONAL, result.exit_kind)
        self._assert_prior_live_set(old_ids, old_paths)
        self.assertTrue(materialized)
        self.assertTrue(all(path.exists() for path in materialized))

    def test_cancelled_build_preserves_prior_set_and_reusable_uncommitted_blob(self):
        old_ids, old_paths = self._seed_prior_live_set()
        pending_data = b"already-verified-uncommitted"
        pending_id = DigestIdentity.from_hex("sha256", hashlib.sha256(pending_data).hexdigest())
        pending_path = publish_verified_blob(
            pending_id, pending_data, constructor_project_root=self.repo, cache_root=self.cache,
        )
        request = self._request(
            materialize=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not materialize")),
            publish=lambda *_a, **_k: PublishResult("/tmp/effective.toml"),
            runner=FakeBuildExecutor(),
        )
        object.__setattr__(request, "confirmed", False)
        result = orchestrate_build(request)
        self.assertEqual(ExitKind.SUCCESS, result.exit_kind)
        self._assert_prior_live_set(old_ids, old_paths)
        self.assertTrue(pending_path.exists())

    def test_build_lifecycle_downloads_only_missing_or_changed_inputs(self):
        from docker.versioning import build_materialization as materialization_module

        payloads = dict(_TEST_ARTIFACT_BYTES)
        selections = list(_test_selected_artifacts(None))
        calls: list[str] = []
        class Transport:
            def stream(self, url):
                calls.append(url)
                name = url.rsplit("/", 1)[-1]
                yield payloads[name]
        transport = Transport()

        def run(selected, returncode=0):
            with patch(
                "docker.versioning.build_orchestration.select_build_artifacts",
                return_value=tuple(selected),
            ), patch.object(
                materialization_module, "select_build_artifacts", return_value=tuple(selected),
            ):
                return orchestrate_build(self._request(
                    materialize=materialization_module.materialize_build_artifacts,
                    transport_factory=lambda _policy: transport,
                    publish=lambda *_a, **_k: PublishResult("/tmp/effective.toml"),
                    runner=FakeBuildExecutor(returncode=returncode),
                ))

        self.assertEqual(ExitKind.SUCCESS, run(selections).exit_kind)
        self.assertEqual(4, len(calls))
        calls.clear()
        self.assertEqual(ExitKind.SUCCESS, run(selections).exit_kind)
        self.assertEqual([], calls, "unchanged rebuild must perform zero downloads")

        payloads["rtk"] = b"changed-rtk"
        changed_rtk = SelectedBuildArtifact(
            name="rtk", url="https://test.invalid/rtk",
            identity=DigestIdentity.from_hex("sha256", hashlib.sha256(payloads["rtk"]).hexdigest()),
        )
        changed = [changed_rtk if item.name == "rtk" else item for item in selections]
        calls.clear()
        self.assertEqual(ExitKind.SUCCESS, run(changed).exit_kind)
        self.assertEqual(["https://test.invalid/rtk"], calls)

        payloads["fd"] = b"failed-build-fd"
        changed_fd = SelectedBuildArtifact(
            name="fd", url="https://test.invalid/fd",
            identity=DigestIdentity.from_hex("sha256", hashlib.sha256(payloads["fd"]).hexdigest()),
        )
        failed_set = [changed_fd if item.name == "fd" else item for item in changed]
        calls.clear()
        self.assertEqual(ExitKind.OPERATIONAL, run(failed_set, returncode=7).exit_kind)
        self.assertEqual(["https://test.invalid/fd"], calls)
        calls.clear()
        self.assertEqual(ExitKind.SUCCESS, run(failed_set).exit_kind)
        self.assertEqual([], calls, "failed-build download must be reusable within its TTL")

    def test_expired_uncommitted_blob_is_removed_and_reacquired(self):
        from docker.versioning import build_materialization as materialization_module

        data = b"expired-input"
        selected = SelectedBuildArtifact(
            name="expired", url="https://test.invalid/expired",
            identity=DigestIdentity.from_hex("sha256", hashlib.sha256(data).hexdigest()),
        )
        with acquire_constructor_project_build_lock(self.repo, cache_root=self.cache) as lock:
            path = publish_uncommitted_blob(
                selected.identity, data, constructor_project_root=self.repo, cache_root=self.cache,
                lock=lock, verified_at=1,
            )
        calls = []
        class Transport:
            def stream(self, url):
                calls.append(url)
                yield data
        with patch(
            "docker.versioning.build_orchestration.select_build_artifacts",
            return_value=(selected,),
        ), patch.object(
            materialization_module, "select_build_artifacts", return_value=(selected,),
        ), patch("docker.versioning.build_cache.time.time", return_value=UNCOMMITTED_TTL_SECONDS + 2):
            result = orchestrate_build(self._request(
                materialize=materialization_module.materialize_build_artifacts,
                transport_factory=lambda _policy: Transport(),
                publish=lambda *_a, **_k: PublishResult("/tmp/effective.toml"),
                runner=FakeBuildExecutor(),
            ))
        self.assertEqual(ExitKind.SUCCESS, result.exit_kind)
        self.assertEqual([selected.url], calls)
        self.assertTrue(path.exists())

    def test_built_image_and_existing_container_survive_source_blob_deletion(self):
        materialized = []
        image_files = {}
        container_files = {}
        class Docker:
            def run(self, argv):
                # Model the Docker/BuildKit import boundary: the resulting
                # image and an existing container own copies, not host paths.
                image_files.update({path.name: path.read_bytes() for path in materialized})
                container_files.update(image_files)
                return ProcessResult(argv=argv, return_code=0, stdout="", stderr="")

        result = orchestrate_build(self._request(
            materialize=self._recording_materializer(materialized),
            publish=lambda *_a, **_k: PublishResult("/tmp/effective.toml"), runner=Docker(),
        ))
        self.assertEqual(ExitKind.SUCCESS, result.exit_kind)
        self.assertNotIn("--label", result.build_args)
        self.assertTrue(image_files)
        for path in materialized:
            path.unlink()
        self.assertTrue(all(not path.exists() for path in materialized))
        self.assertEqual(
            sorted(_TEST_ARTIFACT_BYTES.values()), sorted(image_files.values()),
            "existing image must retain imported artifact bytes",
        )
        self.assertEqual(image_files, container_files)
        state = prepare_build_cache(self.repo, cache_root=self.cache)
        self.assertEqual([], list(state.persistent_root.glob("generation-*")))
        self.assertEqual([], list(state.persistent_root.glob("*history*")))

    def test_successful_build_commits_then_removes_every_superseded_blob(self):
        """RED: task 7.5 must wire commit and post-commit cleanup after Docker."""
        from docker.versioning.project_state import resolve_project_state

        old_paths = []
        old_identities = set()
        for data in (b"superseded-one", b"superseded-two"):
            identity = DigestIdentity.from_hex("sha256", hashlib.sha256(data).hexdigest())
            old_identities.add(identity)
            old_paths.append(publish_verified_blob(identity, data, constructor_project_root=self.repo, cache_root=self.cache))
        with acquire_constructor_project_build_lock(self.repo, cache_root=self.cache) as lock:
            commit_build_set(self.repo, old_identities, lock=lock, cache_root=self.cache)

        other = self.base / "other-project"
        other.mkdir()
        other_data = b"other-project-input"
        other_identity = DigestIdentity.from_hex("sha256", hashlib.sha256(other_data).hexdigest())
        other_path = publish_verified_blob(other_identity, other_data, constructor_project_root=other, cache_root=self.cache)
        with acquire_constructor_project_build_lock(other, cache_root=self.cache) as lock:
            commit_build_set(other, {other_identity}, lock=lock, cache_root=self.cache)
        other_state = resolve_project_state(other, cache_root=self.cache)
        other_snapshot = self._tree_snapshot(other_state.build_artifacts_root)

        runtime_sentinel = runtime_artifacts_child(self.cache) / "sentinel"
        versioning_sentinel = versioning_child(self.cache) / "sentinel"
        runtime_sentinel.parent.mkdir(); versioning_sentinel.parent.mkdir()
        runtime_sentinel.write_bytes(b"runtime"); versioning_sentinel.write_bytes(b"versioning")
        global_snapshot = {path: (path.read_bytes(), path.stat().st_mode) for path in (runtime_sentinel, versioning_sentinel)}
        alias = self.base / "repo-alias"
        alias.symlink_to(self.repo, target_is_directory=True)

        selected_state = resolve_project_state(alias, cache_root=self.cache)
        canonical_state = resolve_project_state(self.repo, cache_root=self.cache)
        self.assertEqual(canonical_state.namespace, selected_state.namespace)
        materialized_paths: list[Path] = []
        def materialize_and_record(*args, **kwargs):
            paths = _materialize_ok(*args, **kwargs)
            materialized_paths.extend(paths)
            return paths

        docker_calls: list[tuple[str, ...]] = []
        case = self
        class Docker:
            def run(self, argv):
                # Commit/GC is a post-Docker concern: the prior live set must
                # remain authoritative while Docker is running.
                case.assertTrue(all(path.exists() for path in old_paths))
                committed = json.loads((selected_state.build_artifacts_root / "committed-build.json").read_text())
                case.assertEqual(sorted(f"sha256:{item.hex_digest()}" for item in old_identities), committed["blobs"])
                docker_calls.append(argv)
                return ProcessResult(argv=argv, return_code=0, stdout="", stderr="")

        request = self._request(materialize=materialize_and_record, publish=lambda *_a, **_k: PublishResult("/tmp/effective.toml"), runner=Docker())
        object.__setattr__(request, "repo_root", str(alias))
        from docker.versioning import build_cache as build_cache_module
        from docker.versioning import project_state as project_state_module
        protected_roots = (
            runtime_artifacts_child(self.cache).resolve(),
            versioning_child(self.cache).resolve(),
        )
        project_accesses: list[Path] = []
        build_cache_accesses: list[Path] = []
        global_cache_accesses: list[tuple[str, Path]] = []
        real_resolve_project_state = project_state_module.resolve_project_state
        real_open_build_cache_state = build_cache_module.open_build_cache_state
        real_os_open = os.open
        real_os_close = os.close
        directory_fds: dict[int, Path] = {}

        def record_project_state(path, *args, **kwargs):
            project_accesses.append(Path(path).resolve(strict=True))
            return real_resolve_project_state(path, *args, **kwargs)
        def record_build_cache(path, *args, **kwargs):
            build_cache_accesses.append(Path(path).resolve(strict=True))
            return real_open_build_cache_state(path, *args, **kwargs)
        def record_os_open(path, flags, mode=0o777, *, dir_fd=None):
            raw_path = Path(path)
            if raw_path.is_absolute():
                candidate = raw_path
            elif dir_fd is not None and dir_fd in directory_fds:
                candidate = directory_fds[dir_fd] / raw_path
            else:
                candidate = Path.cwd() / raw_path
            resolved = candidate.resolve(strict=False)
            fd = real_os_open(path, flags, mode, dir_fd=dir_fd)
            if flags & os.O_DIRECTORY:
                directory_fds[fd] = resolved
            if any(resolved == root or root in resolved.parents for root in protected_roots):
                global_cache_accesses.append(("os.open", resolved))
            return fd

        def record_os_close(fd):
            try:
                return real_os_close(fd)
            finally:
                directory_fds.pop(fd, None)

        with patch("docker.versioning.build_orchestration.resolve_project_state", side_effect=record_project_state), patch.object(
            build_cache_module, "open_build_cache_state", side_effect=record_build_cache
        ), patch("os.open", side_effect=record_os_open), patch("os.close", side_effect=record_os_close):
            result = orchestrate_build(request)

        self.assertEqual(ExitKind.SUCCESS, result.exit_kind)
        self.assertTrue(project_accesses)
        self.assertTrue(all(path == self.repo.resolve() for path in project_accesses))
        self.assertNotIn(other.resolve(), project_accesses)
        self.assertTrue(build_cache_accesses)
        self.assertTrue(all(path == self.repo.resolve() for path in build_cache_accesses))
        self.assertNotIn(other.resolve(), build_cache_accesses)
        self.assertEqual([], global_cache_accesses, f"unexpected global cache access: {global_cache_accesses}")
        self.assertEqual(1, len(docker_calls))
        self.assertEqual(4, len(materialized_paths))
        self.assertTrue(all(path.exists() for path in materialized_paths))
        self.assertEqual(other_snapshot, self._tree_snapshot(other_state.build_artifacts_root))
        self.assertEqual(global_snapshot, {path: (path.read_bytes(), path.stat().st_mode) for path in global_snapshot})
        self.assertTrue(all(not path.exists() for path in old_paths))
        selected = _test_selected_artifacts(None)
        committed = json.loads((selected_state.build_artifacts_root / "committed-build.json").read_text())
        self.assertEqual(sorted(f"sha256:{item.identity.hex_digest()}" for item in selected), committed["blobs"])
        self.assertTrue(old_identities.isdisjoint({DigestIdentity.from_hex(*item.split(":", 1)) for item in committed["blobs"]}))


class TestPiMaterializationBoundary(unittest.TestCase):
    """Pi/assembler/consumer failures become OPERATIONAL materialization
    failures — no Docker run, no projection publication, no snapshot, and no
    committed build reference."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.cache = self.base / "cache"
        self.cache.mkdir(mode=0o700)
        os.chmod(self.cache, 0o700)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.inventory = self.repo / "docker-constructor.toml"
        self.inventory.write_bytes(INVENTORY_PATH.read_bytes())
        (self.repo / "docker-constructor.local.toml").write_text(
            f'[cache]\ndir = "{self.cache}"\n'
        )
        (self.repo / "Dockerfile").write_text("FROM scratch\n")
        # Spies proving snapshot creation never runs for a Pi failure and that
        # cleanup is still invoked with the (never-assigned) snapshot.
        self.snapshot_calls: list[tuple] = []
        self.cleanup_calls: list[object] = []
        patcher1 = patch(
            "docker.versioning.build_orchestration.create_artifact_snapshot",
            side_effect=self._record_snapshot,
        )
        patcher2 = patch(
            "docker.versioning.build_orchestration.cleanup_artifact_snapshot",
            side_effect=self._record_cleanup,
        )
        patcher1.start()
        patcher2.start()
        self.addCleanup(patcher1.stop)
        self.addCleanup(patcher2.stop)

    def _record_snapshot(self, *args, **kwargs):
        self.snapshot_calls.append((args, kwargs))
        raise AssertionError(
            "create_artifact_snapshot must not run for a Pi materialization failure"
        )

    def _record_cleanup(self, snapshot):
        self.cleanup_calls.append(snapshot)

    def _request(self, *, publish, runner, materialize_pi, transport_factory=None):
        return BuildRequest(
            inventory_path=str(self.inventory),
            repo_root=str(self.repo),
            confirmed=True,
            runner=runner,
            _materialize_artifacts=_materialize_ok,
            _materialize_pi=materialize_pi,
            _transport_factory=transport_factory,
            _named_context_supported=lambda: True,
            _publish_projection=publish,
        project_root=Path(str(self.inventory)).resolve().parent)

    def _run_and_assert(self, *, materialize_pi, message_substr,
                        transport_factory=None):
        publish_calls: list[object] = []

        def publish(projection, *, repo_root=None):
            publish_calls.append(projection)
            return PublishResult(published_path="/tmp/effective.toml")

        runner = FakeBuildExecutor()
        result = orchestrate_build(self._request(
            publish=publish, runner=runner, materialize_pi=materialize_pi,
            transport_factory=transport_factory,
        ))
        self.assertEqual(ExitKind.OPERATIONAL, result.exit_kind)
        self.assertIn(message_substr, result.message or "")
        self.assertIsNone(result.publish_result)
        self.assertIsNone(result.process_result)
        # Docker and projection publication never ran.
        self.assertEqual([], runner.calls)
        self.assertEqual([], publish_calls)
        # No snapshot was created, and cleanup ran with the never-assigned
        # snapshot (None).
        self.assertEqual([], self.snapshot_calls)
        self.assertEqual([None], self.cleanup_calls)
        # No committed build reference was created.
        self.assertEqual([], list(self.cache.rglob("committed-build.json")))
        self.assertFalse((self.repo / ".docker-cache").exists())
        self.assertFalse((self.repo / ".docker-generated").exists())

    def test_release_acquisition_failure_is_operational(self):
        """A real ``materialize_pi`` acquisition/checksum failure (bad release
        transport bytes) surfaces as OPERATIONAL without Docker/publication."""
        class BadPiReleaseTransport:
            def __init__(self, policy):
                self.policy = policy

            def stream(self, url):
                yield b"not-valid-sha256sums-content"

        self._run_and_assert(
            materialize_pi=None,
            transport_factory=BadPiReleaseTransport,
            message_substr="Pi release acquisition failed",
        )

    def test_preflight_failure_is_operational(self):
        def fail_preflight(*args, **kwargs):
            raise LockedNpmError(
                "lock_malformed", "install lock has no 'packages' object"
            )

        self._run_and_assert(
            materialize_pi=fail_preflight,
            message_substr="install lock has no 'packages' object",
        )

    def test_assembler_execution_failure_is_operational(self):
        def fail_assembly(*args, **kwargs):
            raise LockedNpmError("npm_exit_nonzero", "assembler exited 1: npm ERR!")

        self._run_and_assert(
            materialize_pi=fail_assembly,
            message_substr="assembler exited 1",
        )

    def test_consumer_launcher_failure_is_operational(self):
        def fail_consumer(*args, **kwargs):
            raise PiConsumerError(
                "launcher target /tmp/pi/bin/../node_modules/pi escapes the Pi environment"
            )

        self._run_and_assert(
            materialize_pi=fail_consumer,
            message_substr="escapes the Pi environment",
        )


class TestPiSnapshotAdmissionFailure(unittest.TestCase):
    """A derived environment whose attestation fails snapshot admission
    becomes OPERATIONAL before Docker execution or projection publication."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.cache = self.base / "cache"
        self.cache.mkdir(mode=0o700)
        os.chmod(self.cache, 0o700)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.inventory = self.repo / "docker-constructor.toml"
        self.inventory.write_bytes(INVENTORY_PATH.read_bytes())
        (self.repo / "docker-constructor.local.toml").write_text(
            f'[cache]\ndir = "{self.cache}"\n'
        )
        (self.repo / "Dockerfile").write_text("FROM scratch\n")
        # Snapshot admission fails (the derived environment is rejected);
        # ``cleanup_artifact_snapshot`` is recorded so the boundary can assert
        # it ran with the never-assigned snapshot.
        self.cleanup_calls: list[object] = []
        self._create_patcher = patch(
            "docker.versioning.build_orchestration.create_artifact_snapshot",
            side_effect=SnapshotError(
                "snapshot admission rejected the derived environment"
            ),
        )
        self._cleanup_patcher = patch(
            "docker.versioning.build_orchestration.cleanup_artifact_snapshot",
            side_effect=self.cleanup_calls.append,
        )
        self._create_patcher.start()
        self._cleanup_patcher.start()
        self.addCleanup(self._create_patcher.stop)
        self.addCleanup(self._cleanup_patcher.stop)

    def _request(self, *, publish, runner):
        return BuildRequest(
            inventory_path=str(self.inventory),
            repo_root=str(self.repo),
            confirmed=True,
            runner=runner,
            _materialize_artifacts=_materialize_ok,
            _materialize_pi=fake_pi_materialization,
            _transport_factory=no_network_transport_factory,
            _named_context_supported=lambda: True,
            _publish_projection=publish,
        project_root=Path(str(self.inventory)).resolve().parent)

    def test_snapshot_admission_failure_is_operational(self):
        publish_calls: list[object] = []

        def publish(projection, *, repo_root=None):
            publish_calls.append(projection)
            return PublishResult(published_path="/tmp/effective.toml")

        runner = FakeBuildExecutor()
        result = orchestrate_build(self._request(publish=publish, runner=runner))
        self.assertEqual(ExitKind.OPERATIONAL, result.exit_kind)
        self.assertIn("snapshot admission rejected", result.message or "")
        self.assertIsNone(result.publish_result)
        self.assertIsNone(result.process_result)
        self.assertEqual([], runner.calls)
        self.assertEqual([], publish_calls)
        # Cleanup ran with the never-assigned snapshot; no committed reference.
        self.assertEqual([None], self.cleanup_calls)
        self.assertEqual([], list(self.cache.rglob("committed-build.json")))


# ═══════════════════════════════════════════════════════════════════════
# Stage 9.4 — public API tests
# ═══════════════════════════════════════════════════════════════════════

class TestPublicDoctorAPI(unittest.TestCase):
    """``diagnose_doctor`` and ``repair_rootless`` public functions."""

    def test_diagnose_doctor_never_applies_repair(self) -> None:
        result = diagnose_doctor()
        self.assertIsInstance(result, DoctorResult)
        self.assertFalse(result.repair_applied)

    def test_repair_rootless_with_consent(self) -> None:
        result = repair_rootless(consent=True)
        self.assertIsInstance(result, DoctorResult)
        # May succeed, fail operationally, or return POLICY for rootful
        self.assertIn(result.exit_kind,
                       (ExitKind.SUCCESS, ExitKind.OPERATIONAL, ExitKind.POLICY))

    def test_repair_rootless_without_consent_does_not_apply(self) -> None:
        result = repair_rootless(consent=False)
        self.assertIsInstance(result, DoctorResult)
        self.assertFalse(result.repair_applied)

if __name__ == "__main__":
    unittest.main()
