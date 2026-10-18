"""Docker Registry provider tests."""

from __future__ import annotations

import unittest

from docker.versioning.model import (
    DockerRegistrySource,
    DockerRegistryUpdate,
    UpdateKind,
    UpdateTarget,
)
from docker.versioning.providers.base import ProviderContext
from docker.versioning.providers.docker_registry import DockerRegistryProvider
from tests.versioning.support.fake_git import FakeGitTransport
from tests.versioning.support.fake_http import FakeHttpTransport


class TestDockerRegistryProvider(unittest.TestCase):
    def setUp(self):
        self.http = FakeHttpTransport()
        self.git = FakeGitTransport()
        self.provider = DockerRegistryProvider()
        # The provider probes /v2/ for auth. Reply 200 = no auth required.
        self.http.set(
            "GET",
            "https://registry-1.docker.io/v2/",
            status=200,
            body=b"{}",
        )

    def _ctx(self):
        return ProviderContext(
            http=self.http,
            git=self.git,
            include_prerelease=False,
            tokens={},
        )

    def _target(
        self, tag="24-trixie-slim", registry="docker.io", repository="library/node"
    ):
        return UpdateTarget(
            path="build.stages.base.node",
            current=tag,
            source=DockerRegistrySource(registry=registry, repository=repository),
            update=DockerRegistryUpdate(stable_only=True, track="tag-digest"),
            artifacts={},
        )

    def test_unchanged_digest(self):
        upstream = (
            "sha256:ae91dcc111a68c9d2d81ff2a17bda61be126426176fde6fe7d08ab13b7f50573"
        )
        self.http.set(
            "GET",
            "https://registry-1.docker.io/v2/library/node/manifests/24-trixie-slim",
            status=200,
            headers={"Docker-Content-Digest": upstream},
        )
        result = self.provider.discover(self._target(), self._ctx())
        self.assertIsNotNone(result.candidate)
        self.assertEqual(result.candidate.value, upstream)
        self.assertEqual(result.candidate.kind, UpdateKind.DIGEST_REFRESH)
        # Confirm no requests were made to docker.io — alias was normalized.
        request_urls = [url for _, url, _ in self.http.requests]
        self.assertNotIn("https://docker.io/", " ".join(request_urls))

    def test_changed_digest(self):
        upstream = (
            "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        )
        self.http.set(
            "GET",
            "https://registry-1.docker.io/v2/library/node/manifests/24-trixie-slim",
            status=200,
            headers={"Docker-Content-Digest": upstream},
        )
        result = self.provider.discover(self._target(), self._ctx())
        self.assertIsNotNone(result.candidate)
        self.assertEqual(result.candidate.value, upstream)
        self.assertEqual(result.candidate.kind, UpdateKind.DIGEST_REFRESH)

    def test_no_digest_attribute(self):
        """Provider returns the upstream digest as candidate regardless of
        whether the target carries a stored digest. Comparison is done by
        the coordinator."""
        upstream = (
            "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
        )
        self.http.set(
            "GET",
            "https://registry-1.docker.io/v2/library/node/manifests/24-trixie-slim",
            status=200,
            headers={"Docker-Content-Digest": upstream},
        )
        result = self.provider.discover(self._target(), self._ctx())
        self.assertIsNotNone(result.candidate)
        self.assertEqual(result.candidate.value, upstream)

    # -- non-Docker-Hub and negative coverage ----------------------------

    def test_custom_registry_not_rewritten(self):
        """Non-Docker-Hub registries must not be rewritten."""
        upstream = (
            "sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
        )
        self.http.set(
            "GET",
            "https://registry.example.com/v2/",
            status=200,
            body=b"{}",
        )
        self.http.set(
            "GET",
            "https://registry.example.com/v2/myteam/myimage/manifests/v1.0",
            status=200,
            headers={"Docker-Content-Digest": upstream},
        )
        result = self.provider.discover(
            self._target(
                tag="v1.0",
                registry="registry.example.com",
                repository="myteam/myimage",
            ),
            self._ctx(),
        )
        self.assertIsNotNone(result.candidate)
        self.assertEqual(result.candidate.value, upstream)

    def test_missing_digest_header_unavailable(self):
        """Missing Docker-Content-Digest must report unavailable."""
        self.http.set(
            "GET",
            "https://registry-1.docker.io/v2/library/node/manifests/24-trixie-slim",
            status=200,
            headers={},
        )
        result = self.provider.discover(self._target(), self._ctx())
        self.assertIsNotNone(result.unavailable_reason)
        self.assertIn("Docker Registry", result.unavailable_reason)

    def test_index_docker_io_alias_normalized(self):
        """index.docker.io is also normalized to registry-1.docker.io."""
        upstream = (
            "sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
        )
        self.http.set(
            "GET",
            "https://registry-1.docker.io/v2/",
            status=200,
            body=b"{}",
        )
        self.http.set(
            "GET",
            "https://registry-1.docker.io/v2/library/node/manifests/24-trixie-slim",
            status=200,
            headers={"Docker-Content-Digest": upstream},
        )
        result = self.provider.discover(
            self._target(
                tag="24-trixie-slim",
                registry="index.docker.io",
                repository="library/node",
            ),
            self._ctx(),
        )
        self.assertIsNotNone(result.candidate)
        self.assertEqual(result.candidate.value, upstream)

    # -- lowercase header regression tests ------------------------------

    def test_lowercase_docker_content_digest(self):
        """Digest is found even when Docker-Content-Digest is lowercase."""
        upstream = (
            "sha256:1111111111111111111111111111111111111111111111111111111111111111"
        )
        self.http.set(
            "GET",
            "https://registry-1.docker.io/v2/library/node/manifests/24-trixie-slim",
            status=200,
            headers={"docker-content-digest": upstream},
        )
        result = self.provider.discover(self._target(), self._ctx())
        self.assertIsNotNone(result.candidate)
        self.assertEqual(result.candidate.value, upstream)

    def test_lowercase_www_authenticate_token_flow(self):
        """Token flow succeeds with lowercase www-authenticate header."""
        # Override the default /v2/ probe: return 401 with lowercase headers.
        self.http.set(
            "GET",
            "https://registry-1.docker.io/v2/",
            status=401,
            headers={
                "www-authenticate": (
                    'Bearer realm="https://auth.docker.io/token",'
                    'service="registry.docker.io"'
                ),
            },
        )
        # Token endpoint response.
        self.http.set(
            "GET",
            "https://auth.docker.io/token"
            "?service=registry.docker.io"
            "&scope=repository:library/node:pull",
            status=200,
            body=b'{"token": "fake-bearer-token"}',
        )
        # Manifest response with digest.
        upstream = (
            "sha256:2222222222222222222222222222222222222222222222222222222222222222"
        )
        self.http.set(
            "GET",
            "https://registry-1.docker.io/v2/library/node/manifests/24-trixie-slim",
            status=200,
            headers={"Docker-Content-Digest": upstream},
        )
        result = self.provider.discover(self._target(), self._ctx())
        self.assertIsNotNone(result.candidate)
        self.assertEqual(result.candidate.value, upstream)
