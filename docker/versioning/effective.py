"""Effective configuration: override application and deterministic serialization.

No Docker, no network, no subprocess.
"""
from __future__ import annotations

import dataclasses
import json
import tomllib
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional

from .constraints import Constraint, ConstraintClause, NumericVersion, parse_numeric_version
from .errors import (
    EffectiveConfigError,
    OverrideValidationError,
    UnknownPathError,
    UnsupportedOverrideError,
    VersionSyntaxError,
)
from .model import (
    BuildInventory,
    EffectiveArtifact,
    EffectiveBuildProjection,
    EffectiveNode,
    EffectivePiExtensionEntry,
    EffectivePiRelease,
    EffectiveRuntimeProjection,
    EffectiveRust,
    EffectiveTool,
    Inventory,
    NpmArtifact,
    PiExtensionEntry,
    PythonEntry,
    RuntimeInventory,
)
from .artifact_cache import SelectedArtifact
from .semver import SemverError, validate as _validate_semver
from docker.transactions.capabilities import (
    CapabilityError,
    DirectoryCapability,
)
from docker.transactions.cleanup import CleanupFailures
from docker.transactions.errors import (
    DestinationExists,
    TransactionError,
    carry_secondary_diagnostics,
)
from docker.transactions.posix import PosixFileOps
from docker.transactions.regular import RegularFileContracts

# ---------------------------------------------------------------------------
# Override register
# ---------------------------------------------------------------------------

_OverrideHandler = Any  # callable


def _apply_python_override(
    inventory: Inventory, value: str, path: str
) -> Inventory:
    entry = inventory.stages.toolchain.python
    if entry.override is None:
        raise UnsupportedOverrideError(
            f"{path}: no override policy configured"
        )
    policy = entry.override
    if policy.scheme != "numeric":
        raise UnsupportedOverrideError(
            f"{path}: override scheme is {policy.scheme!r}, expected 'numeric'"
        )

    # Parse
    try:
        candidate = parse_numeric_version(value)
    except VersionSyntaxError as exc:
        raise OverrideValidationError(
            f"{path}: {value!r} is not a valid X.Y.Z numeric version"
        ) from exc

    # Constraint check
    if not policy.constraint.matches(candidate):
        raise OverrideValidationError(
            f"{path}: {candidate} does not satisfy {policy.constraint}"
        )

    new_python = dataclasses.replace(entry, version=str(candidate))
    new_toolchain = dataclasses.replace(
        inventory.stages.toolchain, python=new_python
    )
    new_stages = dataclasses.replace(
        inventory.stages, toolchain=new_toolchain
    )
    return dataclasses.replace(inventory, stages=new_stages)


SUPPORTED_OVERRIDES: Mapping[
    str, Callable[[Inventory, str, str], Inventory]
] = {
    "build.stages.toolchain.python.version": _apply_python_override,
}


# ---------------------------------------------------------------------------
# Effective configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EffectiveConfiguration:
    """Immutable snapshot after override application."""
    inventory: Inventory
    overrides: Mapping[str, str]


def apply_overrides(
    inventory: Inventory,
    overrides: Mapping[str, str],
) -> EffectiveConfiguration:
    """Apply validated overrides and return the effective configuration."""
    result = inventory
    for path, value in overrides.items():
        handler = SUPPORTED_OVERRIDES.get(path)
        if handler is None:
            raise UnsupportedOverrideError(
                f"{path}: not an overrideable path"
            )
        result = handler(result, value, path)
    return EffectiveConfiguration(
        inventory=result,
        overrides=MappingProxyType(dict(overrides)),
    )


# ---------------------------------------------------------------------------
# Build projection resolver (Stage 4)
# ---------------------------------------------------------------------------

# Override paths supported in build projection resolution
# Supported platform identifiers for build projection resolution
_SUPPORTED_PLATFORMS: set[str] = {"linux-amd64", "linux-arm64"}

# Override paths supported in build projection resolution
_BUILD_OVERRIDE_HANDLERS: Mapping[str, str] = {
    "build.stages.toolchain.python.version": "python",
}


def resolve_build_projection(
    build: BuildInventory,
    overrides: Mapping[str, str],
    *,
    platform: str = "linux-amd64",
) -> EffectiveBuildProjection:
    """Resolve the effective build projection from reviewed build inventory.

    Applies validated overrides and returns a frozen projection containing
    only the fields required for Docker build construction and host-side
    verification.  No runtime extensions, update metadata, override policy,
    or operational state appear in the projection.

    *platform* selects the target architecture.  Must be one of
    ``"linux-amd64"`` or ``"linux-arm64"``.
    """
    if platform not in _SUPPORTED_PLATFORMS:
        raise EffectiveConfigError(
            f"Unsupported platform {platform!r}; "
            f"must be one of {sorted(_SUPPORTED_PLATFORMS)}"
        )
    effective_python = build.stages.toolchain.python.version

    for path, value in overrides.items():
        kind = _BUILD_OVERRIDE_HANDLERS.get(path)
        if kind is None:
            raise UnsupportedOverrideError(
                f"{path}: not an overrideable path"
            )
        if kind == "python":
            effective_python = _resolve_python_override(
                build.stages.toolchain.python, value, path
            )

    # Resolve base node image
    node_entry = build.stages.base.node
    registry = node_entry.source.registry.rstrip("/")
    node_image = f"{registry}/{node_entry.source.repository}:{node_entry.tag}@{node_entry.digest}"

    # Resolve Rust + rustup
    rust = build.stages.toolchain.rust

    def _resolve_artifact(artifacts, p: str, name: str):
        art = artifacts.get(p)
        if art is None:
            raise EffectiveConfigError(
                f"No artifact for platform {p!r} in {name} entry"
            )
        return EffectiveArtifact(url=art.url, sha256=art.sha256)

    uv = build.stages.toolchain.uv
    rtk = build.stages.rtk_prebuilt.rtk
    fd = build.stages.fd_prebuilt.fd

    return EffectiveBuildProjection(
        platform=platform,
        node=EffectiveNode(
            image=node_image,
            node_version=node_entry.node_version,
            npm_version=node_entry.npm_version,
        ),
        rust=EffectiveRust(
            version=rust.version,
            profile=rust.profile,
            components=tuple(rust.components),
            rustup=_resolve_artifact(rust.rustup, platform, "rustup"),
        ),
        uv=EffectiveTool(
            version=uv.version,
            artifact=_resolve_artifact(uv.artifacts, platform, "uv"),
        ),
        python_version=effective_python,
        ty_version=build.stages.toolchain.ty.version,
        rtk=EffectiveTool(
            version=rtk.version,
            artifact=_resolve_artifact(rtk.artifacts, platform, "rtk"),
        ),
        fd=EffectiveTool(
            version=fd.version,
            artifact=_resolve_artifact(fd.artifacts, platform, "fd"),
        ),
        pi_version=build.stages.pi_tools.pi.version,
        pi_release=EffectivePiRelease(
            package=build.stages.pi_tools.pi.source.package,
            release_repository=build.stages.pi_tools.pi.source.release_repository,
            release_tag_prefix=build.stages.pi_tools.pi.source.release_tag_prefix,
        ),
        openspec_version=build.stages.openspec_tools.openspec.version,
        oh_my_zsh_revision=build.stages.runtime.oh_my_zsh.revision,
    )


def _resolve_python_override(
    entry: PythonEntry,
    value: str,
    path: str,
) -> str:
    """Validate a Python version override and return the effective version."""
    if entry.override is None:
        raise UnsupportedOverrideError(
            f"{path}: no override policy configured"
        )
    policy = entry.override
    if policy.scheme != "numeric":
        raise UnsupportedOverrideError(
            f"{path}: override scheme is {policy.scheme!r}, expected 'numeric'"
        )

    try:
        candidate = parse_numeric_version(value)
    except VersionSyntaxError as exc:
        raise OverrideValidationError(
            f"{path}: {value!r} is not a valid X.Y.Z numeric version"
        ) from exc

    if not policy.constraint.matches(candidate):
        raise OverrideValidationError(
            f"{path}: {candidate} does not satisfy {policy.constraint}"
        )

    return str(candidate)


# ---------------------------------------------------------------------------
# TOML-name mapping
# ---------------------------------------------------------------------------

_TOML_NAMES: dict[str, str] = {
    "rtk_prebuilt": "rtk-prebuilt",
    "fd_prebuilt": "fd-prebuilt",
    "pi_tools": "pi-tools",
    "openspec_tools": "openspec-tools",
    "oh_my_zsh": "oh-my-zsh",
    "pi_extensions": "pi-extensions",
    "stable_only": "stable_only",
    "allow_prerelease": "allow_prerelease",
    "tag_prefix": "tag_prefix",
    "required_platforms": "required_platforms",
    "linux_amd64": "linux-amd64",
    "linux_arm64": "linux-arm64",
}


def _toml_name(field_name: str) -> str:
    return _TOML_NAMES.get(field_name, field_name)


# ---------------------------------------------------------------------------
# Deterministic serialization
# ---------------------------------------------------------------------------

_NATIVE_JSON = (str, int, float, bool, type(None))
_DOMAIN_PRIMITIVES = (Constraint, ConstraintClause, NumericVersion)
_DOMAIN_PRIMITIVE_NAMES = frozenset({
    "Constraint", "ConstraintClause", "NumericVersion",
})


def _is_domain_primitive(value: object) -> bool:
    """Recognize constraint values across both supported import paths.

    The CLI can load these modules as ``versioning.*`` while package tests use
    ``docker.versioning.*``.  Those paths create distinct class identities in
    one process, so an ``isinstance`` check alone is insufficient.
    """
    value_type = type(value)
    return isinstance(value, _DOMAIN_PRIMITIVES) or (
        value_type.__name__ in _DOMAIN_PRIMITIVE_NAMES
        and value_type.__module__.endswith(".constraints")
    )


def _is_inventory(value: object) -> bool:
    """Check if value is an Inventory without importing the class."""
    return dataclasses.is_dataclass(value) and type(value).__name__ == "Inventory"


def to_plain_data(value: Any) -> object:
    """Convert a frozen dataclass graph to JSON-serializable plain data.

    Uses TOML-style names (e.g. ``rtk-prebuilt`` instead of ``rtk_prebuilt``).
    MappingProxyType becomes dict; tuples become lists.
    Domain primitives (Constraint, NumericVersion) become canonical strings.
    """
    if isinstance(value, _NATIVE_JSON):
        return value
    if _is_domain_primitive(value):
        return str(value)
    if isinstance(value, (tuple, list)):
        return [to_plain_data(v) for v in value]
    if isinstance(value, Mapping):
        return {
            _toml_name(str(k)): to_plain_data(v)
            for k, v in value.items()
        }
    if _is_inventory(value):
        # Inventory: nest pi-extensions under runtime, not at top level
        result: dict[str, object] = {}
        for field in dataclasses.fields(value):
            if field.name.startswith("_"):
                continue
            v = getattr(value, field.name)
            if field.name == "runtime_pi_extensions":
                runtime = result.setdefault("runtime", {})
                if not isinstance(runtime, dict):
                    raise TypeError("runtime projection must be a mapping")
                runtime["pi-extensions"] = to_plain_data(v)
            elif field.name == "host_access":
                runtime = result.setdefault("runtime", {})
                if not isinstance(runtime, dict):
                    raise TypeError("runtime projection must be a mapping")
                runtime["host-access"] = to_plain_data(v)
            elif field.name == "stages":
                build = result.setdefault("build", {})
                if not isinstance(build, dict):
                    raise TypeError("build projection must be a mapping")
                build["stages"] = to_plain_data(v)
            else:
                result[_toml_name(field.name)] = to_plain_data(v)
        return result
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        result: dict[str, object] = {}
        for field in dataclasses.fields(value):
            if field.name.startswith("_"):
                continue
            v = getattr(value, field.name)
            # Round-trip: RustEntry.rustup is Mapping[str,ArtifactEntry] in the model
            # but the canonical TOML path is build.stages.toolchain.rust.rustup.artifacts.*
            # rustup_source / rustup_update are also sub-keys of rustup
            if field.name == "rustup":
                result[_toml_name(field.name)] = {"artifacts": to_plain_data(v)}
            elif field.name in ("rustup_source", "rustup_update"):
                target_key = "source" if field.name == "rustup_source" else "update"
                rustup = result.setdefault("rustup", {})
                if not isinstance(rustup, dict):
                    raise TypeError("rustup projection must be a mapping")
                rustup[target_key] = to_plain_data(v)
            else:
                result[_toml_name(field.name)] = to_plain_data(v)
        return result
    return str(value)


def serialize_effective_inventory(
    effective: EffectiveConfiguration,
) -> dict[str, object]:
    """Return canonical plain-data representation of the effective inventory."""
    result = to_plain_data(effective.inventory)
    if not isinstance(result, dict):
        raise TypeError("effective inventory must serialize to a mapping")
    return result


def get_path(
    effective: EffectiveConfiguration, path: str
) -> object:
    """Traverse the canonical plain data by dot-separated path.

    Raises UnknownPathError for invalid, empty, or special paths.
    """
    if not path or ".." in path:
        raise UnknownPathError(f"{path}: invalid path")
    if path.startswith("__") or path.endswith("__"):
        raise UnknownPathError(f"{path}: restricted path")

    data = to_plain_data(effective.inventory)

    # Special: root path
    if path == ".":
        return data

    segments = path.split(".")
    current: object = data
    for i, segment in enumerate(segments):
        if not segment:
            raise UnknownPathError(f"{path}: empty segment at position {i}")
        if segment.startswith("__") or segment.endswith("__"):
            raise UnknownPathError(f"{path}: restricted segment {segment!r}")
        if not isinstance(current, Mapping):
            raise UnknownPathError(
                f"{path}: {'.'.join(segments[:i])} is not a mapping"
            )
        if segment not in current:
            raise UnknownPathError(f"{path}: {segment!r} not found")
        current = current[segment]

    return current


# ---------------------------------------------------------------------------
# Runtime projection resolver (Stage 5)
# ---------------------------------------------------------------------------

_RUNTIME_OVERRIDE_PREFIX = "runtime.pi-extensions."
_RUNTIME_OVERRIDE_SUFFIX = ".version"


def resolve_runtime(
    runtime: RuntimeInventory,
    overrides: Mapping[str, str],
) -> tuple[list[SelectedArtifact], EffectiveRuntimeProjection]:
    """Resolve the effective runtime projection from reviewed runtime
    inventory.

    Returns a pair of:

    * **host materialization selections** — one
      :class:`SelectedArtifact` per unique selected integrity,
      carrying the exact reviewed URL and SRI integrity for
      download, verification, and caching.
    * **container-safe projection** — :class:`EffectiveRuntimeProjection`
      containing only the fields required for container-side
      installation (package identity, version, canonical
      ``artifact_id``, ``integrity``, and ``metadata_file``).

    Applies validated overrides.  No build entries, update/override
    policy, source metadata, or unselected artifacts appear in the
    projection.

    Raises:
        UnsupportedOverrideError: override path not recognised.
        OverrideValidationError: override value fails policy check.
        EffectiveConfigError: override version has no matching artifact.
    """
    extensions: dict[str, EffectivePiExtensionEntry] = {}
    selected: dict[str, SelectedArtifact] = {}

    for path in overrides:
        _validate_runtime_override_path(path, runtime)

    for name, entry in runtime.pi_extensions.items():
        effective_version = entry.version

        for path, value in overrides.items():
            expected = f"{_RUNTIME_OVERRIDE_PREFIX}{name}{_RUNTIME_OVERRIDE_SUFFIX}"
            if path == expected:
                effective_version = _validate_runtime_override(
                    entry, value, path
                )

        if effective_version not in entry.artifacts:
            raise EffectiveConfigError(
                f"runtime.pi-extensions.{name}: version {effective_version!r} "
                f"has no matching entry in artifacts"
            )

        artifact = entry.artifacts[effective_version]

        # Deduplicate by integrity: one SelectedArtifact per unique
        # integrity, even when multiple extensions share it.
        key = artifact.integrity
        if key not in selected:
            selected[key] = SelectedArtifact(
                url=artifact.url,
                integrity=artifact.integrity,
            )

        from .model import _derive_artifact_id
        artifact_id = _derive_artifact_id(artifact.integrity)

        extensions[name] = EffectivePiExtensionEntry(
            package=entry.source.package,
            version=effective_version,
            artifact_id=artifact_id,
            integrity=artifact.integrity,
            metadata_file=entry.validation.metadata_file,
        )

    # Deterministic ordering — insertion order of the inventory's
    # TOML layout must not affect ordering.
    extensions = {k: extensions[k] for k in sorted(extensions)}
    selected_list = [
        selected[k]
        for k in sorted(selected)
    ]
    return (
        selected_list,
        EffectiveRuntimeProjection(extensions=extensions),
    )


def _validate_runtime_override_path(path: str, runtime: RuntimeInventory) -> None:
    """Validate that *path* is a recognised runtime override."""
    if not path.startswith(_RUNTIME_OVERRIDE_PREFIX):
        raise UnsupportedOverrideError(
            f"{path}: override path must start with {_RUNTIME_OVERRIDE_PREFIX!r}"
        )
    if not path.endswith(_RUNTIME_OVERRIDE_SUFFIX):
        raise UnsupportedOverrideError(
            f"{path}: override path must end with {_RUNTIME_OVERRIDE_SUFFIX!r}"
        )
    # Extract extension name: drop prefix and suffix
    middle = path[len(_RUNTIME_OVERRIDE_PREFIX):-len(_RUNTIME_OVERRIDE_SUFFIX)]
    if not middle or middle not in runtime.pi_extensions:
        raise UnsupportedOverrideError(
            f"{path}: no such Pi extension {middle!r}"
        )


def _validate_runtime_override(
    entry: PiExtensionEntry,
    value: str,
    path: str,
) -> str:
    """Validate an override value against the extension's policy."""
    try:
        _validate_semver(value)
    except SemverError as exc:
        raise OverrideValidationError(
            f"{path}: {value!r} — {exc}"
        ) from exc

    policy = entry.override
    if policy.scheme != "numeric":
        raise OverrideValidationError(
            f"{path}: unsupported override scheme {policy.scheme!r}"
        )

    # Parse the numeric (X.Y.Z) portion for constraint matching.
    # Prerelease detection: any version string containing '-' before
    # a possible '+' build suffix is a prerelease.
    base_for_numeric = value.split("+", 1)[0]
    has_prerelease = "-" in base_for_numeric
    numeric_str = base_for_numeric.split("-", 1)[0]

    try:
        nv = parse_numeric_version(numeric_str)
    except Exception as exc:
        raise OverrideValidationError(
            f"{path}: {value!r} cannot be parsed as numeric version: {exc}"
        ) from exc

    if not policy.allow_prerelease and has_prerelease:
        raise OverrideValidationError(
            f"{path}: {value!r} has prerelease, but policy forbids it"
        )

    if policy.scheme != "numeric":
        raise OverrideValidationError(
            f"{path}: unsupported override scheme {policy.scheme!r}"
        )

    if not policy.constraint.matches(nv):
        raise OverrideValidationError(
            f"{path}: {value!r} does not satisfy constraint "
            f"{policy.constraint!r}"
        )

    return value


# ---------------------------------------------------------------------------
# Runtime projection lifecycle (Stage 5)
# ---------------------------------------------------------------------------

import hashlib
import os
import tempfile

# ── filesystem boundary ──────────────────────────────────────────────


class Filesystem:
    """Injectable filesystem boundary for lifecycle operations.

    Every lifecycle function that touches the host filesystem
    accepts a ``_fs`` parameter.  Tests inject a :class:`FakeFS`
    (backed by a ``dict[str, bytes]``) so the real filesystem is
    never mutated.

    Attributes:
        open: ``builtins.open`` equivalent.
        unlink: ``os.unlink`` equivalent.
        link: ``os.link`` equivalent.
        fsync: ``os.fsync`` equivalent.
        mkstemp: ``tempfile.mkstemp`` equivalent.
        chmod: ``os.chmod`` equivalent.
        urandom: ``os.urandom`` equivalent.
        path: ``os.path`` module (or substitute).
        makedirs: ``os.makedirs`` equivalent.
        close_fd: ``os.close`` equivalent.
        runtime_root: explicit external project-state runtime directory.
    """

    __slots__ = (
        "open", "unlink", "link", "fsync", "mkstemp",
        "chmod", "urandom", "path", "makedirs", "close_fd",
        "runtime_root", "ops",
    )

    def __init__(
        self,
        *,
        open=open,
        unlink=os.unlink,
        link=os.link,
        fsync=os.fsync,
        mkstemp=tempfile.mkstemp,
        chmod=os.chmod,
        urandom=os.urandom,
        path=os.path,
        makedirs=os.makedirs,
        close_fd=os.close,
        runtime_root=None,
        ops=None,
    ):
        self.open = open
        self.unlink = unlink
        self.link = link
        self.fsync = fsync
        self.mkstemp = mkstemp
        self.chmod = chmod
        self.urandom = urandom
        self.path = path
        self.makedirs = makedirs
        self.close_fd = close_fd
        self.runtime_root = runtime_root
        # Descriptor-relative L0 seam.  Secure publication delegates to the
        # shared L2 regular-file contracts through this object while the
        # facade keeps its path-oriented lifecycle/injection methods.
        self.ops = ops if ops is not None else PosixFileOps()


_DEFAULT_FS = Filesystem(runtime_root=None)

# ── serialized-projection schema constants ──────────────────────────

# Allowed top-level keys in the serialized runtime projection.
_SERIALIZED_TOP_KEYS = frozenset({"extensions"})

# Allowed per-extension keys.
_SERIALIZED_EXT_KEYS = frozenset({
    "package", "version", "artifact_id", "integrity", "metadata_file",
})

# Allowed SRI integrity algorithms and their digest byte-lengths.
# From the W3C Subresource Integrity spec: sha256 (32 B), sha384
# (48 B), sha512 (64 B).
_SRI_ALGORITHMS: dict[str, int] = {
    "sha256": 32,
    "sha384": 48,
    "sha512": 64,
}


def _validate_serialized_projection(data: object) -> None:
    """Validate the serialized runtime projection dict before writing.

    This is an independent closed-schema validator for the plain-dict
    representation that will be serialized to TOML.  It rejects:

    * unknown top-level or nested keys (host-only fields)
    * malformed integrity (not valid SRI: sha256/sha384/sha512)
    * invalid base64 payload or wrong digest length
    * unsafe ``metadata_file`` paths (absolute, ``..``, empty segments)
    * invalid/missing npm tarball URLs
    * empty string values
    * non-dict ``extensions``

    An empty extensions table is valid: launching without optional runtime
    extensions still requires a mounted, closed-schema projection.

    Raises:
        EffectiveConfigError: any invariant is violated.
    """
    if not isinstance(data, dict):
        raise EffectiveConfigError(
            "runtime projection must be a TOML table"
        )

    unknown = set(data) - _SERIALIZED_TOP_KEYS
    if unknown:
        raise EffectiveConfigError(
            f"unknown top-level key(s) {sorted(unknown)!r} "
            f"in serialized runtime projection"
        )

    extensions = data.get("extensions")
    if not isinstance(extensions, dict):
        raise EffectiveConfigError(
            "'extensions' must be a table in serialized runtime projection"
        )
    for ext_name, ext_val in extensions.items():
        if not isinstance(ext_val, dict):
            raise EffectiveConfigError(
                f"extensions.{ext_name}: must be a table"
            )

        unknown = set(ext_val) - _SERIALIZED_EXT_KEYS
        if unknown:
            raise EffectiveConfigError(
                f"extensions.{ext_name}: unknown key(s) "
                f"{sorted(unknown)!r}"
            )

        # ── required string fields ────────────────────────────
        for key in ("package", "version", "artifact_id", "integrity",
                     "metadata_file"):
            val = ext_val.get(key)
            if not isinstance(val, str) or not val:
                raise EffectiveConfigError(
                    f"extensions.{ext_name}.{key}: must be a "
                    f"non-empty string"
                )

        # ── metadata_file safety ──────────────────────────────
        _validate_metadata_file(
            ext_val["metadata_file"],
            prefix=f"extensions.{ext_name}",
        )

        # ── artifact_id safety ────────────────────────────────
        _validate_artifact_id(
            ext_val["artifact_id"],
            prefix=f"extensions.{ext_name}",
        )

        # ── integrity: SRI validation ────────────────────────
        _validate_integrity(
            ext_val["integrity"],
            prefix=f"extensions.{ext_name}",
        )

        # ── artifact_id must agree with integrity ─────────────
        from .model import _derive_artifact_id
        expected_id = _derive_artifact_id(ext_val["integrity"])
        if ext_val["artifact_id"] != expected_id:
            raise EffectiveConfigError(
                f"extensions.{ext_name}.artifact_id: "
                f"{ext_val['artifact_id']!r} does not match the "
                f"canonical identity {expected_id!r} derived from "
                f"integrity"
            )


def _validate_integrity(integrity: str, *, prefix: str) -> None:
    """Validate SRI integrity: ``alg-base64payload``.

    Supported algorithms: sha256 (32 B), sha384 (48 B), sha512 (64 B).
    The base64 payload is decoded strictly and the byte length must
    match the algorithm's digest size.  Invalid base64, wrong digest
    length, unsupported algorithm, and missing separator are all
    rejected.
    """
    if "-" not in integrity:
        raise EffectiveConfigError(
            f"{prefix}.integrity: must be 'alg-base64payload' "
            f"(e.g. 'sha512-...'), got {integrity!r}"
        )

    alg, payload = integrity.split("-", 1)
    expected_len = _SRI_ALGORITHMS.get(alg)
    if expected_len is None:
        raise EffectiveConfigError(
            f"{prefix}.integrity: unsupported algorithm {alg!r}; "
            f"must be one of {sorted(_SRI_ALGORITHMS)!r}"
        )

    import base64
    try:
        digest = base64.b64decode(payload, validate=True)
    except Exception as exc:
        raise EffectiveConfigError(
            f"{prefix}.integrity: invalid base64 payload: {exc}"
        ) from exc

    if len(digest) != expected_len:
        raise EffectiveConfigError(
            f"{prefix}.integrity: {alg} digest must be "
            f"{expected_len} bytes, got {len(digest)}"
        )


def _validate_metadata_file(value: str, *, prefix: str) -> None:
    """Validate that *value* is a safe relative metadata file path.

    Accepts ``package.json`` and other relative paths with no ``..``
    traversal and no empty segments.  Absolute paths are rejected.
    """
    if value.startswith("/"):
        raise EffectiveConfigError(
            f"{prefix}.metadata_file: absolute path {value!r} not allowed"
        )
    segments = value.split("/")
    if ".." in segments or "" in segments:
        raise EffectiveConfigError(
            f"{prefix}.metadata_file: {value!r} contains invalid segments"
        )
    if not value.strip():
        raise EffectiveConfigError(
            f"{prefix}.metadata_file: must be non-empty"
        )


def _validate_artifact_id(value: str, *, prefix: str) -> None:
    """Validate that *value* is a safe canonical artifact_id path.

    Must be a non-empty relative path consisting of
    ``<algorithm>/<urlsafe-base64>.tgz``.  No absolute paths,
    traversal (``..``), or empty segments are allowed.
    """
    if not value:
        raise EffectiveConfigError(
            f"{prefix}.artifact_id: must be a non-empty relative path"
        )
    if value.startswith("/"):
        raise EffectiveConfigError(
            f"{prefix}.artifact_id: absolute path {value!r} not allowed"
        )
    segments = value.split("/")
    if ".." in segments or "" in segments:
        raise EffectiveConfigError(
            f"{prefix}.artifact_id: {value!r} contains invalid segments"
        )


def _runtime_root(_fs: Filesystem | None = None) -> str:
    """Return the explicitly configured external runtime-projection root."""
    if _fs is None:
        _fs = _DEFAULT_FS
    if not _fs.runtime_root:
        raise EffectiveConfigError(
            "an explicit external project-state runtime root is required"
        )
    runtime_root = os.fspath(_fs.runtime_root)
    if not os.path.isabs(runtime_root):
        raise EffectiveConfigError("runtime projection root must be absolute")
    _fs.makedirs(runtime_root, exist_ok=True)
    return runtime_root


def _validate_safe_path(
    path: str,
    _fs: Filesystem | None = None,
) -> str:
    """Ensure *path* is safe for runtime projection lifecycle operations.

    Returns the real absolute path after successful validation.

    Raises:
        EffectiveConfigError: path is outside the configured external
            runtime root, contains ``..``, or traverses a symlink.
    """
    if _fs is None:
        _fs = _DEFAULT_FS
    runtime_dir = _runtime_root(_fs)

    # Resolve symlinks and relative components early.
    real = _fs.path.realpath(path)
    real_runtime = _fs.path.realpath(runtime_dir)

    common = _fs.path.commonpath([real, real_runtime])
    if common != real_runtime:
        raise EffectiveConfigError(
            f"runtime projection path {real!r} is outside "
            f"the configured external runtime root {real_runtime!r}"
        )

    if _fs.path.islink(path):
        raise EffectiveConfigError(
            f"runtime projection path {path!r} is a symlink, not allowed "
            f"for security"
        )

    return real


def _generate_projection_path(
    _fs: Filesystem | None = None,
) -> str:
    """Generate a unique non-existent filename beneath the configured
    external project-state runtime root.

    Unlike ``mkstemp`` this does **not** pre-create an empty file —
    the caller performs the atomic write later so the destination is
    never visible as an empty or partial file.
    """
    if _fs is None:
        _fs = _DEFAULT_FS
    runtime_dir = _runtime_root(_fs)
    # 16 random bytes → 32 hex chars gives enough uniqueness for
    # concurrent launches without pre-creating a file.
    token = _fs.urandom(16).hex()
    return _fs.path.join(
        runtime_dir,
        f"docker-constructor.runtime.{token}.toml",
    )


class RuntimeProjectionHandle:
    """Ownership handle for a private runtime projection file.

    The handle is a context manager that removes its file on exit
    unless :meth:`discard` was called.  Only the owning handle can
    remove its private file — each handle tracks its own path and
    will never remove a file it did not create.

    Usage::

        fs = Filesystem(runtime_root=external_project_state_runtime_root)
        with create_runtime_projection(proj, _fs=fs) as h:
            print(h.path, h.content_hash)
            # ... use the file ...
        # file is gone here

    Attributes:
        path: absolute path to the projection TOML file.
        content_hash: hex-encoded SHA-256 digest of the content.
    """

    __slots__ = ("_path", "_hash", "_fs", "_keep")

    def __init__(
        self,
        path: str,
        content_hash: str,
        *,
        _fs: Filesystem | None = None,
    ) -> None:
        self._path = path
        self._hash = content_hash
        self._fs = _fs if _fs is not None else _DEFAULT_FS
        self._keep = False

    # -- public read-only properties ---------------------------------

    @property
    def path(self) -> str:
        """Absolute path to the projection TOML file."""
        return self._path

    @property
    def content_hash(self) -> str:
        """Hex-encoded SHA-256 digest of the projection content."""
        return self._hash

    # -- lifecycle ---------------------------------------------------

    def discard(self) -> None:
        """Mark the file to be kept — skip automatic cleanup on exit."""
        self._keep = True

    def __iter__(self):
        """Unpack as ``(path, content_hash)`` — backward compat."""
        yield self._path
        yield self._hash

    def __enter__(self) -> "RuntimeProjectionHandle":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> bool | None:
        if not self._keep:
            try:
                self._fs.unlink(self._path)
            except FileNotFoundError:
                pass
        return None  # do not suppress exceptions


def create_runtime_projection(
    projection: EffectiveRuntimeProjection,
    *,
    host_path: str | None = None,
    _fs: Filesystem | None = None,
) -> RuntimeProjectionHandle:
    """Serialize *projection* to a private host file atomically.

    Returns a :class:`RuntimeProjectionHandle` that owns the file.
    The handle is a context manager — the file is removed on exit
    unless :meth:`~RuntimeProjectionHandle.discard` is called.

    The ``content_hash`` attribute holds a stable hex-encoded
    SHA-256 digest of the projection content.  The identity is
    computed before the file is written so callers can verify it
    without re-reading the file.

    The file is created atomically beneath the explicit external project-state
    runtime root configured on *_fs*. A caller may provide *host_path* within
    that root for deterministic testing. The destination must not already
    exist (no-clobber).  All bytes are written through buffered
    I/O to handle partial writes correctly.  Temporary files are
    cleaned up on every failure path.
    """
    if _fs is None:
        _fs = _DEFAULT_FS
    from .rendering import _write_toml

    # ── validate the projection DTO ──────────────────────────────
    # Trigger __post_init__ validation (closed-DTO enforcement).
    # Already validated during construction, but this guards against
    # misuse where a bare dict was cast to the type.
    EffectiveRuntimeProjection(
        **{k: v for k, v in projection.__dict__.items()
           if k != "extensions"},
        extensions=dict(projection.extensions),
    )

    # ── build canonical plain-data representation ────────────────
    import io

    data: dict[str, object] = {
        "extensions": {
            name: {
                "package": ext.package,
                "version": ext.version,
                "artifact_id": ext.artifact_id,
                "integrity": ext.integrity,
                "metadata_file": ext.metadata_file,
            }
            for name, ext in sorted(
                projection.extensions.items(), key=lambda kv: kv[0]
            )
        },
    }

    buf = io.StringIO()
    _write_toml(buf, data)
    # The generic writer omits empty inline tables. Runtime projections retain
    # the required top-level table even when no optional extensions are selected.
    if not projection.extensions:
        buf.write("[extensions]\n")
    content = buf.getvalue().encode("utf-8")

    # ── validate serialized projection before publication ──────────
    _validate_serialized_projection(data)

    # ── round-trip: re-parse generated TOML, re-validate ──────────
    # Catches escaping/serialization defects (control characters,
    # unescaped quotes, broken multi-line strings) that would
    # otherwise publish invalid or corrupted mounted TOML.
    try:
        parsed = tomllib.loads(content.decode("utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise EffectiveConfigError(
            f"generated runtime projection TOML is invalid: {exc}"
        ) from None
    _validate_serialized_projection(parsed)

    # ── stable content identity ──────────────────────────────────
    content_hash = hashlib.sha256(content).hexdigest()

    # ── atomic no-clobber write ───────────────────────────────
    if host_path is None:
        host_path = _generate_projection_path(_fs)

    _validate_safe_path(host_path, _fs)
    dirname = _fs.path.dirname(host_path)
    basename = _fs.path.basename(host_path)

    # ── atomic no-clobber write through the shared L2 contract ───
    # Publication runs descriptor-relative over the injected L0 backend: a
    # private sibling is established at the final ``0444`` mode and committed
    # with one atomic no-clobber link.  No pathname is reconstructed from the
    # open directory descriptor, and atomic publication makes no
    # parent-directory durability claim.  DTO validation, canonical
    # serialization, content identity, the lifecycle handle, and the domain
    # ``EffectiveConfigError`` diagnostics stay in this adapter.
    ops = _fs.ops
    directory: DirectoryCapability | None = None
    primary: BaseException | None = None
    try:
        directory = DirectoryCapability.from_secure_path(
            ops, dirname, label="runtime projection",
        )
        RegularFileContracts(ops).atomic_no_clobber(
            directory, basename, content, 0o444,
        )
    except DestinationExists as exc:
        primary = EffectiveConfigError(
            f"runtime projection {host_path!r} already exists; "
            f"refusing to overwrite another launch's projection"
        )
        raise primary from exc
    except TransactionError as exc:
        # Preserve the previous raw-``OSError`` behavior for every
        # non-collision operational failure while carrying any shared
        # cleanup diagnostics across the boundary.  An unsafe or unexpected
        # transaction failure with no raw cause keeps its own type.
        cause = exc.cause
        if isinstance(cause, OSError):
            carry_secondary_diagnostics(cause, exc)
            primary = cause
            raise cause
        primary = exc
        raise
    except CapabilityError as exc:
        # ``DirectoryCapability.from_secure_path`` wraps an operational
        # parent-directory open failure in a ``CapabilityError``.  Preserve
        # the previous raw-``OSError`` behavior for such failures (carrying
        # any attached cleanup diagnostics) while leaving a genuine
        # capability-validation error unchanged.
        cause = exc.__cause__
        if isinstance(cause, OSError):
            carry_secondary_diagnostics(cause, exc)
            primary = cause
            raise cause
        primary = exc
        raise
    except BaseException as exc:
        # A remaining capability-validation failure or a process-control
        # interruption propagates unchanged and is never translated into an
        # ordinary I/O failure.
        primary = exc
        raise
    finally:
        if directory is not None:
            # One close attempt only; a failed close is never retried because
            # POSIX does not guarantee the descriptor stays open.  An ordinary
            # close failure is secondary to a publication failure and is the
            # only failure when the publication succeeded; a process-control
            # interruption from close is authoritative and is never converted.
            failures = CleanupFailures(primary)
            failures.run(directory.close, ordinary=(OSError,))
            result = failures.complete()
            if result is not None:
                raise result

    return RuntimeProjectionHandle(host_path, content_hash, _fs=_fs)


def cleanup_runtime_projection(
    host_path: str,
    _fs: Filesystem | None = None,
) -> None:
    """Remove a private runtime projection file after launch.

    Only files beneath the explicit external project-state runtime root
    configured on *_fs* are eligible for removal. Symlinks and paths outside
    that root are rejected. Already-removed files succeed silently.

    .. deprecated::
        Prefer :class:`RuntimeProjectionHandle` as a context manager
        — it removes the file automatically on exit.
    """
    if _fs is None:
        _fs = _DEFAULT_FS
    _validate_safe_path(host_path, _fs)
    try:
        _fs.unlink(host_path)
    except FileNotFoundError:
        pass
