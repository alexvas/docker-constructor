"""Phase 6 -- single mode-selected retained tail and channel isolation.

These tests bind the Phase 6 deliverables of
``add-configurable-network-url-display``:

* exactly one bounded retained tail per stream, whose representation is
  selected by the assembler request's ``network_url_display`` before stream
  capture -- URL-free sanitized text for ``redacted``, safely rendered
  normalized hostname/path text for ``host-path`` (with ``<redacted-path>``
  fallback), and terminal-safe source text for ``exact``;
* no non-selected representation retained in any mode, with or without an
  external SDK sink, while SDK events stay transient, redacted, URL-free, and
  path-free;
* the selected retained buffer is what text failure context and JSON
  ``host_failure.tail`` render, once, under the existing retained-context
  label;
* the selector is excluded from semantic execution and identity paths, so
  three otherwise equivalent assembler requests differ only in local
  diagnostics.

No test touches a real Docker daemon, socket, or network.
"""

from __future__ import annotations

import collections
import functools
import io
import json
import os
import signal
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from docker.constructor_cli import CommandRequest, _real_dispatcher, _render
from docker.npm_environment import (
    AssemblyTimeoutError,
    LockedNpmError,
    ProcessResult,
    assemble_environment,
    publication,
    verify_output,
)
from docker.npm_environment.execution import (
    DockerRunExecutor,
    _exit_failure,
    _timeout_error,
    default_stream_factory,
    default_tail_projector,
)
import docker.npm_environment.streaming as _streaming_module
from docker.npm_environment.streaming import (
    TAIL_BYTES,
    TRUNCATION_NOTICE,
    StreamReaderFailure,
    StreamingCapture,
    collect_streams,
)

#: Resolved defensively so the behavioral regressions in this module still
#: import and fail on assertions against a pre-change implementation rather
#: than failing at import time.
RETAINED_CAPTURE_ATTR = getattr(
    _streaming_module, "RETAINED_CAPTURE_ATTR", "retained_capture"
)
from docker.versioning.build_orchestration import (
    BuildResult,
    _host_failure_context,
)
from docker.versioning.constructor_project import ConstructorProject
from docker.versioning.diagnostic_projection import REDACTED_PATH_MARKER
from docker.versioning.dispatch_types import ExitKind
from docker.versioning.host_presentation import (
    HostPresentationMode,
    HostPresentationSession,
    PresentationPlan,
    PresentationSelection,
    WORKER_JOIN_SECONDS,
    format_failure_report,
)
from docker.versioning.model import NetworkUrlDisplay
from docker.versioning.npm_diagnostic_stream import (
    NpmDiagnosticStream,
    make_stream_factory,
    project_tail,
)

from tests.test_npm_environment_acceptance import (
    PopulatingExecutor,
    _assembler,
    _lock,
    _tree_spec,
    _validated,
)
from tests.test_npm_environment_phase3_deadline import (
    _DeadlineProc,
    _assert_no_workers,
)

RESEARCH_LINE = (
    "npm http fetch GET 200 "
    "https://registry.npmjs.org/npm-http-research-fixture/-/"
    "npm-http-research-fixture-1.0.0.tgz 15ms (cache miss)"
)
RESEARCH_BYTES = (RESEARCH_LINE + "\n").encode()

REDACTED_TAIL = "npm http fetch GET 200 <redacted> 15ms (cache miss)\n"
HOST_PATH_TAIL = (
    "npm http fetch GET 200 "
    "registry.npmjs.org/npm-http-research-fixture/-/"
    "npm-http-research-fixture-1.0.0.tgz 15ms (cache miss)\n"
)
EXACT_TAIL = RESEARCH_LINE + "\n"

SECRET = "caller-secret-token"
DISCLOSURE_LINE = (
    f"fetch https://user:pw@registry.npmjs.org/{SECRET}/pkg.tgz?q=1#frag "
    "proxy=https://proxy.internal:8080/path\n"
)
DISCLOSURE_BYTES = DISCLOSURE_LINE.encode()

EXPECTED = {
    NetworkUrlDisplay.REDACTED: REDACTED_TAIL,
    NetworkUrlDisplay.HOST_PATH: HOST_PATH_TAIL,
    NetworkUrlDisplay.EXACT: EXACT_TAIL,
}


class _Reader:
    def __init__(self, data: bytes):
        self._data = data

    def __call__(self, _max_bytes: int) -> bytes:
        data, self._data = self._data, b""
        return data


def _capture(parts, *, mode, secrets=(), sink=None, tail_bytes=TAIL_BYTES):
    return collect_streams(
        stdout_read=_Reader(b"".join(parts)),
        stderr_read=_Reader(b""),
        secrets=secrets,
        sink=sink,
        stream_factory=make_stream_factory(
            secrets, network_url_display=mode
        ),
        tail_projector=functools.partial(
            project_tail, network_url_display=mode
        ),
        network_url_display=mode,
    )


class _ServeOnceThenBlock:
    """Reader that serves *data* once, then blocks until released at EOF."""

    def __init__(
        self,
        data: bytes,
        released: threading.Event,
        served: threading.Event | None = None,
    ):
        self._data = data
        self._released = released
        self._served_event = served
        self._served = False

    def __call__(self, _max_bytes: int) -> bytes:
        if not self._served:
            self._served = True
            if self._served_event is not None:
                self._served_event.set()
            return self._data
        self._released.wait(5.0)
        return b""


class _FailAfterServe:
    """Reader that serves *data* once, then raises a real reader failure."""

    def __init__(self, data: bytes, message: str = "reader exploded"):
        self._data = data
        self._message = message

    def __call__(self, _max_bytes: int) -> bytes:
        if self._data:
            data, self._data = self._data, b""
            return data
        raise OSError(self._message)


def _stream_kwargs(mode, *, secrets=()):
    return {
        "stream_factory": make_stream_factory(
            secrets, network_url_display=mode
        ),
        "tail_projector": functools.partial(
            project_tail, network_url_display=mode
        ),
    }


def _reader_failure_error(
    mode, *, parts=(RESEARCH_BYTES,), secrets=(), sink=None
):
    """Run the real collector to a reader failure and return its exception.

    Exercises the production failure path after diagnostics have been
    captured, rather than constructing a :class:`StreamReaderFailure` by
    hand.
    """
    try:
        collect_streams(
            stdout_read=_FailAfterServe(b"".join(parts)),
            stderr_read=_Reader(b""),
            secrets=secrets,
            sink=sink,
            network_url_display=mode,
            **_stream_kwargs(mode, secrets=secrets),
        )
    except StreamReaderFailure as exc:
        return exc
    raise AssertionError("expected StreamReaderFailure")


def _interruption_error(mode, *, parts=(RESEARCH_BYTES,)):
    """Run the real collector to a control-flow interruption and return it.

    SIGINT is delivered to the coordinating thread after the reader has
    served one diagnostic, so the interruption unwinds through the
    production ``collect_streams`` path and the resulting exception carries
    the selected retained capture.
    """
    released = threading.Event()
    served = threading.Event()
    stdout = _ServeOnceThenBlock(b"".join(parts), released, served)

    def stderr_read(_max_bytes: int) -> bytes:
        released.wait(5.0)
        return b""

    def on_interruption() -> None:
        released.set()

    def interrupt() -> None:
        if not served.wait(5.0):
            return
        time.sleep(0.05)
        signal.pthread_kill(threading.main_thread().ident, signal.SIGINT)

    timer = threading.Thread(target=interrupt, daemon=True)
    timer.start()
    try:
        try:
            collect_streams(
                stdout_read=stdout,
                stderr_read=stderr_read,
                on_interruption=on_interruption,
                network_url_display=mode,
                **_stream_kwargs(mode),
            )
        except KeyboardInterrupt as exc:
            return exc
        raise AssertionError("expected KeyboardInterrupt")
    finally:
        timer.join(5.0)


def _render_json_failure(testcase, error):
    """Render one failure through the facade JSON channel (unit helper).

    This exercises the facade's JSON conversion with a hand-built result, so
    it is direct renderer coverage only.  End-to-end interruption reporting is
    proven separately by :class:`TestInterruptionEndToEnd`, which drives the
    real orchestration/CLI boundary.

    Asserts a single valid document and that no live presentation sink was
    installed on the build request.
    """
    context = _host_failure_context(error)
    result = BuildResult(
        exit_kind=ExitKind.OPERATIONAL,
        message=str(error),
        host_failure=context,
    )
    request = CommandRequest(
        command="build",
        constructor_project=ConstructorProject(Path.cwd().resolve()),
        output="json",
        verbose=False,
        color="never",
        command_args={"yes": True},
    )
    with mock.patch(
        "docker.versioning.build_orchestration.orchestrate_build",
        return_value=result,
    ) as orchestrate:
        converted = _real_dispatcher("build", request)
    build_request = orchestrate.call_args.args[0]
    testcase.assertIsNone(
        build_request.event_sink,
        "JSON failure rendering must not install a live presentation sink",
    )
    stdout, stderr = _render(
        "build",
        converted,
        fmt="json",
        color="never",
        verbose=False,
        stdout_is_tty=False,
        stderr_is_tty=False,
    )
    testcase.assertEqual("", stderr)
    return context, json.loads(stdout)


def _text_failure(testcase, error, mode):
    """Render one failure through the text channel and return its context."""
    context = _host_failure_context(error)
    report = format_failure_report(
        context.phase,
        context.step,
        summary=context.summary,
        tail=context.tail,
        tail_stream=context.tail_stream,
        timeout_retained_context=context.timeout_retained_context,
        logical_resource=context.logical_resource,
        hostnames=context.hostnames,
        network_url_display=mode,
        exception_types=context.exception_types,
    )
    return context, report


class TestSelectedRetainedTail(unittest.TestCase):
    """Tasks 6.1--6.3: one mode-selected representation is retained."""

    def _tail(self, mode, *, parts=(RESEARCH_BYTES,), secrets=()):
        stream = NpmDiagnosticStream(
            "stdout", secrets, network_url_display=mode
        )
        for part in parts:
            stream.feed_bytes(part)
        stream.finish()
        return stream

    def test_redacted_tail_is_ordered_url_free_sanitized_text(self):
        tail = self._tail(NetworkUrlDisplay.REDACTED).tail()
        self.assertEqual(REDACTED_TAIL, tail)
        # Neither source-safe nor host-path representation is retained.
        self.assertNotIn("https://", tail)
        self.assertNotIn("registry.npmjs.org", tail)
        self.assertIn("<redacted>", tail)

    def test_host_path_tail_renders_safe_host_path_with_fallback(self):
        stream = self._tail(
            NetworkUrlDisplay.HOST_PATH,
            parts=[(RESEARCH_LINE + "\n").encode(), DISCLOSURE_BYTES],
            secrets=(SECRET,),
        )
        tail = stream.tail()
        self.assertIn(HOST_PATH_TAIL.strip(), tail)
        # The unsafe (secret-bearing) path falls back to the fixed marker.
        self.assertIn(
            f"registry.npmjs.org{REDACTED_PATH_MARKER}", tail
        )
        # No duplicate redacted tail and no scheme/userinfo/port/query.
        self.assertNotIn("<redacted>", tail)
        self.assertNotIn("https://", tail)
        self.assertNotIn("user:pw@", tail)
        self.assertNotIn(":8080", tail)
        self.assertNotIn("?q=1", tail)

    def test_exact_tail_preserves_bounded_terminal_safe_source(self):
        stream = self._tail(
            NetworkUrlDisplay.EXACT,
            parts=[DISCLOSURE_BYTES, b"npm warn \x1b[31mred\x1b[0m\n"],
            secrets=(SECRET,),
        )
        tail = stream.tail()
        # Source URLs, credentials, proxy detail, query, fragment, and caller
        # secrets are preserved exactly...
        self.assertIn(f"https://user:pw@registry.npmjs.org/{SECRET}/pkg.tgz?q=1#frag", tail)
        self.assertIn("proxy=https://proxy.internal:8080/path", tail)
        # ...while terminal controls stay inert and no other tail is retained.
        self.assertNotIn("\x1b", tail)
        self.assertIn(r"\x1b[31m", tail)
        self.assertNotIn("<redacted>", tail)
        self.assertNotIn(REDACTED_PATH_MARKER, tail)

    def test_each_mode_defaults_to_redacted_when_unspecified(self):
        stream = NpmDiagnosticStream("stdout")
        stream.feed_bytes(RESEARCH_BYTES)
        stream.finish()
        self.assertEqual(REDACTED_TAIL, stream.tail())


class TestSingleRepresentationMatrix(unittest.TestCase):
    """Tasks 6.4--6.5: one local representation, with and without a sink."""

    def test_without_sdk_sink_exactly_one_mode_representation_is_stored(self):
        for mode, expected in EXPECTED.items():
            with self.subTest(mode=mode):
                capture = _capture([RESEARCH_BYTES], mode=mode)
                self.assertEqual(expected, capture.stdout_tail)
                self.assertEqual("", capture.stderr_tail)

    def test_with_sdk_sink_local_tail_is_selected_and_events_are_safe(self):
        for mode, expected in EXPECTED.items():
            with self.subTest(mode=mode):
                received = []
                capture = _capture(
                    [DISCLOSURE_BYTES + RESEARCH_BYTES],
                    mode=mode,
                    secrets=(SECRET,),
                    sink=received.append,
                )
                # Exactly the one selected local representation is retained.
                self.assertIn(expected.strip(), capture.stdout_tail)
                if mode is NetworkUrlDisplay.REDACTED:
                    self.assertNotIn("registry.npmjs.org", capture.stdout_tail)
                elif mode is NetworkUrlDisplay.HOST_PATH:
                    self.assertNotIn("<redacted>", capture.stdout_tail)
                else:
                    self.assertNotIn("<redacted>", capture.stdout_tail)
                # Every SDK event is transient, redacted, URL-free, path-free.
                self.assertTrue(received)
                for chunk in received:
                    self.assertNotIn("https://", chunk.text)
                    self.assertNotIn(SECRET, chunk.text)
                    self.assertNotIn(REDACTED_PATH_MARKER, chunk.text)
                    self.assertNotIn("user:pw@", chunk.text)


class TestFailureContextChannels(unittest.TestCase):
    """Tasks 6.6--6.7: text and JSON failure output use the selected tail."""

    def _report(self, mode, capture, *, timeout=False):
        if timeout:
            error = _timeout_error(
                1.0,
                stdout=capture.stdout_tail,
                stderr=capture.stderr_tail,
                truncation_notice=capture.truncation_notice,
            )
        else:
            error = _exit_failure(
                1,
                stderr=capture.stderr_tail,
                stdout=capture.stdout_tail,
                truncation_notice=capture.truncation_notice,
            )
        context = _host_failure_context(error)
        self.assertTrue(
            context.timeout_retained_context if timeout else True
        )
        report = format_failure_report(
            context.phase,
            context.step,
            summary=context.summary,
            tail=context.tail,
            tail_stream=context.tail_stream,
            timeout_retained_context=context.timeout_retained_context,
            logical_resource=context.logical_resource,
            hostnames=context.hostnames,
            network_url_display=mode,
            exception_types=context.exception_types,
        )
        return context, report

    def test_text_failure_context_renders_selected_tail_once(self):
        for mode, expected in EXPECTED.items():
            with self.subTest(mode=mode):
                capture = _capture([RESEARCH_BYTES], mode=mode)
                context, report = self._report(mode, capture)
                self.assertEqual(expected.strip(), context.tail)
                self.assertIn(expected.strip(), report)
                self.assertEqual(
                    1, report.count("Retained diagnostics (may repeat live output):")
                )

    def test_text_failure_context_is_mode_selected_after_live_output(self):
        for mode, expected in EXPECTED.items():
            with self.subTest(mode=mode):
                received = []
                capture = _capture(
                    [RESEARCH_BYTES], mode=mode, sink=received.append
                )
                self.assertTrue(received)
                _context, report = self._report(mode, capture)
                self.assertIn(expected.strip(), report)
                # The retained tail is never reconciled with live history.
                self.assertEqual(
                    1, report.count("Retained diagnostics (may repeat live output):")
                )

    def test_empty_tail_produces_no_diagnostic_section(self):
        for mode in NetworkUrlDisplay:
            with self.subTest(mode=mode):
                capture = _capture([b""], mode=mode)
                _context, report = self._report(mode, capture)
                self.assertNotIn("Retained diagnostics", report)

    def test_truncated_tail_is_bounded_and_mode_selected(self):
        for mode in NetworkUrlDisplay:
            with self.subTest(mode=mode):
                oversized = (
                    "npm warn "
                    + "x" * (TAIL_BYTES * 2)
                    + " https://registry.npmjs.org/pkg.tgz http://proxy.internal/x\n"
                )
                capture = _capture([oversized.encode()], mode=mode)
                self.assertLessEqual(
                    len(capture.stdout_tail.encode("utf-8")), TAIL_BYTES
                )
                _context, report = self._report(mode, capture)
                self.assertEqual(
                    1, report.count("Retained diagnostics (may repeat live output):")
                )

    def test_timeout_retained_context_uses_the_selected_tail(self):
        for mode, expected in EXPECTED.items():
            with self.subTest(mode=mode):
                capture = _capture([RESEARCH_BYTES], mode=mode)
                context, report = self._report(mode, capture, timeout=True)
                self.assertTrue(context.timeout_retained_context)
                self.assertIn(expected.strip(), report)

    def test_reader_failure_detail_is_mode_selected_and_terminal_safe(self):
        class _Failing:
            def __init__(self, message: str):
                self._message = message

            def __call__(self, _max_bytes: int) -> bytes:
                raise OSError(self._message)

        detail = "boom " + DISCLOSURE_LINE.strip()
        for mode in NetworkUrlDisplay:
            with self.subTest(mode=mode):
                with self.assertRaises(StreamReaderFailure) as raised:
                    collect_streams(
                        stdout_read=_Failing(detail),
                        stderr_read=_Reader(b""),
                        secrets=(SECRET,),
                        stream_factory=make_stream_factory(
                            (SECRET,), network_url_display=mode
                        ),
                        tail_projector=functools.partial(
                            project_tail, network_url_display=mode
                        ),
                        network_url_display=mode,
                    )
                text = str(raised.exception)
                self.assertNotIn("\x1b", text)
                if mode is NetworkUrlDisplay.REDACTED:
                    self.assertNotIn("https://", text)
                    self.assertNotIn(SECRET, text)
                elif mode is NetworkUrlDisplay.HOST_PATH:
                    self.assertNotIn("https://", text)
                    self.assertIn("registry.npmjs.org", text)
                else:
                    self.assertIn(SECRET, text)

    def test_aborted_finalization_retains_the_selected_representation(self):
        def _aborted(mode: NetworkUrlDisplay) -> str:
            abort = threading.Event()
            stream = NpmDiagnosticStream(
                "stdout", (SECRET,), network_url_display=mode
            )
            stream.feed_bytes(b"npm status https://registry.npmjs.org/" + SECRET.encode())
            abort.set()
            stream.finish(abort=True)
            return stream.tail()

        redacted = _aborted(NetworkUrlDisplay.REDACTED)
        self.assertNotIn("registry.npmjs.org", redacted)
        self.assertNotIn(SECRET, redacted)
        exact = _aborted(NetworkUrlDisplay.EXACT)
        self.assertIn("https://registry.npmjs.org/" + SECRET, exact)

    def test_cancelled_capture_tail_is_mode_selected_and_fails_closed(self):
        from docker.versioning.diagnostic_projection import (
            INCOMPLETE_TOKEN_MARKER,
        )

        unterminated = b"npm status https://registry.npmjs.org/pkg.tgz"
        for mode in NetworkUrlDisplay:
            with self.subTest(mode=mode):
                abort = threading.Event()
                abort.set()
                capture = collect_streams(
                    stdout_read=_Reader(unterminated),
                    stderr_read=_Reader(b""),
                    stream_factory=make_stream_factory(
                        (), network_url_display=mode
                    ),
                    tail_projector=functools.partial(
                        project_tail, network_url_display=mode
                    ),
                    abort_event=abort,
                    network_url_display=mode,
                )
                if mode is NetworkUrlDisplay.EXACT:
                    self.assertEqual(
                        "npm status https://registry.npmjs.org/pkg.tgz",
                        capture.stdout_tail,
                    )
                else:
                    self.assertIn(
                        INCOMPLETE_TOKEN_MARKER, capture.stdout_tail
                    )
                    self.assertNotIn(
                        "registry.npmjs.org", capture.stdout_tail
                    )

    def test_json_failure_tail_is_mode_selected_and_one_document(self):
        for mode, expected in EXPECTED.items():
            with self.subTest(mode=mode):
                capture = _capture([RESEARCH_BYTES], mode=mode)
                error = _exit_failure(
                    1,
                    stderr=capture.stderr_tail,
                    stdout=capture.stdout_tail,
                    truncation_notice=capture.truncation_notice,
                )
                result = BuildResult(
                    exit_kind=ExitKind.OPERATIONAL,
                    message=str(error),
                    host_failure=_host_failure_context(error),
                )
                request = CommandRequest(
                    command="build",
                    constructor_project=ConstructorProject(
                        Path.cwd().resolve()
                    ),
                    output="json",
                    verbose=False,
                    color="never",
                    command_args={"yes": True},
                )
                with mock.patch(
                    "docker.versioning.build_orchestration.orchestrate_build",
                    return_value=result,
                ):
                    converted = _real_dispatcher("build", request)
                stdout, stderr = _render(
                    "build",
                    converted,
                    fmt="json",
                    color="never",
                    verbose=False,
                    stdout_is_tty=False,
                    stderr_is_tty=False,
                )
                self.assertEqual("", stderr)
                payload = json.loads(stdout)
                self.assertEqual(
                    expected.strip(), payload["data"]["host_failure"]["tail"]
                )


class TestFailureChannelMatrix(unittest.TestCase):
    """Tasks 6.6--6.7: the full failure-channel matrix, text and JSON.

    Every case exercises a real production path after diagnostics have been
    captured -- an actual :func:`collect_streams` reader failure or
    interruption, an actual ``_exit_failure``, or an actual
    ``_timeout_error`` built from a real capture -- never a hand-built
    exception.  Each case renders through both the text and JSON channels.
    """

    LABEL = "Retained diagnostics (may repeat live output):"

    def _assert_section(self, report, expected, *, present):
        if present:
            self.assertEqual(1, report.count(self.LABEL))
            self.assertIn(expected, report)
        else:
            self.assertNotIn(self.LABEL, report)

    def test_nonempty_renders_once_in_text_and_json(self):
        for mode, expected in EXPECTED.items():
            with self.subTest(mode=mode):
                capture = _capture([RESEARCH_BYTES], mode=mode)
                error = _exit_failure(
                    1,
                    stderr="",
                    stdout=capture.stdout_tail,
                    truncation_notice=capture.truncation_notice,
                )
                context, report = _text_failure(self, error, mode)
                self.assertEqual(expected.strip(), context.tail)
                self.assertLessEqual(
                    len(context.tail.encode("utf-8")), TAIL_BYTES
                )
                self.assertNotIn("\x1b", context.tail)
                self._assert_section(report, expected.strip(), present=True)
                json_context, payload = _render_json_failure(self, error)
                self.assertEqual(expected.strip(), json_context.tail)
                self.assertEqual(
                    expected.strip(),
                    payload["data"]["host_failure"]["tail"],
                )

    def test_empty_omits_the_section_in_text_and_json(self):
        for mode in NetworkUrlDisplay:
            with self.subTest(mode=mode):
                capture = _capture([b""], mode=mode)
                error = _exit_failure(1, stderr="", stdout=capture.stdout_tail)
                context, report = _text_failure(self, error, mode)
                self.assertEqual("", context.tail)
                self._assert_section(report, "", present=False)
                _context, payload = _render_json_failure(self, error)
                self.assertEqual(
                    "", payload["data"]["host_failure"]["tail"]
                )

    def test_truncated_tail_is_bounded_in_text_and_json(self):
        oversized = (
            "npm warn "
            + "x" * (TAIL_BYTES * 2)
            + " https://registry.npmjs.org/pkg.tgz\n"
        ).encode()
        for mode in NetworkUrlDisplay:
            with self.subTest(mode=mode):
                capture = _capture([oversized], mode=mode)
                self.assertLessEqual(
                    len(capture.stdout_tail.encode("utf-8")), TAIL_BYTES
                )
                error = _exit_failure(
                    1, stderr="", stdout=capture.stdout_tail
                )
                context, report = _text_failure(self, error, mode)
                self.assertLessEqual(
                    len(context.tail.encode("utf-8")), TAIL_BYTES
                )
                self._assert_section(
                    report, context.tail, present=bool(context.tail)
                )
                self.assertNotIn("\x1b", context.tail)
                _context, payload = _render_json_failure(self, error)
                json_tail = payload["data"]["host_failure"]["tail"]
                self.assertLessEqual(len(json_tail.encode("utf-8")), TAIL_BYTES)
                self.assertEqual(context.tail, json_tail)

    def test_timeout_uses_the_captured_tail_in_text_and_json(self):
        for mode, expected in EXPECTED.items():
            with self.subTest(mode=mode):
                capture = _capture([RESEARCH_BYTES], mode=mode)
                error = _timeout_error(
                    1.0,
                    stdout=capture.stdout_tail,
                    stderr=capture.stderr_tail,
                    truncation_notice=capture.truncation_notice,
                )
                context, report = _text_failure(self, error, mode)
                self.assertTrue(context.timeout_retained_context)
                self.assertEqual(expected.strip(), context.tail)
                self._assert_section(report, expected.strip(), present=True)
                _context, payload = _render_json_failure(self, error)
                self.assertEqual(
                    expected.strip(),
                    payload["data"]["host_failure"]["tail"],
                )
                self.assertTrue(
                    payload["data"]["host_failure"][
                        "timeout_retained_context"
                    ]
                )

    def test_reader_failure_carries_its_selected_tail(self):
        for mode, expected in EXPECTED.items():
            with self.subTest(mode=mode):
                error = _reader_failure_error(mode)
                self.assertEqual(
                    expected.strip(), getattr(error, "diagnostic_tail", "")
                )
                self.assertEqual("stdout", error.diagnostic_stream)
                self.assertIsInstance(
                    getattr(error, RETAINED_CAPTURE_ATTR, None),
                    StreamingCapture,
                )
                self.assertEqual(TRUNCATION_NOTICE, error.truncation_notice)
                context, report = _text_failure(self, error, mode)
                self.assertEqual(expected.strip(), context.tail)
                self.assertNotIn("\x1b", context.tail)
                self._assert_section(report, expected.strip(), present=True)
                _context, payload = _render_json_failure(self, error)
                self.assertEqual(
                    expected.strip(),
                    payload["data"]["host_failure"]["tail"],
                )

    @unittest.skipUnless(
        hasattr(signal, "pthread_kill"), "requires signal.pthread_kill"
    )
    def test_interruption_carries_its_selected_tail(self):
        for mode, expected in EXPECTED.items():
            with self.subTest(mode=mode):
                error = self._interruption_error(mode)
                self.assertIsInstance(error, KeyboardInterrupt)
                self.assertEqual(
                    expected.strip(), getattr(error, "diagnostic_tail", "")
                )
                self.assertIsInstance(
                    getattr(error, RETAINED_CAPTURE_ATTR, None),
                    StreamingCapture,
                )
                context, report = _text_failure(self, error, mode)
                self.assertEqual(expected.strip(), context.tail)
                self.assertNotIn("\x1b", context.tail)
                self._assert_section(report, expected.strip(), present=True)
                _context, payload = _render_json_failure(self, error)
                self.assertEqual(
                    expected.strip(),
                    payload["data"]["host_failure"]["tail"],
                )

    def test_already_shown_live_tail_repeats_once_in_both_channels(self):
        for mode, expected in EXPECTED.items():
            with self.subTest(mode=mode):
                received: list = []
                error = _reader_failure_error(mode, sink=received.append)
                self.assertTrue(received)
                context, report = _text_failure(self, error, mode)
                self.assertEqual(expected.strip(), context.tail)
                self._assert_section(report, expected.strip(), present=True)
                # Live SDK chunks stay transient, redacted, URL-free.
                for chunk in received:
                    self.assertNotIn("https://", chunk.text)
                _context, payload = _render_json_failure(self, error)
                self.assertEqual(
                    expected.strip(),
                    payload["data"]["host_failure"]["tail"],
                )

    def test_terminal_controls_stay_inert_in_every_mode(self):
        for mode in NetworkUrlDisplay:
            with self.subTest(mode=mode):
                error = _reader_failure_error(
                    mode, parts=(b"npm warn \x1b[31mred\x1b[0m\n",)
                )
                tail = getattr(error, "diagnostic_tail", "")
                self.assertNotIn("\x1b", tail)
                self.assertIn(r"\x1b[31m", tail)
                context, report = _text_failure(self, error, mode)
                self.assertNotIn("\x1b", context.tail)
                self.assertNotIn("\x1b", report)

    def _interruption_error(self, mode):
        return _interruption_error(mode)


class _ReaderFailureExecutor:
    """Streaming executor whose ``run_streaming`` raises a real failure."""

    def __init__(self, error: BaseException):
        self.calls: list[tuple[str, ...]] = []
        self._error = error

    def run_streaming(self, argv, **_kwargs):
        self.calls.append(tuple(argv))
        raise self._error

    def run(self, argv):
        self.calls.append(tuple(argv))
        if tuple(argv[:3]) == ("docker", "rm", "-f"):
            return ProcessResult(argv, 0, "", "")
        raise AssertionError(f"unexpected run: {argv}")


class TestAssemblerReaderFailurePropagation(unittest.TestCase):
    """Task 6.11: a reader failure keeps its selected tail through assembly."""

    def test_reader_failure_capture_reaches_structured_failure_context(self):
        tmp = tempfile.TemporaryDirectory(prefix="phase6-reader-failure-")
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        raw = _lock("consumer-a", "app-a", "shared")
        for mode, expected in EXPECTED.items():
            with self.subTest(mode=mode):
                reader_failure = _reader_failure_error(mode)
                executor = _ReaderFailureExecutor(reader_failure)
                cache_root = base / mode.value
                cache_root.mkdir()
                with self.assertRaises(LockedNpmError) as raised:
                    assemble_environment(
                        validated=_validated(raw, "app-a"),
                        assembler=_assembler(),
                        cache_root=cache_root,
                        executor=executor,
                        network_url_display=mode,
                    )
                error = raised.exception
                # The primary failure stays ``executor_failure``; only the
                # already-selected retained tail is preserved alongside it.
                self.assertEqual("executor_failure", error.reason)
                self.assertEqual(
                    expected.strip(), getattr(error, "diagnostic_tail", "")
                )
                context = _host_failure_context(error)
                self.assertEqual(expected.strip(), context.tail)
                _json_context, payload = _render_json_failure(self, error)
                self.assertEqual(
                    expected.strip(),
                    payload["data"]["host_failure"]["tail"],
                )
                self.assertTrue(
                    any(
                        tuple(call[:3]) == ("docker", "rm", "-f")
                        for call in executor.calls
                    )
                )


class TestConcurrentTimeoutReaderFailure(unittest.TestCase):
    """Regression: a fired deadline concurrent with a reader failure.

    The deadline stays primary, but the timeout must reuse the reader
    failure's already-selected retained tails instead of producing a
    tail-less error.
    """

    def test_fired_timeout_reuses_reader_failure_capture(self):
        import docker.npm_environment.execution as execution_module

        for mode, expected in EXPECTED.items():
            with self.subTest(mode=mode):
                reader_failure = _reader_failure_error(mode)
                proc = _DeadlineProc()

                def concurrent_collect(**_kwargs):
                    time.sleep(0.5)
                    raise reader_failure

                with mock.patch.object(
                    execution_module,
                    "collect_streams",
                    side_effect=concurrent_collect,
                ), mock.patch.object(
                    execution_module.subprocess,
                    "Popen",
                    return_value=proc,
                ):
                    with self.assertRaises(
                        AssemblyTimeoutError
                    ) as timed_out:
                        DockerRunExecutor().run_streaming(
                            ("docker", "run", "--rm", "alpine", "true"),
                            deadline_seconds=0.2,
                            grace_seconds=0.5,
                        )
                timeout = timed_out.exception
                self.assertEqual("assembly_timeout", timeout.reason)
                self.assertEqual(expected.strip(), timeout.diagnostic_tail)
                notes = "\n".join(getattr(timeout, "__notes__", ()))
                self.assertIn(
                    "also interrupted by StreamReaderFailure", notes
                )
                self.assertTrue(proc.terminated)
                self.assertTrue(proc.reaped)
                _assert_no_workers(self)


class _RecordingActivity:
    def __init__(self):
        self.steps: list[str] = []
        self.cache_reuses = 0
        self.events: list[tuple[str, str]] = []

    def step(self, name, *, container_name=None):
        self.steps.append(name)
        self.events.append(("start", name))
        activity = self

        class _Step:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc_info):
                activity.events.append(("end", name))
                return False

        return _Step()

    def cache_reuse(self):
        self.cache_reuses += 1
        self.events.append(("cache_reuse", ""))


class TestPresentationOnlyInvariance(unittest.TestCase):
    """Task 6.8: three equivalent requests differ only in local diagnostics."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="phase6-invariance-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.assembler = _assembler()
        self.raw = _lock("consumer-a", "app-a", "shared")

    def _run(self, mode, cache_root):
        executor = PopulatingExecutor(
            _tree_spec("consumer-a", "app-a", "shared"), b"marker", self.raw
        )
        activity = _RecordingActivity()
        result = assemble_environment(
            validated=_validated(self.raw, "app-a"),
            assembler=self.assembler,
            cache_root=cache_root,
            executor=executor,
            activity=activity,
            network_url_display=mode,
        )
        return result, executor, activity

    @staticmethod
    def _normalized_argv(calls, cache_root):
        """Return *calls* with the per-run host cache path removed.

        The three comparisons use distinct cache directories so each request
        actually executes; the only argv difference is that host path.  Any
        selector-derived difference would survive this normalization.
        """
        token = str(cache_root)
        return [
            tuple(part.replace(token, "<CACHE>") for part in call)
            for call in calls
        ]

    def test_three_modes_have_identical_semantics(self):
        runs = {}
        roots = {}
        for mode in NetworkUrlDisplay:
            cache_root = self.base / mode.value
            cache_root.mkdir()
            roots[mode] = cache_root
            runs[mode] = self._run(mode, cache_root)

        reference_mode = NetworkUrlDisplay.REDACTED
        reference = runs[reference_mode]
        for mode, (result, executor, activity) in runs.items():
            with self.subTest(mode=mode):
                self.assertEqual(
                    reference[0].input_identity.digest,
                    result.input_identity.digest,
                )
                self.assertEqual(
                    reference[0].output_identity, result.output_identity
                )
                self.assertEqual(reference[0].tree_digest, result.tree_digest)
                self.assertEqual(
                    reference[0].evidence_path.read_bytes(),
                    result.evidence_path.read_bytes(),
                )
                # npm/Docker argv is identical once the per-run host cache
                # path (the only intended difference) is normalized out.
                self.assertEqual(
                    self._normalized_argv(
                        reference[1].calls, roots[reference_mode]
                    ),
                    self._normalized_argv(executor.calls, roots[mode]),
                )
                # Lifecycle event production is identical.
                self.assertEqual(reference[2].events, activity.events)

    def test_all_modes_remain_independently_verifiable(self):
        for mode in NetworkUrlDisplay:
            with self.subTest(mode=mode):
                cache_root = self.base / f"verify-{mode.value}"
                cache_root.mkdir()
                result, _executor, _activity = self._run(mode, cache_root)
                namespace = publication.prepare_assembler_namespace(
                    cache_root, self.assembler.digest
                )
                self.assertIsNotNone(
                    verify_output(
                        namespace,
                        result.output_identity,
                        input_identity=result.input_identity,
                    )
                )

    def test_failure_primary_result_and_cleanup_are_selector_independent(self):
        outcomes = {}
        for mode in NetworkUrlDisplay:
            with self.subTest(mode=mode):
                cache_root = self.base / f"fail-{mode.value}"
                cache_root.mkdir()
                executor = _RaisingExecutor()
                with self.assertRaises(LockedNpmError) as raised:
                    assemble_environment(
                        validated=_validated(self.raw, "app-a"),
                        assembler=self.assembler,
                        cache_root=cache_root,
                        executor=executor,
                        network_url_display=mode,
                    )
                outcomes[mode] = (raised.exception.reason, raised.exception.detail)
                # The cleanup force-removal client is still invoked.
                self.assertTrue(
                    any(
                        tuple(call[:3]) == ("docker", "rm", "-f")
                        for call in executor.calls
                    )
                )
        self.assertEqual(1, len(set(outcomes.values())))

    def test_cache_lookup_and_reuse_ignore_the_selector(self):
        cache_root = self.base / "shared"
        cache_root.mkdir()
        first_result, first_executor, _first_activity = self._run(
            NetworkUrlDisplay.REDACTED, cache_root
        )
        self.assertEqual(1, len(first_executor.calls))
        for mode in (
            NetworkUrlDisplay.HOST_PATH,
            NetworkUrlDisplay.EXACT,
        ):
            with self.subTest(mode=mode):
                result, executor, activity = self._run(mode, cache_root)
                self.assertEqual([], executor.calls)
                self.assertEqual(1, activity.cache_reuses)
                self.assertEqual(
                    first_result.input_identity.digest,
                    result.input_identity.digest,
                )
                self.assertEqual(
                    first_result.output_identity, result.output_identity
                )

    def test_revealing_modes_do_not_change_selected_tail_semantics(self):
        # The three requests above are semantically identical; only the local
        # retained capture may differ, and each mode retains its own form.
        captures = {
            mode: _capture([RESEARCH_BYTES], mode=mode)
            for mode in NetworkUrlDisplay
        }
        self.assertEqual(REDACTED_TAIL, captures[NetworkUrlDisplay.REDACTED].stdout_tail)
        self.assertEqual(HOST_PATH_TAIL, captures[NetworkUrlDisplay.HOST_PATH].stdout_tail)
        self.assertEqual(EXACT_TAIL, captures[NetworkUrlDisplay.EXACT].stdout_tail)


class TestSingleBufferOwnership(unittest.TestCase):
    """Tasks 6.13--6.14: exactly one retained buffer per stream."""

    def _stream(self, mode, *, secrets=()):
        factory = make_stream_factory(secrets, network_url_display=mode)
        return factory("stdout")

    def test_only_one_retained_buffer_receives_payloads(self):
        for mode in NetworkUrlDisplay:
            with self.subTest(mode=mode):
                stream = self._stream(mode)
                # Exactly one attribute is a retained deque.
                deques = [
                    name
                    for name, value in vars(stream).items()
                    if isinstance(value, collections.deque)
                ]
                self.assertEqual(["_tail"], deques)
                # The alternate projector owns no retained tail buffer.
                alternate = stream._retained_projector
                if mode is NetworkUrlDisplay.HOST_PATH:
                    self.assertIsNotNone(alternate)
                    self.assertFalse(
                        any(
                            isinstance(value, collections.deque)
                            for value in vars(alternate).values()
                        )
                    )
                else:
                    self.assertIsNone(alternate)

    def test_source_content_is_never_retained_in_non_exact_modes(self):
        for mode in (
            NetworkUrlDisplay.REDACTED,
            NetworkUrlDisplay.HOST_PATH,
        ):
            with self.subTest(mode=mode):
                stream = self._stream(mode, secrets=(SECRET,))
                stream.feed_bytes(DISCLOSURE_BYTES)
                stream.finish()
                tail = stream.tail()
                self.assertNotIn(SECRET, tail)
                self.assertNotIn("user:pw@", tail)
                self.assertNotIn("?q=1", tail)
                self.assertNotIn("#frag", tail)

    def test_collector_returns_the_same_single_tail(self):
        for mode in NetworkUrlDisplay:
            with self.subTest(mode=mode):
                capture = _capture([RESEARCH_BYTES], mode=mode)
                self.assertEqual(EXPECTED[mode], capture.stdout_tail)

    def test_exactly_one_buffer_with_and_without_an_sdk_sink(self):
        for mode in NetworkUrlDisplay:
            with self.subTest(mode=mode):
                plain = _capture([RESEARCH_BYTES], mode=mode)
                received = []
                with_sink = _capture(
                    [RESEARCH_BYTES], mode=mode, sink=received.append
                )
                # The sink is transient: the retained tail is unchanged and
                # still the single selected representation.
                self.assertEqual(plain.stdout_tail, with_sink.stdout_tail)
                self.assertEqual(EXPECTED[mode], with_sink.stdout_tail)
                self.assertTrue(received)
                self.assertTrue(all(not c.finalized or c.text for c in received))


class _UrlOutputExecutor:
    """Non-streaming fake executor returning one URL-bearing exit failure."""

    def __init__(self, stdout: str, return_code: int = 1):
        self.calls: list[tuple[str, ...]] = []
        self._stdout = stdout
        self._return_code = return_code

    def run(self, argv: tuple[str, ...]):
        self.calls.append(argv)
        if tuple(argv[:3]) == ("docker", "rm", "-f"):
            return ProcessResult(argv, 0, "", "")
        return ProcessResult(argv, self._return_code, self._stdout, "")


class _RaisingExecutor:
    """Fake executor that fails the assembly but succeeds the cleanup."""

    def __init__(self):
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: tuple[str, ...]):
        self.calls.append(argv)
        if tuple(argv[:3]) == ("docker", "rm", "-f"):
            return ProcessResult(argv, 0, "", "")
        raise OSError("network down")


class TestAssemblerRequestBinding(unittest.TestCase):
    """Task 6.9: the request selector is bound before stream capture."""

    def test_default_factory_and_projector_are_mode_selected(self):
        for mode, expected in EXPECTED.items():
            with self.subTest(mode=mode):
                factory = default_stream_factory((), mode)
                stream = factory("stdout")
                stream.feed_bytes(RESEARCH_BYTES)
                stream.finish()
                self.assertEqual(expected, stream.tail())
                self.assertEqual(
                    expected.strip(),
                    default_tail_projector(mode)(RESEARCH_LINE),
                )

    def test_request_tail_is_mode_selected_without_an_injected_factory(self):
        tmp = tempfile.TemporaryDirectory(prefix="phase6-binding-")
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        raw = _lock("consumer-a", "app-a", "shared")
        for mode, expected in EXPECTED.items():
            with self.subTest(mode=mode):
                cache_root = base / mode.value
                cache_root.mkdir()
                executor = _UrlOutputExecutor(RESEARCH_LINE + "\n")
                with self.assertRaises(LockedNpmError) as raised:
                    assemble_environment(
                        validated=_validated(raw, "app-a"),
                        assembler=_assembler(),
                        cache_root=cache_root,
                        executor=executor,
                        network_url_display=mode,
                    )
                self.assertEqual(
                    expected.strip(), raised.exception.diagnostic_tail
                )

    def test_direct_request_rejects_an_invalid_selector_before_effects(self):
        tmp = tempfile.TemporaryDirectory(prefix="phase6-binding-")
        self.addCleanup(tmp.cleanup)
        raw = _lock("consumer-a", "app-a", "shared")
        executor = _UrlOutputExecutor(RESEARCH_LINE + "\n")
        with self.assertRaises(ValueError):
            assemble_environment(
                validated=_validated(raw, "app-a"),
                assembler=_assembler(),
                cache_root=Path(tmp.name) / "cache",
                executor=executor,
                network_url_display="reveal",  # type: ignore[arg-type]
            )
        self.assertEqual([], executor.calls)


class _InterruptionBoundaryBase(unittest.TestCase):
    """Shared fixture for interruption tests at the real boundaries.

    Diagnostics are captured by an actual ``collect_streams`` call that is
    then interrupted, and the resulting ``KeyboardInterrupt`` propagates
    through the real ``orchestrate_build``/``_real_dispatcher``/``main``
    boundary.  No ``BuildResult`` is constructed by hand and no orchestration
    return is mocked, so the subclasses assert about the production path
    rather than the renderer in isolation.
    """

    LABEL = "Retained diagnostics (may repeat live output):"

    def setUp(self) -> None:
        from tests.build_test_support import INVENTORY_PATH

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.project = root / "project"
        self.project.mkdir()
        self.cache = root / "cache"
        self.cache.mkdir(mode=0o700)
        os.chmod(self.cache, 0o700)
        self.inventory = self.project / "docker-constructor.toml"
        self.inventory.write_bytes(INVENTORY_PATH.read_bytes())
        (self.project / "Dockerfile").write_text("FROM scratch\n")

    def _write_local(self, mode, *, host_heartbeat: str = "off") -> None:
        (self.project / "docker-constructor.local.toml").write_text(
            f'[cache]\ndir = "{self.cache}"\n'
            f"[output]\nhost_heartbeat = \"{host_heartbeat}\"\n"
            f'network_url_display = "{mode.value}"\n'
        )

    def _interrupting_materializer(self, mode):
        def interrupting(*_args, **_kwargs):
            raise _interruption_error(mode)

        return interrupting

    def _run_main(
        self, *, mode, output, host_heartbeat: str = "off",
        session_factory=None, stdout_stream=None, stderr_stream=None,
    ):
        from docker import constructor_cli
        from tests.build_test_support import (
            digest_valid_selected_artifacts,
            publish_digest_valid_artifacts,
        )

        self._write_local(mode, host_heartbeat=host_heartbeat)
        out = stdout_stream if stdout_stream is not None else io.StringIO()
        err = stderr_stream if stderr_stream is not None else io.StringIO()
        created: list[object] = []
        real_make = constructor_cli._make_presentation_session

        def recording_make(policy, *, text_output, stderr_is_tty):
            if session_factory is not None:
                handle = session_factory(
                    policy, text_output=text_output,
                    stderr_is_tty=stderr_is_tty,
                )
            else:
                handle = real_make(
                    policy, text_output=text_output,
                    stderr_is_tty=stderr_is_tty,
                )
            created.append(handle)
            return handle

        with (
            mock.patch(
                "docker.versioning.build_orchestration.select_build_artifacts",
                digest_valid_selected_artifacts,
            ),
            mock.patch(
                "docker.versioning.build_orchestration.materialize_build_artifacts",
                publish_digest_valid_artifacts,
            ),
            mock.patch(
                "docker.versioning.build_orchestration._default_named_context_supported",
                return_value=True,
            ),
            mock.patch(
                "docker.versioning.build_orchestration._materialize_pi_for_build",
                side_effect=self._interrupting_materializer(mode),
            ),
            mock.patch(
                "docker.launcher.artifact_cache.materialize_selected_artifacts",
                return_value={},
            ),
            mock.patch.object(
                constructor_cli,
                "_make_presentation_session",
                side_effect=recording_make,
            ),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            with self.assertRaises(KeyboardInterrupt) as raised:
                constructor_cli.main(
                    [
                        "--project-directory",
                        str(self.project),
                        "--output",
                        output,
                        "build",
                        "--yes",
                    ],
                    stdout_isatty=lambda: False,
                    stderr_isatty=lambda: False,
                )
        self.last_interruption = raised.exception
        return out.getvalue(), err.getvalue(), created


class TestInterruptionEndToEnd(_InterruptionBoundaryBase):
    """Tasks 6.6--6.7: interruption reaches the real boundaries."""

    @unittest.skipUnless(
        hasattr(signal, "pthread_kill"), "requires signal.pthread_kill"
    )
    def test_text_interruption_reports_selected_tail_once(self):
        from docker import constructor_cli

        for mode, expected in EXPECTED.items():
            with self.subTest(mode=mode):
                _out, err, _created = self._run_main(
                    mode=mode, output="text"
                )
                self.assertEqual(1, err.count(self.LABEL))
                self.assertIn(expected.strip(), err)
                self.assertEqual(1, err.count(expected.strip()))
                self.assertIn("[INTERRUPTED]", err)
                # No live session owns output, so no ownership marker is set.
                self.assertFalse(
                    getattr(
                        self.last_interruption,
                        constructor_cli.INTERRUPTION_REPORT_OWNED_ATTR,
                        False,
                    )
                )

    @unittest.skipUnless(
        hasattr(signal, "pthread_kill"), "requires signal.pthread_kill"
    )
    def test_json_interruption_reports_selected_tail_without_live_sink(self):
        for mode, expected in EXPECTED.items():
            with self.subTest(mode=mode):
                out, err, created = self._run_main(mode=mode, output="json")
                # One valid document only, and no live presentation sink.
                self.assertEqual("", err)
                self.assertEqual([], created)
                self.assertNotEqual(
                    "",
                    out,
                    "interruption must produce structured JSON, not silence",
                )
                payload = json.loads(out)
                self.assertEqual("interrupted", payload["status"])
                self.assertEqual(
                    expected.strip(),
                    payload["data"]["host_failure"]["tail"],
                )

    @unittest.skipUnless(
        hasattr(signal, "pthread_kill"), "requires signal.pthread_kill"
    )
    def test_interruption_joins_presentation_worker_and_propagates(self):
        mode = NetworkUrlDisplay.REDACTED
        _out, err, created = self._run_main(
            mode=mode, output="text", host_heartbeat="lines"
        )
        self.assertEqual(1, len(created))
        session = created[0]
        self.assertIsNotNone(session)
        # Presentation shutdown still completed: no live worker survives.
        self.assertFalse(session.worker.is_alive)
        self.assertEqual(1, err.count(self.LABEL))
        self.assertIn(EXPECTED[mode].strip(), err)

    @unittest.skipUnless(
        hasattr(signal, "pthread_kill"), "requires signal.pthread_kill"
    )
    def test_orchestration_interruption_preserves_context_and_cleanup(self):
        from docker.versioning.build_cache import (
            acquire_constructor_project_build_lock,
        )
        from docker.versioning.build_orchestration import (
            BuildRequest,
            describe_host_failure,
            orchestrate_build,
        )
        from tests.build_test_support import (
            digest_valid_selected_artifacts,
            publish_digest_valid_artifacts,
        )
        from tests.pi_fixtures import no_network_transport_factory

        for mode, expected in EXPECTED.items():
            with self.subTest(mode=mode):
                self._write_local(mode)
                request = BuildRequest(
                    inventory_path=str(self.inventory),
                    project_root=str(self.project),
                    confirmed=True,
                    _materialize_pi=self._interrupting_materializer(mode),
                    _transport_factory=no_network_transport_factory,
                    _named_context_supported=lambda: True,
                    network_url_display=mode,
                )
                with (
                    mock.patch(
                        "docker.versioning.build_orchestration.select_build_artifacts",
                        digest_valid_selected_artifacts,
                    ),
                    mock.patch(
                        "docker.versioning.build_orchestration.materialize_build_artifacts",
                        publish_digest_valid_artifacts,
                    ),
                    mock.patch(
                        "docker.versioning.build_orchestration._default_named_context_supported",
                        return_value=True,
                    ),
                    mock.patch(
                        "docker.launcher.artifact_cache.materialize_selected_artifacts",
                        return_value={},
                    ),
                ):
                    with self.assertRaises(KeyboardInterrupt) as raised:
                        orchestrate_build(request)
                exc = raised.exception
                self.assertEqual(
                    expected.strip(), getattr(exc, "diagnostic_tail", "")
                )
                context = describe_host_failure(exc)
                self.assertEqual(expected.strip(), context.tail)
                # Interruption cleanup still released the build lock.
                with acquire_constructor_project_build_lock(
                    self.project, cache_root=self.cache,
                ):
                    pass


class _ScriptedRenderer:
    """Presentation renderer double with scripted durable behaviour.

    ``durable`` records every call.  When ``fail_marker`` is set, the call
    whose text contains it raises; when ``block_on`` is set, that call blocks
    until ``gate`` is released.  Lifecycle hooks are inert so the tests drive
    the real session/mailbox/worker shutdown lifecycle.
    """

    def __init__(self, *, fail_marker=None, block_on=None, gate=None):
        self.durable_calls: list[str] = []
        self.fail_marker = fail_marker
        self.block_on = block_on
        self.gate = gate if gate is not None else threading.Event()

    def durable(self, text: str) -> None:
        self.durable_calls.append(text)
        if self.block_on is not None and self.block_on in text:
            self.gate.wait(30.0)
        if self.fail_marker is not None and self.fail_marker in text:
            raise OSError("scripted renderer failure")

    def set_status(self, text: str) -> None:
        pass

    def set_slot(self, text: str) -> None:
        pass

    def clear_slot(self) -> None:
        pass

    def clear_status(self) -> None:
        pass

    def clear_all(self) -> None:
        pass

    def finalize_diagnostic(self, text: str, *, restore_status) -> None:
        pass


class _BrokenStream:
    """Output stream whose writes fail like a closed downstream pipe."""

    def __init__(self) -> None:
        self.write_attempts = 0
        self.flush_attempts = 0

    def write(self, _text: str) -> int:
        self.write_attempts += 1
        raise BrokenPipeError("broken pipe")

    def flush(self) -> None:
        self.flush_attempts += 1
        raise BrokenPipeError("broken pipe")

    def isatty(self) -> bool:
        return False

    def getvalue(self) -> str:
        return ""


class TestInterruptionPresentationOwnership(_InterruptionBoundaryBase):
    """Interruption reporting respects live presentation ownership.

    A live session owns the retained failure report: it is submitted before
    the bounded shutdown, the facade never re-renders it, and a failed or
    blocked renderer can never displace the original ``KeyboardInterrupt``.
    """

    def _session_factory(self, renderer):
        def factory(policy, *, text_output, stderr_is_tty):
            plan = PresentationPlan(
                HostPresentationMode.LINES,
                PresentationSelection.LIVE,
                policy.network_url_display,
            )
            return HostPresentationSession(renderer, plan)

        return factory

    def _assert_build_lock_released(self):
        from docker.versioning.build_cache import (
            acquire_constructor_project_build_lock,
        )

        with acquire_constructor_project_build_lock(
            self.project, cache_root=self.cache,
        ):
            pass

    @unittest.skipUnless(
        hasattr(signal, "pthread_kill"), "requires signal.pthread_kill"
    )
    def test_healthy_session_owns_the_report_exactly_once(self):
        from docker import constructor_cli

        mode = NetworkUrlDisplay.HOST_PATH
        renderer = _ScriptedRenderer()
        out, err, created = self._run_main(
            mode=mode,
            output="text",
            session_factory=self._session_factory(renderer),
        )
        self.assertEqual(1, len(created))
        session = created[0]
        self.assertIsNotNone(session)
        report_calls = [c for c in renderer.durable_calls if self.LABEL in c]
        self.assertEqual(1, len(report_calls))
        self.assertIn(EXPECTED[mode].strip(), report_calls[0])
        # Ownership is explicit, so the facade never re-renders the report.
        self.assertTrue(
            getattr(
                self.last_interruption,
                constructor_cli.INTERRUPTION_REPORT_OWNED_ATTR,
                False,
            )
        )
        self.assertEqual("", out)
        self.assertEqual("", err)
        # The bounded shutdown ran and cleanup still released the lock.
        self.assertFalse(session.worker.is_alive)
        self._assert_build_lock_released()

    @unittest.skipUnless(
        hasattr(signal, "pthread_kill"), "requires signal.pthread_kill"
    )
    def test_failed_renderer_never_falls_back_or_retries(self):
        from docker import constructor_cli

        mode = NetworkUrlDisplay.EXACT
        renderer = _ScriptedRenderer(fail_marker=self.LABEL)
        out, err, created = self._run_main(
            mode=mode,
            output="text",
            session_factory=self._session_factory(renderer),
        )
        session = created[0]
        # The session admitted and attempted the report exactly once.
        self.assertEqual(
            1, len([c for c in renderer.durable_calls if self.LABEL in c])
        )
        self.assertTrue(
            getattr(
                self.last_interruption,
                constructor_cli.INTERRUPTION_REPORT_OWNED_ATTR,
                False,
            )
        )
        # A failed renderer must not trigger a synchronous fallback write.
        self.assertEqual("", out)
        self.assertEqual("", err)
        self.assertNotIn(self.LABEL, out)
        self.assertNotIn(self.LABEL, err)
        self.assertFalse(session.worker.is_alive)
        self._assert_build_lock_released()

    @unittest.skipUnless(
        hasattr(signal, "pthread_kill"), "requires signal.pthread_kill"
    )
    def test_blocked_renderer_stays_within_shutdown_budget(self):
        mode = NetworkUrlDisplay.REDACTED
        gate = threading.Event()
        renderer = _ScriptedRenderer(block_on=self.LABEL, gate=gate)
        self.addCleanup(gate.set)
        started = time.monotonic()
        out, err, _created = self._run_main(
            mode=mode,
            output="text",
            session_factory=self._session_factory(renderer),
        )
        elapsed = time.monotonic() - started
        # Bounded by the existing presentation shutdown budget, never by the
        # renderer's unbounded block.
        self.assertLess(elapsed, WORKER_JOIN_SECONDS + 3.0)
        self.assertEqual(
            1, len([c for c in renderer.durable_calls if self.LABEL in c])
        )
        self.assertEqual("", out)
        self.assertEqual("", err)
        self._assert_build_lock_released()

    @unittest.skipUnless(
        hasattr(signal, "pthread_kill"), "requires signal.pthread_kill"
    )
    def test_broken_text_output_still_propagates_the_interruption(self):
        from docker import constructor_cli

        # Sessionless text reporting writes directly to stderr; a broken
        # stream must not replace the original interruption.
        broken = _BrokenStream()
        _out, _err, _created = self._run_main(
            mode=NetworkUrlDisplay.HOST_PATH,
            output="text",
            stderr_stream=broken,
        )
        self.assertEqual(1, broken.write_attempts)
        self.assertFalse(
            getattr(
                self.last_interruption,
                constructor_cli.INTERRUPTION_REPORT_OWNED_ATTR,
                False,
            )
        )

    @unittest.skipUnless(
        hasattr(signal, "pthread_kill"), "requires signal.pthread_kill"
    )
    def test_broken_json_output_still_propagates_the_interruption(self):
        from docker import constructor_cli

        # JSON reporting writes the structured document to stdout; a broken
        # stream must not replace the original interruption.
        broken = _BrokenStream()
        _out, _err, created = self._run_main(
            mode=NetworkUrlDisplay.EXACT,
            output="json",
            stdout_stream=broken,
        )
        self.assertEqual([], created)
        self.assertEqual(1, broken.write_attempts)
        self.assertFalse(
            getattr(
                self.last_interruption,
                constructor_cli.INTERRUPTION_REPORT_OWNED_ATTR,
                False,
            )
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
