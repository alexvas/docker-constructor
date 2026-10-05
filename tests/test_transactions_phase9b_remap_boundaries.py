"""Phase 9B task 9B.2 - remap-boundary and architecture tests.

Twelve audited domain adapters remap a transaction/capability wrapper to its
raw operational cause.  They must use the narrow
:func:`docker.transactions.errors.carry_secondary_diagnostics` operation rather
than reading shared diagnostic storage or building an arbitrary secondary list.
Exactly two non-remap aggregators keep the low-level ``attach_secondary``
mechanism.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

from docker import transactions

_REPO = Path(__file__).resolve().parents[1]
_REMAP_FILES = {
    "docker/npm_environment/publication.py": 2,
    "docker/versioning/rendering.py": 1,
    "docker/versioning/artifact_cache.py": 2,
    "docker/versioning/build_cache.py": 5,
    "docker/versioning/effective.py": 2,
}
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


class RemapSiteTests(unittest.TestCase):
    def test_every_audited_site_uses_the_narrow_carry_operation(self) -> None:
        total = 0
        for path, expected in _REMAP_FILES.items():
            calls = _calls(path).get("carry_secondary_diagnostics", [])
            self.assertEqual(len(calls), expected, path)
            total += len(calls)
        self.assertEqual(total, 12)

    def test_remap_calls_pass_cause_and_wrapper_positionally(self) -> None:
        for path in _REMAP_FILES:
            for call in _calls(path).get("carry_secondary_diagnostics", []):
                self.assertEqual(len(call.args), 2, path)
                self.assertIsInstance(call.args[0], ast.Name)
                self.assertEqual(call.args[0].id, "cause", path)
                self.assertIsInstance(call.args[1], ast.Name)
                self.assertEqual(call.args[1].id, "exc", path)

    def test_domain_remaps_do_not_read_shared_storage_or_lists(self) -> None:
        for path in _REMAP_FILES:
            source = _source(path)
            self.assertFalse("_transaction_secondary" in source, path)
            self.assertFalse("list(exc.secondary)" in source, path)
            self.assertFalse("attach_secondary" in source, path)

    def test_domain_remaps_do_not_construct_a_secondary_iterable(self) -> None:
        for path in _REMAP_FILES:
            for call in _calls(path).get("carry_secondary_diagnostics", []):
                for arg in call.args:
                    self.assertFalse(
                        isinstance(arg, (ast.List, ast.ListComp, ast.GeneratorExp)),
                        path,
                    )
                self.assertFalse(any(kw.arg for kw in call.keywords), path)


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

    def test_no_other_domain_module_uses_carry(self) -> None:
        allowed = set(_REMAP_FILES) | set(_EXPLICIT_AGGREGATORS)
        for path in _REPO.glob("docker/**/*.py"):
            relative = path.relative_to(_REPO).as_posix()
            if (
                relative in allowed
                or relative.startswith("docker/transactions/")
                or relative.startswith("docker/filesystem/")
            ):
                continue
            self.assertFalse(
                "carry_secondary_diagnostics" in path.read_text(encoding="utf-8"),
                relative,
            )


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
