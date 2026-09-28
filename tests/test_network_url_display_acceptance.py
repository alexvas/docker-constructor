"""Release acceptance matrix for the network URL display policies."""
from __future__ import annotations

import json
import unittest
from pathlib import Path


class TestNetworkUrlDisplayAcceptanceMatrix(unittest.TestCase):
    REQUIRED_ROWS = {
        "interactive_text_all_policies",
        "noninteractive_lines_all_policies",
        "text_failure_all_policies",
        "json_failure_all_policies",
        "simultaneous_sdk_all_policies",
        "success_all_policies",
        "ordinary_failure_all_policies",
        "timeout_all_policies",
        "interruption_all_policies",
    }

    def test_cross_capability_matrix(self) -> None:
        path = Path(__file__).parent / "data" / "network_url_display_acceptance_matrix.json"
        matrix = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(self.REQUIRED_ROWS, set(matrix))

        loader = unittest.defaultTestLoader
        for row, test_ids in matrix.items():
            self.assertTrue(test_ids, row)
            for test_id in test_ids:
                with self.subTest(row=row, test_id=test_id):
                    suite = loader.loadTestsFromName(test_id)
                    result = unittest.TestResult()
                    suite.run(result)
                    details = "\n".join(
                        text for _test, text in (*result.failures, *result.errors)
                    )
                    self.assertEqual(
                        1,
                        result.testsRun,
                        f"{test_id!r} did not resolve exactly once:\n{details}",
                    )
                    self.assertTrue(
                        result.wasSuccessful(),
                        f"acceptance row {row!r} failed at {test_id!r}:\n{details}",
                    )


if __name__ == "__main__":
    unittest.main()
