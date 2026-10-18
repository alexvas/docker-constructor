"""PyPI provider tests with fake HTTP transport."""

from __future__ import annotations

import json
import unittest

from docker.versioning.model import (
    PyPiSource,
    PyPiUpdate,
    UpdateTarget,
)
from docker.versioning.providers.base import ProviderContext
from docker.versioning.providers.pypi import PyPiProvider
from tests.versioning.support.fake_http import FakeHttpTransport
from tests.versioning.support.fake_git import FakeGitTransport


class TestPyPiProvider(unittest.TestCase):
    def setUp(self):
        self.http = FakeHttpTransport()
        self.git = FakeGitTransport()
        self.provider = PyPiProvider()

    def _ctx(self, include_prerelease=False):
        return ProviderContext(
            http=self.http,
            git=self.git,
            include_prerelease=include_prerelease,
            tokens={},
        )

    def _target(self, version="0.0.61", package="ty", stable_only=True):
        return UpdateTarget(
            path="build.stages.toolchain.ty",
            current=version,
            source=PyPiSource(package=package),
            update=PyPiUpdate(stable_only=stable_only),
            artifacts={},
        )

    def _set_releases(self, package, releases_dict):
        self.http.set(
            "GET",
            f"https://pypi.org/pypi/{package}/json",
            status=200,
            body=json.dumps({"info": {}, "releases": releases_dict}).encode(),
        )

    def test_current_when_no_newer(self):
        self._set_releases(
            "ty",
            {
                "0.0.61": [{"filename": "ty-0.0.61.tar.gz"}],
                "0.0.60": [{"filename": "ty-0.0.60.tar.gz"}],
            },
        )
        result = self.provider.discover(self._target("0.0.61"), self._ctx())
        self.assertIsNotNone(result.candidate)
        self.assertEqual(result.candidate.value, "0.0.61")

    def test_outdated_when_newer(self):
        self._set_releases(
            "ty",
            {
                "0.0.61": [{"filename": "ty-0.0.61.tar.gz"}],
                "0.0.62": [{"filename": "ty-0.0.62.tar.gz"}],
            },
        )
        result = self.provider.discover(self._target("0.0.61"), self._ctx())
        self.assertEqual(result.candidate.value, "0.0.62")

    def test_prerelease_filtered_when_stable_only(self):
        self._set_releases(
            "ty",
            {
                "0.0.61": [{"filename": "ty-0.0.61.tar.gz"}],
                "0.0.62-alpha.1": [{"filename": "ty-0.0.62-alpha.1.tar.gz"}],
            },
        )
        result = self.provider.discover(
            self._target("0.0.61", stable_only=True), self._ctx()
        )
        self.assertEqual(result.candidate.value, "0.0.61")

    def test_yanked_releases_excluded(self):
        self._set_releases(
            "ty",
            {
                "0.0.61": [{"filename": "ty-0.0.61.tar.gz"}],
                "0.0.62": [{"filename": "ty-0.0.62.tar.gz", "yanked": True}],
            },
        )
        result = self.provider.discover(self._target("0.0.61"), self._ctx())
        self.assertEqual(result.candidate.value, "0.0.61")

    def test_package_not_found(self):
        self.http.set(
            "GET",
            "https://pypi.org/pypi/nonexistent/json",
            status=404,
            body=b"{}",
        )
        target = self._target(package="nonexistent")
        result = self.provider.discover(target, self._ctx())
        self.assertIsNotNone(result.unavailable_reason)

    def test_malformed_json(self):
        self.http.set(
            "GET",
            "https://pypi.org/pypi/bad/json",
            status=200,
            body=b"not json",
        )
        target = self._target(package="bad")
        result = self.provider.discover(target, self._ctx())
        self.assertIsNotNone(result.unavailable_reason)

    def test_empty_releases(self):
        self._set_releases("empty", {})
        target = self._target(package="empty")
        result = self.provider.discover(target, self._ctx())
        self.assertIsNotNone(result.skipped_reason)

    def test_no_downgrade_when_upstream_max_older(self):
        """Upstream max (0.0.50) below current (2.0.0) → reported CURRENT."""
        self._set_releases(
            "some-pkg",
            {
                "0.0.50": [{"filename": "a.whl", "yanked": False}],
                "0.0.1": [{"filename": "b.whl", "yanked": False}],
            },
        )
        target = self._target(package="some-pkg", version="2.0.0")
        result = self.provider.discover(target, self._ctx())
        self.assertIsNotNone(result.candidate)
        self.assertEqual(result.candidate.value, "2.0.0")

    def test_published_at_from_upload_time_iso_8601(self) -> None:
        """Earliest valid upload_time_iso_8601 propagated as published_at."""
        self._set_releases(
            "pkg",
            {
                "1.0.0": [
                    {
                        "filename": "a.whl",
                        "upload_time_iso_8601": "2025-06-01T10:30:00Z",
                    },
                    {
                        "filename": "b.whl",
                        "upload_time_iso_8601": "2025-06-01T10:29:00Z",
                    },
                ],
            },
        )
        result = self.provider.discover(
            self._target("0.9.0", package="pkg"), self._ctx()
        )
        self.assertIsNotNone(result.candidate)
        self.assertEqual("1.0.0", result.candidate.value)
        # earliest upload_time_iso_8601 normalised to Z-suffix
        self.assertEqual("2025-06-01T10:29:00Z", result.candidate.published_at)

    def test_upload_time_iso_8601_ignored_when_malformed(self) -> None:
        """Malformed timestamps are skipped; published_at stays None."""
        self._set_releases(
            "pkg",
            {
                "1.0.0": [
                    {"filename": "a.whl", "upload_time_iso_8601": "not-a-timestamp"},
                    {"filename": "b.whl", "upload_time_iso_8601": "also-bad"},
                ],
            },
        )
        result = self.provider.discover(
            self._target("0.9.0", package="pkg"), self._ctx()
        )
        self.assertIsNotNone(result.candidate)
        self.assertEqual("1.0.0", result.candidate.value)
        self.assertIsNone(result.candidate.published_at)

    def test_legacy_upload_time_not_used(self) -> None:
        """Only upload_time_iso_8601 is read; legacy upload_time ignored."""
        self._set_releases(
            "pkg",
            {
                "1.0.0": [
                    {
                        "filename": "a.whl",
                        "upload_time_iso_8601": "2025-01-15T00:00:00Z",
                        "upload_time": "2010-01-01T00:00:00",
                    },
                ],
            },
        )
        result = self.provider.discover(
            self._target("0.9.0", package="pkg"), self._ctx()
        )
        self.assertEqual("2025-01-15T00:00:00Z", result.candidate.published_at)

    def test_current_return_includes_published_at(self) -> None:
        """CURRENT early-return preserves upload_time_iso_8601."""
        self._set_releases(
            "pkg",
            {
                "1.0.0": [
                    {
                        "filename": "a.whl",
                        "upload_time_iso_8601": "2025-03-01T00:00:00Z",
                    },
                ],
            },
        )
        result = self.provider.discover(
            self._target("1.0.0", package="pkg"), self._ctx()
        )
        self.assertIsNotNone(result.candidate)
        self.assertEqual("1.0.0", result.candidate.value)
        self.assertEqual("2025-03-01T00:00:00Z", result.candidate.published_at)

    def test_earliest_instant_uses_parsed_comparison(self) -> None:
        """Fractional vs no-fraction ordering must use parsed datetimes.

        Lexicographic comparison misorders no-fraction (Z) vs .9Z
        because Z (ASCII 90) > . (ASCII 46).  Parsed comparison
        treats no-fraction as equivalent to .0, so the no-fraction
        entry wins when it is earlier.
        """
        self._set_releases(
            "pkg",
            {
                "1.0.0": [
                    {
                        "filename": "later.whl",
                        "upload_time_iso_8601": "2025-06-01T10:30:00.9Z",
                    },
                    {
                        "filename": "earlier.whl",
                        "upload_time_iso_8601": "2025-06-01T10:30:00Z",
                    },
                ],
            },
        )
        result = self.provider.discover(
            self._target("0.9.0", package="pkg"), self._ctx()
        )
        self.assertIsNotNone(result.candidate)
        self.assertEqual("1.0.0", result.candidate.value)
        # No-fraction entry is earlier → wins
        self.assertEqual("2025-06-01T10:30:00Z", result.candidate.published_at)

    def test_over_six_fraction_digits_skipped(self) -> None:
        """>6-digit upload_time_iso_8601 is rejected; next-best wins."""
        self._set_releases(
            "pkg",
            {
                "2.0.0": [
                    {
                        "filename": "nanoseconds.whl",
                        "upload_time_iso_8601": "2025-01-01T00:00:00.1234567Z",
                    },
                    {
                        "filename": "microseconds.whl",
                        "upload_time_iso_8601": "2025-01-01T00:00:00.654321Z",
                    },
                ],
            },
        )
        result = self.provider.discover(
            self._target("1.0.0", package="pkg"), self._ctx()
        )
        self.assertIsNotNone(result.candidate)
        self.assertEqual("2.0.0", result.candidate.value)
        self.assertEqual("2025-01-01T00:00:00.654321Z", result.candidate.published_at)
