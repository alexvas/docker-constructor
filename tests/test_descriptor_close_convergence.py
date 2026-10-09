"""Phase 1 convergence contracts for the npm single-descriptor lifecycle.

Task 1.8 binds the two migrated npm functions to the shared
``docker.filesystem`` lifecycle:

* ``_write_lockfile`` and ``_hash_file_entry`` transfer each opened
  regular-file descriptor into an ``OwnedDescriptor`` immediately;
* neither function issues a direct ``os.close``, an injected raw backend
  close, or a bespoke release-state machine; and
* both modules import only the explicit lightweight foundation submodules,
  while recursion, hashing, lockfile semantics, and domain errors remain in
  the npm modules.

The close gate resolves symbol origins rather than matching spellings.  It
follows ``import os as ...``, ``from os import close as ...``, transitive
local assignments of a raw close callable, and arbitrarily named descriptor
backends.  Any ``.close(...)`` call not provably bound to a shared
``OwnedDescriptor`` is rejected, so a renamed injected call such as
``backend.close(fd)`` cannot evade the gate.  Fixtures in
:class:`CloseGateEvasionTests` pin each evasion path.

The file is intentionally named ``test_descriptor_close_convergence.py`` so
Phase 7 can extend it into the repository-wide convergence gate.
"""
from __future__ import annotations

import ast
import textwrap
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_NPM = _REPO / "docker" / "npm_environment"

#: module path -> migrated function that must own its descriptor.
_MIGRATED = {
    _NPM / "execution.py": "_write_lockfile",
    _NPM / "tree.py": "_hash_file_entry",
}

#: Release-state spellings that only the shared owner may implement.
_BESPOKE_RELEASE_NAMES = frozenset(
    {"_RELEASED", "_TRANSFERRED", "_LIVE", "_state", "_released"}
)

#: Fully-qualified raw close primitives.  ``posix.close`` is included because
#: the POSIX adapter is the sole permitted direct caller.
_RAW_CLOSE_QUALNAMES = frozenset({"os.close", "posix.close"})

#: Fully-qualified shared-owner constructors.  A close call on a value bound
#: to one of these is the only permitted close inside a migrated function.
_OWNER_QUALNAMES = frozenset(
    {
        "OwnedDescriptor",
        "docker.filesystem.descriptors.OwnedDescriptor",
    }
)


def _parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"))


def _find_function(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            node.name == name
        ):
            return node
    raise AssertionError(f"function {name!r} not found")


def _find_method(tree: ast.AST, class_name: str, method_name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                    item.name == method_name
                ):
                    return item
    raise AssertionError(f"method {class_name}.{method_name} not found")


def _module_aliases(tree: ast.AST) -> dict[str, str]:
    """Map each imported local name to its fully-qualified dotted origin."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    aliases[alias.asname] = alias.name
                else:
                    root = alias.name.split(".")[0]
                    aliases[root] = root
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if alias.name == "*":
                    continue
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return aliases


def _qualname(expr: ast.expr, aliases: dict[str, str]) -> str:
    """Resolve an expression to a dotted origin using import aliases."""
    if isinstance(expr, ast.Name):
        return aliases.get(expr.id, expr.id)
    if isinstance(expr, ast.Attribute):
        base = _qualname(expr.value, aliases)
        return f"{base}.{expr.attr}" if base else expr.attr
    return ""


def _base_names(expr: ast.expr) -> set[str]:
    """Return the root/last-attribute names of an attribute chain."""
    names: set[str] = set()
    node = expr
    while isinstance(node, ast.Attribute):
        names.add(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        names.add(node.id)
    return names


def _is_owner_call(value: ast.expr | None, aliases: dict[str, str]) -> bool:
    return isinstance(value, ast.Call) and (
        _qualname(value.func, aliases) in _OWNER_QUALNAMES
    )


def _owner_names(
    function: ast.FunctionDef, aliases: dict[str, str]
) -> set[str]:
    """Names provably bound to a live shared ``OwnedDescriptor``."""
    owners: set[str] = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Assign) and _is_owner_call(node.value, aliases):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    owners.add(target.id)
        elif (
            isinstance(node, ast.AnnAssign)
            and _is_owner_call(node.value, aliases)
            and isinstance(node.target, ast.Name)
        ):
            owners.add(node.target.id)
        elif isinstance(node, ast.NamedExpr) and _is_owner_call(
            node.value, aliases
        ):
            if isinstance(node.target, ast.Name):
                owners.add(node.target.id)
        elif isinstance(node, ast.withitem) and _is_owner_call(
            node.context_expr, aliases
        ):
            if isinstance(node.optional_vars, ast.Name):
                owners.add(node.optional_vars.id)
    # Propagate aliases: ``alias = owner`` (fixpoint for arbitrary chains).
    changed = True
    while changed:
        changed = False
        for node in ast.walk(function):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Name):
                if node.value.id in owners:
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id not in owners:
                            owners.add(target.id)
                            changed = True
    return owners


def _raw_close_names(
    function: ast.FunctionDef, aliases: dict[str, str]
) -> set[str]:
    """Names bound to a raw close primitive, including aliases."""
    names = {
        name for name, origin in aliases.items() if origin in _RAW_CLOSE_QUALNAMES
    }
    changed = True
    while changed:
        changed = False
        for node in ast.walk(function):
            value: ast.expr | None = None
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                value, targets = node.value, list(node.targets)
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                value, targets = node.value, [node.target]
            elif isinstance(node, ast.NamedExpr):
                value, targets = node.value, [node.target]
            if value is None:
                continue
            origin = _qualname(value, aliases)
            is_raw = origin in _RAW_CLOSE_QUALNAMES or (
                isinstance(value, ast.Name) and value.id in names
            )
            if not is_raw:
                continue
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in names:
                    names.add(target.id)
                    changed = True
    return names


def _close_violations(
    function: ast.FunctionDef, aliases: dict[str, str]
) -> list[str]:
    """Return every close-like call not provably bound to the shared owner.

    The gate fails closed: a ``.close(...)`` call whose receiver cannot be
    resolved to an ``OwnedDescriptor`` (or an alias of one) is a violation,
    regardless of the receiver's spelling.
    """
    owners = _owner_names(function, aliases)
    raw = _raw_close_names(function, aliases)
    violations: list[str] = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "close":
            if _base_names(func.value) & owners:
                continue
            violations.append(ast.unparse(func))
        elif isinstance(func, ast.Name):
            origin = aliases.get(func.id, func.id)
            if (
                func.id == "close"
                or func.id in raw
                or origin in _RAW_CLOSE_QUALNAMES
            ):
                violations.append(ast.unparse(func))
    return violations


def _foundation_imports(tree: ast.AST) -> list[str]:
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    return modules


def _inline_function(source: str) -> tuple[ast.FunctionDef, dict[str, str]]:
    """Parse a dedented source snippet and return its function and aliases."""
    tree = ast.parse(textwrap.dedent(source))
    return _find_function(tree, "f"), _module_aliases(tree)


class MigratedFunctionOwnershipTests(unittest.TestCase):
    """Task 1.8 — both migrated functions delegate release to the owner."""

    def test_each_migrated_function_constructs_an_owned_descriptor(self) -> None:
        for path, function_name in _MIGRATED.items():
            with self.subTest(module=path.name, function=function_name):
                function = _find_function(_parse(path), function_name)
                self.assertIn(
                    "OwnedDescriptor",
                    {node.id for node in ast.walk(function) if isinstance(node, ast.Name)},
                )
                self.assertIn(
                    "PosixDescriptorOps",
                    {node.id for node in ast.walk(function) if isinstance(node, ast.Name)},
                )

    def test_each_migrated_function_has_no_unowned_close_call(self) -> None:
        for path, function_name in _MIGRATED.items():
            with self.subTest(module=path.name, function=function_name):
                tree = _parse(path)
                function = _find_function(tree, function_name)
                self.assertEqual(
                    _close_violations(function, _module_aliases(tree)),
                    [],
                )

    def test_each_migrated_function_has_no_bespoke_release_state(self) -> None:
        for path, function_name in _MIGRATED.items():
            with self.subTest(module=path.name, function=function_name):
                function = _find_function(_parse(path), function_name)
                found = {node.id for node in ast.walk(function) if isinstance(node, ast.Name)} | {
                    node.attr for node in ast.walk(function) if isinstance(node, ast.Attribute)
                }
                self.assertEqual(found & _BESPOKE_RELEASE_NAMES, set())

    def test_npm_modules_import_only_explicit_foundation_submodules(self) -> None:
        for path in _MIGRATED:
            with self.subTest(module=path.name):
                imports = _foundation_imports(_parse(path))
                self.assertNotIn("docker.transactions", imports)
                for module in imports:
                    if module == "docker.filesystem" or module.startswith(
                        "docker.filesystem."
                    ):
                        self.assertGreaterEqual(
                            len(module.split(".")),
                            3,
                            f"aggregate foundation import {module!r}",
                        )


class CloseGateEvasionTests(unittest.TestCase):
    """Task 1.8 — renamed/aliased close calls cannot evade the gate."""

    def test_gate_rejects_renamed_injected_backend_close(self) -> None:
        cases = {
            "renamed_backend": """
                from docker.filesystem.operations import PosixDescriptorOps

                def f():
                    backend = PosixDescriptorOps()
                    backend.close(3)
            """,
            "renamed_backend_attribute": """
                from docker.filesystem.operations import PosixDescriptorOps

                def f(self):
                    self._ops = PosixDescriptorOps()
                    self._ops.close(3)
            """,
        }
        for label, source in cases.items():
            with self.subTest(case=label):
                function, aliases = _inline_function(source)
                self.assertNotEqual(_close_violations(function, aliases), [])

    def test_gate_rejects_aliased_and_assigned_raw_closes(self) -> None:
        cases = {
            "aliased_os_module": """
                import os as o

                def f(fd):
                    o.close(fd)
            """,
            "imported_close_alias": """
                from os import close as c

                def f(fd):
                    c(fd)
            """,
            "assigned_close_callable": """
                import os

                def f(fd):
                    closer = os.close
                    closer(fd)
            """,
            "bare_close": """
                def f(fd):
                    close(fd)
            """,
            "direct_os_close": """
                import os

                def f(fd):
                    os.close(fd)
            """,
        }
        for label, source in cases.items():
            with self.subTest(case=label):
                function, aliases = _inline_function(source)
                self.assertNotEqual(_close_violations(function, aliases), [])

    def test_gate_permits_only_shared_owner_close(self) -> None:
        cases = {
            "direct_owner": """
                from docker.filesystem.descriptors import OwnedDescriptor
                from docker.filesystem.operations import PosixDescriptorOps

                def f(fd):
                    ops = PosixDescriptorOps()
                    owner = OwnedDescriptor(ops, fd, label="entry")
                    owner.close()
            """,
            "context_owner": """
                from docker.filesystem.descriptors import OwnedDescriptor
                from docker.filesystem.operations import PosixDescriptorOps

                def f(fd):
                    with OwnedDescriptor(
                        PosixDescriptorOps(), fd, label="entry"
                    ) as owner:
                        owner.close()
            """,
        }
        for label, source in cases.items():
            with self.subTest(case=label):
                function, aliases = _inline_function(source)
                self.assertEqual(_close_violations(function, aliases), [])


class NpmDomainAuthorityTests(unittest.TestCase):
    """Task 1.8 — recursion, hashing, lockfile, and errors stay in npm."""

    def test_tree_module_keeps_recursion_hashing_and_domain_errors(self) -> None:
        source = (_NPM / "tree.py").read_text(encoding="utf-8")
        self.assertIn("def _walk_entries", source)
        self.assertIn("_walk_entries(sub, rel, visit)", source)
        self.assertIn("hashlib.sha256", source)
        self.assertIn("LockedNpmError", source)

    def test_execution_lockfile_function_keeps_lockfile_semantics(self) -> None:
        function = _find_function(_parse(_NPM / "execution.py"), "_write_lockfile")
        source = ast.unparse(function)
        self.assertIn("O_EXCL", source)
        self.assertIn("O_NOFOLLOW", source)
        self.assertIn("unsafe_staging_path", source)
        self.assertIn("LockedNpmError", source)


#: Phase 2 — migrated method and function coordinates.
_PHASE2_METHODS = {
    _REPO / "docker" / "runtime_installer.py": (
        "RuntimeArtifactReader",
        "open_verified",
    ),
}
_PHASE2_FUNCTIONS = {
    _REPO
    / "docker"
    / "versioning"
    / "build_context_confinement.py": "_write_private",
}

#: Domain identifiers that must not leak into the neutral foundation.
_FORBIDDEN_FOUNDATION_NAMES = frozenset(
    {
        "InstallError",
        "IntegrityError",
        "ProjectionError",
        "ConfinementError",
        "open_verified",
        "_write_private",
        "_MOUNTED_ARTIFACT_ROOT",
    }
)

#: Domain packages the lightweight foundation must never import.
_FORBIDDEN_FOUNDATION_IMPORTS = (
    "docker.runtime_installer",
    "docker.versioning",
    "docker.npm_environment",
    "docker.transactions",
)


def _phase2_functions():
    """Yield ``(path, label, FunctionDef)`` for every Phase 2 migration."""
    for path, function_name in _PHASE2_FUNCTIONS.items():
        yield path, function_name, _find_function(_parse(path), function_name)
    for path, (class_name, method_name) in _PHASE2_METHODS.items():
        yield path, f"{class_name}.{method_name}", _find_method(
            _parse(path), class_name, method_name,
        )


class RuntimeBuildContextConvergenceTests(unittest.TestCase):
    """Task 2.10 — runtime/build-context release stays in the owner."""

    def test_migrated_functions_construct_an_owned_descriptor(self) -> None:
        for path, label, function in _phase2_functions():
            with self.subTest(module=path.name, function=label):
                names = {
                    node.id
                    for node in ast.walk(function)
                    if isinstance(node, ast.Name)
                }
                self.assertIn("OwnedDescriptor", names)
                self.assertIn("PosixDescriptorOps", names)

    def test_migrated_functions_have_no_unowned_close_call(self) -> None:
        for path, label, function in _phase2_functions():
            with self.subTest(module=path.name, function=label):
                tree = _parse(path)
                self.assertEqual(
                    _close_violations(function, _module_aliases(tree)),
                    [],
                )

    def test_migrated_functions_have_no_bespoke_release_state(self) -> None:
        for path, label, function in _phase2_functions():
            with self.subTest(module=path.name, function=label):
                found = {
                    node.id
                    for node in ast.walk(function)
                    if isinstance(node, ast.Name)
                } | {
                    node.attr
                    for node in ast.walk(function)
                    if isinstance(node, ast.Attribute)
                }
                self.assertEqual(found & _BESPOKE_RELEASE_NAMES, set())

    def test_migrated_modules_import_only_explicit_foundation_submodules(self) -> None:
        for path in list(_PHASE2_FUNCTIONS) + list(_PHASE2_METHODS):
            with self.subTest(module=path.name):
                imports = _foundation_imports(_parse(path))
                self.assertNotIn("docker.transactions", imports)
                for module in imports:
                    if module == "docker.filesystem" or module.startswith(
                        "docker.filesystem."
                    ):
                        self.assertGreaterEqual(
                            len(module.split(".")),
                            3,
                            f"aggregate foundation import {module!r}",
                        )

    def test_foundation_gains_no_domain_authority(self) -> None:
        for path in sorted((_REPO / "docker" / "filesystem").glob("*.py")):
            with self.subTest(module=path.name):
                tree = _parse(path)
                for module in _foundation_imports(tree):
                    is_forbidden = module in _FORBIDDEN_FOUNDATION_IMPORTS or any(
                        module == prefix or module.startswith(prefix + ".")
                        for prefix in _FORBIDDEN_FOUNDATION_IMPORTS
                    )
                    self.assertFalse(
                        is_forbidden,
                        f"domain import {module!r} in foundation {path.name}",
                    )
                names = {
                    node.id
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Name)
                } | {
                    node.attr
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Attribute)
                }
                found = names & _FORBIDDEN_FOUNDATION_NAMES
                self.assertEqual(
                    found, set(), f"domain authority {found} in {path.name}",
                )


#: Phase 3 — migrated function and method coordinates.
_PHASE3_FUNCTIONS = {
    _REPO / "docker" / "versioning" / "build_snapshot.py": (
        "_copy_source_fd_to_destination"
    ),
}
_PHASE3_METHODS = {
    _REPO / "docker" / "versioning" / "artifact_cache.py": (
        "LocalCacheFilesystem",
        "create_temp",
    ),
}

#: Phase 3 — a class that privately owns one descriptor across methods.
_PHASE3_OWNED_CLASSES = {
    _REPO / "docker" / "versioning" / "project_state.py": "ValidatedProjectState",
}

#: Phase 3 migrated modules that gained an explicit foundation import.
_PHASE3_MODULES = (
    _REPO / "docker" / "versioning" / "build_snapshot.py",
    _REPO / "docker" / "versioning" / "artifact_cache.py",
    _REPO / "docker" / "versioning" / "project_state.py",
)

#: Public interfaces whose exact signature must not change in Phase 3.
_PHASE3_PUBLIC_SIGNATURES = {
    ("docker.versioning.build_snapshot", "create_artifact_snapshot"): (
        "(selected: 'Iterable[SelectedBuildArtifact]', blobs: 'Iterable[Path]', "
        "*, constructor_project_root: 'str | Path', cache_root: 'str | Path | "
        "None' = None, project_state: 'ProjectState | None' = None, derived: "
        "'DerivedEnvironmentSource | None' = None) -> 'MaterializedSnapshot'"
    ),
    ("docker.versioning.build_snapshot", "cleanup_artifact_snapshot"): (
        "(snapshot: 'MaterializedSnapshot | Path | None') -> 'None'"
    ),
    ("docker.versioning.artifact_cache", "LocalCacheFilesystem.create_temp"): (
        "(self, path: 'str') -> 'None'"
    ),
    ("docker.versioning.project_state", "validate_project_state"): (
        "(state: 'ProjectState') -> 'ValidatedProjectState'"
    ),
    ("docker.versioning.project_state", "resolve_project_state"): (
        "(project_root: 'str | Path', *, cache_root: 'str | Path | None' = "
        "None, create: 'bool' = True, validate_children: 'bool' = True) -> "
        "'ProjectState'"
    ),
    ("docker.versioning.project_state", "ValidatedProjectState.__init__"): (
        "(self, state: 'ProjectState', namespace_fd: 'int')"
    ),
    ("docker.versioning.project_state", "ValidatedProjectState.close"): (
        "(self) -> 'None'"
    ),
    ("docker.versioning.project_state", "ValidatedProjectState.__enter__"): (
        '(self) -> "\'ValidatedProjectState\'"'
    ),
    ("docker.versioning.project_state", "ValidatedProjectState.__exit__"): (
        "(self, exc_type, exc, tb) -> 'None'"
    ),
}


def _phase3_functions():
    """Yield ``(path, label, FunctionDef)`` for every Phase 3 migration."""
    for path, function_name in _PHASE3_FUNCTIONS.items():
        yield path, function_name, _find_function(_parse(path), function_name)
    for path, (class_name, method_name) in _PHASE3_METHODS.items():
        yield path, f"{class_name}.{method_name}", _find_method(
            _parse(path), class_name, method_name,
        )


def _class_owner_names(
    tree: ast.AST, class_name: str, aliases: dict[str, str],
) -> set[str]:
    """Return every name/attribute a class binds to a shared owner."""
    owners: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name == class_name):
            continue
        for item in ast.walk(node):
            if isinstance(item, ast.Assign):
                targets, value = item.targets, item.value
            elif isinstance(item, ast.AnnAssign) and item.value is not None:
                targets, value = [item.target], item.value
            else:
                continue
            if not _is_owner_call(value, aliases):
                continue
            for target in targets:
                if isinstance(target, ast.Attribute):
                    owners.add(target.attr)
                elif isinstance(target, ast.Name):
                    owners.add(target.id)
    return owners


def _close_violations_with_owners(
    function: ast.FunctionDef, owners: set[str], aliases: dict[str, str],
) -> list[str]:
    raw = _raw_close_names(function, aliases)
    violations: list[str] = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "close":
            if _base_names(func.value) & owners:
                continue
            violations.append(ast.unparse(func))
        elif isinstance(func, ast.Name):
            origin = aliases.get(func.id, func.id)
            if (
                func.id == "close"
                or func.id in raw
                or origin in _RAW_CLOSE_QUALNAMES
            ):
                violations.append(ast.unparse(func))
    return violations


def _raw_close_targets(
    function: ast.FunctionDef, aliases: dict[str, str],
) -> list[str]:
    """Return the unparsed argument of every raw close call in *function*."""
    raw = _raw_close_names(function, aliases)
    targets: list[str] = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_raw = False
        if isinstance(func, ast.Attribute):
            is_raw = _qualname(func, aliases) in _RAW_CLOSE_QUALNAMES
        elif isinstance(func, ast.Name):
            origin = aliases.get(func.id, func.id)
            is_raw = (
                func.id == "close"
                or func.id in raw
                or origin in _RAW_CLOSE_QUALNAMES
            )
        if is_raw and node.args:
            targets.append(ast.unparse(node.args[0]))
    return targets


class VersioningConvergenceTests(unittest.TestCase):
    """Task 3.7 — versioning single releases stay in the owner."""

    def test_migrated_functions_construct_an_owned_descriptor(self) -> None:
        for path, label, function in _phase3_functions():
            with self.subTest(module=path.name, function=label):
                names = {
                    node.id
                    for node in ast.walk(function)
                    if isinstance(node, ast.Name)
                }
                self.assertIn("OwnedDescriptor", names)
                self.assertIn("PosixDescriptorOps", names)

    def test_snapshot_function_has_no_unowned_close_call(self) -> None:
        for path, function_name in _PHASE3_FUNCTIONS.items():
            with self.subTest(module=path.name, function=function_name):
                tree = _parse(path)
                function = _find_function(tree, function_name)
                self.assertEqual(
                    _close_violations(function, _module_aliases(tree)),
                    [],
                )

    def test_create_temp_migrates_child_and_retains_parent_accumulator(self) -> None:
        for path, (class_name, method_name) in _PHASE3_METHODS.items():
            with self.subTest(module=path.name):
                tree = _parse(path)
                aliases = _module_aliases(tree)
                function = _find_method(tree, class_name, method_name)
                owners = _owner_names(function, aliases)
                self.assertTrue(owners)
                # The only remaining raw close is the retained parent
                # accumulator; the migrated child descriptor is never closed
                # raw.
                self.assertEqual(
                    _close_violations_with_owners(function, owners, aliases),
                    ["os.close"],
                )
                self.assertEqual(
                    _raw_close_targets(function, aliases),
                    ["parent_fd"],
                )

    def test_validated_project_state_delegates_to_private_owner(self) -> None:
        for path, class_name in _PHASE3_OWNED_CLASSES.items():
            with self.subTest(module=path.name):
                tree = _parse(path)
                aliases = _module_aliases(tree)
                owners = _class_owner_names(tree, class_name, aliases)
                self.assertIn("_owner", owners)
                close = _find_method(tree, class_name, "close")
                self.assertEqual(
                    _close_violations_with_owners(close, owners, aliases),
                    [],
                )

    def test_migrated_releases_have_no_bespoke_state_machine(self) -> None:
        for path, label, function in _phase3_functions():
            with self.subTest(module=path.name, function=label):
                found = {
                    node.id
                    for node in ast.walk(function)
                    if isinstance(node, ast.Name)
                } | {
                    node.attr
                    for node in ast.walk(function)
                    if isinstance(node, ast.Attribute)
                }
                self.assertEqual(found & _BESPOKE_RELEASE_NAMES, set())
        for path, class_name in _PHASE3_OWNED_CLASSES.items():
            with self.subTest(module=path.name):
                tree = _parse(path)
                close = _find_method(tree, class_name, "close")
                found = {
                    node.id
                    for node in ast.walk(close)
                    if isinstance(node, ast.Name)
                } | {
                    node.attr
                    for node in ast.walk(close)
                    if isinstance(node, ast.Attribute)
                }
                self.assertEqual(found & _BESPOKE_RELEASE_NAMES, set())

    def test_migrated_modules_import_explicit_foundation_submodules(self) -> None:
        for path in _PHASE3_MODULES:
            with self.subTest(module=path.name):
                imports = _foundation_imports(_parse(path))
                self.assertTrue(
                    any(
                        module.startswith("docker.filesystem.")
                        for module in imports
                    ),
                    f"no explicit foundation import in {path.name}",
                )
                for module in imports:
                    if module == "docker.filesystem" or module.startswith(
                        "docker.filesystem."
                    ):
                        self.assertGreaterEqual(
                            len(module.split(".")),
                            3,
                            f"aggregate foundation import {module!r}",
                        )

    def test_foundation_has_no_reverse_dependency_on_versioning(self) -> None:
        forbidden = "docker.versioning"
        for path in sorted((_REPO / "docker" / "filesystem").glob("*.py")):
            with self.subTest(module=path.name):
                for module in _foundation_imports(_parse(path)):
                    self.assertFalse(
                        module == forbidden
                        or module.startswith(forbidden + "."),
                        f"reverse dependency {module!r} in {path.name}",
                    )

    def test_public_interfaces_are_signature_stable(self) -> None:
        import importlib
        import inspect

        for (module_name, qualname), expected in _PHASE3_PUBLIC_SIGNATURES.items():
            with self.subTest(interface=f"{module_name}.{qualname}"):
                target = importlib.import_module(module_name)
                for part in qualname.split("."):
                    target = getattr(target, part)
                self.assertEqual(str(inspect.signature(target)), expected)


if __name__ == "__main__":
    unittest.main()
