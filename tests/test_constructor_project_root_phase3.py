"""Phase 3 integration coverage for external constructor-project state."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from docker.launcher import (
    ProcessResult,
    RunRequest,
    WorkspaceSelection,
    orchestrate_run,
)
from docker.versioning.build_orchestration import (
    BuildRequest,
    orchestrate_build,
)
from docker.versioning.build_orchestration import (
    ProcessResult as BuildProcessResult,
)
from docker.versioning.dispatch_types import ExitKind
from docker.versioning.project_state import resolve_project_state
from tests.build_test_support import (
    INVENTORY_PATH,
    digest_valid_selected_artifacts,
    fake_pi_materialization,
    publish_digest_valid_artifacts,
)


class _BuildRunner:
    def run(self, argv):
        return BuildProcessResult(tuple(argv), 0, "", "")


class _RunExecutor:
    def __init__(self, callback=lambda _argv: None):
        self.callback = callback

    def run(self, argv, *, mode=None, interactive=None):
        self.callback(argv)
        return ProcessResult(tuple(argv), 0, "", "")


class _Inspector:
    def list_names(self):
        return ()


class _VerifyRunner:
    """Daemon-free runner; real verification still parses the host TOML."""

    def run(self, argv, *, mode=None):
        return ProcessResult(tuple(argv), 0, "", "")


def _tree_snapshot(root: Path):
    """Capture every entry type, metadata relevant to mutation, and content."""
    snapshot = {Path("."): ("directory", stat.S_IMODE(root.lstat().st_mode))}
    for path in sorted(root.rglob("*")):
        mode = stat.S_IMODE(path.lstat().st_mode)
        relative = path.relative_to(root)
        if path.is_symlink():
            snapshot[relative] = ("symlink", mode, os.readlink(path))
        elif path.is_dir():
            snapshot[relative] = ("directory", mode)
        else:
            snapshot[relative] = ("file", mode, path.read_bytes())
    return snapshot


class ExternalProjectStatePhase3Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.installation = root / "installation-checkout"
        self.installation.mkdir()
        self.project = root / "constructor-project"
        self.project.mkdir()
        self.primary = root / "primary-workspace"
        self.primary.mkdir()
        self.extra = root / "extra-workspace"
        self.extra.mkdir()
        self.cache = root / "external-cache"
        self.cache.mkdir(mode=0o700)
        shutil.copyfile(INVENTORY_PATH, self.project / "docker-constructor.toml")
        (self.project / "Dockerfile").write_text("FROM scratch\n")
        (self.project / "docker-constructor.local.toml").write_text(
            f'[cache]\ndir = "{self.cache}"\n'
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _build(self):
        with patch(
            "docker.versioning.build_orchestration.select_build_artifacts",
            digest_valid_selected_artifacts,
        ):
            return orchestrate_build(
                BuildRequest(
                    inventory_path=str(self.project / "docker-constructor.toml"),
                    project_root=self.project,
                    repo_root=str(self.installation),
                    confirmed=True,
                    runner=_BuildRunner(),
                    _materialize_artifacts=publish_digest_valid_artifacts,
                    _materialize_pi=fake_pi_materialization,
                    _named_context_supported=lambda: True,
                )
            )

    def test_build_publication_uses_real_default_publisher_in_external_namespace(self):
        """P3.R1: publication is atomic and never writes either checkout."""
        result = self._build()
        self.assertEqual(ExitKind.SUCCESS, result.exit_kind, result.message)
        state = resolve_project_state(self.project.resolve(), cache_root=self.cache)
        published = state.generated_root / "docker-constructor.build.effective.toml"
        self.assertEqual(str(published), result.publish_result.published_path)
        self.assertTrue(published.is_file())
        self.assertFalse(any(state.generated_root.glob(".build-effective-*.toml")))
        self.assertFalse((self.installation / ".docker-generated").exists())
        self.assertFalse((self.project / ".docker-generated").exists())

    def test_build_symlink_root_uses_canonical_identity_at_every_boundary(self):
        """P3.G1: build identity must never retain a symlink spelling."""
        import docker.versioning.build_orchestration as build_orchestration

        alias = self.project.parent / "constructor-project-alias"
        alias.symlink_to(self.project, target_is_directory=True)
        resolver_args, materializer_roots, publisher_roots = [], [], []
        real_resolver = build_orchestration.resolve_project_state

        def record_resolver(project, *args, **kwargs):
            resolver_args.append(Path(project))
            return real_resolver(project, *args, **kwargs)

        def record_materializer(projection, *, constructor_project_root, **kwargs):
            materializer_roots.append(Path(constructor_project_root))
            return publish_digest_valid_artifacts(
                projection,
                constructor_project_root=constructor_project_root,
                **kwargs,
            )

        def record_publisher(projection, *, repo_root):
            publisher_roots.append(Path(repo_root))
            state = real_resolver(repo_root, cache_root=self.cache)
            return build_orchestration._publish_projection_default(
                projection,
                repo_root=repo_root,
                project_state=state,
            )

        with (
            patch.object(
                build_orchestration,
                "resolve_project_state",
                side_effect=record_resolver,
            ),
            patch.object(
                build_orchestration,
                "select_build_artifacts",
                digest_valid_selected_artifacts,
            ),
        ):
            result = orchestrate_build(
                BuildRequest(
                    inventory_path=str(self.project / "docker-constructor.toml"),
                    project_root=alias,
                    repo_root=str(self.installation),
                    confirmed=True,
                    runner=_BuildRunner(),
                    _materialize_artifacts=record_materializer,
                    _publish_projection=record_publisher,
                    _materialize_pi=fake_pi_materialization,
                    _named_context_supported=lambda: True,
                )
            )
        canonical = self.project.resolve()
        self.assertEqual(ExitKind.SUCCESS, result.exit_kind, result.message)
        self.assertEqual([canonical], resolver_args)
        self.assertEqual([canonical], materializer_roots)
        self.assertEqual([canonical], publisher_roots)
        canonical_state = resolve_project_state(canonical, cache_root=self.cache)
        alias_state = resolve_project_state(alias, cache_root=self.cache)
        self.assertEqual(canonical_state.namespace, alias_state.namespace)
        self.assertEqual(
            str(
                canonical_state.generated_root
                / "docker-constructor.build.effective.toml"
            ),
            result.publish_result.published_path,
        )
        self.assertFalse((self.cache / "constructor-project-alias").exists())

    def test_run_projection_is_verified_by_facade_default_lookup_and_alias(self):
        """P3.R2: launch output is the facade's default verification input."""
        from docker.constructor_cli import main
        from docker.versioning import runtime_verification
        from docker.versioning.effective import Filesystem, create_runtime_projection

        alias = self.project.parent / "constructor-project-alias"
        alias.symlink_to(self.project, target_is_directory=True)
        created = []

        def retain_projection(projection, *, parent_dir):
            path = Path(parent_dir) / f"runtime-{uuid.uuid4().hex}.toml"
            handle = create_runtime_projection(
                projection, host_path=str(path), _fs=Filesystem(runtime_root=parent_dir)
            )
            handle.discard()
            created.append(path)
            return handle

        request = RunRequest(
            inventory_path=str(self.project / "docker-constructor.toml"),
            project_root=str(self.project),
            image="test:latest",
            selection=WorkspaceSelection(
                workspace=str(self.primary), extra_workspaces=(str(self.extra),)
            ),
            pi_home_host=str(self.installation / "pi-home"),
            _constructor_cache_root=str(self.cache),
            inspector=_Inspector(),
            executor=_RunExecutor(),
            _create_projection=retain_projection,
        )
        with patch(
            "docker.launcher.artifact_cache.materialize_selected_artifacts",
            return_value={},
        ):
            launch = orchestrate_run(request)
        self.assertEqual(ExitKind.SUCCESS, launch.exit_kind, launch.message)
        projection = created.pop()
        state = resolve_project_state(self.project, cache_root=self.cache)
        self.assertTrue(projection.is_relative_to(state.runtime_root))
        captured = []
        real_verify = runtime_verification.verify_runtime

        def record_and_verify(verify_request):
            captured.append(verify_request.runtime_projection_path)
            return real_verify(verify_request)

        try:
            with (
                patch(
                    "docker.constructor_cli._discover_runtime_projection_from_container",
                    return_value=None,
                ),
                patch(
                    "docker.versioning.runtime_verification.verify_runtime",
                    side_effect=record_and_verify,
                ),
            ):
                main(
                    [
                        "--project-directory",
                        str(alias),
                        "verify",
                        "--scope",
                        "runtime",
                        "--container",
                        "pi-test",
                        "--workspace",
                        str(self.primary),
                    ],
                    _process_runner=_VerifyRunner(),
                )
            self.assertEqual([projection], captured)
            self.assertEqual(
                state.namespace,
                resolve_project_state(alias, cache_root=self.cache).namespace,
            )
        finally:
            projection.unlink(missing_ok=True)

    def test_foreign_projects_and_workspaces_remain_namespace_neutral(self):
        """P3.R6: only canonical constructor identity reaches both resolvers."""
        before = {
            root: _tree_snapshot(root)
            for root in (self.installation, self.project, self.primary, self.extra)
        }
        build_identities, run_identities = [], []
        import docker.launcher as launcher
        from docker.versioning import build_orchestration

        real_build_resolve = build_orchestration.resolve_project_state
        real_run_resolve = launcher.resolve_project_state

        def record_build(project, *args, **kwargs):
            build_identities.append(Path(project).resolve())
            return real_build_resolve(project, *args, **kwargs)

        def record_run(project, *args, **kwargs):
            run_identities.append(Path(project).resolve())
            return real_run_resolve(project, *args, **kwargs)

        with (
            patch(
                "docker.versioning.build_orchestration.resolve_project_state",
                side_effect=record_build,
            ),
            patch("docker.launcher.resolve_project_state", side_effect=record_run),
        ):
            build = self._build()
            with patch(
                "docker.launcher.artifact_cache.materialize_selected_artifacts",
                return_value={},
            ):
                run = orchestrate_run(
                    RunRequest(
                        inventory_path=str(self.project / "docker-constructor.toml"),
                        project_root=str(self.project),
                        image="test:latest",
                        selection=WorkspaceSelection(
                            workspace=str(self.primary),
                            extra_workspaces=(str(self.extra),),
                        ),
                        pi_home_host=str(self.installation / "pi-home"),
                        _constructor_cache_root=str(self.cache),
                        inspector=_Inspector(),
                        executor=_RunExecutor(),
                    )
                )
        self.assertEqual(ExitKind.SUCCESS, build.exit_kind, build.message)
        self.assertEqual(ExitKind.SUCCESS, run.exit_kind, run.message)
        identities = build_identities + run_identities
        self.assertIn(self.project.resolve(), identities)
        for foreign in (self.installation, self.primary, self.extra):
            self.assertNotIn(foreign.resolve(), identities)
        self.assertTrue(
            resolve_project_state(
                self.project, cache_root=self.cache
            ).generated_root.is_dir()
        )
        for root, tree in before.items():
            self.assertEqual(
                tree, _tree_snapshot(root), f"foreign tree changed: {root}"
            )
        forbidden = (
            ".docker-generated",
            "namespace",
            "projection",
            "evidence",
            "lock",
            "marker",
            "manifest",
            "temporary",
        )
        for root in (self.installation, self.primary, self.extra):
            self.assertFalse(
                any(
                    any(word in part.name.lower() for word in forbidden)
                    for part in root.rglob("*")
                ),
                root,
            )

    def test_default_evidence_isolated_from_foreign_projects_and_workspaces(self):
        """P3.R6: script defaults evidence to the selected project's namespace."""
        # A schema-only inventory makes the child launch fail before network
        # acquisition; the collector itself still completes deterministic setup.
        (self.project / "docker-constructor.toml").write_text("schema = 1\n")
        before = {
            root: _tree_snapshot(root)
            for root in (
                self.installation,
                self.project,
                self.primary,
                self.extra,
            )
        }
        probes = Path(self.tmp.name) / "command-probes"
        probes.mkdir()
        ps_count = probes / "ps-count"
        docker = probes / "docker"
        docker.write_text(f"""#!/bin/sh
if [ "$1" = ps ]; then
  count=0
  [ -f {ps_count!s} ] && count=$(cat {ps_count!s})
  count=$((count + 1)); printf '%s' "$count" > {ps_count!s}
  [ "$count" -gt 1 ] && printf 'evidence-container\\n'
fi
exit 0
""")
        docker.chmod(0o755)
        env = os.environ.copy()
        script_home = Path(self.tmp.name) / "script-home"
        script_xdg_cache = Path(self.tmp.name) / "script-xdg-cache"
        script_home.mkdir()
        script_xdg_cache.mkdir()
        env.update(
            {
                "HOME": str(script_home),
                "XDG_CACHE_HOME": str(script_xdg_cache),
                "PATH": str(probes) + os.pathsep + env["PATH"],
                "PYTHONPATH": str(Path(__file__).parents[1]),
            }
        )
        result = subprocess.run(
            [
                str(
                    Path(__file__).parents[1]
                    / "docker/collect-runtime-artifact-evidence.sh"
                ),
                "--project-directory",
                str(self.project),
                "--workspace",
                str(self.primary),
                "--duration",
                "1",
            ],
            cwd=self.installation,
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
        state = resolve_project_state(self.project.resolve(), cache_root=self.cache)
        evidence = list(state.evidence_root.glob("runtime-artifacts-*"))
        self.assertEqual(1, len(evidence), result.stdout + result.stderr)
        self.assertTrue(evidence[0].is_relative_to(state.evidence_root))
        self.assertEqual(
            state.namespace,
            resolve_project_state(self.project, cache_root=self.cache).namespace,
        )
        for root, snapshot in before.items():
            self.assertEqual(
                snapshot, _tree_snapshot(root), f"foreign tree changed: {root}"
            )
        forbidden = (
            ".docker-generated",
            "projection",
            "evidence",
            "namespace",
            "lock",
            "marker",
            "manifest",
            "temporary",
        )
        for root in (self.installation, self.project, self.primary, self.extra):
            self.assertFalse(
                any(
                    any(word in path.name.lower() for word in forbidden)
                    for path in root.rglob("*")
                ),
                root,
            )

    def test_explicit_runtime_projection_is_parsed_without_default_lookup(self):
        """P3.R7: caller path is parsed by real verification, not redirected."""
        from docker.constructor_cli import main
        from docker.versioning import runtime_verification
        from docker.versioning.effective import (
            EffectiveRuntimeProjection,
            Filesystem,
            create_runtime_projection,
        )

        state = resolve_project_state(self.project, cache_root=self.cache)
        before_project, before_namespace = (
            _tree_snapshot(self.project),
            _tree_snapshot(state.namespace),
        )
        external = Path(self.tmp.name) / "caller-provided-runtime.toml"
        handle = create_runtime_projection(
            EffectiveRuntimeProjection(extensions={}),
            host_path=str(external),
            _fs=Filesystem(runtime_root=str(external.parent)),
        )
        handle.discard()
        read_paths, captured = [], []
        real_read = Path.read_bytes
        real_verify = runtime_verification.verify_runtime

        def record_read(path):
            read_paths.append(path)
            return real_read(path)

        def record_and_verify(verify_request):
            captured.append(verify_request.runtime_projection_path)
            return real_verify(verify_request)

        try:
            with (
                patch(
                    "docker.constructor_cli._discover_runtime_projection_from_container",
                    side_effect=AssertionError("mount discovery called"),
                ),
                patch(
                    "docker.versioning.project_state.resolve_project_state",
                    side_effect=AssertionError("default lookup called"),
                ),
                patch(
                    "pathlib.Path.read_bytes", autospec=True, side_effect=record_read
                ),
                patch(
                    "docker.versioning.runtime_verification.verify_runtime",
                    side_effect=record_and_verify,
                ),
            ):
                main(
                    [
                        "--project-directory",
                        str(self.project),
                        "verify",
                        "--scope",
                        "runtime",
                        "--container",
                        "pi-test",
                        "--workspace",
                        str(self.primary),
                        "--runtime-projection",
                        str(external),
                    ],
                    _process_runner=_VerifyRunner(),
                )
            self.assertEqual([external], captured)
            self.assertIn(external, read_paths)
        finally:
            external.unlink(missing_ok=True)
        self.assertEqual(before_project, _tree_snapshot(self.project))
        self.assertEqual(before_namespace, _tree_snapshot(state.namespace))


if __name__ == "__main__":
    unittest.main()
