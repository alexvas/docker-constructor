"""RED contracts for ``docker.versioning.cache_storage`` — Phase 1.

Phase 1 delivers a pure path-resolution layer only:

* ``resolve_default_root(xdg_cache_home, *, home) -> Path`` returns the
  explicit ``${XDG_CACHE_HOME}/docker-constructor`` candidate only when
  ``XDG_CACHE_HOME`` is non-empty and absolute, otherwise the
  ``~/.cache/docker-constructor`` fallback candidate.
* ``resolve_local_root(value, *, xdg_cache_home, home) -> Path | None``
  lexically validates a local ``[cache].dir`` override and returns the
  normalized dedicated root, or ``None`` when no override is configured.
* ``versioning_child(root) -> Path`` derives the HTTP cache child
  ``<root>/versioning``.
* ``runtime_artifacts_blobs_child(root) -> Path`` derives the verified
  artifact blob child ``<root>/runtime-artifacts/blobs``.
* ``CacheStorageError`` (a ``ValueError`` subclass) carries path-specific
  rejection diagnostics.

Filesystem inspection and mutation remain outside Phase 1.  Every
resolution and derivation test below executes inside a no-filesystem-I/O
guard so an implementation cannot pass by calling ``Path.resolve()``,
``stat()``, or any other filesystem API only on a subset of inputs.  These
tests intentionally precede the module implementation; run them before the
GREEN task and expect failures until ``cache_storage.py`` exists.
"""
from __future__ import annotations

import ast
import contextlib
import unittest
from pathlib import Path
from unittest import mock

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CACHE_STORAGE_PATH = _REPO_ROOT / "docker" / "versioning" / "cache_storage.py"

# Modules cache_storage.py must not import.  cache.py owns HTTP entry
# format/TTL; artifact_cache.py/verification.py own content addressing,
# blob verification, locks, and atomic publication; transports.py owns
# transport construction; constructor_cli.py and launcher.py are CLI and
# container-launch boundaries.
_FORBIDDEN_MODULES = frozenset(
    {
        "docker.constructor_cli",
        "docker.launcher",
        "docker.transactions",
        "docker.filesystem",
        "docker.versioning.transports",
        "docker.versioning.cache",
        "docker.versioning.artifact_cache",
        "docker.versioning.verification",
        "docker.versioning.runtime_verification",
        "docker.versioning.providers",
        "docker.versioning.providers.base",
    }
)


def _cache_storage():
    """Import the module under test lazily so RED failures are per-test."""
    from docker.versioning import cache_storage

    return cache_storage


def _imported_module_names(source: str) -> set[str]:
    """Return statically imported module names from *source*.

    Relative imports are resolved against the ``docker.versioning``
    package, which is where ``cache_storage.py`` lives.
    """
    tree = ast.parse(source)
    names: set[str] = set()
    package = "docker.versioning"

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            level = node.level or 0
            if level == 0:
                base = node.module
                if base:
                    names.add(base)
                    for alias in node.names:
                        names.add(f"{base}.{alias.name}")
                continue

            parts = package.split(".")
            prefix = parts[: max(0, len(parts) - (level - 1))]
            if node.module:
                base = ".".join(prefix + [node.module])
            else:
                base = ".".join(prefix)
            names.add(base)
            for alias in node.names:
                names.add(f"{base}.{alias.name}")

    return names


def _filesystem_guard(name: str):
    """Raise a clear failure if pure resolution touches the filesystem."""

    def _guard(*args, **kwargs):
        raise AssertionError(
            f"pure cache-root resolution must not access the filesystem "
            f"via {name}(...)"
        )

    return _guard


class _PureResolutionTestCase(unittest.TestCase):
    """Base for tests that require zero filesystem I/O during resolution."""

    @contextlib.contextmanager
    def assert_no_filesystem_io(self):
        path_methods = [
            "exists",
            "is_dir",
            "is_file",
            "is_symlink",
            "stat",
            "lstat",
            "mkdir",
            "resolve",
            "absolute",
            "read_text",
            "write_text",
            "read_bytes",
            "write_bytes",
            "touch",
            "unlink",
            "rmdir",
            "glob",
            "rglob",
            "iterdir",
            "open",
            "chmod",
            "rename",
            "replace",
            "symlink_to",
            "owner",
            "group",
        ]
        os_functions = [
            "stat",
            "lstat",
            "listdir",
            "scandir",
            "makedirs",
            "mkdir",
            "chmod",
            "access",
            "remove",
            "rmdir",
            "rename",
            "replace",
            "symlink",
            "readlink",
            "getcwd",
            "open",
            "fstat",
            "read",
            "write",
        ]
        os_path_functions = [
            "exists",
            "lexists",
            "isfile",
            "isdir",
            "islink",
            "realpath",
            "samefile",
        ]

        patchers: list = []
        for name in path_methods:
            patchers.append(
                mock.patch.object(Path, name, side_effect=_filesystem_guard(name))
            )
        for name in os_functions:
            patchers.append(
                mock.patch(f"os.{name}", side_effect=_filesystem_guard(name))
            )
        for name in os_path_functions:
            patchers.append(
                mock.patch(f"os.path.{name}", side_effect=_filesystem_guard(name))
            )
        patchers.append(
            mock.patch("builtins.open", side_effect=_filesystem_guard("open"))
        )

        for patcher in patchers:
            patcher.start()
        try:
            yield
        finally:
            for patcher in reversed(patchers):
                patcher.stop()

    def _guarded(self, fn, *args, **kwargs):
        """Run *fn* inside the no-filesystem-I/O guard and return its result."""
        with self.assert_no_filesystem_io():
            return fn(*args, **kwargs)


class TestCacheStorageModuleBoundary(unittest.TestCase):
    """1.1 — the module must be acyclic and import no forbidden module."""

    def test_module_is_importable_and_exposes_pure_api(self) -> None:
        cache_storage = _cache_storage()
        self.assertTrue(callable(cache_storage.resolve_default_root))
        self.assertTrue(callable(cache_storage.resolve_local_root))
        self.assertTrue(callable(cache_storage.versioning_child))
        self.assertTrue(callable(cache_storage.runtime_artifacts_blobs_child))
        self.assertTrue(issubclass(cache_storage.CacheStorageError, ValueError))

    def test_module_imports_no_forbidden_modules(self) -> None:
        source = _CACHE_STORAGE_PATH.read_text()
        imported = _imported_module_names(source)
        forbidden = imported & _FORBIDDEN_MODULES
        self.assertEqual(
            forbidden,
            set(),
            "cache_storage.py must not import: " + ", ".join(sorted(forbidden)),
        )


class TestDefaultRootResolution(_PureResolutionTestCase):
    """1.2 — pure resolution of the XDG-based default constructor root."""

    def _resolve(self, xdg: str | None, home: Path) -> Path:
        cache_storage = _cache_storage()
        return self._guarded(
            cache_storage.resolve_default_root, xdg, home=home
        )

    def test_explicit_absolute_xdg_produces_candidate(self) -> None:
        root = self._resolve("/xdg/cache", Path("/home/user"))
        self.assertEqual(root, Path("/xdg/cache/docker-constructor"))

    def test_trailing_slash_xdg_produces_candidate(self) -> None:
        root = self._resolve("/xdg/cache/", Path("/home/user"))
        self.assertEqual(root, Path("/xdg/cache/docker-constructor"))

    def test_empty_xdg_falls_back_to_home_cache(self) -> None:
        root = self._resolve("", Path("/home/user"))
        self.assertEqual(root, Path("/home/user/.cache/docker-constructor"))

    def test_none_xdg_falls_back_to_home_cache(self) -> None:
        root = self._resolve(None, Path("/home/user"))
        self.assertEqual(root, Path("/home/user/.cache/docker-constructor"))

    def test_relative_xdg_falls_back_to_home_cache(self) -> None:
        for value in ("relative", "./cache", "../cache"):
            with self.subTest(value=value):
                root = self._resolve(value, Path("/home/user"))
                self.assertEqual(root, Path("/home/user/.cache/docker-constructor"))

    def test_tilde_prefixed_xdg_falls_back_to_home_cache(self) -> None:
        root = self._resolve("~/cache", Path("/home/user"))
        self.assertEqual(root, Path("/home/user/.cache/docker-constructor"))


class TestLocalRootResolution(_PureResolutionTestCase):
    """1.3 / 1.4 — lexical validation of a local ``[cache].dir`` override."""

    XDG = "/xdg/cache"
    HOME = Path("/home/testuser")

    def _resolve(self, value: str | None) -> Path | None:
        cache_storage = _cache_storage()
        return self._guarded(
            cache_storage.resolve_local_root,
            value,
            xdg_cache_home=self.XDG,
            home=self.HOME,
        )

    def _assert_rejected(self, value: str, pattern: str | None = None) -> None:
        cache_storage = _cache_storage()
        with self.assert_no_filesystem_io():
            if pattern is None:
                with self.assertRaises(cache_storage.CacheStorageError):
                    cache_storage.resolve_local_root(
                        value, xdg_cache_home=self.XDG, home=self.HOME
                    )
            else:
                with self.assertRaisesRegex(cache_storage.CacheStorageError, pattern):
                    cache_storage.resolve_local_root(
                        value, xdg_cache_home=self.XDG, home=self.HOME
                    )

    # ── acceptance ───────────────────────────────────────────────

    def test_absent_local_root_returns_none(self) -> None:
        self.assertIsNone(self._resolve(None))

    def test_absolute_dedicated_child_of_xdg_accepted(self) -> None:
        root = self._resolve("/xdg/cache/docker-constructor-custom")
        self.assertEqual(root, Path("/xdg/cache/docker-constructor-custom"))

    def test_absolute_non_xdg_dedicated_root_accepted(self) -> None:
        root = self._resolve("/opt/constructor-cache")
        self.assertEqual(root, Path("/opt/constructor-cache"))

    def test_lexical_variants_of_dedicated_child_normalized(self) -> None:
        variants = {
            "/xdg/cache/docker-constructor-custom/": (
                "/xdg/cache/docker-constructor-custom"
            ),
            "/xdg/cache/docker-constructor-custom/.": (
                "/xdg/cache/docker-constructor-custom"
            ),
            "/xdg/cache/./docker-constructor-custom": (
                "/xdg/cache/docker-constructor-custom"
            ),
            "/xdg/cache/sub/../docker-constructor-custom": (
                "/xdg/cache/docker-constructor-custom"
            ),
        }
        for value, expected in variants.items():
            with self.subTest(value=value):
                self.assertEqual(self._resolve(value), Path(expected))

    # ── rejection: shape ─────────────────────────────────────────

    def test_empty_local_root_rejected(self) -> None:
        self._assert_rejected("", pattern="absolute")

    def test_relative_local_root_rejected(self) -> None:
        for value in ("cache", "./cache", "../cache", "relative/cache"):
            with self.subTest(value=value):
                self._assert_rejected(value, pattern="absolute")

    def test_tilde_prefixed_local_root_rejected(self) -> None:
        for value in ("~/cache", "~/.cache/docker-constructor"):
            with self.subTest(value=value):
                self._assert_rejected(value, pattern="absolute")

    # ── rejection: unsafe absolute roots ─────────────────────────

    def test_xdg_cache_home_rejected(self) -> None:
        self._assert_rejected("/xdg/cache", pattern="XDG_CACHE_HOME")

    def test_home_directory_rejected(self) -> None:
        self._assert_rejected("/home/testuser", pattern="dedicated")

    def test_filesystem_root_rejected(self) -> None:
        self._assert_rejected("/", pattern="dedicated")

    def test_ancestor_of_xdg_rejected(self) -> None:
        for value in ("/xdg", "/"):
            with self.subTest(value=value):
                self._assert_rejected(value)

    def test_lexical_equivalents_of_unsafe_roots_rejected(self) -> None:
        equivalents = [
            "/xdg/cache/",            # equal XDG (trailing slash)
            "/xdg/cache/.",           # equal XDG (dot segment)
            "/xdg/cache/sub/..",      # equal XDG (dot-dot collapse)
            "/xdg/./cache",           # equal XDG
            "/home/testuser/.",       # equal HOME
            "/home/testuser/sub/..",  # equal HOME
            "/xdg/cache/..",          # ancestor of XDG
            "/xdg/..",                # filesystem root
            "/.",                     # filesystem root
        ]
        for value in equivalents:
            with self.subTest(value=value):
                self._assert_rejected(value)

    def test_double_leading_slash_roots_rejected(self) -> None:
        # ``os.path.normpath`` preserves ``//`` while Linux resolves it to
        # ``/``; canonicalization must collapse it before unsafe-root checks
        # so ``//`` cannot bypass the filesystem-root, XDG, or ancestor guard.
        cases = {
            "//": "dedicated",
            "//xdg/cache": "XDG_CACHE_HOME",
            "//xdg/cache/": "XDG_CACHE_HOME",
            "//xdg/./cache": "XDG_CACHE_HOME",
            "//xdg/cache/sub/..": "XDG_CACHE_HOME",
            "//xdg": "dedicated",
            "//xdg/..": "dedicated",
            "//home/testuser": "dedicated",
            "//home/testuser/.": "dedicated",
        }
        for value, pattern in cases.items():
            with self.subTest(value=value):
                self._assert_rejected(value, pattern=pattern)


class TestCacheChildDerivation(_PureResolutionTestCase):
    """Phase 1 — derive named children from a resolved dedicated root."""

    XDG = "/xdg/cache"
    HOME = Path("/home/testuser")

    def _default_root(self) -> Path:
        cache_storage = _cache_storage()
        return self._guarded(
            cache_storage.resolve_default_root, self.XDG, home=self.HOME
        )

    def _local_root(self) -> Path:
        cache_storage = _cache_storage()
        return self._guarded(
            cache_storage.resolve_local_root,
            "/xdg/cache/docker-constructor-custom",
            xdg_cache_home=self.XDG,
            home=self.HOME,
        )

    def _derive(self, root: Path) -> tuple[Path, Path]:
        cache_storage = _cache_storage()
        with self.assert_no_filesystem_io():
            versioning = cache_storage.versioning_child(root)
            blobs = cache_storage.runtime_artifacts_blobs_child(root)
        return versioning, blobs

    def test_versioning_child_from_default_root(self) -> None:
        root = self._default_root()
        versioning, _ = self._derive(root)
        self.assertEqual(versioning, root / "versioning")

    def test_runtime_artifacts_blobs_child_from_default_root(self) -> None:
        root = self._default_root()
        _, blobs = self._derive(root)
        self.assertEqual(blobs, root / "runtime-artifacts" / "blobs")

    def test_children_from_valid_local_root(self) -> None:
        root = self._local_root()
        versioning, blobs = self._derive(root)
        self.assertEqual(versioning, root / "versioning")
        self.assertEqual(blobs, root / "runtime-artifacts" / "blobs")

    def test_children_are_distinct_named_subtrees(self) -> None:
        root = self._default_root()
        versioning, blobs = self._derive(root)
        self.assertNotEqual(versioning, blobs)
        self.assertEqual(versioning.parent, root)
        self.assertEqual(blobs.parent, root / "runtime-artifacts")


class TestPureResolutionFunctionsHaveNoFilesystemIO(_PureResolutionTestCase):
    """8.10 — every pure resolution function stays filesystem-free.

    The capability migration must not leak filesystem access into the
    lexical layer, so every public resolver and named-child derivation runs
    under the no-filesystem-I/O guard.
    """

    def test_resolution_and_derivation_never_touch_the_filesystem(self) -> None:
        cache_storage = _cache_storage()
        home = Path("/home/testuser")
        with self.assert_no_filesystem_io():
            cache_storage.resolve_default_root("/xdg/cache", home=home)
            cache_storage.resolve_default_root(None, home=home)
            cache_storage.resolve_local_root(
                "/xdg/cache/custom", xdg_cache_home="/xdg/cache", home=home
            )
            cache_storage.resolve_local_root(
                None, xdg_cache_home="/xdg/cache", home=home
            )
            root = cache_storage.resolve_effective_root(
                None, xdg_cache_home="/xdg/cache", home=home
            )
            cache_storage.versioning_child(root)
            cache_storage.runtime_artifacts_child(root)
            cache_storage.runtime_artifacts_blobs_child(root)
            cache_storage.runtime_artifacts_tmp_child(root)
            cache_storage.runtime_artifacts_locks_child(root)
