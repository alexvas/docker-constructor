"""Update discovery coordinator and suggestion rendering.

No network — delegates all HTTP/git through injected transports.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import (
    Callable,
    Mapping,
    Optional,
    Sequence,
)

from .errors import (
    VersionConfigError,
    ProviderUnavailableError,
)
from .model import (
    ArtifactEntry,
    CandidateArtifact,
    DockerRegistrySource,
    DockerRegistryUpdate,
    GitHubReleaseSource,
    GitHubReleaseUpdate,
    GitSource,
    GitRefUpdate,
    Inventory,
    NodeEntry,
    NpmSource,
    NpmUpdate,
    OhMyZshEntry,
    PiExtensionEntry,
    PrebuiltToolEntry,
    PythonEntry,
    PyPiSource,
    PyPiUpdate,
    RustChannelSource,
    RustChannelUpdate,
    RustEntry,
    StaticUrlUpdate,
    TyEntry,
    UvEntry,
    UvPythonSource,
    UvPythonUpdate,
    UpdateCandidate,
    UpdateKind,
    UpdateResult,
    UpdateStatus,
    UpdateTarget,
    OverridePolicy,
)
from .providers.base import (
    ProviderContext,
    ProviderResult,
    UpdateProvider,
)
from .providers.npm import NpmProvider
from .providers.pypi import PyPiProvider
from .providers.github import GitHubReleaseProvider
from .providers.rust import RustChannelProvider
from .providers.docker_registry import DockerRegistryProvider
from .providers.git import GitRefProvider
from .providers.uv_python import UvPythonProvider
from .providers.static_url import StaticUrlProvider


# ---------------------------------------------------------------------------
# Default provider registry
# ---------------------------------------------------------------------------

_DEFAULT_PROVIDERS: Mapping[str, UpdateProvider] = MappingProxyType(
    {
        "npm": NpmProvider(),
        "pypi": PyPiProvider(),
        "github-release": GitHubReleaseProvider(),
        "rust-channel": RustChannelProvider(),
        "docker-registry": DockerRegistryProvider(),
        "git-ref": GitRefProvider(),
        "uv-python": UvPythonProvider(),
        "static-url": StaticUrlProvider(),
    }
)


# ---------------------------------------------------------------------------
# Target traversal
# ---------------------------------------------------------------------------


class Scope(Enum):
    """Target scope for update discovery."""

    BUILD = "build"
    RUNTIME = "runtime"
    ALL = "all"


def build_update_targets(
    inventory: Inventory,
) -> tuple[UpdateTarget, ...]:
    """Flatten the inventory into a deterministic ordered tuple of update targets.

    Pi extensions are sorted by TOML key.
    """
    result: list[UpdateTarget] = []

    s = inventory.stages

    # base.node
    node = s.base.node
    result.append(
        UpdateTarget(
            path="build.stages.base.node",
            current=node.tag,
            source=node.source,
            update=node.update,
            artifacts={},
        )
    )

    # toolchain.rust
    result.append(
        UpdateTarget(
            path="build.stages.toolchain.rust",
            current=s.toolchain.rust.version,
            source=s.toolchain.rust.source,
            update=s.toolchain.rust.update,
            artifacts={},
        )
    )
    # toolchain.rust.rustup (static-url bootstrap artifact)
    result.append(
        UpdateTarget(
            path="build.stages.toolchain.rust.rustup",
            current=s.toolchain.rust.rustup.get(
                "linux-amd64", ArtifactEntry(url="", sha256="")
            ).sha256,
            source=s.toolchain.rust.rustup_source,
            update=s.toolchain.rust.rustup_update,
            artifacts=dict(s.toolchain.rust.rustup),
        )
    )

    # toolchain.uv
    result.append(
        UpdateTarget(
            path="build.stages.toolchain.uv",
            current=s.toolchain.uv.version,
            source=s.toolchain.uv.source,
            update=s.toolchain.uv.update,
            artifacts=dict(s.toolchain.uv.artifacts),
        )
    )

    # toolchain.python
    result.append(
        UpdateTarget(
            path="build.stages.toolchain.python",
            current=s.toolchain.python.version,
            source=s.toolchain.python.source,
            update=s.toolchain.python.update,
            artifacts={},
            override=s.toolchain.python.override,
        )
    )

    # toolchain.ty
    result.append(
        UpdateTarget(
            path="build.stages.toolchain.ty",
            current=s.toolchain.ty.version,
            source=s.toolchain.ty.source,
            update=s.toolchain.ty.update,
            artifacts={},
        )
    )

    # rtk-prebuilt
    result.append(
        UpdateTarget(
            path="build.stages.rtk-prebuilt.rtk",
            current=s.rtk_prebuilt.rtk.version,
            source=s.rtk_prebuilt.rtk.source,
            update=s.rtk_prebuilt.rtk.update,
            artifacts=dict(s.rtk_prebuilt.rtk.artifacts),
        )
    )

    # fd-prebuilt
    result.append(
        UpdateTarget(
            path="build.stages.fd-prebuilt.fd",
            current=s.fd_prebuilt.fd.version,
            source=s.fd_prebuilt.fd.source,
            update=s.fd_prebuilt.fd.update,
            artifacts=dict(s.fd_prebuilt.fd.artifacts),
        )
    )

    # pi-tools.pi
    result.append(
        UpdateTarget(
            path="build.stages.pi-tools.pi",
            current=s.pi_tools.pi.version,
            source=s.pi_tools.pi.source,
            update=s.pi_tools.pi.update,
            artifacts={},
        )
    )

    # openspec-tools.openspec
    result.append(
        UpdateTarget(
            path="build.stages.openspec-tools.openspec",
            current=s.openspec_tools.openspec.version,
            source=s.openspec_tools.openspec.source,
            update=s.openspec_tools.openspec.update,
            artifacts={},
        )
    )

    # runtime.oh-my-zsh
    result.append(
        UpdateTarget(
            path="build.stages.runtime.oh-my-zsh",
            current=s.runtime.oh_my_zsh.revision,
            source=s.runtime.oh_my_zsh.source,
            update=s.runtime.oh_my_zsh.update,
            artifacts={},
        )
    )

    # pi-extensions (sorted by key)
    for name in sorted(inventory.runtime_pi_extensions.keys()):
        ext = inventory.runtime_pi_extensions[name]
        result.append(
            UpdateTarget(
                path=f"runtime.pi-extensions.{name}",
                current=ext.version,
                source=ext.source,
                update=ext.update,
                artifacts={},
            )
        )

    return tuple(result)


# ---------------------------------------------------------------------------
# Replacement ownership and raw block evidence
# ---------------------------------------------------------------------------

_REPLACEMENT_OWNERS: Mapping[str, str] = MappingProxyType(
    {
        "build.stages.base.node": "build.stages.base.node",
        "build.stages.toolchain.rust": "build.stages.toolchain.rust",
        "build.stages.toolchain.rust.rustup": "build.stages.toolchain.rust",
        "build.stages.toolchain.uv": "build.stages.toolchain.uv",
        "build.stages.toolchain.python": "build.stages.toolchain.python",
        "build.stages.toolchain.ty": "build.stages.toolchain.ty",
        "build.stages.rtk-prebuilt.rtk": "build.stages.rtk-prebuilt.rtk",
        "build.stages.fd-prebuilt.fd": "build.stages.fd-prebuilt.fd",
        "build.stages.pi-tools.pi": "build.stages.pi-tools.pi",
        "build.stages.openspec-tools.openspec": "build.stages.openspec-tools.openspec",
        "build.stages.runtime.oh-my-zsh": "build.stages.runtime.oh-my-zsh",
    }
)


def replacement_owner(path: str) -> str:
    """Return the replaceable raw inventory block owning update target *path*.

    Every current update target maps to exactly one replaceable raw
    inventory block.  Rust and its nested rustup bootstrap share the
    ``build.stages.toolchain.rust`` block; Pi extensions each own their
    ``runtime.pi-extensions.<name>`` block.
    """
    owner = _REPLACEMENT_OWNERS.get(path)
    if owner is not None:
        return owner
    if path.startswith("runtime.pi-extensions."):
        return path
    raise KeyError(f"no replacement owner registered for update target {path!r}")


_RUNTIME_EXTENSIONS_PREFIX = "runtime.pi-extensions."


def path_segments(path: str) -> tuple[str, ...]:
    """Split a dotted canonical path into raw inventory key segments.

    ``runtime.pi-extensions.<name>`` is special-cased: everything after
    the fixed ``runtime.pi-extensions.`` prefix is a single extension
    name that may itself contain dots, matching the quoted TOML table key
    ``runtime.pi-extensions."<name>"``.  All other paths split on ``.``
    because their segments never contain dots.
    """
    if path.startswith(_RUNTIME_EXTENSIONS_PREFIX):
        return ("runtime", "pi-extensions", path[len(_RUNTIME_EXTENSIONS_PREFIX) :])
    return tuple(path.split("."))


def group_targets_by_owner(
    targets: Sequence[UpdateTarget],
) -> tuple[tuple[str, tuple[UpdateTarget, ...]], ...]:
    """Group update targets by replacement owner in first-appearance order.

    Deterministic: owners appear in the order their first target appeared
    in *targets*, and each owner appears exactly once (no overlapping
    owner blocks).
    """
    grouped: dict[str, list[UpdateTarget]] = {}
    order: list[str] = []
    for target in targets:
        owner = replacement_owner(target.path)
        if owner not in grouped:
            grouped[owner] = []
            order.append(owner)
        grouped[owner].append(target)
    return tuple((owner, tuple(grouped[owner])) for owner in order)


def extract_raw_block(
    raw: Mapping[str, object],
    segments: Sequence[str],
) -> dict[str, object]:
    """Return a deep copy of the raw reviewed inventory block at *segments*.

    *segments* are raw TOML key segments (e.g. ``("build", "stages",
    "toolchain", "rust")``).  Each segment is used as a literal table key,
    so extension names containing dots
    (``runtime.pi-extensions."foo.bar"``) are handled correctly.  The
    returned dict is a deep copy, so callers may overlay candidates
    without mutating the source inventory.
    """
    node: object = raw
    for part in segments:
        if not isinstance(node, Mapping):
            raise KeyError(f"raw inventory has no table at {segments!r}")
        if part not in node:
            raise KeyError(f"raw inventory missing key {segments!r}")
        node = node[part]
    if not isinstance(node, Mapping):
        raise KeyError(f"raw inventory node {segments!r} is not a table")
    return copy.deepcopy(dict(node))


def _overlay_key_path(
    block: dict[str, object],
    key_path: tuple[str, ...],
    value: object,
) -> None:
    """Set *value* at *key_path* within *block*, creating intermediate tables."""
    node: dict[str, object] = block
    for part in key_path[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[key_path[-1]] = value


def _target_suffix(owner: str, path: str) -> tuple[str, ...]:
    """Return the raw key path of *path* relative to owner *owner*."""
    owner_segments = path_segments(owner)
    target_segments = path_segments(path)
    if target_segments == owner_segments:
        return ()
    if (
        len(target_segments) <= len(owner_segments)
        or target_segments[: len(owner_segments)] != owner_segments
    ):
        raise KeyError(f"target {path!r} is not under owner {owner!r}")
    return target_segments[len(owner_segments) :]


def _repoint_rust_manifest(
    block: dict[str, object],
    suffix: tuple[str, ...],
    old_version: str,
    new_version: str,
) -> None:
    """Re-point a rust-channel ``source.manifest`` at the candidate version.

    The reviewed rust manifest URL embeds the toolchain version
    (``channel-rust-<version>.toml``) and the loader rejects a manifest whose
    URL no longer contains the declared ``version``.  Replace the old version
    substring in place so the complete replacement block reloads; any
    custom mirror host/path is preserved.
    """
    node: dict[str, object] = block
    for part in suffix:
        child = node.get(part)
        if not isinstance(child, dict):
            return
        node = child
    source = node.get("source")
    if not isinstance(source, dict):
        return
    manifest = source.get("manifest")
    if not isinstance(manifest, str) or old_version not in manifest:
        return
    new_manifest = manifest.replace(old_version, new_version)
    if new_version not in new_manifest:
        return
    _overlay_key_path(block, suffix + ("source", "manifest"), new_manifest)


def overlay_candidates(
    raw_block: Mapping[str, object],
    *,
    owner: str,
    targets: Sequence[UpdateTarget],
    results: Mapping[str, UpdateResult],
) -> dict[str, object]:
    """Return *raw_block* with applicable candidate leaves overlaid.

    Only ``OUTDATED`` + applicable results are applied.  The candidate's
    version / tag / digest / revision and artifact URL / checksum values
    replace the matching raw leaves; every other raw value (unchanged
    source, update policy, override, validation, and non-updated artifact
    platforms) is preserved verbatim.  The input is never mutated.
    """
    block = copy.deepcopy(dict(raw_block))

    for target in targets:
        result = results.get(target.path)
        if result is None:
            continue
        if result.status != UpdateStatus.OUTDATED or not result.applicable:
            continue
        if result.candidate is None:
            continue

        suffix = _target_suffix(owner, target.path)

        if result.kind == UpdateKind.VERSION:
            _overlay_key_path(block, suffix + ("version",), result.candidate)
            if result.provider == "github-release":
                _overlay_key_path(
                    block,
                    suffix + ("source", "tag"),
                    result.candidate,
                )
            elif result.provider == "rust-channel":
                _repoint_rust_manifest(
                    block,
                    suffix,
                    target.current,
                    result.candidate,
                )
        elif result.kind == UpdateKind.DIGEST_REFRESH:
            if result.artifacts:
                for platform, art in sorted(result.artifacts.items()):
                    if art.url:
                        _overlay_key_path(
                            block,
                            suffix + ("artifacts", platform, "url"),
                            art.url,
                        )
                    if art.sha256 is not None:
                        _overlay_key_path(
                            block,
                            suffix + ("artifacts", platform, "sha256"),
                            art.sha256,
                        )
            else:
                _overlay_key_path(block, suffix + ("digest",), result.candidate)
        elif result.kind == UpdateKind.REVISION:
            _overlay_key_path(block, suffix + ("revision",), result.candidate)

        # Artifact URL / checksum overlay for candidates carrying artifacts
        # (already handled above for DIGEST_REFRESH static-url refreshes).
        if result.artifacts and result.kind != UpdateKind.DIGEST_REFRESH:
            for platform, art in sorted(result.artifacts.items()):
                if art.url:
                    _overlay_key_path(
                        block,
                        suffix + ("artifacts", platform, "url"),
                        art.url,
                    )
                if art.sha256 is not None:
                    _overlay_key_path(
                        block,
                        suffix + ("artifacts", platform, "sha256"),
                        art.sha256,
                    )
                if art.integrity is not None:
                    _overlay_key_path(
                        block,
                        suffix + ("artifacts", platform, "integrity"),
                        art.integrity,
                    )

    return block


def build_replacement_blocks(
    raw: Mapping[str, object],
    targets: Sequence[UpdateTarget],
    results: Sequence[UpdateResult],
) -> tuple[tuple[str, dict[str, object]], ...]:
    """Build ``(owner, overlaid raw block)`` pairs for every owner with an
    applicable candidate.

    Owners are emitted in inventory order (first-appearance order of
    their targets).  Each block is the complete raw reviewed subtree for
    that owner with every applicable candidate leaf overlaid.

    Version-keyed npm Pi-extension tarball artifacts are re-keyed from the
    candidate's registry ``dist`` data, and the rust ``source.manifest`` is
    re-pointed at the candidate version, so every replacement block
    round-trips through ``load_inventory``.
    """
    applicable: dict[str, UpdateResult] = {}
    for result in results:
        if (
            result.status == UpdateStatus.OUTDATED
            and result.applicable
            and result.candidate is not None
        ):
            applicable[result.path] = result

    blocks: list[tuple[str, dict[str, object]]] = []
    for owner, group_targets in group_targets_by_owner(targets):
        if not any(target.path in applicable for target in group_targets):
            continue
        raw_block = extract_raw_block(raw, path_segments(owner))
        overlaid = overlay_candidates(
            raw_block,
            owner=owner,
            targets=group_targets,
            results=applicable,
        )
        blocks.append((owner, overlaid))

    return tuple(blocks)


# ---------------------------------------------------------------------------
# Deterministic complete-block TOML rendering
# ---------------------------------------------------------------------------

_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _format_toml_key(key: str) -> str:
    """Format a TOML key, quoting it when it is not a bare key."""
    if _BARE_KEY_RE.match(key):
        return key
    return '"' + key.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _format_toml_string(value: str) -> str:
    """Format a TOML basic string, escaping backslash/quote/control chars."""
    out: list[str] = []
    for ch in value:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _format_toml_value(value: object) -> str:
    """Format a scalar or scalar-array TOML value."""
    if isinstance(value, str):
        return _format_toml_string(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_format_toml_value(v) for v in value) + "]"
    raise TypeError(f"cannot serialize {type(value).__name__} as TOML: {value!r}")


def _format_table_header(segments: Sequence[str]) -> str:
    return "[" + ".".join(_format_toml_key(s) for s in segments) + "]"


def _emit_toml_table(
    segments: Sequence[str],
    table: Mapping[str, object],
    lines: list[str],
) -> None:
    """Append deterministic TOML lines for *table* at *segments*.

    Scalar leaves are emitted before nested tables (TOML closes a table
    once its first sub-table is opened), and both are sorted by key so the
    output is byte-stable for identical input data.
    """
    scalar_keys = [k for k in sorted(table) if not isinstance(table[k], Mapping)]
    table_keys = [k for k in sorted(table) if isinstance(table[k], Mapping)]

    if not scalar_keys and not table_keys:
        lines.append(_format_table_header(segments))
        return

    if scalar_keys:
        lines.append(_format_table_header(segments))
        for key in scalar_keys:
            lines.append(f"{_format_toml_key(key)} = {_format_toml_value(table[key])}")

    for key in table_keys:
        if lines and lines[-1] != "":
            lines.append("")
        nested = table[key]
        if not isinstance(nested, Mapping):
            raise TypeError("nested TOML value must be a mapping")
        _emit_toml_table(
            tuple(segments) + (key,),
            nested,
            lines,
        )


def display_path(path: str) -> str:
    """Return the human-facing display path for canonical *path*.

    Strips the leading ``build.stages.`` or ``runtime.`` prefix exactly
    once, whichever applies.  The full canonical path is retained in the
    TOML table headers emitted for a replacement block; the shortened form
    is used only for the visual ``# --- <display path> ---`` header.
    """
    for prefix in ("build.stages.", "runtime."):
        if path.startswith(prefix):
            return path[len(prefix) :]
    return path


def serialize_replacement_block(
    owner: str,
    block: Mapping[str, object],
) -> str:
    """Render one overlaid replacement block as deterministic TOML.

    The fragment uses the owner's full canonical table path, preserves every
    reviewed leaf (including nested ``source`` / ``update`` / ``artifacts`` /
    ``override`` / ``validation`` tables), and is built directly from the raw
    overlaid data — never from defaults-populated typed models.

    Rendering invariants (Phase 2 INTROSPECT):

    * Dotted keys — extension names and version-catalog keys — are quoted;
      bare ``[A-Za-z0-9_-]+`` keys are not.
    * Scalar leaves are emitted before nested tables (TOML closes a table
      once its first sub-table opens), and every level is key-sorted.
    * Array values render as single-line inline arrays (``["a", "b"]``).
    * A fragment is a standalone-parseable TOML snippet but is NOT a complete
      inventory: it omits ``schema`` and sibling blocks, so callers merge it
      into a full inventory before ``load_inventory`` (see round-trip tests).
    * The first line is the visual boundary ``# --- <display path> ---``
      (display-only; stripped by ``tomllib`` on reload).
    """
    lines: list[str] = [f"# --- {display_path(owner)} ---"]
    _emit_toml_table(path_segments(owner), block, lines)
    return "\n".join(lines) + "\n"


def render_replacement_fragments(
    raw: Mapping[str, object],
    targets: Sequence[UpdateTarget],
    results: Sequence[UpdateResult],
) -> str:
    """Render complete replacement TOML fragments for every applicable owner.

    Returns the concatenation of one ``serialize_replacement_block`` output
    per replaceable block that has at least one applicable candidate, in
    deterministic inventory order.  Returns ``""`` when nothing is
    applicable.
    """
    blocks = build_replacement_blocks(raw, targets, results)
    return "\n".join(
        serialize_replacement_block(owner, block) for owner, block in blocks
    )


# ---------------------------------------------------------------------------
# Coordination
# ---------------------------------------------------------------------------


def _classify_candidate(
    target: UpdateTarget,
    candidate: UpdateCandidate,
    current_entry_digest: str | None,
) -> tuple[UpdateStatus, UpdateKind, bool, str | None]:
    """Classify an update candidate for a target.

    Returns (status, kind, applicable, reason).
    """
    kind = UpdateKind.VERSION

    # npm Pi-extension candidates must carry a valid tarball URL and SRI
    # integrity.  The provider flags missing/invalid ``dist`` data on the
    # candidate; treat it as INCOMPLETE rather than an applicable
    # replacement, because a version-only block would fail the loader's
    # version-keyed artifacts check.
    if (
        isinstance(target.update, NpmUpdate)
        and target.path.startswith("runtime.pi-extensions.")
        and candidate.incomplete_reason is not None
    ):
        return (UpdateStatus.INCOMPLETE, kind, False, candidate.incomplete_reason)

    # Detect Docker digest refresh
    if isinstance(target.update, DockerRegistryUpdate):
        if target.update.track == "tag-digest":
            kind = UpdateKind.DIGEST_REFRESH
            # If provider returned the same value as current (tag), it's current
            if candidate.value == target.current:
                return (UpdateStatus.CURRENT, kind, False, None)
            # If no stored digest, can't compare
            if current_entry_digest is None:
                return (
                    UpdateStatus.SKIPPED,
                    kind,
                    False,
                    "no stored digest for comparison",
                )
            # If candidate matches stored digest, it's current
            if candidate.value == current_entry_digest:
                return (UpdateStatus.CURRENT, kind, False, None)
            # Different digest → refresh available
            return (UpdateStatus.OUTDATED, kind, True, None)

    # Detect git revision
    if isinstance(target.update, GitRefUpdate):
        kind = UpdateKind.REVISION
    elif isinstance(target.update, StaticUrlUpdate):
        kind = UpdateKind.DIGEST_REFRESH

    # Static-url: compare returned digest against stored artifact digest
    if isinstance(target.update, StaticUrlUpdate):
        if not target.artifacts:
            return (UpdateStatus.SKIPPED, kind, False, "no stored artifact digest")
        stored_digest = next(iter(target.artifacts.values())).sha256
        if candidate.value == stored_digest:
            return (UpdateStatus.CURRENT, kind, False, None)
        return (UpdateStatus.OUTDATED, kind, True, None)

    # Current check
    if candidate.value == target.current:
        return (UpdateStatus.CURRENT, kind, False, None)

    # Check override constraint for Python
    if target.override is not None and isinstance(target.source, UvPythonSource):
        from .constraints import parse_numeric_version

        try:
            cand_ver = parse_numeric_version(candidate.value)
        except Exception:
            return (
                UpdateStatus.INCOMPLETE,
                kind,
                False,
                f"candidate version {candidate.value!r} is not a valid numeric version",
            )

        if not target.override.constraint.matches(cand_ver):
            return (
                UpdateStatus.INCOMPLETE,
                kind,
                False,
                f"candidate {candidate.value} does not satisfy {target.override.constraint}",
            )

    # Check artifact completeness for GitHub releases
    if isinstance(target.source, GitHubReleaseSource) and isinstance(
        target.update, GitHubReleaseUpdate
    ):
        required = target.update.required_platforms
        for platform in required:
            art = candidate.artifacts.get(platform) if candidate.artifacts else None
            if art is None:
                return (
                    UpdateStatus.INCOMPLETE,
                    kind,
                    False,
                    f"missing required platform artifact: {platform}",
                )
            if not art.url:
                return (
                    UpdateStatus.INCOMPLETE,
                    kind,
                    False,
                    f"missing URL for platform artifact: {platform}",
                )
            if art.sha256 is None:
                return (
                    UpdateStatus.INCOMPLETE,
                    kind,
                    False,
                    f"missing SHA-256 for platform artifact: {platform}",
                )

    return (UpdateStatus.OUTDATED, kind, True, None)


@dataclass(frozen=True)
class UpdateProgressEvent:
    """Immutable snapshot of one selected target about to be resolved.

    Carries presentation-neutral discovery facts only: the one-based
    index, the total selected target count after scope and ``--only``
    filtering, the full target path, and the provider name.  It never
    carries terminal control, rendering, transport, timing, concurrency,
    or serialized-output state.
    """

    index: int
    total: int
    path: str
    provider: str


ProgressCallback = Callable[[UpdateProgressEvent], None]


def check_updates(
    inventory: Inventory,
    *,
    providers: Mapping[str, UpdateProvider] | None = None,
    context: ProviderContext,
    only: tuple[str, ...] = (),
    scope: Scope = Scope.ALL,
    progress: ProgressCallback | None = None,
) -> tuple[UpdateResult, ...]:
    """Discover updates for every target in *inventory*.

    Args:
        inventory: Validated inventory.
        providers: Provider registry (defaults to built-in providers).
        context: Transport and policy context.
        only: Optional filter — provider names or inventory paths.
        scope: Target scope — ``Scope.BUILD``, ``Scope.RUNTIME``, or
            ``Scope.ALL``.  Targets whose path does not start with the
            scope prefix are excluded before any ``--only`` filter is
            applied.
        progress: Optional synchronous callback invoked with an
            :class:`UpdateProgressEvent` immediately before each
            selected target is resolved.  Observational only: it does
            not affect results, ordering, or classification, and its
            failures are not treated as provider failures.
    """
    if providers is None:
        providers = _DEFAULT_PROVIDERS

    if not isinstance(scope, Scope):
        raise TypeError(f"scope must be a Scope, got {type(scope).__name__}")

    targets = build_update_targets(inventory)

    # Apply scope filter
    if scope == Scope.BUILD:
        targets = tuple(t for t in targets if t.path.startswith("build."))
    elif scope == Scope.RUNTIME:
        targets = tuple(t for t in targets if t.path.startswith("runtime."))

    # ── remember scoped target set for diagnostics ──────────────────
    _scoped_targets = targets
    _scoped_providers = sorted({t.update.provider for t in _scoped_targets})

    # Apply --only filter
    if only:
        filtered: list[UpdateTarget] = []
        for t in targets:
            for filt in only:
                if (
                    filt == t.update.provider
                    or t.path == filt
                    or t.path.startswith(filt)
                ):
                    filtered.append(t)
                    break
        targets = tuple(filtered)
        if not targets and only:
            # Gather known paths and provider names for a helpful
            # error message so users can spot typos.
            from .errors import UnknownFilterError

            _scoped_paths = sorted({t.path for t in _scoped_targets})
            raise UnknownFilterError(
                f"--only filter(s) matched nothing: "
                f"{', '.join(repr(f) for f in only)}"
                f"\n  Known paths (scope): {', '.join(_scoped_paths) or '(none)'}"
                f"\n  Known providers (scope): {', '.join(_scoped_providers) or '(none)'}"
            )

    # Resolve current_entry_digest for node
    node_digest = inventory.stages.base.node.digest

    results: list[UpdateResult] = []
    total = len(targets)
    for idx, target in enumerate(targets, start=1):
        provider_name = target.update.provider

        # Emit the start event before any provider lookup or network
        # work so observers see progress as it is about to happen.
        # Callback failures are intentionally not caught here: they are
        # observer errors, never provider failures.
        if progress is not None:
            progress(
                UpdateProgressEvent(
                    index=idx,
                    total=total,
                    path=target.path,
                    provider=provider_name,
                )
            )

        provider = providers.get(provider_name)

        if provider is None:
            results.append(
                UpdateResult(
                    path=target.path,
                    provider=provider_name,
                    current=target.current,
                    candidate=None,
                    status=UpdateStatus.SKIPPED,
                    kind=UpdateKind.VERSION,
                    applicable=False,
                    reason=f"no provider for {provider_name!r}",
                    artifacts={},
                )
            )
            continue

        # Call provider
        try:
            prov_result = provider.discover(target, context)
        except Exception as exc:
            results.append(
                UpdateResult(
                    path=target.path,
                    provider=provider_name,
                    current=target.current,
                    candidate=None,
                    status=UpdateStatus.UNAVAILABLE,
                    kind=UpdateKind.VERSION,
                    applicable=False,
                    reason=str(exc),
                    artifacts={},
                )
            )
            continue

        if prov_result.unavailable_reason is not None:
            results.append(
                UpdateResult(
                    path=target.path,
                    provider=provider_name,
                    current=target.current,
                    candidate=None,
                    status=UpdateStatus.UNAVAILABLE,
                    kind=UpdateKind.VERSION,
                    applicable=False,
                    reason=prov_result.unavailable_reason,
                    artifacts={},
                )
            )
            continue

        if prov_result.skipped_reason is not None:
            results.append(
                UpdateResult(
                    path=target.path,
                    provider=provider_name,
                    current=target.current,
                    candidate=None,
                    status=UpdateStatus.SKIPPED,
                    kind=UpdateKind.VERSION,
                    applicable=False,
                    reason=prov_result.skipped_reason,
                    artifacts={},
                )
            )
            continue

        candidate = prov_result.candidate
        if candidate is None:
            results.append(
                UpdateResult(
                    path=target.path,
                    provider=provider_name,
                    current=target.current,
                    candidate=None,
                    status=UpdateStatus.UNAVAILABLE,
                    kind=UpdateKind.VERSION,
                    applicable=False,
                    reason="provider returned no candidate",
                    artifacts={},
                )
            )
            continue

        # Classify
        current_entry_digest = (
            node_digest if target.path == "build.stages.base.node" else None
        )
        status, kind, applicable, reason = _classify_candidate(
            target, candidate, current_entry_digest
        )

        results.append(
            UpdateResult(
                path=target.path,
                provider=provider_name,
                current=target.current,
                candidate=candidate.value,
                status=status,
                kind=kind,
                applicable=applicable,
                reason=reason,
                artifacts=candidate.artifacts,
                digest=candidate.digest,
                published_at=candidate.published_at,
            )
        )

    return tuple(results)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


def render_table(results: Sequence[UpdateResult]) -> str:
    """Render a human-readable ASCII table of update results."""
    header = "PATH  PROVIDER  CURRENT  CANDIDATE  STATUS  KIND  APPLICABLE  DETAIL"
    lines = [header]

    for r in results:
        art_str = str(r.candidate) if r.candidate is not None else "-"
        applicable_str = "yes" if r.applicable else "no"
        detail = r.reason if r.reason else "-"
        line = f"{r.path}  {r.provider}  {r.current}  {art_str}  {r.status.value}  {r.kind.value}  {applicable_str}  {detail}"
        lines.append(line)

    return "\n".join(lines)


def serialize_results(
    results: Sequence[UpdateResult],
) -> list[dict[str, object]]:
    """Serialize update results to the canonical dict contract.

    Returns the full ``UpdateResult.to_dict()`` record for each result —
    including ``applicable``, ``kind``, ``reason``, and artifact metadata.
    Both the legacy CLI rendering and the read-only service consume this
    so the contract never drifts between the two consumers.
    """
    return [r.to_dict() for r in results]


def serialize_suggestions(
    results: Sequence[UpdateResult],
) -> list[dict[str, object]]:
    """Build structured non-mutating suggestions from update results.

    Only entries with ``status == OUTDATED and applicable == True`` are
    included.  Each entry is a ``{"path": …, "changes": {…}}`` dict
    suitable for rendering as TOML or JSON.
    """
    suggestions: list[dict[str, object]] = []
    for r in results:
        if r.status != UpdateStatus.OUTDATED or not r.applicable:
            continue
        if r.candidate is None:
            continue
        changes: dict[str, str] = {}
        if r.kind == UpdateKind.VERSION:
            changes["version"] = r.candidate
        elif r.kind == UpdateKind.DIGEST_REFRESH:
            changes["digest"] = r.candidate
        elif r.kind == UpdateKind.REVISION:
            changes["revision"] = r.candidate

        # Source tag only for GitHub releases (other providers have no tag field)
        if r.kind == UpdateKind.VERSION and r.provider == "github-release":
            changes["source.tag"] = r.candidate

        # Artifact changes.  Pi-extension tarball artifacts are text-only:
        # their version-keyed URL/integrity is applied to the complete
        # replacement fragment, but the structured JSON leaf-change contract
        # remains version-only.
        is_extension = r.path.startswith("runtime.pi-extensions.")
        for platform, art in sorted(r.artifacts.items()):
            if is_extension:
                continue
            if art.url:
                changes[f"artifacts.{platform}.url"] = art.url
            if art.sha256:
                changes[f"artifacts.{platform}.sha256"] = art.sha256

        suggestions.append({"path": r.path, "changes": changes})
    return suggestions


def render_json(results: Sequence[UpdateResult]) -> str:
    """Render update results as a canonical JSON array."""
    return json.dumps(
        {"results": serialize_results(results)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def render_suggestions_json(
    results: Sequence[UpdateResult],
) -> str:
    """Render JSON with both results and structured suggestions."""
    return json.dumps(
        {
            "results": serialize_results(results),
            "suggestions": serialize_suggestions(results),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
