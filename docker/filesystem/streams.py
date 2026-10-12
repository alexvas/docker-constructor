"""Bounded, domain-neutral ownership for one already-open fd-owning stream.

``OwnedStream`` adopts one file object that already owns its operating-system
descriptor (for example ``subprocess.Popen`` stdout/stderr pipes) and becomes
its sole release authority.  It never opens, reads, writes, drains, proxies, or
reconstructs path/process policy, and it exposes no integer-descriptor accessor:
the wrapped stream is borrowed through :attr:`OwnedStream.stream` and only the
owner may release it.

Release is a synchronized, irreversible at-most-once attempt.  The live to
release-attempted claim is made under a private lock and the underlying close
runs *outside* that lock, so a reentrant or concurrent caller never issues a
second close and never deadlocks on a close that reenters the owner.  A losing
caller does not wait for the winning close and must not treat the terminal
claim as proof that the winning close completed.  The module deliberately
depends only on the standard library and the foundation cleanup primitives so
it stays below every domain package.
"""
from __future__ import annotations

import threading
from typing import Any, Self

from docker.filesystem.cleanup import CleanupFailures

_LIVE = "live"
_RELEASED = "released"


class OwnedStream:
    """Exclusively own one already-open fd-owning stream until release."""

    __slots__ = ("_stream", "_label", "_state", "_lock")

    def __init__(self, stream: Any, *, label: str) -> None:
        # Validate and prepare before transfer. Rejected candidates remain
        # caller-owned; constructor failure never grants release authority.
        if not isinstance(label, str) or not label:
            raise ValueError(
                f"stream label must be a non-empty string, not {label!r}"
            )
        if not callable(getattr(stream, "close", None)):
            raise TypeError(
                f"owned stream must expose a callable close(), not {stream!r}"
            )
        lock = threading.Lock()
        self._lock = lock
        self._label = label
        self._state = _LIVE
        self._stream = stream

    @property
    def stream(self) -> Any:
        """Return the borrowed live stream; grants no release authority."""
        if self._state is not _LIVE:
            raise ValueError(f"stream {self._label!r} is not live")
        return self._stream

    @property
    def label(self) -> str:
        return self._label

    @property
    def released(self) -> bool:
        return self._state is _RELEASED

    def close(self) -> None:
        # Claim the release before invoking the underlying close: the state
        # means "close attempted", not "close definitely succeeded".  POSIX
        # does not guarantee a failed pipe close kept the descriptor open, so
        # a retry could release a reused descriptor.  The underlying close is
        # invoked outside the lock so a close that reenters this owner (or a
        # concurrent caller) cannot deadlock or issue a second close.
        with self._lock:
            if self._state is not _LIVE:
                return
            self._state = _RELEASED
            stream = self._stream
        stream.close()

    def __enter__(self) -> Self:
        _ = self.stream
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
        # No active primary: a close failure is the only failure and
        # propagates.
        self.close()


def _prepare_stream(*, label: str) -> OwnedStream:
    """Prepare a private empty slot before a resource factory is invoked."""
    if not isinstance(label, str) or not label:
        raise ValueError("stream label must be a non-empty string")
    lock = threading.Lock()
    owner = object.__new__(OwnedStream)
    owner._lock = lock
    owner._label = label
    owner._state = _RELEASED
    owner._stream = None
    return owner


def _bind_prepared(owner: OwnedStream, stream: Any) -> None:
    """Account for a factory result using its already-prepared authority.

    Private escrow binding precedes fallible interface/setup validation. This
    is not public rejected adoption: the launching coordinator already owns
    every factory result, including a result whose later setup fails.
    """
    owner._stream = stream
    owner._state = _LIVE


__all__ = ["OwnedStream"]
