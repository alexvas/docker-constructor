"""Phase 3 tasks 3.1–3.7 — the directory descriptor capability.

``DirectoryDescriptor`` grants secure absolute-path walking, single-basename
child operations, and validated adoption on top of ``OwnedDescriptor``.  These
contracts pin construction authority, the no-follow walk, parent/child handoff
and failure precedence, basename validation, and the primitive child
operations.
"""
from __future__ import annotations

import ast
import errno
import inspect
import os
import stat
import unittest
from pathlib import Path
from unittest import mock

from docker.filesystem.descriptors import (
    DescriptorError,
    DirectoryDescriptor,
    OwnedDescriptor,
    UnsafeDescriptorError,
)

_REPO = Path(__file__).resolve().parents[1]
_FOUNDATION = _REPO / "docker" / "filesystem"
_POK = inspect.Parameter.POSITIONAL_OR_KEYWORD
_KO = inspect.Parameter.KEYWORD_ONLY
_EMPTY = inspect.Parameter.empty

_CHILD_METHODS = (
    "open_secure_path",
    "adopt",
    "child_basename",
    "open_directory",
    "create_directory",
    "open_or_create_directory",
    "stat_child",
    "list_names",
    "unlink_child",
    "remove_child_directory",
)


def _dir_stat(uid: int | None = None) -> os.stat_result:
    owner = os.geteuid() if uid is None else uid
    return os.stat_result((stat.S_IFDIR | 0o700, 0, 0, 1, owner, 0, 0, 0, 0, 0))


def _file_stat(uid: int | None = None) -> os.stat_result:
    owner = os.geteuid() if uid is None else uid
    return os.stat_result((stat.S_IFREG | 0o600, 0, 0, 1, owner, 0, 0, 0, 0, 0))


def _params(func: object) -> list[tuple[str, object, object]]:
    return [
        (parameter.name, parameter.kind, parameter.default)
        for parameter in inspect.signature(func).parameters.values()
    ]


def _return_name(func: object) -> str:
    annotation = inspect.signature(func).return_annotation
    if annotation is inspect.Signature.empty:
        return "<empty>"
    if isinstance(annotation, str):
        return annotation
    return getattr(annotation, "__name__", str(annotation))


class _FakeOps:
    """A recording ``DescriptorOps`` double with an fd ledger.

    Every ``openat`` allocates a fresh descriptor, every ``close`` removes it
    from the live set and rejects a second close, so a leaked or
    double-released descriptor is observable directly.
    """

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._next_fd = 100
        self.open_fds: set[int] = set()
        self.closed_fds: list[int] = []
        self._stats: dict[int, os.stat_result] = {}
        self.open_errors: dict[str, BaseException] = {}
        self.fstat_errors: dict[int, BaseException] = {}
        self.fchmod_errors: dict[int, BaseException] = {}
        self.close_errors: dict[int, BaseException] = {}
        self.mkdirat_errors: dict[str, BaseException] = {}
        self.statat_errors: dict[str, BaseException] = {}
        self.unlinkat_errors: dict[str, BaseException] = {}
        self.rmdirat_errors: dict[str, BaseException] = {}
        self.statat_results: dict[str, os.stat_result] = {}
        self.listdir_results: dict[int, list[str]] = {}
        self.fchmods: list[tuple[int, int]] = []

    def _allocate(self) -> int:
        fd = self._next_fd
        self._next_fd += 1
        self._stats[fd] = _dir_stat()
        self.open_fds.add(fd)
        return fd

    def openat(self, directory_fd: int | None, name: str, flags: int, mode: int = 0) -> int:
        self.calls.append(("openat", directory_fd, name, flags))
        # Open errors are one-shot: a missing entry can be created and then
        # opened again by ``open_or_create_directory``.
        error = self.open_errors.pop(name, None)
        if error is not None:
            raise error
        return self._allocate()

    def close(self, fd: int) -> None:
        self.calls.append(("close", fd))
        if fd not in self.open_fds:
            raise AssertionError(f"descriptor {fd} was closed twice or was never open")
        # Record the attempt before propagating a failure so a retry is caught
        # as a double close rather than appearing to succeed.
        self.open_fds.discard(fd)
        self.closed_fds.append(fd)
        error = self.close_errors.get(fd)
        if error is not None:
            raise error

    def fstat(self, fd: int) -> os.stat_result:
        self.calls.append(("fstat", fd))
        error = self.fstat_errors.get(fd)
        if error is not None:
            raise error
        return self._stats[fd]

    def fchmod(self, fd: int, mode: int) -> None:
        self.calls.append(("fchmod", fd, mode))
        error = self.fchmod_errors.get(fd)
        if error is not None:
            raise error
        self.fchmods.append((fd, mode))

    def mkdirat(self, directory_fd: int, name: str, mode: int) -> None:
        self.calls.append(("mkdirat", directory_fd, name, mode))
        error = self.mkdirat_errors.get(name)
        if error is not None:
            raise error

    def statat(self, directory_fd: int, name: str, *, follow_symlinks: bool) -> os.stat_result:
        self.calls.append(("statat", directory_fd, name, follow_symlinks))
        error = self.statat_errors.get(name)
        if error is not None:
            raise error
        return self.statat_results.get(name, _dir_stat())

    def listdir(self, fd: int) -> list[str]:
        self.calls.append(("listdir", fd))
        return list(self.listdir_results.get(fd, []))

    def unlinkat(self, directory_fd: int, name: str) -> None:
        self.calls.append(("unlinkat", directory_fd, name))
        error = self.unlinkat_errors.get(name)
        if error is not None:
            raise error

    def rmdirat(self, directory_fd: int, name: str) -> None:
        self.calls.append(("rmdirat", directory_fd, name))
        error = self.rmdirat_errors.get(name)
        if error is not None:
            raise error


def _adopt_parent(ops: _FakeOps) -> DirectoryDescriptor:
    """Return a live fake parent descriptor and clear the adoption calls."""
    fd = ops.openat(None, "/", os.O_RDONLY)
    parent = DirectoryDescriptor.adopt(ops, fd, label="parent")
    ops.calls.clear()
    return parent


def _openat_calls(ops: _FakeOps) -> list[tuple]:
    return [call for call in ops.calls if call[0] == "openat"]


def _close_calls(ops: _FakeOps) -> list[int]:
    return [call[1] for call in ops.calls if call[0] == "close"]


def _invoke_directory_creation(
    parent: DirectoryDescriptor,
    path: str,
    name: str,
    mode: int,
    *,
    require_owner: bool = True,
) -> DirectoryDescriptor:
    """Exercise one child-creation path so fixtures can cover both."""
    if path == "create":
        return parent.create_directory(
            name, mode=mode, require_owner=require_owner
        )
    if path == "open_or_create":
        return parent.open_or_create_directory(
            name, mode=mode, require_owner=require_owner
        )
    raise AssertionError(f"unknown creation path: {path}")


def _override_child_stat(ops: _FakeOps, stat_result: os.stat_result) -> None:
    """Make every later ``openat`` report *stat_result* for its descriptor."""
    original_open = ops.openat

    def openat(directory_fd, name, flags, mode=0):
        fd = original_open(directory_fd, name, flags, mode)
        ops._stats[fd] = stat_result
        return fd

    ops.openat = openat  # type: ignore[method-assign]


def _raise_from_fchmod(ops: _FakeOps, error: BaseException) -> None:
    """Make ``ops.fchmod`` raise *error* for whatever descriptor it sees."""

    def fchmod(fd: int, mode: int) -> None:
        ops.calls.append(("fchmod", fd, mode))
        raise error

    ops.fchmod = fchmod  # type: ignore[method-assign]


def _raise_from_fchmod_and_fail_close(
    ops: _FakeOps, error: BaseException, close_error: BaseException
) -> None:
    """Make ``ops.fchmod`` raise *error* and the ensuing close fail too."""

    def fchmod(fd: int, mode: int) -> None:
        ops.calls.append(("fchmod", fd, mode))
        ops.close_errors[fd] = close_error
        raise error

    ops.fchmod = fchmod  # type: ignore[method-assign]


class ConstructionAuthorityTests(unittest.TestCase):
    """Task 3.1 — direct construction is rejected before ownership transfer."""

    def test_direct_construction_raises_type_error_without_operations(self) -> None:
        ops = _FakeOps()
        with self.assertRaises(TypeError):
            DirectoryDescriptor(ops, 100, label="direct")
        self.assertEqual(ops.calls, [])
        self.assertEqual(ops.open_fds, set())

    def test_only_open_secure_path_and_adopt_construct(self) -> None:
        for name in _CHILD_METHODS:
            with self.subTest(name=name):
                self.assertTrue(hasattr(DirectoryDescriptor, name))
        for forbidden in ("from_path", "from_fd", "from_secure_path"):
            with self.subTest(forbidden=forbidden):
                self.assertFalse(hasattr(DirectoryDescriptor, forbidden))

    def test_directory_descriptor_is_an_owned_descriptor(self) -> None:
        self.assertTrue(issubclass(DirectoryDescriptor, OwnedDescriptor))


class SecureWalkTests(unittest.TestCase):
    """Task 3.2 — absolute, component-relative, no-follow walking."""

    def test_relative_path_is_rejected_before_any_operation(self) -> None:
        ops = _FakeOps()
        with self.assertRaises(DescriptorError):
            DirectoryDescriptor.open_secure_path(ops, "relative/path")
        self.assertEqual(ops.calls, [])

    def test_walk_opens_each_component_relative_to_its_parent(self) -> None:
        ops = _FakeOps()
        descriptor = DirectoryDescriptor.open_secure_path(ops, "/a/b", label="target")
        self.assertIsInstance(descriptor, DirectoryDescriptor)
        self.assertEqual(descriptor.label, "target")
        opens = _openat_calls(ops)
        self.assertEqual(opens[0][1:3], (None, "/"))
        self.assertEqual(opens[1][1:3], (100, "a"))
        self.assertEqual(opens[2][1:3], (101, "b"))
        # Every intermediate parent is released; only the leaf stays live.
        self.assertEqual(ops.open_fds, {descriptor.fd})
        descriptor.close()
        self.assertEqual(ops.open_fds, set())

    def test_final_component_must_be_a_directory(self) -> None:
        ops = _FakeOps()
        ops.fstat_errors.clear()
        original_open = ops.openat

        def openat(directory_fd, name, flags, mode=0):
            fd = original_open(directory_fd, name, flags, mode)
            if name == "b":
                ops._stats[fd] = _file_stat()
            return fd

        ops.openat = openat  # type: ignore[method-assign]
        with self.assertRaises(UnsafeDescriptorError):
            DirectoryDescriptor.open_secure_path(ops, "/a/b", label="target")
        self.assertEqual(ops.open_fds, set())

    def test_effective_owner_is_required_by_default_and_optional(self) -> None:
        foreign = os.geteuid() + 1
        ops = _FakeOps()
        DirectoryDescriptor.open_secure_path(ops, "/a/b", label="t")
        # Reject a foreign-owned leaf.
        ops2 = _FakeOps()
        original_open = ops2.openat

        def openat(directory_fd, name, flags, mode=0):
            fd = original_open(directory_fd, name, flags, mode)
            if name == "b":
                ops2._stats[fd] = _dir_stat(uid=foreign)
            return fd

        ops2.openat = openat  # type: ignore[method-assign]
        with self.assertRaises(UnsafeDescriptorError):
            DirectoryDescriptor.open_secure_path(ops2, "/a/b", label="t")
        self.assertEqual(ops2.open_fds, set())

        # The same foreign-owned leaf is accepted when ownership is not required.
        ops3 = _FakeOps()
        original_open3 = ops3.openat

        def openat3(directory_fd, name, flags, mode=0):
            fd = original_open3(directory_fd, name, flags, mode)
            if name == "b":
                ops3._stats[fd] = _dir_stat(uid=foreign)
            return fd

        ops3.openat = openat3  # type: ignore[method-assign]
        descriptor = DirectoryDescriptor.open_secure_path(
            ops3, "/a/b", label="t", require_owner=False
        )
        self.assertEqual(descriptor.fd, 102)
        descriptor.close()


class SecureWalkHandoffTests(unittest.TestCase):
    """Task 3.3 — a failed parent close never leaks or retries."""

    def test_failed_parent_close_releases_child_once_and_returns_nothing(self) -> None:
        ops = _FakeOps()
        ops.close_errors[101] = OSError(errno.EIO, "parent close failed")
        with self.assertRaises(OSError):
            DirectoryDescriptor.open_secure_path(ops, "/a/b", label="target")
        # 100 (root) closed during descent, 101 (retained parent) attempted
        # once (and failed), 102 (child) released exactly once.
        self.assertEqual(_close_calls(ops), [100, 101, 102])
        self.assertEqual(ops.open_fds, set())
        self.assertNotIn(102, ops.open_fds)

    def test_child_close_failure_is_secondary_to_the_parent_failure(self) -> None:
        ops = _FakeOps()
        parent_error = OSError(errno.EIO, "parent close failed")
        child_error = OSError(errno.EIO, "child close failed")
        ops.close_errors[101] = parent_error
        ops.close_errors[102] = child_error
        with self.assertRaises(OSError) as ctx:
            DirectoryDescriptor.open_secure_path(ops, "/a/b", label="target")
        self.assertIs(ctx.exception, parent_error)
        self.assertEqual(_close_calls(ops), [100, 101, 102])
        self.assertEqual(ops.open_fds, set())


class SecureWalkFailureTests(unittest.TestCase):
    """Task 3.4 — every still-owned descriptor is released once."""

    def test_component_open_failure_releases_the_retained_parent(self) -> None:
        ops = _FakeOps()
        ops.open_errors["b"] = OSError(errno.ENOTDIR, "not a directory")
        with self.assertRaises(DescriptorError):
            DirectoryDescriptor.open_secure_path(ops, "/a/b", label="target")
        self.assertEqual(_close_calls(ops), [100, 101])
        self.assertEqual(ops.open_fds, set())

    def test_symlinked_component_is_rejected_without_following(self) -> None:
        ops = _FakeOps()
        ops.open_errors["b"] = OSError(errno.ELOOP, "too many levels of symlinks")
        with self.assertRaises(UnsafeDescriptorError):
            DirectoryDescriptor.open_secure_path(ops, "/a/b", label="target")
        self.assertEqual(ops.open_fds, set())

    def test_stat_failure_releases_the_adopted_child_and_parent(self) -> None:
        ops = _FakeOps()
        ops.fstat_errors[102] = OSError(errno.EIO, "stat failed")
        with self.assertRaises(DescriptorError):
            DirectoryDescriptor.open_secure_path(ops, "/a/b", label="target")
        self.assertEqual(_close_calls(ops), [100, 102, 101])
        self.assertEqual(ops.open_fds, set())

    def test_interruption_during_validation_releases_every_descriptor(self) -> None:
        ops = _FakeOps()
        ops.fstat_errors[102] = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            DirectoryDescriptor.open_secure_path(ops, "/a/b", label="target")
        self.assertEqual(ops.open_fds, set())
        self.assertEqual(sorted(ops.closed_fds), [100, 101, 102])

    def test_unexpected_defect_during_validation_releases_every_descriptor(self) -> None:
        ops = _FakeOps()
        ops.fstat_errors[102] = RuntimeError("unexpected defect")
        with self.assertRaises(RuntimeError):
            DirectoryDescriptor.open_secure_path(ops, "/a/b", label="target")
        self.assertEqual(ops.open_fds, set())
        self.assertEqual(sorted(ops.closed_fds), [100, 101, 102])


class ChildBasenameTests(unittest.TestCase):
    """Task 3.5 — one canonical basename only, before any operation."""

    def setUp(self) -> None:
        self.ops = _FakeOps()
        self.parent = _adopt_parent(self.ops)

    def test_valid_basename_is_returned(self) -> None:
        self.assertEqual(self.parent.child_basename("child"), "child")

    def test_unsafe_basenames_are_rejected_without_operation(self) -> None:
        for name in ("", ".", "..", "a/b", "/absolute", "a\x00b"):
            with self.subTest(name=name):
                self.ops.calls.clear()
                with self.assertRaises(DescriptorError):
                    self.parent.child_basename(name)
                self.assertEqual(self.ops.calls, [])

    def test_alternate_separator_is_rejected(self) -> None:
        with mock.patch.object(os, "altsep", "\\"):
            with self.assertRaises(DescriptorError):
                self.parent.child_basename("a\\b")


class ChildDirectoryTests(unittest.TestCase):
    """Task 3.6 — open, exclusive create, and create-or-open."""

    def setUp(self) -> None:
        self.ops = _FakeOps()
        self.parent = _adopt_parent(self.ops)

    def test_open_directory_opens_one_existing_no_follow_child(self) -> None:
        child = self.parent.open_directory("child")
        self.assertIsInstance(child, DirectoryDescriptor)
        self.assertEqual(child.label, "child")
        self.assertIn(("openat", self.parent.fd, "child", mock.ANY), self.ops.calls)
        self.assertNotIn(("fchmod", child.fd, 0o700), self.ops.calls)
        self.assertEqual(self.ops.fchmods, [])
        child.close()

    def test_open_directory_rejects_a_non_directory_child(self) -> None:
        ops = _FakeOps()
        parent = _adopt_parent(ops)
        original_open = ops.openat

        def openat(directory_fd, name, flags, mode=0):
            fd = original_open(directory_fd, name, flags, mode)
            ops._stats[fd] = _file_stat()
            return fd

        ops.openat = openat  # type: ignore[method-assign]
        with self.assertRaises(UnsafeDescriptorError):
            parent.open_directory("child")
        self.assertEqual(ops.open_fds, {parent.fd})
        parent.close()

    def test_create_directory_uses_exclusive_mode_and_post_open_validation(self) -> None:
        child = self.parent.create_directory("fresh", mode=0o700)
        self.assertIsInstance(child, DirectoryDescriptor)
        self.assertIn(("mkdirat", self.parent.fd, "fresh", 0o700), self.ops.calls)
        self.assertIn((child.fd, 0o700), self.ops.fchmods)
        child.close()

    def test_create_directory_reports_a_creation_failure(self) -> None:
        self.ops.mkdirat_errors["fresh"] = OSError(errno.EACCES, "denied")
        with self.assertRaises(DescriptorError):
            self.parent.create_directory("fresh", mode=0o700)
        self.assertEqual(_close_calls(self.ops), [])

    def test_open_or_create_directory_opens_an_existing_child(self) -> None:
        child = self.parent.open_or_create_directory("existing", mode=0o700)
        self.assertIn((child.fd, 0o700), self.ops.fchmods)
        self.assertEqual([call for call in self.ops.calls if call[0] == "mkdirat"], [])
        child.close()

    def test_open_or_create_directory_creates_a_missing_child(self) -> None:
        self.ops.open_errors["missing"] = FileNotFoundError(errno.ENOENT, "missing")
        child = self.parent.open_or_create_directory("missing", mode=0o700)
        self.assertIn(("mkdirat", self.parent.fd, "missing", 0o700), self.ops.calls)
        self.assertIn((child.fd, 0o700), self.ops.fchmods)
        child.close()

    def test_open_or_create_directory_does_not_swallow_other_errors(self) -> None:
        self.ops.open_errors["child"] = OSError(errno.ELOOP, "symlink")
        with self.assertRaises(UnsafeDescriptorError):
            self.parent.open_or_create_directory("child", mode=0o700)
        self.assertEqual([call for call in self.ops.calls if call[0] == "mkdirat"], [])

    def test_open_or_create_directory_rejects_a_foreign_owner(self) -> None:
        ops = _FakeOps()
        parent = _adopt_parent(ops)
        original_open = ops.openat

        def openat(directory_fd, name, flags, mode=0):
            fd = original_open(directory_fd, name, flags, mode)
            ops._stats[fd] = _dir_stat(uid=os.geteuid() + 1)
            return fd

        ops.openat = openat  # type: ignore[method-assign]
        with self.assertRaises(UnsafeDescriptorError):
            parent.open_or_create_directory("child", mode=0o700)
        self.assertEqual(ops.open_fds, {parent.fd})
        parent.close()

    def test_child_operations_reject_unsafe_names_before_operating(self) -> None:
        for call in (
            lambda: self.parent.open_directory("a/b"),
            lambda: self.parent.create_directory("a/b", mode=0o700),
            lambda: self.parent.open_or_create_directory("a/b", mode=0o700),
        ):
            with self.subTest(call=call):
                self.ops.calls.clear()
                with self.assertRaises(DescriptorError):
                    call()
                self.assertEqual(self.ops.calls, [])


class LabelValidationTests(unittest.TestCase):
    """Invalid labels are rejected before any filesystem operation."""

    _INVALID_LABELS = ("", 123)

    def test_secure_path_rejects_invalid_label_before_opening(self) -> None:
        for path in ("/", "/a/b"):
            for label in self._INVALID_LABELS:
                with self.subTest(path=path, label=label):
                    ops = _FakeOps()
                    with self.assertRaises(DescriptorError):
                        DirectoryDescriptor.open_secure_path(
                            ops, path, label=label  # type: ignore[arg-type]
                        )
                    self.assertEqual(ops.calls, [])
                    self.assertEqual(ops.open_fds, set())

    def test_child_factories_reject_invalid_label_before_operation(self) -> None:
        for label in self._INVALID_LABELS:
            factories = (
                (
                    "open_directory",
                    lambda parent, value=label: parent.open_directory(
                        "child", label=value  # type: ignore[arg-type]
                    ),
                ),
                (
                    "create_directory",
                    lambda parent, value=label: parent.create_directory(
                        "child", mode=0o700, label=value  # type: ignore[arg-type]
                    ),
                ),
                (
                    "open_or_create_directory",
                    lambda parent, value=label: parent.open_or_create_directory(
                        "child", mode=0o700, label=value  # type: ignore[arg-type]
                    ),
                ),
            )
            for name, invoke in factories:
                with self.subTest(factory=name, label=label):
                    ops = _FakeOps()
                    parent = _adopt_parent(ops)
                    with self.assertRaises(DescriptorError):
                        invoke(parent)
                    self.assertEqual(ops.calls, [])
                    self.assertEqual(ops.open_fds, {parent.fd})
                    parent.close()

    def test_invalid_label_rejection_by_adopt_leaves_caller_fd_open(self) -> None:
        for label in self._INVALID_LABELS:
            with self.subTest(label=label):
                ops = _FakeOps()
                fd = ops.openat(None, "/", os.O_RDONLY)
                with self.assertRaises(DescriptorError):
                    DirectoryDescriptor.adopt(
                        ops, fd, label=label  # type: ignore[arg-type]
                    )
                self.assertIn(fd, ops.open_fds)
                self.assertEqual(_close_calls(ops), [])
                ops.close(fd)


class MissingChildFallbackTests(unittest.TestCase):
    """Only an ENOENT from the initial open triggers creation."""

    def setUp(self) -> None:
        self.ops = _FakeOps()
        self.parent = _adopt_parent(self.ops)
        self.child_fd = self.parent.fd + 1

    def _assert_no_creation_and_child_released_once(self, cause: OSError) -> None:
        with self.assertRaises(DescriptorError) as ctx:
            self.parent.open_or_create_directory("child", mode=0o700)
        self.assertIs(ctx.exception.cause, cause)
        self.assertEqual(
            [call for call in self.ops.calls if call[0] == "mkdirat"], []
        )
        self.assertEqual(_close_calls(self.ops), [self.child_fd])
        self.assertEqual(self.ops.open_fds, {self.parent.fd})
        self.assertFalse(self.parent.released)
        self.parent.close()

    def test_fstat_enoent_does_not_trigger_creation(self) -> None:
        cause = FileNotFoundError(errno.ENOENT, "missing during stat")
        self.ops.fstat_errors[self.child_fd] = cause
        self._assert_no_creation_and_child_released_once(cause)

    def test_fchmod_enoent_does_not_trigger_creation(self) -> None:
        cause = OSError(errno.ENOENT, "missing during chmod")
        self.ops.fchmod_errors[self.child_fd] = cause
        self._assert_no_creation_and_child_released_once(cause)


class ValidationBeforeSecuringTests(unittest.TestCase):
    """Directory type and ownership are validated before any chmod."""

    def test_create_directory_secures_after_validation(self) -> None:
        ops = _FakeOps()
        parent = _adopt_parent(ops)
        child = parent.create_directory("fresh", mode=0o700)
        names = [call[0] for call in ops.calls]
        self.assertEqual(names, ["mkdirat", "openat", "fstat", "fchmod"])
        self.assertEqual(ops.fchmods, [(child.fd, 0o700)])
        child.close()
        parent.close()

    def test_open_or_create_secures_after_validation(self) -> None:
        ops = _FakeOps()
        parent = _adopt_parent(ops)
        child = parent.open_or_create_directory("existing", mode=0o700)
        names = [call[0] for call in ops.calls]
        self.assertEqual(names, ["openat", "fstat", "fchmod"])
        self.assertEqual(ops.fchmods, [(child.fd, 0o700)])
        child.close()
        parent.close()

    def _assert_rejected_without_chmod(self, stat_result: os.stat_result) -> None:
        for path in ("create", "open_or_create"):
            with self.subTest(path=path):
                ops = _FakeOps()
                parent = _adopt_parent(ops)
                _override_child_stat(ops, stat_result)
                with self.assertRaises(UnsafeDescriptorError):
                    _invoke_directory_creation(parent, path, "child", 0o700)
                self.assertEqual(
                    [call for call in ops.calls if call[0] == "fchmod"], []
                )
                self.assertEqual(ops.fchmods, [])
                self.assertEqual(_close_calls(ops), [parent.fd + 1])
                self.assertEqual(ops.open_fds, {parent.fd})
                parent.close()

    def test_foreign_owned_child_is_rejected_without_chmod(self) -> None:
        self._assert_rejected_without_chmod(_dir_stat(uid=os.geteuid() + 1))

    def test_non_directory_child_is_rejected_without_chmod(self) -> None:
        self._assert_rejected_without_chmod(_file_stat())

    def test_require_owner_false_permits_foreign_ownership(self) -> None:
        for path in ("create", "open_or_create"):
            with self.subTest(path=path):
                ops = _FakeOps()
                parent = _adopt_parent(ops)
                _override_child_stat(ops, _dir_stat(uid=os.geteuid() + 1))
                child = _invoke_directory_creation(
                    parent, path, "child", 0o700, require_owner=False
                )
                self.assertIsInstance(child, DirectoryDescriptor)
                self.assertEqual(ops.fchmods, [(child.fd, 0o700)])
                child.close()
                parent.close()


class ChildSecuringFailureTests(unittest.TestCase):
    """A failing ``fchmod`` releases the opened child under shared precedence."""

    _PATHS = ("create", "open_or_create")

    def _assert_child_released_once(
        self, ops: _FakeOps, parent: DirectoryDescriptor
    ) -> int:
        self.assertEqual(ops.open_fds, {parent.fd})
        self.assertEqual(len(ops.closed_fds), 1)
        child_fd = ops.closed_fds[0]
        self.assertNotEqual(child_fd, parent.fd)
        self.assertEqual(_close_calls(ops), [child_fd])
        return child_fd

    def test_ordinary_secure_failure_translates_and_releases_child(self) -> None:
        for path in self._PATHS:
            with self.subTest(path=path):
                ops = _FakeOps()
                parent = _adopt_parent(ops)
                cause = OSError(errno.EACCES, "denied")
                _raise_from_fchmod(ops, cause)
                with self.assertRaises(DescriptorError) as ctx:
                    _invoke_directory_creation(parent, path, "child", 0o700)
                self.assertIs(ctx.exception.cause, cause)
                self._assert_child_released_once(ops, parent)
                parent.close()

    def test_keyboard_interrupt_while_securing_stays_authoritative(self) -> None:
        for path in self._PATHS:
            with self.subTest(path=path):
                ops = _FakeOps()
                parent = _adopt_parent(ops)
                error = KeyboardInterrupt()
                _raise_from_fchmod(ops, error)
                with self.assertRaises(KeyboardInterrupt) as ctx:
                    _invoke_directory_creation(parent, path, "child", 0o700)
                # ``assertRaises`` proves no child capability was returned.
                self.assertIs(ctx.exception, error)
                self._assert_child_released_once(ops, parent)
                parent.close()

    def test_unexpected_defect_while_securing_stays_authoritative(self) -> None:
        for path in self._PATHS:
            with self.subTest(path=path):
                ops = _FakeOps()
                parent = _adopt_parent(ops)
                error = RuntimeError("unexpected securing defect")
                _raise_from_fchmod(ops, error)
                with self.assertRaises(RuntimeError) as ctx:
                    _invoke_directory_creation(parent, path, "child", 0o700)
                self.assertIs(ctx.exception, error)
                self._assert_child_released_once(ops, parent)
                parent.close()

    def test_secure_failure_with_close_failure_keeps_original_and_diagnostics(
        self,
    ) -> None:
        for path in self._PATHS:
            for error in (KeyboardInterrupt(), RuntimeError("primary defect")):
                with self.subTest(path=path, primary=type(error).__name__):
                    ops = _FakeOps()
                    parent = _adopt_parent(ops)
                    close_error = OSError(errno.EIO, "close failed")
                    _raise_from_fchmod_and_fail_close(ops, error, close_error)
                    with self.assertRaises(BaseException) as ctx:
                        _invoke_directory_creation(parent, path, "child", 0o700)
                    self.assertIs(ctx.exception, error)
                    self._assert_child_released_once(ops, parent)
                    self.assertIn(
                        close_error, getattr(error, "_transaction_secondary", [])
                    )
                    parent.close()


class PrimitiveOperationTests(unittest.TestCase):
    """Task 3.7 — each primitive acts on exactly one validated basename."""

    def setUp(self) -> None:
        self.ops = _FakeOps()
        self.parent = _adopt_parent(self.ops)

    def test_stat_child_uses_the_configured_symlink_policy(self) -> None:
        self.ops.statat_results["child"] = _file_stat()
        result = self.parent.stat_child("child")
        self.assertIs(result, self.ops.statat_results["child"])
        self.assertIn(("statat", self.parent.fd, "child", False), self.ops.calls)
        self.ops.calls.clear()
        self.parent.stat_child("child", follow_symlinks=True)
        self.assertIn(("statat", self.parent.fd, "child", True), self.ops.calls)

    def test_list_names_returns_the_directory_entries(self) -> None:
        self.ops.listdir_results[self.parent.fd] = ["b", "a"]
        self.assertEqual(self.parent.list_names(), ("b", "a"))
        self.assertIn(("listdir", self.parent.fd), self.ops.calls)

    def test_unlink_child_targets_one_basename(self) -> None:
        self.parent.unlink_child("child")
        self.assertIn(("unlinkat", self.parent.fd, "child"), self.ops.calls)

    def test_remove_child_directory_targets_one_basename(self) -> None:
        self.parent.remove_child_directory("child")
        self.assertIn(("rmdirat", self.parent.fd, "child"), self.ops.calls)

    def test_primitive_operations_reject_unsafe_names_before_operating(self) -> None:
        for call in (
            lambda: self.parent.stat_child("a/b"),
            lambda: self.parent.stat_child("."),
            lambda: self.parent.unlink_child("a/b"),
            lambda: self.parent.unlink_child("/abs"),
            lambda: self.parent.remove_child_directory("a\x00b"),
        ):
            with self.subTest(call=call):
                self.ops.calls.clear()
                with self.assertRaises(DescriptorError):
                    call()
                self.assertEqual(self.ops.calls, [])

    def test_primitive_failures_propagate_the_original_oserror(self) -> None:
        error = OSError(errno.EIO, "injected")
        self.ops.unlinkat_errors["child"] = error
        with self.assertRaises(OSError) as ctx:
            self.parent.unlink_child("child")
        self.assertIs(ctx.exception, error)


class DirectoryApiSignatureTests(unittest.TestCase):
    """Task 3.16 — the DirectoryDescriptor API matches the design exactly."""

    def test_construction_seams_are_exact(self) -> None:
        cases = {
            DirectoryDescriptor.open_secure_path: (
                [
                    ("ops", _POK, _EMPTY),
                    ("path", _POK, _EMPTY),
                    ("label", _KO, None),
                    ("require_owner", _KO, True),
                ],
                "DirectoryDescriptor",
            ),
            DirectoryDescriptor.adopt: (
                [
                    ("ops", _POK, _EMPTY),
                    ("fd", _POK, _EMPTY),
                    ("label", _KO, _EMPTY),
                    ("require_owner", _KO, True),
                ],
                "DirectoryDescriptor",
            ),
        }
        for func, (expected_params, expected_return) in cases.items():
            with self.subTest(func=func.__qualname__):
                self.assertEqual(_params(func), expected_params)
                self.assertEqual(_return_name(func), expected_return)

    def test_child_methods_are_exact(self) -> None:
        cases = {
            DirectoryDescriptor.child_basename: (
                [("self", _POK, _EMPTY), ("name", _POK, _EMPTY)],
                "str",
            ),
            DirectoryDescriptor.open_directory: (
                [
                    ("self", _POK, _EMPTY),
                    ("name", _POK, _EMPTY),
                    ("label", _KO, None),
                    ("require_owner", _KO, True),
                ],
                "DirectoryDescriptor",
            ),
            DirectoryDescriptor.create_directory: (
                [
                    ("self", _POK, _EMPTY),
                    ("name", _POK, _EMPTY),
                    ("mode", _KO, _EMPTY),
                    ("label", _KO, None),
                    ("require_owner", _KO, True),
                ],
                "DirectoryDescriptor",
            ),
            DirectoryDescriptor.open_or_create_directory: (
                [
                    ("self", _POK, _EMPTY),
                    ("name", _POK, _EMPTY),
                    ("mode", _KO, _EMPTY),
                    ("label", _KO, None),
                    ("require_owner", _KO, True),
                ],
                "DirectoryDescriptor",
            ),
            DirectoryDescriptor.stat_child: (
                [
                    ("self", _POK, _EMPTY),
                    ("name", _POK, _EMPTY),
                    ("follow_symlinks", _KO, False),
                ],
                "os.stat_result",
            ),
            DirectoryDescriptor.list_names: (
                [("self", _POK, _EMPTY)],
                "tuple[str, ...]",
            ),
            DirectoryDescriptor.unlink_child: (
                [("self", _POK, _EMPTY), ("name", _POK, _EMPTY)],
                "None",
            ),
            DirectoryDescriptor.remove_child_directory: (
                [("self", _POK, _EMPTY), ("name", _POK, _EMPTY)],
                "None",
            ),
        }
        for func, (expected_params, expected_return) in cases.items():
            with self.subTest(func=func.__qualname__):
                self.assertEqual(_params(func), expected_params)
                self.assertEqual(_return_name(func), expected_return)

    def test_no_variadic_parameters(self) -> None:
        for name in _CHILD_METHODS:
            with self.subTest(name=name):
                kinds = {
                    parameter.kind
                    for parameter in inspect.signature(
                        getattr(DirectoryDescriptor, name)
                    ).parameters.values()
                }
                self.assertNotIn(inspect.Parameter.VAR_KEYWORD, kinds)
                self.assertNotIn(inspect.Parameter.VAR_POSITIONAL, kinds)


class FoundationApiSurfaceTests(unittest.TestCase):
    """Task 3.17 — the foundation exposes only the design-specified API."""

    _EXPECTED_PUBLIC = {
        "__init__.py": set(),
        "cleanup.py": {
            "CleanupFailures",
            "attach_secondary",
            "carry_secondary_diagnostics",
        },
        "operations.py": {"DescriptorOps", "PosixDescriptorOps"},
        "descriptors.py": {
            "DescriptorError",
            "UnsafeDescriptorError",
            "OwnedDescriptor",
            "DirectoryDescriptor",
        },
    }
    _FORBIDDEN_DEFINITION_SUBSTRINGS = (
        "remove_tree",
        "rmtree",
        "walk",
        "scandir",
        "retry",
        "fsync",
        "fdatasync",
        "flock",
        "lock",
        "atomic",
        "durability",
        "cache",
        "namespace",
        "npm",
        "versioning",
    )

    def _tree(self, filename: str) -> ast.Module:
        return ast.parse((_FOUNDATION / filename).read_text(encoding="utf-8"))

    def test_public_definition_surface_is_exact(self) -> None:
        for filename, expected in self._EXPECTED_PUBLIC.items():
            names: set[str] = set()
            for node in self._tree(filename).body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if not node.name.startswith("_"):
                        names.add(node.name)
                elif isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and not target.id.startswith("_"):
                            names.add(target.id)
                elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    if not node.target.id.startswith("_"):
                        names.add(node.target.id)
            with self.subTest(module=filename):
                self.assertEqual(names, expected)

    def test_no_forbidden_public_definition_names(self) -> None:
        for filename in self._EXPECTED_PUBLIC:
            for node in ast.walk(self._tree(filename)):
                if not isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                ):
                    continue
                for token in self._FORBIDDEN_DEFINITION_SUBSTRINGS:
                    with self.subTest(module=filename, name=node.name, token=token):
                        self.assertNotIn(token, node.name.lower())

    def test_no_recursive_traversal_durability_or_locking_operations(self) -> None:
        forbidden_attrs = {
            "walk",
            "scandir",
            "fsync",
            "fdatasync",
            "replace",
            "rename",
            "flock",
            "lockf",
        }
        forbidden_modules = {"shutil", "fcntl"}
        for filename in self._EXPECTED_PUBLIC:
            tree = self._tree(filename)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        with self.subTest(module=filename, imported=alias.name):
                            self.assertNotIn(
                                alias.name.split(".")[0], forbidden_modules
                            )
                elif isinstance(node, ast.ImportFrom) and node.module:
                    with self.subTest(module=filename, imported=node.module):
                        self.assertNotIn(
                            node.module.split(".")[0], forbidden_modules
                        )
                elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                    if node.value.id == "os":
                        with self.subTest(module=filename, attribute=node.attr):
                            self.assertNotIn(node.attr, forbidden_attrs)

    def test_package_initializer_exposes_no_aggregate_api(self) -> None:
        self.assertEqual(self._tree("__init__.py").body, [])


class FdLedgerTests(unittest.TestCase):
    """Task 3.18 — every injected descriptor has exactly one final owner."""

    def _assert_balanced(self, ops: _FakeOps) -> None:
        self.assertEqual(
            len(ops.closed_fds),
            len(set(ops.closed_fds)),
            "a descriptor received more than one close attempt",
        )
        allocated = set(ops.closed_fds) | ops.open_fds
        self.assertEqual(
            len(allocated),
            ops._next_fd - 100,
            "the ledger does not account for every allocated descriptor",
        )

    def test_successful_walk_then_detach_keeps_one_live_owner(self) -> None:
        ops = _FakeOps()
        descriptor = DirectoryDescriptor.open_secure_path(ops, "/a/b", label="target")
        self._assert_balanced(ops)
        self.assertEqual(ops.open_fds, {descriptor.fd})
        raw = descriptor.fd
        self.assertEqual(descriptor.detach(), raw)
        before = list(ops.closed_fds)
        descriptor.close()
        self.assertEqual(ops.closed_fds, before)
        self.assertIn(raw, ops.open_fds)
        ops.close(raw)
        self._assert_balanced(ops)
        self.assertEqual(ops.open_fds, set())

    def test_every_failing_walk_closes_each_descriptor_at_most_once(self) -> None:
        def open_error(ops: _FakeOps) -> None:
            ops.open_errors["b"] = OSError(errno.ENOTDIR, "not a directory")

        def stat_error(ops: _FakeOps) -> None:
            ops.fstat_errors[102] = OSError(errno.EIO, "stat failed")

        def parent_close_error(ops: _FakeOps) -> None:
            ops.close_errors[101] = OSError(errno.EIO, "parent close failed")

        def child_close_error(ops: _FakeOps) -> None:
            # A child release is attempted only when the walk fails; combine it
            # with a failed parent close so both independent releases run.
            ops.close_errors[101] = OSError(errno.EIO, "parent close failed")
            ops.close_errors[102] = OSError(errno.EIO, "child close failed")

        for scenario, configure in (
            ("component-open", open_error),
            ("stat", stat_error),
            ("parent-close", parent_close_error),
            ("parent-and-child-close", child_close_error),
        ):
            with self.subTest(scenario=scenario):
                ops = _FakeOps()
                configure(ops)
                with self.assertRaises(BaseException):
                    DirectoryDescriptor.open_secure_path(ops, "/a/b", label="target")
                self._assert_balanced(ops)
                self.assertEqual(ops.open_fds, set())

    def test_failed_parent_close_is_attempted_exactly_once(self) -> None:
        ops = _FakeOps()
        ops.close_errors[101] = OSError(errno.EIO, "parent close failed")
        with self.assertRaises(OSError):
            DirectoryDescriptor.open_secure_path(ops, "/a/b", label="target")
        self.assertEqual(ops.closed_fds.count(101), 1)
        self.assertEqual(_close_calls(ops).count(101), 1)

    def test_securing_failures_release_every_child_fd_at_most_once(self) -> None:
        for path in ("create", "open_or_create"):
            for error in (
                KeyboardInterrupt(),
                RuntimeError("unexpected securing defect"),
                OSError(errno.EACCES, "denied"),
            ):
                with self.subTest(path=path, primary=type(error).__name__):
                    ops = _FakeOps()
                    parent = _adopt_parent(ops)
                    _raise_from_fchmod(ops, error)
                    with self.assertRaises(BaseException):
                        _invoke_directory_creation(parent, path, "child", 0o700)
                    self._assert_balanced(ops)
                    self.assertEqual(ops.open_fds, {parent.fd})
                    self.assertEqual(len(ops.closed_fds), 1)
                    parent.close()
                    self._assert_balanced(ops)
                    self.assertEqual(ops.open_fds, set())


if __name__ == "__main__":
    unittest.main()
