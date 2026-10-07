"""Phase 8 — cache-storage descriptor-capability migration.

``docker.versioning.cache_storage`` owns cache-root resolution, XDG
behavior, ``0700`` mode policy, recovery guidance, and the
``CacheStorageError`` mapping.  This module pins the descriptor-ownership
boundary it now shares with ``docker.filesystem``:

* a parent-release failure during the secure cache walk is never retried and
  never leaks an already-opened child;
* cache validation/creation/publication failures stay authoritative over an
  ordinary descriptor-close failure, which is retained as secondary
  diagnostic context;
* the module imports only the explicit lightweight foundation submodules and
  no aggregate package, transaction substrate, or higher cache consumer;
* the pure resolution layer still performs no filesystem I/O;
* the foundation keeps its acyclic direction.

The tests patch ``os`` and inject ``DescriptorOps`` so they exercise the
production ``PosixDescriptorOps`` adapter as well as the capability seam.
"""
from __future__ import annotations

import ast
import errno
import os
import stat
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from docker.filesystem.operations import PosixDescriptorOps

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CACHE_STORAGE_PATH = _REPO_ROOT / "docker" / "versioning" / "cache_storage.py"
_FOUNDATION = _REPO_ROOT / "docker" / "filesystem"


def _cache_storage():
    """Import the module under test lazily so RED failures are per-test."""
    from docker.versioning import cache_storage

    return cache_storage


def _fd_path(fd: int) -> str | None:
    """Return the normalized path an open descriptor refers to, or ``None``."""
    try:
        return os.path.normpath(os.readlink(f"/proc/self/fd/{fd}"))
    except OSError:
        return None


def _openat_target(directory_fd: int | None, name: str) -> str | None:
    """Return the normalized path an ``openat`` call would resolve to."""
    if directory_fd is None:
        return os.path.normpath(name)
    parent = _fd_path(directory_fd)
    if parent is None:
        return None
    return os.path.normpath(os.path.join(parent, name))


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def _secondary_exceptions(exc: BaseException) -> list[object]:
    """Return the secondary cleanup diagnostics retained by *exc*."""
    secondary = getattr(exc, "secondary", None)
    if isinstance(secondary, list):
        return list(secondary)
    slot = getattr(exc, "_transaction_secondary", None)
    if isinstance(slot, list):
        return list(slot)
    return list(getattr(exc, "__notes__", []))


def _carries_secondary(exc: BaseException, needle: BaseException) -> bool:
    """True when *exc* retains *needle* as secondary diagnostic context."""
    for item in _secondary_exceptions(exc):
        if item is needle:
            return True
        if isinstance(item, str) and str(needle) in item:
            return True
    return False


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


def _fail_close_on(target: Path, error: OSError):
    """Build an ``os.close`` fake that fails only for *target*'s descriptor."""
    real_close = os.close
    target_norm = os.path.normpath(str(target))

    def fake_close(fd, *args, **kwargs):
        if _fd_path(fd) == target_norm:
            real_close(fd)
            raise error
        return real_close(fd, *args, **kwargs)

    return fake_close


def _fail_nth_fstat(target: Path, n: int, error: OSError):
    """Build an ``os.fstat`` fake failing on the *n*-th call for *target*.

    ``open_secure_path()`` validates the adopted leaf with an ``fstat`` before
    ``_open_directory_for_write()`` performs its own privacy check, so a
    count-aware fault targets the second call without defeating adoption.
    """
    real_fstat = os.fstat
    target_norm = os.path.normpath(str(target))
    calls = {"count": 0}

    def fake_fstat(fd, *args, **kwargs):
        if _fd_path(fd) == target_norm:
            calls["count"] += 1
            if calls["count"] >= n:
                raise error
        return real_fstat(fd, *args, **kwargs)

    return fake_fstat


class _LedgerOps(PosixDescriptorOps):
    """Descriptor backend recording live descriptors and injecting faults.

    ``openat`` records every returned descriptor as live; ``close`` flags an
    attempt on a descriptor that is not live (a double close) and clears it on
    the way out, so a leaked descriptor is distinguishable from a retried
    close across descriptor-number reuse.  Faults are keyed by the resolved
    descriptor path so they stay stable under descriptor-number reuse.
    """

    def __init__(self) -> None:
        self.open_order: list[int] = []
        self.open_paths: list[tuple[int, str | None]] = []
        self.close_order: list[int] = []
        self.close_paths: list[str | None] = []
        self.live: set[int] = set()
        self.double_closes: list[int] = []
        self.close_errors: dict[str, OSError] = {}
        self.openat_errors: dict[str, OSError] = {}
        self.fchmod_errors: dict[str, OSError] = {}
        self.fstat_uids: dict[str, int] = {}
        self.fstat_calls: dict[str, int] = {}
        self.fstat_errors: dict[str, tuple[int, BaseException]] = {}
        self.fstat_failures: dict[str, dict[int, BaseException]] = {}
        self.mkdir_order: list[tuple[str | None, str]] = []
        self.chmod_order: list[tuple[str | None, int]] = []

    def openat(self, directory_fd, name, flags, mode=0):
        target = _openat_target(directory_fd, name)
        error = self.openat_errors.get(target) if target is not None else None
        if error is not None:
            raise error
        fd = super().openat(directory_fd, name, flags, mode)
        self.open_order.append(fd)
        self.open_paths.append((fd, _fd_path(fd)))
        self.live.add(fd)
        return fd

    def close(self, fd: int) -> None:
        path = _fd_path(fd)
        self.close_order.append(fd)
        self.close_paths.append(path)
        if fd not in self.live:
            self.double_closes.append(fd)
        try:
            super().close(fd)
        finally:
            self.live.discard(fd)
        error = self.close_errors.get(path) if path is not None else None
        if error is not None:
            raise error

    def fstat(self, fd: int):
        path = _fd_path(fd)
        if path is not None:
            count = self.fstat_calls.get(path, 0) + 1
            self.fstat_calls[path] = count
            failure = self.fstat_errors.get(path)
            if failure is not None and count >= failure[0]:
                raise failure[1]
            one_shot = self.fstat_failures.get(path)
            if one_shot is not None and count in one_shot:
                raise one_shot[count]
        info = super().fstat(fd)
        uid = self.fstat_uids.get(path) if path is not None else None
        if uid is None:
            return info
        return types.SimpleNamespace(
            st_mode=info.st_mode, st_uid=uid, st_gid=info.st_gid
        )

    def fchmod(self, fd: int, mode: int) -> None:
        path = _fd_path(fd)
        self.chmod_order.append((path, mode))
        error = self.fchmod_errors.get(path) if path is not None else None
        if error is not None:
            raise error
        super().fchmod(fd, mode)

    def mkdirat(self, directory_fd: int, name: str, mode: int) -> None:
        self.mkdir_order.append((_fd_path(directory_fd), name))
        super().mkdirat(directory_fd, name, mode)


class _CacheTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="cache-storage-cap-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.root = self.base / "tree"
        self.root.mkdir()


# ---------------------------------------------------------------------------
# 8.1 — parent-release failure must not retry or leak the opened child
# ---------------------------------------------------------------------------


class TestCacheWalkerReleasePrecedence(_CacheTestCase):
    """A raw parent/child handoff could retry a failed parent close and leak
    the already-opened child."""

    def test_parent_close_failure_is_not_retried_and_child_is_released(self):
        # Exercise the production ``PosixDescriptorOps`` adapter by patching
        # ``os.open``/``os.close`` directly, so the case runs against the raw
        # handoff shape as well as the migrated capability walk.
        real_open = os.open
        real_close = os.close
        opened: list[int] = []
        closed: dict[int, int] = {}
        target: list[int] = []

        def fake_open(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            opened.append(fd)
            if not target:
                # The first descriptor opened is the filesystem root the
                # secure walk retains before its first handoff.
                target.append(fd)
            return fd

        def fake_close(fd, *args, **kwargs):
            closed[fd] = closed.get(fd, 0) + 1
            if target and fd == target[0]:
                real_close(fd)
                raise OSError(errno.EIO, "injected parent close failure")
            return real_close(fd, *args, **kwargs)

        with mock.patch("os.open", side_effect=fake_open), mock.patch(
            "os.close", side_effect=fake_close
        ):
            with self.assertRaises(OSError):
                _cache_storage().prepare_resolved_root(self.root)

        # The failing parent is closed at most once (never retried), and every
        # descriptor the failed operation opened receives a release attempt so
        # the already-opened child cannot leak.
        self.assertEqual(closed[target[0]], 1)
        self.assertEqual(set(opened) - set(closed), set())

    def test_opened_child_is_released_when_parent_close_fails(self):
        ops = _LedgerOps()
        ops.close_errors["/"] = OSError(
            errno.EIO, "injected parent close failure"
        )

        with self.assertRaises(OSError):
            _cache_storage().prepare_resolved_root(self.root, ops=ops)

        self.assertEqual(ops.close_paths.count("/"), 1)
        self.assertEqual(ops.double_closes, [])
        self.assertEqual(ops.live, set())


# ---------------------------------------------------------------------------
# 8.5 — active parent-walk validation failure stays primary over close
# ---------------------------------------------------------------------------


class TestParentWalkValidationPrecedence(_CacheTestCase):
    """``_open_parent_fd()`` must keep an active parent-component validation
    failure primary when releasing the current directory capability fails."""

    def test_unsafe_parent_component_stays_primary_over_current_close(self):
        cache_storage = _cache_storage()
        # A regular file where a parent component is expected makes the secure
        # walk reject the component one level above the root, while ``base``
        # is the live capability that must then be released.
        unsafe = self.base / "unsafe"
        unsafe.write_text("not a directory")
        root = unsafe / "tree"
        base_path = os.path.normpath(str(self.base))
        close_error = OSError(errno.EIO, "injected current close failure")
        ops = _LedgerOps()
        ops.close_errors[base_path] = close_error

        with self.assertRaises(cache_storage.CacheStorageError) as ctx:
            cache_storage.prepare_resolved_root(root, ops=ops)

        self.assertIn("unsafe parent component", str(ctx.exception))
        self.assertTrue(_carries_secondary(ctx.exception, close_error))
        # The failing capability is released exactly once and nothing leaks.
        self.assertEqual(ops.close_paths.count(base_path), 1)
        self.assertEqual(ops.double_closes, [])
        self.assertEqual(ops.live, set())


# ---------------------------------------------------------------------------
# 8.2 — validation/creation/publication failures stay primary over close
# ---------------------------------------------------------------------------


class TestCachePrimaryFailureOverClose(_CacheTestCase):
    """An ordinary descriptor-close failure must never replace an active
    cache validation, ownership, mode, creation, or publication failure."""

    def test_ownership_failure_stays_primary_over_parent_close(self):
        close_error = OSError(errno.EIO, "injected parent close failure")
        foreign_uid = os.getuid() + 1
        with mock.patch(
            "os.fstat", side_effect=_foreign_uid_fstat(self.root, foreign_uid)
        ), mock.patch(
            "os.close", side_effect=_fail_close_on(self.root.parent, close_error)
        ):
            with self.assertRaises(_cache_storage().CacheStorageError) as ctx:
                _cache_storage().prepare_resolved_root(self.root)

        self.assertIn("not owned", str(ctx.exception))
        self.assertTrue(_carries_secondary(ctx.exception, close_error))

    def test_type_failure_stays_primary_over_parent_close(self):
        path = self.base / "regular-file"
        path.write_text("not a directory")
        close_error = OSError(errno.EIO, "injected parent close failure")
        with mock.patch(
            "os.close", side_effect=_fail_close_on(path.parent, close_error)
        ):
            with self.assertRaises(_cache_storage().CacheStorageError) as ctx:
                _cache_storage().prepare_resolved_root(path)

        self.assertIn("cache entry", str(ctx.exception))
        self.assertTrue(_carries_secondary(ctx.exception, close_error))

    def test_mode_failure_stays_primary_over_parent_close(self):
        os.chmod(self.root, 0o755)
        close_error = OSError(errno.EIO, "injected parent close failure")
        root_norm = os.path.normpath(str(self.root))
        parent_norm = os.path.normpath(str(self.root.parent))
        real_fchmod = os.fchmod
        real_close = os.close
        state = {"securing": False}

        def fake_fchmod(fd, mode, *args, **kwargs):
            if _fd_path(fd) == root_norm:
                state["securing"] = True
                raise PermissionError(13, "Permission denied")
            return real_fchmod(fd, mode, *args, **kwargs)

        def fake_close(fd, *args, **kwargs):
            if _fd_path(fd) == parent_norm and state["securing"]:
                real_close(fd, *args, **kwargs)
                raise close_error
            return real_close(fd, *args, **kwargs)

        with mock.patch("os.fchmod", side_effect=fake_fchmod), mock.patch(
            "os.close", side_effect=fake_close
        ):
            with self.assertRaises(_cache_storage().CacheStorageError) as ctx:
                _cache_storage().prepare_resolved_root(self.root)

        self.assertIn("cannot secure", str(ctx.exception))
        self.assertTrue(_carries_secondary(ctx.exception, close_error))

    def test_publication_failure_stays_primary_over_directory_close(self):
        cache_storage = _cache_storage()
        versioning = self.root / "versioning"
        cache_storage.prepare_resolved_root(self.root)
        close_error = OSError(errno.EIO, "injected directory close failure")
        with mock.patch(
            "os.replace",
            side_effect=OSError(errno.EROFS, "read-only filesystem"),
        ), mock.patch(
            "os.close", side_effect=_fail_close_on(versioning, close_error)
        ):
            with self.assertRaises(cache_storage.CacheStorageError) as ctx:
                cache_storage.publish_private_entry(versioning, "tmp", "final")

        self.assertIn("cannot publish", str(ctx.exception))
        self.assertTrue(_carries_secondary(ctx.exception, close_error))

    def test_creation_ownership_failure_stays_primary_with_ledger(self):
        ops = _LedgerOps()
        ops.fstat_uids[str(self.root)] = os.getuid() + 1
        close_error = OSError(errno.EIO, "injected parent close failure")
        ops.close_errors[str(self.root.parent)] = close_error

        with self.assertRaises(_cache_storage().CacheStorageError) as ctx:
            _cache_storage().prepare_resolved_root(self.root, ops=ops)

        self.assertIn("not owned", str(ctx.exception))
        self.assertTrue(_carries_secondary(ctx.exception, close_error))
        self.assertEqual(ops.double_closes, [])
        self.assertEqual(ops.live, set())


# ---------------------------------------------------------------------------
# 8.5/8.7 — explicit-XDG success path releases every walk capability
# ---------------------------------------------------------------------------


class TestExplicitXdgLifecycle(_CacheTestCase):
    """A successful XDG walk must release its final directory descriptor."""

    def test_successful_explicit_xdg_walk_releases_every_capability(self):
        cache_storage = _cache_storage()
        xdg = self.base / "xdg-cache"
        ops = _LedgerOps()

        root = cache_storage.prepare_default_root(
            str(xdg), home=self.home, ops=ops
        )

        self.assertTrue(xdg.is_dir())
        self.assertTrue(root.is_dir())
        opened_paths = [path for _, path in ops.open_paths]
        # Every opened directory receives exactly one release attempt: no
        # successful-walk leaf is left live, and no descriptor is retried.
        self.assertEqual(sorted(opened_paths), sorted(ops.close_paths))
        self.assertEqual(ops.double_closes, [])
        self.assertEqual(ops.live, set())


# ---------------------------------------------------------------------------
# 8.6 — write-directory validation precedence and exactly-once release
# ---------------------------------------------------------------------------


class TestWriteDirectoryValidationPrecedence(_CacheTestCase):
    """``_open_directory_for_write()`` keeps its validation failure primary
    over an ordinary directory-close failure and releases exactly once."""

    def _prepared_versioning(self) -> tuple[object, Path]:
        cache_storage = _cache_storage()
        cache_storage.prepare_resolved_root(self.root)
        return cache_storage, self.root / "versioning"

    def test_fstat_failure_stays_primary_over_directory_close(self):
        cache_storage, versioning = self._prepared_versioning()
        fstat_error = OSError(errno.EIO, "injected fstat failure")
        close_error = OSError(errno.EIO, "injected directory close failure")
        ops = _LedgerOps()
        # The first ``fstat`` is the owner validation during adoption; fail
        # the second, the privacy/access check inside the write helper.
        ops.fstat_errors[str(versioning)] = (2, fstat_error)
        ops.close_errors[str(versioning)] = close_error

        with self.assertRaises(cache_storage.CacheStorageError) as ctx:
            cache_storage.open_private_entry(versioning, "entry", ops=ops)

        self.assertIn("cannot access", str(ctx.exception))
        self.assertIs(ctx.exception.__cause__, fstat_error)
        self.assertTrue(_carries_secondary(ctx.exception, close_error))
        self.assertEqual(ops.double_closes, [])
        self.assertEqual(ops.live, set())

    def test_mode_failure_stays_primary_over_directory_close(self):
        cache_storage, versioning = self._prepared_versioning()
        os.chmod(versioning, 0o755)
        close_error = OSError(errno.EIO, "injected directory close failure")
        ops = _LedgerOps()
        ops.close_errors[str(versioning)] = close_error

        with self.assertRaises(cache_storage.CacheStorageError) as ctx:
            cache_storage.open_private_entry(versioning, "entry", ops=ops)

        self.assertIn("not private", str(ctx.exception))
        self.assertTrue(_carries_secondary(ctx.exception, close_error))
        self.assertEqual(ops.double_closes, [])
        self.assertEqual(ops.live, set())

    def test_unexpected_defect_fstat_failure_is_reraised_and_released_once(self):
        """A non-``OSError`` privacy-check defect must release the adopted
        capability exactly once and be re-raised unchanged rather than
        remapped to ``CacheStorageError``."""
        cache_storage, versioning = self._prepared_versioning()
        failure = RuntimeError("injected unexpected fstat defect")
        ops = _LedgerOps()
        ops.fstat_errors[str(versioning)] = (2, failure)

        with self.assertRaises(RuntimeError) as ctx:
            cache_storage.open_private_entry(versioning, "entry", ops=ops)

        self.assertIs(ctx.exception, failure)
        self.assertEqual(ops.close_paths.count(str(versioning)), 1)
        self.assertEqual(ops.double_closes, [])
        self.assertEqual(ops.live, set())

    def test_process_control_fstat_failure_is_reraised_and_released_once(self):
        """A ``KeyboardInterrupt`` during the privacy check must not leak the
        adopted capability and must propagate unchanged."""
        cache_storage, versioning = self._prepared_versioning()
        failure = KeyboardInterrupt()
        ops = _LedgerOps()
        ops.fstat_errors[str(versioning)] = (2, failure)

        with self.assertRaises(KeyboardInterrupt) as ctx:
            cache_storage.open_private_entry(versioning, "entry", ops=ops)

        self.assertIs(ctx.exception, failure)
        self.assertEqual(ops.close_paths.count(str(versioning)), 1)
        self.assertEqual(ops.double_closes, [])
        self.assertEqual(ops.live, set())


# ---------------------------------------------------------------------------
# 8.7 — regular-file handoff when the owning directory close fails
# ---------------------------------------------------------------------------


class TestOpenPrivateEntryHandoff(_CacheTestCase):
    """``open_private_entry()`` must not return an opened file descriptor until
    the owning directory capability has closed, and must never leak the file
    when that directory close fails after the file is open and secured."""

    def _prepared_versioning(self) -> tuple[object, Path]:
        cache_storage = _cache_storage()
        cache_storage.prepare_resolved_root(self.root)
        return cache_storage, self.root / "versioning"

    def test_directory_close_failure_releases_opened_file(self):
        cache_storage, versioning = self._prepared_versioning()
        dir_error = OSError(errno.EIO, "injected directory close failure")
        ops = _LedgerOps()
        ops.close_errors[str(versioning)] = dir_error

        with self.assertRaises(OSError) as ctx:
            cache_storage.open_private_entry(versioning, "entry", ops=ops)

        # The directory-close failure stays primary and no descriptor is
        # returned; the secured regular file is released exactly once.
        self.assertIs(ctx.exception, dir_error)
        self.assertIn(str(versioning / "entry"), ops.close_paths)
        self.assertEqual(ops.double_closes, [])
        self.assertEqual(ops.live, set())
        opened_paths = [path for _, path in ops.open_paths]
        self.assertEqual(sorted(opened_paths), sorted(ops.close_paths))

    def test_combined_directory_and_file_close_failure_keeps_directory_primary(self):
        cache_storage, versioning = self._prepared_versioning()
        dir_error = OSError(errno.EIO, "injected directory close failure")
        file_error = OSError(errno.EIO, "injected file close failure")
        ops = _LedgerOps()
        ops.close_errors[str(versioning)] = dir_error
        ops.close_errors[str(versioning / "entry")] = file_error

        with self.assertRaises(OSError) as ctx:
            cache_storage.open_private_entry(versioning, "entry", ops=ops)

        self.assertIs(ctx.exception, dir_error)
        self.assertTrue(_carries_secondary(ctx.exception, file_error))
        self.assertEqual(ops.double_closes, [])
        self.assertEqual(ops.live, set())
        opened_paths = [path for _, path in ops.open_paths]
        self.assertEqual(sorted(opened_paths), sorted(ops.close_paths))

    def test_secure_failure_stays_primary_over_directory_close(self):
        cache_storage, versioning = self._prepared_versioning()
        secure_error = OSError(errno.EIO, "injected fchmod failure")
        dir_error = OSError(errno.EIO, "injected directory close failure")
        ops = _LedgerOps()
        ops.fchmod_errors[str(versioning / "entry")] = secure_error
        ops.close_errors[str(versioning)] = dir_error

        with self.assertRaises(cache_storage.CacheStorageError) as ctx:
            cache_storage.open_private_entry(versioning, "entry", ops=ops)

        # The domain "cannot secure" error stays primary; the directory-close
        # failure is retained as secondary and both descriptors are released.
        self.assertIn("cannot secure", str(ctx.exception))
        self.assertTrue(_carries_secondary(ctx.exception, dir_error))
        self.assertEqual(ops.double_closes, [])
        self.assertEqual(ops.live, set())
        opened_paths = [path for _, path in ops.open_paths]
        self.assertEqual(sorted(opened_paths), sorted(ops.close_paths))


# ---------------------------------------------------------------------------
# 8.3 / 8.8 — explicit foundation imports and dependency boundary
# ---------------------------------------------------------------------------


def _imported_module_paths(source: str) -> set[str]:
    """Return statically imported module names without collapsing dotted paths.

    Relative imports keep their bare ``node.module`` (``from .errors import``
    -> ``errors``) so the leaf allowlist can name stdlib and sibling modules.
    """
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                if node.module:
                    modules.add(node.module)
            elif node.module:
                modules.add(node.module)
    return modules


class TestCacheStorageFoundationAdoption(unittest.TestCase):
    """8.3 / 8.8 — only the explicit lightweight submodules are admitted."""

    def _source(self) -> str:
        return _CACHE_STORAGE_PATH.read_text(encoding="utf-8")

    def test_adopts_explicit_foundation_submodules(self):
        modules = _imported_module_paths(self._source())
        self.assertIn("docker.filesystem.descriptors", modules)
        self.assertIn("docker.filesystem.operations", modules)

    def test_rejects_aggregate_and_transaction_imports(self):
        modules = _imported_module_paths(self._source())
        self.assertNotIn("docker.filesystem", modules)
        self.assertNotIn("docker.transactions", modules)
        self.assertFalse(
            sorted(m for m in modules if m.startswith("docker.transactions."))
        )

    def test_rejects_higher_cache_consumers(self):
        modules = _imported_module_paths(self._source())
        for forbidden in (
            "docker.versioning.cache",
            "docker.versioning.artifact_cache",
            "docker.versioning.verification",
            "docker.versioning.transports",
            "docker.constructor_cli",
            "docker.launcher",
        ):
            self.assertNotIn(forbidden, modules)


# ---------------------------------------------------------------------------
# 8.9 / 8.11 — ownership boundary and reverse dependency
# ---------------------------------------------------------------------------


def _defines(tree: ast.AST, name: str) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == name:
                return True
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return True
    return False


class TestCacheStorageFoundationBoundary(unittest.TestCase):
    """cache_storage owns no raw directory close and keeps its domain API."""

    def _source(self) -> str:
        return _CACHE_STORAGE_PATH.read_text(encoding="utf-8")

    def test_has_no_raw_owned_directory_close(self):
        source = self._source()
        raw_close = [
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "close"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "os"
        ]
        self.assertEqual(raw_close, [])
        self.assertNotIn("os.close", source)

    def test_still_owns_cache_root_resolution_and_security_policy(self):
        tree = ast.parse(self._source())
        for name in (
            "resolve_default_root",
            "resolve_local_root",
            "resolve_effective_root",
            "prepare_default_root",
            "prepare_local_root",
            "prepare_resolved_root",
            "prepare_project_root",
            "open_private_entry",
            "publish_private_entry",
            "_normalize",
            "_CACHE_ROOT_NAME",
        ):
            self.assertTrue(_defines(tree, name), name)

    def test_foundation_imports_no_domain_or_transaction_package(self):
        forbidden = ("docker.versioning", "docker.transactions", "docker.npm_environment")
        offenders: list[tuple[str, str]] = []
        for path in sorted(_FOUNDATION.rglob("*.py")):
            modules = _imported_module_paths(path.read_text(encoding="utf-8"))
            for module in modules:
                if module.startswith(forbidden):
                    offenders.append((str(path.relative_to(_REPO_ROOT)), module))
        self.assertEqual([], offenders)


# ---------------------------------------------------------------------------
# 8.4 — pre-migration characterization (passes before and after migration)
# ---------------------------------------------------------------------------


class TestCacheStorageCharacterization(_CacheTestCase):
    """Unchanged domain behavior the migration must preserve."""

    def test_local_cache_config_error_mapping_is_pinned(self):
        cache_storage = _cache_storage()
        from docker.versioning.errors import InventoryError

        self.assertEqual(
            cache_storage.parse_local_cache_config(None, None).dir, None
        )
        with self.assertRaises(InventoryError):
            cache_storage.parse_local_cache_config("not-a-table", None)
        with self.assertRaises(InventoryError):
            cache_storage.parse_local_cache_config({"unknown": "x"}, None)
        with self.assertRaises(InventoryError):
            cache_storage.parse_local_cache_config({"dir": 5}, None)

    def test_unsafe_local_root_mapping_is_pinned(self):
        cache_storage = _cache_storage()
        with self.assertRaises(cache_storage.CacheStorageError):
            cache_storage.resolve_local_root(
                "relative", xdg_cache_home=None, home=self.home
            )
        with self.assertRaises(cache_storage.CacheStorageError):
            cache_storage.resolve_local_root(
                "/", xdg_cache_home=None, home=self.home
            )
        self.assertIsNone(
            cache_storage.resolve_local_root(
                None, xdg_cache_home=None, home=self.home
            )
        )

    def test_parent_mutation_and_recovery_guidance_are_pinned(self):
        cache_storage = _cache_storage()
        xdg = self.base / "xdg" / "cache"
        xdg.mkdir(parents=True)
        os.chmod(xdg, 0o755)
        root = cache_storage.prepare_default_root(str(xdg), home=self.home)
        self.assertEqual(_mode(xdg), 0o755)
        self.assertEqual(_mode(root), 0o700)

        unsafe = xdg / "docker-constructor-custom"
        unsafe.mkdir()
        os.chmod(unsafe, 0o755)
        (unsafe / "versioning").write_text("not a directory")
        with self.assertRaises(cache_storage.CacheStorageError) as ctx:
            cache_storage.prepare_local_root(
                str(unsafe), xdg_cache_home=str(xdg), home=self.home
            )
        message = str(ctx.exception).lower()
        self.assertTrue(
            "remove" in message or "restore" in message, message
        )


# ---------------------------------------------------------------------------
# 8.5/8.7 — ENOENT is absence only for an initial open, never for validation
# ---------------------------------------------------------------------------


class TestStageNarrowedAbsenceHandling(_CacheTestCase):
    """A validation-stage ``ENOENT`` (``fstat`` after a successful open) is a
    real failure: it must propagate with close diagnostics, leave permissions
    untouched, and never trigger creation or hardening.  A genuinely missing
    entry still maps to absence and is still created."""

    def test_cache_root_validation_enoent_propagates_without_mutation(self):
        cache_storage = _cache_storage()
        os.chmod(self.root, 0o755)
        validation_error = OSError(errno.ENOENT, "injected validation ENOENT")
        close_error = OSError(errno.EIO, "injected root close failure")
        ops = _LedgerOps()
        # The first ``fstat`` is the adoption/validation of the existing root;
        # a swallowed error would let later hardening run and chmod the tree.
        ops.fstat_failures[str(self.root)] = {1: validation_error}
        ops.close_errors[str(self.root)] = close_error

        with self.assertRaises(cache_storage.CacheStorageError) as ctx:
            cache_storage.prepare_resolved_root(self.root, ops=ops)

        self.assertIn("unsafe cache entry", str(ctx.exception))
        self.assertTrue(_carries_secondary(ctx.exception.__cause__, close_error))
        self.assertEqual(ops.mkdir_order, [])
        self.assertEqual(ops.chmod_order, [])
        self.assertEqual(_mode(self.root), 0o755)
        self.assertEqual(ops.close_paths.count(str(self.root)), 1)
        self.assertEqual(ops.double_closes, [])
        self.assertEqual(ops.live, set())

    def test_explicit_xdg_validation_enoent_propagates_without_creation(self):
        cache_storage = _cache_storage()
        xdg = self.base / "xdg-cache"
        xdg.mkdir()
        os.chmod(xdg, 0o755)
        validation_error = OSError(errno.ENOENT, "injected validation ENOENT")
        close_error = OSError(errno.EIO, "injected xdg close failure")
        ops = _LedgerOps()
        ops.fstat_failures[str(xdg)] = {1: validation_error}
        ops.close_errors[str(xdg)] = close_error

        with self.assertRaises(cache_storage.CacheStorageError) as ctx:
            cache_storage.prepare_default_root(str(xdg), home=self.home, ops=ops)

        self.assertIn(
            "cannot inspect XDG_CACHE_HOME component", str(ctx.exception)
        )
        self.assertTrue(_carries_secondary(ctx.exception.__cause__, close_error))
        self.assertEqual(ops.mkdir_order, [])
        self.assertEqual(_mode(xdg), 0o755)
        self.assertEqual(ops.close_paths.count(str(xdg)), 1)
        self.assertEqual(ops.double_closes, [])
        self.assertEqual(ops.live, set())

    def test_write_directory_validation_enoent_is_not_absence(self):
        cache_storage = _cache_storage()
        cache_storage.prepare_resolved_root(self.root)
        versioning = self.root / "versioning"
        validation_error = OSError(errno.ENOENT, "injected validation ENOENT")
        close_error = OSError(errno.EIO, "injected versioning close failure")
        ops = _LedgerOps()
        # The first ``fstat`` validates the adopted leaf; a misclassified
        # absence would report "does not exist" instead of the real failure.
        ops.fstat_failures[str(versioning)] = {1: validation_error}
        ops.close_errors[str(versioning)] = close_error

        with self.assertRaises(cache_storage.CacheStorageError) as ctx:
            cache_storage.open_private_entry(versioning, "entry", ops=ops)

        self.assertNotIn("does not exist", str(ctx.exception))
        self.assertIn("unsafe cache entry", str(ctx.exception))
        self.assertTrue(_carries_secondary(ctx.exception.__cause__, close_error))
        self.assertEqual(ops.chmod_order, [])
        self.assertEqual(ops.close_paths.count(str(versioning)), 1)
        self.assertEqual(ops.double_closes, [])
        self.assertEqual(ops.live, set())

    def test_genuinely_missing_descendant_is_still_created(self):
        cache_storage = _cache_storage()
        versioning = self.root / "versioning"
        self.assertFalse(versioning.exists())

        cache_storage.prepare_resolved_root(self.root)

        self.assertTrue(versioning.is_dir())
        self.assertEqual(_mode(versioning), 0o700)

    def test_genuinely_missing_xdg_component_is_still_created(self):
        cache_storage = _cache_storage()
        xdg = self.base / "missing" / "xdg-cache"

        root = cache_storage.prepare_default_root(str(xdg), home=self.home)

        self.assertTrue(xdg.is_dir())
        self.assertEqual(_mode(xdg), 0o700)
        self.assertTrue(root.is_dir())

    def test_genuinely_missing_write_directory_still_reports_absence(self):
        cache_storage = _cache_storage()
        missing = self.root / "versioning"

        with self.assertRaises(cache_storage.CacheStorageError) as ctx:
            cache_storage.open_private_entry(missing, "entry")

        self.assertIn("does not exist", str(ctx.exception))


# ---------------------------------------------------------------------------
# 8.5/8.7 — root-acquisition failures are mapped to CacheStorageError
# ---------------------------------------------------------------------------


class TestRootAcquisitionDomainMapping(_CacheTestCase):
    """A ``DescriptorError`` from the initial root acquisition in
    ``_open_parent_fd()`` or ``_prepare_explicit_xdg()`` must be translated to
    ``CacheStorageError`` so callers keep their existing ``OSError``/``ValueError``
    handling, while chaining and cleanup diagnostics stay reachable."""

    def setUp(self) -> None:
        super().setUp()
        self.xdg = self.base / "xdg"
        self.xdg.mkdir()
        os.chmod(self.xdg, 0o755)
        os.chmod(self.root, 0o755)

    def _invoke_parent(self, ops):
        return _cache_storage().prepare_resolved_root(self.root, ops=ops)

    def _invoke_xdg(self, ops):
        return _cache_storage().prepare_default_root(
            str(self.xdg), home=self.home, ops=ops
        )

    def _assert_no_mutation(self, ops, watched: Path):
        self.assertEqual(ops.mkdir_order, [])
        self.assertEqual(ops.chmod_order, [])
        self.assertEqual(_mode(watched), 0o755)
        self.assertEqual(ops.live, set())
        self.assertEqual(ops.double_closes, [])

    def test_parent_root_open_emfile_is_domain_mapped(self):
        cache_storage = _cache_storage()
        open_error = OSError(errno.EMFILE, "injected root open EMFILE")
        ops = _LedgerOps()
        ops.openat_errors[os.sep] = open_error

        with self.assertRaises(cache_storage.CacheStorageError) as ctx:
            self._invoke_parent(ops)

        self.assertIn("cannot access parent directory", str(ctx.exception))
        self.assertIn("restore access or retry", str(ctx.exception))
        self.assertIs(type(ctx.exception.__cause__).__name__, "DescriptorError")
        self.assertIs(ctx.exception.__cause__.cause, open_error)
        # The factory never returned a descriptor, so nothing was opened or
        # released; the foundation owns the (empty) cleanup.
        self.assertEqual(ops.open_paths, [])
        self.assertEqual(ops.close_paths, [])
        self._assert_no_mutation(ops, self.root)

    def test_xdg_root_open_emfile_is_domain_mapped(self):
        cache_storage = _cache_storage()
        open_error = OSError(errno.EMFILE, "injected root open EMFILE")
        ops = _LedgerOps()
        ops.openat_errors[os.sep] = open_error

        with self.assertRaises(cache_storage.CacheStorageError) as ctx:
            self._invoke_xdg(ops)

        self.assertIn("XDG_CACHE_HOME", str(ctx.exception))
        self.assertIn("cannot access filesystem root", str(ctx.exception))
        self.assertIs(type(ctx.exception.__cause__).__name__, "DescriptorError")
        self.assertIs(ctx.exception.__cause__.cause, open_error)
        self.assertEqual(ops.open_paths, [])
        self.assertEqual(ops.close_paths, [])
        self._assert_no_mutation(ops, self.xdg)

    def test_parent_root_validation_eio_is_domain_mapped(self):
        cache_storage = _cache_storage()
        validation_error = OSError(errno.EIO, "injected root validation EIO")
        ops = _LedgerOps()
        ops.fstat_failures[os.sep] = {1: validation_error}

        with self.assertRaises(cache_storage.CacheStorageError) as ctx:
            self._invoke_parent(ops)

        self.assertIn("cannot access parent directory", str(ctx.exception))
        self.assertIs(type(ctx.exception.__cause__).__name__, "DescriptorError")
        self.assertIs(ctx.exception.__cause__.cause, validation_error)
        # Adoption released the failed root exactly once.
        self.assertEqual(ops.close_paths.count(os.sep), 1)
        self.assertEqual(len(ops.open_paths), len(ops.close_paths))
        self._assert_no_mutation(ops, self.root)

    def test_xdg_root_validation_eio_is_domain_mapped(self):
        cache_storage = _cache_storage()
        validation_error = OSError(errno.EIO, "injected root validation EIO")
        ops = _LedgerOps()
        ops.fstat_failures[os.sep] = {1: validation_error}

        with self.assertRaises(cache_storage.CacheStorageError) as ctx:
            self._invoke_xdg(ops)

        self.assertIn("XDG_CACHE_HOME", str(ctx.exception))
        self.assertIs(type(ctx.exception.__cause__).__name__, "DescriptorError")
        self.assertIs(ctx.exception.__cause__.cause, validation_error)
        self.assertEqual(ops.close_paths.count(os.sep), 1)
        self.assertEqual(len(ops.open_paths), len(ops.close_paths))
        self._assert_no_mutation(ops, self.xdg)

    def test_parent_root_validation_and_close_failure_retains_diagnostics(self):
        cache_storage = _cache_storage()
        validation_error = OSError(errno.EIO, "injected root validation EIO")
        close_error = OSError(errno.EIO, "injected root close failure")
        ops = _LedgerOps()
        ops.fstat_failures[os.sep] = {1: validation_error}
        ops.close_errors[os.sep] = close_error

        with self.assertRaises(cache_storage.CacheStorageError) as ctx:
            self._invoke_parent(ops)

        self.assertIn("cannot access parent directory", str(ctx.exception))
        self.assertIs(type(ctx.exception.__cause__).__name__, "DescriptorError")
        self.assertIs(ctx.exception.__cause__.cause, validation_error)
        self.assertTrue(
            _carries_secondary(ctx.exception.__cause__, close_error)
        )
        self.assertEqual(ops.close_paths.count(os.sep), 1)
        self.assertEqual(len(ops.open_paths), len(ops.close_paths))
        self._assert_no_mutation(ops, self.root)

    def test_xdg_root_validation_and_close_failure_retains_diagnostics(self):
        cache_storage = _cache_storage()
        validation_error = OSError(errno.EIO, "injected root validation EIO")
        close_error = OSError(errno.EIO, "injected root close failure")
        ops = _LedgerOps()
        ops.fstat_failures[os.sep] = {1: validation_error}
        ops.close_errors[os.sep] = close_error

        with self.assertRaises(cache_storage.CacheStorageError) as ctx:
            self._invoke_xdg(ops)

        self.assertIn("XDG_CACHE_HOME", str(ctx.exception))
        self.assertIs(type(ctx.exception.__cause__).__name__, "DescriptorError")
        self.assertIs(ctx.exception.__cause__.cause, validation_error)
        self.assertTrue(
            _carries_secondary(ctx.exception.__cause__, close_error)
        )
        self.assertEqual(ops.close_paths.count(os.sep), 1)
        self.assertEqual(len(ops.open_paths), len(ops.close_paths))
        self._assert_no_mutation(ops, self.xdg)


if __name__ == "__main__":
    unittest.main()
