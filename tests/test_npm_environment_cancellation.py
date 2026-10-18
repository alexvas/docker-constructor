"""Phase 3 — cancellation and cleanup (task 3.4).

Container cleanup and staging cleanup must run for every ``BaseException``
path: interruption, arbitrary executor exceptions, and structured npm
failures.  Successful assembly keeps its staging for later validation.
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
    LockedNpmError,
    ProcessResult,
    RootSpec,
    assemble,
    assembler_script_digest,
    compute_assembler_identity,
    npm_policy_digest,
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


def _validated():
    raw = json.dumps(
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
                },
            },
        }
    ).encode()
    validated = preflight(
        raw,
        roots=(RootSpec("a", "1.0.0"),),
        platform=_PLATFORM,
        node_version=_NODE,
        npm_version=_NPM,
    )
    return validated


def _assembler():
    return compute_assembler_identity(
        image_digest=_IMAGE,
        node_version=_NODE,
        npm_version=_NPM,
        script_digest=assembler_script_digest(),
        policy_digest=npm_policy_digest(),
        platform=_PLATFORM,
    )


class RecordingExecutor:
    def __init__(self, behavior):
        self.behavior = behavior
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: tuple[str, ...]):
        self.calls.append(argv)
        return self.behavior(argv)


def _interrupt_on_run(argv):
    if argv[1] == "run":
        raise KeyboardInterrupt()
    return ProcessResult(argv, 0, "", "")


def _value_error_on_run(argv):
    if argv[1] == "run":
        raise ValueError("executor exploded")
    return ProcessResult(argv, 0, "", "")


def _system_exit_on_run(argv):
    if argv[1] == "run":
        raise SystemExit("cancelled")
    return ProcessResult(argv, 0, "", "")


class TestCancellationAndCleanup(unittest.TestCase):
    def _cache_root(self):
        tmp = tempfile.TemporaryDirectory(prefix="npm-env-cancel-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "cache"
        root.mkdir()
        return root

    def _staging_workspaces(self, cache_root: Path) -> list[Path]:
        staging_parent = (
            cache_root
            / "npm-environments"
            / "assembler"
            / _assembler().digest
            / "staging"
        )
        if not staging_parent.exists():
            return []
        return [p for p in staging_parent.iterdir() if p.is_dir()]

    def test_keyboard_interrupt_cleans_container_and_staging(self):
        executor = RecordingExecutor(_interrupt_on_run)
        with self.assertRaises(KeyboardInterrupt):
            assemble(
                validated=_validated(),
                assembler=_assembler(),
                cache_root=self._cache_root(),
                executor=executor,
            )
        self.assertTrue(any(c[1] == "rm" and c[2] == "-f" for c in executor.calls))
        # The cleanup argv targets the same container name docker run used.
        run_call = next(c for c in executor.calls if c[1] == "run")
        name = run_call[run_call.index("--name") + 1]
        self.assertTrue(
            any(
                c[:3] == ("docker", "rm", "-f") and c[3] == name for c in executor.calls
            )
        )

    def test_executor_exception_cleans_staging(self):
        executor = RecordingExecutor(_value_error_on_run)
        cache_root = self._cache_root()
        with self.assertRaises(LockedNpmError) as ctx:
            assemble(
                validated=_validated(),
                assembler=_assembler(),
                cache_root=cache_root,
                executor=executor,
            )
        self.assertEqual(ctx.exception.reason, "executor_failure")
        self.assertEqual(self._staging_workspaces(cache_root), [])

    def test_nonzero_exit_cleans_staging(self):
        def nonzero(argv):
            return ProcessResult(argv, 1, "", "boom")

        executor = RecordingExecutor(nonzero)
        cache_root = self._cache_root()
        with self.assertRaises(LockedNpmError):
            assemble(
                validated=_validated(),
                assembler=_assembler(),
                cache_root=cache_root,
                executor=executor,
            )
        self.assertEqual(self._staging_workspaces(cache_root), [])

    def test_success_keeps_staging(self):
        executor = RecordingExecutor(lambda argv: ProcessResult(argv, 0, "", ""))
        cache_root = self._cache_root()
        result = assemble(
            validated=_validated(),
            assembler=_assembler(),
            cache_root=cache_root,
            executor=executor,
        )
        self.assertTrue(result.staging.exists())
        self.assertIn(result.staging, self._staging_workspaces(cache_root))

    def test_system_exit_cleans_container_and_staging(self):
        executor = RecordingExecutor(_system_exit_on_run)
        cache_root = self._cache_root()
        with self.assertRaises(SystemExit) as ctx:
            assemble(
                validated=_validated(),
                assembler=_assembler(),
                cache_root=cache_root,
                executor=executor,
            )
        self.assertEqual(str(ctx.exception), "cancelled")
        self.assertTrue(any(c[1] == "rm" and c[2] == "-f" for c in executor.calls))
        self.assertEqual(self._staging_workspaces(cache_root), [])

    def test_keyboard_interrupt_cleans_staging(self):
        executor = RecordingExecutor(_interrupt_on_run)
        cache_root = self._cache_root()
        with self.assertRaises(KeyboardInterrupt):
            assemble(
                validated=_validated(),
                assembler=_assembler(),
                cache_root=cache_root,
                executor=executor,
            )
        self.assertEqual(self._staging_workspaces(cache_root), [])


if __name__ == "__main__":
    unittest.main()
