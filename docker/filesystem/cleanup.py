"""Domain-neutral, primary-preserving cleanup-failure accumulator.

This lightweight module standardizes only failure precedence and diagnostic
preservation for independent, exactly-once resource-release actions.  It
derives no paths, removes no entries or trees, retries no actions, defines no
rollback, maps no domain errors, and decides no idempotent absence.  Those
remain the caller's authority.

The module deliberately depends on the standard library alone so it can be
reused across otherwise independent Constructor subsystems without loading the
aggregate durable-transaction substrate.  The diagnostic helpers recognize a
primary or source that exposes the generic ``add_secondary``/``secondary``
protocol by duck typing, so a domain exception hierarchy keeps its structured
storage while every other exception uses a bounded private slot or notes.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

_ActionT = TypeVar("_ActionT")

# A bounded textual fallback keeps secondary diagnostics observable when the
# authoritative exception cannot carry an attribute (for example a frozen
# domain exception or a subclass with ``__slots__``).  Each note is truncated,
# and the total number of secondary-cleanup notes an exception may carry is
# capped **across repeated** ``attach_secondary`` calls (not separately per
# call), so a long-lived primary never accumulates unbounded context.
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

    A source that follows the generic ``add_secondary``/``secondary`` protocol
    keeps its diagnostics on ``secondary``, while every other exception uses
    the generic private slot.  A plain exception with neither form has none.
    """
    secondary = getattr(source, "secondary", None)
    if isinstance(secondary, list) and callable(getattr(source, "add_secondary", None)):
        return list(secondary)
    existing = getattr(source, _SECONDARY_SLOT, None)
    if isinstance(existing, list):
        return list(existing)
    return []


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
    add_secondary = getattr(primary, "add_secondary", None)
    if callable(add_secondary):
        for exc in pending:
            add_secondary(exc)
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


def carry_secondary_diagnostics(
    target: BaseException,
    source: BaseException,
) -> None:
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


class CleanupFailures:
    """Accumulate independent cleanup outcomes with deterministic precedence.

    Construction classifies an original primary that is not an ``Exception``
    as the first authoritative process-control interruption, so a caller's
    ``KeyboardInterrupt``/cancellation stays primary over every later cleanup
    defect or interruption.  An ``Exception`` primary is retained as the
    displaced original and can be replaced only by a higher-precedence
    interruption or unexpected defect.

    :meth:`run` invokes each action exactly once and never re-raises an action
    failure, so every later independent action is still attempted.
    :meth:`complete` is the sole terminal operation.
    """

    __slots__ = (
        "_original_primary",
        "_ordinary_failures",
        "_unexpected",
        "_interruptions",
        "_completed",
    )

    def __init__(self, primary: BaseException | None) -> None:
        self._ordinary_failures: list[Exception] = []
        self._unexpected: list[Exception] = []
        self._interruptions: list[BaseException] = []
        self._completed = False
        if primary is None:
            self._original_primary: BaseException | None = None
        elif isinstance(primary, Exception):
            self._original_primary: BaseException | None = primary
        else:
            # A process-control primary (``KeyboardInterrupt``, cancellation,
            # ``SystemExit``, ...) is the first authoritative interruption and
            # precedes every interruption raised later during cleanup.
            self._original_primary = None
            self._interruptions.append(primary)

    # -- inspection (diagnostic only; grants no authority) ----------------
    @property
    def original_primary(self) -> BaseException | None:
        return self._original_primary

    @property
    def ordinary_failures(self) -> tuple[Exception, ...]:
        return tuple(self._ordinary_failures)

    @property
    def unexpected(self) -> tuple[Exception, ...]:
        return tuple(self._unexpected)

    @property
    def interruptions(self) -> tuple[BaseException, ...]:
        return tuple(self._interruptions)

    # -- actions ----------------------------------------------------------
    def run(
        self,
        action: Callable[[], _ActionT],
        *,
        ordinary: tuple[type[Exception], ...],
    ) -> None:
        """Attempt *action* exactly once, capturing any failure.

        ``ordinary`` is mandatory and keyword-only.  An exception matching it
        is recorded as an ordinary cleanup failure; another ``Exception`` is an
        unexpected cleanup defect; a non-``Exception`` ``BaseException`` is a
        process-control interruption.  The action's return value is discarded
        and grants no authority.
        """
        if self._completed:
            raise RuntimeError("cleanup accumulator is already complete")
        policy = _validate_ordinary(ordinary)
        if not callable(action):
            raise TypeError("cleanup action must be callable")
        try:
            action()
        except BaseException as exc:  # noqa: BLE001 - classification is the point
            if not isinstance(exc, Exception):
                self._interruptions.append(exc)
            elif isinstance(exc, policy):
                self._ordinary_failures.append(exc)
            else:
                self._unexpected.append(exc)

    # -- terminal ---------------------------------------------------------
    def complete(self) -> Exception | None:
        """Apply precedence and return the caller-owned ordinary failure.

        Process-control interruptions and unexpected cleanup defects are raised
        unchanged (first in action order).  An original ``Exception`` primary
        is preserved and ``None`` is returned so the caller's existing
        propagation stays authoritative.  Otherwise the first ordinary cleanup
        failure is returned for caller-owned mapping, or ``None`` when nothing
        failed.
        """
        if self._completed:
            raise RuntimeError("cleanup accumulator is already complete")
        self._completed = True

        if self._interruptions:
            primary = self._interruptions[0]
            secondary: list[BaseException] = list(self._interruptions[1:])
            secondary.extend(self._unexpected)
            if self._original_primary is not None:
                secondary.append(self._original_primary)
            secondary.extend(self._ordinary_failures)
            attach_secondary(primary, secondary)
            raise primary

        if self._unexpected:
            primary = self._unexpected[0]
            unexpected_secondary: list[BaseException] = list(self._unexpected[1:])
            if self._original_primary is not None:
                unexpected_secondary.append(self._original_primary)
            unexpected_secondary.extend(self._ordinary_failures)
            attach_secondary(primary, unexpected_secondary)
            raise primary

        if self._original_primary is not None:
            attach_secondary(self._original_primary, list(self._ordinary_failures))
            return None

        if self._ordinary_failures:
            first = self._ordinary_failures[0]
            attach_secondary(first, list(self._ordinary_failures[1:]))
            return first

        return None


def _validate_ordinary(
    ordinary: tuple[type[Exception], ...],
) -> tuple[type[Exception], ...]:
    if not isinstance(ordinary, tuple):
        raise TypeError("ordinary must be a tuple of Exception types")
    for entry in ordinary:
        if not isinstance(entry, type) or not issubclass(entry, Exception):
            raise TypeError("ordinary must contain only Exception subclasses")
    return ordinary
