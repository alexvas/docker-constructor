"""Fake Git transport for provider tests.

Maps (repository, ref) pairs to commit hashes.
Records all calls for assertion.  Fails on unexpected calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FakeGitTransport:
    """A fake git transport that returns pre-configured commit hashes."""

    _refs: dict[tuple[str, str], str] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)

    def set(self, repository: str, ref: str, commit: str) -> None:
        self._refs[(repository, ref)] = commit

    def resolve_ref(self, repository: str, ref: str) -> str:
        self.calls.append((repository, ref))
        key = (repository, ref)
        if key not in self._refs:
            raise AssertionError(
                f"Unexpected git ref resolution: {repository} {ref}\n"
                f"Known: {list(self._refs.keys())}"
            )
        return self._refs[key]


class FailingGitTransport:
    """A git transport that fails on any call — for regression tests."""

    def resolve_ref(self, repository: str, ref: str) -> str:
        raise AssertionError(f"Unexpected git ref resolution: {repository} {ref}")
