"""Phase 2 task 2.1 — descriptor-operations protocol and POSIX adapter.

These contracts pin the injectable ``DescriptorOps`` seam and prove that the
production ``PosixDescriptorOps`` adapter delegates to the corresponding
descriptor-relative POSIX calls while preserving the raised ``OSError``
subclass, ``errno``, and cause behavior.
"""
from __future__ import annotations

import errno
import inspect
import os
import stat
import tempfile
import unittest

from docker.filesystem.operations import DescriptorOps, PosixDescriptorOps

_OPS_NAMES = (
    "openat",
    "close",
    "fstat",
    "fchmod",
    "mkdirat",
    "statat",
    "listdir",
    "unlinkat",
    "rmdirat",
)

_POK = inspect.Parameter.POSITIONAL_OR_KEYWORD
_KO = inspect.Parameter.KEYWORD_ONLY
_EMPTY = inspect.Parameter.empty


def _params(func: object) -> list[tuple[str, object, object]]:
    return [
        (parameter.name, parameter.kind, parameter.default)
        for parameter in inspect.signature(func).parameters.values()
    ]


_DESIGN_SIGNATURES: dict[str, list[tuple[str, object, object]]] = {
    "openat": [
        ("self", _POK, _EMPTY),
        ("directory_fd", _POK, _EMPTY),
        ("name", _POK, _EMPTY),
        ("flags", _POK, _EMPTY),
        ("mode", _POK, 0),
    ],
    "close": [("self", _POK, _EMPTY), ("fd", _POK, _EMPTY)],
    "fstat": [("self", _POK, _EMPTY), ("fd", _POK, _EMPTY)],
    "fchmod": [("self", _POK, _EMPTY), ("fd", _POK, _EMPTY), ("mode", _POK, _EMPTY)],
    "mkdirat": [
        ("self", _POK, _EMPTY),
        ("directory_fd", _POK, _EMPTY),
        ("name", _POK, _EMPTY),
        ("mode", _POK, _EMPTY),
    ],
    "statat": [
        ("self", _POK, _EMPTY),
        ("directory_fd", _POK, _EMPTY),
        ("name", _POK, _EMPTY),
        ("follow_symlinks", _KO, _EMPTY),
    ],
    "listdir": [("self", _POK, _EMPTY), ("fd", _POK, _EMPTY)],
    "unlinkat": [
        ("self", _POK, _EMPTY),
        ("directory_fd", _POK, _EMPTY),
        ("name", _POK, _EMPTY),
    ],
    "rmdirat": [
        ("self", _POK, _EMPTY),
        ("directory_fd", _POK, _EMPTY),
        ("name", _POK, _EMPTY),
    ],
}


class DescriptorOpsContractTests(unittest.TestCase):
    def test_protocol_exposes_every_design_method(self) -> None:
        self.assertTrue(getattr(DescriptorOps, "_is_protocol", False))
        for name in _OPS_NAMES:
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(DescriptorOps, name, None)))

    def test_production_adapter_exposes_every_design_method(self) -> None:
        for name in _OPS_NAMES:
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(PosixDescriptorOps, name, None)))

    def test_production_adapter_signatures_match_protocol(self) -> None:
        for name in _OPS_NAMES:
            with self.subTest(name=name):
                protocol = inspect.signature(getattr(DescriptorOps, name))
                adapter = inspect.signature(getattr(PosixDescriptorOps, name))
                self.assertEqual(
                    [(p.name, p.kind, p.default) for p in protocol.parameters.values()],
                    [(p.name, p.kind, p.default) for p in adapter.parameters.values()],
                )


class DescriptorOpsSignatureTests(unittest.TestCase):
    """Task 2.14 — the operations surface matches the design exactly."""

    def test_protocol_and_adapter_match_the_design_signatures(self) -> None:
        for name, expected in _DESIGN_SIGNATURES.items():
            with self.subTest(name=name):
                self.assertEqual(_params(getattr(DescriptorOps, name)), expected)
                self.assertEqual(_params(getattr(PosixDescriptorOps, name)), expected)

    def test_no_extra_constructor_options(self) -> None:
        for name in _OPS_NAMES:
            with self.subTest(name=name):
                kinds = {
                    parameter.kind
                    for parameter in inspect.signature(
                        getattr(DescriptorOps, name)
                    ).parameters.values()
                }
                self.assertNotIn(inspect.Parameter.VAR_KEYWORD, kinds)
                self.assertNotIn(inspect.Parameter.VAR_POSITIONAL, kinds)


class PosixDescriptorOpsDelegationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.ops = PosixDescriptorOps()

    def _open_root(self) -> int:
        fd = self.ops.openat(None, self.root, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(self._close_quietly, fd)
        return fd

    def _close_quietly(self, fd: int) -> None:
        try:
            self.ops.close(fd)
        except OSError:
            pass

    def test_openat_fstat_listdir_and_close_delegate(self) -> None:
        fd = self._open_root()
        self.assertTrue(stat.S_ISDIR(self.ops.fstat(fd).st_mode))
        self.assertEqual(self.ops.listdir(fd), [])
        with open(os.path.join(self.root, "entry"), "w", encoding="utf-8") as stream:
            stream.write("x")
        self.assertIn("entry", self.ops.listdir(fd))
        self.ops.close(fd)
        with self.assertRaises(OSError):
            self.ops.fstat(fd)

    def test_mkdirat_statat_rmdirat_and_unlinkat_delegate(self) -> None:
        fd = self._open_root()
        self.ops.mkdirat(fd, "child", 0o700)
        child = self.ops.statat(fd, "child", follow_symlinks=False)
        self.assertTrue(stat.S_ISDIR(child.st_mode))
        self.ops.rmdirat(fd, "child")
        with self.assertRaises(FileNotFoundError):
            self.ops.statat(fd, "child", follow_symlinks=False)

        leaf_fd = self.ops.openat(fd, "leaf", os.O_CREAT | os.O_WRONLY, 0o600)
        self.ops.close(leaf_fd)
        self.assertTrue(stat.S_ISREG(self.ops.statat(fd, "leaf", follow_symlinks=False).st_mode))
        self.ops.unlinkat(fd, "leaf")
        with self.assertRaises(FileNotFoundError):
            self.ops.statat(fd, "leaf", follow_symlinks=False)

    def test_fchmod_delegates(self) -> None:
        fd = self._open_root()
        leaf_fd = self.ops.openat(fd, "leaf", os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            self.ops.fchmod(leaf_fd, 0o640)
            self.assertEqual(stat.S_IMODE(self.ops.fstat(leaf_fd).st_mode), 0o640)
        finally:
            self.ops.close(leaf_fd)

    def test_statat_preserves_no_follow_and_follow_behavior(self) -> None:
        fd = self._open_root()
        self.ops.mkdirat(fd, "target", 0o700)
        os.symlink("target", os.path.join(self.root, "link"))
        link_info = self.ops.statat(fd, "link", follow_symlinks=False)
        self.assertTrue(stat.S_ISLNK(link_info.st_mode))
        target_info = self.ops.statat(fd, "link", follow_symlinks=True)
        self.assertTrue(stat.S_ISDIR(target_info.st_mode))

    def test_missing_entry_preserves_oserror_subclass_and_errno(self) -> None:
        fd = self._open_root()
        with self.assertRaises(FileNotFoundError) as ctx:
            self.ops.openat(None, os.path.join(self.root, "missing"), os.O_RDONLY)
        self.assertEqual(ctx.exception.errno, errno.ENOENT)
        with self.assertRaises(FileNotFoundError) as ctx:
            self.ops.statat(fd, "missing", follow_symlinks=False)
        self.assertEqual(ctx.exception.errno, errno.ENOENT)

    def test_not_a_directory_preserves_errno(self) -> None:
        fd = self._open_root()
        leaf_fd = self.ops.openat(fd, "leaf", os.O_CREAT | os.O_WRONLY, 0o600)
        self.ops.close(leaf_fd)
        with self.assertRaises(NotADirectoryError) as ctx:
            self.ops.openat(fd, "leaf/child", os.O_RDONLY)
        self.assertEqual(ctx.exception.errno, errno.ENOTDIR)


if __name__ == "__main__":
    unittest.main()
