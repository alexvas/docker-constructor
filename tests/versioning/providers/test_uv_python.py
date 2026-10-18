"""uv-python provider tests."""

from __future__ import annotations

import json
import unittest

from docker.versioning.model import (
    UpdateTarget,
    UvPythonSource,
    UvPythonUpdate,
)
from docker.versioning.providers.base import ProviderContext
from docker.versioning.providers.uv_python import UvPythonProvider
from tests.versioning.support.fake_git import FakeGitTransport
from tests.versioning.support.fake_http import FakeHttpTransport

_RELEASES_URL = (
    "https://api.github.com/repos/astral-sh/python-build-standalone/"
    "releases?per_page=30"
)


def _make_release(assets):
    """Minimal GitHub release JSON with assets."""
    return {
        "tag_name": "20250115",
        "name": "20250115",
        "prerelease": False,
        "draft": False,
        "assets": assets,
    }


def _make_asset(version):
    """A CPython install-only linux amd64 asset."""
    return {
        "name": (
            f"cpython-{version}+20250115-x86_64-unknown-linux-gnu-install_only.tar.gz"
        ),
        "browser_download_url": (
            f"https://github.com/astral-sh/python-build-standalone/releases/"
            f"download/20250115/cpython-{version}+20250115-"
            "x86_64-unknown-linux-gnu-install_only.tar.gz"
        ),
    }


class TestUvPythonProvider(unittest.TestCase):
    def setUp(self):
        self.http = FakeHttpTransport()
        self.git = FakeGitTransport()
        self.provider = UvPythonProvider()

    def _ctx(self):
        return ProviderContext(
            http=self.http,
            git=self.git,
            include_prerelease=False,
            tokens={},
        )

    def _target(self, version="3.14.6"):
        return UpdateTarget(
            path="build.stages.toolchain.python",
            current=version,
            source=UvPythonSource(implementation="cpython"),
            update=UvPythonUpdate(implementation="cpython", stable_only=True),
            artifacts={},
        )

    def _set_releases(self, releases):
        self.http.set(
            "GET",
            _RELEASES_URL,
            status=200,
            body=json.dumps(releases).encode(),
        )

    def test_current(self):
        self._set_releases(
            [_make_release([_make_asset("3.14.6"), _make_asset("3.13.5")])]
        )
        result = self.provider.discover(self._target("3.14.6"), self._ctx())
        self.assertIsNotNone(result.candidate)
        self.assertEqual(result.candidate.value, "3.14.6")

    def test_outdated(self):
        self._set_releases(
            [_make_release([_make_asset("3.14.6"), _make_asset("3.15.0")])]
        )
        result = self.provider.discover(self._target("3.14.6"), self._ctx())
        self.assertEqual(result.candidate.value, "3.15.0")

    def test_malformed_metadata(self):
        self.http.set(
            "GET",
            _RELEASES_URL,
            status=200,
            body=b"not json",
        )
        result = self.provider.discover(self._target(), self._ctx())
        self.assertIsNotNone(result.unavailable_reason)

    def test_no_cpython_data(self):
        self._set_releases([_make_release([{"name": "pypy-7.3.11+20250115-..."}])])
        result = self.provider.discover(self._target(), self._ctx())
        self.assertIsNotNone(result.unavailable_reason)

    def test_http_error(self):
        self.http.set("GET", _RELEASES_URL, status=500)
        result = self.provider.discover(self._target(), self._ctx())
        self.assertIsNotNone(result.unavailable_reason)

    def test_no_downgrade_when_upstream_max_older(self):
        """Upstream max (3.14.6) below selected (3.15.0) → reported CURRENT."""
        self._set_releases(
            [_make_release([_make_asset("3.14.6"), _make_asset("3.13.5")])]
        )
        result = self.provider.discover(self._target("3.15.0"), self._ctx())
        self.assertIsNotNone(result.candidate)
        self.assertEqual(result.candidate.value, "3.15.0")

    def test_published_at_from_release(self) -> None:
        """uv-python propagates published_at from the winning release."""
        rel1 = _make_release([_make_asset("3.14.6")])
        rel1["published_at"] = "2025-01-15T10:00:00Z"
        rel2 = _make_release([_make_asset("3.15.0")])
        rel2["published_at"] = "2025-06-01T12:00:00Z"
        self._set_releases([rel1, rel2])
        result = self.provider.discover(self._target("3.14.6"), self._ctx())
        self.assertIsNotNone(result.candidate)
        self.assertEqual("3.15.0", result.candidate.value)
        self.assertEqual("2025-06-01T12:00:00Z", result.candidate.published_at)

    def test_missing_published_at_is_none(self) -> None:
        """Release without published_at → published_at is None."""
        self._set_releases(
            [
                _make_release([_make_asset("3.14.6")]),
                _make_release([_make_asset("3.15.0")]),
            ]
        )
        result = self.provider.discover(self._target("3.14.6"), self._ctx())
        self.assertIsNone(result.candidate.published_at)

    def test_current_return_includes_published_at(self) -> None:
        """CURRENT early-return preserves published_at from the release."""
        rel = _make_release([_make_asset("3.14.6")])
        rel["published_at"] = "2025-04-01T00:00:00Z"
        self._set_releases([rel])
        result = self.provider.discover(self._target("3.14.6"), self._ctx())
        self.assertIsNotNone(result.candidate)
        self.assertEqual("3.14.6", result.candidate.value)
        self.assertEqual("2025-04-01T00:00:00Z", result.candidate.published_at)
