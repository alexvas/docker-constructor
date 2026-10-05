"""Phase 9A task 9A.5 — specialized L3 cleanup fault injection.

These tests pin that specialized build-blob publication/verification,
snapshot construction, failed materialization publication, and npm-owned
staging cleanup treat ``FileNotFoundError`` as idempotent absence only inside
a domain action that declares it, while permission, I/O, unexpected defects,
and interruptions remain observable and never skip later independent cleanup.
"""
from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from docker.transactions.cleanup import CleanupFailures
from docker.versioning.build_materialization import (
    MaterializationError,
    SelectedBuildArtifact,
    _remove_owned_leaf,
    materialize_artifact,
)
from docker.versioning.build_snapshot import (
    SnapshotError,
    create_artifact_snapshot,
)
from docker.versioning.digest_identity import DigestIdentity


class _Transport:
    def __init__(self, chunks):
        self._chunks = chunks

    def stream(self, url):
        yield from self._chunks


def _secondary_of(exc: BaseException) -> list[BaseException]:
    return getattr(exc, "_transaction_secondary", [])


def _snapshot_pairs(root):
    result = []
    for name, payload in (("rustup", b"r"), ("uv", b"u"), ("rtk", b"t"), ("fd", b"f")):
        blob = root / f"{name}.blob"
        blob.write_bytes(payload)
        blob.chmod(0o444)
        result.append(
            (
                SelectedBuildArtifact(
                    name,
                    "https://not-exposed.invalid/",
                    DigestIdentity.from_hex("sha256", hashlib.sha256(payload).hexdigest()),
                ),
                blob,
            )
        )
    return result


@contextlib.contextmanager
def _staging_instruments(record, *, staging_open_fault=None, staging_close_fault=None):
    """Record the staging path/fd and inject staging open/close faults.

    ``staging_close_fault`` maps the 1-based close attempt to an exception to
    raise (or ``None`` to let the close proceed).  The staging fd is released
    on exit if a fault left it open, so a failed close is never leaked into the
    next test.
    """
    import docker.versioning.build_snapshot as build_snapshot

    real_mkdtemp = tempfile.mkdtemp
    real_open = os.open
    real_close = os.close

    def fake_mkdtemp(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        if kwargs.get("prefix") == "transaction-":
            record["staging"] = path
        return path

    def fake_open(path, flags, *args, **kwargs):
        rendered = os.fspath(path)
        if staging_open_fault is not None and rendered == record.get("staging"):
            raise staging_open_fault
        fd = real_open(path, flags, *args, **kwargs)
        if rendered == record.get("staging"):
            record.setdefault("staging_fd", fd)
        return fd

    def fake_close(fd):
        if fd == record.get("staging_fd"):
            attempt = record.get("close_attempts", 0) + 1
            record["close_attempts"] = attempt
            if staging_close_fault is not None:
                fault = staging_close_fault(attempt)
                if fault is not None:
                    raise fault
        result = real_close(fd)
        if fd == record.get("staging_fd"):
            record["staging_closed"] = True
        return result

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(build_snapshot.tempfile, "mkdtemp", fake_mkdtemp))
        stack.enter_context(patch.object(build_snapshot.os, "open", fake_open))
        stack.enter_context(patch.object(build_snapshot.os, "close", fake_close))
        try:
            yield
        finally:
            fd = record.get("staging_fd")
            if fd is not None and not record.get("staging_closed"):
                try:
                    real_close(fd)
                except OSError:
                    pass


class RemoveOwnedLeafTests(unittest.TestCase):
    def test_absent_entry_is_idempotent_absence(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _remove_owned_leaf(Path(td) / "missing")

    def test_permission_failure_is_not_suppressed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "entry"
            target.write_bytes(b"x")
            with patch.object(Path, "unlink", side_effect=PermissionError(errno.EACCES, "no")):
                with self.assertRaises(PermissionError):
                    _remove_owned_leaf(target)


class BuildMaterializationCleanupTests(unittest.TestCase):
    def _selected(self, payload: bytes = b"payload") -> SelectedBuildArtifact:
        return SelectedBuildArtifact(
            "uv",
            "https://secret.example/path",
            DigestIdentity.from_hex("sha256", hashlib.sha256(payload).hexdigest()),
        )

    def test_failed_verification_cleanup_failure_stays_secondary(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "cache"
            cache.mkdir(mode=0o700)
            real_unlink = Path.unlink

            def fake_unlink(self, *args, **kwargs):
                if self.suffix == ".blob":
                    raise PermissionError(errno.EACCES, "injected blob unlink")
                return real_unlink(self, *args, **kwargs)

            with patch(
                "docker.versioning.build_materialization._verify_hit",
                return_value=False,
            ), patch.object(Path, "unlink", fake_unlink):
                with self.assertRaises(MaterializationError) as ctx:
                    materialize_artifact(
                        self._selected(),
                        constructor_project_root=td,
                        cache_root=cache,
                        transport=_Transport((b"payload",)),
                    )
            self.assertIn("failed verification", str(ctx.exception))
            secondary = _secondary_of(ctx.exception)
            self.assertTrue(
                any(isinstance(exc, PermissionError) for exc in secondary),
                secondary,
            )

    def test_digest_mismatch_temporary_cleanup_failure_stays_secondary(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "cache"
            cache.mkdir(mode=0o700)
            real_unlink = Path.unlink

            def fake_unlink(self, *args, **kwargs):
                if self.name.startswith(".materialize-"):
                    raise PermissionError(errno.EACCES, "injected temp unlink")
                return real_unlink(self, *args, **kwargs)

            with patch.object(Path, "unlink", fake_unlink):
                with self.assertRaises(MaterializationError) as ctx:
                    materialize_artifact(
                        self._selected(),
                        constructor_project_root=td,
                        cache_root=cache,
                        transport=_Transport((b"wrong payload",)),
                    )
            secondary = _secondary_of(ctx.exception)
            self.assertTrue(
                any(isinstance(exc, PermissionError) for exc in secondary),
                secondary,
            )


class SnapshotConstructionCleanupTests(unittest.TestCase):
    def _pairs(self, root: Path):
        result = []
        for name, payload in (("rustup", b"r"), ("uv", b"u"), ("rtk", b"t"), ("fd", b"f")):
            blob = root / f"{name}.blob"
            blob.write_bytes(payload)
            blob.chmod(0o444)
            result.append(
                (
                    SelectedBuildArtifact(
                        name,
                        "https://not-exposed.invalid/",
                        DigestIdentity.from_hex("sha256", hashlib.sha256(payload).hexdigest()),
                    ),
                    blob,
                )
            )
        return result

    def test_construction_failure_cleanup_failure_stays_secondary(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pairs = self._pairs(root)
            cleanup_failure = SnapshotError("injected cleanup failure")
            with patch(
                "docker.versioning.build_snapshot.validate_host_owner_traversal",
                side_effect=RuntimeError("traversal failed"),
            ), patch(
                "docker.versioning.build_snapshot.cleanup_artifact_snapshot",
                side_effect=cleanup_failure,
            ):
                with self.assertRaises(RuntimeError) as ctx:
                    create_artifact_snapshot(
                        (x[0] for x in pairs),
                        (x[1] for x in pairs),
                        constructor_project_root=root,
                    )
            self.assertIn("traversal failed", str(ctx.exception))
            self.assertIn(cleanup_failure, _secondary_of(ctx.exception))

    def test_construction_interruption_stays_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pairs = self._pairs(root)
            cleanup_failure = PermissionError(errno.EACCES, "injected cleanup")
            with patch(
                "docker.versioning.build_snapshot.validate_host_owner_traversal",
                side_effect=KeyboardInterrupt("construction interrupted"),
            ), patch(
                "docker.versioning.build_snapshot.cleanup_artifact_snapshot",
                side_effect=cleanup_failure,
            ):
                with self.assertRaises(KeyboardInterrupt) as ctx:
                    create_artifact_snapshot(
                        (x[0] for x in pairs),
                        (x[1] for x in pairs),
                        constructor_project_root=root,
                    )
            self.assertEqual(_secondary_of(ctx.exception), [cleanup_failure])


class SnapshotDescriptorCloseTests(unittest.TestCase):
    """9A.9 — a snapshot staging/source close failure never masks a primary."""

    def _pairs(self, root: Path):
        result = []
        for name, payload in (("rustup", b"r"), ("uv", b"u"), ("rtk", b"t"), ("fd", b"f")):
            blob = root / f"{name}.blob"
            blob.write_bytes(payload)
            blob.chmod(0o444)
            result.append(
                (
                    SelectedBuildArtifact(
                        name,
                        "https://not-exposed.invalid/",
                        DigestIdentity.from_hex("sha256", hashlib.sha256(payload).hexdigest()),
                    ),
                    blob,
                )
            )
        return result

    def _close_failure_patches(self, record):
        import docker.versioning.build_snapshot as build_snapshot

        real_mkdtemp = tempfile.mkdtemp
        real_open = os.open
        real_close = os.close

        def fake_mkdtemp(*args, **kwargs):
            path = real_mkdtemp(*args, **kwargs)
            if kwargs.get("prefix") == "transaction-":
                record["staging"] = path
            return path

        def fake_open(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            rendered = os.fspath(path)
            if rendered == record.get("staging"):
                record.setdefault("target_fds", set()).add(fd)
            elif rendered in record.get("blob_paths", ()):
                record.setdefault("target_fds", set()).add(fd)
            return fd

        def fake_close(fd):
            if fd in record.get("target_fds", ()):
                record["target_fds"].discard(fd)
                raise OSError(errno.EIO, "injected descriptor close failure")
            return real_close(fd)

        return (
            patch.object(build_snapshot.tempfile, "mkdtemp", fake_mkdtemp),
            patch.object(build_snapshot.os, "open", fake_open),
            patch.object(build_snapshot.os, "close", fake_close),
        )

    def test_staging_close_failure_never_masks_construction_primary(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pairs = self._pairs(root)
            record: dict[str, object] = {}
            mkdtemp_patch, open_patch, close_patch = self._close_failure_patches(record)
            with mkdtemp_patch, open_patch, close_patch, patch(
                "docker.versioning.build_snapshot.validate_host_owner_traversal",
                side_effect=RuntimeError("traversal failed"),
            ):
                with self.assertRaises(RuntimeError) as ctx:
                    create_artifact_snapshot(
                        (x[0] for x in pairs),
                        (x[1] for x in pairs),
                        constructor_project_root=root,
                    )
            self.assertIn("traversal failed", str(ctx.exception))
            secondary = _secondary_of(ctx.exception)
            self.assertTrue(
                any(getattr(exc, "errno", None) == errno.EIO for exc in secondary),
                secondary,
            )

    def test_source_close_failure_never_masks_iteration_primary(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pairs = self._pairs(root)
            record: dict[str, object] = {
                "blob_paths": {os.fspath(blob) for _, blob in pairs},
            }
            mkdtemp_patch, open_patch, close_patch = self._close_failure_patches(record)
            with mkdtemp_patch, open_patch, close_patch, patch(
                "docker.versioning.build_snapshot._validate_hard_link_destination",
                side_effect=RuntimeError("destination validation failed"),
            ):
                with self.assertRaises(RuntimeError) as ctx:
                    create_artifact_snapshot(
                        (x[0] for x in pairs),
                        (x[1] for x in pairs),
                        constructor_project_root=root,
                    )
            self.assertIn("destination validation failed", str(ctx.exception))
            secondary = _secondary_of(ctx.exception)
            self.assertTrue(
                any(getattr(exc, "errno", None) == errno.EIO for exc in secondary),
                secondary,
            )


class SnapshotStagingOpenCleanupTests(unittest.TestCase):
    """A staging-directory open failure still surfaces cleanup failures."""

    def _create(self, root, pairs):
        return create_artifact_snapshot(
            (x[0] for x in pairs),
            (x[1] for x in pairs),
            constructor_project_root=root,
        )

    def test_open_failure_with_ordinary_cleanup_failure_keeps_domain_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pairs = _snapshot_pairs(root)
            record: dict[str, object] = {}
            open_fault = PermissionError(errno.EACCES, "staging open denied")
            cleanup_failure = SnapshotError("injected ordinary cleanup failure")
            with _staging_instruments(record, staging_open_fault=open_fault), patch(
                "docker.versioning.build_snapshot.cleanup_artifact_snapshot",
                side_effect=cleanup_failure,
            ):
                with self.assertRaises(SnapshotError) as ctx:
                    self._create(root, pairs)
        self.assertIn("cannot open snapshot staging directory", str(ctx.exception))
        self.assertIs(ctx.exception.__cause__, open_fault)
        self.assertEqual(_secondary_of(ctx.exception), [cleanup_failure])

    def test_open_failure_with_unexpected_cleanup_defect_is_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pairs = _snapshot_pairs(root)
            record: dict[str, object] = {}
            open_fault = PermissionError(errno.EACCES, "staging open denied")
            defect = RuntimeError("unexpected cleanup defect")
            with _staging_instruments(record, staging_open_fault=open_fault), patch(
                "docker.versioning.build_snapshot.cleanup_artifact_snapshot",
                side_effect=defect,
            ):
                with self.assertRaises(RuntimeError) as ctx:
                    self._create(root, pairs)
        self.assertIs(ctx.exception, defect)
        secondary = _secondary_of(defect)
        self.assertEqual(len(secondary), 1)
        self.assertIsInstance(secondary[0], SnapshotError)
        self.assertIn("cannot open snapshot staging directory", str(secondary[0]))
        self.assertIs(secondary[0].__cause__, open_fault)

    def test_open_failure_with_cleanup_interruption_is_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pairs = _snapshot_pairs(root)
            record: dict[str, object] = {}
            open_fault = PermissionError(errno.EACCES, "staging open denied")
            interruption = KeyboardInterrupt("cleanup interrupted")
            with _staging_instruments(record, staging_open_fault=open_fault), patch(
                "docker.versioning.build_snapshot.cleanup_artifact_snapshot",
                side_effect=interruption,
            ):
                with self.assertRaises(KeyboardInterrupt) as ctx:
                    self._create(root, pairs)
        self.assertIs(ctx.exception, interruption)
        secondary = _secondary_of(interruption)
        self.assertEqual(len(secondary), 1)
        self.assertIsInstance(secondary[0], SnapshotError)
        self.assertIs(secondary[0].__cause__, open_fault)


class SnapshotCleanupPrecedenceTests(unittest.TestCase):
    """Cleanup/descriptor-close failures never displace the first authority."""

    def _create(self, root, pairs):
        return create_artifact_snapshot(
            (x[0] for x in pairs),
            (x[1] for x in pairs),
            constructor_project_root=root,
        )

    def test_programmer_defect_during_construction_cleanup_is_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pairs = _snapshot_pairs(root)
            record: dict[str, object] = {}
            construction_failure = SnapshotError("construction failed")
            defect = RuntimeError("cleanup programmer defect")
            with _staging_instruments(record), patch(
                "docker.versioning.build_snapshot.validate_host_owner_traversal",
                side_effect=construction_failure,
            ), patch(
                "docker.versioning.build_snapshot.cleanup_artifact_snapshot",
                side_effect=defect,
            ):
                with self.assertRaises(RuntimeError) as ctx:
                    self._create(root, pairs)
        self.assertIs(ctx.exception, defect)
        self.assertIn(construction_failure, _secondary_of(defect))
        self.assertEqual(record["close_attempts"], 1)

    def test_tree_cleanup_interruption_precedes_staging_close_interruption(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pairs = _snapshot_pairs(root)
            record: dict[str, object] = {}
            construction_failure = SnapshotError("construction failed")
            cleanup_interruption = KeyboardInterrupt("tree cleanup interrupted")
            close_interruption = SystemExit("staging close interrupted")
            with _staging_instruments(
                record, staging_close_fault=lambda attempt: close_interruption,
            ), patch(
                "docker.versioning.build_snapshot.validate_host_owner_traversal",
                side_effect=construction_failure,
            ), patch(
                "docker.versioning.build_snapshot.cleanup_artifact_snapshot",
                side_effect=cleanup_interruption,
            ):
                with self.assertRaises(KeyboardInterrupt) as ctx:
                    self._create(root, pairs)
            self.assertEqual(record["close_attempts"], 1)
        self.assertIs(ctx.exception, cleanup_interruption)
        secondary = _secondary_of(cleanup_interruption)
        self.assertIn(close_interruption, secondary)
        self.assertIn(construction_failure, secondary)

    def test_cleanup_and_close_ordinary_failures_stay_secondary(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pairs = _snapshot_pairs(root)
            record: dict[str, object] = {}
            construction_failure = SnapshotError("construction failed")
            cleanup_failure = OSError(errno.EIO, "tree cleanup failed")
            close_failure = OSError(errno.EBUSY, "staging close failed")
            with _staging_instruments(
                record, staging_close_fault=lambda attempt: close_failure,
            ), patch(
                "docker.versioning.build_snapshot.validate_host_owner_traversal",
                side_effect=construction_failure,
            ), patch(
                "docker.versioning.build_snapshot.cleanup_artifact_snapshot",
                side_effect=cleanup_failure,
            ):
                with self.assertRaises(SnapshotError) as ctx:
                    self._create(root, pairs)
            self.assertEqual(record["close_attempts"], 1)
        self.assertIs(ctx.exception, construction_failure)
        secondary = _secondary_of(construction_failure)
        self.assertIn(cleanup_failure, secondary)
        self.assertIn(close_failure, secondary)


class AccumulatorIdempotentAbsenceBoundaryTests(unittest.TestCase):
    def test_accumulator_does_not_suppress_file_not_found(self) -> None:
        failures = CleanupFailures(None)
        error = FileNotFoundError(errno.ENOENT, "not found")
        failures.run(
            lambda: (_ for _ in ()).throw(error),
            ordinary=(OSError,),
        )
        self.assertIs(failures.complete(), error)


if __name__ == "__main__":
    unittest.main()
