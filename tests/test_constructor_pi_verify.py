"""Image-side Pi verification (``docker/verify-pi.mjs``) acceptance tests.

Each test runs the actual Node verification script against a temp ``/opt/pi``
tree plus locally-generated assembler and launcher evidence, proving the
Dockerfile verification fails for changed files, a substituted tree with the
original evidence, a tampered evidence body, a tampered output identity, and
escaping or dangling symlinks.  No Docker daemon or network is involved.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR.parent))

# Repository/test path setup must run before these imports.
from docker.npm_environment import (  # noqa: E402
    RootSpec,
    npm_policy_flags,
    preflight,
)
from docker.npm_environment.evidence import (  # noqa: E402
    AssemblerEvidence,
    AssemblerEvidenceBody,
    compute_assembled_output_identity,
    evidence_body_digest,
    serialize_evidence,
)
from docker.npm_environment.identity import AssemblerIdentity, AssemblerInputIdentity  # noqa: E402
from docker.npm_environment.tree import build_tree_manifest  # noqa: E402
from docker.versioning.pi_consumer import (  # noqa: E402
    launcher_evidence,
    plan_launcher,
    select_pi_metadata,
)

VERIFY_PI = _THIS_DIR.parent / "docker" / "verify-pi.mjs"
_FIXTURE = _THIS_DIR / "data" / "pi_install_lock_0.84.4.json"
_PACKAGE = "@earendil-works/pi-coding-agent"
_LOCK_PATH = "node_modules/@earendil-works/pi-coding-agent"
_BIN_TARGET = "dist/bundle/cli.js"
PI_VERSION = "0.84.4"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _input_identity() -> AssemblerInputIdentity:
    assembler = AssemblerIdentity(
        image_digest="0" * 64,
        node_version="24.18.0",
        npm_version="11.16.0",
        script_digest="0" * 64,
        policy_digest="0" * 64,
        platform="linux-x64",
        digest="0" * 64,
    )
    return AssemblerInputIdentity(
        roots=(),
        lockfile_digest="0" * 64,
        assembler=assembler,
        digest="a" * 64,
    )


def _launcher_plan(pi_root: Path):
    validated = preflight(
        _FIXTURE.read_bytes(),
        roots=(RootSpec(_PACKAGE, PI_VERSION),),
        platform="linux-x64",
        node_version="24.18.0",
        npm_version="11.16.0",
    )
    metadata = select_pi_metadata(validated, package=_PACKAGE)
    return plan_launcher(metadata, environment_root=pi_root)


def _make_tree_read_only(root: Path) -> None:
    """Strip every write bit, mirroring the assembler's read-only publish."""
    for entry in sorted(root.rglob("*")):
        if entry.is_symlink():
            continue
        os.chmod(
            entry,
            stat.S_IMODE(entry.lstat().st_mode) & ~0o222,
            follow_symlinks=False,
        )


def _make_writable(root: Path) -> None:
    """Restore write bits so a read-only fixture tree can be mutated."""
    for entry in sorted(root.rglob("*")):
        if entry.is_symlink():
            continue
        os.chmod(
            entry,
            stat.S_IMODE(entry.lstat().st_mode) | 0o222,
            follow_symlinks=False,
        )


class _Scenario:
    """A fully-built valid verification scenario (tree + evidence + env)."""

    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="pi-verify-")
        base = Path(self.tmp.name)
        self.pi_root = base / "opt" / "pi"
        self.evidence_dir = base / "evidence"
        self.evidence_dir.mkdir(parents=True)

        # Assembled tree (before the consumer launcher is added).
        cli = self.pi_root / _LOCK_PATH / _BIN_TARGET
        cli.parent.mkdir(parents=True, exist_ok=True)
        cli.write_text("#!/usr/bin/env node\nconsole.log('0.84.4');\n")
        cli.chmod(0o755)
        (self.pi_root / "package-lock.json").write_text(
            '{"name":"@earendil-works/pi-coding-agent-install","lockfileVersion":3}\n'
        )
        real = cli.parent / "real.js"
        real.write_text("#!/usr/bin/env node\nconsole.log('0.84.4');\n")
        os.symlink("real.js", cli.parent / "cli-link.js")

        # The assembler publishes a read-only tree (every write bit stripped);
        # seal the fixture before rebuilding the canonical manifest so the
        # evidence modes reflect the published, immutable bytes.
        _make_tree_read_only(self.pi_root)
        self.manifest = build_tree_manifest(self.pi_root)
        self.tree_digest = self.manifest.digest

        # Consumer launcher at opt/pi/bin/pi.
        self.launcher_plan = _launcher_plan(self.pi_root)
        launcher = self.pi_root / "bin" / "pi"
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.write_bytes(self.launcher_plan.contents)
        launcher.chmod(self.launcher_plan.mode)

        self.launcher_evidence = launcher_evidence(self.launcher_plan)
        # Rewrite the launcher evidence path for this test's PI_ROOT (the
        # production constant is /opt/pi/bin/pi).
        launcher_body = json.loads(self.launcher_evidence.data)
        launcher_body["launcher_path"] = str(launcher)
        self.launcher_evidence_bytes = json.dumps(
            launcher_body, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        self.launcher_evidence_digest = _sha256(self.launcher_evidence_bytes)

        # Assembler evidence using production serialization.
        input_identity = _input_identity()
        body = AssemblerEvidenceBody(
            input_identity=input_identity,
            tree_digest=self.tree_digest,
            tree_entries=self.manifest.entries,
            packages=(),
            omitted_optionals=(),
            integrity_less=(),
            root_metadata=(),
            npm_policy_flags=npm_policy_flags(),
        )
        self.evidence_digest = evidence_body_digest(body)
        self.output_identity = compute_assembled_output_identity(
            input_identity,
            self.tree_digest,
            self.evidence_digest,
        ).digest
        self.assembler_evidence = AssemblerEvidence(
            output_identity=self.output_identity,
            input_identity=input_identity,
            tree_digest=self.tree_digest,
            evidence_digest=self.evidence_digest,
            body=body,
        )
        self._write_evidence()

    # ── evidence-file materialization ────────────────────────────────

    def _write_evidence(self) -> None:
        (self.evidence_dir / "pi-assembler-evidence.json").write_bytes(
            serialize_evidence(self.assembler_evidence)
        )
        (self.evidence_dir / "pi-launcher-evidence.json").write_bytes(
            self.launcher_evidence_bytes
        )

    def _env(self, **extra: str) -> dict[str, str]:
        env = {
            "PI_VERSION": PI_VERSION,
            "PI_ASSEMBLED_OUTPUT_IDENTITY": self.output_identity,
            "PI_TREE_DIGEST": self.tree_digest,
            "PI_ASSEMBLER_EVIDENCE_DIGEST": self.evidence_digest,
            "PI_ASSEMBLER_EVIDENCE_BYTES_DIGEST": _sha256(
                serialize_evidence(self.assembler_evidence)
            ),
            "PI_LAUNCHER_EVIDENCE_DIGEST": self.launcher_evidence_digest,
            "PI_ROOT": str(self.pi_root),
            "PI_ASSEMBLER_EVIDENCE_PATH": str(
                self.evidence_dir / "pi-assembler-evidence.json"
            ),
            "PI_LAUNCHER_EVIDENCE_PATH": str(
                self.evidence_dir / "pi-launcher-evidence.json"
            ),
        }
        env.update(extra)
        return env

    def run_verify(self, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["node", str(VERIFY_PI)],
            env={**os.environ, **self._env(**extra)},
            capture_output=True,
            text=True,
        )

    def cleanup(self) -> None:
        self.tmp.cleanup()


class TestVerifyPiScript(unittest.TestCase):
    def _scenario(self) -> _Scenario:
        scenario = _Scenario()
        self.addCleanup(scenario.cleanup)
        return scenario

    def test_valid_tree_and_evidence_pass(self) -> None:
        scenario = self._scenario()
        proc = scenario.run_verify()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("pi verification OK", proc.stdout)

    def test_changed_file_under_pi_root_fails(self) -> None:
        scenario = self._scenario()
        cli = scenario.pi_root / _LOCK_PATH / _BIN_TARGET
        cli.chmod(0o755)  # temporarily writable to rewrite the read-only file
        cli.write_text("#!/usr/bin/env node\nconsole.log('tampered');\n")
        cli.chmod(0o555)  # restore the sealed mode; only the contents changed
        proc = scenario.run_verify()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("tree digest", proc.stderr)

    def test_substituted_tree_with_original_evidence_fails(self) -> None:
        scenario = self._scenario()
        # Replace the imported tree wholesale while keeping the original
        # evidence and launcher.
        _make_writable(scenario.pi_root)
        shutil.rmtree(scenario.pi_root)
        scenario.pi_root.mkdir(parents=True)
        (scenario.pi_root / "package-lock.json").write_text(
            '{"lockfileVersion":3,"substituted":true}\n'
        )
        cli = scenario.pi_root / _LOCK_PATH / _BIN_TARGET
        cli.parent.mkdir(parents=True, exist_ok=True)
        cli.write_text("#!/usr/bin/env node\nconsole.log('0.84.4');\n")
        launcher = scenario.pi_root / "bin" / "pi"
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.write_bytes(scenario.launcher_plan.contents)
        launcher.chmod(scenario.launcher_plan.mode)
        proc = scenario.run_verify()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("tree digest", proc.stderr)

    def test_modified_serialized_evidence_bytes_digest_fails(self) -> None:
        scenario = self._scenario()
        proc = scenario.run_verify(PI_ASSEMBLER_EVIDENCE_BYTES_DIGEST="f" * 64)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("serialized assembler evidence digest", proc.stderr)

    def test_modified_evidence_body_with_unchanged_digest_fields_fails(self) -> None:
        scenario = self._scenario()
        evidence = json.loads(serialize_evidence(scenario.assembler_evidence).decode())
        # Tamper with a body field while leaving the top-level digest fields
        # (evidence_digest/output_identity/tree_digest) unchanged.
        evidence["body"]["npm_policy_flags"].append("--tampered")
        evidence_path = scenario.evidence_dir / "pi-assembler-evidence.json"
        evidence_path.write_text(
            json.dumps(evidence, sort_keys=True, separators=(",", ":"))
        )
        proc = scenario.run_verify(
            PI_ASSEMBLER_EVIDENCE_BYTES_DIGEST=_sha256(evidence_path.read_bytes())
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("evidence body digest", proc.stderr)

    def test_modified_output_identity_fails(self) -> None:
        scenario = self._scenario()
        evidence = json.loads(serialize_evidence(scenario.assembler_evidence).decode())
        evidence["output_identity"] = "f" * 64
        evidence_path = scenario.evidence_dir / "pi-assembler-evidence.json"
        evidence_path.write_text(
            json.dumps(evidence, sort_keys=True, separators=(",", ":"))
        )
        proc = scenario.run_verify(
            PI_ASSEMBLER_EVIDENCE_BYTES_DIGEST=_sha256(evidence_path.read_bytes())
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("output identity", proc.stderr)

    def test_escaping_symlink_in_imported_tree_fails(self) -> None:
        scenario = self._scenario()
        bundle = scenario.pi_root / _LOCK_PATH / "dist" / "bundle"
        bundle.chmod(0o755)  # temporarily writable to add the symlink
        os.symlink("../../../outside.js", bundle / "escape.js")
        proc = scenario.run_verify()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("symlink", proc.stderr)

    def test_dangling_symlink_in_imported_tree_fails(self) -> None:
        scenario = self._scenario()
        bundle = scenario.pi_root / _LOCK_PATH / "dist" / "bundle"
        bundle.chmod(0o755)  # temporarily writable to add the symlink
        os.symlink("does-not-exist.js", bundle / "dangling.js")
        proc = scenario.run_verify()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("dangling", proc.stderr)

    def test_cli_target_execute_bit_removed_fails(self) -> None:
        scenario = self._scenario()
        cli = scenario.pi_root / _LOCK_PATH / _BIN_TARGET
        cli.chmod(0o444)  # drop the executable bits, contents unchanged
        proc = scenario.run_verify()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("executable bits", proc.stderr)

    def test_execute_bit_added_where_absent_fails(self) -> None:
        scenario = self._scenario()
        pkg = scenario.pi_root / "package-lock.json"  # evidence has no exec bits
        pkg.chmod(0o555)  # add executable bits
        proc = scenario.run_verify()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("executable bits", proc.stderr)

    def test_write_bit_added_to_file_fails(self) -> None:
        scenario = self._scenario()
        pkg = scenario.pi_root / "package-lock.json"  # sealed 0o444
        pkg.chmod(0o644)  # add the owner write bit
        proc = scenario.run_verify()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("writable", proc.stderr)

    def test_write_bit_added_to_directory_fails(self) -> None:
        scenario = self._scenario()
        d = scenario.pi_root / "node_modules"  # sealed 0o555
        d.chmod(0o755)  # add the owner write bit
        proc = scenario.run_verify()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("writable", proc.stderr)


if __name__ == "__main__":
    unittest.main()
