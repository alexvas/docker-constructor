"""Regression tests for maintenance-script cache-root preparation."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).parents[1]


def _write_probe(directory: Path, name: str, marker: Path) -> None:
    path = directory / name
    path.write_text(f"#!/bin/sh\nprintf '%s\\n' {name} >> {marker}\nexit 97\n")
    path.chmod(0o755)


def _snapshot(root: Path) -> dict[str, tuple[str, bytes | str]]:
    """Capture project-owned inputs without following symlinks."""
    result: dict[str, tuple[str, bytes | str]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[relative] = ("symlink", os.readlink(path))
        elif path.is_dir():
            result[relative] = ("directory", "")
        else:
            result[relative] = ("file", path.read_bytes())
    return result


class TestMaintenanceCachePreparation(unittest.TestCase):
    def _environment(self, base: Path, marker: Path) -> dict[str, str]:
        probes = base / "probes"
        probes.mkdir()
        for name in ("find", "rm", "docker"):
            _write_probe(probes, name, marker)
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(base / "home"),
                "XDG_CACHE_HOME": str(base / "xdg"),
                "PATH": str(probes) + os.pathsep + env["PATH"],
                "PYTHONPATH": str(_ROOT),
            }
        )
        (base / "home").mkdir()
        (base / "xdg").mkdir()
        return env

    def _project_directory(self, base: Path, cache_dir: Path) -> Path:
        project = base / "project"
        project.mkdir()
        (project / "docker-constructor.toml").write_text("schema = 1\n")
        (project / "docker-constructor.local.toml").write_text(
            f"[cache]\ndir = {str(cache_dir)!r}\n"
        )
        return project

    def test_evidence_rejects_symlinked_local_root_before_find(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            marker = base / "operations"
            outside = base / "outside"
            outside.mkdir()
            sentinel = outside / "sentinel"
            sentinel.write_bytes(b"unchanged")
            selected = base / "selected-cache"
            selected.symlink_to(outside, target_is_directory=True)
            project = self._project_directory(base, selected)
            result = subprocess.run(
                [
                    str(_ROOT / "docker/collect-runtime-artifact-evidence.sh"),
                    "--project-directory",
                    str(project),
                    "--output-dir",
                    str(base / "evidence"),
                ],
                env=self._environment(base, marker),
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("symlink", result.stderr.lower())
            self.assertFalse(
                marker.exists(), marker.read_text() if marker.exists() else ""
            )
            self.assertEqual(sentinel.read_bytes(), b"unchanged")

    def test_acceptance_rejects_unsafe_local_root_before_rm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            marker = base / "operations"
            env = self._environment(base, marker)
            xdg = Path(env["XDG_CACHE_HOME"])
            sentinel = xdg / "sentinel"
            sentinel.write_bytes(b"unchanged")
            project = self._project_directory(base, xdg)
            result = subprocess.run(
                [
                    str(_ROOT / "docker/collect-runtime-artifact-acceptance.sh"),
                    "--project-directory",
                    str(project),
                    "--output-dir",
                    str(base / "evidence"),
                    "--fresh-cache",
                ],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("dedicated", result.stderr.lower())
            self.assertFalse(
                marker.exists(), marker.read_text() if marker.exists() else ""
            )
            self.assertEqual(sentinel.read_bytes(), b"unchanged")

    def test_evidence_default_output_and_pi_home_use_external_project_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            marker = base / "operations"
            env = self._environment(base, marker)
            project = self._project_directory(base, base / "cache")
            result = subprocess.run(
                [
                    str(_ROOT / "docker/collect-runtime-artifact-evidence.sh"),
                    "--project-directory",
                    str(project),
                ],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(97, result.returncode)
            state_root = base / "cache" / "projects"
            evidence = list(state_root.glob("*/evidence/runtime-artifacts-*"))
            self.assertEqual(1, len(evidence))
            self.assertTrue((evidence[0] / "home/.pi").is_dir())
            self.assertFalse((Path(env["HOME"]) / ".pi").exists())
            self.assertFalse((project / ".docker-generated").exists())

    def test_evidence_explicit_output_and_pi_home_are_caller_directed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            marker = base / "operations"
            env = self._environment(base, marker)
            project = self._project_directory(base, base / "cache")
            (project / "Dockerfile").write_text("FROM scratch\n")
            (project / ".env").write_text("WORKSPACE_ROOT=/workspace\n")
            (project / ".docker-local").mkdir()
            (project / ".docker-local" / "sentinel").write_text("unchanged\n")
            before = _snapshot(project)
            output = Path(env["XDG_CACHE_HOME"]) / "evidence"
            pi_home = Path(env["HOME"]) / ".pi"
            result = subprocess.run(
                [
                    str(
                        _ROOT / "docker/collect-runtime-artifact-evidence.sh",
                    ),
                    "--project-directory",
                    str(project),
                    "--output-dir",
                    str(output),
                    "--pi-home",
                    str(pi_home),
                ],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(97, result.returncode)
            self.assertTrue(output.is_dir())
            self.assertTrue(pi_home.is_dir())
            self.assertEqual(before, _snapshot(project))
            self.assertFalse((base / "cache" / "projects").exists())
            self.assertFalse((project / ".docker-generated").exists())

    def test_acceptance_default_output_uses_external_evidence_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            marker = base / "operations"
            env = self._environment(base, marker)
            project = self._project_directory(base, base / "cache")
            before = _snapshot(project)
            result = subprocess.run(
                [
                    str(_ROOT / "docker/collect-runtime-artifact-acceptance.sh"),
                    "--project-directory",
                    str(project),
                ],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(97, result.returncode)
            evidence = list(
                (base / "cache" / "projects").glob(
                    "*/evidence/runtime-artifacts-acceptance-*"
                )
            )
            self.assertEqual(1, len(evidence))
            self.assertEqual(before, _snapshot(project))
            self.assertFalse((project / ".docker-generated").exists())


if __name__ == "__main__":
    unittest.main()
