"""Build-context confinement evidence (Phase 4 task 4.5).

Docker transmits everything under the selected build context unless an
applicable ``.dockerignore`` excludes it.  These tests drive the production
``execute_build`` boundary end to end (with injected fakes) and prove, for
external and explicit contexts, that the host-only ``docker-constructor.toml``
and ``docker-constructor.local.toml`` cannot enter Docker's context.
"""
from __future__ import annotations

import fnmatch
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.build_test_support import (
    digest_valid_selected_artifacts,
    fake_pi_materialization,
    fixture_directory,
    publish_digest_valid_artifacts,
)

from docker.networking import ProcessResult
from docker.versioning.build_context_confinement import (
    ConfinementError,
    confinement_ignore_rules,
    materialize_build_context_confinement,
    plan_build_context_confinement,
)
from docker.versioning.build_orchestration import (
    BuildRequest,
    PublishResult,
    orchestrate_build,
)
from docker.versioning.build_snapshot import MaterializedSnapshot
from docker.versioning.dispatch_types import ExitKind

REPO_ROOT = Path(__file__).resolve().parents[1]
_REVIEWED_INVENTORY_BYTES = (REPO_ROOT / "docker-constructor.toml").read_bytes()
_DOCKERFILE_BYTES = b"FROM scratch\n"
_REVIEWED_NAME = "docker-constructor.toml"
_LOCAL_NAME = "docker-constructor.local.toml"


def _fixture_snapshot(*_args, **_kwargs):
    path = fixture_directory("fixture-snapshot-")
    return MaterializedSnapshot(path, path / "manifest.json")


def setUpModule():
    global _snapshot_patcher
    _snapshot_patcher = mock.patch(
        "docker.versioning.build_orchestration.create_artifact_snapshot",
        side_effect=_fixture_snapshot,
    )
    _snapshot_patcher.start()


def tearDownModule():
    _snapshot_patcher.stop()


class _CapturingRunner:
    """Recording build executor; ``on_call`` runs while confinement exists."""

    def __init__(self, *, return_code: int = 0, on_call=None):
        self.calls: list[tuple[str, ...]] = []
        self.return_code = return_code
        self.on_call = on_call

    def run(self, argv: tuple[str, ...]) -> ProcessResult:
        self.calls.append(tuple(argv))
        if self.on_call is not None:
            self.on_call(tuple(argv))
        return ProcessResult(
            argv=tuple(argv), return_code=self.return_code,
            stdout="", stderr="" if self.return_code == 0 else "build error",
        )


def _docker_match(pattern: str, relative: str) -> bool:
    """Approximate Docker's ``filepath.Match`` for the patterns used here."""
    return fnmatch.fnmatch(relative, pattern)


def _docker_ignored(patterns: list[str], relative: str) -> bool:
    """Docker ignore semantics: the last matching pattern wins."""
    ignored = False
    for line in patterns:
        pattern = line.strip()
        if not pattern or pattern.startswith("#"):
            continue
        negate = pattern.startswith("!")
        if negate:
            pattern = pattern[1:]
        if _docker_match(pattern, relative):
            ignored = not negate
    return ignored


class _ConfinementHarness(unittest.TestCase):
    """Create a real external project and run the production build boundary."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="confinement-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self._saved_env = {
            key: os.environ.get(key) for key in ("HOME", "XDG_CACHE_HOME")
        }
        os.environ["HOME"] = str(self.home)
        os.environ["XDG_CACHE_HOME"] = str(self.base / "xdg")
        self.addCleanup(self._restore_env)

    def _restore_env(self) -> None:
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # ── project fixtures ─────────────────────────────────────────────
    def _make_project(
        self,
        *,
        relative: str = "",
        reviewed: bool = True,
        local: bool = True,
        dockerfile: bool = True,
        root_ignore: str | None = None,
        dockerfile_ignore: str | None = None,
    ) -> Path:
        project = self.base / relative if relative else self.base / "project"
        project.mkdir(parents=True, exist_ok=True)
        if reviewed:
            (project / _REVIEWED_NAME).write_bytes(_REVIEWED_INVENTORY_BYTES)
        if local:
            (project / _LOCAL_NAME).write_text("")
        if dockerfile:
            (project / "Dockerfile").write_bytes(_DOCKERFILE_BYTES)
        if root_ignore is not None:
            (project / ".dockerignore").write_text(root_ignore)
        if dockerfile_ignore is not None:
            (project / "Dockerfile.dockerignore").write_text(dockerfile_ignore)
        return project

    def _orchestrate(
        self,
        project: Path,
        *,
        context: str | None = None,
        dockerfile: str | None = None,
        runner=None,
        materialize=None,
        publish=None,
    ):
        runner = runner or _CapturingRunner()
        materialize = materialize or publish_digest_valid_artifacts
        publish = publish or (lambda *_a, **_k: PublishResult("/tmp/effective.toml"))
        request = BuildRequest(
            inventory_path=str(project / _REVIEWED_NAME),
            repo_root=str(project),
            project_root=str(project),
            context=str(project) if context is None else context,
            dockerfile=str(project / "Dockerfile") if dockerfile is None else dockerfile,
            confirmed=True,
            runner=runner,
            _materialize_artifacts=materialize,
            _materialize_pi=fake_pi_materialization,
            _publish_projection=publish,
            _named_context_supported=lambda: True,
        )
        with mock.patch(
            "docker.versioning.build_orchestration.select_build_artifacts",
            side_effect=digest_valid_selected_artifacts,
        ):
            return orchestrate_build(request)

    # ── assertions ───────────────────────────────────────────────────
    @staticmethod
    def _generated_paths(argv: tuple[str, ...]) -> tuple[Path, Path]:
        file_value = argv[argv.index("--file") + 1]
        return Path(file_value), Path(file_value + ".dockerignore")

    def _assert_context_is_last(self, argv: tuple[str, ...], expected: str) -> None:
        self.assertEqual(expected, argv[-1])

    def _assert_no_generated_state(self) -> None:
        for surviving in self.base.rglob("build-context-*"):
            self.fail(f"generated confinement state survived: {surviving}")


class TestDefaultAndExplicitContexts(_ConfinementHarness):
    """Task 4.5: default and explicit contexts confine both documents."""

    def _assert_confined(self, project: Path, expected_context: str) -> None:
        observed: dict[str, object] = {}

        def on_call(argv: tuple[str, ...]) -> None:
            generated, ignorefile = self._generated_paths(argv)
            observed["generated"] = generated
            observed["ignorefile"] = ignorefile
            observed["rules"] = ignorefile.read_text(encoding="utf-8").splitlines()
            observed["entries"] = sorted(p.name for p in generated.parent.iterdir())
            observed["dockerfile_bytes"] = generated.read_bytes()
            observed["context"] = argv[-1]

        runner = _CapturingRunner(on_call=on_call)
        result = self._orchestrate(project, runner=runner)

        self.assertEqual(ExitKind.SUCCESS, result.exit_kind, result.message)
        self.assertEqual(1, len(runner.calls))
        argv = runner.calls[0]

        generated = observed["generated"]
        assert isinstance(generated, Path)
        # --file points at the transaction-owned Dockerfile copy.
        self.assertNotEqual(project.resolve() / "Dockerfile", generated)
        self.assertFalse(generated.is_relative_to(project.resolve()))
        self.assertEqual("Dockerfile", generated.name)
        self.assertEqual(_DOCKERFILE_BYTES, observed["dockerfile_bytes"])

        rules = observed["rules"]
        assert isinstance(rules, list)
        self.assertTrue(_docker_ignored(rules, _REVIEWED_NAME))
        self.assertTrue(_docker_ignored(rules, _LOCAL_NAME))

        # Neither source document is copied into generated state.
        self.assertEqual(["Dockerfile", "Dockerfile.dockerignore"], observed["entries"])

        # The context remains the final positional argument.
        self._assert_context_is_last(argv, expected_context)

        # Generated state is removed once the transaction completes.
        ignorefile = observed["ignorefile"]
        assert isinstance(ignorefile, Path)
        self.assertFalse(ignorefile.exists())

    def test_default_context_equal_to_project_root(self) -> None:
        project = self._make_project(relative="project")
        self._assert_confined(project, str(project))

    def test_explicit_context_equal_to_project_root(self) -> None:
        project = self._make_project(relative="project")
        self._assert_confined(project, str(project))


class TestNestedAndOutsideContexts(_ConfinementHarness):
    """Task 4.5 item 7: explicit parent and out-of-context inventories."""

    def test_nested_project_uses_context_relative_paths(self) -> None:
        project = self._make_project(relative="project")
        (self.base / "Dockerfile").write_bytes(_DOCKERFILE_BYTES)
        (self.base / "README.txt").write_text("parent context\n")
        observed: dict[str, list[str]] = {}

        def on_call(argv: tuple[str, ...]) -> None:
            _, ignorefile = self._generated_paths(argv)
            observed["rules"] = ignorefile.read_text(encoding="utf-8").splitlines()

        runner = _CapturingRunner(on_call=on_call)
        result = self._orchestrate(
            project,
            context=str(self.base),
            dockerfile=str(self.base / "Dockerfile"),
            runner=runner,
        )
        self.assertEqual(ExitKind.SUCCESS, result.exit_kind, result.message)
        argv = runner.calls[0]
        self.assertIn("project/docker-constructor.toml", observed["rules"])
        self.assertIn("project/docker-constructor.local.toml", observed["rules"])
        self.assertTrue(
            _docker_ignored(
                observed["rules"], "project/docker-constructor.local.toml"
            )
        )
        self._assert_context_is_last(argv, str(self.base))

    def test_inventory_outside_build_context_skips_confinement(self) -> None:
        project = self._make_project(relative="proj")
        context = self.base / "ctx"
        context.mkdir()
        (context / "Dockerfile").write_bytes(_DOCKERFILE_BYTES)
        observed: dict[str, str] = {}

        def on_call(argv: tuple[str, ...]) -> None:
            observed["file_value"] = argv[argv.index("--file") + 1]

        runner = _CapturingRunner(on_call=on_call)
        result = self._orchestrate(
            project,
            context=str(context),
            dockerfile=str(context / "Dockerfile"),
            runner=runner,
        )
        self.assertEqual(ExitKind.SUCCESS, result.exit_kind, result.message)
        argv = runner.calls[0]
        # No confinement is needed: the original Dockerfile is used unchanged.
        self.assertEqual(str(context / "Dockerfile"), observed["file_value"])
        self.assertFalse(Path(observed["file_value"] + ".dockerignore").exists())
        self._assert_context_is_last(argv, str(context))


class TestSymlinkedDocuments(_ConfinementHarness):
    """Confinement follows lexical context entries, never symlink targets."""

    def _outside_directory(self) -> Path:
        outside = self.base / "outside"
        outside.mkdir(parents=True, exist_ok=True)
        return outside

    def _symlink_entry(self, project: Path, name: str, target: Path) -> None:
        entry = project / name
        if entry.is_symlink() or entry.exists():
            entry.unlink()
        entry.symlink_to(target)

    def _capture_rules(self, project: Path, **kwargs) -> list[str]:
        captured: dict[str, list[str]] = {}

        def on_call(argv: tuple[str, ...]) -> None:
            _, ignorefile = self._generated_paths(argv)
            captured["rules"] = ignorefile.read_text(encoding="utf-8").splitlines()

        runner = _CapturingRunner(on_call=on_call)
        result = self._orchestrate(project, runner=runner, **kwargs)
        self.assertEqual(ExitKind.SUCCESS, result.exit_kind, result.message)
        return captured["rules"]

    def test_symlinked_reviewed_document_is_excluded(self) -> None:
        target = self._outside_directory() / "reviewed.toml"
        target.write_bytes(_REVIEWED_INVENTORY_BYTES)
        project = self._make_project(relative="project", local=False)
        self._symlink_entry(project, _REVIEWED_NAME, target)

        rules = self._capture_rules(project)

        self.assertIn(_REVIEWED_NAME, rules)
        self.assertTrue(_docker_ignored(rules, _REVIEWED_NAME))
        # The resolved outside target is never named in the ignore rules.
        self.assertNotIn(str(target), rules)

    def test_symlinked_local_companion_is_excluded(self) -> None:
        target = self._outside_directory() / "local.toml"
        target.write_text("")
        project = self._make_project(relative="project")
        self._symlink_entry(project, _LOCAL_NAME, target)

        rules = self._capture_rules(project)

        self.assertIn(_LOCAL_NAME, rules)
        self.assertTrue(_docker_ignored(rules, _LOCAL_NAME))
        self.assertNotIn(str(target), rules)

    def test_symlinked_entries_are_inspected_during_docker_call(self) -> None:
        outside = self._outside_directory()
        reviewed_target = outside / "reviewed.toml"
        reviewed_target.write_bytes(_REVIEWED_INVENTORY_BYTES)
        local_target = outside / "local.toml"
        local_target.write_text("")
        project = self._make_project(relative="project", local=False)
        self._symlink_entry(project, _REVIEWED_NAME, reviewed_target)
        self._symlink_entry(project, _LOCAL_NAME, local_target)

        observed: dict[str, object] = {}

        def on_call(argv: tuple[str, ...]) -> None:
            generated, ignorefile = self._generated_paths(argv)
            observed["entries"] = sorted(p.name for p in generated.parent.iterdir())
            observed["rules"] = ignorefile.read_text(encoding="utf-8").splitlines()

        runner = _CapturingRunner(on_call=on_call)
        result = self._orchestrate(project, runner=runner)
        self.assertEqual(ExitKind.SUCCESS, result.exit_kind, result.message)
        self.assertEqual(1, len(runner.calls))

        entries = observed["entries"]
        assert isinstance(entries, list)
        self.assertEqual(["Dockerfile", "Dockerfile.dockerignore"], entries)

        rules = observed["rules"]
        assert isinstance(rules, list)
        self.assertTrue(_docker_ignored(rules, _REVIEWED_NAME))
        self.assertTrue(_docker_ignored(rules, _LOCAL_NAME))
        self.assertNotIn(str(reviewed_target), rules)
        self.assertNotIn(str(local_target), rules)


class TestSymlinkedDockerfile(_ConfinementHarness):
    """A symlinked Dockerfile selects rules beside its requested path."""

    def _symlink_dockerfile(self, project: Path, target: Path) -> None:
        entry = project / "Dockerfile"
        if entry.is_symlink() or entry.exists():
            entry.unlink()
        entry.symlink_to(target)

    def test_symlinked_dockerfile_uses_requested_ignore_file(self) -> None:
        outside = self.base / "outside"
        outside.mkdir(parents=True, exist_ok=True)
        real = outside / "RealDockerfile"
        real.write_bytes(b"FROM real-target\n")
        # Rules beside the canonical target must never be selected.
        (outside / "RealDockerfile.dockerignore").write_text("target-only-rule\n")

        project = self._make_project(
            relative="project", dockerfile=False, root_ignore="root-only-rule\n"
        )
        (project / "Dockerfile.dockerignore").write_text("requested-only-rule\n")
        self._symlink_dockerfile(project, real)

        observed: dict[str, object] = {}

        def on_call(argv: tuple[str, ...]) -> None:
            generated, ignorefile = self._generated_paths(argv)
            observed["generated"] = generated
            observed["bytes"] = generated.read_bytes()
            observed["rules"] = ignorefile.read_text(encoding="utf-8").splitlines()

        runner = _CapturingRunner(on_call=on_call)
        result = self._orchestrate(project, runner=runner)
        self.assertEqual(ExitKind.SUCCESS, result.exit_kind, result.message)
        self.assertEqual(1, len(runner.calls))
        argv = runner.calls[0]

        # --file points at the transaction-owned Dockerfile copy, carrying the
        # canonical target's exact bytes.
        generated = observed["generated"]
        assert isinstance(generated, Path)
        self.assertEqual("Dockerfile", generated.name)
        self.assertNotEqual(real, generated)
        self.assertEqual(b"FROM real-target\n", observed["bytes"])

        rules = observed["rules"]
        assert isinstance(rules, list)
        # Rules come from beside the requested path, not the target or the root.
        self.assertIn("requested-only-rule", rules)
        self.assertNotIn("root-only-rule", rules)
        self.assertNotIn("target-only-rule", rules)
        # Forced inventory/companion exclusions remain last.
        self.assertEqual(
            [_REVIEWED_NAME, _LOCAL_NAME], [rule for rule in rules if rule][-2:]
        )
        self.assertTrue(_docker_ignored(rules, _REVIEWED_NAME))
        self.assertTrue(_docker_ignored(rules, _LOCAL_NAME))

        # The original context remains the final positional argument.
        self._assert_context_is_last(argv, str(project))


class TestIgnoreRulePreservation(_ConfinementHarness):
    """Task 4.5 item 6/7: preserve project rules and override negation."""

    def _generated_rules(self, project: Path, **kwargs) -> list[str]:
        captured: dict[str, list[str]] = {}

        def on_call(argv: tuple[str, ...]) -> None:
            _, ignorefile = self._generated_paths(argv)
            captured["rules"] = ignorefile.read_text(encoding="utf-8").splitlines()

        runner = _CapturingRunner(on_call=on_call)
        result = self._orchestrate(project, runner=runner, **kwargs)
        self.assertEqual(ExitKind.SUCCESS, result.exit_kind, result.message)
        return captured["rules"]

    def test_missing_optional_companion_still_excludes_both_paths(self) -> None:
        project = self._make_project(relative="project", local=False)
        rules = self._generated_rules(project)
        self.assertTrue(_docker_ignored(rules, _REVIEWED_NAME))
        self.assertTrue(_docker_ignored(rules, _LOCAL_NAME))

    def test_existing_project_rules_are_preserved(self) -> None:
        project = self._make_project(relative="project", root_ignore="*.tmp\nsecret/\n")
        rules = self._generated_rules(project)
        self.assertIn("*.tmp", rules)
        self.assertIn("secret/", rules)
        self.assertTrue(_docker_ignored(rules, "scratch.tmp"))

    def test_later_negation_cannot_reinclude_local_companion(self) -> None:
        project = self._make_project(
            relative="project",
            root_ignore=f"!{_LOCAL_NAME}\n",
        )
        rules = self._generated_rules(project)
        # The forced exclusion is the last positive match for the companion.
        self.assertTrue(_docker_ignored(rules, _LOCAL_NAME))
        forced = [rule for rule in rules if rule and not rule.startswith("!")]
        self.assertEqual(_LOCAL_NAME, forced[-1])

    def test_dockerfile_specific_ignore_takes_precedence(self) -> None:
        project = self._make_project(
            relative="project",
            root_ignore="root-only-rule\n",
            dockerfile_ignore="dockerfile-only-rule\n",
        )
        rules = self._generated_rules(project)
        self.assertIn("dockerfile-only-rule", rules)
        self.assertNotIn("root-only-rule", rules)


class TestFailClosed(_ConfinementHarness):
    """Task 4.5 item 8: unsafe confinement never invokes Docker."""

    def _assert_no_effects(self, result) -> None:
        self.assertIn(result.exit_kind, (ExitKind.CONFIG, ExitKind.OPERATIONAL))
        self.assertEqual((), result.build_args)
        self.assertIsNone(result.process_result)

    def test_missing_dockerfile_fails_closed(self) -> None:
        project = self._make_project(relative="project", dockerfile=False)
        runner = _CapturingRunner()
        result = self._orchestrate(project, runner=runner)
        self._assert_no_effects(result)
        self.assertEqual([], runner.calls)

    def test_containment_resolution_failure_fails_closed(self) -> None:
        project = self._make_project(relative="project")
        runner = _CapturingRunner()
        with mock.patch(
            "docker.versioning.build_context_confinement._canonical",
            side_effect=ConfinementError("cannot determine containment"),
        ):
            result = self._orchestrate(project, runner=runner)
        self._assert_no_effects(result)
        self.assertEqual([], runner.calls)

    def test_unreadable_ignore_rules_fail_closed(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root bypasses file permission checks")
        project = self._make_project(relative="project", root_ignore="*.tmp\n")
        (project / ".dockerignore").chmod(0o000)
        self.addCleanup((project / ".dockerignore").chmod, 0o600)
        runner = _CapturingRunner()
        result = self._orchestrate(project, runner=runner)
        self._assert_no_effects(result)
        self.assertEqual([], runner.calls)

    def test_unreadable_dockerfile_fails_closed(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root bypasses file permission checks")
        project = self._make_project(relative="project")
        (project / "Dockerfile").chmod(0o000)
        self.addCleanup((project / "Dockerfile").chmod, 0o600)
        runner = _CapturingRunner()
        result = self._orchestrate(project, runner=runner)
        self._assert_no_effects(result)
        self.assertEqual([], runner.calls)

    def test_generated_state_publication_failure_fails_closed(self) -> None:
        project = self._make_project(relative="project")
        runner = _CapturingRunner()
        with mock.patch(
            "docker.versioning.build_context_confinement.tempfile.mkdtemp",
            side_effect=OSError("read-only generated state"),
        ):
            result = self._orchestrate(project, runner=runner)
        self._assert_no_effects(result)
        self.assertEqual([], runner.calls)

    def test_malformed_ignore_encoding_fails_without_effects(self) -> None:
        project = self._make_project(relative="project")
        (project / ".dockerignore").write_bytes(b"\xff\xfe not utf-8\n")
        materialize_calls: list[int] = []
        publish_calls: list[int] = []
        runner = _CapturingRunner()

        def record_materialize(*args, **kwargs):
            materialize_calls.append(1)
            return publish_digest_valid_artifacts(*args, **kwargs)

        def record_publish(*args, **kwargs):
            publish_calls.append(1)
            return PublishResult("/tmp/effective.toml")

        result = self._orchestrate(
            project,
            runner=runner,
            materialize=record_materialize,
            publish=record_publish,
        )
        self._assert_no_effects(result)
        self.assertEqual([], runner.calls)
        self.assertEqual([], materialize_calls)
        self.assertEqual([], publish_calls)
        self._assert_no_generated_state()


class TestTransactionCleanup(_ConfinementHarness):
    """Task 4.5 item 9: generated state is removed on every exit path."""

    @staticmethod
    def _capture(holder: dict, *, return_code: int = 0):
        def on_call(argv: tuple[str, ...]) -> None:
            generated, _ = _ConfinementHarness._generated_paths(argv)
            holder["dir"] = generated.parent
            holder["existed"] = generated.parent.exists()

        return _CapturingRunner(return_code=return_code, on_call=on_call)

    def test_confinement_removed_after_success(self) -> None:
        project = self._make_project(relative="project")
        holder: dict = {}
        result = self._orchestrate(project, runner=self._capture(holder))
        self.assertEqual(ExitKind.SUCCESS, result.exit_kind, result.message)
        self.assertTrue(holder["existed"])
        self.assertFalse(holder["dir"].exists())

    def test_confinement_removed_after_docker_failure(self) -> None:
        project = self._make_project(relative="project")
        holder: dict = {}
        result = self._orchestrate(
            project, runner=self._capture(holder, return_code=1)
        )
        self.assertEqual(ExitKind.OPERATIONAL, result.exit_kind)
        self.assertFalse(holder["dir"].exists())

    def test_confinement_removed_after_materialization_failure(self) -> None:
        from docker.versioning.build_materialization import MaterializationError

        project = self._make_project(relative="project")
        holder: dict = {}

        def fail_materialize(*_args, **_kwargs):
            holder["materialize"] = True
            raise MaterializationError("integrity failed")

        result = self._orchestrate(
            project,
            runner=_CapturingRunner(on_call=lambda argv: holder.setdefault("calls", []).append(argv)),
            materialize=fail_materialize,
        )
        self.assertEqual(ExitKind.OPERATIONAL, result.exit_kind)
        self.assertEqual([], holder.get("calls", []))
        self._assert_no_generated_state()

    def test_confinement_removed_after_unexpected_exception(self) -> None:
        project = self._make_project(relative="project")
        holder: dict = {}

        def boom(*_args, **_kwargs):
            raise RuntimeError("unexpected")

        with mock.patch(
            "docker.versioning.build_orchestration.commit_build_set",
            side_effect=boom,
        ):
            with self.assertRaises(RuntimeError):
                self._orchestrate(project, runner=self._capture(holder))
        self._assert_no_generated_state()


class TestUnitPlanning(unittest.TestCase):
    """Direct unit coverage for the confinement planner."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="confinement-unit-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "docker-constructor.toml").write_text("")
        (self.root / "docker-constructor.local.toml").write_text("")
        (self.root / "Dockerfile").write_text("FROM scratch\n")

    def test_inactive_when_no_document_is_contained(self) -> None:
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        plan = plan_build_context_confinement(
            inventory_path=self.root / "docker-constructor.toml",
            context=str(elsewhere),
            dockerfile=None,
        )
        self.assertFalse(plan.active)
        self.assertEqual((), plan.relative_documents)

    def test_forced_rules_follow_seed_rules(self) -> None:
        plan = plan_build_context_confinement(
            inventory_path=self.root / "docker-constructor.toml",
            context=str(self.root),
            dockerfile=None,
        )
        rules = confinement_ignore_rules(plan)
        self.assertEqual(
            ("docker-constructor.toml", "docker-constructor.local.toml"),
            rules[-2:],
        )

    def test_inactive_materialization_is_rejected(self) -> None:
        plan = plan_build_context_confinement(
            inventory_path=self.root / "docker-constructor.toml",
            context=str(self.root / "elsewhere"),
            dockerfile=None,
        )
        with self.assertRaises(ConfinementError):
            materialize_build_context_confinement(plan, generated_root=self.root)

    def test_symlinked_documents_keep_lexical_relative_paths(self) -> None:
        outside_tmp = tempfile.TemporaryDirectory(prefix="confinement-outside-")
        self.addCleanup(outside_tmp.cleanup)
        outside = Path(outside_tmp.name)
        (outside / "reviewed.toml").write_bytes(_REVIEWED_INVENTORY_BYTES)
        (outside / "local.toml").write_text("")

        project = self.root / "project"
        project.mkdir()
        (project / _REVIEWED_NAME).symlink_to(outside / "reviewed.toml")
        (project / _LOCAL_NAME).symlink_to(outside / "local.toml")

        plan = plan_build_context_confinement(
            inventory_path=project / _REVIEWED_NAME,
            context=str(self.root),
            dockerfile=None,
        )

        self.assertTrue(plan.active)
        expected = (
            "project/docker-constructor.toml",
            "project/docker-constructor.local.toml",
        )
        self.assertEqual(expected, plan.relative_documents)
        self.assertEqual(expected, confinement_ignore_rules(plan)[-2:])

    def test_symlinked_dockerfile_uses_requested_ignore_file(self) -> None:
        outside_tmp = tempfile.TemporaryDirectory(prefix="confinement-outside-")
        self.addCleanup(outside_tmp.cleanup)
        outside = Path(outside_tmp.name)
        real = outside / "RealDockerfile"
        real.write_text("FROM real-target\n")
        # Rules beside the canonical target must never be selected.
        (outside / "RealDockerfile.dockerignore").write_text("target-only-rule\n")

        context = self.root / "ctx"
        context.mkdir()
        (context / _REVIEWED_NAME).write_text("")
        (context / _LOCAL_NAME).write_text("")
        (context / "Dockerfile").symlink_to(real)
        (context / "Dockerfile.dockerignore").write_text("requested-only-rule\n")
        (context / ".dockerignore").write_text("root-only-rule\n")

        plan = plan_build_context_confinement(
            inventory_path=context / _REVIEWED_NAME,
            context=str(context),
            dockerfile=None,
        )

        self.assertTrue(plan.active)
        self.assertEqual(
            Path(os.path.abspath(context / "Dockerfile")), plan.dockerfile_requested
        )
        self.assertEqual(Path(os.path.realpath(real)), plan.dockerfile_source)
        self.assertIn("requested-only-rule", plan.seed_rules)
        self.assertNotIn("root-only-rule", plan.seed_rules)
        self.assertNotIn("target-only-rule", plan.seed_rules)

    def test_invalid_utf8_root_ignore_raises(self) -> None:
        (self.root / ".dockerignore").write_bytes(b"\xff\xfe not utf-8\n")
        with self.assertRaises(ConfinementError):
            plan_build_context_confinement(
                inventory_path=self.root / _REVIEWED_NAME,
                context=str(self.root),
                dockerfile=None,
            )

    def test_invalid_utf8_dockerfile_ignore_raises_without_fallback(self) -> None:
        (self.root / ".dockerignore").write_text("root-only-rule\n")
        (self.root / "Dockerfile.dockerignore").write_bytes(b"\xff\xfe not utf-8\n")
        with self.assertRaises(ConfinementError):
            plan_build_context_confinement(
                inventory_path=self.root / _REVIEWED_NAME,
                context=str(self.root),
                dockerfile=None,
            )
        # Planning is side-effect free: no generated confinement state exists
        # and the root rules were never substituted for the malformed file.
        self.assertEqual([], list(self.root.rglob("build-context-*")))
        self.assertEqual(
            "root-only-rule", (self.root / ".dockerignore").read_text().strip()
        )
