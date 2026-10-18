"""Focused regressions for Phase 1 external project-state propagation."""

from __future__ import annotations
import hashlib
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from docker.versioning.build_orchestration import BuildRequest, plan_build
from docker.versioning.dispatch_types import ExitKind
from docker.versioning.project_state import (
    ProjectStateError,
    _project_identity,
    _safe_basename,
    resolve_project_state,
)
from tests.build_test_support import INVENTORY_PATH


class CacheRootPropagationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "project"
        self.project.mkdir()

    def test_local_cache_dir_is_carried_by_build_plan(self):
        inventory = self.root / "constructor.toml"
        inventory.write_bytes(Path(INVENTORY_PATH).read_bytes())
        cache = self.root / "configured-cache"
        inventory.with_name("docker-constructor.local.toml").write_text(
            f'[cache]\ndir = "{cache}"\n'
        )
        plan = plan_build(
            BuildRequest(
                inventory_path=str(inventory),
                repo_root=str(self.project),
                project_root=Path(str(inventory)).resolve().parent,
            )
        )
        self.assertEqual(plan.exit_kind, ExitKind.SUCCESS)
        self.assertEqual(plan.cache_root, cache)
        self.assertFalse(cache.exists(), "planning must not prepare cache state")

    def test_default_plan_uses_xdg_root(self):
        inventory = self.root / "constructor.toml"
        inventory.write_bytes(Path(INVENTORY_PATH).read_bytes())
        xdg = self.root / "xdg"
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(xdg)}):
            plan = plan_build(
                BuildRequest(
                    inventory_path=str(inventory),
                    repo_root=str(self.project),
                    project_root=Path(str(inventory)).resolve().parent,
                )
            )
        self.assertEqual(plan.exit_kind, ExitKind.SUCCESS)
        self.assertEqual(plan.cache_root, xdg / "docker-constructor")
        self.assertFalse(xdg.exists(), "dry planning must not create XDG state")

    def test_dry_run_plan_leaves_absent_cache_completely_untouched(self):
        inventory = self.root / "constructor.toml"
        inventory.write_bytes(Path(INVENTORY_PATH).read_bytes())
        cache = self.root / "absent-cache"
        inventory.with_name("docker-constructor.local.toml").write_text(
            f'[cache]\ndir = "{cache}"\n'
        )
        plan = plan_build(
            BuildRequest(
                inventory_path=str(inventory),
                repo_root=str(self.project),
                dry_run=True,
                project_root=Path(str(inventory)).resolve().parent,
            )
        )
        self.assertEqual(plan.exit_kind, ExitKind.SUCCESS)
        self.assertFalse(cache.exists())
        self.assertFalse((self.project / ".docker-generated").exists())

    def test_fresh_state_creates_private_evidence_children(self):
        cache = self.root / "cache"
        cache.mkdir(mode=0o700)
        state = resolve_project_state(self.project, cache_root=cache, create=True)
        self.assertTrue((state.namespace / "project.json").is_file())
        self.assertTrue(state.evidence_root.is_dir())
        self.assertFalse((self.project / ".docker-generated").exists())

    def test_read_only_lookup_keeps_missing_namespace_prospective(self):
        cache = self.root / "cache"
        cache.mkdir(mode=0o700)
        state = resolve_project_state(self.project, cache_root=cache, create=False)
        self.assertFalse(state.namespace.exists())
        self.assertFalse(state.evidence_root.exists())

    def test_explicit_runtime_projection_bypasses_malformed_default_state(self):
        from docker.constructor_cli import _resolve_runtime_projection

        cache = self.root / "cache"
        cache.mkdir(mode=0o700)
        state = resolve_project_state(self.project, cache_root=cache)
        (state.namespace / "project.json").write_text("malformed")
        explicit = self.root / "explicit.toml"
        explicit.write_text("[extensions]\n")
        self.assertEqual(
            _resolve_runtime_projection(explicit, self.project, cache), explicit
        )

    def test_implicit_runtime_projection_fails_closed_for_malformed_state(self):
        from docker.constructor_cli import _resolve_runtime_projection

        cache = self.root / "cache"
        cache.mkdir(mode=0o700)
        state = resolve_project_state(self.project, cache_root=cache)
        (state.namespace / "project.json").write_text("malformed")
        with self.assertRaises(ProjectStateError):
            _resolve_runtime_projection(None, self.project, cache)

    def test_default_runtime_lookup_uses_canonical_symlink_namespace(self):
        from docker.constructor_cli import _resolve_runtime_projection

        cache = self.root / "cache"
        cache.mkdir(mode=0o700)
        alias = self.root / "project-alias"
        alias.symlink_to(self.project, target_is_directory=True)
        canonical_state = resolve_project_state(self.project, cache_root=cache)
        projection = canonical_state.runtime_root / "runtime.toml"
        projection.write_text("[extensions]\n")
        self.assertEqual(
            projection,
            _resolve_runtime_projection(None, alias, cache),
        )
        self.assertEqual(
            canonical_state.namespace,
            resolve_project_state(alias, cache_root=cache).namespace,
        )

    def test_explicit_runtime_projection_never_resolves_default_namespace(self):
        from docker.constructor_cli import _resolve_runtime_projection

        cache = self.root / "cache"
        cache.mkdir(mode=0o700)
        state = resolve_project_state(self.project, cache_root=cache)
        before = sorted(
            (p.relative_to(state.namespace), p.read_bytes())
            for p in state.namespace.rglob("*")
            if p.is_file()
        )
        explicit = self.root / "outside-default-namespace.toml"
        explicit.write_text("[extensions]\n")
        with mock.patch(
            "docker.versioning.project_state.resolve_project_state",
            side_effect=AssertionError(
                "explicit lookup must not resolve default state"
            ),
        ):
            self.assertEqual(
                explicit,
                _resolve_runtime_projection(explicit, self.project, cache),
            )
        after = sorted(
            (p.relative_to(state.namespace), p.read_bytes())
            for p in state.namespace.rglob("*")
            if p.is_file()
        )
        self.assertEqual(before, after)
        self.assertFalse((self.project / ".docker-generated").exists())

    def test_namespace_symlink_race_fails_without_touching_target(self):
        cache = self.root / "cache"
        cache.mkdir(mode=0o700)
        state = resolve_project_state(self.project, cache_root=cache)
        outside = self.root / "outside"
        outside.mkdir()
        original_open = os.open
        tripped = False

        def race(name, flags, *args, **kwargs):
            nonlocal tripped
            if name == "runtime" and not tripped:
                tripped = True
                state.runtime_root.rmdir()
                state.runtime_root.symlink_to(outside, target_is_directory=True)
            return original_open(name, flags, *args, **kwargs)

        with mock.patch("os.open", side_effect=race):
            with self.assertRaises(ProjectStateError):
                resolve_project_state(self.project, cache_root=cache)
        self.assertEqual(list(outside.iterdir()), [])

    def test_unsafe_later_child_prevents_early_child_recreation(self):
        cache = self.root / "cache"
        cache.mkdir(mode=0o700)
        state = resolve_project_state(self.project, cache_root=cache)
        state.generated_root.rmdir()  # remove an early child
        os.chmod(state.runtime_root, 0o755)  # make a later child unsafe
        before = self._namespace_snapshot(state.namespace)
        with self.assertRaises(ProjectStateError):
            resolve_project_state(self.project, cache_root=cache, create=True)
        self.assertEqual(self._namespace_snapshot(state.namespace), before)
        self.assertFalse(
            state.generated_root.exists(),
            "generated must not be recreated before validation fails",
        )

    def _namespace_snapshot(self, namespace):
        entries = []
        for path in sorted(namespace.rglob("*")):
            st = path.lstat()
            kind = (
                "dir" if path.is_dir() else ("symlink" if path.is_symlink() else "file")
            )
            entries.append(
                (str(path.relative_to(namespace)), kind, stat.S_IMODE(st.st_mode))
            )
        metadata = (namespace / "project.json").read_bytes()
        return (tuple(entries), metadata)

    def test_integrity_failures_are_not_absent_state(self):
        cache = self.root / "cache"
        cache.mkdir(mode=0o700)
        state = resolve_project_state(self.project, cache_root=cache)
        metadata = state.namespace / "project.json"
        metadata.write_text("malformed")
        os.chmod(metadata, 0o600)
        with self.assertRaises(ProjectStateError):
            resolve_project_state(self.project, cache_root=cache, create=False)
        metadata.unlink()
        metadata.symlink_to(self.root / "outside")
        with self.assertRaises(ProjectStateError):
            resolve_project_state(self.project, cache_root=cache, create=False)


class ProjectIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _canonical(self, name):
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        return path.resolve()

    def test_safe_basename_replaces_spaces_and_punctuation(self):
        self.assertEqual(_safe_basename("my project"), "my-project")
        self.assertEqual(_safe_basename("my/project:name"), "my-project-name")
        self.assertEqual(_safe_basename("a@b#c$d"), "a-b-c-d")

    def test_safe_basename_trims_leading_trailing_dots_and_dashes(self):
        self.assertEqual(_safe_basename("...-my-project-..."), "my-project")
        self.assertEqual(_safe_basename(".hidden."), "hidden")

    def test_safe_basename_replaces_unicode_letters(self):
        self.assertEqual(_safe_basename("prøject"), "pr-ject")
        self.assertEqual(_safe_basename("日本語"), "project")

    def test_safe_basename_empty_result_falls_back_to_project(self):
        self.assertEqual(_safe_basename("..--.."), "project")
        self.assertEqual(_safe_basename("...---..."), "project")
        self.assertEqual(_safe_basename(""), "project")

    def test_safe_basename_keeps_explicit_ascii_alnum_dot_underscore_dash(self):
        self.assertEqual(_safe_basename("My_Project.v1-2"), "My_Project.v1-2")

    def test_identity_is_sha256_of_canonical_absolute_path_utf8(self):
        path = self._canonical("my project")
        expected = hashlib.sha256(str(path).encode("utf-8")).hexdigest()
        self.assertEqual(_project_identity(path), expected)
        cache = self.root / "cache"
        cache.mkdir(mode=0o700)
        state = resolve_project_state(path, cache_root=cache)
        self.assertEqual(state.identity, expected)
        self.assertEqual(state.namespace.name.split("-")[-1], expected[:16])

    def test_safe_basename_is_deterministic_but_not_authoritative(self):
        a = self._canonical("my project")
        b = self._canonical("my-project")
        self.assertEqual(_safe_basename(a.name), _safe_basename(a.name))
        self.assertEqual(_safe_basename(b.name), _safe_basename(b.name))
        self.assertEqual(_safe_basename(a.name), _safe_basename(b.name))
        self.assertNotEqual(_project_identity(a), _project_identity(b))


if __name__ == "__main__":
    unittest.main()
