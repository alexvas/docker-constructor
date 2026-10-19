"""Phase 6 controlled npm HTTP logging research contract."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests import npm_http_logging_research as research
from tests.npm_http_logging_research import (
    NPM_VERSION,
    _aggregate_projected_lines,
    _run,
    run_research,
)


class TestNpmHttpLoggingResearch(unittest.TestCase):
    def test_local_fixture_exercises_all_required_scenarios(self) -> None:
        observations = run_research()
        self.assertEqual(len(observations), 10)
        self.assertEqual(
            {item.scenario for item in observations},
            {"cache-miss", "cache-hit", "retry", "timeout", "high-volume"},
        )
        self.assertEqual({item.loglevel for item in observations}, {"notice", "http"})
        for item in observations:
            with self.subTest(scenario=item.scenario, loglevel=item.loglevel):
                self.assertLess(item.elapsed_seconds, 15)
                self.assertNotIn("127.0.0.1", item.stdout + item.stderr)
                self.assertNotIn(
                    "https://", item.projected_stdout + item.projected_stderr
                )
                if item.first_observation_seconds is None:
                    self.fail("the pipe reader observed no output")
                # Research reports what the reader actually observed. A final
                # npm error burst may be read after exit under CPU contention.
                self.assertEqual(
                    item.confirmed_live_output,
                    item.first_observation_returncode is None,
                )
                if item.confirmed_live_output:
                    self.assertLessEqual(
                        item.first_observation_seconds, item.exit_observation_seconds
                    )
                else:
                    self.assertEqual(item.first_observation_returncode, item.returncode)
                self.assertGreater(item.exit_observation_seconds, 0)
                self.assertGreater(item.line_count, 0)
                self.assertGreater(item.projected_line_count, 0)
        self.assertEqual(len({item.cache_id for item in observations}), 10)
        for item in observations:
            with self.subTest(cache=item.cache_id):
                self.assertTrue(item.cache_initially_empty)
                self.assertEqual(item.cache_seeded, item.scenario == "cache-hit")
        outcomes = {
            (item.scenario, item.loglevel): item.returncode for item in observations
        }
        self.assertEqual(outcomes[("cache-miss", "http")], 0)
        self.assertEqual(outcomes[("cache-hit", "http")], 0)
        self.assertNotEqual(outcomes[("retry", "http")], 0)
        self.assertNotEqual(outcomes[("timeout", "http")], 0)
        self.assertNotEqual(outcomes[("high-volume", "http")], 0)
        self.assertTrue(
            any(
                "attempt" in item.stderr
                for item in observations
                if item.scenario == "retry" and item.loglevel == "http"
            )
        )
        self.assertTrue(
            any(
                "timeout" in item.stderr.lower()
                for item in observations
                if item.scenario == "timeout" and item.loglevel == "http"
            )
        )
        raw_http = {
            item.scenario: item.stderr
            for item in observations
            if item.loglevel == "http"
        }
        self.assertIn("(cache miss)", raw_http["cache-miss"])
        self.assertIn("(cache hit)", raw_http["cache-hit"])
        self.assertNotIn("(cache miss)", raw_http["cache-hit"])
        high_volume = raw_http["high-volume"]
        high_observation = next(
            item
            for item in observations
            if item.scenario == "high-volume" and item.loglevel == "http"
        )
        self.assertGreater(len(high_volume.splitlines()), 30)
        self.assertNotIn("https://", "\n".join(high_observation.aggregated_lines))
        # Distinct npm attempts remain distinct; production does not normalize them.
        self.assertIn("attempt 1 failed", "\n".join(high_observation.aggregated_lines))
        self.assertIn("attempt 2 failed", "\n".join(high_observation.aggregated_lines))
        self.assertGreaterEqual(len(high_observation.aggregated_lines), 30)

    def test_identical_final_lines_coalesce_regardless_of_classification(self) -> None:
        lines = "\n".join(
            (
                "status",
                "status",
                "npm warn useful",
                "npm warn useful",
                "npm error useful",
                "npm error useful",
                "attempt 1",
                "attempt 2",
            )
        )
        aggregated = _aggregate_projected_lines(lines)
        self.assertEqual(
            aggregated,
            (
                "status (repeated 2 times)",
                "npm warn useful (repeated 2 times)",
                "npm error useful (repeated 2 times)",
                "attempt 1",
                "attempt 2",
            ),
        )

    def test_inherited_proxy_and_npm_configuration_cannot_bypass_fixture(self) -> None:
        inherited = {
            "NO_PROXY": "registry.npmjs.org",
            "no_proxy": "registry.npmjs.org",
            "npm_config_registry": "https://invalid.example",
            "npm_config_proxy": "http://invalid.example:9",
        }
        with mock.patch.dict(os.environ, inherited, clear=False):
            observations = run_research()
        self.assertTrue(
            all(
                item.hostnames == ("registry.npmjs.org",) or not item.hostnames
                for item in observations
            )
        )
        self.assertTrue(
            any(
                item.scenario == "cache-miss" and item.returncode == 0
                for item in observations
            )
        )

    def test_output_read_while_child_waits_for_observation_is_confirmed_live(self):
        read = research._read_fd
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "observed"

            class ObservedReturncodes:
                def __init__(self, values):
                    self.values = values

                def append(self, value):
                    self.values.append(value)
                    # The child cannot exit until liveness has been sampled.
                    marker.mkdir()

            def observed_read(fd, chunks, first, returncodes, lock, child, gate=None):
                return read(
                    fd,
                    chunks,
                    first,
                    ObservedReturncodes(returncodes),
                    lock,
                    child,
                    gate,
                )

            with mock.patch.object(research, "_read_fd", observed_read):
                observation = _run(
                    (
                        sys.executable,
                        "-c",
                        "import os,time;print('live output',flush=True);"
                        f"\nwhile not os.path.isdir({str(marker)!r}): time.sleep(0.001)",
                    ),
                    cwd=Path(temporary),
                    env={"PATH": os.environ["PATH"]},
                    scenario="retry",
                    cache_initially_empty=True,
                    cache_seeded=False,
                    cache_id="live",
                    loglevel="notice",
                )
        self.assertEqual(observation.stdout, "live output\n")
        self.assertEqual(observation.returncode, 0)
        self.assertIsNotNone(observation.first_observation_seconds)
        self.assertIsNone(observation.first_observation_returncode)
        self.assertTrue(observation.confirmed_live_output)

    def test_output_read_after_child_exit_is_not_confirmed_live(self) -> None:
        read = research._read_fd

        def after_exit(fd, chunks, first, returncodes, lock, child, gate=None):
            # Observe the runner's existing wait authority, without another reap.
            end, pause = time.monotonic() + 5, threading.Event()
            while child.returncode is None and time.monotonic() < end:
                pause.wait(0.001)
            self.assertEqual(child.returncode, 0)
            return read(fd, chunks, first, returncodes, lock, child, gate)

        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(research, "_read_fd", after_exit),
        ):
            observation = _run(
                (sys.executable, "-c", "print('late output')"),
                cwd=Path(temporary),
                env={"PATH": os.environ["PATH"]},
                scenario="retry",
                cache_initially_empty=True,
                cache_seeded=False,
                cache_id="late",
                loglevel="notice",
            )
        self.assertEqual(observation.stdout, "late output\n")
        self.assertEqual(observation.first_observation_returncode, 0)
        self.assertIsNotNone(observation.first_observation_seconds)
        self.assertFalse(observation.confirmed_live_output)

    def test_hanging_child_is_terminated_and_reaped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            started = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                _run(
                    (sys.executable, "-c", "import time; time.sleep(60)"),
                    cwd=Path(temporary),
                    env={"PATH": os.environ["PATH"]},
                    scenario="timeout",
                    cache_initially_empty=True,
                    cache_seeded=False,
                    cache_id="hang",
                    timeout_seconds=0.1,
                )
            self.assertLess(time.monotonic() - started, 3)


class TestResearchReportContract(unittest.TestCase):
    def test_report_records_inputs_evidence_and_binary_decision(self) -> None:
        from pathlib import Path

        report = (
            Path(__file__).resolve().parents[1]
            / "docs"
            / "research"
            / "npm-http-logging.md"
        ).read_text()
        for required in (
            "24.18.0",
            NPM_VERSION,
            "node:24.18.0-trixie-slim",
            "Command and environment",
            "Raw fixtures",
            "Projected fixtures",
            "First-observation time",
            "Exit-observation time",
            "Raw line count",
            "Projected line count",
            "Return code",
            "High-volume",
            "Aggregation",
            "Decision: ACCEPT",
            "Production impact",
            "production npm invocation",
            "policy digest",
            "evidence identity",
            "cache identity",
            "unchanged",
            "not yet production policy",
        ):
            self.assertIn(required, report)
        for scenario in ("cache-miss", "cache-hit", "retry", "timeout"):
            for loglevel in ("notice", "http"):
                self.assertIn(f"| {scenario} | {loglevel} |", report)
        raw = report.split("### Raw fixtures\n", 1)[1].split(
            "### Projected fixtures\n", 1
        )[0]
        projected = report.split("### Projected fixtures\n", 1)[1].split(
            "### Measured required scenarios\n", 1
        )[0]
        for marker in (
            "https://registry.npmjs.org/",
            "(cache miss)",
            "(cache hit)",
            "attempt 1 failed",
            "timeout",
        ):
            self.assertIn(marker, raw)
        for marker in (
            "<redacted> 15ms (cache miss)",
            "@<redacted> 0ms (cache hit)",
            "<redacted> attempt 1 failed",
            "timeout at: <redacted>",
        ):
            self.assertIn(marker, projected)

    def test_canonical_production_command_has_accepted_loglevel(self) -> None:
        from docker.npm_environment.assembler import (
            ASSEMBLER_SCRIPT,
            NPM_CI_COMMAND,
            NPM_CI_FLAGS,
        )

        self.assertIn("--loglevel=http", NPM_CI_COMMAND)
        self.assertIn("--loglevel=http", NPM_CI_FLAGS)
        self.assertIn("--loglevel=http", ASSEMBLER_SCRIPT)


if __name__ == "__main__":
    unittest.main()
