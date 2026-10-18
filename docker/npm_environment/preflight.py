"""Side-effect-free assembler input preflight.

``preflight`` performs closed parsing, root and closure validation,
transitive-metadata handling, keyed reviewed-root metadata extraction, and
satisfaction of every declared reviewed-root ``engines.node`` range, then
returns an immutable :class:`ValidatedAssemblyInput`.  It performs no Docker
execution, network access, cache mutation, staging, identity locking, or
publication and exposes no output path or output evidence.

When the caller reviewed a separate install-package manifest (the Pi release
contract), ``package_bytes`` is the exact
``pi-coding-agent-install-package.json`` bytes.  They are strictly parsed and
bound to the lockfile's manifest root — the package name and exact reviewed
version must match, and the package's declared dependencies must agree with
the lockfile root entry — so the manifest is a real assembler input rather
than a download-and-discard side effect.
"""

from __future__ import annotations

import hashlib
import json
from typing import Mapping, Sequence

from docker.versioning.semver import SemverError, validate as _validate_semver

from .errors import LockedNpmError
from .lockfile import parse_lockfile
from .model import RootSpec, ValidatedAssemblyInput
from .semver_range import satisfies


def _parse_install_package(package_bytes: bytes, lockfile) -> dict:
    """Strictly parse and bind the install-package manifest to the lockfile.

    Returns the parsed manifest object.  Raises :class:`LockedNpmError` for
    non-JSON bytes, a non-object document, a name/version that does not match
    the lockfile manifest root, or a ``dependencies`` map that disagrees with
    the lockfile root entry.
    """
    try:
        text = package_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LockedNpmError(
            "malformed_package",
            "install-package.json bytes are not valid UTF-8",
        ) from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LockedNpmError(
            "malformed_package",
            "install-package.json is not valid JSON",
        ) from exc
    if not isinstance(data, Mapping):
        raise LockedNpmError(
            "malformed_package",
            "install-package.json must be an object",
        )

    name = data.get("name")
    if not isinstance(name, str) or not name:
        raise LockedNpmError(
            "invalid_package_name",
            "install-package.json must declare a non-empty string 'name'",
        )
    version = data.get("version")
    if not isinstance(version, str) or not version:
        raise LockedNpmError(
            "invalid_package_version",
            "install-package.json must declare a non-empty string 'version'",
        )
    if name != lockfile.root.name:
        raise LockedNpmError(
            "package_name_mismatch",
            f"install-package name {name!r} does not match the lockfile "
            f"root name {lockfile.root.name!r}",
        )
    if version != lockfile.root.version:
        raise LockedNpmError(
            "package_version_mismatch",
            f"install-package version {version!r} does not match the "
            f"reviewed version {lockfile.root.version!r}",
        )

    if "dependencies" not in data:
        dependencies = {}
    else:
        dependencies = data["dependencies"]
    if not isinstance(dependencies, Mapping) or any(
        not isinstance(k, str) or not isinstance(v, str)
        for k, v in dependencies.items()
    ):
        raise LockedNpmError(
            "invalid_package_dependencies",
            "install-package 'dependencies' must be a string-to-string object",
        )
    lock_dependencies = dict(lockfile.root.dependencies)
    if dict(dependencies) != lock_dependencies:
        raise LockedNpmError(
            "package_lock_root_disagreement",
            f"install-package dependencies {dict(dependencies)!r} do not "
            f"match the lockfile root dependencies {lock_dependencies!r}",
        )
    return data


def preflight(
    raw: bytes,
    *,
    roots: Sequence[RootSpec],
    platform: str,
    node_version: str,
    npm_version: str,
    package_bytes: bytes | None = None,
) -> ValidatedAssemblyInput:
    """Validate the exact lockfile bytes into a :class:`ValidatedAssemblyInput`.

    *raw* is the exact ``package-lock.json`` bytes; *platform* is an
    ``os-arch`` token; *roots* are the exact root package versions;
    *node_version* and *npm_version* are the caller-owned reviewed exact
    tool versions; *package_bytes* is the optional exact
    ``pi-coding-agent-install-package.json`` bytes, which are strictly parsed
    and bound to the lockfile manifest root when provided.  Raises
    :class:`LockedNpmError` for every contract violation, including a
    reviewed-root ``engines.node`` range that the reviewed Node version does
    not satisfy.
    """
    for label, value in (("node", node_version), ("npm", npm_version)):
        if not isinstance(value, str) or not value:
            raise LockedNpmError(
                "invalid_tool_version",
                f"reviewed {label} version must be an exact non-empty semver, "
                f"got {value!r}",
            )
        try:
            _validate_semver(value)
        except SemverError as exc:
            raise LockedNpmError(
                "invalid_tool_version",
                f"reviewed {label} version {value!r} is not an exact semver",
            ) from exc

    lockfile = parse_lockfile(raw, platform=platform, roots=roots)

    package_digest: str | None = None
    if package_bytes is not None:
        if not isinstance(package_bytes, bytes):
            raise LockedNpmError(
                "invalid_package_bytes",
                "install-package input must be bytes",
            )
        _parse_install_package(package_bytes, lockfile)
        package_digest = hashlib.sha256(package_bytes).hexdigest()

    # Only reviewed-root ``engines.node`` declarations are authoritative.
    # Transitive and manifest ranges were syntax-validated and discarded
    # during parsing and never constrain the reviewed Node version.
    for meta in lockfile.root_metadata:
        if meta.engines_node is not None and not satisfies(
            meta.engines_node, node_version
        ):
            raise LockedNpmError(
                "incompatible_node_engine",
                f"reviewed root {meta.package_name!r} at {meta.lock_path!r} "
                f"declares engines.node {meta.engines_node!r}, which is not "
                f"satisfied by reviewed Node {node_version!r}",
            )

    return ValidatedAssemblyInput(
        lockfile_digest=lockfile.source_digest,
        lockfile_bytes=raw,
        platform=platform,
        node_version=node_version,
        npm_version=npm_version,
        roots=lockfile.roots,
        packages=lockfile.packages,
        omitted_optionals=lockfile.omitted_optionals,
        root_metadata=lockfile.root_metadata,
        integrity_less=lockfile.integrity_less,
        package_bytes=package_bytes,
        package_digest=package_digest,
    )
