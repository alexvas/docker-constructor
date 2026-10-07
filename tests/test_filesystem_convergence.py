"""Phase 9 convergence contracts for the descriptor foundation.

The static audit binds the complete public foundation surface, compatibility
re-exports, explicit consumer dependencies, dependency direction, sole secure
walker, and migrated directory-close ownership.  The runtime audit imports
one target per fresh interpreter and records all loaded project modules so an
accidental aggregate or reverse dependency cannot hide behind prior imports.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_DOCKER = _REPO / "docker"

_PUBLIC_DEFINITIONS = {
    "docker/filesystem/__init__.py": frozenset(),
    "docker/filesystem/cleanup.py": frozenset(
        {"CleanupFailures", "attach_secondary", "carry_secondary_diagnostics"}
    ),
    "docker/filesystem/operations.py": frozenset(
        {"DescriptorOps", "PosixDescriptorOps"}
    ),
    "docker/filesystem/descriptors.py": frozenset(
        {"DescriptorError", "UnsafeDescriptorError", "OwnedDescriptor", "DirectoryDescriptor"}
    ),
}
_CONSUMER_IMPORTS = {
    "docker/transactions/capabilities.py": frozenset(
        {"docker.filesystem.descriptors", "docker.filesystem.operations"}
    ),
    "docker/npm_environment/storage.py": frozenset(
        {"docker.filesystem.descriptors", "docker.filesystem.operations"}
    ),
    "docker/npm_environment/tree.py": frozenset(
        {"docker.filesystem.descriptors", "docker.filesystem.operations"}
    ),
    "docker/versioning/cache_storage.py": frozenset(
        {
            "docker.filesystem.cleanup",
            "docker.filesystem.descriptors",
            "docker.filesystem.operations",
        }
    ),
}
_COMPATIBILITY_IMPORTS = {
    "docker/transactions/cleanup.py": {
        "CleanupFailures": ("docker.filesystem.cleanup", "CleanupFailures")
    },
    "docker/transactions/errors.py": {
        "attach_secondary": ("docker.filesystem.cleanup", "attach_secondary"),
        "carry_secondary_diagnostics": (
            "docker.filesystem.cleanup",
            "carry_secondary_diagnostics",
        ),
    },
}
_MIGRATED_DIRECTORY_MODULES = frozenset(
    {
        "docker/npm_environment/storage.py",
        "docker/npm_environment/tree.py",
        "docker/versioning/cache_storage.py",
    }
)

# Importing an npm leaf normally executes the existing eager package
# initializer.  This is a narrowly documented compatibility baseline, not
# permission for storage.py or tree.py to import these layers directly.  Exact
# equality rejects any new aggregate leakage while preserving normal imports.
_NPM_PACKAGE_IMPORT_BASELINE = frozenset(
    {
        "docker",
        "docker.filesystem",
        "docker.filesystem.cleanup",
        "docker.filesystem.descriptors",
        "docker.filesystem.operations",
        "docker.npm_environment",
        "docker.npm_environment.assembler",
        "docker.npm_environment.errors",
        "docker.npm_environment.evidence",
        "docker.npm_environment.execution",
        "docker.npm_environment.identity",
        "docker.npm_environment.image_ref",
        "docker.npm_environment.lifecycle",
        "docker.npm_environment.lockfile",
        "docker.npm_environment.model",
        "docker.npm_environment.network",
        "docker.npm_environment.observability",
        "docker.npm_environment.preflight",
        "docker.npm_environment.publication",
        "docker.npm_environment.run_vector",
        "docker.npm_environment.semver_range",
        "docker.npm_environment.smoke",
        "docker.npm_environment.storage",
        "docker.npm_environment.streaming",
        "docker.npm_environment.tree",
        "docker.npm_environment.validation",
        "docker.transactions",
        "docker.transactions.capabilities",
        "docker.transactions.cleanup",
        "docker.transactions.codec",
        "docker.transactions.errors",
        "docker.transactions.locking",
        "docker.transactions.posix",
        "docker.transactions.regular",
        "docker.versioning",
        "docker.versioning.constraints",
        "docker.versioning.corporate_network",
        "docker.versioning.errors",
        "docker.versioning.integrity",
        "docker.versioning.model",
        "docker.versioning.npm_tarball",
        "docker.versioning.semver",
    }
)


def _sources() -> dict[str, str]:
    return {
        path.relative_to(_REPO).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(_DOCKER.rglob("*.py"))
    }


def _imports(tree: ast.AST) -> set[str]:
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


def _imported_names(tree: ast.AST) -> dict[str, tuple[str, str]]:
    result: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.module is None:
            continue
        for alias in node.names:
            result[alias.asname or alias.name] = (node.module, alias.name)
    return result


def _enclosing_function(parents: dict[ast.AST, ast.AST], node: ast.AST) -> str | None:
    current = parents.get(node)
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current.name
        current = parents.get(current)
    return None


def _static_violations(sources: dict[str, str]) -> list[str]:
    """Return all convergence violations in *sources* (also used for fixtures)."""
    violations: list[str] = []
    trees = {path: ast.parse(source, filename=path) for path, source in sources.items()}

    for path, expected in _PUBLIC_DEFINITIONS.items():
        tree = trees.get(path)
        if tree is None:
            violations.append(f"missing foundation module: {path}")
            continue
        actual = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and not node.name.startswith("_")
        }
        if actual != expected:
            violations.append(f"public definitions {path}: {sorted(actual)} != {sorted(expected)}")

    init_tree = trees.get("docker/filesystem/__init__.py")
    if init_tree is not None and any(
        isinstance(node, (ast.Import, ast.ImportFrom)) for node in ast.walk(init_tree)
    ):
        violations.append("docker.filesystem package initializer imports or re-exports names")

    for path, tree in trees.items():
        if not path.startswith("docker/filesystem/"):
            continue
        forbidden = sorted(
            module
            for module in _imports(tree)
            if module == "docker.transactions"
            or module.startswith("docker.transactions.")
            or module == "docker.npm_environment"
            or module.startswith("docker.npm_environment.")
            or module == "docker.versioning"
            or module.startswith("docker.versioning.")
        )
        if forbidden:
            violations.append(f"reverse dependencies {path}: {forbidden}")

    for path, expected in _CONSUMER_IMPORTS.items():
        tree = trees.get(path)
        if tree is None:
            violations.append(f"missing migrated consumer: {path}")
            continue
        imported = _imports(tree)
        explicit = {name for name in imported if name.startswith("docker.filesystem")}
        if explicit != expected:
            violations.append(f"foundation imports {path}: {sorted(explicit)} != {sorted(expected)}")
        if "docker.filesystem" in explicit:
            violations.append(f"aggregate foundation import: {path}")
        if path in {
            "docker/npm_environment/storage.py",
            "docker/npm_environment/tree.py",
        }:
            transaction_imports = sorted(
                name
                for name in imported
                if name == "docker.transactions"
                or name.startswith("docker.transactions.")
            )
            if transaction_imports:
                violations.append(
                    f"direct npm transaction dependency {path}: {transaction_imports}"
                )

    for path, expected in _COMPATIBILITY_IMPORTS.items():
        tree = trees.get(path)
        if tree is None:
            violations.append(f"missing compatibility module: {path}")
            continue
        names = _imported_names(tree)
        for public_name, origin in expected.items():
            if names.get(public_name) != origin:
                violations.append(f"compatibility import {path}:{public_name} != {origin}")

    # There is exactly one generic secure-walk definition.  Domain recursion
    # remains local, but consumers must not define another component walker.
    walkers = []
    for path, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "open_secure_path":
                walkers.append(f"{path}:{node.lineno}")
    expected_walker = ["docker/filesystem/descriptors.py:239"]
    if walkers != expected_walker:
        violations.append(f"secure walkers: {walkers} != {expected_walker}")

    # Migrated modules may raw-close regular files (tree._hash_file) or
    # pre-adoption raw descriptors (cache storage), but never an owned
    # directory descriptor.  Calls to capability.close are the required form.
    raw_close_allowlist = {
        ("docker/npm_environment/tree.py", "_hash_file_entry", "os"),
        # These injected closes release caller-owned regular-file descriptors
        # before capability adoption.  Both are protected by CleanupFailures.
        (
            "docker/versioning/cache_storage.py",
            "_release_file_and_capability",
            "ops",
        ),
        (
            "docker/versioning/cache_storage.py",
            "_release_file_keeping_primary",
            "ops",
        ),
    }
    for path in _MIGRATED_DIRECTORY_MODULES:
        tree = trees.get(path)
        if tree is None:
            continue
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "close":
                continue
            receiver = ast.unparse(node.func.value)
            if receiver != "os" and receiver != "ops" and not receiver.endswith("._ops"):
                continue
            owner = _enclosing_function(parents, node)
            if (path, owner, receiver) not in raw_close_allowlist:
                violations.append(
                    f"unclassified raw close {path}:{node.lineno} "
                    f"in {owner} via {receiver}.close"
                )
    return violations


_IMPORT_PROBE = """
import importlib
import json
import sys
importlib.import_module(sys.argv[1])
print(json.dumps(sorted(name for name in sys.modules if name == 'docker' or name.startswith('docker.'))))
"""


def _loaded_modules(target: str) -> list[str]:
    completed = subprocess.run(
        [sys.executable, "-c", _IMPORT_PROBE, target],
        cwd=_REPO,
        check=True,
        text=True,
        capture_output=True,
    )
    return json.loads(completed.stdout)


def _runtime_violations(matrix: dict[str, list[str]]) -> list[str]:
    violations: list[str] = []
    foundation_allowed = {
        "docker.filesystem": {"docker", "docker.filesystem"},
        "docker.filesystem.cleanup": {
            "docker", "docker.filesystem", "docker.filesystem.cleanup"
        },
        "docker.filesystem.operations": {
            "docker", "docker.filesystem", "docker.filesystem.operations"
        },
        "docker.filesystem.descriptors": {
            "docker", "docker.filesystem", "docker.filesystem.cleanup",
            "docker.filesystem.descriptors",
        },
    }
    for target, allowed in foundation_allowed.items():
        actual = set(matrix.get(target, []))
        if actual != allowed:
            violations.append(f"isolated foundation import {target}: {sorted(actual - allowed)}")

    cache_loaded = set(matrix.get("docker.versioning.cache_storage", []))
    cache_forbidden = sorted(
        name for name in cache_loaded
        if name == "docker.transactions" or name.startswith("docker.transactions.")
        or name == "docker.npm_environment" or name.startswith("docker.npm_environment.")
    )
    if cache_forbidden:
        violations.append(f"cache reverse/aggregate loading: {cache_forbidden}")

    for target in ("docker.npm_environment.storage", "docker.npm_environment.tree"):
        loaded = set(matrix.get(target, []))
        if loaded != _NPM_PACKAGE_IMPORT_BASELINE:
            added = sorted(loaded - _NPM_PACKAGE_IMPORT_BASELINE)
            missing = sorted(_NPM_PACKAGE_IMPORT_BASELINE - loaded)
            violations.append(
                f"npm package import baseline {target}: "
                f"added={added}, missing={missing}"
            )

    for target in (
        "docker.transactions.cleanup",
        "docker.transactions.errors",
        "docker.transactions.capabilities",
    ):
        loaded = set(matrix.get(target, []))
        forbidden = sorted(
            name for name in loaded
            if name == "docker.npm_environment" or name.startswith("docker.npm_environment.")
            or name == "docker.versioning" or name.startswith("docker.versioning.")
        )
        if forbidden:
            violations.append(f"transaction reverse loading {target}: {forbidden}")
        if "docker.filesystem.cleanup" not in loaded:
            violations.append(f"transaction compatibility path lacks cleanup foundation: {target}")
        if target.endswith("capabilities") and not {
            "docker.filesystem.descriptors", "docker.filesystem.operations"
        }.issubset(loaded):
            violations.append(f"transaction capability path lacks descriptor foundation: {target}")
    return violations


class RepositoryWideAstContractTests(unittest.TestCase):
    def test_repository_converges_on_one_descriptor_foundation(self) -> None:
        self.assertEqual(_static_violations(_sources()), [])

    def test_contract_detects_intentionally_violating_fixture(self) -> None:
        fixture = _sources()
        fixture["docker/filesystem/__init__.py"] = "from docker.transactions import DirectoryCapability\n"
        fixture["docker/npm_environment/storage.py"] += textwrap.dedent(
            """
            from docker.transactions.errors import TransactionError

            def open_secure_path():
                os.close(fd)

            def injected_raw_close(ops):
                ops.close(fd)

            class DuplicateOwner:
                def injected_member_close(self):
                    self._ops.close(self._fd)
            """
        )
        violations = _static_violations(fixture)
        self.assertTrue(any("initializer imports" in item for item in violations))
        self.assertTrue(any("secure walkers" in item for item in violations))
        self.assertTrue(any("direct npm transaction dependency" in item for item in violations))
        raw_close_violations = [
            item for item in violations if "unclassified raw close" in item
        ]
        self.assertEqual(len(raw_close_violations), 3)
        self.assertTrue(any("via os.close" in item for item in raw_close_violations))
        self.assertTrue(any("via ops.close" in item for item in raw_close_violations))
        self.assertTrue(any("via self._ops.close" in item for item in raw_close_violations))


class IsolatedImportMatrixTests(unittest.TestCase):
    TARGETS = (
        "docker.filesystem",
        "docker.filesystem.cleanup",
        "docker.filesystem.operations",
        "docker.filesystem.descriptors",
        "docker.transactions.cleanup",
        "docker.transactions.errors",
        "docker.transactions.capabilities",
        "docker.npm_environment.storage",
        "docker.npm_environment.tree",
        "docker.versioning.cache_storage",
    )

    def test_isolated_import_matrix_has_no_leakage(self) -> None:
        matrix = {target: _loaded_modules(target) for target in self.TARGETS}
        self.assertEqual(_runtime_violations(matrix), [])

    def test_matrix_detects_intentionally_violating_fixture(self) -> None:
        matrix = {target: ["docker", target] for target in self.TARGETS}
        matrix["docker.filesystem.cleanup"].append("docker.transactions")
        matrix["docker.versioning.cache_storage"].append("docker.transactions.locking")
        matrix["docker.transactions.cleanup"].append("docker.versioning.model")
        matrix["docker.npm_environment.storage"] = sorted(
            _NPM_PACKAGE_IMPORT_BASELINE | {"docker.constructor_cli"}
        )
        violations = _runtime_violations(matrix)
        self.assertTrue(any("isolated foundation import" in item for item in violations))
        self.assertTrue(any("cache reverse/aggregate loading" in item for item in violations))
        self.assertTrue(any("transaction reverse loading" in item for item in violations))
        self.assertTrue(any("npm package import baseline" in item for item in violations))


if __name__ == "__main__":
    unittest.main()
