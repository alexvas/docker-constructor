"""Rust stable channel provider tests."""

from __future__ import annotations

import unittest

from docker.versioning.model import (
    RustChannelSource,
    RustChannelUpdate,
    UpdateTarget,
)
from docker.versioning.providers.base import ProviderContext
from docker.versioning.providers.rust import RustChannelProvider
from tests.versioning.support.fake_git import FakeGitTransport
from tests.versioning.support.fake_http import FakeHttpTransport


class TestRustProvider(unittest.TestCase):
    def setUp(self):
        self.http = FakeHttpTransport()
        self.git = FakeGitTransport()
        self.provider = RustChannelProvider()

    def _ctx(self):
        return ProviderContext(
            http=self.http,
            git=self.git,
            include_prerelease=False,
            tokens={},
        )

    def _target(self, version="1.88.0"):
        return UpdateTarget(
            path="build.stages.toolchain.rust",
            current=version,
            source=RustChannelSource(
                manifest=f"https://static.rust-lang.org/dist/channel-rust-{version}.toml",
            ),
            update=RustChannelUpdate(channel="stable", stable_only=True),
            artifacts={},
        )

    def _set_manifest(self, version):
        body = f"""
[pkg.rust]
version = "{version}"

[renames]
"""
        self.http.set(
            "GET",
            "https://static.rust-lang.org/dist/channel-rust-stable.toml",
            status=200,
            body=body.encode(),
        )

    def test_current(self):
        self._set_manifest("1.88.0")
        result = self.provider.discover(self._target("1.88.0"), self._ctx())
        self.assertIsNotNone(result.candidate)
        self.assertEqual(result.candidate.value, "1.88.0")

    def test_current_with_metadata(self):
        """Real manifests have metadata like '1.97.1 (hash date)'."""
        self._set_manifest("1.88.0 (abcdef01 2025-06-01)")
        result = self.provider.discover(self._target("1.88.0"), self._ctx())
        self.assertIsNotNone(result.candidate)
        self.assertEqual(result.candidate.value, "1.88.0")

    def test_outdated(self):
        self._set_manifest("1.89.0")
        result = self.provider.discover(self._target("1.88.0"), self._ctx())
        self.assertEqual(result.candidate.value, "1.89.0")

    def test_outdated_with_metadata(self):
        """Candidate value must be the numeric part only, not the full string."""
        self._set_manifest("1.89.0 (deadbeef 2025-07-15)")
        result = self.provider.discover(self._target("1.88.0"), self._ctx())
        self.assertEqual(result.candidate.value, "1.89.0")

    def test_malformed_toml(self):
        self.http.set(
            "GET",
            "https://static.rust-lang.org/dist/channel-rust-stable.toml",
            status=200,
            body=b"not [valid toml!!!",
        )
        result = self.provider.discover(self._target("1.88.0"), self._ctx())
        self.assertIsNotNone(result.unavailable_reason)

    def test_missing_version_field(self):
        self.http.set(
            "GET",
            "https://static.rust-lang.org/dist/channel-rust-stable.toml",
            status=200,
            body=b'[pkg]\nnot_version = "1.0.0"\n',
        )
        result = self.provider.discover(self._target("1.88.0"), self._ctx())
        self.assertIsNotNone(result.unavailable_reason)

    def test_http_error(self):
        self.http.set(
            "GET",
            "https://static.rust-lang.org/dist/channel-rust-stable.toml",
            status=404,
        )
        result = self.provider.discover(self._target("1.88.0"), self._ctx())
        self.assertIsNotNone(result.unavailable_reason)

    def test_no_downgrade_when_upstream_stable_older(self):
        """Upstream stable (1.80.0) below selected (1.88.0) → reported CURRENT."""
        self._set_manifest("1.80.0")
        result = self.provider.discover(self._target("1.88.0"), self._ctx())
        self.assertIsNotNone(result.candidate)
        self.assertEqual(result.candidate.value, "1.88.0")

    def test_rejects_malformed_version_no_space_before_extra(self):
        """Rejects ``1.97.1garbage`` — missing whitespace before metadata."""
        self._set_manifest("1.97.1garbage")
        result = self.provider.discover(self._target("1.88.0"), self._ctx())
        self.assertIsNotNone(result.unavailable_reason)
        self.assertIn("unexpected version", result.unavailable_reason.lower())

    def test_rejects_malformed_version_prerelease_suffix(self):
        """Rejects ``1.97.1-rc1`` — stable channel should not have dash suffixes."""
        self._set_manifest("1.97.1-rc1")
        result = self.provider.discover(self._target("1.88.0"), self._ctx())
        self.assertIsNotNone(result.unavailable_reason)
        self.assertIn("unexpected version", result.unavailable_reason.lower())

    def test_rejects_two_component_version(self):
        """Rejects ``1.97`` — only two components, not X.Y.Z."""
        self._set_manifest("1.97")
        result = self.provider.discover(self._target("1.88.0"), self._ctx())
        self.assertIsNotNone(result.unavailable_reason)
        self.assertIn("unexpected version", result.unavailable_reason.lower())
