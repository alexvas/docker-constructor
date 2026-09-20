"""Phase 5 — result/evidence serialization contracts (task 5.4).

Evidence and result DTOs serialize to deterministic canonical bytes that
cover every input, package, integrity omission, flag, and path, and carry no
Pi/runtime-specific fields.  Parsing re-verifies every digest and rejects
substituted tree/evidence pairs rather than trusting serialized claims.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR.parent))

from docker.npm_environment import (
    LockedNpmError,
    RootSpec,
    assembler_script_digest,
    compute_assembler_identity,
    compute_assembler_input_identity,
    npm_policy_digest,
    parse_evidence,
    parse_result,
    preflight,
    publication,
    publish_environment,
    serialize_evidence,
    serialize_result,
)

_IMAGE = "sha256:" + "a" * 64
_NODE = "24.18.0"
_NPM = "11.16.0"
_PLATFORM = "linux-x64"


def _sri() -> str:
    return "sha512-" + base64.b64encode(bytes([0xAB]) * 64).decode()


def _url(name: str, version: str) -> str:
    stem = name.rsplit("/", 1)[-1]
    return f"https://registry.npmjs.org/{name}/-/{stem}-{version}.tgz"


def _rich_lock() -> bytes:
    return json.dumps(
        {
            "name": "root",
            "version": "1.0.0",
            "lockfileVersion": 3,
            "requires": True,
            "packages": {
                "": {
                    "name": "root",
                    "version": "1.0.0",
                    "dependencies": {"a": "1.0.0"},
                },
                "node_modules/a": {
                    "version": "1.0.0",
                    "resolved": _url("a", "1.0.0"),
                    "integrity": _sri(),
                    "bin": {"acmd": "bin/a.js"},
                    "engines": {"node": ">=20.0.0"},
                    "dependencies": {"dep": "^1.0.0"},
                    "optionalDependencies": {"native": "^2.0.0"},
                },
                "node_modules/dep": {
                    "version": "1.0.0",
                    "resolved": _url("dep", "1.0.0"),
                },
                "node_modules/native": {
                    "version": "2.0.0",
                    "resolved": _url("native", "2.0.0"),
                    "integrity": _sri(),
                    "optional": True,
                    "os": ["win32"],
                },
            },
        }
    ).encode()


def _validated():
    return preflight(
        _rich_lock(),
        roots=(RootSpec("a", "1.0.0"),),
        platform=_PLATFORM,
        node_version=_NODE,
        npm_version=_NPM,
    )


def _assembler():
    return compute_assembler_identity(
        image_digest=_IMAGE,
        node_version=_NODE,
        npm_version=_NPM,
        script_digest=assembler_script_digest(),
        policy_digest=npm_policy_digest(),
        platform=_PLATFORM,
    )


def _write_pkg(root: Path, path: str, name: str, version: str, **extra: object) -> None:
    p = root / path / "package.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {"name": name, "version": version}
    data.update(extra)
    p.write_text(json.dumps(data))


def _write_tree(root: Path) -> None:
    _write_pkg(
        root, "node_modules/a", "a", "1.0.0",
        dependencies={"dep": "^1.0.0"},
        optionalDependencies={"native": "^2.0.0"},
    )
    _write_pkg(root, "node_modules/dep", "dep", "1.0.0")
    _write_pkg(root, "", "root", "1.0.0")
    # The lock declares ``a``'s bin target ``bin/a.js``; the assembled tree
    # must contain it for output validation to pass.
    bin_dir = root / "node_modules" / "a" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "a.js").write_text("#!/usr/bin/env node\n")


_RESULT_KEYS = frozenset(
    {
        "environment_root", "evidence_path", "output_identity", "input_identity",
        "tree_digest", "evidence_digest", "roots", "root_metadata", "packages",
        "omitted_optionals", "integrity_less", "image_digest", "node_version",
        "npm_version", "script_digest", "policy_digest", "platform",
        "lockfile_digest", "npm_policy_flags", "tree_entries",
    }
)

_EVIDENCE_KEYS = frozenset(
    {
        "output_identity", "input_identity", "tree_digest", "evidence_digest",
        "body",
    }
)


class _SerializationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="npm-env-serialize-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.cache_root = self.base / "cache"
        self.cache_root.mkdir()
        self.validated = _validated()
        self.assembler = _assembler()
        self.namespace = publication.prepare_assembler_namespace(
            self.cache_root, self.assembler.digest
        )
        self.input_identity = compute_assembler_input_identity(
            self.validated, self.assembler
        )
        tree = self.base / "tree"
        tree.mkdir()
        _write_tree(tree)
        self.result = publish_environment(
            validated=self.validated,
            tree_root=tree,
            namespace=self.namespace,
            input_identity=self.input_identity,
        )
        self.evidence = parse_evidence(
            (self.result.evidence_path).read_bytes()
        )


class TestRoundTripAndDeterminism(_SerializationTestCase):
    def test_evidence_round_trips_to_identical_bytes(self):
        data = serialize_evidence(self.evidence)
        reparsed = parse_evidence(data)
        self.assertEqual(serialize_evidence(reparsed), data)

    def test_result_round_trips_to_identical_bytes(self):
        data = serialize_result(self.result)
        reparsed = parse_result(data)
        self.assertEqual(serialize_result(reparsed), data)

    def test_serialization_is_deterministic(self):
        self.assertEqual(
            serialize_evidence(self.evidence), serialize_evidence(self.evidence)
        )
        self.assertEqual(serialize_result(self.result), serialize_result(self.result))

    def test_result_and_evidence_agree(self):
        data = serialize_result(self.result)
        reparsed = parse_result(data)
        self.assertEqual(reparsed.output_identity, self.evidence.output_identity)
        self.assertEqual(reparsed.tree_digest, self.evidence.tree_digest)
        self.assertEqual(reparsed.evidence_digest, self.evidence.evidence_digest)
        self.assertEqual(reparsed.input_identity, self.evidence.input_identity)


class TestCoversEveryRequiredField(_SerializationTestCase):
    def test_result_serialization_covers_every_field(self):
        raw = json.loads(serialize_result(self.result).decode("utf-8"))
        self.assertEqual(set(raw), _RESULT_KEYS)
        self.assertEqual(raw["image_digest"], _IMAGE)
        self.assertEqual(raw["node_version"], _NODE)
        self.assertEqual(raw["npm_version"], _NPM)
        self.assertEqual(raw["platform"], _PLATFORM)
        self.assertEqual(raw["lockfile_digest"], self.validated.lockfile_digest)
        self.assertEqual(
            raw["npm_policy_flags"],
            [
                "--ignore-scripts",
                "--no-bin-links",
                "--no-audit",
                "--no-fund",
                "--loglevel=http",
            ],
        )
        # Every input, package, integrity omission, flag, and path present.
        self.assertEqual(
            [p["name"] for p in raw["packages"]], ["a", "dep"]
        )
        self.assertEqual(
            [i["name"] for i in raw["integrity_less"]], ["dep"]
        )
        self.assertEqual(
            [o["name"] for o in raw["omitted_optionals"]], ["native"]
        )
        self.assertEqual(
            [m["package_name"] for m in raw["root_metadata"]], ["a"]
        )
        self.assertEqual(raw["root_metadata"][0]["bin"], [["acmd", "bin/a.js"]])
        self.assertEqual(raw["root_metadata"][0]["engines_node"], ">=20.0.0")
        self.assertTrue(raw["environment_root"])
        self.assertTrue(raw["evidence_path"])
        self.assertTrue(raw["tree_entries"])

    def test_evidence_serialization_covers_every_field(self):
        raw = json.loads(serialize_evidence(self.evidence).decode("utf-8"))
        self.assertEqual(set(raw), _EVIDENCE_KEYS)
        self.assertEqual(raw["output_identity"], self.result.output_identity)
        self.assertEqual(raw["tree_digest"], self.result.tree_digest)
        self.assertEqual(raw["evidence_digest"], self.result.evidence_digest)
        body = raw["body"]
        self.assertEqual(
            [p["name"] for p in body["packages"]], ["a", "dep"]
        )
        self.assertEqual(
            [i["name"] for i in body["integrity_less"]], ["dep"]
        )
        self.assertEqual(
            [o["name"] for o in body["omitted_optionals"]], ["native"]
        )
        self.assertEqual(
            [m["package_name"] for m in body["root_metadata"]], ["a"]
        )

    def test_no_pi_or_runtime_fields(self):
        result_raw = serialize_result(self.result).decode("utf-8")
        evidence_raw = serialize_evidence(self.evidence).decode("utf-8")
        for forbidden in (
            "launcher", "extension", "settings", "build_context",
            "cli_guard", "consumer", "pi", "runtime",
        ):
            self.assertNotIn(forbidden, result_raw.lower())
            self.assertNotIn(forbidden, evidence_raw.lower())


class TestRejectsSubstitution(_SerializationTestCase):
    def test_evidence_tree_digest_substitution_rejected(self):
        data = bytearray(serialize_evidence(self.evidence))
        text = json.loads(data.decode("utf-8"))
        text["tree_digest"] = "f" * 64
        with self.assertRaises(LockedNpmError):
            parse_evidence(json.dumps(text).encode("utf-8"))

    def test_evidence_output_identity_substitution_rejected(self):
        text = json.loads(serialize_evidence(self.evidence).decode("utf-8"))
        text["output_identity"] = "f" * 64
        with self.assertRaises(LockedNpmError):
            parse_evidence(json.dumps(text).encode("utf-8"))

    def test_evidence_tree_entry_substitution_rejected(self):
        text = json.loads(serialize_evidence(self.evidence).decode("utf-8"))
        text["body"]["tree_entries"][0]["digest"] = "f" * 64
        with self.assertRaises(LockedNpmError):
            parse_evidence(json.dumps(text).encode("utf-8"))

    def test_evidence_body_swap_rejected(self):
        # A body from a different evidence envelope cannot be spliced in
        # without breaking the evidence-body digest binding.
        text = json.loads(serialize_evidence(self.evidence).decode("utf-8"))
        text["body"]["tree_digest"] = "f" * 64
        with self.assertRaises(LockedNpmError):
            parse_evidence(json.dumps(text).encode("utf-8"))

    def test_result_tree_digest_substitution_rejected(self):
        text = json.loads(serialize_result(self.result).decode("utf-8"))
        text["tree_digest"] = "f" * 64
        with self.assertRaises(LockedNpmError):
            parse_result(json.dumps(text).encode("utf-8"))

    def test_result_output_identity_substitution_rejected(self):
        text = json.loads(serialize_result(self.result).decode("utf-8"))
        text["output_identity"] = "f" * 64
        with self.assertRaises(LockedNpmError):
            parse_result(json.dumps(text).encode("utf-8"))


class TestRedundantFieldSubstitution(_SerializationTestCase):
    """Each redundant result field must agree with the input identity."""

    def test_each_redundant_field_substitution_rejected(self):
        raw = json.loads(serialize_result(self.result).decode("utf-8"))
        cases = {
            "roots": [{"name": "tampered", "version": "1.0.0"}],
            "lockfile_digest": "f" * 64,
            "image_digest": "sha256:" + "f" * 64,
            "node_version": "99.99.99",
            "npm_version": "99.99.99",
            "script_digest": "f" * 64,
            "policy_digest": "f" * 64,
            "platform": "tampered-platform",
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                altered = json.loads(json.dumps(raw))
                altered[field] = value
                with self.assertRaises(LockedNpmError) as ctx:
                    parse_result(json.dumps(altered).encode("utf-8"))
                self.assertEqual(ctx.exception.reason, "evidence_malformed")


class TestCanonicalTreeEntryEncoding(_SerializationTestCase):
    """Tree entries must be canonically ordered and duplicate-path free."""

    def _swap_first_two(self, entries: list) -> None:
        entries[0], entries[1] = entries[1], entries[0]

    def _duplicate_first(self, entries: list) -> None:
        entries.insert(1, json.loads(json.dumps(entries[0])))

    def test_evidence_reordered_tree_entries_rejected(self):
        text = json.loads(serialize_evidence(self.evidence).decode("utf-8"))
        self._swap_first_two(text["body"]["tree_entries"])
        with self.assertRaises(LockedNpmError) as ctx:
            parse_evidence(json.dumps(text).encode("utf-8"))
        self.assertEqual(ctx.exception.reason, "evidence_malformed")

    def test_evidence_duplicate_tree_entry_path_rejected(self):
        text = json.loads(serialize_evidence(self.evidence).decode("utf-8"))
        self._duplicate_first(text["body"]["tree_entries"])
        with self.assertRaises(LockedNpmError) as ctx:
            parse_evidence(json.dumps(text).encode("utf-8"))
        self.assertEqual(ctx.exception.reason, "evidence_malformed")

    def test_result_reordered_tree_entries_rejected(self):
        text = json.loads(serialize_result(self.result).decode("utf-8"))
        self._swap_first_two(text["tree_entries"])
        with self.assertRaises(LockedNpmError) as ctx:
            parse_result(json.dumps(text).encode("utf-8"))
        self.assertEqual(ctx.exception.reason, "evidence_malformed")

    def test_result_duplicate_tree_entry_path_rejected(self):
        text = json.loads(serialize_result(self.result).decode("utf-8"))
        self._duplicate_first(text["tree_entries"])
        with self.assertRaises(LockedNpmError) as ctx:
            parse_result(json.dumps(text).encode("utf-8"))
        self.assertEqual(ctx.exception.reason, "evidence_malformed")


if __name__ == "__main__":
    unittest.main()
