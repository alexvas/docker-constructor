"""Neutral locked-assembly operational-activity boundary.

The standalone ``npm_environment`` package owns execution semantics but must
not depend on the host presentation/event layer.  This module defines the tiny
structural contract the orchestrator calls at each closed assembly boundary;
the versioning layer supplies the concrete implementation and receives Phase 1
operational events.

Step names are exactly the closed
:class:`~docker.versioning.host_progress.HostStep` values so the implementation
can validate them against one authoritative enum without importing it here.
"""
from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Protocol


class AssemblyActivity(Protocol):
    """Structural contract for locked-assembly operational instrumentation."""

    def step(
        self, name: str, *, container_name: str | None = None
    ) -> AbstractContextManager[None]:
        """Scope one named assembly step; emit start and terminal facts."""
        ...

    def cache_reuse(self) -> None:
        """Report a verified cache hit with no container or npm activity."""
        ...


class _NullStep:
    def __enter__(self) -> "_NullStep":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


class NullAssemblyActivity:
    """No-op activity used when no event sink/observation is requested."""

    def step(
        self, name: str, *, container_name: str | None = None
    ) -> _NullStep:
        return _NullStep()

    def cache_reuse(self) -> None:
        return None


#: Shared stateless no-op instance.
NULL_ACTIVITY = NullAssemblyActivity()


__all__ = ["AssemblyActivity", "NULL_ACTIVITY", "NullAssemblyActivity"]
