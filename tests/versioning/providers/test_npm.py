"""npm provider tests with fake HTTP transport."""
from __future__ import annotations

import base64
import json
import unittest

from docker.versioning.model import (
    GitHubReleaseSource,
    NpmSource,
    NpmUpdate,
    PiReleaseSource,
    UpdateTarget,
    UpdateKind,
)
from docker.versioning.providers.base import ProviderContext
from docker.versioning.providers.npm import NpmProvider
from tests.versioning.support.fake_http import FakeHttpTransport, FailingHttpTransport
from tests.versioning.support.fake_git import FakeGitTransport


class TestNpmProvider(unittest.TestCase):
    def setUp(self):
        self.http = FakeHttpTransport()
        self.git = FakeGitTransport()
        self.provider = NpmProvider()

    def _ctx(self, include_prerelease=False):
        return ProviderContext(
            http=self.http,
            git=self.git,
            include_prerelease=include_prerelease,
            tokens={},
        )

    def _target(self, version="1.2.3", package="@scope/pkg", stable_only=True):
        return UpdateTarget(
            path="build.stages.pi-tools.pi",
            current=version,
            source=NpmSource(package=package),
            update=NpmUpdate(stable_only=stable_only),
            artifacts={},
        )

    def _set_versions(self, package, versions_dict):
        from urllib.parse import quote
        encoded = quote(package, safe="")
        self.http.set(
            "GET",
            f"https://registry.npmjs.org/{encoded}",
            status=200,
            body=json.dumps({"versions": versions_dict}).encode(),
        )

    def test_current_when_no_newer_version(self):
        self._set_versions("@scope/pkg", {
            "1.2.3": {"version": "1.2.3"},
            "1.2.2": {"version": "1.2.2"},
        })
        result = self.provider.discover(self._target("1.2.3"), self._ctx())
        self.assertIsNotNone(result.candidate)
        self.assertEqual(result.candidate.value, "1.2.3")

    def test_outdated_when_newer_stable(self):
        self._set_versions("@scope/pkg", {
            "1.2.3": {"version": "1.2.3"},
            "1.3.0": {"version": "1.3.0"},
        })
        result = self.provider.discover(self._target("1.2.3"), self._ctx())
        self.assertEqual(result.candidate.value, "1.3.0")

    def test_prerelease_excluded_when_stable_only(self):
        self._set_versions("@scope/pkg", {
            "1.2.3": {"version": "1.2.3"},
            "1.3.0-beta.1": {"version": "1.3.0-beta.1"},
        })
        result = self.provider.discover(
            self._target("1.2.3", stable_only=True), self._ctx()
        )
        self.assertEqual(result.candidate.value, "1.2.3")

    def test_prerelease_included_with_flag(self):
        self._set_versions("@scope/pkg", {
            "1.2.3": {"version": "1.2.3"},
            "1.3.0-beta.1": {"version": "1.3.0-beta.1"},
        })
        result = self.provider.discover(
            self._target("1.2.3", stable_only=False),
            self._ctx(include_prerelease=True),
        )
        self.assertEqual(result.candidate.value, "1.3.0-beta.1")

    def test_1_10_gt_1_9(self):
        self._set_versions("@scope/pkg", {
            "1.2.3": {"version": "1.2.3"},
            "1.9.0": {"version": "1.9.0"},
            "1.10.0": {"version": "1.10.0"},
        })
        result = self.provider.discover(self._target("1.2.3"), self._ctx())
        self.assertEqual(result.candidate.value, "1.10.0")

    def test_missing_package(self):
        self.http.set(
            "GET", "https://registry.npmjs.org/no-such-pkg",
            status=404, body=b"{}",
        )
        target = self._target(package="no-such-pkg")
        result = self.provider.discover(target, self._ctx())
        self.assertIsNotNone(result.unavailable_reason)

    def test_malformed_json(self):
        self.http.set(
            "GET", "https://registry.npmjs.org/bad-pkg",
            status=200, body=b"not json",
        )
        target = self._target(package="bad-pkg")
        result = self.provider.discover(target, self._ctx())
        self.assertIsNotNone(result.unavailable_reason)

    def test_no_versions(self):
        self.http.set(
            "GET", "https://registry.npmjs.org/empty-pkg",
            status=200,
            body=json.dumps({"versions": {}}).encode(),
        )
        target = self._target(package="empty-pkg")
        result = self.provider.discover(target, self._ctx())
        self.assertIsNotNone(result.skipped_reason)

    def test_token_not_in_result(self):
        self._set_versions("@private/pkg", {
            "1.0.0": {"version": "1.0.0"},
            "2.0.0": {"version": "2.0.0"},
        })
        ctx = ProviderContext(
            http=self.http, git=self.git,
            include_prerelease=False,
            tokens={"NPM_TOKEN": "secret-token-123"},
        )
        target = self._target(package="@private/pkg")
        result = self.provider.discover(target, ctx)
        self.assertEqual(result.candidate.value, "2.0.0")
        # Token should appear in the request Authorization header
        self.assertEqual(len(self.http.requests), 1)
        headers = self.http.requests[0][2]
        self.assertIn("Bearer secret-token-123", headers.get("Authorization", ""))
        # Token should NOT appear in the result (output)
        self.assertNotIn("secret-token-123", str(result))

    def test_scoped_package_url_encoded(self):
        self._set_versions("@earendil-works/pi-coding-agent", {
            "1.0.0": {"version": "1.0.0"},
        })
        target = self._target(package="@earendil-works/pi-coding-agent")
        self.provider.discover(target, self._ctx())
        # Check that the URL was percent-encoded
        url = self.http.requests[0][1]
        self.assertIn("%40", url)
        self.assertIn("%2F", url)

    def _pi_target(
        self,
        version="1.2.3",
        package="@earendil-works/pi-coding-agent",
        stable_only=True,
    ) -> UpdateTarget:
        return UpdateTarget(
            path="build.stages.pi-tools.pi",
            current=version,
            source=PiReleaseSource(
                package=package,
                release_repository="earendil-works/pi",
                release_tag_prefix="v",
            ),
            update=NpmUpdate(stable_only=stable_only),
            artifacts={},
        )

    def test_pi_release_source_queries_encoded_package_and_yields_candidate(
        self,
    ) -> None:
        """A dedicated PiReleaseSource uses its npm package identity."""
        self._set_versions("@earendil-works/pi-coding-agent", {
            "1.2.3": {"version": "1.2.3"},
            "1.3.0": {"version": "1.3.0"},
        })
        result = self.provider.discover(self._pi_target(), self._ctx())
        self.assertIsNone(result.skipped_reason)
        self.assertIsNone(result.unavailable_reason)
        self.assertIsNotNone(result.candidate)
        self.assertEqual("1.3.0", result.candidate.value)
        url = self.http.requests[0][1]
        self.assertIn("%40", url)
        self.assertIn("%2F", url)

    def test_pi_release_source_honors_stable_filter_and_publication_time(
        self,
    ) -> None:
        """Pi follows ordinary npm stable filtering and publication time."""
        import json as _json
        from urllib.parse import quote
        pkg = "@earendil-works/pi-coding-agent"
        encoded = quote(pkg, safe="")
        self.http.set(
            "GET", f"https://registry.npmjs.org/{encoded}",
            status=200,
            body=_json.dumps({
                "versions": {
                    "1.2.3": {"version": "1.2.3"},
                    "1.3.0-beta.1": {"version": "1.3.0-beta.1"},
                },
                "time": {"1.2.3": "2025-03-01T00:00:00Z"},
            }).encode(),
        )
        result = self.provider.discover(
            self._pi_target(stable_only=True), self._ctx()
        )
        self.assertIsNotNone(result.candidate)
        self.assertEqual("1.2.3", result.candidate.value)
        self.assertEqual("2025-03-01T00:00:00Z", result.candidate.published_at)

    def test_unrelated_source_returns_skipped_diagnostic(self) -> None:
        """Non-npm-discoverable source types keep the skipped diagnostic."""
        target = UpdateTarget(
            path="build.stages.rtk-prebuilt.rtk",
            current="1.0.0",
            source=GitHubReleaseSource(repository="rtk-ai/rtk", tag="v1.0.0"),
            update=NpmUpdate(stable_only=True),
            artifacts={},
        )
        result = self.provider.discover(target, self._ctx())
        self.assertIsNotNone(result.skipped_reason)
        self.assertIn("expected npm source", result.skipped_reason)
        self.assertIn("GitHubReleaseSource", result.skipped_reason)
        self.assertEqual(self.http.requests, [])

    def test_network_error(self):
        """Unexpected transport failure produces UNAVAILABLE."""
        # Don't set up any URL — it will fail
        self.http._responses.clear()
        target = self._target(package="pkg")
        result = self.provider.discover(target, self._ctx())
        self.assertIsNotNone(result.unavailable_reason)

    def test_no_downgrade_when_upstream_max_older(self):
        """Upstream max (1.0.0) below current (2.0.0) → reported CURRENT."""
        self._set_versions("some-pkg", {
            "1.0.0": {"version": "1.0.0"},
            "0.9.0": {"version": "0.9.0"},
        })
        target = self._target(package="some-pkg", version="2.0.0")
        result = self.provider.discover(target, self._ctx())
        self.assertIsNotNone(result.candidate)
        # Must report CURRENT, not the older 1.0.0
        self.assertEqual(result.candidate.value, "2.0.0")

    def test_published_at_from_version_time(self) -> None:
        """npm registry version time propagated as published_at."""
        import json as _json
        from urllib.parse import quote
        pkg = "some-pkg"
        encoded = quote(pkg, safe="")
        self.http.set(
            "GET", f"https://registry.npmjs.org/{encoded}",
            status=200,
            body=_json.dumps({
                "versions": {
                    "1.0.0": {"version": "1.0.0"},
                    "2.0.0": {"version": "2.0.0"},
                },
                "time": {
                    "1.0.0": "2025-01-01T00:00:00.000Z",
                    "2.0.0": "2025-06-15T12:30:00.000Z",
                },
            }).encode(),
        )
        target = self._target(package=pkg, version="1.0.0")
        result = self.provider.discover(target, self._ctx())
        self.assertIsNotNone(result.candidate)
        self.assertEqual("2.0.0", result.candidate.value)
        self.assertEqual("2025-06-15T12:30:00.000Z", result.candidate.published_at)

    def test_missing_time_field_yields_none(self) -> None:
        """When the 'time' object is absent, published_at is None."""
        self._set_versions("some-pkg", {
            "1.0.0": {"version": "1.0.0"},
            "2.0.0": {"version": "2.0.0"},
        })
        target = self._target(package="some-pkg", version="1.0.0")
        result = self.provider.discover(target, self._ctx())
        self.assertIsNotNone(result.candidate)
        self.assertIsNone(result.candidate.published_at)

    def test_current_return_includes_published_at(self) -> None:
        """CURRENT early-return preserves published_at from time object."""
        import json as _json
        from urllib.parse import quote
        pkg = "some-pkg"
        encoded = quote(pkg, safe="")
        self.http.set(
            "GET", f"https://registry.npmjs.org/{encoded}",
            status=200,
            body=_json.dumps({
                "versions": {"1.0.0": {"version": "1.0.0"}},
                "time": {"1.0.0": "2025-02-01T00:00:00Z"},
            }).encode(),
        )
        target = self._target(package=pkg, version="1.0.0")
        result = self.provider.discover(target, self._ctx())
        self.assertIsNotNone(result.candidate)
        self.assertEqual("1.0.0", result.candidate.value)
        self.assertEqual("2025-02-01T00:00:00Z", result.candidate.published_at)

    def test_extension_target_receives_tarball_artifact(self) -> None:
        """Pi-extension targets carry version-keyed tarball url + integrity."""
        valid_integrity = "sha512-" + base64.b64encode(b"A" * 64).decode()
        self._set_extension_candidate(
            "@scope/pkg",
            {
                "tarball": "https://registry.npmjs.org/@scope/pkg/-/pkg-2.0.0.tgz",
                "integrity": valid_integrity,
            },
        )
        result = self.provider.discover(self._ext_target("@scope/pkg"), self._ctx())
        self.assertIsNotNone(result.candidate)
        self.assertEqual("2.0.0", result.candidate.value)
        self.assertIsNone(result.candidate.incomplete_reason)
        art = result.candidate.artifacts.get("2.0.0")
        self.assertIsNotNone(art)
        self.assertEqual(art.url, "https://registry.npmjs.org/@scope/pkg/-/pkg-2.0.0.tgz")
        self.assertEqual(art.integrity, valid_integrity)
        self.assertIsNone(art.sha256)

    def _ext_target(self, package: str = "@scope/pkg") -> UpdateTarget:
        return UpdateTarget(
            path="runtime.pi-extensions.pkg-ext",
            current="1.0.0",
            source=NpmSource(package=package),
            update=NpmUpdate(stable_only=True),
            artifacts={},
        )

    def _set_extension_candidate(self, package: str, dist) -> None:
        """Register a registry response with a 1.0.0 current and 2.0.0 candidate.

        ``dist`` may be a dict (merged into the candidate), or ``None`` to
        omit the ``dist`` table entirely.
        """
        from urllib.parse import quote
        encoded = quote(package, safe="")
        candidate: dict = {"version": "2.0.0"}
        if dist is not None:
            candidate["dist"] = dist
        self.http.set(
            "GET", f"https://registry.npmjs.org/{encoded}",
            status=200,
            body=json.dumps({
                "versions": {
                    "1.0.0": {"version": "1.0.0"},
                    "2.0.0": candidate,
                },
            }).encode(),
        )

    def test_extension_missing_dist_is_incomplete(self) -> None:
        self._set_extension_candidate("@scope/pkg", None)
        result = self.provider.discover(self._ext_target(), self._ctx())
        self.assertIsNotNone(result.candidate)
        self.assertEqual(result.candidate.artifacts, {})
        self.assertEqual(result.candidate.incomplete_reason, "missing npm tarball URL")

    def test_extension_missing_tarball_is_incomplete(self) -> None:
        self._set_extension_candidate("@scope/pkg", {"integrity": "sha512-AAAA"})
        result = self.provider.discover(self._ext_target(), self._ctx())
        self.assertEqual(result.candidate.artifacts, {})
        self.assertEqual(result.candidate.incomplete_reason, "missing npm tarball URL")

    def test_extension_missing_integrity_is_incomplete(self) -> None:
        self._set_extension_candidate(
            "@scope/pkg",
            {"tarball": "https://registry.npmjs.org/@scope/pkg/-/pkg-2.0.0.tgz"},
        )
        result = self.provider.discover(self._ext_target(), self._ctx())
        self.assertEqual(result.candidate.artifacts, {})
        self.assertEqual(result.candidate.incomplete_reason, "missing npm integrity")

    def test_extension_invalid_integrity_is_incomplete(self) -> None:
        self._set_extension_candidate(
            "@scope/pkg",
            {
                "tarball": "https://registry.npmjs.org/@scope/pkg/-/pkg-2.0.0.tgz",
                "integrity": "sha512-not-valid-base64!!!",
            },
        )
        result = self.provider.discover(self._ext_target(), self._ctx())
        self.assertEqual(result.candidate.artifacts, {})
        self.assertIn("invalid npm integrity", result.candidate.incomplete_reason)

    def test_extension_integrity_wrong_length_is_incomplete(self) -> None:
        """Valid SRI syntax but wrong decoded length is still incomplete."""
        short = "sha512-" + base64.b64encode(b"A" * 3).decode()
        self._set_extension_candidate(
            "@scope/pkg",
            {
                "tarball": "https://registry.npmjs.org/@scope/pkg/-/pkg-2.0.0.tgz",
                "integrity": short,
            },
        )
        result = self.provider.discover(self._ext_target(), self._ctx())
        self.assertEqual(result.candidate.artifacts, {})
        self.assertIn("invalid npm integrity", result.candidate.incomplete_reason)

    def _valid_integrity(self) -> str:
        return "sha512-" + base64.b64encode(b"A" * 64).decode()

    def test_extension_tarball_url_wrong_package_is_incomplete(self) -> None:
        """Tarball URL for another package → incomplete, no artifact."""
        self._set_extension_candidate(
            "@scope/pkg",
            {
                "tarball": "https://registry.npmjs.org/@scope/other/-/other-2.0.0.tgz",
                "integrity": self._valid_integrity(),
            },
        )
        result = self.provider.discover(self._ext_target("@scope/pkg"), self._ctx())
        self.assertIsNotNone(result.candidate)
        self.assertEqual(result.candidate.artifacts, {})
        self.assertIn("invalid npm tarball URL", result.candidate.incomplete_reason)

    def test_extension_tarball_url_wrong_version_is_incomplete(self) -> None:
        """Tarball URL for a different version → incomplete, no artifact."""
        self._set_extension_candidate(
            "@scope/pkg",
            {
                "tarball": "https://registry.npmjs.org/@scope/pkg/-/pkg-3.0.0.tgz",
                "integrity": self._valid_integrity(),
            },
        )
        result = self.provider.discover(self._ext_target("@scope/pkg"), self._ctx())
        self.assertIsNotNone(result.candidate)
        self.assertEqual(result.candidate.artifacts, {})
        self.assertIn("invalid npm tarball URL", result.candidate.incomplete_reason)

    def test_extension_tarball_url_not_registry_is_incomplete(self) -> None:
        """Tarball URL not on the npm registry host → incomplete, no artifact."""
        self._set_extension_candidate(
            "@scope/pkg",
            {
                "tarball": "https://example.com/@scope/pkg/-/pkg-2.0.0.tgz",
                "integrity": self._valid_integrity(),
            },
        )
        result = self.provider.discover(self._ext_target("@scope/pkg"), self._ctx())
        self.assertIsNotNone(result.candidate)
        self.assertEqual(result.candidate.artifacts, {})
        self.assertIn("invalid npm tarball URL", result.candidate.incomplete_reason)

    def test_build_target_gets_no_artifacts(self) -> None:
        """Build-stage npm tools keep an empty artifact map (version-only)."""
        self._set_versions("build-pkg", {
            "1.0.0": {"version": "1.0.0"},
            "2.0.0": {
                "version": "2.0.0",
                "dist": {
                    "tarball": "https://registry.npmjs.org/build-pkg/-/build-pkg-2.0.0.tgz",
                    "integrity": "sha512-AAAA",
                },
            },
        })
        target = self._target(package="build-pkg", version="1.0.0")
        result = self.provider.discover(target, self._ctx())
        self.assertIsNotNone(result.candidate)
        self.assertEqual(result.candidate.artifacts, {})
        self.assertIsNone(result.candidate.incomplete_reason)
