"""Phase 7 task 7.2 — runtime projection publication on L2 atomic no-clobber.

The runtime projection is a private per-launch TOML file published beneath
the explicit external project-state runtime root.  Phase 7 migrates its
publication to the shared L2 **atomic no-clobber** contract while keeping
DTO validation, canonical serialization, content identity, the ``0444`` mode
policy, the lifecycle handle, and the existing ``EffectiveConfigError``
diagnostics in the runtime domain.  Atomic publication makes no
parent-directory durability claim.

These tests pin: complete bytes and final mode before visibility, typed
collision mapping, raw non-collision failures, safe-path rejection,
interruption passthrough, lifecycle cleanup, and the absence of any
``fsync``/durability step.
"""
from __future__ import annotations

import base64
import errno
import hashlib
import io
import os
import stat
import tempfile
import unittest

from docker.versioning.effective import (
    EffectiveConfigError,
    Filesystem,
    create_runtime_projection,
    cleanup_runtime_projection,
    _generate_projection_path,
    _validate_safe_path,
)
from docker.versioning.model import (
    EffectivePiExtensionEntry,
    EffectiveRuntimeProjection,
)
from docker.versioning.rendering import _write_toml
from tests.transactions_test_support import InjectedOps, temporary_entries

_RAW = hashlib.sha512(b"runtime-projection-payload").digest()
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


def _expected_bytes(projection: EffectiveRuntimeProjection) -> bytes:
    # Canonical serialization is the domain's TOML writer; publication must
    # expose exactly those bytes with the final mode before visibility.
    data: dict[str, object] = {
        "extensions": {
            name: {
                "package": ext.package,
                "version": ext.version,
                "artifact_id": ext.artifact_id,
                "integrity": ext.integrity,
                "metadata_file": ext.metadata_file,
            }
            for name, ext in sorted(projection.extensions.items())
        },
    }
    buf = io.StringIO()
    _write_toml(buf, data)
    if not projection.extensions:
        buf.write("[extensions]\n")
    return buf.getvalue().encode("utf-8")


class _RuntimeProjectionCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.boundary = os.path.join(self.root, "runtime")
        os.makedirs(self.boundary, mode=0o700)
        self.projection = _projection()
        self.ops = InjectedOps()
        self.fs = Filesystem(runtime_root=self.boundary, ops=self.ops)

    def _target(self, name: str = "projection.toml") -> str:
        return os.path.join(self.boundary, name)

    def _leftovers(self) -> list[str]:
        return temporary_entries(self.boundary)


class _ComponentOpenFailOps(InjectedOps):
    """Fail ``openat`` only when opening the named path component."""

    def __init__(self, component: str, error: BaseException) -> None:
        super().__init__()
        self._component = component
        self._error = error

    def openat(self, dir_fd, name, flags, mode=0o777):
        if name == self._component:
            self._record("openat", dir_fd, name, flags, mode)
            raise self._error
        return super().openat(dir_fd, name, flags, mode)


class AtomicPublicationTests(_RuntimeProjectionCase):
    def test_publishes_complete_bytes_with_final_mode_before_visibility(self) -> None:
        target = self._target()
        content = _expected_bytes(self.projection)
        observed_modes: list[int] = []
        real_linkat = self.ops.linkat

        def _record_mode(src_dir_fd, src, dst_dir_fd, dst, **kwargs):
            info = os.stat(src, dir_fd=src_dir_fd, follow_symlinks=False)
            observed_modes.append(stat.S_IMODE(info.st_mode))
            return real_linkat(src_dir_fd, src, dst_dir_fd, dst, **kwargs)

        self.ops.linkat = _record_mode  # type: ignore[method-assign]
        handle = create_runtime_projection(
            self.projection, host_path=target, _fs=self.fs,
        )
        self.assertEqual(handle.path, target)
        with open(target, "rb") as stream:
            self.assertEqual(stream.read(), content)
        self.assertEqual(stat.S_IMODE(os.lstat(target).st_mode), 0o444)
        # The private sibling already carried the final mode when committed.
        self.assertEqual(observed_modes, [0o444])
        self.assertEqual(self._leftovers(), [])

    def test_commits_with_one_atomic_link_not_rename(self) -> None:
        create_runtime_projection(
            self.projection, host_path=self._target(), _fs=self.fs,
        )
        self.assertEqual(self.ops.counts.get("linkat"), 1)
        self.assertNotIn("renameat", self.ops.counts)

    def test_makes_no_parent_directory_durability_claim(self) -> None:
        create_runtime_projection(
            self.projection, host_path=self._target(), _fs=self.fs,
        )
        self.assertNotIn("fsync", self.ops.counts)

    def test_content_hash_matches_published_bytes(self) -> None:
        target = self._target()
        handle = create_runtime_projection(
            self.projection, host_path=target, _fs=self.fs,
        )
        with open(target, "rb") as stream:
            actual = hashlib.sha256(stream.read()).hexdigest()
        self.assertEqual(handle.content_hash, actual)


class CollisionTests(_RuntimeProjectionCase):
    def test_typed_collision_maps_to_existing_effective_config_error(self) -> None:
        target = self._target()
        with open(target, "wb") as stream:
            stream.write(b"prior-launch")
        with self.assertRaises(EffectiveConfigError) as ctx:
            create_runtime_projection(
                self.projection, host_path=target, _fs=self.fs,
            )
        message = str(ctx.exception)
        self.assertIn(f"runtime projection {target!r} already exists", message)
        self.assertIn("refusing to overwrite another launch's projection", message)
        with open(target, "rb") as stream:
            self.assertEqual(stream.read(), b"prior-launch")
        self.assertEqual(self._leftovers(), [])

    def test_collision_preserves_raw_cause_chain(self) -> None:
        from docker.transactions.errors import DestinationExists

        target = self._target()
        with open(target, "wb") as stream:
            stream.write(b"prior")
        with self.assertRaises(EffectiveConfigError) as ctx:
            create_runtime_projection(
                self.projection, host_path=target, _fs=self.fs,
            )
        self.assertIsInstance(ctx.exception.__cause__, DestinationExists)


class OperationalFailureTests(_RuntimeProjectionCase):
    def test_write_failure_reraises_raw_oserror(self) -> None:
        target = self._target()
        error = OSError(errno.EIO, "injected runtime projection write")
        self.ops.failures["write"] = error
        with self.assertRaises(OSError) as ctx:
            create_runtime_projection(
                self.projection, host_path=target, _fs=self.fs,
            )
        self.assertIs(ctx.exception, error)
        self.assertEqual(ctx.exception.errno, errno.EIO)
        self.assertFalse(os.path.exists(target))
        self.assertEqual(self._leftovers(), [])

    def test_link_failure_reraises_raw_oserror(self) -> None:
        target = self._target()
        error = OSError(errno.EIO, "injected runtime projection link")
        self.ops.failures["linkat"] = error
        with self.assertRaises(OSError) as ctx:
            create_runtime_projection(
                self.projection, host_path=target, _fs=self.fs,
            )
        self.assertIs(ctx.exception, error)
        self.assertFalse(os.path.exists(target))
        self.assertEqual(self._leftovers(), [])

    def test_parent_open_failure_reraises_raw_oserror(self) -> None:
        error = PermissionError(errno.EACCES, "injected parent open")
        ops = _ComponentOpenFailOps(os.path.basename(self.boundary), error)
        fs = Filesystem(runtime_root=self.boundary, ops=ops)
        target = self._target("open-fail.toml")
        with self.assertRaises(OSError) as ctx:
            create_runtime_projection(
                self.projection, host_path=target, _fs=fs,
            )
        self.assertIs(ctx.exception, error)
        self.assertIs(type(ctx.exception), PermissionError)
        self.assertEqual(ctx.exception.errno, errno.EACCES)
        self.assertFalse(os.path.exists(target))
        self.assertEqual(self._leftovers(), [])

    def test_parent_stat_failure_reraises_raw_oserror(self) -> None:
        target = self._target()
        error = OSError(errno.EIO, "injected parent stat")
        self.ops.failures["fstat"] = error
        with self.assertRaises(OSError) as ctx:
            create_runtime_projection(
                self.projection, host_path=target, _fs=self.fs,
            )
        self.assertIs(ctx.exception, error)
        self.assertEqual(ctx.exception.errno, errno.EIO)
        self.assertFalse(os.path.exists(target))
        self.assertEqual(self._leftovers(), [])

    def test_interruption_passes_through_unchanged(self) -> None:
        target = self._target()
        interrupt = KeyboardInterrupt()
        self.ops.failures["linkat"] = interrupt
        with self.assertRaises(KeyboardInterrupt) as ctx:
            create_runtime_projection(
                self.projection, host_path=target, _fs=self.fs,
            )
        self.assertIs(ctx.exception, interrupt)
        self.assertFalse(os.path.exists(target))
        self.assertEqual(self._leftovers(), [])


class SafePathTests(_RuntimeProjectionCase):
    def test_symlink_destination_is_rejected(self) -> None:
        real = self._target("real.toml")
        with open(real, "wb") as stream:
            stream.write(b"real")
        link = self._target("link.toml")
        os.symlink(real, link)
        with self.assertRaises(EffectiveConfigError) as ctx:
            create_runtime_projection(
                self.projection, host_path=link, _fs=self.fs,
            )
        self.assertIn("symlink", str(ctx.exception))
        with open(real, "rb") as stream:
            self.assertEqual(stream.read(), b"real")

    def test_path_outside_runtime_root_is_rejected(self) -> None:
        outside = tempfile.NamedTemporaryFile(suffix=".toml", delete=False)
        outside.close()
        self.addCleanup(os.unlink, outside.name)
        with self.assertRaises(EffectiveConfigError) as ctx:
            create_runtime_projection(
                self.projection, host_path=outside.name, _fs=self.fs,
            )
        self.assertIn("outside", str(ctx.exception))

    def test_symlinked_intermediate_parent_is_rejected(self) -> None:
        # The symlink target stays inside the runtime root, so the
        # ``realpath`` containment check alone would permit it.  The symlink
        # is an intermediate component (a real subdirectory follows it), so
        # opening the parent by its full pathname would follow the link; the
        # secure descriptor walk must still reject it.
        real_parent = self._target("real-parent")
        real_sub = os.path.join(real_parent, "real-sub")
        os.makedirs(real_sub, mode=0o700)
        link_parent = self._target("link-parent")
        os.symlink(real_parent, link_parent)
        target = os.path.join(link_parent, "real-sub", "projection.toml")
        real_target = os.path.join(real_sub, "projection.toml")
        with self.assertRaises(OSError):
            create_runtime_projection(
                self.projection, host_path=target, _fs=self.fs,
            )
        self.assertFalse(os.path.exists(real_target))
        self.assertFalse(os.path.exists(target))
        self.assertEqual(temporary_entries(real_sub), [])
        self.assertEqual(temporary_entries(real_parent), [])
        self.assertEqual(temporary_entries(self.boundary), [])


class LifecycleTests(_RuntimeProjectionCase):
    def test_context_manager_removes_published_file(self) -> None:
        target = self._target()
        with create_runtime_projection(
            self.projection, host_path=target, _fs=self.fs,
        ) as handle:
            self.assertTrue(os.path.isfile(handle.path))
        self.assertFalse(os.path.exists(target))

    def test_discard_keeps_published_file(self) -> None:
        target = self._target()
        with create_runtime_projection(
            self.projection, host_path=target, _fs=self.fs,
        ) as handle:
            handle.discard()
        self.assertTrue(os.path.isfile(target))
        cleanup_runtime_projection(target, _fs=self.fs)
        self.assertFalse(os.path.exists(target))

    def test_default_path_is_generated_beneath_runtime_root(self) -> None:
        path = _generate_projection_path(self.fs)
        self.assertTrue(path.startswith(self.boundary + os.sep))
        self.assertFalse(os.path.exists(path))

    def test_safe_path_helper_still_validates(self) -> None:
        safe = self._target("safe.toml")
        self.assertEqual(_validate_safe_path(safe, self.fs), os.path.realpath(safe))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
