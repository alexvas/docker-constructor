"""Phase 5 — independent post-install output validation (task 5.1).

``validate_assembled_tree`` must not trust npm's exit code.  These tests
drive it with synthetic assembled trees and require it to accept an exact
closure match and reject every corruption class: wrong name/version/path,
missing closure nodes, omitted-optional leakage, extra packages, foreign
ownership, group/other-writable permissions, escaping symlinks, and special
files.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR.parent))

from docker.npm_environment import (
    LockedNpmError,
    RootSpec,
    preflight,
    validate_assembled_tree,
)

_PLATFORM = "linux-x64"
_NODE = "24.18.0"
_NPM = "11.16.0"


def _sri() -> str:
    return "sha512-" + base64.b64encode(bytes([0xAB]) * 64).decode()


def _url(name: str, version: str) -> str:
    stem = name.rsplit("/", 1)[-1]
    return f"https://registry.npmjs.org/{name}/-/{stem}-{version}.tgz"


def _pkg(name: str, version: str, **extra: object) -> dict:
    node: dict = {
        "version": version,
        "resolved": _url(name, version),
        "integrity": _sri(),
    }
    node.update(extra)
    return node


def _lock(
    *,
    roots: dict[str, str] | None = None,
    packages: dict[str, dict] | None = None,
) -> bytes:
    root_node: dict = {"name": "root", "version": "1.0.0"}
    if roots:
        root_node["dependencies"] = dict(roots)
    pkg_nodes: dict[str, dict] = {}
    if packages:
        pkg_nodes.update(packages)
    for name, version in (roots or {}).items():
        pkg_nodes.setdefault(f"node_modules/{name}", _pkg(name, version))
    return json.dumps(
        {
            "name": "root",
            "version": "1.0.0",
            "lockfileVersion": 3,
            "requires": True,
            "packages": {"": root_node, **pkg_nodes},
        }
    ).encode()


def _validated(roots: dict[str, str], packages: dict[str, dict] | None = None):
    raw = _lock(roots=roots, packages=packages)
    return preflight(
        raw,
        roots=tuple(RootSpec(n, v) for n, v in sorted(roots.items())),
        platform=_PLATFORM,
        node_version=_NODE,
        npm_version=_NPM,
    )


def _write_pkg(root: Path, path: str, name: str, version: str, **extra: object) -> None:
    p = root / path / "package.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {"name": name, "version": version}
    data.update(extra)
    p.write_text(json.dumps(data))


class _OutputTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="npm-env-output-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "tree"
        self.root.mkdir()

    def _tree(self, *, with_b: bool = True) -> None:
        _write_pkg(self.root, "node_modules/a", "a", "1.0.0")
        if with_b:
            _write_pkg(self.root, "node_modules/b", "b", "1.0.0")


class TestExactClosureAccepted(_OutputTestCase):
    def test_exact_paths_names_versions_closure_accepted(self):
        validated = _validated(
            {"a": "1.0.0"},
            packages={
                "node_modules/a": _pkg("a", "1.0.0", dependencies={"b": "^1.0.0"}),
                "node_modules/b": _pkg("b", "1.0.0"),
            },
        )
        self._tree()
        # The root package.json is the synthesized manifest; add it too.
        _write_pkg(self.root, "", "root", "1.0.0")
        manifest = validate_assembled_tree(validated, self.root)
        self.assertTrue(
            any(e.path == "node_modules/a/package.json" for e in manifest.entries)
        )

    def test_scoped_and_nested_paths_accepted(self):
        validated = _validated(
            {"@scope/tool": "1.2.3"},
            packages={
                "node_modules/@scope/tool": _pkg(
                    "@scope/tool", "1.2.3", dependencies={"strip-ansi": "^5.0.0"}
                ),
                "node_modules/strip-ansi": _pkg("strip-ansi", "5.2.0"),
            },
        )
        _write_pkg(self.root, "node_modules/@scope/tool", "@scope/tool", "1.2.3")
        _write_pkg(self.root, "node_modules/strip-ansi", "strip-ansi", "5.2.0")
        _write_pkg(self.root, "", "root", "1.0.0")
        validate_assembled_tree(validated, self.root)


class TestExactNameVersionPath(_OutputTestCase):
    def _validated(self):
        return _validated({"a": "1.0.0"})

    def test_wrong_name_rejected(self):
        validated = self._validated()
        _write_pkg(self.root, "node_modules/a", "wrong", "1.0.0")
        with self.assertRaises(LockedNpmError) as ctx:
            validate_assembled_tree(validated, self.root)
        self.assertEqual(ctx.exception.reason, "output_name_mismatch")

    def test_wrong_version_rejected(self):
        validated = self._validated()
        _write_pkg(self.root, "node_modules/a", "a", "9.9.9")
        with self.assertRaises(LockedNpmError) as ctx:
            validate_assembled_tree(validated, self.root)
        self.assertEqual(ctx.exception.reason, "output_version_mismatch")

    def test_missing_package_directory_rejected(self):
        validated = self._validated()
        with self.assertRaises(LockedNpmError) as ctx:
            validate_assembled_tree(validated, self.root)
        self.assertEqual(ctx.exception.reason, "output_package_missing")

    def test_package_at_wrong_path_rejected(self):
        validated = self._validated()
        _write_pkg(self.root, "node_modules/aa", "a", "1.0.0")
        with self.assertRaises(LockedNpmError) as ctx:
            validate_assembled_tree(validated, self.root)
        self.assertEqual(ctx.exception.reason, "output_package_missing")


class TestDependencyClosure(_OutputTestCase):
    def _validated(self):
        return _validated(
            {"a": "1.0.0"},
            packages={
                "node_modules/a": _pkg("a", "1.0.0", dependencies={"b": "^1.0.0"}),
                "node_modules/b": _pkg("b", "1.0.0"),
            },
        )

    def test_missing_closure_node_rejected(self):
        validated = self._validated()
        self._tree(with_b=False)
        with self.assertRaises(LockedNpmError) as ctx:
            validate_assembled_tree(validated, self.root)
        self.assertEqual(ctx.exception.reason, "output_package_missing")

    def test_removed_dependency_still_enforced(self):
        # The installed ``a`` package.json omits its locked dependency ``b``;
        # the lock's dependency edge — not the on-disk declaration — is the
        # authoritative closure contract, so ``b`` is still checked and must
        # be present.
        validated = self._validated()
        self._tree()  # ``a`` on disk declares no dependencies
        validate_assembled_tree(validated, self.root)

    def test_weakened_dependency_range_still_enforced(self):
        # The installed ``a`` package.json weakens b's range to ``*``; the
        # locked ``^1.0.0`` range remains authoritative.
        validated = self._validated()
        self._tree()
        _write_pkg(
            self.root,
            "node_modules/a",
            "a",
            "1.0.0",
            dependencies={"b": "*"},
        )
        validate_assembled_tree(validated, self.root)

    def test_strengthened_on_disk_range_is_ignored(self):
        # The installed ``a`` package.json strengthens b's range to ``^2.0.0``,
        # which the installed b@1.0.0 does not satisfy; the locked ``^1.0.0``
        # range is authoritative, so validation passes.
        validated = self._validated()
        self._tree()
        _write_pkg(
            self.root,
            "node_modules/a",
            "a",
            "1.0.0",
            dependencies={"b": "^2.0.0"},
        )
        validate_assembled_tree(validated, self.root)


class TestNpmStyleDependencyPlacement(_OutputTestCase):
    """npm-style ``node_modules`` ancestor dependency resolution."""

    def test_dependency_beneath_declaring_package_accepted(self):
        validated = _validated(
            {"a": "1.0.0"},
            packages={
                "node_modules/a": _pkg("a", "1.0.0", dependencies={"x": "^1.0.0"}),
                "node_modules/a/node_modules/x": _pkg("x", "1.0.0"),
            },
        )
        _write_pkg(
            self.root, "node_modules/a", "a", "1.0.0", dependencies={"x": "^1.0.0"}
        )
        _write_pkg(self.root, "node_modules/a/node_modules/x", "x", "1.0.0")
        validate_assembled_tree(validated, self.root)

    def test_dependency_hoisted_to_ancestor_accepted(self):
        validated = _validated(
            {"a": "1.0.0"},
            packages={
                "node_modules/a": _pkg("a", "1.0.0", dependencies={"b": "^1.0.0"}),
                "node_modules/a/node_modules/b": _pkg(
                    "b", "1.0.0", dependencies={"x": "^1.0.0"}
                ),
                "node_modules/a/node_modules/x": _pkg("x", "1.0.0"),
            },
        )
        _write_pkg(
            self.root, "node_modules/a", "a", "1.0.0", dependencies={"b": "^1.0.0"}
        )
        _write_pkg(
            self.root,
            "node_modules/a/node_modules/b",
            "b",
            "1.0.0",
            dependencies={"x": "^1.0.0"},
        )
        _write_pkg(self.root, "node_modules/a/node_modules/x", "x", "1.0.0")
        validate_assembled_tree(validated, self.root)

    def test_nearest_reachable_placement_selected(self):
        validated = _validated(
            {"a": "1.0.0", "b": "1.0.0"},
            packages={
                "node_modules/a": _pkg("a", "1.0.0", dependencies={"x": "^1.0.0"}),
                "node_modules/a/node_modules/x": _pkg("x", "1.0.0"),
                "node_modules/b": _pkg("b", "1.0.0", dependencies={"x": "^2.0.0"}),
                "node_modules/x": _pkg("x", "2.0.0"),
            },
        )
        _write_pkg(
            self.root, "node_modules/a", "a", "1.0.0", dependencies={"x": "^1.0.0"}
        )
        _write_pkg(self.root, "node_modules/a/node_modules/x", "x", "1.0.0")
        _write_pkg(
            self.root, "node_modules/b", "b", "1.0.0", dependencies={"x": "^2.0.0"}
        )
        _write_pkg(self.root, "node_modules/x", "x", "2.0.0")
        validate_assembled_tree(validated, self.root)


class TestOmissionsAndExtras(_OutputTestCase):
    def _validated_with_omission(self):
        return _validated(
            {"a": "1.0.0"},
            packages={
                "node_modules/a": _pkg(
                    "a", "1.0.0", optionalDependencies={"native": "^2.0.0"}
                ),
                "node_modules/native": _pkg(
                    "native", "2.0.0", optional=True, os=["win32"]
                ),
            },
        )

    def test_omitted_optional_absent_accepted(self):
        validated = self._validated_with_omission()
        self.assertEqual([o.name for o in validated.omitted_optionals], ["native"])
        _write_pkg(self.root, "node_modules/a", "a", "1.0.0")
        validate_assembled_tree(validated, self.root)

    def test_omitted_optional_present_rejected(self):
        validated = self._validated_with_omission()
        _write_pkg(self.root, "node_modules/a", "a", "1.0.0")
        _write_pkg(self.root, "node_modules/native", "native", "2.0.0")
        with self.assertRaises(LockedNpmError) as ctx:
            validate_assembled_tree(validated, self.root)
        # The validated closure is authoritative: an omitted optional that
        # leaked onto disk is an unexpected extra package.
        self.assertEqual(ctx.exception.reason, "output_unexpected_package")

    def test_extra_package_rejected(self):
        validated = _validated({"a": "1.0.0"})
        _write_pkg(self.root, "node_modules/a", "a", "1.0.0")
        _write_pkg(self.root, "node_modules/sneaky", "sneaky", "1.0.0")
        with self.assertRaises(LockedNpmError) as ctx:
            validate_assembled_tree(validated, self.root)
        self.assertEqual(ctx.exception.reason, "output_unexpected_package")


class TestExtrasPackagePlacement(_OutputTestCase):
    """Only valid npm package placements count as extra installed packages."""

    def _validated(self):
        return _validated({"a": "1.0.0"})

    def test_package_json_inside_package_content_allowed(self):
        # ``examples/demo/package.json`` is ordinary package content, not an
        # installed package root.
        validated = self._validated()
        _write_pkg(self.root, "node_modules/a", "a", "1.0.0")
        _write_pkg(self.root, "node_modules/a/examples/demo", "demo", "0.0.0")
        validate_assembled_tree(validated, self.root)

    def test_unlisted_package_rejected(self):
        validated = self._validated()
        _write_pkg(self.root, "node_modules/a", "a", "1.0.0")
        _write_pkg(self.root, "node_modules/unlisted", "unlisted", "1.0.0")
        with self.assertRaises(LockedNpmError) as ctx:
            validate_assembled_tree(validated, self.root)
        self.assertEqual(ctx.exception.reason, "output_unexpected_package")

    def test_nested_unlisted_package_rejected(self):
        validated = self._validated()
        _write_pkg(self.root, "node_modules/a", "a", "1.0.0")
        _write_pkg(
            self.root, "node_modules/a/node_modules/unlisted", "unlisted", "1.0.0"
        )
        with self.assertRaises(LockedNpmError) as ctx:
            validate_assembled_tree(validated, self.root)
        self.assertEqual(ctx.exception.reason, "output_unexpected_package")

    def test_unlisted_package_without_manifest_rejected(self):
        # A valid package placement directory is an extra even when it has
        # no package.json; the previous manifest-only scan missed these.
        validated = self._validated()
        _write_pkg(self.root, "node_modules/a", "a", "1.0.0")
        (self.root / "node_modules" / "unlisted").mkdir(parents=True)
        with self.assertRaises(LockedNpmError) as ctx:
            validate_assembled_tree(validated, self.root)
        self.assertEqual(ctx.exception.reason, "output_unexpected_package")

    def test_nested_unlisted_package_without_manifest_rejected(self):
        validated = self._validated()
        _write_pkg(self.root, "node_modules/a", "a", "1.0.0")
        (self.root / "node_modules" / "a" / "node_modules" / "unlisted").mkdir(
            parents=True
        )
        with self.assertRaises(LockedNpmError) as ctx:
            validate_assembled_tree(validated, self.root)
        self.assertEqual(ctx.exception.reason, "output_unexpected_package")


class TestOwnershipPermissionsSymlinksSpecial(_OutputTestCase):
    def _validated(self):
        return _validated({"a": "1.0.0"})

    def _tree(self) -> Path:
        _write_pkg(self.root, "node_modules/a", "a", "1.0.0")
        return self.root / "node_modules" / "a"

    def test_foreign_ownership_rejected(self):
        validated = self._validated()
        self._tree()
        with self.assertRaises(LockedNpmError) as ctx:
            validate_assembled_tree(validated, self.root, uid=os.geteuid() + 1)
        self.assertEqual(ctx.exception.reason, "output_ownership_mismatch")

    def test_group_writable_rejected(self):
        validated = self._validated()
        pkg = self._tree()
        os.chmod(pkg, 0o775)
        with self.assertRaises(LockedNpmError) as ctx:
            validate_assembled_tree(validated, self.root)
        self.assertEqual(ctx.exception.reason, "output_unsafe_permissions")

    def test_world_writable_file_rejected(self):
        validated = self._validated()
        self._tree()
        manifest = self.root / "node_modules" / "a" / "package.json"
        os.chmod(manifest, 0o666)
        with self.assertRaises(LockedNpmError) as ctx:
            validate_assembled_tree(validated, self.root)
        self.assertEqual(ctx.exception.reason, "output_unsafe_permissions")

    def test_escaping_symlink_rejected(self):
        validated = self._validated()
        self._tree()
        (self.root / "node_modules" / "a" / "escape").symlink_to("../../../outside")
        with self.assertRaises(LockedNpmError) as ctx:
            validate_assembled_tree(validated, self.root)
        self.assertEqual(ctx.exception.reason, "unsafe_symlink_target")

    def test_contained_symlink_accepted(self):
        validated = self._validated()
        self._tree()
        (self.root / "node_modules" / "a" / "self").symlink_to("package.json")
        validate_assembled_tree(validated, self.root)

    def test_special_file_rejected(self):
        validated = self._validated()
        self._tree()
        os.mkfifo(self.root / "node_modules" / "a" / "pipe")
        with self.assertRaises(LockedNpmError) as ctx:
            validate_assembled_tree(validated, self.root)
        self.assertEqual(ctx.exception.reason, "unsupported_entry_type")


class TestBinTargetValidation(_OutputTestCase):
    """Reviewed-root executable declarations must resolve inside the tree."""

    def _validated(self):
        return _validated(
            {"a": "1.0.0"},
            packages={"node_modules/a": _pkg("a", "1.0.0", bin={"acmd": "bin/run"})},
        )

    def _write_package(self) -> None:
        _write_pkg(self.root, "node_modules/a", "a", "1.0.0")

    def test_existing_executable_target_accepted(self):
        validated = self._validated()
        self._write_package()
        (self.root / "node_modules" / "a" / "bin").mkdir(parents=True)
        (self.root / "node_modules" / "a" / "bin" / "run").write_text("#!/bin/sh\n")
        validate_assembled_tree(validated, self.root)

    def test_contained_symlink_target_accepted(self):
        validated = self._validated()
        self._write_package()
        lib = self.root / "node_modules" / "a" / "lib"
        lib.mkdir(parents=True)
        (lib / "run.js").write_text("#!/usr/bin/env node\n")
        bin_dir = self.root / "node_modules" / "a" / "bin"
        bin_dir.mkdir()
        (bin_dir / "run").symlink_to("../lib/run.js")
        validate_assembled_tree(validated, self.root)

    def test_missing_executable_target_rejected(self):
        validated = self._validated()
        self._write_package()
        with self.assertRaises(LockedNpmError) as ctx:
            validate_assembled_tree(validated, self.root)
        self.assertEqual(ctx.exception.reason, "output_bin_target_missing")

    def test_dangling_symlink_target_rejected(self):
        validated = self._validated()
        self._write_package()
        bin_dir = self.root / "node_modules" / "a" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "run").symlink_to("missing.js")
        with self.assertRaises(LockedNpmError) as ctx:
            validate_assembled_tree(validated, self.root)
        self.assertEqual(ctx.exception.reason, "output_bin_target_dangling")

    def test_escaping_executable_target_rejected(self):
        validated = self._validated()
        self._write_package()
        outside = self.root.parent / "outside"
        outside.mkdir()
        (outside / "run").write_text("#!/bin/sh\n")
        # ``bin`` is a symlink to a directory outside the assembled tree.
        (self.root / "node_modules" / "a" / "bin").symlink_to(
            outside, target_is_directory=True
        )
        with self.assertRaises(LockedNpmError) as ctx:
            validate_assembled_tree(validated, self.root)
        self.assertEqual(ctx.exception.reason, "output_bin_target_escape")


if __name__ == "__main__":
    unittest.main()
