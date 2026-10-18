"""Phase 1 — canonical assembler/assembler-input identities (task 1.4).

``AssemblerInputIdentity`` identifies assembly *inputs* only: canonical
reviewed roots, the exact lockfile-byte digest, and the assembler identity.
It is derived only from a successful preflight :class:`ValidatedAssemblyInput`,
and the supplied value is re-verified by re-running preflight rather than
trusted, so callers cannot bypass preflight validation or substitute fields.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import sys
import unittest
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR.parent))

from docker.npm_environment import (
    AssemblerInputIdentity,
    IntegrityLessNode,
    LockedNpmError,
    OmittedOptional,
    ReviewedRootMetadata,
    RootSpec,
    ValidatedAssemblyInput,
    compute_assembler_identity,
    compute_assembler_input_identity,
    preflight,
)

_IMAGE = "sha256:" + "a" * 64
_NODE = "24.18.0"
_NPM = "11.16.0"
_SCRIPT = "script-digest"
_POLICY = "policy-digest"
_PLATFORM = "linux-x64"


def _sri() -> str:
    return "sha512-" + base64.b64encode(bytes([0xAB]) * 64).decode()


def _url(name: str, version: str) -> str:
    stem = name.rsplit("/", 1)[-1]
    return f"https://registry.npmjs.org/{name}/-/{stem}-{version}.tgz"


def _pkg_node(name: str, version: str, **extra: object) -> dict:
    node: dict = {
        "version": version,
        "resolved": _url(name, version),
        "integrity": _sri(),
    }
    node.update(extra)
    return node


def _assembler(**overrides: str) -> object:
    kwargs = {
        "image_digest": _IMAGE,
        "node_version": _NODE,
        "npm_version": _NPM,
        "script_digest": _SCRIPT,
        "policy_digest": _POLICY,
        "platform": _PLATFORM,
    }
    kwargs.update(overrides)
    return compute_assembler_identity(**kwargs)


def _lock(roots: dict[str, str], packages: dict[str, dict] | None = None) -> bytes:
    pkg_nodes: dict[str, dict] = {
        f"node_modules/{name}": _pkg_node(name, version)
        for name, version in roots.items()
    }
    if packages:
        pkg_nodes.update(packages)
    return json.dumps(
        {
            "name": "root",
            "version": "1.0.0",
            "lockfileVersion": 3,
            "requires": True,
            "packages": {
                "": {"name": "root", "version": "1.0.0", "dependencies": roots},
                **pkg_nodes,
            },
        }
    ).encode()


def _validated(
    roots: dict[str, str],
    *,
    platform: str = _PLATFORM,
    node_version: str = _NODE,
    npm_version: str = _NPM,
) -> tuple[bytes, ValidatedAssemblyInput]:
    raw = _lock(roots)
    validated = preflight(
        raw,
        roots=tuple(RootSpec(n, v) for n, v in sorted(roots.items())),
        platform=platform,
        node_version=node_version,
        npm_version=npm_version,
    )
    return raw, validated


class TestAssemblerIdentity(unittest.TestCase):
    def test_deterministic(self):
        first = _assembler()
        second = _assembler()
        self.assertEqual(first.digest, second.digest)

    def test_each_component_affects_digest(self):
        base = _assembler()
        for field in (
            "image_digest",
            "node_version",
            "npm_version",
            "script_digest",
            "policy_digest",
            "platform",
        ):
            changed = _assembler(**{field: f"{field}-changed"})
            self.assertNotEqual(base.digest, changed.digest, field)

    def test_components_exposed_on_identity(self):
        ident = _assembler()
        self.assertEqual(ident.image_digest, _IMAGE)
        self.assertEqual(ident.node_version, _NODE)
        self.assertEqual(ident.npm_version, _NPM)
        self.assertEqual(ident.script_digest, _SCRIPT)
        self.assertEqual(ident.policy_digest, _POLICY)
        self.assertEqual(ident.platform, _PLATFORM)


class TestAssemblerInputIdentity(unittest.TestCase):
    def test_type_describes_inputs_only(self):
        # No output-tree, evidence, or assembled-output fields are allowed.
        fields = {f.name for f in dataclasses.fields(AssemblerInputIdentity)}
        self.assertEqual(
            fields,
            {"roots", "lockfile_digest", "assembler", "digest", "package_digest"},
        )

    def test_canonical_roots_sorted_regardless_of_input_order(self):
        _raw_a, validated_a = _validated({"b": "1.0.0", "a": "1.0.0"})
        _raw_b, validated_b = _validated({"a": "1.0.0", "b": "1.0.0"})
        ident_a = compute_assembler_input_identity(validated_a, _assembler())
        ident_b = compute_assembler_input_identity(validated_b, _assembler())
        self.assertEqual([r.name for r in ident_a.roots], ["a", "b"])
        self.assertEqual(ident_a.roots, ident_b.roots)

    def test_lock_bytes_affect_identity(self):
        _raw_a, validated_a = _validated({"a": "1.0.0"})
        _raw_b, validated_b = _validated({"a": "1.0.1"})
        ident_a = compute_assembler_input_identity(validated_a, _assembler())
        ident_b = compute_assembler_input_identity(validated_b, _assembler())
        self.assertNotEqual(ident_a.lockfile_digest, ident_b.lockfile_digest)
        self.assertNotEqual(ident_a.digest, ident_b.digest)

    def test_assembler_identity_affects_input_identity(self):
        _raw, validated = _validated({"a": "1.0.0"})
        ident_a = compute_assembler_input_identity(validated, _assembler())
        ident_b = compute_assembler_input_identity(
            validated, _assembler(image_digest="sha256:" + "b" * 64)
        )
        self.assertNotEqual(ident_a.digest, ident_b.digest)

    def test_lockfile_digest_is_sha256_of_exact_bytes(self):
        raw, validated = _validated({"a": "1.0.0"})
        ident = compute_assembler_input_identity(validated, _assembler())
        self.assertEqual(ident.lockfile_digest, hashlib.sha256(raw).hexdigest())

    def test_exact_byte_digest_verification(self):
        raw, validated = _validated({"a": "1.0.0"})
        self.assertEqual(validated.lockfile_digest, hashlib.sha256(raw).hexdigest())
        ident = compute_assembler_input_identity(validated, _assembler())
        self.assertEqual(ident.lockfile_digest, validated.lockfile_digest)

    def test_platform_mismatch_rejected(self):
        _raw, validated = _validated({"a": "1.0.0"})  # linux-x64
        with self.assertRaises(LockedNpmError) as ctx:
            compute_assembler_input_identity(
                validated, _assembler(platform="darwin-arm64")
            )
        self.assertEqual(ctx.exception.reason, "platform_mismatch")

    def test_node_version_mismatch_rejected(self):
        _raw, validated = _validated({"a": "1.0.0"})
        with self.assertRaises(LockedNpmError) as ctx:
            compute_assembler_input_identity(
                validated, _assembler(node_version="20.0.0")
            )
        self.assertEqual(ctx.exception.reason, "node_version_mismatch")

    def test_npm_version_mismatch_rejected(self):
        _raw, validated = _validated({"a": "1.0.0"})
        with self.assertRaises(LockedNpmError) as ctx:
            compute_assembler_input_identity(
                validated, _assembler(npm_version="10.0.0")
            )
        self.assertEqual(ctx.exception.reason, "npm_version_mismatch")

    def test_incompatible_root_engine_blocked_before_identity(self):
        raw = _lock(
            {"a": "1.0.0"},
            packages={
                "node_modules/a": _pkg_node("a", "1.0.0", engines={"node": ">=99"})
            },
        )
        with self.assertRaises(LockedNpmError) as ctx:
            preflight(
                raw,
                roots=(RootSpec("a", "1.0.0"),),
                platform=_PLATFORM,
                node_version=_NODE,
                npm_version=_NPM,
            )
        self.assertEqual(ctx.exception.reason, "incompatible_node_engine")

    def test_whitespace_and_newline_changes_produce_different_identities(self):
        raw_a, validated_a = _validated({"a": "1.0.0"})
        raw_b = raw_a + b"\n"
        validated_b = preflight(
            raw_b,
            roots=(RootSpec("a", "1.0.0"),),
            platform=_PLATFORM,
            node_version=_NODE,
            npm_version=_NPM,
        )
        ident_a = compute_assembler_input_identity(validated_a, _assembler())
        ident_b = compute_assembler_input_identity(validated_b, _assembler())
        self.assertNotEqual(ident_a.lockfile_digest, ident_b.lockfile_digest)
        self.assertNotEqual(ident_a.digest, ident_b.digest)

    def test_coherent_platform_change_affects_digest(self):
        raw, validated_linux = _validated({"a": "1.0.0"})
        validated_other = preflight(
            raw,
            roots=(RootSpec("a", "1.0.0"),),
            platform="darwin-arm64",
            node_version=_NODE,
            npm_version=_NPM,
        )
        ident_linux = compute_assembler_input_identity(validated_linux, _assembler())
        ident_other = compute_assembler_input_identity(
            validated_other, _assembler(platform="darwin-arm64")
        )
        self.assertNotEqual(ident_linux.digest, ident_other.digest)

    def test_input_identity_digest_is_deterministic(self):
        _raw, validated = _validated({"a": "1.0.0"})
        first = compute_assembler_input_identity(validated, _assembler())
        second = compute_assembler_input_identity(validated, _assembler())
        self.assertEqual(first.digest, second.digest)


class TestValidatedInputRejection(unittest.TestCase):
    """A substituted validated input is re-verified, never trusted."""

    def _expect_rejected(self, validated, reason, **changes):
        tampered = dataclasses.replace(validated, **changes)
        with self.assertRaises(LockedNpmError) as ctx:
            compute_assembler_input_identity(tampered, _assembler())
        self.assertEqual(ctx.exception.reason, reason)

    def test_lockfile_bytes_substitution_rejected(self):
        _raw, validated = _validated({"a": "1.0.0"})
        tampered = dataclasses.replace(
            validated, lockfile_bytes=validated.lockfile_bytes + b"\n"
        )
        with self.assertRaises(LockedNpmError) as ctx:
            compute_assembler_input_identity(tampered, _assembler())
        self.assertEqual(ctx.exception.reason, "lockfile_bytes_mismatch")

    def test_lockfile_digest_substitution_rejected(self):
        _raw, validated = _validated({"a": "1.0.0"})
        self._expect_rejected(
            validated, "lockfile_bytes_mismatch", lockfile_digest="0" * 64
        )

    def test_semantic_field_substitution_rejected(self):
        _raw, validated = _validated({"a": "1.0.0", "b": "1.0.0"})
        cases = {
            "roots": {"roots": tuple(reversed(validated.roots))},
            "packages": {"packages": ()},
            "omitted_optionals": {
                "omitted_optionals": (
                    OmittedOptional(
                        "", "x", "1.0.0", "platform-inapplicable", _PLATFORM
                    ),
                )
            },
            "integrity_less": {
                "integrity_less": (
                    IntegrityLessNode(
                        "x",
                        "node_modules/x",
                        "1.0.0",
                        "https://registry.npmjs.org/x/-/x-1.0.0.tgz",
                    ),
                )
            },
            "root_metadata": {"root_metadata": ()},
        }
        for label, changes in cases.items():
            with self.subTest(field=label):
                self._expect_rejected(validated, "validated_input_mismatch", **changes)


class TestInputDeterminism(unittest.TestCase):
    """Identical preflight inputs yield identical identities."""

    def test_identical_preflight_yields_identical_identity(self):
        raw = _lock({"a": "1.0.0", "b": "1.0.0"})
        roots = (RootSpec("a", "1.0.0"), RootSpec("b", "1.0.0"))

        def run():
            return preflight(
                raw,
                roots=roots,
                platform=_PLATFORM,
                node_version=_NODE,
                npm_version=_NPM,
            )

        first = run()
        second = run()
        self.assertEqual(first, second)
        ident_first = compute_assembler_input_identity(first, _assembler())
        ident_second = compute_assembler_input_identity(second, _assembler())
        self.assertEqual(ident_first.digest, ident_second.digest)

    def test_root_argument_order_canonicalized(self):
        raw = _lock({"a": "1.0.0", "b": "1.0.0"})
        first = preflight(
            raw,
            roots=(RootSpec("a", "1.0.0"), RootSpec("b", "1.0.0")),
            platform=_PLATFORM,
            node_version=_NODE,
            npm_version=_NPM,
        )
        second = preflight(
            raw,
            roots=(RootSpec("b", "1.0.0"), RootSpec("a", "1.0.0")),
            platform=_PLATFORM,
            node_version=_NODE,
            npm_version=_NPM,
        )
        self.assertEqual(first.roots, second.roots)
        ident_first = compute_assembler_input_identity(first, _assembler())
        ident_second = compute_assembler_input_identity(second, _assembler())
        self.assertEqual(ident_first.digest, ident_second.digest)


class TestAssemblerTamperRejection(unittest.TestCase):
    """A stale or forged assembler digest is rejected, never trusted."""

    def test_component_tampering_rejected(self):
        _raw, validated = _validated({"a": "1.0.0"})
        assembler = _assembler()
        cases = {
            "image_digest": {"image_digest": "sha256:" + "b" * 64},
            "node_version": {"node_version": "20.0.0"},
            "npm_version": {"npm_version": "10.0.0"},
            "script_digest": {"script_digest": "tampered-script"},
            "policy_digest": {"policy_digest": "tampered-policy"},
            "platform": {"platform": "darwin-arm64"},
        }
        for label, changes in cases.items():
            with self.subTest(component=label):
                tampered = dataclasses.replace(assembler, **changes)
                with self.assertRaises(LockedNpmError) as ctx:
                    compute_assembler_input_identity(validated, tampered)
                self.assertEqual(ctx.exception.reason, "assembler_identity_mismatch")

    def test_digest_only_tampering_rejected(self):
        _raw, validated = _validated({"a": "1.0.0"})
        assembler = _assembler()
        tampered = dataclasses.replace(assembler, digest="0" * 64)
        with self.assertRaises(LockedNpmError) as ctx:
            compute_assembler_input_identity(validated, tampered)
        self.assertEqual(ctx.exception.reason, "assembler_identity_mismatch")


if __name__ == "__main__":
    unittest.main()
