"""Generic owned-descriptor lifecycle.

``OwnedDescriptor`` exclusively owns one live descriptor until it transfers
that ownership with :meth:`OwnedDescriptor.detach` or attempts release with
:meth:`OwnedDescriptor.close`.  Release is an irreversible at-most-once
attempt: the capability marks itself released before invoking the underlying
close, never retries, and issues no operation once released or transferred.

``DescriptorError`` and ``UnsafeDescriptorError`` are the bounded foundation
errors carrying a stage and an optional raw cause.  The module is
domain-neutral: it derives no paths, performs no recursion, and defines no
durability, locking, transaction, cache, or npm authority.  It depends only on
the standard library and the foundation's cleanup primitives.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Self

from docker.filesystem.cleanup import CleanupFailures

if TYPE_CHECKING:
    from docker.filesystem.operations import DescriptorOps

_STAGE_CONSTRUCTION = "construct-descriptor"
_STAGE_RELEASE = "release-descriptor"
_STAGE_TRANSFER = "transfer-descriptor"

_LIVE = "live"
_TRANSFERRED = "transferred"
_RELEASED = "released"


class DescriptorError(Exception):
    """A bounded descriptor operation or validation failure."""

    def __init__(
        self,
        stage: str,
        message: str,
        *,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.cause = cause
        if cause is not None:
            self.__cause__ = cause


class UnsafeDescriptorError(DescriptorError):
    """A descriptor is a symlink, non-directory, foreign-owned, or otherwise unsafe."""


class OwnedDescriptor:
    """Exclusively own one live descriptor until transfer or release."""

    __slots__ = ("_ops", "_fd", "_label", "_state")

    def __init__(self, ops: DescriptorOps, fd: int, *, label: str) -> None:
        if isinstance(fd, bool) or not isinstance(fd, int) or fd < 0:
            raise DescriptorError(
                _STAGE_CONSTRUCTION,
                f"descriptor fd must be a non-negative integer, not {fd!r}",
            )
        if not isinstance(label, str) or not label:
            raise DescriptorError(
                _STAGE_CONSTRUCTION,
                f"descriptor label must be a non-empty string, not {label!r}",
            )
        # A successful validation transfers ownership unconditionally.  No
        # filesystem validation happens here: this is the supported lifecycle
        # seam, and the directory capability performs its own checks.
        self._ops = ops
        self._fd = fd
        self._label = label
        self._state = _LIVE

    @property
    def fd(self) -> int:
        if self._state is not _LIVE:
            raise DescriptorError(
                _STAGE_RELEASE, f"descriptor {self._label!r} is not live"
            )
        return self._fd

    @property
    def label(self) -> str:
        return self._label

    @property
    def released(self) -> bool:
        return self._state is _RELEASED

    def close(self) -> None:
        # Mark release before invoking close: the transition means "close
        # attempted", not "close definitely succeeded".  POSIX does not
        # guarantee a failed close kept the descriptor open, so a retry could
        # close a reused descriptor; a second close is therefore a no-op that
        # issues no operation.
        if self._state is _LIVE:
            self._state = _RELEASED
            self._ops.close(self._fd)

    def detach(self) -> int:
        """Transfer the raw live descriptor to the caller without closing it."""
        if self._state is not _LIVE:
            raise DescriptorError(
                _STAGE_TRANSFER, f"descriptor {self._label!r} is not live"
            )
        self._state = _TRANSFERRED
        return self._fd

    def __enter__(self) -> Self:
        _ = self.fd
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc is not None:
            # Preserve the active failure: an ordinary close failure is
            # attached as secondary diagnostic context and returning normally
            # lets Python re-raise the original exception.  A process-control
            # interruption raised by close is authoritative and is not
            # converted.
            failures = CleanupFailures(exc)
            failures.run(self.close, ordinary=(OSError,))
            result = failures.complete()
            if result is not None:
                raise result
            return
        # No active exception: a close failure is the only failure and
        # propagates.
        self.close()
