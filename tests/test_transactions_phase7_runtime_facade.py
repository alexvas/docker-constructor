"""Phase 7 task 7.3 — the runtime ``Filesystem`` compatibility/domain facade.

The existing runtime-projection ``Filesystem`` stays a domain facade over
the shared L0 backend.  It retains:

* path generation and runtime-root validation,
* lifecycle ownership (the ``RuntimeProjectionHandle``),
* the existing injection seams (constructor keyword arguments and the new
  descriptor-relative ``ops`` seam),

while secure publication delegates to descriptor-relative L0 mechanics
(``openat``/``fchmod``/``write``/``linkat``/``unlinkat``) without
reconstructing a pathname from an open descriptor.

These tests pin both the preserved facade surface and the descriptor-based
publication boundary.
"""
from __future__ import annotations

import base64
import hashlib
import os
import stat
import tempfile
import tomllib
import unittest

from docker.versioning.effective import (
    EffectiveConfigError,
    Filesystem,
    RuntimeProjectionHandle,
    _generate_projection_path,
    _runtime_root,
    _validate_safe_path,
    cleanup_runtime_projection,
    create_runtime_projection,
)
from docker.versioning.model import (
    EffectivePiExtensionEntry,
    EffectiveRuntimeProjection,
)
from docker.transactions.posix import PosixFileOps
from tests.transactions_test_support import InjectedOps, temporary_entries

_RAW = hashlib.sha512(b"facade-payload").digest()
_INTEGRITY = "sha512-" + base64.b64encode(_RAW).decode()
_ARTIFACT_ID = "sha512/" + _INTEGRITY.split("-", 1)[1].replace("+", "-").replace("/", "_") + ".tgz"


def _projection() -> EffectiveRuntimeProjection:
    return EffectiveRuntimeProjection(
        extensions={
            "pi-read": EffectivePiExtensionEntry(
                package="@example/pi-read",
                version="1.2.3",
                artifact_id=_ARTIFACT_ID,
                integrity=_INTEGRITY,
                metadata_file="package.json",
            ),
        },
    )


class _FacadeCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.boundary = os.path.join(self.root, "runtime")
        os.makedirs(self.boundary, mode=0o700)
        self.projection = _projection()


class PreservedSurfaceTests(_FacadeCase):
    def test_legacy_constructor_seams_are_retained(self) -> None:
        sentinel = object()
        fs = Filesystem(runtime_root=self.boundary)
        for name in (
            "open", "unlink", "link", "fsync", "mkstemp", "chmod",
            "urandom", "path", "makedirs", "close_fd", "runtime_root",
        ):
            self.assertTrue(hasattr(fs, name), f"Filesystem lost seam {name!r}")
        fs.unlink = sentinel
        self.assertIs(fs.unlink, sentinel)

    def test_default_and_explicit_l0_seam(self) -> None:
        default = Filesystem(runtime_root=self.boundary)
        self.assertIsInstance(default.ops, PosixFileOps)
        injected = InjectedOps()
        explicit = Filesystem(runtime_root=self.boundary, ops=injected)
        self.assertIs(explicit.ops, injected)

    def test_path_generation_uses_injected_seams(self) -> None:
        calls: list[bytes] = []
        joined: list[tuple[str, ...]] = []

        class _Path:
            @staticmethod
            def join(*args):
                joined.append(args)
                return os.path.join(*args)

        fs = Filesystem(
            runtime_root=self.boundary,
            urandom=lambda n: calls.append(n) or b"\x01" * n,
            path=_Path(),
            makedirs=lambda p, exist_ok=False: None,
        )
        path = _generate_projection_path(fs)
        self.assertEqual(calls, [16])
        self.assertEqual(len(joined[0]), 2)
        self.assertTrue(path.endswith(".toml"))

    def test_runtime_root_validation_is_retained(self) -> None:
        with self.assertRaises(EffectiveConfigError) as ctx:
            _runtime_root(Filesystem(runtime_root=None))
        self.assertIn("explicit external project-state runtime root", str(ctx.exception))
        with self.assertRaises(EffectiveConfigError):
            _runtime_root(Filesystem(runtime_root="relative/path"))

    def test_safe_path_validation_uses_injected_path_module(self) -> None:
        calls: list[str] = []

        class _Path:
            @staticmethod
            def realpath(p):
                calls.append(p)
                return os.path.realpath(p)

            @staticmethod
            def commonpath(paths):
                return os.path.commonpath(paths)

            @staticmethod
            def islink(p):
                return os.path.islink(p)

        fs = Filesystem(
            runtime_root=self.boundary,
            path=_Path(),
            makedirs=lambda p, exist_ok=False: None,
        )
        target = os.path.join(self.boundary, "safe.toml")
        self.assertEqual(_validate_safe_path(target, fs), os.path.realpath(target))
        self.assertIn(target, calls)


class LifecycleOwnershipTests(_FacadeCase):
    def test_handle_cleanup_uses_injected_filesystem_unlink(self) -> None:
        target = os.path.join(self.boundary, "owned.toml")
        calls: list[str] = []

        def _tracked_unlink(path: str) -> None:
            calls.append(path)
            os.unlink(path)

        fs = Filesystem(
            runtime_root=self.boundary,
            ops=InjectedOps(),
            unlink=_tracked_unlink,
        )
        handle = create_runtime_projection(
            self.projection, host_path=target, _fs=fs,
        )
        with handle:
            self.assertTrue(os.path.isfile(target))
        self.assertIn(target, calls)
        self.assertFalse(os.path.exists(target))

    def test_handle_discard_keeps_ownership(self) -> None:
        target = os.path.join(self.boundary, "keep.toml")
        fs = Filesystem(runtime_root=self.boundary, ops=InjectedOps())
        handle = create_runtime_projection(
            self.projection, host_path=target, _fs=fs,
        )
        self.assertIsInstance(handle, RuntimeProjectionHandle)
        handle.discard()
        self.assertTrue(os.path.isfile(target))
        cleanup_runtime_projection(target, _fs=fs)
        self.assertFalse(os.path.exists(target))


class DescriptorRelativePublicationTests(_FacadeCase):
    def test_publication_uses_descriptor_relative_l0(self) -> None:
        ops = InjectedOps()
        fs = Filesystem(runtime_root=self.boundary, ops=ops)
        target = os.path.join(self.boundary, "descriptor.toml")
        create_runtime_projection(self.projection, host_path=target, _fs=fs)

        link_calls = [args for name, args in ops.calls if name == "linkat"]
        self.assertEqual(len(link_calls), 1)
        src_dir_fd, src_name, dst_dir_fd, dst_name = link_calls[0]
        # The commit is one atomic link of a private sibling within the same
        # retained directory descriptor; no pathname is reconstructed.
        self.assertEqual(src_dir_fd, dst_dir_fd)
        self.assertTrue(src_name.startswith(".transaction-"))
        self.assertEqual(dst_name, "descriptor.toml")
        self.assertNotIn("/", src_name)
        self.assertNotIn("/", dst_name)
        self.assertNotEqual(src_name, dst_name)
        # The legacy path-oriented publication seams are not used.
        self.assertEqual(ops.counts.get("linkat"), 1)
        self.assertNotIn("renameat", ops.counts)
        self.assertEqual(temporary_entries(self.boundary), [])
        with open(target, "rb") as stream:
            parsed = tomllib.load(stream)
        self.assertIn("extensions", parsed)
        self.assertEqual(stat.S_IMODE(os.lstat(target).st_mode), 0o444)

    def test_parent_walk_is_descriptor_relative_across_components(self) -> None:
        ops = InjectedOps()
        fs = Filesystem(runtime_root=self.boundary, ops=ops)
        target = os.path.join(self.boundary, "parent.toml")
        create_runtime_projection(self.projection, host_path=target, _fs=fs)

        opens = [args for name, args in ops.calls if name == "openat"]
        # The secure walk anchors at the filesystem root and then opens exactly
        # one single-component name per element, each relative to the
        # previously opened descriptor.
        self.assertEqual(opens[0][0], None)
        self.assertEqual(opens[0][1], os.sep)
        walked: list[str] = []
        for dir_fd, name, *_ in opens[1:]:
            self.assertIsNotNone(
                dir_fd, "every path component must be opened via dir_fd",
            )
            self.assertNotIn(
                os.sep, name, "a component name must never be a path",
            )
            walked.append(name)
            if name == os.path.basename(self.boundary):
                break
        self.assertEqual(
            walked, [part for part in self.boundary.split(os.sep) if part],
        )

    def test_no_descriptor_path_is_reconstructed(self) -> None:
        ops = InjectedOps()
        fs = Filesystem(runtime_root=self.boundary, ops=ops)
        target = os.path.join(self.boundary, "reconstructed.toml")
        create_runtime_projection(self.projection, host_path=target, _fs=fs)
        # No absolute host path is ever handed back to ``openat`` after the
        # root anchor; every later open is relative to a live descriptor.
        for dir_fd, name, *_ in (args for n, args in ops.calls if n == "openat"):
            if dir_fd is None:
                self.assertEqual(name, os.sep)
            else:
                self.assertNotIn(os.sep, name)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
