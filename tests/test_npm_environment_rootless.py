"""Phase 3 — rootless Docker container-identity resolution.

Rootless Docker maps the invoking host user to container UID/GID ``0``, so
the assembler must run the container as ``--user 0:0`` in that mode to keep
the owner-private host bind mounts (staging and npm cache) writable.  On a
rootful daemon the container keeps running as the invoking host UID/GID.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR.parent))

import docker.npm_environment.execution as execution_module
from docker.npm_environment import (
    DockerRunExecutor,
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


class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _EmptyPipe:
    """Byte pipe stub that reports immediate EOF for streaming readers."""

    def read(self, n: int) -> bytes:
        return b""

    def close(self) -> None:
        pass


class _FakePopenProc:
    """Minimal ``subprocess.Popen`` stub for the streaming executor."""

    def __init__(self, returncode=0):
        self.returncode = returncode
        self.stdout = _EmptyPipe()
        self.stderr = _EmptyPipe()

    def wait(self, timeout: float | None = None) -> int:
        # Matches ``subprocess.Popen.wait(timeout=...)``: the process has
        # already exited, so the optional timeout is accepted and ignored.
        return self.returncode


class TestIsRootlessDocker(unittest.TestCase):
    def test_rootless_stdout_is_detected(self):
        with mock.patch.object(
            execution_module.subprocess,
            "run",
            return_value=_FakeProc(stdout="...\n  rootless\n..."),
        ) as run:
            self.assertTrue(execution_module._is_rootless_docker())
        self.assertEqual(run.call_args[0][0], ["docker", "info"])

    def test_rootful_stdout_is_not_rootless(self):
        with mock.patch.object(
            execution_module.subprocess, "run", return_value=_FakeProc()
        ):
            self.assertFalse(execution_module._is_rootless_docker())

    def test_nonzero_docker_info_is_not_rootless(self):
        with mock.patch.object(
            execution_module.subprocess,
            "run",
            return_value=_FakeProc(returncode=1, stderr="cannot connect"),
        ):
            self.assertFalse(execution_module._is_rootless_docker())

    def test_missing_docker_binary_is_not_rootless(self):
        with mock.patch.object(
            execution_module.subprocess, "run", side_effect=OSError("no docker")
        ):
            self.assertFalse(execution_module._is_rootless_docker())


class TestDockerRunExecutorResolveUser(unittest.TestCase):
    def test_explicit_uid_gid_win(self):
        executor = DockerRunExecutor()
        with mock.patch.object(execution_module.subprocess, "run") as run:
            self.assertEqual(executor.resolve_user(123, 456), (123, 456))
        run.assert_not_called()

    def test_rootless_resolves_to_zero(self):
        executor = DockerRunExecutor()
        with mock.patch.object(
            execution_module, "_is_rootless_docker", return_value=True
        ):
            self.assertEqual(executor.resolve_user(None, None), (0, 0))

    def test_rootful_resolves_to_host_identity(self):
        executor = DockerRunExecutor()
        with mock.patch.object(
            execution_module, "_is_rootless_docker", return_value=False
        ):
            self.assertEqual(
                executor.resolve_user(None, None),
                (os.getuid(), os.getgid()),
            )


class _ResolvingExecutor:
    """Fake executor exposing the optional ``resolve_user`` hook."""

    def __init__(self, resolved: tuple[int, int]):
        self.resolved = resolved
        self.calls: list[tuple[str, ...]] = []

    def resolve_user(self, uid, gid):
        return self.resolved

    def run(self, argv: tuple[str, ...]):
        self.calls.append(argv)
        return ProcessResult(argv, 0, "", "")


class _PlainExecutor:
    """Fake executor without the optional ``resolve_user`` hook."""

    def __init__(self):
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: tuple[str, ...]):
        self.calls.append(argv)
        return ProcessResult(argv, 0, "", "")


def _run_user(executor) -> tuple[str, str]:
    """Run assemble and return the ``--user`` uid/gid from the run argv."""
    tmp = tempfile.TemporaryDirectory(prefix="npm-env-rootless-")
    try:
        cache_root = Path(tmp.name) / "cache"
        cache_root.mkdir()
        result = assemble(
            validated=_validated(),
            assembler=_assembler(),
            cache_root=cache_root,
            executor=executor,
        )
    finally:
        tmp.cleanup()
    argv = result.argv
    return argv[argv.index("--user") + 1]


class TestAssembleUsesExecutorResolution(unittest.TestCase):
    def test_executor_resolution_is_used(self):
        executor = _ResolvingExecutor((0, 0))
        self.assertEqual(_run_user(executor), "0:0")

    def test_no_hook_falls_back_to_host_identity(self):
        executor = _PlainExecutor()
        self.assertEqual(_run_user(executor), f"{os.getuid()}:{os.getgid()}")
        # The fallback never probes Docker, so only the run argv is issued.
        self.assertEqual(len(executor.calls), 1)


class TestRealExecutorEndToEnd(unittest.TestCase):
    def test_rootless_run_vector_uses_zero(self):
        def fake_run(argv, **kwargs):
            if argv[0] == "docker" and argv[1] == "info":
                return _FakeProc(stdout="...\n  rootless\n...")
            raise AssertionError(f"unexpected argv: {argv}")

        def fake_popen(argv, **kwargs):
            if argv[0] == "docker" and argv[1] == "run":
                return _FakePopenProc()
            raise AssertionError(f"unexpected argv: {argv}")

        tmp = tempfile.TemporaryDirectory(prefix="npm-env-rootless-e2e-")
        self.addCleanup(tmp.cleanup)
        cache_root = Path(tmp.name) / "cache"
        cache_root.mkdir()
        with (
            mock.patch.object(execution_module.subprocess, "run", side_effect=fake_run),
            mock.patch.object(
                execution_module.subprocess, "Popen", side_effect=fake_popen
            ),
        ):
            result = assemble(
                validated=_validated(),
                assembler=_assembler(),
                cache_root=cache_root,
                executor=DockerRunExecutor(),
            )
        argv = result.argv
        self.assertEqual(argv[argv.index("--user") + 1], "0:0")


if __name__ == "__main__":
    unittest.main()
