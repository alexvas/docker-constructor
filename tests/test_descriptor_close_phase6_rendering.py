"""Phase 6 (tasks 6.15 / 6.33, 6.17 / 6.35) — rendering descriptor ownership.

RED before migration: ``_validate_effective_destination`` released its
no-follow destination descriptor with ``ops.close`` inside a lambda;
``write_effective_build`` closed the not-yet-adopted generated-directory
descriptor with ``ops.close`` inside a lambda; and ``write_effective_inventory``
relied on implicit ``os.fdopen(fd, ..., closefd=True)`` ownership of the
``mkstemp()`` descriptor.  These tests require the migrated contract: each
descriptor is adopted into an ``OwnedDescriptor`` and every release delegates
to ``owner.close`` (terminal before the syscall, never retried) with the
existing precedence and completion guarantees.
"""
from __future__ import annotations

import errno
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from docker.transactions.capabilities import DirectoryCapability
from docker.transactions.posix import PosixFileOps
from docker.versioning import rendering as rendering_module
from docker.versioning.effective import apply_overrides
from docker.versioning.inventory import load_inventory
from docker.versioning.rendering import write_effective_build
from tests.descriptor_ownership_support import (
    file_object_faults,
    recording,
    secondary_diagnostics,
)
from tests.test_transactions_phase6_effective_projection import (
    _GeneratedFstatOps,
    _ProjectionCase,
)

_INVENTORY = Path(__file__).resolve().parents[1] / "docker-constructor.toml"
_DESTINATION = "docker-constructor.build.effective.toml"


class ValidateEffectiveDestinationOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="phase6-rendering-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.directory = DirectoryCapability.from_path(
            PosixFileOps(), self.root, label="generated"
        )
        self.addCleanup(self.directory.close)

    def test_existing_destination_releases_through_shared_owner(self) -> None:
        target = self.root / _DESTINATION
        target.write_bytes(b"prior")
        os.chmod(target, 0o600)
        with recording(rendering_module) as rec:
            rendering_module._validate_effective_destination(
                PosixFileOps(), self.directory, target.name
            )
        rec.assert_terminal_and_at_most_once(self)

    def test_absent_destination_constructs_no_owner(self) -> None:
        with recording(rendering_module) as rec:
            rendering_module._validate_effective_destination(
                PosixFileOps(), self.directory, "absent.toml"
            )
        self.assertEqual(rec.owners, [])

    def test_permissive_destination_precedence_over_close_failure(self) -> None:
        target = self.root / _DESTINATION
        target.write_bytes(b"prior")
        os.chmod(target, 0o644)
        close_error = OSError(errno.EIO, "injected destination close")
        with recording(
            rendering_module, close_fault=lambda fd, owner: close_error
        ) as rec:
            with self.assertRaises(
                rendering_module.EffectiveInventoryOutputError
            ) as ctx:
                rendering_module._validate_effective_destination(
                    PosixFileOps(), self.directory, target.name
                )
        self.assertIn("expected 0o600", str(ctx.exception))
        rec.assert_terminal_and_at_most_once(self)
        self.assertIn(close_error, secondary_diagnostics(ctx.exception))


class WriteEffectiveBuildOwnershipTests(_ProjectionCase):
    def test_missing_destination_success_detaches_generated_owner(self) -> None:
        with recording(rendering_module) as rec:
            write_effective_build(
                self.projection, repo_root=self.root, project_state=self.state
            )
        # The generated-directory owner is the only rendering-owned descriptor
        # and is transferred into the capability, never closed here.
        self.assertEqual(len(rec.owners), 1)
        self.assertFalse(rec.owners[0].released)
        self.assertEqual(rec.close_calls, [])

    def test_failed_adoption_releases_generated_owner(self) -> None:
        self.ops = _GeneratedFstatOps(OSError(errno.EIO, "injected generated fstat"))
        with recording(rendering_module) as rec:
            with self.assertRaises(
                rendering_module.EffectiveInventoryOutputError
            ):
                write_effective_build(
                    self.projection, repo_root=self.root, project_state=self.state
                )
        rec.assert_terminal_and_at_most_once(self)


class WriteEffectiveInventoryOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="phase6-rendering-inventory-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.effective = apply_overrides(load_inventory(_INVENTORY), {})
        self.destination = self.root / "nested" / "effective-inventory.toml"

    def test_success_releases_owner_before_publication(self) -> None:
        events: list[str] = []
        real_replace = os.replace

        def recording_replace(*args, **kwargs):
            events.append("replace")
            return real_replace(*args, **kwargs)

        def record_close(fd, owner):
            events.append("close")
            return None

        with recording(rendering_module, close_fault=record_close) as rec:
            with mock.patch.object(
                rendering_module.os, "replace", side_effect=recording_replace
            ):
                rendering_module.write_effective_inventory(
                    self.effective, self.destination
                )
        rec.assert_terminal_and_at_most_once(self)
        self.assertEqual(events, ["close", "replace"])

    def test_write_failure_keeps_primary_and_releases_owner(self) -> None:
        self.destination.parent.mkdir(parents=True)
        self.destination.write_text("prior")
        close_error = OSError(errno.EIO, "injected descriptor release failure")
        with recording(
            rendering_module, close_fault=lambda fd, owner: close_error
        ) as rec:
            with mock.patch.object(
                rendering_module, "_write_toml", side_effect=OSError("injected write")
            ):
                with self.assertRaises(OSError) as ctx:
                    rendering_module.write_effective_inventory(
                        self.effective, self.destination
                    )
        self.assertEqual(str(ctx.exception), "injected write")
        rec.assert_terminal_and_at_most_once(self)
        self.assertIn(close_error, secondary_diagnostics(ctx.exception))
        self.assertEqual(self.destination.read_text(), "prior")
        self.assertEqual(list(self.destination.parent.glob(".versions-*")), [])

    def test_write_failure_stays_primary_over_file_and_descriptor_close(self) -> None:
        self.destination.parent.mkdir(parents=True)
        self.destination.write_text("prior")
        write_error = OSError(errno.EIO, "injected write")
        file_close_error = OSError(errno.EIO, "injected file-object close")
        descriptor_error = OSError(errno.EIO, "injected descriptor release")
        with recording(
            rendering_module, close_fault=lambda fd, owner: descriptor_error
        ) as rec:
            with file_object_faults(
                rendering_module, close_fault=file_close_error
            ):
                with mock.patch.object(
                    rendering_module, "_write_toml", side_effect=write_error
                ):
                    with mock.patch.object(
                        rendering_module.os, "replace"
                    ) as replace:
                        with self.assertRaises(OSError) as ctx:
                            rendering_module.write_effective_inventory(
                                self.effective, self.destination
                            )
        self.assertIs(ctx.exception, write_error)
        secondary = secondary_diagnostics(ctx.exception)
        self.assertIn(file_close_error, secondary)
        self.assertIn(descriptor_error, secondary)
        replace.assert_not_called()
        rec.assert_terminal_and_at_most_once(self)
        self.assertEqual(self.destination.read_text(), "prior")
        self.assertEqual(list(self.destination.parent.glob(".versions-*")), [])

    def test_file_object_close_failure_stays_primary_over_descriptor_close(self) -> None:
        self.destination.parent.mkdir(parents=True)
        self.destination.write_text("prior")
        file_close_error = OSError(errno.EIO, "injected file-object close")
        descriptor_error = OSError(errno.EIO, "injected descriptor release")
        with recording(
            rendering_module, close_fault=lambda fd, owner: descriptor_error
        ) as rec:
            with file_object_faults(
                rendering_module, close_fault=file_close_error
            ):
                with mock.patch.object(
                    rendering_module.os, "replace"
                ) as replace:
                    with self.assertRaises(OSError) as ctx:
                        rendering_module.write_effective_inventory(
                            self.effective, self.destination
                        )
        self.assertIs(ctx.exception, file_close_error)
        self.assertIn(descriptor_error, secondary_diagnostics(ctx.exception))
        replace.assert_not_called()
        rec.assert_terminal_and_at_most_once(self)
        self.assertEqual(self.destination.read_text(), "prior")
        self.assertEqual(list(self.destination.parent.glob(".versions-*")), [])
