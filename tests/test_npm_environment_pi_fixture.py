"""Phase 1 — published Pi install-lock compatibility fixture (task 1.8).

The byte-exact checked-in ``tests/data/pi_install_lock_0.84.4.json`` is an
unsanitized copy of a pinned published Pi install lock.  This test reads and
asserts the Pi version from the fixture's own root package metadata, records
the exact immutable published source URL plus the SHA-256 of the checked-in
bytes, derives ``RootSpec`` values from exact top-level locked-node versions,
and verifies the complete ``linux-x64`` closure is accepted with only
evidence-backed ``platform-inapplicable`` omissions.  It never reads
``/opt/pi`` or any installed agent.
"""

from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR.parent))

from docker.npm_environment import (
    LockfileV3,
    RootSpec,
    ValidatedAssemblyInput,
    compute_assembler_identity,
    compute_assembler_input_identity,
    parse_lockfile,
    preflight,
)

_FIXTURE = _THIS_DIR / "data" / "pi_install_lock_0.84.4.json"

# Immutable published source corresponding to the version embedded in the
# fixture's own root package metadata (commit-pinned tag v0.84.4).
_SOURCE_URL = (
    "https://raw.githubusercontent.com/earendil-works/pi/"
    "b79e4cc834970cca69daebffab7df1da7d1e52c4/"
    "packages/coding-agent/install-lock/package-lock.json"
)

# SHA-256 of the checked-in bytes (verified against the published source).
_SHA256 = "a9f805a677f0860328059390b0f62adcc655299952f293127a8db8939818dff4"

_NODE_VERSION = "24.18.0"
_NPM_VERSION = "11.16.0"


class TestPublishedPiInstallLock(unittest.TestCase):
    """The byte-exact published Pi install-lock parses as a complete closure."""

    @classmethod
    def _raw(cls) -> bytes:
        return _FIXTURE.read_bytes()

    def test_fixture_lives_under_tests_data_not_opt_pi(self):
        self.assertTrue(_FIXTURE.is_relative_to(_THIS_DIR / "data"))
        self.assertIn("tests/data", str(_FIXTURE))

    def test_sha256_matches_recorded_provenance(self):
        self.assertEqual(hashlib.sha256(self._raw()).hexdigest(), _SHA256)

    def test_pi_version_read_from_root_package_metadata(self):
        data = json.loads(self._raw().decode())
        root = data["packages"][""]
        self.assertEqual(root["name"], "@earendil-works/pi-coding-agent-install")
        self.assertEqual(root["version"], "0.84.4")

    def test_roots_derived_from_exact_top_level_locked_versions(self):
        data = json.loads(self._raw().decode())
        top_deps = data["packages"][""]["dependencies"]
        # Every top-level dependency spec is an exact locked version, never a
        # root-manifest range.
        roots = tuple(RootSpec(n, v) for n, v in top_deps.items())
        self.assertEqual(len(roots), 1)
        self.assertEqual(roots[0].name, "@earendil-works/pi-coding-agent")
        self.assertEqual(roots[0].version, "0.84.4")
        for name, version in top_deps.items():
            self.assertFalse(any(c in version for c in "^~|<>*"))

    def test_fixture_exercises_required_metadata_forms(self):
        data = json.loads(self._raw().decode())
        packages = data["packages"]

        def count(pred) -> int:
            return sum(1 for p, n in packages.items() if p and pred(n))

        self.assertGreaterEqual(count(lambda n: "funding" in n), 1)
        self.assertGreaterEqual(count(lambda n: "deprecated" in n), 1)
        self.assertGreaterEqual(count(lambda n: "bin" in n), 1)
        self.assertGreaterEqual(count(lambda n: "license" in n), 1)
        self.assertGreaterEqual(
            count(
                lambda n: (
                    isinstance(n.get("engines"), dict)
                    and "||" in str(n["engines"].get("node", ""))
                )
            ),
            1,
        )
        # Manifest engines.node is syntax-valid and must be discarded.
        self.assertEqual(data["packages"][""]["engines"]["node"], ">=22.19.0")

    def test_complete_linux_x64_closure_accepted(self):
        raw = self._raw()
        roots = (RootSpec("@earendil-works/pi-coding-agent", "0.84.4"),)
        lockfile = parse_lockfile(raw, platform="linux-x64", roots=roots)
        self.assertIsInstance(lockfile, LockfileV3)

        data = json.loads(raw.decode())
        total = len(data["packages"]) - 1  # exclude the manifest node
        installed = len(lockfile.packages)
        omitted = len(lockfile.omitted_optionals)
        # Every lock node is either installed or evidence-backed omitted.
        self.assertEqual(installed + omitted, total)

        # Every omission is backed by locked platform metadata.
        self.assertTrue(
            all(o.reason == "platform-inapplicable" for o in lockfile.omitted_optionals)
        )

    def test_reviewed_root_metadata_and_integrity_less_records(self):
        raw = self._raw()
        roots = (RootSpec("@earendil-works/pi-coding-agent", "0.84.4"),)
        lockfile = parse_lockfile(raw, platform="linux-x64", roots=roots)

        self.assertEqual(len(lockfile.root_metadata), 1)
        meta = lockfile.root_metadata[0]
        self.assertEqual(meta.package_name, "@earendil-works/pi-coding-agent")
        self.assertEqual(meta.lock_path, "node_modules/@earendil-works/pi-coding-agent")
        self.assertEqual(meta.bin, (("pi", "dist/bundle/cli.js"),))
        self.assertEqual(meta.engines_node, ">=22.19.0")

        # The published install-lock synthesizes the internal workspace
        # packages without integrity; all seven are recorded explicitly.
        self.assertEqual(len(lockfile.integrity_less), 7)
        for record in lockfile.integrity_less:
            self.assertTrue(record.name.startswith("@earendil-works/pi-"))
            self.assertTrue(record.resolved.startswith("https://registry.npmjs.org/"))
            self.assertEqual(record.version, "0.84.4")
        names = {r.name for r in lockfile.integrity_less}
        self.assertIn("@earendil-works/pi-coding-agent", names)

    def test_preflight_binds_digest_and_tools(self):
        raw = self._raw()
        roots = (RootSpec("@earendil-works/pi-coding-agent", "0.84.4"),)
        validated = preflight(
            raw,
            roots=roots,
            platform="linux-x64",
            node_version=_NODE_VERSION,
            npm_version=_NPM_VERSION,
        )
        self.assertIsInstance(validated, ValidatedAssemblyInput)
        self.assertEqual(validated.lockfile_digest, _SHA256)
        self.assertEqual(validated.roots, roots)
        self.assertEqual(validated.node_version, _NODE_VERSION)
        self.assertEqual(validated.npm_version, _NPM_VERSION)

    def test_repeated_model_and_identity_derivation_deterministic(self):
        raw = self._raw()
        roots = (RootSpec("@earendil-works/pi-coding-agent", "0.84.4"),)
        first = preflight(
            raw,
            roots=roots,
            platform="linux-x64",
            node_version=_NODE_VERSION,
            npm_version=_NPM_VERSION,
        )
        second = preflight(
            raw,
            roots=roots,
            platform="linux-x64",
            node_version=_NODE_VERSION,
            npm_version=_NPM_VERSION,
        )
        self.assertEqual(first.packages, second.packages)
        self.assertEqual(first.root_metadata, second.root_metadata)
        self.assertEqual(first.omitted_optionals, second.omitted_optionals)
        self.assertEqual(first.lockfile_digest, second.lockfile_digest)

        assembler = compute_assembler_identity(
            image_digest="sha256:" + "a" * 64,
            node_version=_NODE_VERSION,
            npm_version=_NPM_VERSION,
            script_digest="script-digest",
            policy_digest="policy-digest",
            platform="linux-x64",
        )
        ident_first = compute_assembler_input_identity(first, assembler)
        ident_second = compute_assembler_input_identity(second, assembler)
        self.assertEqual(ident_first.digest, ident_second.digest)
        self.assertEqual(ident_first.lockfile_digest, _SHA256)


if __name__ == "__main__":
    unittest.main()
