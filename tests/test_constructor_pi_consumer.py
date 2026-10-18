"""Phase 6 tasks 6.4/6.6 — Pi root selection, launcher, and evidence.

``bin.pi`` is consumed only from the keyed DTO entry for the exact selected
``@earendil-works/pi-coding-agent`` root identity/resolved path, never from
another reviewed root and never reparsed from raw package input.  The
consumer-created launcher's safe relative target, exact contents, non-writable
executable mode, and containment through complete symlink resolution are
verified before admission; dangling or escaping targets are rejected.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR.parent))

from docker.npm_environment import RootSpec, preflight
from docker.versioning.pi_consumer import (
    LAUNCHER_MODE,
    LAUNCHER_PATH,
    PiConsumerError,
    launcher_evidence,
    parse_launcher_evidence,
    plan_launcher,
    resolve_contained_target,
    select_pi_metadata,
    validate_launcher_evidence,
)

_FIXTURE = _THIS_DIR / "data" / "pi_install_lock_0.84.4.json"
_PACKAGE = "@earendil-works/pi-coding-agent"
_LOCK_PATH = "node_modules/@earendil-works/pi-coding-agent"
_BIN_TARGET = "dist/bundle/cli.js"


def _validated() -> object:
    raw = _FIXTURE.read_bytes()
    return preflight(
        raw,
        roots=(RootSpec(_PACKAGE, "0.84.4"),),
        platform="linux-x64",
        node_version="24.18.0",
        npm_version="11.16.0",
    )


def _tree(*, with_target: bool = True) -> Path:
    root = Path(tempfile.mkdtemp(prefix="pi-consumer-tree-"))
    target = root / _LOCK_PATH / _BIN_TARGET
    target.parent.mkdir(parents=True, exist_ok=True)
    if with_target:
        target.write_text("#!/usr/bin/env node\nconsole.log('pi')\n")
        target.chmod(0o755)
    return root


class TestSelectPiMetadata(unittest.TestCase):
    def test_selects_exact_pi_root_by_identity_and_path(self):
        meta = select_pi_metadata(_validated(), package=_PACKAGE)
        self.assertEqual(meta.package_name, _PACKAGE)
        self.assertEqual(meta.lock_path, _LOCK_PATH)
        self.assertEqual(meta.bin, (("pi", _BIN_TARGET),))
        self.assertEqual(meta.engines_node, ">=22.19.0")

    def test_unknown_package_rejected(self):
        with self.assertRaises(PiConsumerError):
            select_pi_metadata(_validated(), package="@scope/other")


class TestLauncherPlan(unittest.TestCase):
    def test_launcher_consumes_bin_pi_only_from_selected_metadata(self):
        meta = select_pi_metadata(_validated(), package=_PACKAGE)
        plan = plan_launcher(meta, environment_root=_tree())
        self.assertEqual(
            plan.target,
            f"{_LOCK_PATH}/{_BIN_TARGET}",
        )
        self.assertIn(_BIN_TARGET, plan.contents.decode())
        self.assertIn(f"../{_LOCK_PATH}/{_BIN_TARGET}", plan.contents.decode())
        self.assertEqual(plan.mode, LAUNCHER_MODE)

    def test_launcher_mode_is_non_writable_executable(self):
        self.assertEqual(LAUNCHER_MODE, 0o555)
        self.assertEqual(LAUNCHER_MODE & 0o222, 0)

    def test_dangling_target_rejected(self):
        meta = select_pi_metadata(_validated(), package=_PACKAGE)
        with self.assertRaises(PiConsumerError) as ctx:
            plan_launcher(meta, environment_root=_tree(with_target=False))
        self.assertIn("dangling", str(ctx.exception))

    def test_missing_pi_bin_rejected(self):
        meta = select_pi_metadata(_validated(), package=_PACKAGE)
        from dataclasses import replace

        no_pi = replace(meta, bin=())
        with self.assertRaises(PiConsumerError):
            plan_launcher(no_pi, environment_root=_tree())


class TestContainment(unittest.TestCase):
    def test_contained_target_resolves(self):
        root = _tree()
        resolved = resolve_contained_target(root, _LOCK_PATH, _BIN_TARGET)
        self.assertTrue(resolved.is_file())
        self.assertTrue(resolved.is_relative_to(Path(os.path.realpath(root))))

    def test_str_environment_root_resolves(self):
        root = _tree()
        resolved = resolve_contained_target(str(root), _LOCK_PATH, _BIN_TARGET)
        self.assertTrue(resolved.is_file())
        self.assertEqual(
            resolved,
            Path(os.path.realpath(root / _LOCK_PATH / _BIN_TARGET)),
        )
        self.assertTrue(resolved.is_relative_to(Path(os.path.realpath(root))))

    def test_contained_symlink_import_resolves(self):
        root = _tree()
        # Replace the CLI file with a symlink to another contained file.
        cli = root / _LOCK_PATH / _BIN_TARGET
        real = cli.parent / "real-cli.js"
        cli.unlink()
        real.write_text("#!/usr/bin/env node\n")
        os.symlink("real-cli.js", cli)
        resolved = resolve_contained_target(root, _LOCK_PATH, _BIN_TARGET)
        self.assertEqual(resolved.name, "real-cli.js")
        self.assertTrue(resolved.is_relative_to(Path(os.path.realpath(root))))

    def test_escaping_symlink_rejected(self):
        root = _tree()
        outside = Path(tempfile.mkdtemp(prefix="pi-outside-")) / "escape.js"
        outside.write_text("#!/usr/bin/env node\n")
        cli = root / _LOCK_PATH / _BIN_TARGET
        cli.unlink()
        os.symlink(os.fspath(outside), cli)
        with self.assertRaises(PiConsumerError) as ctx:
            resolve_contained_target(root, _LOCK_PATH, _BIN_TARGET)
        self.assertIn("escape", str(ctx.exception))

    def test_directory_target_rejected(self):
        root = _tree()
        cli = root / _LOCK_PATH / _BIN_TARGET
        cli.unlink()
        cli.mkdir()
        with self.assertRaises(PiConsumerError):
            resolve_contained_target(root, _LOCK_PATH, _BIN_TARGET)


class TestLauncherEvidence(unittest.TestCase):
    def test_evidence_is_deterministic_and_digest_bound(self):
        meta = select_pi_metadata(_validated(), package=_PACKAGE)
        plan = plan_launcher(meta, environment_root=_tree())
        first = launcher_evidence(plan)
        second = launcher_evidence(plan)
        self.assertEqual(first.data, second.data)
        self.assertEqual(first.digest, second.digest)
        self.assertEqual(first.digest, hashlib.sha256(first.data).hexdigest())
        self.assertEqual(len(first.digest), 64)

    def test_evidence_records_exact_contents_mode_target_containment(self):
        meta = select_pi_metadata(_validated(), package=_PACKAGE)
        plan = plan_launcher(meta, environment_root=_tree())
        evidence = launcher_evidence(plan)
        body = json.loads(evidence.data)
        self.assertEqual(body["launcher_path"], LAUNCHER_PATH)
        self.assertEqual(body["mode"], 0o555)
        self.assertEqual(body["target"], f"{_LOCK_PATH}/{_BIN_TARGET}")
        self.assertEqual(body["containment"], True)
        self.assertEqual(
            body["contents_sha256"], hashlib.sha256(plan.contents).hexdigest()
        )


class TestParseLauncherEvidence(unittest.TestCase):
    """The launcher-evidence schema is strict: exactly five fields."""

    def _evidence(self):
        meta = select_pi_metadata(_validated(), package=_PACKAGE)
        plan = plan_launcher(meta, environment_root=_tree())
        return launcher_evidence(plan), plan

    def _tampered(self, **overrides) -> bytes:
        evidence, _ = self._evidence()
        body = json.loads(evidence.data)
        body.update(overrides)
        return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def test_round_trip_parses_valid_evidence(self):
        evidence, plan = self._evidence()
        parsed = parse_launcher_evidence(evidence.data)
        self.assertEqual(parsed.launcher_path, LAUNCHER_PATH)
        self.assertEqual(
            parsed.contents_sha256,
            hashlib.sha256(plan.contents).hexdigest(),
        )
        self.assertEqual(parsed.mode, LAUNCHER_MODE)
        self.assertEqual(parsed.target, plan.target)
        self.assertIs(parsed.containment, True)
        self.assertEqual(parsed.digest, evidence.digest)
        self.assertEqual(parsed.data, evidence.data)

    def test_missing_field_rejected(self):
        evidence, _ = self._evidence()
        body = json.loads(evidence.data)
        del body["target"]
        data = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        with self.assertRaises(PiConsumerError) as ctx:
            parse_launcher_evidence(data)
        self.assertIn("missing field", str(ctx.exception))
        self.assertIn("target", str(ctx.exception))

    def test_unknown_field_rejected(self):
        data = self._tampered(extra="x")
        with self.assertRaises(PiConsumerError) as ctx:
            parse_launcher_evidence(data)
        self.assertIn("unknown field", str(ctx.exception))
        self.assertIn("extra", str(ctx.exception))

    def test_wrong_launcher_path_rejected(self):
        data = self._tampered(launcher_path="/elsewhere/pi")
        with self.assertRaises(PiConsumerError) as ctx:
            parse_launcher_evidence(data)
        self.assertIn("launcher_path", str(ctx.exception))

    def test_uppercase_sha256_rejected(self):
        evidence, _ = self._evidence()
        body = json.loads(evidence.data)
        body["contents_sha256"] = body["contents_sha256"].upper()
        data = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        with self.assertRaises(PiConsumerError) as ctx:
            parse_launcher_evidence(data)
        self.assertIn("contents_sha256", str(ctx.exception))

    def test_wrong_mode_rejected(self):
        data = self._tampered(mode=0o755)
        with self.assertRaises(PiConsumerError) as ctx:
            parse_launcher_evidence(data)
        self.assertIn("mode", str(ctx.exception))

    def test_absolute_target_rejected(self):
        data = self._tampered(target="/etc/passwd")
        with self.assertRaises(PiConsumerError) as ctx:
            parse_launcher_evidence(data)
        self.assertIn("relative", str(ctx.exception))

    def test_escaping_target_rejected(self):
        data = self._tampered(target="../outside")
        with self.assertRaises(PiConsumerError) as ctx:
            parse_launcher_evidence(data)
        self.assertIn("escape", str(ctx.exception))

    def test_containment_not_true_rejected(self):
        data = self._tampered(containment=False)
        with self.assertRaises(PiConsumerError) as ctx:
            parse_launcher_evidence(data)
        self.assertIn("containment", str(ctx.exception))


class TestValidateLauncherEvidence(unittest.TestCase):
    """Contents and target are re-bound; arbitrary bytes are never accepted."""

    def test_valid_contents_and_environment_pass(self):
        meta = select_pi_metadata(_validated(), package=_PACKAGE)
        root = _tree()
        plan = plan_launcher(meta, environment_root=root)
        evidence = launcher_evidence(plan)
        result = validate_launcher_evidence(
            evidence.data,
            launcher_contents=plan.contents,
            environment_root=root,
        )
        self.assertEqual(result.digest, evidence.digest)

    def test_str_environment_root_valid_evidence_succeeds(self):
        meta = select_pi_metadata(_validated(), package=_PACKAGE)
        root = _tree()
        plan = plan_launcher(meta, environment_root=root)
        evidence = launcher_evidence(plan)
        result = validate_launcher_evidence(
            evidence.data,
            launcher_contents=plan.contents,
            environment_root=str(root),
        )
        self.assertEqual(result.digest, evidence.digest)

    def test_contents_digest_mismatch_rejected(self):
        meta = select_pi_metadata(_validated(), package=_PACKAGE)
        root = _tree()
        plan = plan_launcher(meta, environment_root=root)
        evidence = launcher_evidence(plan)
        with self.assertRaises(PiConsumerError) as ctx:
            validate_launcher_evidence(
                evidence.data,
                launcher_contents=b"#!/bin/sh\n",
                environment_root=root,
            )
        self.assertIn("contents do not match", str(ctx.exception))

    def test_arbitrary_contents_with_matching_digest_rejected(self):
        # The canonical-script recompute rejects arbitrary bytes even when the
        # evidence's contents_sha256 records their digest.
        meta = select_pi_metadata(_validated(), package=_PACKAGE)
        root = _tree()
        plan = plan_launcher(meta, environment_root=root)
        arbitrary = b'#!/bin/sh\nexec node /tmp/elsewhere "$@"\n'
        body = json.loads(launcher_evidence(plan).data)
        body["contents_sha256"] = hashlib.sha256(arbitrary).hexdigest()
        data = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        with self.assertRaises(PiConsumerError) as ctx:
            validate_launcher_evidence(
                data,
                launcher_contents=arbitrary,
                environment_root=root,
            )
        self.assertIn("not the canonical script", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
