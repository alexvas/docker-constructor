"""Phase 9A — explicit cleanup primary (no ambient exception capture).

Cleanup accumulators must be constructed from the *operation's own* exception,
not from ``sys.exc_info()`` inside a ``finally``.  Inside a caller's ``except``
block the ambient exception belongs to the caller, so an ambient capture would
attach cleanup diagnostics to an unrelated exception and silently swallow a
close failure on the success path.

These tests invoke each affected operation while an unrelated caller exception
is being handled and assert the four contract behaviors:

* a successful operation plus a failed close raises the close failure;
* the caller's handled exception receives no cleanup diagnostics;
* a successful operation and cleanup return normally (including inside an
  interruption handler); and
* a locally failing operation preserves its own primary and attaches ordinary
  cleanup failures to it.
"""
from __future__ import annotations

import errno
import hashlib
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from docker.npm_environment import publication
from docker.versioning import (
    artifact_cache,
    build_cache,
    build_materialization,
    build_snapshot,
    project_state,
)
from docker.versioning.build_materialization import SelectedBuildArtifact
from docker.versioning.digest_identity import DigestIdentity
from tests.test_constructor_materialization import _make_integrity_for

_SECONDARY_SLOT = "_transaction_secondary"


def _run_inside_handler(operation):
    """Run *operation* while an unrelated caller exception is being handled."""
    caller_exc = ValueError("caller handled")
    outcome: dict[str, object] = {}
    try:
        raise caller_exc
    except ValueError:
        try:
            outcome["value"] = operation()
        except BaseException as exc:
            outcome["raised"] = exc
    return caller_exc, outcome


class _AmbientPrimaryTestCase(unittest.TestCase):
    def assert_untouched(self, exc: BaseException) -> None:
        self.assertFalse(
            hasattr(exc, _SECONDARY_SLOT),
            f"caller exception gained cleanup diagnostics: {vars(exc)}",
        )
        self.assertNotIn("__notes__", vars(exc))

    def assert_secondary(self, primary: BaseException, secondary: BaseException) -> None:
        attached = getattr(primary, _SECONDARY_SLOT, None)
        self.assertIsInstance(attached, list)
        self.assertIn(secondary, attached)


def _distinct_close_faults():
    """Return a close replacement raising a fresh ``OSError`` per call."""
    calls: list[OSError] = []

    def close(fd: int) -> None:
        exc = OSError(errno.EIO, f"close failed for fd {fd}")
        calls.append(exc)
        raise exc

    return close, calls


class FsyncDirAmbientPrimaryTests(_AmbientPrimaryTestCase):
    """``publication._fsync_dir`` — single-resource ``finally`` close."""

    def test_close_failure_raises_and_leaves_caller_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            close_exc = OSError(errno.EIO, "close failed")

            def operation() -> None:
                with mock.patch.object(
                    publication.os, "close", side_effect=close_exc
                ):
                    publication._fsync_dir(td)

            caller_exc, outcome = _run_inside_handler(operation)
        self.assertIs(outcome.get("raised"), close_exc)
        self.assert_untouched(caller_exc)

    def test_success_returns_normally_inside_handler(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            caller_exc, outcome = _run_inside_handler(
                lambda: publication._fsync_dir(td)
            )
        self.assertEqual(outcome.get("value"), None)
        self.assertNotIn("raised", outcome)
        self.assert_untouched(caller_exc)

    def test_success_inside_interruption_handler_returns_normally(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            outcome: dict[str, object] = {}
            try:
                raise KeyboardInterrupt()
            except KeyboardInterrupt:
                try:
                    publication._fsync_dir(td)
                    outcome["value"] = "ok"
                except BaseException as exc:
                    outcome["raised"] = exc
        self.assertEqual(outcome.get("value"), "ok")
        self.assertNotIn("raised", outcome)

    def test_local_operation_failure_preserves_primary_and_attaches_close(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            op_exc = OSError(errno.EIO, "fsync failed")
            close_exc = OSError(errno.EIO, "close failed")

            def operation() -> None:
                with mock.patch.object(
                    publication.os, "fsync", side_effect=op_exc
                ), mock.patch.object(
                    publication.os, "close", side_effect=close_exc
                ):
                    publication._fsync_dir(td)

            caller_exc, outcome = _run_inside_handler(operation)
        self.assertIs(outcome.get("raised"), op_exc)
        self.assert_secondary(op_exc, close_exc)
        self.assert_untouched(caller_exc)


class DurableWriteAmbientPrimaryTests(_AmbientPrimaryTestCase):
    """``publication._durable_write`` — ``finally`` close around a write loop."""

    def test_close_failure_raises_and_leaves_caller_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "leaf"
            close_exc = OSError(errno.EIO, "close failed")

            def operation() -> None:
                with mock.patch.object(
                    publication.os, "close", side_effect=close_exc
                ):
                    publication._durable_write(target, b"payload")

            caller_exc, outcome = _run_inside_handler(operation)
        self.assertIs(outcome.get("raised"), close_exc)
        self.assert_untouched(caller_exc)

    def test_local_operation_failure_preserves_primary_and_attaches_close(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "leaf"
            op_exc = OSError(errno.EIO, "write failed")
            close_exc = OSError(errno.EIO, "close failed")

            def operation() -> None:
                with mock.patch.object(
                    publication.os, "write", side_effect=op_exc
                ), mock.patch.object(
                    publication.os, "close", side_effect=close_exc
                ):
                    publication._durable_write(target, b"payload")

            caller_exc, outcome = _run_inside_handler(operation)
        self.assertIs(outcome.get("raised"), op_exc)
        self.assert_secondary(op_exc, close_exc)
        self.assert_untouched(caller_exc)


class ReadMetadataAmbientPrimaryTests(_AmbientPrimaryTestCase):
    """``project_state._read_metadata`` — ``finally`` close of a read descriptor."""

    def _namespace(self, data: bytes) -> int:
        namespace_fd = os.open(
            self.tmp.name, os.O_RDONLY | os.O_DIRECTORY
        )
        file_fd = os.open(
            "project.json",
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
            dir_fd=namespace_fd,
        )
        try:
            os.fchmod(file_fd, 0o600)
            os.write(file_fd, data)
        finally:
            os.close(file_fd)
        return namespace_fd

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_close_failure_raises_and_leaves_caller_untouched(self) -> None:
        expected = b"identity-metadata"
        namespace_fd = self._namespace(expected)
        try:
            close_exc = OSError(errno.EIO, "close failed")

            def operation() -> None:
                with mock.patch.object(
                    project_state.os, "close", side_effect=close_exc
                ):
                    project_state._read_metadata(namespace_fd, expected, "label")

            caller_exc, outcome = _run_inside_handler(operation)
        finally:
            os.close(namespace_fd)
        self.assertIs(outcome.get("raised"), close_exc)
        self.assert_untouched(caller_exc)

    def test_local_operation_failure_preserves_primary_and_attaches_close(self) -> None:
        expected = b"identity-metadata"
        namespace_fd = self._namespace(expected)
        try:
            op_exc = OSError(errno.EIO, "fstat failed")
            close_exc = OSError(errno.EIO, "close failed")

            def operation() -> None:
                with mock.patch.object(
                    project_state.os, "fstat", side_effect=op_exc
                ), mock.patch.object(
                    project_state.os, "close", side_effect=close_exc
                ):
                    project_state._read_metadata(namespace_fd, expected, "label")

            caller_exc, outcome = _run_inside_handler(operation)
        finally:
            os.close(namespace_fd)
        self.assertIs(outcome.get("raised"), op_exc)
        self.assert_secondary(op_exc, close_exc)
        self.assert_untouched(caller_exc)


class BuildCacheExceptionHandlerPrimaryTests(_AmbientPrimaryTestCase):
    """``build_cache._open_namespace_fd`` — existing ``except`` handler."""

    def test_local_failure_preserves_primary_and_attaches_close(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            op_exc = OSError(errno.EIO, "fstat failed")
            close_exc = OSError(errno.EIO, "close failed")

            def operation() -> None:
                with mock.patch.object(
                    build_cache.os, "fstat", side_effect=op_exc
                ), mock.patch.object(
                    build_cache.os, "close", side_effect=close_exc
                ):
                    build_cache._open_namespace_fd(Path(td))

            caller_exc, outcome = _run_inside_handler(operation)
        self.assertIs(outcome.get("raised"), op_exc)
        self.assert_secondary(op_exc, close_exc)
        self.assert_untouched(caller_exc)


class BuildSnapshotHardLinkAmbientPrimaryTests(_AmbientPrimaryTestCase):
    """``build_snapshot._validate_hard_link_destination`` — ``finally`` close."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.payload = b"hard-link-payload"
        self.source = os.path.join(self.tmp.name, "source")
        self.destination = os.path.join(self.tmp.name, "dest")
        with open(self.source, "wb") as stream:
            stream.write(self.payload)
        os.link(self.source, self.destination)
        os.chmod(self.destination, 0o444)
        self.dir_fd = os.open(self.tmp.name, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, self.dir_fd)
        self.source_stat = os.stat(self.source)
        self.expected = hashlib.sha256(self.payload).hexdigest()

    def test_close_failure_raises_and_leaves_caller_untouched(self) -> None:
        close_exc = OSError(errno.EIO, "close failed")

        def operation() -> None:
            with mock.patch.object(
                build_snapshot.os, "close", side_effect=close_exc
            ):
                build_snapshot._validate_hard_link_destination(
                    "dest", self.dir_fd, self.source_stat, self.expected
                )

        caller_exc, outcome = _run_inside_handler(operation)
        self.assertIs(outcome.get("raised"), close_exc)
        self.assert_untouched(caller_exc)

    def test_local_validation_failure_preserves_primary_and_attaches_close(self) -> None:
        close_exc = OSError(errno.EIO, "close failed")

        def operation() -> None:
            with mock.patch.object(
                build_snapshot.os, "close", side_effect=close_exc
            ):
                build_snapshot._validate_hard_link_destination(
                    "dest", self.dir_fd, self.source_stat, "0" * 64
                )

        caller_exc, outcome = _run_inside_handler(operation)
        raised = outcome.get("raised")
        self.assertIsInstance(raised, build_snapshot.SnapshotError)
        self.assert_secondary(raised, close_exc)
        self.assert_untouched(caller_exc)


class MaterializeArtifactAmbientPrimaryTests(_AmbientPrimaryTestCase):
    """``build_materialization.materialize_artifact`` — except+finally mapping."""

    class _OneChunkTransport:
        def __init__(self, payload: bytes) -> None:
            self.payload = payload

        def stream(self, url: str):
            yield self.payload

    def test_cleanup_failure_raises_and_leaves_caller_untouched(self) -> None:
        payload = b"materialized-artifact"
        with tempfile.TemporaryDirectory() as td:
            selected = SelectedBuildArtifact(
                "uv",
                "https://x.test/pkg.tgz",
                DigestIdentity.from_hex("sha256", hashlib.sha256(payload).hexdigest()),
            )
            cache = os.path.join(td, "cache")
            os.makedirs(cache, mode=0o700)
            unlink_exc = OSError(errno.EIO, "unlink failed")

            def operation():
                with mock.patch.object(
                    build_materialization.Path, "unlink", side_effect=unlink_exc
                ):
                    return build_materialization.materialize_artifact(
                        selected,
                        constructor_project_root=td,
                        cache_root=cache,
                        transport=self._OneChunkTransport(payload),
                    )

            caller_exc, outcome = _run_inside_handler(operation)
        self.assertIs(outcome.get("raised"), unlink_exc)
        self.assert_untouched(caller_exc)


class NestedScopeAmbientPrimaryTests(_AmbientPrimaryTestCase):
    """``artifact_cache.inspect_verified_blob_readonly`` — nested ``finally`` scopes."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.payload = b"nested-scope-payload"
        self.integrity = _make_integrity_for(self.payload)
        identity = DigestIdentity.from_sri(self.integrity)
        self.cache_root = os.path.join(self.tmp.name, "cache")
        algorithm_dir = os.path.join(self.cache_root, identity.algorithm)
        os.makedirs(algorithm_dir, mode=0o700)
        blob_path = os.path.join(
            algorithm_dir, f"{identity.runtime_safe_digest()}.tgz"
        )
        with open(blob_path, "wb") as stream:
            stream.write(self.payload)
        os.chmod(blob_path, 0o444)

    def test_valid_hit_returns_true_inside_handler(self) -> None:
        caller_exc, outcome = _run_inside_handler(
            lambda: artifact_cache.inspect_verified_blob_readonly(
                self.integrity, cache_root=self.cache_root
            )
        )
        self.assertIs(outcome.get("value"), True)
        self.assertNotIn("raised", outcome)
        self.assert_untouched(caller_exc)

    def test_each_nested_close_is_attempted_once_and_leaves_caller_untouched(self) -> None:
        close, calls = _distinct_close_faults()
        with mock.patch.object(artifact_cache.os, "close", side_effect=close):
            caller_exc, outcome = _run_inside_handler(
                lambda: artifact_cache.inspect_verified_blob_readonly(
                    self.integrity, cache_root=self.cache_root
                )
            )
        # blob, algorithm, and root descriptors are each released once.
        self.assertEqual(len(calls), 3)
        self.assertIs(outcome.get("raised"), calls[0])
        self.assert_secondary(calls[0], calls[1])
        self.assert_secondary(calls[0], calls[2])
        self.assert_untouched(caller_exc)


if __name__ == "__main__":
    unittest.main()
