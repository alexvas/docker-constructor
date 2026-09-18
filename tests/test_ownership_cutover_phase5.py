"""Phase 5 ownership-cutover architecture and contract tests.

Phases 1-4 established the shared TOML document boundary
(``configuration_document_validation``) and the local aggregate owner
(``local_project_configuration``), then migrated every command and domain
consumer. This module locks that ownership in with AST-level architecture
tests and a contract inventory for the clauses removed from
``runtime-host-access``:

* no production module parses TOML outside the explicit non-project
  allowlist, and no project-document consumer parses TOML or projects a
  project-document parse failure outside the shared boundary;
* no module constructs the local aggregate outside its owner, including
  qualified (``model.LocalConfig``) and aliased (``import ... as Config``)
  forms, and the reviewed-inventory module no longer exposes legacy
  companion loaders;
* domain modules do not depend on the aggregate owner, the runtime
  host-access owner owns no cache/network state, and cache-root policy
  stays in ``cache_storage``;
* every scenario and every atomic normative obligation moved out of
  ``runtime-host-access`` is reconciled against the authoritative
  pre-cutover requirement (main spec, or the archived change that
  introduced it) and maps to a destination capability, a destination
  requirement/scenario, and one or more passing regression tests that
  directly exercise the obligation;
* the set of atomic obligations is not defined by this test module at all:
  it is loaded from the independent semantic-source manifest
  ``tests/data/moved_local_state_obligations.json``, whose excerpts plus a
  documented set of non-normative connectors must tile the authoritative
  requirement body exactly.  An obligation omitted from the manifest is
  therefore reported as uncovered normative source text.

All of these are guards, not drivers: if Phases 1-4 left a violation, the
matching test fails and ``5.5``/``5.6`` correct it.
"""
from __future__ import annotations

import ast
import importlib
import io
import json
import re
import textwrap
import unittest
from dataclasses import dataclass, replace
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DOCKER = _REPO_ROOT / "docker"
_CHANGE_NAME = "extract-local-project-configuration"
_CHANGE_DIR = _REPO_ROOT / "openspec" / "changes" / _CHANGE_NAME
_BOUNDARY = _DOCKER / "versioning" / "configuration_document_validation.py"
_AGGREGATE = _DOCKER / "versioning" / "local_project_configuration.py"
_INVENTORY = _DOCKER / "versioning" / "inventory.py"
_CACHE_STORAGE = _DOCKER / "versioning" / "cache_storage.py"

_TOML_ROOTS = ("tomllib", "tomli")

# Production modules that legitimately parse NON-project TOML.  The first
# entry is the shared project-document boundary and the only module permitted
# to parse ``docker-constructor.toml`` or ``docker-constructor.local.toml``;
# every other entry parses a generated projection or an unrelated manifest
# (Cargo).  Any module outside this allowlist that imports or calls into
# ``tomllib``/``tomli`` is an independent project-document parser.
_TOML_PARSING_ALLOWLIST = (
    "docker/versioning/configuration_document_validation.py",
    "docker/versioning/effective.py",
    "docker/versioning/verification.py",
    "docker/versioning/runtime_verification.py",
    "docker/runtime_installer.py",
    "docker/versioning/providers/rust.py",
)
_PROJECT_DOCUMENT_PARSER = "docker/versioning/configuration_document_validation.py"

# Names that identify a module as locating or consuming one of the two fixed
# project documents.  Such a module must delegate parsing to the boundary; if
# it parses TOML itself it is an independent parser regardless of its filename.
_PROJECT_DOCUMENT_CONSUMER_NAMES = (
    "LOCAL_COMPANION_BASENAME",
    "resolve_local_companion_path",
    "load_local_project_configuration",
    "load_optional_local_project_configuration",
    "parse_configuration_document",
    "validate_configuration_documents",
    "validate_local_document",
)

# ``TOMLDecodeError`` may be inspected only by the shared boundary and by the
# generated-projection round-trip in ``effective.py`` (not a project document).
_TOML_DECODE_ERROR_OWNERS = (
    "docker/versioning/configuration_document_validation.py",
    "docker/versioning/effective.py",
)

# Domain owners must not depend on the aggregate owner or the document
# boundary; the aggregate composes domain parsers, never the reverse.
_DOMAIN_MODULES = (
    "docker/versioning/host_access.py",
    "docker/versioning/cache_storage.py",
    "docker/versioning/corporate_network.py",
)
_AGGREGATE_OWNER_MODULES = ("local_project_configuration", "configuration_document_validation")

# Cache-storage primitives that must not act on the shared cache root outside
# ``cache_storage.py``.  ``artifact_cache.py`` is deliberately excluded: its
# ``cache_root`` is the project-scoped artifact containment root, an
# independently owned policy, not the shared constructor cache root.
_CACHE_POLICY_PRIMITIVES = frozenset({
    "normpath", "isabs", "islink", "is_symlink", "realpath", "lstat",
    "chmod", "mkdir", "makedirs",
})
_CACHE_POLICY_SCOPE = (
    "docker/versioning/host_access.py",
    "docker/versioning/corporate_network.py",
    "docker/versioning/local_project_configuration.py",
    "docker/versioning/inventory.py",
    "docker/constructor_cli.py",
    "docker/launcher.py",
)
_CACHE_STORAGE_ALLOWED_IMPORTS = frozenset({
    "os", "stat", "pathlib", "errors", "model", "__future__",
})

# Aggregate-owner public names.  They may be *imported* by consumers but only
# defined by the aggregate module.
_AGGREGATE_OWNER_NAMES = (
    "resolve_local_companion_path",
    "validate_local_document",
    "load_local_project_configuration",
    "load_optional_local_project_configuration",
    "LOCAL_TABLE_NAMES",
    "_LOCAL_DOMAIN_TABLES",
)

# Legacy companion-loading adapters removed in 5.6.
_LEGACY_COMPANION_ADAPTERS = ("load_local_config", "load_local_config_for_inventory")


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------

def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _docker_sources() -> list[Path]:
    return sorted(_DOCKER.rglob("*.py"))


def _production_sources() -> dict[str, str]:
    """Map repo-relative production module path to its source text."""
    return {_relative(path): _source(path) for path in _docker_sources()}


def _relative(path: Path) -> str:
    return str(path.relative_to(_REPO_ROOT))


def _root_module(module: str | None) -> str:
    return module.split(".")[0] if module else ""


def _imported_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(_root_module(alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                modules.add(_root_module(node.module))
    return modules


def _toml_usage(tree: ast.AST) -> bool:
    """True when *tree* imports or calls into ``tomllib``/``tomli``."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(_root_module(alias.name) in _TOML_ROOTS for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if _root_module(node.module) in _TOML_ROOTS:
                return True
        elif isinstance(node, ast.Attribute):
            base: ast.AST = node
            while isinstance(base, ast.Attribute):
                base = base.value
            if isinstance(base, ast.Name) and base.id in _TOML_ROOTS:
                return True
    return False


def _references_name(tree: ast.AST, name: str) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == name:
            return True
        if isinstance(node, ast.Attribute) and node.attr == name:
            return True
    return False


def _toml_parsing_offenders(
    sources: dict[str, str], allowlist: frozenset[str]
) -> list[str]:
    """Return production modules that parse TOML but are not allowlisted."""
    offenders: list[str] = []
    for relative in sorted(sources):
        if relative in allowlist:
            continue
        if _toml_usage(ast.parse(sources[relative])):
            offenders.append(relative)
    return offenders


def _aggregate_constructions(tree: ast.AST) -> list[int]:
    """Line numbers of ``LocalConfig`` constructions, resolving import aliases.

    Catches the bare ``LocalConfig(...)`` form, the qualified
    ``model.LocalConfig(...)`` form, and locally aliased bindings such as
    ``from docker.versioning.model import LocalConfig as Config`` or
    ``Config = model.LocalConfig`` followed by ``Config(...)``.
    """
    aliases: set[str] = {"LocalConfig"}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.split(".")[-1] == "model"
        ):
            for alias in node.names:
                if alias.name == "LocalConfig":
                    aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.Assign):
            value = node.value
            is_reference = (
                isinstance(value, ast.Name) and value.id in aliases
            ) or (
                isinstance(value, ast.Attribute) and value.attr == "LocalConfig"
            )
            if is_reference:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        aliases.add(target.id)

    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in aliases:
            hits.append(node.lineno)
        elif isinstance(func, ast.Attribute) and func.attr == "LocalConfig":
            hits.append(node.lineno)
    return hits


def _defines(tree: ast.AST, name: str) -> bool:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == name:
                return True
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return True
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                return True
    return False


def _name_is_cache_like(node: ast.AST) -> bool:
    def names(value: ast.AST) -> set[str]:
        out: set[str] = set()
        for child in ast.walk(value):
            if isinstance(child, ast.Name):
                out.add(child.id)
            elif isinstance(child, ast.Attribute):
                out.add(child.attr)
            elif isinstance(child, ast.keyword) and child.arg:
                out.add(child.arg)
            elif isinstance(child, ast.Constant) and isinstance(child.value, str):
                out.add(child.value)
        return out

    return any("cache" in name.lower() for name in names(node))


def _call_name(func: ast.AST) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


# ---------------------------------------------------------------------------
# 5.1 Project TOML parsing / parse-error projection ownership
# ---------------------------------------------------------------------------

class TestProjectTomlParsingOwnedByBoundary(unittest.TestCase):
    maxDiff = None

    def test_only_allowlisted_production_modules_parse_toml(self) -> None:
        self.assertEqual(
            [],
            _toml_parsing_offenders(
                _production_sources(), frozenset(_TOML_PARSING_ALLOWLIST)
            ),
        )

    def test_project_document_consumers_never_parse_toml(self) -> None:
        offenders: list[str] = []
        for relative, source in sorted(_production_sources().items()):
            if relative == _PROJECT_DOCUMENT_PARSER:
                continue
            tree = ast.parse(source)
            if not _toml_usage(tree):
                continue
            if any(
                _references_name(tree, name)
                for name in _PROJECT_DOCUMENT_CONSUMER_NAMES
            ):
                offenders.append(relative)
        self.assertEqual([], offenders)

    def test_parse_error_projection_is_owned_by_the_boundary(self) -> None:
        allowed = set(_TOML_DECODE_ERROR_OWNERS)
        offenders: list[str] = []
        for path in _docker_sources():
            relative = _relative(path)
            if relative in allowed:
                continue
            if _references_name(ast.parse(_source(path)), "TOMLDecodeError"):
                offenders.append(relative)
        self.assertEqual([], offenders)

    def test_boundary_is_the_only_project_document_parser(self) -> None:
        from docker.versioning import configuration_document_validation as boundary

        self.assertTrue(callable(boundary.parse_configuration_document))
        self.assertTrue(callable(boundary.validate_configuration_documents))


class TestProjectTomlDetectorIsNotVacuous(unittest.TestCase):
    """Every parser form is flagged unless explicitly allowlisted."""

    _SYNTHETIC = textwrap.dedent(
        """
        import tomllib
        from pathlib import Path

        def load_local(path: Path):
            data = path.read_bytes()
            try:
                return tomllib.loads(data.decode("utf-8"))
            except tomllib.TOMLDecodeError as exc:
                raise RuntimeError(str(exc))
        """
    )

    _PARSER_FORMS = {
        "direct call": "import tomllib\ndef f(b):\n    return tomllib.loads(b)\n",
        "aliased import": (
            "import tomllib as parser\ndef f(b):\n    return parser.loads(b)\n"
        ),
        "from-import": "from tomllib import loads\ndef f(b):\n    return loads(b)\n",
        "tomli fallback": (
            "import tomli as tomllib\ndef f(b):\n    return tomllib.loads(b)\n"
        ),
        "module attribute chain": (
            "import tomllib\ndef f(b):\n    return tomllib.loads(b.decode())\n"
        ),
    }

    def test_toml_usage_detected(self) -> None:
        self.assertTrue(_toml_usage(ast.parse(self._SYNTHETIC)))

    def test_toml_decode_error_reference_detected(self) -> None:
        self.assertTrue(
            _references_name(ast.parse(self._SYNTHETIC), "TOMLDecodeError")
        )

    def test_every_parser_form_is_detected(self) -> None:
        for label, source in self._PARSER_FORMS.items():
            with self.subTest(form=label):
                self.assertTrue(_toml_usage(ast.parse(source)))

    def test_parsing_in_a_newly_added_module_is_flagged(self) -> None:
        sources = {
            "docker/versioning/brand_new.py": "import tomllib\ntomllib.loads('x')\n"
        }
        self.assertEqual(
            ["docker/versioning/brand_new.py"],
            _toml_parsing_offenders(sources, frozenset(_TOML_PARSING_ALLOWLIST)),
        )

    def test_explicitly_allowlisted_unrelated_parser_is_not_flagged(self) -> None:
        sources = {
            "docker/versioning/providers/rust.py": (
                "import tomllib\ntomllib.loads('x')\n"
            )
        }
        self.assertEqual(
            [],
            _toml_parsing_offenders(sources, frozenset(_TOML_PARSING_ALLOWLIST)),
        )


# ---------------------------------------------------------------------------
# 5.2 Local aggregate ownership / 5.3 dependency direction and sole owners
# ---------------------------------------------------------------------------

class TestLocalAggregateOwnedByAggregateModule(unittest.TestCase):
    maxDiff = None

    def test_aggregate_is_constructed_only_by_its_owner(self) -> None:
        offenders: list[str] = []
        for path in _docker_sources():
            if path == _AGGREGATE:
                continue
            for lineno in _aggregate_constructions(ast.parse(_source(path))):
                offenders.append(f"{_relative(path)}:{lineno}")
        self.assertEqual([], offenders)

    def test_aggregate_owner_names_are_defined_only_by_the_owner(self) -> None:
        offenders: list[tuple[str, str]] = []
        for path in _docker_sources():
            if path == _AGGREGATE:
                continue
            relative = _relative(path)
            tree = ast.parse(_source(path))
            for name in _AGGREGATE_OWNER_NAMES:
                if _defines(tree, name):
                    offenders.append((relative, name))
        self.assertEqual([], offenders)

    def test_inventory_module_exposes_no_legacy_companion_loaders(self) -> None:
        tree = ast.parse(_source(_INVENTORY))
        offenders = [name for name in _LEGACY_COMPANION_ADAPTERS if _defines(tree, name)]
        self.assertEqual([], offenders)

    def test_runtime_host_access_owner_owns_no_aggregate_state(self) -> None:
        source = _source(_DOCKER / "versioning" / "host_access.py")
        tree = ast.parse(source)
        forbidden_types = {
            "LocalConfig", "LocalCacheConfig", "LocalCorporateTrust", "LocalNetworkProxy",
        }
        offenders = sorted(
            name for name in forbidden_types if _references_name(tree, name)
        )
        self.assertEqual([], offenders)
        offenders = sorted(
            name for name in (
                "parse_local_cache_config",
                "parse_local_corporate_trust",
                "parse_local_network_proxy",
            )
            if _references_name(tree, name)
        )
        self.assertEqual([], offenders)


class TestAggregateConstructionDetectorIsNotVacuous(unittest.TestCase):
    """Aliased and qualified aggregate constructions are all detected."""

    _FORMS = {
        "bare name": "from docker.versioning.model import LocalConfig\nLocalConfig()",
        "module attribute": (
            "import docker.versioning.model as model\nmodel.LocalConfig()"
        ),
        "relative module attribute": (
            "from . import model\nmodel.LocalConfig()"
        ),
        "imported alias": (
            "from docker.versioning.model import LocalConfig as Config\nConfig()"
        ),
        "assignment alias": (
            "from docker.versioning.model import LocalConfig\n"
            "Config = LocalConfig\nConfig()"
        ),
        "attribute assignment alias": (
            "from . import model\nCfg = model.LocalConfig\nCfg()"
        ),
    }

    def test_every_construction_form_is_detected(self) -> None:
        for label, source in self._FORMS.items():
            with self.subTest(form=label):
                self.assertTrue(_aggregate_constructions(ast.parse(source)))

    def test_non_local_config_calls_are_not_false_positives(self) -> None:
        source = (
            "from docker.versioning.model import LocalHostAccess\n"
            "LocalHostAccess()\n"
        )
        self.assertEqual([], _aggregate_constructions(ast.parse(source)))


class TestDependencyDirectionAndSoleOwners(unittest.TestCase):
    maxDiff = None

    def test_domain_modules_do_not_import_the_aggregate_or_boundary(self) -> None:
        offenders: list[tuple[str, str]] = []
        for relative in _DOMAIN_MODULES:
            tree = ast.parse(_source(_REPO_ROOT / relative))
            imported = _imported_modules(tree)
            for forbidden in _AGGREGATE_OWNER_MODULES:
                if forbidden in imported:
                    offenders.append((relative, forbidden))
        self.assertEqual([], offenders)

    def test_cache_storage_remains_an_acyclic_leaf(self) -> None:
        tree = ast.parse(_source(_CACHE_STORAGE))
        unexpected = sorted(_imported_modules(tree) - _CACHE_STORAGE_ALLOWED_IMPORTS)
        self.assertEqual([], unexpected)

    def test_phase_2_and_4_modules_add_no_cache_root_policy(self) -> None:
        offenders: list[tuple[str, int, str]] = []
        for relative in _CACHE_POLICY_SCOPE:
            tree = ast.parse(_source(_REPO_ROOT / relative))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = _call_name(node.func)
                if name in _CACHE_POLICY_PRIMITIVES and _name_is_cache_like(node):
                    offenders.append((relative, node.lineno, name))
        self.assertEqual([], offenders)

    def test_cache_authority_api_is_defined_only_by_cache_storage(self) -> None:
        authority = (
            "resolve_default_root", "resolve_local_root", "resolve_effective_root",
            "prepare_default_root", "prepare_local_root", "prepare_resolved_root",
            "prepare_project_root", "versioning_child", "runtime_artifacts_child",
        )
        offenders: list[tuple[str, str]] = []
        for path in _docker_sources():
            if path == _CACHE_STORAGE:
                continue
            tree = ast.parse(_source(path))
            for name in authority:
                if _defines(tree, name):
                    offenders.append((_relative(path), name))
        self.assertEqual([], offenders)


# ---------------------------------------------------------------------------
# 5.4 Contract inventory for clauses moved out of runtime-host-access
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class MovedClause:
    """One scenario removed from ``runtime-host-access`` and its destination.

    ``regression_tests`` names every regression that must exist for the
    scenario's full THEN set: a scenario whose outcome has several independent
    branches maps to several focused tests.
    """

    source_scenario: str
    destination_capability: str
    destination_requirement: str
    destination_scenario: str
    regression_tests: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AuthoritativeObligation:
    """One atomic normative obligation of the removed requirement.

    ``obligation_id`` is a stable clause identifier; ``source_excerpt`` is the
    verbatim phrase carrying that one obligation in the authoritative
    requirement body.  Entries are loaded from the independent semantic-source
    manifest, never defined inline here.
    """

    obligation_id: str
    source_excerpt: str


@dataclass(frozen=True, slots=True)
class ObligationManifest:
    """Independent semantic source for the removed requirement's obligations.

    The manifest is a checked-in data file (``tests/data/
    moved_local_state_obligations.json``) derived by hand from the pre-cutover
    requirement.  ``connectors`` documents the non-normative glue allowed to
    separate obligation excerpts; the coverage validator requires the excerpts
    plus these connectors to tile the requirement body exactly.
    """

    requirement_title: str
    source_spec: str
    connectors: tuple[str, ...]
    obligations: tuple[AuthoritativeObligation, ...]


_OBLIGATION_MANIFEST_PATH = (
    Path(__file__).resolve().parent / "data" / "moved_local_state_obligations.json"
)


def _load_obligation_manifest(
    path: Path = _OBLIGATION_MANIFEST_PATH,
) -> ObligationManifest:
    """Load the independent obligation manifest from *path*."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    obligations = tuple(
        AuthoritativeObligation(
            obligation_id=entry["obligation_id"],
            source_excerpt=entry["source_excerpt"],
        )
        for entry in raw["obligations"]
    )
    return ObligationManifest(
        requirement_title=raw["requirement_title"],
        source_spec=raw["source_spec"],
        connectors=tuple(raw["non_normative_connectors"]),
        obligations=obligations,
    )


# The single authority for which obligations exist is the external manifest.
# This module never declares the obligation set inline.
_OBLIGATION_MANIFEST = _load_obligation_manifest()

# Markers that make a text fragment normative.  A documented connector must
# contain none of them; uncovered text containing any of them is reported.
_NORMATIVE_MARKERS = (
    "shall",
    "must",
    "neither",
    "nor",
    "reject",
    "rejects",
    "rejecting",
    "require",
    "requires",
    "derive",
    "derives",
    "enabled",
    "override",
    "independent",
)


@dataclass(frozen=True, slots=True)
class MovedObligation:
    """One manifest obligation mapped to its destination and regressions."""

    obligation_id: str
    destination_capability: str
    destination_requirement: str
    destination_scenario: str
    regression_tests: tuple[str, ...]


# The removed requirement ``Store constructor-project machine-local state
# separately`` had these nine scenarios.  Every one maps to a destination
# capability delta and one or more currently passing regression tests.
MOVED_CLAUSES: tuple[MovedClause, ...] = (
    MovedClause(
        source_scenario="Resolving the project local companion",
        destination_capability="local-project-configuration",
        destination_requirement="Resolve one project-local configuration companion",
        destination_scenario="Resolving the selected project's companion",
        regression_tests=(
            "tests.test_local_project_configuration_phase2."
            "TestFixedLocalCompanionResolution."
            "test_companion_is_always_the_fixed_basename_beside_the_inventory",
            "tests.test_local_project_configuration_phase2."
            "TestFixedLocalCompanionResolution."
            "test_resolution_never_searches_ancestors_or_the_working_directory",
        ),
    ),
    MovedClause(
        source_scenario="Loading a dedicated local cache root without host access",
        destination_capability="user-cache-storage",
        destination_requirement=(
            "Store persistent constructor caches and external constructor-project "
            "state under private roots"
        ),
        destination_scenario="Using a configured local cache root without host access",
        regression_tests=(
            "tests.test_moved_local_state_obligations_phase5."
            "TestLocalCacheRootWithoutHostAccess."
            "test_configured_local_cache_root_is_used_with_host_access_disabled",
        ),
    ),
    MovedClause(
        source_scenario="Rejecting a relative local cache root",
        destination_capability="user-cache-storage",
        destination_requirement="Secure constructor-owned cache paths",
        destination_scenario="Rejecting an empty or relative local cache root",
        regression_tests=(
            "tests.versioning.test_cache_storage.TestLocalRootResolution."
            "test_empty_local_root_rejected",
            "tests.versioning.test_cache_storage.TestLocalRootResolution."
            "test_relative_local_root_rejected",
            "tests.versioning.test_cache_storage.TestLocalRootResolution."
            "test_tilde_prefixed_local_root_rejected",
        ),
    ),
    MovedClause(
        source_scenario="Rejecting XDG_CACHE_HOME as a local cache root",
        destination_capability="user-cache-storage",
        destination_requirement="Secure constructor-owned cache paths",
        destination_scenario="Rejecting XDG_CACHE_HOME as a local cache root",
        regression_tests=(
            "tests.versioning.test_cache_storage.TestLocalRootResolution."
            "test_xdg_cache_home_rejected",
            "tests.versioning.test_cache_storage.TestLocalRootResolution."
            "test_lexical_equivalents_of_unsafe_roots_rejected",
        ),
    ),
    MovedClause(
        source_scenario="Rejecting shared or dangerous local cache roots",
        destination_capability="user-cache-storage",
        destination_requirement="Secure constructor-owned cache paths",
        destination_scenario="Rejecting shared or dangerous local cache roots",
        regression_tests=(
            "tests.versioning.test_cache_storage.TestLocalRootResolution."
            "test_home_directory_rejected",
            "tests.versioning.test_cache_storage.TestLocalRootResolution."
            "test_filesystem_root_rejected",
            "tests.versioning.test_cache_storage.TestLocalRootResolution."
            "test_ancestor_of_xdg_rejected",
            "tests.versioning.test_cache_storage_security.TestLocalRootSecurity."
            "test_unsafe_local_roots_rejected_before_write",
            "tests.test_moved_local_state_obligations_phase5."
            "TestDangerousRootDiagnostics."
            "test_dangerous_root_diagnostics_name_cache_dir_and_require_dedicated",
        ),
    ),
    MovedClause(
        source_scenario="Rejecting a symlinked local cache root",
        destination_capability="user-cache-storage",
        destination_requirement="Secure constructor-owned cache paths",
        destination_scenario="Rejecting a symlinked local cache root without following it",
        regression_tests=(
            "tests.versioning.test_cache_storage_security.TestLocalRootSecurity."
            "test_symlinked_selected_root_rejected",
            "tests.test_moved_local_state_obligations_phase5."
            "TestNoFollowInspectionOfExistingSelectedRoot."
            "test_dangling_symlink_selected_root_rejected_without_creating_target",
        ),
    ),
    MovedClause(
        source_scenario="Using corporate proxy without host access",
        destination_capability="local-project-configuration",
        destination_requirement="Compose closed domain-owned local tables",
        destination_scenario=(
            "Corporate network configuration is independent from host access"
        ),
        regression_tests=(
            "tests.test_domain_consumer_migration_phase4."
            "TestCorporateNetworkIndependence.test_proxy_is_usable_without_host_access",
            "tests.test_constructor_corporate_network_acceptance_red."
            "TestHostAccessIndependenceAcceptanceRed."
            "test_external_proxy_without_host_access_plans_and_runs",
        ),
    ),
    MovedClause(
        source_scenario="Rejecting malformed local state",
        destination_capability="configuration-document-validation",
        destination_requirement="Reject invalid project configuration before effects",
        destination_scenario="Local configuration is invalid",
        regression_tests=(
            # Malformed TOML syntax branch (retained).
            "tests.test_domain_consumer_migration_phase4."
            "TestMalformedLocalFailsBeforeEffects."
            "test_readonly_and_doctor_commands_reject_malformed_local",
            "tests.test_domain_consumer_migration_phase4."
            "TestMalformedLocalFailsBeforeEffects.test_verify_rejects_malformed_local",
            # Structured-diagnostic branches (unknown keys and invalid values).
            "tests.test_local_project_configuration_phase2."
            "TestClosedLocalTableRegistry.test_every_unknown_top_level_table_is_rejected",
            "tests.test_local_project_configuration_phase2."
            "TestOwnerSchemaDiagnosticsRetainDocumentIdentity."
            "test_unknown_and_misplaced_fields_keep_path_and_field",
            "tests.test_local_project_configuration_phase2."
            "TestOwnerSchemaDiagnosticsRetainDocumentIdentity."
            "test_domain_invalid_values_keep_path_and_field_without_the_value",
            # Per-branch diagnostic + effect-ordering regressions.
            "tests.test_malformed_local_state_phase5."
            "TestMalformedLocalStateBranches."
            "test_unknown_top_level_key_reports_field_and_blocks_effects",
            "tests.test_malformed_local_state_phase5."
            "TestMalformedLocalStateBranches."
            "test_unknown_nested_key_reports_field_and_blocks_effects",
            "tests.test_malformed_local_state_phase5."
            "TestMalformedLocalStateBranches."
            "test_malformed_syntax_reports_classification_and_blocks_effects",
            "tests.test_malformed_local_state_phase5."
            "TestMalformedLocalStateBranches."
            "test_invalid_host_address_reports_field_and_blocks_effects",
            "tests.test_malformed_local_state_phase5."
            "TestMalformedLocalStateBranches."
            "test_invalid_cache_dir_reports_field_and_blocks_effects",
            "tests.test_malformed_local_state_phase5."
            "TestMalformedLocalStateBranches."
            "test_invalid_corporate_trust_reports_field_and_blocks_effects",
            "tests.test_malformed_local_state_phase5."
            "TestMalformedLocalStateBranches."
            "test_invalid_network_proxy_reports_field_and_blocks_effects",
        ),
    ),
    MovedClause(
        source_scenario="Falling back when local cache directory is absent",
        destination_capability="user-cache-storage",
        destination_requirement=(
            "Store persistent constructor caches and external constructor-project "
            "state under private roots"
        ),
        destination_scenario="Falling back from an absent or relative XDG cache home",
        regression_tests=(
            "tests.versioning.test_cache_storage.TestDefaultRootResolution."
            "test_explicit_absolute_xdg_produces_candidate",
            "tests.versioning.test_cache_storage_security."
            "TestDefaultRootDirectoryHardening."
            "test_missing_explicit_xdg_created_0700",
            "tests.versioning.test_cache_storage_security.TestXdgEligibility."
            "test_existing_writable_xdg_accepted",
            "tests.versioning.test_cache_storage.TestDefaultRootResolution."
            "test_empty_xdg_falls_back_to_home_cache",
            "tests.versioning.test_cache_storage.TestDefaultRootResolution."
            "test_none_xdg_falls_back_to_home_cache",
            "tests.versioning.test_cache_storage.TestDefaultRootResolution."
            "test_relative_xdg_falls_back_to_home_cache",
            "tests.versioning.test_cache_storage_security.TestXdgEligibility."
            "test_xdg_non_directory_rejected_without_fallback",
            "tests.versioning.test_cache_storage_security.TestXdgEligibility."
            "test_xdg_unwritable_rejected_without_fallback",
            "tests.test_local_project_configuration_phase2."
            "TestAbsentCompanionDefaults."
            "test_absent_companion_returns_immutable_domain_defaults",
        ),
    ),
)

# Branches named by scenarios whose THEN set has several independent outcomes.
# Every named regression must appear in the scenario's ``regression_tests``;
# removing one must fail the branch-coverage test.
DANGEROUS_ROOT_BRANCH_REGRESSIONS: tuple[str, ...] = (
    "tests.versioning.test_cache_storage.TestLocalRootResolution."
    "test_home_directory_rejected",
    "tests.versioning.test_cache_storage.TestLocalRootResolution."
    "test_filesystem_root_rejected",
    "tests.versioning.test_cache_storage.TestLocalRootResolution."
    "test_ancestor_of_xdg_rejected",
    "tests.versioning.test_cache_storage_security.TestLocalRootSecurity."
    "test_unsafe_local_roots_rejected_before_write",
    "tests.test_moved_local_state_obligations_phase5."
    "TestDangerousRootDiagnostics."
    "test_dangerous_root_diagnostics_name_cache_dir_and_require_dedicated",
)

FALLBACK_BRANCH_REGRESSIONS: tuple[str, ...] = (
    "tests.versioning.test_cache_storage.TestDefaultRootResolution."
    "test_explicit_absolute_xdg_produces_candidate",
    "tests.versioning.test_cache_storage_security."
    "TestDefaultRootDirectoryHardening."
    "test_missing_explicit_xdg_created_0700",
    "tests.versioning.test_cache_storage_security.TestXdgEligibility."
    "test_existing_writable_xdg_accepted",
    "tests.versioning.test_cache_storage.TestDefaultRootResolution."
    "test_empty_xdg_falls_back_to_home_cache",
    "tests.versioning.test_cache_storage.TestDefaultRootResolution."
    "test_none_xdg_falls_back_to_home_cache",
    "tests.versioning.test_cache_storage.TestDefaultRootResolution."
    "test_relative_xdg_falls_back_to_home_cache",
    "tests.versioning.test_cache_storage_security.TestXdgEligibility."
    "test_xdg_non_directory_rejected_without_fallback",
    "tests.versioning.test_cache_storage_security.TestXdgEligibility."
    "test_xdg_unwritable_rejected_without_fallback",
    "tests.test_local_project_configuration_phase2."
    "TestAbsentCompanionDefaults."
    "test_absent_companion_returns_immutable_domain_defaults",
)

# Every malformed-local-state input branch named by the "Rejecting malformed
# local state" scenario.  Each branch maps to a focused regression that proves
# the structured field path and that the failure precedes network, cache
# mutation, artifact materialization, and Docker execution.  Removing any one
# from the scenario's ``regression_tests`` must fail branch coverage.
MALFORMED_STATE_BRANCH_REGRESSIONS: tuple[str, ...] = (
    "tests.test_malformed_local_state_phase5."
    "TestMalformedLocalStateBranches."
    "test_unknown_top_level_key_reports_field_and_blocks_effects",
    "tests.test_malformed_local_state_phase5."
    "TestMalformedLocalStateBranches."
    "test_unknown_nested_key_reports_field_and_blocks_effects",
    "tests.test_malformed_local_state_phase5."
    "TestMalformedLocalStateBranches."
    "test_malformed_syntax_reports_classification_and_blocks_effects",
    "tests.test_malformed_local_state_phase5."
    "TestMalformedLocalStateBranches."
    "test_invalid_host_address_reports_field_and_blocks_effects",
    "tests.test_malformed_local_state_phase5."
    "TestMalformedLocalStateBranches."
    "test_invalid_cache_dir_reports_field_and_blocks_effects",
    "tests.test_malformed_local_state_phase5."
    "TestMalformedLocalStateBranches."
    "test_invalid_corporate_trust_reports_field_and_blocks_effects",
    "tests.test_malformed_local_state_phase5."
    "TestMalformedLocalStateBranches."
    "test_invalid_network_proxy_reports_field_and_blocks_effects",
)

# The removed requirement body's atomic obligations and their destinations.
# Every authoritative obligation id must appear here exactly once and carry at
# least one regression that directly exercises it.
MOVED_OBLIGATIONS: tuple[MovedObligation, ...] = (
    MovedObligation(
        obligation_id="fixed_companion_location",
        destination_capability="local-project-configuration",
        destination_requirement="Resolve one project-local configuration companion",
        destination_scenario="Resolving the selected project's companion",
        regression_tests=(
            "tests.test_local_project_configuration_phase2."
            "TestFixedLocalCompanionResolution."
            "test_companion_is_always_the_fixed_basename_beside_the_inventory",
            "tests.test_local_project_configuration_phase2."
            "TestFixedLocalCompanionResolution."
            "test_resolution_never_searches_ancestors_or_the_working_directory",
        ),
    ),
    MovedObligation(
        obligation_id="closed_local_tables",
        destination_capability="local-project-configuration",
        destination_requirement="Compose closed domain-owned local tables",
        destination_scenario="Independent local concerns are composed",
        regression_tests=(
            "tests.test_local_project_configuration_phase2."
            "TestClosedLocalTableRegistry."
            "test_registry_is_closed_to_the_four_existing_tables",
            "tests.test_local_project_configuration_phase2."
            "TestClosedLocalTableRegistry."
            "test_exactly_four_existing_tables_are_accepted",
        ),
    ),
    MovedObligation(
        obligation_id="absolute_dedicated_cache_root",
        destination_capability="user-cache-storage",
        destination_requirement="Secure constructor-owned cache paths",
        destination_scenario="Using an existing dedicated local cache root",
        regression_tests=(
            "tests.versioning.test_cache_storage.TestLocalRootResolution."
            "test_absolute_dedicated_child_of_xdg_accepted",
            "tests.versioning.test_cache_storage.TestLocalRootResolution."
            "test_absolute_non_xdg_dedicated_root_accepted",
        ),
    ),
    MovedObligation(
        obligation_id="separate_named_cache_children",
        destination_capability="user-cache-storage",
        destination_requirement=(
            "Store persistent constructor caches and external constructor-project "
            "state under private roots"
        ),
        destination_scenario="Using a configured local cache root without host access",
        regression_tests=(
            "tests.versioning.test_cache_storage.TestCacheChildDerivation."
            "test_children_are_distinct_named_subtrees",
            "tests.versioning.test_cache_storage.TestCacheChildDerivation."
            "test_children_from_valid_local_root",
        ),
    ),
    MovedObligation(
        obligation_id="empty_and_relative_rejection",
        destination_capability="user-cache-storage",
        destination_requirement="Secure constructor-owned cache paths",
        destination_scenario="Rejecting an empty or relative local cache root",
        regression_tests=(
            "tests.versioning.test_cache_storage.TestLocalRootResolution."
            "test_empty_local_root_rejected",
            "tests.versioning.test_cache_storage.TestLocalRootResolution."
            "test_relative_local_root_rejected",
            "tests.versioning.test_cache_storage.TestLocalRootResolution."
            "test_tilde_prefixed_local_root_rejected",
        ),
    ),
    MovedObligation(
        obligation_id="lexical_normalization",
        destination_capability="user-cache-storage",
        destination_requirement="Secure constructor-owned cache paths",
        destination_scenario="Rejecting shared or dangerous local cache roots",
        regression_tests=(
            "tests.versioning.test_cache_storage.TestLocalRootResolution."
            "test_lexical_variants_of_dedicated_child_normalized",
            "tests.versioning.test_cache_storage.TestLocalRootResolution."
            "test_lexical_equivalents_of_unsafe_roots_rejected",
        ),
    ),
    MovedObligation(
        obligation_id="xdg_equality_rejection",
        destination_capability="user-cache-storage",
        destination_requirement="Secure constructor-owned cache paths",
        destination_scenario="Rejecting XDG_CACHE_HOME as a local cache root",
        regression_tests=(
            "tests.versioning.test_cache_storage.TestLocalRootResolution."
            "test_xdg_cache_home_rejected",
            "tests.versioning.test_cache_storage.TestLocalRootResolution."
            "test_lexical_equivalents_of_unsafe_roots_rejected",
        ),
    ),
    MovedObligation(
        obligation_id="home_directory_rejection",
        destination_capability="user-cache-storage",
        destination_requirement="Secure constructor-owned cache paths",
        destination_scenario="Rejecting shared or dangerous local cache roots",
        regression_tests=(
            "tests.versioning.test_cache_storage.TestLocalRootResolution."
            "test_home_directory_rejected",
        ),
    ),
    MovedObligation(
        obligation_id="filesystem_root_rejection",
        destination_capability="user-cache-storage",
        destination_requirement="Secure constructor-owned cache paths",
        destination_scenario="Rejecting shared or dangerous local cache roots",
        regression_tests=(
            "tests.versioning.test_cache_storage.TestLocalRootResolution."
            "test_filesystem_root_rejected",
        ),
    ),
    MovedObligation(
        obligation_id="ancestor_of_xdg_rejection",
        destination_capability="user-cache-storage",
        destination_requirement="Secure constructor-owned cache paths",
        destination_scenario="Rejecting shared or dangerous local cache roots",
        regression_tests=(
            "tests.versioning.test_cache_storage.TestLocalRootResolution."
            "test_ancestor_of_xdg_rejected",
            "tests.versioning.test_cache_storage.TestLocalRootResolution."
            "test_double_leading_slash_roots_rejected",
        ),
    ),
    MovedObligation(
        obligation_id="no_follow_inspection",
        destination_capability="user-cache-storage",
        destination_requirement="Secure constructor-owned cache paths",
        destination_scenario="Using an existing dedicated local cache root",
        regression_tests=(
            "tests.test_moved_local_state_obligations_phase5."
            "TestNoFollowInspectionOfExistingSelectedRoot."
            "test_dangling_symlink_selected_root_rejected_without_creating_target",
            "tests.test_moved_local_state_obligations_phase5."
            "TestNoFollowInspectionOfExistingSelectedRoot."
            "test_symlinked_parent_component_rejected_without_following",
            "tests.test_moved_local_state_obligations_phase5."
            "TestNoFollowInspectionOfExistingSelectedRoot."
            "test_existing_selected_root_is_inspected_and_secured_in_place",
        ),
    ),
    MovedObligation(
        obligation_id="symlink_rejection",
        destination_capability="user-cache-storage",
        destination_requirement="Secure constructor-owned cache paths",
        destination_scenario="Rejecting a symlinked local cache root without following it",
        regression_tests=(
            "tests.versioning.test_cache_storage_security.TestLocalRootSecurity."
            "test_symlinked_selected_root_rejected",
            "tests.test_moved_local_state_obligations_phase5."
            "TestNoFollowInspectionOfExistingSelectedRoot."
            "test_dangling_symlink_selected_root_rejected_without_creating_target",
        ),
    ),
    MovedObligation(
        obligation_id="reviewed_policy_isolation",
        destination_capability="local-project-configuration",
        destination_requirement="Keep local configuration separate from reviewed state",
        destination_scenario="Producing reviewed and effective projections",
        regression_tests=(
            "tests.test_moved_local_state_obligations_phase5."
            "TestReviewedStateIsolation."
            "test_local_companion_cannot_declare_reviewed_inventory_tables",
            "tests.test_moved_local_state_obligations_phase5."
            "TestReviewedStateIsolation."
            "test_local_companion_does_not_change_reviewed_projection",
            "tests.test_domain_consumer_migration_phase4."
            "TestLocalSourceConfinement."
            "test_local_state_absent_from_serialization_and_vectors",
        ),
    ),
    MovedObligation(
        obligation_id="reviewed_cache_ttl_isolation",
        destination_capability="local-project-configuration",
        destination_requirement="Keep local configuration separate from reviewed state",
        destination_scenario="Producing reviewed and effective projections",
        regression_tests=(
            "tests.test_moved_local_state_obligations_phase5."
            "TestReviewedStateIsolation."
            "test_local_companion_cannot_declare_reviewed_cache_ttl",
            "tests.test_moved_local_state_obligations_phase5."
            "TestReviewedStateIsolation."
            "test_local_companion_does_not_change_reviewed_projection",
        ),
    ),
    MovedObligation(
        obligation_id="corporate_trust_independence",
        destination_capability="local-project-configuration",
        destination_requirement="Compose closed domain-owned local tables",
        destination_scenario=(
            "Corporate network configuration is independent from host access"
        ),
        regression_tests=(
            "tests.test_moved_local_state_obligations_phase5."
            "TestCorporateNetworkIndependence."
            "test_corporate_trust_and_proxy_need_no_host_access_state",
            "tests.test_constructor_corporate_network_acceptance_red."
            "TestConfiguredBuildRunAcceptanceRed."
            "test_configured_build_dry_run_carries_corporate_args",
        ),
    ),
    MovedObligation(
        obligation_id="proxy_independence",
        destination_capability="local-project-configuration",
        destination_requirement="Compose closed domain-owned local tables",
        destination_scenario=(
            "Corporate network configuration is independent from host access"
        ),
        regression_tests=(
            "tests.test_constructor_corporate_network_acceptance_red."
            "TestHostAccessIndependenceAcceptanceRed."
            "test_external_proxy_without_host_access_plans_and_runs",
            "tests.test_domain_consumer_migration_phase4."
            "TestCorporateNetworkIndependence."
            "test_proxy_is_usable_without_host_access",
        ),
    ),
    MovedObligation(
        obligation_id="proxy_url_non_derivation",
        destination_capability="local-project-configuration",
        destination_requirement="Compose closed domain-owned local tables",
        destination_scenario=(
            "Corporate network configuration is independent from host access"
        ),
        regression_tests=(
            "tests.test_constructor_corporate_network_acceptance_red."
            "TestHostAccessIndependenceAcceptanceRed."
            "test_external_proxy_without_host_access_plans_and_runs",
        ),
    ),
)

# Pre-cutover authoritative text for the requirement removed from
# ``runtime-host-access``.  Before this change is archived the requirement
# lives in the main spec; once archived it survives in the archived change
# that originally introduced it.  Both are searched independently of the
# inventory so an omission cannot pass.
_PRE_CUTOVER_SPEC = (
    _REPO_ROOT / "openspec" / "specs" / "runtime-host-access" / "spec.md"
)
_ARCHIVE_ROOT = _REPO_ROOT / "openspec" / "changes" / "archive"
_REMOVED_REQUIREMENT = "Store constructor-project machine-local state separately"

_SPEC_CACHE_DIRS = (
    _CHANGE_DIR,
    *sorted((_REPO_ROOT / "openspec" / "changes" / "archive").glob(
        f"*{_CHANGE_NAME}"
    )),
)


@dataclass(frozen=True, slots=True)
class SpecRequirement:
    """Authoritative requirement body and scenario headings."""

    body: str
    scenarios: tuple[str, ...]


def _authoritative_spec_candidates() -> list[Path]:
    """Pre-cutover main spec first, then archived specs that retain it."""
    candidates = [_PRE_CUTOVER_SPEC]
    if _ARCHIVE_ROOT.is_dir():
        for change in sorted(_ARCHIVE_ROOT.iterdir()):
            candidates.extend(sorted(change.glob("specs/*/spec.md")))
    return candidates


def _find_authoritative_requirement(title: str) -> SpecRequirement | None:
    """Locate *title* in the pre-cutover spec or an archived spec."""
    for path in _authoritative_spec_candidates():
        if not path.is_file():
            continue
        parsed = _requirements(path.read_text(encoding="utf-8"))
        requirement = parsed.get(title)
        if requirement is not None and requirement.scenarios:
            return requirement
    return None


def _requirements(text: str) -> dict[str, SpecRequirement]:
    """Parse a spec into requirement title -> body/scenarios.

    The body is every non-heading line between the ``### Requirement`` heading
    and its first ``#### Scenario`` heading, whitespace-collapsed.  Lines after
    the first scenario (the WHEN/THEN list) are not treated as body.
    """
    requirements: dict[str, SpecRequirement] = {}
    title: str | None = None
    body_lines: list[str] = []
    scenarios: list[str] = []
    in_scenario = False

    def flush() -> None:
        if title is not None:
            requirements[title] = SpecRequirement(
                body=" ".join(body_lines),
                scenarios=tuple(scenarios),
            )

    for line in text.splitlines():
        if line.startswith("### Requirement: "):
            flush()
            title = line[len("### Requirement: "):].strip()
            body_lines = []
            scenarios = []
            in_scenario = False
        elif line.startswith("#### Scenario: ") and title is not None:
            scenarios.append(line[len("#### Scenario: "):].strip())
            in_scenario = True
        elif title is not None and not in_scenario and not line.startswith("#"):
            stripped = line.strip()
            if stripped:
                body_lines.append(stripped)
    flush()
    return requirements


def _parse_removed_requirements(text: str) -> set[str]:
    """Requirement titles under the delta spec's ``## REMOVED Requirements``."""
    removed: set[str] = set()
    in_removed = False
    for line in text.splitlines():
        if line.startswith("## "):
            in_removed = line.strip() == "## REMOVED Requirements"
        elif in_removed and line.startswith("### Requirement: "):
            removed.add(line[len("### Requirement: "):].strip())
    return removed


def _delta_spec_path(capability: str) -> Path | None:
    for base in _SPEC_CACHE_DIRS:
        candidate = base / "specs" / capability / "spec.md"
        if candidate.is_file():
            return candidate
    return None


def _resolve_regression_test(test_id: str) -> object:
    module_name, class_name, method_name = test_id.rsplit(".", 2)
    module = importlib.import_module(module_name)
    test_class = getattr(module, class_name)
    return getattr(test_class, method_name)


def _mapping_errors(
    label: str,
    capability: str,
    requirement: str,
    scenario: str,
    regression_tests: tuple[str, ...],
) -> list[str]:
    """Check one inventory entry against its destination spec and tests."""
    errors: list[str] = []
    if not regression_tests:
        errors.append(f"{label}: no regression test mapped")
    spec_path = _delta_spec_path(capability)
    if spec_path is None:
        return errors + [f"{label}: no delta spec for {capability}"]
    requirements = _requirements(spec_path.read_text(encoding="utf-8"))
    entry = requirements.get(requirement)
    if entry is None:
        errors.append(f"{label}: {capability} has no requirement {requirement!r}")
    elif scenario not in entry.scenarios:
        errors.append(f"{label}: {requirement!r} has no scenario {scenario!r}")
    for regression_test in regression_tests:
        try:
            _resolve_regression_test(regression_test)
        except (ImportError, AttributeError, ValueError) as exc:
            errors.append(
                f"{label}: regression {regression_test!r} does not resolve ({exc})"
            )
    return errors


def _obligation_source_errors(
    obligations: tuple[AuthoritativeObligation, ...],
    body: str,
) -> list[str]:
    """Reconcile the authoritative obligation list with the spec body."""
    errors: list[str] = []
    normalized = " ".join(body.split())
    seen: set[str] = set()
    for obligation in obligations:
        if obligation.obligation_id in seen:
            errors.append(f"duplicate obligation id: {obligation.obligation_id}")
        seen.add(obligation.obligation_id)
        count = normalized.count(obligation.source_excerpt)
        if count != 1:
            errors.append(
                f"{obligation.obligation_id}: excerpt "
                f"{obligation.source_excerpt!r} occurs {count} times in the "
                "authoritative body"
            )
    return errors


def _untiled_text(text: str, connectors: tuple[str, ...]) -> str:
    """Return the suffix of *text* that no connector sequence covers.

    The empty string means *text* is fully composed of documented
    non-normative connectors.  Any other return value is normative source text
    that no manifest entry accounts for.
    """
    coverage = [False] * (len(text) + 1)
    coverage[0] = True
    reachable = 0
    for index in range(len(text) + 1):
        if not coverage[index]:
            continue
        reachable = index
        for connector in connectors:
            if text.startswith(connector, index):
                coverage[index + len(connector)] = True
    return text[reachable:]


def _source_coverage_errors(
    obligations: tuple[AuthoritativeObligation, ...],
    body: str,
    connectors: tuple[str, ...],
) -> list[str]:
    """Fail if any authoritative text is not accounted for by the manifest.

    Every excerpt must occur exactly once.  In body order, the excerpts (plus
    the documented non-normative connectors between them) must tile the whole
    whitespace-normalized body.  Any residue is reported as uncovered
    normative text, so dropping an obligation from the manifest fails even
    though the remaining entries are individually valid.
    """
    errors = _obligation_source_errors(obligations, body)
    if errors:
        return errors
    normalized = " ".join(body.split())
    located = sorted(
        (normalized.index(obligation.source_excerpt), obligation)
        for obligation in obligations
    )
    cursor = 0
    for index, obligation in located:
        uncovered = _untiled_text(normalized[cursor:index], connectors)
        if uncovered:
            errors.append(
                "uncovered normative text before "
                f"{obligation.obligation_id}: {uncovered!r}"
            )
        cursor = index + len(obligation.source_excerpt)
    uncovered = _untiled_text(normalized[cursor:], connectors)
    if uncovered:
        errors.append(
            f"uncovered normative text after the last obligation: {uncovered!r}"
        )
    return errors


def _without_obligation(
    manifest: ObligationManifest,
    obligation_id: str,
) -> ObligationManifest:
    """Return a copy of *manifest* with one obligation removed."""
    remaining = tuple(
        item for item in manifest.obligations if item.obligation_id != obligation_id
    )
    assert len(remaining) == len(manifest.obligations) - 1, obligation_id
    return replace(manifest, obligations=remaining)


def _inventory_errors(clauses: tuple[MovedClause, ...]) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    for clause in clauses:
        if clause.source_scenario in seen:
            errors.append(f"duplicate source scenario: {clause.source_scenario}")
        seen.add(clause.source_scenario)
        errors.extend(
            _mapping_errors(
                clause.source_scenario,
                clause.destination_capability,
                clause.destination_requirement,
                clause.destination_scenario,
                clause.regression_tests,
            )
        )
    return errors


def _obligation_inventory_errors(
    obligations: tuple[MovedObligation, ...],
) -> list[str]:
    errors: list[str] = []
    known = {item.obligation_id for item in _OBLIGATION_MANIFEST.obligations}
    seen: set[str] = set()
    for obligation in obligations:
        if obligation.obligation_id in seen:
            errors.append(f"duplicate obligation: {obligation.obligation_id}")
        seen.add(obligation.obligation_id)
        if obligation.obligation_id not in known:
            errors.append(f"unknown obligation id: {obligation.obligation_id}")
            continue
        errors.extend(
            _mapping_errors(
                obligation.obligation_id,
                obligation.destination_capability,
                obligation.destination_requirement,
                obligation.destination_scenario,
                obligation.regression_tests,
            )
        )
    return errors


def _scenario_entry(source_scenario: str) -> MovedClause:
    for clause in MOVED_CLAUSES:
        if clause.source_scenario == source_scenario:
            return clause
    raise AssertionError(f"no inventory entry for scenario {source_scenario!r}")


def _missing_regressions(
    entry: MovedClause,
    required: tuple[str, ...],
) -> list[str]:
    return [test_id for test_id in required if test_id not in entry.regression_tests]


class TestMovedClauseInventory(unittest.TestCase):
    maxDiff = None

    def _authoritative(self) -> SpecRequirement:
        requirement = _find_authoritative_requirement(_REMOVED_REQUIREMENT)
        self.assertIsNotNone(
            requirement,
            f"authoritative pre-cutover requirement {_REMOVED_REQUIREMENT!r} "
            "must exist in the main spec or an archived spec",
        )
        assert requirement is not None  # for type checkers
        return requirement

    def test_every_removed_scenario_is_inventoried(self) -> None:
        authoritative = self._authoritative()
        self.assertEqual(
            set(authoritative.scenarios),
            {clause.source_scenario for clause in MOVED_CLAUSES},
            "inventory must cover every scenario in the authoritative requirement",
        )
        self.assertEqual(
            len(MOVED_CLAUSES),
            len({clause.source_scenario for clause in MOVED_CLAUSES}),
            "scenario inventory must not duplicate a source scenario",
        )

    def test_every_atomic_obligation_is_inventoried(self) -> None:
        manifest = _OBLIGATION_MANIFEST
        authoritative = self._authoritative()
        # The manifest must be about the removed requirement...
        self.assertEqual(_REMOVED_REQUIREMENT, manifest.requirement_title)
        # ...its excerpts must each occur exactly once...
        self.assertEqual(
            [],
            _obligation_source_errors(manifest.obligations, authoritative.body),
        )
        # ...and excerpts plus non-normative connectors must tile the body, so
        # any obligation dropped from the manifest leaves normative text
        # uncovered even though the surviving entries are individually valid.
        self.assertEqual(
            [],
            _source_coverage_errors(
                manifest.obligations, authoritative.body, manifest.connectors
            ),
            "manifest obligations must account for all normative source text",
        )
        # Only now compare the independently-derived obligation set to the
        # mapping layer.  Equality rejects both an omission and an invention.
        self.assertEqual(
            {item.obligation_id for item in manifest.obligations},
            {item.obligation_id for item in MOVED_OBLIGATIONS},
            "mapping must cover every manifest obligation exactly, and no more",
        )
        self.assertEqual(
            len(MOVED_OBLIGATIONS),
            len({item.obligation_id for item in MOVED_OBLIGATIONS}),
            "obligation inventory must not assign one obligation twice",
        )

    def test_manifest_connectors_are_documented_as_non_normative(self) -> None:
        self.assertTrue(_OBLIGATION_MANIFEST.connectors, "connectors must be listed")
        for connector in _OBLIGATION_MANIFEST.connectors:
            lowered = connector.lower()
            offenders = [marker for marker in _NORMATIVE_MARKERS if marker in lowered]
            self.assertEqual(
                [],
                offenders,
                f"connector {connector!r} must carry no normative marker",
            )

    def test_every_normative_marker_lies_within_a_manifest_excerpt(self) -> None:
        authoritative = self._authoritative()
        normalized = " ".join(authoritative.body.split())
        covered: list[bool] = [False] * len(normalized)
        for obligation in _OBLIGATION_MANIFEST.obligations:
            start = normalized.index(obligation.source_excerpt)
            for index in range(start, start + len(obligation.source_excerpt)):
                covered[index] = True
        for marker in _NORMATIVE_MARKERS:
            for match in re.finditer(rf"\b{re.escape(marker)}\b", normalized, re.IGNORECASE):
                start, end = match.span()
                self.assertTrue(
                    all(covered[start:end]),
                    f"normative marker {match.group(0)!r} at offset {start} "
                    "is not assigned to any manifest obligation",
                )

    def test_delta_spec_declares_the_requirement_removed(self) -> None:
        delta = (
            _CHANGE_DIR / "specs" / "runtime-host-access" / "spec.md"
        ).read_text(encoding="utf-8")
        self.assertIn("## REMOVED Requirements", delta)
        self.assertIn(_REMOVED_REQUIREMENT, _parse_removed_requirements(delta))

    def test_each_scenario_maps_to_a_destination_and_passing_regressions(self) -> None:
        self.assertEqual([], _inventory_errors(MOVED_CLAUSES))

    def test_each_obligation_maps_to_a_destination_and_regressions(self) -> None:
        self.assertEqual([], _obligation_inventory_errors(MOVED_OBLIGATIONS))

    def test_dangerous_root_scenario_covers_every_authoritative_branch(self) -> None:
        entry = _scenario_entry("Rejecting shared or dangerous local cache roots")
        self.assertEqual(
            [], _missing_regressions(entry, DANGEROUS_ROOT_BRANCH_REGRESSIONS)
        )

    def test_fallback_scenario_covers_every_authoritative_branch(self) -> None:
        entry = _scenario_entry("Falling back when local cache directory is absent")
        self.assertEqual([], _missing_regressions(entry, FALLBACK_BRANCH_REGRESSIONS))

    def test_malformed_state_scenario_covers_every_branch(self) -> None:
        entry = _scenario_entry("Rejecting malformed local state")
        self.assertEqual(
            [], _missing_regressions(entry, MALFORMED_STATE_BRANCH_REGRESSIONS)
        )

    def test_cache_without_host_access_scenario_uses_a_disabled_test(self) -> None:
        entry = _scenario_entry("Loading a dedicated local cache root without host access")
        discouraged = (
            "tests.test_constructor_host_access_acceptance."
            "TestCustomInventoryWithCache.test_custom_inventory_with_local_cache"
        )
        self.assertNotIn(
            discouraged,
            entry.regression_tests,
            "the mapping must not use a test that enables [runtime.host-access]",
        )
        self.assertIn(
            "tests.test_moved_local_state_obligations_phase5."
            "TestLocalCacheRootWithoutHostAccess."
            "test_configured_local_cache_root_is_used_with_host_access_disabled",
            entry.regression_tests,
        )

    def test_every_referenced_regression_runs_and_passes(self) -> None:
        import unittest as _unittest

        suite = _unittest.TestSuite()
        test_ids: list[str] = []
        for clause in MOVED_CLAUSES:
            test_ids.extend(clause.regression_tests)
        for obligation in MOVED_OBLIGATIONS:
            test_ids.extend(obligation.regression_tests)
        for test_id in dict.fromkeys(test_ids):
            module_name, class_name, method_name = test_id.rsplit(".", 2)
            module = importlib.import_module(module_name)
            suite.addTest(getattr(module, class_name)(method_name))
        result = _unittest.TextTestRunner(
            stream=io.StringIO(), verbosity=0
        ).run(suite)
        self.assertTrue(result.wasSuccessful(), "mapped regression tests must pass")


class TestMovedClauseDetectorIsNotVacuous(unittest.TestCase):
    """An omission, a bogus destination, or a missing regression is detected."""

    def test_bogus_scenario_destination_is_flagged(self) -> None:
        broken = (
            MOVED_CLAUSES[0],
            MovedClause(
                source_scenario="Rejecting malformed local state",
                destination_capability="user-cache-storage",
                destination_requirement="no such requirement",
                destination_scenario="no such scenario",
                regression_tests=("tests.nonexistent.module.Class.method",),
            ),
        )
        errors = _inventory_errors(broken)
        self.assertTrue(any("no such requirement" in message for message in errors))
        self.assertTrue(any("does not resolve" in message for message in errors))

    def test_bogus_obligation_destination_is_flagged(self) -> None:
        broken = (
            MOVED_OBLIGATIONS[0],
            MovedObligation(
                obligation_id="proxy_url_non_derivation",
                destination_capability="user-cache-storage",
                destination_requirement="no such requirement",
                destination_scenario="no such scenario",
                regression_tests=("tests.nonexistent.module.Class.method",),
            ),
        )
        errors = _obligation_inventory_errors(broken)
        self.assertTrue(any("no such requirement" in message for message in errors))
        self.assertTrue(any("does not resolve" in message for message in errors))

    def test_obligation_without_a_regression_is_flagged(self) -> None:
        broken = (replace(MOVED_OBLIGATIONS[0], regression_tests=()),)
        errors = _obligation_inventory_errors(broken)
        self.assertTrue(any("no regression test" in message for message in errors))

    def test_obligation_omission_is_detected(self) -> None:
        # This layer compares the mapping against the manifest's ids; the
        # manifest-omission layer is covered by
        # ``test_manifest_obligation_omission_fails_source_coverage``.
        omitted = MOVED_OBLIGATIONS[:-1]
        self.assertNotEqual(
            {item.obligation_id for item in _OBLIGATION_MANIFEST.obligations},
            {item.obligation_id for item in omitted},
        )

    def test_manifest_obligation_omission_fails_source_coverage(self) -> None:
        authoritative = _find_authoritative_requirement(_REMOVED_REQUIREMENT)
        assert authoritative is not None
        # The intact manifest covers the source exactly.
        self.assertEqual(
            [],
            _source_coverage_errors(
                _OBLIGATION_MANIFEST.obligations,
                authoritative.body,
                _OBLIGATION_MANIFEST.connectors,
            ),
        )
        # Dropping ANY single manifest entry must leave normative text
        # uncovered, detected from the source text alone (MOVED_OBLIGATIONS is
        # never consulted here).
        for obligation in _OBLIGATION_MANIFEST.obligations:
            reduced = _without_obligation(_OBLIGATION_MANIFEST, obligation.obligation_id)
            errors = _source_coverage_errors(
                reduced.obligations,
                authoritative.body,
                reduced.connectors,
            )
            self.assertTrue(
                errors,
                f"omitting {obligation.obligation_id!r} from the manifest must be "
                "detected from the source text",
            )
            self.assertTrue(
                any("uncovered normative text" in message for message in errors),
                errors,
            )

    def test_compound_obligation_omissions_are_detected_from_source(self) -> None:
        authoritative = _find_authoritative_requirement(_REMOVED_REQUIREMENT)
        assert authoritative is not None
        # Each of these is a subordinate/list obligation attached to a larger
        # clause; a set comparison cannot see it, so the source-coverage
        # validator must.
        for obligation_id in (
            "reviewed_cache_ttl_isolation",
            "ancestor_of_xdg_rejection",
            "proxy_url_non_derivation",
            "separate_named_cache_children",
            "home_directory_rejection",
            "proxy_independence",
        ):
            reduced = _without_obligation(_OBLIGATION_MANIFEST, obligation_id)
            errors = _source_coverage_errors(
                reduced.obligations,
                authoritative.body,
                reduced.connectors,
            )
            self.assertTrue(errors, f"omitting {obligation_id!r} must be detected")
            self.assertTrue(
                any("uncovered normative text" in message for message in errors),
                errors,
            )

    def test_obligation_duplication_is_detected(self) -> None:
        broken = MOVED_OBLIGATIONS + (MOVED_OBLIGATIONS[0],)
        errors = _obligation_inventory_errors(broken)
        self.assertTrue(any("duplicate obligation" in message for message in errors))

    def test_unknown_obligation_id_is_detected(self) -> None:
        broken = (replace(MOVED_OBLIGATIONS[0], obligation_id="invented"),)
        errors = _obligation_inventory_errors(broken)
        self.assertTrue(any("unknown obligation id" in message for message in errors))

    def test_dangerous_root_branch_removal_is_detected(self) -> None:
        entry = _scenario_entry("Rejecting shared or dangerous local cache roots")
        trimmed = replace(entry, regression_tests=entry.regression_tests[:-1])
        self.assertTrue(
            _missing_regressions(trimmed, DANGEROUS_ROOT_BRANCH_REGRESSIONS)
        )

    def test_fallback_branch_removal_is_detected(self) -> None:
        entry = _scenario_entry("Falling back when local cache directory is absent")
        trimmed = replace(entry, regression_tests=entry.regression_tests[:-1])
        self.assertTrue(_missing_regressions(trimmed, FALLBACK_BRANCH_REGRESSIONS))

    def test_malformed_state_branch_removal_is_detected(self) -> None:
        entry = _scenario_entry("Rejecting malformed local state")
        for required in MALFORMED_STATE_BRANCH_REGRESSIONS:
            with self.subTest(required=required):
                trimmed = replace(
                    entry,
                    regression_tests=tuple(
                        test_id
                        for test_id in entry.regression_tests
                        if test_id != required
                    ),
                )
                self.assertIn(
                    required,
                    _missing_regressions(trimmed, MALFORMED_STATE_BRANCH_REGRESSIONS),
                    "removing any required malformed-state branch must be detected",
                )

    def test_excerpt_drift_is_detected(self) -> None:
        authoritative = _find_authoritative_requirement(_REMOVED_REQUIREMENT)
        assert authoritative is not None
        drifted = (
            AuthoritativeObligation(
                obligation_id="invented",
                source_excerpt="this phrase is not in the authoritative body",
            ),
        )
        errors = _obligation_source_errors(drifted, authoritative.body)
        self.assertTrue(any("occurs 0 times" in message for message in errors))

    def test_authoritative_requirement_resolves(self) -> None:
        self.assertIsNotNone(_find_authoritative_requirement(_REMOVED_REQUIREMENT))


if __name__ == "__main__":
    unittest.main()
