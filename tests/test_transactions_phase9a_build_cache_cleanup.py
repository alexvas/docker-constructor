"""Phase 9A — build-cache publication and state-open cleanup hardening.

These tests fault-inject the two ``docker/versioning/build_cache.py`` cleanup
paths fixed for primary-preserving release:

* :func:`publish_verified_blob` — on the failure path the temporary payload
  descriptor, the temporary entry, and every other publication descriptor are
  released through one accumulator.  A close interruption or unexpected defect
  must neither skip the temporary unlink nor retry the failed descriptor close,
  and only declared absence (``FileNotFoundError``) may be suppressed for the
  unlink.

* :func:`open_build_cache_state` — if the namespace close fails after all child
  descriptors were opened, every retained child must be released exactly once
  before the failure propagates.  On the failed-open path an interruption
  raised while releasing a child is authoritative over the original open
  failure and later ordinary namespace release failures.

Descriptors are never identified by fd number alone: a faulted close leaves its
descriptor open, but fd numbers are otherwise reused freely.  Counts are
therefore taken from the post-fault call window and from the snapshot of
descriptors that were open when the fault was injected.
"""
from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import docker.versioning.build_cache as build_cache
from docker.versioning.digest_identity import DigestIdentity
from docker.versioning.project_state import resolve_project_state


def _secondary_of(exc: BaseException) -> list[BaseException]:
    return list(getattr(exc, "_transaction_secondary", []))


class _CloseFaults:
    """Record descriptor opens/closes and inject close faults by fd policy."""

    def __init__(self, paths, policy=None):
        self.real_close = os.close
        self.paths = paths
        self.calls: list[int] = []
        self.attempts: dict[int, int] = {}
        self.faulted: list[int] = []
        self.fault_index: dict[int, int] = {}
        self.fault_snapshot: dict[int, frozenset[int]] = {}
        self.open_fds: set[int] = set()
        self._policy = policy or (lambda fd, path, attempt: None)

    def note_open(self, fd: int) -> None:
        self.open_fds.add(fd)

    def close(self, fd: int) -> None:
        index = len(self.calls)
        self.calls.append(fd)
        self.attempts[fd] = self.attempts.get(fd, 0) + 1
        fault = self._policy(fd, self.paths.get(fd), self.attempts[fd])
        if fault is not None:
            self.faulted.append(fd)
            self.fault_index.setdefault(fd, index)
            self.fault_snapshot.setdefault(fd, frozenset(self.open_fds))
            raise fault
        self.real_close(fd)
        self.open_fds.discard(fd)

    def post_fault(self, fd: int) -> list[int]:
        return self.calls[self.fault_index[fd] + 1:]

    def cleanup(self) -> None:
        for fd in dict.fromkeys(self.faulted):
            try:
                self.real_close(fd)
            except OSError:
                pass


class _BuildCacheCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.project = self.base / "project"
        self.project.mkdir()
        self.cache = self.base / "cache"
        self.cache.mkdir(mode=0o700)
        # Warm the namespace, then skip the (idempotent) preparation call inside
        # the functions under test so fd churn cannot pollute call counting.
        resolve_project_state(self.project, cache_root=self.cache)
        self.paths = build_cache.prepare_build_cache(self.project, cache_root=self.cache)

    def identity(self, data: bytes) -> DigestIdentity:
        return DigestIdentity.from_hex("sha256", hashlib.sha256(data).hexdigest())

    def publish(self, data: bytes = b"payload"):
        return build_cache.publish_verified_blob(
            self.identity(data), data,
            constructor_project_root=self.project, cache_root=self.cache,
        )

    @contextlib.contextmanager
    def _publication_instruments(self, *, close_policy=None, unlink_policy=None):
        """Yield (faults, paths, unlinks) with ``os.replace`` failure injected."""

        paths: dict[int, str] = {}
        faults = _CloseFaults(paths, close_policy)
        unlinks: list[str] = []
        real_open = os.open
        real_unlink = os.unlink
        real_replace = os.replace

        def recording_open(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            paths[fd] = os.fspath(path)
            faults.note_open(fd)
            return fd

        def recording_unlink(path, *, dir_fd=None):
            name = os.fspath(path)
            unlinks.append(name)
            if unlink_policy is not None:
                fault = unlink_policy(name)
                if fault is not None:
                    raise fault
            return real_unlink(path, dir_fd=dir_fd)

        def failing_replace(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
            if os.fspath(src).startswith(".publish-"):
                raise build_cache.BuildCacheError("injected publication failure")
            return real_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                build_cache, "prepare_build_cache", lambda *a, **k: self.paths))
            stack.enter_context(mock.patch.object(os, "close", faults.close))
            stack.enter_context(mock.patch.object(os, "open", recording_open))
            stack.enter_context(mock.patch.object(os, "unlink", recording_unlink))
            stack.enter_context(mock.patch.object(os, "replace", failing_replace))
            try:
                yield faults, paths, unlinks
            finally:
                faults.cleanup()

    @contextlib.contextmanager
    def _state_instruments(self, *, close_policy=None, fail_child_open_at=None):
        """Yield (faults, namespace_fds, child_fds) with child opens recorded."""

        paths: dict[int, str] = {}
        faults = _CloseFaults(paths, close_policy)
        namespace_fds: list[int] = []
        child_fds: list[int] = []
        real_namespace = build_cache._open_namespace_fd
        real_child = build_cache._open_validated_child

        def recording_namespace(namespace):
            fd = real_namespace(namespace)
            paths[fd] = os.fspath(namespace)
            faults.note_open(fd)
            namespace_fds.append(fd)
            return fd

        def recording_child(parent_fd, name, *, label):
            if fail_child_open_at is not None and len(child_fds) == fail_child_open_at:
                raise build_cache.BuildCacheError(f"injected child open failure: {label}")
            fd = real_child(parent_fd, name, label=label)
            paths[fd] = name
            faults.note_open(fd)
            child_fds.append(fd)
            return fd

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                build_cache, "prepare_build_cache", lambda *a, **k: self.paths))
            stack.enter_context(mock.patch.object(os, "close", faults.close))
            stack.enter_context(mock.patch.object(build_cache, "_open_namespace_fd", recording_namespace))
            stack.enter_context(mock.patch.object(build_cache, "_open_validated_child", recording_child))
            try:
                yield faults, namespace_fds, child_fds
            finally:
                faults.cleanup()


class PublicationCleanupTests(_BuildCacheCase):
    """``publish_verified_blob`` failure-path release."""

    def _temporary_fd(self, paths: dict[int, str]) -> int:
        return next(fd for fd, name in paths.items() if name.startswith(".publish-"))

    def test_close_interruption_still_unlinks_temporary_entry(self) -> None:
        interruption = KeyboardInterrupt()
        policy = lambda fd, path, attempt: interruption if path and path.startswith(".publish-") else None

        with self._publication_instruments(close_policy=policy) as (faults, paths, unlinks):
            with self.assertRaises(KeyboardInterrupt) as caught:
                self.publish()
            temp_fd = self._temporary_fd(paths)
            snapshot = faults.fault_snapshot[temp_fd]
            post = faults.post_fault(temp_fd)
            unlink_count = sum(1 for name in unlinks if name.startswith(".publish-"))

        self.assertIs(caught.exception, interruption)
        self.assertEqual(unlink_count, 1, "temporary entry was not unlinked exactly once")
        self.assertEqual(post.count(temp_fd), 0, "failed descriptor close was retried")
        others = snapshot - {temp_fd}
        self.assertEqual(len(others), 4)
        for fd in others:
            self.assertEqual(post.count(fd), 1, f"descriptor {fd} not released exactly once")

    def test_unexpected_close_defect_still_unlinks_and_continues(self) -> None:
        defect = ValueError("injected close defect")
        policy = lambda fd, path, attempt: defect if path and path.startswith(".publish-") else None

        with self._publication_instruments(close_policy=policy) as (faults, paths, unlinks):
            with self.assertRaises(ValueError) as caught:
                self.publish()
            temp_fd = self._temporary_fd(paths)
            snapshot = faults.fault_snapshot[temp_fd]
            post = faults.post_fault(temp_fd)
            unlink_count = sum(1 for name in unlinks if name.startswith(".publish-"))

        self.assertIs(caught.exception, defect)
        self.assertEqual(unlink_count, 1)
        self.assertEqual(post.count(temp_fd), 0)
        for fd in snapshot - {temp_fd}:
            self.assertEqual(post.count(fd), 1)

    def test_unlink_permission_failure_remains_observable(self) -> None:
        denial = PermissionError(errno.EACCES, "temporary unlink denied")

        with self._publication_instruments(
            unlink_policy=lambda name: denial if name.startswith(".publish-") else None
        ):
            with self.assertRaises(build_cache.BuildCacheError) as caught:
                self.publish()

        self.assertIn(denial, _secondary_of(caught.exception))

    def test_unlink_io_failure_remains_observable(self) -> None:
        io_error = OSError(errno.EIO, "temporary unlink I/O failure")

        with self._publication_instruments(
            unlink_policy=lambda name: io_error if name.startswith(".publish-") else None
        ):
            with self.assertRaises(build_cache.BuildCacheError) as caught:
                self.publish()

        self.assertIn(io_error, _secondary_of(caught.exception))

    def test_declared_temporary_absence_is_harmless(self) -> None:
        absent = FileNotFoundError(errno.ENOENT, "temporary already gone")

        with self._publication_instruments(
            unlink_policy=lambda name: absent if name.startswith(".publish-") else None
        ):
            with self.assertRaises(build_cache.BuildCacheError) as caught:
                self.publish()

        self.assertNotIn(absent, _secondary_of(caught.exception))
        self.assertEqual(_secondary_of(caught.exception), [])

    def test_temporary_name_collision_leaves_pre_existing_entry_untouched(self) -> None:
        # Force the generated temporary name to match a pre-existing entry so
        # the exclusive create (O_EXCL) collides.  This publication never owns
        # that entry, so its cleanup must neither unlink nor modify it.
        temp_name = ".publish-" + bytes(16).hex()
        existing = self.paths.tmp_root / temp_name
        original = b"pre-existing entry contents"
        existing.write_bytes(original)
        os.chmod(existing, 0o644)
        before = existing.stat()

        with mock.patch.object(build_cache.os, "urandom", lambda n: bytes(n)):
            with self._publication_instruments() as (faults, paths, unlinks):
                with self.assertRaises(FileExistsError):
                    self.publish(b"payload")
                # Every descriptor opened for the failed publication is gone.
                self.assertEqual(faults.open_fds, set())
                collided = [fd for fd, name in paths.items() if name == temp_name]
                self.assertEqual(collided, [], "O_EXCL collision unexpectedly opened temp_name")

        self.assertNotIn(temp_name, unlinks)
        self.assertEqual(existing.read_bytes(), original)
        after = existing.stat()
        self.assertEqual(after.st_ino, before.st_ino)
        self.assertEqual(after.st_size, before.st_size)


class StateOpenCleanupTests(_BuildCacheCase):
    """``open_build_cache_state`` release paths."""

    def test_namespace_close_failure_releases_every_retained_child(self) -> None:
        namespace_close = OSError(errno.EIO, "namespace close failed")
        seen: dict[str, list[int]] = {"namespace": []}

        def policy(fd, path, attempt):
            if seen["namespace"] and fd == seen["namespace"][0]:
                return namespace_close
            return None

        with self._state_instruments(close_policy=policy) as (faults, ns_fds, child_fds):
            seen["namespace"] = ns_fds
            with self.assertRaises(OSError) as caught:
                build_cache.open_build_cache_state(self.project, cache_root=self.cache)
            opened_children = list(child_fds)
            snapshot = faults.fault_snapshot[ns_fds[0]]
            post = faults.post_fault(ns_fds[0])

        self.assertIs(caught.exception, namespace_close)
        self.assertEqual(len(opened_children), 5)
        self.assertEqual(set(opened_children) | {ns_fds[0]}, snapshot)
        for fd in opened_children:
            self.assertEqual(post.count(fd), 1, f"retained child {fd} not released exactly once")
        self.assertEqual(post.count(ns_fds[0]), 0, "failed namespace close was retried")

    def test_namespace_close_interruption_releases_every_child(self) -> None:
        interruption = KeyboardInterrupt()
        seen: dict[str, list[int]] = {"namespace": []}

        def policy(fd, path, attempt):
            if seen["namespace"] and fd == seen["namespace"][0]:
                return interruption
            return None

        with self._state_instruments(close_policy=policy) as (faults, ns_fds, child_fds):
            seen["namespace"] = ns_fds
            with self.assertRaises(KeyboardInterrupt) as caught:
                build_cache.open_build_cache_state(self.project, cache_root=self.cache)
            opened_children = list(child_fds)
            post = faults.post_fault(ns_fds[0])

        self.assertIs(caught.exception, interruption)
        self.assertEqual(len(opened_children), 5)
        for fd in opened_children:
            self.assertEqual(post.count(fd), 1, f"retained child {fd} not released exactly once")

    def test_child_release_interruption_authoritative_over_namespace_failure(self) -> None:
        namespace_close = OSError(errno.EIO, "namespace close failed")
        interruption = KeyboardInterrupt()
        seen: dict[str, list[int]] = {"child": [], "namespace": []}

        def policy(fd, path, attempt):
            if seen["child"] and fd == seen["child"][0]:
                return interruption
            if seen["namespace"] and fd == seen["namespace"][0]:
                return namespace_close
            return None

        with self._state_instruments(
            close_policy=policy, fail_child_open_at=1
        ) as (faults, ns_fds, child_fds):
            seen["namespace"] = ns_fds
            seen["child"] = child_fds
            with self.assertRaises(KeyboardInterrupt) as caught:
                build_cache.open_build_cache_state(self.project, cache_root=self.cache)
            opened_children = list(child_fds)
            post = faults.post_fault(interruption_fd := opened_children[0])
            namespace_count = post.count(ns_fds[0])

        self.assertIs(caught.exception, interruption)
        self.assertEqual(len(opened_children), 1)
        self.assertEqual(post.count(interruption_fd), 0, "failed child close was retried")
        self.assertEqual(namespace_count, 1, "namespace descriptor was not released once")
        secondary = _secondary_of(caught.exception)
        self.assertIn(namespace_close, secondary)
        self.assertTrue(any(isinstance(item, build_cache.BuildCacheError) for item in secondary))

    def test_child_interruption_after_namespace_failure_is_authoritative(self) -> None:
        namespace_close = OSError(errno.EIO, "namespace close failed")
        interruption = KeyboardInterrupt()
        seen: dict[str, list[int]] = {"child": [], "namespace": []}

        def policy(fd, path, attempt):
            if seen["namespace"] and fd == seen["namespace"][0]:
                return namespace_close
            if seen["child"] and fd == seen["child"][-1]:
                return interruption
            return None

        with self._state_instruments(close_policy=policy) as (faults, ns_fds, child_fds):
            seen["namespace"] = ns_fds
            seen["child"] = child_fds
            with self.assertRaises(KeyboardInterrupt) as caught:
                build_cache.open_build_cache_state(self.project, cache_root=self.cache)
            opened_children = list(child_fds)
            interrupted_child = opened_children[-1]
            post = faults.post_fault(interrupted_child)
            child_counts = [post.count(fd) for fd in opened_children]
            namespace_count = faults.attempts.get(ns_fds[0], 0)

        self.assertIs(caught.exception, interruption)
        self.assertEqual(len(opened_children), 5)
        # The interrupted child is the last opened (released first); every
        # other child is still released exactly once after it.
        self.assertEqual(child_counts, [1, 1, 1, 1, 0])
        self.assertEqual(namespace_count, 1)
        self.assertIn(namespace_close, _secondary_of(caught.exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
