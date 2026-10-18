"""Phase 1 — field-enumeration introspection (task 1.7).

The closed parser enumerates every accepted ``package-lock.json`` field per
role (manifest, reviewed root, transitive) and rejects any field it does not
explicitly accept.  Functional ``bin``/``engines.node`` for reviewed roots is
distinguished from accepted-and-ignored ``license``/``funding``/``deprecated``
and transitive/manifest ``bin``/``engines``.  Ignored values never appear in
DTOs, evidence, or semantic identity inputs and cannot influence output
silently.
"""

from __future__ import annotations

import base64
import json
import sys
import unittest
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR.parent))

# Repository/test path setup must run before these imports.
from docker import npm_environment  # noqa: E402
from docker.npm_environment import (  # noqa: E402
    LockedNpmError,
    RootSpec,
    compute_assembler_identity,
    compute_assembler_input_identity,
    parse_lockfile,
    preflight,
)
from docker.npm_environment import lockfile as lockfile_module  # noqa: E402


def _sri() -> str:
    return "sha512-" + base64.b64encode(bytes([0xAB]) * 64).decode()


def _node(name: str, version: str, **extra: object) -> dict:
    stem = name.rsplit("/", 1)[-1]
    node: dict = {
        "version": version,
        "resolved": f"https://registry.npmjs.org/{name}/-/{stem}-{version}.tgz",
        "integrity": _sri(),
    }
    node.update(extra)
    return node


def _lock(root_extra: dict | None = None, package_extra: dict | None = None) -> bytes:
    root: dict = {"name": "root", "version": "1.0.0", "dependencies": {"a": "1.0.0"}}
    if root_extra:
        root.update(root_extra)
    packages = {
        "": root,
        "node_modules/a": _node("a", "1.0.0", **(package_extra or {})),
    }
    return json.dumps(
        {
            "name": "root",
            "version": "1.0.0",
            "lockfileVersion": 3,
            "requires": True,
            "packages": packages,
        }
    ).encode("utf-8")


_MANIFEST = {
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
_PACKAGE = {
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
    "bin",
    "engines",
    "license",
    "funding",
    "deprecated",
}


class TestFieldEnumeration(unittest.TestCase):
    def test_role_whitelists_present_nonempty_and_distinct(self):
        manifest = lockfile_module._MANIFEST_NODE_FIELDS
        reviewed = lockfile_module._REVIEWED_ROOT_NODE_FIELDS
        transitive = lockfile_module._TRANSITIVE_NODE_FIELDS
        for fields in (manifest, reviewed, transitive):
            self.assertIsInstance(fields, frozenset)
            self.assertTrue(fields)
        # Three separate role sets are enumerated independently.
        self.assertIsNot(reviewed, transitive)

    def test_exact_membership_per_role(self):
        self.assertEqual(
            lockfile_module._TOP_LEVEL_FIELDS,
            {"lockfileVersion", "packages", "name", "version", "requires"},
        )
        self.assertEqual(lockfile_module._MANIFEST_NODE_FIELDS, _MANIFEST)
        self.assertEqual(lockfile_module._REVIEWED_ROOT_NODE_FIELDS, _PACKAGE)
        self.assertEqual(lockfile_module._TRANSITIVE_NODE_FIELDS, _PACKAGE)
        self.assertEqual(
            lockfile_module._REJECTED_FIELDS,
            {"link", "inBundle", "bundled", "workspaces"},
        )

    def test_bin_is_functional_only_for_reviewed_roots(self):
        # ``bin`` is not accepted on the manifest node; it is accepted on
        # package nodes (functional for reviewed roots, ignored for
        # transitive nodes).
        self.assertNotIn("bin", lockfile_module._MANIFEST_NODE_FIELDS)
        self.assertIn("bin", lockfile_module._REVIEWED_ROOT_NODE_FIELDS)
        self.assertIn("bin", lockfile_module._TRANSITIVE_NODE_FIELDS)

    def test_dev_dependencies_manifest_only(self):
        self.assertIn("devDependencies", lockfile_module._MANIFEST_NODE_FIELDS)
        self.assertNotIn("devDependencies", lockfile_module._REVIEWED_ROOT_NODE_FIELDS)
        self.assertNotIn("devDependencies", lockfile_module._TRANSITIVE_NODE_FIELDS)

    def test_rejected_fields_are_not_in_accepted_whitelist(self):
        for fields in (
            lockfile_module._MANIFEST_NODE_FIELDS,
            lockfile_module._REVIEWED_ROOT_NODE_FIELDS,
            lockfile_module._TRANSITIVE_NODE_FIELDS,
        ):
            self.assertEqual(fields & lockfile_module._REJECTED_FIELDS, set())

    def test_libc_is_rejected_not_ignored(self):
        for libc in (["glibc"], ["musl"]):
            raw = _lock(package_extra={"libc": libc})
            with self.assertRaises(LockedNpmError) as ctx:
                parse_lockfile(
                    raw, platform="linux-x64", roots=(RootSpec("a", "1.0.0"),)
                )
            self.assertEqual(ctx.exception.reason, "unsupported_libc")
        for fields in (
            lockfile_module._REVIEWED_ROOT_NODE_FIELDS,
            lockfile_module._TRANSITIVE_NODE_FIELDS,
        ):
            self.assertNotIn("libc", fields)


class TestIgnoredValuesAreInert(unittest.TestCase):
    def test_ignored_transitive_fields_absent_from_dto_and_evidence(self):
        packages = {
            "": {"name": "root", "version": "1.0.0", "dependencies": {"a": "1.0.0"}},
            "node_modules/a": _node("a", "1.0.0", dependencies={"b": "^1.0.0"}),
            "node_modules/b": _node(
                "b",
                "1.5.0",
                bin={"b": "cli.js"},
                engines={"node": "18 || 20 || >=22"},
                license="MIT",
                funding={"url": "https://example.com/sponsor"},
                deprecated="use b instead",
            ),
        }
        raw = json.dumps(
            {
                "name": "root",
                "version": "1.0.0",
                "lockfileVersion": 3,
                "requires": True,
                "packages": packages,
            }
        ).encode("utf-8")
        lockfile = parse_lockfile(
            raw, platform="linux-x64", roots=(RootSpec("a", "1.0.0"),)
        )
        pkg_b = next(p for p in lockfile.packages if p.name == "b")
        for attr in ("bin", "engines", "license", "funding", "deprecated"):
            self.assertFalse(hasattr(pkg_b, attr), attr)
        # Only the reviewed root carries (empty) metadata; transitive ``b``
        # must not appear there.
        self.assertEqual([m.package_name for m in lockfile.root_metadata], ["a"])

    def test_ignored_values_change_identity_only_via_lock_bytes(self):
        def build(license_value: str) -> bytes:
            return _lock(package_extra={"license": license_value})

        first_raw = build("MIT")
        second_raw = build("Apache-2.0")
        first = preflight(
            first_raw,
            roots=(RootSpec("a", "1.0.0"),),
            platform="linux-x64",
            node_version="24.18.0",
            npm_version="11.16.0",
        )
        second = preflight(
            second_raw,
            roots=(RootSpec("a", "1.0.0"),),
            platform="linux-x64",
            node_version="24.18.0",
            npm_version="11.16.0",
        )
        self.assertEqual(first.packages, second.packages)
        self.assertEqual(first.root_metadata, second.root_metadata)
        self.assertNotEqual(first.lockfile_digest, second.lockfile_digest)

        assembler = compute_assembler_identity(
            image_digest="sha256:" + "a" * 64,
            node_version="24.18.0",
            npm_version="11.16.0",
            script_digest="script",
            policy_digest="policy",
            platform="linux-x64",
        )
        ident_first = compute_assembler_input_identity(first, assembler)
        ident_second = compute_assembler_input_identity(second, assembler)
        # Roots and assembler are unchanged; only the lock-byte digest moved.
        self.assertEqual(ident_first.roots, ident_second.roots)
        self.assertEqual(ident_first.assembler, ident_second.assembler)
        self.assertNotEqual(ident_first.lockfile_digest, ident_second.lockfile_digest)
        self.assertNotEqual(ident_first.digest, ident_second.digest)

    def test_no_custom_transitive_engine_diagnostic(self):
        # A syntax-valid transitive range the reviewed Node does not satisfy
        # is accepted with no Constructor compatibility diagnostic.
        packages = {
            "": {"name": "root", "version": "1.0.0", "dependencies": {"a": "1.0.0"}},
            "node_modules/a": _node("a", "1.0.0", dependencies={"b": "^1.0.0"}),
            "node_modules/b": _node("b", "1.5.0", engines={"node": ">=99"}),
        }
        raw = json.dumps(
            {
                "name": "root",
                "version": "1.0.0",
                "lockfileVersion": 3,
                "requires": True,
                "packages": packages,
            }
        ).encode("utf-8")
        validated = preflight(
            raw,
            roots=(RootSpec("a", "1.0.0"),),
            platform="linux-x64",
            node_version="24.18.0",
            npm_version="11.16.0",
        )
        self.assertEqual([m.package_name for m in validated.root_metadata], ["a"])
        for attr in ("compatibility", "transitive_engine", "engine_diagnostic"):
            self.assertFalse(hasattr(validated, attr), attr)


class TestUnsupportedFieldsRejected(unittest.TestCase):
    def test_unknown_top_level_field_rejected(self):
        raw = json.dumps(
            {
                "name": "root",
                "version": "1.0.0",
                "lockfileVersion": 3,
                "requires": True,
                "madeUpField": "surprise",
                "packages": {
                    "": {
                        "name": "root",
                        "version": "1.0.0",
                        "dependencies": {"a": "1.0.0"},
                    },
                    "node_modules/a": _node("a", "1.0.0"),
                },
            }
        ).encode("utf-8")
        with self.assertRaises(LockedNpmError) as ctx:
            parse_lockfile(raw, platform="linux-x64", roots=(RootSpec("a", "1.0.0"),))
        self.assertEqual(ctx.exception.reason, "unsupported_field")

    def test_unknown_manifest_field_rejected(self):
        raw = _lock(root_extra={"madeUpField": "surprise"})
        with self.assertRaises(LockedNpmError) as ctx:
            parse_lockfile(raw, platform="linux-x64", roots=(RootSpec("a", "1.0.0"),))
        self.assertEqual(ctx.exception.reason, "unsupported_field")

    def test_unknown_package_field_rejected(self):
        raw = _lock(package_extra={"madeUpField": "surprise"})
        with self.assertRaises(LockedNpmError) as ctx:
            parse_lockfile(raw, platform="linux-x64", roots=(RootSpec("a", "1.0.0"),))
        self.assertEqual(ctx.exception.reason, "unsupported_field")


class TestPublicApiSurface(unittest.TestCase):
    def test_public_api_surface_is_closed(self):
        for name in (
            "parse_lockfile",
            "preflight",
            "compute_assembler_identity",
            "compute_assembler_input_identity",
            "parse_range",
            "satisfies",
            "LockedNpmError",
            "RootSpec",
            "LockPackage",
            "OmittedOptional",
            "LockfileV3",
            "AssemblerIdentity",
            "AssemblerInputIdentity",
            "ValidatedAssemblyInput",
            "ReviewedRootMetadata",
            "RootMetadataKey",
            "IntegrityLessNode",
        ):
            self.assertTrue(hasattr(npm_environment, name), name)

    def test_legacy_environment_identity_names_are_gone(self):
        self.assertFalse(hasattr(npm_environment, "EnvironmentIdentity"))
        self.assertFalse(hasattr(npm_environment, "compute_environment_identity"))


if __name__ == "__main__":
    unittest.main()
