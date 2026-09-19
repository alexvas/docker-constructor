"""Frozen dataclass models for the version inventory.

Every versioned entry has a corresponding frozen dataclass with
source/update metadata.  Provider-specific types are separate classes,
not generic dict proxies.

All containers exposed after validation are immutable:
- component/tag lists become tuples
- artifact maps become types.MappingProxyType
- extension maps become types.MappingProxyType
"""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Optional, Protocol

from .constraints import Constraint, NumericVersion  # re-export for convenience

# ---------------------------------------------------------------------------
# Shared semver validation (used by both model and inventory layers)
# ---------------------------------------------------------------------------

from .semver import (
    SemverError,
    validate as _validate_semver,
)


class InvalidArtifactKey(ValueError):
    """Raised when an artifact-map key is not a valid, non-moving semver."""


# ---------------------------------------------------------------------------
# Override policy
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OverridePolicy:
    constraint: Constraint
    allow_prerelease: bool
    scheme: str


# ---------------------------------------------------------------------------
# Artifact
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ArtifactEntry:
    url: str
    sha256: str


# ---------------------------------------------------------------------------
# Source metadata (provider-specific)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GitHubReleaseSource:
    repository: str
    tag: str
    type: str = "github-release"


@dataclass(frozen=True)
class NpmSource:
    package: str
    type: str = "npm"


@dataclass(frozen=True)
class PiReleaseSource:
    """Reviewed Pi release contract: npm package identity plus the exact
    GitHub release repository and tag prefix used to derive immutable
    release-asset URLs (``SHA256SUMS`` and the two installation files)."""
    package: str
    release_repository: str
    release_tag_prefix: str
    type: str = "pi-release"


@dataclass(frozen=True)
class PyPiSource:
    package: str
    type: str = "pypi"


@dataclass(frozen=True)
class UvPythonSource:
    implementation: str  # "cpython" only
    type: str = "uv-python"


@dataclass(frozen=True)
class RustChannelSource:
    manifest: str
    type: str = "rust-channel"


@dataclass(frozen=True)
class DockerRegistrySource:
    registry: str
    repository: str
    type: str = "docker-registry"


@dataclass(frozen=True)
class GitSource:
    repository: str
    type: str = "git"


@dataclass(frozen=True)
class StaticUrlSource:
    """A version-independent source whose integrity is verified via a
    published checksum file (e.g. rustup-init bootstrap binary).

    The ``checksum_url`` points to an authoritative ``sha256sum``-format
    file.  The per-platform artifact URLs and digests live in the parent
    entry's ``artifacts`` table (e.g. ``rustup.artifacts.linux-amd64``).
    """
    checksum_url: str
    type: str = "static-url"


# ---------------------------------------------------------------------------
# Update metadata (provider-specific)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GitHubReleaseUpdate:
    stable_only: bool
    tag_prefix: str = ""
    required_platforms: tuple[str, ...] = ()
    provider: str = "github-release"


@dataclass(frozen=True)
class NpmUpdate:
    stable_only: bool
    provider: str = "npm"


@dataclass(frozen=True)
class PyPiUpdate:
    stable_only: bool
    provider: str = "pypi"


@dataclass(frozen=True)
class UvPythonUpdate:
    implementation: str  # "cpython" only
    stable_only: bool
    provider: str = "uv-python"


@dataclass(frozen=True)
class RustChannelUpdate:
    channel: str
    stable_only: bool
    provider: str = "rust-channel"


@dataclass(frozen=True)
class DockerRegistryUpdate:
    stable_only: bool
    track: str
    provider: str = "docker-registry"


@dataclass(frozen=True)
class GitRefUpdate:
    ref: str
    provider: str = "git-ref"


@dataclass(frozen=True)
class StaticUrlUpdate:
    """Update contract for version-independent static URLs — the artifact
    is content-addressed (SHA-256), and a provider can detect drift by
    re-downloading and comparing the digest."""
    stable_only: bool = True
    provider: str = "static-url"


# ---------------------------------------------------------------------------
# Entry types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NodeEntry:
    tag: str
    digest: str
    node_version: str
    npm_version: str
    source: DockerRegistrySource
    update: DockerRegistryUpdate


@dataclass(frozen=True)
class RustEntry:
    version: str
    profile: str
    components: tuple[str, ...]
    source: RustChannelSource
    update: RustChannelUpdate
    rustup: Mapping[str, ArtifactEntry]
    rustup_source: StaticUrlSource
    rustup_update: StaticUrlUpdate


@dataclass(frozen=True)
class UvEntry:
    version: str
    artifacts: Mapping[str, ArtifactEntry]
    source: GitHubReleaseSource
    update: GitHubReleaseUpdate


@dataclass(frozen=True)
class PythonEntry:
    version: str
    source: UvPythonSource
    update: UvPythonUpdate
    override: Optional[OverridePolicy] = None


@dataclass(frozen=True)
class TyEntry:
    version: str
    source: PyPiSource
    update: PyPiUpdate


@dataclass(frozen=True)
class PrebuiltToolEntry:
    version: str
    artifacts: Mapping[str, ArtifactEntry]
    source: GitHubReleaseSource
    update: GitHubReleaseUpdate


@dataclass(frozen=True)
class NpmToolEntry:
    version: str
    source: NpmSource
    update: NpmUpdate


@dataclass(frozen=True)
class PiToolEntry:
    version: str
    source: PiReleaseSource
    update: NpmUpdate


@dataclass(frozen=True)
class OhMyZshEntry:
    revision: str
    source: GitSource
    update: GitRefUpdate


def _validate_artifact_key(key: str, ext_name: str) -> None:
    """Reject artifact-map keys that are not exact non-moving semver."""
    try:
        _validate_semver(key)
    except SemverError as exc:
        raise InvalidArtifactKey(
            f"PiExtensionEntry({ext_name!r}): artifact key {key!r} "
            f"{exc}"
        ) from exc


def _validate_npm_tarball_url(
    url: str, package: str, version_key: str,
) -> None:
    """Validate *url* is an exact npm registry tarball for *package*.

    Delegates to :func:`docker.versioning.npm_tarball.validate`,
    mapping :class:`~docker.versioning.npm_tarball.NpmTarballUrlError`
    to :class:`InvalidArtifactKey` for the model boundary.
    """
    from .npm_tarball import NpmTarballUrlError, validate

    try:
        validate(url, package, version_key)
    except NpmTarballUrlError as exc:
        raise InvalidArtifactKey(str(exc)) from exc


def _validate_runtime_projection(projection: object) -> None:
    """Closed-DTO validator — rejects anything not in the
    ``EffectiveRuntimeProjection`` / ``EffectivePiExtensionEntry`` schema.

    The runtime projection is mounted read-only at container start;
    it MUST NOT carry build entries, update providers, override policy,
    source metadata, or unselected artifacts.
    """
    from dataclasses import fields, is_dataclass

    # Import deferred to avoid circular dependency at module level
    allowed = {
        "extensions": dict,
        # EffectivePiExtensionEntry fields:
        "package": str,
        "version": str,
        "artifact_id": str,
        "integrity": str,
        "metadata_file": str,
    }
    disallowed = [
        "source", "update", "override", "validation", "artifacts",
        "build", "stages", "platform", "node", "rust", "uv",
        "python_version", "ty_version", "rtk", "fd",
        "pi_version", "openspec_version", "oh_my_zsh_revision",
    ]

    def _reject_disallowed_fields(obj: object, prefix: str) -> None:
        if not is_dataclass(obj):
            return
        for f in fields(obj):
            if f.name in disallowed:
                raise ValueError(
                    f"{prefix}.{f.name}: disallowed in runtime projection"
                )
            if f.name not in allowed:
                raise ValueError(
                    f"{prefix}.{f.name}: unrecognized field in runtime projection"
                )
            val = getattr(obj, f.name)
            if isinstance(val, dict):
                for sub_k, sub_v in val.items():
                    _reject_disallowed_fields(
                        sub_v, f"{prefix}.{f.name}.{sub_k}"
                    )

    _reject_disallowed_fields(projection, "")


@dataclass(frozen=True)
class NpmArtifact:
    """Reviewed npm-dist artifact with integrity verification."""
    url: str
    integrity: str


@dataclass(frozen=True)
class RuntimeValidation:
    """Host-side validation metadata for runtime extension packages."""
    metadata_file: str

    def __post_init__(self) -> None:
        _require_safe_metadata_path("RuntimeValidation", self.metadata_file)


# shared validation used by both model and inventory layers
_PACKAGE_JSON = "package.json"


def _require_safe_metadata_path(source: str, value: str) -> None:
    """Reject metadata_file values that cross the installer trust boundary."""
    # Use object.__setattr__ to allow mutation inside frozen __post_init__
    if value == _PACKAGE_JSON:
        return
    if value.startswith("/"):
        raise ValueError(
            f"{source}.metadata_file: absolute path {value!r} not allowed; "
            f"must be 'package.json' or a safe relative path"
        )
    segments = value.split("/")
    if ".." in segments or "" in segments:
        raise ValueError(
            f"{source}.metadata_file: {value!r} contains path traversal "
            f"or empty segments; must be 'package.json' or a safe relative path"
        )


@dataclass(frozen=True)
class PiExtensionEntry:
    version: str
    source: NpmSource
    update: NpmUpdate
    artifacts: Mapping[str, NpmArtifact]
    validation: RuntimeValidation
    override: OverridePolicy

    def __post_init__(self) -> None:
        if not self.artifacts:
            raise ValueError(
                f"PiExtensionEntry({self.source.package!r}): "
                f"artifacts must contain at least one entry"
            )
        if self.version not in self.artifacts:
            raise ValueError(
                f"PiExtensionEntry: default version {self.version!r} "
                f"must have a matching entry in artifacts"
            )
        # Validate every artifact-map key is an exact non-moving semver
        # and that the tarball URL exactly matches the package + version.
        ext_name = self.source.package
        for key, artifact in self.artifacts.items():
            _validate_artifact_key(key, ext_name)
            _validate_npm_tarball_url(artifact.url, ext_name, key)
        # Ensure immutability even when a plain dict is passed
        object.__setattr__(self, "artifacts", MappingProxyType(dict(self.artifacts)))


# ---------------------------------------------------------------------------
# Effective build projection (Stage 4)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EffectiveArtifact:
    """Resolved platform artifact with URL and checksum."""
    url: str
    sha256: str


@dataclass(frozen=True)
class EffectiveNode:
    """Resolved base image reference plus caller-owned reviewed tool
    versions (never inferred from the image tag)."""
    image: str
    node_version: str
    npm_version: str


@dataclass(frozen=True)
class EffectivePiRelease:
    """Resolved Pi release-source selection for host assembly."""
    package: str
    release_repository: str
    release_tag_prefix: str


@dataclass(frozen=True)
class EffectiveRust:
    """Resolved Rust toolchain selection."""
    version: str
    profile: str
    components: tuple[str, ...]
    rustup: EffectiveArtifact


@dataclass(frozen=True)
class EffectiveTool:
    """Resolved tool with version and mandatory platform artifact."""
    version: str
    artifact: EffectiveArtifact


@dataclass(frozen=True)
class EffectiveBuildProjection:
    """Host-only effective build projection after override application."""
    platform: str
    node: EffectiveNode
    rust: EffectiveRust
    uv: EffectiveTool
    python_version: str
    ty_version: str
    rtk: EffectiveTool
    fd: EffectiveTool
    pi_version: str
    pi_release: EffectivePiRelease
    openspec_version: str
    oh_my_zsh_revision: str


@dataclass(frozen=True)
class EffectivePiExtensionEntry:
    """Single Pi extension entry in the effective runtime projection.

    Contains only the fields required for container-side installation:
    package identity, effective version, canonical mounted-artifact
    identity, SRI integrity, and metadata_file path.  No source,
    update, override, unselected artifacts, or downloadable URLs
    are included.

    The *artifact_id* is a relative path of the form
    ``<algorithm>/<urlsafe-base64>.tgz`` derived from the *integrity*
    SRI string.  The two fields MUST agree: the algorithm prefix
    and decoded digest bytes must match.
    """
    package: str
    version: str
    artifact_id: str
    integrity: str
    metadata_file: str

    def __post_init__(self) -> None:
        # 1. Validate metadata_file is safe (carried forward from
        #    the old RuntimeValidation check).
        _require_safe_metadata_path("EffectivePiExtensionEntry",
                                    self.metadata_file)

        # 2. Validate artifact_id: must be a safe relative path.
        _require_safe_artifact_id("EffectivePiExtensionEntry",
                                  self.artifact_id)

        # 3. Validate integrity is a well-formed SRI string.
        _require_well_formed_integrity("EffectivePiExtensionEntry",
                                       self.integrity)

        # 4. Derive the canonical artifact_id from the integrity and
        #    verify they agree.
        expected_id = _derive_artifact_id(self.integrity)
        if self.artifact_id != expected_id:
            raise ValueError(
                f"EffectivePiExtensionEntry: artifact_id {self.artifact_id!r} "
                f"does not match the canonical identity {expected_id!r} "
                f"derived from integrity {self.integrity!r}"
            )


# ── helpers for EffectivePiExtensionEntry.__post_init__ ──────────────

# Allowed SRI integrity algorithms and their digest byte-lengths.
# From the W3C Subresource Integrity spec.
_SRI_ALGORITHMS: dict[str, int] = {
    "sha256": 32,
    "sha384": 48,
    "sha512": 64,
}


def _require_well_formed_integrity(source: str, value: str) -> None:
    """Reject integrity strings that are not valid SRI."""
    if "-" not in value:
        raise ValueError(
            f"{source}.integrity: {value!r} is not a valid SRI string "
            f"(missing '-' separator)"
        )
    algo, b64 = value.split("-", 1)
    if algo not in _SRI_ALGORITHMS:
        raise ValueError(
            f"{source}.integrity: unknown algorithm {algo!r}"
        )
    expected_len = _SRI_ALGORITHMS[algo]
    try:
        raw = base64.b64decode(b64, validate=True)
    except Exception as exc:
        raise ValueError(
            f"{source}.integrity: invalid base64 payload: {exc}"
        ) from None
    if len(raw) != expected_len:
        raise ValueError(
            f"{source}.integrity: expected {expected_len} bytes "
            f"for {algo}, got {len(raw)}"
        )


def _require_safe_artifact_id(source: str, value: str) -> None:
    """Reject artifact_id values that contain absolute paths,
    traversal, leading slash, or empty segments."""
    if not value or value.startswith("/"):
        raise ValueError(
            f"{source}.artifact_id: {value!r} must be a non-empty "
            f"relative path (must not start with '/')"
        )
    segments = value.split("/")
    if ".." in segments or "" in segments:
        raise ValueError(
            f"{source}.artifact_id: {value!r} contains path traversal "
            f"or empty segments"
        )


def _derive_artifact_id(integrity: str) -> str:
    """Derive the canonical artifact_id from an SRI integrity string.

    The result is ``<algorithm>/<urlsafe-base64>.tgz`` where
    ``urlsafe-base64`` replaces ``+`` with ``-`` and ``/`` with ``_``.
    """
    algo, b64 = integrity.split("-", 1)
    safe = b64.replace("+", "-").replace("/", "_")
    return f"{algo}/{safe}.tgz"


@dataclass(frozen=True)
class EffectiveRuntimeProjection:
    """Container-only effective runtime projection.

    This DTO is mounted read-only at
    ``/run/pi-cli/docker-constructor.runtime.toml``.  It SHALL NOT
    contain build entries, update providers, override policy,
    source metadata, or unselected artifacts.
    """
    extensions: Mapping[str, EffectivePiExtensionEntry]

    def __post_init__(self) -> None:
        _validate_runtime_projection(self)
        object.__setattr__(
            self, "extensions",
            MappingProxyType(dict(self.extensions)),
        )


# --------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BaseStage:
    node: NodeEntry


@dataclass(frozen=True)
class ToolchainStage:
    rust: RustEntry
    uv: UvEntry
    python: PythonEntry
    ty: TyEntry


@dataclass(frozen=True)
class RtkPrebuiltStage:
    rtk: PrebuiltToolEntry


@dataclass(frozen=True)
class FdPrebuiltStage:
    fd: PrebuiltToolEntry


@dataclass(frozen=True)
class PiToolsStage:
    pi: PiToolEntry


@dataclass(frozen=True)
class OpenSpecToolsStage:
    openspec: NpmToolEntry


@dataclass(frozen=True)
class RuntimeStage:
    oh_my_zsh: OhMyZshEntry


@dataclass(frozen=True)
class Stages:
    base: BaseStage
    toolchain: ToolchainStage
    rtk_prebuilt: RtkPrebuiltStage
    fd_prebuilt: FdPrebuiltStage
    pi_tools: PiToolsStage
    openspec_tools: OpenSpecToolsStage
    runtime: RuntimeStage


@dataclass(frozen=True)
class HostAccessPolicy:
    """Reviewed, immutable runtime host-access policy."""
    enabled: bool = False
    mode: str | None = None
    proxy_port: int | None = None


@dataclass(frozen=True)
class LocalHostAccess:
    """Machine-local host address; never an inventory overlay."""
    address: str | None = None


@dataclass(frozen=True)
class LocalCacheConfig:
    """Machine-local cache directory; never reviewed policy."""
    dir: str | None = None


@dataclass(frozen=True)
class LocalCorporateTrust:
    """Machine-local corporate trust intent; never an inventory overlay."""
    enabled: bool = False


@dataclass(frozen=True)
class LocalNetworkProxy:
    """Machine-local credential-free proxy; never an inventory overlay."""
    url: str | None = None
    no_proxy: str | None = None


@dataclass(frozen=True)
class LocalOutputPolicy:
    """Host-only facade presentation policy from local ``[output]``."""
    host_heartbeat: str = "interactive"
    show_network_hosts: bool = False


@dataclass(frozen=True)
class LocalConfig:
    """Closed local companion state."""
    host_access: LocalHostAccess = LocalHostAccess()
    cache: LocalCacheConfig = LocalCacheConfig()
    corporate_trust: LocalCorporateTrust = LocalCorporateTrust()
    network_proxy: LocalNetworkProxy = LocalNetworkProxy()
    output: LocalOutputPolicy = LocalOutputPolicy()


@dataclass(frozen=True)
class BuildLocalInputs:
    """Domain-owned local slices consumed by build planning.

    This is the *only* local-companion projection that may cross into
    build orchestration.  It deliberately excludes the host-only
    ``[output]`` presentation policy (``host_heartbeat`` /
    ``show_network_hosts``), which is retained by the facade.
    """
    corporate_trust_enabled: bool = False
    cache_dir: str | None = None
    network_proxy_url: str | None = None
    network_proxy_no_proxy: str | None = None

    @classmethod
    def from_local_config(cls, local: "LocalConfig") -> "BuildLocalInputs":
        """Project aggregate local state onto build-planning inputs only."""
        return cls(
            corporate_trust_enabled=local.corporate_trust.enabled,
            cache_dir=local.cache.dir,
            network_proxy_url=local.network_proxy.url,
            network_proxy_no_proxy=local.network_proxy.no_proxy,
        )


@dataclass(frozen=True)
class CacheConfig:
    """Reviewed portable cache policy from docker-constructor.toml."""
    ttl: int | None = None
    """Default TTL in seconds (positive integer)."""


@dataclass(frozen=True)
class BuildInventory:
    """Immutable container for build-phase dependencies."""
    stages: Stages

    def __post_init__(self):
        # stages is already a frozen dataclass, so it is immutable
        # at the top level.  This post_init is a guardrail for
        # Stage 2 when the field becomes the canonical name.
        if not isinstance(self.stages, Stages):
            raise TypeError(
                f"BuildInventory.stages must be a Stages instance, "
                f"got {type(self.stages).__name__}"
            )


@dataclass(frozen=True)
class RuntimeInventory:
    """Immutable container for runtime dependencies and launch policy."""
    pi_extensions: Mapping[str, PiExtensionEntry]
    host_access: HostAccessPolicy = HostAccessPolicy()

    def __post_init__(self):
        # Always copy into a fresh dict and wrap in MappingProxyType.
        # Never trust an incoming MappingProxyType backed by a still-
        # reachable mutable dict.
        object.__setattr__(
            self, "pi_extensions",
            MappingProxyType(dict(self.pi_extensions)),
        )


@dataclass(frozen=True)
class Inventory:
    # Field names are intentionally the legacy names stages /
    # runtime_pi_extensions so existing consumers (construction,
    # dataclasses.replace, serialization, CLI, effective, rendering,
    # orchestration, updates, build-wrapper) continue to work without
    # migration.  The canonical build / runtime properties below
    # satisfy the Stage‑1 typed‑container contract.  Field names are
    # migrated to build / runtime in Stage 2 together with the
    # repository TOML and all fixtures.
    schema: int
    stages: Stages
    runtime_pi_extensions: Mapping[str, PiExtensionEntry]
    cache: CacheConfig | None = None
    host_access: HostAccessPolicy = HostAccessPolicy()

    def __post_init__(self):
        # Normalize runtime_pi_extensions so direct construction
        # (bypassing load_inventory) cannot expose mutable state.
        # Uses the same logic as RuntimeInventory.__post_init__.
        object.__setattr__(
            self, "runtime_pi_extensions",
            MappingProxyType(dict(self.runtime_pi_extensions)),
        )

    # ------------------------------------------------------------------
    # Stage‑1 canonical accessors (thin typed wrappers)
    # ------------------------------------------------------------------
    # These satisfy the contract that inventory.build and
    # inventory.runtime return BuildInventory / RuntimeInventory
    # without changing any existing call site.

    @property
    def build(self) -> BuildInventory:
        """Canonical phase container for build-stage dependencies."""
        return BuildInventory(stages=self.stages)

    @property
    def runtime(self) -> RuntimeInventory:
        """Canonical phase container for runtime Pi extensions."""
        return RuntimeInventory(
            pi_extensions=self.runtime_pi_extensions,
            host_access=self.host_access,
        )


# ---------------------------------------------------------------------------
# Update discovery types (Stage 3)
# ---------------------------------------------------------------------------

from enum import Enum


class UpdateStatus(str, Enum):
    CURRENT = "current"
    OUTDATED = "outdated"
    SKIPPED = "skipped"
    UNAVAILABLE = "unavailable"
    INCOMPLETE = "incomplete"


class UpdateKind(str, Enum):
    VERSION = "version"
    DIGEST_REFRESH = "digest-refresh"
    REVISION = "revision"


@dataclass(frozen=True)
class CandidateArtifact:
    platform: str
    name: str
    url: str
    sha256: str | None
    # Subresource-integrity digest (e.g. ``sha512-…``).  Used for npm
    # Pi-extension tarballs, whose reviewed inventory artifacts carry
    # ``integrity`` rather than ``sha256``.
    integrity: str | None = None


def _validate_utc_rfc3339(value: str | None) -> str | None:
    """Validate *value* against the RFC 3339 date-time grammar.

    Accepts ``Z`` and zero UTC offset (``+00:00``); normalises the
    offset to ``Z``-suffix.  ``-00:00`` is rejected — it denotes an
    unknown local offset, not authoritative UTC.  Other non-UTC
    offsets (``+01:00``) are rejected.  The fractional-precision
    portion is preserved exactly as received.

    Returns the validated timestamp, or ``None`` when *value* is
    absent, non-UTC, or unparseable.
    """
    import re
    from datetime import datetime, timezone, timedelta

    if value is None or not isinstance(value, str) or not value.strip():
        return None

    # RFC 3339 date-time (section 5.6):
    #   date-time = full-date "T" full-time
    #   time-offset = "Z" / time-numoffset
    #   time-numoffset = ("+" / "-") time-hour ":" time-minute
    #   partial-time = time-hour ":" time-minute ":" time-second [time-secfrac]
    #   time-secfrac  = "." 1*DIGIT
    # We cap at 6 fractional digits (microsecond) — that is Python's
    # datetime resolution.
    m = re.fullmatch(
        r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?)"
        r"(Z|(?:[+-]\d{2}:\d{2}))",
        value,
    )
    if not m:
        return None

    date_frac, offset = m.group(1), m.group(3)

    # Validate calendar date
    try:
        dt = datetime.fromisoformat(date_frac)
    except ValueError:
        return None

    # Validate UTC offset
    if offset == "Z":
        pass
    elif offset == "+00:00":
        pass
    elif offset == "-00:00":
        return None  # unknown local offset, not UTC
    else:
        # Parse offset, accept only zero
        off_h, off_m = int(offset[1:3]), int(offset[4:6])
        total_min = off_h * 60 + off_m
        if offset[0] == "-":
            total_min = -total_min
        if total_min != 0:
            return None

    # Normalise: offset → Z suffix; fractional precision preserved as-is
    base = dt.strftime("%Y-%m-%dT%H:%M:%S")
    if m.group(2):
        base += m.group(2)
    return base + "Z"


@dataclass(frozen=True)
class UpdateCandidate:
    value: str
    kind: UpdateKind
    artifacts: Mapping[str, CandidateArtifact]
    digest: str | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)
    published_at: str | None = None
    # Set by the npm provider for Pi-extension candidates whose ``dist`` is
    # missing or invalid; the coordinator classifies such candidates as
    # INCOMPLETE (not applicable) instead of as ready replacements.
    incomplete_reason: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "artifacts", MappingProxyType(dict(self.artifacts)))
        if self.published_at is not None:
            validated = _validate_utc_rfc3339(self.published_at)
            if validated is None:
                raise ValueError(
                    f"UpdateCandidate.published_at must be a valid UTC RFC 3339 "
                    f"timestamp, got {self.published_at!r}"
                )
            object.__setattr__(self, "published_at", validated)
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True)
class UpdateResult:
    path: str
    provider: str
    current: str
    candidate: str | None
    status: UpdateStatus
    kind: UpdateKind
    applicable: bool
    reason: str | None
    artifacts: Mapping[str, CandidateArtifact]
    digest: str | None = None
    published_at: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "artifacts", MappingProxyType(dict(self.artifacts)))
        if self.published_at is not None:
            validated = _validate_utc_rfc3339(self.published_at)
            if validated is None:
                raise ValueError(
                    f"UpdateResult.published_at must be a valid UTC RFC 3339 "
                    f"timestamp, got {self.published_at!r}"
                )
            object.__setattr__(self, "published_at", validated)

    def to_dict(self) -> dict[str, object]:
        """Return a deterministic JSON-serializable dict."""
        import json
        result: dict[str, object] = {
            "applicable": self.applicable,
            "candidate": self.candidate,
            "current": self.current,
            "kind": self.kind.value,
            "path": self.path,
            "provider": self.provider,
            "reason": self.reason,
            "status": self.status.value,
        }
        if self.digest is not None:
            result["digest"] = self.digest
        if self.published_at is not None:
            result["published_at"] = self.published_at
        # artifacts: plain dict of key → {url, sha256?, integrity?}
        if self.artifacts:
            serialized_artifacts: dict[str, dict[str, object]] = {}
            for p, a in sorted(self.artifacts.items()):
                entry: dict[str, object] = {"url": a.url}
                if a.sha256 is not None:
                    entry["sha256"] = a.sha256
                if a.integrity is not None:
                    entry["integrity"] = a.integrity
                serialized_artifacts[p] = entry
            result["artifacts"] = serialized_artifacts
        return result


class UpdateMetadata(Protocol):
    @property
    def provider(self) -> str: ...


@dataclass(frozen=True)
class UpdateTarget:
    """A single entry ready for update discovery."""
    path: str
    current: str
    source: object  # SourceMetadata subclass
    update: UpdateMetadata
    artifacts: Mapping[str, ArtifactEntry]
    override: Optional[OverridePolicy] = None

    def __post_init__(self):
        object.__setattr__(self, "artifacts", MappingProxyType(dict(self.artifacts)))
