"""Bounded L1/L2 transaction errors.

An L2 operation failure carries the failing ``stage`` and the original
``cause``.  The raw cause stays observable through ``__cause__`` so an
``OSError`` subclass and its ``errno`` are never lost.  Ordinary cleanup and
close failures are attached as secondary diagnostics and never replace a
primary failure, while process-control interruptions raised by cleanup
propagate unchanged.
"""
from __future__ import annotations

STAGE_ALLOCATE = "allocate-temporary"
STAGE_MODE = "establish-mode"
STAGE_WRITE = "write"
STAGE_FSYNC_FILE = "fsync-file"
STAGE_COMMIT = "commit"
STAGE_FSYNC_DIRECTORY = "fsync-directory"
STAGE_VALIDATE_DESTINATION = "validate-destination"
STAGE_REPLACE = "replace"
STAGE_VALIDATE = "validate"
STAGE_READ = "read"
STAGE_UNLINK = "unlink"
STAGE_CLOSE = "close"
STAGE_LOCK_PREPARE = "prepare-lock"
STAGE_LOCK_ACQUIRE = "acquire-lock"
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
        self.secondary.append(exc)


class UnsafeFileError(TransactionError):
    """A leaf is a symlink, non-regular, foreign-owned, multiplied, or forbidden-mode."""


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


def attach_secondary(primary: BaseException, secondary: list[BaseException]) -> None:
    """Attach cleanup/close diagnostics without replacing *primary*."""
    if not secondary:
        return
    if isinstance(primary, TransactionError):
        for exc in secondary:
            primary.add_secondary(exc)
        return
    existing = getattr(primary, "_transaction_secondary", None)
    if existing is None:
        existing = []
        try:
            object.__setattr__(primary, "_transaction_secondary", existing)
        except (AttributeError, TypeError):
            return
    existing.extend(secondary)
