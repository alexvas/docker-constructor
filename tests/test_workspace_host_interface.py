"""Phase 4 workspace-oriented host-interface contracts."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from docker.constructor_cli import _build_parser, _read_env_key
from docker.launcher import (
    NoWorkspaceError,
    WorkspaceSelection,
    resolve_workspace_selection,
)
from docker.versioning.rendering import RunRenderInputs


class WorkspaceCliTests(unittest.TestCase):
    def test_workspace_options_and_removed_aliases(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            [
                "run",
                "-w",
                "/work/main",
                "--extra-workspace",
                "/work/extra",
                "--extra-workspace",
                "/work/other",
                "--workspace-root",
                "/work",
            ]
        )
        self.assertEqual(args.workspace, "/work/main")
        self.assertEqual(args.extra_workspaces, ["/work/extra", "/work/other"])
        self.assertEqual(args.workspace_root, "/work")
        for removed in ("--main-project", "-m", "--project", "--base-project-dir"):
            with self.assertRaises(SystemExit):
                parser.parse_args(["run", removed, "/work/main"])


class WorkspaceSelectionTests(unittest.TestCase):
    def test_order_duplicates_symlinks_and_tui_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "target"
            target.mkdir()
            link = root / "link"
            link.symlink_to(target, target_is_directory=True)
            original_cwd = os.getcwd()
            os.chdir(root)
            try:
                relative_selection = resolve_workspace_selection(
                    workspace=".",
                    extra_workspaces=("target",),
                )
            finally:
                os.chdir(original_cwd)
            self.assertEqual(relative_selection.workspace, str(root))
            self.assertEqual(relative_selection.extra_workspaces, (str(target),))

            selection = resolve_workspace_selection(
                workspace=str(link),
                extra_workspaces=(str(target),),
            )
            self.assertEqual(selection.workspace, os.path.abspath(link))
            self.assertEqual(selection.extra_workspaces, (os.path.abspath(target),))
            with self.assertRaises(ValueError):
                resolve_workspace_selection(
                    workspace=str(target),
                    extra_workspaces=(str(target),),
                )

        class Selector:
            def select(self) -> WorkspaceSelection:
                return WorkspaceSelection("/tui", ("/tui-extra",))

        selected = resolve_workspace_selection(
            workspace="/cli",
            extra_workspaces=("/extra",),
            tui=True,
            selector=Selector(),
        )
        self.assertEqual(selected, WorkspaceSelection("/cli", ("/extra",)))
        with self.assertRaises(NoWorkspaceError):
            resolve_workspace_selection()


class WorkspaceDotenvTests(unittest.TestCase):
    def test_workspace_root_precedence_and_home_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            dotenv = Path(td) / ".env"
            dotenv.write_text("WORKSPACE_ROOT=/from-env\nBASE_PROJECT_DIR=/old\n")
            self.assertEqual(_read_env_key("WORKSPACE_ROOT", dotenv), "/from-env")
            # The dispatcher reads WORKSPACE_ROOT only from the selected
            # constructor project's project-local dotenv; BASE_PROJECT_DIR
            # is not an active fallback.


class WorkspaceTuiTests(unittest.TestCase):
    def test_selection_status_uses_primary_and_extra_labels(self) -> None:
        from docker.tui import _selection_status_label

        label = _selection_status_label("app", 2)
        self.assertIn("Enter:primary", label)
        self.assertIn("primary: app", label)
        self.assertIn("extra: 2", label)
        self.assertNotIn("Enter:main", label)
        self.assertNotIn("main:", label)
        self.assertNotIn("additional", label)

    def test_run_tui_selects_primary_and_extra_and_scrolls_both_directions(
        self,
    ) -> None:
        import docker.tui
        from docker.tui import TreeNode, run_tui

        class FakeScreen:
            def __init__(self) -> None:
                self._keys = [
                    docker.tui.curses.KEY_DOWN,
                    ord(" "),
                    *[docker.tui.curses.KEY_DOWN] * 6,
                    *[docker.tui.curses.KEY_UP] * 5,
                    docker.tui.curses.KEY_F5,
                ]
                self._lines: list[str] = []
                self.frames: list[tuple[str, ...]] = []

            def getmaxyx(self) -> tuple[int, int]:
                return (7, 80)  # four visible list rows

            def erase(self) -> None:
                self._lines = []

            def clear(self) -> None:
                self._lines = []

            def addstr(self, _row: int, _col: int, text: str, *_attrs: object) -> None:
                self._lines.append(text)

            def refresh(self) -> None:
                self.frames.append(tuple(self._lines))

            def getch(self) -> int:
                return self._keys.pop(0)

        screen = FakeScreen()
        roots = [TreeNode(Path(f"/work/node-{i}"), f"node-{i}") for i in range(8)]
        with (
            patch.object(
                docker.tui.curses, "wrapper", side_effect=lambda draw: draw(screen)
            ),
            patch.object(docker.tui.curses, "curs_set"),
            patch.object(docker.tui.curses, "start_color"),
            patch.object(docker.tui.curses, "use_default_colors"),
            patch.object(docker.tui.curses, "init_pair"),
            patch.object(docker.tui.curses, "color_pair", return_value=0),
        ):
            primary, extras = run_tui([], roots)

        self.assertEqual(primary.node.path, Path("/work/node-0"))
        self.assertEqual([item.node.path for item in extras], [Path("/work/node-1")])
        rendered_frames = ["\n".join(frame) for frame in screen.frames]
        self.assertTrue(any("node-6/" in frame for frame in rendered_frames))
        self.assertTrue(any("node-2/" in frame for frame in rendered_frames[7:]))

    def test_tui_selector_returns_primary_and_extra_workspaces(self) -> None:
        import docker.tui
        from docker.constructor_cli import _tui_workspace_selector
        from docker.tui import FlatItem, TreeNode

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            primary = FlatItem(
                "tree", "primary", node=TreeNode(root / "primary", "primary")
            )
            extra = FlatItem("tree", "extra", node=TreeNode(root / "extra", "extra"))
            with (
                patch.object(
                    docker.tui, "build_tree", return_value=[primary.node, extra.node]
                ),
                patch.object(docker.tui, "run_tui", return_value=(primary, [extra])),
            ):
                selection = _tui_workspace_selector(str(root)).select()
        self.assertEqual(
            selection, WorkspaceSelection(str(root / "primary"), (str(root / "extra"),))
        )

    def test_tui_selector_lexically_absolutizes_relative_tree_paths(self) -> None:
        import docker.tui
        from docker.constructor_cli import _tui_workspace_selector
        from docker.tui import FlatItem, TreeNode

        primary = FlatItem("tree", "primary", node=TreeNode(Path("link"), "primary"))
        extra = FlatItem("tree", "extra", node=TreeNode(Path("extra"), "extra"))
        with (
            patch.object(
                docker.tui, "build_tree", return_value=[primary.node, extra.node]
            ),
            patch.object(docker.tui, "run_tui", return_value=(primary, [extra])),
        ):
            selection = _tui_workspace_selector(".").select()

        self.assertEqual(selection.workspace, os.path.abspath("link"))
        self.assertEqual(selection.extra_workspaces, (os.path.abspath("extra"),))


class WorkspaceDtoTests(unittest.TestCase):
    def test_render_inputs_expose_workspace_fields(self) -> None:
        inputs = RunRenderInputs(
            image="image",
            container_name="pi-1",
            projection_host_path="/tmp/p",
            projection_container_path="/run/pi-cli/docker-constructor.runtime.toml",
            pi_home_host="/tmp/pi",
            workspace="/work/main",
            extra_workspaces=("/work/extra",),
        )
        self.assertEqual(inputs.workspace, "/work/main")
        self.assertEqual(inputs.extra_workspaces, ("/work/extra",))


if __name__ == "__main__":
    unittest.main()
