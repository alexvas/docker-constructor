"""Phase 6 (tasks 6.10–6.11) — project-state descriptor ownership.

RED before migration: ``_open_private_dir_at``, ``_read_metadata``, and
``resolve_project_state`` released raw ``os.open`` descriptors with a direct
``os.close``, and ``validate_project_state`` released every ancestor with a raw
``os.close``.  Precedence and at-most-once behavior were already correct, but
there was no shared terminal owner, so the migration contract
(terminal-before-close + tracked transfer + at-most-once) is the RED part.
``ValidatedProjectState`` already owned its retained namespace descriptor.
"""

from __future__ import annotations

import errno
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from docker.versioning import project_state as project_state_module
from docker.versioning.project_state import (
    ProjectState,
    ProjectStateError,
    _ensure_private_dir_at,
    _metadata,
    _open_private_dir_at,
    _project_identity,
    _read_metadata,
    _safe_basename,
    resolve_project_state,
    validate_project_state,
)
from tests.descriptor_ownership_support import recording, secondary_diagnostics


class _ProjectStateCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="phase6-project-state-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.project = self.base / "project"
        self.project.mkdir()
        self.cache = self.base / "cache"
        os.mkdir(self.cache, 0o700)
        os.chmod(self.cache, 0o700)
        self.identity = _project_identity(self.project.resolve())
        self.namespace = (
            self.cache
            / "projects"
            / f"{_safe_basename(self.project.name)}-{self.identity[:16]}"
        )

    def _state(self) -> ProjectState:
        ns = self.namespace
        return ProjectState(
            project_path=self.project.resolve(),
            cache_root=self.cache,
            namespace=ns,
            identity=self.identity,
            generated_root=ns / "generated",
            runtime_root=ns / "runtime",
            evidence_root=ns / "evidence",
            build_artifacts_root=ns / "build-artifacts",
            transactions_root=ns / "transactions",
        )

    def _prepare_projects(self) -> None:
        projects = self.cache / "projects"
        os.mkdir(projects, 0o700)
        os.chmod(projects, 0o700)

    def _prepare_namespace(self) -> None:
        self._prepare_projects()
        os.mkdir(self.namespace, 0o700)
        os.chmod(self.namespace, 0o700)

    def _write_metadata(self) -> None:
        target = self.namespace / "project.json"
        target.write_bytes(_metadata(self.project.resolve(), self.identity))
        os.chmod(target, 0o600)

    def _open_namespace_fd(self) -> int:
        return _open_private_dir_at(None, os.fspath(self.namespace), label="namespace")


class PrivateDirectoryOwnershipTests(_ProjectStateCase):
    def test_success_transfers_live_descriptor_to_the_caller(self) -> None:
        with recording(project_state_module) as rec:
            fd = _open_private_dir_at(
                None, os.fspath(self.cache), label=f"cache root {self.cache}"
            )
        try:
            self.assertTrue(stat.S_ISDIR(os.fstat(fd).st_mode))
        finally:
            os.close(fd)
        # The internal owner handed the descriptor to the caller (detach); it
        # is neither released nor a leak.
        self.assertEqual(len(rec.owners), 1)
        self.assertFalse(rec.owners[0].released)

    def test_stat_failure_releases_through_shared_owner(self) -> None:
        fault = OSError(errno.EIO, "injected fstat")
        with recording(project_state_module) as rec:
            with mock.patch.object(project_state_module.os, "fstat", side_effect=fault):
                with self.assertRaises(ProjectStateError):
                    _open_private_dir_at(
                        None, os.fspath(self.cache), label=f"cache root {self.cache}"
                    )
        rec.assert_terminal_and_at_most_once(self)

    def test_unsafe_mode_releases_through_shared_owner(self) -> None:
        os.chmod(self.cache, 0o755)
        with recording(project_state_module) as rec:
            with self.assertRaises(ProjectStateError):
                _open_private_dir_at(
                    None, os.fspath(self.cache), label=f"cache root {self.cache}"
                )
        rec.assert_terminal_and_at_most_once(self)

    def test_stat_interruption_releases_through_shared_owner(self) -> None:
        fault = KeyboardInterrupt("injected fstat interruption")
        with recording(project_state_module) as rec:
            with mock.patch.object(project_state_module.os, "fstat", side_effect=fault):
                with self.assertRaises(KeyboardInterrupt) as caught:
                    _open_private_dir_at(
                        None, os.fspath(self.cache), label=f"cache root {self.cache}"
                    )
        self.assertIs(caught.exception, fault)
        self.assertEqual(len(rec.close_calls), 1)
        rec.assert_terminal_and_at_most_once(self)

    def test_stat_unexpected_defect_releases_through_shared_owner(self) -> None:
        fault = RuntimeError("injected fstat defect")
        with recording(project_state_module) as rec:
            with mock.patch.object(project_state_module.os, "fstat", side_effect=fault):
                with self.assertRaises(RuntimeError) as caught:
                    _open_private_dir_at(
                        None, os.fspath(self.cache), label=f"cache root {self.cache}"
                    )
        self.assertIs(caught.exception, fault)
        self.assertEqual(len(rec.close_calls), 1)
        rec.assert_terminal_and_at_most_once(self)

    def test_stat_interruption_keeps_primary_over_close_failure(self) -> None:
        fault = KeyboardInterrupt("injected fstat interruption")
        close_fault = OSError(errno.EIO, "injected close")
        with recording(project_state_module, close_faults=[close_fault]) as rec:
            with mock.patch.object(project_state_module.os, "fstat", side_effect=fault):
                with self.assertRaises(KeyboardInterrupt) as caught:
                    _open_private_dir_at(
                        None, os.fspath(self.cache), label=f"cache root {self.cache}"
                    )
        self.assertIs(caught.exception, fault)
        self.assertIn(close_fault, secondary_diagnostics(caught.exception))
        rec.assert_terminal_and_at_most_once(self)

    def test_stat_unexpected_defect_keeps_primary_over_close_failure(self) -> None:
        fault = RuntimeError("injected fstat defect")
        close_fault = OSError(errno.EIO, "injected close")
        with recording(project_state_module, close_faults=[close_fault]) as rec:
            with mock.patch.object(project_state_module.os, "fstat", side_effect=fault):
                with self.assertRaises(RuntimeError) as caught:
                    _open_private_dir_at(
                        None, os.fspath(self.cache), label=f"cache root {self.cache}"
                    )
        self.assertIs(caught.exception, fault)
        self.assertIn(close_fault, secondary_diagnostics(caught.exception))
        rec.assert_terminal_and_at_most_once(self)


class MetadataOwnershipTests(_ProjectStateCase):
    def test_success_releases_the_metadata_owner(self) -> None:
        self._prepare_namespace()
        self._write_metadata()
        expected = _metadata(self.project.resolve(), self.identity)
        namespace_fd = self._open_namespace_fd()
        try:
            with recording(project_state_module) as rec:
                _read_metadata(namespace_fd, expected, "namespace")
        finally:
            os.close(namespace_fd)
        rec.assert_terminal_and_at_most_once(self)

    def test_mismatch_releases_the_metadata_owner(self) -> None:
        self._prepare_namespace()
        target = self.namespace / "project.json"
        target.write_bytes(b"not-the-expected-metadata\n")
        os.chmod(target, 0o600)
        expected = _metadata(self.project.resolve(), self.identity)
        namespace_fd = self._open_namespace_fd()
        try:
            with recording(project_state_module) as rec:
                with self.assertRaises(ProjectStateError):
                    _read_metadata(namespace_fd, expected, "namespace")
        finally:
            os.close(namespace_fd)
        rec.assert_terminal_and_at_most_once(self)

    def test_missing_metadata_raises_before_any_owner(self) -> None:
        self._prepare_namespace()
        namespace_fd = self._open_namespace_fd()
        try:
            with recording(project_state_module) as rec:
                with self.assertRaises(ProjectStateError):
                    _read_metadata(namespace_fd, b"expected", "namespace")
        finally:
            os.close(namespace_fd)
        self.assertEqual(rec.owners, [])


class ResolveProjectStateOwnershipTests(_ProjectStateCase):
    def test_create_true_releases_every_owner(self) -> None:
        with recording(project_state_module) as rec:
            state = resolve_project_state(
                self.project, cache_root=self.cache, create=True
            )
        rec.assert_terminal_and_at_most_once(self)
        self.assertEqual(state.namespace, self.namespace)
        self.assertTrue((self.namespace / "build-artifacts").is_dir())
        self.assertGreaterEqual(len(rec.owners), 3)

    def test_create_false_early_return_releases_owners(self) -> None:
        with recording(project_state_module) as rec:
            state = resolve_project_state(
                self.project, cache_root=self.cache, create=False
            )
        rec.assert_terminal_and_at_most_once(self)
        self.assertEqual(state.namespace, self.namespace)
        self.assertEqual(len(rec.owners), 2)  # transient + caller root owner

    def test_missing_cache_root_without_create_constructs_no_owner(self) -> None:
        missing = self.base / "absent"
        with recording(project_state_module) as rec:
            state = resolve_project_state(
                self.project, cache_root=missing, create=False
            )
        self.assertEqual(state.cache_root, missing)
        self.assertEqual(rec.owners, [])

    def test_metadata_failure_stays_primary_over_close_failure(self) -> None:
        # The namespace exists but predates no metadata write and is not newly
        # created, so the metadata read stays the primary failure while the
        # owner releases attach as secondary diagnostics.
        self._prepare_namespace()
        close_fault = OSError(errno.EIO, "injected close")

        def fault(fd, owner):
            return close_fault

        with recording(project_state_module, close_fault=fault) as rec:
            with self.assertRaises(ProjectStateError) as caught:
                resolve_project_state(self.project, cache_root=self.cache, create=True)
        self.assertIn("metadata", str(caught.exception))
        self.assertIn(close_fault, secondary_diagnostics(caught.exception))
        rec.assert_terminal_and_at_most_once(self)


class ValidateProjectStateSharedOwnerTests(_ProjectStateCase):
    def test_successful_validation_releases_ancestors_and_transfers_namespace(
        self,
    ) -> None:
        self._prepare_namespace()
        self._write_metadata()
        state = self._state()
        with recording(project_state_module) as rec:
            validated = validate_project_state(state)
            os.fstat(validated.namespace_fd)  # retained live by the returned state
            validated.close()
        rec.assert_terminal_and_at_most_once(self)
        self.assertGreaterEqual(len(rec.owners), 3)

    def test_missing_namespace_releases_ancestors_through_owners(self) -> None:
        self._prepare_projects()
        state = self._state()
        with recording(project_state_module) as rec:
            with self.assertRaises(ProjectStateError):
                validate_project_state(state)
        rec.assert_terminal_and_at_most_once(self)
        self.assertGreaterEqual(len(rec.owners), 2)


class EnsurePrivateDirectoryOwnershipTests(_ProjectStateCase):
    """Task — a created child descriptor must not leak when its chmod fails."""

    _CHILD_LABEL = "ensured private directory child"

    def _parent_fd(self) -> int:
        return os.open(os.fspath(self.cache), os.O_RDONLY | os.O_DIRECTORY)

    def _ensured_owners(self, rec):
        return [owner for owner in rec.owners if owner.label == self._CHILD_LABEL]

    def _assert_one_child_release(self, rec) -> None:
        owners = self._ensured_owners(rec)
        self.assertEqual(len(owners), 1)
        closes = [c for c in rec.close_calls if c[2] is owners[0]]
        self.assertEqual(len(closes), 1)
        self.assertTrue(closes[0][1])
        rec.assert_terminal_and_at_most_once(self)

    def test_chmod_failure_releases_created_child_once(self) -> None:
        fault = OSError(errno.EIO, "injected fchmod failure")
        parent_fd = self._parent_fd()
        try:
            with recording(project_state_module) as rec:
                with mock.patch.object(
                    project_state_module.os, "fchmod", side_effect=fault
                ):
                    with self.assertRaises(OSError) as caught:
                        _ensure_private_dir_at(parent_fd, "child", label="child")
        finally:
            os.close(parent_fd)
        self.assertIs(caught.exception, fault)
        self._assert_one_child_release(rec)

    def test_chmod_failure_keeps_primary_over_close_failure(self) -> None:
        fault = OSError(errno.EIO, "injected fchmod failure")
        close_fault = OSError(errno.EIO, "injected descriptor close failure")
        parent_fd = self._parent_fd()
        try:
            with recording(project_state_module, close_faults=[close_fault]) as rec:
                with mock.patch.object(
                    project_state_module.os, "fchmod", side_effect=fault
                ):
                    with self.assertRaises(OSError) as caught:
                        _ensure_private_dir_at(parent_fd, "child", label="child")
        finally:
            os.close(parent_fd)
        self.assertIs(caught.exception, fault)
        self.assertIn(close_fault, secondary_diagnostics(fault))
        self._assert_one_child_release(rec)

    def test_chmod_interruption_releases_created_child_once(self) -> None:
        fault = KeyboardInterrupt("injected fchmod interruption")
        parent_fd = self._parent_fd()
        try:
            with recording(project_state_module) as rec:
                with mock.patch.object(
                    project_state_module.os, "fchmod", side_effect=fault
                ):
                    with self.assertRaises(KeyboardInterrupt) as caught:
                        _ensure_private_dir_at(parent_fd, "child", label="child")
        finally:
            os.close(parent_fd)
        self.assertIs(caught.exception, fault)
        self._assert_one_child_release(rec)

    def test_chmod_interruption_keeps_primary_over_close_failure(self) -> None:
        fault = KeyboardInterrupt("injected fchmod interruption")
        close_fault = OSError(errno.EIO, "injected descriptor close failure")
        parent_fd = self._parent_fd()
        try:
            with recording(project_state_module, close_faults=[close_fault]) as rec:
                with mock.patch.object(
                    project_state_module.os, "fchmod", side_effect=fault
                ):
                    with self.assertRaises(KeyboardInterrupt) as caught:
                        _ensure_private_dir_at(parent_fd, "child", label="child")
        finally:
            os.close(parent_fd)
        self.assertIs(caught.exception, fault)
        self.assertIn(close_fault, secondary_diagnostics(fault))
        self._assert_one_child_release(rec)

    def test_success_returns_live_created_child_descriptor(self) -> None:
        parent_fd = self._parent_fd()
        try:
            with recording(project_state_module) as rec:
                fd, created = _ensure_private_dir_at(parent_fd, "child", label="child")
            try:
                self.assertTrue(created)
                self.assertTrue(stat.S_ISDIR(os.fstat(fd).st_mode))
                self.assertEqual(stat.S_IMODE(os.fstat(fd).st_mode), 0o700)
            finally:
                os.close(fd)
        finally:
            os.close(parent_fd)
        # The outer owner detached the descriptor; it must not be released.
        owners = self._ensured_owners(rec)
        self.assertEqual(len(owners), 1)
        self.assertFalse(owners[0].released)

    def test_existing_entry_not_chmodded_returns_live_descriptor(self) -> None:
        os.mkdir(self.cache / "existing", 0o700)
        os.chmod(self.cache / "existing", 0o700)
        parent_fd = self._parent_fd()
        try:
            with recording(project_state_module) as rec:
                with mock.patch.object(
                    project_state_module.os, "fchmod", wraps=os.fchmod
                ) as fchmod_spy:
                    fd, created = _ensure_private_dir_at(
                        parent_fd, "existing", label="existing"
                    )
            try:
                self.assertFalse(created)
                self.assertTrue(stat.S_ISDIR(os.fstat(fd).st_mode))
            finally:
                os.close(fd)
        finally:
            os.close(parent_fd)
        fchmod_spy.assert_not_called()
        # An existing entry takes the early return through
        # ``_open_private_dir_at``; its single owner is detached (not
        # released), leaving the returned descriptor live.
        owners = [
            owner for owner in rec.owners if owner.label == "private directory existing"
        ]
        self.assertEqual(len(owners), 1)
        self.assertFalse(owners[0].released)
