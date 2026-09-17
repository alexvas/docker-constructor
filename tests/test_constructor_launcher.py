"""RED — Workspace Launcher Tests (Stage 11.1).

Failing tests for workspace selection, pi-N allocation, and
launch-vector assembly.  All tests use injected fakes — no Docker
daemon, subprocess, filesystem, or network access.

Design constraints:
  - ``--workspace PATH`` selects the primary workspace explicitly.
  - ``--extra-workspace PATH`` (repeatable) adds an extra workspace.
  - ``--tui`` invokes interactive workspace selection.
  - Workspace-selection precedence:
    1. Explicit ``--workspace``
    2. TUI selection (when ``--tui`` is requested)
    3. Automatic selection (if retained)
    4. Otherwise: actionable ``NoWorkspaceError``
  - The first ``--extra-workspace`` is never silently promoted to primary workspace.
  - pi-N allocation uses ``docker ps -a`` inspection, best-effort.
  - The launch vector assembles into :class:`RunRenderInputs` and
    delegates to :func:`render_run_vector`.
"""

from __future__ import annotations

import os
from pathlib import Path
import unittest

from docker.launcher import (
    ContainerInspectError,
    ContainerNameInspector,
    DockerContainerInspector,
    DockerRunExecutor,
    ExecutionMode,
    NoWorkspaceError,
    ProcessResult,
    ProcessRunner,
    WorkspaceSelection,
    WorkspaceSelector,
    ProjectionFactory,
    RunExecutor,
    RunRequest,
    RunResult,
)
from docker.versioning.rendering import RunRenderInputs, render_run_vector
from docker.versioning.dispatch_types import ExitKind

# ═══════════════════════════════════════════════════════════════════
# Shared test doubles
# ═══════════════════════════════════════════════════════════════════


# ── repo-cache leak guard ───────────────────────────────────────

_REPO_CACHE = os.path.join(os.path.dirname(__file__), "..", ".docker-generated", "runtime-artifacts")


def _repo_cache_file_set() -> frozenset[str]:
    """Return an immutable set of every regular file under the repository
    cache tree, relative to the repo root."""
    try:
        files: set[str] = set()
        for dirpath, _dirnames, filenames in os.walk(_REPO_CACHE):
            for name in filenames:
                files.add(os.path.relpath(os.path.join(dirpath, name)))
        return frozenset(files)
    except FileNotFoundError:
        return frozenset()


def _assert_repo_cache_unchanged(before: frozenset[str]) -> frozenset[str]:
    """Assert the repository cache has the exact same files as *before*.
    Returns the current set for chaining (e.g. snapshot for next check)."""
    after = _repo_cache_file_set()
    assert after == before, (
        f"repo cache leaked: {sorted(after - before)} added, "
        f"{sorted(before - after)} removed"
    )
    return after


# ────────────────────────────────────────────────────────────────



# ── shared-lock-directory leak guard ────────────────────────────────
# Tests must NEVER create /tmp/locks.  FileIdentityLockFactory
# derives its lock root from the cache-root parent, so every test
# must place its cache root beneath a private temporary directory.
# Pre-existing /tmp/locks from outside callers is tolerated — the
# guard only flags a *new* creation.  Tests do not own or delete it.

_SHARED_LOCK_LEAK = "/tmp/locks"


def _shared_lock_leak_snapshot() -> bool:
    """Return ``True`` when the shared lock directory already exists."""
    return os.path.exists(_SHARED_LOCK_LEAK)


def _assert_no_shared_lock_created(before: bool, *, _path: str = _SHARED_LOCK_LEAK) -> None:
    """Fail when the shared lock directory was *created* during a test.

    ``before`` is the snapshot taken in ``setUp``.  If ``_path`` did not
    exist before but exists now, the test leaked it."""
    if before:
        return
    assert not os.path.exists(_path), (
        f"{_path} was created outside the test root — "
        "a test passed FileIdentityLockFactory a cache_root whose "
        "parent is not confined to a per-test temporary directory"
    )



class FakeProcessRunner(ProcessRunner):
    """Deterministic :class:`ProcessRunner` that consumes canned
    :class:`ProcessResult` responses in FIFO order.  Falls back to
    a non-zero result when no responses remain."""

    def __init__(self, responses: list[ProcessResult] | None = None) -> None:
        self._responses: list[ProcessResult] = list(responses or [])
        self.calls: list[list[str]] = []

    def run(self, argv: list[str], *,
            mode: ExecutionMode = ExecutionMode.CAPTURED) -> ProcessResult:
        self.calls.append(argv)
        if self._responses:
            return self._responses.pop(0)
        return ProcessResult(
            argv=tuple(argv),
            return_code=1,
            stdout="",
            stderr=f"no canned response for {argv[0]}",
        )

    def add(self, result: ProcessResult) -> None:
        self._responses.append(result)


# ═══════════════════════════════════════════════════════════════════
# 11.1.1 - Workspace selection from CLI flags
# ═══════════════════════════════════════════════════════════════════


class TestWorkspaceSelectionFromArgs(unittest.TestCase):
    """Workspace selection contract: the CLI parser MUST support the
    full flag surface described in the Phase 11 plan, and workspace
    resolution MUST produce a :class:`WorkspaceSelection` that
    observes the documented precedence rules."""

    # ── helpers ──────────────────────────────────────────────────

    @staticmethod
    def _select(*, selector: WorkspaceSelector | None = None, **flags) -> WorkspaceSelection:
        """Simulate resolving workspace selection from parsed CLI flags.

        Mapped to the future ``resolve_workspace_selection()`` in
        ``docker/launcher.py``.
        """
        from docker.launcher import resolve_workspace_selection
        return resolve_workspace_selection(
            workspace=flags.get("workspace"),
            extra_workspaces=flags.get("extra_workspaces", ()),
            tui=flags.get("tui", False),
            workspace_root=flags.get("workspace_root"),
            selector=selector,
        )

    # ── explicit primary workspace ────────────────────────────────────

    def test_explicit_primary_workspace_without_extra_workspaces(self) -> None:
        sel = self._select(workspace="/work/p1")
        self.assertEqual(sel.workspace, "/work/p1")
        self.assertEqual(sel.extra_workspaces, ())

    def test_explicit_primary_workspace_with_one_extra_workspace(self) -> None:
        sel = self._select(
            workspace="/work/p1", extra_workspaces=["/work/p2"],
        )
        self.assertEqual(sel.workspace, "/work/p1")
        self.assertEqual(sel.extra_workspaces, ("/work/p2",))

    def test_explicit_primary_workspace_with_two_extra_workspaces(self) -> None:
        sel = self._select(
            workspace="/work/p1",
            extra_workspaces=["/work/p2", "/work/p3"],
        )
        self.assertEqual(sel.workspace, "/work/p1")
        self.assertEqual(sel.extra_workspaces, ("/work/p2", "/work/p3"))

    def test_explicit_primary_workspace_with_many_extra_workspaces(self) -> None:
        sel = self._select(
            workspace="/work/primary",
            extra_workspaces=[f"/work/p{i}" for i in range(5)],
        )
        self.assertEqual(sel.workspace, "/work/primary")
        self.assertEqual(len(sel.extra_workspaces), 5)

    # ── no primary workspace → error ──────────────────────────────────

    def test_no_workspace_raises(self) -> None:
        with self.assertRaises(NoWorkspaceError):
            self._select()

    def test_no_workspace_only_optional_raises(self) -> None:
        """The first --extra-workspace is never silently treated as primary workspace."""
        with self.assertRaises(NoWorkspaceError):
            self._select(extra_workspaces=["/work/p1"])

    def test_no_workspace_multiple_optional_raises(self) -> None:
        with self.assertRaises(NoWorkspaceError):
            self._select(extra_workspaces=["/work/p1", "/work/p2"])

    # ── stable extra-workspace ordering ─────────────────────────────────

    def test_extra_workspaces_preserve_insertion_order(self) -> None:
        sel = self._select(
            workspace="/work/primary",
            extra_workspaces=["/work/z", "/work/a", "/work/m"],
        )
        self.assertEqual(
            sel.extra_workspaces,
            ("/work/z", "/work/a", "/work/m"),
        )

    # ── duplicate rejection ──────────────────────────────────────

    def test_primary_workspace_equal_to_extra_workspace_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._select(
                workspace="/work/p1", extra_workspaces=["/work/p1"],
            )

    def test_duplicate_extra_workspace_paths_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._select(
                workspace="/work/p1",
                extra_workspaces=["/work/p2", "/work/p2"],
            )

    # ── path validation ──────────────────────────────────────────

    def test_relative_workspace_is_normalized_to_absolute(self) -> None:
        selection = self._select(workspace="relative/path")
        self.assertEqual(
            selection.workspace, os.path.abspath("relative/path"),
        )

    def test_relative_extra_workspace_is_normalized_to_absolute(self) -> None:
        selection = self._select(
            workspace="/work/p1",
            extra_workspaces=["relative/path"],
        )
        self.assertEqual(
            selection.extra_workspaces,
            (os.path.abspath("relative/path"),),
        )

    def test_empty_workspace_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._select(workspace="")

    def test_empty_extra_workspace_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._select(
                workspace="/work/p1", extra_workspaces=[""],
            )

    # ── normalized-path duplicate detection ──────────────────────

    def test_normalized_duplicate_primary_and_extra_workspace_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._select(
                workspace="/work/a",
                extra_workspaces=["/work/./a"],
            )

    def test_normalized_duplicate_extra_workspaces_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._select(
                workspace="/work/primary",
                extra_workspaces=["/work/a", "/work/./a"],
            )

    def test_normalized_duplicate_deep_traversal_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._select(
                workspace="/work/primary",
                extra_workspaces=["/work/a/b/../c", "/work/a/c"],
            )

    # ── TUI selection (injected WorkspaceSelector boundary) ────────

    def test_tui_selects_workspace(self) -> None:
        """When --tui is requested and the selector returns a
        WorkspaceSelection, that selection becomes the result."""
        sel = self._select(
            tui=True,
            selector=_FakeSelector("/work/tui-primary"),
        )
        self.assertEqual(sel.workspace, "/work/tui-primary")
        self.assertEqual(sel.extra_workspaces, ())

    def test_tui_selects_primary_and_extra_workspaces(self) -> None:
        """The TUI can return extra workspaces alongside the
        primary workspace — they are preserved in the result."""
        sel = self._select(
            tui=True,
            selector=_FakeSelector(
                "/work/tui-primary", ("/work/tui-extra1", "/work/tui-extra2"),
            ),
        )
        self.assertEqual(sel.workspace, "/work/tui-primary")
        self.assertEqual(
            sel.extra_workspaces,
            ("/work/tui-extra1", "/work/tui-extra2"),
        )

    def test_tui_cancellation_returns_error(self) -> None:
        """When --tui is requested but the selector returns None
        (user cancelled), the result is NoWorkspaceError."""
        with self.assertRaises(NoWorkspaceError):
            self._select(tui=True, selector=_FakeSelector.cancelled())

    def test_tui_without_primary_workspace_flag_still_requires_selection(self) -> None:
        """--tui alone with no --workspace must still resolve a
        primary workspace through the TUI; cancellation is an error."""
        with self.assertRaises(NoWorkspaceError):
            self._select(
                tui=True, extra_workspaces=["/work/p1"],
                selector=_FakeSelector.cancelled(),
            )

    # ── TUI + explicit primary workspace still works ──────────────────────────

    def test_tui_with_explicit_workspace(self) -> None:
        """Explicit --workspace takes precedence over --tui.
        The selector is not consulted."""
        sel = self._select(
            workspace="/work/primary", tui=True,
            selector=_FakeSelector("/work/should-not-be-used"),
        )
        self.assertEqual(sel.workspace, "/work/primary")

    def test_explicit_primary_workspace_ignores_tui_cancellation(self) -> None:
        """When --workspace is given, even a cancelled TUI must
        not prevent the selection — explicit beats TUI."""
        sel = self._select(
            workspace="/work/primary", tui=True,
            selector=_FakeSelector.cancelled(),
        )
        self.assertEqual(sel.workspace, "/work/primary")

    # ── malformed TUI results ───────────────────────────────────

    def test_tui_result_primary_equal_to_extra_workspace_rejected(self) -> None:
        """The resolver validates the selector's result — a
        returned workspace that duplicates an extra workspace must
        be rejected, not blindly trusted."""
        with self.assertRaises(ValueError):
            self._select(
                tui=True,
                selector=_FakeSelector.from_raw("/work/p1", ("/work/p1",)),
            )

    def test_tui_result_duplicate_optional_paths_rejected(self) -> None:
        """The resolver must reject a selector result where two
        extra workspaces are the same path."""
        with self.assertRaises(ValueError):
            self._select(
                tui=True,
                selector=_FakeSelector.from_raw(
                    "/work/primary", ("/work/extra", "/work/extra"),
                ),
            )

    def test_tui_result_normalized_duplicate_rejected(self) -> None:
        """The resolver must detect duplicates after normalization,
        e.g. /work/a and /work/./a."""
        with self.assertRaises(ValueError):
            self._select(
                tui=True,
                selector=_FakeSelector.from_raw(
                    "/work/primary", ("/work/a", "/work/./a"),
                ),
            )

    def test_tui_result_relative_workspace_is_normalized_to_absolute(self) -> None:
        """A selector result receives the same lexical absolute
        normalization as CLI workspace paths."""
        selection = self._select(
            tui=True,
            selector=_FakeSelector.from_raw("relative/path", ()),
        )
        self.assertEqual(
            selection.workspace, os.path.abspath("relative/path"),
        )

    def test_tui_result_empty_workspace_rejected(self) -> None:
        """A selector that returns an empty workspace must be
        rejected."""
        with self.assertRaises(ValueError):
            self._select(
                tui=True,
                selector=_FakeSelector.from_raw("", ()),
            )


# ═══════════════════════════════════════════════════════════════════
# 11.1.3 - pi-N allocation
# ═══════════════════════════════════════════════════════════════════


class _FakeSelector:
    """Fake :class:`WorkspaceSelector` for deterministic tests."""

    def __init__(
        self,
        primary_workspace: str,
        extra_workspaces: tuple[str, ...] = (),
    ) -> None:
        self._primary_workspace = primary_workspace
        self._extra_workspaces = extra_workspaces

    @classmethod
    def cancelled(cls) -> "_FakeSelector":
        """Return a selector that simulates user cancellation."""
        inst = cls.__new__(cls)
        inst._primary_workspace = ""  # signals cancelled
        inst._extra_workspaces = ()
        return inst

    @classmethod
    def from_raw(
        cls, primary_workspace: str, extra_workspaces: tuple[str, ...],
    ) -> "_FakeSelector":
        """Return a selector that returns a pre-built
        :class:`WorkspaceSelection` with potentially invalid fields.

        Uses ``object.__setattr__`` to bypass ``__post_init__``
        validation so the resolver's own validation can be tested.
        """
        sel = WorkspaceSelection.__new__(WorkspaceSelection)
        object.__setattr__(sel, "workspace", primary_workspace)
        object.__setattr__(sel, "extra_workspaces", extra_workspaces)
        inst = cls.__new__(cls)
        inst._result = sel
        return inst

    def select(self) -> WorkspaceSelection | None:
        if hasattr(self, "_result"):
            return self._result  # from_raw
        if not self._primary_workspace:
            return None
        return WorkspaceSelection(
            workspace=self._primary_workspace,
            extra_workspaces=self._extra_workspaces,
        )


class FakeContainerNameInspector:
    """Fake :class:`ContainerNameInspector` for deterministic tests."""

    def __init__(self, names: set[str] | None = None,
                 *, fail_with: Exception | None = None) -> None:
        self._names = names or set()
        self._fail_with = fail_with

    def list_names(self) -> set[str]:
        if self._fail_with is not None:
            raise self._fail_with
        return self._names


class TestPiNAllocation(unittest.TestCase):
    """pi-N allocation against an injected Docker-inspection boundary.

    Uses ``docker ps -a --format '{{.Names}}'`` to discover existing
    container names, then selects the lowest free ``pi-N`` number.
    Allocation is best-effort: another process may claim the name
    before ``docker run`` executes.
    """

    # ── helpers ──────────────────────────────────────────────────

    @staticmethod
    def _allocate(
        inspector: ContainerNameInspector,
    ) -> str:
        from docker.launcher import allocate_pi_name
        return allocate_pi_name(inspector)

    # ── fresh namespace ──────────────────────────────────────────

    def test_no_existing_names_returns_pi_1(self) -> None:
        inspector = FakeContainerNameInspector(set())
        self.assertEqual(self._allocate(inspector), "pi-1")

    def test_no_matching_names_returns_pi_1(self) -> None:
        inspector = FakeContainerNameInspector({"nginx", "redis"})
        self.assertEqual(self._allocate(inspector), "pi-1")

    # ── pi-1 taken ───────────────────────────────────────────────

    def test_pi_1_exists_returns_pi_2(self) -> None:
        inspector = FakeContainerNameInspector({"pi-1"})
        self.assertEqual(self._allocate(inspector), "pi-2")

    def test_pi_1_and_pi_2_exist_returns_pi_3(self) -> None:
        inspector = FakeContainerNameInspector({"pi-1", "pi-2"})
        self.assertEqual(self._allocate(inspector), "pi-3")

    # ── sparse allocation ────────────────────────────────────────

    def test_pi_1_and_pi_3_exist_returns_pi_2(self) -> None:
        """Lowest free number is pi-2 even though pi-3 exists."""
        inspector = FakeContainerNameInspector({"pi-1", "pi-3"})
        self.assertEqual(self._allocate(inspector), "pi-2")

    def test_unordered_names_still_produce_lowest_free(self) -> None:
        inspector = FakeContainerNameInspector({"pi-5", "pi-2", "pi-1"})
        self.assertEqual(self._allocate(inspector), "pi-3")

    def test_many_existing_pi_names(self) -> None:
        inspector = FakeContainerNameInspector(
            {f"pi-{i}" for i in range(1, 50)}
        )
        self.assertEqual(self._allocate(inspector), "pi-50")

    # ── non-pi names do not reserve slots ────────────────────────

    def test_pi_1_old_does_not_reserve_pi_1(self) -> None:
        inspector = FakeContainerNameInspector({"pi-1-old"})
        self.assertEqual(self._allocate(inspector), "pi-1")

    def test_xpi_1_does_not_reserve_pi_1(self) -> None:
        inspector = FakeContainerNameInspector({"xpi-1"})
        self.assertEqual(self._allocate(inspector), "pi-1")

    def test_pi_a_does_not_reserve_any_number(self) -> None:
        inspector = FakeContainerNameInspector({"pi-a", "pi-b"})
        self.assertEqual(self._allocate(inspector), "pi-1")

    def test_pi_without_number_ignored(self) -> None:
        inspector = FakeContainerNameInspector({"pi-"})
        self.assertEqual(self._allocate(inspector), "pi-1")

    def test_pi_with_leading_zero_parsed(self) -> None:
        """pi-01 reserves pi-1 because pi-01 is an integer 1."""
        inspector = FakeContainerNameInspector({"pi-01"})
        self.assertEqual(self._allocate(inspector), "pi-2")

    def test_pi_with_large_number(self) -> None:
        inspector = FakeContainerNameInspector({"pi-999999999999"})
        result = self._allocate(inspector)
        self.assertEqual(result, "pi-1")

    # ── inspection failures ──────────────────────────────────────

    def test_inspection_failure_is_actionable(self) -> None:
        inspector = FakeContainerNameInspector(
            fail_with=ContainerInspectError("docker not found"),
        )
        with self.assertRaises(ContainerInspectError) as ctx:
            self._allocate(inspector)
        self.assertIn("docker not found", str(ctx.exception))

    def test_docker_not_installed_structured(self) -> None:
        inspector = FakeContainerNameInspector(
            fail_with=ContainerInspectError(
                "Docker executable not found",
            ),
        )
        with self.assertRaises(ContainerInspectError) as ctx:
            self._allocate(inspector)
        self.assertIn("Docker executable", str(ctx.exception))

    # ── malformed output ─────────────────────────────────────────

    def test_malformed_number_skipped(self) -> None:
        """A malformed pi name like 'pi-abc' must not crash the
        allocator — it is simply ignored."""
        inspector = FakeContainerNameInspector({"pi-1", "pi-abc"})
        self.assertEqual(self._allocate(inspector), "pi-2")

    def test_all_malformed_falls_back_to_pi_1(self) -> None:
        inspector = FakeContainerNameInspector({"pi-abc", "pi-xyz"})
        self.assertEqual(self._allocate(inspector), "pi-1")


# ═══════════════════════════════════════════════════════════════════
# 11.1.4 - Launch-vector contract
# ═══════════════════════════════════════════════════════════════════


class TestLaunchVectorContract(unittest.TestCase):
    """Integration of workspace selection → :class:`RunRenderInputs` →
    :func:`render_run_vector`.  The launcher must produce a correct
    ``docker run`` vector from selected extra_workspaces without Compose."""

    # ── helpers ──────────────────────────────────────────────────

    @staticmethod
    def _build_inputs(
        selection: WorkspaceSelection,
        *,
        image: str = "pi-cli-pi:latest",
        container_name: str = "pi-1",
        pi_home_host: str = "/home/alice/.pi",
        projection_host_path: str = (
            "/home/dev/.cache/docker-constructor/projects/constructor-identity/runtime/proj.toml"
        ),
        projection_container_path: str = (
            "/run/pi-cli/docker-constructor.runtime.toml"
        ),
        tty: bool = True,
        stdin_open: bool = True,
        chown_on_start: str | None = None,
    ) -> RunRenderInputs:
        from docker.launcher import build_run_inputs
        return build_run_inputs(
            selection=selection,
            image=image,
            container_name=container_name,
            pi_home_host=pi_home_host,
            projection_host_path=projection_host_path,
            projection_container_path=projection_container_path,
            tty=tty,
            stdin_open=stdin_open,
            chown_on_start=chown_on_start,
        )

    @staticmethod
    def _vector(inputs: RunRenderInputs) -> tuple[str, ...]:
        return render_run_vector(inputs)

    # ── basic shape ──────────────────────────────────────────────

    def test_command_starts_with_docker_run_rm(self) -> None:
        sel = WorkspaceSelection(workspace="/work/p1")
        args = self._vector(self._build_inputs(sel))
        self.assertEqual(args[:3], ("docker", "run", "--rm"))

    def test_no_compose_in_output(self) -> None:
        sel = WorkspaceSelection(workspace="/work/p1")
        args = self._vector(self._build_inputs(sel))
        self.assertNotIn("compose", args)
        self.assertNotIn("-f", args)
        self.assertNotIn("--file", args)

    # ── container name ───────────────────────────────────────────

    def test_container_name_in_vector(self) -> None:
        sel = WorkspaceSelection(workspace="/work/p1")
        inputs = self._build_inputs(sel, container_name="pi-7")
        args = self._vector(inputs)
        name_idx = args.index("--name")
        self.assertEqual(args[name_idx + 1], "pi-7")

    # ── Pi home mount ────────────────────────────────────────────

    def test_pi_home_mounted_to_container(self) -> None:
        """Host Pi home is mounted at /home/dev/.pi in the container."""
        sel = WorkspaceSelection(workspace="/work/p1")
        inputs = self._build_inputs(sel, pi_home_host="/host/pi")
        args = self._vector(inputs)
        spec = _parse_mount_spec(args, dst="/home/dev/.pi")
        self.assertEqual(spec["src"], "/host/pi")
        self.assertEqual(spec["dst"], "/home/dev/.pi")

    # ── primary workspace 1:1 mount + workdir ─────────────────────────

    def test_workspace_1to1_mount(self) -> None:
        sel = WorkspaceSelection(workspace="/work/p1")
        args = self._vector(self._build_inputs(sel))
        spec = _parse_mount_spec(args, dst="/work/p1")
        self.assertEqual(spec["src"], "/work/p1")

    def test_workspace_is_workdir(self) -> None:
        sel = WorkspaceSelection(workspace="/work/p1")
        args = self._vector(self._build_inputs(sel))
        wd_idx = args.index("--workdir")
        self.assertEqual(args[wd_idx + 1], "/work/p1")

    # ── extra workspaces ────────────────────────────────────────

    def test_one_extra_workspace_mount(self) -> None:
        sel = WorkspaceSelection(
            workspace="/work/primary",
            extra_workspaces=("/work/extra",),
        )
        args = self._vector(self._build_inputs(sel))
        spec = _parse_mount_spec(args, dst="/work/extra")
        self.assertEqual(spec["src"], "/work/extra")

    def test_two_extra_workspaces_mount(self) -> None:
        sel = WorkspaceSelection(
            workspace="/work/primary",
            extra_workspaces=("/work/a", "/work/b"),
        )
        args = self._vector(self._build_inputs(sel))
        spec_a = _parse_mount_spec(args, dst="/work/a")
        spec_b = _parse_mount_spec(args, dst="/work/b")
        self.assertEqual(spec_a["src"], "/work/a")
        self.assertEqual(spec_b["src"], "/work/b")

    def test_extra_workspaces_preserved_in_order(self) -> None:
        sel = WorkspaceSelection(
            workspace="/work/primary",
            extra_workspaces=("/work/z", "/work/a"),
        )
        args = self._vector(self._build_inputs(sel))
        z_idx = _find_mount(args, dst="/work/z")
        a_idx = _find_mount(args, dst="/work/a")
        self.assertLess(z_idx, a_idx, "extra-workspace mounts must preserve order")

    # ── WORKSPACE_PATH_* environment ───────────────────────────────

    def test_runtime_path_1_is_primary_workspace(self) -> None:
        sel = WorkspaceSelection(workspace="/work/p1")
        args = self._vector(self._build_inputs(sel))
        self.assertIn("WORKSPACE_PATH_1=/work/p1", args)

    def test_runtime_path_2_is_first_extra_workspace(self) -> None:
        sel = WorkspaceSelection(
            workspace="/work/primary",
            extra_workspaces=("/work/extra1", "/work/extra2"),
        )
        args = self._vector(self._build_inputs(sel))
        self.assertIn("WORKSPACE_PATH_1=/work/primary", args)
        self.assertIn("WORKSPACE_PATH_2=/work/extra1", args)
        self.assertIn("WORKSPACE_PATH_3=/work/extra2", args)

    def test_runtime_path_count_matches_extra_workspaces(self) -> None:
        sel = WorkspaceSelection(
            workspace="/work/primary",
            extra_workspaces=("/work/a", "/work/b", "/work/c"),
        )
        args = self._vector(self._build_inputs(sel))
        project_path_count = sum(
            1 for a in args if a.startswith("WORKSPACE_PATH_")
        )
        self.assertEqual(project_path_count, 4)  # 1 primary + 3 extra

    def test_many_extra_workspaces_each_mounted_and_exported(self) -> None:
        """Every extra workspace appears as a 1:1 mount *and* as a
        consecutive WORKSPACE_PATH_N with no gaps in numbering."""
        extra_workspaces = tuple(f"/work/extra{i}" for i in range(1, 6))
        sel = WorkspaceSelection(
            workspace="/work/primary",
            extra_workspaces=extra_workspaces,
        )
        args = self._vector(self._build_inputs(sel))

        # Primary workspace: mount + WORKSPACE_PATH_1
        primary_spec = _parse_mount_spec(args, dst="/work/primary")
        self.assertEqual(primary_spec["src"], "/work/primary")
        self.assertIn("WORKSPACE_PATH_1=/work/primary", args)

        # Each extra workspace: mounted 1:1 and exported consecutively
        for i, path in enumerate(extra_workspaces, start=2):
            spec = _parse_mount_spec(args, dst=path)
            self.assertEqual(
                spec["src"], path,
                f"extra workspace {path!r} must be mounted 1:1",
            )
            self.assertIn(
                f"WORKSPACE_PATH_{i}={path}", args,
                f"{path!r} must be exported as WORKSPACE_PATH_{i}",
            )

        # No gaps: exactly N+1 WORKSPACE_PATH_ vars for N extra workspaces
        exported = sorted(
            a for a in args if a.startswith("WORKSPACE_PATH_")
        )
        expected = [
            f"WORKSPACE_PATH_{i}={p}"
            for i, p in enumerate(
                ("/work/primary",) + extra_workspaces, start=1,
            )
        ]
        self.assertEqual(
            exported, expected,
            "WORKSPACE_PATH_* must be consecutive with no gaps",
        )

    # ── host access ─────────────────────────────────────────────

    def test_enabled_host_access_in_add_host(self) -> None:
        from dataclasses import replace

        from docker.versioning.rendering import RunHostAccess
        sel = WorkspaceSelection(workspace="/work/p1")
        inputs = replace(
            self._build_inputs(sel),
            host_access=RunHostAccess(address="192.168.1.1", mode="external-address"),
        )
        args = self._vector(inputs)
        self.assertIn("--add-host", args)
        host_idx = args.index("--add-host")
        self.assertEqual(
            args[host_idx + 1], "host.docker.internal:192.168.1.1",
        )

    # ── image and command ────────────────────────────────────────

    def test_image_placed_correctly(self) -> None:
        sel = WorkspaceSelection(workspace="/work/p1")
        inputs = self._build_inputs(sel, image="my-image:tag")
        args = self._vector(inputs)
        # Image must be the last argument before any passthrough command.
        self.assertIn("my-image:tag", args)

    # ── chown_on_start ───────────────────────────────────────────

    def test_chown_on_start_set(self) -> None:
        sel = WorkspaceSelection(workspace="/work/p1")
        inputs = self._build_inputs(sel, chown_on_start="1")
        args = self._vector(inputs)
        self.assertIn("CHOWN_WORK_ON_START=1", args)

    def test_no_chown_on_start_when_none(self) -> None:
        sel = WorkspaceSelection(workspace="/work/p1")
        inputs = self._build_inputs(sel, chown_on_start=None)
        args = self._vector(inputs)
        chown_vars = [a for a in args if "CHOWN_WORK_ON_START" in a]
        self.assertEqual(chown_vars, [])

    # ── full pi-N integration ────────────────────────────────────

    def test_pi_n_allocation_integrated_into_vector(self) -> None:
        """End-to-end: allocate pi-N from an inspector, produce a
        vector where --name matches the allocated name."""
        inspector = FakeContainerNameInspector({"pi-1", "pi-2"})
        from docker.launcher import allocate_pi_name

        name = allocate_pi_name(inspector)
        self.assertEqual(name, "pi-3")

        sel = WorkspaceSelection(workspace="/work/p1")
        inputs = self._build_inputs(sel, container_name=name)
        args = self._vector(inputs)

        name_idx = args.index("--name")
        self.assertEqual(args[name_idx + 1], "pi-3")


# ═══════════════════════════════════════════════════════════════════
# helpers
# ═══════════════════════════════════════════════════════════════════


def _find_mount(args: tuple[str, ...], *, dst: str) -> int:
    """Find the start index of a ``--mount`` argument by
    destination path.  Returns the index of ``--mount``."""
    for i, a in enumerate(args):
        if a == "--mount":
            spec = args[i + 1]
            for part in spec.split(","):
                if part == f"dst={dst}":
                    return i
    raise ValueError(f"mount with dst={dst!r} not found in args")


def _parse_mount_spec(
    args: tuple[str, ...], *, dst: str,
) -> dict[str, str]:
    """Find a ``--mount`` by *dst* and return its spec as a
    ``{key: value}`` dict."""
    idx = _find_mount(args, dst=dst)
    spec_str = args[idx + 1]
    result: dict[str, str] = {}
    for part in spec_str.split(","):
        k, _, v = part.partition("=")
        result[k] = v
    return result


# ═══════════════════════════════════════════════════════════════════
# 11.2 - Run transaction (orchestrate_run)
# ═══════════════════════════════════════════════════════════════════


class FakeRunExecutor:
    """Recording :class:`RunExecutor` for deterministic tests."""

    def __init__(
        self,
        *,
        returncode: int = 0,
        fail_with: Exception | None = None,
    ) -> None:
        self.returncode = returncode
        self._fail_with = fail_with
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: tuple[str, ...], *,
            interactive: bool = False) -> ProcessResult:
        if self._fail_with is not None:
            raise self._fail_with
        self.calls.append(argv)
        return ProcessResult(
            argv=argv,
            return_code=self.returncode,
            stdout="ok" if self.returncode == 0 else "",
            stderr="" if self.returncode == 0 else "container failed",
        )


class _BombExecutor:
    """Executor that explodes if ``run()`` is ever invoked.

    Used in dry-run tests to prove the orchestrator never calls
    Docker — including ``docker run``.  Any invocation is a test
    failure."""

    def run(self, argv: tuple[str, ...], *,
            interactive: bool = False) -> ProcessResult:
        raise AssertionError(
            "_BombExecutor.run() called — dry-run must not invoke Docker",
        )


class _BombInspector:
    """Inspector that explodes if ``list_names()``
    is ever invoked.

    Used in dry-run tests to prove the orchestrator never calls
    ``docker ps`` for pi-N allocation.  Any invocation is a test
    failure."""

    def list_names(self) -> frozenset[str]:
        raise AssertionError(
            "_BombInspector.list_names() called — "
            "dry-run must not invoke docker ps",
        )


class _BombProjectionFactory:
    """Projection factory that explodes if called.

    Used in dry-run tests to prove the orchestrator never creates
    a temporary projection file — not even "create then delete".
    """

    def __call__(
        self,
        projection: object,
        *,
        parent_dir: str,
    ) -> object:
        raise AssertionError(
            "_BombProjectionFactory called — "
            "dry-run must not create projection files",
        )


class _RecordingHandle:
    """Spy context manager that records enter/exit calls and
    creates/removes a real file for observable cleanup assertions."""

    def __init__(self, path: str, content_hash: str) -> None:
        self._path = path
        self._hash = content_hash
        self.entered = False
        self.exited = False
        self.exit_args: tuple[object, object, object] | None = None

    @property
    def path(self) -> str:
        return self._path

    @property
    def content_hash(self) -> str:
        return self._hash

    def __enter__(self) -> "_RecordingHandle":
        self.entered = True
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        with open(self._path, "w") as fh:
            fh.write("fake-projection-content")
        return self

    def __exit__(
        self,
        exc_type: object,
        exc_val: object,
        exc_tb: object,
    ) -> bool:
        self.exited = True
        self.exit_args = (exc_type, exc_val, exc_tb)
        if os.path.exists(self._path):
            os.remove(self._path)
        return False  # never suppress


class RecordingProjectionFactory:
    """Spy :class:`ProjectionFactory` that records every call and
    returns :class:`_RecordingHandle` instances for inspection.

    The content hash is computed from the serialized projection so
    override-selection tests can prove artifact switching."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, str]] = []
        self.handles: list[_RecordingHandle] = []

    def __call__(
        self,
        projection: object,
        *,
        parent_dir: str,
    ) -> _RecordingHandle:
        import dataclasses
        import hashlib
        import json

        # Convert the projection to a plain dict for hashing.
        # dataclasses.asdict chokes on MappingProxyType, so we
        # rebuild the extensions dict manually.
        raw_proj: dict[str, object] = {
            "extensions": {
                name: dataclasses.asdict(entry)
                for name, entry in getattr(
                    projection, "extensions", {}
                ).items()
            }
        }
        raw = json.dumps(raw_proj, sort_keys=True, default=str)
        content_hash = hashlib.sha256(
            raw.encode("utf-8")
        ).hexdigest()
        path = os.path.join(parent_dir, "proj.toml")
        h = _RecordingHandle(path, content_hash)
        self.calls.append((projection, parent_dir))
        self.handles.append(h)
        return h


class TestRunTransaction(unittest.TestCase):
    """Run-transaction contract for :func:`orchestrate_run`.

    Covers: runtime overrides, private projection creation, read-only
    mount, gateway mapping, TTY modes, dry-run, Docker failure, and
    projection cleanup.
    """

    def setUp(self) -> None:
        self._lock_leak_before = _shared_lock_leak_snapshot()
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self._artifact_cache_root = os.path.join(
            self._tmpdir.name, "runtime-artifacts", "blobs",
        )
        self._constructor_cache_root = os.path.join(self._tmpdir.name, "constructor-cache")
        self._artifact_locks_root = os.path.join(self._tmpdir.name, "artifact-locks")
        self._artifact_tmp_root = os.path.join(self._tmpdir.name, "artifact-tmp")
        os.mkdir(self._constructor_cache_root, 0o700)
        # ── repo-cache leak guard ───────────────────────────
        self._repo_cache_snapshot = _repo_cache_file_set()
        self._proj_parent = os.path.join(
            self._tmpdir.name, "projects", "constructor-identity", "runtime",
        )
        # Build a fixture TOML that adds a second artifact version for
        # pi-read so override-selection tests can prove a non-default
        # artifact is resolved.
        self._inventory_path = self._make_fixture_toml()

    def _make_fixture_toml(self) -> str:
        """Copy the real docker-constructor.toml and inject an
        second ``pi-read`` artifact at version ``0.3.0`` so
        override-version selection is observable."""
        import shutil
        real = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "docker-constructor.toml"),
        )
        fixture = os.path.join(self._tmpdir.name, "docker-constructor.toml")
        shutil.copy2(real, fixture)
        override_url = (
            "https://registry.npmjs.org/@arcanemachine/pi-read/"
            "-/pi-read-0.3.0.tgz"
        )
        with open(fixture, "a") as fh:
            fh.write(
                '\n'
                '[runtime.pi-extensions.pi-read.artifacts."0.3.0"]\n'
                f'url = "{override_url}"\n'
                'integrity = "sha512-placeholder"\n'
            )

        # Make every fixture artifact deterministic and network-independent.
        import base64
        import hashlib
        import re
        with open(fixture) as fh:
            content = fh.read()

        def replace_integrity(match: re.Match[str]) -> str:
            url = match.group(1)
            digest = base64.b64encode(
                hashlib.sha512(self._artifact_bytes(url)).digest()
            ).decode("ascii")
            return f'url = "{url}"\nintegrity = "sha512-{digest}"'

        # The canonical inventory may order ``integrity`` before or after
        # ``url`` within an artifact table.  Rewrite both orderings into a
        # single deterministic ``url``-then-``integrity`` form so every
        # artifact digest matches the injected fetcher bytes.
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
        with open(fixture, "w") as fh:
            fh.write(content)
        return fixture

    @staticmethod
    def _artifact_bytes(url: str) -> bytes:
        return ("launcher-fixture-artifact:" + url).encode("utf-8")

    def tearDown(self) -> None:
        self._tmpdir.cleanup()
        _ = _assert_repo_cache_unchanged(self._repo_cache_snapshot)
        _assert_no_shared_lock_created(self._lock_leak_before)

    # ── helpers ──────────────────────────────────────────────────

    def _request(self, **overrides: object) -> RunRequest:
        from docker.launcher import orchestrate_run  # keep import alive
        kwargs: dict[str, object] = {
            "inventory_path": self._inventory_path,
            "project_root": str(Path(self._inventory_path).parent),
            "image": "pi-cli-pi:latest",
            "selection": WorkspaceSelection(workspace="/work/p1"),
            "pi_home_host": "/home/alice/.pi",
            "_create_projection": RecordingProjectionFactory(),
            "_artifact_fetcher": self._artifact_bytes,
            "_artifact_cache_root": self._artifact_cache_root,
            "_artifact_locks_root": self._artifact_locks_root,
            "_artifact_tmp_root": self._artifact_tmp_root,
            "_constructor_cache_root": self._constructor_cache_root,
        }
        kwargs.update(overrides)
        return RunRequest(**kwargs)  # type: ignore[arg-type]

    @staticmethod
    def _run(req: RunRequest) -> RunResult:
        from docker.launcher import orchestrate_run
        return orchestrate_run(req)

    # ── happy path ───────────────────────────────────────────────

    def test_successful_run_produces_run_args(self) -> None:
        req = self._request(
            executor=FakeRunExecutor(returncode=0),
            inspector=FakeContainerNameInspector(set()),
        )
        result = self._run(req)
        self.assertEqual(result.exit_kind, ExitKind.SUCCESS)
        self.assertTrue(len(result.run_args) > 0,
                        "run_args must be non-empty")
        self.assertEqual(result.run_args[:3], ("docker", "run", "--rm"))

    def test_successful_run_invokes_executor(self) -> None:
        executor = FakeRunExecutor(returncode=0)
        req = self._request(
            executor=executor,
            inspector=FakeContainerNameInspector(set()),
        )
        result = self._run(req)
        self.assertEqual(result.exit_kind, ExitKind.SUCCESS)
        self.assertEqual(len(executor.calls), 1,
                         "executor must be called exactly once")
        self.assertEqual(
            executor.calls[0], result.run_args,
            "executor must receive exactly the rendered args",
        )

    def test_process_result_captured(self) -> None:
        executor = FakeRunExecutor(returncode=0)
        req = self._request(
            executor=executor,
            inspector=FakeContainerNameInspector(set()),
        )
        result = self._run(req)
        self.assertIsNotNone(result.process_result)
        self.assertEqual(result.process_result.return_code, 0)  # type: ignore[union-attr]

    # ── private projection ───────────────────────────────────────

    def test_projection_created_under_external_constructor_runtime(self) -> None:
        req = self._request(
            executor=FakeRunExecutor(returncode=0),
            inspector=FakeContainerNameInspector(set()),
        )
        result = self._run(req)
        self.assertIsNotNone(result.projection_path)
        self.assertIn(
            "/projects/",
            result.projection_path,  # type: ignore[arg-type]
            "projection must be under external constructor project state",
        )
        self.assertIn("/runtime/", result.projection_path)  # type: ignore[arg-type]

    def test_projection_has_content_hash(self) -> None:
        req = self._request(
            executor=FakeRunExecutor(returncode=0),
            inspector=FakeContainerNameInspector(set()),
        )
        result = self._run(req)
        self.assertIsNotNone(result.projection_hash)
        self.assertEqual(len(result.projection_hash or ""), 64,  # type: ignore[arg-type]
                         "SHA-256 hash must be 64 hex chars")

    # ── read-only mount ──────────────────────────────────────────

    def test_projection_mounted_readonly(self) -> None:
        req = self._request(
            executor=FakeRunExecutor(returncode=0),
            inspector=FakeContainerNameInspector(set()),
        )
        result = self._run(req)
        # Find the projection mount and verify 'readonly' is in the spec.
        proj_idx = _find_mount(
            result.run_args,
            dst="/run/pi-cli/docker-constructor.runtime.toml",
        )
        spec = result.run_args[proj_idx + 1]
        self.assertIn("readonly", spec,
                      "runtime projection must be mounted read-only")

    # ── host access ─────────────────────────────────────────────

    def test_disabled_host_access_no_mapping_by_default(self) -> None:
        """When the inventory has no ``[runtime.host-access]``, host
        access is disabled and no ``--add-host`` is rendered."""
        req = self._request(
            executor=FakeRunExecutor(returncode=0),
            inspector=FakeContainerNameInspector(set()),
        )
        result = self._run(req)
        self.assertNotIn("--add-host", result.run_args,
                         "disabled host access must not emit --add-host")

    # ── TTY modes ────────────────────────────────────────────────

    def test_tty_on_by_default(self) -> None:
        req = self._request(
            executor=FakeRunExecutor(returncode=0),
            inspector=FakeContainerNameInspector(set()),
        )
        result = self._run(req)
        self.assertIn("--tty", result.run_args)

    def test_tty_off_when_requested(self) -> None:
        req = self._request(
            tty=False,
            executor=FakeRunExecutor(returncode=0),
            inspector=FakeContainerNameInspector(set()),
        )
        result = self._run(req)
        self.assertNotIn("--tty", result.run_args)

    def test_interactive_on_by_default(self) -> None:
        req = self._request(
            executor=FakeRunExecutor(returncode=0),
            inspector=FakeContainerNameInspector(set()),
        )
        result = self._run(req)
        self.assertIn("--interactive", result.run_args)

    def test_interactive_off_when_requested(self) -> None:
        req = self._request(
            stdin_open=False,
            executor=FakeRunExecutor(returncode=0),
            inspector=FakeContainerNameInspector(set()),
        )
        result = self._run(req)
        self.assertNotIn("--interactive", result.run_args)

    # ── pi-N allocation ──────────────────────────────────────────

    def test_container_name_allocated(self) -> None:
        req = self._request(
            executor=FakeRunExecutor(returncode=0),
            inspector=FakeContainerNameInspector({"pi-1", "pi-2"}),
        )
        result = self._run(req)
        self.assertEqual(result.container_name, "pi-3")
        name_idx = result.run_args.index("--name")
        self.assertEqual(result.run_args[name_idx + 1], "pi-3")

    def test_default_factory_creates_real_projection_and_cleans_up(self) -> None:
        """When no _create_projection is injected, the default
        factory must call the real create_runtime_projection and
        produce a handle beneath external constructor project state.
        The handle must clean up on exit."""
        req = self._request(
            executor=FakeRunExecutor(returncode=0),
            inspector=FakeContainerNameInspector(set()),
            _create_projection=None,
        )
        result = self._run(req)
        self.assertEqual(result.exit_kind, ExitKind.SUCCESS)
        self.assertIsNotNone(result.projection_path)
        expected_prefix = os.path.realpath(os.path.join(self._constructor_cache_root, "projects"))
        self.assertTrue(
            os.path.realpath(
                result.projection_path  # type: ignore[arg-type]
            ).startswith(expected_prefix),
            f"default factory path {result.projection_path!r} "
            f"must be under {expected_prefix!r}",
        )
        # The handle must have cleaned up on exit.
        self.assertFalse(
            os.path.exists(result.projection_path),  # type: ignore[arg-type]
            "default factory handle must remove projection after exit",
        )

    # ── dry-run ──────────────────────────────────────────────────

    def test_dry_run_produces_display_string(self) -> None:
        req = self._request(
            dry_run=True,
            executor=_BombExecutor(),
            inspector=_BombInspector(),
            _create_projection=_BombProjectionFactory(),
        )
        result = self._run(req)
        self.assertEqual(result.exit_kind, ExitKind.SUCCESS)
        self.assertIsNotNone(result.display_string)
        display = result.display_string or ""
        self.assertIn("docker run", display)

    def test_dry_run_does_not_invoke_executor(self) -> None:
        req = self._request(
            dry_run=True,
            executor=_BombExecutor(),
            inspector=_BombInspector(),
            _create_projection=_BombProjectionFactory(),
        )
        result = self._run(req)
        self.assertEqual(result.exit_kind, ExitKind.SUCCESS)
        # If any bomb exploded the test would have already failed.

    def test_dry_run_still_renders_args(self) -> None:
        req = self._request(
            dry_run=True,
            executor=_BombExecutor(),
            inspector=_BombInspector(),
            _create_projection=_BombProjectionFactory(),
        )
        result = self._run(req)
        self.assertTrue(len(result.run_args) > 0)

    def test_dry_run_includes_artifact_mounts(self) -> None:
        """Dry-run display and run_args must include artifact mount
        ``--mount … read  read`` arguments with fixed container targets
        beneath ``/run/pi-cli/runtime-artifacts``."""
        req = self._request(
            dry_run=True,
            executor=_BombExecutor(),
            inspector=_BombInspector(),
            _create_projection=_BombProjectionFactory(),
        )
        result = self._run(req)
        self.assertEqual(result.exit_kind, ExitKind.SUCCESS)
        # Check the display string.
        display = result.display_string or ""
        self.assertIn("--mount type=bind", display)
        self.assertIn("/run/pi-cli/runtime-artifacts/", display)
        self.assertIn(",readonly", display)
        # Check the argument vector.
        args = result.run_args
        art_mounts = [
            i for i, tok in enumerate(args)
            if tok == "--mount"
            and "runtime-artifacts" in args[i + 1]
        ]
        self.assertGreater(
            len(art_mounts), 0,
            "dry-run arg vector must contain at least one "
            "artifact --mount argument",
        )
        for idx in art_mounts:
            opts = args[idx + 1]
            self.assertIn("type=bind", opts)
            self.assertIn(",readonly", opts)
            self.assertIn(
                "dst=/run/pi-cli/runtime-artifacts/", opts,
            )

    def test_dry_run_display_excludes_reviewed_urls(self) -> None:
        """The dry-run display string MUST NOT expose registry URLs
        or artifact download endpoints."""
        req = self._request(
            dry_run=True,
            executor=_BombExecutor(),
            inspector=_BombInspector(),
            _create_projection=_BombProjectionFactory(),
        )
        result = self._run(req)
        self.assertEqual(result.exit_kind, ExitKind.SUCCESS)
        display = result.display_string or ""
        self.assertNotIn("registry.npmjs.org", display)
        self.assertNotIn("https://", display)

    def test_dry_run_display_is_shell_escaped(self) -> None:
        """``orchestrate_run`` calls ``shlex.join`` directly (not
        ``render_command_display``), so the display string must
        single-quote artifact mount options containing spaces or ``$``.
        Round-trip through ``shlex.split`` must recover the arg vector."""
        from unittest import mock
        from docker.versioning.rendering import ArtifactMount
        injected = (
            ArtifactMount(
                host_path="/home/alice/my projects/cache/sha512/abc.tgz",
                container_target=(
                    "/run/pi-cli/runtime-artifacts/sha512/abc.tgz"
                ),
            ),
        )

        def _patched_plan(selected, *, cache_root: str) -> tuple[ArtifactMount, ...]:
            return injected

        req = self._request(
            dry_run=True,
            executor=_BombExecutor(),
            inspector=_BombInspector(),
            _create_projection=_BombProjectionFactory(),
        )

        with mock.patch(
            "docker.launcher.plan_dry_run_artifact_mounts",
            side_effect=_patched_plan,
        ):
            result = self._run(req)
        self.assertEqual(result.exit_kind, ExitKind.SUCCESS)
        display = result.display_string or ""
        # Must contain the quoted mount option.
        self.assertIn(
            "'type=bind,src=/home/alice/my projects/", display,
            "mount option with space must be single-quoted",
        )
        # Round-trip: shlex.split the display must recover the run_args.
        import shlex
        self.assertEqual(
            shlex.split(display), list(result.run_args),
            "display must round-trip through shlex.split",
        )

    def test_dry_run_excludes_unselected_catalog_artifact(self) -> None:
        """The dry-run display and run_args must include artifact
        mounts for *selected* artifacts only — unselected catalog
        variants (e.g. an alternate version) MUST NOT appear."""
        from docker.versioning.model import _derive_artifact_id
        import base64, hashlib
        # Compute the unselected 0.3.0 artifact_id directly.
        unselected_url = (
            "https://registry.npmjs.org/@arcanemachine/pi-read/"
            "-/pi-read-0.3.0.tgz"
        )
        unselected_integrity = "sha512-" + base64.b64encode(
            hashlib.sha512(self._artifact_bytes(unselected_url)).digest(),
        ).decode("ascii")
        unselected_artifact_id = _derive_artifact_id(unselected_integrity)
        unselected_target = (
            f"/run/pi-cli/runtime-artifacts/{unselected_artifact_id}"
        )

        req = self._request(
            dry_run=True,
            executor=_BombExecutor(),
            inspector=_BombInspector(),
            _create_projection=_BombProjectionFactory(),
        )
        result = self._run(req)
        self.assertEqual(result.exit_kind, ExitKind.SUCCESS)
        display = result.display_string or ""

        # Selected artifact(s) must be present.
        self.assertIn("--mount type=bind", display)
        self.assertIn("/run/pi-cli/runtime-artifacts/", display)

        # The *unselected* 0.3.0 variant must NOT leak into the
        # display or arg vector.
        self.assertNotIn(unselected_target, display)
        self.assertNotIn(
            unselected_target,
            " ".join(result.run_args),
        )

    # ── Docker failure ───────────────────────────────────────────

    def test_executor_nonzero_exit_is_operational_error(self) -> None:
        req = self._request(
            executor=FakeRunExecutor(returncode=1),
            inspector=FakeContainerNameInspector(set()),
        )
        result = self._run(req)
        self.assertEqual(result.exit_kind, ExitKind.OPERATIONAL)
        self.assertIsNotNone(result.process_result)
        self.assertEqual(
            result.process_result.return_code, 1,  # type: ignore[union-attr]
        )

    def test_executor_raises_oserror(self) -> None:
        req = self._request(
            executor=FakeRunExecutor(
                fail_with=OSError("docker not found"),
            ),
            inspector=FakeContainerNameInspector(set()),
        )
        result = self._run(req)
        self.assertEqual(result.exit_kind, ExitKind.OPERATIONAL)
        self.assertIn("docker not found", result.message or "")

    def test_executor_exception_mapped_not_propagated(self) -> None:
        """Unexpected executor exceptions (OSError, RuntimeError,
        etc.) must be mapped to an OPERATIONAL :class:`RunResult`,
        never propagated to the caller."""
        req = self._request(
            executor=FakeRunExecutor(
                fail_with=RuntimeError("unexpected crash"),
            ),
            inspector=FakeContainerNameInspector(set()),
        )
        result = self._run(req)
        self.assertEqual(
            result.exit_kind, ExitKind.OPERATIONAL,
            "unexpected executor exception must be mapped, not raised",
        )
        self.assertIsNotNone(result.message)

    # ── projection cleanup (recording factory) ───────────────────

    def test_projection_created_and_cleaned_up_on_success(self) -> None:
        """On success the projection handle must be entered (file
        created), then exited (file removed)."""
        factory = RecordingProjectionFactory()
        req = self._request(
            executor=FakeRunExecutor(returncode=0),
            inspector=FakeContainerNameInspector(set()),
            _create_projection=factory,
        )
        result = self._run(req)
        self.assertEqual(result.exit_kind, ExitKind.SUCCESS)
        self.assertEqual(len(factory.handles), 1)
        h = factory.handles[0]
        self.assertTrue(h.entered, "projection must be entered (file created)")
        self.assertTrue(h.exited, "projection must be exited (file removed)")
        self.assertFalse(
            os.path.exists(h.path),
            "projection file must be gone after success",
        )

    def test_projection_created_and_cleaned_up_on_nonzero_exit(self) -> None:
        """Docker non-zero exit → handle entered, exited, file gone."""
        factory = RecordingProjectionFactory()
        req = self._request(
            executor=FakeRunExecutor(returncode=1),
            inspector=FakeContainerNameInspector(set()),
            _create_projection=factory,
        )
        result = self._run(req)
        self.assertEqual(result.exit_kind, ExitKind.OPERATIONAL)
        self.assertEqual(len(factory.handles), 1)
        h = factory.handles[0]
        self.assertTrue(h.entered)
        self.assertTrue(h.exited)
        self.assertFalse(os.path.exists(h.path))

    def test_projection_created_and_cleaned_up_on_executor_oserror(self) -> None:
        """When the executor raises OSError the handle must still
        be exited and the file removed."""
        factory = RecordingProjectionFactory()
        req = self._request(
            executor=FakeRunExecutor(
                fail_with=OSError("docker not found"),
            ),
            inspector=FakeContainerNameInspector(set()),
            _create_projection=factory,
        )
        result = self._run(req)
        self.assertEqual(len(factory.handles), 1)
        h = factory.handles[0]
        self.assertTrue(h.entered)
        self.assertTrue(
            h.exited,
            "projection must be cleaned up even when executor raises OSError",
        )
        self.assertFalse(os.path.exists(h.path))

    def test_projection_created_and_cleaned_up_on_runtime_error(self) -> None:
        """When the executor raises an arbitrary RuntimeError the
        handle must still be exited and the file removed."""
        factory = RecordingProjectionFactory()
        req = self._request(
            executor=FakeRunExecutor(
                fail_with=RuntimeError("unexpected crash"),
            ),
            inspector=FakeContainerNameInspector(set()),
            _create_projection=factory,
        )
        result = self._run(req)
        self.assertEqual(len(factory.handles), 1)
        h = factory.handles[0]
        self.assertTrue(h.entered)
        self.assertTrue(
            h.exited,
            "projection must be cleaned up even on unexpected RuntimeError",
        )
        self.assertFalse(os.path.exists(h.path))

    def test_dry_run_creates_no_temporary_files(self) -> None:
        """Dry-run SHALL not create temporary configuration files.

        A bomb projection factory proves the orchestrator never
        attempts to create a projection — not even a create-then-
        delete cycle.  Bomb executor and inspector independently
        prove no Docker process is launched."""
        req = self._request(
            dry_run=True,
            executor=_BombExecutor(),
            inspector=_BombInspector(),
            _create_projection=_BombProjectionFactory(),
        )
        result = self._run(req)
        # If any bomb exploded the test would have already failed.
        # Additionally assert no projection path is reported as
        # an on-disk file.
        if result.projection_path is not None:
            self.assertFalse(
                os.path.exists(result.projection_path),
                "dry-run must not create projection files on disk",
            )

    # ── dry-run cache hit/miss reporting ────────────────────

    # ── helpers ─────────────────────────────────────────────

    @staticmethod
    def _ids_for_urls(urls: list[str]) -> list[str]:
        """Compute sorted artifact_ids for *urls* using the
        same deterministic derivation as the fixture."""
        import base64, hashlib
        from docker.versioning.model import _derive_artifact_id
        return sorted(
            _derive_artifact_id(
                "sha512-"
                + base64.b64encode(
                    hashlib.sha512(
                        TestRunTransaction._artifact_bytes(u),
                    ).digest()
                ).decode("ascii"),
            )
            for u in urls
        )

    @staticmethod
    def _make_blob(
        cache_root: str, url: str,
    ) -> tuple[str, bytes]:
        """Write a valid blob for *url* under *cache_root* and
        return ``(artifact_id, test_bytes)``."""
        import base64, hashlib
        from docker.versioning.model import _derive_artifact_id
        test_bytes = TestRunTransaction._artifact_bytes(url)
        h = hashlib.sha512(test_bytes)
        raw = base64.b64encode(h.digest()).decode("ascii")
        integrity = f"sha512-{raw}"
        artifact_id = _derive_artifact_id(integrity)
        blob_path = os.path.join(cache_root, artifact_id)
        os.makedirs(os.path.dirname(blob_path), exist_ok=True)
        fd = os.open(
            blob_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            mode=0o600,
        )
        try:
            os.write(fd, test_bytes)
        finally:
            os.close(fd)
        return artifact_id, test_bytes

    # ── tests ────────────────────────────────────────────────

    def test_dry_run_reports_all_misses_when_cache_empty(self) -> None:
        """When no cache blobs exist, every unique resolved
        artifact must appear in ``artifact_cache_misses`` and
        none in ``artifact_cache_hits``."""
        expected_ids = self._ids_for_urls([
            "https://registry.npmjs.org/@arcanemachine/pi-read/-/pi-read-0.2.1.tgz",
            "https://registry.npmjs.org/@narumitw/pi-usage/-/pi-usage-0.60.7.tgz",
            "https://registry.npmjs.org/pi-proxy/-/pi-proxy-1.0.0.tgz",
        ])
        req = self._request(
            dry_run=True,
            executor=_BombExecutor(),
            inspector=_BombInspector(),
            _create_projection=_BombProjectionFactory(),
        )
        result = self._run(req)
        self.assertEqual(result.exit_kind, ExitKind.SUCCESS)
        self.assertEqual(
            result.artifact_cache_hits, (),
            "empty cache must produce zero hits",
        )
        self.assertEqual(
            sorted(result.artifact_cache_misses),
            expected_ids,
            "every selected artifact must be a miss when cache is empty",
        )

    def test_dry_run_reports_hit_for_valid_cached_blob(self) -> None:
        """A valid regular private blob whose bytes match the
        declared SRI integrity is reported as a cache hit."""
        url = "https://registry.npmjs.org/@arcanemachine/pi-read/-/pi-read-0.2.1.tgz"
        artifact_id, _test_bytes = self._make_blob(
            self._artifact_cache_root, url,
        )
        req = self._request(
            dry_run=True,
            executor=_BombExecutor(),
            inspector=_BombInspector(),
            _create_projection=_BombProjectionFactory(),
        )
        result = self._run(req)
        self.assertEqual(result.exit_kind, ExitKind.SUCCESS)
        self.assertIn(
            artifact_id, result.artifact_cache_hits,
            "verified blob must be a cache hit",
        )
        self.assertNotIn(
            artifact_id, result.artifact_cache_misses,
            "verified blob must not appear in misses",
        )
        expected_miss_ids = self._ids_for_urls([
            "https://registry.npmjs.org/@narumitw/pi-usage/-/pi-usage-0.60.7.tgz",
            "https://registry.npmjs.org/pi-proxy/-/pi-proxy-1.0.0.tgz",
        ])
        self.assertEqual(
            sorted(result.artifact_cache_misses),
            expected_miss_ids,
        )

    def test_dry_run_reports_corrupt_blob_as_miss(self) -> None:
        """A cache blob with bytes that do NOT match its declared
        SRI integrity must be reported as a miss, not a hit."""
        import base64, hashlib
        from docker.versioning.model import _derive_artifact_id

        url = "https://registry.npmjs.org/@arcanemachine/pi-read/-/pi-read-0.2.1.tgz"
        h = hashlib.sha512(self._artifact_bytes(url))
        integrity = (
            "sha512-"
            + base64.b64encode(h.digest()).decode("ascii")
        )
        artifact_id = _derive_artifact_id(integrity)

        # Write wrong bytes *with owner-only permissions* so the
        # security gate passes but the digest comparison fails.
        blob_path = os.path.join(
            self._artifact_cache_root, artifact_id,
        )
        os.makedirs(os.path.dirname(blob_path), exist_ok=True)
        fd = os.open(
            blob_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            mode=0o600,
        )
        try:
            os.write(fd, b"wrong bytes -- digest mismatch")
        finally:
            os.close(fd)

        req = self._request(
            dry_run=True,
            executor=_BombExecutor(),
            inspector=_BombInspector(),
            _create_projection=_BombProjectionFactory(),
        )
        result = self._run(req)
        self.assertEqual(result.exit_kind, ExitKind.SUCCESS)
        self.assertNotIn(
            artifact_id, result.artifact_cache_hits,
            "corrupt blob must not be reported as a hit",
        )
        self.assertIn(
            artifact_id, result.artifact_cache_misses,
            "corrupt blob must be reported as a planned miss",
        )

    # ── no-follow containment: root / algorithm dir / leaf ──

    def test_dry_run_symlinked_cache_root_is_miss(self) -> None:
        """When the cache root itself is a symlink
        (``O_NOFOLLOW`` fails), no blob is reachable and every
        artifact is a miss."""
        import base64, hashlib
        from docker.versioning.model import _derive_artifact_id

        url = "https://registry.npmjs.org/pi-proxy/-/pi-proxy-1.0.0.tgz"
        h = hashlib.sha512(self._artifact_bytes(url))
        integrity = (
            "sha512-"
            + base64.b64encode(h.digest()).decode("ascii")
        )
        artifact_id = _derive_artifact_id(integrity)

        # Write a valid blob at a real directory, then symlink
        # the cache root to it so that the root itself is the
        # symlink target.
        real_root = os.path.join(self._tmpdir.name, "real-root")
        self._make_blob(real_root, url)

        # Remove the real cache root and replace with symlink.
        os.makedirs(os.path.dirname(self._artifact_cache_root),
                    exist_ok=True)
        import shutil
        if os.path.lexists(self._artifact_cache_root):
            shutil.rmtree(self._artifact_cache_root)
        os.symlink(real_root, self._artifact_cache_root)

        req = self._request(
            dry_run=True,
            executor=_BombExecutor(),
            inspector=_BombInspector(),
            _create_projection=_BombProjectionFactory(),
        )
        result = self._run(req)
        self.assertEqual(result.exit_kind, ExitKind.SUCCESS)
        self.assertNotIn(
            artifact_id, result.artifact_cache_hits,
            "symlinked cache root must not yield any hit",
        )
        self.assertIn(
            artifact_id, result.artifact_cache_misses,
            "symlinked cache root must report artifacts as misses",
        )

    def test_dry_run_symlinked_algorithm_dir_external_is_miss(self) -> None:
        """When the algorithm subdirectory is a symlink pointing
        outside the cache root, the blob it points to is never
        opened — the entry is a miss."""
        import base64, hashlib
        from docker.versioning.model import _derive_artifact_id

        url = "https://registry.npmjs.org/pi-proxy/-/pi-proxy-1.0.0.tgz"
        h = hashlib.sha512(self._artifact_bytes(url))
        integrity = (
            "sha512-"
            + base64.b64encode(h.digest()).decode("ascii")
        )
        artifact_id = _derive_artifact_id(integrity)

        # Write valid bytes *outside* the cache root tree.
        external_dir = os.path.join(self._tmpdir.name, "external")
        external_blob = os.path.join(external_dir, artifact_id)
        os.makedirs(os.path.dirname(external_blob), exist_ok=True)
        fd = os.open(
            external_blob, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            mode=0o600,
        )
        try:
            os.write(fd, self._artifact_bytes(url))
        finally:
            os.close(fd)

        # Symlink the algorithm directory to the external dir.
        algo = artifact_id.split("/", 1)[0]
        algo_dir = os.path.join(self._artifact_cache_root, algo)
        os.makedirs(self._artifact_cache_root, exist_ok=True)
        if os.path.lexists(algo_dir):
            os.remove(algo_dir)
        os.symlink(external_dir, algo_dir)

        req = self._request(
            dry_run=True,
            executor=_BombExecutor(),
            inspector=_BombInspector(),
            _create_projection=_BombProjectionFactory(),
        )
        result = self._run(req)
        self.assertEqual(result.exit_kind, ExitKind.SUCCESS)
        self.assertNotIn(
            artifact_id, result.artifact_cache_hits,
            "symlinked algorithm dir must not yield a hit",
        )
        self.assertIn(
            artifact_id, result.artifact_cache_misses,
            "symlinked algorithm dir must be a miss",
        )

    def test_dry_run_symlinked_algorithm_dir_internal_is_miss(self) -> None:
        """When the algorithm subdirectory is a symlink pointing
        to another directory *inside* the cache root, O_NOFOLLOW
        still rejects it as a miss."""
        import base64, hashlib
        from docker.versioning.model import _derive_artifact_id

        url = "https://registry.npmjs.org/pi-proxy/-/pi-proxy-1.0.0.tgz"
        h = hashlib.sha512(self._artifact_bytes(url))
        integrity = (
            "sha512-"
            + base64.b64encode(h.digest()).decode("ascii")
        )
        artifact_id = _derive_artifact_id(integrity)

        # Create a real directory *inside* the cache root with a
        # valid blob.
        real_algo_dir = os.path.join(
            self._artifact_cache_root, "real-sha512",
        )
        full_real_path = os.path.join(real_algo_dir,
                                      artifact_id.split("/", 1)[1])
        os.makedirs(os.path.dirname(full_real_path), exist_ok=True)
        fd = os.open(
            full_real_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            mode=0o600,
        )
        try:
            os.write(fd, self._artifact_bytes(url))
        finally:
            os.close(fd)

        # Symlink the algorithm directory to the internal real dir.
        algo = artifact_id.split("/", 1)[0]
        algo_dir = os.path.join(self._artifact_cache_root, algo)
        if os.path.lexists(algo_dir):
            os.remove(algo_dir)
        os.symlink(real_algo_dir, algo_dir)

        req = self._request(
            dry_run=True,
            executor=_BombExecutor(),
            inspector=_BombInspector(),
            _create_projection=_BombProjectionFactory(),
        )
        result = self._run(req)
        self.assertEqual(result.exit_kind, ExitKind.SUCCESS)
        self.assertNotIn(
            artifact_id, result.artifact_cache_hits,
            "internal symlinked algorithm dir must not yield a hit",
        )
        self.assertIn(
            artifact_id, result.artifact_cache_misses,
            "internal symlinked algorithm dir must be a miss",
        )

    def test_dry_run_symlinked_blob_is_miss(self) -> None:
        """A leaf blob that itself is a symlink is rejected
        by O_NOFOLLOW — zero bytes read, reported as a miss."""
        import base64, hashlib
        from docker.versioning.model import _derive_artifact_id

        url = "https://registry.npmjs.org/pi-proxy/-/pi-proxy-1.0.0.tgz"
        h = hashlib.sha512(self._artifact_bytes(url))
        integrity = (
            "sha512-"
            + base64.b64encode(h.digest()).decode("ascii")
        )
        artifact_id = _derive_artifact_id(integrity)

        # Write a valid blob at an alternative path, then
        # symlink the expected leaf path to it.
        real_blob_dir = os.path.join(
            self._artifact_cache_root, "real-blobs",
        )
        real_blob = os.path.join(real_blob_dir,
                                 artifact_id.split("/", 1)[1])
        os.makedirs(os.path.dirname(real_blob), exist_ok=True)
        fd = os.open(
            real_blob, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            mode=0o600,
        )
        try:
            os.write(fd, self._artifact_bytes(url))
        finally:
            os.close(fd)

        # Create the expected algorithm directory and symlink
        # the leaf blob name to the real blob.
        leaf_dir = os.path.join(
            self._artifact_cache_root,
            artifact_id.rsplit("/", 1)[0],
        )
        os.makedirs(leaf_dir, exist_ok=True)
        leaf_path = os.path.join(
            self._artifact_cache_root, artifact_id,
        )
        os.symlink(real_blob, leaf_path)

        req = self._request(
            dry_run=True,
            executor=_BombExecutor(),
            inspector=_BombInspector(),
            _create_projection=_BombProjectionFactory(),
        )
        result = self._run(req)
        self.assertEqual(result.exit_kind, ExitKind.SUCCESS)
        self.assertNotIn(
            artifact_id, result.artifact_cache_hits,
            "symlinked blob must not be reported as a hit",
        )
        self.assertIn(
            artifact_id, result.artifact_cache_misses,
            "symlinked blob must be reported as a miss",
        )

    def test_dry_run_does_not_mutate_cache(self) -> None:
        """Dry-run inspection SHALL NOT create, remove, rename,
        chmod, lock, or otherwise mutate any cache directory
        contents."""
        url = "https://registry.npmjs.org/@arcanemachine/pi-read/-/pi-read-0.2.0.tgz"
        self._make_blob(self._artifact_cache_root, url)

        # Snapshot the cache tree before dry-run.
        before = set()
        for dirpath, dirnames, filenames in os.walk(
            self._artifact_cache_root,
        ):
            for fn in filenames:
                fp = os.path.join(dirpath, fn)
                st = os.lstat(fp)
                before.add((
                    os.path.relpath(fp, self._artifact_cache_root),
                    st.st_mode,
                    st.st_mtime,
                    st.st_size,
                ))
            for dn in dirnames:
                dp = os.path.join(dirpath, dn)
                st = os.lstat(dp)
                before.add((
                    os.path.relpath(dp, self._artifact_cache_root),
                    st.st_mode,
                    st.st_mtime,
                    st.st_size,
                ))

        req = self._request(
            dry_run=True,
            executor=_BombExecutor(),
            inspector=_BombInspector(),
            _create_projection=_BombProjectionFactory(),
        )
        result = self._run(req)
        self.assertEqual(result.exit_kind, ExitKind.SUCCESS)

        after = set()
        for dirpath, dirnames, filenames in os.walk(
            self._artifact_cache_root,
        ):
            for fn in filenames:
                fp = os.path.join(dirpath, fn)
                st = os.lstat(fp)
                after.add((
                    os.path.relpath(fp, self._artifact_cache_root),
                    st.st_mode,
                    st.st_mtime,
                    st.st_size,
                ))
            for dn in dirnames:
                dp = os.path.join(dirpath, dn)
                st = os.lstat(dp)
                after.add((
                    os.path.relpath(dp, self._artifact_cache_root),
                    st.st_mode,
                    st.st_mtime,
                    st.st_size,
                ))

        self.assertEqual(
            before, after,
            "dry-run must not mutate cache (entries, modes, "
            "mtime, or sizes changed)",
        )

    def test_dry_run_preserves_blob_atime(self) -> None:
        """Reading a valid cache blob during dry-run inspection
        must not update its access time (``O_NOATIME``)."""
        url = "https://registry.npmjs.org/@arcanemachine/pi-read/-/pi-read-0.2.0.tgz"
        artifact_id, _test_bytes = self._make_blob(
            self._artifact_cache_root, url,
        )
        blob_path = os.path.join(
            self._artifact_cache_root, artifact_id,
        )

        # Set atime to a known value in the past so we can
        # detect any update.
        st_before = os.stat(blob_path)
        _past_atime = st_before.st_atime - 3600.0
        _past_mtime = st_before.st_mtime
        os.utime(blob_path, (_past_atime, _past_mtime))

        req = self._request(
            dry_run=True,
            executor=_BombExecutor(),
            inspector=_BombInspector(),
            _create_projection=_BombProjectionFactory(),
        )
        result = self._run(req)
        self.assertEqual(result.exit_kind, ExitKind.SUCCESS)

        st_after = os.stat(blob_path)
        self.assertAlmostEqual(
            st_after.st_atime, _past_atime,
            msg="dry-run must not update blob atime",
        )

    def test_dry_run_noatime_denied_is_miss(self) -> None:
        """When ``O_NOATIME`` is denied (``PermissionError``),
        ``inspect_verified_blob_readonly`` must return ``False``
        — never fall back to a plain read that would update atime."""
        from unittest import mock

        url = "https://registry.npmjs.org/@arcanemachine/pi-read/-/pi-read-0.2.1.tgz"
        artifact_id, _test_bytes = self._make_blob(
            self._artifact_cache_root, url,
        )

        # Intercept os.open: raise PermissionError for any call
        # that includes O_NOATIME, then delegate to the real
        # os.open for all other calls.
        _real_open = os.open
        _NOATIME = getattr(os, "O_NOATIME", 0x40000)

        def _guarded_open(path, flags, *args, **kwargs):
            if flags & _NOATIME:
                raise PermissionError(
                    "O_NOATIME not permitted (simulated)",
                )
            return _real_open(path, flags, *args, **kwargs)

        req = self._request(
            dry_run=True,
            executor=_BombExecutor(),
            inspector=_BombInspector(),
            _create_projection=_BombProjectionFactory(),
        )
        with mock.patch(
            "docker.versioning.artifact_cache.os.open",
            side_effect=_guarded_open,
        ):
            result = self._run(req)
        self.assertEqual(result.exit_kind, ExitKind.SUCCESS)
        self.assertNotIn(
            artifact_id, result.artifact_cache_hits,
            "O_NOATIME denied must not yield a hit",
        )
        self.assertIn(
            artifact_id, result.artifact_cache_misses,
            "O_NOATIME denied must report artifact as a miss",
        )
        # The blob on disk must still exist and be unchanged —
        # the miss was a rejection, not a deletion.
        blob_path = os.path.join(
            self._artifact_cache_root, artifact_id,
        )
        self.assertTrue(
            os.path.isfile(blob_path),
            "blob must survive noatime-denied dry-run unchanged",
        )

    def test_inspect_rejects_unsupported_algorithm(self) -> None:
        """``inspect_verified_blob_readonly`` must return
        ``False`` for an unsupported integrity algorithm without
        raising — even when a file happens to exist at the
        derived path."""
        from docker.versioning import artifact_cache as _ac

        # Write a file at the path that sha999-AQID.tgz would
        # resolve to — if the function didn't short-circuit on
        # the algorithm it would try to hash and fail.
        algo_dir = os.path.join(self._artifact_cache_root, "sha999")
        os.makedirs(algo_dir, exist_ok=True)
        blob_path = os.path.join(algo_dir, "AQID.tgz")
        fd = os.open(
            blob_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            mode=0o600,
        )
        try:
            os.write(fd, b"some bytes")
        finally:
            os.close(fd)

        result = _ac.inspect_verified_blob_readonly(
            "sha999-AQID",
            cache_root=self._artifact_cache_root,
        )
        self.assertFalse(
            result,
            "unsupported algorithm must return False",
        )

    def test_inspect_rejects_malformed_integrity(self) -> None:
        """``inspect_verified_blob_readonly`` must return
        ``False`` for malformed integrity strings (missing dash,
        non-base64 digest) rather than raising."""
        from docker.versioning import artifact_cache as _ac

        for malformed in (
            "not-an-sri",
            "sha512",
            "sha512-",
            "sha512-!!!",
            "",
        ):
            with self.subTest(integrity=malformed):
                result = _ac.inspect_verified_blob_readonly(
                    malformed,
                    cache_root=self._artifact_cache_root,
                )
                self.assertFalse(
                    result,
                    f"malformed integrity {malformed!r} "
                    "must return False",
                )

    # ── missing boundaries ───────────────────────────────────────

    def test_no_executor_and_not_dry_run_is_error(self) -> None:
        req = self._request(
            executor=None,
            inspector=FakeContainerNameInspector(set()),
        )
        result = self._run(req)
        self.assertEqual(result.exit_kind, ExitKind.OPERATIONAL)
        self.assertIsNotNone(result.message)

    def test_no_inspector_is_error(self) -> None:
        req = self._request(
            executor=FakeRunExecutor(returncode=0),
            inspector=None,
        )
        result = self._run(req)
        self.assertEqual(result.exit_kind, ExitKind.OPERATIONAL)
        self.assertIsNotNone(result.message)

    # ── runtime overrides ───────────────────────────────────────

    def test_valid_override_produces_success(self) -> None:
        """A recognised runtime override for an existing extension
        with a version that satisfies its policy must be accepted."""
        req = self._request(
            overrides={
                "runtime.pi-extensions.pi-read.version": "0.3.0",
            },
            executor=FakeRunExecutor(returncode=0),
            inspector=FakeContainerNameInspector(set()),
        )
        result = self._run(req)
        self.assertEqual(
            result.exit_kind, ExitKind.SUCCESS,
            f"valid override must succeed, got {result.exit_kind}: {result.message}",
        )

    def test_override_selects_alternate_artifact(self) -> None:
        """Overriding pi-read from the default 0.2.0 to 0.3.0 must
        produce a different projection than the default, proving the
        override reached artifact selection — not just no-op accepted."""
        # Default run (no overrides)
        default_result = self._run(self._request(
            executor=FakeRunExecutor(returncode=0),
            inspector=FakeContainerNameInspector(set()),
        ))
        self.assertEqual(default_result.exit_kind, ExitKind.SUCCESS)

        # Overridden run
        overridden_result = self._run(self._request(
            overrides={
                "runtime.pi-extensions.pi-read.version": "0.3.0",
            },
            executor=FakeRunExecutor(returncode=0),
            inspector=FakeContainerNameInspector(set()),
        ))
        self.assertEqual(overridden_result.exit_kind, ExitKind.SUCCESS)

        # The two projections must differ — different artifact selected.
        self.assertIsNotNone(default_result.projection_hash)
        self.assertIsNotNone(overridden_result.projection_hash)
        self.assertNotEqual(
            default_result.projection_hash,
            overridden_result.projection_hash,
            "override to alternate artifact must change the projection content",
        )

    def test_unknown_override_path_is_config_error(self) -> None:
        """An override path that does not match any known extension
        must be rejected before projection creation, Docker inspection,
        or Docker execution."""
        req = self._request(
            overrides={
                "runtime.pi-extensions.nonexistent.version": "1.0.0",
            },
            executor=_BombExecutor(),
            inspector=_BombInspector(),
            _create_projection=_BombProjectionFactory(),
        )
        result = self._run(req)
        self.assertEqual(
            result.exit_kind, ExitKind.CONFIG,
            f"unknown override must be CONFIG, got {result.exit_kind}",
        )
        # If any bomb exploded the test would have already failed.

    def test_build_scoped_override_rejected_in_run(self) -> None:
        """A build-scoped override path must not be accepted by
        the run transaction.  Validation must reject it before any
        side effect."""
        req = self._request(
            overrides={
                "build.stages.toolchain.python.version": "3.15.0",
            },
            executor=_BombExecutor(),
            inspector=_BombInspector(),
            _create_projection=_BombProjectionFactory(),
        )
        result = self._run(req)
        self.assertEqual(
            result.exit_kind, ExitKind.CONFIG,
            f"build override in run must be CONFIG, got {result.exit_kind}",
        )

    def test_override_version_not_in_catalog_is_config_error(self) -> None:
        """An override version that has no matching artifact catalog
        entry must be rejected before any side effect."""
        req = self._request(
            overrides={
                "runtime.pi-extensions.pi-read.version": "99.99.99",
            },
            executor=_BombExecutor(),
            inspector=_BombInspector(),
            _create_projection=_BombProjectionFactory(),
        )
        result = self._run(req)
        self.assertEqual(
            result.exit_kind, ExitKind.CONFIG,
            f"missing catalog entry must be CONFIG, got {result.exit_kind}",
        )

    def test_override_rejection_creates_no_projection(self) -> None:
        """When an override is rejected, the projection factory must
        never be called and no file must appear on disk."""
        req = self._request(
            overrides={
                "runtime.pi-extensions.nonexistent.version": "1.0.0",
            },
            executor=_BombExecutor(),
            inspector=_BombInspector(),
            _create_projection=_BombProjectionFactory(),
        )
        result = self._run(req)
        # If any bomb exploded the test would have already failed.
        if result.projection_path is not None:
            self.assertFalse(
                os.path.exists(result.projection_path),
                "rejected override must not create a projection file",
            )

    def test_default_no_overrides_produces_success(self) -> None:
        """With no overrides supplied, the default versions from
        the reviewed inventory must be used without error."""
        req = self._request(
            executor=FakeRunExecutor(returncode=0),
            inspector=FakeContainerNameInspector(set()),
        )
        result = self._run(req)
        self.assertEqual(
            result.exit_kind, ExitKind.SUCCESS,
            "default (no overrides) must succeed",
        )

    # ── projected paths are absolute ─────────────────────────────

    def test_projection_host_path_is_absolute(self) -> None:
        req = self._request(
            executor=FakeRunExecutor(returncode=0),
            inspector=FakeContainerNameInspector(set()),
        )
        result = self._run(req)
        self.assertTrue(
            (result.projection_path or "").startswith("/"),
            "projection path must be absolute",
        )

    def test_workspace_1to1_in_result(self) -> None:
        req = self._request(
            selection=WorkspaceSelection(
                workspace="/work/primary",
                extra_workspaces=("/work/extra",),
            ),
            executor=FakeRunExecutor(returncode=0),
            inspector=FakeContainerNameInspector(set()),
        )
        result = self._run(req)
        # Primary workspace mounted 1:1
        primary_spec = _parse_mount_spec(result.run_args, dst="/work/primary")
        self.assertEqual(primary_spec["src"], "/work/primary")
        # Extra workspace mounted 1:1
        opt_spec = _parse_mount_spec(result.run_args, dst="/work/extra")
        self.assertEqual(opt_spec["src"], "/work/extra")
        # Both exported
        self.assertIn("WORKSPACE_PATH_1=/work/primary", result.run_args)
        self.assertIn("WORKSPACE_PATH_2=/work/extra", result.run_args)

    # ── projection readability before execution ──────────────────

    def test_projection_readable_before_docker_run(self) -> None:
        """Before ``docker run`` executes, the projection file must
        be world-readable (0444) so container-remapped users can
        access it via the bind-mount.

        Exercises the real :func:`create_runtime_projection` through
        a wrapping spy — the file on disk is created by the
        production code, not by a test double."""
        import os
        import stat

        from docker.versioning.effective import create_runtime_projection

        # The real factory must publish beneath the resolved external runtime
        # directory supplied by orchestration.
        import uuid
        try:
            recorded: list[tuple[str, str]] = []  # (path, content_hash)
            observed_modes: list[int] = []

            class _WrappingFactory:
                """Spy that delegates to the real projection factory."""

                def __call__(self, projection: object, *,
                             parent_dir: str) -> Any:
                    from docker.versioning.effective import Filesystem
                    handle = create_runtime_projection(
                        projection,
                        host_path=os.path.join(parent_dir, f"proj-{uuid.uuid4().hex}.toml"),
                        _fs=Filesystem(runtime_root=parent_dir),
                    )
                    recorded.append((handle.path, handle.content_hash))
                    return handle

            class _AssertingExecutor:
                def run(self, argv: tuple[str, ...], *,
                        interactive: bool = False) -> Any:
                    for path, _ in recorded:
                        if os.path.exists(path):
                            mode = os.stat(path).st_mode & 0o777
                            observed_modes.append(mode)
                    from docker.launcher import ProcessResult
                    return ProcessResult(
                        argv=argv, return_code=0,
                        stdout="", stderr="",
                    )

            req = self._request(
                executor=_AssertingExecutor(),
                inspector=FakeContainerNameInspector(set()),
                _create_projection=_WrappingFactory(),
            )

            result = self._run(req)
            # Sanity: the wrapper was called and the executor ran.
            self.assertTrue(
                len(recorded) >= 1,
                "wrapping factory was not called — projection creation "
                "may have failed before execution",
            )
            self.assertTrue(
                len(observed_modes) >= 1,
                "executor was never reached — projection created but "
                "execution did not proceed",
            )
            for mode in observed_modes:
                self.assertEqual(
                    0o444, mode,
                    f"projection mode {oct(mode)} before docker run "
                    f"— expected 0o444 for remapped-container access",
                )
            # Content hash was recorded from the real factory.
            self.assertIsNotNone(recorded[0][1])
        finally:
            for path, _ in recorded:
                if os.path.exists(path):
                    os.unlink(path)


    # ── artifact deduplication preserves package metadata ─────

    def test_shared_integrity_preserves_distinct_package_metadata_and_produces_one_mount(self) -> None:
        """Two packages sharing one integrity materialize once and
        produce one Docker mount, yet both projection entries retain
        their distinct package, version, and validation metadata."""
        import base64
        import hashlib
        import re

        # Add a second extension with its own valid npm URL.  We
        # then assign both entries the same integrity (derived from
        # pi-read's bytes) and override the fetcher to return those
        # bytes for both URLs.
        pi_shared_url = (
            "https://registry.npmjs.org/@arcanemachine/pi-shared/"
            "-/pi-shared-1.0.0.tgz"
        )
        shared_section = (
            '\n'
            '[runtime.pi-extensions.pi-shared]\n'
            'version = "1.0.0"\n'
            '[runtime.pi-extensions.pi-shared.source]\n'
            'type = "npm"\n'
            'package = "@arcanemachine/pi-shared"\n'
            '[runtime.pi-extensions.pi-shared.artifacts."1.0.0"]\n'
            f'url = "{pi_shared_url}"\n'
            'integrity = "sha512-placeholder"\n'
            '[runtime.pi-extensions.pi-shared.validation]\n'
            'metadata_file = "shared-package.json"\n'
            '[runtime.pi-extensions.pi-shared.update]\n'
            'provider = "npm"\n'
            'stable_only = true\n'
            '[runtime.pi-extensions.pi-shared.override]\n'
            'constraint = ">=1.0.0"\n'
            'allow_prerelease = false\n'
            'scheme = "numeric"\n'
        )
        with open(self._inventory_path, "a") as fh:
            fh.write(shared_section)

        # Recompute integrities.  The placeholder is replaced as
        # usual, but then we overwrite pi-shared's integrity with
        # the same value as pi-read's so both share one identity.
        with open(self._inventory_path) as fh:
            content = fh.read()

        def _replace_integrity(match: re.Match[str]) -> str:
            url = match.group(1)
            digest = base64.b64encode(
                hashlib.sha512(self._artifact_bytes(url)).digest()
            ).decode("ascii")
            return f'url = "{url}"\nintegrity = "sha512-{digest}"'

        content = re.sub(
            r'url = "([^"]+)"\nintegrity = "[^"]+"',
            _replace_integrity,
            content,
        )

        # Extract pi-read's integrity and assign it to pi-shared.
        pi_read_url = (
            "https://registry.npmjs.org/@arcanemachine/pi-read/"
            "-/pi-read-0.2.1.tgz"
        )
        shared_integrity_digest = base64.b64encode(
            hashlib.sha512(self._artifact_bytes(pi_read_url)).digest()
        ).decode("ascii")
        shared_integrity = f"sha512-{shared_integrity_digest}"
        content = content.replace(
            f'url = "{pi_shared_url}"\nintegrity = "sha512-'
            + base64.b64encode(
                hashlib.sha512(self._artifact_bytes(pi_shared_url)).digest()
            ).decode("ascii")
            + '"',
            f'url = "{pi_shared_url}"\nintegrity = "{shared_integrity}"',
        )
        with open(self._inventory_path, "w") as fh:
            fh.write(content)

        # Shared bytes for the two entries that share integrity;
        # all other fetches use the original artifact bytes.
        _orig_fetch = self._artifact_bytes
        shared_bytes = self._artifact_bytes(pi_read_url)

        def _shared_fetcher(url: str) -> bytes:
            if url in (pi_read_url, pi_shared_url):
                return shared_bytes
            return _orig_fetch(url)

        factory = RecordingProjectionFactory()
        req = self._request(
            _create_projection=factory,
            _artifact_fetcher=_shared_fetcher,
            executor=FakeRunExecutor(returncode=0),
            inspector=FakeContainerNameInspector(set()),
        )
        result = self._run(req)
        self.assertEqual(result.exit_kind, ExitKind.SUCCESS,
                         f"unexpected {result.exit_kind}: {result.message}")

        # ── projection metadata ────────────────────────────
        self.assertEqual(len(factory.calls), 1)
        recorded_proj = factory.calls[0][0]
        ext = getattr(recorded_proj, "extensions", {})
        self.assertIn("pi-read", ext)
        self.assertIn("pi-shared", ext)
        pi_read_entry = ext["pi-read"]
        pi_shared_entry = ext["pi-shared"]
        self.assertEqual(pi_read_entry.package, "@arcanemachine/pi-read")
        self.assertEqual(pi_shared_entry.package, "@arcanemachine/pi-shared")
        self.assertEqual(pi_read_entry.version, "0.2.1")
        self.assertEqual(pi_shared_entry.version, "1.0.0")
        self.assertEqual(pi_read_entry.metadata_file, "package.json")
        self.assertEqual(pi_shared_entry.metadata_file, "shared-package.json")
        self.assertEqual(
            pi_read_entry.integrity,
            pi_shared_entry.integrity,
            "both entries must share the same integrity",
        )

        # ── one Docker mount for the shared blob ────────────
        # Mounts appear as "--mount" followed by
        # "type=bind,src=...,dst=/run/pi-cli/runtime-artifacts/...,readonly"
        # in consecutive argv entries.
        artifact_mount_targets = [
            a for a in result.run_args
            if a.startswith("type=bind,")
            and "/runtime-artifacts/" in a
        ]
        # pi-read/pi-shared share one blob, plus pi-usage and pi-proxy = 3 total
        self.assertEqual(len(artifact_mount_targets), 3, artifact_mount_targets)
        # The shared blob's mount is referenced once (not duplicated).
        shared_digest = shared_integrity_digest.replace("+", "-").replace("/", "_")
        shared_mounts = [
            a for a in artifact_mount_targets if shared_digest in a
        ]
        self.assertEqual(len(shared_mounts), 1, shared_mounts)
        self.assertIn("readonly", shared_mounts[0])

    # ── cache-hit offline ─────────────────────────────────────__

    def test_cache_hit_performs_no_network_request(self) -> None:
        """When every selected artifact is already cached,
        ``orchestrate_run`` performs zero extension artifact
        network requests — the injected fetcher is never called."""
        # Pre-populate the cache by materializing all selected
        # artifacts through the real pipeline.
        req1 = self._request(
            _artifact_fetcher=self._artifact_bytes,
            executor=FakeRunExecutor(returncode=0),
            inspector=FakeContainerNameInspector(set()),
        )
        result1 = self._run(req1)
        self.assertEqual(
            result1.exit_kind, ExitKind.SUCCESS,
            f"pre-population run must succeed: {result1.message}",
        )

        # Second run with a fetcher that records calls and raises
        # if invoked — proving the cache fully satisfied every
        # artifact.
        calls: list[str] = []

        def _no_network(url: str) -> bytes:
            calls.append(url)
            raise AssertionError(
                f"unexpected artifact fetch for {url}"
            )

        req2 = self._request(
            _artifact_fetcher=_no_network,
            executor=FakeRunExecutor(returncode=0),
            inspector=FakeContainerNameInspector(set()),
        )
        result2 = self._run(req2)
        self.assertEqual(
            result2.exit_kind, ExitKind.SUCCESS,
            f"cache-hit run must succeed: {result2.message}",
        )
        self.assertEqual(
            calls, [],
            "no artifact URL must be fetched when all blobs are "
            "cached — transport must not be invoked",
        )

    def test_cache_miss_transport_failure_fails_before_docker(self) -> None:
        """When a selected artifact is not in the cache and the
        transport fails, ``orchestrate_run`` returns an
        OPERATIONAL failure, never executes Docker, and includes
        an actionable diagnostic with the transport failure detail
        and the selected artifact URL."""
        executor = FakeRunExecutor(returncode=0)

        def _failing_fetcher(url: str) -> bytes:
            raise RuntimeError(
                f"simulated transport failure for {url}"
            )

        req = self._request(
            _artifact_fetcher=_failing_fetcher,
            executor=executor,
            inspector=FakeContainerNameInspector(set()),
        )
        result = self._run(req)
        self.assertEqual(
            result.exit_kind, ExitKind.OPERATIONAL,
            f"transport failure must return OPERATIONAL, "
            f"got {result.exit_kind}: {result.message}",
        )
        self.assertIn(
            "Failed to materialize runtime artifacts",
            result.message or "",
            "failure message must identify materialization as the cause",
        )
        self.assertIn(
            "simulated transport failure",
            result.message or "",
            "failure message must preserve the original transport "
            "failure detail so callers can diagnose the cause",
        )
        self.assertIn(
            "registry.npmjs.org",
            result.message or "",
            "failure message must include the selected artifact URL "
            "that could not be fetched",
        )
        self.assertEqual(
            len(executor.calls), 0,
            "Docker must not execute when artifact materialization fails",
        )


# ═══════════════════════════════════════════════════════════════════
# 11.3 - Docker-backed boundaries (ProcessRunner injection)
# ═══════════════════════════════════════════════════════════════════


class TestDockerContainerInspector(unittest.TestCase):
    """Contract for :class:`DockerContainerInspector` — runs
    ``docker ps -a`` through an injected :class:`ProcessRunner`.
    All tests are daemon-independent."""

    def test_passes_correct_argv(self) -> None:
        runner = FakeProcessRunner([
            ProcessResult(
                argv=("docker", "ps", "-a", "--format", "{{.Names}}"),
                return_code=0,
                stdout="pi-1\npi-2\n",
            ),
        ])
        inspector = DockerContainerInspector(runner)
        names = inspector.list_names()
        self.assertEqual(names, {"pi-1", "pi-2"})
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(
            runner.calls[0],
            ["docker", "ps", "-a", "--format", "{{.Names}}"],
        )

    def test_parses_docker_ps_output(self) -> None:
        runner = FakeProcessRunner([
            ProcessResult(
                argv=(),
                return_code=0,
                stdout="pi-1\npi-3\npi-7\n",
            ),
        ])
        inspector = DockerContainerInspector(runner)
        self.assertEqual(inspector.list_names(), {"pi-1", "pi-3", "pi-7"})

    def test_empty_output_no_containers(self) -> None:
        runner = FakeProcessRunner([
            ProcessResult(argv=(), return_code=0, stdout=""),
        ])
        inspector = DockerContainerInspector(runner)
        self.assertEqual(inspector.list_names(), set())

    def test_whitespace_only_output(self) -> None:
        runner = FakeProcessRunner([
            ProcessResult(argv=(), return_code=0, stdout="\n  \n"),
        ])
        inspector = DockerContainerInspector(runner)
        self.assertEqual(inspector.list_names(), set())

    def test_docker_unavailable_raises_inspect_error(self) -> None:
        runner = FakeProcessRunner([
            ProcessResult(
                argv=(),
                return_code=1,
                stderr="Cannot connect to the Docker daemon",
            ),
        ])
        inspector = DockerContainerInspector(runner)
        with self.assertRaises(ContainerInspectError):
            inspector.list_names()

    def test_process_runner_oserror_wrapped_as_inspect_error(self) -> None:
        """ContainerNameInspector promises ContainerInspectError when
        Docker is unavailable.  Raw OSError from the process runner
        must be caught and wrapped so callers only handle the
        documented domain error."""
        class BrokenRunner(ProcessRunner):
            def run(self, argv: list[str], *,
                    mode: ExecutionMode = ExecutionMode.CAPTURED,
                    ) -> ProcessResult:
                raise OSError("docker not found")

        inspector = DockerContainerInspector(BrokenRunner())
        with self.assertRaises(ContainerInspectError) as ctx:
            inspector.list_names()
        self.assertIn("docker not found", str(ctx.exception))

    def test_duplicate_names_in_output(self) -> None:
        """If docker ps returns duplicates, they collapse to a single set entry."""
        runner = FakeProcessRunner([
            ProcessResult(
                argv=(),
                return_code=0,
                stdout="pi-1\npi-1\npi-2\n",
            ),
        ])
        inspector = DockerContainerInspector(runner)
        self.assertEqual(inspector.list_names(), {"pi-1", "pi-2"})


class TestDockerRunExecutor(unittest.TestCase):
    """Contract for :class:`DockerRunExecutor` — passes the rendered
    ``docker`` argument vector through an injected
    :class:`ProcessRunner`.  All tests are daemon-independent."""

    def test_passes_argv_through_to_runner(self) -> None:
        runner = FakeProcessRunner([
            ProcessResult(
                argv=("docker", "run", "--rm", "alpine"),
                return_code=0,
            ),
        ])
        executor = DockerRunExecutor(runner)
        result = executor.run(("docker", "run", "--rm", "alpine"))
        self.assertEqual(result.return_code, 0)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(
            runner.calls[0],
            ["docker", "run", "--rm", "alpine"],
        )

    def test_returns_process_result_unchanged(self) -> None:
        canned = ProcessResult(
            argv=("docker", "run", "img"),
            return_code=0,
            stdout="hello",
            stderr="",
        )
        runner = FakeProcessRunner([canned])
        executor = DockerRunExecutor(runner)
        result = executor.run(("docker", "run", "img"))
        self.assertEqual(result.argv, ("docker", "run", "img"))
        self.assertEqual(result.return_code, 0)
        self.assertEqual(result.stdout, "hello")
        self.assertEqual(result.stderr, "")

    def test_nonzero_exit_preserved(self) -> None:
        runner = FakeProcessRunner([
            ProcessResult(
                argv=("docker", "run", "bad"),
                return_code=127,
                stderr="image not found",
            ),
        ])
        executor = DockerRunExecutor(runner)
        result = executor.run(("docker", "run", "bad"))
        self.assertEqual(result.return_code, 127)
        self.assertIn("image not found", result.stderr)

    def test_process_runner_oserror_propagates(self) -> None:
        class BrokenRunner(ProcessRunner):
            def run(self, argv: list[str], *,
                    mode: ExecutionMode = ExecutionMode.CAPTURED,
                    ) -> ProcessResult:
                raise OSError("docker not found")

        executor = DockerRunExecutor(BrokenRunner())
        with self.assertRaises(OSError):
            executor.run(("docker", "run", "img"))

    # ── execution mode ────────────────────────────────────────

    def test_interactive_mode_disables_capture_and_inherits_stdin(self) -> None:
        """In interactive mode the executor must instruct the
        runner to skip ``capture_output`` and inherit stdin so
        Docker can attach to the host terminal."""
        mode_seen: list[dict[str, object]] = []

        class _SpyRunner(ProcessRunner):
            def run(self, argv: list[str], *,
                    mode: ExecutionMode = ExecutionMode.CAPTURED,
                    ) -> ProcessResult:
                mode_seen.append({"mode": mode})
                return ProcessResult(argv=tuple(argv), return_code=0)

        executor = DockerRunExecutor(_SpyRunner())
        # interactive=True → mode is INTERACTIVE
        executor.run(("docker", "run", "--tty", "img"),
                     interactive=True)
        self.assertTrue(
            len(mode_seen) >= 1,
            "executor must invoke the runner",
        )
        self.assertIs(
            mode_seen[0]["mode"], ExecutionMode.INTERACTIVE,
            "interactive mode must disable capture_output "
            "so Docker inherits the host terminal",
        )

    def test_captured_mode_preserves_capture_output(self) -> None:
        """When running without ``--tty``/``--interactive``,
        ``capture_output`` must remain ``True`` so the facade can
        surface diagnostics from captured stdout/stderr."""
        mode_seen: list[dict[str, object]] = []

        class _SpyRunner(ProcessRunner):
            def run(self, argv: list[str], *,
                    mode: ExecutionMode = ExecutionMode.CAPTURED,
                    ) -> ProcessResult:
                mode_seen.append({"mode": mode})
                return ProcessResult(argv=tuple(argv), return_code=0)

        executor = DockerRunExecutor(_SpyRunner())
        # interactive=False → mode is CAPTURED
        executor.run(("docker", "run", "img"),
                     interactive=False)
        self.assertTrue(
            len(mode_seen) >= 1,
            "executor must invoke the runner",
        )
        self.assertIs(
            mode_seen[0]["mode"], ExecutionMode.CAPTURED,
            "captured mode must enable capture_output "
            "so diagnostics are available",
        )


class TestProcessRunnerExecutionModes(unittest.TestCase):
    """Contract for :class:`ProcessRunner` execution modes.

    ``ProcessRunner.run()`` must support both interactive (terminal-
    attached) and captured (diagnostics-preserving) execution."""

    def test_interactive_mode_skips_capture_output(self) -> None:
        """When ``capture_output=False``, ``subprocess.run`` must
        **not** be called with ``capture_output=True`` — the host
        terminal must be inherited."""
        import subprocess as _subprocess_module

        original_run = _subprocess_module.run
        captured_kwargs: list[dict[str, object]] = []

        def _fake_subprocess_run(argv: list[str], **kw: object) -> object:
            captured_kwargs.append(kw)
            from subprocess import CompletedProcess
            return CompletedProcess(argv, 0, stdout="", stderr="")

        _subprocess_module.run = _fake_subprocess_run  # type: ignore[assignment]
        try:
            runner = ProcessRunner()
            runner.run(["docker", "run", "--tty", "img"],
                       mode=ExecutionMode.INTERACTIVE)
            self.assertTrue(
                len(captured_kwargs) >= 1,
                "subprocess.run must be called exactly once",
            )
            self.assertFalse(
                captured_kwargs[0].get("capture_output"),
                "interactive mode must not capture output — "
                "subprocess.run must receive capture_output=False",
            )
        finally:
            _subprocess_module.run = original_run  # type: ignore[assignment]

    def test_interactive_mode_inherits_std_streams(self) -> None:
        """Beyond ``capture_output=False``, interactive execution
        must pass ``stdin=None, stdout=None, stderr=None`` to
        ``subprocess.run`` so the container inherits the host
        terminal rather than receiving /dev/null or pipes."""
        import subprocess as _subprocess_module

        original_run = _subprocess_module.run
        captured_kwargs: list[dict[str, object]] = []

        def _fake_subprocess_run(argv: list[str], **kw: object) -> object:
            captured_kwargs.append(kw)
            from subprocess import CompletedProcess
            return CompletedProcess(argv, 0, stdout=None, stderr=None)

        _subprocess_module.run = _fake_subprocess_run  # type: ignore[assignment]
        try:
            runner = ProcessRunner()
            runner.run(["docker", "run", "--tty", "img"],
                       mode=ExecutionMode.INTERACTIVE)
            self.assertTrue(
                len(captured_kwargs) >= 1,
                "subprocess.run must be called exactly once",
            )
            kw = captured_kwargs[0]
            self.assertIsNone(
                kw.get("stdin"),
                "interactive mode must inherit stdin (stdin=None)",
            )
            self.assertIsNone(
                kw.get("stdout"),
                "interactive mode must inherit stdout (stdout=None)",
            )
            self.assertIsNone(
                kw.get("stderr"),
                "interactive mode must inherit stderr (stderr=None)",
            )
            self.assertFalse(
                kw.get("capture_output"),
                "interactive mode must disable capture_output",
            )
        finally:
            _subprocess_module.run = original_run  # type: ignore[assignment]

    def test_captured_mode_defaults_to_capture_enabled(self) -> None:
        """Without an explicit mode flag, ``capture_output`` must
        remain ``True`` so existing diagnostics continue to work."""
        import subprocess as _subprocess_module

        original_run = _subprocess_module.run
        captured_kwargs: list[dict[str, object]] = []

        def _fake_subprocess_run(argv: list[str], **kw: object) -> object:
            captured_kwargs.append(kw)
            from subprocess import CompletedProcess
            return CompletedProcess(argv, 0, stdout="ok", stderr="")

        _subprocess_module.run = _fake_subprocess_run  # type: ignore[assignment]
        try:
            runner = ProcessRunner()
            # Current behaviour: always captures. This must stay true
            # when no mode flag is passed.
            result = runner.run(["docker", "run", "img"])
            self.assertEqual(result.stdout, "ok")
            self.assertTrue(
                captured_kwargs and captured_kwargs[0].get("capture_output"),
                "default mode must capture output for diagnostics",
            )
        finally:
            _subprocess_module.run = original_run  # type: ignore[assignment]

    def test_captured_mode_forbids_tty_allocation(self) -> None:
        """Captured (non-interactive) execution must **not**
        pass ``--tty`` or ``--interactive`` to Docker.
        ``capture_output`` alone must handle ``stdout`` /
        ``stderr``; stdin defaults to inherit (the existing
        behaviour is sufficient)."""
        import subprocess as _subprocess_module

        original_run = _subprocess_module.run
        captured_kwargs: list[dict[str, object]] = []
        captured_argv: list[list[str]] = []

        def _fake_subprocess_run(argv: list[str], **kw: object) -> object:
            captured_kwargs.append(kw)
            captured_argv.append(argv)
            from subprocess import CompletedProcess
            return CompletedProcess(argv, 0, stdout="", stderr="")

        _subprocess_module.run = _fake_subprocess_run  # type: ignore[assignment]
        try:
            runner = ProcessRunner()
            # ``capture_output`` yet.
            runner.run(["docker", "run", "img"],
                       mode=ExecutionMode.CAPTURED)
            self.assertTrue(
                len(captured_kwargs) >= 1,
                "subprocess.run must be called",
            )
            kw = captured_kwargs[0]
            # Captured mode must still capture stdout/stderr.
            self.assertTrue(
                kw.get("capture_output"),
                "captured mode must enable capture_output",
            )
            # No TTY allocation in the docker argv.
            flat_argv = " ".join(captured_argv[0])
            self.assertNotIn(
                "--tty", flat_argv,
                "captured mode must not pass --tty to Docker",
            )
            self.assertNotIn(
                "--interactive", flat_argv,
                "captured mode must not pass --interactive to Docker",
            )
        finally:
            _subprocess_module.run = original_run  # type: ignore[assignment]


class TestOrchestrateRunExecutionModes(TestRunTransaction):
    """When ``orchestrate_run`` receives a mode-aware executor,
    it must dispatch interactive vs captured mode based on the
    ``tty`` / ``stdin_open`` flags in :class:`RunRequest`."""

    def setUp(self) -> None:
        super().setUp()

    def test_tty_true_invokes_executor_in_interactive_mode(self) -> None:
        """When ``RunRequest.tty=True``, the executor must be
        invoked with ``interactive=True`` so Docker inherits the
        host terminal."""
        modes_seen: list[dict[str, object]] = []

        class _SpyExecutor:
            def run(self, argv: tuple[str, ...], *,
                    interactive: bool = False) -> ProcessResult:
                modes_seen.append({"interactive": interactive})
                return ProcessResult(argv=argv, return_code=0)

        req = self._request(
            tty=True,
            stdin_open=True,
            executor=_SpyExecutor(),
            inspector=FakeContainerNameInspector(set()),
        )
        result = self._run(req)
        self.assertEqual(result.exit_kind, ExitKind.SUCCESS)
        self.assertTrue(
            len(modes_seen) >= 1,
            "executor was never invoked",
        )
        # RED: orchestrator never passes interactive=True.
        # The default False silently masks the missing signal.
        self.assertTrue(
            modes_seen[0]["interactive"],
            "orchestrate_run must pass interactive=True to the "
            "executor when tty=True",
        )

    def test_no_tty_invokes_executor_in_captured_mode(self) -> None:
        """When ``RunRequest.tty=False``, the executor must be
        invoked with ``interactive=False`` so stdout/stderr are
        captured for diagnostics."""
        modes_seen: list[dict[str, object]] = []

        class _SpyExecutor:
            def run(self, argv: tuple[str, ...], *,
                    interactive: bool = True) -> ProcessResult:
                modes_seen.append({"interactive": interactive})
                return ProcessResult(argv=argv, return_code=0)

        req = self._request(
            tty=False,
            stdin_open=False,
            executor=_SpyExecutor(),
            inspector=FakeContainerNameInspector(set()),
        )
        result = self._run(req)
        self.assertEqual(result.exit_kind, ExitKind.SUCCESS)
        self.assertTrue(
            len(modes_seen) >= 1,
            "executor was never invoked",
        )
        # RED: orchestrator never passes interactive=False.
        # The default True silently produces the wrong mode.
        self.assertFalse(
            modes_seen[0]["interactive"],
            "orchestrate_run must pass interactive=False to the "
            "executor when tty=False",
        )

    def test_stdin_open_without_tty_is_still_interactive(self) -> None:
        """Streaming is the policy when **either** ``tty`` or
        ``stdin_open`` is enabled — not only when both are.
        ``(tty=False, stdin_open=True)`` must still dispatch
        ``interactive=True`` so stdin can reach the container."""
        modes_seen: list[dict[str, object]] = []

        class _SpyExecutor:
            def run(self, argv: tuple[str, ...], *,
                    interactive: bool = False) -> ProcessResult:
                modes_seen.append({"interactive": interactive})
                return ProcessResult(argv=argv, return_code=0)

        req = self._request(
            tty=False,
            stdin_open=True,
            executor=_SpyExecutor(),
            inspector=FakeContainerNameInspector(set()),
        )
        result = self._run(req)
        self.assertEqual(result.exit_kind, ExitKind.SUCCESS)
        self.assertTrue(
            len(modes_seen) >= 1,
            "executor was never invoked",
        )
        # RED: orchestrator never passes interactive=True.
        self.assertTrue(
            modes_seen[0]["interactive"],
            "orchestrate_run must pass interactive=True when "
            "stdin_open=True (even with tty=False)",
        )

    def test_tty_without_stdin_open_is_still_interactive(self) -> None:
        """Streaming is the policy when **either** ``tty`` or
        ``stdin_open`` is enabled.  ``(tty=True, stdin_open=False)``
        must dispatch ``interactive=True`` so container output is
        visible immediately."""
        modes_seen: list[dict[str, object]] = []

        class _SpyExecutor:
            def run(self, argv: tuple[str, ...], *,
                    interactive: bool = False) -> ProcessResult:
                modes_seen.append({"interactive": interactive})
                return ProcessResult(argv=argv, return_code=0)

        req = self._request(
            tty=True,
            stdin_open=False,
            executor=_SpyExecutor(),
            inspector=FakeContainerNameInspector(set()),
        )
        result = self._run(req)
        self.assertEqual(result.exit_kind, ExitKind.SUCCESS)
        self.assertTrue(
            len(modes_seen) >= 1,
            "executor was never invoked",
        )
        # RED: orchestrator never passes interactive=True.
        self.assertTrue(
            modes_seen[0]["interactive"],
            "orchestrate_run must pass interactive=True when "
            "tty=True (even with stdin_open=False)",
        )

    def test_executor_oserror_maps_to_operational(self) -> None:
        """When the executor raises :class:`OSError` — Docker
        not installed, permission denied, binary not found —
        ``orchestrate_run`` must catch it and return an
        ``OPERATIONAL`` result with the original error message
        preserved so the facade can surface actionable
        diagnostics.

        The failure must surface even when the orchestrator
        has correctly dispatched ``interactive=True`` based on
        ``tty``/``stdin_open`` flags — the mode signal must
        reach the executor before the crash."""
        _oserror_message = (
            "[Errno 2] No such file or directory: 'docker'"
        )
        mode_seen: list[dict[str, object]] = []

        class _CrashingExecutor:
            def run(self, argv: tuple[str, ...], *,
                    interactive: bool = False) -> ProcessResult:
                mode_seen.append({"interactive": interactive})
                raise OSError(_oserror_message)

        req = self._request(
            tty=True,
            stdin_open=True,
            executor=_CrashingExecutor(),
            inspector=FakeContainerNameInspector(set()),
        )
        result = self._run(req)
        # RED: orchestrator never passes interactive=True.
        self.assertTrue(
            len(mode_seen) >= 1,
            "executor was never invoked",
        )
        self.assertTrue(
            mode_seen[0]["interactive"],
            "orchestrate_run must pass interactive=True "
            "before the executor raises OSError",
        )
        self.assertEqual(
            result.exit_kind, ExitKind.OPERATIONAL,
            "OSError during interactive execution must map "
            "to OPERATIONAL",
        )
        self.assertIn(
            _oserror_message, result.message or "",
            "OPERATIONAL result must preserve the original "
            "OSError message for diagnostics",
        )


class TestEndToEndPlanningGuards(TestRunTransaction):
    """RED — invalid inventory, override, artifact identity, cache
    root, or mount target must fail **before** cache mutation,
    projection publication, gateway effects, or Docker execution."""

    # ── malformed inventory helpers ────────────────────────────

    @staticmethod
    def _make_malformed_inventory(
        base_path: str,
        *,
        bad_integrity: str,
    ) -> str:
        """Copy the real inventory and replace the *existing*
        ``pi-read`` 0.2.1 integrity line with *bad_integrity*.

        The replacement must be a valid-base64, correct-prefix
        value so the TOML parses successfully and the model regex
        accepts it — the failure point is the missing semantic
        artifact-identity / mount-target validation during run
        planning, not TOML-level integrity rejection."""
        import shutil

        real = os.path.abspath(
            os.path.join(
                os.path.dirname(__file__),
                "..", "docker-constructor.toml",
            ),
        )
        fixture = os.path.join(base_path, "malformed.toml")
        shutil.copy2(real, fixture)
        with open(fixture) as fh:
            text = fh.read()
        # The existing pi-read 0.2.1 integrity line:
        original = (
            'integrity = "sha512-Vq1axnAU513JW4oqYkSIO4bQZFTPFwXz6V70n4'
            'fPNPplcDqK2bTx23viML88+0CrmjgvmvTVuG1ZruNCShOyiA=="'
        )
        replaced = text.replace(original, f'integrity = "{bad_integrity}"')
        if replaced == text:
            raise RuntimeError(
                "Failed to substitute integrity in fixture — "
                "the expected original line was not found"
            )
        with open(fixture, "w") as fh:
            fh.write(replaced)
        return fixture

    @staticmethod
    def _make_single_extension_fixture(
        base_path: str,
        *,
        bad_integrity: str,
    ) -> str:
        """Create a minimal inventory TOML containing **only**
        the ``pi-read`` extension with a deliberately wrong
        *bad_integrity* (valid-SRI format, wrong digest).

        No other extensions are present, so a correct
        materializer fetches exactly the pi-read URL and fails
        at digest comparison — the call-count assertion is
        deterministic."""
        fixture = os.path.join(base_path, "single-ext.toml")
        canonical = os.path.abspath(os.path.join(
            os.path.dirname(__file__), "..", "docker-constructor.toml",
        ))
        with open(canonical) as source:
            build_prefix = source.read().split(
                "\n[runtime.pi-extensions.", 1,
            )[0]
        with open(fixture, "w") as fh:
            fh.write(build_prefix + "\n\n")
            fh.write(
                '[runtime.pi-extensions.pi-read]\n'
                'version = "0.2.0"\n'
                '\n'
                '[runtime.pi-extensions.pi-read.source]\n'
                'type = "npm"\n'
                'package = "@arcanemachine/pi-read"\n'
                '\n'
                '[runtime.pi-extensions.pi-read.artifacts."0.2.0"]\n'
                'url = "https://registry.npmjs.org/@arcanemachine'
                '/pi-read/-/pi-read-0.2.0.tgz"\n'
                f'integrity = "{bad_integrity}"\n'
                '\n'
                '[runtime.pi-extensions.pi-read.update]\n'
                'provider = "npm"\n'
                'stable_only = true\n'
                '\n'
                '[runtime.pi-extensions.pi-read.override]\n'
                'constraint = ">=0.2.0"\n'
                'allow_prerelease = false\n'
                'scheme = "numeric"\n'
                '\n'
                '[runtime.pi-extensions.pi-read.validation]\n'
                'metadata_file = "package.json"\n'
            )
        return fixture

    # ── spies ──────────────────────────────────────────────────

    def setUp(self) -> None:
        super().setUp()
        self._stages: list[str] = []

    class _StageSpy:
        """Records ordered stage traversal."""

        def __init__(self, stages: list[str]) -> None:
            self._stages = stages

        def record(self, name: str) -> None:
            self._stages.append(name)

    @staticmethod
    def _spy_factory(
        stages: list[str],
    ):
        """Projection factory that records when it was called."""

        class _Spy(RecordingProjectionFactory):
            def __call__(self, projection, *, parent_dir):
                stages.append("projection")
                return super().__call__(projection, parent_dir=parent_dir)

        return _Spy()

    @staticmethod
    def _spy_executor(
        stages: list[str],
        returncode: int = 0,
    ):
        """Executor that records invocation."""

        class _Spy:
            def run(
                self,
                argv: tuple[str, ...],
                *,
                interactive: bool = False,
            ) -> object:
                from docker.launcher import ProcessResult
                stages.append("executor")
                return ProcessResult(argv=argv, return_code=returncode)

        return _Spy()

    # ── assertion helpers ──────────────────────────────────────

    def _assert_never_reached(self, stage: str) -> None:
        self.assertNotIn(
            stage, self._stages,
            f"{stage} stage reached — should have been blocked; "
            f"stages={self._stages}",
        )

    @staticmethod
    def _gateway_effect_triggered(
        result: object,
    ) -> bool:
        """True when the rendered Docker args include ``--add-host``
        (the persisted gateway effect)."""
        return (
            hasattr(result, "run_args")
            and result.run_args
            and any(
                "--add-host" in a
                for a in result.run_args
            )
        )

    @staticmethod
    def _install_cache_spy(
        watch_prefix: str = "runtime-artifacts",
    ) -> tuple[list[str], object]:
        """Instrument the full cache filesystem boundary and return
        ``(ops_list, restore_fn)``.

        Every ``open``, ``os.rename``, ``os.replace``, ``os.link``,
        ``os.mkdir``, ``os.listdir``, and ``os.scandir`` call touching
        a path that contains *watch_prefix* is recorded in *ops_list*.

        The caller MUST invoke the returned *restore* callable after
        the operation-under-test to unpatch the builtins."""
        import builtins as _bi

        prefix = watch_prefix
        ops: list[str] = []
        _orig_open = _bi.open
        _orig_rename = os.rename
        _orig_replace = os.replace
        _orig_link = os.link
        _orig_mkdir = os.mkdir
        _orig_listdir = os.listdir
        _orig_scandir = os.scandir

        def _is_cache(obj: object) -> bool:
            return prefix in str(obj)

        def _trap_open(file, *a, **kw):
            if _is_cache(file):
                ops.append(f"open:{file}")
            return _orig_open(file, *a, **kw)

        def _trap_rename(src, dst, *args, **kwargs):
            if _is_cache(src) or _is_cache(dst):
                ops.append(f"rename:{src}->{dst}")
            return _orig_rename(src, dst, *args, **kwargs)

        def _trap_replace(src, dst, *args, **kwargs):
            if _is_cache(src) or _is_cache(dst):
                ops.append(f"replace:{src}->{dst}")
            return _orig_replace(src, dst, *args, **kwargs)

        def _trap_link(src, dst, *args, **kwargs):
            if _is_cache(src) or _is_cache(dst):
                ops.append(f"link:{src}->{dst}")
            return _orig_link(src, dst, *args, **kwargs)

        def _trap_mkdir(path, *a, **kw):
            if _is_cache(path):
                ops.append(f"mkdir:{path}")
            return _orig_mkdir(path, *a, **kw)

        def _trap_listdir(path):
            if _is_cache(path):
                ops.append(f"listdir:{path}")
            return _orig_listdir(path)

        def _trap_scandir(path):
            if _is_cache(path):
                ops.append(f"scandir:{path}")
            return _orig_scandir(path)

        _bi.open = _trap_open  # type: ignore[assignment]
        os.rename = _trap_rename  # type: ignore[assignment]
        os.replace = _trap_replace  # type: ignore[assignment]
        os.link = _trap_link  # type: ignore[assignment]
        os.mkdir = _trap_mkdir  # type: ignore[assignment]
        os.listdir = _trap_listdir  # type: ignore[assignment]
        os.scandir = _trap_scandir  # type: ignore[assignment]

        def _restore() -> None:
            _bi.open = _orig_open  # type: ignore[assignment]
            os.rename = _orig_rename  # type: ignore[assignment]
            os.replace = _orig_replace  # type: ignore[assignment]
            os.link = _orig_link  # type: ignore[assignment]
            os.mkdir = _orig_mkdir  # type: ignore[assignment]
            os.listdir = _orig_listdir  # type: ignore[assignment]
            os.scandir = _orig_scandir  # type: ignore[assignment]

        return ops, _restore

    def _assert_no_effects(
        self,
        result: object,
        cache_ops: list[str],
        *,
        projection: bool = True,
        cache: bool = True,
        gateway: bool = True,
        executor: bool = True,
    ) -> None:
        """Assert that none of the requested boundaries were
        touched.

        *projection* — ``_create_projection`` was never called.
        *cache* — no filesystem ops under ``runtime-artifacts``.
        *gateway* — ``--add-host`` not in rendered args.
        *executor* — ``executor.run()`` was never invoked."""
        if projection:
            self._assert_never_reached("projection")
        if cache:
            self.assertFalse(
                cache_ops,
                f"cache ops triggered but should have been blocked: "
                f"{cache_ops}",
            )
        if gateway:
            self.assertFalse(
                self._gateway_effect_triggered(result),
                "gateway effect triggered but should have been blocked",
            )
        if executor:
            self._assert_never_reached("executor")

    # ── invalid inventory ─────────────────────────────────────

    def test_invalid_inventory_fails_before_projection(self) -> None:
        cache_ops, restore = self._install_cache_spy()
        try:
            req = self._request(
                inventory_path="/nonexistent/inventory.toml",
                executor=self._spy_executor(self._stages),
                inspector=FakeContainerNameInspector(set()),
                _create_projection=self._spy_factory(self._stages),
            )
            result = self._run(req)
            self.assertEqual(result.exit_kind, ExitKind.CONFIG)
            self._assert_no_effects(result, cache_ops)
        finally:
            restore()

    # ── invalid override ──────────────────────────────────────

    def test_invalid_override_fails_before_projection(self) -> None:
        cache_ops, restore = self._install_cache_spy()
        try:
            req = self._request(
                overrides={
                    "runtime.pi-extensions.nonexistent.version": "9.9.9",
                },
                executor=self._spy_executor(self._stages),
                inspector=FakeContainerNameInspector(set()),
                _create_projection=self._spy_factory(self._stages),
            )
            result = self._run(req)
            self.assertEqual(result.exit_kind, ExitKind.CONFIG)
            self._assert_no_effects(result, cache_ops)
        finally:
            restore()

    # ── malformed / unsupported identity ─────────────────────

    def test_malformed_integrity_fails_before_any_effect(self) -> None:
        """An integrity string that fails the model regex
        (e.g. ``sha1-`` prefix or non-base64 payload) must be
        rejected at inventory load — **before** any cache,
        gateway, projection, or executor boundary."""
        malformed = self._make_malformed_inventory(
            self._tmpdir.name,
            bad_integrity="sha1-!!!!notbase64!!!!",
        )
        cache_ops, restore = self._install_cache_spy()
        try:
            req = self._request(
                inventory_path=malformed,
                executor=self._spy_executor(self._stages),
                inspector=FakeContainerNameInspector(set()),
                _create_projection=self._spy_factory(self._stages),
            )
            result = self._run(req)
            self.assertEqual(result.exit_kind, ExitKind.CONFIG)
            self._assert_no_effects(result, cache_ops)
        finally:
            restore()

    def test_unsupported_algorithm_fails_before_any_effect(self) -> None:
        """An integrity with an unsupported algorithm
        (e.g. ``sha1-AAAA...``) must be rejected at inventory
        load — **before** any cache, gateway, projection, or
        executor boundary."""
        malformed = self._make_malformed_inventory(
            self._tmpdir.name,
            bad_integrity="sha1-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==",
        )
        cache_ops, restore = self._install_cache_spy()
        try:
            req = self._request(
                inventory_path=malformed,
                executor=self._spy_executor(self._stages),
                inspector=FakeContainerNameInspector(set()),
                _create_projection=self._spy_factory(self._stages),
            )
            result = self._run(req)
            self.assertEqual(result.exit_kind, ExitKind.CONFIG)
            self._assert_no_effects(result, cache_ops)
        finally:
            restore()

    # ── cache-root validation ─────────────────────────────────

    def test_run_request_must_not_defer_materialization(self) -> None:
        """``RunRequest`` MUST NOT expose a ``cache_root`` field.
        Materialization is not a caller choice — every non-dry
        run must prepare artifacts before publication.  An
        deferred field that skips materialization when empty
        contradicts the authoritative launcher/runtime specs."""
        import dataclasses
        from docker.launcher import RunRequest
        fields = {f.name for f in dataclasses.fields(RunRequest)}
        self.assertNotIn(
            "cache_root", fields,
            "RunRequest must NOT expose cache_root — "
            "materialization is constructor-owned, not caller-selected",
        )

    def test_invalid_cache_root_fails_before_cache_and_execution(
        self,
    ) -> None:
        """RED — when the constructor-owned cache root target
        is a symlink (or otherwise corrupt), the orchestrator
        must fail **before** any cache mutation, gateway
        rendering, or Docker execution.

        The cache root is monkey-patched to a known-corrupt
        symlink so the spy and the orchestrator observe the
        same path."""
        from pathlib import Path
        cache_root = Path(self._tmpdir.name, "sub", "runtime-artifacts")
        symlink_dest = Path(self._tmpdir.name, "nowhere")
        os.mkdir(str(cache_root.parent))
        os.symlink(str(symlink_dest), str(cache_root))

        watch = str(cache_root)
        cache_ops, restore_spy = self._install_cache_spy(
            watch_prefix=watch,
        )
        try:
            req = self._request(
                _artifact_cache_root=watch,
                executor=self._spy_executor(self._stages),
                inspector=FakeContainerNameInspector(set()),
                _create_projection=self._spy_factory(self._stages),
            )
            result = self._run(req)
        finally:
            restore_spy()

        # RED: cache root is a constructor-owned constant but
        # orchestrate_run never inspects it — execution proceeds.
        self._assert_no_effects(result, cache_ops)

    # ── digest-mismatch at materialization ───────────────────

    def test_digest_mismatch_fails_before_projection_and_execution(
        self,
    ) -> None:
        """RED — when the materializer fetches the artifact
        and the computed digest does not match the inventory
        integrity, the orchestrator must fail **before**
        projection publication, gateway rendering, and Docker
        execution.

        The test injects a deterministic byte fetcher that
        records every call.  It asserts the fetcher is called
        exactly once with the reviewed URL and returns known
        wrong bytes.  Temporary cache work during
        download+verify is allowed, but no verified blob may
        be published and no projection, gateway, or executor
        effect may occur.

        A unique temporary cache root under the test's own
        ``_tmpdir`` is used and the module-level constant is
        patched so the test is hermetic — independent of
        repository state, prior runs, or developer-local
        artifact caches."""
        import docker.versioning.artifact_cache as _artifact_cache
        from unittest import mock

        malformed = self._make_single_extension_fixture(
            self._tmpdir.name,
            bad_integrity=(
                "sha512-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
                "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=="
            ),
        )

        expected_url = (
            "https://registry.npmjs.org/@arcanemachine/pi-read"
            "/-/pi-read-0.2.0.tgz"
        )
        fetch_calls: list[tuple[str, ...]] = []

        def _wrong_bytes(url: str) -> bytes:
            fetch_calls.append((url,))
            return b"known-wrong-bytes-for-deterministic-mismatch"

        import tempfile
        isolated_cache = tempfile.TemporaryDirectory(
            prefix="digest-mismatch-cache-",
            dir=self._tmpdir.name,
        )
        cache_root = os.path.join(isolated_cache.name, "blobs")
        self.assertFalse(
            os.path.lexists(cache_root),
            "digest-mismatch cache must start absent and cannot reuse a hit",
        )

        cache_ops, restore_spy = self._install_cache_spy()
        try:
            req = self._request(
                _artifact_cache_root=cache_root,
                inventory_path=malformed,
                _artifact_fetcher=_wrong_bytes,
                executor=self._spy_executor(self._stages),
                inspector=FakeContainerNameInspector(set()),
                _create_projection=self._spy_factory(self._stages),
            )
            result = self._run(req)

            # RED: no materialization layer — the fetcher is never
            # called, the inventory loads, projection is created,
            # and the executor runs.
            self.assertEqual(
                fetch_calls,
                [(expected_url,)],
                "fetcher must be called exactly once with the "
                "reviewed URL before projection or executor",
            )
            self._assert_no_effects(
                result, cache_ops,
                cache=False,
            )
            published: list[str] = []
            if os.path.isdir(cache_root):
                for dirpath, _dirnames, filenames in os.walk(cache_root):
                    for name in filenames:
                        published.append(os.path.join(dirpath, name))
            self.assertFalse(
                published,
                f"verified blob(s) published under {cache_root} "
                f"despite digest mismatch: {published}",
            )
            self.assertFalse(
                any(op.startswith(p) for p in ("rename", "replace", "link")
                    for op in cache_ops),
                f"publication op in cache despite digest mismatch: "
                f"{cache_ops}",
            )
        finally:
            restore_spy()
            isolated_cache.cleanup()


# ═══════════════════════════════════════════════════════════════
# Orchestration order tests — Task 6.1
# ═══════════════════════════════════════════════════════════════


class _LoggedHandle:
    """Context manager that records enter/exit in a shared event log."""

    def __init__(self, path: str, content_hash: str,
                 log: list[str]) -> None:
        self._path = path
        self._hash = content_hash
        self._log = log

    @property
    def path(self) -> str:
        return self._path

    @property
    def content_hash(self) -> str:
        return self._hash

    def __enter__(self) -> "_LoggedHandle":
        self._log.append("projection_publish")
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        with open(self._path, "w") as fh:
            fh.write("fake-projection")
        return self

    def __exit__(self, *args: object) -> bool:
        self._log.append("projection_cleanup")
        if os.path.exists(self._path):
            os.remove(self._path)
        return False


class _LoggedProjectionFactory:
    """Factory that returns :class:`_LoggedHandle` instances wired
    to a shared event log."""

    def __init__(self, log: list[str]) -> None:
        self._log = log
        self.calls: list[tuple[object, str]] = []
        self.handles: list[_LoggedHandle] = []

    def __call__(
        self,
        projection: object,
        *,
        parent_dir: str,
    ) -> _LoggedHandle:
        import hashlib
        import json
        raw_proj: dict[str, object] = {
            "extensions": {
                name: __import__("dataclasses").asdict(entry)
                for name, entry in getattr(
                    projection, "extensions", {},
                ).items()
            }
        }
        raw = json.dumps(raw_proj, sort_keys=True, default=str)
        content_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        path = os.path.join(parent_dir, "proj.toml")
        h = _LoggedHandle(path, content_hash, self._log)
        self.calls.append((projection, parent_dir))
        self.handles.append(h)
        return h


class _LoggedExecutor:
    """Executor that records ``"docker_execute"`` in a shared
    event log."""

    def __init__(self, log: list[str], *, returncode: int = 0) -> None:
        self._log = log
        self.returncode = returncode
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: tuple[str, ...], *,
            interactive: bool = False) -> ProcessResult:
        self._log.append("docker_execute")
        self.calls.append(argv)
        return ProcessResult(
            argv=argv,
            return_code=self.returncode,
            stdout="ok" if self.returncode == 0 else "",
            stderr="" if self.returncode == 0 else "failed",
        )


class TestOrchestrationOrdering(unittest.TestCase):
    """Orchestration event order:

    validate-and-plan → materialize → projection_publish →
    gateway_rendered → docker_execute → projection_cleanup.

    Every boundary records events in a single shared ``list[str]``.
    No production injection — all faking is done via
    ``unittest.mock.patch`` on the existing production boundaries.
    """

    _REQUIRED_ORDER = [
        "validate_and_plan",
        "materialize",
        "projection_publish",
        "vector_rendered",
        "docker_execute",
        "projection_cleanup",
    ]

    def setUp(self) -> None:
        import tempfile
        self._lock_leak_before = _shared_lock_leak_snapshot()
        self._tmpdir = tempfile.TemporaryDirectory()
        self._artifact_cache_root = os.path.join(
            self._tmpdir.name, "runtime-artifacts", "blobs",
        )
        self._repo_cache_snapshot = _repo_cache_file_set()
        self._proj_parent = os.path.join(
            self._tmpdir.name, "projects", "constructor-identity", "runtime",
        )
        self._inventory_path = self._make_fixture_toml()
        self._event_log: list[str] = []

    def _make_fixture_toml(self) -> str:
        """Copy the real ``docker-constructor.toml`` with
        deterministic artifact integrities."""
        import base64
        import hashlib
        import re
        import shutil
        real = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..",
                         "docker-constructor.toml"),
        )
        fixture = os.path.join(self._tmpdir.name, "fixture.toml")
        shutil.copy2(real, fixture)
        with open(fixture) as fh:
            content = fh.read()

        def replace_integrity(match: re.Match[str]) -> str:
            url = match.group(1)
            digest = base64.b64encode(
                hashlib.sha512(
                    ("order-fixture:" + url).encode("utf-8")
                ).digest()
            ).decode("ascii")
            return f'url = "{url}"\nintegrity = "sha512-{digest}"'

        # Handle both ``url``/``integrity`` key orderings in the
        # canonical inventory (see TestRunTransaction._make_fixture_toml).
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
        with open(fixture, "w") as fh:
            fh.write(content)
        return fixture

    def tearDown(self) -> None:
        self._tmpdir.cleanup()
        _ = _assert_repo_cache_unchanged(self._repo_cache_snapshot)
        _assert_no_shared_lock_created(self._lock_leak_before)

    @staticmethod
    def _artifact_bytes(url: str) -> bytes:
        return ("order-fixture:" + url).encode("utf-8")

    def _request(self, **overrides: object) -> RunRequest:
        from docker.launcher import WorkspaceSelection
        kwargs: dict[str, object] = {
            "inventory_path": self._inventory_path,
            "project_root": str(Path(self._inventory_path).parent),
            "image": "pi-cli-pi:latest",
            "selection": WorkspaceSelection(workspace="/work/p1"),
            "pi_home_host": "/home/alice/.pi",
            "_create_projection": _LoggedProjectionFactory(self._event_log),
            "_artifact_fetcher": self._artifact_bytes,
            "_artifact_cache_root": self._artifact_cache_root,
        }
        kwargs.update(overrides)
        return RunRequest(**kwargs)  # type: ignore[arg-type]

    @staticmethod
    def _run(req: RunRequest) -> RunResult:
        from docker.launcher import orchestrate_run
        return orchestrate_run(req)

    @staticmethod
    def _fake_blobs_for(
        selected: list[SelectedArtifact],
        cache_root: str,
    ) -> dict[str, VerifiedCacheBlob]:
        """Build a fake ``VerifiedCacheBlob`` dict from *selected*
        artifacts, creating actual files so validation passes."""
        from docker.versioning.artifact_cache import (
            VerifiedCacheBlob,
        )
        from docker.versioning.model import _derive_artifact_id
        result: dict[str, VerifiedCacheBlob] = {}
        for art in selected:
            if art.integrity in result:
                continue
            artifact_id = _derive_artifact_id(art.integrity)
            algorithm = art.integrity.split("-", 1)[0]
            digest = artifact_id.rsplit("/", 1)[1].replace(".tgz", "")
            host_path = os.path.join(cache_root, artifact_id)
            os.makedirs(os.path.dirname(host_path), exist_ok=True)
            with open(host_path, "wb") as fh:
                fh.write(b"fake-verified-blob")
            result[art.integrity] = VerifiedCacheBlob(
                algorithm=algorithm,
                digest=digest,
                integrity=art.integrity,
                host_path=host_path,
            )
        return result

    # ── helpers for patching boundaries ──────────────────────

    def _plan_patch(self) -> object:
        """Return a side-effect that records ``"validate_and_plan"``
        then delegates to the real ``resolve_runtime``."""
        from docker.versioning.effective import resolve_runtime as _real

        def _plan_wrapper(
            runtime,
            overrides,
        ):
            self._event_log.append("validate_and_plan")
            return _real(runtime, overrides)

        return _plan_wrapper

    def _materialize_patch(self) -> object:
        """Return a side-effect that records ``"materialize"``
        then returns fake verified blobs.  Captures the selected
        list so the test can assert all artifacts are present."""

        self._materialized_selected: list[SelectedArtifact] = []

        def _mat(
            selected,
            *,
            transport,
            filesystem,
            lock_factory,
            temp_dir,
            cache_root,
            temp_root,
        ):
            self._event_log.append("materialize")
            self._materialized_selected[:] = selected
            return self._fake_blobs_for(selected, cache_root)

        return _mat

    def _render_patch(self) -> object:
        """Return a side-effect that delegates to the real
        ``render_run_vector`` and records ``"vector_rendered"``."""
        from docker.versioning.rendering import render_run_vector as _real

        def _rendered(inputs):
            rv = _real(inputs)
            self._event_log.append("vector_rendered")
            return rv

        return _rendered

    # ── happy-path order ──────────────────────────────────────

    def test_empty_selection_skips_materialization_and_cache_mutation(self) -> None:
        from unittest import mock
        from docker.versioning.model import EffectiveRuntimeProjection

        executor = _LoggedExecutor(self._event_log, returncode=0)
        req = self._request(
            executor=executor,
            inspector=FakeContainerNameInspector(set()),
        )
        with mock.patch(
            "docker.versioning.effective.resolve_runtime",
            return_value=([], EffectiveRuntimeProjection(extensions={})),
        ), mock.patch(
            "docker.versioning.artifact_cache.materialize_selected_artifacts",
        ) as materialize, mock.patch(
            "docker.versioning.rendering.render_run_vector",
            side_effect=self._render_patch(),
        ):
            result = self._run(req)

        self.assertEqual(ExitKind.SUCCESS, result.exit_kind)
        materialize.assert_not_called()
        self.assertFalse(os.path.exists(self._artifact_cache_root))
        self.assertFalse(os.path.exists(os.path.dirname(self._artifact_cache_root)))

    def test_event_order(self) -> None:
        """Events MUST occur in the exact required sequence:
        validate-and-plan → materialize → projection_publish →
        gateway_rendered → docker_execute → projection_cleanup."""
        from unittest import mock

        executor = _LoggedExecutor(self._event_log, returncode=0)
        req = self._request(
            executor=executor,
            inspector=FakeContainerNameInspector(set()),
        )

        with mock.patch(
            "docker.versioning.effective.resolve_runtime",
            side_effect=self._plan_patch(),
        ), mock.patch(
            "docker.versioning.artifact_cache.materialize_selected_artifacts",
            side_effect=self._materialize_patch(),
        ), mock.patch(
            "docker.versioning.rendering.render_run_vector",
            side_effect=self._render_patch(),
        ):
            result = self._run(req)

        self.assertEqual(result.exit_kind, ExitKind.SUCCESS)
        self.assertEqual(
            self._event_log,
            self._REQUIRED_ORDER,
            "events must occur in the required order",
        )
        # The materializer must receive exactly the resolved set
        # — all reviewed fixture artifacts, not just the first.
        import base64
        import hashlib
        from docker.versioning.artifact_cache import SelectedArtifact
        _fixture_urls = [
            "https://registry.npmjs.org/@arcanemachine/pi-read/-/pi-read-0.2.1.tgz",
            "https://registry.npmjs.org/@narumitw/pi-usage/-/pi-usage-0.60.7.tgz",
            "https://registry.npmjs.org/pi-proxy/-/pi-proxy-1.0.0.tgz",
        ]
        expected = [
            SelectedArtifact(
                url=u,
                integrity="sha512-"
                + base64.b64encode(
                    hashlib.sha512(
                        ("order-fixture:" + u).encode("utf-8")
                    ).digest()
                ).decode("ascii"),
            )
            for u in _fixture_urls
        ]
        self.assertEqual(
            sorted(self._materialized_selected, key=lambda a: a.url),
            sorted(expected, key=lambda a: a.url),
            "materializer must receive exactly the resolved set",
        )

    # ── materialization failure blocks downstream ────────────

    def test_materialization_failure_stops_before_projection(
        self,
    ) -> None:
        """When materialization raises, no downstream events
        (projection, gateway, Docker, cleanup) fire."""
        from unittest import mock

        def _failing_materialize(
            selected,
            *,
            transport,
            filesystem,
            lock_factory,
            temp_dir,
            cache_root,
            temp_root,
        ):
            self._event_log.append("materialize")
            from docker.versioning.artifact_cache import (
                ArtifactMaterializationError,
            )
            raise ArtifactMaterializationError("transport", "boom")

        executor = _LoggedExecutor(self._event_log, returncode=0)
        req = self._request(
            executor=executor,
            inspector=FakeContainerNameInspector(set()),
        )

        with mock.patch(
            "docker.versioning.effective.resolve_runtime",
            side_effect=self._plan_patch(),
        ), mock.patch(
            "docker.versioning.artifact_cache.materialize_selected_artifacts",
            side_effect=_failing_materialize,
        ), mock.patch(
            "docker.versioning.rendering.render_run_vector",
            side_effect=self._render_patch(),
        ):
            result = self._run(req)

        self.assertEqual(result.exit_kind, ExitKind.OPERATIONAL)
        self.assertIn("boom", result.message or "")
        # validate_and_plan fires first; materialize fires and
        # raises — nothing after.  The gateway patch is installed
        # but must never be called.
        self.assertEqual(
            self._event_log,
            ["validate_and_plan", "materialize"],
        )

    # ── Docker failure: projection still cleaned up ──────────

    def test_docker_nonzero_cleans_projection(self) -> None:
        """When Docker exits non-zero, ``projection_cleanup``
        still fires after ``docker_execute``."""
        from unittest import mock

        executor = _LoggedExecutor(self._event_log, returncode=1)
        req = self._request(
            executor=executor,
            inspector=FakeContainerNameInspector(set()),
        )

        with mock.patch(
            "docker.versioning.effective.resolve_runtime",
            side_effect=self._plan_patch(),
        ), mock.patch(
            "docker.versioning.artifact_cache.materialize_selected_artifacts",
            side_effect=self._materialize_patch(),
        ), mock.patch(
            "docker.versioning.rendering.render_run_vector",
            side_effect=self._render_patch(),
        ):
            result = self._run(req)

        self.assertEqual(result.exit_kind, ExitKind.OPERATIONAL)
        self.assertEqual(
            self._event_log,
            self._REQUIRED_ORDER,
            "projection_cleanup must fire even on Docker failure",
        )

    def test_docker_raises_cleans_projection(self) -> None:
        """When the executor raises an exception,
        ``projection_cleanup`` still fires."""
        from unittest import mock

        class _RaisingExecutor:
            def __init__(self, log):
                self._log = log
                self.calls: list[tuple[str, ...]] = []

            def run(self, argv, *, interactive=False):
                self._log.append("docker_execute")
                raise OSError("docker not found")

        executor = _RaisingExecutor(self._event_log)
        req = self._request(
            executor=executor,
            inspector=FakeContainerNameInspector(set()),
        )

        with mock.patch(
            "docker.versioning.effective.resolve_runtime",
            side_effect=self._plan_patch(),
        ), mock.patch(
            "docker.versioning.artifact_cache.materialize_selected_artifacts",
            side_effect=self._materialize_patch(),
        ), mock.patch(
            "docker.versioning.rendering.render_run_vector",
            side_effect=self._render_patch(),
        ):
            result = self._run(req)

        self.assertEqual(result.exit_kind, ExitKind.OPERATIONAL)
        self.assertIn("docker not found", result.message or "")
        self.assertEqual(
            self._event_log,
            self._REQUIRED_ORDER,
            "projection_cleanup must fire even when executor raises",
        )

    # ── later failure preserves shared verified blobs ───────

    def test_later_failure_preserves_verified_blobs(self) -> None:
        """When Docker fails (non-zero exit), the verified cache
        blobs must remain on disk — a failed launch does not
        delete shared cache entries."""
        from unittest import mock

        executor = _LoggedExecutor(self._event_log, returncode=1)
        req = self._request(
            executor=executor,
            inspector=FakeContainerNameInspector(set()),
        )

        with mock.patch(
            "docker.versioning.effective.resolve_runtime",
            side_effect=self._plan_patch(),
        ), mock.patch(
            "docker.versioning.artifact_cache.materialize_selected_artifacts",
            side_effect=self._materialize_patch(),
        ), mock.patch(
            "docker.versioning.rendering.render_run_vector",
            side_effect=self._render_patch(),
        ):
            self._run(req)

        # After the run, every blob that was materialized must
        # still exist as a regular file.
        for art in self._materialized_selected:
            from docker.versioning.model import _derive_artifact_id
            blob_path = os.path.join(
                self._artifact_cache_root,
                _derive_artifact_id(art.integrity),
            )
            self.assertTrue(
                os.path.isfile(blob_path),
                f"blob must survive launch failure: {blob_path}",
            )


if __name__ == "__main__":
    unittest.main()
