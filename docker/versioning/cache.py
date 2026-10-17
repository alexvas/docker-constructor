"""Read-through cache for HTTP responses with optional disk persistence.

Wraps an :class:`HttpTransport` and caches responses to both an
in-memory (fast) and an on-disk (persistent across invocations) store.

**Security**: Authenticated responses are keyed by a non-secret hash of
the ``Authorization`` header so that cached private data is never served
to unauthenticated callers (or callers with a different token).  Cache
files are written with owner-only permissions (``0600``); directory
creation and ``0700`` hardening are owned by ``cache_storage.py`` at
construction time, not by :class:`DiskCache`.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, TextIO, cast

from docker._owned_file import owned_file
from docker.filesystem.cleanup import CleanupFailures
from docker.filesystem.descriptors import OwnedDescriptor
from docker.filesystem.operations import DescriptorOps, PosixDescriptorOps

from .providers.base import HttpResponse, HttpTransport


# ---------------------------------------------------------------------------
# Auth-scope derivation
# ---------------------------------------------------------------------------


def _derive_scope(headers: object) -> str:
    """Return a stable, non-secret scope string from request headers.

    When an ``Authorization`` header is present its SHA-256 (first 16 hex
    chars) is used so that different tokens produce different scopes.
    Without any auth the scope is the literal ``"public"``.

    Header name matching is **case-insensitive** (RFC 7230 § 3.2).
    """
    auth = _extract_authorization(headers)
    if auth:
        return "auth:" + hashlib.sha256(auth.encode()).hexdigest()[:16]
    return "public"


def _casefold_get(headers: object, name: str) -> str:
    """Case-insensitive header lookup for a Mapping or iterable-of-pairs.

    Uses ``casefold()`` to match RFC 7230 § 3.2 (header field names are
    case-insensitive).  Returns ``""`` when the header is absent.
    """
    if headers is None:
        return ""
    key = name.casefold()
    if isinstance(headers, Mapping):
        for k, v in headers.items():
            if str(k).casefold() == key:
                return str(v) if v else ""
        return ""
    if isinstance(headers, (list, tuple)):
        for k, v in headers:
            if str(k).casefold() == key:
                return str(v)
    return ""


def _extract_authorization(headers: object) -> str:
    """Extract the ``Authorization`` header value (case-insensitive),
    or ``""`` when the header is absent."""
    return _casefold_get(headers, "authorization")


def _extract_accept(headers: object) -> str:
    """Extract the ``Accept`` header value (case-insensitive), or ``""``."""
    return _casefold_get(headers, "accept")


def _derive_representation(headers: object) -> str:
    """Return a cache-key component derived from representation-affecting
    headers (``Accept``).  An absent or ``*/*`` Accept header yields the
    neutral token ``"wildcard"``."""
    accept = _extract_accept(headers).strip()
    if not accept or accept == "*/*":
        return "wildcard"
    # Normalise: sort comma-separated media ranges for stable keys
    parts = sorted(v.strip() for v in accept.split(","))
    norm = ", ".join(parts)
    return "accept:" + hashlib.sha256(norm.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Disk cache
# ---------------------------------------------------------------------------


class DiskCache:
    """On-disk cache for HTTP responses keyed by ``(method, url, scope)``.

    Each entry is stored as a JSON file whose name is the SHA-256 of the
    cache key.  Files are written with owner-only permissions (``0600``).
    The cache directory itself is prepared and hardened to ``0700`` by
    ``cache_storage.py`` before the cache is constructed; ``DiskCache``
    never chmods its directory.

    Entries older than *ttl* seconds are treated as expired.
    """

    __slots__ = ("_dir", "_ttl", "_read_only")

    def __init__(self, directory: Path, ttl: int | None = None, *, read_only: bool = False) -> None:
        self._dir = directory
        self._ttl = ttl
        self._read_only = read_only

    # -- public API ---------------------------------------------------------

    def get(self, method: str, url: str, *, scope: str = "public", representation: str = "wildcard") -> HttpResponse | None:
        """Return a cached ``HttpResponse``-compatible object or ``None``."""
        path = self._path_for(method, url, scope, representation)
        try:
            with owned_file(lambda: open(path, encoding="utf-8"), label="cached response") as fh:
                data = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

        # Reject structurally corrupt entries (missing required keys)
        try:
            status = data["status"]
            stored_headers = dict(data.get("headers", {}))
            body_b64 = data["body_b64"]
        except (KeyError, TypeError):
            return None

        if self._ttl is not None:
            age = time.time() - data.get("ts", 0)
            if age > self._ttl:
                return None

        try:
            body = base64.b64decode(body_b64)
        except Exception:
            return None

        return _cached_response(status, stored_headers, body)

    def set(
        self, method: str, url: str, response: object, *, scope: str = "public", representation: str = "wildcard",
    ) -> None:
        """Persist *response* to disk.  No-op when the cache is read-only.

        The cache directory must already be prepared by ``cache_storage``;
        entries are published no-follow with ``0600`` and the directory is
        never recreated or chmod-ed here.
        """
        if self._read_only:
            return
        from .cache_storage import open_private_entry, publish_private_entry

        path = self._path_for(method, url, scope, representation)
        body = getattr(response, "body", b"")
        if not isinstance(body, bytes):
            body = b""
        stored_headers = dict(getattr(response, "headers", {}))
        # Strip Authorization from the stored response so it cannot leak
        stored_headers.pop("Authorization", None)
        stored_headers.pop("authorization", None)
        payload = {
            "ts": time.time(),
            "status": getattr(response, "status", 200),
            "headers": stored_headers,
            "body_b64": base64.b64encode(body).decode("ascii"),
        }
        tmp_name = path.name + ".tmp"
        fd = open_private_entry(self._dir, tmp_name)
        owner = OwnedDescriptor(
            cast(DescriptorOps, PosixDescriptorOps()), fd,
            label=f"cache entry {tmp_name}",
        )
        fh: TextIO | None = None
        primary: BaseException | None = None
        try:
            fh = os.fdopen(owner.fd, "w", encoding="utf-8", closefd=False)
            json.dump(payload, fh)
            # Flush and close the file object explicitly, then release the
            # descriptor, before publication.  Explicit cleanup keeps an
            # operation failure primary while a file-object-close and/or
            # descriptor-release failure is retained as ordinary secondary
            # diagnostics; a cleanup failure with no operation failure becomes
            # the primary failure and prevents publication.
            failures = CleanupFailures(None)
            failures.run(fh.close, ordinary=(OSError,))
            fh = None
            failures.run(owner.close, ordinary=(OSError,))
            result = failures.complete()
            if result is not None:
                raise result
        except BaseException as exc:
            primary = exc
            raise
        finally:
            # The private entry descriptor is owned immediately and released
            # through the shared owner before publication (terminal before the
            # close, never retried).  A JSON-write/file-object-close failure
            # stays primary and an ordinary descriptor-release failure is
            # attached as secondary; a cleanup failure without a primary
            # prevents publication.
            failures = CleanupFailures(primary)
            if fh is not None:
                failures.run(fh.close, ordinary=(OSError,))
            failures.run(owner.close, ordinary=(OSError,))
            result = failures.complete()
            if result is not None:
                raise result
        publish_private_entry(self._dir, tmp_name, path.name)

    def clear(self) -> None:
        """Remove all cached entries from disk."""
        if not self._dir.exists():
            return
        for child in self._dir.iterdir():
            try:
                child.unlink()
            except OSError:
                pass

    # -- helpers ------------------------------------------------------------

    def _path_for(self, method: str, url: str, scope: str = "public", representation: str = "wildcard") -> Path:
        key = f"{method}:{url}:{scope}:{representation}"
        digest = hashlib.sha256(key.encode()).hexdigest()
        return self._dir / digest


def _cached_response(status: int, headers: dict[str, str], body: bytes) -> HttpResponse:
    """Reconstruct a cached response value."""
    return HttpResponse(status=status, headers=headers, body=body)


# ---------------------------------------------------------------------------
# Two-tier transport (memory + disk)
# ---------------------------------------------------------------------------


# Sentinel for "no disk cache argument was passed" — distinct from
# ``None`` (which means "caller explicitly wants no disk cache").


class CachingHttpTransport(HttpTransport):
    """Read-through HTTP cache with in-memory and on-disk tiers.

    On a cache miss the delegate transport is called and the response
    is stored in both tiers.  On a hit the in-memory tier is checked
    first; the disk tier provides persistence across CLI invocations.

    **Auth isolation**: The in-memory and disk caches derive a
    non-secret *scope* from the ``Authorization`` header.  Responses
    fetched with one token are never served to a caller with a
    different token (or no token).

    Parameters:
        delegate: The underlying transport to call on cache miss.
        ttl: Cache TTL in seconds.  ``None`` means entries never expire
            for an explicitly supplied disk cache.
        disk_cache: Optional :class:`DiskCache` for persistence.
            Persistent disk caching requires an explicitly supplied,
            already-prepared ``DiskCache``.  When omitted, caching is
            memory-only (equivalent to passing ``None``).
    """

    __slots__ = ("_delegate", "_ttl", "_mem", "_disk")

    def __init__(
        self,
        delegate: HttpTransport,
        ttl: int | None = None,
        disk_cache: DiskCache | None = None,
    ) -> None:
        self._delegate = delegate
        self._ttl = ttl
        # Key: (method, url, scope, representation)
        self._mem: dict[tuple[str, str, str, str], tuple[float, HttpResponse]] = {}
        self._disk = disk_cache

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] = MappingProxyType({}),
        nocache: bool = False,
    ) -> HttpResponse:
        # For credential-bearing requests (e.g. Docker bearer-token
        # endpoints), skip caching entirely to avoid persisting secrets
        # in the disk cache.
        if nocache:
            headers_dict: dict[str, str] = {}
            if headers is not None:
                headers_dict = dict(headers)
            return self._delegate.request(method, url, headers=headers_dict)

        scope = _derive_scope(headers)
        representation = _derive_representation(headers)
        key = (method, url, scope, representation)
        now = time.monotonic()

        # 1. In-memory (fastest)
        entry = self._mem.get(key)
        if entry is not None:
            ts, resp = entry
            if self._ttl is None or (now - ts) < self._ttl:
                return resp

        # 2. Disk (persistent across invocations)
        if self._disk is not None:
            cached = self._disk.get(method, url, scope=scope, representation=representation)
            if cached is not None:
                self._mem[key] = (now, cached)
                return cached

        # 3. Delegate
        headers_dict: dict[str, str] = {}
        if headers is not None:
            headers_dict = dict(headers)

        resp = self._delegate.request(method, url, headers=headers_dict)
        self._mem[key] = (now, resp)
        if self._disk is not None:
            self._disk.set(method, url, resp, scope=scope, representation=representation)
        return resp

    def clear(self) -> None:
        """Remove all cached entries (memory + disk)."""
        self._mem.clear()
        if self._disk is not None:
            self._disk.clear()

    @property
    def size(self) -> int:
        """Number of entries in the in-memory cache."""
        return len(self._mem)

    @property
    def ttl(self) -> int | None:
        """Current TTL (seconds) or ``None`` for infinite."""
        return self._ttl

    @property
    def disk(self) -> DiskCache | None:
        """The underlying disk cache, if any."""
        return self._disk
