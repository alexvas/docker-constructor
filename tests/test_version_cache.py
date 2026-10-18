"""Tests for in-memory HTTP response cache, disk persistence, and security."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from docker.versioning.cache import CachingHttpTransport, DiskCache, _cached_response
from docker.versioning.cache_storage import CacheStorageError
from tests.versioning.support.fake_http import FakeHttpTransport


class _CountingTransport(FakeHttpTransport):
    """Transport that counts how many times each URL was requested."""

    def __init__(self):
        super().__init__()
        self.call_counts: dict[tuple[str, str], int] = {}

    def request(self, method, url, *, headers=()):
        key = (method, url)
        self.call_counts[key] = self.call_counts.get(key, 0) + 1
        return super().request(method, url, headers=headers)


class TestCachingHttpTransport(unittest.TestCase):
    def setUp(self):
        self.delegate = _CountingTransport()
        self.delegate.set("GET", "https://example.com/a", status=200, body=b"hello")
        self.delegate.set("GET", "https://example.com/b", status=200, body=b"world")

    # ------------------------------------------------------------------
    # Basic read-through
    # ------------------------------------------------------------------

    def test_cache_miss_calls_delegate(self):
        cache = CachingHttpTransport(self.delegate)
        resp = cache.request("GET", "https://example.com/a")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.body, b"hello")
        self.assertEqual(
            self.delegate.call_counts.get(("GET", "https://example.com/a")), 1
        )

    def test_cache_hit_avoids_delegate(self):
        cache = CachingHttpTransport(self.delegate)
        cache.request("GET", "https://example.com/a")  # miss — fills cache
        resp = cache.request("GET", "https://example.com/a")  # hit
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.body, b"hello")
        # Delegate called exactly once for this URL
        self.assertEqual(
            self.delegate.call_counts.get(("GET", "https://example.com/a")), 1
        )

    def test_different_methods_are_separate_keys(self):
        self.delegate.set("HEAD", "https://example.com/a", status=200, body=b"")
        cache = CachingHttpTransport(self.delegate)
        cache.request("GET", "https://example.com/a")
        cache.request("HEAD", "https://example.com/a")
        self.assertEqual(
            self.delegate.call_counts.get(("GET", "https://example.com/a")), 1
        )
        self.assertEqual(
            self.delegate.call_counts.get(("HEAD", "https://example.com/a")), 1
        )

    def test_different_urls_are_separate_keys(self):
        cache = CachingHttpTransport(self.delegate)
        cache.request("GET", "https://example.com/a")
        cache.request("GET", "https://example.com/b")
        cache.request("GET", "https://example.com/a")
        self.assertEqual(
            self.delegate.call_counts.get(("GET", "https://example.com/a")), 1
        )
        self.assertEqual(
            self.delegate.call_counts.get(("GET", "https://example.com/b")), 1
        )
        self.assertEqual(cache.size, 2)

    def test_omitted_disk_cache_is_memory_only(self):
        """A finite TTL with an omitted ``disk_cache`` must not create or
        read any legacy or constructor cache directory — caching is
        memory-only until an explicit prepared disk cache is supplied."""
        with tempfile.TemporaryDirectory() as tmp:
            old_xdg = os.environ.get("XDG_CACHE_HOME")
            old_home = os.environ.get("HOME")
            xdg = Path(tmp) / "xdg" / "cache"
            home = Path(tmp) / "home"
            os.environ["XDG_CACHE_HOME"] = str(xdg)
            os.environ["HOME"] = str(home)
            try:
                home.mkdir(parents=True, exist_ok=True)
                cache = CachingHttpTransport(self.delegate, ttl=3600)
                resp = cache.request("GET", "https://example.com/a")
                self.assertEqual(resp.body, b"hello")
                # Second request is served from memory, not disk.
                cache.request("GET", "https://example.com/a")
                self.assertEqual(
                    self.delegate.call_counts.get(("GET", "https://example.com/a")),
                    1,
                )
                self.assertFalse((xdg / "pi-cli" / "versioning").exists())
                self.assertFalse((xdg / "docker-constructor").exists())
                self.assertFalse((home / ".cache").exists())
            finally:
                if old_xdg is None:
                    os.environ.pop("XDG_CACHE_HOME", None)
                else:
                    os.environ["XDG_CACHE_HOME"] = old_xdg
                if old_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = old_home

    # ------------------------------------------------------------------
    # TTL behaviour
    # ------------------------------------------------------------------

    def test_infinite_ttl_never_expires(self):
        cache = CachingHttpTransport(self.delegate, ttl=None, disk_cache=None)
        cache.request("GET", "https://example.com/a")
        # force monotonic forward
        cache.request("GET", "https://example.com/a")
        self.assertEqual(
            self.delegate.call_counts.get(("GET", "https://example.com/a")), 1
        )

    def test_finite_ttl_expires_after_duration(self):
        cache = CachingHttpTransport(
            self.delegate, ttl=0, disk_cache=None
        )  # zero = always expired
        cache.request("GET", "https://example.com/a")
        cache.request("GET", "https://example.com/a")
        self.assertEqual(
            self.delegate.call_counts.get(("GET", "https://example.com/a")), 2
        )

    def test_finite_ttl_respected_within_window(self):
        cache = CachingHttpTransport(self.delegate, ttl=3600, disk_cache=None)
        cache.request("GET", "https://example.com/a")
        cache.request("GET", "https://example.com/a")
        cache.request("GET", "https://example.com/a")
        self.assertEqual(
            self.delegate.call_counts.get(("GET", "https://example.com/a")), 1
        )

    # ------------------------------------------------------------------
    # Clear
    # ------------------------------------------------------------------

    def test_clear_empties_cache(self):
        cache = CachingHttpTransport(self.delegate)
        cache.request("GET", "https://example.com/a")
        cache.request("GET", "https://example.com/b")
        self.assertEqual(cache.size, 2)
        cache.clear()
        self.assertEqual(cache.size, 0)
        # Next request hits delegate again
        cache.request("GET", "https://example.com/a")
        self.assertEqual(
            self.delegate.call_counts.get(("GET", "https://example.com/a")), 2
        )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    def test_size(self):
        cache = CachingHttpTransport(self.delegate)
        self.assertEqual(cache.size, 0)
        cache.request("GET", "https://example.com/a")
        self.assertEqual(cache.size, 1)
        cache.request("GET", "https://example.com/b")
        self.assertEqual(cache.size, 2)

    def test_ttl_property(self):
        cache = CachingHttpTransport(self.delegate, disk_cache=None)
        self.assertIsNone(cache.ttl)
        cache2 = CachingHttpTransport(self.delegate, ttl=60, disk_cache=None)
        self.assertEqual(cache2.ttl, 60)

    # ------------------------------------------------------------------
    # nocache flag — credential-bearing requests
    # ------------------------------------------------------------------

    def test_nocache_bypasses_both_read_and_write(self):
        """``nocache=True`` must call the delegate every time and
        must never store the response in memory or on disk."""
        tmpdir = Path(tempfile.mkdtemp())
        try:
            dc = DiskCache(tmpdir, ttl=3600)
            cache = CachingHttpTransport(self.delegate, ttl=3600, disk_cache=dc)

            # First nocache request
            resp1 = cache.request("GET", "https://example.com/a", nocache=True)
            self.assertEqual(resp1.body, b"hello")
            self.assertEqual(
                self.delegate.call_counts.get(("GET", "https://example.com/a")), 1
            )
            self.assertEqual(cache.size, 0)  # not stored in memory

            # Second nocache request — must call delegate again
            resp2 = cache.request("GET", "https://example.com/a", nocache=True)
            self.assertEqual(resp2.body, b"hello")
            self.assertEqual(
                self.delegate.call_counts.get(("GET", "https://example.com/a")), 2
            )

            # Verify nothing was written to disk
            path = dc._path_for("GET", "https://example.com/a")
            self.assertFalse(path.exists(), f"nocache wrote to disk: {path}")
        finally:
            import shutil

            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_nocache_does_not_read_existing_cache(self):
        """Even when a cached response exists, ``nocache=True`` must
        bypass it and call the delegate."""
        tmpdir = Path(tempfile.mkdtemp())
        try:
            dc = DiskCache(tmpdir, ttl=3600)
            cache = CachingHttpTransport(self.delegate, ttl=3600, disk_cache=dc)

            # First: regular (cacheable) request populates cache
            cache.request("GET", "https://example.com/a")
            self.assertEqual(
                self.delegate.call_counts.get(("GET", "https://example.com/a")), 1
            )
            self.assertEqual(cache.size, 1)

            # Second: nocache — must bypass memory and disk cache
            self.delegate.set(
                "GET", "https://example.com/a", status=200, body=b"updated"
            )
            resp = cache.request("GET", "https://example.com/a", nocache=True)
            self.assertEqual(resp.body, b"updated")
            self.assertEqual(
                self.delegate.call_counts.get(("GET", "https://example.com/a")), 2
            )
            self.assertEqual(cache.size, 1)  # only original entry remains
        finally:
            import shutil

            shutil.rmtree(tmpdir, ignore_errors=True)

    # ------------------------------------------------------------------
    # Edge cases
    # ------------------------------------------------------------------

    def test_error_responses_are_cached_too(self):
        self.delegate.set("GET", "https://example.com/err", status=500, body=b"fail")
        cache = CachingHttpTransport(self.delegate)
        resp1 = cache.request("GET", "https://example.com/err")
        self.assertEqual(resp1.status, 500)
        resp2 = cache.request("GET", "https://example.com/err")
        self.assertEqual(resp2.status, 500)
        self.assertEqual(
            self.delegate.call_counts.get(("GET", "https://example.com/err")), 1
        )


# ---------------------------------------------------------------------------
# DiskCache unit tests
# ---------------------------------------------------------------------------

import tempfile


class TestDiskCache(unittest.TestCase):
    def setUp(self):
        self._dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil

        shutil.rmtree(self._dir, ignore_errors=True)

    def test_set_and_get(self):
        dc = DiskCache(self._dir)
        resp = _cached_response(200, {"content-type": "text/plain"}, b"hello")
        dc.set("GET", "https://example.com/x", resp)
        cached = dc.get("GET", "https://example.com/x")
        self.assertIsNotNone(cached)
        self.assertEqual(cached.status, 200)  # type: ignore[union-attr]
        self.assertEqual(cached.body, b"hello")  # type: ignore[union-attr]

    def test_miss_returns_none(self):
        dc = DiskCache(self._dir)
        self.assertIsNone(dc.get("GET", "https://example.com/nonexistent"))

    def test_different_keys(self):
        dc = DiskCache(self._dir)
        dc.set("GET", "https://example.com/a", _cached_response(200, {}, b"a"))
        dc.set("POST", "https://example.com/a", _cached_response(201, {}, b"b"))
        self.assertIsNotNone(dc.get("GET", "https://example.com/a"))
        self.assertIsNotNone(dc.get("POST", "https://example.com/a"))

    def test_ttl_expiry(self):
        dc = DiskCache(self._dir, ttl=0)  # instantly expired
        dc.set("GET", "https://example.com/x", _cached_response(200, {}, b"stale"))
        self.assertIsNone(dc.get("GET", "https://example.com/x"))

    def test_clear(self):
        dc = DiskCache(self._dir)
        dc.set("GET", "https://example.com/x", _cached_response(200, {}, b"x"))
        self.assertIsNotNone(dc.get("GET", "https://example.com/x"))
        dc.clear()
        self.assertIsNone(dc.get("GET", "https://example.com/x"))

    def test_read_only_disk_cache_writes_are_noop(self):
        """DiskCache(read_only=True).set() must not write cache files."""
        dc = DiskCache(self._dir, read_only=True)
        resp = _cached_response(200, {}, b"should-not-persist")
        dc.set("GET", "https://example.com/x", resp)
        # No cache files must have been created
        path = dc._path_for("GET", "https://example.com/x")
        self.assertFalse(path.exists(), f"read-only cache wrote file: {path}")
        # And get() must miss
        self.assertIsNone(dc.get("GET", "https://example.com/x"))

    def test_corrupt_file_returns_none(self):
        dc = DiskCache(self._dir)
        path = dc._path_for("GET", "https://example.com/bad")
        self._dir.mkdir(parents=True, exist_ok=True)
        path.write_text("not json", encoding="utf-8")
        self.assertIsNone(dc.get("GET", "https://example.com/bad"))


class TestDiskCachePersistence(unittest.TestCase):
    """Simulate cross-invocation persistence: two CachingHttpTransport
    instances sharing a disk cache directory."""

    def setUp(self):
        from tests.versioning.support.fake_http import FakeHttpTransport

        self._dir = Path(tempfile.mkdtemp())
        self.delegate = _CountingTransport()

    def tearDown(self):
        import shutil

        shutil.rmtree(self._dir, ignore_errors=True)

    def test_second_invocation_reads_from_disk(self):
        self.delegate.set("GET", "https://example.com/a", status=200, body=b"first")
        dc = DiskCache(self._dir, ttl=3600)
        cache1 = CachingHttpTransport(self.delegate, ttl=3600, disk_cache=dc)
        cache1.request("GET", "https://example.com/a")
        self.assertEqual(
            self.delegate.call_counts.get(("GET", "https://example.com/a")), 1
        )

        # Simulate second invocation: new transport, same disk cache
        delegate2 = _CountingTransport()
        dc2 = DiskCache(self._dir, ttl=3600)
        cache2 = CachingHttpTransport(delegate2, ttl=3600, disk_cache=dc2)
        resp = cache2.request("GET", "https://example.com/a")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.body, b"first")
        # Second delegate was never called — served from disk
        self.assertIsNone(delegate2.call_counts.get(("GET", "https://example.com/a")))

    def test_disk_respects_ttl_on_second_invocation(self):
        self.delegate.set("GET", "https://example.com/b", status=200, body=b"stale")
        dc = DiskCache(self._dir, ttl=0)  # instantly expired
        cache1 = CachingHttpTransport(self.delegate, ttl=0, disk_cache=dc)
        cache1.request("GET", "https://example.com/b")

        # Second invocation with expired TTL
        delegate2 = _CountingTransport()
        delegate2.set("GET", "https://example.com/b", status=200, body=b"fresh")
        dc2 = DiskCache(self._dir, ttl=0)
        cache2 = CachingHttpTransport(delegate2, ttl=0, disk_cache=dc2)
        resp = cache2.request("GET", "https://example.com/b")
        self.assertEqual(resp.body, b"fresh")
        self.assertEqual(delegate2.call_counts.get(("GET", "https://example.com/b")), 1)


# ---------------------------------------------------------------------------
# Security: auth isolation, file permissions, corrupt-entry resilience
# ---------------------------------------------------------------------------


class TestAuthIsolation(unittest.TestCase):
    """Authenticated responses must never be served to a different auth context."""

    def setUp(self):
        self._dir = Path(tempfile.mkdtemp())
        self.delegate = _CountingTransport()

    def tearDown(self):
        import shutil

        shutil.rmtree(self._dir, ignore_errors=True)

    def test_authenticated_cache_not_served_to_public(self):
        """Token-scoped response is invisible without the same token."""
        self.delegate.set(
            "GET", "https://example.com/private", status=200, body=b"secret"
        )
        dc = DiskCache(self._dir, ttl=3600)
        cache = CachingHttpTransport(self.delegate, ttl=3600, disk_cache=dc)

        # First request: with token
        auth_headers = {"Authorization": "Bearer tok-abc"}
        resp1 = cache.request(
            "GET", "https://example.com/private", headers=auth_headers
        )
        self.assertEqual(resp1.body, b"secret")
        self.assertEqual(
            self.delegate.call_counts.get(("GET", "https://example.com/private")), 1
        )

        # Second request: without token — must miss cache
        resp2 = cache.request("GET", "https://example.com/private")
        # Delegate called again (public scope missed the auth-scoped cache)
        self.assertEqual(
            self.delegate.call_counts.get(("GET", "https://example.com/private")), 2
        )
        self.assertEqual(resp2.body, b"secret")  # delegate returned same body

    def test_different_tokens_are_isolated(self):
        """Two different tokens produce independent cache entries."""
        self.delegate.set(
            "GET", "https://example.com/pkg", status=200, body=b"private-a"
        )
        dc = DiskCache(self._dir, ttl=3600)
        cache = CachingHttpTransport(self.delegate, ttl=3600, disk_cache=dc)

        # Token A
        resp_a = cache.request(
            "GET", "https://example.com/pkg", headers={"Authorization": "Bearer tok-a"}
        )
        self.assertEqual(resp_a.body, b"private-a")
        self.assertEqual(
            self.delegate.call_counts.get(("GET", "https://example.com/pkg")), 1
        )

        # Token B — different scope, cache miss
        self.delegate.set(
            "GET", "https://example.com/pkg", status=200, body=b"private-b"
        )
        resp_b = cache.request(
            "GET", "https://example.com/pkg", headers={"Authorization": "Bearer tok-b"}
        )
        self.assertEqual(resp_b.body, b"private-b")
        self.assertEqual(
            self.delegate.call_counts.get(("GET", "https://example.com/pkg")), 2
        )

    def test_authorization_stripped_from_stored_response(self):
        """The Authorization header must not appear in the cached payload."""
        self.delegate.set("GET", "https://example.com/pkg", status=200, body=b"ok")
        dc = DiskCache(self._dir, ttl=3600)
        cache = CachingHttpTransport(self.delegate, ttl=3600, disk_cache=dc)

        cache.request(
            "GET",
            "https://example.com/pkg",
            headers={"Authorization": "Bearer secret-token"},
        )

        # Read the raw file from disk
        scope = (
            "auth:" + hashlib.sha256("Bearer secret-token".encode()).hexdigest()[:16]
        )
        path = dc._path_for("GET", "https://example.com/pkg", scope)
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        headers_on_disk = data.get("headers", {})
        self.assertNotIn("Authorization", headers_on_disk)
        self.assertNotIn("authorization", headers_on_disk)

    def test_arbitrary_authorization_casing_not_public(self):
        """Any casing of the ``Authorization`` header (e.g. ``AUTHORIZATION``,
        ``AuThOrIzAtIoN``) must be treated as authed, not as public.
        HTTP header names are case-insensitive (RFC 7230 § 3.2)."""
        for casing in ("authorization", "AUTHORIZATION", "AuThOrIzAtIoN"):
            with self.subTest(casing=casing):
                subdir = Path(tempfile.mkdtemp())
                try:
                    delegate = _CountingTransport()
                    delegate.set(
                        "GET", "https://example.com/private", status=200, body=b"secret"
                    )
                    dc = DiskCache(subdir, ttl=3600)
                    cache = CachingHttpTransport(delegate, ttl=3600, disk_cache=dc)

                    headers = {casing: "Bearer tok-" + casing}
                    resp1 = cache.request(
                        "GET", "https://example.com/private", headers=headers
                    )
                    self.assertEqual(resp1.body, b"secret")
                    self.assertEqual(
                        delegate.call_counts.get(
                            ("GET", "https://example.com/private")
                        ),
                        1,
                    )

                    # No auth → must miss cache
                    resp2 = cache.request("GET", "https://example.com/private")
                    self.assertEqual(
                        delegate.call_counts.get(
                            ("GET", "https://example.com/private")
                        ),
                        2,
                    )
                finally:
                    import shutil

                    shutil.rmtree(subdir, ignore_errors=True)

    def test_any_casing_same_token_shares_scope(self):
        """Same token value with any header casing produces identical scope."""
        self.delegate.set("GET", "https://example.com/x", status=200, body=b"shared")
        dc = DiskCache(self._dir, ttl=3600)
        cache = CachingHttpTransport(self.delegate, ttl=3600, disk_cache=dc)

        # First request: standard casing
        cache.request(
            "GET", "https://example.com/x", headers={"Authorization": "Bearer tok"}
        )
        self.assertEqual(
            self.delegate.call_counts.get(("GET", "https://example.com/x")), 1
        )

        # Second request: AUTHORIZATION — same scope → cache hit
        resp2 = cache.request(
            "GET", "https://example.com/x", headers={"AUTHORIZATION": "Bearer tok"}
        )
        self.assertEqual(resp2.body, b"shared")
        self.assertEqual(
            self.delegate.call_counts.get(("GET", "https://example.com/x")), 1
        )


class TestRepresentationIsolation(unittest.TestCase):
    """Requests with different Accept headers must produce distinct cache entries."""

    def setUp(self):
        self._dir = Path(tempfile.mkdtemp())
        self.delegate = _CountingTransport()

    def tearDown(self):
        import shutil

        shutil.rmtree(self._dir, ignore_errors=True)

    def test_different_accept_types_are_isolated(self):
        """``Accept: application/json`` and ``Accept: text/html`` must not collide."""
        self.delegate.set("GET", "https://example.com/api", status=200, body=b'{"v":1}')
        dc = DiskCache(self._dir, ttl=3600)
        cache = CachingHttpTransport(self.delegate, ttl=3600, disk_cache=dc)

        resp_json = cache.request(
            "GET", "https://example.com/api", headers={"Accept": "application/json"}
        )
        self.assertEqual(resp_json.body, b'{"v":1}')
        self.assertEqual(
            self.delegate.call_counts.get(("GET", "https://example.com/api")), 1
        )

        # Different Accept → cache miss
        self.delegate.set("GET", "https://example.com/api", status=200, body=b"<html>")
        resp_html = cache.request(
            "GET", "https://example.com/api", headers={"Accept": "text/html"}
        )
        self.assertEqual(resp_html.body, b"<html>")
        self.assertEqual(
            self.delegate.call_counts.get(("GET", "https://example.com/api")), 2
        )

    def test_no_accept_and_wildcard_are_equivalent(self):
        """Absent Accept and ``Accept: */*`` share the same cache entry."""
        self.delegate.set("GET", "https://example.com/data", status=200, body=b"ok")
        dc = DiskCache(self._dir, ttl=3600)
        cache = CachingHttpTransport(self.delegate, ttl=3600, disk_cache=dc)

        resp1 = cache.request("GET", "https://example.com/data")
        self.assertEqual(resp1.body, b"ok")
        self.assertEqual(
            self.delegate.call_counts.get(("GET", "https://example.com/data")), 1
        )

        # */* normalises to the same wildcard representation
        resp2 = cache.request(
            "GET", "https://example.com/data", headers={"Accept": "*/*"}
        )
        self.assertEqual(resp2.body, b"ok")
        self.assertEqual(
            self.delegate.call_counts.get(("GET", "https://example.com/data")), 1
        )  # cache hit

    def test_any_accept_casing_recognised(self):
        """Any casing of ``Accept`` (``ACCEPT``, ``aCcEpT``, etc.) is
        case-insensitively detected via ``casefold()``."""
        for casing in ("Accept", "ACCEPT", "aCcEpT", "accept"):
            with self.subTest(casing=casing):
                subdir = Path(tempfile.mkdtemp())
                try:
                    delegate = _CountingTransport()
                    delegate.set(
                        "GET", "https://example.com/y", status=200, body=b"json"
                    )
                    dc = DiskCache(subdir, ttl=3600)
                    cache = CachingHttpTransport(delegate, ttl=3600, disk_cache=dc)

                    cache.request(
                        "GET",
                        "https://example.com/y",
                        headers={"Accept": "application/json"},
                    )
                    self.assertEqual(
                        delegate.call_counts.get(("GET", "https://example.com/y")), 1
                    )

                    # Arbitrary casing, same value → cache hit
                    resp2 = cache.request(
                        "GET",
                        "https://example.com/y",
                        headers={casing: "application/json"},
                    )
                    self.assertEqual(resp2.body, b"json")
                    self.assertEqual(
                        delegate.call_counts.get(("GET", "https://example.com/y")), 1
                    )
                finally:
                    import shutil

                    shutil.rmtree(subdir, ignore_errors=True)

    def test_accept_sorted_normalisation(self):
        """Comma-separated Accept values are sorted for stable cache keys."""
        self.delegate.set("GET", "https://example.com/z", status=200, body=b"data")
        dc = DiskCache(self._dir, ttl=3600)
        cache = CachingHttpTransport(self.delegate, ttl=3600, disk_cache=dc)

        # Order A
        cache.request(
            "GET",
            "https://example.com/z",
            headers={"Accept": "text/html, application/json"},
        )
        self.assertEqual(
            self.delegate.call_counts.get(("GET", "https://example.com/z")), 1
        )

        # Order B (reversed) — normalised to same representation → hit
        resp2 = cache.request(
            "GET",
            "https://example.com/z",
            headers={"Accept": "application/json, text/html"},
        )
        self.assertEqual(resp2.body, b"data")
        self.assertEqual(
            self.delegate.call_counts.get(("GET", "https://example.com/z")), 1
        )


class TestFilePermissions(unittest.TestCase):
    def setUp(self):
        self._dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil

        shutil.rmtree(self._dir, ignore_errors=True)

    def test_cache_file_is_owner_only(self):
        dc = DiskCache(self._dir)
        resp = _cached_response(200, {}, b"x")
        dc.set("GET", "https://example.com/x", resp)
        path = dc._path_for("GET", "https://example.com/x")
        mode = path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600, f"expected 0600, got {oct(mode)}")

    def test_cache_dir_is_owner_only(self):
        """A prepared 0700 directory is preserved (never widened) by writes."""
        dc = DiskCache(self._dir)
        resp = _cached_response(200, {}, b"x")
        dc.set("GET", "https://example.com/x", resp)
        mode = self._dir.stat().st_mode & 0o777
        self.assertEqual(mode, 0o700, f"expected 0700, got {oct(mode)}")

    def test_permissions_survive_second_write(self):
        """Re-writing an entry must not widen permissions."""
        dc = DiskCache(self._dir)
        dc.set("GET", "https://example.com/a", _cached_response(200, {}, b"first"))
        # Manually weaken permissions to simulate a pre-existing loose file
        path = dc._path_for("GET", "https://example.com/a")
        path.chmod(0o644)
        # Overwrite — should clamp back to 0600
        dc.set("GET", "https://example.com/a", _cached_response(200, {}, b"second"))
        mode = path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_pre_existing_parent_not_chmodded(self):
        """DiskCache must not chmod a pre-existing parent; the supplied
        cache directory itself must already be prepared 0700."""
        parent = self._dir / "pre-existing"
        parent.mkdir()
        parent.chmod(0o755)  # deliberately world-readable
        cache_dir = parent / "cache"
        cache_dir.mkdir()
        os.chmod(cache_dir, 0o700)  # prepared owner-only
        dc = DiskCache(cache_dir)
        dc.set("GET", "https://example.com/x", _cached_response(200, {}, b"x"))
        # Parent must still have its original 0755 permissions
        parent_mode = parent.stat().st_mode & 0o777
        self.assertEqual(parent_mode, 0o755, f"expected 0755, got {oct(parent_mode)}")
        # Grandparent (self._dir) must also be untouched
        gp_mode = self._dir.stat().st_mode & 0o777
        self.assertEqual(gp_mode, 0o700, f"expected 0700, got {oct(gp_mode)}")

    def test_unprepared_0755_directory_fails_closed(self):
        """DiskCache must reject an owned-but-non-private (0755)
        caller-supplied directory without chmodding it — the write fails
        closed through the cache-storage helper, no entry is created, and
        the 0755 mode is preserved."""
        self._dir.mkdir(parents=True, exist_ok=True)
        self._dir.chmod(0o755)
        dc = DiskCache(self._dir)
        with self.assertRaises(CacheStorageError):
            dc.set("GET", "https://example.com/x", _cached_response(200, {}, b"x"))
        # No entry file was created.
        path = dc._path_for("GET", "https://example.com/x")
        self.assertFalse(path.exists())
        # The directory was rejected, not chmodded.
        mode = self._dir.stat().st_mode & 0o777
        self.assertEqual(mode, 0o755, f"expected 0755 unchanged, got {oct(mode)}")

    def test_missing_directory_fails_closed(self):
        """DiskCache must not recreate an absent prepared directory as
        0755; it fails closed with the cache-storage error instead."""
        missing = self._dir / "not-prepared" / "versioning"
        dc = DiskCache(missing)
        with self.assertRaises(CacheStorageError):
            dc.set("GET", "https://example.com/x", _cached_response(200, {}, b"x"))
        self.assertFalse(missing.exists())
        self.assertFalse((self._dir / "not-prepared").exists())


class TestCorruptEntryResilience(unittest.TestCase):
    def setUp(self):
        self._dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil

        shutil.rmtree(self._dir, ignore_errors=True)

    def test_missing_status_key_returns_none(self):
        dc = DiskCache(self._dir)
        path = dc._path_for("GET", "https://example.com/bad")
        self._dir.mkdir(parents=True, exist_ok=True)
        # Valid JSON but no 'status' key
        path.write_text('{"headers":{},"body_b64":""}', encoding="utf-8")
        self.assertIsNone(dc.get("GET", "https://example.com/bad"))

    def test_body_b64_is_not_a_string_returns_none(self):
        dc = DiskCache(self._dir)
        path = dc._path_for("GET", "https://example.com/bad")
        self._dir.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"status":200,"headers":{},"body_b64":123}',
            encoding="utf-8",
        )
        self.assertIsNone(dc.get("GET", "https://example.com/bad"))

    def test_truncated_json_returns_none(self):
        dc = DiskCache(self._dir)
        path = dc._path_for("GET", "https://example.com/bad")
        self._dir.mkdir(parents=True, exist_ok=True)
        path.write_text('{"status":200,"headers":{},"body_b64', encoding="utf-8")
        self.assertIsNone(dc.get("GET", "https://example.com/bad"))

    def test_empty_file_returns_none(self):
        dc = DiskCache(self._dir)
        path = dc._path_for("GET", "https://example.com/empty")
        self._dir.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        self.assertIsNone(dc.get("GET", "https://example.com/empty"))
