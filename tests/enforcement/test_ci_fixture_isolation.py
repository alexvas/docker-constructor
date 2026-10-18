"""Clean-checkout and independent-project CI regressions."""

import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from tests import test_constructor_build_materialization as materialization
from tests import test_constructor_phase4_named_context as named_context
from tests import test_process_strict_actions_and_reporting as action_reporting
from tests.enforcement.green_fixtures import read_text
from tests.enforcement.python_check_fixtures import REPO_ROOT
from tests.npm_http_logging_research import NODE_VERSION, NPM_VERSION


def test_ci_provisions_the_reviewed_node_and_npm():
    workflow = read_text(REPO_ROOT / ".github/workflows/validation.yml")
    assert f'node-version: "{NODE_VERSION}"' in workflow
    assert f"npm install --global npm@{NPM_VERSION}" in workflow


def test_ca_failure_fixture_does_not_read_a_developer_bundle():
    original = Path.read_bytes
    forbidden = materialization.ROOT / ".docker-local/corporate-ca-bundle.crt"

    def clean_checkout(path):
        if path == forbidden:
            raise FileNotFoundError("developer-only CA is absent in clean checkout")
        return original(path)

    case = materialization.TestMaterializationOrchestration(
        "test_disappearing_ca_is_redacted_and_prevents_all_execution_effects"
    )
    with patch.object(Path, "read_bytes", clean_checkout):
        result = case.run()
    assert not result.errors, result.errors
    assert not result.failures, result.failures


@pytest.mark.parametrize("delay", ["helper", "supervisor"])
def test_cumulative_failure_contract_allows_a_retained_cleanup_continuation(
    monkeypatch, delay
):
    original_action = action_reporting.Action
    original_owner = action_reporting.action_owner
    actions = []

    class DeferredCleanup(original_action):
        def observe(self):
            result = super().observe()
            if not self.finish and result.cleanup_complete:
                return action_reporting.process.ActionResult(
                    result.done, False, result.failures
                )
            return result

    def prepared_owner(action):
        actions.append(action)
        owner = original_owner(action)
        wait = owner.wait_cleanup
        release = threading.Event()
        policy = owner._run_policy

        def deferred_policy(end):
            assert release.wait(2), "test did not release the retained supervisor"
            policy(end)

        if delay == "supervisor":
            monkeypatch.setattr(owner, "_run_policy", deferred_policy)

        def observe_same_continuation(timeout):
            if delay == "helper":
                action.finish = True
            release.set()
            return wait(timeout)

        monkeypatch.setattr(owner, "wait_cleanup", observe_same_continuation)
        return owner

    if delay == "helper":
        monkeypatch.setattr(action_reporting, "Action", DeferredCleanup)
    monkeypatch.setattr(action_reporting, "action_owner", prepared_owner)
    action_reporting.test_cumulative_action_failures_are_not_duplicated_or_erased()
    assert len(actions) == 1
    assert actions[0].starts == (1 if delay == "helper" else 0)


def test_cleanup_requests_have_independent_project_lock_identities(tmp_path):
    case = named_context.TestPhase4Cleanup()
    first = case._request(tmp_path / "first")
    second = case._request(tmp_path / "second")
    assert first.project_root == str(tmp_path / "first")
    assert second.project_root == str(tmp_path / "second")
    assert first.project_root != second.project_root
    assert first.repo_root == first.project_root
    assert second.repo_root == second.project_root
    assert first.inventory_path == str(tmp_path / "first/docker-constructor.toml")
    assert second.inventory_path == str(tmp_path / "second/docker-constructor.toml")
