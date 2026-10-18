"""Provider base protocol and transport interfaces.

No Docker, no network, no subprocess.  Provider adapters receive injected
transports so tests can supply deterministic fake responses without needing
real network access.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import (
    Mapping,
    Optional,
    Protocol,
    Sequence,
    runtime_checkable,
)

from ..model import (
    CandidateArtifact,
    UpdateCandidate,
    UpdateTarget,
)

# ---------------------------------------------------------------------------
# Transport protocols
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes

    def __post_init__(self):
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))


class HttpTransport(Protocol):
    """Inject HTTP requests through this interface.

    Provider adapters call `.request()`; production code provides a real
    implementation (based on urllib), tests provide deterministic fakes.
    """

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] = MappingProxyType({}),
        nocache: bool = False,
    ) -> HttpResponse: ...


class GitRefTransport(Protocol):
    """Resolve a git ref to a commit hash.

    Production code runs ``git ls-remote``; tests provide fakes.
    """

    def resolve_ref(self, repository: str, ref: str) -> str: ...


# ---------------------------------------------------------------------------
# Provider context
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderContext:
    """Context passed to every provider adapter when discovering updates.

    Tokens are injected from environment variables (GITHUB_TOKEN, etc.)
    only during explicit ``check-updates`` execution.
    """

    http: HttpTransport
    git: GitRefTransport
    include_prerelease: bool
    tokens: Mapping[str, str]

    def __post_init__(self):
        object.__setattr__(self, "tokens", MappingProxyType(dict(self.tokens)))


# ---------------------------------------------------------------------------
# Provider result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderResult:
    """Returned by every provider adapter's ``discover()`` method.

    Exactly one of ``candidate`` or ``unavailable_reason`` / ``skipped_reason``
    should be set (not both, not none without reason).
    """

    candidate: Optional[UpdateCandidate] = None
    unavailable_reason: Optional[str] = None
    skipped_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# UpdateProvider protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class UpdateProvider(Protocol):
    """Every provider adapter must implement this protocol."""

    name: str

    def discover(
        self,
        target: UpdateTarget,
        context: ProviderContext,
    ) -> ProviderResult: ...
