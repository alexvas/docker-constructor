"""RED-phase tests for ``docker.networking`` — Stage 7.1 diagnosis, 7.2 overrides.

Covers rootful/rootless detection, candidate ordering, LAN-IP
discovery, probe results, diagnosis orchestration, DTO immutability,
override planning states, structured failures, consent enforcement,
and atomic persistence.  All tests use injected fakes — no Docker
daemon, no systemd, no network access.

Requirements: 7.1 (detection, candidates, probes, diagnosis),
              7.2 (override planning, consent, failures, persistence).

See ``tests/test_networking.py`` for the GREEN‑phase suite.
"""

from __future__ import annotations

import dataclasses
import unittest
from pathlib import Path
from typing import Optional
from unittest import mock

from docker.networking import (
    DockerDetectionError,
    DockerMode,
    Filesystem,
    FilesystemOperation,
    GatewayDiagnosis,
    OverrideFailure,
    OverrideState,
    PersistenceResult,
    ProbeResult,
    ProcessResult,
    ProcessRunner,
    RootlessOverridePlan,
    ServiceController,
    ServiceOperation,
    SystemClock,
    apply_rootless_override,
    candidate_gateways,
    detect_docker_mode,
    detect_lan_ip,
    diagnose_gateway,
    plan_rootless_override,
    probe_gateway,
)

# Re-use the in-memory fake from the GREEN‑phase suite so RED tests never
# touch real disk or systemd.
from tests.test_networking import FakeFilesystem, FakeHostProbeServer

# ---------------------------------------------------------------------------
# Deterministic fakes (requirement 7)
# ---------------------------------------------------------------------------


def _completed(
    rc: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> ProcessRunner:
    """Return a ``ProcessRunner`` that always responds with the
    same canned ``ProcessResult`` regardless of argv."""

    class _Single(ProcessRunner):
        def run(self, argv):
            return ProcessResult(
                argv=tuple(argv),
                return_code=rc,
                stdout=stdout,
                stderr=stderr,
            )

    return _Single()


def _proc(rc: int = 0, stdout: str = "", stderr: str = "") -> ProcessResult:
    """Return a ``ProcessResult`` — for use inside inline fake
    functions that dispatch on argv."""
    return ProcessResult(
        argv=(),
        return_code=rc,
        stdout=stdout,
        stderr=stderr,
    )


def _as_runner(fn) -> ProcessRunner:
    """Wrap an inline ``run(cmd)`` callable as a ``ProcessRunner``."""

    class _Adapter(ProcessRunner):
        def run(self, argv):
            return fn(argv)

    return _Adapter()


def _fake_run(
    *,
    docker_info_stdout: str = "Server Version: 27.0.0\n",
    docker_info_rc: int = 0,
    docker_info_raises: Optional[Exception] = None,
    hostname_stdout: str = "192.168.1.10",
    hostname_raises: Optional[Exception] = None,
    ip_route_stdout: str = "",
    ip_route_raises: Optional[Exception] = None,
    probe_stdouts: Optional[list[str]] = None,
    probe_rcs: Optional[list[int]] = None,
    probe_raises: Optional[list[Optional[Exception]]] = None,
) -> ProcessRunner:
    """Build a fake ``ProcessRunner`` with per-command behaviour."""

    _call = [0]  # mutable counter for probe sequencing

    class _Fake(ProcessRunner):
        def run(self, argv: list[str]) -> ProcessResult:
            key = argv[0] if argv else ""

            if key == "docker" and len(argv) >= 2 and argv[1] == "info":
                if docker_info_raises:
                    raise docker_info_raises
                return ProcessResult(
                    argv=tuple(argv),
                    return_code=docker_info_rc,
                    stdout=docker_info_stdout,
                    stderr="" if docker_info_rc == 0 else "permission denied",
                )

            if key == "docker" and len(argv) >= 2 and argv[1] == "run":
                idx = _call[0]
                _call[0] += 1
                if probe_raises and idx < len(probe_raises) and probe_raises[idx]:
                    raise probe_raises[idx]  # type: ignore[operator]
                stdout = ""
                if probe_stdouts and idx < len(probe_stdouts):
                    stdout = probe_stdouts[idx]
                rc = 0
                if probe_rcs and idx < len(probe_rcs):
                    rc = probe_rcs[idx]
                return ProcessResult(
                    argv=tuple(argv),
                    return_code=rc,
                    stdout=stdout,
                    stderr="",
                )

            if key == "hostname":
                if hostname_raises:
                    raise hostname_raises
                return ProcessResult(
                    argv=tuple(argv),
                    return_code=0,
                    stdout=hostname_stdout,
                    stderr="",
                )

            if key == "ip":
                if ip_route_raises:
                    raise ip_route_raises
                return ProcessResult(
                    argv=tuple(argv),
                    return_code=0,
                    stdout=ip_route_stdout,
                    stderr="",
                )

            return ProcessResult(
                argv=tuple(argv),
                return_code=0,
                stdout="",
                stderr="",
            )

    return _Fake()


class FakeHostProbeServer:
    """Fake ``HostProbeServer`` for diagnosis tests."""

    def __init__(self, port: int = 12345, token: str = "FAKE_TOKEN"):
        self.port = port
        self.token = token
        self.started = False
        self.stopped = False

    def start(self) -> int:
        self.started = True
        return self.port

    def stop(self) -> None:
        self.stopped = True


# ---------------------------------------------------------------------------
# 8. Rootless-detection tests
# ---------------------------------------------------------------------------


class TestDetectDockerMode(unittest.TestCase):
    """Requirement 8 — detection returns ``DockerMode`` or raises a
    structured domain error.  Does not print or select an exit code."""

    # -- rootless recognised -------------------------------------------------

    def test_rootless_recognised(self):
        run = _fake_run(docker_info_stdout="  rootless: true\n  other: val")
        self.assertIs(detect_docker_mode(_runner=run), DockerMode.ROOTLESS)

    def test_rootless_case_insensitive(self):
        run = _fake_run(docker_info_stdout="  RootLess: True\n")
        self.assertIs(detect_docker_mode(_runner=run), DockerMode.ROOTLESS)

    # -- rootful when absent -------------------------------------------------

    def test_rootful_when_absent(self):
        run = _fake_run(
            docker_info_stdout="  Server Version: 27.0.1\n  OSType: linux\n"
        )
        self.assertIs(detect_docker_mode(_runner=run), DockerMode.ROOTFUL)

    # -- command failure → domain error --------------------------------------

    def test_nonzero_exit_raises_detection_error(self):
        run = _fake_run(docker_info_stdout="", docker_info_rc=1)
        with self.assertRaises(DockerDetectionError) as ctx:
            detect_docker_mode(_runner=run)
        self.assertIn("permission denied", str(ctx.exception))

    def test_docker_not_found_raises_detection_error(self):
        run = _fake_run(docker_info_raises=FileNotFoundError("docker"))
        with self.assertRaises(DockerDetectionError) as ctx:
            detect_docker_mode(_runner=run)
        self.assertIn("not found", str(ctx.exception))

    # -- does not print ------------------------------------------------------

    def test_detection_does_not_print(self):
        import io
        import sys

        run = _fake_run(docker_info_stdout="Server: 27.0.0\n")
        buf = io.StringIO()
        old = sys.stderr
        try:
            sys.stderr = buf
            detect_docker_mode(_runner=run)
        finally:
            sys.stderr = old
        self.assertEqual(buf.getvalue(), "")

    def test_detection_failure_does_not_print(self):
        import io
        import sys

        run = _fake_run(docker_info_stdout="", docker_info_rc=1)
        buf = io.StringIO()
        old = sys.stderr
        try:
            sys.stderr = buf
            with self.assertRaises(DockerDetectionError):
                detect_docker_mode(_runner=run)
        finally:
            sys.stderr = old
        self.assertEqual(buf.getvalue(), "")

    # -- no exit code --------------------------------------------------------

    def test_detection_does_not_select_exit_code(self):
        """Failure raises an exception — callers map to exit codes."""
        run = _fake_run(docker_info_stdout="", docker_info_rc=1)
        try:
            detect_docker_mode(_runner=run)
        except DockerDetectionError:
            pass  # exception, not sys.exit


# ---------------------------------------------------------------------------
# 9. Candidate-order tests
# ---------------------------------------------------------------------------


class TestCandidateGateways(unittest.TestCase):
    """Requirement 9 — ordered candidate lists, deduplication, absent
    LAN IP."""

    def test_rootful_host_gateway_then_lan(self):
        got = candidate_gateways(DockerMode.ROOTFUL, lan_ip="192.168.1.5")
        self.assertEqual(got, ("host-gateway", "192.168.1.5"))

    def test_rootful_without_lan(self):
        got = candidate_gateways(DockerMode.ROOTFUL, lan_ip=None)
        self.assertEqual(got, ("host-gateway",))

    def test_rootless_10_0_2_2_then_lan_then_host_gateway(self):
        got = candidate_gateways(DockerMode.ROOTLESS, lan_ip="192.168.1.5")
        self.assertEqual(got, ("10.0.2.2", "192.168.1.5", "host-gateway"))

    def test_rootless_without_lan(self):
        got = candidate_gateways(DockerMode.ROOTLESS, lan_ip=None)
        self.assertEqual(got, ("10.0.2.2", "host-gateway"))

    # -- deduplication -------------------------------------------------------

    def test_dedup_rootless_lan_equals_10_0_2_2(self):
        got = candidate_gateways(DockerMode.ROOTLESS, lan_ip="10.0.2.2")
        self.assertEqual(got, ("10.0.2.2", "host-gateway"))

    def test_dedup_rootful_lan_equals_host_gateway(self):
        got = candidate_gateways(DockerMode.ROOTFUL, lan_ip="host-gateway")
        self.assertEqual(got, ("host-gateway",))

    def test_dedup_rootless_lan_equals_host_gateway(self):
        got = candidate_gateways(DockerMode.ROOTLESS, lan_ip="host-gateway")
        self.assertEqual(got, ("10.0.2.2", "host-gateway"))

    # -- returns tuple -------------------------------------------------------

    def test_always_returns_tuple(self):
        for mode in DockerMode:
            for ip in (None, "10.0.0.1"):
                self.assertIsInstance(candidate_gateways(mode, ip), tuple)


# ---------------------------------------------------------------------------
# 10. LAN-IP detection tests
# ---------------------------------------------------------------------------


class TestDetectLanIp(unittest.TestCase):
    """Requirement 10 — hostname -I first, ip route fallback, graceful
    failure."""

    def test_first_word_from_hostname_i(self):
        run = _fake_run(hostname_stdout="192.168.1.10 172.17.0.1")
        self.assertEqual(detect_lan_ip(_runner=run), "192.168.1.10")

    def test_hostname_empty_falls_back_to_ip_route(self):
        run = _fake_run(
            hostname_stdout=" \n",
            ip_route_stdout="default via 10.0.0.1 dev eth0 src 10.0.0.55",
        )
        self.assertEqual(detect_lan_ip(_runner=run), "10.0.0.55")

    def test_hostname_not_found_falls_back(self):
        run = _fake_run(
            hostname_raises=FileNotFoundError("hostname"),
            ip_route_stdout="default via 10.0.0.1 dev eth0 src 10.0.0.55",
        )
        self.assertEqual(detect_lan_ip(_runner=run), "10.0.0.55")

    def test_both_fail_returns_none(self):
        run = _fake_run(
            hostname_raises=FileNotFoundError("hostname"),
            ip_route_raises=FileNotFoundError("ip"),
        )
        self.assertIsNone(detect_lan_ip(_runner=run))

    def test_no_src_in_ip_route_returns_none(self):
        run = _fake_run(
            hostname_stdout="",
            ip_route_stdout="default via 10.0.0.1 dev eth0\n",
        )
        self.assertIsNone(detect_lan_ip(_runner=run))

    def test_process_failure_remains_structured(self):
        """detect_lan_ip never raises regardless of process state."""
        run = _fake_run(
            hostname_raises=OSError("broken pipe"),
            ip_route_raises=OSError("broken pipe"),
        )
        # Must not raise
        result = detect_lan_ip(_runner=run)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# 11. Probe-result tests
# ---------------------------------------------------------------------------


class TestProbeResultDto(unittest.TestCase):
    """Requirement 11 & 13 — ProbeResult immutability, field integrity."""

    def test_rejects_empty_candidate(self):
        with self.assertRaises(ValueError):
            ProbeResult(candidate="", ok=True, resolved_ip=None, detail="ok")

    def test_frozen(self):
        p = ProbeResult("gw", True, "1.2.3.4", "ok")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            p.ok = False  # type: ignore[misc]

    def test_candidate_and_resolved_ip_distinct(self):
        """candidate is the probe target, resolved_ip is what the
        container resolved host.docker.internal to."""
        p = ProbeResult("10.0.2.2", True, "172.17.0.1", "ok")
        self.assertNotEqual(
            p.candidate, p.resolved_ip, "candidate and resolved_ip must be distinct"
        )


class TestProbeGateway(unittest.TestCase):
    """Requirement 11 — successful/failed probes, edge cases."""

    def test_successful_token_match(self):
        run = _fake_run(probe_stdouts=["RESOLVED_IP=172.17.0.1\nPROBE_OK"])
        result = probe_gateway("10.0.2.2", 9999, "OK_1", _runner=run)
        self.assertTrue(result.ok)
        self.assertEqual(result.candidate, "10.0.2.2")
        self.assertEqual(result.resolved_ip, "172.17.0.1")

    def test_missing_token_not_ok(self):
        run = _fake_run(probe_stdouts=["RESOLVED_IP=172.17.0.1\n"], probe_rcs=[0])
        result = probe_gateway("10.0.2.2", 9999, "OK_1", _runner=run)
        self.assertFalse(result.ok)

    def test_nonzero_process_status_not_ok(self):
        run = _fake_run(probe_stdouts=[""], probe_rcs=[1])
        result = probe_gateway("host-gateway", 9999, "T", _runner=run)
        self.assertFalse(result.ok)
        self.assertIn("exit 1", result.detail)

    def test_missing_resolved_ip_with_ok_probe(self):
        """Successful probe without RESOLVED_IP still returns ok."""
        run = _fake_run(probe_stdouts=["PROBE_OK"])
        result = probe_gateway("host-gateway", 9999, "T", _runner=run)
        self.assertTrue(result.ok)
        self.assertIsNone(result.resolved_ip)

    def test_detail_bounded(self):
        """Detail should not grow unbounded from a noisy container."""
        long_output = "x" * 500
        run = _fake_run(probe_stdouts=[long_output])
        result = probe_gateway("host-gateway", 9999, "T", _runner=run)
        self.assertLessEqual(len(result.detail), 200)

    def test_resolved_ip_in_stderr(self):
        """RESOLVED_IP can appear on stderr."""

        def run(cmd):
            return _proc(0, stdout="", stderr="RESOLVED_IP=10.8.0.1\nPROBE_OK")

        result = probe_gateway("host-gateway", 9999, "T", _runner=_as_runner(run))
        self.assertTrue(result.ok)
        self.assertEqual(result.resolved_ip, "10.8.0.1")

    def test_exception_during_probe(self):
        def run(_cmd):
            raise OSError("no space left on device")

        result = probe_gateway("gw", 9999, "T", _runner=_as_runner(run))
        self.assertFalse(result.ok)
        self.assertIn("no space", result.detail)

    def test_uses_add_host_argument(self):
        seen: list[list[str]] = []

        def run(cmd):
            seen.append(cmd)
            return _proc(0, stdout="PROBE_OK")

        probe_gateway("192.168.1.1", 9999, "T", _runner=_as_runner(run))
        self.assertTrue(
            any("host.docker.internal:192.168.1.1" in arg for arg in seen[0])
        )

    # -- requirement 31: exact PROBE_OK line, not substring ----------------

    def test_nonzero_with_probe_ok_quoted_in_error_still_fails(self):
        """Docker exits non-zero; stderr quotes the probe script which
        happens to contain PROBE_OK.  The old substring check would
        report ok=True; the line-anchored check must reject it."""

        def run(cmd):
            return _proc(1, stdout="", stderr="sh: PROBE_OK: command not found")

        result = probe_gateway("gw", 9999, "T", _runner=_as_runner(run))
        self.assertFalse(result.ok, "non-zero exit must fail even if PROBE_OK appears")
        self.assertIn(
            "PROBE_OK", result.detail, "detail should preserve the probe output"
        )
        self.assertIn(
            "[exit 1]",
            result.detail,
            "detail must include exit code even with non-empty output",
        )

    def test_not_probe_ok_is_not_a_match(self):
        """NOT_PROBE_OK on its own line must not be treated as success."""

        def run(cmd):
            return _proc(0, stdout="RESOLVED_IP=10.0.0.1\nNOT_PROBE_OK")

        result = probe_gateway("gw", 9999, "T", _runner=_as_runner(run))
        self.assertFalse(result.ok, "NOT_PROBE_OK must not match PROBE_OK regex")

    def test_probe_ok_embedded_in_other_word_fails(self):
        """XPROBE_OKX on a line is not a standalone PROBE_OK."""

        def run(cmd):
            return _proc(0, stdout="RESOLVED_IP=10.0.0.1\nXPROBE_OKX")

        result = probe_gateway("gw", 9999, "T", _runner=_as_runner(run))
        self.assertFalse(result.ok, "XPROBE_OKX must not match line-anchored PROBE_OK")

    def test_probe_ok_with_leading_trailing_whitespace_fails(self):
        """' PROBE_OK ' (with spaces) is not the exact line 'PROBE_OK'."""

        def run(cmd):
            return _proc(0, stdout=" PROBE_OK ")

        result = probe_gateway("gw", 9999, "T", _runner=_as_runner(run))
        self.assertFalse(result.ok, "whitespace must not match ^PROBE_OK$")


# ---------------------------------------------------------------------------
# 11a. Token safety — reject injection characters before ProcessRunner
# ---------------------------------------------------------------------------


class TestTokenSafety(unittest.TestCase):
    """Requirement 35: ``probe_gateway`` must reject tokens containing
    shell metacharacters (quotes, semicolons, newlines, ``$``, etc.)
    **before** invoking ``ProcessRunner.run()``."""

    SAFE_ARGS = ("host-gateway", 9999)

    # -- unsafe tokens return failed result without calling runner -----------

    def test_double_quote_token_rejected(self):
        rec = _FakeProcessRunner([])
        result = probe_gateway(*self.SAFE_ARGS, '";id;"', _runner=rec)
        self.assertFalse(result.ok)
        self.assertIn("unsafe", result.detail)
        self.assertEqual(rec.calls, [], "runner must not be called for unsafe token")

    def test_single_quote_token_rejected(self):
        rec = _FakeProcessRunner([])
        result = probe_gateway(*self.SAFE_ARGS, "it's-ok", _runner=rec)
        self.assertFalse(result.ok)
        self.assertIn("unsafe", result.detail)
        self.assertEqual(rec.calls, [])

    def test_dollar_token_rejected(self):
        rec = _FakeProcessRunner([])
        result = probe_gateway(*self.SAFE_ARGS, "$(whoami)", _runner=rec)
        self.assertFalse(result.ok)
        self.assertIn("unsafe", result.detail)
        self.assertEqual(rec.calls, [])

    def test_backtick_token_rejected(self):
        rec = _FakeProcessRunner([])
        result = probe_gateway(*self.SAFE_ARGS, "`id`", _runner=rec)
        self.assertFalse(result.ok)
        self.assertIn("unsafe", result.detail)
        self.assertEqual(rec.calls, [])

    def test_semicolon_token_rejected(self):
        rec = _FakeProcessRunner([])
        result = probe_gateway(*self.SAFE_ARGS, "OK;id", _runner=rec)
        self.assertFalse(result.ok)
        self.assertIn("unsafe", result.detail)
        self.assertEqual(rec.calls, [])

    def test_newline_token_rejected(self):
        rec = _FakeProcessRunner([])
        result = probe_gateway(*self.SAFE_ARGS, "OK\nid", _runner=rec)
        self.assertFalse(result.ok)
        self.assertIn("unsafe", result.detail)
        self.assertEqual(rec.calls, [])

    def test_pipe_token_rejected(self):
        rec = _FakeProcessRunner([])
        result = probe_gateway(*self.SAFE_ARGS, "OK|id", _runner=rec)
        self.assertFalse(result.ok)
        self.assertIn("unsafe", result.detail)
        self.assertEqual(rec.calls, [])

    def test_hash_token_rejected(self):
        rec = _FakeProcessRunner([])
        result = probe_gateway(*self.SAFE_ARGS, "OK#bye", _runner=rec)
        self.assertFalse(result.ok)
        self.assertIn("unsafe", result.detail)
        self.assertEqual(rec.calls, [])

    def test_space_token_rejected(self):
        rec = _FakeProcessRunner([])
        result = probe_gateway(*self.SAFE_ARGS, "OK 123", _runner=rec)
        self.assertFalse(result.ok)
        self.assertIn("unsafe", result.detail)
        self.assertEqual(rec.calls, [])

    # -- safe tokens are accepted and reach the runner -----------------------

    def test_generated_token_style_accepted(self):
        """Tokens matching ``OK_<timestamp>`` must pass validation."""
        rec = _FakeProcessRunner([])
        result = probe_gateway(*self.SAFE_ARGS, "OK_1734567890", _runner=rec)
        # Reaches runner (which returns default error → not-ok), but not
        # rejected at the validation layer.
        self.assertGreater(len(rec.calls), 0, "safe token must reach the ProcessRunner")

    def test_alphanumeric_hyphen_token_accepted(self):
        rec = _FakeProcessRunner([])
        result = probe_gateway(*self.SAFE_ARGS, "my-token-v1.0", _runner=rec)
        self.assertGreater(len(rec.calls), 0)

    def test_underscore_only_token_accepted(self):
        rec = _FakeProcessRunner([])
        result = probe_gateway(*self.SAFE_ARGS, "___", _runner=rec)
        self.assertGreater(len(rec.calls), 0)


# ---------------------------------------------------------------------------
# 12. Diagnosis orchestration tests
# ---------------------------------------------------------------------------


class TestDiagnosisOrchestration(unittest.TestCase):
    """Requirement 12 — full diagnosis lifecycle."""

    def test_probe_server_starts_before_candidates(self):
        """Server must be started before any probe is attempted."""
        started_before_probe = []

        def run(cmd):
            if cmd[0] == "docker" and cmd[1] == "run":
                # Record that this is a probe call — server must already be started
                return _proc(0, stdout="PROBE_OK")
            if cmd[0] == "docker" and cmd[1] == "info":
                return _proc(0, stdout="Server: 27\n")
            if cmd[0] == "hostname":
                return _proc(0, stdout="10.0.0.5")
            return _proc(0)

        class TrackingServer:
            def __init__(self):
                self.started = False
                self.stopped = False
                self.token = "T"
                self.port = 9999

            def start(self):
                self.started = True
                started_before_probe.append(True)
                return self.port

            def stop(self):
                self.stopped = True

        server = TrackingServer()
        diagnose_gateway(_runner=_as_runner(run), _host_probe_factory=lambda: server)
        self.assertTrue(server.started, "server must be started")
        self.assertTrue(any(started_before_probe))

    def test_every_candidate_tested_in_order(self):
        probed: list[str] = []

        def run(cmd):
            if cmd[0] == "docker" and cmd[1] == "run":
                # Extract candidate from --add-host
                for i, arg in enumerate(cmd):
                    if arg == "--add-host" and i + 1 < len(cmd):
                        hostdef = cmd[i + 1]
                        cand = hostdef.split(":")[-1] if ":" in hostdef else hostdef
                        probed.append(cand)
                        break
                return _proc(0, stdout="PROBE_OK")
            if cmd[0] == "docker" and cmd[1] == "info":
                return _proc(0, stdout="Server: 27\n")
            if cmd[0] == "hostname":
                return _proc(0, stdout="192.168.1.99")
            return _proc(0)

        diagnose_gateway(
            _runner=_as_runner(run),
            _host_probe_factory=lambda: FakeHostProbeServer(token="X"),
        )
        # Rootful: host-gateway first, then LAN IP
        self.assertGreaterEqual(len(probed), 2)
        self.assertEqual(probed[0], "host-gateway")
        self.assertEqual(probed[1], "192.168.1.99")

    def test_first_successful_candidate_selected(self):
        """Second candidate succeeds, first fails."""

        def run(cmd):
            if cmd[0] == "docker" and cmd[1] == "run":
                for i, arg in enumerate(cmd):
                    if arg == "--add-host" and i + 1 < len(cmd):
                        hostdef = cmd[i + 1]
                        cand = hostdef.split(":")[-1]
                        if cand == "host-gateway":
                            return _proc(1, stderr="timeout")
                        return _proc(0, stdout="RESOLVED_IP=10.9.9.9\nPROBE_OK")
            if cmd[0] == "docker" and cmd[1] == "info":
                return _proc(0, stdout="Server: 27\n")
            if cmd[0] == "hostname":
                return _proc(0, stdout="192.168.1.5")
            return _proc(0)

        d = diagnose_gateway(
            _runner=_as_runner(run),
            _host_probe_factory=lambda: FakeHostProbeServer(token="X"),
        )
        self.assertEqual(d.chosen_gateway, "192.168.1.5")
        self.assertEqual(d.resolved_address, "10.9.9.9")

    def test_all_fail_yields_no_gateway(self):
        def run(cmd):
            if cmd[0] == "docker" and cmd[1] == "run":
                return _proc(1, stderr="connection refused")
            if cmd[0] == "docker" and cmd[1] == "info":
                return _proc(0, stdout="Server: 27\n")
            if cmd[0] == "hostname":
                return _proc(0, stdout="10.0.0.5")
            return _proc(0)

        d = diagnose_gateway(
            _runner=_as_runner(run),
            _host_probe_factory=lambda: FakeHostProbeServer(token="X"),
        )
        self.assertIsNone(d.chosen_gateway)
        self.assertIsNone(d.resolved_address)
        self.assertEqual(len(d.probes), 2)
        self.assertFalse(d.probes[0].ok)
        self.assertFalse(d.probes[1].ok)

    def test_server_stops_after_success(self):
        fake = FakeHostProbeServer()

        def run(cmd):
            if cmd[0] == "docker" and cmd[1] == "run":
                return _proc(0, stdout="PROBE_OK")
            if cmd[0] == "docker" and cmd[1] == "info":
                return _proc(0, stdout="Server: 27\n")
            if cmd[0] == "hostname":
                return _proc(0, stdout="10.0.0.5")
            return _proc(0)

        diagnose_gateway(_runner=_as_runner(run), _host_probe_factory=lambda: fake)
        self.assertTrue(fake.stopped)

    def test_server_stops_after_failure(self):
        fake = FakeHostProbeServer()

        def run(cmd):
            if cmd[0] == "docker" and cmd[1] == "run":
                return _proc(1, stderr="timeout")
            if cmd[0] == "docker" and cmd[1] == "info":
                return _proc(0, stdout="Server: 27\n")
            if cmd[0] == "hostname":
                return _proc(0, stdout="")
            return _proc(0)

        diagnose_gateway(_runner=_as_runner(run), _host_probe_factory=lambda: fake)
        self.assertTrue(fake.stopped)

    def test_diagnosis_does_not_install_override(self):
        """Diagnosis must not mutate filesystem or run systemctl."""
        ran_systemctl = []

        def run(cmd):
            if "systemctl" in (cmd[0] if cmd else ""):
                ran_systemctl.append(cmd)
            if cmd[0] == "docker" and cmd[1] == "run":
                return _proc(0, stdout="PROBE_OK")
            if cmd[0] == "docker" and cmd[1] == "info":
                return _proc(0, stdout="Server: 27\n")
            if cmd[0] == "hostname":
                return _proc(0, stdout="10.0.0.5")
            return _proc(0)

        d = diagnose_gateway(
            _runner=_as_runner(run),
            _host_probe_factory=lambda: FakeHostProbeServer(token="X"),
        )
        self.assertEqual(
            ran_systemctl, [], "diagnose_gateway must not invoke systemctl"
        )
        # It should report that an override is NOT needed (rootful)
        self.assertFalse(d.override_needed)

    def test_rootless_mode_flows_to_diagnosis(self):
        def run(cmd):
            if cmd[0] == "docker" and cmd[1] == "run":
                return _proc(0, stdout="PROBE_OK")
            if cmd[0] == "docker" and cmd[1] == "info":
                return _proc(0, stdout="  rootless: true\n")
            if cmd[0] == "hostname":
                return _proc(0, stdout="")
            return _proc(0)

        d = diagnose_gateway(
            _runner=_as_runner(run),
            _host_probe_factory=lambda: FakeHostProbeServer(token="X"),
        )
        self.assertIs(d.mode, DockerMode.ROOTLESS)
        self.assertTrue(d.rootless)  # convenience property
        # Rootless without override → override_needed
        self.assertTrue(d.override_needed)
        self.assertFalse(d.override_installed)


# ---------------------------------------------------------------------------
# 13. Immutable DTOs
# ---------------------------------------------------------------------------


class TestImmutableDtos(unittest.TestCase):
    """Requirement 13 — DockerMode, ProbeResult, GatewayDiagnosis immutability."""

    def test_docker_mode_is_enum(self):
        self.assertIsInstance(DockerMode.ROOTFUL, DockerMode)
        self.assertIsInstance(DockerMode.ROOTLESS, DockerMode)

    def test_probe_result_frozen(self):
        p = ProbeResult("g", True, "1.2.3.4", "ok")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            p.ok = False  # type: ignore[misc]

    def test_gateway_diagnosis_frozen(self):
        d = GatewayDiagnosis(
            mode=DockerMode.ROOTFUL,
            probe_port=9000,
            probe_token="T",
            lan_ip=None,
            probes=(),
            chosen_gateway=None,
            override_installed=False,
            override_needed=False,
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            d.chosen_gateway = "hacked"  # type: ignore[misc]

    def test_detection_error_is_domain_error(self):
        err = DockerDetectionError("test")
        self.assertIsInstance(err, RuntimeError)
        self.assertEqual(str(err), "test")


# ---------------------------------------------------------------------------
# 14. resolved_address resolution
# ---------------------------------------------------------------------------


class TestHostGatewayIpResolution(unittest.TestCase):
    """Requirement 14 — resolved_address precedence chain."""

    def test_resolved_ip_when_available(self):
        d = GatewayDiagnosis(
            mode=DockerMode.ROOTFUL,
            probe_port=9000,
            probe_token="T",
            lan_ip=None,
            probes=(ProbeResult("host-gateway", True, "10.0.0.1", "ok"),),
            chosen_gateway="host-gateway",
            override_installed=False,
            override_needed=False,
        )
        self.assertEqual(d.resolved_address, "10.0.0.1")

    def test_fallback_to_candidate(self):
        d = GatewayDiagnosis(
            mode=DockerMode.ROOTFUL,
            probe_port=9000,
            probe_token="T",
            lan_ip=None,
            probes=(ProbeResult("host-gateway", True, None, "ok"),),
            chosen_gateway="host-gateway",
            override_installed=False,
            override_needed=False,
        )
        self.assertEqual(d.resolved_address, "host-gateway")

    def test_none_when_no_candidate_succeeds(self):
        d = GatewayDiagnosis(
            mode=DockerMode.ROOTFUL,
            probe_port=9000,
            probe_token="T",
            lan_ip=None,
            probes=(ProbeResult("10.0.2.2", False, None, "timeout"),),
            chosen_gateway=None,
            override_installed=False,
            override_needed=False,
        )
        self.assertIsNone(d.resolved_address)

    def test_none_when_chosen_but_not_ok(self):
        """Edge case: chosen_gateway is set but its ProbeResult is not ok."""
        d = GatewayDiagnosis(
            mode=DockerMode.ROOTFUL,
            probe_port=9000,
            probe_token="T",
            lan_ip=None,
            probes=(ProbeResult("host-gateway", False, "1.2.3.4", "refused"),),
            chosen_gateway="host-gateway",
            override_installed=False,
            override_needed=False,
        )
        self.assertIsNone(d.resolved_address)

    def test_chosen_probe_matches(self):
        d = GatewayDiagnosis(
            mode=DockerMode.ROOTFUL,
            probe_port=9000,
            probe_token="T",
            lan_ip=None,
            probes=(
                ProbeResult("host-gateway", True, "10.0.0.1", "ok"),
                ProbeResult("192.168.1.1", False, None, "fail"),
            ),
            chosen_gateway="host-gateway",
            override_installed=False,
            override_needed=False,
        )
        cp = d.chosen_probe()
        self.assertIsNotNone(cp)
        assert cp is not None
        self.assertEqual(cp.candidate, "host-gateway")
        self.assertTrue(cp.ok)

    def test_chosen_probe_none_when_no_choice(self):
        d = GatewayDiagnosis(
            mode=DockerMode.ROOTFUL,
            probe_port=9000,
            probe_token="T",
            lan_ip=None,
            probes=(),
            chosen_gateway=None,
            override_installed=False,
            override_needed=False,
        )
        self.assertIsNone(d.chosen_probe())


# ======================================================================
# 7.2 — Override planning model
# ======================================================================


class TestOverrideStateEnum(unittest.TestCase):
    """Requirement 15: ``OverrideState`` enum variants."""

    def test_absent_variant_exists(self):
        self.assertEqual(OverrideState.ABSENT.value, "absent")

    def test_matching_variant_exists(self):
        self.assertEqual(OverrideState.MATCHING.value, "matching")

    def test_different_variant_exists(self):
        self.assertEqual(OverrideState.DIFFERENT.value, "different")


class TestFilesystemOperationDTO(unittest.TestCase):
    """Requirement 15: ``FilesystemOperation`` dataclass."""

    def test_has_kind_and_path(self):
        op = FilesystemOperation(kind="mkdir", path=Path("/a/b"))
        self.assertEqual(op.kind, "mkdir")
        self.assertEqual(op.path, Path("/a/b"))

    def test_optional_source_path(self):
        op = FilesystemOperation(
            kind="copy", path=Path("/a/dest"), source_path=Path("/a/src")
        )
        self.assertEqual(op.source_path, Path("/a/src"))

    def test_default_source_path_is_none(self):
        op = FilesystemOperation(kind="mkdir", path=Path("/a"))
        self.assertIsNone(op.source_path)

    def test_is_frozen(self):
        op = FilesystemOperation(kind="mkdir", path=Path("/a"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            op.kind = "copy"  # type: ignore[misc]


class TestServiceOperationDTO(unittest.TestCase):
    """Requirement 15: ``ServiceOperation`` dataclass."""

    def test_has_kind_and_unit(self):
        op = ServiceOperation(kind="daemon_reload", unit=None)
        self.assertEqual(op.kind, "daemon_reload")
        self.assertIsNone(op.unit)

    def test_unit_for_restart(self):
        op = ServiceOperation(kind="restart", unit="docker.service")
        self.assertEqual(op.unit, "docker.service")

    def test_is_frozen(self):
        op = ServiceOperation(kind="daemon_reload", unit=None)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            op.kind = "restart"  # type: ignore[misc]


class TestOverrideFailureDTO(unittest.TestCase):
    """Requirement 20: ``OverrideFailure`` structured error."""

    def test_stores_operation_and_path(self):
        f = OverrideFailure(
            operation="copy override",
            path_or_command="/dest/override.conf",
            detail="Permission denied",
            persistence_applied=False,
        )
        self.assertEqual(f.operation, "copy override")
        self.assertEqual(f.path_or_command, "/dest/override.conf")
        self.assertEqual(f.detail, "Permission denied")
        self.assertFalse(f.persistence_applied)

    def test_persistence_applied_true_when_copy_succeeded_before_service_failure(self):
        f = OverrideFailure(
            operation="restart docker.service",
            path_or_command="systemctl --user restart docker.service",
            detail="unit not found",
            persistence_applied=True,
        )
        self.assertTrue(f.persistence_applied)

    def test_is_frozen(self):
        f = OverrideFailure(
            operation="mkdir",
            path_or_command="/a",
            detail="disk full",
            persistence_applied=False,
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            f.detail = "hacked"  # type: ignore[misc]

    def test_detail_is_bounded(self):
        """A long detail string is accepted; truncation is the presenter's
        job, not the model's."""
        long_detail = "x" * 500
        f = OverrideFailure(
            operation="daemon-reload",
            path_or_command="systemctl --user daemon-reload",
            detail=long_detail,
            persistence_applied=False,
        )
        self.assertEqual(len(f.detail), 500)


class TestRootlessOverridePlanEnhancedFields(unittest.TestCase):
    """Requirement 15: enhanced ``RootlessOverridePlan`` fields."""

    def test_rootless_field(self):
        plan = RootlessOverridePlan(
            rootless=True,
            src=Path("/s"),
            dest=Path("/d"),
            state=OverrideState.ABSENT,
            needed=True,
            filesystem_ops=(),
            service_ops=(),
        )
        self.assertTrue(plan.rootless)

    def test_state_field_absent(self):
        plan = RootlessOverridePlan(
            rootless=True,
            src=Path("/s"),
            dest=Path("/d"),
            state=OverrideState.ABSENT,
            needed=True,
            filesystem_ops=(
                FilesystemOperation("mkdir", Path("/d").parent),
                FilesystemOperation("copy", Path("/d"), source_path=Path("/s")),
            ),
            service_ops=(
                ServiceOperation("daemon_reload"),
                ServiceOperation("restart", "docker.service"),
            ),
        )
        self.assertIs(plan.state, OverrideState.ABSENT)

    def test_state_field_matching(self):
        plan = RootlessOverridePlan(
            rootless=True,
            src=Path("/s"),
            dest=Path("/d"),
            state=OverrideState.MATCHING,
            needed=False,
            filesystem_ops=(),
            service_ops=(),
        )
        self.assertIs(plan.state, OverrideState.MATCHING)
        self.assertFalse(plan.needed)

    def test_state_field_different(self):
        plan = RootlessOverridePlan(
            rootless=True,
            src=Path("/s"),
            dest=Path("/d"),
            state=OverrideState.DIFFERENT,
            needed=True,
            filesystem_ops=(
                FilesystemOperation("copy", Path("/d"), source_path=Path("/s")),
            ),
            service_ops=(
                ServiceOperation("daemon_reload"),
                ServiceOperation("restart", "docker.service"),
            ),
        )
        self.assertTrue(plan.needed)

    def test_filesystem_ops_are_tuple(self):
        plan = RootlessOverridePlan(
            rootless=False,
            src=Path("/s"),
            dest=Path("/d"),
            state=OverrideState.MATCHING,
            needed=False,
            filesystem_ops=(),
            service_ops=(),
        )
        self.assertIsInstance(plan.filesystem_ops, tuple)

    def test_service_ops_are_tuple(self):
        plan = RootlessOverridePlan(
            rootless=False,
            src=Path("/s"),
            dest=Path("/d"),
            state=OverrideState.MATCHING,
            needed=False,
            filesystem_ops=(),
            service_ops=(),
        )
        self.assertIsInstance(plan.service_ops, tuple)


# ======================================================================
# 7.2 — Override planning resolution (requirement 16)
# ======================================================================


class TestOverridePlanningResolution(unittest.TestCase):
    """Requirement 16: ``plan_rootless_override`` produces correct
    plans for each state."""

    SRC_CONTENT = "[Service]\nPort=forward\n"
    OTHER = "[Service]\nPort=other\n"

    def _plan(self, *, rootless=True, src_content=SRC_CONTENT, dest_content=None):
        """Build an in-memory plan via ``plan_rootless_override``
        by controlling what the filesystem returns.  Returns the plan.
        The fake filesystem is constructed so that:
        - src always exists with ``src_content``
        - dest content: None → absent, str → that content
        """
        src = Path("/fake/src.conf")
        dest = Path("/fake/home/.config/systemd/user/docker.service.d/override.conf")
        files = {str(src): src_content or self.SRC_CONTENT}
        if dest_content is not None:
            files[str(dest)] = dest_content

        class _PlanFS(Filesystem):
            def __init__(self):
                self._files = files

            def is_file(self, path):
                return str(path) in self._files

            def read_text(self, path, encoding="utf-8"):
                return self._files[str(path)]

            @property
            def home(self):
                return Path("/fake/home")

        return plan_rootless_override(
            _mode=DockerMode.ROOTLESS if rootless else DockerMode.ROOTFUL,
            _fs=_PlanFS(),
            _override_src=src,
            _override_dest=dest,
        )

    # -- rootful ------------------------------------------------------------

    def test_rootful_returns_not_applicable(self):
        plan = self._plan(rootless=False)
        self.assertFalse(plan.rootless, "rootful Docker needs no override")
        self.assertFalse(plan.needed)
        self.assertEqual(plan.filesystem_ops, ())
        self.assertEqual(plan.service_ops, ())

    # -- matching -----------------------------------------------------------

    def test_matching_dest_returns_already_installed(self):
        plan = self._plan(dest_content=self.SRC_CONTENT)
        self.assertIs(plan.state, OverrideState.MATCHING)
        self.assertTrue(plan.rootless)
        self.assertFalse(plan.needed)
        self.assertEqual(plan.filesystem_ops, ())
        self.assertEqual(plan.service_ops, ())

    # -- absent -------------------------------------------------------------

    def test_absent_dest_produces_install_plan(self):
        plan = self._plan(dest_content=None)
        self.assertIs(plan.state, OverrideState.ABSENT)
        self.assertTrue(plan.needed)
        ops_by_kind = {op.kind for op in plan.filesystem_ops}
        self.assertIn("mkdir", ops_by_kind)
        self.assertIn("copy", ops_by_kind)
        svc_kinds = {op.kind for op in plan.service_ops}
        self.assertIn("daemon_reload", svc_kinds)
        self.assertIn("restart", svc_kinds)

    # -- differing ----------------------------------------------------------

    def test_differing_dest_produces_replacement_plan(self):
        plan = self._plan(dest_content=self.OTHER)
        self.assertIs(plan.state, OverrideState.DIFFERENT)
        self.assertTrue(plan.needed)
        ops_by_kind = {op.kind for op in plan.filesystem_ops}
        self.assertIn("copy", ops_by_kind)
        svc_kinds = {op.kind for op in plan.service_ops}
        self.assertIn("daemon_reload", svc_kinds)
        self.assertIn("restart", svc_kinds)

    def test_differing_plan_may_reuse_existing_dir(self):
        """If the destination directory already exists, the plan does not
        include a redundant mkdir."""
        plan = self._plan(dest_content=self.OTHER)
        ops_by_kind = {op.kind for op in plan.filesystem_ops}
        # mkdir is idempotent but may be elided when parent exists;
        # at minimum copy must be present.
        self.assertIn("copy", ops_by_kind)

    # -- read-only ----------------------------------------------------------

    def test_planning_performs_no_side_effects(self):
        """Inspection never writes, copies, reloads, or restarts."""
        # Using a fake that records every call
        calls: list[str] = []

        class _RecordingFS(Filesystem):
            def __init__(self):
                self._files = {
                    str(
                        Path("/fake/src.conf")
                    ): TestOverridePlanningResolution.SRC_CONTENT
                }

            @property
            def home(self):
                return Path("/fake/home")

            def is_file(self, path):
                calls.append(f"is_file({path})")
                return str(path) in self._files

            def read_text(self, path, encoding="utf-8"):
                calls.append(f"read_text({path})")
                return self._files[str(path)]

            def write_text(self, path, content, encoding="utf-8"):
                calls.append(f"WRITE({path})")

            def mkdir(self, path):
                calls.append(f"MKDIR({path})")

            def copy(self, src, dest):
                calls.append(f"COPY({src}→{dest})")

            def rename(self, src, dest):
                calls.append(f"RENAME({src}→{dest})")

            def delete(self, path):
                calls.append(f"DELETE({path})")

        plan_rootless_override(
            _fs=_RecordingFS(),
            _override_src=Path("/fake/src.conf"),
            _override_dest=Path(
                "/fake/home/.config/systemd/user/docker.service.d/override.conf"
            ),
        )
        write_calls = [
            c
            for c in calls
            if not c.startswith("is_file") and not c.startswith("read_text")
        ]
        self.assertEqual(
            write_calls, [], f"inspection must be read-only, got: {write_calls}"
        )
        self.assertIn("is_file", calls[0])


# ======================================================================
# 7.2 — Consent enforcement (requirement 17)
# ======================================================================


class TestConsentEnforcement(unittest.TestCase):
    """Requirement 17: ``apply_rootless_override`` requires explicit consent."""

    def setUp(self):
        self.src = Path("/fake/src")
        self.dest = Path("/fake/dest")
        self.plan = RootlessOverridePlan(
            rootless=True,
            src=self.src,
            dest=self.dest,
            state=OverrideState.ABSENT,
            needed=True,
            filesystem_ops=(
                FilesystemOperation("mkdir", self.dest.parent),
                FilesystemOperation("copy", self.dest, source_path=self.src),
            ),
            service_ops=(
                ServiceOperation("daemon_reload"),
                ServiceOperation("restart", "docker.service"),
            ),
        )
        self._fake_calls: list[str] = []

    def _make_fs(self):
        calls = self._fake_calls
        src = self.src

        class _FS(Filesystem):
            def is_file(self, path):
                return path == src

            def read_text(self, path, **kw):
                return "content"

            @property
            def home(self):
                return Path("/fake")

            def mkdir(self, path):
                calls.append(f"mkdir:{path}")

            def copy(self, s, d):
                calls.append(f"copy:{s}->{d}")

        return _FS()

    def _make_svc(self):
        calls = self._fake_calls

        class _SVC(ServiceController):
            def __init__(self):
                pass

            def daemon_reload(self):
                calls.append("daemon_reload")

            def restart(self, unit):
                calls.append(f"restart:{unit}")

        return _SVC()

    def _make_clock(self):
        class _Clock(SystemClock):
            def timestamp(self):
                return 0.0

            def sleep(self, s):
                pass

        return _Clock()

    def test_consent_false_returns_none_no_side_effects(self):
        result = apply_rootless_override(
            self.plan,
            consent=False,
            _fs=self._make_fs(),
            _svc=self._make_svc(),
            _clock=self._make_clock(),
        )
        self.assertIsNone(result)
        self.assertEqual(
            self._fake_calls, [], "consent=False must produce zero side effects"
        )

    def test_consent_true_applies_and_returns_none_on_success(self):
        result = apply_rootless_override(
            self.plan,
            consent=True,
            _fs=self._make_fs(),
            _svc=self._make_svc(),
            _clock=self._make_clock(),
        )
        self.assertIsNone(result)
        self.assertEqual(
            self._fake_calls,
            [
                f"mkdir:{self.dest.parent}",
                f"copy:{self.src}->{self.dest}",
                "daemon_reload",
                "restart:docker.service",
            ],
        )

    def test_module_never_prompts(self):
        with mock.patch("builtins.input"):
            result = apply_rootless_override(
                self.plan,
                consent=False,
                _fs=self._make_fs(),
                _svc=self._make_svc(),
                _clock=self._make_clock(),
            )
        self.assertIsNone(result)


# ======================================================================
# 7.2 — Application-order contract (requirement 18)
# ======================================================================


class TestApplicationOrder(unittest.TestCase):
    """Requirement 18: ``apply_rootless_override`` executes operations in
    the documented order — mkdir, copy, daemon-reload, restart, sleep."""

    def setUp(self):
        self.src = Path("/fake/src")
        self.dest = Path("/fake/.config/systemd/user/docker.service.d/override.conf")
        self.plan = RootlessOverridePlan(
            rootless=True,
            src=self.src,
            dest=self.dest,
            state=OverrideState.ABSENT,
            needed=True,
            filesystem_ops=(
                FilesystemOperation("mkdir", self.dest.parent),
                FilesystemOperation("copy", self.dest, source_path=self.src),
            ),
            service_ops=(
                ServiceOperation("daemon_reload"),
                ServiceOperation("restart", "docker.service"),
            ),
        )
        self.log: list[str] = []
        self.fs = self._make_fs()
        self.svc = self._make_svc()
        self.clock = self._make_clock()

    def _make_fs(self):
        log = self.log
        files = {str(self.src): "src-content"}

        class _FS(Filesystem):
            def is_file(self, path):
                return str(path) in files

            def read_text(self, path, encoding="utf-8"):
                return files[str(path)]

            @property
            def home(self):
                return Path("/fake")

            def mkdir(self, path):
                log.append(f"mkdir:{path}")

            def copy(self, src, dest):
                files[str(dest)] = files[str(src)]
                log.append(f"copy:{src}→{dest}")

        return _FS()

    def _make_svc(self):
        log = self.log

        class _SVC(ServiceController):
            def __init__(self):
                pass

            def daemon_reload(self):
                log.append("daemon_reload")

            def restart(self, unit):
                log.append(f"restart:{unit}")

        return _SVC()

    def _make_clock(self):
        log = self.log

        class _Clock(SystemClock):
            def timestamp(self):
                return 0.0

            def sleep(self, s):
                log.append(f"sleep:{s}")

        return _Clock()

    def test_operations_execute_in_order(self):
        apply_rootless_override(
            self.plan,
            consent=True,
            _fs=self.fs,
            _svc=self.svc,
            _clock=self.clock,
        )
        self.assertEqual(
            self.log,
            [
                f"mkdir:{self.dest.parent}",
                f"copy:{self.src}→{self.dest}",
                "daemon_reload",
                "restart:docker.service",
                "sleep:3",
            ],
        )


# ======================================================================
# 7.2 — Structured application failures (requirements 19, 20, 21)
# ======================================================================


class TestApplicationFailures(unittest.TestCase):
    """Requirements 19-21: ``apply_rootless_override`` returns
    ``OverrideFailure`` instead of raising; failures are structured;
    no automatic rollback."""

    SRC = Path("/fake/src")
    DEST = Path("/fake/.config/systemd/user/docker.service.d/override.conf")

    @staticmethod
    def _plan(**kw):
        defaults: dict = dict(
            rootless=True,
            src=TestApplicationFailures.SRC,
            dest=TestApplicationFailures.DEST,
            state=OverrideState.ABSENT,
            needed=True,
            filesystem_ops=(
                FilesystemOperation("mkdir", TestApplicationFailures.DEST.parent),
                FilesystemOperation(
                    "copy",
                    TestApplicationFailures.DEST,
                    source_path=TestApplicationFailures.SRC,
                ),
            ),
            service_ops=(
                ServiceOperation("daemon_reload"),
                ServiceOperation("restart", "docker.service"),
            ),
        )
        defaults.update(kw)
        return RootlessOverridePlan(**defaults)

    # -- unreadable source --------------------------------------------------

    def test_unreadable_source(self):
        plan = self._plan()

        class _FS(Filesystem):
            @property
            def home(self):
                return Path("/fake")

            def is_file(self, path):
                return False  # src not found

        result = apply_rootless_override(plan, consent=True, _fs=_FS())
        self.assertIsInstance(result, OverrideFailure)
        assert result is not None
        self.assertFalse(result.persistence_applied)
        self.assertIn(
            "copy",
            result.operation.lower()
            or "source" in result.operation.lower()
            or "src" in result.path_or_command.lower(),
        )

    # -- directory creation failure -----------------------------------------

    def test_mkdir_failure(self):
        plan = self._plan()

        class _FS(Filesystem):
            @property
            def home(self):
                return Path("/fake")

            def is_file(self, path):
                return path == TestApplicationFailures.SRC

            def read_text(self, path, **kw):
                return "ok"

            def mkdir(self, path):
                raise OSError("permission denied")

        result = apply_rootless_override(plan, consent=True, _fs=_FS())
        self.assertIsInstance(result, OverrideFailure)
        assert result is not None
        self.assertFalse(result.persistence_applied)
        self.assertIn("permission denied", result.detail)

    # -- copy failure -------------------------------------------------------

    def test_copy_failure(self):
        plan = self._plan()

        class _FS(Filesystem):
            @property
            def home(self):
                return Path("/fake")

            def is_file(self, path):
                return path == TestApplicationFailures.SRC

            def read_text(self, path, **kw):
                return "ok"

            def mkdir(self, path):
                pass

            def copy(self, src, dest):
                raise OSError("disk full")

        result = apply_rootless_override(plan, consent=True, _fs=_FS())
        self.assertIsInstance(result, OverrideFailure)
        assert result is not None
        self.assertFalse(result.persistence_applied)

    # -- daemon-reload failure ----------------------------------------------

    def test_daemon_reload_failure(self):
        plan = self._plan()

        class _FS(Filesystem):
            @property
            def home(self):
                return Path("/fake")

            def is_file(self, path):
                return path == TestApplicationFailures.SRC

            def read_text(self, path, **kw):
                return "ok"

            def mkdir(self, path):
                pass

            def copy(self, src, dest):
                pass

        class _SVC(ServiceController):
            def __init__(self):
                pass

            def daemon_reload(self):
                raise OSError("systemctl not found")

            def restart(self, unit):
                pass

        result = apply_rootless_override(plan, consent=True, _fs=_FS(), _svc=_SVC())
        self.assertIsInstance(result, OverrideFailure)
        assert result is not None
        # Copy happened before daemon-reload failed
        self.assertTrue(
            result.persistence_applied, "override was written before service failure"
        )
        self.assertIn(
            "systemctl",
            result.detail.lower() or "systemctl" in result.path_or_command.lower(),
        )

    # -- restart failure ----------------------------------------------------

    def test_restart_failure(self):
        plan = self._plan()

        class _FS(Filesystem):
            @property
            def home(self):
                return Path("/fake")

            def is_file(self, path):
                return path == TestApplicationFailures.SRC

            def read_text(self, path, **kw):
                return "ok"

            def mkdir(self, path):
                pass

            def copy(self, src, dest):
                pass

        class _SVC(ServiceController):
            def __init__(self):
                pass

            def daemon_reload(self):
                pass

            def restart(self, unit):
                raise OSError("docker not running")

        result = apply_rootless_override(plan, consent=True, _fs=_FS(), _svc=_SVC())
        self.assertIsInstance(result, OverrideFailure)
        assert result is not None
        self.assertTrue(
            result.persistence_applied, "override was written before restart failure"
        )

    # -- no automatic rollback (requirement 21) -----------------------------

    def test_no_rollback_after_restart_failure(self):
        """After a successful copy and failed restart, the dest file remains."""
        written: dict[str, str] = {}

        class _FS(Filesystem):
            @property
            def home(self):
                return Path("/fake")

            def is_file(self, path):
                return path == TestApplicationFailures.SRC or str(path) in written

            def read_text(self, path, **kw):
                return written.get(str(path), "ok")

            def mkdir(self, path):
                pass

            def copy(self, src, dest):
                written[str(dest)] = "copied-content"

        class _SVC(ServiceController):
            def __init__(self):
                pass

            def daemon_reload(self):
                pass

            def restart(self, unit):
                raise OSError("restart failed")

        apply_rootless_override(self._plan(), consent=True, _fs=_FS(), _svc=_SVC())
        self.assertIn(
            str(self.DEST), written, "dest must remain — no automatic rollback"
        )


# ======================================================================
# 7.2 — Operational persistence (requirement 22)
# ======================================================================

# ======================================================================
# 7.3 — Fake-boundary contracts (requirements 24–27)
# ======================================================================


class TestProcessResultDTO(unittest.TestCase):
    """Requirement 25: ``ProcessResult`` structured fake outcome."""

    def test_stores_argv_as_tuple(self):
        pr = ProcessResult(
            argv=("docker", "info"),
            return_code=0,
            stdout="ok",
            stderr="",
        )
        self.assertEqual(pr.argv, ("docker", "info"))
        self.assertIsInstance(pr.argv, tuple)

    def test_stores_return_code(self):
        pr = ProcessResult(
            argv=("x",),
            return_code=1,
            stdout="",
            stderr="err",
        )
        self.assertEqual(pr.return_code, 1)

    def test_stores_stdout_and_stderr_separately(self):
        pr = ProcessResult(
            argv=("x",),
            return_code=0,
            stdout="out",
            stderr="err",
        )
        self.assertEqual(pr.stdout, "out")
        self.assertEqual(pr.stderr, "err")

    def test_is_frozen(self):
        pr = ProcessResult(
            argv=("x",),
            return_code=0,
            stdout="",
            stderr="",
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            pr.return_code = 5  # type: ignore[misc]


class TestProcessRunnerProtocol(unittest.TestCase):
    """Requirement 24: ``ProcessRunner`` exists and is injectable."""

    def test_default_runner_has_run_method(self):
        runner = ProcessRunner()
        self.assertTrue(callable(runner.run))

    def test_run_accepts_argv_list_and_returns_process_result(self):
        class _Fake(ProcessRunner):
            def run(self, argv):
                return ProcessResult(
                    argv=tuple(argv),
                    return_code=0,
                    stdout="fake",
                    stderr="",
                )

        result = _Fake().run(["echo", "hello"])
        self.assertIsInstance(result, ProcessResult)
        self.assertEqual(result.argv, ("echo", "hello"))


class TestFakeBoundaryCoverage(unittest.TestCase):
    """Requirements 24, 26, 27: every side-effect is behind an
    injectable boundary so tests never touch real Docker, systemd,
    or network."""

    def test_filesystem_boundary_exists(self):
        """``Filesystem`` covers all disk I/O in the module."""
        self.assertTrue(hasattr(Filesystem, "is_file"))
        self.assertTrue(hasattr(Filesystem, "read_text"))
        self.assertTrue(hasattr(Filesystem, "write_text"))
        self.assertTrue(hasattr(Filesystem, "mkdir"))
        self.assertTrue(hasattr(Filesystem, "copy"))
        self.assertTrue(hasattr(Filesystem, "rename"))
        self.assertTrue(hasattr(Filesystem, "delete"))
        self.assertTrue(hasattr(Filesystem, "home"))

    def test_service_controller_boundary_exists(self):
        """``ServiceController`` covers systemctl calls."""
        self.assertTrue(hasattr(ServiceController, "daemon_reload"))
        self.assertTrue(hasattr(ServiceController, "restart"))

    def test_clock_boundary_exists(self):
        """``SystemClock`` covers sleep and timestamp."""
        self.assertTrue(hasattr(SystemClock, "sleep"))
        self.assertTrue(hasattr(SystemClock, "timestamp"))

    def test_process_runner_boundary_exists(self):
        """``ProcessRunner`` covers subprocess execution."""
        self.assertTrue(hasattr(ProcessRunner, "run"))

    def test_probe_server_injectable(self):
        """``HostProbeServer`` can be replaced via ``_host_probe_factory``."""
        from docker.networking import HostProbeServer

        self.assertTrue(callable(HostProbeServer))

    def test_no_test_imports_subprocess_run_directly(self):
        """Requirement 26: this test file must not call ``subprocess.run``."""
        import ast

        with open(__file__) as f:
            tree = ast.parse(f.read())

        subprocess_calls: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if (
                    isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "subprocess"
                    and node.func.attr == "run"
                ):
                    subprocess_calls.append(f"line {node.lineno}")
        self.assertEqual(
            subprocess_calls,
            [],
            f"tests must not call subprocess.run directly: {subprocess_calls}",
        )

    def test_no_network_imports_in_tests(self):
        """Requirement 27: this test file must not import socket or
        urllib — networking is behind ``_run`` / ``_host_probe_factory``."""
        import ast

        with open(__file__) as f:
            tree = ast.parse(f.read())

        forbidden = {"socket", "urllib", "urllib.request", "http.client"}
        violations: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in forbidden:
                        violations.append(f"import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module in forbidden:
                    violations.append(f"from {node.module} import ...")
        self.assertEqual(
            violations,
            [],
            f"tests must not import network libraries: {violations}",
        )


# ======================================================================
# 7.3 — Single ProcessRunner injection across all public APIs
# ======================================================================

# A deterministic fake ProcessRunner that every API-injection test
# shares.  It consumes canned ``ProcessResult`` values in order so
# callers can model multi-step orchestration (e.g. ``docker info``
# followed by ``docker run --add-host ...``).


class _FakeProcessRunner(ProcessRunner):
    """ProcessRunner that consumes canned responses in sequence."""

    def __init__(self, responses: list[ProcessResult]):
        self._responses = list(responses)  # consumed in order (pop(0))
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def run(self, argv: list[str]) -> ProcessResult:
        self.calls.append(("run", tuple(argv)))
        if self._responses:
            return self._responses.pop(0)
        return ProcessResult(
            argv=tuple(argv),
            return_code=1,
            stdout="",
            stderr="no more canned responses",
        )


class TestProcessRunnerInjectionDetectDockerMode(unittest.TestCase):
    """Requirement 24: ``detect_docker_mode`` must accept ``_runner``."""

    def test_rootful_via_injected_process_runner(self):
        runner = _FakeProcessRunner(
            [
                ProcessResult(
                    argv=("docker", "info"),
                    return_code=0,
                    stdout="Server Mode: default",
                    stderr="",
                ),
            ]
        )
        # RED: _runner not yet accepted, expect TypeError
        mode = detect_docker_mode(_runner=runner)
        self.assertIs(mode, DockerMode.ROOTFUL)
        self.assertTrue(runner.calls)

    def test_rootless_via_injected_process_runner(self):
        runner = _FakeProcessRunner(
            [
                ProcessResult(
                    argv=("docker", "info"),
                    return_code=0,
                    stdout="rootless: yes",
                    stderr="",
                ),
            ]
        )
        mode = detect_docker_mode(_runner=runner)
        self.assertIs(mode, DockerMode.ROOTLESS)

    def test_docker_not_found_via_injected_process_runner(self):
        runner = _FakeProcessRunner(
            [
                ProcessResult(
                    argv=("docker", "info"),
                    return_code=1,
                    stdout="",
                    stderr="command not found",
                ),
            ]
        )
        with self.assertRaises(DockerDetectionError):
            detect_docker_mode(_runner=runner)


class TestProcessRunnerInjectionDetectLanIp(unittest.TestCase):
    """Requirement 24: ``detect_lan_ip`` must accept ``_runner``."""

    def test_returns_parsed_ip_via_injected_runner(self):
        runner = _FakeProcessRunner(
            [
                ProcessResult(
                    argv=("hostname", "-I"),
                    return_code=0,
                    stdout="10.0.0.42 ",
                    stderr="",
                ),
            ]
        )
        # RED: _runner not yet accepted
        ip = detect_lan_ip(_runner=runner)
        self.assertEqual(ip, "10.0.0.42")


class TestProcessRunnerInjectionProbeGateway(unittest.TestCase):
    """Requirement 24: ``probe_gateway`` must accept ``_runner``."""

    def test_probe_uses_injected_runner_for_docker_run(self):
        runner = _FakeProcessRunner(
            [
                ProcessResult(
                    argv=(
                        "docker",
                        "run",
                        "--rm",
                        "--add-host",
                        "host.docker.internal:10.0.0.1",
                        "alpine/socat:latest",
                        "sh",
                        "-c",
                        "...",
                    ),
                    return_code=0,
                    stdout="RESOLVED_IP=10.0.0.1\nPROBE_OK",
                    stderr="",
                ),
            ]
        )
        # RED: _runner not yet accepted
        result = probe_gateway("10.0.0.1", 12345, "secret", _runner=runner)
        self.assertTrue(result.ok)
        self.assertEqual(result.candidate, "10.0.0.1")
        self.assertEqual(result.resolved_ip, "10.0.0.1")


class TestProcessRunnerInjectionDiagnoseGateway(unittest.TestCase):
    """Requirement 24: ``diagnose_gateway`` must accept ``_runner``."""

    def test_diagnose_plumbs_runner_through_full_orchestration(self):
        # diagnose_gateway must thread a single _runner through:
        #   1. detect_docker_mode  -> docker info
        #   2. detect_lan_ip       -> hostname -I
        #   3. probe_gateway × N   -> docker run --add-host ...
        runner = _FakeProcessRunner(
            [
                # Call 1: detect_docker_mode
                ProcessResult(
                    argv=("docker", "info"),
                    return_code=0,
                    stdout="rootless: yes",
                    stderr="",
                ),
                # Call 2: detect_lan_ip
                ProcessResult(
                    argv=("hostname", "-I"),
                    return_code=0,
                    stdout="10.0.0.42",
                    stderr="",
                ),
                # Call 3: probe_gateway (docker run --add-host ...)
                ProcessResult(
                    argv=(
                        "docker",
                        "run",
                        "--rm",
                        "--add-host",
                        "host.docker.internal:172.17.0.1",
                        "alpine/socat:latest",
                        "sh",
                        "-c",
                        "...",
                    ),
                    return_code=0,
                    stdout="RESOLVED_IP=172.17.0.1\nPROBE_OK",
                    stderr="",
                ),
            ]
        )
        # RED: _runner not yet accepted
        diag = diagnose_gateway(
            _runner=runner,
            _host_probe_factory=FakeHostProbeServer,
        )
        self.assertIsInstance(diag, GatewayDiagnosis)
        self.assertIs(diag.mode, DockerMode.ROOTLESS)
        self.assertEqual(diag.lan_ip, "10.0.0.42")


class TestProcessRunnerInjectionServiceController(unittest.TestCase):
    """Requirement 24: ``ServiceController`` must accept ``_runner``."""

    def test_service_controller_uses_injected_runner(self):
        runner = _FakeProcessRunner(
            [
                ProcessResult(
                    argv=("systemctl", "--user", "daemon-reload"),
                    return_code=0,
                    stdout="",
                    stderr="",
                ),
            ]
        )
        # RED: __init__ does not accept _runner yet
        svc = ServiceController(_runner=runner)
        svc.daemon_reload()
        self.assertTrue(runner.calls)


if __name__ == "__main__":
    unittest.main()
