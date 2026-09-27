#!/usr/bin/env python3
"""Phase 6 retention probe (tasks 6.1-6.5), compatible across revisions.

The probe asserts the Phase 6 retention contract through the assembled
collector surface: each mode's single retained tail is the selected
representation (``redacted`` / ``host-path`` / ``exact``), with and without an
external SDK sink, while every SDK event stays transient and URL-free.

It is deliberately revision-tolerant.  When the checked-out revision predates
the assembler-request selector, the probe calls the pre-change constructor
signature instead of passing ``network_url_display``.  A pre-change checkout
therefore fails on retained *content* (the wrong representation is stored),
never on a missing import or an unsupported argument.  The follow-up
failure-propagation fix does not touch retention at all, so this probe is the
correct pre-implementation check for tasks 6.1-6.5.

Usage (from a checkout root)::

    python3 openspec/changes/add-configurable-network-url-display/verification/phase6_retention_probe.py

Exit status is non-zero when any retention assertion fails.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from docker.versioning.model import NetworkUrlDisplay  # noqa: E402
from docker.versioning.npm_diagnostic_stream import (  # noqa: E402
    NpmDiagnosticStream,
    make_stream_factory,
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

EXPECTED = {
    NetworkUrlDisplay.REDACTED: REDACTED_TAIL,
    NetworkUrlDisplay.HOST_PATH: HOST_PATH_TAIL,
    NetworkUrlDisplay.EXACT: EXACT_TAIL,
}


class _Reader:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def __call__(self, _max_bytes: int) -> bytes:
        data, self._data = self._data, b""
        return data


def _accepts(func, name: str) -> bool:
    try:
        return name in inspect.signature(func).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins only
        return False


def _stream_factory(mode, secrets=()):
    if _accepts(make_stream_factory, "network_url_display"):
        return make_stream_factory(secrets, network_url_display=mode)
    return make_stream_factory(secrets)


def _stream(mode, secrets=()):
    if _accepts(NpmDiagnosticStream.__init__, "network_url_display"):
        return NpmDiagnosticStream("stdout", secrets, network_url_display=mode)
    return NpmDiagnosticStream("stdout", secrets)


def _capture(mode, *, sink=None, secrets=()):
    from docker.npm_environment.streaming import collect_streams

    kwargs = dict(
        stdout_read=_Reader(RESEARCH_BYTES),
        stderr_read=_Reader(b""),
        secrets=secrets,
        sink=sink,
        stream_factory=_stream_factory(mode, secrets),
    )
    if _accepts(collect_streams, "network_url_display"):
        kwargs["network_url_display"] = mode
    return collect_streams(**kwargs)


def _check(label: str, actual, expected, failures: list[str]) -> None:
    if actual == expected:
        print(f"PASS {label}")
    else:
        print(f"FAIL {label}: retained {actual!r}, expected {expected!r}")
        failures.append(label)


def main() -> int:
    failures: list[str] = []

    # Tasks 6.1-6.3: each mode feeds exactly one selected retained tail.
    for mode, expected in EXPECTED.items():
        stream = _stream(mode)
        stream.feed_bytes(RESEARCH_BYTES)
        stream.finish()
        _check(f"6.1-6.3 stream tail [{mode.value}]", stream.tail(), expected, failures)

    # Task 6.4: without an SDK sink, exactly one selected representation.
    for mode, expected in EXPECTED.items():
        capture = _capture(mode, sink=None)
        _check(f"6.4 no-sink capture [{mode.value}]", capture.stdout_tail, expected, failures)

    # Task 6.5: with an SDK sink, the local tail stays selected while every
    # SDK event stays transient and URL-free.
    for mode, expected in EXPECTED.items():
        received: list[object] = []
        capture = _capture(mode, sink=received.append)
        _check(f"6.5 with-sink capture [{mode.value}]", capture.stdout_tail, expected, failures)
        for chunk in received:
            text = getattr(chunk, "text", "")
            if "https://" in text:
                print(f"FAIL 6.5 SDK event leaked scheme [{mode.value}]: {text!r}")
                failures.append(f"6.5 sdk-scheme [{mode.value}]")

    print(f"\n{len(failures)} failing retention assertion(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
