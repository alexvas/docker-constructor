"""Shutdown must publish new control flow even after incomplete delivery."""

import threading
from unittest import mock

import pytest

import docker.process as process
from tests.test_process_lifecycle import Child, stalled


@pytest.mark.parametrize("method", ["capture", "stream"])
@pytest.mark.parametrize("operation", ["shutdown", "context"])
def test_incomplete_collection_wait_interruption_without_continuation_publication(
    method, operation
):
    child = Child()
    owner, pipe = stalled(process, child)
    paused, resume = threading.Event(), threading.Event()
    original_publish = owner._publish
    interruption = KeyboardInterrupt("new phase wait interruption")

    def publish(complete):
        original_publish(complete)
        paused.set()
        assert resume.wait(5), "test-controlled continuation was not released"

    try:
        with mock.patch.object(owner, "_publish", publish):
            assert not owner.shutdown().complete
            assert paused.wait(1)
            result = (
                owner.capture()
                if method == "capture"
                else owner.stream(lambda *args: None)
            )
            assert not result.lifecycle.complete
            snapshot = result.lifecycle.failures
            assert owner._collection_delivered
            assert not owner._phase_done.is_set()
            assert pipe.attempts == 0
            with mock.patch.object(
                owner._phase_done, "wait", side_effect=interruption
            ) as wait:
                with pytest.raises(KeyboardInterrupt) as caught:
                    if operation == "shutdown":
                        owner.shutdown()
                    else:
                        with owner:
                            pass
                assert caught.value is interruption
                assert wait.call_count == 1
            assert interruption.cleanup is owner
            assert not hasattr(interruption, "process_result")
            assert not owner.cleanup_status.complete
            failure = next(
                f for f in owner.cleanup_status.failures if f.error is interruption
            )
            assert failure.kind is process.FailureKind.INTERRUPTION
            assert failure.phase is process.FailurePhase.COORDINATION
            assert failure.stream is None
            assert result.lifecycle.failures is snapshot
            assert all(f.error is not interruption for f in snapshot)
            assert pipe.attempts == 0
            # The interruption must not replay, even while publication is paused.
            assert not owner.shutdown().complete
            with owner:
                pass
    finally:
        pipe.release.set()
        resume.set()
        assert owner.wait_cleanup(2).complete
        assert not owner._continuation.is_alive()
        assert pipe.attempts == child.stderr.attempts == 1
        assert owner.shutdown().complete
