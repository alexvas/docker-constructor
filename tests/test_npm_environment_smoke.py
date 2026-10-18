"""Phase 3 — host smoke script core (task 3.8).

The dedicated host smoke script (``scripts/smoke-npm-assembler``) accepts
only the reviewed immutable image reference and an explicit report-file
path, runs the production assembly path end to end, and fails unless the
installed closure matches, no lifecycle script ran, and no executable link
exists.  These tests cover argument validation, failure propagation, and
report serialization through an injected executor — no nested Docker.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR.parent))

# Repository/test path setup must run before these imports.
from docker.npm_environment import (  # noqa: E402
    LIFECYCLE_MARKER,
    SMOKE_NODE_VERSION,
    SMOKE_NPM_VERSION,
    SMOKE_PLATFORM,
    SMOKE_REPORT_SCHEMA_VERSION,
    LockedNpmError,
    ProcessResult,
    assembler_script_digest,
    extract_image_digest,
    npm_policy_digest,
    parse_smoke_args,
    preflight,
    run_smoke,
    serialize_smoke_report,
    smoke_fixture_bytes,
    smoke_fixture_digest,
    smoke_roots,
)

_IMAGE = "sha256:" + "a" * 64
_REPO_IMAGE = "registry.example/node@sha256:" + "b" * 64
_REPO_DIGEST = "sha256:" + "b" * 64
_TAGGED_IMAGE = "registry.example/node:24@sha256:" + "c" * 64
_TAGGED_DIGEST = "sha256:" + "c" * 64

#: The validated closure of the embedded fixture: lock path -> (name, version).
_FIXTURE_PACKAGES = {
    pkg.path: (pkg.name, pkg.version)
    for pkg in preflight(
        smoke_fixture_bytes(),
        roots=smoke_roots(),
        platform=SMOKE_PLATFORM,
        node_version=SMOKE_NODE_VERSION,
        npm_version=SMOKE_NPM_VERSION,
    ).packages
}


class SmokeExecutor:
    """Fake executor that materialises a scripted ``node_modules`` tree.

    ``packages`` maps ``node_modules``-relative lock paths to
    ``(name, version)`` pairs; each gets a matching ``package.json`` so the
    closure verifier can check name/version identity.
    """

    def __init__(
        self,
        *,
        return_code: int = 0,
        stdout: str = "",
        stderr: str = "",
        packages: dict[str, tuple[str, str]] | None = None,
        create_bin: bool = False,
        create_marker: bool = False,
        package_lock: bool = True,
    ):
        self.return_code = return_code
        self.stdout = stdout
        self.stderr = stderr
        self.packages = dict(_FIXTURE_PACKAGES if packages is None else packages)
        self.create_bin = create_bin
        self.create_marker = create_marker
        self.package_lock = package_lock
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: tuple[str, ...]):
        self.calls.append(argv)
        staging = self._staging(argv)
        node_modules = staging / "node_modules"
        node_modules.mkdir(parents=True, exist_ok=True)
        for rel, (name, version) in self.packages.items():
            pkg_dir = staging / rel
            pkg_dir.mkdir(parents=True, exist_ok=True)
            (pkg_dir / "package.json").write_text(
                json.dumps({"name": name, "version": version})
            )
        if self.package_lock:
            (node_modules / ".package-lock.json").write_text("{}")
        if self.create_bin:
            (node_modules / ".bin").mkdir(exist_ok=True)
        if self.create_marker:
            marker = staging / LIFECYCLE_MARKER
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("postinstall ran")
        return ProcessResult(argv, self.return_code, self.stdout, self.stderr)

    def _staging(self, argv: tuple[str, ...]) -> Path:
        for i, tok in enumerate(argv):
            if tok == "--volume":
                return Path(argv[i + 1].split(":", 1)[0])
        raise AssertionError(f"no --volume mount in argv: {argv}")


def _report_path(tmp: Path) -> Path:
    return tmp / "report.json"


def _without(packages: dict[str, tuple[str, str]], *rels: str):
    return {k: v for k, v in packages.items() if k not in rels}


class SmokeTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="npm-env-smoke-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.cache_root = self.tmp / "cache"
        self.cache_root.mkdir()

    def _run(self, executor, *, image=_IMAGE, report_path=None):
        report_path = report_path or _report_path(self.tmp)
        return run_smoke(
            image=image,
            report_path=report_path,
            executor=executor,
            cache_root=self.cache_root,
            timestamp="2025-06-01T00:00:00+00:00",
        )

    def _report(self, report_path=None):
        report_path = report_path or _report_path(self.tmp)
        return json.loads(report_path.read_text())


class TestParseSmokeArgs(SmokeTestCase):
    def test_rejects_wrong_argument_count(self):
        for argv in ([], ["img"], ["img", "report", "extra"]):
            with self.assertRaises(LockedNpmError) as ctx:
                parse_smoke_args(argv)
            self.assertEqual(ctx.exception.reason, "invalid_smoke_arguments")

    def test_rejects_mutable_image_reference(self):
        with self.assertRaises(LockedNpmError) as ctx:
            parse_smoke_args(["node:24", str(_report_path(self.tmp))])
        self.assertEqual(ctx.exception.reason, "invalid_image_reference")

    def test_rejects_empty_report_path(self):
        with self.assertRaises(LockedNpmError) as ctx:
            parse_smoke_args([_IMAGE, ""])
        self.assertEqual(ctx.exception.reason, "invalid_smoke_arguments")

    def test_rejects_missing_report_parent(self):
        missing = self.tmp / "does-not-exist" / "report.json"
        with self.assertRaises(LockedNpmError) as ctx:
            parse_smoke_args([_IMAGE, str(missing)])
        self.assertEqual(ctx.exception.reason, "invalid_smoke_arguments")

    def test_rejects_directory_report_path(self):
        with self.assertRaises(LockedNpmError) as ctx:
            parse_smoke_args([_IMAGE, str(self.tmp)])
        self.assertEqual(ctx.exception.reason, "invalid_smoke_arguments")

    def test_accepts_valid_arguments(self):
        args = parse_smoke_args([_IMAGE, str(_report_path(self.tmp))])
        self.assertEqual(args.image, _IMAGE)
        self.assertEqual(args.report_path, str(_report_path(self.tmp)))


class TestFailurePropagation(SmokeTestCase):
    def test_assembly_nonzero_exit_fails(self):
        code = self._run(SmokeExecutor(return_code=3))
        self.assertEqual(code, 1)
        report = self._report()
        self.assertEqual(report["result"], "fail")
        self.assertEqual(report["failure"]["reason"], "npm_exit_nonzero")
        statuses = {c["name"]: c["status"] for c in report["checks"]}
        self.assertEqual(statuses["assembly"], "fail")

    def test_missing_package_fails(self):
        code = self._run(
            SmokeExecutor(packages=_without(_FIXTURE_PACKAGES, "node_modules/isexe"))
        )
        self.assertEqual(code, 1)
        report = self._report()
        self.assertEqual(report["failure"]["reason"], "smoke_closure_mismatch")
        statuses = {c["name"]: c["status"] for c in report["checks"]}
        self.assertEqual(statuses["installed_closure"], "fail")

    def test_extra_top_level_package_fails(self):
        packages = dict(_FIXTURE_PACKAGES)
        packages["node_modules/extra"] = ("extra", "1.0.0")
        code = self._run(SmokeExecutor(packages=packages))
        self.assertEqual(code, 1)
        report = self._report()
        self.assertEqual(report["failure"]["reason"], "smoke_closure_mismatch")

    def test_wrong_version_fails(self):
        packages = dict(_FIXTURE_PACKAGES)
        packages["node_modules/isexe"] = ("isexe", "9.9.9")
        code = self._run(SmokeExecutor(packages=packages))
        self.assertEqual(code, 1)
        report = self._report()
        self.assertEqual(report["failure"]["reason"], "smoke_closure_mismatch")

    def test_unexpected_nested_package_fails(self):
        packages = dict(_FIXTURE_PACKAGES)
        packages["node_modules/which/node_modules/extra"] = ("extra", "1.0.0")
        code = self._run(SmokeExecutor(packages=packages))
        self.assertEqual(code, 1)
        report = self._report()
        self.assertEqual(report["failure"]["reason"], "smoke_closure_mismatch")

    def test_unexpected_scoped_package_fails(self):
        packages = dict(_FIXTURE_PACKAGES)
        packages["node_modules/@evil/foo"] = ("@evil/foo", "1.0.0")
        code = self._run(SmokeExecutor(packages=packages))
        self.assertEqual(code, 1)
        report = self._report()
        self.assertEqual(report["failure"]["reason"], "smoke_closure_mismatch")

    def test_lifecycle_marker_file_fails(self):
        code = self._run(SmokeExecutor(create_marker=True))
        self.assertEqual(code, 1)
        report = self._report()
        self.assertEqual(report["failure"]["reason"], "smoke_lifecycle_effect")
        statuses = {c["name"]: c["status"] for c in report["checks"]}
        self.assertEqual(statuses["no_lifecycle_script_effect"], "fail")

    def test_executable_link_fails(self):
        code = self._run(SmokeExecutor(create_bin=True))
        self.assertEqual(code, 1)
        report = self._report()
        self.assertEqual(report["failure"]["reason"], "smoke_executable_link")
        statuses = {c["name"]: c["status"] for c in report["checks"]}
        self.assertEqual(statuses["no_executable_link"], "fail")


class TestExtractImageDigest(unittest.TestCase):
    def test_bare_digest_unchanged(self):
        self.assertEqual(extract_image_digest(_IMAGE), _IMAGE)

    def test_repository_qualified_digest(self):
        self.assertEqual(extract_image_digest(_REPO_IMAGE), _REPO_DIGEST)

    def test_tagged_repository_digest(self):
        self.assertEqual(extract_image_digest(_TAGGED_IMAGE), _TAGGED_DIGEST)


class TestReportSerialization(SmokeTestCase):
    def test_success_report_is_canonical_and_complete(self):
        executor = SmokeExecutor()
        code = self._run(executor)
        self.assertEqual(code, 0)
        report = self._report()

        self.assertEqual(report["schema_version"], SMOKE_REPORT_SCHEMA_VERSION)
        self.assertEqual(report["image_digest"], _IMAGE)
        self.assertEqual(report["node_version"], SMOKE_NODE_VERSION)
        self.assertEqual(report["npm_version"], SMOKE_NPM_VERSION)
        self.assertEqual(report["script_digest"], assembler_script_digest())
        self.assertEqual(report["policy_digest"], npm_policy_digest())
        self.assertEqual(report["fixture_digest"], smoke_fixture_digest())
        self.assertEqual(report["lockfile_digest"], smoke_fixture_digest())
        self.assertEqual(report["timestamp"], "2025-06-01T00:00:00+00:00")
        self.assertEqual(report["result"], "pass")
        self.assertIsNone(report["failure"])

        check_names = [c["name"] for c in report["checks"]]
        self.assertEqual(
            check_names,
            [
                "image_reference",
                "preflight",
                "assembler_identity",
                "assembly",
                "installed_closure",
                "no_lifecycle_script_effect",
                "no_executable_link",
            ],
        )
        self.assertTrue(all(c["status"] == "pass" for c in report["checks"]))

    def test_report_round_trips(self):
        code = self._run(SmokeExecutor())
        self.assertEqual(code, 0)
        text = _report_path(self.tmp).read_text()
        # Canonical serialization is deterministic and re-parses to the same
        # document.
        self.assertEqual(text, serialize_smoke_report(json.loads(text)))
        self.assertTrue(text.endswith("\n"))

    def test_repository_qualified_digest_report(self):
        code = self._run(SmokeExecutor(), image=_REPO_IMAGE)
        self.assertEqual(code, 0)
        report = self._report()
        self.assertEqual(report["result"], "pass")
        self.assertEqual(report["image_digest"], _REPO_DIGEST)
        self.assertNotIn("registry.example", report["image_digest"])
        self.assertNotIn("node", report["image_digest"])

    def test_tagged_digest_report(self):
        code = self._run(SmokeExecutor(), image=_TAGGED_IMAGE)
        self.assertEqual(code, 0)
        report = self._report()
        self.assertEqual(report["result"], "pass")
        self.assertEqual(report["image_digest"], _TAGGED_DIGEST)
        self.assertNotIn("registry.example", report["image_digest"])
        self.assertNotIn(":24", report["image_digest"])
        self.assertNotIn("node", report["image_digest"])

    def test_failed_report_records_failure_and_timestamp(self):
        code = self._run(SmokeExecutor(return_code=7))
        self.assertEqual(code, 1)
        report = self._report()
        self.assertEqual(report["result"], "fail")
        self.assertEqual(report["failure"]["reason"], "npm_exit_nonzero")
        self.assertEqual(report["timestamp"], "2025-06-01T00:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
