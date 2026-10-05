"""Internal primary-preserving cleanup-failure accumulator.

This module standardizes only failure precedence and diagnostic preservation
for independent, exactly-once resource-release actions.  It derives no paths,
removes no entries or trees, retries no actions, defines no rollback, maps no
domain errors, and decides no idempotent absence.  Those remain the caller's
authority.

The accumulator deliberately lives outside the L0-L2 contract surface and is
not re-exported from :mod:`docker.transactions`: a consumer imports it
directly and supplies its own actions, ordinary-failure policy, and final
domain mapping.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from .errors import attach_secondary

_ActionT = TypeVar("_ActionT")


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
