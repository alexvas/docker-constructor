"""Phase 6 tasks 6.2–6.3 — effective build projection and user output.

Task 6.2: the effective build projection is published through the shared L2
durable-replacement contract through a rendering-domain adapter.  Validation,
TOML serialization, destination identity, and ``EffectiveInventoryOutputError``
diagnostics stay in the rendering domain; unusual operational failures remain
observable as their raw ``OSError`` with chaining, and interruptions pass
through unchanged.

Task 6.3: ``write_effective_inventory`` is a user-directed atomic output.  It
keeps its sibling-temporary-file plus ``os.replace`` mechanics and is not
migrated to the shared L2 durable contract.
"""
from __future__ import annotations

import errno
import os
import stat
import tempfile
import tomllib
import types
import unittest
from pathlib import Path
from unittest import mock

from docker.transactions.regular import RegularFileContracts
from docker.transactions.errors import STAGE_CLOSE, STAGE_VALIDATE, TransactionError
from docker.versioning import rendering as rendering_module
from docker.versioning import project_state as project_state_module
from docker.versioning.effective import apply_overrides, resolve_build_projection
from docker.versioning.inventory import load_inventory
from docker.versioning.rendering import (
    EffectiveInventoryOutputError,
    write_effective_build,
    write_effective_inventory,
)
from docker.versioning.project_state import resolve_project_state
from tests.transactions_test_support import InjectedOps, temporary_entries

_REPO = Path(__file__).resolve().parents[1]
_INVENTORY = _REPO / "docker-constructor.toml"
_DESTINATION = "docker-constructor.build.effective.toml"


class _DualFaultOps(InjectedOps):
    """Fail the destination validation ``fstat`` and that same close.

    The generated-directory validation and every other descriptor keep
    working; only the retained no-follow destination descriptor is faulted, so
    the test isolates the ``_validate_effective_destination`` cleanup path.
    """

    def __init__(self, destination_name: str) -> None:
        super().__init__()
        self._destination_name = destination_name
        self._destination_fd: int | None = None
        self.fstat_primary = OSError(errno.EIO, "injected destination fstat")
        self.close_secondary = OSError(errno.EIO, "injected destination close")

    def openat(self, dir_fd, name, flags, mode=0o777):
        fd = super().openat(dir_fd, name, flags, mode)
        if name == self._destination_name:
            self._destination_fd = fd
        return fd

    def fstat(self, fd):
        if fd == self._destination_fd:
            self._record("fstat", fd)
            raise self.fstat_primary
        return super().fstat(fd)

    def close(self, fd):
        if fd == self._destination_fd:
            self._record("close", fd)
            raise self.close_secondary
        return super().close(fd)


class _GeneratedCloseOps(InjectedOps):
    """Fail closing the retained generated-directory descriptor.

    Every other descriptor behaves normally, so publication and its L2
    cleanup can succeed or fault independently of the generated-directory
    close under test.
    """

    def __init__(self, close_error: BaseException | None = None) -> None:
        super().__init__()
        self._generated_fd: int | None = None
        self.generated_raw_closes = 0
        self.close_error: BaseException = (
            close_error
            if close_error is not None
            else OSError(errno.EIO, "injected generated close")
        )

    def openat(self, dir_fd, name, flags, mode=0o777):
        fd = super().openat(dir_fd, name, flags, mode)
        if name == "generated":
            self._generated_fd = fd
        return fd

    def close(self, fd):
        if fd == self._generated_fd:
            self.generated_raw_closes += 1
            self._record("close", fd)
            raise self.close_error
        return super().close(fd)


class _GeneratedFstatOps(InjectedOps):
    """Fail the second ``fstat`` of the generated descriptor only.

    The explicit pre-adoption ``fstat`` succeeds, so the fault isolates the
    ``DirectoryCapability.from_fd()`` factory boundary; every other descriptor
    behaves normally and raw closes of the generated descriptor are counted.
    """

    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self._generated_fd: int | None = None
        self._generated_fstats = 0
        self.generated_raw_closes = 0
        self.fstat_error = error

    def openat(self, dir_fd, name, flags, mode=0o777):
        fd = super().openat(dir_fd, name, flags, mode)
        if name == "generated":
            self._generated_fd = fd
        return fd

    def fstat(self, fd):
        if fd == self._generated_fd:
            self._generated_fstats += 1
            if self._generated_fstats == 2:
                self._record("fstat", fd)
                raise self.fstat_error
        return super().fstat(fd)

    def close(self, fd):
        if fd == self._generated_fd:
            self.generated_raw_closes += 1
        return super().close(fd)


class _GeneratedTrackingOps(InjectedOps):
    """Count raw closes of the generated descriptor without a fault."""

    def __init__(self) -> None:
        super().__init__()
        self._generated_fd: int | None = None
        self.generated_raw_closes = 0

    def openat(self, dir_fd, name, flags, mode=0o777):
        fd = super().openat(dir_fd, name, flags, mode)
        if name == "generated":
            self._generated_fd = fd
        return fd

    def close(self, fd):
        if fd == self._generated_fd:
            self.generated_raw_closes += 1
        return super().close(fd)


class _DestinationOpenOps(InjectedOps):
    """Fail the no-follow destination ``openat`` with an operational error.

    Every other descriptor (the generated directory, metadata, and the L2
    temporary sibling) opens normally, so the test isolates the unexpected
    operational failure of the destination probe.
    """

    def __init__(self, destination_name: str, error: BaseException) -> None:
        super().__init__()
        self._destination_name = destination_name
        self.open_error = error

    def openat(self, dir_fd, name, flags, mode=0o777):
        if name == self._destination_name:
            raise self.open_error
        return super().openat(dir_fd, name, flags, mode)


class _DestinationCloseOps(InjectedOps):
    """Fail closing only the retained destination descriptor.

    Validation reads the real on-disk stat, so a real unsafe destination (for
    example a permissive mode) produces the primary diagnostic while the close
    failure is injected independently.
    """

    def __init__(self, destination_name: str, close_error: BaseException) -> None:
        super().__init__()
        self._destination_name = destination_name
        self._destination_fd: int | None = None
        self.close_error = close_error

    def openat(self, dir_fd, name, flags, mode=0o777):
        fd = super().openat(dir_fd, name, flags, mode)
        if name == self._destination_name:
            self._destination_fd = fd
        return fd

    def close(self, fd):
        if fd == self._destination_fd:
            self._record("close", fd)
            raise self.close_error
        return super().close(fd)


class _ProjectionCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "constructor"
        self.root.mkdir()
        self.cache = self.base / "cache"
        self.cache.mkdir(mode=0o700)
        self.state = resolve_project_state(self.root, cache_root=self.cache)
        self.projection = resolve_build_projection(
            load_inventory(_INVENTORY).build, {}
        )
        self.ops = InjectedOps()
        patcher = mock.patch.object(
            rendering_module, "PosixFileOps", lambda: self.ops
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    @property
    def dest(self) -> Path:
        return self.state.generated_root / _DESTINATION

    def _write_prior(self) -> None:
        self.dest.write_bytes(b"prior")
        os.chmod(self.dest, 0o600)

    def _leftovers(self) -> list[str]:
        return temporary_entries(str(self.state.generated_root))


class EffectiveProjectionMigrationTests(_ProjectionCase):
    def test_routes_through_l2_durable_replace(self) -> None:
        self._write_prior()
        records: list[tuple[str, bytes, int, str]] = []

        class _Spy(RegularFileContracts):
            def durable_replace(self, directory, name, data, mode):
                records.append((name, bytes(data), mode, directory.label))
                return super().durable_replace(directory, name, data, mode)

        with mock.patch.object(rendering_module, "RegularFileContracts", _Spy):
            write_effective_build(
                self.projection, repo_root=self.root, project_state=self.state
            )
        self.assertEqual(len(records), 1)
        name, data, mode, label = records[0]
        self.assertEqual(name, _DESTINATION)
        self.assertEqual(mode, 0o600)
        self.assertEqual(label, "generated")
        parsed = tomllib.loads(data.decode("utf-8"))
        self.assertEqual(parsed["platform"], self.projection.platform)
        self.assertEqual(parsed["pi"]["version"], self.projection.pi_version)

    def test_publication_writes_valid_toml_with_private_mode(self) -> None:
        self._write_prior()
        result = write_effective_build(
            self.projection, repo_root=self.root, project_state=self.state
        )
        self.assertEqual(result, self.dest)
        self.assertNotEqual(self.dest.read_bytes(), b"prior")
        parsed = tomllib.loads(self.dest.read_text(encoding="utf-8"))
        for section in ("platform", "node", "rust", "uv", "python", "ty",
                        "rtk", "fd", "pi", "openspec", "oh-my-zsh"):
            self.assertIn(section, parsed)
        self.assertEqual(stat.S_IMODE(self.dest.stat().st_mode), 0o600)
        self.assertEqual(self._leftovers(), [])
        self.assertFalse((self.root / ".docker-generated").exists())

    def test_file_flush_precedes_replacement_and_parent_flush_precedes_success(
        self,
    ) -> None:
        self._write_prior()
        write_effective_build(
            self.projection, repo_root=self.root, project_state=self.state
        )
        order = self.ops.order
        self.assertEqual(order.count("fsync"), 2)
        first_fsync = order.index("fsync")
        rename_index = order.index("renameat")
        second_fsync = len(order) - 1 - order[::-1].index("fsync")
        self.assertLess(first_fsync, rename_index)
        self.assertLess(rename_index, second_fsync)
        rename_dir_fd = self.ops.arg_pairs("renameat")[0][0]
        fsync_fds = [args[0] for args in self.ops.arg_pairs("fsync")]
        self.assertNotEqual(fsync_fds[0], rename_dir_fd)
        self.assertEqual(fsync_fds[1], rename_dir_fd)

    def test_missing_destination_is_created_safely(self) -> None:
        self.assertFalse(self.dest.exists())
        write_effective_build(
            self.projection, repo_root=self.root, project_state=self.state
        )
        self.assertEqual(stat.S_IMODE(self.dest.stat().st_mode), 0o600)
        self.assertEqual(self._leftovers(), [])

    def test_generated_symlink_swap_fails_closed(self) -> None:
        outside = self.base / "outside"
        outside.mkdir()
        real = self.state.generated_root
        real.rename(real.with_name("generated-real"))
        real.symlink_to(outside, target_is_directory=True)
        with self.assertRaises((EffectiveInventoryOutputError, OSError)):
            write_effective_build(
                self.projection, repo_root=self.root, project_state=self.state
            )
        self.assertEqual(list(outside.iterdir()), [])

    def test_permissive_generated_directory_is_rejected(self) -> None:
        os.chmod(self.state.generated_root, 0o755)
        self.addCleanup(os.chmod, self.state.generated_root, 0o700)
        with self.assertRaises(EffectiveInventoryOutputError):
            write_effective_build(
                self.projection, repo_root=self.root, project_state=self.state
            )
        self.assertEqual(stat.S_IMODE(self.state.generated_root.stat().st_mode), 0o755)

    def test_symlink_destination_is_rejected_without_repair(self) -> None:
        outside = self.base / "outside-destination"
        outside.write_text("target")
        self.dest.symlink_to(outside)
        with self.assertRaises(EffectiveInventoryOutputError):
            write_effective_build(
                self.projection, repo_root=self.root, project_state=self.state
            )
        self.assertEqual(outside.read_text(), "target")
        self.assertTrue(self.dest.is_symlink())

    def test_non_regular_destination_is_rejected(self) -> None:
        self.dest.mkdir()
        with self.assertRaises(EffectiveInventoryOutputError):
            write_effective_build(
                self.projection, repo_root=self.root, project_state=self.state
            )
        self.assertTrue(self.dest.is_dir())

    def test_permissive_destination_is_rejected_without_repair(self) -> None:
        self.dest.write_bytes(b"prior")
        os.chmod(self.dest, 0o644)
        with self.assertRaises(EffectiveInventoryOutputError) as ctx:
            write_effective_build(
                self.projection, repo_root=self.root, project_state=self.state
            )
        self.assertIn("expected 0o600", str(ctx.exception))
        self.assertEqual(stat.S_IMODE(self.dest.stat().st_mode), 0o644)
        self.assertEqual(self.dest.read_bytes(), b"prior")

    def test_multiply_linked_destination_is_rejected(self) -> None:
        self._write_prior()
        os.link(self.dest, self.state.generated_root / "linked")
        with self.assertRaises(EffectiveInventoryOutputError):
            write_effective_build(
                self.projection, repo_root=self.root, project_state=self.state
            )
        self.assertEqual(self.dest.read_bytes(), b"prior")

    def test_foreign_owned_destination_is_rejected(self) -> None:
        self._write_prior()
        target = os.path.realpath(str(self.dest))
        real_fstat = os.fstat

        def foreign(fd, *args, **kwargs):
            value = real_fstat(fd, *args, **kwargs)
            try:
                name = os.path.realpath(os.readlink(f"/proc/self/fd/{fd}"))
            except OSError:
                name = ""
            if name == target:
                return types.SimpleNamespace(
                    st_mode=value.st_mode,
                    st_uid=os.geteuid() + 1,
                    st_nlink=value.st_nlink,
                    st_gid=value.st_gid,
                )
            return value

        with mock.patch.object(rendering_module.os, "fstat", side_effect=foreign):
            with self.assertRaises(EffectiveInventoryOutputError):
                write_effective_build(
                    self.projection, repo_root=self.root, project_state=self.state
                )
        self.assertEqual(self.dest.read_bytes(), b"prior")

    def test_operational_failure_reraises_raw_oserror(self) -> None:
        self._write_prior()
        self.ops.failures["renameat"] = OSError(errno.EIO, "injected replacement")
        with self.assertRaises(OSError) as ctx:
            write_effective_build(
                self.projection, repo_root=self.root, project_state=self.state
            )
        self.assertEqual(ctx.exception.errno, errno.EIO)
        self.assertEqual(self.dest.read_bytes(), b"prior")
        self.assertEqual(self._leftovers(), [])

    def test_interruption_passes_through_unchanged(self) -> None:
        self._write_prior()
        self.ops.failures["renameat"] = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            write_effective_build(
                self.projection, repo_root=self.root, project_state=self.state
            )
        self.assertEqual(self.dest.read_bytes(), b"prior")
        self.assertEqual(self._leftovers(), [])

    def test_publication_failure_survives_generated_close_failure(self) -> None:
        self._write_prior()
        self.ops = _GeneratedCloseOps()
        publication_error = OSError(errno.EIO, "injected publication")
        self.ops.failures["renameat"] = publication_error
        with self.assertRaises(OSError) as ctx:
            write_effective_build(
                self.projection, repo_root=self.root, project_state=self.state
            )
        primary = ctx.exception
        # The publication failure stays the raised primary exception.
        self.assertIs(primary, publication_error)
        self.assertEqual(primary.errno, errno.EIO)
        self.assertEqual(primary.strerror, "injected publication")
        # The generated-directory close failure is attached exactly once as a
        # typed close-stage secondary diagnostic carrying the raw descriptor
        # error as both ``cause`` and ``__cause__``.
        secondary = list(getattr(primary, "_transaction_secondary", []))
        close_secondaries = [
            exc
            for exc in secondary
            if isinstance(exc, TransactionError) and exc.stage == STAGE_CLOSE
        ]
        self.assertEqual(len(close_secondaries), 1)
        self.assertIs(close_secondaries[0].cause, self.ops.close_error)
        self.assertIs(close_secondaries[0].__cause__, self.ops.close_error)
        self.assertIsNot(primary, self.ops.close_error)
        self.assertEqual(self.dest.read_bytes(), b"prior")

    def test_generated_close_failure_propagates_when_publication_succeeds(
        self,
    ) -> None:
        self._write_prior()
        self.ops = _GeneratedCloseOps()
        with self.assertRaises(TransactionError) as ctx:
            write_effective_build(
                self.projection, repo_root=self.root, project_state=self.state
            )
        # No earlier error exists, so the typed close failure is the primary
        # exception and its raw descriptor cause stays inspectable.
        failure = ctx.exception
        self.assertEqual(failure.stage, STAGE_CLOSE)
        self.assertIs(failure.cause, self.ops.close_error)
        self.assertIs(failure.__cause__, self.ops.close_error)
        self.assertEqual(failure.cause.errno, errno.EIO)
        # Publication completed before the close failure was reported.
        parsed = tomllib.loads(self.dest.read_text(encoding="utf-8"))
        self.assertEqual(parsed["platform"], self.projection.platform)
        self.assertEqual(self._leftovers(), [])

    def test_generated_close_interruption_propagates_unchanged(self) -> None:
        self._write_prior()
        self.ops = _GeneratedCloseOps(close_error=KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt) as ctx:
            write_effective_build(
                self.projection, repo_root=self.root, project_state=self.state
            )
        # The interruption is neither attached as secondary nor converted.
        self.assertIs(ctx.exception, self.ops.close_error)
        self.assertEqual(
            list(getattr(ctx.exception, "_transaction_secondary", [])), []
        )
        parsed = tomllib.loads(self.dest.read_text(encoding="utf-8"))
        self.assertEqual(parsed["platform"], self.projection.platform)

    def test_destination_open_operational_failure_escapes_raw(self) -> None:
        self._write_prior()
        for error in (
            OSError(errno.EIO, "injected destination open EIO"),
            OSError(errno.EMFILE, "injected destination open EMFILE"),
        ):
            with self.subTest(errno=error.errno):
                self.ops = _DestinationOpenOps(_DESTINATION, error)
                with self.assertRaises(OSError) as ctx:
                    write_effective_build(
                        self.projection,
                        repo_root=self.root,
                        project_state=self.state,
                    )
                # The operational failure is not misreported as an unsafe
                # non-regular destination; the original exception escapes.
                self.assertIs(ctx.exception, error)
                self.assertEqual(ctx.exception.errno, error.errno)
                self.assertEqual(self.dest.read_bytes(), b"prior")
                self.assertEqual(stat.S_IMODE(self.dest.stat().st_mode), 0o600)
                self.assertEqual(self._leftovers(), [])

    def test_invalid_mode_survives_destination_close_failure(self) -> None:
        self.dest.write_bytes(b"prior")
        os.chmod(self.dest, 0o644)
        close_error = OSError(errno.EIO, "injected destination close")
        self.ops = _DestinationCloseOps(_DESTINATION, close_error)
        with self.assertRaises(EffectiveInventoryOutputError) as ctx:
            write_effective_build(
                self.projection, repo_root=self.root, project_state=self.state
            )
        primary = ctx.exception
        # The unsafe-mode diagnostic is the primary failure; the close failure
        # is only a secondary diagnostic.
        self.assertIn("expected 0o600", str(primary))
        secondary = list(getattr(primary, "_transaction_secondary", []))
        self.assertEqual(
            sum(1 for exc in secondary if exc is close_error), 1
        )
        self.assertIsNot(primary, close_error)
        # The destination is neither repaired nor replaced.
        self.assertEqual(self.dest.read_bytes(), b"prior")
        self.assertEqual(stat.S_IMODE(self.dest.stat().st_mode), 0o644)
        self.assertEqual(self._leftovers(), [])

    def test_fstat_failure_survives_destination_close_failure(self) -> None:
        self._write_prior()
        self.ops = _DualFaultOps(_DESTINATION)
        with self.assertRaises(OSError) as ctx:
            write_effective_build(
                self.projection, repo_root=self.root, project_state=self.state
            )
        primary = ctx.exception
        # The fstat failure stays the raised primary exception.
        self.assertIs(primary, self.ops.fstat_primary)
        self.assertEqual(primary.errno, errno.EIO)
        self.assertEqual(primary.strerror, "injected destination fstat")
        # The close failure is recorded as secondary, not as the primary.
        secondary = list(getattr(primary, "_transaction_secondary", []))
        self.assertEqual(len(secondary), 1)
        self.assertIs(secondary[0], self.ops.close_secondary)
        self.assertIsNot(primary, self.ops.close_secondary)
        # No repair, no committed temporary, and the prior file is preserved.
        self.assertEqual(self.dest.read_bytes(), b"prior")
        self.assertEqual(self._leftovers(), [])

    def test_publication_failure_attaches_namespace_close_secondary(self) -> None:
        self._write_prior()
        publication_error = OSError(errno.EIO, "injected publication")
        namespace_close_error = OSError(errno.EIO, "injected namespace close")
        self.ops.failures["renameat"] = publication_error
        recorded: dict[str, int] = {}

        class _RecordingValidated(project_state_module.ValidatedProjectState):
            def __init__(self, state, namespace_fd):
                super().__init__(state, namespace_fd)
                recorded["fd"] = namespace_fd

        real_close = os.close

        def closing(fd):
            if fd == recorded.get("fd"):
                recorded["attempts"] = recorded.get("attempts", 0) + 1
                raise namespace_close_error
            return real_close(fd)

        with mock.patch.object(
            project_state_module, "ValidatedProjectState", _RecordingValidated
        ), mock.patch.object(
            project_state_module.os, "close", side_effect=closing
        ):
            with self.assertRaises(OSError) as ctx:
                write_effective_build(
                    self.projection, repo_root=self.root, project_state=self.state
                )
        primary = ctx.exception
        # The publication failure stays primary through the namespace close.
        self.assertIs(primary, publication_error)
        self.assertEqual(primary.errno, errno.EIO)
        self.assertEqual(primary.strerror, "injected publication")
        secondary = list(getattr(primary, "_transaction_secondary", []))
        self.assertEqual(
            sum(1 for exc in secondary if exc is namespace_close_error), 1
        )
        self.assertIsNot(primary, namespace_close_error)
        self.assertEqual(recorded.get("attempts"), 1)
        self.assertEqual(self.dest.read_bytes(), b"prior")

    def test_missing_generated_child_is_effective_output_error(self) -> None:
        self.state.generated_root.rmdir()
        with self.assertRaises(EffectiveInventoryOutputError):
            write_effective_build(
                self.projection, repo_root=self.root, project_state=self.state
            )


class GeneratedAdoptionOwnershipTests(_ProjectionCase):
    """Follow-up 5.7/5.17/5.18: the effective-build generated-directory
    factory and close boundary.

    Once ``DirectoryCapability.from_fd()`` succeeds the retained descriptor is
    owned by the capability and must be released through it; a failed
    adoption leaves the raw descriptor caller-owned and closes it exactly
    once.  A typed factory failure is translated back to its raw ``OSError``
    cause for the pre-Phase-5 public contract, while a close-stage failure is
    classified as an ordinary close and keeps the raw descriptor error as its
    cause.
    """

    def setUp(self) -> None:
        super().setUp()
        self.capability_closes: list[str] = []
        real_close = rendering_module.DirectoryCapability.close

        def recording_close(capability):
            self.capability_closes.append(capability.label)
            return real_close(capability)

        patcher = mock.patch.object(
            rendering_module.DirectoryCapability, "close", recording_close
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write(self):
        return write_effective_build(
            self.projection, repo_root=self.root, project_state=self.state
        )

    def test_from_fd_operational_failure_preserves_raw_oserror(self) -> None:
        raw = OSError(errno.EIO, "injected generated fstat")
        self.ops = _GeneratedFstatOps(raw)
        with self.assertRaises(OSError) as ctx:
            self._write()
        # The typed factory wrapper is translated back to the original raw
        # ``OSError`` object, preserving the pre-Phase-5 public behavior.
        self.assertIs(ctx.exception, raw)
        self.assertNotIsInstance(ctx.exception, TransactionError)
        self.assertEqual(ctx.exception.errno, errno.EIO)

    def test_secondary_diagnostics_survive_wrapper_translation(self) -> None:
        cause = OSError(errno.EIO, "injected generated fstat")
        secondary = OSError(errno.EIO, "injected close diagnostic")
        wrapper = TransactionError(
            STAGE_VALIDATE, "cannot stat directory", cause=cause
        )
        wrapper.add_secondary(secondary)
        with mock.patch.object(
            rendering_module.DirectoryCapability, "from_fd", side_effect=wrapper
        ):
            with self.assertRaises(OSError) as ctx:
                self._write()
        self.assertIs(ctx.exception, cause)
        # The wrapper's retained diagnostics are carried onto the raw cause.
        self.assertIn(
            secondary, list(getattr(cause, "_transaction_secondary", []))
        )

    def test_successful_adoption_closes_through_capability_exactly_once(self) -> None:
        self.ops = _GeneratedTrackingOps()
        self._write()
        # Ownership transferred on success, so release goes through the
        # capability exactly once and never as a second direct raw close.
        self.assertEqual(self.capability_closes.count("generated"), 1)
        self.assertEqual(self.ops.generated_raw_closes, 1)

    def test_failed_adoption_closes_raw_descriptor_exactly_once(self) -> None:
        raw = OSError(errno.EIO, "injected generated fstat")
        self.ops = _GeneratedFstatOps(raw)
        with self.assertRaises(OSError):
            self._write()
        # Adoption did not succeed, so the still caller-owned descriptor is
        # closed exactly once directly and never through a capability.
        self.assertEqual(self.ops.generated_raw_closes, 1)
        self.assertNotIn("generated", self.capability_closes)

    def test_close_failure_without_primary_is_typed_close(self) -> None:
        self.ops = _GeneratedCloseOps()
        with self.assertRaises(TransactionError) as ctx:
            self._write()
        failure = ctx.exception
        self.assertEqual(failure.stage, STAGE_CLOSE)
        self.assertIs(failure.cause, self.ops.close_error)
        self.assertIs(failure.__cause__, self.ops.close_error)
        self.assertEqual(self.capability_closes.count("generated"), 1)
        self.assertEqual(self.ops.generated_raw_closes, 1)

    def test_close_failure_with_active_primary_is_secondary(self) -> None:
        self.ops = _GeneratedCloseOps()
        publication_error = OSError(errno.EIO, "injected publication")
        self.ops.failures["renameat"] = publication_error
        with self.assertRaises(OSError) as ctx:
            self._write()
        primary = ctx.exception
        self.assertIs(primary, publication_error)
        secondary = list(getattr(primary, "_transaction_secondary", []))
        typed = [
            exc
            for exc in secondary
            if isinstance(exc, TransactionError) and exc.stage == STAGE_CLOSE
        ]
        self.assertEqual(len(typed), 1)
        self.assertIs(typed[0].cause, self.ops.close_error)
        self.assertIsNot(primary, self.ops.close_error)

    def test_process_control_during_adoption_is_unwrapped(self) -> None:
        for interruption in (KeyboardInterrupt(), SystemExit()):
            with self.subTest(interruption=type(interruption).__name__):
                self.ops = _GeneratedFstatOps(interruption)
                with self.assertRaises(type(interruption)) as ctx:
                    self._write()
                # The interruption is neither wrapped nor attached, and the
                # caller-owned descriptor is still released exactly once.
                self.assertIs(ctx.exception, interruption)
                self.assertEqual(
                    list(getattr(ctx.exception, "_transaction_secondary", [])),
                    [],
                )
                self.assertEqual(self.ops.generated_raw_closes, 1)


class UserDirectedInventoryOutputBoundaryTests(unittest.TestCase):
    """Task 6.3: the user-directed inventory output keeps its own mechanics."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.effective = apply_overrides(load_inventory(_INVENTORY), {})
        self.destination = self.root / "nested" / "effective-inventory.toml"

    def test_uses_sibling_temporary_file_and_replace(self) -> None:
        with mock.patch.object(
            rendering_module.os, "replace", wraps=os.replace
        ) as replace:
            write_effective_inventory(self.effective, self.destination)
        self.assertEqual(replace.call_count, 1)
        source, target = replace.call_args.args
        self.assertEqual(Path(source).parent, self.destination.parent)
        self.assertEqual(Path(target), self.destination)
        self.assertTrue(self.destination.is_file())
        self.assertEqual(list(self.destination.parent.glob(".versions-*")), [])

    def test_write_failure_cleans_temporary_and_preserves_destination(self) -> None:
        self.destination.parent.mkdir(parents=True)
        self.destination.write_text("prior")
        with mock.patch.object(
            rendering_module, "_write_toml", side_effect=OSError("injected")
        ):
            with self.assertRaises(OSError):
                write_effective_inventory(self.effective, self.destination)
        self.assertEqual(self.destination.read_text(), "prior")
        self.assertEqual(list(self.destination.parent.glob(".versions-*")), [])

    def test_not_migrated_to_shared_l2_durable_contracts(self) -> None:
        def forbidden(*args, **kwargs):  # pragma: no cover - must never run
            raise AssertionError("user output must not use the shared L2 contracts")

        with mock.patch.object(RegularFileContracts, "durable_replace", forbidden), \
                mock.patch.object(RegularFileContracts, "atomic_no_clobber", forbidden), \
                mock.patch.object(RegularFileContracts, "durable_no_clobber", forbidden):
            write_effective_inventory(self.effective, self.destination)
        self.assertTrue(self.destination.is_file())

    def test_does_not_claim_directory_durability(self) -> None:
        captured: list[int] = []
        real_fsync = rendering_module.os.fsync

        def recording(fd):
            captured.append(fd)
            return real_fsync(fd)

        with mock.patch.object(rendering_module.os, "fsync", side_effect=recording):
            write_effective_inventory(self.effective, self.destination)
        self.assertEqual(captured, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
