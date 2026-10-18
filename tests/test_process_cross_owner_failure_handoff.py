"""Sharing an exact error cannot reanchor either owner's immutable values."""

import pytest

from docker.process import FailurePhase, OwnedProcess
from tests.test_process_failure_snapshot import Child, Pipe


def test_cross_owner_delivery_keeps_separate_values_without_exception_snapshots():
    error = OSError("helper release failed")
    helper = Child(error)
    parent = Child()
    parent.stdout, parent.stderr = Pipe(b"parent stdout"), Pipe(b"parent stderr")
    owners = []
    try:
        helper_owner = OwnedProcess.launch(
            ["helper"], factory=lambda *a, **k: helper, grace_seconds=0.2
        )
        owners.append(helper_owner)
        helper_result = helper_owner.capture()
        helper_failure = helper_result.lifecycle.failures[0]
        assert helper_result.stdout == b"retained stdout"
        assert (
            helper_failure.error is error
            and helper_failure.phase is FailurePhase.RELEASE
        )
        assert not hasattr(error, "process_result")

        parent_owner = OwnedProcess.launch(
            ["parent"], factory=lambda *a, **k: parent, grace_seconds=0.2
        )
        owners.append(parent_owner)
        assert all(event.wait(1) for event in parent_owner._stopped)
        # This test supplies a caller operation, not managed-action transport;
        # the separate strict-action suite binds helper rebasing to ACTION.
        parent_owner.shutdown(primary=error)
        parent_result = parent_owner.capture()
        parent_failure = parent_result.lifecycle.failures[0]
        assert parent_result.stdout == b"parent stdout"
        assert (
            parent_failure.error is error
            and parent_failure.phase is FailurePhase.COORDINATION
        )
        assert (
            parent_result is not helper_result and parent_failure is not helper_failure
        )
        assert helper_result.stdout == b"retained stdout"
        assert helper_result.lifecycle.failures == (helper_failure,)
        assert helper_failure.phase is FailurePhase.RELEASE
        assert not hasattr(error, "process_result")
        assert (
            helper_owner.cleanup_status.complete
            and parent_owner.cleanup_status.complete
        )
        for owner in owners:
            with pytest.raises(RuntimeError):
                owner.capture()
    finally:
        for owner in owners:
            owner.shutdown()
            assert owner.wait_cleanup(1).complete
        if owners:
            assert helper.stdout.attempts == helper.stderr.attempts == 1
        if len(owners) == 2:
            assert parent.stdout.attempts == parent.stderr.attempts == 1
