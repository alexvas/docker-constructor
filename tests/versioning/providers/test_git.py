"""Git ref provider tests."""

from __future__ import annotations

import unittest

from docker.versioning.model import (
    GitSource,
    GitRefUpdate,
    UpdateTarget,
    UpdateKind,
)
from docker.versioning.providers.base import ProviderContext
from docker.versioning.providers.git import GitRefProvider
from tests.versioning.support.fake_http import FakeHttpTransport
from tests.versioning.support.fake_git import FakeGitTransport


class TestGitProvider(unittest.TestCase):
    def setUp(self):
        self.http = FakeHttpTransport()
        self.git = FakeGitTransport()
        self.provider = GitRefProvider()

    def _ctx(self):
        return ProviderContext(
            http=self.http,
            git=self.git,
            include_prerelease=False,
            tokens={},
        )

    def _target(
        self, revision="a" * 40, repo="https://github.com/test/repo.git", ref="master"
    ):
        return UpdateTarget(
            path="build.stages.runtime.oh-my-zsh",
            current=revision,
            source=GitSource(repository=repo),
            update=GitRefUpdate(ref=ref),
            artifacts={},
        )

    def test_current(self):
        rev = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        self.git.set("https://github.com/test/repo.git", "master", rev)
        result = self.provider.discover(self._target(rev), self._ctx())
        self.assertIsNotNone(result.candidate)
        self.assertEqual(result.candidate.value, rev)

    def test_revision_update(self):
        old = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        new = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        self.git.set("https://github.com/test/repo.git", "master", new)
        result = self.provider.discover(self._target(old), self._ctx())
        self.assertEqual(result.candidate.value, new)
        self.assertEqual(result.candidate.kind, UpdateKind.REVISION)

    def test_malformed_revision(self):
        self.git.set("https://github.com/test/repo.git", "master", "not-a-sha")
        result = self.provider.discover(self._target(), self._ctx())
        self.assertIsNotNone(result.unavailable_reason)

    def test_transport_failure(self):
        self.git.set("https://github.com/test/repo.git", "master", "a" * 40)
        # Don't set for a different repo
        result = self.provider.discover(
            self._target(repo="https://github.com/other/repo.git"),
            self._ctx(),
        )
        self.assertIsNotNone(result.unavailable_reason)
