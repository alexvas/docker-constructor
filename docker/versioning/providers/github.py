"""GitHub Releases update provider.

Queries https://api.github.com/repos/<repo>/releases for latest stable release
with required platform assets and published checksums.
"""

from __future__ import annotations

import json
import re
from typing import Optional

from ..model import (
    CandidateArtifact,
    GitHubReleaseSource,
    GitHubReleaseUpdate,
    UpdateCandidate,
    UpdateKind,
    UpdateTarget,
)
from ..semver import SemanticVersion, parse
from .base import ProviderContext, ProviderResult

# Strip v-prefix for semver parsing
_SEMVER_TAG_RE = re.compile(r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(-.*)?$")

# Match checksum lines: <64 hex>  <filename>  (from SHA256SUMS-style files)
# or <64 hex> *<filename> (binary mode)
_CHECKSUM_LINE_RE = re.compile(r"^([0-9a-fA-F]{64})\s+[*\s]?(.+?)\s*$")

# Recognise asset names that are checksum files
_CHKSUM_ASSET_RE = re.compile(
    r"(?i)^(?:SHA256SUMS?|sha256sums?|checksums?[-_]?sha256)(?:\.txt)?$"
)

# Match inline checksum mentions in release body:
#   sha256: <hex>  or  sha256(<hex>)  or  asset-name  sha256 <hex>
_CHECKSUM_INLINE_RE = re.compile(
    r"(?:sha256|SHA-?256)\s*[:=]\s*([0-9a-fA-F]{64})",
)

# Asset name to look for in body near a checksum
_CHECKSUM_NEAR_ASSET_RE_TEMPLATE = re.escape


def _strip_prefix(tag: str, prefix: str) -> str:
    if prefix and tag.startswith(prefix):
        return tag[len(prefix) :]
    return tag


def _parse_tag_semver(tag: str, prefix: str) -> Optional[SemanticVersion]:
    """Parse a tag like v1.2.3 or 1.2.3 into a SemanticVersion."""
    stripped = _strip_prefix(tag, prefix)
    try:
        return parse(stripped)
    except ValueError:
        return None


def _is_prerelease(release: dict[str, object]) -> bool:
    return bool(release.get("prerelease", False))


def _is_draft(release: dict[str, object]) -> bool:
    return bool(release.get("draft", False))


def _best_release(
    releases: list[dict[str, object]],
    prefix: str,
    stable_only: bool,
) -> tuple[Optional[str], Optional[dict[str, object]]]:
    """Pick the highest semver release that satisfies stable_only policy."""
    best_sv: Optional[SemanticVersion] = None
    best_tag: Optional[str] = None
    best_data: Optional[dict[str, object]] = None

    for rel in releases:
        if _is_draft(rel):
            continue
        if stable_only and _is_prerelease(rel):
            continue
        tag = rel.get("tag_name")
        if not isinstance(tag, str):
            continue
        sv = _parse_tag_semver(tag, prefix)
        if sv is None:
            continue
        if best_sv is None or sv > best_sv:
            best_sv = sv
            best_tag = tag
            best_data = rel

    return best_tag, best_data


def _find_asset(
    release: dict[str, object],
    current_url: str,
    current_version: str,
    candidate_version: str,
    prefix: str,
) -> Optional[dict[str, object]]:
    """Find an asset whose name matches the expected pattern."""
    assets = release.get("assets")
    if not isinstance(assets, list):
        return None

    # Extract basename from current URL
    current_basename = current_url.rsplit("/", 1)[-1]

    # Build candidate basenames:
    # 1. Replace current version with candidate version
    # 2. Replace stripped version with stripped candidate version
    candidates: set[str] = set()
    candidates.add(current_basename.replace(current_version, candidate_version))

    v_curr = _strip_prefix(current_version, prefix)
    v_cand = _strip_prefix(candidate_version, prefix)
    if v_curr != current_version or v_cand != candidate_version:
        candidates.add(current_basename.replace(v_curr, v_cand))

    matching = []
    for asset in assets:
        name = asset.get("name")
        if isinstance(name, str) and name in candidates:
            matching.append(asset)

    if len(matching) == 1:
        return matching[0]
    return None


def _extract_checksum_for_asset(
    asset_name: str,
    release_body: str,
) -> Optional[str]:
    """Extract a SHA-256 checksum tied to *asset_name* from release body.

    Handles two common patterns:

    1. **SHA256SUMS block** — body contains lines like:
       ``<64 hex>  <filename>``

    2. **Inline mention** — body contains the asset name near a
       ``sha256: <hex>`` token.
    """
    # Pattern 1: checksum-line style (may use literal * prefix)
    escaped_name = re.escape(asset_name)
    line_pattern = re.compile(
        r"^([0-9a-fA-F]{64})\s+\*?" + escaped_name + r"\s*$",
        re.MULTILINE,
    )
    m = line_pattern.search(release_body)
    if m:
        return m.group(1).lower()

    # Pattern 2: asset name mentioned in same paragraph as sha256: <hex>
    paragraphs = re.split(r"\n\s*\n", release_body)
    sha_pat = _CHECKSUM_INLINE_RE
    for para in paragraphs:
        if asset_name not in para:
            continue
        cm = sha_pat.search(para)
        if cm:
            return cm.group(1).lower()

    return None


def _resolve_checksums(
    assets: list[dict[str, object]],
    context: ProviderContext,
) -> Optional[dict[str, str]]:
    """Download and parse a checksum-file asset if present in *assets*.

    Returns a ``{filename: sha256}`` dict, or ``None`` when no checksum
    file is found or it cannot be fetched / parsed.
    """
    for asset in assets:
        name = asset.get("name")
        if not isinstance(name, str):
            continue
        if not _CHKSUM_ASSET_RE.match(name):
            continue
        url = asset.get("browser_download_url")
        if not isinstance(url, str):
            continue

        try:
            resp = context.http.request("GET", url, headers={})
        except Exception:
            return None

        if resp.status != 200:
            return None

        try:
            text = resp.body.decode("utf-8", errors="replace")
        except Exception:
            return None

        result: dict[str, str] = {}
        for line in text.splitlines():
            m = _CHECKSUM_LINE_RE.match(line)
            if m:
                digest = m.group(1).lower()
                fname = m.group(2).strip()
                if fname.startswith("*"):
                    fname = fname[1:]
                result[fname] = digest
        return result if result else None

    return None


# Regex for per-asset companion checksum files.
# Matches names like ``tool_amd64.deb.sha256`` or ``tool_amd64.deb.sha256sum``.
_COMPANION_RE = re.compile(r"(?i)\.sha256(sum)?(\.txt)?$")

# Recognised field names for a per-asset digest published by the
# release API alongside the ``name`` / ``browser_download_url`` fields.
_ASSET_DIGEST_FIELDS = ("digest", "sha256", "content_sha256", "checksum", "hash")

# Any 64 lower-case hex string is accepted as a valid SHA-256.
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# GitHub and other registries may publish digests in ``sha256:<hex>``
# or ``sha256=<hex>`` format.  Extract the bare hex for uniform storage.
_PREFIXED_SHA256_RE = re.compile(
    r"^(?:sha256|SHA256|SHA-256|sha-256)\s*[:=]\s*([0-9a-f]{64})$"
)


def _normalise_digest(raw: str) -> str | None:
    """Return a bare 64-hex SHA-256 string from *raw*, or ``None``.

    Accepts both bare hex and ``sha256:<hex>`` / ``sha256=<hex>``
    prefixed forms returned by real registries.
    """
    if _SHA256_RE.match(raw):
        return raw
    m = _PREFIXED_SHA256_RE.match(raw)
    if m:
        return m.group(1)
    return None


def _extract_asset_digest(asset: dict[str, object]) -> str | None:
    """Return a normalised bare SHA-256 digest from *asset* metadata,
    if present."""
    for field in _ASSET_DIGEST_FIELDS:
        raw = asset.get(field)
        if isinstance(raw, str):
            digest = _normalise_digest(raw)
            if digest is not None:
                return digest
    return None


def _resolve_asset_companion_checksum(
    asset_name: str,
    assets: list[dict[str, object]],
    context: ProviderContext,
) -> Optional[str]:
    """Resolve a checksum from a companion ``<asset>.sha256`` file in *assets*.

    Many projects publish per-asset checksums alongside each binary,
    e.g. ``mytool_1.0_amd64.deb.sha256``.  This is more authoritative
    than a SHA256SUMS file or body-text heuristics because it is tied
    directly to a single artifact.
    """
    for asset in assets:
        name = asset.get("name")
        if not isinstance(name, str):
            continue
        # Companion name: starts with asset_name, ends with .sha256 / .sha256sum
        if not name.startswith(asset_name):
            continue
        suffix = name[len(asset_name) :]
        if not _COMPANION_RE.match(suffix):
            continue

        url = asset.get("browser_download_url")
        if not isinstance(url, str):
            continue

        try:
            resp = context.http.request("GET", url, headers={})
        except Exception:
            return None

        if resp.status != 200:
            return None

        try:
            text = resp.body.decode("utf-8", errors="replace")
        except Exception:
            return None

        # Companion files typically contain a single bare hex digest, or
        # a two-column line like ``<hex>  <filename>``.
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return None

        # Single-line bare hex
        first = lines[0]
        if re.fullmatch(r"[0-9a-fA-F]{64}", first):
            return first.lower()

        # Multi-column format (first word is the hex digest)
        m = _CHECKSUM_LINE_RE.match(first)
        if m:
            return m.group(1).lower()

    return None


class GitHubReleaseProvider:
    name = "github-release"

    def discover(
        self,
        target: UpdateTarget,
        context: ProviderContext,
    ) -> ProviderResult:
        source = target.source
        if not isinstance(source, GitHubReleaseSource):
            return ProviderResult(
                skipped_reason=(
                    f"{target.path}: expected github-release source, "
                    f"got {type(source).__name__}"
                )
            )
        upd = target.update
        if not isinstance(upd, GitHubReleaseUpdate):
            return ProviderResult(
                skipped_reason=(
                    f"{target.path}: expected github-release update, "
                    f"got {type(upd).__name__}"
                )
            )

        repo = source.repository
        prefix = upd.tag_prefix
        stable_only = upd.stable_only and not context.include_prerelease
        required = upd.required_platforms

        url = f"https://api.github.com/repos/{repo}/releases"

        headers: dict[str, str] = {"Accept": "application/vnd.github+json"}
        token = context.tokens.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            resp = context.http.request("GET", url, headers=headers)
        except Exception as exc:
            return ProviderResult(unavailable_reason=f"GitHub API: {exc}")

        if resp.status in (403, 429):
            return ProviderResult(
                unavailable_reason=(f"GitHub API rate-limited (HTTP {resp.status})")
            )
        if resp.status != 200:
            return ProviderResult(
                unavailable_reason=f"GitHub API returned HTTP {resp.status}"
            )

        try:
            data = json.loads(resp.body)
        except Exception as exc:
            return ProviderResult(
                unavailable_reason=f"GitHub API: invalid JSON ({exc})"
            )

        if not isinstance(data, list):
            return ProviderResult(
                unavailable_reason="GitHub API: expected array of releases"
            )

        best_tag, best_rel = _best_release(data, prefix, stable_only)
        if best_tag is None or best_rel is None:
            return ProviderResult(
                skipped_reason=(f"GitHub API: no suitable releases for {repo}")
            )

        current = target.current
        from ..model import _validate_utc_rfc3339

        raw_published_at = best_rel.get("published_at")
        published_at = _validate_utc_rfc3339(
            raw_published_at if isinstance(raw_published_at, str) else None
        )

        if best_tag == current:
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
        cur_sv = _parse_tag_semver(current, prefix)
        best_sv = _parse_tag_semver(best_tag, prefix)
        if cur_sv is not None and best_sv is not None and best_sv < cur_sv:
            return ProviderResult(
                candidate=UpdateCandidate(
                    value=current,
                    kind=UpdateKind.VERSION,
                    artifacts={},
                    published_at=published_at,
                )
            )

        # Resolve authoritative checksums from a published SHA256SUMS-like
        # asset (if present in the release).  Fall back to release body text.
        release_body = best_rel.get("body", "")
        body_text = release_body if isinstance(release_body, str) else ""
        raw_assets = best_rel.get("assets")
        assets_list: list[dict[str, object]] = (
            [a for a in raw_assets if isinstance(a, dict)]
            if isinstance(raw_assets, list)
            else []
        )
        checksum_map = _resolve_checksums(assets_list, context)

        def _get_checksum(asset_name: str, asset: dict[str, object]) -> Optional[str]:
            # 0. Asset metadata digest (most authoritative — published by API)
            digest = _extract_asset_digest(asset)
            if digest is not None:
                return digest
            # 1. Asset-level companion file
            digest = _resolve_asset_companion_checksum(
                asset_name,
                assets_list,
                context,
            )
            if digest is not None:
                return digest
            # 2. SHA256SUMS file (per-release checksums)
            if checksum_map is not None:
                return checksum_map.get(asset_name)
            # 3. Release body text (heuristic fallback)
            return _extract_checksum_for_asset(asset_name, body_text)

        candidate_artifacts: dict[str, CandidateArtifact] = {}

        for platform in required:
            current_art = target.artifacts.get(platform)
            if current_art is None:
                continue

            asset = _find_asset(best_rel, current_art.url, current, best_tag, prefix)
            if asset is None:
                continue

            asset_name = asset.get("name", "")
            asset_url = asset.get("browser_download_url")
            if not isinstance(asset_url, str):
                continue

            sha256 = _get_checksum(str(asset_name), asset)

            candidate_artifacts[platform] = CandidateArtifact(
                platform=platform,
                name=str(asset_name),
                url=asset_url,
                sha256=sha256,
            )

        # Return the candidate with whatever artifacts were resolved.
        # The coordinator classifies INCOMPLETE when required artifacts
        # are missing or lack SHA-256.
        return ProviderResult(
            candidate=UpdateCandidate(
                value=best_tag,
                kind=UpdateKind.VERSION,
                artifacts=candidate_artifacts,
                published_at=published_at,
            ),
        )
