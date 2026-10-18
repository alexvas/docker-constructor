"""Lifecycle failure matrix for the only new file-read seam."""

from types import SimpleNamespace

import pytest

import docker._owned_file as seam
from tests.enforcement.green_fixtures import read_bytes


@pytest.mark.parametrize(
    ("operation_error", "close_error"),
    [
        (None, None),
        (None, OSError("close")),
        (OSError("read"), OSError("close")),
        (KeyboardInterrupt("read interrupted"), OSError("close")),
        (None, KeyboardInterrupt("close interrupted")),
    ],
)
def test_owned_fixture_read_failure_order_and_repeat_cleanup(
    monkeypatch, operation_error, close_error
):
    owners = []
    original = seam.OwnedStream
    closed = []

    def prepare(*args, **kwargs):
        owner = original(*args, **kwargs)
        owners.append(owner)
        return owner

    def read():
        if operation_error:
            raise operation_error
        return b"fixture"

    def close():
        closed.append(1)
        if close_error:
            raise close_error

    stream = SimpleNamespace(read=read, close=close)
    path = SimpleNamespace(open=lambda mode: stream)
    monkeypatch.setattr(seam, "OwnedStream", prepare)
    expected = operation_error or close_error
    if expected:
        with pytest.raises(type(expected)) as caught:
            read_bytes(path)
        assert caught.value is expected
        if operation_error and close_error:
            assert close_error in operation_error._transaction_secondary
    else:
        assert read_bytes(path) == b"fixture"
    assert len(owners) == 1
    owners[0].close()
    owners[0].close()
    assert closed == [1]


def test_independent_fixture_read_after_failed_release():
    closed = []

    def broken_close():
        closed.append("broken")
        raise OSError("close")

    first = SimpleNamespace(
        open=lambda mode: SimpleNamespace(
            read=lambda: b"first",
            close=broken_close,
        )
    )
    second = SimpleNamespace(
        open=lambda mode: SimpleNamespace(
            read=lambda: b"second",
            close=lambda: closed.append("second"),
        )
    )
    with pytest.raises(OSError, match="close"):
        read_bytes(first)
    assert read_bytes(second) == b"second"
    assert closed == ["broken", "second"]
