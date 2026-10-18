"""6BC.3 RED: required compatibility cleanup survives child observation failure.

This binds the compatibility plan without Docker policy in the process
layer. The user authorized independent action initiation after failed child
observation; the original 6BB expectations are revised accordingly.
"""

from unittest import mock

import pytest

from tests.test_process_lifecycle import Child, workers, secondary
from tests.test_process_stop_policy import Action, api, launch


def test_required_compatibility_action_is_independent_of_local_poll_failure(api):
    child = Child()
    primary = OSError("domain operation")
    poll_error = OSError("local child observation")
    child.poll = mock.Mock(side_effect=poll_error)
    required = Action(api)
    policy = api.StopPolicy(
        (api.StopStage(0.01, required, local_stop_before=True),), 0.025
    )
    owner = launch(api, policy, child)
    try:
        result = owner.shutdown(primary=primary)
        assert result.complete and result.failures[0].error is primary
        assert any(f.error is poll_error for f in result.failures)
        assert child.stdout.attempts == child.stderr.attempts == 1
        assert child.actions == ["wait", "kill", "wait"]
        # Domain cleanup must not be conditional on successful main-child
        # observation. Cancel is release of prepared action capacity, not
        # an alternative domain request entry point.
        assert required.calls == ["prepare", "start", "cancel"]
    finally:
        owner.wait_cleanup(1)
        assert not workers()


@pytest.mark.parametrize("poll_kind", [OSError, RuntimeError, KeyboardInterrupt])
@pytest.mark.parametrize("start_kind", [OSError, RuntimeError, KeyboardInterrupt])
@pytest.mark.parametrize("primary_kind", [None, ValueError, KeyboardInterrupt])
def test_poll_and_action_failure_preserve_authority_and_independent_cleanup(
    api,
    poll_kind,
    start_kind,
    primary_kind,
):
    child = Child()
    poll_error = poll_kind("context poll")
    start_error = start_kind("independent action start")
    primary = primary_kind("operation") if primary_kind else None
    child.poll = mock.Mock(side_effect=poll_error)
    first, following = Action(api), Action(api)
    original_start = first.start

    def start(context):
        original_start(context)
        raise start_error

    first.start = start
    policy = api.StopPolicy(
        (api.StopStage(0.01, first), api.StopStage(0.01, following)), 0.025
    )
    owner = launch(api, policy, child)
    try:
        result = owner.shutdown(primary=primary)
        candidates = [e for e in (primary, poll_error, start_error) if e is not None]
        interruptions = [e for e in candidates if not isinstance(e, Exception)]
        defects = [e for e in (poll_error, start_error) if isinstance(e, RuntimeError)]
        winner = (interruptions or defects or candidates)[0]
        assert result.complete and result.failures[0].error is winner
        assert all(any(f.error is e for f in result.failures) for e in candidates)
        assert first.calls == following.calls == ["prepare", "start", "cancel"]
        assert first.contexts[0].returncode is following.contexts[0].returncode is None
        assert child.stdout.attempts == child.stderr.attempts == 1
        assert child.actions == ["wait", "kill", "wait"]
        before = (tuple(first.calls), tuple(following.calls), tuple(secondary(winner)))
        assert owner.shutdown().failures[0].error is winner
        assert owner.wait_cleanup(1).complete
        assert before == (
            tuple(first.calls),
            tuple(following.calls),
            tuple(secondary(winner)),
        )
    finally:
        owner.wait_cleanup(1)
        assert not workers()
