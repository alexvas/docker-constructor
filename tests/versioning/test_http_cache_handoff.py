"""Handoff regression tests: transport construction prepares the shared
dedicated cache root before DiskCache writes.

These tests pin the Phase 2 boundary:

* ``build_transports()`` resolves and prepares the constructor cache root
  through ``cache_storage`` (``prepare_local_root`` / ``prepare_default_root``)
  and derives the HTTP directory with ``versioning_child``.
* ``DiskCache`` only ever receives that already-prepared directory and never
  recreates or chmods it.
"""
from __future__ import annotations

import os
import stat
import tempfile
import types
import unittest
from pathlib import Path

from docker.versioning.cache_storage import CacheStorageError
from docker.versioning.model import CacheConfig, LocalCacheConfig


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


class _HandoffTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="http-handoff-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self._saved = {
            key: os.environ.get(key) for key in ("HOME", "XDG_CACHE_HOME")
        }
        os.environ["HOME"] = str(self.home)

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _build(self, *, xdg: str, local_dir: str | None = None,
               inventory_cache: CacheConfig | None = None,
               no_cache: bool = False):
        from docker.versioning.transports import build_transports

        os.environ["XDG_CACHE_HOME"] = xdg
        local = (
            LocalCacheConfig(dir=local_dir)
            if local_dir is not None
            else None
        )
        return build_transports(
            no_cache=no_cache,
            inventory_cache=inventory_cache,
            local_cache=local,
        )

    def _entries(self, directory: Path) -> list[Path]:
        return [p for p in directory.iterdir() if p.is_file()]


class TestTransportRootPreparation(_HandoffTestCase):
    def test_existing_xdg_unchanged_versioning_0700_entry_0600(self) -> None:
        xdg = self.base / "xdg" / "cache"
        xdg.mkdir(parents=True)
        os.chmod(xdg, 0o755)

        config = self._build(xdg=str(xdg))
        disk = config.http.disk
        self.assertIsNotNone(disk)

        versioning = xdg / "docker-constructor" / "versioning"
        self.assertEqual(_mode(versioning), 0o700)

        disk.set(
            "GET",
            "https://example.test/entry",
            types.SimpleNamespace(status=200, headers={}, body=b"payload"),
        )

        self.assertEqual(_mode(xdg), 0o755)
        entries = self._entries(versioning)
        self.assertEqual(len(entries), 1)
        self.assertEqual(_mode(entries[0]), 0o600)

    def test_missing_xdg_created_0700(self) -> None:
        xdg = self.base / "xdg" / "cache"  # missing

        config = self._build(xdg=str(xdg))
        disk = config.http.disk
        self.assertIsNotNone(disk)

        self.assertEqual(_mode(xdg), 0o700)
        versioning = xdg / "docker-constructor" / "versioning"
        self.assertEqual(_mode(versioning), 0o700)

    def test_unsafe_local_root_fails_before_write(self) -> None:
        xdg = self.base / "xdg" / "cache"
        xdg.mkdir(parents=True)

        for bad in (str(xdg), "/", str(self.home), str(self.base)):
            with self.subTest(local_dir=bad):
                with self.assertRaises(CacheStorageError):
                    self._build(xdg=str(xdg), local_dir=bad)

        self.assertFalse((xdg / "docker-constructor").exists())
        self.assertFalse((self.home / ".cache").exists())

    def test_reviewed_ttl_is_used_with_and_without_local_companion(self) -> None:
        xdg = self.base / "xdg" / "cache"
        reviewed = CacheConfig(ttl=123)
        self.assertEqual(self._build(xdg=str(xdg), inventory_cache=reviewed)
                         .http.ttl, 123)
        self.assertEqual(self._build(
            xdg=str(xdg), local_dir=str(self.base / "dedicated"),
            inventory_cache=reviewed,
        ).http.ttl, 123)

    def test_no_cache_bypasses_disk_without_changing_reviewed_ttl(self) -> None:
        config = self._build(
            xdg=str(self.base / "xdg"),
            inventory_cache=CacheConfig(ttl=123), no_cache=True,
        )
        self.assertFalse(hasattr(config.http, "disk"))

    def test_no_ttl_still_constructs_persistent_disk_cache(self) -> None:
        """No reviewed TTL and no CLI TTL still produce a persistent
        ``ttl=None`` (infinite) disk cache that survives re-reads."""
        from docker.versioning.cache import CachingHttpTransport, DiskCache
        from docker.versioning.transports import build_transports
        from tests.versioning.support.fake_http import (
            FailingHttpTransport,
            FakeHttpTransport,
        )

        xdg = self.base / "xdg" / "cache"
        os.environ["XDG_CACHE_HOME"] = str(xdg)

        config = build_transports(
            no_cache=False, suggest_mode=False,
        )
        disk = config.http.disk
        self.assertIsNotNone(disk)

        versioning = xdg / "docker-constructor" / "versioning"
        self.assertEqual(_mode(versioning), 0o700)

        delegate = FakeHttpTransport()
        delegate.set("GET", "https://example.test/persist", status=200, body=b"persisted")
        cache = CachingHttpTransport(delegate, ttl=None, disk_cache=disk)
        resp = cache.request("GET", "https://example.test/persist")
        self.assertEqual(resp.body, b"persisted")

        entries = self._entries(versioning)
        self.assertEqual(len(entries), 1)
        self.assertEqual(_mode(entries[0]), 0o600)

        # A fresh instance reads the persistent entry with ttl=None
        # without touching the network.
        disk2 = DiskCache(versioning, ttl=None)
        cache2 = CachingHttpTransport(
            FailingHttpTransport(), ttl=None, disk_cache=disk2,
        )
        resp2 = cache2.request("GET", "https://example.test/persist")
        self.assertEqual(resp2.body, b"persisted")


class TestSuggestModeNoPreparation(_HandoffTestCase):
    def test_suggest_mode_does_not_prepare_root(self) -> None:
        from docker.versioning.transports import build_transports

        xdg = self.base / "xdg" / "cache"
        os.environ["XDG_CACHE_HOME"] = str(xdg)

        config = build_transports(
            no_cache=False,
            suggest_mode=True,
        )

        self.assertIsNone(config.http.disk)
        self.assertFalse(xdg.exists())
        self.assertFalse((self.home / ".cache").exists())
