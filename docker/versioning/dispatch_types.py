"""Shared dispatch types used by both the facade and internal services.

These types live in their own module to avoid circular imports between
``docker.docker-constructor`` (the facade) and
``docker.versioning.readonly_service`` (the domain service).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ExitKind(Enum):
    SUCCESS = "success"
    POLICY = "policy"
    CLI = "cli"
    CONFIG = "config"
    OPERATIONAL = "operational"


@dataclass(frozen=True)
class CommandResult:
    """Structured result produced by a domain dispatcher.

    The facade renders this generically — it does not inspect
    ``data`` or ``message`` beyond formatting.
    """

    exit_kind: ExitKind
    data: object | None = None
    message: str | None = None
    debug: str | None = None
    message_owned_by_presentation: bool = False
    """Whether a live actor owns final text output for ``message``.

    The message remains available for structured consumers and JSON; generic
    text rendering suppresses it to avoid duplicate output or fallback retry.
    """
