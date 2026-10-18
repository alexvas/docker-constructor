"""Rust stable channel update provider.

Parses https://static.rust-lang.org/dist/channel-rust-stable.toml
to discover the latest Rust stable version.
"""

from __future__ import annotations

import re
import tomllib

from .base import ProviderContext, ProviderResult, UpdateProvider
from ..constraints import parse_numeric_version
from ..model import (
    RustChannelSource,
    RustChannelUpdate,
    UpdateCandidate,
    UpdateKind,
    UpdateTarget,
)

_RUST_VERSION_RE = re.compile(
    r"^((?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))(?:\s+\([^)]*\))?$"
)

# Groups: 1 = X.Y.Z prefix (always present)

CHANNEL_MANIFEST_URL = "https://static.rust-lang.org/dist/channel-rust-stable.toml"


class RustChannelProvider:
    name = "rust-channel"

    def discover(
        self,
        target: UpdateTarget,
        context: ProviderContext,
    ) -> ProviderResult:
        source = target.source
        if not isinstance(source, RustChannelSource):
            return ProviderResult(
                skipped_reason=f"{target.path}: expected rust-channel source, got {type(source).__name__}"
            )
        upd = target.update
        if not isinstance(upd, RustChannelUpdate):
            return ProviderResult(
                skipped_reason=f"{target.path}: expected rust-channel update, got {type(upd).__name__}"
            )

        # Always fetch the current stable manifest
        url = CHANNEL_MANIFEST_URL

        try:
            resp = context.http.request("GET", url, headers={})
        except Exception as exc:
            return ProviderResult(unavailable_reason=f"Rust channel: {exc}")

        if resp.status != 200:
            return ProviderResult(
                unavailable_reason=f"Rust channel returned HTTP {resp.status}"
            )

        try:
            manifest = tomllib.loads(resp.body.decode("utf-8"))
        except Exception as exc:
            return ProviderResult(
                unavailable_reason=f"Rust channel: invalid TOML ({exc})"
            )

        if not isinstance(manifest, dict):
            return ProviderResult(
                unavailable_reason="Rust channel: unexpected manifest format"
            )

        pkg = manifest.get("pkg")
        if not isinstance(pkg, dict):
            return ProviderResult(
                unavailable_reason="Rust channel: missing 'pkg' section"
            )

        rust = pkg.get("rust")
        if not isinstance(rust, dict):
            return ProviderResult(
                unavailable_reason="Rust channel: missing 'pkg.rust' section"
            )

        stable_version = rust.get("version")
        if not isinstance(stable_version, str):
            return ProviderResult(
                unavailable_reason="Rust channel: missing stable 'version' field"
            )

        # Validate version format and extract X.Y.Z portion.
        # Real manifests include metadata: "1.97.1 (hash date)"
        m = _RUST_VERSION_RE.match(stable_version)
        if not m:
            return ProviderResult(
                unavailable_reason=f"Rust channel: unexpected version format {stable_version!r}"
            )
        version = m.group(1)

        current = target.current
        if version == current:
            return ProviderResult(
                candidate=UpdateCandidate(
                    value=current,
                    kind=UpdateKind.VERSION,
                    artifacts={},
                )
            )

        # Prevent downgrade: if upstream stable is older than selected,
        # report CURRENT rather than OUTDATED.
        try:
            cur_nv = parse_numeric_version(current)
            cand_nv = parse_numeric_version(version)
            if cand_nv < cur_nv:
                return ProviderResult(
                    candidate=UpdateCandidate(
                        value=current,
                        kind=UpdateKind.VERSION,
                        artifacts={},
                    )
                )
        except Exception:
            pass  # fall through to string comparison

        return ProviderResult(
            candidate=UpdateCandidate(
                value=version,
                kind=UpdateKind.VERSION,
                artifacts={},
            )
        )
