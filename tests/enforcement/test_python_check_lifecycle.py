"""Lifecycle contracts for the Python check tooling (P2 task 2.13).

The tooling adds three ownership branches: one ``OwnedDescriptor`` per
regular-file read, one directory capability per traversed directory, and one
``OwnedProcess`` per check-command invocation.  These tests pin the mandatory
failure matrix for each branch: an active operation failure stays primary over
an ordinary release failure, a sole release failure is reported, a
process-control interruption keeps its identity while cleanup still runs,
repeated cleanup never retries an ambiguous release, independent resources
release once, and incomplete process cleanup is never reported as clean.
Directory and process cases use the shared ownership APIs themselves; only the
incomplete-cleanup observation uses a documented-contract stub because the
real owner completes deterministically on this platform.
"""

from __future__ import annotations

import errno
import os
import sys
from unittest import mock

import pytest

from docker.filesystem.descriptors import DirectoryDescriptor
from docker.filesystem.operations import PosixDescriptorOps
from docker.filesystem.streams import OwnedStream
from docker.process import (
    FailureInfo,
    FailureKind,
    FailurePhase,
    LifecycleResult,
    OwnedProcess,
    ProcessResult,
    ProcessStream,
)
from tests.enforcement.python_check_fixtures import load_check_module


@pytest.fixture(scope="module")
def checks():
    return load_check_module()


def _fd_path(fd: int) -> str | None:
    try:
        return os.path.normpath(os.readlink(f"/proc/self/fd/{fd}"))
    except OSError:
        return None


def _secondary_exceptions(exc: BaseException) -> list[object]:
    secondary = getattr(exc, "secondary", None)
    if isinstance(secondary, list):
        return list(secondary)
    slot = getattr(exc, "_transaction_secondary", None)
    if isinstance(slot, list):
        return list(slot)
    return list(getattr(exc, "__notes__", []))


def _carries_secondary(exc: BaseException, needle: BaseException) -> bool:
    for item in _secondary_exceptions(exc):
        if item is needle:
            return True
        if isinstance(item, str) and str(needle) in item:
            return True
    return False


@pytest.fixture()
def capability(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "first.py").write_text("value = 1\n", encoding="utf-8")
    (root / "second.py").write_text("value = 2\n", encoding="utf-8")
    capability = DirectoryDescriptor.open_secure_path(
        PosixDescriptorOps(), str(root), label="root", require_owner=False
    )
    try:
        yield root, capability
    finally:
        capability.close()


def _failing_read(target: str, error: BaseException):
    real_read = os.read

    def failing(fd: int, *args, **kwargs) -> bytes:
        if _fd_path(fd) == target:
            raise error
        return real_read(fd, *args, **kwargs)

    return failing


def _close_hook(target: str, error: BaseException):
    real_close = os.close
    attempts: list[int] = []

    def failing(fd: int, *args, **kwargs) -> None:
        if _fd_path(fd) == target:
            attempts.append(fd)
            real_close(fd, *args, **kwargs)
            raise error
        real_close(fd, *args, **kwargs)

    return failing, attempts


def test_open_failure_maps_to_a_selection_error(checks, monkeypatch, capability):
    root, directory = capability
    open_error = OSError(errno.ELOOP, "injected open failure")
    monkeypatch.setattr(os, "open", mock.Mock(side_effect=open_error))

    with pytest.raises(checks.SourceSelectionError) as context:
        checks._read_entry(directory, "first.py", "root/first.py")

    assert context.value.__cause__ is open_error


def test_read_failure_stays_primary_over_a_close_failure(
    checks, monkeypatch, capability
):
    root, directory = capability
    target = os.path.normpath(str(root / "first.py"))
    read_error = OSError(errno.EIO, "injected read failure")
    close_error = OSError(errno.EIO, "injected close failure")
    failing_close, attempts = _close_hook(target, close_error)
    monkeypatch.setattr(os, "read", _failing_read(target, read_error))
    monkeypatch.setattr(os, "close", failing_close)

    with pytest.raises(checks.SourceSelectionError) as context:
        checks._read_entry(directory, "first.py", "root/first.py")

    assert context.value.__cause__ is read_error
    assert _carries_secondary(context.value, close_error)
    assert len(attempts) == 1


def test_sole_close_failure_is_reported(checks, monkeypatch, capability):
    root, directory = capability
    target = os.path.normpath(str(root / "first.py"))
    close_error = OSError(errno.EIO, "injected close failure")
    failing_close, attempts = _close_hook(target, close_error)
    monkeypatch.setattr(os, "close", failing_close)

    with pytest.raises(checks.SourceSelectionError) as context:
        checks._read_entry(directory, "first.py", "root/first.py")

    assert context.value.__cause__ is close_error
    assert "cannot release candidate" in str(context.value)
    assert len(attempts) == 1


def test_close_interruption_identity_is_preserved(checks, monkeypatch, capability):
    root, directory = capability
    target = os.path.normpath(str(root / "first.py"))
    interruption = KeyboardInterrupt("injected close interruption")
    failing_close, attempts = _close_hook(target, interruption)
    monkeypatch.setattr(os, "close", failing_close)

    with pytest.raises(KeyboardInterrupt) as context:
        checks._read_entry(directory, "first.py", "root/first.py")

    assert context.value is interruption
    assert len(attempts) == 1


def test_repeat_cleanup_after_a_failed_close_is_not_retried(
    checks, monkeypatch, capability
):
    root, directory = capability
    target = os.path.normpath(str(root / "first.py"))
    close_error = OSError(errno.EIO, "injected close failure")
    failing_close, attempts = _close_hook(target, close_error)
    created: list[object] = []
    real_owner = checks.OwnedDescriptor

    class Capturing(real_owner):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

    monkeypatch.setattr(checks, "OwnedDescriptor", Capturing)
    monkeypatch.setattr(os, "close", failing_close)

    with pytest.raises(checks.SourceSelectionError):
        checks._read_entry(directory, "first.py", "root/first.py")

    assert len(created) == 1
    owner = created[0]
    assert owner.released
    owner.close()
    owner.close()
    assert len(attempts) == 1


def test_independent_reads_release_each_descriptor_once(
    checks, monkeypatch, capability
):
    root, directory = capability
    closed: list[str] = []
    real_close = os.close

    def recording(fd: int, *args, **kwargs) -> None:
        path = _fd_path(fd)
        if path is not None:
            closed.append(path)
        real_close(fd, *args, **kwargs)

    monkeypatch.setattr(os, "close", recording)

    assert checks._read_entry(directory, "first.py", "root/first.py") == b"value = 1\n"
    assert (
        checks._read_entry(directory, "second.py", "root/second.py") == b"value = 2\n"
    )

    expected = [
        os.path.normpath(str(root / "first.py")),
        os.path.normpath(str(root / "second.py")),
    ]
    assert sorted(closed) == sorted(expected)


def test_failed_command_is_not_reported_as_clean(checks, tmp_path):
    result = checks.run_captured(["definitely-not-a-check-command"], root=tmp_path)

    assert result.returncode is None
    assert result.clean is False


def test_unavailable_executable_is_a_discovery_error(checks, tmp_path):
    with pytest.raises(checks.RuffDiscoveryError):
        checks.ruff_visible_files(tmp_path, ruff=str(tmp_path / "missing-ruff"))


@pytest.fixture()
def nested_capability(tmp_path):
    root = tmp_path / "repo"
    (root / "pkg" / "inner").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_text("value = 1\n", encoding="utf-8")
    (root / "pkg" / "inner" / "deep.py").write_text("value = 2\n", encoding="utf-8")
    capability = DirectoryDescriptor.open_secure_path(
        PosixDescriptorOps(), str(root), label="repo", require_owner=False
    )
    try:
        yield root, capability
    finally:
        capability.close()


@pytest.fixture()
def scannable(tmp_path):
    root = tmp_path / "repo"
    for name in ("docker", "tests", "scripts"):
        (root / name).mkdir(parents=True)
        (root / name / f"{name}_entry.py").write_text("value = 1\n", encoding="utf-8")
    (root / "scripts" / "pkg" / "inner").mkdir(parents=True)
    (root / "scripts" / "pkg" / "mod.py").write_text("value = 1\n", encoding="utf-8")
    (root / "scripts" / "pkg" / "inner" / "deep.py").write_text(
        "value = 2\n", encoding="utf-8"
    )
    return root


def _stat_hook(directory_path: str, name: str, error: BaseException):
    real_stat = PosixDescriptorOps.statat
    attempts: list[str] = []

    def failing(self, directory_fd, child, *, follow_symlinks):
        if child == name and _fd_path(directory_fd) == directory_path:
            attempts.append(child)
            raise error
        return real_stat(self, directory_fd, child, follow_symlinks=follow_symlinks)

    return failing, attempts


def test_traversal_failure_stays_primary_over_directory_release_failure(
    checks, monkeypatch, nested_capability
):
    root, capability = nested_capability
    package = os.path.normpath(str(root / "pkg"))
    traversal_error = OSError(errno.EIO, "injected traversal failure")
    close_error = OSError(errno.EIO, "injected directory close failure")
    failing_stat, stat_attempts = _stat_hook(package, "mod.py", traversal_error)
    failing_close, close_attempts = _close_hook(package, close_error)
    monkeypatch.setattr(PosixDescriptorOps, "statat", failing_stat)
    monkeypatch.setattr(os, "close", failing_close)

    with pytest.raises(checks.SourceSelectionError) as context:
        checks._walk(capability, "repo", {}, lambda *args: None, [])

    assert context.value.__cause__ is traversal_error
    assert _carries_secondary(context.value, close_error)
    assert stat_attempts == ["mod.py"]
    assert len(close_attempts) == 1


def test_directory_release_failure_is_reported(checks, monkeypatch, nested_capability):
    root, capability = nested_capability
    package = os.path.normpath(str(root / "pkg"))
    close_error = OSError(errno.EIO, "injected directory close failure")
    failing_close, attempts = _close_hook(package, close_error)
    monkeypatch.setattr(os, "close", failing_close)

    with pytest.raises(checks.SourceSelectionError) as context:
        checks._walk(capability, "repo", {}, lambda *args: None, [])

    assert context.value.__cause__ is close_error
    assert "cannot release directory" in str(context.value)
    assert len(attempts) == 1


def test_directory_release_interruption_keeps_identity_and_parent_is_released(
    checks, monkeypatch, nested_capability
):
    root, capability = nested_capability
    package = os.path.normpath(str(root / "pkg"))
    repository = os.path.normpath(str(root))
    interruption = KeyboardInterrupt("injected directory close interruption")
    real_close = os.close
    package_attempts: list[int] = []
    parent_attempts: list[int] = []

    def failing(fd: int) -> None:
        path = _fd_path(fd)
        if path == package:
            package_attempts.append(fd)
            real_close(fd)
            raise interruption
        if path == repository:
            parent_attempts.append(fd)
        real_close(fd)

    monkeypatch.setattr(os, "close", failing)

    with pytest.raises(KeyboardInterrupt) as context:
        with capability:
            checks._walk(capability, "repo", {}, lambda *args: None, [])

    assert context.value is interruption
    assert len(package_attempts) == 1
    assert capability.released is True
    assert len(parent_attempts) == 1


def test_repeated_directory_cleanup_does_not_retry_a_failed_release(
    checks, monkeypatch, nested_capability
):
    root, capability = nested_capability
    package = os.path.normpath(str(root / "pkg"))
    close_error = OSError(errno.EIO, "injected directory close failure")
    failing_close, attempts = _close_hook(package, close_error)
    created: list[DirectoryDescriptor] = []
    real_open = DirectoryDescriptor.open_directory

    def recording(self, name, **kwargs):
        child = real_open(self, name, **kwargs)
        created.append(child)
        return child

    monkeypatch.setattr(DirectoryDescriptor, "open_directory", recording)
    monkeypatch.setattr(os, "close", failing_close)

    with pytest.raises(checks.SourceSelectionError):
        checks._walk(capability, "repo", {}, lambda *args: None, [])

    assert created
    owner = created[0]
    assert owner.label == "repo/pkg"
    assert owner.released is True
    owner.close()
    owner.close()
    assert len(attempts) == 1


def test_child_directory_release_failure_does_not_prevent_parent_release(
    checks, monkeypatch, scannable
):
    inner = os.path.normpath(str(scannable / "scripts" / "pkg" / "inner"))
    package = os.path.normpath(str(scannable / "scripts" / "pkg"))
    scripts = os.path.normpath(str(scannable / "scripts"))
    close_error = OSError(errno.EIO, "injected inner close failure")
    real_close = os.close
    inner_attempts: list[int] = []
    parent_attempts: list[str] = []

    def failing(fd: int) -> None:
        path = _fd_path(fd)
        if path == inner:
            inner_attempts.append(fd)
            real_close(fd)
            raise close_error
        if path in (package, scripts):
            parent_attempts.append(path)
        real_close(fd)

    monkeypatch.setattr(os, "close", failing)

    with pytest.raises(checks.SourceSelectionError) as context:
        checks._scan(str(scannable), None, lambda *args: None)

    assert context.value.__cause__ is close_error
    assert "cannot release directory" in str(context.value)
    assert len(inner_attempts) == 1
    assert parent_attempts == [package, scripts]


def test_directory_listing_failure_is_a_bounded_selection_error(
    checks, monkeypatch, nested_capability
):
    root, capability = nested_capability
    package = os.path.normpath(str(root / "pkg"))
    listing_error = OSError(errno.EIO, "injected directory listing failure")
    real_listdir = PosixDescriptorOps.listdir

    def failing(self, directory_fd):
        if _fd_path(directory_fd) == package:
            raise listing_error
        return real_listdir(self, directory_fd)

    monkeypatch.setattr(PosixDescriptorOps, "listdir", failing)

    with pytest.raises(checks.SourceSelectionError) as context:
        checks._walk(capability, "repo", {}, lambda *args: None, [])

    assert context.value.__cause__ is listing_error
    assert "cannot list directory" in str(context.value)


def test_source_root_release_failure_is_reported(checks, monkeypatch, scannable):
    scripts = os.path.normpath(str(scannable / "scripts"))
    close_error = OSError(errno.EIO, "injected source root close failure")
    failing_close, attempts = _close_hook(scripts, close_error)
    monkeypatch.setattr(os, "close", failing_close)

    with pytest.raises(checks.SourceSelectionError) as context:
        checks._scan(str(scannable), None, lambda *args: None)

    assert context.value.__cause__ is close_error
    assert "cannot release directory" in str(context.value)
    assert len(attempts) == 1


def _failure(error, phase, stream=None):
    return FailureInfo(error, FailureKind.ORDINARY, phase, stream)


def _process_result(failures=(), *, complete=True, returncode=0):
    return ProcessResult(
        b"", b"", returncode, False, LifecycleResult(complete, tuple(failures))
    )


class _StubOwner:
    """Documented-contract owner stub for incomplete-cleanup observation."""

    def __init__(self, result, *, exit_error=None, collection_error=None):
        self.result = result
        self.exit_error = exit_error
        self.collection_error = collection_error
        self.collections = 0
        self.callback = None
        self.exits = 0
        self.exit_primary = None
        self._status = result.lifecycle

    @property
    def cleanup_status(self):
        return self._status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.exits += 1
        self.exit_primary = exc
        if exc is None and self.exit_error is not None:
            raise self.exit_error
        return False

    def capture(self):
        self.collections += 1
        if self.collection_error is not None:
            raise self.collection_error
        return self.result

    def stream(self, callback, *, on_stream_end=None):
        self.collections += 1
        self.callback = callback
        if self.collection_error is not None:
            raise self.collection_error
        return self.result

    def wait_cleanup(self, timeout):
        assert timeout > 0
        self._status = LifecycleResult(True, self._status.failures)
        return self._status


def _install_stub_launch(monkeypatch, checks, owner):
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    class StubLauncher:
        @classmethod
        def launch(cls, argv, **kwargs):
            calls.append((tuple(argv), kwargs))
            return owner

    monkeypatch.setattr(checks, "OwnedProcess", StubLauncher)
    return calls


def _run_wrapper(checks, kind, root, argv=("check-tool",)):
    if kind == "captured":
        return checks.run_captured(list(argv), root=root)
    return checks.run_streaming(list(argv), root=root)


@pytest.mark.parametrize("kind", ["captured", "streaming"])
def test_process_wrapper_keeps_primary_failure_before_release_failure(
    checks, monkeypatch, tmp_path, kind
):
    primary = OSError(errno.EIO, "injected read failure")
    release = OSError(errno.EIO, "injected stream release failure")
    result = _process_result(
        (
            _failure(primary, FailurePhase.READ, ProcessStream.STDOUT),
            _failure(release, FailurePhase.RELEASE, ProcessStream.STDOUT),
        ),
        returncode=1,
    )
    owner = _StubOwner(result)
    calls = _install_stub_launch(monkeypatch, checks, owner)

    outcome = _run_wrapper(checks, kind, tmp_path)

    assert outcome.clean is False
    assert outcome.failures == (primary, release)
    assert outcome.returncode == 1
    assert owner.collections == 1
    assert owner.exits == 1
    assert len(calls) == 1
    assert calls[0][0] == ("check-tool",)
    assert calls[0][1]["popen_kwargs"] == {"cwd": str(tmp_path)}


@pytest.mark.parametrize("kind", ["captured", "streaming"])
def test_process_wrapper_reports_cleanup_only_failure(
    checks, monkeypatch, tmp_path, kind
):
    forward = mock.Mock()
    release = OSError(errno.EIO, "injected stream release failure")
    owner = _StubOwner(
        _process_result((_failure(release, FailurePhase.RELEASE),), returncode=0)
    )
    _install_stub_launch(monkeypatch, checks, owner)
    monkeypatch.setattr(checks, "_forward", forward)

    outcome = _run_wrapper(checks, kind, tmp_path)

    assert outcome.clean is False
    assert outcome.returncode == 0
    assert outcome.failures == (release,)
    assert owner.exits == 1
    if kind == "streaming":
        assert owner.callback is forward


@pytest.mark.parametrize("kind", ["captured", "streaming"])
def test_process_wrapper_keeps_collection_interruption_identity(
    checks, monkeypatch, tmp_path, kind
):
    interruption = KeyboardInterrupt("injected collection interruption")
    owner = _StubOwner(_process_result(), collection_error=interruption)
    _install_stub_launch(monkeypatch, checks, owner)

    with pytest.raises(KeyboardInterrupt) as context:
        _run_wrapper(checks, kind, tmp_path)

    assert context.value is interruption
    assert owner.exits == 1
    assert owner.exit_primary is interruption


@pytest.mark.parametrize("kind", ["captured", "streaming"])
def test_process_wrapper_keeps_owner_cleanup_interruption_identity(
    checks, monkeypatch, tmp_path, kind
):
    interruption = KeyboardInterrupt("injected owner cleanup interruption")
    owner = _StubOwner(_process_result(), exit_error=interruption)
    _install_stub_launch(monkeypatch, checks, owner)

    with pytest.raises(KeyboardInterrupt) as context:
        _run_wrapper(checks, kind, tmp_path)

    assert context.value is interruption
    assert owner.exits == 1


@pytest.mark.parametrize("kind", ["captured", "streaming"])
def test_process_wrapper_never_reports_incomplete_cleanup_as_clean(
    checks, monkeypatch, tmp_path, kind
):
    owner = _StubOwner(_process_result(complete=False))
    _install_stub_launch(monkeypatch, checks, owner)

    outcome = _run_wrapper(checks, kind, tmp_path)

    assert outcome.clean is False
    assert outcome.failures == ()
    assert owner.exits == 1
    assert owner.cleanup_status.complete is False
    observed = owner.wait_cleanup(1)
    assert observed.complete is True
    assert outcome.clean is False


@pytest.mark.parametrize("kind", ["captured", "streaming"])
def test_real_owner_cleanup_failure_is_not_clean_and_repeats_no_release(
    checks, monkeypatch, tmp_path, kind
):
    real_launch = OwnedProcess.launch
    owners = []

    class Recording:
        @classmethod
        def launch(cls, *args, **kwargs):
            owner = real_launch(*args, **kwargs)
            owners.append(owner)
            return owner

    attempts: list[str] = []
    real_close = OwnedStream.close

    def failing(owner):
        attempts.append(owner.label)
        real_close(owner)
        raise OSError(errno.EIO, "injected stream release failure")

    monkeypatch.setattr(checks, "OwnedProcess", Recording)
    monkeypatch.setattr(OwnedStream, "close", failing)

    outcome = _run_wrapper(checks, kind, tmp_path, (sys.executable, "-c", "pass"))

    assert outcome.clean is False
    assert outcome.returncode == 0
    assert len(outcome.failures) == 2
    assert all(isinstance(error, OSError) for error in outcome.failures)
    assert len(owners) == 1
    owner = owners[0]
    assert owner.cleanup_status.complete is True
    assert owner.wait_cleanup(0.5).complete is True
    settled = len(attempts)
    assert settled == 2
    owner.shutdown()
    owner.shutdown()
    assert len(attempts) == settled
    assert owner.cleanup_status.complete is True


def test_streaming_callback_interruption_survives_cleanup_failure(
    checks, monkeypatch, tmp_path
):
    interruption = KeyboardInterrupt("injected streaming callback interruption")
    attempts: list[str] = []
    real_close = OwnedStream.close

    def interrupt(stream, chunk):
        raise interruption

    def failing(owner):
        attempts.append(owner.label)
        real_close(owner)
        raise OSError(errno.EIO, "injected stream release failure")

    monkeypatch.setattr(checks, "_forward", interrupt)
    monkeypatch.setattr(OwnedStream, "close", failing)

    with pytest.raises(KeyboardInterrupt) as context:
        checks.run_streaming([sys.executable, "-c", "print('ok')"], root=tmp_path)

    assert context.value is interruption
    assert len(attempts) == 2
