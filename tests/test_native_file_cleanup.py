"""Local file readers retain failure precedence without native double release."""

from __future__ import annotations

import io
import stat
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from docker import runtime_installer
from docker.versioning import build_materialization, cache, verification
from docker.versioning.digest_identity import DigestIdentity


class ReadStream(io.BytesIO):
    def __init__(self, data, operation=None, cleanup=None):
        super().__init__(data)
        self.operation = operation
        self.cleanup = cleanup
        self.attempts = 0

    def read(self, *args, **kwargs):
        if self.operation is not None:
            raise self.operation
        return super().read(*args, **kwargs)

    def close(self):
        self.attempts += 1
        super().close()
        if self.cleanup is not None:
            raise self.cleanup


def reader_case(name, stream):
    """Invoke the actual production boundary, without acquiring an OS fd."""
    if name == "verification":
        with patch.object(Path, "open", return_value=stream):
            return verification._load_build_projection(Path("/projection"))
    if name == "hit":
        identity = DigestIdentity.from_hex("sha256", "00" * 32)
        with (
            patch.object(
                Path,
                "lstat",
                return_value=SimpleNamespace(st_mode=stat.S_IFREG | 0o444),
            ),
            patch.object(Path, "open", return_value=stream),
        ):
            return build_materialization._verify_hit(Path("/blob"), identity)
    with patch("builtins.open", return_value=stream):
        if name == "projection":
            return runtime_installer.read_projection("/projection")
        if name == "metadata":
            return runtime_installer.InstallContext.real_metadata().read(
                pi_home="/pi", metadata_file="package.json", package="example"
            )
        return cache.DiskCache(Path("/cache")).get("GET", "https://example.invalid")


READERS = ("verification", "hit", "projection", "metadata", "cache")
DATA = {
    "verification": b"[extensions]\n",
    "hit": b"blob",
    "projection": b"[extensions]\n",
    "metadata": b"{}",
    "cache": b'{"status":200,"body_b64":""}',
}


class NativeFileReaderTests(unittest.TestCase):
    def test_read_interruption_survives_ordinary_close_failure(self):
        for name in READERS:
            with self.subTest(reader=name):
                primary = KeyboardInterrupt("read interrupted")
                cleanup = OSError("close failed")
                stream = ReadStream(DATA[name], primary, cleanup)
                with self.assertRaises(KeyboardInterrupt) as caught:
                    reader_case(name, stream)
                self.assertIs(caught.exception, primary)
                self.assertEqual(stream.attempts, 1)
                self.assertTrue(stream.closed)

    def test_sole_close_interruption_keeps_identity(self):
        for name in READERS:
            with self.subTest(reader=name):
                cleanup = KeyboardInterrupt("close interrupted")
                stream = ReadStream(DATA[name], cleanup=cleanup)
                with self.assertRaises(KeyboardInterrupt) as caught:
                    reader_case(name, stream)
                self.assertIs(caught.exception, cleanup)
                self.assertEqual(stream.attempts, 1)

    def test_success_releases_once(self):
        for name in READERS:
            with self.subTest(reader=name):
                stream = ReadStream(DATA[name])
                result = reader_case(name, stream)
                if name == "projection":
                    self.assertEqual(result, [])
                elif name == "metadata":
                    self.assertEqual(result, {})
                elif name == "verification":
                    self.assertEqual(result, {"extensions": {}})
                elif name == "hit":
                    self.assertIs(result, False)
                else:
                    self.assertEqual(result.status, 200)
                self.assertEqual(stream.attempts, 1)
                self.assertTrue(stream.closed)


class NativeFileBoundaryTests(unittest.TestCase):
    def test_ordinary_operation_remains_selected_over_ordinary_close(self):
        for name in READERS:
            with self.subTest(reader=name):
                primary = OSError("read failed")
                cleanup = OSError("close failed")
                stream = ReadStream(DATA[name], primary, cleanup)
                if name in ("cache", "hit"):
                    self.assertIs(
                        reader_case(name, stream), None if name == "cache" else False
                    )
                elif name == "projection":
                    with self.assertRaises(runtime_installer.ProjectionError) as caught:
                        reader_case(name, stream)
                    self.assertIn("read failed", str(caught.exception))
                    self.assertNotIn("close failed", str(caught.exception))
                elif name == "metadata":
                    with self.assertRaises(runtime_installer.InstallError) as caught:
                        reader_case(name, stream)
                    self.assertIs(caught.exception.__cause__, primary)
                else:
                    with self.assertRaises(OSError) as caught:
                        reader_case(name, stream)
                    self.assertIs(caught.exception, primary)
                self.assertEqual(len(primary._transaction_secondary), 1)
                self.assertIs(primary._transaction_secondary[0], cleanup)
                self.assertEqual(stream.attempts, 1)

    def test_sole_ordinary_close_uses_existing_domain_boundary(self):
        for name in READERS:
            with self.subTest(reader=name):
                cleanup = OSError("close failed")
                stream = ReadStream(DATA[name], cleanup=cleanup)
                if name in ("cache", "hit"):
                    self.assertIs(
                        reader_case(name, stream), None if name == "cache" else False
                    )
                else:
                    expected = {
                        "verification": OSError,
                        "projection": runtime_installer.ProjectionError,
                        "metadata": runtime_installer.InstallError,
                    }[name]
                    with self.assertRaises(expected) as caught:
                        reader_case(name, stream)
                    if name == "verification":
                        self.assertIs(caught.exception, cleanup)
                    elif name == "metadata":
                        self.assertIs(caught.exception.__cause__, cleanup)
                    else:
                        self.assertIn("close failed", str(caught.exception))
                self.assertEqual(stream.attempts, 1)

    def test_native_context_manager_never_receives_release_authority(self):
        class NativeContextForbidden(ReadStream):
            def __enter__(self):
                raise AssertionError("native file context entered")

            def __exit__(self, *args):
                raise AssertionError("native file context exited")

        for name in READERS:
            with self.subTest(reader=name):
                stream = NativeContextForbidden(DATA[name])
                reader_case(name, stream)
                self.assertEqual(stream.attempts, 1)


class PreparedFileOwnershipTests(unittest.TestCase):
    def test_failure_precedence_and_terminal_repeat_cleanup(self):
        from docker import _owned_file
        from docker.filesystem.streams import OwnedStream

        for operation in (
            None,
            OSError("read"),
            RuntimeError("read"),
            KeyboardInterrupt("read"),
        ):
            for cleanup in (
                None,
                OSError("close"),
                RuntimeError("close"),
                SystemExit("close"),
            ):
                with self.subTest(
                    operation=type(operation).__name__, cleanup=type(cleanup).__name__
                ):
                    owners = []
                    stream = ReadStream(b"", cleanup=cleanup)

                    def owner_factory(*args, owners=owners, **kwargs):
                        owner = OwnedStream(*args, **kwargs)
                        owners.append(owner)
                        return owner

                    # First interruption > unexpected defect > active ordinary > ordinary close.
                    if operation is not None and not isinstance(operation, Exception):
                        expected = operation
                    elif cleanup is not None and not isinstance(cleanup, OSError):
                        expected = cleanup
                    else:
                        expected = operation if operation is not None else cleanup
                    observed = None
                    with patch.object(
                        _owned_file, "OwnedStream", side_effect=owner_factory
                    ):
                        try:
                            with _owned_file.owned_file(
                                lambda stream=stream: stream, label="file"
                            ) as borrowed:
                                self.assertIs(borrowed, stream)
                                if operation is not None:
                                    raise operation
                        except BaseException as error:
                            observed = error
                    self.assertIs(observed, expected)
                    self.assertEqual(len(owners), 1)
                    owners[0].close()
                    owners[0].close()
                    self.assertTrue(owners[0].released)
                    self.assertEqual(stream.attempts, 1)

    def test_preparation_failures_acquire_nothing(self):
        from unittest.mock import Mock

        from docker import _owned_file

        for target in ("OwnedStream", "_FileRelease"):
            for failure in (
                OSError("prepare"),
                RuntimeError("prepare"),
                KeyboardInterrupt("prepare"),
            ):
                with self.subTest(target=target, failure=type(failure).__name__):
                    opener = Mock()
                    with patch.object(_owned_file, target, side_effect=failure):
                        with self.assertRaises(type(failure)) as caught:
                            with _owned_file.owned_file(opener, label="file"):
                                self.fail("preparation unexpectedly succeeded")
                    self.assertIs(caught.exception, failure)
                    opener.assert_not_called()

    def test_invalid_label_does_not_acquire(self):
        from unittest.mock import Mock

        from docker._owned_file import owned_file

        opener = Mock()
        with self.assertRaises(ValueError):
            with owned_file(opener, label=""):
                self.fail("invalid label accepted")
        opener.assert_not_called()

    def test_acquisition_failure_releases_empty_prepared_owner_once(self):
        from unittest.mock import Mock

        from docker import _owned_file
        from docker.filesystem.streams import OwnedStream

        for failure in (OSError("open"), KeyboardInterrupt("open")):
            with self.subTest(failure=type(failure).__name__):
                owners = []

                def owner_factory(*args, owners=owners, **kwargs):
                    owner = OwnedStream(*args, **kwargs)
                    owners.append(owner)
                    return owner

                opener = Mock(side_effect=failure)
                with patch.object(
                    _owned_file, "OwnedStream", side_effect=owner_factory
                ):
                    with self.assertRaises(type(failure)) as caught:
                        with _owned_file.owned_file(opener, label="file"):
                            self.fail("failed acquisition yielded")
                self.assertIs(caught.exception, failure)
                opener.assert_called_once_with()
                self.assertTrue(owners[0].released)
                self.assertIsNone(owners[0]._stream.stream)
                owners[0].close()

    def test_reentrant_cleanup_uses_same_terminal_owner(self):
        from docker import _owned_file
        from docker.filesystem.streams import OwnedStream

        owners = []

        class ReentrantStream(ReadStream):
            def close(self):
                owners[0].close()
                super().close()

        def owner_factory(*args, owners=owners, **kwargs):
            owner = OwnedStream(*args, **kwargs)
            owners.append(owner)
            return owner

        stream = ReentrantStream(b"")
        with patch.object(_owned_file, "OwnedStream", side_effect=owner_factory):
            with _owned_file.owned_file(lambda stream=stream: stream, label="file"):
                pass
        self.assertEqual(stream.attempts, 1)

    def test_concurrent_loser_cannot_repeat_winning_close(self):
        import threading

        from docker import _owned_file
        from docker.filesystem.streams import OwnedStream

        entered, finish = threading.Event(), threading.Event()
        owners, loser_failures = [], []
        failure = OSError("winning close")

        class BlockingClose(ReadStream):
            def close(self):
                entered.set()
                if not finish.wait(5):
                    raise AssertionError("test did not release close barrier")
                super().close()

        def owner_factory(*args, owners=owners, **kwargs):
            owner = OwnedStream(*args, **kwargs)
            owners.append(owner)
            return owner

        def losing_cleanup():
            try:
                if not entered.wait(5):
                    raise AssertionError("winning close did not begin")
                owners[0].close()
            except BaseException as error:
                loser_failures.append(error)
            finally:
                finish.set()

        stream = BlockingClose(b"", cleanup=failure)
        thread = threading.Thread(target=losing_cleanup)
        try:
            with patch.object(_owned_file, "OwnedStream", side_effect=owner_factory):
                with self.assertRaises(OSError) as caught:
                    with _owned_file.owned_file(
                        lambda stream=stream: stream, label="file"
                    ):
                        thread.start()
                self.assertIs(caught.exception, failure)
        finally:
            finish.set()
            if thread.ident is not None:
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(loser_failures, [])
        self.assertEqual(stream.attempts, 1)
        owners[0].close()
        self.assertEqual(stream.attempts, 1)

    def test_real_file_and_reader_release(self):
        import tempfile

        from docker._owned_file import owned_file

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "projection.toml"
            with owned_file(lambda: path.open("wb"), label="test projection") as stream:
                stream.write(b"[extensions]\n")
            self.assertTrue(stream.closed)
            self.assertEqual(
                verification._load_build_projection(path), {"extensions": {}}
            )
            self.assertEqual(runtime_installer.read_projection(str(path)), [])

    def test_sibling_cleanup_runs_after_first_close_failure(self):
        from docker._owned_file import owned_file

        failure = OSError("inner close")
        first, second = ReadStream(b"", cleanup=failure), ReadStream(b"")
        with self.assertRaises(OSError) as caught:
            with owned_file(lambda: second, label="outer"):
                with owned_file(lambda: first, label="inner"):
                    pass
        self.assertIs(caught.exception, failure)
        self.assertEqual((first.attempts, second.attempts), (1, 1))


if __name__ == "__main__":
    unittest.main()
