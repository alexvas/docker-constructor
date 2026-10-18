"""Phase 4.1 RED evidence: `check-updates --suggest` is fully non-mutating.

Runs the real facade (`docker.constructor_cli.main`) end-to-end against a
temporary inventory with stubbed providers (no network), then asserts that
producing complete replacement fragments never writes the inventory, the
working directory, a cache policy, or any generated file.
"""

from __future__ import annotations

import io
import os
import pathlib
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from docker.versioning.model import UpdateCandidate, UpdateKind
from docker.versioning.providers.base import ProviderResult

from tests.versioning.support.inventory_builder import minimal_toml


def _stub_providers() -> dict[str, object]:
    """Provider registry where only ``pypi`` reports an applicable update."""

    class _Current:
        def discover(self, target, context):  # noqa: ANN001
            return ProviderResult(
                candidate=UpdateCandidate(
                    value=target.current,
                    kind=UpdateKind.VERSION,
                    artifacts={},
                )
            )

    class _TyOutdated:
        def discover(self, target, context):  # noqa: ANN001
            return ProviderResult(
                candidate=UpdateCandidate(
                    value="0.0.62",
                    kind=UpdateKind.VERSION,
                    artifacts={},
                )
            )

    return {
        "npm": _Current(),
        "pypi": _TyOutdated(),
        "github-release": _Current(),
        "rust-channel": _Current(),
        "docker-registry": _Current(),
        "git-ref": _Current(),
        "uv-python": _Current(),
        "static-url": _Current(),
    }


class TestSuggestIsNonMutating(unittest.TestCase):
    """4.1 — `--suggest` writes nothing while emitting complete fragments."""

    def test_suggest_writes_no_files_and_renders_fragment(self) -> None:
        from docker import constructor_cli

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            inv_path = tmp_path / "docker-constructor.toml"
            inv_path.write_text(minimal_toml())
            before = inv_path.read_bytes()

            original_cwd = os.getcwd()
            try:
                os.chdir(tmp_path)
                out = io.StringIO()
                err = io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    with mock.patch(
                        "docker.versioning.updates._DEFAULT_PROVIDERS",
                        _stub_providers(),
                    ):
                        rc = constructor_cli.main(
                            [
                                "--project-directory",
                                str(pathlib.Path(inv_path).parent),
                                "check-updates",
                                "--suggest",
                            ],
                            stdout_isatty=lambda: False,
                            stderr_isatty=lambda: False,
                        )
            finally:
                os.chdir(original_cwd)

            # Inventory is byte-for-byte unchanged.
            self.assertEqual(inv_path.read_bytes(), before)

            # No cache policy, generated file, or working-tree artifact
            # appeared: the directory still contains only the inventory.
            self.assertEqual(
                sorted(p.name for p in tmp_path.iterdir()),
                ["docker-constructor.toml"],
            )

            self.assertEqual(rc, 0)
            text = out.getvalue()
            # A complete, labelled fragment was produced.
            self.assertIn(
                "─── manual replacement blocks "
                "(review-only — not applied automatically) ───",
                text,
            )
            self.assertIn("# --- toolchain.ty ---", text)
            self.assertIn("[build.stages.toolchain.ty]", text)
            self.assertIn('version = "0.0.62"', text)


if __name__ == "__main__":
    unittest.main()
