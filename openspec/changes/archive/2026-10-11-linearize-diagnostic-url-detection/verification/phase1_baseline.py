#!/usr/bin/env python3
"""Phase 1 one-off baseline experiment (not part of tests or CI).

Captures, against the pre-implementation recognizer, the demonstrated
quadratic ``_find_url_start`` growth and the current oversized-tail behavior.
It changes no fixtures and asserts nothing; it only records medians, profiles,
environment, commands and the process exit status so a later phase can compare
the same measurements.

Run from the repository root:

    .venv/bin/python openspec/changes/linearize-diagnostic-url-detection/verification/phase1_baseline.py \
        [repeats]

The default is 5 timing repeats per size so each reported value is a median.
"""
from __future__ import annotations

import cProfile
import gc
import io
import json
import os
import platform
import pstats
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))

from docker.versioning.diagnostic_projection import (  # noqa: E402
    PENDING_LIMIT_BYTES,
    DiagnosticProjector,
    _find_url_start,
)
from docker.versioning.model import NetworkUrlDisplay  # noqa: E402
from docker.versioning.npm_diagnostic_stream import project_tail  # noqa: E402

SIZES = (1024, 2048, 4096, 8192, 16384)
REPEATS = int(sys.argv[1]) if len(sys.argv) > 1 else 5

#: One realistic diagnostic used for the per-mode capture profiles: a known
#: literal URL, a secret, and an unknown-scheme URL.
PROFILE_SAMPLE = (
    "GET https://registry.example.com/pkg/-/pkg-1.0.0.tgz 15ms done "
    "token=supersecret "
    "custom+transport://user:password@internal.example/private?token=value "
)


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


def _uninterrupted(size: int) -> str:
    """One scheme-character run with no separator or terminator."""
    return "a" * size


def _separated(size: int) -> str:
    """Fixed-size scheme-character tokens separated by single spaces."""
    token = "a" * 8
    count = max(1, size // (len(token) + 1))
    return " ".join([token] * count)[:size]


def _feed_oversized(size: int):
    text = "a" * size + " end"

    def run() -> str:
        projector = DiagnosticProjector()
        out = list(projector.feed_text(text))
        out.extend(projector.finish())
        return "".join(out)

    return run


def _profile_mode(mode: NetworkUrlDisplay, repeats: int = 200) -> dict:
    profiler = cProfile.Profile()
    profiler.enable()
    for _ in range(repeats):
        project_tail(
            PROFILE_SAMPLE,
            secrets=("supersecret",),
            network_url_display=mode,
        )
    profiler.disable()
    stream = io.StringIO()
    pstats.Stats(profiler, stream=stream).sort_stats("cumulative").print_stats(10)
    return {"repeats": repeats, "profile": stream.getvalue()}


def main() -> int:
    print("=== phase1 baseline experiment ===")
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
                "repeats": REPEATS,
            },
            indent=2,
        ),
    )
    print()

    print("--- _find_url_start: uninterrupted vs separated ---")
    detection: dict[str, dict] = {}
    for size in SIZES:
        uninterrupted_text = _uninterrupted(size)
        separated_text = _separated(size)

        def run_uninterrupted(text=uninterrupted_text):
            _find_url_start(text)

        def run_separated(text=separated_text):
            _find_url_start(text)

        print(f"size={size} bytes ...", flush=True)
        detection[str(size)] = {
            "uninterrupted": {
                "length_chars": len(uninterrupted_text),
                "result": _find_url_start(uninterrupted_text),
                **_median(run_uninterrupted, REPEATS),
            },
            "separated": {
                "length_chars": len(separated_text),
                "result": _find_url_start(separated_text),
                **_median(run_separated, REPEATS),
            },
        }
    print(json.dumps(detection, indent=2))
    print()

    print("--- existing oversized-tail timings ---")
    oversized: dict[str, dict] = {}
    for size in (PENDING_LIMIT_BYTES, 2 * PENDING_LIMIT_BYTES):
        run = _feed_oversized(size)
        print(f"oversized size={size} bytes ...", flush=True)
        result = run()
        oversized[str(size)] = {
            "input_chars": size + len(" end"),
            "result": result,
            "result_length": len(result),
            **_median(run, REPEATS),
        }
    print(json.dumps(oversized, indent=2))
    print()

    print("--- per-mode capture profiles ---")
    profiles: dict[str, dict] = {}
    for mode in NetworkUrlDisplay:
        print(f"mode={mode.value} ...", flush=True)
        profiles[mode.value] = {
            "rendered": project_tail(
                PROFILE_SAMPLE,
                secrets=("supersecret",),
                network_url_display=mode,
            ),
            **_profile_mode(mode),
        }
    print(json.dumps(profiles, indent=2))
    print()

    print("=== baseline complete ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
