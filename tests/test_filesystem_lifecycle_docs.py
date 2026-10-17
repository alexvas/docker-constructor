"""Phase 6B tasks 6B.11–6B.13/6B.16 — executable developer lifecycle guide.

The developer contract in ``docs/filesystem-lifecycle.md`` must stay aligned
with the implemented API: every positive example executes, the documented
negative examples are present, the developer-guide cross-references resolve,
and the guide names the convergence gates that enforce it.
"""
from __future__ import annotations

import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_GUIDE = _REPO / "docs" / "filesystem-lifecycle.md"


def _executable_examples() -> list[str]:
    lines = _GUIDE.read_text(encoding="utf-8").splitlines()
    collecting = False
    in_block = False
    blocks: list[str] = []
    current: list[str] = []
    for line in lines:
        if line.startswith("## "):
            if collecting and line.strip() != "## Executable examples":
                break
            collecting = line.strip() == "## Executable examples"
            continue
        if not collecting:
            continue
        if line.strip().startswith("```python"):
            in_block = True
            current = []
            continue
        if in_block and line.strip() == "```":
            in_block = False
            blocks.append("\n".join(current))
            continue
        if in_block:
            current.append(line)
    return blocks


class ExecutableExampleTests(unittest.TestCase):
    def test_guide_has_executable_examples(self) -> None:
        examples = _executable_examples()
        self.assertEqual(len(examples), 3)
        # Fewer examples still cover each ownership choice and borrowed fd I/O.
        source = "\n".join(examples)
        for token in ("OwnedDescriptor(", "closefd=False", "OwnedStream(",
                      "DirectoryDescriptor.open_secure_path(", "open_or_create_directory("):
            with self.subTest(api=token):
                self.assertIn(token, source)

    def test_every_positive_example_executes(self) -> None:
        for index, source in enumerate(_executable_examples()):
            with self.subTest(example=index):
                namespace: dict = {"__name__": "docs_example"}
                exec(compile(source, f"<docs-example-{index}>", "exec"), namespace)


class GuideContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.guide = _GUIDE.read_text(encoding="utf-8")

    def test_documents_wrapper_selection_and_ownership(self) -> None:
        for token in (
            "OwnedDescriptor",
            "DirectoryDescriptor",
            "OwnedStream",
            "closefd=False",
            "detach()",
            "CleanupFailures",
            "ownership handoff",
            "primitive",
            "shared-owner internal release",
        ):
            with self.subTest(token=token):
                self.assertIn(token, self.guide)

    def test_documents_negative_examples(self) -> None:
        for token in (
            "Implicit `fdopen` ownership",
            "Double raw-fileno ownership",
            "Repeated owner reconstruction",
            "Borrowed release",
        ):
            with self.subTest(token=token):
                self.assertIn(token, self.guide)

    def test_names_the_convergence_enforcement(self) -> None:
        self.assertIn("test_descriptor_close_convergence.py", self.guide)
        self.assertIn("test_filesystem_convergence.py", self.guide)

    def test_explicit_submodule_import_rule(self) -> None:
        self.assertIn("never import the aggregate", self.guide)


class CrossReferenceTests(unittest.TestCase):
    def test_readmes_link_the_guide_and_agents_rule(self) -> None:
        for name in ("README.md", "README.en.md", "README.zh.md"):
            with self.subTest(readme=name):
                text = (_REPO / name).read_text(encoding="utf-8")
                self.assertIn("docs/filesystem-lifecycle.md", text)
                self.assertIn("AGENTS.md", text)

    def test_root_agents_links_the_guide(self) -> None:
        text = (_REPO / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("docs/filesystem-lifecycle.md", text)
        self.assertIn("OwnedDescriptor", text)
        self.assertIn("OwnedStream", text)
        self.assertIn("closefd=False", text)

    def test_openspec_config_states_the_rule(self) -> None:
        text = (_REPO / "openspec" / "config.yaml").read_text(encoding="utf-8")
        self.assertIn("docs/filesystem-lifecycle.md", text)
        self.assertIn("docker.filesystem", text)

    def test_assembler_doc_links_the_guide(self) -> None:
        text = (_REPO / "docs" / "npm-environment-assembler.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("filesystem-lifecycle.md", text)
        self.assertIn("OwnedStream", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
