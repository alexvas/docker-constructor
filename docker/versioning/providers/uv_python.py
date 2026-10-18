"""uv-managed Python update provider.

Discovers the latest CPython release using the authoritative
interpreter metadata consumed by uv.
"""

from __future__ import annotations

import json
import re

from .base import ProviderContext, ProviderResult, UpdateProvider
from ..constraints import NumericVersion, parse_numeric_version
from ..model import (
    UvPythonSource,
    UvPythonUpdate,
    UpdateCandidate,
    UpdateKind,
    UpdateTarget,
)

_CPYTHON_VERSION_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")

# Authoritative CPython releases from python-build-standalone GitHub API
_UV_PYTHON_RELEASES_URL = (
    "https://api.github.com/repos/astral-sh/python-build-standalone/releases"
)


class UvPythonProvider:
    name = "uv-python"

    def discover(
        self,
        target: UpdateTarget,
        context: ProviderContext,
    ) -> ProviderResult:
        source = target.source
        if not isinstance(source, UvPythonSource):
            return ProviderResult(
                skipped_reason=f"{target.path}: expected uv-python source, got {type(source).__name__}"
            )
        upd = target.update
        if not isinstance(upd, UvPythonUpdate):
            return ProviderResult(
                skipped_reason=f"{target.path}: expected uv-python update, got {type(upd).__name__}"
            )

        if source.implementation != "cpython":
            return ProviderResult(
                skipped_reason=f"{target.path}: unsupported implementation {source.implementation!r}"
            )

        url = f"{_UV_PYTHON_RELEASES_URL}?per_page=30"
        headers = {}
        token = context.tokens.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            resp = context.http.request("GET", url, headers=headers)
        except Exception as exc:
            return ProviderResult(unavailable_reason=f"uv-python: {exc}")

        if resp.status != 200:
            return ProviderResult(
                unavailable_reason=f"uv-python GitHub API returned HTTP {resp.status}"
            )

        try:
            releases = json.loads(resp.body)
        except Exception as exc:
            return ProviderResult(unavailable_reason=f"uv-python: invalid JSON ({exc})")

        if not isinstance(releases, list):
            return ProviderResult(
                unavailable_reason="uv-python: unexpected releases format (expected array)"
            )

        # Release tags are date-based (e.g. "20250115") and not semantic.
        # Each release has assets named like "cpython-3.14.6+20250115-x86_64-unknown-linux-gnu-install_only.tar.gz".
        # Extract the CPython version from asset names.
        _asset_version_re = re.compile(
            r"^cpython-(\d+\.\d+\.\d+)\+\d+-x86_64-unknown-linux-gnu"
        )
        best: tuple[int, int, int] | None = None
        best_raw: str | None = None
        best_published_at_raw: object = None

        # Filter non-release/draft if stable_only (respecting include_prerelease override)
        strict_stable = upd.stable_only and not context.include_prerelease

        for release in releases:
            if not isinstance(release, dict):
                continue
            if strict_stable and (release.get("prerelease") or release.get("draft")):
                continue
            for asset in release.get("assets", []):
                name = asset.get("name", "")
                m = _asset_version_re.match(name)
                if not m:
                    continue
                raw = m.group(1)
                parts = raw.split(".")
                try:
                    version_tuple = (int(parts[0]), int(parts[1]), int(parts[2]))
                except ValueError, IndexError:
                    continue
                if best is None or version_tuple > best:
                    best = version_tuple
                    best_raw = raw
                    best_published_at_raw = release.get("published_at")

        if best_raw is None:
            return ProviderResult(
                unavailable_reason="uv-python: no valid CPython versions found"
            )

        from ..model import _validate_utc_rfc3339

        published_at = _validate_utc_rfc3339(best_published_at_raw)

        current = target.current
        if best_raw == current:
            return ProviderResult(
                candidate=UpdateCandidate(
                    value=current,
                    kind=UpdateKind.VERSION,
                    artifacts={},
                    published_at=published_at,
                )
            )

        # Prevent downgrade: if upstream maximum is older than selected,
        # report CURRENT rather than OUTDATED.
        try:
            cur_nv = parse_numeric_version(current)
            cand_nv = parse_numeric_version(best_raw)
            if cand_nv < cur_nv:
                return ProviderResult(
                    candidate=UpdateCandidate(
                        value=current,
                        kind=UpdateKind.VERSION,
                        artifacts={},
                        published_at=published_at,
                    )
                )
        except Exception:
            pass  # fall through to string comparison

        # published_at was already extracted above (before early returns)
        return ProviderResult(
            candidate=UpdateCandidate(
                value=best_raw,
                kind=UpdateKind.VERSION,
                artifacts={},
                published_at=published_at,
            )
        )
