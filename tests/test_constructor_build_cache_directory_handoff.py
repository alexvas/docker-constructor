"""Phase 5 — build-cache directory handoff ownership.

``docker/versioning/build_cache.py::_open_relative_dir`` and
``_bootstrap_constructor_project_lock_parent`` walk a descriptor-relative
directory chain.  Before migration both retained every intermediate descriptor
as a raw integer and released it with a direct ``os.close``.  That makes the
fd ledger implicit:

* there is no explicit terminal ownership record for the retained parent, so a
  failed parent close could be retried and an already-open child could leak;
* an unsafe child rejection followed by an ordinary close failure could mask
  the intended ``BuildTransactionError``; and
* the ``_bootstrap`` walker left the freshly opened child untracked when
  ``fstat`` or the parent release failed.

These tests pin the Phase 5 contract:

* every open, transfer, return, and close is accounted for by one shared
  ``OwnedDescriptor`` ledger (``RelativeDirLedgerTests``);
* each child is owned before the parent is released, the parent becomes
  terminal before its handoff close, and a failed parent close is never
  retried (``RelativeDirHandoffTests``);
* validation rejection stays primary over an ordinary close failure and the
  opened child is released exactly once
  (``BootstrapLockParentTests``); and
* the executable decision test confirms ``_open_relative_dir`` cannot use
  ``DirectoryDescriptor`` without changing its established contract, so it
  uses an ``OwnedDescriptor`` ledger
  (``HandoffDecisionTests``).
"""
from __future__ import annotations

import contextlib
import errno
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import docker.versioning.build_cache as build_cache
from docker.filesystem.descriptors import (
    DescriptorError,
    DirectoryDescriptor,
    OwnedDescriptor,
)
from docker.filesystem.operations import PosixDescriptorOps
from docker.versioning.project_state import resolve_project_state


def _secondary_of(exc: BaseException) -> list[BaseException]:
    return list(getattr(exc, "_transaction_secondary", []))


def _foreign_uid(stat_result: os.stat_result) -> os.stat_result:
    """Return *stat_result* with a uid that no longer matches ``geteuid()``."""
    return os.stat_result(
        (
            stat_result.st_mode,
            stat_result.st_ino,
            stat_result.st_dev,
            stat_result.st_nlink,
            stat_result.st_uid + 1,
            stat_result.st_gid,
            stat_result.st_size,
            stat_result.st_atime,
            stat_result.st_mtime,
            stat_result.st_ctime,
        )
    )


class _OwnerLedger:
    """Record every shared-owner construction and first release attempt."""

    def __init__(self) -> None:
        self.owners: list[OwnedDescriptor] = []
        self.releases: list[int] = []

    @property
    def owned_fds(self) -> list[int]:
        return [owner._fd for owner in self.owners]

    @property
    def retained_owners(self) -> list[OwnedDescriptor]:
        """Owners that were not transferred to another shared owner.

        A transferred owner handed its descriptor to a successor (for example
        ``_open_namespace_fd`` now adopts its descriptor and transfers it to
        the caller), so it is neither a leak nor a release obligation.
        """
        return [owner for owner in self.owners if owner._state != "transferred"]

    def owner_for(self, fd: int) -> OwnedDescriptor:
        for owner in self.owners:
            if owner._fd == fd and owner._state != "transferred":
                return owner
        raise AssertionError(f"no recorded owner for fd {fd}")

    @contextlib.contextmanager
    def install(self):
        ledger = self

        class _RecordingOwner(OwnedDescriptor):
            def __init__(self, ops, fd, *, label):
                super().__init__(ops, fd, label=label)
                ledger.owners.append(self)

            def close(self):
                if not self.released:
                    ledger.releases.append(self._fd)
                super().close()

        with mock.patch.object(build_cache, "OwnedDescriptor", _RecordingOwner):
            yield ledger


class _OpenRecorder:
    """Record every descriptor returned by ``os.dup``/``os.open``."""

    def __init__(self) -> None:
        self.opened: list[int] = []

    @contextlib.contextmanager
    def install(self):
        recorder = self
        real_dup = os.dup
        real_open = os.open

        def dup(fd: int) -> int:
            new_fd = real_dup(fd)
            recorder.opened.append(new_fd)
            return new_fd

        def open_(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            recorder.opened.append(fd)
            return fd

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(build_cache.os, "dup", dup))
            stack.enter_context(mock.patch.object(build_cache.os, "open", open_))
            yield recorder


class _CloseFaults:
    """Record closes and inject faults by close-call index or armed position."""

    def __init__(self) -> None:
        self.real_close = os.close
        self.calls: list[int] = []
        self.faulted: list[int] = []
        self.faults: dict[int, BaseException] = {}
        self.armed = False
        self.armed_calls: list[int] = []
        self.armed_fault_plan: list[BaseException | None] = []

    def close(self, fd: int) -> None:
        index = len(self.calls)
        self.calls.append(fd)
        if self.armed:
            armed_index = len(self.armed_calls)
            self.armed_calls.append(fd)
            if armed_index < len(self.armed_fault_plan):
                fault = self.armed_fault_plan[armed_index]
                if fault is not None:
                    self.faulted.append(fd)
                    raise fault
        else:
            fault = self.faults.get(index)
            if fault is not None:
                self.faulted.append(fd)
                raise fault
        self.real_close(fd)

    def cleanup(self) -> None:
        for fd in dict.fromkeys(self.faulted):
            try:
                self.real_close(fd)
            except OSError:
                pass

    @contextlib.contextmanager
    def index_faults(self, faults):
        self.faults = dict(faults)
        with mock.patch.object(build_cache.os, "close", self.close):
            try:
                yield self
            finally:
                self.cleanup()

    @contextlib.contextmanager
    def armed_faults(self, faults):
        self.armed_fault_plan = list(faults)
        with mock.patch.object(build_cache.os, "close", self.close):
            try:
                yield self
            finally:
                self.cleanup()


@contextlib.contextmanager
def _arm_after_namespace_open(controller: _CloseFaults, *, foreign_uid: bool):
    """Arm close faults and foreign-uid fstat after the namespace is opened.

    ``resolve_project_state`` opens and validates its own descriptors before
    ``_bootstrap_constructor_project_lock_parent`` opens the namespace fd, so
    the fault/foreign-uid window starts only once ``_open_namespace_fd`` has
    returned.
    """
    real_open_namespace = build_cache._open_namespace_fd
    real_fstat = os.fstat
    armed = controller.armed

    def open_namespace(namespace):
        fd = real_open_namespace(namespace)
        controller.armed = True
        return fd

    def fstat(fd):
        result = real_fstat(fd)
        if foreign_uid and controller.armed:
            return _foreign_uid(result)
        return result

    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(build_cache.os, "fstat", fstat))
        stack.enter_context(
            mock.patch.object(build_cache, "_open_namespace_fd", open_namespace)
        )
        try:
            yield
        finally:
            controller.armed = armed


class _RelativeDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.namespace = self.root / "namespace"
        self.namespace.mkdir(mode=0o700)
        self.namespace_fd = os.open(
            self.namespace, os.O_RDONLY | os.O_DIRECTORY
        )
        self.addCleanup(self._close_namespace)

    def _close_namespace(self) -> None:
        try:
            os.close(self.namespace_fd)
        except OSError:
            pass

    def _close_fd(self, fd: int) -> None:
        try:
            os.close(fd)
        except OSError:
            pass


class RelativeDirLedgerTests(_RelativeDirCase):
    """Tasks 5.1–5.2 — the fd ledger accounts for every descriptor."""

    def test_success_walk_accounts_for_every_open_transfer_and_close(self) -> None:
        with _OwnerLedger().install() as ledger, _OpenRecorder().install() as opened:
            fd = build_cache._open_relative_dir(
                self.namespace_fd, ("a", "b"), create=True
            )
        self.addCleanup(self._close_fd, fd)

        self.assertTrue(stat.S_ISDIR(os.fstat(fd).st_mode))
        # Every descriptor opened (the namespace duplicate and both children)
        # is represented by exactly one shared owner.
        self.assertEqual(ledger.owned_fds, opened.opened)
        self.assertEqual(len(ledger.owners), 3)
        # Intermediate parents are released once, in handoff order; the final
        # descriptor is transferred live to the caller.
        self.assertEqual(ledger.releases, ledger.owned_fds[:2])
        self.assertEqual(fd, ledger.owned_fds[-1])
        # fd numbers may be reused after a close, so assert terminal state on
        # the owner objects in creation order rather than by fd membership.
        self.assertFalse(ledger.owners[-1].released)
        self.assertTrue(ledger.owners[0].released)
        self.assertTrue(ledger.owners[1].released)

    def test_missing_without_create_releases_owner_and_returns_none(self) -> None:
        with _OwnerLedger().install() as ledger, _OpenRecorder().install() as opened:
            result = build_cache._open_relative_dir(
                self.namespace_fd, ("missing",), create=False
            )

        self.assertIsNone(result)
        self.assertEqual(ledger.owned_fds, opened.opened)
        self.assertEqual(ledger.owned_fds, ledger.releases)
        for owner in ledger.owners:
            self.assertTrue(owner.released)

    def test_open_failure_maps_to_build_cache_error_and_releases_owner(self) -> None:
        cause = OSError(errno.EACCES, "open denied")
        real_open = os.open

        def open_(path, flags, *args, **kwargs):
            if kwargs.get("dir_fd") is not None:
                raise cause
            return real_open(path, flags, *args, **kwargs)

        with _OwnerLedger().install() as ledger:
            with mock.patch.object(build_cache.os, "open", open_):
                with self.assertRaises(build_cache.BuildCacheError) as ctx:
                    build_cache._open_relative_dir(
                        self.namespace_fd, ("a",), create=True
                    )
        self.assertIn("cannot open build-cache component 'a'", str(ctx.exception))
        self.assertIs(ctx.exception.__cause__, cause)
        self.assertEqual(ledger.releases, ledger.owned_fds)
        self.assertEqual(len(ledger.owned_fds), 1)

    def test_stat_failure_maps_to_build_cache_error_and_releases_owner(self) -> None:
        (self.namespace / "a").mkdir(mode=0o700)
        cause = OSError(errno.EACCES, "stat denied")
        real_stat = os.stat

        def stat_(path, *args, **kwargs):
            if kwargs.get("dir_fd") is not None:
                raise cause
            return real_stat(path, *args, **kwargs)

        with _OwnerLedger().install() as ledger:
            with mock.patch.object(build_cache.os, "stat", stat_):
                with self.assertRaises(build_cache.BuildCacheError) as ctx:
                    build_cache._open_relative_dir(
                        self.namespace_fd, ("a",), create=True
                    )
        self.assertIn("cannot access build-cache component 'a'", str(ctx.exception))
        self.assertEqual(ledger.releases, ledger.owned_fds)

    def test_mkdir_failure_maps_to_build_cache_error_and_releases_owner(self) -> None:
        cause = OSError(errno.EACCES, "mkdir denied")
        real_mkdir = os.mkdir

        def mkdir_(path, mode=0o777, *args, **kwargs):
            if kwargs.get("dir_fd") is not None:
                raise cause
            return real_mkdir(path, mode, *args, **kwargs)

        with _OwnerLedger().install() as ledger:
            with mock.patch.object(build_cache.os, "mkdir", mkdir_):
                with self.assertRaises(build_cache.BuildCacheError) as ctx:
                    build_cache._open_relative_dir(
                        self.namespace_fd, ("a",), create=True
                    )
        self.assertIn("cannot create build-cache component 'a'", str(ctx.exception))
        self.assertEqual(ledger.releases, ledger.owned_fds)

    def test_fchmod_failure_releases_child_and_parent_once(self) -> None:
        defect = OSError(errno.EIO, "fchmod failed")
        with _OwnerLedger().install() as ledger:
            with mock.patch.object(build_cache.os, "fchmod", side_effect=defect):
                with self.assertRaises(OSError) as ctx:
                    build_cache._open_relative_dir(
                        self.namespace_fd, ("a",), create=True
                    )
        self.assertIs(ctx.exception, defect)
        # The child was opened before fchmod ran, so both the child and the
        # retained parent receive exactly one release attempt, child first.
        self.assertEqual(len(ledger.owned_fds), 2)
        self.assertEqual(ledger.releases, [ledger.owned_fds[1], ledger.owned_fds[0]])
        for owner in ledger.owners:
            self.assertTrue(owner.released)

    def test_unsafe_component_rejected_and_owner_released(self) -> None:
        (self.namespace / "link").symlink_to(self.root)
        with _OwnerLedger().install() as ledger:
            with self.assertRaises(build_cache.BuildCacheError) as ctx:
                build_cache._open_relative_dir(
                    self.namespace_fd, ("link",), create=False
                )
        self.assertIn("unsafe build-cache component 'link'", str(ctx.exception))
        self.assertEqual(ledger.releases, ledger.owned_fds)
        for owner in ledger.owners:
            self.assertTrue(owner.released)

    def test_repeated_close_after_failure_is_noop(self) -> None:
        cause = OSError(errno.EACCES, "open denied")
        real_open = os.open

        def open_(path, flags, *args, **kwargs):
            if kwargs.get("dir_fd") is not None:
                raise cause
            return real_open(path, flags, *args, **kwargs)

        with _OwnerLedger().install() as ledger:
            with mock.patch.object(build_cache.os, "open", open_):
                with self.assertRaises(build_cache.BuildCacheError):
                    build_cache._open_relative_dir(
                        self.namespace_fd, ("a",), create=True
                    )
            before = list(ledger.releases)
            for owner in ledger.owners:
                owner.close()
            self.assertEqual(ledger.releases, before)


class RelativeDirHandoffTests(_RelativeDirCase):
    """Task 5.3 — parent release is terminal and never retried."""

    def test_parent_close_failure_releases_child_without_retrying_parent(self) -> None:
        fault = OSError(errno.EIO, "parent close failed")
        with _OwnerLedger().install() as ledger, _CloseFaults().index_faults(
            {0: fault}
        ) as closes:
            with self.assertRaises(OSError) as ctx:
                build_cache._open_relative_dir(
                    self.namespace_fd, ("a",), create=True
                )
        self.assertIs(ctx.exception, fault)
        parent_fd, child_fd = ledger.owned_fds
        self.assertEqual(closes.calls, [parent_fd, child_fd])
        self.assertEqual(ledger.releases, [parent_fd, child_fd])
        # The parent became terminal even though its close failed, so it was
        # never retried; the child was released exactly once and not returned.
        self.assertTrue(ledger.owner_for(parent_fd).released)
        self.assertTrue(ledger.owner_for(child_fd).released)

    def test_parent_close_interruption_releases_child_and_stays_authoritative(self) -> None:
        with _OwnerLedger().install() as ledger, _CloseFaults().index_faults(
            {0: KeyboardInterrupt()}
        ) as closes:
            with self.assertRaises(KeyboardInterrupt):
                build_cache._open_relative_dir(
                    self.namespace_fd, ("a", "b"), create=True
                )
        parent_fd, child_fd = ledger.owned_fds
        self.assertEqual(closes.calls, [parent_fd, child_fd])
        for owner in ledger.owners:
            self.assertTrue(owner.released)


class _BootstrapCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.project = base / "project"
        self.project.mkdir()
        self.cache = base / "cache"
        self.cache.mkdir(mode=0o700)

    def _bootstrap(self):
        return build_cache._bootstrap_constructor_project_lock_parent(
            self.project, cache_root=self.cache
        )


class BootstrapCharacterizationTests(_BootstrapCase):
    """Task 5.6 — characterize the pre-migration bootstrap contract."""

    def test_bootstrap_creates_private_component_and_returns_child_fd(self) -> None:
        state, fd = self._bootstrap()
        self.addCleanup(self._close_fd, fd)
        self.assertTrue(stat.S_ISDIR(os.fstat(fd).st_mode))
        component = state.namespace / "build-artifacts"
        info = os.stat(component, follow_symlinks=False)
        self.assertTrue(stat.S_ISDIR(info.st_mode))
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o700)

    def test_bootstrap_is_idempotent(self) -> None:
        state, first = self._bootstrap()
        self.addCleanup(self._close_fd, first)
        state2, second = self._bootstrap()
        self.addCleanup(self._close_fd, second)
        self.assertEqual(state.namespace, state2.namespace)
        component = state.namespace / "build-artifacts"
        self.assertTrue(stat.S_ISDIR(os.stat(component).st_mode))

    def _close_fd(self, fd: int) -> None:
        try:
            os.close(fd)
        except OSError:
            pass


class BootstrapLockParentTests(_BootstrapCase):
    """Tasks 5.4–5.5 — bootstrap rejection and handoff precedence."""

    def test_unsafe_child_rejection_stays_primary_over_close_failure(self) -> None:
        fault = OSError(errno.EIO, "child close failed")
        with _OwnerLedger().install() as ledger:
            with _CloseFaults().armed_faults([fault]) as closes:
                with _arm_after_namespace_open(closes, foreign_uid=True):
                    with self.assertRaises(build_cache.BuildTransactionError) as ctx:
                        self._bootstrap()
        self.assertIn("unsafe lock parent component", str(ctx.exception))
        # The close failure is secondary diagnostic context, not the primary.
        self.assertTrue(
            any(item is fault for item in _secondary_of(ctx.exception)),
            _secondary_of(ctx.exception),
        )
        # The opened child and the namespace each received one release attempt.
        self.assertEqual(len(closes.armed_calls), 2)
        self.assertEqual(len(set(closes.armed_calls)), 2)
        self.assertEqual(len(ledger.releases), len(ledger.retained_owners))
        for owner in ledger.retained_owners:
            self.assertTrue(owner.released)

    def test_parent_close_failure_is_not_retried_and_child_does_not_leak(self) -> None:
        fault = OSError(errno.EIO, "parent close failed")
        with mock.patch.object(
            build_cache, "_PERSISTENT_PARTS", ("alpha", "beta")
        ):
            with _OwnerLedger().install() as ledger:
                with _CloseFaults().armed_faults([fault, None, None]) as closes:
                    with _arm_after_namespace_open(closes, foreign_uid=False):
                        with self.assertRaises(OSError) as ctx:
                            self._bootstrap()
        self.assertIs(ctx.exception, fault)
        # The first armed close is the intermediate parent; the second is the
        # already-open child; the third is the independent namespace cleanup.
        # The failed parent is never retried.
        self.assertEqual(len(closes.armed_calls), 3)
        parent_fd, child_fd, namespace_fd = closes.armed_calls
        self.assertEqual(closes.armed_calls.count(parent_fd), 1)
        self.assertEqual(closes.armed_calls.count(child_fd), 1)
        parent_owner = ledger.owner_for(parent_fd)
        child_owner = ledger.owner_for(child_fd)
        self.assertTrue(parent_owner.released)
        self.assertTrue(child_owner.released)
        # The namespace is cleaned up independently.
        self.assertTrue(ledger.owner_for(namespace_fd).released)

    def test_parent_close_interruption_stays_authoritative(self) -> None:
        with mock.patch.object(
            build_cache, "_PERSISTENT_PARTS", ("alpha", "beta")
        ):
            with _OwnerLedger().install() as ledger:
                with _CloseFaults().armed_faults([KeyboardInterrupt(), None, None]) as closes:
                    with _arm_after_namespace_open(closes, foreign_uid=False):
                        with self.assertRaises(KeyboardInterrupt):
                            self._bootstrap()
        self.assertEqual(len(closes.armed_calls), 3)
        parent_fd, child_fd, namespace_fd = closes.armed_calls
        self.assertEqual(closes.armed_calls.count(parent_fd), 1)
        self.assertEqual(closes.armed_calls.count(child_fd), 1)
        for owner in ledger.retained_owners:
            self.assertTrue(owner.released)


@contextlib.contextmanager
def _record_build_cache_creation(events: list[str]):
    """Append the mode/creation descriptor operations ``_open_relative_dir`` runs."""
    real_mkdir = os.mkdir
    real_chmod = os.chmod
    real_open = os.open
    real_fchmod = os.fchmod

    def mkdir(path, mode=0o777, *args, **kwargs):
        events.append("mkdir")
        return real_mkdir(path, mode, *args, **kwargs)

    def chmod(path, mode, *args, **kwargs):
        events.append("chmod")
        return real_chmod(path, mode, *args, **kwargs)

    def open_(path, flags, *args, **kwargs):
        events.append("open")
        return real_open(path, flags, *args, **kwargs)

    def fchmod(fd: int, mode: int) -> None:
        events.append("fchmod")
        return real_fchmod(fd, mode)

    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(build_cache.os, "mkdir", mkdir))
        stack.enter_context(mock.patch.object(build_cache.os, "chmod", chmod))
        stack.enter_context(mock.patch.object(build_cache.os, "open", open_))
        stack.enter_context(mock.patch.object(build_cache.os, "fchmod", fchmod))
        yield events


@contextlib.contextmanager
def _umask(mask: int):
    previous = os.umask(mask)
    try:
        yield
    finally:
        os.umask(previous)


class _RecordingDescriptorOps(PosixDescriptorOps):
    """Record the capability's descriptor-relative creation operations."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def mkdirat(self, directory_fd: int, name: str, mode: int) -> None:
        self.events.append("mkdirat")
        return super().mkdirat(directory_fd, name, mode)

    def openat(
        self, directory_fd: int | None, name: str, flags: int, mode: int = 0
    ) -> int:
        self.events.append("openat")
        return super().openat(directory_fd, name, flags, mode)

    def fstat(self, fd: int) -> os.stat_result:
        self.events.append("fstat")
        return super().fstat(fd)

    def fchmod(self, fd: int, mode: int) -> None:
        self.events.append("fchmod")
        return super().fchmod(fd, mode)


class HandoffDecisionTests(_RelativeDirCase):
    """Task 5.7 — an executable comparison selects the owner ledger.

    ``_open_relative_dir`` keeps its own ``OwnedDescriptor`` ledger instead of
    delegating to ``DirectoryDescriptor`` because the two creation contracts
    genuinely differ: the walker secures the freshly created pathname to
    ``0700`` *before* opening it, then enforces the mode again on the
    descriptor, while the capability opens and validates the child before
    applying the descriptor mode.  Under a umask that strips owner permissions
    the walker can still open what it created and the capability cannot, so the
    walker's release ledger cannot be replaced by the capability.
    """

    def test_creation_contracts_diverge_in_operation_order(self) -> None:
        walker_events: list[str] = []
        with _record_build_cache_creation(walker_events):
            walker_fd = build_cache._open_relative_dir(
                self.namespace_fd, ("walker-child",), create=True
            )
        self.addCleanup(self._close_fd, walker_fd)

        capability_ops = _RecordingDescriptorOps()
        namespace_cap = DirectoryDescriptor.adopt(
            capability_ops,
            os.dup(self.namespace_fd),
            label="namespace",
            require_owner=False,
        )
        self.addCleanup(namespace_cap.close)
        capability_ops.events.clear()
        child_cap = namespace_cap.create_directory(
            "capability-child", mode=0o700, require_owner=False
        )
        self.addCleanup(child_cap.close)

        # The walker chmods the pathname to 0700 before opening it, then
        # re-applies the mode on the descriptor.
        self.assertEqual(walker_events, ["mkdir", "chmod", "open", "fchmod"])
        # The capability creates, opens, and validates the child before it
        # applies the descriptor mode; it never secures the pathname first.
        self.assertEqual(
            capability_ops.events, ["mkdirat", "openat", "fstat", "fchmod"]
        )
        self.assertLess(walker_events.index("chmod"), walker_events.index("open"))
        self.assertLess(
            capability_ops.events.index("openat"),
            capability_ops.events.index("fchmod"),
        )
        self.assertNotIn("chmod", capability_ops.events)

    def test_restrictive_umask_exposes_the_creation_contract_mismatch(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("requires an unprivileged owner to observe EACCES")
        # A umask that strips every permission bit makes ``mkdir`` create a
        # mode-0000 directory; only an implementation that repairs the mode
        # before opening can still open what it just created.
        with _umask(0o777):
            walker_fd = build_cache._open_relative_dir(
                self.namespace_fd, ("walker-child",), create=True
            )
        self.addCleanup(self._close_fd, walker_fd)
        self.assertEqual(stat.S_IMODE(os.fstat(walker_fd).st_mode), 0o700)

        capability_ops = PosixDescriptorOps()
        namespace_cap = DirectoryDescriptor.adopt(
            capability_ops,
            os.dup(self.namespace_fd),
            label="namespace",
            require_owner=False,
        )
        self.addCleanup(namespace_cap.close)
        with _umask(0o777):
            with self.assertRaises(DescriptorError) as ctx:
                namespace_cap.create_directory(
                    "capability-child", mode=0o700, require_owner=False
                )
        # The capability created the entry and then failed to open it because
        # the umask-stripped mode is applied only after the open.
        self.assertIsInstance(ctx.exception.__cause__, PermissionError)
        created = self.namespace / "capability-child"
        self.assertEqual(stat.S_IMODE(os.stat(created).st_mode), 0o000)
        # Restore traverse permission so the temporary tree can be removed.
        os.chmod(created, 0o700)

    def test_open_relative_dir_uses_owned_descriptor_ledger(self) -> None:
        # Implementation-convergence check only: the walker must release every
        # descriptor through its own ``OwnedDescriptor`` ledger.  This asserts
        # the migration result, not that the two APIs are semantically equal.
        import ast

        source = Path(build_cache.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        aliases: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_open_relative_dir"
        )
        owner_calls = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", None) == "OwnedDescriptor"
        ]
        self.assertGreaterEqual(len(owner_calls), 2)
        self.assertIn("OwnedDescriptor", aliases)
        self.assertNotIn("DirectoryDescriptor", aliases)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
