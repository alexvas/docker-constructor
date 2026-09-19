"""Regression coverage for Docker build output policies and rendering."""
from __future__ import annotations

import dataclasses
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch
from pathlib import Path

from tests.build_test_support import INVENTORY_PATH, fake_pi_materialization, fixture_directory, no_network_transport_factory

from docker.networking import BuildOutputPolicy, ProcessResult
from docker.versioning.build_snapshot import MaterializedSnapshot
from docker.versioning.build_orchestration import (
    BuildRequest, BuildResult, PublishResult, SubprocessBuildExecutor, orchestrate_build,
)
from docker.versioning.dispatch_types import ExitKind
from docker.versioning.host_progress import HostPhase, HostPhaseEvent, HostPhaseState
from docker.versioning.pi_assembly import PiAssemblyError


class RecordingExecutor:
    def __init__(self, result: ProcessResult) -> None:
        self.result, self.calls = result, []

    def run(self, argv: tuple[str, ...]) -> ProcessResult:
        self.calls.append(argv)
        return self.result


def _fixture_materialize(*_args, **_kwargs):
    root = fixture_directory("fixture-blobs-")
    return tuple(root / name for name in ("rustup.blob", "uv.blob", "rtk.blob", "fd.blob"))


def _fixture_snapshot(*_args, **_kwargs):
    path = fixture_directory("fixture-snapshot-")
    return MaterializedSnapshot(path, path / "manifest.json")


def setUpModule():
    global _snapshot_patcher
    _snapshot_patcher = patch("docker.versioning.build_orchestration.create_artifact_snapshot", side_effect=_fixture_snapshot)
    _snapshot_patcher.start()


def tearDownModule():
    _snapshot_patcher.stop()


def _request(policy: BuildOutputPolicy, runner: RecordingExecutor, *, progress: str = "auto") -> BuildRequest:
    return BuildRequest(
        inventory_path=str(INVENTORY_PATH), confirmed=True, progress=progress,
        output_policy=policy, runner=runner,
        _materialize_artifacts=_fixture_materialize,
        _materialize_pi=fake_pi_materialization,
        _transport_factory=no_network_transport_factory,
        _named_context_supported=lambda: True,
        _publish_projection=lambda *_args, **_kwargs: __import__(
            "docker.versioning.build_orchestration", fromlist=["PublishResult"]
        ).PublishResult("/tmp/effective.toml"),
    project_root=Path(str(INVENTORY_PATH)).resolve().parent)


class TestBuildOutputModel(unittest.TestCase):
    def test_request_supports_only_typed_policies_with_captured_default(self):
        self.assertIs(BuildRequest(inventory_path="x", project_root=Path("x").resolve().parent).output_policy,
                      BuildOutputPolicy.CAPTURED)
        self.assertEqual({BuildOutputPolicy.STREAMED, BuildOutputPolicy.CAPTURED},
                         set(BuildOutputPolicy))

    def test_request_rejects_non_policy_values(self):
        with self.assertRaisesRegex(ValueError, "BuildOutputPolicy"):
            BuildRequest(inventory_path="x", output_policy="invalid", project_root=Path("x").resolve().parent)  # type: ignore[arg-type]

    def test_process_result_explicitly_identifies_output_policy(self):
        result = ProcessResult(("docker", "build", "."), 0, "", "",
                               BuildOutputPolicy.STREAMED)
        self.assertIs(result.output_policy, BuildOutputPolicy.STREAMED)


class TestSubprocessBuildExecutor(unittest.TestCase):
    def test_constructor_rejects_invalid_output_policy(self):
        with self.assertRaisesRegex(ValueError, "BuildOutputPolicy"):
            SubprocessBuildExecutor("invalid")  # type: ignore[arg-type]

    @patch("docker.versioning.build_orchestration.subprocess.run")
    def test_streamed_execution_inherits_streams(self, run):
        run.return_value = subprocess.CompletedProcess(["docker"], 0)
        result = SubprocessBuildExecutor(BuildOutputPolicy.STREAMED).run(("docker", "build", "."))
        self.assertEqual(["docker", "build", "."], run.call_args.args[0])
        self.assertEqual({"shell": False, "text": True}, run.call_args.kwargs)
        self.assertEqual(("", "", BuildOutputPolicy.STREAMED),
                         (result.stdout, result.stderr, result.output_policy))

    @patch("docker.versioning.build_orchestration.subprocess.run")
    def test_captured_execution_preserves_diagnostics_and_return_code(self, run):
        run.return_value = subprocess.CompletedProcess(["docker"], 17, "out", "err")
        result = SubprocessBuildExecutor(BuildOutputPolicy.CAPTURED).run(("docker", "build", "."))
        self.assertEqual({"shell": False, "text": True, "capture_output": True}, run.call_args.kwargs)
        self.assertEqual((17, "out", "err", BuildOutputPolicy.CAPTURED),
                         (result.return_code, result.stdout, result.stderr, result.output_policy))

class TestBuildFailuresAndProgress(unittest.TestCase):
    def test_transition_is_terminal_before_docker_and_keeps_progress_argument(self):
        events = []

        class OrderingRunner(RecordingExecutor):
            def run(self, argv):
                self.events_at_run = tuple(events)
                return super().run(argv)

        runner = OrderingRunner(ProcessResult(
            ("docker",), 0, "", "", BuildOutputPolicy.STREAMED
        ))
        request = dataclasses.replace(
            _request(BuildOutputPolicy.STREAMED, runner, progress="plain"),
            event_sink=events.append,
        )
        result = orchestrate_build(request)
        transition = (
            HostPhaseEvent(HostPhase.DOCKER_TRANSITION, HostPhaseState.STARTED),
            HostPhaseEvent(HostPhase.DOCKER_TRANSITION, HostPhaseState.SUCCEEDED),
        )
        self.assertEqual(transition, runner.events_at_run[-2:])
        position = result.build_args.index("--progress")
        self.assertEqual("plain", result.build_args[position + 1])

    def test_host_materialization_failure_never_invokes_docker(self):
        runner = RecordingExecutor(ProcessResult(
            ("docker",), 0, "", "", BuildOutputPolicy.STREAMED
        ))

        def failed_materializer(*_args, **_kwargs):
            raise PiAssemblyError("host failure")

        request = dataclasses.replace(
            _request(BuildOutputPolicy.STREAMED, runner),
            _materialize_pi=failed_materializer,
        )
        result = orchestrate_build(request)
        self.assertIs(ExitKind.OPERATIONAL, result.exit_kind)
        self.assertEqual([], runner.calls)

    def test_streamed_failure_reports_code_without_replaying_stderr(self):
        runner = RecordingExecutor(ProcessResult(("docker",), 23, "", "shown once", BuildOutputPolicy.STREAMED))
        result = orchestrate_build(_request(BuildOutputPolicy.STREAMED, runner))
        self.assertEqual(ExitKind.OPERATIONAL, result.exit_kind)
        self.assertIn("23", result.message or "")
        self.assertNotIn("shown once", result.message or "")

    def test_captured_failure_retains_streams_and_return_code(self):
        runner = RecordingExecutor(ProcessResult(("docker",), 17, "stdout", "stderr", BuildOutputPolicy.CAPTURED))
        result = orchestrate_build(_request(BuildOutputPolicy.CAPTURED, runner))
        self.assertEqual(ExitKind.OPERATIONAL, result.exit_kind)
        self.assertEqual((17, "stdout", "stderr"),
                         (result.process_result.return_code, result.process_result.stdout, result.process_result.stderr))  # type: ignore[union-attr]

    def test_progress_vector_is_unchanged_in_each_policy(self):
        for policy in BuildOutputPolicy:
            for progress in ("auto", "plain", "tty"):
                runner = RecordingExecutor(ProcessResult(("docker",), 0, "", "", policy))
                request = _request(policy, runner, progress=progress)
                result = orchestrate_build(request)
                position = result.build_args.index("--progress")
                self.assertEqual(progress, result.build_args[position + 1])
                self.assertEqual(result.build_args, runner.calls[0])


class TestFacadeOutputPolicyAndRendering(unittest.TestCase):
    def _run(self, argv, *, stdout_isatty=False, stderr_isatty=False, result=None):
        from docker import constructor_cli as cli
        captured = []
        if result is None:
            result = BuildResult(ExitKind.SUCCESS, "image build completed",
                                 ("docker", "build", "."), "docker build .")
        def fake(request, *, inventory, local_inputs):
            captured.append(request)
            return result
        out, err = io.StringIO(), io.StringIO()
        with patch("docker.versioning.build_orchestration.orchestrate_build", side_effect=fake):
            with redirect_stdout(out), redirect_stderr(err):
                rc = cli.main(argv, stdout_isatty=lambda: stdout_isatty,
                              stderr_isatty=lambda: stderr_isatty,
                              _prompt_user=lambda _: True)
        return rc, out.getvalue(), err.getvalue(), captured[0]

    def test_text_selects_streaming_regardless_of_tty(self):
        for tty in (False, True):
            _, _, _, request = self._run(["build", "-y"], stdout_isatty=tty, stderr_isatty=not tty)
            self.assertIs(BuildOutputPolicy.STREAMED, request.output_policy)

    def test_json_selects_capture_and_is_one_document_without_docker_output(self):
        result = BuildResult(ExitKind.SUCCESS, "image build completed", ("docker", "build", "."), "docker build .",
                             ProcessResult(("docker",), 0, "docker-out", "docker-err", BuildOutputPolicy.CAPTURED))
        _, out, err, request = self._run(["--output", "json", "build", "-y"], result=result)
        self.assertIs(BuildOutputPolicy.CAPTURED, request.output_policy)
        self.assertEqual("", err)
        payload = json.loads(out)
        self.assertEqual(out.strip(), json.dumps(payload, indent=2, sort_keys=True))
        self.assertNotIn("docker-out", out[:out.find("{")])
        self.assertEqual(0, payload["data"]["return_code"])

    def test_json_failure_has_captured_details(self):
        result = BuildResult(ExitKind.OPERATIONAL, "stderr", ("docker",), "docker build",
                             ProcessResult(("docker",), 19, "stdout", "stderr", BuildOutputPolicy.CAPTURED))
        _, out, _, _ = self._run(["--output", "json", "build", "-y"], result=result)
        data = json.loads(out)["data"]
        self.assertEqual({"return_code": 19, "stdout": "stdout", "stderr": "stderr", "output_policy": "captured"},
                         {key: data[key] for key in ("return_code", "stdout", "stderr", "output_policy")})

    def test_text_success_is_concise_but_dry_run_and_verbose_keep_vector(self):
        _, out, _, _ = self._run(["build", "-y"])
        self.assertIn("image build completed", out)
        self.assertNotIn("docker build .", out)
        _, dry_out, _, _ = self._run(["build", "--dry-run"])
        self.assertIn("docker build .", dry_out)
        _, verbose_out, _, _ = self._run(["--verbose", "build", "-y"])
        self.assertIn("docker build .", verbose_out)

    def test_published_path_visibility_follows_output_mode(self):
        published_path = "/tmp/effective.toml"
        result = BuildResult(
            ExitKind.SUCCESS, "image build completed", ("docker", "build", "."),
            "docker build .", publish_result=PublishResult(published_path),
        )
        _, text_out, _, _ = self._run(["build", "-y"], result=result)
        self.assertNotIn("published_path", text_out)
        self.assertNotIn(published_path, text_out)
        self.assertEqual(published_path, result.publish_result.published_path)  # type: ignore[union-attr]

        _, verbose_out, _, _ = self._run(["--verbose", "build", "-y"], result=result)
        self.assertIn("published_path", verbose_out)
        self.assertIn(published_path, verbose_out)

        _, json_out, _, _ = self._run(["--output", "json", "build", "-y"], result=result)
        self.assertEqual(published_path, json.loads(json_out)["data"]["published_path"])

