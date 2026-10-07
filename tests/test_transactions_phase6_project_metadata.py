"""Phase 6 task 6.1 — project identity metadata over durable no-clobber.

Project identity metadata is published through the shared L2 durable
no-clobber contract: exact bytes with mode ``0600`` before visibility, a
private sibling flushed before publication, and a parent-directory flush
before success.  A temporary-name collision retries a fresh private name at
most three times.  Only a typed final-destination collision is treated as a
concurrent winner; the caller reopens and verifies the winner's exact bytes,
so an unsafe or mismatched entry is still rejected with the existing
``ProjectStateError`` diagnostic.
"""
from __future__ import annotations

import errno
import os
import stat
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from docker.transactions.errors import (
    STAGE_ALLOCATE,
    STAGE_VALIDATE,
    DestinationExists,
    TransactionError,
)
from docker.transactions.posix import PosixFileOps
from docker.transactions.regular import RegularFileContracts
from docker.versioning import project_state as project_state_module
from docker.versioning.project_state import (
    ProjectStateError,
    _metadata,
    _project_identity,
    _publish_metadata,
    _read_metadata,
    resolve_project_state,
    validate_project_state,
)
from tests.transactions_test_support import InjectedOps, temporary_entries

_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_LABEL = "namespace"


class _ProjectMetadataCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.project = self.base / "project"
        self.project.mkdir()
        self.cache = self.base / "cache"
        self.cache.mkdir(mode=0o700)
        self.namespace = self.cache / "projects" / "project-abc"
        self.namespace.parent.mkdir(mode=0o700)
        self.namespace.mkdir(mode=0o700)
        self.namespace_fd = os.open(self.namespace, _DIR_FLAGS)
        self.addCleanup(os.close, self.namespace_fd)

        canonical = self.project.resolve()
        self.expected = _metadata(canonical, _project_identity(canonical))

        self.ops = InjectedOps()
        patcher = mock.patch.object(
            project_state_module, "PosixFileOps", lambda: self.ops
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    @property
    def target(self) -> Path:
        return self.namespace / "project.json"

    def _leftovers(self) -> list[str]:
        return temporary_entries(str(self.namespace))


class ProjectMetadataPublicationTests(_ProjectMetadataCase):
    def test_routes_through_l2_durable_no_clobber(self) -> None:
        records: list[tuple[str, bytes, int, str]] = []

        class _Spy(RegularFileContracts):
            def durable_no_clobber(self, directory, name, data, mode):
                records.append((name, bytes(data), mode, directory.label))
                return super().durable_no_clobber(directory, name, data, mode)

        with mock.patch.object(project_state_module, "RegularFileContracts", _Spy):
            _publish_metadata(self.namespace_fd, self.expected, _LABEL)
        self.assertEqual(records, [("project.json", self.expected, 0o600, _LABEL)])

    def test_writes_exact_bytes_and_private_mode(self) -> None:
        _publish_metadata(self.namespace_fd, self.expected, _LABEL)
        self.assertEqual(self.target.read_bytes(), self.expected)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)
        self.assertEqual(self._leftovers(), [])

    def test_file_flush_precedes_publication_and_parent_flush_precedes_success(
        self,
    ) -> None:
        _publish_metadata(self.namespace_fd, self.expected, _LABEL)
        order = self.ops.order
        self.assertEqual(order.count("fsync"), 2)
        first_fsync = order.index("fsync")
        link_index = order.index("linkat")
        second_fsync = len(order) - 1 - order[::-1].index("fsync")
        self.assertLess(first_fsync, link_index)
        self.assertLess(link_index, second_fsync)
        fsync_fds = [args[0] for args in self.ops.arg_pairs("fsync")]
        self.assertNotEqual(fsync_fds[0], self.namespace_fd)
        self.assertEqual(fsync_fds[1], self.namespace_fd)

    def test_temporary_collision_retries_three_fresh_allocations(self) -> None:
        attempts: list[str] = []
        real_openat = PosixFileOps.openat

        def colliding(self, dir_fd, name, flags, mode=0o777):
            if isinstance(name, str) and name.startswith(".transaction-"):
                attempts.append(name)
                if len(attempts) < 3:
                    raise FileExistsError(errno.EEXIST, "collision", name)
            return real_openat(self, dir_fd, name, flags, mode)

        with mock.patch.object(PosixFileOps, "openat", colliding):
            _publish_metadata(self.namespace_fd, self.expected, _LABEL)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(len(set(attempts)), 3)
        self.assertEqual(self.target.read_bytes(), self.expected)
        self.assertEqual(self._leftovers(), [])

    def test_exhausted_temporary_allocation_is_project_state_error(self) -> None:
        self.ops.failures["openat"] = FileExistsError(errno.EEXIST, "collision")
        with self.assertRaises(ProjectStateError) as ctx:
            _publish_metadata(self.namespace_fd, self.expected, _LABEL)
        self.assertIn(
            "cannot publish project identity metadata", str(ctx.exception)
        )
        cause = ctx.exception.__cause__
        self.assertIsInstance(cause, TransactionError)
        self.assertEqual(cause.stage, STAGE_ALLOCATE)
        self.assertFalse(self.target.exists())
        self.assertEqual(self._leftovers(), [])

    def test_operational_failure_is_project_state_error_with_observable_cause(
        self,
    ) -> None:
        self.ops.failures["linkat"] = OSError(errno.EIO, "injected publication")
        with self.assertRaises(ProjectStateError) as ctx:
            _publish_metadata(self.namespace_fd, self.expected, _LABEL)
        cause = ctx.exception.__cause__
        self.assertIsInstance(cause, TransactionError)
        raw = cause.cause
        self.assertIsInstance(raw, OSError)
        self.assertEqual(raw.errno, errno.EIO)
        self.assertIs(cause.__cause__, raw)
        self.assertFalse(self.target.exists())
        self.assertEqual(self._leftovers(), [])

    def test_interruption_passes_through_unchanged(self) -> None:
        self.ops.failures["linkat"] = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            _publish_metadata(self.namespace_fd, self.expected, _LABEL)
        self.assertFalse(self.target.exists())
        self.assertEqual(self._leftovers(), [])


class ConcurrentWinnerVerificationTests(_ProjectMetadataCase):
    def test_matching_final_collision_defers_to_winner_verification(self) -> None:
        self.target.write_bytes(self.expected)
        os.chmod(self.target, 0o600)
        _publish_metadata(self.namespace_fd, self.expected, _LABEL)
        _read_metadata(self.namespace_fd, self.expected, _LABEL)
        self.assertEqual(self.target.read_bytes(), self.expected)
        self.assertEqual(self._leftovers(), [])

    def test_collision_cleanup_failure_is_surfaced(self) -> None:
        self.target.write_bytes(self.expected)
        os.chmod(self.target, 0o600)
        cleanup_error = OSError(errno.EIO, "injected temporary unlink cleanup")
        # The final-destination collision is a clean concurrent-winner outcome
        # only when its private temporary entry is actually cleaned up.
        self.ops.failures["unlinkat"] = cleanup_error
        with self.assertRaises(ProjectStateError) as ctx:
            _publish_metadata(self.namespace_fd, self.expected, _LABEL)
        cause = ctx.exception.__cause__
        self.assertIsInstance(cause, DestinationExists)
        self.assertIn(cleanup_error, cause.secondary)
        # The concurrent winner is neither deleted nor replaced.
        self.assertEqual(self.target.read_bytes(), self.expected)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)

    def test_mismatched_final_collision_is_rejected(self) -> None:
        other = self.base / "other"
        other.mkdir()
        other_canonical = other.resolve()
        other_bytes = _metadata(other_canonical, _project_identity(other_canonical))
        self.target.write_bytes(other_bytes)
        os.chmod(self.target, 0o600)
        _publish_metadata(self.namespace_fd, self.expected, _LABEL)
        with self.assertRaises(ProjectStateError) as ctx:
            _read_metadata(self.namespace_fd, self.expected, _LABEL)
        self.assertIn("does not match", str(ctx.exception))
        self.assertEqual(self.target.read_bytes(), other_bytes)

    def test_unsafe_final_collision_is_preserved_and_rejected(self) -> None:
        outside = self.base / "outside.json"
        outside.write_text("outside")
        self.target.symlink_to(outside)
        _publish_metadata(self.namespace_fd, self.expected, _LABEL)
        self.assertTrue(self.target.is_symlink())
        self.assertEqual(outside.read_text(), "outside")
        with self.assertRaises(ProjectStateError):
            _read_metadata(self.namespace_fd, self.expected, _LABEL)
        self.assertEqual(self._leftovers(), [])

    def test_non_regular_and_permissive_entries_are_rejected(self) -> None:
        self.target.mkdir()
        with self.assertRaises(ProjectStateError):
            _read_metadata(self.namespace_fd, self.expected, _LABEL)
        self.target.rmdir()
        self.target.write_bytes(self.expected)
        os.chmod(self.target, 0o644)
        with self.assertRaises(ProjectStateError):
            _read_metadata(self.namespace_fd, self.expected, _LABEL)

    def test_foreign_owned_entry_is_rejected(self) -> None:
        self.target.write_bytes(self.expected)
        os.chmod(self.target, 0o600)
        target = os.path.realpath(str(self.target))
        real_fstat = os.fstat

        def foreign(fd, *args, **kwargs):
            value = real_fstat(fd, *args, **kwargs)
            try:
                name = os.path.realpath(os.readlink(f"/proc/self/fd/{fd}"))
            except OSError:
                name = ""
            if name == target:
                return types.SimpleNamespace(
                    st_mode=value.st_mode,
                    st_uid=os.geteuid() + 1,
                    st_gid=value.st_gid,
                )
            return value

        with mock.patch.object(project_state_module.os, "fstat", side_effect=foreign):
            with self.assertRaises(ProjectStateError):
                _read_metadata(self.namespace_fd, self.expected, _LABEL)


class ResolveProjectStatePublicationTests(_ProjectMetadataCase):
    def test_resolve_publishes_exact_metadata_with_private_mode(self) -> None:
        state = resolve_project_state(self.project, cache_root=self.cache)
        target = state.namespace / "project.json"
        expected = _metadata(state.project_path, state.identity)
        self.assertEqual(target.read_bytes(), expected)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(
            temporary_entries(str(state.namespace)),
            [],
        )


class NamespaceCloseMaskingTests(_ProjectMetadataCase):
    """``ValidatedProjectState.__exit__`` close failures are secondary-only."""

    def _state(self):
        return resolve_project_state(self.project, cache_root=self.cache)

    def _inject(self, close_error, recorded):
        real_close = os.close

        def closing(fd):
            if fd == recorded.get("fd"):
                recorded["attempts"] = recorded.get("attempts", 0) + 1
                raise close_error
            return real_close(fd)

        return closing

    def test_close_failure_does_not_mask_active_failure(self) -> None:
        state = self._state()
        primary = OSError(errno.EIO, "injected body failure")
        close_error = OSError(errno.EIO, "injected namespace close")
        recorded: dict[str, int] = {}
        with mock.patch.object(
            project_state_module.os,
            "close",
            side_effect=self._inject(close_error, recorded),
        ):
            with self.assertRaises(OSError) as ctx:
                with validate_project_state(state) as validated:
                    recorded["fd"] = validated.namespace_fd
                    raise primary
        # The body failure stays the raised primary exception.
        self.assertIs(ctx.exception, primary)
        self.assertEqual(primary.errno, errno.EIO)
        secondary = list(getattr(primary, "_transaction_secondary", []))
        self.assertEqual(sum(1 for exc in secondary if exc is close_error), 1)
        self.assertIsNot(primary, close_error)
        # The descriptor was closed exactly once (no retry).
        self.assertEqual(recorded.get("attempts"), 1)

    def test_close_failure_propagates_when_body_succeeds(self) -> None:
        state = self._state()
        close_error = OSError(errno.EIO, "injected namespace close")
        recorded: dict[str, int] = {}
        with mock.patch.object(
            project_state_module.os,
            "close",
            side_effect=self._inject(close_error, recorded),
        ):
            with self.assertRaises(OSError) as ctx:
                with validate_project_state(state) as validated:
                    recorded["fd"] = validated.namespace_fd
        # No earlier exception exists, so the close error is primary.
        self.assertIs(ctx.exception, close_error)
        self.assertEqual(recorded.get("attempts"), 1)

    def test_close_interruption_propagates_unchanged(self) -> None:
        state = self._state()
        interruption = KeyboardInterrupt()
        recorded: dict[str, int] = {}
        with mock.patch.object(
            project_state_module.os,
            "close",
            side_effect=self._inject(interruption, recorded),
        ):
            with self.assertRaises(KeyboardInterrupt) as ctx:
                with validate_project_state(state) as validated:
                    recorded["fd"] = validated.namespace_fd
        # The interruption is neither attached as secondary nor converted.
        self.assertIs(ctx.exception, interruption)
        self.assertEqual(
            list(getattr(interruption, "_transaction_secondary", [])), []
        )
        self.assertEqual(recorded.get("attempts"), 1)


class PublicationCapabilityBoundaryTests(_ProjectMetadataCase):
    """Capability creation is inside the publication domain-error boundary."""

    def test_capability_fstat_failure_is_publication_error(self) -> None:
        raw = OSError(errno.EIO, "injected capability fstat")
        self.ops.failures["fstat"] = raw
        with self.assertRaises(ProjectStateError) as ctx:
            _publish_metadata(self.namespace_fd, self.expected, _LABEL)
        self.assertEqual(
            str(ctx.exception), f"cannot publish project identity metadata {_LABEL}"
        )
        # The typed validation wrapper is chained directly from the domain
        # error, and the raw operational failure stays reachable through the
        # wrapper's stored and direct cause.
        wrapped = ctx.exception.__cause__
        self.assertIsInstance(wrapped, TransactionError)
        self.assertEqual(wrapped.stage, STAGE_VALIDATE)
        self.assertIs(wrapped.cause, raw)
        self.assertIs(wrapped.__cause__, raw)
        self.assertEqual(wrapped.cause.errno, errno.EIO)
        # Nothing was published.
        self.assertFalse(self.target.exists())
        self.assertEqual(self._leftovers(), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
