from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from docker import constructor_cli
from docker.versioning.constructor_project import (
    ConstructorProject,
    resolve_constructor_project,
)


class TestConstructorProjectResolver(unittest.TestCase):
    def test_defaults_to_physical_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.assertEqual(
                root.resolve(), resolve_constructor_project(None, cwd=root).root
            )

    def test_resolves_relative_selection_from_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            cwd = Path(raw)
            selected = cwd / "child"
            selected.mkdir()
            self.assertEqual(
                selected.resolve(), resolve_constructor_project("child", cwd=cwd).root
            )

    def test_accepts_absolute_selection(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            selected = Path(raw)
            self.assertEqual(
                selected.resolve(), resolve_constructor_project(selected).root
            )

    def test_normalizes_directory_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            cwd = Path(raw)
            target = cwd / "target"
            target.mkdir()
            link = cwd / "link"
            link.symlink_to(target, target_is_directory=True)
            self.assertEqual(target.resolve(), resolve_constructor_project(link).root)

    def test_rejects_missing_path_without_parent_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            cwd = Path(raw)
            (cwd / "docker-constructor.toml").touch()
            with self.assertRaisesRegex(ValueError, "does not exist"):
                resolve_constructor_project("missing", cwd=cwd)

    def test_rejects_non_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "file"
            path.touch()
            with self.assertRaisesRegex(ValueError, "not a directory"):
                resolve_constructor_project(path)

    def test_does_not_fall_back_to_installation_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            cwd = Path(raw)
            project = resolve_constructor_project(None, cwd=cwd)
            self.assertEqual(cwd.resolve(), project.root)
            self.assertNotEqual(
                Path(constructor_cli.__file__).resolve().parent.parent, project.root
            )


class TestConstructorProjectPaths(unittest.TestCase):
    def test_fixed_paths_share_one_normalized_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            project = resolve_constructor_project(Path(raw))
            root = Path(raw).resolve()
            self.assertEqual(root / "docker-constructor.toml", project.inventory)
            self.assertEqual(
                root / "docker-constructor.local.toml", project.local_config
            )
            self.assertEqual(root / "Dockerfile", project.dockerfile)
            self.assertEqual(root / ".env", project.dotenv)
            self.assertEqual(root / ".docker-local", project.local_inputs)

    def test_value_is_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            project = resolve_constructor_project(Path(raw))
            with self.assertRaises(FrozenInstanceError):
                project.root = Path("/other")  # type: ignore[misc]


class TestConstructorProjectParserAndDispatch(unittest.TestCase):
    def test_accepts_global_project_directory(self) -> None:
        parser = constructor_cli._build_parser()
        args = parser.parse_args(["--project-directory", "/tmp", "show"])
        self.assertEqual("/tmp", args.project_directory)

    def test_rejects_removed_inventory_option(self) -> None:
        parser = constructor_cli._build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["--inventory", "/tmp/custom.toml", "show"])

    def test_resolves_once_and_reuses_value_at_dispatch_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            calls: list[ConstructorProject] = []
            known_project = ConstructorProject(root)

            class Dispatcher:
                def execute(
                    self, command: str, request: constructor_cli.CommandRequest
                ):
                    calls.append(request.constructor_project)
                    os.chdir(Path.home())
                    self.assert_project(request.constructor_project)
                    return constructor_cli.CommandResult(
                        exit_kind=constructor_cli.ExitKind.SUCCESS,
                        data={"ok": True},
                    )

                @staticmethod
                def assert_project(project: ConstructorProject) -> None:
                    if project.root != root or project.inventory.parent != root:
                        raise AssertionError("dispatcher observed split project roots")

            old_cwd = Path.cwd()
            try:
                with patch(
                    "docker.constructor_cli.resolve_constructor_project",
                    return_value=known_project,
                ) as resolver:
                    rc = constructor_cli.main(
                        ["--project-directory", str(root), "show"],
                        dispatcher=Dispatcher(),
                    )
                self.assertEqual(0, rc)
                resolver.assert_called_once_with(str(root))
                self.assertIs(known_project, calls[0])
            finally:
                os.chdir(old_cwd)


if __name__ == "__main__":
    unittest.main()
