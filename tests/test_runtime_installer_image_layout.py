"""Phase 2 task 2.6 — isolated image-layout import contract.

The runtime image does not ship the source repository.  It copies only the
repository Python files named by ``Dockerfile`` instructions into
``/usr/local/lib/pi-cli``.  This test reconstructs that exact layout from the
Dockerfile COPY instructions, runs a subprocess outside the repository with
``PYTHONPATH`` scrubbed, and imports ``docker.runtime_installer`` plus its
lightweight ``docker.filesystem`` dependencies.

Before the ``docker/filesystem/`` copy exists the staged layout cannot resolve
those imports, so the test fails; once the copy is present it passes without
adding the repository to the subprocess import path.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DOCKERFILE = (REPO / "Dockerfile").read_text(encoding="utf-8")
_INSTALL_ROOT = "/usr/local/lib/pi-cli"

_IMPORT_PROGRAM = textwrap.dedent(
    """
    import sys

    sys.path.insert(0, {stage!r})
    import docker.runtime_installer
    import docker.filesystem.cleanup
    import docker.filesystem.descriptors
    import docker.filesystem.operations
    print("IMPORT_OK")
    """
)


def _copy_instructions(text: str) -> list[str]:
    """Return logical ``COPY`` instructions, joining continuations."""
    result: list[str] = []
    current = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not current and (not line or line.startswith("#")):
            continue
        current = f"{current} {line}".strip()
        if current.endswith("\\"):
            current = current[:-1].rstrip()
            continue
        if current.split(maxsplit=1)[0].upper() == "COPY":
            result.append(current)
        current = ""
    return result


def _pi_cli_sources(text: str) -> list[tuple[str, str]]:
    """Return ``(source, destination)`` COPY local operands into the root."""
    sources: list[tuple[str, str]] = []
    for instruction in _copy_instructions(text):
        body = instruction[len("COPY"):].strip()
        operands = shlex.split(body)
        while operands and operands[0].startswith("--"):
            if operands[0] == "--from" or operands[0].startswith("--from="):
                operands = []
                break
            operands.pop(0)
        if len(operands) < 2:
            continue
        source, destination = operands[0], operands[1]
        if destination == _INSTALL_ROOT or destination.startswith(
            _INSTALL_ROOT + "/"
        ):
            sources.append((source, destination))
    return sources


class TestIsolatedImageLayoutImport(unittest.TestCase):
    """Task 2.6 — the image layout resolves runtime lifecycle imports."""

    def _stage_image_layout(self, tmp: str) -> str:
        stage = os.path.join(tmp, "stage")
        for source, destination in _pi_cli_sources(DOCKERFILE):
            rel = destination[len(_INSTALL_ROOT):].lstrip("/")
            target = os.path.join(stage, rel)
            src = REPO / source
            if not src.exists():
                self.fail(f"Dockerfile COPY source is missing: {source}")
            if src.is_dir():
                shutil.copytree(
                    src,
                    target,
                    ignore=shutil.ignore_patterns("__pycache__"),
                )
            else:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.copy2(src, target)
        return stage

    def test_runtime_installer_imports_from_image_layout(self) -> None:
        tmp = tempfile.mkdtemp(prefix="image-layout-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        stage = self._stage_image_layout(tmp)
        run_dir = os.path.join(tmp, "run")
        os.makedirs(run_dir, exist_ok=True)
        env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
        result = subprocess.run(
            [sys.executable, "-c", _IMPORT_PROGRAM.format(stage=stage)],
            cwd=run_dir,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            0,
            result.returncode,
            f"isolated image import failed:\n{result.stderr}",
        )
        self.assertIn("IMPORT_OK", result.stdout)
