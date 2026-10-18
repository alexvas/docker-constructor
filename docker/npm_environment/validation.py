"""Independent post-install validation of assembled npm trees.

After the container exits successfully, Constructor must not trust npm's
exit code.  ``validate_assembled_tree`` re-checks the assembled filesystem
against the validated lock closure:

* every expected package directory exists at its exact lock path;
* each on-disk ``package.json`` carries the expected name and version;
* the installed closure contains no extra package and no omitted optional
  that should have been left out;
* every locked dependency edge resolves through npm-style ``node_modules``
  ancestor lookup to an installed version satisfying its locked range;
* every entry is owned by the invoking user (or an explicitly supplied
  expected identity);
* no entry is group- or world-writable;
* every preserved reviewed-root ``bin`` target exists, is not a dangling
  symlink, and resolves inside the assembled tree;
* the tree contains no special files and no escaping symlink (delegated to
  :func:`build_tree_manifest`, which also hashes every regular file).

The function is pure w.r.t. the tree: it reads and validates but mutates
nothing.  ``make_tree_read_only`` is the separate mutation step that strips
every write bit before the canonical published-tree manifest is rebuilt.
"""

from __future__ import annotations

import json
import os
import stat as _stat
from pathlib import Path

from .errors import LockedNpmError
from .model import ValidatedAssemblyInput
from .semver_range import satisfies
from .tree import TreeEntry, TreeManifest, build_tree_manifest

#: Write bits for group and other — rejected on every assembled entry.
_GROUP_OTHER_WRITE = 0o022


def _read_package_json(tree_root: Path, rel_path: str) -> dict:
    """Read and parse one installed ``package.json`` as a JSON object."""
    path = tree_root / f"{rel_path}/package.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LockedNpmError(
            "output_package_missing",
            f"cannot read installed package manifest {path}: {exc}",
        ) from exc
    if not isinstance(data, dict):
        raise LockedNpmError(
            "output_package_missing",
            f"installed package manifest {path} is not an object",
        )
    return data


def _is_package_placement(path: str) -> bool:
    """Return whether *path* is a valid npm package installation path.

    A package is installed at a path that starts with ``node_modules/``,
    alternates ``node_modules`` and package-name components, and ends with a
    package name (an unscoped name or ``@scope/name``).  Subdirectories
    inside a package's own content — ``node_modules/pkg/examples/demo`` — are
    therefore not package placements.
    """
    if (
        not path
        or not path.startswith("node_modules/")
        or path.endswith("/")
        or "//" in path
        or "\\" in path
    ):
        return False
    segments = path.split("/")
    i = 1  # segments[0] is the leading "node_modules"
    while i < len(segments):
        seg = segments[i]
        if seg == "node_modules" or seg == "":
            return False
        if seg.startswith("@"):
            if i + 1 >= len(segments) or segments[i + 1] == "":
                return False
            i += 2
        else:
            i += 1
        if i < len(segments):
            if segments[i] != "node_modules":
                return False
            i += 1
            if i >= len(segments):
                return False
    return True


def _dependency_candidate_paths(declaring_path: str, name: str) -> tuple[str, ...]:
    """Return npm-style ``node_modules`` lookup paths for *name*.

    Node resolves a dependency declared by a package at lock path *P* by
    checking ``P/node_modules/<name>`` first, then each progressively
    hoisted ancestor's ``node_modules/<name>``, and finally the top-level
    ``node_modules/<name>``.  The paths are returned in that order.
    """
    candidates: list[str] = []
    current = declaring_path
    while True:
        candidates.append(
            f"{current}/node_modules/{name}" if current else f"node_modules/{name}"
        )
        if not current:
            break
        if "/node_modules/" in current:
            current = current.rsplit("/node_modules/", 1)[0]
        else:
            break
    top = f"node_modules/{name}"
    if candidates[-1] != top:
        candidates.append(top)
    return tuple(candidates)


def _resolve_dependency(
    installed: dict[str, tuple[str, str]],
    declaring_path: str,
    name: str,
    rng: str,
) -> str:
    """Resolve one on-disk dependency with npm ancestor semantics.

    The first reachable installed candidate is authoritative: if it exists
    its version must satisfy *rng*, otherwise the nearer placement is never
    bypassed in favour of a farther one.  Returns the resolved version and
    raises ``output_missing_dependency`` when no reachable candidate exists
    or ``output_unsatisfied_dependency`` when the nearest candidate's version
    does not satisfy the declared range.
    """
    for candidate in _dependency_candidate_paths(declaring_path, name):
        resolved = installed.get(candidate)
        if resolved is None:
            continue
        version = resolved[1]
        if not satisfies(rng, version):
            raise LockedNpmError(
                "output_unsatisfied_dependency",
                f"{declaring_path!r}: dependency {name!r} resolves to "
                f"{candidate!r} at version {version!r}, which does not "
                f"satisfy range {rng!r}",
            )
        return version
    raise LockedNpmError(
        "output_missing_dependency",
        f"{declaring_path!r}: dependency {name!r} is not installed in any "
        "reachable node_modules location",
    )


def _validate_bin_targets(validated: ValidatedAssemblyInput, root: Path) -> None:
    """Validate every preserved reviewed-root executable declaration.

    Each ``bin`` target is resolved relative to its package lock path, then
    symlinks are followed to a final real path.  A target that does not
    exist, is a dangling symlink, or resolves outside the assembled tree is
    rejected.  This runs before the tree-manifest build so an escaping
    executable path is reported with its own reason rather than as a generic
    unsafe symlink.
    """
    real_root = os.path.realpath(str(root))
    for meta in validated.root_metadata:
        for command, target in meta.bin:
            candidate = root / meta.lock_path / target
            if not os.path.lexists(candidate):
                raise LockedNpmError(
                    "output_bin_target_missing",
                    f"reviewed root {meta.package_name!r} at {meta.lock_path!r} "
                    f"declares bin {command!r} at {target!r}, but "
                    f"{candidate} does not exist",
                )
            if not os.path.exists(candidate):
                raise LockedNpmError(
                    "output_bin_target_dangling",
                    f"reviewed root {meta.package_name!r} at {meta.lock_path!r} "
                    f"declares bin {command!r} at {target!r}, but "
                    f"{candidate} is a dangling symlink",
                )
            real_target = os.path.realpath(str(candidate))
            if real_target != real_root and not real_target.startswith(
                real_root + os.sep
            ):
                raise LockedNpmError(
                    "output_bin_target_escape",
                    f"reviewed root {meta.package_name!r} at {meta.lock_path!r} "
                    f"declares bin {command!r} at {target!r}, which resolves "
                    f"outside the assembled tree: {real_target}",
                )


def validate_assembled_tree(
    validated: ValidatedAssemblyInput,
    tree_root: str | Path,
    *,
    uid: int | None = None,
    gid: int | None = None,
) -> TreeManifest:
    """Independently validate the assembled tree at *tree_root*.

    Returns the canonical pre-publication manifest (used to strip write
    bits before the final manifest is built).  *uid*/*gid* default to the
    invoking process identity; callers may pass an explicit expected owner
    to test foreign-ownership rejection.
    """
    root = Path(tree_root)
    expected_uid = os.geteuid() if uid is None else uid
    expected_gid = os.getegid() if gid is None else gid

    _validate_bin_targets(validated, root)

    manifest = build_tree_manifest(root)  # rejects special files/escaping links
    by_path: dict[str, TreeEntry] = {e.path: e for e in manifest.entries}

    for entry in manifest.entries:
        if entry.uid != expected_uid or entry.gid != expected_gid:
            raise LockedNpmError(
                "output_ownership_mismatch",
                f"{entry.path!r}: ownership ({entry.uid}:{entry.gid}) is not "
                f"owned by expected identity ({expected_uid}:{expected_gid})",
            )
        if entry.kind != "symlink" and entry.mode & _GROUP_OTHER_WRITE:
            raise LockedNpmError(
                "output_unsafe_permissions",
                f"{entry.path!r}: mode {entry.mode:04o} grants write access "
                "to group or others",
            )

    # Exact paths, names, and versions for every closure package.
    installed: dict[str, tuple[str, str]] = {}
    for package in validated.packages:
        dir_entry = by_path.get(package.path)
        if dir_entry is None or dir_entry.kind != "directory":
            raise LockedNpmError(
                "output_package_missing",
                f"expected package directory {package.path!r} is missing",
            )
        file_entry = by_path.get(f"{package.path}/package.json")
        if file_entry is None or file_entry.kind != "file":
            raise LockedNpmError(
                "output_package_missing",
                f"expected package manifest {package.path + '/package.json'!r} "
                "is missing",
            )
        data = _read_package_json(root, package.path)
        on_disk_name = data.get("name")
        on_disk_version = data.get("version")
        if on_disk_name != package.name:
            raise LockedNpmError(
                "output_name_mismatch",
                f"{package.path!r}: installed name {on_disk_name!r} does not "
                f"match expected name {package.name!r}",
            )
        if on_disk_version != package.version:
            raise LockedNpmError(
                "output_version_mismatch",
                f"{package.path!r}: installed version {on_disk_version!r} "
                f"does not match expected version {package.version!r}",
            )
        installed[package.path] = (package.name, package.version)

    # Omissions and extras: the validated closure is authoritative.  An
    # installed-package directory (a valid npm package placement beneath a
    # ``node_modules`` component) absent from the validated closure — including
    # an omitted optional that leaked onto disk — is an unexpected extra
    # package, with or without a ``package.json``.  Directories nested inside
    # an installed package's own content (e.g. examples or fixtures) are not
    # package placements and are ordinary entries.
    for entry in manifest.entries:
        if entry.kind != "directory":
            continue
        if entry.path in installed:
            continue
        if _is_package_placement(entry.path):
            raise LockedNpmError(
                "output_unexpected_package",
                f"unexpected installed package directory {entry.path!r}",
            )

    # Dependency closure re-checked from the validated lock closure with
    # npm-style ``node_modules`` ancestor resolution.  The lock's dependency
    # edges — not the installed ``package.json`` declarations — are the
    # authoritative closure contract: for each locked dependency of a package
    # at path P, the first installed candidate beneath
    # ``P/node_modules/<name>``, then each hoisted ancestor, then the top
    # level is used.  Only explicitly recorded optional omissions are
    # permitted to be absent.
    omitted_by_parent: dict[str, set[str]] = {}
    for omitted in validated.omitted_optionals:
        omitted_by_parent.setdefault(omitted.parent_path, set()).add(omitted.name)

    for package in validated.packages:
        omitted = omitted_by_parent.get(package.path, set())
        for name, rng in package.dependencies:
            _resolve_dependency(installed, package.path, name, rng)
        for name, rng in package.optional_dependencies:
            if name in omitted:
                continue
            _resolve_dependency(installed, package.path, name, rng)

    return manifest


def make_tree_read_only(tree_root: str | Path, manifest: TreeManifest) -> None:
    """Strip every write bit from the validated tree's files and directories.

    Symlinks are left untouched so no target outside the (already verified
    contained) link is ever followed.  The tree root itself is deliberately
    left writable so the publisher can move it into place with a single
    atomic rename; the publisher seals the moved root afterwards.  The final
    canonical published-tree manifest must be rebuilt after this step so its
    recorded modes match the immutable published bytes.
    """
    root = Path(tree_root)
    for entry in manifest.entries:
        if entry.kind not in ("file", "directory"):
            continue
        path = root / entry.path
        try:
            st = os.lstat(path)
        except OSError as exc:
            raise LockedNpmError(
                "unsafe_cache_path",
                f"cannot stat tree entry {path} while making it read-only: {exc}",
            ) from exc
        os.chmod(
            path,
            _stat.S_IMODE(st.st_mode) & ~0o222,
            follow_symlinks=False,
        )
