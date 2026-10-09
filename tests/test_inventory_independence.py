"""Executable narrow dependency audit for the migrated behavioural suite.

It rejects new reads/copies of the *repository* ``docker-constructor.toml``
from migrated behavioural sources, while accepting committed fixture reads
and a small set of explicitly named live-contract scopes.  The analysis
parses the source with :mod:`ast`, follows multiline path expressions and
straightforward aliases, and permits a module-level live path only when it
is a pure path construction (never an import-time load or read).
"""
from __future__ import annotations

import ast
import hashlib
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_INVENTORY_NAME = "docker-constructor.toml"

#: Behavioural sources migrated onto stable test-owned fixtures.
MIGRATED_BEHAVIOURAL_SOURCES = (
    "tests/build_test_support.py",
    "tests/inventory_fixtures.py",
    "tests/test_inventory_fixtures.py",
    "tests/test_constructor_launcher.py",
    "tests/test_constructor_inventory_runtime.py",
    "tests/test_constructor_build_materialization.py",
    "tests/test_constructor_pi_inventory.py",
    "tests/test_constructor_check_updates_acceptance.py",
    "tests/test_constructor_facade.py",
    "tests/test_version_effective.py",
    "tests/test_version_visual_boundaries.py",
    "tests/test_live_inventory_contract.py",
)

#: Named live-contract test scopes allowed to touch the repository
#: inventory.  A scope is a class or fully-qualified function name; a
#: behavioral test in the same file is *not* covered by these scopes.
LIVE_CONTRACT_SCOPES: dict[str, frozenset[str]] = {
    "tests/test_constructor_inventory_runtime.py": frozenset({
        "TestLiveInventoryRuntimeContract",
    }),
    "tests/test_constructor_pi_inventory.py": frozenset({
        "TestLiveNodeMetadataContract",
        "TestReviewedPiReleaseContract",
    }),
    "tests/test_version_visual_boundaries.py": frozenset({
        "TestLiveInventoryVisualHeaders",
    }),
    "tests/test_live_inventory_contract.py": frozenset({
        "TestLiveInventoryContract",
    }),
}

#: Module-level symbols allowed to bind the repository inventory path for a
#: live-contract scope.  Binding is allowed, but any *use* outside the named
#: scope is still a violation.
LIVE_CONTRACT_SYMBOLS: dict[str, frozenset[str]] = {
    "tests/test_constructor_inventory_runtime.py": frozenset({"_LIVE_INVENTORY"}),
    "tests/test_version_visual_boundaries.py": frozenset({"CANONICAL"}),
    "tests/test_live_inventory_contract.py": frozenset({"REAL_INVENTORY"}),
}

#: Well-known names that resolve to the repository root (or are file-relative
#: aliases of it).
_REPOSITORY_ROOT_NAMES = frozenset({
    "ROOT",
    "_REPO",
    "_REPO_ROOT",
    "REPO_ROOT",
    "_TEST_PROJECT_ROOT",
})


def _contains_name(node: ast.AST, names: frozenset[str]) -> bool:
    return any(
        isinstance(sub, ast.Name) and sub.id in names
        for sub in ast.walk(node)
    )


def _contains_inventory_literal(node: ast.AST) -> bool:
    return any(
        isinstance(sub, ast.Constant)
        and sub.value == REPOSITORY_INVENTORY_NAME
        for sub in ast.walk(node)
    )


def _iter_assignments(node: ast.AST):
    if isinstance(node, ast.Assign):
        for target in node.targets:
            yield target, node.value
    elif isinstance(node, ast.AnnAssign):
        yield node.target, node.value


def _collect_root_aliases(module: ast.Module) -> frozenset[str]:
    """Collect straightforward aliases of repository-root expressions.

    ``ROOT = Path(__file__)...`` makes ``ROOT`` a root alias, and so does
    ``root = ROOT`` or ``_REPO = _THIS_DIR.parent`` once ``_THIS_DIR`` is
    itself a file-relative alias.  This catches ``root / "...toml"``
    bypasses without any line-based text matching.
    """
    aliases: set[str] = set()
    changed = True
    while changed:
        changed = False
        root_names = _REPOSITORY_ROOT_NAMES | aliases | {"__file__"}
        for node in ast.walk(module):
            for target, value in _iter_assignments(node):
                if value is None or not isinstance(target, ast.Name):
                    continue
                if _contains_inventory_literal(value):
                    continue
                if _contains_name(value, frozenset(root_names)):
                    if target.id not in aliases:
                        aliases.add(target.id)
                        changed = True
    return frozenset(aliases)


def _is_repository_inventory_expr(node: ast.AST, root_names: frozenset[str]) -> bool:
    return (
        node is not None
        and _contains_inventory_literal(node)
        and _contains_name(node, root_names)
    )


#: Names that construct a filesystem path (never perform inventory IO).
_PATH_CONSTRUCTOR_NAMES = frozenset({
    "Path",
    "PurePath",
    "PosixPath",
    "WindowsPath",
    "str",
})

#: Path-object attributes / methods that only navigate (never read bytes).
_PATH_NAVIGATION_ATTRS = frozenset({
    "anchor",
    "absolute",
    "drive",
    "expanduser",
    "joinpath",
    "name",
    "parent",
    "parents",
    "parts",
    "relative_to",
    "resolve",
    "root",
    "stem",
    "suffix",
    "suffixes",
    "with_name",
    "with_stem",
    "with_suffix",
})

#: Pure ``os.path`` helpers that only manipulate path strings.
_OS_PATH_FUNCS = frozenset({
    "abspath",
    "basename",
    "dirname",
    "expanduser",
    "expandvars",
    "join",
    "normpath",
    "realpath",
    "relpath",
    "split",
    "splitext",
})


def _is_os_path_receiver(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "path"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _is_path_construction(node: ast.AST) -> bool:
    """Return ``True`` when *node* only builds a path (never reads it).

    A module-level live binding may only be the path itself; wrapping the
    repository-inventory path in ``load_inventory(...)`` or ``.read_text()``
    / ``.read_bytes()`` is import-time IO and is rejected.
    """
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, ast.Name):
        return True
    if isinstance(node, ast.BinOp):
        return isinstance(node.op, ast.Div) and (
            _is_path_construction(node.left)
            and _is_path_construction(node.right)
        )
    if isinstance(node, ast.Attribute):
        return (
            node.attr in _PATH_NAVIGATION_ATTRS
            and _is_path_construction(node.value)
        )
    if isinstance(node, ast.Subscript):
        return _is_path_construction(node.value) and isinstance(
            node.slice, ast.Constant,
        )
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name):
            allowed = func.id in _PATH_CONSTRUCTOR_NAMES
        elif isinstance(func, ast.Attribute):
            if _is_os_path_receiver(func.value):
                allowed = func.attr in _OS_PATH_FUNCS
            else:
                allowed = (
                    func.attr in _PATH_NAVIGATION_ATTRS
                    and _is_path_construction(func.value)
                )
        else:
            allowed = False
        if not allowed:
            return False
        return all(_is_path_construction(arg) for arg in node.args) and all(
            keyword.value is None or _is_path_construction(keyword.value)
            for keyword in node.keywords
        )
    return False


class _RepositoryInventoryVisitor(ast.NodeVisitor):
    def __init__(
        self,
        source_name: str,
        root_names: frozenset[str],
        live_handles: frozenset[str],
        allowed_node_ids: frozenset[int],
    ) -> None:
        self.source_name = source_name
        self.root_names = root_names
        self.live_handles = live_handles
        self.allowed_node_ids = allowed_node_ids
        self.violations: list[tuple[int, str]] = []
        self._scope: list[str] = []
        self._inside_inventory = False

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    def _visit_function(self, node) -> None:
        parent = self._scope[-1] if self._scope else None
        self._scope.append(f"{parent}.{node.name}" if parent else node.name)
        self.generic_visit(node)
        self._scope.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def _scope_authorized(self) -> bool:
        scope = self._scope[-1] if self._scope else None
        if scope is None:
            return False
        allowed = LIVE_CONTRACT_SCOPES.get(self.source_name, frozenset())
        return any(
            scope == entry or scope.startswith(entry + ".") for entry in allowed
        )

    def visit(self, node: ast.AST):
        previous = self._inside_inventory
        is_inventory = (
            isinstance(node, ast.expr)
            and _is_repository_inventory_expr(node, self.root_names)
        )
        if (
            is_inventory
            and not previous
            and id(node) not in self.allowed_node_ids
            and not self._scope_authorized()
        ):
            self.violations.append(
                (node.lineno, f"repository inventory path: {ast.unparse(node)}"),
            )
        self._inside_inventory = previous or is_inventory
        result = super().visit(node)
        self._inside_inventory = previous
        return result

    def visit_Name(self, node: ast.Name) -> None:
        if (
            isinstance(node.ctx, ast.Load)
            and node.id in self.live_handles
            and not self._scope_authorized()
        ):
            self.violations.append(
                (node.lineno, f"uses live-inventory handle {node.id!r} outside its live scope"),
            )
        self.generic_visit(node)


def find_repository_inventory_coupling(
    source: str,
    source_name: str = "<string>",
) -> list[tuple[int, str]]:
    """Return ``(line_number, description)`` for repository-inventory access.

    The analysis parses the source and follows multiline path expressions and
    straightforward root/alias assignments.  Access is permitted only inside
    the named live-contract scopes for *source_name*; a behavioral test
    referencing an allowed module-level handle still fails.
    """
    try:
        module = ast.parse(source)
    except SyntaxError:
        return []

    root_names = _REPOSITORY_ROOT_NAMES | _collect_root_aliases(module) | {"__file__"}
    authorized_symbols = LIVE_CONTRACT_SYMBOLS.get(source_name, frozenset())
    live_handles: set[str] = set()
    allowed_node_ids: set[int] = set()
    for statement in module.body:
        for target, value in _iter_assignments(statement):
            if value is None or not _is_repository_inventory_expr(value, root_names):
                continue
            if (
                isinstance(target, ast.Name)
                and target.id in authorized_symbols
                and _is_path_construction(value)
            ):
                live_handles.add(target.id)
                allowed_node_ids.add(id(value))

    visitor = _RepositoryInventoryVisitor(
        source_name, root_names, frozenset(live_handles), frozenset(allowed_node_ids),
    )
    visitor.visit(module)
    deduped: dict[int, str] = {}
    for line_number, message in visitor.violations:
        deduped.setdefault(line_number, message)
    return sorted(deduped.items())


class TestMigratedSourcesHaveNoRepositoryCoupling(unittest.TestCase):
    def test_migrated_behavioural_sources_are_independent(self):
        for source_name in MIGRATED_BEHAVIOURAL_SOURCES:
            path = REPO_ROOT / source_name
            self.assertTrue(path.is_file(), source_name)
            violations = find_repository_inventory_coupling(
                path.read_text(encoding="utf-8"), source_name,
            )
            self.assertEqual(
                [], violations,
                f"{source_name} reads/copies the repository inventory: {violations}",
            )


class TestAuditDetectsRepresentativeCoupling(unittest.TestCase):
    def test_detects_copy_of_repository_inventory(self):
        # The original build_test_support.py coupling.
        source = (
            "INVENTORY_PATH = Path(_TEMPORARY_DIRECTORY.name) / "
            '"docker-constructor.toml"\n'
            "shutil.copyfile(\n"
            '    Path(__file__).resolve().parents[1] / "docker-constructor.toml",\n'
            "    INVENTORY_PATH,\n"
            ")\n"
        )
        violations = find_repository_inventory_coupling(
            source, "tests/build_test_support.py",
        )
        self.assertEqual(1, len(violations))

    def test_detects_read_of_repository_inventory(self):
        source = 'inv = load_inventory(ROOT / "docker-constructor.toml")\n'
        violations = find_repository_inventory_coupling(
            source, "tests/test_version_effective.py",
        )
        self.assertEqual(1, len(violations))
        self.assertEqual(1, violations[0][0])
        self.assertIn("repository inventory path", violations[0][1])

    def test_detects_launcher_copy_pattern(self):
        source = (
            "real = os.path.abspath(os.path.join(\n"
            '    os.path.dirname(__file__), "..", "docker-constructor.toml",\n'
            "))\n"
            "shutil.copy2(real, fixture)\n"
        )
        violations = find_repository_inventory_coupling(
            source, "tests/test_constructor_launcher.py",
        )
        self.assertEqual(1, len(violations))

    def test_detects_multiline_repository_path(self):
        source = (
            "def helper():\n"
            "    path = ROOT / (\n"
            '        "docker-constructor.toml"\n'
            "    )\n"
            "    return load_inventory(path)\n"
        )
        violations = find_repository_inventory_coupling(
            source, "tests/test_version_effective.py",
        )
        self.assertEqual(1, len(violations))
        self.assertIn("repository inventory path", violations[0][1])

    def test_detects_path_alias(self):
        source = (
            "def helper():\n"
            '    path = ROOT / "docker-constructor.toml"\n'
            "    return load_inventory(path)\n"
        )
        violations = find_repository_inventory_coupling(
            source, "tests/test_version_effective.py",
        )
        self.assertEqual(1, len(violations))

    def test_detects_root_alias(self):
        source = (
            "def helper():\n"
            "    root = ROOT\n"
            '    path = root / "docker-constructor.toml"\n'
            "    return load_inventory(path)\n"
        )
        violations = find_repository_inventory_coupling(
            source, "tests/test_version_effective.py",
        )
        self.assertEqual(1, len(violations))
        self.assertEqual(3, violations[0][0])

    def test_detects_live_handle_misuse_from_behavioral_test(self):
        source = (
            'REAL_INVENTORY = REPO_ROOT / "docker-constructor.toml"\n'
            "class TestBehavioral(unittest.TestCase):\n"
            "    def test_reads(self):\n"
            "        load_inventory(REAL_INVENTORY)\n"
        )
        violations = find_repository_inventory_coupling(
            source, "tests/test_live_inventory_contract.py",
        )
        self.assertEqual(1, len(violations))
        self.assertEqual(4, violations[0][0])
        self.assertIn("live-inventory handle", violations[0][1])

    def test_detects_inline_access_from_behavioral_test(self):
        source = (
            "class TestStableNodeVersions(unittest.TestCase):\n"
            "    def test_reads(self):\n"
            '        load_inventory(_REPO / "docker-constructor.toml")\n'
        )
        violations = find_repository_inventory_coupling(
            source, "tests/test_constructor_pi_inventory.py",
        )
        self.assertEqual(1, len(violations))

    def test_rejects_import_time_loader_call(self):
        # An authorized symbol may bind the path, not read it at import.
        source = (
            "REAL_INVENTORY = load_inventory(\n"
            '    REPO_ROOT / "docker-constructor.toml"\n'
            ")\n"
        )
        violations = find_repository_inventory_coupling(
            source, "tests/test_live_inventory_contract.py",
        )
        self.assertEqual(1, len(violations))
        self.assertIn("repository inventory path", violations[0][1])

    def test_rejects_import_time_read_text(self):
        source = (
            "REAL_INVENTORY = (\n"
            '    REPO_ROOT / "docker-constructor.toml"\n'
            ").read_text()\n"
        )
        violations = find_repository_inventory_coupling(
            source, "tests/test_live_inventory_contract.py",
        )
        self.assertEqual(1, len(violations))

    def test_rejects_import_time_read_bytes(self):
        source = (
            "REAL_INVENTORY = (\n"
            '    REPO_ROOT / "docker-constructor.toml"\n'
            ").read_bytes()\n"
        )
        violations = find_repository_inventory_coupling(
            source, "tests/test_live_inventory_contract.py",
        )
        self.assertEqual(1, len(violations))


class TestAuditAcceptsLegitimateSources(unittest.TestCase):
    def test_accepts_committed_fixture_reads(self):
        source = (
            "from tests.inventory_fixtures import stable_inventory_path\n"
            "inv = load_inventory(stable_inventory_path())\n"
        )
        self.assertEqual(
            [], find_repository_inventory_coupling(source, "tests/anything.py"),
        )

    def test_accepts_temporary_output_named_like_inventory(self):
        # A temp output file that merely shares the filename is not a
        # repository read.
        source = (
            'fixture = os.path.join(self._tmpdir.name, "docker-constructor.toml")\n'
        )
        self.assertEqual(
            [], find_repository_inventory_coupling(source, "tests/anything.py"),
        )

    def test_accepts_named_live_contract_symbol(self):
        # A pure module-level path binding is the only permitted exception.
        source = 'REAL_INVENTORY = REPO_ROOT / "docker-constructor.toml"\n'
        self.assertEqual(
            [],
            find_repository_inventory_coupling(
                source, "tests/test_live_inventory_contract.py",
            ),
        )

    def test_accepts_pure_path_binding_with_navigation(self):
        source = (
            "REAL_INVENTORY = (\n"
            "    Path(__file__).resolve().parents[1]\n"
            ').joinpath("docker-constructor.toml")\n'
        )
        self.assertEqual(
            [],
            find_repository_inventory_coupling(
                source, "tests/test_live_inventory_contract.py",
            ),
        )

    def test_live_exception_is_not_a_blanket_file_exemption(self):
        # The same file may not read the inventory another way.
        source = 'other = load_inventory(ROOT / "docker-constructor.toml")\n'
        violations = find_repository_inventory_coupling(
            source, "tests/test_live_inventory_contract.py",
        )
        self.assertEqual(1, len(violations))
        self.assertIn("repository inventory path", violations[0][1])

    def test_accepts_live_handle_used_inside_its_named_scope(self):
        source = (
            'REAL_INVENTORY = REPO_ROOT / "docker-constructor.toml"\n'
            "class TestLiveInventoryContract(unittest.TestCase):\n"
            "    def test_reads(self):\n"
            "        load_inventory(REAL_INVENTORY)\n"
        )
        self.assertEqual(
            [],
            find_repository_inventory_coupling(
                source, "tests/test_live_inventory_contract.py",
            ),
        )

    def test_accepts_inline_access_inside_named_live_scope(self):
        source = (
            "class TestLiveNodeMetadataContract(unittest.TestCase):\n"
            "    def test_reads(self):\n"
            '        load_inventory(_REPO / "docker-constructor.toml")\n'
        )
        self.assertEqual(
            [],
            find_repository_inventory_coupling(
                source, "tests/test_constructor_pi_inventory.py",
            ),
        )


#: Representative fully-migrated behavioural suites executed with the
#: repository inventory denied.
REPRESENTATIVE_BEHAVIOURAL_TESTS = (
    "tests.test_inventory_fixtures",
    "tests.test_constructor_launcher.TestRunTransaction",
    "tests.test_constructor_inventory_runtime.TestClosedRuntimeSchema",
    "tests.test_constructor_build_materialization.TestBuildArtifactSelection",
    "tests.test_version_effective",
    "tests.test_constructor_check_updates_acceptance.TestCheckUpdatesAcceptance",
    "tests.test_constructor_facade.TestInteractiveProgress",
    "tests.test_version_visual_boundaries.TestStableFixtureVisualHeaders",
    "tests.test_constructor_pi_inventory.TestSmokeToolConsistency",
)

_DENIAL_HOOK = '''\
import os, sys
_REPO = os.path.realpath({repo!r})
_INV = os.path.join(_REPO, "docker-constructor.toml")

def _deny(event, args):
    if event != "open":
        return
    path = args[0]
    if not isinstance(path, (str, bytes, os.PathLike)):
        return
    try:
        resolved = os.path.realpath(os.fspath(path))
    except (TypeError, ValueError):
        return
    if resolved == _INV:
        raise PermissionError("repository inventory read denied: " + resolved)

sys.addaudithook(_deny)
sys.path.insert(0, _REPO)
'''

_SUITE_RUNNER = textwrap.dedent('''\
import unittest
loader = unittest.TestLoader()
suite = unittest.TestSuite()
for name in {names}:
    suite.addTests(loader.loadTestsFromName(name))
if suite.countTestCases() == 0:
    raise SystemExit(2)
result = unittest.TextTestRunner(verbosity=1).run(suite)
raise SystemExit(0 if result.wasSuccessful() else 1)
''')

_ORIGINAL_COPY_COUPLING = textwrap.dedent('''\
import shutil, tempfile
td = tempfile.mkdtemp()
shutil.copyfile(
    os.path.join(_REPO, "docker-constructor.toml"),
    os.path.join(td, "copy.toml"),
)
print("COPY_SUCCEEDED")
''')


def _inventory_digest() -> str:
    return hashlib.sha256(
        (REPO_ROOT / REPOSITORY_INVENTORY_NAME).read_bytes()
    ).hexdigest()


class TestSubprocessDeniesRepositoryInventoryReads(unittest.TestCase):
    """Deny reads of the resolved repository inventory in a subprocess,
    then load helpers and run representative migrated suites."""

    def _run_with_denial(self, body: str) -> subprocess.CompletedProcess[str]:
        script = _DENIAL_HOOK.format(repo=str(REPO_ROOT)) + body
        return subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )

    def test_representative_migrated_suites_run_without_repository_reads(self):
        before = _inventory_digest()
        completed = self._run_with_denial(
            _SUITE_RUNNER.format(
                names=repr(REPRESENTATIVE_BEHAVIOURAL_TESTS),
            )
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertNotIn("read denied", completed.stderr)
        self.assertEqual(before, _inventory_digest())

    def test_denial_detects_original_copy_coupling(self):
        completed = self._run_with_denial(_ORIGINAL_COPY_COUPLING)
        self.assertNotEqual(0, completed.returncode)
        self.assertNotIn("COPY_SUCCEEDED", completed.stdout)
        self.assertIn("read denied", completed.stderr)

    def test_denial_detects_direct_read_coupling(self):
        body = textwrap.dedent('''\
        from pathlib import Path
        Path(_REPO, "docker-constructor.toml").read_text()
        print("READ_SUCCEEDED")
        ''')
        completed = self._run_with_denial(body)
        self.assertNotEqual(0, completed.returncode)
        self.assertNotIn("READ_SUCCEEDED", completed.stdout)
        self.assertIn("read denied", completed.stderr)

    def test_denial_hook_never_edits_repository_inventory(self):
        before = _inventory_digest()
        self._run_with_denial(_ORIGINAL_COPY_COUPLING)
        self.assertEqual(before, _inventory_digest())


if __name__ == "__main__":
    unittest.main()