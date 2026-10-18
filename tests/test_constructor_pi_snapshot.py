"""Phase 6 tasks 6.4/6.6 + validation-before-admission — Pi snapshot tests.

The assembled Pi tree, consumer launcher, and both evidence sets are admitted
to the immutable snapshot only after every derived-environment binding is
re-validated against the attestation.  The source tree is re-mapped against the
assembler evidence, the launcher is re-verified against the launcher evidence,
the tree is copied only after validation, and the copied staging tree is
recomputed and re-validated before publication.  The two checksum-verified
installation files are never exposed.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR.parent))

# Repository/test path setup must run before these imports.
import docker.versioning.build_snapshot as build_snapshot_module  # noqa: E402
from docker.versioning.build_materialization import SelectedBuildArtifact  # noqa: E402
from docker.versioning.build_snapshot import (  # noqa: E402
    DerivedEnvironmentSource,
    SnapshotError,
    cleanup_artifact_snapshot,
    create_artifact_snapshot,
)
from docker.versioning.digest_identity import DigestIdentity  # noqa: E402
from docker.versioning.project_state import resolve_project_state  # noqa: E402
from tests.pi_fixtures import PI_BIN_TARGET, build_pi_attestation  # noqa: E402

_CLI_REL = "node_modules/@earendil-works/pi-coding-agent/" + PI_BIN_TARGET

# Snapshot-relative location of the isolated Pi derived-environment directory.
_ISOLATION = Path("derived-environments/pi")


def _selected(root: Path):
    result = []
    for name, payload in (
        ("rustup", b"r"),
        ("uv", b"u"),
        ("rtk", b"t"),
        ("fd", b"f"),
    ):
        blob = root / f"{name}.blob"
        blob.write_bytes(payload)
        blob.chmod(0o444)
        result.append(
            (
                SelectedBuildArtifact(
                    name,
                    "https://not-exposed.invalid/",
                    DigestIdentity.from_hex(
                        "sha256",
                        hashlib.sha256(payload).hexdigest(),
                    ),
                ),
                blob,
            )
        )
    return result


def _tree() -> Path:
    """Create a minimal assembled Pi tree whose ``bin.pi`` target exists."""
    root = Path(tempfile.mkdtemp(prefix="pi-snapshot-tree-"))
    cli = root / _CLI_REL
    cli.parent.mkdir(parents=True, exist_ok=True)
    cli.write_text("#!/usr/bin/env node\nconsole.log('pi')\n")
    cli.chmod(0o755)
    return root


def _derived_source(parts) -> DerivedEnvironmentSource:
    """Build the full attestation-bearing derived-environment source."""
    return DerivedEnvironmentSource(
        environment_root=parts.environment_root,
        launcher_contents=parts.launcher_plan.contents,
        launcher_mode=parts.launcher_plan.mode,
        assembler_evidence=parts.assembler_evidence,
        launcher_evidence=parts.launcher_evidence.data,
        assembler_evidence_digest=parts.assembler_evidence_digest,
        assembler_evidence_bytes_digest=parts.assembler_evidence_bytes_digest,
        launcher_evidence_digest=parts.launcher_evidence_digest,
        assembled_output_identity=parts.output_identity,
        canonical_tree_digest=parts.tree_digest,
    )


def _parts_for(tree: Path):
    return build_pi_attestation(environment_root=tree)


class _SnapshotHarness:
    """Build the four prebuilt blobs plus one derived-environment source."""

    def __init__(self, test: unittest.TestCase, derived: DerivedEnvironmentSource):
        self.test = test
        self.tmp = tempfile.TemporaryDirectory(prefix="pi-snapshot-root-")
        test.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pairs = _selected(self.root)
        self.cache = self.root / "cache"
        self.cache.mkdir(mode=0o700)
        self.state = resolve_project_state(self.root, cache_root=self.cache)
        self.derived = derived

    def run(self):
        snapshot = create_artifact_snapshot(
            (x[0] for x in self.pairs),
            (x[1] for x in self.pairs),
            constructor_project_root=self.root,
            project_state=self.state,
            derived=self.derived,
        )
        self.test.addCleanup(cleanup_artifact_snapshot, snapshot)
        return snapshot

    def assert_fails(self, message_substr: str) -> None:
        with self.test.assertRaises(SnapshotError) as ctx:
            create_artifact_snapshot(
                (x[0] for x in self.pairs),
                (x[1] for x in self.pairs),
                constructor_project_root=self.root,
                project_state=self.state,
                derived=self.derived,
            )
        self.test.assertIn(message_substr, str(ctx.exception))
        # The staging tree was removed; no snapshot was published.
        self.test.assertEqual([], list(self.cache.rglob("transaction-*")))


class TestPiSnapshotAdmission(unittest.TestCase):
    def test_admits_tree_launcher_and_both_evidence_sets(self):
        tree = _tree()
        self.addCleanup(shutil.rmtree, tree, True)
        derived = _derived_source(_parts_for(tree))
        snapshot = _SnapshotHarness(self, derived).run()
        tree_cli = snapshot.path / _ISOLATION / "opt" / "pi" / _CLI_REL
        self.assertTrue(tree_cli.is_file())
        # Executable bit preserved, write bits removed.
        self.assertEqual(stat.S_IMODE(tree_cli.stat().st_mode), 0o555)
        launcher = snapshot.path / _ISOLATION / "opt" / "pi" / "bin" / "pi"
        self.assertEqual(stat.S_IMODE(launcher.stat().st_mode), 0o555)
        assembler_evidence = snapshot.path / _ISOLATION / "pi-assembler-evidence.json"
        launcher_evidence = snapshot.path / _ISOLATION / "pi-launcher-evidence.json"
        self.assertEqual(stat.S_IMODE(assembler_evidence.stat().st_mode), 0o444)
        self.assertEqual(stat.S_IMODE(launcher_evidence.stat().st_mode), 0o444)
        manifest = json.loads(snapshot.manifest.read_text())
        self.assertIn("pi", manifest)
        self.assertEqual(manifest["pi"]["isolation_dir"], _ISOLATION.as_posix())
        self.assertEqual(
            manifest["pi"]["tree"]["path"],
            (_ISOLATION / "opt" / "pi").as_posix(),
        )
        self.assertEqual(
            manifest["pi"]["launcher"]["path"],
            (_ISOLATION / "opt" / "pi" / "bin" / "pi").as_posix(),
        )
        self.assertEqual(
            manifest["pi"]["assembler_evidence"]["path"],
            (_ISOLATION / "pi-assembler-evidence.json").as_posix(),
        )
        self.assertEqual(
            manifest["pi"]["launcher_evidence"]["path"],
            (_ISOLATION / "pi-launcher-evidence.json").as_posix(),
        )

    def test_isolated_layout_and_prebuilt_artifacts_outside(self):
        tree = _tree()
        self.addCleanup(shutil.rmtree, tree, True)
        derived = _derived_source(_parts_for(tree))
        snapshot = _SnapshotHarness(self, derived).run()

        # The snapshot root holds only the four prebuilt logical names, the
        # manifest, and the single isolation directory — no Pi evidence or
        # tree at the root.
        self.assertEqual(
            {p.name for p in snapshot.path.iterdir()},
            {
                "rustup-init",
                "uv.tar.gz",
                "rtk.deb",
                "fd.deb",
                "manifest.json",
                "derived-environments",
            },
        )
        self.assertFalse((snapshot.path / "pi-assembler-evidence.json").exists())
        self.assertFalse((snapshot.path / "pi-launcher-evidence.json").exists())
        self.assertFalse((snapshot.path / "opt").exists())

        isolation = snapshot.path / _ISOLATION
        # The complete Pi tree and both evidence sets live only under the
        # isolation directory.
        self.assertEqual(
            {p.name for p in isolation.iterdir()},
            {"opt", "pi-assembler-evidence.json", "pi-launcher-evidence.json"},
        )
        self.assertTrue((isolation / "opt" / "pi" / _CLI_REL).is_file())
        self.assertTrue((isolation / "opt" / "pi" / "bin" / "pi").is_file())
        self.assertTrue((isolation / "pi-assembler-evidence.json").is_file())
        self.assertTrue((isolation / "pi-launcher-evidence.json").is_file())

        # The whole isolation directory and every descendant are immutable.
        self.assertEqual(stat.S_IMODE(isolation.stat().st_mode), 0o555)
        for path in isolation.rglob("*"):
            mode = stat.S_IMODE(path.stat().st_mode)
            if path.is_dir():
                self.assertEqual(mode, 0o555, path)
            else:
                self.assertEqual(mode & 0o222, 0, path)

        # Prebuilt artifacts remain outside the isolation directory.
        for name in ("rustup-init", "uv.tar.gz", "rtk.deb", "fd.deb"):
            self.assertTrue((snapshot.path / name).is_file())
        self.assertFalse(
            any(
                p.name in {"rustup-init", "uv.tar.gz", "rtk.deb", "fd.deb"}
                for p in isolation.rglob("*")
            )
        )

    def test_installation_files_are_absent_from_snapshot(self):
        tree = _tree()
        self.addCleanup(shutil.rmtree, tree, True)
        derived = _derived_source(_parts_for(tree))
        snapshot = _SnapshotHarness(self, derived).run()
        manifest = snapshot.manifest.read_text()
        self.assertNotIn("pi-coding-agent-install-package.json", manifest)
        self.assertNotIn("pi-coding-agent-install-package-lock.json", manifest)
        names = {p.name for p in snapshot.path.rglob("*")}
        self.assertNotIn("pi-coding-agent-install-package.json", names)
        self.assertNotIn("pi-coding-agent-install-package-lock.json", names)

    def test_contained_symlink_preserved_no_follow(self):
        tree = _tree()
        self.addCleanup(shutil.rmtree, tree, True)
        cli = tree / _CLI_REL
        real = cli.parent / "real.js"
        cli.unlink()
        real.write_text("#!/usr/bin/env node\n")
        real.chmod(0o755)
        os.symlink("real.js", cli)
        derived = _derived_source(_parts_for(tree))
        snapshot = _SnapshotHarness(self, derived).run()
        imported = snapshot.path / _ISOLATION / "opt" / "pi" / _CLI_REL
        self.assertTrue(imported.is_symlink())
        self.assertEqual(os.readlink(imported), "real.js")

    def test_escaping_symlink_rejected(self):
        tree = _tree()
        self.addCleanup(shutil.rmtree, tree, True)
        parts = _parts_for(tree)
        outside = Path(tempfile.mkdtemp(prefix="pi-escape-")) / "escape.js"
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        outside.write_text("#!/usr/bin/env node\n")
        cli = tree / _CLI_REL
        cli.unlink()
        os.symlink(os.fspath(outside), cli)
        derived = _derived_source(parts)
        _SnapshotHarness(self, derived).assert_fails("validation failed")


class TestPiSnapshotValidation(unittest.TestCase):
    """Every derived-environment binding is re-validated before any copy."""

    def _parts(self):
        tree = _tree()
        self.addCleanup(shutil.rmtree, tree, True)
        return tree, _parts_for(tree)

    @staticmethod
    def _mutated_launcher_evidence(parts, mutate, *, canonical=True):
        """Re-serialize launcher evidence with a mutated body and fresh digest."""
        body = json.loads(parts.launcher_evidence.data)
        mutate(body)
        new_data = (
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
            if canonical
            else json.dumps(body, indent=2).encode("utf-8")
        )
        return new_data, hashlib.sha256(new_data).hexdigest()

    def _derived_with_launcher(self, parts, mutate, *, canonical=True):
        new_data, new_digest = self._mutated_launcher_evidence(
            parts, mutate, canonical=canonical
        )
        return dataclasses.replace(
            _derived_source(parts),
            launcher_evidence=new_data,
            launcher_evidence_digest=new_digest,
        )

    def test_source_tree_changed_after_materialization_rejected(self):
        tree, parts = self._parts()
        cli = tree / _CLI_REL
        cli.write_text("#!/usr/bin/env node\nconsole.log('tampered')\n")
        derived = _derived_source(parts)
        _SnapshotHarness(self, derived).assert_fails(
            "source tree does not match the assembler evidence"
        )

    def test_copied_tree_differing_from_source_rejected(self):
        tree, parts = self._parts()
        real_copy = build_snapshot_module._copy_contained_tree

        def divergent_copy(source, destination):
            real_copy(source, destination)
            copied_cli = destination / _CLI_REL
            os.chmod(copied_cli, 0o644)
            copied_cli.write_text("#!/usr/bin/env node\nconsole.log('tampered')\n")
            os.chmod(copied_cli, 0o555)

        derived = _derived_source(parts)
        with patch.object(
            build_snapshot_module, "_copy_contained_tree", side_effect=divergent_copy
        ):
            _SnapshotHarness(self, derived).assert_fails("copied Pi tree")

    def test_each_attestation_value_incorrect_rejected(self):
        cases = {
            "assembler_evidence_digest": (
                "assembler evidence body digest does not match the attestation"
            ),
            "assembler_evidence_bytes_digest": (
                "assembler evidence digest does not match the attestation"
            ),
            "launcher_evidence_digest": (
                "launcher evidence digest does not match the attestation"
            ),
            "assembled_output_identity": (
                "assembler evidence output identity does not match the attestation"
            ),
            "canonical_tree_digest": (
                "assembler evidence tree digest does not match the attestation"
            ),
        }
        for field, message in cases.items():
            with self.subTest(attestation_field=field):
                tree, parts = self._parts()
                derived = dataclasses.replace(
                    _derived_source(parts), **{field: "0" * 64}
                )
                _SnapshotHarness(self, derived).assert_fails(message)

    def test_substituted_assembler_evidence_rejected(self):
        tree, parts = self._parts()
        other_tree = _tree()
        self.addCleanup(shutil.rmtree, other_tree, True)
        other_cli = other_tree / _CLI_REL
        other_cli.write_text("#!/usr/bin/env node\nconsole.log('other')\n")
        other_cli.chmod(0o755)
        other_parts = _parts_for(other_tree)
        derived = dataclasses.replace(
            _derived_source(parts),
            assembler_evidence=other_parts.assembler_evidence,
        )
        _SnapshotHarness(self, derived).assert_fails(
            "assembler evidence digest does not match the attestation"
        )

    def test_substituted_launcher_contents_rejected(self):
        tree, parts = self._parts()
        derived = dataclasses.replace(
            _derived_source(parts),
            launcher_contents=b'#!/bin/sh\nexec node /tmp/elsewhere "$@"\n',
        )
        _SnapshotHarness(self, derived).assert_fails(
            "launcher contents do not match the launcher evidence"
        )

    def test_substituted_launcher_evidence_rejected(self):
        tree, parts = self._parts()
        derived = dataclasses.replace(
            _derived_source(parts),
            launcher_evidence=b'{"launcher_path":"/opt/pi/bin/pi"}\n',
        )
        _SnapshotHarness(self, derived).assert_fails(
            "launcher evidence digest does not match the attestation"
        )

    def test_launcher_target_containment_rejected(self):
        # The launcher evidence targets a file that no longer resolves inside
        # the environment (the target was replaced by a dangling link).
        tree, parts = self._parts()
        cli = tree / _CLI_REL
        cli.unlink()
        os.symlink("/definitely/outside", cli)
        derived = _derived_source(parts)
        _SnapshotHarness(self, derived).assert_fails("validation failed")

    def test_launcher_evidence_unknown_field_rejected(self):
        # Even with a recomputed digest, an unknown field violates the strict
        # shared launcher-evidence schema used by snapshot admission.
        tree, parts = self._parts()
        body = json.loads(parts.launcher_evidence.data)
        body["extra"] = "x"
        new_data = json.dumps(body, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        derived = dataclasses.replace(
            _derived_source(parts),
            launcher_evidence=new_data,
            launcher_evidence_digest=hashlib.sha256(new_data).hexdigest(),
        )
        _SnapshotHarness(self, derived).assert_fails("unknown field")

    def test_launcher_contents_not_canonical_script_rejected(self):
        # Arbitrary bytes are rejected even when their digest is recorded, via
        # the canonical-script recompute from the evidenced target.
        tree, parts = self._parts()
        arbitrary = b'#!/bin/sh\nexec node /tmp/elsewhere "$@"\n'
        body = json.loads(parts.launcher_evidence.data)
        body["contents_sha256"] = hashlib.sha256(arbitrary).hexdigest()
        new_data = json.dumps(body, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        derived = dataclasses.replace(
            _derived_source(parts),
            launcher_contents=arbitrary,
            launcher_evidence=new_data,
            launcher_evidence_digest=hashlib.sha256(new_data).hexdigest(),
        )
        _SnapshotHarness(self, derived).assert_fails("not the canonical script")

    def test_launcher_evidence_missing_field_rejected(self):
        tree, parts = self._parts()
        derived = self._derived_with_launcher(
            parts, lambda body: body.pop("containment")
        )
        _SnapshotHarness(self, derived).assert_fails("missing field")

    def test_launcher_evidence_wrong_path_rejected(self):
        tree, parts = self._parts()
        derived = self._derived_with_launcher(
            parts, lambda body: body.__setitem__("launcher_path", "/usr/local/bin/pi")
        )
        _SnapshotHarness(self, derived).assert_fails("does not equal")

    def test_launcher_evidence_malformed_contents_digest_rejected(self):
        tree, parts = self._parts()
        for bad in ("not-a-digest", 123, "A" * 64, None):
            with self.subTest(contents_sha256=bad):
                derived = self._derived_with_launcher(
                    parts,
                    lambda body, bad=bad: body.__setitem__("contents_sha256", bad),
                )
                _SnapshotHarness(self, derived).assert_fails("contents_sha256")

    def test_launcher_evidence_incorrect_mode_rejected(self):
        tree, parts = self._parts()
        for bad in (0o777, 0o755, "0o555", True):
            with self.subTest(mode=bad):
                derived = self._derived_with_launcher(
                    parts,
                    lambda body, bad=bad: body.__setitem__("mode", bad),
                )
                _SnapshotHarness(self, derived).assert_fails("mode")

    def test_launcher_evidence_absolute_target_rejected(self):
        tree, parts = self._parts()
        derived = self._derived_with_launcher(
            parts, lambda body: body.__setitem__("target", "/usr/bin/node")
        )
        _SnapshotHarness(self, derived).assert_fails("not a relative POSIX path")

    def test_launcher_evidence_escaping_target_rejected(self):
        tree, parts = self._parts()
        derived = self._derived_with_launcher(
            parts, lambda body: body.__setitem__("target", "../outside.js")
        )
        _SnapshotHarness(self, derived).assert_fails("escapes its base")

    def test_non_canonical_but_valid_launcher_evidence_admitted(self):
        # Exact canonical evidence bytes are NOT part of the admission
        # contract: admission binds the byte digest to the attestation and
        # then re-validates the parsed schema and values.  Valid fields
        # serialized with different whitespace are therefore still admitted.
        tree, parts = self._parts()
        derived = self._derived_with_launcher(parts, lambda body: None, canonical=False)
        snapshot = _SnapshotHarness(self, derived).run()
        launcher_evidence = snapshot.path / _ISOLATION / "pi-launcher-evidence.json"
        self.assertEqual(launcher_evidence.read_bytes(), derived.launcher_evidence)

    def test_launcher_failures_precede_tree_copy_and_publication(self):
        # A launcher-evidence schema failure raises before the derived tree is
        # copied and before any snapshot is published: the copy function must
        # never be reached and no transaction directory may remain.
        tree, parts = self._parts()
        derived = self._derived_with_launcher(parts, lambda body: body.pop("target"))
        harness = _SnapshotHarness(self, derived)
        with patch.object(
            build_snapshot_module,
            "_copy_contained_tree",
            side_effect=AssertionError(
                "derived tree must not be copied on validation failure"
            ),
        ):
            harness.assert_fails("missing field")


if __name__ == "__main__":
    unittest.main()
