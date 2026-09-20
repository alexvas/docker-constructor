"""Phase 3 — assembler script and Docker execution boundary (tasks 3.2, 3.3).

The consumer-neutral assembler script asserts the container's actual Node and
npm versions before any npm execution, runs exactly
``npm ci --ignore-scripts --no-bin-links --no-audit --no-fund``, never enables
``engine-strict``, and never creates executable links from reviewed-root
``bin`` metadata.  Install scripts never run; nonzero exits stay structured;
and stdout/stderr redact sensitive command inputs.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR.parent))

from docker.npm_environment import (
    AssemblyRun,
    LockedNpmError,
    RootSpec,
    assembler_script_bytes,
    assembler_script_digest,
    assemble,
    compute_assembler_identity,
    npm_policy_digest,
    npm_policy_flags,
    preflight,
)

_IMAGE = "sha256:" + "a" * 64
_NODE = "24.18.0"
_NPM = "11.16.0"
_PLATFORM = "linux-x64"


def _sri() -> str:
    import base64

    return "sha512-" + base64.b64encode(bytes([0xAB]) * 64).decode()


def _url(name: str, version: str) -> str:
    stem = name.rsplit("/", 1)[-1]
    return f"https://registry.npmjs.org/{name}/-/{stem}-{version}.tgz"


def _lock(roots: dict[str, str], *, engines: dict[str, str] | None = None) -> bytes:
    pkg_nodes = {}
    for name, version in roots.items():
        node = {
            "version": version,
            "resolved": _url(name, version),
            "integrity": _sri(),
        }
        if engines:
            node["engines"] = {"node": engines[name]}
        pkg_nodes[f"node_modules/{name}"] = node
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
    node_version: str = _NODE,
    engines: dict[str, str] | None = None,
):
    raw = _lock(roots, engines=engines)
    validated = preflight(
        raw,
        roots=tuple(RootSpec(n, v) for n, v in sorted(roots.items())),
        platform=_PLATFORM,
        node_version=node_version,
        npm_version=_NPM,
    )
    return raw, validated


def _assembler(**overrides: str):
    kwargs = {
        "image_digest": _IMAGE,
        "node_version": _NODE,
        "npm_version": _NPM,
        "script_digest": assembler_script_digest(),
        "policy_digest": npm_policy_digest(),
        "platform": _PLATFORM,
    }
    kwargs.update(overrides)
    return compute_assembler_identity(**kwargs)


class FakeExecutor:
    """Records argv and returns canned results (or raises)."""

    def __init__(
        self,
        *,
        return_code: int = 0,
        stdout: str = "",
        stderr: str = "",
        raise_on_run: BaseException | None = None,
    ):
        self.return_code = return_code
        self.stdout = stdout
        self.stderr = stderr
        self.raise_on_run = raise_on_run
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: tuple[str, ...]):
        self.calls.append(argv)
        if self.raise_on_run is not None and argv[1] == "run":
            raise self.raise_on_run
        from docker.npm_environment import ProcessResult

        return ProcessResult(argv, self.return_code, self.stdout, self.stderr)


class TestFixedNpmPolicy(unittest.TestCase):
    def test_policy_flags_exact(self):
        self.assertEqual(
            npm_policy_flags(),
            (
                "--ignore-scripts",
                "--no-bin-links",
                "--no-audit",
                "--no-fund",
                "--loglevel=http",
            ),
        )

    def test_policy_digest_deterministic(self):
        self.assertEqual(npm_policy_digest(), npm_policy_digest())
        self.assertEqual(len(npm_policy_digest()), 64)

    def test_script_runs_npm_ci_with_exact_policy(self):
        script = assembler_script_bytes().decode("utf-8")
        self.assertIn(
            "npm ci --ignore-scripts --no-bin-links --no-audit --no-fund",
            script,
        )

    def test_script_asserts_versions_before_npm_ci(self):
        script = assembler_script_bytes().decode("utf-8")
        lines = script.splitlines()
        npm_ci_index = next(
            i for i, line in enumerate(lines) if line.strip().startswith("npm ci")
            or line.strip().startswith("exec npm ci")
        )
        # The version assertions must appear before the npm ci invocation.
        self.assertLess(npm_ci_index, len(lines))
        prefix = "\n".join(lines[:npm_ci_index])
        self.assertIn("node --version", prefix)
        self.assertIn("npm --version", prefix)

    def test_engine_strict_remains_disabled(self):
        script = assembler_script_bytes().decode("utf-8")
        self.assertNotIn("--engine-strict", script)
        self.assertNotIn("engine-strict", script)
        self.assertNotIn("engine_strict", script)

    def test_no_bin_link_creation(self):
        script = assembler_script_bytes().decode("utf-8")
        self.assertIn("--no-bin-links", script)
        self.assertNotIn("npm link", script)
        self.assertNotIn("npm bin", script)

    def test_install_scripts_never_run(self):
        script = assembler_script_bytes().decode("utf-8")
        self.assertIn("--ignore-scripts", script)
        for banned in ("npm run", "npm exec", "preinstall", "postinstall"):
            self.assertNotIn(banned, script)


class TestReviewedRootEngineEnforcement(unittest.TestCase):
    def test_multiple_roots_all_satisfied(self):
        _raw, validated = _validated(
            {"a": "1.0.0", "b": "1.0.0"},
            engines={"a": ">=24.0.0", "b": ">=22.0.0"},
        )
        self.assertEqual(len(validated.root_metadata), 2)

    def test_one_incompatible_root_identified(self):
        with self.assertRaises(LockedNpmError) as ctx:
            _validated(
                {"a": "1.0.0", "b": "1.0.0"},
                engines={"a": ">=24.0.0", "b": ">=99.0.0"},
            )
        self.assertEqual(ctx.exception.reason, "incompatible_node_engine")
        self.assertIn("b", ctx.exception.detail)
        self.assertIn("node_modules/b", ctx.exception.detail)

    def test_incompatible_transitive_range_is_non_fatal(self):
        # A transitive (non-reviewed) node declares an unsatisfied range; it
        # must be accepted-and-discarded with no custom diagnostic.
        raw = _lock({"a": "1.0.0"})
        root = json.loads(raw.decode())
        root["packages"]["node_modules/a"]["dependencies"] = {"dep": "1.0.0"}
        root["packages"]["node_modules/dep"] = {
            "version": "1.0.0",
            "resolved": _url("dep", "1.0.0"),
            "integrity": _sri(),
            "engines": {"node": ">=99.0.0"},
        }
        validated = preflight(
            json.dumps(root).encode(),
            roots=(RootSpec("a", "1.0.0"),),
            platform=_PLATFORM,
            node_version=_NODE,
            npm_version=_NPM,
        )
        # Accepted; no "dep" metadata survives in any DTO.
        self.assertFalse(any(
            m.package_name == "dep" for m in validated.root_metadata
        ))
        self.assertTrue(any(p.name == "dep" for p in validated.packages))


class TestStructuredFailures(unittest.TestCase):
    def _assemble(self, executor, *, secrets=(), validated=None):
        if validated is None:
            _raw, validated = _validated({"a": "1.0.0"})
        tmp = tempfile.TemporaryDirectory(prefix="npm-env-exec-")
        self.addCleanup(tmp.cleanup)
        cache_root = Path(tmp.name) / "cache"
        cache_root.mkdir()
        return assemble(
            validated=validated,
            assembler=_assembler(),
            cache_root=cache_root,
            executor=executor,
            secrets=secrets,
        )

    def test_success_returns_assembly_run(self):
        executor = FakeExecutor(stdout="installed\n", stderr="warn\n")
        result = self._assemble(executor)
        self.assertIsInstance(result, AssemblyRun)
        self.assertEqual(result.stdout, "installed\n")
        self.assertEqual(result.stderr, "warn\n")
        self.assertTrue(result.staging.exists())

    def test_nonzero_exit_is_structured(self):
        executor = FakeExecutor(return_code=1, stderr="npm ERR! something")
        with self.assertRaises(LockedNpmError) as ctx:
            self._assemble(executor)
        self.assertEqual(ctx.exception.reason, "npm_exit_nonzero")
        self.assertIn("1", ctx.exception.detail)

    def test_node_version_mismatch_is_structured(self):
        executor = FakeExecutor(return_code=65, stderr="node version mismatch")
        with self.assertRaises(LockedNpmError) as ctx:
            self._assemble(executor)
        self.assertEqual(ctx.exception.reason, "node_version_mismatch")

    def test_npm_version_mismatch_is_structured(self):
        executor = FakeExecutor(return_code=66, stderr="npm version mismatch")
        with self.assertRaises(LockedNpmError) as ctx:
            self._assemble(executor)
        self.assertEqual(ctx.exception.reason, "npm_version_mismatch")

    def test_stdout_stderr_redact_secrets(self):
        executor = FakeExecutor(
            return_code=1,
            stdout="registry uses token SUPERSECRET123\n",
            stderr="failed to reach https://proxy.example:3128 SUPERSECRET123\n",
        )
        with self.assertRaises(LockedNpmError) as ctx:
            self._assemble(executor, secrets=("SUPERSECRET123", "https://proxy.example:3128"))
        self.assertNotIn("SUPERSECRET123", ctx.exception.detail)
        self.assertNotIn("https://proxy.example:3128", ctx.exception.detail)
        self.assertIn("<redacted>", ctx.exception.detail)

    def test_rejects_before_effects(self):
        _raw, validated = _validated({"a": "1.0.0"})
        changed = dataclasses.replace(validated, node_version="20.0.0")
        executor = FakeExecutor()
        tmp = tempfile.TemporaryDirectory(prefix="npm-env-exec-")
        self.addCleanup(tmp.cleanup)
        cache_root = Path(tmp.name) / "cache"
        with self.assertRaises(LockedNpmError):
            assemble(
                validated=changed,
                assembler=_assembler(),
                cache_root=cache_root,
                executor=executor,
            )
        self.assertFalse(cache_root.exists())
        self.assertEqual(executor.calls, [])


if __name__ == "__main__":
    unittest.main()
