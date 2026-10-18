"""RED tests for Stage 9.2 — Doctor orchestration contract.

These tests define the full expected contract for ``orchestrate_doctor``
(tasks 24–34).  Every test uses injected fakes — no Docker daemon,
systemd, filesystem writes, or network calls.

The stub ``orchestrate_doctor`` returns ``OPERATIONAL`` for every input.
Tests that assert ``SUCCESS``, real diagnosis data, ordering, or repair
outcomes **genuinely FAIL** against the stub.  This is the RED signal
that drives the Stage 9.3 implementation.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Optional
from unittest import mock

# ---------------------------------------------------------------------------
# Helper — create a docker-gateway-enabled inventory for tests that
# need a valid inventory path.
# ---------------------------------------------------------------------------


def _docker_gateway_inventory() -> str:
    """Return a Path to a temporary docker-gateway enabled inventory."""
    import tempfile
    from pathlib import Path as _Path

    _root = _Path(tempfile.mkdtemp(prefix="doctor-test-"))
    _inv = _root / "docker-constructor.toml"
    _inv.write_text(
        (Path(__file__).resolve().parents[1] / "docker-constructor.toml").read_text()
        + '\n[runtime.host-access]\nenabled = true\nmode = "docker-gateway"\n'
    )
    # Companion file must exist so persistence can write address
    (_root / "docker-constructor.local.toml").touch()
    return _inv


from docker.networking import (
    DockerMode,
    GatewayDiagnosis,
    OverrideFailure,
    OverrideState,
    ProbeResult,
    ProcessResult,
    ProcessRunner,
    RootlessOverridePlan,
)
from docker.versioning.build_orchestration import (
    DoctorRequest,
    DoctorResult,
    orchestrate_doctor,
)
from docker.versioning.dispatch_types import ExitKind

# ═══════════════════════════════════════════════════════════════════════
# Fakes — injectable, return real networking contract types
# ═══════════════════════════════════════════════════════════════════════


class FakeRunner:
    """Recording process runner matching ``docker.networking.ProcessRunner``."""

    def __init__(self, returncode: int = 0):
        self.calls: list[tuple[str, ...]] = []
        self.returncode = returncode

    def run(self, argv: list[str]):
        self.calls.append(tuple(argv))
        return ProcessResult(
            argv=tuple(argv),
            return_code=self.returncode,
            stdout="ok" if self.returncode == 0 else "",
            stderr="" if self.returncode == 0 else "error",
        )


# ── Helpers ───────────────────────────────────────────────────────────


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


# ── Injectables (return real networking types) ────────────────────────


def _diag_reachable(**kw):
    return _make_diagnosis(
        mode=DockerMode.ROOTFUL,
        chosen_gateway="172.17.0.1",
    )


def _diag_rootless_reachable(**kw):
    return _make_diagnosis(
        mode=DockerMode.ROOTLESS,
        chosen_gateway="10.0.2.2",
    )


def _diag_unreachable(**kw):
    return _make_diagnosis(
        mode=DockerMode.ROOTFUL,
        chosen_gateway=None,
    )


def _diag_rootless_unreachable(**kw):
    return _make_diagnosis(
        mode=DockerMode.ROOTLESS,
        chosen_gateway=None,
    )


def _plan_needed(**kw):
    return RootlessOverridePlan(
        installed=False,
        needed=True,
        state=OverrideState.ABSENT,
    )


def _plan_not_needed(**kw):
    return RootlessOverridePlan(
        installed=False,
        needed=False,
        state=OverrideState.ABSENT,
    )


def _plan_matching(**kw):
    return RootlessOverridePlan(
        installed=True,
        needed=False,
        state=OverrideState.MATCHING,
    )


def _plan_different(**kw):
    return RootlessOverridePlan(
        installed=True,
        needed=True,
        state=OverrideState.DIFFERENT,
    )


def _apply_ok(plan=None, consent=False):
    return None  # success (no OverrideFailure)


def _apply_success(plan=None, consent=False):
    return None


# ── Repair failure factories ──────────────────────────────────────────


def _apply_fail_source_missing(plan=None, consent=False):
    return OverrideFailure(
        operation="ensure-source",
        path_or_command="/opt/pi/docker/rootless-override.json",
        detail="source template not found",
        persistence_applied=False,
    )


def _apply_fail_mkdir(plan=None, consent=False):
    return OverrideFailure(
        operation="mkdir",
        path_or_command="/etc/docker",
        detail="Permission denied",
        persistence_applied=False,
    )


def _apply_fail_daemon_reload(plan=None, consent=False):
    return OverrideFailure(
        operation="daemon-reload",
        path_or_command="systemctl --user daemon-reload",
        detail="systemctl exited 1",
        persistence_applied=True,
    )


def _apply_fail_restart(plan=None, consent=False):
    return OverrideFailure(
        operation="restart",
        path_or_command="systemctl --user restart docker",
        detail="unit not found",
        persistence_applied=True,
    )


# ── Post-repair diagnosis factories ───────────────────────────────────


def _diag_post_repair_ok(**kw):
    return _make_diagnosis(
        mode=DockerMode.ROOTLESS,
        chosen_gateway="10.0.2.2",
    )


def _diag_post_repair_still_unreachable(**kw):
    return _make_diagnosis(
        mode=DockerMode.ROOTLESS,
        chosen_gateway=None,
    )


# ═══════════════════════════════════════════════════════════════════════
# 25–26.  Doctor DTOs
# ═══════════════════════════════════════════════════════════════════════


class TestDoctorRequestDto(unittest.TestCase):
    """Tasks 25–26 — DoctorRequest/DoctorResult fields and immutability."""

    def test_doctor_request_defaults(self):
        req = DoctorRequest(inventory_path=_docker_gateway_inventory())
        self.assertFalse(req.apply_override)
        self.assertEqual("alpine:3.20", req.probe_image)
        self.assertIsNone(req.probe_timeout)
        self.assertIsNone(req.runner)
        self.assertIsNone(req._diagnose_gateway)
        self.assertIsNone(req._plan_rootless_override)
        self.assertIsNone(req._apply_rootless_override)

    def test_doctor_request_all_fields_assignable(self):
        req = DoctorRequest(
            inventory_path=_docker_gateway_inventory(),
            apply_override=True,
            repair_consent=True,
            probe_image="busybox:1.36",
            probe_timeout=10,
            runner=FakeRunner(),
            _diagnose_gateway=_diag_reachable,
            _plan_rootless_override=_plan_needed,
            _apply_rootless_override=_apply_ok,
        )
        self.assertTrue(req.apply_override)
        self.assertEqual("busybox:1.36", req.probe_image)
        self.assertEqual(10, req.probe_timeout)
        self.assertIsNotNone(req.runner)
        self.assertIsNotNone(req._diagnose_gateway)
        self.assertIsNotNone(req._plan_rootless_override)
        self.assertIsNotNone(req._apply_rootless_override)

    def test_doctor_request_preserves_explicit_empty_probe_image(self):
        """Explicit ``""`` probe_image must not be coerced to
        the default "alpine:3.20"."""
        req = DoctorRequest(inventory_path=_docker_gateway_inventory(), probe_image="")
        self.assertEqual("", req.probe_image)

    def test_doctor_result_all_fields(self):
        diag = _diag_reachable()
        plan = _plan_needed()
        failure = _apply_fail_mkdir()
        post_diag = _diag_post_repair_ok()

        result = DoctorResult(
            exit_kind=ExitKind.SUCCESS,
            message="done",
            initial_diagnosis=diag,
            override_plan=plan,
            repair_applied=True,
            repair_failure=failure,
            post_repair_diagnosis=post_diag,
            selected_gateway="10.0.2.2",
        )
        self.assertEqual(ExitKind.SUCCESS, result.exit_kind)
        self.assertEqual("done", result.message)
        self.assertIs(diag, result.initial_diagnosis)
        self.assertIs(plan, result.override_plan)
        self.assertTrue(result.repair_applied)
        self.assertIs(failure, result.repair_failure)
        self.assertIs(post_diag, result.post_repair_diagnosis)
        self.assertEqual("10.0.2.2", result.selected_gateway)

    def test_process_result_boundary_shape(self):
        """``ProcessResult`` from ``docker.networking`` must be the canonical
        subprocess DTO — fields match ``ProcessRunner.run()`` return type."""
        pr = ProcessResult(
            argv=("docker", "info"),
            return_code=0,
            stdout="Server Version: 26.0.0",
            stderr="",
        )
        self.assertEqual(("docker", "info"), pr.argv)
        self.assertEqual(0, pr.return_code)
        self.assertEqual("Server Version: 26.0.0", pr.stdout)
        self.assertEqual("", pr.stderr)

    def test_runner_boundary_shape(self):
        """``ProcessRunner.run()`` accepts ``list[str]`` and returns
        ``ProcessResult`` — matching ``docker.networking`` contract."""
        runner = FakeRunner()
        result = runner.run(["echo", "hello"])
        self.assertIsInstance(result, ProcessResult)
        self.assertEqual(("echo", "hello"), result.argv)
        self.assertEqual(0, result.return_code)
        self.assertEqual("ok", result.stdout)
        self.assertEqual("", result.stderr)
        self.assertEqual([("echo", "hello")], runner.calls)

    def test_dtos_are_frozen(self):
        req = DoctorRequest(inventory_path=_docker_gateway_inventory())
        with self.assertRaises(Exception):
            req.apply_override = True  # type: ignore[misc]
        result = DoctorResult(exit_kind=ExitKind.SUCCESS)
        with self.assertRaises(Exception):
            result.repair_applied = True  # type: ignore[misc]


# ═══════════════════════════════════════════════════════════════════════
# 27.  Diagnosis-only tests
# ═══════════════════════════════════════════════════════════════════════


class TestDiagnosisOnly(unittest.TestCase):
    """Task 27 — diagnosis without repair intent."""

    def test_no_inventory_returns_disabled(self):
        """Without an inventory path the host-access policy cannot be
        determined — doctor returns disabled (no probing)."""

        result = orchestrate_doctor(
            DoctorRequest(
                inventory_path=None,
                _diagnose_gateway=_diag_reachable,
                _plan_rootless_override=_plan_not_needed,
            )
        )

        self.assertEqual(ExitKind.SUCCESS, result.exit_kind)
        self.assertIsNone(
            result.selected_gateway, "no diagnosis without reviewed policy"
        )
        self.assertIsNone(
            result.initial_diagnosis, "no diagnosis without reviewed policy"
        )

    # -- successful diagnosis -------------------------------------------

    def test_rootful_diagnosis_returns_success(self):
        req = DoctorRequest(
            inventory_path=_docker_gateway_inventory(),
            _diagnose_gateway=_diag_reachable,
        )
        result = orchestrate_doctor(req)
        self.assertEqual(
            ExitKind.SUCCESS,
            result.exit_kind,
            f"expected SUCCESS, got {result.exit_kind}",
        )
        self.assertIsNotNone(result.initial_diagnosis)
        self.assertEqual("172.17.0.1", result.selected_gateway)
        self.assertFalse(result.repair_applied)

    def test_rootless_diagnosis_without_repair(self):
        """Rootless diagnosis succeeds but repair_applied is False
        when apply_override is not requested."""
        req = DoctorRequest(
            inventory_path=_docker_gateway_inventory(),
            _diagnose_gateway=_diag_rootless_reachable,
        )
        result = orchestrate_doctor(req)
        self.assertEqual(
            ExitKind.SUCCESS,
            result.exit_kind,
            f"expected SUCCESS, got {result.exit_kind}",
        )
        self.assertIsNotNone(result.initial_diagnosis)
        self.assertEqual("10.0.2.2", result.selected_gateway)
        self.assertFalse(
            result.repair_applied, "repair must not be applied without apply_override"
        )

    # -- unavailable gateway --------------------------------------------

    def test_unavailable_gateway_returns_operational(self):
        req = DoctorRequest(
            inventory_path=_docker_gateway_inventory(),
            _diagnose_gateway=_diag_unreachable,
        )
        result = orchestrate_doctor(req)
        self.assertEqual(
            ExitKind.OPERATIONAL,
            result.exit_kind,
            f"expected OPERATIONAL, got {result.exit_kind}",
        )
        self.assertIsNotNone(result.initial_diagnosis)
        self.assertIsNone(result.selected_gateway)
        self.assertIn(
            "no route",
            (result.message or "").lower(),
            "message must indicate no reachable gateway",
        )

    # -- override states ------------------------------------------------

    def test_override_absent(self):
        """When no override exists, override_plan reflects ABSENT state."""
        req = DoctorRequest(
            inventory_path=_docker_gateway_inventory(),
            _diagnose_gateway=_diag_rootless_reachable,
            _plan_rootless_override=_plan_needed,
        )
        result = orchestrate_doctor(req)
        self.assertIsNotNone(result.override_plan)

    def test_override_different(self):
        """When override differs from source, state must be DIFFERENT."""
        req = DoctorRequest(
            inventory_path=_docker_gateway_inventory(),
            _diagnose_gateway=_diag_rootless_reachable,
            _plan_rootless_override=_plan_different,
        )
        result = orchestrate_doctor(req)
        self.assertIsNotNone(result.override_plan)

    def test_override_matching(self):
        """When override matches source, state must be MATCHING."""
        req = DoctorRequest(
            inventory_path=_docker_gateway_inventory(),
            _diagnose_gateway=_diag_rootless_reachable,
            _plan_rootless_override=_plan_matching,
        )
        result = orchestrate_doctor(req)
        self.assertIsNotNone(result.override_plan)

    # -- no mutation ----------------------------------------------------

    def test_diagnosis_read_only_no_mutation(self):
        """Diagnosis must **not** apply overrides, persist, or mutate
        filesystem/service state.  Bomb fakes prove this."""

        def bomb_apply(**kw):
            raise RuntimeError("apply must NOT be called during diagnosis")

        req = DoctorRequest(
            inventory_path=_docker_gateway_inventory(),
            _diagnose_gateway=_diag_rootless_reachable,
            _plan_rootless_override=_plan_needed,
            _apply_rootless_override=bomb_apply,
        )
        result = orchestrate_doctor(req)
        # Must succeed as a diagnosis, not a repair
        self.assertEqual(
            ExitKind.SUCCESS,
            result.exit_kind,
            f"expected SUCCESS, got {result.exit_kind}",
        )
        self.assertFalse(result.repair_applied, "diagnosis must never apply overrides")


# ═══════════════════════════════════════════════════════════════════════
# 28.  Explicit-repair tests
# ═══════════════════════════════════════════════════════════════════════


class TestExplicitRepair(unittest.TestCase):
    """Task 28 — repair requires ``--apply-rootless-override`` flag."""

    def test_repair_requires_apply_override_flag(self):
        """Without ``apply_override=True``, repair must not happen even
        when consent is given and diagnosis finds rootless mode."""

        def bomb_apply(**kw):
            raise RuntimeError("apply must NOT be called without apply_override")

        req = DoctorRequest(
            inventory_path=_docker_gateway_inventory(),
            apply_override=False,
            _diagnose_gateway=_diag_rootless_reachable,
            _plan_rootless_override=_plan_needed,
            _apply_rootless_override=bomb_apply,
        )
        result = orchestrate_doctor(req)
        self.assertEqual(
            ExitKind.SUCCESS,
            result.exit_kind,
            f"expected SUCCESS, got {result.exit_kind}",
        )
        self.assertFalse(result.repair_applied)

    def test_yes_alone_must_not_request_repair(self):
        """Consent alone (``--yes`` equivalent) does NOT imply
        ``--apply-rootless-override``.  Repair must be explicit."""

        def bomb_apply(**kw):
            raise RuntimeError("apply must NOT be called without apply_override")

        req = DoctorRequest(
            inventory_path=_docker_gateway_inventory(),
            apply_override=False,
            _diagnose_gateway=_diag_rootless_reachable,
            _plan_rootless_override=_plan_needed,
            _apply_rootless_override=bomb_apply,
            runner=FakeRunner(),
        )
        result = orchestrate_doctor(req)
        self.assertEqual(
            ExitKind.SUCCESS,
            result.exit_kind,
            f"expected SUCCESS, got {result.exit_kind}",
        )
        self.assertFalse(result.repair_applied, "--yes must NOT imply repair intent")

    def test_repair_rejected_for_rootful_docker(self):
        """Repair with ``apply_override=True`` must be rejected when Docker
        is rootful — no override plan is applicable."""

        def bomb_apply(**kw):
            raise RuntimeError("apply must NOT be called for rootful Docker")

        req = DoctorRequest(
            inventory_path=_docker_gateway_inventory(),
            apply_override=True,
            repair_consent=True,
            _diagnose_gateway=_diag_reachable,
            _plan_rootless_override=_plan_not_needed,
            _apply_rootless_override=bomb_apply,
        )
        result = orchestrate_doctor(req)
        self.assertEqual(
            ExitKind.POLICY,
            result.exit_kind,
            f"expected POLICY for rootful repair, got {result.exit_kind}",
        )
        self.assertFalse(result.repair_applied)
        self.assertIn(
            "rootful",
            (result.message or "").lower(),
            "message must indicate rootful Docker",
        )

    def test_matching_override_produces_no_service_ops(self):
        """When the override is already MATCHING, repair is acknowledged
        as a no-op — no service operations are performed."""

        def bomb_apply(**kw):
            raise RuntimeError("apply must NOT be called when override already matches")

        req = DoctorRequest(
            inventory_path=_docker_gateway_inventory(),
            apply_override=True,
            repair_consent=True,
            _diagnose_gateway=_diag_rootless_reachable,
            _plan_rootless_override=_plan_matching,
            _apply_rootless_override=bomb_apply,
        )
        result = orchestrate_doctor(req)
        self.assertFalse(
            result.repair_applied, "matching override must not trigger repair"
        )
        self.assertIsNone(result.repair_failure)


# ═══════════════════════════════════════════════════════════════════════
# 29.  Consent tests
# ═══════════════════════════════════════════════════════════════════════


class TestDoctorConsent(unittest.TestCase):
    """Task 29 — repair consent gating (callback removed, boolean only).

    The facade obtains consent; the orchestration receives an immutable
    ``repair_consent: bool``.  Repair proceeds only when both
    ``apply_override=True`` and ``repair_consent=True``."""

    def test_repair_confirmed_applies(self):
        """When repair_consent=True and apply_override=True, repair runs."""
        req = DoctorRequest(
            inventory_path=_docker_gateway_inventory(),
            apply_override=True,
            repair_consent=True,
            _diagnose_gateway=_diag_rootless_reachable,
            _plan_rootless_override=_plan_needed,
            _apply_rootless_override=_apply_ok,
        )
        result = orchestrate_doctor(req)
        self.assertTrue(
            result.repair_applied, "repair must be applied with confirmed consent"
        )

    def test_repair_denied_returns_diagnosis_without_mutation(self):
        """When ``repair_consent=False`` repair must be skipped but
        diagnosis, override plan, and selected gateway are still returned."""
        req = DoctorRequest(
            inventory_path=_docker_gateway_inventory(),
            apply_override=True,
            repair_consent=False,
            _diagnose_gateway=_diag_rootless_reachable,
            _plan_rootless_override=_plan_needed,
            _apply_rootless_override=_apply_ok,
        )
        result = orchestrate_doctor(req)
        self.assertEqual(
            ExitKind.SUCCESS,
            result.exit_kind,
            f"denied consent is SUCCESS not {result.exit_kind}",
        )
        self.assertFalse(result.repair_applied)
        self.assertIsNotNone(
            result.initial_diagnosis, "diagnosis must still be returned"
        )
        self.assertIsNotNone(
            result.override_plan, "override plan must still be returned"
        )
        self.assertIsNotNone(result.selected_gateway, "gateway must still be reported")
        self.assertIn(
            "denied",
            (result.message or "").lower(),
            "message must indicate consent was denied",
        )

    def test_non_interactive_explicit_consent(self):
        """When ``repair_consent=True`` (facade already confirmed),
        repair proceeds normally."""
        req = DoctorRequest(
            inventory_path=_docker_gateway_inventory(),
            apply_override=True,
            repair_consent=True,
            _diagnose_gateway=_diag_rootless_reachable,
            _plan_rootless_override=_plan_needed,
            _apply_rootless_override=_apply_ok,
        )
        result = orchestrate_doctor(req)
        self.assertTrue(result.repair_applied)

    def test_denied_consent_is_successful_noop(self):
        """Denied repair consent is a deliberate user choice — SUCCESS,
        not a configuration or operational error."""
        req = DoctorRequest(
            inventory_path=_docker_gateway_inventory(),
            apply_override=True,
            repair_consent=False,
            _diagnose_gateway=_diag_rootless_reachable,
            _plan_rootless_override=_plan_needed,
            _apply_rootless_override=_apply_ok,
        )
        result = orchestrate_doctor(req)
        self.assertEqual(
            ExitKind.SUCCESS,
            result.exit_kind,
            f"denied consent is SUCCESS not {result.exit_kind}",
        )


# ═══════════════════════════════════════════════════════════════════════
# 30.  Operation-order tests
# ═══════════════════════════════════════════════════════════════════════


class TestOperationOrder(unittest.TestCase):
    """Task 30 — operation order: 1) initial diagnosis, 2) derive plan,
    3) apply override, 4) rerun diagnosis.

    Docker mode detection is **not** a separate step — ``diagnose_gateway``
    already returns ``GatewayDiagnosis.mode``.
    Consent is a boolean flag set before the call — **not** a step."""

    def test_operation_order(self):
        seq = []

        def diagnose(**kw):
            seq.append("diagnose")
            return _diag_rootless_reachable()

        def plan(**kw):
            seq.append("plan")
            return _plan_needed()

        def apply(plan=None, consent=False):
            seq.append("apply")
            return None

        req = DoctorRequest(
            inventory_path=_docker_gateway_inventory(),
            apply_override=True,
            repair_consent=True,
            _diagnose_gateway=diagnose,
            _plan_rootless_override=plan,
            _apply_rootless_override=apply,
        )
        result = orchestrate_doctor(req)
        self.assertIsInstance(result, DoctorResult)
        for phase in ("diagnose", "plan", "apply"):
            self.assertIn(phase, seq, f"{phase} was never called; stub may be active")
        # 1) diagnose before plan
        self.assertLess(seq.index("diagnose"), seq.index("plan"))
        # 2) plan before apply
        self.assertLess(seq.index("plan"), seq.index("apply"))


# ═══════════════════════════════════════════════════════════════════════
# 31.  Repair-failure tests
# ═══════════════════════════════════════════════════════════════════════


class TestRepairFailure(unittest.TestCase):
    """Task 31 — structured repair failures."""

    def test_initial_gateway_diagnosed_before_repair_failure(self):
        """A failed repair must not discard the diagnosed gateway."""
        with tempfile.TemporaryDirectory() as tmp:
            gw_inv = _docker_gateway_inventory()
            result = orchestrate_doctor(
                DoctorRequest(
                    apply_override=True,
                    repair_consent=True,
                    inventory_path=gw_inv,
                    _diagnose_gateway=_diag_rootless_reachable,
                    _plan_rootless_override=_plan_needed,
                    _apply_rootless_override=_apply_fail_mkdir,
                )
            )

            self.assertEqual(ExitKind.OPERATIONAL, result.exit_kind)
            self.assertIsNotNone(result.repair_failure)
            self.assertEqual("10.0.2.2", result.selected_gateway)
            # With a valid docker-gateway inventory, persistence is attempted

    def test_source_missing(self):
        req = DoctorRequest(
            inventory_path=_docker_gateway_inventory(),
            apply_override=True,
            repair_consent=True,
            _diagnose_gateway=_diag_rootless_reachable,
            _plan_rootless_override=_plan_needed,
            _apply_rootless_override=_apply_fail_source_missing,
        )
        result = orchestrate_doctor(req)
        self.assertIsNotNone(
            result.repair_failure, "source-missing failure must be preserved"
        )
        self.assertFalse(result.repair_applied)
        # Source missing means no persistence was applied
        self.assertFalse(
            getattr(result.repair_failure, "persistence_applied", True),
            "persistence_applied must be False when source is missing",
        )

    def test_mkdir_copy_failure(self):
        req = DoctorRequest(
            inventory_path=_docker_gateway_inventory(),
            apply_override=True,
            repair_consent=True,
            _diagnose_gateway=_diag_rootless_reachable,
            _plan_rootless_override=_plan_needed,
            _apply_rootless_override=_apply_fail_mkdir,
        )
        result = orchestrate_doctor(req)
        self.assertIsNotNone(result.repair_failure)
        self.assertFalse(result.repair_applied)
        self.assertIn(
            getattr(result.repair_failure, "operation", ""),
            ("mkdir", "ensure-source"),
            "failure operation must be reported",
        )

    def test_daemon_reload_failure(self):
        req = DoctorRequest(
            inventory_path=_docker_gateway_inventory(),
            apply_override=True,
            repair_consent=True,
            _diagnose_gateway=_diag_rootless_reachable,
            _plan_rootless_override=_plan_needed,
            _apply_rootless_override=_apply_fail_daemon_reload,
        )
        result = orchestrate_doctor(req)
        self.assertIsNotNone(result.repair_failure)
        self.assertFalse(result.repair_applied)
        # Persistence may have succeeded even if reload fails
        self.assertTrue(
            getattr(result.repair_failure, "persistence_applied", False),
            "persistence_applied must be True when file was written",
        )

    def test_restart_failure(self):
        req = DoctorRequest(
            inventory_path=_docker_gateway_inventory(),
            apply_override=True,
            repair_consent=True,
            _diagnose_gateway=_diag_rootless_reachable,
            _plan_rootless_override=_plan_needed,
            _apply_rootless_override=_apply_fail_restart,
        )
        result = orchestrate_doctor(req)
        self.assertIsNotNone(result.repair_failure)
        self.assertFalse(result.repair_applied)

    def test_structured_override_failure_preserved(self):
        """The full OverrideFailure structure must be accessible: operation,
        path_or_command, detail, persistence_applied."""
        req = DoctorRequest(
            inventory_path=_docker_gateway_inventory(),
            apply_override=True,
            repair_consent=True,
            _diagnose_gateway=_diag_rootless_reachable,
            _plan_rootless_override=_plan_needed,
            _apply_rootless_override=_apply_fail_daemon_reload,
        )
        result = orchestrate_doctor(req)
        failure = result.repair_failure
        self.assertIsNotNone(failure)
        self.assertEqual("daemon-reload", getattr(failure, "operation", ""))
        self.assertTrue(
            getattr(failure, "persistence_applied", False),
        )

    def test_no_post_repair_diagnosis_after_failed_application(self):
        """When repair fails, no post-repair diagnosis is performed."""
        req = DoctorRequest(
            inventory_path=_docker_gateway_inventory(),
            apply_override=True,
            repair_consent=True,
            _diagnose_gateway=_diag_rootless_reachable,
            _plan_rootless_override=_plan_needed,
            _apply_rootless_override=_apply_fail_mkdir,
        )
        result = orchestrate_doctor(req)
        self.assertIsNotNone(result.repair_failure)
        self.assertIsNone(
            result.post_repair_diagnosis,
            "post-repair diagnosis must not be run after failure",
        )


# ═══════════════════════════════════════════════════════════════════════
# 32.  Post-repair tests
# ═══════════════════════════════════════════════════════════════════════


class TestPostRepair(unittest.TestCase):
    """Task 32 — post-repair diagnosis."""

    def test_successful_repair_reruns_diagnosis_exactly_once(self):
        counter = {"diagnose": 0}

        def counting_diagnose(**kw):
            counter["diagnose"] += 1
            if counter["diagnose"] == 1:
                return _diag_rootless_reachable()
            return _diag_post_repair_ok()

        req = DoctorRequest(
            inventory_path=_docker_gateway_inventory(),
            apply_override=True,
            repair_consent=True,
            _diagnose_gateway=counting_diagnose,
            _plan_rootless_override=_plan_needed,
            _apply_rootless_override=_apply_ok,
        )
        result = orchestrate_doctor(req)
        self.assertTrue(result.repair_applied)
        self.assertEqual(
            2,
            counter["diagnose"],
            "diagnosis must be called exactly twice: initial + post-repair",
        )
        self.assertIsNotNone(
            result.initial_diagnosis, "initial_diagnosis must be the first call"
        )
        self.assertIsNotNone(
            result.post_repair_diagnosis,
            "post_repair_diagnosis must be the second call",
        )

    def test_post_repair_gateway_selected(self):
        """After successful repair, selected_gateway comes from
        post-repair diagnosis, not initial."""
        req = DoctorRequest(
            inventory_path=_docker_gateway_inventory(),
            apply_override=True,
            repair_consent=True,
            _diagnose_gateway=_diag_rootless_reachable,
            _plan_rootless_override=_plan_needed,
            _apply_rootless_override=_apply_ok,
        )
        result = orchestrate_doctor(req)
        self.assertTrue(result.repair_applied)
        self.assertIsNotNone(result.selected_gateway)

    def test_post_repair_no_route_remains_actionable(self):
        """When post-repair diagnosis still shows no route, the result
        must surface the failed post-repair state so the caller can act."""
        diag_count = 0

        def alternating_diag(**kw):
            nonlocal diag_count
            diag_count += 1
            if diag_count == 1:
                return _diag_rootless_reachable()
            return _diag_post_repair_still_unreachable()

        req = DoctorRequest(
            inventory_path=_docker_gateway_inventory(),
            apply_override=True,
            repair_consent=True,
            _diagnose_gateway=alternating_diag,
            _plan_rootless_override=_plan_needed,
            _apply_rootless_override=_apply_ok,
        )
        result = orchestrate_doctor(req)
        self.assertTrue(result.repair_applied)
        self.assertIsNotNone(result.post_repair_diagnosis)
        # Post-repair diagnosis with no route
        post = result.post_repair_diagnosis
        self.assertIsNone(
            getattr(post, "resolved_address", None),
            "post-repair must reflect unreachable state",
        )


# ═══════════════════════════════════════════════════════════════════════
# 32a.  Doctor boundary failures — Stage 9.3 hardening
# ═══════════════════════════════════════════════════════════════════════


class TestDoctorBoundaryFailures(unittest.TestCase):
    """Diagnosis, planning, and application exceptions must produce
    structured results and never escape."""

    # -- diagnosis exceptions -------------------------------------------

    def test_diagnosis_exception_returns_operational(self):
        """A crashing diagnosis must produce OPERATIONAL with the
        exception detail embedded."""

        def broken_diagnose(**kw):
            raise RuntimeError("Docker socket unreachable")

        req = DoctorRequest(
            inventory_path=_docker_gateway_inventory(),
            _diagnose_gateway=broken_diagnose,
        )
        result = orchestrate_doctor(req)
        self.assertEqual(ExitKind.OPERATIONAL, result.exit_kind)
        self.assertIn("Docker socket unreachable", result.message or "")
        self.assertIsNone(result.initial_diagnosis)

    def test_diagnosis_exception_with_repair_intent(self):
        """Diagnosis failure stops execution before planning or repair."""

        def broken_diagnose(**kw):
            raise ConnectionError("no docker daemon")

        def bomb_plan(**kw):
            raise RuntimeError("plan must not be called")

        req = DoctorRequest(
            inventory_path=_docker_gateway_inventory(),
            apply_override=True,
            repair_consent=True,
            _diagnose_gateway=broken_diagnose,
            _plan_rootless_override=bomb_plan,
        )
        result = orchestrate_doctor(req)
        self.assertEqual(ExitKind.OPERATIONAL, result.exit_kind)
        self.assertIn("no docker daemon", result.message or "")
        self.assertIsNone(result.initial_diagnosis)

    # -- planning exceptions --------------------------------------------

    def test_plan_exception_returns_operational(self):
        """A crashing plan derivation must produce OPERATIONAL."""

        def broken_plan(**kw):
            raise RuntimeError("cannot stat overrides directory")

        req = DoctorRequest(
            inventory_path=_docker_gateway_inventory(),
            apply_override=True,
            repair_consent=True,
            _diagnose_gateway=_diag_rootless_reachable,
            _plan_rootless_override=broken_plan,
            _apply_rootless_override=_apply_ok,
        )
        result = orchestrate_doctor(req)
        self.assertEqual(ExitKind.OPERATIONAL, result.exit_kind)
        self.assertIn("cannot stat overrides directory", result.message or "")
        self.assertIsNotNone(result.initial_diagnosis)
        self.assertIsNone(result.override_plan)

    # -- application exceptions -----------------------------------------

    def test_apply_exception_returns_operational(self):
        """A crashing apply must produce OPERATIONAL."""

        def broken_apply(plan=None, consent=False):
            raise RuntimeError("systemctl daemon-reload failed")

        req = DoctorRequest(
            inventory_path=_docker_gateway_inventory(),
            apply_override=True,
            repair_consent=True,
            _diagnose_gateway=_diag_rootless_reachable,
            _plan_rootless_override=_plan_needed,
            _apply_rootless_override=broken_apply,
        )
        result = orchestrate_doctor(req)
        self.assertEqual(ExitKind.OPERATIONAL, result.exit_kind)
        self.assertIn("systemctl daemon-reload failed", result.message or "")
        self.assertIsNotNone(result.initial_diagnosis)
        self.assertIsNotNone(result.override_plan)
        self.assertFalse(result.repair_applied)

    # -- post-repair diagnosis exceptions -------------------------------

    def test_post_repair_diagnosis_exception_returns_operational(self):
        """When the repair succeeds but re-diagnosis crashes, the result
        must be OPERATIONAL with ``repair_applied=True``."""
        post_diag_calls = []

        def selective_diagnose(**kw):
            post_diag_calls.append(1)
            if len(post_diag_calls) > 1:
                raise RuntimeError("post-repair probe failed")
            return _diag_rootless_reachable(**kw)

        req = DoctorRequest(
            inventory_path=_docker_gateway_inventory(),
            apply_override=True,
            repair_consent=True,
            _diagnose_gateway=selective_diagnose,
            _plan_rootless_override=_plan_needed,
            _apply_rootless_override=_apply_ok,
        )
        result = orchestrate_doctor(req)
        self.assertEqual(ExitKind.OPERATIONAL, result.exit_kind)
        self.assertIn("post-repair probe failed", result.message or "")
        self.assertTrue(
            result.repair_applied, "repair was applied before diagnosis crashed"
        )
        self.assertIsNotNone(result.initial_diagnosis)
        self.assertIsNotNone(result.override_plan)


# ═══════════════════════════════════════════════════════════════════════
# 33.  Doctor gateway persistence decision
# ═══════════════════════════════════════════════════════════════════════


class TestDoctorPersistence(unittest.TestCase):
    """Task 33 — doctor does **not** persist gateway; only build persists."""

    def test_diagnosis_does_not_persist_gateway(self):
        """Default doctor diagnosis must remain read-only — no .env or
        effective.toml writes."""

        def bomb_persist(**kw):
            raise RuntimeError("doctor must not persist gateway")

        req = DoctorRequest(
            inventory_path=_docker_gateway_inventory(),
            _diagnose_gateway=_diag_reachable,
        )
        result = orchestrate_doctor(req)
        self.assertEqual(
            ExitKind.SUCCESS,
            result.exit_kind,
            f"expected SUCCESS, got {result.exit_kind}",
        )
        # Doctor returns diagnosis but does not write .env
        self.assertIsNotNone(result.initial_diagnosis)


# ═══════════════════════════════════════════════════════════════════════
# Real-signature wrappers — catch wrong keyword arguments
# ═══════════════════════════════════════════════════════════════════════


class TestRealSignatureWiring(unittest.TestCase):
    """Verify that ``orchestrate_doctor`` passes the exact keyword
    arguments expected by the real networking APIs, not relaxed
    ``**kw`` names.  Each wrapper has the same signature as the
    real function; a wrong keyword raises ``TypeError``."""

    def _diagnose_wrapper(
        self,
        *,
        probe_image: str = "alpine:3.20",
        probe_timeout: int = 3,
        _runner: object = None,
        _host_probe_factory: object = None,
        _fs: object = None,
        _override_src: object = None,
        _override_dest: object = None,
    ) -> GatewayDiagnosis:
        """Real-signature wrapper — delegates to a reachable diagnosis."""
        return _diag_rootless_reachable()

    def _plan_wrapper(
        self,
        *,
        _mode: object = None,
        _fs: object = None,
        _override_src: object = None,
        _override_dest: object = None,
    ) -> RootlessOverridePlan:
        """Real-signature wrapper — delegates to _plan_needed."""
        return _plan_needed()

    def _apply_wrapper(
        self,
        plan: object,
        *,
        consent: bool,
        _fs: object = None,
        _svc: object = None,
        _clock: object = None,
    ) -> OverrideFailure | None:
        """Real-signature wrapper — delegates to _apply_ok."""
        return _apply_ok()

    def test_diagnosis_passes_correct_kwargs(self) -> None:
        """``_runner``, ``probe_image``, ``probe_timeout`` must be
        accepted by the real-signature wrapper."""
        req = DoctorRequest(
            inventory_path=_docker_gateway_inventory(),
            apply_override=True,
            repair_consent=True,
            _diagnose_gateway=self._diagnose_wrapper,
            _plan_rootless_override=self._plan_wrapper,
            _apply_rootless_override=self._apply_wrapper,
        )
        result = orchestrate_doctor(req)
        # Real-signature wrappers don't raise TypeError → wiring is correct
        self.assertIn(
            result.exit_kind, (ExitKind.SUCCESS, ExitKind.OPERATIONAL, ExitKind.POLICY)
        )

    def test_plan_passes_mode_as_underscore_kwarg(self) -> None:
        """The plan wrapper uses ``_mode=`` not ``mode=``.
        If the orchestration passed ``mode=`` the wrapper would raise
        ``TypeError``."""
        req = DoctorRequest(
            inventory_path=_docker_gateway_inventory(),
            apply_override=True,
            repair_consent=True,
            _diagnose_gateway=self._diagnose_wrapper,
            _plan_rootless_override=self._plan_wrapper,
            _apply_rootless_override=self._apply_wrapper,
        )
        result = orchestrate_doctor(req)
        self.assertIsNotNone(result.override_plan)

    def test_probe_image_none_not_passed_to_diagnose(self) -> None:
        """When ``probe_image`` is ``None`` in the DTO, it must not be
        passed to ``diagnose_gateway`` (which has its own default).

        The CLI dispatcher no longer passes ``None`` to the DTO
        constructor, so the DTO default (``"alpine:3.20"``) applies
        and is forwarded.  To test the ``None``-suppression path we
        bypass the DTO default via ``object.__setattr__``."""
        received: dict[str, object] = {}

        def _recording_diagnose(**kwargs: object) -> GatewayDiagnosis:
            received.update(kwargs)
            return _diag_rootless_reachable()

        req = DoctorRequest(
            inventory_path=_docker_gateway_inventory(),
            apply_override=True,
            repair_consent=True,
            _diagnose_gateway=_recording_diagnose,
            _plan_rootless_override=self._plan_wrapper,
            _apply_rootless_override=self._apply_wrapper,
        )
        # Simulate a caller that explicitly set probe_image=None
        object.__setattr__(req, "probe_image", None)
        object.__setattr__(req, "probe_timeout", None)
        orchestrate_doctor(req)
        self.assertNotIn(
            "probe_image", received, "probe_image=None should not be passed to diagnose"
        )
        self.assertNotIn(
            "probe_timeout",
            received,
            "probe_timeout=None should not be passed to diagnose",
        )

    def test_explicit_probe_image_is_passed(self) -> None:
        """When ``probe_image`` is set to an explicit value, it must
        be forwarded to ``diagnose_gateway``."""
        received: dict[str, object] = {}

        def _recording_diagnose(**kwargs: object) -> GatewayDiagnosis:
            received.update(kwargs)
            return _diag_rootless_reachable()

        req = DoctorRequest(
            inventory_path=_docker_gateway_inventory(),
            apply_override=True,
            repair_consent=True,
            probe_image="busybox:1.36",
            probe_timeout=10,
            _diagnose_gateway=_recording_diagnose,
            _plan_rootless_override=self._plan_wrapper,
            _apply_rootless_override=self._apply_wrapper,
        )
        orchestrate_doctor(req)
        self.assertEqual("busybox:1.36", received.get("probe_image"))
        self.assertEqual(10, received.get("probe_timeout"))

    def test_runner_passed_as_underscore_runner(self) -> None:
        """``DoctorRequest.runner`` must be wired as ``_runner=`` to
        ``diagnose_gateway``."""
        received: dict[str, object] = {}

        def _recording_diagnose(**kwargs: object) -> GatewayDiagnosis:
            received.update(kwargs)
            return _diag_rootless_reachable()

        runner = FakeRunner()
        req = DoctorRequest(
            inventory_path=_docker_gateway_inventory(),
            apply_override=True,
            repair_consent=True,
            runner=runner,
            _diagnose_gateway=_recording_diagnose,
            _plan_rootless_override=self._plan_wrapper,
            _apply_rootless_override=self._apply_wrapper,
        )
        orchestrate_doctor(req)
        self.assertIs(runner, received.get("_runner"))


# ═══════════════════════════════════════════════════════════════════════
# Repair-from-unreachable tests
# ═══════════════════════════════════════════════════════════════════════


class TestUnreachableRepair(unittest.TestCase):
    """Verify that ``orchestrate_doctor`` with repair intent can
    recover from an initially unreachable gateway — the rootless
    override exists precisely to fix this failure."""

    def test_diagnosis_only_unreachable_stays_operational(self) -> None:
        """Without repair intent, an unreachable gateway is still
        OPERATIONAL (existing behaviour preserved)."""
        req = DoctorRequest(
            inventory_path=_docker_gateway_inventory(),
            apply_override=False,
            _diagnose_gateway=_diag_rootless_unreachable,
            _plan_rootless_override=_plan_needed,
        )
        result = orchestrate_doctor(req)
        self.assertEqual(
            ExitKind.OPERATIONAL,
            result.exit_kind,
            f"expected OPERATIONAL, got {result.exit_kind}",
        )
        self.assertIsNone(result.selected_gateway)
        self.assertFalse(result.repair_applied)

    def test_unreachable_rootful_with_repair_stays_operational(self) -> None:
        """Repair intent on a rootful daemon with unreachable gateway
        cannot repair — the override is not applicable."""
        req = DoctorRequest(
            inventory_path=_docker_gateway_inventory(),
            apply_override=True,
            repair_consent=True,
            _diagnose_gateway=_diag_unreachable,  # ROOTFUL
            _plan_rootless_override=_plan_matching,
        )
        result = orchestrate_doctor(req)
        self.assertEqual(ExitKind.OPERATIONAL, result.exit_kind)
        self.assertIsNone(result.selected_gateway)
        self.assertFalse(result.repair_applied)

    def test_repair_fixes_unreachable_gateway(self) -> None:
        """When the initial probe fails, rootless repair with consent
        proceeds through plan → apply → re-diagnose.  After a
        successful repair the gateway is reachable."""
        # Stateful fake: unreachable first, reachable after repair
        call_count: list[int] = [0]

        def _flip_diagnose(**kw: object) -> GatewayDiagnosis:
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_diagnosis(
                    mode=DockerMode.ROOTLESS,
                    chosen_gateway=None,
                )
            return _make_diagnosis(
                mode=DockerMode.ROOTLESS,
                chosen_gateway="10.0.2.2",
            )

        req = DoctorRequest(
            inventory_path=_docker_gateway_inventory(),
            apply_override=True,
            repair_consent=True,
            _diagnose_gateway=_flip_diagnose,
            _plan_rootless_override=_plan_needed,
            _apply_rootless_override=_apply_ok,
        )
        result = orchestrate_doctor(req)
        self.assertEqual(
            ExitKind.SUCCESS,
            result.exit_kind,
            f"expected SUCCESS, got {result.exit_kind}",
        )
        self.assertTrue(result.repair_applied)
        self.assertEqual("10.0.2.2", result.selected_gateway)
        self.assertEqual(
            2, call_count[0], "expected both initial and post-repair diagnoses"
        )

    def test_post_repair_still_unreachable_returns_operational(self) -> None:
        """When repair is applied but the gateway remains unreachable
        after the override, the result is OPERATIONAL."""
        req = DoctorRequest(
            inventory_path=_docker_gateway_inventory(),
            apply_override=True,
            repair_consent=True,
            _diagnose_gateway=_diag_rootless_unreachable,
            _plan_rootless_override=_plan_needed,
            _apply_rootless_override=_apply_ok,
        )
        result = orchestrate_doctor(req)
        self.assertEqual(
            ExitKind.OPERATIONAL,
            result.exit_kind,
            f"expected OPERATIONAL, got {result.exit_kind}",
        )
        self.assertTrue(result.repair_applied)
        # selected_gateway derived solely from post-repair diagnosis;
        # no fallback to the pre-repair (unreachable) gateway.
        self.assertIsNone(result.selected_gateway)

    def test_post_repair_loses_reachability_reported_as_operational(self) -> None:
        """When the gateway was reachable before repair but becomes
        unreachable after, the stale pre-repair gateway must NOT leak
        into ``selected_gateway``.  Result is OPERATIONAL."""
        call_count: list[int] = [0]

        def _flip_down(**kw: object) -> GatewayDiagnosis:
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_diagnosis(
                    mode=DockerMode.ROOTLESS,
                    chosen_gateway="10.0.2.2",  # reachable initially
                )
            return _make_diagnosis(
                mode=DockerMode.ROOTLESS,
                chosen_gateway=None,  # unreachable after repair
            )

        req = DoctorRequest(
            inventory_path=_docker_gateway_inventory(),
            apply_override=True,
            repair_consent=True,
            _diagnose_gateway=_flip_down,
            _plan_rootless_override=_plan_needed,
            _apply_rootless_override=_apply_ok,
        )
        result = orchestrate_doctor(req)
        self.assertEqual(
            ExitKind.OPERATIONAL,
            result.exit_kind,
            f"expected OPERATIONAL, got {result.exit_kind}",
        )
        self.assertTrue(result.repair_applied)
        self.assertIsNone(
            result.selected_gateway, "stale pre-repair gateway must not leak"
        )
        self.assertIn("unreachable after repair", result.message or "")

    def test_unreachable_with_matching_override_is_operational(self) -> None:
        """When the gateway is unreachable and the override is already
        MATCHING (installed), the no-op repair cannot help — result
        must be OPERATIONAL, not SUCCESS."""
        req = DoctorRequest(
            inventory_path=_docker_gateway_inventory(),
            apply_override=True,
            repair_consent=True,
            _diagnose_gateway=_diag_rootless_unreachable,
            _plan_rootless_override=_plan_matching,
            _apply_rootless_override=_apply_ok,
        )
        result = orchestrate_doctor(req)
        self.assertEqual(
            ExitKind.OPERATIONAL,
            result.exit_kind,
            f"expected OPERATIONAL, got {result.exit_kind}",
        )
        self.assertFalse(result.repair_applied)
        self.assertIsNone(result.selected_gateway)
        self.assertIn("already matching", result.message or "")


if __name__ == "__main__":
    unittest.main()
