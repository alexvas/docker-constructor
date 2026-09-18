"""Phase 3 release-order evidence.

The cache-root contract requires cache storage to validate and prepare the
selected root and every encountered descendant *before* cache-owned state is
released to an effectful consumer.  These tests drive the production
orchestration entry points and prove that a cache failure is surfaced while
the effect boundary was never reached.

Each test supplies either an invalid selected root (dangerous) or an unsafe
existing descendant, patches the effect boundary with a recording probe,
invokes the production entry point, and asserts:

* the cache error is surfaced to the caller, and
* the effect probe was not called.

Effect categories: cache mutation, network access, artifact publication,
container execution, and Docker execution.
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _fixture_inventory(directory: Path) -> Path:
    """Copy the reviewed inventory with deterministic artifact integrities."""
    source = _REPO_ROOT / "docker-constructor.toml"
    inventory = directory / "docker-constructor.toml"
    content = source.read_text()

    def replace_integrity(match: re.Match[str]) -> str:
        url = match.group(1)
        digest = base64.b64encode(hashlib.sha512(
            ("release-order:" + url).encode()
        ).digest()).decode()
        return f'url = "{url}"\nintegrity = "sha512-{digest}"'

    content = re.sub(
        r'url = "([^"]+)"\nintegrity = "[^"]+"', replace_integrity, content,
    )
    content = re.sub(
        r'integrity = "[^"]+"\nurl = "([^"]+)"', replace_integrity, content,
    )
    inventory.write_text(content)
    return inventory


def _write_companion(inventory: Path, cache_dir: str) -> None:
    inventory.with_name("docker-constructor.local.toml").write_text(
        f"[cache]\ndir = {cache_dir!r}\n"
    )


class _RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: tuple[str, ...], *, interactive: bool = False):
        from docker.launcher import ProcessResult
        self.calls.append(argv)
        return ProcessResult(argv=argv, return_code=0, stdout="", stderr="")


class _NoContainersInspector:
    def list_names(self) -> set[str]:
        return set()


class _ReleaseOrderingTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="release-order-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.checkout = self.base / "checkout"
        self.checkout.mkdir()
        self.xdg = self.base / "xdg"
        self.inventory = _fixture_inventory(self.checkout)
        self._saved = {
            key: os.environ.get(key) for key in ("HOME", "XDG_CACHE_HOME")
        }
        home = self.base / "home"
        home.mkdir()
        os.environ["HOME"] = str(home)
        os.environ["XDG_CACHE_HOME"] = str(self.xdg)
        self.addCleanup(self._restore_env)

    def _restore_env(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _make_dedicated_root(self, *, unsafe_descendant: bool) -> Path:
        root = self.base / "dedicated-cache"
        root.mkdir(mode=0o700)
        if unsafe_descendant:
            target = self.base / "attacker-target"
            target.mkdir()
            os.symlink(target, root / "runtime-artifacts")
        return root


class TestRunEffectsBlockedBeforeRelease(_ReleaseOrderingTestCase):
    """``orchestrate_run`` must not reach network, publication, or Docker."""

    def _run(self, cache_dir: str):
        from docker.launcher import RunRequest, WorkspaceSelection, orchestrate_run

        executor = _RecordingExecutor()
        fetchers: list[str] = []

        def fetch(url: str) -> bytes:
            fetchers.append(url)
            return b"should-not-be-fetched"

        with mock.patch(
            "docker.versioning.artifact_cache.materialize_selected_artifacts",
        ) as materialize, mock.patch(
            "urllib.request.urlopen",
            side_effect=AssertionError("network access reached"),
        ):
            result = orchestrate_run(RunRequest(
                inventory_path=str(self.inventory),
                image="test-image",
                selection=WorkspaceSelection(workspace="/work/project"),
                pi_home_host="/home/test/.pi",
                repo_root=str(self.checkout),
                project_root=self.checkout,
                _artifact_fetcher=fetch,
                executor=executor,
                inspector=_NoContainersInspector(),
            ))
        return result, materialize, fetchers, executor

    def test_dangerous_root_blocks_every_effect(self) -> None:
        _write_companion(self.inventory, "/")

        result, materialize, fetchers, executor = self._run("/")

        self.assertEqual("config", result.exit_kind.value, result.message)
        materialize.assert_not_called()
        self.assertEqual([], fetchers, "network fetcher must not be called")
        self.assertEqual([], executor.calls, "container execution must not run")
        self.assertFalse(self.xdg.exists(), "default cache root must not be created")
        self.assertFalse(
            (self.checkout / "runtime-artifacts").exists(),
            "no cache child may be created in the checkout",
        )

    def test_unsafe_descendant_blocks_every_effect(self) -> None:
        root = self._make_dedicated_root(unsafe_descendant=True)
        root_before = sorted(p.name for p in root.iterdir())
        _write_companion(self.inventory, str(root))

        result, materialize, fetchers, executor = self._run(str(root))

        self.assertEqual("config", result.exit_kind.value, result.message)
        materialize.assert_not_called()
        self.assertEqual([], fetchers)
        self.assertEqual([], executor.calls)
        # The cache-storage mutation gate ran before any cache write: the
        # unsafe descendant is still a symlink and no child was created.
        self.assertTrue((root / "runtime-artifacts").is_symlink())
        self.assertEqual(root_before, sorted(p.name for p in root.iterdir()))
        self.assertFalse((root / "versioning").exists())

    def test_valid_root_reaches_effect_boundaries(self) -> None:
        """Control: a valid root reaches materialization and Docker."""
        from docker.launcher import RunRequest, WorkspaceSelection, orchestrate_run

        root = self._make_dedicated_root(unsafe_descendant=False)
        _write_companion(self.inventory, str(root))
        executor = _RecordingExecutor()
        with mock.patch(
            "docker.versioning.artifact_cache.materialize_selected_artifacts",
            return_value={},
        ) as materialize:
            result = orchestrate_run(RunRequest(
                inventory_path=str(self.inventory),
                image="test-image",
                selection=WorkspaceSelection(workspace="/work/project"),
                pi_home_host="/home/test/.pi",
                repo_root=str(self.checkout),
                project_root=self.checkout,
                executor=executor,
                inspector=_NoContainersInspector(),
            ))
        self.assertNotEqual("config", result.exit_kind.value, result.message)
        materialize.assert_called_once()
        self.assertTrue(executor.calls, "valid root must reach container execution")

    def test_dangerous_root_does_not_mutate_any_cache_entry(self) -> None:
        from docker.versioning import cache_storage

        for bad in ("/", "relative/path", ""):
            with self.subTest(cache_dir=bad):
                _write_companion(self.inventory, bad)
                with mock.patch.object(
                    cache_storage, "_create_and_secure",
                ) as create, mock.patch.object(
                    cache_storage, "_harden_tree",
                ) as harden:
                    result, *_ = self._run(bad)
                self.assertEqual("config", result.exit_kind.value, result.message)
                create.assert_not_called()
                harden.assert_not_called()


class TestBuildDockerExecutionBlockedBeforeRelease(_ReleaseOrderingTestCase):
    """``orchestrate_build`` must not reach Docker when the root is unsafe."""

    def _orchestrate(self, cache_dir: str):
        from docker.versioning.build_orchestration import (
            BuildRequest,
            orchestrate_build,
        )

        _write_companion(self.inventory, cache_dir)
        with mock.patch(
            "docker.versioning.build_orchestration.execute_build",
        ) as execute:
            result = orchestrate_build(BuildRequest(
                inventory_path=str(self.inventory),
                project_root=str(self.checkout),
                confirmed=True,
                runner=mock.Mock(),
            ))
        return result, execute

    def test_dangerous_root_blocks_docker_execution(self) -> None:
        result, execute = self._orchestrate("/")
        self.assertEqual("config", result.exit_kind.value, result.message)
        execute.assert_not_called()

    def test_empty_root_blocks_docker_execution(self) -> None:
        result, execute = self._orchestrate("")
        self.assertEqual("config", result.exit_kind.value, result.message)
        execute.assert_not_called()

    def test_valid_dedicated_root_reaches_execution_boundary(self) -> None:
        """Control: a valid root does reach the execution boundary."""
        result, execute = self._orchestrate(str(self.base / "dedicated-cache"))
        # The plan is valid, so ``execute_build`` is invoked (and fails on
        # the mock runner), proving the block was caused by the cache root.
        execute.assert_called_once()


class TestBuildUnsafeDescendantBlockedBeforeRelease(_ReleaseOrderingTestCase):
    """Real ``execute_build`` rejects an unsafe encountered descendant before
    materialization, publication, or the Docker runner.

    ``execute_build`` prepares the shared root with ``prepare_project_root``
    and then validates its own project-scoped ``projects`` child through
    ``resolve_project_state``.  It deliberately never reads or writes the
    global ``runtime-artifacts``/``versioning`` subtrees, so a symlink there
    is not an encountered descendant and does not block a build.  The run
    path, which does use the global subtree, rejects the same descendant
    (see ``TestRunEffectsBlockedBeforeRelease``).
    """

    def _plan_and_execute(self, make_descendant):
        from docker.versioning.build_orchestration import (
            BuildRequest,
            execute_build,
            plan_build,
        )

        root = self.base / "dedicated-cache"
        root.mkdir(mode=0o700)
        if make_descendant is not None:
            make_descendant(root)
        root_before = sorted(p.name for p in root.iterdir())
        _write_companion(self.inventory, str(root))

        reached: list[str] = []

        def _probe(name: str):
            def probe(*_args, **_kwargs):
                reached.append(name)
                raise AssertionError(f"{name} reached before cache rejection")
            return probe

        class _Runner:
            def run(self, argv):
                reached.append("runner")
                raise AssertionError("docker runner reached before cache rejection")

        request = BuildRequest(
            inventory_path=str(self.inventory),
            project_root=str(self.checkout),
            confirmed=True,
            runner=_Runner(),
            _named_context_supported=lambda: True,
            _materialize_artifacts=_probe("materialize"),
            _publish_projection=_probe("publish"),
        )
        plan = plan_build(request)
        self.assertEqual(
            "success", plan.exit_kind.value,
            f"descendant inspection is a preparation concern: {plan.message}",
        )
        # ``execute_build`` is invoked for real: its cache preparation must
        # reject the unsafe descendant before any injected effect probe runs.
        result = execute_build(plan, request)
        return result, reached, root, root_before

    def test_symlinked_project_descendant_blocks_all_build_effects(self) -> None:
        target = self.base / "attacker-target"
        target.mkdir()

        result, reached, root, root_before = self._plan_and_execute(
            lambda root: os.symlink(target, root / "projects"),
        )

        self.assertEqual("operational", result.exit_kind.value, result.message)
        self.assertEqual([], reached, "no build effect may be reached")
        self.assertTrue((root / "projects").is_symlink())
        self.assertEqual(root_before, sorted(p.name for p in root.iterdir()))

    def test_non_directory_project_descendant_blocks_all_build_effects(self) -> None:
        def make(root: Path) -> None:
            (root / "projects").write_text("not a directory")

        result, reached, root, root_before = self._plan_and_execute(make)

        self.assertEqual("operational", result.exit_kind.value, result.message)
        self.assertEqual([], reached, "no build effect may be reached")
        self.assertTrue((root / "projects").is_file())
        self.assertEqual(root_before, sorted(p.name for p in root.iterdir()))

    def test_global_cache_subtrees_are_not_build_path_descendants(self) -> None:
        """A build never reads or writes the global ``runtime-artifacts`` tree.

        The build path secures only the shared root and its own ``projects``
        namespace, so a symlinked global subtree is not encountered and does
        not block a build.  ``orchestrate_run``, which does use the global
        subtree, rejects it (``TestRunEffectsBlockedBeforeRelease``).
        """
        from docker.versioning.build_orchestration import (
            BuildRequest,
            execute_build,
            plan_build,
        )

        root = self.base / "dedicated-cache"
        root.mkdir(mode=0o700)
        target = self.base / "attacker-target"
        target.mkdir()
        os.symlink(target, root / "runtime-artifacts")
        _write_companion(self.inventory, str(root))

        reached: list[str] = []

        def materialize(*_args, **_kwargs):
            reached.append("materialize")
            raise AssertionError("materialize reached")

        request = BuildRequest(
            inventory_path=str(self.inventory),
            project_root=str(self.checkout),
            confirmed=True,
            runner=mock.Mock(),
            _named_context_supported=lambda: True,
            _materialize_artifacts=materialize,
        )
        plan = plan_build(request)
        self.assertEqual("success", plan.exit_kind.value, plan.message)
        with self.assertRaises(AssertionError):
            execute_build(plan, request)
        self.assertEqual(["materialize"], reached)
        # The build created its own namespace but never wrote through the
        # symlinked global subtree.
        self.assertTrue((root / "runtime-artifacts").is_symlink())
        self.assertEqual([], list(target.iterdir()))

    def test_valid_root_reaches_first_build_effect(self) -> None:
        """Control: a valid root reaches the first downstream effect."""
        from docker.versioning.build_orchestration import (
            BuildRequest,
            execute_build,
            plan_build,
        )

        root = self.base / "dedicated-cache"
        root.mkdir(mode=0o700)
        _write_companion(self.inventory, str(root))
        reached: list[str] = []

        def materialize(*_args, **_kwargs):
            reached.append("materialize")
            raise AssertionError("materialize reached")

        request = BuildRequest(
            inventory_path=str(self.inventory),
            project_root=str(self.checkout),
            confirmed=True,
            runner=mock.Mock(),
            _named_context_supported=lambda: True,
            _materialize_artifacts=materialize,
            _publish_projection=mock.Mock(),
        )
        plan = plan_build(request)
        self.assertEqual("success", plan.exit_kind.value, plan.message)
        with self.assertRaises(AssertionError):
            execute_build(plan, request)
        self.assertEqual(["materialize"], reached)


class TestCacheOwnedDescendantMutationOrdering(_ReleaseOrderingTestCase):
    """Existing descendants are inspected before any mutation."""

    def test_symlinked_descendant_rejected_before_create(self) -> None:
        from docker.versioning import cache_storage

        root = self._make_dedicated_root(unsafe_descendant=True)
        with mock.patch.object(cache_storage, "_create_and_secure") as create:
            with self.assertRaises(cache_storage.CacheStorageError):
                cache_storage.prepare_resolved_root(root)
        create.assert_not_called()
        self.assertTrue((root / "runtime-artifacts").is_symlink())
        self.assertFalse((root / "versioning").exists())

    def test_unsafe_root_rejected_before_inspection(self) -> None:
        from docker.versioning import cache_storage

        with mock.patch.object(cache_storage, "_inspect_entry") as inspect, \
             mock.patch.object(cache_storage, "_create_and_secure") as create:
            with self.assertRaises(cache_storage.CacheStorageError):
                cache_storage.prepare_local_root(
                    "/", xdg_cache_home=str(self.xdg),
                    home=self.base / "home",
                )
        inspect.assert_not_called()
        create.assert_not_called()

    def test_valid_root_prepares_expected_named_children(self) -> None:
        from docker.versioning import cache_storage

        root = self.base / "valid-cache"
        prepared = cache_storage.prepare_resolved_root(root)
        self.assertEqual(root, prepared)
        for parts in cache_storage._DESCENDANT_PARTS:
            child = root.joinpath(*parts)
            self.assertTrue(child.is_dir(), str(child))
            self.assertEqual(stat.S_IMODE(child.stat().st_mode), 0o700)


if __name__ == "__main__":
    unittest.main()
