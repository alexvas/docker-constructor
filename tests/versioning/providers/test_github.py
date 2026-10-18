"""GitHub Releases provider tests."""

from __future__ import annotations

import json
import unittest

from docker.versioning.model import (
    ArtifactEntry,
    GitHubReleaseSource,
    GitHubReleaseUpdate,
    UpdateTarget,
    UpdateKind,
)
from docker.versioning.providers.base import ProviderContext
from docker.versioning.providers.github import GitHubReleaseProvider
from tests.versioning.support.fake_http import FakeHttpTransport
from tests.versioning.support.fake_git import FakeGitTransport


class TestGitHubProvider(unittest.TestCase):
    def setUp(self):
        self.http = FakeHttpTransport()
        self.git = FakeGitTransport()
        self.provider = GitHubReleaseProvider()

    def _ctx(self):
        return ProviderContext(
            http=self.http,
            git=self.git,
            include_prerelease=False,
            tokens={},
        )

    def _target(
        self,
        version="v1.0.0",
        repo="owner/repo",
        prefix="v",
        platforms=("linux-amd64",),
        artifacts=None,
    ):
        if artifacts is None:
            artifacts = {
                "linux-amd64": ArtifactEntry(
                    url=f"https://github.com/{repo}/releases/download/{version}/tool_amd64.deb",
                    sha256="a" * 64,
                ),
            }
        return UpdateTarget(
            path="build.stages.fd-prebuilt.fd",
            current=version,
            source=GitHubReleaseSource(repository=repo, tag=version),
            update=GitHubReleaseUpdate(
                stable_only=True,
                tag_prefix=prefix,
                required_platforms=tuple(platforms),
            ),
            artifacts=artifacts,
        )

    def _set_releases(self, repo, releases):
        self.http.set(
            "GET",
            f"https://api.github.com/repos/{repo}/releases",
            status=200,
            body=json.dumps(releases).encode(),
            headers={"Accept": "application/vnd.github+json"},
        )

    def test_current_when_no_newer(self):
        self._set_releases(
            "owner/repo",
            [
                {
                    "tag_name": "v1.0.0",
                    "draft": False,
                    "prerelease": False,
                    "assets": [],
                },
            ],
        )
        result = self.provider.discover(self._target("v1.0.0"), self._ctx())
        self.assertIsNotNone(result.candidate)
        self.assertEqual(result.candidate.value, "v1.0.0")

    def test_outdated_when_newer_release(self):
        self._set_releases(
            "owner/repo",
            [
                {
                    "tag_name": "v1.0.0",
                    "draft": False,
                    "prerelease": False,
                    "assets": [],
                },
                {
                    "tag_name": "v2.0.0",
                    "draft": False,
                    "prerelease": False,
                    "assets": [
                        {
                            "name": "tool_amd64.deb",
                            "browser_download_url": "https://example.com/tool_amd64.deb",
                        },
                    ],
                },
            ],
        )
        result = self.provider.discover(self._target("v1.0.0"), self._ctx())
        self.assertEqual(result.candidate.value, "v2.0.0")

    def test_draft_releases_ignored(self):
        self._set_releases(
            "owner/repo",
            [
                {
                    "tag_name": "v1.0.0",
                    "draft": False,
                    "prerelease": False,
                    "assets": [],
                },
                {
                    "tag_name": "v2.0.0",
                    "draft": True,
                    "prerelease": False,
                    "assets": [],
                },
            ],
        )
        result = self.provider.discover(self._target("v1.0.0"), self._ctx())
        self.assertEqual(result.candidate.value, "v1.0.0")

    def test_prerelease_filtered(self):
        self._set_releases(
            "owner/repo",
            [
                {
                    "tag_name": "v1.0.0",
                    "draft": False,
                    "prerelease": False,
                    "assets": [],
                },
                {
                    "tag_name": "v2.0.0-rc1",
                    "draft": False,
                    "prerelease": True,
                    "assets": [],
                },
            ],
        )
        result = self.provider.discover(self._target("v1.0.0"), self._ctx())
        self.assertEqual(result.candidate.value, "v1.0.0")

    def test_v_prefix_stripped_for_comparison(self):
        self._set_releases(
            "owner/repo",
            [
                {
                    "tag_name": "v1.9.0",
                    "draft": False,
                    "prerelease": False,
                    "assets": [],
                },
                {
                    "tag_name": "v1.10.0",
                    "draft": False,
                    "prerelease": False,
                    "assets": [],
                },
            ],
        )
        result = self.provider.discover(self._target("v1.9.0"), self._ctx())
        self.assertEqual(result.candidate.value, "v1.10.0")

    def test_rate_limit(self):
        self.http.set(
            "GET",
            "https://api.github.com/repos/owner/repo/releases",
            status=429,
            body=b'{"message":"API rate limit exceeded"}',
        )
        result = self.provider.discover(self._target("v1.0.0"), self._ctx())
        self.assertIsNotNone(result.unavailable_reason)

    def test_github_token_in_header(self):
        self._set_releases(
            "owner/repo",
            [
                {
                    "tag_name": "v1.0.0",
                    "draft": False,
                    "prerelease": False,
                    "assets": [],
                },
            ],
        )
        ctx = ProviderContext(
            http=self.http,
            git=self.git,
            include_prerelease=False,
            tokens={"GITHUB_TOKEN": "ghp_test123"},
        )
        result = self.provider.discover(self._target("v1.0.0"), self._ctx())
        self.assertEqual(result.candidate.value, "v1.0.0")

    def test_missing_asset(self):
        """When no asset matches, provider still returns candidate but it will be classified as INCOMPLETE by coordinator."""
        self._set_releases(
            "owner/repo",
            [
                {
                    "tag_name": "v1.0.0",
                    "draft": False,
                    "prerelease": False,
                    "assets": [],
                },
                {
                    "tag_name": "v2.0.0",
                    "draft": False,
                    "prerelease": False,
                    "assets": [
                        {
                            "name": "different_name.deb",
                            "browser_download_url": "https://example.com/different.deb",
                        },
                    ],
                },
            ],
        )
        result = self.provider.discover(self._target("v1.0.0"), self._ctx())
        self.assertEqual(result.candidate.value, "v2.0.0")
        # No matching linux-amd64 artifact
        self.assertNotIn("linux-amd64", result.candidate.artifacts)

    def test_checksums_from_asset_file(self):
        """When a SHA256SUMS asset is published, it is downloaded and
        preferred over body-text heuristics."""
        release_body = (
            "# Fake body — this checksum (bbbb) should NOT be used\n"
            "tool_amd64.deb sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        )
        self._set_releases(
            "owner/repo",
            [
                {
                    "tag_name": "v1.0.0",
                    "draft": False,
                    "prerelease": False,
                    "assets": [],
                },
                {
                    "tag_name": "v2.0.0",
                    "draft": False,
                    "prerelease": False,
                    "body": release_body,
                    "assets": [
                        {
                            "name": "SHA256SUMS",
                            "browser_download_url": "https://example.com/SHA256SUMS",
                        },
                        {
                            "name": "tool_amd64.deb",
                            "browser_download_url": "https://example.com/tool_amd64.deb",
                        },
                    ],
                },
            ],
        )
        self.http.set(
            "GET",
            "https://example.com/SHA256SUMS",
            status=200,
            body=(
                b"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc  tool_amd64.deb\n"
                b"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd  other.bin\n"
            ),
        )
        result = self.provider.discover(self._target("v1.0.0"), self._ctx())
        self.assertEqual(result.candidate.value, "v2.0.0")
        art = result.candidate.artifacts.get("linux-amd64")
        self.assertIsNotNone(art)
        # Should use the authoritative checksum from SHA256SUMS, not body text
        self.assertEqual(art.sha256, "c" * 64)

    def test_checksums_file_unavailable_falls_back_to_body(self):
        """When the SHA256SUMS asset cannot be fetched, fall back to body."""
        release_body = (
            "# Release notes\n"
            "tool_amd64.deb sha256:abababababababababababababababababababababababababababababababab\n"
        )
        self._set_releases(
            "owner/repo",
            [
                {
                    "tag_name": "v1.0.0",
                    "draft": False,
                    "prerelease": False,
                    "assets": [],
                },
                {
                    "tag_name": "v2.0.0",
                    "draft": False,
                    "prerelease": False,
                    "body": release_body,
                    "assets": [
                        {
                            "name": "sha256sums.txt",
                            "browser_download_url": "https://example.com/broken",
                        },
                        {
                            "name": "tool_amd64.deb",
                            "browser_download_url": "https://example.com/tool_amd64.deb",
                        },
                    ],
                },
            ],
        )
        self.http.set(
            "GET",
            "https://example.com/broken",
            status=404,
            body=b"Not Found",
        )
        result = self.provider.discover(self._target("v1.0.0"), self._ctx())
        self.assertEqual(result.candidate.value, "v2.0.0")
        art = result.candidate.artifacts.get("linux-amd64")
        self.assertIsNotNone(art)
        # Should fall back to body-text checksum
        self.assertEqual(art.sha256, "ab" + "ab" * 31)

    def test_checksums_file_missing_asset_still_incomplete(self):
        """When SHA256SUMS exists but does not include the matched asset,
        the artifact still lacks a checksum."""
        self._set_releases(
            "owner/repo",
            [
                {
                    "tag_name": "v1.0.0",
                    "draft": False,
                    "prerelease": False,
                    "assets": [],
                },
                {
                    "tag_name": "v2.0.0",
                    "draft": False,
                    "prerelease": False,
                    "assets": [
                        {
                            "name": "SHA256SUMS",
                            "browser_download_url": "https://example.com/SHA256SUMS",
                        },
                        {
                            "name": "tool_amd64.deb",
                            "browser_download_url": "https://example.com/tool_amd64.deb",
                        },
                    ],
                },
            ],
        )
        self.http.set(
            "GET",
            "https://example.com/SHA256SUMS",
            status=200,
            body=b"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee  other.bin\n",
        )
        result = self.provider.discover(self._target("v1.0.0"), self._ctx())
        self.assertEqual(result.candidate.value, "v2.0.0")
        art = result.candidate.artifacts.get("linux-amd64")
        self.assertIsNotNone(art)
        # SHA256SUMS exists but has no entry for tool_amd64.deb
        # No body text either → sha256 should be None (INCOMPLETE)
        self.assertIsNone(art.sha256)

    def test_companion_sha256_file_used_for_checksum(self):
        """When ``<asset>.sha256`` companion file is published, its
        digest is preferred over SHA256SUMS and body text."""
        self._set_releases(
            "owner/repo",
            [
                {
                    "tag_name": "v1.0.0",
                    "draft": False,
                    "prerelease": False,
                    "assets": [],
                },
                {
                    "tag_name": "v2.0.0",
                    "draft": False,
                    "prerelease": False,
                    "body": "tool_amd64.deb sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
                    "assets": [
                        {
                            "name": "SHA256SUMS",
                            "browser_download_url": "https://example.com/SHA256SUMS",
                        },
                        {
                            "name": "tool_amd64.deb",
                            "browser_download_url": "https://example.com/tool_amd64.deb",
                        },
                        {
                            "name": "tool_amd64.deb.sha256",
                            "browser_download_url": "https://example.com/tool_amd64.deb.sha256",
                        },
                    ],
                },
            ],
        )
        # SHA256SUMS file has a DIFFERENT checksum — companion should win
        self.http.set(
            "GET",
            "https://example.com/SHA256SUMS",
            status=200,
            body=b"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb  tool_amd64.deb\n",
        )
        # Companion file has the authoritative digest
        self.http.set(
            "GET",
            "https://example.com/tool_amd64.deb.sha256",
            status=200,
            body=b"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee\n",
        )
        result = self.provider.discover(self._target("v1.0.0"), self._ctx())
        self.assertEqual(result.candidate.value, "v2.0.0")
        art = result.candidate.artifacts.get("linux-amd64")
        self.assertIsNotNone(art)
        # Companion file digest wins over SHA256SUMS and body text
        self.assertEqual(art.sha256, "e" * 64)

    def test_companion_sha256_wins_when_no_checksums_file(self):
        """Without a SHA256SUMS asset, the companion file still provides
        an authoritative digest — release is OUTDATED+applicable, not
        INCOMPLETE."""
        self._set_releases(
            "owner/repo",
            [
                {
                    "tag_name": "v1.0.0",
                    "draft": False,
                    "prerelease": False,
                    "assets": [],
                },
                {
                    "tag_name": "v2.0.0",
                    "draft": False,
                    "prerelease": False,
                    "assets": [
                        {
                            "name": "tool_amd64.deb",
                            "browser_download_url": "https://example.com/tool_amd64.deb",
                        },
                        {
                            "name": "tool_amd64.deb.sha256",
                            "browser_download_url": "https://example.com/tool_amd64.deb.sha256",
                        },
                    ],
                },
            ],
        )
        self.http.set(
            "GET",
            "https://example.com/tool_amd64.deb.sha256",
            status=200,
            body=b"ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff\n",
        )
        result = self.provider.discover(self._target("v1.0.0"), self._ctx())
        self.assertEqual(result.candidate.value, "v2.0.0")
        art = result.candidate.artifacts.get("linux-amd64")
        self.assertIsNotNone(art)
        self.assertEqual(art.sha256, "f" * 64)

    def test_companion_sha256sum_recognised(self):
        """``.sha256sum`` suffix is also recognised as a companion file."""
        self._set_releases(
            "owner/repo",
            [
                {
                    "tag_name": "v1.0.0",
                    "draft": False,
                    "prerelease": False,
                    "assets": [],
                },
                {
                    "tag_name": "v2.0.0",
                    "draft": False,
                    "prerelease": False,
                    "assets": [
                        {
                            "name": "tool_amd64.deb",
                            "browser_download_url": "https://example.com/tool_amd64.deb",
                        },
                        {
                            "name": "tool_amd64.deb.sha256sum",
                            "browser_download_url": "https://example.com/tool_amd64.deb.sha256sum",
                        },
                    ],
                },
            ],
        )
        self.http.set(
            "GET",
            "https://example.com/tool_amd64.deb.sha256sum",
            status=200,
            body=b"abababababababababababababababababababababababababababababababab  tool_amd64.deb\n",
        )
        result = self.provider.discover(self._target("v1.0.0"), self._ctx())
        art = result.candidate.artifacts.get("linux-amd64")
        self.assertIsNotNone(art)
        self.assertEqual(art.sha256, "ab" + "ab" * 31)

    def test_no_downgrade_when_upstream_max_older(self):
        """Upstream max (v1.5.0) below current (v3.0.0) → reported CURRENT."""
        self._set_releases(
            "owner/repo",
            [
                {
                    "tag_name": "v1.5.0",
                    "draft": False,
                    "prerelease": False,
                    "assets": [
                        {
                            "name": "tool_amd64.deb",
                            "browser_download_url": "https://example.com/tool_amd64.deb",
                        },
                    ],
                },
                {
                    "tag_name": "v1.0.0",
                    "draft": False,
                    "prerelease": False,
                    "assets": [],
                },
            ],
        )
        result = self.provider.discover(self._target("v3.0.0"), self._ctx())
        self.assertIsNotNone(result.candidate)
        # Must report CURRENT (v3.0.0), not the older v1.5.0
        self.assertEqual(result.candidate.value, "v3.0.0")

    def test_asset_digest_field_wins_over_companion(self):
        """When the asset has a ``digest`` field in GitHub's real
        ``sha256:<hex>`` format, it is preferred over companion file,
        SHA256SUMS, and body text."""
        self._set_releases(
            "owner/repo",
            [
                {
                    "tag_name": "v1.0.0",
                    "draft": False,
                    "prerelease": False,
                    "assets": [],
                },
                {
                    "tag_name": "v2.0.0",
                    "draft": False,
                    "prerelease": False,
                    "body": "tool_amd64.deb sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
                    "assets": [
                        {
                            "name": "SHA256SUMS",
                            "browser_download_url": "https://example.com/SHA256SUMS",
                        },
                        {
                            "name": "tool_amd64.deb",
                            "browser_download_url": "https://example.com/tool_amd64.deb",
                            "digest": "sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
                        },
                        {
                            "name": "tool_amd64.deb.sha256",
                            "browser_download_url": "https://example.com/tool_amd64.deb.sha256",
                        },
                    ],
                },
            ],
        )
        self.http.set(
            "GET",
            "https://example.com/SHA256SUMS",
            status=200,
            body=b"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa  tool_amd64.deb\n",
        )
        self.http.set(
            "GET",
            "https://example.com/tool_amd64.deb.sha256",
            status=200,
            body=b"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n",
        )
        result = self.provider.discover(self._target("v1.0.0"), self._ctx())
        art = result.candidate.artifacts.get("linux-amd64")
        self.assertIsNotNone(art)
        # Asset digest wins; returned as bare hex after normalisation
        self.assertEqual(art.sha256, "d" * 64)

    def test_asset_digest_sha256_equals_format_normalised(self):
        """The ``sha256=<hex>`` variant (GitHub packages) is also accepted
        and normalised to bare hex."""
        self._set_releases(
            "owner/repo",
            [
                {
                    "tag_name": "v1.0.0",
                    "draft": False,
                    "prerelease": False,
                    "assets": [],
                },
                {
                    "tag_name": "v2.0.0",
                    "draft": False,
                    "prerelease": False,
                    "assets": [
                        {
                            "name": "tool_amd64.deb",
                            "browser_download_url": "https://example.com/tool_amd64.deb",
                            "hash": "sha256=cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
                        },
                    ],
                },
            ],
        )
        result = self.provider.discover(self._target("v1.0.0"), self._ctx())
        art = result.candidate.artifacts.get("linux-amd64")
        self.assertIsNotNone(art)
        self.assertEqual(art.sha256, "c" * 64)

    def test_asset_digest_case_variants_accepted(self):
        """``SHA256:``, ``SHA-256:``, and ``sha-256:`` prefixes are all
        normalised to bare hex."""
        self._set_releases(
            "owner/repo",
            [
                {
                    "tag_name": "v1.0.0",
                    "draft": False,
                    "prerelease": False,
                    "assets": [],
                },
                {
                    "tag_name": "v2.0.0",
                    "draft": False,
                    "prerelease": False,
                    "assets": [
                        {
                            "name": "tool_amd64.deb",
                            "browser_download_url": "https://example.com/tool_amd64.deb",
                            "digest": "SHA256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                        },
                    ],
                },
            ],
        )
        result = self.provider.discover(self._target("v1.0.0"), self._ctx())
        art = result.candidate.artifacts.get("linux-amd64")
        self.assertIsNotNone(art)
        self.assertEqual(art.sha256, "b" * 64)

    def test_bare_hex_digest_still_accepted(self):
        """Bare 64-char hex (no prefix) is still accepted as a valid digest."""
        self._set_releases(
            "owner/repo",
            [
                {
                    "tag_name": "v1.0.0",
                    "draft": False,
                    "prerelease": False,
                    "assets": [],
                },
                {
                    "tag_name": "v2.0.0",
                    "draft": False,
                    "prerelease": False,
                    "assets": [
                        {
                            "name": "tool_amd64.deb",
                            "browser_download_url": "https://example.com/tool_amd64.deb",
                            "sha256": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
                        },
                    ],
                },
            ],
        )
        result = self.provider.discover(self._target("v1.0.0"), self._ctx())
        art = result.candidate.artifacts.get("linux-amd64")
        self.assertIsNotNone(art)
        self.assertEqual(art.sha256, "e" * 64)

    def test_prefixed_digest_with_spaces_normalised(self):
        """``sha256  :  <hex>`` with whitespace around the separator is
        normalised to bare hex."""
        self._set_releases(
            "owner/repo",
            [
                {
                    "tag_name": "v1.0.0",
                    "draft": False,
                    "prerelease": False,
                    "assets": [],
                },
                {
                    "tag_name": "v2.0.0",
                    "draft": False,
                    "prerelease": False,
                    "assets": [
                        {
                            "name": "tool_amd64.deb",
                            "browser_download_url": "https://example.com/tool_amd64.deb",
                            "content_sha256": "sha256:  1111111111111111111111111111111111111111111111111111111111111111",
                        },
                    ],
                },
            ],
        )
        result = self.provider.discover(self._target("v1.0.0"), self._ctx())
        art = result.candidate.artifacts.get("linux-amd64")
        self.assertIsNotNone(art)
        self.assertEqual(art.sha256, "1" * 64)

    def test_non_hex_asset_digest_ignored(self):
        """When the asset digest is not a 64-char hex string it is
        silently ignored — fallback continues."""
        self._set_releases(
            "owner/repo",
            [
                {
                    "tag_name": "v1.0.0",
                    "draft": False,
                    "prerelease": False,
                    "assets": [],
                },
                {
                    "tag_name": "v2.0.0",
                    "draft": False,
                    "prerelease": False,
                    "body": "tool_amd64.deb sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
                    "assets": [
                        {
                            "name": "tool_amd64.deb",
                            "browser_download_url": "https://example.com/tool_amd64.deb",
                            "digest": "not-a-valid-hex",
                        },
                    ],
                },
            ],
        )
        result = self.provider.discover(self._target("v1.0.0"), self._ctx())
        art = result.candidate.artifacts.get("linux-amd64")
        self.assertIsNotNone(art)
        # Falls through to body text
        self.assertEqual(art.sha256, "f" * 64)

    def test_published_at_from_release(self) -> None:
        """GitHub release published_at propagated to candidate."""
        self._set_releases(
            "owner/repo",
            [
                {
                    "tag_name": "v1.0.0",
                    "draft": False,
                    "prerelease": False,
                    "assets": [],
                    "published_at": "2025-01-15T10:30:00Z",
                },
                {
                    "tag_name": "v2.0.0",
                    "draft": False,
                    "prerelease": False,
                    "assets": [],
                    "published_at": "2025-06-01T12:00:00Z",
                },
            ],
        )
        result = self.provider.discover(self._target("v1.0.0"), self._ctx())
        self.assertIsNotNone(result.candidate)
        self.assertEqual("v2.0.0", result.candidate.value)
        self.assertEqual("2025-06-01T12:00:00Z", result.candidate.published_at)

    def test_missing_published_at_is_none(self) -> None:
        """Release without published_at → published_at is None."""
        self._set_releases(
            "owner/repo",
            [
                {
                    "tag_name": "v1.0.0",
                    "draft": False,
                    "prerelease": False,
                    "assets": [],
                },
                {
                    "tag_name": "v2.0.0",
                    "draft": False,
                    "prerelease": False,
                    "assets": [],
                },
            ],
        )
        result = self.provider.discover(self._target("v1.0.0"), self._ctx())
        self.assertIsNone(result.candidate.published_at)

    def test_non_utc_published_at_rejected(self) -> None:
        """Non-UTC published_at (timezone-less) → published_at is None."""
        self._set_releases(
            "owner/repo",
            [
                {
                    "tag_name": "v1.0.0",
                    "draft": False,
                    "prerelease": False,
                    "assets": [],
                },
                {
                    "tag_name": "v2.0.0",
                    "draft": False,
                    "prerelease": False,
                    "assets": [],
                    "published_at": "2025-01-01T00:00:00",
                },
            ],
        )
        result = self.provider.discover(self._target("v1.0.0"), self._ctx())
        self.assertIsNone(result.candidate.published_at)

    def test_minus_00_00_published_at_rejected(self) -> None:
        """-00:00 offset in release → published_at is None."""
        self._set_releases(
            "owner/repo",
            [
                {
                    "tag_name": "v1.0.0",
                    "draft": False,
                    "prerelease": False,
                    "assets": [],
                },
                {
                    "tag_name": "v2.0.0",
                    "draft": False,
                    "prerelease": False,
                    "assets": [],
                    "published_at": "2025-01-01T00:00:00-00:00",
                },
            ],
        )
        result = self.provider.discover(self._target("v1.0.0"), self._ctx())
        self.assertIsNone(result.candidate.published_at)

    def test_current_return_includes_published_at(self) -> None:
        """CURRENT early-return preserves published_at."""
        self._set_releases(
            "owner/repo",
            [
                {
                    "tag_name": "v1.0.0",
                    "draft": False,
                    "prerelease": False,
                    "assets": [],
                    "published_at": "2025-01-15T10:30:00Z",
                },
            ],
        )
        result = self.provider.discover(self._target("v1.0.0"), self._ctx())
        self.assertIsNotNone(result.candidate)
        self.assertEqual("v1.0.0", result.candidate.value)
        self.assertEqual("2025-01-15T10:30:00Z", result.candidate.published_at)
