"""Dependency-neutral fetch request-count identity.

Phase 5 of ``add-configurable-network-url-display`` needs the presentation
actor to group recognized successful npm fetches without importing the npm
fetch parser (which depends on the URL projector, which in turn depends on the
presentation event model).  :class:`FetchGroupKey` therefore lives here, in a
low-level module whose only dependency is the presentation-only
:class:`~docker.versioning.model.NetworkUrlDisplay` enum.

:mod:`docker.versioning.npm_fetch` re-exports :class:`FetchGroupKey` so its
public identity API stays stable, and the internal presentation envelope in
:mod:`docker.versioning.host_progress` reuses this exact type.  Nothing here
parses, renders, or touches transport, Docker, or the filesystem.
"""
from __future__ import annotations

from dataclasses import dataclass

from docker.versioning.model import NetworkUrlDisplay


@dataclass(frozen=True, slots=True)
class FetchGroupKey:
    """Policy-specific request-count group identity for one recognized fetch.

    ``display`` keeps ``redacted`` and ``host-path`` identities from ever
    comparing equal.  ``host_path`` is the normalized hostname plus canonical
    safe path for ``host-path`` mode and ``None`` for ``redacted`` mode, where
    the URL is intentionally excluded from the key.
    """

    display: NetworkUrlDisplay
    method: str
    status: int
    attempt: int | None
    cache_outcome: str | None
    host_path: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.display, NetworkUrlDisplay):
            raise TypeError("display must be a NetworkUrlDisplay member")


__all__ = ["FetchGroupKey"]
