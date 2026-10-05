"""Phase 9A regression tests: ``validate_project_state`` release paths.

Covers the descriptor-ownership and primary-preservation contract of
``docker.versioning.project_state.validate_project_state``:

- a missing ancestor is mapped to ``ProjectStateError`` and retained as the
  primary, so an ordinary ancestor-close failure is only a secondary
  diagnostic instead of replacing the domain error;
- a failed ancestor release on the success path still releases the retained
  namespace descriptor exactly once (no leak, no retry);
- a namespace-close process-control interruption stays authoritative over later
  ancestor cleanup instead of being lost behind a stale primary;
- every independent release is attempted exactly once.
"""
from __future__ import annotations

import contextlib
import errno
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from docker.versioning import project_state as project_state_module
from docker.versioning.project_state import (
    ProjectState,
    ProjectStateError,
    _metadata,
    _project_identity,
    _safe_basename,
    validate_project_state,
)

# Position of each descriptor in the validation open order.
_ROOT = 0
_PROJECTS = 1
_NAMESPACE = 2


class _Recorder:
    """Record descriptor opens/closes and inject per-position close failures."""

    def __init__(self) -> None:
        self.open_order: list[tuple[str, int]] = []
        self.closes: list[int] = []
        self.fail_at: dict[int, BaseException] = {}
        self._real_open = project_state_module._open_private_dir_at
        self._real_close = os.close

    def _open(self, parent_fd, name, *, label):
        fd = self._real_open(parent_fd, name, label=label)
        self.open_order.append((label, fd))
        return fd

    def _close(self, fd):
        self.closes.append(fd)
        for index, (_label, opened_fd) in enumerate(self.open_order):
            if opened_fd == fd and index in self.fail_at:
                raise self.fail_at[index]
        return self._real_close(fd)

    @contextlib.contextmanager
    def installed(self):
        with mock.patch.object(
            project_state_module,
            "_open_private_dir_at",
            side_effect=self._open,
        ), mock.patch.object(
            project_state_module.os,
            "close",
            side_effect=self._close,
        ):
            yield self

    def fd(self, index: int) -> int:
        return self.open_order[index][1]

    def attempts(self, index: int) -> int:
        return self.closes.count(self.fd(index))


class _ProjectStateCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.project = self.base / "project"
        self.project.mkdir()
        self.cache = self.base / "cache"
        os.mkdir(self.cache, 0o700)
        os.chmod(self.cache, 0o700)
        self.identity = _project_identity(self.project.resolve())
        self.namespace = (
            self.cache / "projects"
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
        expected = _metadata(self.project.resolve(), self.identity)
        target = self.namespace / "project.json"
        target.write_bytes(expected)
        os.chmod(target, 0o600)

    @staticmethod
    def _secondary(exc: BaseException) -> list[BaseException]:
        return list(getattr(exc, "_transaction_secondary", []))


class MissingAncestorCleanupTests(_ProjectStateCase):
    def test_missing_namespace_preserves_domain_error_with_close_secondary(self) -> None:
        self._prepare_projects()  # namespace directory deliberately absent
        state = self._state()
        recorder = _Recorder()
        close_error = OSError(errno.EIO, "projects close failed")
        with recorder.installed():
            recorder.fail_at[_PROJECTS] = close_error
            with self.assertRaises(ProjectStateError) as caught:
                validate_project_state(state)

        self.assertIn("missing project namespace", str(caught.exception))
        self.assertIn(close_error, self._secondary(caught.exception))
        # Both retained ancestors attempted exactly once; the namespace was
        # never opened.
        self.assertEqual(len(recorder.open_order), 2)
        self.assertEqual(recorder.attempts(_PROJECTS), 1)
        self.assertEqual(recorder.attempts(_ROOT), 1)

    def test_missing_projects_preserves_domain_error_with_root_close_secondary(self) -> None:
        state = self._state()  # neither projects nor namespace exists
        recorder = _Recorder()
        close_error = OSError(errno.EIO, "root close failed")
        with recorder.installed():
            recorder.fail_at[_ROOT] = close_error
            with self.assertRaises(ProjectStateError) as caught:
                validate_project_state(state)

        self.assertIn("missing project state", str(caught.exception))
        self.assertIn(close_error, self._secondary(caught.exception))
        self.assertEqual(len(recorder.open_order), 1)
        self.assertEqual(recorder.attempts(_ROOT), 1)


class SuccessfulValidationAncestorReleaseTests(_ProjectStateCase):
    def test_ancestor_close_failure_releases_namespace_exactly_once(self) -> None:
        self._prepare_namespace()
        self._write_metadata()
        state = self._state()
        recorder = _Recorder()
        close_error = OSError(errno.EIO, "root close failed")
        with recorder.installed():
            recorder.fail_at[_ROOT] = close_error
            with self.assertRaises(OSError) as caught:
                validate_project_state(state)

        # The ancestor failure prevents returning the validated state, so it is
        # authoritative and the retained namespace descriptor is released once.
        self.assertIs(caught.exception, close_error)
        self.assertEqual(recorder.attempts(_PROJECTS), 1)
        self.assertEqual(recorder.attempts(_ROOT), 1)
        self.assertEqual(recorder.attempts(_NAMESPACE), 1)

    def test_successful_validation_retains_namespace_when_ancestors_close(self) -> None:
        self._prepare_namespace()
        self._write_metadata()
        state = self._state()
        recorder = _Recorder()
        with recorder.installed():
            validated = validate_project_state(state)
            # Ancestors were released; the namespace descriptor remains open for
            # the caller and is handed to the returned state.
            self.assertEqual(recorder.attempts(_PROJECTS), 1)
            self.assertEqual(recorder.attempts(_ROOT), 1)
            self.assertEqual(recorder.attempts(_NAMESPACE), 0)
            self.assertEqual(validated.namespace_fd, recorder.fd(_NAMESPACE))
            validated.close()
            self.assertEqual(recorder.attempts(_NAMESPACE), 1)


class NamespaceInterruptionPrecedenceTests(_ProjectStateCase):
    def _failing_state(self) -> ProjectState:
        # Namespace exists but has no project.json, so validation fails with the
        # namespace descriptor already open.
        self._prepare_namespace()
        return self._state()

    def test_namespace_close_interruption_authoritative_over_ancestor_cleanup(self) -> None:
        state = self._failing_state()
        recorder = _Recorder()
        interruption = KeyboardInterrupt()
        with recorder.installed():
            recorder.fail_at[_NAMESPACE] = interruption
            with self.assertRaises(KeyboardInterrupt) as caught:
                validate_project_state(state)

        self.assertIs(caught.exception, interruption)
        self.assertEqual(recorder.attempts(_NAMESPACE), 1)
        self.assertEqual(recorder.attempts(_PROJECTS), 1)
        self.assertEqual(recorder.attempts(_ROOT), 1)
        # The displaced domain error is preserved as a secondary diagnostic.
        self.assertTrue(
            any(isinstance(item, ProjectStateError) for item in self._secondary(interruption))
        )

    def test_first_namespace_interruption_precedes_later_root_interruption(self) -> None:
        state = self._failing_state()
        recorder = _Recorder()
        namespace_interruption = KeyboardInterrupt()
        root_interruption = SystemExit()
        with recorder.installed():
            recorder.fail_at[_NAMESPACE] = namespace_interruption
            recorder.fail_at[_ROOT] = root_interruption
            with self.assertRaises(KeyboardInterrupt) as caught:
                validate_project_state(state)

        self.assertIs(caught.exception, namespace_interruption)
        self.assertEqual(recorder.attempts(_NAMESPACE), 1)
        self.assertEqual(recorder.attempts(_ROOT), 1)
        self.assertIn(root_interruption, self._secondary(namespace_interruption))


class FailedValidationSingleReleaseTests(_ProjectStateCase):
    def test_every_release_attempted_exactly_once_on_metadata_failure(self) -> None:
        self._prepare_namespace()
        state = self._state()  # no project.json -> metadata read fails
        recorder = _Recorder()
        close_error = OSError(errno.EIO, "projects close failed")
        with recorder.installed():
            recorder.fail_at[_PROJECTS] = close_error
            with self.assertRaises(ProjectStateError) as caught:
                validate_project_state(state)

        self.assertEqual(recorder.attempts(_NAMESPACE), 1)
        self.assertEqual(recorder.attempts(_PROJECTS), 1)
        self.assertEqual(recorder.attempts(_ROOT), 1)
        self.assertIn(close_error, self._secondary(caught.exception))
