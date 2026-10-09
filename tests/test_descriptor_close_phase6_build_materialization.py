"""Phase 6 (tasks 6.16 / 6.34) — build-materialization descriptor ownership.

RED before migration: ``materialize_artifact`` created its temporary file with
``tempfile.mkstemp()`` and handed the raw descriptor to
``os.fdopen(fd, "wb")``, relying on implicit ``closefd=True`` ownership of the
file object.  There was no shared terminal owner, so the descriptor was not
tracked and could not demonstrate terminal-before-close or at-most-once
release.  These tests require the migrated contract: the ``mkstemp()`` fd is
adopted immediately into an ``OwnedDescriptor``, the file object is opened with
``closefd=False``, and the owner is released through shared precedence after
the file-object cleanup.
"""
from __future__ import annotations

import errno
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from docker.versioning import build_materialization as materialization_module
from docker.versioning.build_cache import build_blob_path
from docker.versioning.build_materialization import (
    MaterializationError,
    SelectedBuildArtifact,
    materialize_artifact,
)
from docker.versioning.digest_identity import DigestIdentity
from docker.versioning.project_state import resolve_project_state
from tests.descriptor_ownership_support import (
    file_object_faults,
    recording,
    secondary_diagnostics,
)
from tests.test_constructor_build_materialization import RecordingTransport


def _selected(payload: bytes = b"payload") -> SelectedBuildArtifact:
    return SelectedBuildArtifact(
        "uv",
        "https://example.invalid/artifact",
        DigestIdentity.from_hex("sha256", hashlib.sha256(payload).hexdigest()),
    )


class MaterializeArtifactOwnershipTests(unittest.TestCase):
    def _environment(self):
        tmp = tempfile.TemporaryDirectory(prefix="phase6-materialize-")
        self.addCleanup(tmp.cleanup)
        cache = Path(tmp.name) / "cache"
        cache.mkdir(mode=0o700)
        state = resolve_project_state(tmp.name, cache_root=cache)
        return tmp.name, cache, state

    def _destination(self, state):
        return build_blob_path(
            state.build_artifacts_root / "blobs", _selected().identity
        )

    def _temporaries(self, state) -> list[Path]:
        return list((state.build_artifacts_root / "tmp").iterdir())

    def test_streamed_miss_releases_owner_through_shared_lifecycle(self) -> None:
        root, cache, _ = self._environment()
        transport = RecordingTransport((b"pay", b"load"))
        with recording(materialization_module) as rec:
            path = materialize_artifact(
                _selected(), constructor_project_root=root,
                cache_root=cache, transport=transport,
            )
        rec.assert_terminal_and_at_most_once(self)
        self.assertEqual(path.read_bytes(), b"payload")

    def test_failed_stream_removes_temporary_and_releases_owner(self) -> None:
        root, cache, state = self._environment()
        transport = RecordingTransport((b"wrong",))
        with recording(materialization_module) as rec:
            with self.assertRaises(MaterializationError):
                materialize_artifact(
                    _selected(), constructor_project_root=root,
                    cache_root=cache, transport=transport,
                )
        rec.assert_terminal_and_at_most_once(self)
        self.assertEqual([], self._temporaries(state))

    def test_descriptor_release_failure_prevents_publication(self) -> None:
        root, cache, state = self._environment()
        transport = RecordingTransport((b"pay", b"load"))
        close_error = OSError(errno.EIO, "injected descriptor release failure")
        with recording(
            materialization_module, close_fault=lambda fd, owner: close_error
        ) as rec:
            with mock.patch.object(
                materialization_module.os, "replace"
            ) as replace:
                with self.assertRaises(MaterializationError) as ctx:
                    materialize_artifact(
                        _selected(), constructor_project_root=root,
                        cache_root=cache, transport=transport,
                    )
        # The release failure is mapped by the materialization boundary, not
        # escaped as a raw OSError, and it can never follow a publication.
        self.assertIs(ctx.exception.__cause__, close_error)
        replace.assert_not_called()
        rec.assert_terminal_and_at_most_once(self)
        self.assertEqual([], self._temporaries(state))
        self.assertFalse(self._destination(state).exists())

    def test_write_failure_stays_primary_over_file_and_descriptor_close(self) -> None:
        root, cache, state = self._environment()
        transport = RecordingTransport((b"pay", b"load"))
        write_error = OSError(errno.ENOSPC, "injected write")
        file_close_error = OSError(errno.EIO, "injected file-object close")
        descriptor_error = OSError(errno.EIO, "injected descriptor release")
        with recording(
            materialization_module, close_fault=lambda fd, owner: descriptor_error
        ) as rec:
            with file_object_faults(
                materialization_module,
                write_fault=write_error,
                close_fault=file_close_error,
            ):
                with mock.patch.object(
                    materialization_module.os, "replace"
                ) as replace:
                    with self.assertRaises(MaterializationError) as ctx:
                        materialize_artifact(
                            _selected(), constructor_project_root=root,
                            cache_root=cache, transport=transport,
                        )
        self.assertIs(ctx.exception.__cause__, write_error)
        secondary = secondary_diagnostics(ctx.exception)
        self.assertIn(file_close_error, secondary)
        self.assertIn(descriptor_error, secondary)
        replace.assert_not_called()
        rec.assert_terminal_and_at_most_once(self)
        self.assertEqual([], self._temporaries(state))
        self.assertFalse(self._destination(state).exists())

    def test_flush_failure_stays_primary_over_descriptor_close(self) -> None:
        root, cache, state = self._environment()
        transport = RecordingTransport((b"pay", b"load"))
        flush_error = OSError(errno.EIO, "injected flush")
        descriptor_error = OSError(errno.EIO, "injected descriptor release")
        with recording(
            materialization_module, close_fault=lambda fd, owner: descriptor_error
        ) as rec:
            with file_object_faults(
                materialization_module, flush_fault=flush_error
            ):
                with mock.patch.object(
                    materialization_module.os, "replace"
                ) as replace:
                    with self.assertRaises(MaterializationError) as ctx:
                        materialize_artifact(
                            _selected(), constructor_project_root=root,
                            cache_root=cache, transport=transport,
                        )
        self.assertIs(ctx.exception.__cause__, flush_error)
        self.assertIn(descriptor_error, secondary_diagnostics(ctx.exception))
        replace.assert_not_called()
        rec.assert_terminal_and_at_most_once(self)
        self.assertEqual([], self._temporaries(state))
        self.assertFalse(self._destination(state).exists())

    def test_file_object_close_failure_stays_primary_over_descriptor_close(self) -> None:
        root, cache, state = self._environment()
        transport = RecordingTransport((b"pay", b"load"))
        file_close_error = OSError(errno.EIO, "injected file-object close")
        descriptor_error = OSError(errno.EIO, "injected descriptor release")
        with recording(
            materialization_module, close_fault=lambda fd, owner: descriptor_error
        ) as rec:
            with file_object_faults(
                materialization_module, close_fault=file_close_error
            ):
                with mock.patch.object(
                    materialization_module.os, "replace"
                ) as replace:
                    with self.assertRaises(MaterializationError) as ctx:
                        materialize_artifact(
                            _selected(), constructor_project_root=root,
                            cache_root=cache, transport=transport,
                        )
        self.assertIs(ctx.exception.__cause__, file_close_error)
        self.assertIn(descriptor_error, secondary_diagnostics(ctx.exception))
        replace.assert_not_called()
        rec.assert_terminal_and_at_most_once(self)
        self.assertEqual([], self._temporaries(state))
        self.assertFalse(self._destination(state).exists())

    def test_initialization_failure_releases_owner_and_removes_temporary(self) -> None:
        root, cache, state = self._environment()
        transport = RecordingTransport((b"payload",))
        init_error = RuntimeError("injected initialization failure")
        with recording(materialization_module) as rec:
            with mock.patch.object(
                materialization_module.hashlib, "new", side_effect=init_error
            ):
                with mock.patch.object(
                    materialization_module.os, "replace"
                ) as replace:
                    with self.assertRaises(MaterializationError) as ctx:
                        materialize_artifact(
                            _selected(), constructor_project_root=root,
                            cache_root=cache, transport=transport,
                        )
        self.assertIs(ctx.exception.__cause__, init_error)
        replace.assert_not_called()
        rec.assert_terminal_and_at_most_once(self)
        self.assertEqual([], self._temporaries(state))
        self.assertFalse(self._destination(state).exists())

    def test_path_construction_failure_releases_owner_and_removes_temporary(
        self,
    ) -> None:
        root, cache, state = self._environment()
        transport = RecordingTransport((b"payload",))
        init_error = RuntimeError("injected Path construction failure")
        real_path = Path

        def faulty_path(value, *args, **kwargs):
            # Fail only for the freshly minted mkstemp() name; earlier path
            # construction (project/cache roots, blob destination) must keep
            # working so the failure is reached deterministically.
            if ".materialize-" in str(value):
                raise init_error
            return real_path(value, *args, **kwargs)

        with recording(materialization_module) as rec:
            with mock.patch.object(
                materialization_module, "Path", side_effect=faulty_path
            ):
                with mock.patch.object(
                    materialization_module.os, "replace"
                ) as replace:
                    with self.assertRaises(MaterializationError) as ctx:
                        materialize_artifact(
                            _selected(), constructor_project_root=root,
                            cache_root=cache, transport=transport,
                        )
        self.assertIs(ctx.exception.__cause__, init_error)
        replace.assert_not_called()
        rec.assert_terminal_and_at_most_once(self)
        self.assertEqual([], self._temporaries(state))
        self.assertFalse(self._destination(state).exists())

    def test_transport_failure_stays_primary_over_descriptor_release(self) -> None:
        root, cache, state = self._environment()
        transport = RecordingTransport((b"pay",), MaterializationError("interrupted"))
        descriptor_error = OSError(errno.EIO, "injected descriptor release")
        with recording(
            materialization_module, close_fault=lambda fd, owner: descriptor_error
        ) as rec:
            with mock.patch.object(
                materialization_module.os, "replace"
            ) as replace:
                with self.assertRaises(MaterializationError) as ctx:
                    materialize_artifact(
                        _selected(), constructor_project_root=root,
                        cache_root=cache, transport=transport,
                    )
        self.assertEqual(str(ctx.exception), "interrupted")
        self.assertIn(descriptor_error, secondary_diagnostics(ctx.exception))
        replace.assert_not_called()
        rec.assert_terminal_and_at_most_once(self)
        self.assertEqual([], self._temporaries(state))
        self.assertFalse(self._destination(state).exists())
