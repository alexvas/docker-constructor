"""Focused network-resource repairs; broader coordination remains planned."""
import io
from types import SimpleNamespace
from urllib.error import HTTPError
from unittest.mock import Mock

import pytest

from docker import networking
from docker.versioning import artifact_cache, transports


class Body(io.BytesIO):
    """Descriptor-less response body with observable release attempts."""
    def __init__(self, read_error=None, close_error=None):
        super().__init__(b"not found")
        self.read_error = read_error
        self.close_error = close_error
        self.close_calls = 0

    def read(self, *args):
        if self.read_error is not None:
            raise self.read_error
        return super().read(*args)

    def close(self):
        self.close_calls += 1
        super().close()
        if self.close_error is not None:
            raise self.close_error


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("close_error", [None, OSError("close"), KeyboardInterrupt("close")])
def test_http_error_body_is_released_once(monkeypatch, streaming, close_error):
    body = Body(close_error=close_error)
    error = HTTPError("https://example.invalid/", 404, "missing", {}, body)
    monkeypatch.setattr("urllib.request.urlopen", Mock(side_effect=error))
    chunks = artifact_cache.HttpStreamingTransport().fetch_chunks(error.url)
    try:
        if streaming:
            expected = close_error if isinstance(close_error, KeyboardInterrupt) else error
            with pytest.raises(BaseException) as raised:
                next(chunks)
            assert raised.value is expected
            chunks.close()
        elif close_error is not None:
            with pytest.raises(BaseException) as raised:
                transports.build_transports(no_cache=True).http.request("GET", error.url)
            assert raised.value is close_error
        else:
            result = transports.build_transports(no_cache=True).http.request("GET", error.url)
            assert (result.status, result.body) == (404, b"not found")
        assert body.closed
        assert body.close_calls == 1
        error.close()
        assert body.close_calls == 1
    finally:
        chunks.close()
        body.close_error = None
        error.close()


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("operation", [OSError("read"), KeyboardInterrupt("read")])
def test_http_error_operation_survives_ordinary_close(monkeypatch, streaming, operation):
    body = Body(read_error=operation, close_error=OSError("close"))
    error = HTTPError("https://example.invalid/", 404, "missing", {}, body)
    monkeypatch.setattr("urllib.request.urlopen", Mock(side_effect=error))
    try:
        with pytest.raises(BaseException) as raised:
            if streaming:
                next(artifact_cache.HttpStreamingTransport().fetch_chunks(error.url))
            else:
                transports.build_transports(no_cache=True).http.request("GET", error.url)
        assert raised.value is (error if streaming else operation)
        assert body.close_calls == 1
    finally:
        body.close_error = None
        error.close()


@pytest.mark.parametrize("close_error", [None, OSError("iterator close")])
def test_artifact_consumer_finalizes_retained_iterator(monkeypatch, tmp_path, close_error):
    from tests.test_constructor_materialization import (
        _FakeFilesystem, _FakeLockFactory, _FakeTempDir, _VALID_INTEGRITY,
    )
    closed = []

    def generate():
        try:
            yield b"payload"
            yield b"unconsumed"
        finally:
            closed.append(True)
            if close_error is not None:
                raise close_error

    chunks = generate()
    filesystem = _FakeFilesystem()
    operation = OSError("write")
    monkeypatch.setattr(filesystem, "append_temp", Mock(side_effect=operation))
    root = tmp_path / "runtime-artifacts"
    (root / "tmp").mkdir(parents=True)
    try:
        with pytest.raises(artifact_cache.ArtifactMaterializationError) as raised:
            artifact_cache.materialize_selected_artifacts(
                [artifact_cache.SelectedArtifact(url="https://example.invalid/", integrity=_VALID_INTEGRITY)],
                transport=SimpleNamespace(fetch_chunks=lambda url: chunks),
                filesystem=filesystem, lock_factory=_FakeLockFactory(), temp_dir=_FakeTempDir(),
                cache_root=str(root / "blobs"),
            )
        assert raised.value.__cause__ is operation
        assert closed == [True]
        chunks.close()
        assert closed == [True]
    finally:
        chunks.close()


def probe_fakes(monkeypatch, *, start_error=None, shutdown_error=None, close_error=None):
    server = SimpleNamespace(
        server_address=("0.0.0.0", 12345), serve_forever=Mock(),
        shutdown=Mock(side_effect=shutdown_error), server_close=Mock(side_effect=close_error),
    )
    thread = SimpleNamespace(start=Mock(side_effect=start_error), join=Mock())
    monkeypatch.setattr(networking.http.server, "ThreadingHTTPServer", Mock(return_value=server))
    monkeypatch.setattr(networking, "threading", SimpleNamespace(Thread=Mock(return_value=thread)))
    return server, thread


def test_probe_owner_preparation_precedes_acquisition(monkeypatch):
    factory = Mock()
    monkeypatch.setattr(networking.http.server, "ThreadingHTTPServer", factory)
    failure = RuntimeError("owner preparation")
    monkeypatch.setattr(networking, "OwnedStream", Mock(side_effect=failure))
    probe = networking.HostProbeServer()
    with pytest.raises(RuntimeError) as raised:
        probe.start()
    assert raised.value is failure
    factory.assert_not_called()
    probe.stop()


def test_probe_server_construction_failure_has_no_fabricated_cleanup(monkeypatch):
    failure = OSError("bind")
    monkeypatch.setattr(networking.http.server, "ThreadingHTTPServer", Mock(side_effect=failure))
    probe = networking.HostProbeServer()
    with pytest.raises(OSError) as raised:
        probe.start()
    assert raised.value is failure
    probe.stop()


def test_probe_thread_construction_failure_closes_listener(monkeypatch):
    server, _ = probe_fakes(monkeypatch)
    failure = RuntimeError("thread construction")
    networking.threading.Thread.side_effect = failure
    probe = networking.HostProbeServer()
    with pytest.raises(RuntimeError) as raised:
        probe.start()
    assert raised.value is failure
    server.shutdown.assert_not_called()
    server.server_close.assert_called_once()
    probe.stop()
    server.server_close.assert_called_once()


def test_probe_start_and_close_failure_preserve_start(monkeypatch):
    primary, cleanup = OSError("start"), OSError("close")
    server, thread = probe_fakes(monkeypatch, start_error=primary, close_error=cleanup)
    probe = networking.HostProbeServer()
    with pytest.raises(OSError) as raised:
        probe.start()
    assert raised.value is primary
    server.server_close.assert_called_once()
    thread.join.assert_not_called()
    probe.stop()
    server.server_close.assert_called_once()


def test_probe_reentrant_cleanup_cannot_release_twice(monkeypatch):
    server, thread = probe_fakes(monkeypatch)
    probe = networking.HostProbeServer()
    probe.start()
    server.server_close.side_effect = probe.stop
    probe.stop()
    server.server_close.assert_called_once()
    thread.join.assert_called_once_with(timeout=5)


def test_probe_real_listener_and_worker_are_released():
    probe = networking.HostProbeServer()
    try:
        assert probe.start() > 0
        server, thread = probe._httpd, probe._thread
        assert server is not None and thread is not None
        probe.stop()
        assert server.socket.fileno() == -1
        assert not thread.is_alive()
        probe.stop()
    finally:
        probe.stop()


@pytest.mark.parametrize("start_error", [OSError("start"), KeyboardInterrupt("start")])
def test_probe_start_failure_closes_without_shutdown_or_join(monkeypatch, start_error):
    server, thread = probe_fakes(monkeypatch, start_error=start_error)
    probe = networking.HostProbeServer()
    with pytest.raises(BaseException) as raised:
        probe.start()
    assert raised.value is start_error
    server.server_close.assert_called_once()
    server.shutdown.assert_not_called()
    thread.join.assert_not_called()
    probe.stop()
    server.server_close.assert_called_once()


@pytest.mark.parametrize("shutdown_error", [None, OSError("shutdown"), KeyboardInterrupt("shutdown")])
@pytest.mark.parametrize("close_error", [None, OSError("close"), KeyboardInterrupt("close")])
def test_probe_stop_attempts_independent_cleanup_once(monkeypatch, shutdown_error, close_error):
    server, thread = probe_fakes(monkeypatch, shutdown_error=shutdown_error, close_error=close_error)
    probe = networking.HostProbeServer()
    assert probe.start() == 12345
    expected = next((e for e in (shutdown_error, close_error) if isinstance(e, KeyboardInterrupt)),
                    shutdown_error or close_error)
    if expected is None:
        probe.stop()
    else:
        with pytest.raises(BaseException) as raised:
            probe.stop()
        assert raised.value is expected
    try:
        probe.stop()
    except BaseException as repeated:
        pytest.fail(f"repeat stop raised {type(repeated).__name__}")
    server.shutdown.assert_called_once()
    server.server_close.assert_called_once()
    thread.join.assert_called_once_with(timeout=5)
