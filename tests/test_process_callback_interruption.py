"""6BC.5 RED: first-delivery reporter interruption and cleanup precedence."""

from unittest import mock

import pytest

import docker.process as process
from tests.test_process_stream_outcomes import Child, enums, launch


@pytest.mark.parametrize("source", ["callback", "reporter", "fallback"])
@pytest.mark.parametrize("kind", [KeyboardInterrupt, SystemExit, GeneratorExit])
@pytest.mark.parametrize("callback_kind", ["chunk", "end"])
def test_first_delivery_interruption_preserves_identity_and_independent_cleanup(
    source, kind, callback_kind
):
    stream, _ = enums()
    first, later = kind("first"), SystemExit("later")
    close_error = OSError("independent close")
    child = Child()
    close = child.stdout.close

    def fail_close():
        close()
        raise close_error

    child.stdout.close = fail_close
    calls, reports = [], []

    def report(error, **context):
        reports.append(error)
        if source == "reporter":
            raise first if len(reports) == 1 else later
        if source == "fallback":
            raise ValueError("ordinary reporter error")

    def callback(identity, value):
        calls.append((callback_kind, identity))
        if source == "callback":
            raise first if len(calls) == 1 else later
        raise ValueError("ordinary callback error")

    ends = []

    def end(identity, terminal):
        ends.append(identity)
        if callback_kind == "end":
            callback(identity, terminal)

    owner = launch(child, reporter=report)
    try:
        with mock.patch.object(
            process,
            "_fallback_callback_diagnostic",
            create=True,
            side_effect=[first, later],
        ):
            with pytest.raises(kind) as caught:
                owner.stream(
                    callback if callback_kind == "chunk" else lambda *a: None,
                    on_stream_end=end,
                )
        assert caught.value is first
        assert len(ends) == 2 and set(ends) == set(stream)
        status = owner.cleanup_status
        assert status.failures[0].error is first
        assert status.failures[0].phase.name == (
            "CALLBACK" if source == "callback" else "REPORTER"
        )
        assert not hasattr(first, "process_result")
        assert any(f.error is close_error for f in status.failures)
        assert child.stdout.close_attempts == child.stderr.close_attempts == 1
        assert child.wait_attempts == 1
        before = (tuple(calls), tuple(reports), tuple(ends))
        assert owner.wait_cleanup(1).complete
        owner.shutdown()
        assert before == (tuple(calls), tuple(reports), tuple(ends))
        assert child.stdout.close_attempts == child.stderr.close_attempts == 1
    finally:
        owner.shutdown()
        assert owner.wait_cleanup(1).complete
