"""Git ref update provider.

Uses git ls-remote transport to check for upstream revisions.
"""

from __future__ import annotations

import re

from ..model import (
    GitRefUpdate,
    GitSource,
    UpdateCandidate,
    UpdateKind,
    UpdateTarget,
)
from .base import ProviderContext, ProviderResult, UpdateProvider

_GIT_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")


class GitRefProvider:
    name = "git-ref"

    def discover(
        self,
        target: UpdateTarget,
        context: ProviderContext,
    ) -> ProviderResult:
        source = target.source
        if not isinstance(source, GitSource):
            return ProviderResult(
                skipped_reason=f"{target.path}: expected git source, got {type(source).__name__}"
            )
        upd = target.update
        if not isinstance(upd, GitRefUpdate):
            return ProviderResult(
                skipped_reason=f"{target.path}: expected git-ref update, got {type(upd).__name__}"
            )

        repository = source.repository
        ref = upd.ref

        try:
            commit = context.git.resolve_ref(repository, ref)
        except Exception as exc:
            return ProviderResult(unavailable_reason=f"Git: {exc}")

        commit = commit.strip()
        if not _GIT_REVISION_RE.match(commit):
            return ProviderResult(
                unavailable_reason=f"Git: malformed commit hash {commit!r}"
            )

        current = target.current
        if commit == current:
            return ProviderResult(
                candidate=UpdateCandidate(
                    value=current,
                    kind=UpdateKind.REVISION,
                    artifacts={},
                )
            )

        return ProviderResult(
            candidate=UpdateCandidate(
                value=commit,
                kind=UpdateKind.REVISION,
                artifacts={},
            )
        )
