"""Phase 7 facade acceptance coverage for constructor-project isolation."""
from __future__ import annotations

import os
import shutil
import stat
import tempfile
import unittest
from unittest.mock import patch
from contextlib import contextmanager
from pathlib import Path

from docker.constructor_cli import main
from docker.versioning.dispatch_types import CommandResult, ExitKind
from docker.versioning.project_state import resolve_project_state
from tests.build_test_support import (
    INVENTORY_PATH, digest_valid_selected_artifacts, fake_pi_materialization,
    publish_digest_valid_artifacts,
)


def _entries(root: Path) -> set[Path]:
    return {root, *(root.rglob("*") if root.exists() else ())}


def _snapshot(root: Path) -> dict[Path, tuple[object, ...]]:
    result: dict[Path, tuple[object, ...]] = {Path("."): ("dir", stat.S_IMODE(root.stat().st_mode))}
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if path.is_dir():
            result[relative] = ("dir", stat.S_IMODE(path.stat().st_mode))
        else:
            result[relative] = ("file", path.read_bytes())
    return result


@contextmanager
def _cwd(path: Path):
    old = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


class ConstructorProjectFacadeAcceptancePhase7Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.source_checkout = root / "source-checkout"; self.source_checkout.mkdir()
        self.project = root / "selected-project"; self.project.mkdir()
        self.primary = root / "primary-workspace"; self.primary.mkdir()
        self.extra = root / "extra-workspace"; self.extra.mkdir()
        self.outside = root / "outside"; self.outside.mkdir()
        self.cache = root / "xdg-cache"; self.cache.mkdir(mode=0o700); self.cache.chmod(0o700)
        self.local_cache = self.cache / "constructor-state"; self.local_cache.mkdir(mode=0o700)
        self.home = root / "home"; self.home.mkdir(mode=0o700)
        self.env = patch.dict(os.environ, {
            "HOME": str(self.home), "XDG_CACHE_HOME": str(self.cache),
        })
        self.env.start(); self.addCleanup(self.env.stop)
        shutil.copyfile(INVENTORY_PATH, self.project / "docker-constructor.toml")
        (self.project / "Dockerfile").write_text("FROM scratch\n")
        (self.project / "docker-constructor.local.toml").write_text(
            f'[cache]\ndir = "{self.local_cache}"\n'
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_explicit_project_real_facade_plans_all_commands_outside_checkout(self):
        """P7.R1: real facade planning uses only the selected project."""
        import io
        from contextlib import redirect_stdout, redirect_stderr
        from docker.versioning.readonly_service import _HANDLERS

        before = {path: _snapshot(path) for path in (self.project, self.primary, self.extra)}
        original_updates = _HANDLERS["check-updates"]
        _HANDLERS["check-updates"] = lambda *_args, **_kwargs: CommandResult(ExitKind.SUCCESS)
        try:
            commands = (
                ["validate"], ["show"], ["check-updates"], ["build", "--dry-run"],
                ["run", "--workspace", str(self.primary), "--extra-workspace", str(self.extra), "--dry-run"],
                ["doctor"],
            )
            stdout = io.StringIO()
            with _cwd(self.outside), redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                results = [main(["--project-directory", str(self.project), *command]) for command in commands]
        finally:
            _HANDLERS["check-updates"] = original_updates
        self.assertEqual([0] * len(commands), results)
        state = resolve_project_state(self.project.resolve(), cache_root=self.local_cache, create=False)
        self.assertTrue((self.project / "docker-constructor.toml").is_file())
        self.assertTrue((self.project / "docker-constructor.local.toml").is_file())
        self.assertIn(str(self.project / "Dockerfile"), stdout.getvalue())
        self.assertIn(str(self.project), stdout.getvalue())
        self.assertIn(f"--workdir {self.primary}", stdout.getvalue())
        self.assertIn(f"src={self.extra},dst={self.extra}", stdout.getvalue())
        self.assertFalse(state.namespace.is_relative_to(self.primary))
        self.assertFalse(state.namespace.is_relative_to(self.extra))
        for path, tree in before.items():
            self.assertEqual(tree, _snapshot(path), f"{path} was mutated")

    def test_explicit_project_publishes_build_and_runtime_projections_only_in_its_namespace(self):
        """P7.R1: real non-dry-run facade orchestration has one namespace identity."""
        from docker.launcher import ProcessResult
        from docker.versioning.build_orchestration import ProcessResult as BuildProcessResult

        identities: list[Path] = []
        class BuildRunner:
            def __init__(self, *_args, **_kwargs): pass
            def run(self, argv): return BuildProcessResult(tuple(argv), 0, "", "")
        run_vectors: list[tuple[str, ...]] = []
        class RunExecutor:
            def run(self, argv, *, mode=None, interactive=None):
                run_vectors.append(tuple(argv))
                return ProcessResult(tuple(argv), 0, "", "")
        class Inspector:
            def list_names(self): return ()

        import docker.launcher as launcher
        import docker.versioning.build_orchestration as build
        real_build_resolve, real_run_resolve = build.resolve_project_state, launcher.resolve_project_state
        def record_build(path, *args, **kwargs):
            identities.append(Path(path).resolve())
            return real_build_resolve(path, *args, **kwargs)
        def record_run(path, *args, **kwargs):
            identities.append(Path(path).resolve())
            return real_run_resolve(path, *args, **kwargs)
        before = {path: _snapshot(path) for path in (self.project, self.primary, self.extra)}
        with _cwd(self.outside), \
             patch("docker.versioning.build_orchestration.resolve_project_state", side_effect=record_build), \
             patch("docker.launcher.resolve_project_state", side_effect=record_run), \
             patch("docker.versioning.build_orchestration.SubprocessBuildExecutor", BuildRunner), \
             patch("docker.versioning.build_orchestration._default_named_context_supported", return_value=True), \
             patch("docker.versioning.build_orchestration.select_build_artifacts", digest_valid_selected_artifacts), \
             patch("docker.versioning.build_orchestration.materialize_build_artifacts", publish_digest_valid_artifacts), \
             patch("docker.versioning.build_orchestration._materialize_pi_for_build", lambda *_args: fake_pi_materialization()), \
             patch("docker.launcher.artifact_cache.materialize_selected_artifacts", return_value={}):
            self.assertEqual(0, main(["--project-directory", str(self.project), "build", "--yes"]))
            self.assertEqual(0, main([
                "--project-directory", str(self.project), "run", "--workspace", str(self.primary),
                "--extra-workspace", str(self.extra),
            ], _run_executor=RunExecutor(), _container_inspector=Inspector()))
        state = resolve_project_state(self.project.resolve(), cache_root=self.local_cache, create=False)
        self.assertTrue((state.generated_root / "docker-constructor.build.effective.toml").is_file())
        self.assertTrue(any(str(state.runtime_root) in value for value in run_vectors[0]))
        self.assertTrue(identities)
        self.assertEqual({self.project.resolve()}, set(identities))
        for path, tree in before.items():
            self.assertEqual(tree, _snapshot(path), f"{path} was mutated")

    def test_explicit_evidence_output_under_xdg_cache_does_not_change_project_identity(self):
        """P7.R2: real evidence collection preserves caller-directed output."""
        from docker.launcher import ProcessResult

        class Runner:
            def run(self, argv, *, mode=None):
                return ProcessResult(tuple(argv), 0, "", "")

        output = self.cache / "caller-evidence"
        state = resolve_project_state(self.project.resolve(), cache_root=self.local_cache)
        projection = state.generated_root / "docker-constructor.build.effective.toml"
        projection.write_text(
            '[python]\nversion = "3.12.0"\n[node]\nimage = "node:20"\n'
            '[rust]\nversion = "1.77.0"\ncomponents = ["cargo"]\n'
            '[uv]\nversion = "0.5.0"\n[ty]\nversion = "v0.9.0"\n'
            '[rtk]\nversion = "0.31.0"\n[fd]\nversion = "9.0.0"\n'
            '[pi]\nversion = "v1.4.236"\n[openspec]\nversion = "v0.15.0"\n'
            '[oh-my-zsh]\nrevision = "abc1234"\n'
        )
        before = {path: _snapshot(path) for path in (self.project, self.primary, self.extra)}
        # Runtime-artifact cache is global cache infrastructure, not
        # project-generated state; provision its empty fixture root first.
        (self.local_cache / "versioning").mkdir(exist_ok=True)
        for child in ("blobs", "locks", "tmp"):
            (self.local_cache / "runtime-artifacts" / child).mkdir(parents=True, exist_ok=True)
        cache_before = _entries(self.cache)
        local_cache_before = _entries(self.local_cache)
        identities: list[Path] = []
        inventory_paths: list[Path] = []
        companion_inputs: list[Path] = []
        import docker.versioning.inventory as inventory
        import docker.versioning.project_state as project_state
        real_resolve = project_state.resolve_project_state
        real_inventory_path = __import__("docker.constructor_cli", fromlist=["_"])._resolve_inventory_path
        real_load_config = inventory.load_project_configuration

        def record_resolve(path, *args, **kwargs):
            identities.append(Path(path).resolve())
            return real_resolve(path, *args, **kwargs)
        def record_inventory_path(request):
            path = real_inventory_path(request)
            inventory_paths.append(path)
            return path
        def record_load_config(path, **kwargs):
            companion_inputs.append(Path(path).with_name("docker-constructor.local.toml"))
            return real_load_config(path, **kwargs)

        from docker.versioning.verification import BuildVerificationResult
        with _cwd(self.outside), \
             patch("docker.versioning.verification.verify_build", return_value=BuildVerificationResult("test", (), True, ())), \
             patch("docker.versioning.project_state.resolve_project_state", side_effect=record_resolve), \
             patch("docker.constructor_cli._resolve_inventory_path", side_effect=record_inventory_path), \
             patch("docker.versioning.inventory.load_project_configuration", side_effect=record_load_config):
            explicit_rc = main([
                "--project-directory", str(self.project), "verify", "--scope", "build",
                "--collect-evidence", "--output-dir", str(output),
            ], _process_runner=Runner())
            default_rc = main([
                "--project-directory", str(self.project), "verify", "--scope", "build",
                "--collect-evidence",
            ], _process_runner=Runner())
        self.assertEqual(0, explicit_rc)
        self.assertEqual(0, default_rc)
        self.assertTrue(identities)
        self.assertEqual({self.project.resolve()}, set(identities))
        for forbidden in (self.primary, self.extra, self.outside, output):
            self.assertNotIn(forbidden.resolve(), identities)
        self.assertEqual([self.project / "docker-constructor.toml"] * 2, inventory_paths)
        self.assertEqual([self.project / "docker-constructor.local.toml"] * 2, companion_inputs)
        self.assertTrue((output / "index.txt").is_file())
        self.assertTrue(any(state.evidence_root.iterdir()))
        created = (_entries(self.cache) - cache_before) | (_entries(self.local_cache) - local_cache_before)
        for path in created:
            self.assertTrue(
                path.is_relative_to(output) or path.is_relative_to(state.namespace),
                f"generated state escaped the project namespace: {path}",
            )
        self.assertTrue(output.is_relative_to(self.cache))
        self.assertNotEqual(state.namespace, output)
        self.assertTrue(projection.is_relative_to(state.generated_root))
        for path, tree in before.items():
            self.assertEqual(tree, _snapshot(path), f"{path} was mutated")

    def test_default_cwd_uses_project_for_real_planning_flows(self):
        """P7.R3: CWD selection reaches real validate/build/run/doctor/verify planning."""
        import io
        from contextlib import redirect_stdout, redirect_stderr
        from docker.versioning.verification import BuildVerificationResult

        state = resolve_project_state(self.project.resolve(), cache_root=self.local_cache)
        projection = state.generated_root / "docker-constructor.build.effective.toml"
        projection.write_text('[python]\nversion = "3.12.0"\n')
        commands = (
            ["validate"], ["build", "--dry-run"],
            ["run", "--workspace", str(self.primary), "--dry-run"], ["doctor"],
            ["verify", "--scope", "build", "--dry-run"],
        )
        with _cwd(self.project), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), patch(
            "docker.versioning.verification.verify_build",
            return_value=BuildVerificationResult("test", (), True, ()),
        ):
            results = [main(command) for command in commands]
        self.assertEqual([0] * len(commands), results)
        self.assertTrue(projection.is_relative_to(state.generated_root))
        self.assertFalse((self.project / ".docker-generated").exists())
