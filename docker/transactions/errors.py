"""Bounded L1/L2 transaction errors.

An L2 operation failure carries the failing ``stage`` and the original
``cause``.  The raw cause stays observable through ``__cause__`` so an
``OSError`` subclass and its ``errno`` are never lost.  Ordinary cleanup and
close failures are attached as secondary diagnostics and never replace a
primary failure, while process-control interruptions raised by cleanup
propagate unchanged.

The generic diagnostic helpers are owned by the lightweight foundation so
they can be reused without loading the aggregate transaction substrate.  They
are re-exported here unchanged for existing transaction callers.
"""
from __future__ import annotations

from docker.filesystem.cleanup import (
    _MAX_SECONDARY_NOTES,
    attach_secondary,
    carry_secondary_diagnostics,
)

STAGE_ALLOCATE = "allocate-temporary"
STAGE_MODE = "establish-mode"
STAGE_WRITE = "write"
STAGE_FSYNC_FILE = "fsync-file"
STAGE_COMMIT = "commit"
STAGE_FSYNC_DIRECTORY = "fsync-directory"
STAGE_VALIDATE_DESTINATION = "validate-destination"
STAGE_REPLACE = "replace"
STAGE_OPEN = "open-directory"
STAGE_VALIDATE = "validate"
STAGE_READ = "read"
STAGE_UNLINK = "unlink"
STAGE_CLOSE = "close"
STAGE_LOCK_PREPARE = "prepare-lock"
STAGE_LOCK_ACQUIRE = "acquire-lock"
STAGE_LOCK_STAT = "stat-lock"
STAGE_LOCK_VALIDATE = "validate-lock"
STAGE_LOCK_MODE = "repair-lock-mode"
STAGE_UNLOCK = "unlock"


class TransactionError(Exception):
    """A bounded L1/L2 operation failed at a named ``stage``."""

    def __init__(self, stage: str, message: str, *, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.stage = stage
        self.cause = cause
        self.secondary: list[BaseException] = []
        if cause is not None:
            self.__cause__ = cause

    def add_secondary(self, exc: BaseException) -> None:
        if exc is self:
            return
        if any(item is exc for item in self.secondary):
            return
        self.secondary.append(exc)


class UnsafeFileError(TransactionError):
    """A leaf is a symlink, non-regular, foreign-owned, multiplied, or forbidden-mode."""


class _CloseStageMeta(type):
    """Metaclass matching exactly the close-stage ``TransactionError`` instances.

    ``CleanupFailures.run`` classifies action failures by exception type, but
    L1 reports every operation stage through the single ``TransactionError``
    type.  Defining ``__instancecheck__`` on the metaclass lets
    :class:`CloseStageFailure` participate in ``ordinary`` type tuples while
    matching by stage, so a close accumulator can declare
    ``ordinary=(CloseStageFailure,)`` without demoting an unrelated read,
    validate, or open failure to an ordinary close diagnostic.
    """

    def __instancecheck__(cls, instance: object) -> bool:
        return isinstance(instance, TransactionError) and instance.stage == STAGE_CLOSE


class CloseStageFailure(TransactionError, metaclass=_CloseStageMeta):
    """Classification marker for close-stage transaction failures.

    This class is never raised.  It exists so a cleanup accumulator can pass
    ``ordinary=(CloseStageFailure,)`` and have **only** a
    ``TransactionError`` whose ``stage == STAGE_CLOSE`` treated as an ordinary
    close failure; an unrelated stage surfaced by a close action stays an
    authoritative unexpected defect rather than being suppressed as a close
    diagnostic.  Exception matching (``except``) does not consult
    ``__instancecheck__``, so this marker must only appear in an ``ordinary``
    tuple and never in an ``except`` clause.
    """


class DestinationExists(TransactionError):
    """Typed final-destination collision outcome; never a temporary collision."""

    def __init__(self, name: str, *, cause: BaseException | None = None) -> None:
        super().__init__(STAGE_COMMIT, f"destination already exists: {name}", cause=cause)
        self.name = name


class CapabilityError(ValueError):
    """A capability is released, malformed, or used outside its directory."""


class LockError(TransactionError):
    """A shared advisory-lock operation failed at a named ``stage``."""


class LockContention(LockError):
    """``FAIL_FAST`` acquisition found the lock namespace already held."""

    def __init__(self, message: str, *, cause: BaseException | None = None) -> None:
        super().__init__(STAGE_LOCK_ACQUIRE, message, cause=cause)
