#!/usr/bin/env python3
"""Phase 1 one-off oversized-tail *fixture* baseline (not tests or CI).

Measures the actual existing oversized-tail and retained-tail scenarios
exercised by the pre-implementation test suite.  Each scenario reproduces its
test's own input construction verbatim -- the test files are **not** modified
-- and runs it through the same production path.  This is the evidence task
1.2 requires; the synthetic ``"a"*N + " end"`` experiment in
``phase1_baseline.py`` remains supplementary evidence only.

The set covers two groups:

* the twelve bounded oversized-token fixtures of
  ``tests/test_host_diagnostic_projection.py``,
  ``tests/test_host_path_projection.py`` and
  ``tests/test_npm_diagnostic_collection_phase8.py``; and
* the two original bottleneck retained-tail scenarios of
  ``tests/test_retained_tail_phase6.py`` whose ``TAIL_BYTES * 2`` uninterrupted
  payloads are run through the collector, the failure context, the text report
  and the JSON facade across all three ``NetworkUrlDisplay`` modes.

Scenario identifiers cite
``tests/<file>::<Class>::<test>``.  The script asserts nothing: it records the
observable result, medians, environment, command and process exit status so a
later phase can compare the same measurements.

Run from the repository root:

    .venv/bin/python openspec/changes/linearize-diagnostic-url-detection/verification/phase1_oversized_fixture_baseline.py \
        [repeats]

The default is 5 timing repeats per scenario so each reported value is a
median.
"""
from __future__ import annotations

import functools
import gc
import json
import os
import platform
import statistics
import sys
import time
from collections.abc import Callable
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))

from docker.constructor_cli import (  # noqa: E402
    CommandRequest,
    _real_dispatcher,
    _render,
)
from docker.npm_environment.execution import _exit_failure  # noqa: E402
from docker.npm_environment.streaming import (  # noqa: E402
    DIAGNOSTIC_LINE_LIMIT_BYTES,
    TAIL_BYTES,
    collect_streams,
)
from docker.versioning.build_orchestration import (  # noqa: E402
    BuildResult,
    _host_failure_context,
)
from docker.versioning.constructor_project import ConstructorProject  # noqa: E402
from docker.versioning.diagnostic_identity import SessionUrlIdentity  # noqa: E402
from docker.versioning.diagnostic_projection import (  # noqa: E402
    INCOMPLETE_TOKEN_MARKER,
    OVERSIZED_TOKEN_MARKER,
    PENDING_LIMIT_BYTES,
    DiagnosticProjector,
    sanitize_diagnostic_text,
)
from docker.versioning.dispatch_types import ExitKind  # noqa: E402
from docker.versioning.host_presentation import (  # noqa: E402
    format_failure_report,
)
from docker.versioning.model import NetworkUrlDisplay  # noqa: E402
from docker.versioning.npm_diagnostic_stream import (  # noqa: E402
    NpmDiagnosticStream,
    OVERSIZED_DIAGNOSTIC_MARKER,
    make_stream_factory,
    project_tail,
)

REPEATS = int(sys.argv[1]) if len(sys.argv) > 1 else 5


class _DrainEntryProbe(DiagnosticProjector):
    """Projector that records retained bytes at every drain entry.

    Replicates ``tests/test_host_diagnostic_projection.py``'s probe verbatim.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.drain_entry_bytes: list[int] = []

    def _drain(self) -> list[str]:
        self.drain_entry_bytes.append(len(self._pending.encode("utf-8")))
        return super()._drain()


def _collect(chunks, *, fingerprinter=None):
    """Replicates the phase-8 test helper of the same name."""
    stream = NpmDiagnosticStream("stdout", (), fingerprinter=fingerprinter)
    live = []
    for chunk in chunks:
        live.extend(stream.feed_bytes(chunk))
    live.extend(stream.finish())
    return live, stream.tail()


class _Reader:
    """One-shot byte reader replicating the retained-tail test helper."""

    def __init__(self, data: bytes):
        self._data = data

    def __call__(self, _max_bytes: int) -> bytes:
        data, self._data = self._data, b""
        return data


def _capture(parts, *, mode, secrets=()):
    """Replicates ``tests/test_retained_tail_phase6.py::_capture``."""
    return collect_streams(
        stdout_read=_Reader(b"".join(parts)),
        stderr_read=_Reader(b""),
        secrets=secrets,
        stream_factory=make_stream_factory(
            secrets, network_url_display=mode
        ),
        tail_projector=functools.partial(
            project_tail, network_url_display=mode
        ),
        network_url_display=mode,
    )


def _render_text_report(mode, error):
    """Replicates the retained-tail text-channel rendering path."""
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


def _render_json_tail(error):
    """Replicates the retained-tail JSON facade rendering path."""
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
    ):
        converted = _real_dispatcher("build", request)
    stdout, _stderr = _render(
        "build",
        converted,
        fmt="json",
        color="never",
        verbose=False,
        stdout_is_tty=False,
        stderr_is_tty=False,
    )
    return context, json.loads(stdout)


def _median(fn, repeats: int) -> dict:
    samples: list[float] = []
    for _ in range(repeats):
        gc.collect()
        start = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - start)
    return {
        "median_seconds": statistics.median(samples),
        "min_seconds": min(samples),
        "max_seconds": max(samples),
        "samples_seconds": samples,
    }


# -- actual existing oversized-tail fixtures ---------------------------------


def _fixture_candidate_cannot_be_retained_past_the_byte_limit() -> dict:
    candidate = "https://example.com/" + "a" * (PENDING_LIMIT_BYTES + 512)
    safe, hostnames = sanitize_diagnostic_text(f"pre {candidate} post")
    return {
        "oversized_marker": OVERSIZED_TOKEN_MARKER in safe,
        "hostnames": list(hostnames),
        "ends_with_post": safe.endswith("post"),
        "safe_length": len(safe),
    }


def _fixture_multibyte_unterminated_candidate_stays_within_the_limit() -> dict:
    projector = DiagnosticProjector()
    text = "https://example.com/" + "🎉" * (2 * PENDING_LIMIT_BYTES)
    out: list[str] = []
    peak = 0
    for index in range(0, len(text), 512):
        out.extend(projector.feed_text(text[index : index + 512]))
        peak = max(peak, projector.pending_size)
    out.extend(projector.finish())
    joined = "".join(out)
    return {
        "oversized_marker": OVERSIZED_TOKEN_MARKER in joined,
        "peak_pending_size": peak,
        "pending_limit_bytes": PENDING_LIMIT_BYTES,
        "retained_emoji_run": "🎉" * 16 in joined,
    }


def _fixture_multibyte_append_never_exceeds_the_limit_at_drain_entry() -> dict:
    text = "https://example.com/" + "🎉" * (2 * PENDING_LIMIT_BYTES)
    projector = _DrainEntryProbe()
    out = list(projector.feed_text(text))
    out.extend(projector.finish())
    joined = "".join(out)
    return {
        "oversized_marker": OVERSIZED_TOKEN_MARKER in joined,
        "peak_drain_entry_bytes": max(projector.drain_entry_bytes, default=0),
        "pending_limit_bytes": PENDING_LIMIT_BYTES,
    }


def _fixture_pending_state_remains_bounded_for_an_unterminated_token() -> dict:
    projector = DiagnosticProjector()
    data = ("https://example.com/" + "a" * (4 * PENDING_LIMIT_BYTES)).encode()
    out: list[str] = []
    peak = 0
    for index in range(0, len(data), 7):
        out.extend(projector.feed_bytes(data[index : index + 7]))
        peak = max(peak, projector.pending_size)
    out.extend(projector.finish())
    joined = "".join(out)
    return {
        "oversized_marker": OVERSIZED_TOKEN_MARKER in joined,
        "peak_pending_size": peak,
        "pending_limit_bytes": PENDING_LIMIT_BYTES,
        "hostnames": list(projector.hostnames),
    }


def _fixture_oversized_token_discards_until_boundary_then_recovers() -> dict:
    huge = "https://example.com/" + "a" * (2 * PENDING_LIMIT_BYTES)
    safe, hostnames = sanitize_diagnostic_text(
        f"pre {huge} post https://good.example/x end"
    )
    return {
        "oversized_marker": OVERSIZED_TOKEN_MARKER in safe,
        "hostnames": list(hostnames),
        "ends_with_recovered": safe.endswith("post <redacted> end"),
        "contains_original_host": "example.com" in safe,
    }


def _fixture_oversized_token_without_boundary_finalizes_without_incomplete() -> dict:
    projector = DiagnosticProjector()
    out = list(
        projector.feed_text(
            "https://example.com/" + "a" * (2 * PENDING_LIMIT_BYTES)
        )
    )
    out.extend(projector.finish())
    joined = "".join(out)
    return {
        "oversized_marker": OVERSIZED_TOKEN_MARKER in joined,
        "incomplete_marker": INCOMPLETE_TOKEN_MARKER in joined,
        "hostnames": list(projector.hostnames),
    }


def _fixture_oversized_url_token_yields_no_host_path_fact() -> dict:
    projector = DiagnosticProjector()
    produced = list(
        projector.feed_text(
            "https://example.com/" + "a" * (PENDING_LIMIT_BYTES + 32)
        )
    )
    projector.finish()
    return {
        "oversized_marker": OVERSIZED_TOKEN_MARKER in "".join(produced),
        "host_paths": [fact.text for fact in projector.host_paths],
    }


def _fixture_oversized_unterminated_url_stays_within_pending_limit() -> dict:
    secret_url = "https://registry.example.com/" + "a" * (16 * 1024)
    stream = NpmDiagnosticStream("stdout", ())
    observed: list[int] = []
    live = []
    for index in range(0, len(secret_url), 256):
        live.extend(stream.feed_bytes(secret_url[index : index + 256].encode()))
        observed.append(stream.pending_sanitizer_bytes)
    live.extend(stream.finish())
    joined = "".join(chunk.text for chunk in live)
    return {
        "peak_pending_sanitizer_bytes": max(observed, default=0),
        "pending_limit_bytes": PENDING_LIMIT_BYTES,
        "oversized_marker": OVERSIZED_TOKEN_MARKER in joined,
        "tail_leaks_run": "a" * 100 in stream.tail(),
    }


def _fixture_oversized_newline_free_diagnostic_stays_within_line_limit() -> dict:
    stream = NpmDiagnosticStream("stdout", ())
    observed: list[int] = []
    live = []
    limit = DIAGNOSTIC_LINE_LIMIT_BYTES
    payload = (
        b"npm notice "
        + b"word " * ((limit - 11) // 5)
        + b"DISCARDME" * 2000
    )
    for index in range(0, len(payload), 1024):
        live.extend(stream.feed_bytes(payload[index : index + 1024]))
        observed.append(stream.pending_line_bytes)
    live.extend(stream.finish())
    joined = "".join(chunk.text for chunk in live)
    return {
        "peak_pending_line_bytes": max(observed, default=0),
        "line_limit_bytes": limit,
        "oversized_diagnostic_marker": OVERSIZED_DIAGNOSTIC_MARKER in joined,
        "prefix_delivered": "word word" in joined,
        "discarded_leaks": "DISCARDME" in joined or "DISCARDME" in stream.tail(),
    }


def _fixture_recovery_after_safe_boundary() -> dict:
    payload = b"npm notice " + b"chunk " * (20 * 1024) + b"\nrecovered line\n"
    live, tail = _collect([payload])
    joined = "".join(chunk.text for chunk in live)
    return {
        "oversized_diagnostic_marker": OVERSIZED_DIAGNOSTIC_MARKER in joined,
        "recovered_in_live": "recovered line" in joined,
        "recovered_in_tail": "recovered line" in tail,
    }


def _fixture_input_keeps_draining_after_overflow() -> dict:
    payload = b"npm notice " + b"drain " * (20 * 1024) + b"\nok\n"
    stream = NpmDiagnosticStream("stdout", ())
    produced = 0
    for index in range(0, len(payload), 1024):
        produced += len(stream.feed_bytes(payload[index : index + 1024]))
    produced += len(stream.finish())
    return {
        "chunks_produced": produced,
        "peak_pending_line_bytes": stream.pending_line_bytes,
        "pending_limit_bytes": DIAGNOSTIC_LINE_LIMIT_BYTES,
        "pending_sanitizer_bytes": stream.pending_sanitizer_bytes,
        "sanitizer_limit_bytes": PENDING_LIMIT_BYTES,
    }


def _fixture_oversized_url_heavy_line_discards_metadata_and_recovers() -> dict:
    identity = SessionUrlIdentity()
    limit = DIAGNOSTIC_LINE_LIMIT_BYTES
    prefix = b"npm warn " + b"pad " * ((limit - 9) // 4) + b"pad"[: (limit - 9) % 4]
    oversized = prefix + b" https://a.example/secret" * 100 + b"\n"
    stream = NpmDiagnosticStream("stdout", (), fingerprinter=identity)
    live = []
    observed_sanitizer: list[int] = []
    observed_line: list[int] = []
    payload = oversized + b"npm warn see https://b.example/ok\n"
    for index in range(0, len(payload), 4096):
        live.extend(stream.feed_bytes(payload[index : index + 4096]))
        observed_sanitizer.append(stream.pending_sanitizer_bytes)
        observed_line.append(stream.pending_line_bytes)
    live.extend(stream.finish())
    finalized = [chunk for chunk in live if chunk.finalized]
    marker_chunks = [
        chunk
        for chunk in finalized
        if OVERSIZED_DIAGNOSTIC_MARKER in chunk.text
    ]
    recovered = [
        chunk for chunk in finalized if chunk.text.startswith("npm warn see")
    ]
    return {
        "peak_pending_sanitizer_bytes": max(observed_sanitizer, default=0),
        "peak_pending_line_bytes": max(observed_line, default=0),
        "oversized_diagnostic_chunks": len(marker_chunks),
        "marker_hostnames": sorted(
            host for chunk in marker_chunks for host in chunk.hostnames
        ),
        "recovered_chunks": len(recovered),
        "recovered_hostnames": sorted(
            host for chunk in recovered for host in chunk.hostnames
        ),
        "tail_leaks_a_example": "a.example" in stream.tail(),
    }


def _fixture_truncated_tail_is_bounded_and_mode_selected() -> dict:
    """Verbatim input of the phase-6 retained-tail bounded/mode test.

    ``tests/test_retained_tail_phase6.py::TestFailureContextChannels
    ::test_truncated_tail_is_bounded_and_mode_selected``
    """
    oversized = (
        "npm warn "
        + "x" * (TAIL_BYTES * 2)
        + " https://registry.npmjs.org/pkg.tgz http://proxy.internal/x\n"
    )
    modes: dict[str, dict] = {}
    for mode in NetworkUrlDisplay:
        capture = _capture([oversized.encode()], mode=mode)
        error = _exit_failure(
            1,
            stderr=capture.stderr_tail,
            stdout=capture.stdout_tail,
            truncation_notice=capture.truncation_notice,
        )
        _context, report = _render_text_report(mode, error)
        modes[mode.value] = {
            "payload_x_run_bytes": TAIL_BYTES * 2,
            "captured_tail_bytes": len(
                capture.stdout_tail.encode("utf-8")
            ),
            "tail_bytes_limit": TAIL_BYTES,
            "retained_diagnostics_section_count": report.count(
                "Retained diagnostics (may repeat live output):"
            ),
        }
    return modes


def _fixture_truncated_tail_is_bounded_in_text_and_json() -> dict:
    """Verbatim input of the phase-6 bounded text/JSON retained-tail test.

    ``tests/test_retained_tail_phase6.py::TestFailureChannelMatrix
    ::test_truncated_tail_is_bounded_in_text_and_json``
    """
    oversized = (
        "npm warn "
        + "x" * (TAIL_BYTES * 2)
        + " https://registry.npmjs.org/pkg.tgz\n"
    ).encode()
    modes: dict[str, dict] = {}
    for mode in NetworkUrlDisplay:
        capture = _capture([oversized], mode=mode)
        error = _exit_failure(1, stderr="", stdout=capture.stdout_tail)
        context, report = _render_text_report(mode, error)
        _context, payload = _render_json_tail(error)
        json_tail = payload["data"]["host_failure"]["tail"]
        modes[mode.value] = {
            "payload_x_run_bytes": TAIL_BYTES * 2,
            "captured_tail_bytes": len(
                capture.stdout_tail.encode("utf-8")
            ),
            "context_tail_bytes": len(context.tail.encode("utf-8")),
            "json_tail_bytes": len(json_tail.encode("utf-8")),
            "tail_bytes_limit": TAIL_BYTES,
            "retained_diagnostics_section_count": report.count(
                "Retained diagnostics (may repeat live output):"
            ),
            "json_tail_equals_context": json_tail == context.tail,
        }
    return modes


SCENARIOS: tuple[tuple[str, str, Callable[[], dict]], ...] = (
    (
        "tests/test_host_diagnostic_projection.py::TestBoundarySafeUrlSanitization::test_candidate_cannot_be_retained_past_the_byte_limit",
        "projector: candidate past byte limit",
        _fixture_candidate_cannot_be_retained_past_the_byte_limit,
    ),
    (
        "tests/test_host_diagnostic_projection.py::TestBoundarySafeUrlSanitization::test_multibyte_unterminated_candidate_stays_within_the_limit",
        "projector: multibyte unterminated, 512-char feeds",
        _fixture_multibyte_unterminated_candidate_stays_within_the_limit,
    ),
    (
        "tests/test_host_diagnostic_projection.py::TestBoundarySafeUrlSanitization::test_multibyte_append_never_exceeds_the_limit_at_drain_entry",
        "projector: multibyte whole feed, drain-entry peak",
        _fixture_multibyte_append_never_exceeds_the_limit_at_drain_entry,
    ),
    (
        "tests/test_host_diagnostic_projection.py::TestBoundarySafeUrlSanitization::test_pending_state_remains_bounded_for_an_unterminated_token",
        "projector: 7-byte feeds of 4x-limit token",
        _fixture_pending_state_remains_bounded_for_an_unterminated_token,
    ),
    (
        "tests/test_host_diagnostic_projection.py::TestBoundarySafeUrlSanitization::test_oversized_token_discards_until_boundary_then_recovers",
        "projector: oversized token then recovery URL",
        _fixture_oversized_token_discards_until_boundary_then_recovers,
    ),
    (
        "tests/test_host_diagnostic_projection.py::TestBoundarySafeUrlSanitization::test_oversized_token_without_boundary_finalizes_without_incomplete_marker",
        "projector: oversized token finalized at EOF",
        _fixture_oversized_token_without_boundary_finalizes_without_incomplete,
    ),
    (
        "tests/test_host_path_projection.py::TestHostPathBoundaries::test_oversized_url_token_yields_no_host_path_fact",
        "host-path: oversized URL token, no fact",
        _fixture_oversized_url_token_yields_no_host_path_fact,
    ),
    (
        "tests/test_npm_diagnostic_collection_phase8.py::TestCollectorBranch::test_oversized_unterminated_url_stays_within_pending_limit",
        "collector: 16 KiB URL, 256-byte feeds",
        _fixture_oversized_unterminated_url_stays_within_pending_limit,
    ),
    (
        "tests/test_npm_diagnostic_collection_phase8.py::TestCollectorBranch::test_oversized_newline_free_diagnostic_stays_within_line_limit",
        "collector: newline-free line past 64 KiB",
        _fixture_oversized_newline_free_diagnostic_stays_within_line_limit,
    ),
    (
        "tests/test_npm_diagnostic_collection_phase8.py::TestCollectorBranch::test_recovery_after_safe_boundary",
        "collector: recovery after safe boundary",
        _fixture_recovery_after_safe_boundary,
    ),
    (
        "tests/test_npm_diagnostic_collection_phase8.py::TestCollectorBranch::test_input_keeps_draining_after_overflow",
        "collector: input keeps draining after overflow",
        _fixture_input_keeps_draining_after_overflow,
    ),
    (
        "tests/test_npm_diagnostic_collection_phase8.py::TestConsumablePerLineFacts::test_oversized_url_heavy_line_discards_metadata_and_recovers",
        "collector: oversized URL-heavy line, metadata + recovery",
        _fixture_oversized_url_heavy_line_discards_metadata_and_recovers,
    ),
    (
        "tests/test_retained_tail_phase6.py::TestFailureContextChannels::test_truncated_tail_is_bounded_and_mode_selected",
        "retained-tail: bounded + mode-selected text (x-run past 2x TAIL_BYTES)",
        _fixture_truncated_tail_is_bounded_and_mode_selected,
    ),
    (
        "tests/test_retained_tail_phase6.py::TestFailureChannelMatrix::test_truncated_tail_is_bounded_in_text_and_json",
        "retained-tail: bounded text + JSON (x-run past 2x TAIL_BYTES)",
        _fixture_truncated_tail_is_bounded_in_text_and_json,
    ),
)


def main() -> int:
    print("=== phase1 oversized-tail fixture baseline ===")
    print("command:", " ".join(sys.argv))
    print(
        "environment:",
        json.dumps(
            {
                "python_version": platform.python_version(),
                "python_implementation": platform.python_implementation(),
                "executable": sys.executable,
                "platform": platform.platform(),
                "machine": platform.machine(),
                "processor": platform.processor(),
                "cpu_count": os.cpu_count(),
                "pending_limit_bytes": PENDING_LIMIT_BYTES,
                "diagnostic_line_limit_bytes": DIAGNOSTIC_LINE_LIMIT_BYTES,
                "repeats": REPEATS,
            },
            indent=2,
        ),
    )
    print()

    results: list[dict] = []
    for test_id, label, fixture in SCENARIOS:
        print(f"--- {label} ---", flush=True)
        print(f"test: {test_id}")
        print("observable:", json.dumps(fixture(), indent=2))
        results.append(
            {
                "test_id": test_id,
                "label": label,
                **_median(fixture, REPEATS),
            }
        )
        print()

    print("--- medians ---")
    print(json.dumps(results, indent=2))
    print()
    print("=== oversized-tail fixture baseline complete ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
