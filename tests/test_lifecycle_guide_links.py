"""Validate guide links, including line anchors to named executable tests."""
from __future__ import annotations

import ast
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUIDES = ("process-lifecycle.md", "npm-environment-assembler.md")
LINK = re.compile(r'\[([^\]]+)\]\(([^\s)]+)(?:\s+"([^"]+)")?\)')


def _test_definitions(source: str) -> dict[int, str]:
    definitions = {}

    def visit(node, parents=()):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, (*parents, child.name))
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if child.name.startswith("test_"):
                    definitions[child.lineno] = ".".join((*parents, child.name))
                # Nested helper functions are not independent executable tests.
            else:
                visit(child, parents)

    visit(ast.parse(source))
    return definitions


def _check_local_link(guide: Path, url: str, symbol: str | None) -> None:
    parsed = urlsplit(url)
    assert not parsed.scheme and not parsed.netloc, f"not a local link: {url}"
    target = (guide.parent / unquote(parsed.path)).resolve()
    assert target.is_relative_to(ROOT), f"link escapes repository: {url}"
    assert target.is_file(), f"missing link target: {url}"
    if target.is_relative_to(ROOT / "tests"):
        assert target.name.startswith("test_") and target.suffix == ".py", url
        assert re.fullmatch(r"L[1-9][0-9]*", parsed.fragment), f"missing/invalid test line: {url}"
        assert symbol, f"test link needs its qualified name in the Markdown title: {url}"
        definitions = _test_definitions(target.read_text(encoding="utf-8"))
        assert definitions.get(int(parsed.fragment[1:])) == symbol, (
            f"test moved or renamed: {url} (expected {symbol})"
        )
    else:
        # These guides currently link whole local documents, not heading anchors.
        assert not parsed.fragment, f"unchecked document anchor: {url}"


@pytest.mark.parametrize("name", GUIDES)
def test_lifecycle_guide_links_resolve_to_documents_and_named_tests(name):
    guide = ROOT / "docs" / name
    links = LINK.findall(guide.read_text(encoding="utf-8"))
    assert links, f"no links found in {guide}"
    test_links = 0
    for label, url, symbol in links:
        assert label.strip(), f"empty link label: {url}"
        _check_local_link(guide, url, symbol or None)
        if symbol:
            test_links += 1
    assert test_links == 6, f"missing contract scenarios in {guide}"


@pytest.mark.parametrize("url,symbol", [
    ("../tests/test_missing_lifecycle_example.py#L1", "test_missing"),
    ("../../outside-repository.py#L1", "test_missing"),
    ("../tests/test_process_single_collection.py", "test_reject_repeated_and_mixed_collection_without_work"),
    ("../tests/test_process_single_collection.py#L0", "test_reject_repeated_and_mixed_collection_without_work"),
    ("../tests/test_process_single_collection.py#L1", "test_reject_repeated_and_mixed_collection_without_work"),
    ("../tests/test_process_single_collection.py#L63", "test_renamed"),
    ("../tests/test_process_single_collection.py#L63", None),
    ("process-lifecycle.md#missing-heading", None),
])
def test_broken_or_misdirected_guide_links_are_rejected(url, symbol):
    with pytest.raises(AssertionError):
        _check_local_link(ROOT / "docs/process-lifecycle.md", url, symbol)


def test_test_definition_lookup_handles_class_methods_but_not_nested_helpers():
    source = "class Examples:\n    def test_method(self):\n        def test_helper(): pass\n"
    assert _test_definitions(source) == {2: "Examples.test_method"}
