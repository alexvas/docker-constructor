"""Static-URL provider tests."""
from __future__ import annotations

import unittest

from docker.versioning.errors import InventoryError
from docker.versioning.inventory import load_inventory
from docker.versioning.model import (
    ArtifactEntry,
    CandidateArtifact,
    StaticUrlSource,
    StaticUrlUpdate,
    UpdateTarget,
)
from docker.versioning.providers.base import ProviderContext
from docker.versioning.providers.static_url import StaticUrlProvider
from tests.versioning.support.fake_http import FakeHttpTransport
from tests.versioning.support.fake_git import FakeGitTransport


_ARTIFACT_URL = "https://static.rust-lang.org/rustup/dist/x86_64-unknown-linux-gnu/rustup-init"
_CHECKSUM_URL = _ARTIFACT_URL + ".sha256"
_STORED_SHA256 = "4acc9acc76d5079515b46346a485974457b5a79893cfb01112423c89aeb5aa10"


class TestStaticUrlProvider(unittest.TestCase):
    """Deterministic adapter tests for static-URL published-checksum discovery.

    All tests use FakeHttpTransport — no real network ever reached.
    """

    def setUp(self):
        self.http = FakeHttpTransport()
        self.git = FakeGitTransport()
        self.provider = StaticUrlProvider()

    def _ctx(self):
        return ProviderContext(
            http=self.http, git=self.git,
            include_prerelease=False, tokens={},
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _checksum_body(digest: str, filename: str = "rustup-init") -> bytes:
        return f"{digest}  {filename}\n".encode()

    def _target(self, sha256=_STORED_SHA256, checksum_url=_CHECKSUM_URL):
        return UpdateTarget(
            path="build.stages.toolchain.rust.rustup",
            current=sha256,
            source=StaticUrlSource(checksum_url=checksum_url),
            update=StaticUrlUpdate(stable_only=True),
            artifacts={
                "linux-amd64": ArtifactEntry(url=_ARTIFACT_URL, sha256=sha256),
            },
        )

    def _set_checksum(self, digest: str):
        self.http.set("GET", _CHECKSUM_URL, status=200,
                      body=self._checksum_body(digest))

    # ------------------------------------------------------------------
    # Single platform — published checksum
    # ------------------------------------------------------------------

    def test_current_published_digest_matches(self):
        """Published checksum matches stored → CURRENT."""
        self._set_checksum(_STORED_SHA256)
        target = self._target()
        result = self.provider.discover(target, self._ctx())
        self.assertIsNotNone(result.candidate)
        self.assertEqual(result.candidate.value, _STORED_SHA256)
        self.assertIsNone(result.unavailable_reason)
        self.assertIsNone(result.skipped_reason)

    def test_outdated_published_digest_changed(self):
        """Published checksum differs → OUTDATED with new digest."""
        new_digest = "b" * 64
        self._set_checksum(new_digest)
        target = self._target()
        result = self.provider.discover(target, self._ctx())
        self.assertIsNotNone(result.candidate)
        self.assertEqual(result.candidate.value, new_digest)
        self.assertIsNone(result.unavailable_reason)

    def test_candidate_carries_updated_artifacts(self):
        """Changed checksum → candidate artifacts reflect the new digest."""
        new_digest = "c" * 64
        self._set_checksum(new_digest)
        target = self._target()
        result = self.provider.discover(target, self._ctx())
        artifacts = result.candidate.artifacts
        self.assertIn("linux-amd64", artifacts)
        self.assertEqual(artifacts["linux-amd64"].sha256, new_digest)
        self.assertEqual(artifacts["linux-amd64"].url, _ARTIFACT_URL)

    def test_unavailable_http_error(self):
        """Checksum URL returns 404 → unavailable."""
        self.http.set("GET", _CHECKSUM_URL, status=404)
        result = self.provider.discover(self._target(), self._ctx())
        self.assertIsNotNone(result.unavailable_reason)
        self.assertIn("fail", result.unavailable_reason.lower())

    def test_unavailable_os_error(self):
        """Checksum URL raises OSError → unavailable."""
        self.http.set_exception("GET", _CHECKSUM_URL, OSError("timeout"))
        result = self.provider.discover(self._target(), self._ctx())
        self.assertIsNotNone(result.unavailable_reason)
        self.assertIn("fail", result.unavailable_reason.lower())

    def test_malformed_no_digest(self):
        """Checksum file has no 64-hex-char digest → unavailable."""
        self.http.set("GET", _CHECKSUM_URL, status=200,
                      body=b"not a valid checksum file\n")
        result = self.provider.discover(self._target(), self._ctx())
        self.assertIsNotNone(result.unavailable_reason)
        self.assertIn("fail", result.unavailable_reason.lower())

    def test_space_before_hex(self):
        """Line with leading whitespace still parsed."""
        body = f"  {_STORED_SHA256}  rustup-init\n".encode()
        self.http.set("GET", _CHECKSUM_URL, status=200, body=body)
        result = self.provider.discover(self._target(), self._ctx())
        self.assertIsNotNone(result.candidate)
        self.assertEqual(result.candidate.value, _STORED_SHA256)

    def test_star_prefix_filename(self):
        """sha256sum *filename format accepted."""
        body = f"{_STORED_SHA256} *rustup-init\n".encode()
        self.http.set("GET", _CHECKSUM_URL, status=200, body=body)
        result = self.provider.discover(self._target(), self._ctx())
        self.assertIsNotNone(result.candidate)
        self.assertEqual(result.candidate.value, _STORED_SHA256)

    def test_uppercase_normalised(self):
        """Uppercase hex digest normalised to lowercase."""
        upper = _STORED_SHA256.upper()
        self.http.set("GET", _CHECKSUM_URL, status=200,
                      body=f"{upper}  rustup-init\n".encode())
        result = self.provider.discover(self._target(), self._ctx())
        self.assertIsNotNone(result.candidate)
        self.assertEqual(result.candidate.value, _STORED_SHA256)

    # ------------------------------------------------------------------
    # Skipped
    # ------------------------------------------------------------------

    def test_skipped_empty_artifacts(self):
        target = UpdateTarget(
            path="build.stages.toolchain.rust.rustup",
            current="",
            source=StaticUrlSource(checksum_url=_CHECKSUM_URL),
            update=StaticUrlUpdate(stable_only=True),
            artifacts={},
        )
        result = self.provider.discover(target, self._ctx())
        self.assertIsNotNone(result.skipped_reason)
        self.assertIn("no artifacts", result.skipped_reason.lower())

    # ------------------------------------------------------------------
    # No real-network fallback guarantee
    # ------------------------------------------------------------------

    def test_fake_transport_fails_unexpected_requests(self):
        """FakeHttpTransport raises AssertionError on unregistered URLs.

        This guarantees that if a test were to accidentally reach a real
        URL, it would fail immediately instead of silently hitting the
        network.
        """
        target = UpdateTarget(
            path="build.stages.toolchain.rust.rustup",
            current="",
            source=StaticUrlSource(checksum_url="https://unregistered.example.com/CHECKSUM"),
            update=StaticUrlUpdate(stable_only=True),
            artifacts={
                "linux-amd64": ArtifactEntry(
                    url="https://unregistered.example.com/artifact",
                    sha256="a" * 64,
                ),
            },
        )
        with self.assertRaises(AssertionError):
            self.provider.discover(target, self._ctx())


# ---------------------------------------------------------------------------
# Multi-artifact rejection
# ---------------------------------------------------------------------------

_MULTI_ARTIFACT_TOML = """\
schema = 1

[build.stages.base.node]
tag = "24-trixie-slim"
digest = "sha256:ae91dcc111a68c9d2d81ff2a17bda61be126426176fde6fe7d08ab13b7f50573"
node_version = "24.18.0"
npm_version = "11.16.0"

[build.stages.base.node.source]
type = "docker-registry"
registry = "docker.io"
repository = "library/node"

[build.stages.base.node.update]
provider = "docker-registry"
stable_only = true
track = "tag-digest"

[build.stages.toolchain.rust]
version = "1.88.0"
profile = "minimal"
components = ["rustfmt", "clippy"]

[build.stages.toolchain.rust.source]
type = "rust-channel"
manifest = "https://static.rust-lang.org/dist/channel-rust-1.88.0.toml"

[build.stages.toolchain.rust.update]
provider = "rust-channel"
channel = "stable"
stable_only = true

[build.stages.toolchain.rust.rustup.source]
type = "static-url"
checksum_url = "https://static.rust-lang.org/rustup/dist/x86_64-unknown-linux-gnu/rustup-init.sha256"

[build.stages.toolchain.rust.rustup.update]
provider = "static-url"
stable_only = true

[build.stages.toolchain.rust.rustup.artifacts.linux-amd64]
url = "https://static.rust-lang.org/rustup/dist/x86_64-unknown-linux-gnu/rustup-init"
sha256 = "4acc9acc76d5079515b46346a485974457b5a79893cfb01112423c89aeb5aa10"

[build.stages.toolchain.rust.rustup.artifacts.linux-arm64]
url = "https://static.rust-lang.org/rustup/dist/aarch64-unknown-linux-gnu/rustup-init"
sha256 = "4acc9acc76d5079515b46346a485974457b5a79893cfb01112423c89aeb5aa11"

[build.stages.toolchain.uv]
version = "0.11.29"

[build.stages.toolchain.uv.source]
type = "github-release"
repository = "astral-sh/uv"
tag = "0.11.29"

[build.stages.toolchain.uv.artifacts.linux-amd64]
url = "https://github.com/astral-sh/uv/releases/download/0.11.29/uv-x86_64-unknown-linux-gnu.tar.gz"
sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

[build.stages.toolchain.uv.update]
provider = "github-release"
stable_only = true
required_platforms = ["linux-amd64"]

[build.stages.toolchain.python]
version = "3.14.6"

[build.stages.toolchain.python.source]
type = "uv-python"
implementation = "cpython"

[build.stages.toolchain.python.update]
provider = "uv-python"
implementation = "cpython"
stable_only = true

[build.stages.toolchain.python.override]
constraint = ">=3.14.6"
allow_prerelease = false
scheme = "numeric"

[build.stages.toolchain.ty]
version = "0.0.61"

[build.stages.toolchain.ty.source]
type = "pypi"
package = "ty"

[build.stages.toolchain.ty.update]
provider = "pypi"
stable_only = true

[build.stages.rtk-prebuilt.rtk]
version = "v0.43.0"

[build.stages.rtk-prebuilt.rtk.source]
type = "github-release"
repository = "rtk-ai/rtk"
tag = "v0.43.0"

[build.stages.rtk-prebuilt.rtk.artifacts.linux-amd64]
url = "https://github.com/rtk-ai/rtk/releases/download/v0.43.0/rtk_amd64.deb"
sha256 = "eb571d784b3269521722ebe2f0dc2409e89da6bd70bf097ddb21e9d4b3b240b9"

[build.stages.rtk-prebuilt.rtk.update]
provider = "github-release"
stable_only = true
tag_prefix = "v"
required_platforms = ["linux-amd64"]

[build.stages.fd-prebuilt.fd]
version = "v10.4.2"

[build.stages.fd-prebuilt.fd.source]
type = "github-release"
repository = "sharkdp/fd"
tag = "v10.4.2"

[build.stages.fd-prebuilt.fd.artifacts.linux-amd64]
url = "https://github.com/sharkdp/fd/releases/download/v10.4.2/fd_10.4.2_amd64.deb"
sha256 = "0e44eb5fca93f09bc6f5430b90acdf44c8e069d0a903700aeb4820629337b67b"

[build.stages.fd-prebuilt.fd.update]
provider = "github-release"
stable_only = true
tag_prefix = "v"
required_platforms = ["linux-amd64"]

[build.stages.pi-tools.pi]
version = "0.80.10"

[build.stages.pi-tools.pi.source]
type = "npm"
package = "@earendil-works/pi-coding-agent"

[build.stages.pi-tools.pi.update]
provider = "npm"
stable_only = true

[build.stages.openspec-tools.openspec]
version = "1.6.0"

[build.stages.openspec-tools.openspec.source]
type = "npm"
package = "@fission-ai/openspec"

[build.stages.openspec-tools.openspec.update]
provider = "npm"
stable_only = true

[build.stages.runtime.oh-my-zsh]
revision = "70ad5e3df8f7bed68aa6672029496926e632aedd"

[build.stages.runtime.oh-my-zsh.source]
type = "git"
repository = "https://github.com/ohmyzsh/ohmyzsh.git"

[build.stages.runtime.oh-my-zsh.update]
provider = "git-ref"
ref = "master"

[runtime.pi-extensions.pi-read]
version = "0.2.0"

[runtime.pi-extensions.pi-read.source]
type = "npm"
package = "@arcanemachine/pi-read"

[runtime.pi-extensions.pi-read.update]
provider = "npm"
stable_only = true
"""


class TestStaticUrlMultiArtifactRejection(unittest.TestCase):
    """Inventory-level rejection of static-url entries with >1 artifacts."""

    def test_multi_artifact_rejected(self):
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write(_MULTI_ARTIFACT_TOML)
            f.flush()
            from pathlib import Path
            from docker.versioning.inventory import load_inventory
            try:
                with self.assertRaises(InventoryError) as ctx:
                    load_inventory(Path(f.name))
                self.assertEqual(
                    ctx.exception.field,
                    "build.stages.toolchain.rust.rustup",
                )
            finally:
                import os
                os.unlink(f.name)
