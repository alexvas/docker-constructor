"""Phase 5 host-materialization event and output-mode contracts."""
from __future__ import annotations

import dataclasses
import io
import unittest
from unittest.mock import patch

from docker import constructor_cli
from docker.versioning.host_progress import (
    GuardedHostEventSink,
    HostDiagnosticEvent,
    HostDiagnosticStream,
    HostPhase,
    HostPhaseEvent,
    HostPhaseState,
)


class TestHostEventProtocol(unittest.TestCase):
    def test_events_are_frozen_and_use_only_fixed_members(self):
        phase = HostPhaseEvent(HostPhase.RELEASE_ACQUISITION, HostPhaseState.STARTED)
        diagnostic = HostDiagnosticEvent(
            HostPhase.LOCKED_ASSEMBLY, HostDiagnosticStream.STDOUT, "safe"
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            phase.state = HostPhaseState.FAILED  # type: ignore[misc]
        with self.assertRaises(TypeError):
            HostPhaseEvent("dynamic", HostPhaseState.STARTED)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            HostDiagnosticEvent(HostPhase.LOCKED_ASSEMBLY, "combined", "safe")  # type: ignore[arg-type]
        self.assertFalse(hasattr(phase, "exception"))
        self.assertEqual("safe", diagnostic.text)

    def test_raising_sink_is_disabled_and_never_escapes(self):
        calls = []

        def raising(event):
            calls.append(event)
            raise RuntimeError("presentation failed")

        sink = GuardedHostEventSink(raising)
        sink(HostPhaseEvent(HostPhase.RELEASE_ACQUISITION, HostPhaseState.STARTED))
        sink(HostPhaseEvent(HostPhase.RELEASE_ACQUISITION, HostPhaseState.SUCCEEDED))
        self.assertEqual(1, len(calls))


class TestHostEventFacade(unittest.TestCase):
    def test_renderer_uses_stderr_and_phase_stream_tags(self):
        output = io.StringIO()
        renderer = constructor_cli._HostEventRenderer(output)
        renderer(HostPhaseEvent(HostPhase.LOCKED_ASSEMBLY, HostPhaseState.STARTED))
        renderer(HostDiagnosticEvent(
            HostPhase.LOCKED_ASSEMBLY, HostDiagnosticStream.STDERR, "warning"
        ))
        rendered = output.getvalue()
        self.assertIn("locked assembly: started", rendered)
        self.assertIn("locked_assembly [stderr]: warning", rendered)

    def test_dispatcher_override_does_not_construct_a_host_renderer(self):
        cases = (
            (["--output", "json", "build", "--dry-run"], True, 0),
            (["--output", "text", "build", "--dry-run"], True, 0),
            (["--output", "text", "build", "--dry-run"], False, 0),
            (["--output", "text", "build", "-y"], True, 0),
        )
        for argv, tty, expected in cases:
            with self.subTest(argv=argv, tty=tty), patch.object(
                constructor_cli, "_HostEventRenderer"
            ) as renderer:
                constructor_cli.main(
                    argv,
                    dispatcher=lambda *_args, **_kwargs: constructor_cli.CommandResult(
                        exit_kind=constructor_cli.ExitKind.SUCCESS
                    ),
                    stdout_isatty=lambda: False,
                    stderr_isatty=lambda: tty,
                )
                self.assertEqual(expected, renderer.call_count)


if __name__ == "__main__":
    unittest.main()
