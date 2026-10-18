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

_REPO = Path(__file__).resolve().parents[2]
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
    # Memory-buffer exemptions require stable import evidence, unlike the
    # may-origin maps used to *detect* potentially dangerous operations. Reject
    # conflicting imports and any source binding/mutation of these roots. This
    # deliberately overrejects shadowing even in another lexical scope.
    for name, origin in list(aliases.items()):
        if origin not in {"io", "io.StringIO", "io.BytesIO"}:
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.Name, ast.Attribute))
                and isinstance(
                    node.ctx,
                    (ast.Store, ast.Del),
                )
                and name in _base_names(node)
            ):
                aliases[name] = "<unproven>"
            elif isinstance(node, ast.arg) and node.arg == name:
                aliases[name] = "<unproven>"
            elif (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and node.name == name
            ):
                aliases[name] = "<unproven>"
            elif (
                isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar))
                and node.name == name
            ):
                aliases[name] = "<unproven>"
            elif isinstance(node, ast.MatchMapping) and node.rest == name:
                aliases[name] = "<unproven>"
            elif isinstance(node, ast.Import):
                if any(
                    (item.asname or item.name.split(".")[0]) == name
                    and item.name != origin
                    for item in node.names
                ):
                    aliases[name] = "<unproven>"
            elif isinstance(node, ast.ImportFrom):
                if any(
                    (item.asname or item.name) == name
                    and (node.level or f"{node.module}.{item.name}" != origin)
                    for item in node.names
                ):
                    aliases[name] = "<unproven>"
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


def _owner_names(function: ast.FunctionDef, aliases: dict[str, str]) -> set[str]:
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
        elif isinstance(node, ast.NamedExpr) and _is_owner_call(node.value, aliases):
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


def _raw_close_names(function: ast.FunctionDef, aliases: dict[str, str]) -> set[str]:
    """Names bound to a raw close primitive, including aliases."""
    names = {name for name, origin in aliases.items() if origin in _RAW_CLOSE_QUALNAMES}
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


def _close_violations(function: ast.FunctionDef, aliases: dict[str, str]) -> list[str]:
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
            if func.id == "close" or func.id in raw or origin in _RAW_CLOSE_QUALNAMES:
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
                    {
                        node.id
                        for node in ast.walk(function)
                        if isinstance(node, ast.Name)
                    },
                )
                self.assertIn(
                    "PosixDescriptorOps",
                    {
                        node.id
                        for node in ast.walk(function)
                        if isinstance(node, ast.Name)
                    },
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
                found = {
                    node.id for node in ast.walk(function) if isinstance(node, ast.Name)
                } | {
                    node.attr
                    for node in ast.walk(function)
                    if isinstance(node, ast.Attribute)
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
    _REPO / "docker" / "versioning" / "build_context_confinement.py": "_write_private",
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
        yield (
            path,
            f"{class_name}.{method_name}",
            _find_method(
                _parse(path),
                class_name,
                method_name,
            ),
        )


class RuntimeBuildContextConvergenceTests(unittest.TestCase):
    """Task 2.10 — runtime/build-context release stays in the owner."""

    def test_migrated_functions_construct_an_owned_descriptor(self) -> None:
        for path, label, function in _phase2_functions():
            with self.subTest(module=path.name, function=label):
                names = {
                    node.id for node in ast.walk(function) if isinstance(node, ast.Name)
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
                    node.id for node in ast.walk(function) if isinstance(node, ast.Name)
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
                    node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
                } | {
                    node.attr
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Attribute)
                }
                found = names & _FORBIDDEN_FOUNDATION_NAMES
                self.assertEqual(
                    found,
                    set(),
                    f"domain authority {found} in {path.name}",
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
        "(self) -> \"'ValidatedProjectState'\""
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
        yield (
            path,
            f"{class_name}.{method_name}",
            _find_method(
                _parse(path),
                class_name,
                method_name,
            ),
        )


def _class_owner_names(
    tree: ast.AST,
    class_name: str,
    aliases: dict[str, str],
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
            if not _is_ownerish_call(value, aliases):
                continue
            for target in targets:
                if isinstance(target, ast.Attribute):
                    owners.add(target.attr)
                elif isinstance(target, ast.Name):
                    owners.add(target.id)
    return owners


def _proven_memory_buffer_close(
    function: ast.FunctionDef,
    receiver: ast.expr,
    aliases: dict[str, str],
) -> bool:
    """Recognize only fresh, empty stdlib memory buffers, never file streams.

    Deliberately no variable/annotation/name-based receiver exemption: the
    receiver must be the constructor call itself with an imported io origin.
    Local shadowing or mutation of its import root makes that origin unproven.
    """
    if not isinstance(receiver, ast.Call) or receiver.args or receiver.keywords:
        return False
    if _qualname(receiver.func, aliases) not in {"io.StringIO", "io.BytesIO"}:
        return False
    root = receiver.func
    while isinstance(root, ast.Attribute):
        root = root.value
    if not isinstance(root, ast.Name) or root.id not in aliases:
        return False
    for node in ast.walk(function):
        if (
            isinstance(node, (ast.Name, ast.Attribute))
            and isinstance(
                node.ctx,
                (ast.Store, ast.Del),
            )
            and root.id in _base_names(node)
        ):
            return False
        if isinstance(node, ast.arg) and node.arg == root.id:
            return False
        if (
            isinstance(node, ast.alias)
            and (node.asname or node.name.split(".")[0]) == root.id
        ):
            return False
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.name == root.id
        ):
            return False
        if isinstance(node, ast.ExceptHandler) and node.name == root.id:
            return False
    return True


def _close_violations_with_owners(
    function: ast.FunctionDef,
    owners: set[str],
    aliases: dict[str, str],
    *,
    shared_owner_release: bool = False,
) -> list[str]:
    """Return every close-like call not provably bound to a shared owner.

    ``shared_owner_release`` is granted only for an explicitly verified
    shared-owner implementation; even then the method must prove it becomes
    terminal before the close and suppresses a repeated release.
    """
    raw = _raw_close_names(function, aliases)
    violations: list[str] = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "run"
            and node.args
            and isinstance(node.args[0], ast.Attribute)
            and node.args[0].attr == "close"
        ):
            # A cleanup accumulator releases an owner by passing its bound
            # close callable: ``failures.run(owner.close, ordinary=...)``.
            # The receiver must still be a proven owner, so the gate inspects
            # the bound method rather than losing the release.
            if _base_names(node.args[0].value) & owners:
                continue
            violations.append(ast.unparse(node.args[0]))
            continue
        if isinstance(func, ast.Attribute) and func.attr == "close":
            if _base_names(func.value) & owners:
                continue
            if shared_owner_release and _shared_owner_release_proof(function, node):
                continue
            if _proven_memory_buffer_close(function, func.value, aliases):
                continue
            violations.append(ast.unparse(func))
        elif isinstance(func, ast.Name):
            origin = aliases.get(func.id, func.id)
            if func.id == "close" or func.id in raw or origin in _RAW_CLOSE_QUALNAMES:
                violations.append(ast.unparse(func))
    return violations


def _terminal_field_before(
    function: ast.FunctionDef, close_node: ast.Call
) -> str | None:
    """Return the boolean field marked ``True`` before *close_node* runs.

    A shared owner becomes terminal before its release attempt, for example
    ``self._closed = True``.  The field name (not a bare flag) is returned so
    the terminal transition and the repeat-safe guard can be tied to the same
    state.
    """
    close_line = close_node.lineno
    for node in ast.walk(function):
        if not isinstance(node, ast.Assign) or node.lineno >= close_line:
            continue
        if not (isinstance(node.value, ast.Constant) and node.value.value is True):
            continue
        for target in node.targets:
            if isinstance(target, ast.Attribute):
                return target.attr
    return None


def _guarded_by_terminal_field(
    function: ast.FunctionDef, close_node: ast.Call, field: str
) -> bool:
    """True when *close_node* sits inside ``if not self.<field>:``.

    The negated terminal guard makes a repeated release a no-op, so the
    backend close is issued at most once even after a failed attempt.
    """
    for node in ast.walk(function):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not (isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)):
            continue
        operand = test.operand
        if not (isinstance(operand, ast.Attribute) and operand.attr == field):
            continue
        if any(child is close_node for child in ast.walk(node)):
            return True
    return False


def _synchronized_release_proof(
    function: ast.FunctionDef, close_node: ast.Call
) -> bool:
    """Prove a synchronized terminal claim precedes the underlying close.

    ``OwnedStream.close`` claims the live-to-release-attempted transition
    under ``self._lock`` before invoking ``stream.close()`` outside the lock,
    so a concurrent/repeated caller issues no second close.  This is the
    bounded stream-owner internal release, not a consumer close.
    """
    if not (
        isinstance(close_node.func, ast.Attribute)
        and isinstance(close_node.func.value, ast.Name)
    ):
        return False
    receiver = close_node.func.value.id
    for claim in function.body:
        if not isinstance(claim, ast.With) or not any(
            ast.unparse(item.context_expr) == "self._lock" for item in claim.items
        ):
            continue
        # The repeated-release guard, terminal transition and captured stream
        # must be consecutive operations in the same synchronized claim. The
        # actual close must follow that claim, outside its lock.
        statements = [ast.unparse(node) for node in claim.body]
        if statements != [
            "if self._state is not _LIVE:\n    return",
            "self._state = _RELEASED",
            f"{receiver} = self._stream",
        ]:
            continue
        if close_node.lineno > claim.end_lineno and any(
            isinstance(node, ast.Expr) and node.value is close_node
            for node in function.body
        ):
            return True
    return False


def _shared_owner_release_proof(
    function: ast.FunctionDef, close_node: ast.Call
) -> bool:
    """Prove terminal-before-close and repeat-safe release for *close_node*.

    The caller supplies the verified shared-owner identity; this proof
    additionally requires the method to mark itself terminal before the close
    and to guard the close behind the same terminal field.  A verified
    synchronized state claim (the bounded ``OwnedStream`` owner) is accepted
    as the stream-family equivalent.  A consumer that merely sets
    ``self._closed = True`` before ``self._ops.close(...)`` does not satisfy
    the repeat-safe guard and is rejected.
    """
    field = _terminal_field_before(function, close_node)
    if field is None:
        return _synchronized_release_proof(function, close_node)
    return _guarded_by_terminal_field(function, close_node, field)


def _raw_close_targets(
    function: ast.FunctionDef,
    aliases: dict[str, str],
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
                func.id == "close" or func.id in raw or origin in _RAW_CLOSE_QUALNAMES
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
                    node.id for node in ast.walk(function) if isinstance(node, ast.Name)
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

    def test_create_temp_migrates_child_and_parent(self) -> None:
        for path, (class_name, method_name) in _PHASE3_METHODS.items():
            with self.subTest(module=path.name):
                tree = _parse(path)
                aliases = _module_aliases(tree)
                function = _find_method(tree, class_name, method_name)
                owners = _owner_names(function, aliases)
                self.assertTrue(owners)
                # Phase 6 migrates both the freshly created child and the
                # retained parent path to shared owners; no raw close remains.
                self.assertEqual(
                    _close_violations_with_owners(function, owners, aliases),
                    [],
                )
                self.assertEqual(_raw_close_targets(function, aliases), [])

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
                    node.id for node in ast.walk(function) if isinstance(node, ast.Name)
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
                    node.id for node in ast.walk(close) if isinstance(node, ast.Name)
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
                    any(module.startswith("docker.filesystem.") for module in imports),
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
                        module == forbidden or module.startswith(forbidden + "."),
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


#: Phase 4 — the authoritative build-cache aggregate owner module.
_BUILD_CACHE = _REPO / "docker" / "versioning" / "build_cache.py"

#: Phase 4 — public interfaces whose exact signature must not change.
_PHASE4_PUBLIC_SIGNATURES = {
    ("docker.versioning.build_cache", "BuildCacheState"): (
        "(paths: 'BuildCachePaths', persistent_fd: 'int', blobs_fd: 'int', "
        "tmp_fd: 'int', markers_fd: 'int', transactions_fd: 'int') -> None"
    ),
    ("docker.versioning.build_cache", "BuildCacheState.close"): "(self) -> 'None'",
    ("docker.versioning.build_cache", "open_build_cache_state"): (
        "(constructor_project_root: 'str | Path', *, cache_root: 'str | Path | "
        "None' = None, project_state: 'ProjectState | None' = None) -> "
        "'BuildCacheState'"
    ),
}

#: Phase 4 — the five private owners in historical release order.
_PHASE4_OWNER_LABELS = (
    "build-cache transactions",
    "build-cache markers",
    "build-cache tmp",
    "build-cache blobs",
    "build-cache persistent",
)


class BuildCacheStateConvergenceTests(unittest.TestCase):
    """Task 4.7 — the build-cache aggregate owner stays converged."""

    def _method(self, method_name: str):
        tree = _parse(_BUILD_CACHE)
        aliases = _module_aliases(tree)
        return tree, aliases, _find_method(tree, "BuildCacheState", method_name)

    def test_close_has_no_unowned_close_or_raw_multi_close_loop(self) -> None:
        _, aliases, close = self._method("close")
        self.assertEqual(_close_violations(close, aliases), [])
        # The only loop left iterates the detached owner collection, never a
        # raw fd tuple.
        loops = [
            node
            for node in ast.walk(close)
            if isinstance(node, (ast.For, ast.AsyncFor))
        ]
        self.assertEqual(len(loops), 1)
        self.assertEqual(ast.unparse(loops[0].iter), "owners")

    def test_close_uses_exactly_one_aggregate_accumulator(self) -> None:
        _, aliases, close = self._method("close")
        accumulators = [
            node
            for node in ast.walk(close)
            if isinstance(node, ast.Call)
            and _qualname(node.func, aliases)
            == "docker.transactions.cleanup.CleanupFailures"
        ]
        self.assertEqual(len(accumulators), 1)
        run_calls = [
            node
            for node in ast.walk(close)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "run"
        ]
        self.assertEqual(len(run_calls), 1)
        self.assertEqual(ast.unparse(run_calls[0].args[0]), "owner.close")
        ordinary = next(
            keyword.value
            for keyword in run_calls[0].keywords
            if keyword.arg == "ordinary"
        )
        self.assertEqual(ast.unparse(ordinary), "(OSError,)")

    def test_post_init_creates_one_owner_per_descriptor_field(self) -> None:
        _, aliases, post = self._method("__post_init__")
        names = {node.id for node in ast.walk(post) if isinstance(node, ast.Name)}
        self.assertIn("OwnedDescriptor", names)
        self.assertIn("PosixDescriptorOps", names)
        targets = {
            ast.unparse(target)
            for node in ast.walk(post)
            if isinstance(node, ast.Assign)
            for target in node.targets
        }
        self.assertIn("self._owners", targets)
        owner_calls = [
            node
            for node in ast.walk(post)
            if isinstance(node, ast.Call)
            and _qualname(node.func, aliases) in _OWNER_QUALNAMES
        ]
        self.assertEqual(len(owner_calls), 5)
        labels = [
            keyword.value.value
            for call in owner_calls
            for keyword in call.keywords
            if keyword.arg == "label"
        ]
        self.assertEqual(labels, list(_PHASE4_OWNER_LABELS))

    def test_no_bespoke_release_state_machine(self) -> None:
        tree = _parse(_BUILD_CACHE)
        for method_name in ("__post_init__", "close"):
            method = _find_method(tree, "BuildCacheState", method_name)
            found = {
                node.id for node in ast.walk(method) if isinstance(node, ast.Name)
            } | {
                node.attr
                for node in ast.walk(method)
                if isinstance(node, ast.Attribute)
            }
            self.assertEqual(found & _BESPOKE_RELEASE_NAMES, set())

    def test_open_build_cache_state_is_annotated_with_the_class(self) -> None:
        tree = _parse(_BUILD_CACHE)
        function = _find_function(tree, "open_build_cache_state")
        self.assertEqual(ast.unparse(function.returns), "BuildCacheState")
        classes = {node.name for node in tree.body if isinstance(node, ast.ClassDef)}
        self.assertIn("BuildCacheState", classes)

    def test_module_imports_explicit_foundation_submodules(self) -> None:
        imports = _foundation_imports(_parse(_BUILD_CACHE))
        self.assertTrue(
            any(module.startswith("docker.filesystem.") for module in imports),
            "no explicit foundation import in build_cache.py",
        )
        for module in imports:
            if module == "docker.filesystem" or module.startswith("docker.filesystem."):
                self.assertGreaterEqual(
                    len(module.split(".")),
                    3,
                    f"aggregate foundation import {module!r}",
                )

    def test_public_interfaces_are_signature_stable(self) -> None:
        import importlib
        import inspect

        for (module_name, qualname), expected in _PHASE4_PUBLIC_SIGNATURES.items():
            with self.subTest(interface=f"{module_name}.{qualname}"):
                target = importlib.import_module(module_name)
                for part in qualname.split("."):
                    target = getattr(target, part)
                self.assertEqual(str(inspect.signature(target)), expected)


#: Phase 5 — the two build-cache directory-handoff functions.
_PHASE5_FUNCTIONS = (
    "_open_relative_dir",
    "_bootstrap_constructor_project_lock_parent",
)

#: Phase 5 — signatures that must stay stable across the migration.
_PHASE5_SIGNATURES = {
    ("docker.versioning.build_cache", "_open_relative_dir"): (
        "(namespace_fd: 'int', parts: 'tuple[str, ...]', *, "
        "create: 'bool') -> 'int | None'"
    ),
    (
        "docker.versioning.build_cache",
        "_bootstrap_constructor_project_lock_parent",
    ): (
        "(constructor_project_root: 'str | Path', *, cache_root: 'str | Path | "
        "None' = None) -> 'tuple[ProjectState, int]'"
    ),
}


class Phase5HandoffConvergenceTests(unittest.TestCase):
    """Task 5.10 — build-cache directory handoffs stay converged."""

    def _function(self, name: str):
        tree = _parse(_BUILD_CACHE)
        aliases = _module_aliases(tree)
        return tree, aliases, _find_function(tree, name)

    def test_migrated_functions_construct_an_owned_descriptor(self) -> None:
        for name in _PHASE5_FUNCTIONS:
            with self.subTest(function=name):
                _, _, function = self._function(name)
                names = {
                    node.id for node in ast.walk(function) if isinstance(node, ast.Name)
                }
                self.assertIn("OwnedDescriptor", names)
                self.assertIn("PosixDescriptorOps", names)

    def test_migrated_functions_have_no_unowned_close_call(self) -> None:
        for name in _PHASE5_FUNCTIONS:
            with self.subTest(function=name):
                _, aliases, function = self._function(name)
                self.assertEqual(_close_violations(function, aliases), [])

    def test_no_raw_close_remains_in_handoffs(self) -> None:
        # Every release is a shared-owner close, so no direct handoff close or
        # unowned raw close target remains in either migrated function.
        for name in _PHASE5_FUNCTIONS:
            with self.subTest(function=name):
                _, aliases, function = self._function(name)
                self.assertEqual(_raw_close_targets(function, aliases), [])

    def test_migrated_functions_have_no_bespoke_release_state(self) -> None:
        for name in _PHASE5_FUNCTIONS:
            with self.subTest(function=name):
                _, _, function = self._function(name)
                found = {
                    node.id for node in ast.walk(function) if isinstance(node, ast.Name)
                } | {
                    node.attr
                    for node in ast.walk(function)
                    if isinstance(node, ast.Attribute)
                }
                self.assertEqual(found & _BESPOKE_RELEASE_NAMES, set())

    def test_handoffs_do_not_introduce_directory_descriptor(self) -> None:
        tree = _parse(_BUILD_CACHE)
        aliases = _module_aliases(tree)
        self.assertNotIn("DirectoryDescriptor", aliases)
        for name in _PHASE5_FUNCTIONS:
            with self.subTest(function=name):
                _, _, function = self._function(name)
                names = {
                    node.id for node in ast.walk(function) if isinstance(node, ast.Name)
                }
                self.assertNotIn("DirectoryDescriptor", names)

    def test_bootstrap_is_not_a_second_generic_secure_walker(self) -> None:
        _, _, function = self._function("_bootstrap_constructor_project_lock_parent")
        # A generic walker would expose configuration (owner/creation policy)
        # as parameters and define a reusable inner walk.  The domain bootstrap
        # keeps its policy local and defines no nested walker.
        argument_names = {
            arg.arg for arg in function.args.args + function.args.kwonlyargs
        }
        self.assertEqual(argument_names, {"constructor_project_root", "cache_root"})
        nested = [
            node
            for node in ast.walk(function)
            if node is not function
            and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        self.assertEqual(nested, [])

    def test_build_domain_errors_remain_local(self) -> None:
        # Path derivation and domain mapping stay in build_cache rather than
        # moving into the domain-neutral foundation.
        _, _, relative = self._function("_open_relative_dir")
        relative_names = {
            node.id for node in ast.walk(relative) if isinstance(node, ast.Name)
        }
        self.assertIn("BuildCacheError", relative_names)
        _, _, bootstrap = self._function("_bootstrap_constructor_project_lock_parent")
        bootstrap_names = {
            node.id for node in ast.walk(bootstrap) if isinstance(node, ast.Name)
        }
        self.assertIn("BuildTransactionError", bootstrap_names)

    def test_public_interfaces_are_signature_stable(self) -> None:
        import importlib
        import inspect

        for (module_name, qualname), expected in _PHASE5_SIGNATURES.items():
            with self.subTest(interface=f"{module_name}.{qualname}"):
                target = importlib.import_module(module_name)
                for part in qualname.split("."):
                    target = getattr(target, part)
                self.assertEqual(str(inspect.signature(target)), expected)


# ═══════════════════════════════════════════════════════════════════
# Phase 6 (task 6.37) — generated per-function ownership ledger
# ═══════════════════════════════════════════════════════════════════

#: Fully-qualified descriptor factories that yield an equivalent owner.
_OWNERISH_QUALNAME_TOKENS = (
    "OwnedDescriptor",
    "OwnedStream",
    "Capability",
    "DirectoryDescriptor",
)
_OWNERISH_QUALNAME_SUFFIXES = (
    ".open_secure_path",
    ".from_fd",
    ".from_path",
    ".from_secure_path",
    ".open_regular",
)

#: Type hints that mark an injected parameter/field as a shared owner.
_DESCRIPTOR_TYPE_HINTS = (
    "OwnedDescriptor",
    "OwnedStream",
    "Capability",
    "DirectoryDescriptor",
)

#: Fully-qualified implicit-ownership and descriptor-creation primitives.
_FDOPEN_QUALNAMES = frozenset({"os.fdopen"})
_MKSTEMP_QUALNAMES = frozenset({"tempfile.mkstemp"})

#: ``module::Class.method`` implementations verified by the transaction
#: capability suite as shared-owner internal releases.  Each becomes terminal
#: before its backend close and suppresses a repeated release, but only these
#: explicitly verified methods may keep an injected backend close.
_VERIFIED_SHARED_OWNER_RELEASES = frozenset(
    {
        "docker/transactions/capabilities.py::FileCapability.close",
        "docker/filesystem/streams.py::OwnedStream.close",
    }
)

#: Tasks 6.1–6.18 — migrated module-level functions.
_PHASE6_FUNCTIONS: dict[str, tuple[str, ...]] = {
    "docker/npm_environment/publication.py": (
        "_durable_write",
        "_fsync_dir",
        "_fsync_tree",
        "_atomic_publish",
    ),
    "docker/transactions/locking.py": ("_verify_unchanged", "_release_descriptor"),
    "docker/versioning/artifact_cache.py": (
        "inspect_verified_blob_readonly",
        "_ensure_dir_private",
        "_open_directory_chain",
        "_remove_tree_at",
    ),
    "docker/versioning/build_cache.py": (
        "_open_namespace_fd",
        "_validate_relative_dir",
        "_ensure_relative_dir",
        "prepare_build_cache",
        "_open_validated_child",
        "_validate_existing_blob",
        "_validate_existing_marker",
        "open_build_cache_state",
        "publish_verified_blob",
        "_validate_blob_descriptor",
        "_verify_published_blob",
        "acquire_constructor_project_build_lock",
        "_open_blob_algorithm",
        "_remove_snapshot_tree",
    ),
    "docker/versioning/project_state.py": (
        "_open_private_dir_at",
        "_read_metadata",
        "resolve_project_state",
        "validate_project_state",
    ),
    "docker/versioning/build_snapshot.py": (
        "_open_validated_source",
        "_validate_hard_link_destination",
        "create_artifact_snapshot",
    ),
    "docker/versioning/build_cleanup.py": (
        "_close_leaf",
        "_unlink_entry",
        "_open_algorithm_directory",
    ),
    "docker/versioning/cache_storage.py": (
        "_release_file_and_capability",
        "_release_file_keeping_primary",
    ),
    "docker/versioning/rendering.py": (
        "_validate_effective_destination",
        "write_effective_build",
        "write_effective_inventory",
    ),
    "docker/versioning/build_materialization.py": ("materialize_artifact",),
}

#: Tasks 6.1–6.18 — migrated methods on a class.
_PHASE6_METHODS: dict[str, tuple[tuple[str, str], ...]] = {
    "docker/transactions/capabilities.py": (
        ("DirectoryCapability", "_adopt"),
        ("DirectoryCapability", "open_regular"),
        ("DirectoryCapability", "close"),
        ("FileCapability", "close"),
    ),
    "docker/transactions/regular.py": (
        ("RegularFileContracts", "durable_unlink"),
        ("RegularFileContracts", "_validate_destination"),
        ("RegularFileContracts", "_close_fd"),
        ("RegularFileContracts", "_discard"),
    ),
    "docker/versioning/artifact_cache.py": (
        ("LocalCacheFilesystem", "create_temp"),
        ("LocalCacheFilesystem", "stat_blob"),
        ("LocalCacheFilesystem", "read_bytes"),
        ("LocalCacheFilesystem", "inspect_and_digest"),
        ("LocalCacheFilesystem", "ensure_secure_dir"),
        ("LocalCacheFilesystem", "append_temp"),
        ("LocalCacheFilesystem", "finalize_temp"),
        ("LocalCacheFilesystem", "cleanup_temp"),
        ("LocalCacheFilesystem", "quarantine_or_remove"),
        ("LocalCacheFilesystem", "get_permissions"),
        ("LocalCacheFilesystem", "set_permissions"),
        ("LocalCacheFilesystem", "atomic_publish"),
        ("LocalTemporaryDirectory", "mkdtemp"),
        ("FileIdentityLock", "acquire"),
    ),
    "docker/versioning/build_cache.py": (("_BuildStorageHandle", "_open"),),
    "docker/versioning/cache.py": (("DiskCache", "set"),),
}

#: Task 6B.14 — subprocess stream ownership functions and methods.  Their
#: stdout/stderr origins are the file objects returned by ``Popen(..., PIPE)``
#: and their only release authority is one shared ``OwnedStream`` per stream.
_PHASE6B_STREAM_FUNCTIONS: dict[str, tuple[str, ...]] = {
    "docker/npm_environment/execution.py": ("_bounded_docker_rm",),
    "docker/npm_environment/lifecycle.py": ("_run_captured", "run_captured"),
}

_PHASE6B_STREAM_METHODS: dict[str, tuple[tuple[str, str], ...]] = {
    "docker/npm_environment/execution.py": (
        ("DockerRunExecutor", "run_streaming"),
        ("_ContainerRemovalAction", "_work"),
        ("_ContainerRemovalAction", "observe"),
    ),
    "docker/filesystem/streams.py": (("OwnedStream", "close"),),
}


def _local_primitive_aliases(
    function: ast.FunctionDef,
    aliases: dict[str, str],
) -> dict[str, str]:
    """Track assigned primitive origins within this function, not its siblings.

    This is a conservative may-origin map: reassignment cannot erase evidence
    that a callable may acquire a descriptor or implicitly release one.
    """
    result = dict(aliases)
    assignments = [
        node
        for node in ast.walk(function)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
    ]
    changed = True
    while changed:
        changed = False
        for node in assignments:
            value = node.value
            if value is None:
                continue
            origin = _qualname(value, result)
            if origin not in _FDOPEN_QUALNAMES | _MKSTEMP_QUALNAMES:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if not isinstance(target, ast.Name):
                    continue
                previous = result.get(target.id)
                if (
                    previous in _FDOPEN_QUALNAMES | _MKSTEMP_QUALNAMES
                    and previous != origin
                ):
                    raise AssertionError(
                        f"ambiguous descriptor primitive alias: {target.id}"
                    )
                if previous != origin:
                    result[target.id] = origin
                    changed = True
    return result


def _call_origin(node: ast.Call, aliases: dict[str, str]) -> str:
    """Resolve a call's callee to a dotted origin (aliases included)."""
    func = node.func
    if isinstance(func, ast.Attribute):
        return _qualname(func, aliases)
    if isinstance(func, ast.Name):
        return aliases.get(func.id, func.id)
    return ""


def _annotation_is_owner(annotation: ast.expr | None) -> bool:
    if annotation is None:
        return False
    text = ast.unparse(annotation)
    return any(hint in text for hint in _DESCRIPTOR_TYPE_HINTS)


def _is_ownerish_call(value: ast.expr | None, aliases: dict[str, str]) -> bool:
    """True for a shared owner or an equivalent capability/descriptor factory."""
    if not isinstance(value, ast.Call):
        return False
    loc = _qualname(value.func, aliases)
    if loc in _OWNER_QUALNAMES:
        return True
    if any(token in loc for token in _OWNERISH_QUALNAME_TOKENS):
        return True
    return loc.endswith(_OWNERISH_QUALNAME_SUFFIXES)


def _ownerish_expression(value: ast.expr, owners: set[str]) -> bool:
    """True when *value* is a name/conditional composed solely of owners."""
    if isinstance(value, ast.Name):
        return value.id in owners
    if isinstance(value, ast.IfExp):
        return _ownerish_expression(value.body, owners) and _ownerish_expression(
            value.orelse, owners
        )
    if isinstance(value, ast.BoolOp):
        return bool(value.values) and all(
            _ownerish_expression(item, owners) for item in value.values
        )
    return False


def _ownerish_iterable(iterable: ast.expr, owners: set[str]) -> bool:
    """True when iterating *iterable* yields owned values.

    Accepts an owner name, a tuple/list literal of owned expressions, and
    ``reversed(<ownerish iterable>)``, so a release loop cannot hide an owner
    behind a container or a reversal.
    """
    if isinstance(iterable, ast.Name):
        return iterable.id in owners
    if isinstance(iterable, ast.Call):
        return (
            isinstance(iterable.func, ast.Name)
            and iterable.func.id == "reversed"
            and len(iterable.args) == 1
            and _ownerish_iterable(iterable.args[0], owners)
        )
    if isinstance(iterable, (ast.Tuple, ast.List)):
        return bool(iterable.elts) and all(
            _ownerish_expression(element, owners) for element in iterable.elts
        )
    return False


def _ownerish_names(
    function: ast.FunctionDef,
    aliases: dict[str, str],
) -> set[str]:
    """Names/parameters provably bound to a shared owner or equivalent.

    Resolves explicit ``OwnedDescriptor`` construction, equivalent
    capability/descriptor factories, and injected parameters or fields whose
    annotation is a shared-owner type.
    """
    owners = set(_owner_names(function, aliases))
    injected = (
        list(function.args.posonlyargs)
        + list(function.args.args)
        + list(function.args.kwonlyargs)
    )
    for extra in (function.args.vararg, function.args.kwarg):
        if extra is not None:
            injected.append(extra)
    for arg in injected:
        if _annotation_is_owner(arg.annotation):
            owners.add(arg.arg)
    for node in ast.walk(function):
        if isinstance(node, ast.Assign):
            value, targets = node.value, node.targets
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            value, targets = node.value, [node.target]
        elif isinstance(node, ast.NamedExpr):
            value, targets = node.value, [node.target]
        else:
            continue
        if not _is_ownerish_call(value, aliases):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                owners.add(target.id)
            elif isinstance(target, (ast.Tuple, ast.List)):
                for element in target.elts:
                    if isinstance(element, ast.Name):
                        owners.add(element.id)
    # A local variable annotated with a shared-owner type is an owner even
    # when its value is a factory call the name-based rule cannot resolve
    # (for example ``capability: LockCapability | None = acquire_lock(...)``).
    for node in ast.walk(function):
        if (
            isinstance(node, ast.AnnAssign)
            and _annotation_is_owner(node.annotation)
            and isinstance(node.target, ast.Name)
        ):
            owners.add(node.target.id)
    # Propagate parallel/aliased assignments where the value tuple already
    # holds an owner: ``previous, current = current, child``.
    changed = True
    while changed:
        changed = False
        for node in ast.walk(function):
            if isinstance(node, ast.For):
                if _ownerish_iterable(node.iter, owners):
                    elements = (
                        node.target.elts
                        if isinstance(node.target, (ast.Tuple, ast.List))
                        else [node.target]
                    )
                    for element in elements:
                        if isinstance(element, ast.Name) and element.id not in owners:
                            owners.add(element.id)
                            changed = True
                continue
            if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
                continue
            value = node.value
            if value is None:
                continue
            targets = (
                list(node.targets) if isinstance(node, ast.Assign) else [node.target]
            )
            if isinstance(value, (ast.Tuple, ast.List)):
                member_names = {
                    element.id
                    for element in value.elts
                    if isinstance(element, ast.Name)
                }
                if not (member_names & owners):
                    continue
                for target in targets:
                    elements = (
                        target.elts
                        if isinstance(target, (ast.Tuple, ast.List))
                        else [target]
                    )
                    for element in elements:
                        if isinstance(element, ast.Name) and element.id not in owners:
                            owners.add(element.id)
                            changed = True
            elif _ownerish_expression(value, owners):
                for target in targets:
                    if isinstance(target, ast.Name) and target.id not in owners:
                        owners.add(target.id)
                        changed = True
    return owners


def _live_owner_fd(
    function: ast.FunctionDef,
    call: ast.Call,
    owners: set[str],
    aliases: dict[str, str] | None = None,
) -> bool:
    """Prove the receiver's live identity at this call, not a name anywhere.

    Interpret the enclosing function in evaluation order. Aliases and captured
    close/detach methods refer to an identity, so rebinding one name cannot
    revive it or invalidate a different owner. Branches merge conservatively:
    possible release poisons an identity; differing bindings become unknown.
    Nested function bodies are not executed merely because they are defined.
    Loop backedges join bindings/invalidation to a bounded fixed point; a call
    must be safe at every abstract visit, not only its first iteration.
    """
    argument = call.args[0] if call.args else None
    if not (
        isinstance(argument, ast.Attribute)
        and argument.attr == "fd"
        and isinstance(argument.value, (ast.Name, ast.Attribute))
    ):
        return False
    aliases = aliases or {}
    bindings = {}
    invalid = set()
    answer = None
    loop_depth = 0
    analysis_failed = False
    construction_sites = {}
    exception_collectors = []

    def record_exception_state():
        # Any expression/assignment prefix can precede an exceptional transfer.
        # Keep aliases even if a later successful statement overwrites them.
        for entries in exception_collectors:
            if not entries or entries[-1] != bindings:
                entries.append(dict(bindings))

    def finished():
        return answer is not None and loop_depth == 0

    def fresh(site=None):
        # Stable allocation-site identities make loop joins finite. Reusing an
        # invalidated site is conservatively unproven, never a resurrection.
        identity = (
            object() if site is None else construction_sites.setdefault(site, object())
        )
        return ("owner", frozenset({identity}))

    for parameter in (
        function.args.posonlyargs + function.args.args + function.args.kwonlyargs
    ):
        if _annotation_is_owner(parameter.annotation):
            bindings[parameter.arg] = fresh()

    def bind(target, value):
        record_exception_state()
        if isinstance(target, (ast.Name, ast.Attribute)):
            bindings[ast.unparse(target)] = value
        elif isinstance(target, (ast.Tuple, ast.List)):
            values = (
                value
                if isinstance(value, list) and len(value) == len(target.elts)
                else [None] * len(target.elts)
            )
            for member, item in zip(target.elts, values):
                bind(member, item)

    def evaluate(node):
        nonlocal answer
        if finished() or node is None:
            return None
        record_exception_state()
        if isinstance(node, ast.Name):
            return bindings.get(node.id)
        if isinstance(node, ast.Attribute):
            base = evaluate(node.value)
            if isinstance(base, tuple) and base[0] in {"owner", "uncertain"}:
                if node.attr in {"close", "detach"}:
                    return ("terminal", base[1])
            key = ast.unparse(node)
            # Class-owned fields are supplied by the enclosing class proof;
            # local names are established only by parameters/ordered bindings.
            if (
                key not in bindings
                and isinstance(node.value, ast.Name)
                and node.value.id == "self"
                and node.attr in owners
            ):
                bindings[key] = fresh()
            return bindings.get(key)
        if isinstance(node, (ast.Tuple, ast.List)):
            return [evaluate(item) for item in node.elts]
        if isinstance(node, ast.Call):
            callee = evaluate(node.func)
            for item in node.args:
                evaluate(item)
            for keyword in node.keywords:
                evaluate(keyword.value)
            record_exception_state()
            if node is call:
                receiver = evaluate(argument.value)
                valid = (
                    isinstance(receiver, tuple)
                    and receiver[0] == "owner"
                    and receiver[1].isdisjoint(invalid)
                )
                answer = answer is not False and valid
            elif isinstance(callee, tuple) and callee[0] in {"terminal", "uncertain"}:
                invalid.update(callee[1])
            return fresh(node) if _is_ownerish_call(node, aliases) else None
        if isinstance(node, ast.NamedExpr):
            value = evaluate(node.value)
            bind(node.target, value)
            return value
        if isinstance(node, ast.Lambda):
            return None
        for child in ast.iter_child_nodes(node):
            evaluate(child)
        return None

    def alternatives(bodies):
        before = dict(bindings)
        outcomes = []
        for body in bodies:
            bindings.clear()
            bindings.update(before)
            statements(body)
            if finished():
                return
            outcomes.append(dict(bindings))
        merge_states(outcomes)

    def merge_states(outcomes):
        bindings.clear()
        for name in set().union(*(state.keys() for state in outcomes)):
            value = outcomes[0].get(name)
            values = [state.get(name) for state in outcomes]
            if all(item == value for item in values):
                bindings[name] = value
            else:
                # Keep possible identities even when a binding is uncertain:
                # invoking a possibly captured release still invalidates them.
                known = [item for item in values if isinstance(item, tuple)]
                identities = frozenset().union(*(item[1] for item in known))
                kind = (
                    known[0][0]
                    if len(known) == len(values)
                    and all(item[0] == known[0][0] for item in known)
                    else "uncertain"
                )
                bindings[name] = (kind, identities) if known else None

    def collect_entries(body, entries):
        exception_collectors.append(entries)
        try:
            statements(body)
            record_exception_state()
        finally:
            exception_collectors.pop()

    def try_statement(node):
        # Handlers can run after any prefix of the try body, not just from
        # pre-try bindings or successful completion. Else is outside their
        # protected region; finally also sees failures in else/handlers.
        final_entries = [dict(bindings)]
        if node.finalbody:
            exception_collectors.append(final_entries)
        try:
            exceptional = [dict(bindings)]
            collect_entries(node.body, exceptional)
            if finished():
                return
            statements(node.orelse)
            if finished():
                return
            outcomes = [dict(bindings)]
            for handler in node.handlers:
                merge_states(exceptional)
                evaluate(handler.type)
                if handler.name:
                    bindings[handler.name] = None
                if isinstance(node, ast.TryStar):
                    # Multiple except* handlers may run, including after a
                    # preceding handler itself fails partway through.
                    collect_entries(handler.body, exceptional)
                else:
                    statements(handler.body)
                if finished():
                    return
                if handler.name:
                    # Python deletes an exception target at handler exit.
                    bindings[handler.name] = None
                outcomes.append(dict(bindings))
            merge_states(outcomes)
        finally:
            if node.finalbody:
                exception_collectors.pop()
        if node.finalbody:
            merge_states([dict(bindings)] + final_entries)
            statements(node.finalbody)

    def statements(body):
        nonlocal loop_depth, analysis_failed
        for node in body:
            if finished():
                return
            record_exception_state()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                # Definition rebinds its name, but does not run its body.
                bindings[node.name] = None
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = evaluate(node.value)
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                for target in targets:
                    bind(target, value)
            elif isinstance(node, (ast.AugAssign, ast.Delete)):
                evaluate(node)
                targets = (
                    node.targets if isinstance(node, ast.Delete) else [node.target]
                )
                for target in targets:
                    bind(target, None)
            elif isinstance(node, ast.If):
                evaluate(node.test)
                alternatives([node.body, node.orelse])
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
                loop_depth += 1
                if isinstance(node, (ast.For, ast.AsyncFor)):
                    evaluate(node.iter)
                # Include the zero-iteration edge and keep traversing after a
                # matching call to account for releases/rebindings on backedges.
                # No loop bound, break/continue or termination proof is assumed.
                for _ in range(2 * sum(1 for _ in ast.walk(node)) + 1):
                    before, released = dict(bindings), set(invalid)
                    if isinstance(node, ast.While):
                        evaluate(node.test)
                    else:
                        bind(node.target, None)
                    alternatives([node.body, []])
                    if bindings == before and invalid == released:
                        break
                else:
                    # Unsupported non-finite abstract state must not supply
                    # wrapper-close authority through a partial analysis.
                    analysis_failed = True
                loop_depth -= 1
                statements(node.orelse)
            elif isinstance(node, (ast.Try, ast.TryStar)):
                try_statement(node)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                contexts = []
                for item in node.items:
                    value = evaluate(item.context_expr)
                    contexts.append(value)
                    if item.optional_vars is not None:
                        bind(item.optional_vars, value)
                statements(node.body)
                if not finished():
                    for value in contexts:
                        if isinstance(value, tuple) and value[0] in {
                            "owner",
                            "uncertain",
                        }:
                            invalid.update(value[1])
            else:
                evaluate(node)

    statements(function.body)
    return answer is True and not analysis_failed


def _fdopen_violations(
    function: ast.FunctionDef,
    owners: set[str],
    aliases: dict[str, str],
) -> list[str]:
    """Reject implicit ``fdopen`` ownership; accept only a proven owner fd.

    An omitted ``closefd`` is treated as ``True`` and rejected.  ``closefd=False``
    is accepted only when the descriptor argument is a proven shared-owner
    attribute (``owner.fd``).
    """
    aliases = _local_primitive_aliases(function, aliases)
    violations: list[str] = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        if _call_origin(node, aliases) not in _FDOPEN_QUALNAMES:
            continue
        closefd = next((kw for kw in node.keywords if kw.arg == "closefd"), None)
        if closefd is None:
            violations.append(f"implicit closefd=True at line {node.lineno}")
        elif ast.unparse(closefd.value) != "False":
            violations.append(
                f"fdopen closefd={ast.unparse(closefd.value)} at line {node.lineno}"
            )
        else:
            if not _live_owner_fd(function, node, owners, aliases):
                violations.append(
                    f"fdopen closefd=False without proven owner at line {node.lineno}"
                )
    return violations


def _file_object_names(
    function: ast.FunctionDef,
    owners: set[str],
    aliases: dict[str, str],
) -> set[str]:
    """Names bound to a file object opened from a proven owner fd.

    An explicit ``fh = os.fdopen(owner.fd, ..., closefd=False)`` is the only
    accepted file-object form: the descriptor stays owned by the shared owner
    and the file object is a legitimate explicit-cleanup receiver, so its
    ``fh.close()`` is not an unresolved consumer close.
    """
    aliases = _local_primitive_aliases(function, aliases)
    names: set[str] = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Assign):
            value, targets = node.value, node.targets
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            value, targets = node.value, [node.target]
        else:
            continue
        if not isinstance(value, ast.Call):
            continue
        if _call_origin(value, aliases) not in _FDOPEN_QUALNAMES:
            continue
        closefd = next((kw for kw in value.keywords if kw.arg == "closefd"), None)
        if closefd is None or ast.unparse(closefd.value) != "False":
            continue
        if not _live_owner_fd(function, value, owners, aliases):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
    return names


#: Constructs whose body runs only conditionally.  An adoption nested in one
#: of these does not happen on every path and cannot protect the descriptor.
_CONDITIONAL_NODES = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.Try,
    ast.Match,
    ast.IfExp,
    ast.BoolOp,
    ast.Lambda,
)


def _parent_map(function: ast.FunctionDef) -> dict[int, ast.AST]:
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(function):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    return parents


def _node_order(function: ast.FunctionDef) -> dict[int, int]:
    """Return a source-order (pre-order DFS) index for every node."""
    order: dict[int, int] = {}
    counter = 0

    def visit(node: ast.AST) -> None:
        nonlocal counter
        order[id(node)] = counter
        counter += 1
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(function)
    return order


def _has_ancestor(
    node: ast.AST, ancestor: ast.AST, parents: dict[int, ast.AST]
) -> bool:
    current = parents.get(id(node))
    while current is not None:
        if current is ancestor:
            return True
        current = parents.get(id(current))
    return False


def _is_conditionally_guarded(node: ast.AST, parents: dict[int, ast.AST]) -> bool:
    """True when *node* executes only inside a conditional construct."""
    current = parents.get(id(node))
    while current is not None:
        if isinstance(current, _CONDITIONAL_NODES):
            return True
        current = parents.get(id(current))
    return False


def _enclosing_statement(node: ast.AST, parents: dict[int, ast.AST]) -> ast.stmt | None:
    current: ast.AST | None = node
    while current is not None:
        if isinstance(current, ast.stmt):
            return current
        current = parents.get(id(current))
    return None


def _mkstemp_fd_names(statement: ast.stmt, aliases: dict[str, str]) -> set[str]:
    """Return the fd name(s) bound by a ``tempfile.mkstemp()`` statement."""
    if isinstance(statement, ast.Assign):
        value, targets = statement.value, list(statement.targets)
    elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
        value, targets = statement.value, [statement.target]
    else:
        return set()
    if not (
        isinstance(value, ast.Call)
        and _call_origin(value, aliases) in _MKSTEMP_QUALNAMES
    ):
        return set()
    names: set[str] = set()
    for target in targets:
        if (
            isinstance(target, ast.Tuple)
            and target.elts
            and isinstance(target.elts[0], ast.Name)
        ):
            names.add(target.elts[0].id)
        elif isinstance(target, ast.Name):
            names.add(target.id)
    return names


def _passes_fd(call: ast.Call, fd_names: set[str]) -> bool:
    for arg in list(call.args) + [kw.value for kw in call.keywords]:
        if isinstance(arg, ast.Name) and arg.id in fd_names:
            return True
    return False


def _fallible_before(
    function: ast.FunctionDef,
    mk_stmt: ast.stmt,
    adopt_stmt: ast.stmt,
    parents: dict[int, ast.AST],
    order: dict[int, int],
    fd_names: set[str],
) -> bool:
    """True when a call or fd use sits between *mk_stmt* and *adopt_stmt*."""
    low, high = order[id(mk_stmt)], order[id(adopt_stmt)]
    for node in ast.walk(function):
        if not (low < order[id(node)] < high):
            continue
        if _has_ancestor(node, mk_stmt, parents):
            # Part of the ``mkstemp()`` expression itself.
            continue
        if isinstance(node, ast.Call):
            return True
        if isinstance(node, ast.Name) and node.id in fd_names:
            return True
    return False


def _adoption_is_proven(
    function: ast.FunctionDef,
    mk_stmt: ast.stmt,
    adopt: ast.Call,
    parents: dict[int, ast.AST],
    order: dict[int, int],
    fd_names: set[str],
) -> bool:
    adopt_stmt = _enclosing_statement(adopt, parents)
    if adopt_stmt is None:
        return False
    if order[id(adopt_stmt)] <= order[id(mk_stmt)]:
        return False
    if _is_conditionally_guarded(adopt_stmt, parents):
        return False
    return not _fallible_before(function, mk_stmt, adopt_stmt, parents, order, fd_names)


def _mkstemp_violations(
    function: ast.FunctionDef,
    owners: set[str],
    aliases: dict[str, str],
) -> list[str]:
    """Require each ``mkstemp()`` fd to be adopted unconditionally and first.

    A temporary descriptor is safe only once a shared owner holds it.  The
    owner transfer must therefore happen on every path (not behind an ``if``)
    and before any other fallible operation, so an initialization failure can
    neither leak the descriptor nor leave the temporary entry behind.  Finding
    an owner call *somewhere* in the function is not enough.
    """
    aliases = _local_primitive_aliases(function, aliases)
    parents = _parent_map(function)
    order = _node_order(function)
    violations: list[str] = []
    for mk_stmt in [node for node in ast.walk(function) if isinstance(node, ast.stmt)]:
        fd_names = _mkstemp_fd_names(mk_stmt, aliases)
        if not fd_names:
            continue
        adoptions = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and _is_ownerish_call(node, aliases)
            and _passes_fd(node, fd_names)
        ]
        if not any(
            _adoption_is_proven(function, mk_stmt, adopt, parents, order, fd_names)
            for adopt in adoptions
        ):
            violations.append(f"unadopted mkstemp fd {sorted(fd_names)}")
    return violations


def _phase6_function_nodes():
    """Yield ``(label, tree, aliases, function, class_owners)`` per function."""
    for rel, names in {**_PHASE6_FUNCTIONS, **_PHASE6B_STREAM_FUNCTIONS}.items():
        tree = _parse(_REPO / rel)
        aliases = _module_aliases(tree)
        for name in names:
            yield f"{rel}::{name}", tree, aliases, _find_function(tree, name), set()
    for rel, methods in {**_PHASE6_METHODS, **_PHASE6B_STREAM_METHODS}.items():
        tree = _parse(_REPO / rel)
        aliases = _module_aliases(tree)
        for class_name, method_name in methods:
            function = _find_method(tree, class_name, method_name)
            yield (
                f"{rel}::{class_name}.{method_name}",
                tree,
                aliases,
                function,
                _class_owner_names(tree, class_name, aliases),
            )


def _adopted_fd_argument(call: ast.Call, aliases: dict[str, str]) -> ast.expr | None:
    """Return the integer-fd argument of a shared-descriptor adoption call."""
    loc = _qualname(call.func, aliases)
    if "OwnedDescriptor" not in loc and "DirectoryDescriptor" not in loc:
        return None
    if len(call.args) >= 2:
        return call.args[1]
    for keyword in call.keywords:
        if keyword.arg == "fd":
            return keyword.value
    return None


def _duplicate_fd_adoption_violations(
    function: ast.FunctionDef, aliases: dict[str, str]
) -> list[str]:
    """Reject one integer descriptor adopted by two independent owners."""
    seen: dict[str, int] = {}
    violations: list[str] = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        fd_arg = _adopted_fd_argument(node, aliases)
        if fd_arg is None or not isinstance(fd_arg, ast.Name):
            continue
        if fd_arg.id in seen:
            violations.append(
                f"duplicate fd adoption {fd_arg.id} at line {node.lineno}"
            )
        seen[fd_arg.id] = node.lineno
    return violations


def _ownership_ledger() -> dict[str, dict[str, object]]:
    """Generate the per-function ownership ledger for tasks 6.1–6.18."""
    ledger: dict[str, dict[str, object]] = {}
    for label, _tree, aliases, function, class_owners in _phase6_function_nodes():
        owners = _ownerish_names(function, aliases) | class_owners
        # A file object explicitly opened from a proven owner fd is a
        # permitted cleanup receiver; the shared owner remains the descriptor
        # authority and is still required to be released.
        close_owners = owners | _file_object_names(function, owners, aliases)
        ledger[label] = {
            "owners": sorted(owners),
            "close_violations": _close_violations_with_owners(
                function,
                close_owners,
                aliases,
                shared_owner_release=label in _VERIFIED_SHARED_OWNER_RELEASES,
            ),
            "fdopen_violations": _fdopen_violations(function, owners, aliases),
            "mkstemp_violations": _mkstemp_violations(function, owners, aliases),
            "raw_close_targets": _raw_close_targets(function, aliases),
            "duplicate_fd_adoption_violations": _duplicate_fd_adoption_violations(
                function, aliases
            ),
        }
    return ledger


class GeneratedOwnershipLedgerTests(unittest.TestCase):
    """Task 6.37 — the generated per-function ownership ledger stays clean."""

    def test_ledger_covers_every_named_phase6_function(self) -> None:
        ledger = _ownership_ledger()
        for mapping in (_PHASE6_FUNCTIONS, _PHASE6B_STREAM_FUNCTIONS):
            for rel, names in mapping.items():
                for name in names:
                    self.assertIn(f"{rel}::{name}", ledger)
        for mapping in (_PHASE6_METHODS, _PHASE6B_STREAM_METHODS):
            for rel, methods in mapping.items():
                for class_name, method_name in methods:
                    self.assertIn(f"{rel}::{class_name}.{method_name}", ledger)

    def test_no_unresolved_close_like_call(self) -> None:
        for label, facts in _ownership_ledger().items():
            with self.subTest(function=label):
                self.assertEqual(facts["close_violations"], [])

    def test_verified_shared_owner_releases_are_ledger_entries(self) -> None:
        # Every backend-close exemption must name a real scanned
        # implementation whose proven terminal/repeat-safe release stays
        # clean; an exemption cannot be granted to code the ledger never saw.
        ledger = _ownership_ledger()
        self.assertTrue(_VERIFIED_SHARED_OWNER_RELEASES)
        for label in sorted(_VERIFIED_SHARED_OWNER_RELEASES):
            with self.subTest(implementation=label):
                self.assertIn(label, ledger)
                self.assertEqual(ledger[label]["close_violations"], [])

    def test_no_implicit_fdopen_ownership(self) -> None:
        for label, facts in _ownership_ledger().items():
            with self.subTest(function=label):
                self.assertEqual(facts["fdopen_violations"], [])

    def test_every_mkstemp_fd_transfers_to_an_owner(self) -> None:
        for label, facts in _ownership_ledger().items():
            with self.subTest(function=label):
                self.assertEqual(facts["mkstemp_violations"], [])

    def test_no_former_consumer_close_remains(self) -> None:
        for label, facts in _ownership_ledger().items():
            with self.subTest(function=label):
                self.assertEqual(facts["raw_close_targets"], [])

    def test_every_fd_adopts_at_most_one_owner(self) -> None:
        for label, facts in _ownership_ledger().items():
            with self.subTest(function=label):
                self.assertEqual(facts["duplicate_fd_adoption_violations"], [])


def _inline_ledger(source: str):
    tree = ast.parse(textwrap.dedent(source))
    aliases = _module_aliases(tree)
    function = _find_function(tree, "f")
    return function, _ownerish_names(function, aliases), aliases


class OwnershipLedgerEvasionTests(unittest.TestCase):
    """Task 6.37 — aliased/implicit primitives cannot evade the ledger."""

    def test_origin_resolved_memory_buffer_close_is_accepted(self) -> None:
        for imports, factory in (
            ("import io as memory", "memory.StringIO"),
            ("from io import BytesIO as Buffer", "Buffer"),
        ):
            with self.subTest(factory=factory):
                function, owners, aliases = _inline_ledger(
                    f"{imports}\ndef f():\n    {factory}().close()\n"
                )
                self.assertEqual(
                    _close_violations_with_owners(function, owners, aliases), []
                )

    def test_unproven_memory_buffer_close_is_rejected(self) -> None:
        cases = (
            "from unknown import StringIO\ndef f():\n    StringIO().close()",
            "from unknown import Buffer\nfrom io import StringIO as Buffer\ndef f():\n    Buffer().close()",
            "from io import StringIO as Buffer\nBuffer = unknown\ndef f():\n    Buffer().close()",
            "import io as memory\nmemory.StringIO = unknown\ndef f():\n    memory.StringIO().close()",
            "from .io import StringIO as Buffer\ndef f():\n    Buffer().close()",
            "from io import StringIO as Buffer\nclass Buffer:\n    pass\ndef f():\n    Buffer().close()",
            "from io import StringIO as Buffer\ndef f(value):\n    match value:\n        case Buffer:\n            Buffer().close()",
            "from io import StringIO as Buffer\ndef f(Buffer):\n    Buffer().close()",
            "from io import StringIO as Buffer\ndef f(other):\n    Buffer = other\n    Buffer().close()",
            "import io as memory\ndef f(other):\n    memory.StringIO = other\n    memory.StringIO().close()",
            "from io import StringIO as Buffer\ndef f(other, flag):\n    if flag:\n        Buffer = other\n    Buffer().close()",
            "from io import StringIO as Buffer\ndef f(other):\n    value = Buffer()\n    value = other\n    value.close()",
        )
        for source in cases:
            with self.subTest(source=source):
                function, owners, aliases = _inline_ledger(source)
                self.assertTrue(
                    _close_violations_with_owners(function, owners, aliases)
                )

    def test_transitively_assigned_fdopen_aliases_are_rejected(self) -> None:
        for expression in (
            'wrap(fd, "w")',
            'wrap(fd, "w", closefd=True)',
            'wrap(fd, "w", closefd=False)',
        ):
            with self.subTest(expression=expression):
                function, owners, aliases = _inline_ledger(
                    "import os as operating\n"
                    "def f(fd):\n"
                    "    first = operating.fdopen\n"
                    "    wrap = first\n"
                    f"    {expression}\n"
                )
                self.assertNotEqual(_fdopen_violations(function, owners, aliases), [])

    def test_transitively_assigned_mkstemp_alias_is_rejected(self) -> None:
        function, owners, aliases = _inline_ledger(
            "from tempfile import mkstemp as create\n"
            "def f():\n"
            "    first = create\n"
            "    acquire = first\n"
            "    fd, path = acquire()\n"
            "    return fd\n"
        )
        self.assertNotEqual(_mkstemp_violations(function, owners, aliases), [])

    def test_fdopen_requires_a_live_descriptor_attribute(self) -> None:
        for setup, argument in (
            ("owner.close()\n", "owner.fd"),
            ("owner.detach()\n", "owner.fd"),
            ("", "owner.label"),
        ):
            with self.subTest(setup=setup, argument=argument):
                function, owners, aliases = _inline_ledger(
                    "import os\n"
                    "from docker.filesystem.descriptors import OwnedDescriptor\n"
                    "def f(owner: OwnedDescriptor):\n"
                    + "".join("    " + line + "\n" for line in setup.splitlines())
                    + f'    stream = os.fdopen({argument}, "w", closefd=False)\n'
                )
                self.assertNotEqual(_fdopen_violations(function, owners, aliases), [])
                self.assertEqual(_file_object_names(function, owners, aliases), set())

    def test_fdopen_rejects_invalidated_or_reassigned_owner_bindings(self) -> None:
        cases = (
            'owner.close(); stream = os.fdopen(owner.fd, "w", closefd=False)',
            'owner.detach(); stream = os.fdopen(owner.fd, "w", closefd=False)',
            'alias = owner\nalias.close()\nstream = os.fdopen(owner.fd, "w", closefd=False)',
            'alias = owner\nowner.detach()\nstream = os.fdopen(alias.fd, "w", closefd=False)',
            'first = owner\nsecond = first\nsecond.close(); stream = os.fdopen(owner.fd, "w", closefd=False)',
            'release = owner.close\nfinish = release\nfinish(); stream = os.fdopen(owner.fd, "w", closefd=False)',
            'transfer = owner.detach\ntransfer()\nstream = os.fdopen(owner.fd, "w", closefd=False)',
            'owner = other\nstream = os.fdopen(owner.fd, "w", closefd=False)',
            'alias = owner\nalias = other\nstream = os.fdopen(alias.fd, "w", closefd=False)',
            'release = owner.close\nalias = owner\nowner = other\nrelease()\nstream = os.fdopen(alias.fd, "w", closefd=False)',
            'if condition:\n    owner.close()\nstream = os.fdopen(owner.fd, "w", closefd=False)',
            'if condition:\n    owner = other\nstream = os.fdopen(owner.fd, "w", closefd=False)',
            'alias = owner\nif condition:\n    alias = other\nalias.close()\nstream = os.fdopen(owner.fd, "w", closefd=False)',
            'release = owner.close\nif condition:\n    release = other\nrelease()\nstream = os.fdopen(owner.fd, "w", closefd=False)',
            'stream = os.fdopen((owner.close(), owner)[1].fd, "w", closefd=False)',
            'release = owner.close\nstream = os.fdopen(owner.fd, (release(), "w")[1], closefd=False)',
            'del owner\nstream = os.fdopen(owner.fd, "w", closefd=False)',
            'stream = os.fdopen(local.fd, "w", closefd=False)\nlocal = OwnedDescriptor(ops, fd, label="late")',
        )
        for body in cases:
            with self.subTest(body=body):
                function, owners, aliases = _inline_ledger(
                    "import os\n"
                    "from docker.filesystem.descriptors import OwnedDescriptor\n"
                    "def f(owner: OwnedDescriptor, other, condition):\n"
                    + textwrap.indent(body, "    ")
                )
                with self.subTest(check="_fdopen_violations"):
                    self.assertTrue(_fdopen_violations(function, owners, aliases))
                with self.subTest(check="_file_object_names"):
                    self.assertEqual(
                        _file_object_names(function, owners, aliases), set()
                    )

    def test_fdopen_rejects_exception_path_owner_invalidation(self) -> None:
        cases = (
            "try:\n    alias = owner\n    work()\nexcept Exception:\n    alias.close()",
            "try:\n    alias = owner\n    work()\nexcept Exception:\n    alias.detach()",
            "try:\n    release = owner.close\n    work()\nexcept Exception:\n    release()",
            "try:\n    release = owner.detach\n    work()\nexcept Exception:\n    release()",
            "try:\n    first = owner\n    second = first\n    work()\nexcept Exception:\n    second.close()",
            "try:\n    alias = owner\n    work()\n    alias = other\nexcept Exception:\n    alias.close()",
            "try:\n    release = owner.close\n    work()\n    release = other\nexcept Exception:\n    release()",
            "try:\n    work(alias := owner)\nexcept Exception:\n    alias.close()",
            "try:\n    if condition:\n        alias = owner\n    work()\nexcept Exception:\n    alias.close()",
            "try:\n    try:\n        alias = owner\n        work()\n    finally:\n        pass\nexcept Exception:\n    alias.close()",
            "try:\n    alias = owner\n    work()\nexcept* Exception:\n    alias.close()",
            "try:\n    work()\nexcept Exception as owner:\n    pass",
            "try:\n    alias = work(alias := owner)\nexcept Exception:\n    alias.close()",
            "try:\n    work()\nexcept* ValueError:\n    alias = owner\n    work()\n    alias = other\nexcept* TypeError:\n    alias.close()",
            "try:\n    alias = owner\n    work()\n    alias = other\nfinally:\n    alias.close()",
            "try:\n    work()\nexcept Exception:\n    pass\nelse:\n    alias = owner\n    work()\n    alias = other\nfinally:\n    alias.close()",
            "try:\n    work()\nexcept Exception:\n    alias = owner\n    work()\n    alias = other\nfinally:\n    alias.close()",
        )
        for body in cases:
            with self.subTest(body=body):
                function, owners, aliases = _inline_ledger(
                    "import os\n"
                    "from docker.filesystem.descriptors import OwnedDescriptor\n"
                    "def f(owner: OwnedDescriptor, other, condition):\n"
                    + textwrap.indent(body, "    ")
                    + '\n    stream = os.fdopen(owner.fd, "w", closefd=False)\n'
                )
                with self.subTest(check="_fdopen_violations"):
                    self.assertTrue(_fdopen_violations(function, owners, aliases))
                with self.subTest(check="_file_object_names"):
                    self.assertEqual(
                        _file_object_names(function, owners, aliases), set()
                    )

    def test_fdopen_accepts_exception_paths_preserving_the_owner(self) -> None:
        cases = (
            "try:\n    work()\nexcept Exception:\n    pass",
            "try:\n    alias = other\n    work()\nexcept Exception:\n    alias.close()",
            "try:\n    release = other.close\n    work()\nexcept Exception:\n    release()",
            "alias = other\ntry:\n    work()\nexcept Exception:\n    alias.close()\nelse:\n    alias = owner",
            'try:\n    work()\nexcept Exception:\n    stream = os.fdopen(owner.fd, "w", closefd=False)\n    owner.close()',
            "release = owner.close\ntry:\n    work()\nexcept Exception as release:\n    pass",
            'alias = owner\ntry:\n    work()\nexcept Exception:\n    pass\nstream = os.fdopen(alias.fd, "w", closefd=False)',
            "try:\n    alias = other\n    work()\nfinally:\n    alias.close()",
            'try:\n    stream = os.fdopen(owner.fd, "w", closefd=False)\nfinally:\n    owner.close()',
        )
        for body in cases:
            with self.subTest(body=body):
                function, owners, aliases = _inline_ledger(
                    "import os\n"
                    "from docker.filesystem.descriptors import OwnedDescriptor\n"
                    "def f(owner: OwnedDescriptor, other: OwnedDescriptor):\n"
                    + textwrap.indent(body, "    ")
                    + (
                        '\n    stream = os.fdopen(owner.fd, "w", closefd=False)\n'
                        if "os.fdopen" not in body
                        else "\n"
                    )
                )
                with self.subTest(check="_fdopen_violations"):
                    self.assertEqual(_fdopen_violations(function, owners, aliases), [])
                with self.subTest(check="_file_object_names"):
                    self.assertEqual(
                        _file_object_names(function, owners, aliases), {"stream"}
                    )

    def test_fdopen_rejects_loop_carried_owner_invalidation(self) -> None:
        cases = (
            'for i in range(2):\n    stream = os.fdopen(owner.fd, "w", closefd=False); owner.close()',
            'while condition:\n    stream = os.fdopen(owner.fd, "w", closefd=False)\n    owner.detach()',
            'alias = owner\nfor i in range(2):\n    stream = os.fdopen(owner.fd, "w", closefd=False)\n    alias.close()',
            'release = owner.close\nfor i in range(2):\n    stream = os.fdopen(owner.fd, "w", closefd=False)\n    release()',
            'for i in range(2):\n    stream = os.fdopen(owner.fd, "w", closefd=False)\n    owner = other',
            'for i in range(2):\n    for j in range(2):\n        stream = os.fdopen(owner.fd, "w", closefd=False)\n    owner.close()',
            'first = second = other\nfor i in range(4):\n    stream = os.fdopen(owner.fd, "w", closefd=False)\n    first.close()\n    first = second\n    second = owner',
            'for i in range(2):\n    with owner:\n        stream = os.fdopen(owner.fd, "w", closefd=False)',
            'for i in range(2):\n    try:\n        stream = os.fdopen(owner.fd, "w", closefd=False)\n    finally:\n        owner.close()',
            'for i in range(2):\n    owner = OwnedDescriptor(ops, fd, label="site")\n    stream = os.fdopen(owner.fd, "w", closefd=False)\n    owner.close()',
        )
        for body in cases:
            with self.subTest(body=body):
                function, owners, aliases = _inline_ledger(
                    "import os\n"
                    "from docker.filesystem.descriptors import OwnedDescriptor\n"
                    "def f(owner: OwnedDescriptor, other, condition):\n"
                    + textwrap.indent(body, "    ")
                )
                with self.subTest(check="_fdopen_violations"):
                    self.assertTrue(_fdopen_violations(function, owners, aliases))
                with self.subTest(check="_file_object_names"):
                    self.assertEqual(
                        _file_object_names(function, owners, aliases), set()
                    )

    def test_fdopen_accepts_loops_without_carried_owner_invalidation(self) -> None:
        cases = (
            'for i in range(2):\n    stream = os.fdopen(owner.fd, "w", closefd=False)',
            'while condition:\n    stream = os.fdopen(owner.fd, "w", closefd=False)\nowner.close()',
            'for i in range(2):\n    stream = os.fdopen(owner.fd, "w", closefd=False)\n    other.close()',
            'for i in range(2):\n    for j in range(2):\n        stream = os.fdopen(owner.fd, "w", closefd=False)',
            'for i in range(2):\n    alias = owner\n    stream = os.fdopen(alias.fd, "w", closefd=False)',
            'for i in range(2):\n    local = OwnedDescriptor(ops, fd, label="site")\n    stream = os.fdopen(local.fd, "w", closefd=False)',
        )
        for body in cases:
            with self.subTest(body=body):
                function, owners, aliases = _inline_ledger(
                    "import os\n"
                    "from docker.filesystem.descriptors import OwnedDescriptor\n"
                    "def f(owner: OwnedDescriptor, other: OwnedDescriptor, condition):\n"
                    + textwrap.indent(body, "    ")
                )
                self.assertEqual(_fdopen_violations(function, owners, aliases), [])
                self.assertEqual(
                    _file_object_names(function, owners, aliases), {"stream"}
                )

    def test_fdopen_liveness_uses_the_binding_at_the_call(self) -> None:
        cases = (
            'alias = owner\nstream = os.fdopen(alias.fd, "w", closefd=False)',
            'stream = os.fdopen(owner.fd, "w", closefd=False); owner.close()',
            'alias = owner\nowner = other\nstream = os.fdopen(alias.fd, "w", closefd=False)',
            'release = owner.close\nrelease = other\nstream = os.fdopen(owner.fd, "w", closefd=False)',
            'def later():\n    owner.close()\nstream = os.fdopen(owner.fd, "w", closefd=False)',
            'alias = owner\nowner = OwnedDescriptor(ops, fd, label="new")\nalias.close()\nstream = os.fdopen(owner.fd, "w", closefd=False)',
            'release = owner.close\nowner = OwnedDescriptor(ops, fd, label="new")\nrelease()\nstream = os.fdopen(owner.fd, "w", closefd=False)',
        )
        for body in cases:
            with self.subTest(body=body):
                function, owners, aliases = _inline_ledger(
                    "import os\n"
                    "from docker.filesystem.descriptors import OwnedDescriptor\n"
                    "def f(owner: OwnedDescriptor, other):\n"
                    + textwrap.indent(body, "    ")
                )
                self.assertEqual(_fdopen_violations(function, owners, aliases), [])
                self.assertEqual(
                    _file_object_names(function, owners, aliases), {"stream"}
                )

    def test_transitive_fdopen_alias_with_live_owner_is_accepted(self) -> None:
        function, owners, aliases = _inline_ledger(
            "import os\n"
            "from docker.filesystem.descriptors import OwnedDescriptor\n"
            "def f(owner: OwnedDescriptor):\n"
            "    first = os.fdopen\n"
            "    wrap = first\n"
            '    stream = wrap(owner.fd, "w", closefd=False)\n'
        )
        self.assertEqual(_fdopen_violations(function, owners, aliases), [])
        self.assertEqual(_file_object_names(function, owners, aliases), {"stream"})

    def test_conflicting_primitive_aliases_fail_closed(self) -> None:
        function, owners, aliases = _inline_ledger(
            "import os, tempfile\n"
            "def f(fd):\n"
            "    operation = os.fdopen\n"
            "    operation = tempfile.mkstemp\n"
            "    operation(fd)\n"
        )
        with self.assertRaises(AssertionError):
            _fdopen_violations(function, owners, aliases)

    def test_aliased_fdopen_without_closefd_is_rejected(self) -> None:
        function, owners, aliases = _inline_ledger(
            """
            import os as o

            def f(fd):
                o.fdopen(fd, "w")
            """
        )
        self.assertNotEqual(_fdopen_violations(function, owners, aliases), [])

    def test_fdopen_closefd_false_without_owner_is_rejected(self) -> None:
        function, owners, aliases = _inline_ledger(
            """
            import os

            def f(fd):
                os.fdopen(fd, "w", closefd=False)
            """
        )
        self.assertNotEqual(_fdopen_violations(function, owners, aliases), [])

    def test_fdopen_closefd_false_with_owner_is_accepted(self) -> None:
        function, owners, aliases = _inline_ledger(
            """
            import os
            from docker.filesystem.descriptors import OwnedDescriptor
            from docker.filesystem.operations import PosixDescriptorOps

            def f(fd):
                owner = OwnedDescriptor(PosixDescriptorOps(), fd, label="e")
                os.fdopen(owner.fd, "w", closefd=False)
            """
        )
        self.assertEqual(_fdopen_violations(function, owners, aliases), [])

    def test_aliased_mkstemp_without_adoption_is_rejected(self) -> None:
        function, owners, aliases = _inline_ledger(
            """
            import tempfile as t

            def f():
                fd, path = t.mkstemp()
                return fd
            """
        )
        self.assertNotEqual(_mkstemp_violations(function, owners, aliases), [])

    def test_mkstemp_adoption_is_accepted(self) -> None:
        function, owners, aliases = _inline_ledger(
            """
            import tempfile
            from docker.filesystem.descriptors import OwnedDescriptor
            from docker.filesystem.operations import PosixDescriptorOps

            def f():
                fd, path = tempfile.mkstemp()
                owner = OwnedDescriptor(PosixDescriptorOps(), fd, label="e")
                return owner.detach()
            """
        )
        self.assertEqual(_mkstemp_violations(function, owners, aliases), [])

    def test_mkstemp_fd_use_before_adoption_is_rejected(self) -> None:
        function, owners, aliases = _inline_ledger(
            """
            import os
            import tempfile
            from docker.filesystem.descriptors import OwnedDescriptor
            from docker.filesystem.operations import PosixDescriptorOps

            def f():
                fd, path = tempfile.mkstemp()
                os.fchmod(fd, 0o600)
                owner = OwnedDescriptor(PosixDescriptorOps(), fd, label="e")
                return owner.detach()
            """
        )
        self.assertNotEqual(_mkstemp_violations(function, owners, aliases), [])

    def test_mkstemp_fallible_operation_before_adoption_is_rejected(self) -> None:
        function, owners, aliases = _inline_ledger(
            """
            import hashlib
            import tempfile
            from docker.filesystem.descriptors import OwnedDescriptor
            from docker.filesystem.operations import PosixDescriptorOps

            def f():
                fd, path = tempfile.mkstemp()
                hashlib.sha256()
                owner = OwnedDescriptor(PosixDescriptorOps(), fd, label="e")
                return owner.detach()
            """
        )
        self.assertNotEqual(_mkstemp_violations(function, owners, aliases), [])

    def test_mkstemp_conditional_adoption_is_rejected(self) -> None:
        function, owners, aliases = _inline_ledger(
            """
            import tempfile
            from docker.filesystem.descriptors import OwnedDescriptor
            from docker.filesystem.operations import PosixDescriptorOps

            def f(flag):
                fd, path = tempfile.mkstemp()
                if flag:
                    owner = OwnedDescriptor(PosixDescriptorOps(), fd, label="e")
                return fd
            """
        )
        self.assertNotEqual(_mkstemp_violations(function, owners, aliases), [])

    def test_injected_backend_field_close_is_rejected(self) -> None:
        function, owners, aliases = _inline_ledger(
            """
            def f(self, fd):
                self._ops.close(fd)
            """
        )
        self.assertNotEqual(
            _close_violations_with_owners(function, owners, aliases), []
        )

    def test_injected_owner_parameter_is_accepted(self) -> None:
        function, owners, aliases = _inline_ledger(
            """
            from docker.filesystem.descriptors import OwnedDescriptor

            def f(owner: OwnedDescriptor):
                owner.close()
            """
        )
        self.assertEqual(_close_violations_with_owners(function, owners, aliases), [])

    def test_consumer_backend_close_disguised_as_shared_owner_is_rejected(self) -> None:
        # Mimicking the full terminal/guard shape does not make the enclosing
        # implementation a verified shared owner, so no exemption is granted.
        function, owners, aliases = _inline_ledger(
            """
            def f(self, fd):
                if not self._closed:
                    self._closed = True
                    self._ops.close(fd)
            """
        )
        self.assertNotEqual(
            _close_violations_with_owners(function, owners, aliases), []
        )

    def test_verified_shared_owner_release_is_accepted(self) -> None:
        function, owners, aliases = _inline_ledger(
            """
            def f(self, fd):
                if not self._closed:
                    self._closed = True
                    self._ops.close(fd)
            """
        )
        self.assertEqual(
            _close_violations_with_owners(
                function, owners, aliases, shared_owner_release=True
            ),
            [],
        )

    def test_shared_owner_release_without_repeat_guard_is_rejected(self) -> None:
        # The verified identity alone is not enough: a claimed shared owner
        # must still prove terminal-before-close and repeat-safe release.
        function, owners, aliases = _inline_ledger(
            """
            def f(self, fd):
                self._closed = True
                self._ops.close(fd)
            """
        )
        self.assertNotEqual(
            _close_violations_with_owners(
                function, owners, aliases, shared_owner_release=True
            ),
            [],
        )

    def test_injected_backend_close_without_terminal_flag_is_rejected(self) -> None:
        function, owners, aliases = _inline_ledger(
            """
            def f(self, fd):
                self._ops.close(fd)
            """
        )
        self.assertNotEqual(
            _close_violations_with_owners(
                function, owners, aliases, shared_owner_release=True
            ),
            [],
        )

    def test_reversed_owner_sequence_release_is_accepted(self) -> None:
        function, owners, aliases = _inline_ledger(
            """
            from collections.abc import Sequence
            from docker.filesystem.streams import OwnedStream

            def f(owners: Sequence[OwnedStream], failures):
                for owner in reversed(owners):
                    failures.run(owner.close, ordinary=(OSError,))
            """
        )
        self.assertEqual(_close_violations_with_owners(function, owners, aliases), [])

    def test_tuple_of_owners_release_is_accepted(self) -> None:
        function, owners, aliases = _inline_ledger(
            """
            from docker.filesystem.streams import OwnedStream

            def f(first: OwnedStream, second: OwnedStream, failures):
                for owner in (first, second):
                    failures.run(owner.close, ordinary=(OSError,))
            """
        )
        self.assertEqual(_close_violations_with_owners(function, owners, aliases), [])

    def test_annotated_local_owner_release_is_accepted(self) -> None:
        function, owners, aliases = _inline_ledger(
            """
            from docker.filesystem.streams import OwnedStream

            def f(factory):
                owner: OwnedStream | None = None
                owner = factory()
                owner.close()
            """
        )
        self.assertEqual(_close_violations_with_owners(function, owners, aliases), [])


class SubprocessStreamOwnershipTests(unittest.TestCase):
    """Task 6B.14 — subprocess stream origins cannot evade the ledger."""

    def test_unowned_subprocess_pipe_close_is_rejected(self) -> None:
        function, owners, aliases = _inline_ledger(
            """
            def f(proc):
                proc.stdout.close()
            """
        )
        self.assertNotEqual(
            _close_violations_with_owners(function, owners, aliases), []
        )

    def test_removed_adoption_factory_name_grants_no_release_authority(self) -> None:
        function, owners, aliases = _inline_ledger(
            """
            from docker.npm_environment.execution import _adopt_stream_owners
            def f(proc):
                owner = _adopt_stream_owners(proc, grace_seconds=1)
                owner.close()
            """
        )
        self.assertNotEqual(
            _close_violations_with_owners(function, owners, aliases), []
        )

    def test_borrowed_stream_close_after_adoption_is_rejected(self) -> None:
        function, owners, aliases = _inline_ledger(
            """
            from docker.filesystem.streams import OwnedStream

            def f(stream):
                owner = OwnedStream(stream, label="stdout")
                stream.close()
            """
        )
        self.assertNotEqual(
            _close_violations_with_owners(function, owners, aliases), []
        )

    def test_per_cleanup_owner_recreation_is_rejected(self) -> None:
        function, owners, aliases = _inline_ledger(
            """
            from docker.filesystem.streams import OwnedStream

            def f(stream):
                OwnedStream(stream, label="stdout").close()
            """
        )
        self.assertNotEqual(
            _close_violations_with_owners(function, owners, aliases), []
        )

    def test_consumer_class_disguised_as_owned_stream_is_rejected(self) -> None:
        function, owners, aliases = _inline_ledger(
            """
            def f(self, stream):
                class OwnedStream:
                    def close(self):
                        stream.close()
                return OwnedStream
            """
        )
        self.assertNotEqual(
            _close_violations_with_owners(function, owners, aliases), []
        )

    def test_annotated_owned_stream_release_is_accepted(self) -> None:
        function, owners, aliases = _inline_ledger(
            """
            from docker.filesystem.streams import OwnedStream

            def f(owner: OwnedStream):
                owner.close()
            """
        )
        self.assertEqual(_close_violations_with_owners(function, owners, aliases), [])

    def test_loop_over_owned_stream_sequence_is_accepted(self) -> None:
        function, owners, aliases = _inline_ledger(
            """
            from collections.abc import Sequence
            from docker.filesystem.streams import OwnedStream

            def f(owners: Sequence[OwnedStream]):
                for owner in owners:
                    owner.close()
            """
        )
        self.assertEqual(_close_violations_with_owners(function, owners, aliases), [])

    def test_conditional_owner_binding_is_accepted(self) -> None:
        function, owners, aliases = _inline_ledger(
            """
            from docker.filesystem.streams import OwnedStream

            def f(failed, first: OwnedStream, second: OwnedStream):
                selected = first if failed == "stdout" else second
                selected.close()
            """
        )
        self.assertEqual(_close_violations_with_owners(function, owners, aliases), [])

    def test_foundation_owned_stream_close_is_verified_internal_release(self) -> None:
        tree = _parse(_REPO / "docker" / "filesystem" / "streams.py")
        function = _find_method(tree, "OwnedStream", "close")
        aliases = _module_aliases(tree)
        self.assertEqual(
            _close_violations_with_owners(
                function, set(), aliases, shared_owner_release=True
            ),
            [],
        )

    def test_duplicate_integer_fd_adoption_is_rejected(self) -> None:
        function, owners, aliases = _inline_ledger(
            """
            from docker.filesystem.descriptors import OwnedDescriptor
            from docker.filesystem.operations import PosixDescriptorOps

            def f(fd):
                first = OwnedDescriptor(PosixDescriptorOps(), fd, label="a")
                second = OwnedDescriptor(PosixDescriptorOps(), fd, label="b")
                first.close()
                second.close()
            """
        )
        self.assertNotEqual(_duplicate_fd_adoption_violations(function, aliases), [])

    def test_accumulator_bound_owner_close_is_accepted(self) -> None:
        function, owners, aliases = _inline_ledger(
            """
            from collections.abc import Sequence
            from docker.filesystem.streams import OwnedStream

            def f(owners: Sequence[OwnedStream], failures):
                for owner in owners:
                    failures.run(owner.close, ordinary=(OSError,))
            """
        )
        self.assertEqual(_close_violations_with_owners(function, owners, aliases), [])

    def test_accumulator_bound_consumer_close_is_rejected(self) -> None:
        function, owners, aliases = _inline_ledger(
            """
            def f(proc, failures):
                failures.run(proc.stdout.close, ordinary=(OSError,))
            """
        )
        self.assertNotEqual(
            _close_violations_with_owners(function, owners, aliases), []
        )


class Phase7IntegratedEvasionTests(unittest.TestCase):
    """Release and new-module fixtures share the process convergence proof."""

    def test_unowned_success_finally_multiclose_and_handoff_are_rejected(self):
        bodies = (
            "os.close(fd)",
            "try:\n    work()\nfinally:\n    os.close(fd)",
            "for fd in descriptors:\n    backend.close(fd)",
            "child = backend.open(parent, name)\nbackend.close(parent)\nreturn child",
            "self._closed = True\nself._ops.close(fd)",
        )
        for body in bodies:
            with self.subTest(body=body):
                function, owners, aliases = _inline_ledger(
                    "import os\ndef f(self, backend, fd, descriptors, parent, name):\n"
                    + textwrap.indent(body, "    ")
                )
                self.assertTrue(
                    _close_violations_with_owners(
                        function,
                        owners,
                        aliases,
                        shared_owner_release=True,
                    )
                )

    def test_borrowed_file_wrapper_close_is_not_descriptor_release(self):
        function, owners, aliases = _inline_ledger(
            "import os\n"
            "from docker.filesystem.descriptors import OwnedDescriptor\n"
            "def f(owner: OwnedDescriptor):\n"
            '    stream = os.fdopen(owner.fd, "w", closefd=False)\n'
            "    stream.close()\n"
            "    owner.close()\n"
        )
        self.assertEqual(_fdopen_violations(function, owners, aliases), [])
        receivers = owners | _file_object_names(function, owners, aliases)
        self.assertEqual(
            _close_violations_with_owners(function, receivers, aliases), []
        )

    def test_new_modules_and_reverse_imports_do_not_gain_authority(self):
        from tests.enforcement.process_convergence import convergence_report
        from tests.enforcement.test_filesystem_convergence import _sources

        sources = _sources()
        additions = (
            (
                "docker/phase7_new_consumer.py",
                "import subprocess as sp\nfrom subprocess import PIPE as P\n"
                "def f(argv):\n    spawn = sp.Popen\n"
                "    child = spawn(argv, stdout=P, stderr=P)\n"
                "    release = child.stderr.close\n    release()\n",
            ),
            ("docker/phase7_new_consumer.py", "from docker import filesystem\n"),
            ("docker/filesystem/phase7_reverse.py", "import docker.process\n"),
        )
        for path, source in additions:
            with self.subTest(path=path, source=source):
                self.assertTrue(convergence_report({**sources, path: source})["errors"])

    def test_prepared_policy_drain_budget_helper_and_deferred_evasions(self):
        from tests.enforcement.process_convergence import convergence_report
        from tests.enforcement.test_filesystem_convergence import _sources

        sources = _sources()
        mutations = (
            (
                "docker/process.py",
                "self._acquired[index] = True",
                "self._acquired[index] = False",
            ),
            (
                "docker/process.py",
                "return not self._started[index] or self._stopped[index].is_set()",
                "return True",
            ),
            ("docker/process.py", "if math.fsum(caps) > total:", "if False:"),
            ("docker/process.py", "while not self._finished.is_set():", "while False:"),
            (
                "docker/process.py",
                "result.done and result.cleanup_complete",
                "result.done",
            ),
            (
                "docker/npm_environment/execution.py",
                "min(self.grace, remaining / 3)",
                "self.grace",
            ),
            (
                "docker/npm_environment/execution.py",
                "self.owner = operation.owner",
                "self.owner = None",
            ),
        )
        for path, old, new in mutations:
            with self.subTest(path=path, mutation=old):
                self.assertIn(old, sources[path])
                changed = {**sources, path: sources[path].replace(old, new)}
                self.assertTrue(convergence_report(changed)["errors"])


if __name__ == "__main__":
    unittest.main()
