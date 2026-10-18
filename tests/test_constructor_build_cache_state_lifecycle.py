"""Phase 4 — ``BuildCacheState`` aggregate descriptor owner lifecycle.

``open_build_cache_state()`` returns the authoritative ``BuildCacheState``
owning five independent descriptor fields: ``persistent_fd``, ``blobs_fd``,
``tmp_fd``, ``markers_fd``, and ``transactions_fd``.  These tests pin the
Phase 4 contract:

* every retained descriptor receives exactly one terminal release attempt,
  even when an earlier release fails;
* repeated cleanup issues no operation after any success or failure outcome;
* the shared cleanup precedence selects the authoritative failure without
  wrapping a process-control interruption or an unexpected defect; and
* the public constructor, dataclass fields, integer field values, and the
  ``BuildCacheState`` instance returned by ``open_build_cache_state()`` are
  unchanged.

The close order under test is the historical release order
``transactions_fd, markers_fd, tmp_fd, blobs_fd, persistent_fd``.
"""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import gc
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import docker.versioning.build_cache as build_cache


#: Historical release order (reverse of the open order).
_RELEASE_ORDER = (
    "transactions_fd",
    "markers_fd",
    "tmp_fd",
    "blobs_fd",
    "persistent_fd",
)


def _secondary_of(exc: BaseException) -> list[BaseException]:
    return list(getattr(exc, "_transaction_secondary", []))


class _BuildCacheStateCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.project = base / "project"
        self.project.mkdir()
        self.cache = base / "cache"
        self.cache.mkdir(mode=0o700)
        self.state = build_cache.open_build_cache_state(
            self.project,
            cache_root=self.cache,
        )
        self.fds = tuple(getattr(self.state, name) for name in _RELEASE_ORDER)
        self._faulted: list[int] = []
        self.addCleanup(self._release_leftovers)

    def _release_leftovers(self) -> None:
        for fd in dict.fromkeys(self.fds):
            try:
                os.close(fd)
            except OSError:
                pass

    @contextlib.contextmanager
    def _close_instruments(self, policy):
        real_close = os.close
        events: list[int] = []
        attempts: dict[int, int] = {}

        def fake_close(fd):
            attempts[fd] = attempts.get(fd, 0) + 1
            events.append(fd)
            fault = policy(fd, attempts[fd])
            if fault is not None:
                self._faulted.append(fd)
                raise fault
            real_close(fd)

        try:
            with mock.patch.object(build_cache.os, "close", fake_close):
                yield events, attempts
        finally:
            for fd in dict.fromkeys(self._faulted):
                try:
                    real_close(fd)
                except OSError:
                    pass

    def _assert_every_descriptor_once(self, events, attempts) -> None:
        self.assertEqual(set(events), set(self.fds))
        for fd in self.fds:
            self.assertEqual(attempts[fd], 1, f"fd {fd} was not attempted exactly once")


class BuildCacheStateReleaseTests(_BuildCacheStateCase):
    """Tasks 4.1–4.3 — exhaustive at-most-once aggregate release."""

    def test_single_ordinary_failure_at_each_position_closes_every_descriptor(
        self,
    ) -> None:
        # Release the state opened by setUp() before the loop overwrites
        # self.state; otherwise its five descriptors leak.
        self.state.close()
        for position in range(len(_RELEASE_ORDER)):
            with self.subTest(position=position):
                self.state = build_cache.open_build_cache_state(
                    self.project,
                    cache_root=self.cache,
                )
                self.fds = tuple(getattr(self.state, name) for name in _RELEASE_ORDER)
                faulted_fd = self.fds[position]
                failure = OSError(errno.EIO, f"close failed at {position}")

                def policy(fd, attempt, faulted_fd=faulted_fd, failure=failure):
                    return failure if fd == faulted_fd else None

                with self._close_instruments(policy) as (events, attempts):
                    with self.assertRaises(OSError) as caught:
                        self.state.close()

                self.assertIs(caught.exception, failure)
                self._assert_every_descriptor_once(events, attempts)

    def test_multiple_ordinary_failures_attempt_every_and_order_secondary(self) -> None:
        failures = {
            self.fds[0]: OSError(errno.EIO, "first ordered failure"),
            self.fds[2]: OSError(errno.EIO, "second ordered failure"),
            self.fds[4]: OSError(errno.EIO, "third ordered failure"),
        }

        def policy(fd, attempt):
            return failures.get(fd)

        with self._close_instruments(policy) as (events, attempts):
            with self.assertRaises(OSError) as caught:
                self.state.close()

        self.assertIs(caught.exception, failures[self.fds[0]])
        self._assert_every_descriptor_once(events, attempts)
        self.assertEqual(
            _secondary_of(caught.exception),
            [failures[self.fds[2]], failures[self.fds[4]]],
        )

    def test_no_repeated_operation_after_multiple_ordinary_failures(self) -> None:
        failures = {self.fds[1]: OSError(errno.EIO, "middle failure")}

        with self._close_instruments(lambda fd, attempt: failures.get(fd)) as (
            events,
            attempts,
        ):
            with self.assertRaises(OSError):
                self.state.close()
            before = len(events)
            self.state.close()
            self.assertEqual(len(events), before)

    def test_unexpected_defect_at_each_position_attempts_every_release(self) -> None:
        # Release the state opened by setUp() before the loop overwrites
        # self.state; otherwise its five descriptors leak.
        self.state.close()
        for position in range(len(_RELEASE_ORDER)):
            with self.subTest(position=position):
                self.state = build_cache.open_build_cache_state(
                    self.project,
                    cache_root=self.cache,
                )
                self.fds = tuple(getattr(self.state, name) for name in _RELEASE_ORDER)
                faulted_fd = self.fds[position]
                defect = RuntimeError(f"unexpected close defect at {position}")

                def policy(fd, attempt, faulted_fd=faulted_fd, defect=defect):
                    return defect if fd == faulted_fd else None

                with self._close_instruments(policy) as (events, attempts):
                    with self.assertRaises(RuntimeError) as caught:
                        self.state.close()

                self.assertIs(caught.exception, defect)
                self._assert_every_descriptor_once(events, attempts)

    def test_interruption_at_each_position_attempts_every_release(self) -> None:
        # Release the state opened by setUp() before the loop overwrites
        # self.state; otherwise its five descriptors leak.
        self.state.close()
        for position in range(len(_RELEASE_ORDER)):
            with self.subTest(position=position):
                self.state = build_cache.open_build_cache_state(
                    self.project,
                    cache_root=self.cache,
                )
                self.fds = tuple(getattr(self.state, name) for name in _RELEASE_ORDER)
                faulted_fd = self.fds[position]
                interruption = KeyboardInterrupt()

                def policy(
                    fd, attempt, faulted_fd=faulted_fd, interruption=interruption
                ):
                    return interruption if fd == faulted_fd else None

                with self._close_instruments(policy) as (events, attempts):
                    with self.assertRaises(KeyboardInterrupt) as caught:
                        self.state.close()

                self.assertIs(caught.exception, interruption)
                self._assert_every_descriptor_once(events, attempts)


class BuildCacheStateTerminalTests(_BuildCacheStateCase):
    """Task 4.4 — aggregate terminal state and repeat cleanup."""

    def test_success_close_is_terminal_and_repeat_is_noop(self) -> None:
        detached_at_first_syscall: list[bool] = []

        def policy(fd, attempt):
            if not detached_at_first_syscall:
                detached_at_first_syscall.append(self.state._owners == ())
            return None

        with self._close_instruments(policy) as (events, attempts):
            self.state.close()
            first = len(events)
            self.state.close()
            self.state.close()

        self._assert_every_descriptor_once(events, attempts)
        self.assertEqual(first, 5)
        self.assertEqual(len(events), 5)
        self.assertEqual(detached_at_first_syscall, [True])

    def test_failure_close_is_terminal_and_repeat_is_noop(self) -> None:
        failure = OSError(errno.EIO, "first release failure")

        def policy(fd, attempt):
            return failure if fd == self.fds[0] else None

        with self._close_instruments(policy) as (events, attempts):
            with self.assertRaises(OSError) as caught:
                self.state.close()
            before = len(events)
            self.state.close()
            self.state.close()
            self.assertEqual(len(events), before)

        self.assertIs(caught.exception, failure)
        self._assert_every_descriptor_once(events, attempts)

    def test_owner_set_terminal_before_first_close_syscall(self) -> None:
        reentrant_events: list[int] = []

        def policy(fd, attempt):
            if not reentrant_events:
                reentrant_events.append(fd)
                # The complete owner set is already detached/terminal, so a
                # re-entrant cleanup issues no additional syscall.
                self.assertEqual(self.state._owners, ())
                self.state.close()
            return None

        with self._close_instruments(policy) as (events, attempts):
            self.state.close()

        self._assert_every_descriptor_once(events, attempts)

    def test_context_cleanup_preserves_active_primary(self) -> None:
        primary = RuntimeError("active operation failure")
        failure = OSError(errno.EIO, "release failure under primary")

        def policy(fd, attempt):
            return failure if fd == self.fds[0] else None

        with self._close_instruments(policy) as (events, attempts):
            failures = build_cache.CleanupFailures(primary)
            failures.run(self.state.close, ordinary=(OSError,))
            result = failures.complete()
            self.assertIsNone(result)
            self.state.close()

        self._assert_every_descriptor_once(events, attempts)
        secondary = _secondary_of(primary)
        self.assertIn(failure, secondary)


class BuildCacheStateCompatibilityTests(_BuildCacheStateCase):
    """Task 4.5 — construction and public compatibility."""

    def test_dataclass_fields_and_order_unchanged(self) -> None:
        fields = [
            field.name for field in dataclasses.fields(build_cache.BuildCacheState)
        ]
        self.assertEqual(
            fields,
            [
                "paths",
                "persistent_fd",
                "blobs_fd",
                "tmp_fd",
                "markers_fd",
                "transactions_fd",
            ],
        )

    def test_open_build_cache_state_returns_build_cache_state(self) -> None:
        self.assertIsInstance(self.state, build_cache.BuildCacheState)

    def test_integer_field_values_match_private_owners(self) -> None:
        owners = self.state._owners
        self.assertEqual(len(owners), 5)
        owner_fds = tuple(owner.fd for owner in owners)
        self.assertEqual(set(owner_fds), set(self.fds))
        for name, owner in zip(_RELEASE_ORDER, owners):
            self.assertEqual(getattr(self.state, name), owner.fd)

    def test_private_owners_are_shared_owned_descriptors(self) -> None:
        from docker.filesystem.descriptors import OwnedDescriptor

        for owner in self.state._owners:
            self.assertIsInstance(owner, OwnedDescriptor)
            self.assertFalse(owner.released)


class BuildCacheStateDescriptorLeakTests(unittest.TestCase):
    """Task 4.8 regression — the release suite must not leak descriptors.

    ``BuildCacheStateReleaseTests`` overwrites the state opened by ``setUp``
    in its per-position loops.  If any of those states is replaced without
    being closed, the suite leaks five descriptors per position-loop test.
    This runs the release suite in-process and compares the process's open
    descriptor count before and after (Linux ``/proc/self/fd`` only).
    """

    _FD_DIR = "/proc/self/fd"

    def _descriptor_snapshot(self) -> set[str]:
        gc.collect()
        return set(os.listdir(self._FD_DIR))

    def test_release_suite_leaves_descriptor_count_unchanged(self) -> None:
        if not os.path.isdir(self._FD_DIR):
            self.skipTest("/proc/self/fd is unavailable (non-Linux)")

        before = self._descriptor_snapshot()
        result = unittest.TestResult()
        unittest.TestLoader().loadTestsFromTestCase(BuildCacheStateReleaseTests).run(
            result,
        )
        self.assertEqual(list(result.errors), [], "release suite raised errors")
        self.assertEqual(list(result.failures), [], "release suite had failures")
        after = self._descriptor_snapshot()
        self.assertEqual(
            len(after),
            len(before),
            "descriptor growth across BuildCacheStateReleaseTests: "
            f"{len(before)} -> {len(after)}",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
