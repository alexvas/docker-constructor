from __future__ import annotations

import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from docker import constructor_cli
from docker.versioning.dispatch_types import CommandResult, ExitKind
from docker.versioning.inventory import resolve_local_companion_path

_REPO = Path(__file__).resolve().parent.parent


def _project(*, dockerfile: bool = False) -> tempfile.TemporaryDirectory[str]:
    temp = tempfile.TemporaryDirectory()
    root = Path(temp.name)
    shutil.copyfile(_REPO / "docker-constructor.toml", root / "docker-constructor.toml")
    if dockerfile:
        (root / "Dockerfile").write_text("FROM scratch\n")
    return temp


def _main(argv: list[str], **kwargs: object) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = constructor_cli.main(argv, **kwargs)
    return rc, out.getvalue(), err.getvalue()


class TestFixedProjectInputs(unittest.TestCase):
    def test_readonly_commands_use_explicit_project_inventory(self) -> None:
        with _project() as temp:
            root = Path(temp)
            seen: list[Path] = []

            def dispatch(inventory_path: Path, command: str, **kwargs: object) -> CommandResult:
                seen.append(Path(inventory_path))
                return CommandResult(exit_kind=ExitKind.SUCCESS)

            with patch("docker.versioning.readonly_service.dispatch", dispatch):
                for command in ("validate", "show", "check-updates"):
                    rc, _, _ = _main(["--project-directory", str(root), command])
                    self.assertEqual(0, rc)
            self.assertEqual([root / "docker-constructor.toml"] * 3, seen)

    def test_readonly_command_uses_cwd_inventory_without_dockerfile(self) -> None:
        with _project() as temp:
            root = Path(temp)
            seen: list[Path] = []
            old_cwd = Path.cwd()

            def dispatch(inventory_path: Path, command: str, **kwargs: object) -> CommandResult:
                seen.append(Path(inventory_path))
                return CommandResult(exit_kind=ExitKind.SUCCESS)

            try:
                os.chdir(root)
                with patch("docker.versioning.readonly_service.dispatch", dispatch):
                    rc, _, _ = _main(["validate"])
            finally:
                os.chdir(old_cwd)
            self.assertEqual(0, rc)
            self.assertEqual([root / "docker-constructor.toml"], seen)

    def test_build_uses_selected_context_and_root_dockerfile(self) -> None:
        with _project(dockerfile=True) as temp:
            root = Path(temp).resolve()
            captured: list[object] = []

            def orchestrate(request: object, *, inventory: object, local_inputs: object) -> object:
                from docker.versioning.build_orchestration import BuildResult
                captured.append(request)
                return BuildResult(exit_kind=ExitKind.SUCCESS)

            with patch("docker.versioning.build_orchestration.orchestrate_build", orchestrate):
                rc, _, _ = _main([
                    "--project-directory", str(root), "build", "--dry-run"
                ])
            self.assertEqual(0, rc)
            request = captured[0]
            self.assertEqual(str(root), request.context)
            self.assertEqual(str(root / "Dockerfile"), request.dockerfile)
            self.assertEqual(str(root / "docker-constructor.toml"), request.inventory_path)

    def test_real_build_vector_uses_selected_context_and_dockerfile(self) -> None:
        from dataclasses import replace
        from docker.versioning.build_orchestration import BuildRequest, plan_build
        from docker.versioning.rendering import (
            Materialized, NoDerivedEnvironment, render_build_vector,
        )
        from tests.test_constructor_build_orchestration import FakeBuildExecutor

        with tempfile.TemporaryDirectory() as installed, tempfile.TemporaryDirectory() as context:
            root = _REPO.resolve()
            selected_context = Path(context).resolve()
            dockerfile = selected_context / "Dockerfile"
            dockerfile.write_text("FROM scratch\n")
            plan = plan_build(BuildRequest(
                inventory_path=str(root / "docker-constructor.toml"),
                context=str(selected_context), dockerfile=str(dockerfile),
                project_root=str(root),
            ))
            self.assertEqual(ExitKind.SUCCESS, plan.exit_kind, plan.message)
            inputs = replace(
                plan.render_inputs,
                named_context=Materialized(
                    str(selected_context), NoDerivedEnvironment()
                ),
            )
            vector = render_build_vector(inputs)
            runner = FakeBuildExecutor()
            runner.run(vector)
            self.assertEqual(1, len(runner.calls))
            vector = runner.calls[0]
            file_index = vector.index("--file")
            self.assertEqual(str(dockerfile), vector[file_index + 1])
            self.assertEqual(str(selected_context), vector[-1])
            self.assertNotIn(str(Path(installed).resolve()), vector)

    def test_missing_dockerfile_fails_before_publication_or_execution(self) -> None:
        with _project() as temp:
            root = Path(temp).resolve()
            publication_calls: list[object] = []
            runner_calls: list[object] = []
            with patch(
                "docker.versioning.build_orchestration._publish_projection_default",
                side_effect=lambda *a, **k: publication_calls.append((a, k)),
            ), patch(
                "docker.versioning.build_orchestration.SubprocessBuildExecutor.run",
                side_effect=lambda *a, **k: runner_calls.append((a, k)),
            ):
                rc, _, err = _main([
                    "--project-directory", str(root), "build", "--dry-run"
                ])
            self.assertEqual(3, rc)
            self.assertIn(str(root / "Dockerfile"), err)
            self.assertEqual([], publication_calls)
            self.assertEqual([], runner_calls)

    def test_resolver_always_uses_fixed_companion_basename(self) -> None:
        resolved = resolve_local_companion_path(Path("/project/custom.toml"))
        self.assertEqual(Path("/project/docker-constructor.local.toml"), resolved)
        self.assertNotEqual(Path("/project/custom.local.toml"), resolved)

    def test_doctor_resolves_the_selected_project_fixed_companion(self) -> None:
        with _project() as temp:
            root = Path(temp).resolve()
            fixed = root / "docker-constructor.local.toml"
            fixed.write_text("")
            (root / "custom.local.toml").write_text("not = [")
            resolved: list[Path] = []

            def resolve(inventory_path: Path) -> tuple[str, Path, None]:
                self.assertEqual(root / "docker-constructor.toml", inventory_path)
                resolved.append(fixed)
                return "external-address", fixed, None

            with patch(
                "docker.versioning.build_orchestration._resolve_doctor_host_access", resolve
            ):
                rc, _, _ = _main(["--project-directory", str(root), "doctor"])
            self.assertEqual(0, rc)
            self.assertEqual([fixed], resolved)

    def test_launcher_dotenv_is_selected_project_owned(self) -> None:
        with tempfile.TemporaryDirectory() as selected, tempfile.TemporaryDirectory() as installed:
            selected_root, installed_root = Path(selected), Path(installed)
            (selected_root / ".env").write_text("WORKSPACE_ROOT=/selected/workspaces\n")
            (installed_root / ".env").write_text("WORKSPACE_ROOT=/installed/workspaces\n")
            with patch.object(constructor_cli, "_INSTALLATION_ROOT", installed_root):
                value = constructor_cli._read_env_key(
                    "WORKSPACE_ROOT", selected_root / ".env"
                )
            self.assertEqual("/selected/workspaces", value)

    def test_build_trust_uses_only_selected_project_bundle(self) -> None:
        from dataclasses import replace
        from docker.versioning.build_orchestration import BuildRequest, plan_build
        from docker.versioning.rendering import (
            Materialized, NoDerivedEnvironment, render_build_vector,
        )

        bundle_text = "-----BEGIN CERTIFICATE-----\nQQ==\n-----END CERTIFICATE-----\n"
        with _project(dockerfile=True) as temp, tempfile.TemporaryDirectory() as installed:
            root, install_root = Path(temp).resolve(), Path(installed).resolve()
            (root / "docker-constructor.local.toml").write_text(
                "[corporate-trust]\nenabled = true\n"
            )
            (root / "custom.local.toml").write_text("not = [")
            for owner, marker in ((root, bundle_text), (install_root, bundle_text.replace("QQ==", "Qg=="))):
                (owner / ".docker-local").mkdir()
                (owner / ".docker-local/corporate-ca-bundle.crt").write_text(marker)
            plan = plan_build(BuildRequest(
                inventory_path=str(root / "docker-constructor.toml"),
                context=str(root), dockerfile=str(root / "Dockerfile"),
                project_root=str(root), repo_root=str(install_root),
                dry_run=True,
            ))
            self.assertEqual(ExitKind.SUCCESS, plan.exit_kind, plan.message)
            self.assertIsNotNone(plan.render_inputs)
            inputs = replace(
                plan.render_inputs,
                named_context=Materialized(str(root), NoDerivedEnvironment()),
            )
            rendered = " ".join(render_build_vector(inputs))
            self.assertEqual(
                root / ".docker-local/corporate-ca-bundle.crt",
                plan.host_network_policy.ca_bundle,
            )
            self.assertIn("CORPORATE_TRUST_ENABLED=true", rendered)
            self.assertNotIn(
                str(install_root / ".docker-local/corporate-ca-bundle.crt"), rendered
            )

    def test_run_trust_mount_uses_only_selected_project_bundle(self) -> None:
        from docker.launcher import WorkspaceSelection, RunRequest, orchestrate_run
        from tests.test_constructor_corporate_network_run_red import (
            _bomb_artifact, _bomb_executor, _bomb_inspector, _bomb_projection,
            _collect_mounts,
        )

        bundle_text = "-----BEGIN CERTIFICATE-----\nQQ==\n-----END CERTIFICATE-----\n"
        with _project() as temp, tempfile.TemporaryDirectory() as installed:
            root, install_root = Path(temp).resolve(), Path(installed).resolve()
            (root / "docker-constructor.local.toml").write_text(
                "[corporate-trust]\nenabled = true\n"
            )
            (root / "custom.local.toml").write_text("not = [")
            for owner, payload in ((root, bundle_text), (install_root, bundle_text.replace("QQ==", "Qg=="))):
                (owner / ".docker-local").mkdir()
                (owner / ".docker-local/corporate-ca-bundle.crt").write_text(payload)
            effects: list[str] = []
            result = orchestrate_run(RunRequest(
                inventory_path=str(root / "docker-constructor.toml"),
                image="pi-cli-pi:latest",
                selection=WorkspaceSelection(workspace="/work/project"),
                pi_home_host="/home/user/.pi", project_root=str(root),
                repo_root=str(install_root), dry_run=True,
                executor=_bomb_executor(effects), inspector=_bomb_inspector(effects),
                _create_projection=_bomb_projection(effects),
                _artifact_fetcher=_bomb_artifact(effects),
            ))
            self.assertEqual(ExitKind.SUCCESS, result.exit_kind, result.message)
            mount_text = " ".join(str(m) for m in _collect_mounts(result.run_args))
            self.assertIn(str(root / ".docker-local/corporate-ca-bundle.crt"), mount_text)
            self.assertNotIn(str(install_root / ".docker-local/corporate-ca-bundle.crt"), mount_text)

    def test_verify_trust_uses_selected_root_and_rejects_installation_fallback(self) -> None:
        bundle_text = "-----BEGIN CERTIFICATE-----\nQQ==\n-----END CERTIFICATE-----\n"
        with _project() as temp, tempfile.TemporaryDirectory() as installed:
            root, install_root = Path(temp).resolve(), Path(installed).resolve()
            (root / "docker-constructor.local.toml").write_text(
                "[corporate-trust]\nenabled = true\n"
            )
            for owner in (root, install_root):
                (owner / ".docker-local").mkdir()
                (owner / ".docker-local/corporate-ca-bundle.crt").write_text(bundle_text)
            seen: list[Path] = []
            original = constructor_cli._resolve_verify_corporate_network

            class Runner:
                def run(self, argv: object, **kwargs: object):
                    from docker.launcher import ProcessResult
                    return ProcessResult(
                        argv=tuple(argv), return_code=1, stdout="", stderr="unavailable"
                    )

            runner = Runner()

            def resolve(inv_path: str, project_root: Path, **_kwargs: object):
                seen.append(project_root)
                return original(inv_path, project_root)

            with patch.object(constructor_cli, "_INSTALLATION_ROOT", install_root), patch.object(
                constructor_cli, "_resolve_verify_corporate_network", resolve
            ):
                _main([
                    "--project-directory", str(root), "verify", "--scope", "runtime",
                    "--runtime-projection", str(root / "missing-runtime.toml"),
                    "--container", "none",
                ], _process_runner=runner)
            self.assertEqual([root], seen)

            (root / ".docker-local/corporate-ca-bundle.crt").unlink()
            with patch.object(constructor_cli, "_INSTALLATION_ROOT", install_root):
                rc, _, err = _main([
                    "--project-directory", str(root), "verify", "--scope", "runtime",
                    "--runtime-projection", str(root / "missing-runtime.toml"),
                    "--container", "none",
                ], _process_runner=runner)
            self.assertEqual(3, rc)
            self.assertIn(str(root / ".docker-local/corporate-ca-bundle.crt"), err)
            self.assertNotIn(str(install_root / ".docker-local/corporate-ca-bundle.crt"), err)

    def test_build_threads_selected_root_for_local_and_trust_inputs(self) -> None:
        with _project(dockerfile=True) as temp, tempfile.TemporaryDirectory() as installed:
            root = Path(temp).resolve()
            captured: list[object] = []

            def orchestrate(request: object, *, inventory: object, local_inputs: object) -> object:
                from docker.versioning.build_orchestration import BuildResult
                captured.append(request)
                return BuildResult(exit_kind=ExitKind.SUCCESS)

            with patch.object(constructor_cli, "_INSTALLATION_ROOT", Path(installed)), patch(
                "docker.versioning.build_orchestration.orchestrate_build", orchestrate
            ):
                rc, _, _ = _main([
                    "--project-directory", str(root), "build", "--dry-run"
                ])
            self.assertEqual(0, rc)
            self.assertEqual(str(root), captured[0].project_root)
            self.assertNotEqual(str(Path(installed)), captured[0].project_root)


if __name__ == "__main__":
    unittest.main()
