"""Controlled reproductions of hosted startup and observation scheduling gaps."""

import threading
import time
import unittest
from dataclasses import replace
from unittest import mock

import pytest

from docker.process import OwnedProcess
from tests import npm_http_logging_research as research
from tests import test_npm_environment_phase4_lifecycle as lifecycle_fixture
from tests import test_npm_http_logging_research as research_fixture
from tests import test_process_captured_action_faults as captured_fixture


def test_captured_helper_fixture_establishes_startup_before_stage_expiry():
    work = captured_fixture.CapturedAction._work

    def delayed_work(action):
        wait = action.request.wait

        def delayed_request(timeout=None):
            result = wait(timeout)
            if result and action.started:
                time.sleep(0.3)
            return result

        with mock.patch.object(action.request, "wait", delayed_request):
            work(action)

    with mock.patch.object(captured_fixture.CapturedAction, "_work", delayed_work):
        captured_fixture.test_real_main_retains_incomplete_captured_helper_and_late_release_outcome()


def test_deadline_fixture_excludes_unrelated_launch_scheduling():
    launch = OwnedProcess.launch

    def delayed_launch(*args, **kwargs):
        time.sleep(0.3)
        return launch(*args, **kwargs)

    result = unittest.TestResult()
    with mock.patch.object(OwnedProcess, "launch", delayed_launch):
        lifecycle_fixture.TestDeadlineSupervisor(
            "test_deadline_shorter_than_poll_interval_is_not_delayed"
        ).run(result)
    assert result.wasSuccessful(), (result.failures, result.errors)


def test_deadline_fixture_rejects_excessive_supervisor_poll_wait():
    start = OwnedProcess._start_supervisor

    def slow_poll(owner):
        wait = owner._request.wait

        def delayed_wait(timeout=None):
            return wait(0.2 if owner._supervising and not owner._requested else timeout)

        owner._request.wait = delayed_wait
        start(owner)

    result = unittest.TestResult()
    with mock.patch.object(OwnedProcess, "_start_supervisor", slow_poll):
        lifecycle_fixture.TestDeadlineSupervisor(
            "test_deadline_shorter_than_poll_interval_is_not_delayed"
        ).run(result)
    assert not result.errors and len(result.failures) == 1
    assert "all(interval <= 0.02" in result.failures[0][1]


@pytest.mark.parametrize(
    "name, false_claim",
    [
        ("test_output_read_while_child_waits_for_observation_is_confirmed_live", False),
        ("test_output_read_after_child_exit_is_not_confirmed_live", True),
    ],
)
def test_research_controls_reject_false_liveness_claims(name, false_claim):
    run = research_fixture._run

    def false_observation(*args, **kwargs):
        return replace(run(*args, **kwargs), confirmed_live_output=false_claim)

    result = unittest.TestResult()
    with mock.patch.object(research_fixture, "_run", false_observation):
        research_fixture.TestNpmHttpLoggingResearch(name).run(result)
    assert not result.errors and len(result.failures) == 1
    assert "observation.confirmed_live_output" in result.failures[0][1]


def test_research_contract_preserves_truthful_post_exit_observations():
    read = research._read_fd
    run = research._run
    observations = []

    def after_exit(fd, chunks, first, live, lock, child, gate=None):
        # Observe the original runner's reap, never independently wait/reap.
        end = time.monotonic() + 15
        pause = threading.Event()
        while child.returncode is None and time.monotonic() < end:
            pause.wait(0.001)
        assert child.returncode is not None
        return read(fd, chunks, first, live, lock, child, gate)

    def observed_run(*args, **kwargs):
        observation = run(*args, **kwargs)
        observations.append(observation)
        return observation

    result = unittest.TestResult()
    with (
        mock.patch.object(research, "_read_fd", after_exit),
        mock.patch.object(research, "_run", observed_run),
    ):
        research_fixture.TestNpmHttpLoggingResearch(
            "test_local_fixture_exercises_all_required_scenarios"
        ).run(result)
    assert result.wasSuccessful(), (result.failures, result.errors)
    assert len(observations) == 12  # Ten measurements and two cache seeds.
    assert all(item.first_observation_seconds is not None for item in observations)
    assert not any(item.confirmed_live_output for item in observations)
