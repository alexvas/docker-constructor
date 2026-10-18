"""Permanent pytest skip review; applicability rules are in docs/python-checks.md.

The controller observes all xdist reports. No file or process is opened here.
Unknown skips and any xfail/xpass block success without overriding prior failure.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

import pytest

_PRIVILEGE_REASON = "an existing 'docker-dev' account is required"
PRIVILEGE_SKIPS = {
    "tests/test_constructor_build_snapshot.py::TestArtifactSnapshot::"
    "test_foreign_owner_hard_link_is_rejected_without_adoption": _PRIVILEGE_REASON,
    "tests/test_constructor_build_snapshot.py::TwoBuildOwnershipMaintenanceRegression::"
    "test_two_builds_reuse_blobs_after_project_ownership_maintenance": _PRIVILEGE_REASON,
    "tests/test_constructor_build_cache_permissions.py::ConstructorProjectForeignOwnership::"
    "test_foreign_owned_constructor_project_keeps_external_state_private": _PRIVILEGE_REASON,
    "tests/test_constructor_phase4_named_context.py::TestPhase4Lifecycle::"
    "test_differing_uid_cannot_traverse_private_snapshot": "sudo runuser -u nobody unavailable: [Errno 2] No such file or directory: 'sudo'",
}
_REGISTRY = (
    "tests/test_bound_pi_assembly_execution_phase6_registry.py::"
    "TestDockerBackedRegistryAcceptance::"
)
_REGISTRY_TESTS = (
    "test_constructor_cold_and_warm_build_preserve_progress_and_final_image",
    "test_cold_then_network_blocked_warm_reuses_verified_publication",
    "test_stalled_request_fails_finitely_without_network_classification",
    "test_large_registry_failure_uses_bounded_redacted_production_diagnostics",
    "test_partial_download_preserves_opaque_cache_and_retry_succeeds",
    "test_constructor_build_interruption_cleans_up_and_retry_succeeds",
    "test_interruption_preserves_prior_output_and_cache_then_retry_succeeds",
)
OPT_IN_SKIPS = {
    _REGISTRY + name: (
        "run through validate-bound-pi-assembly-execution-phase6-registry",
        "BOUND_PI_PHASE6_HERMETIC",
    )
    for name in _REGISTRY_TESTS
}
OPT_IN_SKIPS.update(
    {
        "tests/test_bound_pi_assembly_execution_phase6_host.py::"
        "TestDockerBackedHostAcceptance::"
        "test_outer_deadline_reaps_container_and_removes_staging": (
            "run through validate-bound-pi-assembly-execution-phase6-host",
            "BOUND_PI_PHASE6_HERMETIC",
        ),
        "tests/test_npm_environment_phase3_validate.py::TestRealPinnedContainerSmoke::"
        "test_node_image_executes_assembler_script": (
            "set NPM_ENV_REAL_DOCKER=1 to run the real pinned-container smoke test",
            "NPM_ENV_REAL_DOCKER",
        ),
    }
)


def approved_skip(nodeid: str, reason: str, *, environ: Mapping[str, str]) -> bool:
    """Approve only a reviewed test/reason pair in its documented environment."""
    if reason.startswith("Skipped: "):
        reason = reason[len("Skipped: ") :]
    if nodeid in PRIVILEGE_SKIPS:
        return (
            reason == PRIVILEGE_SKIPS[nodeid]
            and environ.get("CI") != "true"
            and environ.get("PRIVILEGED_HELPERS") != "1"
        )
    rule = OPT_IN_SKIPS.get(nodeid)
    return bool(rule and reason == rule[0] and environ.get(rule[1]) != "1")


class SkipReview:
    """Collect reviewed skips and reject unknown skips or expected failures."""

    def __init__(self, config, *, environ=None):
        self.config = config
        self.environ = os.environ if environ is None else environ

    def _review(self, report):
        if hasattr(report, "wasxfail"):
            self.config._green_skip_findings.append(
                f"{report.nodeid}: unexpected xfail/xpass: {report.wasxfail}"
            )
        elif report.skipped:
            detail = report.longrepr
            reason = detail[2] if isinstance(detail, tuple) else str(detail)
            entry = f"{report.nodeid}: {reason}"
            self.config._green_skip_seen.append(entry)
            if not approved_skip(report.nodeid, reason, environ=self.environ):
                self.config._green_skip_findings.append(entry)

    def pytest_runtest_logreport(self, report):
        self._review(report)

    def pytest_collectreport(self, report):
        self._review(report)

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session, exitstatus):
        if self.config._green_skip_findings and not session.exitstatus:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED

    def pytest_terminal_summary(self, terminalreporter):
        if self.config._green_skip_seen:
            terminalreporter.section(
                "Reviewed skip reasons (test / applicability policy)"
            )
            for entry in sorted(set(self.config._green_skip_seen)):
                terminalreporter.write_line(entry)
        if self.config._green_skip_findings:
            terminalreporter.section(
                "BLOCKED: unreviewed skips or unexpected xfail/xpass"
            )
            for entry in sorted(set(self.config._green_skip_findings)):
                terminalreporter.write_line(entry)


def pytest_configure(config):
    config._green_skip_findings = []
    config._green_skip_seen = []
    config.pluginmanager.register(SkipReview(config), "green-skip-review")
