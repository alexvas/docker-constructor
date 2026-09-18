"""TOML inventory loader and validator.

No Docker, network, or subprocess. Uses the shared configuration-document
boundary plus dataclasses, re, pathlib, and types.
"""
from __future__ import annotations

import base64
import builtins
import re
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Optional, Union

from .configuration_document_validation import (
    DocumentIdentity,
    DocumentRole,
    ParsedConfigurationDocument,
    ProjectedOwnerResult,
    capture_owner_result,
    parse_configuration_document,
    release_owner_result,
    validate_configuration_documents,
)
from .errors import ConstraintSyntaxError, InventoryError, VersionConfigError, VersionSyntaxError
from .local_project_configuration import (
    load_local_project_configuration,
    load_optional_local_project_configuration,
    resolve_local_companion_path,
    validate_local_document,
)
from .constraints import (
    parse_numeric_version,
    parse_constraint,
    validate_constraint_consistency,
    NumericVersion,
)
from .model import (
    ArtifactEntry,
    BaseStage,
    CacheConfig,
    HostAccessPolicy,
    LocalConfig,
    DockerRegistrySource,
    DockerRegistryUpdate,
    FdPrebuiltStage,
    GitHubReleaseSource,
    GitHubReleaseUpdate,
    GitRefUpdate,
    GitSource,
    InvalidArtifactKey,
    Inventory,
    NodeEntry,
    NpmArtifact,
    NpmSource,
    NpmToolEntry,
    NpmUpdate,
    OhMyZshEntry,
    OpenSpecToolsStage,
    OverridePolicy,
    PiExtensionEntry,
    PiReleaseSource,
    PiToolEntry,
    PiToolsStage,
    PrebuiltToolEntry,
    PyPiSource,
    PyPiUpdate,
    PythonEntry,
    RtkPrebuiltStage,
    RuntimeInventory,
    RuntimeStage,
    RuntimeValidation,
    RustChannelSource,
    RustChannelUpdate,
    RustEntry,
    Stages,
    StaticUrlSource,
    StaticUrlUpdate,
    ToolchainStage,
    TyEntry,
    UvEntry,
    UvPythonSource,
    UvPythonUpdate,
)
from .npm_tarball import validate as _validate_npm_tarball_url_model
from .semver import SemverError, validate as _validate_semver


# ---------------------------------------------------------------------------
# Regex constants
# ---------------------------------------------------------------------------

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
NODE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
PEM_CERTIFICATE_BEGIN = "-----BEGIN CERTIFICATE-----"
PEM_CERTIFICATE_END = "-----END CERTIFICATE-----"
GIT_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
VERSION_STRICT_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
PREBUILT_VERSION_RE = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")

# Moving version tags forbidden everywhere (Rust selectors, npm tags, etc.)
_MOVING_RUST_SELECTORS = frozenset({"stable", "beta", "nightly"})


# ---------------------------------------------------------------------------
# Helpers for TOML access with dot-path error reporting
# ---------------------------------------------------------------------------

def _dot(path: tuple[str, ...]) -> str:
    return ".".join(path)


def require_table(
    data: Mapping[str, object],
    path: tuple[str, ...],
) -> Mapping[str, object]:
    current: Any = data
    for key in path:
        if not isinstance(current, dict):
            raise InventoryError(
                f"{_dot(path[:path.index(key)])}: expected table, got {type(current).__name__}",
                field=_dot(path[:path.index(key)]),
            )
        if key not in current:
            raise InventoryError(f"{_dot(path)}: missing required key", field=_dot(path))
        current = current[key]
    if not isinstance(current, dict):
        raise InventoryError(
            f"{_dot(path)}: expected table, got {type(current).__name__}",
            field=_dot(path),
        )
    return current


def require_string(
    data: Mapping[str, object],
    path: tuple[str, ...],
) -> str:
    *parent_path, key = path
    current = require_table(data, tuple(parent_path)) if parent_path else data
    if key not in current:
        raise InventoryError(f"{_dot(path)}: missing required key", field=_dot(path))
    value = current[key]
    if not isinstance(value, str):
        raise InventoryError(
            f"{_dot(path)}: expected string, got {type(value).__name__}",
            field=_dot(path),
        )
    return value


def require_bool(
    data: Mapping[str, object],
    path: tuple[str, ...],
) -> bool:
    *parent_path, key = path
    current = require_table(data, tuple(parent_path)) if parent_path else data
    if key not in current:
        raise InventoryError(f"{_dot(path)}: missing required key", field=_dot(path))
    value = current[key]
    if not isinstance(value, bool):
        raise InventoryError(
            f"{_dot(path)}: expected boolean, got {type(value).__name__}",
            field=_dot(path),
        )
    return value


def require_int(
    data: Mapping[str, object],
    path: tuple[str, ...],
) -> int:
    *parent_path, key = path
    current = require_table(data, tuple(parent_path)) if parent_path else data
    if key not in current:
        raise InventoryError(f"{_dot(path)}: missing required key", field=_dot(path))
    value = current[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise InventoryError(
            f"{_dot(path)}: expected integer, got {type(value).__name__}",
            field=_dot(path),
        )
    return value


def require_nonempty_string(
    data: Mapping[str, object],
    path: tuple[str, ...],
) -> str:
    value = require_string(data, path)
    if not value.strip():
        raise InventoryError(f"{_dot(path)}: must not be empty", field=_dot(path))
    return value


# ---------------------------------------------------------------------------
# PathReader
# ---------------------------------------------------------------------------

class _PathReader:
    """Wraps a raw TOML dict for dot-path-aware validation."""

    def __init__(self, root: Mapping[builtins.str, object]):
        self._root = root

    def tbl(self, path: tuple[builtins.str, ...]) -> Mapping[builtins.str, object]:
        return require_table(self._root, path)

    def str(self, path: tuple[builtins.str, ...]) -> builtins.str:
        return require_string(self._root, path)

    def nonempty_str(self, path: tuple[builtins.str, ...]) -> builtins.str:
        return require_nonempty_string(self._root, path)

    def bool(self, path: tuple[builtins.str, ...]) -> builtins.bool:
        return require_bool(self._root, path)

    def int(self, path: tuple[builtins.str, ...]) -> builtins.int:
        return require_int(self._root, path)

    @property
    def root(self) -> Mapping[builtins.str, object]:
        return self._root


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _validate_sha256(hex_str: str, path: str) -> None:
    if not SHA256_RE.match(hex_str):
        raise InventoryError(
            f"{path}: expected 64 hexadecimal characters, got {hex_str!r}", field=path
        )


def _validate_node_digest(raw: str, path: str) -> None:
    if ":" not in raw:
        raise InventoryError(
            f"{path}: expected 'sha256:<hex>', missing colon in {raw!r}", field=path
        )
    if not NODE_DIGEST_RE.match(raw):
        raise InventoryError(
            f"{path}: expected 'sha256:<64 lowercase hex>', got {raw!r}", field=path
        )
    hex_part = raw.removeprefix("sha256:")
    if len(set(hex_part)) == 1:
        raise InventoryError(
            f"{path}: checksum looks like a placeholder"
        , field=path)


def _validate_url_contains_version(url: str, version: str, path: str) -> None:
    if version not in url:
        raise InventoryError(
            f"{path}: URL must contain the declared version {version!r}", field=path
        )


def _validate_admissible_url(url: str, path: str) -> None:
    """Reject URLs that are not plausible download origins."""
    from urllib.parse import urlsplit as _urlsplit

    if not url.startswith("https://"):
        raise InventoryError(
            f"{path}: URL must start with 'https://'", field=path
        )
    parsed = _urlsplit(url)
    if not parsed.hostname:
        raise InventoryError(
            f"{path}: URL must contain a non-empty hostname", field=path
        )
    if not parsed.path or parsed.path == "/":
        raise InventoryError(
            f"{path}: URL must contain a non-empty path", field=path
        )


def _validate_linux_amd64_artifact(
    artifacts: Mapping[str, object], parent_path: str
) -> None:
    if "linux-amd64" not in artifacts:
        raise InventoryError(
            f"{parent_path}.artifacts: missing required 'linux-amd64' platform artifact",
            field=f"{parent_path}.artifacts",
        )


def _reject_placeholder_sha256(sha256: str, path: str) -> None:
    if len(set(sha256)) == 1:
        raise InventoryError(
            f"{path}: checksum looks like a placeholder"
        , field=path)


def _reject_moving_rust_version(version: str, path: str) -> None:
    base = version.split("-")[0].lower()
    if base in _MOVING_RUST_SELECTORS:
        raise InventoryError(
            f"{path}: moving selector {version!r} is not allowed, use explicit X.Y.Z", field=path
        )


def _validate_rust_version(version: str, path: str) -> None:
    """Rust version must be exact X.Y.Z — rejects arbitrary strings and moving
    selectors such as stable/beta/nightly.
    """
    _reject_moving_rust_version(version, path)
    if not VERSION_STRICT_RE.match(version):
        raise InventoryError(
            f"{path}: expected exact X.Y.Z version, got {version!r}"
        , field=path)


def _validate_npm_version(version: str, path: str) -> None:
    """npm tool version must be exact X.Y.Z — rejects ranges and tags like 'latest'."""
    if not VERSION_STRICT_RE.match(version):
        raise InventoryError(
            f"{path}: expected exact X.Y.Z version, got {version!r}"
        , field=path)


def _validate_uv_version(version: str, path: str) -> None:
    """UV version must be exact X.Y.Z — rejects ranges, tags, and v-prefix."""
    if not VERSION_STRICT_RE.match(version):
        raise InventoryError(
            f"{path}: expected exact X.Y.Z version, got {version!r}"
        , field=path)


def _validate_prebuilt_version(version: str, path: str) -> None:
    """Prebuilt-tool version must be vX.Y.Z — rejects bare X.Y.Z, ranges, tags."""
    if not PREBUILT_VERSION_RE.match(version):
        raise InventoryError(
            f"{path}: expected vX.Y.Z version, got {version!r}"
        , field=path)


def _validate_extension_version(version: str, path: str) -> None:
    """Extension version must be a valid semver (https://semver.org), not a moving tag.

    Accepts X.Y.Z and semver prerelease/build forms (e.g., 0.2.0-beta.1).
    Rejects moving tags like 'latest', 'stable', 'next', 'dev', 'canary', 'nightly',
    and malformed suffixes like '-!!!' or '-01'.
    """
    try:
        _validate_semver(version)
    except SemverError as exc:
        raise InventoryError(f"{path}: {exc}", field=path) from exc


def _validate_artifact_catalog_key(key: str, path: str) -> None:
    """Reject artifact-catalog keys that are moving tags or invalid semver.

    Unlike *_validate_extension_version* (which validates the default
    ``version`` field), this validates every key inside the
    ``[runtime.pi-extensions.<name>.artifacts]`` table so that malformed
    entries never escape to the model layer.
    """
    try:
        _validate_semver(key)
    except SemverError as exc:
        raise InventoryError(f"{path}: {exc}", field=path) from exc


def _validate_npm_tarball_url(
    url: str, package: str, version_key: str, path: str
) -> None:
    """Thin wrapper — calls shared validator, maps
    :class:`~docker.versioning.npm_tarball.NpmTarballUrlError`
    to ``InventoryError`` with the canonical TOML path."""
    from .npm_tarball import NpmTarballUrlError

    try:
        _validate_npm_tarball_url_model(url, package, version_key)
    except NpmTarballUrlError as exc:
        raise InventoryError(f"{path}.url: {exc}", field=f"{path}.url") from exc


# ---------------------------------------------------------------------------
# Runtime extension artifact & validation helpers
# ---------------------------------------------------------------------------


def _load_extension_artifacts(
    r: _PathReader, ext_path: tuple[str, ...], package: str
) -> Mapping[str, NpmArtifact]:
    """Load and validate [runtime.pi-extensions.<name>.artifacts] sub-table."""
    path_dot = ".".join(ext_path)
    try:
        artifacts_table = r.tbl(ext_path + ("artifacts",))
    except InventoryError as e:
        if "missing" in str(e).lower():
            raise InventoryError(
                f"{path_dot}.artifacts: missing required section"
            , field=f"{path_dot}.artifacts") from e
        raise

    if not artifacts_table:
        raise InventoryError(
            f"{path_dot}.artifacts: must contain at least one version entry"
        , field=f"{path_dot}.artifacts")

    result: dict[str, NpmArtifact] = {}
    for version_key, artifact_raw in artifacts_table.items():
        if not isinstance(artifact_raw, dict):
            raise InventoryError(
                f"{path_dot}.artifacts.{version_key}: expected table"
            , field=f"{path_dot}.artifacts.{version_key}")
        _validate_artifact_catalog_key(
            version_key, f"{path_dot}.artifacts.{version_key}"
        )
        _check_unknown_keys(
            artifact_raw,
            ext_path + ("artifacts", version_key),
        )

        url = require_string(r.root, ext_path + ("artifacts", version_key, "url"))

        integrity = require_string(r.root, ext_path + ("artifacts", version_key, "integrity"))
        from .integrity import IntegrityError, validate_integrity
        try:
            validate_integrity(integrity)
        except IntegrityError as exc:
            raise InventoryError(
                f"{path_dot}.artifacts.{version_key}.integrity: {exc}"
            , field=f"{path_dot}.artifacts.{version_key}.integrity") from exc

        # Verify the URL is an exact npm registry tarball path:
        #   https://registry.npmjs.org/<package>/-/<pkg_name>-<version>.tgz
        # Substring matching is not enough — the version must appear as the
        # tarball filename suffix, not in a query string or unrelated path.
        _validate_npm_tarball_url(
            url, package, version_key, f"{path_dot}.artifacts.{version_key}",
        )

        result[version_key] = NpmArtifact(url=url, integrity=integrity)

    return MappingProxyType(result)




def _load_extension_validation(
    r: _PathReader, ext_path: tuple[str, ...]
) -> RuntimeValidation:
    """Load [runtime.pi-extensions.<name>.validation]."""
    path_dot = ".".join(ext_path)
    try:
        validation_raw = r.tbl(ext_path + ("validation",))
    except InventoryError as e:
        if "missing" in str(e).lower():
            raise InventoryError(
                f"{path_dot}.validation: missing required section"
            , field=f"{path_dot}.validation") from e
        raise
    metadata_file = require_string(r.root, ext_path + ("validation", "metadata_file"))
    try:
        return RuntimeValidation(metadata_file=metadata_file)
    except ValueError as e:
        raise InventoryError(
            f"{path_dot}.validation.metadata_file: {e}"
        , field=f"{path_dot}.validation.metadata_file") from e


def _extension_identity(source: NpmSource) -> tuple[str, str]:
    """Return a normalized identity for an npm extension."""
    return ("npm", source.package)


def _check_cross_phase_duplicates(
    stages: "Stages", extensions: dict[str, PiExtensionEntry]
) -> None:
    """Reject packages that appear in both build and runtime."""
    build_npm_identities: dict[tuple[str, str], str] = {}

    # Collect npm packages from build stages
    # pi-tools.pi
    pi_pkg = stages.pi_tools.pi.source.package
    build_npm_identities[("npm", pi_pkg)] = "build.stages.pi-tools.pi"
    # openspec-tools.openspec
    os_pkg = stages.openspec_tools.openspec.source.package
    build_npm_identities[("npm", os_pkg)] = "build.stages.openspec-tools.openspec"

    for name, ext in extensions.items():
        ext_identity = _extension_identity(ext.source)
        ext_path = f"runtime.pi-extensions.{name}"
        if ext_identity in build_npm_identities:
            raise InventoryError(
                f"duplicate npm package {ext.source.package!r}: "
                f"previously defined at {build_npm_identities[ext_identity]}, "
                f"duplicate at {ext_path}"
            , field=ext_path)


# --------------------------------------------------------------------------
# Unknown-key rejection
# --------------------------------------------------------------------------

# Allowed keys for every known TOML table (relative to stages/ runtime/).
# Extra keys or typoed fields cause path-qualified InventoryError.

_KNOWN_KEYS: dict[tuple[str, ...], frozenset[str]] = {}


def _register(path: tuple[str, ...], *keys: str) -> None:
    _KNOWN_KEYS[path] = frozenset(keys)


# --- Top-level ---
_register((), "schema", "build", "runtime")
_register(("build",), "stages")
_register(("build", "stages",), "base", "toolchain", "rtk-prebuilt", "fd-prebuilt",
          "pi-tools", "openspec-tools", "runtime")
_register(("build", "stages", "base"), "node")
_register(("build", "stages", "base", "node"), "tag", "digest", "node_version", "npm_version", "source", "update")
_register(("build", "stages", "base", "node", "source"), "type", "registry", "repository")
_register(("build", "stages", "base", "node", "update"), "provider", "stable_only", "track")

_register(("build", "stages", "toolchain"), "rust", "uv", "python", "ty")
_register(("build", "stages", "toolchain", "rust"), "version", "profile", "components", "source", "update", "rustup")
_register(("build", "stages", "toolchain", "rust", "source"), "type", "manifest")
_register(("build", "stages", "toolchain", "rust", "rustup"), "source", "update", "artifacts")
_register(("build", "stages", "toolchain", "rust", "rustup", "source"), "type", "checksum_url")
_register(("build", "stages", "toolchain", "rust", "rustup", "update"), "provider", "stable_only")
_register(("build", "stages", "toolchain", "rust", "rustup", "artifacts", "__ANY__"), "url", "sha256")
_register(("build", "stages", "toolchain", "rust", "update"), "provider", "channel", "stable_only")

_register(("build", "stages", "toolchain", "uv"), "version", "source", "artifacts", "update")
_register(("build", "stages", "toolchain", "uv", "source"), "type", "repository", "tag")
_register(("build", "stages", "toolchain", "uv", "update"), "provider", "stable_only", "tag_prefix", "required_platforms")
_register(("build", "stages", "toolchain", "uv", "artifacts", "__ANY__"), "url", "sha256")

_register(("build", "stages", "toolchain", "python"), "version", "source", "update", "override")
_register(("build", "stages", "toolchain", "python", "source"), "type", "implementation")
_register(("build", "stages", "toolchain", "python", "update"), "provider", "implementation", "stable_only")
_register(("build", "stages", "toolchain", "python", "override"), "constraint", "allow_prerelease", "scheme")

_register(("build", "stages", "toolchain", "ty"), "version", "source", "update")
_register(("build", "stages", "toolchain", "ty", "source"), "type", "package")
_register(("build", "stages", "toolchain", "ty", "update"), "provider", "stable_only")

for _prebuilt_stage, _tool_name in [("rtk-prebuilt", "rtk"), ("fd-prebuilt", "fd")]:
    _register(("build", "stages", _prebuilt_stage), _tool_name)
    _register(("build", "stages", _prebuilt_stage, _tool_name), "version", "source", "artifacts", "update")
    _register(("build", "stages", _prebuilt_stage, _tool_name, "source"), "type", "repository", "tag")
    _register(("build", "stages", _prebuilt_stage, _tool_name, "update"), "provider", "stable_only", "tag_prefix", "required_platforms")
    _register(("build", "stages", _prebuilt_stage, _tool_name, "artifacts", "__ANY__"), "url", "sha256")

for _npm_stage, _npm_name in [("openspec-tools", "openspec")]:
    _register(("build", "stages", _npm_stage), _npm_name)
    _register(("build", "stages", _npm_stage, _npm_name), "version", "source", "update")
    _register(("build", "stages", _npm_stage, _npm_name, "source"), "type", "package")
    _register(("build", "stages", _npm_stage, _npm_name, "update"), "provider", "stable_only")

_register(("build", "stages", "pi-tools"), "pi")
_register(("build", "stages", "pi-tools", "pi"), "version", "source", "update")
_register(("build", "stages", "pi-tools", "pi", "source"), "type", "package", "release_repository", "release_tag_prefix")
_register(("build", "stages", "pi-tools", "pi", "update"), "provider", "stable_only")

_register(("build", "stages", "runtime"), "oh-my-zsh")
_register(("build", "stages", "runtime", "oh-my-zsh"), "revision", "source", "update")
_register(("build", "stages", "runtime", "oh-my-zsh", "source"), "type", "repository")
_register(("build", "stages", "runtime", "oh-my-zsh", "update"), "provider", "ref")

# Runtime pi-extensions (dynamic — allowed keys defined per entry)
_register(("runtime",), "pi-extensions", "host-access")
_register(("runtime", "host-access"), "enabled", "mode", "proxy-port")
_register(("runtime", "pi-extensions", "__ANY__"), "version", "source", "update", "artifacts", "validation", "override")
_register(("runtime", "pi-extensions", "__ANY__", "source"), "type", "package")
_register(("runtime", "pi-extensions", "__ANY__", "artifacts", "__ANY__"), "url", "integrity")
_register(("runtime", "pi-extensions", "__ANY__", "update"), "provider", "stable_only")
_register(("runtime", "pi-extensions", "__ANY__", "validation"), "metadata_file")
_register(("runtime", "pi-extensions", "__ANY__", "override"), "constraint", "allow_prerelease", "scheme")


def _check_unknown_keys(table: Mapping[str, object], path: tuple[str, ...], *, allowed: set[str] | None = None) -> None:
    """Raise InventoryError if *table* contains keys not in the known set for *path*.

    The special key ``__ANY__`` in the registry matches any last component,
    which supports dynamic tables like pi-extensions.

    If *allowed* is given it serves as an explicit override — the registry
    lookup is skipped entirely.
    """
    if allowed is not None:
        for key in table:
            if key not in allowed:
                raise InventoryError(
                    f"{_dot(path + (key,))}: unknown key {key!r}",
                    field=_dot(path + (key,)),
                )
        return

    registry = _KNOWN_KEYS.get(path)
    if registry is None:
        # Try ancestor with __ANY__ wildcard in the last component
        for depth in range(len(path), 0, -1):
            candidate = path[:depth - 1] + ("__ANY__",) + path[depth:]
            registry = _KNOWN_KEYS.get(candidate)
            if registry is not None:
                break
    if registry is None:
        return  # No rule → skip (top-level or genuinely dynamic)

    for key in table:
        if key not in registry:
            raise InventoryError(
                f"{_dot(path + (key,))}: unknown key {key!r}",
                field=_dot(path + (key,)),
            )


def _parse_override_policy(
    override_data: Mapping[str, object], path_prefix: str
) -> OverridePolicy:
    constraint_str = require_string(override_data, ("constraint",))
    try:
        constraint = parse_constraint(constraint_str)
    except ConstraintSyntaxError:
        raise InventoryError(
            f"{path_prefix}.constraint: invalid constraint",
            field=f"{path_prefix}.constraint",
        )

    allow_prerelease = require_bool(override_data, ("allow_prerelease",))
    scheme = require_string(override_data, ("scheme",))

    if scheme not in ("numeric",):
        raise InventoryError(
            f"{path_prefix}.scheme: unsupported scheme {scheme!r}, only 'numeric' is allowed"
        , field=f"{path_prefix}.scheme")

    if scheme == "numeric" and allow_prerelease:
        raise InventoryError(
            f"{path_prefix}.allow_prerelease: numeric scheme does not support prereleases"
        , field=f"{path_prefix}.allow_prerelease")

    try:
        validate_constraint_consistency(constraint)
    except ConstraintSyntaxError:
        raise InventoryError(
            f"{path_prefix}.constraint: contradictory constraint",
            field=f"{path_prefix}.constraint",
        )

    return OverridePolicy(
        constraint=constraint,
        allow_prerelease=allow_prerelease,
        scheme=scheme,
    )


# ---------------------------------------------------------------------------
# Source / Update loader — explicit dispatch (no generic dict[str, type])
# ---------------------------------------------------------------------------

def _load_source(r: _PathReader, path: tuple[str, ...]) -> Any:
    """Parse a [*.source] table and return the typed source dataclass."""
    source_data = r.tbl(path + ("source",))
    stype = require_string(r.root, path + ("source", "type"))
    dot = _dot(path)

    if stype == "github-release":
        repository = require_nonempty_string(r.root, path + ("source", "repository"))
        tag = require_nonempty_string(r.root, path + ("source", "tag"))
        return GitHubReleaseSource(repository=repository, tag=tag)

    elif stype == "pi-release":
        package = require_nonempty_string(r.root, path + ("source", "package"))
        release_repository = require_nonempty_string(
            r.root, path + ("source", "release_repository")
        )
        release_tag_prefix = require_string(
            r.root, path + ("source", "release_tag_prefix")
        )
        if release_tag_prefix.startswith("/"):
            raise InventoryError(
                f"{dot}.source.release_tag_prefix: must not start with '/', "
                f"got {release_tag_prefix!r}"
            , field=f"{dot}.source.release_tag_prefix")
        return PiReleaseSource(
            package=package,
            release_repository=release_repository,
            release_tag_prefix=release_tag_prefix,
        )

    elif stype == "npm":
        package = require_nonempty_string(r.root, path + ("source", "package"))
        return NpmSource(package=package)

    elif stype == "pypi":
        package = require_nonempty_string(r.root, path + ("source", "package"))
        return PyPiSource(package=package)

    elif stype == "uv-python":
        impl = require_nonempty_string(r.root, path + ("source", "implementation"))
        if impl != "cpython":
            raise InventoryError(
                f"{dot}.source.implementation: unsupported implementation {impl!r}, only 'cpython' is allowed"
            , field=f"{dot}.source.implementation")
        return UvPythonSource(implementation=impl)

    elif stype == "rust-channel":
        manifest = require_nonempty_string(r.root, path + ("source", "manifest"))
        return RustChannelSource(manifest=manifest)

    elif stype == "docker-registry":
        registry = require_nonempty_string(r.root, path + ("source", "registry"))
        repository = require_nonempty_string(r.root, path + ("source", "repository"))
        return DockerRegistrySource(registry=registry, repository=repository)

    elif stype == "git":
        repository = require_nonempty_string(r.root, path + ("source", "repository"))
        return GitSource(repository=repository)

    elif stype == "static-url":
        checksum_url = require_nonempty_string(
            r.root, path + ("source", "checksum_url")
        )
        _validate_admissible_url(
            checksum_url, f"{dot}.source.checksum_url"
        )
        return StaticUrlSource(checksum_url=checksum_url)

    else:
        raise InventoryError(
            f"{dot}.source.type: unknown source type {stype!r}"
        , field=f"{dot}.source.type")


def _load_update(r: _PathReader, path: tuple[str, ...]) -> Any:
    """Parse a [*.update] table and return the typed update dataclass."""
    update_data = r.tbl(path + ("update",))
    provider = require_string(r.root, path + ("update", "provider"))
    dot = _dot(path)

    if provider == "github-release":
        stable_only = require_bool(r.root, path + ("update", "stable_only"))
        tag_prefix = update_data.get("tag_prefix", "")
        if not isinstance(tag_prefix, str):
            raise InventoryError(
                f"{dot}.update.tag_prefix: expected string, got {type(tag_prefix).__name__}"
            , field=f"{dot}.update.tag_prefix")
        rp_raw = update_data.get("required_platforms", [])
        if not isinstance(rp_raw, list) or not all(isinstance(x, str) for x in rp_raw):
            raise InventoryError(
                f"{dot}.update.required_platforms: expected list of strings"
            , field=f"{dot}.update.required_platforms")
        return GitHubReleaseUpdate(
            stable_only=stable_only,
            tag_prefix=tag_prefix,
            required_platforms=tuple(rp_raw),
        )

    elif provider == "npm":
        stable_only = require_bool(r.root, path + ("update", "stable_only"))
        return NpmUpdate(stable_only=stable_only)

    elif provider == "pypi":
        stable_only = require_bool(r.root, path + ("update", "stable_only"))
        return PyPiUpdate(stable_only=stable_only)

    elif provider == "uv-python":
        impl = require_nonempty_string(r.root, path + ("update", "implementation"))
        if impl != "cpython":
            raise InventoryError(
                f"{dot}.update.implementation: unsupported implementation {impl!r}, only 'cpython' is allowed"
            , field=f"{dot}.update.implementation")
        stable_only = require_bool(r.root, path + ("update", "stable_only"))
        return UvPythonUpdate(implementation=impl, stable_only=stable_only)

    elif provider == "rust-channel":
        channel = require_nonempty_string(r.root, path + ("update", "channel"))
        if channel != "stable":
            raise InventoryError(
                f"{dot}.update.channel: unsupported channel {channel!r}, only 'stable' is allowed"
            , field=f"{dot}.update.channel")
        stable_only = require_bool(r.root, path + ("update", "stable_only"))
        return RustChannelUpdate(channel=channel, stable_only=stable_only)

    elif provider == "docker-registry":
        stable_only = require_bool(r.root, path + ("update", "stable_only"))
        track = require_string(r.root, path + ("update", "track"))
        if track != "tag-digest":
            raise InventoryError(
                f"{dot}.update.track: unsupported track {track!r}, only 'tag-digest' is allowed"
            , field=f"{dot}.update.track")
        return DockerRegistryUpdate(stable_only=stable_only, track=track)

    elif provider == "git-ref":
        ref = require_nonempty_string(r.root, path + ("update", "ref"))
        return GitRefUpdate(ref=ref)

    elif provider == "static-url":
        stable_only = require_bool(r.root, path + ("update", "stable_only"))
        return StaticUrlUpdate(stable_only=stable_only)

    else:
        raise InventoryError(
            f"{dot}.update.provider: unknown provider {provider!r}"
        , field=f"{dot}.update.provider")


# ---------------------------------------------------------------------------
# Source/Update compatibility
# ---------------------------------------------------------------------------

_SOURCE_UPDATE_MAP = {
    "github-release": "github-release",
    "npm": "npm",
    "pi-release": "npm",
    "pypi": "pypi",
    "uv-python": "uv-python",
    "rust-channel": "rust-channel",
    "docker-registry": "docker-registry",
    "git": "git-ref",
    "static-url": "static-url",
}


def _check_compat(source_type: str, update_provider: str, dot: str) -> None:
    expected = _SOURCE_UPDATE_MAP.get(source_type)
    if expected != update_provider:
        raise InventoryError(
            f"{dot}.update.provider: provider {update_provider!r} "
            f"is incompatible with source type {source_type!r}"
        , field=f"{dot}.update.provider")


# ---------------------------------------------------------------------------
# Entry-specific mandated source/update types
# ---------------------------------------------------------------------------

# Each entry has exactly one legal source type and one legal update provider.
# This is stricter than generic compatibility: Python must be uv-python (not
# pypi even though pypi↔pypi is a valid pair), Rust must be rust-channel,
# etc.

_ENTRY_SOURCE_CLASSES = {
    "build.stages.base.node": (DockerRegistrySource, DockerRegistryUpdate),
    "build.stages.toolchain.rust": (RustChannelSource, RustChannelUpdate),
    "build.stages.toolchain.rust.rustup": (StaticUrlSource, StaticUrlUpdate),
    "build.stages.toolchain.uv": (GitHubReleaseSource, GitHubReleaseUpdate),
    "build.stages.toolchain.python": (UvPythonSource, UvPythonUpdate),
    "build.stages.toolchain.ty": (PyPiSource, PyPiUpdate),
    "build.stages.rtk-prebuilt.rtk": (GitHubReleaseSource, GitHubReleaseUpdate),
    "build.stages.fd-prebuilt.fd": (GitHubReleaseSource, GitHubReleaseUpdate),
    "build.stages.pi-tools.pi": (PiReleaseSource, NpmUpdate),
    "build.stages.openspec-tools.openspec": (NpmSource, NpmUpdate),
    "build.stages.runtime.oh-my-zsh": (GitSource, GitRefUpdate),
}


def _check_entry_source_update(
    source: Any, update: Any, dot: str
) -> None:
    """Verify that source and update objects match the mandated classes for *dot*."""
    expected = _ENTRY_SOURCE_CLASSES.get(dot)
    if expected is None:
        return  # pi-extensions handled separately
    exp_src_cls, exp_upd_cls = expected
    if type(source) is not exp_src_cls:
        actual_type = getattr(source, "type", type(source).__name__)
        raise InventoryError(
            f"{dot}.source.type: expected {exp_src_cls.type!r} for this entry, "
            f"got {actual_type!r}"
        , field=f"{dot}.source.type")
    if type(update) is not exp_upd_cls:
        actual_prov = getattr(update, "provider", type(update).__name__)
        raise InventoryError(
            f"{dot}.update.provider: expected {exp_upd_cls.provider!r} for this entry, "
            f"got {actual_prov!r}"
        , field=f"{dot}.update.provider")


# ---------------------------------------------------------------------------
# Artifact loading
# ---------------------------------------------------------------------------

def _load_artifacts(
    r: _PathReader, path: tuple[str, ...], version: str
) -> dict[str, ArtifactEntry]:
    dot = _dot(path)
    artifacts_data = r.tbl(path + ("artifacts",))
    _validate_linux_amd64_artifact(artifacts_data, dot)

    artifacts: dict[str, ArtifactEntry] = {}
    for platform in artifacts_data:
        # Validate platform artifact table keys (url + sha256 only, no typos like sh256)
        plat_path = path + ("artifacts", platform)
        _check_unknown_keys(r.tbl(plat_path), plat_path)
        art_url = r.str(path + ("artifacts", platform, "url"))
        art_sha256 = r.str(path + ("artifacts", platform, "sha256"))
        _validate_sha256(art_sha256, f"{dot}.artifacts.{platform}.sha256")
        _reject_placeholder_sha256(art_sha256, f"{dot}.artifacts.{platform}.sha256")
        _validate_url_contains_version(
            art_url, version, f"{dot}.artifacts.{platform}.url"
        )
        artifacts[platform] = ArtifactEntry(url=art_url, sha256=art_sha256)

    return artifacts


def _validate_required_platforms(
    upd: Any,
    artifacts: dict[str, ArtifactEntry],
    dot: str,
) -> None:
    if isinstance(upd, GitHubReleaseUpdate) and upd.required_platforms:
        for rp in upd.required_platforms:
            if rp not in artifacts:
                raise VersionConfigError(
                    f"{dot}.update.required_platforms: "
                    f"required platform {rp!r} has no artifact entry",
                    field=f"{dot}.update.required_platforms",
                )


# ---------------------------------------------------------------------------
# Inventory loader
# ---------------------------------------------------------------------------

def load_inventory_raw(versions_path: Path) -> dict[str, object]:
    """Load reviewed TOML through the shared document-validation boundary."""
    document = parse_configuration_document(
        DocumentIdentity(DocumentRole.REVIEWED, versions_path)
    )
    return dict(document.data)


def load_inventory(versions_path: Path) -> Inventory:
    """Load and validate a docker-constructor.toml inventory file."""
    identity = DocumentIdentity(DocumentRole.REVIEWED, versions_path)
    return release_owner_result(_load_reviewed_inventory(identity))


def _load_reviewed_inventory(
    identity: DocumentIdentity,
) -> ProjectedOwnerResult[Inventory]:
    """Validate the parsed reviewed document without retaining it on failure."""
    document = parse_configuration_document(identity)
    return capture_owner_result(
        identity, lambda: validate_inventory(dict(document.data))
    )


def load_project_configuration(
    inventory_path: Path,
    *,
    host_access_mode: str | None = None,
) -> tuple[Inventory, LocalConfig]:
    """Release reviewed and present local configuration only after both validate.

    This is the single command transaction: both fixed documents are routed
    through the shared configuration-document boundary, and every returned
    value comes from one parsed reviewed document and one parsed local
    document. When ``host_access_mode`` is not supplied, the inspected
    reviewed ``[runtime.host-access]`` mode is used so the local
    ``[host-access]`` owner can accept its reviewed ``docker-gateway``
    exception without a second reviewed parse.
    """
    reviewed = DocumentIdentity(DocumentRole.REVIEWED, inventory_path)
    companion = resolve_local_companion_path(inventory_path)
    local = DocumentIdentity(DocumentRole.LOCAL, companion)
    identities = (reviewed, local) if companion.exists() else (reviewed,)
    outcome = validate_configuration_documents(
        identities,
        lambda documents: _validate_project_documents(
            documents, reviewed, local, host_access_mode
        ),
    )
    return release_owner_result(outcome)


def _reviewed_host_access_mode(inventory: Inventory) -> str | None:
    """Return the reviewed host-access mode that governs local parsing."""
    policy = getattr(inventory.runtime, "host_access", None)
    if not getattr(policy, "enabled", False):
        return None
    mode = getattr(policy, "mode", None)
    return mode if isinstance(mode, str) else None


def _validate_project_documents(
    documents: tuple[ParsedConfigurationDocument, ...],
    reviewed: DocumentIdentity,
    local: DocumentIdentity,
    host_access_mode: str | None,
) -> ProjectedOwnerResult[tuple[Inventory, LocalConfig]]:
    """Validate both roles and return a safe outcome without raising.

    This frame holds the parsed documents, but it always returns an outcome, so
    it is never retained by the published owner-schema error.
    """
    by_role = {document.identity.role: document for document in documents}
    reviewed_outcome = capture_owner_result(
        reviewed,
        lambda: validate_inventory(dict(by_role[DocumentRole.REVIEWED].data)),
    )
    if reviewed_outcome.error is not None:
        return ProjectedOwnerResult(error=reviewed_outcome.error)
    inventory = reviewed_outcome.value
    assert inventory is not None
    effective_mode = host_access_mode
    if effective_mode is None:
        effective_mode = _reviewed_host_access_mode(inventory)
    if DocumentRole.LOCAL not in by_role:
        # Route absent-companion defaults through the aggregate validator so
        # each domain owner supplies its own default, exactly as for an
        # existing empty companion.
        local_config = validate_local_document(
            {}, host_access_mode=effective_mode
        )
        return ProjectedOwnerResult(value=(inventory, local_config))
    local_outcome = capture_owner_result(
        local,
        lambda: validate_local_document(
            dict(by_role[DocumentRole.LOCAL].data),
            host_access_mode=effective_mode,
        ),
    )
    if local_outcome.error is not None:
        return ProjectedOwnerResult(error=local_outcome.error)
    local_config = local_outcome.value
    assert local_config is not None
    return ProjectedOwnerResult(value=(inventory, local_config))


def resolve_corporate_trust_bundle_path(repository_root: Path | str) -> Path:
    """Return the fixed corporate trust source for a constructor project."""
    return Path(repository_root) / ".docker-local" / "corporate-ca-bundle.crt"


def _split_pem_lines(text: str) -> list[str]:
    """Split into lines using only PEM line breaks (LF or CRLF).

    ``str.splitlines`` would also treat other control characters such as
    vertical tab and form feed as line boundaries, silently removing them
    from payloads and defeating strict Base64 validation.
    """
    return [line.rstrip("\r") for line in text.split("\n")]


def _validate_pem_certificate_blocks(text: str, bundle: Path) -> None:
    """Require complete, decodable PEM certificate blocks.

    A valid corporate trust bundle must be one or more complete
    ``-----BEGIN CERTIFICATE-----`` / ``-----END CERTIFICATE-----``
    blocks.  Each delimiter must occupy its own line — a standalone PEM
    boundary line, not a substring of a longer line — with the Base64
    payload on the intervening lines.  Only PEM line breaks are permitted
    inside a payload; other whitespace and control characters are left in
    place for strict Base64 validation to reject.  Truncated blocks,
    unmatched delimiters, arbitrary text outside blocks, invalid Base64
    payloads, and empty payloads are rejected.  Bundle completeness beyond
    these PEM checks remains the operator's responsibility.
    """
    begin = PEM_CERTIFICATE_BEGIN
    end = PEM_CERTIFICATE_END

    payload_lines: list[str] = []
    in_block = False
    blocks = 0

    for line in _split_pem_lines(text):
        stripped = line.strip()
        if stripped == begin:
            if in_block:
                raise InventoryError(
                    f"corporate trust bundle {bundle} has nested or misordered "
                    f"PEM certificate delimiters"
                , field=None)
            in_block = True
            payload_lines = []
            blocks += 1
            continue
        if stripped == end:
            if not in_block:
                raise InventoryError(
                    f"corporate trust bundle {bundle} has unmatched PEM "
                    f"certificate delimiters"
                , field=None)
            compact = "".join(payload_lines)
            try:
                decoded = base64.b64decode(compact, validate=True)
            except Exception as exc:
                raise InventoryError(
                    f"corporate trust bundle {bundle} has an invalid Base64 "
                    f"certificate payload"
                , field=None) from exc
            if not decoded:
                raise InventoryError(
                    f"corporate trust bundle {bundle} has an empty certificate "
                    f"payload"
                , field=None)
            in_block = False
            payload_lines = []
            continue
        if in_block:
            if begin in stripped or end in stripped:
                raise InventoryError(
                    f"corporate trust bundle {bundle} has nested or misordered "
                    f"PEM certificate delimiters"
                , field=None)
            payload_lines.append(line)
            continue
        if stripped:
            raise InventoryError(
                f"corporate trust bundle {bundle} contains non-PEM content "
                f"outside certificate blocks"
            , field=None)

    if in_block:
        raise InventoryError(
            f"corporate trust bundle {bundle} has an unterminated PEM "
            f"certificate block"
        , field=None)
    if blocks == 0:
        raise InventoryError(
            f"corporate trust bundle {bundle} is not PEM certificate material"
        , field=None)


def validate_corporate_trust_bundle(path: Path | str) -> Path:
    """Validate the fixed corporate trust bundle and return its path.

    Raises ``InventoryError`` carrying the bundle path for a missing,
    unreadable, empty, or malformed bundle.
    """
    bundle = Path(path)
    if not bundle.is_file():
        raise InventoryError(
            f"corporate trust enabled but bundle {bundle} is missing; "
            f"create .docker-local/corporate-ca-bundle.crt"
        , field=None)
    try:
        data = bundle.read_bytes()
    except OSError as exc:
        raise InventoryError(
            f"corporate trust bundle {bundle} is unreadable: {exc}"
        , field=None) from exc
    if not data.strip():
        raise InventoryError(f"corporate trust bundle {bundle} is empty", field=None)
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError as exc:
        raise InventoryError(
            f"corporate trust bundle {bundle} is not PEM certificate material: "
            f"non-ASCII content"
        , field=None) from exc
    # Only space, tab, CR, and LF are legal PEM whitespace.  Other control
    # characters (vertical tab, form feed, NUL, DEL, …) are rejected here so
    # an enabled bundle fails at the command boundary instead of passing host
    # validation but failing later inside the Dockerfile's own strict
    # ASCII/control check.
    if any(
        (ord(ch) < 0x20 and ch not in "\t\r\n") or ord(ch) == 0x7F
        for ch in text
    ):
        raise InventoryError(
            f"corporate trust bundle {bundle} contains control characters; "
            f"use printable ASCII with space, tab, and LF/CRLF line breaks only"
        , field=None)
    _validate_pem_certificate_blocks(text, bundle)
    return bundle


def load_local_config(
    path: Path,
    *,
    host_access_mode: str | None = None,
) -> LocalConfig:
    """Load local TOML through the aggregate local-project boundary."""
    return load_local_project_configuration(path, host_access_mode=host_access_mode)


def load_local_config_for_inventory(
    inventory_path: Path,
    *,
    repository_root: Path | None = None,
    host_access_mode: str | None = None,
) -> LocalConfig:
    """Load only the fixed companion beside the selected project inventory."""
    del repository_root  # retained compatibility parameter; no fallback is allowed
    return load_optional_local_project_configuration(
        inventory_path, host_access_mode=host_access_mode
    )


def resolve_local_corporate_settings(
    inventory_path: Path,
    *,
    repository_root: Path | None = None,
    host_access_mode: str | None = None,
) -> LocalConfig:
    """Load and validate the complete local-companion schema.

    Unlike the earlier corporate-only parsing, this runs the same closed
    local-companion schema validation as :func:`load_local_config`, so unknown
    top-level keys, invalid ``[host-access]`` values, and invalid ``[cache]``
    values fail closed with an ``InventoryError`` before build/run execution.
    When corporate trust is enabled, the fixed constructor-project bundle is
    additionally validated before any Docker invocation. The selected root
    must be supplied by the build/run boundary and is never discovered.
    """
    local = load_optional_local_project_configuration(
        inventory_path, host_access_mode=host_access_mode
    )
    if local.corporate_trust.enabled:
        if repository_root is None:
            raise InventoryError(
                "corporate trust is enabled but project_root is absent; "
                "cannot resolve <project-root>/.docker-local/"
                "corporate-ca-bundle.crt"
            , field=None)
        validate_corporate_trust_bundle(
            resolve_corporate_trust_bundle_path(repository_root)
        )
    return local


def validate_inventory(raw: Mapping[str, object]) -> Inventory:
    """Validate raw TOML data and return an Inventory."""
    r = _PathReader(raw)

    # Schema
    if "schema" not in raw:
        raise InventoryError("schema: missing required top-level key", field="schema")
    schema = r.int(("schema",))

    if schema != 1:
        raise InventoryError(
            f"schema: unsupported version {schema}, only 1 is supported", field="schema"
        )

    # --- detect canonical or legacy layout ---
    has_build = "build" in raw
    has_stages = "stages" in raw

    if has_build and has_stages:
        raise InventoryError(
            "cannot use both 'build' and 'stages' top-level keys; "
            "move all content under [build.stages]", field="stages"
        )

    if has_build:
        # Canonical layout — build + runtime are required
        known_top = {"schema", "build", "runtime", "cache"}
        _check_unknown_keys(raw, (), allowed=known_top)

        if "runtime" not in raw:
            raise InventoryError("runtime: missing required key", field="runtime")
        # runtime must be a table, not a scalar
        if not isinstance(raw["runtime"], dict):
            raise InventoryError(
                f"runtime: expected table, got {type(raw['runtime']).__name__}"
            , field="runtime")

        build_raw = r.tbl(("build",))
        _check_unknown_keys(build_raw, ("build",), allowed={"stages"})
        if "stages" not in build_raw:
            raise InventoryError("build.stages: missing required key", field="build.stages")
    elif has_stages:
        raise InventoryError(
            "stages: unknown top-level key; "
            "move build dependencies under [build.stages]", field="stages"
        )
    else:
        raise InventoryError("build: missing required key — expected [build.stages] table", field="build")

    # --- base ---
    _check_unknown_keys(r.tbl(("build", "stages",)), ("build", "stages",))
    _check_unknown_keys(r.tbl(("build", "stages", "base",)), ("build", "stages", "base",))
    _check_unknown_keys(r.tbl(("build", "stages", "base", "node",)), ("build", "stages", "base", "node",))
    tag = r.str(("build", "stages", "base", "node", "tag"))
    digest = r.str(("build", "stages", "base", "node", "digest"))
    node_version = r.str(("build", "stages", "base", "node", "node_version"))
    npm_version = r.str(("build", "stages", "base", "node", "npm_version"))
    _validate_node_digest(digest, "build.stages.base.node.digest")
    _validate_npm_version(node_version, "build.stages.base.node.node_version")
    _validate_npm_version(npm_version, "build.stages.base.node.npm_version")
    node_source = _load_source(r, ("build", "stages", "base", "node"))
    node_update = _load_update(r, ("build", "stages", "base", "node"))
    _check_compat(node_source.type, node_update.provider, "build.stages.base.node")
    _check_entry_source_update(node_source, node_update, "build.stages.base.node")
    _check_unknown_keys(r.tbl(("build", "stages", "base", "node", "source",)), ("build", "stages", "base", "node", "source",))
    _check_unknown_keys(r.tbl(("build", "stages", "base", "node", "update",)), ("build", "stages", "base", "node", "update",))

    # --- toolchain: rust ---
    _check_unknown_keys(r.tbl(("build", "stages", "toolchain",)), ("build", "stages", "toolchain",))
    _check_unknown_keys(r.tbl(("build", "stages", "toolchain", "rust",)), ("build", "stages", "toolchain", "rust",))
    rust_version = r.str(("build", "stages", "toolchain", "rust", "version"))
    _validate_rust_version(rust_version, "build.stages.toolchain.rust.version")
    rust_profile = r.str(("build", "stages", "toolchain", "rust", "profile"))
    rust_data = r.tbl(("build", "stages", "toolchain", "rust"))
    components_raw = rust_data.get("components")
    if not isinstance(components_raw, list):
        raise InventoryError("build.stages.toolchain.rust.components: expected list", field="build.stages.toolchain.rust.components")
    components: list[str] = []
    for i, c in enumerate(components_raw):
        if not isinstance(c, str):
            raise InventoryError(
                f"build.stages.toolchain.rust.components[{i}]: expected string"
            , field=f"build.stages.toolchain.rust.components[{i}]")
        components.append(c)
    rust_source = _load_source(r, ("build", "stages", "toolchain", "rust"))
    rust_update = _load_update(r, ("build", "stages", "toolchain", "rust"))
    _check_compat(rust_source.type, rust_update.provider, "build.stages.toolchain.rust")
    _check_entry_source_update(rust_source, rust_update, "build.stages.toolchain.rust")
    # Mandatory rustup bootstrap artifact (platform-keyed, no version in URL)
    rustup_source = _load_source(r, ("build", "stages", "toolchain", "rust", "rustup"))
    rustup_update = _load_update(r, ("build", "stages", "toolchain", "rust", "rustup"))
    _check_compat(rustup_source.type, rustup_update.provider, "build.stages.toolchain.rust.rustup")
    _check_entry_source_update(rustup_source, rustup_update, "build.stages.toolchain.rust.rustup")
    # Per-platform artifact URLs are validated individually below.
    rustup_raw = r.tbl(("build", "stages", "toolchain", "rust", "rustup", "artifacts"))
    rustup_artifacts: dict[str, ArtifactEntry] = {}
    for platform in rustup_raw:
        plat_path = ("build", "stages", "toolchain", "rust", "rustup", "artifacts", platform)
        _check_unknown_keys(r.tbl(plat_path), plat_path)
        art_url = r.str(plat_path + ("url",))
        art_sha256 = r.str(plat_path + ("sha256",))
        _validate_sha256(
            art_sha256,
            f"build.stages.toolchain.rust.rustup.artifacts.{platform}.sha256",
        )
        _reject_placeholder_sha256(
            art_sha256,
            f"build.stages.toolchain.rust.rustup.artifacts.{platform}.sha256",
        )
        _validate_admissible_url(
            art_url,
            f"build.stages.toolchain.rust.rustup.artifacts.{platform}.url",
        )
        rustup_artifacts[platform] = ArtifactEntry(
            url=art_url, sha256=art_sha256,
        )
    _validate_linux_amd64_artifact(
        rustup_raw, "build.stages.toolchain.rust.rustup",
    )
    if rustup_source.type == "static-url" and len(rustup_artifacts) > 1:
        raise InventoryError(
            "build.stages.toolchain.rust.rustup: static-url sources do not support"
            " multi-platform artifacts — each architecture must declare its"
            " own checksum_url"
        , field="build.stages.toolchain.rust.rustup")
    _check_unknown_keys(r.tbl(("build", "stages", "toolchain", "rust", "source",)), ("build", "stages", "toolchain", "rust", "source",))
    _check_unknown_keys(r.tbl(("build", "stages", "toolchain", "rust", "update",)), ("build", "stages", "toolchain", "rust", "update",))
    _check_unknown_keys(r.tbl(("build", "stages", "toolchain", "rust", "rustup", "source",)), ("build", "stages", "toolchain", "rust", "rustup", "source",))
    _check_unknown_keys(r.tbl(("build", "stages", "toolchain", "rust", "rustup", "update",)), ("build", "stages", "toolchain", "rust", "rustup", "update",))
    _validate_url_contains_version(
        rust_source.manifest, rust_version, "build.stages.toolchain.rust.source.manifest"
    )

    # --- toolchain: uv ---
    _check_unknown_keys(r.tbl(("build", "stages", "toolchain", "uv",)), ("build", "stages", "toolchain", "uv",))
    uv_version = r.str(("build", "stages", "toolchain", "uv", "version"))
    _validate_uv_version(uv_version, "build.stages.toolchain.uv.version")
    uv_source = _load_source(r, ("build", "stages", "toolchain", "uv"))
    uv_update = _load_update(r, ("build", "stages", "toolchain", "uv"))
    _check_compat(uv_source.type, uv_update.provider, "build.stages.toolchain.uv")
    _check_entry_source_update(uv_source, uv_update, "build.stages.toolchain.uv")
    _check_unknown_keys(r.tbl(("build", "stages", "toolchain", "uv", "source",)), ("build", "stages", "toolchain", "uv", "source",))
    _check_unknown_keys(r.tbl(("build", "stages", "toolchain", "uv", "update",)), ("build", "stages", "toolchain", "uv", "update",))
    if isinstance(uv_source, GitHubReleaseSource) and uv_source.tag != uv_version:
        raise VersionConfigError(
            f"build.stages.toolchain.uv.source.tag: must equal declared version "
            f"({uv_source.tag!r} != {uv_version!r})",
            field="build.stages.toolchain.uv.source.tag",
        )
    uv_artifacts = _load_artifacts(r, ("build", "stages", "toolchain", "uv"), uv_version)
    _validate_required_platforms(uv_update, uv_artifacts, "build.stages.toolchain.uv")

    # --- toolchain: python ---
    _check_unknown_keys(r.tbl(("build", "stages", "toolchain", "python",)), ("build", "stages", "toolchain", "python",))
    py_version = r.str(("build", "stages", "toolchain", "python", "version"))
    try:
        parse_numeric_version(py_version)
    except VersionSyntaxError:
        raise InventoryError(
            "build.stages.toolchain.python.version: invalid numeric version",
            field="build.stages.toolchain.python.version",
        )
    py_source = _load_source(r, ("build", "stages", "toolchain", "python"))
    py_update = _load_update(r, ("build", "stages", "toolchain", "python"))
    _check_compat(py_source.type, py_update.provider, "build.stages.toolchain.python")
    _check_entry_source_update(py_source, py_update, "build.stages.toolchain.python")
    _check_unknown_keys(r.tbl(("build", "stages", "toolchain", "python", "source",)), ("build", "stages", "toolchain", "python", "source",))
    _check_unknown_keys(r.tbl(("build", "stages", "toolchain", "python", "update",)), ("build", "stages", "toolchain", "python", "update",))

    py_override: Optional[OverridePolicy] = None
    py_data = r.tbl(("build", "stages", "toolchain", "python"))
    if "override" in py_data:
        over_data = r.tbl(("build", "stages", "toolchain", "python", "override"))
        _check_unknown_keys(over_data, ("build", "stages", "toolchain", "python", "override",))
        py_override = _parse_override_policy(over_data, "build.stages.toolchain.python.override")
        py_ver = parse_numeric_version(py_version)
        if not py_override.constraint.matches(py_ver):
            raise VersionConfigError(
                f"build.stages.toolchain.python.version: {py_version} does not satisfy "
                f"override constraint '{py_override.constraint}'",
                field="build.stages.toolchain.python.version",
            )

    # --- toolchain: ty ---
    _check_unknown_keys(r.tbl(("build", "stages", "toolchain", "ty",)), ("build", "stages", "toolchain", "ty",))
    ty_version = r.str(("build", "stages", "toolchain", "ty", "version"))
    try:
        parse_numeric_version(ty_version)
    except VersionSyntaxError:
        raise InventoryError(
            "build.stages.toolchain.ty.version: invalid numeric version",
            field="build.stages.toolchain.ty.version",
        )
    ty_source = _load_source(r, ("build", "stages", "toolchain", "ty"))
    ty_update = _load_update(r, ("build", "stages", "toolchain", "ty"))
    _check_compat(ty_source.type, ty_update.provider, "build.stages.toolchain.ty")
    _check_entry_source_update(ty_source, ty_update, "build.stages.toolchain.ty")
    _check_unknown_keys(r.tbl(("build", "stages", "toolchain", "ty", "source",)), ("build", "stages", "toolchain", "ty", "source",))
    _check_unknown_keys(r.tbl(("build", "stages", "toolchain", "ty", "update",)), ("build", "stages", "toolchain", "ty", "update",))

    # --- prebuilt tools ---
    _check_unknown_keys(r.tbl(("build", "stages", "rtk-prebuilt",)), ("build", "stages", "rtk-prebuilt",))
    _check_unknown_keys(r.tbl(("build", "stages", "fd-prebuilt",)), ("build", "stages", "fd-prebuilt",))
    rtk_tool = _load_prebuilt_tool(r, ("build", "stages", "rtk-prebuilt", "rtk"))
    fd_tool = _load_prebuilt_tool(r, ("build", "stages", "fd-prebuilt", "fd"))

    # --- npm tools ---
    _check_unknown_keys(r.tbl(("build", "stages", "pi-tools",)), ("build", "stages", "pi-tools",))
    _check_unknown_keys(r.tbl(("build", "stages", "openspec-tools",)), ("build", "stages", "openspec-tools",))
    pi_tool = _load_pi_tool(r, ("build", "stages", "pi-tools", "pi"))
    openspec_tool = _load_npm_tool(r, ("build", "stages", "openspec-tools", "openspec"))

    # --- runtime ---
    _check_unknown_keys(r.tbl(("build", "stages", "runtime",)), ("build", "stages", "runtime",))
    _check_unknown_keys(r.tbl(("build", "stages", "runtime", "oh-my-zsh",)), ("build", "stages", "runtime", "oh-my-zsh",))
    omz_revision = r.str(("build", "stages", "runtime", "oh-my-zsh", "revision"))
    if not GIT_REVISION_RE.match(omz_revision):
        raise InventoryError(
            f"build.stages.runtime.oh-my-zsh.revision: expected 40 hex characters, got {omz_revision!r}"
        , field="build.stages.runtime.oh-my-zsh.revision")
    omz_source = _load_source(r, ("build", "stages", "runtime", "oh-my-zsh"))
    omz_update = _load_update(r, ("build", "stages", "runtime", "oh-my-zsh"))
    _check_compat(omz_source.type, omz_update.provider, "build.stages.runtime.oh-my-zsh")
    _check_entry_source_update(omz_source, omz_update, "build.stages.runtime.oh-my-zsh")
    _check_unknown_keys(r.tbl(("build", "stages", "runtime", "oh-my-zsh", "source",)), ("build", "stages", "runtime", "oh-my-zsh", "source",))
    _check_unknown_keys(r.tbl(("build", "stages", "runtime", "oh-my-zsh", "update",)), ("build", "stages", "runtime", "oh-my-zsh", "update",))

    # --- pi extensions and reviewed launch policy ---
    _check_unknown_keys(r.tbl(("runtime",)), ("runtime",))
    host_access = _load_host_access_policy(raw)
    pi_ext_data = r.tbl(("runtime", "pi-extensions"))
    extensions: dict[str, PiExtensionEntry] = {}
    _ext_identities: dict[tuple[str, str], str] = {}  # (family, identity) → first path
    for name, ext_raw in pi_ext_data.items():
        ext_path = f"runtime.pi-extensions.{name}"
        if not isinstance(ext_raw, dict):
            raise InventoryError(f"{ext_path}: expected table", field=ext_path)
        _check_unknown_keys(ext_raw, ("runtime", "pi-extensions", name,))

        # ── version ────────────────────────────────────────────────
        ext_version = require_string(raw, ("runtime", "pi-extensions", name, "version"))
        _validate_extension_version(
            ext_version, f"runtime.pi-extensions.{name}.version"
        )

        # ── source ─────────────────────────────────────────────────
        # Reject entry-level package — it belongs in source
        if "package" in ext_raw:
            raise InventoryError(
                f"runtime.pi-extensions.{name}.package: "
                f"entry-level 'package' is forbidden; use [*.source].package instead"
            , field=f"runtime.pi-extensions.{name}.package")
        ext_source = _load_source(r, ("runtime", "pi-extensions", name))
        _check_unknown_keys(r.tbl(("runtime", "pi-extensions", name, "source",)), ("runtime", "pi-extensions", name, "source",))
        if type(ext_source) is not NpmSource:
            raise InventoryError(
                f"runtime.pi-extensions.{name}.source.type: "
                f"expected 'npm' for pi extensions, got {ext_source.type!r}"
            , field=f"runtime.pi-extensions.{name}.source.type")

        # ── artifacts ──────────────────────────────────────────────
        ext_artifacts = _load_extension_artifacts(
            r, ("runtime", "pi-extensions", name), ext_source.package
        )
        if ext_version not in ext_artifacts:
            raise InventoryError(
                f"runtime.pi-extensions.{name}: default version {ext_version!r} "
                f"must have a matching entry in artifacts"
            , field=f"runtime.pi-extensions.{name}.version")

        # ── update ─────────────────────────────────────────────────
        ext_update = _load_update(r, ("runtime", "pi-extensions", name))
        _check_unknown_keys(r.tbl(("runtime", "pi-extensions", name, "update",)), ("runtime", "pi-extensions", name, "update",))
        _check_compat(ext_source.type, ext_update.provider, f"runtime.pi-extensions.{name}")
        _check_entry_source_update(
            ext_source, ext_update, f"runtime.pi-extensions.{name}"
        )
        if type(ext_update) is not NpmUpdate:
            raise InventoryError(
                f"runtime.pi-extensions.{name}.update.provider: "
                f"expected 'npm' for pi extensions, got {ext_update.provider!r}"
            , field=f"runtime.pi-extensions.{name}.update.provider")

        # ── validation ─────────────────────────────────────────────
        ext_validation = _load_extension_validation(
            r, ("runtime", "pi-extensions", name)
        )
        _check_unknown_keys(r.tbl(("runtime", "pi-extensions", name, "validation",)), ("runtime", "pi-extensions", name, "validation",))

        # ── override ───────────────────────────────────────────────
        if "override" not in ext_raw:
            raise InventoryError(
                f"runtime.pi-extensions.{name}.override: missing required section"
            , field=f"runtime.pi-extensions.{name}.override")
        ext_override = _parse_override_policy(
            r.tbl(("runtime", "pi-extensions", name, "override")),
            f"runtime.pi-extensions.{name}.override",
        )
        _check_unknown_keys(
            r.tbl(("runtime", "pi-extensions", name, "override")),
            ("runtime", "pi-extensions", name, "override"),
        )

        # ── version satisfies override constraint ─────────────────
        # Strip pre-release and build metadata for numeric comparison;
        # overrides use numeric scheme which operates on X.Y.Z only.
        numeric_ver = ext_version.split("-", 1)[0].split("+", 1)[0]
        try:
            ver = parse_numeric_version(numeric_ver)
        except VersionSyntaxError:
            raise InventoryError(
                f"runtime.pi-extensions.{name}.version: invalid numeric version",
                field=f"runtime.pi-extensions.{name}.version",
            )
        if not ext_override.constraint.matches(ver):
            raise InventoryError(
                f"runtime.pi-extensions.{name}: version does not satisfy override constraint",
                field=f"runtime.pi-extensions.{name}.version",
            )

        # ── duplicate detection ────────────────────────────────────
        ext_identity = _extension_identity(ext_source)
        if ext_identity in _ext_identities:
            raise InventoryError(
                f"duplicate npm package {ext_source.package!r}: "
                f"previously defined at {_ext_identities[ext_identity]}, "
                f"duplicate at runtime.pi-extensions.{name}"
            , field=f"runtime.pi-extensions.{name}.source.package")
        _ext_identities[ext_identity] = f"runtime.pi-extensions.{name}"

        extensions[name] = PiExtensionEntry(
            version=ext_version,
            source=ext_source,
            artifacts=ext_artifacts,
            update=ext_update,
            validation=ext_validation,
            override=ext_override,
        )

    # ── construct stages ──────────────────────────────────────────
    stages = Stages(
        base=BaseStage(
            node=NodeEntry(
                tag=tag, digest=digest, node_version=node_version,
                npm_version=npm_version, source=node_source, update=node_update,
            )
        ),
        toolchain=ToolchainStage(
            rust=RustEntry(
                version=rust_version, profile=rust_profile,
                components=tuple(components), source=rust_source, update=rust_update,
                rustup=MappingProxyType(rustup_artifacts),
                rustup_source=rustup_source, rustup_update=rustup_update,
            ),
            uv=UvEntry(
                version=uv_version,
                artifacts=MappingProxyType(uv_artifacts),
                source=uv_source, update=uv_update,
            ),
            python=PythonEntry(
                version=py_version, source=py_source, update=py_update, override=py_override,
            ),
            ty=TyEntry(version=ty_version, source=ty_source, update=ty_update),
        ),
        rtk_prebuilt=RtkPrebuiltStage(rtk=rtk_tool),
        fd_prebuilt=FdPrebuiltStage(fd=fd_tool),
        pi_tools=PiToolsStage(pi=pi_tool),
        openspec_tools=OpenSpecToolsStage(openspec=openspec_tool),
        runtime=RuntimeStage(
            oh_my_zsh=OhMyZshEntry(
                revision=omz_revision, source=omz_source, update=omz_update,
            )
        ),
    )

    # ── cross-phase duplicate detection ────────────────────────────
    _check_cross_phase_duplicates(stages, extensions)

    return Inventory(
        schema=schema,
        stages=stages,
        runtime_pi_extensions=MappingProxyType(extensions),
        cache=_load_cache_config(raw),
        host_access=host_access,
    )


# ---------------------------------------------------------------------------
# Cache config loader
# ---------------------------------------------------------------------------

def _load_host_access_policy(raw: Mapping[str, object]) -> HostAccessPolicy:
    runtime = raw.get("runtime")
    assert isinstance(runtime, dict)  # validated by validate_inventory
    policy = runtime.get("host-access")
    if policy is None:
        return HostAccessPolicy()
    if not isinstance(policy, dict):
        raise InventoryError("runtime.host-access: expected table", field="runtime.host-access")
    _check_unknown_keys(policy, ("runtime", "host-access"))
    enabled = policy.get("enabled")
    if not isinstance(enabled, bool):
        raise InventoryError("runtime.host-access.enabled: expected boolean", field="runtime.host-access.enabled")
    mode = policy.get("mode")
    port = policy.get("proxy-port")
    if not enabled:
        if mode is not None:
            raise InventoryError("runtime.host-access.mode: forbidden when enabled is false", field="runtime.host-access.mode")
        if port is not None:
            raise InventoryError("runtime.host-access.proxy-port: forbidden when enabled is false", field="runtime.host-access.proxy-port")
        return HostAccessPolicy(enabled=False)
    if mode not in ("docker-gateway", "external-address"):
        raise InventoryError("runtime.host-access.mode: expected 'docker-gateway' or 'external-address'", field="runtime.host-access.mode")
    if port is not None and (not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535):
        raise InventoryError("runtime.host-access.proxy-port: expected integer from 1 through 65535", field="runtime.host-access.proxy-port")
    return HostAccessPolicy(enabled=True, mode=mode, proxy_port=port)


def _load_cache_config(raw: Mapping[str, object]) -> CacheConfig | None:
    """Parse and validate the optional reviewed ``[cache]`` policy."""
    cache_raw = raw.get("cache")
    if cache_raw is None:
        return None
    if not isinstance(cache_raw, dict):
        raise InventoryError("cache: must be a table", field="cache")

    cache_ttl: int | None = None

    for key, val in cache_raw.items():
        if key == "dir":
            raise InventoryError(
                "cache.dir: retired reviewed field; move it to [cache].dir in the local companion"
            , field="cache.dir")
        elif key == "ttl":
            if not isinstance(val, int) or val <= 0:
                raise InventoryError("cache.ttl: must be a positive integer", field="cache.ttl")
            cache_ttl = val
        else:
            raise InventoryError(f"cache: unknown key {key!r}", field=f"cache.{key}")

    return CacheConfig(ttl=cache_ttl)


# ---------------------------------------------------------------------------
# Tool loaders
# ---------------------------------------------------------------------------

def _load_prebuilt_tool(r: _PathReader, path: tuple[str, ...]) -> PrebuiltToolEntry:
    _check_unknown_keys(r.tbl(path), path)
    version = r.str(path + ("version",))
    _validate_prebuilt_version(version, _dot(path) + ".version")
    src = _load_source(r, path)
    upd = _load_update(r, path)
    _check_compat(src.type, upd.provider, _dot(path))
    _check_entry_source_update(src, upd, _dot(path))
    _check_unknown_keys(r.tbl(path + ("source",)), path + ("source",))
    _check_unknown_keys(r.tbl(path + ("update",)), path + ("update",))
    if isinstance(src, GitHubReleaseSource) and src.tag != version:
        raise VersionConfigError(
            f"{_dot(path)}.source.tag: must equal declared version "
            f"({src.tag!r} != {version!r})",
            field=f"{_dot(path)}.source.tag",
        )
    artifacts = _load_artifacts(r, path, version)
    _validate_required_platforms(upd, artifacts, _dot(path))
    return PrebuiltToolEntry(
        version=version,
        artifacts=MappingProxyType(artifacts),
        source=src, update=upd,
    )


def _load_npm_tool(r: _PathReader, path: tuple[str, ...]) -> NpmToolEntry:
    _check_unknown_keys(r.tbl(path), path)
    version = r.str(path + ("version",))
    _validate_npm_version(version, _dot(path) + ".version")
    src = _load_source(r, path)
    upd = _load_update(r, path)
    _check_compat(src.type, upd.provider, _dot(path))
    _check_entry_source_update(src, upd, _dot(path))
    _check_unknown_keys(r.tbl(path + ("source",)), path + ("source",))
    _check_unknown_keys(r.tbl(path + ("update",)), path + ("update",))
    return NpmToolEntry(version=version, source=src, update=upd)


def _load_pi_tool(r: _PathReader, path: tuple[str, ...]) -> PiToolEntry:
    _check_unknown_keys(r.tbl(path), path)
    version = r.str(path + ("version",))
    _validate_npm_version(version, _dot(path) + ".version")
    src = _load_source(r, path)
    upd = _load_update(r, path)
    _check_compat(src.type, upd.provider, _dot(path))
    _check_entry_source_update(src, upd, _dot(path))
    _check_unknown_keys(r.tbl(path + ("source",)), path + ("source",))
    _check_unknown_keys(r.tbl(path + ("update",)), path + ("update",))
    if not isinstance(src, PiReleaseSource):
        raise InventoryError(
            f"{_dot(path)}.source.type: expected 'pi-release' for Pi, "
            f"got {src.type!r}"
        , field=f"{_dot(path)}.source.type")
    return PiToolEntry(version=version, source=src, update=upd)
