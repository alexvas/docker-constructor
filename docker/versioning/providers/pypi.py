"""PyPI update provider.

Checks https://pypi.org/pypi/ for package version updates.
"""

from __future__ import annotations

import json
from typing import Optional
from urllib.parse import quote

from ..model import (
    PyPiSource,
    PyPiUpdate,
    UpdateCandidate,
    UpdateKind,
    UpdateTarget,
)
from ..semver import SemanticVersion, parse
from .base import ProviderContext, ProviderResult, UpdateProvider


def _pypi_url(package: str) -> str:
    return f"https://pypi.org/pypi/{quote(package, safe='')}/json"


def _latest_stable(releases: dict[str, object]) -> Optional[str]:
    best: Optional[SemanticVersion] = None
    best_raw: Optional[str] = None
    for raw, files in releases.items():
        try:
            sv = parse(raw)
        except ValueError:
            continue
        if sv.is_prerelease:
            continue
        if best is None or sv > best:
            best = sv
            best_raw = raw
    return best_raw


def _latest_any(releases: dict[str, object]) -> Optional[str]:
    best: Optional[SemanticVersion] = None
    best_raw: Optional[str] = None
    for raw, files in releases.items():
        try:
            sv = parse(raw)
        except ValueError:
            continue
        if best is None or sv > best:
            best = sv
            best_raw = raw
    return best_raw


def _is_yanked(files: list[dict[str, object]]) -> bool:
    """A release is yanked if ALL its files have yanked=true."""
    if not files:
        return False
    return all(f.get("yanked", False) for f in files)


class PyPiProvider:
    name = "pypi"

    def discover(
        self,
        target: UpdateTarget,
        context: ProviderContext,
    ) -> ProviderResult:
        source = target.source
        if not isinstance(source, PyPiSource):
            return ProviderResult(
                skipped_reason=f"{target.path}: expected pypi source, got {type(source).__name__}"
            )
        upd = target.update
        if not isinstance(upd, PyPiUpdate):
            return ProviderResult(
                skipped_reason=f"{target.path}: expected pypi update, got {type(upd).__name__}"
            )

        package = source.package
        url = _pypi_url(package)

        headers: dict[str, str] = {}
        token = context.tokens.get("PYPI_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            resp = context.http.request("GET", url, headers=dict(headers))
        except Exception as exc:
            return ProviderResult(unavailable_reason=f"PyPI: {exc}")

        if resp.status == 404:
            return ProviderResult(
                unavailable_reason=f"PyPI: package {package!r} not found"
            )
        if resp.status != 200:
            return ProviderResult(
                unavailable_reason=f"PyPI returned HTTP {resp.status}"
            )

        try:
            data = json.loads(resp.body)
        except Exception as exc:
            return ProviderResult(unavailable_reason=f"PyPI: invalid JSON ({exc})")

        if not isinstance(data, dict):
            return ProviderResult(unavailable_reason="PyPI: unexpected response format")

        releases_data = data.get("releases")
        if not isinstance(releases_data, dict):
            return ProviderResult(
                skipped_reason=f"PyPI: no releases data for {package}"
            )

        # Filter out yanked releases
        releases: dict[str, object] = {}
        for raw, files in releases_data.items():
            if isinstance(files, list) and _is_yanked(files):
                continue
            releases[raw] = files

        if not releases:
            return ProviderResult(
                skipped_reason=f"PyPI: all releases for {package} are yanked or unavailable"
            )

        current = target.current
        selector = (
            _latest_stable
            if (upd.stable_only and not context.include_prerelease)
            else _latest_any
        )
        candidate_raw = selector(releases)
        if candidate_raw is None:
            return ProviderResult(
                skipped_reason=f"PyPI: no matching versions for {package}"
            )

        # Extract earliest upload time for the selected version.
        # Validated UTC RFC 3339 via _validate_utc_rfc3339.
        # Compare parsed datetimes to avoid lexicographic misordering
        # when fractional precision differs (Z vs .9Z).
        from datetime import datetime, timezone

        from ..model import _validate_utc_rfc3339

        pypi_published_at: str | None = None
        best_dt: datetime | None = None
        cand_files = releases.get(candidate_raw)
        if isinstance(cand_files, list):
            for f in cand_files:
                if not isinstance(f, dict):
                    continue
                ut = _validate_utc_rfc3339(f.get("upload_time_iso_8601"))
                if ut is None:
                    continue
                try:
                    dt = datetime.fromisoformat(ut.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if best_dt is None or dt < best_dt:
                    best_dt = dt
                    pypi_published_at = ut

        if candidate_raw == current:
            return ProviderResult(
                candidate=UpdateCandidate(
                    value=current,
                    kind=UpdateKind.VERSION,
                    artifacts={},
                    published_at=pypi_published_at,
                )
            )

        # Prevent downgrade: if upstream maximum is older than selected,
        # report CURRENT rather than OUTDATED.
        try:
            cur_sv = parse(current)
            cand_sv = parse(candidate_raw)
            if cand_sv < cur_sv:
                return ProviderResult(
                    candidate=UpdateCandidate(
                        value=current,
                        kind=UpdateKind.VERSION,
                        artifacts={},
                        published_at=pypi_published_at,
                    )
                )
        except ValueError:
            pass  # fall through to string comparison

        # pypi_published_at was already extracted above (before early returns)
        return ProviderResult(
            candidate=UpdateCandidate(
                value=candidate_raw,
                kind=UpdateKind.VERSION,
                artifacts={},
                published_at=pypi_published_at,
            )
        )
