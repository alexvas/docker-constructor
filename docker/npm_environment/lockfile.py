"""Closed ``package-lock.json`` v3 parsing and validation.

The assembler accepts only a strict, reviewed subset of the npm lock format:
registry-only HTTPS nodes with exact versions, optional valid SRI integrity,
safe ``node_modules`` placement, and supported dependency metadata.
Everything else — ``file:``/git/link/workspace/bundled nodes, traversal,
root drift, unsatisfied ranges, missing closure nodes, malformed metadata,
and unknown fields — is rejected *before* any network, Docker, cache, or
publication effect.

Accepted-field contract (Phase 1 introspection, task 1.7)
---------------------------------------------------------

Three node roles use separate closed accepted-field sets:

* Manifest node (``packages[""]``): ``name``, ``version``, ``dependencies``,
  ``devDependencies``, ``optionalDependencies``, ``peerDependencies``,
  ``peerDependenciesMeta``, plus accepted-and-ignored ``engines``,
  ``license``, ``funding``, ``deprecated`` (``_MANIFEST_NODE_FIELDS``).
* Reviewed-root node: the structural package fields plus functional ``bin``
  and ``engines`` and accepted-and-ignored ``license``, ``funding``,
  ``deprecated`` (``_REVIEWED_ROOT_NODE_FIELDS``).
* Transitive node: the same structural package fields plus accepted-and-
  ignored ``bin``, ``engines``, ``license``, ``funding``, ``deprecated``
  (``_TRANSITIVE_NODE_FIELDS``).

Top-level: ``lockfileVersion``, ``packages``, ``name``, ``version``,
``requires`` (``_TOP_LEVEL_FIELDS``).  Unsupported source/layout features
``link``, ``inBundle``, ``bundled``, ``workspaces`` are rejected
(``_REJECTED_FIELDS``).  ``libc`` platform constraints are also rejected in
this phase rather than silently ignored (``unsupported_libc``).  Any other
field is rejected with ``unsupported_field``.

Functional vs. ignored metadata
-------------------------------

For every reviewed root, ``bin`` and ``engines.node`` are validated
functional metadata preserved under the root's ``(package identity, lock
path)`` key.  Root ``bin`` values must be unambiguous safe relative paths.
For the manifest and every transitive node, ``bin``/``engines``/``license``/
``funding``/``deprecated`` are shape-validated then discarded and never
appear in any DTO.  A non-manifest registry node may omit ``integrity``
only with an exact version and a validated HTTPS registry URL; such nodes
are recorded explicitly as :class:`IntegrityLessNode`.

Optional-node omission model (Phase 1, task 1.3)
-----------------------------------------------

An optional node is omitted only when it is present in the lock closure and
its validated ``os``/``cpu`` constraints are inapplicable to the requested
platform, recorded as ``OmittedOptional`` with
``reason="platform-inapplicable"``.  An optional node absent from the
closure is rejected as ``missing_node``, because no lock-contained os/cpu
evidence proves it is platform-inapplicable.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Mapping, Sequence

from docker.versioning.integrity import (
    IntegrityError,
    validate_integrity,
)
from docker.versioning.npm_tarball import (
    NpmTarballUrlError,
    validate as _validate_tarball,
)
from docker.versioning.semver import (
    SemverError,
    validate as _validate_semver,
)

from .errors import LockedNpmError
from .model import (
    IntegrityLessNode,
    LockfileV3,
    LockPackage,
    OmittedOptional,
    ReviewedRootMetadata,
    RootSpec,
)
from .semver_range import NpmRangeError, parse_range, satisfies

# ── accepted-field whitelists (see the 1.7 introspection contract) ────

_TOP_LEVEL_FIELDS = frozenset(
    {"lockfileVersion", "packages", "name", "version", "requires"}
)

_MANIFEST_NODE_FIELDS = frozenset(
    {
        "name",
        "version",
        "dependencies",
        "devDependencies",
        "optionalDependencies",
        "peerDependencies",
        "peerDependenciesMeta",
        "engines",
        "license",
        "funding",
        "deprecated",
    }
)

_PACKAGE_STRUCTURAL_FIELDS = frozenset(
    {
        "name",
        "version",
        "resolved",
        "integrity",
        "dependencies",
        "optionalDependencies",
        "peerDependencies",
        "peerDependenciesMeta",
        "dev",
        "optional",
        "peer",
        "hasInstallScript",
        "os",
        "cpu",
    }
)

# Reviewed-root and transitive nodes share the same accepted structural
# fields but differ in how ``bin``/``engines`` are handled: functional and
# preserved for reviewed roots, accepted-and-ignored for transitive nodes.
# They remain separate closed sets so the 1.7 introspection can enumerate
# the three roles independently.
_REVIEWED_ROOT_NODE_FIELDS = _PACKAGE_STRUCTURAL_FIELDS | {
    "bin",
    "engines",
    "license",
    "funding",
    "deprecated",
}
_TRANSITIVE_NODE_FIELDS = _PACKAGE_STRUCTURAL_FIELDS | {
    "bin",
    "engines",
    "license",
    "funding",
    "deprecated",
}

# Fields that denote unsupported npm source/layout features and are
# rejected outright rather than ignored.
_REJECTED_FIELDS = frozenset({"link", "inBundle", "bundled", "workspaces"})

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._~-]*$")
_PLATFORM_RE = re.compile(r"^[a-z0-9]+-[a-z0-9]+$")


# ── platform helpers ───────────────────────────────────────────────────


def _split_platform(platform: str) -> tuple[str, str]:
    if not _PLATFORM_RE.match(platform):
        raise LockedNpmError(
            "invalid_platform",
            f"platform must be an os-arch token like 'linux-x64', got {platform!r}",
        )
    os_name, _, arch = platform.partition("-")
    return os_name, arch


def _check_list(actual: str, entries: tuple[str, ...] | None) -> bool:
    """Faithful port of npm's ``checkList`` platform predicate."""
    if entries is None:
        return True
    if len(entries) == 1 and entries[0] == "any":
        return True
    negated = 0
    match = False
    for entry in entries:
        negate = entry.startswith("!")
        test = entry[1:] if negate else entry
        if negate:
            negated += 1
            if actual == test:
                return False
        else:
            match = match or (actual == test)
    return match or negated == len(entries)


# ── name / path validation ─────────────────────────────────────────────


def _validate_name(name: str) -> None:
    if name.startswith("@"):
        if name.count("/") != 1:
            raise LockedNpmError("invalid_name", f"invalid package name {name!r}")
        scope, _, pkg = name.partition("/")
        scope_name = scope[1:]
        if not scope_name or not pkg:
            raise LockedNpmError("invalid_name", f"invalid package name {name!r}")
        if not _NAME_RE.match(scope_name) or not _NAME_RE.match(pkg):
            raise LockedNpmError("invalid_name", f"invalid package name {name!r}")
        return
    if not _NAME_RE.match(name):
        raise LockedNpmError("invalid_name", f"invalid package name {name!r}")


def _parse_path(path: str) -> tuple[str, str]:
    """Validate a ``packages`` key; return ``(parent_path, node_name)``.

    The empty string is the root (parent ``""``, name ``""``).  Every other
    key must be an alternating ``node_modules`` / package-name path with no
    traversal, absolute prefix, backslash, or empty segment.
    """
    if path == "":
        return "", ""
    if path.startswith("/") or path.endswith("/") or "//" in path or "\\" in path:
        raise LockedNpmError("unsafe_path", f"unsafe lock path {path!r}")
    segments = path.split("/")
    if segments[0] != "node_modules":
        raise LockedNpmError("unsafe_path", f"unsafe lock path {path!r}")

    names: list[str] = []
    i = 1
    while i < len(segments):
        seg = segments[i]
        if seg == "node_modules":
            raise LockedNpmError("unsafe_path", f"unsafe lock path {path!r}")
        if seg.startswith("@"):
            if i + 1 >= len(segments):
                raise LockedNpmError("unsafe_path", f"unsafe lock path {path!r}")
            name = f"{seg}/{segments[i + 1]}"
            i += 2
        else:
            name = seg
            i += 1
        names.append(name)
        if i < len(segments):
            if segments[i] != "node_modules":
                raise LockedNpmError("unsafe_path", f"unsafe lock path {path!r}")
            i += 1

    for name in names:
        _validate_name(name)

    if len(names) == 1:
        return "", names[0]
    parent_path = "node_modules/" + "/node_modules/".join(names[:-1])
    return parent_path, names[-1]


# ── dependency-map parsing ─────────────────────────────────────────────


def _parse_dep_map(value: object, path: str, field: str) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, Mapping):
        raise LockedNpmError(
            "malformed_lockfile",
            f"{field} at {path or '<root>'} must be an object",
        )
    pairs: list[tuple[str, str]] = []
    for raw_name, raw_range in value.items():
        if not isinstance(raw_name, str):
            raise LockedNpmError("invalid_name", f"invalid dependency key at {path!r}")
        _validate_name(raw_name)
        if not isinstance(raw_range, str) or not raw_range:
            raise LockedNpmError(
                "invalid_range",
                f"dependency {raw_name!r} at {path or '<root>'} "
                f"has invalid range {raw_range!r}",
            )
        try:
            parse_range(raw_range)
        except NpmRangeError as exc:
            raise LockedNpmError(
                "invalid_range",
                f"dependency {raw_name!r} at {path or '<root>'} "
                f"has unsupported range {raw_range!r}",
            ) from exc
        pairs.append((raw_name, raw_range))
    return tuple(sorted(pairs))


def _parse_platform_list(
    value: object, path: str, field: str
) -> tuple[str, ...] | None:
    if value is None:
        return None
    entries = [value] if isinstance(value, str) else value
    if not isinstance(entries, list):
        raise LockedNpmError(
            "malformed_lockfile", f"{field} at {path!r} must be a list"
        )
    out: list[str] = []
    for entry in entries:
        if not isinstance(entry, str) or not entry:
            raise LockedNpmError(
                "malformed_lockfile", f"{field} at {path!r} must be strings"
            )
        body = entry[1:] if entry.startswith("!") else entry
        if not body or any(ch.isspace() for ch in body):
            raise LockedNpmError(
                "malformed_lockfile", f"invalid {field} entry {entry!r} at {path!r}"
            )
        out.append(entry)
    return tuple(out)


# ── node parsing ───────────────────────────────────────────────────────


def _parse_peer_meta(value: object, path: str, peer_names: set[str]) -> tuple[str, ...]:
    """Parse ``peerDependenciesMeta`` into the sorted optional-peer names.

    npm accepts ``peerDependenciesMeta`` entries that do not correspond to a
    declared ``peerDependencies`` name (e.g. ``debug``'s optional
    ``supports-color``); such entries are shape-validated and ignored because
    there is no peer edge for them to make optional.
    """
    if value is None:
        return ()
    if not isinstance(value, Mapping):
        raise LockedNpmError(
            "malformed_lockfile",
            f"peerDependenciesMeta at {path or '<root>'} must be an object",
        )
    out: list[str] = []
    for raw_name, meta in value.items():
        if not isinstance(raw_name, str):
            raise LockedNpmError(
                "invalid_name", f"invalid peerDependenciesMeta key at {path!r}"
            )
        _validate_name(raw_name)
        if not isinstance(meta, Mapping):
            raise LockedNpmError(
                "malformed_lockfile",
                f"peerDependenciesMeta entry for {raw_name!r} at {path!r} "
                f"must be an object",
            )
        optional = meta.get("optional", False)
        if not isinstance(optional, bool):
            raise LockedNpmError(
                "malformed_lockfile",
                f"peerDependenciesMeta optional for {raw_name!r} at {path!r} "
                f"must be a boolean",
            )
        if raw_name in peer_names and optional:
            out.append(raw_name)
    return tuple(sorted(out))


def _reject_unsupported_fields(node: Mapping[str, object], path: str) -> None:
    if "libc" in node:
        raise LockedNpmError(
            "unsupported_libc",
            f"node at {path or '<root>'} declares 'libc', which is not "
            f"modeled in this phase",
        )
    for field in _REJECTED_FIELDS:
        value = node.get(field)
        if value:
            raise LockedNpmError(
                "unsupported_source",
                f"node at {path or '<root>'} uses unsupported {field!r} "
                f"source/layout feature",
            )


def _reject_unknown_fields(
    node: Mapping[str, object], path: str, allowed: frozenset[str]
) -> None:
    """Reject any field not explicitly accepted for this node kind."""
    unknown = sorted(set(node) - set(allowed))
    if unknown:
        raise LockedNpmError(
            "unsupported_field",
            f"unsupported field(s) {unknown!r} at {path or '<root>'}",
        )


# ── ignored-metadata validation ────────────────────────────────────────


def _validate_engines(value: object, path: str) -> str | None:
    """Validate ``engines`` and return the ``engines.node`` range, if any.

    ``engines`` is a closed object containing only optional ``node``; a
    present ``node`` must be a non-empty string in valid strict npm range
    syntax.  The returned range is later preserved only for reviewed roots.
    """
    if not isinstance(value, Mapping):
        raise LockedNpmError(
            "malformed_lockfile",
            f"engines at {path or '<root>'} must be an object",
        )
    unknown = set(value) - {"node"}
    if unknown:
        raise LockedNpmError(
            "unsupported_engine_key",
            f"engines at {path or '<root>'} declares unsupported key(s) "
            f"{sorted(unknown)!r}; only 'node' is modeled",
        )
    if "node" not in value:
        return None
    node_range = value["node"]
    if not isinstance(node_range, str) or not node_range:
        raise LockedNpmError(
            "invalid_engine_range",
            f"engines.node at {path or '<root>'} must be a non-empty string",
        )
    try:
        parse_range(node_range)
    except NpmRangeError as exc:
        raise LockedNpmError(
            "invalid_engine_range",
            f"engines.node at {path or '<root>'} has unsupported range {node_range!r}",
        ) from exc
    return node_range


def _validate_bin(value: object, path: str) -> tuple[tuple[str, str], ...]:
    """Validate the ``bin`` shape: an object of non-empty string pairs."""
    if not isinstance(value, Mapping):
        raise LockedNpmError(
            "malformed_lockfile", f"bin at {path or '<root>'} must be an object"
        )
    out: list[tuple[str, str]] = []
    for raw_name, raw_target in value.items():
        if not isinstance(raw_name, str) or not raw_name:
            raise LockedNpmError(
                "malformed_lockfile",
                f"bin key at {path or '<root>'} must be a non-empty string",
            )
        if not isinstance(raw_target, str) or not raw_target:
            raise LockedNpmError(
                "malformed_lockfile",
                f"bin target for {raw_name!r} at {path or '<root>'} "
                f"must be a non-empty string",
            )
        out.append((raw_name, raw_target))
    return tuple(sorted(out))


_BIN_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def _validate_bin_target(target: str, path: str) -> None:
    """Reject unsafe/ambiguous executable targets for a reviewed root.

    Targets must be unambiguous relative POSIX paths: no backslashes, no
    leading slash (absolute), no Windows drive prefix, no ``.``/``..``/empty
    components.  Transitive ``bin`` is shape-validated only and never
    reaches this check.
    """
    if "\\" in target or target.startswith("/") or _BIN_WINDOWS_DRIVE_RE.match(target):
        raise LockedNpmError(
            "unsafe_executable_path",
            f"reviewed-root bin target {target!r} at {path!r} is not a "
            f"safe relative path",
        )
    for segment in target.split("/"):
        if segment in ("", ".", ".."):
            raise LockedNpmError(
                "unsafe_executable_path",
                f"reviewed-root bin target {target!r} at {path!r} is not a "
                f"safe relative path",
            )


def _validate_string_field(value: object, field: str, path: str) -> None:
    if not isinstance(value, str):
        raise LockedNpmError(
            "malformed_lockfile",
            f"{field} at {path or '<root>'} must be a string",
        )


def _validate_funding_object(value: Mapping[str, object], path: str) -> None:
    unknown = set(value) - {"url", "type"}
    if unknown:
        raise LockedNpmError(
            "malformed_lockfile",
            f"funding object at {path or '<root>'} has unsupported key(s) "
            f"{sorted(unknown)!r}; only 'url' and 'type' are valid",
        )
    if "url" not in value or not isinstance(value["url"], str) or not value["url"]:
        raise LockedNpmError(
            "malformed_lockfile",
            f"funding object at {path or '<root>'} requires a non-empty string 'url'",
        )
    if "type" in value and (not isinstance(value["type"], str) or not value["type"]):
        raise LockedNpmError(
            "malformed_lockfile",
            f"funding 'type' at {path or '<root>'} must be a non-empty string",
        )


def _validate_funding(value: object, path: str) -> None:
    if isinstance(value, str):
        if not value:
            raise LockedNpmError(
                "malformed_lockfile",
                f"funding at {path or '<root>'} must be a non-empty string",
            )
        return
    if isinstance(value, Mapping):
        _validate_funding_object(value, path)
        return
    if isinstance(value, list):
        if not value:
            raise LockedNpmError(
                "malformed_lockfile",
                f"funding at {path or '<root>'} must be a non-empty array",
            )
        for entry in value:
            if isinstance(entry, str):
                if not entry:
                    raise LockedNpmError(
                        "malformed_lockfile",
                        f"funding entry at {path or '<root>'} must be a "
                        f"non-empty string",
                    )
            elif isinstance(entry, Mapping):
                _validate_funding_object(entry, path)
            else:
                raise LockedNpmError(
                    "malformed_lockfile",
                    f"funding entry at {path or '<root>'} must be a string or object",
                )
        return
    raise LockedNpmError(
        "malformed_lockfile",
        f"funding at {path or '<root>'} must be a string, object, or array",
    )


@dataclass(frozen=True)
class _ParsedNode:
    """Intermediate parse result: the DTO plus transient validated metadata.

    ``bin`` and ``engines_node`` are shape/syntax validated for every node;
    they are preserved only for reviewed roots and discarded elsewhere.
    ``integrity_less`` marks an accepted registry node that omitted SRI.
    """

    package: LockPackage
    bin: tuple[tuple[str, str], ...]
    engines_node: str | None
    integrity_less: bool


def _parse_node(
    path: str,
    node: Mapping[str, object],
    os_name: str,
    arch: str,
) -> _ParsedNode:
    _parent_path, derived_name = _parse_path(path)
    _reject_unsupported_fields(node, path)
    is_manifest = path == ""
    allowed = _MANIFEST_NODE_FIELDS if is_manifest else _REVIEWED_ROOT_NODE_FIELDS
    _reject_unknown_fields(node, path, allowed)

    # Accepted-and-ignored metadata is shape/syntax validated uniformly for
    # every role; only reviewed roots preserve ``bin``/``engines.node``.
    engines_node = (
        _validate_engines(node["engines"], path) if "engines" in node else None
    )
    bin_entries = _validate_bin(node["bin"], path) if "bin" in node else ()
    if "license" in node:
        _validate_string_field(node["license"], "license", path)
    if "deprecated" in node:
        _validate_string_field(node["deprecated"], "deprecated", path)
    if "funding" in node:
        _validate_funding(node["funding"], path)

    if is_manifest:
        name = node.get("name")
        if name is not None and not isinstance(name, str):
            raise LockedNpmError("malformed_lockfile", "root name must be a string")
        if isinstance(name, str):
            _validate_name(name)
        version = node.get("version")
        if version is not None and not isinstance(version, str):
            raise LockedNpmError("malformed_lockfile", "root version must be a string")
        deps = _parse_dep_map(node.get("dependencies"), path, "dependencies")
        opt_deps = _parse_dep_map(
            node.get("optionalDependencies"), path, "optionalDependencies"
        )
        dev_deps = _parse_dep_map(node.get("devDependencies"), path, "devDependencies")
        peer_deps = _parse_dep_map(
            node.get("peerDependencies"), path, "peerDependencies"
        )
        optional_peers = _parse_peer_meta(
            node.get("peerDependenciesMeta"), path, {n for n, _ in peer_deps}
        )
        return _ParsedNode(
            package=LockPackage(
                path="",
                name=name if isinstance(name, str) else "",
                version=version if isinstance(version, str) else "",
                resolved="",
                integrity="",
                dependencies=deps,
                optional_dependencies=opt_deps,
                dev_dependencies=dev_deps,
                peer_dependencies=peer_deps,
                optional_peers=optional_peers,
                dev=False,
                optional=False,
                peer=False,
                has_install_script=False,
                os=None,
                cpu=None,
                applicable=True,
            ),
            bin=(),
            engines_node=None,
            integrity_less=False,
        )

    # Non-root package node.
    if "name" in node:
        node_name = node.get("name")
        if node_name != derived_name:
            raise LockedNpmError(
                "invalid_placement",
                f"node at {path!r} declares name {node_name!r} "
                f"but its path implies {derived_name!r}",
            )

    raw_version = node.get("version")
    if not isinstance(raw_version, str) or not raw_version:
        raise LockedNpmError(
            "invalid_version", f"node at {path!r} is missing an exact version"
        )
    try:
        _validate_semver(raw_version)
    except SemverError as exc:
        raise LockedNpmError(
            "invalid_version", f"node at {path!r} has invalid version {raw_version!r}"
        ) from exc

    raw_resolved = node.get("resolved")
    if not isinstance(raw_resolved, str) or not raw_resolved:
        raise LockedNpmError(
            "invalid_resolved", f"node at {path!r} is missing a resolved URL"
        )
    try:
        _validate_tarball(raw_resolved, derived_name, raw_version)
    except NpmTarballUrlError as exc:
        raise LockedNpmError(
            "invalid_resolved", f"node at {path!r} has unsafe URL {raw_resolved!r}"
        ) from exc

    # ``integrity`` is optional: a present value must be valid SRI; an
    # absent value is accepted only because the node has an exact version
    # and a validated HTTPS registry URL (already checked above) and is
    # recorded explicitly as integrity-less.
    if "integrity" in node:
        raw_integrity = node["integrity"]
        if not isinstance(raw_integrity, str) or not raw_integrity:
            raise LockedNpmError(
                "invalid_integrity",
                f"node at {path!r} has invalid SRI integrity {raw_integrity!r}",
            )
        try:
            validate_integrity(raw_integrity)
        except IntegrityError as exc:
            raise LockedNpmError(
                "invalid_integrity",
                f"node at {path!r} has invalid SRI integrity {raw_integrity!r}",
            ) from exc
        integrity = raw_integrity
        integrity_less = False
    else:
        integrity = ""
        integrity_less = True

    os_field = _parse_platform_list(node.get("os"), path, "os")
    cpu_field = _parse_platform_list(node.get("cpu"), path, "cpu")

    applicable = _check_list(os_name, os_field) and _check_list(arch, cpu_field)

    def flag(field: str) -> bool:
        value = node.get(field, False)
        if not isinstance(value, bool):
            raise LockedNpmError(
                "malformed_lockfile", f"{field} at {path!r} must be a boolean"
            )
        return value

    peer_deps = _parse_dep_map(node.get("peerDependencies"), path, "peerDependencies")
    return _ParsedNode(
        package=LockPackage(
            path=path,
            name=derived_name,
            version=raw_version,
            resolved=raw_resolved,
            integrity=integrity,
            dependencies=_parse_dep_map(node.get("dependencies"), path, "dependencies"),
            optional_dependencies=_parse_dep_map(
                node.get("optionalDependencies"), path, "optionalDependencies"
            ),
            dev_dependencies=(),
            peer_dependencies=peer_deps,
            optional_peers=_parse_peer_meta(
                node.get("peerDependenciesMeta"), path, {n for n, _ in peer_deps}
            ),
            dev=flag("dev"),
            optional=flag("optional"),
            peer=flag("peer"),
            has_install_script=flag("hasInstallScript"),
            os=os_field,
            cpu=cpu_field,
            applicable=applicable,
        ),
        bin=bin_entries,
        engines_node=engines_node,
        integrity_less=integrity_less,
    )


# ── graph validation ───────────────────────────────────────────────────


def _canonical_roots(roots: Sequence[RootSpec]) -> tuple[RootSpec, ...]:
    seen: set[str] = set()
    out: list[RootSpec] = []
    for root in roots:
        _validate_name(root.name)
        try:
            _validate_semver(root.version)
        except SemverError as exc:
            raise LockedNpmError(
                "invalid_version",
                f"root {root.name!r} has invalid version {root.version!r}",
            ) from exc
        if root.name in seen:
            raise LockedNpmError("duplicate_root", f"duplicate root {root.name!r}")
        seen.add(root.name)
        out.append(RootSpec(root.name, root.version))
    return tuple(sorted(out))


def _ancestor_dirs(parent_path: str) -> list[str]:
    dirs = [parent_path]
    current = parent_path
    while current != "":
        idx = current.rfind("/node_modules/")
        if idx == -1:
            current = ""
        else:
            current = current[:idx]
        dirs.append(current)
    return dirs


def _resolve(
    pkg_map: Mapping[str, LockPackage], parent_path: str, name: str
) -> LockPackage | None:
    for directory in _ancestor_dirs(parent_path):
        candidate = (
            f"{directory}/node_modules/{name}" if directory else f"node_modules/{name}"
        )
        if candidate in pkg_map:
            return pkg_map[candidate]
    return None


def _parent_dir(path: str) -> str:
    """Return the parent package path of *path* (``""`` for a top-level node)."""
    idx = path.rfind("/node_modules/")
    return "" if idx == -1 else path[:idx]


def _resolve_peer(
    pkg_map: Mapping[str, LockPackage], declaring_path: str, name: str
) -> LockPackage | None:
    """Resolve a peer dependency from the declaring package's parent level.

    npm never satisfies a peer from inside the declaring package itself:
    ``node_modules/a/node_modules/peer`` cannot satisfy ``a``'s peer
    dependency.  The search begins at the declaring package's parent
    directory and walks upward, exactly like regular resolution from that
    parent.
    """
    parent = _parent_dir(declaring_path)
    for directory in _ancestor_dirs(parent):
        candidate = (
            f"{directory}/node_modules/{name}" if directory else f"node_modules/{name}"
        )
        if candidate in pkg_map:
            return pkg_map[candidate]
    return None


def _resolve_edge(
    pkg_map: Mapping[str, LockPackage], path: str, name: str, kind: str
) -> LockPackage | None:
    """Resolve one dependency edge, using peer semantics for peer edges."""
    if kind == "peerDependency":
        return _resolve_peer(pkg_map, path, name)
    return _resolve(pkg_map, path, name)


def _edges(node: LockPackage) -> tuple[tuple[str, str, bool, str], ...]:
    """Return ``(name, range, is_optional, kind)`` edges installed for *node*.

    Required edges are ``dependencies`` (plus root ``devDependencies``);
    ``optionalDependencies`` and optional peers (via ``peerDependenciesMeta``)
    are optional.  Peer edges apply only to non-root nodes, because the root
    project's peers are supplied by the consumer environment, not installed.

    When a name appears in both ``dependencies`` and ``optionalDependencies``,
    the optional declaration wins: the regular dependency edge is dropped and
    the optional range is authoritative.
    """
    optional_names = {name for (name, _rng) in node.optional_dependencies}
    edges: list[tuple[str, str, bool, str]] = [
        (name, rng, False, "dependency")
        for (name, rng) in node.dependencies
        if name not in optional_names
    ]
    if node.path == "":
        edges.extend(
            (name, rng, False, "devDependency") for (name, rng) in node.dev_dependencies
        )
    edges.extend(
        (name, rng, True, "optionalDependency")
        for (name, rng) in node.optional_dependencies
    )
    if node.path != "":
        optional_peers = set(node.optional_peers)
        edges.extend(
            (name, rng, name in optional_peers, "peerDependency")
            for (name, rng) in node.peer_dependencies
        )
    return tuple(edges)


def _graph_membership_paths(pkg_map: Mapping[str, LockPackage]) -> frozenset[str]:
    """Return every lock path root-connected in the lock graph.

    Starts at the root node (``""``) and follows every resolved dependency
    edge — including edges under platform-omitted optional packages — so
    descendants of an omitted optional remain members of the lock graph even
    though they are not part of the installed platform closure.
    """
    reached: set[str] = set()
    queue: list[str] = [""]
    while queue:
        path = queue.pop()
        if path in reached:
            continue
        reached.add(path)
        node = pkg_map[path]
        for name, _rng, _is_optional, kind in _edges(node):
            resolved = _resolve_edge(pkg_map, path, name, kind)
            if resolved is not None and resolved.path not in reached:
                queue.append(resolved.path)
    return frozenset(reached)


def _reject_disconnected_nodes(
    pkg_map: Mapping[str, LockPackage],
    membership: frozenset[str],
) -> None:
    """Reject every non-root node not reachable from the root lock node.

    Lock-graph membership is a separate, stronger check than the installed-
    closure traversal: a self-referencing or mutually-referencing disconnected
    package is still not root-connected and is rejected as an extra node.
    """
    for path, node in pkg_map.items():
        if path == "":
            continue
        if path not in membership:
            raise LockedNpmError(
                "extra_lock_node",
                f"node {node.name!r} at {path!r} is not reachable from the "
                f"root lock node",
            )


def _validate_roots(
    roots: tuple[RootSpec, ...],
    root: LockPackage,
    pkg_map: Mapping[str, LockPackage],
) -> None:
    optional_names = {name for (name, _rng) in root.optional_dependencies}
    declared: dict[str, tuple[str, bool]] = {}
    for name, rng in root.dependencies:
        if name in optional_names:
            # A name in both ``dependencies`` and ``optionalDependencies`` is
            # governed by the optional declaration: skip it here and let the
            # optional loop below record the authoritative range.
            continue
        declared[name] = (rng, False)
    for name, rng in root.dev_dependencies:
        if name in declared:
            raise LockedNpmError(
                "root_drift", f"root declares {name!r} in more than one dependency map"
            )
        declared[name] = (rng, False)
    for name, rng in root.optional_dependencies:
        if name in declared:
            # Only reachable when ``name`` also appears in ``devDependencies``
            # (a ``dependencies`` overlap was skipped above): genuine drift.
            raise LockedNpmError(
                "root_drift", f"root declares {name!r} in more than one dependency map"
            )
        declared[name] = (rng, True)

    # The exact roots are the required direct dependencies plus any optional
    # direct dependencies that are applicable on this platform.  Platform-
    # omitted optional roots are excluded from the expected set and must not
    # be provided; their omission is recorded by closure validation.
    applicable_optional: set[str] = set()
    for name, (_, is_optional) in declared.items():
        if is_optional:
            node = _resolve(pkg_map, "", name)
            if node is not None and node.applicable:
                applicable_optional.add(name)

    expected = {
        name for name, (_, is_optional) in declared.items() if not is_optional
    } | applicable_optional
    provided = {r.name for r in roots}
    if expected != provided:
        raise LockedNpmError(
            "root_drift",
            f"declared root set {sorted(expected)!r} does not match "
            f"provided roots {sorted(provided)!r}",
        )

    for root_spec in roots:
        rng, _ = declared[root_spec.name]
        if not satisfies(rng, root_spec.version):
            raise LockedNpmError(
                "root_range_mismatch",
                f"root {root_spec.name!r}@{root_spec.version!r} does not "
                f"satisfy declared range {rng!r}",
            )
        node = _resolve(pkg_map, "", root_spec.name)
        if node is None or node.version != root_spec.version:
            raise LockedNpmError(
                "root_drift",
                f"root {root_spec.name!r}@{root_spec.version!r} is not locked "
                f"at that exact version",
            )


def _validate_closure(
    pkg_map: Mapping[str, LockPackage],
    root: LockPackage,
    platform: str,
) -> tuple[tuple[OmittedOptional, ...], frozenset[str]]:
    omitted: list[OmittedOptional] = []
    refs: dict[str, list[tuple[str, str, bool]]] = {}

    # Traverse only the installed tree.  Starting from the root, follow every
    # applicable node's edges.  An absent node is a missing-node error even
    # for an optional edge: omission requires lock-contained os/cpu evidence.
    # A present but platform-inapplicable optional node is recorded as omitted
    # and its subtree is never entered: its descendants are not required to
    # exist or satisfy ranges on the current platform.
    visited: set[str] = set()
    queue: list[str] = [""]
    while queue:
        path = queue.pop()
        if path in visited:
            continue
        visited.add(path)
        node = pkg_map[path]
        for name, rng, is_optional, kind in _edges(node):
            resolved = _resolve_edge(pkg_map, path, name, kind)
            if resolved is None:
                if kind == "peerDependency" and is_optional:
                    # npm never auto-installs an optional peer: an absent
                    # optional peer is normal and needs no omission evidence.
                    continue
                raise LockedNpmError(
                    "missing_node",
                    f"{kind} {name!r} required by {path or '<root>'} "
                    f"is missing from the closure",
                )
            if not satisfies(rng, resolved.version):
                raise LockedNpmError(
                    "unsatisfied_range",
                    f"{resolved.name}@{resolved.version!r} at {resolved.path!r} "
                    f"does not satisfy {name!r} range {rng!r} "
                    f"required by {path or '<root>'} (as {kind})",
                )
            if not resolved.applicable:
                if is_optional:
                    omitted.append(
                        OmittedOptional(
                            path, name, rng, "platform-inapplicable", platform
                        )
                    )
                else:
                    raise LockedNpmError(
                        "inapplicable_required_node",
                        f"required {kind} {name!r} at {resolved.path!r} "
                        f"is not installable on platform {platform!r}",
                    )
                # The omitted node's subtree is not installed: do not traverse it.
                continue
            refs.setdefault(resolved.path, []).append((path, name, is_optional))
            if resolved.path not in visited:
                queue.append(resolved.path)

    for resolved_path, entries in refs.items():
        node = pkg_map[resolved_path]
        if node.optional and any(not is_opt for (_p, _n, is_opt) in entries):
            raise LockedNpmError(
                "optional_marked_required",
                f"optional node {node.name!r} at {resolved_path!r} is required "
                f"by a non-optional dependency edge",
            )

    return (
        tuple(sorted(omitted, key=lambda o: (o.parent_path, o.name))),
        frozenset(visited),
    )


# ── public API ─────────────────────────────────────────────────────────


def parse_lockfile(
    raw: bytes,
    *,
    platform: str,
    roots: Sequence[RootSpec],
) -> LockfileV3:
    """Parse and validate a ``package-lock.json`` v3 document.

    *raw* is the exact lockfile bytes; *platform* is an ``os-arch`` token
    (e.g. ``"linux-x64"``); *roots* are the exact root package versions.
    The bytes are hashed as-is and decoded strictly as UTF-8 only for JSON
    parsing; no newline, BOM, whitespace, or encoding normalization is
    applied.  Raises :class:`LockedNpmError` for every contract violation.
    """
    source_digest = hashlib.sha256(raw).hexdigest()
    os_name, arch = _split_platform(platform)

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LockedNpmError(
            "invalid_utf8",
            "package-lock.json bytes are not valid UTF-8",
        ) from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LockedNpmError(
            "malformed_lockfile", "package-lock.json is not valid JSON"
        ) from exc
    if not isinstance(data, Mapping):
        raise LockedNpmError(
            "malformed_lockfile", "package-lock.json must be an object"
        )

    unknown_top = sorted(set(data) - set(_TOP_LEVEL_FIELDS))
    if unknown_top:
        raise LockedNpmError(
            "unsupported_field",
            f"unsupported top-level field(s) {unknown_top!r}",
        )

    lockfile_version = data.get("lockfileVersion")
    if lockfile_version != 3:
        raise LockedNpmError(
            "unsupported_lockfile_version",
            f"lockfileVersion must be 3, got {lockfile_version!r}",
        )

    packages_raw = data.get("packages")
    if not isinstance(packages_raw, Mapping):
        raise LockedNpmError(
            "missing_packages", "package-lock.json is missing 'packages'"
        )

    canonical_roots = _canonical_roots(roots)

    parsed: dict[str, _ParsedNode] = {}
    for path, node in packages_raw.items():
        if not isinstance(path, str):
            raise LockedNpmError("malformed_lockfile", "packages keys must be strings")
        if not isinstance(node, Mapping):
            raise LockedNpmError(
                "malformed_lockfile", f"package node {path!r} must be an object"
            )
        parsed[path] = _parse_node(path, node, os_name, arch)

    if "" not in parsed:
        raise LockedNpmError(
            "malformed_lockfile", "lockfile is missing the root node ('' key)"
        )

    pkg_map: dict[str, LockPackage] = {path: p.package for path, p in parsed.items()}
    root = pkg_map[""]
    _validate_roots(canonical_roots, root, pkg_map)

    # Lock-graph membership: reject non-root nodes not reachable from the
    # root lock node (a disconnected, self/mutually-referencing package).
    membership = _graph_membership_paths(pkg_map)
    _reject_disconnected_nodes(pkg_map, membership)

    omitted, reachable = _validate_closure(pkg_map, root, platform)

    # Reviewed roots are the exact lock paths each RootSpec resolves to.
    # Their ``bin``/``engines.node`` are functional metadata: validate bin
    # targets as safe relative paths and preserve both, keyed by
    # (package identity, lock path).  Transitive/manifest metadata is
    # discarded after the shape/syntax validation already performed.
    reviewed_paths: set[str] = set()
    for root_spec in canonical_roots:
        resolved_root = _resolve(pkg_map, "", root_spec.name)
        if resolved_root is not None:
            reviewed_paths.add(resolved_root.path)

    # Integrity-less records cover every accepted non-manifest package node,
    # including platform-omitted optional nodes that are excluded from the
    # installed closure below.  They are sorted by lock path and kept
    # separate from ``packages``.
    integrity_less: list[IntegrityLessNode] = []
    for path, parsed_node in parsed.items():
        if path == "":
            continue
        if parsed_node.integrity_less:
            node = pkg_map[path]
            integrity_less.append(
                IntegrityLessNode(
                    name=node.name,
                    path=path,
                    version=node.version,
                    resolved=node.resolved,
                )
            )
    integrity_less.sort(key=lambda n: n.path)

    packages: list[LockPackage] = []
    root_metadata: list[ReviewedRootMetadata] = []
    for path in sorted(reachable):
        if path == "":
            continue
        node = pkg_map[path]
        parsed_node = parsed[path]
        if path in reviewed_paths:
            for _name, target in parsed_node.bin:
                _validate_bin_target(target, path)
            root_metadata.append(
                ReviewedRootMetadata(
                    package_name=node.name,
                    lock_path=path,
                    bin=parsed_node.bin,
                    engines_node=parsed_node.engines_node,
                )
            )
        packages.append(node)

    root_metadata.sort(key=lambda m: (m.package_name, m.lock_path))

    top_name = data.get("name")
    top_version = data.get("version")
    return LockfileV3(
        lockfile_version=3,
        source_digest=source_digest,
        name=top_name if isinstance(top_name, str) else None,
        version=top_version if isinstance(top_version, str) else None,
        platform=platform,
        roots=canonical_roots,
        root=root,
        packages=tuple(packages),
        omitted_optionals=omitted,
        root_metadata=tuple(root_metadata),
        integrity_less=tuple(integrity_less),
    )
