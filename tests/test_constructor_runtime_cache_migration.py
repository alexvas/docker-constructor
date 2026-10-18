"""Phase 5.3: checkout-local runtime-artifact cache migration isolation."""

from __future__ import annotations

import base64
import builtins
import hashlib
import os
import re
import shutil
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest import mock

from docker.launcher import (
    ProcessResult,
    RunRequest,
    WorkspaceSelection,
    orchestrate_run,
)


def _snapshot(root: Path) -> dict[str, tuple[bytes | None, int, int]]:
    result: dict[str, tuple[bytes | None, int, int]] = {}
    for path in [root, *sorted(root.rglob("*"))]:
        stat = path.stat()
        result[str(path.relative_to(root))] = (
            path.read_bytes() if path.is_file() else None,
            stat.st_mode & 0o777,
            stat.st_mtime_ns,
        )
    return result


@contextmanager
def _reject_legacy_access(root: Path):
    """Reject path and descriptor-relative access to the legacy tree."""
    root_text = os.path.abspath(root)
    original_readlink = os.readlink
    accesses: list[tuple[str, str]] = []

    def resolved(path: object, fd: int | None = None) -> str:
        if isinstance(path, int):
            return os.path.abspath(original_readlink(f"/proc/self/fd/{path}"))
        try:
            value = os.fspath(path)
        except TypeError:
            return ""
        if fd is not None and not os.path.isabs(value):
            value = os.path.join(original_readlink(f"/proc/self/fd/{fd}"), value)
        return os.path.abspath(value)

    def check(operation: str, path: object, fd: int | None = None) -> None:
        value = resolved(path, fd)
        if value == root_text or value.startswith(root_text + os.sep):
            accesses.append((operation, value))
            raise AssertionError(f"legacy runtime cache accessed: {operation} {value}")

    def one(operation: str, original):
        def guarded(path, *args, **kwargs):
            check(operation, path, kwargs.get("dir_fd"))
            return original(path, *args, **kwargs)

        return guarded

    def two(operation: str, original):
        def guarded(source, destination, *args, **kwargs):
            check(operation, source, kwargs.get("src_dir_fd"))
            check(operation, destination, kwargs.get("dst_dir_fd"))
            return original(source, destination, *args, **kwargs)

        return guarded

    with ExitStack() as patches:
        for name in (
            "open",
            "access",
            "stat",
            "lstat",
            "scandir",
            "listdir",
            "readlink",
            "chmod",
            "unlink",
            "remove",
            "mkdir",
            "rmdir",
        ):
            patches.enter_context(
                mock.patch.object(os, name, one(name, getattr(os, name)))
            )
        for name in ("rename", "replace"):
            patches.enter_context(
                mock.patch.object(os, name, two(name, getattr(os, name)))
            )
        patches.enter_context(mock.patch("builtins.open", one("open", builtins.open)))
        for name in ("copy", "copy2", "copyfile"):
            patches.enter_context(
                mock.patch.object(shutil, name, two(name, getattr(shutil, name)))
            )
        yield accesses


class _Executor:
    def run(self, argv: tuple[str, ...], *, interactive: bool = False) -> ProcessResult:
        return ProcessResult(argv=argv, return_code=0)


class _Inspector:
    def list_names(self) -> set[str]:
        return set()


class TestLegacyRuntimeArtifactCacheMigration(unittest.TestCase):
    def test_phase_5_3_launcher_never_accesses_legacy_runtime_artifact_tree(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _base, checkout, xdg = (
                Path(directory),
                Path(directory) / "checkout",
                Path(directory) / "xdg",
            )
            checkout.mkdir()
            legacy = checkout / ".docker-generated" / "runtime-artifacts"
            sentinel_contents = {
                "blobs": b"legacy-blob",
                "locks": b"legacy-lock",
                "tmp": b"legacy-tmp",
            }
            for child, content in sentinel_contents.items():
                path = legacy / child / "sentinel"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                os.chmod(path, 0o640)
                os.utime(path, ns=(1_000_000_000, 1_000_000_000))
            before = _snapshot(legacy)

            inventory = checkout / "docker-constructor.toml"
            content = (
                Path(__file__).parents[1] / "docker-constructor.toml"
            ).read_text()

            def sri(match: re.Match[str]) -> str:
                url = match.group(1)
                digest = base64.b64encode(
                    hashlib.sha512(("new:" + url).encode()).digest()
                ).decode()
                return f'url = "{url}"\nintegrity = "sha512-{digest}"'

            content = re.sub(r'url = "([^"]+)"\nintegrity = "[^"]+"', sri, content)
            content = re.sub(r'integrity = "[^"]+"\nurl = "([^"]+)"', sri, content)
            inventory.write_text(content)

            fetched: list[str] = []

            def fetch(url: str) -> bytes:
                fetched.append(url)
                return ("new:" + url).encode()

            previous_cwd = os.getcwd()
            os.chdir(checkout)
            try:
                with (
                    mock.patch.dict(
                        os.environ, {"XDG_CACHE_HOME": str(xdg)}, clear=False
                    ),
                    _reject_legacy_access(legacy) as accesses,
                ):
                    result = orchestrate_run(
                        RunRequest(
                            inventory_path=str(inventory),
                            image="test-image",
                            selection=WorkspaceSelection(workspace="/work/project"),
                            pi_home_host="/home/test/.pi",
                            repo_root=str(checkout),
                            _artifact_fetcher=fetch,
                            executor=_Executor(),
                            inspector=_Inspector(),
                            project_root=Path(str(inventory)).resolve().parent,
                        )
                    )
            finally:
                os.chdir(previous_cwd)

            self.assertEqual(result.exit_kind.value, "success", result.message)
            self.assertEqual(accesses, [])
            self.assertEqual(_snapshot(legacy), before)
            new_blobs = xdg / "docker-constructor" / "runtime-artifacts" / "blobs"
            published = list(new_blobs.rglob("*.tgz"))
            self.assertTrue(fetched)
            self.assertTrue(published)
            self.assertTrue(
                all(
                    path.read_bytes() not in sentinel_contents.values()
                    for path in published
                )
            )
            self.assertFalse(any((xdg / "docker-constructor").rglob("sentinel")))


if __name__ == "__main__":
    unittest.main()
