"""Phase 9A (9A.9 hardening) — descriptor-handoff ownership.

``build_cache._open_relative_dir`` and ``artifact_cache._open_directory_chain``
walk a directory chain one descriptor at a time.  The child descriptor must
become the owned descriptor *before* the parent close is attempted, otherwise a
failing parent close is retried by the exception handler and the freshly opened
child leaks.

These tests record the descriptors opened by the walker and inject close faults
by call order, then assert that:

* the parent descriptor is attempted exactly once (a failed close is never
  retried);
* the descriptor released after a failed parent close is the newly opened
  child, not the parent again;
* ordinary, unexpected, and process-control failures follow the accumulator's
  precedence contract (primary authoritative, cleanup failures secondary).
"""
from __future__ import annotations

import contextlib
import errno
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import docker.versioning.artifact_cache as artifact_cache
import docker.versioning.build_cache as build_cache


def _secondary_of(exc: BaseException) -> list[BaseException]:
    return getattr(exc, "_transaction_secondary", [])


class _Instrumentation:
    """Record descriptor opens and inject close faults by call order.

    ``faults`` maps the zero-based index of a close call to the exception that
    call must raise instead of closing.  Descriptors whose real close was
    skipped by a fault are released by :meth:`cleanup`; all other descriptor
    numbers may have been legitimately reused, so they are never re-closed.
    """

    def __init__(self, faults=None):
        self.real_close = os.close
        self.real_open = os.open
        self.real_dup = os.dup
        self._faults = dict(faults or {})
        self.opened: list[int] = []
        self.calls: list[int] = []
        self.attempts: dict[int, int] = {}
        self._leaked: list[int] = []

    def close(self, fd: int) -> None:
        index = len(self.calls)
        self.calls.append(fd)
        self.attempts[fd] = self.attempts.get(fd, 0) + 1
        fault = self._faults.pop(index, None)
        if fault is not None:
            self._leaked.append(fd)
            raise fault
        self.real_close(fd)

    def open(self, path, flags, *args, **kwargs):
        fd = self.real_open(path, flags, *args, **kwargs)
        if kwargs.get("dir_fd") is not None or os.fspath(path) == os.sep:
            self.opened.append(fd)
        return fd

    def dup(self, fd: int) -> int:
        new_fd = self.real_dup(fd)
        self.opened.append(new_fd)
        return new_fd

    def cleanup(self) -> None:
        for fd in dict.fromkeys(self._leaked):
            try:
                self.real_close(fd)
            except OSError:
                pass


@contextlib.contextmanager
def _instrumented(faults=None, **patch_kwargs):
    instrumentation = _Instrumentation(faults)
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(os, "close", instrumentation.close))
        stack.enter_context(patch.object(os, "open", instrumentation.open))
        stack.enter_context(patch.object(os, "dup", instrumentation.dup))
        for attribute, kwargs in patch_kwargs.items():
            stack.enter_context(patch.object(os, attribute, **kwargs))
        try:
            yield instrumentation
        finally:
            instrumentation.cleanup()


class DescriptorHandoffAssertions:
    def assert_parent_then_child(self, instr) -> None:
        """The failed parent close is followed by a single child release."""
        self.assertGreaterEqual(len(instr.opened), 2, instr.opened)
        parent_fd, child_fd = instr.opened[0], instr.opened[1]
        self.assertNotEqual(parent_fd, child_fd)
        self.assertEqual(instr.calls, [parent_fd, child_fd])

    def assert_releases_match_opens(self, instr) -> None:
        """Every opened descriptor is released exactly once, in order.

        ``attempts`` is not consulted because descriptor numbers are reused
        across the walk; the ordered open/close correspondence is the
        invariant that proves there is no leaked or double close.
        """
        self.assertEqual(instr.calls, instr.opened)


class BuildCacheRelativeDirTests(DescriptorHandoffAssertions, unittest.TestCase):
    """``build_cache._open_relative_dir`` descriptor handoff."""

    def test_normal_walk_returns_final_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ns_fd = os.open(td, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with _instrumented() as instr:
                    fd = build_cache._open_relative_dir(ns_fd, ("a", "b"), create=True)
                try:
                    self.assertTrue(stat.S_ISDIR(os.fstat(fd).st_mode))
                    # Intermediates are released; the final descriptor is owned.
                    self.assertEqual(instr.calls, instr.opened[:-1])
                    self.assertEqual(fd, instr.opened[-1])
                finally:
                    os.close(fd)
            finally:
                os.close(ns_fd)

    def test_ordinary_parent_close_failure_releases_child_without_retrying_parent(self) -> None:
        fault = OSError(errno.EIO, "parent close failed")
        with tempfile.TemporaryDirectory() as td:
            ns_fd = os.open(td, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with _instrumented({0: fault}) as instr:
                    with self.assertRaises(OSError) as ctx:
                        build_cache._open_relative_dir(ns_fd, ("a", "b"), create=True)
                self.assertIs(ctx.exception, fault)
                self.assert_parent_then_child(instr)
            finally:
                os.close(ns_fd)

    def test_child_close_failure_is_secondary_to_parent_close_failure(self) -> None:
        parent_fault = OSError(errno.EIO, "parent close failed")
        child_fault = OSError(errno.EIO, "child close failed")
        with tempfile.TemporaryDirectory() as td:
            ns_fd = os.open(td, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with _instrumented({0: parent_fault, 1: child_fault}) as instr:
                    with self.assertRaises(OSError) as ctx:
                        build_cache._open_relative_dir(ns_fd, ("a", "b"), create=True)
                self.assertIs(ctx.exception, parent_fault)
                self.assert_parent_then_child(instr)
                secondary = _secondary_of(ctx.exception)
                self.assertTrue(
                    any("child close failed" in str(exc) for exc in secondary),
                    secondary,
                )
            finally:
                os.close(ns_fd)

    def test_unexpected_fchmod_failure_releases_child_and_parent_once(self) -> None:
        fault = RuntimeError("fchmod defect")
        with tempfile.TemporaryDirectory() as td:
            ns_fd = os.open(td, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with _instrumented(None, fchmod={"side_effect": fault}) as instr:
                    with self.assertRaises(RuntimeError) as ctx:
                        build_cache._open_relative_dir(ns_fd, ("a",), create=True)
                self.assertIs(ctx.exception, fault)
                # The child was opened before fchmod ran, so both owned
                # descriptors must be released exactly once, child first.
                self.assertGreaterEqual(len(instr.opened), 2)
                parent_fd, child_fd = instr.opened[0], instr.opened[1]
                self.assertEqual(instr.calls, [child_fd, parent_fd])
            finally:
                os.close(ns_fd)

    def test_fchmod_interruption_releases_child_and_parent_and_stays_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ns_fd = os.open(td, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with _instrumented(None, fchmod={"side_effect": KeyboardInterrupt()}) as instr:
                    with self.assertRaises(KeyboardInterrupt):
                        build_cache._open_relative_dir(ns_fd, ("a",), create=True)
                parent_fd, child_fd = instr.opened[0], instr.opened[1]
                self.assertEqual(instr.calls, [child_fd, parent_fd])
            finally:
                os.close(ns_fd)

    def test_parent_close_interruption_releases_child_without_retrying_parent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ns_fd = os.open(td, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with _instrumented({0: KeyboardInterrupt()}) as instr:
                    with self.assertRaises(KeyboardInterrupt):
                        build_cache._open_relative_dir(ns_fd, ("a", "b"), create=True)
                self.assert_parent_then_child(instr)
            finally:
                os.close(ns_fd)

    def test_missing_component_returns_none_and_closes_owned_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ns_fd = os.open(td, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with _instrumented() as instr:
                    result = build_cache._open_relative_dir(
                        ns_fd, ("missing",), create=False
                    )
                self.assertIsNone(result)
                self.assertEqual(instr.calls, instr.opened)
                self.assertEqual(set(instr.attempts.values()), {1})
            finally:
                os.close(ns_fd)

    def test_missing_component_close_failure_is_not_retried(self) -> None:
        fault = OSError(errno.EIO, "missing-path close failed")
        with tempfile.TemporaryDirectory() as td:
            ns_fd = os.open(td, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with _instrumented({0: fault}) as instr:
                    with self.assertRaises(OSError) as ctx:
                        build_cache._open_relative_dir(ns_fd, ("missing",), create=False)
                self.assertIs(ctx.exception, fault)
                # The retained descriptor is closed once and never retried.
                self.assertEqual(instr.calls, instr.opened)
            finally:
                os.close(ns_fd)


class ArtifactCacheDirectoryChainTests(DescriptorHandoffAssertions, unittest.TestCase):
    """``artifact_cache._open_directory_chain`` descriptor handoff."""

    def test_normal_walk_returns_final_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "a" / "b"
            with _instrumented() as instr:
                fd = artifact_cache._open_directory_chain(str(target), create=True)
            try:
                self.assertTrue(stat.S_ISDIR(os.fstat(fd).st_mode))
                self.assertEqual(instr.calls, instr.opened[:-1])
                self.assertEqual(fd, instr.opened[-1])
            finally:
                os.close(fd)

    def test_ordinary_parent_close_failure_releases_child_without_retrying_parent(self) -> None:
        fault = OSError(errno.EIO, "parent close failed")
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "a" / "b"
            with _instrumented({0: fault}) as instr:
                with self.assertRaises(OSError) as ctx:
                    artifact_cache._open_directory_chain(str(target), create=True)
            self.assertIs(ctx.exception, fault)
            self.assert_parent_then_child(instr)

    def test_child_close_failure_is_secondary_to_parent_close_failure(self) -> None:
        parent_fault = OSError(errno.EIO, "parent close failed")
        child_fault = OSError(errno.EIO, "child close failed")
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "a" / "b"
            with _instrumented({0: parent_fault, 1: child_fault}) as instr:
                with self.assertRaises(OSError) as ctx:
                    artifact_cache._open_directory_chain(str(target), create=True)
            self.assertIs(ctx.exception, parent_fault)
            self.assert_parent_then_child(instr)
            secondary = _secondary_of(ctx.exception)
            self.assertTrue(
                any("child close failed" in str(exc) for exc in secondary),
                secondary,
            )

    def test_unexpected_parent_close_defect_releases_child(self) -> None:
        fault = RuntimeError("unexpected close defect")
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "a" / "b"
            with _instrumented({0: fault}) as instr:
                with self.assertRaises(RuntimeError) as ctx:
                    artifact_cache._open_directory_chain(str(target), create=True)
            self.assertIs(ctx.exception, fault)
            self.assert_parent_then_child(instr)

    def test_parent_close_interruption_releases_child_and_stays_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "a" / "b"
            with _instrumented({0: KeyboardInterrupt()}) as instr:
                with self.assertRaises(KeyboardInterrupt):
                    artifact_cache._open_directory_chain(str(target), create=True)
            self.assert_parent_then_child(instr)

    def test_mkdir_unexpected_defect_releases_owned_descriptor(self) -> None:
        fault = RuntimeError("mkdir defect")
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "a" / "b"
            with _instrumented(None, mkdir={"side_effect": fault}) as instr:
                with self.assertRaises(RuntimeError) as ctx:
                    artifact_cache._open_directory_chain(str(target), create=True)
            self.assertIs(ctx.exception, fault)
            # Every descriptor opened up to the failure is released once.
            self.assert_releases_match_opens(instr)

    def test_missing_component_without_create_releases_owned_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "a" / "b"
            with _instrumented() as instr:
                with self.assertRaises(FileNotFoundError):
                    artifact_cache._open_directory_chain(str(target), create=False)
            self.assert_releases_match_opens(instr)


if __name__ == "__main__":
    unittest.main()
