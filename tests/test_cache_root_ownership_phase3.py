"""Phase 3 cache-root ownership tests.

``docker.versioning.cache_storage`` is the sole authority for the shared
constructor cache root.  This module records that ownership and the rule
that keeps it intact:

* Consumers may call the public cache-storage selectors and preparation
  functions (``resolve_default_root``, ``resolve_local_root``,
  ``resolve_effective_root``, ``prepare_*_root``), and may use thin
  adapters that add no cache policy of their own.
* Consumers must not reproduce normalization, fallback, dangerous-root,
  no-follow, permission, or canonical-child rules outside
  ``cache_storage.py``.

Phase 3 found no duplicated root-selection policy; these tests preserve
that ownership rather than driving a production change.
"""
from __future__ import annotations

import ast
import inspect
import textwrap
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DOCKER = _REPO_ROOT / "docker"
_CACHE_STORAGE = _DOCKER / "versioning" / "cache_storage.py"

# The cache-owned authority API.  These names must be defined by
# ``cache_storage.py`` and nowhere else.
_AUTHORITY_FUNCTIONS = (
    "resolve_default_root",
    "resolve_local_root",
    "resolve_effective_root",
    "prepare_default_root",
    "prepare_local_root",
    "prepare_resolved_root",
    "prepare_project_root",
    "versioning_child",
    "runtime_artifacts_child",
    "runtime_artifacts_blobs_child",
    "runtime_artifacts_locks_child",
    "runtime_artifacts_tmp_child",
)

# Modules that select, prepare, or consume the *shared* constructor cache
# root.  These are the modules where a re-implementation of cache policy
# would be a real ownership regression.
_SHARED_CACHE_CONSUMERS = (
    "docker/versioning/project_state.py",
    "docker/versioning/transports.py",
    "docker/versioning/build_orchestration.py",
    "docker/versioning/build_cache.py",
    "docker/versioning/effective.py",
    "docker/versioning/cache.py",
    "docker/versioning/local_project_configuration.py",
    "docker/constructor_cli.py",
    "docker/launcher.py",
)

# Cache-policy primitives that must stay in ``cache_storage.py`` when they
# act on the shared cache root.
_POLICY_PRIMITIVES = frozenset({
    "normpath", "isabs", "islink", "is_symlink", "realpath", "lstat",
    "chmod", "mkdir", "makedirs",
})

# Environment/home/root expressions that must not be compared against a
# cache root outside cache storage (reading them to *pass into* a
# cache-storage API is delegation, not policy).
_DANGEROUS_ROOT_NAMES = frozenset({
    "home", "expanduser", "environ", "XDG_CACHE_HOME", "geteuid", "/",
})

# Canonical shared-child names that only cache-storage helpers may
# assemble into a path.
_CANONICAL_CHILD_NAMES = frozenset({"versioning", "runtime-artifacts"})


def _docker_sources() -> list[Path]:
    return sorted(_DOCKER.rglob("*.py"))


def _call_name(func: ast.AST) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _names_in(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.add(child.id)
        elif isinstance(child, ast.Attribute):
            names.add(child.attr)
        elif isinstance(child, ast.keyword) and child.arg:
            names.add(child.arg)
        elif isinstance(child, ast.Constant) and isinstance(child.value, str):
            names.add(child.value)
    return names


def _mentions_shared_cache(node: ast.AST) -> bool:
    return any("cache" in name.lower() for name in _names_in(node))


def _canonical_child_constructions(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, name)`` for literal canonical child paths."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            for operand in (node.left, node.right):
                if (
                    isinstance(operand, ast.Constant)
                    and operand.value in _CANONICAL_CHILD_NAMES
                ):
                    found.append((node.lineno, operand.value))
        elif isinstance(node, ast.Call) and _call_name(node.func) == "join":
            for arg in node.args:
                if (
                    isinstance(arg, ast.Constant)
                    and arg.value in _CANONICAL_CHILD_NAMES
                ):
                    found.append((node.lineno, arg.value))
    return found


class _Phase3TestCase(unittest.TestCase):
    maxDiff = None

    def _source(self, path: Path) -> str:
        return path.read_text(encoding="utf-8")

    def _consumer_sources(self) -> list[tuple[str, ast.AST]]:
        return [
            (relative, ast.parse(self._source(_REPO_ROOT / relative)))
            for relative in _SHARED_CACHE_CONSUMERS
        ]


class TestCacheStorageOwnsTheAuthorityAPI(_Phase3TestCase):
    """Every authority function is defined by cache_storage and by no one else."""

    def test_every_authority_function_is_defined_by_cache_storage(self) -> None:
        from docker.versioning import cache_storage

        missing = [
            name for name in _AUTHORITY_FUNCTIONS
            if not callable(getattr(cache_storage, name, None))
        ]
        self.assertEqual([], missing)

    def test_no_other_module_defines_an_authority_function(self) -> None:
        offenders: list[tuple[str, str]] = []
        authority = set(_AUTHORITY_FUNCTIONS)
        for path in _docker_sources():
            if path == _CACHE_STORAGE:
                continue
            tree = ast.parse(self._source(path))
            for node in tree.body:
                if isinstance(node, ast.FunctionDef) and node.name in authority:
                    offenders.append((str(path.relative_to(_REPO_ROOT)), node.name))
        self.assertEqual([], offenders)


class TestConsumersDelegateToCacheStorage(_Phase3TestCase):
    """Consumers obtain root/child decisions from cache-owned APIs."""

    def test_consumers_call_cache_owned_apis(self) -> None:
        expected = {
            "docker/versioning/transports.py": {"prepare_default_root", "prepare_local_root"},
            "docker/versioning/project_state.py": {"prepare_default_root"},
            "docker/versioning/build_orchestration.py": {"prepare_project_root", "resolve_effective_root"},
            "docker/versioning/build_cache.py": {"resolve_default_root"},
            "docker/constructor_cli.py": {"prepare_resolved_root", "resolve_effective_root"},
            "docker/launcher.py": {"prepare_resolved_root", "resolve_effective_root"},
        }
        for relative, api in expected.items():
            with self.subTest(module=relative):
                tree = ast.parse(self._source(_REPO_ROOT / relative))
                called = {
                    _call_name(node.func)
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Call)
                }
                self.assertTrue(
                    api & called,
                    f"{relative} does not call any of {sorted(api)}",
                )

    def test_build_cache_thin_adapter_adds_no_policy(self) -> None:
        """The one consumer adapter may be thin; it must add no policy."""
        from docker.versioning import build_cache

        source = inspect.getsource(build_cache._resolve_cache_root)
        tree = ast.parse(textwrap.dedent(source))
        called = {
            _call_name(node.func)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
        }
        policy = called & _POLICY_PRIMITIVES
        self.assertEqual(set(), policy, "adapter must only delegate")
        self.assertIn("resolve_default_root", called)


class TestNoDuplicatedCachePolicy(_Phase3TestCase):
    """No shared-cache consumer may re-implement cache storage policy.

    Scope rule: the primitives under test are flagged only when they act
    on a value whose name denote the shared constructor cache root (an
    identifier or attribute containing ``cache``), or when they assemble a
    canonical shared child.  Passing ``XDG_CACHE_HOME``/``home`` *into* a
    cache-storage API is delegation and is never flagged.

    ``artifact_cache.py`` is deliberately out of scope: its ``cache_root``
    parameter is the project-scoped artifact containment root, an
    independently owned policy, not the shared constructor cache root.
    """

    def test_no_shared_cache_consumer_normalizes_or_validates_the_root(self) -> None:
        offenders: list[tuple[str, int, str]] = []
        for relative, tree in self._consumer_sources():
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = _call_name(node.func)
                if name in _POLICY_PRIMITIVES and _mentions_shared_cache(node):
                    offenders.append((relative, node.lineno, name))
        self.assertEqual([], offenders)

    def test_no_shared_cache_consumer_compares_cache_root_to_home_or_xdg(self) -> None:
        offenders: list[tuple[str, int]] = []
        for relative, tree in self._consumer_sources():
            for node in ast.walk(tree):
                if not isinstance(node, ast.Compare):
                    continue
                names = _names_in(node)
                if (
                    any("cache" in name.lower() for name in names)
                    and names & _DANGEROUS_ROOT_NAMES
                ):
                    offenders.append((relative, node.lineno))
        self.assertEqual([], offenders)

    def test_no_module_outside_cache_storage_builds_canonical_children(self) -> None:
        offenders: list[tuple[str, int, str]] = []
        for path in _docker_sources():
            if path == _CACHE_STORAGE:
                continue
            tree = ast.parse(self._source(path))
            for lineno, name in _canonical_child_constructions(tree):
                offenders.append((str(path.relative_to(_REPO_ROOT)), lineno, name))
        self.assertEqual([], offenders)


class TestAggregateCacheParsingStaysLightweight(_Phase3TestCase):
    """The aggregate owns the ``[cache]`` table shape/type/default only."""

    def test_aggregate_cache_parser_has_no_environment_or_filesystem_logic(self) -> None:
        from docker.versioning import cache_storage

        source = inspect.getsource(cache_storage.parse_local_cache_config)
        tree = ast.parse(textwrap.dedent(source))
        identifiers: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                identifiers.add(node.id)
            elif isinstance(node, ast.Attribute):
                identifiers.add(node.attr)
        forbidden = {
            "environ", "isabs", "normpath", "resolve", "resolve_local_root",
            "resolve_default_root", "prepare_local_root", "prepare_default_root",
            "lstat", "stat", "chmod", "mkdir", "islink", "is_symlink",
            "expanduser", "home", "owner", "getuid", "geteuid",
        }
        self.assertEqual([], sorted(identifiers & forbidden))
        strings = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        self.assertEqual([], [s for s in strings if "XDG" in s])
        # It still owns the table shape, unknown fields, type, and default.
        self.assertIn("host_access_mode", source)
        self.assertIn("unknown", source)

    def test_configured_dir_remains_untrusted_until_cache_storage_resolves_it(self) -> None:
        from docker.versioning.local_project_configuration import validate_local_document

        for raw in ("relative/path", "", "/"):
            with self.subTest(dir=raw):
                config = validate_local_document(
                    {"cache": {"dir": raw}}, host_access_mode=None
                )
                # Aggregate parsing neither rejects nor rewrites the value.
                self.assertEqual(raw, config.cache.dir)

    def test_cache_storage_rejects_the_same_untrusted_values(self) -> None:
        from docker.versioning.cache_storage import (
            CacheStorageError,
            resolve_local_root,
        )

        for raw in ("relative/path", "", "/"):
            with self.subTest(dir=raw):
                with self.assertRaises(CacheStorageError):
                    resolve_local_root(
                        raw,
                        xdg_cache_home="/home/user/.cache",
                        home=Path("/home/user"),
                    )


class TestDetectorIsNotVacuous(_Phase3TestCase):
    """The duplicate-policy detector flags a synthetic re-implementation."""

    _SYNTHETIC = textwrap.dedent(
        """
        import os
        from pathlib import Path

        def leak(cache_root):
            normalized = os.path.normpath(cache_root)
            if os.path.isabs(cache_root) or cache_root == "/":
                return Path.home() == cache_root
            child = cache_root / "versioning"
            return os.path.join(cache_root, "runtime-artifacts")
        """
    )

    def test_detector_flags_normalization_validation_and_children(self) -> None:
        tree = ast.parse(self._SYNTHETIC)
        primitives = [
            _call_name(node.func)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and _call_name(node.func) in _POLICY_PRIMITIVES
            and _mentions_shared_cache(node)
        ]
        self.assertIn("normpath", primitives)
        self.assertIn("isabs", primitives)

        comparisons = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Compare)
            and any("cache" in n.lower() for n in _names_in(node))
            and _names_in(node) & _DANGEROUS_ROOT_NAMES
        ]
        # Two comparisons: ``cache_root == "/"`` and ``Path.home() == cache_root``.
        self.assertEqual(2, len(comparisons))

        children = _canonical_child_constructions(tree)
        self.assertEqual(
            sorted(["runtime-artifacts", "versioning"]),
            sorted(name for _line, name in children),
        )


if __name__ == "__main__":
    unittest.main()
