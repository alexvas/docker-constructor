"""Phase 5A — typed-error preservation across consumer boundaries.

Phase 9B once required domain adapters to remap a transaction/capability
wrapper to its raw operational cause through the narrow
:func:`docker.transactions.errors.carry_secondary_diagnostics` operation.
Phase 5A supersedes that contract: a successful L1 or lock-layer normalization
is no longer undone at a consumer boundary.  Domain adapters preserve the
typed failure unchanged or chain their own domain error directly from it, so
no production domain module replaces a typed failure with its raw cause.

Cause inspection that drives a control-flow or domain decision (for example
recognizing ``FileNotFoundError``) is still permitted; only exception
replacement is rejected.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

from docker import transactions

_REPO = Path(__file__).resolve().parents[1]

#: Modules below the consumer layer that legitimately use the shared
#: diagnostic-copy primitive to forward retained diagnostics onto a *new*
#: typed wrapper (never onto a raw cause).
_FOUNDATION_MODULES = ("docker/transactions/", "docker/filesystem/")

#: Two non-remap aggregators keep the low-level ``attach_secondary`` mechanism.
_EXPLICIT_AGGREGATORS = (
    "docker/transactions/locking.py",
    "docker/versioning/build_orchestration.py",
)


def _source(path: str) -> str:
    return (_REPO / path).read_text(encoding="utf-8")


def _calls(path: str) -> dict[str, list[ast.Call]]:
    tree = ast.parse(_source(path))
    found: dict[str, list[ast.Call]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            found.setdefault(node.func.id, []).append(node)
    return found


class TypedPreservationTests(unittest.TestCase):
    def test_no_domain_module_remaps_a_typed_cause(self) -> None:
        for path in _REPO.glob("docker/**/*.py"):
            relative = path.relative_to(_REPO).as_posix()
            if relative.startswith(_FOUNDATION_MODULES):
                continue
            self.assertNotIn(
                "carry_secondary_diagnostics",
                _source(relative),
                relative,
            )

    def test_domain_remaps_do_not_read_shared_storage_or_lists(self) -> None:
        # The former remap files must not fall back to raw diagnostic storage
        # or build an arbitrary secondary iterable now that they preserve the
        # typed wrapper directly.
        for relative in (
            "docker/npm_environment/publication.py",
            "docker/versioning/rendering.py",
            "docker/versioning/artifact_cache.py",
            "docker/versioning/build_cache.py",
            "docker/versioning/effective.py",
            "docker/versioning/project_state.py",
        ):
            source = _source(relative)
            self.assertFalse("_transaction_secondary" in source, relative)
            self.assertFalse("list(exc.secondary)" in source, relative)
            self.assertFalse("attach_secondary" in source, relative)

    def test_build_cleanup_aggregates_the_typed_failure(self) -> None:
        calls = _calls("docker/versioning/build_cleanup.py")
        self.assertEqual(calls.get("carry_secondary_diagnostics", []), [])
        source = _source("docker/versioning/build_cleanup.py")
        self.assertIn("_algorithm_failure(algorithm, exc)", source)


class ExplicitAggregatorTests(unittest.TestCase):
    def test_lock_descriptor_release_aggregation_stays_explicit(self) -> None:
        calls = _calls("docker/transactions/locking.py")
        self.assertEqual(len(calls.get("carry_secondary_diagnostics", [])), 0)
        attach = calls.get("attach_secondary", [])
        self.assertEqual(len(attach), 1)
        rendered = ast.unparse(attach[0])
        self.assertIn("reported", rendered)

    def test_caller_owned_build_lock_release_reporting_stays_explicit(self) -> None:
        calls = _calls("docker/versioning/build_orchestration.py")
        self.assertEqual(len(calls.get("carry_secondary_diagnostics", [])), 0)
        attach = calls.get("attach_secondary", [])
        self.assertEqual(len(attach), 1)
        self.assertEqual(ast.unparse(attach[0]), "attach_secondary(primary, [release_exc])")


class CarryExportBoundaryTests(unittest.TestCase):
    def test_carry_is_not_a_top_level_export(self) -> None:
        self.assertNotIn("carry_secondary_diagnostics", transactions.__all__)
        self.assertFalse(hasattr(transactions, "carry_secondary_diagnostics"))

    def test_carry_storage_stays_centralized_in_errors(self) -> None:
        from docker.transactions import errors

        self.assertTrue(hasattr(errors, "carry_secondary_diagnostics"))
        self.assertTrue(hasattr(errors, "attach_secondary"))


if __name__ == "__main__":
    unittest.main()
