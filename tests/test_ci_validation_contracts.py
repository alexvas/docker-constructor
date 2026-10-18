"""Static contracts for repository validation automation."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "validation.yml"


class TestTypeCheckCiContract(unittest.TestCase):
    def test_clean_ci_job_runs_canonical_type_check(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")

        self.assertRegex(workflow, r'python-version:\s*["\']3\.14["\']')
        self.assertRegex(workflow, r"\bty==0\.0\.73\b")
        self.assertRegex(workflow, r"run:\s*scripts/check-types\s*(?:\n|$)")
        self.assertNotIn("--exit-zero", workflow)

    def test_external_actions_are_immutably_pinned(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        uses = re.findall(r"^\s*uses:\s*(\S+)", workflow, flags=re.MULTILINE)

        self.assertTrue(uses)
        for action in uses:
            with self.subTest(action=action):
                self.assertRegex(action, r"@[0-9a-f]{40}$")


if __name__ == "__main__":
    unittest.main()
