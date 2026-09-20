"""Phase 3 — cleanup failure chaining (items 4–6).

Cleanup failures never replace the primary assembly exception: the original
failure stays identifiable and every container/staging cleanup failure is
attached as a structured note.  Prior committed environments are untouched;
only the failing staging workspace is ever removed.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR.parent))

import docker.npm_environment.execution as execution_module
from docker.npm_environment import (
    LockedNpmError,
    ProcessResult,
    RootSpec,
    assemble,
    assembler_script_digest,
    compute_assembler_identity,
    npm_policy_digest,
    preflight,
)
from docker.versioning.npm_diagnostic_stream import project_tail

_URL = "https://user:pass@registry.example.com/pkg?token=abc#frag"

_IMAGE = "sha256:" + "a" * 64
_NODE = "24.18.0"
_NPM = "11.16.0"
_PLATFORM = "linux-x64"


def _sri() -> str:
    import base64

    return "sha512-" + base64.b64encode(bytes([0xAB]) * 64).decode()


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
                    "resolved": "https://registry.npmjs.org/a/-/a-1.0.0.tgz",
                    "integrity": _sri(),
                },
            },
        }
    ).encode()
    return preflight(
        raw,
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


class ScriptedExecutor:
    """Returns/raises per-phase (``run`` vs ``rm``) outcomes."""

    def __init__(
        self,
        *,
        run_result: ProcessResult | None = None,
        run_exc: BaseException | None = None,
        rm_result: ProcessResult | None = None,
        rm_exc: BaseException | None = None,
    ):
        self.run_result = run_result
        self.run_exc = run_exc
        self.rm_result = rm_result
        self.rm_exc = rm_exc
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: tuple[str, ...]):
        self.calls.append(argv)
        if argv[1] == "run":
            if self.run_exc is not None:
                raise self.run_exc
            if self.run_result is not None:
                return self.run_result
            return ProcessResult(argv, 0, "", "")
        if argv[1] == "rm":
            if self.rm_exc is not None:
                raise self.rm_exc
            if self.rm_result is not None:
                return self.rm_result
            return ProcessResult(argv, 0, "", "")
        raise AssertionError(f"unexpected argv: {argv}")


class CleanupTestCase(unittest.TestCase):
    def _cache_root(self):
        tmp = tempfile.TemporaryDirectory(prefix="npm-env-cleanup-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "cache"
        root.mkdir()
        return root

    def _staging_workspaces(self, cache_root: Path) -> list[Path]:
        staging_parent = (
            cache_root / "npm-environments" / "assembler" / _assembler().digest
            / "staging"
        )
        if not staging_parent.exists():
            return []
        return [p for p in staging_parent.iterdir() if p.is_dir()]

    def _assemble(
        self, executor, cache_root: Path, *, validated=None, tail_projector=None
    ):
        if validated is None:
            validated = _validated()
        return assemble(
            validated=validated,
            assembler=_assembler(),
            cache_root=cache_root,
            executor=executor,
            secrets=("SUPERSECRET",),
            tail_projector=tail_projector,
        )

    def _run_failure(self) -> ProcessResult:
        return ProcessResult(("docker", "run"), 1, "", "npm ERR! boom")


class TestCleanupCombinations(CleanupTestCase):
    def test_assembly_failure_with_successful_cleanup(self):
        executor = ScriptedExecutor(
            run_result=self._run_failure(),
            rm_result=ProcessResult(("docker", "rm"), 0, "", ""),
        )
        cache_root = self._cache_root()
        with self.assertRaises(LockedNpmError) as ctx:
            self._assemble(executor, cache_root)
        self.assertEqual(ctx.exception.reason, "npm_exit_nonzero")
        self.assertEqual(getattr(ctx.exception, "__notes__", []), [])
        self.assertEqual(self._staging_workspaces(cache_root), [])
        # Container cleanup was attempted once.
        self.assertTrue(any(c[1] == "rm" for c in executor.calls))

    def test_assembly_failure_plus_container_cleanup_failure(self):
        executor = ScriptedExecutor(
            run_result=self._run_failure(),
            rm_exc=OSError("docker rm failed"),
        )
        cache_root = self._cache_root()
        with self.assertRaises(LockedNpmError) as ctx:
            self._assemble(executor, cache_root)
        # Original failure remains identifiable.
        self.assertEqual(ctx.exception.reason, "npm_exit_nonzero")
        # Container cleanup failure is observable.
        notes = ctx.exception.__notes__
        self.assertEqual(len(notes), 1)
        self.assertIn("container", notes[0])
        self.assertIn("docker rm failed", notes[0])
        self.assertEqual(self._staging_workspaces(cache_root), [])

    def test_assembly_failure_plus_staging_cleanup_failure(self):
        executor = ScriptedExecutor(
            run_result=self._run_failure(),
            rm_result=ProcessResult(("docker", "rm"), 0, "", ""),
        )
        cache_root = self._cache_root()
        with mock.patch.object(
            execution_module,
            "remove_staging_workspace",
            side_effect=LockedNpmError("unsafe_staging_path", "cannot remove: boom"),
        ):
            with self.assertRaises(LockedNpmError) as ctx:
                self._assemble(executor, cache_root)
        self.assertEqual(ctx.exception.reason, "npm_exit_nonzero")
        notes = ctx.exception.__notes__
        self.assertEqual(len(notes), 1)
        self.assertIn("staging", notes[0])
        self.assertIn("residue may remain at", notes[0])
        self.assertIn("unsafe_staging_path", notes[0])

    def test_both_cleanup_operations_fail(self):
        executor = ScriptedExecutor(
            run_result=self._run_failure(),
            rm_exc=OSError("docker rm failed"),
        )
        cache_root = self._cache_root()
        with mock.patch.object(
            execution_module,
            "remove_staging_workspace",
            side_effect=LockedNpmError("unsafe_staging_path", "cannot remove: boom"),
        ):
            with self.assertRaises(LockedNpmError) as ctx:
                self._assemble(executor, cache_root)
        self.assertEqual(ctx.exception.reason, "npm_exit_nonzero")
        notes = ctx.exception.__notes__
        self.assertEqual(len(notes), 2)
        self.assertIn("container", notes[0])
        self.assertIn("staging", notes[1])

    def test_keyboard_interrupt_during_execution_and_cleanup(self):
        executor = ScriptedExecutor(
            run_exc=KeyboardInterrupt("interrupted run"),
            rm_exc=KeyboardInterrupt("interrupted cleanup"),
        )
        cache_root = self._cache_root()
        with self.assertRaises(KeyboardInterrupt) as ctx:
            self._assemble(executor, cache_root)
        # The original interruption stays primary.
        self.assertIsInstance(ctx.exception, KeyboardInterrupt)
        # Cancellation during cleanup is observable, not treated as success.
        notes = ctx.exception.__notes__
        self.assertEqual(len(notes), 1)
        self.assertIn("container", notes[0])
        self.assertIn("interrupted cleanup", notes[0])

    def test_arbitrary_executor_exception_keeps_primary_and_notes(self):
        executor = ScriptedExecutor(
            run_exc=ValueError("executor exploded"),
            rm_exc=OSError("docker rm failed"),
        )
        cache_root = self._cache_root()
        with self.assertRaises(LockedNpmError) as ctx:
            self._assemble(executor, cache_root)
        self.assertEqual(ctx.exception.reason, "executor_failure")
        self.assertIn("executor exploded", str(ctx.exception))
        notes = ctx.exception.__notes__
        self.assertEqual(len(notes), 1)
        self.assertIn("container", notes[0])
        self.assertIn("docker rm failed", notes[0])


class TestNoCleanupAttemptedBeforeContainer(CleanupTestCase):
    def test_preflight_mismatch_skips_container_cleanup(self):
        # A recheck failure before ``docker run`` must not attempt a bogus
        # container removal or leave staging behind.
        import dataclasses

        changed = dataclasses.replace(_validated(), node_version="20.0.0")
        executor = ScriptedExecutor()
        cache_root = self._cache_root()
        with self.assertRaises(LockedNpmError) as ctx:
            self._assemble(executor, cache_root, validated=changed)
        self.assertEqual(ctx.exception.reason, "node_version_mismatch")
        self.assertFalse(any(c[1] == "rm" for c in executor.calls))
        self.assertEqual(self._staging_workspaces(cache_root), [])


class TestCleanupDetailProjection(CleanupTestCase):
    """URL-bearing cleanup output must not leak through attached notes."""

    def _assert_url_free(self, note: str) -> None:
        for fragment in (
            _URL,
            "https://",
            "registry.example.com",
            "user:pass",
            "token=abc",
            "#frag",
            "SUPERSECRET",
        ):
            self.assertNotIn(fragment, note)
        # The sanitized replacement must still be observable in the note.
        self.assertIn("<redacted>", note)

    def test_container_cleanup_exception_note_is_url_free(self):
        executor = ScriptedExecutor(
            run_result=self._run_failure(),
            rm_exc=OSError(f"docker rm failed fetching {_URL} (secret SUPERSECRET)"),
        )
        cache_root = self._cache_root()
        with self.assertRaises(LockedNpmError) as ctx:
            self._assemble(executor, cache_root, tail_projector=project_tail)
        # The original failure stays primary.
        self.assertEqual(ctx.exception.reason, "npm_exit_nonzero")
        notes = ctx.exception.__notes__
        self.assertEqual(len(notes), 1)
        self.assertIn("container", notes[0])
        self.assertIn("docker_rm_exception", notes[0])
        self._assert_url_free(notes[0])

    def test_container_cleanup_stderr_note_is_url_free(self):
        executor = ScriptedExecutor(
            run_result=self._run_failure(),
            rm_result=ProcessResult(
                ("docker", "rm"),
                1,
                "",
                f"failed to remove {_URL} (secret SUPERSECRET)",
            ),
        )
        cache_root = self._cache_root()
        with self.assertRaises(LockedNpmError) as ctx:
            self._assemble(executor, cache_root, tail_projector=project_tail)
        self.assertEqual(ctx.exception.reason, "npm_exit_nonzero")
        notes = ctx.exception.__notes__
        self.assertEqual(len(notes), 1)
        self.assertIn("container", notes[0])
        self.assertIn("docker_rm_nonzero", notes[0])
        self._assert_url_free(notes[0])

    def test_staging_cleanup_exception_note_is_url_free(self):
        executor = ScriptedExecutor(
            run_result=self._run_failure(),
            rm_result=ProcessResult(("docker", "rm"), 0, "", ""),
        )
        cache_root = self._cache_root()
        with mock.patch.object(
            execution_module,
            "remove_staging_workspace",
            side_effect=LockedNpmError(
                "unsafe_staging_path",
                f"cannot remove {_URL} (secret SUPERSECRET)",
            ),
        ):
            with self.assertRaises(LockedNpmError) as ctx:
                self._assemble(executor, cache_root, tail_projector=project_tail)
        self.assertEqual(ctx.exception.reason, "npm_exit_nonzero")
        notes = ctx.exception.__notes__
        self.assertEqual(len(notes), 1)
        # The note still identifies the operation and the residue risk.
        self.assertIn("staging", notes[0])
        self.assertIn("unsafe_staging_path", notes[0])
        self.assertIn("residue may remain at", notes[0])
        self._assert_url_free(notes[0])

    def test_staging_cleanup_arbitrary_exception_note_is_url_free(self):
        executor = ScriptedExecutor(
            run_result=self._run_failure(),
            rm_result=ProcessResult(("docker", "rm"), 0, "", ""),
        )
        cache_root = self._cache_root()
        with mock.patch.object(
            execution_module,
            "remove_staging_workspace",
            side_effect=RuntimeError(f"rm failed {_URL} SUPERSECRET"),
        ):
            with self.assertRaises(LockedNpmError) as ctx:
                self._assemble(executor, cache_root, tail_projector=project_tail)
        self.assertEqual(ctx.exception.reason, "npm_exit_nonzero")
        notes = ctx.exception.__notes__
        self.assertEqual(len(notes), 1)
        self.assertIn("staging", notes[0])
        self.assertIn("RuntimeError", notes[0])
        self.assertIn("residue may remain at", notes[0])
        self._assert_url_free(notes[0])

    def test_fallback_projector_keeps_secret_redaction(self):
        # With no injected projector the existing secret-redacted behavior is
        # preserved; host callers always inject the URL-free projector.
        executor = ScriptedExecutor(
            run_result=self._run_failure(),
            rm_exc=OSError("docker rm failed with SUPERSECRET"),
        )
        cache_root = self._cache_root()
        with self.assertRaises(LockedNpmError) as ctx:
            self._assemble(executor, cache_root)
        notes = ctx.exception.__notes__
        self.assertEqual(len(notes), 1)
        self.assertNotIn("SUPERSECRET", notes[0])
        self.assertIn("docker rm failed", notes[0])


if __name__ == "__main__":
    unittest.main()
