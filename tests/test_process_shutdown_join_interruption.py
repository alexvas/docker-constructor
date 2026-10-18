"""New shutdown interruptions must not be suppressed as delivered failures."""

from unittest import mock

import pytest

from docker.process import FailureKind, FailurePhase, OwnedProcess
from tests.test_process_failure_snapshot import Child


@pytest.mark.parametrize("method", ["capture", "stream"])
@pytest.mark.parametrize("operation", ["context", "shutdown"])
@pytest.mark.parametrize("prior_failure", [False, True])
def test_post_collection_join_interruption_is_new_control_flow(
    method, operation, prior_failure
):
    child = Child()
    owner = OwnedProcess.launch(
        ["child"], factory=lambda *a, **k: child, grace_seconds=0.1
    )
    original = OSError("already delivered")
    interruption = KeyboardInterrupt("new continuation join")
    try:
        assert all(event.wait(1) for event in owner._stopped)
        with mock.patch.object(
            child,
            "poll",
            side_effect=original if prior_failure else None,
            return_value=7,
        ):
            result = (
                owner.capture()
                if method == "capture"
                else owner.stream(lambda *args: None)
            )
        assert result.lifecycle.complete
        snapshot = result.lifecycle.failures
        assert bool(snapshot) is prior_failure
        if prior_failure:
            assert snapshot[0].error is original
        with mock.patch.object(
            owner._continuation, "join", side_effect=interruption
        ) as join:
            with pytest.raises(KeyboardInterrupt) as caught:
                if operation == "context":
                    with owner:
                        pass
                else:
                    owner.shutdown()
            assert caught.value is interruption
            assert join.call_count == 1
        assert interruption.cleanup is owner
        assert not hasattr(interruption, "process_result")
        assert result.lifecycle.failures is snapshot
        failure = next(
            f for f in owner.cleanup_status.failures if f.error is interruption
        )
        assert failure.kind is FailureKind.INTERRUPTION
        assert failure.phase is FailurePhase.JOIN and failure.stream is None
        # Once delivered, the new interruption is not replayed by repeat cleanup.
        assert owner.shutdown().complete
        with owner:
            pass
        assert child.stdout.attempts == child.stderr.attempts == 1
    finally:
        owner.shutdown()
        assert owner.wait_cleanup(1).complete


def test_shutdown_new_join_interruption_does_not_replay_delivered_interruption():
    child = Child()
    owner = OwnedProcess.launch(
        ["child"], factory=lambda *a, **k: child, grace_seconds=0.1
    )
    original = KeyboardInterrupt("delivered during collection")
    new = KeyboardInterrupt("new join interruption")
    try:
        assert all(event.wait(1) for event in owner._stopped)
        with mock.patch.object(child, "poll", side_effect=original):
            with pytest.raises(KeyboardInterrupt) as caught:
                owner.capture()
        assert caught.value is original
        assert owner.shutdown().complete
        with mock.patch.object(owner._continuation, "join", side_effect=new):
            with pytest.raises(KeyboardInterrupt) as caught:
                owner.shutdown()
        assert caught.value is new
        assert new.cleanup is owner
        assert owner.cleanup_status.failures[0].error is original
        assert owner.shutdown().complete
        assert child.stdout.attempts == child.stderr.attempts == 1
    finally:
        owner.shutdown()
        assert owner.wait_cleanup(1).complete
