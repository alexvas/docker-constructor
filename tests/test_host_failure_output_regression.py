"""Phase 9 regressions for independent retained context and terminal layout."""
from __future__ import annotations

import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from docker.npm_environment.execution import _exit_failure, _timeout_error
from docker.versioning.build_orchestration import BuildResult, _host_failure_context
from docker.versioning.constructor_project import ConstructorProject
from docker.versioning.dispatch_types import ExitKind
from docker.versioning.host_progress import HostDiagnosticStream, HostPhase, HostStep
from docker.versioning.host_presentation import TerminalHostRenderer, format_failure_report


class TestRetainedFailureContext(unittest.TestCase):
    def _render(self, error) -> str:
        context = _host_failure_context(error)
        return format_failure_report(
            context.phase,
            context.step,
            summary=context.summary,
            tail=context.tail,
            tail_stream=context.tail_stream,
            timeout_retained_context=context.timeout_retained_context,
            logical_resource=context.logical_resource,
            hostnames=context.hostnames,
            exception_types=context.exception_types,
        )

    def test_exit_and_timeout_use_the_same_repeat_warning(self) -> None:
        for error, text in (
            (_exit_failure(1, stderr="npm ERR! retained", stdout=""), "npm ERR! retained"),
            (_timeout_error(1800, stderr="npm timeout retained"), "npm timeout retained"),
        ):
            with self.subTest(error=type(error).__name__):
                rendered = self._render(error)
                self.assertEqual(1, rendered.count(text))
                self.assertIn("Retained diagnostics (may repeat live output):", rendered)

    def test_complete_tail_is_independent_of_prior_live_output(self) -> None:
        tail = "progress 1\nprogress 2\nfinal error"
        rendered = format_failure_report(
            HostPhase.LOCKED_ASSEMBLY,
            HostStep.NPM_EXECUTION,
            summary="assembler exited 1",
            tail=tail,
            tail_stream=HostDiagnosticStream.STDERR,
        )
        for line in tail.splitlines():
            self.assertEqual(1, rendered.count(line))

    def test_more_than_delivery_history_limit_is_not_filtered(self) -> None:
        tail = "\n".join(f"line {index}" for index in range(600))
        rendered = format_failure_report(
            HostPhase.LOCKED_ASSEMBLY,
            HostStep.NPM_EXECUTION,
            tail=tail,
        )
        self.assertIn("line 0", rendered)
        self.assertIn("line 599", rendered)

    def test_empty_tail_is_authoritative(self) -> None:
        rendered = format_failure_report(
            HostPhase.LOCKED_ASSEMBLY,
            HostStep.NPM_EXECUTION,
            summary="assembler exited 1",
            tail="",
        )
        self.assertEqual(1, rendered.count("assembler exited 1"))
        self.assertNotIn("diagnostics", rendered.lower())

    def test_summary_resource_and_types_remain_structurally_separate(self) -> None:
        rendered = format_failure_report(
            HostPhase.LOCKED_ASSEMBLY,
            HostStep.NPM_EXECUTION,
            summary="safe summary",
            tail="safe tail",
            logical_resource="npm-assembler-0123456789ab",
            exception_types=("RuntimeError", "OSError"),
        )
        self.assertIn("safe summary", rendered)
        self.assertIn("logical asset: npm-assembler-0123456789ab", rendered)
        self.assertIn("error types: RuntimeError -> OSError", rendered)
        self.assertEqual(1, rendered.count("safe tail"))


class TestLiveFinalReportOwnership(unittest.TestCase):
    class Session:
        sink = staticmethod(lambda event: None)

        def __init__(self) -> None:
            self.reports: list[str] = []
            self.shutdown_calls = 0
            self.shutdown_result = True

        def submit_final_report(self, text: str) -> bool:
            self.reports.append(text)
            return True

        def shutdown(self) -> bool:
            self.shutdown_calls += 1
            return self.shutdown_result

    def test_live_actor_owns_final_report_and_generic_message_is_suppressed(self) -> None:
        from docker.constructor_cli import CommandRequest, _real_dispatcher

        error = _exit_failure(1, stderr="npm ERR! actor tail", stdout="")
        result = BuildResult(
            exit_kind=ExitKind.OPERATIONAL,
            message=str(error),
            host_failure=_host_failure_context(error),
        )
        request = CommandRequest(
            command="build",
            constructor_project=ConstructorProject(Path.cwd().resolve()),
            output="text",
            verbose=False,
            color="auto",
            command_args={"yes": True},
        )
        session = self.Session()
        with patch(
            "docker.versioning.build_orchestration.orchestrate_build",
            return_value=result,
        ):
            converted = _real_dispatcher(
                "build", request, _host_presentation_factory=lambda policy: session
            )
        self.assertIn("npm ERR! actor tail", converted.message or "")
        self.assertTrue(converted.message_owned_by_presentation)
        self.assertEqual(1, len(session.reports))
        self.assertIn("npm ERR! actor tail", session.reports[0])
        self.assertGreaterEqual(session.shutdown_calls, 1)

    def test_failed_live_report_admission_is_not_retried_by_generic_writer(self) -> None:
        from docker.constructor_cli import CommandRequest, _real_dispatcher

        error = _exit_failure(1, stderr="not retried", stdout="")
        result = BuildResult(
            exit_kind=ExitKind.OPERATIONAL,
            message=str(error),
            host_failure=_host_failure_context(error),
        )
        request = CommandRequest(
            command="build",
            constructor_project=ConstructorProject(Path.cwd().resolve()),
            output="text",
            verbose=False,
            color="auto",
            command_args={"yes": True},
        )
        session = self.Session()
        session.submit_final_report = lambda text: False  # type: ignore[method-assign]
        with patch(
            "docker.versioning.build_orchestration.orchestrate_build",
            return_value=result,
        ):
            converted = _real_dispatcher(
                "build", request, _host_presentation_factory=lambda policy: session
            )
        self.assertIn("not retried", converted.message or "")
        self.assertTrue(converted.message_owned_by_presentation)

        from docker.constructor_cli import _render

        stdout, stderr = _render(
            "build",
            converted,
            fmt="text",
            color="never",
            verbose=False,
            stdout_is_tty=False,
            stderr_is_tty=False,
        )
        self.assertEqual("", stdout)
        self.assertEqual("", stderr)

    def test_renderer_failure_keeps_actor_ownership_and_report(self) -> None:
        from docker.constructor_cli import CommandRequest, _real_dispatcher

        error = _exit_failure(1, stderr="renderer failure tail", stdout="")
        request = CommandRequest(
            command="build",
            constructor_project=ConstructorProject(Path.cwd().resolve()),
            output="text",
            verbose=False,
            color="never",
            command_args={"yes": True},
        )
        session = self.Session()
        session.shutdown_result = False
        with patch(
            "docker.versioning.build_orchestration.orchestrate_build",
            return_value=BuildResult(
                exit_kind=ExitKind.OPERATIONAL,
                message=str(error),
                host_failure=_host_failure_context(error),
            ),
        ):
            converted = _real_dispatcher(
                "build", request, _host_presentation_factory=lambda policy: session
            )
        self.assertTrue(converted.message_owned_by_presentation)
        self.assertIn("renderer failure tail", converted.message or "")
        self.assertEqual(1, len(session.reports))

    def test_json_retains_one_structured_failure_document(self) -> None:
        from docker.constructor_cli import CommandRequest, _real_dispatcher, _render

        error = _exit_failure(1, stderr="json retained tail", stdout="")
        result = BuildResult(
            exit_kind=ExitKind.OPERATIONAL,
            message=str(error),
            host_failure=_host_failure_context(error),
        )
        request = CommandRequest(
            command="build",
            constructor_project=ConstructorProject(Path.cwd().resolve()),
            output="json",
            verbose=False,
            color="never",
            command_args={"yes": True},
        )
        with patch(
            "docker.versioning.build_orchestration.orchestrate_build",
            return_value=result,
        ):
            converted = _real_dispatcher("build", request)
        self.assertFalse(converted.message_owned_by_presentation)
        self.assertIn("json retained tail", converted.message or "")
        self.assertIsInstance(converted.data, dict)
        self.assertIn("host_failure", converted.data)
        stdout, stderr = _render(
            "build", converted, fmt="json", color="never", verbose=False,
            stdout_is_tty=False, stderr_is_tty=False,
        )
        payload = json.loads(stdout)
        self.assertEqual("json retained tail", payload["data"]["host_failure"]["tail"])
        self.assertEqual("", stderr)


class TestNarrowTerminalRendering(unittest.TestCase):
    def test_transient_is_clipped_but_durable_text_is_complete(self) -> None:
        stream = io.StringIO()
        renderer = TerminalHostRenderer(stream, terminal_width=lambda: 8)
        renderer.set_status("wide界status")
        transient = stream.getvalue().split("\r")[-1]
        self.assertNotIn("status", transient)

        renderer.durable("wide界status")
        self.assertIn("wide界status\n", stream.getvalue())

    def test_finalization_clears_transient_before_durable_write(self) -> None:
        stream = io.StringIO()
        renderer = TerminalHostRenderer(stream, terminal_width=lambda: 12)
        renderer.set_status("123456789012345")
        renderer.finalize_diagnostic("complete diagnostic", restore_status=None)
        output = stream.getvalue()
        self.assertIn("\r\x1b[K", output)
        self.assertTrue(output.endswith("complete diagnostic\n"))


if __name__ == "__main__":
    unittest.main()
