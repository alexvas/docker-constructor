"""Skip review is keyed by test identity, reason and applicability, not count."""

from __future__ import annotations

import importlib

import pytest

SNAPSHOT = (
    "tests/test_constructor_build_snapshot.py::TestArtifactSnapshot::"
    "test_foreign_owner_hard_link_is_rejected_without_adoption"
)
REGISTRY = (
    "tests/test_bound_pi_assembly_execution_phase6_registry.py::"
    "TestDockerBackedRegistryAcceptance::"
    "test_constructor_cold_and_warm_build_preserve_progress_and_final_image"
)


def policy():
    return importlib.import_module("tests.green_skip_policy")


def test_documented_environment_skip_allowed_locally_but_not_in_ci():
    reason = "an existing 'docker-dev' account is required"
    assert policy().approved_skip(SNAPSHOT, reason, environ={})
    assert not policy().approved_skip(SNAPSHOT, reason, environ={"CI": "true"})


def test_documented_nonapplicability_requires_optin_to_be_absent():
    reason = "run through validate-bound-pi-assembly-execution-phase6-registry"
    assert policy().approved_skip(REGISTRY, reason, environ={})
    assert not policy().approved_skip(
        REGISTRY, reason, environ={"BOUND_PI_PHASE6_HERMETIC": "1"}
    )


@pytest.mark.parametrize(
    ("nodeid", "reason"),
    [
        (SNAPSHOT, "temporarily broken"),
        (
            "tests/new_test.py::test_regression",
            "an existing 'docker-dev' account is required",
        ),
        (REGISTRY, "network failed"),
        (SNAPSHOT, ""),
    ],
)
def test_unknown_identity_or_reason_is_not_approved(nodeid, reason):
    assert not policy().approved_skip(nodeid, reason, environ={})


def test_all_documented_optin_tests_have_individual_dispositions():
    rules = policy().OPT_IN_SKIPS
    assert len(rules) == len(set(rules))
    for nodeid, (reason, variable) in rules.items():
        assert policy().approved_skip(nodeid, reason, environ={})
        assert not policy().approved_skip(nodeid, reason, environ={variable: "1"})


def test_actual_plugin_is_loaded_for_mandated_pytest(pytestconfig):
    assert pytestconfig.pluginmanager.hasplugin("tests.green_skip_policy")


def test_unknown_skip_and_xfail_force_failed_session():
    module = policy()
    from types import SimpleNamespace

    config = SimpleNamespace(_green_skip_findings=[], _green_skip_seen=[])
    plugin = module.SkipReview(config, environ={})
    plugin.pytest_runtest_logreport(
        SimpleNamespace(
            nodeid="tests/new.py::test_unknown",
            skipped=True,
            longrepr=("tests/new.py", 1, "Skipped: broken"),
            when="setup",
        )
    )
    plugin.pytest_runtest_logreport(
        SimpleNamespace(
            nodeid="tests/new.py::test_xfail",
            skipped=True,
            wasxfail="broken",
            longrepr="broken",
            when="call",
        )
    )
    session = SimpleNamespace(exitstatus=0)
    plugin.pytest_sessionfinish(session, 0)
    assert session.exitstatus == 1
    assert len(config._green_skip_findings) == 2


@pytest.mark.parametrize(
    "body", ["pytest.skip('unknown reason')", "pytest.xfail('unknown reason')"]
)
def test_real_pytest_unknown_skip_or_xfail_is_failed(tmp_path, body):
    from tests.enforcement.python_check_fixtures import (
        REPO_ROOT,
        load_check_module,
        write_source,
    )

    path = write_source(
        tmp_path,
        "test_isolated_policy.py",
        "import pytest\ndef test_case():\n    " + body + "\n",
    )
    result = load_check_module().run_captured(
        [
            str(REPO_ROOT / ".venv/bin/python"),
            "-m",
            "pytest",
            "-p",
            "tests.green_skip_policy",
            str(path),
            "-q",
        ],
        root=REPO_ROOT,
    )
    assert result.clean
    assert result.returncode == 1, result.stdout + result.stderr
    assert b"BLOCKED: unreviewed skips" in result.stdout


def test_review_never_rewrites_an_existing_failure_to_success():
    from types import SimpleNamespace

    module = policy()
    config = SimpleNamespace(_green_skip_findings=[], _green_skip_seen=[])
    session = SimpleNamespace(exitstatus=2)
    module.SkipReview(config, environ={}).pytest_sessionfinish(session, 2)
    assert session.exitstatus == 2
