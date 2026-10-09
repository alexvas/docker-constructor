"""npm registry update provider.

Checks https://registry.npmjs.org/ for package version updates.
"""
from __future__ import annotations

import json
from typing import Optional
from urllib.parse import quote

from .base import HttpTransport, ProviderContext, ProviderResult, UpdateProvider
from ..model import (
    CandidateArtifact,
    NpmSource,
    NpmUpdate,
    PiReleaseSource,
    UpdateCandidate,
    UpdateKind,
    UpdateTarget,
)
from ..integrity import is_valid_integrity
from ..semver import SemanticVersion, parse


def _encode_package(pkg: str) -> str:
    """Percent-encode npm package name for URL."""
    return quote(pkg, safe="")


def _npm_registry_url(package: str) -> str:
    return f"https://registry.npmjs.org/{_encode_package(package)}"


def _latest_stable(versions: dict[str, object]) -> Optional[str]:
    """Return the highest stable semver key from the versions dict."""
    best: Optional[SemanticVersion] = None
    best_raw: Optional[str] = None
    for raw in versions:
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


def _latest_any(versions: dict[str, object]) -> Optional[str]:
    """Return the highest semver key, including prereleases."""
    best: Optional[SemanticVersion] = None
    best_raw: Optional[str] = None
    for raw in versions:
        try:
            sv = parse(raw)
        except ValueError:
            continue
        if best is None or sv > best:
            best = sv
            best_raw = raw
    return best_raw


def _candidate_extension_dist(
    target: UpdateTarget,
    package: str,
    candidate_raw: str,
    versions: dict[str, object],
) -> tuple[dict[str, CandidateArtifact], str | None]:
    """Return ``(artifacts, incomplete_reason)`` for a candidate.

    Only ``runtime.pi-extensions.<name>`` targets receive artifacts: their
    reviewed inventory stores ``artifacts."<version>" = { url, integrity }``
    and the loader requires the default ``version`` to have a matching entry.
    Both values come straight from the registry ``dist`` object.

    When the candidate's ``dist`` is missing, or lacks a non-empty
    ``tarball`` / ``integrity``, or the integrity fails the inventory SRI
    check, or the tarball URL fails the shared npm-tarball identity
    validation (scheme, registry host, package path, and version-suffixed
    filename), artifacts is empty and a human-readable reason is returned so
    the coordinator can mark the candidate INCOMPLETE rather than treating a
    version-only update as a ready replacement.  Build-stage npm tools and
    non-extension targets return ``({}, None)`` and keep version-only
    updates.
    """
    if not target.path.startswith("runtime.pi-extensions."):
        return {}, None
    version_data = versions.get(candidate_raw)
    if not isinstance(version_data, dict):
        return {}, "missing npm version metadata"
    dist = version_data.get("dist")
    if not isinstance(dist, dict):
        return {}, "missing npm tarball URL"
    tarball = dist.get("tarball")
    if not isinstance(tarball, str) or not tarball.strip():
        return {}, "missing npm tarball URL"
    from ..npm_tarball import NpmTarballUrlError, validate
    try:
        validate(tarball, package, candidate_raw)
    except NpmTarballUrlError:
        return {}, f"invalid npm tarball URL {tarball!r}"
    integrity = dist.get("integrity")
    if not isinstance(integrity, str) or not integrity.strip():
        return {}, "missing npm integrity"
    if not is_valid_integrity(integrity):
        return {}, f"invalid npm integrity {integrity!r}"
    return {
        candidate_raw: CandidateArtifact(
            platform=candidate_raw,
            name=package.rsplit("/", 1)[-1],
            url=tarball,
            sha256=None,
            integrity=integrity,
        ),
    }, None


class NpmProvider:
    name = "npm"

    def discover(
        self,
        target: UpdateTarget,
        context: ProviderContext,
    ) -> ProviderResult:
        source = target.source
        # Pi uses its npm package identity for version discovery while its
        # dedicated PiReleaseSource retains the GitHub release metadata used
        # for authoritative installation assets.  Only these two explicit
        # npm-discoverable source models are admitted; every other source
        # type keeps the existing source-type skipped diagnostic.
        if not isinstance(source, (NpmSource, PiReleaseSource)):
            return ProviderResult(
                skipped_reason=f"{target.path}: expected npm source, got {type(source).__name__}"
            )
        upd = target.update
        if not isinstance(upd, NpmUpdate):
            return ProviderResult(
                skipped_reason=f"{target.path}: expected npm update, got {type(upd).__name__}"
            )

        package = source.package
        url = _npm_registry_url(package)

        headers: dict[str, str] = {}
        token = context.tokens.get("NPM_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            resp = context.http.request("GET", url, headers=dict(headers))
        except Exception as exc:
            return ProviderResult(unavailable_reason=f"npm registry: {exc}")

        if resp.status != 200:
            return ProviderResult(
                unavailable_reason=f"npm registry returned HTTP {resp.status}"
            )

        try:
            data = json.loads(resp.body)
        except Exception as exc:
            return ProviderResult(
                unavailable_reason=f"npm registry: invalid JSON ({exc})"
            )

        if not isinstance(data, dict):
            return ProviderResult(
                unavailable_reason="npm registry: unexpected response format"
            )

        versions = data.get("versions")
        if not isinstance(versions, dict) or not versions:
            return ProviderResult(
                skipped_reason=f"npm registry: no versions for {package}"
            )

        current = target.current

        selector = _latest_stable if (upd.stable_only and not context.include_prerelease) else _latest_any
        candidate_raw = selector(versions)
        if candidate_raw is None:
            return ProviderResult(
                skipped_reason=f"npm registry: no matching versions for {package}"
            )

        # Extract authoritative version publication time (validated UTC)
        from ..model import _validate_utc_rfc3339
        npm_time: str | None = None
        times = data.get("time")
        if isinstance(times, dict):
            npm_time = _validate_utc_rfc3339(times.get(candidate_raw))

        if candidate_raw == current:
            return ProviderResult(
                candidate=UpdateCandidate(
                    value=current,
                    kind=UpdateKind.VERSION,
                    artifacts={},
                    published_at=npm_time,
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
                        published_at=npm_time,
                    )
                )
        except ValueError:
            pass  # fall through to string comparison

        # npm_time was already extracted above (before early returns)
        artifacts, incomplete_reason = _candidate_extension_dist(
            target, package, candidate_raw, versions,
        )
        return ProviderResult(
            candidate=UpdateCandidate(
                value=candidate_raw,
                kind=UpdateKind.VERSION,
                artifacts=artifacts,
                published_at=npm_time,
                incomplete_reason=incomplete_reason,
            )
        )
