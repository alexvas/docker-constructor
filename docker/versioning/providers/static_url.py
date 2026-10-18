"""Static-URL update provider.

Discovers content drift for version-independent static URLs by fetching a
published checksum file and comparing its digest against the stored values.

The source must declare a ``checksum_url`` pointing to an authoritative
``sha256sum``-format file.  The provider never downloads the full artifact
body — it only fetches the lightweight checksum file.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..model import (
    CandidateArtifact,
    StaticUrlSource,
    UpdateKind,
)
from .base import (
    ProviderResult,
    UpdateCandidate,
)

if TYPE_CHECKING:
    from ..model import UpdateTarget
    from .base import ProviderContext


class StaticUrlProvider:
    """Provider for ``static-url`` sources — version-independent URLs whose
    integrity is verified against a published checksum file.

    The provider fetches the ``checksum_url`` declared in the source,
    parses the hex digest, and compares it against the single stored
    platform artifact digest.  Only single-platform entries are supported
    (each architecture has a different checksum).
    """

    name = "static-url"

    def discover(
        self,
        target: UpdateTarget,
        context: ProviderContext,
    ) -> ProviderResult:
        if not target.artifacts:
            return ProviderResult(
                skipped_reason="no artifacts to verify for static-url target"
            )

        source = target.source
        if not isinstance(source, StaticUrlSource):
            return ProviderResult(
                unavailable_reason="static-url source metadata missing checksum_url"
            )

        published = self._fetch_checksum_digest(source.checksum_url, context)
        if published is None:
            return ProviderResult(
                unavailable_reason=f"failed to fetch or parse checksum at {source.checksum_url}"
            )

        # Compare the published digest against every stored platform artifact.
        stored_digests = {p: a.sha256 for p, a in target.artifacts.items()}

        if all(s == published for s in stored_digests.values()):
            return ProviderResult(
                candidate=UpdateCandidate(
                    value=published,
                    kind=UpdateKind.DIGEST_REFRESH,
                    artifacts={},
                ),
            )

        # Digest changed — report new candidate with updated artifacts
        return ProviderResult(
            candidate=UpdateCandidate(
                value=published,
                kind=UpdateKind.DIGEST_REFRESH,
                artifacts={
                    p: CandidateArtifact(
                        platform=p,
                        name=p,
                        url=a.url,
                        sha256=published,
                    )
                    for p, a in target.artifacts.items()
                },
            ),
        )

    @staticmethod
    def _fetch_checksum_digest(url: str, context: ProviderContext) -> str | None:
        """Fetch a checksum file and extract the first hex digest from it."""
        try:
            resp = context.http.request("GET", url, headers={})
        except OSError:
            return None
        if resp.status != 200:
            return None
        return _parse_sha256_checksum(resp.body)


# ---------------------------------------------------------------------------
# Checksum parsing
# ---------------------------------------------------------------------------


def _parse_sha256_checksum(raw: bytes) -> str | None:
    """Extract the first 64-character hex digest from a checksum file.

    Supports standard ``sha256sum`` format::

        4acc9acc...  filename
        4acc9acc... *filename
        4acc9acc...  filename1
        4acc9acc... *filename2

    Also tolerates bare hex (single 64-hex-char line with no filename).
    """
    import re as _re

    text = raw.decode("utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _re.match(r"^([0-9a-fA-F]{64})\b", line)
        if m:
            return m.group(1).lower()
    return None
