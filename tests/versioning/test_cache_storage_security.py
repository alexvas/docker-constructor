"""RED contracts for ``docker.versioning.cache_storage`` — Phase 2.

Phase 2 adds the filesystem validation and hardening layer to
``cache_storage.py`` while keeping the pure resolution layer untouched.
``cache_storage.py`` owns **directory** preparation and security only; it
never serializes HTTP entries or publishes artifact blobs.

Filesystem API under test (added to ``cache_storage.py`` in Phase 2):

* ``prepare_default_root(xdg_cache_home, *, home) -> Path`` resolves the
  default root and hardens it plus its constructor-owned descendants.
  For a non-empty absolute ``XDG_CACHE_HOME`` it creates a missing XDG
  directory with ``0700``, accepts an existing writable directory without
  changing its mode, and rejects a non-directory or unwritable XDG path
  with no fallback.  Empty or non-absolute XDG values use the
  ``~/.cache/docker-constructor`` fallback.
* ``prepare_local_root(value, *, xdg_cache_home, home) -> Path`` resolves
  a local ``[cache].dir`` override and hardens it plus its descendants.

Constructor-owned descendant directories are all ``0700``:

    <root>/versioning
    <root>/runtime-artifacts
    <root>/runtime-artifacts/blobs
    <root>/runtime-artifacts/locks
    <root>/runtime-artifacts/tmp

Security contract — no-follow, descriptor-relative:

* Every existing selected root and descendant is validated **before any
  mutation**.  Symlinks, non-directory entries, foreign-owned
  directories, and paths that cannot be secured raise
  ``CacheStorageError``.
* Inspection and hardening are descriptor-based and no-follow: a
  directory is opened with ``O_DIRECTORY | O_NOFOLLOW`` (descriptor-
  relative to its parent where available), inspected with ``fstat`` on
  the opened descriptor, and its owner UID is compared against the
  invoking effective UID (``os.geteuid()``).  Hardening uses
  ``fchmod`` on the same opened descriptor.  Equivalent safe descriptor-
  based behavior is acceptable; the tests do not prescribe a pathname-
  based call sequence.
* No parent of the resolved root (an existing ``XDG_CACHE_HOME``,
  ``~/.cache``, or ``~``) is ever chmod-ed.

Test seams: foreign ownership is simulated by patching ``os.fstat`` to
report a different ``st_uid`` for the target directory's descriptor;
unsecurable directories are simulated by patching ``os.fchmod`` to raise
``PermissionError`` for that descriptor.  Descriptors are resolved back
to paths via ``/proc/self/fd`` so the mocks stay path-specific.

File-mode contracts stay with their owners and are exercised through them,
not through ``cache_storage.py``: ``cache.py``'s ``DiskCache`` writes HTTP
entries with ``0600``, and ``artifact_cache.py`` publishes verified blobs
with ``0444`` (the latter is asserted here through real publication into a
prepared ``runtime-artifacts/blobs`` directory).
"""

from __future__ import annotations

import base64
import hashlib
import os
import stat
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


def _cache_storage():
    """Import the module under test lazily so RED failures are per-test."""
    from docker.versioning import cache_storage

    return cache_storage


_DESCENDANT_PARTS = (
    ("versioning",),
    ("runtime-artifacts",),
    ("runtime-artifacts", "blobs"),
    ("runtime-artifacts", "locks"),
    ("runtime-artifacts", "tmp"),
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def _fd_path(fd: int) -> str | None:
    """Return the normalized path an open descriptor refers to, or ``None``."""
    try:
        return os.path.normpath(os.readlink(f"/proc/self/fd/{fd}"))
    except OSError:
        return None


def _foreign_uid_fstat(target: Path, foreign_uid: int):
    """Build an ``os.fstat`` fake that reports *foreign_uid* for *target*."""
    real_fstat = os.fstat
    target_norm = os.path.normpath(str(target))

    def fake_fstat(fd, *args, **kwargs):
        st = real_fstat(fd, *args, **kwargs)
        if _fd_path(fd) == target_norm:
            return types.SimpleNamespace(
                st_mode=st.st_mode, st_uid=foreign_uid, st_gid=st.st_gid
            )
        return st

    return fake_fstat


def _deny_fchmod_for(target: Path):
    """Build an ``os.fchmod`` fake that fails only for *target*'s descriptor."""
    real_fchmod = os.fchmod
    target_norm = os.path.normpath(str(target))

    def fake_fchmod(fd, mode, *args, **kwargs):
        if _fd_path(fd) == target_norm:
            raise PermissionError(13, "Permission denied")
        return real_fchmod(fd, mode, *args, **kwargs)

    return fake_fchmod


class _CacheSecurityTestCase(unittest.TestCase):
    """Base for Phase 2 filesystem tests."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="cache-storage-sec-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.home = self.base / "home"
        self.home.mkdir()

    def _xdg_dir(self, mode: int | None = None) -> Path:
        """Return an existing ``<tmp>/xdg/cache`` directory, optionally chmod-ed."""
        xdg = self.base / "xdg" / "cache"
        xdg.mkdir(parents=True, exist_ok=True)
        if mode is not None:
            os.chmod(xdg, mode)
        return xdg

    def _prepare_default(self, xdg: str | None) -> Path:
        return _cache_storage().prepare_default_root(xdg, home=self.home)

    def _prepare_local(self, value: str, *, xdg: str) -> Path:
        return _cache_storage().prepare_local_root(
            value, xdg_cache_home=xdg, home=self.home
        )

    def assert_secured_tree(self, root: Path) -> None:
        """Require *root* and every constructor descendant to be ``0700``."""
        self.assertEqual(_mode(root), 0o700, f"{root} must be owner-only 0700")
        for parts in _DESCENDANT_PARTS:
            child = root.joinpath(*parts)
            self.assertTrue(child.is_dir(), f"{child} must be a directory")
            self.assertEqual(_mode(child), 0o700, f"{child} must be owner-only 0700")


class TestDefaultRootDirectoryHardening(_CacheSecurityTestCase):
    """2.1 — default root and descendant directories are created ``0700``."""

    def test_new_root_under_existing_xdg_created_0700(self) -> None:
        xdg = self._xdg_dir(mode=0o755)
        root = self._prepare_default(str(xdg))
        self.assertEqual(root, xdg / "docker-constructor")
        self.assert_secured_tree(root)

    def test_missing_explicit_xdg_created_0700(self) -> None:
        xdg = self.base / "xdg" / "cache"  # missing
        root = self._prepare_default(str(xdg))
        self.assertEqual(_mode(xdg), 0o700)
        self.assertEqual(root, xdg / "docker-constructor")
        self.assert_secured_tree(root)

    def test_fallback_root_created_0700(self) -> None:
        cache_parent = self.home / ".cache"
        cache_parent.mkdir()
        os.chmod(cache_parent, 0o755)

        root = self._prepare_default("")
        self.assertEqual(root, cache_parent / "docker-constructor")
        self.assert_secured_tree(root)


class TestLocalRootDirectoryHardening(_CacheSecurityTestCase):
    """2.1 — a dedicated local root is created ``0700`` with descendants."""

    def test_missing_local_root_created_0700(self) -> None:
        xdg = self._xdg_dir()
        value = xdg / "docker-constructor-custom"

        root = self._prepare_local(str(value), xdg=str(xdg))
        self.assertEqual(root, value)
        self.assert_secured_tree(root)


class TestHttpEntryMode(_CacheSecurityTestCase):
    """2.2 — ``cache.py`` writes ``0600`` entries into a prepared directory.

    The mode contract is exercised through the real owner (``DiskCache``),
    not through ``cache_storage.py``; ``cache_storage`` only prepares the
    ``versioning/`` parent.
    """

    def test_disk_cache_entry_written_0600(self) -> None:
        from docker.versioning.cache import DiskCache

        xdg = self._xdg_dir()
        root = self._prepare_default(str(xdg))
        versioning = root / "versioning"
        self.assertEqual(_mode(versioning), 0o700)

        cache = DiskCache(versioning, ttl=3600)
        response = types.SimpleNamespace(status=200, headers={}, body=b"payload")
        cache.set("GET", "https://example.test/entry", response)

        entries = [p for p in versioning.iterdir() if p.is_file()]
        self.assertEqual(len(entries), 1)
        self.assertEqual(_mode(entries[0]), 0o600)

    def test_xdg_mode_unchanged_after_http_write(self) -> None:
        """An existing XDG_CACHE_HOME keeps its mode through an HTTP write."""
        from docker.versioning.cache import DiskCache

        xdg = self._xdg_dir(mode=0o755)
        root = self._prepare_default(str(xdg))
        versioning = root / "versioning"

        cache = DiskCache(versioning, ttl=3600)
        cache.set(
            "GET",
            "https://example.test/entry",
            types.SimpleNamespace(status=200, headers={}, body=b"payload"),
        )

        self.assertEqual(_mode(xdg), 0o755)
        self.assertEqual(_mode(versioning), 0o700)


class _FakeStreamingTransport:
    """Minimal in-memory transport for one canned artifact payload."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self.calls: list[str] = []

    def fetch_chunks(self, url: str):
        self.calls.append(url)
        yield self._data


def _integrity_for(data: bytes, algorithm: str = "sha512") -> str:
    """Build an SRI integrity string for *data*."""
    digest = hashlib.new(algorithm, data).digest()
    return f"{algorithm}-{base64.b64encode(digest).decode('ascii')}"


class TestVerifiedBlobMode(_CacheSecurityTestCase):
    """2.2 — real artifact-cache publication writes a verified blob ``0444``.

    Publication, hashing, verification, locks, temporary files, and atomic
    replacement stay owned by ``artifact_cache.py``; ``cache_storage.py``
    only prepares the ``runtime-artifacts/blobs`` directory.
    """

    def test_published_blob_0444_in_prepared_blobs_dir(self) -> None:
        from docker.versioning.artifact_cache import (
            FileIdentityLockFactory,
            LocalCacheFilesystem,
            LocalTemporaryDirectory,
            SelectedArtifact,
            materialize_selected_artifacts,
        )

        xdg = self._xdg_dir()
        root = self._prepare_default(str(xdg))
        blobs = root / "runtime-artifacts" / "blobs"

        data = b"verified-blob-bytes"
        integrity = _integrity_for(data)
        transport = _FakeStreamingTransport(data)

        result = materialize_selected_artifacts(
            [SelectedArtifact("https://x.test/pkg.tgz", integrity)],
            transport=transport,
            filesystem=LocalCacheFilesystem(),
            lock_factory=FileIdentityLockFactory(str(blobs)),
            temp_dir=LocalTemporaryDirectory(),
            temp_root=str(root / "runtime-artifacts" / "tmp"),
            cache_root=str(blobs),
        )

        blob = list(result.values())[0]
        self.assertEqual(_mode(Path(blob.host_path)), 0o444)
        self.assertEqual(transport.calls, ["https://x.test/pkg.tgz"])


class TestParentModesUnchanged(_CacheSecurityTestCase):
    """2.2 — no resolved-root parent is ever chmod-ed."""

    def test_existing_xdg_mode_unchanged(self) -> None:
        xdg = self._xdg_dir(mode=0o755)
        self._prepare_default(str(xdg))
        self.assertEqual(_mode(xdg), 0o755)

    def test_fallback_parents_mode_unchanged(self) -> None:
        cache_parent = self.home / ".cache"
        cache_parent.mkdir()
        os.chmod(cache_parent, 0o755)
        os.chmod(self.home, 0o700)

        self._prepare_default("")

        self.assertEqual(_mode(cache_parent), 0o755)
        self.assertEqual(_mode(self.home), 0o700)

    def test_local_root_parent_mode_unchanged(self) -> None:
        xdg = self._xdg_dir(mode=0o755)
        value = xdg / "docker-constructor-custom"
        value.mkdir()
        os.chmod(value, 0o755)

        self._prepare_local(str(value), xdg=str(xdg))

        self.assertEqual(_mode(xdg), 0o755)
        self.assertEqual(_mode(value), 0o700)


class TestXdgEligibility(_CacheSecurityTestCase):
    """2.3 — existing XDG is accepted, unusable XDG fails without fallback."""

    def test_existing_writable_xdg_accepted(self) -> None:
        xdg = self._xdg_dir(mode=0o755)
        root = self._prepare_default(str(xdg))
        self.assertEqual(root, xdg / "docker-constructor")
        self.assertTrue(root.is_dir())

    def test_xdg_non_directory_rejected_without_fallback(self) -> None:
        parent = self.base / "xdg"
        parent.mkdir()
        not_a_dir = parent / "cache"
        not_a_dir.write_text("not a directory")

        with self.assertRaisesRegex(
            _cache_storage().CacheStorageError, "XDG_CACHE_HOME"
        ):
            self._prepare_default(str(not_a_dir))

        self.assertFalse((self.home / ".cache").exists())

    def test_xdg_component_symlink_rejected_without_following(self) -> None:
        """A symlinked component beneath the XDG parent is rejected and
        never followed; no constructor root appears under the target."""
        real = self.base / "real-xdg"
        real.mkdir()
        link = self.base / "xdg-link"
        link.symlink_to(real, target_is_directory=True)
        xdg = link / "cache"

        with self.assertRaises(_cache_storage().CacheStorageError):
            self._prepare_default(str(xdg))

        self.assertFalse((real / "cache").exists())
        self.assertFalse((real / "docker-constructor").exists())
        self.assertFalse((self.home / ".cache").exists())

    def test_xdg_itself_symlink_rejected_without_following(self) -> None:
        """An XDG path that is itself a symlink is rejected; the target is
        left untouched and no fallback cache is created."""
        real = self.base / "real-cache"
        real.mkdir()
        parent = self.base / "xdg"
        parent.mkdir()
        xdg = parent / "cache"
        xdg.symlink_to(real, target_is_directory=True)

        with self.assertRaises(_cache_storage().CacheStorageError):
            self._prepare_default(str(xdg))

        self.assertFalse((real / "docker-constructor").exists())
        self.assertFalse((self.home / ".cache").exists())

    def test_missing_xdg_component_swapped_to_symlink_rejected(self) -> None:
        """A missing XDG component replaced by a symlink during creation is
        caught by the no-follow reopen and never followed."""
        parent = self.base / "xdg"
        parent.mkdir()
        xdg = parent / "cache"
        real = self.base / "real-cache"
        real.mkdir()

        real_mkdir = os.mkdir

        def racing_mkdir(path, *args, **kwargs):
            result = real_mkdir(path, *args, **kwargs)
            if os.path.basename(str(path)) == "cache":
                dir_fd = kwargs.get("dir_fd")
                os.rmdir(path, dir_fd=dir_fd)
                os.symlink(str(real), path, dir_fd=dir_fd)
            return result

        with mock.patch("os.mkdir", side_effect=racing_mkdir):
            with self.assertRaises(_cache_storage().CacheStorageError):
                self._prepare_default(str(xdg))

        self.assertFalse((real / "docker-constructor").exists())
        self.assertFalse((self.home / ".cache").exists())

    def test_xdg_unwritable_rejected_without_fallback(self) -> None:
        xdg = self._xdg_dir(mode=0o000)
        try:
            with self.assertRaisesRegex(
                _cache_storage().CacheStorageError, "XDG_CACHE_HOME"
            ):
                self._prepare_default(str(xdg))
            self.assertFalse((self.home / ".cache").exists())
        finally:
            os.chmod(xdg, 0o700)

    def test_fallback_used_only_for_empty_or_non_absolute_xdg(self) -> None:
        cache_parent = self.home / ".cache"
        for bad in ("", "relative", "~/cache"):
            with self.subTest(xdg=bad):
                root = self._prepare_default(bad)
                self.assertEqual(root, cache_parent / "docker-constructor")


class TestLocalRootSecurity(_CacheSecurityTestCase):
    """2.3 — existing roots are secured; unsafe roots and descendants are
    rejected before mutation."""

    def test_existing_owned_root_secured_to_0700(self) -> None:
        xdg = self._xdg_dir()
        value = xdg / "docker-constructor-custom"
        value.mkdir()
        os.chmod(value, 0o755)

        root = self._prepare_local(str(value), xdg=str(xdg))

        self.assertEqual(_mode(value), 0o700)
        self.assert_secured_tree(root)

    def test_unsafe_local_roots_rejected_before_write(self) -> None:
        """Roots equal to XDG_CACHE_HOME, ``/``, ``$HOME``, or an XDG
        ancestor are rejected during preparation — before any cache write."""
        xdg = self._xdg_dir()
        for bad in (str(xdg), "/", str(self.home), str(self.base)):
            with self.subTest(value=bad):
                with self.assertRaises(_cache_storage().CacheStorageError):
                    self._prepare_local(bad, xdg=str(xdg))

        self.assertFalse((self.home / ".cache").exists())
        self.assertFalse((xdg / "docker-constructor").exists())

    def test_symlinked_selected_root_rejected(self) -> None:
        target = self.base / "real"
        target.mkdir()
        link = self.base / "link"
        link.symlink_to(target, target_is_directory=True)

        with self.assertRaises(_cache_storage().CacheStorageError):
            self._prepare_local(str(link), xdg=str(self.base / "xdg" / "cache"))

        self.assertEqual(list(target.iterdir()), [])

    def test_non_directory_selected_root_rejected(self) -> None:
        value = self.base / "file"
        value.write_text("not a directory")

        with self.assertRaises(_cache_storage().CacheStorageError):
            self._prepare_local(str(value), xdg=str(self.base / "xdg" / "cache"))

    def test_symlinked_descendant_rejected(self) -> None:
        xdg = self._xdg_dir()
        value = xdg / "docker-constructor-custom"
        value.mkdir()
        external = self.base / "external"
        external.mkdir()
        (value / "versioning").symlink_to(external, target_is_directory=True)

        with self.assertRaises(_cache_storage().CacheStorageError):
            self._prepare_local(str(value), xdg=str(xdg))

        self.assertEqual(list(external.iterdir()), [])
        self.assertEqual(sorted(p.name for p in value.iterdir()), ["versioning"])

    def test_non_directory_descendant_rejected(self) -> None:
        xdg = self._xdg_dir()
        value = xdg / "docker-constructor-custom"
        value.mkdir()
        os.chmod(value, 0o755)
        versioning = value / "versioning"
        versioning.write_text("not a directory")

        with self.assertRaises(_cache_storage().CacheStorageError):
            self._prepare_local(str(value), xdg=str(xdg))

        # Inspection happens before mutation: nothing is chmod-ed or created.
        self.assertEqual(_mode(value), 0o755)
        self.assertTrue(versioning.is_file())
        self.assertFalse((value / "runtime-artifacts").exists())

    def test_foreign_owned_directory_rejected(self) -> None:
        xdg = self._xdg_dir()
        value = xdg / "docker-constructor-custom"
        value.mkdir()
        os.chmod(value, 0o755)

        foreign_uid = os.getuid() + 1
        with mock.patch("os.fstat", side_effect=_foreign_uid_fstat(value, foreign_uid)):
            with self.assertRaises(_cache_storage().CacheStorageError) as caught:
                self._prepare_local(str(value), xdg=str(xdg))

        self.assertIn(str(value), str(caught.exception))
        self.assertEqual(_mode(value), 0o755)
        self.assertEqual(list(value.iterdir()), [])

    def test_foreign_owned_descendant_rejected(self) -> None:
        xdg = self._xdg_dir()
        value = xdg / "docker-constructor-custom"
        value.mkdir()
        os.chmod(value, 0o755)
        descendant = value / "versioning"
        descendant.mkdir()

        foreign_uid = os.getuid() + 1
        with mock.patch(
            "os.fstat", side_effect=_foreign_uid_fstat(descendant, foreign_uid)
        ):
            with self.assertRaises(_cache_storage().CacheStorageError) as caught:
                self._prepare_local(str(value), xdg=str(xdg))

        message = str(caught.exception).lower()
        self.assertIn(str(descendant), message)
        self.assertTrue(
            "restore" in message or "remove" in message,
            f"recovery guidance missing from: {caught.exception}",
        )
        self.assertEqual(_mode(value), 0o755)
        self.assertEqual(_mode(descendant), 0o755)
        self.assertFalse((value / "runtime-artifacts").exists())

    def test_unsecurable_existing_root_rejected(self) -> None:
        xdg = self._xdg_dir()
        value = xdg / "docker-constructor-custom"
        value.mkdir()
        os.chmod(value, 0o755)

        with mock.patch(
            "os.fchmod", side_effect=PermissionError(13, "Permission denied")
        ):
            with self.assertRaises(_cache_storage().CacheStorageError) as caught:
                self._prepare_local(str(value), xdg=str(xdg))

        message = str(caught.exception).lower()
        self.assertIn(str(value), message)
        self.assertTrue(
            "restore" in message or "remove" in message,
            f"recovery guidance missing from: {caught.exception}",
        )
        self.assertEqual(_mode(value), 0o755)
        self.assertEqual(list(value.iterdir()), [])

    def test_unsecurable_descendant_rejected(self) -> None:
        xdg = self._xdg_dir()
        value = xdg / "docker-constructor-custom"
        value.mkdir()
        os.chmod(value, 0o755)
        locks = value / "runtime-artifacts" / "locks"
        locks.mkdir(parents=True)
        os.chmod(locks, 0o755)

        with mock.patch("os.fchmod", side_effect=_deny_fchmod_for(locks)):
            with self.assertRaises(_cache_storage().CacheStorageError) as caught:
                self._prepare_local(str(value), xdg=str(xdg))

        message = str(caught.exception).lower()
        self.assertIn(str(locks), message)
        self.assertTrue(
            "restore" in message or "remove" in message,
            f"recovery guidance missing from: {caught.exception}",
        )
        self.assertEqual(_mode(locks), 0o755)
        self.assertFalse((value / "runtime-artifacts" / "tmp").exists())


class TestEntryNameValidation(_CacheSecurityTestCase):
    """Entry helpers reject non-basename names and never escape the
    prepared directory."""

    def _prepared_versioning(self) -> Path:
        xdg = self._xdg_dir()
        root = self._prepare_default(str(xdg))
        return root / "versioning"

    def test_unsafe_names_rejected_without_escape(self) -> None:
        cache_storage = _cache_storage()
        versioning = self._prepared_versioning()

        for bad in ("../outside", "nested/file", "/absolute", ".", "..", "", "a/"):
            with self.subTest(name=bad):
                with self.assertRaises(cache_storage.CacheStorageError):
                    cache_storage.open_private_entry(versioning, bad)
                with self.assertRaises(cache_storage.CacheStorageError):
                    cache_storage.publish_private_entry(versioning, bad, "final")
                with self.assertRaises(cache_storage.CacheStorageError):
                    cache_storage.publish_private_entry(versioning, "tmp", bad)

        # Nothing may have been created inside or escaped outside.
        self.assertEqual(sorted(p.name for p in versioning.iterdir()), [])
        self.assertFalse((versioning.parent / "outside").exists())
        self.assertFalse((self.base / "outside").exists())
