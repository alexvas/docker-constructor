"""Phase 2 tasks 2.2–2.5 and 2.14–2.15 — owned descriptor lifecycle.

A directly constructible ``OwnedDescriptor`` exclusively owns one live
descriptor until it transfers that ownership or attempts release.  These
contracts pin construction validation, the live/transferred/release-attempted
state machine, irreversible at-most-once release, explicit detach, and Phase 1
cleanup precedence at the context-manager boundary.
"""
from __future__ import annotations

import ast
import errno
import inspect
import os
import sys
import tempfile
import unittest
from pathlib import Path

from docker.filesystem.cleanup import carry_secondary_diagnostics
from docker.filesystem.descriptors import (
    DescriptorError,
    OwnedDescriptor,
    UnsafeDescriptorError,
)

_LIVE_FD = 7
_OTHER_FD = 8

_REPO = Path(__file__).resolve().parents[1]
_FOUNDATION = _REPO / "docker" / "filesystem"
_POK = inspect.Parameter.POSITIONAL_OR_KEYWORD
_KO = inspect.Parameter.KEYWORD_ONLY
_EMPTY = inspect.Parameter.empty
_FORBIDDEN_DOMAIN_MODULES = (
    "docker.transactions",
    "docker.npm_environment",
    "docker.versioning",
    "docker.constructor_cli",
    "docker.launcher",
)
_FORBIDDEN_AUTHORITY = (
    "os.walk",
    "os.scandir",
    "os.fsync",
    "os.fdatasync",
    "shutil.rmtree",
    "fcntl",
    "flock",
    "remove_tree",
    "LockedNpmError",
    "CacheStorageError",
)
_FORBIDDEN_PATH_DERIVATION = (
    "os.path.join",
    "os.path.realpath",
    "os.path.abspath",
    "os.path.normpath",
    "os.path.dirname",
    "os.path.basename",
)


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


class _AggregateError(Exception):
    """A primary that follows the generic secondary-diagnostic protocol."""

    def __init__(self, message: str = "aggregate") -> None:
        super().__init__(message)
        self.secondary: list[BaseException] = []

    def add_secondary(self, exc: BaseException) -> None:
        if not any(item is exc for item in self.secondary):
            self.secondary.append(exc)


class _Cancellation(BaseException):
    """A cancellation-style, non-I/O process-control exception."""


class _RecordingOps:
    """A minimal in-memory ``DescriptorOps`` double for ownership-state tests."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.close_error: BaseException | None = None
        self.close_errors: dict[int, BaseException] = {}
        self.before_close = None

    def close(self, fd: int) -> None:
        self.calls.append(("close", fd))
        if self.before_close is not None:
            self.before_close(fd)
        error = self.close_errors.get(fd, self.close_error)
        if error is not None:
            raise error


def _owned(ops: _RecordingOps, fd: int = _LIVE_FD, label: str = "entry") -> OwnedDescriptor:
    return OwnedDescriptor(ops, fd, label=label)


class DescriptorErrorMessageTests(unittest.TestCase):
    def test_retains_stage_message_and_cause(self) -> None:
        cause = OSError(errno.ENOENT, "missing")
        error = DescriptorError("validate-descriptor", "descriptor rejected", cause=cause)
        self.assertEqual(error.stage, "validate-descriptor")
        self.assertEqual(str(error), "descriptor rejected")
        self.assertIs(error.cause, cause)
        self.assertIs(error.__cause__, cause)

    def test_cause_defaults_to_none_without_chaining(self) -> None:
        error = DescriptorError("validate-descriptor", "descriptor rejected")
        self.assertIsNone(error.cause)
        self.assertIsNone(error.__cause__)


class UnsafeDescriptorErrorMessageTests(unittest.TestCase):
    def test_inherits_the_descriptor_error_constructor_unchanged(self) -> None:
        cause = OSError(errno.ELOOP, "loop")
        error = UnsafeDescriptorError("validate-descriptor", "not a directory", cause=cause)
        self.assertIsInstance(error, DescriptorError)
        self.assertEqual(error.stage, "validate-descriptor")
        self.assertEqual(str(error), "not a directory")
        self.assertIs(error.cause, cause)
        self.assertIs(error.__cause__, cause)


class OwnedDescriptorConstructionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ops = _RecordingOps()

    def test_accepts_a_non_negative_integer_and_non_empty_label(self) -> None:
        owned = _owned(self.ops)
        self.assertEqual(owned.fd, _LIVE_FD)
        self.assertEqual(owned.label, "entry")
        self.assertFalse(owned.released)
        owned.close()

    def test_construction_performs_no_filesystem_validation(self) -> None:
        owned = _owned(self.ops)
        self.assertEqual(self.ops.calls, [])
        owned.close()

    def test_rejects_negative_fd_before_accepting_ownership(self) -> None:
        with self.assertRaises(DescriptorError):
            OwnedDescriptor(self.ops, -1, label="entry")
        self.assertEqual(self.ops.calls, [])

    def test_rejects_non_integer_fd_before_accepting_ownership(self) -> None:
        for bad in ("3", 3.0, None, object()):
            with self.subTest(fd=bad):
                with self.assertRaises(DescriptorError):
                    OwnedDescriptor(self.ops, bad, label="entry")
        self.assertEqual(self.ops.calls, [])

    def test_rejects_empty_or_non_string_label_before_accepting_ownership(self) -> None:
        for bad in ("", None, 7, object()):
            with self.subTest(label=bad):
                with self.assertRaises(DescriptorError):
                    OwnedDescriptor(self.ops, _LIVE_FD, label=bad)
        self.assertEqual(self.ops.calls, [])

    def test_invalid_label_leaves_the_callers_descriptor_open(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        fd = os.open(tmp.name, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with self.assertRaises(DescriptorError):
                OwnedDescriptor(self.ops, fd, label="")
            self.assertEqual(self.ops.calls, [])
            self.assertTrue(os.fstat(fd))
        finally:
            os.close(fd)


class OwnedDescriptorStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ops = _RecordingOps()

    def test_fd_label_and_released_expose_live_state(self) -> None:
        owned = _owned(self.ops)
        self.assertEqual(owned.fd, _LIVE_FD)
        self.assertEqual(owned.label, "entry")
        self.assertFalse(owned.released)
        owned.close()

    def test_label_stays_accessible_after_release(self) -> None:
        owned = _owned(self.ops)
        owned.close()
        self.assertEqual(owned.label, "entry")

    def test_fd_access_fails_after_release(self) -> None:
        owned = _owned(self.ops)
        owned.close()
        with self.assertRaises(DescriptorError):
            _ = owned.fd

    def test_close_marks_release_before_invoking_close(self) -> None:
        owned = _owned(self.ops)
        observed: list[bool] = []
        self.ops.before_close = lambda fd: observed.append(owned.released)
        owned.close()
        self.assertEqual(observed, [True])

    def test_close_is_irreversible_and_not_retried_after_success(self) -> None:
        owned = _owned(self.ops)
        owned.close()
        self.assertTrue(owned.released)
        owned.close()
        self.assertEqual(self.ops.calls, [("close", _LIVE_FD)])

    def test_close_is_not_retried_after_failure(self) -> None:
        failure = OSError(errno.EIO, "injected close")
        self.ops.close_error = failure
        owned = _owned(self.ops)
        with self.assertRaises(OSError) as ctx:
            owned.close()
        self.assertIs(ctx.exception, failure)
        self.assertTrue(owned.released)
        owned.close()
        self.assertEqual(self.ops.calls, [("close", _LIVE_FD)])

    def test_detach_returns_the_live_fd_and_issues_no_operation(self) -> None:
        owned = _owned(self.ops)
        returned = owned.detach()
        self.assertEqual(returned, _LIVE_FD)
        self.assertEqual(self.ops.calls, [])
        self.assertFalse(owned.released)

    def test_detach_is_terminal_and_rejects_access_and_second_detach(self) -> None:
        owned = _owned(self.ops)
        owned.detach()
        with self.assertRaises(DescriptorError):
            _ = owned.fd
        with self.assertRaises(DescriptorError):
            owned.detach()
        self.assertEqual(self.ops.calls, [])

    def test_close_after_detach_issues_no_operation(self) -> None:
        owned = _owned(self.ops)
        owned.detach()
        owned.close()
        self.assertEqual(self.ops.calls, [])

    def test_context_manager_enter_returns_the_live_capability(self) -> None:
        owned = _owned(self.ops)
        with owned as entered:
            self.assertIs(entered, owned)
        self.assertTrue(owned.released)
        self.assertEqual(self.ops.calls, [("close", _LIVE_FD)])

    def test_entering_a_released_capability_is_rejected(self) -> None:
        owned = _owned(self.ops)
        owned.close()
        with self.assertRaises(DescriptorError):
            with owned:
                pass


class OwnedDescriptorCleanupPrecedenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ops = _RecordingOps()
        self.close_error = OSError(errno.EIO, "injected close")

    def test_close_failure_without_active_primary_is_sole(self) -> None:
        owned = _owned(self.ops)
        self.ops.close_error = self.close_error
        with self.assertRaises(OSError) as ctx:
            with owned:
                pass
        self.assertIs(ctx.exception, self.close_error)
        self.assertTrue(owned.released)

    def test_close_failure_is_secondary_to_an_active_primary(self) -> None:
        owned = _owned(self.ops)
        self.ops.close_error = self.close_error
        primary = _AggregateError("body failed")
        with self.assertRaises(_AggregateError) as ctx:
            with owned:
                raise primary
        self.assertIs(ctx.exception, primary)
        self.assertEqual(primary.secondary, [self.close_error])
        self.assertTrue(owned.released)

    def test_close_failure_is_secondary_on_a_descriptor_error(self) -> None:
        owned = _owned(self.ops)
        self.ops.close_error = self.close_error
        primary = DescriptorError("body", "body failed")
        with self.assertRaises(DescriptorError) as ctx:
            with owned:
                raise primary
        self.assertIs(ctx.exception, primary)
        collector = _AggregateError("collector")
        carry_secondary_diagnostics(collector, primary)
        self.assertEqual(collector.secondary, [self.close_error])

    def test_process_control_close_failure_is_authoritative(self) -> None:
        for interruption in (KeyboardInterrupt(), _Cancellation()):
            with self.subTest(interruption=type(interruption).__name__):
                ops = _RecordingOps()
                owned = _owned(ops)
                ops.close_error = interruption
                primary = _AggregateError("body failed")
                with self.assertRaises(type(interruption)) as ctx:
                    with owned:
                        raise primary
                self.assertIs(ctx.exception, interruption)
                self.assertTrue(owned.released)

    def test_close_failure_does_not_prevent_independent_later_cleanup(self) -> None:
        outer = _owned(self.ops, _LIVE_FD, label="outer")
        inner = _owned(self.ops, _OTHER_FD, label="inner")
        # Fail the *inner* close so the failure happens before the outer cleanup.
        self.ops.close_errors[_OTHER_FD] = self.close_error
        with self.assertRaises(OSError) as ctx:
            with outer:
                with inner:
                    pass
        self.assertIs(ctx.exception, self.close_error)
        # The inner failure must not suppress the independent, later outer
        # release: the inner close is attempted first and the outer close still
        # runs afterwards, exactly once.
        self.assertEqual(self.ops.calls, [("close", _OTHER_FD), ("close", _LIVE_FD)])
        self.assertTrue(inner.released)
        self.assertTrue(outer.released)


class DescriptorSignatureTests(unittest.TestCase):
    """Task 2.14 — the descriptor surface matches the design exactly."""

    def test_descriptor_error_constructor_is_exact(self) -> None:
        expected = [
            ("self", _POK, _EMPTY),
            ("stage", _POK, _EMPTY),
            ("message", _POK, _EMPTY),
            ("cause", _KO, None),
        ]
        self.assertEqual(_params(DescriptorError.__init__), expected)

    def test_unsafe_descriptor_error_inherits_without_widening(self) -> None:
        self.assertEqual(
            inspect.signature(UnsafeDescriptorError.__init__),
            inspect.signature(DescriptorError.__init__),
        )

    def test_owned_descriptor_constructor_is_exact(self) -> None:
        expected = [
            ("self", _POK, _EMPTY),
            ("ops", _POK, _EMPTY),
            ("fd", _POK, _EMPTY),
            ("label", _KO, _EMPTY),
        ]
        self.assertEqual(_params(OwnedDescriptor.__init__), expected)

    def test_owned_descriptor_lifecycle_methods_are_exact(self) -> None:
        for method in (OwnedDescriptor.close, OwnedDescriptor.detach, OwnedDescriptor.__enter__):
            with self.subTest(method=method.__name__):
                self.assertEqual(_params(method), [("self", _POK, _EMPTY)])

    def test_owned_descriptor_exit_signature_is_exact(self) -> None:
        expected = [
            ("self", _POK, _EMPTY),
            ("exc_type", _POK, _EMPTY),
            ("exc", _POK, _EMPTY),
            ("tb", _POK, _EMPTY),
        ]
        self.assertEqual(_params(OwnedDescriptor.__exit__), expected)
        self.assertEqual(_return_name(OwnedDescriptor.__exit__), "None")

    def test_owned_descriptor_property_getters_are_exact(self) -> None:
        expected = {"fd": "int", "label": "str", "released": "bool"}
        for name, annotation in expected.items():
            with self.subTest(property=name):
                descriptor = getattr(OwnedDescriptor, name)
                self.assertIsInstance(descriptor, property)
                fget = descriptor.fget
                self.assertIsNotNone(fget)
                self.assertEqual(_params(fget), [("self", _POK, _EMPTY)])
                self.assertEqual(_return_name(fget), annotation)

    def test_no_extra_constructor_options(self) -> None:
        for func in (
            DescriptorError.__init__,
            UnsafeDescriptorError.__init__,
            OwnedDescriptor.__init__,
        ):
            with self.subTest(func=func.__qualname__):
                kinds = {
                    parameter.kind
                    for parameter in inspect.signature(func).parameters.values()
                }
                self.assertNotIn(inspect.Parameter.VAR_KEYWORD, kinds)
                self.assertNotIn(inspect.Parameter.VAR_POSITIONAL, kinds)


class FoundationAuthorityBoundaryTests(unittest.TestCase):
    """Task 2.15 — no path derivation, recursion, durability, or domain authority."""

    _MODULES = ("operations.py", "descriptors.py")

    def test_imports_stay_within_the_standard_library_and_foundation(self) -> None:
        for filename in self._MODULES:
            tree = ast.parse((_FOUNDATION / filename).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        with self.subTest(module=filename, imported=alias.name):
                            self.assertIn(alias.name.split(".")[0], sys.stdlib_module_names)
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    module = node.module
                    top = module.split(".")[0]
                    with self.subTest(module=filename, imported=module):
                        self.assertTrue(
                            module == "docker.filesystem"
                            or module.startswith("docker.filesystem.")
                            or top in sys.stdlib_module_names
                        )
                        for forbidden in _FORBIDDEN_DOMAIN_MODULES:
                            self.assertFalse(
                                module == forbidden or module.startswith(forbidden + ".")
                            )

    def test_no_path_derivation_durability_locking_or_domain_authority(self) -> None:
        tokens = _FORBIDDEN_AUTHORITY + _FORBIDDEN_PATH_DERIVATION
        for filename in self._MODULES:
            source = (_FOUNDATION / filename).read_text(encoding="utf-8")
            for token in tokens:
                with self.subTest(module=filename, token=token):
                    self.assertNotIn(token, source)

    def test_no_recursive_functions(self) -> None:
        for filename in self._MODULES:
            tree = ast.parse((_FOUNDATION / filename).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for call in ast.walk(node):
                    if not isinstance(call, ast.Call):
                        continue
                    func = call.func
                    if isinstance(func, ast.Name) and func.id == node.name:
                        self.fail(f"{filename}:{node.name} recurses through a bare name")
                    if (
                        isinstance(func, ast.Attribute)
                        and func.attr == node.name
                        and isinstance(func.value, ast.Name)
                        and func.value.id in ("self", "cls")
                    ):
                        self.fail(f"{filename}:{node.name} recurses through self/cls")


if __name__ == "__main__":
    unittest.main()
