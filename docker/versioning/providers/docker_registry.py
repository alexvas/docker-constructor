"""Docker Registry update provider.

Checks a Docker manifest for the selected tag and reports digest refreshes.
"""

from __future__ import annotations

import json
import re
from typing import Mapping

from ..model import (
    DockerRegistrySource,
    DockerRegistryUpdate,
    UpdateCandidate,
    UpdateKind,
    UpdateTarget,
)
from .base import ProviderContext, ProviderResult

_DOCKER_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_AUTH_REALM_RE = re.compile(r'realm="([^"]+)"')
_AUTH_SERVICE_RE = re.compile(r'service="([^"]+)"')

# Docker Hub hostname aliases normalized to the canonical Registry API endpoint.
_DOCKER_HUB_ALIASES: dict[str, str] = {
    "docker.io": "registry-1.docker.io",
    "index.docker.io": "registry-1.docker.io",
}


def _header_case_insensitive(headers: "Mapping[str, str]", name: str) -> str | None:
    """Look up an HTTP header name case-insensitively."""
    name_lower = name.lower()
    for key, val in headers.items():
        if key.lower() == name_lower:
            return val
    return None


class DockerRegistryProvider:
    name = "docker-registry"

    def discover(
        self,
        target: UpdateTarget,
        context: ProviderContext,
    ) -> ProviderResult:
        source = target.source
        if not isinstance(source, DockerRegistrySource):
            return ProviderResult(
                skipped_reason=(
                    f"{target.path}: expected docker-registry source, "
                    f"got {type(source).__name__}"
                )
            )
        upd = target.update
        if not isinstance(upd, DockerRegistryUpdate):
            return ProviderResult(
                skipped_reason=(
                    f"{target.path}: expected docker-registry update, "
                    f"got {type(upd).__name__}"
                )
            )

        if upd.track != "tag-digest":
            return ProviderResult(
                skipped_reason=f"{target.path}: unsupported track {upd.track!r}"
            )

        registry = source.registry.rstrip("/")
        repository = source.repository
        tag = target.current

        # Normalize Docker Hub aliases to the canonical Registry API endpoint.
        api_registry = _normalize_registry(registry)

        # Acquire a bearer token if the registry requires authentication.
        # Docker Hub issues 401 with WWW-Authenticate for anonymous pulls.
        bearer = self._acquire_token(api_registry, repository, context)

        manifest_url = f"https://{api_registry}/v2/{repository}/manifests/{tag}"
        headers = {
            "Accept": (
                "application/vnd.docker.distribution.manifest.v2+json, "
                "application/vnd.oci.image.manifest.v1+json, "
                "application/vnd.docker.distribution.manifest.list.v2+json, "
                "application/vnd.oci.image.index.v1+json"
            ),
        }
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"

        try:
            resp = context.http.request("GET", manifest_url, headers=dict(headers))
        except Exception as exc:
            return ProviderResult(unavailable_reason=f"Docker Registry: {exc}")

        if resp.status == 401:
            return ProviderResult(
                unavailable_reason=(
                    "Docker Registry: authentication required (token flow failed)"
                )
            )
        if resp.status != 200:
            return ProviderResult(
                unavailable_reason=(f"Docker Registry returned HTTP {resp.status}")
            )

        upstream_digest = _header_case_insensitive(
            resp.headers, "Docker-Content-Digest"
        )
        if not upstream_digest:
            return ProviderResult(
                unavailable_reason=(
                    "Docker Registry: missing Docker-Content-Digest header"
                )
            )

        if not _DOCKER_DIGEST_RE.match(upstream_digest):
            return ProviderResult(
                unavailable_reason=(
                    f"Docker Registry: invalid digest format {upstream_digest!r}"
                )
            )

        # Return the upstream digest as a candidate.
        # The coordinator compares it against the stored digest and
        # classifies as CURRENT (same) or OUTDATED (different).
        return ProviderResult(
            candidate=UpdateCandidate(
                value=upstream_digest,
                kind=UpdateKind.DIGEST_REFRESH,
                artifacts={},
                digest=upstream_digest,
            )
        )

    # ------------------------------------------------------------------
    # Token acquisition
    # ------------------------------------------------------------------

    def _acquire_token(
        self,
        registry: str,
        repository: str,
        context: ProviderContext,
    ) -> str | None:
        """Try anonymous bearer-token flow. Returns token or None."""
        # Check for an explicit token first.
        explicit = context.tokens.get("DOCKER_REGISTRY_TOKEN")
        if explicit:
            return explicit

        # Probe /v2/ to discover auth requirements.
        probe_url = f"https://{registry}/v2/"
        try:
            resp = context.http.request("GET", probe_url, headers={}, nocache=True)
        except Exception:
            return None

        if resp.status == 200:
            # No auth required.
            return None

        if resp.status != 401:
            return None

        www_auth = _header_case_insensitive(resp.headers, "WWW-Authenticate") or ""
        realm = _extract_www_auth_param(www_auth, "realm")
        service = _extract_www_auth_param(www_auth, "service")
        scopes = _extract_www_auth_param(www_auth, "scope")

        if not realm or not service:
            return None

        token_url = f"{realm}?service={service}"
        if scopes:
            token_url += f"&scope={scopes}"
        else:
            token_url += f"&scope=repository:{repository}:pull"

        try:
            tr = context.http.request("GET", token_url, headers={}, nocache=True)
        except Exception:
            return None

        if tr.status != 200:
            return None

        try:
            data = json.loads(tr.body)
        except Exception:
            return None

        token = data.get("token") or data.get("access_token")
        return token if isinstance(token, str) else None


def _normalize_registry(registry: str) -> str:
    """Normalize known Docker Hub aliases to the canonical Registry API host.

    Non-Docker-Hub hostnames are returned unchanged.
    """
    return _DOCKER_HUB_ALIASES.get(registry, registry)


def _extract_www_auth_param(header: str, key: str) -> str | None:
    """Extract a quoted WWW-Authenticate parameter."""
    pattern = key + r'="([^"]*)"'
    m = re.search(pattern, header)
    return m.group(1) if m else None
