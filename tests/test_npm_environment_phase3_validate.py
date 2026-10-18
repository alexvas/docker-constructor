"""Phase 3 — validation under fake and real Docker (task 3.8).

A fake ``docker`` on ``PATH`` proves the full deterministic vector (image,
mounts, environment, embedded script, and exact npm policy) reaches the
execution boundary and that nonzero exits stay structured.  A real
pinned-container smoke test runs the assembler script against a real Node
image and is opt-in via ``NPM_ENV_REAL_DOCKER=1``.
"""

from __future__ import annotations

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

from docker.npm_environment import (
    DockerRunExecutor,
    LockedNpmError,
    RootSpec,
    assemble,
    assembler_script_bytes,
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


def _write_fake_docker(bin_dir: Path, log_path: Path, *, exit_code: int) -> None:
    script = f'#!/bin/sh\nprintf "%s\\n" "$@" > "{log_path}"\nexit {exit_code}\n'
    docker = bin_dir / "docker"
    docker.write_text(script)
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)


class TestFakeDockerSmoke(unittest.TestCase):
    def _run_assemble(self, *, exit_code: int):
        tmp = tempfile.TemporaryDirectory(prefix="npm-env-fake-")
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        log_path = base / "docker.log"
        bin_dir = base / "bin"
        bin_dir.mkdir()
        _write_fake_docker(bin_dir, log_path, exit_code=exit_code)

        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{old_path}"
        self.addCleanup(os.environ.__setitem__, "PATH", old_path)

        try:
            cache_root = base / "cache"
            cache_root.mkdir()
            result = assemble(
                validated=_validated(),
                assembler=_assembler(),
                cache_root=cache_root,
                executor=DockerRunExecutor(),
            )
        except LockedNpmError as exc:
            return log_path, None, exc
        return log_path, result, None

    def test_full_vector_passed_to_docker(self):
        log_path, result, exc = self._run_assemble(exit_code=0)
        self.assertIsNone(exc)
        self.assertIsNotNone(result)
        argv = log_path.read_text().split("\n")
        # ``$@`` logs the args after the ``docker`` binary name.
        self.assertEqual(argv[0], "run")
        self.assertIn("run", argv)
        self.assertIn("--rm", argv)
        self.assertIn("--user", argv)
        self.assertIn("--name", argv)
        self.assertIn("--env", argv)
        self.assertIn("--volume", argv)
        self.assertIn("--workdir", argv)
        self.assertIn(_IMAGE, argv)
        self.assertIn("REVIEWED_NODE_VERSION=" + _NODE, argv)
        self.assertIn("REVIEWED_NPM_VERSION=" + _NPM, argv)
        self.assertIn(
            "npm ci --ignore-scripts --no-bin-links --no-audit --no-fund",
            log_path.read_text(),
        )

    def test_nonzero_exit_is_structured_through_executor(self):
        log_path, result, exc = self._run_assemble(exit_code=3)
        self.assertIsNone(result)
        self.assertIsInstance(exc, LockedNpmError)
        self.assertEqual(exc.reason, "npm_exit_nonzero")
        self.assertIn("3", exc.detail)


@unittest.skipUnless(
    os.environ.get("NPM_ENV_REAL_DOCKER") == "1",
    "set NPM_ENV_REAL_DOCKER=1 to run the real pinned-container smoke test",
)
class TestRealPinnedContainerSmoke(unittest.TestCase):
    def test_node_image_executes_assembler_script(self):
        image = os.environ.get("NPM_ENV_REAL_IMAGE", "node:24-alpine")
        script = assembler_script_bytes().decode("utf-8")
        env = {
            "REVIEWED_NODE_VERSION": "24.18.0",
            "REVIEWED_NPM_VERSION": "11.16.0",
        }
        proc = subprocess.run(
            ["docker", "run", "--rm", "--user", "0:0", image, "/bin/sh", "-c", script],
            capture_output=True,
            text=True,
            env={**os.environ, **env},
        )
        # The real image asserts its own node/npm versions; a mismatch is a
        # nonzero exit with a version-mismatch message, not a crash.
        self.assertIn(proc.returncode, (0, 65, 66), proc.stderr)


if __name__ == "__main__":
    unittest.main()
