"""Phase 4 — transaction directory capabilities reuse the shared foundation.

``DirectoryCapability`` keeps its permanent transaction authority contract, its
public ``from_fd(ops, fd, label)`` pre-transfer validation semantics, and its
L1 regular-file factory, but delegates directory ownership, secure path
walking, and release state to
:class:`docker.filesystem.descriptors.DirectoryDescriptor`.  A failed
pre-transfer validation leaves the caller-owned descriptor open and never
issues a close.
"""
from __future__ import annotations

import ast
import errno
import inspect
import os
import stat
import tempfile
import unittest
from pathlib import Path

from docker.filesystem.descriptors import DescriptorError
from docker.transactions import capabilities as capabilities_module
from docker.transactions.capabilities import DirectoryCapability, FileCapability
from docker.transactions.errors import (
    STAGE_CLOSE,
    STAGE_OPEN,
    STAGE_VALIDATE,
    CapabilityError,
    TransactionError,
)
from tests.transactions_test_support import InjectedOps

_REPO = Path(__file__).resolve().parents[1]


def _source(relpath: str) -> str:
    return (_REPO / relpath).read_text(encoding="utf-8")


def _dir_stat(uid: int | None = None) -> os.stat_result:
    values = [
        stat.S_IFDIR | 0o700,
        0,
        0,
        1,
        os.geteuid() if uid is None else uid,
        0,
        0,
        0,
        0,
        0,
    ]
    return os.stat_result(values)


def _file_stat() -> os.stat_result:
    values = list(_dir_stat())
    values[0] = stat.S_IFREG | 0o600
    return os.stat_result(values)


class FromFdContractTests(unittest.TestCase):
    """Task 4.1 — ``from_fd`` validates before it transfers ownership."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ops = InjectedOps()
        self._owned: list[int] = []

    def tearDown(self) -> None:
        for fd in self._owned:
            try:
                os.close(fd)
            except OSError:
                pass

    def _open_root(self) -> int:
        fd = os.open(self.tmp.name, os.O_RDONLY | os.O_DIRECTORY)
        self._owned.append(fd)
        return fd

    def _release(self, fd: int) -> None:
        os.close(fd)
        self._owned.remove(fd)

    def test_successful_validation_transfers_sole_ownership(self) -> None:
        fd = self._open_root()
        self.ops.reset()
        capability = DirectoryCapability.from_fd(self.ops, fd, "root")
        self.assertEqual(capability.fd, fd)
        self.assertEqual(capability.label, "root")
        self.assertFalse(capability.closed)
        self.assertIs(capability.token, capability.token)
        # Validation transferred ownership without closing anything.
        self.assertNotIn("close", self.ops.order)
        capability.close()
        self.assertTrue(capability.closed)
        with self.assertRaises(CapabilityError) as ctx:
            _ = capability.fd
        self.assertIsNone(ctx.exception.__cause__)
        self.assertEqual(self.ops.order.count("close"), 1)
        self._owned.remove(fd)

    def test_failed_fstat_leaves_fd_caller_owned_and_open(self) -> None:
        fd = self._open_root()
        self.ops.reset()
        error = OSError(errno.EIO, "injected fstat")
        self.ops.failures["fstat"] = error
        with self.assertRaises(TransactionError) as ctx:
            DirectoryCapability.from_fd(self.ops, fd, "root")
        self.assertEqual(ctx.exception.stage, STAGE_VALIDATE)
        self.assertIs(ctx.exception.cause, error)
        self.assertNotIn("close", self.ops.order)
        # The rejected descriptor is still open and usable by the caller.
        self.assertTrue(stat.S_ISDIR(os.fstat(fd).st_mode))
        self._release(fd)

    def test_failed_directory_type_validation_leaves_fd_caller_owned(self) -> None:
        fd = self._open_root()
        self.ops.reset()
        self.ops.fstat_override = lambda info: _file_stat()
        with self.assertRaises(CapabilityError):
            DirectoryCapability.from_fd(self.ops, fd, "root")
        self.assertNotIn("close", self.ops.order)
        self.assertTrue(stat.S_ISDIR(os.fstat(fd).st_mode))
        self._release(fd)

    def test_failed_owner_validation_leaves_fd_caller_owned(self) -> None:
        fd = self._open_root()
        self.ops.reset()
        self.ops.fstat_override = lambda info: _dir_stat(uid=os.geteuid() + 1)
        with self.assertRaises(CapabilityError):
            DirectoryCapability.from_fd(self.ops, fd, "root")
        self.assertNotIn("close", self.ops.order)
        self.assertTrue(stat.S_ISDIR(os.fstat(fd).st_mode))
        self._release(fd)

    def test_no_failed_validation_path_invokes_close(self) -> None:
        cases = (
            (
                "fstat",
                lambda ops: ops.failures.__setitem__(
                    "fstat", OSError(errno.EIO, "boom")
                ),
                TransactionError,
            ),
            (
                "type",
                lambda ops: setattr(ops, "fstat_override", lambda info: _file_stat()),
                CapabilityError,
            ),
            (
                "owner",
                lambda ops: setattr(
                    ops,
                    "fstat_override",
                    lambda info: _dir_stat(uid=os.geteuid() + 1),
                ),
                CapabilityError,
            ),
        )
        for label, prepare, expected in cases:
            with self.subTest(path=label):
                ops = InjectedOps()
                prepare(ops)
                fd = os.open(self.tmp.name, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    with self.assertRaises(expected):
                        DirectoryCapability.from_fd(ops, fd, "root")
                    self.assertEqual(ops.counts.get("close", 0), 0)
                finally:
                    os.close(fd)

    def test_close_fault_injection_propagates_and_is_not_retried(self) -> None:
        fd = self._open_root()
        self.ops.reset()
        capability = DirectoryCapability.from_fd(self.ops, fd, "root")
        self._owned.remove(fd)
        close_error = OSError(errno.EIO, "injected close")
        self.ops.failures["close"] = close_error
        with self.assertRaises(TransactionError) as ctx:
            capability.close()
        self.assertEqual(ctx.exception.stage, STAGE_CLOSE)
        self.assertIs(ctx.exception.cause, close_error)
        self.assertTrue(capability.closed)
        self.assertEqual(self.ops.order.count("close"), 1)
        # A second close is a no-op that issues no system call.
        capability.close()
        self.assertEqual(self.ops.order.count("close"), 1)


class SharedDelegationStructureTests(unittest.TestCase):
    """Task 4.2 — delegate to the shared descriptor, not a second owner."""

    def test_module_imports_and_calls_the_shared_descriptor(self) -> None:
        tree = ast.parse(_source("docker/transactions/capabilities.py"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "docker.filesystem.descriptors"
            ):
                imported.update(alias.name for alias in node.names)
        self.assertIn("DirectoryDescriptor", imported)
        source = _source("docker/transactions/capabilities.py")
        self.assertIn("DirectoryDescriptor.open_secure_path", source)
        self.assertIn("DirectoryDescriptor._transfer_validated", source)

    def test_module_defines_no_second_component_walker(self) -> None:
        source = _source("docker/transactions/capabilities.py")
        self.assertNotIn("name.split(os.sep)", source)
        self.assertNotIn("ops.openat(None, os.sep", source)
        self.assertNotIn("components = [", source)

    def test_directory_capability_owns_no_release_state(self) -> None:
        tree = ast.parse(_source("docker/transactions/capabilities.py"))
        directory = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "DirectoryCapability"
        )
        slots: set[object] = set()
        for node in directory.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "__slots__"
                for target in node.targets
            ):
                slots = {element.value for element in node.value.elts}
        self.assertEqual(slots, {"_ops", "_descriptor", "_label", "_token"})
        self.assertNotIn("_closed", slots)
        # No raw fd ownership and no second release-state field.
        self.assertNotIn("_fd", slots)
        methods = {
            node.name
            for node in directory.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for forbidden in ("_validate", "_open_child_fd", "_secure_and_adopt"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, methods)

    def test_from_fd_uses_the_validated_transfer_seam_not_adopt(self) -> None:
        source = _source("docker/transactions/capabilities.py")
        tree = ast.parse(source)
        function = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "from_fd"
        )
        segment = ast.get_source_segment(source, function) or ""
        self.assertIn("_transfer_validated", segment)
        self.assertNotIn(".adopt(", segment)


class DirectoryOwnershipAstTests(unittest.TestCase):
    """Task 4.6 — the foundation is the sole release/walk implementation."""

    def test_foundation_owns_release_state_and_secure_walk(self) -> None:
        source = _source("docker/filesystem/descriptors.py")
        tree = ast.parse(source)
        classes = {
            node.name for node in tree.body if isinstance(node, ast.ClassDef)
        }
        self.assertIn("OwnedDescriptor", classes)
        self.assertIn("DirectoryDescriptor", classes)
        self.assertIn("_RELEASED", source)
        self.assertIn("ops.openat(None, os.sep", source)

    def test_no_transaction_module_reimplements_the_walk(self) -> None:
        offenders = [
            path.name
            for path in (_REPO / "docker" / "transactions").glob("*.py")
            if "ops.openat(None, os.sep" in path.read_text(encoding="utf-8")
            or "name.split(os.sep)" in path.read_text(encoding="utf-8")
        ]
        self.assertEqual([], offenders)

    def test_directory_capability_defines_no_lifecycle_state(self) -> None:
        source = _source("docker/transactions/capabilities.py")
        tree = ast.parse(source)
        directory = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "DirectoryCapability"
        )
        segment = ast.get_source_segment(source, directory) or ""
        self.assertNotIn("_closed", segment)
        self.assertNotIn("_RELEASED", segment)
        self.assertNotIn("name.split(os.sep)", segment)
        self.assertNotIn("components = [", segment)


class CompatibilityContractTests(unittest.TestCase):
    """Task 4.7 — signatures and aggregate exports are unchanged."""

    _EXPECTED_ALL = [
        "CapabilityError",
        "DestinationExists",
        "DirectoryCapability",
        "FileCapability",
        "LockCapability",
        "LockContention",
        "LockError",
        "LockPolicy",
        "PosixFileOps",
        "RegularFileContracts",
        "TransactionError",
        "UnsafeFileError",
        "decode",
        "encode",
    ]

    def test_from_fd_signature_is_permanent(self) -> None:
        parameters = inspect.signature(DirectoryCapability.from_fd).parameters
        self.assertEqual(list(parameters), ["ops", "fd", "label"])

    def test_factory_signatures_are_permanent(self) -> None:
        for name in ("from_path", "from_secure_path"):
            with self.subTest(factory=name):
                parameters = inspect.signature(
                    getattr(DirectoryCapability, name)
                ).parameters
                self.assertEqual(list(parameters), ["ops", "path", "label"])
                self.assertEqual(
                    parameters["label"].kind, inspect.Parameter.KEYWORD_ONLY
                )
                self.assertIsNone(parameters["label"].default)

    def test_transaction_aggregate_exports_are_unchanged(self) -> None:
        import docker.transactions as transactions

        self.assertEqual(transactions.__all__, self._EXPECTED_ALL)

    def test_directory_capability_still_builds_file_capabilities(self) -> None:
        self.assertTrue(hasattr(DirectoryCapability, "open_regular"))
        self.assertTrue(hasattr(DirectoryCapability, "child_basename"))
        self.assertIsNotNone(FileCapability)

    def test_module_keeps_the_regular_file_validator(self) -> None:
        self.assertTrue(hasattr(capabilities_module, "_validate_regular"))


class FromSecurePathRootFailureTests(unittest.TestCase):
    """A failed initial root ``openat`` never leaks a raw ``OSError``.

    ``DirectoryCapability.from_secure_path`` only maps
    ``DescriptorError``/``UnsafeDescriptorError`` back to ``CapabilityError``,
    so the foundation walk must translate the very first root open failure too
    — including for ``"/"``, which would otherwise be adopted directly.
    """

    def _first_open_failure(self, errno_value: int) -> tuple[InjectedOps, OSError]:
        ops = InjectedOps()
        error = OSError(errno_value, "injected root open")
        ops.failures["openat"] = lambda count: error if count == 1 else None
        return ops, error

    def test_root_open_failure_is_cause_preserving_typed_or_safety_error(self) -> None:
        for path in ("/", "/a/b"):
            with self.subTest(path=path, errno=errno.EIO):
                ops, error = self._first_open_failure(errno.EIO)
                with self.assertRaises(TransactionError) as ctx:
                    DirectoryCapability.from_secure_path(ops, path)
                self.assertEqual(ctx.exception.stage, STAGE_OPEN)
                self.assertIs(ctx.exception.cause, error)
                self.assertIs(ctx.exception.__cause__, error)
                # Only the root open was attempted; nothing to release.
                self.assertEqual(ops.counts.get("openat", 0), 1)
                self.assertEqual(ops.counts.get("close", 0), 0)
                self.assertEqual(
                    str(ctx.exception),
                    f"cannot open directory capability {path!r}: {error}",
                )
            with self.subTest(path=path, errno=errno.ELOOP):
                ops, error = self._first_open_failure(errno.ELOOP)
                with self.assertRaises(CapabilityError) as ctx:
                    DirectoryCapability.from_secure_path(ops, path)
                self.assertNotIsInstance(ctx.exception, TransactionError)
                self.assertIs(ctx.exception.__cause__, error)
                self.assertEqual(ops.counts.get("openat", 0), 1)
                self.assertEqual(ops.counts.get("close", 0), 0)


class FromSecurePathCompatibilityTests(unittest.TestCase):
    """Task 4.5 — pre-migration ``from_secure_path`` wording and causes.

    The shared foundation translates generic walk failures into
    ``DescriptorError``/``UnsafeDescriptorError``; this adapter must restore
    the exact transaction-facing ``CapabilityError`` text and ``__cause__``
    behavior that existed before the migration.  These are characterization
    tests: they pin the behavior of the previous transaction-local walk so the
    delegation layer cannot silently change diagnostics.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.ops = InjectedOps()

    def test_relative_path_refusal_keeps_message_and_has_no_cause(self) -> None:
        with self.assertRaises(CapabilityError) as ctx:
            DirectoryCapability.from_secure_path(self.ops, "relative/dir")
        self.assertEqual(
            str(ctx.exception),
            "directory capability 'relative/dir' requires an absolute path",
        )
        self.assertIsNone(ctx.exception.__cause__)
        # The refusal happens before any descriptor operation.
        self.assertEqual(self.ops.calls, [])

    def test_root_path_open_failure_keeps_message_and_raw_cause(self) -> None:
        error = OSError(errno.EIO, "injected root open")
        self.ops.failures["openat"] = lambda count: error if count == 1 else None
        with self.assertRaises(TransactionError) as ctx:
            DirectoryCapability.from_secure_path(self.ops, os.sep)
        self.assertEqual(ctx.exception.stage, STAGE_OPEN)
        self.assertEqual(
            str(ctx.exception),
            f"cannot open directory capability {os.sep!r}: {error}",
        )
        self.assertIs(ctx.exception.cause, error)

    def test_child_open_failure_keeps_message_and_raw_cause(self) -> None:
        target = os.path.join(self.root, "a", "b")
        os.makedirs(os.path.join(self.root, "a"), mode=0o700)
        components = [part for part in target.split(os.sep) if part]
        error = OSError(errno.ENOENT, "injected child open")
        self.ops.failures["openat"] = lambda count: (
            error if count == len(components) + 1 else None
        )
        with self.assertRaises(TransactionError) as ctx:
            DirectoryCapability.from_secure_path(self.ops, target)
        self.assertEqual(ctx.exception.stage, STAGE_OPEN)
        self.assertEqual(
            str(ctx.exception),
            f"cannot open directory capability {target!r}: {error}",
        )
        self.assertIs(ctx.exception.cause, error)

    def test_injected_eloop_open_failure_keeps_message_and_raw_cause(self) -> None:
        target = os.path.join(self.root, "a", "b")
        os.makedirs(os.path.join(self.root, "a"), mode=0o700)
        components = [part for part in target.split(os.sep) if part]
        error = OSError(errno.ELOOP, "injected symlink rejection")
        self.ops.failures["openat"] = lambda count: (
            error if count == len(components) + 1 else None
        )
        with self.assertRaises(CapabilityError) as ctx:
            DirectoryCapability.from_secure_path(self.ops, target)
        self.assertEqual(
            str(ctx.exception),
            f"cannot open directory capability {target!r}: {error}",
        )
        self.assertIs(ctx.exception.__cause__, error)

    def test_real_intermediate_symlink_keeps_message_and_raw_cause(self) -> None:
        real = os.path.join(self.root, "real")
        os.makedirs(real, mode=0o700)
        link = os.path.join(self.root, "link")
        os.symlink(real, link)
        with self.assertRaises(CapabilityError) as ctx:
            DirectoryCapability.from_secure_path(self.ops, link)
        cause = ctx.exception.__cause__
        self.assertIsInstance(cause, OSError)
        self.assertIn(cause.errno, {errno.ELOOP, errno.ENOTDIR})
        self.assertEqual(
            str(ctx.exception),
            f"cannot open directory capability {link!r}: {cause}",
        )

    def test_final_nondirectory_keeps_message_and_has_no_cause(self) -> None:
        target = os.path.join(self.root, "target")
        os.makedirs(target, mode=0o700)
        values = list(os.stat(target))
        values[0] = stat.S_IFREG | 0o600
        self.ops.fstat_override = lambda info: os.stat_result(values)
        with self.assertRaises(CapabilityError) as ctx:
            DirectoryCapability.from_secure_path(self.ops, target)
        self.assertEqual(
            str(ctx.exception),
            f"directory capability {target!r} is not a directory",
        )
        self.assertIsNone(ctx.exception.__cause__)

    def test_final_foreign_owner_keeps_message_and_has_no_cause(self) -> None:
        target = os.path.join(self.root, "target")
        os.makedirs(target, mode=0o700)
        values = list(os.stat(target))
        values[4] = os.geteuid() + 1
        self.ops.fstat_override = lambda info: os.stat_result(values)
        with self.assertRaises(CapabilityError) as ctx:
            DirectoryCapability.from_secure_path(self.ops, target)
        self.assertEqual(
            str(ctx.exception),
            f"directory capability {target!r} is not owned by the invoking user",
        )
        self.assertIsNone(ctx.exception.__cause__)

    def test_validation_label_is_preserved_in_type_message(self) -> None:
        target = os.path.join(self.root, "target")
        os.makedirs(target, mode=0o700)
        values = list(os.stat(target))
        values[0] = stat.S_IFREG | 0o600
        self.ops.fstat_override = lambda info: os.stat_result(values)
        with self.assertRaises(CapabilityError) as ctx:
            DirectoryCapability.from_secure_path(self.ops, target, label="custom")
        self.assertEqual(
            str(ctx.exception),
            "directory capability 'custom' is not a directory",
        )
        self.assertIsNone(ctx.exception.__cause__)

    def test_validation_label_is_preserved_in_owner_message(self) -> None:
        target = os.path.join(self.root, "target")
        os.makedirs(target, mode=0o700)
        values = list(os.stat(target))
        values[4] = os.geteuid() + 1
        self.ops.fstat_override = lambda info: os.stat_result(values)
        with self.assertRaises(CapabilityError) as ctx:
            DirectoryCapability.from_secure_path(self.ops, target, label="custom")
        self.assertEqual(
            str(ctx.exception),
            "directory capability 'custom' is not owned by the invoking user",
        )
        self.assertIsNone(ctx.exception.__cause__)


class MappedCauseRegressionTests(unittest.TestCase):
    """Task 4.5 — mapped transaction errors keep no foundation ``__cause__``.

    The pre-migration ``DirectoryCapability`` raised bare ``CapabilityError``
    instances for a released ``fd``, an unsafe child basename, and the final
    type/owner validation failures.  Delegating to the shared foundation must
    not leak the foundation exception through ``__cause__``, and must not leak
    the foundation's normalized internal label into the message.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.ops = InjectedOps()

    def _make_directory(self) -> str:
        target = os.path.join(self.root, "target")
        os.makedirs(target, mode=0o700)
        return target

    def _fail_type(self, target: str) -> None:
        values = list(os.stat(target))
        values[0] = stat.S_IFREG | 0o600
        self.ops.fstat_override = lambda info: os.stat_result(values)

    def _fail_owner(self, target: str) -> None:
        values = list(os.stat(target))
        values[4] = os.geteuid() + 1
        self.ops.fstat_override = lambda info: os.stat_result(values)

    def test_released_fd_error_has_no_cause(self) -> None:
        capability = DirectoryCapability.from_secure_path(
            self.ops, self._make_directory()
        )
        capability.close()
        with self.assertRaises(CapabilityError) as ctx:
            _ = capability.fd
        self.assertIsNone(ctx.exception.__cause__)

    def test_invalid_child_basename_error_has_no_cause(self) -> None:
        capability = DirectoryCapability.from_secure_path(
            self.ops, self._make_directory()
        )
        try:
            with self.assertRaises(CapabilityError) as ctx:
                capability.child_basename("a/b")
            self.assertEqual(str(ctx.exception), "unsafe child basename: 'a/b'")
            self.assertIsNone(ctx.exception.__cause__)
        finally:
            capability.close()

    def test_empty_label_nondirectory_message_uses_transaction_label(self) -> None:
        target = self._make_directory()
        self._fail_type(target)
        with self.assertRaises(CapabilityError) as ctx:
            DirectoryCapability.from_secure_path(self.ops, target, label="")
        self.assertEqual(
            str(ctx.exception), "directory capability '' is not a directory"
        )
        self.assertNotIn("<directory-capability>", str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)

    def test_empty_label_foreign_owner_message_uses_transaction_label(self) -> None:
        target = self._make_directory()
        self._fail_owner(target)
        with self.assertRaises(CapabilityError) as ctx:
            DirectoryCapability.from_secure_path(self.ops, target, label="")
        self.assertEqual(
            str(ctx.exception),
            "directory capability '' is not owned by the invoking user",
        )
        self.assertNotIn("<directory-capability>", str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)

    def test_non_string_label_nondirectory_message_uses_transaction_label(self) -> None:
        target = self._make_directory()
        self._fail_type(target)
        label = Path("label")
        with self.assertRaises(CapabilityError) as ctx:
            DirectoryCapability.from_secure_path(self.ops, target, label=label)
        self.assertEqual(
            str(ctx.exception),
            f"directory capability {label!r} is not a directory",
        )
        self.assertNotIn("<directory-capability>", str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)

    def test_non_string_label_foreign_owner_message_uses_transaction_label(self) -> None:
        target = self._make_directory()
        self._fail_owner(target)
        label = Path("label")
        with self.assertRaises(CapabilityError) as ctx:
            DirectoryCapability.from_secure_path(self.ops, target, label=label)
        self.assertEqual(
            str(ctx.exception),
            f"directory capability {label!r} is not owned by the invoking user",
        )
        self.assertNotIn("<directory-capability>", str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)


class CleanupDiagnosticCarryTests(unittest.TestCase):
    """Task 4.5 — translated errors retain foundation close diagnostics.

    A final type/owner rejection or a walk open failure can be followed by a
    failure while the secure walk releases its retained descriptors.  The
    transaction adapter replaces the foundation error with a
    ``CapabilityError``; it must carry the retained cleanup exceptions onto
    that replacement so diagnostics are not lost, while preserving the
    primary message/cause and never retrying a close.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.ops = InjectedOps()

    def _target(self) -> tuple[str, list[str]]:
        target = os.path.join(self.root, "target")
        os.makedirs(target, mode=0o700)
        components = [part for part in target.split(os.sep) if part]
        return target, components

    @staticmethod
    def _secondary(error: BaseException) -> list[BaseException]:
        return list(getattr(error, "_transaction_secondary", []))

    def test_nondirectory_validation_carries_leaf_close_failure(self) -> None:
        target, components = self._target()
        values = list(os.stat(target))
        values[0] = stat.S_IFREG | 0o600
        self.ops.fstat_override = lambda info: os.stat_result(values)
        close_error = OSError(errno.EIO, "injected leaf close")
        # Descent closes one descriptor per non-leaf component; the next
        # close is the adopted leaf released by the failed validation.
        leaf_close = len(components)
        self.ops.failures["close"] = (
            lambda count: close_error if count == leaf_close else None
        )
        with self.assertRaises(CapabilityError) as ctx:
            DirectoryCapability.from_secure_path(self.ops, target)
        error = ctx.exception
        self.assertEqual(
            str(error), f"directory capability {target!r} is not a directory"
        )
        self.assertIsNone(error.__cause__)
        self.assertEqual(self._secondary(error), [close_error])
        # Every descriptor was closed at most once: descent closes, the leaf,
        # and the retained parent.
        self.assertEqual(self.ops.counts["close"], len(components) + 1)

    def test_foreign_owner_validation_carries_leaf_close_failure(self) -> None:
        target, components = self._target()
        values = list(os.stat(target))
        values[4] = os.geteuid() + 1
        self.ops.fstat_override = lambda info: os.stat_result(values)
        close_error = OSError(errno.EIO, "injected leaf close")
        leaf_close = len(components)
        self.ops.failures["close"] = (
            lambda count: close_error if count == leaf_close else None
        )
        with self.assertRaises(CapabilityError) as ctx:
            DirectoryCapability.from_secure_path(self.ops, target)
        error = ctx.exception
        self.assertEqual(
            str(error),
            f"directory capability {target!r} is not owned by the invoking user",
        )
        self.assertIsNone(error.__cause__)
        self.assertEqual(self._secondary(error), [close_error])
        self.assertEqual(self.ops.counts["close"], len(components) + 1)

    def test_ordinary_open_failure_carries_parent_close_failure(self) -> None:
        target, components = self._target()
        open_error = OSError(errno.ENOENT, "injected open")
        self.ops.failures["openat"] = (
            lambda count: open_error if count == len(components) + 1 else None
        )
        close_error = OSError(errno.EIO, "injected parent close")
        # The retained parent is the last descriptor released after the failed
        # leaf open, following one descent close per intermediate component.
        parent_close = len(components)
        self.ops.failures["close"] = (
            lambda count: close_error if count == parent_close else None
        )
        with self.assertRaises(TransactionError) as ctx:
            DirectoryCapability.from_secure_path(self.ops, target)
        error = ctx.exception
        self.assertEqual(error.stage, STAGE_OPEN)
        self.assertEqual(
            str(error),
            f"cannot open directory capability {target!r}: {open_error}",
        )
        self.assertIs(error.cause, open_error)
        self.assertEqual(error.secondary, [close_error])
        self.assertEqual(self.ops.counts["close"], len(components))

    def test_eloop_open_failure_carries_parent_close_failure(self) -> None:
        target, components = self._target()
        open_error = OSError(errno.ELOOP, "injected symlink rejection")
        self.ops.failures["openat"] = (
            lambda count: open_error if count == len(components) + 1 else None
        )
        close_error = OSError(errno.EIO, "injected parent close")
        parent_close = len(components)
        self.ops.failures["close"] = (
            lambda count: close_error if count == parent_close else None
        )
        with self.assertRaises(CapabilityError) as ctx:
            DirectoryCapability.from_secure_path(self.ops, target)
        error = ctx.exception
        self.assertEqual(
            str(error),
            f"cannot open directory capability {target!r}: {open_error}",
        )
        self.assertIs(error.__cause__, open_error)
        self.assertEqual(self._secondary(error), [close_error])
        self.assertEqual(self.ops.counts["close"], len(components))


class EmptyLabelCompatibilityTests(unittest.TestCase):
    """A historically accepted empty label keeps working through every factory.

    The shared descriptor owner requires a non-empty internal label, so the
    transaction label is preserved separately and echoed back by
    :attr:`DirectoryCapability.label`.  ``label=""`` must never surface as a
    ``DescriptorError`` or ``CapabilityError`` and must still transfer and
    release ownership exactly once.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ops = InjectedOps()
        self._owned: list[int] = []

    def tearDown(self) -> None:
        for fd in self._owned:
            try:
                os.close(fd)
            except OSError:
                pass

    def _open_root(self) -> int:
        fd = os.open(self.tmp.name, os.O_RDONLY | os.O_DIRECTORY)
        self._owned.append(fd)
        return fd

    def _assert_empty_label_owns_and_closes_once(self, capability) -> int:
        self.assertEqual("", capability.label)
        fd = capability.fd
        self.assertIsInstance(fd, int)
        self.assertFalse(capability.closed)
        # Only the capability's own release touches the backend from here on.
        self.ops.reset()
        capability.close()
        self.assertTrue(capability.closed)
        self.assertEqual(1, self.ops.order.count("close"))
        # Released exactly once: the descriptor is gone and no close was retried.
        with self.assertRaises(OSError):
            os.fstat(fd)
        with self.assertRaises(CapabilityError):
            _ = capability.fd
        return fd

    def test_from_path_accepts_an_empty_label(self) -> None:
        capability = DirectoryCapability.from_path(self.ops, self.tmp.name, label="")
        self._assert_empty_label_owns_and_closes_once(capability)

    def test_from_secure_path_accepts_an_empty_label(self) -> None:
        capability = DirectoryCapability.from_secure_path(
            self.ops, self.tmp.name, label=""
        )
        self._assert_empty_label_owns_and_closes_once(capability)

    def test_from_fd_accepts_an_empty_label_after_transaction_validation(self) -> None:
        fd = self._open_root()
        self.ops.reset()
        try:
            capability = DirectoryCapability.from_fd(self.ops, fd, "")
        except DescriptorError as exc:  # pragma: no cover - compatibility guard
            self.fail(f"empty label must not expose DescriptorError: {exc!r}")
        # The existing fstat/type/owner validation ran before ownership moved.
        self.assertIn("fstat", self.ops.order)
        self.assertNotIn("close", self.ops.order)
        self.assertEqual(fd, capability.fd)
        self._assert_empty_label_owns_and_closes_once(capability)
        self._owned.remove(fd)


class NonStringLabelCompatibilityTests(unittest.TestCase):
    """A truthy non-string label must never strand the detached secure leaf.

    ``from_secure_path`` transfers the validated leaf out of the foundation
    walk with ``detach()`` before the transaction capability constructs its
    shared ``DirectoryDescriptor``.  The internal diagnostic label is
    normalized to a non-empty string, so a non-string transaction label can no
    longer make the post-detach transfer construction reject the descriptor
    with no owner left to close it.  ``DirectoryCapability.label`` still reports
    the original transaction label.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ops = InjectedOps()
        self._opened: list[int] = []
        original = self.ops.openat

        def capturing_openat(*args: object, **kwargs: object) -> int:
            fd = original(*args, **kwargs)
            self._opened.append(fd)
            return fd

        self.ops.openat = capturing_openat  # type: ignore[method-assign]

    def test_internal_label_always_normalizes_to_a_non_empty_string(self) -> None:
        helper = capabilities_module._internal_label
        self.assertEqual("root", helper("root"))
        self.assertEqual(capabilities_module._INTERNAL_LABEL, helper(""))
        for rejected in (Path("label"), object(), 0, 123, None, b"label"):
            self.assertEqual(capabilities_module._INTERNAL_LABEL, helper(rejected))

    def test_truthy_non_string_label_owns_and_releases_the_leaf_once(self) -> None:
        for label in (Path("label"), object()):
            with self.subTest(label=label):
                self._opened.clear()
                try:
                    capability = DirectoryCapability.from_secure_path(
                        self.ops, self.tmp.name, label=label
                    )
                except (CapabilityError, DescriptorError):
                    # A rejection is only tolerable if it leaked nothing; then
                    # fail, because the normalized label must construct.
                    for fd in set(self._opened):
                        with self.assertRaises(OSError):
                            os.fstat(fd)
                    self.fail("a normalized non-string label must construct")
                # The transaction-facing label is preserved verbatim.
                self.assertIs(label, capability.label)
                leaf_fd = capability.fd
                self.assertIsInstance(leaf_fd, int)
                self.assertFalse(capability.closed)
                # Every other descriptor the secure walk opened is released.
                for fd in set(self._opened) - {leaf_fd}:
                    with self.assertRaises(OSError):
                        os.fstat(fd)
                self.ops.reset()
                capability.close()
                self.assertTrue(capability.closed)
                self.assertEqual(1, self.ops.order.count("close"))
                # No leak: the leaf is truly gone and no close was retried.
                with self.assertRaises(OSError):
                    os.fstat(leaf_fd)
                with self.assertRaises(CapabilityError):
                    _ = capability.fd
class SecurePathValidationStatFailureTests(unittest.TestCase):
    """``from_secure_path`` restores the raw stat-failure contract.

    The previous ``_adopt`` path let a failing ``ops.fstat`` propagate as the
    original ``OSError`` (with cleanup diagnostics attached).  The secure walk
    wraps that final-leaf stat failure in a validation-stage
    ``DescriptorError``, so the compatibility adapter must unwrap it back to
    the raw cause — carrying the foundation's secondary close diagnostics —
    instead of inventing a new ``CapabilityError``.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ops = InjectedOps()

    def _close_failure_after_stat(self, error: OSError) -> None:
        """Fail the first close that follows the leaf validation stat."""
        state = {"statted": False, "failed": False}
        self.ops.hooks["fstat"] = lambda *args: state.__setitem__("statted", True)

        def close_failure(count: int) -> OSError | None:
            if state["statted"] and not state["failed"]:
                state["failed"] = True
                return error
            return None

        self.ops.failures["close"] = close_failure

    def _close_attempts(self) -> list[int]:
        return [args[0] for name, args in self.ops.calls if name == "close"]

    def test_final_leaf_fstat_failure_is_a_typed_validation_error(self) -> None:
        error = OSError(errno.EIO, "injected leaf fstat")
        self.ops.failures["fstat"] = error
        with self.assertRaises(TransactionError) as ctx:
            DirectoryCapability.from_secure_path(self.ops, self.tmp.name)
        self.assertEqual(ctx.exception.stage, STAGE_VALIDATE)
        self.assertIs(ctx.exception.cause, error)
        self.assertNotIsInstance(ctx.exception, CapabilityError)
        # Only the final leaf is validated by stat.
        self.assertEqual(self.ops.counts.get("fstat", 0), 1)
        # Every opened descriptor receives exactly one close attempt.
        self.assertEqual(
            self.ops.counts.get("openat", 0), self.ops.counts.get("close", 0)
        )
        leaf_fd = next(
            args[0] for name, args in self.ops.calls if name == "fstat"
        )
        self.assertIn(leaf_fd, self._close_attempts())

    def test_fstat_failure_keeps_close_failure_as_secondary(self) -> None:
        fstat_error = OSError(errno.EIO, "injected leaf fstat")
        close_error = OSError(errno.EIO, "injected leaf close")
        self.ops.failures["fstat"] = fstat_error
        self._close_failure_after_stat(close_error)
        with self.assertRaises(TransactionError) as ctx:
            DirectoryCapability.from_secure_path(self.ops, self.tmp.name)
        # The stat failure stays authoritative; the close defect is secondary.
        self.assertEqual(ctx.exception.stage, STAGE_VALIDATE)
        self.assertIs(ctx.exception.cause, fstat_error)
        self.assertNotIsInstance(ctx.exception, CapabilityError)
        self.assertEqual(ctx.exception.secondary, [close_error])
        # Every opened descriptor receives exactly one close attempt.
        self.assertEqual(
            self.ops.counts.get("openat", 0), self.ops.counts.get("close", 0)
        )
        leaf_fd = next(
            args[0] for name, args in self.ops.calls if name == "fstat"
        )
        self.assertIn(leaf_fd, self._close_attempts())


if __name__ == "__main__":
    unittest.main()
