"""Phase 5A — typed error preservation across consumer boundaries.

These tests hold the executable inventory that distinguishes legitimate cause
inspection (absence, errno, no-follow safety, domain-result selection) from
cause *replacement*.  A successful L1 or lock-layer normalization is preserved
by every L2 adapter, cleanup accumulator, lock wrapper, and domain boundary;
a domain error chains directly from the typed failure, never from its raw
cause.  Raw ``OSError`` remains authoritative only before capability adoption,
at a directly injected POSIX operation, or under a separately documented
raw-contract exception (the inventory of which is empty for this change).
"""
from __future__ import annotations

import ast
import errno
import os
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from docker.filesystem.cleanup import CleanupFailures
from docker.transactions.capabilities import CapabilityError, DirectoryCapability
from docker.transactions.cleanup import CleanupFailures as TransactionCleanupFailures
from docker.transactions.errors import (
    STAGE_CLOSE,
    STAGE_LOCK_ACQUIRE,
    STAGE_LOCK_MODE,
    STAGE_LOCK_STAT,
    STAGE_LOCK_VALIDATE,
    STAGE_VALIDATE,
    CloseStageFailure,
    LockError,
    TransactionError,
)
from docker.transactions.posix import PosixFileOps
from docker.versioning.build_cache import ConstructorProjectBuildLock
from tests.transactions_test_support import InjectedOps

_REPO = Path(__file__).resolve().parents[1]

# ── Task 5A.1 — repository-wide cause-access inventory ──────────────────
#
# Every production cause access is classified as one of:
#   definition           — the exception constructor stores its own cause
#   typed-propagation    — a new typed wrapper is built and chained from the
#                          cause (never replaced by it)
#   domain-wrapping      — a domain error chains directly from the typed
#                          failure, or the typed wrapper is returned unchanged
#   inspect-only         — the cause drives a control-flow/domain decision
#   pre-adoption-raw     — a raw POSIX failure from a still caller-owned
#                          descriptor (not derived from a typed wrapper)
#   justified-raw        — exact raw exception type required by a documented
#                          public contract (none for this change)
_CAUSE_ACCESS_INVENTORY: dict[tuple[str, str], str] = {
    ("docker/transactions/errors.py", "TransactionError.__init__"): "definition",
    (
        "docker/transactions/capabilities.py",
        "DirectoryCapability.from_secure_path",
    ): "typed-propagation",
    ("docker/versioning/build_cache.py", "_raise_lock_failure"): "inspect-only",
    ("docker/versioning/build_cache.py", "_validate_existing_blob"): "inspect-only",
    ("docker/versioning/build_cache.py", "_validate_existing_marker"): "inspect-only",
    (
        "docker/versioning/build_orchestration.py",
        "_exception_chain",
    ): "inspect-only",
    (
        "docker/versioning/build_orchestration.py",
        "_format_build_cleanup_diagnostic",
    ): "inspect-only",
    (
        "docker/versioning/host_progress.py",
        "_FailureContextRegistry.lookup",
    ): "inspect-only",
    (
        "docker/versioning/diagnostic_projection.py",
        "_exception_type_chain",
    ): "inspect-only",
    ("docker/npm_environment/storage.py", "_foundation_cause"): "inspect-only",
    ("docker/npm_environment/storage.py", "_failing_component"): "inspect-only",
    ("docker/npm_environment/storage.py", "_open_failure_detail"): "inspect-only",
    ("docker/npm_environment/storage.py", "_child_failure_detail"): "inspect-only",
    ("docker/npm_environment/storage.py", "_create_staging_directory"): "inspect-only",
    ("docker/npm_environment/tree.py", "_foundation_cause"): "inspect-only",
    ("docker/versioning/cache_storage.py", "_foundation_cause"): "inspect-only",
    ("docker/versioning/cache_storage.py", "_errno_of"): "inspect-only",
    ("docker/versioning/cache_storage.py", "_entry_error"): "inspect-only",
    ("docker/versioning/cache_storage.py", "_secure_error"): "inspect-only",
    ("docker/versioning/cache_storage.py", "_prepare_explicit_xdg"): "inspect-only",
}

#: No production site is grandfathered as requiring the exact raw exception
#: type.  Every candidate was classified for typed preservation (task 5A.7).
_JUSTIFIED_RAW_CONTRACTS: dict[tuple[str, str], str] = {}

#: Cause-access sites that intentionally keep a raw POSIX boundary because the
#: descriptor is still caller-owned or the operation is directly injected.
_PRE_ADOPTION_RAW_SITES = {
    "docker/npm_environment/publication.py": "_release_identity_lock",
    "docker/versioning/artifact_cache.py": "FileIdentityLock.release",
    "docker/versioning/rendering.py": "write_effective_build",
    "docker/versioning/effective.py": "create_runtime_projection",
}


def _cause_access_sites() -> set[tuple[str, str]]:
    """Return every ``.cause``/``.__cause__``/``getattr(..., "cause")`` site."""
    sites: set[tuple[str, str]] = set()
    for path in sorted((_REPO / "docker").rglob("*.py")):
        relative = path.relative_to(_REPO).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))

        class _Visitor(ast.NodeVisitor):
            def __init__(self) -> None:
                self.stack: list[str] = []

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                self.stack.append(node.name)
                self.generic_visit(node)
                self.stack.pop()

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                self.stack.append(node.name)
                self.generic_visit(node)
                self.stack.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Attribute(self, node: ast.Attribute) -> None:
                if node.attr in ("cause", "__cause__") and isinstance(
                    node.ctx, ast.Load
                ):
                    sites.add((relative, ".".join(self.stack)))
                self.generic_visit(node)

            def visit_Call(self, node: ast.Call) -> None:
                if (
                    isinstance(node.func, ast.Name)
                    and node.func.id == "getattr"
                    and len(node.args) >= 2
                ):
                    name = node.args[1]
                    if isinstance(name, ast.Constant) and name.value in (
                        "cause",
                        "__cause__",
                    ):
                        sites.add((relative, ".".join(self.stack)))
                self.generic_visit(node)

        _Visitor().visit(tree)
    return sites


class CauseAccessInventoryTests(unittest.TestCase):
    """Task 5A.1 — every production cause access is classified."""

    def test_every_detected_site_is_classified(self) -> None:
        sites = _cause_access_sites()
        unclassified = sites - set(_CAUSE_ACCESS_INVENTORY)
        self.assertEqual(unclassified, set())

    def test_inventory_covers_the_expected_sites(self) -> None:
        required = {
            ("docker/versioning/build_cache.py", "_validate_existing_blob"),
            ("docker/versioning/build_cache.py", "_validate_existing_marker"),
            ("docker/versioning/build_orchestration.py", "_exception_chain"),
            ("docker/versioning/host_progress.py", "_FailureContextRegistry.lookup"),
        }
        self.assertTrue(required <= set(_CAUSE_ACCESS_INVENTORY))

    def test_no_domain_adapter_is_classified_as_raw_replacement(self) -> None:
        for classification in _CAUSE_ACCESS_INVENTORY.values():
            self.assertIn(
                classification,
                {
                    "definition",
                    "typed-propagation",
                    "domain-wrapping",
                    "inspect-only",
                    "pre-adoption-raw",
                    "justified-raw",
                },
            )


# ── Task 5A.12 — reject cause unwrapping at consumer boundaries ─────────


_INSPECTION_CALLS = frozenset(
    {
        "isinstance",
        "issubclass",
        "type",
        "getattr",
        "hasattr",
        "id",
        "str",
        "repr",
        "bool",
        "format",
        "len",
    }
)

#: Escape kinds that replace or aggregate the typed wrapper with its raw
#: cause: a bare raw re-raise, a ``raise ... from cause`` chain, a raw return,
#: a returned aggregate, a constructor/call argument, or an explicit
#: ``carry_secondary_diagnostics`` transfer.
_ESCAPE_KINDS = frozenset(
    {"raise", "raise-from", "return", "return-aggregate", "aggregate", "carry"}
)

#: Typed transaction/lock wrappers that may legitimately be constructed from a
#: raw foundation cause at a ``typed-propagation`` site.  The exemption is
#: expression-specific: only ``raise <wrapper>(..., cause=cause) from cause``
#: (or the equivalent alias form) is permitted, never a bare raw re-raise.
_APPROVED_TYPED_WRAPPERS = frozenset({"TransactionError", "CapabilityError"})

#: Approved wrappers whose constructor must receive the raw cause through an
#: explicit ``cause=`` keyword (as opposed to embedding it in the message).
_WRAPPERS_REQUIRING_CAUSE_KEYWORD = frozenset({"TransactionError"})


def _is_cause_accessor(value: ast.AST | None) -> bool:
    """Return whether *value* reads a ``.cause``/``.__cause__``/``getattr``."""
    if isinstance(value, ast.Attribute):
        return value.attr in ("cause", "__cause__")
    if isinstance(value, ast.Call):
        func = value.func
        if (
            isinstance(func, ast.Name)
            and func.id == "getattr"
            and len(value.args) >= 2
        ):
            name = value.args[1]
            return isinstance(name, ast.Constant) and name.value in (
                "cause",
                "__cause__",
            )
    return False


def _cause_derived_names(function: ast.AST) -> set[str]:
    """Return names bound, transitively, to a cause accessor.

    Plain and annotated assignments are both tracked, so ``cause = exc.cause``,
    ``cause: BaseException = exc.cause``, and ``result = cause`` all classify
    ``cause``/``result`` as cause-derived.
    """
    names: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(function):
            value: ast.AST | None = None
            targets: list[ast.Name] = []
            if isinstance(node, ast.Assign):
                value = node.value
                targets = [t for t in node.targets if isinstance(t, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                value = node.value
                if isinstance(node.target, ast.Name):
                    targets = [node.target]
            if value is None:
                continue
            if _is_cause_load(value, names):
                for target in targets:
                    if target.id not in names:
                        names.add(target.id)
                        changed = True
    return names


def _is_cause_load(node: ast.AST | None, names: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in names
    return _is_cause_accessor(node)


def _is_inspection_call(node: ast.Call) -> bool:
    """Return whether *node* only inspects/renders its arguments."""
    func = node.func
    return isinstance(func, ast.Name) and func.id in _INSPECTION_CALLS


def _call_contains_cause(node: ast.AST | None, names: set[str]) -> bool:
    """Return whether *node* passes a cause-derived value as an argument.

    Only direct arguments count: a cause used to derive a larger inspected
    expression (for example ``current.failures`` inside a diagnostic format)
    is not itself aggregated by the call.
    """
    if not isinstance(node, ast.Call) or _is_inspection_call(node):
        return False
    arguments: list[ast.AST] = list(node.args) + [
        keyword.value for keyword in node.keywords
    ]
    return any(_is_cause_load(argument, names) for argument in arguments)


def _is_approved_wrapper_construction(node: ast.Call) -> bool:
    """Return whether *node* constructs an approved typed wrapper."""
    return (
        isinstance(node.func, ast.Name)
        and node.func.id in _APPROVED_TYPED_WRAPPERS
    )


def _referenced_cause_names(node: ast.AST | None, names: set[str]) -> set[str]:
    """Return the cause-derived names/direct accessors read inside *node*."""
    if node is None:
        return set()
    found: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id in names:
            found.add(sub.id)
        elif _is_cause_accessor(sub):
            found.add("<accessor>")
    return found


def _typed_wrapper_constructions(function: ast.AST) -> dict[str, list[ast.Call]]:
    """Map local names to approved typed-wrapper constructor calls."""
    constructions: dict[str, list[ast.Call]] = {}
    for node in ast.walk(function):
        value: ast.AST | None = None
        targets: list[ast.Name] = []
        if isinstance(node, ast.Assign):
            value = node.value
            targets = [t for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            value = node.value
            if isinstance(node.target, ast.Name):
                targets = [node.target]
        if value is None:
            continue
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id in _APPROVED_TYPED_WRAPPERS
        ):
            for target in targets:
                constructions.setdefault(target.id, []).append(value)
    return constructions


def _approved_wrapper_calls(
    node: ast.AST | None,
    constructions: dict[str, list[ast.Call]],
) -> list[ast.Call]:
    """Return the approved typed-wrapper calls *node* could evaluate to."""
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id in _APPROVED_TYPED_WRAPPERS:
            return [node]
        return []
    if isinstance(node, ast.Name):
        return list(constructions.get(node.id, []))
    return []


def _has_cause_keyword(
    call: ast.Call,
    chained: set[str],
    names: set[str],
) -> bool:
    """Return whether *call* stores a chained cause via ``cause=``."""
    for keyword in call.keywords:
        if keyword.arg == "cause" and (
            _referenced_cause_names(keyword.value, names) & chained
        ):
            return True
    return False


def _is_approved_typed_propagation(
    node: ast.Raise,
    names: set[str],
    constructions: dict[str, list[ast.Call]],
) -> bool:
    """Return whether *node* is a permitted typed-wrapper construction chain.

    The single permitted escape is a new approved typed wrapper built from the
    same raw cause and raised with that cause as the direct chaining cause:
    ``raise TransactionError(..., cause=cause) from cause`` (or the equivalent
    alias form).  A bare raw re-raise, a domain-error chain, a return, or an
    aggregation is never permitted, even at a ``typed-propagation`` site.
    """
    if node.cause is None:
        return False
    chained = _referenced_cause_names(node.cause, names)
    if not chained:
        return False
    for call in _approved_wrapper_calls(node.exc, constructions):
        wrapper_name = call.func.id if isinstance(call.func, ast.Name) else None
        wrapper_names = _referenced_cause_names(call, names)
        if not chained <= wrapper_names:
            continue
        if wrapper_name in _WRAPPERS_REQUIRING_CAUSE_KEYWORD and not _has_cause_keyword(
            call, chained, names
        ):
            continue
        return True
    return False


def _cause_escapes(function: ast.AST) -> list[tuple[int, str, ast.AST]]:
    """Return every site in *function* that lets a cause-derived value escape.

    A cause-derived value may be *inspected* (``isinstance``, ``type``,
    ``getattr``, ``str``/``repr``, truthiness, comparisons, and attribute
    reads such as ``exc.cause.errno``), but it must never be raised, returned,
    or passed into an aggregating constructor/function call that replaces the
    typed wrapper.
    """
    names = _cause_derived_names(function)
    issues: list[tuple[int, str, ast.AST]] = []
    for node in ast.walk(function):
        if isinstance(node, ast.Raise):
            if node.exc is not None and _is_cause_load(node.exc, names):
                issues.append((node.lineno, "raise", node))
            if node.cause is not None and _is_cause_load(node.cause, names):
                issues.append((node.lineno, "raise-from", node))
        elif isinstance(node, ast.Return):
            if node.value is not None:
                if _is_cause_load(node.value, names):
                    issues.append((node.lineno, "return", node))
                elif _call_contains_cause(node.value, names):
                    issues.append((node.lineno, "return-aggregate", node))
        elif isinstance(node, ast.Call):
            if _is_inspection_call(node) or _is_approved_wrapper_construction(node):
                continue
            if _call_contains_cause(node, names):
                kind = (
                    "carry"
                    if isinstance(node.func, ast.Name)
                    and node.func.id == "carry_secondary_diagnostics"
                    else "aggregate"
                )
                issues.append((node.lineno, kind, node))
    return issues


def _prohibited_escapes(
    function: ast.AST,
    *,
    classification: str | None = None,
) -> list[tuple[int, str]]:
    """Return escapes that are not permitted for *function*.

    Only an expression-specific approved typed-wrapper construction at a
    ``typed-propagation`` site is permitted; every other escape (including a
    bare re-raise, a return, or an aggregation inside that same function) is
    reported.
    """
    names = _cause_derived_names(function)
    constructions = _typed_wrapper_constructions(function)
    prohibited: list[tuple[int, str]] = []
    for line, kind, node in _cause_escapes(function):
        if (
            classification == "typed-propagation"
            and kind == "raise-from"
            and isinstance(node, ast.Raise)
            and _is_approved_typed_propagation(node, names, constructions)
        ):
            continue
        prohibited.append((line, kind))
    return prohibited


def _iter_functions(tree: ast.AST):
    """Yield ``(qualified_name, function)`` for every function definition."""
    stack: list[str] = []

    def walk(node: ast.AST):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                stack.append(child.name)
                yield from walk(child)
                stack.pop()
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                stack.append(child.name)
                yield ".".join(stack), child
                yield from walk(child)
                stack.pop()
            else:
                yield from walk(child)

    yield from walk(tree)


def _unwrap_sites() -> list[tuple[str, str, int, str]]:
    issues: list[tuple[str, str, int, str]] = []
    for path in sorted((_REPO / "docker").rglob("*.py")):
        relative = path.relative_to(_REPO).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function_name, function in _iter_functions(tree):
            classification = _CAUSE_ACCESS_INVENTORY.get((relative, function_name))
            for line, kind in _prohibited_escapes(
                function, classification=classification
            ):
                issues.append((relative, function_name, line, kind))
    return issues


def _escapes_from_source(
    source: str,
    *,
    classification: str | None = None,
) -> list[str]:
    """Return the prohibited escape kinds in a single synthetic function."""
    tree = ast.parse(textwrap.dedent(source))
    function = tree.body[0]
    assert isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
    return [kind for _, kind in _prohibited_escapes(function, classification=classification)]


class CauseUnwrappingGuardTests(unittest.TestCase):
    """Task 5A.12 — no consumer raises/returns/aggregates a typed cause."""

    def _allowed(self, relative: str, function: str) -> bool:
        # The only exemption for an actual raw-cause replacement is an explicit
        # per-site entry; a function-level classification grants nothing.
        return (relative, function) in _JUSTIFIED_RAW_CONTRACTS

    def test_no_unwrapping_site_exists(self) -> None:
        for relative, function, line, kind in _unwrap_sites():
            self.assertTrue(
                self._allowed(relative, function),
                f"{relative}:{line} {function} {kind}s a typed raw cause",
            )

    def test_reported_sites_use_known_escape_kinds(self) -> None:
        for _, _, _, kind in _unwrap_sites():
            self.assertIn(kind, _ESCAPE_KINDS)

    def test_no_prohibited_escape_remains_in_production(self) -> None:
        self.assertEqual(_unwrap_sites(), [])

    def test_approved_typed_propagation_is_permitted(self) -> None:
        self.assertEqual(
            _escapes_from_source(
                """
                def translate(exc):
                    cause = exc.cause
                    raise TransactionError("stage", "message", cause=cause) from cause
                """,
                classification="typed-propagation",
            ),
            [],
        )

    def test_approved_typed_propagation_alias_form_is_permitted(self) -> None:
        self.assertEqual(
            _escapes_from_source(
                """
                def translate(exc):
                    cause = exc.cause
                    error = CapabilityError(f"unsafe: {cause}")
                    raise error from cause
                """,
                classification="typed-propagation",
            ),
            [],
        )

    def test_classified_site_rejects_bare_raise_of_cause(self) -> None:
        self.assertIn(
            "raise",
            _escapes_from_source(
                """
                def translate(exc):
                    cause = exc.cause
                    raise cause
                """,
                classification="typed-propagation",
            ),
        )

    def test_classified_site_rejects_return_of_cause(self) -> None:
        self.assertIn(
            "return",
            _escapes_from_source(
                """
                def translate(exc):
                    cause = exc.cause
                    return cause
                """,
                classification="typed-propagation",
            ),
        )

    def test_classified_site_rejects_aggregation(self) -> None:
        self.assertIn(
            "aggregate",
            _escapes_from_source(
                """
                def translate(exc):
                    cause = exc.cause
                    failures = CleanupFailures(cause)
                    return failures
                """,
                classification="typed-propagation",
            ),
        )

    def test_classified_site_rejects_domain_chaining(self) -> None:
        self.assertIn(
            "raise-from",
            _escapes_from_source(
                """
                def translate(exc):
                    cause = exc.cause
                    raise DomainError("nope") from cause
                """,
                classification="typed-propagation",
            ),
        )

    def test_classified_site_rejects_wrapper_without_cause_argument(self) -> None:
        # An approved wrapper raised from the cause is only permitted when the
        # wrapper actually stores the raw cause (``cause=``) for
        # ``TransactionError``; otherwise it is a bare replacement.
        self.assertIn(
            "raise-from",
            _escapes_from_source(
                """
                def translate(exc):
                    cause = exc.cause
                    raise TransactionError("stage", "message") from cause
                """,
                classification="typed-propagation",
            ),
        )

    def test_definition_classification_grants_no_escape(self) -> None:
        # ``definition`` sites may store a supplied cause as exception state
        # (``self.cause = cause``), but that grants no permission to read a
        # typed wrapper's ``.cause`` and raise it.
        self.assertIn(
            "raise",
            _escapes_from_source(
                """
                def remap(exc):
                    cause = exc.cause
                    raise cause
                """,
                classification="definition",
            ),
        )

    def test_pre_adoption_classification_grants_no_escape(self) -> None:
        # ``pre-adoption-raw`` sites receive raw POSIX errors directly; reading a
        # typed wrapper's ``.cause`` and returning it is still prohibited.
        self.assertIn(
            "return",
            _escapes_from_source(
                """
                def release(exc):
                    cause = exc.cause
                    return cause
                """,
                classification="pre-adoption-raw",
            ),
        )

    def test_production_from_secure_path_typed_propagation_is_permitted(self) -> None:
        path = _REPO / "docker/transactions/capabilities.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        functions = dict(_iter_functions(tree))
        function = functions["DirectoryCapability.from_secure_path"]
        self.assertEqual(
            _prohibited_escapes(function, classification="typed-propagation"),
            [],
        )

    def test_mutated_from_secure_path_branch_is_rejected(self) -> None:
        # An equivalent classified function whose branch replaces the typed
        # wrapper with a bare raw re-raise must be rejected even though the real
        # function is classified ``typed-propagation``.
        source = """
            class DirectoryCapability:
                def from_secure_path(exc):
                    cause = exc.cause
                    if cause is not None:
                        raise cause
                    raise exc
        """
        tree = ast.parse(textwrap.dedent(source))
        function = tree.body[0].body[0]
        kinds = [
            kind
            for _, kind in _prohibited_escapes(
                function, classification="typed-propagation"
            )
        ]
        self.assertIn("raise", kinds)

    def test_flags_direct_raise_of_cause(self) -> None:
        self.assertIn(
            "raise",
            _escapes_from_source(
                """
                def remap(exc):
                    raise exc.cause
                """
            ),
        )

    def test_flags_aliased_raise_of_cause(self) -> None:
        self.assertIn(
            "raise",
            _escapes_from_source(
                """
                def remap(exc):
                    cause = exc.cause
                    raise cause
                """
            ),
        )

    def test_flags_domain_chaining_from_direct_cause(self) -> None:
        self.assertIn(
            "raise-from",
            _escapes_from_source(
                """
                def remap(exc):
                    raise DomainError() from exc.cause
                """
            ),
        )

    def test_flags_domain_chaining_from_alias(self) -> None:
        self.assertIn(
            "raise-from",
            _escapes_from_source(
                """
                def remap(exc):
                    cause = exc.cause
                    raise DomainError() from cause
                """
            ),
        )

    def test_flags_annotated_cause_alias_chaining(self) -> None:
        self.assertIn(
            "raise-from",
            _escapes_from_source(
                """
                def remap(exc):
                    cause: BaseException = exc.cause
                    raise DomainError() from cause
                """
            ),
        )

    def test_flags_assignment_into_aggregate(self) -> None:
        self.assertIn(
            "aggregate",
            _escapes_from_source(
                """
                def collect(exc):
                    primary = exc.cause
                    failures = CleanupFailures(primary)
                    return failures
                """
            ),
        )

    def test_flags_constructor_call_aggregation(self) -> None:
        self.assertIn(
            "aggregate",
            _escapes_from_source(
                """
                def collect(exc):
                    failures = CleanupFailures(exc.cause)
                    return failures
                """
            ),
        )

    def test_flags_method_call_aggregation(self) -> None:
        self.assertIn(
            "aggregate",
            _escapes_from_source(
                """
                def collect(exc):
                    errors = []
                    cause = exc.cause
                    errors.append(cause)
                    return errors
                """
            ),
        )

    def test_flags_transitive_alias_aggregation(self) -> None:
        self.assertIn(
            "aggregate",
            _escapes_from_source(
                """
                def collect(exc):
                    cause = exc.cause
                    result = cause
                    errors = []
                    errors.append(result)
                    return errors
                """
            ),
        )

    def test_flags_returned_raw_cause(self) -> None:
        self.assertIn(
            "return",
            _escapes_from_source(
                """
                def unwrap(exc):
                    return exc.cause
                """
            ),
        )

    def test_flags_returned_aggregate(self) -> None:
        self.assertIn(
            "return-aggregate",
            _escapes_from_source(
                """
                def unwrap(exc):
                    return AggregateFailure(exc.cause)
                """
            ),
        )

    def test_flags_carry_transfer(self) -> None:
        self.assertIn(
            "carry",
            _escapes_from_source(
                """
                def remap(exc):
                    cause = exc.cause
                    carry_secondary_diagnostics(cause, exc)
                """
            ),
        )

    def test_allows_isinstance_inspection(self) -> None:
        self.assertEqual(
            _escapes_from_source(
                """
                def inspect(exc):
                    if isinstance(exc.cause, FileNotFoundError):
                        return None
                    return None
                """
            ),
            [],
        )

    def test_allows_errno_comparison_and_attribute_read(self) -> None:
        self.assertEqual(
            _escapes_from_source(
                """
                def inspect(exc):
                    cause = exc.cause
                    return cause is not None and cause.errno == errno.ENOENT
                """
            ),
            [],
        )

    def test_allows_type_and_render_inspection(self) -> None:
        self.assertEqual(
            _escapes_from_source(
                """
                def inspect(exc):
                    cause = exc.cause
                    label = f"{type(cause).__name__}: {cause}"
                    return label
                """
            ),
            [],
        )


# ── Task 5A.7 — justify or reject every raw-contract exception ──────────


class RawContractInventoryTests(unittest.TestCase):
    """Task 5A.7 — no raw-contract exception is grandfathered."""

    def test_no_justified_raw_contract_exists(self) -> None:
        self.assertEqual(_JUSTIFIED_RAW_CONTRACTS, {})

    def test_pre_adoption_raw_boundaries_are_declared(self) -> None:
        # Raw ``OSError`` stays authoritative at these declared boundaries.
        for relative, function in _PRE_ADOPTION_RAW_SITES.items():
            source = (_REPO / relative).read_text(encoding="utf-8")
            name = function.split(".")[-1]
            self.assertIn(f"def {name}", source, (relative, function))


# ── Task 5A.2 — build-cleanup typed aggregation ─────────────────────────


class BuildCleanupAggregationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def test_typed_validation_remains_aggregated(self) -> None:
        from docker.versioning import build_cleanup

        ops = InjectedOps()
        blobs = DirectoryCapability.from_path(PosixFileOps(), self.root)
        self.addCleanup(blobs.close)
        self.addCleanup(ops.failures.clear)
        os.makedirs(os.path.join(self.root, "sha256"), mode=0o700)
        stat_error = OSError(errno.EIO, "injected factory stat")
        close_error = OSError(errno.EIO, "injected caller-owned close")
        ops.failures["fstat"] = stat_error
        ops.failures["close"] = close_error

        capability, failure = build_cleanup._open_algorithm_directory(
            ops, blobs, "sha256"
        )
        self.assertIsNone(capability)
        self.assertIsNotNone(failure)
        aggregated = failure.error
        # The typed validation failure stays the aggregated error and its exact
        # raw stat cause remains reachable through the wrapper.
        self.assertIsInstance(aggregated, TransactionError)
        self.assertEqual(aggregated.stage, STAGE_VALIDATE)
        self.assertIs(aggregated.cause, stat_error)
        self.assertIs(aggregated.__cause__, stat_error)
        # The caller-owned raw close failure is secondary on the typed wrapper,
        # never copied onto the raw cause.
        self.assertIn(close_error, aggregated.secondary)
        self.assertNotIn(
            close_error, list(getattr(stat_error, "_transaction_secondary", []))
        )

    def test_raw_close_failure_stays_raw_and_secondary(self) -> None:
        # A failure *before* adoption keeps the still caller-owned descriptor's
        # cleanup a raw POSIX boundary.
        from docker.versioning import build_cleanup

        ops = InjectedOps()
        blobs = DirectoryCapability.from_path(PosixFileOps(), self.root)
        self.addCleanup(blobs.close)
        self.addCleanup(ops.failures.clear)
        os.makedirs(os.path.join(self.root, "sha256"), mode=0o700)
        close_error = OSError(errno.EIO, "injected raw close")
        ops.failures["close"] = close_error
        ops.failures["fstat"] = OSError(errno.EIO, "injected stat")
        _, failure = build_cleanup._open_algorithm_directory(ops, blobs, "sha256")
        self.assertIn(close_error, failure.error.secondary)


# ── Task 5A.3 — project-state and rendering domain chaining ─────────────


class ProjectStateChainingTests(unittest.TestCase):
    def test_typed_capability_failure_chains_from_domain_error(self) -> None:
        from docker.versioning import project_state

        raw = OSError(errno.EIO, "injected fstat")
        wrapper = TransactionError(
            STAGE_VALIDATE, "cannot stat directory", cause=raw
        )
        secondary = OSError(errno.EACCES, "injected close")
        wrapper.add_secondary(secondary)
        with mock.patch.object(
            project_state.DirectoryCapability, "from_fd", side_effect=wrapper
        ):
            with self.assertRaises(project_state.ProjectStateError) as ctx:
                project_state._publish_metadata(0, b"payload", "label")
        # The domain error chains directly from the typed wrapper.
        self.assertIs(ctx.exception.__cause__, wrapper)
        self.assertIs(wrapper.cause, raw)
        self.assertIs(wrapper.__cause__, raw)
        # The wrapper keeps its secondary diagnostics; nothing was copied onto
        # the raw cause.
        self.assertIn(secondary, wrapper.secondary)
        self.assertNotIn(
            secondary, list(getattr(raw, "_transaction_secondary", []))
        )


class RenderingChainingTests(unittest.TestCase):
    def test_typed_factory_failure_chains_from_domain_error(self) -> None:
        from docker.versioning import rendering

        raw = OSError(errno.EIO, "injected fstat")
        wrapper = TransactionError(
            STAGE_VALIDATE, "cannot stat directory", cause=raw
        )
        secondary = OSError(errno.EACCES, "injected close")
        wrapper.add_secondary(secondary)
        with self.assertRaises(rendering.EffectiveInventoryOutputError) as ctx:
            rendering._raise_effective_failure("generated", wrapper)
        self.assertIs(ctx.exception.__cause__, wrapper)
        self.assertIs(wrapper.cause, raw)
        self.assertIn(secondary, wrapper.secondary)
        self.assertNotIn(
            secondary, list(getattr(raw, "_transaction_secondary", []))
        )


# ── Task 5A.4 — adapter propagation and domain chaining ─────────────────


class AdapterTypedPreservationTests(unittest.TestCase):
    def _lock_error(self, stage: str) -> tuple[LockError, OSError]:
        raw = OSError(errno.EIO, f"injected {stage}")
        return LockError(stage, f"cannot run {stage}", cause=raw), raw

    def test_npm_publication_preserves_typed_lock_failure(self) -> None:
        from docker.npm_environment import publication

        for stage in (
            STAGE_LOCK_STAT,
            STAGE_LOCK_MODE,
            STAGE_LOCK_ACQUIRE,
            STAGE_CLOSE,
        ):
            with self.subTest(stage=stage):
                typed, raw = self._lock_error(stage)
                mapped = publication._identity_lock_failure(typed)
                self.assertIs(mapped, typed)
                self.assertIs(mapped.cause, raw)

    def test_artifact_cache_preserves_typed_lock_failure(self) -> None:
        from docker.versioning import artifact_cache

        for stage in (
            STAGE_LOCK_STAT,
            STAGE_LOCK_MODE,
            STAGE_LOCK_ACQUIRE,
            STAGE_CLOSE,
        ):
            with self.subTest(stage=stage):
                typed, raw = self._lock_error(stage)
                mapped = artifact_cache._identity_lock_failure(typed)
                self.assertIs(mapped, typed)
                self.assertIs(mapped.cause, raw)

    def test_build_cache_preserves_typed_lock_failure(self) -> None:
        from docker.versioning import build_cache

        with tempfile.TemporaryDirectory() as root:
            directory = DirectoryCapability.from_path(PosixFileOps(), root)
            self.addCleanup(directory.close)
            for stage in (
                STAGE_LOCK_STAT,
                STAGE_LOCK_MODE,
                STAGE_LOCK_ACQUIRE,
                STAGE_CLOSE,
            ):
                with self.subTest(stage=stage):
                    typed, raw = self._lock_error(stage)
                    with self.assertRaises(LockError) as ctx:
                        build_cache._raise_lock_failure(
                            directory, "missing.lock", typed
                        )
                    self.assertIs(ctx.exception, typed)
                    self.assertIs(ctx.exception.cause, raw)

    def test_build_cache_marker_failure_preserves_typed_wrapper(self) -> None:
        from docker.versioning import build_cache

        raw = OSError(errno.EIO, "injected replacement")
        wrapper = TransactionError("replace", "cannot replace", cause=raw)
        with self.assertRaises(TransactionError) as ctx:
            build_cache._raise_marker_failure("state.json", wrapper)
        self.assertIs(ctx.exception, wrapper)
        self.assertIs(ctx.exception.cause, raw)

    def test_effective_config_chains_typed_failure(self) -> None:
        from docker.versioning import effective

        raw = OSError(errno.EIO, "injected open")
        wrapper = TransactionError("open-directory", "cannot open", cause=raw)

        with mock.patch.object(effective, "_validate_safe_path"), mock.patch.object(
            effective.DirectoryCapability,
            "from_secure_path",
            side_effect=wrapper,
        ):
            with self.assertRaises(effective.EffectiveConfigError) as ctx:
                effective.create_runtime_projection(
                    _minimal_projection(),
                    host_path="/tmp/x.toml",
                    _fs=_fake_projection_filesystem(),
                )
        self.assertIs(ctx.exception.__cause__, wrapper)
        self.assertIs(wrapper.cause, raw)

    def test_effective_config_propagates_safety_capability_failure(self) -> None:
        from docker.versioning import effective

        raw = OSError(errno.ENOTDIR, "injected no-follow")
        safety = CapabilityError("runtime projection path is unsafe")
        safety.__cause__ = raw

        with mock.patch.object(effective, "_validate_safe_path"), mock.patch.object(
            effective.DirectoryCapability,
            "from_secure_path",
            side_effect=safety,
        ):
            with self.assertRaises(CapabilityError) as ctx:
                effective.create_runtime_projection(
                    _minimal_projection(),
                    host_path="/tmp/x.toml",
                    _fs=_fake_projection_filesystem(),
                )
        # A no-follow safety rejection is not an operational transaction
        # failure: it keeps its original ``CapabilityError`` classification and
        # is propagated unchanged, with the raw platform error reachable as its
        # direct cause.
        self.assertIs(ctx.exception, safety)
        self.assertIs(ctx.exception.__cause__, raw)
        self.assertNotIsInstance(ctx.exception, effective.EffectiveConfigError)

    def test_effective_config_propagates_process_control_interruption(self) -> None:
        from docker.versioning import effective

        interrupt = KeyboardInterrupt()

        with mock.patch.object(effective, "_validate_safe_path"), mock.patch.object(
            effective.DirectoryCapability,
            "from_secure_path",
            side_effect=interrupt,
        ):
            with self.assertRaises(KeyboardInterrupt) as ctx:
                effective.create_runtime_projection(
                    _minimal_projection(),
                    host_path="/tmp/x.toml",
                    _fs=_fake_projection_filesystem(),
                )
        self.assertIs(ctx.exception, interrupt)

    def test_render_failure_preserves_typed_wrapper(self) -> None:
        from docker.versioning import rendering

        raw = OSError(errno.EIO, "injected replace")
        wrapper = TransactionError("replace", "cannot replace", cause=raw)
        with self.assertRaises(rendering.EffectiveInventoryOutputError) as ctx:
            rendering._raise_effective_failure("docker-constructor.build.effective.toml", wrapper)
        self.assertIs(ctx.exception.__cause__, wrapper)
        self.assertIs(wrapper.cause, raw)


def _minimal_projection():
    from docker.versioning.model import EffectiveRuntimeProjection

    return EffectiveRuntimeProjection(extensions={})


def _fake_projection_filesystem():
    """Build an injected filesystem shim for ``create_runtime_projection``."""

    class _FakePath:
        @staticmethod
        def dirname(path: str) -> str:
            return os.path.dirname(path)

        @staticmethod
        def basename(path: str) -> str:
            return os.path.basename(path)

    class _FakeFilesystem:
        ops = PosixFileOps()
        path = _FakePath()

    return _FakeFilesystem()


# ── Task 5A.5 — lock-release cleanup classification ─────────────────────


class LockReleaseCleanupTests(unittest.TestCase):
    def _lock_error(self) -> LockError:
        return LockError(
            STAGE_CLOSE, "cannot release lock", cause=OSError(errno.EIO, "unlock")
        )

    def test_cleanup_classifies_typed_lock_error_directly(self) -> None:
        typed = self._lock_error()
        in_flight: list[Exception] = []

        def action() -> None:
            try:
                raise typed
            except LockError as exc:
                in_flight.append(exc)
                raise

        failures = CleanupFailures(None)
        failures.run(action, ordinary=(LockError,))
        result = failures.complete()
        self.assertIs(result, typed)
        self.assertEqual(in_flight, [typed])

    def test_active_primary_is_preserved_with_typed_secondary(self) -> None:
        primary = RuntimeError("body failed")
        typed = self._lock_error()
        failures = CleanupFailures(primary)
        failures.run(lambda: (_ for _ in ()).throw(typed), ordinary=(LockError,))
        result = failures.complete()
        self.assertIsNone(result)
        self.assertIn(typed, list(primary._transaction_secondary))

    def test_every_independent_release_is_attempted_once(self) -> None:
        calls: list[str] = []

        def first() -> None:
            calls.append("first")
            raise self._lock_error()

        def second() -> None:
            calls.append("second")

        failures = CleanupFailures(None)
        failures.run(first, ordinary=(LockError,))
        failures.run(second, ordinary=(LockError,))
        result = failures.complete()
        self.assertIsInstance(result, LockError)
        self.assertEqual(calls, ["first", "second"])

    def test_process_control_propagates_over_typed_lock_error(self) -> None:
        interrupt = KeyboardInterrupt()

        def first() -> None:
            raise self._lock_error()

        def second() -> None:
            raise interrupt

        failures = CleanupFailures(None)
        failures.run(first, ordinary=(LockError,))
        failures.run(second, ordinary=(LockError,))
        with self.assertRaises(KeyboardInterrupt) as ctx:
            failures.complete()
        self.assertIs(ctx.exception, interrupt)

    def test_shared_and_transaction_cleanup_are_the_same_object(self) -> None:
        self.assertIs(CleanupFailures, TransactionCleanupFailures)


class ConstructorProjectBuildLockExitPolicyTests(unittest.TestCase):
    """Task 5A.5 — the lock's ``__exit__`` keeps typed release failures typed.

    ``ConstructorProjectBuildLock.__exit__`` must treat only the expected typed
    release failures (a shared ``LockError`` and a close-stage
    ``TransactionError``) as ordinary cleanup diagnostics.  Every other release
    defect stays authoritative over an in-flight body exception.
    """

    def _lock(self, capability: mock.Mock, directory: mock.Mock) -> ConstructorProjectBuildLock:
        return ConstructorProjectBuildLock(
            ops=mock.Mock(),
            capability=capability,
            generation_directory=directory,
            constructor_project_root=Path("/project"),
            cache_root=Path("/cache"),
            namespace=Path("/namespace"),
            project_state=mock.Mock(),
        )

    def _exit_with_body_error(
        self, lock: ConstructorProjectBuildLock
    ) -> BaseException:
        with self.assertRaises(RuntimeError) as ctx:
            with lock:
                raise RuntimeError("body failure")
        return ctx.exception

    def test_typed_lock_error_is_secondary_to_active_body_exception(self) -> None:
        capability = mock.Mock()
        lock_error = LockError(
            STAGE_CLOSE, "cannot release lock", cause=OSError(errno.EIO, "unlock")
        )
        capability.close.side_effect = lock_error
        lock = self._lock(capability, mock.Mock())
        body = self._exit_with_body_error(lock)
        self.assertIsInstance(body, RuntimeError)
        self.assertEqual(str(body), "body failure")
        self.assertIn(lock_error, list(body._transaction_secondary))

    def test_close_stage_transaction_error_is_secondary_to_active_body_exception(
        self,
    ) -> None:
        directory = mock.Mock()
        close_failure = TransactionError(STAGE_CLOSE, "cannot close generation directory")
        directory.close.side_effect = close_failure
        lock = self._lock(mock.Mock(), directory)
        body = self._exit_with_body_error(lock)
        self.assertIsInstance(body, RuntimeError)
        self.assertIn(close_failure, list(body._transaction_secondary))

    def test_unexpected_raw_oserror_from_release_is_authoritative(self) -> None:
        capability = mock.Mock()
        raw = OSError(errno.EIO, "unlock")
        capability.close.side_effect = raw
        lock = self._lock(capability, mock.Mock())
        with self.assertRaises(OSError) as ctx:
            with lock:
                raise RuntimeError("body failure")
        self.assertIs(ctx.exception, raw)
        secondary = list(getattr(raw, "_transaction_secondary", []))
        self.assertTrue(
            any(isinstance(item, RuntimeError) for item in secondary), secondary
        )

    def test_unexpected_runtime_error_from_release_is_authoritative(self) -> None:
        capability = mock.Mock()
        defect = RuntimeError("unexpected release defect")
        capability.close.side_effect = defect
        lock = self._lock(capability, mock.Mock())
        with self.assertRaises(RuntimeError) as ctx:
            with lock:
                raise RuntimeError("body failure")
        self.assertIs(ctx.exception, defect)
        body = next(
            item
            for item in list(getattr(defect, "_transaction_secondary", []))
            if isinstance(item, RuntimeError) and str(item) == "body failure"
        )
        self.assertEqual(str(body), "body failure")

    def test_process_control_interruption_from_release_is_authoritative(self) -> None:
        capability = mock.Mock()
        interrupt = KeyboardInterrupt()
        capability.close.side_effect = interrupt
        lock = self._lock(capability, mock.Mock())
        with self.assertRaises(KeyboardInterrupt) as ctx:
            with lock:
                raise RuntimeError("body failure")
        self.assertIs(ctx.exception, interrupt)


# ── Task 5A.6 — legitimate inspection / intentional raw boundaries ──────


class LegitimateCauseInspectionTests(unittest.TestCase):
    def test_missing_marker_read_keeps_typed_wrapper(self) -> None:
        from docker.versioning import build_cache

        raw = FileNotFoundError(errno.ENOENT, "missing marker")
        wrapper = TransactionError(STAGE_VALIDATE, "cannot read", cause=raw)

        class _Contracts:
            def __init__(self, ops) -> None:
                pass

            def validated_read(self, directory, name, allowed_mode):
                raise wrapper

        with tempfile.TemporaryDirectory() as root:
            directory = DirectoryCapability.from_path(PosixFileOps(), root)
            self.addCleanup(directory.close)
            with mock.patch.object(
                build_cache, "RegularFileContracts", _Contracts
            ):
                with self.assertRaises(TransactionError) as ctx:
                    build_cache._read_marker_json(
                        PosixFileOps(), directory, "missing.json"
                    )
        self.assertIs(ctx.exception, wrapper)
        self.assertIs(ctx.exception.cause, raw)

    def test_absent_blob_inspection_returns_without_replacement(self) -> None:
        from docker.versioning import build_cache
        from docker.versioning.digest_identity import DigestIdentity

        raw = FileNotFoundError(errno.ENOENT, "missing blob")
        wrapper = TransactionError(STAGE_VALIDATE, "cannot open", cause=raw)

        class _Directory:
            label = "blobs"

            def open_regular(self, name, *, allowed_mode):
                raise wrapper

        # ``_validate_existing_blob`` selects the absence outcome from the typed
        # failure's cause and returns without replacing the wrapper.
        build_cache._validate_existing_blob(
            _Directory(), DigestIdentity("sha256", bytes(32))
        )

    def test_no_follow_safety_rejection_keeps_capability_error(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            real = os.path.join(root, "real")
            os.mkdir(real, mode=0o700)
            link = os.path.join(root, "link")
            os.symlink(real, link)
            ops = InjectedOps()
            with self.assertRaises(CapabilityError) as ctx:
                DirectoryCapability.from_secure_path(
                    ops, os.path.join(link, "child")
                )
        raw = ctx.exception.__cause__
        self.assertIsInstance(raw, OSError)
        self.assertIn(raw.errno, (errno.ELOOP, errno.ENOTDIR))

    def test_direct_posix_boundary_preserves_raw_error(self) -> None:
        ops = PosixFileOps()
        raw = OSError(errno.EIO, "injected")
        with mock.patch.object(PosixFileOps, "close", side_effect=raw):
            with self.assertRaises(OSError) as ctx:
                ops.close(0)
        self.assertIs(ctx.exception, raw)

    def test_pre_adoption_raw_close_stays_raw_oserror(self) -> None:
        # ``DownstreamCleanup`` demonstrates the pre-adoption boundary: a
        # caller-owned descriptor close is a raw POSIX operation.
        ops = InjectedOps()
        raw = OSError(errno.EIO, "injected raw close")
        ops.failures["close"] = raw
        with self.assertRaises(OSError) as ctx:
            ops.close(0)
        self.assertIs(ctx.exception, raw)


# ── Task 5A.13 — one coherent exception graph ───────────────────────────


class ExceptionGraphTests(unittest.TestCase):
    def test_lock_release_graph_is_domain_then_typed_then_raw(self) -> None:
        from docker.npm_environment import publication

        raw = OSError(errno.EIO, "injected probe close")
        typed = LockError(STAGE_CLOSE, "cannot release lock", cause=raw)
        mapped = publication._identity_lock_failure(typed)
        self.assertIs(mapped, typed)
        self.assertIs(mapped.cause, raw)
        self.assertIs(mapped.__cause__, raw)

    def test_domain_error_graph_chains_through_typed_failure(self) -> None:
        from docker.versioning import rendering

        raw = OSError(errno.EIO, "injected replace")
        typed = TransactionError("replace", "cannot replace", cause=raw)
        with self.assertRaises(rendering.EffectiveInventoryOutputError) as ctx:
            rendering._raise_effective_failure("destination", typed)
        graph = [ctx.exception]
        current = ctx.exception.__cause__
        while current is not None:
            graph.append(current)
            current = current.__cause__
        self.assertEqual(graph[0].__class__, rendering.EffectiveInventoryOutputError)
        self.assertIs(graph[1], typed)
        self.assertIs(graph[2], raw)

    def test_secondary_diagnostics_are_not_duplicated(self) -> None:
        from docker.versioning import rendering

        raw = OSError(errno.EIO, "injected replace")
        typed = TransactionError("replace", "cannot replace", cause=raw)
        secondary = OSError(errno.EACCES, "injected close")
        typed.add_secondary(secondary)
        with self.assertRaises(rendering.EffectiveInventoryOutputError):
            rendering._raise_effective_failure("destination", typed)
        self.assertEqual(list(typed.secondary).count(secondary), 1)
        self.assertNotIn(
            secondary, list(getattr(raw, "_transaction_secondary", []))
        )


# ── Task 5A.14 — ownership-boundary inventory ───────────────────────────


class OwnershipBoundaryInventoryTests(unittest.TestCase):
    def test_rendering_close_boundary_splits_by_adoption_state(self) -> None:
        source = (_REPO / "docker/versioning/rendering.py").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "failures.run(generated.close, ordinary=(CloseStageFailure,))", source
        )
        self.assertIn("failures.run(lambda: ops.close(generated_fd), ordinary=(OSError,))", source)
        self.assertIn("if generated is not None:", source)
        self.assertIn("elif generated_fd is not None:", source)

    def test_successful_adoption_never_falls_back_to_raw_release(self) -> None:
        # After adoption the rendering boundary must release through the
        # capability, not the raw descriptor.
        source = (_REPO / "docker/versioning/rendering.py").read_text(
            encoding="utf-8"
        )
        finally_block = source.split("finally:", 1)[1]
        self.assertIn("if generated is not None:", finally_block)
        self.assertIn("elif generated_fd is not None:", finally_block)

    def test_build_cleanup_raw_close_is_pre_adoption_only(self) -> None:
        source = (_REPO / "docker/versioning/build_cleanup.py").read_text(
            encoding="utf-8"
        )
        # The raw close of the still caller-owned descriptor remains a POSIX
        # boundary; the typed failure is aggregated unchanged.
        self.assertIn("accumulator.run(lambda: ops.close(fd), ordinary=(OSError,))", source)
        self.assertIn("_algorithm_failure(algorithm, exc)", source)
        self.assertNotIn("reported: BaseException = cause if isinstance(cause, OSError) else exc", source)


if __name__ == "__main__":
    unittest.main()
