"""Phase 6 (tasks 6.18 / 6.36) — persistent cache descriptor ownership.

RED before migration: ``DiskCache.set`` handed the fd returned by
``open_private_entry()`` straight to ``os.fdopen(fd, "w")``, relying on
implicit ``closefd=True`` ownership of the file object, then published.  There
was no shared terminal owner, so the descriptor was not tracked and could not
demonstrate terminal-before-close or at-most-once release.  These tests require
the migrated contract: the fd is adopted immediately into an
``OwnedDescriptor``, the file object is opened with ``closefd=False``, and the
owner is released through shared precedence before publication.
"""
from __future__ import annotations

import errno
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from docker.versioning import cache as cache_module
from docker.versioning import cache_storage as cache_storage_module
from docker.versioning.cache import DiskCache
from docker.versioning.providers.base import HttpResponse
from tests.descriptor_ownership_support import (
    file_object_faults,
    recording,
    secondary_diagnostics,
)


class DiskCacheSetOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="phase6-cache-")
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name) / "entries"
        self.directory.mkdir(mode=0o700)
        os.chmod(self.directory, 0o700)
        self.cache = DiskCache(self.directory, ttl=3600)
        self.response = HttpResponse(status=200, headers={}, body=b"hello")

    def _entry(self) -> Path:
        return self.cache._path_for("GET", "https://example.com/a")

    def test_success_releases_owner_before_publication(self) -> None:
        events: list[str] = []
        published: list[str] = []
        real_publish = cache_storage_module.publish_private_entry

        def recording_publish(directory, tmp_name, final_name):
            published.append("publish")
            return real_publish(directory, tmp_name, final_name)

        def record_close(fd, owner):
            events.append("close")
            return None

        with mock.patch.object(
            cache_storage_module, "publish_private_entry", side_effect=recording_publish
        ):
            with recording(cache_module, close_fault=record_close) as rec:
                self.cache.set("GET", "https://example.com/a", self.response)
        rec.assert_terminal_and_at_most_once(self)
        self.assertEqual(events, ["close"])
        self.assertEqual(published, ["publish"])
        self.assertTrue(self._entry().exists())

    def test_json_write_failure_stays_primary_and_prevents_publication(self) -> None:
        close_error = OSError(errno.EIO, "injected descriptor release failure")
        with recording(
            cache_module, close_fault=lambda fd, owner: close_error
        ) as rec:
            with mock.patch.object(
                cache_module.json, "dump", side_effect=ValueError("injected write")
            ):
                with self.assertRaises(ValueError) as ctx:
                    self.cache.set("GET", "https://example.com/a", self.response)
        self.assertEqual(str(ctx.exception), "injected write")
        rec.assert_terminal_and_at_most_once(self)
        self.assertIn(close_error, secondary_diagnostics(ctx.exception))
        self.assertFalse(self._entry().exists())

    def test_descriptor_release_failure_prevents_publication(self) -> None:
        close_error = OSError(errno.EIO, "injected descriptor release failure")
        with recording(
            cache_module, close_fault=lambda fd, owner: close_error
        ) as rec:
            with self.assertRaises(OSError) as ctx:
                self.cache.set("GET", "https://example.com/a", self.response)
        self.assertIs(ctx.exception, close_error)
        rec.assert_terminal_and_at_most_once(self)
        self.assertFalse(self._entry().exists())

    def test_json_write_failure_stays_primary_over_file_and_descriptor_close(self) -> None:
        write_error = ValueError("injected write")
        file_close_error = OSError(errno.EIO, "injected file-object close")
        descriptor_error = OSError(errno.EIO, "injected descriptor release")
        with recording(
            cache_module, close_fault=lambda fd, owner: descriptor_error
        ) as rec:
            with file_object_faults(cache_module, close_fault=file_close_error):
                with mock.patch.object(
                    cache_module.json, "dump", side_effect=write_error
                ):
                    with mock.patch.object(
                        cache_storage_module, "publish_private_entry"
                    ) as publish:
                        with self.assertRaises(ValueError) as ctx:
                            self.cache.set(
                                "GET", "https://example.com/a", self.response
                            )
        self.assertIs(ctx.exception, write_error)
        secondary = secondary_diagnostics(ctx.exception)
        self.assertIn(file_close_error, secondary)
        self.assertIn(descriptor_error, secondary)
        publish.assert_not_called()
        rec.assert_terminal_and_at_most_once(self)
        self.assertFalse(self._entry().exists())

    def test_file_object_close_failure_stays_primary_over_descriptor_close(self) -> None:
        file_close_error = OSError(errno.EIO, "injected file-object close")
        descriptor_error = OSError(errno.EIO, "injected descriptor release")
        with recording(
            cache_module, close_fault=lambda fd, owner: descriptor_error
        ) as rec:
            with file_object_faults(cache_module, close_fault=file_close_error):
                with mock.patch.object(
                    cache_storage_module, "publish_private_entry"
                ) as publish:
                    with self.assertRaises(OSError) as ctx:
                        self.cache.set(
                            "GET", "https://example.com/a", self.response
                        )
        self.assertIs(ctx.exception, file_close_error)
        self.assertIn(descriptor_error, secondary_diagnostics(ctx.exception))
        publish.assert_not_called()
        rec.assert_terminal_and_at_most_once(self)
        self.assertFalse(self._entry().exists())
