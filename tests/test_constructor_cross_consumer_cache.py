"""Production-path cross-consumer cache integration coverage."""

from __future__ import annotations

import base64
import builtins
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from docker.launcher import (
    ProcessResult,
    RunRequest,
    WorkspaceSelection,
    orchestrate_run,
)
from docker.versioning.readonly_service import dispatch


class _Executor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: tuple[str, ...], *, interactive: bool = False) -> ProcessResult:
        self.calls.append(argv)
        return ProcessResult(argv=argv, return_code=0, stdout="ok", stderr="")


class _Inspector:
    def list_names(self) -> set[str]:
        return set()


class _EvidenceRunner:
    def run(self, argv: tuple[str, ...]) -> ProcessResult:
        return ProcessResult(argv=argv, return_code=0, stdout="evidence", stderr="")


class _Clock:
    def now(self) -> float:
        return 1.0


@contextmanager
def _reject_legacy_access(*roots: Path):
    """Reject explicit and descriptor-relative access to legacy cache trees."""
    legacy_roots = tuple(os.path.abspath(root) for root in roots)
    accesses: list[tuple[str, str]] = []
    raw_readlink = os.readlink

    def resolve(path: object, *, dir_fd: int | None = None) -> str:
        if isinstance(path, int):
            return os.path.abspath(raw_readlink(f"/proc/self/fd/{path}"))
        try:
            value = os.fspath(path)
        except TypeError:
            return ""
        if dir_fd is not None and not os.path.isabs(value):
            value = os.path.join(raw_readlink(f"/proc/self/fd/{dir_fd}"), value)
        return os.path.abspath(value)

    def check(operation: str, path: object, *, dir_fd: int | None = None) -> None:
        candidate = resolve(path, dir_fd=dir_fd)
        if any(
            candidate == root or candidate.startswith(root + os.sep)
            for root in legacy_roots
        ):
            accesses.append((operation, candidate))
            raise AssertionError(f"legacy cache access: {operation} {candidate}")

    def single_path_guard(operation, original):
        def wrapper(path, *args, **kwargs):
            check(operation, path, dir_fd=kwargs.get("dir_fd"))
            return original(path, *args, **kwargs)

        return wrapper

    def two_path_guard(
        operation, original, *, descriptor_names=("src_dir_fd", "dst_dir_fd")
    ):
        def wrapper(source, destination, *args, **kwargs):
            check(operation, source, dir_fd=kwargs.get(descriptor_names[0]))
            check(operation, destination, dir_fd=kwargs.get(descriptor_names[1]))
            return original(source, destination, *args, **kwargs)

        return wrapper

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
                mock.patch.object(
                    os,
                    name,
                    single_path_guard(name, getattr(os, name)),
                )
            )
        for name in ("rename", "replace"):
            patches.enter_context(
                mock.patch.object(
                    os,
                    name,
                    two_path_guard(name, getattr(os, name)),
                )
            )
        patches.enter_context(
            mock.patch("builtins.open", single_path_guard("open", builtins.open))
        )
        for name in ("copy", "copy2", "copyfile"):
            patches.enter_context(
                mock.patch.object(
                    shutil,
                    name,
                    two_path_guard(
                        name,
                        getattr(shutil, name),
                        descriptor_names=("src_dir_fd", "dst_dir_fd"),
                    ),
                )
            )
        yield accesses


def _tree_snapshot(root: Path) -> dict[str, tuple[bytes | None, int, int]]:
    """Capture entries, modes, timestamps, and regular-file contents."""
    snapshot: dict[str, tuple[bytes | None, int, int]] = {}
    if not root.exists():
        return snapshot
    for path in [root, *sorted(root.rglob("*"))]:
        stat = path.stat()
        snapshot[str(path.relative_to(root))] = (
            path.read_bytes() if path.is_file() else None,
            stat.st_mode & 0o777,
            stat.st_mtime_ns,
        )
    return snapshot


class TestCrossConsumerCacheRoot(unittest.TestCase):
    def _fixture_inventory(self, directory: Path) -> Path:
        source = Path(__file__).parents[1] / "docker-constructor.toml"
        inventory = directory / "docker-constructor.toml"
        content = source.read_text().replace(
            "[cache]\n",
            "[cache]\nttl = 123\n",
            1,
        )

        def replace_integrity(match: re.Match[str]) -> str:
            url = match.group(1)
            digest = base64.b64encode(
                hashlib.sha512(("artifact:" + url).encode()).digest()
            ).decode()
            return f'url = "{url}"\nintegrity = "sha512-{digest}"'

        content = re.sub(
            r'url = "([^"]+)"\nintegrity = "[^"]+"',
            replace_integrity,
            content,
        )
        content = re.sub(
            r'integrity = "[^"]+"\nurl = "([^"]+)"',
            replace_integrity,
            content,
        )
        inventory.write_text(content)
        return inventory

    def _run_consumers(
        self,
        *,
        checkout: Path,
        xdg: Path,
        local_root: Path | None,
    ) -> Path:
        inventory = self._fixture_inventory(checkout)
        if local_root is not None:
            inventory.with_name("docker-constructor.local.toml").write_text(
                f"[cache]\ndir = {str(local_root)!r}\n"
            )

        requested: list[str] = []

        class _Response:
            status = 200
            headers: dict[str, str] = {}

            def read(self) -> bytes:
                return b"1.0.0\n"

        def urlopen(request, timeout=30):
            requested.append(request.full_url)
            return type(
                "ResponseContext",
                (),
                {
                    "__enter__": lambda self: _Response(),
                    "__exit__": lambda self, *args: None,
                },
            )()

        class _UpdateProvider:
            name = "static-url"

            def discover(self, target, context):
                context.http.request("GET", "https://updates.test/version")
                from docker.versioning.providers.base import ProviderResult

                return ProviderResult(unavailable_reason="integration fixture")

        artifact_calls: list[str] = []

        def fetch(url: str) -> bytes:
            artifact_calls.append(url)
            return ("artifact:" + url).encode()

        old_cwd = os.getcwd()
        os.chdir(checkout)
        try:
            with (
                mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(xdg)}, clear=False),
                mock.patch("urllib.request.urlopen", side_effect=urlopen),
                mock.patch(
                    "docker.versioning.updates._DEFAULT_PROVIDERS",
                    {"static-url": _UpdateProvider()},
                ),
            ):
                # This loads reviewed [cache].ttl and resolves the local companion.
                result = dispatch(
                    inventory,
                    "check-updates",
                    command_args={"_inventory_path": str(inventory), "scope": "all"},
                )
                self.assertIsNotNone(result.data)
                self.assertTrue(
                    requested,
                    f"update discovery did not request fixture URL: {result.data}",
                )
                first_request_count = len(requested)
                # A second discovery has a new transport, so this proves the
                # reviewed ttl=123 is applied to the persisted HTTP entry.
                dispatch(
                    inventory,
                    "check-updates",
                    command_args={"_inventory_path": str(inventory), "scope": "all"},
                )
                self.assertEqual(len(requested), first_request_count)
                runtime = orchestrate_run(
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
            os.chdir(old_cwd)

        self.assertEqual(runtime.exit_kind.value, "success", runtime.message)
        self.assertTrue(requested, "update discovery must issue HTTP requests")
        self.assertTrue(artifact_calls, "runtime must fetch selected artifacts")
        # The consumers resolve themselves; this expected root is only asserted.
        return local_root if local_root is not None else xdg / "docker-constructor"

    def _exercise(self, *, local: bool) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            checkout, xdg = base / "checkout", base / "xdg"
            checkout.mkdir()
            local_root = base / "dedicated-cache" if local else None
            legacy_http = xdg / "pi-cli" / "versioning"
            legacy_artifacts = checkout / ".docker-generated" / "runtime-artifacts"
            for root, name in (
                (legacy_http, "response.json"),
                (legacy_artifacts, "blob.tgz"),
            ):
                root.mkdir(parents=True, exist_ok=True)
                (root / name).write_bytes(b"legacy")
                os.chmod(root / name, 0o640)
            before_http, before_artifacts = (
                _tree_snapshot(legacy_http),
                _tree_snapshot(legacy_artifacts),
            )

            # Keep the no-access guard active for every production path,
            # including projection creation and default evidence collection.
            with _reject_legacy_access(legacy_http, legacy_artifacts) as accesses:
                root = self._run_consumers(
                    checkout=checkout,
                    xdg=xdg,
                    local_root=local_root,
                )
                from docker import constructor_cli

                stdout, stderr = io.StringIO(), io.StringIO()
                with (
                    mock.patch.object(constructor_cli, "_INSTALLATION_ROOT", checkout),
                    mock.patch.dict(
                        os.environ, {"XDG_CACHE_HOME": str(xdg)}, clear=False
                    ),
                    redirect_stdout(stdout),
                    redirect_stderr(stderr),
                ):
                    code = constructor_cli.main(
                        [
                            "--project-directory",
                            str(Path(checkout / "docker-constructor.toml").parent),
                            "--output",
                            "json",
                            "verify",
                            "--scope",
                            "build",
                            "--collect-evidence",
                            "--dry-run",
                            "--container",
                            "pi-test",
                        ],
                        _process_runner=_EvidenceRunner(),
                        _prompt_user=lambda _: True,
                    )
            self.assertEqual(accesses, [])

            self.assertTrue(
                any((root / "versioning").iterdir()),
                f"expected HTTP cache beneath {root}; found {list(base.rglob('*'))}",
            )
            self.assertTrue(any((root / "runtime-artifacts" / "blobs").rglob("*.tgz")))
            self.assertTrue((root / "runtime-artifacts" / "locks").is_dir())
            self.assertTrue((root / "runtime-artifacts" / "tmp").is_dir())
            from docker.versioning.project_state import resolve_project_state

            state = resolve_project_state(checkout, cache_root=root)
            projection = state.runtime_root
            self.assertTrue(projection.is_dir())
            self.assertIn(code, (0, 4), stderr.getvalue() + stdout.getvalue())
            evidence_dir = Path(
                json.loads(stdout.getvalue())["data"]["verification"][
                    "collect_evidence"
                ]["output_dir"]
            )
            self.assertTrue(evidence_dir.is_relative_to(state.evidence_root))
            self.assertTrue(evidence_dir.is_dir())
            self.assertFalse((root / ".docker-generated").exists())
            # A second independent check proves that no legacy mutation or
            # metadata change occurred during any guarded operation.
            self.assertEqual(_tree_snapshot(legacy_http), before_http)
            self.assertEqual(_tree_snapshot(legacy_artifacts), before_artifacts)

    def test_default_xdg_root_is_shared_by_production_consumers(self) -> None:
        self._exercise(local=False)

    def test_local_root_is_shared_by_production_consumers(self) -> None:
        self._exercise(local=True)


if __name__ == "__main__":
    unittest.main()
