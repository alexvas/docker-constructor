"""Structured error hierarchy for the locked npm environment assembler.

Every validation failure carries a machine-readable *reason* so callers can
map violations to diagnostics without parsing message strings.  Reasons are
stable identifiers; the human-readable *detail* is free-form.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

#: Exception-machinery attributes that must remain writable on raised
#: instances: traceback handling, ``BaseException.add_note`` (``__notes__``),
#: and cause/context chaining.
_EXC_INTERNAL_ATTRS = frozenset(
    {
        "__notes__",
        "__traceback__",
        "__cause__",
        "__context__",
        "__suppress_context__",
    }
)


class LockedNpmError(Exception):
    """A lock, root, identity, or placement contract violation.

    ``reason`` and ``detail`` are immutable value fields; only the
    exception-machinery dunder attributes may be assigned after
    construction, so traceback handling and :meth:`BaseException.add_note`
    work while the value fields stay frozen.
    """

    reason: str
    """Machine-readable reason code (e.g. ``"root_drift"``,
    ``"unsatisfied_range"``, ``"unsafe_path"``)."""

    detail: str
    """Human-readable detail suitable for diagnostics."""

    def __init__(
        self,
        reason: str,
        detail: str,
        *,
        summary: str | None = None,
        diagnostic_tail: str = "",
        diagnostic_stream: str | None = None,
    ) -> None:
        super().__init__(detail)
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "detail", detail)
        object.__setattr__(
            self,
            "summary",
            summary or (
                "locked npm assembly timed out"
                if reason == "assembly_timeout"
                else "locked npm assembly failed"
            ),
        )
        object.__setattr__(self, "diagnostic_tail", diagnostic_tail)
        object.__setattr__(self, "diagnostic_stream", diagnostic_stream)

    def __setattr__(self, name: str, value: object) -> None:
        if name in _EXC_INTERNAL_ATTRS:
            object.__setattr__(self, name, value)
            return
        raise FrozenInstanceError(f"cannot assign to field {name!r}")

    def __str__(self) -> str:
        return self.detail


class AssemblyTimeoutError(LockedNpmError):
    """Structured timeout failure from the constructor-owned total deadline.

    Raised by the streaming executor when the reviewed total assembly
    duration expires; ``reason`` is the stable ``"assembly_timeout"``
    identifier and the human-readable *detail* includes any bounded redacted
    assembler diagnostics.
    """

    def __init__(
        self,
        detail: str,
        *,
        summary: str | None = None,
        diagnostic_tail: str = "",
        diagnostic_stream: str | None = None,
    ) -> None:
        super().__init__(
            "assembly_timeout",
            detail,
            summary=summary,
            diagnostic_tail=diagnostic_tail,
            diagnostic_stream=diagnostic_stream,
        )
