"""Shared support for Phase 6 descriptor-ownership lifecycle tests.

Phase 6 migrates the remaining consumer-owned descriptor releases to the
shared ``OwnedDescriptor`` lifecycle.  A migrated release must satisfy three
executable properties that a raw ``os.close`` cannot demonstrate:

* **terminal-before-close** — ownership is marked terminal *before* the close
  syscall is issued, so an ambiguous close failure is never retried;
* **at-most-once release** — a repeated cleanup attempt issues no second
  syscall; and
* **operation-primary precedence** — an active operation failure stays
  authoritative while an ordinary close failure is attached as secondary.

:class:`OwnershipRecorder` patches a production module's ``OwnedDescriptor``
symbol with a recording subclass so a test can observe those properties on the
owners the module actually constructs.  It wraps the production-supplied
descriptor backend so the *real* close primitive is still exercised, while the
recorder captures the owner's released-state at close time and can inject
ordinary close failures.
"""
from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator, Sequence
from typing import Any
from unittest import mock

from docker.filesystem.descriptors import OwnedDescriptor


@contextlib.contextmanager
def recording(
    module: Any,
    *,
    close_faults: Sequence[BaseException | None] = (),
    close_fault: Callable[[int, OwnedDescriptor], BaseException | None] | None = None,
):
    """Patch *module*'s ``OwnedDescriptor`` and yield an :class:`OwnershipRecorder`.

    ``close_faults`` is consumed in close order: a ``None`` entry lets the real
    close proceed while an exception instance is raised as the close outcome.
    ``close_fault`` is consulted for every close and can select a fault by fd or
    owner, which is useful for multi-descriptor functions.
    """
    recorder = OwnershipRecorder(close_faults, close_fault)
    with recorder.install(module):
        yield recorder


class OwnershipRecorder:
    """Record shared-owner construction and release attempts for one module."""

    def __init__(
        self,
        close_faults: Sequence[BaseException | None] = (),
        close_fault: Callable[[int, OwnedDescriptor], BaseException | None] | None = None,
    ) -> None:
        self.owners: list[OwnedDescriptor] = []
        # ``(fd, released_before_close, owner)`` in syscall order.  The owner
        # identity matters because the OS reuses fd numbers after a close.
        self.close_calls: list[tuple[int, bool, OwnedDescriptor]] = []
        self.faults: list[BaseException | None] = list(close_faults)
        self.close_fault = close_fault

    # -- queries ----------------------------------------------------------
    @property
    def released_before_close(self) -> list[bool]:
        return [released for _, released, _ in self.close_calls]

    def fds(self) -> list[int]:
        return [owner._fd for owner in self.owners]

    def owner_for(self, fd: int) -> OwnedDescriptor:
        for owner in self.owners:
            if owner._fd == fd:
                return owner
        raise AssertionError(f"no recorded owner for fd {fd}")

    def assert_terminal_and_at_most_once(self, case: Any) -> None:
        """Assert every recorded owner closed terminally and exactly once."""
        case.assertTrue(
            self.close_calls, "migration must release through a shared owner"
        )
        case.assertTrue(
            all(self.released_before_close),
            f"close ran before ownership was terminal: {self.close_calls!r}",
        )
        counts: dict[int, int] = {}
        for _, _, owner in self.close_calls:
            counts[id(owner)] = counts.get(id(owner), 0) + 1
        case.assertTrue(
            all(count == 1 for count in counts.values()),
            f"an owner received more than one close attempt: {counts!r}",
        )

    # -- installation -----------------------------------------------------
    def _wrap_ops(self, ops: Any, owner: OwnedDescriptor):
        recorder = self

        class _RecordingOps:
            def close(self, fd: int) -> None:
                recorder.close_calls.append((fd, owner.released, owner))
                fault: BaseException | None = None
                if recorder.faults:
                    fault = recorder.faults.pop(0)
                if fault is None and recorder.close_fault is not None:
                    fault = recorder.close_fault(fd, owner)
                if fault is not None:
                    raise fault
                ops.close(fd)

            def __getattr__(self, name: str) -> Any:
                return getattr(ops, name)

        return _RecordingOps()

    @contextlib.contextmanager
    def install(self, module: Any) -> Iterator[OwnershipRecorder]:
        recorder = self
        # Pre-migration modules need not import the shared owner at all; fall
        # back to the real class so the patch still installs and the test's
        # ownership assertions fail (rather than erroring on a missing name).
        real_owner = getattr(module, "OwnedDescriptor", OwnedDescriptor)

        class _RecordingOwner(real_owner):  # type: ignore[misc, valid-type]
            def __init__(self, ops: Any, fd: int, *, label: str) -> None:
                super().__init__(recorder._wrap_ops(ops, self), fd, label=label)
                recorder.owners.append(self)

        with mock.patch.object(module, "OwnedDescriptor", _RecordingOwner, create=True):
            yield recorder


class FileObjectFaults:
    """Proxy a real file object to inject ``read``/``write``/``close`` faults.

    The wrapped object is the genuine file descriptor wrapper, so successful
    operations still reach the descriptor while a test can make one file-object
    operation report a failure.  ``close`` still closes the Python file object
    (which, for ``closefd=False``, never touches the descriptor).
    """

    __slots__ = ("_real", "_read_fault", "_write_fault", "_flush_fault", "_close_fault")

    def __init__(
        self,
        real: Any,
        *,
        read_fault: BaseException | None = None,
        write_fault: BaseException | None = None,
        flush_fault: BaseException | None = None,
        close_fault: BaseException | None = None,
    ) -> None:
        self._real = real
        self._read_fault = read_fault
        self._write_fault = write_fault
        self._flush_fault = flush_fault
        self._close_fault = close_fault

    def read(self, *args: Any) -> Any:
        if self._read_fault is not None:
            raise self._read_fault
        return self._real.read(*args)

    def write(self, data: Any) -> Any:
        if self._write_fault is not None:
            raise self._write_fault
        return self._real.write(data)

    def flush(self) -> None:
        if self._flush_fault is not None:
            raise self._flush_fault
        self._real.flush()

    def close(self) -> None:
        result = self._real.close()
        if self._close_fault is not None:
            raise self._close_fault
        return result

    def fileno(self) -> int:
        return self._real.fileno()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


@contextlib.contextmanager
def file_object_faults(
    module: Any,
    *,
    read_fault: BaseException | None = None,
    write_fault: BaseException | None = None,
    flush_fault: BaseException | None = None,
    close_fault: BaseException | None = None,
):
    """Patch *module*'s ``os.fdopen`` to wrap the file object with faults.

    This makes file-object cleanup observable independently of the descriptor
    release, so a test can prove which failure stays primary when both fail.
    """
    real_fdopen = module.os.fdopen

    def faulty_fdopen(fd: int, *args: Any, **kwargs: Any) -> FileObjectFaults:
        return FileObjectFaults(
            real_fdopen(fd, *args, **kwargs),
            read_fault=read_fault,
            write_fault=write_fault,
            flush_fault=flush_fault,
            close_fault=close_fault,
        )

    with mock.patch.object(module.os, "fdopen", side_effect=faulty_fdopen):
        yield


def secondary_diagnostics(exc: BaseException) -> list[BaseException]:
    """Return the ordinary secondary cleanup diagnostics retained by *exc*."""
    return list(getattr(exc, "_transaction_secondary", []))
