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


# A bounded textual fallback keeps secondary diagnostics observable when the
# authoritative exception cannot carry an attribute (for example a subclass
# with ``__slots__``).  Each note is truncated, and the total number of
# secondary-cleanup notes an exception may carry is capped **across repeated**
# ``attach_secondary`` calls (not separately per call), so a long-lived primary
# never accumulates unbounded context.
_SECONDARY_SLOT = "_transaction_secondary"
_MAX_NOTE_CHARS = 240
_MAX_SECONDARY_NOTES = 32


def _note_text(exc: BaseException) -> str:
    try:
        rendered = f"secondary cleanup failure: {type(exc).__name__}: {exc}"
    except Exception:  # pragma: no cover - defensive: broken __str__
        rendered = f"secondary cleanup failure: {type(exc).__name__}"
    if len(rendered) > _MAX_NOTE_CHARS:
        rendered = rendered[: _MAX_NOTE_CHARS - 3] + "..."
    return rendered


def _add_secondary_notes(primary: BaseException, secondary: list[BaseException]) -> None:
    add_note = getattr(primary, "add_note", None)
    if not callable(add_note):  # pragma: no cover - pre-3.11 interpreter
        return
    # The cap is a total per-primary budget: existing notes (including unrelated
    # ones) are neither removed nor overwritten, and count toward the limit so
    # repeated attachments cannot grow ``__notes__`` without bound.
    existing = getattr(primary, "__notes__", None)
    try:
        existing_count = len(existing) if existing is not None else 0
    except TypeError:  # pragma: no cover - exotic non-sized __notes__
        existing_count = 0
    remaining = _MAX_SECONDARY_NOTES - existing_count
    if remaining <= 0:
        return
    for exc in secondary[:remaining]:
        try:
            add_note(_note_text(exc))
        except Exception:  # pragma: no cover - exotic exception type
            return


def _retained_secondary(source: BaseException) -> list[BaseException]:
    """Return the secondary diagnostics *source* already retains.

    Both storage forms are read: a :class:`TransactionError` keeps its
    diagnostics on ``secondary``, while every other exception uses the
    generic private slot.  A plain exception with neither form has none.
    """
    if isinstance(source, TransactionError):
        return list(source.secondary)
    existing = getattr(source, _SECONDARY_SLOT, None)
    if isinstance(existing, list):
        return list(existing)
    return []


def carry_secondary_diagnostics(target: BaseException, source: BaseException) -> None:
    """Carry *source*'s retained secondary diagnostics onto *target*.

    This is the narrow, intention-revealing operation for a domain adapter
    that replaces a transaction/capability wrapper with its raw operational
    cause: ``carry_secondary_diagnostics(cause, wrapper)``.  It accepts no
    caller-supplied iterable and does not consume *source*: it copies only the
    diagnostics the wrapper already retained in source order.

    Attachment storage stays centralized in :func:`attach_secondary`, so an
    object-capable *target* retains the secondary exception objects and
    de-duplicates them by identity across repeated carries, while a target
    that cannot carry attributes receives only bounded textual notes without
    an identity-de-duplication guarantee.  The operation never carries
    *source* itself, ``__cause__``, ``__context__``, or unrelated notes, and
    it preserves *target* identity.
    """
    if target is source:
        return
    retained = [
        exc
        for exc in _retained_secondary(source)
        if exc is not target and exc is not source and exc is not None
    ]
    if not retained:
        return
    attach_secondary(target, retained)


def _append_unique(existing: list[BaseException], exc: BaseException) -> None:
    if any(item is exc for item in existing):
        return
    existing.append(exc)


def attach_secondary(primary: BaseException, secondary: list[BaseException]) -> None:
    """Attach cleanup/close diagnostics without replacing *primary*.

    Exception objects are retained without duplicate identity.  When the
    authoritative exception cannot carry an attribute, bounded
    :meth:`BaseException.add_note` text keeps the diagnostics observable
    without changing the authoritative exception's identity.
    """
    pending = [exc for exc in secondary if exc is not primary and exc is not None]
    if not pending:
        return
    if isinstance(primary, TransactionError):
        for exc in pending:
            primary.add_secondary(exc)
        return
    existing = getattr(primary, _SECONDARY_SLOT, None)
    if isinstance(existing, list):
        for exc in pending:
            _append_unique(existing, exc)
        return
    collected: list[BaseException] = []
    for exc in pending:
        _append_unique(collected, exc)
    try:
        setattr(primary, _SECONDARY_SLOT, collected)
    except (AttributeError, TypeError):
        _add_secondary_notes(primary, collected)
