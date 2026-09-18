"""Transport construction for provider update checks.

Provides a single ``build_transports()`` entry point that constructs
the production HTTP/Git transports and collects auth tokens from the
environment.  Both the legacy CLI and the read-only service consume
this API — domain services never depend on CLI parsing modules.
"""

from __future__ import annotations

import os as _os
from pathlib import Path
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Mapping

from .immutable import deep_freeze

if TYPE_CHECKING:
    from .providers.base import HttpTransport, GitRefTransport


@dataclass(frozen=True)
class TransportConfig:
    """Resolved transport/fetch configuration for provider checks.

    Carries the constructed HTTP and Git transports plus any
    environment-sourced auth tokens — everything needed to construct
    a ``ProviderContext`` for ``check_updates()``.
    """

    http: HttpTransport
    """Production HTTP client (optionally caching)."""

    git: GitRefTransport
    """Production Git-ref transport."""

    tokens: Mapping[str, str] = field(default_factory=dict)
    """Auth tokens harvested from environment variables.

    Stored as an immutable ``MappingProxyType`` — callers cannot
    mutate the token map after construction.
    """

    def __post_init__(self) -> None:
        object.__setattr__(self, "tokens", deep_freeze(self.tokens))


def build_transports(
    *,
    no_cache: bool = False,
    inventory_cache: object | None = None,
    local_cache: object | None = None,
    suggest_mode: bool = False,
) -> TransportConfig:
    """Construct production HTTP/Git transports and collect auth tokens.

    Parameters
    ----------
    no_cache:
        When ``True``, skip the caching layer entirely — every request
        hits the network.
    inventory_cache:
        Optional ``CacheConfig`` from the validated inventory
        (``[cache]`` section of ``docker-constructor.toml``).
    local_cache:
        Optional ``LocalCacheConfig`` cache slice of the shared local
        aggregate result (``[cache]`` section of the host-only companion).
        Contains only the untrusted configured ``dir`` string; cache-storage
        owns resolution, normalization, and safety.
    suggest_mode:
        When ``True``, disk cache is **never** created or written to.
        An in-memory-only caching layer is used instead so that
        ``--suggest`` is a purely non-mutating operation.

    Returns
    -------
    TransportConfig
        Ready-to-use transports and auth tokens.
    """
    from .providers.base import HttpTransport, GitRefTransport

    class _ProductionHttp(HttpTransport):
        def request(self, method, url, *, headers=(), nocache=False):
            import urllib.request
            import urllib.error
            req = urllib.request.Request(
                url, method=method, headers=dict(headers),
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    body = resp.read()
                    return type("HttpResponse", (), {
                        "status": resp.status,
                        "headers": dict(resp.headers),
                        "body": body,
                    })()
            except urllib.error.HTTPError as e:
                body = e.read() if hasattr(e, "read") else b""
                return type("HttpResponse", (), {
                    "status": e.code,
                    "headers": dict(e.headers)
                    if hasattr(e, "headers") else {},
                    "body": body,
                })()
            except OSError as e:
                return type("HttpResponse", (), {
                    "status": 503,
                    "headers": {},
                    "body": f"timeout_or_connection_error: {e}".encode(),
                })()

    class _ProductionGit(GitRefTransport):
        def resolve_ref(self, repository, ref):
            import subprocess
            try:
                result = subprocess.run(
                    ["git", "ls-remote", repository, ref],
                    capture_output=True, text=True, timeout=30,
                )
                if result.returncode != 0:
                    raise RuntimeError(result.stderr.strip())
                line = result.stdout.strip().split("\n")[0]
                return line.split()[0]
            except FileNotFoundError:
                raise RuntimeError("git executable not found")

    from .cache import CachingHttpTransport, DiskCache
    from .cache_storage import (
        prepare_default_root,
        prepare_local_root,
        versioning_child,
    )
    from .model import CacheConfig, LocalCacheConfig

    http = _ProductionHttp()

    if not no_cache:
        # Reviewed inventory policy is the sole TTL source.
        resolved_cache_ttl = (
            inventory_cache.ttl
            if isinstance(inventory_cache, CacheConfig)
            else None
        )
        if suggest_mode:
            disk = None
            http = CachingHttpTransport(
                http, ttl=resolved_cache_ttl, disk_cache=disk,
            )
        else:
            # Resolve and prepare the shared dedicated constructor root
            # before constructing DiskCache. cache_storage owns root
            # validation/hardening; DiskCache owns JSON format and TTL.
            local_dir = (
                local_cache.dir
                if isinstance(local_cache, LocalCacheConfig)
                else None
            )
            xdg = _os.environ.get("XDG_CACHE_HOME")
            home = Path(_os.path.expanduser("~"))

            if local_dir is not None:
                prepared_root = prepare_local_root(
                    local_dir, xdg_cache_home=xdg, home=home,
                )
            else:
                prepared_root = prepare_default_root(xdg, home=home)

            disk = DiskCache(
                versioning_child(prepared_root), ttl=resolved_cache_ttl,
            )
            http = CachingHttpTransport(
                http, ttl=resolved_cache_ttl, disk_cache=disk,
            )

    git = _ProductionGit()
    tokens = _collect_tokens()
    return TransportConfig(http=http, git=git, tokens=tokens)


def _collect_tokens() -> dict[str, str]:
    """Harvest provider auth tokens from environment variables."""
    tokens: dict[str, str] = {}
    for var in (
        "GITHUB_TOKEN", "NPM_TOKEN", "PYPI_TOKEN", "DOCKER_REGISTRY_TOKEN",
    ):
        val = _os.environ.get(var)
        if val:
            tokens[var] = val
    return tokens
