"""Phase 10 rollout, legacy isolation, and final acceptance contracts."""
from __future__ import annotations

import ast
import unittest
from pathlib import Path


_ROOT = Path(__file__).parents[1]
_CUTOVER = _ROOT / "docs" / "development-build-cache-cutover.md"
_EXPECTED_COMMAND = "python - <<'PY'"


class CutoverDocumentationTests(unittest.TestCase):
    """The destructive instruction must resolve exactly one project's cache."""

    def test_instruction_is_exact_project_resolved_and_narrow(self) -> None:
        text = _CUTOVER.read_text(encoding="utf-8")
        self.assertEqual(text.count(_EXPECTED_COMMAND), 1)
        self.assertIn("resolve_project_state(Path.cwd(), create=False)", text)
        self.assertIn("target = state.build_artifacts_root", text)
        self.assertIn("if target.name != \"build-artifacts\"", text)
        self.assertIn("shutil.rmtree(target)", text)
        self.assertIn("Run from the constructor project root", text)

    def test_instruction_rejects_broad_or_approximate_removal(self) -> None:
        text = _CUTOVER.read_text(encoding="utf-8")
        forbidden = (
            "rm -rf ~/.cache",
            "rm -rf ${XDG_CACHE_HOME}",
            "rm -rf $XDG_CACHE_HOME",
            "rm -rf *",
            "projects/*",
            "rmtree(state.cache_root)",
            "rmtree(state.project_root)",
        )
        for guidance in forbidden:
            with self.subTest(guidance=guidance):
                self.assertNotIn(guidance, text)

    def test_embedded_python_is_syntactically_valid(self) -> None:
        text = _CUTOVER.read_text(encoding="utf-8")
        snippet = text.split("```sh\n", 1)[1].split("\n```", 1)[0]
        source = snippet.split("\n", 1)[1].rsplit("\nPY", 1)[0]
        ast.parse(source)


if __name__ == "__main__":
    unittest.main()
